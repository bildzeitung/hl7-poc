#!/usr/bin/env bash
#
# Run the harness's OWN gate tests (`uv run --frozen nox -s harness_tests`,
# i.e. tests/harness/) only when the change under gate touched a harness path.
#
# WHY A CONDITIONAL GATE. tests/harness/ tests the harness -- the guard
# scripts, the skill markdown, the hook wiring -- never the project. Its
# verdict can only change when one of those files changes. Running it inside
# every `nox -s tests` charged every producer gate, every /land re-gate and
# every replay session a full harness run to re-certify files the branch never
# touched. This script is the fence that keeps the pins live for the branches
# that DO edit the harness while costing the rest nothing.
#
# Usage: scripts/harness-tests-gate.sh --base-ref <ref> [--always]
#
#   --base-ref <ref>  what the change is measured against: the merge-base of
#                     <ref> and HEAD. A producer in a worktree passes
#                     origin/main; /land's re-gate passes origin/main (the ref
#                     Section 1 fetched, not yet pushed over); the replay
#                     passes its own base ref.
#   --always          run the suite regardless of the diff. The replay's
#                     baseline uses this: every gate is baselined on the bare
#                     base ref before anything is attributed, and a diff-less
#                     skip would baseline nothing.
#
# "Touched" = any path under one of HARNESS_PATHS below differs between the
# merge-base and the WORKING TREE (committed or not), or is untracked and not
# ignored there. The working tree, not HEAD, because the tree that gates green
# must be the tree that gets committed (docs/architecture.md).
#
# Exit 0 -> skipped (nothing under HARNESS_PATHS changed), or ran and passed.
#           Also 0, with a note, when tests/harness/ holds no gate tests at
#           all: a project may deliberately drop the suite (harness-doctor.sh
#           warns about it) and must not red every gate for having done so.
# Exit 1 -> ran and nox reported a failed session: a real finding.
# Exit 2 -> COULD NOT RUN: not a git repo, <ref> does not resolve, `uv` is
#           not on PATH, or nox exited with something other than 0/1 (127,
#           a signal). Never a branch verdict -- surface it, don't bounce.
set -u

HARNESS_PATHS=(scripts/ .claude/ tests/harness/ noxfile.py pyproject.toml uv.lock)

BASE_REF=""
ALWAYS=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --base-ref)
      [ "$#" -gt 1 ] || { echo "GATE COULD NOT RUN: --base-ref needs a value" >&2; exit 2; }
      BASE_REF="$2"; shift 2 ;;
    --base-ref=*) BASE_REF="${1#--base-ref=}"; shift ;;
    --always)     ALWAYS=1; shift ;;
    *) echo "GATE COULD NOT RUN: unknown argument '$1' (usage: --base-ref <ref> [--always])" >&2; exit 2 ;;
  esac
done
[ -n "$BASE_REF" ] || { echo "GATE COULD NOT RUN: --base-ref <ref> is required" >&2; exit 2; }

TOP="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "GATE COULD NOT RUN: not inside a git repository" >&2; exit 2; }
cd "$TOP" || { echo "GATE COULD NOT RUN: cannot cd to '$TOP'" >&2; exit 2; }

set -- tests/harness/test_*.py
if [ ! -e "$1" ]; then
  echo "harness-tests-gate: tests/harness/ has no gate tests -- nothing to run" \
    "(harness-doctor.sh warns about this; drop this call site if that is deliberate)"
  exit 0
fi

if [ "$ALWAYS" = 1 ]; then
  echo "harness-tests-gate: --always -- running nox -s harness_tests"
else
  if ! merge_base="$(git merge-base "$BASE_REF" HEAD 2>/dev/null)" || [ -z "$merge_base" ]; then
    echo "GATE COULD NOT RUN: '$BASE_REF' does not resolve to a commit with a merge-base" >&2
    echo "against HEAD. Fetch it, or pass the ref the branch was cut from." >&2
    exit 2
  fi
  # Working tree vs merge-base (committed AND uncommitted tracked changes),
  # plus untracked-but-not-ignored files: an agent gates before it commits.
  changed="$(
    { git diff --name-only "$merge_base" -- "${HARNESS_PATHS[@]}" \
        && git ls-files --others --exclude-standard -- "${HARNESS_PATHS[@]}"; } 2>/dev/null
  )" || {
    echo "GATE COULD NOT RUN: git could not diff the tree against $merge_base" >&2; exit 2; }
  if [ -z "$changed" ]; then
    echo "harness-tests-gate: no harness path changed against $BASE_REF -- tests/harness not run"
    exit 0
  fi
  n="$(printf '%s\n' "$changed" | grep -c .)"
  echo "harness-tests-gate: $n harness path(s) changed against $BASE_REF -- running nox -s harness_tests"
  printf '%s\n' "$changed" | sed 's/^/  /'
fi

command -v uv >/dev/null 2>&1 || {
  echo "GATE COULD NOT RUN: 'uv' is not on PATH -- install uv and re-run" >&2; exit 2; }

uv run --frozen nox -s harness_tests
rc=$?
case "$rc" in
  0|1) exit "$rc" ;;
  *)
    echo "GATE COULD NOT RUN: nox -s harness_tests exited $rc -- exit 1 is the only content" >&2
    echo "verdict; anything else here is the machine (a missing tool, a signal), not the branch." >&2
    exit 2 ;;
esac
