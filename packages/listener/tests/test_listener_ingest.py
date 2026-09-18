import asyncio
import json
import os
import signal

import pytest

from hl7poc.listener import (
    CR,
    FS,
    VT,
    _close_mllp_server,
    build_ack,
    build_service_bus_message,
    drain_spool,
    extract_frames,
    process_frame,
    serve,
)
from hl7poc.model import CanonicalMessage, MessageHeader, Patient

ADT_A01 = (
    "MSH|^~\\&|SND|FAC|RCV|FAC2|20240101120000||ADT^A01|MSG001|P|2.5\r"
    "PID|1||MRN123^^^HOSP^MR||Doe^John^A||19800101|M\r"
)

BAD_FRAME = "GARBAGE NOT HL7\r"

# python-hl7 0.4.5 fails an internal sanity assert on this MSH-2, so parsing
# never reaches a second segment -- the MSH alone is the whole reproducer.
BAD_MSH2_FRAME = "MSH|^^^^|SND|FAC|RCV|FAC2|20240101120000||ADT^A01|MSG004|P|2.5\r"


def _framed(raw: str, *, trailing_cr: bool = True) -> bytes:
    tail = FS + CR if trailing_cr else FS
    return VT + raw.encode() + tail


# ---- extract_frames --------------------------------------------------------


def test_extract_frames_handles_frame_split_across_reads() -> None:
    whole = _framed(ADT_A01)
    part_a, part_b = whole[:10], whole[10:]

    frames, buf = extract_frames(part_a)
    assert frames == []

    frames, buf = extract_frames(buf + part_b)
    assert frames == [ADT_A01.encode()]
    assert buf == b""


def test_extract_frames_handles_two_frames_in_one_read() -> None:
    combined = _framed(ADT_A01) + _framed(BAD_FRAME)

    frames, buf = extract_frames(combined)

    assert frames == [ADT_A01.encode(), BAD_FRAME.encode()]
    assert buf == b""


def test_extract_frames_handles_fs_without_trailing_cr() -> None:
    frames, buf = extract_frames(_framed(ADT_A01, trailing_cr=False))

    assert frames == [ADT_A01.encode()]
    assert buf == b""


# ---- build_ack --------------------------------------------------------------


def test_build_ack_carries_code_and_control_id_from_header() -> None:
    header = MessageHeader(
        msg_type="ADT",
        event="A01",
        control_id="MSG001",
        sending_app="SND",
        sending_fac="FAC",
        message_ts="20240101120000",
        hl7_version="2.5",
    )

    ack = build_ack(header, "AA")

    assert "MSA|AA|MSG001\r" in ack
    assert ack.startswith("MSH|")


# ---- process_frame ----------------------------------------------------------


def test_good_frame_gets_aa_and_forwards_model_json(tmp_path) -> None:
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    rejected_dir = spool_dir / "rejected"
    forwarded: list[tuple[CanonicalMessage, object]] = []

    async def stub_forward(message, file) -> None:
        forwarded.append((message, file))

    async def run() -> str:
        return await process_frame(
            ADT_A01.encode(),
            spool_dir=spool_dir,
            rejected_dir=rejected_dir,
            forward=stub_forward,
            tasks=set(),
        )

    ack = asyncio.run(run())

    assert "MSA|AA|MSG001" in ack
    assert len(forwarded) == 1
    message, file = forwarded[0]
    assert message.header.control_id == "MSG001"
    # The forwarded body is the canonical model as JSON, not raw HL7.
    body = json.loads(message.to_json())
    assert body["patient"]["mrn"] == "MRN123"
    assert file.exists()


@pytest.mark.parametrize(
    "frame",
    [BAD_FRAME, BAD_MSH2_FRAME],
    ids=["not-hl7", "malformed-msh2"],
)
def test_bad_frame_gets_ae_and_is_rejected_not_forwarded(frame, tmp_path) -> None:
    # malformed-msh2 covers the bare AssertionError python-hl7 raises rather
    # than an HL7Exception: it must NACK and reject like any unmappable frame.
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    rejected_dir = spool_dir / "rejected"
    forwarded: list[object] = []

    async def stub_forward(message, file) -> None:
        forwarded.append(message)

    async def run() -> str:
        return await process_frame(
            frame.encode(),
            spool_dir=spool_dir,
            rejected_dir=rejected_dir,
            forward=stub_forward,
            tasks=set(),
        )

    ack = asyncio.run(run())

    assert "MSA|AE|" in ack
    assert forwarded == []
    assert list(spool_dir.glob("*.hl7")) == []
    assert len(list(rejected_dir.glob("*.hl7"))) == 1


def test_invalid_utf8_frame_gets_ae_and_is_rejected_not_forwarded(tmp_path) -> None:
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    rejected_dir = spool_dir / "rejected"
    forwarded: list[object] = []
    invalid = (
        VT
        + b"MSH|^~\\&|SND|FAC|RCV|FAC2|20240101120000||ADT^A01|\xff\xfe|P|2.5\r"
        + FS
        + CR
    )

    async def stub_forward(message, file) -> None:
        forwarded.append(message)

    async def run() -> str:
        frames, _ = extract_frames(invalid)
        return await process_frame(
            frames[0],
            spool_dir=spool_dir,
            rejected_dir=rejected_dir,
            forward=stub_forward,
            tasks=set(),
        )

    ack = asyncio.run(run())

    assert "MSA|AE|" in ack
    assert forwarded == []
    assert list(spool_dir.glob("*.hl7")) == []
    assert len(list(rejected_dir.glob("*.hl7"))) == 1


def test_drain_spool_preserves_cr_segment_terminators(tmp_path) -> None:
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    rejected_dir = spool_dir / "rejected"

    async def discard(message, file) -> None:
        """Leave the spool file in place so the drain has something to find."""

    asyncio.run(
        process_frame(
            ADT_A01.encode(),
            spool_dir=spool_dir,
            rejected_dir=rejected_dir,
            forward=discard,
            tasks=set(),
        )
    )

    drained: list[CanonicalMessage] = []

    async def record(message, file) -> None:
        drained.append(message)

    asyncio.run(drain_spool(spool_dir, rejected_dir, record))

    assert len(drained) == 1
    assert drained[0].patient.mrn == "MRN123"


def test_drain_spool_poison_file_does_not_stop_later_files(tmp_path) -> None:
    """One unmappable spooled frame must not abort the whole drain pass --
    every later file in the same pass still drains."""
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    rejected_dir = spool_dir / "rejected"

    # drain_spool walks sorted(glob("*.hl7")), so these names put the poison
    # frame ahead of the good one -- the good one only drains if the poison
    # frame did not abort the loop.
    (spool_dir / "0_poison.hl7").write_bytes(BAD_MSH2_FRAME.encode())
    (spool_dir / "1_good.hl7").write_bytes(ADT_A01.encode())

    drained: list[CanonicalMessage] = []

    async def record(message, file) -> None:
        drained.append(message)

    asyncio.run(drain_spool(spool_dir, rejected_dir, record))

    assert len(drained) == 1
    assert drained[0].patient.mrn == "MRN123"
    assert len(list(rejected_dir.glob("*.hl7"))) == 1


def test_drain_spool_rejects_undecodable_file_without_aborting_the_pass(
    tmp_path,
) -> None:
    """A crash between process_frame's spool write and its reject can leave
    undecodable bytes in the spool; the drain must set that file aside and
    keep going rather than strand every later file behind it."""
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    rejected_dir = spool_dir / "rejected"
    (spool_dir / "0-bad.hl7").write_bytes(b"MSH|^~\\&|A\xff\r")
    (spool_dir / "1-good.hl7").write_bytes(ADT_A01.encode())

    drained: list[CanonicalMessage] = []

    async def record(message, file) -> None:
        drained.append(message)

    asyncio.run(drain_spool(spool_dir, rejected_dir, record))

    assert [m.patient.mrn for m in drained] == ["MRN123"]
    assert [f.name for f in rejected_dir.glob("*.hl7")] == ["0-bad.hl7"]


def test_extract_frames_drops_unframed_junk() -> None:
    frames, buf = extract_frames(b"junk with no start block")

    assert frames == []
    assert buf == b""


def test_service_bus_message_id_is_never_empty() -> None:
    message = CanonicalMessage(
        header=MessageHeader(
            msg_type="ADT",
            event="A01",
            control_id="",
            sending_app="SND",
            sending_fac="FAC",
            message_ts="",
            hl7_version="2.5",
        ),
        patient=Patient(mrn="MRN123"),
    )

    # An empty message_id would make duplicate detection collapse every
    # control-id-less message into one.
    assert build_service_bus_message(message).message_id


def test_close_mllp_server_bounded_even_with_connection_left_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression for hl7-poc-0rs: on Python 3.12+ wait_closed() waits for every
    # accepted connection to close and has no timeout of its own, so a client
    # that never hangs up must not hang shutdown.
    monkeypatch.setattr("hl7poc.listener.MLLP_CLOSE_BUDGET", 0.2)

    async def run() -> tuple[float, bool]:
        opened = asyncio.Event()
        handler_exited = asyncio.Event()

        async def handle(reader, writer) -> None:
            opened.set()
            try:
                while await reader.read(100):
                    pass
            finally:
                writer.close()
                handler_exited.set()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        host, port = server.sockets[0].getsockname()[:2]
        _reader, writer = await asyncio.open_connection(host, port)
        await opened.wait()

        start = asyncio.get_running_loop().time()
        await _close_mllp_server(server)
        elapsed = asyncio.get_running_loop().time() - start
        await asyncio.wait_for(handler_exited.wait(), timeout=2)
        writer.close()
        return elapsed, handler_exited.is_set()

    elapsed, handler_exited = asyncio.run(run())

    assert elapsed < 2.0
    assert handler_exited


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_serve_shutdown_bounded_when_service_bus_close_hangs(
    monkeypatch: pytest.MonkeyPatch, tmp_path, sig: signal.Signals
) -> None:
    # Regression for hl7-poc-brn: sender.close() / ServiceBusClient.close()
    # do network I/O with no timeout of their own, so a Service Bus that has
    # gone unreachable since the link was established must not hang shutdown.
    monkeypatch.setattr("hl7poc.listener.SENDER_CLOSE_BUDGET", 0.2)

    class HangingSender:
        async def send_messages(self, message) -> None:
            raise AssertionError("not exercised in this test")

        async def close(self) -> None:
            await asyncio.sleep(60)

    class HangingClient:
        def get_queue_sender(self, queue: str) -> HangingSender:
            return HangingSender()

        async def close(self) -> None:
            await asyncio.sleep(60)

    monkeypatch.setattr(
        "hl7poc.listener.ServiceBusClient.from_connection_string",
        lambda *a, **k: HangingClient(),
    )

    async def run() -> float:
        serve_task = asyncio.create_task(
            serve(
                mllp_port=0,
                http_port=0,
                servicebus_connection="Endpoint=sb://unused/;SharedAccessKeyName=x;SharedAccessKey=x",
                servicebus_queue="q",
                spool_dir=tmp_path,
            )
        )
        await asyncio.sleep(0.1)  # let serve() install its signal handlers
        start = asyncio.get_running_loop().time()
        os.kill(os.getpid(), sig)
        await asyncio.wait_for(serve_task, timeout=5)
        return asyncio.get_running_loop().time() - start

    elapsed = asyncio.run(run())

    # Two bounded closes at 0.2s each; a 5s bound here would be satisfied by
    # the wait_for above and assert nothing.
    assert elapsed < 2.0
