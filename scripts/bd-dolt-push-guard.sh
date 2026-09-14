#!/usr/bin/env bash
#
# Backstop guard for scripts/bd-dolt-push.sh: refuse to publish a
# suspicious local bd DB over refs/dolt/data.
#
# Background: a code-reviewer/coding launch worktree was observed with a
# STRAY, worktree-local bd DB (a fresh .beads/embeddeddolt, bootstrap-hydrated
# from that branch's committed, passively-lagging .beads/issues.jsonl) instead
# of resolving to the ONE shared main-checkout DB. A bd write against a ticket
# that happened to exist in that stale jsonl snapshot would have succeeded
# SILENTLY against the stray DB, and `bd-dolt-push.sh` would then have
# published that ~245-issue stale DB over refs/dolt/data, REVERTING ~159
# issues of real state cross-machine.
#
# A live diagnostic could NOT reproduce the stray-DB
# mechanism itself -- 10 separate probes, including a live re-run of the
# ticket's own repro steps, all resolved to the ONE shared, authoritative DB.
# So this is deliberately a BACKSTOP against a mechanism that is real (it
# happened once, with a concrete transcript) but not understood, not a fix for
# a reproduced defect. It is scoped to `bd-dolt-push.sh` -- the chokepoint that
# actually publishes cross-machine -- not to every bd write, which would be a
# much larger surface with much more false-positive exposure for comparatively
# little extra safety (a write landing on a stray DB cannot reach any OTHER
# machine until something publishes it).
#
# Two limits of that scope, both accepted and recorded in docs/decisions.md
# -- do not "discover" either one as a bug:
#   - `bd-dolt-push.sh` is the main publisher, not the only one: /challenge
#     calls `bd dolt push` directly as a DELIBERATE exemption from the wrapper
#     (an earlier fix -- it is human-invoked and interactive, so a failed push is seen
#     in the transcript). Those publishes are unguarded, by choice.
#   - This guard covers publishing a stale DB, NOT the incident's other harm: a
#     label swap that silently succeeds against a stray DB strands a ticket
#     invisibly to /land, with no push involved at all. Tracked as an earlier fix.
#
# Refuses (non-zero exit, message on stderr) when EITHER:
#
#   1. The resolved `.beads` directory (via `bd where --json`) contains
#      `.auto-import-issues.jsonl` -- bd's own marker that the local DB was
#      bootstrap-hydrated from a passive jsonl snapshot rather than built up
#      via ordinary dolt-native writes/pulls. This is exactly what the
#      documented incident showed, and CLAUDE.md already treats a jsonl
#      import as never a legitimate substitute for `bd dolt pull` ("import
#      only upserts and silently misses deletions").
#
#   2. The current issue count (`bd count --json`) is below
#      BD_DOLT_PUSH_GUARD_MIN_RATIO_PCT percent (default 90) of a local,
#      per-DB-path high-water-mark cache file that `bd-dolt-push.sh` itself
#      writes immediately after every successful push. This is a network-free
#      proxy for "wildly below the remote's count": our own last
#      confirmed-pushed count is a hard floor, since the real remote can only
#      have grown (or stayed level) since -- it never requires contacting the
#      remote on top of what the push itself already needs.
#
# Deliberately does NOT false-positive on a fresh clone / `bd init`:
#   - `bd init` never calls this script or `bd-dolt-push.sh`.
#   - `bd init` restores state via `bd dolt pull`, never a jsonl import, so it
#     creates no `.auto-import-issues.jsonl` marker.
#   - A freshly-initialized DB has no high-water-mark cache file yet at its
#     resolved path, so check 2 has no baseline to compare against and does
#     not fire (a missing cache is "unknown", not "suspicious").
#
# Fails OPEN (does not block) if `bd where`/`bd count` themselves cannot be
# read -- the underlying `bd dolt push` will surface that failure on its own,
# and blocking here on an unrelated bd hiccup would risk bricking an otherwise
# legitimate push.
#
# Escape hatch: BD_DOLT_PUSH_GUARD_FORCE=1 skips both checks (loudly, on
# stderr) for the rare deliberate case -- disaster recovery, an intentional
# bulk prune immediately followed by a push.
#
# Usage: scripts/bd-dolt-push-guard.sh
# Env overrides: BD_DOLT_PUSH_GUARD_MIN_RATIO_PCT (default 90; must be a
#                  non-negative integer -- a malformed value REFUSES the push
#                  rather than silently skipping the check, see below)
#                BD_DOLT_PUSH_GUARD_FORCE (bypasses both checks; the falsy
#                  spellings 0/false/no/off are NOT a bypass, so writing
#                  "don't force" cannot silently mean the opposite)
#
# This script writes nothing at all -- it only reads. The high-water-mark cache
# it consults is written by bd-dolt-push.sh after a real successful push.

set -euo pipefail

MIN_RATIO_PCT="${BD_DOLT_PUSH_GUARD_MIN_RATIO_PCT:-90}"

# The falsy spellings are deliberately NOT a bypass: with a plain non-empty
# test, BD_DOLT_PUSH_GUARD_FORCE=0 / =false / =no -- the obvious ways to write
# "don't force" -- would SILENTLY disable both checks, which is the same
# "a broken config must never be indistinguishable from a clean DB" trap the
# ratio validation below exists to close. Anything else non-empty forces.
case "${BD_DOLT_PUSH_GUARD_FORCE:-}" in
  '' | 0 | false | FALSE | no | NO | off | OFF) ;;
  *)
    echo "bd-dolt-push-guard: BD_DOLT_PUSH_GUARD_FORCE=${BD_DOLT_PUSH_GUARD_FORCE} -- skipping suspicious-DB checks" >&2
    exit 0
    ;;
esac

# Validate the ratio BEFORE it reaches the arithmetic below, and fail CLOSED.
# Without this, a malformed value silently DISABLES check 2 and lets the push
# through: `$(( last * MIN_RATIO_PCT ))` sits inside an `if` condition, where
# `set -e` is suspended, so e.g. MIN_RATIO_PCT='90%' raises a bash syntax error
# on stderr, evaluates the condition false, and falls through to `exit 0` -- a
# guard reporting "all clear" while doing no checking at all, from a one-character
# typo. (Malformed input was also INCONSISTENT: '90%' exited 0/allowed, while
# 'abc' tripped `set -u` and exited 1/blocked.) Mirrors the same fail-closed
# rule bd-dolt-push.sh already applies to a non-numeric BD_DOLT_PUSH_MAX_ATTEMPTS
# ("Never succeed without having pushed") -- a broken guard config must never be
# indistinguishable from a clean DB. Deliberately placed AFTER the FORCE bypass,
# so the escape hatch keeps working even with a typo'd ratio in the environment.
case "$MIN_RATIO_PCT" in
  '' | *[!0-9]*)
    echo "bd-dolt-push-guard: REFUSING to push -- BD_DOLT_PUSH_GUARD_MIN_RATIO_PCT='${MIN_RATIO_PCT}' is not a non-negative integer (expected e.g. 90, with no '%'). The remedy is to FIX THE VALUE: it is this guard's own config, and refusing beats silently skipping the check. Do not reach for BD_DOLT_PUSH_GUARD_FORCE to get around a typo." >&2
    exit 1
    ;;
esac

where_json=$(bd where --json 2>/dev/null) || {
  echo "bd-dolt-push-guard: 'bd where --json' failed -- cannot assess the resolved DB, not blocking (bd dolt push will surface the underlying failure)" >&2
  exit 0
}

db_dir=$(printf '%s' "$where_json" | jq -r '.path // empty' 2>/dev/null || true)
if [ -z "$db_dir" ]; then
  echo "bd-dolt-push-guard: 'bd where --json' returned no .path -- cannot assess the resolved DB, not blocking" >&2
  exit 0
fi

# Check 1: auto-import-from-jsonl marker.
if [ -f "$db_dir/.auto-import-issues.jsonl" ]; then
  cat >&2 <<EOF
bd-dolt-push-guard: REFUSING to push.

The bd DB resolved at:
  $db_dir
carries $db_dir/.auto-import-issues.jsonl -- bd's own marker that this DB was
auto-hydrated from a passive jsonl snapshot, not built up via ordinary
dolt-native writes/pulls. Publishing it over refs/dolt/data risks reverting
real state cross-machine.

IF YOU ARE AN AGENT: do NOT set BD_DOLT_PUSH_GUARD_FORCE. STOP and escalate to
a human, quoting this message. This guard fires on a mechanism nobody has been
able to reproduce, so a refusal here is information, not an obstacle to route
around -- and forcing it publishes over shared, cross-machine state.

IF YOU ARE A HUMAN and this DB really is what you intend to publish (e.g.
deliberate disaster recovery), re-run with BD_DOLT_PUSH_GUARD_FORCE=1.
EOF
  exit 1
fi

# Check 2: issue count vs. local high-water-mark cache.
count_json=$(bd count --json 2>/dev/null) || {
  echo "bd-dolt-push-guard: 'bd count --json' failed -- cannot assess issue count, not blocking" >&2
  exit 0
}
current=$(printf '%s' "$count_json" | jq -r '.count // empty' 2>/dev/null || true)

cache_file="$db_dir/.bd-dolt-push-guard-highwater"
# Every "cannot assess" input collapses to "" and simply does not fire, which is
# the fail-open half of this guard's contract: a missing / unreadable / garbage
# cache, or an uninterpretable count, is UNKNOWN, not SUSPICIOUS. (A missing
# cache is the normal first-push-on-this-machine state, and the fresh-clone
# case the guard must never block.) A zero baseline is likewise not a baseline.
last=$(cat "$cache_file" 2>/dev/null || true)
case "$current" in '' | *[!0-9]*) current="";; esac
case "$last" in '' | *[!0-9]*) last="";; esac
if [ -n "$current" ] && [ -n "$last" ] && [ "$last" -gt 0 ]; then
  # Refuse when current * 100 < last * MIN_RATIO_PCT (integer arithmetic;
  # avoids a bc/awk dependency for a simple percentage comparison).
  if [ "$((current * 100))" -lt "$((last * MIN_RATIO_PCT))" ]; then
    cat >&2 <<EOF
bd-dolt-push-guard: REFUSING to push.

The bd DB resolved at:
  $db_dir
reports $current issues, wildly below the $last issues this same DB path had
at its last successfully-recorded push (below the
BD_DOLT_PUSH_GUARD_MIN_RATIO_PCT=${MIN_RATIO_PCT}% floor). Publishing it over
refs/dolt/data risks reverting real state cross-machine.

IF YOU ARE AN AGENT: do NOT set BD_DOLT_PUSH_GUARD_FORCE. STOP and escalate to
a human, quoting this message. A count that dropped this far is information,
not an obstacle to route around -- and forcing it publishes over shared,
cross-machine state.

IF YOU ARE A HUMAN and this drop is deliberate (e.g. an intentional bulk prune
you just ran), re-run with BD_DOLT_PUSH_GUARD_FORCE=1.
EOF
    exit 1
  fi
fi

exit 0
