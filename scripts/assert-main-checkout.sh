#!/usr/bin/env bash
#
# Main-checkout identity guard for /land. Called as the FIRST LINE of every
# land/SKILL.md fence that issues a cwd-resolved mutation it cannot otherwise
# protect, each needing its OWN call because every fenced block in that file
# is a separate Bash invocation, so one block's `exit` cannot stop
# another's. Do NOT maintain the call-site list here: it went stale within one
# ticket of being written, which is why this header now names the RULE instead.
# tests/test_land_skill_guard_coverage.py sweeps every fence in land/SKILL.md
# and is the authoritative record of which mutations are guarded and which
# are exempt. The same rule covers a caller that is itself a script rather
# than a fence: a tool whose whole body assumes the main checkout calls this
# as its own first action instead of making every one of its call sites
# remember to. Note that not every caller opens with a `git reset
# --hard` -- the reset is the worst case the diagnostic below narrates, not the
# only thing this guard protects.
#
# THE BUG THIS REPLACES: Section 1 used to run its `git checkout -f main`
# through `git -C "$(git rev-parse --show-toplevel)" checkout -f main`, on
# the theory that the `-C` pinned the command to the main checkout. It did
# not: `--show-toplevel` resolves relative to CWD, so the value it produces
# is always wherever you already are -- in the main checkout that's a no-op
# (redundant, harmless), and in a worktree it resolves to THAT WORKTREE's own
# root, never the main checkout. A `-C` computed from cwd cannot redirect a
# command away from cwd; it can only restate it. That reads as a safety guard
# and is not one. Worse, the genuinely destructive line two lines later --
# `git reset --hard origin/main` -- carried no `-C` at all, so run from a
# worktree it would hard-reset THAT WORKTREE's own branch, discarding any
# uncommitted work there that no `git reflog` recovers (discarded commits are
# recoverable; discarded uncommitted work is not). `/land` is defined to run
# only in the main checkout (see the top of land/SKILL.md), so this was
# latent, not live, when filed -- but a guard that looks like protection and
# provides none is worse than no guard, because nobody double-checks it.
#
# THE FIX: an IDENTITY check, not a redirect. `git rev-parse --git-common-dir`
# returns the one `.git` directory every worktree of a repo shares, main
# checkout included -- so the main checkout's own toplevel is that
# directory's parent, and a linked worktree's toplevel never is (a linked
# worktree's PRIVATE gitdir is `<common>/.git/worktrees/<name>`, but its
# COMMON dir -- what this reads -- is still the shared `<common>/.git`).
# Unlike `--show-toplevel`, this value does NOT depend on which worktree you
# happen to be standing in, which is exactly what makes it usable to
# DISTINGUISH the two rather than just restate wherever cwd already is.
#
# This script only ASSERTS; it never redirects a command to a different
# directory and never repairs anything. On a mismatch the only sanctioned
# response is to STOP the /land pass and report -- not to retry against a
# guessed-correct directory, and not to `cd` there and continue, since a
# harness that dispatched /land into the wrong directory in the first place
# is a bug an operator needs to see, not something to route around silently.
#
# Usage: assert-main-checkout.sh
#   (no arguments -- this is a pure precondition, nothing to parametrize)
#
# Exit 0 -- cwd's toplevel IS harness's main checkout. Safe to proceed.
# Exit 1 -- cwd's toplevel is NOT the main checkout (e.g. a linked worktree).
#           A diagnostic is already printed to stderr. STOP AND REPORT --
#           /land is defined to run only in the main checkout; do not retry
#           from here.
# Exit 2 -- usage error; OR `git rev-parse` itself could not answer (cwd is
#           not inside any repository, git is missing/too old for
#           `--path-format`, the repo is unreadable); OR the repository
#           layout is not the standard non-bare shape this script assumes
#           (`--git-common-dir` not ending in `/.git`, e.g. a bare repo, a
#           submodule, `--separate-git-dir`, or an unusual $GIT_DIR).
#           Caller bug, machine fault, or an unsupported layout -- never a
#           location verdict; do not treat this as "not the main checkout".
#
# Those three are the WHOLE contract: this script must never exit anything
# else. Both `git rev-parse` calls below are therefore wrapped rather than
# left to `set -e`, which would propagate git's own 128 -- an undocumented
# status carrying a bare `fatal:` with no an earlier fix context, and, worse,
# indistinguishable to the caller from the 0/1/2 it is told to expect. The
# "not inside a repository" case is not hypothetical: it is the same class of
# harness misdispatch that motivated `scripts/isolation-guard.sh`.
#
# Not sourced from `scripts/gate-lib.sh`: that helper's "GATE COULD NOT RUN"
# banner belongs to the CONTENT-verifying gates (validate-mermaid.sh,
# merge-precheck.sh, land-merge-one.sh), where exit 2 means "could not judge
# the content." This is a precondition GUARD, and it matches its true
# siblings -- isolation-guard.sh and
# recycled-worktree-guard.sh, neither of which sources gate-lib.sh either.
#
# One of harness's 0/1/2 precondition guards. The shared contract -- exit
# meanings, the STOP-AND-REPORT rule, the roster, why these are not
# gate-lib.sh consumers -- is stated ONCE and deliberately NOT restated here:
# [docs/agents-workflow.md]

set -euo pipefail

if [ "$#" -ne 0 ]; then
  echo "usage: $0 (no arguments)" >&2
  exit 2
fi

if ! common_dir="$(git rev-parse --path-format=absolute --git-common-dir)"; then
  echo "assert-main-checkout: MACHINE FAULT -- 'git rev-parse --git-common-dir' failed" \
       "(git's own error is above). cwd is most likely not inside any git repository at" \
       "all, which is a harness/dispatch fault rather than a location verdict. STOP AND" \
       "REPORT; do not treat this as 'not the main checkout'." >&2
  exit 2
fi

case "$common_dir" in
  */.git)
    main_checkout="${common_dir%/.git}"
    ;;
  *)
    echo "assert-main-checkout: unsupported repository layout -- --git-common-dir" \
         "returned '$common_dir', which does not end in /.git (a bare repo, or an" \
         "unusual \$GIT_DIR). Cannot safely derive the main checkout path. This is a" \
         "MACHINE FAULT, not a location verdict -- do not treat it as running in the" \
         "wrong directory." >&2
    exit 2
    ;;
esac

if ! cwd_toplevel="$(git rev-parse --show-toplevel)"; then
  echo "assert-main-checkout: MACHINE FAULT -- 'git rev-parse --show-toplevel' failed" \
       "(git's own error is above) even though --git-common-dir resolved to" \
       "'$common_dir'. There is no work tree to compare against, so no location verdict" \
       "is possible. STOP AND REPORT." >&2
  exit 2
fi

if [ "$cwd_toplevel" != "$main_checkout" ]; then
  echo "NOT RUNNING IN THE MAIN CHECKOUT: cwd resolves to '$cwd_toplevel'," \
       "but the main checkout is '$main_checkout'. /land is defined to run only in the" \
       "main checkout, on main -- every command in this caller's block assumes that and" \
       "carries no -C of its own. Run from here, they would mutate THIS directory instead:" \
       "silently committing or merging onto ITS branch, and at worst ('git reset --hard'," \
       "'git checkout') destroying uncommitted work here that no reflog recovers. STOP AND" \
       "REPORT to the operator; do not retry without understanding why cwd was wrong." >&2
  exit 1
fi

exit 0
