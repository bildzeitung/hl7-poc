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
# Env:     MIN_FRAMES (default 10), WAIT_SECS (default 180),
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
WORKER_HTTP_PORT=${WORKER_HTTP_PORT:-8081}

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

cleanup() {
  echo "--- teardown"
  docker compose rm -sf simhospital servicebus mssql >/dev/null 2>&1 || true
  for pid_var in WORKER_PID LISTENER_PID; do
    pid=${!pid_var}
    if [[ -n $pid ]] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid"
      wait "$pid" 2>/dev/null || true
    fi
  done
  if $KEEP; then
    echo "kept: $WORK (listener.log, worker.log, simhospital.log, spool/)"
  else
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

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

# A frame that forwards successfully is unlinked from the spool immediately
# (spool-drains-on-forward is the documented behaviour), so counting current
# spool contents undercounts arrivals. SEEN_LOG accumulates every filename
# ever observed, forwarded or not, as the durable count of frames that landed.
SEEN_LOG=$WORK/seen-frames.log
touch "$SEEN_LOG"
record_seen() { find "$SPOOL" -maxdepth 1 -name '*.hl7' -printf '%f\n' >>"$SEEN_LOG"; }
count_seen() { sort -u "$SEEN_LOG" | wc -l; }
sb_healthy() { curl -s localhost:8080/ready | grep -q '"sb_healthy": *true'; }
worker_progressed() { grep -qE 'session accepted:|notified mrn=' "$WORK/worker.log"; }

deadline=$((SECONDS + WAIT_SECS))
until (($(count_seen) >= MIN_FRAMES)) && sb_healthy && worker_progressed; do
  if ((SECONDS > deadline)); then
    echo "FAIL: chain incomplete after ${WAIT_SECS}s"
    echo "  frames arrived: $(count_seen) (wanted $MIN_FRAMES)"
    echo "  sb_healthy:     $(curl -s localhost:8080/ready)"
    echo "  worker progressed: $(worker_progressed && echo yes || echo no)"
    docker compose logs --tail 20 simhospital
    tail -20 "$WORK/worker.log"
    exit 1
  fi
  record_seen
  sleep 2
done
record_seen
docker compose logs simhospital >"$WORK/simhospital.log" 2>&1

frames=$(count_seen)
rejected=0
[[ -d $SPOOL/rejected ]] && rejected=$(find "$SPOOL/rejected" -type f | wc -l)

echo "--- results"
echo "frames arrived:     $frames"
echo "frames rejected:    $rejected"
echo "sb_healthy:         $(curl -s localhost:8080/ready)"
echo "worker evidence:    $(grep -E 'session accepted:|notified mrn=' "$WORK/worker.log" | tail -5)"

if ((rejected > 0)); then
  echo "FAIL: $rejected frame(s) NACKed and set aside in rejected/"
  exit 1
fi
echo "PASS"
