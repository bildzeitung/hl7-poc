# Coding conventions

Prescriptive style **fiats** — the "because the maintainer said so" rules for writing code in this
repo. Unlike the design docs elsewhere under [`docs/`](design.md), the rules here carry **no
independent rationale** and don't need one to bind: they are a maintainer's call, not a reasoned
consequence of the architecture.

The litmus that keeps this file honest: **if a rule *does* earn a "why anyone would reach for it on
their own," it has stopped being a fiat** and belongs in the relevant design doc, not here.

This file is `@import`ed by [`CLAUDE.md`](../CLAUDE.md), so its contents are mechanically inlined
into the main session **and** every non-fork subagent — the `coding` producer, the `code-reviewer`,
the `land-review` agent, and the `/land` session that dispatches it. One source; the write-side and
the review-side read the same bytes, so a fiat here cannot drift between who writes the code and who
gates it.

**Keep it tight — one short section per rule.**

---

## One Screen or custom Widget per module

Every `Screen` subclass and every custom `Widget` subclass lives in **its own module file** — one
top-level UI class per `.py`.

Does **not** apply to inline helper widgets: a small, one-off subclass defined next to its sole
caller and used nowhere else may stay in that caller's module. The line is reuse and standing — a
screen, or a widget any other module imports, gets its own file; a private helper that exists only to
serve one parent does not.

## Typer, never argparse

Every Python CLI in this repo is built with **Typer**. Never `argparse`.

Every option and argument uses the `Annotated` form — `x: Annotated[Path | None, typer.Option(...)]
= None` — never a bare `typer.Option(...)` in the default-argument position. This holds for *every*
parameter, including the `bool`/`str` ones the linter does not flag; the lint's own blind spot is
what made the file inconsistent in the first place.

## Comments: state a constraint, invariant, or "why" — never narrate the edit

A comment earns its place only by stating a constraint, invariant, or *why* that the code itself
cannot show. Contrastive anchor: `# increment counter` → delete (the code already says this); `#
counter is 1-based to match the wire protocol` → keep (the code can't say this on its own).

**Change-narration comments are forbidden.** A comment that describes the *edit* rather than the
code — "now uses X", "moved from Y", "changed to handle Z" — belongs in the commit message, not the
source.

**Exemptions (the single source — anything auditing comments in this repo reads this list, and
never keeps its own copy):** license headers; docstrings serving an API/help contract; lint
directives (`# noqa`, `# fmt: skip`); a TODO/FIXME carrying a live bd id; shebang/encoding lines;
comments inside vendored or generated files.

## Derive identifiers, never retype them

A long opaque identifier — a full git SHA, a ticket id, a `.claude/worktrees/` hash — is never
hand-typed from a shorter prefix or from memory. Always derive it mechanically: `$(git rev-parse
<ref>)` for a commit SHA, `bd show <id>` for a ticket id, the actual path on disk for a worktree
hash.

A `PreToolUse(Bash)` hook (`scripts/sha-fabrication-guard.sh`) backstops this for 40-hex git SHAs
only; the fiat covers every opaque identifier.
