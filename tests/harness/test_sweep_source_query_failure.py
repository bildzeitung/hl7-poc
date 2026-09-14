"""Regression pin for proj-5qbi: `.claude/skills/sweep/SKILL.md` Sections 1 and 2
must DETECT a failed `bd`/`jq` source query -- and must not report failure when
the query succeeded.

THE BUG BEING PINNED. Section 6 rewrites the durable digest issue WHOLESALE from
`$CURRENT`. Measured on bd 1.1.0, a failing `bd list` writes its diagnostic to
stderr and ZERO bytes to stdout, so in `VAR=$(bd ... | jq ...)` without
`set -o pipefail`, jq reads no input, emits nothing, and exits 0 -- the
assignment reports success, `$CURRENT` comes out empty, and the rewrite deletes
every real human-decision item from the record. Section 2 additionally used
`while read ... < <(bd ... | jq ...)`, whose exit status is `read`'s, never the
pipeline's, so no `pipefail` setting could have surfaced it there.

BOTH DIRECTIONS ARE PINNED, and the healthy-path one is not decoration -- see
`test_successful_pass_writes_no_marker_and_exits_zero` for the specific inverted
signal it exists to catch, which a static grep for `pipefail` would pass.

Same fake-`bd`-on-PATH-plus-real-fenced-block pattern as
tests/test_sweep_new_ids_ordering.py; block extraction is
tests/conftest.py::bash_fence_blocks (proj-kjei).
"""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import pytest
from conftest import _CHECKOUT_ROOT, SWEEP_SKILL_BLOCKS, only_block_with, run_block

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the skill's fenced blocks shell out to jq"
)

MARKER = "source_query_failed"


def _skill_blocks() -> list[str]:
    return SWEEP_SKILL_BLOCKS


def _only_block_with(*needles: str, what: str) -> str:
    """Thin wrapper over the shared tests/conftest.py::only_block_with, same
    discipline and same call shape as tests/test_sweep_new_ids_ordering.py's
    and tests/test_land_conflicts_state.py's helpers of the same name -- each
    binds its own ``_skill_blocks()`` (proj-pm37/proj-n6q0)."""
    return only_block_with(_skill_blocks(), *needles, what=what)


def _section_1_block() -> str:
    """Section 1 -- the land-escalated + human queue reads.

    Located by text this ticket does NOT change (the two query shapes), never by
    the marker/`pipefail` lines under test: a locator keyed on the fix would
    match zero blocks the moment the fix regressed, and every test here would
    die inside `_only_block_with` complaining about drift instead of reddening
    on its own terms.
    """
    return _only_block_with(
        "--label land-escalated",
        "bd human list",
        what="Section 1's queue collection",
    )


def _section_2_block() -> str:
    """Section 2 -- epics whose children have all closed."""
    return _only_block_with(
        "--label epic-audited",
        "epic-children-closed.sh",
        what="Section 2's closable-epic collection",
    )


def _section_5_block() -> str:
    """Section 5 -- reads the marker to suppress Section 6's digest rewrite."""
    return _only_block_with(
        "comm -13",
        'LAST_BODY=$(bd show "$DIGEST_ID"',
        what="Section 5's delta computation",
    )


def _section_8_block() -> str:
    """Section 8 -- reads the marker to report the failure."""
    return _only_block_with(
        "bd-dolt-push.sh",
        "STRANDED_STATE",
        what="Section 8's publish/report",
    )


def _fake_bd(bin_dir: Path, *, failing: str | None) -> None:
    """A PATH dir holding a fake `bd`. `failing` names the first argv word whose
    invocation should model a real bd failure -- diagnostic on stderr, ZERO bytes
    on stdout, non-zero exit (the measured bd 1.1.0 behaviour this whole ticket
    turns on); every other subcommand returns a well-formed empty-ish result.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    fail_branch = (
        textwrap.dedent(f"""\
            if [ "$1" = "{failing}" ]; then
              echo "Error: simulated bd failure" >&2
              exit 1
            fi
        """)
        if failing is not None
        else ""
    )
    fake_bd = bin_dir / "bd"
    fake_bd.write_text(
        "#!/usr/bin/env bash\nset -uo pipefail\n"
        + fail_branch
        + textwrap.dedent("""\
            if [ "$1" = "human" ]; then
              # A genuinely empty `bd human list` serializes as literal null,
              # not [] -- the case the `(. // [])` guard exists for.
              echo 'null'
            else
              echo '[]'
            fi
        """)
    )
    fake_bd.chmod(0o755)


@pytest.mark.parametrize(
    ("block_fn", "failing"),
    [
        (_section_1_block, "list"),
        (_section_1_block, "human"),
        (_section_2_block, "list"),
    ],
    ids=["s1-escalated", "s1-human", "s2-epics"],
)
def test_failed_source_query_writes_the_marker(
    block_fn, failing: str, sweep_tmp: Path, tmp_path: Path
) -> None:
    """A failing `bd` -- zero bytes on stdout, so `jq` itself exits 0 -- must
    still be detected and must leave $SWEEP_TMP/source_query_failed behind, the
    marker Section 5 reads to suppress Section 6's wholesale digest rewrite."""
    bin_dir = tmp_path / "fakebin"
    _fake_bd(bin_dir, failing=failing)
    run_block(block_fn(), sweep_tmp, bin_dir, cwd=_CHECKOUT_ROOT)
    assert (sweep_tmp / MARKER).exists(), (
        "a failed bd source query did not write $SWEEP_TMP/source_query_failed -- "
        "Section 5 will fall through and Section 6 will rewrite the digest "
        "wholesale from an empty $CURRENT, deleting every real human-decision "
        "item from the durable record (proj-5qbi)"
    )


@pytest.mark.parametrize(
    "block_fn", [_section_1_block, _section_2_block], ids=["s1", "s2"]
)
def test_successful_pass_writes_no_marker_and_exits_zero(
    block_fn, sweep_tmp: Path, tmp_path: Path
) -> None:
    """The other direction, and the one a `pipefail` grep cannot see: on a
    healthy pass the block must NOT write the marker, and must exit 0."""
    bin_dir = tmp_path / "fakebin"
    _fake_bd(bin_dir, failing=None)
    proc = run_block(block_fn(), sweep_tmp, bin_dir, cwd=_CHECKOUT_ROOT)
    assert not (sweep_tmp / MARKER).exists(), (
        "a SUCCESSFUL pass wrote $SWEEP_TMP/source_query_failed -- Section 5 "
        "would then skip the digest rewrite on every pass, silently freezing "
        "the durable record (proj-5qbi)"
    )
    assert proc.returncode == 0, (
        "a successful pass exited non-zero "
        f"(rc={proc.returncode}, stderr={proc.stderr!r}) -- most likely the "
        "marker write moved to a TRAILING conditional (`[ $FAILED = 1 ] && touch "
        "...`), whose short-circuit on the healthy path becomes the block's own "
        "exit status and reports every good pass as a failed source query "
        "(proj-5qbi)"
    )


@pytest.mark.parametrize(
    "block_fn", [_section_5_block, _section_8_block], ids=["s5", "s8"]
)
def test_the_marker_name_is_shared_by_its_readers(block_fn) -> None:
    """The marker filename IS the contract between Sections 1/2 (which write it)
    and Sections 5/8 (which read it), and nothing else connects them -- they are
    separate Bash invocations (§0). Rename or relocate it on one side only and
    the gate is silently disconnected: Section 5 stops suppressing the rewrite
    and Section 8 stops reporting, with no error anywhere. That is the exact
    silent-disconnection class this ticket exists to close, so both readers are
    pinned to the same literal the writers use.
    """
    assert MARKER in block_fn(), (
        f"this block no longer references the {MARKER!r} marker that Sections "
        "1/2 write -- the failed-source-query gate is disconnected (proj-5qbi)"
    )
