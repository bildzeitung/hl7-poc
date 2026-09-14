#!/usr/bin/env bash
#
# Enumerate the `blocks`-dependents of a ticket -- the tickets that will
# unblock prematurely if <id> closes without being re-pointed.
#
# `/land`'s Bounce section (`.claude/skills/land/SKILL.md`) supersedes a
# bounced/escalated ticket with a fresh rebuild ticket via `bd supersede`,
# which CLOSES the original. Any OTHER ticket that depends on the original
# via a `blocks` edge (e.g. a diagnosis spike gating its follow-ups) then
# reads that blocker as satisfied and unblocks -- against work that still
# sits unbuilt in the rebuild. Bounce re-points each such dependent onto the
# rebuild instead, so it stays blocked until the rebuild actually lands.
#
# This script derives that dependent set via `bd show <id> --json
# --include-dependents`. The `--include-dependents` flag is REQUIRED
#: `bd show`'s `.dependents` array is
# populated ONLY with that opt-in flag ("may be slow on hub beads" per `bd
# show --help`) -- without it, `dependent_count` can be non-zero while
# `.dependents` is absent from the JSON entirely, and a derivation built on
# `.dependents` silently iterates zero times. Three sibling checks
# (scripts/epic-children-closed.sh's callers) hit the identical bug and
# failed silently SAFE (a missed epic-completion flag reads the same as "not
# complete yet"). This site is worse: a dropped re-point fails silently
# UNSAFE -- the dependent unblocks and /code's fan-out can dispatch a builder
# onto it before the rebuild it actually needs even exists.
#
# Usage: scripts/blocks-dependents.sh <id>
#
# Prints the id of each ticket that depends on <id> via a `blocks` edge, one
# per line, in the order `bd show` returns them. Prints nothing if there are
# none, and exits 0 -- "no blocks-dependents" is an answer, not an error; the
# caller (Bounce) treats an empty result (nothing to re-point) the same as a
# non-empty one.
#
# But a FAILED query is NOT an empty one: `set -euo pipefail` below means a
# `bd show` that errors (bd missing, unknown id, DB locked) exits NON-ZERO
# rather than printing nothing and claiming success. Keep it that way -- do
# not "simplify" this into an unconditional exit 0. That non-zero is what
# makes this script's regression tests sabotage-provable: the fake `bd` in
# tests/test_blocks_dependents.py rejects a `bd show` missing
# --include-dependents by exiting 1, and it is pipefail propagating that 1
# out of here that turns a dropped flag into 5 RED tests instead of a silent
# empty result. An unconditional exit 0 would leave the tests green with the
# flag gone -- i.e. it would delete the gate this script exists to be, and
# hand the UNSAFE site back the silent no-op it was extracted to prevent.
#
# The caller (`/land`'s Bounce section, .claude/skills/land/SKILL.md) captures this script's
# output via `if ! DEPS=$(...); then <escalate>; fi` rather than a bare `for DEP in $(...)`, so a bd
# RUNTIME failure here (bd missing, Dolt DB locked, an id it can't resolve) is read as a failed
# derivation and escalates instead of silently re-pointing nothing (an earlier fix; this script itself
# only gates the DERIVATION regressing, which is what an earlier fix was about -- the caller reading the
# exit status is what closes the runtime-failure gap on top of that).
#
# Read-only: this script only ever calls `bd show`, never a bd write. The
# caller (/land Bounce) is the one that runs `bd dep add`.
#
# CONSIDERED AND REJECTED: `bd dep list <id> --direction=up --type=blocks
# --json | jq -r '.[].id'`. It returns an identical id set and needs no opt-in
# flag, which looks like it removes the footgun above outright. It does not
# -- it trades one footgun for two, and both are WORSE than the one it
# removes:
#   * drop `--type=blocks` and you get EVERY dependent (parent-child,
#     discovered-from, related) re-pointed onto the rebuild, silently
#     corrupting the graph rather than merely under-filling it;
#   * drop `--direction=up` and it silently returns <id>'s DEPENDENCIES --
#     a well-formed, plausible, entirely wrong set.
# Neither is a flag whose absence any fixture can distinguish from "this
# ticket has no blocks-dependents", whereas the missing --include-dependents
# IS caught, loudly, by tests/test_blocks_dependents.py. It is also not
# cheaper: `bd dep list --json` embeds whole issue objects (full
# description/design/acceptance_criteria) exactly as `--include-dependents`
# does. No reason to switch; re-deriving this costs more than reading it.

set -euo pipefail

id="${1:?usage: blocks-dependents.sh <id>}"

bd show "$id" --json --include-dependents |
  jq -r '.[0].dependents[]? | select(.dependency_type=="blocks") | .id'
