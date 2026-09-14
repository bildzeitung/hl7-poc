#!/usr/bin/env bash
#
# Remove a branch AND every branch stacked on it from /land's accepted set.
#
# THE INVARIANT THIS ENFORCES (Section 3a): "a base that leaves the merge set
# takes its dependents with it." Ordering the set up front is not sufficient,
# because a base can drop OUT later -- kicked back on a real merge conflict, or
# bounced during isolation. If it does and its dependents stay, the loop merges
# a dependent whose base is no longer landing, putting the departed base's
# un-landed, just-rejected content onto the default branch under the
# DEPENDENT's ticket name.
#
# WHY A SCRIPT. This was a shell recipe written inside a COMMENT in
# .claude/skills/land/SKILL.md -- a `grep -vxF`/`mv` pair the agent was expected
# to implement inline, plus a transitive dependent lookup it had to do by hand,
# at the exact moment a conflict had just fired. Detection was made executable
# (scripts/stacked-graph.sh); leaving the ACTION as prose is the half of the fix
# that actually decides what reaches the default branch.
#
# THE REDUCTION MUST BE WRITTEN TO THE FILE, not just to the caller's shell
# variable: the isolation-replay loop re-reads the file, and would otherwise
# re-merge a branch this pass already kicked back.
#
# Usage:
#   scripts/drop-from-accepted.sh <id> --accepted <file> [--graph <file>]
#
#   <id>         the branch leaving the merge set
#   --accepted   /land's ordered accepted-set file, rewritten in place
#   --graph      scripts/stacked-graph.sh output. Omit only when the pass has
#                no stacked branches at all; omitting it when there ARE stacks
#                silently skips the dependent drop, which is the whole point.
#
# Output (stdout), one per line, in the order they were removed:
#   DROPPED <id>      the branch named on the command line
#   HELD    <id>      a dependent removed with it -- the caller owes each of
#                     these a HELD note; they are not conflicted and not
#                     rejected, they simply have no foundation this pass.
#
# Exit codes: 0 = reduction applied (or the id was already absent, which is
# idempotent and fine), 2 = machine fault. There is no exit 1: removing a branch
# from a set is not a verdict about it.
set -u

# The source itself must fail CLOSED -- see gate-lib.sh's Usage
# section for the measurement and why the guard can't use the library it loads.
# The export this script was ported from open-coded the "GATE COULD NOT RUN:"
# banner inline, which is exactly the stranded-copy pattern the rule exists for
# (and which is invisible to tests/test_gate_lib.py's consumer sweep, since
# that sweep anchors on this exact source line). Same fix as its sibling port
# scripts/stacked-graph.sh.
# shellcheck source=gate-lib.sh
if ! . "$(dirname "$0")/gate-lib.sh" --no-advisory; then
  echo "GATE COULD NOT RUN: scripts/gate-lib.sh is missing or unreadable" >&2
  echo "next to $0 -- this is a machine/checkout fault, not a branch verdict." >&2
  exit 2
fi

ID=""
ACCEPTED=""
GRAPH=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --accepted) shift; [ "$#" -gt 0 ] || gate_could_not_run "--accepted needs a value"; ACCEPTED="$1" ;;
    --graph)    shift; [ "$#" -gt 0 ] || gate_could_not_run "--graph needs a value"; GRAPH="$1" ;;
    -*)         gate_could_not_run "unknown argument '$1'" \
                  "usage: drop-from-accepted.sh <id> --accepted <file> [--graph <file>]" ;;
    *)          [ -z "$ID" ] || gate_could_not_run "more than one id given"; ID="$1" ;;
  esac
  shift
done

[ -n "$ID" ]       || gate_could_not_run "no id given"
[ -n "$ACCEPTED" ] || gate_could_not_run "--accepted is required"
# A MISSING accepted file is a machine fault, never an empty set: continuing
# would report "nothing to drop" for a reduction that never happened.
[ -f "$ACCEPTED" ] || gate_could_not_run "accepted file '$ACCEPTED' does not exist"
if [ -n "$GRAPH" ] && [ ! -f "$GRAPH" ]; then
  gate_could_not_run "graph file '$GRAPH' does not exist" \
    "Pass the file scripts/stacked-graph.sh wrote, or omit --graph only if this pass has no stacks."
fi

# Dependents of $ID = every EDGE whose BASE is $ID. stacked-graph.sh emits the
# transitive closure, so one pass catches a dependent-of-a-dependent too; no
# closure walk is needed (and hand-rolling one here is how this goes wrong).
DEPENDENTS=""
if [ -n "$GRAPH" ]; then
  # Deduped IN awk (`!seen[$2]++`) rather than by piping to `sort -u`, so the
  # substitution is a SINGLE command and its status is awk's own. Through a
  # pipeline the status would be sort's, and an awk read failure would be
  # masked -- degrading to the silent "no dependents" this script exists to
  # remove. The guard is escalated out here at top level, never from inside the
  # substitution: an `exit` there ends only the subshell.
  DEPENDENTS="$(awk -F'\t' -v base="$ID" '$1 == "EDGE" && $3 == base && !seen[$2]++ { print $2 }' "$GRAPH")" \
    || gate_could_not_run "could not read dependents out of graph file '$GRAPH'" \
         "Reading this as 'no dependents' would leave a stacked branch in the merge set."
fi

# ONE rewrite for the WHOLE reduction, never one per id. Atomicity is the
# reason, not speed: escalating out of a per-id loop would leave the accepted
# file HALF-reduced while exiting 2, breaking the same "no partial reduction may
# be applied on a fault" invariant this script's own refusals are built around.
# `grep -f -` takes every id at once, and `-xF` keeps each one an exact,
# whole-line fixed string, so an id that is a prefix of another is still not
# collateral.
DROP_LIST="$ID"
[ -z "$DEPENDENTS" ] || DROP_LIST="$DROP_LIST
$DEPENDENTS"

# `$$`-suffixed: a fixed sibling name would have two concurrent passes over the
# same accepted file clobbering each other's temp.
TMP="$ACCEPTED.tmp.$$"

# grep exits 1 when it filters out the LAST remaining line, and an empty
# accepted set is a legitimate outcome here -- an all-kicked-back pass. So 1 is
# a CONTENT result, not a fault. Anything ABOVE 1 (unreadable file, I/O error)
# is a machine fault and must NOT reach the `mv`: a blanket `|| true` there
# would move a truncated temp file over the accepted set, silently emptying the
# merge set. Same 1-vs-else partition as gate-lib.sh's escalate_unless_content,
# open-coded because that helper escalates on rc 0 -- it assumes an if/else
# arm's `$?`, where 0 already went down the other arm, and here 0 is the
# ordinary success path.
printf '%s\n' "$DROP_LIST" | grep -vxF -f - "$ACCEPTED" > "$TMP"
rc=$?
[ "$rc" -le 1 ] || {
  rm -f "$TMP"
  gate_could_not_run "grep failed (exit $rc) reducing '$ACCEPTED'" \
    "The accepted set is left untouched; do not read this as an empty merge set."
}
mv -f "$TMP" "$ACCEPTED"

# Reported only AFTER the reduction has reached the file: a caller must never
# see a DROPPED/HELD line for a reduction that did not land on disk.
printf 'DROPPED\t%s\n' "$ID"
for dep in $DEPENDENTS; do
  printf 'HELD\t%s\n' "$dep"
done

exit 0
