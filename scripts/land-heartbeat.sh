#!/usr/bin/env bash
#
# Re-stamp (or release) the single-lander lock using THIS pass's own token.
#
# WHY THIS EXISTS. The token read-back was six lines of boilerplate repeated at
# seven sites in .claude/skills/land/SKILL.md -- read the token file, warn if
# empty, call land-lock.sh -- and a coverage gate had to check that every one of
# the seven carried its warning, because paraphrasing one silently dropped it
# out of coverage. One script means one copy to get right.
#
# The token lives in a FILE beside the lock, not a variable: no shell state
# survives between the skill's separate Bash invocations, and it sits outside
# $STATE_DIR because Section 1's per-pass scratch wipe would otherwise delete it
# mid-pass. It is lock state, not per-pass scratch.
#
# Usage:
#   scripts/land-heartbeat.sh                  # re-stamp (best effort, never fails the pass)
#   scripts/land-heartbeat.sh --release        # release at a normal end-of-pass exit
#
# Exit codes: always 0 on the heartbeat path -- lock bookkeeping must never stop
# a pass that is otherwise fine. --release propagates land-lock.sh's own status
# so a caller can report a failed release, but no caller is required to act.
# The ONE exception is exit 2, a usage error: same reservation land-lock.sh
# makes, for the same reason. A mistyped flag (`--relase`) must not quietly
# degrade a release into a heartbeat -- that leaves the lock held for the whole
# staleness window while the caller's exit status says everything went fine.
set -u

case "$#:${1:-}" in
  0:) MODE="heartbeat" ;;
  1:--release) MODE="release" ;;
  *)
    echo "usage: $0 [--release]" >&2
    exit 2
    ;;
esac

# Sibling resolution is `$(dirname "$0")`, the same idiom scripts/land-merge-one.sh
# uses for every script it shells out to -- NOT a repo-graph lookup. The upstream
# template reached for `git rev-parse --show-toplevel` here; going through the repo
# graph to find a file that is literally next to this one buys nothing, and it
# forced every test to plant a copy of land-lock.sh inside its throwaway repo.
LAND_LOCK="$(dirname "$0")/land-lock.sh"

# Same idiom as land-lock.sh's own $LOCK resolution: `--path-format=absolute
# --git-common-dir`, not bare `--git-dir`. From a LINKED worktree bare --git-dir
# returns that worktree's PRIVATE gitdir, so a reader using it would look in the
# wrong place for a token SKILL.md writes at the shared --git-common-dir path.
# SKILL.md's write site, its two Section 3 read-back sites and this read site all
# use the one idiom and must stay paired -- changing one alone silently orphans
# the token.
GITDIR="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || GITDIR=""
if [ -z "$GITDIR" ]; then
  echo "land-heartbeat: WARNING -- not inside a git repository; no $MODE performed" >&2
  exit 0
fi

TOKEN="$(cat "$GITDIR/land-lock-token" 2>/dev/null || true)"

# An absent token is NOT fatal and NOT silently ignored. land-lock.sh refuses a
# blind call outright rather than acting on an unowned lock, so the honest
# outcome is: say so, do nothing, let the staleness window handle it.
if [ -z "$TOKEN" ]; then
  if [ "$MODE" = "release" ]; then
    echo "land: WARNING -- no own-token available; land-lock ownership check is DISABLED for this" \
      "call -- land-lock.sh REFUSES a blind release, so the lock stays held until it ages out" >&2
  else
    echo "land: WARNING -- no own-token available; land-lock ownership check is DISABLED for this" \
      "call -- so this heartbeat simply does not fire" >&2
  fi
  exit 0
fi

if [ "$MODE" = "release" ]; then
  exec "$LAND_LOCK" release "$TOKEN"
fi

"$LAND_LOCK" heartbeat "$TOKEN" || true
exit 0
