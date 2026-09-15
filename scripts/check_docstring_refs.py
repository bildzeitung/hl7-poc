#!/usr/bin/env python3
"""Verify every Sphinx-style symbol-naming role -- ``:func:``, ``:class:``,
``:data:``, ``:meth:``, ``:attr:``, ``:mod:``, ``:exc:``, ``:obj:`` -- naming a
``hl7poc.*`` symbol in a docstring or comment under the project's source and
test roots resolves to a real, importable symbol.

Nothing gated this before: ``scripts/check_links.py`` is markdown-only. A
single rename could leave a dangling ref behind, and only a hand sweep
would catch it -- and a hand sweep can itself miss one that never named a
real symbol at all.

That same class of hand sweep also has to find a second, independent
defect: refs LINE-WRAPPED mid-role, e.g.::

    :func:`hl7poc.listener.transform.
    parse_header`

Sphinx cannot resolve a role containing a newline + indentation, so these
are already broken as cross-references regardless of whether the symbol
exists -- and they are invisible to the ``grep -rn <name>`` a rename
normally relies on. This gate normalizes intra-role whitespace before
resolving (so a wrapped-but-correct ref like the one above is not a false
positive), and separately reports every wrapped ref as its own finding.

SCOPE DECISION: only roles
naming a ``hl7poc.*`` symbol are resolved. A role naming anything else (a
stdlib or third-party symbol, e.g. ``:func:`httpx.get```) is silently
skipped -- this repo's docstrings write those routinely, and there is no
value in this gate reasoning about symbols it doesn't own. This also
disposes of the ``~`` Sphinx "show only the last component" prefix cleanly:
it is stripped before the ``hl7poc.`` prefix check, so ``:func:`~hl7poc.model.
CanonicalMessage``` is treated identically to the unprefixed form.

This scoping covers RESOLUTION only. The wrapped-role check below is
deliberately repo-wide: a role split across a line break is a syntax defect
that stops Sphinx resolving it and hides it from ``grep`` no matter who owns
the symbol, so ``:func:`httpx.<newline>get``` hard-fails too. Ownership is a
question about whether we can check a symbol exists; wrapping is not.

RESOLUTION ALGORITHM: a dotted path ``hl7poc.listener.transform.parse_header``
is resolved by importing the longest importable *module* prefix, then
walking the remaining dotted segments as attribute access from there. This
is deliberate, not incidental -- a ``hl7poc.mod.symbol`` ref may name a
MODULE-ATTRIBUTE path, not necessarily a literal ``def``/``class`` site: a
symbol re-exported by a package's ``__init__.py`` resolves even though it
is defined in a submodule. A plain "does this exact file define this exact
name" check would reject every such re-exported ref as a false positive.

CHECKED-REFERENCE COUNT: the success line states how many ``hl7poc.*`` roles
were actually run through :func:`resolve_ref` (see ``check()``'s third
return value), not merely how many roles matched repo-wide. A gate whose
success output is identical whether it checked one reference or zero is
worse than useless -- it reads as "docstring refs are checked" while
silently checking nothing, exactly the failure mode a rename onto an empty
scan root produces. Zero checked references is therefore treated as a
FAILURE (exit 1) here, not just a loud line: a checkout where the scan roots
resolve but nothing under them cites an ``hl7poc.*`` symbol is a green-over-
zero regression this gate must not let past silently.

This gate only ever reads Python source under the roots ``_scan_roots``
derives; it never reads ``docs/`` prose, so nothing there can trip it.

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
# ALL the symbol-naming roles this repo writes, deliberately: ``:mod:`` is
# exactly what a module MOVE breaks, which is the same class of event that
# motivated this gate; ``:attr:`` is semantically identical to the covered
# ``:data:``. Gating only a subset would leave some ``hl7poc.*`` refs
# silently unchecked while reading as "docstring refs are checked" -- a
# false negative, which is worse than a false positive here because it
# manufactures confidence.
_ROLE_RE = re.compile(
    r":(?:func|class|data|meth|attr|mod|exc|obj):`([^`]*)`", re.DOTALL
)


@dataclass(frozen=True)
class RefFinding:
    """One reported ref, carrying its own wording in ``reason`` -- mirrors
    ``check_links.py``'s ``LinkError``. The two finding kinds differ only in
    that wording; both hard-fail, so which list a finding lands in decides
    only how the findings are grouped and counted in ``main()``'s report,
    never severity. The split is kept over one flat
    list because list membership is a typed discriminator the callers (and
    tests) can rely on, where ``reason`` is free text."""

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
    with the uv workspace layout (``packages/*/src``, ``packages/*/tests``).
    A checkout can legitimately have both
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
    unless it also carries a default. Both are real, and without this a
    perfectly valid ``:data:`SomeDataclass.some_field``` reads as a
    false-positive dangling ref, which is exactly the kind of noise that
    gets a gate disabled."""
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
    ``hl7poc.*`` symbol. Imports the longest importable prefix as a module,
    then walks any remaining dotted segments as attribute access -- so a
    module-attribute re-export path (defined in a submodule but re-exported
    by a package's ``__init__.py``) resolves correctly. A segment that
    misses ``hasattr`` but names a declared dataclass/pydantic field (see
    ``_has_declared_field``) still counts -- there is nothing further to
    descend into past it, so it's treated as a terminal match."""
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


def check(root: Path) -> tuple[list[RefFinding], list[RefFinding], int]:
    """Returns ``(unresolved, wrapped, checked)``. ``checked`` counts every
    role that passed the ``hl7poc.`` prefix filter and was run through
    ``resolve_ref``, whether or not it resolved. Counting here rather than
    deciding here is deliberate: what a zero count MEANS is a policy about
    this checkout, so it lives in ``main()`` -- see the module docstring's
    CHECKED-REFERENCE COUNT section. A ``--root`` inspection of a legitimately
    ref-free tree must still be able to report zero without erroring."""
    unresolved: list[RefFinding] = []
    wrapped: list[RefFinding] = []
    checked = 0
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
            if not normalized.startswith("hl7poc."):
                continue  # third-party/stdlib -- out of scope, see module docstring
            checked += 1
            if not resolve_ref(normalized):
                unresolved.append(
                    RefFinding(source, line_no, normalized, "unresolved reference")
                )
    return unresolved, wrapped, checked


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
    ``hl7poc.*`` symbol under this checkout's scan roots (see
    ``_scan_roots``) does not resolve, if any symbol-naming role is
    line-wrapped, or if zero references were checked at all -- see the
    module docstring's CHECKED-REFERENCE COUNT section."""
    target_root = (root or REPO_ROOT).resolve()
    # Every ``src``-shaped scan root, legacy or per-package, goes on
    # sys.path -- ``--root``'s tree must win over any ambient ``hl7poc``,
    # and the guard has to be able to observe that it already has.
    for scan_dir in _scan_roots(target_root):
        src_dir = str(target_root / scan_dir)
        if Path(scan_dir).name == "src" and src_dir not in sys.path:
            sys.path.insert(0, src_dir)
    unresolved, wrapped, checked = check(target_root)
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
    if checked == 0:
        print(
            "FAIL: zero hl7poc.* references checked -- the scan roots resolved but "
            "nothing under them cites an hl7poc.* symbol; see the module "
            "docstring's CHECKED-REFERENCE COUNT section",
            file=sys.stderr,
        )
        raise typer.Exit(1)
    print(
        f"OK: checked {checked} hl7poc.* reference(s) under this checkout's scan "
        "roots -- every symbol-naming Sphinx role resolves and none is line-wrapped"
    )


if __name__ == "__main__":
    app()
