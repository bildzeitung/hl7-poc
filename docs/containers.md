# Containers

## Component view

```mermaid
flowchart LR
    sender["HL7 sender\n(SimHospital / MLLP client)"]

    subgraph listener_proc["Listener process (hl7poc.listener)"]
        mllp["MLLP server\n(handle_mllp)"]
        spool[("Spool dir\n*.hl7 files")]
        rejected[("rejected/\n(unmappable frames)")]
        retry["retry_loop\n(drain_spool every 5s)"]
        probe_l["/live, /ready\n(HTTP probe)"]
    end

    queue[["Service Bus queue\nhl7-events\n(session_id = MRN)"]]

    subgraph worker_proc["Worker process (hl7poc.worker)"]
        pump["pump\n(session receiver)"]
        decide["decide()\nnotification rules"]
        probe_w["/live\n(HTTP probe)"]
    end

    push_target["Webhook / stdout\n(push stand-in)"]

    sender <-- "MLLP frame + ACK/NACK" --> mllp
    mllp -- "write raw bytes\n(durable before ACK)" --> spool
    mllp -- "forward canonical JSON\n(direct, best-effort)" --> queue
    retry -- "re-read + forward\nunsent spool files" --> spool
    retry -- "forward canonical JSON" --> queue
    mllp -. "unmappable frame" .-> rejected
    retry -. "unmappable frame" .-> rejected
    queue -- "session-ordered\ncanonical JSON" --> pump
    pump --> decide
    decide -- "payload (or none)" --> push_target
```

Divergence note: `mllp` forwards directly via an in-process `asyncio.create_task`, not through the
spool queue depicted as a separate hop — the arrow above is the same `_forward` coroutine `retry_loop`
also calls, not a second channel. The `in_flight` set (not shown) is what keeps a direct forward and
a concurrent `retry_loop` drain pass from sending the same spool file twice.

## Message lifecycle (including shutdown drain)

```mermaid
sequenceDiagram
    participant S as HL7 sender
    participant M as handle_mllp
    participant D as disk spool
    participant F as _forward (task)
    participant Q as Service Bus (hl7-events)
    participant R as retry_loop
    participant W as worker.pump

    S->>M: MLLP frame (VT...FS CR)
    M->>D: write raw bytes (spool, durable)
    alt UTF-8 decode or transform fails
        M->>D: move to rejected/ (never retried)
        M-->>S: NACK (MSA-1 = AE)
    else transform succeeds
        M-->>S: ACK (MSA-1 = AA)
        M->>F: create_task(forward), mark in_flight
        F->>Q: send canonical JSON (session_id = MRN)
        Q->>W: session-ordered delivery
        W->>W: decide() -> push or drop
        F->>D: unlink spool file on send success
        F--xD: leave file on send failure (retried by retry_loop)
    end

    loop every 5s, independent of direct forwards
        R->>D: glob *.hl7, skip files in in_flight
        R->>Q: forward whatever remains
        R->>D: unlink on success
    end

    Note over M,R: Shutdown (SIGTERM/SIGINT)
    M->>M: state.shutting_down = true (/ready -> 503)
    M->>M: close MLLP server (bounded wait, then close_clients)
    M->>R: cancel retry_loop, await it fully
    Note over M,R: retry_loop is awaited to completion BEFORE the\nfinal drain runs, so the two never call drain_spool\nconcurrently and no spool file is forwarded twice
    M->>F: await in-flight forward tasks
    M->>D: final drain_spool pass (bounded, 8s)
    M->>Q: close sender, then Service Bus client
```

Two production hops write to `hl7-events` from the listener side (a direct forward per received
frame, and the periodic `retry_loop` drain for anything a direct forward missed or failed); only one
of them ever touches a given spool file, enforced by the `in_flight` set and, at shutdown, by fully
awaiting `retry_loop`'s cancellation before `_final_drain` runs (fixed under `hl7-poc-8g2`).

Two images, one per service, each built from the repo root so the build context
includes the root `pyproject.toml`, `uv.lock`, and the shared `packages/core`
member.

```bash
docker build -f packages/listener/Dockerfile -t hl7poc-listener .
docker build -f packages/worker/Dockerfile -t hl7poc-worker .
```

Both follow the same two-stage shape: a `ghcr.io/astral-sh/uv:python3.14-bookworm-slim`
builder stage runs `uv sync --frozen --no-dev --no-editable --package hl7poc-<svc>`
(deps cached as their own layer before source is copied in), then a
`python:3.14-slim-bookworm` runtime stage copies only `/app/.venv`, runs as a
non-root user, and `ENTRYPOINT`s straight on the console script with no `CMD` —
the bare command runs the service. There is no `HEALTHCHECK`: k8s probes
`/live` and `/ready` directly instead.

## Env-var contract

Both images are configured entirely through environment variables — nothing
is baked in at build time. Defaults and CLI-flag equivalents live in
[`docs/configuration.md`](configuration.md), the single source for them; this
section only says which variables each image reads.

**Listener** (`hl7poc-listener`, `EXPOSE 2575 8080`):

- `SERVICEBUS_CONNECTION`, `SERVICEBUS_QUEUE` — where canonical messages are forwarded
- `HTTP_PORT` — the `/live` and `/ready` probe port
- `MLLP_PORT` — the MLLP listen port
- `SPOOL_DIR` — durable spool directory; set to `/spool` in the image, owned by
  the non-root user and declared as a `VOLUME`, so a k8s volume mount survives a
  container restart

**Worker** (`hl7poc-worker`, `EXPOSE 8081`):

- `SERVICEBUS_CONNECTION`, `SERVICEBUS_QUEUE` — the canonical-message queue to consume
- `HTTP_PORT` — the `/live` probe port
- `WEBHOOK_URL` — where qualifying notifications are pushed

## Local test infrastructure vs. shipping code

`docker-compose.yml` and `servicebus-config.json` are **local test
infrastructure**: SimHospital stands in for an HL7 source and the Service Bus
emulator stands in for the broker, so a developer can run the listener and
worker from the checkout (`uv run hl7listener` / `uv run hl7worker`) against
something that looks like production. These Dockerfiles are **shipping
production code** destined for a k8s cluster. Compose never builds or runs
either service image, and it never will — that boundary is deliberate, not an
oversight to "complete" later.

Out of scope here: k8s manifests, Helm charts, an image registry, and CI image
publishing are separate tickets when wanted. Image scanning and SBOM
generation are likewise out of scope.

## Service Bus emulator's SQL dependency

The `servicebus` service in `docker-compose.yml` needs a SQL Server-compatible
backing store; its `mssql` neighbor is `mcr.microsoft.com/mssql/server:2022-latest`,
matching Microsoft's current emulator setup docs. It used to be
`mcr.microsoft.com/azure-sql-edge:latest`, but Azure SQL Edge was retired on
2025-09-30, so that image is no longer maintained.
