# PoC for HL7 Messages

This project explores HL7 messages.

## Infrastructure

* Google SimHospital -- HL7 events
* Microsoft Azure Service Bus Simulator

## Services

* listener: Receive messages from SimHospital; transform them into a canonical
            format and then send them into the service bus

* worker: Pull messages from the service bus and perform some trivial task
          according the particular message type received

## Local Service Bus emulator

`docker-compose.yml` mounts `servicebus-config.json` into the emulator, declaring the
`hl7-events` queue (sessions + duplicate detection enabled; see the file for details).
A local `hl7listener`/`hl7worker` should connect with the emulator's fixed developer
connection string:

```
Endpoint=sb://localhost;SharedAccessKeyName=RootManageSharedAccessKey;SharedAccessKey=SAS_KEY_VALUE;UseDevelopmentEmulator=true;
```

## Smoke test: SimHospital -> listener

`scripts/smoke-sim-listener.sh` validates the path that works without Service Bus:
SimHospital sends MLLP to a local `hl7listener`, which spools, maps and ACKs every
frame. Service Bus forwarding is **not** covered -- with no emulator up, forwards fail
and frames stay in the spool, as designed.

Preconditions: Docker Desktop (supplies `host.docker.internal`), the locally built
`simhospital:latest` image, and ports 2575/8080 free.

```bash
scripts/smoke-sim-listener.sh          # PASS/FAIL, exit 0/1 (2 = precondition)
scripts/smoke-sim-listener.sh --keep   # keep spool + logs for inspection
```

It passes when at least `MIN_FRAMES` (default 10) frames are spooled within
`WAIT_SECS` (default 120) and none were rejected. It runs SimHospital at
`PATHWAYS_PER_HOUR=3600` (compose defaults to 60).

### Demonstrating it by hand

Use three terminals to show each piece live:

1. **Listener:**

   ```bash
   uv run --frozen hl7listener \
     --servicebus-connection 'Endpoint=sb://localhost;SharedAccessKeyName=RootManageSharedAccessKey;SharedAccessKey=SAS_KEY_VALUE;UseDevelopmentEmulator=true;' \
     --spool-dir /tmp/demo-spool
   ```

2. **Readiness:** `curl -s localhost:8080/ready` shows `mllp_listening: true`, and
   `sb_healthy: false` since no bus is running.
3. **Simulator:** `PATHWAYS_PER_HOUR=3600 docker compose up simhospital`. Its log shows
   `Sending message` lines. The dashboard is at http://localhost:8000/simulated-hospital/.
4. **Arrivals:** `watch -n2 'ls /tmp/demo-spool | wc -l'` shows the count rising.
   To view a message, run `tr '\r' '\n' < "$(ls /tmp/demo-spool/*.hl7 | head -1)"`.
   `/tmp/demo-spool/rejected/` stays absent or empty.
5. **Teardown:** `docker compose rm -sf simhospital`, then stop the listener with Ctrl-C.

## Smoke test: full chain (SimHospital -> listener -> Service Bus -> worker)

`scripts/smoke-sim-full-chain.sh` covers what `smoke-sim-listener.sh` deliberately
leaves out: Service Bus forwarding and the worker. It brings up the Service Bus
emulator stack (`servicebus` + its `mssql` dependency), the listener, SimHospital,
and `hl7worker`, and requires evidence that messages both reached Service Bus
(`/ready` reports `sb_healthy: true`, and frames leave the spool once forwarded)
and were consumed by the worker (its log shows a session accepted).

Preconditions: Docker Desktop, the locally built `simhospital:latest` image, and
ports 2575/8080/8081/5672/5300 free.

```bash
scripts/smoke-sim-full-chain.sh          # PASS/FAIL, exit 0/1 (2 = precondition)
scripts/smoke-sim-full-chain.sh --keep   # keep spool + logs for inspection
```

It passes when, within `WAIT_SECS` (default 180), the simulator has emitted at least
`MIN_FRAMES` (default 10) frames, `sb_healthy` is true, and the worker's log shows it
accepted a session — then, with the simulator stopped, the spool drains to empty with
nothing rejected by the listener and nothing dead-lettered by the worker. That last
group is the real proof; the rest are early signals. Volume is counted at the
simulator because a successfully forwarded frame is unlinked from the spool within
milliseconds, so the spool counts what is *stuck*, not what arrived.

### Demonstrating it by hand

Five terminals: the listener, the simulator, the emulator, the worker, and one
for checks. The order is deliberate. Bringing Service Bus up late lets the spool
fill so you can watch it drain, and bringing the worker up last lets the queue
build a backlog so you can watch the worker consume it.

Before the demo, run `docker compose pull mssql servicebus` so the emulator
starts without a download.

1. **Listener (terminal 1):**

   ```bash
   uv run --frozen hl7listener \
     --servicebus-connection 'Endpoint=sb://localhost;SharedAccessKeyName=RootManageSharedAccessKey;SharedAccessKey=SAS_KEY_VALUE;UseDevelopmentEmulator=true;' \
     --spool-dir /tmp/demo-spool
   ```

2. **Simulator (terminal 2):** `PATHWAYS_PER_HOUR=3600 docker compose up simhospital`.
   Its log shows `Sending message` lines. The dashboard is at http://localhost:8000/simulated-hospital/.
3. **Spool fills (terminal 5):** `curl -s localhost:8080/ready` shows
   `mllp_listening: true` and `sb_healthy: false`. Then run
   `watch -n2 'ls /tmp/demo-spool | wc -l'` and leave it running: the count climbs
   because there is no bus to forward to.
4. **Service Bus emulator (terminal 3):** `docker compose up mssql servicebus`.
   Wait until `curl -s localhost:5300/health` returns `{"status":"healthy"}`.
5. **Spool drains (terminal 5):** within a few seconds (the spool retry runs every 5s), `/ready` shows
   `sb_healthy: true` and the spool count falls to zero as the listener forwards
   its backlog. The messages now wait in the `hl7-events` queue.
6. **Worker (terminal 4):**

   ```bash
   uv run --frozen hl7worker \
     --servicebus-connection 'Endpoint=sb://localhost;SharedAccessKeyName=RootManageSharedAccessKey;SharedAccessKey=SAS_KEY_VALUE;UseDevelopmentEmulator=true;'
   ```

   It logs a burst of `session accepted: <mrn>` lines as it works through the
   queued backlog, then settles to the simulator's pace. For SIU/final-ORU events
   it also logs `notified mrn=...`.
7. **Teardown:** `docker compose rm -sf simhospital servicebus mssql`, then stop
   the listener and worker with Ctrl-C.
