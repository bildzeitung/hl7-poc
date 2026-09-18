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
| `FORWARDED_REPORT_URL` | `--report-url` | none (no reporting) | Full URL of the dashboard's `POST /api/forwarded` endpoint the listener fire-and-forgets an empty event to after each successful forward; when unset, no HTTP call is made at all. Feeds the dashboard's derived queue-depth estimate (see Dashboard row below). Deliberately NOT named `REPORT_URL`: that is the worker's `/api/handled` knob, and one shared exported value would silently count handled events as forwarded. |

## Worker (`hl7worker`)

| Env var | CLI option | Default | Meaning |
|---|---|---|---|
| `SERVICEBUS_CONNECTION` | `--servicebus-connection` | none (required) | Connection string for the Service Bus namespace (or emulator) the worker consumes from. |
| `SERVICEBUS_QUEUE` | `--servicebus-queue` | `hl7-events` | Queue the worker pulls canonical model JSON from. |
| `WEBHOOK_URL` | `--webhook-url` | none (prints to stdout instead) | Where the worker POSTs a notification payload; when unset, the worker prints the JSON payload. |
| `HTTP_PORT` | `--http-port` | `8081` | Port for the `/live` probe endpoint (the worker has no `/ready` — it has no inbound traffic to gate on). |
| `REPORT_URL` | `--report-url` | none (no reporting) | Full URL of the dashboard's `POST /api/handled` endpoint the worker fire-and-forgets a handled-message event to after each message (completed or dead-lettered); when unset, no HTTP call is made at all. |

## Dashboard (`hl7dashboard`)

| Env var | CLI option | Default | Meaning |
|---|---|---|---|
| `DASHBOARD_PORT` | `--port` | `8082` | Port the dashboard serves `/`, `/api/status`, `POST /api/handled` and `POST /api/forwarded` on. |
| `LISTENER_READY_URL` | `--listener-ready-url` | `http://localhost:8080/ready` | Listener `/ready` URL the dashboard polls into `/api/status`. |
| `SPOOL_DIR` | `--spool-dir` | `./spool` | Spool directory the dashboard counts `*.hl7` files in. |
| `BUS_HEALTH_URL` | `--bus-health-url` | `http://localhost:5300/health` | Bus emulator `/health` URL the dashboard polls into `/api/status`. |

`/api/status`'s `queue_depth` has no CLI knob of its own -- it is always `max(0, forwarded_total -
handled_total)`, DERIVED rather than queried, because the Service Bus emulator has no working
admin/runtime-properties API for queue depth (spiked 2026-09-18: `ServiceBusAdministrationClient`
targets a management HTTPS port the emulator never opens, and its `:5300` admin port's
`MessageCount` field doesn't update on send). `forwarded_total` comes from the listener's
`FORWARDED_REPORT_URL` (above); `handled_total` is the worker's `REPORT_URL`-fed completed+dead_lettered count
(see Worker row above). See `hl7poc.dashboard.status.derive_queue_depth`'s docstring for the full
accuracy caveats.

## Build constants

Fixed values carried over from the reference implementations, not exposed as env vars:

| Constant | Value | Where it applies |
|---|---|---|
| Spool retry / drain interval | 5s | Listener: how often the retry loop re-drains the spool. |
| MLLP close budget | 8s | Listener: time budget on `SIGTERM`/`SIGINT` to wait for already-accepted MLLP connections to close on their own; when it expires the listener force-closes the still-open ones (`close_clients()`), so this stage cannot hang. |
| Shutdown drain budget | 8s | Listener: time budget on `SIGTERM`/`SIGINT` to drain the spool before exiting. |
| Sender close budget | 4s | Listener: time budget (applied twice: once for the queue sender, once for the `ServiceBusClient` itself) on `SIGTERM`/`SIGINT` to close the Service Bus AMQP connection before giving up. |
| Worker shutdown budget | 30s | Worker: time budget on `SIGTERM`/`SIGINT` to finish the in-flight session before exiting. |
| Session idle wait | 5s | Worker: how long the pump waits for `NEXT_AVAILABLE_SESSION` before looping. |
| Lock renewal max | 300s | Worker: `AutoLockRenewer`'s maximum lock renewal duration for a session. |
| Probe read timeout | 3s | Both: timeout for a probe HTTP handler read; defined once as `hl7poc.probe.READ_TIMEOUT`. |
| Webhook timeout | 5s | Worker: timeout for a webhook POST when `WEBHOOK_URL` is set. |
| Report timeout | 2s | Worker: timeout for a handled-event POST when `REPORT_URL` is set; defined once as `hl7poc.worker.REPORT_TIMEOUT`. Listener: same value/purpose for its forwarded-event POST when `FORWARDED_REPORT_URL` is set, defined separately as `hl7poc.listener.REPORT_TIMEOUT`. |
| Handled ring size | 50 | Dashboard: number of most-recent `/api/handled` events kept in memory (`hl7poc.dashboard.HANDLED_RING_SIZE`); older events roll off, only the running totals survive. |

## Local SimHospital demo and smoke test

| Env var | Used by | Default | Meaning |
|---|---|---|---|
| `PATHWAYS_PER_HOUR` | `docker-compose.yml` (SimHospital's `-pathways_per_hour` flag); `scripts/smoke-sim-listener.sh`; `scripts/smoke-sim-full-chain.sh` | `60` in compose; both smoke scripts export `3600` when unset | Rate SimHospital generates pathways (and therefore HL7 messages) at. The scripts' higher default lets their `WAIT_SECS` window reliably see `MIN_FRAMES`. |
| `MIN_FRAMES` | `scripts/smoke-sim-listener.sh`; `scripts/smoke-sim-full-chain.sh` | `10` (both) | Frames required before the smoke test passes: spooled by the listener (listener-only script), or sent by SimHospital (full-chain script). |
| `WAIT_SECS` | `scripts/smoke-sim-listener.sh`; `scripts/smoke-sim-full-chain.sh` | `120` (listener-only); `180` (full-chain) | Seconds the smoke test waits for `MIN_FRAMES` (full-chain script: also for the listener's Service Bus health and worker progress) before failing. |

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

## Service Bus emulator: duplicate-detection window

`servicebus-config.json`'s `hl7-events` queue sets `DuplicateDetectionHistoryTimeWindow` to `PT5M`
(`RequiresDuplicateDetection` stays `true`). The emulator image rejects any value above `PT5M` and
exits at startup, so `PT5M` is the ceiling, not a chosen retention target.

This window is a second line of defense, not the mechanism that stops the listener's own
direct-forward-vs-retry-drain race: the listener tracks, in-process, which spool files a direct
forward (`process_frame` -> `_forward`) currently owns (`hl7poc.listener`'s `in_flight` set), and the
5s spool retry drain (`drain_spool`) skips any file still owned rather than forwarding it a second
time. This closes the race regardless of whether MSH-10 is populated. The broker's duplicate
detection still matters for what the in-process guard cannot cover: a re-send after a listener crash
that lands more than 5 minutes after the original send is not deduplicated (and never was), and a
frame with no MSH-10 falls back to a fresh random message id per send (`build_service_bus_message`),
so it also would not be deduplicated by the broker alone.
