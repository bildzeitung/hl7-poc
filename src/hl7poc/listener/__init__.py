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
import signal
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import hl7
import typer
from azure.servicebus import ServiceBusMessage
from azure.servicebus.aio import ServiceBusClient

from hl7poc.listener.transform import TransformError, parse_message
from hl7poc.model import CanonicalMessage, MessageHeader

app = typer.Typer(add_completion=False)
logger = logging.getLogger(__name__)

VT, FS, CR = b"\x0b", b"\x1c", b"\x0d"

ForwardFn = Callable[[CanonicalMessage, "Path | None"], Awaitable[None]]


class ListenerState:
    """Readiness state shared between the MLLP/probe handlers and the retry loop.

    /ready requires all three: MLLP bound, Service Bus reachable, not
    shutting down -- a plain instance attribute set is enough since every
    reader/writer runs on the same event loop thread.
    """

    def __init__(self) -> None:
        self.mllp_listening = False
        self.sb_healthy = False
        self.shutting_down = False

    def as_dict(self) -> dict[str, bool]:
        return {
            "mllp_listening": self.mllp_listening,
            "sb_healthy": self.sb_healthy,
            "shutting_down": self.shutting_down,
        }


# ---- MLLP framing (pure, unit-testable without asyncio) -------------------


def extract_frames(buf: bytes) -> tuple[list[str], bytes]:
    """Split complete VT...FS[CR] frames out of an accumulating byte buffer.

    Returns the decoded frames found and whatever partial data is left for
    the next read -- so a frame split across two socket reads is simply
    whatever remains after the first call, fed back in on the second.
    """
    frames: list[str] = []
    while (start := buf.find(VT)) != -1 and (end := buf.find(FS, start)) != -1:
        raw = buf[start + 1 : end].decode("utf-8", errors="replace")
        skip = 2 if buf[end + 1 : end + 2] == CR else 1
        buf = buf[end + skip :]
        frames.append(raw)
    return frames, buf


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


def _fallback_header(raw: str) -> MessageHeader:
    """Best-effort MSH read for the ACK when the full transform failed.

    Never raises -- an ACK must still go out even for a message python-hl7
    itself cannot parse. Used only to fill MSA-2 and the ACK's own MSH; the
    real header the mapping would have produced is unavailable here.
    """
    try:
        msh = hl7.parse(raw).segment("MSH")

        def field(n: int) -> str:
            return str(msh[n]) if n < len(msh) else ""

        msg_type, _, event = field(9).partition("^")
        return MessageHeader(
            msg_type=msg_type or "UNK",
            event=event,
            control_id=field(10) or str(uuid.uuid4()),
            sending_app=field(3),
            sending_fac=field(4),
            message_ts="",
            hl7_version=field(12),
        )
    except Exception:  # noqa: BLE001 - this read must never itself fail the ACK
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
        message_id=message.header.control_id,  # duplicate detection key
        application_properties={
            "msgType": message.header.msg_type,  # for future topic SQL filters
            "event": message.header.event,
        },
    )


# ---- per-frame ingest: spool -> transform -> ACK -> schedule forward ------


def _spool_path(spool_dir: Path) -> Path:
    return spool_dir / f"{time.time_ns()}-{uuid.uuid4().hex}.hl7"


async def process_frame(
    raw: str,
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
        file.write_text(raw, encoding="utf-8")  # durable first
    except OSError as err:
        logger.error("spool write failed, NACKing: %s", err)
        return build_ack(_fallback_header(raw), "AE")

    try:
        message = parse_message(raw)
    except TransformError as err:
        header = _fallback_header(raw)
        logger.error(
            "mapping failed for control id %s, NACKing and rejecting: %s",
            header.control_id,
            err,
        )
        rejected_dir.mkdir(parents=True, exist_ok=True)
        file.replace(rejected_dir / file.name)
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
    except (ConnectionResetError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


# ---- probe endpoints (stdlib-free-of-frameworks on purpose) --------------


async def handle_http(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    state: ListenerState,
) -> None:
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=3)
        path = request_line.split(b" ")[1].decode() if b" " in request_line else "/"
        if path == "/live":
            status, body = "200 OK", b"ok"
        elif path == "/ready":
            ready = (
                state.mllp_listening and state.sb_healthy and not state.shutting_down
            )
            status = "200 OK" if ready else "503 Service Unavailable"
            body = str(state.as_dict()).encode()
        else:
            status, body = "404 Not Found", b""
        writer.write(
            f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
    except (TimeoutError, IndexError, ConnectionResetError):
        pass
    finally:
        writer.close()


# ---- spool drain (retry loop) ---------------------------------------------


async def drain_spool(spool_dir: Path, rejected_dir: Path, forward: ForwardFn) -> None:
    """Re-parse and forward every spooled frame. Re-parsed here, not cached,
    so a mapping bug never loses data -- the spool stays raw HL7."""
    for file in sorted(spool_dir.glob("*.hl7")):
        raw = file.read_text(encoding="utf-8")
        try:
            message = parse_message(raw)
        except TransformError as err:
            logger.error("drain: mapping failed, rejecting %s: %s", file.name, err)
            rejected_dir.mkdir(parents=True, exist_ok=True)
            file.replace(rejected_dir / file.name)
            continue
        await forward(message, file)


async def _forward(
    message: CanonicalMessage,
    file: Path | None,
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
        if file:
            file.unlink(missing_ok=True)
    except Exception as err:  # noqa: BLE001 - any send failure degrades readiness
        state.sb_healthy = False
        logger.error(
            "forward failed (%s), left in spool: %s", message.header.control_id, err
        )


async def _startup_probe(sender, send_lock: asyncio.Lock, state: ListenerState) -> None:
    try:
        async with send_lock:
            await sender.send_messages(
                ServiceBusMessage(
                    body="probe",
                    session_id="_probe",
                    application_properties={"msgType": "PROBE"},
                )
            )
        state.sb_healthy = True
        logger.info("service bus reachable")
    except Exception as err:  # noqa: BLE001
        logger.warning("service bus not ready yet: %s", err)


async def retry_loop(
    state: ListenerState,
    sender,
    send_lock: asyncio.Lock,
    spool_dir: Path,
    rejected_dir: Path,
) -> None:
    forward = functools.partial(
        _forward, sender=sender, send_lock=send_lock, state=state
    )
    while not state.shutting_down:
        try:
            if not state.sb_healthy:
                await _startup_probe(sender, send_lock, state)
            await drain_spool(spool_dir, rejected_dir, forward)
        except Exception:
            logger.exception("retry loop error")
        await asyncio.sleep(5)


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

    state = ListenerState()
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
            functools.partial(handle_http, state=state), "0.0.0.0", http_port
        )
        logger.info("probes on :%d", http_port)

        retry = asyncio.create_task(
            retry_loop(state, sender, send_lock, spool_dir, rejected_dir)
        )

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()

        # k8s: flip /ready to 503 first so traffic stops routing, then drain.
        state.shutting_down = True
        mllp_server.close()
        await mllp_server.wait_closed()
        try:
            await asyncio.wait_for(
                drain_spool(spool_dir, rejected_dir, forward), timeout=8
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
