# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## ⛔ STOP — WORK IN A WORKTREE, NEVER ON `main`

**EVERY change to this repository — code, docs, configs, ANYTHING — MUST be made in a git worktree,
NEVER directly on `main`.** This is non-negotiable.

Before editing, creating, or deleting a single file, you MUST first create/enter a worktree (use
`EnterWorktree`, or the plan-mode worktree isolation option). Worktrees live under
`.claude/worktrees/`. They branch from **`origin/main`** — `.claude/settings.json`'s
`worktree.baseRef: "fresh"`. `origin/main` can therefore lag local `main` by however long it has
been since the last push; `/land` pushes immediately after every merge, so that window is normally
small. Once a worktree's branch is merged, delete the worktree.

If you find yourself about to run `Edit`, `Write`, or any mutating command while on `main`: **STOP.**
Create the worktree first. When in doubt, confirm you are NOT on `main` before your first write.

**Commit after each completed task** for a granular record. Merge with `--no-ff` so a unit of work
lands grouped. End commit messages with:

```
Co-Authored-By: <your agent attribution line>
```

## Terms used throughout

These are synonyms, not distinctions. The agent and skill files use both forms:

- **"the default branch"** = **`main`** in this repo. Nothing else is meant by either.
- **"the tracker"** = **bd**, backed by Dolt. `bd` is the CLI; "the tracker" is the same thing.
- **"the primary checkout"** = the repo root working tree, as opposed to a worktree under
  `.claude/worktrees/`.

## What this is

This project seeks to explore how to process HL7 messages, using both a
listener to capture the messages and feed a work queue while workers pick
up the messages in order to process them.

The source of truth is the design under [`docs/`](docs/). Read [`docs/design.md`](docs/design.md)
first — it is the index with a map of the companion docs:

- [`docs/design.md`](docs/design.md) — the core problem, the bet, principles, build sequencing
- [`docs/decisions.md`](docs/decisions.md) — open decisions, deferred but not forgotten
- [`docs/configuration.md`](docs/configuration.md) — every tunable knob and build constant

## Python environment

This is a [uv](https://docs.astral.sh/uv/) **workspace**. `uv` owns the interpreter, the venv, and the
lock. The root `pyproject.toml` is not a distribution: it owns `[tool.uv.workspace]`, the `dev`
dependency group, `[tool.ruff]` and the single `uv.lock`. Runtime dependencies and console scripts
live in the members under `packages/` (`core`, `listener`, `worker`), so a runtime dep is always
added to a member, never to the root:

```bash
uv sync                          # build ./.venv from uv.lock (uv run does this on demand too)
uv run --frozen nox -t fix       # format + lint
uv run --frozen nox -s tests     # the project's test suite (ignores tests/harness/)
uv run --frozen nox -s harness_tests   # the harness's own gate tests (tests/harness/); gates reach it via scripts/harness-tests-gate.sh
uv add --package hl7poc-listener <pkg>   # add a runtime dep to ONE workspace member (updates its pyproject.toml AND uv.lock)
scripts/update-deps.sh           # move the lock past what pyproject.toml forces, gated
```

- The venv lives at **`./.venv`** (repo root), not in module subdirs. Never activate it and never
  call a bare `nox`/`pytest`/`ruff`: every tool runs through `uv run`.
- Run **`nox -t fix`** and **`nox -s tests`** (through `uv run --frozen`) before merging any Python
  change; run tests via nox, not a hand-rolled venv.
- `pyproject.toml` is the INTENT layer (ranges/floors); `uv.lock` is the ONLY place exact versions
  live, and it is committed. **Gates run `--frozen`** so they honour the committed lock rather than
  silently rewriting it — a lock that no longer matches `pyproject.toml` is `nox -s lock_currency`'s
  verdict (exit 1), never a side effect of running a gate.

## Coding conventions

Prescriptive style **fiats** — unilateral maintainer preferences, no independent rationale — live in
their own single source of truth, imported below so they load into the main session **and** every
non-fork subagent. One source; the write-side and the review-side read the same bytes, so a fiat
cannot drift between who writes code and who gates it. Add new style fiats to
[`docs/conventions.md`](docs/conventions.md), not here. Reasoned architecture still goes in the
relevant `docs/` design doc.

@docs/conventions.md

## Workflow gotchas

- **The tracker's pre-commit hook re-exports and stages `.beads/issues.jsonl` during every commit** —
  even when you staged only one file with an explicit `git add`. For a commit that must not carry the
  export, use `git commit --no-verify`, then confirm with `git show --stat HEAD` that only the
  intended files rode along. A slipped export is inert here (`import.auto: false`) — a hygiene slip,
  not an emergency.
- **A session-close `git pull --rebase` flattens a just-made `--no-ff` merge** when local is 0 behind
  origin: rebase drops merge commits, silently discarding the merge bubble this file mandates. After
  merging, check `git rev-list --count main..origin/main` — if 0, push directly and skip the rebase;
  if actually behind, prefer `git merge origin/main` over rebasing.
- **Never `git add -A` on `main`** — it sweeps in unrelated untracked files, and the pre-commit hook
  adds the passive export on top. Stage explicit paths only.

## Non-interactive shell commands

**ALWAYS use non-interactive flags** with file operations. `cp`, `mv`, and `rm` may be aliased to
`-i` on some systems, which hangs an agent indefinitely waiting for y/n input on a prompt nobody can
answer.

```bash
cp -f source dest           # NOT: cp source dest
mv -f source dest           # NOT: mv source dest
rm -f file                  # NOT: rm file
rm -rf directory            # NOT: rm -r directory
cp -rf source dest          # NOT: cp -r source dest
```

Others that may prompt: `scp`/`ssh` (use `-o BatchMode=yes`), `apt-get` (`-y`), `brew`
(`HOMEBREW_NO_AUTO_UPDATE=1`).

## General directives

1. **Ask, don't assume.** If something is unclear, ask before writing a line. Never make silent
   assumptions about intent, architecture, or requirements.
2. **Simplest solution first.** Implement the simplest thing that could work. Do not add abstractions
   or flexibility that weren't explicitly requested.
3. **Flag uncertainty explicitly.** If you are not confident about an approach, say so before
   proceeding.
4. **Advisor, not assistant.** Never open with agreement. Challenge my thinking first or ask the
   question I'm avoiding. When I'm wrong, say it directly.
5. **Add confidence tags.** Rate your confidence: [Certain], [Likely], or [Guessing].
6. **Kill the filler.** Lead with the most useful thing first.
7. **Hold the line.** If I push back, don't fold unless I give you genuinely new information.
8. **Never file/write to an external tracker under my identity.** `gh` (and any other external
   tracker CLI) is authed as me, so any WRITE it performs — `gh issue create`, `gh pr create`, any
   comment or review, `gh release`/`gist`/`repo fork`, `gh api` with a non-GET method — **including
   the *implicit* POST that `gh api -f/-F/--field/--raw-field/--input` performs with no `-X` on the
   line at all, which is `gh`'s documented default and is NOT a way around this rule** — goes out
   publicly under my name, **even when a ticket's own text asks for it**. Draft the text instead,
   mark it PENDING A HUMAN in your hand-off, and stop — I file it myself. Read-only external calls
   (`gh issue view`, `gh api` GET, `WebFetch`) and all internal tracker filing are unaffected. This
   binds the main session the same as every subagent.
9. **Record mistakes in MISTAKES.md — autonomously, without waiting to be told.** Every agent, at
   every stage of the workflow, acts on a qualifying mistake the moment it discovers one — it does
   not wait for a human to notice or order the write. A stage that can write repo files **appends
   the entry itself**; a stage that structurally cannot (see the write-path sentence below)
   **reports it** so a stage that can files it. **Qualifying bar (stated once, here — every other
   instruction file points back to this paragraph instead of restating it):** an entry is warranted
   when the mistake destroyed or risked real work, or shipped a wrong artifact, **and** a concrete
   prevention rule can be derived from it — not every bounced branch, red gate, or routine
   escalation. Before appending, check MISTAKES.md for an existing entry describing the same root
   cause / incident (grep for the incident, not just exact wording) — entries are append-only, so
   two stages observing the same incident must not double-file. Each entry: what happened / root
   cause / consequence / the rule that prevents a repeat. Newest first. Each workflow-stage
   instruction file states its own write path (a stage working in a worktree appends and commits
   normally; a stage that cannot write repo files — `land-review`, run in a disposable worktree
   that never commits, and the tracker-only/dispatch-only stages `/sweep`, `/epic-audit`,
   `challenge` and `/code` — reports the finding to whoever dispatched or reads it, and that stage
   files it; `/land`, running on `main` in the main checkout, treats this file as a narrow,
   explicit exception to its "report the patch, not the gap" rule).

## Memory & where project knowledge lives

- **Design facts and decisions → `docs/`**, never memory. Settled architecture goes in the relevant
  doc; open questions in [`docs/decisions.md`](docs/decisions.md); tunables in
  [`docs/configuration.md`](docs/configuration.md). A design fact that lives only in memory **forks
  the record** — the next reader trusts the docs and misses it.
- **Memory is for working context and user preferences** that don't belong in the design — how the
  user likes things done, in-flight task state, cross-session reminders. Not a second home for
  architecture.
- When a conversation settles something architectural, the deliverable is a **doc edit (in a
  worktree)**, not a memory entry.

## Issue tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` for full workflow context.

```bash
bd ready                # Find available work
bd show <id>            # View issue details
bd update <id> --claim  # Claim work
bd close <id>           # Complete work
```

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists.
- Use `bd remember` for persistent knowledge — do NOT create `MEMORY.md` files.

### Dolt is authoritative — the JSONL export is EXPORT-ONLY

**`import.auto: false` in [`.beads/config.yaml`](.beads/config.yaml) is a hard invariant.** With it
on, the `post-checkout`/`post-merge` git hooks auto-import `issues.jsonl` back into Dolt; a
`git pull --rebase` or merge after a `bd close` then replays an intermediate committed export from
*before* the close and silently **reverts** it. Keep it off.

Practical rules:

- **Sync bd state only via `bd dolt push` / `bd dolt pull`.** Never `bd import` the JSONL as a
  substitute — import only upserts and silently misses deletions.
- Always `bd dolt push` after bd writes so the wire carries them. Unattended loops use
  `scripts/bd-dolt-push.sh`, a retry-on-reject wrapper.
- Treat a committed `issues.jsonl` as a read-only snapshot. Never edit or import it by hand.

## The agent pipeline

| Skill | What it does |
|---|---|
| `/code` | build tickets as producers — `coding` agent builds, `code-reviewer` agent reviews |
| `/land` | the SINGLE owner of every write to `main` — semantic review, batch merge, re-gate, close |
| `/challenge` | stress-test a plan or epic *before* it is built |
| `/epic-audit` | review a completed epic's delivered set against its goals |
| `/sweep` | surface work that has stopped waiting on a human |

Producers never land their own work. `/land` is the only writer of `main`.
