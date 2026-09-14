#!/usr/bin/env bash
#
# Merge a single accepted `land/<id>` branch onto the current checkout with its
# pre-computed commit message, retrying once past a re-staged passive beads
# export -- see `scripts/beads-passive-exports.txt` for the canonical list of
# such exports. Extracted
# `/land`'s Section 3 merge loop
# used to define this as an inline bash FUNCTION (`merge_one`) and read a
# bash ASSOCIATIVE ARRAY (`MSG`) populated by a separate, earlier fenced code
# block in .claude/skills/land/SKILL.md. Those are two different Bash tool
# invocations -- the harness does not carry shell state (variables, arrays, or
# function definitions) between them -- so by the time the merge loop ran,
# `MSG` was empty and `merge_one` may not even exist, and the failure was
# SILENT (an empty-message merge, or an unrelated-looking error with no
# output). OBSERVED landing the 2026-07-26 an earlier fix/an earlier fix/an earlier fix pass.
# A script on disk has no such problem: it is available identically to every
# Bash invocation that calls it, with no bash state to redeclare.
#
# Usage: scripts/land-merge-one.sh <id> <land-msg-dir> [own-token]
#
#   <id>            -- the bd ticket id whose `origin/land/<id>` branch is
#                      about to be merged into the current checkout (main).
#   <land-msg-dir>  -- a directory containing one file per id, named exactly
#                      `<land-msg-dir>/<id>`, whose full content (verbatim) is
#                      the merge commit message. Written once, ahead of any
#                      merge, by /land's Section 3a precompute step -- this
#                      script never calls `bd` itself, so the "one bd-show
#                      pass instead of N subprocess calls per merge" property
#                      an earlier fix established stays intact.
#   [own-token]     -- OPTIONAL at THIS script's own argument level (kept
#                      optional so a direct invocation without a token still
#                      runs): the current /land pass's own remembered
#                      `scripts/land-lock.sh acquire` token,
#                      threaded straight through to this script's own
#                      heartbeat call below so it can refuse to overwrite a
#                      lock this pass no longer owns. land-lock.sh's OWN
#                      [own-token] argument is REQUIRED as of an earlier fix, so
#                      omitting it HERE does not reach land-lock.sh as an
#                      empty argument: this script substitutes the explicit
#                      `--land-lock-blind` sentinel below, keeping the
#                      pre-an earlier fix blind heartbeat this argument's contract
#                      has always promised a token-less caller. That
#                      degradation is NOT silent -- the loud, non-fatal
#                      an earlier fix warning below fires at the point the
#                      argument is missing -- but the loudness is THIS
#                      script's, not land-lock.sh's exit-2 enforcement, which
#                      this path deliberately never reaches. See
#                      scripts/land-lock.sh's own header for what supplying a
#                      real token does and does not change.
#
# Exit codes -- same 0/1/2 convention as scripts/merge-precheck.sh and
# scripts/validate-mermaid.sh (an earlier fix's rule: exit 2 is a MACHINE/setup
# fault, never a branch's content, and must never be read as a conflict or a
# reason to bounce):
#
#   0 -- merged cleanly (on the first attempt, or after the jsonl-restore
#        retry). Nothing on stdout.
#   1 -- a REAL textual conflict. The conflicting path(s) (one per line, no
#        other chatter) are printed to STDOUT for the caller to capture as
#        $CONFLICTS -- e.g. `CONFLICTS=$(scripts/land-merge-one.sh "$id"
#        "$MSG_DIR")`. The merge is already aborted (working tree left clean)
#        before this script returns.
#   2 -- could not even attempt the merge: bad usage, cwd is not harness's main
#        checkout -- or that guard could not run at all (an earlier fix, see
#        below; the two are distinguished in the diagnostic, never conflated),
#        a missing/empty message file for
#        <id> (Section 3a's precompute did not run, or did not cover this
#        id), or an unexpected git failure that is neither the retryable
#        jsonl trap nor a real conflict (an empty `git ls-files -u` yet the
#        merge still failed). Stderr names the cause. This is the "LOUD, never
#        a silent empty-message merge" acceptance criterion -- refusing here
#        is exactly what closes an earlier fix's failure mode.
#
# MAIN-CHECKOUT IDENTITY: every git call below --
# `git merge --no-ff`, `git restore --staged --worktree`, `git merge --abort`
# -- is cwd-resolved, with no `-C`/`--git-dir` of its own pinning it to a
# specific checkout. This script therefore asserts its own main-checkout
# identity (`scripts/assert-main-checkout.sh`) as its first real action,
# below, folding any failure into its exit-2 contract -- rather than relying
# on each call site to fence-guard it, which is a discipline every future
# caller would have had to remember independently. NO CALLER NEEDS TO FENCE
# THIS SCRIPT; a caller with cwd-resolved mutations of its OWN still needs
# its own guard. Do NOT list the call sites here -- the same roster went
# stale within one ticket of being written in assert-main-checkout.sh's
# header, which is why neither script keeps one now. Full decision and
# reasoning: docs/agents-workflow.md's main-checkout section.
#
# Never touches bd, never pushes, never runs a gate -- purely the merge step,
# plus one side effect: it heartbeats the single-lander lock via
# `scripts/land-lock.sh heartbeat`, best-effort and non-fatal, so that a
# lock's staleness check measures idle time rather than the whole pass's
# duration -- see that script's own header for the full mechanism.
# The caller (the /land skill) decides what a 1 or a 2 means for the pass.

set -uo pipefail   # deliberately NOT -e: every branch below inspects an exit
                   # code by hand; -e would short-circuit exactly the paths
                   # this script exists to distinguish (retryable jsonl trap
                   # vs. real conflict vs. machine fault).

# The ONE owner of the gate-could-not-run contract: the banner
# every exit-2 diagnostic in this repo opens with, this script's own advisory
# trailer below, and the exit 2 itself. Sourced from scripts/gate-lib.sh
# so it cannot drift from the other gate scripts.
#
# The source itself must fail CLOSED -- see gate-lib.sh's Usage
# section for the measurement and why the guard can't use the library it loads.
# This gate's own advisory trailer, bound at source time (an earlier fix; see
# gate-lib.sh's GATE_ADVISORY contract for the mechanism and why it is not a
# separate assignment). tests/test_land_merge_one.py::_assert_machine_fault_contract
# pins the advisory TEXT below on an exit-2 path, which no static sweep can
# see -- route any new exit-2 test through it.
# shellcheck source=gate-lib.sh
if ! . "$(dirname "$0")/gate-lib.sh" \
     "This is a machine fault a human must fix, not a branch conflict --" \
     "do not kick this branch back needs-rebase in place of diagnosing it."; then
  echo "GATE COULD NOT RUN: scripts/gate-lib.sh is missing or unreadable" >&2
  echo "next to $0 -- this is a machine/checkout fault, not a branch verdict." >&2
  exit 2
fi

# Main-checkout identity guard -- FIRST, ahead of even the
# arg-count check: it is a cwd PRECONDITION (see the file header), not caller
# input, and holds regardless of whether the arguments are well-formed.
#
# The guard MISSING is a different fault from the guard saying no, and must
# not be reported as one (the same distinction gate-lib.sh's source guard
# above draws, and recycled-worktree-guard.sh's bootstrap-gap rule):
# without this check a non-executable script exits 127 and would be narrated
# below as a location verdict.
if [ ! -x "$(dirname "$0")/assert-main-checkout.sh" ]; then
  gate_could_not_run \
    "scripts/assert-main-checkout.sh is missing or not executable next to $0." \
    "This is a bootstrap/checkout fault -- the guard could not run at all, which" \
    "is NOT a verdict that cwd is the wrong checkout, and never a branch conflict."
fi

# Both of the guard's failure modes (exit 1: wrong location; exit 2: machine
# fault) fold into THIS script's exit 2, never its exit 1 (the real-conflict
# code): neither is a branch's content. The guard already printed its own
# diagnostic to stderr.
if ! "$(dirname "$0")/assert-main-checkout.sh"; then
  gate_could_not_run \
    "not running in harness's main checkout (see the diagnostic above)." \
    "scripts/assert-main-checkout.sh refused -- every git call in this" \
    "script is cwd-resolved with no -C of its own, so this is a" \
    "machine/dispatch fault, never a branch conflict."
fi

# Arg-count check next -- first among the CALLER-INPUT checks -- and it must
# exit 2, never `${1:?...}`, whose exit 1 is exactly the CONFLICT code (same
# reasoning as merge-precheck.sh's header). 2 or 3 args: [own-token]
# is optional.
if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  gate_could_not_run \
    "usage: land-merge-one.sh <id> <land-msg-dir> [own-token]" \
    "Got $# argument(s), expected 2 or 3. This is a caller bug, not a" \
    "branch conflict, so it exits 2 (never 1) to stay out of the conflict path."
fi
id="$1"
msg_dir="$2"
own_token="${3:-}"
# No own-token (or an empty one) -- warn loudly, then hand land-lock.sh the
# explicit `--land-lock-blind` sentinel instead. Both halves are about the same
# case, so they live in the same block:
#   - the WARNING fires here, at the point of the missing argument,
#     with nothing else to grep through. Every SKILL.md caller warns at its own
#     read-back site too, so on that path this doubles up; it earns its place by
#     covering a DIRECT invocation, which has no caller diagnostic at all.
#     STDERR, never stdout -- stdout is the caller's $CONFLICTS channel (see the
#     heartbeat comment below).
#   - the SENTINEL is required because land-lock.sh's own [own-token] argument
#     is REQUIRED as of an empty string is refused outright (exit 2),
#     which would turn a caller who simply omitted THIS script's optional third
#     argument into a heartbeat that stops re-stamping the lock at all, rather
#     than the best-effort blind re-stamp this script's own contract has always
#     promised a token-less caller. `land-lock-blind-ok:` -- this is the second
#     of land-lock.sh's two sanctioned sentinel uses; see its OWNERSHIP CHECK
#     header for both.
if [ -z "$own_token" ]; then
  echo "land-merge-one: WARNING -- no own-token supplied;" \
    "land-lock ownership check is DISABLED for this call's heartbeat" >&2
  own_token="--land-lock-blind"
fi

# Heartbeat the single-lander lock. This script runs once per
# accepted branch in /land's Section 3 first merge loop AND its
# isolation-replay copy, so a single call site here re-stamps the lock every
# branch-iteration of BOTH loops without a second call site in SKILL.md --
# the token's age then reflects the gap since the last branch merged, not
# since Section 0's original `acquire`. `$(dirname "$0")` resolves to the
# real scripts/ dir regardless of how this script itself was invoked
# (relative from the repo root in a normal /land pass, or an absolute test
# path) -- same idiom scripts/merge-precheck.sh uses for gate-lib.sh.
# Best-effort and non-fatal: a heartbeat write failure must never abort an
# otherwise-clean merge (see scripts/land-lock.sh's own heartbeat comment for
# why its exit 1 is not treated as fatal by its callers).
#
# STDOUT is redirected because this script's own stdout is the caller's
# $CONFLICTS channel and must stay clean; STDERR deliberately is NOT. That
# script's exit-1 contract is "log and continue -- a human should look if this
# repeats every tick", and swallowing its diagnostic here would leave the
# heartbeat silently dead at the one call site that fires on every merge --
# the same "must be observable, never silent" standard an earlier fix set for the
# lock itself. Extra stderr is already normal on this path (the merge's own
# error text goes there below).
#
# `$own_token` is non-empty by this point -- either the
# caller's real token, or the blind sentinel substituted above -- which
# land-lock.sh now requires. What supplying a real token does and does not
# change is documented once, in the Usage block above -- not restated here.
"$(dirname "$0")/land-lock.sh" heartbeat "$own_token" >/dev/null || true

msg_file="$msg_dir/$id"
if [ ! -s "$msg_file" ]; then
  gate_could_not_run \
    "no precomputed merge message for '$id' at '$msg_file'." \
    "/land's Section 3a precompute step must run, and must write a message" \
    "for every id in this pass's accepted set, before any merge is attempted" \
    "-- refusing to merge '$id' with a fabricated or empty commit message."
fi
msg="$(<"$msg_file")"

err="$(git merge --no-ff "origin/land/$id" -m "$msg" 2>&1)" && exit 0

# A native bash match, NOT `printf ... | grep -q`: under `pipefail` above,
# `grep -q` exits the moment it matches, which can SIGPIPE the writer and make
# the whole pipeline report 141 -- silently skipping the retry this branch
# exists to perform. No pipeline, no subprocesses, no hazard.
if [[ "$err" == *"would be overwritten by merge"* ]] \
   && [ -z "$(git ls-files -u)" ]; then
  # Passive-export trap, not a conflict: a passive beads export got (re-)staged by
  # something other than this merge. Restore every entry on the canonical
  # list rather than one
  # hardcoded relpath, then retry the SAME merge once.
  #
  # ONE `git restore` PER ENTRY, deliberately -- NOT one call listing them
  # all. `git restore` is atomic over its pathspecs: if any single one is
  # unknown to git in this repo state (an export that exists on the list but
  # has never been committed here), it errors and restores NOTHING, silently
  # via the `2>/dev/null || true` below. Per entry, an unknown path is a
  # harmless no-op and the others still restore. VERIFIED by experiment.
  #
  # FAIL LOUD if the list is unreadable or empty, the same way
  # scripts/worktree-gc-classify.sh's gate does: restoring nothing here would
  # otherwise surface as the generic "merge failed, no unmerged paths" exit 2
  # below, naming the wrong cause.
  exports_list="$(dirname "$0")/beads-passive-exports.txt"
  [ -r "$exports_list" ] || gate_could_not_run \
    "cannot read the canonical passive-export list at '$exports_list'." \
    "The merge retry cannot know which paths to unstage without it."
  mapfile -t exports < "$exports_list"
  [ "${#exports[@]}" -gt 0 ] || gate_could_not_run \
    "the canonical passive-export list at '$exports_list' has no entries."
  for export_path in "${exports[@]}"; do
    [ -n "$export_path" ] || continue
    git restore --staged --worktree "$export_path" 2>/dev/null || true
  done
  err="$(git merge --no-ff "origin/land/$id" -m "$msg" 2>&1)" && exit 0
fi

printf '%s\n' "$err" >&2
unmerged="$(git ls-files -u)"
if [ -n "$unmerged" ]; then
  # A REAL textual conflict -- name the paths for the needs-rebase kick-back,
  # then abort back to a clean tree before returning.
  printf '%s\n' "$unmerged" | cut -f2- | sort -u
  git merge --abort
  exit 1
fi

# Neither the jsonl trap nor a real conflict (empty ls-files -u, merge still
# failed): an unexpected git failure. This is a machine fault, not a branch
# verdict -- exit 2, loud, never silent.
gate_could_not_run \
  "'git merge --no-ff origin/land/$id' failed, but git ls-files -u is empty." \
  "This is neither the retryable jsonl trap nor a real textual conflict --" \
  "see git's own error above for the cause."
