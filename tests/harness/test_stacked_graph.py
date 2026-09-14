"""scripts/stacked-graph.sh -- stacked land-branch detection, on real git repos.

This logic used to live as bash inside .claude/skills/land/SKILL.md, where it did
not parse (its outer loop was a comment) and could not be tested at all. Every
case below is a repo built by `_repo()` and measured, not a string match against
markdown.

The cases that matter are the ones the skill's prose warns about and that a
plausible reimplementation gets wrong:

  * a base whose tip MOVED after the dependent merged it (tip-ancestry fails here)
  * a pair with TWO merge-bases, one of which is on the default branch
    (single-result `git merge-base` picks arbitrarily and misses the stack)
  * SIBLINGS on a common base (related, but no edge is the correct answer)
  * transitive stacks, where "nearest base" is what /land must diff against
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "stacked-graph.sh"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)


def _repo(tmp_path: Path) -> Path:
    """A repo with an `origin/main` and an `origin/land/*` namespace.

    Uses a real second repo as `origin` rather than faking remote refs: the
    script reads `refs/remotes/origin/land/*`, and a fixture that hand-writes
    those refs would not prove the ref discovery works.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main", ".")
    _git(origin, "config", "user.email", "t@t")
    _git(origin, "config", "user.name", "t")
    # One case pushes `main` itself. A non-bare origin refuses a push to its own
    # checked-out branch; origin's worktree is never read here, only its refs.
    _git(origin, "config", "receive.denyCurrentBranch", "ignore")
    _commit(origin, "base")

    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    return work


def _branch(repo: Path, name: str, start: str) -> None:
    _git(repo, "checkout", "-q", "-B", name, start)


def _publish(repo: Path, *branches: str) -> None:
    _git(repo, "push", "-q", "origin", *[f"{b}:{b}" for b in branches])
    _git(repo, "fetch", "-q", "origin")


def _run(repo: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), "--base-ref", "origin/main", *extra],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def _edges(result: subprocess.CompletedProcess[str]) -> set[tuple[str, str, str]]:
    return {
        tuple(line.split("\t")[1:])  # (dependent, base, kind)
        for line in result.stdout.splitlines()
        if line.startswith("EDGE\t")
    }


def test_no_land_branches_is_a_successful_empty_run(tmp_path: Path) -> None:
    r = _run(_repo(tmp_path))
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""


def test_two_independent_branches_are_not_related(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for name in ("land/a", "land/b"):
        _branch(repo, name, "origin/main")
        _commit(repo, name.replace("/", "_"))
    _publish(repo, "land/a", "land/b")
    r = _run(repo)
    assert r.returncode == 0, r.stderr
    assert _edges(r) == set()


def test_simple_stack_is_detected_with_direction(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _branch(repo, "land/base", "origin/main")
    _commit(repo, "base_work")
    _branch(repo, "land/dep", "origin/main")
    _commit(repo, "dep_work")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge base", "land/base")
    _publish(repo, "land/base", "land/dep")

    assert _edges(_run(repo)) == {("dep", "base", "direct")}


def test_stack_survives_the_base_tip_moving_afterwards(tmp_path: Path) -> None:
    """The case tip-ancestry gets wrong, and the normal flow rather than a corner.

    A base keeps moving while unlanded -- its reviewer pushes fixes. After that,
    the base's tip is no longer an ancestor of the dependent, so a
    `merge-base --is-ancestor <base> <dep>` test reports "unrelated" and the
    whole stack goes invisible.
    """
    repo = _repo(tmp_path)
    _branch(repo, "land/base", "origin/main")
    _commit(repo, "base_work")
    _branch(repo, "land/dep", "origin/main")
    _commit(repo, "dep_work")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge base", "land/base")
    _branch(repo, "land/base", "land/base")
    _commit(repo, "base_review_fix")  # the base tip moves
    _publish(repo, "land/base", "land/dep")

    # Precondition: the naive tip test really does fail here, so this fixture
    # is exercising what it claims to.
    naive = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "origin/land/base", "origin/land/dep"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    assert naive.returncode != 0, "fixture no longer reproduces the moved-tip case"

    assert _edges(_run(repo)) == {("dep", "base", "direct")}


def test_second_on_branch_merge_base_does_not_hide_the_stack(tmp_path: Path) -> None:
    """Two merge-bases, one ON the default branch -- single-result merge-base
    returns one arbitrarily, and picking that one reads as 'unrelated'."""
    repo = _repo(tmp_path)
    # base is cut EARLY; main then advances; dep is cut from that LATER point.
    # That ordering is what makes the two merge-bases incomparable: the base's
    # own commit and dep's cut point neither contains the other.
    _branch(repo, "land/base", "origin/main")
    _commit(repo, "base_work")

    _git(repo, "checkout", "-q", "main")
    _commit(repo, "main_moved")
    _git(repo, "push", "-q", "origin", "main")

    _branch(repo, "land/dep", "main")
    _commit(repo, "dep_work")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge base", "land/base")

    # The BASE then takes a needs-rebase pickup of main, AFTER dep merged it.
    _branch(repo, "land/base", "land/base")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge main", "main")
    _publish(repo, "land/base", "land/dep")

    n = len(
        _git(repo, "merge-base", "--all", "origin/land/base", "origin/land/dep").split()
    )
    assert n >= 2, f"fixture no longer produces multiple merge-bases (got {n})"

    assert _edges(_run(repo)) == {("dep", "base", "direct")}


def test_siblings_on_a_common_base_get_no_edge_between_them(tmp_path: Path) -> None:
    """Related (they share the base's commits) but neither is stacked on the
    other. No edge between them is the CORRECT answer, not a miss."""
    repo = _repo(tmp_path)
    _branch(repo, "land/base", "origin/main")
    _commit(repo, "base_work")
    for dep in ("land/d1", "land/d2"):
        _branch(repo, dep, "origin/main")
        _commit(repo, dep.replace("/", "_"))
        _git(repo, "merge", "-q", "--no-ff", "-m", "merge base", "land/base")
    _publish(repo, "land/base", "land/d1", "land/d2")

    edges = _edges(_run(repo))
    assert ("d1", "d2", "direct") not in edges and ("d2", "d1", "direct") not in edges
    assert edges == {("d1", "base", "direct"), ("d2", "base", "direct")}


def test_transitive_stack_marks_only_the_nearest_base_direct(tmp_path: Path) -> None:
    """a <- b <- c. /land diffs c against b, never against a: handing it the
    transitive base would carry b's work into c's scope."""
    repo = _repo(tmp_path)
    _branch(repo, "land/a", "origin/main")
    _commit(repo, "a_work")
    _branch(repo, "land/b", "origin/main")
    _commit(repo, "b_work")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge a", "land/a")
    _branch(repo, "land/c", "origin/main")
    _commit(repo, "c_work")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge b", "land/b")
    _publish(repo, "land/a", "land/b", "land/c")

    edges = _edges(_run(repo))
    assert ("b", "a", "direct") in edges
    assert ("c", "b", "direct") in edges
    assert ("c", "a", "transitive") in edges, (
        "full relation must still contain the transitive edge"
    )
    assert ("c", "a", "direct") not in edges


def test_branched_from_base_is_reported_unordered_not_guessed(tmp_path: Path) -> None:
    """Documented gap 2: branching directly off the base puts the shared commit
    on BOTH first-parent spines, so no direction exists. The pair must surface
    as related-but-unordered rather than being given an invented direction."""
    repo = _repo(tmp_path)
    _branch(repo, "land/base", "origin/main")
    _commit(repo, "base_work")
    _branch(repo, "land/dep", "land/base")  # branched off, not merged in
    _commit(repo, "dep_work")
    _publish(repo, "land/base", "land/dep")

    r = _run(repo, "--report-unordered")
    assert r.returncode == 0, r.stderr
    assert _edges(r) == set(), "no direction may be invented for this shape"
    assert "UNORDERED\tbase\tdep" in r.stdout


def test_directed_stack_is_never_also_reported_unordered(tmp_path: Path) -> None:
    """Direction is only found in the ordering that puts the BASE first, but
    UNORDERED pairs are collected in the x < y ordering. When the base's id
    sorts AFTER the dependent's, those two orderings differ, and a genuinely
    stacked pair used to be emitted as an EDGE *and* as UNORDERED -- a false
    ambiguity on roughly half of all real stacks, since ids sort arbitrarily.

    Every other case in this file happens to name its base so it sorts first,
    which is exactly why this went unnoticed; here the base is `land/zzz`.
    """
    repo = _repo(tmp_path)
    _branch(repo, "land/zzz", "origin/main")
    _commit(repo, "zzz_work")
    _branch(repo, "land/aaa", "origin/main")
    _commit(repo, "aaa_work")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge zzz", "land/zzz")
    _publish(repo, "land/zzz", "land/aaa")

    r = _run(repo, "--report-unordered")
    assert r.returncode == 0, r.stderr
    assert _edges(r) == {("aaa", "zzz", "direct")}
    assert "UNORDERED" not in r.stdout, r.stdout


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        (["--base-ref", "origin/does-not-exist"], "does not resolve"),
        (["--bogus-flag"], "unknown argument"),
    ],
)
def test_machine_faults_exit_2_and_never_read_as_no_stacks(
    tmp_path: Path, args: list[str], needle: str
) -> None:
    """A query that could not run must not be indistinguishable from 'no stacks'
    -- that conflation is what would let /land merge a dependent before its base."""
    repo = _repo(tmp_path)
    r = subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 2, r.stdout
    assert needle in r.stderr
    assert r.stdout == ""
