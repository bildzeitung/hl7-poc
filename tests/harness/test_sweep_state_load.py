"""Pins proj-3oik: `.claude/skills/sweep/SKILL.md`'s `$SWEEP_TMP` load cluster
adopts `scripts/land-state-load.sh` (proj-dc4n) for its "default" policy sites
(missing fatal, empty OK), and the two `2>/dev/null` sites (deferred/stranded
in the report/publish section) are deliberately left alone -- their
missing-file behaviour is a non-fatal third state, which neither of
`land-state-load.sh`'s two policies expresses.

Same discipline as `tests/test_land_conflicts_state.py`'s exact call-site
pins: this asserts the literal call each retrofitted site makes, never a
character-proximity/distance check, so a future edit that quietly reverts a
site to a bare `cat` (silently flipping its policy back to a hand-rolled
spelling -- the exact hazard proj-dc4n's own rationale names) turns this file
red.

Pinned against the SHIPPED SKILL.md, not a reimplementation -- the bug class
this guards against lives in a markdown fence no other gate parses.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from conftest import SWEEP_SKILL_BLOCKS, only_block_with


def _section_3_block() -> str:
    """§3 -- builds $CURRENT from $ESCALATED/$HUMAN/$CLOSABLE."""
    return only_block_with(
        SWEEP_SKILL_BLOCKS,
        "CURRENT=$(printf",
        "sort -u -t$'\\t' -k1,1",
        what="§3's current-queue build",
    )


def _section_6_prep_block() -> str:
    """§6's precondition/prep block -- re-derives DIGEST_ID and $CURRENT."""
    return only_block_with(
        SWEEP_SKILL_BLOCKS,
        "sweep-digest-id.sh",
        "LAST_BODY=$(bd show",
        what="§6's digest-rewrite precondition/prep",
    )


def _section_7_block() -> str:
    """§7 -- splits new ids into report-it vs push-it."""
    return only_block_with(
        SWEEP_SKILL_BLOCKS,
        "new_annotated",
        "push_ids",
        "awk -F'\\t'",
        what="§7's new-ids split",
    )


def _section_8_block() -> str:
    """§8 -- deferred/stranded 3-valued state + publish."""
    return only_block_with(
        SWEEP_SKILL_BLOCKS,
        "bd-dolt-push.sh",
        "STRANDED_STATE",
        what="§8's publish/report",
    )


def _only_line_for(block: str, needle: str) -> str:
    """The single call-site line in ``block`` mentioning ``needle``, isolated so
    a --require-nonempty check on one $SWEEP_TMP path cannot accidentally match
    a DIFFERENT path's flag a few lines away."""
    lines = [ln for ln in block.splitlines() if needle in ln]
    assert len(lines) == 1, (
        f"expected exactly 1 line mentioning {needle!r}, found {len(lines)} -- "
        "this test's locator has drifted; re-check by hand"
    )
    return lines[0]


# ---------------------------------------------------------------------------
# Retrofitted sites: exact call to scripts/land-state-load.sh, default policy
# (no --require-nonempty -- these five sites all had "missing fatal, empty
# OK" before the retrofit, never an emptiness check).
# ---------------------------------------------------------------------------

RETROFITTED = [
    pytest.param(_section_3_block, "ESCALATED", "escalated", id="s3-escalated"),
    pytest.param(_section_3_block, "HUMAN", "human", id="s3-human"),
    pytest.param(_section_3_block, "CLOSABLE", "closable", id="s3-closable"),
    pytest.param(_section_6_prep_block, "CURRENT", "current", id="s6prep-current"),
    pytest.param(_section_7_block, "CURRENT", "current", id="s7-current"),
]


@pytest.mark.parametrize(("block", "var", "name"), RETROFITTED)
def test_site_uses_the_shared_loader(
    block: Callable[[], str], var: str, name: str
) -> None:
    call = f'{var}="$(scripts/land-state-load.sh "$SWEEP_TMP/{name}" -- \\'
    assert call in block(), (
        f"the ${var} read of $SWEEP_TMP/{name} no longer calls "
        "scripts/land-state-load.sh -- a reverted bare `cat` would silently "
        "drop the loud diagnostic (proj-3oik)"
    )


@pytest.mark.parametrize(("block", "var", "name"), RETROFITTED)
def test_site_keeps_the_default_policy(
    block: Callable[[], str], var: str, name: str
) -> None:
    line = _only_line_for(block(), f'land-state-load.sh "$SWEEP_TMP/{name}"')
    assert "--require-nonempty" not in line, (
        f"the ${var} read of $SWEEP_TMP/{name} gained --require-nonempty -- it "
        "never had an emptiness check before the retrofit; that would turn a "
        "legitimately empty queue into a hard abort (proj-3oik)"
    )


def test_section_7_new_ids_existence_check_is_left_alone() -> None:
    """§7's `[ -f "$SWEEP_TMP/new_ids" ]` is an EXISTENCE check, not a content
    load -- it never reads the file's bytes, only tests presence, and an
    empty-but-present file must run on through (proj-fm7t). Out of scope for
    the land-state-load.sh retrofit (not one of the ticket's listed sites);
    this test just pins that it is untouched."""
    assert '[ -f "$SWEEP_TMP/new_ids" ] || {' in _section_7_block(), (
        "§7's $SWEEP_TMP/new_ids existence check changed shape -- re-verify by "
        "hand whether it should now be in scope for the loader retrofit"
    )


# ---------------------------------------------------------------------------
# Deliberately-not-retrofitted sites: §8's deferred/stranded reads. Pinned to
# stay on a bare `cat ... 2>/dev/null`, with a short note at each site saying
# why -- catches a future editor "helpfully" wiring these onto the shared
# loader's fatal-on-missing policy, which would flip a currently non-fatal,
# expected state (§2a/§2b never ran, or hasn't run yet) into a hard abort of
# §8's publish step.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("var", "name"),
    [
        ("DEFERRED", "deferred"),
        ("STRANDED", "stranded"),
        # The blocked-human subtraction added a THIRD site on this exact shape. Its file is
        # written by §1, not by a §2c block of its own, but the reason it must stay
        # un-retrofitted is identical: a missing file has to remain a non-fatal `missing`,
        # not abort §8's publish step.
        ("BLOCKED_HUMAN", "blocked_human"),
    ],
)
def test_section_8_load_is_deliberately_not_retrofitted(var: str, name: str) -> None:
    site = _section_8_block()

    # The exact shape IS the pin: a bare `cat ... 2>/dev/null` inside the `if`
    # entails "not routed through scripts/land-state-load.sh", so no separate
    # absence assertion is needed. Both of that script's policies exit 1 on a
    # missing file; §8 needs missing to stay a non-fatal {var}_STATE=missing,
    # or the publish step below aborts whenever §2a/§2b hasn't run this pass.
    assert f'if {var}="$(cat "$SWEEP_TMP/{name}" 2>/dev/null)"; then' in site, (
        f"§8's ${var} read changed shape -- if it now calls "
        "scripts/land-state-load.sh, re-verify that a missing file still "
        f"yields {var}_STATE=missing rather than aborting §8's publish step"
    )


@pytest.mark.parametrize("name", ["deferred", "stranded", "blocked_human"])
def test_section_8_load_carries_its_own_note(name: str) -> None:
    """Acceptance criterion (proj-3oik) is per-SITE: each un-retrofitted read
    carries its own note, not one shared note covering both. Pinned as an exact
    line, like every other pin here -- deliberately NOT a "is there a proj-3oik
    mention within N lines" proximity check, which is the anti-pattern
    tests/test_land_conflicts_state.py exists to avoid."""
    note = (
        f"# NOT retrofitted onto scripts/land-state-load.sh -- a "
        f"missing $SWEEP_TMP/{name} is a"
    )
    assert note in _section_8_block(), (
        f"§8's {name} read lost its own note explaining why it is deliberately "
        "not retrofitted onto scripts/land-state-load.sh (proj-3oik)"
    )
