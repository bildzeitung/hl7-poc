"""Regression pin for proj-42fh: `.claude/skills/sweep/SKILL.md` Section 2b's
`started_at` 24h age discriminator (added by proj-3k6x) had no test executing
the real fenced block. This pins that arithmetic mechanically.

What it pins: a ticket claimed just under 24h ago is NOT listed; one claimed
just over 24h ago IS listed; a `null` `started_at` and an ABSENT `started_at`
key are both listed (no age evidence to exclude them on); and a healthy query
exits 0 writing real content, not the `SWEEP-QUERY-ERROR` sentinel.

NON-VACUITY, verified by execution against a mutated `.claude/skills/sweep/SKILL.md`
(re-run either mutation by hand if this test's coverage is ever doubted):
- flip `<` to `>` in the `select()` clause -> the under-24h row is listed and
  the over-24h row is not, reddening both boundary assertions;
- drop the `.started_at == null or` branch -> jq's `fromdateiso8601` raises on
  the null row, `pipefail` trips, and the block writes `SWEEP-QUERY-ERROR`,
  reddening the sentinel assertion.
Deliberately NOT shipped as mutation tests of their own: they would assert only
that a mutant behaves differently (which the assertions below already imply),
while coupling permanently to the exact jq source text.

Same fake-`bd`-on-PATH-plus-real-fenced-block pattern as
tests/test_sweep_source_query_failure.py and tests/test_sweep_new_ids_ordering.py;
block extraction is tests/conftest.py::bash_fence_blocks (proj-kjei), execution
is tests/conftest.py::run_block (proj-n6q0).
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import _CHECKOUT_ROOT, SWEEP_SKILL_BLOCKS, only_block_with, run_block

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the skill's fenced blocks shell out to jq"
)

# IDs chosen to be distinguishable in assertions -- not real bd ids.
JUST_UNDER_ID = "proj-under23h"  # started_at 23h ago -- must be EXCLUDED (live build)
JUST_OVER_ID = "proj-over25h"  # started_at 25h ago -- must be LISTED (stranded)
NULL_ID = "proj-nullstart"  # started_at: null -- must be LISTED (no age evidence)
ABSENT_ID = "proj-noattr"  # no started_at key at all -- must be LISTED


def _iso(dt: datetime) -> str:
    """Plain ISO8601-Z, no fractional seconds -- the shape `bd` 1.1.0 emits and
    the shape jq's `fromdateiso8601` parses (proj-3k6x's review verified this
    against live bd)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _skill_blocks() -> list[str]:
    return SWEEP_SKILL_BLOCKS


def _section_2b_block() -> str:
    """Section 2b -- the stranded in_progress collection with the age filter.

    Located by the query shape rather than by the section heading, so moving or
    renaming §2b does not break the locator; `only_block_with` asserts exactly
    one hit, so a structural change that makes this ambiguous fails loudly
    rather than silently pinning the wrong block.
    """
    return only_block_with(
        _skill_blocks(),
        "STRANDED=$(bd list --status in_progress",
        "started_at | fromdateiso8601",
        what="Section 2b's stranded-ticket collection",
    )


def _fake_bd(bin_dir: Path, rows: list[dict]) -> None:
    """A PATH dir holding a fake `bd` that answers `bd list --status
    in_progress ...` with a fixed JSON array, and everything else with `[]`.

    The heredoc terminator must stay at column 0 -- the payload is interpolated
    unindented and `<<'JSON'` (not `<<-`) matches only an unindented terminator.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake_bd = bin_dir / "bd"
    fake_bd.write_text(
        "#!/usr/bin/env bash\n"
        "set -uo pipefail\n"
        'if [ "$1" = "list" ]; then\n'
        "  cat <<'JSON'\n"
        f"{json.dumps(rows)}\n"
        "JSON\n"
        "else\n"
        "  echo '[]'\n"
        "fi\n"
    )
    fake_bd.chmod(0o755)


def _rows() -> list[dict]:
    now = datetime.now(UTC)
    return [
        {
            "id": JUST_UNDER_ID,
            "title": "claimed 23h ago",
            "started_at": _iso(now - timedelta(hours=23)),
        },
        {
            "id": JUST_OVER_ID,
            "title": "claimed 25h ago",
            "started_at": _iso(now - timedelta(hours=25)),
        },
        {
            "id": NULL_ID,
            "title": "null started_at",
            "started_at": None,
        },
        {
            "id": ABSENT_ID,
            "title": "absent started_at key",
        },
    ]


def test_boundary_and_null_absent_rows(sweep_tmp: Path, tmp_path: Path) -> None:
    bin_dir = tmp_path / "fakebin"
    _fake_bd(bin_dir, _rows())
    proc = run_block(_section_2b_block(), sweep_tmp, bin_dir, cwd=_CHECKOUT_ROOT)

    assert proc.returncode == 0, (
        f"a healthy §2b query exited non-zero (rc={proc.returncode}, "
        f"stderr={proc.stderr!r})"
    )

    stranded_path = sweep_tmp / "stranded"
    assert stranded_path.exists(), "§2b did not persist $SWEEP_TMP/stranded"
    content = stranded_path.read_text(encoding="utf-8")

    assert content != "SWEEP-QUERY-ERROR", (
        f"a healthy §2b query wrote the SWEEP-QUERY-ERROR sentinel instead of "
        f"real content (stderr={proc.stderr!r})"
    )

    assert JUST_UNDER_ID not in content, (
        f"a ticket claimed 23h ago (under the 24h threshold) was listed as "
        f"stranded -- it is still a live build, not a stranding (proj-3k6x). "
        f"content={content!r}"
    )
    assert JUST_OVER_ID in content, (
        f"a ticket claimed 25h ago (over the 24h threshold) was NOT listed as "
        f"stranded. content={content!r}"
    )
    assert NULL_ID in content, (
        f"a ticket with a null started_at (no age evidence) was NOT listed as "
        f"stranded -- it should be treated as stranded defensively, per §2b's "
        f"own comment. content={content!r}"
    )
    assert ABSENT_ID in content, (
        f"a ticket with no started_at key at all was NOT listed as stranded -- "
        f"same defensive branch as the null case. content={content!r}"
    )
