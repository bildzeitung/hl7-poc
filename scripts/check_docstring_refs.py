#!/usr/bin/env python3
"""Verify every Sphinx-style symbol-naming role -- ``:func:``, ``:class:``,
``:data:``, ``:meth:``, ``:attr:``, ``:mod:``, ``:exc:``, ``:obj:`` -- naming a
``harness.*`` symbol in a docstring or comment under the project's source and
test roots resolves to a real, importable symbol.

Nothing gated this before: ``scripts/check_links.py`` is markdown-only. A
single rename left FOUR dangling refs across two branches that
merged in the same ``/land`` pass, and only a hand sweep
caught them -- one of the four had never named a real symbol at all.

That same sweep found a second, independent defect class: refs
LINE-WRAPPED mid-role, e.g.::

    :func:`harness.cited_answer.
    _resolve_targets`

Sphinx cannot resolve a role containing a newline + indentation, so these
are already broken as cross-references regardless of whether the symbol
exists -- and they are invisible to the ``grep -rn <name>`` a rename
normally relies on, which is *exactly* how ``an earlier fix``'s wrapped site was
missed. This gate normalizes intra-role whitespace before resolving (so a
wrapped-but-correct ref like the one above is not a false positive), and
separately reports every wrapped ref as its own finding.

SCOPE DECISION: only roles
naming a ``harness.*`` symbol are resolved. A role naming anything else (a
stdlib or third-party symbol, e.g. ``:func:`httpx.get```) is silently
skipped -- this repo's docstrings write those routinely, and there is no
value in this gate reasoning about symbols it doesn't own. This also
disposes of the ``~`` Sphinx "show only the last component" prefix cleanly:
it is stripped before the ``harness.`` prefix check, so ``:func:`~harness.cli.
_tabular_table``` is treated identically to the unprefixed form.

This scoping covers RESOLUTION only. The wrapped-role check below is
deliberately repo-wide: a role split across a line break is a syntax defect
that stops Sphinx resolving it and hides it from ``grep`` no matter who owns
the symbol, so ``:func:`httpx.<newline>get``` hard-fails too. Ownership is a
question about whether we can check a symbol exists; wrapping is not.

RESOLUTION ALGORITHM: a dotted path ``harness.cli._short_date`` is resolved by
importing the longest importable *module* prefix, then walking the
remaining dotted segments as attribute access from there. This is
deliberate, not incidental -- this repo's own convention is that a
``harness.mod.symbol`` ref names a MODULE-ATTRIBUTE path, not necessarily a
literal ``def``/``class`` site: ``harness.cli._short_date`` resolves because
``cli/__init__.py`` re-exports it via ``from harness.timestamps import
_short_date``-style imports, not because it is defined in ``cli/__init__.py``
itself. A plain "does this exact file define this exact name" check would
reject every such re-exported ref as a false positive.

WRAPPED-REF DISPOSITION: the 81 pre-existing wrapped sites were
mechanically unwrapped in one pass -- prose-only, no behavior change, every
formerly-wrapped ref still resolves identically since the normalize step was
already collapsing its whitespace before resolving. With the backlog at
zero, this gate now HARD-FAILS on any wrapped role, same as an unresolved
one -- see ``docs/decisions.md`` for the recorded reasoning behind the
warn-then-unwrap-then-hard-fail sequencing.

``docs/decisions.md``'s own append-only exemption from pointer sweeps does
not interact with this gate at all: this gate only ever reads Python source
under the roots ``_scan_roots`` derives, never ``docs/`` prose.

Usage::

    python scripts/check_docstring_refs.py            # scan this checkout's derived roots
    python scripts/check_docstring_refs.py --root DIR  # scan a different tree (tests)
"""

from __future__ import annotations

import dataclasses
import importlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(add_completion=False)

REPO_ROOT = Path(__file__).resolve().parent.parent

# A Sphinx cross-reference role naming a Python symbol. DOTALL so the
# backtick-delimited target can itself span a line-wrap -- catching that is
# the whole point; whitespace inside is normalized below, not here.
#
# ALL the symbol-naming roles this repo writes, deliberately -- not just the
# four the ticket enumerated. ``:mod:`` (193 sites) is the single largest body
# of refs in the repo and is exactly what a module MOVE breaks, which is the
# same class of event that motivated this gate; ``:attr:`` (71) is
# semantically identical to the covered ``:data:``. Gating four of the eight
# would have left 209 of the 1124 ``harness.*`` refs silently unchecked while
# reading as "docstring refs are checked" -- a false negative, which is worse
# than a false positive here because it manufactures confidence. Widening
# costs nothing: ``resolve_ref`` already resolves bare-module paths, and the
# repo reports zero unresolved refs under the wider set.
_ROLE_RE = re.compile(
    r":(?:func|class|data|meth|attr|mod|exc|obj):`([^`]*)`", re.DOTALL
)


@dataclass(frozen=True)
class RefFinding:
    """One reported ref, carrying its own wording in ``reason`` -- mirrors
    ``check_links.py``'s ``LinkError``. The two finding kinds differ only in
    that wording. Since an earlier fix BOTH kinds hard-fail, so which list a
    finding lands in no longer decides severity -- it decides only how the
    findings are grouped and counted in ``main()``'s report. The split is
    kept over one flat list because list membership is a typed discriminator
    the callers (and tests) can rely on, where ``reason`` is free text."""

    path: Path
    line_no: int
    ref: str
    reason: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line_no}: {self.reason} -> {self.ref}"


_ROOT_PATTERNS = ("src", "tests", "packages/*/src", "packages/*/tests")


def _scan_roots(root: Path) -> list[str]:
    """Source/test directories (relative to ``root``) that exist on disk.

    ``_ROOT_PATTERNS`` unions the legacy flat layout (``src/``, ``tests/``)
    with the uv workspace layout (``packages/*/src``, ``packages/*/tests``)
    hl7-poc-ouc's split introduces. A checkout can legitimately have both
    mid-migration (some packages moved, others not), so this is a union,
    never an either/or choice -- pinning either form alone makes the gate a
    silent no-op on the other, which is the failure this gate exists to
    prevent, one layer up.

    The patterns still encode two conventions: members live under
    ``packages/``, and each keeps its sources in ``src``/``tests``.
    Reading ``[tool.uv.workspace].members`` instead would only ever replace
    the first, so the coupling is inherent rather than a shortcut."""
    return sorted(
        str(path.relative_to(root))
        for pattern in _ROOT_PATTERNS
        for path in root.glob(pattern)
        if path.is_dir()
    )


def _tracked_python_files(root: Path) -> list[Path]:
    """Every ``*.py`` file git tracks under this checkout's scan roots (see
    ``_scan_roots``) -- sourced from ``git ls-files``, like
    ``check_links.py``'s own walk, so scratch or gitignored files never
    enter this gate. (The pathspec scoping is this gate's own:
    ``check_links.py`` dropped its pathspecs once its walk went repo-wide.)"""
    existing_dirs = _scan_roots(root)
    if not existing_dirs:
        return []
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--", *existing_dirs],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(root / rel for rel in out.split() if rel.endswith(".py"))


def normalize_ref(raw: str) -> str:
    """Collapse a role's raw backtick content down to a single dotted path --
    strips a leading ``~`` (Sphinx's "display only the last component"
    marker) and removes ALL internal whitespace, which is what makes a
    line-wrapped-but-otherwise-correct ref resolve identically to its
    unwrapped form."""
    collapsed = re.sub(r"\s+", "", raw)
    return collapsed.removeprefix("~")


def _has_declared_field(obj: object, name: str) -> bool:
    """True if ``name`` is a dataclass field or a pydantic model field
    DECLARED on class ``obj``, even though neither shows up via ``hasattr``
    unless it also carries a default. Both are real, common in this repo
    (:class:`harness.chunking.Passage` is a frozen dataclass; ``Settings`` in
    :mod:`harness.config` is a pydantic ``BaseSettings``) -- without this, a
    perfectly valid ``:data:`Passage.char_range``` reads as a false-positive
    dangling ref, which is exactly the kind of noise that gets a gate
    disabled."""
    if not isinstance(obj, type):
        return False
    if dataclasses.is_dataclass(obj) and any(
        f.name == name for f in dataclasses.fields(obj)
    ):
        return True
    model_fields = getattr(obj, "model_fields", None)
    return isinstance(model_fields, dict) and name in model_fields


def resolve_ref(dotted: str) -> bool:
    """True if ``dotted`` (already normalized) names a real, importable
    ``harness.*`` symbol. Imports the longest importable prefix as a module,
    then walks any remaining dotted segments as attribute access -- so a
    module-attribute re-export path (``harness.cli._short_date``, exported by
    ``cli/__init__.py`` rather than defined there) resolves correctly. A
    segment that misses ``hasattr`` but names a declared dataclass/pydantic
    field (see ``_has_declared_field``) still counts -- there is nothing
    further to descend into past it, so it's treated as a terminal match."""
    parts = dotted.split(".")
    module = None
    split_at = 0
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        try:
            module = importlib.import_module(candidate)
            split_at = i
            break
        except ImportError:
            continue
    if module is None:
        return False
    obj: object = module
    for part in parts[split_at:]:
        if hasattr(obj, part):
            obj = getattr(obj, part)
        elif _has_declared_field(obj, part):
            obj = object()  # a field, not a live attribute -- nothing to descend into
        else:
            return False
    return True


def _refs_in_file(text: str) -> list[tuple[int, str]]:
    """``(line_no, raw_backtick_content)`` for every ``:func:``/``:class:``/
    ``:data:``/``:meth:`` role in ``text``. ``line_no`` is the role's OPENING
    line -- correct for a wrapped ref too, since that's where a human
    reading the file sees the reference start."""
    return [
        (text.count("\n", 0, m.start()) + 1, m.group(1))
        for m in _ROLE_RE.finditer(text)
    ]


def check(root: Path) -> tuple[list[RefFinding], list[RefFinding]]:
    unresolved: list[RefFinding] = []
    wrapped: list[RefFinding] = []
    for source in _tracked_python_files(root):
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, raw in _refs_in_file(text):
            normalized = normalize_ref(raw)
            if "\n" in raw:
                wrapped.append(
                    RefFinding(source, line_no, normalized, "line-wrapped reference")
                )
            if not normalized.startswith("harness."):
                continue  # third-party/stdlib -- out of scope, see module docstring
            if not resolve_ref(normalized):
                unresolved.append(
                    RefFinding(source, line_no, normalized, "unresolved reference")
                )
    return unresolved, wrapped


@app.command()
def main(
    root: Annotated[
        Path | None,
        typer.Option(
            "--root", help="Repo root to scan (defaults to this checkout's root)."
        ),
    ] = None,
) -> None:
    """Fail if any symbol-naming Sphinx role (see ``_ROLE_RE``) naming a
    ``harness.*`` symbol under this checkout's scan roots (see
    ``_scan_roots``) does not resolve, OR if any symbol-naming role is
    line-wrapped -- see the module docstring's WRAPPED-REF DISPOSITION."""
    target_root = (root or REPO_ROOT).resolve()
    # Every ``src``-shaped scan root, legacy or per-package, goes on
    # sys.path -- ``--root``'s tree must win over any ambient ``harness``,
    # and the guard has to be able to observe that it already has.
    for scan_dir in _scan_roots(target_root):
        src_dir = str(target_root / scan_dir)
        if Path(scan_dir).name == "src" and src_dir not in sys.path:
            sys.path.insert(0, src_dir)
    unresolved, wrapped = check(target_root)
    # Each kind prints its own findings immediately followed by its own
    # count -- interleaving the two loops first would detach every count
    # from the lines it counts whenever both kinds fire at once.
    if wrapped:
        for ref in wrapped:
            print(str(ref), file=sys.stderr)
        print(f"\n{len(wrapped)} line-wrapped reference(s) found", file=sys.stderr)
    if unresolved:
        for ref in unresolved:
            print(str(ref), file=sys.stderr)
        print(
            f"\n{len(unresolved)} unresolved docstring reference(s) found",
            file=sys.stderr,
        )
    if wrapped or unresolved:
        raise typer.Exit(1)
    print(
        "OK: every symbol-naming Sphinx role naming a harness.* symbol under "
        "this checkout's scan roots resolves and none is line-wrapped"
    )


if __name__ == "__main__":
    app()
