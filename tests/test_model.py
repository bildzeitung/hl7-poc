import pytest

from hl7poc.model import (
    CanonicalMessage,
    MessageHeader,
    ModelError,
    Observation,
    Patient,
    Result,
    Visit,
)


def _sample() -> CanonicalMessage:
    return CanonicalMessage(
        header=MessageHeader(
            msg_type="ORU",
            event="R01",
            control_id="MSG001",
            sending_app="SND",
            sending_fac="FAC",
            message_ts="20240101120000",
            hl7_version="2.5",
        ),
        patient=Patient(mrn="MRN1", family_name="Doe", given_name="John"),
        visit=Visit(patient_class="I", location="WARD1"),
        result=Result(
            filler_order_number="FL1",
            observations=[Observation(set_id="1", identifier="CODE1", value="5")],
        ),
    )


def test_round_trip_to_json_from_json() -> None:
    message = _sample()
    assert CanonicalMessage.from_json(message.to_json()) == message


def test_from_json_rejects_missing_schema_version() -> None:
    body = _sample().to_json()
    without_version = body.replace('"schema_version": 1', "")
    # Drop the trailing comma left by the removal above so the JSON parses;
    # this test cares about the missing-key check, not JSON well-formedness.
    without_version = without_version.replace(", }", "}")
    with pytest.raises(ModelError, match="schema_version"):
        CanonicalMessage.from_json(without_version)


def test_from_json_rejects_invalid_json() -> None:
    with pytest.raises(ModelError):
        CanonicalMessage.from_json("not json")
