#!/usr/bin/env bash
#
# Whether an epic's parent-child children are ALL closed.
#
# Three skills each need this exact check and each had their OWN inline copy
# of the same broken derivation: `/land`'s Section-4 epic-completion flag,
# `/epic-audit`'s "confirm from live state" gate (`.claude/skills/epic-audit/
# SKILL.md`), and `/sweep`'s "epics ready for a human close-decision" step
# (`.claude/skills/sweep/SKILL.md`). All three read `bd show <epic-id>
# --json`'s `.dependents[]` array to enumerate children -- but that array is
# populated ONLY when `bd show` is called with the opt-in `--include-dependents`
# flag (verified live against bd 1.1.0: `dependent_count` is non-zero but the
# `dependents` key is absent from the JSON entirely without the flag -- an
# intended performance opt-in, "may be slow on hub beads" per `bd show
# --help`, not a bd bug). So every one of the three call sites always saw an
# empty `$kids`, and the `(($kids|length)>0)` false-positive guard (there
# deliberately, since `all($kids[]; ...)` is vacuously true on `[]`) always
# tripped short -- dead code, in all three places, that failed silently SAFE
# (a missed check reads identically to "not complete yet").
#
# This script derives the child set via `bd list --parent <epic-id> --all
# --json` instead, which returns every child (open + closed) directly --
# each issue carries its own `.parent` field, so no dependents array, no
# opt-in flag, and no `dependency_type` filtering is needed at all.
#
# Usage: scripts/epic-children-closed.sh <epic-id>
#
# Prints exactly one line to stdout:
#   true   -- the epic has >=1 parent-child child and every one is closed
#   false  -- otherwise, INCLUDING zero children (the false-positive guard
#             above -- do not drop it while reusing this script)
#
# This is a query, not an assertion: exit code is always 0 regardless of the
# answer. Callers layer their own additional guards (epic status, labels
# already applied, issue_type) on top -- this script answers exactly one
# question and nothing else.
#
# Read-only: this script only ever calls `bd list`, never a bd write.

set -euo pipefail

epic_id="${1:?usage: epic-children-closed.sh <epic-id>}"

# --all is REQUIRED: a bare `bd list` is open-only and would silently drop every
# CLOSED child -- i.e. exactly the children that make an epic complete -- turning
# this into a check that never fires.
#
# --limit 0 is load-bearing, not noise. The canonical reason, the bd
# 1.1.0 measurements, and why this is HARDENING rather than a live fix all live
# in /sweep's SKILL.md. The stake is highest at this site: the jq
# check below is `all(.[]; .status == "closed")` over whatever rows come back, so an
# epic with >50 children whose 51st-and-later child is still OPEN would read a
# silently truncated first-50 window and report "true" -- a false "all closed"
# that flags the epic ready-to-audit (or ready-to-close) while real work is
# still open. This script is shared by /land, /epic-audit, and /sweep, so one
# capped read here would mis-fire in all three callers at once.
bd list --parent "$epic_id" --all --limit 0 --json |
  jq -r 'if (length > 0) and (all(.[]; .status == "closed"))
         then "true"
         else "false" end'
