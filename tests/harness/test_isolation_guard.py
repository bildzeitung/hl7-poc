"""Tests for scripts/isolation-guard.sh (proj-ska2 / proj-jk44).

A `code-reviewer` dispatch was observed with its cwd pinned to the MAIN
CHECKOUT at the repo root, checked out on `main` -- `isolation: "worktree"`
never took at all. This is a DIFFERENT failure from
scripts/recycled-worktree-guard.sh's proj-nt98 (a *recycled* worktree still
on a previous ticket's branch -- a worktree, just the wrong one): here there
was no worktree whatsoever. Both documented `EnterWorktree` self-rescue
routes were refused by the harness, and nothing MECHANICAL then stopped the
dispatched agent from running Edit/Write/`nox -t fix` directly against the
main checkout on main -- only an English "if my cwd is main, STOP"
instruction held, and that same incident's agent went on to invent an
unsanctioned `git worktree add` + `git -C` workaround instead of actually
stopping.

This script closes the gap: a single, shellcheck'd, unit-tested precondition
-- "do I have an isolated worktree AT ALL" -- run as the first executable
action of the cycle, before anything else. Unlike recycled-worktree-guard.sh,
it never repairs anything on failure (there is no safe way to fabricate an
isolated worktree from a non-isolated context); the only sanctioned response
is a hard stop, which is exactly what exit 1 signals here.

All tests run the ACTUAL `scripts/isolation-guard.sh` against real git
repositories (with real `git worktree add` checkouts) built in `tmp_path` --
no fake git, no mocked subprocess -- sabotage-provable per the proj-verb bar:
reverting the script's `case` guard directly would turn the corresponding
test here red.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _gitrepo import _git
from conftest import AGENTS_DIR

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "isolation-guard.sh"


def _init_repo(tmp_path: Path) -> Path:
    """A throwaway repo with one commit on `main`, isolated user config."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _add_worktree(repo: Path, rel_path: str, branch: str) -> Path:
    wt = repo / rel_path
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", str(wt), "-b", branch, "main")
    return wt


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_wrong_argument_count_exits_2(tmp_path: Path) -> None:
    """Any argument at all is a usage error (exit 2) -- this is a pure,
    unparametrized precondition, distinct from a worktree problem (0/1)."""
    repo = _init_repo(tmp_path)
    result = _run(repo, "unexpected-arg")
    assert result.returncode == 2, result.stdout + result.stderr


def test_not_inside_any_repository_is_exit_2_not_a_raw_git_128(
    tmp_path: Path,
) -> None:
    """cwd outside any git repository at all -- `git rev-parse --show-toplevel`
    fails, and the script must convert that into its own documented exit 2
    with a diagnostic, NOT let `set -e` propagate git's raw 128 (proj-t6ni).

    Why 128 is unacceptable is the family contract's business, not this
    module's: docs/agents-workflow.md, "Precondition guards (the 0/1/2
    family)". Mirrors
    tests/test_assert_main_checkout.py::test_not_inside_any_repository_is_exit_2_not_a_raw_git_128,
    the sibling this behavior was backported from."""
    outside = tmp_path / "not-a-repo"
    outside.mkdir()

    result = _run(outside)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "MACHINE FAULT" in result.stderr
    assert "MACHINE FAULT" in result.stderr


def test_cwd_under_claude_worktrees_passes(tmp_path: Path) -> None:
    """The genuinely-isolated case: cwd is a worktree under
    `.claude/worktrees/` -- exit 0, nothing printed to stderr."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, ".claude/worktrees/agent-abc123", "worktree-agent-abc123")

    result = _run(wt)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""


def test_main_checkout_at_repo_root_is_refused(tmp_path: Path) -> None:
    """The EXACT proj-ska2 production repro: cwd is the main checkout itself
    (not any worktree at all), sitting on main. Must refuse (exit 1) and
    name proj-ska2 plus the hard-stop instruction in its diagnostic."""
    repo = _init_repo(tmp_path)

    result = _run(repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "NOT DISPATCHED INTO AN ISOLATED WORKTREE" in result.stderr
    assert "NOT DISPATCHED INTO AN ISOLATED WORKTREE" in result.stderr
    assert "STOP AND REPORT" in result.stderr
    # The whole point: the diagnostic must foreclose self-rescue, not just
    # describe the problem -- a caller reading only this message must not be
    # able to talk itself into EnterWorktree or `git worktree add` next.
    assert "EnterWorktree" in result.stderr
    assert "git worktree add" in result.stderr


def test_a_worktree_outside_claude_worktrees_is_also_refused(tmp_path: Path) -> None:
    """A real `git worktree add` checkout, just not under `.claude/worktrees/`,
    is still not an isolated launch worktree -- exit 1, same as the bare
    main-checkout case. `.claude/worktrees/` is the only path the harness's
    own dispatch is documented to use."""
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, "not-a-launch-worktree", "worktree-agent-elsewhere")

    result = _run(wt)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "NOT DISPATCHED INTO AN ISOLATED WORKTREE" in result.stderr


@pytest.mark.parametrize(
    "near_miss_dir",
    [".claude/worktrees-stale/agent-abc123", "x.claude/worktrees/agent-abc123"],
    ids=["trailing-anchor", "leading-anchor"],
)
def test_a_near_miss_directory_name_is_refused(
    tmp_path: Path, near_miss_dir: str
) -> None:
    """The `case` glob must match the literal path SEGMENT `.claude/worktrees/`
    -- BOTH its `/` anchors, not merely the substring between them. One
    parameter per anchor, because each is the sole catcher of its own mutation:

    - `.claude/worktrees-stale/...` pins the TRAILING `/` (the segment is
      `worktrees-stale`, not `worktrees`). Relaxing the glob to
      `*/.claude/worktrees*` leaves every other test in this module green.
    - `x.claude/worktrees/...` pins the LEADING `/` (the segment is `x.claude`,
      not `.claude`). Deleting the leading `*/` outright is caught by the other
      tests here, but merely WEAKENING it to `*` -- `*.claude/worktrees/*` --
      left the entire module green until this parameter was added (proj-v12j).

    Sabotage-verified. The twin pin, against the byte-identical glob in
    scripts/recycled-worktree-guard.sh, lives in
    tests/test_recycled_worktree_guard.py.
    """
    repo = _init_repo(tmp_path)
    wt = _add_worktree(repo, near_miss_dir, "worktree-agent-stale")

    result = _run(wt)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "NOT DISPATCHED INTO AN ISOLATED WORKTREE" in result.stderr


def test_every_agent_definition_invokes_the_guard() -> None:
    """The guard only protects a dispatch that actually calls it, and every
    call site is prose in a `.claude/agents/*.md` file -- so a new agent
    definition would ship unguarded with nothing going red.

    This pins the call sites themselves against the SHIPPED files (the
    `tests/_hookharness.py` precedent: assert what is committed, never a
    reimplementation). If a future agent definition is genuinely exempt,
    this test is the place to record why.
    """
    agent_defs = sorted(AGENTS_DIR.glob("*.md"))
    assert agent_defs, "no .claude/agents/*.md found -- has the layout moved?"

    missing = [
        path.name
        for path in agent_defs
        if "scripts/isolation-guard.sh" not in path.read_text(encoding="utf-8")
    ]
    assert not missing, (
        f"agent definition(s) {missing} never invoke scripts/isolation-guard.sh -- "
        "a dispatch of theirs that loses its isolation worktree would run unguarded "
        "against the main checkout on main (proj-ska2)"
    )


def _frontmatter(path: Path) -> str:
    """The YAML frontmatter block of a `.claude/agents/*.md` file.

    Deliberately NOT a whole-file substring search, and `land-review.md` is the
    file that proves why: its PROSE says "This frontmatter now carries
    `isolation: worktree`", an exact unquoted match. A naive whole-file `in`
    check therefore stays GREEN for `land-review` with the frontmatter key
    deleted -- and `land-review` is the one role where that key is the SOLE
    enforcement point (proj-p2vi dropped its call-site option), i.e. precisely
    the file a whole-file check would fail to protect. Verified by sabotage.

    (`coding.md`/`code-reviewer.md` happen to use the quoted `isolation:
    "worktree"` form in prose, so a naive check would catch those two -- which
    is exactly the kind of accident that makes a substring check look adequate
    until the one case that matters slips through it.)
    """
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name} does not open with a --- fence"
    end = text.index("\n---\n", 3)
    return text[4:end]


def test_every_agent_definition_pins_isolation_in_frontmatter() -> None:
    """The sibling of the guard-call-site check above, on the other axis.

    `isolation: worktree` in the agent definition makes isolation a property
    of the ROLE, so a dispatch cannot lose it by forgetting a call-site option
    (proj-kt6g); for `land-review` the frontmatter is the SOLE enforcement
    point (proj-p2vi dropped `land/SKILL.md`'s call-site option), and proj-ojsr
    extended the key to `coding` and `code-reviewer` after proj-ska2's 6-of-6
    no-worktree fan-out. Nothing went red if an edit silently dropped that line
    -- exactly the gap the guard-call-site test above exists to close, on the
    key rather than the call.

    If a future agent definition is genuinely exempt (a read-only agent that
    must NOT get its own worktree, say), this test is the place to record why.
    """
    agent_defs = sorted(AGENTS_DIR.glob("*.md"))
    assert agent_defs, "no .claude/agents/*.md found -- has the layout moved?"

    missing = [
        path.name
        for path in agent_defs
        if "isolation: worktree"
        not in [line.strip() for line in _frontmatter(path).splitlines()]
    ]
    assert not missing, (
        f"agent definition(s) {missing} carry no `isolation: worktree` frontmatter "
        "key -- isolation would then rest entirely on every call site remembering "
        "to pass it (proj-kt6g, proj-ojsr)"
    )


def test_refusal_never_mutates_anything(tmp_path: Path) -> None:
    """Unlike recycled-worktree-guard.sh, this script never repairs on
    failure -- confirm HEAD, branches, and the working tree are untouched
    either way."""
    repo = _init_repo(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "untracked.txt").write_text("must survive\n")

    result = _run(repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
    assert (repo / "untracked.txt").exists()
    branches = _git(repo, "branch", "--list", "rescue/*").stdout
    assert branches.strip() == ""
