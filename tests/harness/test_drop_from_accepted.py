"""scripts/drop-from-accepted.sh -- Section 3a's "a base takes its dependents with it".

This was a shell recipe inside a COMMENT in land/SKILL.md. Its failure mode is the
one the whole stacked-branch apparatus exists to prevent: a dependent merges while
its base has left the merge set, putting the base's just-rejected content onto the
default branch under the dependent's ticket name.

The cases that matter are the reduction actually reaching the FILE (the replay loop
re-reads it), the transitive drop, and the refusals -- a missing accepted file or a
missing graph must be machine faults, never a silent "nothing to drop".
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from conftest import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "drop-from-accepted.sh"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


def _accepted(tmp_path: Path, *ids: str) -> Path:
    p = tmp_path / "accepted"
    p.write_text("".join(f"{i}\n" for i in ids))
    return p


def _graph(tmp_path: Path, *edges: tuple[str, str, str]) -> Path:
    p = tmp_path / "graph"
    p.write_text("".join(f"EDGE\t{d}\t{b}\t{k}\n" for d, b, k in edges))
    return p


def test_drops_the_named_id_and_rewrites_the_file(tmp_path: Path) -> None:
    acc = _accepted(tmp_path, "a", "b", "c")
    r = _run("b", "--accepted", str(acc))
    assert r.returncode == 0, r.stderr
    assert r.stdout == "DROPPED\tb\n"
    assert acc.read_text() == "a\nc\n", (
        "the reduction must reach the FILE, not just stdout"
    )


def test_drops_direct_dependents_with_the_base(tmp_path: Path) -> None:
    acc = _accepted(tmp_path, "base", "dep", "other")
    g = _graph(tmp_path, ("dep", "base", "direct"))
    r = _run("base", "--accepted", str(acc), "--graph", str(g))
    assert r.returncode == 0, r.stderr
    assert acc.read_text() == "other\n"
    assert "DROPPED\tbase" in r.stdout and "HELD\tdep" in r.stdout


def test_drops_transitive_dependents_too(tmp_path: Path) -> None:
    """a <- b <- c. Dropping a must take BOTH: c inherits a's content just as
    much as b does. stacked-graph.sh emits the closure, so the transitive edge
    is present and no closure walk is hand-rolled here."""
    acc = _accepted(tmp_path, "a", "b", "c", "unrelated")
    g = _graph(
        tmp_path, ("b", "a", "direct"), ("c", "b", "direct"), ("c", "a", "transitive")
    )
    r = _run("a", "--accepted", str(acc), "--graph", str(g))
    assert r.returncode == 0, r.stderr
    assert acc.read_text() == "unrelated\n"
    assert sorted(l.split("\t")[1] for l in r.stdout.splitlines()) == ["a", "b", "c"]


def test_a_dependent_of_something_else_is_not_dropped(tmp_path: Path) -> None:
    acc = _accepted(tmp_path, "x", "y", "ydep")
    g = _graph(tmp_path, ("ydep", "y", "direct"))
    r = _run("x", "--accepted", str(acc), "--graph", str(g))
    assert r.returncode == 0, r.stderr
    assert acc.read_text() == "y\nydep\n"


def test_dropping_the_last_entry_leaves_an_empty_file_not_an_error(
    tmp_path: Path,
) -> None:
    """An all-kicked-back pass is legitimate. grep exits 1 when it filters out the
    last line, and aborting there would break the reduction exactly when it matters."""
    acc = _accepted(tmp_path, "only")
    r = _run("only", "--accepted", str(acc))
    assert r.returncode == 0, r.stderr
    assert acc.read_text() == ""


def test_dropping_an_absent_id_is_idempotent(tmp_path: Path) -> None:
    acc = _accepted(tmp_path, "a", "b")
    r = _run("zzz", "--accepted", str(acc))
    assert r.returncode == 0, r.stderr
    assert acc.read_text() == "a\nb\n"


def test_substring_ids_are_not_collateral(tmp_path: Path) -> None:
    """`grep -vxF` -- fixed string, whole line. A prefix match would silently
    drop an unrelated branch from the merge set."""
    acc = _accepted(tmp_path, "t1", "t10", "t1-extra")
    r = _run("t1", "--accepted", str(acc))
    assert r.returncode == 0, r.stderr
    assert acc.read_text() == "t10\nt1-extra\n"


def test_missing_accepted_file_is_a_machine_fault(tmp_path: Path) -> None:
    r = _run("a", "--accepted", str(tmp_path / "nope"))
    assert r.returncode == 2
    assert "does not exist" in r.stderr
    assert r.stdout == ""


def test_missing_graph_file_is_a_machine_fault_not_a_skipped_drop(
    tmp_path: Path,
) -> None:
    """Reading a missing graph as 'no dependents' is precisely the silent skip
    this script exists to remove."""
    acc = _accepted(tmp_path, "a", "b")
    r = _run("a", "--accepted", str(acc), "--graph", str(tmp_path / "nope"))
    assert r.returncode == 2
    assert r.stdout == ""
    assert acc.read_text() == "a\nb\n", "no partial reduction may be applied on a fault"


def test_an_unreadable_accepted_file_does_not_empty_the_merge_set(
    tmp_path: Path,
) -> None:
    """grep exit 1 is CONTENT (it filtered out the last line); anything above 1 is
    a machine fault. A blanket `|| true` would move the empty temp file over the
    accepted set -- silently emptying the merge set on an I/O error."""
    acc = _accepted(tmp_path, "a", "b")
    acc.chmod(0o000)
    if os.access(acc, os.R_OK):  # running as root: the fault cannot be provoked
        acc.chmod(0o644)
        pytest.skip("cannot make a file unreadable as this user")
    r = _run("a", "--accepted", str(acc))
    acc.chmod(0o644)
    assert r.returncode == 2, r.stderr
    assert r.stdout == ""
    assert acc.read_text() == "a\nb\n"


def test_bad_invocation_is_a_machine_fault(tmp_path: Path) -> None:
    acc = _accepted(tmp_path, "a")
    for args in (["--accepted", str(acc)], ["a"], ["a", "b", "--accepted", str(acc)]):
        r = _run(*args)
        assert r.returncode == 2, args
