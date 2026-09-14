---
name: comment-audit
description: Sequence a comment quality pass on project source — comment-auditor (report-only findings) → human veto seam → comment-groomer (applies surviving findings, comment-lines-only). Thin orchestrator; the taxonomy and rubric live solely in the agent files. Examples — "/comment-audit" (current branch's diff), "/comment-audit land/proj-abc", "/comment-audit src/", "/comment-audit docs/".
---

# comment-audit

I am a thin dispatch skill, modeled on `/code`'s pattern: I **sequence** two existing agents — I
never audit or groom a comment myself, and the two agents never call each other directly.

1. **comment-auditor** (report-only, stronger model) — produces a findings block.
2. **A veto seam** — a human strikes false positives / adjusts actions, or (unattended) a fixed
   threshold does.
3. **comment-groomer** (worktree, cheaper model) — applies only the findings that survived the
   seam.

The taxonomy, smell list, and rubric are **not restated here** — they live solely in
[`comment-auditor.md`](../../agents/comment-auditor.md) and
[`comment-groomer.md`](../../agents/comment-groomer.md), which both already carry the
`docs/conventions.md` Comments fiat via `CLAUDE.md`'s `@import`. I would only fork the record by
copying any of it into this file.

## 1. Parse the target

- **`/comment-audit <branch>`** — a branch target (e.g. `land/proj-abc`, or any local/remote ref).
  Audit is **diff-scoped**: `main...<branch>` hunks, plus comments adjacent to the changed code,
  per the auditor's own drift rule. This is the common case — a branch already in flight through
  the normal producer/reviewer cycle.
- **`/comment-audit <path>`** — a path target (a file or a directory tree). Audit is a **whole-tree
  sweep** over everything under that path, not diff-scoped.
- **Bare `/comment-audit`** — defaults to the **current branch's diff** against `main`, same
  scoping as the branch-target case above.

An argument that resolves as **both** a ref and an existing path is genuinely ambiguous — ask. If
only one of the two resolves, take that one without asking.

## 2. Dispatch comment-auditor, surface its findings verbatim

Dispatch `subagent_type: "comment-auditor"` at the resolved target (branch diff or path). No
call-site `isolation` option — its own frontmatter (`isolation: worktree`) is sufficient, same
convention as every other agent dispatch in this repo.

When it returns, **relay its full findings block to the user verbatim** — file:line anchors,
taxonomy classification, and suggested action per finding, unedited.

If the auditor reports zero findings, say so plainly and stop — there is nothing for the groomer
to do.

## 3. The veto seam — non-negotiable

I **never** hand the auditor's full findings list straight to the groomer. Every invocation stops
here for a disposition pass before grooming proceeds:

- **Interactive (a human is available):** present the findings block and let the human strike
  false positives and adjust suggested actions, finding by finding. What remains after the human's
  edits is the surviving set.
- **Autonomous (no user available to respond — e.g. a `/loop` invocation):** apply a fixed
  threshold — only findings at **high severity AND confidence ≥ 0.9** survive automatically. Every
  finding below that bar is **not applied**; it is reported as pending a human, the same shape as a
  build-time escalation elsewhere in this repo (surfaced, never silently dropped or silently
  applied).

The threshold value is tunable by whoever operates the autonomous path; that *something* always
stands between the auditor's raw output and the groomer's input is not. A run that skips this step
has skipped the whole point of splitting audit from apply.

If the surviving set is empty (everything vetoed, or nothing cleared the autonomous threshold),
say so and stop — do not dispatch the groomer over nothing.

## 4. Dispatch comment-groomer with the surviving findings only

Dispatch `subagent_type: "comment-groomer"`, passing exactly the surviving findings block (not the
auditor's original output) and the same target it was scoped against. Its own non-negotiables —
comment-lines-only diff, the fiat's untouchable exemptions, re-verifying each finding itself
before applying, re-gating with `uv run --frozen nox -t fix` + `uv run --frozen nox -s tests`, committing as
its own commit — are unchanged by being dispatched through me; I add nothing to its contract.

**The checkout and the push are the groomer's own contract** (`comment-groomer.md`) — it writes no
bd state either way. What's left mine to carry in the dispatch prompt is only *where the work goes*
and *whose job the hand-off is*, not the checkout or push mechanics themselves.

- **Branch target (including the bare/current-branch case):** the branch is already an in-flight
  `land/<id>` ticket somewhere in the normal producer → reviewer → lander pipeline. I name that
  branch in the dispatch prompt; the groomer fetches it, checks it out, and pushes back on its own.
  Its **label stays untouched**: that branch's `ready-for-code-review`/`ready-for-land` state is owned
  by whichever stage is already driving it, not by this sweep.

  Timing caveat worth stating: appending a commit to a branch already sitting at `ready-for-land`
  moves the tip away from the `land_head` the reviewer recorded, and `/land`'s drift precheck reads
  that as **drift**. Prefer grooming a branch before its technical review; if one is already
  `ready-for-land`, expect the drift bounce and say so in the report.
- **Path target (a whole-tree sweep with no branch already in flight):** there is no existing ticket
  to fold into — this is new, standalone work. I file a bd issue for the sweep first (`bd create
  --type=task --title="comment-audit sweep: <path>" --description="…"`), then dispatch the groomer
  with that id as the branch to push its sweep to. Because the groomer
  writes no bd state, **I** do the hand-off once it reports its head SHA: set `review_head` to that
  SHA and add **`ready-for-code-review`**, so the normal `code-reviewer` → `/land` pipeline picks it
  up. I never mark it `ready-for-land` and never touch the default branch myself.

Either way, **default-branch writes stay `/land`'s alone** — nothing in this skill merges, closes a
ticket, or pushes `main`.

## 5. Report

Close every run with:

- What was **applied** (the groomer's own commit, and which findings it covered).
- What was **skipped or reverted** by the groomer itself (a finding it couldn't re-confirm against
  the code, or a hunk that touched executable bytes).
- What is still **pending a human** per section 3 — every finding vetoed in an interactive run, or
  that missed the autonomous threshold.

## Constraints

- **Thin wrapper only.** I carry no audit logic, no taxonomy, no rubric of my own — see the top of
  this file. If a rule needs stating twice, it belongs in the agent files, not here.
- **Concurrency.** `/code`'s cap is one shared budget **within a single invocation** — a separate
  `/comment-audit` run draws from nothing and is throttled by nothing, so the two stack rather than
  share. I dispatch at most one agent at a time anyway, but don't start a sweep on top of a running
  `/code` fan-out without accounting for the extra agent yourself.
- **No self-review.** comment-auditor and comment-groomer never call each other, and I never
  apply a finding myself outside the groomer's own mechanical verification.
