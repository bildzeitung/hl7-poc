"""Build the on-demand SQLite FTS5 index over ``docs/*.md``.

Takes the chunker's units
and loads them into an FTS5 virtual table, per the decided shape recorded in
``docs/decisions.md``. This module is *only* the build step; the query CLI is
``an earlier fix``.

Two structural constraints, both settled and non-negotiable here:

- **stdlib ``sqlite3`` FTS5 only** -- no new dependency, and this module does
  not import or depend on harness's own embedding/FTS retrieval pipeline
  (``src/harness/retrieval.py``, ``embedding.py``, ``vectorstore.py``). See the
  epic's SETTLED item 4: pointing harness's own retrieval at harness's own design
  record would make "what did we decide about retrieval" unanswerable
  precisely when retrieval is broken or mid-refactor.
- **Full rebuild on every invocation, no cache.** Measured 26-35 ms for the
  whole 1.26 MB corpus (329 units) during the epic's ``/challenge`` -- see
  :func:`build_index`'s docstring for a fresh measurement recorded on this
  ticket. The trigger to revisit is ~500 ms, not the originally-stated 2 s
  (60x current, would never fire).

The build target is a temp/XDG cache dir OUTSIDE the repository worktree --
one of the two mechanisms enforcing the never-tracked constraint. :func:`cache_db_path`
resolves it with the stdlib alone: ``$XDG_CACHE_HOME/harness/docs-index.sqlite3``
if set, else ``~/.cache/harness/docs-index.sqlite3`` -- the same fallback
``platformdirs`` would compute on Linux/macOS, without adding a dependency
that is today only a transitive pull-in of ``fastembed`` (i.e. of the very
retrieval pipeline this module must stay independent of).
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
from pathlib import Path
from types import ModuleType


def _load_chunker() -> ModuleType:
    """Load scripts/docs_index_chunker.py under a PRIVATE sys.modules name.

    scripts/ is not an installed package, and this module's own test loads
    THIS module by path via tests/conftest.py's load_module_from_path, which
    registers under sys.modules -- and independently,
    tests/test_docs_index_chunker.py does the same for the chunker itself,
    under the name "docs_index_chunker". That helper's own docstring warns
    the registration is permanent for the session and a second load under
    the same name asserts loudly. A plain `import docs_index_chunker`
    here (even with scripts/ on sys.path) would register that exact name
    too -- and whichever of the two test files collects second would then
    hit that assert, non-deterministically, since pytest collection order
    is what decides which one "wins" the name first. Loading under a
    name private to this module (never chosen by any test or CLI) sidesteps
    the collision entirely, at the cost of a harmless second copy of the
    chunker module object if both are loaded in the same session.
    """
    name = "_docs_index_build_chunker_impl"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parent / "docs_index_chunker.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_chunker = _load_chunker()
Unit = _chunker.Unit
chunk_corpus = _chunker.chunk_corpus

#: Default corpus dir, exported so the future query CLI doesn't
#: hand-roll the same `Path(__file__).resolve.parent.parent / "docs"`.
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOCS_DIR = REPO_ROOT / "docs"

_SCHEMA = (
    "CREATE VIRTUAL TABLE units USING fts5("
    "path, line_lo UNINDEXED, line_hi UNINDEXED, first_line, body, doc_class UNINDEXED"
    ")"
)


def cache_db_path() -> Path:
    """The on-disk index location: always OUTSIDE the repo worktree.

    ``$XDG_CACHE_HOME/harness/docs-index.sqlite3`` if ``XDG_CACHE_HOME`` is set
    to an ABSOLUTE path, else ``~/.cache/harness/docs-index.sqlite3`` -- the
    standard XDG Base Directory fallback. Neither path can resolve inside a git
    worktree under normal operation, which is the structural half of the
    never-tracked constraint.

    The absoluteness check is not pedantry about the spec (which does say a
    non-absolute value must be ignored): a relative ``XDG_CACHE_HOME`` is
    resolved against the *current* directory, so for a process running in the
    checkout it would place the index INSIDE the worktree -- defeating the
    structural half of the constraint this function exists to provide.
    """
    xdg_cache_home = Path(os.environ.get("XDG_CACHE_HOME") or "")
    base = xdg_cache_home if xdg_cache_home.is_absolute() else Path.home() / ".cache"
    return base / "harness" / "docs-index.sqlite3"


def build_index(
    docs_dir: Path | str = DEFAULT_DOCS_DIR, db_path: Path | None = None
) -> sqlite3.Connection:
    """Chunk ``docs_dir`` and load the units into a fresh FTS5 index.

    No cache: any existing file at ``db_path`` (default: :func:`cache_db_path`)
    is removed first and the index is rebuilt from scratch on every call --
    the decided freshness answer, since a
    full rebuild is cheap (measured below) and staleness is a worse failure
    mode than a few extra milliseconds.

    MEASURED: 5 repeated on-disk builds via this
    function, over the real docs/ corpus, on a dev machine: 83-118 ms
    (mean ~95 ms). Higher than the epic's 26-35 ms /challenge figure -- that
    number covered chunk+in-process-FTS5-insert only, whereas this also pays
    for a fresh on-disk sqlite file's mkdir/unlink/open/commit I/O each call,
    per the never-tracked constraint's OUTSIDE-the-worktree cache target.
    Still nothing to optimize: well under the ~500 ms revisit trigger
    (docs/decisions.md), not the originally-stated 2 s figure.

    Returns an open connection to the built database; the caller queries and
    closes it.
    """
    resolved_db_path = db_path if db_path is not None else cache_db_path()
    resolved_db_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_db_path.unlink(missing_ok=True)

    conn = sqlite3.connect(resolved_db_path)
    try:
        conn.execute(_SCHEMA)
        units: list[Unit] = chunk_corpus(docs_dir)
        conn.executemany(
            "INSERT INTO units (path, line_lo, line_hi, first_line, body, doc_class) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                (u.path, u.line_lo, u.line_hi, u.first_line, u.body, u.doc_class)
                for u in units
            ),
        )
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn
