import pytest

from hl7poc.listener.transform import TransformError, parse_message

ADT_A01 = (
    "MSH|^~\\&|SND|FAC|RCV|FAC2|20240101120000||ADT^A01|MSG001|P|2.5\r"
    "PID|1||MRN123^^^HOSP^MR||Doe^John^A||19800101|M\r"
    "PV1|1|I|WARD1^ROOM2\r"
)

# SCH-11 (TQ) components 4/5 are start/end; SCH-25 is the filler status code.
SIU_S12 = (
    "MSH|^~\\&|SND|FAC|RCV|FAC2|20240101120000||SIU^S12|MSG002|P|2.5\r"
    "PID|1||MRN124||Smith^Jane\r"
    "SCH|APPT1|FILLER1|1||SCHED1|E1|Checkup^Routine|ROUTINE|30|min|"
    "^^^20240102090000^20240102093000||||||||||||||Booked^^HL70278\r"
)

ORU_R01 = (
    "MSH|^~\\&|SND|FAC|RCV|FAC2|20240101120000||ORU^R01|MSG003|P|2.5\r"
    "PID|1||MRN125||Brown^Sam\r"
    "OBR|1|PL1|FL1|CODE1^Test One^L|||20240101120500||||||||||||||||||F\r"
    "OBX|1|ST|CODE2^Test One^L||val1~Smith \\T\\ Jones|mg/dL|1-5|N|||F\r"
    "OBX|2|ST|CODE3^Test Three^L||val3|mg/dL|1-5|N|||F\r"
)


def test_adt_a01_maps_header_patient_visit() -> None:
    message = parse_message(ADT_A01)

    assert message.header.msg_type == "ADT"
    assert message.header.event == "A01"
    assert message.header.control_id == "MSG001"
    assert message.patient.mrn == "MRN123"
    assert message.patient.family_name == "Doe"
    assert message.patient.given_name == "John"
    assert message.patient.middle_name == "A"
    assert message.patient.dob == "19800101"
    assert message.patient.sex == "M"
    assert message.visit is not None
    assert message.visit.patient_class == "I"
    assert message.appointment is None
    assert message.result is None


def test_siu_s12_maps_appointment_from_sch() -> None:
    message = parse_message(SIU_S12)

    assert message.header.msg_type == "SIU"
    assert message.header.event == "S12"
    assert message.appointment is not None
    assert message.appointment.id == "APPT1"
    assert message.appointment.start == "20240102090000"
    assert message.appointment.end == "20240102093000"
    assert message.appointment.status == "Booked"
    assert message.appointment.reason == "Routine"


def test_oru_r01_maps_result_and_observations_with_escapes_and_repetition() -> None:
    message = parse_message(ORU_R01)

    assert message.header.msg_type == "ORU"
    assert message.result is not None
    assert message.result.placer_order_number == "PL1"
    assert message.result.filler_order_number == "FL1"
    assert message.result.universal_service_id == "CODE1"
    assert message.result.status == "F"
    assert message.result.observation_ts == "20240101120500"
    assert len(message.result.observations) == 2

    first, second = message.result.observations
    assert first.identifier == "CODE2"
    # OBX-5 carries a repetition (kept as raw "~"-joined text) and "\T\",
    # HL7's escape for the subcomponent separator "&". The decoded "&" is
    # what proves unescaping runs on a mapped field, not raw passthrough.
    assert first.value == "val1~Smith & Jones"
    assert second.identifier == "CODE3"
    assert second.value == "val3"


def test_unknown_message_type_yields_header_and_patient_only() -> None:
    raw = (
        "MSH|^~\\&|SND|FAC|RCV|FAC2|20240101120000||ZZZ^Z01|MSG004|P|2.5\r"
        "PID|1||MRN999||Nobody\r"
    )
    message = parse_message(raw)

    assert message.header.msg_type == "ZZZ"
    assert message.patient.mrn == "MRN999"
    assert message.visit is None
    assert message.appointment is None
    assert message.result is None


def test_malformed_msh_raises_transform_error() -> None:
    with pytest.raises(TransformError):
        parse_message("GARBAGE NOT HL7")
