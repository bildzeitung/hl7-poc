---
name: epic-audit
description: Closing-side completion gate for an epic — the mirror of `challenge`. When an epic's children have all closed, review the delivered set against the epic's goals and acceptance criteria: are there gaps, dropped scope, or child tickets that closed inconsistent with the epic's intent? Actionable gaps are filed as child tickets (they flow into /code); judgment calls are escalated to a human. Runs once per completed epic, or as a /loop sweep. Examples — "/epic-audit", "/epic-audit <epic-id>", "audit the completed epics", "/loop 30m /epic-audit".
---

# epic-audit

I am the **closing-side epic gate** — the mirror of [`challenge`](../challenge/SKILL.md). Where
`challenge` stress-tests an epic *before* it is built, I review it *after* every child has closed:
did the delivered set actually satisfy the epic's goals, or did something quietly fall through?

Unlike `challenge` (which only reports), I am allowed to **write the tracker** — but narrowly, by an
explicit disposition rule: a **clear, actionable gap** becomes a child ticket that flows into
`/code`; a **genuine judgment call** is **escalated to a human**, never guessed into speculative
work. On a clean bill I mark the epic reviewed and move on.

I run once per pass and stop. I do **not** merge, close product tickets, write the default branch, or
dispatch other agents.

## How I'm triggered

Detection and review are split. `/land` is the single writer that closes the last child, so it is
the one that *notices* completion: when it closes an epic's final `parent-child` child it labels the
epic **`epic-ready-to-audit`**. That label is my fast-path signal — but only a hint. My real gate is
the **live child-completion state**, so I also catch epics the label missed (a child closed by hand,
or an epic that completed before this mechanism existed).

- **Bare `/epic-audit`** — sweep every *auditable* epic, preferring flagged ones. If none are
  auditable, that's a clean no-op — say so and stop.
- **`/epic-audit <epic-id>`** — audit exactly that epic now, regardless of label.

## 1. Setup — the tracker DB is authoritative

I write the tracker, so I follow the same sync discipline as `/land`: **Dolt is authoritative;
`.beads/issues.jsonl` is an export-only artifact, never a sync wire.** Pull at the start, push after
every batch of writes.

```bash
bd dolt pull
```

## 2. Select the auditable epics

An epic is **auditable** when ALL hold:

- `issue_type == "epic"` and `status != "closed"` — I never re-open or audit a closed epic;
- it has **≥1** `parent-child` child, and **every** such child is `closed`;
- it is **not** already labeled **`epic-audited`**, OR it is, but that stamp has gone **stale**
  (hl7-poc-2bo): it has a parent-child child whose `created_at` is at or after the `audited_at`
  metadata I stamped at the last audit (step 5). `scripts/epic-audit-stale.sh <epic-id>` is the
  shared derivation — same script `epic-completion-check.sh` uses for `/land`'s re-arm signal, so
  the two never drift. Idempotency for the non-stale case is unchanged: a genuinely-audited epic is
  never re-picked.

```bash
# Fast path — epics /land flagged (this now includes re-armed, previously-audited epics: /land's
# epic-completion-check.sh only sets epic-ready-to-audit on a stale epic-audited epic, never on a
# current one):
bd list --type=epic --label epic-ready-to-audit --status open --limit 0 --json
# Safety net — every open epic, including epic-audited ones (staleness is checked per-candidate
# below, not by excluding the label here -- excluding it would hide exactly the re-armed epics
# this ticket exists to catch):
bd list --type=epic --status open --limit 0 --json
```

For each safety-net candidate, skip it unless `scripts/epic-audit-stale.sh <epic-id>` prints `true`
**or** the epic carries no `epic-audited` label at all:

```bash
if bd show <epic> --json | jq -e '(.[0].labels // []) | index("epic-audited")' >/dev/null; then
  [ "$(scripts/epic-audit-stale.sh <epic>)" = "true" ] || continue   # current audit still holds
fi
```

**`--limit 0` on both is load-bearing, not noise.** Without it the query silently truncates at the
default page size. The stake is highest on the **safety net**: open epics accumulate over a
project's life, and that query is the only thing that ever catches one `/land` failed to flag — a
silent cap means an epic past the default limit never gets audited at all, indefinitely.

For each candidate, confirm the child-completion gate from **live state**; the label is not trusted
on its own. `scripts/epic-children-closed.sh` is the shared check (also used by `/land` and
`/sweep`). It deliberately does **not** read `bd show`'s `.dependents` array — that is populated
only with the opt-in `--include-dependents` flag, and without it `dependent_count` can be non-zero
while `.dependents` is entirely absent, which silently made this check dead code everywhere it was
tried. It derives children via `bd list --parent <epic-id> --all --limit 0 --json` instead.

Both candidate queries already filter `--type=epic --status open`, so child completion is the whole
remaining verdict:

```bash
if [ "$(scripts/epic-children-closed.sh <epic>)" = "true" ]; then
  echo "AUDITABLE"
else
  echo "SKIP"
fi
```

## 3. Read the whole epic before forming an opinion

Same discipline as `challenge` §1 — no verdict until I've read all of it:

- The epic's `description`, `acceptance_criteria`, `design`, and `notes` — this is the **intent** I
  judge against.
- **Every** child: `bd show <child> --json`. What it actually delivered, its own acceptance, its
  `close_reason`, any supersede or bounce history. A child that closed via `bd supersede` during a
  land bounce handed its real work to a rebuild ticket; `/land` re-parents that rebuild onto this
  epic, so it normally shows up as an open child and correctly keeps the epic incomplete. Still worth
  a glance: if a superseded child's rebuild is *not* a child here, the epic can read "complete" while
  work is still open.
- Cross-check against the source of truth in `docs/` — an epic whose delivery drifted from a settled
  decision is a finding.

## 4. Review — did the delivered set complete the epic?

- **Acceptance met?** Walk each clause of `acceptance_criteria`. Is each actually satisfied by a
  closed child's delivered work — not merely "all children closed"? An unmet clause with no ticket
  covering it is the primary thing I file.
- **Dropped or silently narrowed scope?** Did the description promise something no child delivered?
  Was a child re-scoped or superseded in a way that dropped part of the goal without a replacement?
- **Consistency with intent.** Did any child close in a way that *contradicts* the epic — a stub left
  in, a decision recorded in a tracker note instead of `docs/`, an invariant the epic depended on now
  violated?
- **Coherence of the whole.** The children each passed their own gate; do they add up to the working
  capability the epic describes, or are there seams between them that nobody owns?

Be precise, skip the clean parts, and don't manufacture findings to look thorough — a clean bill is a
valid, common outcome.

## 5. Disposition — file, escalate, or nothing

**First, before filing anything, stamp the audit time:**

```bash
bd update <epic> --set-metadata audited_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

**Stamp BEFORE filing (decided by a human, hl7-poc-2bo).** Every gap/escalation ticket filed below
is a parent-child child of this epic, so its `created_at` is at or after the stamp and marks the
audit stale (`scripts/epic-audit-stale.sh` compares with `>=`, so a gap filed in the same second
still counts). Once those gaps close, the epic re-arms and a follow-up audit verifies they were
delivered. This converges: each re-audit files only new gaps, and a clean re-audit files none.

### Actionable gap → file a child ticket (flows into `/code`)

When the fix is *clear enough to hand a builder* — a good bug report: what's missing, why, and what
"done" looks like. File it as a child of the epic so it surfaces in `bd ready`. Use
`--no-inherit-labels` so it does **not** pick up `epic-audited`/`epic-ready-to-audit` from the
parent:

```bash
bd create --type=<task|bug|feature> --parent=<epic> --no-inherit-labels --label=epic-audit-gap \
  --title="<what's missing> (epic-audit gap on <epic>)" \
  --description="Found by /epic-audit closing-side review of <epic>.

GAP: <what the epic promised / an acceptance clause requires that no closed child delivered>
EVIDENCE: <the acceptance clause + which children were expected to cover it>
DONE WHEN: <the observable condition that closes this gap>" \
  --acceptance="<measurable success condition>"
```

### Needs a human call → escalate (do not guess work)

When the finding is a *judgment call* — is this acceptance clause even in scope? was this re-scope
intended? does "done" depend on an unrecorded decision? — I do **not** invent a ticket. I raise a
`decision` ticket carrying the **`human`** label, the project's escalation mechanism:

```bash
bd create --type=decision --parent=<epic> --no-inherit-labels --label=human,epic-audit-gap \
  --title="DECISION: <the question> (epic-audit on <epic>)" \
  --description="/epic-audit needs a human call before it can file work for <epic>.

QUESTION: <the ambiguity — stated as a decision, with the options as I see them>
WHY IT'S NOT AUTO-FILEABLE: <what makes this judgment, not a clear bug report>"
```

This matches the project's escalation norm: **self-correct autonomously; surface only a real decision
or a "making it worse".**

### Clean bill → nothing to file

I file nothing, but still mark the epic reviewed so it isn't swept again.

## 6. Mark the epic reviewed and publish

However the review came out, retire the work signal and label the epic (`audited_at` was already
stamped at the start of step 5):

```bash
bd label add <epic> epic-audited
bd label remove <epic> epic-ready-to-audit   # no-op if it wasn't set
scripts/bd-dolt-push.sh
```

`epic-audited` is not terminal: an audited epic is out of my sweep **until** it has a parent-child
child created at or after `audited_at` — my own gap tickets or any later-added child. When that
child closes, `/land` re-flags the epic `epic-ready-to-audit` and the safety net above picks it up.
An epic-audited epic with no `audited_at` (audited before this mechanism existed) never re-arms on
its own; re-auditing it is an explicit `/epic-audit <epic>`. The epic itself stays **open**; closing
it is the human's call once the audit's gaps are resolved.

## What I never do

- **Close an epic or a child, merge, or write the default branch.** I file and escalate.
- **Auto-file a judgment call.** Ambiguous → `human`-labeled `decision` ticket, never speculative
  work.
- **Re-audit a still-current `epic-audited` epic** (its stamp not stale per
  `scripts/epic-audit-stale.sh`) in a sweep, or file duplicate gaps.
- **Commit or `bd import` the passive `.beads/*.jsonl`** in place of the push script.
- **Record a design decision in a tracker note** instead of `docs/` — that forks the record. A gap
  that is really a design question is an escalation, and its resolution lands in `docs/`.
- **Append to MISTAKES.md directly.** I have no `isolation: worktree` and I write only the tracker —
  no `git`, no repo file edits (see the list above) — so I'm in the same position as the main session
  under the never-edit-on-the-default-branch rule. If reviewing a delivered epic turns up a mistake
  meeting the mistake log's bar, I report it in my Stop-and-report readout instead, as a
  `MISTAKES.md CANDIDATE` block in the log's entry shape (the same block name `land-review` uses, so
  it is greppable across reports). Filing it is for whoever reads that report: a human directly, or a
  `/code`-dispatched producer whose `coding.md` already carries the autonomous filing instruction.

## Stop and report

Per epic audited: **clean** (nothing filed), or the gap tickets I **filed** (IDs) and the questions I
**escalated** (IDs, each owing a human a decision). If a sweep found nothing auditable, say so
plainly and stop.
