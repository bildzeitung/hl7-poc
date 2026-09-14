"""Pins proj-rfon's fix: $CONFLICTS must survive from its producer to the
"Needs rebase -- kick back" consumer in `.claude/skills/land/SKILL.md`.

An agent executing this skill runs each fenced ```bash block as its own,
separate Bash tool invocation -- shell variables do not survive between them
(the same defect class proj-sfnb closed for $MSG/$ACCEPTED/$LANDED). Before
this fix, $CONFLICTS was captured in Section 2b's `merge-precheck.sh` call and
in each of Section 3's two `land-merge-one.sh` merge loops, but the "Needs
rebase -- kick back" block that interpolates it into a `bd --append-notes` is
a later, separate block -- so $CONFLICTS read there was always empty, and the
kick-back note silently got a blank line where the conflicting paths belong.

The fix persists $CONFLICTS to a file under `$STATE_DIR/conflicts/<id>`
(`.git/land-state/conflicts/<id>`) at each producer site, reusing the
$STATE_DIR mechanism proj-sfnb already established for the accepted/landed/msg
files, and has the kick-back block read it back from disk -- refusing loudly
(never with a blank paths section) if that file is missing or empty.

Pinned against the SHIPPED SKILL.md, not a reimplementation (the
tests/test_land_lock.py precedent) -- the bug lived in a markdown fence no
gate parses, so only reading the actual file catches a regression.

proj-wjw4 extended this file to the OTHER half of the same mechanism: WHERE
the per-pass `rm -rf "$STATE_DIR"` sits. It used to run in Section 3a, three
sections after the pass began and after 2b had already written its conflicts
record, so 2b was ordered IN PROSE to kick back before 3a ran or lose the
file. The wipe is hoisted to Section 1, ahead of every writer, which makes
that ordering structural instead of remembered -- and the pins below are what
keep it that way, since nothing else in this repo parses this markdown.

proj-youi generalized this file's charter accordingly: it now owns DOCUMENT-ORDER
invariants of `.claude/skills/land/SKILL.md` that no other gate parses, not only
the $CONFLICTS/$STATE_DIR ones the paragraphs above describe. The pin added there
(Section 3's re-gate precedes Section 4's `origin/main` push) is about neither
$CONFLICTS nor $STATE_DIR; it lives here because this is where the block-boundary
scaffolding and the cross-block-ordering precedent already are.
"""

from __future__ import annotations

from conftest import LAND_SKILL_BLOCKS, only_block_with

#: Module-level and kept HERE, directly under the imports, so an edit that
#: removes a test cannot take them with it -- which has now happened twice.
_REGATE = "nox -s tests"
"""The re-gate needle: the session that actually gates CONTENT.

Not `nox -s lock_currency`, which matches the same block but whose exit 2 the
skill explicitly treats as NOT a red gate, and which is the chain's newest and
most volatile member. Pinning on it would go red when someone drops it for the
reason the doc already contemplates, and stay green if `nox -s tests` were
removed -- both directions wrong.
"""

_PUSH = "git push origin main"
"""The push needle: Section 4's write of the default branch, which must never be
hoisted above Section 3's re-gate."""


def _skill_blocks() -> list[str]:
    """Each ```bash fence as its own string, in document order -- what an
    agent actually EXECUTES, one Bash tool invocation per block.

    Unlike a single concatenation of every fenced block (the
    tests/test_land_lock.py precedent), this preserves block BOUNDARIES,
    which is the point here: whether $CONFLICTS is read from a file or from a
    bash variable set earlier is a per-block question. Reads the shared,
    session-cached :data:`conftest.LAND_SKILL_BLOCKS` (proj-pxwn) rather than
    re-reading and re-parsing land/SKILL.md on every call -- this helper used
    to be a thin wrapper over tests/conftest.py::bash_fence_blocks, onto which
    proj-ovgs unified this file's own former private copy (byte-identical in
    behaviour on every consumed file -- verified, so this file's pins policed
    exactly the same block set before and after). The parser's known blind
    spots are documented next to it, not restated here.
    """
    return LAND_SKILL_BLOCKS


def _only_block_with(*needles: str, what: str) -> str:
    """The single fenced block containing every needle.

    Asserts there is EXACTLY one. `next(..., None)` would silently pin the
    first of several near-identical blocks, which in this file is a live
    hazard: Section 3's two merge loops are deliberately the same shape, so a
    locator that stops at the first match would pin one loop and let the other
    regress unseen -- the precise failure the Section-3 test exists to catch.

    Thin wrapper over the shared tests/conftest.py::only_block_with, onto which
    proj-pm37 unified this file's own former private copy.
    """
    return only_block_with(_skill_blocks(), *needles, what=what)


def _kick_back_block() -> str:
    return _only_block_with(
        "--add-label needs-rebase",
        "bd update",
        what="the needs-rebase kick-back",
    )


def test_state_dir_is_wiped_once_in_section_1_ahead_of_every_writer() -> None:
    """proj-wjw4: the per-pass `rm -rf "$STATE_DIR"` lives in Section 1's
    setup fence -- the first block of the pass -- and nowhere else.

    Below any writer it destroys that writer's state mid-pass: the observed
    victim was 2b's conflicts record, which the kick-back block reads back
    from disk (the tests above), and which 3a's wipe deleted whenever the
    kick-back was deferred past it. Hoisting the wipe is what removed that
    ordering constraint; prose alone would let a future editor put it back.

    `_only_block_with`'s exactly-one assertion is deliberately also sensitive
    to a COMMENT quoting the literal wipe -- that is how origin/main's shape
    scored two matches (2b carried a comment naming 3a's wipe). A comment
    restating where the wipe lives is the same remembered-not-structural
    pattern this ticket deleted, so re-flagging it is the intent, not a
    false positive.
    """
    site = _only_block_with('rm -rf "$STATE_DIR"', what="the per-pass $STATE_DIR wipe")
    assert "git checkout -f main" in site, (
        "the $STATE_DIR wipe is no longer in Section 1's setup fence (the only "
        "block that runs `git checkout -f main`). Anywhere later and a block "
        "that writes under $STATE_DIR before it -- 2b's conflicts record, 3a's "
        "accepted/msg files -- loses that state to it (proj-wjw4)"
    )


def test_section_1_block_still_ends_on_the_pass_start_reset() -> None:
    """The wipe must not become Section 1's LAST command (proj-wjw4).

    No fenced block in this file runs under `set -e` (the governing rule at
    the top: not even `set -e` carries between blocks), so a block's exit
    status is its last command's and nothing else. `rm -rf` reports success
    even on a path that does not exist -- measured, a failed `git reset
    --hard origin/main` goes from rc 128 to a block that exits 0 -- which
    would let the pass spend N `land-review` dispatches, a full re-gate and a
    `git push origin main` on top of un-reset residue. Same hazard 2b's own
    `if [ "$rc" = 1 ]` comment reasons about, from the other direction.
    """
    site = _only_block_with(
        "git reset --hard origin/main",
        "git checkout -f main",
        what="Section 1's pass-start block",
    )
    last = [
        ln for ln in site.splitlines() if ln.strip() and not ln.strip().startswith("#")
    ][-1]
    assert last.startswith("git reset --hard origin/main"), (
        "Section 1's setup block no longer ends on `git reset --hard "
        f"origin/main` -- its last executed line is {last.strip()!r}. That "
        "command's exit status is the only machine-readable signal the block "
        "gives; ending on anything that cannot fail hides a failed reset "
        "(proj-wjw4)"
    )


def test_2b_precheck_persists_conflicts_to_the_state_dir() -> None:
    """Section 2b's merge-precheck.sh call is the COMMON producer path -- it
    pre-dates proj-sfnb, runs on every branch every pass, and the ticket is
    explicit that a fix covering only Section 3 misses the frequent case."""
    site = _only_block_with(
        'merge-precheck.sh origin/main "origin/land/<id>"',
        what="Section 2b's merge-precheck.sh call",
    )
    assert 'CONFLICTS_DIR="$STATE_DIR/conflicts"' in site, (
        "2b no longer derives CONFLICTS_DIR -- proj-rfon's fix regressed"
    )
    assert 'printf \'%s\\n\' "$CONFLICTS" > "$CONFLICTS_DIR/<id>"' in site, (
        "2b's rc=1 branch no longer persists $CONFLICTS to disk -- the kick-back "
        "block (a separate Bash invocation) would see an empty variable again "
        "(proj-rfon)"
    )
    assert 'if [ "$rc" = 1 ]; then' in site, (
        "2b's persist is no longer guarded by an `if`. It must NOT be written as "
        '`[ "$rc" = 1 ] && printf ...`: as this block\'s LAST command that AND-list '
        "makes the whole Bash invocation exit 1 on the COMMON clean path (rc=0) and "
        "exit 0 on a real conflict -- inverting the block's exit status, which is the "
        "only signal it gives the agent at all (it prints nothing on any path). "
        "Verified empirically, with and without `set -e` (proj-rfon technical review)."
    )


def _merge_script_calls() -> list[str]:
    """The two fenced call sites that hand /land's merge work to a script.

    Neither merge loop is fenced any more: the first pass is
    scripts/land-merge-batch.sh and the isolation replay is scripts/land-replay.sh,
    each covered directly by its own test module. What the skill still owns -- and
    what can still drift silently -- is the WIRING: the state paths it hands them.
    """
    blocks = [
        b
        for b in _skill_blocks()
        if "land-merge-batch.sh" in b or "land-replay.sh" in b
    ]
    assert len(blocks) == 2, (
        f"expected exactly 2 fenced blocks calling the merge scripts (first pass + "
        f"isolation replay), found {len(blocks)} -- re-check by hand before adjusting"
    )
    return blocks


def test_both_merge_script_calls_are_given_the_state_paths_they_need() -> None:
    """A dropped flag is silent in the direction that matters: a missing REQUIRED
    path makes the script fault loudly, but --graph is OPTIONAL, and losing it
    stops a conflicting base from taking its dependents with it -- Section 3a's
    invariant gone without a word."""
    for call in _merge_script_calls():
        for flag in (
            "--accepted",
            "--landed",
            "--msg-dir",
            "--conflicts-dir",
            "--graph",
        ):
            assert flag in call, f"a merge script call no longer passes {flag}"


def test_the_replay_call_passes_its_resume_state() -> None:
    """Without --state the replay cannot persist progress, so every deadline yield
    restarts from the top and the pass never converges."""
    replay = [b for b in _merge_script_calls() if "land-replay.sh" in b]
    assert len(replay) == 1
    assert "--state" in replay[0], (
        "the replay call lost --state; it can no longer resume"
    )
    assert "--base-ref" in replay[0], (
        "the replay call lost --base-ref; its reset target is implicit"
    )


def test_kick_back_block_reads_conflicts_from_disk_not_a_bare_variable() -> None:
    """The exact regression proj-rfon fixed: the 'Needs rebase -- kick back'
    block interpolated a bare $CONFLICTS into a bd --append-notes without ever
    setting it IN THAT SAME BLOCK. It is a separate Bash invocation from every
    producer above, so a bare $CONFLICTS there is always empty.

    proj-dc4n retargeted the read itself onto scripts/land-state-load.sh, but
    the invariant this test pins is unchanged: the read must still be FROM
    $STATE_DIR/conflicts/<id> on disk, in this same block, before the value is
    interpolated."""
    site = _kick_back_block()
    assert (
        'CONFLICTS=$(scripts/land-state-load.sh "$STATE_DIR/conflicts/<id>"' in site
    ), (
        "the kick-back block no longer reads $CONFLICTS back from "
        "$STATE_DIR/conflicts/<id> via scripts/land-state-load.sh -- it must not "
        "rely on a bash variable set in an earlier, separate Bash invocation "
        "(proj-rfon)"
    )

    read_pos = site.index(
        'CONFLICTS=$(scripts/land-state-load.sh "$STATE_DIR/conflicts/<id>"'
    )
    update_pos = site.index("bd update <id> --remove-label ready-for-land")
    assert read_pos < update_pos, (
        "the kick-back block reads $CONFLICTS from disk AFTER the bd update call "
        "-- too late to be interpolated into the --append-notes text"
    )


def test_kick_back_block_refuses_loudly_on_missing_or_empty_conflicts() -> None:
    """Acceptance criterion (proj-rfon): an empty/missing conflicts record
    must be LOUD, never a kick-back note with a blank paths section.

    proj-dc4n moved the loudness itself into scripts/land-state-load.sh
    (its own "STATE LOAD FAILED" diagnostic to stderr) -- what this block must
    still do is pass --require-nonempty (so an EMPTY record is refused, not
    just a missing one) and exit before the bd update call on failure."""
    site = _kick_back_block()
    assert "--require-nonempty" in site, (
        "the kick-back block's land-state-load.sh call dropped --require-nonempty "
        "-- an empty (but present) conflicts record would silently be treated as "
        "success, reaching the --append-notes text with a blank paths section"
    )

    load_pos = site.index(
        'CONFLICTS=$(scripts/land-state-load.sh "$STATE_DIR/conflicts/<id>"'
    )
    update_pos = site.index("bd update <id> --remove-label ready-for-land")
    exit_pos = site.index("|| exit 1", load_pos)
    assert load_pos < exit_pos < update_pos, (
        "the land-state-load.sh call's failure is not wired to exit BEFORE the "
        "bd update call -- a missing/empty conflicts record could still produce "
        "a kick-back note with a blank paths section"
    )


_SECTION_4_PUSH_MARK = "Heartbeat here so every per-ticket close"
"""What distinguishes Section 4's push fence from the other `_PUSH` block.

Content, not document position: the fence that pushes Section 3's merge output
is the one that goes on to heartbeat and drain the per-ticket close work. A
positional tiebreak (`min(push_indices)`) would keep passing while silently
pinning the WRONG fence if SKILL.md were ever reorganized so "Stop and report"
sat above Section 4 -- the same first-of-several-near-identical-blocks hazard
`_only_block_with` exists to refuse.
"""


def _regate_and_push_indices(blocks: list[str]) -> tuple[list[int], int]:
    """Document-order positions of the re-gate blocks and Section 4's push
    block -- the latter identified by CONTENT (`_SECTION_4_PUSH_MARK`).

    There is a SECOND `git push origin main` fence, in "Stop and report"'s own
    MISTAKES.md entry point -- it fires only on a pass that never reaches
    Section 3/4 at all (nothing merged, so nothing to gate), and pushes only a
    MISTAKES.md doc commit, never Section 3's merge output. It is deliberately
    excluded from THIS pin, which is about Section 4's merge-output push
    specifically -- the one this test's whole premise (`origin/main` only
    advances to already-gated content) is about.

    Also polices the structural assumption itself, so a SKILL.md reshape that
    adds or drops one of these blocks fails loudly here rather than silently
    changing what the ordering pin means.
    """
    regate_indices = [i for i, b in enumerate(blocks) if _REGATE in b]
    push_indices = [i for i, b in enumerate(blocks) if _PUSH in b]
    assert len(regate_indices) == 1, (
        f"expected exactly 1 fenced block running `{_REGATE}` (Section 3's combined "
        f"Green re-gate + the Red isolation-replay re-gate), found "
        f"{len(regate_indices)} -- this test's assumption about SKILL.md's "
        "structure has drifted; re-check by hand before adjusting the count"
    )
    assert len(push_indices) == 2, (
        f"expected exactly 2 fenced blocks running `{_PUSH}` (Section 4's "
        f"merge-output push + Stop and report's no-accepted-set MISTAKES.md "
        f"push), found {len(push_indices)} -- this test's assumption about "
        "SKILL.md's structure has drifted; re-check by hand before adjusting "
        "the count"
    )
    section_4 = [i for i in push_indices if _SECTION_4_PUSH_MARK in blocks[i]]
    assert len(section_4) == 1, (
        f"expected exactly 1 of the {len(push_indices)} `{_PUSH}` blocks to "
        f"carry `{_SECTION_4_PUSH_MARK}` (Section 4's merge-output push), "
        f"found {len(section_4)} -- this test's assumption about SKILL.md's "
        "structure has drifted; re-check by hand before adjusting the needle"
    )
    return regate_indices, section_4[0]


def _regate_precedes_push(blocks: list[str]) -> bool:
    """Whether EVERY re-gate block precedes the push block.

    Shared by the pin below and its sabotage twin ON PURPOSE: a twin that
    re-derives the comparison instead of calling it proves only that the
    re-derived expression is order-sensitive, which says nothing about the
    assertion actually shipped. Routing both through this one function is what
    makes the sabotage bind -- weaken the comparison here and the twin goes red.
    """
    regate_indices, push_index = _regate_and_push_indices(blocks)
    return max(regate_indices) < push_index


def test_section_3_regate_precedes_section_4_push_origin_trunk() -> None:
    """proj-youi: pins mechanically what proj-rlz8 left to prose -- Section 4's
    `origin/main` push sits after Section 3's re-gate, in document order.

    A DOCUMENT-ORDER pin, not an execution-order guarantee: an agent that skips
    a section could still push un-gated content even with this test green (the
    same insufficiency that bounds `test_land_skill_guard_coverage.py`'s
    `_unguarded_mutations` sweep, whose "precedes" check is likewise document
    order and, narrower still, only within one fence). It earns its keep
    anyway because the threat Section 4's prose names is exactly a document edit
    ("if a future edit ever reorders push and gate"), and document order is
    precisely what such an edit changes.

    Owned here as a property of `/land`'s own Section 3 -> Section 4
    sequencing, not as a service to `scripts/recycled-worktree-guard.sh`: the
    premise ("`origin/main` only ever advances to already-gated content") is
    relied on by every launch worktree, which branches from `origin/main`
    (`.claude/settings.json`'s `worktree.baseRef: "fresh"`) -- so a reorder's
    blast radius is every fresh agent worktree, not just that guard's reset
    path.
    """
    blocks = _skill_blocks()
    regate_indices, push_index = _regate_and_push_indices(blocks)
    assert _regate_precedes_push(blocks), (
        f"Section 3's re-gate (`{_REGATE}`) no longer precedes Section 4's "
        f"`{_PUSH}` in document order -- re-gate blocks at {regate_indices}, "
        f"push block at {push_index}. A reorder here would let un-gated "
        "content reach `origin/main`, and every fresh agent worktree branches "
        "from it (proj-youi, proj-rlz8)"
    )


def test_section_3_regate_precedes_push_is_sabotage_proven() -> None:
    """Proves the pin above is non-vacuous by running the SAME
    `_regate_precedes_push()` it asserts on, against a minimally reordered copy
    of the real parsed block list.

    The sabotage is the exact edit the pin exists to catch: swap the push block
    with the LAST re-gate block, i.e. hoist the push above Section 3's re-gate.
    Deliberately not "move the push to index 0" -- that makes the comparison
    `max(regate) < 0`, false for ANY input, so such a twin passes even against a
    document that is already broken, and proves nothing.
    """
    blocks = _skill_blocks()
    regate_indices, push_index = _regate_and_push_indices(blocks)

    # The copy is MANDATORY, not defensive housekeeping (proj-pxwn):
    # `_skill_blocks()` now returns the session-shared `conftest.LAND_SKILL_BLOCKS`
    # itself, so reordering in place would corrupt it for every test that runs
    # after this one -- an order-dependent failure elsewhere in the suite.
    sabotaged = list(blocks)
    last_regate = max(regate_indices)
    sabotaged[push_index], sabotaged[last_regate] = (
        sabotaged[last_regate],
        sabotaged[push_index],
    )
    assert not _regate_precedes_push(sabotaged), (
        "sabotage (hoisting the push above Section 3's re-gate) did not make "
        "`_regate_precedes_push()` return False -- the real pin is vacuous"
    )


def _commands(block: str) -> list[str]:
    """A fenced block's command lines, comments and indentation stripped.

    The two MISTAKES.md filing sites annotate the same commands with
    site-specific trailing comments ("same reason as Section 4's copy" vs the
    original rationale); the pin below is about the COMMANDS staying identical,
    not the prose around them.
    """
    out = []
    for raw in block.splitlines():
        line = raw.split("#", 1)[0].strip() if not raw.strip().startswith("#") else ""
        if line:
            out.append(line)
    return out


def test_both_mistakes_md_filing_sites_run_identical_commands() -> None:
    """`/land`'s MISTAKES.md filing block exists at two entry points (Section
    4's, for a pass that lands something; "Stop and report"'s, for a pass with
    no accepted set at all). SKILL.md is prose an agent reads, and each entry
    point has to stand alone, so the duplication is deliberate -- but nothing
    otherwise stops the two copies from silently diverging when the dedup rule
    or the commit shape next changes.

    Same remedy the `land-merge-batch.sh`/`land-replay.sh` idioms got: leave
    the duplication, pin it byte-identical (commands only -- see `_commands`).
    The append/commit fence is what carries the drift risk; the trailing
    `git push origin main` is genuinely unique to the Stop-and-report copy and
    deliberately outside this pin's scope.
    """
    blocks = _skill_blocks()
    filing = [_commands(b) for b in blocks if "git add MISTAKES.md" in b]
    assert len(filing) == 2, (
        f"expected exactly 2 MISTAKES.md filing fences (Section 4's + Stop and "
        f"report's), found {len(filing)} -- re-check by hand before adjusting "
        "the count"
    )
    assert filing[0] == filing[1], (
        "the two MISTAKES.md filing fences have drifted apart:\n"
        f"  Section 4:       {filing[0]}\n"
        f"  Stop and report: {filing[1]}\n"
        "keep them command-identical, or collapse them to one block"
    )


def test_both_mistakes_md_filing_sites_share_one_dedup_grep() -> None:
    """The dedup grep is the load-bearing half of the "grep for the incident,
    not just exact wording" rule -- if one copy keeps a single fixed phrase
    while the other alternates candidates, one entry point double-files.
    Pinned separately from the fence above because the grep lives in its own
    fence at both sites.
    """
    blocks = _skill_blocks()
    greps = {
        line
        for b in blocks
        for line in _commands(b)
        if line.startswith("grep") and "MISTAKES.md" in line
    }
    assert len(greps) == 1, (
        f"expected both MISTAKES.md dedup greps to be the same command, found "
        f"{len(greps)} distinct: {sorted(greps)}"
    )
