"""Gate: no fenced ```bash/```sh block in a `.claude/skills/*/SKILL.md` (proj-x495) or
a `.claude/agents/*.md` (proj-lv04) may reference a shell variable that isn't ALSO
assigned somewhere within that SAME block.

## The bug class

`proj-sfnb` fixed one instance of this in `.claude/skills/land/SKILL.md`: Section 3a
populated a `declare -A MSG` associative array that Section 3's merge loop read back two
fenced blocks later. An agent executing a skill runs each fenced block as its own,
separate Bash tool invocation -- shell state (variables, arrays, functions, traps,
`set -e`/`set -o pipefail`) never survives between them, which is a harness-level fact,
not a style preference (see the governing rule land/SKILL.md states at its own top,
and `docs/agents-workflow.md`'s sibling section this test enforces). `$MSG` silently
expanded to empty, and `git merge -m ''` failed with completely empty stdout AND
stderr. That fix also established the two sanctioned remedies: **re-derive** the value
fresh in every block that needs it (cheap, deterministic), or **persist it to a file**
that every later block reads back and asserts loaded.

## Why per-block, not file-global

The obvious-looking alternative reading -- "flag a variable only if NO block in the
whole file assigns it" -- is wrong, and demonstrably so: `$MSG` (the original bug) WAS
assigned somewhere in the file (Section 3a's own block), just not in the block that
used it. The correct check has to be **per use-site**: for every `$VAR`/`${VAR...}`
reference inside a block, is `VAR` ALSO assigned somewhere in that SAME block? If not,
running that block in isolation (the only way it is ever actually run) sees an unbound
variable. This also correctly does NOT flag a variable that is assigned earlier in the
SAME block as a later use within it (e.g. a `while read` loop's own loop variables) --
that is completely ordinary same-invocation shell state, not the bug.

## Precision -- what counts as an assignment

Verified against every real fenced block in `.claude/skills/*/SKILL.md` while writing
this gate (see git history / the proj-x495 hand-off for the full audit trail). Treated
as an assignment of `VAR`, anywhere in a block, at or after a statement boundary
(start of line; after `;`, `&`, `|`, `(`; after `if`/`while`/`until`/`then`/`do`/
`else`/`elif`, optionally negated with `!`):

- `VAR=value`, `VAR+=value` (never `==`/`!=`/`<=`/`>=` comparisons, which this
  regex does not match at all since it requires a bare `=` not doubled or paired
  with a comparison operator)
- `export VAR=`, `local VAR=`, `readonly VAR=`, `declare [-flags] VAR=`
- `declare -A VAR` / `declare -a VAR` (a bare declaration, no `=`)
- `read [-flags] VAR1 VAR2 ...` -- every whitespace-separated name is assigned,
  including the common `while IFS=$'\\t' read -r e TITLE; do` shape, where the
  trailing `; do` on the same physical line must NOT swallow `TITLE` as part of
  its own token
- `mapfile`/`readarray [-flags] VAR`
- `for VAR in ...` and C-style `for ((VAR=...`

A **comment is never scanned** for either an assignment or a use -- `#` outside any
quote, at the start of the line or preceded by whitespace (so `${VAR#pattern}`'s bare
`#` parameter-expansion operator, which has no preceding whitespace, is correctly left
alone). This matters: this codebase's skills carry heavy inline prose commentary that
routinely *quotes* a variable name while explaining history or a rejected design
(`# ...a hand-restated $LANDED is now structural` is prose about a fix, not code that
runs), and scanning comment text produced false positives during development of this
gate against the real files.

## Precision -- what counts as a use, and what is deliberately excluded

`$VAR` and `${VAR...}` (any `${VAR` prefix -- covers `${VAR:-default}`,
`${VAR#pattern}`, `${VAR/a/b}`, `${VAR[@]}`, `${#VAR}` is intentionally NOT matched
since `#VAR` inside `${#VAR}` is a length operator on `VAR`, not a second identifier;
the outer `VAR` is still caught by `_USE_BRACED`). Command substitution `$(...)` and
arithmetic `$((...))` are not treated as a use of a variable named `(` -- the regex
requires an identifier character immediately after `$`/`${`. Bash's own positional and
special parameters (`$0`-`$9`, `$@`, `$*`, `$#`, `$?`, `$!`, `$$`, `$-`, `$_`) are
excluded outright -- they are never "assigned" in the sense this gate checks. A short,
explicit list of environment variables this repo's skills legitimately expect to be set
by the calling shell/operator, never by the skill's own bash, is also excluded (see
`_KNOWN_ENV_VARS` below) -- each with its own one-line justification, the same bar as
the allowlist.

## The two false positives this precision was built to avoid

A cruder prototype run during `proj-sfnb`'s review flagged `TITLE` and `ROW` in
`/sweep` (`.claude/skills/sweep/SKILL.md` Section 2's `while IFS=$'\\t' read -r e
TITLE; do ... ROW=$(printf ...) ...`) -- both are legitimately assigned and used
within that SAME block, but a naive `^VAR=`-only scan (no `read` support, no
indentation tolerance) missed both. **This gate's own test suite pins both as
non-findings** (`test_read_assigns_every_named_variable`,
`test_indented_assignment_still_counts`) precisely because a parser that
over-flags real, correct code is worse than one that under-flags: an
over-flagging gate gets its findings suppressed or its allowlist bloated with
non-bugs, which is how this exact rot restarts (per the ticket that added this
gate, proj-x495).

## Scope: EVERY skill file AND every agent file, with a small, per-variable allowlist

proj-x495 explicitly permitted either scoping this gate to `land/SKILL.md` only and
widening later, or shipping repo-wide with an allowlist. This gate ships **repo-wide**
(`.claude/skills/*/SKILL.md`) with an allowlist, because the bug class is not
land-local (per the ticket's own title) and `/sweep` already carried
real, confirmed instances that a land-only gate would leave silently uncovered.

`proj-lv04` widened the same gate to `.claude/agents/*.md` -- markdown instruction
files whose fenced bash an agent executes exactly the same way, block by block, under
the same harness rule; the bug class is not skills-specific either. At the time of
widening the shipped parser reported ZERO violations across all three agent files
(`coding.md` 31 fenced bash blocks, `code-reviewer.md` 11, `land-review.md` 3), so
widening needed no allowlist entry and no fix to any agent file, only a change to
which paths are globbed. That the widened gate actually *catches* an agent-file
regression is pinned permanently rather than checked once by hand, by
`test_sabotaged_agent_file_is_caught_by_find_violations`. Both roots share **one**
`ALLOWLIST`, keyed by a path relative to `.claude/` (not to either root) precisely so
a key can never be ambiguous about which side of the tree it names -- e.g.
`skills/land/SKILL.md` vs. a hypothetical `agents/land.md`.

**There is deliberately no whole-file escape hatch** -- not even for `land/SKILL.md`,
which was initially skipped file-wide. The reasoning for that decision and against it
lives in `docs/agents-workflow.md`'s section above ("There is no whole-file escape
hatch, deliberately"); `test_every_skill_and_agent_file_is_covered` pins the outcome.
Measured while making the call, and recorded here because it is evidence rather than
rationale: at the time (across `main`, that branch, and all five in-flight sibling
branches touching the file), the parser reported exactly `$ACCEPTED` and `$CONFLICTS`
and nothing else -- no false positive, and no sibling introduced a new instance.
`$CONFLICTS` went inert once `proj-rfon` landed and `proj-p1r3` deleted its entry, so
`$ACCEPTED` is now the only one -- for the reason recorded next to it in `ALLOWLIST`
below.

That file already went through one thorough remediation (`proj-sfnb`: `$MSG` converted
to a per-id file under `$MSG_DIR`, `$LANDED` built up incrementally -- each successful
merge appends to `$STATE_DIR/landed` in the SAME block that merges it -- and read back
with an assert-on-load by a later block). The one small, purely-notational fix this
ticket makes in it (`$id`/`$B` -> `<id>`/`<B>` in the Section 3a "HELD" note template,
matching the file's own established `<...>` convention already used two sections later)
needs no allowlist entry because it stops being a `$`-reference at all.

`challenge/SKILL.md` carries no fenced ```bash/```sh blocks at all. `code/SKILL.md`
carries **nine** and `epic-audit/SKILL.md` six, all clean. Not one of `code/SKILL.md`'s
nine fences puts its backticks at column 0: eight are indented under a list item, four
open inside a markdown blockquote, and three are BOTH -- the per-fence breakdown is
recorded once, next to the parser, in `tests/conftest.py::bash_fence_blocks`. A scanner
anchored at column 0 -- `line.startswith("```")`, the shape `tests/test_land_lock.py`
used before proj-ovgs fixed it -- therefore sees zero blocks in that file and would
report it as carrying no bash at all. `_bash_blocks` (imported from
`tests/conftest.py::bash_fence_blocks`, proj-ovgs unified what were three near-identical
private copies into one shared helper) normalizes each line before testing, so it sees
all nine -- this docstring previously recorded the column-0 answer (zero) as fact, then
(after proj-ovgs, before proj-wroz) a stale count of five.

The blockquote half of that was a SECOND blind spot, independent of the indentation one
and closed later (proj-wroz): a bare `>` survives `.strip()`, so
`stripped.startswith("```")` never matched a `> ```bash` fence even once the indentation
fix had landed -- and the marker has to come off the CONTENT lines too, or a same-block
assign-then-use pair still reads as unassigned (mechanism stated once, at
`_BLOCKQUOTE_MARKER`; pinned by `test_blockquoted_fence_content_lines_are_also_unmarked`
below). Filed as a sibling of proj-ovgs rather than folded into it: proj-ovgs's scope was
the indented-fence variant and the three-copies-to-one unification, and the blockquote
fix was measured free -- the six SKILL.md files go 51 -> 55 blocks and this gate's full
corpus, which also takes in `.claude/agents/*.md`, goes 96 -> 100, with zero new
violations either way -- only once that unification had already landed. Fixing it in
`tests/conftest.py`'s shared helper, once, rather than adding a fourth private copy of
the same workaround `tests/test_bd_list_limit_gate.py` already carries on its own input.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from pathlib import Path

from conftest import (
    AGENTS_DIR,
    SKILLS_DIR,
    markdown_corpus_blocks,
    markdown_corpus_files,
)
from conftest import bash_fence_blocks as _bash_blocks

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CLAUDE_DIR = REPO_ROOT / ".claude"

# Bash's own positional/special parameters -- never "assigned" by any skill's own code,
# so a use of one is never a finding.
_SPECIAL_VARS = {"?", "!", "$", "#", "@", "*", "-", "_"} | {str(i) for i in range(10)}

# Environment variables this repo's skills legitimately rely on being set OUTSIDE any
# fenced block -- by the calling shell, the operator, or (for LAND_* knobs) an explicit
# override convention documented in docs/agents-workflow.md, which is where landing-loop
# dev-tooling knobs live rather than docs/configuration.md.
#
# dict[name -> reason], not a bare set (proj-rscn) -- mirrors ALLOWLIST's idiom below, so
# the same non-empty-reason bar applies to both (`test_known_env_vars_all_have_a_reason`).
# Before this, each entry's justification lived only in an ordinary `#` comment, where no
# test could reach it -- exactly the gap `test_allowlist_entries_all_have_a_reason`
# already closed for ALLOWLIST.
#
# LIVENESS, not just reason text (proj-rscn, mirroring proj-e49j's fix to ALLOWLIST just
# below): a reason string stays convincing even after the use it excuses is gone.
# `LAND_LOCK_STALE_SECONDS` was exactly this -- and its former entry here is the worked
# example of why this pin exists, not merely a hypothetical: it is a real, IMPLEMENTED,
# operator-settable knob (`scripts/land-lock.sh` line 449 reads it via
# `${LAND_LOCK_STALE_SECONDS:-1800}`, and `land/SKILL.md` prose documents it at line 97),
# but this gate's corpus is fenced ```bash blocks in `.claude/skills/*/SKILL.md` +
# `.claude/agents/*.md` ONLY (`_source_files()`) -- `scripts/*.sh` is never scanned, and
# prose outside a fence isn't either. Measured at this ticket's build (proj-rscn): no
# fenced block anywhere in the real corpus references it, so the entry excused nothing
# today. It was DELETED rather than kept "because it's a real knob" -- the gate this set
# feeds is corpus-scoped by construction (every other entry and every ALLOWLIST key names
# something that shows up as a genuine cross-block reference within THIS corpus); an entry
# whose only real use lives in a script outside that corpus doesn't correct a false
# positive here, it just masks one that isn't happening -- ready to silently re-excuse a
# future reintroduction of the exact bug on that name inside a skill/agent file, the
# moment one is written (this is not a judgment call about a documented-but-unimplemented
# knob -- LAND_LOCK_STALE_SECONDS is implemented and used, just never inside this corpus).
# `test_every_known_env_var_still_matches_a_real_violation`, below the gate itself, pins
# that every remaining name here is still used-and-unassigned somewhere in the real corpus
# today; `test_every_known_env_var_is_provably_checked_by_sabotage` proves that pin is not
# vacuous.
#
# That pin is a SIBLING of ALLOWLIST's, not a shared helper: an entry here has no file
# dimension (it excuses that name in EVERY corpus file, not one), so it asks a
# different-shaped question. See `_dead_known_env_vars`'s docstring for the rest.
_KNOWN_ENV_VARS: dict[str, str] = {
    "TMPDIR": (
        "standard POSIX env var; sweep/SKILL.md reads it only via `${TMPDIR:-/tmp}` to "
        "place its own cross-block scratch state, and never assigns it."
    ),
}

# (path relative to CLAUDE_DIR, variable name) -> reason a human can audit. Relative
# to `.claude/`, not to either SKILLS_DIR or AGENTS_DIR (proj-lv04 widened this gate to
# both roots), so a key can never be ambiguous about which side of the tree it names --
# e.g. "skills/land/SKILL.md" vs. a hypothetical "agents/land.md". An entry with no
# reason is exactly how this bug class was allowed to rot in the first place
# (proj-x495) -- never add one without a specific, checkable justification.
#
# Scope of an entry is FILE-WIDE, not block-scoped: allowlisting ($ACCEPTED, land) also
# excuses a NEW block that references $ACCEPTED cross-block (verified by sabotage). That
# is the deliberate trade -- a (file, block_index, var) key would be more precise but
# would break every time anyone inserted a block earlier in the file, failing on
# unrelated edits until someone "fixed" it by widening the entry. Keep entries rare and
# keep the names specific; a generic name here is much costlier than a specific one.
#
# LIVENESS, not just reason text (proj-e49j): a reason string stays convincing even
# after the violation it excuses is fixed -- exactly what happened to $CONFLICTS (see
# the "no whole-file escape hatch" paragraph in the module docstring above). An entry
# going stale is not inert; it is a MASK that silently re-excuses a brand-new
# reintroduction of the same bug. `test_every_allowlist_entry_still_matches_a_real_
# violation`, below the gate itself, pins that every key here still corresponds to a
# real, unfiltered violation today; `test_every_allowlist_entry_is_provably_checked_by_
# sabotage` proves that pin is not vacuous.
ALLOWLIST: dict[tuple[str, str], str] = {
    ("skills/land/SKILL.md", "ACCEPTED"): (
        "Section 3a's ordered, land-review-verdict-derived accepted set -- "
        "computed by the agent's own "
        "reasoning across Sections 2c (dispatched land-review verdicts) and 3a "
        "(stacked-branch ordering), never by any single deterministic bash command in "
        "the file. land/SKILL.md states the checkable property itself, right next to "
        "the persist block: the set 'encodes land-review's per-branch judgment, which "
        "is not queryable from git or bd' -- so there is nothing upstream in this "
        "file's own bash to re-derive or persist it FROM. Note the block that uses it "
        "immediately persists it onward ($STATE_DIR/accepted), which every later block "
        "reads back -- so the cross-block hop this gate exists to catch is already "
        "closed downstream; only the initial hand-off from the agent's reasoning into "
        "bash remains. Removing this entry needs a genuine mechanical source for the "
        "set, which means land-review persisting its verdict machine-readably -- its "
        "contract rules that out today (proj-p1r3 audited for one and found none)."
    ),
}


def _strip_comment(line: str) -> str:
    """Truncate at the first unquoted `#` that starts a real comment (line start, or
    preceded by whitespace) -- never a `${VAR#pattern}` parameter-expansion operator,
    which has no preceding whitespace. Quote tracking is intentionally simple (no
    backslash-escape handling) -- sufficient for this corpus, verified against every
    real block while writing this gate."""
    in_single = False
    in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif (
            ch == "#"
            and not in_single
            and not in_double
            and (i == 0 or line[i - 1] in " \t")
        ):
            return line[:i]
    return line


# ---- USE extraction --------------------------------------------------------------
# $VAR or ${VAR...}. Never $(...)  (command substitution) or $((...)) as a use of a
# variable literally named "(" -- both regexes require an identifier character
# immediately after the $/${, so neither matches those.
_USE_SIMPLE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
_USE_BRACED = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")


def _used_vars(block: str) -> set[str]:
    found: set[str] = set()
    for raw_line in block.splitlines():
        line = _strip_comment(raw_line)
        found.update(_USE_SIMPLE.findall(line))
        found.update(_USE_BRACED.findall(line))
    return found - _SPECIAL_VARS


# ---- ASSIGNMENT extraction --------------------------------------------------------
# A statement boundary: start of line, after a separator, or after a keyword that
# introduces a new command. Shared by both assignment regexes below -- they were
# written with two hand-copied alternations, and the copy had silently lost
# `else|elif|if|while|until`, so `else declare -a Q` read as unassigned while the
# equivalent `else X=1` did not (proj-x495 review; pinned by
# test_declare_after_else_is_an_assignment).
_STMT_BOUNDARY = r"(?:^|[;&|(]|\b(?:then|do|else|elif|if|while|until)\b)\s*"

# `(?:!\s*)?` for a negated command, e.g. `if ! DEPS=$(...); then`.
# `(?!=)` so a `==` comparison is never read as an assignment.
_ASSIGN_STMT = re.compile(
    _STMT_BOUNDARY + r"(?:!\s*)?"
    r"(?:export\s+|local\s+|readonly\s+|declare\s+(?:-[A-Za-z]+\s+)*)?"
    r"([A-Za-z_][A-Za-z0-9_]*)\+?=(?!=)"
)
# A bare `declare -A VAR` / `local -a VAR` (no `=`) -- still a real assignment/declaration.
_ASSIGN_DECLARE_NOEQ = re.compile(
    _STMT_BOUNDARY
    + r"(?:local|declare|readonly)\s+(?:-[A-Za-z]+\s+)+([A-Za-z_][A-Za-z0-9_]*)\b(?!=)"
)
_ASSIGN_MAPFILE = re.compile(
    r"\b(?:mapfile|readarray)\s+(?:-[A-Za-z]+\s+)*([A-Za-z_][A-Za-z0-9_]*)\b"
)
_ASSIGN_FOR = re.compile(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b")
_ASSIGN_FOR_C = re.compile(r"\bfor\s+\(\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
_ASSIGN_READ = re.compile(r"\bread\s+(?:-[A-Za-z]+\s+)*(.+)$")
_STATEMENT_TERMINATOR = re.compile(r"[;&|<>]")


def _assigned_vars(block: str) -> set[str]:
    found: set[str] = set()
    for raw_line in block.splitlines():
        line = _strip_comment(raw_line)
        found.update(_ASSIGN_STMT.findall(line))
        found.update(_ASSIGN_DECLARE_NOEQ.findall(line))
        found.update(_ASSIGN_MAPFILE.findall(line))
        found.update(_ASSIGN_FOR.findall(line))
        found.update(_ASSIGN_FOR_C.findall(line))
        m = _ASSIGN_READ.search(line)
        if m:
            # `while IFS=$'\t' read -r e TITLE; do` -- stop at the first statement
            # terminator so `; do` on the same physical line doesn't get tokenized
            # as though it named a variable.
            rest = _STATEMENT_TERMINATOR.split(m.group(1), maxsplit=1)[0]
            for tok in rest.split():
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", tok):
                    found.add(tok)
    return found


def _unassigned_uses(block: str, *, known: Collection[str] = ()) -> set[str]:
    """Every variable USED in `block` but not ASSIGNED in that same block, minus
    `known`. The single definition of this gate's core notion -- `find_violations`
    (the gate), `_violations_in_block` (the unit-test probe) and `_dead_known_env_vars`
    (the liveness pin) all route through it, differing ONLY in what they pass as
    `known`. That sharing is load-bearing, not tidiness: the liveness pin's whole job
    is to ask "would this name have been a finding if it weren't excused", so if its
    notion of a finding ever drifted from the gate's, the pin would go quietly
    vacuous -- the exact disease proj-rscn exists to close, one level up. `known`
    defaults to empty so the unfiltered form is the plain call."""
    return (_used_vars(block) - set(known)) - _assigned_vars(block)


def _blocks_for(path: Path) -> Sequence[str]:
    """Fenced bash blocks for `path` -- conftest's cached `markdown_corpus_blocks()`
    when `path` is one of the real shipped corpus files it covers, else a fresh
    read + parse (proj-es1i).

    This fallback is the sabotage seam: `test_sabotaged_agent_file_is_caught_by_find_violations`
    and the `tmp_path`-sourced `_dead_allowlist_keys`/`_dead_known_env_vars` tests all
    pass synthetic paths that are never in `markdown_corpus_blocks()`, so they fall
    through to the direct read every time -- unaffected by the cache.

    `Sequence`, not `Collection`: `find_violations` `enumerate`s the result and
    reports the index as the BLOCK NUMBER in its failure message, so a stable
    order is part of this contract, not an incidental property of the two
    concrete types returned."""
    for corpus_path, blocks in markdown_corpus_blocks():
        if corpus_path == path:
            return blocks
    return _bash_blocks(path.read_text(encoding="utf-8"))


def find_violations(path: Path) -> list[tuple[int, str]]:
    """(block index, variable name) for every USE in a block that is not also
    ASSIGNED somewhere in that same block, excluding special/known-env vars.
    Order is block index ascending, then variable name -- deterministic for a
    stable, readable failure message."""
    violations: list[tuple[int, str]] = []
    for i, block in enumerate(_blocks_for(path)):
        for var in sorted(_unassigned_uses(block, known=_KNOWN_ENV_VARS)):
            violations.append((i, var))
    return violations


def _source_files() -> list[Path]:
    """Every file this gate parses: each skill's SKILL.md, plus every agent
    definition under `.claude/agents/*.md` (proj-lv04) -- both execute fenced bash
    the same way, block by block, under the same harness rule. `ALLOWLIST` keys are
    relative to `CLAUDE_DIR`, computed the same way at each call site, so a key can
    never be ambiguous about which of the two roots it names.

    The traversal itself is conftest's :func:`markdown_corpus_files` -- this
    module's hand-rolled `sorted(SKILLS_DIR.glob(...)) + sorted(AGENTS_DIR.glob(...))`
    was one of the copies proj-2evf hoisted, and it produces the identical
    per-glob-sorted, concatenated order."""
    return markdown_corpus_files()


# =====================================================================================
# Unit tests -- the parser's own precision, against synthetic snippets. Every case here
# is a pattern that either genuinely occurs in .claude/skills/*/SKILL.md today, or is
# named explicitly in proj-x495 as a false-positive risk to guard against.
# =====================================================================================


def _violations_in_block(block_text: str) -> set[str]:
    return _unassigned_uses(block_text, known=_KNOWN_ENV_VARS)


def test_simple_assignment_then_use_is_clean() -> None:
    assert _violations_in_block('FOO="bar"\necho "$FOO"\n') == set()


def test_use_with_no_assignment_anywhere_in_block_is_flagged() -> None:
    assert _violations_in_block('echo "$FOO"\n') == {"FOO"}


def test_cross_block_reference_is_flagged_the_land_regression_shape() -> None:
    """The exact proj-sfnb shape: assigned in one block, used in a DIFFERENT one.
    Each call below simulates a SEPARATE Bash tool invocation (a separate block) --
    `find_violations` is what actually enforces this over a real file; here we just
    confirm the per-block primitive sees block B's use as unassigned, regardless of
    what block A did (a different `_violations_in_block` call, therefore no shared
    Python state either -- mirrors "no shared shell state")."""
    block_a = (
        'declare -A MSG\nMSG[proj-abc]="Merge land/proj-abc: summary (proj-abc)"\n'
    )
    block_b = 'git merge -m "${MSG[proj-abc]}"\n'
    assert _violations_in_block(block_a) == set()
    assert _violations_in_block(block_b) == {"MSG"}


def test_read_assigns_every_named_variable() -> None:
    """Regression pin for the /sweep TITLE false positive (proj-x495's own audit): a
    cruder prototype had no `read` support at all, so `TITLE` in
    `while IFS=$'\\t' read -r e TITLE; do ... "$TITLE" ... done` read as unassigned."""
    block = "while IFS=$'\\t' read -r e TITLE; do\n  echo \"$e $TITLE\"\ndone\n"
    assert _violations_in_block(block) == set()


def test_indented_assignment_still_counts() -> None:
    """Regression pin for the /sweep ROW false positive: a cruder prototype anchored
    `^VAR=` with no leading-whitespace tolerance, so an assignment indented inside a
    loop body (completely ordinary shell) read as unassigned."""
    block = '  ROW=$(printf \'%s\' "hi")\n  echo "$ROW"\n'
    assert _violations_in_block(block) == set()


def test_for_loop_variable_is_assigned() -> None:
    block = 'for id in $ACCEPTED; do\n  echo "$id"\ndone\n'
    # $ACCEPTED itself is unassigned in THIS block (expected -- it's the point of
    # this test's fixture, not what's under test); $id must not also be flagged.
    assert _violations_in_block(block) == {"ACCEPTED"}


def test_command_substitution_is_not_a_use_of_a_variable_named_open_paren() -> None:
    assert _violations_in_block('X=$(echo hi)\necho "$X"\n') == set()


def test_arithmetic_expansion_does_not_false_positive_on_the_paren() -> None:
    block = "N=3\necho $((N + 1))\n"
    assert _violations_in_block(block) == set()


def test_default_expansion_is_a_use_not_an_assignment() -> None:
    """`${VAR:-default}` READS var (with a fallback); it must never be mistaken for
    an assignment of VAR. A name in _KNOWN_ENV_VARS would be filtered out before
    this check could see it, so use a plain unknown name (TIMEOUT) here instead --
    test_tmpdir_default_expansion_is_never_flagged below covers the filtered
    case."""
    assert _violations_in_block('echo "${TIMEOUT:-30}"\n') == {"TIMEOUT"}


def test_tmpdir_default_expansion_is_never_flagged() -> None:
    """Found during this gate's own development against the real files: sweep/SKILL.md
    reads `${TMPDIR:-/tmp}` to place its own cross-block scratch state -- a standard
    POSIX env var, never assigned by any skill's own bash."""
    assert (
        _violations_in_block('SWEEP_TMP="${TMPDIR:-/tmp}/harness-sweep-state"\n')
        == set()
    )


def test_special_parameters_are_never_flagged() -> None:
    block = 'echo "$?" "$1" "$@" "$#" "$$" "$!" "$-" "$_" "$0"\n'
    assert _violations_in_block(block) == set()


def test_comment_only_reference_is_not_a_use() -> None:
    """Regression pin for the land/SKILL.md false positives this gate's development
    found (proj-x495): heavy inline prose routinely quotes a variable name while
    describing history or a rejected design -- that must never count as a real use.
    Indented, because a real comment inside a loop body is."""
    block = 'true\n  #   grep -vxF "$dropped" "$STATE_DIR/accepted" > tmp\n'
    assert _violations_in_block(block) == set()


def test_parameter_expansion_hash_is_not_a_comment_start() -> None:
    """`${VAR#pattern}` / `${#ARR[@]}` -- the `#` is a parameter-expansion operator,
    not a comment, because nothing whitespace precedes it.

    The fixture is deliberately UNQUOTED. Two earlier pins for this rule wrote the
    `#` inside `"..."`, which made both vacuous: with the rule mutated to "any `#`
    starts a comment" the truncated line left no unassigned use either way, so
    neither test could fail (verified by mutation -- dropping the whitespace rule
    AND the quote tracking together killed zero tests). Here, truncating at the `#`
    would also swallow the `&& C=1` that follows, so the mutant reports `C` as
    unassigned and the test fails.
    """
    block = 'A=${B#x} && C=1\necho "$A $B $C"\n'
    assert _violations_in_block(block) == {"B"}


def test_if_assignment_with_negation_counts() -> None:
    """`if ! DEPS=$(cmd); then` -- land/SKILL.md's real Section-3-bounce shape.
    A prototype without `if`/negation support in its statement-boundary regex
    would miss this and flag DEPS as unassigned."""
    block = 'if ! DEPS=$(cmd); then\n  echo "$DEPS"\nfi\n'
    assert _violations_in_block(block) == set()


def test_equality_comparison_is_not_an_assignment() -> None:
    """`==` is a comparison, not a write, and must never register X as ASSIGNED --
    otherwise a real missing assignment sitting next to a comparison is masked.

    `((X==1))` and not `[ "$X" == "$Y" ]`: in the bracketed form the char before
    `==` is a quote, so no identifier abuts the `=` and `_ASSIGN_STMT` cannot match
    with OR without its `(?!=)` guard -- the earlier pin here was vacuous (verified:
    deleting `(?!=)` killed zero tests). The arithmetic form has the identifier
    directly against the `==`, so it is what actually exercises the guard.
    """
    block = 'echo "$X"\nif ((X==1)); then echo hi; fi\n'
    assert _violations_in_block(block) == {"X"}


def test_declare_dash_a_without_initializer_is_an_assignment() -> None:
    block = 'declare -A MSG\nMSG[foo]=bar\necho "${MSG[foo]}"\n'
    assert _violations_in_block(block) == set()


def test_declare_after_else_is_an_assignment() -> None:
    """`_ASSIGN_DECLARE_NOEQ` used to carry its own hand-copied, narrower boundary
    alternation (`^|[;&|(]|then|do`), so a bare `declare` after `else`/`if`/`while`
    read as unassigned while the equivalent `else X=1` did not. Both regexes now
    share `_STMT_BOUNDARY`; this fails if they are split again."""
    assert (
        _violations_in_block('if x; then y; else declare -a Q; fi\necho "${Q[@]}"\n')
        == set()
    )


def test_mapfile_and_readarray_assign_their_target() -> None:
    """No block in the corpus uses these today, so nothing else would notice if
    `_ASSIGN_MAPFILE` were deleted as dead -- it is not dead, it is unexercised."""
    assert _violations_in_block('mapfile -t ARR < f\necho "${ARR[0]}"\n') == set()
    assert _violations_in_block('readarray LINES < f\necho "$LINES"\n') == set()


def test_c_style_for_with_spaces_around_equals() -> None:
    """`for ((i = 0; ...))` is legal bash and is caught ONLY by `_ASSIGN_FOR_C` --
    `_ASSIGN_STMT` needs the `=` to abut the identifier. (Conversely `for ((i+=2))`
    is caught only by `_ASSIGN_STMT`.) They look redundant and are not."""
    assert (
        _violations_in_block("for (( i = 0; i<3; i++ )); do echo $i; done\n") == set()
    )


def test_export_and_local_prefixed_assignment() -> None:
    block = 'export FOO="bar"\nlocal BAZ="qux"\necho "$FOO $BAZ"\n'
    assert _violations_in_block(block) == set()


def test_non_bash_fence_is_never_scanned() -> None:
    """A plain (unlabeled) fence or a ```text fence is prose/template, never
    executed -- e.g. a confirmation template using `<PLACEHOLDER>`-style
    placeholders inside a plain fence, which must never be parsed as bash at all."""
    markdown = '```\necho "$UNASSIGNED"\n```\n'
    assert _bash_blocks(markdown) == []


def test_sh_fence_is_scanned_the_same_as_bash() -> None:
    markdown = '```sh\necho "$UNASSIGNED"\n```\n'
    blocks = _bash_blocks(markdown)
    assert len(blocks) == 1
    assert _violations_in_block(blocks[0]) == {"UNASSIGNED"}


def test_indented_fence_is_still_a_fence_the_shape() -> None:
    """A fence nested under a list item is indented, and a scanner anchored at column 0
    (`line.startswith("```")` -- what `tests/test_land_lock.py` did before `proj-ovgs`
    fixed it) is blind to it. Eight of `code/SKILL.md`'s nine bash blocks open with an
    indented fence (the ninth is blockquoted instead -- see the test below), so this is
    not hypothetical: a column-0 scanner reports that file as carrying no bash at all.
    `_bash_blocks` (the shared `bash_fence_blocks` helper, imported from
    `tests/conftest.py`) must strip first."""
    markdown = '1. Step one:\n\n   ```bash\n   echo "$UNASSIGNED"\n   ```\n'
    blocks = _bash_blocks(markdown)
    assert len(blocks) == 1
    assert _violations_in_block(blocks[0]) == {"UNASSIGNED"}


def test_blockquoted_fence_is_still_a_fence_the_shape() -> None:
    """A fence nested inside a markdown blockquote (`> ```bash`) is a SECOND, independent
    blind spot from the indented one above: `>` survives `.strip()`, so
    `stripped.startswith("```")` never matched it even after proj-ovgs's fix. This is not
    hypothetical either -- four of `code/SKILL.md`'s nine bash blocks open this way
    (~lines 65, 292, 324, 367). Line 65 is this test's exact shape -- blockquoted at
    column 0, no indentation -- so it is the one fence in the corpus that the indented
    test above cannot reach even in principle. `_bash_blocks` must strip the blockquote
    marker from the fence delimiters to open/close the block at all."""
    markdown = '> Step one:\n>\n> ```bash\n> echo "$UNASSIGNED"\n> ```\n'
    blocks = _bash_blocks(markdown)
    assert len(blocks) == 1
    assert _violations_in_block(blocks[0]) == {"UNASSIGNED"}


def test_blockquoted_fence_content_lines_are_also_unmarked() -> None:
    """Stripping the blockquote marker from the fence DELIMITERS is not enough on its
    own: every CONTENT line inside a blockquoted fence carries the same literal `> `
    prefix in the raw source (real code/SKILL.md shape: `> REPO_ROOT=...` on one line,
    `> ... "$REPO_ROOT" ...` on the next). If that prefix survived into the extracted
    block text, `REPO_ROOT=...`'s own `^`-anchored assignment regex would never match
    (the line would start with `> `, not the identifier), so a same-block
    assign-then-use pair would falsely report the variable as used-but-unassigned --
    exactly the false positive this test would catch if only the fence markers, and not
    the content, were unmarked."""
    markdown = '> ```bash\n> FOO=1\n> echo "$FOO"\n> ```\n'
    blocks = _bash_blocks(markdown)
    assert len(blocks) == 1
    assert _violations_in_block(blocks[0]) == set()


def test_unterminated_final_fence_is_flushed_not_dropped() -> None:
    """One of the three proj-p4qb rules; the reasoning lives with the parser.

    Sabotage recipe: in `bash_fence_blocks`, delete the trailing
    `if current is not None: blocks.append(...)` flush and this test goes red (the
    unterminated block silently vanishes, `_bash_blocks(markdown) == []`)."""
    markdown = '```bash\necho "$UNASSIGNED"\n'
    blocks = _bash_blocks(markdown)
    assert len(blocks) == 1, blocks
    assert _violations_in_block(blocks[0]) == {"UNASSIGNED"}


def test_four_backtick_fence_is_scanned_the_shape() -> None:
    """A four-backtick fence opens, AND the literal ```-prefixed line in its body
    survives as content rather than closing it early -- the two halves of the rule
    together, since a four-backtick fence exists precisely to hold such a line.

    Sabotage recipe (each half, separately): in `_fence_parsing._FENCE_MARKER_RE`, drop the `{3,}`
    -> exact `{3}` and the open marker no longer matches, so
    `_bash_blocks(markdown) == []`; or drop `_fence_parsing.closes_fence`'s `len(stripped) >=
    len(fence)` conjunct and the ``` content line closes the block early, so the
    `"```" in blocks[0]` assertion goes red."""
    markdown = '````bash\necho "$UNASSIGNED"\n```\necho done\n````\n'
    blocks = _bash_blocks(markdown)
    assert len(blocks) == 1, blocks
    assert "```" in blocks[0], (
        "the literal triple-backtick content line was lost -- it must survive as "
        f"ordinary text inside a four-backtick block: {blocks[0]!r}"
    )
    assert _violations_in_block(blocks[0]) == {"UNASSIGNED"}


def test_tilde_fence_is_scanned_and_backticks_do_not_close_it() -> None:
    """A ~~~bash fence opens, AND a ``` line inside it is content -- the SAME-character
    half of the closing rule, which the four-backtick test above cannot reach (it only
    exercises the length half).

    Sabotage recipe (each half, separately): in `_fence_parsing._FENCE_MARKER_RE`, drop the `~{3,}`
    alternative and the open marker no longer matches, so
    `_bash_blocks(markdown) == []`; or relax `_fence_parsing.closes_fence`'s `set(stripped) ==
    {fence[0]}` to accept any fence character and the ``` line closes the tilde block
    early, so the `"```" in blocks[0]` assertion goes red."""
    markdown = '~~~bash\necho "$UNASSIGNED"\n```\necho done\n~~~\n'
    blocks = _bash_blocks(markdown)
    assert len(blocks) == 1, blocks
    assert "```" in blocks[0], (
        "a backtick run closed a TILDE-opened fence -- the two fence characters "
        f"never close each other: {blocks[0]!r}"
    )
    assert _violations_in_block(blocks[0]) == {"UNASSIGNED"}


def test_two_separate_blocks_are_returned_separately() -> None:
    markdown = '```bash\nFOO=1\n```\nprose in between\n```bash\necho "$FOO"\n```\n'
    blocks = _bash_blocks(markdown)
    assert len(blocks) == 2
    assert _violations_in_block(blocks[0]) == set()
    assert _violations_in_block(blocks[1]) == {"FOO"}


# =====================================================================================
# The gate itself, against the real, shipped skill AND agent files.
# =====================================================================================


def test_allowlist_entries_all_have_a_reason() -> None:
    for key, reason in ALLOWLIST.items():
        assert reason.strip(), f"allowlist entry {key} has an empty reason"


def _unfiltered_live_pairs(
    sources: list[Path] | None = None,
) -> set[tuple[Path, str]]:
    """(source path, var) for every used-and-unassigned occurrence anywhere in
    `sources` (default: the real shipped skill/agent corpus, `_source_files()`),
    computed with NO filtering at all -- neither ALLOWLIST nor `_KNOWN_ENV_VARS`.

    This is the ONE shared, unfiltered scan both liveness pins below project from
    (proj-dutt). Before this, `_dead_allowlist_keys` routed through `find_violations`
    (which subtracts `_KNOWN_ENV_VARS` internally) while `_dead_known_env_vars` called
    `_unassigned_uses` directly with `known` left empty -- two different primitives
    that happened to agree only because ALLOWLIST filtering lives outside
    `find_violations` while `_KNOWN_ENV_VARS` filtering lives inside it. That was an
    accident of where two filters currently sit, not a property either primitive
    actually pinned: the day someone moved `_KNOWN_ENV_VARS` filtering out of
    `find_violations` (a reasonable symmetry cleanup), `_dead_allowlist_keys` would
    silently start reporting any allowlist key whose var name is also a known env var
    as dead, with nothing catching it. Routing both pins through this single scan and
    letting each apply its OWN filter on top (see the two call sites below) removes
    that hazard structurally: each pin's semantics are now pinned in ITS OWN code,
    not inherited from where a filter happens to live elsewhere.

    Paths are returned as given (not made root-relative) so this primitive commits to
    no particular key shape -- `_dead_allowlist_keys` derives a root-relative key from
    them; `_dead_known_env_vars` only needs the var name and never looks at the path.
    """
    if sources is None:
        sources = _source_files()
    pairs: set[tuple[Path, str]] = set()
    for source_md in sources:
        for block in _blocks_for(source_md):
            for var in _unassigned_uses(block):
                pairs.add((source_md, var))
    return pairs


def _dead_allowlist_keys(
    allowlist: dict[tuple[str, str], str],
    *,
    sources: list[Path] | None = None,
    root: Path = CLAUDE_DIR,
    known: Collection[str] | None = None,
) -> list[tuple[str, str]]:
    """(file, var) keys in `allowlist` that no longer correspond to any real,
    unfiltered violation across `sources` (default: the real shipped skill/agent
    corpus, `_source_files()`) whose var name is NOT also in `known`.
    A key returned here is dead: the (file, var) pair it names does not exist as a
    live, non-known-env cross-block use anywhere today, so it currently excuses
    nothing -- but the entry stays in `ALLOWLIST` regardless, ready to silently
    re-excuse a BRAND-NEW reintroduction of the exact same bug the moment one lands
    (the `$CONFLICTS` history in the module docstring above is this exact sequence,
    observed).

    Vars in `known` (default `_KNOWN_ENV_VARS`) are excluded from `live`, mirroring
    production: `find_violations` subtracts `_KNOWN_ENV_VARS` before ALLOWLIST is ever
    consulted, so no genuine ALLOWLIST key would legitimately name a known env var.
    `_dead_known_env_vars` below deliberately applies no such filter -- see
    `_unfiltered_live_pairs`'s docstring for why that asymmetry is intentional.
    `test_allowlist_key_for_a_known_env_var_name_is_reported_dead` pins this filter
    directly, so it can no longer regress silently if the shared scan is refactored
    again.

    `allowlist`, `sources`, `root` and `known` are all parameters, not read from the
    module globals directly, so
    `test_every_allowlist_entry_is_provably_checked_by_sabotage`
    below can exercise this exact primitive -- the same one the real pin uses --
    against a deliberately bogus key and a deliberately "already fixed" synthetic
    file, without mutating any real `.claude/` file on disk (out of this ticket's
    footprint, and it would make the pin's own correctness depend on real-file state
    changing underneath it later).

    `root` is what keys are derived relative to, and it exists so the sabotage test
    runs the SAME `str(path.relative_to(root))` expression the real pin does rather
    than a lookalike -- a proof over a key-derivation path production never takes
    would prove nothing about production, which is the very failure mode this module
    is about. Keys are therefore relative to `CLAUDE_DIR` here exactly as in
    `test_no_cross_block_shell_state_outside_the_allowlist`, and relative to
    `tmp_path` there.
    """
    if known is None:
        known = _KNOWN_ENV_VARS
    live = {
        (str(path.relative_to(root)), var)
        for path, var in _unfiltered_live_pairs(sources)
        if var not in known
    }
    return sorted(set(allowlist) - live)


def test_every_allowlist_entry_still_matches_a_real_violation() -> None:
    """The liveness pin proj-e49j adds. `test_allowlist_entries_all_have_a_reason`
    above only asserts the reason string is non-empty -- it cannot distinguish a live
    entry from a dead one, and a dead entry's reason string reads exactly as
    convincing as a live one's (proj-p1r3's `$CONFLICTS` had a perfectly good reason
    the entire time it was masking). This closes that gap: every key in the real
    `ALLOWLIST` must still correspond to a real, unfiltered violation found by
    `find_violations` today. When one doesn't, the remedy is to DELETE the entry --
    never to change any code -- which is why the message below says so explicitly.

    Passes unmodified against current main: both `ALLOWLIST` entries are live,
    independently verified by `test_every_allowlist_entry_is_provably_checked_by_
    sabotage` below (which proves this pin is not vacuous) and by proj-p1r3's own
    review (exactly two violations repo-wide, exactly two keys, at that branch's tip).
    """
    dead = _dead_allowlist_keys(ALLOWLIST)
    assert dead == [], (
        "these ALLOWLIST entries no longer correspond to any real cross-block "
        "violation -- they mask nothing today, but they will silently re-mask a "
        "brand-new reintroduction of the exact same bug the moment one is written. "
        f"Delete them from ALLOWLIST (do not change any other code): {dead}"
    )


def test_every_allowlist_entry_is_provably_checked_by_sabotage(
    tmp_path: Path,
) -> None:
    """Non-vacuousness proof for the pin above, per proj-e49j's own acceptance
    criteria: a test that passes both before and after the regression it's meant to
    catch is worthless here -- that is the exact failure mode this ticket exists to
    close. Two sabotage shapes, both run directly against `_dead_allowlist_keys` (the
    same primitive the real pin calls) rather than by mutating any real `.claude/`
    file on disk:

    1. ADDING A BOGUS KEY -- a (file, var) pair that matches no real violation
       anywhere -- must be reported dead.
    2. FIXING THE UNDERLYING VIOLATION -- ONE synthetic file, rewritten in place, so
       the derived key is held constant and the file's CONTENT is the only thing that
       varies between the two assertions. First it carries the real cross-block shape
       (assigned in one fenced block, used in a separate one -- a violation); then the
       identical assignment and use collapsed into a SINGLE block (no longer a
       violation, the same fix shape `proj-rfon` applied to the real `$CONFLICTS`
       instance). The same key is live before and dead after, which is what proves the
       helper tracks content rather than the key's shape.

       Two files, one "live" and one "fixed", would NOT prove that and must not be
       reintroduced: distinct filenames derive distinct keys, so the "fixed" assertion
       would pass on a name mismatch and hold just as green with the violation left
       fully intact -- a vacuous proof of non-vacuity, the exact self-refuting shape
       this module exists to catch. Measured, not theorized (proj-e49j review).

    Both shapes run through `_dead_allowlist_keys` (the same primitive the real pin
    calls), passing `root=tmp_path` so key derivation runs the identical
    `str(path.relative_to(root))` expression production runs -- and against a fixture
    path with the same multi-segment shape as a real key -- rather than mutating any
    real `.claude/` file on disk.

    Sabotage recipe for this ticket's own future maintenance, recorded here per the
    acceptance criteria: delete either assertion below, or replace `_dead_allowlist_
    keys`'s body with `return []` unconditionally, and this test goes red.
    """
    source = tmp_path / "skills" / "fixture" / "SKILL.md"
    source.parent.mkdir(parents=True)
    key = ("skills/fixture/SKILL.md", "FOO")

    def dead(k: tuple[str, str]) -> list[tuple[str, str]]:
        return _dead_allowlist_keys({k: "fixture"}, sources=[source], root=tmp_path)

    source.write_text(
        '```bash\nFOO=1\n```\n\n```bash\necho "$FOO"\n```\n', encoding="utf-8"
    )
    assert dead(key) == [], (
        "fixture assumption broken: FOO is not actually a live cross-block violation "
        "in the unfixed fixture"
    )

    bogus = ("skills/fixture/SKILL.md", "THIS_VAR_DOES_NOT_EXIST_ANYWHERE")
    assert dead(bogus) == [bogus], (
        "a bogus key matching no real violation must be reported dead"
    )

    source.write_text('```bash\nFOO=1\necho "$FOO"\n```\n', encoding="utf-8")
    assert dead(key) == [key], (
        "fixing the underlying violation must flip the SAME key from live to dead"
    )


def test_allowlist_key_for_a_known_env_var_name_is_reported_dead(
    tmp_path: Path,
) -> None:
    """The gap proj-dutt closes: an ALLOWLIST key whose var name is ALSO a
    `_KNOWN_ENV_VARS` entry must be reported dead by the allowlist liveness pin, even
    though the name IS a live unfiltered cross-block occurrence in the fixture --
    because `_KNOWN_ENV_VARS` filtering happens before ALLOWLIST is ever consulted in
    production (inside `find_violations`), so no genuine ALLOWLIST key would ever
    legitimately name a known env var. Before proj-dutt nothing pinned this: the
    filtering was inherited from `_dead_allowlist_keys` routing through
    `find_violations`, and the existing sabotage test's synthetic `FOO` is not a known
    env var, so moving the filter would have gone unnoticed.

    The name is taken FROM `_KNOWN_ENV_VARS` rather than hardcoded, so this test does
    not go stale (or quietly vacuous) if a particular entry is ever deleted, and the
    call uses the DEFAULT `known` so the default's binding to `_KNOWN_ENV_VARS` is
    what's exercised. The second assertion is the in-line non-vacuity control: the
    same key, same fixture, filter emptied via `known=()` -- it must flip to live, so
    the first assertion cannot pass for any reason other than the filter.
    """
    known_name = min(_KNOWN_ENV_VARS)
    source = tmp_path / "skills" / "fixture" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        f'```bash\n{known_name}=1\n```\n\n```bash\necho "${known_name}"\n```\n',
        encoding="utf-8",
    )
    key = ("skills/fixture/SKILL.md", known_name)
    allowlist = {key: "fixture"}
    assert _dead_allowlist_keys(allowlist, sources=[source], root=tmp_path) == [key], (
        f"an ALLOWLIST key naming the known env var {known_name} must be reported "
        "dead even though it is a live unfiltered cross-block occurrence here"
    )
    assert (
        _dead_allowlist_keys(allowlist, sources=[source], root=tmp_path, known=()) == []
    ), (
        "non-vacuity control: with the known-env filter emptied, the SAME key must be "
        "live -- so the assertion above turns on the filter and nothing else"
    )


def test_known_env_vars_all_have_a_reason() -> None:
    for name, reason in _KNOWN_ENV_VARS.items():
        assert reason.strip(), f"_KNOWN_ENV_VARS entry {name!r} has an empty reason"


def _dead_known_env_vars(
    known: Collection[str],
    *,
    sources: list[Path] | None = None,
) -> list[str]:
    """Names in `known` that no longer correspond to any real, UNFILTERED
    used-and-unassigned occurrence anywhere in `sources` (default: the real shipped
    skill/agent corpus, `_source_files()`). A name returned here is dead: nothing in
    the corpus references it as a cross-block (or any-block) use today, so it
    currently excuses nothing -- but the entry stays in `_KNOWN_ENV_VARS` regardless,
    ready to silently re-excuse a BRAND-NEW reintroduction of a cross-block bug on
    that exact name the moment one is written (`LAND_LOCK_STALE_SECONDS`, deleted
    above, was measured in exactly this state at this ticket's build, proj-rscn).

    Shares `_unfiltered_live_pairs` with `_dead_allowlist_keys` above (proj-dutt),
    projected to just the var names since entries here are bare names, not (file, var)
    keys. Applies NO known-env filter of its own -- `known` IS `_KNOWN_ENV_VARS` here,
    so filtering by it would report every name dead regardless of corpus content, the
    self-defeating shape this helper exists to avoid. That asymmetry with
    `_dead_allowlist_keys` is deliberate; see `_unfiltered_live_pairs`'s docstring.

    `known` and `sources` are parameters, not read from module globals directly, so
    `test_every_known_env_var_is_provably_checked_by_sabotage` below can exercise
    this exact primitive against a deliberately bogus name and a deliberately
    "already fixed" synthetic file, without mutating any real `.claude/` file on
    disk. `known` is a `Collection[str]`, not the `_KNOWN_ENV_VARS` dict type:
    liveness is a question about the NAMES only, and typing it that way keeps a
    caller (the sabotage test) from having to invent a reason string the function
    never reads. No `root` parameter (unlike `_dead_allowlist_keys`): entries here
    are bare names, not (file, var) keys, so there is no relative path to derive.
    """
    live = {var for _path, var in _unfiltered_live_pairs(sources)}
    return sorted(set(known) - live)


def test_every_known_env_var_still_matches_a_real_violation() -> None:
    """The `_KNOWN_ENV_VARS` sibling of `test_every_allowlist_entry_still_matches_a_
    real_violation` above, same liveness rationale (proj-rscn, mirroring proj-e49j).
    Every name in `_KNOWN_ENV_VARS` must still be used-and-unassigned somewhere in
    the real corpus today. When one isn't, the remedy is to DELETE the entry -- the
    exact call this ticket already made for `LAND_LOCK_STALE_SECONDS` (see its
    former entry's own reasoning, kept in git history and in the comment now
    documenting the deletion above `_KNOWN_ENV_VARS`).

    Passes unmodified against current main: both remaining entries (`TMPDIR`,
    `LAND_WORKTREE_DIRONLY_MIN_AGE_SECONDS`) are live, independently verified by
    `test_every_known_env_var_is_provably_checked_by_sabotage` below (which proves
    this pin is not vacuous) and by this ticket's own measurement at build time.
    """
    dead = _dead_known_env_vars(_KNOWN_ENV_VARS)
    assert dead == [], (
        "these _KNOWN_ENV_VARS entries no longer correspond to any real "
        "used-and-unassigned occurrence in the corpus -- they exclude nothing "
        "today, but they will silently re-excuse a brand-new cross-block "
        "reintroduction of the same bug on that name the moment one is written. "
        f"Delete them from _KNOWN_ENV_VARS (do not change any other code): {dead}"
    )


def test_every_known_env_var_is_provably_checked_by_sabotage(
    tmp_path: Path,
) -> None:
    """Non-vacuousness proof for the pin above, same recipe as `test_every_
    allowlist_entry_is_provably_checked_by_sabotage` and the same lesson that
    review found the hard way there (proj-e49j): a test that passes both before
    and after the regression it's meant to catch is worthless. ONE synthetic file,
    rewritten in place, so the name is held constant and the file's CONTENT is the
    only thing that varies between assertions -- two files (one "live", one
    "fixed") would derive nothing comparable and would pass vacuously, the exact
    self-refuting shape rejected there and rejected here for the same reason.

    Sabotage recipe for this ticket's own future maintenance: delete either
    assertion below, or replace `_dead_known_env_vars`'s body with `return []`
    unconditionally, and this test goes red.
    """
    source = tmp_path / "skills" / "fixture" / "SKILL.md"
    source.parent.mkdir(parents=True)
    name = "FOO_ENV_VAR"

    def dead(n: str) -> list[str]:
        return _dead_known_env_vars([n], sources=[source])

    source.write_text('```bash\necho "$FOO_ENV_VAR"\n```\n', encoding="utf-8")
    assert dead(name) == [], (
        "fixture assumption broken: FOO_ENV_VAR is not actually a live "
        "used-and-unassigned reference in the unfixed fixture"
    )

    bogus = "THIS_VAR_DOES_NOT_EXIST_ANYWHERE"
    assert dead(bogus) == [bogus], (
        "a bogus name matching no real used-and-unassigned occurrence must be "
        "reported dead"
    )

    source.write_text(
        '```bash\nFOO_ENV_VAR=1\necho "$FOO_ENV_VAR"\n```\n', encoding="utf-8"
    )
    assert dead(name) == [name], (
        "assigning the var in the same block (removing its only real use) must "
        "flip the SAME name from live to dead"
    )


def test_every_skill_and_agent_file_is_covered() -> None:
    """No file-level escape hatch exists, deliberately (proj-x495 review). A whole-file
    skip would leave NEW cross-block variables in that file unguarded too, not just the
    known ones -- and the file it was first reached for, `land/SKILL.md`, is the sole
    writer of `main`. Per-variable allowlisting keeps every other block in the file
    covered. This pins that: every skill AND every agent file carrying bash blocks is
    actually parsed (proj-lv04 added the `.claude/agents/*.md` half)."""
    # Iterates `_source_files()` -- the gate's OWN source list -- deliberately,
    # not `markdown_corpus_blocks()` directly (proj-es1i review): the sabotage
    # pin below documents that "`.claude/agents/*.md` being globbed at all" is
    # THIS test's job, and that only holds while this test reads what the gate
    # reads. Narrowing `_source_files()` back to skills-only must go red here.
    # The cache win still lands, via `_blocks_for`.
    scanned = [
        str(p.relative_to(CLAUDE_DIR)) for p in _source_files() if _blocks_for(p)
    ]
    assert "skills/land/SKILL.md" in scanned, scanned
    assert "agents/coding.md" in scanned, scanned
    # code/SKILL.md earns its own entry: it is the ONE real file whose every
    # fence opens off column 0 -- six plainly indented, three indented AND
    # blockquoted, one blockquoted only (proj-wroz) -- so it is the only one of
    # the three whose entry here goes red if `bash_fence_blocks` ever regresses
    # to a column-0 `line.startswith("```")` scanner (proj-ovgs). land/SKILL.md and
    # coding.md both keep 20 and 25 blocks under that regression -- still
    # non-empty, so they would sit here looking fine while the parser was
    # broken, and this coverage pin would pass vacuously. Verified by mutation.
    assert "skills/code/SKILL.md" in scanned, scanned


def test_code_skill_blockquoted_blocks_are_visible_and_clean() -> None:
    """Sabotage-verified against the REAL file, not a synthetic snippet (proj-wroz's
    acceptance criteria). `code/SKILL.md` carries ten bash blocks; four open with a
    blockquoted fence (~lines 65, 307, 339, 388) and were invisible to `_bash_blocks`
    before this fix. Both regressions were re-measured against this exact file rather
    than estimated: drop the blockquote strip from `bash_fence_blocks` and
    `len(blocks) == 10` fails at **6**; regress it further to the pre-proj-ovgs column-0
    `line.startswith("```")` scanner and it fails at **0**.

    The count alone is not enough -- a delimiters-only strip also parses to 10, measured.
    The load-bearing assertion is the last one: block 0 (~lines 65-68) is the real
    `$REPO_ROOT` assign-then-use pair the synthetic
    `test_blockquoted_fence_content_lines_are_also_unmarked` above covers in miniature,
    and it reports `{"REPO_ROOT"}` rather than `set()` under that partial fix."""
    text = (SKILLS_DIR / "code" / "SKILL.md").read_text(encoding="utf-8")
    blocks = _bash_blocks(text)
    assert len(blocks) == 10, blocks
    assert "REPO_ROOT" in blocks[0], blocks[0]
    assert _violations_in_block(blocks[0]) == set(), blocks[0]


def test_no_cross_block_shell_state_outside_the_allowlist() -> None:
    """The actual gate. EVERY `.claude/skills/*/SKILL.md` and every
    `.claude/agents/*.md` is parsed (proj-lv04); any (block, variable) violation not
    covered by `ALLOWLIST` fails this test with enough detail to find and fix it."""
    failures: list[str] = []
    for source_md in _source_files():
        rel = str(source_md.relative_to(CLAUDE_DIR))
        for block_index, var in find_violations(source_md):
            if (rel, var) in ALLOWLIST:
                continue
            failures.append(
                f"{rel} block {block_index}: ${var} is used but not assigned in the "
                f"same fenced block. Fenced ```bash blocks run as SEPARATE Bash tool "
                f"invocations -- shell state does not survive between them (proj-sfnb; "
                f"see docs/agents-workflow.md's 'Guard against cross-block shell "
                f"state...' section). Either re-derive ${var} inside this block, "
                f"persist it to a file an earlier block wrote and this one reads back "
                f"(with an assert-on-load), or add ('{rel}', '{var}') to ALLOWLIST in "
                f"this file with a specific reason."
            )
    assert not failures, "\n".join(failures)


def test_sabotaged_agent_file_is_caught_by_find_violations(tmp_path: Path) -> None:
    """proj-lv04's sabotage verification, kept as a permanent regression pin rather
    than a one-off manual check: a cross-block variable injected into a REAL agent
    file's fenced bash (the exact `.claude/agents/coding.md` regression shape -- a
    variable set in one ```bash block and read in a separate later one) must be
    caught by `find_violations`, the same primitive
    `test_no_cross_block_shell_state_outside_the_allowlist` runs over every skill and
    agent file.

    Routed through `find_violations` on a COPY under `tmp_path`; the real file on
    disk is never written. Calling that primitive rather than re-implementing its
    loop is what makes this a pin at all: an inlined copy of the loop body still
    passes with `find_violations` itself mutated to the file-global reading this
    module's "Why per-block, not file-global" section rejects, since under that
    reading `SABOTAGE_VAR` IS assigned somewhere in the file. Verified by mutation
    during proj-lv04's review, where the inlined form was what shipped.

    Note this also depends on `.claude/agents/*.md` being globbed at all, which is
    `test_every_skill_and_agent_file_is_covered`'s job, not this test's -- reverting
    the widening fails there, not here.
    """
    sabotaged = tmp_path / "coding.md"
    sabotaged.write_text(
        (AGENTS_DIR / "coding.md").read_text(encoding="utf-8")
        + '\n```bash\nSABOTAGE_VAR=1\n```\n\n```bash\necho "$SABOTAGE_VAR"\n```\n',
        encoding="utf-8",
    )
    violations = find_violations(sabotaged)
    assert "SABOTAGE_VAR" in {v for _, v in violations}, violations
