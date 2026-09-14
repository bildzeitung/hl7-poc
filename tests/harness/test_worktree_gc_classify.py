"""Tests for scripts/worktree-gc-classify.sh (proj-9owc).

`/land`'s Section 4 worktree-GC backstop sweep decides what to do with each
candidate under `.claude/worktrees/` via a per-candidate predicate that --
before this ticket -- lived only as inline bash in a markdown fence, reachable
by no gate at all. The script's own header carries WHY it was extracted and
what each bucket means; that is deliberately not retold here.

The SWEEP around it has since been extracted the same way, into
`scripts/worktree-gc-sweep.sh` (proj-s9xe.5, tested in
`tests/test_worktree_gc_sweep.py`) -- that script, not the surviving
`.claude/skills/land/SKILL.md` fence, is what the bucket-vocabulary test at
the bottom of this module now reads. The fence is deleted by the call-site
ticket that wires the sweep in.

What matters to the tests: the script only ever PRINTS a bucket name -- it
never removes a worktree or deletes a branch -- so every fixture below only
has to assert the printed bucket, never inspect git state afterward for
something the script itself never touches.

All tests run the ACTUAL `scripts/worktree-gc-classify.sh` against real git
repositories (with a real `origin` remote and real `git worktree add`
checkouts) built in `tmp_path` -- no fake git, no mocked subprocess --
sabotage-provable per the proj-verb bar: reverting the script's captured-arm
ancestor test, its dirty-tree exclude list, its age floor, or its branch-shape
`case` directly would turn the corresponding test here red.

Fixture matrix (from the ticket's own acceptance criteria) and its bucket:

  1. clean + old + worktree-agent-*, not merged  -> dir-only  (ref kept)
  2. dirty, otherwise full-reclaim-eligible       -> keep-dirty
  3. clean but too young, worktree-agent-*        -> keep-notmerged
  4. detached HEAD (empty branch), not merged     -> keep-notmerged
  5. land/-branched, not merged, not captured     -> keep-notmerged
  6. live (non-stale) lock                        -> keep-locked
  7. "stale lock, unlocked then classified"       -> classified on the merits
     (staleness itself is scripts/worktree-lock-stale.sh's job, already
     covered by tests/test_worktree_lock_stale.py -- this script never judges
     staleness; SKILL.md's loop resolves it and unlocks BEFORE calling this
     script, passing locked=0. This case pins that once locked=0 is passed,
     classification proceeds normally rather than getting stuck at
     keep-locked.)
  8. a vanished worktree directory                -> keep-dirty (fail CLOSED,
     never treated as eligible for `--force`)

Plus the two positive buckets (full-reclaim via each ancestry arm), the
proj-em6v worktree-uniqueness-suffix strip on the origin arm, and a usage-
error pin.

Two tests do not touch git at all: they pin that the sweep script's `case "$BUCKET"`
dispatch handles exactly the buckets this script can print. That coupling is
the one part of the decision path the extraction did NOT make testable on its
own -- a rename on either side fails safe into `*)` and silently stops GC
while everything stays green.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from _gitrepo import _git
from conftest import REPO_ROOT

#: The GC loop moved out of land/SKILL.md into this script; the bucket contract
#: it must handle is the classifier's, wherever the loop lives.
SWEEP_TEXT = (REPO_ROOT / "scripts" / "worktree-gc-sweep.sh").read_text()

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "worktree-gc-classify.sh"


def _init_repo(tmp_path: Path) -> Path:
    """A throwaway repo with a real `origin` remote and one commit on
    `main`, pushed -- the classify script reads both `main` and
    `origin/<branch>` refs directly, so both must be real."""
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


def _add_worktree(
    repo: Path, rel_path: str, branch: str, base: str = "origin/main"
) -> Path:
    """A real `git worktree add`, branched off `base` (default `origin/main`,
    matching `worktree.baseRef: "fresh"`)."""
    wt = repo / rel_path
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", str(wt), "-b", branch, base)
    return wt


def _commit(
    wt: Path, filename: str, message: str, committer_date: str | None = None
) -> str:
    """`committer_date` forges the committer timestamp the script reads via
    `git log -1 --format=%ct`. It goes through a direct `subprocess.run`
    because `_gitrepo._git` accepts no env; everything else stays on `_git`."""
    (wt / filename).write_text(f"{message}\n")
    _git(wt, "add", filename)
    if committer_date is None:
        _git(wt, "commit", "-q", "-m", message)
    else:
        result = subprocess.run(
            ["git", "commit", "-q", "-m", message],
            cwd=wt,
            env={**os.environ, "GIT_COMMITTER_DATE": committer_date},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, f"git commit failed: {result.stderr}"
    return _git(wt, "rev-parse", "HEAD").stdout.strip()


def _run(
    repo: Path,
    wt: Path,
    sha: str,
    locked: str,
    branch: str,
    min_age_seconds: str = "21600",
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), str(wt), sha, locked, branch, min_age_seconds],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _bucket(result: subprocess.CompletedProcess) -> str:
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def _is_ancestor(repo: Path, sha: str, ref: str) -> bool:
    """`git merge-base --is-ancestor` legitimately exits 1 for "not an
    ancestor" -- unlike every other git call in this module, that is not a
    fixture-setup failure, so this bypasses `_gitrepo._git`'s own
    `returncode == 0` assertion rather than tripping it on the expected-false
    case."""
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, ref],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.returncode == 0


# --- the two sides of the bucket vocabulary agree ------------------------


def _buckets_the_script_can_print() -> set[str]:
    """Every bucket name `worktree-gc-classify.sh` actually echoes.

    Derived from the script, never restated here -- a hand-typed expectation
    would just be a third copy of the vocabulary this test exists to keep at
    two. The usage line is excluded by the `$` anchor: it carries a trailing
    `>&2`.
    """
    return set(
        re.findall(
            r'^\s*echo "([a-z][a-z-]*)"$',
            SCRIPT.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    )


def _buckets_the_land_loop_handles() -> set[str]:
    """Every non-default arm label of the sweep script's `case "$BUCKET"` dispatch."""
    text = SWEEP_TEXT
    start = text.index('case "$BUCKET" in')
    end = text.index("\n  esac", start)
    return set(re.findall(r"^\s*([a-z][a-z-]*)\)", text[start:end], re.MULTILINE))


def test_land_loop_handles_exactly_the_buckets_the_script_prints() -> None:
    """`/land`'s loop maps each bucket to a counter and a (possibly
    DESTRUCTIVE) action by matching the script's output as a literal string,
    in a markdown fence that no other gate reads. Nothing else links the two
    vocabularies, so a rename or typo on either side is caught by nothing:
    the loop would fall to its `*)` arm for every candidate and count the
    whole sweep as `failed`, silently zeroing out worktree GC. That fails
    SAFE (no worktree is touched), which is exactly why it could run
    unnoticed -- `/land` stays green and the leak just grows.

    This is the same residual the extraction itself exists to close, one
    level up: the predicate is now unit-tested, but the DISPATCH on its
    result was still ungated. Both sides are derived, so this test cannot
    drift into agreeing with itself.
    """
    assert _buckets_the_script_can_print() == _buckets_the_land_loop_handles()


def test_the_bucket_vocabulary_is_the_documented_five() -> None:
    """Pins the vocabulary itself, so the agreement test above cannot be
    satisfied by BOTH sides losing the same bucket -- e.g. `dir-only`
    disappearing from the script and the loop together would leave proj-yrtu's
    whole reclaim arm dead while both tests stayed green."""
    assert _buckets_the_script_can_print() == {
        "full-reclaim",
        "dir-only",
        "keep-locked",
        "keep-notmerged",
        "keep-dirty",
    }


# --- usage --------------------------------------------------------------


def test_wrong_argument_count_exits_2(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    result = subprocess.run(
        ["bash", str(SCRIPT), "one", "two", "three"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "usage" in result.stderr


# --- fixture 6: live (non-stale) lock ------------------------------------


def test_locked_is_kept_regardless_of_ancestry_or_dirt(tmp_path: Path) -> None:
    """locked="1" must short-circuit to keep-locked even for a candidate that
    would otherwise be a clean, merged full-reclaim -- `/land`'s loop only
    ever passes locked="1" for a lock scripts/worktree-lock-stale.sh could NOT
    prove dead, and this script must never second-guess that."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, ".claude/worktrees/locked", "worktree-agent-locked")
    sha = _git(
        wt, "rev-parse", "HEAD"
    ).stdout.strip()  # == main tip: trivially "merged"

    result = _run(repo, wt, sha, "1", "worktree-agent-locked")

    assert _bucket(result) == "keep-locked"


# --- fixture 7: "stale lock, unlocked then classified" -------------------


def test_locked_zero_after_stale_resolution_is_classified_on_the_merits(
    tmp_path: Path,
) -> None:
    """This script performs no staleness check itself -- SKILL.md's loop
    resolves a stale lock (via scripts/worktree-lock-stale.sh) and calls
    `git worktree unlock` BEFORE calling this script, passing locked="0" once
    resolved. Pins that once locked="0" is passed, the candidate is judged
    normally rather than getting stuck at keep-locked -- i.e. staleness
    resolution and classification compose correctly across the two scripts."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, ".claude/worktrees/was-stale", "worktree-agent-wasstale")
    sha = _git(wt, "rev-parse", "HEAD").stdout.strip()

    result = _run(repo, wt, sha, "0", "worktree-agent-wasstale")

    assert _bucket(result) == "full-reclaim"


# --- full-reclaim: both ancestry arms ------------------------------------


def test_merged_into_trunk_and_clean_is_full_reclaim(tmp_path: Path) -> None:
    """The core positive case: a builder's worktree whose branch has since
    been merged into `main` (not merely sitting at zero divergence from it)
    and whose tree is clean."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, ".claude/worktrees/merged", "worktree-agent-merged")
    sha = _commit(wt, "done.txt", "builder work")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge it", "worktree-agent-merged")
    assert _is_ancestor(repo, sha, "main")

    result = _run(repo, wt, sha, "0", "worktree-agent-merged")

    assert _bucket(result) == "full-reclaim"


def test_captured_on_origin_counterpart_not_merged_into_trunk_is_full_reclaim(
    tmp_path: Path,
) -> None:
    """proj-amif's widened arm: a reviewer/rebase-pickup worktree whose branch
    is pushed to `origin/land/<id>` but never merges into `main` (an
    escalated ticket, by definition) is still captured, and therefore
    reclaimable, once its HEAD is an ancestor of that origin ref."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, ".claude/worktrees/land-x", "land/proj-x")
    sha = _commit(wt, "review.txt", "reviewer work")
    _git(wt, "push", "-q", "origin", "land/proj-x")
    # Not merged into main at all.
    assert not _is_ancestor(repo, sha, "main")

    result = _run(repo, wt, sha, "0", "land/proj-x")

    assert _bucket(result) == "full-reclaim"


def test_worktree_uniqueness_suffix_is_stripped_before_the_origin_lookup(
    tmp_path: Path,
) -> None:
    """proj-em6v: a reviewer/rebase-pickup checks a branch out locally as
    `land/<id>--<worktree-dir>`, never the bare `land/<id>` origin uses. The
    script must strip everything from the first `--` before comparing against
    `origin/<branch>`, or this arm is permanently dead for every such
    worktree."""
    repo = _init_repo(tmp_path)
    build_wt = _add_worktree(repo, ".claude/worktrees/land-y-build", "land/proj-y")
    sha = _commit(build_wt, "review.txt", "reviewer work")
    _git(build_wt, "push", "-q", "origin", "land/proj-y")

    # A SEPARATE worktree, same commit, checked out under the SUFFIXED local
    # name a reviewer/pickup would actually use.
    wt = _add_worktree(
        repo, ".claude/worktrees/land-y-suffixed", "land/proj-y--agent-abc123", base=sha
    )

    result = _run(repo, wt, sha, "0", "land/proj-y--agent-abc123")

    assert _bucket(result) == "full-reclaim"


# --- fixture 2: dirty ------------------------------------------------------


def test_dirty_captured_worktree_is_kept(tmp_path: Path) -> None:
    """Otherwise full-reclaim-eligible (merged, clean ancestry) but the tree
    itself carries an uncommitted change -- proj-9hgu's dirty-tree guard must
    keep it regardless of ancestry."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, ".claude/worktrees/dirty", "worktree-agent-dirty")
    sha = _commit(wt, "done.txt", "builder work")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge it", "worktree-agent-dirty")
    (wt / "scratch.tmp").write_text("uncommitted\n")

    result = _run(repo, wt, sha, "0", "worktree-agent-dirty")

    assert _bucket(result) == "keep-dirty"


def test_bd_export_churn_alone_does_not_count_as_dirty(tmp_path: Path) -> None:
    """proj-bns3: `.beads/issues.jsonl`/`.beads/interactions.jsonl` are
    EXCLUDED from the dirty judgment -- the passive bd export is, by
    invariant, never real work (import.auto: false, proj-6ra), so a modified
    export alone must not zero out an otherwise-clean full-reclaim."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, ".claude/worktrees/bd-churn", "worktree-agent-bdchurn")
    sha = _commit(wt, "done.txt", "builder work")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge it", "worktree-agent-bdchurn")
    beads_dir = wt / ".beads"
    beads_dir.mkdir()
    (beads_dir / "issues.jsonl").write_text('{"id": "proj-1"}\n')

    result = _run(repo, wt, sha, "0", "worktree-agent-bdchurn")

    assert _bucket(result) == "full-reclaim"


# --- fixture 8: vanished directory -----------------------------------------


def test_vanished_directory_fails_closed_to_keep_dirty(tmp_path: Path) -> None:
    """A worktree whose DIRECTORY has vanished from disk (e.g. removed by
    some other path) but whose `git worktree list --porcelain` entry the
    caller is still iterating over -- `git -C <path> status --porcelain`
    itself errors, and the script must treat that as "could not prove clean"
    (keep-dirty), never as license to proceed toward `--force`."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, ".claude/worktrees/vanishing", "worktree-agent-vanishing")
    sha = _commit(wt, "done.txt", "builder work")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge it", "worktree-agent-vanishing")
    shutil.rmtree(wt)  # bypass git's own bookkeeping -- the dir is just gone

    result = _run(repo, wt, sha, "0", "worktree-agent-vanishing")

    assert _bucket(result) == "keep-dirty"


# --- fixture 1 & 3: worktree-agent-* dir-only reclaim + age floor ---------


def test_worktree_agent_not_merged_clean_and_old_enough_is_dir_only(
    tmp_path: Path,
) -> None:
    """A builder's own branch, never pushed anywhere, not merged into main,
    but old enough (min-age-seconds trivially satisfied) and clean ->
    dir-only. The script never touches the branch ref itself -- it only
    prints the bucket -- so there is nothing to assert about the ref here;
    SKILL.md's own case arm is what keeps it.

    proj-ej6u originally widened this floor to `-3600` because under
    `pytest -n 8` a `date +%s` read on one vCPU could transiently observe
    CLOCK_REALTIME a few seconds BEHIND a just-committed `git commit`'s
    recorded timestamp read on another core, making the script's
    `now - last_commit_ts` negative and failing even a `-ge 0` floor (same
    backward-wall-clock class as proj-0dnk and proj-44cq). proj-o7rt fixed the
    real cause instead: the script now clamps a negative age to 0 before the
    `-ge` comparison, so with a `min_age_seconds` floor of `0` the comparison
    is `age >= 0`, which holds unconditionally regardless of clock skew.
    Restored to `0` (proj-d7xw) to exercise the real boundary rather than a
    workaround-widened one; the too-young sibling below still pins the other
    side of the floor."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, ".claude/worktrees/leaked", "worktree-agent-leaked")
    sha = _commit(wt, "wip.txt", "abandoned build")
    assert not _is_ancestor(repo, sha, "main")

    result = _run(repo, wt, sha, "0", "worktree-agent-leaked", min_age_seconds="0")

    assert _bucket(result) == "dir-only"


def test_worktree_agent_future_dated_commit_with_zero_floor_is_dir_only(
    tmp_path: Path,
) -> None:
    """proj-o7rt: a committer timestamp AHEAD of the classifying process's own
    `date +%s` makes `now - last_commit_ts` negative, and an unclamped
    negative age failed even a `min_age_seconds=0` floor -- the
    `keep-notmerged` misclassification behind
    `test_dir_only_reclaim_removes_the_directory_but_keeps_the_ref`'s flake
    under `-n 8`. The script's own comment carries why the clock can step
    backward; what only this test can say is that it FORGES the date rather
    than racing a real clock, so reverting the clamp turns it red on every
    run instead of occasionally.
    """
    repo = _init_repo(tmp_path)
    wt = _add_worktree(
        repo, ".claude/worktrees/future-dated", "worktree-agent-futuredated"
    )
    sha = _commit(
        wt, "wip.txt", "abandoned build", committer_date=str(int(time.time()) + 3600)
    )

    result = _run(repo, wt, sha, "0", "worktree-agent-futuredated", min_age_seconds="0")

    assert _bucket(result) == "dir-only"


def test_worktree_agent_not_merged_but_too_young_is_kept_notmerged(
    tmp_path: Path,
) -> None:
    """Same shape as above, but the age floor is set far in the future
    relative to the commit -- a build still cycling has a recent HEAD commit
    almost by construction, so this must fail SAFE and keep it."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(
        repo, ".claude/worktrees/fresh-build", "worktree-agent-freshbuild"
    )
    sha = _commit(wt, "wip.txt", "still building")

    result = _run(
        repo, wt, sha, "0", "worktree-agent-freshbuild", min_age_seconds="999999999"
    )

    assert _bucket(result) == "keep-notmerged"


def test_worktree_agent_not_merged_dirty_and_old_enough_is_kept_dirty(
    tmp_path: Path,
) -> None:
    """The dir-only arm gates on the SAME dirty-tree guard as full-reclaim --
    old enough and worktree-agent-shaped is not sufficient by itself.

    proj-ej6u originally widened the floor to `-3600` for the same reason as
    the clean/old-enough sibling above; proj-o7rt's clamp makes `0` the
    deterministic floor here too, and it is restored (proj-d7xw) for the same
    reason -- see that sibling's docstring for the mechanism in full."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(
        repo, ".claude/worktrees/leaked-dirty", "worktree-agent-leakeddirty"
    )
    sha = _commit(wt, "wip.txt", "abandoned build")
    (wt / "scratch.tmp").write_text("uncommitted\n")

    result = _run(repo, wt, sha, "0", "worktree-agent-leakeddirty", min_age_seconds="0")

    assert _bucket(result) == "keep-dirty"


# --- fixture 4: detached HEAD ----------------------------------------------


def test_empty_branch_never_matches_the_worktree_agent_glob(tmp_path: Path) -> None:
    """A DETACHED worktree's branch is always "" in the porcelain SKILL.md's
    loop parses. The `case "$br" in worktree-agent-*)` pattern must never
    match an empty string -- if it did, a detached worktree would wrongly
    enter the dir-only arm instead of falling to the unconditional
    keep-notmerged default."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, ".claude/worktrees/detached", "worktree-agent-detachedsrc")
    sha = _commit(wt, "wip.txt", "some work")
    assert not _is_ancestor(repo, sha, "main")

    # Even with an aggressively permissive age floor, an EMPTY branch name
    # must still fall to keep-notmerged, never dir-only.
    result = _run(repo, wt, sha, "0", "", min_age_seconds="0")

    assert _bucket(result) == "keep-notmerged"


# --- fixture 5: land/-branched, not captured -------------------------------


def test_land_branched_not_merged_and_never_pushed_is_kept_notmerged(
    tmp_path: Path,
) -> None:
    """A `land/<id>`-branched worktree that is neither merged into main nor
    captured on any origin counterpart (never pushed) -- kept, full stop; this
    shape never enters the worktree-agent-* dir-only arm regardless of age."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, ".claude/worktrees/land-unpushed", "land/proj-unpushed")
    sha = _commit(wt, "wip.txt", "reviewer work, never pushed")
    assert not _is_ancestor(repo, sha, "main")

    result = _run(repo, wt, sha, "0", "land/proj-unpushed", min_age_seconds="0")

    assert _bucket(result) == "keep-notmerged"
