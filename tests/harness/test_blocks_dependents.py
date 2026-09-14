"""Tests for scripts/blocks-dependents.sh (proj-verb).

WHY the script's `--include-dependents` flag is required, and why THIS site
(unlike the three sibling checks proj-v4rk extracted) fails silently UNSAFE
without it: see the header comment of scripts/blocks-dependents.sh. That
rationale is deliberately NOT restated here -- it lives next to the code it
constrains, so it cannot drift out of sync with a second copy.

What this file adds on top of that: the regression gate. These tests exist
so a future edit that drops the flag fails LOUDLY here, rather than silently
re-pointing nothing at the one site where "nothing" is the unsafe answer.

`test_old_dependents_derivation_without_flag_never_fires` is a pinned
demonstration of that failure mode: it embeds the pre-extraction jq snippet
(minus `--include-dependents`, i.e. what a regression would look like)
directly and asserts it silently yields nothing against a `bd show`-shaped
fixture that HAS blocks-dependents in the DB but omits `.dependents` from
the JSON -- exactly matching real `bd show --json` without the flag,
verified live against bd 1.1.0 (see proj-v4rk). It is not a regression
guard (it doesn't touch the actual script) -- the guards are the fake-`bd`
tests below, which exercise `scripts/blocks-dependents.sh` directly and
fail if the script's own `--include-dependents` flag is ever dropped.
Same fake-`bd`-on-PATH pattern as tests/test_epic_completion_check.py and
tests/test_epic_children_closed.py.

RATIFIED DEFERRAL (proj-863q's technical review, ratified durably here by
proj-ea5b -- the residual ticket proj-863q's own scope note deliberately
left this cluster to): the `_fake_bd` bodies in these three modules are NOT
unified into one shared fake-`bd` builder, despite looking alike at a
glance. Each embeds a genuinely different `bd` subcommand contract inside
its shell script -- this module's fake `bd` serves only `bd show
--include-dependents` and actively REJECTS a call missing that flag (the
whole point of the regression guard above); test_epic_children_closed.py's
serves only `bd list --parent`; test_epic_completion_check.py's dispatches
both `show` and `list`. A shared builder would have to be parameterized
over all three validation rules, and over the differing fixture-dict
arities each takes -- added complexity, not a clean extraction like `_git`
(tests/_gitrepo.py). Do not
re-litigate this without a concrete shape that beats that trade.
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
SCRIPT = REPO_ROOT / "scripts" / "blocks-dependents.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the script shells out to jq"
)


def _fake_bd(tmp_path: Path, show_fixtures: dict[str, list[dict]]) -> Path:
    """A PATH dir holding a fake `bd` that serves `show_fixtures[<id>]` for
    `bd show <id> --json --include-dependents`. Fails loudly if the script
    calls `bd show` WITHOUT `--include-dependents` -- that is exactly the
    regression this harness exists to catch."""
    show_path = tmp_path / "show_fixtures.json"
    show_path.write_text(json.dumps(show_fixtures))

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()

    fake_bd = bin_dir / "bd"
    fake_bd.write_text(
        textwrap.dedent(f"""\
            #!/usr/bin/env bash
            # Fake `bd show <id> --json --include-dependents` for testing
            # blocks-dependents.sh. Deliberately rejects any invocation that
            # drops --include-dependents, so a script regression that removes
            # the flag fails LOUDLY here instead of silently returning [].
            set -euo pipefail
            [ "$1" = "show" ] || {{ echo "unsupported: $*" >&2; exit 1; }}
            id="$2"
            shift 2
            case " $* " in
              *" --include-dependents "*) ;;
              *) echo "fake bd: refusing bd show without --include-dependents" >&2
                 exit 1 ;;
            esac
            jq -c --arg id "$id" \\
              '.[$id] // error("no show fixture for \\($id)")' "{show_path}"
            """)
    )
    fake_bd.chmod(0o755)
    return bin_dir


def _run(
    id_: str, tmp_path: Path, *, show_fixtures: dict[str, list[dict]]
) -> subprocess.CompletedProcess:
    bin_dir = _fake_bd(tmp_path, show_fixtures)
    return subprocess.run(
        ["bash", str(SCRIPT), id_],
        env=fake_bin_env(bin_dir),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _dependent(id_: str, dependency_type: str) -> dict:
    return {"id": id_, "dependency_type": dependency_type}


def test_blocks_dependents_are_listed(tmp_path: Path) -> None:
    show_fixtures = {
        "proj-orig": [
            {
                "id": "proj-orig",
                "dependents": [
                    _dependent("proj-followup-a", "blocks"),
                    _dependent("proj-followup-b", "blocks"),
                ],
            }
        ]
    }
    result = _run("proj-orig", tmp_path, show_fixtures=show_fixtures)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["proj-followup-a", "proj-followup-b"]


def test_non_blocks_dependents_are_excluded(tmp_path: Path) -> None:
    """`parent-child` and `discovered-from` dependents must NOT be re-pointed
    -- only `blocks` edges create the premature-unblock hazard."""
    show_fixtures = {
        "proj-orig": [
            {
                "id": "proj-orig",
                "dependents": [
                    _dependent("proj-child", "parent-child"),
                    _dependent("proj-spinoff", "discovered-from"),
                    _dependent("proj-related", "related"),
                    _dependent("proj-blocker-of-orig", "blocks"),
                ],
            }
        ]
    }
    result = _run("proj-orig", tmp_path, show_fixtures=show_fixtures)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["proj-blocker-of-orig"]


def test_no_dependents_key_prints_nothing(tmp_path: Path) -> None:
    """The `dependents` key can be entirely absent (e.g. a fixture that never
    populated it) -- the script must not error, just print nothing."""
    show_fixtures = {"proj-solo": [{"id": "proj-solo"}]}
    result = _run("proj-solo", tmp_path, show_fixtures=show_fixtures)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_empty_dependents_array_prints_nothing(tmp_path: Path) -> None:
    show_fixtures = {"proj-solo": [{"id": "proj-solo", "dependents": []}]}
    result = _run("proj-solo", tmp_path, show_fixtures=show_fixtures)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_script_calls_bd_show_with_include_dependents_flag(tmp_path: Path) -> None:
    """Direct proof the script itself passes --include-dependents: the fake
    `bd` above refuses (exit 1) any `bd show` call missing that flag, so a
    regression that drops it turns this into a failure, not a silent []."""
    show_fixtures = {
        "proj-orig": [
            {"id": "proj-orig", "dependents": [_dependent("proj-dep", "blocks")]}
        ]
    }
    result = _run("proj-orig", tmp_path, show_fixtures=show_fixtures)
    assert result.returncode == 0, result.stderr
    assert "refusing bd show without --include-dependents" not in result.stderr


def test_old_dependents_derivation_without_flag_never_fires() -> None:
    """Pinned demonstration of the bug this script exists to avoid
    reintroducing: a `bd show --json` call WITHOUT `--include-dependents`
    never populates `.dependents` (bd 1.1.0), so a derivation built on it
    silently yields nothing -- even though the ticket genuinely has
    blocks-dependents in the DB (modeled here only via a non-zero
    `dependent_count`, since the payload itself cannot say so; that
    inability is exactly the bug).

    NOT a regression guard for scripts/blocks-dependents.sh -- it embeds the
    flag-less derivation inline and passes regardless of what the script
    does (verified: still passes with the script deleted). The regression
    guards are the fake-`bd` tests above, which fail if the real script's
    `--include-dependents` flag is ever dropped.
    """
    orig = {
        "id": "proj-orig",
        # deliberately no "dependents" key -- matches real `bd show --json`
        # without --include-dependents, despite a non-zero dependent_count.
        "dependent_count": 2,
    }
    old_snippet_without_flag = """
        .[0].dependents[]? | select(.dependency_type=="blocks") | .id
    """
    result = subprocess.run(
        ["jq", "-r", old_snippet_without_flag],
        input=json.dumps([orig]),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "", (
        "the flag-less derivation printed dependents -- this fixture no "
        "longer reproduces the bug this script exists to avoid reintroducing"
    )
