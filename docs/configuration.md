# Configuration

Every tunable knob and build constant for this project, in one place. Each service option is a
Typer `Annotated` option backed by an environment variable (see
[`docs/conventions.md`](conventions.md) for the Typer/Annotated fiat).

## Listener (`hl7listener`)

| Env var | CLI option | Default | Meaning |
|---|---|---|---|
| `MLLP_PORT` | `--mllp-port` | `2575` | Port the listener binds for incoming MLLP HL7 traffic. |
| `HTTP_PORT` | `--http-port` | `8080` | Port for the `/live` and `/ready` probe endpoints. |
| `SERVICEBUS_CONNECTION` | `--servicebus-connection` | none (required) | Connection string for the Service Bus namespace (or emulator) the listener forwards to. |
| `SERVICEBUS_QUEUE` | `--servicebus-queue` | `hl7-events` | Queue the listener forwards canonical model JSON to. |
| `SPOOL_DIR` | `--spool-dir` | `./spool` locally, `/spool` in the container image | Directory raw HL7 frames are durably spooled to before forwarding. |

## Worker (`hl7worker`)

| Env var | CLI option | Default | Meaning |
|---|---|---|---|
| `SERVICEBUS_CONNECTION` | `--servicebus-connection` | none (required) | Connection string for the Service Bus namespace (or emulator) the worker consumes from. |
| `SERVICEBUS_QUEUE` | `--servicebus-queue` | `hl7-events` | Queue the worker pulls canonical model JSON from. |
| `WEBHOOK_URL` | `--webhook-url` | none (prints to stdout instead) | Where the worker POSTs a notification payload; when unset, the worker prints the JSON payload. |
| `HTTP_PORT` | `--http-port` | `8081` | Port for the `/live` probe endpoint (the worker has no `/ready` — it has no inbound traffic to gate on). |

## Build constants

Fixed values carried over from the reference implementations, not exposed as env vars:

| Constant | Value | Where it applies |
|---|---|---|
| Spool retry / drain interval | 5s | Listener: how often the retry loop re-drains the spool. |
| MLLP close budget | 8s | Listener: time budget on `SIGTERM`/`SIGINT` to wait for already-accepted MLLP connections to close before giving up and draining anyway. |
| Shutdown drain budget | 8s | Listener: time budget on `SIGTERM`/`SIGINT` to drain the spool before exiting. |
| Worker shutdown budget | 30s | Worker: time budget on `SIGTERM`/`SIGINT` to finish the in-flight session before exiting. |
| Session idle wait | 5s | Worker: how long the pump waits for `NEXT_AVAILABLE_SESSION` before looping. |
| Lock renewal max | 300s | Worker: `AutoLockRenewer`'s maximum lock renewal duration for a session. |
| Probe read timeout | 3s | Both: timeout for a probe HTTP handler read; defined once as `hl7poc.probe.READ_TIMEOUT`. |
| Webhook timeout | 5s | Worker: timeout for a webhook POST when `WEBHOOK_URL` is set. |

## Local SimHospital demo and smoke test

| Env var | Used by | Default | Meaning |
|---|---|---|---|
| `PATHWAYS_PER_HOUR` | `docker-compose.yml` (SimHospital's `-pathways_per_hour` flag) | `60` in compose | Rate SimHospital generates pathways (and therefore HL7 messages) at. |
| `PATHWAYS_PER_HOUR` | `scripts/smoke-sim-listener.sh` (exported before invoking compose) | `3600` in the script | Same knob, overridden to a much higher rate so the smoke test's `WAIT_SECS` window reliably sees `MIN_FRAMES` spooled. |
| `MIN_FRAMES` | `scripts/smoke-sim-listener.sh` | `10` | Frames that must be spooled by the listener before the smoke test passes. |
| `WAIT_SECS` | `scripts/smoke-sim-listener.sh` | `120` | Seconds the smoke test waits for `MIN_FRAMES` before failing. |

`PATHWAYS_PER_HOUR` has two different defaults depending on entry point: `docker-compose.yml`
defaults to `60` (a demo-friendly rate) when run directly; `scripts/smoke-sim-listener.sh` exports
`3600` before invoking compose, overriding that default so the smoke test's `WAIT_SECS` window
reliably observes `MIN_FRAMES` spooled frames.

## Python and image tags

Rationale for the Python version and the image tags: [`docs/decisions.md`](decisions.md).

- Python 3.14 end to end (`.python-version` at the repo root matches the local venv).
- Listener/worker container build stage: `ghcr.io/astral-sh/uv:python3.14-bookworm-slim`.
- Listener/worker container runtime stage: `python:3.14-slim-bookworm`.

## Local Service Bus emulator connection string

The value a local `hl7listener`/`hl7worker` should pass for `SERVICEBUS_CONNECTION` is the
emulator's fixed developer connection string, recorded in [README.md's "Local Service Bus
emulator" section](../README.md#local-service-bus-emulator) — the canonical location. Not
duplicated here.
