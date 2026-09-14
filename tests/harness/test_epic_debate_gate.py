"""Tests for scripts/epic-debate-gate.sh (proj-bw5k).

`/code` auto-select must refuse to build a child ticket whose parent epic has
never been debated at least once — `/challenge` is supposed to be the mandatory
stress-test gate before an epic's children get built, but nothing enforced it
(exactly what happened with `proj-olmi`: its children were built and landed
without the epic ever being debated). `/challenge` now stamps a durable
`epic-debated` label on an epic when it debates it
(.claude/skills/challenge/SKILL.md); this script is the mechanical check
`/code`'s auto-select step (.claude/skills/code/SKILL.md) runs, per candidate
ticket, after the existing human/epic filter (proj-8pqv).

These tests never touch a real bd database: a fake `bd` executable on PATH
serves canned `bd show <id> --json` fixtures keyed by id, so the script's own
dependency-array / label logic is what's under test, not bd itself.
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
SCRIPT = REPO_ROOT / "scripts" / "epic-debate-gate.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the gate shells out to jq"
)


def _show_payload(
    id_: str, *, epic: str | None, labels: list[str] | None
) -> list[dict]:
    """A `bd show <id> --json` payload shaped like real bd output.

    Real `bd show` embeds the parent epic as a nested object inside
    `.dependencies[]` (not via top-level `parent_id`/`epic_id`, which are
    null) — this mirrors only the fields the gate script actually reads.
    """
    deps = None
    if epic is not None:
        deps = [
            {
                "id": epic,
                "issue_type": "epic",
                "dependency_type": "parent-child",
            }
        ]
    return [{"id": id_, "labels": labels, "dependencies": deps}]


def _fake_bd(tmp_path: Path, fixtures: dict[str, list[dict]]) -> Path:
    """A PATH dir holding a fake `bd` that serves `fixtures[<id>]` for
    `bd show <id> --json`. Only `bd` is shimmed; the real `jq` (used by both the
    fake `bd` and the gate script itself) still resolves via the inherited
    PATH, which `_run` appends after this dir."""
    fixtures_path = tmp_path / "fixtures.json"
    fixtures_path.write_text(json.dumps(fixtures))

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()

    fake_bd = bin_dir / "bd"
    fake_bd.write_text(
        textwrap.dedent(f"""\
            #!/usr/bin/env bash
            # Fake `bd show <id> --json` for testing epic-debate-gate.sh.
            set -euo pipefail
            [ "$1" = "show" ] || {{ echo "unsupported: $*" >&2; exit 1; }}
            id="$2"
            jq -c --arg id "$id" '.[$id] // error("no fixture for \\($id)")' "{fixtures_path}"
            """)
    )
    fake_bd.chmod(0o755)
    return bin_dir


def _run(
    id_: str, fixtures: dict[str, list[dict]], tmp_path: Path
) -> subprocess.CompletedProcess:
    bin_dir = _fake_bd(tmp_path, fixtures)
    return subprocess.run(
        ["bash", str(SCRIPT), id_],
        env=fake_bin_env(bin_dir),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_no_epic_ticket_builds(tmp_path: Path) -> None:
    """A ticket with no parent epic is unaffected — nothing to debate-gate."""
    fixtures = {"proj-solo": _show_payload("proj-solo", epic=None, labels=None)}
    result = _run("proj-solo", fixtures, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "BUILD proj-solo"


def test_child_of_debated_epic_builds(tmp_path: Path) -> None:
    """A child of an epic carrying `epic-debated` builds normally."""
    fixtures = {
        "proj-olmi.4": _show_payload("proj-olmi.4", epic="proj-olmi", labels=None),
        "proj-olmi": _show_payload("proj-olmi", epic=None, labels=["epic-debated"]),
    }
    result = _run("proj-olmi.4", fixtures, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "BUILD proj-olmi.4"


def test_child_of_undebated_epic_is_skipped(tmp_path: Path) -> None:
    """A child of an epic that has never been debated is refused, with the
    epic id named in the reason so the operator can act on it."""
    fixtures = {
        "proj-olmi.4": _show_payload("proj-olmi.4", epic="proj-olmi", labels=None),
        "proj-olmi": _show_payload("proj-olmi", epic=None, labels=None),
    }
    result = _run("proj-olmi.4", fixtures, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "SKIP proj-olmi.4 epic not debated (proj-olmi)"


def test_child_of_epic_with_other_labels_but_not_debated_is_skipped(
    tmp_path: Path,
) -> None:
    """The gate checks specifically for `epic-debated`, not just any label."""
    fixtures = {
        "proj-x.1": _show_payload("proj-x.1", epic="proj-x", labels=None),
        "proj-x": _show_payload("proj-x", epic=None, labels=["human"]),
    }
    result = _run("proj-x.1", fixtures, tmp_path)
    assert result.stdout.strip() == "SKIP proj-x.1 epic not debated (proj-x)"
