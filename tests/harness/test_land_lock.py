"""Tests for scripts/land-lock.sh (proj-aps3, proj-ao95, proj-y3dw).

WHY the trap-and-PID design this replaces could not work -- an agent runs
each fenced `bash` block of `.claude/skills/land/SKILL.md` as its own Bash
invocation, so a `trap ... EXIT` fires before the next section runs and a
recorded PID is *always* already dead -- and why the replacement is a
wall-clock staleness token: see the header comment of scripts/land-lock.sh.
That rationale is deliberately NOT restated here (the
tests/test_blocks_dependents.py precedent) -- it lives next to the code it
constrains, so it cannot drift out of sync with a second copy. The header
also records the mechanism's known limits: the `heartbeat` subcommand
(proj-m87j) moves the TTL toward idle-time semantics over the two loops it
brackets, plus (proj-v4sv) two boundary call sites that close the two
originally-uncovered stretches that grew with queue size -- but not over the
whole pass; CAVEAT 1 enumerates the one stretch that still stays uncovered
(Section 3's combined re-gate, which does not grow with queue size) and why
the 1800s default was therefore left alone. The stale-lock reclaim path
(CAVEAT 2) is now closed OUTRIGHT via
`flock(1)` (proj-y3dw) -- see that section of the header for why the earlier
mkdir-gate design (proj-ao95, proj-78ih) could narrow but never fully close
it -- and the record's owner token (5th field) is both preserved across
heartbeat calls and -- since proj-q9pm -- compared against the calling pass's
own remembered token, so `heartbeat`/`release` refuse to touch a record this
pass no longer owns. That comparison is skipped when the caller omits its
token, which is why the SKILL.md call-site pins near the bottom of this file
gate that every real call site supplies one.

What this file adds on top of that is the regression gate, in four parts:

1. Behavioural tests that run the ACTUAL script against a real, throwaway
   git repository in `tmp_path` (its only external dependency is
   `git rev-parse --git-common-dir`, to place the lock under the repo's
   shared `.git/` -- repo-global rather than worktree-private, proj-xkpd) --
   no fake git, no mocked subprocess. Reintroducing either half of the old
   design turns these red: a `trap` release makes
   `test_second_acquire_without_release_is_skipped_while_fresh` fail (the
   lock is gone by the next invocation), and a `kill -0` liveness check
   makes `test_fresh_lock_with_an_unreachable_pid_is_not_reclaimed` fail.
   Both mutations were run against this file and confirmed red (5 and 6
   failures of the 14 behavioural tests that existed when that was measured).
   Two tests pin the repo-global lock path (proj-xkpd), and both pin the SAME
   property -- "an acquire from elsewhere in the repo contends on the same
   file" -- from the two directions that can break it:
   `test_lock_path_is_identical_from_a_linked_worktree` (a real `git worktree
   add`; red if `--git-common-dir` reverts to `--git-dir`) and
   `test_lock_path_is_identical_from_a_subdirectory_of_the_main_checkout` (a
   cwd-relative path that stops resolving). Note that neither pins
   `--path-format=absolute` itself on a current git -- see that second test's
   docstring, which is explicit about the limit.

2. A concurrency stress test (proj-ao95, updated for proj-y3dw) reproducing
   the actual defect: many rounds of N-way concurrent `acquire` calls against
   the SAME manually crafted stale lock, asserting exactly one winner per
   round. Run against the pre-fix script (`rm` then create, two steps) this
   reliably shows multiple winners in some rounds; against the mkdir-gate
   design it still showed multiple winners on a starting arrangement this
   test deliberately did not cover (see its own docstring); against the
   flock'd script it must never show more than one, in any round, from ANY
   starting arrangement -- the whole point of a real mutex over a
   hand-rolled gate object. The CONTENTION LEVEL is part of the gate, not a
   free parameter. See that test's own docstring for the measurements.

3. Call-site pins against the SHIPPED `SKILL.md`. The defect lived in a
   markdown fence, where no gate reaches it, so the behavioural tests above
   would all stay green while SKILL.md quietly went back to an inline trap
   and stopped calling this script at all. Same reasoning and same shape as
   `tests/test_isolation_guard.py`'s `test_every_agent_definition_invokes_
   the_guard`.

   These pins are only as good as the parser under them, which is why one of
   them checks the parser rather than the skill:
   `test_fenced_bash_sees_every_bash_marker_including_indented_ones` asserts
   the shared `tests/conftest.py::bash_fence_blocks` helper sees every bash
   fence in SKILL.md, against an independently-derived count. Without it these
   pins silently covered 20 of 24 blocks (proj-ovgs).

4. The flock portability fallback (proj-y3dw): `acquire` refuses to proceed,
   reporting a MACHINE FAULT and skipping the tick, when `flock(1)` is not on
   PATH, rather than silently reverting to unsafe behaviour on a platform
   (macOS, stock git-bash) that lacks util-linux.

Retired by proj-y3dw, not restated here: the mkdir-gate-specific tests this
file used to carry (a reclaim gate's self-heal, its owner-token re-check, and
the `LAND_LOCK_TEST_STALL_SECONDS` test-only stall hook that staged the
alive-but-stalled-holder displacement). `flock(1)` closes the whole class of
races those tests existed to pin -- there is no gate object left for them to
exercise. See scripts/land-lock.sh's header (CAVEAT 2) for the full history.
"""

from __future__ import annotations

import concurrent.futures
import fcntl
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import pytest
from _gitrepo import _git
from conftest import (
    _BLOCKQUOTE_MARKER,
    LAND_SKILL_BASH,
    LAND_SKILL_BLOCKS,
    LAND_SKILL_TEXT,
    bash_fence_blocks,
    fake_bin_env,
    only_block_with,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "land-lock.sh"
MERGE_ONE = REPO_ROOT / "scripts" / "land-merge-one.sh"

# The literal sentinel land-lock.sh accepts in place of a real own-token to
# opt a heartbeat/release call OUT of the ownership check on purpose
# (proj-yuwt) -- see that script's own header for the one sanctioned call
# site (SKILL.md Section 0's parse-failure bail). Used throughout this file
# wherever a test needs a VALID heartbeat/release call but is not itself
# exercising the ownership comparison -- an omitted or empty own-token is, as
# of proj-yuwt, a caller bug (exit 2), not a supported degraded mode, so a
# bare `_run("heartbeat", repo=repo)` no longer reproduces the old
# blind-and-legal behaviour.
BLIND = "--land-lock-blind"


def _init_repo(tmp_path: Path) -> Path:
    """A throwaway git repo -- just enough for `git rev-parse --git-common-dir`.

    The `user.email`/`user.name` config is not decoration: any test here that
    goes on to make a COMMIT (the linked-worktree tests below need one, since
    `git worktree add` requires a ref to branch from) fails outright with
    `fatal: empty ident name` on a machine with no ambient global git identity
    -- a fresh clone or a CI container (measured: exit 128). Setting it here
    rather than in those tests matches its sibling script-test modules that
    all configure it inside their own `_init_repo`, and keeps this file
    from passing only on machines that happen to have a global identity
    configured.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    return repo


def _lock_path(repo: Path) -> Path:
    return repo / ".git" / "land.lock"


def _path_without_flock(tmp_path: Path) -> str:
    """A PATH string that resolves every ordinary tool the script needs (git,
    bash, coreutils, ...) but never `flock` -- built by symlink-farming every
    directory currently on $PATH into a scratch tree with `flock` entries
    excluded, rather than merely dropping the one directory `flock` happens
    to live in (which would still leave it reachable if some other $PATH
    entry also carries a copy). Used to exercise the proj-y3dw portability
    fallback without needing an actual flock-less machine.

    Only the directories that ACTUALLY carry a `flock` are farmed; every
    other $PATH entry is passed through unchanged. The invariant is
    identical either way ("no entry on this PATH resolves flock, every other
    tool still resolves"), and it still holds if a third directory grows a
    `flock` later -- but on a typical box that is 2 of ~26 directories, so
    this builds a few thousand symlinks instead of ~9000 (measured here:
    9011 before, and the teardown of those pays again). `os.scandir` does
    the isdir/listdir work in one pass."""
    shim_root = tmp_path / "path-without-flock"
    shim_root.mkdir()
    shim_dirs: list[str] = []
    for i, d in enumerate(os.environ.get("PATH", "").split(os.pathsep)):
        if not d or not os.path.isdir(d):
            continue
        try:
            entries = [e.name for e in os.scandir(d)]
        except OSError:
            continue
        if "flock" not in entries:
            # Cannot resolve `flock` anyway -- no need to mirror it.
            shim_dirs.append(d)
            continue
        shim_dir = shim_root / str(i)
        shim_dir.mkdir()
        for name in entries:
            if name == "flock":
                continue
            try:
                (shim_dir / name).symlink_to(os.path.join(d, name))
            except OSError:
                continue
        shim_dirs.append(str(shim_dir))
    return os.pathsep.join(shim_dirs)


def _write_stale_lock(repo: Path, *, age: int = 100_000) -> int:
    """A lock record far past any plausible staleness threshold. Deliberately
    FOUR fields: an old record predating the owner token must still parse, and
    that back-compatibility is worth exercising rather than assuming."""
    old_epoch = int(time.time()) - age
    _lock_path(repo).write_text(
        f"12345 some-old-host {old_epoch} 2020-01-01T00:00:00Z\n"
    )
    return old_epoch


def _run(
    *args: str, repo: Path, env_overrides: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    env = {**os.environ, **(env_overrides or {})}
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )


# ---------------------------------------------------------------------------
# Basic acquire / release
# ---------------------------------------------------------------------------


def test_acquire_on_a_fresh_repo_succeeds_and_writes_a_lock_file(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)

    result = _run("acquire", repo=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    # Placement matters as much as content: under `.git/`, so it is
    # per-machine and can never be committed (same path the inline snippet
    # this replaces used).
    lock = _lock_path(repo)
    assert lock.exists()
    fields = lock.read_text().split()
    assert len(fields) == 5, fields  # pid hostname epoch iso8601 owner-token
    assert fields[0].isdigit()  # pid, recorded for humans only (see header)
    assert fields[2].isdigit()  # epoch seconds -- the ONLY field acquire reads back
    # Owner token (proj-ao95): opaque, non-empty, distinct across acquisitions
    # -- not read back by anything in THIS script yet (see CAVEAT 2 / proj-q9pm)
    # but must actually be present and vary, or a future ownership check has
    # nothing to compare against.
    assert fields[4], fields
    second = _run("release", BLIND, repo=repo)
    assert second.returncode == 0
    reacquired = _run("acquire", repo=repo)
    assert reacquired.returncode == 0
    assert lock.read_text().split()[4] != fields[4], (
        "two separate acquisitions produced the SAME owner token"
    )


def test_second_acquire_without_release_is_skipped_while_fresh(
    tmp_path: Path,
) -> None:
    """This is the core defect: a lock just acquired must still be held for
    a SEPARATE, later invocation -- exactly the cross-Bash-call scenario the
    old trap-based design got wrong (it released before the next invocation
    even ran). Two separate `subprocess.run` calls stand in for two separate
    Bash tool invocations."""
    repo = _init_repo(tmp_path)
    first = _run("acquire", repo=repo)
    assert first.returncode == 0, first.stdout + first.stderr

    second = _run("acquire", repo=repo)

    assert second.returncode == 1, second.stdout + second.stderr
    assert "skipping this tick" in second.stderr
    # The lock is untouched -- still the FIRST acquire's record, not reclaimed.
    lock = _lock_path(repo)
    assert lock.exists()


def test_release_then_acquire_succeeds_again(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    assert _run("acquire", repo=repo).returncode == 0

    released = _run("release", BLIND, repo=repo)
    assert released.returncode == 0
    assert not _lock_path(repo).exists()

    reacquired = _run("acquire", repo=repo)
    assert reacquired.returncode == 0, reacquired.stdout + reacquired.stderr


def test_release_with_no_lock_held_is_a_harmless_no_op(tmp_path: Path) -> None:
    """A caller must be able to call `release` even on a path where it never
    held the lock (e.g. it just skipped the tick) without that erroring."""
    repo = _init_repo(tmp_path)

    result = _run("release", BLIND, repo=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not _lock_path(repo).exists()


# ---------------------------------------------------------------------------
# A rev-parse failure must land inside the documented 0/1/2 contract, never
# escape as git's own bare 128 (proj-8qkb)
#
# PRE-FIX, `$LOCK="$(git rev-parse ...)/land.lock"` was a bare command
# substitution under `set -euo pipefail`, so running from outside any git
# repository exited with git's raw 128 and a bare `fatal:` -- a status the
# script's own header says it never returns. Each subcommand maps that failure
# onto a DIFFERENT documented exit (the whole reason these are three tests and
# not one), so the script's exit code IS the assertion here; the stderr
# substring pins that the diagnostic is attributed to land-lock rather than
# left as git's unattributable `fatal:`. Why exit 1 and not
# assert-main-checkout.sh's exit 2 lives in the script, next to the code.
#
# `tmp_path` is not itself inside a git repository (unlike a checkout under
# this repo's own tree), so no `GIT_CEILING_DIRECTORIES` dance is needed --
# same fixture shape as tests/test_assert_main_checkout.py's
# test_not_inside_any_repository_is_exit_2_not_a_raw_git_128.
#
# All three sabotage-verified together: reverting the `if ! GIT_COMMON_DIR=...`
# wrap back to the bare form turns all three red with returncode 128.
# ---------------------------------------------------------------------------


def test_acquire_outside_any_git_repository_exits_1_not_a_raw_128(
    tmp_path: Path,
) -> None:
    """`acquire` maps the failure onto its existing exit-1 MACHINE FAULT
    branch -- the same class as "cannot create the lockfile", so a caller is
    never told "another lander is running" when the machine is at fault."""
    outside = tmp_path / "not-a-repo"
    outside.mkdir()

    result = _run("acquire", repo=outside)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "land-lock: MACHINE FAULT" in result.stderr
    assert "skipping this tick" in result.stderr.lower()


def test_heartbeat_outside_any_git_repository_exits_1_not_a_raw_128(
    tmp_path: Path,
) -> None:
    """`heartbeat` has its own documented exit-1 "could not write the lock
    file" branch, with wording distinct from acquire's MACHINE FAULT one --
    heartbeat is bookkeeping, so its diagnostic must not read as a landing
    verdict."""
    outside = tmp_path / "not-a-repo"
    outside.mkdir()

    result = _run("heartbeat", BLIND, repo=outside)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "land-lock: heartbeat" in result.stderr


def test_release_outside_any_git_repository_still_exits_0(tmp_path: Path) -> None:
    """`release` is documented to ALWAYS exit 0 ("rm -f is idempotent"), and
    that promise must survive this fix -- so this is the one subcommand whose
    documented exit is unchanged by the wrap, yet it still went 128 pre-fix.
    It must reach 0 through the new branch, with a diagnostic, rather than
    silently."""
    outside = tmp_path / "not-a-repo"
    outside.mkdir()

    result = _run("release", BLIND, repo=outside)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "land-lock: release" in result.stderr


# ---------------------------------------------------------------------------
# Repo-global lock path (proj-xkpd) -- `--git-dir` is worktree-PRIVATE
# ---------------------------------------------------------------------------


def test_lock_path_is_identical_from_a_linked_worktree(tmp_path: Path) -> None:
    """Why a worktree-private gitdir would break the lock is explained at the
    `LOCK=` line in scripts/land-lock.sh, not restated here (this module's
    no-second-copy policy, above).

    What this test contributes: it is red against the `--git-dir` form -- the
    worktree's `acquire` succeeds instead of being skipped -- and green against
    `--git-common-dir`. Verified by running exactly that mutation (proj-xkpd);
    both acquires then reported success with different tokens."""
    repo = _init_repo(tmp_path)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "feat", "main")

    from_main = _run("acquire", repo=repo)
    assert from_main.returncode == 0, from_main.stdout + from_main.stderr
    assert _lock_path(repo).exists()

    from_worktree = _run("acquire", repo=worktree)

    # If the lock path were worktree-relative, this `acquire` would succeed
    # (it would be writing to a DIFFERENT, not-yet-existing file) instead of
    # being skipped as "still held".
    assert from_worktree.returncode == 1, (
        "acquire from the linked worktree succeeded even though the main "
        "checkout already holds the lock -- the lock path is not repo-global"
        f"\nmain: {from_main.stdout}{from_main.stderr}"
        f"\nworktree: {from_worktree.stdout}{from_worktree.stderr}"
    )
    assert "skipping this tick" in from_worktree.stderr

    # And no SEPARATE, worktree-private lock file was created either -- the
    # worktree's `acquire` genuinely contended on the SAME file, not merely
    # failed for some unrelated reason while writing its own private one.
    #
    # ASK GIT where the private gitdir is rather than hand-spelling it: the
    # directory under `.git/worktrees/` is named after the worktree's DIRECTORY
    # basename (`wt`), NOT its branch (`feat`), so a hand-spelled
    # `.git/worktrees/feat/...` names a path git never creates under EITHER
    # form of the fix -- unconditionally absent, and therefore proof of
    # nothing. Same repo-wide fiat as elsewhere in this file (docs/conventions.md,
    # "Derive identifiers, never retype them").
    private_gitdir = Path(
        _git(
            worktree, "rev-parse", "--path-format=absolute", "--git-dir"
        ).stdout.strip()
    )
    private_lock = private_gitdir / "land.lock"
    assert not private_lock.exists(), (
        "a worktree-private lock file exists -- the worktree computed a "
        f"DIFFERENT $LOCK ({private_lock}) than the main checkout"
    )


def test_lock_path_is_identical_from_a_subdirectory_of_the_main_checkout(
    tmp_path: Path,
) -> None:
    """A hazard the linked-worktree test above cannot catch, because it is not
    about worktrees at all -- see the `--path-format=absolute` paragraph at the
    `LOCK=` line in scripts/land-lock.sh for why cwd-relativity matters.

    Be precise about what this does and does not pin, because it is weaker than
    it looks. Dropping `--path-format=absolute` alone does NOT turn it red on a
    git that resolves the relative answer against cwd (measured: git 2.43
    answers `../../.git`, which still names the right file). The contract it
    actually guards is "an acquire from a subdirectory contends on the SAME
    file as one from the root", which is the property mutual exclusion needs.
    It DOES go red once the path stops resolving from cwd -- the older-git
    behaviour of returning a bare `.git` relative to the TOPLEVEL. Verified
    non-vacuous by mutating $LOCK to a hardcoded `.git/land.lock`: the subdir
    acquire then takes the MACHINE FAULT branch, so `returncode == 1` passes
    coincidentally and the STDERR assertion below is the one that catches it.
    Keep both."""
    repo = _init_repo(tmp_path)
    subdir = repo / "sub" / "deeper"
    subdir.mkdir(parents=True)

    from_root = _run("acquire", repo=repo)
    assert from_root.returncode == 0, from_root.stdout + from_root.stderr
    assert _lock_path(repo).exists()

    from_subdir = _run("acquire", repo=subdir)

    assert from_subdir.returncode == 1, (
        "acquire from a subdirectory of the main checkout succeeded even "
        "though the root already holds the lock -- the lock path is cwd-relative"
        f"\nroot: {from_root.stdout}{from_root.stderr}"
        f"\nsubdir: {from_subdir.stdout}{from_subdir.stderr}"
    )
    assert "skipping this tick" in from_subdir.stderr

    # No second lock file anywhere under the subdirectory -- the subdir's
    # `acquire` contended on the SAME file rather than writing its own.
    assert not list(subdir.rglob("land.lock")), (
        "a lock file was created under the subdirectory -- $LOCK resolved "
        "relative to cwd instead of to the shared .git"
    )


# ---------------------------------------------------------------------------
# Heartbeat -- turns the TTL from acquisition-age into idle-time (proj-m87j)
# ---------------------------------------------------------------------------


def test_heartbeat_refreshes_an_existing_locks_timestamp(tmp_path: Path) -> None:
    """The core new behaviour: a lock this pass already holds gets a fresh
    epoch on every heartbeat call, without needing to go through `acquire`
    again (which would just fail -- the lock is still fresh)."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    old_epoch = int(time.time()) - 100
    lock.write_text(f"12345 host {old_epoch} 2020-01-01T00:00:00Z\n")

    result = _run("heartbeat", BLIND, repo=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    new_epoch = int(lock.read_text().split()[2])
    assert new_epoch > old_epoch


def test_heartbeat_preserves_the_existing_owner_token(tmp_path: Path) -> None:
    """MERGE RESOLUTION pin (proj-ao95 x proj-m87j): `heartbeat` must PRESERVE
    field 5 (the owner token) rather than regenerate or blank it -- a
    heartbeat that mints a fresh token every tick would destroy the ownership
    continuity a future check (proj-q9pm) needs to compare against, while
    every other heartbeat test here (which only checks the epoch/timestamp)
    would stay green regardless. This is the exact regression the MERGE NOTE
    in scripts/land-lock.sh's header called out: reverting `heartbeat`'s
    `lock_record "$CUR_TOKEN"` call back to main's original, argument-less
    `lock_record` either crashes under `set -u` (the positional is mandatory)
    or, if defaulted instead of reverted, changes the token -- either way
    this test goes red.
    """
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    old_epoch = int(time.time()) - 100
    original_token = "deadbeefcafef00d"
    lock.write_text(f"12345 host {old_epoch} 2020-01-01T00:00:00Z {original_token}\n")

    result = _run("heartbeat", BLIND, repo=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    fields = lock.read_text().split()
    assert len(fields) == 5, fields
    assert fields[4] == original_token, (
        "heartbeat changed the owner token -- it must PRESERVE field 5, "
        "never regenerate or blank it (see MERGE RESOLUTION in "
        "scripts/land-lock.sh's header)"
    )
    # The timestamp itself must still have refreshed -- this is not "heartbeat
    # is a no-op" in disguise.
    assert int(fields[2]) > old_epoch


def test_heartbeat_on_a_pre_token_four_field_lock_mints_a_fresh_token(
    tmp_path: Path,
) -> None:
    """Backward compatibility: a lock record predating the owner token
    (proj-aps3-era, four fields) has nothing to preserve, so `heartbeat`
    mints a fresh one -- matching `acquire`'s own shape for the same case --
    rather than crashing or writing an empty 5th field."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    old_epoch = int(time.time()) - 100
    lock.write_text(f"12345 host {old_epoch} 2020-01-01T00:00:00Z\n")

    result = _run("heartbeat", BLIND, repo=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    fields = lock.read_text().split()
    assert len(fields) == 5, fields
    assert fields[4], "heartbeat left field 5 empty instead of minting a token"


def test_heartbeat_on_a_missing_lock_creates_one(tmp_path: Path) -> None:
    """Heartbeat is unconditional -- if the lock file is somehow already gone
    (should not happen at either documented call site, but must not crash the
    pass if it does), it creates a fresh one rather than erroring, since the
    caller's intent ("the pass is still alive right now") is the same either
    way as a normal refresh."""
    repo = _init_repo(tmp_path)
    assert not _lock_path(repo).exists()

    result = _run("heartbeat", BLIND, repo=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _lock_path(repo).exists()


def test_stale_lock_refreshed_by_heartbeat_is_not_reclaimed(tmp_path: Path) -> None:
    """The regression this subcommand exists to prevent: a lock old enough to
    be judged stale under the TTL must NOT be reclaimed by a later `acquire`
    once a `heartbeat` in between has re-stamped it fresh -- i.e. the TTL
    genuinely measures idle time now, not the original acquire's age."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    old_epoch = int(time.time()) - 1000
    lock.write_text(f"12345 host {old_epoch} 2020-01-01T00:00:00Z\n")

    heartbeat = _run(
        "heartbeat", BLIND, repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "500"}
    )
    assert heartbeat.returncode == 0, heartbeat.stdout + heartbeat.stderr

    # Without the heartbeat above, this acquire would reclaim the lock (its
    # original 1000s age exceeds the 500s threshold) -- see
    # test_stale_lock_is_reclaimed below for that baseline behaviour.
    second_acquire = _run(
        "acquire", repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "500"}
    )

    assert second_acquire.returncode == 1, second_acquire.stdout + second_acquire.stderr
    assert "skipping this tick" in second_acquire.stderr


def test_heartbeat_write_failure_is_reported_but_never_crashes(
    tmp_path: Path,
) -> None:
    """A heartbeat that cannot write (unwritable git dir) must exit 1 with a
    clear diagnostic -- never a silent success, and never an uncaught
    failure that could be mistaken for a script bug (proj-aps3's own "lock
    was not held must be observable, never silent" standard, applied here to
    the write path instead of the read path)."""
    repo = _init_repo(tmp_path)
    git_dir = repo / ".git"
    original_mode = git_dir.stat().st_mode
    git_dir.chmod(0o500)  # readable + traversable, not writable
    try:
        result = _run("heartbeat", BLIND, repo=repo)
    finally:
        git_dir.chmod(original_mode)  # or tmp_path teardown fails

    assert result.returncode == 1, result.stdout + result.stderr
    assert "heartbeat could not write" in result.stderr
    assert not _lock_path(repo).exists()


# ---------------------------------------------------------------------------
# Ownership check (proj-q9pm) -- heartbeat/release refuse to touch a record
# this pass no longer owns, once they are given their own token to check
# against.
# ---------------------------------------------------------------------------


def test_heartbeat_with_explicit_blind_sentinel_still_blindly_preserves(
    tmp_path: Path,
) -> None:
    """The one sanctioned opt-out (proj-yuwt): the literal `--land-lock-blind`
    sentinel in place of a real own-token reproduces the pre-proj-q9pm
    behaviour exactly -- no ownership comparison at all, even against a
    record naming a completely different token. Unlike before proj-yuwt, this
    is no longer reachable by simply OMITTING the argument -- that is now a
    caller bug (exit 2, see test_heartbeat_with_missing_own_token_is_exit_2
    below) -- the caller must spell the sentinel explicitly."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    old_epoch = int(time.time()) - 100
    lock.write_text(
        f"12345 host {old_epoch} 2020-01-01T00:00:00Z someone-elses-token\n"
    )

    result = _run("heartbeat", BLIND, repo=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    fields = lock.read_text().split()
    assert fields[4] == "someone-elses-token"


@pytest.mark.parametrize("cmd", ["heartbeat", "release"])
@pytest.mark.parametrize(
    ("args", "needle"),
    [
        # Omitted entirely -> caught by the arg-COUNT check ("usage: ...").
        ((), "usage"),
        # Present but empty -> caught by the arg-VALUE check, which names the
        # sentinel. Both must be rejected, and both must leave the record alone.
        (("",), "requires a non-empty own-token"),
    ],
    ids=["omitted", "empty"],
)
def test_missing_or_empty_own_token_is_exit_2(
    tmp_path: Path, cmd: str, args: tuple[str, ...], needle: str
) -> None:
    """proj-yuwt: the argument is now REQUIRED on BOTH subcommands. Omitting it
    (or passing an empty string) is a caller bug, not a supported degraded
    mode -- land-lock.sh refuses outright (exit 2, a usage error, never a lock
    verdict) instead of silently falling back to the pre-proj-q9pm blind
    behaviour. The lock record itself must be untouched in every case: this is
    a rejection before any lock-file logic runs, so in particular `release`
    must NOT have removed the lock on its way out."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    old_epoch = int(time.time()) - 100
    lock.write_text(f"12345 host {old_epoch} 2020-01-01T00:00:00Z my-token\n")
    original = lock.read_text()

    result = _run(cmd, *args, repo=repo)

    assert result.returncode == 2, result.stdout + result.stderr
    assert needle in result.stderr
    assert lock.read_text() == original, f"{cmd} touched the lock despite exit 2"


def test_heartbeat_refuses_to_overwrite_a_lock_reclaimed_by_another_pass(
    tmp_path: Path,
) -> None:
    """The exact scenario this ticket's acceptance criteria name: pass A
    acquires (token A), pass A's lock goes stale and is reclaimed by pass B
    (token B), and pass A -- unaware it lost the lock -- calls heartbeat with
    its own remembered token A. heartbeat must NOT overwrite B's record: the
    mismatch between A (what this call believes it owns) and B (what the
    record on disk actually says) must refuse the write rather than silently
    re-stamping over the new holder, which is exactly the self-concealing
    overlap scripts/land-lock.sh's OWNERSHIP CHECK section describes."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)

    acquire_a = _run("acquire", repo=repo)
    assert acquire_a.returncode == 0, acquire_a.stdout + acquire_a.stderr
    token_a = acquire_a.stdout.strip().split("token ")[1].rstrip(")")

    # Simulate pass A's lock going stale and pass B reclaiming it -- write a
    # record directly (equivalent to a real reclaim's end state) naming a
    # DIFFERENT token, so the file now reflects pass B, not pass A.
    old_epoch = int(time.time()) - 1000
    lock.write_text(f"99999 other-host {old_epoch} 2020-01-01T00:00:00Z token-b\n")

    # Pass A, unaware it lost the lock, heartbeats with its OWN remembered
    # token (A) -- not what is currently on disk (B).
    heartbeat_a = _run("heartbeat", token_a, repo=repo)

    assert heartbeat_a.returncode == 1, heartbeat_a.stdout + heartbeat_a.stderr
    assert "REFUSING to overwrite" in heartbeat_a.stderr
    # The record on disk must still be pass B's, byte-for-byte -- this is the
    # actual assertion the scenario cares about, not just the exit code.
    assert (
        lock.read_text()
        == f"99999 other-host {old_epoch} 2020-01-01T00:00:00Z token-b\n"
    )


def test_heartbeat_with_matching_own_token_re_stamps_normally(tmp_path: Path) -> None:
    """The non-mismatch path: when the caller's own token DOES match the
    record's current owner, heartbeat behaves exactly as it always has --
    the ownership check is not a tax on the common case."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    old_epoch = int(time.time()) - 100
    lock.write_text(f"12345 host {old_epoch} 2020-01-01T00:00:00Z my-token\n")

    result = _run("heartbeat", "my-token", repo=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    fields = lock.read_text().split()
    assert fields[4] == "my-token"
    assert int(fields[2]) > old_epoch


def test_release_refuses_to_remove_a_lock_reclaimed_by_another_pass(
    tmp_path: Path,
) -> None:
    """The `release` half of the same scenario: pass A, displaced by pass B,
    must not delete B's live lock on its own way out. `release` still exits
    0 (its own always-exit-0 contract), but the file must survive."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    old_epoch = int(time.time()) - 1000
    lock.write_text(f"99999 other-host {old_epoch} 2020-01-01T00:00:00Z token-b\n")

    result = _run("release", "token-a", repo=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "REFUSING to remove" in result.stderr
    assert lock.exists(), "release deleted another pass's live lock"


def test_release_with_matching_own_token_removes_the_lock(tmp_path: Path) -> None:
    """The non-mismatch path for release: this pass's own token still owns
    the record, so release proceeds exactly as it always has."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    old_epoch = int(time.time()) - 100
    lock.write_text(f"12345 host {old_epoch} 2020-01-01T00:00:00Z my-token\n")

    result = _run("release", "my-token", repo=repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not lock.exists()


# ---------------------------------------------------------------------------
# Staleness reclaim -- the mechanism that replaces the dead-PID trap logic
# ---------------------------------------------------------------------------


def test_stale_lock_is_reclaimed(tmp_path: Path) -> None:
    """A lock recorded well past the staleness threshold is treated as an
    abandoned prior pass and reclaimed -- the self-healing half of the fix."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    old_epoch = int(time.time()) - 1000
    lock.write_text(f"12345 some-old-host {old_epoch} 2020-01-01T00:00:00Z\n")

    result = _run(
        "acquire", repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "500"}
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "reclaiming stale lock" in result.stdout
    # The lock file now reflects the NEW acquisition, not the stale one.
    new_fields = lock.read_text().split()
    assert int(new_fields[2]) > old_epoch


def test_fresh_lock_is_not_reclaimed_under_a_large_threshold(
    tmp_path: Path,
) -> None:
    """A lock younger than the threshold must never be reclaimed. (This is
    not a boundary test: pinning `>=` against `>` would mean asserting on a
    one-second window that `date` can cross mid-test, and the two differ by
    one second on the 1800s default -- operationally nothing.)"""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    recent_epoch = int(time.time())
    lock.write_text(f"12345 host {recent_epoch} 2026-01-01T00:00:00Z\n")

    result = _run(
        "acquire", repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "3600"}
    )

    assert result.returncode == 1, result.stdout + result.stderr
    # Untouched: still the original epoch.
    assert lock.read_text().split()[2] == str(recent_epoch)


def test_fresh_lock_with_an_unreachable_pid_is_not_reclaimed(tmp_path: Path) -> None:
    """Regression pin: a future edit reintroducing `kill -0 $OWNER_PID`
    liveness would reclaim this lock immediately, because PID 999999999
    almost certainly does not exist on any test machine -- even though the
    recorded timestamp is fresh. Liveness must be judged ONLY by the
    timestamp, never the pid field."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    recent_epoch = int(time.time())
    lock.write_text(f"999999999 some-host {recent_epoch} 2026-01-01T00:00:00Z\n")

    result = _run(
        "acquire", repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "1800"}
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "skipping this tick" in result.stderr


def test_malformed_lock_file_is_treated_as_still_held_not_reclaimed(
    tmp_path: Path,
) -> None:
    """A lock file that doesn't parse (no numeric epoch in the 3rd field) is
    age-unknown, not age-zero and not age-infinite -- the conservative
    reading is "still held", even under a threshold of 0, which would
    reclaim anything with a legible timestamp instantly."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    lock.write_text("this is not a lock record\n")

    result = _run("acquire", repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "0"})

    assert result.returncode == 1, result.stdout + result.stderr
    # Left exactly as it was -- never rewritten out from under an ambiguous read.
    assert lock.read_text() == "this is not a lock record\n"


def test_default_staleness_threshold_is_generous(tmp_path: Path) -> None:
    """No env override: a lock a couple of minutes old must NOT be reclaimed
    by the default threshold -- a genuinely still-running /land pass
    (dispatching several land-review subagents, then a combined re-gate) can
    easily take a few minutes between two Bash invocations."""
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)
    two_minutes_ago = int(time.time()) - 120
    lock.write_text(f"12345 host {two_minutes_ago} 2026-01-01T00:00:00Z\n")

    result = _run("acquire", repo=repo)

    assert result.returncode == 1, result.stdout + result.stderr


def test_default_staleness_threshold_is_still_about_1800s(tmp_path: Path) -> None:
    """Pin the DEFAULT itself, not just that it exceeds two minutes.

    Every other test here passes LAND_LOCK_STALE_SECONDS explicitly, and the
    test above passes for any default over 120s -- so before this pin, the one
    number carrying the whole safety margin could be lowered with nothing going
    red. It is not an arbitrary constant: proj-m87j's `heartbeat` bounds the
    *dangerous* direction (a live pass reclaimed mid-merge), but the window
    must still outlast the longest unheartbeated stretch, and the binding one
    -- a single `land-review` Opus dispatch -- has never been measured. Agent
    dispatches in this repo run to double-digit minutes, so a 600s window is
    the same order as the gap rather than clear of it; the reduction proj-m87j
    proposed was reverted for exactly that reason (see scripts/land-lock.sh,
    CAVEAT 1). Lowering it is proj-cp4o's job, and requires the measurement.

    Asserted behaviourally, from the outside: a lock 1700s old is still held,
    a lock 1900s old is stale. Deliberately not a grep for the literal -- this
    fails if the semantics change, not merely if the digits move.

    The 100s margins are deliberate, and were 1s until proj-44cq: the age is
    stamped from ONE clock read here, in Python, then measured against a
    SECOND, later read inside `land-lock.sh`'s own subprocess, so under load
    (`-n 8`, interpreter/bash startup, git work) that gap could exceed a
    second and age a 1799s lock past 1800s -- a false, one-sided red. This is
    the same hazard `test_fresh_lock_is_not_reclaimed_under_a_large_threshold`
    calls out for its own boundary; the margin is how this file has always
    handled it (every other threshold test here uses a 2x-or-wider ratio).

    The trade is real: the pin now catches only a default outside
    (1700s, 1900s), so a lowering to e.g. 1750s would slip through. Measured
    on this branch -- defaults of 900s and 3600s each turn it red, 1750s does
    not.

    Rejected as worse: injecting a pinned "now" into the script. That would
    put a test-only env var directly into the *decision* input of a
    production lock's staleness path -- unlike `LAND_LOCK_FLOCK_TIMEOUT_SECONDS`
    (used elsewhere in this file), which only bounds how long an `acquire`
    waits and cannot change which decision it reaches.
    """
    repo = _init_repo(tmp_path)
    lock = _lock_path(repo)

    lock.write_text(f"12345 host {int(time.time()) - 1700} 2026-01-01T00:00:00Z\n")
    well_inside = _run("acquire", repo=repo)
    assert well_inside.returncode == 1, (
        "a lock 1700s old was reclaimed -- LAND_LOCK_STALE_SECONDS was lowered "
        "well below 1800s. That is proj-cp4o's decision to make, with "
        "measurements (scripts/land-lock.sh, CAVEAT 1)."
    )

    lock.write_text(f"12345 host {int(time.time()) - 1900} 2026-01-01T00:00:00Z\n")
    well_outside = _run("acquire", repo=repo)
    assert well_outside.returncode == 0, (
        "a lock 1900s old was NOT reclaimed -- LAND_LOCK_STALE_SECONDS was "
        "raised well above 1800s, so an abandoned lock now blocks landing for "
        f"much longer than the documented 30min: "
        f"{well_outside.stdout + well_outside.stderr}"
    )


# ---------------------------------------------------------------------------
# Atomic reclaim (proj-y3dw) -- flock(1) closes the two-winner race outright
# ---------------------------------------------------------------------------


def test_concurrent_acquire_against_a_stale_lock_has_exactly_one_winner(
    tmp_path: Path,
) -> None:
    """The regression this ticket fixes: two concurrent `acquire` calls both
    exiting 0 against one stale lock. Reproduce the race, then assert every
    round has EXACTLY one winner.

    CONTENTION IS LOAD-BEARING -- do not lower it. 8-way (where the original
    `rm`-then-create defect was first measured) is NOT enough to cover this
    code: measured against the PRIOR mkdir-gate design (proj-ao95/proj-78ih):

        8-way,  200 rounds -> 0 multi-winner   (the bug is INVISIBLE here)
        32-way,  60 rounds -> 11 multi-winner  (~18%/round)

    So a version of this test at 8-way would have passed against code that
    still admitted two landers onto `main`. 32 workers x 40 rounds makes a
    regression a near-certainty to surface (P(miss) ~ 0.8^40) and costs a
    few seconds. The assertion is `== 1`, not `<= 1`, deliberately: a "fix"
    that simply blocked every racer would wedge landing outright while
    satisfying any check that only counted upwards.

    UNLIKE the mkdir-gate design this replaces, there is no starting-state
    caveat here: `flock(1)` serializes the WHOLE acquire decision (fresh
    attempt, staleness check, reclaim) behind one kernel mutex, so there is
    no gate OBJECT whose age or ownership a racer can misjudge, and no
    "already-abandoned gate" arrangement that reaches a residual the mkdir
    design could not close (proj-y3dw MEASURED that arrangement admitting 2
    of 150 rounds of multiple winners against the mkdir-gate script, at this
    same 32-way/28-way-saturation level, with no stall required). This test
    is therefore sound at ANY starting arrangement -- the whole point of a
    real mutex over a hand-rolled gate.
    """
    repo = _init_repo(tmp_path)
    rounds = 40
    workers = 32
    # Hoisted, not per-round: a Barrier resets itself once all `workers`
    # parties have tripped it, and every round trips it exactly once with
    # exactly that many.
    barrier = threading.Barrier(workers)

    def _race() -> subprocess.CompletedProcess:
        barrier.wait(timeout=30)
        return _run(
            "acquire", repo=repo, env_overrides={"LAND_LOCK_STALE_SECONDS": "1800"}
        )

    for round_num in range(rounds):
        _write_stale_lock(repo)

        # Release every racer from the same barrier. Without it each thread
        # reaches its own `subprocess.run` whenever the GIL and the fork storm
        # let it, which staggers the starts by milliseconds -- enough to
        # thin the interleaving badly: measured against a deliberately
        # reverted re-validation on the prior mkdir-gate design, the
        # un-barriered version detected the regression in only 4 of 6 runs,
        # and a ~30% miss rate on a two-lander bug is not a gate. With the
        # barrier it is 10 of 10. Retained here even though flock's own
        # exclusivity does not depend on interleaving timing -- it is what
        # makes 32 genuinely concurrent attempts land inside the tiny
        # acquire-decision window each round, which is the point of the test.
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_race) for _ in range(workers)]
            results = [f.result() for f in futures]

        winners = [r for r in results if r.returncode == 0]
        assert len(winners) == 1, (
            f"round {round_num}: {len(winners)} winners (expected exactly 1)\n"
            + "\n".join(
                f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}" for r in results
            )
        )


def test_a_slow_holder_blocks_a_concurrent_acquire_rather_than_racing_it(
    tmp_path: Path,
) -> None:
    """The direct behavioural pin for what `flock -x -w` buys over the mkdir
    gate: a SECOND `acquire` that arrives while the first is still inside the
    flock'd section must BLOCK (and, on timeout, skip the tick) rather than
    being handed any chance to interleave with it -- there is no window here
    for a second winner to appear in, unlike the old gate's aging/self-heal
    machinery.

    Stages a real holder of the flock without any script-level test hook
    (retired along with the mkdir gate, proj-y3dw): a background Python
    thread takes an exclusive `fcntl.flock` directly on `$LOCK.flock`, held
    for `hold_seconds`, standing in for "another /land pass is inside its own
    acquire". A concurrent `acquire` invocation with a short
    `LAND_LOCK_FLOCK_TIMEOUT_SECONDS` must then time out and skip the tick --
    never race ahead and reclaim/write regardless -- and the lock record
    itself must be untouched afterward.
    """
    repo = _init_repo(tmp_path)
    _write_stale_lock(repo)
    lock = _lock_path(repo)
    original_record = lock.read_text()
    flock_file = lock.with_name(lock.name + ".flock")

    hold_seconds = 2
    released = threading.Event()

    def _hold() -> None:
        with open(flock_file, "w") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            time.sleep(hold_seconds)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            released.set()

    holder = threading.Thread(target=_hold)
    holder.start()
    time.sleep(0.2)  # let the holder actually take the flock before racing it
    try:
        result = _run(
            "acquire",
            repo=repo,
            env_overrides={
                "LAND_LOCK_STALE_SECONDS": "1800",
                "LAND_LOCK_FLOCK_TIMEOUT_SECONDS": "1",
            },
        )
    finally:
        holder.join(timeout=hold_seconds + 5)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "skipping this tick" in result.stderr
    assert released.is_set(), "the background holder never actually released the flock"
    # Never got far enough to read, let alone reclaim, the lock record.
    assert lock.read_text() == original_record


def test_flock_missing_from_path_is_a_reported_machine_fault(tmp_path: Path) -> None:
    """proj-y3dw's portability fallback: `acquire` must refuse to proceed
    when `flock(1)` is not on PATH (macOS, stock git-bash) rather than
    silently falling back to the two-winner-capable pre-flock behaviour."""
    repo = _init_repo(tmp_path)
    path_without_flock = _path_without_flock(tmp_path)

    # Non-vacuous: confirm the shimmed PATH genuinely cannot resolve `flock`
    # before trusting the assertion below to mean anything.
    probe = subprocess.run(
        ["bash", "-c", "command -v flock"],
        env={**os.environ, "PATH": path_without_flock},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert probe.returncode != 0, (
        "the shimmed PATH still resolves flock -- _path_without_flock did not "
        f"actually exclude it: {probe.stdout!r}"
    )

    result = _run("acquire", repo=repo, env_overrides={"PATH": path_without_flock})

    assert result.returncode == 1, result.stdout + result.stderr
    assert "MACHINE FAULT" in result.stderr
    assert "flock" in result.stderr
    assert not _lock_path(repo).exists()


def test_every_write_lock_call_site_is_inside_the_flocked_section() -> None:
    """Structural pin for "route 2" -- the FRESH path must be inside the mutex.

    proj-y3dw's whole thesis is that BOTH acquire paths (fresh attempt and
    reclaim) execute inside ONE mutex. The measured route 2 it closed was
    precisely the fresh path executing OUTSIDE the reclaim's serialization:
    a top-of-script `write_lock` landing between a reclaimer's staleness
    decision and its `rm -f "$LOCK"` wins, and then the reclaimer's `rm`
    destroys that record and its own `write_lock` succeeds into the hole --
    two winners.

    This is pinned STRUCTURALLY rather than behaviourally, and that is a
    deliberate choice rather than a shortcut. MEASURED during proj-y3dw's
    technical review: with the fresh `write_lock` moved back above the
    `flock` acquisition (route 2 reintroduced verbatim, portability check
    left in place), the ENTIRE module -- including the 32-way x 40-round
    barriered stress test -- stayed green, 31 passed. The two-winner window
    is real but narrow and timing-dependent, so a stochastic test does not
    gate it at any contention level this suite can afford; and the obvious
    extra stress arrangement does NOT help either -- starting a round with
    NO lock present still yields exactly one winner even with route 2
    reintroduced, because `write_lock`'s `noclobber` is itself atomic. What
    actually distinguishes the two designs is WHERE the call sits, so that
    is what this asserts.

    Deliberately tolerant of reformatting: it compares line ORDER of real
    call sites against the `flock` acquisition, not exact source text.
    """
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()

    def _code(line: str) -> str:
        return "" if line.lstrip().startswith("#") else line

    flock_lines = [
        i for i, ln in enumerate(lines) if re.search(r"^\s*if ! flock ", _code(ln))
    ]
    assert len(flock_lines) == 1, (
        f"expected exactly one `flock` acquisition in {SCRIPT.name}, found "
        f"{len(flock_lines)} at lines {[i + 1 for i in flock_lines]} -- if the "
        "script legitimately grew a second one, this pin needs updating "
        "deliberately, not silently."
    )
    flock_line = flock_lines[0]

    # Call sites only -- never the `write_lock() {` definition, never prose.
    call_sites = [
        i
        for i, ln in enumerate(lines)
        if re.search(r"^\s*(if\s+)?write_lock\s+\"?\$", _code(ln))
    ]
    assert call_sites, (
        "found no `write_lock` call sites at all -- this pin has gone vacuous "
        f"against {SCRIPT.name}; re-derive it against the current script."
    )

    early = [i + 1 for i in call_sites if i < flock_line]
    assert not early, (
        f"`write_lock` is called at line(s) {early} of {SCRIPT.name}, BEFORE "
        f"the flock is taken at line {flock_line + 1}. That reintroduces "
        "proj-y3dw's measured route 2: the fresh-lock path would no longer be "
        "serialized against a concurrent reclaim's rm+write, so two /land "
        "passes can both believe they hold the lock and write `main` at once. "
        "The whole point of the flock is that the ENTIRE acquire decision -- "
        "fresh attempt included -- runs inside it."
    )


# ---------------------------------------------------------------------------
# Usage errors
# ---------------------------------------------------------------------------


def test_no_args_is_exit_2(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    result = _run(repo=repo)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "usage" in result.stderr


def test_unknown_subcommand_is_exit_2(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    result = _run("status", repo=repo)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "usage" in result.stderr


# ---------------------------------------------------------------------------
# Machine fault vs. "another lander"
# ---------------------------------------------------------------------------


def test_uncreatable_lock_reports_a_machine_fault_not_another_lander(
    tmp_path: Path,
) -> None:
    """`write_lock` discards its own stderr, so a lock that cannot be created
    at all (unwritable git dir, full disk) is indistinguishable from one that
    already exists unless the script checks. Reporting it as "another /land is
    running" would block landing indefinitely behind a lander that does not
    exist, every tick, with nothing naming the real cause -- proj-aps3's own
    notes require "lock was not held" to be observable rather than silent."""
    repo = _init_repo(tmp_path)
    git_dir = repo / ".git"
    original_mode = git_dir.stat().st_mode
    git_dir.chmod(0o500)  # readable + traversable, not writable
    try:
        result = _run("acquire", repo=repo)
    finally:
        git_dir.chmod(original_mode)  # or tmp_path teardown fails

    assert result.returncode == 1, result.stdout + result.stderr
    assert "MACHINE FAULT" in result.stderr
    assert "another /land" not in result.stderr
    assert not _lock_path(repo).exists()


# ---------------------------------------------------------------------------
# Persistent MACHINE FAULT escalation (proj-oup2)
#
# The counter is deliberately NOT under $GIT_COMMON_DIR (see land-lock.sh's own
# comment beside LAND_LOCK_FAULT_COUNT_FILE) -- it lives under $TMPDIR, so every
# test here overrides TMPDIR to a throwaway, per-test directory. Without that
# override these tests would pollute -- and be polluted by -- the real
# machine-wide counter file (including a concurrent test run, or a real /land
# loop on the same box).
# ---------------------------------------------------------------------------


#: The throwaway $TMPDIR every test below redirects land-lock.sh at. Named once
#: -- _fault_env and _fault_count_path must never disagree about it, or an
#: `assert not ...exists()` would pass against a path the script never writes.
_FAULT_TMPDIR_NAME = "fault-tmpdir"


def _fault_env(tmp_path: Path) -> dict[str, str]:
    fault_tmp = tmp_path / _FAULT_TMPDIR_NAME
    fault_tmp.mkdir(exist_ok=True)
    return {"TMPDIR": str(fault_tmp), "LAND_LOCK_FAULT_ESCALATE_THRESHOLD": "3"}


def _fault_count_path(tmp_path: Path) -> Path:
    return tmp_path / _FAULT_TMPDIR_NAME / "harness-land-lock-fault-count"


def _faulty_repo_and_env(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A repo plus the env that makes every `acquire` in it hit a MACHINE FAULT
    (no `flock` on $PATH), with the counter redirected into the throwaway
    $TMPDIR. The three-line preamble every test below would otherwise repeat."""
    return _init_repo(tmp_path), {
        **_fault_env(tmp_path),
        "PATH": _path_without_flock(tmp_path),
    }


def test_a_single_machine_fault_does_not_escalate(tmp_path: Path) -> None:
    """Below threshold: MACHINE FAULT is still reported, but no ESCALATE line."""
    repo, env = _faulty_repo_and_env(tmp_path)

    result = _run("acquire", repo=repo, env_overrides=env)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "MACHINE FAULT" in result.stderr
    assert "ESCALATE" not in result.stderr
    assert _fault_count_path(tmp_path).read_text().strip() == "1"


def test_consecutive_machine_faults_escalate_at_the_threshold(
    tmp_path: Path,
) -> None:
    """Three consecutive MACHINE FAULT acquires (the default threshold) must
    escalate on the third; the first two must not."""
    repo, env = _faulty_repo_and_env(tmp_path)

    first = _run("acquire", repo=repo, env_overrides=env)
    second = _run("acquire", repo=repo, env_overrides=env)
    third = _run("acquire", repo=repo, env_overrides=env)

    assert "ESCALATE" not in first.stderr, first.stderr
    assert "ESCALATE" not in second.stderr, second.stderr
    assert "ESCALATE" in third.stderr, third.stderr
    assert "MACHINE FAULT" in third.stderr
    assert _fault_count_path(tmp_path).read_text().strip() == "3"


def test_escalation_keeps_repeating_past_the_threshold(tmp_path: Path) -> None:
    """Not just the first crossing -- a caller checking only the LATEST tick's
    output must still see it, so every tick at or past threshold announces.

    Seeds the counter file directly rather than re-driving three faulted
    acquires (the test above already pins the crossing itself, and each real
    acquire costs a fresh $PATH-without-flock symlink farm)."""
    repo, env = _faulty_repo_and_env(tmp_path)
    _fault_count_path(tmp_path).write_text("7\n", encoding="utf-8")

    result = _run("acquire", repo=repo, env_overrides=env)

    assert "ESCALATE" in result.stderr, result.stderr
    assert _fault_count_path(tmp_path).read_text().strip() == "8", (
        "a tick already past the threshold must still keep counting up"
    )


def test_a_clean_acquire_resets_the_fault_counter(tmp_path: Path) -> None:
    """A machine fault, then a machine that recovers: the counter must NOT
    carry over silently -- otherwise two isolated faults years apart could
    eventually cross the threshold together, which is not what "consecutive"
    means."""
    repo, faulty_env = _faulty_repo_and_env(tmp_path)
    healthy_env = _fault_env(tmp_path)

    faulted = _run("acquire", repo=repo, env_overrides=faulty_env)
    assert faulted.returncode == 1, faulted.stdout + faulted.stderr
    assert _fault_count_path(tmp_path).read_text().strip() == "1"

    healed = _run("acquire", repo=repo, env_overrides=healthy_env)
    assert healed.returncode == 0, healed.stdout + healed.stderr
    assert not _fault_count_path(tmp_path).exists()

    _run("release", BLIND, repo=repo, env_overrides=healthy_env)
    refaulted = _run("acquire", repo=repo, env_overrides=faulty_env)
    assert refaulted.returncode == 1, refaulted.stdout + refaulted.stderr
    assert "ESCALATE" not in refaulted.stderr, (
        "counter carried over across the healthy acquire in between -- it "
        "must have reset to 0, not merely paused"
    )
    assert _fault_count_path(tmp_path).read_text().strip() == "1"


def test_a_transient_lock_held_skip_resets_the_fault_counter_not_bumps_it(
    tmp_path: Path,
) -> None:
    """`skip_lock_still_held` (a fresh lock genuinely held by another /land)
    is NOT a MACHINE FAULT -- the flock, the lock dir, and the lock file all
    worked fine. It must reset the counter (proof the machine is healthy),
    never bump it."""
    repo, faulty_env = _faulty_repo_and_env(tmp_path)
    healthy_env = _fault_env(tmp_path)

    faulted = _run("acquire", repo=repo, env_overrides=faulty_env)
    assert faulted.returncode == 1, faulted.stdout + faulted.stderr
    assert _fault_count_path(tmp_path).read_text().strip() == "1"

    held = _run("acquire", repo=repo, env_overrides=healthy_env)
    assert held.returncode == 0, held.stdout + held.stderr  # fresh acquire

    still_held = _run("acquire", repo=repo, env_overrides=healthy_env)
    assert still_held.returncode == 1, still_held.stdout + still_held.stderr
    assert "another /land appears to still be running" in still_held.stderr
    assert "MACHINE FAULT" not in still_held.stderr
    assert not _fault_count_path(tmp_path).exists(), (
        "a transient 'lock held' skip must reset the fault counter, not "
        "leave it untouched or bump it"
    )


def test_fault_escalate_threshold_is_configurable(tmp_path: Path) -> None:
    repo, faulty_env = _faulty_repo_and_env(tmp_path)
    env = {**faulty_env, "LAND_LOCK_FAULT_ESCALATE_THRESHOLD": "1"}

    result = _run("acquire", repo=repo, env_overrides=env)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "ESCALATE" in result.stderr, result.stderr


def test_fault_counter_is_not_scoped_under_git_common_dir(tmp_path: Path) -> None:
    """Deliberate design choice (proj-oup2): if the counter lived under
    $GIT_COMMON_DIR it would fail to persist for exactly the MACHINE FAULT
    class it exists to track (an unwritable/missing git dir) -- self-defeating.
    Pin that it lives under $TMPDIR instead, and specifically that an
    unwritable .git/ does not prevent the counter from being written."""
    repo = _init_repo(tmp_path)
    git_dir = repo / ".git"
    original_mode = git_dir.stat().st_mode
    git_dir.chmod(0o500)  # readable + traversable, not writable
    try:
        result = _run("acquire", repo=repo, env_overrides=_fault_env(tmp_path))
    finally:
        git_dir.chmod(original_mode)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "MACHINE FAULT" in result.stderr
    assert _fault_count_path(tmp_path).read_text().strip() == "1", (
        "the fault counter must persist even when .git/ itself is unwritable "
        "-- that is precisely the fault it needs to survive to count"
    )


# ---------------------------------------------------------------------------
# Call-site pins against the SHIPPED SKILL.md (the fence is where the bug was)
# ---------------------------------------------------------------------------


def test_land_skill_acquires_and_releases_through_this_script() -> None:
    """The lock only serializes a pass that actually calls this script, and
    every call site is prose in a markdown fence that no gate parses. Pin the
    shipped file (the tests/_hookharness.py precedent: assert what is
    committed, never a reimplementation)."""
    text = LAND_SKILL_TEXT

    assert "scripts/land-lock.sh acquire" in text, (
        "land/SKILL.md never acquires the single-lander lock -- overlapping "
        "/loop 5m /land ticks would both write main (proj-aps3)"
    )
    # Both explicit release sites: Section 1's empty-queue exit and the end of
    # Section 4. Losing one is not a correctness bug (the TTL still reclaims)
    # but it silently costs up to LAND_LOCK_STALE_SECONDS of blocked landing.
    assert text.count("scripts/land-heartbeat.sh --release") >= 2, (
        "land/SKILL.md lost one of its two explicit `release` call sites -- "
        "that pass now waits out the whole staleness window instead"
    )


def test_land_skill_escalation_stderr_capture_not_under_git_dir() -> None:
    """proj-oup2's Section 0 failure branch captures `scripts/land-lock.sh
    acquire`'s stderr to a scratch file so it can grep it for the
    'land-lock: ESCALATE' marker. That file MUST NOT live under $STATE_DIR /
    the git dir: the technical review found the original implementation at
    $STATE_DIR/land-lock-acquire-stderr, which fails BEFORE `acquire` even
    runs in exactly the unwritable-git-dir MACHINE FAULT this feature exists
    to escalate -- no counter bump, no ESCALATE marker, and proj-119w's own
    diagnostic lost too. A future edit could silently move it back under
    $STATE_DIR without anything else here catching it."""
    executed = LAND_SKILL_BASH

    match = re.search(r'ACQUIRE_ERR_FILE="([^"]+)"', executed)
    assert match is not None, (
        "land/SKILL.md no longer defines ACQUIRE_ERR_FILE -- Section 0's "
        "escalation branch has nothing to grep for 'land-lock: ESCALATE', so "
        "a persistent MACHINE FAULT silently stops reaching a human at all"
    )
    acquire_err_file = match.group(1)

    # Every spelling of "somewhere under the git dir" -- $STATE_DIR, the
    # `git rev-parse --git-dir` derivation, and the raw env vars -- fails the
    # same way, so they are one assertion rather than one per spelling.
    for git_dir_spelling in ("STATE_DIR", "git-dir", "GIT_DIR", "GIT_COMMON_DIR"):
        assert git_dir_spelling not in acquire_err_file, (
            f"ACQUIRE_ERR_FILE is defined in terms of ${git_dir_spelling} -- "
            f"it resolves under the git dir, and on exactly the "
            "unwritable-git-dir MACHINE FAULT this feature escalates, "
            "redirecting stderr there fails BEFORE `acquire` runs at all: no "
            "counter bump, no ESCALATE marker, and proj-119w's own diagnostic "
            "is lost too, so the fault escalates LESS visibly than it did "
            f"before proj-oup2 shipped. Got: {acquire_err_file}"
        )


def test_land_skill_escalation_ticket_dedup_has_no_status_open() -> None:
    """proj-oup2's dedup lookup (before filing the human-labeled escalation
    ticket) must NOT carry `--status open`: `bd list` already excludes closed
    issues on its own, and pinning `--status open` would miss this very
    ticket once a human moves it to in_progress/blocked while investigating
    -- silently duplicating it on every subsequent /loop 5m /land tick until
    the fault clears."""
    executed = LAND_SKILL_BASH

    # Anchor on the specific `bd list --label human` lookup this feature
    # added, not just any `bd list` call in the file, so this test fails on
    # the right line if a second, unrelated `bd list --status open` call is
    # ever added elsewhere in the skill. The match spans the WHOLE logical
    # command, following `\`-continuations: the call is written across two
    # physical lines, so stopping at the first newline would miss a
    # `--status open` added on the continuation.
    match = re.search(r"bd list --label human(?:[^\n]*\\\n)*[^\n]*", executed)
    assert match is not None, (
        "land/SKILL.md no longer runs the `bd list --label human` dedup "
        "lookup before filing the escalation ticket -- without it a "
        "persistent MACHINE FAULT files a fresh duplicate ticket on every "
        "tick instead of filing once per fault episode"
    )
    assert "--status open" not in match.group(0), (
        "the escalation ticket's dedup lookup (`bd list --label human ...`) "
        "carries --status open -- it would miss the escalation ticket once "
        "a human moves it to in_progress/blocked while investigating, and "
        "/land would then file a fresh duplicate every tick until the fault "
        "clears"
    )


def test_land_skill_escalation_ticket_filed_exactly_once() -> None:
    """The human-labeled escalation ticket must be filed by exactly ONE `bd
    create` call site, and that call must sit inside the not-found guard (the
    dedup lookup's result is empty) -- never refreshed per tick. A fault that
    persists for days is thousands of /loop 5m /land ticks; a second call
    site, or one that fires unconditionally, would grow the ticket's history
    or commit to Dolt every 5 minutes for information a human already has."""
    executed = LAND_SKILL_BASH

    creates = list(re.finditer(r"bd create --type=decision --label=human", executed))
    assert len(creates) == 1, (
        f"expected exactly ONE `bd create --type=decision --label=human` "
        f"call site for the escalation ticket, found {len(creates)} -- a "
        "second site (or a dropped one) means the ticket is either filed "
        "more than once per fault episode or never filed at all"
    )

    # The call must be textually inside the not-found guard's BODY. The
    # guard is nested (indented) inside Section 0's `|| { ... }` failure
    # branch, so its closing `fi` carries the same indentation as its `if` --
    # match on that, rather than on the next line-initial `fi`, which lands
    # ~7900 chars away in an unrelated section and would admit a `bd create`
    # moved clean outside the guard.
    guard = re.search(
        r'^(?P<indent>[ \t]*)if \[ -z "\$EXISTING_ESCALATION" \]; then\n'
        r"(?P<body>.*?)"
        r"^(?P=indent)fi$",
        executed,
        re.DOTALL | re.MULTILINE,
    )
    assert guard is not None, (
        'land/SKILL.md no longer has the `if [ -z "$EXISTING_ESCALATION" ]; '
        "then ... fi` not-found guard (or its closing `fi` no longer lines up "
        "with its `if`) -- without that guard the escalation ticket is refiled "
        "on every tick instead of once per fault episode"
    )
    assert creates[0].group(0) in guard.group("body"), (
        "the `bd create` call for the escalation ticket is not inside the "
        '`if [ -z "$EXISTING_ESCALATION" ]; then ... fi` not-found guard -- '
        "an unguarded (or misplaced) call would refresh/refile the ticket on "
        "every tick instead of filing it once per fault episode"
    )


def test_land_skill_heartbeats_the_lock_once_per_ticket_in_section_2a() -> None:
    """proj-m87j: the vet loop (Section 2a) heartbeats the lock as the first
    action of every iteration, so the staleness window measures the gap since
    the last ticket's dispatch rather than the whole pass's duration. A dropped
    call site silently reintroduces the acquisition-age exposure this ticket
    exists to close -- pinned the same way acquire and release are above.

    Scope, stated so a reader does not over-trust it: this pins that the call
    EXISTS (and its companion below, that it exists inside an executable
    fence). Neither pins WHERE -- that it is in Section 2a, that it is first in
    the loop body, or that the loop runs once per ticket. Markdown call sites
    have no better mechanical gate available here; placement rests on review.
    """
    text = LAND_SKILL_TEXT

    # NOT a bare `"scripts/land-heartbeat.sh" in text`: that also matches the
    # two `--release` lines, so the pin would stay green with every real
    # heartbeat call site deleted. Match the script NOT followed by --release.
    assert re.search(r"scripts/land-heartbeat\.sh(?!\s*--release)", text), (
        "land/SKILL.md never heartbeats the single-lander lock -- the TTL is "
        "back to measuring acquisition age, not idle time (proj-m87j)"
    )


def test_land_skill_heartbeats_at_both_new_boundary_call_sites() -> None:
    """proj-v4sv closed the two originally-uncovered stretches that grew with
    queue size (scripts/land-lock.sh's CAVEAT 1: gap (a), Section 0's acquire
    -> the first Section 2a heartbeat; gap (c), the last Section 3 merge
    heartbeat -> Section 4's release) by adding TWO boundary heartbeat call
    sites in land/SKILL.md, on top of the two loop-bracketing sites
    test_land_skill_heartbeats_the_lock_once_per_ticket_in_section_2a already
    pins the mere existence of. That test alone is silently blind to either
    NEW site quietly being dropped later (existence of >=1 call stays true
    even if a specific site vanishes) -- pin the COUNT instead, so a future
    edit that drops one of the THREE in-skill sites is caught here rather than
    by a live /land pass losing its lock mid-pass. (The fourth call site of
    the four CAVEAT 1 enumerates lives in `scripts/land-merge-one.sh`, not in
    the skill, and is pinned by tests/test_land_merge_one.py.)

    A count alone can't prove WHICH site vanished if it drops, so this also
    pins each new site's TEXTUAL POSITION relative to a fixed landmark next to
    it -- gap (a)'s new call must appear before the "## 1a." heading (so it
    covers Section 1 and precedes Section 1a's O(n^2) work); gap (c)'s new
    call must appear after "git push origin main" and before the per-ticket
    `bd close` loop's own `for id in $LANDED` line (so it covers the push and
    precedes the per-ticket work). Both landmarks are pinned elsewhere in this
    file / test_land_conflicts_state.py for unrelated reasons, so this test
    does not introduce a new fragile anchor on its own.
    """
    text = LAND_SKILL_TEXT

    positions = [
        m.start()
        # Line-anchored so a backticked prose mention cannot pad the count, but
        # NOT anchored at the line END: `|| true` is exactly the best-effort
        # spelling the surrounding prose describes, and an indented call site
        # (inside the Red branch's nested fence) is equally legitimate. The
        # `--release` lookahead keeps the two release sites out of a HEARTBEAT
        # count.
        for m in re.finditer(
            r"^[ \t]*scripts/land-heartbeat\.sh(?!\s*--release)", text, re.MULTILINE
        )
    ]
    assert len(positions) == 3, (
        f"expected exactly 3 in-skill 'scripts/land-heartbeat.sh' call sites (Section 1 -> 1a boundary [proj-v4sv], Section 2a's "
        f"per-ticket vet loop, Section 4's push-main -> release boundary "
        f"[proj-v4sv]), found {len(positions)}. A dropped site silently re-widens "
        "one of the two queue-size-growing gaps proj-v4sv closed."
    )

    heading_1a = text.index("## 1a. Compute the stacked-branch graph")
    assert positions[0] < heading_1a, (
        "gap (a)'s new heartbeat call site must sit strictly between the end "
        "of Section 1 and the '## 1a.' heading -- otherwise Section 1a's "
        "O(n^2) merge-base work is no longer inside the covered stretch "
        "(proj-v4sv acceptance criteria)"
    )

    # Anchor on the EXECUTED `git push origin main` line (own line, no
    # backticks), not the prose mention of it earlier in Section 4.
    push_trunk = text.index("\ngit push origin main\n")
    landed_loop = text.index("for id in $LANDED; do\n  bd close")
    assert push_trunk < positions[2] < landed_loop, (
        "gap (c)'s new heartbeat call site must sit strictly between "
        "'git push origin main' and the per-ticket 'bd close' loop -- "
        "otherwise the per-ticket bd close / epic-completion-check.sh / "
        "bd-dolt-push.sh / branch-delete / worktree-GC work is no longer "
        "inside the covered stretch (proj-v4sv acceptance criteria)"
    )


# `<own-token>` is REQUIRED on `heartbeat`/`release` as of proj-yuwt --
# land-lock.sh itself now refuses (exit 2) a bare/empty argument, so a call
# site that forgot it fails LOUDLY at the script layer. These pins stay as a
# second, cheaper layer that catches the same drift earlier and more legibly
# (a failing test naming the exact offending line, rather than a live /land
# pass discovering it at exit 2) -- proj-yuwt's own scope note keeps them
# rather than deleting them for exactly this reason. A call site may opt out
# of supplying a real token ONLY by carrying the `land-lock-blind-ok` marker
# with a stated reason AND spelling the explicit `--land-lock-blind`
# sentinel -- today exactly one does: Section 0's bail-out release, which
# fires when the token could not be parsed at all and so has none to supply.
_BLIND_OK = "land-lock-blind-ok"


def test_land_skill_persists_its_own_acquire_token_for_later_blocks() -> None:
    """Section 0 must capture `acquire`'s printed token and write it to
    `$(git rev-parse --path-format=absolute --git-common-dir)/land-lock-token`
    -- deliberately OUTSIDE `$STATE_DIR` (proj-l7mj): Section 1's
    `rm -rf "$STATE_DIR"` would otherwise destroy it before any consumer
    reads it (it did, in production, on every pass -- see the mechanical
    execution test below). Nothing else can carry the token forward: no
    shell state survives to the later, separate Bash invocations that
    heartbeat and release (proj-sfnb), so if this write is lost every later
    call site reads an empty token and silently degrades to the blind,
    pre-proj-q9pm behaviour. `--path-format=absolute --git-common-dir`, not
    bare `--git-dir`, is required as of proj-k6h0: bare `--git-dir` is
    worktree-PRIVATE, so a linked worktree's write would orphan the token
    the shared reader (scripts/land-heartbeat.sh) looks for."""
    executed = LAND_SKILL_BASH

    # Match the WRITE specifically, not a bare mention of the filename: the
    # read-back sites name that same path four more times, so an `in executed`
    # check stays green with the write itself deleted (measured by sabotage --
    # it did).
    assert re.search(
        r">\s*\"\$\(git rev-parse --path-format=absolute --git-common-dir\)"
        r'/land-lock-token"',
        executed,
    ), (
        "land/SKILL.md never WRITES $(git rev-parse --path-format=absolute "
        "--git-common-dir)/land-lock-token -- every later heartbeat/release "
        "then reads an empty token and the proj-q9pm ownership check is "
        "silently disabled for the whole pass (or, from a linked worktree, "
        "orphaned entirely -- proj-k6h0)"
    )
    assert '"$STATE_DIR/land-lock-token"' not in executed, (
        "land/SKILL.md still writes or reads the token under $STATE_DIR -- "
        'Section 1\'s `rm -rf "$STATE_DIR"` wipes it before any consumer '
        "reads it (proj-l7mj, the exact bug this shape (c') fixes)"
    )


def test_every_land_lock_heartbeat_and_release_call_site_supplies_its_own_token() -> (
    None
):
    """The pin that actually holds proj-q9pm up, kept as a second layer
    alongside land-lock.sh's own required-argument enforcement (proj-yuwt) --
    a bare `land-lock.sh heartbeat`/`release` with no real token now fails
    loudly at the script layer too, but this test catches the drift earlier
    and names the exact offending line. A line may opt out of a real token
    ONLY by carrying the `land-lock-blind-ok` marker with a stated reason AND
    spelling the explicit `--land-lock-blind` sentinel -- today exactly one
    does: Section 0's bail-out release, which fires when the token could not
    be parsed at all and so has none to supply."""
    executed = LAND_SKILL_BASH

    offenders = [
        line.strip()
        for line in executed.splitlines()
        if re.search(r"land-lock\.sh (heartbeat|release)(\s|$)", line)
        and not re.search(r"land-lock\.sh (heartbeat|release)\s+\"\$MY_TOKEN\"", line)
        # An opt-out must BOTH carry the marker (so a human reviewing the
        # skill sees a stated reason) and spell the sentinel (so the call
        # actually runs -- as of proj-yuwt a bare heartbeat/release exits 2,
        # which the marker alone would not prevent).
        and not (_BLIND_OK in line and BLIND in line)
    ]

    assert not offenders, (
        f"land/SKILL.md heartbeat/release call site(s) supply no own-token: "
        f"{offenders}. Since proj-yuwt land-lock.sh refuses such a call outright "
        "(exit 2), so the call site does not degrade to the pre-proj-q9pm blind "
        'behaviour -- it stops working. Pass `"$MY_TOKEN"` (re-read from '
        f"$(git rev-parse --path-format=absolute --git-common-dir)"
        f"/land-lock-token in that same block), or -- "
        f"only if it genuinely has no token to supply -- mark the line "
        f"`{_BLIND_OK}` with a reason AND pass the explicit `{BLIND}` sentinel."
    )


def test_land_skill_threads_its_own_token_into_land_merge_one() -> None:
    """`scripts/land-merge-one.sh` heartbeats on every invocation and cannot read
    $STATE_DIR on its own, so the token has to be threaded to it. Both merge paths
    are scripts now (land-merge-batch.sh, land-replay.sh) and both take it as
    --own-token; a call site that drops the flag makes every merge under it
    heartbeat blind."""
    calls = [
        b
        for b in LAND_SKILL_BLOCKS
        if "land-merge-batch.sh" in b or "land-replay.sh" in b
    ]
    assert len(calls) == 2, (
        f"expected both merge-script call sites (first pass + isolation replay), "
        f"found {len(calls)} -- has the skill's layout drifted?"
    )
    offenders = [c for c in calls if "--own-token" not in c]
    assert not offenders, (
        "a merge-script call site omits --own-token, so land-merge-one.sh heartbeats "
        "blind for every branch it merges"
    )


def test_fenced_bash_sees_every_bash_marker_including_indented_ones() -> None:
    """Regression pin for proj-ovgs, independent of `_fenced_bash`/
    `bash_fence_blocks` itself: derive the EXPECTED fence count a second,
    unrelated way (a plain stripped-line ```bash/```sh marker count) and
    assert the parser's own block count matches it.

    This is the shape the ticket asked for explicitly: a startswith-anchored
    parser undercounts (it saw 20 of 24 blocks against the shipped file when
    this test was written -- 4 indented fences invisible to it), so this
    test would have failed against the pre-fix parser without needing to
    know in advance which blocks are indented or where.

    Sabotage recipe (recorded per this repo's non-vacuous-test standard):
    in tests/conftest.py's `bash_fence_blocks`, replace `stripped = line.strip()`
    with `stripped = line` (i.e. stop stripping before the fence-marker check,
    reintroducing the exact column-0-anchored bug this ticket fixes). Every
    caller of `bash_fence_blocks` -- this test included -- then parses only 20
    blocks against this file instead of 24, and this test goes red reporting
    exactly that mismatch. Restoring `stripped = line.strip()` turns it green
    again. Verified by hand against this exact file while writing this ticket.

    The independent count must recognize the same FENCE SHAPES the parser
    does, or it stops being an independent second derivation and becomes a
    stale narrower one: proj-p4qb widened `bash_fence_blocks` to open on
    four-or-more backticks and on `~~~`, and against an exactly-three-backtick
    marker count the first author to write the four-backtick form -- the one
    the parser's own docstring says an author MUST use for a block containing
    a ```-prefixed line -- would fail this test on a CORRECT parse, with a
    message blaming the parser for missing a fence it actually found.
    Measured while widening it: one added four-backtick (or tilde) bash fence
    took land/SKILL.md to 25 parsed against 24 markers. What stays independent
    is the METHOD -- a flat per-line marker count, no state machine, no call
    into the parser -- not the grammar.

    KNOWN GAP on one axis, opened by proj-kjei and left deliberately unfixed:
    the parser now refuses to open a bash fence NESTED inside an enclosing
    non-bash fence (CommonMark -- a fence cannot open inside an open fence), but
    a flat per-line count cannot see nesting and still counts the inner marker.
    Measured on this exact file while reviewing proj-kjei: as shipped, 24
    markers / 24 parsed, green; append one illustrative ````text block
    containing a ```bash example and it becomes 25 markers / 24 parsed, so this
    test goes RED blaming the parser for missing a fence it correctly declined.
    Latent -- zero nested fence openers exist across the repo's 58 tracked .md
    files. Not fixed here because the only fix is to make the count
    nesting-aware, i.e. give it a state machine, which is precisely the
    independence this test trades on. If someone ever adds such a block to
    land/SKILL.md, the answer is to re-derive the expected count some third way,
    NOT to relax the parser.

    proj-wroz widened the parser again -- one leading blockquote marker is
    stripped from every line -- so the count strips it too, through the SAME
    `_BLOCKQUOTE_MARKER` the parser normalizes with. Sharing the marker shape
    is not sharing the method: still a flat per-line count, no scan loop.
    Verified against a scratch copy with one illustrative `> ```bash` /
    `> echo hi` / `> ``` ` block appended: without the strip, one fewer
    marker than parsed blocks, and this test goes red blaming the parser for
    finding a fence the counter simply could not see under its blockquote;
    with it, the two agree. No blockquoted fence exists in land/SKILL.md
    today, so the strip is a measured no-op as shipped.
    """
    text = LAND_SKILL_TEXT
    opening_marker = re.compile(r"^(?:`{3,}|~{3,})\s*(?:bash|sh)$")
    expected_marker_count = sum(
        1
        for raw_line in text.splitlines()
        if opening_marker.match(_BLOCKQUOTE_MARKER.sub("", raw_line, count=1).strip())
    )
    # A sanity floor on the independent count itself -- if this ever drops to
    # 0 the file lost every bash fence, which is a different, louder bug this
    # test should not quietly go vacuous over.
    assert expected_marker_count > 0

    parsed_block_count = len(bash_fence_blocks(text))

    assert parsed_block_count == expected_marker_count, (
        f"parsed {parsed_block_count} ```bash/```sh fenced blocks but "
        f"{expected_marker_count} opening ```bash/```sh markers exist in the "
        "file -- EITHER the parser is missing some (e.g. an INDENTED fence a "
        "column-0-anchored scanner cannot see, proj-ovgs) OR this test's own "
        "independent counter has drifted from the parser's fence-shape/"
        "blockquote rules and is over- or under-counting -- check both "
        "before assuming the parser is at fault"
    )


def test_land_skill_never_reintroduces_an_inline_lock() -> None:
    """The exact regression this ticket fixed: a lock managed inline in a
    fenced block via `trap`/`kill -0`/`noclobber`, none of which can outlive
    the single Bash invocation that block runs in."""
    executed = LAND_SKILL_BASH
    assert "land-lock.sh acquire" in executed, (
        "the acquire call is not inside an executable ```bash fence -- "
        "_fenced_bash() or the skill's layout has drifted"
    )
    # Same `--release` exclusion as the pin above, for the same reason.
    assert re.search(r"land-heartbeat\.sh(?!\s*--release)", executed), (
        "the heartbeat call (Section 2a) is not inside an executable ```bash "
        "fence -- test_land_skill_heartbeats_the_lock_once_per_ticket_in_"
        "section_2a found it in the file's prose but not where it is actually "
        "EXECUTED (proj-m87j)"
    )

    offenders = [
        pattern for pattern in ("trap ", "kill -0", "noclobber") if pattern in executed
    ]
    assert not offenders, (
        f"land/SKILL.md EXECUTES inline lock machinery {offenders} -- a trap dies "
        "with its own fenced block and a recorded PID is always already dead by "
        "the next one; the lock must go through scripts/land-lock.sh (proj-aps3)"
    )


def test_every_own_token_readback_site_warns_when_empty() -> None:
    """proj-67nk: land/SKILL.md's own-token READ-BACK sites (every place that
    does `MY_TOKEN="$(cat "$(git rev-parse --path-format=absolute
    --git-common-dir)/land-lock-token" ...)"`)
    must each be followed by a loud, non-fatal stderr diagnostic when the read
    comes back empty, rather than silently proceeding blind. `land-lock.sh`
    treats an empty own-token argument EXACTLY as an absent one, so a
    missing/empty token file (a pass resumed mid-flight before Section 0 ever
    ran, or Section 2a/3/4 run by hand without Section 0) used to disable the
    ownership check with nothing in the log -- invisible to the call-site pins
    above, which are purely textual and prove only that `"$MY_TOKEN"` is
    spelled at each site, never that it is non-empty at run time. (Before
    proj-l7mj, the token additionally lived under `$STATE_DIR` and was
    reliably wiped by Section 1 on EVERY pass -- a stronger, now-fixed cause
    of the same empty read; see the mechanical execution test below.)

    Textual pin, same shape and same limit as the pins above it (see
    the module docstring, part 3): it proves the diagnostic is spelled at
    every call site in the SHIPPED file, not that it fires at run time. Both
    counts are taken over `_fenced_bash()` — the EXECUTED blocks only, same
    as those three pins — so prose that merely quotes either string can
    neither redden this test nor pad the warning count to cover a call site
    that genuinely lost its diagnostic.

    Section 0's own WRITE-then-bail-out release (`land-lock-blind-ok`) is
    deliberately excluded from both counts: it has no token to READ by
    construction, so it is not a read-back site at all and must not warn
    (this ticket's acceptance criteria name it as off limits)."""
    executed = LAND_SKILL_BASH

    token_reads = executed.count(
        'cat "$(git rev-parse --path-format=absolute --git-common-dir)/land-lock-token"'
    )
    assert token_reads == 2, (
        f"expected exactly 2 reads of $(git rev-parse --path-format=absolute"
        f" --git-common-dir)/land-lock-token"
        f" in land/SKILL.md -- Section 3's two merge script call sites, the only "
        f"remaining places that need $MY_TOKEN read back from disk to forward it "
        f"via --own-token to land-merge-batch.sh/land-replay.sh (every OTHER "
        f"former read-back site now runs scripts/land-heartbeat.sh, which "
        f"re-derives the token itself and no longer needs it threaded in), found "
        f"{token_reads} -- if a call site was genuinely added or removed, "
        "update this pin's count deliberately and check the new/removed site "
        "got (or lost) its own proj-67nk diagnostic too"
    )

    warning_sites = executed.count("DISABLED for this call ")
    assert warning_sites == token_reads, (
        f"found {token_reads} own-token read-back sites but only "
        f"{warning_sites} carry the 'no own-token available' "
        "warning -- every read-back site must warn when the token comes "
        "back empty, not just some of them"
    )


def test_land_merge_one_warns_on_an_empty_own_token_argument() -> None:
    """proj-67nk's second half: `land-merge-one.sh`'s own `$own_token`
    pass-through (fed by two of the five SKILL.md sites pinned above) must
    also carry its own diagnostic -- covering the case where the script is
    invoked directly, without going through a SKILL.md caller's warning at
    all. Textual pin against the shipped script, same reasoning as the
    SKILL.md pins above: proves the diagnostic is spelled at the call site,
    not that it fires at run time.

    The `>&2` redirect is pinned too, not incidentally: this script's STDOUT
    is the caller's `$CONFLICTS` channel (`CONFLICTS=$(land-merge-one.sh
    ...)` in SKILL.md Section 3), so a diagnostic that lost its redirect
    would be captured as conflict output and misread as a real conflict."""
    text = MERGE_ONE.read_text(encoding="utf-8")

    assert 'if [ -z "$own_token" ]; then\n  echo' in text, (
        "scripts/land-merge-one.sh does not warn when its own-token argument "
        "is empty -- the heartbeat call below then silently disables the "
        "ownership check with nothing in the log "
    )
    assert 'heartbeat" >&2' in text, (
        "scripts/land-merge-one.sh's empty-own-token warning no longer goes "
        "to stderr -- this script's stdout is the caller's $CONFLICTS "
        "channel, so a warning on stdout is read back as a merge conflict"
    )


# ---------------------------------------------------------------------------
# proj-l7mj: MECHANICAL regression, verified BY EXECUTION -- run Section 0
# then Section 1 as two separate Bash invocations (the governing rule's own
# model) against a real throwaway "main checkout", then read the token back
# exactly as Section 2a does. The three textual pins above (updated for the
# new path) are blind to this bug BY CONSTRUCTION: they prove a line is
# spelled "$(git rev-parse --path-format=absolute --git-common-dir)/
# land-lock-token" in the shipped file,
# never that the WIPE positioned between the write and every read-back site
# leaves that file intact at run time. Only running the real fences catches
# that -- which is exactly how this bug shipped past the three textual pins
# that already existed for the ownership check (proj-q9pm) at the time.
# ---------------------------------------------------------------------------


def _acquire_block() -> str:
    """Section 0's lock-acquire block -- locates it by content, not by
    heading, so a future reflow of the section doesn't silently repin the
    wrong fence. `only_block_with` asserts exactly one hit."""
    return only_block_with(
        LAND_SKILL_BLOCKS,
        "scripts/land-lock.sh acquire",
        "land-lock-token",
        what="Section 0's acquire block",
    )


def _pass_start_block() -> str:
    """Section 1's pass-start block -- the one that wipes $STATE_DIR and ends
    on `git reset --hard origin/main` (same locator shape as
    tests/test_land_conflicts_state.py's `_only_block_with`, kept independent
    rather than imported so this file's own execution-based pin does not
    depend on that module's helper existing or matching in shape)."""
    return only_block_with(
        LAND_SKILL_BLOCKS,
        "assert-main-checkout.sh",
        'rm -rf "$STATE_DIR"',
        "git reset --hard origin/main",
        what="Section 1's pass-start block",
    )


def _init_main_checkout_with_origin(tmp_path: Path) -> Path:
    """A throwaway "main checkout" -- a real, non-worktree repo (so
    `scripts/assert-main-checkout.sh` passes) with a `main` branch pushed to
    a real `origin` remote (so Section 1's `git fetch origin` /
    `git reset --hard origin/main` have something real to resolve), plus a
    `scripts/` symlink to this repo's real `scripts/` directory so the
    fences' own relative `scripts/land-lock.sh` / `scripts/assert-main-
    checkout.sh` calls resolve exactly as they do for a real /land pass
    (cwd-relative, no PATH lookup: a path containing a slash is never
    PATH-searched)."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))

    repo = _init_repo(tmp_path)  # same throwaway-repo shape, identity config included
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")
    (repo / "scripts").symlink_to(REPO_ROOT / "scripts")
    return repo


def _fake_bd_dir(tmp_path: Path) -> Path:
    """A PATH-prepended dir holding a fake `bd` that answers every subcommand
    (Section 1 only calls `bd dolt pull`) with success and nothing else --
    this test exercises the lock/token mechanism, not bd itself."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    fake_bd = bin_dir / "bd"
    fake_bd.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_bd.chmod(0o755)
    return bin_dir


def _run_block(block: str, repo: Path, bin_dir: Path) -> subprocess.CompletedProcess:
    """One fenced block as its own fresh `bash` subprocess, cwd'd at the
    throwaway main checkout -- mirrors an agent's one-Bash-tool-invocation-
    per-fence execution model (proj-sfnb), the same convention
    `tests/conftest.py::run_block` uses for other skills' fences. Not that
    helper itself: it cwd's at the REAL checkout root, which is exactly the
    directory this test must NOT touch (it would contend with this machine's
    own `.git/land.lock` and `.git/land-state/`)."""
    return subprocess.run(
        ["bash", "-c", block],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        env=fake_bin_env(bin_dir),
        check=False,
    )


def test_section_0_then_section_1_leaves_the_token_readable_by_section_2a(
    tmp_path: Path,
) -> None:
    """THE regression pin for proj-l7mj, verified by execution rather than by
    reading. Before the fix: Section 0 wrote the token to
    `$STATE_DIR/land-lock-token`, and Section 1's `rm -rf "$STATE_DIR"` -- run
    as a LATER, separate Bash invocation, exactly as a real /land pass does --
    deleted it before this test's read-back, reproducing the live 2026-08-06
    observation (`ls .git/land-state` -> no such directory) exactly. After the
    fix: the token lives beside `.git/land.lock`, which Section 1 never
    touches, so it survives.

    Reads the token back the same way Section 2a's own fence does: `cat
    "$(git rev-parse --path-format=absolute --git-common-dir)/land-lock-token"`,
    run as a THIRD, separate
    Bash invocation -- not a Python-side file read -- so this pin exercises
    the exact mechanism a real pass relies on, not merely the file's final
    state on disk."""
    repo = _init_main_checkout_with_origin(tmp_path)
    bin_dir = _fake_bd_dir(tmp_path)

    acquire_result = _run_block(_acquire_block(), repo, bin_dir)
    assert acquire_result.returncode == 0, (
        f"Section 0's acquire block failed: rc={acquire_result.returncode}, "
        f"stdout={acquire_result.stdout!r}, stderr={acquire_result.stderr!r}"
    )

    pass_start_result = _run_block(_pass_start_block(), repo, bin_dir)
    assert pass_start_result.returncode == 0, (
        f"Section 1's pass-start block failed: rc={pass_start_result.returncode}, "
        f"stdout={pass_start_result.stdout!r}, stderr={pass_start_result.stderr!r}"
    )

    readback = _run_block(
        'cat "$(git rev-parse --path-format=absolute --git-common-dir)/land-lock-token"',
        repo,
        bin_dir,
    )
    assert readback.returncode == 0 and readback.stdout.strip(), (
        "Section 2a's own token read-back came back empty after Section 0 "
        "then Section 1 ran -- the ownership check is silently disabled for "
        f"the whole pass (proj-l7mj). stdout={readback.stdout!r}, "
        f"stderr={readback.stderr!r}"
    )

    lock_record = (repo / ".git" / "land.lock").read_text(encoding="utf-8").split()
    assert len(lock_record) == 5, lock_record
    assert readback.stdout.strip() == lock_record[4], (
        "the token read back by Section 2a does not match field 5 (the owner "
        f"token) of the lock record this pass itself just wrote -- "
        f"read_back={readback.stdout.strip()!r}, lock_record={lock_record!r}"
    )
