"""Tests for scripts/epic-audit-stale.sh (hl7-poc-2bo).

A parent-child child added after an audit (found in code review, a /land
bounce re-parent, by hand, or the audit's own gap tickets) must re-arm the
epic, or the `epic-audited` claim silently goes stale. This script is the
shared derivation of staleness -- "true" when the epic carries
`epic-audited` AND has a parent-child child whose `created_at` is at or after
the `audited_at` metadata stamped at the last audit --
reused by both `scripts/epic-completion-check.sh` (/land's re-arm signal) and
/epic-audit's own safety-net selection query, so the two can never drift.

Same fake-`bd` pattern as test_epic_completion_check.py and
test_epic_children_closed.py (shims `bd show` and `bd list --parent`).
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
SCRIPT = REPO_ROOT / "scripts" / "epic-audit-stale.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the script shells out to jq"
)


def _fake_bd(
    tmp_path: Path,
    *,
    show_fixtures: dict[str, list[dict]],
    list_fixtures: dict[str, list[dict]],
) -> Path:
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
            # Fake `bd show`/`bd list --parent` for testing epic-audit-stale.sh.
            set -euo pipefail
            case "$1" in
              show)
                id="$2"
                jq -c --arg id "$id" \\
                  '.[$id] // error("no show fixture for \\($id)")' "{show_path}"
                ;;
              list)
                # invoked as: bd list --parent <id> --all --limit 0 --json
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
    epic_id: str,
    tmp_path: Path,
    *,
    show_fixtures: dict[str, list[dict]],
    list_fixtures: dict[str, list[dict]],
) -> subprocess.CompletedProcess:
    bin_dir = _fake_bd(
        tmp_path, show_fixtures=show_fixtures, list_fixtures=list_fixtures
    )
    return subprocess.run(
        ["bash", str(SCRIPT), epic_id],
        env=fake_bin_env(bin_dir),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _epic(
    *, labels: list[str] | None = None, audited_at: str | None = None
) -> list[dict]:
    entry: dict = {"id": "proj-epic", "labels": labels or []}
    if audited_at is not None:
        entry["metadata"] = {"audited_at": audited_at}
    return [entry]


def test_not_epic_audited_is_false(tmp_path: Path) -> None:
    """No epic-audited label at all -- never stale (nothing to be stale about)."""
    show_fixtures = {"proj-epic": _epic(labels=[])}
    result = _run("proj-epic", tmp_path, show_fixtures=show_fixtures, list_fixtures={})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "false"


def test_audited_with_no_newer_child_is_false(tmp_path: Path) -> None:
    """epic-audited, and every parent-child child predates audited_at -- the
    audit still holds."""
    show_fixtures = {
        "proj-epic": _epic(labels=["epic-audited"], audited_at="2026-09-15T00:00:00Z")
    }
    list_fixtures = {
        "proj-epic": [
            {"id": "proj-child", "created_at": "2026-09-10T00:00:00Z"},
        ]
    }
    result = _run(
        "proj-epic",
        tmp_path,
        show_fixtures=show_fixtures,
        list_fixtures=list_fixtures,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "false"


def test_audited_with_newer_child_is_true(tmp_path: Path) -> None:
    """The bug this ticket fixes: a parent-child child created AFTER
    audited_at means the epic-audited stamp is stale and must re-arm."""
    show_fixtures = {
        "proj-epic": _epic(labels=["epic-audited"], audited_at="2026-09-15T00:00:00Z")
    }
    list_fixtures = {
        "proj-epic": [
            {"id": "proj-old-child", "created_at": "2026-09-10T00:00:00Z"},
            {"id": "proj-new-child", "created_at": "2026-09-18T00:00:00Z"},
        ]
    }
    result = _run(
        "proj-epic",
        tmp_path,
        show_fixtures=show_fixtures,
        list_fixtures=list_fixtures,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "true"


def test_audited_with_no_audited_at_metadata_is_false(tmp_path: Path) -> None:
    """A legacy epic-audited epic from before this mechanism existed carries
    no audited_at metadata at all -- treated as not stale rather than
    force-re-auditing every such epic."""
    show_fixtures = {"proj-epic": _epic(labels=["epic-audited"], audited_at=None)}
    list_fixtures = {
        "proj-epic": [{"id": "proj-child", "created_at": "2026-09-18T00:00:00Z"}]
    }
    result = _run(
        "proj-epic",
        tmp_path,
        show_fixtures=show_fixtures,
        list_fixtures=list_fixtures,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "false"


def test_audited_with_zero_children_is_false(tmp_path: Path) -> None:
    """No parent-child children at all -- `any([]; ...)` is vacuously false,
    so this must not read stale."""
    show_fixtures = {
        "proj-epic": _epic(labels=["epic-audited"], audited_at="2026-09-15T00:00:00Z")
    }
    result = _run(
        "proj-epic",
        tmp_path,
        show_fixtures=show_fixtures,
        list_fixtures={"proj-epic": []},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "false"


def test_child_created_same_second_as_stamp_is_true(tmp_path: Path) -> None:
    """/epic-audit stamps audited_at BEFORE filing gaps, at second precision:
    a gap filed in the same second must still re-arm (>=, not >)."""
    show_fixtures = {
        "proj-epic": _epic(labels=["epic-audited"], audited_at="2026-09-15T00:00:00Z")
    }
    list_fixtures = {
        "proj-epic": [{"id": "proj-gap", "created_at": "2026-09-15T00:00:00Z"}]
    }
    result = _run(
        "proj-epic", tmp_path, show_fixtures=show_fixtures, list_fixtures=list_fixtures
    )
    assert result.stdout.strip() == "true"
