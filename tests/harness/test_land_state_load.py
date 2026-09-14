"""Tests for scripts/land-state-load.sh (proj-dc4n).

`/land`'s SKILL.md reads $STATE_DIR files at four sites with four different
hand-rolled spellings encoding two policies -- missing-fatal/empty-OK, and
missing-fatal/empty-ALSO-fatal (`--require-nonempty` here) -- one of the four
(Section 4's `landed` load) silently dropped the diagnostic proj-0jan added at
the sibling site with the identical policy. This script makes the policy an
explicit argument, shellcheck'd and unit-tested, so a future editor doesn't
pick a fifth spelling by guess.

These tests run the ACTUAL script as a subprocess against real files on disk
-- no shell-snippet reimplementation -- so a regression in the script itself
(not a description of it) turns these red.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "land-state-load.sh"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_missing_file_is_fatal_default_policy(tmp_path: Path) -> None:
    missing = tmp_path / "accepted"
    result = _run(str(missing))
    assert result.returncode == 1
    assert result.stdout == ""
    assert "STATE LOAD FAILED" in result.stderr
    assert str(missing) in result.stderr


def test_missing_file_is_fatal_require_nonempty_policy(tmp_path: Path) -> None:
    missing = tmp_path / "accepted"
    result = _run(str(missing), "--require-nonempty")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "STATE LOAD FAILED" in result.stderr


def test_empty_file_is_ok_under_default_policy(tmp_path: Path) -> None:
    f = tmp_path / "accepted"
    f.write_text("")
    result = _run(str(f))
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert result.stderr == ""


def test_whitespace_only_file_is_ok_under_default_policy(tmp_path: Path) -> None:
    f = tmp_path / "accepted"
    f.write_text("   \n\n  \n")
    result = _run(str(f))
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_empty_file_is_fatal_under_require_nonempty(tmp_path: Path) -> None:
    f = tmp_path / "accepted"
    f.write_text("")
    result = _run(str(f), "--require-nonempty")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "STATE LOAD FAILED" in result.stderr
    assert "missing or empty" in result.stderr


def test_newlines_only_file_is_fatal_under_require_nonempty(tmp_path: Path) -> None:
    """Trailing newlines are stripped by the command substitution, so a file of
    only newlines reads back as the empty string and IS refused."""
    f = tmp_path / "accepted"
    f.write_text("\n\n\n")
    result = _run(str(f), "--require-nonempty")
    assert result.returncode == 1
    assert "missing or empty" in result.stderr


def test_spaces_only_file_passes_require_nonempty(tmp_path: Path) -> None:
    """The exact behaviour of the `[ -n "$(cat ...)" ]` this script replaces at
    both --require-nonempty call sites: only TRAILING NEWLINES are stripped, so
    a file of spaces/tabs is non-empty and passes. Pinned so the refactor stays
    a pure one -- tightening this to trim all whitespace would start rejecting
    inputs the pre-proj-dc4n call sites accepted, at the kick-back site turning
    a (degenerate but accepted) conflicts record into a refused pass."""
    f = tmp_path / "conflicts"
    f.write_text("   \t \n")
    result = _run(str(f), "--require-nonempty")
    assert result.returncode == 0
    assert result.stderr == ""


def test_nonempty_file_prints_content_under_either_policy(tmp_path: Path) -> None:
    f = tmp_path / "accepted"
    f.write_text("proj-aaaa\nproj-bbbb\n")
    for extra_args in ((), ("--require-nonempty",)):
        result = _run(str(f), *extra_args)
        assert result.returncode == 0
        assert result.stdout == "proj-aaaa\nproj-bbbb\n"
        assert result.stderr == ""


def test_directory_in_place_of_file_is_fatal(tmp_path: Path) -> None:
    d = tmp_path / "accepted"
    d.mkdir()
    result = _run(str(d))
    assert result.returncode == 1
    assert "STATE LOAD FAILED" in result.stderr


def test_context_lines_are_appended_to_the_diagnostic(tmp_path: Path) -> None:
    missing = tmp_path / "conflicts" / "proj-xxxx"
    result = _run(
        str(missing),
        "--require-nonempty",
        "--",
        "first context line",
        "second context line",
    )
    assert result.returncode == 1
    assert "first context line" in result.stderr
    assert "second context line" in result.stderr


def test_no_args_is_a_usage_error_not_a_content_verdict(tmp_path: Path) -> None:
    result = _run()
    assert result.returncode == 1
    assert result.stdout == ""
    assert "usage" in result.stderr.lower()


def test_unexpected_trailing_arg_without_dashdash_is_a_usage_error(
    tmp_path: Path,
) -> None:
    f = tmp_path / "accepted"
    f.write_text("x\n")
    result = _run(str(f), "bogus")
    assert result.returncode == 1
    assert "usage" in result.stderr.lower()
