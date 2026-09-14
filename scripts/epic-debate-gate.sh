#!/usr/bin/env bash
#
# Decide whether a single bd ticket should be auto-selected by /code's ready-
# frontier filter, /code must refuse to auto-select a child
# ticket whose parent epic has never been debated at least once. `/challenge`
# stamps a durable `epic-debated` label on an epic when it debates it
# (.claude/skills/challenge/SKILL.md); this script is the mechanical check
# `/code`'s auto-select step (.claude/skills/code/SKILL.md, step 2) runs for
# each candidate that already survived the existing human/epic filter
#.
#
# Usage: scripts/epic-debate-gate.sh <ticket-id>
#
# A child links to its epic via a `parent-child` dependency whose target has
# `issue_type: epic` (verified against real bd output: `bd show <child-id>
# --json` embeds the epic as a nested object inside `.dependencies[]`, not via
# the top-level `parent_id`/`epic_id` fields, which are null). This script wants
# that embedded object specifically -- it needs the EPIC's own `issue_type` and
# labels, not just its id, and the deps entry carries them in the same call.
#
# NB: `parent_id`/`epic_id` being null does NOT mean "no top-level
# parent field" -- `bd show <child-id> --json` also exposes a `.parent` scalar,
# which IS populated and agrees with this walk on every ticket sampled. It is
# the simpler read when only the parent's ID is needed, and is what the sibling
# scripts/epic-completion-check.sh uses. Both are correct; they differ in what
# they return, not in reliability. `bd ready
# --json` doesn't carry `dependencies` at all, so this script re-fetches the
# candidate via `bd show` to get them.
#
# Prints exactly one line to stdout:
#   BUILD <id>                                    -- no parent epic, or the
#                                                     parent epic carries
#                                                     epic-debated
#   SKIP <id> epic not debated (<epic-id>)         -- parent epic exists and
#                                                     lacks the marker
#
# Read-only: this script only ever calls `bd show`, never a bd write.

set -euo pipefail

id="${1:?usage: epic-debate-gate.sh <ticket-id>}"

ticket_json=$(bd show "$id" --json)

epic_id=$(printf '%s' "$ticket_json" | jq -r '
  (.[0].dependencies // [])
  | map(select(.dependency_type == "parent-child" and .issue_type == "epic"))
  | (.[0].id // empty)
')

if [ -z "$epic_id" ]; then
  echo "BUILD $id"
  exit 0
fi

epic_json=$(bd show "$epic_id" --json)
debated=$(printf '%s' "$epic_json" | jq -r '(.[0].labels // []) | any(. == "epic-debated")')

if [ "$debated" = "true" ]; then
  echo "BUILD $id"
else
  echo "SKIP $id epic not debated ($epic_id)"
fi
