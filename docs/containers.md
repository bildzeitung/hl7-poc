# Containers

## Component view

```mermaid
flowchart LR
    sender["HL7 sender<br/>(SimHospital / MLLP client)"]

    subgraph listener_proc["Listener process (hl7poc.listener)"]
        mllp["MLLP server<br/>(handle_mllp / process_frame)"]
        fwd["_forward<br/>(one coroutine, both callers)"]
        spool[("Spool dir<br/>*.hl7 files")]
        rejected[("rejected/<br/>(unmappable frames)")]
        retry["retry_loop<br/>(drain_spool, 5s sleep)"]
        probe_l["/live, /ready<br/>(HTTP probe)"]
    end

    queue[["Service Bus queue<br/>hl7-events<br/>(session_id = MRN)"]]

    subgraph worker_proc["Worker process (hl7poc.worker)"]
        pump["pump<br/>(session receiver)"]
        decide["decide()<br/>notification rules"]
        probe_w["/live<br/>(HTTP probe)"]
    end

    push_target["Webhook / stdout<br/>(push stand-in)"]
    dlq[("Dead-letter queue<br/>(poison messages)")]

    sender <-->|"MLLP frame + ACK/NACK"| mllp
    mllp -->|"write raw bytes<br/>(durable before ACK)"| spool
    mllp -->|"create_task (direct)"| fwd
    retry -->|"glob, skip in_flight"| spool
    retry -->|"await (retry)"| fwd
    fwd -->|"canonical JSON"| queue
    fwd -.->|"unlink on success"| spool
    mllp -.->|"unmappable frame"| rejected
    retry -.->|"unmappable frame"| rejected
    queue -->|"session-ordered<br/>canonical JSON"| pump
    pump --> decide
    decide -->|"payload (or none)"| push_target
    pump -.->|"unprocessable"| dlq
```

The direct forward and `retry_loop` are two callers of the same `_forward` coroutine, not two
channels. The `in_flight` set keeps a `retry_loop` pass from re-sending a spool file a direct
forward still owns; it does not cover two concurrent `drain_spool` passes, which is why shutdown
stops `retry_loop` before the final drain (below).

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
    participant L as serve()

    S->>M: MLLP frame (VT...FS CR)
    M->>D: write raw bytes (durable)
    alt spool write fails
        M-->>S: NACK (MSA-1 = AE)
    else UTF-8 decode or transform fails
        M->>D: move to rejected/ (never retried)
        M-->>S: NACK (MSA-1 = AE)
    else transform succeeds
        M->>F: mark in_flight, create_task(_forward)
        M-->>S: ACK (MSA-1 = AA)
        F->>Q: send canonical JSON (session_id = MRN)
        alt send succeeds
            F->>D: unlink spool file
        else send fails
            F->>F: log, leave file for retry_loop
        end
        F->>M: done: clear in_flight
    end

    Q->>W: session-ordered delivery (async)
    W->>W: decide() -> push or drop, then complete (or dead-letter)

    loop until shutting_down, 5s sleep between passes
        R->>D: glob *.hl7, skip files in in_flight
        R->>F: await _forward for each remaining file
    end

    Note over L: Shutdown (SIGTERM/SIGINT)
    L->>L: shutting_down = true (/ready -> 503)
    L->>M: close MLLP server (8s bound, then close_clients)
    L->>R: cancel retry_loop, await it fully
    Note over L,R: retry_loop is awaited to completion BEFORE the<br/>final drain, so two drain_spool passes never run<br/>concurrently and no spool file is forwarded twice
    L->>F: await in-flight forward tasks
    L->>D: final drain_spool pass
    Note over L,D: forward-await + final drain share one 8s bound
    L->>Q: close sender, then Service Bus client (4s bound each)
```

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
