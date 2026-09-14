#!/usr/bin/env bash
#
# Is a `git worktree lock` reason STALE (the session that acquired it is
# dead), or must it still be treated as LIVE?
#
# WHY THIS EXISTS: /land's Section 4 worktree-GC backstop skips every
# `locked` worktree unconditionally, before any other predicate runs. The
# lock recorded by the Claude Code harness (every `isolation: "worktree"`
# launch worktree, plus `.claude/agents/coding.md`'s own explicit
# pre-first-commit lock) is PER-SESSION, not per-agent -- confirmed by
# measurement: several worktrees can share ONE lock-owner pid
# (the session/parent process), so a dead session leaves EVERY worktree it
# ever locked stuck behind the `locked` check forever -- `locked` is tested
# first, ahead of any merged/dirty predicate, so nothing downstream ever
# gets a chance to reclaim them.
#
# WHY NOT PLAIN `kill -0 <pid>` (the usual unsafe idiom): a pid is reused by
# the OS once its original process exits, so `kill -0` alone cannot tell
# "the original session is still running" from "an unrelated later process
# happens to have been assigned the same pid." /land's OWN single-lander
# lock (scripts/land-lock.sh) hit exactly this class of problem and solved
# it with a WALL-CLOCK staleness window instead, because the pid it records
# is a single Bash tool sub-invocation that has, by construction, already
# exited by the time a later invocation reads it -- pid liveness is
# structurally meaningless there (see land-lock.sh's own header).
#
# THIS case is different: the pid recorded in a `git worktree lock` reason
# is the long-lived HARNESS/AGENT SESSION process, not a single Bash
# sub-invocation, so pid liveness IS a meaningful signal here -- it just
# needs the reuse hazard closed. `/proc/<pid>/stat`'s own `starttime` field
# (the 22nd whitespace-separated field, counting `(comm)` as one field) is
# the fix: the harness records a `start <token>` alongside the pid at lock
# time, and that token is the SAME starttime value `/proc/<pid>/stat` itself
# reports for that pid at the moment the lock was taken. A pid that has
# since been reused shows a *different* starttime than the one recorded, so
# comparing the two closes the reuse hole without resorting to a fixed
# wall-clock window (which would either reclaim a genuinely still-running,
# merely-long-lived session, or leave a dead session's worktrees leaked for
# the entire window regardless of how obviously dead they are).
#
# Usage: worktree-lock-stale.sh <lock-reason-text>
#   <lock-reason-text> is the porcelain `locked` line's reason with the
#   leading "locked " stripped, e.g.
#   "claude agent agent-<hash> (pid 1838142 start 76727921)" -- or empty,
#   for a lock taken with no reason at all (e.g. a human's own `git worktree
#   lock` with no message).
#
# Exit 0 -- STALE: the recorded pid is not running at all, or it IS running
#           but /proc's starttime for it no longer matches the recorded
#           token (pid reuse) -- safe to treat this lock as not actually
#           held by a live session.
# Exit 1 -- NOT proven stale: either the lock is genuinely live (pid
#           running, starttime matches), or the reason text couldn't be
#           parsed, or /proc/<pid>/stat couldn't be read. FAILS CLOSED in
#           every one of those ambiguous cases -- a lock this script cannot
#           positively prove dead is treated as live, never reclaimed.
#           Getting this wrong in the other direction risks exactly what
#           an earlier fix already cost once: destroying a live build's worktree.
#           Also returned on a usage error (wrong argument count) -- a
#           caller bug is not evidence of staleness either.

set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <lock-reason-text>" >&2
  exit 1
fi
reason="$1"

pid=$(printf '%s' "$reason" | sed -n 's/.*[^0-9]pid \([0-9][0-9]*\).*/\1/p')
token=$(printf '%s' "$reason" | sed -n 's/.*[^0-9]start \([0-9][0-9]*\).*/\1/p')

[ -n "$pid" ] || exit 1                # can't parse a pid at all -- fail closed (live)
kill -0 "$pid" 2>/dev/null || exit 0   # pid not running at all -- stale
[ -n "$token" ] || exit 1              # pid alive but no token recorded to compare -- fail closed

stat_file="/proc/$pid/stat"
[ -r "$stat_file" ] || exit 1          # can't read /proc for a pid we just proved is alive -- fail closed

# Robust starttime extraction: comm (field 2) is parenthesized and may itself
# contain spaces or even parens, so a naive `awk '{print $22}'` can misalign.
# Match up to the LAST ')' in the line instead (greedy `.*\)`) -- Linux
# guarantees every field after it is a single space-separated token with no
# parens -- then starttime is the 20th field of THAT remainder (fields 3..
# of the original line renumber to 1.. here, so original field 22 ==
# remainder field 20). Verified against a real /proc/<pid>/stat on this host.
proc_start=$(awk '{
  match($0, /.*\)/)
  rest = substr($0, RSTART + RLENGTH + 1)
  split(rest, f, " ")
  print f[20]
}' "$stat_file" 2>/dev/null)

[ -n "$proc_start" ] || exit 1                  # couldn't parse -- fail closed
[ "$proc_start" != "$token" ] && exit 0         # pid reused by a different process -- stale
exit 1                                           # still the same process -- live
