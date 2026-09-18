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
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Annotated

import typer
from azure.servicebus import NEXT_AVAILABLE_SESSION
from azure.servicebus.aio import AutoLockRenewer, ServiceBusClient
from azure.servicebus.exceptions import OperationTimeoutError, ServiceBusError

from hl7poc.model import CanonicalMessage
from hl7poc.probe import handle_http

app = typer.Typer(add_completion=False)
logger = logging.getLogger(__name__)

REPORT_TIMEOUT = 2  # seconds; see docs/configuration.md's "Report timeout" row

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


def push(payload: dict, webhook_url: str | None) -> None:
    body = json.dumps({**payload, "sent_at": datetime.now(UTC).isoformat()}).encode()
    if webhook_url:
        req = urllib.request.Request(
            webhook_url, data=body, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5)  # stand-in for FCM/APNs call
    else:
        print(f"PUSH -> {body.decode()}")


def report(event: dict, report_url: str) -> None:
    """POST a handled-message event to the dashboard. Never raises."""
    body = json.dumps(event).encode()
    req = urllib.request.Request(
        report_url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=REPORT_TIMEOUT)
    except (urllib.error.URLError, OSError, ValueError) as err:
        logger.warning("report to dashboard failed: %s", err)


def _report_event(
    model: CanonicalMessage | None, msg, outcome: str, *, notified: bool
) -> dict:
    mrn = model.patient.mrn or "unknown" if model else "unknown"
    msg_type = f"{model.header.msg_type}^{model.header.event}" if model else "unknown"
    return {
        "mrn": mrn,
        "session_id": getattr(msg, "session_id", None),
        "msg_type": msg_type,
        "outcome": outcome,
        "notified": notified,
        "reported_at": datetime.now(UTC).isoformat(),
    }


async def handle(
    receiver, msg, webhook_url: str | None, report_url: str | None = None
) -> None:
    model: CanonicalMessage | None = None
    notified = False
    outcome = "completed"
    try:
        raw = b"".join(msg.body).decode("utf-8")  # strict: the worker is the last hop
        model = CanonicalMessage.from_json(raw)
        payload = decide(model)
        if payload:
            await asyncio.get_running_loop().run_in_executor(
                None, push, payload, webhook_url
            )
            notified = True
        await receiver.complete_message(msg)
        if payload:
            print(f"notified mrn={payload['mrn']} ({payload['source_event']})")
    except ServiceBusError:
        raise  # settlement/link problems: let the outer loop rebuild the session
    except Exception as err:  # noqa: BLE001 - poison message, don't loop on it
        outcome = "dead_lettered"
        print(f"dead-lettering {msg.message_id}: {err}", file=sys.stderr)
        await receiver.dead_letter_message(
            msg, reason="ProcessingError", error_description=str(err)[:512]
        )
    if report_url:
        event = _report_event(model, msg, outcome, notified=notified)
        # Off the event loop, same as push -- reporting must never slow message
        # handling, and a swallowed failure here must not affect settlement above.
        await asyncio.get_running_loop().run_in_executor(
            None, report, event, report_url
        )


async def pump(
    client: ServiceBusClient,
    renewer: AutoLockRenewer,
    queue: str,
    webhook_url: str | None,
    stop_event: asyncio.Event,
    report_url: str | None = None,
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
                    await handle(receiver, msg, webhook_url, report_url)
        except OperationTimeoutError:
            await asyncio.sleep(1)  # no sessions with messages right now
        except ServiceBusError as err:
            print(f"service bus error, retrying: {err}", file=sys.stderr)
            await asyncio.sleep(5)


async def _serve(
    servicebus_connection: str,
    servicebus_queue: str,
    webhook_url: str | None,
    http_port: int,
    report_url: str | None = None,
) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    http_server = await asyncio.start_server(handle_http, "0.0.0.0", http_port)
    print(f"liveness on :{http_port}")

    try:
        async with ServiceBusClient.from_connection_string(
            servicebus_connection
        ) as client:
            renewer = AutoLockRenewer()
            pump_task = asyncio.create_task(
                pump(
                    client,
                    renewer,
                    servicebus_queue,
                    webhook_url,
                    stop_event,
                    report_url,
                )
            )
            stop_wait_task = asyncio.create_task(stop_event.wait())
            try:
                await asyncio.wait(
                    {pump_task, stop_wait_task}, return_when=asyncio.FIRST_COMPLETED
                )
                stop_event.set()  # finish current session, take no new ones
                # The pump only returns on its own through an unhandled
                # exception; awaiting it re-raises so the process exits
                # non-zero instead of leaving /live healthy over a dead pump.
                await asyncio.wait_for(pump_task, timeout=30)
            finally:
                stop_wait_task.cancel()
                await renewer.close()
    finally:
        http_server.close()


@app.command()
def main(
    servicebus_connection: Annotated[str, typer.Option(envvar="SERVICEBUS_CONNECTION")],
    servicebus_queue: Annotated[
        str, typer.Option(envvar="SERVICEBUS_QUEUE")
    ] = "hl7-events",
    webhook_url: Annotated[str | None, typer.Option(envvar="WEBHOOK_URL")] = None,
    http_port: Annotated[int, typer.Option(envvar="HTTP_PORT")] = 8081,
    report_url: Annotated[str | None, typer.Option(envvar="REPORT_URL")] = None,
) -> None:
    """Start the HL7 worker."""
    logging.basicConfig(level=logging.INFO)
    asyncio.run(
        _serve(
            servicebus_connection, servicebus_queue, webhook_url, http_port, report_url
        )
    )
