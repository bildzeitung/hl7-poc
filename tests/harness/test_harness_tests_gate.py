"""Tests for scripts/harness-tests-gate.sh.

tests/harness/ tests the harness, never the project, so its verdict can only
change when a harness path (scripts/, .claude/, tests/harness/, noxfile.py,
pyproject.toml, uv.lock) changes. Before this gate the suite rode inside
`nox -s tests`, so every producer gate, every /land re-gate and every replay
session re-ran it to re-certify files the branch never touched. The gate
script runs `uv run --frozen nox -s harness_tests` only when the working tree
differs from the merge-base under one of those paths (or `--always`), and
otherwise skips with a one-line note and exit 0.

All tests run the ACTUAL script against real git repositories built in
`tmp_path`, with a fake `uv` first on PATH that records its argv and exits
with a chosen status -- the house pattern (fake `bd` on PATH), no seam in
the script. What is pinned:

* the skip / run decision, per path class: committed on the branch,
  uncommitted-tracked, untracked, deleted; a non-harness change alone skips;
  harness content already in the BASE does not count;
* `--always` runs with no diff at all;
* the 0/1/2 contract: nox's 1 passes through as 1; 127 (or any other
  non-0/1) becomes 2; an unresolvable base ref is 2; `uv` off PATH is 2;
* an absent tests/harness/ is a note and 0, never a red -- a project that
  dropped the suite must not red every gate for having done so.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from _gitrepo import _commit_file, _git
from conftest import fake_bin_env

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "harness-tests-gate.sh"


def _repo(tmp_path: Path) -> Path:
    """A repo with an `origin` whose `main` carries one harness file and one
    project file, checked out on a branch cut from origin/main, plus a
    tests/harness/ stub so the gate has something to run."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "remote", "add", "origin", str(origin))
    _commit_file(repo, "scripts/guard.sh", "#!/bin/sh\nexit 0\n", "harness: a guard")
    _commit_file(repo, "src/app.py", "x = 1\n", "project: app")
    _commit_file(
        repo,
        "tests/harness/test_stub.py",
        "def test_x():\n    assert True\n",
        "harness: stub",
    )
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "checkout", "-q", "-b", "work", "origin/main")
    return repo


def _fake_uv(tmp_path: Path, exit_code: int = 0) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "uv.log"
    (bin_dir / "uv").write_text(
        f'#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" >> "{log}"\nexit {exit_code}\n'
    )
    (bin_dir / "uv").chmod(0o755)
    return bin_dir, log


def _run(repo: Path, bin_dir: Path, *args: str, env: dict | None = None):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env or fake_bin_env(bin_dir),
        check=False,
    )


def _uv_argv(log: Path) -> list[str]:
    return log.read_text().split() if log.exists() else []


# --- the skip / run decision -------------------------------------------------


def test_no_change_at_all_skips_with_a_note(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    bin_dir, log = _fake_uv(tmp_path)
    r = _run(repo, bin_dir, "--base-ref", "origin/main")
    assert r.returncode == 0, r.stderr
    assert "no harness path changed" in r.stdout
    assert _uv_argv(log) == [], "uv must not be invoked on a skip"


def test_project_only_change_skips_even_though_base_holds_harness_files(
    tmp_path: Path,
) -> None:
    """scripts/guard.sh exists in origin/main. A branch that edits only src/
    must not be charged for it: the diff is against the merge-base, not
    against an empty tree."""
    repo = _repo(tmp_path)
    bin_dir, log = _fake_uv(tmp_path)
    _commit_file(repo, "src/app.py", "x = 2\n", "project: change")
    (repo / "src" / "new.py").write_text("y = 1\n")  # untracked, non-harness
    r = _run(repo, bin_dir, "--base-ref", "origin/main")
    assert r.returncode == 0, r.stderr
    assert "not run" in r.stdout
    assert _uv_argv(log) == []


@pytest.mark.parametrize(
    "path",
    [
        "scripts/new-guard.sh",
        ".claude/skills/land/SKILL.md",
        "tests/harness/test_new.py",
        "noxfile.py",
        "pyproject.toml",
        "uv.lock",
    ],
)
def test_committed_change_under_each_harness_path_runs(
    tmp_path: Path, path: str
) -> None:
    repo = _repo(tmp_path)
    bin_dir, log = _fake_uv(tmp_path)
    _commit_file(repo, path, "changed\n", f"harness: {path}")
    r = _run(repo, bin_dir, "--base-ref", "origin/main")
    assert r.returncode == 0, r.stderr
    assert "running nox -s harness_tests" in r.stdout
    assert f"  {path}" in r.stdout, "the run note names the path that triggered it"
    assert _uv_argv(log) == ["run", "--frozen", "nox", "-s", "harness_tests"]


def test_uncommitted_tracked_change_runs(tmp_path: Path) -> None:
    """An agent gates before it commits: the WORKING TREE is what is measured."""
    repo = _repo(tmp_path)
    bin_dir, log = _fake_uv(tmp_path)
    (repo / "scripts" / "guard.sh").write_text("#!/bin/sh\nexit 1\n")
    r = _run(repo, bin_dir, "--base-ref", "origin/main")
    assert r.returncode == 0, r.stderr
    assert "scripts/guard.sh" in r.stdout
    assert _uv_argv(log) == ["run", "--frozen", "nox", "-s", "harness_tests"]


def test_untracked_harness_file_runs(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    bin_dir, log = _fake_uv(tmp_path)
    (repo / ".claude/agents").mkdir(parents=True)
    (repo / ".claude/agents/new.md").write_text("stub\n")
    r = _run(repo, bin_dir, "--base-ref", "origin/main")
    assert r.returncode == 0, r.stderr
    assert ".claude/agents/new.md" in r.stdout
    assert _uv_argv(log) != []


def test_ignored_untracked_harness_path_does_not_count(tmp_path: Path) -> None:
    """`.claude/settings.local.json` and worktree junk are gitignored in the
    shipped .gitignore; `--exclude-standard` keeps them out of the decision."""
    repo = _repo(tmp_path)
    bin_dir, log = _fake_uv(tmp_path)
    _commit_file(repo, ".gitignore", ".claude/settings.local.json\n", "ignore")
    _git(repo, "push", "-q", "origin", "work:main")
    _git(repo, "fetch", "-q", "origin")
    (repo / ".claude").mkdir(exist_ok=True)
    (repo / ".claude" / "settings.local.json").write_text("{}\n")
    r = _run(repo, bin_dir, "--base-ref", "origin/main")
    assert r.returncode == 0, r.stderr
    assert _uv_argv(log) == [], r.stdout


def test_deleted_harness_file_runs(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    bin_dir, log = _fake_uv(tmp_path)
    _git(repo, "rm", "-q", "scripts/guard.sh")
    _git(repo, "commit", "-q", "-m", "drop a guard")
    r = _run(repo, bin_dir, "--base-ref", "origin/main")
    assert r.returncode == 0, r.stderr
    assert _uv_argv(log) != []


def test_always_runs_with_no_diff(tmp_path: Path) -> None:
    """The replay baselines every gate on the bare base ref, where there is no
    diff at all; a diff-driven skip would baseline nothing."""
    repo = _repo(tmp_path)
    bin_dir, log = _fake_uv(tmp_path)
    r = _run(repo, bin_dir, "--base-ref", "origin/main", "--always")
    assert r.returncode == 0, r.stderr
    assert "--always" in r.stdout
    assert _uv_argv(log) == ["run", "--frozen", "nox", "-s", "harness_tests"]


# --- the 0 / 1 / 2 contract --------------------------------------------------


def test_nox_red_is_exit_1(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    bin_dir, _ = _fake_uv(tmp_path, exit_code=1)
    _commit_file(repo, "scripts/x.sh", "#!/bin/sh\n", "harness")
    r = _run(repo, bin_dir, "--base-ref", "origin/main")
    assert r.returncode == 1


@pytest.mark.parametrize("code", [127, 3, 130])
def test_nox_non_content_exit_is_2(tmp_path: Path, code: int) -> None:
    repo = _repo(tmp_path)
    bin_dir, _ = _fake_uv(tmp_path, exit_code=code)
    _commit_file(repo, "scripts/x.sh", "#!/bin/sh\n", "harness")
    r = _run(repo, bin_dir, "--base-ref", "origin/main")
    assert r.returncode == 2
    assert "GATE COULD NOT RUN" in r.stderr and str(code) in r.stderr


def test_unresolvable_base_ref_is_2(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    bin_dir, log = _fake_uv(tmp_path)
    r = _run(repo, bin_dir, "--base-ref", "origin/no-such-branch")
    assert r.returncode == 2
    assert "does not resolve" in r.stderr
    assert _uv_argv(log) == []


def test_missing_base_ref_argument_is_2(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    bin_dir, _ = _fake_uv(tmp_path)
    r = _run(repo, bin_dir)
    assert r.returncode == 2 and "--base-ref" in r.stderr


def test_uv_off_path_is_2_not_a_verdict(tmp_path: Path) -> None:
    """PATH holds only a directory with git in it -- no uv anywhere."""
    repo = _repo(tmp_path)
    _commit_file(repo, "scripts/x.sh", "#!/bin/sh\n", "harness")
    only_git = tmp_path / "only-git"
    only_git.mkdir()
    real_git = shutil.which("git")
    assert real_git
    os.symlink(real_git, only_git / "git")
    for tool in ("bash", "grep", "printf"):
        real = shutil.which(tool)
        if real:
            os.symlink(real, only_git / tool)
    env = dict(os.environ, PATH=str(only_git))
    r = _run(repo, only_git, "--base-ref", "origin/main", env=env)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "'uv' is not on PATH" in r.stderr


def test_no_harness_tests_present_is_a_note_and_0(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    bin_dir, log = _fake_uv(tmp_path)
    _git(repo, "rm", "-q", "-r", "tests/harness")
    _git(repo, "commit", "-q", "-m", "drop the harness suite")
    r = _run(repo, bin_dir, "--base-ref", "origin/main")
    assert r.returncode == 0, r.stderr
    assert "has no gate tests" in r.stdout
    assert _uv_argv(log) == [], "nothing to run means nothing runs"


def test_not_a_git_repo_is_2(tmp_path: Path) -> None:
    bin_dir, _ = _fake_uv(tmp_path)
    bare = tmp_path / "plain"
    bare.mkdir()
    r = _run(bare, bin_dir, "--base-ref", "origin/main")
    assert r.returncode == 2 and "not inside a git repository" in r.stderr
