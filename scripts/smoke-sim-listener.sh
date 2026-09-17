#!/bin/bash
#
# Smoke test: SimHospital -> MLLP -> hl7listener -> spool.
#
# Validates what works without Service Bus: the listener binds MLLP and its
# probes, SimHospital (compose service, locally built image) reaches it via
# host.docker.internal, and every received frame is spooled, mapped, and ACKed
# (nothing lands in rejected/). Forwarding to Service Bus is NOT exercised --
# with no emulator running, forwards fail and frames stay in the spool, which
# is the intended spool-first behaviour.
#
# Usage: scripts/smoke-sim-listener.sh [--keep]
#   --keep   leave the spool dir and logs in place for inspection
# Env:     MIN_FRAMES (default 10), WAIT_SECS (default 120),
#          PATHWAYS_PER_HOUR (default 3600, passed through to compose)
# Exit:    0 pass, 1 fail, 2 precondition not met.

set -euo pipefail

cd "$(dirname "$0")/.."

MIN_FRAMES=${MIN_FRAMES:-10}
WAIT_SECS=${WAIT_SECS:-120}
export PATHWAYS_PER_HOUR=${PATHWAYS_PER_HOUR:-3600}
KEEP=false
[[ ${1:-} == --keep ]] && KEEP=true

# The listener only forwards; any connection string satisfies the required option.
SB_CONN='Endpoint=sb://localhost;SharedAccessKeyName=RootManageSharedAccessKey;SharedAccessKey=SAS_KEY_VALUE;UseDevelopmentEmulator=true;'

die() { echo "PRECONDITION: $*" >&2; exit 2; }

docker image inspect simhospital:latest >/dev/null 2>&1 \
  || die "local image simhospital:latest not found (it is a custom build; nothing to pull)"
for port in 2575 8080; do
  if (echo >/dev/tcp/127.0.0.1/$port) 2>/dev/null; then
    die "port $port already in use"
  fi
done

WORK=$(mktemp -d -t hl7-smoke.XXXXXX)
SPOOL=$WORK/spool
mkdir -p "$SPOOL"
LISTENER_PID=

cleanup() {
  echo "--- teardown"
  docker compose stop simhospital >/dev/null 2>&1 || true
  docker compose rm -f simhospital >/dev/null 2>&1 || true
  if [[ -n $LISTENER_PID ]] && kill -0 "$LISTENER_PID" 2>/dev/null; then
    kill -TERM "$LISTENER_PID"
    wait "$LISTENER_PID" 2>/dev/null || true
  fi
  if $KEEP; then
    echo "kept: $WORK (listener.log, simhospital.log, spool/)"
  else
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

echo "--- starting listener (spool: $SPOOL)"
uv run --frozen hl7listener --servicebus-connection "$SB_CONN" --spool-dir "$SPOOL" \
  >"$WORK/listener.log" 2>&1 &
LISTENER_PID=$!

deadline=$((SECONDS + 30))
until curl -sf localhost:8080/ready >/dev/null; do
  if ((SECONDS > deadline)) || ! kill -0 "$LISTENER_PID" 2>/dev/null; then
    echo "FAIL: listener never became ready"; tail -20 "$WORK/listener.log"; exit 1
  fi
  sleep 1
done
echo "ready: $(curl -s localhost:8080/ready)"

echo "--- starting simhospital (pathways/hour: $PATHWAYS_PER_HOUR)"
docker compose up -d simhospital

count_frames() { find "$SPOOL" -maxdepth 1 -name '*.hl7' | wc -l; }
deadline=$((SECONDS + WAIT_SECS))
until (($(count_frames) >= MIN_FRAMES)); do
  if ((SECONDS > deadline)); then
    echo "FAIL: only $(count_frames) frames spooled in ${WAIT_SECS}s (wanted $MIN_FRAMES)"
    docker compose logs --tail 20 simhospital
    exit 1
  fi
  sleep 2
done
docker compose logs simhospital >"$WORK/simhospital.log" 2>&1

frames=$(count_frames)
rejected=0
[[ -d $SPOOL/rejected ]] && rejected=$(find "$SPOOL/rejected" -type f | wc -l)
sample=$(find "$SPOOL" -maxdepth 1 -name '*.hl7' | sort | head -1)

echo "--- results"
echo "frames spooled:  $frames"
echo "frames rejected: $rejected"
echo "message types:   $(awk -F'|' '/^MSH/ {print $9}' RS='\r' "$SPOOL"/*.hl7 | sort | uniq -c | tr '\n' ';')"
echo "sample MSH:      $(head -c 200 "$sample" | tr '\r' '\n' | head -1)"

if ((rejected > 0)); then
  echo "FAIL: $rejected frame(s) NACKed and set aside in rejected/"
  exit 1
fi
echo "PASS"
