"""scripts/land-replay.sh -- the resumable isolation replay, on real repos.

The replay runs `5 + 5N` gate sessions in a path that cannot be backgrounded
and is capped at 600s per tool call. A straight-through loop hits that ceiling
mid-attribution, so nothing is bounced and the next pass rebuilds the same set and
reds again. These cases pin the properties that stop that:

  * a baseline red is NOT attributed to a branch (it would delete an innocent one)
  * a culprit is backed out and REPORTED, never bounced here
  * a resume never re-merges what an earlier invocation finished
  * the deadline yields with progress persisted, and resuming completes the work

Gates are stubbed through LAND_GATE_CMD so the cases run in under a second; the
real sessions are what the stub stands in for, not what is under test here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
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
    # Test scaffolding (the copied scripts/, the gate stub, its baseline marker)
    # is untracked in this throwaway clone; exclude it so the script's and the
    # tests' clean-tree checks see only what a branch actually changed.
    (repo / ".git" / "info" / "exclude").write_text(
        "/scripts/\n/gate-stub.sh\n/.replay-baselined\n"
    )
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
        "state": d / "replay-state",
    }


def _stub(
    repo: Path,
    *,
    red_when_present: str | None = None,
    red_phase: str = "tests",
    baseline_red: bool = False,
) -> Path:
    """A gate stub. Reds only when a marker file is present in the tree, which is
    how a specific branch's content is made to 'turn the gate red'."""
    p = repo / "gate-stub.sh"
    cond = (
        f'if [ -f "{red_when_present}" ] && [ "$1" = "{red_phase}" ]; then exit 1; fi'
        if red_when_present
        else ":"
    )
    base = (
        "if [ ! -f .replay-baselined ]; then touch .replay-baselined; exit 1; fi"
        if baseline_red
        else ":"
    )
    p.write_text(f"#!/usr/bin/env bash\n{base}\n{cond}\nexit 0\n")
    p.chmod(0o755)
    return p


def _replay(
    repo: Path, st: dict[str, Path], stub: Path, *extra: str, graph: Path | None = None
):
    env = {**os.environ, "LAND_GATE_CMD": str(stub)}
    args = [
        "bash",
        str(repo / "scripts" / "land-replay.sh"),
        "--accepted",
        str(st["accepted"]),
        "--landed",
        str(st["landed"]),
        "--msg-dir",
        str(st["msg"]),
        "--conflicts-dir",
        str(st["conflicts"]),
        "--state",
        str(st["state"]),
        "--base-ref",
        "origin/main",
        *extra,
    ]
    if graph:
        args += ["--graph", str(graph)]
    return subprocess.run(
        args, cwd=repo, capture_output=True, text=True, env=env, check=False
    )


def _records(r) -> list[tuple[str, str]]:
    return [tuple(l.split("\t")) for l in r.stdout.splitlines() if "\t" in l]


def test_all_green_replays_every_branch_and_finishes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _land_branch(repo, "a", "a.txt", "a")
    _land_branch(repo, "b", "b.txt", "b")
    st = _state(repo, "a", "b")
    r = _replay(repo, st, _stub(repo))
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert _records(r) == [("SURVIVOR", "a"), ("SURVIVOR", "b")]
    assert st["landed"].read_text() == "a\nb\n"


def test_baseline_red_is_never_attributed_to_a_branch(tmp_path: Path) -> None:
    """The incident this guards: an ambient env var reddened the suite on a bare
    ref with nothing merged. Attributing it would close an innocent ticket and
    delete a reviewed branch."""
    repo = _repo(tmp_path)
    _land_branch(repo, "a", "a.txt", "a")
    st = _state(repo, "a")
    r = _replay(repo, st, _stub(repo, baseline_red=True))
    assert r.returncode == 2, r.stdout
    assert "before any branch merged" in r.stderr
    assert _records(r) == [], "no branch may be named on a baseline red"
    assert st["landed"].read_text() == ""


@pytest.mark.parametrize("red_phase", ["fix", "tests", "harness", "build"])
def test_a_culprit_is_backed_out_and_reported_not_bounced(
    tmp_path: Path, red_phase: str
) -> None:
    """Every phase is pinned separately: a gate the per-branch loop forgets to
    invoke, or whose rc it drops, would let a red one through as a SURVIVOR."""
    repo = _repo(tmp_path)
    _land_branch(repo, "good", "good.txt", "g")
    _land_branch(repo, "bad", "bad.txt", "b")
    st = _state(repo, "good", "bad")
    r = _replay(repo, st, _stub(repo, red_when_present="bad.txt", red_phase=red_phase))
    assert r.returncode == 1, f"{r.stdout}\n{r.stderr}"
    assert ("SURVIVOR", "good") in _records(r)
    assert ("CULPRIT", "bad") in _records(r)
    assert st["landed"].read_text() == "good\n", (
        "a culprit must never reach the landed set"
    )
    assert (repo / "good.txt").exists(), "the survivor stays merged"
    assert not (repo / "bad.txt").exists(), "the culprit must be backed out of the tree"


def test_resume_after_a_culprit_does_not_remerge_it(tmp_path: Path) -> None:
    """The caller bounces or escalates the culprit, then re-invokes. Re-merging it
    would re-red the gate forever."""
    repo = _repo(tmp_path)
    _land_branch(repo, "good", "good.txt", "g")
    _land_branch(repo, "bad", "bad.txt", "b")
    _land_branch(repo, "later", "later.txt", "l")
    st = _state(repo, "good", "bad", "later")
    stub = _stub(repo, red_when_present="bad.txt")

    first = _replay(repo, st, stub)
    assert first.returncode == 1
    assert ("CULPRIT", "bad") in _records(first)

    second = _replay(repo, st, stub)
    assert second.returncode == 0, f"{second.stdout}\n{second.stderr}"
    recs = _records(second)
    assert ("SURVIVOR", "later") in recs
    assert all(rec[1] != "bad" for rec in recs), "the culprit must not be retried"
    assert ("SURVIVOR", "good") not in recs, (
        "an already-finished survivor must not be re-merged"
    )
    assert st["landed"].read_text() == "good\nlater\n"


def test_resume_does_not_re_baseline_or_reset_away_survivors(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _land_branch(repo, "good", "good.txt", "g")
    _land_branch(repo, "bad", "bad.txt", "b")
    st = _state(repo, "good", "bad")
    stub = _stub(repo, red_when_present="bad.txt")
    _replay(repo, st, stub)
    assert (repo / "good.txt").exists()
    _replay(repo, st, stub)
    assert (repo / "good.txt").exists(), (
        "a resume must not reset the tree back to the base ref"
    )


def test_deadline_yields_with_progress_persisted_then_resumes(tmp_path: Path) -> None:
    """A deadline of 40s with a stub that sleeps past the per-branch budget forces
    the yield after the first branch; the resume must finish the rest."""
    repo = _repo(tmp_path)
    for t in ("a", "b", "c"):
        _land_branch(repo, t, f"{t}.txt", t)
    st = _state(repo, "a", "b", "c")
    # The harness phase answers instantly: the real gate is a constant-time
    # skip unless the branch touched a harness path, so a sleeping stub there
    # would charge the deadline for time no real replay spends.
    slow = repo / "slow-stub.sh"
    slow.write_text(
        '#!/usr/bin/env bash\n[ "$1" = harness ] && exit 0\nsleep 4\nexit 0\n'
    )
    slow.chmod(0o755)

    first = _replay(repo, st, slow, "--deadline-seconds", "40")
    assert first.returncode == 3, f"{first.stdout}\n{first.stderr}"
    more = [r for r in _records(first) if r[0] == "MORE"]
    assert more, "a deadline yield must report how much is left"
    survivors = [r[1] for r in _records(first) if r[0] == "SURVIVOR"]
    assert survivors, "the yield must come after real progress, not before any"
    assert st["landed"].read_text().split() == survivors

    fast = _replay(repo, st, _stub(repo), "--deadline-seconds", "570")
    assert fast.returncode == 0, f"{fast.stdout}\n{fast.stderr}"
    assert st["landed"].read_text().split() == ["a", "b", "c"]


def test_deadline_is_clamped_below_the_tool_cap(tmp_path: Path) -> None:
    """A caller passing 900 would reintroduce the ceiling this script exists to
    stay under, so the clamp is not advisory."""
    repo = _repo(tmp_path)
    _land_branch(repo, "a", "a.txt", "a")
    st = _state(repo, "a")
    r = _replay(repo, st, _stub(repo), "--deadline-seconds", "9000")
    assert r.returncode == 0, r.stderr
    assert ("SURVIVOR", "a") in _records(r)


def test_a_conflict_drops_dependents_and_the_replay_continues(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _land_branch(repo, "a", "f.txt", "from a\n")
    _land_branch(repo, "base", "f.txt", "from base\n")  # conflicts with a
    _land_branch(repo, "dep", "d.txt", "dep\n")
    st = _state(repo, "a", "base", "dep")
    graph = repo / "graph"
    graph.write_text("EDGE\tdep\tbase\tdirect\n")

    r = _replay(repo, st, _stub(repo), graph=graph)
    recs = _records(r)
    assert ("SURVIVOR", "a") in recs
    assert ("CONFLICT", "base") in recs
    assert ("HELD", "dep") in recs
    assert ("SURVIVOR", "dep") not in recs
    assert st["landed"].read_text() == "a\n"


def test_a_merge_machine_fault_stops_the_pass(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _land_branch(repo, "a", "a.txt", "a")
    _land_branch(repo, "b", "b.txt", "b")
    st = _state(repo, "a", "b")
    (st["msg"] / "b").unlink()
    r = _replay(repo, st, _stub(repo))
    assert r.returncode == 2, r.stdout
    assert ("FAULT", "b") in _records(r)
    assert "b" not in st["landed"].read_text().split()


def test_bad_invocation_is_a_machine_fault(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    st = _state(repo, "a")
    r = _replay(repo, st, _stub(repo), "--deadline-seconds", "not-a-number")
    assert r.returncode == 2
    assert "must be an integer" in r.stderr


def test_a_mid_loop_gate_127_is_a_machine_fault_never_a_culprit(tmp_path: Path) -> None:
    """Exit 1 is the only content verdict a gate has. A 127 (nox vanished from
    PATH mid-run), 126, or 128+n is the machine, not the branch that happened to
    be merged when it hit -- backing it out and reporting a CULPRIT would bounce
    a perfectly good branch on a bootstrap gap."""
    repo = _repo(tmp_path)
    _land_branch(repo, "good", "good.txt", "g")
    _land_branch(repo, "bad", "bad.txt", "b")
    st = _state(repo, "good", "bad")
    stub = repo / "gate-stub.sh"
    stub.write_text(
        '#!/usr/bin/env bash\nif [ -f bad.txt ] && [ "$1" = tests ]; then exit 127; fi\nexit 0\n'
    )
    stub.chmod(0o755)
    r = _replay(repo, st, stub)
    assert r.returncode == 2, f"{r.stdout}\n{r.stderr}"
    assert ("CULPRIT", "bad") not in _records(r), (
        "a 127 must never be read as a content red"
    )
    assert "machine fault" in r.stderr
    assert st["landed"].read_text() == "good\n"


def test_a_baseline_reformat_is_gate_could_not_run(tmp_path: Path) -> None:
    """The fix session runs a formatter, so it can exit 0 while REFORMATTING the
    bare base tree. Nothing in the accepted set is to blame either way; landing
    anything on top of it would pollute every per-branch dirty-tree check."""
    repo = _repo(tmp_path)
    _land_branch(repo, "a", "a.txt", "a")
    st = _state(repo, "a")
    stub = repo / "gate-stub.sh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = fix ] && [ ! -f .fix-ran ]; then touch .fix-ran; echo dirty >> f.txt; fi\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    r = _replay(repo, st, stub)
    assert r.returncode == 2, f"{r.stdout}\n{r.stderr}"
    assert "reformatted the bare base tree" in r.stderr
    assert _records(r) == [], "a baseline reformat must not be attributed to any branch"


def test_a_survivor_reformat_is_amended_into_the_merge_commit(tmp_path: Path) -> None:
    """A green fix session may still reformat the just-merged branch's files.
    Left loose, that dirties the tree for the NEXT iteration's `git merge`, which
    most likely machine-faults against it -- so it is folded into the merge
    commit before the loop continues."""
    repo = _repo(tmp_path)
    _land_branch(repo, "a", "a.txt", "raw\n")
    _land_branch(repo, "b", "b.txt", "b")
    st = _state(repo, "a", "b")
    stub = repo / "gate-stub.sh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = fix ] && [ -f a.txt ]; then printf "formatted\\n" > a.txt; fi\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    r = _replay(repo, st, stub)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert [rec for rec in _records(r) if rec[0] == "SURVIVOR"] == [
        ("SURVIVOR", "a"),
        ("SURVIVOR", "b"),
    ]
    assert (repo / "a.txt").read_text() == "formatted\n"
    dirty = _git(repo, "status", "--porcelain")
    assert dirty == "", f"the reformat was left loose in the tree: {dirty!r}"
