#!/bin/bash -u
#
# Retry wrapper for `bd dolt push`, hardening it against the two concurrent-writer
# failure modes documented in docs/decisions.md and validated in docs/agents-workflow.md:
#
#   1. Rejected push (non-fast-forward). `dolt push` to refs/dolt/data is
#      fast-forward-only + atomically CAS-protected on the branch ref, exactly like
#      `git push` — a losing concurrent writer is REJECTED, never silently dropped,
#      and the fix is mechanical: pull (merge the winner's commit in), then retry.
#   2. Embedded-mode lock contention. harness runs bd in embedded (in-process Dolt
#      engine) mode, and every worktree on a machine shares ONE physical Dolt store
#      (single-writer, enforced via file lock per beads' own docs). A concurrent
#      writer mid-operation produces a transient "database is locked" error that
#      clears as soon as that writer's single operation finishes.
#
# Both failure modes are transient and self-clear within a few seconds under
# harness's actual write pattern (disjoint per-ticket rows, short-lived locks) — a
# short backoff-and-retry loop is the correct-weight fix; switching bd to Dolt
# server mode is deliberately NOT done here (see docs/decisions.md).
#
# Before any of that: scripts/bd-dolt-push-guard.sh runs once, up
# front, as a backstop against publishing a suspicious local DB (one that was
# bootstrap-hydrated from a stale, passive jsonl snapshot instead of built up
# via ordinary dolt-native writes/pulls -- a code-reviewer/coding worktree was
# observed with exactly this once, and would have reverted ~159 issues of
# real cross-machine state had the guard existed then). See that script's own
# header for the full mechanism and the two failure modes it is deliberately
# designed NOT to trip on (a fresh clone / `bd init`, and requiring the
# remote to be reachable on every ordinary bd write).
#
# Usage: scripts/bd-dolt-push.sh [any `bd dolt push` flags, e.g. --remote foo]
# Env overrides (mainly for tests): BD_DOLT_PUSH_MAX_ATTEMPTS, BD_DOLT_PUSH_BASE_DELAY
# Guard env overrides: BD_DOLT_PUSH_GUARD_MIN_RATIO_PCT, BD_DOLT_PUSH_GUARD_FORCE

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$SCRIPT_DIR/bd-dolt-push-guard.sh" || exit 1

MAX_ATTEMPTS="${BD_DOLT_PUSH_MAX_ATTEMPTS:-5}"
BASE_DELAY="${BD_DOLT_PUSH_BASE_DELAY:-2}"

attempt=1
while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
  bd dolt push "$@"
  status=$?
  if [ "$status" -eq 0 ]; then
    # Record this push's confirmed issue count as bd-dolt-push-guard.sh's
    # local high-water-mark baseline for next time. Best-effort
    # and silent on any failure -- never fail an otherwise-successful push
    # over bookkeeping, and a missing/stale cache just means the guard's
    # count check has no baseline next time, which it already treats as
    # "unknown", not "suspicious".
    db_dir=$(bd where --json 2>/dev/null | jq -r '.path // empty' 2>/dev/null) || true
    if [ -n "${db_dir:-}" ]; then
      count=$(bd count --json 2>/dev/null | jq -r '.count // empty' 2>/dev/null) || true
      if [ -n "${count:-}" ]; then
        echo "$count" >"$db_dir/.bd-dolt-push-guard-highwater" 2>/dev/null || true
      fi
    fi
    exit 0
  fi

  if [ "$attempt" -eq "$MAX_ATTEMPTS" ]; then
    echo "bd-dolt-push: giving up after ${MAX_ATTEMPTS} attempts (exit ${status})" >&2
    exit "$status"
  fi

  # Exponential backoff + small jitter, so a herd of concurrent producers doesn't
  # retry in lockstep and re-collide.
  delay=$((BASE_DELAY * (1 << (attempt - 1))))
  jitter=$((RANDOM % BASE_DELAY + 1))
  sleep_for=$((delay + jitter))
  echo "bd-dolt-push: attempt ${attempt}/${MAX_ATTEMPTS} failed (exit ${status}) — pulling + retrying in ${sleep_for}s" >&2

  # Best-effort: fold in whatever the winner pushed so this attempt has a shot at
  # fast-forwarding. If the pull itself fails (e.g. the lock is still held), fall
  # through to the retry anyway — the next push attempt will surface the same
  # failure and keep backing off.
  bd dolt pull || true
  sleep "$sleep_for"
  attempt=$((attempt + 1))
done

# Not reachable for MAX_ATTEMPTS >= 1: the loop only ever leaves via an `exit` above.
# But a zero / negative / non-numeric BD_DOLT_PUSH_MAX_ATTEMPTS skips the loop body
# entirely, and falling off the end here would exit 0 — reporting a successful push
# that never ran, which is the exact silent-strand this wrapper exists to prevent.
# Never succeed without having pushed.
echo "bd-dolt-push: no push attempted (BD_DOLT_PUSH_MAX_ATTEMPTS=${MAX_ATTEMPTS} is not a positive integer)" >&2
exit 1
