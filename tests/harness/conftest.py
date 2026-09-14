"""Shared helpers for the agent-harness gate tests.

Extracted from the source project's much larger conftest.py: this file carries
ONLY what the harness tests need -- the markdown fence parser, the corpus
locators, and the small shell-block runners. It has no dependency on any
application code, so it drops into a fresh project unchanged.

The fence parser is the single home of the rule for what counts as "bash an
agent actually executes". Several gates below key on it, so a change to
`fence_scan`'s rules changes what every one of them considers executed.
"""

from __future__ import annotations

import functools
import importlib.util
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from _fence_parsing import closes_fence, match_fence_marker

#: Repo root -- this file is tests/harness/conftest.py, two directories below it.
_CHECKOUT_ROOT = Path(__file__).resolve().parent.parent.parent
REPO_ROOT = _CHECKOUT_ROOT


def load_module_from_path(name: str, path: Path) -> ModuleType:
    """Load the script/module at ``path`` under module name ``name``.

    Registers the module in ``sys.modules`` before executing it. That is
    load-bearing, not defensive: a module defining a dataclass fails outright
    without it, because ``dataclasses`` looks the class's own module up via
    ``sys.modules`` during class creation. scripts/check_links.py has a frozen
    ``@dataclass`` and dies with ``AttributeError: 'NoneType' object has no
    attribute '__dict__'`` if it is executed unregistered.

    The registration is permanent for the session -- nothing evicts it -- so
    ``name`` must not be a name anything else imports: a hypothetical
    scripts/build.py loaded as ``"build"`` would displace the real ``build``
    distribution for every later test in that worker. The assert below does
    NOT catch that; the module being displaced is typically not yet resident
    when the load happens. It catches the collision that always *is*
    detectable -- loading the same name a second time -- which is a genuine
    hazard for any caller whose module has import-time side effects.
    """
    assert name not in sys.modules, (
        f"{name!r} is already in sys.modules -- loading {path} under that "
        f"name would replace it for the rest of the session"
    )
    spec = importlib.util.spec_from_file_location(name, path)
    # Type-narrowing only, not a real failure mode: spec_from_file_location
    # returns None only when no loader claims the suffix, which cannot happen
    # for the .py paths this is called with.
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def pytest_configure(config: pytest.Config) -> None:
    """Register the harness's custom markers.

    Registered here (the template ships no pytest.ini/pyproject config of its
    own) so `--strict-markers`, if the host project enables it, does not turn
    the marker into a collection error.
    """
    config.addinivalue_line(
        "markers",
        "serial: this test asserts a wall-clock budget, so it must not share the machine with "
        "sibling pytest-xdist workers. `nox -s tests` runs the suite as `-m 'not serial'` under "
        "xdist and then re-invokes pytest as `-m serial -n 0`, so every test still runs exactly "
        "once.",
    )


# today's corpus, measured, but not in general -- a `>>`-leading line double-strips to a
# bare one -- and nothing strips twice any more. (tests/test_land_lock.py's independent
# fence COUNTER strips through this same constant off-path, by design: it must not call
# `fence_scan` at all, and must not re-type the marker shape either -- an earlier fix.)
_BLOCKQUOTE_MARKER = re.compile(r"^[ \t]*>[ \t]?")

# The fence-marker match and same-marker-at-least-as-long close rule are the
# ONE importable CommonMark primitive
# -- consumed here, not re-implemented. ``fence_scan`` below adds the parts
# that primitive deliberately leaves to its caller: blockquote-marker
# stripping, info-string capture, block-ordinal numbering, and
# unterminated-fence flushing. ``scripts/check_links.py``'s own
# ``_content_lines`` is NOT a consumer of the same primitive -- it toggles on
# ANY marker (no same-character/length close rule), a deliberately different,
# simpler rule; see that module's docstring.


def fence_scan(
    markdown: str,
) -> Iterator[tuple[int, str, str | None, int]]:
    """Partition every CONTENT line of ``markdown`` by which fence, if any, it
    sits inside -- the ONE state machine both ``bash_fence_blocks`` (fenced
    ```bash/```sh execution) and ``tests/test_bd_list_limit_gate.py``'s
    ``inline_violations`` (inline-backtick prose) are built on, so the two
    partition one document identically by construction rather than by two
    loops staying in sync by hand.

    Yields ``(lineno, line, enclosing_info, block_ordinal)`` per content
    line, in document order:

    * ``lineno`` -- 1-based, into the ORIGINAL ``markdown`` (not shifted by
      any fence removal).
    * ``line`` -- the line with one leading blockquote marker removed
      (``_BLOCKQUOTE_MARKER``), matching ``bash_fence_blocks``'s existing
      normalization -- a fence nested inside a blockquote is an ordinary
      fence. Only that marker is removed; leading/trailing whitespace inside a
      fenced block survives untouched, so a caller matching *content* against a
      pattern normally ``.strip()``s it itself.
    * ``enclosing_info`` -- the still-open fence's info string (``"bash"``,
      ``"text"``, ``""`` for a bare fence with nothing after it, ...) for a
      line INSIDE a fence, or ``None`` for a line outside every fence.
    * ``block_ordinal`` -- 0-based count of fence blocks opened so far in the
      document. Meaningful only while ``enclosing_info is not None``; lets a
      caller regroup content lines by which physical fence produced them
      without re-deriving fence boundaries of its own.

    A fence DELIMITER line itself (the opening ```` ```lang ```` or the
    closing ```` ``` ````) is never yielded -- neither consumer has ever
    wanted the delimiter text, only what is inside it.

    Matches the fence marker on the stripped line, never
    ``line.startswith("```")``: a fence indented under a markdown list item is
    legitimately indented, and a column-0-anchored scanner reports such a file
    as carrying no bash at all -- the an earlier fix bug. Measured on
    ``.claude/skills/code/SKILL.md``, the one consumed file that exercises
    both shapes: of its nine bash fences, five are plainly indented, three are
    indented AND inside a blockquote, and one (line 65) is a top-level
    blockquote with no indentation at all. Not one of the nine puts its
    backticks at column 0, which is why a column-0 scanner sees zero blocks
    there.

    Three further rules, all settled by an earlier fix and all latent on today's
    corpus -- zero instances of any of them exist in any of the repo's
    markdown files, measured, so this is hardening rather than a live-bug fix:

    * a FOUR-OR-MORE-backtick fence and a TILDE (``~~~bash``) fence are both
      scanned (see :func:`_fence_parsing.match_fence_marker`), not
      silently skipped.
    * a closing run must be the SAME character as the opening one and AT LEAST
      AS LONG (CommonMark), so a ```-prefixed line inside a four-backtick
      block is content, not a close -- which is the whole reason an author
      reaches for the four-backtick form.
    * an UNTERMINATED final fence is FLUSHED, not dropped. Dropping is the
      same false-assurance shape this generator exists to delete: a gate
      would report "clean" for a block it never parsed.

    **Only ONE fence is ever tracked open at a time** -- this is
    what closes the asymmetry that survived an earlier fix's constant-sharing: the
    old ``bash_fence_blocks`` opened ``current`` (its own tracking state) only
    on a bash/sh info string, so it never recorded that it was already inside
    an ENCLOSING non-bash fence, and read a ```bash run nested inside a
    ````text block as executable -- a false POSITIVE, not a silent miss
    (measured latent: zero nested fence openers across all 58 tracked .md
    files, 203 top-level fences scanned, at the time this was found). This
    state machine tracks the CURRENTLY OPEN fence regardless of its info
    string, so a ```bash-looking line encountered while ``enclosing_info`` is
    already ``"text"`` is correctly reported as a content line of that outer
    ``text`` fence (i.e. never opens its own nested tracking), matching what
    CommonMark itself does: a fence cannot open inside an already-open fence,
    only close it (or fail to, and remain content).

    One remaining known boundary, and the only one left of the OPPOSITE kind
    -- corruption rather than a silent skip: the blockquote strip cannot tell
    a blockquote marker from a redirection, so it also fires on a CONTENT line
    whose first non-blank character is ``>``. ``>&2 echo hi`` extracts as
    ``&2 echo hi``, ``>> log`` as ``> log``, ``> out`` as ``out``. Unlike the
    three above this is silent CORRUPTION, not a silent skip, so a gate
    asserting on exact command text would assert against the mangled form.
    Measured under no consumed file has such a line today -- every
    block the pre-strip parser saw is byte-identical after it, across every
    ``.claude/skills/*/SKILL.md`` and ``.claude/agents/*.md`` -- so re-measure
    rather than assume if one is ever added.
    """
    fence = ""  # the opening run, e.g. "```" or "````" or "~~~"
    info: str | None = None
    ordinal = -1
    for lineno, raw_line in enumerate(markdown.splitlines(), 1):
        line = _BLOCKQUOTE_MARKER.sub("", raw_line, count=1)
        stripped = line.strip()
        if fence:
            if closes_fence(stripped, fence):
                fence = ""
                info = None
                continue
            yield (lineno, line, info, ordinal)
            continue
        marker_match = match_fence_marker(stripped)
        if marker_match:
            fence, raw_info = marker_match
            info = raw_info.strip()
            ordinal += 1
            continue
        yield (lineno, line, None, ordinal)


def bash_fence_blocks(markdown: str) -> list[str]:
    """Every fenced ```bash/```sh block in ``markdown``, as separate strings,
    in document order -- what an agent actually EXECUTES, one Bash tool
    invocation per block.

    Built on :func:`fence_scan`: groups its content lines whose
    ``enclosing_info`` is ``"bash"`` or ``"sh"`` by ``block_ordinal``, in
    document order. A caller that wants every block concatenated into one
    string (e.g. to check for an offending token whose position within the
    file doesn't matter) can ``"\\n".join(bash_fence_blocks(markdown))`` the
    result. See :func:`fence_scan`'s docstring for the full rule set
    (indentation, blockquotes, four-backtick/tilde fences, unterminated
    fences, the nested-fence fix, and the known blockquote/redirection
    corruption boundary) -- stated once, there, not duplicated here.
    """
    blocks: list[str] = []
    current: list[str] = []
    current_ordinal: int | None = None
    for _lineno, line, info, ordinal in fence_scan(markdown):
        if info not in {"bash", "sh"}:
            continue
        if ordinal != current_ordinal:
            if current_ordinal is not None:
                blocks.append("\n".join(current))
            current = []
            current_ordinal = ordinal
        current.append(line)
    if current_ordinal is not None:  # unterminated final fence -- flushed
        blocks.append("\n".join(current))
    return blocks


def only_block_with(blocks: list[str], *needles: str, what: str) -> str:
    """The single block in ``blocks`` containing every needle -- asserts exactly
    one.

    Asserts exactly one hit rather than taking ``next(..., None)``, which would
    silently pin the first of several near-identical-looking blocks; each
    caller's docstring names the live pair it would misfire on.

    Takes ``blocks`` rather than a markdown path so it composes with either
    caller's own ``_skill_blocks()`` (each closes over a different SKILL.md via
    :func:`bash_fence_blocks`), instead of this function picking the file.
    """
    hits = [b for b in blocks if all(n in b for n in needles)]
    assert len(hits) == 1, (
        f"expected exactly 1 fenced block for {what}, found {len(hits)} -- this "
        "test's assumption about SKILL.md's structure has drifted; re-check by "
        "hand before adjusting the locator"
    )
    return hits[0]


def fake_bin_env(bin_dir: Path) -> dict[str, str]:
    """``os.environ``, overlaid so ``bin_dir`` is first on ``PATH``.

    How a test puts a fake tool (usually a fake ``bd``) in front of the real
    one for a subprocess under test: the real environment untouched, except
    ``PATH`` gaining ``bin_dir`` at the front. Callers needing further keys
    overlay them on the result, as :func:`run_block` does with ``TMPDIR``.

    An unset ``PATH`` yields ``bin_dir`` alone rather than a trailing empty
    entry, which POSIX reads as the current directory.
    """
    existing = os.environ.get("PATH", "")
    path = f"{bin_dir}{os.pathsep}{existing}" if existing else str(bin_dir)
    return dict(os.environ, PATH=path)


def run_block(
    block: str, sweep_tmp: Path, bin_dir: Path, *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Run one fenced ```bash block as its own, fresh subprocess.

    This is the execution convention every test that runs a skill's real
    fenced blocks (rather than merely locating them, see
    :func:`only_block_with`) is built on: one fresh ``bash`` subprocess PER
    block, mirroring an agent's own one-Bash-tool-invocation-per-fence
    execution model, so nothing a block sets in its own shell survives into
    the next one. ``bin_dir`` is prepended to ``PATH``
    ahead of the real one -- the caller's fake ``bd`` (or other faked tool)
    lives there, via :func:`fake_bin_env`. ``TMPDIR`` is redirected to
    ``sweep_tmp``'s parent so a block's own ``${TMPDIR:-/tmp}/harness-sweep-state``
    derivation lands exactly on the ``sweep_tmp`` fixture's directory -- that
    derivation is ``/sweep``'s §0 convention SPECIFICALLY, so a caller testing
    a different skill's blocks inherits a redirection it did not ask for.

    ``cwd`` is REQUIRED, keyword-only, and has no default -- it used to
    default silently to :data:`_CHECKOUT_ROOT`, the live checkout. Every
    existing caller only ever hands this function /sweep's read-only fences,
    so passing ``cwd=_CHECKOUT_ROOT`` there is fine and stays explicit at
    the call site; the point of removing the default is that a future caller
    handing this a destructive fence (a /land or /code section) is now
    forced to make its own cwd choice instead of silently inheriting the
    live checkout -- a defect class this suite has hit for real (see
    tests/test_gate_lib.py's `_run_script` docstring).
    """
    env = dict(fake_bin_env(bin_dir), TMPDIR=str(sweep_tmp.parent))
    return subprocess.run(
        ["bash", "-c", block],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        check=False,
    )


@pytest.fixture
def sweep_tmp(tmp_path: Path) -> Path:
    """Mirrors ``/sweep``'s own §0 layout: ``$SWEEP_TMP =
    $TMPDIR/harness-sweep-state``.

    Shared by every pin of that layout, so a future change to it moves here
    once.
    """
    d = tmp_path / "harness-sweep-state"
    d.mkdir()
    return d


def _fenced_bash(markdown: str) -> str:
    """The ```bash fences only, concatenated into one string -- what an agent
    actually EXECUTES.

    Scanning the whole file would also match prose that merely *describes* a
    command (quoting it while explaining a past defect), so a pin that wants
    to assert something about what actually runs has to separate the executed
    fences from the surrounding description. That split is also the point:
    the fence is the one part of these skill docs no other gate parses, which
    is how more than one bug here survived unnoticed until someone read the
    file by hand.

    ``tests/test_land_lock.py`` and ``tests/test_assert_main_checkout.py``
    each carried their own copy of this until an earlier fix; the parser's rules
    and blind spots live next to :func:`bash_fence_blocks`, not here.
    """
    return "\n".join(bash_fence_blocks(markdown))


#: The two roots every skill/agent-definition markdown corpus glob scans over.
#: Was defined independently in several tests/ modules (byte-identical
#: ``REPO_ROOT / ".claude" / "skills"`` / ``... / "agents"`` pairs) until
#: an earlier fix hoisted them here -- a layout change needed each updated by hand,
#: and conftest.py had no directory-level constants at all, only the
#: per-file ones below (``LAND_SKILL``, ``CODE_REVIEWER_AGENT``, ...).
SKILLS_DIR = _CHECKOUT_ROOT / ".claude" / "skills"
AGENTS_DIR = _CHECKOUT_ROOT / ".claude" / "agents"

#: (root, glob pattern) pairs covering the whole markdown corpus a Claude Code
#: agent executes bash out of: every skill's ``SKILL.md`` and every subagent
#: definition under ``.claude/agents``. The single source of truth for the
#: scan surface -- :func:`markdown_corpus_files` below is its flattened,
#: sorted-per-glob-then-concatenated view. A module that needs its own
#: monkeypatch target keeps a local alias bound to this tuple (e.g.
#: ``SCAN_GLOBS = MARKDOWN_CORPUS_GLOBS``, rebound by ``monkeypatch.setattr``)
#: rather than redefining the pairs.
#:
#: A ``tuple``, not a ``list``, for the same reason
#: :func:`markdown_corpus_blocks` returns tuples: every aliasing module shares
#: this one object, so an in-place ``append``/``remove`` anywhere would
#: silently change the scan surface for the whole session. Rebinding an alias
#: is the supported narrowing; mutating is now a ``TypeError``.
MARKDOWN_CORPUS_GLOBS: tuple[tuple[Path, str], ...] = (
    (SKILLS_DIR, "*/SKILL.md"),
    (AGENTS_DIR, "*.md"),
)


def markdown_corpus_files() -> list[Path]:
    """Every file in :data:`MARKDOWN_CORPUS_GLOBS`, sorted within each glob and
    concatenated in glob order -- the exact ordering every prior hand-rolled
    ``sorted(SKILLS_DIR.glob(...)) + sorted(AGENTS_DIR.glob(...))`` produced,
    preserved so no consumer's iteration order shifts.

    Not cached: it is a pair of directory listings, cheap enough that a shared
    cache would buy nothing. Contrast :func:`markdown_corpus_blocks` below,
    which IS cached because it also reads and fence-parses file content -- the
    expensive, worth-caching part.
    """
    files: list[Path] = []
    for base, pattern in MARKDOWN_CORPUS_GLOBS:
        files.extend(sorted(base.glob(pattern)))
    return files


@functools.cache
def markdown_corpus_text() -> tuple[tuple[Path, str], ...]:
    """(path, raw text) for every file in :data:`MARKDOWN_CORPUS_GLOBS`, in
    :func:`markdown_corpus_files` order -- every reader that comes through
    this cache reads the corpus ONCE per session between them, however many
    gates or per-file constants want it. It is NOT true that the
    corpus is read once per session unconditionally -- two gates still read
    corpus files directly; see :func:`markdown_corpus_blocks`'s "scope of the
    win" paragraph. :func:`markdown_corpus_blocks` below derives its
    fence-parsed view from this instead of reading the files itself, and the
    per-file ``_TEXT`` constants (:data:`LAND_SKILL_TEXT`,
    :data:`SWEEP_SKILL_TEXT`, and the read that backs
    :data:`CODE_REVIEWER_AGENT_BLOCKS`) look their file up here via
    :func:`_corpus_text` rather than calling ``read_text`` a second time.

    Returns tuples, not lists, for the same reason :func:`markdown_corpus_blocks`
    does -- see its docstring.
    """
    return tuple(
        (path, path.read_text(encoding="utf-8")) for path in markdown_corpus_files()
    )


@functools.cache
def markdown_corpus_blocks() -> tuple[tuple[Path, tuple[str, ...]], ...]:
    """(path, fenced-bash-blocks) for every file in :data:`MARKDOWN_CORPUS_GLOBS`,
    in :func:`markdown_corpus_files` order -- the whole-corpus fence-parse
    happens ONCE per session here, however many gates want it, instead of
    once per importing gate. Before that,
    ``tests/test_validate_sha40_call_sites.py`` re-read and re-fence-parsed
    the whole corpus at import time on its own.

    Derives from :func:`markdown_corpus_text` rather than reading files
    itself, so the whole corpus is read exactly once per session regardless
    of how many of the two caches a given test run touches. The
    per-file constants below (:data:`LAND_SKILL_BLOCKS`,
    :data:`CODE_REVIEWER_AGENT_BLOCKS`, :data:`SWEEP_SKILL_BLOCKS`) now also
    derive their text from :func:`markdown_corpus_text` (via
    :func:`_corpus_text`), so no *blocks* view of the corpus is parsed outside
    these two caches. ``tests/test_skill_bash_state.py``'s whole-corpus scan
    sites and ``tests/test_bd_list_limit_gate.py``'s agent-definition ``bd``
    line count read this cache too, rather than their own read + fence-parse
    passes. (No inventory of those call sites here on purpose: naming
    functions in other modules from conftest's docstring only drifts -- grep
    for this function's name to get the live list.)

    Scope of the win, stated honestly: this still deduplicates only the
    fence-parsed *blocks* view. ``tests/test_bd_list_limit_gate.py::_scan_corpus``
    and ``tests/test_sweep_pipeline_label_roster_gate.py::_discover_add_label_sites``
    both need raw text with line offsets a blocks-only cache structurally
    cannot serve (the latter also monkeypatches its own root), so they still
    call ``read_text`` on corpus files -- including all three named above --
    directly. They are deliberately out of scope here, so "read once per
    session" is true of the per-file constants and the whole-corpus gates
    listed above, NOT of the repository's markdown files unconditionally.

    Returns tuples, not lists -- ``@functools.cache`` hands every caller the
    same object, and a mutable ``list`` result would let one caller's mutation
    corrupt every other reader's view (the same hazard the per-skill
    ``_BLOCKS`` constants below narrow by staying at module scope; this one
    is enforced structurally instead, since it fans out over an open-ended
    file set rather than a fixed handful of named constants).
    """
    return tuple(
        (path, tuple(bash_fence_blocks(text))) for path, text in markdown_corpus_text()
    )


def _corpus_text(path: Path) -> str:
    """Look up ``path``'s content in :func:`markdown_corpus_text` -- the
    per-file ``_TEXT`` constants below (and the inline
    :data:`CODE_REVIEWER_AGENT_BLOCKS` read) use this instead of a second
    ``path.read_text(...)`` against a file :func:`markdown_corpus_text`
    already read. Raises if ``path`` isn't covered by
    :data:`MARKDOWN_CORPUS_GLOBS` -- every caller here is one of the three
    fixed corpus files, so a miss means the glob and the constant have
    drifted apart, not a normal runtime condition to swallow.
    """
    for corpus_path, text in markdown_corpus_text():
        if corpus_path == path:
            return text
    raise LookupError(
        f"{path} not covered by MARKDOWN_CORPUS_GLOBS -- markdown_corpus_text() "
        "has no entry for it"
    )


#: The land skill doc. Three of its readers parse its fenced bash blocks (via
#: :func:`bash_fence_blocks`/:func:`_fenced_bash` above), which is why it lives
#: here; test_worktree_gc_classify.py reads the same file but scans its
#: ``case "$BUCKET"`` dispatch directly rather than through the fence parser.
#: Was defined byte-identically in four modules (test_worktree_gc_classify.py,
#: test_land_conflicts_state.py, test_land_lock.py,
#: test_assert_main_checkout.py) until an earlier fix consolidated it here. The last
#: of those has since been split and no longer references
#: LAND_SKILL at all; its text-gate half is test_land_skill_guard_coverage.py.
LAND_SKILL = _CHECKOUT_ROOT / ".claude" / "skills" / "land" / "SKILL.md"

#: The land skill doc's text, read once per session rather than once per test
#:. ``tests/test_land_lock.py`` alone carried eleven
#: static ``LAND_SKILL.read_text(encoding="utf-8")`` call sites against this
#: 174KB file -- and more reads than that per run, since two of them sit in
#: per-test block locators (``_acquire_block``/``_pass_start_block``) rather
#: than in a test body. Every call site there now reads this cached value
#: instead.
#: A test that is pinning the *parser itself*
#: (``test_fenced_bash_sees_every_bash_marker_including_indented_ones``) still
#: calls :func:`bash_fence_blocks` directly on this text rather than going
#: through :data:`LAND_SKILL_BLOCKS`/:data:`LAND_SKILL_BASH` below, since the
#: point of that test is to exercise the parser, not to reuse a pre-parsed
#: result. Sourced from :func:`markdown_corpus_text` via :func:`_corpus_text`
#: rather than its own ``read_text`` call -- LAND_SKILL is one of
#: the corpus files :func:`markdown_corpus_text` already reads.
LAND_SKILL_TEXT = _corpus_text(LAND_SKILL)

#: :func:`bash_fence_blocks` applied to :data:`LAND_SKILL_TEXT` once per
#: session. See :data:`LAND_SKILL_TEXT` above for why this is cached at all.
LAND_SKILL_BLOCKS = bash_fence_blocks(LAND_SKILL_TEXT)

#: :func:`_fenced_bash`'s result on :data:`LAND_SKILL_TEXT` once per session --
#: equivalently, ``"\n".join(LAND_SKILL_BLOCKS)``. See :data:`LAND_SKILL_TEXT`
#: above for why this is cached at all.
LAND_SKILL_BASH = "\n".join(LAND_SKILL_BLOCKS)

#: The sweep skill doc, derived the same way as LAND_SKILL above. Was
#: hand-derived independently in every tests/ module that pins a sweep block
#: (five of them by then) until an earlier fix consolidated it here. Import this
#: rather than re-deriving it: the duplication had already re-forked once, on a
#: file that landed after the first consolidation attempt was written.
SWEEP_SKILL = _CHECKOUT_ROOT / ".claude" / "skills" / "sweep" / "SKILL.md"

#: The code-reviewer agent definition, derived the same way as LAND_SKILL /
#: SWEEP_SKILL above. Added by an earlier fix's technical review, whose
#: ``tests/test_validate_sha40_call_sites.py`` is the first module to pin a
#: fenced bash block in an ``.claude/agents/*.md`` file rather than a
#: ``SKILL.md`` -- ``tests/test_no_hand_derived_skill_md_path.py`` covers both
#: roots, so the constant belongs here for the same reason the skill ones do.
CODE_REVIEWER_AGENT = _CHECKOUT_ROOT / ".claude" / "agents" / "code-reviewer.md"

#: :func:`bash_fence_blocks` over :data:`CODE_REVIEWER_AGENT`, once per session.
#: See :data:`LAND_SKILL_TEXT` for why this is cached at all. No separate
#: ``_TEXT`` constant: unlike ``LAND_SKILL_TEXT`` (several modules read the raw
#: prose), nothing needs the text itself yet, so the read is inlined here rather
#: than exported dead. Sourced from :func:`markdown_corpus_text` via
#: :func:`_corpus_text` rather than its own ``read_text`` call.
CODE_REVIEWER_AGENT_BLOCKS = bash_fence_blocks(_corpus_text(CODE_REVIEWER_AGENT))

#: The sweep skill doc's text, read once per session rather than once per test
#: -- the same fix LAND_SKILL_TEXT above applied to LAND_SKILL.
#: All five tests/test_sweep_*.py modules that previously called
#: ``SWEEP_SKILL.read_text(encoding="utf-8")`` directly now read this instead.
#: Sourced from :func:`markdown_corpus_text` via :func:`_corpus_text` rather
#: than its own ``read_text`` call.
SWEEP_SKILL_TEXT = _corpus_text(SWEEP_SKILL)

#: :func:`bash_fence_blocks` applied to :data:`SWEEP_SKILL_TEXT` once per
#: session. See :data:`SWEEP_SKILL_TEXT` above for why this is cached at all.
SWEEP_SKILL_BLOCKS = bash_fence_blocks(SWEEP_SKILL_TEXT)

# DECISION -- deliberately a plain `#` block, not the `#:`
# attribute-doc form used above: it documents no single constant, and an `#:`
# run here would silently become the rendered doc for whatever constant is
# added below it next.
#
# Why the per-skill constants above rather than a generic
# ``@functools.cache`` on :func:`bash_fence_blocks`: that function returns a
# plain, MUTABLE ``list[str]``, so decorating it would hand every caller in
# the session the *same* list object -- an unenforced contract across ~10
# modules that a future caller could break by mutating its "own" result. The
# constants above (``LAND_SKILL_BLOCKS``/``LAND_SKILL_BASH``,
# ``SWEEP_SKILL_BLOCKS``) narrow that exposure to a fixed, reviewable set
# computed once at import time from a fixed input.
#
# They do NOT eliminate it, and the honest version of the read-only claim is:
# every consumer today is read-only EXCEPT
# ``tests/test_land_conflicts_state.py::test_section_3_regate_precedes_push_is_sabotage_proven``,
# which copies (``sabotaged = list(blocks)``) before reordering. That copy was
# incidental when each call re-parsed the file; it is load-bearing now, and is
# annotated as such at its own call site.
#
# A module that gains its own fresh SKILL.md to pin should add its own
# ``<NAME>_TEXT``/``<NAME>_BLOCKS`` pair here, following this same shape,
# rather than reaching for a shared cached helper. Making
# :func:`bash_fence_blocks` return a ``tuple[str, ...]`` would enforce the
# contract instead of documenting it; it was left alone here because it also
# changes the parser's own pinned equality assertions, well outside a
# tests-only hoist (tracked separately).


# --- TUI test settle helpers -----------------------------------
#
# The ONE home for both of harness's settle-under-load patterns for driving a
# Textual pilot -- see docs/tui.md's "Settling TUI tests under load" section
# for the ruling (which helper applies when) and the verified mechanism
# (wait_for_idle's CPU-vs-wall-clock heuristic is the only load-sensitive
# element in the path; asyncio's ready-queue ordering is not perturbed by OS
# starvation). Moved here verbatim from tests/test_tui_reconcile_screen.py
# and tests/test_tui_browse_screen.py
# , which had independently invented the same
# fix twice with no cross-reference. Do not add a third dialect in a new test
# file -- import one of these two instead.

#: How often :func:`_wait_until` re-checks its predicate.
