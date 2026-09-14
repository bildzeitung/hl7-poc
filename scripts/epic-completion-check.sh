#!/usr/bin/env bash
#
# Decide whether closing a ticket completes its parent epic, # /land's Section-4 epic-completion check must flag a parent epic
# `epic-ready-to-audit` once its last open child closes -- that label is the
# ONLY automatic trigger for `/epic-audit`.
#
# The original inline snippet (`.claude/skills/land/SKILL.md`) enumerated an
# epic's children by reading `bd show <epic-id> --json`'s `.dependents[]`
# array, which is only populated with the opt-in `--include-dependents` flag
# -- see scripts/epic-children-closed.sh (this script's own child-completion
# check, and the same fix reused by /epic-audit and /sweep, which each had
# their own copy of the identical bug) for the full mechanism writeup.
#
# Usage: scripts/epic-completion-check.sh <closed-ticket-id>
#
# Prints exactly one line to stdout when the epic should be flagged:
#   READY <epic-id>      -- <epic-id> is an open epic whose last parent-child
#                            child just closed, and it does not already carry
#                            epic-ready-to-audit or epic-audited
# Prints nothing (exit 0) otherwise: no parent, the parent isn't an open
# epic, an open child remains, or the false-positive guard above holds.
#
# Read-only: this script only ever calls `bd show`/`bd list`, never a bd
# write. The caller (/land Section 4) is the one that runs `bd label add`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

id="${1:?usage: epic-completion-check.sh <closed-ticket-id>}"

parent_id=$(bd show "$id" --json | jq -r '.[0].parent // empty')

if [ -z "$parent_id" ]; then
  exit 0
fi

epic_json=$(bd show "$parent_id" --json)

# Cheap guards FIRST, child query second. Everything here is answerable from
# `epic_json` alone, so rejecting now skips the `bd list --parent` subprocess
# entirely. That ordering pays off exactly when it matters: when several
# siblings of one epic land in the same /land pass, the first one labels the
# epic epic-ready-to-audit, and every later sibling is rejected by the label
# guard below without paying for the child query.
printf '%s' "$epic_json" | jq -e '
  .[0] as $e
  | ($e.labels // []) as $lbl
  | ($e.issue_type == "epic")
    and ($e.status != "closed")
    and (($lbl | index("epic-audited")) | not)
    and (($lbl | index("epic-ready-to-audit")) | not)
' >/dev/null || exit 0

# The false-positive guard lives inside epic-children-closed.sh: zero children
# reads `false`, never `true` (`all(.[]; ...)` is vacuously TRUE on an empty
# array). Do not re-derive this check here -- call the shared script.
[ "$("$SCRIPT_DIR/epic-children-closed.sh" "$parent_id")" = "true" ] || exit 0

echo "READY $parent_id"
