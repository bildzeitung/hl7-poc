#!/bin/bash
#
# Smoke test: SimHospital -> MLLP -> hl7listener -> Service Bus emulator -> hl7worker.
#
# Full chain, unlike scripts/smoke-sim-listener.sh (which stays the fast
# listener-only check): brings up the Service Bus emulator stack (+ its SQL
# Server dependency), the listener, SimHospital, and the worker, and requires
# evidence that Service Bus forwarding AND worker consumption both happened.
#
# Usage: scripts/smoke-sim-full-chain.sh [--keep]
#   --keep   leave logs and the spool dir in place for inspection
# Env:     MIN_FRAMES (default 10, counted at the simulator), WAIT_SECS (default 180),
#          PATHWAYS_PER_HOUR (default 3600, passed through to compose)
# Exit:    0 pass, 1 fail, 2 precondition not met.

set -euo pipefail

cd "$(dirname "$0")/.."

MIN_FRAMES=${MIN_FRAMES:-10}
WAIT_SECS=${WAIT_SECS:-180}
export PATHWAYS_PER_HOUR=${PATHWAYS_PER_HOUR:-3600}
KEEP=false
[[ ${1:-} == --keep ]] && KEEP=true

# The emulator's fixed developer connection string (README); real, not a stand-in --
# both the listener and the worker forward/consume through it.
SB_CONN='Endpoint=sb://localhost;SharedAccessKeyName=RootManageSharedAccessKey;SharedAccessKey=SAS_KEY_VALUE;UseDevelopmentEmulator=true;'
WORKER_HTTP_PORT=8081  # the listener's 8080 is likewise fixed, below

die() { echo "PRECONDITION: $*" >&2; exit 2; }

docker image inspect simhospital:latest >/dev/null 2>&1 \
  || die "local image simhospital:latest not found (it is a custom build; nothing to pull)"
for port in 2575 8080 "$WORKER_HTTP_PORT" 5672 5300; do
  if (echo >/dev/tcp/127.0.0.1/$port) 2>/dev/null; then
    die "port $port already in use"
  fi
done

WORK=$(mktemp -d -t hl7-smoke-full.XXXXXX)
SPOOL=$WORK/spool
mkdir -p "$SPOOL"
LISTENER_PID=
WORKER_PID=
LOGS_PID=

cleanup() {
  echo "--- teardown"
  docker compose rm -sf simhospital servicebus mssql >/dev/null 2>&1 || true
  for pid in "$LOGS_PID" "$WORKER_PID" "$LISTENER_PID"; do
    [[ -n $pid ]] || continue
    kill -TERM "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
  if $KEEP; then
    echo "kept: $WORK (listener.log, worker.log, simhospital.log, spool/)"
  else
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT
# Untrapped, a signal kills the shell without running the EXIT trap, stranding
# the emulator stack; exiting from the handler routes back through cleanup.
trap 'exit 1' INT TERM

echo "--- starting Service Bus emulator (+ mssql)"
docker compose up -d mssql servicebus

deadline=$((SECONDS + 90))
until curl -sf localhost:5300/health >/dev/null; do
  if ((SECONDS > deadline)); then
    echo "FAIL: servicebus emulator never became healthy"
    docker compose logs --tail 40 servicebus
    exit 1
  fi
  sleep 2
done
echo "servicebus emulator healthy"

echo "--- starting listener (spool: $SPOOL)"
uv run --frozen hl7listener --servicebus-connection "$SB_CONN" --spool-dir "$SPOOL" \
  >"$WORK/listener.log" 2>&1 &
LISTENER_PID=$!

echo "--- starting worker"
uv run --frozen hl7worker --servicebus-connection "$SB_CONN" --http-port "$WORKER_HTTP_PORT" \
  >"$WORK/worker.log" 2>&1 &
WORKER_PID=$!

deadline=$((SECONDS + 30))
until curl -sf "localhost:8080/ready" >/dev/null 2>&1 \
  && curl -sf "localhost:$WORKER_HTTP_PORT/live" >/dev/null 2>&1; do
  if ((SECONDS > deadline)) \
    || ! kill -0 "$LISTENER_PID" 2>/dev/null \
    || ! kill -0 "$WORKER_PID" 2>/dev/null; then
    echo "FAIL: listener and/or worker never became ready"
    tail -20 "$WORK/listener.log"
    tail -20 "$WORK/worker.log"
    exit 1
  fi
  sleep 1
done
echo "listener ready: $(curl -s localhost:8080/ready)"

echo "--- starting simhospital (pathways/hour: $PATHWAYS_PER_HOUR)"
docker compose up -d simhospital
touch "$WORK/simhospital.log"
docker compose logs -f --no-log-prefix simhospital >"$WORK/simhospital.log" 2>&1 &
LOGS_PID=$!

# A frame that forwards successfully is unlinked from the spool within
# milliseconds, so polling the spool would miss nearly every frame on a healthy
# chain; volume is counted at the source instead. sb_healthy and the worker's
# log are early signals only -- the invariant that proves every frame traversed
# is the spool draining to empty once the source stops, with nothing rejected
# by the listener and nothing dead-lettered by the worker.
WORKER_EVIDENCE='session accepted:|notified mrn='
frames_sent() { grep -ci 'sending message' "$WORK/simhospital.log" || true; }
spooled() { find "$SPOOL" -maxdepth 1 -name '*.hl7' | wc -l; }
sb_healthy() { curl -s localhost:8080/ready | grep -q '"sb_healthy": *true'; }
worker_progressed() { grep -qE "$WORKER_EVIDENCE" "$WORK/worker.log"; }

deadline=$((SECONDS + WAIT_SECS))
until (($(frames_sent) >= MIN_FRAMES)) && sb_healthy && worker_progressed; do
  if ((SECONDS > deadline)); then
    echo "FAIL: chain incomplete after ${WAIT_SECS}s"
    echo "  frames sent:       $(frames_sent) (wanted $MIN_FRAMES)"
    echo "  sb_healthy:        $(curl -s localhost:8080/ready)"
    echo "  worker progressed: $(worker_progressed && echo yes || echo no)"
    tail -20 "$WORK/simhospital.log"
    tail -20 "$WORK/worker.log"
    exit 1
  fi
  sleep 2
done

frames=$(frames_sent)
docker compose rm -sf simhospital >/dev/null 2>&1 || true

# With the source stopped, anything still spooled is a frame that never
# forwarded, so the spool must reach empty.
deadline=$((SECONDS + 30))
until (($(spooled) == 0)); do
  if ((SECONDS > deadline)); then
    echo "FAIL: $(spooled) frame(s) still unforwarded in the spool after 30s"
    tail -20 "$WORK/listener.log"
    exit 1
  fi
  sleep 1
done

rejected=$(find "$SPOOL/rejected" -type f 2>/dev/null | wc -l || true)
# The worker dead-letters anything it cannot decode or process, which every
# other signal here survives: it would still accept a session, the listener
# would still forward, and the spool would still drain.
dead_lettered=$(grep -c 'dead-lettering' "$WORK/worker.log" || true)

echo "--- results"
echo "frames emitted by simulator: $frames"
echo "frames rejected by listener: $rejected"
echo "messages dead-lettered:      $dead_lettered"
echo "sb_healthy:                  $(curl -s localhost:8080/ready)"
echo "worker evidence:             $(grep -E "$WORKER_EVIDENCE" "$WORK/worker.log" | tail -5)"

if ((rejected > 0)); then
  echo "FAIL: $rejected frame(s) NACKed and set aside in rejected/"
  exit 1
fi
if ((dead_lettered > 0)); then
  echo "FAIL: the worker dead-lettered $dead_lettered message(s)"
  exit 1
fi
echo "PASS"
