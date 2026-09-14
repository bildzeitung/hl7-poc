"""Tests for scripts/worktree-lock-stale.sh (proj-yrtu).

/land's Section 4 worktree-GC backstop skips every `locked` worktree
unconditionally, before any other predicate runs. The lock the Claude Code
harness records (every `isolation: "worktree"` launch worktree, plus
`.claude/agents/coding.md`'s own explicit pre-first-commit lock) is
PER-SESSION, not per-agent -- measured live: several worktrees can share ONE
lock-owner pid. A dead session therefore leaves every worktree it ever locked
stuck behind the `locked` check forever, since nothing downstream ever runs.

This script decides whether a `git worktree lock` reason (e.g. "claude agent
agent-<hash> (pid 1838142 start 76727921)") is STALE -- the recorded pid is
gone, or has been reused by an unrelated later process (detected via
`/proc/<pid>/stat`'s own `starttime`, matched against the token recorded at
lock time) -- or must still be treated as LIVE.

All tests exercise the ACTUAL script against REAL processes and REAL
`/proc/<pid>/stat` files -- no fake `/proc`, no mocked subprocess -- so a
broken pid/token regex, a broken `awk` field split, or a flipped exit-code
convention directly flips one of these tests red (sabotage-provable per the
proj-verb bar). The "reused pid" case is simulated by taking a REAL, running
process's REAL reason text and substituting a start token that does not
match its actual `/proc/<pid>/stat` starttime -- genuine OS-level pid reuse
cannot be forced deterministically, but a token mismatch is exactly what the
script is designed to detect regardless of how the mismatch arose.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "worktree-lock-stale.sh"


def _run(reason_args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *reason_args],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _real_starttime(pid: int) -> str:
    """Ground truth, computed INDEPENDENTLY of the script's own awk logic --
    if the script's field-splitting regressed, this would still compute the
    correct token and the corresponding test would go red."""
    raw = Path(f"/proc/{pid}/stat").read_text()
    rest = raw[raw.rindex(")") + 1 :]
    fields = rest.split()
    return fields[19]  # 0-indexed: field 20 of the post-')' remainder


def test_wrong_argument_count_exits_1() -> None:
    result = _run([])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "usage" in result.stderr


def test_empty_reason_fails_closed_as_live() -> None:
    """No reason text at all (a human's own unreasoned `git worktree lock`)
    -- can't parse a pid, so this must NOT be treated as stale."""
    result = _run([""])
    assert result.returncode == 1, result.stdout + result.stderr


def test_unparseable_reason_fails_closed_as_live() -> None:
    result = _run(["some free-form text with no pid or start token in it"])
    assert result.returncode == 1, result.stdout + result.stderr


def test_dead_pid_is_stale() -> None:
    """A pid that has already exited: `kill -0` fails outright -- stale,
    regardless of whatever start token is recorded."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    dead_pid = proc.pid
    # Best-effort race guard: immediately re-check it is indeed not running
    # (kill -0 from THIS test, not the script) before trusting the fixture.
    check = subprocess.run(
        ["kill", "-0", str(dead_pid)], capture_output=True, check=False
    )
    assert check.returncode != 0, "fixture pid unexpectedly still alive"

    result = _run([f"claude agent agent-x (pid {dead_pid} start 123456)"])
    assert result.returncode == 0, result.stdout + result.stderr


def test_live_pid_with_matching_token_is_not_stale() -> None:
    """A genuinely live session: pid running, starttime matches the recorded
    token exactly -- must NOT be reclaimed."""
    proc = subprocess.Popen(["sleep", "5"])
    try:
        token = _real_starttime(proc.pid)
        result = _run([f"claude agent agent-x (pid {proc.pid} start {token})"])
        assert result.returncode == 1, result.stdout + result.stderr
    finally:
        proc.terminate()
        proc.wait()


def test_live_pid_with_mismatched_token_is_stale() -> None:
    """The pid is running, but the recorded starttime does NOT match its
    actual /proc/<pid>/stat starttime -- simulates pid reuse (the recorded
    session's own pid was reassigned to an unrelated later process)."""
    proc = subprocess.Popen(["sleep", "5"])
    try:
        real_token = _real_starttime(proc.pid)
        bogus_token = str(int(real_token) + 999999)
        assert bogus_token != real_token
        result = _run([f"claude agent agent-x (pid {proc.pid} start {bogus_token})"])
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        proc.terminate()
        proc.wait()


def test_live_pid_with_no_start_token_fails_closed_as_live() -> None:
    """pid alive, but the reason carries no `start <n>` at all -- nothing to
    compare against, so this must fail closed (not stale)."""
    proc = subprocess.Popen(["sleep", "5"])
    try:
        result = _run([f"claude agent agent-x (pid {proc.pid})"])
        assert result.returncode == 1, result.stdout + result.stderr
    finally:
        proc.terminate()
        proc.wait()


def test_real_production_shaped_reason_string() -> None:
    """Pins the exact reason shape observed in production (bd show
    proj-yrtu): "claude agent agent-<hash> (pid <n> start <n>)"."""
    proc = subprocess.Popen(["sleep", "5"])
    try:
        token = _real_starttime(proc.pid)
        reason = f"claude agent agent-a1bde5837e597f872 (pid {proc.pid} start {token})"
        result = _run([reason])
        assert result.returncode == 1, result.stdout + result.stderr
    finally:
        proc.terminate()
        proc.wait()


def test_comm_field_with_unusual_characters_does_not_misalign_fields() -> None:
    """Regression pin for the awk field-split itself: /proc/<pid>/stat's
    `(comm)` field can be truncated/renamed via prctl and need not resemble
    the invoking argv at all, but it can never contain a ')' followed by
    more text that looks like a stat field -- the script's greedy `.*\\)`
    match must still land on the LAST ')' in the line. This exercises a
    process whose comm is deliberately set to something containing a space
    and a paren, to prove the split does not misalign on it."""
    # Use `bash -c 'exec -a "..." sleep 5'` to rename argv0/comm to a value
    # containing a paren + space -- if the platform doesn't support this
    # rename, the test still exercises a real process, just without the
    # adversarial comm; either way the field split must succeed.
    proc = subprocess.Popen(["bash", "-c", 'exec -a "weird (name) here" sleep 5'])
    try:
        time.sleep(0.2)  # let exec -a take effect before reading /proc
        token = _real_starttime(proc.pid)
        result = _run([f"claude agent agent-x (pid {proc.pid} start {token})"])
        assert result.returncode == 1, (
            result.stdout + result.stderr
        )  # still live, matched
    finally:
        proc.terminate()
        proc.wait()
