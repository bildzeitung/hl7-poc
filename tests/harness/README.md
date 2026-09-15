# Harness gate tests

These are the tests that keep the harness's own mechanisms honest. They are **not** application
tests — they have zero dependency on any project code, so they run in a fresh repo unchanged.
Your own tests go in `tests/`; `nox -s tests` collects everything except this directory.

```bash
uv run --frozen nox -s harness_tests
```

## When they run

Not on every gate. Their verdict can only change when a harness path changes — `scripts/`,
`.claude/`, `tests/harness/`, `noxfile.py`, `pyproject.toml`, `uv.lock` — so the producer gates,
`/land`'s re-gate and the isolation replay all reach them through `scripts/harness-tests-gate.sh`,
which diffs the working tree against the merge-base and runs `nox -s harness_tests` only when one
of those paths differs. A branch that never touches the harness pays one line of output. The
installer and `harness-doctor.sh` point at the session whole.

## What they gate

| Kind | Examples | Why it can't be prose |
|---|---|---|
| **Guard-script behaviour** | `test_isolation_guard`, `test_recycled_worktree_guard`, `test_land_lock`, `test_land_heartbeat`, `test_merge_precheck`, `test_worktree_gc_classify` | fixture-backed runs against real git repos — the destructive paths are exactly where a regression is unrecoverable |
| **Skill-markdown scanners** | `test_skill_bash_state` (no cross-block shell state), `test_bd_list_limit_gate` (`--limit 0` on every tracker query), `test_land_skill_guard_coverage` (every mutating fence is guarded) | the skills' bash is *executed* by an agent but lives in markdown, where no linter reaches |
| **Hook wiring** | `test_gh_write_guard`, `test_bd_deps_guard` | the hooks are JSON strings in `settings.json`; nothing else type-checks them |
| **Cross-file invariants** | `test_validate_sha40_call_sites` (the two fenced callers still call the validator) | they pin that two files agree — the exact thing that drifts silently |
| **The gate on this suite** | `test_harness_tests_gate` | the skip/run decision and its 0/1/2 contract, against real repos |

## They are pins, and pins go stale on purpose

Several assert on **exact strings** in the skill markdown. That is deliberate: a reworded gate is a
gate you no longer have. When you legitimately change a skill, the corresponding test fails and you
update it **deliberately** — that failure is the review prompt, not noise.

**They pin the fenced bash, not the prose around it.** Only two modules read narrative text at all.
So rewriting explanation is free; changing a command, a guard, or a state-load call is what trips a
gate — which is the correct scope, not gratuitous brittleness.

Two carry allowlists (`test_bd_list_limit_gate`'s prose-skip entries,
`test_land_skill_guard_coverage`'s mutating-command exemptions). Each demands its entries still
match something live, so an exemption that stops applying fails rather than silently exempting
nothing.

## Do I need all of them?

The suite is 36 modules / 940 tests / under a minute with 8 workers — and, through the gate
script, it costs an ordinary project branch nothing. The test *count* is mostly parametrisation —
`test_gh_write_guard` alone contributes 335 cases, one per `gh` command form, in about ten
seconds. Count is not cost.

Every module covers a script or skill the harness actually invokes; there is no dead weight to
delete. Six modules that only gate the harness's *own* development (suite DRY-ness, drift between
frozen harness files, the doctor, tooling no skill invokes) already stayed behind in the export's
`dev-tests/`. What is genuinely optional here is **whatever gates a skill you don't use**. The
modules are independent, so deleting a file is safe:

| Drop if you… | Modules |
|---|---|
| don't use epics | `test_epic_*` (3) |
| don't run `/sweep` | `test_sweep_*` (5) |
| never fan out `/code` | `test_code_concurrency_cap` |

**Keep regardless**, whatever else you drop — these gate code that deletes worktrees, resets
branches, force-removes refs, or spends the user's public identity, where a regression is
unrecoverable rather than merely wrong:

`test_land_lock`, `test_recycled_worktree_guard`, `test_isolation_guard`, `test_worktree_gc_classify`,
`test_worktree_gc_sweep`, `test_worktree_lock_stale`, `test_land_merge_one`, `test_land_merge_batch`,
`test_land_replay`, `test_merge_precheck`, `test_land_state_load`, `test_gate_lib`,
`test_gh_write_guard`, `test_bd_deps_guard`, `test_validate_sha40*`, `test_blocks_dependents`,
`test_harness_tests_gate`.

The **markdown scanners** (`test_skill_bash_state`, `test_bd_list_limit_gate`,
`test_land_skill_guard_coverage`, `test_land_conflicts_state`) look like the obvious cut and are the
worst one to make: they are the only thing checking the bash *inside* the skills, which no linter
reaches, and they caught four real defects when this harness was first ported.

## Shared helpers

- `conftest.py` — the markdown fence parser and corpus locators. The fence rules live here and
  nowhere else; several gates key on them. Every module's `REPO_ROOT` is three levels up
  (`tests/harness/<module>` → repo root).
- `_fence_parsing.py` — the fence-marker primitives.
- `_gitrepo.py` — real-git-repo helpers: `_git`, plus `_branch_from` and `_commit_file` hoisted from the test modules that had each grown a private copy.
- `_hookharness.py` — locates and runs a `PreToolUse` hook out of `settings.json`. It finds a hook by
  **script name** (e.g. `gh-write-guard.sh`), so renaming a guard script is a deliberate, failing
  change rather than a silent miss.
