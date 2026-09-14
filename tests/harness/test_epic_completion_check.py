"""Tests for scripts/epic-completion-check.sh (proj-v4rk).

`/land` Section 4 flags a parent epic `epic-ready-to-audit` once a landing
pass closes its last open child -- that label is the ONLY automatic trigger
for `/epic-audit`. The inline jq snippet this script replaced enumerated an
epic's children by reading `bd show <epic-id> --json`'s `.dependents[]`
array, but that array is populated ONLY when `bd show` is called with the
opt-in `--include-dependents` flag -- verified live against bd 1.1.0:
`dependent_count` is non-zero but the `dependents` key is entirely absent
from the JSON without the flag. So the old `$kids` derivation was always
`[]`, the `(($kids|length)>0)` false-positive guard always tripped short, and
no epic was EVER flagged -- silently, since a missed flag reads identically
to "not complete yet" (dead code that happened to fail safe).

`test_old_dependents_derivation_never_fires_even_when_fully_closed` below runs
the OLD snippet's logic directly and asserts it produces nothing. Its fixture
models an epic whose children are all closed *in the DB*, but that closedness
is deliberately NOT observable in the payload: the bug is precisely that
`bd show` omits the `dependents` key entirely, leaving only
`dependent_count: 2`. The snippet cannot tell "no children" from "children it
wasn't handed" -- which is why it failed silently. That test is a pinned
DEMONSTRATION, not a guard: it embeds the dead snippet inline and passes no
matter what this repo's scripts do (verified: it still passes with the scripts
deleted).

`test_last_child_closes_epic_is_flagged_ready` is the test meeting the ticket's
bar, "a test that FAILS against today's empty-$kids derivation" -- it drives
the real script, so reverting the derivation to the `.dependents[]` form fails
it. The rest of the suite likewise exercises
`scripts/epic-completion-check.sh` via a fake `bd` executable on PATH, exactly
like tests/test_epic_debate_gate.py does for the sibling epic-debate-gate.sh --
no real bd database is touched.

This module's `_fake_bd` is deliberately NOT unified with
tests/test_blocks_dependents.py's and tests/test_epic_children_closed.py's --
see the "RATIFIED DEFERRAL" paragraph in test_blocks_dependents.py's
docstring for the reasoning (proj-863q, ratified by proj-ea5b).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from conftest import fake_bin_env

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "epic-completion-check.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the script shells out to jq"
)


def _fake_bd(
    tmp_path: Path,
    *,
    show_fixtures: dict[str, list[dict]],
    list_fixtures: dict[str, list[dict]],
) -> Path:
    """A PATH dir holding a fake `bd` that serves `show_fixtures[<id>]` for
    `bd show <id> --json` and `list_fixtures[<parent-id>]` for
    `bd list --parent <parent-id> --all --limit 0 --json`. Only `bd` is shimmed;
    the real `jq` (used by both the fake `bd` and the script itself) still
    resolves via the inherited PATH, which `_run` appends after this dir."""
    show_path = tmp_path / "show_fixtures.json"
    show_path.write_text(json.dumps(show_fixtures))
    list_path = tmp_path / "list_fixtures.json"
    list_path.write_text(json.dumps(list_fixtures))

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()

    fake_bd = bin_dir / "bd"
    fake_bd.write_text(
        textwrap.dedent(f"""\
            #!/usr/bin/env bash
            # Fake `bd show`/`bd list --parent` for testing epic-completion-check.sh.
            set -euo pipefail
            case "$1" in
              show)
                id="$2"
                jq -c --arg id "$id" \\
                  '.[$id] // error("no show fixture for \\($id)")' "{show_path}"
                ;;
              list)
                # invoked as: bd list --parent <id> --all --limit 0 --json
                # -- the id is $3, so a new flag must go AFTER it (inserting one
                # before --parent shifts $3 and fails these tests loudly).
                parent="$3"
                jq -c --arg id "$parent" \\
                  '.[$id] // error("no list fixture for \\($id)")' "{list_path}"
                ;;
              *)
                echo "unsupported: $*" >&2
                exit 1
                ;;
            esac
            """)
    )
    fake_bd.chmod(0o755)
    return bin_dir


def _run(
    id_: str,
    tmp_path: Path,
    *,
    show_fixtures: dict[str, list[dict]],
    list_fixtures: dict[str, list[dict]],
) -> subprocess.CompletedProcess:
    bin_dir = _fake_bd(
        tmp_path, show_fixtures=show_fixtures, list_fixtures=list_fixtures
    )
    return subprocess.run(
        ["bash", str(SCRIPT), id_],
        env=fake_bin_env(bin_dir),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _epic(*, status: str = "open", labels: list[str] | None = None) -> list[dict]:
    return [
        {
            "id": "proj-epic",
            "issue_type": "epic",
            "status": status,
            "labels": labels or [],
        }
    ]


def test_no_parent_prints_nothing(tmp_path: Path) -> None:
    """A ticket with no parent epic — nothing to flag."""
    show_fixtures = {"proj-solo": [{"id": "proj-solo", "parent": None}]}
    result = _run("proj-solo", tmp_path, show_fixtures=show_fixtures, list_fixtures={})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_last_child_closes_epic_is_flagged_ready(tmp_path: Path) -> None:
    """The bug this ticket fixes: closing the LAST open child of an open epic
    must fire READY. Fails against the old .dependents[]-based derivation
    (see test_old_dependents_derivation_never_fires_even_when_fully_closed)."""
    show_fixtures = {
        "proj-child": [{"id": "proj-child", "parent": "proj-epic"}],
        "proj-epic": _epic(),
    }
    list_fixtures = {
        "proj-epic": [
            {"id": "proj-child", "status": "closed"},
            {"id": "proj-other-child", "status": "closed"},
        ]
    }
    result = _run(
        "proj-child",
        tmp_path,
        show_fixtures=show_fixtures,
        list_fixtures=list_fixtures,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "READY proj-epic"


def test_open_child_remaining_is_not_flagged(tmp_path: Path) -> None:
    """An epic with any open child must NOT be flagged."""
    show_fixtures = {
        "proj-child": [{"id": "proj-child", "parent": "proj-epic"}],
        "proj-epic": _epic(),
    }
    list_fixtures = {
        "proj-epic": [
            {"id": "proj-child", "status": "closed"},
            {"id": "proj-other-child", "status": "open"},
        ]
    }
    result = _run(
        "proj-child",
        tmp_path,
        show_fixtures=show_fixtures,
        list_fixtures=list_fixtures,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_zero_children_is_not_flagged(tmp_path: Path) -> None:
    """The false-positive guard: `all($kids[]; ...)` is vacuously TRUE on an
    empty array, so a zero-children result must NOT be flagged. This is the
    exact trap the ticket warns against inverting while fixing $kids."""
    show_fixtures = {
        "proj-child": [{"id": "proj-child", "parent": "proj-epic"}],
        "proj-epic": _epic(),
    }
    list_fixtures = {"proj-epic": []}
    result = _run(
        "proj-child",
        tmp_path,
        show_fixtures=show_fixtures,
        list_fixtures=list_fixtures,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_already_flagged_ready_is_not_reflagged(tmp_path: Path) -> None:
    """An epic already carrying epic-ready-to-audit is left alone."""
    show_fixtures = {
        "proj-child": [{"id": "proj-child", "parent": "proj-epic"}],
        "proj-epic": _epic(labels=["epic-ready-to-audit"]),
    }
    list_fixtures = {"proj-epic": [{"id": "proj-child", "status": "closed"}]}
    result = _run(
        "proj-child",
        tmp_path,
        show_fixtures=show_fixtures,
        list_fixtures=list_fixtures,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_already_audited_epic_is_not_reflagged(tmp_path: Path) -> None:
    """An epic already carrying epic-audited is left alone."""
    show_fixtures = {
        "proj-child": [{"id": "proj-child", "parent": "proj-epic"}],
        "proj-epic": _epic(labels=["epic-audited"]),
    }
    list_fixtures = {"proj-epic": [{"id": "proj-child", "status": "closed"}]}
    result = _run(
        "proj-child",
        tmp_path,
        show_fixtures=show_fixtures,
        list_fixtures=list_fixtures,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_already_closed_epic_is_not_reflagged(tmp_path: Path) -> None:
    """A parent that is itself already closed must NOT be flagged again."""
    show_fixtures = {
        "proj-child": [{"id": "proj-child", "parent": "proj-epic"}],
        "proj-epic": _epic(status="closed"),
    }
    list_fixtures = {"proj-epic": [{"id": "proj-child", "status": "closed"}]}
    result = _run(
        "proj-child",
        tmp_path,
        show_fixtures=show_fixtures,
        list_fixtures=list_fixtures,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_non_epic_parent_is_not_flagged(tmp_path: Path) -> None:
    """A `parent` link whose target isn't issue_type epic (e.g. a plain
    parent-child grouping ticket that isn't itself an epic) is not flagged."""
    show_fixtures = {
        "proj-child": [{"id": "proj-child", "parent": "proj-epic"}],
        "proj-epic": [
            {
                "id": "proj-epic",
                "issue_type": "task",
                "status": "open",
                "labels": [],
            }
        ],
    }
    list_fixtures = {"proj-epic": [{"id": "proj-child", "status": "closed"}]}
    result = _run(
        "proj-child",
        tmp_path,
        show_fixtures=show_fixtures,
        list_fixtures=list_fixtures,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_old_dependents_derivation_never_fires_even_when_fully_closed() -> None:
    """Pinned demonstration of the bug itself (not of the replacement script):
    the OLD inline jq snippet from .claude/skills/land/SKILL.md read
    `$epic.dependents`, which `bd show --json` never populates without the
    opt-in `--include-dependents` flag. Against a `bd show`-shaped fixture with
    NO `dependents` key (matching real bd 1.1.0 output) -- modeling an epic
    whose children are all closed, though the payload cannot say so, which IS
    the bug -- the old derivation produces nothing.

    NOT the test that meets the ticket's acceptance bar, and NOT a regression
    guard: the snippet under test is embedded here rather than read from the
    repo, so this passes regardless of repo state. The bar ("a test that FAILS
    against today's empty-$kids derivation") is met by
    `test_last_child_closes_epic_is_flagged_ready`, which drives the real
    script. This test keeps the dead derivation's behavior on the record now
    that no copy of it survives in the tree.
    """
    epic = {
        "id": "proj-epic",
        "issue_type": "epic",
        "status": "open",
        "labels": [],
        # deliberately no "dependents" key -- matches real bd show --json
        # without --include-dependents, despite a non-zero dependent_count.
        "dependent_count": 2,
    }
    old_snippet = """
        .[0] as $e |
        (($e.dependents // []) | map(select(.dependency_type=="parent-child"))) as $kids |
        ($e.labels // []) as $lbl |
        if ($e.issue_type=="epic") and ($e.status!="closed")
           and (($kids|length)>0) and (all($kids[]; .status=="closed"))
           and (($lbl | index("epic-audited")) | not)
           and (($lbl | index("epic-ready-to-audit")) | not)
        then "READY" else "" end
    """
    result = subprocess.run(
        ["jq", "-r", old_snippet],
        input=json.dumps([epic]),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        "the OLD derivation fired READY -- this fixture no longer "
        "reproduces the bug this ticket exists to fix"
    )
