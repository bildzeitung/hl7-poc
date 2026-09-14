#!/usr/bin/env bash
#
# Merge /land's accepted set, in order, recording what happened. Runs NO gates
# and makes NO tracker writes: it classifies and reports, the caller acts.
#
# WHY A SCRIPT. This was a fenced loop in .claude/skills/land/SKILL.md, and a
# near-duplicate of it appeared again in the isolation-replay path with a comment
# asking a human to "keep the two loops the same shape" -- an unenforced sync
# invariant between two copies of destructive code. It also carried two traps the
# agent had to reproduce correctly by hand:
#
#   * `if CMD; then rc=0; else rc=$?; fi`, NOT `if ! CMD; then rc=$?`. In the
#     negated form `$?` is the NEGATION's status, which inside that arm is always
#     0 -- so a machine-fault 2 would read as a clean merge and the pass would
#     carry on as though the branch had landed.
#   * On a conflict the branch AND its dependents must leave the accepted set,
#     written back to the FILE (the replay loop re-reads it). That was a shell
#     recipe inside a comment; it is scripts/drop-from-accepted.sh now.
#
# Usage:
#   scripts/land-merge-batch.sh --accepted <f> --landed <f> --msg-dir <d>
#                               --conflicts-dir <d> [--graph <f>] [--own-token <t>]
#
# Output (stdout), one record per line, tab-separated, in merge order:
#   LANDED    <id>            merged cleanly; appended to the landed file
#   CONFLICT  <id>            real textual conflict; conflicting paths written to
#                             <conflicts-dir>/<id>; the caller owes it a
#                             needs-rebase kick-back
#   HELD      <id>            dropped because its base left the merge set; the
#                             caller owes it a HELD note. Not conflicted, not
#                             rejected -- no foundation this pass
#   SKIPPED   <id>            already removed from the accepted set before its
#                             turn came (its base conflicted earlier in this run)
#   FAULT     <id>            land-merge-one.sh reported a machine fault
#
# Exit codes: 0 = every branch merged cleanly; 1 = ran fine, but at least one
# branch needs caller follow-up (conflict/held); 2 = machine fault, the pass
# stops. A conflict is a real finding, hence 1 and not 0; a fault is never a
# branch verdict, hence 2 and never a bounce.
set -u

TOP="$(git rev-parse --show-toplevel 2>/dev/null)" || TOP=""
[ -n "$TOP" ] || { echo "GATE COULD NOT RUN: not inside a git repository" >&2; exit 2; }

ACCEPTED=""; LANDED=""; MSG_DIR=""; CONFLICTS_DIR=""; GRAPH=""; OWN_TOKEN=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --accepted)      shift; ACCEPTED="${1:-}" ;;
    --landed)        shift; LANDED="${1:-}" ;;
    --msg-dir)       shift; MSG_DIR="${1:-}" ;;
    --conflicts-dir) shift; CONFLICTS_DIR="${1:-}" ;;
    --graph)         shift; GRAPH="${1:-}" ;;
    --own-token)     shift; OWN_TOKEN="${1:-}" ;;
    *) echo "GATE COULD NOT RUN: unknown argument '$1'" >&2; exit 2 ;;
  esac
  [ "$#" -gt 0 ] || { echo "GATE COULD NOT RUN: trailing option needs a value" >&2; exit 2; }
  shift
done
for req in ACCEPTED LANDED MSG_DIR CONFLICTS_DIR; do
  eval "v=\$$req"
  [ -n "$v" ] || { echo "GATE COULD NOT RUN: --$(echo "$req" | tr 'A-Z_' 'a-z-') is required" >&2; exit 2; }
done
# MISSING is fatal; EMPTY is a legitimate outcome (every branch already left the
# set before this ran). Conflating them is what makes a silent no-op look like a
# clean pass.
[ -f "$ACCEPTED" ] || { echo "GATE COULD NOT RUN: accepted file '$ACCEPTED' does not exist" >&2; exit 2; }
[ -d "$MSG_DIR" ]  || { echo "GATE COULD NOT RUN: msg dir '$MSG_DIR' does not exist" >&2; exit 2; }
mkdir -p "$CONFLICTS_DIR" || exit 2
: >> "$LANDED" || exit 2

# Snapshot the ORDER once. The accepted file is rewritten underneath us every
# time a conflict drops a base's dependents, so iterating the file directly would
# skip entries; iterating the snapshot and re-checking membership per id is what
# keeps "base before dependent" ordering AND honours the reduction.
ORDER="$(cat "$ACCEPTED")"
[ -n "$ORDER" ] || exit 0     # empty accepted set: nothing to merge, cleanly

follow_up=0
for id in $ORDER; do
  [ -n "$id" ] || continue
  # Still in the set? A base that conflicted earlier this run has already taken
  # this branch out, and merging it anyway is the exact hole 3a exists to close.
  # grep's own 1-vs-else partition: 1 = "not in the set", anything ABOVE 1 (the
  # file vanished, an I/O error) is a machine fault. Reading a grep-2 as
  # "already dropped" would silently skip every REMAINING id while leaving them
  # in the accepted file -- neither merged nor reported.
  grep -qxF "$id" "$ACCEPTED"
  grc=$?
  case "$grc" in
    0) ;;
    1)
      printf 'SKIPPED\t%s\n' "$id"
      follow_up=1
      continue
      ;;
    *)
      echo "GATE COULD NOT RUN: grep failed (exit $grc) re-checking '$id' in '$ACCEPTED'" >&2
      echo "Reading this as 'already dropped' would silently skip the rest of the batch." >&2
      exit 2
      ;;
  esac

  # See the header: the non-negated form is load-bearing. A command substitution
  # inside an `if` condition is also exempt from `set -e`, unlike a bare
  # assignment, which would abort before rc could be read.
  if CONFLICTS=$("$TOP/scripts/land-merge-one.sh" "$id" "$MSG_DIR" "$OWN_TOKEN"); then
    rc=0
  else
    rc=$?
  fi

  case "$rc" in
    0)
      # A failed append is a fault, not a shrug: Section 4 reads this file back
      # to decide which tickets to close, so a silently dropped line leaves a
      # merged branch's ticket open with nothing to say so.
      printf '%s\n' "$id" >> "$LANDED" || {
        echo "GATE COULD NOT RUN: could not append '$id' to landed file '$LANDED'" >&2
        echo "This id IS merged onto the current checkout; the record of it is not." >&2
        exit 2
      }
      printf 'LANDED\t%s\n' "$id"
      ;;
    1)
      printf '%s\n' "$CONFLICTS" > "$CONFLICTS_DIR/$id"
      printf 'CONFLICT\t%s\n' "$id"
      follow_up=1
      # The branch just LEFT the merge set -- take its dependents with it, in
      # the FILE, before the loop reaches them.
      dropargs="$id --accepted $ACCEPTED"
      [ -n "$GRAPH" ] && dropargs="$dropargs --graph $GRAPH"
      # Capture, don't pipe: a `drop | awk` pipeline reports awk's always-0
      # status, so a drop-side machine fault (missing --accepted, an unreadable
      # graph) would be swallowed and a conflicted branch silently left in the
      # accepted set.
      # shellcheck disable=SC2086
      if ! DROP_OUT=$("$TOP/scripts/drop-from-accepted.sh" $dropargs); then
        echo "GATE COULD NOT RUN: drop-from-accepted.sh failed for '$id'" >&2
        echo "The accepted set may be only partly reduced -- do not continue merging." >&2
        exit 2
      fi
      while IFS=$'\t' read -r verb held_id; do
        [ "$verb" = "HELD" ] || continue
        printf 'HELD\t%s\n' "$held_id"
      done <<< "$DROP_OUT"
      ;;
    *)
      # Never a branch verdict: do not bounce, do not kick back, stop the pass.
      printf 'FAULT\t%s\n' "$id"
      echo "GATE COULD NOT RUN: land-merge-one.sh reported a machine fault on '$id'" >&2
      exit 2
      ;;
  esac
done

exit "$follow_up"
