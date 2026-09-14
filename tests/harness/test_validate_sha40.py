"""Tests for scripts/validate-sha40.sh (proj-xdg3).

During the /code fan-out of 2026-08-08, a rebase pickup (proj-r9z0) wrote a
39-character `land_head` into bd metadata -- one hex digit short of the real
branch tip. Nothing in the pipeline would have caught it: `/land`'s Section
2a precheck and `code-reviewer.md`'s own `review_head` check both compare
the recorded value against the *actual* branch tip purely to detect DRIFT (a
push after the ticket was marked ready) -- a malformed value never equals a
real SHA either, so it would have been silently misread as drift -- which in
Section 2a means a BOUNCE: the ticket superseded and the branch deleted, on
a branch that was already correct. (A malformed value is instead an
ESCALATE there, proj-xdg3: branch kept, nothing lands, a human re-derives
and re-writes the field.)

`scripts/validate-sha40.sh` gives both read sites one shared, tested
predicate: is this value even shaped like a full 40-lowercase-hex git SHA,
BEFORE it is ever compared against a real branch tip. All tests below run
the actual script via subprocess -- no re-implementation of the regex in
Python that could itself drift from what the shipped script does.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "validate-sha40.sh"

REAL_SHA = "54abb872e69596199d923e85cb61c4d267c6fa18"  # exactly 40 lowercase hex
TRUNCATED_SHA = REAL_SHA[:-1]  # the proj-r9z0 reproduction: 39 chars, one short
OVERLONG_SHA = REAL_SHA + "f"  # 41 chars


def _run(field: str, value: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), field, value],
        capture_output=True,
        text=True,
        check=False,
    )


def test_well_formed_sha_passes() -> None:
    result = _run("land_head", REAL_SHA)
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_39_char_truncated_sha_is_rejected() -> None:
    """Pins the exact proj-r9z0 reproduction shape: one hex digit short."""
    result = _run("land_head", TRUNCATED_SHA)
    assert result.returncode == 1
    assert "MALFORMED" in result.stderr
    assert "land_head" in result.stderr
    assert TRUNCATED_SHA in result.stderr
    assert "39 chars" in result.stderr


def test_41_char_overlong_sha_is_rejected() -> None:
    result = _run("review_head", OVERLONG_SHA)
    assert result.returncode == 1
    assert "MALFORMED" in result.stderr
    assert "41 chars" in result.stderr


def test_uppercase_sha_is_rejected() -> None:
    """Real `git rev-parse` output is always lowercase -- an uppercase run was
    never meant as a SHA, same convention `sha-fabrication-guard.sh` uses."""
    result = _run("land_head", REAL_SHA.upper())
    assert result.returncode == 1
    assert "MALFORMED" in result.stderr


def test_non_hex_characters_rejected() -> None:
    bad = "g" + REAL_SHA[1:]  # 40 chars, but 'g' is not hex
    result = _run("land_head", bad)
    assert result.returncode == 1
    assert "MALFORMED" in result.stderr


def test_empty_value_reported_as_missing_not_usage() -> None:
    """Both call sites read the field with `jq -r '... // empty'`, so an
    unwritten `land_head`/`review_head` arrives here as "". That is a metadata
    condition, not a call-site bug -- it must not be reported as a usage
    error, and (like every rejection) must not read as drift."""
    result = _run("land_head", "")
    assert result.returncode == 1
    assert "MISSING" in result.stderr
    assert "land_head" in result.stderr
    assert "usage" not in result.stderr
    assert "NOT drift" in result.stderr


def test_wrong_argument_count_exits_2_not_1() -> None:
    """A broken CALL is exit 2 ("the machine, never the content", proj-9i2p),
    never exit 1. Load-bearing: both call sites react to a nonzero exit by
    reporting MALFORMED METADATA, so a botched invocation exiting 1 would make
    `/land` escalate an already-correct ticket over a defect in its own markdown."""
    for argv in ([], ["land_head"], ["land_head", "a", "b"]):
        result = subprocess.run(
            [str(SCRIPT), *argv], capture_output=True, text=True, check=False
        )
        assert result.returncode == 2, argv
        assert "usage" in result.stderr
        assert "MALFORMED" not in result.stderr


def test_does_not_check_object_existence() -> None:
    """Deliberately NOT a `git cat-file -e` check -- a well-formed but
    unreachable SHA (e.g. on a branch not yet fetched) is a DIFFERENT
    question (drift), asked separately by the caller. Pins that this script
    stays narrowly scoped to shape, not reachability."""
    fake_but_well_shaped = "0" * 40
    result = _run("land_head", fake_but_well_shaped)
    assert result.returncode == 0
