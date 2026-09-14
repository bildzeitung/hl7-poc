import asyncio
import json

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
from hl7poc.worker import decide, handle, push


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


def test_handle_completes_probe_without_deciding() -> None:
    receiver = _StubReceiver()
    # An empty, non-JSON body: if handle() fell through to decide() instead of
    # returning early on the PROBE check, from_json would reject it and the
    # message would be dead-lettered instead of completed.
    msg = _StubMessage(b"", props={"msgType": "PROBE"})

    asyncio.run(handle(receiver, msg, ""))

    assert receiver.completed == [msg]
    assert receiver.dead_lettered == []


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
