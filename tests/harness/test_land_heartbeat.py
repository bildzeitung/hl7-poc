"""Tests for scripts/land-heartbeat.sh (proj-s9xe.1).

This script replaces the six-line token read-back that used to be repeated
at seven call sites in `.claude/skills/land/SKILL.md` -- read the token file
beside the lock, warn (never fail the pass) if it's empty, then call
`scripts/land-lock.sh heartbeat|release <token>`. Wiring `SKILL.md`'s seven
call sites to actually invoke this script, and deleting the old inline
boilerplate, is the `.6` family's job, not this ticket's -- see proj-s9xe.1's
own scope-narrowing note. This file exercises the SCRIPT itself, standalone,
against a real throwaway git repo (same style as tests/test_land_lock.py),
not `SKILL.md`.

The token lives in a FILE (`$GITDIR/land-lock-token`), not a shell variable,
because no shell state survives between a skill's separate Bash tool
invocations -- and it sits *beside* the lock (inside `.git/`, outside
`$STATE_DIR`) so Section 1's per-pass scratch wipe can never delete it
mid-pass. That file, not an env var, is this script's only real input beyond
`--release`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from _gitrepo import _git

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "land-heartbeat.sh"
LAND_LOCK = REPO_ROOT / "scripts" / "land-lock.sh"


def _init_repo(tmp_path: Path) -> Path:
    """A throwaway git repo for the lock to live in.

    No copy of `land-lock.sh` is planted here: `land-heartbeat.sh` resolves its
    sibling as `$(dirname "$0")/land-lock.sh`, so it always runs THIS repo's
    real script -- while `land-lock.sh` itself resolves the lock from the
    CWD's git dir, which is this throwaway repo. Script under test: real.
    Lock and token state: disposable."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    return repo


def _lock_path(repo: Path) -> Path:
    return repo / ".git" / "land.lock"


def _token_path(repo: Path) -> Path:
    """The repo-GLOBAL token path (`--git-common-dir`), not a per-worktree one:
    in a main checkout `.git` IS the common dir, and a linked worktree's own
    private gitdir must never be used here (proj-k6h0)."""
    return repo / ".git" / "land-lock-token"


def _run(*args: str, repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        env=os.environ.copy(),
        check=False,
    )


def _land_lock(*args: str, repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(LAND_LOCK), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        env=os.environ.copy(),
        check=False,
    )


def _acquire_and_write_token(repo: Path) -> str:
    """Acquire the real lock and stash its owner token at the path this
    script reads -- exactly what SKILL.md's Section 1 boilerplate used to
    do inline (the seven call sites this ticket's follow-up, proj-s9xe.6,
    replaces with a call to this script)."""
    result = _land_lock("acquire", repo=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    token = _lock_path(repo).read_text().split()[4]
    _token_path(repo).write_text(token)
    return token


# ---------------------------------------------------------------------------
# Heartbeat: the common, best-effort path
# ---------------------------------------------------------------------------


def test_heartbeat_with_a_valid_token_refreshes_the_lock_and_exits_0(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    token = _acquire_and_write_token(repo)
    before_epoch = int(_lock_path(repo).read_text().split()[2])

    result = _run(repo=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    fields = _lock_path(repo).read_text().split()
    assert int(fields[2]) >= before_epoch
    # Preserves the owner token -- a heartbeat that minted a fresh one would
    # break the ownership check land-lock.sh's heartbeat/release rely on.
    assert fields[4] == token


def test_heartbeat_with_no_token_file_warns_and_exits_0_without_touching_the_lock(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    _land_lock("acquire", repo=repo)
    before = _lock_path(repo).read_text()
    # Deliberately do NOT write the token file this pass would normally have
    # stashed -- exercises the "own-token unavailable" branch.

    result = _run(repo=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "no own-token available" in result.stderr
    assert "heartbeat simply does not fire" in result.stderr
    assert _lock_path(repo).read_text() == before


def test_heartbeat_outside_any_git_repository_warns_and_exits_0(
    tmp_path: Path,
) -> None:
    """Lock bookkeeping must never stop an otherwise-fine pass -- unlike
    land-lock.sh's own scripts, which map this onto a documented non-zero
    exit, this wrapper's heartbeat path always exits 0 (its own header is
    explicit about this)."""
    outside = tmp_path / "not-a-repo"
    outside.mkdir()

    result = _run(repo=outside)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "not inside a git repository" in result.stderr


def test_heartbeat_on_a_missing_lock_creates_one(tmp_path: Path) -> None:
    """land-lock.sh's own `heartbeat` subcommand self-heals a missing lock
    (its header documents this); the wrapper just has to get out of the way
    and propagate that behaviour, not special-case it."""
    repo = _init_repo(tmp_path)
    # A token file with no corresponding lock record at all.
    _token_path(repo).write_text("some-token")

    result = _run(repo=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _lock_path(repo).exists()


# ---------------------------------------------------------------------------
# --release
# ---------------------------------------------------------------------------


def test_release_with_a_valid_token_releases_the_lock(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _acquire_and_write_token(repo)

    result = _run("--release", repo=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not _lock_path(repo).exists()


def test_release_propagates_land_locks_exit_status(tmp_path: Path) -> None:
    """Unlike the heartbeat path, --release propagates land-lock.sh's own
    status so a caller CAN notice a failed release, even though nothing is
    required to act on it (the script's own header is explicit about the
    asymmetry). land-lock.sh's `release <own-token>` exits 0 for ANY
    syntactically valid (non-empty) token, even a mismatched one -- it only
    refuses to touch someone else's record, silently, still exiting 0 (its
    own header: "release always exits 0 when the argument was valid"). The
    one real non-zero release outcome is a MACHINE FAULT -- an unwritable
    git dir -- so that's what this exercises, the same technique
    test_land_lock.py's own write-failure test uses."""
    repo = _init_repo(tmp_path)
    _acquire_and_write_token(repo)
    git_dir = repo / ".git"
    original_mode = git_dir.stat().st_mode
    git_dir.chmod(0o500)  # readable + traversable, not writable
    try:
        result = _run("--release", repo=repo)
    finally:
        git_dir.chmod(original_mode)  # or tmp_path teardown fails

    assert result.returncode != 0, result.stdout + result.stderr


def test_release_with_no_token_file_warns_and_exits_0_leaving_the_lock_held(
    tmp_path: Path,
) -> None:
    """land-lock.sh REFUSES a blind release outright (proj-yuwt) -- so the
    honest outcome here is: warn, do nothing, leave the lock for the
    staleness window to eventually reclaim. This wrapper's own exit stays 0
    either way (its header's Exit codes section says so explicitly)."""
    repo = _init_repo(tmp_path)
    _land_lock("acquire", repo=repo)

    result = _run("--release", repo=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "land-lock ownership check is DISABLED" in result.stderr
    assert "stays held until it ages out" in result.stderr
    assert _lock_path(repo).exists()


def test_a_mistyped_flag_is_a_usage_error_not_a_silent_heartbeat(
    tmp_path: Path,
) -> None:
    """A near-miss of `--release` must NOT degrade into a heartbeat: that
    would leave the lock held for the whole staleness window while handing
    the caller a 0 exit saying the release succeeded. Exit 2 is the same
    caller-bug reservation land-lock.sh makes for its own argv shapes."""
    repo = _init_repo(tmp_path)
    _acquire_and_write_token(repo)

    result = _run("--relase", repo=repo)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "usage:" in result.stderr
    assert _lock_path(repo).exists()


def test_release_outside_any_git_repository_warns_and_exits_0(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "not-a-repo"
    outside.mkdir()

    result = _run("--release", repo=outside)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "not inside a git repository" in result.stderr


# ---------------------------------------------------------------------------
# Repo-global token path (proj-k6h0) -- `--git-dir` is worktree-PRIVATE
# ---------------------------------------------------------------------------


def test_heartbeat_finds_the_token_when_run_from_a_linked_worktree(
    tmp_path: Path,
) -> None:
    """The token is written at the shared `--git-common-dir` path (SKILL.md's
    write site), so a reader that resolved a worktree-PRIVATE `--git-dir`
    instead would look in the wrong place and find nothing -- the same
    divergence `tests/test_land_lock.py`'s
    `test_lock_path_is_identical_from_a_linked_worktree` exercises for the
    lock file itself. This test is red against a bare `--git-dir` read (the
    linked worktree's own private gitdir has no token file, so the script
    would warn "no own-token available" and skip the heartbeat) and green
    against `--path-format=absolute --git-common-dir`."""
    repo = _init_repo(tmp_path)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "feat", "main")

    # Acquire from the main checkout and stash the token at the shared,
    # --git-common-dir path -- exactly what SKILL.md's write site does.
    token = _acquire_and_write_token(repo)
    before_epoch = int(_lock_path(repo).read_text().split()[2])

    # Run the heartbeat from the LINKED WORKTREE, not the main checkout.
    result = _run(repo=worktree)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "no own-token available" not in result.stderr
    fields = _lock_path(repo).read_text().split()
    assert int(fields[2]) >= before_epoch
    assert fields[4] == token
