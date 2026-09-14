#!/usr/bin/env bash
#
# Resolve /sweep's single digest issue -- the one bd issue carrying the reserved
# `sweep-digest` label -- and REFUSE unless there is exactly one
# (.claude/skills/sweep/SKILL.md sections 5 and 6).
#
# Extracted technical review. /sweep runs each fenced bash block
# as its own Bash tool invocation, so no shell state crosses between them: the
# `$DIGEST_ID` that section 4 resolves cannot reach section 5 (which READS the
# digest body) or section 6 (which REWRITES it wholesale). Both therefore have to
# re-derive it. Written inline that was two byte-for-byte copies of the same bd
# query + jq count + refusal, already drifting in their diagnostics -- exactly the
# "logic shared by two call sites belongs in scripts/, never duplicated in
# markdown" rule docs/agents-workflow.md states, and the same "ungated inline
# shell in a SKILL.md rots silently" lesson as scripts/epic-children-closed.sh
#,
# scripts/release-latest-tag.sh and scripts/release-bump.sh
#.
#
# Usage: scripts/sweep-digest-id.sh
#
# Exit 0  -> prints the digest issue's id to stdout. Exactly one exists.
# Exit 1  -> NOT exactly one digest. Diagnostic to stderr, nothing to stdout.
#            This is a legitimate state, not a fault, and the caller must not
#            proceed past it:
#              N == 0  section 4's bootstrap/no-op path owns this. Either the
#                      queue is empty (a clean no-op pass) or section 4 has not
#                      created the digest yet.
#              N >  1  section 4's anomaly path owns this: "do not guess which is
#                      authoritative and do not write anything." A human
#                      consolidates by hand. Picking `.[0].id` here would
#                      silently overwrite whichever duplicate happened to sort
#                      first -- the precise failure section 4 forbids, and the
#                      reason this refusal is mechanical rather than prose.
# Exit 2  -> MACHINE FAULT (bd or jq failed, malformed JSON). Same "exit 2 is the
#            machine, never the content" convention as scripts/release-bump.sh /
#            scripts/release-latest-tag.sh / scripts/merge-precheck.sh.
#
# Read-only: only ever calls `bd list`, never a bd write.

set -euo pipefail

if [ "$#" -ne 0 ]; then
  echo "sweep-digest-id.sh: takes no arguments (got: $*)" >&2
  exit 2
fi

# --all so a CLOSED digest still counts (a closed duplicate is still a duplicate a
# human must resolve, and silently ignoring it would resurrect the N>1 ambiguity).
# --limit 0 for the same reason every `bd list` in /sweep passes it: no truncation.
if ! rows="$(bd list --label sweep-digest --all --limit 0 --json 2>/dev/null)"; then
  echo "sweep-digest-id.sh: \`bd list --label sweep-digest\` failed" >&2
  exit 2
fi

# `(. // [])` because bd serializes an empty result set as `null`, not `[]`.
if ! n="$(printf '%s' "$rows" | jq '(. // []) | length' 2>/dev/null)"; then
  echo "sweep-digest-id.sh: could not parse \`bd list\` JSON" >&2
  exit 2
fi

if [ "$n" -ne 1 ]; then
  echo "sweep-digest-id.sh: expected exactly 1 issue labelled sweep-digest, found $n." >&2
  if [ "$n" -eq 0 ]; then
    echo "  No digest exists yet -- /sweep section 4's N==0 path owns this (bootstrap, or" >&2
    echo "  a clean no-op pass on an empty queue). Do not read or write a digest here." >&2
  else
    echo "  Duplicate digests -- /sweep section 4's N>1 anomaly path owns this. Do NOT guess" >&2
    echo "  which is authoritative and do NOT write. Report the ids and let a human" >&2
    echo "  consolidate (keep one, strip the sweep-digest label off the rest):" >&2
    printf '%s' "$rows" | jq -r '(. // []) | .[] | "    \(.id)\t\(.title)"' >&2
  fi
  exit 1
fi

printf '%s' "$rows" | jq -r '.[0].id'
