#!/usr/bin/env bash
#
# Recycled-worktree guard, extracted out of FOUR
# duplicated inline copies that had already started to drift
# (.claude/agents/coding.md x2, .claude/agents/code-reviewer.md x1,
# .claude/agents/land-review.md x1): shell embedded in a markdown fence gets
# neither shellcheck nor a unit test, and this repo has already shipped
# silent, months-long bugs from exactly that pattern twice before
# (scripts/epic-children-closed.sh, scripts/code-concurrency-cap.sh).
#
# WHAT THIS ASSERTS: the harness's `isolation: "worktree"` dispatch is
# *supposed* to hand a fresh agent a worktree branched off `origin/main`
# HEAD,
# with zero commits of its own. That assumption has been observed FALSE in
# production: a dispatched agent got a
# RECYCLED worktree still checked out on a previous ticket's build branch,
# carrying that ticket's unreviewed commits, rather than a fresh branch off
# `origin/main`. A branch-name check alone cannot catch this -- the
# recycled branch still looks like a normal `worktree-agent-...` name -- so
# this script asserts the actual commit graph instead: `HEAD` must be an
# ancestor of `origin/main`.
#
# WHY `origin/main`, NOT bare (local) `main`: worktrees share
# `refs/heads/`, so bare `main` is the MAIN CHECKOUT's local `main` branch
# -- and `/land` leaves that ref carrying un-pushed, un-gated `--no-ff`
# merges for the entire window between its merge loop and its push (a
# property of the HEALTHY path, not just a crash path, since `/code`
# producers run concurrently with `/land` by design). Reading bare `main`
# here used to open two concrete holes: (1) a genuinely recycled worktree
# got reset onto that residue, planting OTHER tickets' un-pushed, un-gated
# commits into this build -- the exact contamination this guard exists to
# prevent, arriving THROUGH the guard; (2) a worktree recycled onto a
# `land/<id>` branch that a live `/land` pass had merged into local `main`
# (but not yet pushed) passed the ancestor check trivially, going silent
# with no rescue and no reset. `origin/main` is never in an intermediate
# state -- `/land` only advances it with an already-gated, already-pushed
# `main` -- so both holes close by reading it instead everywhere below.
# That premise is a property of `/land`'s step ORDER, enforced by an agent
# following its skill rather than by any lock or assertion: see
# `.claude/skills/land/SKILL.md` Section 1 (the pass-start reset) and
# Section 4 (the push, which waits on Section 3's re-gate). If a future
# edit ever reorders push and gate, nothing here fails loudly -- this guard
# is load-bearing on that discipline. Naming the landmark that way is
# deliberate: it used to read "push before `bd close`", which
# invited the misreading that the CLOSE order is what the premise rests on.
# It does not -- closing first would leave `origin/main` just as gated.
#
# AND IT DELIBERATELY DOES NOT FETCH FIRST. `origin/main` is read
# from the local ref cache, as of whenever this repo last fetched. That is not
# a gap to close: `worktree.baseRef: "fresh"` branches a launch worktree from
# exactly this ref, so comparing against it asks precisely the right question
# ("is HEAD derived from the base I was supposed to get?"). Fetching would
# only ever advance the RIGHT-hand side of the ancestor test, which can flip
# it false->true but never true->false -- i.e. it would make the guard
# strictly MORE forgiving, weakening the very check this script exists to
# perform -- while adding a network dependency to the first executable action
# of every dispatch. A stale `origin/main` costs nothing on the remedy side
# either: resetting onto a slightly-lagging but already-gated, already-pushed
# ref is exactly what a genuinely fresh worktree gets, and CLAUDE.md already
# accepts that lag. So: no fetch. Do not add one.
#
# On failure it repairs the worktree: tags the current (foreign) HEAD as
# `rescue/recycled-<sha>` (the ref `reset --hard` is about to rewind belongs
# to ANOTHER ticket -- if that ticket had committed but not pushed, this tag
# is the only thing standing between its work and oblivion), then
# `git reset --hard origin/main`. `git clean -fd` then runs unconditionally
# right after, pass or fail -- see the DIRT-AXIS GAP note below.
#
# Usage: recycled-worktree-guard.sh <context-message>
#   <context-message> is a short clause describing what happens next in the
#   caller's own cycle, e.g. "before doing any work" or "before my own
#   fetch+checkout" -- it is appended to the reported reason so a human
#   reading the transcript knows which cycle's guard fired. This is the one
#   thing that legitimately varies across the four call sites; everything
#   else about the guard is identical, which is the whole point of
#   extracting it to one place.
#
# Exit 0 -- clean: either HEAD was already an ancestor of origin/main
#           (nothing to do), or it wasn't and this script just repaired it.
#           Either way the caller's own HEAD is now a clean ancestor of
#           origin/main and it is safe to proceed.
# Exit 1 -- REFUSED to run the destructive remediation because the current
#           directory is not inside an isolated launch worktree
#           (`.claude/worktrees/...`). This is the one case a caller must
#           STOP and report rather than proceed -- it means `isolation:
#           "worktree"` did not take at all, and this script will not touch
#           the main checkout. The diagnostic is already printed to stderr.
# Exit 2 -- the guard COULD NOT RUN, so its verdict is unknown -- never a
#           statement about the worktree's content. Three causes: a usage error (wrong argument
#           count), `git rev-parse --show-toplevel` itself failing (cwd not
#           inside any git repository at all), or `origin/main` not
#           resolving at all. Like exit 1, the caller must STOP and report;
#           unlike exit 1, nothing here is a claim that the worktree is
#           dirty. BOTH git arms are checked EXPLICITLY rather than left to
#           escape as git's own raw 128 under `set -e` for the
#           `--show-toplevel` one for the `origin/main` one,
#           whose own note follows.
#
#           The `origin/main`-unresolvable arm is checked EXPLICITLY, up
#           front, rather than left to fall out of the ancestor test. `git merge-base --is-ancestor HEAD origin/main` exits
#           non-zero for BOTH "not an ancestor" and "no such ref", and `if !`
#           consumes that status, so an unresolvable ref used to take the
#           remediation branch: it printed the CONTAMINATED banner (a false
#           accusation -- the worktree is fine), created a stray
#           `rescue/recycled-<sha>` ref that nothing GCs, and only then died
#           on `git reset --hard`'s own "unknown revision" (exit 128, verified
#           empirically). Safe, but it misdiagnosed a missing ref as foreign
#           commits -- exactly the confusion an earlier fix exists to prevent.
#           Bare `main` could not fail this way (a local branch that always
#           exists), so this arm is specifically the cost of reading a
#           remote-tracking ref, and is paid here rather than in a transcript.
#
# DIRT-AXIS GAP, CLOSED: the ancestor check alone cannot detect a
# worktree recycled onto a `land/<id>` branch that has since landed -- HEAD
# is already an ancestor of origin/main in that case, so the check above
# passes trivially, exactly as it would for a genuinely fresh worktree. Harmless on
# the ANCESTRY axis (what the guard misses in that case already satisfies
# /land's own worktree-reclaim predicate), but not on the DIRT axis: any
# untracked leftovers from the recycled worktree would otherwise survive,
# since the remediation branch below is the only place `git clean -fd` used
# to run. Closed by running `git clean -fd` unconditionally, right after the
# ancestor check either way (see below) -- a no-op on a genuinely fresh
# worktree (nothing untracked to remove, and it never touches `.gitignore`d
# build state like `venv/`), and it clears exactly the leftover dirt on an
# undetected recycle. Full reasoning: docs/decisions.md.
#
# BOOTSTRAP GAP, unavoidable, mitigate at the call site: the
# guard must run FIRST, in a possibly-contaminated worktree, so this very
# script is read FROM that tree. A worktree recycled from a branch cut
# before this script landed on main would not have it on disk at all. The
# call site must anchor the path to `$(git rev-parse --show-toplevel)`
# (never the caller's cwd) and treat a missing/non-executable script as a
# reason to STOP AND REPORT -- never as license to silently skip the guard
# and proceed. See the four .claude/agents/*.md call sites for the wrapper
# that does this.
#
# One of harness's 0/1/2 precondition guards. The shared contract -- exit
# meanings, the STOP-AND-REPORT rule, the roster, why these are not
# gate-lib.sh consumers -- is stated ONCE and deliberately NOT restated here:
# [docs/agents-workflow.md]

set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <context-message>" >&2
  exit 2
fi
context_message="$1"

if ! top="$(git rev-parse --show-toplevel)"; then
  echo "recycled-worktree-guard: GUARD COULD NOT RUN -- 'git rev-parse" \
       "--show-toplevel' failed (git's own error is above). cwd is most likely not inside any" \
       "git repository at all -- the same class of harness misdispatch that motivated this" \
       "script's sibling, scripts/isolation-guard.sh. This says NOTHING about" \
       "whether a worktree is contaminated -- the check did not run. STOP and report" \
       "$context_message." >&2
  exit 2
fi

case "$top" in
  */.claude/worktrees/*) ;;    # an isolated launch worktree -- safe to repair
  *)
    echo "NOT in an isolated launch worktree ($top): refusing to reset. STOP and report." >&2
    exit 1
    ;;
esac

if ! git rev-parse --verify --quiet origin/main >/dev/null; then
  echo "GUARD COULD NOT RUN: \`origin/main\` does not resolve in this repo, so there" \
       "is no trusted ref to check HEAD against. This says NOTHING about whether this worktree is" \
       "contaminated -- the check did not run. Likely causes: no \`origin\` remote, a remote under" \
       "another name, or a clone that has never fetched \`main\`. Fix the remote, then re-dispatch." \
       "STOP and report $context_message." >&2
  exit 2
fi

if ! git merge-base --is-ancestor HEAD origin/main; then
  head_sha="$(git rev-parse --short HEAD)"
  echo "CONTAMINATED LAUNCH WORKTREE: HEAD ($head_sha) is NOT an ancestor of" \
       "origin/main -- this worktree carries commit(s) foreign to origin/main (recycled from a" \
       "previous ticket's build rather than freshly branched off origin/main HEAD). Resetting" \
       "onto origin/main HEAD (never bare main, which /land can leave carrying un-pushed," \
       "un-gated merges for its entire merge window) $context_message." >&2
  git branch "rescue/recycled-$head_sha" HEAD   # keep the evidence -- another ticket's ref
  git reset --hard origin/main
fi
git clean -fd   # unconditional -- runs after the `case` either way, still worktree-scoped
