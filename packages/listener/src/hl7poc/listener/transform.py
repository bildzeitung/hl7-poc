"""HL7 v2 -> hl7poc.model.CanonicalMessage mapping.

python-hl7 (the "hl7" PyPI package) is a listener-only dependency -- see
docs/canonical-model.md. Never import it from hl7poc.model or anywhere the
worker image installs, or the worker image stops being HL7-free.
"""

from __future__ import annotations

import hl7
from hl7.exceptions import HL7Exception
from hl7.util import unescape
from hl7poc.model import (
    Appointment,
    CanonicalMessage,
    MessageHeader,
    Observation,
    Patient,
    Result,
    Visit,
)

# Exceptions python-hl7 (or our own indexing into its containers) can raise
# while walking a message that turns out to be shaped wrong.
_MAPPING_ERRORS = (HL7Exception, KeyError, IndexError, AttributeError, ValueError)


class TransformError(ValueError):
    """Raised when an HL7 message cannot be mapped to the canonical model.

    The listener's ingest path NACKs (AE) on this, the same as a spool
    failure -- the message must not be silently dropped.
    """


def parse_message(raw: str) -> CanonicalMessage:
    """Parse a raw HL7 v2 message into the canonical model."""
    try:
        message = hl7.parse(raw)
        msh = message.segment("MSH")
    except _MAPPING_ERRORS as exc:
        raise TransformError(f"cannot parse MSH: {exc}") from exc

    try:
        return CanonicalMessage(
            header=_build_header(message, msh),
            patient=_build_patient(message),
            visit=_build_visit(message),
            appointment=_build_appointment(message),
            result=_build_result(message),
        )
    except _MAPPING_ERRORS as exc:
        raise TransformError(f"cannot map message: {exc}") from exc


def _optional_segment(message, name: str):
    try:
        return message.segment(name)
    except KeyError:
        return None


def _unescaped(message, segment, index: int) -> str | None:
    if index >= len(segment):
        return None
    text = str(segment[index])
    return unescape(message, text) if text else None


def _component(message, segment, index: int, component: int) -> str | None:
    if index >= len(segment):
        return None
    repetition = segment[index][0]
    if isinstance(repetition, str):
        # python-hl7 collapses a field with no "^" components down to a
        # plain str -- indexing it would slice characters, not components.
        text = repetition if component == 0 else ""
    else:
        try:
            text = str(repetition[component])
        except IndexError:
            text = ""
    return unescape(message, text) if text else None


def _build_header(message, msh) -> MessageHeader:
    return MessageHeader(
        msg_type=_component(message, msh, 9, 0) or "",
        event=_component(message, msh, 9, 1) or "",
        control_id=_unescaped(message, msh, 10) or "",
        sending_app=_unescaped(message, msh, 3) or "",
        sending_fac=_unescaped(message, msh, 4) or "",
        message_ts=_unescaped(message, msh, 7) or "",
        hl7_version=_unescaped(message, msh, 12) or "",
    )


def _build_patient(message) -> Patient:
    pid = _optional_segment(message, "PID")
    if pid is None:
        return Patient()
    return Patient(
        mrn=_component(message, pid, 3, 0),
        family_name=_component(message, pid, 5, 0),
        given_name=_component(message, pid, 5, 1),
        middle_name=_component(message, pid, 5, 2),
        dob=_unescaped(message, pid, 7),
        sex=_unescaped(message, pid, 8),
    )


def _build_visit(message) -> Visit | None:
    pv1 = _optional_segment(message, "PV1")
    if pv1 is None:
        return None
    return Visit(
        patient_class=_unescaped(message, pv1, 2),
        location=_unescaped(message, pv1, 3),
    )


def _build_appointment(message) -> Appointment | None:
    sch = _optional_segment(message, "SCH")
    if sch is None:
        return None
    # SCH-11 is a TQ (Timing Quantity): component 4 is start, 5 is end.
    return Appointment(
        id=_unescaped(message, sch, 1),
        start=_component(message, sch, 11, 3),
        end=_component(message, sch, 11, 4),
        status=_component(message, sch, 25, 0),
        reason=_component(message, sch, 7, 1) or _component(message, sch, 7, 0),
    )


def _build_result(message) -> Result | None:
    obr = _optional_segment(message, "OBR")
    if obr is None:
        return None
    try:
        obx_segments = message.segments("OBX")
    except KeyError:
        obx_segments = []
    return Result(
        placer_order_number=_unescaped(message, obr, 2),
        filler_order_number=_unescaped(message, obr, 3),
        universal_service_id=_component(message, obr, 4, 0),
        status=_unescaped(message, obr, 25),
        observation_ts=_unescaped(message, obr, 7),
        observations=[_build_observation(message, obx) for obx in obx_segments],
    )


def _build_observation(message, obx) -> Observation:
    return Observation(
        set_id=_unescaped(message, obx, 1),
        identifier=_component(message, obx, 3, 0),
        value=_unescaped(message, obx, 5),
        units=_unescaped(message, obx, 6),
        reference_range=_unescaped(message, obx, 7),
        abnormal_flags=_unescaped(message, obx, 8),
        status=_unescaped(message, obx, 11),
    )
