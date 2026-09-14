#!/usr/bin/env python3
"""Derive the CI model-weights cache key from harness's pinned ``Settings``.

SINGLE implementation of the "Resolve model cache key" step shared by
``.github/workflows/tests.yml`` and ``.github/workflows/coverage.yml`` --
before this script existed, both workflows carried a byte-identical Python
heredoc doing the same thing, which is a lockstep-maintenance hazard: adding
a fourth fastembed model, renaming a ``Settings`` field, or changing the key
format required editing both files identically, and missing one would key
the two workflows against different cache scopes (one serving stale weights
for a pin it is no longer testing). Matches the precedent already used
elsewhere in this repo for exactly this class of duplication -- the ``build``
nox session is the SINGLE implementation both ``build.yml`` and
``release.yml`` call (see ``noxfile.py``).

Derives the key from the PINNED model ids rather than a lockfile or a bare
constant, so changing any of them invalidates the cache -- CI must not
silently keep serving stale weights for a pin it is no longer testing. Reads
the ids off ``Settings`` itself rather than regex-scraping ``config.py``'s
source, so the key tracks ``config.py``'s API instead of its formatting (a
refactor that changes no behaviour, e.g. hoisting a default to a module
constant, can't red the calling workflow). Bare ``Settings()`` is a plain
pydantic model (not ``BaseSettings``), so it yields the pinned defaults and
never layers in env vars or a ``$HARNESS_HOME/config.toml`` -- exactly the pins
under test. All three fastembed loaders are named explicitly (embedder,
reranker, NLI/entailment cross-encoder): ``entailment_model`` happens to
share ``rerank_model``'s id today, but naming it here means the key stays
correct on its own terms if that ever diverges, rather than resting on an
invariant asserted in another file.

Must run AFTER the workflow's dependency install step (it imports ``harness``)
and BEFORE the cache restore/save step. Writes a single ``key=...`` line
suitable for direct redirection into ``$GITHUB_OUTPUT``::

    python scripts/model-cache-key.py >> "$GITHUB_OUTPUT"
"""

from __future__ import annotations

from harness.config import Settings


def cache_key() -> str:
    s = Settings()
    ids = f"{s.embedding_model}-{s.rerank_model}-{s.entailment_model}"
    return "models-" + ids.replace("/", "_")


if __name__ == "__main__":
    print(f"key={cache_key()}")
