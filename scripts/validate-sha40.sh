#!/usr/bin/env bash
#
# Validate that a value is a well-formed, full 40-character lowercase-hex git
# SHA -- the shape `metadata.land_head`/`metadata.review_head` must always
# have. Real `git rev-parse` output is always exactly 40
# lowercase hex characters; anything else (a truncated copy, an uppercase
# character, a short prefix) is malformed and must be reported as such rather
# than silently compared against a real branch tip and misread as DRIFT.
#
# BACKGROUND: during the /code fan-out of 2026-08-08, a rebase pickup
# wrote a 39-character land_head into bd metadata -- one hex
# digit short of the real branch tip. Nothing in the pipeline would have
# caught it: /land's Section 2a precheck compares metadata.land_head against
# the actual `origin/land/<id>` tip purely to detect DRIFT (a push after the
# ticket was marked ready), and a malformed value never equals a real SHA
# either, so it would have been thrown back as drift -- which in Section 2a
# means a BOUNCE: the ticket superseded and the branch deleted, rebuilding
# work that was already correct. A malformed value is instead an ESCALATE
# there: the branch is kept, nothing lands, a human re-derives
# and re-writes the field. The
# analogous read site is code-reviewer.md's own review_head comparison. This
# script gives both read sites (and any write site that wants to validate
# before writing) one shared, tested predicate rather than each re-deriving
# a regex inline.
#
# Usage: scripts/validate-sha40.sh <field-name> <value>
#
# Exit 0 -> <value> is exactly 40 lowercase hex characters. Prints nothing.
# Exit 1 -> <value> is NOT well-formed (including empty/unset, which both
#           callers can produce via `jq -r '... // empty'` when the metadata
#           field was never written). Prints one diagnostic line to stderr
#           naming the field, the bad value, and why -- the caller's job is
#           to report this as MALFORMED, distinct from drift/mismatch, never
#           to silently fold it into a drift/needs-rebase/bounce path.
# Exit 2 -> the CALL is wrong (wrong argument count), never a verdict about
#           the content. Same "exit 2 is the machine, never the content"
#           convention as scripts/sweep-digest-id.sh / merge-precheck.sh
#. This distinction is load-bearing here: both callers
#           react to a nonzero exit by reporting MALFORMED METADATA, so if a
#           botched invocation also exited 1, a future edit that dropped an
#           argument would make /land escalate an already-correct ticket while
#           the real defect sat in the markdown call site. A caller seeing 2
#           must fix its own invocation and report nothing about the field.
#
# Not sourced from scripts/gate-lib.sh: that library exists to
# print the multi-line "GATE COULD NOT RUN" advisory a branch-verdict gate
# owes its caller, and its exit-2 cases are machine faults discovered
# mid-run (docker missing, bd failing). This script runs no machine, reads
# no state, and can fail only on its own argument list -- a one-line usage
# message is the whole of what it has to say, so sourcing the library would
# add a fail-closed source guard and an advisory contract to a pure regex
# predicate. It still honors the 0/1/2 meanings the library standardizes.
#
# Deliberately does NOT check that the value is a real, reachable git object
# -- that is a DIFFERENT question (drift: does it match the actual branch
# tip?) that the caller already asks separately. This script only asks "is
# this even shaped like a SHA".

set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "validate-sha40.sh: usage: scripts/validate-sha40.sh <field-name> <value> (got $# args)" \
    "-- this is a broken CALL, not a verdict about any metadata field; fix the invocation and do" \
    "NOT report a malformed-SHA finding on its account." >&2
  exit 2
fi

FIELD="$1"
VALUE="$2"

# An EMPTY value is a distinct condition from a usage error, and both callers
# can produce it: they read the field with `jq -r '... // empty'`, which yields
# "" when the metadata field was never written at all. Reporting that as a
# usage error would send the reader to debug the call site instead of the
# metadata; it is still exit 1, still never drift.
if [ -z "$VALUE" ]; then
  echo "validate-sha40.sh: MISSING: $FIELD is empty or unset in bd metadata -- there is no value" \
    "to compare against a branch tip, so this is NOT drift. Re-derive it (e.g. \`git rev-parse" \
    "<ref>\`) and write the field before re-running." >&2
  exit 1
fi

if [[ "$VALUE" =~ ^[0-9a-f]{40}$ ]]; then
  exit 0
fi

echo "validate-sha40.sh: MALFORMED: $FIELD=\"$VALUE\" is not exactly 40 lowercase hex characters" \
  "(got ${#VALUE} chars) -- this is a malformed SHA, not drift. Do not compare it against a real" \
  "branch tip and do not kick the branch back needs-rebase/bounce on that basis; re-derive the" \
  "value fresh (e.g. \`git rev-parse <ref>\`) and re-write the metadata field." >&2
exit 1
