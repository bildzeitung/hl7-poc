"""scripts/land-merge-batch.sh -- /land's first-pass merge loop, on real repos.

The loop was fenced in land/SKILL.md and near-duplicated in the isolation-replay
path under a comment asking a human to keep the two in sync. The cases below pin
the three things that were previously only prose:

  * a machine fault (rc=2) STOPS the pass and is never read as a clean merge
  * a conflict takes the branch's dependents out of the accepted set, in the FILE
  * a dependent whose base conflicted earlier is SKIPPED, not merged
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from conftest import REPO_ROOT


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    """A repo with an origin, so `origin/land/<id>` refs are real."""
    origin = tmp_path / "o"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main", ".")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main", ".")
    (seed / "f.txt").write_text("base\n")
    _git(seed, "add", "f.txt")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")

    repo = tmp_path / "r"
    _git(tmp_path, "clone", "-q", str(origin), str(repo))
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    shutil.copytree(REPO_ROOT / "scripts", repo / "scripts")
    return repo


def _land_branch(repo: Path, tid: str, path: str, content: str) -> None:
    _git(repo, "checkout", "-q", "-B", f"tmp/{tid}", "origin/main")
    (repo / path).write_text(content)
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", f"work {tid}")
    _git(repo, "push", "-q", "origin", f"tmp/{tid}:land/{tid}")
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "checkout", "-q", "main")


def _state(repo: Path, *ids: str) -> dict[str, Path]:
    d = repo / ".git" / "land-state"
    (d / "msg").mkdir(parents=True)
    (d / "conflicts").mkdir(parents=True)
    (d / "accepted").write_text("".join(f"{i}\n" for i in ids))
    (d / "landed").write_text("")
    for i in ids:
        (d / "msg" / i).write_text(f"Merge land/{i}")
    return {
        "accepted": d / "accepted",
        "landed": d / "landed",
        "msg": d / "msg",
        "conflicts": d / "conflicts",
    }


def _batch(repo: Path, st: dict[str, Path], graph: Path | None = None):
    args = [
        "bash",
        str(repo / "scripts" / "land-merge-batch.sh"),
        "--accepted",
        str(st["accepted"]),
        "--landed",
        str(st["landed"]),
        "--msg-dir",
        str(st["msg"]),
        "--conflicts-dir",
        str(st["conflicts"]),
    ]
    if graph:
        args += ["--graph", str(graph)]
    return subprocess.run(args, cwd=repo, capture_output=True, text=True, check=False)


def _records(r) -> list[tuple[str, str]]:
    return [tuple(l.split("\t")) for l in r.stdout.splitlines() if "\t" in l]


def test_empty_accepted_set_is_a_clean_no_op(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    st = _state(repo)
    r = _batch(repo, st)
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""


def test_all_clean_merges_land_and_are_recorded(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _land_branch(repo, "a", "a.txt", "a")
    _land_branch(repo, "b", "b.txt", "b")
    st = _state(repo, "a", "b")
    r = _batch(repo, st)
    assert r.returncode == 0, r.stderr
    assert _records(r) == [("LANDED", "a"), ("LANDED", "b")]
    assert st["landed"].read_text() == "a\nb\n"
    assert "a.txt" in _git(repo, "ls-files") and "b.txt" in _git(repo, "ls-files")


def test_a_conflicting_branch_is_reported_and_never_recorded_as_landed(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _land_branch(repo, "a", "f.txt", "from a\n")
    _land_branch(repo, "b", "f.txt", "from b\n")  # same file, conflicts with a
    st = _state(repo, "a", "b")
    r = _batch(repo, st)
    assert r.returncode == 1, f"a conflict is a real finding: {r.stdout} {r.stderr}"
    assert ("LANDED", "a") in _records(r)
    assert ("CONFLICT", "b") in _records(r)
    assert st["landed"].read_text() == "a\n", (
        "a conflicted branch must never reach the landed set"
    )
    assert (st["conflicts"] / "b").read_text().strip() != "", (
        "conflicting paths must be persisted"
    )


def test_a_conflict_drops_its_dependents_from_the_accepted_file(tmp_path: Path) -> None:
    """Section 3a's invariant, at the point it is actually enforced."""
    repo = _repo(tmp_path)
    _land_branch(repo, "a", "f.txt", "from a\n")
    _land_branch(repo, "base", "f.txt", "from base\n")  # conflicts with a
    _land_branch(repo, "dep", "d.txt", "dep\n")
    st = _state(repo, "a", "base", "dep")
    graph = repo / "graph"
    graph.write_text("EDGE\tdep\tbase\tdirect\n")

    r = _batch(repo, st, graph=graph)
    assert r.returncode == 1, r.stderr
    recs = _records(r)
    assert ("CONFLICT", "base") in recs
    assert ("HELD", "dep") in recs, "a dependent of a departed base must be held"
    assert ("LANDED", "dep") not in recs, (
        "merging it would land base's rejected content"
    )
    assert "dep" not in st["accepted"].read_text()
    assert st["landed"].read_text() == "a\n"


def test_a_dependent_already_dropped_is_skipped_not_merged(tmp_path: Path) -> None:
    """The accepted file mutates mid-iteration; the loop must re-check membership
    rather than trusting the order snapshot it started from."""
    repo = _repo(tmp_path)
    _land_branch(repo, "a", "f.txt", "from a\n")
    _land_branch(repo, "base", "f.txt", "from base\n")
    _land_branch(repo, "dep", "d.txt", "dep\n")
    st = _state(repo, "a", "base", "dep")
    graph = repo / "graph"
    graph.write_text("EDGE\tdep\tbase\tdirect\n")
    r = _batch(repo, st, graph=graph)
    # `dep` is dropped when `base` conflicts, so its own turn must not merge it.
    assert ("LANDED", "dep") not in _records(r)
    assert "d.txt" not in _git(repo, "ls-files")


def test_a_machine_fault_stops_the_pass_and_is_not_a_clean_merge(
    tmp_path: Path,
) -> None:
    """A missing message file makes land-merge-one.sh exit 2. The negated-`if`
    idiom this script exists to avoid would read that 2 as rc=0 and record a
    LANDED that never happened."""
    repo = _repo(tmp_path)
    _land_branch(repo, "a", "a.txt", "a")
    _land_branch(repo, "b", "b.txt", "b")
    st = _state(repo, "a", "b")
    (st["msg"] / "b").unlink()

    r = _batch(repo, st)
    assert r.returncode == 2, f"a fault must stop the pass: {r.stdout}"
    assert ("FAULT", "b") in _records(r)
    assert ("LANDED", "b") not in _records(r)
    assert "b" not in st["landed"].read_text().split()


def test_missing_accepted_file_is_a_fault_not_an_empty_set(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    st = _state(repo)
    st["accepted"].unlink()
    r = _batch(repo, st)
    assert r.returncode == 2
    assert "does not exist" in r.stderr


def test_a_drop_side_fault_stops_the_pass_instead_of_being_swallowed(
    tmp_path: Path,
) -> None:
    """The reduction used to run as `drop-from-accepted.sh | awk`, whose status
    is awk's (always 0) -- a drop-side machine fault was swallowed and the
    conflicted branch's dependents were silently left in the accepted set. The
    drop's output is captured and its own status checked now."""
    repo = _repo(tmp_path)
    _land_branch(repo, "a", "f.txt", "from a\n")
    _land_branch(repo, "base", "f.txt", "from base\n")  # conflicts with a
    st = _state(repo, "a", "base")
    sabotaged = repo / "scripts" / "drop-from-accepted.sh"
    sabotaged.write_text("#!/usr/bin/env bash\necho boom >&2\nexit 2\n")
    r = _batch(repo, st)
    assert r.returncode == 2, f"{r.stdout}\n{r.stderr}"
    assert "drop-from-accepted.sh failed" in r.stderr
