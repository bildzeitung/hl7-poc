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


def test_drain_spool_skips_file_unlinked_between_glob_and_read(tmp_path) -> None:
    """A concurrent direct-forward task (process_frame -> _forward) can unlink a
    spooled file between drain_spool's glob() and its read_bytes() for that same
    file. That file already made it out via the other path -- drain_spool must
    skip it silently, not raise FileNotFoundError and abort the pass."""
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    rejected_dir = spool_dir / "rejected"
    vanished = spool_dir / "1-vanished.hl7"
    (spool_dir / "0-good.hl7").write_bytes(ADT_A01.encode())
    vanished.write_bytes(ADT_A01.encode())

    drained: list[CanonicalMessage] = []

    async def record(message, file) -> None:
        drained.append(message)
        if file.name == "0-good.hl7":
            # Simulate the concurrent direct-forward's unlink landing between
            # drain_spool's glob() (which already listed "1-vanished.hl7") and
            # the loop reaching it.
            vanished.unlink()

    asyncio.run(drain_spool(spool_dir, rejected_dir, record))

    assert [m.patient.mrn for m in drained] == ["MRN123"]
    assert list(rejected_dir.glob("*.hl7")) == []


def test_drain_spool_skips_file_a_direct_forward_currently_owns(tmp_path) -> None:
    """A file process_frame just spooled and scheduled a direct forward for
    (process_frame -> _forward) must not also be sent by a concurrent
    drain_spool pass -- otherwise the same frame goes out twice. The
    in_flight set is how process_frame records that ownership."""
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    rejected_dir = spool_dir / "rejected"
    owned = spool_dir / "0-owned.hl7"
    owned.write_bytes(ADT_A01.encode())
    (spool_dir / "1-free.hl7").write_bytes(ADT_A01.encode())
    in_flight = {owned}

    drained: list[str] = []

    async def record(message, file) -> None:
        drained.append(file.name)

    asyncio.run(drain_spool(spool_dir, rejected_dir, record, in_flight))

    assert drained == ["1-free.hl7"]
    assert owned.exists()


def test_process_frame_marks_file_in_flight_until_forward_settles(tmp_path) -> None:
    """process_frame must add the spool file to `in_flight` before the forward
    task is scheduled and remove it once the forward settles, so a concurrent
    drain_spool pass can tell the file is (or is no longer) already owned."""
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    rejected_dir = spool_dir / "rejected"
    in_flight: set = set()
    release = asyncio.Event()
    seen_in_flight = False

    async def stub_forward(message, file) -> None:
        nonlocal seen_in_flight
        seen_in_flight = file in in_flight
        await release.wait()

    async def run() -> asyncio.Task:
        await process_frame(
            ADT_A01.encode(),
            spool_dir=spool_dir,
            rejected_dir=rejected_dir,
            forward=stub_forward,
            tasks=(tasks := set()),
            in_flight=in_flight,
        )
        return next(iter(tasks))

    async def scenario() -> None:
        task = await run()
        await asyncio.sleep(0)  # let stub_forward run up to release.wait()
        assert seen_in_flight
        assert len(in_flight) == 1
        release.set()
        await task
        assert in_flight == set()

    asyncio.run(scenario())


def test_serve_shutdown_does_not_double_send_a_file_retry_loop_is_mid_draining(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Regression for hl7-poc-8g2: serve() must fully stop retry_loop (cancel
    + await) before _final_drain runs its own drain_spool pass. Without that
    ordering, retry_loop can still be mid-send for a spool file when
    _final_drain globs the same, still-present file and sends it again."""
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    (spool_dir / "0.hl7").write_bytes(ADT_A01.encode())

    started = 0
    completed = 0
    retry_loop_send_may_finish = asyncio.Event()

    class FakeSender:
        async def send_messages(self, message) -> None:
            nonlocal started, completed
            started += 1
            if started == 1:
                # This is retry_loop's send, already in flight when SIGTERM
                # arrives -- hold it open until after the test has driven
                # serve() through cancelling retry_loop.
                await retry_loop_send_may_finish.wait()
            completed += 1

        async def close(self) -> None:
            pass

    class FakeClient:
        def get_queue_sender(self, queue: str) -> FakeSender:
            return FakeSender()

        async def close(self) -> None:
            pass

    monkeypatch.setattr(
        "hl7poc.listener.ServiceBusClient.from_connection_string",
        lambda *a, **k: FakeClient(),
    )

    async def run() -> None:
        serve_task = asyncio.create_task(
            serve(
                mllp_port=0,
                http_port=0,
                servicebus_connection="Endpoint=sb://unused/;SharedAccessKeyName=x;SharedAccessKey=x",
                servicebus_queue="q",
                spool_dir=spool_dir,
            )
        )
        while started == 0:  # let retry_loop's first drain reach send_messages
            await asyncio.sleep(0)

        os.kill(os.getpid(), signal.SIGTERM)
        # Pass/fail on the fixed code does not depend on this delay (the held
        # send is either cancelled or completes first); the delay only lets an
        # unfixed serve() start _final_drain's second send of the same file.
        await asyncio.sleep(0.1)
        retry_loop_send_may_finish.set()
        await asyncio.wait_for(serve_task, timeout=5)

    asyncio.run(run())

    assert completed == 1
    assert not (spool_dir / "0.hl7").exists()


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
