"""scripts/check_docstring_refs.py -- scan-root derivation (hl7-poc-k3t).

Before this ticket the gate hardcoded ``SCAN_DIRS = ("src", "tests")``. After
the uv workspace split (hl7-poc-ouc) this repo's code lives under
``packages/*/src`` and ``packages/*/tests`` instead, and the hardcoded pair
silently matched nothing -- the gate degraded to reporting green over zero
scanned files, the worst failure mode for a gate.

These tests build throwaway trees on disk covering the legacy layout, the
workspace layout, and both at once, and assert the derived roots -- not just
that today's checkout happens to work. The trees are git-committed because
``_tracked_python_files`` sources its file list from ``git ls-files``: an
untracked tree scans as empty however ``_scan_roots`` resolves it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from _gitrepo import _commit_file, _git
from conftest import REPO_ROOT, load_module_from_path

SCRIPT = REPO_ROOT / "scripts" / "check_docstring_refs.py"

# Assembled from pieces, never written as one literal: ``tests/`` is itself a
# scan root, so an intact role in this file's own source is a dangling --
# or extra-counted -- ref the gate reports against its own test suite.
_DANGLING_ROLE = ":func:" + "`hl7poc.does_not_exist.at_all`"
_INTACT_ROLE = ":class:" + "`hl7poc.model.ModelError`"
_MOD_ROLE = ":mod:" + "`hl7poc.model`"

check_docstring_refs = load_module_from_path("check_docstring_refs", SCRIPT)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")


def _touch(repo: Path, rel: str, content: str = "") -> None:
    """Commit `rel` -- a thin delegation to `_gitrepo._commit_file`, not a
    second copy of it. These trees exist only for their SHAPE, so the commit
    message carries nothing a call site could usefully supply and the content
    is usually empty; spelling both at every call site would bury the paths,
    which are the only thing each test is actually asserting about."""
    _commit_file(repo, rel, content, f"add {rel}")


def test_legacy_layout_only(tmp_path: Path) -> None:
    repo = tmp_path / "legacy"
    _init_repo(repo)
    _touch(repo, "src/pkg/mod.py")
    _touch(repo, "tests/test_mod.py")

    assert check_docstring_refs._scan_roots(repo) == ["src", "tests"]


def test_workspace_layout_only(tmp_path: Path) -> None:
    """The shape hl7-poc-ouc introduces: no root ``src``/``tests``, only
    per-package ``packages/<name>/src`` and ``packages/<name>/tests`` --
    this is the exact case that used to silently scan zero files."""
    repo = tmp_path / "workspace"
    _init_repo(repo)
    _touch(repo, "packages/core/src/hl7poc_core/mod.py")
    _touch(repo, "packages/core/tests/test_mod.py")
    _touch(repo, "packages/listener/src/hl7poc_listener/mod.py")
    _touch(repo, "packages/listener/tests/test_mod.py")

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
    _touch(repo, "src/legacy_pkg/mod.py")
    _touch(repo, "tests/test_legacy.py")
    _touch(repo, "packages/core/src/hl7poc_core/mod.py")
    _touch(repo, "packages/core/tests/test_mod.py")

    roots = check_docstring_refs._scan_roots(repo)

    assert set(roots) == {"src", "tests", "packages/core/src", "packages/core/tests"}


def test_neither_layout_present(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    _init_repo(repo)
    _touch(repo, "README.md", "nothing to scan")

    assert check_docstring_refs._scan_roots(repo) == []


def test_workspace_layout_files_are_actually_scanned(tmp_path: Path) -> None:
    """End-to-end: a dangling ref under ``packages/*/src`` is caught, and
    one under ``packages/*/tests`` too -- not just that the directory is
    listed as a root, but that ``_tracked_python_files`` walks it and
    ``check`` reports the finding. This is the no-op the ticket describes,
    reproduced and shown fixed."""
    repo = tmp_path / "workspace_e2e"
    _init_repo(repo)
    module = f'"""Module.\n\n{_DANGLING_ROLE}\n"""\n'
    _touch(repo, "packages/core/src/hl7poc_core/mod.py", module)
    _touch(repo, "packages/core/tests/test_mod.py", module)

    unresolved, wrapped, checked = check_docstring_refs.check(repo)

    assert not wrapped
    assert checked == 2
    assert {str(f.path.relative_to(repo)) for f in unresolved} == {
        "packages/core/src/hl7poc_core/mod.py",
        "packages/core/tests/test_mod.py",
    }
    assert {f.ref for f in unresolved} == {"hl7poc.does_not_exist.at_all"}


def test_dangling_reference_is_reported_and_intact_one_is_not(
    tmp_path: Path,
) -> None:
    """A dangling ``hl7poc.*`` role is reported; an intact one naming a real
    symbol in this checkout is not -- resolved against THIS repo's own
    ``hl7poc`` package via ``sys.path``, not a throwaway tree, so the test
    exercises ``resolve_ref`` against real imports."""
    repo = tmp_path / "resolve_e2e"
    _init_repo(repo)
    module = f'"""Module.\n\n{_DANGLING_ROLE}\n\n{_INTACT_ROLE}\n"""\n'
    _touch(repo, "packages/core/src/hl7poc_core/mod.py", module)

    unresolved, wrapped, checked = check_docstring_refs.check(repo)

    assert not wrapped
    assert checked == 2
    assert {f.ref for f in unresolved} == {"hl7poc.does_not_exist.at_all"}


def test_checked_count_pins_n_resolvable_references(tmp_path: Path) -> None:
    """A tree with N resolvable ``hl7poc.*`` refs reports ``checked == N``,
    with zero unresolved -- pins the success-path count, not just that
    resolution passes."""
    repo = tmp_path / "count_n"
    _init_repo(repo)
    module = f'"""Module.\n\n{_INTACT_ROLE}\n\n{_MOD_ROLE}\n"""\n'
    _touch(repo, "packages/core/src/hl7poc_core/mod.py", module)

    unresolved, wrapped, checked = check_docstring_refs.check(repo)

    assert not wrapped
    assert not unresolved
    assert checked == 2


def test_checked_count_is_zero_and_distinguishable_when_no_refs(
    tmp_path: Path,
) -> None:
    """A tree with no ``hl7poc.*`` roles at all reports ``checked == 0`` --
    distinguishable from the N-checked case above, which is the whole point
    of returning a count instead of a bare pass/fail."""
    repo = tmp_path / "count_zero"
    _init_repo(repo)
    _touch(repo, "packages/core/src/hl7poc_core/mod.py", '"""No refs here."""\n')

    unresolved, wrapped, checked = check_docstring_refs.check(repo)

    assert not wrapped
    assert not unresolved
    assert checked == 0


def _run_gate(repo: Path) -> subprocess.CompletedProcess:
    """Drive the real CLI entry point in a subprocess rather than calling
    ``check()``. ``check()`` only RETURNS the count; the exit code and the
    wording that make a zero-checked run distinguishable live in ``main()``,
    so nothing below ``main()`` can pin them."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(repo)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_cli_reports_the_count_on_the_success_line(tmp_path: Path) -> None:
    repo = tmp_path / "cli_nonzero"
    _init_repo(repo)
    _touch(
        repo,
        "packages/core/src/hl7poc_core/mod.py",
        f'"""Module.\n\n{_INTACT_ROLE}\n\n{_MOD_ROLE}\n"""\n',
    )

    result = _run_gate(repo)

    assert result.returncode == 0, result.stderr
    assert "checked 2 hl7poc.* reference(s)" in result.stdout


def test_cli_fails_when_zero_references_are_checked(tmp_path: Path) -> None:
    """Zero checked is a hard failure, not a quieter green -- the exact
    green-over-zero vacuity this gate exists to refuse. Pinned at the CLI
    because the exit code is the only thing a caller sees."""
    repo = tmp_path / "cli_zero"
    _init_repo(repo)
    _touch(repo, "packages/core/src/hl7poc_core/mod.py", '"""No refs here."""\n')

    result = _run_gate(repo)

    assert result.returncode == 1
    assert "zero hl7poc.* references checked" in result.stderr
