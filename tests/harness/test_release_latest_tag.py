"""Tests for scripts/release-latest-tag.sh (proj-b2bf).

`/release` Section 1 (`.claude/skills/release/SKILL.md#1-find-the-latest-tag`)
and `scripts/release.sh`'s own tag-monotonicity gate used to each carry their
own hand-typed copy of a tag-selection loop plus a `version_gt()` SemVer
comparator -- untested inline shell, duplicated in two places and free to
drift apart (the skill could propose a bump against one baseline tag while
`release.sh` cut against a different one). Same "ungated inline shell in a
SKILL.md rots silently" lesson already paid for once in this repo via
`scripts/release-bump.sh` (proj-ns3r) and `scripts/merge-precheck.sh`
(proj-mh9g).

THE BUG THIS EXTRACTION FIXES (beyond just de-duplicating): both inline
copies picked tag candidates via the case pattern
`[0-9]*.[0-9]*.[0-9]*) ;; *) continue ;;`, a *glob*, not a regex -- in a case
pattern, a bare `*` matches ANY trailing text, so that pattern actually
accepted `v1.2.3-rc1`, `v1.2.3.4`, and `v1.2.3beta` as if they were ordinary
`vX.Y.Z` releases (verified by hand: all three match the old glob).
`test_prerelease_suffix_tag_is_ignored`,
`test_four_component_tag_is_ignored`, and
`test_non_numeric_suffix_tag_is_ignored` below pin the fix: the new script
requires exactly three all-numeric, dot-separated components via an anchored
regex.

All tests below run the ACTUAL `scripts/release-latest-tag.sh` against real
git repositories built in `tmp_path` -- no fake git, no mocked subprocess.
Same house style as `tests/test_release_bump.py` / `tests/test_merge_precheck.py`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from _gitrepo import _git

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "release-latest-tag.sh"


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


def _tag(repo: Path, name: str) -> None:
    """A lightweight tag on the current HEAD. Each call adds a distinct
    trivial commit first so tags can be given out of creation-order without
    colliding on an identical tree/commit."""
    fname = f"tagfile-{len(list(repo.glob('tagfile-*.txt')))}.txt"
    (repo / fname).write_text(f"content for {name}\n")
    _git(repo, "add", fname)
    _git(repo, "commit", "-q", "-m", f"chore: commit for {name}")
    _git(repo, "tag", name)


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


# --- Mode 1: print the latest tag ------------------------------------------


def test_no_tags_yields_empty_stdout_exit_0(tmp_path: Path) -> None:
    """First release: no vX.Y.Z tag exists yet. Empty output is the correct,
    legitimate answer -- not a fault."""
    repo = _init_repo(tmp_path)

    result = _run(repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def test_semver_greatest_differs_from_most_recently_created(tmp_path: Path) -> None:
    """A tag can be created out of order (e.g. a backport release cut after a
    later one already exists). Selection must be by SemVer value, not by tag
    creation order / `git tag -l`'s default listing order."""
    repo = _init_repo(tmp_path)
    _tag(repo, "v2.0.0")
    _tag(repo, "v1.5.0")  # created AFTER v2.0.0, but SemVer-lower

    result = _run(repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "v2.0.0\n"


def test_pre_1_0_versions(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _tag(repo, "v0.1.0")
    _tag(repo, "v0.3.1")
    _tag(repo, "v0.2.0")

    result = _run(repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "v0.3.1\n"


def test_multi_digit_components_compare_numerically(tmp_path: Path) -> None:
    """v0.10.0 > v0.9.0 -- a NAIVE STRING comparison ("0.10.0" < "0.9.0"
    lexicographically) would get this backwards."""
    repo = _init_repo(tmp_path)
    _tag(repo, "v0.9.0")
    _tag(repo, "v0.10.0")

    result = _run(repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "v0.10.0\n"


def test_prerelease_suffix_tag_is_ignored(tmp_path: Path) -> None:
    """v1.2.3-rc1 matched the OLD glob (`[0-9]*.[0-9]*.[0-9]*`, where a bare
    `*` swallows any trailing text) but must not be treated as an ordinary
    release tag."""
    repo = _init_repo(tmp_path)
    _tag(repo, "v1.0.0")
    _tag(repo, "v1.2.3-rc1")

    result = _run(repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "v1.0.0\n"


def test_four_component_tag_is_ignored(tmp_path: Path) -> None:
    """v1.2.3.4 also matched the old glob."""
    repo = _init_repo(tmp_path)
    _tag(repo, "v1.0.0")
    _tag(repo, "v1.2.3.4")

    result = _run(repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "v1.0.0\n"


def test_non_numeric_suffix_tag_is_ignored(tmp_path: Path) -> None:
    """v1.2.3beta also matched the old glob."""
    repo = _init_repo(tmp_path)
    _tag(repo, "v1.0.0")
    _tag(repo, "v1.2.3beta")

    result = _run(repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "v1.0.0\n"


def test_non_v_prefixed_tag_is_ignored(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _tag(repo, "v1.0.0")
    _tag(repo, "release-2.0.0")

    result = _run(repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "v1.0.0\n"


# --- Mode 2: --gt VERSION ----------------------------------------------------


def test_gt_true_when_candidate_exceeds_latest(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _tag(repo, "v1.2.0")

    result = _run(repo, "--gt", "1.3.0")

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_gt_false_when_candidate_does_not_exceed_latest(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _tag(repo, "v1.2.0")

    result = _run(repo, "--gt", "1.2.0")

    assert result.returncode == 1, result.stderr


def test_gt_false_when_candidate_is_lower(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _tag(repo, "v1.2.0")

    result = _run(repo, "--gt", "1.1.0")

    assert result.returncode == 1, result.stderr


def test_gt_true_when_no_tag_exists_at_all(tmp_path: Path) -> None:
    """First release: anything exceeds "nothing"."""
    repo = _init_repo(tmp_path)

    result = _run(repo, "--gt", "0.1.0")

    assert result.returncode == 0, result.stderr


def test_gt_multi_digit_components(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _tag(repo, "v0.9.0")

    result = _run(repo, "--gt", "0.10.0")

    assert result.returncode == 0, result.stderr


# --- Usage / machine-fault errors --------------------------------------------


def test_git_failure_is_a_machine_fault_exit_2(tmp_path: Path) -> None:
    """`git tag -l` failing is a statement about the MACHINE, never about
    which tag is latest -- so it must exit 2, not report "no tag" (which the
    callers read as "first release", waving through any version at all).

    Mirrors `test_release_bump.py`'s own machine-fault coverage, and is the
    only test that exercises the stderr-capture branch (the mktemp/errfile
    machinery that forwards git's own message verbatim)."""
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()

    result = _run(not_a_repo)

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert "GATE COULD NOT RUN" in result.stderr
    # The errfile branch actually forwarded git's own diagnostic.
    assert "git's own error output:" in result.stderr


def test_gt_git_failure_is_also_exit_2(tmp_path: Path) -> None:
    """Same fault, --gt mode: must NOT be mistaken for "no tag exists, so
    anything exceeds it" (exit 0), which would disable the gate entirely."""
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()

    result = _run(not_a_repo, "--gt", "9.9.9")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "GATE COULD NOT RUN" in result.stderr


def test_malformed_gt_candidate_is_exit_2(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    result = _run(repo, "--gt", "not-a-version")

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert "GATE COULD NOT RUN" in result.stderr


def test_gt_with_no_version_arg_is_exit_2(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    result = _run(repo, "--gt")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "usage" in result.stderr


def test_unrecognized_flag_is_exit_2(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    result = _run(repo, "--bogus")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "usage" in result.stderr


def test_extra_positional_arg_is_exit_2(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    result = _run(repo, "1.2.3")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "usage" in result.stderr
