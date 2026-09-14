"""Tests for scripts/release-bump.sh (proj-ns3r).

`/release` Section 2 (`.claude/skills/release/SKILL.md#2-derive-the-proposal`)
used to derive the conventional-commit SemVer bump with an inline shell
snippet -- ungated shell embedded directly in a SKILL.md, exactly the shape
of bug this repo has already shipped once before undetected
(`scripts/merge-precheck.sh`'s docstring, proj-mh9g).

THE BUG THIS EXTRACTION FIXES: the inline snippet read each commit's full
message via `git log RANGE --format='%B%x00'` + `while IFS= read -r -d ''
MSG`, then took `SUBJECT=$(printf '%s' "$MSG" | head -1)`. git inserts a
newline AFTER each record's `%B` expansion, BEFORE the `%x00` delimiter -- so
the NUL-delimited stream is actually "body1\n\x00body2\n\x00...", not
"body1\x00body2\x00...". Every record from the SECOND onward is therefore
captured WITH A LEADING NEWLINE, so `head -1` on it returns an EMPTY first
line, and the subject regexes never match anything but the newest commit in
the range. `test_two_feat_commits_neither_the_newest_yields_feat` below pins
the exact proj-905v repro shape (2 feat commits, neither the newest in the
range) and is the test the bug's fix must not regress: reverting
`scripts/release-bump.sh`'s subject-reading loop back to the old
`%B%x00` + `head -1` approach turns it red (verified by hand while writing
this suite -- BUMP comes back "none" instead of "feat").

All tests below run the ACTUAL `scripts/release-bump.sh` against real git
repositories built in `tmp_path` -- no fake git, no mocked subprocess. That is
what makes them sabotage-provable per the proj-verb bar (see
tests/test_merge_precheck.py for the same house style).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from _gitrepo import _git
from conftest import fake_bin_env

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "release-bump.sh"


def _init_repo(tmp_path: Path) -> Path:
    """A throwaway git repo with one commit on `main`, isolated user config."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("line1\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "chore: base")
    return repo


def _commit(repo: Path, message: str, filename: str | None = None) -> None:
    """One commit with the given (possibly multi-line) message. Each commit
    touches its own file so every commit is non-empty."""
    name = filename or f"f-{len(list(repo.glob('f-*.txt')))}.txt"
    (repo / name).write_text(f"content for {name}\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)


def _run(
    range_: str, repo: Path, bin_dir: Path | None = None
) -> subprocess.CompletedProcess:
    """Run the real script over `range_` in `repo`. `bin_dir`, when given, is
    PREPENDED to the real PATH so a shim it holds wins the PATH search over the
    genuine binary -- how the grep-fault tests below beat the real grep, while
    every other tool the script needs (git, mktemp) still resolves normally."""
    env = fake_bin_env(bin_dir) if bin_dir is not None else None
    return subprocess.run(
        ["bash", str(SCRIPT), range_],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_two_feat_commits_neither_the_newest_yields_feat(tmp_path: Path) -> None:
    """The exact proj-905v repro shape: v1.1.0..HEAD contains 2 feat(...)
    commits, neither of which is the newest commit in the range (a plain
    "chore:" commit is newest). The old inline snippet computed BUMP=none
    here because only the newest commit's subject survived the leading-
    newline bug intact."""
    repo = _init_repo(tmp_path)
    _git(repo, "tag", "v1.1.0")
    _commit(repo, "feat(retrieval): add reranker")
    _commit(repo, "feat(cli): add --json flag")
    _commit(repo, "chore: tidy up")  # newest -- carries no recognized prefix

    result = _run("v1.1.0..HEAD", repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "feat\n"
    assert result.stderr == ""


def test_single_fix_commit_yields_fix(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "tag", "v0.3.1")
    _commit(repo, "fix(auth): correct token refresh")

    result = _run("v0.3.1..HEAD", repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "fix\n"


def test_no_recognized_prefix_yields_none(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "tag", "v0.3.1")
    _commit(repo, "tidy up formatting")
    _commit(repo, "another unrelated commit")

    result = _run("v0.3.1..HEAD", repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "none\n"


def test_bang_subject_yields_breaking_even_when_not_newest(tmp_path: Path) -> None:
    """A `!:` breaking-change subject must win regardless of position in the
    range -- the same leading-newline bug that broke feat/fix detection would
    also have broken this."""
    repo = _init_repo(tmp_path)
    _git(repo, "tag", "v0.3.1")
    _commit(repo, "feat(api)!: drop legacy endpoint")
    _commit(repo, "fix(cli): trivial typo")  # newest

    result = _run("v0.3.1..HEAD", repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "breaking\n"


def test_breaking_change_in_body_of_non_newest_commit_is_detected(
    tmp_path: Path,
) -> None:
    """BREAKING CHANGE: in a commit BODY (not just the subject) must be
    detected even when that commit is not the newest in the range -- this is
    the acceptance criterion 'BREAKING-CHANGE-in-body detection still works
    under the chosen fix'."""
    repo = _init_repo(tmp_path)
    _git(repo, "tag", "v0.3.1")
    _commit(
        repo,
        "feat(storage): change chunk format\n\nBREAKING CHANGE: old chunks "
        "must be re-embedded.",
    )
    _commit(repo, "fix(cli): trivial typo")  # newest, no breaking marker

    result = _run("v0.3.1..HEAD", repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "breaking\n"


def test_breaking_change_in_body_is_detected_in_a_large_range(tmp_path: Path) -> None:
    """Regression test for a SIGPIPE/pipefail FALSE NEGATIVE found in review.

    The first cut of the script ran the body scan as a pipeline::

        if git log "$RANGE" --format='%B' | grep -qE 'BREAKING[ -]CHANGE:'

    under `set -o pipefail`. `grep -q` exits the instant it matches, closing
    the pipe; `git log` is then killed by SIGPIPE (141); pipefail promotes
    that 141 to the status of the whole pipeline, so the `if` evaluated FALSE
    *because* the marker was found. It only reproduces when the marker is
    early in the stream AND enough output follows to exceed a pipe buffer --
    i.e. a MARKER IN A RECENT COMMIT of a real-sized release range. harness's own
    `v1.1.0..HEAD` is ~75KB of `%B`, well past that line.

    Why `test_breaking_change_in_body_of_non_newest_commit_is_detected` above
    did not catch it: it puts the marker in the OLDER commit of a two-commit
    range, which is safe for two independent reasons at once (the whole stream
    is read before grep can match, and it is a few hundred bytes anyway). It
    asserts the acceptance criterion without being able to fail it. This test
    inverts both conditions.

    The stream size is chosen deliberately, not arbitrarily. Reproducing the
    old failure is a RACE -- `git log` has to still be writing when `grep -q`
    exits -- so the margin has to clear grep's read buffer AND the pipe buffer
    together (~96KB), not just the 64KB pipe buffer. Measured over 20 trials
    per shape against the pre-fix pipeline: ~100KB reproduced 19/20 (i.e. it
    would have let a regression through 1 run in 20), while the ~160KB shape
    below reproduced 20/20. Two 80KB filler commits also cost fewer
    subprocesses than the five 20KB ones first tried here. Keep it at or above
    this size; shrinking it silently weakens the test rather than failing it.
    (80KB each, not one 160KB commit: Linux caps a single argv entry at 128KB,
    so a larger `-m` message fails to commit at all.)
    """
    repo = _init_repo(tmp_path)
    _git(repo, "tag", "v0.3.1")
    filler = "x" * 80_000
    for i in range(2):
        _commit(repo, f"chore: filler {i}\n\n{filler}")
    _commit(  # NEWEST -> emitted FIRST by `git log`, so grep matches immediately
        repo,
        "feat(storage): change chunk format\n\n"
        "BREAKING CHANGE: old chunks must be re-embedded.",
    )

    # Guard the test's own premise: if the stream ever stops being comfortably
    # larger than grep's read buffer plus the pipe buffer, this test would
    # silently stop exercising the bug it exists for.
    stream = _git(repo, "log", "v0.3.1..HEAD", "--format=%B").stdout
    assert len(stream) > 128 * 1024, f"premise broken: stream is only {len(stream)}B"

    result = _run("v0.3.1..HEAD", repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "breaking\n"


def test_scopeless_feat_prefix_yields_feat(tmp_path: Path) -> None:
    """Conventional-commit scopes are OPTIONAL. Every other prefix test in
    this file happens to use one, so a regex that *required* a scope would
    pass the whole suite -- these three pin the bare form."""
    repo = _init_repo(tmp_path)
    _git(repo, "tag", "v0.3.1")
    _commit(repo, "feat: add a thing")

    result = _run("v0.3.1..HEAD", repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "feat\n"


def test_scopeless_fix_prefix_yields_fix(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "tag", "v0.3.1")
    _commit(repo, "fix: correct a thing")

    result = _run("v0.3.1..HEAD", repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "fix\n"


def test_scopeless_bang_prefix_yields_breaking(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "tag", "v0.3.1")
    _commit(repo, "feat!: drop a thing")

    result = _run("v0.3.1..HEAD", repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "breaking\n"


def test_prefix_must_be_at_start_of_subject(tmp_path: Path) -> None:
    """The prefix regexes are anchored -- a subject that merely mentions
    "feat:" or "fix:" mid-line is not a conventional commit. Pins the `^`
    anchor in release-bump.sh's `grep -qE` matching (subjects fed in via a
    here-string, not a pipeline)."""
    repo = _init_repo(tmp_path)
    _git(repo, "tag", "v0.3.1")
    _commit(repo, "chore: revert the feat: add reranker commit")
    _commit(repo, "docs: explain when to use fix: vs feat:")

    result = _run("v0.3.1..HEAD", repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "none\n"


def test_precedence_breaking_over_feat_over_fix(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "tag", "v0.3.1")
    _commit(repo, "fix(a): minor fix")
    _commit(repo, "feat(b): a feature")
    _commit(repo, "feat(c)!: a breaking feature")

    result = _run("v0.3.1..HEAD", repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "breaking\n"


def test_precedence_feat_over_fix(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "tag", "v0.3.1")
    _commit(repo, "fix(a): minor fix")
    _commit(repo, "feat(b): a feature")

    result = _run("v0.3.1..HEAD", repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "feat\n"


def test_unresolvable_range_is_a_machine_fault_exit_2(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    result = _run("totally-bogus-tag-xyz..HEAD", repo)

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert "GATE COULD NOT RUN" in result.stderr


def test_usage_without_args_is_exit_2() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "usage" in result.stderr
    assert result.stdout == ""


def test_usage_with_two_args_is_also_exit_2() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "a..b", "extra"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "usage" in result.stderr


# ---------------------------------------------------------------------------
# proj-umtc: the four grep call sites that consume read_log's output. Why the
# partition is drawn at exit 1 is argued at the greps in
# scripts/release-bump.sh -- not repeated here. The shared `rc=$?`/`-ne 1`
# body those sites now call is gate-lib.sh's escalate_unless_content()
# (proj-1mea); the tests below still drive the REAL script, so they cover the
# partition end-to-end at each site regardless of where that body lives.
#
# Each fake grep below delegates to the REAL grep for every call except the
# one call site under test (matched on grep's own PATTERN argument, `$2` --
# release-bump.sh always invokes `grep -qE "$PAT_..." <<<"$STREAM"`), so a
# test targeting one site cannot be masked by -- or accidentally pass because
# of -- a different site also being broken.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    """An empty directory to hold PATH shims, passed to ``_run(bin_dir=...)``."""
    d = tmp_path / "fakebin"
    d.mkdir()
    return d


def _add_faulty_grep(bin_dir: Path, pattern_marker: str, exit_code: int) -> None:
    """A ``grep`` shim that behaves exactly like the real grep EXCEPT when its
    own pattern argument (``$2``) contains ``pattern_marker``, in which case it
    exits ``exit_code`` instead of actually matching. ``exit_code`` must never
    be 1 -- that is grep's own genuine "no match" content answer, not a fault."""
    assert exit_code != 1, "exit 1 is grep's own content answer, not a fault"
    real_grep = shutil.which("grep")
    assert real_grep, "grep not found -- cannot build a hermetic PATH"
    shim = bin_dir / "grep"
    shim.write_text(
        "#!/bin/bash\n"
        'case "$2" in\n'
        f"  *'{pattern_marker}'*) exit {exit_code} ;;\n"
        "esac\n"
        f'exec "{real_grep}" "$@"\n'
    )
    shim.chmod(0o755)


def _assert_gate_could_not_run(
    result: subprocess.CompletedProcess, *, says: str, exit_code: int
) -> None:
    """The whole contract of a gate-could-not-run exit from this script, pinned
    in ONE place so tightening it later is one edit rather than one per site:
    exit 2 (never 0, which would carry a bump verdict a caller would act on),
    nothing on stdout, the banner, the scan that failed, and grep's OWN
    faulting status echoed back -- which is what proves `rc=$?` captured grep's
    status rather than something else's."""
    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert "GATE COULD NOT RUN" in result.stderr
    assert says in result.stderr
    assert f"exit {exit_code}" in result.stderr


@pytest.mark.parametrize(
    ("subject", "pattern_marker", "exit_code", "says"),
    [
        # Site 1, the BREAKING-CHANGE body scan. Nothing runs before it, so any
        # subject reaches it; a fault here must not read as "no marker in this
        # range" and fall through to the subject checks.
        ("feat: add a thing", "BREAKING", 2, "commit bodies"),
        # Site 2, the `!:` subject scan. No BREAKING-CHANGE body marker, so
        # site 1 genuinely finds nothing and this is the first place a fault
        # can surface.
        ("fix: correct a thing", "!:", 5, "breaking-change marker"),
        # Site 3, the feat subject scan. Neither a body marker nor a `!:`
        # subject, so sites 1 and 2 genuinely find nothing.
        ("fix: correct a thing", "feat", 3, "feat prefix"),
        # Site 4, the fix subject scan (last). The commit carries none of the
        # first three markers, so this is the first place a fault can surface.
        ("chore: tidy up", "fix", 4, "fix prefix"),
    ],
)
def test_grep_fault_at_each_site_is_gate_could_not_run(
    tmp_path: Path,
    fake_bin: Path,
    subject: str,
    pattern_marker: str,
    exit_code: int,
    says: str,
) -> None:
    """A grep faulting with a non-1 exit at ANY of the four sites must escalate
    to exit 2, not be read as "no match". Each case picks a commit subject that
    leaves the earlier sites genuinely matchless, so the site named in the
    parameter really is the one that faulted -- and a distinct exit code per
    case, echoed back in the message, pins that grep's OWN status was captured.
    """
    repo = _init_repo(tmp_path)
    _git(repo, "tag", "v0.3.1")
    _commit(repo, subject)
    _add_faulty_grep(fake_bin, pattern_marker, exit_code)

    result = _run("v0.3.1..HEAD", repo, bin_dir=fake_bin)

    _assert_gate_could_not_run(result, says=says, exit_code=exit_code)


def test_fault_at_breaking_subject_does_not_fall_through_to_feat(
    tmp_path: Path, fake_bin: Path
) -> None:
    """The precedence ladder's fall-through, closed. Kept separate from the
    parametrized cases above because it asserts something they cannot: the
    range holds a feat commit, so the NEXT, lower-precedence check would have
    matched had the fault been allowed to reach it. That is what distinguishes
    "escalated at the correct rung" from a blanket-broken grep failing every
    site identically -- unpartitioned, this repo reports a confident `feat`."""
    repo = _init_repo(tmp_path)
    _git(repo, "tag", "v0.3.1")
    _commit(repo, "feat: add a thing")  # would match feat's own check
    _add_faulty_grep(fake_bin, "!:", 2)

    result = _run("v0.3.1..HEAD", repo, bin_dir=fake_bin)

    _assert_gate_could_not_run(result, says="breaking-change marker", exit_code=2)
    assert "feat prefix" not in result.stderr  # never reached the next arm
