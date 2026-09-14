"""Regression pin for proj-fm7t: `.claude/skills/sweep/SKILL.md` Section 7 must
consume `$SWEEP_TMP/new_ids` -- persisted by Section 5 BEFORE Section 6 rewrites
the digest body -- rather than re-deriving the delta from the digest description,
which by the time Section 7 runs is the body Section 6 just wrote.

THE BUG. Section 5 computes `NEW_IDS = comm -13 LAST_IDS CURRENT_IDS`, where
`LAST_IDS` comes from parsing `SWEEP-ITEM` lines out of the digest issue's
description. Section 6 then rewrites that description wholesale from
`$CURRENT`. Section 7 -- a separate Bash tool invocation, so nothing from
Section 5 survives (proj-sfnb/proj-x495, this file's own governing rule) --
used to re-derive `NEW_IDS` by re-running the SAME computation. But by then the
digest body it reads is the one Section 6 just wrote, so `LAST_IDS ==
CURRENT_IDS` by construction and `NEW_IDS` was ALWAYS EMPTY -- no push, no
`## NEW HUMAN-DECISION ITEMS` report block, ever, on exactly the passes where
the queue actually changed.

WHY A TEXTUAL PIN IS NOT ENOUGH (proj-fm7t acceptance #5). A test that merely
greps Section 7's fenced block for the substring `$SWEEP_TMP/new_ids` would
have passed on the OLD, broken text too -- Section 7 always wrote that path
(as an output, from its own broken re-derivation). The property that actually
matters is an ORDERING one: Section 7's `$SWEEP_TMP/push_ids` must reflect the
delta AS SEEN BEFORE Section 6's rewrite, not after. The only way
to pin that mechanically is to run the real extracted blocks in the order an
agent actually executes them -- one fresh subprocess per fenced block, exactly
as `docs/agents-workflow.md`'s cross-block-shell-state rule describes -- with a
fake `bd` whose `bd show <digest-id>` response is swapped mid-test to model
Section 6 having already run. `test_section_7_reddens_if_restored_to_reading_the_digest_body`
below proves the pin actually catches the regression: it reimplements the OLD,
broken Section 7 shape and asserts it fails these same assertions.

Same fake-`bd`-on-PATH-plus-real-script pattern as tests/test_sweep_digest_id.py
and tests/test_epic_children_closed.py. Block extraction is
tests/conftest.py::bash_fence_blocks (proj-kjei) -- the same parser
tests/test_land_conflicts_state.py uses for the same reason: preserve fenced
block BOUNDARIES, since whether `$NEW_IDS` is read from a file or re-derived
from a bash variable/digest read is a per-block question.
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


def _skill_blocks() -> list[str]:
    return SWEEP_SKILL_BLOCKS


def _only_block_with(*needles: str, what: str) -> str:
    """The single fenced block containing every needle -- asserts exactly one,
    same discipline as tests/test_land_conflicts_state.py's helper of the same
    name (a `next(..., None)` here would silently pin the wrong one of two
    near-identical-looking blocks, e.g. Section 5 and Section 6 both derive
    `$DIGEST_ID` via the same script call).

    Thin wrapper over the shared tests/conftest.py::only_block_with, onto which
    proj-pm37 unified this file's own former private copy."""
    return only_block_with(_skill_blocks(), *needles, what=what)


def _section_5_block() -> str:
    """Section 5 -- computes $NEW_IDS against the (pre-rewrite) digest body and
    must persist it to $SWEEP_TMP/new_ids before Section 6 ever runs."""
    return _only_block_with(
        "comm -13",
        'LAST_BODY=$(bd show "$DIGEST_ID"',
        what="Section 5's delta computation",
    )


def _section_7_block() -> str:
    """Section 7 -- notify/split. Must read this pass's new ids back from
    $SWEEP_TMP/new_ids, never from a fresh `bd show` of the digest.

    Deliberately located by its awk split alone -- text this fix does NOT
    touch -- and never by the fixed-up read itself. A locator keyed on the fix
    would match zero blocks the moment Section 7 regressed, so every test below
    would die inside `_only_block_with` with "this test's assumption about
    SKILL.md's structure has drifted; re-check by hand before adjusting the
    locator" -- an invitation to loosen the locator on precisely the regression
    this file exists to catch. Keyed this way, a regression instead reddens
    `test_section_7_no_longer_reads_the_digest_at_all` on its own terms.
    """
    return _only_block_with(
        "awk -F'\\t' -v ann=",
        what="Section 7's notify/split",
    )


def test_section_5_persists_new_ids_before_any_digest_rewrite() -> None:
    """Static shape check: Section 5's block must write $SWEEP_TMP/new_ids
    (the only mechanism that survives into Section 7 at all, per proj-sfnb),
    and it must do so using $NEW_IDS -- the value computed in THIS block,
    against the digest body as it stood before Section 6 touches it."""
    block = _section_5_block()
    # One assertion, on the whole persist line: it must write $SWEEP_TMP/new_ids
    # (the only mechanism that survives into Section 7 at all) sourced from
    # $NEW_IDS as computed in THIS block via `comm`, not from some other value.
    # `printf '%s'` (no trailing newline) is the same idiom Sections 1/2/2a/2b/3
    # persist with, and is what makes an empty $NEW_IDS a ZERO-BYTE file rather
    # than one blank line -- load-bearing for the missing-vs-empty distinction
    # Section 7 gates on (acceptance #3), so it is pinned here verbatim.
    assert 'printf \'%s\' "$NEW_IDS" > "$SWEEP_TMP/new_ids"' in block, (
        "Section 5 no longer persists $NEW_IDS to $SWEEP_TMP/new_ids with the "
        "repo-standard `printf '%s'` idiom -- Section 7 (a separate Bash "
        "invocation, proj-sfnb) has no other way to see this pass's pre-rewrite "
        "delta, and a trailing newline would make an empty delta indistinguishable "
        "from a one-item one (proj-fm7t)"
    )


def test_section_7_no_longer_reads_the_digest_at_all() -> None:
    """proj-fm7t acceptance #2: Section 7 must not read the digest description
    for delta purposes any more -- no `scripts/sweep-digest-id.sh` re-derivation
    and no `bd show` round-trip in this block at all.

    The positive half is asserted here too, since `_section_7_block`'s locator
    deliberately no longer keys on it (see that helper's docstring).
    """
    block = _section_7_block()
    assert '"$SWEEP_TMP/new_ids"' in block, (
        "Section 7 no longer references $SWEEP_TMP/new_ids at all -- it must "
        "consume the delta Section 5 persisted before Section 6's rewrite "
        "(proj-fm7t)"
    )
    assert "sweep-digest-id.sh" not in block, (
        "Section 7 still re-derives $DIGEST_ID -- it should have no reason to "
        "any more, since it no longer reads the digest body (proj-fm7t)"
    )
    assert "bd show" not in block, (
        "Section 7 still round-trips through `bd show` -- that is the exact "
        "digest-description read this ticket removes (proj-fm7t acceptance #2)"
    )
    assert "comm -13" not in block, (
        "Section 7 still recomputes the delta via `comm -13` -- that recomputation "
        "against the POST-Section-6 digest body is the defect itself (proj-fm7t)"
    )


# --- Execution: run the real extracted blocks, in document order, exactly as
# an agent would (one fresh Bash tool invocation per fenced block) -----------


def _fake_bd(bin_dir: Path, description: str) -> Path:
    """A PATH dir holding a fake `bd` serving:
      - `bd list --label sweep-digest --all --limit 0 --json` (used by
        scripts/sweep-digest-id.sh) -> exactly one digest row, id "proj-dig1".
      - `bd show proj-dig1 --json` -> the current contents of the returned
        body file, which starts as `description`.

    Returns that body file so a test can rewrite it BETWEEN two subprocess
    invocations to model Section 6's rewrite: the fake `bd` re-reads it on
    every call, so a `body_file.write_text(...)` takes effect on the NEXT
    `bd show`, exactly as Section 6's real digest rewrite would.
    """
    fake_bd = bin_dir / "bd"
    body_file = bin_dir / "digest_body.txt"
    body_file.write_text(description)
    fake_bd.write_text(
        textwrap.dedent(f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            if [ "$1" = "list" ]; then
              echo '[{{"id": "proj-dig1", "title": "Human-decision digest"}}]'
            elif [ "$1" = "show" ]; then
              body=$(cat {body_file})
              jq -n --arg d "$body" '[{{"description": $d}}]'
            else
              echo "unsupported: $*" >&2
              exit 1
            fi
        """)
    )
    fake_bd.chmod(0o755)
    return body_file


def test_full_pass_new_item_reaches_push_ids_after_the_digest_rewrite(
    sweep_tmp: Path, tmp_path: Path
) -> None:
    """proj-fm7t acceptance #4 (verified by execution) + #5 (the ordering pin).

    Models a genuinely new queue item end to end: Section 5 computes and
    persists $NEW_IDS against the OLD (pre-rewrite) digest body; then, exactly
    as Section 6 would, the digest body is overwritten so its SWEEP-ITEM lines
    now match $CURRENT (the state Section 7 would actually see, `bd show`d
    fresh, on a real pass); THEN Section 7 runs as its own subprocess.

    If Section 7 ever regresses to re-deriving the delta from the digest
    (the bug this ticket fixes), it would see LAST_IDS == CURRENT_IDS at that
    point -- by construction, since the digest body was just rewritten to
    match -- and compute an empty NEW_IDS, landing an empty $SWEEP_TMP/push_ids
    despite proj-l7mj genuinely being new. That is exactly what this test
    would then observe, and exactly why the assertions below fail against that
    shape (see the sabotage test below).
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    old_body = (
        "## Land/build escalations + human decisions (1)\n"
        "SWEEP-ITEM proj-yuwt land-escalated Some prior item\n"
        "## Epics ready for a human close-decision (0)\n"
        "(none)"
    )
    body_file = _fake_bd(bin_dir, old_body)

    # $SWEEP_TMP/current -- what §3 would have built: the prior item PLUS one
    # genuinely new escalation, proj-l7mj.
    current_rows = (
        "proj-yuwt\tland-escalated\tSome prior item\topen\n"
        "proj-l7mj\tland-escalated\tA new escalation\topen"
    )
    (sweep_tmp / "current").write_text(current_rows)

    # --- Section 5, its own subprocess ---
    r5 = run_block(_section_5_block(), sweep_tmp, bin_dir, cwd=_CHECKOUT_ROOT)
    assert r5.returncode == 0, f"Section 5 failed: {r5.stderr}"

    new_ids_after_5 = (sweep_tmp / "new_ids").read_text()
    assert new_ids_after_5.strip() == "proj-l7mj", (
        f"Section 5 should compute NEW_IDS={{proj-l7mj}} against the OLD digest "
        f"body, got {new_ids_after_5!r}"
    )

    # --- Section 6 (modeled, not executed -- its own write is not under test
    # here): rewrite the digest body to match $CURRENT, exactly as the real
    # Section 6 would on this pass. ---
    new_body = (
        "## Land/build escalations + human decisions (2)\n"
        "SWEEP-ITEM proj-yuwt land-escalated Some prior item\n"
        "SWEEP-ITEM proj-l7mj land-escalated A new escalation\n"
        "## Epics ready for a human close-decision (0)\n"
        "(none)"
    )
    body_file.write_text(new_body)

    # --- Section 7, its OWN, separate subprocess -- nothing from Section 5's
    # shell survives (proj-sfnb); only $SWEEP_TMP/new_ids and
    # $SWEEP_TMP/current on disk do. ---
    r7 = run_block(_section_7_block(), sweep_tmp, bin_dir, cwd=_CHECKOUT_ROOT)
    assert r7.returncode == 0, f"Section 7 failed: {r7.stderr}"

    push_ids = (sweep_tmp / "push_ids").read_text().strip()
    new_annotated = (sweep_tmp / "new_annotated").read_text().strip()
    assert push_ids == "proj-l7mj", (
        "Section 7's $SWEEP_TMP/push_ids must carry the genuinely-new item "
        f"even though the digest body was already rewritten to match $CURRENT "
        f"by the time this block ran; got {push_ids!r}. If this is empty, "
        "Section 7 has regressed to re-deriving the delta from the "
        "(post-rewrite) digest body instead of reading $SWEEP_TMP/new_ids "
        "(proj-fm7t)."
    )
    assert new_annotated == "proj-l7mj\tland-escalated\tA new escalation", (
        f"unexpected $SWEEP_TMP/new_annotated content: {new_annotated!r}"
    )


def test_no_change_pass_is_a_true_no_op(sweep_tmp: Path, tmp_path: Path) -> None:
    """proj-fm7t acceptance #4, the other half: a pass where nothing changed
    must yield empty $NEW_IDS from Section 5, and Section 7 must then produce
    empty push_ids/new_annotated -- no phantom notification."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    body = (
        "## Land/build escalations + human decisions (1)\n"
        "SWEEP-ITEM proj-yuwt land-escalated Some prior item\n"
        "## Epics ready for a human close-decision (0)\n"
        "(none)"
    )
    _fake_bd(bin_dir, body)

    (sweep_tmp / "current").write_text(
        "proj-yuwt\tland-escalated\tSome prior item\topen"
    )

    r5 = run_block(_section_5_block(), sweep_tmp, bin_dir, cwd=_CHECKOUT_ROOT)
    assert r5.returncode == 0, f"Section 5 failed: {r5.stderr}"
    assert (sweep_tmp / "new_ids").read_text().strip() == ""

    r7 = run_block(_section_7_block(), sweep_tmp, bin_dir, cwd=_CHECKOUT_ROOT)
    assert r7.returncode == 0, f"Section 7 failed: {r7.stderr}"
    assert (sweep_tmp / "push_ids").read_text().strip() == ""
    assert (sweep_tmp / "new_annotated").read_text().strip() == ""


def test_section_7_missing_new_ids_file_is_a_loud_gate_failure(
    sweep_tmp: Path, tmp_path: Path
) -> None:
    """proj-fm7t acceptance #3: a MISSING $SWEEP_TMP/new_ids (Section 5 never
    ran this pass) must be a loud "GATE COULD NOT RUN", not a phantom-empty
    read that silently behaves like a legitimate no-op."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _fake_bd(bin_dir, "(none)")

    (sweep_tmp / "current").write_text(
        "proj-yuwt\tland-escalated\tSome prior item\topen"
    )
    # Deliberately do NOT write $SWEEP_TMP/new_ids.

    r7 = run_block(_section_7_block(), sweep_tmp, bin_dir, cwd=_CHECKOUT_ROOT)
    assert r7.returncode == 1
    assert "GATE COULD NOT RUN" in r7.stderr
    assert "new_ids" in r7.stderr
    assert not (sweep_tmp / "push_ids").exists(), (
        "a missing $SWEEP_TMP/new_ids must abort before push_ids is ever "
        "written, empty or otherwise"
    )


def test_section_7_present_but_empty_new_ids_is_not_conflated_with_missing(
    sweep_tmp: Path, tmp_path: Path
) -> None:
    """The other half of acceptance #3: an EMPTY (but present) new_ids file is
    a legitimate "nothing new this pass" and must run cleanly to completion,
    not trip the same refusal a missing file does."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _fake_bd(bin_dir, "(none)")

    (sweep_tmp / "current").write_text(
        "proj-yuwt\tland-escalated\tSome prior item\topen"
    )
    (sweep_tmp / "new_ids").write_text("")

    r7 = run_block(_section_7_block(), sweep_tmp, bin_dir, cwd=_CHECKOUT_ROOT)
    assert r7.returncode == 0, (
        f"Section 7 must not fail on a legitimately-empty new_ids: {r7.stderr}"
    )
    assert (sweep_tmp / "push_ids").read_text().strip() == ""
    assert (sweep_tmp / "new_annotated").read_text().strip() == ""


def test_section_7_reddens_if_restored_to_reading_the_digest_body(
    sweep_tmp: Path, tmp_path: Path
) -> None:
    """SABOTAGE PROOF (proj-fm7t acceptance #5): reimplements the OLD, broken
    Section 7 shape -- re-deriving $NEW_IDS from a fresh `bd show` of the
    digest, exactly as it read before this fix -- and asserts it FAILS the
    same "genuinely new item survives" property the fixed block satisfies in
    `test_full_pass_new_item_reaches_push_ids_after_the_digest_rewrite` above.
    This is the mechanical proof that the test above is not satisfied by any
    Section 7 that merely mentions `$SWEEP_TMP/new_ids` in passing -- it must
    actually be the source of `$NEW_IDS`.
    """
    old_broken_section_7 = r"""
SWEEP_TMP="${TMPDIR:-/tmp}/harness-sweep-state"
DIGEST_ID="$(scripts/sweep-digest-id.sh)" || exit 1
CURRENT="$(cat "$SWEEP_TMP/current")" || exit 1

LAST_BODY=$(bd show "$DIGEST_ID" --json | jq -r '.[0].description')
LAST_IDS=$(printf '%s\n' "$LAST_BODY" | grep '^SWEEP-ITEM' | awk '{print $2}' | sort -u)
CURRENT_IDS=$(printf '%s\n' "$CURRENT" | awk -F'\t' '{print $1}' | sort -u)
NEW_IDS=$(comm -13 <(printf '%s\n' "$LAST_IDS") <(printf '%s\n' "$CURRENT_IDS"))

: > "$SWEEP_TMP/new_annotated"
: > "$SWEEP_TMP/push_ids"
awk -F'\t' -v ann="$SWEEP_TMP/new_annotated" -v push="$SWEEP_TMP/push_ids" '
  NR == FNR      { new[$1] = 1; next }
  !($1 in new)   { next }
  { row = $1 "\t" $2 "\t" $3 }
  $4 == "deferred" { print row " (deferred)" > ann; next }
                   { print row > ann; print $1 > push }
' <(printf '%s\n' "$NEW_IDS") "$SWEEP_TMP/current"
"""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    old_body = (
        "## Land/build escalations + human decisions (1)\n"
        "SWEEP-ITEM proj-yuwt land-escalated Some prior item"
    )
    body_file = _fake_bd(bin_dir, old_body)

    current_rows = (
        "proj-yuwt\tland-escalated\tSome prior item\topen\n"
        "proj-l7mj\tland-escalated\tA new escalation\topen"
    )
    (sweep_tmp / "current").write_text(current_rows)

    r5 = run_block(_section_5_block(), sweep_tmp, bin_dir, cwd=_CHECKOUT_ROOT)
    assert r5.returncode == 0, f"Section 5 failed: {r5.stderr}"
    assert (sweep_tmp / "new_ids").read_text().strip() == "proj-l7mj"

    # Model Section 6's rewrite -- body now matches $CURRENT.
    new_body = (
        "## Land/build escalations + human decisions (2)\n"
        "SWEEP-ITEM proj-yuwt land-escalated Some prior item\n"
        "SWEEP-ITEM proj-l7mj land-escalated A new escalation"
    )
    body_file.write_text(new_body)

    r7 = run_block(old_broken_section_7, sweep_tmp, bin_dir, cwd=_CHECKOUT_ROOT)
    assert r7.returncode == 0, f"reimplemented old Section 7 errored: {r7.stderr}"

    # THIS is the regression this whole file exists to catch: the OLD shape
    # computes an empty push_ids here, because by the time it runs the digest
    # body already matches $CURRENT.
    push_ids = (sweep_tmp / "push_ids").read_text().strip()
    assert push_ids == "", (
        "expected the OLD, broken Section 7 shape to (wrongly) produce an "
        f"EMPTY push_ids here -- got {push_ids!r}. If this assertion fails, "
        "the reimplemented old shape above no longer reproduces proj-fm7t's "
        "bug, and this sabotage proof needs re-deriving against the real "
        "pre-fix git history."
    )
