"""Tests for scripts/merge-precheck.sh (proj-mh9g).

`/land` Section 2b (`.claude/skills/land/SKILL.md`) runs a cheap trial merge
of every `ready-for-land` branch against `main` before spending Opus on the
semantic review -- a branch that no longer merges cleanly is kicked back
`needs-rebase` without judging its content. This script extracts that
precheck out of the inline shell+`git merge-tree` snippet the skill used to
embed directly, which had two live defects (both found landing proj-l38d.6,
2026-07-17, full writeup in the script's own header comment):

DEFECT 1 -- the inline snippet's `tail -n +2` captured `git merge-tree
--write-tree --name-only`'s tree-OID line, the conflicting path(s), a BLANK
LINE, *and* the "Auto-merging"/"CONFLICT" chatter that follows -- including
one "Auto-merging <path>" line per file that merged CLEAN. So a file that
never conflicted got reported as though it had.
`test_third_file_merging_clean_is_excluded` below pins this exactly in the
proj-l38d.6 shape (a real conflict on one file, a second file that merges
clean) and is the test defect 1's fix must not regress.

DEFECT 2 -- `git merge-tree --write-tree` exits 0=clean, 1=conflict, and
something else on failure per its own man page. The inline snippet's
`if MT=$(git merge-tree ...); then : else <kick back> fi` collapsed conflict
and failure into one else-arm, so a broken invocation (bad ref, git < 2.38)
got reported as a branch conflict. `test_unknown_ref_is_a_machine_fault_not_a_conflict`
pins this: it must exit 2, never 1, for a ref that doesn't resolve.

All tests below run the ACTUAL `scripts/merge-precheck.sh` against real git
repositories built in `tmp_path` -- no fake git, no mocked subprocess. That is
what makes them sabotage-provable per the proj-verb bar: reverting either fix
directly in the script (restoring the bare `tail -n +2` for defect 1, or
folding the `case "$rc"` arms back into a single `else` for defect 2) turns
the corresponding test red, verified by hand while writing this suite.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from _gitrepo import _branch_from, _commit_file, _git

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "merge-precheck.sh"


def _init_repo(tmp_path: Path) -> Path:
    """A throwaway git repo with one commit on `main`, isolated user config."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("line1\nline2\nline3\n")
    (repo / "g.txt").write_text("other\n")
    _git(repo, "add", "f.txt", "g.txt")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _run(base: str, branch: str, repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), base, branch],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_clean_merge_exits_0_and_prints_nothing(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _branch_from(repo, "main", "branchA")
    _commit_file(repo, "f.txt", "line1\nCHANGED-A\nline3\n", "A changes f")

    result = _run("main", "branchA", repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def test_real_conflict_prints_exactly_the_conflicting_path(
    tmp_path: Path,
) -> None:
    """Design case 2: two branches edit one file's same lines. Must print
    ONLY the path -- no tree OID, no blank line, no Auto-merging/CONFLICT
    chatter (defect 1's fix)."""
    repo = _init_repo(tmp_path)
    _branch_from(repo, "main", "branchA")
    _commit_file(repo, "f.txt", "line1\nCHANGED-A\nline3\n", "A changes f")
    _branch_from(repo, "main", "branchB")
    _commit_file(repo, "f.txt", "line1\nCHANGED-B\nline3\n", "B changes f too")

    result = _run("branchA", "branchB", repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout == "f.txt\n"
    # None of merge-tree's own chatter leaked through.
    assert "Auto-merging" not in result.stdout
    assert "CONFLICT" not in result.stdout
    assert result.stdout.count("\n") == 1  # no trailing blank line either


def test_third_file_merging_clean_is_excluded(tmp_path: Path) -> None:
    """Design case 3, the proj-l38d.6 shape verbatim: one file conflicts,
    a second file merges clean. The clean file must NOT appear in the
    output -- this is defect 1's regression test. Reverting the
    blank-line truncation to a bare `tail -n +2` reintroduces
    "Auto-merging g.txt" into the output and fails this assertion
    (verified by hand)."""
    repo = _init_repo(tmp_path)
    _branch_from(repo, "main", "branchC")
    _commit_file(repo, "f.txt", "C1\nC2\nC3\n", "C conflicts on f")
    _commit_file(repo, "g.txt", "clean-file-change\n", "C also touches g")
    _branch_from(repo, "main", "branchD")
    _commit_file(repo, "f.txt", "D1\nD2\nD3\n", "D conflicts on f only")

    result = _run("branchC", "branchD", repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout == "f.txt\n"
    assert "g.txt" not in result.stdout


def test_multi_path_conflict_lists_every_path(tmp_path: Path) -> None:
    """Design case 5: every conflicting path listed, one per line."""
    repo = _init_repo(tmp_path)
    _branch_from(repo, "main", "branchE")
    _commit_file(repo, "f.txt", "E1\nE2\nE3\n", "E conflicts on f")
    _commit_file(repo, "g.txt", "E-other\n", "E conflicts on g too")
    _branch_from(repo, "main", "branchF")
    _commit_file(repo, "f.txt", "F1\nF2\nF3\n", "F conflicts on f")
    _commit_file(repo, "g.txt", "F-other\n", "F conflicts on g too")

    result = _run("branchE", "branchF", repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout == "f.txt\ng.txt\n"


def test_unknown_ref_is_a_machine_fault_not_a_conflict(tmp_path: Path) -> None:
    """Design case 4, defect 2's regression test: a ref that doesn't resolve
    must exit 2, never 1 -- and must print no conflicting paths to stdout.
    Empirically, `git merge-tree` itself exits 1 (not "some other code") for
    an unknown ref, identically to a real conflict, but with EMPTY stdout;
    the script's own ref-validation guard (see its header comment) is what
    turns this into exit 2 rather than a misdiagnosed conflict. Collapsing
    that guard (or the exit-1/exit-2 `case` arms) back into a single else-arm
    reintroduces a phantom `needs-rebase` kick-back for a broken ref
    (verified by hand)."""
    repo = _init_repo(tmp_path)
    _branch_from(repo, "main", "branchA")
    _commit_file(repo, "f.txt", "line1\nCHANGED-A\nline3\n", "A changes f")

    result = _run("branchA", "totally-bogus-ref-name-xyz", repo)

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert "GATE COULD NOT RUN" in result.stderr
    assert "not a branch conflict" in result.stderr


def test_unknown_base_ref_is_also_a_machine_fault(tmp_path: Path) -> None:
    """Same as above but the FIRST argument is the bad ref -- both positions
    are validated."""
    repo = _init_repo(tmp_path)
    _branch_from(repo, "main", "branchA")
    _commit_file(repo, "f.txt", "line1\nCHANGED-A\nline3\n", "A changes f")

    result = _run("totally-bogus-ref-name-xyz", "branchA", repo)

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert "GATE COULD NOT RUN" in result.stderr
    assert "not a branch conflict" in result.stderr


def test_unrelated_histories_is_a_machine_fault(tmp_path: Path) -> None:
    """A second failure shape that is neither 0 nor 1: `git merge-tree`
    refuses unrelated histories outright (exit 128). Must surface as exit 2,
    not be misread as a conflict."""
    repo = _init_repo(tmp_path)
    _branch_from(repo, "main", "branchA")
    _commit_file(repo, "f.txt", "line1\nCHANGED-A\nline3\n", "A changes f")

    _git(repo, "checkout", "-q", "--orphan", "orphanC")
    _git(repo, "rm", "-rf", "-q", ".")
    (repo / "o.txt").write_text("orphan\n")
    _git(repo, "add", "o.txt")
    _git(repo, "commit", "-q", "-m", "orphan commit")

    result = _run("branchA", "orphanC", repo)

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert "GATE COULD NOT RUN" in result.stderr
    assert "not a branch conflict" in result.stderr


def test_usage_without_args_is_exit_2_not_a_conflict() -> None:
    """A wrong arg count must exit 2, NEVER 1. In a script run as
    `bash merge-precheck.sh`, the old `${1:?...}` form exited 1 -- exactly the
    CONFLICT code -- so a caller bug would be misread as a branch conflict.
    Reverting the explicit arity check back to `${1:?...}` makes this exit 1
    and fails the assertion (defect: usage error colliding with exit 1).

    The advisory assertion below pins the advisory TEXT, which no structural
    sweep can see. It used to carry a second job -- catching this call site
    (which the header insists come FIRST) being moved above the script's
    separate `GATE_ADVISORY=(...)` assignment -- but proj-ysr6 removed that
    assignment entirely, so only the text check remains."""
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "usage" in result.stderr
    assert "not a branch conflict" in result.stderr
    assert result.stdout == ""


def test_usage_with_one_arg_is_also_exit_2() -> None:
    """The arity check fires on ANY count other than 2, not just zero args."""
    result = subprocess.run(
        ["bash", str(SCRIPT), "only-one-arg"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "usage" in result.stderr


def test_mktemp_failure_is_a_machine_fault_not_a_conflict(tmp_path: Path) -> None:
    """A machine fault in `mktemp` (TMPDIR pointing at a nonexistent dir) must
    exit 2, never 1 -- otherwise the failed `2>"$errfile"` redirect makes the
    merge-tree command substitution exit 1 with empty stdout, i.e. a PHANTOM
    conflict that kicks a perfectly-landable branch back needs-rebase. Refs are
    valid and the merge is clean, so ONLY the mktemp failure can produce a
    non-zero exit here. Removing the `|| gate_could_not_run` guard on mktemp
    reintroduces the exit-1 phantom conflict and fails this test."""
    repo = _init_repo(tmp_path)
    _branch_from(repo, "main", "branchA")
    _commit_file(repo, "f.txt", "line1\nCHANGED-A\nline3\n", "A changes f")

    result = subprocess.run(
        ["bash", str(SCRIPT), "main", "branchA"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "TMPDIR": str(tmp_path / "does-not-exist")},
        check=False,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert "GATE COULD NOT RUN" in result.stderr
    assert "not a branch conflict" in result.stderr
