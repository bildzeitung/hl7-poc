#!/usr/bin/env bash
#
# Whether an epic-audited epic's `epic-audited` label has gone stale because
# the epic gained a parent-child child AFTER the audit that stamped it.
#
# `/epic-audit` is otherwise terminal for an audited epic (SKILL.md: "I don't
# re-arm automatically"), so a child added post-audit -- via bd create
# --parent, a /land bounce re-parent, or by hand -- closes without the epic
# ever being re-reviewed, and the epic-audited claim silently goes stale
# (hl7-poc-2bo). `/epic-audit` stamps metadata `audited_at` (UTC,
# YYYY-MM-DDTHH:MM:SSZ) alongside the label on every audit pass; this script
# derives staleness from that stamp rather than trusting the label alone.
#
# Only parent-child children count, matching epic-children-closed.sh's own
# derivation: a discovered-from ticket is not part of the epic's scope and
# must not re-arm it.
#
# Usage: scripts/epic-audit-stale.sh <epic-id>
#
# Prints exactly one line:
#   true  -- the epic carries epic-audited AND has >=1 parent-child child
#            whose created_at postdates the stamped audited_at
#   false -- otherwise: not epic-audited, no audited_at metadata recorded
#            (an epic-audited epic from before this mechanism existed --
#            treated as not stale rather than force-re-auditing every
#            legacy epic-audited epic), or every parent-child child
#            predates the stamp
#
# ISO-8601 UTC timestamps of this shape sort lexicographically in
# chronological order, so a plain string `>` comparison in jq is exact --
# no date parsing needed.
#
# Read-only: bd show / bd list only, never a bd write.

set -euo pipefail

epic_id="${1:?usage: epic-audit-stale.sh <epic-id>}"

epic_json=$(bd show "$epic_id" --json)

printf '%s' "$epic_json" | jq -e '(.[0].labels // []) | index("epic-audited")' >/dev/null || {
  echo "false"
  exit 0
}

audited_at=$(printf '%s' "$epic_json" | jq -r '.[0].metadata.audited_at // empty')

if [ -z "$audited_at" ]; then
  echo "false"
  exit 0
fi

bd list --parent "$epic_id" --all --limit 0 --json |
  jq -r --arg audited_at "$audited_at" \
    'if any(.[]; .created_at > $audited_at) then "true" else "false" end'
