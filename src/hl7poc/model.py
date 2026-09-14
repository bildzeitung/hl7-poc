"""Canonical HL7 message model: stdlib-only types shared by every hl7poc image.

Both the listener and worker images install this module, so it must stay
dependency-free -- no python-hl7, no third-party imports at all. See
docs/canonical-model.md for the field list and where each value comes from.

By owner decision there is no "raw" or "segments" escape hatch here: an HL7
message the listener can't map to these fields is a transform failure (the
listener's NACK path), not a value carried on the wire.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

SCHEMA_VERSION = 1


class ModelError(ValueError):
    """Raised when a wire body cannot be decoded into a CanonicalMessage."""


@dataclass
class MessageHeader:
    msg_type: str
    event: str
    control_id: str
    sending_app: str
    sending_fac: str
    message_ts: str
    hl7_version: str


@dataclass
class Patient:
    mrn: str | None = None
    family_name: str | None = None
    given_name: str | None = None
    middle_name: str | None = None
    dob: str | None = None
    sex: str | None = None


@dataclass
class Visit:
    patient_class: str | None = None
    location: str | None = None


@dataclass
class Appointment:
    id: str | None = None
    start: str | None = None
    end: str | None = None
    status: str | None = None
    reason: str | None = None


@dataclass
class Observation:
    set_id: str | None = None
    identifier: str | None = None
    value: str | None = None
    units: str | None = None
    reference_range: str | None = None
    abnormal_flags: str | None = None
    status: str | None = None


@dataclass
class Result:
    filler_order_number: str | None = None
    placer_order_number: str | None = None
    universal_service_id: str | None = None
    status: str | None = None
    observation_ts: str | None = None
    observations: list[Observation] = field(default_factory=list)


@dataclass
class CanonicalMessage:
    header: MessageHeader
    patient: Patient
    visit: Visit | None = None
    appointment: Appointment | None = None
    result: Result | None = None
    schema_version: int = SCHEMA_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> CanonicalMessage:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ModelError(f"invalid JSON body: {exc}") from exc
        if "schema_version" not in data:
            raise ModelError("body missing schema_version")

        try:
            header = MessageHeader(**data["header"])
            patient = Patient(**data["patient"])
            visit_data = data.get("visit")
            visit = Visit(**visit_data) if visit_data else None
            appointment_data = data.get("appointment")
            appointment = Appointment(**appointment_data) if appointment_data else None
            result_data = data.get("result")
            result = None
            if result_data:
                observations = [
                    Observation(**o) for o in result_data.get("observations", [])
                ]
                result = Result(**{**result_data, "observations": observations})
        except (KeyError, TypeError) as exc:
            raise ModelError(f"malformed body: {exc}") from exc

        return cls(
            header=header,
            patient=patient,
            visit=visit,
            appointment=appointment,
            result=result,
            schema_version=data["schema_version"],
        )
