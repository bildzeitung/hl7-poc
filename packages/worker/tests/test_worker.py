from __future__ import annotations

import asyncio
import json
from typing import Self

import pytest
from azure.servicebus.exceptions import ServiceBusError

from hl7poc.model import (
    Appointment,
    CanonicalMessage,
    MessageHeader,
    Observation,
    Patient,
    Result,
)
from hl7poc.worker import _REPORT_EXECUTOR, _serve, decide, handle, push, report


def _model(
    msg_type: str,
    event: str,
    *,
    mrn: str | None = "12345",
    appointment_start: str | None = None,
    result_status: str | None = None,
) -> CanonicalMessage:
    header = MessageHeader(
        msg_type=msg_type,
        event=event,
        control_id="1",
        sending_app="SIM",
        sending_fac="SIM",
        message_ts="20260914120000",
        hl7_version="2.5",
    )
    patient = Patient(mrn=mrn)
    appointment = (
        Appointment(start=appointment_start) if appointment_start is not None else None
    )
    result = (
        Result(status=result_status, observations=[Observation()])
        if result_status is not None
        else None
    )
    return CanonicalMessage(
        header=header, patient=patient, appointment=appointment, result=result
    )


# ---- decide -----------------------------------------------------------------


def test_decide_siu_s12_notifies_with_title_and_start() -> None:
    model = _model("SIU", "S12", appointment_start="20260920090000")
    payload = decide(model)
    assert payload == {
        "mrn": "12345",
        "source_event": "SIU^S12",
        "title": "Appointment booked",
        "appointment_start": "20260920090000",
    }


def test_decide_siu_s17_does_not_notify() -> None:
    model = _model("SIU", "S17", appointment_start="20260920090000")
    assert decide(model) is None


def test_decide_oru_r01_final_notifies() -> None:
    model = _model("ORU", "R01", result_status="F")
    payload = decide(model)
    assert payload == {
        "mrn": "12345",
        "source_event": "ORU^R01",
        "title": "Your test results are ready",
    }


def test_decide_oru_r01_preliminary_does_not_notify() -> None:
    model = _model("ORU", "R01", result_status="P")
    assert decide(model) is None


def test_decide_adt_does_not_notify() -> None:
    model = _model("ADT", "A01")
    assert decide(model) is None


def test_decide_missing_mrn_falls_back_to_unknown() -> None:
    model = _model("SIU", "S12", mrn=None, appointment_start="20260920090000")
    payload = decide(model)
    assert payload["mrn"] == "unknown"


# ---- push --------------------------------------------------------------------


def test_push_with_no_webhook_prints_json_with_sent_at(capsys) -> None:
    push({"mrn": "12345", "source_event": "SIU^S12"}, "")
    out = capsys.readouterr().out
    assert out.startswith("PUSH -> ")
    body = json.loads(out.removeprefix("PUSH -> ").strip())
    assert body["mrn"] == "12345"
    assert "sent_at" in body


# ---- report ------------------------------------------------------------------


def test_report_posts_event(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(
        "hl7poc.worker.urllib.request.urlopen",
        lambda req, timeout=None: calls.append(req),
    )
    report({"mrn": "1", "outcome": "completed"}, "http://dashboard.example/api/handled")

    assert len(calls) == 1
    assert calls[0].full_url == "http://dashboard.example/api/handled"
    assert json.loads(calls[0].data)["outcome"] == "completed"


def test_report_swallows_failure(monkeypatch) -> None:
    def _raising_urlopen(req, timeout=None):
        raise OSError("dashboard down")

    monkeypatch.setattr("hl7poc.worker.urllib.request.urlopen", _raising_urlopen)

    report(
        {"mrn": "1", "outcome": "completed"}, "http://dashboard.example/api/handled"
    )  # must not raise


# ---- handle --------------------------------------------------------------------


class _StubReceiver:
    def __init__(self) -> None:
        self.completed: list = []
        self.dead_lettered: list[tuple] = []

    async def complete_message(self, msg) -> None:
        self.completed.append(msg)

    async def dead_letter_message(
        self, msg, *, reason: str, error_description: str
    ) -> None:
        self.dead_lettered.append((msg, reason, error_description))


class _RaisingCompleteReceiver(_StubReceiver):
    async def complete_message(self, msg) -> None:
        raise ServiceBusError("link down")


class _StubMessage:
    def __init__(
        self, body: bytes, *, props: dict | None = None, message_id: str = "m1"
    ):
        self.body = [body]
        self.application_properties = props or {}
        self.message_id = message_id


def test_handle_payload_pushes_and_completes(capsys) -> None:
    receiver = _StubReceiver()
    model = _model("SIU", "S12", appointment_start="20260920090000")
    msg = _StubMessage(model.to_json().encode())

    asyncio.run(handle(receiver, msg, ""))

    assert receiver.completed == [msg]
    out = capsys.readouterr().out
    assert "notified mrn=12345" in out
    assert "PUSH ->" in out


def test_handle_invalid_body_dead_letters_with_processing_error() -> None:
    receiver = _StubReceiver()
    msg = _StubMessage(b"not json")

    asyncio.run(handle(receiver, msg, ""))

    assert receiver.completed == []
    assert len(receiver.dead_lettered) == 1
    assert receiver.dead_lettered[0][1] == "ProcessingError"


def test_handle_undecodable_body_dead_letters_with_processing_error() -> None:
    receiver = _StubReceiver()
    msg = _StubMessage(b"\xff\xfe not valid utf-8")

    asyncio.run(handle(receiver, msg, ""))

    assert receiver.completed == []
    assert len(receiver.dead_lettered) == 1
    assert receiver.dead_lettered[0][1] == "ProcessingError"


def test_handle_push_failure_dead_letters_with_processing_error(monkeypatch) -> None:
    def _raising_push(payload: dict, webhook_url: str) -> None:
        raise OSError("webhook unreachable")

    monkeypatch.setattr("hl7poc.worker.push", _raising_push)
    receiver = _StubReceiver()
    model = _model("SIU", "S12", appointment_start="20260920090000")
    msg = _StubMessage(model.to_json().encode())

    asyncio.run(handle(receiver, msg, "http://webhook.example/"))

    assert receiver.completed == []
    assert len(receiver.dead_lettered) == 1
    assert receiver.dead_lettered[0][1] == "ProcessingError"


def test_handle_service_bus_error_on_complete_propagates() -> None:
    receiver = _RaisingCompleteReceiver()
    model = _model("ADT", "A01")
    msg = _StubMessage(model.to_json().encode())

    with pytest.raises(ServiceBusError):
        asyncio.run(handle(receiver, msg, ""))


# ---- handle: reporting --------------------------------------------------------


def test_handle_no_report_url_makes_no_http_call(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr("hl7poc.worker.report", lambda *a, **kw: calls.append((a, kw)))
    receiver = _StubReceiver()
    model = _model("ADT", "A01")
    msg = _StubMessage(model.to_json().encode())

    asyncio.run(handle(receiver, msg, ""))  # report_url defaults to None

    assert calls == []


def test_handle_completed_reports_outcome_completed(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(
        "hl7poc.worker.report", lambda event, url: calls.append((event, url))
    )
    receiver = _StubReceiver()
    model = _model("SIU", "S12", appointment_start="20260920090000")
    msg = _StubMessage(model.to_json().encode())

    asyncio.run(handle(receiver, msg, "", "http://dashboard.example/api/handled"))
    _REPORT_EXECUTOR.submit(lambda: None).result()  # drain the FIFO report thread

    assert len(calls) == 1
    event, url = calls[0]
    assert url == "http://dashboard.example/api/handled"
    assert event["outcome"] == "completed"
    assert event["notified"] is True
    assert event["mrn"] == "12345"
    assert event["msg_type"] == "SIU^S12"


def test_handle_dead_lettered_reports_outcome_dead_lettered(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(
        "hl7poc.worker.report", lambda event, url: calls.append((event, url))
    )
    receiver = _StubReceiver()
    msg = _StubMessage(b"not json")

    asyncio.run(handle(receiver, msg, "", "http://dashboard.example/api/handled"))
    _REPORT_EXECUTOR.submit(lambda: None).result()  # drain the FIFO report thread

    assert len(calls) == 1
    event, _ = calls[0]
    assert event["outcome"] == "dead_lettered"
    assert event["notified"] is False
    assert event["mrn"] == "unknown"
    assert event["msg_type"] == "unknown"


def test_handle_with_dashboard_down_still_completes(monkeypatch) -> None:
    def _raising_urlopen(req, timeout=None):
        raise OSError("dashboard down")

    monkeypatch.setattr("hl7poc.worker.urllib.request.urlopen", _raising_urlopen)
    receiver = _StubReceiver()
    model = _model("ADT", "A01")
    msg = _StubMessage(model.to_json().encode())

    asyncio.run(handle(receiver, msg, "", "http://dashboard.example/api/handled"))

    assert receiver.completed == [msg]


def test_handle_does_not_wait_for_report(monkeypatch) -> None:
    import threading

    release = threading.Event()
    monkeypatch.setattr("hl7poc.worker.report", lambda *a: release.wait(5))
    receiver = _StubReceiver()
    model = _model("ADT", "A01")
    msg = _StubMessage(model.to_json().encode())

    async def _run() -> None:
        await asyncio.wait_for(
            handle(receiver, msg, "", "http://dashboard.example/api/handled"), 1
        )
        release.set()

    asyncio.run(_run())

    assert receiver.completed == [msg]


# ---- _serve lifecycle ---------------------------------------------------------


class _FakeClient:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeRenewer:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_serve_reraises_a_dead_pump_and_closes_its_resources(monkeypatch) -> None:
    # A pump that dies on an unhandled exception must take the process with it,
    # not leave /live answering 200 over a worker that consumes nothing.
    renewer = _FakeRenewer()
    servers: list = []
    real_start_server = asyncio.start_server

    async def _boom(*args, **kwargs) -> None:
        raise RuntimeError("pump died")

    async def _tracking_start_server(*args, **kwargs):
        server = await real_start_server(*args, **kwargs)
        servers.append(server)
        return server

    monkeypatch.setattr("hl7poc.worker.pump", _boom)
    monkeypatch.setattr("hl7poc.worker.AutoLockRenewer", lambda: renewer)
    monkeypatch.setattr(
        "hl7poc.worker.ServiceBusClient",
        type(
            "_C", (), {"from_connection_string": staticmethod(lambda _: _FakeClient())}
        ),
    )
    monkeypatch.setattr(asyncio, "start_server", _tracking_start_server)

    with pytest.raises(RuntimeError, match="pump died"):
        asyncio.run(_serve("conn", "hl7-events", None, 0))

    assert renewer.closed
    assert servers and not servers[0].is_serving()
