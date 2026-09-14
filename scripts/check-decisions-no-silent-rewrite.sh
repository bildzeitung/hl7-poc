#!/usr/bin/env bash
#
# Silent-in-place-rewrite guard for docs/decisions.md.
#
# tests/test_decisions_supersession_markers.py guards the two marker-SHAPE
# defects an earlier fix found (an off-pattern lead-in, a line-wrapped id). Both
# scans key on an artifact a *marker* leaves behind. A silent in-place
# rewrite of an EXISTING entry -- editing or deleting previously-committed
# text instead of appending, the exact thing docs/decisions.md's own preamble
# forbids -- leaves nothing for a marker-shaped scan to key on.
#
# This script closes that gap with git's own diff: between two points in
# history, did any PRE-EXISTING, non-blank line of docs/decisions.md
# disappear?
#
# SCOPE is base...head, not full repository history: a full-history replay
# was tried and rejected as permanently noisy (paragraph rewrapping alone
# flags dozens of legitimate commits). Full reasoning in docs/decisions.md,
# search "an earlier fix".
#
# Usage: scripts/check-decisions-no-silent-rewrite.sh <base-ref> [<head-ref>]
#   <head-ref> defaults to HEAD. Pass an ordinary ref (e.g. origin/main) --
#   the merge base is resolved here, by the THREE-dot comparison below, and a
#   caller must never hand-compute one.
#
# Exit 0 -> no pre-existing non-blank line of docs/decisions.md was removed
#           between the merge base of the two refs and <head-ref>. Silent.
# Exit 1 -> at least one was. Prints each offending removed line, prefixed
#           with "REMOVED: ", to stdout.
# Exit 2 -> USAGE/MACHINE fault. Per an earlier fix, via gate-lib.sh's shared
#           gate_could_not_run: a machine fault, NEVER a verdict on the
#           content -- a caller must not read it as "no rewrite found".

set -uo pipefail   # deliberately NOT -e: exit 1 is a live content verdict
                   # here, and -e would let an inspected command's own
                   # nonzero status short-circuit the script into it

# The source itself must fail CLOSED -- see gate-lib.sh's Usage
# section for the measurement and why the guard can't use the library it loads.
# shellcheck source=gate-lib.sh
if ! . "$(dirname "$0")/gate-lib.sh" \
     "This is a machine fault a human must fix, not a verdict on" \
     "docs/decisions.md -- never read exit 2 as 'no silent rewrite found'."; then
  echo "GATE COULD NOT RUN: scripts/gate-lib.sh is missing or unreadable" >&2
  echo "next to $0 -- this is a machine/checkout fault, not a branch verdict." >&2
  exit 2
fi

# Arg-count check FIRST, and it must exit 2 -- never `${1:?...}`, whose exit
# status is 1, this script's live "a rewrite was found" verdict.
if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  gate_could_not_run "usage: $0 <base-ref> [<head-ref>]"
fi

BASE_REF="$1"
HEAD_REF="${2:-HEAD}"

if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
  gate_could_not_run "not inside a git repository"
fi

for ref in "${BASE_REF}" "${HEAD_REF}"; do
  if ! git rev-parse --verify --quiet "${ref}^{commit}" >/dev/null; then
    gate_could_not_run "ref '${ref}' does not resolve to a commit"
  fi
done

# THREE dots: git resolves `base...head` to the two refs' MERGE BASE, so only
# <head>'s own changes are scanned. Load-bearing, not stylistic. At review/
# land time the branch under review is routinely BEHIND origin/main, which
# appends to this very file on nearly every land; a two-dot `git diff <base>
# <head>` also reports everything the BASE gained, so main's own new entries
# come back as REMOVED (measured: 12 spurious offenders on this script's own
# branch, zero with three dots) -- the same permanent-noise failure that made
# a full-history replay non-viable, reintroduced at branch scope.
#
# --output-indicator-old marks removed lines with '<' instead of '-', keeping
# them unambiguously distinct from the '--- a/docs/decisions.md' file header.
# Matching a bare '^-' and special-casing '^--- ' would silently SKIP a
# removed content line that itself begins with "-- " (it renders as
# "--- ..."), i.e. fail OPEN on the very thing being guarded.
if ! DIFF_OUTPUT="$(git diff --output-indicator-old='<' \
  "${BASE_REF}...${HEAD_REF}" -- docs/decisions.md)"; then
  gate_could_not_run \
    "git diff '${BASE_REF}...${HEAD_REF}' failed" \
    "(do the two refs share a merge base?)"
fi

# An offender is any '<' line whose content, once trimmed, is non-empty --
# blank-line churn is not a rewrite. File headers, hunk headers ('@@ ... @@'),
# context and added lines are not scanned. The untrimmed text is what gets
# printed, so a human sees the removed line as it was written.
OFFENDERS="$(printf '%s\n' "${DIFF_OUTPUT}" | awk '
  /^</ {
    raw = substr($0, 2)
    trimmed = raw
    gsub(/^[ \t]+|[ \t]+$/, "", trimmed)
    if (length(trimmed) > 0) {
      print "REMOVED: " raw
    }
  }
')"

if [ -n "${OFFENDERS}" ]; then
  printf '%s\n' "${OFFENDERS}"
  exit 1
fi

exit 0
