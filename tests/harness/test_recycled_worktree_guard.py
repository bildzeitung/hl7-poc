"""Tests for scripts/recycled-worktree-guard.sh (proj-ivth, proj-isl3).

The recycled-worktree guard (proj-nt98) used to be a ~15-line inline bash
block duplicated at FOUR sites (.claude/agents/coding.md x2,
.claude/agents/code-reviewer.md x1, .claude/agents/land-review.md x1) --
shell in a markdown fence gets neither shellcheck nor a unit test, and the
four copies had already started to drift from each other (proj-qv5t added a
`.claude/worktrees/` precondition and a rescue-branch line to two of them but
not the third). This extracts the guard to one script so it is covered by
`nox -s shellcheck` and this suite.

proj-isl3: the guard's predicate and remedy now read `origin/main`, never
bare (local) `main`. Worktrees share `refs/heads/`, so bare `main` is the
MAIN CHECKOUT's local `main` branch -- and `/land` leaves that ref carrying
un-pushed, un-gated `--no-ff` merges for the entire window between its merge
loop and its push, on the HEALTHY path, not just a crash path. Every repo
built here therefore gets a real `origin` remote (a bare repo in `tmp_path`)
so local `main` and `origin/main` can be made to diverge exactly the way a
live `/land` pass diverges them -- tests that only ever pushed immediately
could not exercise the residue window this ticket is about at all.

All tests run the ACTUAL `scripts/recycled-worktree-guard.sh` against real
git repositories (with real `git worktree add` checkouts and a real `origin`
remote) built in `tmp_path` -- no fake git, no mocked subprocess --
sabotage-provable per the proj-verb bar: reverting the script's `case` guard,
its ancestor check, its `origin/main` reads, or its rescue-branch line
directly would turn the corresponding test here red.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _gitrepo import _git

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "recycled-worktree-guard.sh"


def _init_repo(tmp_path: Path) -> Path:
    """A throwaway repo with a real `origin` remote and one commit on
    `main`, pushed, isolated user config. `origin/main` is the ref the
    guard now reads (proj-isl3); tests that need a residue window advance
    local `main` without pushing, exactly as a live `/land` pass does."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


def _push_trunk(repo: Path) -> None:
    """Advance `origin/main` to match local `main` -- the moment `/land`'s
    Section 4 push happens, closing the residue window."""
    _git(repo, "push", "-q", "origin", "main")


def _advance_local_trunk_without_pushing(
    repo: Path, filename: str, message: str
) -> None:
    """Commit directly onto local `main` in the main checkout WITHOUT
    pushing -- models the un-pushed, un-gated residue `/land` leaves on
    local `main` for the entire window between its merge loop and its
    push (proj-isl3), whether from a healthy in-flight pass or a crash."""
    _git(repo, "checkout", "-q", "main")
    (repo / filename).write_text(f"{message}\n")
    _git(repo, "add", filename)
    _git(repo, "commit", "-q", "-m", message)


def _add_worktree(
    repo: Path, rel_path: str, branch: str, *, foreign_commit: bool = False
) -> Path:
    """A real `git worktree add`, branched off `origin/main` -- matching
    `worktree.baseRef: "fresh"` (proj-jzbz), what a genuinely fresh launch
    worktree actually branches from.

    With `foreign_commit=True`, one extra commit is made in the new worktree
    -- simulating a previous ticket's build branch that had already
    committed work. That is exactly what makes `HEAD` fail
    `git merge-base --is-ancestor HEAD origin/main`: `origin/main` doesn't
    contain the extra commit, matching the production shape (proj-eshl) of a
    recycled worktree still sitting on a foreign ticket's branch.
    """
    wt = repo / rel_path
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", str(wt), "-b", branch, "origin/main")
    if foreign_commit:
        (wt / "foreign.txt").write_text("someone else's ticket\n")
        _git(wt, "add", "foreign.txt")
        _git(wt, "commit", "-q", "-m", "foreign ticket's build commit")
    return wt


def _run(
    worktree: Path, context: str = "before doing any work"
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), context],
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize(
    "script_args", [[], ["one", "two"]], ids=["no-args", "too-many-args"]
)
def test_wrong_argument_count_exits_2(tmp_path: Path, script_args: list[str]) -> None:
    """Anything but exactly one positional arg is a usage error (exit 2) -- a
    caller bug, distinct from a worktree problem (exit 0/1)."""
    repo = _init_repo(tmp_path)
    result = subprocess.run(
        ["bash", str(SCRIPT), *script_args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, result.stdout + result.stderr


def test_not_inside_any_repository_is_exit_2_not_a_raw_git_128(
    tmp_path: Path,
) -> None:
    """cwd outside any git repository at all -- `git rev-parse --show-toplevel`
    fails, and the script must convert that into its own documented exit 2
    with a `GUARD COULD NOT RUN` diagnostic, NOT let `set -e` propagate git's
    raw 128 (proj-t6ni). Why 128 is unacceptable is the family contract's
    business: docs/agents-workflow.md, "Precondition guards (the 0/1/2
    family)".

    Reached before the `.claude/worktrees/` case check even runs, so this
    must NOT be misreported as "not in an isolated launch worktree" (exit 1)
    -- there is no worktree, isolated or otherwise, to make that claim about.
    Mirrors
    tests/test_assert_main_checkout.py::test_not_inside_any_repository_is_exit_2_not_a_raw_git_128,
    the sibling this behavior was backported from."""
    outside = tmp_path / "not-a-repo"
    outside.mkdir()

    result = _run(outside)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "GUARD COULD NOT RUN" in result.stderr
    assert "GUARD COULD NOT RUN" in result.stderr


def test_clean_worktree_at_trunk_head_is_a_noop(tmp_path: Path) -> None:
    """HEAD == origin/main exactly (a genuinely fresh launch worktree): exit
    0, nothing rescued, nothing reset."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, ".claude/worktrees/fresh", "worktree-agent-fresh")
    head_before = _git(wt, "rev-parse", "HEAD").stdout.strip()

    result = _run(wt)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == head_before
    branches = _git(repo, "branch", "--list", "rescue/*").stdout
    assert branches.strip() == ""


def test_worktree_merely_behind_origin_trunk_is_still_a_noop(tmp_path: Path) -> None:
    """`origin/main` advancing after the worktree was created (a normal
    fan-out race, or an ordinary `/land` push) must NOT trip the guard: HEAD
    is still an ancestor of the new `origin/main` tip."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, ".claude/worktrees/behind", "worktree-agent-behind")
    # Advance main in the main checkout AND push -- origin/main moves too.
    _advance_local_trunk_without_pushing(repo, "g.txt", "main advances")
    _push_trunk(repo)
    head_before = _git(wt, "rev-parse", "HEAD").stdout.strip()

    result = _run(wt)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == head_before


def test_genuinely_fresh_worktree_is_a_noop_even_mid_land_residue_window(
    tmp_path: Path,
) -> None:
    """proj-isl3's OTHER half -- the false-positive axis. The ticket's stated
    expectation is that "the guard's verdict should not depend on whether a
    `/land` pass happens to be mid-flight", which cuts both ways: the two
    sibling tests pin that a CONTAMINATED verdict is reached correctly during
    the residue window, and this one pins that a CLEAN verdict is left alone
    by it. A genuinely fresh worktree sitting at `origin/main` while local
    `main` carries un-pushed merges must stay an exact no-op -- no rescue
    ref, no reset, nothing.

    This is the single most common real state during the window this ticket
    is about (a `/code` producer dispatched while `/land` is mid-pass, which
    happens by design under the shared concurrency cap), and without it
    nothing here would catch a future "hardening" that made the predicate
    stricter than ancestry -- e.g. requiring `HEAD == origin/main`, or
    re-introducing a comparison against local `main` -- which would nuke and
    rescue-tag every healthy worktree dispatched during a land."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(
        repo, ".claude/worktrees/fresh-mid-land", "worktree-agent-fresh2"
    )
    head_before = _git(wt, "rev-parse", "HEAD").stdout.strip()
    # A live /land pass has merged into local main but not yet pushed.
    _advance_local_trunk_without_pushing(
        repo, "residue.txt", "unpushed --no-ff merge, land still in flight"
    )
    assert _git(repo, "rev-parse", "main").stdout.strip() != head_before

    result = _run(wt)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "CONTAMINATED" not in result.stderr
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == head_before
    assert _git(repo, "branch", "--list", "rescue/*").stdout.strip() == ""
    assert not (wt / "residue.txt").exists()


def test_unresolvable_origin_trunk_exits_2_without_accusing_the_worktree(
    tmp_path: Path,
) -> None:
    """Reading a remote-tracking ref adds a failure mode bare `main` never
    had: the ref may not resolve at all. `git merge-base --is-ancestor` exits
    non-zero for both "not an ancestor" and "no such ref", and `if !` consumes
    that status -- so without the explicit up-front check this fell into the
    remediation branch, printed the CONTAMINATED banner about a perfectly
    clean worktree, left a stray `rescue/recycled-<sha>` ref behind, and only
    then died on `git reset --hard`'s own error (exit 128).

    Required behaviour is proj-9i2p's: the machine could not be checked, so
    say exactly that (exit 2) and make no claim about the content. This test
    goes red if the `git rev-parse --verify origin/main` pre-check is
    removed."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, ".claude/worktrees/no-origin", "worktree-agent-no-origin")
    head_before = _git(wt, "rev-parse", "HEAD").stdout.strip()
    # Drop the remote entirely -- origin/main no longer resolves anywhere.
    _git(repo, "remote", "remove", "origin")
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")

    result = _run(wt)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "GUARD COULD NOT RUN" in result.stderr
    # Must NOT accuse the worktree of carrying foreign commits...
    assert "CONTAMINATED" not in result.stderr
    # ...and must leave it exactly as found: no rescue ref, no reset.
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == head_before
    assert _git(repo, "branch", "--list", "rescue/*").stdout.strip() == ""


def test_contaminated_worktree_is_rescued_reset_onto_origin_trunk_and_cleaned(
    tmp_path: Path,
) -> None:
    """The core repro (proj-nt98/proj-eshl): a launch worktree recycled onto
    a foreign ticket's build branch. Must: tag the foreign HEAD as
    rescue/recycled-<sha>, reset HEAD to origin/main (never bare local
    main -- proj-isl3), and remove untracked leftovers (git clean -fd)."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(
        repo,
        ".claude/worktrees/recycled",
        "worktree-agent-other-ticket",
        foreign_commit=True,
    )
    foreign_sha_short = _git(wt, "rev-parse", "--short", "HEAD").stdout.strip()
    foreign_sha_full = _git(wt, "rev-parse", "HEAD").stdout.strip()
    # Untracked leftovers from the recycled worktree's own build.
    (wt / "leftover.tmp").write_text("scratch file from the previous ticket\n")

    result = _run(wt)

    assert result.returncode == 0, result.stdout + result.stderr
    origin_trunk_sha = _git(repo, "rev-parse", "origin/main").stdout.strip()
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == origin_trunk_sha
    # The rescue tag is the durable pointer to the foreign commit -- it must
    # exist and resolve to exactly the commit HEAD was on before the reset,
    # never to origin/main.
    rescue_sha = _git(
        repo, "rev-parse", f"rescue/recycled-{foreign_sha_short}"
    ).stdout.strip()
    assert rescue_sha == foreign_sha_full
    assert rescue_sha != origin_trunk_sha
    assert not (wt / "leftover.tmp").exists()
    assert not (wt / "foreign.txt").exists()  # origin/main never had this file


def test_contaminated_worktree_resets_onto_origin_trunk_not_unpushed_local_residue(
    tmp_path: Path,
) -> None:
    """proj-isl3 failure mode 1 (the reset-onto-residue hole): a genuinely
    recycled worktree must be reset onto `origin/main`, not onto local
    `main` while a `/land` pass has it carrying un-pushed, un-gated merges
    of OTHER tickets. Resetting onto that residue would plant foreign,
    un-gated commits into this build -- the exact contamination this guard
    exists to prevent, arriving THROUGH the guard. This test would go red if
    the script's remedy reverted to bare `git reset --hard main`."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(
        repo,
        ".claude/worktrees/recycled-during-land",
        "worktree-agent-other-ticket-2",
        foreign_commit=True,
    )
    origin_trunk_sha_before = _git(repo, "rev-parse", "origin/main").stdout.strip()
    # A live /land pass merges some OTHER ticket's branch into local main
    # but has not yet reached its Section 4 push -- the residue window.
    _advance_local_trunk_without_pushing(
        repo, "residue.txt", "unpushed --no-ff merge of some other ticket"
    )
    local_trunk_sha = _git(repo, "rev-parse", "main").stdout.strip()
    assert local_trunk_sha != origin_trunk_sha_before  # the window is open

    result = _run(wt)

    assert result.returncode == 0, result.stdout + result.stderr
    reset_head = _git(wt, "rev-parse", "HEAD").stdout.strip()
    assert reset_head == origin_trunk_sha_before  # reset onto origin/main...
    assert reset_head != local_trunk_sha  # ...never onto the un-pushed residue
    assert not (
        wt / "residue.txt"
    ).exists()  # the other ticket's commit never landed here


def test_worktree_recycled_onto_land_branch_merged_but_not_pushed_is_still_caught(
    tmp_path: Path,
) -> None:
    """proj-isl3 failure mode 2 (the false-negative hole): a worktree
    recycled onto a `land/<id>` branch that a live `/land` pass has already
    merged into LOCAL main, but not yet pushed, must still be flagged as
    contaminated -- `origin/main` has not advanced yet, so HEAD is not an
    ancestor of it. Under the old bare-`main` predicate this passed
    trivially (silent, no rescue, no reset) the moment local main absorbed
    the merge; this test would go red if the predicate reverted to bare
    `main`."""
    repo = _init_repo(tmp_path)
    # A previous ticket's build, pushed to origin as land/proj-x -- built in
    # its OWN worktree (not the main checkout: git refuses to have the same
    # branch checked out in two worktrees at once, and the main checkout
    # must stay on main throughout, exactly like the real /land pass this
    # models).
    build_wt = _add_worktree(
        repo, ".claude/worktrees/proj-x-build", "land/proj-x", foreign_commit=True
    )
    _git(build_wt, "push", "-q", "origin", "land/proj-x")
    land_x_sha = _git(build_wt, "rev-parse", "land/proj-x").stdout.strip()
    land_x_sha_short = _git(build_wt, "rev-parse", "--short", "HEAD").stdout.strip()
    _git(repo, "worktree", "remove", "--force", str(build_wt))

    # The harness recycles a launch worktree still checked out on land/proj-x.
    wt = repo / ".claude/worktrees/recycled-onto-land-branch"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", str(wt), "land/proj-x")

    origin_trunk_sha_before = _git(repo, "rev-parse", "origin/main").stdout.strip()

    # A live /land pass merges land/proj-x into local main -- but has not
    # yet reached its Section 4 push. origin/main has NOT moved.
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge land/proj-x", "land/proj-x")
    assert (
        _git(repo, "rev-parse", "origin/main").stdout.strip() == origin_trunk_sha_before
    )

    result = _run(wt)

    assert result.returncode == 0, result.stdout + result.stderr
    # Must have been flagged and repaired -- NOT a silent no-op.
    assert "CONTAMINATED LAUNCH WORKTREE" in result.stderr
    reset_head = _git(wt, "rev-parse", "HEAD").stdout.strip()
    assert reset_head == origin_trunk_sha_before
    rescue_sha = _git(
        repo, "rev-parse", f"rescue/recycled-{land_x_sha_short}"
    ).stdout.strip()
    assert rescue_sha == land_x_sha


def test_reported_reason_names_and_the_context_message(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    wt = _add_worktree(
        repo,
        ".claude/worktrees/recycled2",
        "worktree-agent-other-ticket-3",
        foreign_commit=True,
    )

    result = _run(wt, context="before my own fetch+checkout")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "CONTAMINATED LAUNCH WORKTREE" in result.stderr
    assert "CONTAMINATED LAUNCH WORKTREE" in result.stderr
    assert "before my own fetch+checkout" in result.stderr


def test_outside_isolated_worktree_refuses_and_leaves_everything_untouched(
    tmp_path: Path,
) -> None:
    """A recycled/contaminated checkout OUTSIDE `.claude/worktrees/` must be
    refused, not repaired -- this is the guard that keeps a failed
    `isolation: "worktree"` dispatch from ever reaching the main checkout
    with `reset --hard`/`clean -fd`."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(
        repo, "not-a-launch-worktree", "worktree-agent-elsewhere", foreign_commit=True
    )
    head_before = _git(wt, "rev-parse", "HEAD").stdout.strip()

    result = _run(wt)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "refusing to reset" in result.stderr
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == head_before
    branches = _git(repo, "branch", "--list", "rescue/*").stdout
    assert branches.strip() == ""


@pytest.mark.parametrize(
    "near_miss_dir",
    [".claude/worktrees-stale/agent-abc123", "x.claude/worktrees/agent-abc123"],
    ids=["trailing-anchor", "leading-anchor"],
)
def test_a_near_miss_directory_name_is_refused_not_repaired(
    tmp_path: Path, near_miss_dir: str
) -> None:
    """The `case` glob must match the literal path SEGMENT `.claude/worktrees/`
    -- BOTH its `/` anchors, not merely the substring between them. One
    parameter per anchor, because each is the sole catcher of its own mutation:

    - `.claude/worktrees-stale/...` pins the TRAILING `/` (the segment is
      `worktrees-stale`, not `worktrees`). Relaxing the glob to
      `*/.claude/worktrees*` -- or as far as `*/.claude/*` -- leaves every
      other test in this module green.
    - `x.claude/worktrees/...` pins the LEADING `/` (the segment is
      `x.claude`, not `.claude`). Deleting the leading `*/` outright is caught
      by the other tests here (they stop matching at all), but merely WEAKENING
      it to `*` -- `*.claude/worktrees/*` -- leaves the entire rest of the
      module green, so nothing but this parameter holds that anchor down.

    All sabotage-verified. The contamination is deliberate
    (`foreign_commit=True`): with the glob relaxed, this exact fixture does not
    merely mis-classify -- it reaches `git reset --hard` and rewinds the
    near-miss worktree's HEAD, so the refusal is proven precisely where the
    destructive remediation would otherwise be warranted.

    Ported from tests/test_isolation_guard.py's twin pin, which gained the
    leading-anchor parameter in the same change (proj-v12j).
    """
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, near_miss_dir, "worktree-agent-stale", foreign_commit=True)
    head_before = _git(wt, "rev-parse", "HEAD").stdout.strip()

    result = _run(wt)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "refusing to reset" in result.stderr
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == head_before
    branches = _git(repo, "branch", "--list", "rescue/*").stdout
    assert branches.strip() == ""


def test_dirt_axis_gap_closed_worktree_recycled_onto_an_already_landed_branch_still_gets_cleaned(
    tmp_path: Path,
) -> None:
    """Pins the proj-3v1p fix: a worktree recycled onto a `land/<id>` branch
    that has SINCE LANDED (merged into main AND pushed to origin -- a
    completed land, not merely an in-flight one) has a HEAD that is already
    an ancestor of `origin/main`, so the ancestor check trivially passes and
    the reset/rescue-branch remediation never runs -- exactly as it would for
    a genuinely fresh worktree (harmless on the ancestry axis; matches
    `/land`'s own reclaim predicate). But `git clean -fd` now runs
    unconditionally right after the check either way, so any untracked
    leftover from that prior build is still swept -- this test exists so
    nobody silently reintroduces the old dirt-axis gap by moving `clean -fd`
    back inside the `if` block."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, ".claude/worktrees/landed", "worktree-agent-landed-ticket")
    # Simulate that ticket's branch having FULLY landed: fast-forward local
    # main to it AND push -- origin/main advances too.
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--ff-only", "worktree-agent-landed-ticket")
    _push_trunk(repo)
    (wt / "leftover.tmp").write_text("dirt that must not survive the ancestry check\n")
    head_before = _git(wt, "rev-parse", "HEAD").stdout.strip()

    result = _run(wt)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""  # no CONTAMINATED report: no reset/rescue happened
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == head_before  # not reset
    branches = _git(repo, "branch", "--list", "rescue/*").stdout
    assert branches.strip() == ""  # no rescue branch created
    assert not (wt / "leftover.tmp").exists()  # dirt-axis gap closed (proj-3v1p)
