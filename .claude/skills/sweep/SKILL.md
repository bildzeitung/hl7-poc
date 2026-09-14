---
name: sweep
description: The third /loop leg — a SURFACE-ONLY human-decision surfacer. Scans the tracker for work that has stopped waiting on a human and nothing else consumes (land-escalated branches, human-labeled decision tickets that are not dependency-blocked, epics ready for a human close-decision), dedups against a durable cross-machine digest issue, and surfaces new items; every pass's report ends with the full "Actionable now" list of what's decidable right now (every current, non-deferred row, in full — not just the delta), and also lists every deferred-status ticket (§2a), every in_progress ticket claimed more than 24h ago that carries no pipeline label (§2b), and every dependency-blocked human-labeled ticket (§2c) in its report each pass (read-only, no dedup, never in the digest) so parked, stranded, and not-yet-decidable work stays visible. Writes no default branch, makes no decisions, dispatches no builders/landers/auditors. Run self-paced as /loop 30m /sweep. Examples — "/sweep", "/loop 30m /sweep", "what needs a human decision right now?".
---

# sweep

I am the **human-decision surfacer** — the third `/loop` leg alongside `/code` and `/land` (and
`/epic-audit`). `/code` sweeps `needs-rebase` back to a producer; `/land`'s bounces re-enter
`bd ready`; `/epic-audit` runs itself. The only pipeline outputs with **no downstream consumer** are
the ones parked for a human: a `land-escalated` branch, a `human`-labeled decision ticket, and an
epic that's `epic-audited` + open + every child closed. Nothing pings a human when work parks on one
of these — you only find it by manually running the tracker. I turn that silence into an active
surface.

I also list every `deferred`-status ticket each pass (§2a) — parked work `bd ready` hides by design —
every `in_progress` ticket claimed more than 24h ago that carries none of the pipeline labels
(§2b) — claimed work that fell out of every consumer's sight, since `bd ready` excludes it for being
`in_progress` while every pipeline leg keys on a label it doesn't have — and every open
`human`-labeled ticket that is currently dependency-blocked (§2c) — a sign-off placeholder whose
artifact does not exist yet, so it is not decidable and is subtracted from §1's `$HUMAN` source
before it can reach `$CURRENT`/the digest/the push. All three are **report-only**: no dedup state, no
digest rewrite, no notification.

I am the **lowest-privilege** loop leg, deliberately: I write **one** self-owned bookkeeping issue (a
running digest) and nothing else.

## How I'm triggered

No arguments — I sweep the whole queue every pass. Typically **`/loop 30m /sweep`** (escalations are
exceptions and epics complete rarely, so a slow tick is fine), or ad hoc as bare `/sweep`.

## Non-goals — hold the line

- **Never writes the default branch.** That is `/land` alone.
- **Never builds, lands, or audits.** I dispatch no agent of any kind.
- **Never makes or auto-defaults the human's decision.** Surface only.
- **Never touches `.beads/issues.jsonl`.** No `git add`, no commit, no `bd import`.
- **Never claims work off `bd ready`**, and needs no worktree — every step is tracker plumbing; I
  touch no `git` and write no repo files. (Scratch files under `${TMPDIR:-/tmp}` carrying this pass's
  intermediate state between fenced blocks are neither.)
- **Never promotes a ticket to a human-decision item *because* it is `deferred`, stranded, or
  blocked.** §2a, §2b, and §2c are visibility only: nothing they read enters `$CURRENT`/`$NEW_IDS`,
  touches the digest, or fires a notification.
- **Never auto-remediates a stranded ticket.** §2b does not unclaim, reassign, or reopen anything. A
  human decides whether a stranded ticket is abandoned or deliberately held.
- **Never lets a dependency-blocked `human` ticket sit in `$CURRENT`/the digest/the push, and never
  drops it from view either.** §1 subtracts `bd blocked`'s id set from `$HUMAN` — a sign-off
  placeholder for an artifact that doesn't exist yet is not decidable — but the subtracted rows are
  still listed, unconditionally, every pass, in §2c's report-only "Blocked human tickets" section.
  When the blocking dependency closes, the ticket enters `$CURRENT` for the first time and notifies
  as NEW — deliberate, not a side effect.

## 0. Setup — tracker-authoritative, fresh scratch state

Pull before reading, push after writing. Each fenced block runs as its own separate Bash invocation —
**nothing carries over** (variables, arrays, functions). §1–§3 persist results to files under a
**fixed** scratch directory so later blocks can read them back. Fresh per pass, so no stale queue
data leaks in:

```bash
bd dolt pull
SWEEP_TMP="${TMPDIR:-/tmp}/harness-sweep-state"
rm -rf "$SWEEP_TMP" && mkdir -p "$SWEEP_TMP"
```

## 1. Collect the human-decision queue

Two sources, plus a `bd blocked` subtraction on the `human` source — see the note below the block. I
defensively exclude my own digest issue from the `land-escalated` query.

```bash
SWEEP_TMP="${TMPDIR:-/tmp}/harness-sweep-state"   # re-derive — fresh Bash invocation, see §0

set -o pipefail   # REQUIRED — makes a failed query detectable; see the note below.

# The marker is written INSIDE each failure branch, never as a trailing `[ $FAILED = 1 ] &&
# touch ...`: a conditional at the end of a block becomes the block's own exit status, so the
# healthy path would short-circuit to 1 and report a failure on every good pass. Existence is
# the whole signal — content is irrelevant.
if ! ESCALATED=$(bd list --label land-escalated --exclude-label sweep-digest --limit 0 --json \
  | jq -r '(. // []) | .[] | "\(.id)\tland-escalated\t\(.title)\t\(.status)"'); then
  touch "$SWEEP_TMP/source_query_failed"
  ESCALATED=""   # the capture may be partial/garbled on failure — never persist it as data
fi
printf '%s' "$ESCALATED" > "$SWEEP_TMP/escalated"

if ! HUMAN_RAW=$(bd human list --status open --json \
  | jq -r '(. // []) | .[] | "\(.id)\thuman\t\(.title)"'); then
  touch "$SWEEP_TMP/source_query_failed"
  HUMAN_RAW=""
fi

# A human-labeled ticket that is dependency-blocked is not decidable -- the artifact it signs
# off on does not exist yet -- so subtract `bd blocked`'s id set from $HUMAN before it can
# reach $CURRENT/the digest/PushNotification. $ESCALATED/$CLOSABLE are never filtered this
# way -- this is a $HUMAN-only subtraction. `bd blocked` has no --limit flag to pin (it is a
# distinct subcommand from `bd list`, outside the bd-list limit gate's scan surface).
# Pre-truncate BOTH outputs, unconditionally: awk never opens an output file it writes zero
# rows to, so without these an empty list would leave NO file and §8 would read `missing`
# ("§1 never ran") instead of `ok`/`(none)`. These lines are load-bearing for that three-state
# distinction -- do not drop them in favour of awk's own redirection.
: > "$SWEEP_TMP/human"
: > "$SWEEP_TMP/blocked_human"

# The query and the partition it feeds are ONE branch deliberately: the failure path has to
# write both files itself, so splitting them would mean re-testing a flag 20 lines below the
# branch that set it.
#
# Partition $HUMAN_RAW on membership in $BLOCKED_IDS: the non-blocked rows become the real
# $HUMAN source (unchanged shape, `<id>\thuman\t<title>`); the blocked-out rows are persisted
# separately for §2c's report-only "Blocked human tickets" section, which shares the §2a/§2b
# contract (own scratch file, own sentinel -- see that section below).
if ! BLOCKED_IDS=$(bd blocked --json | jq -r '(. // []) | .[] | .id'); then
  # Same marker as the two queries above: a failed `bd blocked` must NOT be read as "nothing
  # is blocked" -- that would let the whole blocked set flood $CURRENT and false-notify it as
  # new. It suppresses the §6 rewrite exactly like an $ESCALATED/$HUMAN failure.
  touch "$SWEEP_TMP/source_query_failed"
  # The partition itself is meaningless now (we don't know the true blocked set), so $HUMAN is
  # left unfiltered -- harmless, since the marker above already suppresses §6/§7 for this whole
  # pass -- and §2c's own copy gets the SWEEP-QUERY-ERROR sentinel per its contract.
  printf '%s' "SWEEP-QUERY-ERROR" > "$SWEEP_TMP/blocked_human"
  printf '%s' "$HUMAN_RAW" > "$SWEEP_TMP/human"
else
  awk -F'\t' -v human_out="$SWEEP_TMP/human" -v blocked_out="$SWEEP_TMP/blocked_human" '
    NR == FNR { if ($1 != "") blocked[$1] = 1; next }
    $1 == "" { next }
    ($1 in blocked) { print $1 "\t" $3 >> blocked_out; next }
    { print $1 "\t" $2 "\t" $3 >> human_out }
  ' <(printf '%s\n' "$BLOCKED_IDS") <(printf '%s\n' "$HUMAN_RAW")
fi
```

**`set -o pipefail` is what makes those `if !` guards mean anything.** Without it,
`VAR=$(bd … | jq …)` carries the exit status of the *last* command in the pipeline — `jq` alone — and
a failing `bd` never reaches it: a failed tracker query writes its diagnostic to **stderr** and
**zero bytes to stdout**, so `jq` reads no input, emits nothing, and exits `0`. The assignment
reports success on a failed query, and `$ESCALATED` comes out empty — indistinguishable from a
legitimately empty queue, at the exact site that gates §6's wholesale digest rewrite. It is set
*inside* each block, since each block is a fresh shell.

**`--limit 0` on every `bd list` in this skill.** `bd list --help` documents `--limit` with a default
of 50 ("use 0 for unlimited"), and the tracker emits **no** truncation signal — it neither errors nor
marks a short result. Whether the cap applies to an omitted flag has varied by version, so pin the
documented "unlimited" semantics rather than depending on today's behavior. It matters most here: §6
rewrites the digest **wholesale** from `$CURRENT`, so a capped source query would drop items past the
cap from the durable record, read as resolved, and re-notify as "new" on a later pass.

**Why `(. // [])` and not a bare `.[]`:** an *empty* result serializes as literal `null`, not `[]`. A
bare `jq '.[]'` on `null` aborts with `Cannot iterate over null`, which reads as a *failed query* —
and a failed query suppresses the rewrite (§5), so a just-resolved item would **zombie in the digest**
instead of dropping out promptly. `(. // [])` normalizes `null` → `[]`. It does not mask the real
failure signature described above, because `jq` never receives input in that case.

**Why `$ESCALATED` carries a 4th field (`.status`) and `$HUMAN` doesn't:** the `land-escalated` query
passes no `--status` filter — deliberately — so it can return a ticket that is *also* `deferred`, and
§7 needs that per-row status to suppress the push and annotate the report. The value is already on
every row, so capturing it costs no extra call. `$HUMAN` never needs it: that query already filters
to `--status open`.

**Why `$HUMAN` is subtracted against `bd blocked` before it becomes the real `$HUMAN` source:** a
`human`-labeled ticket that is dependency-blocked is a sign-off placeholder for an artifact that
does not exist yet — it is not decidable, so it must not sit in `$CURRENT`, the digest, or the push.
`bd human list --json` carries no dependency fields, so the filter is a subtraction against
`bd blocked --json`'s id set, done once here, never touching `$ESCALATED` or `$CLOSABLE`. The
subtracted-out rows are never dropped from view — they are persisted to their own report-only §2c
section below, so a human ticket blocked on a deferred dependency does not vanish from every surface
indefinitely. A failed `bd blocked` query is treated exactly like a failed `$ESCALATED`/`$HUMAN`
query — it writes `source_query_failed` and suppresses §6/§7 for this pass, never "nothing is
blocked" (which would flood `$CURRENT` with the whole blocked set and false-notify it as new). When
a blocking dependency closes, the ticket enters `$CURRENT` for the first time and notifies as NEW —
the sign-off push arrives exactly when the artifact exists. Deliberate, not a side effect.

## 2. Collect epics ready for a human close-decision

An epic qualifies when it is `epic-audited` + still `open` + **every** `parent-child` child is
`closed` — `/epic-audit` deliberately never closes an epic itself, so nothing else flags these. Same
shared check `/epic-audit` and `/land` use.

```bash
SWEEP_TMP="${TMPDIR:-/tmp}/harness-sweep-state"   # re-derive — see §0

set -o pipefail   # REQUIRED — see §1.

CLOSABLE=""
# Pull id AND title in the ONE list read — rows already carry `title`, so a second `bd show`
# per epic would be a wasted round-trip against derivable state. Capture into a plain variable
# FIRST so its exit status is checkable: piping straight into the loop via `< <(...)` only ever
# exposes `read`'s own exit status to the `while`, silently swallowing a failed upstream no
# matter how pipefail is set (process substitution runs in a separate subshell).
if EPICS=$(bd list --type=epic --label epic-audited --status open --limit 0 --json \
  | jq -r '(. // []) | .[] | [.id, .title] | @tsv'); then
  while IFS=$'\t' read -r e TITLE; do
    [ -z "$e" ] && continue   # a genuinely empty $EPICS still yields one blank read via <<<
    [ "$(scripts/epic-children-closed.sh "$e")" = "true" ] || continue
    ROW=$(printf '%s\tepic-ready-to-close\t%s' "$e" "$TITLE")
    # The newline MUST sit outside the command substitution: `$(...)` strips trailing
    # newlines, so building the row as `printf '...\n'` would silently drop the separator
    # and jam every epic onto ONE line — only visible with >=2 closable epics, which is
    # why it reads fine in a one-epic spot check.
    CLOSABLE="${CLOSABLE}${ROW}
"
  done <<< "$EPICS"
else
  # Same marker as §1 — either section's failure is sufficient to skip §6/§7, so it is one
  # shared existence check, not a per-section one.
  touch "$SWEEP_TMP/source_query_failed"
fi
printf '%s' "$CLOSABLE" > "$SWEEP_TMP/closable"
```

## Report-only sections (§2a, §2b, §2c) — shared contract

§2a (`deferred` tickets), §2b (stranded `in_progress` tickets), and §2c (dependency-blocked `human`
tickets) are three report-only lists that share a **rendering contract** — how each list's result is
persisted and what it's excluded from — stated once here rather than three times below. §2a and §2b
additionally share a **collection contract** — how each list's own `bd` query is run. §2c has no
collection contract of its own: its data is the `bd blocked --json` call §1 already makes (to
compute the `$HUMAN` subtraction), not an independent read — see §2c's own section below for what
stands in its place.

### Rendering contract (§2a, §2b, §2c — no exceptions)

**The sentinel, and why it can't collide with a real row.** Each section persists to its own
`$SWEEP_TMP` file (`$SWEEP_TMP/deferred` for §2a, `$SWEEP_TMP/stranded` for §2b,
`$SWEEP_TMP/blocked_human` for §2c), which §8 (a later, separate invocation) reads back from disk
rather than relying on in-context memory of the block's output. On a query error the writer
overwrites the capture — possibly partial or garbled — with the literal string `SWEEP-QUERY-ERROR`
instead of aborting. That gives each file three readable states:

- **missing** — the writer never ran this pass (e.g. crashed before its `printf`);
- **the sentinel** — the query errored;
- **anything else** — the query succeeded (zero or more real rows).

The sentinel is a single line with **no tab**, structurally impossible for a real row to produce
(every row is `<id>\t<title>` via `@tsv`) — a format invariant, not string luck. §8 checks it by
exact match before treating content as data.

**Deliberately excluded from everything else in this skill.** None of the three lists ever feeds
`$CURRENT`, enters the delta, drives the rewrite decision, triggers a notification, or is written
into the digest. None carries dedup state — each is recomputed fresh, in full, every pass. §2c is
already excluded a layer earlier too: its rows are subtracted out of `$HUMAN` itself in §1, before
`$HUMAN` ever reaches §3 — so unlike §2a/§2b, whose lists are independent of what does reach
`$CURRENT`, §2c's list is the complement of what §1 lets through.

§8 owns what each state renders as — see its three-state rule, and
[Failure handling](#failure-handling--a-sub-step-fails-the-loop-survives).

### Collection contract (§2a, §2b only)

**`set -o pipefail` is load-bearing in each block, not hygiene** — same mechanism as §1.

**`--limit 0` — same reason as §1.** The stake here: each section promises its list in full every
pass, so a capped query would under-report while §8's count still read as the true total.

Failure here is isolated to that step alone: the block writes the sentinel instead of aborting, and
the pass continues.

## 2a. Collect deferred tickets (report-only)

`deferred` tickets are explicitly parked "deal with later" by a human — the opposite of a fresh
human-decision item — but `bd ready` hides them by design and no other loop leg lists them, so once
parked they vanish from every workflow surface.

```bash
SWEEP_TMP="${TMPDIR:-/tmp}/harness-sweep-state"   # re-derive — see §0

set -o pipefail   # REQUIRED — see the shared contract above.

if ! DEFERRED=$(bd list --status deferred --limit 0 --json \
  | jq -r '(. // []) | .[] | [.id, .title] | @tsv'); then
  DEFERRED="SWEEP-QUERY-ERROR"
fi
printf '%s' "$DEFERRED" > "$SWEEP_TMP/deferred"
```

A ticket moving into or out of `deferred` is **not** a new human-decision item.

**The one deliberate overlap:** a ticket that is simultaneously `land-escalated` (§1) and `deferred`
is listed here (unannotated) *and*, on the pass it first appears, in §7/§8's new-items block
(annotated `(deferred)`). That is information about a parked escalation, not redundancy to tidy away.

## 2b. Collect stranded in_progress tickets (report-only)

Claiming a ticket sets `status=in_progress`, removing it from `bd ready` — so `/code` never picks it
up again. Without a `ready-for-*` label it is also invisible to `/code`'s phases and sweeps and to
`/land`. Every consumer keys on either `bd ready` or a label, and `in_progress` + unlabeled satisfies
neither: the ticket is stranded silently.

**Age discriminator — 24h on `started_at`.** Unlike `deferred` (a terminal, parked state),
`in_progress` is a *transient working state*: a producer claims its ticket up front and only applies
`ready-for-code-review` at hand-off, minutes-to-hours later, so for that whole build window the
ticket carries none of the exclude-labels and is indistinguishable from a stranding. Unfiltered, §2b
would list the live build queue every pass. So it filters on **`started_at`** — "when claimed", not
`updated_at`. `bd list` exposes no `--started-*` flag, but the `--json` rows carry the field and this
block already pipes through `jq`.

```bash
SWEEP_TMP="${TMPDIR:-/tmp}/harness-sweep-state"   # re-derive — see §0

set -o pipefail   # REQUIRED, same reason as §2a.

# select(...): only tickets claimed more than 24h ago (86400s). A null/missing started_at
# (shouldn't happen for in_progress, but defensively) is treated as stranded, not filtered
# out — there is no age evidence to exclude it on.
if ! STRANDED=$(bd list --status in_progress --limit 0 --json \
  --exclude-label ready-for-code-review,ready-for-land,needs-rebase,sweep-digest,land-escalated \
  | jq -r '(. // []) | .[]
      | select(.started_at == null or (.started_at | fromdateiso8601) < (now - 86400))
      | [.id, .title] | @tsv'); then
  STRANDED="SWEEP-QUERY-ERROR"
fi
printf '%s' "$STRANDED" > "$SWEEP_TMP/stranded"
```

**The exclude-label list — deliberately not the fuller set §1 might suggest.**
`ready-for-code-review`, `ready-for-land`, and `needs-rebase` exclude live mid-pipeline work — not
strandings, mid-flight. `sweep-digest` excludes my own digest issue. That leaves two labels a reader
would expect to be treated alike, and they are not:

- **`land-escalated` IS excluded.** §1's `land-escalated` query passes no `--status` filter, so an
  `in_progress` + `land-escalated` ticket already reaches `$CURRENT` and the digest through §1.
  Surfacing it here too adds nothing.
- **`human` is deliberately NOT excluded.** §1's `human` source is status-filtered
  (`bd human list --status open`), so an `in_progress` ticket that also carries `human` is invisible
  to it. If §2b also excluded `human`, such a ticket would be surfaced by **neither** — stranded from
  every consumer, exactly the silence this section exists to close.

So the exclude list is narrower than "everything §1 also looks at": it excludes only the label whose
§1 counterpart is status-agnostic.

## 2c. Blocked human tickets (report-only — never touches the digest or notify path)

A further, independent list, but not an independent **read**: its data is the `bd blocked --json`
call §1 already made to subtract dependency-blocked ids out of `$HUMAN` (see §1's note above). §2c is
just this pass's persistence of the rows §1 partitioned out — every open `human`-labeled ticket that
is currently blocked on an unclosed `blocks` dependency, listed for visibility only, so it does not
vanish from every workflow surface for its epic's whole lifetime.

§1 already wrote this section's file (`$SWEEP_TMP/blocked_human`, `<id>\t<title>` rows, or the
`SWEEP-QUERY-ERROR` sentinel on a failed `bd blocked`) as part of partitioning `$HUMAN` — there is no
separate fenced block here to run, and so no `--limit 0`/`set -o pipefail` collection contract of its
own: `bd blocked` exposes no `--limit` flag at all, so there is nothing to pin. The rendering
contract — the persistence/sentinel convention, the three-state file contract, and what this section
is deliberately excluded from — is stated once, for this section and §2a/§2b both, in
[Report-only sections (§2a, §2b, §2c) — shared
contract](#report-only-sections-2a-2b-2c--shared-contract) above, and applies to §2c identically,
with one difference in how its failure surfaces: since §2c's data is §1's `bd blocked` call rather
than a query of its own, a failure there writes `source_query_failed` (§1) *and* the
`SWEEP-QUERY-ERROR` sentinel into `$SWEEP_TMP/blocked_human`, both at once — rather than the sentinel
alone, as §2a/§2b's own failed queries write.

A ticket entering or leaving this list is not itself a new human-decision item — but leaving it (its
blocking dependency closes) is exactly what makes the ticket enter `$CURRENT` for the *first* time in
§1/§3, which *does* trigger a fresh `PushNotification` on that later pass (§5/§7) — the sign-off push
arrives exactly when the artifact it signs off on exists. That is the deliberate point of this
section, not a side effect.

## 3. Build the current queue (dedup on stable IDs)

```bash
SWEEP_TMP="${TMPDIR:-/tmp}/harness-sweep-state"   # re-derive — see §0

# Load §1/§2's results back from disk and assert each one loaded — a missing file means that
# step never ran this pass, and continuing on a phantom-empty queue would risk deleting real
# escalations from the digest (§5's hard precondition, in reverse). The loader's default policy
# is "missing fatal, empty OK"; do NOT add --require-nonempty, an empty queue is the ordinary
# healthy case.
ESCALATED="$(scripts/land-state-load.sh "$SWEEP_TMP/escalated" -- \
  "§1 did not run this pass")" || exit 1
HUMAN="$(scripts/land-state-load.sh "$SWEEP_TMP/human" -- \
  "§1 did not run this pass")" || exit 1
CLOSABLE="$(scripts/land-state-load.sh "$SWEEP_TMP/closable" -- \
  "§2 did not run this pass")" || exit 1

CURRENT=$(printf '%s\n%s\n%s\n' "$ESCALATED" "$HUMAN" "$CLOSABLE" | sed '/^$/d' | sort -u -t$'\t' -k1,1)
printf '%s' "$CURRENT" > "$SWEEP_TMP/current"
```

Each line is `<id>\t<kind>\t<title>`; `<id>` is the dedup key throughout. An `$ESCALATED`-sourced row
carries a 4th field (`.status`); the others never do. Every downstream reader looks only at field 1,
so the extra field is inert until §7.

## 4. Find-or-create the digest issue (locator = reserved label `sweep-digest`)

The dedup state lives **in the digest issue itself** — durable, and it travels cross-machine. A local
scratchpad state file was explicitly rejected: it re-notifies the whole queue from a second machine.
The issue is found by a **reserved label**, not a remembered ID:

```bash
DIGEST_ROWS=$(bd list --label sweep-digest --all --limit 0 --json)
N=$(echo "$DIGEST_ROWS" | jq '(. // []) | length')
```

- **`N == 0`** — bootstrap. Only create it if `$CURRENT` is non-empty (an empty queue with no prior
  digest is a clean no-op). Create it with a **placeholder body carrying no `SWEEP-ITEM` lines**, so
  §5 reads the prior set as empty and every current item counts as new — the first pass must *notify*
  the full standing queue, not silently swallow it. Then claim it immediately so it never appears in
  `bd ready`:

  ```bash
  DIGEST_ID=$(bd create --type=chore \
    --title="Human-decision digest (auto-maintained by /sweep — do not build)" \
    --label=sweep-digest --description="(bootstrapping — /sweep fills this in on this same pass)" --silent)
  bd update "$DIGEST_ID" --claim
  ```
- **`N == 1`** — steady state. Nothing to do here: §5 and §6 each re-derive `$DIGEST_ID` themselves
  via `scripts/sweep-digest-id.sh`, since nothing survives between blocks. **That script re-asserts
  `N == 1` itself** — this branch is not a guard those blocks inherit.
- **`N > 1`** — anomaly. Do **not** guess which is authoritative and do **not** write anything. Report
  the duplicate IDs plainly and stop the write path for this pass; a human consolidates by hand.

## 5. Compute the delta against the prior digest

```bash
SWEEP_TMP="${TMPDIR:-/tmp}/harness-sweep-state"   # re-derive — see §0

# Hard precondition: a failed §1/§2 source query is indistinguishable from an empty queue, and
# §6 rewrites the digest WHOLESALE from $CURRENT — so a failure must suppress the rewrite, not
# fall through to it. §1/§2 write the marker; this reads it.
if [ -f "$SWEEP_TMP/source_query_failed" ]; then
  echo "SOURCE QUERY FAILED THIS PASS (§1 and/or §2) — skipping §6 rewrite and §7" \
    "notification; prior digest left untouched. Re-run /sweep next tick." >&2
  exit 1
fi

# The script refuses unless exactly one digest exists; a bare `.[0].id` would silently pick the
# first of several duplicates or yield "null" when none exists.
DIGEST_ID="$(scripts/sweep-digest-id.sh)" || exit 1
CURRENT="$(scripts/land-state-load.sh "$SWEEP_TMP/current" -- \
  "§3 did not run this pass")" || exit 1

LAST_BODY=$(bd show "$DIGEST_ID" --json | jq -r '.[0].description')
LAST_IDS=$(printf '%s\n' "$LAST_BODY" | grep '^SWEEP-ITEM' | awk '{print $2}' | sort -u)
CURRENT_IDS=$(printf '%s\n' "$CURRENT" | awk -F'\t' '{print $1}' | sort -u)

NEW_IDS=$(comm -13 <(printf '%s\n' "$LAST_IDS") <(printf '%s\n' "$CURRENT_IDS"))

# Persist NOW, before §6 (a later invocation) rewrites the body this block just read. §7 needs
# this exact value — the delta as it stood BEFORE the rewrite — but §7 runs after §6, by which
# point re-deriving from the digest would see the body §6 just wrote (LAST_IDS == CURRENT_IDS by
# construction) and always compute an empty NEW_IDS. `printf '%s'` with no trailing newline, so
# a legitimately-empty result lands as a ZERO-BYTE file rather than one blank line: §7
# distinguishes an absent file ("§5 never ran") from an empty one ("nothing new").
printf '%s' "$NEW_IDS" > "$SWEEP_TMP/new_ids"
```

**Two separate triggers, not one:**

- **Rewrite the digest** whenever `$CURRENT_IDS` differs from `$LAST_IDS` **at all** — an add *or* a
  remove. This is what makes a resolved item actually disappear from the record. (Rewriting only on
  additions leaves resolved items showing as zombies until the next unrelated add.)
- **Notify** (§7) only when `$NEW_IDS` is non-empty — a pure removal is quiet.

If the two sets are equal, nothing changed: skip the write entirely — a true no-op pass.

**Hard precondition — a failed source query suppresses the rewrite.** Stale is recoverable; silently
truncated is not.

## 6. Rewrite the digest (only when the queue changed, and every source query succeeded)

Body format — stable, line-oriented, trivially re-parseable by the next pass and readable by a human:

```
# Human-decision digest (auto-maintained by /sweep — do not edit by hand)

/sweep overwrites this description every pass the queue changes. It is /sweep's durable,
cross-machine dedup state — do not hand-edit it or delete the `sweep-digest` label.

Last swept: <ISO8601 UTC timestamp>

## Land/build escalations + human decisions (<N>)
SWEEP-ITEM <id> <kind> <title>
...
(none)

## Epics ready for a human close-decision (<M>)
SWEEP-ITEM <id> epic-ready-to-close <title>
...
(none)
```

A `SWEEP-ITEM` line is always exactly `<id> <kind> <title>` — **never** the optional 4th `.status`
field. The persisted digest is deliberately unannotated: an annotation would go stale the moment a
ticket's status flips without its id entering or leaving the set, since a rewrite only fires on an
id-set change. The `(deferred)` annotation lives only in the freshly-recomputed per-pass report.

```bash
# Same refusal as §5, load-bearing for a stronger reason: this block WRITES. Under §4's `N > 1`
# anomaly a bare `.[0].id` would overwrite whichever duplicate sorted first.
DIGEST_ID="$(scripts/sweep-digest-id.sh)" || exit 1
BODY_FILE="$(mktemp)"
# …write the digest body (format above) into "$BODY_FILE"…
bd update "$DIGEST_ID" --body-file "$BODY_FILE"
```

## 7. Notify (only when there is a new item to push)

I run as a **skill in the main conversation**, so I have the main session's tools — including
`PushNotification`, which reaches a human away from the terminal. That is the entire point of this
leg: these labels already sat in the tracker, where nobody was looking.

The new-id set is **read back from disk**, never re-derived — §6 has already rewritten the digest by
now, so re-deriving would compare the body §6 just wrote against itself and always produce an empty
delta.

```bash
SWEEP_TMP="${TMPDIR:-/tmp}/harness-sweep-state"   # re-derive — see §0

CURRENT="$(scripts/land-state-load.sh "$SWEEP_TMP/current" -- \
  "§3 did not run this pass")" || exit 1
# Existence, not content: an ABSENT file means §5 never ran and is a hard stop; a file that
# exists but is EMPTY is the ordinary "nothing new" pass and must run through to a clean no-op.
[ -f "$SWEEP_TMP/new_ids" ] || {
  echo "GATE COULD NOT RUN: $SWEEP_TMP/new_ids missing — §5 did not run this pass" >&2
  exit 1
}

# Split the new ids into "report it" (all) vs "push it" (not the deferred ones). Only
# escalated-sourced rows carry a 4th field, so a human/closable row's empty $4 is correctly
# never "deferred". Emit ONLY fields 1-3 — passing the row through whole would leak the raw
# status into every escalated report line. Truncate first, so a zero-match pass leaves these
# empty rather than stale.
: > "$SWEEP_TMP/new_annotated"
: > "$SWEEP_TMP/push_ids"
awk -F'\t' -v ann="$SWEEP_TMP/new_annotated" -v push="$SWEEP_TMP/push_ids" '
  NR == FNR      { new[$1] = 1; next }          # pass 1: the new ids
  !($1 in new)   { next }                       # pass 2: $CURRENT, new rows only
  { row = $1 "\t" $2 "\t" $3 }
  $4 == "deferred" { print row " (deferred)" > ann; next }
                   { print row > ann; print $1 > push }
' "$SWEEP_TMP/new_ids" "$SWEEP_TMP/current"
```

`new_annotated` is the full report block; `push_ids` is the (possibly smaller, possibly empty) subset
eligible for a push — a `deferred` row is dropped from the push, never from the report.

`PushNotification` is a **deferred** tool — its schema isn't loaded up front:

- `ToolSearch` with query `select:PushNotification` to make it callable.
- Call it **once per pass** (not once per item), and **only if `push_ids` is non-empty**, with a short
  summary: how many, and their ids/kinds.

Then **also** include a loud **NEW HUMAN-DECISION ITEMS** block in the §8 report — the full set,
deferred rows included. The two are complementary: the push reaches an away human; the report block
is what they read when they return to the transcript. If `ToolSearch` cannot resolve
`PushNotification` in this session, fall back to the report block alone and say so plainly — never
fail a pass over the notify channel.

## 8. Publish and report

Its own separate invocation, so the report-only lists are re-read from disk:

```bash
SWEEP_TMP="${TMPDIR:-/tmp}/harness-sweep-state"   # re-derive — see §0

# Deliberately NOT §3's fatal guard: §8 must finish either way (the digest push below is
# unrelated to these lists), so a missing file degrades that section alone. One 3-valued state
# per list, not a pair of booleans, so the three are mutually exclusive by construction.
# NOT retrofitted onto scripts/land-state-load.sh -- a missing $SWEEP_TMP/deferred is a
# non-fatal third state, which neither of that script's two policies expresses (both
# exit 1 on missing).
if DEFERRED="$(cat "$SWEEP_TMP/deferred" 2>/dev/null)"; then
  DEFERRED_STATE=ok
  [ "$DEFERRED" = "SWEEP-QUERY-ERROR" ] && DEFERRED_STATE=error
else
  DEFERRED_STATE=missing
fi

# NOT retrofitted onto scripts/land-state-load.sh -- a missing $SWEEP_TMP/stranded is a
# non-fatal third state, same reason as the deferred read above.
if STRANDED="$(cat "$SWEEP_TMP/stranded" 2>/dev/null)"; then
  STRANDED_STATE=ok
  [ "$STRANDED" = "SWEEP-QUERY-ERROR" ] && STRANDED_STATE=error
else
  STRANDED_STATE=missing
fi

# NOT retrofitted onto scripts/land-state-load.sh -- a missing $SWEEP_TMP/blocked_human is a
# non-fatal third state, same reason as the two reads above. §1 wrote this file (as part of
# partitioning $HUMAN), not a §2c block of its own -- see §2c's own note. That changes only
# WHICH block a `missing` state indicts, not the policy.
if BLOCKED_HUMAN="$(cat "$SWEEP_TMP/blocked_human" 2>/dev/null)"; then
  BLOCKED_HUMAN_STATE=ok
  [ "$BLOCKED_HUMAN" = "SWEEP-QUERY-ERROR" ] && BLOCKED_HUMAN_STATE=error
else
  BLOCKED_HUMAN_STATE=missing
fi

# §1/§2's shared marker, read from DISK like everything else here.
if [ -f "$SWEEP_TMP/source_query_failed" ]; then SOURCE_STATE=error; else SOURCE_STATE=ok; fi

scripts/bd-dolt-push.sh   # only if §6 wrote the digest

# The always-present "Actionable now" section, rendered LAST in the report — after the
# report-only sections and after the NEW HUMAN-DECISION ITEMS block (when present), not here.
# Source is every row of $SWEEP_TMP/current (§3), fields 1-3, EXCLUDING any row whose optional 4th
# field (the $ESCALATED-sourced .status, §1) is `deferred` — a deferred row is never listed here;
# it already appears in §2a's unchanged "Deferred (surfaced, not reviewed)" section, so no
# `(deferred)` annotation is needed. Report-only, feeds nothing (no digest change, §6 unchanged; no
# dedup state; no PushNotification change, §7 unchanged). Missing is fatal here the same way it is
# for §5/§7's own re-derivation of $CURRENT: §3 must have run for this section to have anything to
# show — a hard exit here is deliberate and does NOT contradict this block's opening note, which
# scopes "§8 must finish either way" to the three report-only lists (a missing $SWEEP_TMP/deferred
# is an ordinary third state; a missing $SWEEP_TMP/current means the pass itself never happened).
# It runs AFTER the digest push above and never before it precisely so that exit can never suppress
# the publish.
# An item appearing in both this section and the NEW HUMAN-DECISION ITEMS block above it is
# deliberate — "what's new" vs. "what's decidable now" answer different questions.
CURRENT="$(scripts/land-state-load.sh "$SWEEP_TMP/current" -- \
  "§3 did not run this pass")" || exit 1
ACTIONABLE_NOW=$(printf '%s\n' "$CURRENT" | awk -F'\t' '
  NF == 0 { next }
  $4 == "deferred" { next }
  { print $1 " " $2 " " $3 }
')
```

One rule over all three report-only lists — three mutually exclusive states, never confused:

- **`missing`** — the block that writes that section's file never ran this pass — §2a's or §2b's own
  block, or, for `blocked_human`, **§1** (which writes that file as part of partitioning `$HUMAN`;
  §2c has no block of its own). Body: "`<list>` list unavailable this pass"; summary field:
  `unavailable`, **never `0`**.
- **`error`** — that writing block *did* run, but its query failed and wrote the sentinel. Body:
  "`<list>` query failed this pass"; summary field: `error` — never `0`, and never `unavailable`,
  which is a different failure with a different remedy.
- **`ok`** — report normally, including the legitimately-empty case (`(none)`, count may be `0`).

None of the three aborts this block, and none suppresses or is suppressed by any other section —
each of the three lists is judged solely on its own file's content.

```
sweep: queue depth <N>, <M> new, <K> closable, <deferred field> deferred, <stranded field> stranded, <blocked_human field> blocked

## Deferred (surfaced, not reviewed) (<deferred field>)
<id> <title>
...
(none) | unavailable this pass | query failed this pass

## Stranded (in_progress 24h+, no pipeline label) (<stranded field>)
<id> <title>
...
(none) | unavailable this pass | query failed this pass

## Blocked human tickets (dependency-blocked, not yet decidable) (<blocked_human field>)
<id> <title>
...
(none) | unavailable this pass | query failed this pass
```

When `new_annotated` is non-empty, follow with:

```
## NEW HUMAN-DECISION ITEMS (<count>)
<id> <kind> <title>
<id> <kind> <title> (deferred)
...
```

A trailing `(deferred)` means the row is new to the queue this pass but its status is `deferred`: it
was **not** pushed (a human already saw and parked it), but it is not dropped from the report either —
and it may *also* appear in the Deferred section above. That double appearance is deliberate: the two
sections answer different questions ("what's new" vs. "what's parked") and a row can honestly be both.

**Finally, always append the `## Actionable now` section: every pass's report ends with the full
list of human decisions that are actionable RIGHT NOW, not just the delta.** Its rows are
`$ACTIONABLE_NOW` (computed in §8's script above): every row of `$SWEEP_TMP/current` (§3), fields
1-3, in full, EXCLUDING any row whose 4th field is `deferred` (that row already appears in §2a's
"Deferred (surfaced, not reviewed)" section, unchanged — no `(deferred)` annotation is needed here
since deferred rows are excluded outright):

```
## Actionable now (<count of rows in $ACTIONABLE_NOW>)
<id> <kind> <title>
...
(none)
```

This is distinct from the three report-only lists above (§2a/§2b/§2c list *parked/stranded/
not-yet-decidable* work `bd ready` already hides) and from the `NEW HUMAN-DECISION ITEMS` block
above it (that block is delta-only — new since the last digest, deferred rows included and
annotated). This section is the standing, decidable-now queue — `land-escalated`, open `human`, and
`epic-ready-to-close` rows minus anything deferred — every pass, so a human reading the transcript
never has to run `bd show` to see what is still waiting on them and can act on it without first
filtering out parked items themselves. It feeds nothing downstream: no digest change (§6 is
unchanged), no dedup state of its own, no `PushNotification` change (§7 is unchanged — the push
still covers only `$SWEEP_TMP/push_ids`, the NEW non-deferred ids). A row appearing here **and** in
the `NEW HUMAN-DECISION ITEMS` block on the same pass is deliberate, not redundant — "what's new"
vs. "what's decidable now" answer different questions.

If §4 found duplicate digests, or any query failed, say so plainly in the same report — the pass
still ends cleanly. A failed `bd blocked` query is both at once: it sets `$SOURCE_STATE = error`
(via §1's shared marker) *and* `$BLOCKED_HUMAN_STATE = error` (via its own sentinel) — report both,
not just one.

## Failure handling — a sub-step fails, the loop survives

A failed sub-step must never abort the pass — and must never corrupt the digest either. Those pull in
opposite directions, and **the digest wins**: it is rebuilt wholesale from `$CURRENT`, so a source
query that errors is indistinguishable from "that queue is empty", and rewriting on it would delete
real items from the durable record.

- A §1/§2 query error writes the shared marker; §5 checks it and exits before §6/§7 ever run, leaving
  the prior digest exactly as it was. **Stale, not truncated.** An *empty* result serializing as
  `null` is **not** a failure — the `(. // [])` guard normalizes it, so a queue that legitimately
  emptied still rewrites and drops the resolved item promptly.
- The §6 rewrite is all-or-nothing — it completes cleanly or is skipped; no partial write.
- Duplicate digests stop the write path for the pass; the anomaly is reported, never guessed at.
- **A report-only section's failure is isolated to that section alone**, in both directions — for
  §2a and §2b. **§2c is the one exception, deliberately:** its query is §1's own `bd blocked` call,
  so a failure there is *not* isolated the way §2a/§2b's are — it writes both
  `$SWEEP_TMP/blocked_human`'s `SWEEP-QUERY-ERROR` sentinel (for §8's report) *and*
  `$SWEEP_TMP/source_query_failed` (§1's shared marker, since a failed `bd blocked` must not be read
  as "nothing is blocked" — see §1's note). The rewrite-suppression half is real, not redundant
  caution.
- A failed pass still ends with a report and exit 0, so the next tick gets a clean shot.

## What I never do

- **Write the default branch, merge, close a ticket, or touch a producer's worktree.**
- **Dispatch any agent.** I read tracker state and write my own digest issue.
- **Resolve an escalation myself, or guess at a duplicate digest.** Surface, never decide.
- **Let the digest issue enter `bd ready`.** I claim it immediately on creation for exactly that
  reason.
- **Commit or `bd import` the passive JSONL export**, or record a design decision in a tracker note
  instead of `docs/`.
- **File to MISTAKES.md.** I touch no `git` and write no repo files at all (see
  [Non-goals](#non-goals--hold-the-line)), so an autonomous append is structurally out of reach here
  even for a qualifying finding — and this stage surfaces work *other* stages already stopped
  waiting on a human for, not something I discover firsthand. If a surfaced item itself meets the
  mistake log's bar, I note it in my report as a `MISTAKES.md CANDIDATE` block (the same block name
  `land-review` and `/epic-audit` use); filing it is for a human or a stage that can write repo
  files.
