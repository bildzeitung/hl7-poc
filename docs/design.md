# Design

The index for this project's design record. Start here.

## Core problem

[SimHospital](https://github.com/google/simhospital) emits HL7 v2 traffic (ADT, SIU, ORU) over
MLLP. This project turns that traffic into patient notifications: a listener receives the HL7, and
a worker decides which messages are worth telling a patient about and pushes that notification.

## The bet

A **transforming MLLP edge**: the listener parses every HL7 message into one canonical model and
puts only that model — as JSON — on a session-ordered Service Bus queue. No raw HL7 ever travels on
the queue, and the worker never parses HL7 — it consumes the canonical model exclusively. This
reverses an earlier "thin edge, no transform" shape; the reversal and its reasoning are recorded in
[`docs/decisions.md`](decisions.md).

The canonical model's own field list lives in `docs/canonical-model.md`, owned by `hl7-poc-wld.3`
(linked below once that file exists).

Concretely:

- **Listener** (`hl7poc.listener:app`) — a transforming MLLP edge. Spools the raw frame to disk
  first (durability before ACK), parses it with [python-hl7](https://pypi.org/project/python-hl7/)
  into the canonical model, ACKs, and forwards the model as JSON to the `hl7-events` Service Bus
  queue with `session_id` = the patient's MRN. A frame that cannot be mapped is NACKed and set
  aside rather than silently dropped.
- **Worker** (`hl7poc.worker:app`) — a session-aware Service Bus consumer of the canonical model
  only. Decides per message type whether to emit a notification (SIU S12–S15, final ORU^R01); the
  push itself is a print/webhook stand-in for a later real transport.
- **Core** (`hl7poc.model`) — the canonical message model plus JSON (de)serialization, stdlib only.
  The HL7-to-model mapping, and the `hl7` dependency it needs, live in the listener member only, so
  the worker's container image never carries an HL7 parser.

## Principles

- **Durability before ACK.** The listener spools every raw HL7 frame to disk before it ACKs the
  sender. A crash between receipt and forward cannot lose a message; the spool drains once the
  broker is reachable again.
- **Per-MRN ordering.** Service Bus sessions are keyed by the patient's MRN, so one patient's
  events are processed strictly in order at the worker while different patients' events flow
  concurrently.
- **The worker image is HL7-free.** Only `hl7poc.model` (canonical model + JSON) is shared between
  listener and worker; the `hl7` parsing dependency is declared by the listener package alone.
- **Compose is local test infrastructure; the Dockerfiles are shipping code.** `docker-compose.yml`
  and `servicebus-config.json` stand up SimHospital and a Service Bus emulator so a developer can
  run the listener and worker from the checkout with `uv run`. Compose never builds or runs the
  service images — the two `Dockerfile`s under `packages/*/` are what actually ships to a
  container registry and a cluster.

## Shape of the delivered system

- Two independently installable, independently containerized services (listener, worker) sharing
  one `hl7poc-core` distribution, built as a uv workspace of three members.
- Each service is a single-command Typer CLI: `hl7listener` / `hl7worker` runs the service with no
  subcommand; every option is env-var-backed.
- Local development runs both services from the checkout (`uv run hl7listener`, `uv run hl7worker`)
  against a compose stack providing SimHospital and a Service Bus emulator. Production runs the two
  container images, each carrying only its own service plus `hl7poc-core`, against a real Service
  Bus namespace.
- With the compose stack up, SimHospital traffic flows end to end: the listener ACKs, canonical
  JSON bodies land on `hl7-events` with `session_id` = MRN, and the worker consumes them and prints
  notifications for the qualifying message types. No raw HL7 ever reaches the queue.

## Build sequencing

The epic `hl7-poc-wld` sequenced its children as follows (see the epic in the tracker for the
authoritative order and current status):

1. `hl7-poc-c1t` — minimal Typer skeleton for both CLIs, plus the `uv_build` backend (landed).
2. `hl7-poc-wld.3` — canonical message model in core; HL7 → model mapping in the listener via
   python-hl7 (after 1). Field list: `docs/canonical-model.md` once `hl7-poc-wld.3` lands, or that
   ticket's own record until then.
3. `hl7-poc-wld.2` — local broker config: `servicebus-config.json` declaring the `hl7-events`
   session queue (no code dependency).
4. `hl7-poc-abh` — the listener itself (after 1, 2, 3).
5. `hl7-poc-hcs` — the worker itself (after 1, 2, 3).
6. `hl7-poc-ouc` — uv workspace split into `packages/core`, `packages/listener`, `packages/worker`
   (after 1; import paths stay stable so 2/4/5 can land in either order relative to it).
7. `hl7-poc-4f5` — one Dockerfile per service (after 6). Container contract: `docs/containers.md`
   once that ticket lands.
8. `hl7-poc-wld.1` — remove the leftover release/versioning machinery (after 1).
9. `hl7-poc-wld.4` — the design record written from the ticket history: this document and its
   companions (no blockers; done).

Closed along the way: `hl7-poc-izt` (versioning: every distribution stays at static `0.0.0`) and
`hl7-poc-5mx` (shared HL7 segment/field helpers, superseded once `hl7-poc-wld.3` gave the listener a
real parser and the worker stopped seeing HL7 altogether).

## Companion docs

- [`docs/decisions.md`](decisions.md) — every settled decision, append-only, plus what's
  deliberately deferred.
- [`docs/configuration.md`](configuration.md) — every tunable knob and build constant, per service.
- `docs/canonical-model.md` — the canonical model's field list (owned by `hl7-poc-wld.3`; not yet
  written).
- `docs/containers.md` — the container build/run contract (owned by `hl7-poc-4f5`; not yet
  written).
