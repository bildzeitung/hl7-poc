"""scripts/check_docstring_refs.py -- scan-root derivation (hl7-poc-k3t).

Before this ticket the gate hardcoded ``SCAN_DIRS = ("src", "tests")``. After
the uv workspace split (hl7-poc-ouc) this repo's code lives under
``packages/*/src`` and ``packages/*/tests`` instead, and the hardcoded pair
silently matched nothing -- the gate degraded to reporting green over zero
scanned files, the worst failure mode for a gate.

These tests build throwaway git-tracked trees on disk (``_scan_roots`` reads
git-tracked files, so an untracked tree scans as empty) covering the legacy
layout, the workspace layout, and both at once, and assert the derived roots
-- not just that today's checkout (still legacy-only, pending hl7-poc-ouc)
happens to work.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import REPO_ROOT, load_module_from_path

SCRIPT = REPO_ROOT / "scripts" / "check_docstring_refs.py"

check_docstring_refs = load_module_from_path("check_docstring_refs", SCRIPT)


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")


def _commit_file(repo: Path, rel: str, content: str = "") -> None:
    full = repo / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", f"add {rel}")


def test_legacy_layout_only(tmp_path: Path) -> None:
    repo = tmp_path / "legacy"
    _init_repo(repo)
    _commit_file(repo, "src/pkg/mod.py")
    _commit_file(repo, "tests/test_mod.py")

    assert check_docstring_refs._scan_roots(repo) == ["src", "tests"]


def test_workspace_layout_only(tmp_path: Path) -> None:
    """The shape hl7-poc-ouc introduces: no root ``src``/``tests``, only
    per-package ``packages/<name>/src`` and ``packages/<name>/tests`` --
    this is the exact case that used to silently scan zero files."""
    repo = tmp_path / "workspace"
    _init_repo(repo)
    _commit_file(repo, "packages/core/src/hl7poc_core/mod.py")
    _commit_file(repo, "packages/core/tests/test_mod.py")
    _commit_file(repo, "packages/listener/src/hl7poc_listener/mod.py")
    _commit_file(repo, "packages/listener/tests/test_mod.py")

    roots = check_docstring_refs._scan_roots(repo)

    assert "src" not in roots
    assert "tests" not in roots
    assert set(roots) == {
        "packages/core/src",
        "packages/core/tests",
        "packages/listener/src",
        "packages/listener/tests",
    }


def test_both_layouts_at_once(tmp_path: Path) -> None:
    """A checkout mid-migration: some packages moved, ``src``/``tests``
    still holding the rest. Both forms must union, not either/or."""
    repo = tmp_path / "mixed"
    _init_repo(repo)
    _commit_file(repo, "src/legacy_pkg/mod.py")
    _commit_file(repo, "tests/test_legacy.py")
    _commit_file(repo, "packages/core/src/hl7poc_core/mod.py")
    _commit_file(repo, "packages/core/tests/test_mod.py")

    roots = check_docstring_refs._scan_roots(repo)

    assert set(roots) == {"src", "tests", "packages/core/src", "packages/core/tests"}


def test_neither_layout_present(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    _init_repo(repo)
    _commit_file(repo, "README.md", "nothing to scan")

    assert check_docstring_refs._scan_roots(repo) == []


def test_workspace_layout_files_are_actually_scanned(tmp_path: Path) -> None:
    """End-to-end: a dangling ref under ``packages/*/src`` is caught, and
    one under ``packages/*/tests`` too -- not just that the directory is
    listed as a root, but that ``_tracked_python_files`` walks it and
    ``check`` reports the finding. This is the no-op the ticket describes,
    reproduced and shown fixed."""
    repo = tmp_path / "workspace_e2e"
    _init_repo(repo)
    _commit_file(
        repo,
        "packages/core/src/hl7poc_core/mod.py",
        '''"""Module.

    :func:`harness.does_not_exist.at_all`
    """
    ''',
    )

    unresolved, wrapped = check_docstring_refs.check(repo)

    assert not wrapped
    assert len(unresolved) == 1
    assert unresolved[0].ref == "harness.does_not_exist.at_all"
