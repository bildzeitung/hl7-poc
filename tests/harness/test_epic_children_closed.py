"""Tests for scripts/epic-children-closed.sh (proj-v4rk).

Shared "are this epic's parent-child children ALL closed" check, factored out
because three skills each carried their OWN inline copy of the same broken
derivation: `/land`'s Section-4 epic-completion flag, `/epic-audit`'s
"confirm from live state" gate, and `/sweep`'s "epics ready for a human
close-decision" step. All three read `bd show <epic-id> --json`'s
`.dependents[]` array, which `bd show` only populates with the opt-in
`--include-dependents` flag -- verified live against bd 1.1.0:
`dependent_count` is non-zero but the `dependents` key is entirely absent
without the flag. So every one of the three call sites always saw an empty
`$kids`, and the `(($kids|length)>0)` false-positive guard always tripped
short -- dead code in three places, all failing silently safe.

`test_old_dependents_derivation_never_fires_even_when_fully_closed` runs the
OLD shared snippet's logic directly and asserts it produces nothing. Note what
its fixture can and cannot express: the children are closed *in the modeled
DB*, but that is deliberately NOT observable in the payload, because the whole
bug is that `bd show` omits the `dependents` key entirely (only
`dependent_count: 2` survives). The old snippet therefore cannot distinguish
"no children" from "children it wasn't given" -- which is exactly why it failed
silently. That test is a pinned DEMONSTRATION of the old bug, not a regression
guard: it embeds the dead snippet inline and so passes no matter what this
repo's scripts do (verified: it still passes with the scripts deleted outright).

The actual guards are the three tests that exercise the replacement script via
a fake `bd` on PATH -- they are what fails if the derivation regresses (verified
by reverting the script to the `.dependents[]` form: all three fail). That is
the test satisfying the ticket's bar, "a test that FAILS against today's
empty-$kids derivation". Same fake-`bd` pattern as
tests/test_epic_debate_gate.py and tests/test_epic_completion_check.py.

This module's `_fake_bd` is deliberately NOT unified with
tests/test_blocks_dependents.py's and tests/test_epic_completion_check.py's --
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
SCRIPT = REPO_ROOT / "scripts" / "epic-children-closed.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the script shells out to jq"
)


def _fake_bd(tmp_path: Path, list_fixtures: dict[str, list[dict]]) -> Path:
    """A PATH dir holding a fake `bd` that serves `list_fixtures[<epic-id>]`
    for `bd list --parent <epic-id> --all --limit 0 --json`."""
    list_path = tmp_path / "list_fixtures.json"
    list_path.write_text(json.dumps(list_fixtures))

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()

    fake_bd = bin_dir / "bd"
    fake_bd.write_text(
        textwrap.dedent(f"""\
            #!/usr/bin/env bash
            # Fake `bd list --parent` for testing epic-children-closed.sh.
            set -euo pipefail
            [ "$1" = "list" ] || {{ echo "unsupported: $*" >&2; exit 1; }}
            # invoked as: bd list --parent <id> --all --limit 0 --json
            # -- the id is $3, so a new flag must go AFTER it (inserting one
            # before --parent shifts $3 and fails these tests loudly).
            parent="$3"
            jq -c --arg id "$parent" \\
              '.[$id] // error("no list fixture for \\($id)")' "{list_path}"
            """)
    )
    fake_bd.chmod(0o755)
    return bin_dir


def _run(
    epic_id: str, tmp_path: Path, *, list_fixtures: dict[str, list[dict]]
) -> subprocess.CompletedProcess:
    bin_dir = _fake_bd(tmp_path, list_fixtures)
    return subprocess.run(
        ["bash", str(SCRIPT), epic_id],
        env=fake_bin_env(bin_dir),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_all_children_closed_is_true(tmp_path: Path) -> None:
    fixtures = {
        "proj-epic": [
            {"id": "proj-a", "status": "closed"},
            {"id": "proj-b", "status": "closed"},
        ]
    }
    result = _run("proj-epic", tmp_path, list_fixtures=fixtures)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "true"


def test_one_open_child_is_false(tmp_path: Path) -> None:
    fixtures = {
        "proj-epic": [
            {"id": "proj-a", "status": "closed"},
            {"id": "proj-b", "status": "open"},
        ]
    }
    result = _run("proj-epic", tmp_path, list_fixtures=fixtures)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "false"


def test_zero_children_is_false(tmp_path: Path) -> None:
    """The false-positive guard: `all($kids[]; ...)` is vacuously TRUE on an
    empty array, so zero children must read `false`, not `true`."""
    result = _run("proj-epic", tmp_path, list_fixtures={"proj-epic": []})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "false"


def test_old_dependents_derivation_never_fires_even_when_fully_closed() -> None:
    """Pinned demonstration of the bug this script replaces everywhere it was
    copied (`/land`, `/epic-audit`, `/sweep`): the OLD inline jq snippet read
    `$epic.dependents`, which `bd show --json` never populates without the
    opt-in `--include-dependents` flag. Against a `bd show`-shaped fixture with
    NO `dependents` key (matching real bd 1.1.0 output) but a non-zero
    `dependent_count`, the old derivation yields an empty `$kids` and so never
    reports the epic's children as closed -- a real, silent bug in all three
    copies.

    NOT a regression guard: the snippet under test is embedded here, not read
    from the repo, so this passes regardless of what the scripts do. It exists
    to keep the dead derivation's behavior on the record now that no copy of it
    survives in the tree. The guards are the fake-`bd` tests above.
    """
    epic = {
        "id": "proj-epic",
        "issue_type": "epic",
        "status": "open",
        # deliberately no "dependents" key -- matches real bd show --json
        # without --include-dependents, despite a non-zero dependent_count.
        "dependent_count": 2,
    }
    old_snippet = """
        .[0] as $e |
        (($e.dependents // []) | map(select(.dependency_type=="parent-child"))) as $kids |
        if ($e.issue_type=="epic") and ($e.status!="closed")
           and (($kids|length)>0) and (all($kids[]; .status=="closed"))
        then "AUDITABLE" else "SKIP" end
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
    assert result.stdout.strip() == "SKIP", (
        "the OLD derivation reported AUDITABLE -- this fixture no longer "
        "reproduces the bug this ticket exists to fix"
    )
