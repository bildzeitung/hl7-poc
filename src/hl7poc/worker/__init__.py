"""HL7 worker: consumes the canonical model from Service Bus, decides whether
to notify.

Session-aware consumer. Sessions are keyed by MRN at the edge, so this worker
processes each patient's events strictly in order while patients themselves
are handled concurrently across worker replicas.

The worker consumes hl7poc.model JSON only -- it never parses HL7 itself.
Decision logic lives HERE (the center). The "push" is a stand-in: it logs a
JSON payload, or POSTs it to webhook_url if set.

Health model: /live only. A puller has no inbound traffic to gate, so it
needs a liveness probe but no readiness endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import urllib.request
from datetime import UTC, datetime
from typing import Annotated

import typer
from azure.servicebus import NEXT_AVAILABLE_SESSION
from azure.servicebus.aio import AutoLockRenewer, ServiceBusClient
from azure.servicebus.exceptions import OperationTimeoutError, ServiceBusError

from hl7poc.model import CanonicalMessage

app = typer.Typer(add_completion=False)

_SIU_TITLES = {
    "S12": "Appointment booked",
    "S13": "Appointment rescheduled",
    "S14": "Appointment updated",
    "S15": "Appointment canceled",
}


def decide(msg: CanonicalMessage) -> dict | None:
    """Return a payload to push, or None for "no notification"."""
    mrn = msg.patient.mrn or "unknown"
    base = {"mrn": mrn, "source_event": f"{msg.header.msg_type}^{msg.header.event}"}

    if msg.header.msg_type == "SIU":
        title = _SIU_TITLES.get(msg.header.event)
        if title is None:
            return None  # S17 delete, S26 no-show: internal, don't notify patient
        start = msg.appointment.start if msg.appointment else None
        return {**base, "title": title, "appointment_start": start}

    if msg.header.msg_type == "ORU" and msg.header.event == "R01":
        if msg.result and msg.result.status == "F":  # final only; never preliminary
            return {**base, "title": "Your test results are ready"}
        return None

    # ADT and everything else: consumed for state, no patient-facing push.
    return None


def push(payload: dict, webhook_url: str) -> None:
    body = json.dumps({**payload, "sent_at": datetime.now(UTC).isoformat()}).encode()
    if webhook_url:
        req = urllib.request.Request(
            webhook_url, data=body, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5)  # stand-in for FCM/APNs call
    else:
        print(f"PUSH -> {body.decode()}")


def _prop(msg, key: str) -> str:
    props = msg.application_properties or {}
    val = props.get(key) or props.get(key.encode())
    return val.decode() if isinstance(val, (bytes, bytearray)) else (val or "")


async def handle(receiver, msg, webhook_url: str) -> None:
    if _prop(msg, "msgType") == "PROBE":
        await receiver.complete_message(msg)  # listener readiness probes
        return
    raw = b"".join(msg.body).decode("utf-8", errors="replace")
    try:
        model = CanonicalMessage.from_json(raw)
        payload = decide(model)
        if payload:
            await asyncio.get_running_loop().run_in_executor(
                None, push, payload, webhook_url
            )
        await receiver.complete_message(msg)
        if payload:
            print(f"notified mrn={payload['mrn']} ({payload['source_event']})")
    except ServiceBusError:
        raise  # settlement/link problems: let the outer loop rebuild the session
    except Exception as err:  # noqa: BLE001 - poison message, don't loop on it
        print(f"dead-lettering {msg.message_id}: {err}", file=sys.stderr)
        await receiver.dead_letter_message(
            msg, reason="ProcessingError", error_description=str(err)[:512]
        )


async def pump(
    client: ServiceBusClient,
    renewer: AutoLockRenewer,
    queue: str,
    webhook_url: str,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        try:
            receiver = client.get_queue_receiver(
                queue,
                session_id=NEXT_AVAILABLE_SESSION,  # grab any patient with backlog
                max_wait_time=5,  # drop session after 5s idle
            )
            async with receiver:
                renewer.register(
                    receiver, receiver.session, max_lock_renewal_duration=300
                )
                print(f"session accepted: {receiver.session.session_id}")
                async for msg in receiver:
                    await handle(receiver, msg, webhook_url)
        except OperationTimeoutError:
            await asyncio.sleep(1)  # no sessions with messages right now
        except ServiceBusError as err:
            print(f"service bus error, retrying: {err}", file=sys.stderr)
            await asyncio.sleep(5)


async def handle_http(reader, writer) -> None:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=3)
        ok = b" /live " in line or line.startswith(b"GET /live")
        status, body = ("200 OK", b"ok") if ok else ("404 Not Found", b"")
        writer.write(
            f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
    except (TimeoutError, ConnectionResetError):
        pass
    finally:
        writer.close()


async def _serve(
    servicebus_connection: str,
    servicebus_queue: str,
    webhook_url: str,
    http_port: int,
) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    http_server = await asyncio.start_server(handle_http, "0.0.0.0", http_port)
    print(f"liveness on :{http_port}")

    async with ServiceBusClient.from_connection_string(servicebus_connection) as client:
        renewer = AutoLockRenewer()
        pump_task = asyncio.create_task(
            pump(client, renewer, servicebus_queue, webhook_url, stop_event)
        )
        stop_wait_task = asyncio.create_task(stop_event.wait())
        await asyncio.wait(
            {pump_task, stop_wait_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if pump_task.done() and not stop_event.is_set():
            # pump ended on its own -- an unhandled exception, not a shutdown
            # signal. Clean up and re-raise so the process exits non-zero
            # instead of leaving /live reporting healthy over a dead pump.
            stop_wait_task.cancel()
            await renewer.close()
            http_server.close()
            pump_task.result()
        stop_event.set()  # finish current session, take no new ones
        stop_wait_task.cancel()
        await asyncio.wait_for(pump_task, timeout=30)
        await renewer.close()
    http_server.close()


@app.command()
def main(
    servicebus_connection: Annotated[str, typer.Option(envvar="SERVICEBUS_CONNECTION")],
    servicebus_queue: Annotated[
        str, typer.Option(envvar="SERVICEBUS_QUEUE")
    ] = "hl7-events",
    webhook_url: Annotated[str | None, typer.Option(envvar="WEBHOOK_URL")] = None,
    http_port: Annotated[int, typer.Option(envvar="HTTP_PORT")] = 8081,
) -> None:
    """Start the HL7 worker."""
    logging.basicConfig(level=logging.INFO)
    asyncio.run(
        _serve(servicebus_connection, servicebus_queue, webhook_url or "", http_port)
    )
