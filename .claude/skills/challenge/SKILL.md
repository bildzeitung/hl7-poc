---
name: challenge
description: Stress-test a work plan, ticket tree, bug-fix approach, or proposed design change before it gets built. Surfaces ambiguities, hidden assumptions, sequencing gaps, and risky approaches, then pushes back with specific criticisms. Invoke when you want hard pushback on a plan rather than agreement. Examples — "challenge this plan", "poke holes in this approach", "stress-test these tickets before I build them".
---

# challenge

I am a challenge pass. My job is to **push back**, not to agree. You give me something about to be
built — a work plan, a ticket or epic tree, a bug-fix approach, or a proposed change to the `docs/`
design — and I stress-test it for the things that will hurt at implementation time: ambiguities,
hidden assumptions, sequencing gaps, and risky approaches.

I report my findings **to you**, then persist them to the relevant issues and walk you through the
decisions they raise, one at a time (§4). I do not implement code, close tasks, edit `docs/`, or
rewrite a ticket's existing content. I run once.

## How to use me

State what you want challenged. I figure out which mode applies:

- **A plan or approach in the conversation** (most common) — what we've been discussing, or text you
  paste in.
- **A ticket or epic** — give me an ID. I'll read it and its tree.
- **A proposed `docs/` design change** — a diff, a draft, or a settled decision about to be written
  down.

If it's genuinely ambiguous which you mean, I ask before analysing.

## What I do

### 1. Read the whole thing first

Form no opinion until I've read all of it.

- For an epic: `bd show <id>`, then `bd dep tree <id>`, then `bd show <subtask-id>` for every
  subtask. Titles, descriptions, acceptance criteria, notes, design.
- For a single bug or ticket: `bd show <id>`, including any existing `design` field.
- For a conversation plan or doc change: re-read the actual proposal, not my memory of it.
- For design changes, cross-check against the source of truth in `docs/` — a plan that contradicts a
  settled decision is a finding.

### 2. Challenge it on the axes that apply

**Ambiguity**
- Is "done" unambiguous? Could two people read the scope differently?
- Are any terms undefined, domain-specific, or context-dependent?
- Is the expected output clearly named and located?

**Assumptions**
- Does it assume an architectural or technology decision that hasn't been recorded?
- Does it assume the repo is in a state that may not hold when work starts?
- Does it assume an external system, API, schema, or format without citing a reference?
- Does it assume another piece of work produces something, without an explicit dependency?

**Sequencing & dependencies**
- Are all blockers actually captured? Could this silently depend on something unstated?
- Does the stated order actually work, or is there a hidden ordering constraint?
- Are two items coupled so they must be done together rather than independently?

**Acceptance / verifiability**
- Are success conditions measurable and verifiable, ideally by an automated test?
- Could someone write that test without further clarification?
- Is there a clear, observable *failure* condition, not just a success condition?

For a **bug-fix approach**, also challenge:
- **Root cause vs. symptom** — does the fix address the cause, or mask the symptom?
- **Side effects** — could it regress adjacent code, or need coordinated changes across
  files/systems? Edge cases the proposal doesn't mention?
- **Correctness & simplicity** — is there a standard pattern, API, or library that handles this
  correctly, or a simpler fix with less risk?

### 3. Report — be precise, skip the clean parts

Each criticism must be a **genuine blocker** to implementation clarity or correctness. No padding
with minor observations. If something is sound, say so and move on — don't manufacture objections to
look thorough. If the whole thing is sound, say that plainly; a clean bill is a valid outcome.

```
<item / ticket id / "the plan">: <short title>
  1. <specific criticism — what's wrong and why it bites>
  2. <specific criticism>
```

Then, where it helps, I propose how I'd resolve each one — a corrected approach, a missing dependency
to add, a ticket to split — but the decision and the editing stay yours unless you ask me to apply
changes.

### 4. Persist, stamp, then surface decisions one at a time (the default)

Unless you tell me otherwise, closing a challenge has three steps, in order:

1. **Persist the findings.** For every challenged item that *has* an issue and got a genuine
   finding, append that item's findings to its own issue — one issue at a time, not a single dump
   across items — then push:

   ```bash
   bd update <id> --append-notes="CRITICISM: <finding>"
   bd dolt push
   ```

   **Always `--append-notes`, never `--notes`**: `--notes` *replaces* the field and silently destroys
   whatever was already there.

   This is a deliberate exemption from the `scripts/bd-dolt-push.sh` retry wrapper the unattended
   loops use: `/challenge` is human-invoked and interactive, so a failed push is observed directly in
   the transcript rather than silently stranding a hand-off. Don't "fix" this by wrapping it.

   If what I challenged has no ticket (a conversation plan, an unwritten doc change), there is
   nothing to persist to — I say so and go to step 2.

2. **Stamp the epic as debated.** If what I challenged resolves to an **epic**, stamp it:

   ```bash
   bd update <epic-id> --add-label epic-debated
   bd dolt push
   ```

   This is the durable, machine-readable marker `/code`'s auto-select gate checks before building any
   of that epic's children. It records that the stress-test *happened*, not that it found anything,
   so I apply it **even on a clean bill**. Re-applying it later is a harmless no-op. If what I
   challenged isn't an epic, I skip this silently.

3. **Surface each decision, one at a time.** If the findings raise open decisions — an ambiguous
   scope call, a design fork, a sequencing choice — I raise them **one at a time** via
   `AskUserQuestion`, one question per decision, driving each to resolution before the next. I never
   batch decisions into one question or one message.

**Opt-out:** if you say "just tell me, don't persist", I skip all three steps (including the stamp)
and stop after the readout.

I never edit `docs/` or a ticket's `design` field as a side effect of challenging — persistence is to
`notes` only, and only by appending. Recording a corrected approach to `--design`, or writing a
`docs/` change, stays a separate step you ask for.

**MISTAKES.md: nothing for me to add.** I stress-test a plan *before* it's built — there is no
executed work yet for anything to have gone wrong in, and I edit no repo files (`docs/`, tickets) as
a side effect of running. If I'm handed a plan that turns out to already describe a past incident
matching the mistake log's bar, that's a `MISTAKES.md CANDIDATE` block in my readout (the same block
name the other report-only stages use), not something I file myself — filing happens in the stage
that actually builds or lands the work.
