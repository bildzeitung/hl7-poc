"""hl7listener: transforming MLLP edge.

MLLP in -> spool raw (durable first) -> parse into the canonical model
(hl7poc.listener.transform) -> ACK -> forward the model as JSON to Service
Bus, asynchronously. A frame the mapping cannot handle is NACKed and set
aside in <spool-dir>/rejected/ so the 5s retry drain never retries it; the
process keeps serving. See docs/design.md and docs/canonical-model.md.

No import-time side effects: every env-derived value is a CLI option
resolved when the command runs, not at module scope, so `import
hl7poc.listener` works with no env vars set and no broker running.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import signal
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from azure.servicebus import ServiceBusMessage
from azure.servicebus.aio import ServiceBusClient

from hl7poc.listener.transform import TransformError, parse_header, parse_message
from hl7poc.model import CanonicalMessage, MessageHeader
from hl7poc.probe import handle_http

app = typer.Typer(add_completion=False)
logger = logging.getLogger(__name__)

VT, FS, CR = b"\x0b", b"\x1c", b"\x0d"

# asyncio.Server.wait_closed() has no timeout of its own and on Python 3.12+
# waits for every already-accepted connection to close; an MLLP client that
# never closes its side (e.g. a container killed while a connection is still
# half-open through a port proxy) would otherwise hang shutdown indefinitely.
# Kubernetes SIGKILLs after its grace period regardless, so bound this stage
# the same way the spool drain below is bounded.
MLLP_CLOSE_BUDGET = 8

ForwardFn = Callable[[CanonicalMessage, Path], Awaitable[None]]


class ListenerState:
    """Readiness state shared between the MLLP/probe handlers and the retry loop.

    /ready is spool-first -- MLLP bound, spool dir writable, not shutting down;
    Service Bus is deliberately not an input (see docs/decisions.md), so
    sb_healthy is carried only for the response body. A plain instance
    attribute set is enough since every reader/writer runs on the same event
    loop thread.
    """

    def __init__(self, spool_dir: Path) -> None:
        self.spool_dir = spool_dir
        self.mllp_listening = False
        self.sb_healthy = False
        self.shutting_down = False

    def ready(self) -> tuple[bool, dict[str, bool]]:
        spool_writable = os.access(self.spool_dir, os.W_OK)
        fields = {
            "mllp_listening": self.mllp_listening,
            "sb_healthy": self.sb_healthy,
            "shutting_down": self.shutting_down,
            "spool_writable": spool_writable,
        }
        is_ready = self.mllp_listening and spool_writable and not self.shutting_down
        return is_ready, fields


# ---- MLLP framing (pure, unit-testable without asyncio) -------------------


def extract_frames(buf: bytes) -> tuple[list[bytes], bytes]:
    """Split complete VT...FS[CR] frames out of an accumulating byte buffer.

    Returns the raw frame bytes found, undecoded, and whatever partial data
    is left for the next read -- so a frame split across two socket reads is
    simply whatever remains after the first call, fed back in on the second.
    Decoding happens in process_frame, after the bytes are already spooled --
    see docs/decisions.md (undecodable frames are NACKed, never
    silently replaced).
    """
    frames: list[bytes] = []
    while (start := buf.find(VT)) != -1 and (end := buf.find(FS, start)) != -1:
        raw = buf[start + 1 : end]
        skip = 2 if buf[end + 1 : end + 2] == CR else 1
        buf = buf[end + skip :]
        frames.append(raw)
    # Bytes before the next VT can never begin a frame; dropping them keeps a
    # peer that sends unframed junk from growing the buffer without bound.
    start = buf.find(VT)
    return frames, buf[start:] if start != -1 else b""


# ---- ACK building -----------------------------------------------------


def build_ack(header: MessageHeader, code: str) -> str:
    """Build an MSH/MSA ACK from a (real or fallback) message header."""
    ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    version = header.hl7_version or "2.5"
    return (
        f"MSH|^~\\&|EDGE|LISTENER|{header.sending_app}|{header.sending_fac}|{ts}||"
        f"ACK^{header.event}|{uuid.uuid4()}|P|{version}\r"
        f"MSA|{code}|{header.control_id}\r"
    )


def _fallback_header(raw: bytes) -> MessageHeader:
    """Header for the ACK when the full transform failed.

    Never raises -- an ACK must still go out even for a message python-hl7
    itself cannot parse, in which case MSA-2 carries a synthetic id because
    the sender's control id is unreadable. The decode is lossy on purpose:
    this is the one place undecodable bytes may be mangled, because the
    result only ever reaches the ACK and the log, never a forwarded message.
    """
    header = parse_header(raw.decode("utf-8", errors="replace"))
    if header is not None:
        return header
    return MessageHeader(
        msg_type="UNK",
        event="",
        control_id=str(uuid.uuid4()),
        sending_app="",
        sending_fac="",
        message_ts="",
        hl7_version="",
    )


# ---- Service Bus message shaping (pure, unit-testable) --------------------


def build_service_bus_message(message: CanonicalMessage) -> ServiceBusMessage:
    return ServiceBusMessage(
        body=message.to_json(),
        session_id=message.patient.mrn or "unknown",  # per-patient ordering
        # Duplicate detection key. An empty MSH-10 must NOT become an empty
        # message_id: the queue would collapse every control-id-less message
        # into one and drop the rest as duplicates.
        message_id=message.header.control_id or str(uuid.uuid4()),
        application_properties={
            "msgType": message.header.msg_type,  # for future topic SQL filters
            "event": message.header.event,
        },
    )


# ---- per-frame ingest: spool -> transform -> ACK -> schedule forward ------


def _reject(file: Path, rejected_dir: Path) -> None:
    """Set a frame the mapping rejected aside, out of the drain's reach."""
    rejected_dir.mkdir(parents=True, exist_ok=True)
    file.replace(rejected_dir / file.name)


def _spool_path(spool_dir: Path) -> Path:
    return spool_dir / f"{time.time_ns()}-{uuid.uuid4().hex}.hl7"


async def process_frame(
    raw: bytes,
    *,
    spool_dir: Path,
    rejected_dir: Path,
    forward: ForwardFn,
    tasks: set[asyncio.Task],
) -> str:
    """Spool, transform, and ACK one HL7 frame; schedule the forward on success.

    Returns the HL7 ACK text to write back on the wire. The forward task is
    kept in `tasks` (discarded on completion) -- an un-awaited, unreferenced
    asyncio task can be garbage-collected mid-flight.
    """
    file = _spool_path(spool_dir)
    try:
        # Bytes, not text: text mode translates outgoing LF to os.linesep,
        # breaking the byte-exact round-trip a CR-terminated frame needs.
        file.write_bytes(raw)  # durable first, byte-exact
    except OSError as err:
        logger.error("spool write failed, NACKing: %s", err)
        header = _fallback_header(raw)
        return build_ack(header, "AE")

    # Fail closed on undecodable bytes rather than silently replacing them
    # (docs/decisions.md) -- the sender's AA would otherwise cover data that
    # was never actually forwarded intact.
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as err:
        header = _fallback_header(raw)
        logger.error(
            "frame is not valid UTF-8 for control id %s, NACKing and rejecting: %s",
            header.control_id,
            err,
        )
        _reject(file, rejected_dir)
        return build_ack(header, "AE")

    try:
        message = parse_message(text)
    except TransformError as err:
        header = _fallback_header(raw)
        logger.error(
            "mapping failed for control id %s, NACKing and rejecting: %s",
            header.control_id,
            err,
        )
        _reject(file, rejected_dir)
        return build_ack(header, "AE")

    ack = build_ack(message.header, "AA")
    task = asyncio.create_task(forward(message, file))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return ack


# ---- MLLP server ---------------------------------------------------------


async def handle_mllp(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    spool_dir: Path,
    rejected_dir: Path,
    forward: ForwardFn,
    tasks: set[asyncio.Task],
) -> None:
    buf = b""
    try:
        while chunk := await reader.read(65536):
            buf += chunk
            frames, buf = extract_frames(buf)
            for raw in frames:
                ack = await process_frame(
                    raw,
                    spool_dir=spool_dir,
                    rejected_dir=rejected_dir,
                    forward=forward,
                    tasks=tasks,
                )
                writer.write(VT + ack.encode() + FS + CR)
                await writer.drain()
    except ConnectionResetError, asyncio.IncompleteReadError:
        pass
    finally:
        writer.close()


# ---- spool drain (retry loop) ---------------------------------------------


async def drain_spool(spool_dir: Path, rejected_dir: Path, forward: ForwardFn) -> None:
    """Re-parse and forward every spooled frame. Re-parsed here, not cached,
    so a mapping bug never loses data -- the spool stays raw HL7."""
    for file in sorted(spool_dir.glob("*.hl7")):
        # Bytes, not text: universal newlines would turn every CR segment
        # terminator into LF, collapsing the message into one MSH segment.
        # The decode sits inside the reject path because the spool can hold
        # undecodable bytes (process_frame writes durably before it decodes);
        # an escaping UnicodeDecodeError would abort the entire drain pass.
        try:
            raw = file.read_bytes().decode("utf-8")
            message = parse_message(raw)
        except (TransformError, UnicodeDecodeError) as err:
            logger.error("drain: unusable frame, rejecting %s: %s", file.name, err)
            _reject(file, rejected_dir)
            continue
        await forward(message, file)


async def _final_drain(
    tasks: set[asyncio.Task],
    spool_dir: Path,
    rejected_dir: Path,
    forward: ForwardFn,
) -> None:
    """Await in-flight forwards, then drain the spool.

    A forward cancelled mid-send loses nothing (the spool file is unlinked
    only after a successful send), but letting it finish first avoids
    re-sending work that was about to complete.
    """
    await asyncio.gather(*tasks, return_exceptions=True)
    await drain_spool(spool_dir, rejected_dir, forward)


async def _forward(
    message: CanonicalMessage,
    file: Path,
    *,
    sender,
    send_lock: asyncio.Lock,
    state: ListenerState,
) -> None:
    sb_message = build_service_bus_message(message)
    try:
        async with send_lock:
            await sender.send_messages(sb_message)
        state.sb_healthy = True
        file.unlink(missing_ok=True)
    except Exception as err:  # noqa: BLE001 - any send failure degrades readiness
        state.sb_healthy = False
        logger.error(
            "forward failed (%s), left in spool: %s", message.header.control_id, err
        )


async def retry_loop(
    state: ListenerState,
    forward: ForwardFn,
    spool_dir: Path,
    rejected_dir: Path,
) -> None:
    while not state.shutting_down:
        try:
            await drain_spool(spool_dir, rejected_dir, forward)
        except Exception:
            logger.exception("retry loop error")
        await asyncio.sleep(5)


async def _close_mllp_server(mllp_server: asyncio.AbstractServer) -> None:
    """Stop accepting MLLP connections and wait, bounded, for open ones to close.

    wait_closed() has no timeout of its own; see MLLP_CLOSE_BUDGET's comment.
    """
    mllp_server.close()
    try:
        await asyncio.wait_for(mllp_server.wait_closed(), timeout=MLLP_CLOSE_BUDGET)
    except TimeoutError:
        logger.warning("mllp server close timed out; closing open connection(s)")
        # Close the transports so each handle_mllp reads EOF and exits through
        # its own finally, rather than being left for asyncio.run to cancel.
        mllp_server.close_clients()


# ---- lifecycle -------------------------------------------------------------


async def serve(
    *,
    mllp_port: int,
    http_port: int,
    servicebus_connection: str,
    servicebus_queue: str,
    spool_dir: Path,
) -> None:
    spool_dir.mkdir(parents=True, exist_ok=True)
    rejected_dir = spool_dir / "rejected"

    state = ListenerState(spool_dir)
    tasks: set[asyncio.Task] = set()

    async with ServiceBusClient.from_connection_string(
        servicebus_connection
    ) as sb_client:
        sender = sb_client.get_queue_sender(servicebus_queue)
        send_lock = asyncio.Lock()
        forward = functools.partial(
            _forward, sender=sender, send_lock=send_lock, state=state
        )

        mllp_server = await asyncio.start_server(
            functools.partial(
                handle_mllp,
                spool_dir=spool_dir,
                rejected_dir=rejected_dir,
                forward=forward,
                tasks=tasks,
            ),
            "0.0.0.0",
            mllp_port,
        )
        state.mllp_listening = True
        logger.info("MLLP listening on :%d", mllp_port)

        http_server = await asyncio.start_server(
            functools.partial(handle_http, ready=state.ready), "0.0.0.0", http_port
        )
        logger.info("probes on :%d", http_port)

        retry = asyncio.create_task(retry_loop(state, forward, spool_dir, rejected_dir))

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()

        # k8s: flip /ready to 503 first so traffic stops routing, then drain.
        state.shutting_down = True
        await _close_mllp_server(mllp_server)
        try:
            await asyncio.wait_for(
                _final_drain(tasks, spool_dir, rejected_dir, forward), timeout=8
            )
        except TimeoutError:
            logger.warning("shutdown drain timed out; spool retained")
        retry.cancel()
        http_server.close()
        await sender.close()


@app.command()
def listen(
    servicebus_connection: Annotated[str, typer.Option(envvar="SERVICEBUS_CONNECTION")],
    mllp_port: Annotated[int, typer.Option(envvar="MLLP_PORT")] = 2575,
    http_port: Annotated[int, typer.Option(envvar="HTTP_PORT")] = 8080,
    servicebus_queue: Annotated[
        str, typer.Option(envvar="SERVICEBUS_QUEUE")
    ] = "hl7-events",
    spool_dir: Annotated[Path, typer.Option(envvar="SPOOL_DIR")] = Path("./spool"),
) -> None:
    """Start the HL7 listener."""
    logging.basicConfig(level=logging.INFO)
    asyncio.run(
        serve(
            mllp_port=mllp_port,
            http_port=http_port,
            servicebus_connection=servicebus_connection,
            servicebus_queue=servicebus_queue,
            spool_dir=spool_dir,
        )
    )


if __name__ == "__main__":
    sys.exit(app())
