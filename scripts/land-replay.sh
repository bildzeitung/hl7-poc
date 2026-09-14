#!/usr/bin/env bash
#
# /land's isolation replay: on a RED combined re-gate, replay the accepted set
# one branch at a time, re-gating after each, to attribute the red to a branch.
#
# RESUMABLE BY DESIGN. Per branch this runs four gate sessions (fix, tests,
# harness-tests-gate, lock_currency), plus four baseline runs before the loop
# -- `4 + 4N` gate invocations, though the harness gate answers in constant
# time unless the branch touched a harness path (see that script). A single Bash
# tool call is capped at 600s and the harness forbids backgrounding a gate, so a
# straight-through loop hits that ceiling at a modest queue size, mid-loop. The
# consequence is not merely a failed pass: nothing gets attributed, so nothing is
# bounced, the next pass rebuilds the same accepted set and reds again. It
# re-loops. This script therefore works to a DEADLINE, persists its position, and
# asks to be re-invoked (exit 3).
#
# The deadline default lives HERE, not at the call site, so a skill edit cannot
# silently raise it past the tool cap.
#
# IT CLASSIFIES; THE CALLER ACTS. On a culprit it backs the branch out and
# reports it -- it does NOT bounce. Bouncing needs a live-descendant check, a
# supersede, and a rebuild ticket carrying land-review's findings: judgment plus
# tracker writes, which stay with the agent. It also does not drop a culprit's
# dependents, because the caller may escalate instead of bouncing (a bounce that
# would strand a live descendant is an escalation), and only the caller knows
# which. On a plain merge CONFLICT it does drop dependents, because that outcome
# is mechanical and has no judgment in it.
#
# Usage:
#   scripts/land-replay.sh --accepted <f> --landed <f> --msg-dir <d>
#                          --conflicts-dir <d> --state <f>
#                          [--graph <f>] [--own-token <t>]
#                          [--base-ref <ref>] [--deadline-seconds <n>]
#
# Output (stdout), one record per line, tab-separated:
#   SURVIVOR <id>     merged and gated green; kept, and appended to <landed>
#   CONFLICT <id>     textual conflict against an earlier survivor; kicked back
#   HELD     <id>     dropped because its base conflicted; owes a HELD note
#   CULPRIT  <id>     turned the gate red; backed out. Caller bounces or escalates
#   MORE     <n>      deadline reached with <n> branches left; re-invoke to resume
#
# Exit codes:
#   0  finished -- every remaining branch merged green
#   1  a CULPRIT was found and backed out; caller must act, then re-invoke
#   2  machine fault, or a baseline red (not attributable to any branch): stop
#      the pass, land nothing further
#   3  deadline reached, progress persisted; re-invoke to continue
set -u

TOP="$(git rev-parse --show-toplevel 2>/dev/null)" || TOP=""
[ -n "$TOP" ] || { echo "GATE COULD NOT RUN: not inside a git repository" >&2; exit 2; }

# The canonical passive-export exclude list, via the sourced helper -- never a
# hardcoded pathspec: excluding all of `.beads/` wholesale would hide a real
# non-passive `.beads/` change (e.g. config.yaml) from every dirty-tree check
# below. Guarded source: a missing helper must fail CLOSED, not leave
# `load_beads_passive_exports` undefined for the first call site to trip over.
# shellcheck source=beads-passive-exports.sh
if ! . "$TOP/scripts/beads-passive-exports.sh"; then
  echo "GATE COULD NOT RUN: cannot source $TOP/scripts/beads-passive-exports.sh" >&2
  exit 2
fi
if ! load_beads_passive_exports "$TOP/scripts/beads-passive-exports.txt"; then
  echo "GATE COULD NOT RUN: the beads passive-export list is missing or malformed" >&2
  exit 2
fi

ACCEPTED=""; LANDED=""; MSG_DIR=""; CONFLICTS_DIR=""; STATE=""; GRAPH=""; OWN_TOKEN=""
BASE_REF="origin/main"
#: Default deadline. Deliberately well under the 600s tool cap: the check below
#: refuses to START a gate it predicts would cross this, and that prediction is
#: an estimate, so the margin absorbs a slower-than-average run.
DEADLINE=480
while [ "$#" -gt 0 ]; do
  case "$1" in
    --accepted)         shift; ACCEPTED="${1:-}" ;;
    --landed)           shift; LANDED="${1:-}" ;;
    --msg-dir)          shift; MSG_DIR="${1:-}" ;;
    --conflicts-dir)    shift; CONFLICTS_DIR="${1:-}" ;;
    --state)            shift; STATE="${1:-}" ;;
    --graph)            shift; GRAPH="${1:-}" ;;
    --own-token)        shift; OWN_TOKEN="${1:-}" ;;
    --base-ref)         shift; BASE_REF="${1:-}" ;;
    --deadline-seconds) shift; DEADLINE="${1:-}" ;;
    *) echo "GATE COULD NOT RUN: unknown argument '$1'" >&2; exit 2 ;;
  esac
  [ "$#" -gt 0 ] || { echo "GATE COULD NOT RUN: trailing option needs a value" >&2; exit 2; }
  shift
done
for req in ACCEPTED LANDED MSG_DIR CONFLICTS_DIR STATE; do
  eval "v=\$$req"
  [ -n "$v" ] || { echo "GATE COULD NOT RUN: --$(echo "$req" | tr 'A-Z_' 'a-z-') is required" >&2; exit 2; }
done
case "$DEADLINE" in ''|*[!0-9]*) echo "GATE COULD NOT RUN: --deadline-seconds must be an integer" >&2; exit 2 ;; esac
# Clamp rather than trust: a caller that passes 900 would reintroduce exactly the
# ceiling this script exists to stay under.
[ "$DEADLINE" -gt 570 ] && DEADLINE=570
[ "$DEADLINE" -lt 30 ] && DEADLINE=30

[ -f "$ACCEPTED" ] || { echo "GATE COULD NOT RUN: accepted file '$ACCEPTED' does not exist" >&2; exit 2; }
[ -d "$MSG_DIR" ]  || { echo "GATE COULD NOT RUN: msg dir '$MSG_DIR' does not exist" >&2; exit 2; }
mkdir -p "$CONFLICTS_DIR" || exit 2

START="$(date +%s)"

# --- state -----------------------------------------------------------------
# Plain key=value lines under $STATE_DIR. `done` records ids this run has
# finished with (survivor, conflicted, or backed-out culprit) so a resume never
# re-merges them; `est` carries the measured gate duration forward so the very
# first deadline check after a resume is informed rather than guessed.
state_get() { [ -f "$STATE" ] && sed -n "s/^$1=//p" "$STATE" | tail -1; }
state_set() {
  local k="$1" v="$2" tmp="$STATE.tmp"
  { [ -f "$STATE" ] && grep -v "^$k=" "$STATE"; printf '%s=%s\n' "$k" "$v"; } > "$tmp" 2>/dev/null
  mv "$tmp" "$STATE"
}
DONE_FILE="$STATE.done"
is_done()   { [ -f "$DONE_FILE" ] && grep -qxF "$1" "$DONE_FILE"; }
mark_done() { printf '%s\n' "$1" >> "$DONE_FILE"; }

EST="$(state_get est)"; [ -n "$EST" ] || EST=120

# --- gates -----------------------------------------------------------------
# LAND_GATE_CMD exists so a test can substitute a stub; unset, the real sessions
# run. Either way the status is read DIRECTLY -- never through a pipe, whose exit
# status is its last element's, so a killed gate would surface as exit 0.
GATE_CMD="${LAND_GATE_CMD:-}"
run_gate() {
  if [ -n "$GATE_CMD" ]; then "$GATE_CMD" "$1"; return $?; fi
  case "$1" in
    fix)     uv run --frozen --directory "$TOP" nox -t fix ;;
    tests)   uv run --frozen --directory "$TOP" nox -s tests ;;
    # The harness's own suite, only if the tree touched a harness path against
    # $BASE_REF. `--always` (the baseline) runs it regardless: a bare base ref
    # has no diff, and a diff-driven skip would baseline nothing.
    harness) "$TOP/scripts/harness-tests-gate.sh" --base-ref "$BASE_REF" "${@:2}" ;;
    lock)    uv run --frozen --directory "$TOP" nox -s lock_currency ;;
    *)       return 2 ;;
  esac
}

# --- first invocation: reset, baseline ------------------------------------
if [ "$(state_get baselined)" != "1" ]; then
  "$TOP/scripts/assert-main-checkout.sh" || exit 2
  git reset --hard "$BASE_REF" >/dev/null || {
    echo "GATE COULD NOT RUN: could not reset to '$BASE_REF'" >&2; exit 2; }
  : > "$LANDED"          # the reset discarded every merge the first pass recorded
  : > "$DONE_FILE"

  # BASELINE EVERY GATE BEFORE ATTRIBUTING ANYTHING. No gate here is a pure
  # function of the tree, so a red one may have nothing to do with the accepted
  # set -- and this loop DELETES what it blames. An ambient environment variable
  # in the landing shell, set nowhere in the repo, once reddened the suite on a
  # bare ref with nothing merged; trusting it would have closed an innocent
  # ticket and deleted a reviewed branch.
  # Baseline `fix` needs TWO checks, not one: the session runs a formatter, so
  # it can exit 0 while REFORMATTING the bare base tree. Either way -- red, or
  # green-but-dirty -- nothing in the accepted set is to blame, and every
  # per-branch dirty-tree comparison below would be polluted; land nothing.
  run_gate fix; rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "GATE COULD NOT RUN: the fix session is red on bare $BASE_REF, before any branch merged." >&2
    echo "Not attributable to anything in the accepted set; $BASE_REF itself needs a" >&2
    echo "human's fix. Land nothing." >&2
    exit 2
  fi
  # Tracked modifications only (`git diff`, not `git status`): an untracked
  # file beside the checkout (a caller's stub, scratch output) is not a
  # formatter reformat and must not read as one.
  base_reformat="$(git diff --name-only -- . "${BEADS_PASSIVE_EXPORTS_EXCLUDE_PATHSPECS[@]}")"
  if [ -n "$base_reformat" ]; then
    echo "GATE COULD NOT RUN: the fix session reformatted the bare base tree at $BASE_REF" >&2
    echo "(exit 0, but the tree is dirty). The formatter's output is not committed on the" >&2
    echo "base ref; commit it there directly. Land nothing." >&2
    exit 2
  fi

  b0="$(date +%s)"
  run_gate tests; rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "GATE COULD NOT RUN: the suite is red on bare $BASE_REF, before any branch merged." >&2
    echo "Not attributable to anything in the accepted set. Land nothing; surface as a human" >&2
    echo "decision, and check the landing shell's own environment first." >&2
    exit 2
  fi
  # One real measurement beats a guess for every deadline check that follows.
  EST=$(( $(date +%s) - b0 )); [ "$EST" -lt 5 ] && EST=5
  state_set est "$EST"

  run_gate harness --always; rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "GATE COULD NOT RUN: the harness's own suite (harness-tests-gate --always) is red on bare" >&2
    echo "$BASE_REF (exit $rc), before any branch merged. Not attributable to the accepted set." >&2
    echo "Land nothing." >&2
    exit 2
  fi

  run_gate lock; rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "GATE COULD NOT RUN: lock_currency is red on bare $BASE_REF (exit $rc), before any" >&2
    echo "branch merged -- not attributable to the accepted set. Land nothing." >&2
    exit 2
  fi
  state_set baselined 1
fi

# --- the replay loop -------------------------------------------------------
remaining_count() {
  local n=0 id
  while read -r id; do
    [ -n "$id" ] || continue
    is_done "$id" || n=$((n + 1))
  done < "$ACCEPTED"
  printf '%s' "$n"
}

# Word-split on purpose, not `while read`: the body runs the gates, which must
# own stdin, and ids are single whitespace-free tokens.
# shellcheck disable=SC2013
for id in $(cat "$ACCEPTED"); do
  [ -n "$id" ] || continue
  is_done "$id" && continue
  # Membership is re-checked every iteration: an earlier conflict may have taken
  # this branch out of the set, and merging it anyway lands a departed base's
  # content under this ticket's name. grep's own 1-vs-else partition: 1 = "not
  # in the set", anything ABOVE 1 (the file vanished, an I/O error) is a machine
  # fault -- reading it as "already dropped" would silently skip every remaining
  # id while leaving them in the accepted file.
  grep -qxF "$id" "$ACCEPTED"
  grc=$?
  case "$grc" in
    0) ;;
    1) continue ;;
    *)
      echo "GATE COULD NOT RUN: grep failed (exit $grc) re-checking '$id' in '$ACCEPTED'" >&2
      echo "Reading this as 'already dropped' would silently skip the rest of the replay." >&2
      exit 2
      ;;
  esac

  # Deadline: never START work whose gates are predicted to cross it. Four gate
  # sessions per branch, but the harness gate is a constant-time skip unless the
  # branch touched a harness path, and EST already measures fix+tests(+harness),
  # so 3 * EST stays a safe over-estimate.
  now="$(date +%s)"
  if [ $(( now - START + (3 * EST) )) -gt "$DEADLINE" ]; then
    left="$(remaining_count)"
    printf 'MORE\t%s\n' "$left"
    exit 3
  fi

  if CONFLICTS=$("$TOP/scripts/land-merge-one.sh" "$id" "$MSG_DIR" "$OWN_TOKEN"); then
    rc=0
  else
    rc=$?
  fi
  case "$rc" in
    0) : ;;
    1)
      # Conflicts with an earlier survivor. Its content was never judged bad, so
      # this is a kick-back, not a bounce -- and it leaves the merge set, so it
      # takes its dependents with it. Mechanical, no judgment: done here.
      printf '%s\n' "$CONFLICTS" > "$CONFLICTS_DIR/$id"
      printf 'CONFLICT\t%s\n' "$id"
      mark_done "$id"
      dropargs="$id --accepted $ACCEPTED"
      [ -n "$GRAPH" ] && dropargs="$dropargs --graph $GRAPH"
      # Capture, don't pipe: a `drop | awk` pipeline reports awk's always-0
      # status, so a drop-side machine fault would be swallowed and a
      # conflicted branch silently left in the accepted set.
      # shellcheck disable=SC2086
      if ! DROP_OUT=$("$TOP/scripts/drop-from-accepted.sh" $dropargs); then
        echo "GATE COULD NOT RUN: drop-from-accepted.sh failed for '$id'" >&2; exit 2
      fi
      while IFS=$'\t' read -r verb held_id; do
        [ "$verb" = "HELD" ] || continue
        printf 'HELD\t%s\n' "$held_id"
      done <<< "$DROP_OUT"
      continue
      ;;
    *)
      printf 'FAULT\t%s\n' "$id"
      echo "GATE COULD NOT RUN: land-merge-one.sh machine fault on '$id' -- never a branch verdict" >&2
      exit 2
      ;;
  esac

  g0="$(date +%s)"
  run_gate fix;     fix_rc=$?
  run_gate tests;   tests_rc=$?
  run_gate harness; harness_rc=$?   # constant-time skip unless this branch touched the harness
  EST=$(( $(date +%s) - g0 )); [ "$EST" -lt 5 ] && EST=5
  state_set est "$EST"

  # Exit 1 is the ONLY content verdict a gate has. Anything else nonzero --
  # 127 (nox vanished from PATH mid-run), 126, 128+n (killed by a signal) --
  # is the MACHINE, not this branch: stop the whole replay rather than back
  # out and bounce an innocent branch on a bootstrap gap. The merge is left in
  # place because its fate is unknown, not judged.
  for rc in "$fix_rc" "$tests_rc" "$harness_rc"; do
    case "$rc" in
      0|1) ;;
      *)
        echo "GATE COULD NOT RUN: a gate exited $rc on '$id' -- exit 1 is the only content" >&2
        echo "verdict; a 127/126/signal here is a machine fault, never a CULPRIT. Stopping." >&2
        exit 2
        ;;
    esac
  done
  if [ "$fix_rc" -eq 1 ] || [ "$tests_rc" -eq 1 ] || [ "$harness_rc" -eq 1 ]; then
    git reset --hard HEAD~1 >/dev/null || exit 2   # back the culprit out
    mark_done "$id"
    printf 'CULPRIT\t%s\n' "$id"
    exit 1
  fi

  # A green fix session may still have REFORMATTED this branch's files. Fold
  # that into the merge commit now: left loose, it dirties the tree for the
  # NEXT iteration's `git merge`, which most likely machine-faults against it
  # (the CULPRIT path never surfaces this -- `git reset --hard HEAD~1` cleans
  # it along with everything else).
  mapfile -t reformat_paths < <(git diff --name-only -- . "${BEADS_PASSIVE_EXPORTS_EXCLUDE_PATHSPECS[@]}")
  if [ "${#reformat_paths[@]}" -gt 0 ]; then
    git add -- "${reformat_paths[@]}" || {
      echo "GATE COULD NOT RUN: could not stage the fix session's reformat of '$id'" >&2; exit 2; }
    git commit --quiet --amend --no-edit || {
      echo "GATE COULD NOT RUN: could not amend '$id''s merge commit with the reformat" >&2; exit 2; }
  fi

  run_gate lock; rc=$?
  case "$rc" in
    0)
      printf '%s\n' "$id" >> "$LANDED"
      printf 'SURVIVOR\t%s\n' "$id"
      mark_done "$id"
      ;;
    1)
      git reset --hard HEAD~1 >/dev/null || exit 2
      mark_done "$id"
      printf 'CULPRIT\t%s\n' "$id"
      exit 1
      ;;
    *)
      # A machine fault mid-loop (its documented 2, and equally a 127/126/
      # signal) is NOT this branch's verdict. Stop; never bounce.
      echo "GATE COULD NOT RUN: lock_currency machine fault (exit $rc) on '$id' -- stopping, landing nothing further" >&2
      exit 2
      ;;
  esac
done

exit 0
