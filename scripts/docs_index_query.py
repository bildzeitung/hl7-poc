"""Typer CLI over the docs/ lookup index -- ranked pointers, never answers.

Builds the index fresh on
every invocation and returns RANKED POINTERS + SNIPPETS: ``path:line_lo-line_hi``,
the unit's first line, and a short snippet. It NEVER prints a whole unit and
NEVER synthesizes a prose answer -- this is a lookup tool, not a second Q&A
system. The caller reads the
exact range itself.

FTS5 INPUT ESCAPING is the load-bearing half of this ticket. Raw user input
must never reach ``MATCH`` unescaped -- measured against a real index during
the epic's ``/challenge``: a bd issue id fails with ``no such
column: nt98`` (the hyphen parses as a column filter), a slash-prefixed
question (``what did we decide about /land?``) is an FTS5 syntax error, and a
hyphenated term (``push-vs-pull``) fails the same way. Every bd issue id --
the most natural query key in this repo -- would error on query one.
:func:`_escape_query` fixes all three: split on whitespace, wrap EACH
resulting token in double quotes (escaping any embedded ``"`` by doubling
it, FTS5's own quoting rule), and join with a space -- FTS5's implicit AND
between phrase terms. A double-quoted phrase is matched as a literal string
by the FTS5 query grammar, so no character inside it (hyphen, slash, digit)
is ever parsed as query syntax.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Annotated

import typer

app = typer.Typer(add_completion=False)

#: Chunker doc-class tags, in the order they're documented.
_DOC_CLASSES = ("decision-record", "reference/process")

#: How much of a unit's body to show as a snippet -- enough to orient the
#: reader without reprinting the whole unit (which the acceptance criteria
#: forbids outright; median unit is 2.5 KB, so even a generous snippet stays
#: far short of that).
_SNIPPET_CHARS = 240


def _load_build() -> ModuleType:
    """Load scripts/docs_index_build.py under a PRIVATE sys.modules name.

    Same rationale as that module's own ``_load_chunker``: ``scripts/`` is
    not an installed package, and this module's own test loads THIS module
    by path via ``tests/conftest.py``'s ``load_module_from_path``, which
    registers under ``sys.modules`` -- a name private to this module never
    collides with that or with any other loader of ``docs_index_build``.

    IMPORTANT: this is the ONE place ``docs_index_build`` is loaded from
    here, and ``Unit``/``chunk_corpus``/``build_index``/``cache_db_path`` are
    all taken from THIS loaded copy, never re-imported independently -- a
    second independent load of ``docs_index_chunker.py`` would create a
    distinct ``Unit`` class object and break ``isinstance`` silently.
    """
    name = "_docs_index_query_build_impl"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parent / "docs_index_build.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_build = _load_build()


def _escape_query(raw: str) -> str:
    """Tokenize ``raw`` on whitespace and quote each term as an FTS5 phrase.

    See the module docstring for the measured failures this fixes and why.
    Returns the empty string for a raw query with no non-whitespace content
    (an all-whitespace or empty input has no terms to quote).

    NUL bytes are dropped first. Quoting cannot save them: sqlite3 binds a
    ``str`` as a C string, so a NUL anywhere in the MATCH argument truncates
    it mid-token and FTS5 raises ``unterminated string`` -- verified at
    technical review. Unreachable from argv (execve forbids NUL) but not from
    a library caller, so it is handled here rather than assumed away.
    """
    terms = raw.replace("\x00", "").split()
    return " ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _snippet(body: str) -> str:
    """A short, single-line preview of a unit's body -- never the whole unit."""
    flat = " ".join(body.split())
    if len(flat) <= _SNIPPET_CHARS:
        return flat
    return flat[:_SNIPPET_CHARS].rstrip() + "..."


def query(
    raw_query: str,
    doc_class: str | None = None,
    limit: int = 5,
) -> list[tuple[str, int, int, str, str]]:
    """Run ``raw_query`` against a freshly built index and return the top
    ``limit`` hits, ranked by FTS5's ``bm25()``, as ``(path, line_lo,
    line_hi, first_line, snippet)`` tuples. Never returns a whole unit body.
    """
    match = _escape_query(raw_query)
    if not match:
        return []

    conn = _build.build_index()
    try:
        sql = (
            "SELECT path, line_lo, line_hi, first_line, body FROM units "
            "WHERE units MATCH ?"
        )
        params: list[str | int] = [match]
        if doc_class is not None:
            sql += " AND doc_class = ?"
            params.append(doc_class)
        sql += " ORDER BY bm25(units) LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    return [
        (path, line_lo, line_hi, first_line, _snippet(body))
        for path, line_lo, line_hi, first_line, body in rows
    ]


@app.command(
    help=(
        "Find where something is written down in docs/, without reading all "
        "of docs/.\n\nRun this when you need the passage that settles a "
        "question -- a decision id, a term, a phrase. It prints ranked "
        "pointers (path:line_lo-line_hi), each with the unit's first line and "
        "a short snippet, and nothing else: read the cited range yourself. It "
        "never prints a whole unit and never writes prose of its own.\n\nThe "
        "index is rebuilt from docs/ on every run, so results are never "
        "stale."
    )
)
def main(
    text: Annotated[
        str,
        typer.Argument(help="Search terms -- a bd id, a phrase, anything."),
    ],
    doc_class: Annotated[
        str | None,
        typer.Option(
            "--class",
            help="Restrict to one doc class: decision-record or reference/process.",
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            min=1,
            help="Maximum number of ranked pointers to print.",
        ),
    ] = 5,
) -> None:
    """Query the docs/ lookup index and print ranked pointers, never answers.

    Rebuilds the index fresh from docs/*.md on every call (no cache), then
    prints up to --limit results as `path:line_lo-line_hi` + the unit's
    first line + a short snippet. Read the cited range yourself -- this
    tool never prints a whole unit and never synthesizes prose.
    """
    if doc_class is not None and doc_class not in _DOC_CLASSES:
        print(
            f"error: --class must be one of {', '.join(_DOC_CLASSES)}, got {doc_class!r}",
            file=sys.stderr,
        )
        raise typer.Exit(1)

    results = query(text, doc_class=doc_class, limit=limit)
    if not results:
        print("No results.")
        return

    for path, line_lo, line_hi, first_line, snippet in results:
        print(f"{path}:{line_lo}-{line_hi}  {first_line}")
        print(f"    {snippet}")


if __name__ == "__main__":
    app()
