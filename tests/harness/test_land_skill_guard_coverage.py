"""Guard-coverage gates for `.claude/skills/land/SKILL.md`'s use of
`scripts/assert-main-checkout.sh` (proj-pcee, proj-1d2y).

These tests run no script and touch no repo. They parse the SHIPPED
`.claude/skills/land/SKILL.md` and assert over its fenced ```bash blocks'
text -- because the defect this guard exists to prevent lives in a markdown
fence that no other gate parses. `scripts/assert-main-checkout.sh`'s own
runtime behaviour (real script, real git repos) is covered separately in
`tests/test_assert_main_checkout.py`; this module is the text-gate half that
split out of it (proj-2thl).
"""

from __future__ import annotations

import re

from conftest import LAND_SKILL_BASH, LAND_SKILL_BLOCKS, bash_fence_blocks

# Share proj-x495's quote-aware comment stripper rather than adding a second,
# competing implementation -- the same reuse `tests/test_bd_list_limit_gate.py`
# was told to make, for the same reason: "what does an agent actually execute"
# parsing is identical across these gates even when the assertions differ.
from test_skill_bash_state import _strip_comment

# The ```bash fence parser is `tests/conftest.py::bash_fence_blocks` and lives
# there alone; its rules and blind spots are stated next to it and deliberately
# not restated here, per the same no-second-copy rule tests/test_land_lock.py
# and tests/test_land_conflicts_state.py follow.


_GUARD = "scripts/assert-main-checkout.sh"


def test_land_skill_never_reintroduces_the_false_dash_c_idiom() -> None:
    """The exact regression this ticket fixes: `-C "$(git rev-parse
    --show-toplevel)"` gives the *appearance* of pinning a command to the
    main checkout while actually just restating cwd. Must never reappear in
    an EXECUTED fence, and the destructive `git reset --hard origin/main`
    line must never carry any `-C` either (that would be the same false
    assurance one line later)."""
    executed = LAND_SKILL_BASH

    assert "show-toplevel" not in executed, (
        "land/SKILL.md executes a `--show-toplevel`-derived -C again -- this "
        "is the false-assurance idiom proj-pcee removed; it cannot distinguish "
        "the main checkout from a worktree because it resolves relative to cwd"
    )

    for line in executed.splitlines():
        if "git reset --hard origin/main" in line:
            assert " -C " not in line and not line.strip().startswith("-C "), (
                f"the destructive reset carries a -C again: {line!r} -- this is "
                "the exact unguarded, most dangerous half of the proj-pcee defect"
            )


# ---------------------------------------------------------------------------
# Guard-coverage SWEEP (proj-1d2y; superseded the four per-fence pins in
# proj-8p3c) -- open-world: catches a NEW unguarded fence, not only the
# already-known ones. `_unguarded_mutations` owns its mechanics.
#
# Until proj-8p3c this module also pinned each already-discovered guarded fence
# individually, anchored on the commands it contained. That was precise, but
# closed-world: a genuinely NEW class of unguarded mutation -- `git clean -fdx`,
# `git rebase`, a `git checkout -f` with no accompanying reset, a bare `git
# commit -m` missing `--no-verify` -- matched none of those hand-picked
# selectors and was caught by NOTHING. That is exactly how proj-pxyt's own two
# exposures survived a full ticket cycle before a human noticed them by
# inspection.
#
# Subsumption was NOT free, and was not true as first written. Measured during
# proj-8p3c's technical review by replaying each deleted pin's sabotages against
# this sweep: the Section 1 pin also protected `bd dolt pull` and `git fetch
# origin`, and hoisting either into an unguarded fence of its own left the sweep
# GREEN while the deleted pin went RED. Both were added to `_MUTATING_CMD_RE` at
# zero allowlist cost, since the file's only occurrences of each were already
# guarded. Before deleting any future pin, diff its protected set against that
# pattern the same way.
#
# KNOWN LIMITATIONS (proj-1d2y):
#
# 1. INLINE COMMANDS ONLY -- SCRIPT-REFERENCE FOLLOWING IS DELIBERATELY OUT
#    OF SCOPE. This sweep does not open `scripts/*.sh` and classify what it
#    mutates; it only sees commands written directly in the fence. It would
#    NOT have caught proj-pxyt's exposure: that fence contained zero bare
#    mutating git commands -- its only mutation was inside a referenced
#    script. A script reference is caught by nothing here and would need the
#    same manual discovery proj-pxyt's did. Measured while writing this, and
#    re-measured for proj-1nty: of the scripts referenced from an UNGUARDED
#    fence today, the only one running a cwd-resolved mutating git command is
#    `scripts/land-merge-one.sh`, which asserts its OWN main-checkout identity
#    internally (see that script's header) rather than depending on this
#    sweep; every other one's git calls are `merge-base --is-ancestor` and
#    `merge-tree --write-tree`, both read-only. So the gap is latent, not
#    live. Re-measure rather than assume.
# 2. THE MUTATING-COMMAND REGEX IS DELIBERATELY OVER-BROAD, BY DESIGN. It
#    matches `git branch`, `git worktree`, and `git merge` as whole verbs,
#    not just the destructive subcommands (`branch -D`, `worktree remove`,
#    a bare `merge`) -- so it also flags read-only lookalikes: `git merge-
#    base` (`\b` matches against the hyphen), `git branch --merged`, `git
#    worktree list`. Narrowing the regex to exclude every read-only shape
#    would inherit the risk of excluding a genuinely dangerous one by the
#    same narrowing, so each false positive gets an allowlist entry instead.
# 3. AN ALLOWLIST ENTRY GOES STALE IN ONE DIRECTION SILENTLY, AND THAT
#    DIRECTION IS NOT COVERED. A change to an exempt line's TEXT re-flags it
#    loudly (`test_sweep_allowlist_match_is_exact_text_not_shape`), and a
#    deleted line fails `test_every_allowlist_entry_still_matches_a_real_
#    command`. A change to an exempt line's SURROUNDINGS does neither: four
#    reasons below are claims about variable PROVENANCE (`$WT`/`$BR` "read
#    from `git worktree list --porcelain`") that nothing here checks, so
#    rewriting those assignments leaves the key matching byte-for-byte with
#    its recorded reason now false.
# 4. BLOCK EXTRACTION INHERITS `tests/conftest.py::bash_fence_blocks`'s
#    DOCUMENTED BOUNDARIES -- the one failure mode an open-world sweep must
#    not have quietly. proj-p4qb answered the three that used to yield NO
#    BLOCK silently (an unterminated final fence is now flushed; four-backtick
#    and tilde fences are now scanned), so what remains is one boundary of the
#    opposite kind: a content line whose first non-blank character is `>` is
#    silently CORRUPTED by the blockquote strip, not skipped. A mutating
#    command written that way would reach this sweep in mangled form. That
#    helper owns the rule and the measurement; do not restate either here.
#
# `test_every_allowlist_entry_still_matches_a_real_command` below is a hand-written
# liveness pin, mirroring the precedent `test_skill_bash_state.py::_dead_allowlist_keys`
# set. Whether these hand-written pins should share one mechanism was decided
# (rejected extraction, in favor of shared discipline instead) in
# docs/decisions.md, search "The three hand-written liveness pins stay separate"
# (proj-7zap). Do not re-litigate here.
#
# DECISION (proj-eu04): `_dead_allowlist_entries`' live set also ignored
# `_unguarded_mutations`' BLOCK-level guard state (a key reachable only inside
# an already-guarded block, or only on the `_GUARD`-carrying line itself, read
# as live while excusing nothing). Two options were on the table:
#   (a) thread the block-level guard state into a SECOND pass tailored to
#       `_dead_allowlist_entries`, mirroring `_unguarded_mutations`' loop; or
#   (b) factor `_unguarded_mutations`' own loop into a shared primitive
#       (`_unguarded_candidates`) returning every unguarded, mutating command
#       regardless of allowlist, and define "live" as membership in that set --
#       i.e. what the sweep would flag with an emptied allowlist.
# Chose (b). Rejected (a) because it is a second, independently-maintained
# copy of the exact block-state machine `_unguarded_mutations` already owns --
# two loops that must be kept in lockstep by hand is exactly how this class of
# gap (proj-dkak, now this ticket) keeps recurring one level at a time. (b)
# makes drift structurally impossible: `_dead_allowlist_entries` and
# `_unguarded_mutations` now both read the SAME single candidate list, so
# "live" cannot silently diverge from what the sweep actually excuses again.
# ---------------------------------------------------------------------------

# Every command shape that mutates cwd's repo. Not git-only, deliberately:
# `bd dolt pull` writes cwd's OWN `.beads/` Dolt DB (each worktree carries its
# own copy), so a wrong-directory run updates the wrong database. The sweep
# asks ONE question -- does this line mutate cwd's repo -- so both classes
# belong in one pattern rather than a git list plus side checks.
#
# Do NOT re-add `scripts/land-merge-one.sh` here: it was named literally as a
# special case until proj-1nty, and now asserts its own main-checkout identity
# internally instead, so this sweep does not need to know it exists. Re-adding
# it means re-litigating that decision (docs/agents-workflow.md's main-checkout
# section).
#
# The bd WRITE side is out of scope here, not overlooked: `land/SKILL.md` only
# ever reaches it through `scripts/bd-dolt-push.sh`, which is KNOWN
# LIMITATION 1's territory (no script following), not this pattern's.
_MUTATING_CMD_RE = re.compile(
    r"\b(?:git\s+(?:add|am|apply|branch|checkout|cherry-pick|clean|commit|fetch"
    r"|merge|mv|pull|push|rebase|reset|restore|revert|rm|stash|switch|worktree)"
    r"|bd\s+dolt\s+pull)\b"
)

# Exact command text (comment-stripped, `.strip()`'d) -> why it needs no
# `scripts/assert-main-checkout.sh` guard. Keying on exact text is what makes
# an exemption reviewable data rather than prose; KNOWN LIMITATION 3 above
# records what that keying does and does not catch.
_KNOWN_LAND_SKILL_MUTATIONS: dict[str, str] = {
    # --- read-only false positives of the broad verb regex (limitation 2) ---
    # --- explicit ref/path-addressed writes: cwd-independent by construction,
    #     the same reasoning Section 4's own prose gives for these commands
    #     ("each names its own target") ---
    "git push origin main": (
        "ref-addressed (explicit remote+branch); Section 4's own text: cwd-independent"
    ),
    'git push origin --delete "land/$id"': (
        "ref-addressed delete (explicit remote+branch); cwd-independent "
        "(Section 4's per-ticket branch GC)"
    ),
    'git push origin --delete "land/<id>"': (
        "ref-addressed delete (explicit remote+branch); cwd-independent "
        "(Bounce / Escalate-exit-(b) / Escalate-exit-(c))"
    ),
    # --- path-addressed to the passive bd export, never real work ---
    "git restore --staged --worktree .beads/issues.jsonl 2>/dev/null || true": (
        "path-addressed to the passive .beads/issues.jsonl export only -- "
        "never real work (import.auto: false, proj-6ra); a wrong-directory "
        "run only restores that worktree's own copy of a regenerated artifact"
    ),
}


def _normalized_line(raw_line: str) -> str:
    """A raw fenced-block line reduced to its command text: comment-stripped
    and `.strip()`'d. Empty means the line carries no command at all.
    """
    return _strip_comment(raw_line).strip()


def _is_mutating(cmd: str) -> bool:
    """Whether a `_normalized_line` result is a mutating command -- i.e.
    whether an allowlist entry keyed on this exact text could excuse
    anything.
    """
    return bool(cmd) and bool(_MUTATING_CMD_RE.search(cmd))


def _unguarded_candidates_from_blocks(blocks: list[str]) -> list[tuple[int, str]]:
    """Every fenced ```bash block's mutating command that is not preceded, in
    its OWN block, by `scripts/assert-main-checkout.sh` -- i.e. exactly the
    set `_unguarded_mutations` would flag as a violation if `allowlist` were
    empty, paired with its block index. `_unguarded_mutations_from_blocks` and
    `_dead_allowlist_entries_from_blocks` both build on this, and this list is
    the single definition of "live" both mean: a key only excuses anything in
    the sweep if it names a command reachable at THIS point (unguarded
    position, mutating verb) -- not merely present anywhere in the file. See
    the proj-eu04 DECISION in the module comment above for why the two callers
    share one list rather than each owning a loop.

    Takes already-fence-parsed ```bash blocks (e.g. `conftest.LAND_SKILL_BLOCKS`)
    rather than raw markdown, so the real `land/SKILL.md` call sites fence-parse
    the largest doc in the repo once per session instead of once per call
    (proj-lmnx). The `_unguarded_mutations`/`_dead_allowlist_entries` wrappers
    below fence-parse markdown themselves for the synthetic-fixture call sites,
    which construct their own markdown string and have no pre-parsed blocks list
    to pass instead.

    Block boundaries are load-bearing, not tidiness: per land/SKILL.md's
    governing rule (proj-sfnb) each fence is its own Bash invocation, so a
    guard only protects commands in the SAME fence. `guarded` therefore resets
    per block, and reading it in document order is the whole of the "precedes"
    check -- no positional arithmetic, which would go ambiguous the moment two
    lines in one block carry identical text. See the module comment above for
    the regex/allowlist design and its known limitations.
    """
    candidates: list[tuple[int, str]] = []
    for block_index, block in enumerate(blocks):
        guarded = False
        for raw_line in block.splitlines():
            cmd = _normalized_line(raw_line)
            if not cmd:
                continue
            if _GUARD in cmd:
                guarded = True
                continue
            if not _is_mutating(cmd):
                continue
            if guarded:
                continue
            candidates.append((block_index, cmd))
    return candidates


def _unguarded_mutations_from_blocks(
    blocks: list[str], *, allowlist: dict[str, str]
) -> list[str]:
    """Same result as `_unguarded_mutations`, but takes already-fence-parsed
    ```bash blocks -- see `_unguarded_candidates_from_blocks` for why.
    """
    return [
        f"block {block_index}: {cmd!r} is a mutating command with "
        f"no preceding {_GUARD} in its own fenced block, and is not "
        "in the allowlist -- either guard it or record a reasoned "
        "allowlist entry"
        for block_index, cmd in _unguarded_candidates_from_blocks(blocks)
        if cmd not in allowlist
    ]


def _unguarded_mutations(markdown: str, *, allowlist: dict[str, str]) -> list[str]:
    """Every unguarded candidate (see `_unguarded_candidates_from_blocks`) not
    excused by `allowlist`. Empty means full coverage.

    Thin wrapper over `_unguarded_mutations_from_blocks` that fence-parses
    `markdown` itself -- see `_unguarded_candidates_from_blocks`'s docstring for
    why this wrapper exists.
    """
    return _unguarded_mutations_from_blocks(
        bash_fence_blocks(markdown), allowlist=allowlist
    )


def test_land_skill_guard_covers_every_known_mutating_fence() -> None:
    """The sweep, run against the REAL land/SKILL.md with the real allowlist.
    Zero violations means every mutating command in the file is either
    guarded or a recorded, reasoned exemption -- across every fence in the
    file, not just a hand-picked few.

    This also subsumes the Section 1 existence pin proj-pcee added and
    proj-0mkv deleted ("does SKILL.md call the guard at all"): measured,
    dropping Section 1's guard line leaves that pin GREEN -- the string still
    appears in three other fences -- while this sweep reports 4 named
    violations. Do not re-add a per-section duplicate.
    """
    violations = _unguarded_mutations_from_blocks(
        LAND_SKILL_BLOCKS,
        allowlist=_KNOWN_LAND_SKILL_MUTATIONS,
    )
    assert violations == [], "\n".join(violations)


def _dead_allowlist_entries_from_blocks(
    blocks: list[str], *, allowlist: dict[str, str]
) -> list[str]:
    """Allowlist keys in `allowlist` that would excuse nothing in the real sweep
    over `blocks` -- i.e. that do not name any
    `_unguarded_candidates_from_blocks(blocks)` entry, which is the one
    definition of "live" this module has (see that helper, and the
    proj-dkak/proj-eu04 history in the module comment above for the two looser
    definitions it replaced). A key returned here is dead: it currently excuses
    nothing, but stays in the allowlist regardless, ready to silently re-excuse
    a brand-new command that happens to share its exact text. Argument order
    deliberately matches `_unguarded_mutations_from_blocks` above -- same two
    inputs, same shape.

    The parameterized-helper-plus-sabotage shape here is proj-e49j's, whose
    `test_skill_bash_state.py::_dead_allowlist_keys` modeled its own pin on THIS
    module's (proj-1d2y) and then shipped the non-vacuity half this module lacked;
    proj-7zap brings that half back the other way.

    `allowlist` and the blocks are parameters, not read from the module globals
    directly, so `test_every_allowlist_entry_is_provably_checked_by_sabotage` below
    can exercise this exact primitive -- the same one the real pin calls, reached
    through the `_dead_allowlist_entries` wrapper -- against a synthetic fixture,
    without mutating the real `land/SKILL.md` on disk.

    Takes already-fence-parsed ```bash blocks rather than raw markdown -- see
    `_unguarded_candidates_from_blocks`'s docstring for why.
    """
    live = {cmd for _, cmd in _unguarded_candidates_from_blocks(blocks)}
    return sorted(set(allowlist) - live)


def _dead_allowlist_entries(markdown: str, *, allowlist: dict[str, str]) -> list[str]:
    """Dead allowlist keys over `markdown` -- see
    `_dead_allowlist_entries_from_blocks` above for what "dead" means and why
    the inputs are parameters rather than module globals.

    Thin wrapper that fence-parses `markdown` itself, for the
    synthetic-markdown fixture call sites below -- see
    `_unguarded_candidates_from_blocks`'s docstring for why this wrapper exists.
    """
    return _dead_allowlist_entries_from_blocks(
        bash_fence_blocks(markdown), allowlist=allowlist
    )


def test_every_allowlist_entry_still_matches_a_real_command() -> None:
    """The sweep above passes when it finds no violation -- including when it
    finds NOTHING AT ALL. Two silent regressions produce that: a fence parser
    that stops recognizing blocks (the proj-ovgs class), and an exempt command
    deleted from land/SKILL.md while its entry lives on as a frozen verdict
    over text that no longer exists. Both leave `_KNOWN_LAND_SKILL_MUTATIONS`
    entries matching nothing, so requiring every entry to still hit a real
    line is what makes the green above mean something.

    Non-vacuousness of THIS pin is proven separately, by
    `test_every_allowlist_entry_is_provably_checked_by_sabotage` below (proj-7zap) --
    a liveness pin is a negative assertion over data expected to be empty in the
    healthy case, so its interesting branch never runs on real data and this test
    alone cannot show it would catch a real regression.
    """
    orphaned = _dead_allowlist_entries_from_blocks(
        LAND_SKILL_BLOCKS,
        allowlist=_KNOWN_LAND_SKILL_MUTATIONS,
    )

    assert orphaned == [], (
        "these allowlist entries no longer match any command in land/SKILL.md, "
        "so they exempt nothing and the sweep's green is that much emptier -- "
        f"delete them or re-derive them from the current file: {orphaned}"
    )


def test_every_allowlist_entry_is_provably_checked_by_sabotage() -> None:
    """Non-vacuousness proof for the pin above (proj-7zap): a test that passes both
    before and after the regression it's meant to catch is worthless. Mirrors the
    precedent this ticket exists to bring this module up to par with (proj-e49j),
    `test_skill_bash_state.py::test_every_allowlist_entry_is_provably_checked_by_sabotage`,
    including its measured fix: hold ONE key/fixture constant and
    vary only the markdown CONTENT between the live and dead assertions. Two
    differently-named fixtures would derive two different keys, so the "now dead"
    assertion would pass on a name mismatch alone and prove nothing about whether
    `_dead_allowlist_entries` actually tracks content -- the exact vacuous-sabotage
    shape proj-e49j's review caught and proj-1d2y's own pin (until now) had no
    counterpart to guard against at all.

    Sabotage recipe for this ticket's own future maintenance, recorded here per its
    acceptance criteria: replace the body of `_dead_allowlist_entries` with an
    unconditional `return []`, or delete any one of the three assertions below, and
    this test goes red. Both mutations were re-run independently at technical review
    (proj-7zap), as was a third -- a name-only helper, blind to content, which the
    LAST assertion is what catches. That last assertion is the one carrying the
    non-vacuity weight; the first two alone would not distinguish it.
    """
    key = "git push origin main"
    markdown_live = f"```bash\n{key}\n```\n"
    assert _dead_allowlist_entries(markdown_live, allowlist={key: "fixture"}) == [], (
        "fixture assumption broken: the key is not actually a live command in the "
        "unfixed markdown fixture"
    )

    bogus = "git push origin THIS_BRANCH_DOES_NOT_EXIST_ANYWHERE"
    assert _dead_allowlist_entries(markdown_live, allowlist={bogus: "fixture"}) == [
        bogus
    ], "a bogus key matching no real command must be reported dead"

    markdown_fixed = f"```bash\n# {key}\n```\n"  # commented out -- no longer live
    assert _dead_allowlist_entries(markdown_fixed, allowlist={key: "fixture"}) == [
        key
    ], "removing the command from the corpus must flip the SAME key from live to dead"


def test_dead_allowlist_entries_requires_a_mutating_line_not_mere_presence() -> None:
    """Non-vacuity proof for proj-dkak's fix: before this ticket,
    `_dead_allowlist_entries` computed its live set as every comment-stripped,
    `.strip()`'d line in a fenced ```bash block -- with NO `_MUTATING_CMD_RE`
    filter, unlike `_unguarded_mutations`. So a key present in the corpus only as
    a line that never matches `_MUTATING_CMD_RE` (and therefore excuses nothing
    in the sweep -- `_unguarded_mutations` never even reaches its allowlist check
    for such a line) still read as LIVE and passed the liveness pin. That is
    precisely the dead-entry class the pin exists to catch, surviving the pin.

    Sabotage: hold ONE key constant, and use the exact PRE-FIX live-set
    computation (every comment-stripped line, unfiltered -- reproduced inline
    below, not imported, since the fixed `_dead_allowlist_entries` no longer
    computes it) to prove this fixture's key would have read as live before
    this ticket. Then run the SAME key/markdown through the fixed
    `_dead_allowlist_entries` and require it now report the key dead --
    proving the regex filter, not mere textual presence, is what "live" means.

    Recorded mutation (re-run it if you touch this): restore
    `_dead_allowlist_entries`' live set to the unfiltered comprehension
    reproduced below and run this module -- this test, and ONLY this test,
    must go red. Verified at review of proj-dkak: 1 failed, 16 passed.
    """
    key = "echo this line never matches _MUTATING_CMD_RE at all"
    markdown = f"```bash\n{key}\n```\n"

    assert not _MUTATING_CMD_RE.search(key), (
        "fixture assumption broken: the chosen text matches _MUTATING_CMD_RE "
        "after all, so it does not exercise the non-mutating-line case"
    )

    # Reproduce the PRE-FIX live-set computation (unfiltered, comment-stripped
    # lines only) to prove this exact key/markdown pair would have read as
    # live before proj-dkak -- i.e. that the sabotage is not vacuous.
    pre_fix_live = {
        _strip_comment(raw).strip()
        for block in bash_fence_blocks(markdown)
        for raw in block.splitlines()
    }
    assert key in pre_fix_live, (
        "fixture assumption broken: before proj-dkak's fix this key would not "
        "actually have read as live, so the sabotage proves nothing"
    )

    # The fix: the fixed `_dead_allowlist_entries` filters by `_is_mutating`,
    # the SAME primitive `_unguarded_mutations` uses, so a key present only
    # as a non-mutating line excuses nothing and must be reported dead.
    assert _dead_allowlist_entries(markdown, allowlist={key: "fixture"}) == [key], (
        "a key present only as a non-mutating line must be reported dead -- "
        "mere textual presence does not make an allowlist entry live"
    )


def test_dead_allowlist_entries_requires_an_unguarded_position() -> None:
    """Non-vacuity proof for proj-eu04's fix: before it, the live set was every
    `_is_mutating` line in the corpus, blind to block-level guard state, so a
    key reachable only in an already-guarded position read as live.

    Sabotage recipe for this ticket's own future maintenance, per its
    acceptance criteria: hold ONE key constant and vary only the fixture's
    guard placement -- present before the mutating line (excuses nothing,
    must read dead) vs absent from the block entirely (the mutating line is
    reachable, must read live). The second dead case its acceptance criteria
    name -- a key that IS the `_GUARD`-carrying line, which sets `guarded` and
    `continue`s before the mutation check is ever reached -- is asserted last,
    with its own key (it cannot share one: the guard text is what makes it
    that case). Restoring the pre-fix live-set computation
    (every `_is_mutating` line, unfiltered by block guard state -- reproduced
    inline below, not imported, since the fixed `_dead_allowlist_entries` no
    longer computes it) must flip the guarded-block assertion from dead to
    live, proving the sabotage is not vacuous.
    """
    key = "git push origin main"

    # The block's ONLY mutating line is guarded -- `_unguarded_mutations`
    # never reaches its allowlist check here, so this key excuses nothing.
    markdown_guarded = f"```bash\n{_GUARD}\n{key}\n```\n"
    # Same key, same command text, but in a block with NO guard at all --
    # the mutating line is reachable and the key would excuse a real
    # violation.
    markdown_unguarded = f"```bash\n{key}\n```\n"

    # Reproduce the PRE-FIX live-set computation (every `_is_mutating` line,
    # blind to block guard state) to prove this exact key/fixture pair would
    # have read as live before proj-eu04 -- i.e. that the sabotage is not
    # vacuous.
    pre_fix_live = {
        cmd
        for block in bash_fence_blocks(markdown_guarded)
        for raw in block.splitlines()
        if _is_mutating(cmd := _normalized_line(raw))
    }
    assert key in pre_fix_live, (
        "fixture assumption broken: before proj-eu04's fix this key would not "
        "actually have read as live even in the guarded fixture, so the "
        "sabotage proves nothing"
    )

    # The fix: a key reachable only inside a guarded block excuses nothing
    # and must be reported dead.
    assert _dead_allowlist_entries(markdown_guarded, allowlist={key: "fixture"}) == [
        key
    ], (
        "a key present only inside an already-guarded block must be reported "
        "dead -- it excuses nothing in the real sweep"
    )

    # Same key, unguarded block: the SAME key must now read live, proving the
    # guarded-block assertion above is driven by guard placement, not by the
    # key's text.
    assert (
        _dead_allowlist_entries(markdown_unguarded, allowlist={key: "fixture"}) == []
    ), "the same key in an unguarded block must read live -- fixture assumption broken"

    # The other dead case in this ticket's acceptance criteria: a key whose own
    # text CARRIES the guard. Such a line sets `guarded` and `continue`s before
    # the mutation check, so it is never a candidate and excuses nothing --
    # even though it does match `_MUTATING_CMD_RE` on its own.
    guard_carrying = f"{_GUARD} && git push origin main"
    assert _is_mutating(guard_carrying), (
        "fixture assumption broken: the guard-carrying line must itself match "
        "_MUTATING_CMD_RE, or it exercises the proj-dkak case instead"
    )
    assert _dead_allowlist_entries(
        f"```bash\n{guard_carrying}\n```\n", allowlist={guard_carrying: "fixture"}
    ) == [guard_carrying], (
        "a key present only as the `_GUARD`-carrying line itself must be "
        "reported dead -- that line guards, it never gets excused"
    )


def test_sweep_catches_a_brand_new_unguarded_mutation() -> None:
    """The property this whole ticket exists to add: a mutating fence with NO
    guard at all, and not present in any allowlist, must be caught.

    Parametrized over one representative command per mutating verb the regex
    is meant to cover, because the verb list is the sweep's entire open-world
    boundary: dropping a verb from it silently un-covers that whole class
    while every other test here stays green. `git pull` and `git add` are in
    the list by measurement, not taste -- an earlier draft of this sweep
    omitted both, and `git add` is the omission proj-pxyt's review had already
    had to fix once in the per-fence pin proj-8p3c has since deleted.
    """
    for command in (
        "git add .",
        "git am /tmp/p.patch",
        "git apply /tmp/p.patch",
        "git checkout -f main",
        "git cherry-pick deadbeef",
        "git clean -fdx",
        "git commit -m wip",
        "git fetch origin",
        "git merge --no-ff origin/land/x",
        "git pull --rebase",
        "git rebase origin/main",
        "git reset --hard HEAD~1",
        "git restore --worktree .",
        "git revert HEAD",
        "git stash pop",
        "git switch main",
        # Not git, but a cwd-resolved mutation the sweep must still see -- see
        # `_MUTATING_CMD_RE`'s non-git alternatives (proj-8p3c).
        "bd dolt pull",
    ):
        markdown = f"1. Some new step:\n\n   ```bash\n   {command}\n   ```\n"

        violations = _unguarded_mutations(markdown, allowlist={})

        assert violations, (
            f"an unguarded, unallowlisted {command!r} was not flagged -- the "
            "mutating-verb regex no longer covers that verb, and nothing else "
            "in this module would notice"
        )
        assert command in violations[0]


def test_sweep_requires_the_guard_precede_not_merely_be_present() -> None:
    """A guard call present in the block but AFTER the mutating command does
    not protect it -- ordering matters."""
    markdown = f"```bash\ngit reset --hard origin/main\n{_GUARD} || exit 1\n```\n"
    violations = _unguarded_mutations(markdown, allowlist={})
    assert violations, "a guard AFTER the mutation must not count as coverage"


def test_sweep_allowlist_match_is_exact_text_not_shape() -> None:
    """Per this ticket's acceptance criteria: the sweep must fail if an
    allowlisted command's text changes shape, even cosmetically -- proving
    the allowlist is keyed on exact text, not on some looser notion of "the
    same command". Take a real allowlisted entry and perturb it slightly (an
    added flag); the perturbed text is a different string, so it no longer
    matches the allowlist key and must be re-flagged as an unguarded
    candidate needing its own guard or its own fresh allowlist entry.

    Both halves are asserted. The perturbed half alone would pass even if the
    allowlist were never consulted at all, so the unperturbed half is what
    proves the exemption is real and the flag is caused by the perturbation.
    """
    original = "git push origin main"
    perturbed = original + " --porcelain"

    assert (
        _unguarded_mutations(
            f"```bash\n{original}\n```\n", allowlist=_KNOWN_LAND_SKILL_MUTATIONS
        )
        == []
    ), "the unperturbed command is not actually exempt -- fixture assumption broken"

    assert _unguarded_mutations(
        f"```bash\n{perturbed}\n```\n", allowlist=_KNOWN_LAND_SKILL_MUTATIONS
    ), (
        "a cosmetically-changed allowlisted command must not silently keep "
        "matching its old allowlist entry"
    )
