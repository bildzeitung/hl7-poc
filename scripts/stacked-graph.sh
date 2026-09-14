#!/usr/bin/env bash
#
# Detect stacked land branches -- which `land/<id>` was built on top of which
# other still-unlanded `land/<base>` -- and print the relation.
#
# WHY THIS IS A SCRIPT AND NOT A FENCED SNIPPET. This logic previously lived as
# bash inside .claude/skills/land/SKILL.md, and it did not parse: the outer
# "for every ordered pair" loop existed only as a COMMENT, and a bare
# `continue` sat outside any loop. The agent therefore re-derived an O(n^2)
# driver freehand on every pass, for an algorithm whose subtleties that file
# spends forty lines warning about -- with no test possible, because there was
# no code to test. The failure mode is silent: an undetected stack lets /land
# merge a dependent BEFORE its base, dragging the base's unreviewed content
# onto the default branch under the dependent's ticket name.
#
# DETECTION -- shared history off the default branch, NOT tip-ancestry.
# Two branches cut independently share nothing but the default branch. So the
# relation to test is: does any of their merge-bases lie OFF it? If one does,
# the pair shares non-default history, and that shared commit is a base's tip
# at the moment a dependent merged it.
#
#   * Enumerate ALL merge-bases (`--all`), never the single-result form. A base
#     that takes a needs-rebase pickup AFTER a dependent merged it acquires a
#     SECOND merge-base -- the dependent's own cut point, which IS an ancestor
#     of the default branch. Single-result `git merge-base` returns one of the
#     two ARBITRARILY; when it returns the on-branch one, the pair reads as
#     unrelated and the stack goes undetected.
#   * Do NOT reduce this to `git merge-base --is-ancestor <X> <Y>` ("is X's tip
#     contained in Y"). A base's tip MOVES after a dependent merges it -- its
#     reviewer pushes fixes, a pickup merges the default branch in -- so the
#     tip stops being an ancestor and the whole stack goes invisible. That is
#     the NORMAL flow, not a corner case: a producer stacks on a base precisely
#     while that base is still unlanded and therefore still moving.
#
# DIRECTION -- shared history is necessary, not sufficient. An off-branch
# merge-base means one of two things:
#   * A STACK: one of the pair merged the other. The shared commit is on the
#     base's first-parent spine but not the dependent's (which reached it
#     through a merge, i.e. a second parent). An edge is emitted.
#   * SIBLINGS: two dependents that each merged the same third base share that
#     base's commits too. There the shared commit is off BOTH spines, neither
#     ordering matches, and NO edge is emitted -- which is the correct answer.
#     Each sibling is still detected against the base by its own pair.
#
# KNOWN GAPS, documented rather than papered over:
#   1. Force-push. The test survives an append but not a rewrite. Nothing in
#      this harness force-pushes a land branch; if one ever is, that pass's
#      graph is not trustworthy.
#   2. Branched-from-base rather than merged-base. If a producer branches
#      directly off `land/<base>` instead of branching from the default branch
#      and merging the base in, the shared commit lands on BOTH first-parent
#      spines, so no direction is emitted -- the pair is detected as related
#      but unordered. Indistinguishable from a sibling pair by signature, which
#      is why it is reported (see --report-unordered) rather than guessed at.
#
# Usage:
#   scripts/stacked-graph.sh [--base-ref <ref>] [--report-unordered]
#
#   --base-ref            default branch ref to measure against (default: origin/main)
#   --report-unordered    also emit UNORDERED lines for related-but-undirected pairs
#
# Output (stdout), one record per line, tab-separated:
#   EDGE       <dependent>  <base>     direct
#   EDGE       <dependent>  <base>     transitive
#   UNORDERED  <a>          <b>                   (only with --report-unordered)
#
#   UNORDERED means "related, and NO direction exists in either ordering" -- a
#   pair that also carries an EDGE is never reported unordered.
#
#   "direct" = <base> is <dependent>'s NEAREST base: no other base of
#   <dependent> itself has <base> as one of ITS bases. /land needs this to pick
#   the single base land-review diffs against -- handing it a transitive base
#   would make the diff carry the intermediate branch's work as if it were this
#   branch's. The full relation (direct + transitive) is what the bounce and
#   escalation paths need to ask "does deleting X strand a live descendant?".
#
# Exit codes: 0 = ran (zero or more records), 2 = machine fault. There is no
# exit 1: this is a query, not a verdict, so "no stacks" is a successful run.
set -u

# The source itself must fail CLOSED -- see gate-lib.sh's Usage
# section for the measurement and why the guard can't use the library it loads.
# The export this script was ported from carried a permissive variant (source
# if readable, else define a local gate_could_not_run), which both defeats that
# rule and is invisible to tests/test_gate_lib.py's consumer sweep, since that
# sweep anchors on this exact source line. The fail-closed form is the pinned one.
# shellcheck source=gate-lib.sh
if ! . "$(dirname "$0")/gate-lib.sh" --no-advisory; then
  echo "GATE COULD NOT RUN: scripts/gate-lib.sh is missing or unreadable" >&2
  echo "next to $0 -- this is a machine/checkout fault, not a branch verdict." >&2
  exit 2
fi

BASE_REF="origin/main"
REPORT_UNORDERED=0

# Printed by BOTH exit paths (empty edge set, and the normal one), so the
# directed-pair filter below cannot be applied on one path and forgotten on the
# other.
emit_unordered() {
  [ "$REPORT_UNORDERED" = "1" ] || return 0
  [ -n "$UNORD" ] || return 0
  printf '%s' "$UNORD" | while IFS="$(printf '\t')" read -r a b; do
    [ -n "$a" ] || continue
    # A pair is collected into UNORD by whichever ordering has x < y, but
    # direction is only ever found in ONE of the two orderings -- the one with
    # the BASE first. When the base's id sorts AFTER the dependent's, the
    # x < y ordering is the undirected one, so a genuinely stacked pair lands
    # in UNORD as well as in EDGES. Suppress those: UNORDERED means "related
    # and NO direction exists", not "no direction in this ordering".
    if printf '%s' "$EDGES" | grep -qxF "$a	$b" \
      || printf '%s' "$EDGES" | grep -qxF "$b	$a"; then
      continue
    fi
    printf 'UNORDERED\t%s\t%s\n' "$a" "$b"
  done
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --base-ref) shift; [ "$#" -gt 0 ] || gate_could_not_run "--base-ref needs a value"; BASE_REF="$1" ;;
    --report-unordered) REPORT_UNORDERED=1 ;;
    *) gate_could_not_run "unknown argument '$1'" \
         "usage: stacked-graph.sh [--base-ref <ref>] [--report-unordered]" ;;
  esac
  shift
done

git rev-parse --git-dir >/dev/null 2>&1 \
  || gate_could_not_run "not inside a git repository" "cwd: $(pwd)"
git rev-parse --verify --quiet "$BASE_REF" >/dev/null \
  || gate_could_not_run "base ref '$BASE_REF' does not resolve" \
       "Pass --base-ref, or fetch it first; a missing base ref cannot be read as 'no stacks'."

# Candidate branches. Ids are derived from the ref name, never from a ticket
# field: the refs are the live truth about what exists, and a producer that
# forgot to record its `builds_on` breadcrumb must not silently break this.
REFS="$(git for-each-ref --format='%(refname:short)' 'refs/remotes/origin/land/*' 2>/dev/null)" \
  || gate_could_not_run "git for-each-ref failed"
[ -n "$REFS" ] && IDS="$(printf '%s\n' "$REFS" | sed 's#^origin/land/##')" || IDS=""
[ -n "$IDS" ] || exit 0     # nothing to relate -- a successful, empty run

# --- pass 1: pairwise detection -------------------------------------------
# EDGES holds "<dependent><TAB><base>" lines; UNORD holds related-but-undirected.
EDGES=""
UNORD=""
for x in $IDS; do
  for y in $IDS; do
    [ "$x" = "$y" ] && continue
    # Ordered pair, so only test the x-is-base direction here; the y-is-base
    # case is covered when the loop reaches the mirrored pair.
    mbs="$(git merge-base --all "origin/land/$x" "origin/land/$y" 2>/dev/null)" || continue
    [ -n "$mbs" ] || continue
    off=""
    for mb in $mbs; do
      git merge-base --is-ancestor "$mb" "$BASE_REF" 2>/dev/null || off="$off $mb"
    done
    [ -n "$off" ] || continue     # every merge-base is on the default branch -> unrelated
    directed=0
    for mb in $off; do
      if git rev-list --first-parent "$BASE_REF..origin/land/$x" 2>/dev/null | grep -qx "$mb" \
         && ! git rev-list --first-parent "$BASE_REF..origin/land/$y" 2>/dev/null | grep -qx "$mb"; then
        EDGES="${EDGES}${y}	${x}
"
        directed=1
        break
      fi
    done
    # Related but undirected. Emitted once per unordered pair (x<y) so a
    # sibling pair is not reported twice.
    if [ "$directed" = "0" ] && [ "$REPORT_UNORDERED" = "1" ] && [ "$x" \< "$y" ]; then
      UNORD="${UNORD}${x}	${y}
"
    fi
  done
done

[ -n "$EDGES" ] || { emit_unordered; exit 0; }

# --- pass 2: transitive closure + nearest-base classification --------------
# Done in awk rather than bash: a fixpoint over an edge set is where hand-rolled
# shell goes wrong quietly, and this is the half /land's ordering depends on.
printf '%s' "$EDGES" | awk -F'\t' '
  NF == 2 { dep[$1 "\t" $2] = 1; deps[$1] = deps[$1] " " $2; nodes[$1]; nodes[$2] }
  END {
    # Transitive closure by repeated relaxation. The edge set is one pass of
    # /land, so it is tiny; clarity beats an asymptotically better algorithm.
    changed = 1
    while (changed) {
      changed = 0
      for (a in nodes) for (b in nodes) for (c in nodes) {
        if ((a "\t" b) in dep && (b "\t" c) in dep && !((a "\t" c) in dep)) {
          dep[a "\t" c] = 1; changed = 1
        }
      }
    }
    # DIRECT = base X of Y such that no OTHER base B of Y has X as one of ITS
    # bases. Computed from the relation, never from tips (which move).
    for (k in dep) {
      split(k, p, "\t"); y = p[1]; x = p[2]
      kind = "direct"
      for (o in nodes) {
        if (o != x && o != y && (y "\t" o) in dep && (o "\t" x) in dep) { kind = "transitive"; break }
      }
      print "EDGE\t" y "\t" x "\t" kind
    }
  }
' | sort

emit_unordered
exit 0
