#!/bin/bash
#
# Controlled dependency update for harness's uv.lock -- the ONLY sanctioned
# way to move the lock past what pyproject.toml forces. A human sees what is
# changing before anything is gated.
#
# Run from the repo root:
#   scripts/update-deps.sh                    # re-resolve the WHOLE lock, gate, promote/rollback
#   scripts/update-deps.sh --dry-run          # print the version diff only, touch nothing
#   scripts/update-deps.sh --package NAME     # bump just NAME (+ whatever it drags with it)
#   scripts/update-deps.sh --package NAME --dry-run
#   scripts/update-deps.sh --no-file          # promote as usual but never file the churn stub
#
# What it does:
#   1. Save the committed uv.lock aside, then let uv rewrite it in place:
#      `uv lock --upgrade` re-resolves everything fresh; `uv lock
#      --upgrade-package NAME` lets only that package (and anything it forces)
#      move -- everything else stays pinned to what is already committed.
#   2. Print a readable VERSION DIFF against the saved lock -- names and
#      versions only: "pkg  OLD -> NEW" for a bump, "+ pkg VERSION" for an
#      addition, "- pkg  VERSION (removed)". This diff is the whole point of
#      the script: it is the artifact, not the install.
#   3. --dry-run stops here and puts the saved lock back, having changed
#      nothing. Otherwise `uv sync` brings ./.venv to the candidate lock
#      EXACTLY -- uv removes anything the lock no longer names, so there is
#      never a half-migrated venv to reason about.
#   4. Run the gates: `uv run --frozen nox -t fix`, `uv run --frozen nox -s tests`.
#   5. GREEN (sync AND both gates pass) -> the candidate stays as uv.lock. This
#      script never commits -- review (`git diff -- uv.lock`) and commit it
#      yourself. ANY OTHER FAILURE -- the sync itself (an uninstallable pin, a
#      yanked release, a network blip) just as much as a red nox gate --
#      prints the paste-into-bd failure report FIRST, then restores the saved
#      lock and syncs ./.venv back to it. The report does not depend on that
#      rollback succeeding (see FAILURE HANDLING below): the saved lock is
#      always put back either way, and if the rollback sync also fails, a
#      loud warning after the report says so and points at `uv sync` as the
#      manual recovery.
#   6. On promote (step 5's GREEN path only -- never on --dry-run, never on
#      a failed/rolled-back run), file ONE bd stub ticket carrying the
#      VERSION DIFF as a durable work order for a human/producer to read
#      upstream changelogs and judge required-work vs. judgment-call in the
#      context of harness's actual call sites. This is a WORK ORDER, not a
#      finding: the script cannot itself judge required-vs-decision, so the
#      stub's own acceptance criteria delegate that judgment (required-only:
#      file follow-ups only for churn that demonstrably breaks/degrades a
#      harness call site; surface new capabilities and judgment calls in the
#      executor's hand-off instead). Noise gate: filing is skipped entirely
#      when every moved package changed only its patch component
#      (mechanically decidable from the diff already computed) -- a ticket
#      per run that is usually noise gets ignored -- that gate, the diff
#      parsing and the rendering all live in the sourceable
#      scripts/dep-churn-lib.sh so they are unit-tested rather than trapped in
#      this script's uninvokable middle. `--no-file` suppresses filing
#      outright; `--dry-run` never reaches this step at all. Filing writes
#      Dolt, so a missing/failing `bd` (or the `bd dolt push` after it) only
#      WARNS -- it never changes this script's exit status or the lock
#      outcome (this script still never commits anything outside ./.venv and
#      uv.lock).
#
# NO -x -- deviates from the scripts/*.sh house style of `#!/bin/bash -ex`.
# `-e` is off too: the genuinely risky steps -- syncing the candidate and
# running the gates -- are each checked explicitly (`if ! uv sync`, `if !
# uv run ...`) so a failure at ANY of them is caught and handled by THIS
# script, not left to errexit tearing the process down mid-rollback with a
# rewritten lock and no report. `-x` is dropped because uv's own chatter and
# the gate output are already the useful signal -- xtrace would just bury it.
#
# FAILURE HANDLING -- the failure report is built and printed BEFORE the
# rollback is attempted (not after), so a hiccup during the rollback itself
# (e.g. a transient network failure inside `uv sync`) can never swallow the
# report -- the two are independent by construction, not by ordering luck.

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1

LOCK="uv.lock"

DRY_RUN=0
PACKAGE=""
NO_FILE=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --package)
      PACKAGE="${2:?--package requires a package name}"
      shift 2
      ;;
    --no-file)
      NO_FILE=1
      shift
      ;;
    -h|--help)
      echo "usage: $0 [--dry-run] [--package NAME] [--no-file]" >&2
      exit 0
      ;;
    *)
      echo "update-deps.sh: unknown argument '$1'" >&2
      echo "usage: $0 [--dry-run] [--package NAME] [--no-file]" >&2
      exit 1
      ;;
  esac
done

if ! command -v uv >/dev/null 2>&1; then
  echo "update-deps.sh: 'uv' not found on PATH -- install uv first" >&2
  exit 1
fi
if [ ! -f "$LOCK" ]; then
  echo "update-deps.sh: $LOCK not found -- run 'uv lock' and commit it first" >&2
  exit 1
fi
if ! git diff --quiet -- "$LOCK"; then
  echo "update-deps.sh: $LOCK has uncommitted changes -- commit or discard them first" >&2
  exit 1
fi

SAVED="$(mktemp)"
trap 'rm -f "$SAVED"' EXIT
cp "$LOCK" "$SAVED"

# Restore the committed lock byte-for-byte. Used by --dry-run and by rollback.
restore_lock() { cp "$SAVED" "$LOCK"; }

# -q: `uv lock` otherwise narrates every resolved package -- exactly the noise
# the VERSION DIFF below exists to replace.
if [ -n "$PACKAGE" ]; then
  if ! uv lock -q --upgrade-package "$PACKAGE"; then
    echo "update-deps.sh: 'uv lock --upgrade-package $PACKAGE' failed -- $LOCK restored" >&2
    restore_lock
    exit 1
  fi
else
  if ! uv lock -q --upgrade; then
    echo "update-deps.sh: 'uv lock --upgrade' failed -- $LOCK restored" >&2
    restore_lock
    exit 1
  fi
fi

# Readable name==version diff. The parsing, the rendering and the stub's skip
# policy all live in the sourceable scripts/dep-churn-lib.sh so they are
# unit-tested rather than trapped in this script's uninvokable middle. Both
# values below are assigned HERE, at top level -- see that library's CONTRACT
# note for why nothing there may return a value by setting a global.
# shellcheck source=dep-churn-lib.sh
. "$REPO/scripts/dep-churn-lib.sh"

CHANGES_RAW="$(dep_changes_raw "$SAVED" "$LOCK")"
DIFF_TEXT="$(dep_version_diff_text "$LOCK" "$CHANGES_RAW")"
echo "$DIFF_TEXT"

if [ "$DRY_RUN" -eq 1 ]; then
  restore_lock
  exit 0
fi

# File ONE bd stub ticket carrying the VERSION DIFF as a durable work order
# -- only called from the GREEN promote path (step 5). Every
# failure mode here WARNS and returns 0: filing must never change this
# script's exit status or the lock outcome.
file_churn_stub() {
  local skip
  if skip="$(dep_stub_skip_reason "$NO_FILE" "$CHANGES_RAW")"; then
    echo "update-deps.sh: $skip"
    return 0
  fi
  if ! command -v bd >/dev/null 2>&1; then
    echo "update-deps.sh: WARNING -- bd not found; skipping churn-evaluation stub ticket." >&2
    return 0
  fi

  local title body acceptance new_id
  title="Evaluate dependency churn from update-deps.sh ($(date +%Y-%m-%d))"
  body="$(cat <<BODY_EOF
Dependency lock update landed via scripts/update-deps.sh. Evaluate the churn
below in the context of harness's actual call sites.

$DIFF_TEXT
BODY_EOF
)"
  acceptance="Required-only filing policy for THIS ticket's executor: read the upstream changelog for each moved package's crossed versions, then open a follow-up ticket ONLY for churn that demonstrably breaks or degrades an existing harness call site. Do NOT file tickets for new capabilities or other judgment calls -- surface those in your hand-off for a human instead."

  if ! new_id="$(bd create --title="$title" --description="$body" \
      --acceptance="$acceptance" --type=task --silent 2>&1)"; then
    echo "update-deps.sh: WARNING -- bd create failed; skipping churn-evaluation stub ticket. ($new_id)" >&2
    return 0
  fi
  echo "update-deps.sh: filed churn-evaluation stub ticket $new_id"

  if ! "$REPO/scripts/bd-dolt-push.sh" >/dev/null 2>&1; then
    echo "update-deps.sh: WARNING -- bd dolt push failed after filing $new_id; sync manually." >&2
  fi
  return 0
}

echo "update-deps.sh: syncing ./.venv to the candidate lock..."
FAILED_AT=""
if ! uv sync --frozen; then
  FAILED_AT="candidate sync (uv sync failed -- see output above)"
else
  echo "update-deps.sh: running gates (nox -t fix, nox -s tests)..."
  if ! uv run --frozen nox -t fix; then
    FAILED_AT="nox -t fix"
  elif ! uv run --frozen nox -s tests; then
    FAILED_AT="nox -s tests"
  fi
fi

if [ -z "$FAILED_AT" ]; then
  echo "update-deps.sh: gates green -- $LOCK now carries the candidate."
  echo "update-deps.sh: review and commit it yourself: git diff -- $LOCK"
  file_churn_stub
  exit 0
fi

echo "update-deps.sh: FAILED ($FAILED_AT) -- discarding the candidate." >&2

# Build and print the report BEFORE attempting the rollback, and regardless
# of whether that rollback succeeds -- see FAILURE HANDLING in the header.
REPORT="$(cat <<REPORT_EOF
=== update-deps.sh FAILURE REPORT (paste into a bd ticket) ===
Attempted update: $( [ -n "$PACKAGE" ] && echo "single package '$PACKAGE'" || echo "full lock re-resolve" )
Failed at:         $FAILED_AT (see output above for the actual error)
Candidate diff that was attempted:
$DIFF_TEXT
Committed $LOCK:   restored unchanged -- nothing to revert.
=== end report ===
REPORT_EOF
)"
echo "$REPORT"

echo "update-deps.sh: restoring the committed $LOCK and syncing ./.venv back to it..." >&2
restore_lock
if ! uv sync --frozen; then
  echo "update-deps.sh: WARNING -- the rollback 'uv sync' from the committed $LOCK ALSO failed." >&2
  echo "update-deps.sh: ./.venv may now be stale or broken. The report above is still" >&2
  echo "update-deps.sh: accurate (the committed $LOCK is back in place); re-run" >&2
  echo "update-deps.sh: 'uv sync' by hand to restore ./.venv." >&2
fi

exit 1
