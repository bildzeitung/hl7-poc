---
name: code
description: Build one or more tasks as PRODUCERS in two phases — dispatch the `coding` subagent to claim a bd issue, build in an isolated worktree, pass the quality gates, push its branch to origin, and hand off at ready-for-code-review; then dispatch the `code-reviewer` subagent to fetch that branch into its own launch worktree, run the technical review (its own hand-reasoned correctness pass, plus /simplify), re-gate, and swap the ticket to ready-for-land. Producers never merge/close/push the default branch; a separate /land lander does. Every invocation also sweeps for needs-rebase tickets first (branches /land kicked back on a conflict) and dispatches a coding producer to merge the default branch in, re-gate, and push the result itself — an ordinary, non-force push — swapping each straight back to ready-for-land. `/code <id>` (or `/code --single`) is one producer; bare `/code` / `/code --all-ready` / `/code <id> <id> …` fans out N parallel producers across the ready frontier, throttled to a shared concurrency cap. Examples — "/code", "/code abc-1 abc-2", "/code abc-123", "/code --single", "/code add a --json flag to the search CLI".
---

# code

`/code` is the **sole producer entry point**, and it runs each task in **two dispatched phases**:

1. **Build — `coding` subagent (cheaper model):** claim → worktree → working code → green gates →
   branch pushed to `origin/land/<id>` → ticket marked `ready-for-code-review` → keep the worktree →
   stop. The builder does **not** review its own work.
2. **Technical review — `code-reviewer` subagent (stronger model):** fetches the pushed `land/<id>`
   branch into its own launch worktree, runs its own reasoned **correctness pass** against the diff
   plus the tool-backed **`/simplify`**, re-gates, re-pushes, and swaps the ticket to
   **`ready-for-land`** (or escalates).

Splitting build from review means the technical review is done by an agent that didn't write the
code — and the lander's later semantic review is too, so *neither* review of a branch is its
author's. A producer **never** merges, closes the ticket, pushes the default branch, or writes the
primary checkout.

There is **no separate parallel variant.** Once landing left the producer, building one task and
building five became the same act: a producer just leaves a green branch on origin, and a *new*
branch ref doesn't race the default branch, so N producers run safely in parallel *within one
invocation*.

- **bare `/code`**, **`/code --all-ready`**, or **`/code <id> <id> …`** — **N producers in parallel**,
  each in its own worktree. Bare `/code` is the default: fan out across the whole unblocked ready
  frontier.
- **`/code <id>`** or **`/code --single`** — one producer.

Both subagents already own *how* producer work flows. This skill's only job is to launch them
correctly, **in order, build then review**, and relay what came back.

## What to do when invoked

> **Topology — run only one `/code` invocation at a time.** Fan-out *within* one invocation is safe
> and encouraged. Concurrent *invocations* race: steps 0 and 1 both select on a label that is only
> swapped at the **end** of the dispatched agent's work, so invocation B's sweep can select a ticket
> whose agent from invocation A is still live and dispatch a second agent at it. Need more
> parallelism? Pass more IDs to the **same** invocation.

> **Concurrency cap.** Each agent's gate runs the full test suite; enough of them at once takes the
> host down. **Before step 0**, compute the cap once and hold it for the invocation:
>
> ```bash
> REPO_ROOT="$(git rev-parse --show-toplevel)"
> CODE_MAX_CONCURRENT_AGENTS="$("$REPO_ROOT/scripts/code-concurrency-cap.sh")" || CODE_MAX_CONCURRENT_AGENTS=4
> ```
>
> The `||` fires on **exit status, not emptiness** — a `VAR="$(cmd)"` assignment inherits its
> substitution's status, so a missing script trips it whatever stdout held. **Never proceed on an
> empty or non-numeric cap**; say so instead. `CODE_MAX_CONCURRENT_AGENTS` in the environment wins
> outright when set. The script is the source of truth for the derivation.
>
> **It is ONE budget across every dispatch in this invocation** — step 0's pickups, step 1's
> re-entries, Phase 1 builders, Phase 2 reviewers. Builders and reviewers are not separate pools.
> Track in-flight agents; **queue** a dispatch that would exceed the cap and release it as a slot
> frees.

### 0. Sweep for `needs-rebase` kick-backs — every invocation, regardless of argument

`/land`'s cheap conflict precheck can kick a `ready-for-land` branch back: it strips
`ready-for-land`, adds **`needs-rebase`**, and keeps the same `land/<id>` branch. The ticket stays
`in_progress`, so it never surfaces in `bd ready` and nothing else consumes the label.

```bash
bd list --label needs-rebase --status in_progress --limit 0 --json
```

**`--limit 0` is load-bearing, not noise.** Without it the read silently truncates at the default
page size and strands kick-backs past that point, every invocation.

For **each** hit, dispatch a `coding` producer (`subagent_type: "coding"`, **no call-site
`isolation` option** — see Phase 1). Tell it explicitly this is a **rebase pickup**, not a fresh
build:

> `<id>` carries `needs-rebase` (kicked back by `/land`'s conflict precheck) — fetch it and **merge
> the current default branch in** (do not rebase): `git fetch origin land/<id> main`, then
> `TOP=$(git rev-parse --show-toplevel)` and `git checkout -B "land/<id>--${TOP##*/}" FETCH_HEAD` (a
> local name suffixed with your own worktree's directory — unique by construction), `git merge
> origin/main`, re-gate, commit anything the gate loop produced, then `git push origin HEAD:land/<id>`
> (an ordinary push by explicit refspec — the merge only appends, it never rewrites what's already
> there), refresh `land_head` (leaving `land_summary` untouched — it's the code-reviewer's account of
> the work, not of the rebase), and swap `needs-rebase` straight to `ready-for-land`
> yourself. Do not merge, close, or push the default branch. On a merge conflict: if both sides added
> independent, non-overlapping content (a **mechanical** conflict), resolve it directly with `Edit`
> and continue; if the two sides genuinely **disagree**, abort and escalate yourself
> (`land-escalated`, leave the branch as it was) rather than guess. Don't try to remove your own
> launch worktree — you can't remove the one you're standing in, and I reclaim it after you return.

Merging rather than rebasing is what keeps this cycle inside the one dispatched producer: a merge
commit appends, it never rewrites what `land/<id>` already carries on origin, so the push back is an
ordinary fast-forward. **Expect the merge to conflict** — a branch only carries `needs-rebase`
because it already failed a clean-merge precheck — and resolving it keeps the push a fast-forward
all the same.

Dispatch every hit **concurrently** with each other and with Phase 1 builds
(`run_in_background: true`), subject to the concurrency cap. **Do not dispatch a Phase 2 reviewer for
a rebase pickup**: the content already passed technical review; it only needed to replay onto where
the default branch moved. If the sweep finds nothing, say so and move on — not an error.

<a id="reclaim"></a>
**Reclaim its launch worktree the moment it returns — either outcome.** A subagent cannot
`git worktree remove` the worktree it is standing in, so **I** do it from my own repo-root context,
immediately after collecting its result — not batched to the end of the fan-out. **This block is the
only copy; steps 1 and Phase 2 refer back to it.** I don't need the agent to tell me which worktree
was its own: every reviewer and pickup checks the branch out as `land/<id>--<its-own-worktree-dir>`,
so the ticket id alone **derives** both path and branch:

```bash
ID=<the ticket I just dispatched at>
git worktree list --porcelain | awk '
  /^worktree /{p=$2} /^branch /{sub("refs/heads/","",$2); print p"\t"$2}' \
| while IFS="$(printf '\t')" read -r WT BR; do
    case "$BR" in "land/$ID--"*)
      git worktree remove --force "$WT" 2>/dev/null   # single -f: fails SAFE if still locked
      git branch -D "$BR" >/dev/null 2>&1 || true ;;  # worktree first — git won't delete a
    esac                                              # branch that's still checked out
  done
```

Deriving rather than trusting a reported string is what makes this actually close the leak: it needs
no cooperation from the agent, so it works even when the agent crashed, escalated, or returned a
garbled path — and it reclaims **every** worktree that ticket accumulated across N cycles, not just
the last one. It cannot touch the **builder's** worktree: that one is branch-named
`worktree-agent-*`, never `land/<id>--*`, so it stays for `/land` to reclaim on a clean land.

**A single `--force`, never `-f -f`.** The harness *locks* a launch worktree while its agent runs and
unlocks it on exit, so a single `--force` removes a finished agent's worktree but **refuses** a
still-locked one — it fails safe. `-f -f` would rip the worktree out from under a live agent. If a
reclaim looks like a no-op, the agent is still running: wait, don't escalate the flag.

Reclaiming here rather than leaving it to `/land` matters most on an **escalation**: that branch
never merges, so `/land`'s merged-into-default-branch sweep can *never* reach it.

### 1. Sweep for stranded `ready-for-code-review` re-entries

A human resolving a build-time or technical-review escalation re-enters the ticket by re-adding
`ready-for-code-review` (and removing `land-escalated`) **outside** any `/code` run. That ticket stays
`in_progress`, so `bd ready` never returns it — and nothing in the ordinary Phase 1/2 flow looks for
it, because Phase 2 only dispatches a reviewer for a ticket *this same invocation's* Phase 1 built.

```bash
bd list --label ready-for-code-review --status in_progress --limit 0 --json
```

Any hit is by construction stranded from a **previous** invocation — this runs before this
invocation's Phase 1 builds anything. For each hit, confirm the hand-off is reviewable:

```bash
bd show <id> --json | jq -r '.[0].metadata.review_head'   # must be non-empty
```

**If it's empty, derive it from the live branch before giving up.** Nothing on the hand-edit re-entry
path forces `review_head` to be rewritten, and treating empty as unrecoverable strands the ticket
until a human diagnoses it. The remote tip is not a guess — it's the exact ref the reviewer would
fetch anyway:

```bash
SHA="$(git ls-remote origin "refs/heads/land/<id>" | cut -f1)"
scripts/validate-sha40.sh review_head "$SHA" && bd update <id> --set-metadata review_head="$SHA"
```

If that resolves to a well-formed 40-hex SHA, write it and dispatch normally. Note what a derived
value means downstream: it equals the tip by construction, so the reviewer's drift check is
*uninformative* for this ticket rather than meaningfully clean. That costs nothing — the reviewer
reviews the whole branch regardless — but it is why writing it at resolution time stays the norm and
this stays the backstop.

If the ref doesn't exist (no branch was ever pushed), **don't guess**: leave the label alone and
surface it as needing a human to re-escalate or rebuild.

Otherwise dispatch a `code-reviewer` exactly as Phase 2 does, concurrently and under the same cap.
**Reclaim each reviewer's worktree the moment it returns** — [the reclaim block above](#reclaim).

### 2. Resolve the task set

- **No argument** (default) or **`--all-ready`** → read the filtered frontier (callout below) and fan
  out across the **independent, unblocked** frontier. Don't dispatch a ticket whose blocker is also in
  the batch — surface that instead of guessing the order.
- **`--single`** (no ID) → one producer; resolve the pick **here**: build the same filtered frontier,
  take the **top** entry (`bd ready` is already priority-ordered), dispatch it as a named id.
- **One issue ID** → one producer.
- **Several IDs** → fan-out, one producer per ID. Only dispatch IDs that are genuinely independent;
  if two share a dependency, say so and let the human sequence them rather than racing.
- **Free-text** → one producer; tell the agent that is the task — it files the issue itself before
  coding, per its own rules.

> **Auto-select paths only — exclude `human`-labeled tickets and epics.** `bd ready` is a
> dependency-satisfaction query, not a build queue. Two categories reach it and must never be
> **auto**-selected:
>
> - any ticket carrying **`human`** — it exists precisely because an agent cannot resolve it;
>   dispatching a producer either invents the decision the label exists to prevent, or burns a cycle
>   rediscovering that a human was already asked;
> - any ticket with **`issue_type == epic`** — a container with no implementable acceptance criteria.
>
> **Read the frontier as JSON; the `human` label is invisible otherwise.** Plain `bd ready` prints an
> `[epic]` marker but renders no labels at all:
>
> ```bash
> bd ready --json | jq -r '.[] | select((.labels // []) | index("human") | not) | select(.issue_type != "epic") | .id'
> ```
>
> (`labels` is `null`, not `[]`, on a ticket with none — hence the `// []`.) The first entry **is**
> the highest-priority buildable item. **Report each ticket dropped** — id + reason — in step 5: a
> skip is a signal to the operator, not noise.
>
> **If nothing survives the filter, dispatch nothing and say so** — never fall back to a filtered-out
> ticket. A frontier of nothing but `human` tickets and epics is a real, reachable state, and it
> means there is no buildable work right now.
>
> This filter applies **only to auto-selection.** Explicitly-named IDs are an operator override and
> are never filtered — the operator named it on purpose.

> **Auto-select paths only — also exclude children of an un-debated epic.** `/challenge` is the
> intended stress-test gate an epic should pass before its children get built. For every candidate
> surviving the filter above:
>
> ```bash
> scripts/epic-debate-gate.sh <candidate-id>
> ```
>
> It prints `BUILD <id>` (no parent epic, or the parent already carries the `epic-debated` label
> `/challenge` stamps) or `SKIP <id> epic not debated (<epic-id>)`. Keep every `BUILD`; **report every
> `SKIP`** in step 5's skip list. The script only reads; it never writes.
>
> **No escape-hatch flag.** The unblock is to actually debate the epic (`/challenge <epic-id>`,
> cheap) or hand-apply `epic-debated` to acknowledge it was debated informally.
>
> **Scope: this gate runs only inside step 2's auto-select filtering.** It never applies to step 0's
> pickups or step 1's re-entries — both are already mid-flight, past this gate, and re-gating them
> would strand in-flight work behind a retroactively-applied check.

### 3. Phase 1 — dispatch one `coding` builder per task

Use the Agent tool with `subagent_type: "coding"` and **no call-site `isolation` option.** A subagent
pinned at the repo root cannot create its own worktree, so it needs the harness to hand it one at
dispatch — but that requirement travels with the *role*, not the call site: the agent definitions
carry `isolation: worktree` in their own frontmatter, which is sufficient on its own. **This is the
reference statement for all four dispatch sites in this file.**

> **Claim each resolved ticket from *here*, before dispatch — don't rely on the builder.** The
> builder's own claim is an unverified soft instruction it can skip under load, and nothing
> downstream catches it: Phase 2's verification checks *labels* and the *remote branch*, never
> `status`. A skipped claim has been observed carrying a ticket all the way to `ready-for-land` while
> it stayed `open` with a `null` assignee — so it sat in `bd ready` for its whole build (losing the
> claim's double-work protection) *and* step 1's sweep, which filters on `--status in_progress`, was
> blind to it.
>
> ```bash
> bd update <id> --claim     # deterministic here, one controlled flow
> ```
>
> This is the same local DB the builder sees, so the claim is visible immediately — no push needed.
> The builder's own claim becomes an idempotent backstop, and stays the *primary* claim on the one
> path with no id at dispatch: **free-text**.

- **Solo** (`/code <id>`, `--single`, free-text): dispatch exactly one builder in the foreground.
- **Fan-out**: one builder per ticket, concurrently (`run_in_background: true`), up to the cap. Queue
  the remainder and dispatch each as a slot frees. Each builds, pushes, keeps its worktree, and marks
  its own ticket independently; **one builder's escalation must not block its siblings.**

Prompt shape:

> Implement `<id>` as a producer following your cycle (claim → worktree → gates → push
> `origin/land/<id>` → mark `ready-for-code-review`, recording `review_head` → keep the worktree →
> stop). Do **not** review your own work, merge, close, or push the default branch. Stop and escalate
> (revert to green, annotate `land-escalated`, don't hand off) if a clarifying decision is needed
> during the build; stop and report if a gate fails.

### 4. Phase 2 — verify the hand-off, then dispatch a `code-reviewer`

**Never trust a builder's task-notification alone.** A builder that backgrounded its gates and
stalled can still emit a `status=completed` notification with a benign-looking summary even though
nothing was pushed. Check the **actual** state:

```bash
bd show <id> --json | jq -r '.[0].labels'      # ready-for-code-review? land-escalated?
git ls-remote origin refs/heads/land/<id>       # must resolve to a SHA
```

Dispatch the reviewer **only** when both pass. Otherwise read the labels — the two failure modes are
not the same:

- **`land-escalated` present** → the builder escalated *deliberately*: reverted to green, pushed, and
  stopped because a human owes a decision. **Skip it.** No reviewer, and never resume it to "complete
  the hand-off" — that would override the escalation. Surface it in step 5.
- **Otherwise** (no label, or the branch doesn't resolve) → the builder stalled or never finished. Do
  **not** send a reviewer into an unverified state. Resume that same builder (`SendMessage` to its
  agent id — it resumes with full context) and tell it plainly: any background gate it armed will
  never notify it back (a subagent with no live background children is stopped by the harness), so it
  must re-run the gate in the **FOREGROUND** within its own turn and complete the hand-off. Re-check
  both conditions before proceeding.

**Do not run a separate correctness-review workflow here or anywhere in this skill.** The reviewer's
own reasoned pass **is** the correctness review — not a backstop to one. A prior design fanned out a
multi-agent correctness workflow ahead of each reviewer and it was removed on measured cost: four
runs consumed roughly 80% of an entire invocation's token spend, exhausted the session limit, killed
four in-flight reviewers, and serialized every reviewer dispatch behind them — while the reviewers
repeatedly *overturned* its findings on facts it hadn't checked, and the branches that got no
workflow produced findings at least as good. Don't reconstruct it without new evidence that it beats
the reviewer's own pass per token.

Dispatch with `subagent_type: "code-reviewer"`, no call-site `isolation` option. Match the build
cadence: one reviewer in the foreground for a solo build; one per ticket concurrently for a fan-out,
each dispatched as its builder's hand-off verifies — **subject to the same shared cap**.

> Technically review `<id>` (it is `ready-for-code-review`): read `review_head`, `git fetch origin
> land/<id> main`, then `TOP=$(git rev-parse --show-toplevel)` and `git checkout -B
> "land/<id>--${TOP##*/}" FETCH_HEAD` into your own launch worktree. Your own reasoned correctness
> pass against `main...HEAD` **is** the correctness review — there are no pre-computed findings, by
> design, not by failure. Then `/simplify`, re-gate, commit, `git push origin HEAD:land/<id>`, and
> swap the ticket to `ready-for-land`. Do **not** merge, close, or push the default branch. Escalate
> (revert to green, swap to `land-escalated`, don't mark ready) only on a clarifying decision or
> "making it worse."

Give the reviewer whatever *context* is genuinely useful — which sibling branches in this fan-out
touch the same files, what the hand-off notes warn about, a specific claim worth checking. That is
cheap. What it must **not** be given is a fan-out's worth of pre-computed findings to adjudicate.

**Reclaim its worktree the moment it returns — either outcome.** [The reclaim block](#reclaim), per
ticket, not batched.

### 5. Relay each result to the user

Agent final messages aren't shown to the user — surface what matters per ticket across **both**
phases: that the build gates passed and the technical review + re-gate passed, the `land/<id>` branch
and head SHA, and that it reached `ready-for-land`.

- If a **builder** escalated: it reverted to green, pushed, applied `land-escalated`, did **not** hand
  off, and a human owes a build decision.
- If a **reviewer** escalated: green branch pushed, `land-escalated` set, not landable until the
  human decides.
- **Fan-out:** a per-ticket roll-up — which reached ready-for-land, which are still in review, which
  escalated and why.
- **Step 0:** which `needs-rebase` tickets were found, which rebased clean, which hit a genuine
  conflict and were escalated.
- **Step 1:** which stranded tickets were found, which got a reviewer, which were left alone for a
  missing `review_head`.
- **Throttling:** if the cap ever delayed a dispatch, say which cap value was in effect and roughly
  how the queue drained — so a fan-out slower than the ticket count suggests isn't mistaken for a
  stall.
- **Skips:** on an auto-select run, report **every** ticket the filters passed over — id + reason.
  Say so explicitly **even when nothing was skipped** ("no `human`/epic tickets on the frontier"), so
  the operator can tell a filter that found nothing from a filter that never ran.
- **Reclaims:** a one-line tally ("reclaimed N reviewer/pickup worktrees"). Report one
  *individually* only where it failed, which means an agent is somehow still running.

## Notes

- This skill is the **only** sanctioned way to spin up coding work from the main session.
- If an argument is genuinely ambiguous — looks like an ID but isn't one that exists, or a fan-out
  set with hidden dependencies — ask before dispatching rather than guessing.
- **MISTAKES.md: nothing for this skill to add.** `/code` dispatches `coding` and `code-reviewer` and
  relays what they report; it never touches repo files itself and discovers nothing firsthand — any
  qualifying mistake surfaces inside a dispatched subagent's own worktree, and that subagent's own
  instruction file (`coding.md`, `code-reviewer.md`) already carries the autonomous filing
  instruction. Nothing routes through this skill.
