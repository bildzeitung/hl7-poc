"""Fence-aware structural chunker for the ``docs/`` lookup index.

Splits each file of ``docs/*.md`` into retrievable **units** -- ``(path, line_lo,
line_hi, first_line, body)`` -- for the on-demand SQLite FTS5 index over the docs
corpus. This module is *only* the chunker; building/querying the FTS5 index is
``an earlier fix``.

**ONE boundary rule** (the decided shape; the rationale and the refuted two-rule
alternative live in the decision record cited above -- do not re-derive them):
split at the deepest ATX heading level present in the file, or at top-level
``- `` bullets in a file with no headings at all (``docs/decisions.md``). A
post-pass hard-splits any unit still over :data:`MAX_UNIT_BYTES` at the next
rung down -- heading -> bullet -> blank-line-delimited paragraph.

Boundary detection is **fence-aware**: a ``## `` or ``- `` inside a fenced code
block is someone's example, not document structure. See :func:`_fence_flags`.

Units tile each file exactly: line numbers are 1-based, inclusive on both ends,
gapless and non-overlapping, so ``"\\n".join(text.splitlines()[u.line_lo - 1 :
u.line_hi]) == u.body`` and a caller can ``Read`` precisely that range.

Each file is also :func:`classify`-ied as ``"decision-record"`` or
``"reference/process"``, consumed by the ``--class`` filter in the CLI ticket -- per the decision record's explicit split, not a guess.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from harness.fence_parsing import fence_flags as _shared_fence_flags

#: The hard size invariant: no unit may exceed
#: this many UTF-8 encoded bytes. Asserted by a test over the real corpus.
MAX_UNIT_BYTES = 16384

_HEADING_RE = re.compile(r"^(#{1,6})\s")
_BULLET_RE = re.compile(r"^-\s")

#: The decided reference/process set (docs/decisions.md, "Left open,
#: deliberately") -- the complement is "decision-record". Named explicitly in
#: the decision record, not derived from any other property of the files.
_REFERENCE_PROCESS_STEMS = frozenset(
    {"keybindings", "release", "onboarding", "tui", "editing", "test-suite-audit"}
)


@dataclass(frozen=True)
class Unit:
    """One retrievable chunk of a docs/ file."""

    path: str
    line_lo: int
    line_hi: int
    first_line: str
    body: str
    doc_class: str


def classify(path: Path | str) -> str:
    """Tag a docs/ file as ``"decision-record"`` or ``"reference/process"``."""
    return (
        "reference/process"
        if Path(path).stem in _REFERENCE_PROCESS_STEMS
        else "decision-record"
    )


def _fence_flags(lines: list[str]) -> list[bool]:
    """Per-line: is this line's content inside a fenced code block?

    Follows CommonMark's closing rule rather than a bare toggle: a fence is
    closed only by a run of the **same** marker character that is at least as
    long as the opener. Getting this wrong is silently catastrophic -- a ``~~~``
    shown as an example inside a ``` block, or a ```` ```` ```` wrapper around a
    ``` block (both routine in these docs), would close the fence early and
    leave the rest of the file mis-flagged, collapsing it into one giant unit.
    A leading indent is allowed: fences nested in a list item are real fences.

    The opening delimiter line is reported as *not* inside the fence; the
    closing one *is*. Neither can be a heading or a bullet, so the distinction
    does not affect boundary detection.

    A thin wrapper over :func:`harness.fence_parsing.fence_flags`, the ONE
    importable home of these rules. Prior to that ticket this
    was a private third copy of the same CommonMark rule set already
    hand-rolled in ``tests/conftest.py`` (``fence_scan``) -- outside the reach
    of ``tests/test_no_private_fence_state_machine.py``, which now also scans
    ``src/*.py``.
    """
    return _shared_fence_flags(lines)


def _deepest_heading_level(lines: list[str], fence_flags: list[bool]) -> int:
    """The deepest ATX heading level (1-6) present outside fences; 0 if none."""
    deepest = 0
    for line, in_fence in zip(lines, fence_flags, strict=True):
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if m:
            deepest = max(deepest, len(m.group(1)))
    return deepest


def _split_range(
    lines: list[str], line_lo: int, line_hi: int, is_boundary: Callable[[int], bool]
) -> list[tuple[int, int]]:
    """Split the 1-based inclusive range ``[line_lo, line_hi]`` at every 0-based
    line index for which ``is_boundary(i)`` holds.

    Content before the first boundary (if any) becomes its own leading unit
    (e.g. a file's text before its first heading). If no boundary is found in
    the range at all, the whole range is returned unchanged as a single unit.
    """
    lo0, hi0 = line_lo - 1, line_hi - 1
    boundaries = [i for i in range(lo0, hi0 + 1) if is_boundary(i)]
    if not boundaries:
        return [(line_lo, line_hi)]
    ranges: list[tuple[int, int]] = []
    if boundaries[0] != lo0:
        ranges.append((line_lo, boundaries[0]))  # preamble before the first boundary
    for i, start in enumerate(boundaries):
        end = boundaries[i + 1] - 1 if i + 1 < len(boundaries) else hi0
        ranges.append((start + 1, end + 1))
    return ranges


def _bullet_boundary(
    lines: list[str], fence_flags: list[bool]
) -> Callable[[int], bool]:
    """Predicate: is line ``i`` an unfenced top-level ``- `` bullet?

    One definition, used by both callers -- the primary rule for a file with no
    headings at all, and the next rung down when a heading unit is oversized.
    """

    def is_bullet(i: int) -> bool:
        return not fence_flags[i] and bool(_BULLET_RE.match(lines[i]))

    return is_bullet


def _unit_bytes(lines: list[str], line_lo: int, line_hi: int) -> int:
    return len("\n".join(lines[line_lo - 1 : line_hi]).encode("utf-8"))


def _hard_split(
    lines: list[str],
    fence_flags: list[bool],
    line_lo: int,
    line_hi: int,
    *,
    try_bullet: bool,
) -> list[tuple[int, int]]:
    """Post-pass: recursively hard-split an oversized unit at the next boundary
    down. ``try_bullet`` is True only for a unit that came from the heading
    split (a bullet-split or paragraph-split unit is already at (or past) the
    deepest rule, so it goes straight to paragraph splitting)."""
    if _unit_bytes(lines, line_lo, line_hi) <= MAX_UNIT_BYTES:
        return [(line_lo, line_hi)]

    if try_bullet:
        sub_ranges = _split_range(
            lines, line_lo, line_hi, _bullet_boundary(lines, fence_flags)
        )
        if len(sub_ranges) > 1:
            out: list[tuple[int, int]] = []
            for slo, shi in sub_ranges:
                out.extend(_hard_split(lines, fence_flags, slo, shi, try_bullet=False))
            return out
        # No bullets in range: fall through to paragraph splitting below.

    def paragraph_boundary(i: int) -> bool:
        # i == 0 is excluded rather than special-cased: _split_range already
        # emits everything before the first boundary as its own leading range,
        # so the result is identical either way -- and `lines[i - 1]` on i == 0
        # would wrap around to the last line of the file.
        if i == 0 or fence_flags[i] or lines[i].strip() == "":
            return False
        return lines[i - 1].strip() == "" and not fence_flags[i - 1]

    # Terminal rung: nothing smaller than a paragraph to split by, so no
    # recursion. Each sub-range starts AT a paragraph boundary and contains no
    # other, so re-entering here could only ever return it unchanged. A range
    # with no paragraph boundary at all (one unbroken oversized paragraph or
    # line) comes back from _split_range as itself and is emitted as-is -- the
    # documented last resort; not observed against the real corpus.
    return _split_range(lines, line_lo, line_hi, paragraph_boundary)


def chunk_file(path: Path | str, text: str) -> list[Unit]:
    """Chunk one markdown file's text into :class:`Unit` objects."""
    lines = text.splitlines()
    n = len(lines)
    if n == 0:
        return []
    fence_flags = _fence_flags(lines)
    deepest = _deepest_heading_level(lines, fence_flags)

    if deepest:

        def primary_boundary(i: int) -> bool:
            if fence_flags[i]:
                return False
            m = _HEADING_RE.match(lines[i])
            return bool(m) and len(m.group(1)) == deepest
    else:
        primary_boundary = _bullet_boundary(lines, fence_flags)

    ranges = _split_range(lines, 1, n, primary_boundary)

    final_ranges: list[tuple[int, int]] = []
    for lo, hi in ranges:
        final_ranges.extend(
            _hard_split(lines, fence_flags, lo, hi, try_bullet=bool(deepest))
        )

    doc_class = classify(path)
    units = []
    for lo, hi in final_ranges:
        body_lines = lines[lo - 1 : hi]
        body = "\n".join(body_lines)
        units.append(
            Unit(
                path=str(path),
                line_lo=lo,
                line_hi=hi,
                first_line=body_lines[0],  # lo <= hi always, so never empty
                body=body,
                doc_class=doc_class,
            )
        )
    return units


def chunk_corpus(docs_dir: Path | str) -> list[Unit]:
    """Chunk every ``*.md`` file directly under ``docs_dir``, sorted by name."""
    docs_dir = Path(docs_dir)
    units: list[Unit] = []
    for md_path in sorted(docs_dir.glob("*.md")):
        units.extend(chunk_file(md_path, md_path.read_text(encoding="utf-8")))
    return units
