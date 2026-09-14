#!/usr/bin/env bash
#
# End-of-pass local worktree + branch GC for /land. The ONLY local reclaim in a
# pass: there is no per-ticket removal step.
#
# WHY A SCRIPT. This was ~80 lines of destructive shell (worktree remove --force,
# branch -D) fenced inside .claude/skills/land/SKILL.md, where nothing
# lint-checked it and only a markdown scanner could assert anything about it. It
# performs the most dangerous operations in the harness, so it belongs where it
# can be shellcheck'd and unit-tested.
#
# THE CONTRACT. Any worktree under .claude/worktrees/ that is UNLOCKED, CLEAN,
# and EITHER has not diverged from the default branch OR -- if branch-attached --
# has not diverged from its own branch's origin counterpart, is reclaimable,
# whoever made it. The second arm exists because an escalated ticket's reviewer
# worktree never merges by definition; content pushed to origin/land/<id> is
# captured just as safely.
#
# The per-candidate decision lives in "$TOP/scripts/worktree-gc-classify.sh". What lives
# HERE is what a side-effect-free classifier cannot own: reading the porcelain,
# resolving a stale lock (a real mutation), and the two destructive calls the
# classifier only ever recommends.
#
# WHY THE DIRTY CHECK EXISTS ON TOP OF THE ANCESTRY ARMS. A worktree freshly
# branched off the default branch is trivially "merged" by zero divergence, so
# that proxy reads TRUE for a live, uncommitted build the instant it is created.
# An unguarded zero-divergence read once destroyed two builds' uncommitted work
# outright. A dirty tree is never reclaimed, regardless of lock state or ancestry.
#
# ONE UNENFORCED COUPLING keeps this reclaiming anything at all: .gitignore. A
# finished worktree is full of untracked build junk (venv/, .nox/, __pycache__/)
# and reads clean ONLY because those are ignored. Un-ignore one and every
# worktree reads dirty and this sweep silently reclaims NOTHING.
#
# Usage: scripts/worktree-gc-sweep.sh
# Exit codes: 0 = swept (summary on stdout), 2 = machine fault / wrong checkout.
#
# NO --base-ref FLAG. An earlier revision accepted one, but it
# governed backstop 3's `git branch --merged` ONLY -- it never reached the
# worktree sweep (that decision belongs to scripts/worktree-gc-classify.sh,
# which takes no base ref and hardcodes `main`, a character-for-character port
# of the condition this loop used when it lived in a markdown fence) or
# backstop 2 (which keys off remote existence, no base ref at all). Passing
# anything but `main` would therefore have judged bare builder refs against
# one branch while every `worktree remove --force` still judged against
# `main` -- two different notions of "captured" inside one destructive pass.
# This repo has exactly one default branch and no non-test caller ever
# passed a non-default value; the flag was dropped rather than threaded
# through to the classifier. `main` is now a literal at the one site that
# ever consumed it.
#
# That unifies the BASE REF, not the whole predicate: the classifier's capture
# test is the widened one (ancestor of `main` OR of the branch's own
# `origin/<branch>`), while backstop 3 below is still the narrower `git branch
# --merged main` alone. A bare builder ref captured only on `origin/<branch>`
# is therefore reclaimed as a worktree but kept as a ref. That divergence
# predates this change and is deliberately left alone here -- widening a
# `branch -D` is its own decision, deferred in the source project.
#
# Not sourced from scripts/gate-lib.sh, though the "GATE COULD NOT RUN" banner
# below is that library's: same abstention as scripts/assert-main-checkout.sh
# makes, for the same reason -- gate-lib.sh's exit 2 means "could not judge the
# CONTENT", and this is a sweep with a precondition guard, not a content gate.
set -u

TOP="$(git rev-parse --show-toplevel 2>/dev/null)" || TOP=""
[ -n "$TOP" ] || { echo "GATE COULD NOT RUN: not inside a git repository" >&2; exit 2; }

[ "$#" -eq 0 ] || { echo "GATE COULD NOT RUN: unknown argument '$1' (this script takes none)" >&2; exit 2; }

# Every destructive call below is ref- or path-addressed, but the sweep as a
# whole only makes sense from the primary checkout -- and running it from a
# worktree would enumerate that worktree's own siblings.
"$TOP/scripts/assert-main-checkout.sh" || exit 2

# Minimum age of a NOT-MERGED builder worktree's last commit before its DIRECTORY
# (never its branch ref) becomes eligible for the dir-only reclaim below.
MIN_AGE_SECONDS="${LAND_WORKTREE_DIRONLY_MIN_AGE_SECONDS:-21600}"

# FIELD ORDER IS LOAD-BEARING — DO NOT REORDER ($BR must stay LAST). Tab is IFS *whitespace*,
# so `read` collapses adjacent tabs and does NOT preserve an empty MIDDLE field. `branch` is
# the one field that can be empty (a DETACHED worktree — explicitly supported). With `branch`
# in the middle, a detached worktree's line shifts every later field left: $BR swallows the
# locked flag and $LOCKED reads EMPTY, so a LOCKED, LIVE agent's worktree sails past the gate
# into the `--force` below — precisely the "rip a worktree out from under a running agent" harm
# the gate exists to prevent. Keeping `branch` last makes its empty case a TRAILING delimiter,
# which `read` discards harmlessly.
RECLAIMED=0; RECLAIMED_DIR_ONLY=0; SKIP_LOCKED=0; SKIP_NOTMERGED=0; SKIP_DIRTY=0; FAILED=0
STALE_LOCKS_FOUND=0
while IFS=$'\t' read -r WT SHA LOCKED BR; do
  if [ "$LOCKED" = "1" ]; then
    # The lock recorded here is PER-SESSION, not per-agent — measured: several worktrees can
    # share ONE lock-owner pid (the parent session process), so a DEAD session leaves every
    # worktree it ever locked stuck at this check forever. worktree-lock-stale.sh proves the
    # recorded pid is either not running at all, or has been REUSED by an unrelated later
    # process (matching the recorded start-time token) — a plain PID-liveness probe cannot
    # safely make this call. A lock it cannot positively prove dead is left alone (fail closed).
    LOCK_REASON=$(git worktree list --porcelain | awk -v want="$WT" '
      /^worktree / { path=$2; reason="" }
      /^locked/    { reason=substr($0,8) }
      /^$/         { if (path==want) { print reason; exit }; path="" }
    ')
    if "$TOP/scripts/worktree-lock-stale.sh" "$LOCK_REASON"; then
      STALE_LOCKS_FOUND=$((STALE_LOCKS_FOUND + 1))
      # `git worktree remove` refuses a still-locked worktree even with --force — that flag
      # overrides "has modifications," never "is locked". Proving the SESSION is dead is not
      # the same as clearing git's own on-disk lock, so unlock it now and reflect that in
      # $LOCKED so classify judges this candidate as unlocked.
      git worktree unlock "$WT" 2>/dev/null || true
      LOCKED=0
    fi
  fi
  # The classifier is the single source of truth for the bucket. It takes no action; the case
  # below performs the two destructive calls it only ever recommends — reading `git worktree
  # remove`'s own exit status, not merely the fact that we attempted it, so the summary can
  # never report "reclaimed N" when every remove FAILED.
  BUCKET=$("$TOP/scripts/worktree-gc-classify.sh" "$WT" "$SHA" "$LOCKED" "$BR" "$MIN_AGE_SECONDS")
  case "$BUCKET" in
    keep-locked)    SKIP_LOCKED=$((SKIP_LOCKED + 1)) ;;
    keep-notmerged) SKIP_NOTMERGED=$((SKIP_NOTMERGED + 1)) ;;
    keep-dirty)     SKIP_DIRTY=$((SKIP_DIRTY + 1)) ;;
    dir-only)
      if git worktree remove --force "$WT"; then
        RECLAIMED_DIR_ONLY=$((RECLAIMED_DIR_ONLY + 1))   # ref intentionally KEPT
      else
        FAILED=$((FAILED + 1))
      fi
      ;;
    full-reclaim)
      # ASYMMETRY, DELIBERATE: the DIRECTORY removal is checked, the ref delete is
      # not -- `full=N` means "directory gone", not "directory and ref gone". A
      # refused `branch -D` leaks a ref, which is the safe direction and which
      # backstops 2 and 3 below exist to collect on a later pass; a refused
      # `worktree remove` would leave the directory live, which is not.
      if git worktree remove --force "$WT"; then
        [ -n "$BR" ] && git branch -D "$BR" 2>/dev/null || true
        RECLAIMED=$((RECLAIMED + 1))
      else
        FAILED=$((FAILED + 1))
      fi
      ;;
    *)
      # Defensive net against a future classify bug printing something outside its documented
      # bucket set. Fails CLOSED rather than silently falling through either reclaim arm.
      echo "worktree GC: unexpected classify output '$BUCKET' for $WT — treating as failed" >&2
      FAILED=$((FAILED + 1))
      ;;
  esac
done < <(git worktree list --porcelain | awk '
  /^worktree / { path=$2; head=""; branch=""; locked=0 }
  /^HEAD / { head=$2 }
  /^branch refs\/heads\// { branch=substr($0,19) }
  /^locked/ { locked=1 }
  /^$/ { if (path!="" && path ~ /\/\.claude\/worktrees\//) print path"\t"head"\t"locked"\t"branch; path="" }
')
git worktree prune          # drop any now-stale worktree admin entries
# Always emit one line. "reclaimed 0 of 0" (nothing to do) reads differently from "reclaimed 0
# of N" (everything was skipped — worth investigating), and the reason breakdown makes a
# regression that silently zeroes out GC visible instead of indistinguishable from idle.
TOTAL=$((RECLAIMED + RECLAIMED_DIR_ONLY + SKIP_LOCKED + SKIP_NOTMERGED + SKIP_DIRTY + FAILED))
echo "worktree GC: reclaimed $((RECLAIMED + RECLAIMED_DIR_ONLY)) of $TOTAL candidate(s) (full=$RECLAIMED, dir-only=$RECLAIMED_DIR_ONLY, stale-locks-cleared=$STALE_LOCKS_FOUND; skipped: locked=$SKIP_LOCKED, not-merged=$SKIP_NOTMERGED, dirty=$SKIP_DIRTY; failed=$FAILED)"

# Second backstop: dangling local land/<id> refs with no worktree attached at all (so the sweep
# above never considered them) and no remote counterpart left. "Remote gone" is sufficient
# signal on its own: an in-flight ticket's origin/land/<id> always exists, so a missing remote
# means this local ref is already stale. No extra locked/merged check is needed — `git branch
# -D` itself refuses harmlessly if the branch is still checked out somewhere.
#
# List origin's land refs ONCE and only sweep if that listing SUCCEEDED: an unreachable origin
# makes ls-remote exit non-zero, and reading that as "every remote land branch is gone" would
# force-delete every local land ref on a transient network blip. An empty-but-successful
# listing correctly means every local land ref is stale.
#
# STRIP THE WORKTREE SUFFIX BEFORE COMPARING. Reviewers and pickups check the branch out under
# `land/<id>--<their-own-worktree-dir>`, which can NEVER byte-match origin's `land/<id>`.
# Comparing raw would make the "remote still exists — keep" arm dead code for every ref this
# sweep sees, silently demoting the backstop to "delete every land/* ref not currently checked
# out" and force-deleting an in-flight ticket's ref — unpushed commits with it — the moment its
# worktree goes away. `${BR%%--*}` maps the local name back to the remote one and leaves a bare
# name untouched. Safe because an id never contains `--`.
if REMOTE_LAND=$(git ls-remote --heads origin 'land/*' 2>/dev/null); then
  REMOTE_LAND=$(printf '%s\n' "$REMOTE_LAND" | sed 's#^.*refs/heads/##')
  # Report only deletions that ACTUALLY happened, reading `git branch -D`'s own exit status
  # rather than announcing one "before the fact" behind `|| true`. OBSERVED: this backstop once
  # printed "deleting stale local ref …" while the ref still existed afterward — the delete had
  # been refused (still checked out in a locked worktree) and `|| true` swallowed it silently.
  # Process substitution, not a pipe, so these counters survive past the loop.
  B2_DELETED=0; B2_FAILED=0
  while read -r BR; do
    printf '%s\n' "$REMOTE_LAND" | grep -qxF "${BR%%--*}" && continue   # remote exists — keep
    if git branch -D "$BR" 2>/dev/null; then
      B2_DELETED=$((B2_DELETED + 1))
    else
      B2_FAILED=$((B2_FAILED + 1))
    fi
  done < <(git for-each-ref --format='%(refname:short)' 'refs/heads/land/*')
  echo "bare-ref backstop2 (land/*): deleted $B2_DELETED stale local ref(s) (failed=$B2_FAILED)"
fi

# Third backstop: dangling local worktree-agent-* refs with no worktree attached — the same bug
# as backstop 2 but the OTHER namespace, invisible to both nets above, accumulating without
# bound (17 confirmed orphans on one machine). This namespace needs a DIFFERENT guard: a
# builder branch is never pushed to origin, so "remote gone" is meaningless here and would
# delete a LIVE, still-building branch. The correct guard is the same NOTION the worktree
# sweep applies — captured elsewhere — reached by a branch-NAME lookup, because a bare ref has
# no worktree and therefore no HEAD line to test; plus not currently checked out anywhere.
# Same base ref (`main`), but a NARROWER predicate than the classifier's: no
# `origin/<branch>` arm here — see the header. Deliberate; this arm ends in `branch -D`.
MERGED=$(git branch --merged main --format='%(refname:short)')
CHECKED_OUT=$(git worktree list --porcelain | awk '/^branch refs\/heads\//{print substr($0,19)}')
B3_DELETED=0; B3_FAILED=0
while read -r BR; do
  printf '%s\n' "$CHECKED_OUT" | grep -qxF "$BR" && continue   # still checked out — keep
  printf '%s\n' "$MERGED" | grep -qxF "$BR" || continue        # not merged — keep (in-flight)
  if git branch -D "$BR" 2>/dev/null; then
    B3_DELETED=$((B3_DELETED + 1))
  else
    B3_FAILED=$((B3_FAILED + 1))
  fi
done < <(git for-each-ref --format='%(refname:short)' 'refs/heads/worktree-agent-*')
echo "bare-ref backstop3 (worktree-agent-*): deleted $B3_DELETED stale local ref(s) (failed=$B3_FAILED)"

exit 0
