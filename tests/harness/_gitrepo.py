"""Shared test helpers for driving real throwaway git repos (proj-863q).

`_git(repo, *args)` was originally defined byte-identically (modulo the
parameter name `repo` vs `cwd`) in six shell-script test modules, and
extracted here so a fix to its error reporting or timeout didn't have to
land in every copy at once -- the same failure class
`scripts/recycled-worktree-guard.sh`'s four-copy inline-bash duplication had
before it was extracted into one script (proj-ivth), and the same reason
`tests/_hookharness.py` exists one tier up, after three copies of ITS harness
started to drift (proj-zlg8).

That six is HISTORICAL -- the count at extraction, not today's. The current
roster is deliberately NOT enumerated here: the enumeration that used to sit
in this paragraph went stale silently (it still said six well after four more
modules had imported this), and a new importer is the success case this
extraction was for, not drift. Read the roster off the code instead --
`grep -rl '^from _gitrepo import' tests/` (proj-c835).

`_init_repo`/`_add_worktree` are deliberately NOT hoisted here. Their
differences across modules -- a real `origin` remote vs none, branching off
`origin/main` vs bare `main`, an optional `foreign_commit` kwarg, different
fixture file content and commit messages -- encode the different contracts of
the script each module drives; flattening them into one parametrized helper
would likely be a net loss (proj-863q). Each module keeps its own.

Some `_init_repo`/`_add_worktree` bodies are duplicated across modules. Which
modules share a body, how many do, and whether the duplication is
byte-identical or merely identical modulo docstrings, are deliberately NOT
enumerated here -- this paragraph's own hand-typed inventory has already gone
stale three times (6f6ba9c, proj-9owc, proj-c835), the same failure class the
roster paragraph above was rewritten to stop having. Measure it off the code
when it matters, and count the copies of ONE body, not the copies in total:
two differently-shaped `_init_repo`s duplicated twice each is not four copies
of anything, and hoisting them under one name would leave this directory with
differently-shaped `_init_repo`s sharing that name rather than one helper. A
body under the three-copy bar both precedents above fired at is left alone.
proj-ea5b put that question directly (is there an honest shared builder here,
or do these stay separate?) and re-measured every body in this directory
against this rule: same outcome, nothing hoisted. Do not re-litigate it
without a concrete shape; do re-measure, since proj-ea5b's own ticket text
carried an inventory that was already stale by the time it was built.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result


def _branch_from(repo: Path, base: str, name: str) -> None:
    _git(repo, "checkout", "-q", base)
    _git(repo, "checkout", "-q", "-b", name)


def _commit_file(repo: Path, path: str, content: str, message: str) -> None:
    """Write `path` (creating parent dirs) and commit it (proj-9egu).

    The parent `mkdir` is the superset of the three pre-hoist copies: it is a
    no-op for a repo-root path, and required for a nested one.
    """
    full = repo / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", message)
