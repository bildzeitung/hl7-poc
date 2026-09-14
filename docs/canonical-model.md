# Canonical message model

Owner decision (2026-09-14, recorded on hl7-poc-wld): the listener transforms every HL7 message into
this model and puts only the model's JSON on `hl7-events`. No raw HL7 travels on the queue, and the
worker never parses HL7. There is no `raw`/`segments` escape hatch — a message the listener can't map
onto these fields is a transform failure, handled on the listener's NACK (AE) path, not a value
carried on the wire.

This is the source hcs's rule builder writes against without needing to read the listener.

## Where the two halves live

- **`hl7poc.model`** (`src/hl7poc/model.py`) — the types below, `to_json`/`from_json`. Stdlib
  dataclasses and `json` only, no third-party imports: both the listener and worker images install
  this module.
- **`hl7poc.listener.transform`** (`src/hl7poc/listener/transform.py`) — `parse_message(raw: str) ->
  CanonicalMessage`, built on [python-hl7](https://pypi.org/project/hl7/) (the `hl7` PyPI package).
  Raises `TransformError` on anything it can't map.

**The invariant that makes the images separable:** `hl7poc.model` imports nothing third-party, and
`hl7` is imported only from `hl7poc.listener.transform`. While the project is still a single
installable package, `hl7` is declared in the root `pyproject.toml`; once hl7-poc-ouc splits it into
core/listener/worker workspace members, `hl7` moves to the listener member and the "a worker-only
sync cannot `import hl7`" check becomes literally runnable.

## `CanonicalMessage`

```
CanonicalMessage
├── schema_version: int (wire format version; from_json rejects a body whose
│                       schema_version is missing or not the version this code knows)
├── header: MessageHeader
├── patient: Patient
├── visit: Visit | None            (present when the message carries PV1)
├── appointment: Appointment | None (present for SIU family messages, from SCH)
└── result: Result | None          (present for ORU family messages, from OBR/OBX)
```

An unrecognized message type still produces `header` + `patient`; `visit`, `appointment`, and
`result` are `None` unless the corresponding segment is actually present.

### `MessageHeader` — from MSH

| Field | Source |
|---|---|
| `msg_type` | MSH-9.1 |
| `event` | MSH-9.2 |
| `control_id` | MSH-10 |
| `sending_app` | MSH-3 |
| `sending_fac` | MSH-4 |
| `message_ts` | MSH-7 |
| `hl7_version` | MSH-12 |

### `Patient` — from PID (when present; every field optional)

| Field | Source |
|---|---|
| `mrn` | PID-3.1 |
| `family_name` | PID-5.1 |
| `given_name` | PID-5.2 |
| `middle_name` | PID-5.3 |
| `dob` | PID-7 |
| `sex` | PID-8 |

### `Visit` — from PV1, when present

| Field | Source |
|---|---|
| `patient_class` | PV1-2 |
| `location` | PV1-3 |

### `Appointment` — from SCH, when present (SIU family)

| Field | Source |
|---|---|
| `id` | SCH-1 |
| `start` | SCH-11 (TQ), component 4 |
| `end` | SCH-11 (TQ), component 5 |
| `status` | SCH-25.1 |
| `reason` | SCH-7.2 (falls back to SCH-7.1) |

### `Result` — from OBR, when present (ORU family)

| Field | Source |
|---|---|
| `placer_order_number` | OBR-2 |
| `filler_order_number` | OBR-3 |
| `universal_service_id` | OBR-4.1 |
| `status` | OBR-25 |
| `observation_ts` | OBR-7 |
| `observations` | one `Observation` per OBX segment |

### `Observation` — one per OBX

| Field | Source |
|---|---|
| `set_id` | OBX-1 |
| `identifier` | OBX-3.1 |
| `value` | OBX-5 (repetitions kept as raw `~`-joined text; the model has no repeated-value list) |
| `units` | OBX-6 |
| `reference_range` | OBX-7 |
| `abnormal_flags` | OBX-8 |
| `status` | OBX-11 |

All text fields are HL7-unescaped (`\T\` etc.) via python-hl7's `hl7.util.unescape` before landing in
the model.
