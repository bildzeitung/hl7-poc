"""Tests for scripts/discard-beads-passive-export-churn.sh (hl7-poc-1yh).

`git checkout HEAD -- <paths...>` is atomic over its pathspecs: one call
listing every entry of `scripts/beads-passive-exports.txt` restores NOTHING
when any entry is unknown to git (`.beads/issues.jsonl` is never tracked
here). The script must restore each entry with its own call, so a tracked,
dirty `.beads/interactions.jsonl` is still restored. See docs/decisions.md.

Runs the real script against a real git repository in `tmp_path`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from _gitrepo import _commit_file, _git

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "discard-beads-passive-export-churn.sh"


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("line1\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _run(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), str(repo)],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_untracked_entry_does_not_block_restoring_a_tracked_entry(
    tmp_path: Path,
) -> None:
    """One listed export (`.beads/issues.jsonl`) is never tracked in this
    repo -- reproducing the observed 2026-09-16 state. The other
    (`.beads/interactions.jsonl`) IS tracked and has uncommitted churn. The
    real script must restore the tracked one and always exit 0, regardless
    of the untracked one."""
    repo = _init_repo(tmp_path)
    _commit_file(repo, ".beads/interactions.jsonl", "A\n", "seed interactions")
    (repo / ".beads" / "interactions.jsonl").write_text("B\n")  # dirty churn
    (repo / ".beads" / "issues.jsonl").write_text("untracked\n")  # never committed

    result = _run(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / ".beads" / "interactions.jsonl").read_text() == "A\n"
    # The untracked entry is left alone -- `git checkout HEAD --` cannot and
    # must not touch a path git has never heard of.
    assert (repo / ".beads" / "issues.jsonl").read_text() == "untracked\n"


def test_no_listed_paths_dirty_is_a_clean_noop_exit_0(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_file(repo, ".beads/interactions.jsonl", "A\n", "seed interactions")

    result = _run(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    status = _git(repo, "status", "--porcelain")
    assert status.stdout == ""
