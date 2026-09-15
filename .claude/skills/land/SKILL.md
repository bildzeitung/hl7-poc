---
name: land
description: Drain the ready-for-land queue — the SINGLE owner of every write to the default branch. Per pass: cheap-precheck each ready-for-land branch (drift + does it still merge — a conflict is kicked back needs-rebase, no review spent); semantic-review the survivors (via the land-review agent) → accept | bounce | escalate; batch-merge the accepted set --no-ff, re-gate once, isolate the culprit on red; then push, bd close the landed tickets, flag any epic whose last child this pass closed with epic-ready-to-audit, publish tracker state, and GC the merged land/<id> branches and local builder worktrees. Bounces open a new linked ticket carrying the findings; escalations leave the branch for a human and land nothing. Run self-paced as /loop 5m /land on ONE machine; a local lockfile guard skips a tick that would overlap a still-running land. Producers (/code) never land their own work — this skill does.
---

# land

I am the **lander** — the single, sole owner of every write to the default branch. Producers build
reviewed, green branches, push them to `origin/land/<id>`, and mark their ticket `ready-for-land`;
they **never** merge, close, or push. I am the other half of that contract: **nothing reaches the
default branch except through me.**

I run on the **primary checkout, on the default branch** — I am the *one* agent allowed to. Producers
are the inverse. I never touch a producer's worktree. I am typically invoked self-paced as
**`/loop 5m /land`** so I drain the queue while you work, with no daemon to manage.

**Run me from a strong-model session.** I am a skill, not a subagent — I have no model of my own and
inherit the session's. My semantic review and combined re-gate are exactly where that judgment earns
its keep.

## The merge decision belongs to the agent that didn't write the code

My **first task per branch is a semantic review I do not perform myself** — I dispatch the
[`land-review`](../../agents/land-review.md) agent. The independence is the point: the producer
already ran the *technical* review on its own branch with gates green; I add the *semantic* gate —
*should this land?* — from the outside. I do not re-run the technical review and I assume the branch
is green until my re-gate says otherwise.

## Governing rule: no fenced block may depend on shell state from another

**I run each fenced `bash` block below as its own, separate Bash tool invocation. Nothing carries
over** — not variables, not arrays, not function definitions, not `trap`s, not `set -e`/`pipefail`,
not background jobs. Anything one block needs from an earlier one is either **re-derived** (cheap,
deterministic — e.g. `$(git rev-parse --git-dir)`) or **persisted to a file** under `$STATE_DIR`
(`.git/land-state/`, which survives `git reset --hard` because that only touches the index and
working tree). Logic shared by two call sites lives in `scripts/`, never in a bash function defined
in one block and called from another.

This is not a style preference — it is a defect this skill has already shipped. One section
populated a `declare -A MSG` associative array that a later section's merge loop read back; by the
time that loop ran, `MSG` was empty, and `git merge -m ''` failed with **completely empty stdout and
stderr**. Every such failure is silent by default, and this is the one skill that writes the default
branch — so **any block that loads state must also assert it loaded** and abort loudly if it did not.
A loop that iterates zero times and exits 0 is indistinguishable from a clean pass with nothing to do.

---

## 0. Single-lander lock — acquire FIRST, every tick

Serialization comes from **(a)** a local "skip if already running" lockfile and **(b)** the
convention that the loop runs on **one machine**.

The lock spans the whole pass, across every fenced block — the shape the governing rule says cannot
survive a `trap` or a `$$`. It therefore lives in `scripts/land-lock.sh`, keyed on a wall-clock
staleness token rather than a PID (which is always already dead by the time a later block reads it).

**The token is a heartbeat, not a one-shot stamp** — re-stamped once per ticket in Section 2a, on
every `land-merge-one.sh` call, and at the two boundaries whose cost grows with queue size (before
Section 1a, top of Section 4). The combined re-gate runs unheartbeated; it does not grow with queue
size.

**Released at exactly two sites:** Section 1's empty-queue exit and the end of a full pass in
Section 4. **Every other stop leaves the lock held until it ages out** (default 1800s). That is
deliberate: a TTL that asks nothing of any exit site cannot be broken by a future "stop the pass"
that forgets to release. Do not add per-exit releases.

An all-bounced / all-kicked-back pass is **not** a stop — it flows through to Section 4 normally.

```bash
STATE_DIR="$(git rev-parse --git-dir)/land-state"    # re-derive — fresh Bash invocation
mkdir -p "$STATE_DIR"
# On a non-zero acquire, this skip line goes to STDERR and points AT the diagnostic
# land-lock.sh already printed there — whose wording distinguishes a transient "another
# /land is still running" from a permanent MACHINE FAULT (flock missing, an unwritable lock
# dir), the distinction a reader of the loop's output needs. Deliberately NOT `2>&1` into
# $ACQUIRE_OUT: the script's token contract is its STDOUT, and on the SUCCESS path that
# variable feeds the token parse below.
#
# stderr is ALSO captured to a scratch file so the failure branch can inspect it for the
# script's own escalation marker without a second `acquire` call (which would double-count
# its consecutive-fault counter). Both files live under ${TMPDIR:-/tmp}, deliberately NOT
# under $STATE_DIR: an unwritable git dir IS the headline fault being escalated, and a
# redirect into the git dir fails BEFORE `acquire` runs at all.
ACQUIRE_ERR_FILE="${TMPDIR:-/tmp}/land-lock-acquire-stderr"
ACQUIRE_OUT="$(scripts/land-lock.sh acquire 2>"$ACQUIRE_ERR_FILE")" || {
  ACQUIRE_ERR="$(cat "$ACQUIRE_ERR_FILE" 2>/dev/null || true)"
  echo "$ACQUIRE_ERR" >&2
  echo "land: could not acquire the lock this tick — skipping. Read land-lock.sh's own" \
    "diagnostic immediately above: a MACHINE FAULT there is PERMANENT on this machine and" \
    "blocks landing until a human fixes it — not an overrunning tick." >&2
  # land-lock.sh only DETECTS a persistent fault and marks it with a distinctly-prefixed
  # stderr line — it stays tracker-free by design. THIS is the one place that reaches a
  # human: open a `human`-labeled ticket, which /sweep already surfaces. Keyed by a fixed
  # title and FILED ONCE per fault episode: a fault that persists for days is thousands of
  # ticks. The ticket EXISTING is the signal; closing it re-arms filing.
  if grep -q 'land-lock: ESCALATE' "$ACQUIRE_ERR_FILE" 2>/dev/null; then
    # `--limit 0`, and NO `--status open` — bd list already excludes closed issues, while
    # pinning `open` would miss this very ticket once a human moves it to in_progress and
    # then duplicate it every tick. The title goes through jq's `env.` builtin rather than
    # `--arg` so a `$name` binding inside the jq program can't trip the cross-block scanner.
    export ESCALATION_TITLE="land-lock: persistent MACHINE FAULT is blocking /land on this machine"
    EXISTING_ESCALATION="$(bd list --label human --limit 0 --json \
      | jq -r '(. // [])[] | select(.title == env.ESCALATION_TITLE) | .id' | head -1)"
    if [ -z "$EXISTING_ESCALATION" ]; then
      bd create --type=decision --label=human --title="$ESCALATION_TITLE" \
        --description="scripts/land-lock.sh acquire has hit a persistent MACHINE FAULT under
/loop 5m /land on this machine, past its escalation threshold. This is not a routine
overrunning pass; it will not self-heal, and every tick keeps skipping until a human fixes
the underlying cause named below.

Filed ONCE per fault episode, not refreshed per tick — the live diagnostic is in the loop's
own output. Do NOT run \`scripts/land-lock.sh acquire\` by hand to check: on a machine that
has since been fixed it would take the lock out from under the loop. Close this ticket once
the machine is fixed; a recurrence opens a fresh one.

Diagnostic at the time of filing:
$ACQUIRE_ERR"
      bd dolt push
    fi
  fi
  exit 0
}
echo "$ACQUIRE_OUT"
# Persist THIS pass's own acquire token to disk for every later heartbeat/release call site
# — a file, not a variable, because no shell state survives between blocks. It lives OUTSIDE
# $STATE_DIR because Section 1's per-pass scratch wipe would otherwise delete it before any
# consumer read it: this is lock state, not per-pass scratch.
# Loud-fail if the pattern doesn't match rather than silently persisting an empty token.
printf '%s\n' "$ACQUIRE_OUT" \
  | grep -oE 'token [0-9a-f]+' | cut -d' ' -f2 > "$(git rev-parse --path-format=absolute --git-common-dir)/land-lock-token"
[ -s "$(git rev-parse --path-format=absolute --git-common-dir)/land-lock-token" ] || {
  echo "land: could not parse this pass's own token out of: $ACQUIRE_OUT" >&2
  # RELEASE BEFORE BAILING. We hold the lock as of two lines ago, and this is the only exit
  # path in the whole skill that aborts while holding it — without this, a parse bug wedges
  # landing for the FULL staleness window. The explicit blind sentinel is on purpose: we
  # could not parse our own token, and nothing else can have taken the lock in the
  # microseconds since `acquire` succeeded.
  scripts/land-lock.sh release --land-lock-blind   # land-lock-blind-ok: the one sanctioned opt-out, see above
  exit 1
}
```

**Convention:** run the loop on **one machine only** — the local lock does not cross machines.

---

## 1. Setup the pass — tracker-authoritative, fetch origin

**Refuse to start unless I am actually in the primary checkout.** Do not reach for
`-C "$(git rev-parse --show-toplevel)"` instead: `--show-toplevel` resolves against **cwd**, so it
restates the cwd you already have and, from a worktree, resolves to *that worktree's* root. It reads
as a guard and is not one. `assert-main-checkout.sh` uses `--git-common-dir`, which does
distinguish them: every worktree shares one common `.git`, and only the primary checkout's toplevel
is its parent.

**The guard is the FIRST LINE OF THE SAME fenced block as the commands it protects — never its own
block.** Each block is a separate Bash invocation, so a guard in its own block can only `exit` *that*
shell, leaving "does the destructive block run?" to my judgment. Sharing a block makes `||` enforce
it: `git reset --hard` is unreachable unless the assertion passed.

```bash
scripts/assert-main-checkout.sh || exit 1   # STOP — everything below assumes this passed
bd dolt pull            # the tracker DB is authoritative; pull the latest claim/label/close state
git checkout -f main    # I land ON the default branch, in the primary checkout (just asserted)
  # `-f` so this cannot FAIL — not to clean anything; the reset below does that by itself.
git fetch origin        # I need origin/main and every origin/land/<id> fresh
STATE_DIR="$(git rev-parse --git-dir)/land-state"
rm -rf "$STATE_DIR"     # per-pass scratch the reset below cannot clear — see below
git log --oneline origin/main..main   # expected EMPTY; non-empty = residue, printed before it goes
  # Residue here is BY CONSTRUCTION merge commits (a pass that died between Section 3's merges
  # and Section 4's push), so this print must be faithful — the reset below destroys them.
git reset --hard origin/main   # pass-start reset, NOT `pull --rebase` — see below
```

On a non-zero exit the pass stops there — the script's stderr names cwd, the primary checkout, and
why (exit 1 = wrong directory; exit 2 = a machine fault rather than a location verdict). Every
command after it runs unqualified, because the assertion is what guarantees cwd already *is* the
primary checkout, which a cwd-derived `-C` never could.

**Why a hard reset, not `pull --rebase`.** I am the only **agent** that writes the default branch, so
at pass start local should already be bit-for-bit `origin`. The only legitimate difference is a
**previous** pass that died between Section 3's merges and Section 4's push — an exit-2 gate stop, or
an ungraceful crash. Those merges were never gated green on their own and never reached origin:
**residue, not work.**

**This is the *only* place residue is cleared** — every exit site below points here rather than
restoring the branch itself. Residue therefore survives until the *next* pass starts, which buys
self-healing from a bare crash: a killed pass runs no exit-site code at all.

The reset does **not** refuse on a dirty tree, unlike `pull --rebase` — which is why it absorbs a
staged tracker export, and also why it can destroy genuinely uncommitted work. Discarded *commits*
stay in `git reflog`; discarded *uncommitted* work does not. Hence the `git log` above, printed
while the residue still exists. **Do not "simplify" this back to `pull --rebase`.**

**`checkout -f` is there so the checkout cannot *fail*, not to clean anything.** A pass killed
mid-merge leaves an unmerged index, and a bare `checkout` then fails rc=1 *even when already on the
right branch* — stopping the pass at its second command in exactly the crash case this reset heals.
The checkout is needed at all because `reset --hard` moves whatever ref HEAD is on: detached, it
would leave the branch untouched.

**Keep `git reset --hard` as the block's LAST command.** No block here runs under `set -e`, so the
last command's status is the only machine-readable signal the block gives — and `rm -rf` reports
success even on a path that does not exist.

**Section 4's push must stay after Section 3's re-gate.** `recycled-worktree-guard.sh` resets agent
worktrees onto `origin/main` on the premise that `/land` only ever advances that ref with
already-gated content — a property of this step order, not of any lock. Every launch worktree
branches from it, so a reorder's blast radius is every fresh agent worktree.

Then read the queue — every ticket carrying **`ready-for-land`** (it stays `in_progress`; the label,
not the status, is the queue):

```bash
bd list --label ready-for-land --status in_progress --limit 0 --json
```

**`--limit 0` is load-bearing.** A truncated read wouldn't lose a branch outright, but it would
silently under-report and under-land a large backlog, every pass.

If the queue is empty, release the lock and stop:

```bash
scripts/land-heartbeat.sh --release
exit 0
```

Otherwise heartbeat once here, so Section 1a's `O(n²)` work below is isolated as the sole contributor
to this stretch rather than 1a plus Section 1's networked calls combined. Failure is logged but never
stops the pass:

```bash
scripts/land-heartbeat.sh
```

---

## 1a. Compute the stacked-branch graph — once per pass, from git, never from the tracker

A producer sometimes must build one `land/<id>` branch **on top of** another still-unlanded
`land/<base>` — merging it in — because its ticket only makes sense once the base's code exists.
Nothing about a stacked branch's *content* announces this; I detect it purely from **git history**. A
producer records a `builds_on` field as a breadcrumb, but that is redundancy and intent only — I
never trust it as the mechanism.

**This is `scripts/stacked-graph.sh`, not something I derive by hand.** It was inline bash here and
did not even parse — its outer loop was a comment, so the O(n²) driver got re-improvised every pass,
untested, for an algorithm whose failure is silent. Run it once per pass:

```bash
STATE_DIR="$(git rev-parse --git-dir)/land-state"   # re-derive — fresh Bash invocation
mkdir -p "$STATE_DIR"
# REDIRECT, never `| tee`: a pipeline reports the LAST command's status, so `| tee` would
# hand back tee's always-0 exit and swallow the script's exit 2 — leaving a truncated or empty
# $STATE_DIR/graph that the merge scripts below read as a valid "no stacks" graph. That is exactly how
# a dependent gets merged before its base, which is what the exit-2 rule below exists to prevent.
scripts/stacked-graph.sh --base-ref origin/main --report-unordered > "$STATE_DIR/graph" || exit 1
cat "$STATE_DIR/graph"   # the visibility `tee` used to give, now that the status is the script's own
```

Output is one tab-separated record per line:

- `EDGE  <dependent>  <base>  direct` — `<base>` is `<dependent>`'s **nearest** base. This is the one
  2c hands `land-review` to diff against; a *transitive* base would make the diff carry the
  intermediate branch's work as if it were this branch's. Section 3a orders the merge set off these.
- `EDGE  <dependent>  <base>  transitive` — still a real dependency. `direct` + `transitive` together
  are the **full relation**, which is what Bounce and the escalation paths need to ask "does deleting
  this branch strand a live descendant?"
- `UNORDERED <a> <b>` — related but with no derivable direction. Treat as **related**: do not delete
  either branch without the descendant question being answered by a human.

**Exit 2 is a machine fault, never "no stacks."** Stop the pass and surface it: a query that could not
run must not be read as an empty graph, because that is precisely how a dependent gets merged before
its base.

The detection rule, the direction test, and the two known gaps (force-push; branched-from-base rather
than merged-base) live in the script's own header and are pinned by `tests/test_stacked_graph.py`,
which builds real repos for each case — including the moved-base-tip and two-merge-base shapes that a
plausible reimplementation gets wrong. **Do not re-derive this logic here.**

A producer records a `builds_on` field as a breadcrumb, but that is intent only — the graph always
comes from git.

Run it **once** per pass, into `$STATE_DIR/graph`. A file, not a variable: no shell state survives
between the blocks that need it, and `land-merge-batch.sh` reads it to decide which dependents a
conflicting base takes with it. `$STATE_DIR` is wiped at the top of every pass (Section 1), so the
graph can never be carried over from a prior one — a branch can be bounced, dropped, or landed in
between.

---

## 2. Vet each branch — cheap prechecks first, then the semantic review

Steps 2a and 2b are **cheap gates I run before spending a strong model on the semantic review** — a
branch that has drifted or no longer merges cleanly is disqualified on mechanics alone.

### 2a. Re-validate that the tracker and git haven't drifted

The landing context is **minimal by design** — `land_head` + a one-line `land_summary`, read via
`bd show <id> --json` (the branch name is *derived*, never stored). The SHA exists only to **detect
drift**: a push onto the branch *after* the ticket was marked ready.

**First action of every iteration: heartbeat the lock.** This is the call site that keeps the
staleness token measuring idle time rather than the pass's total duration — it fires once per ticket,
right before that ticket's review dispatch, so the gap the TTL must outlast is one dispatch, not the
sum across the queue.

```bash
scripts/land-heartbeat.sh
BD_JSON="$(bd show <id> --json)"
LAND_HEAD="$(jq -r '.[0].metadata.land_head // empty' <<<"$BD_JSON")"
# Shape-check BEFORE comparing to anything. Exit 1 = malformed/missing metadata; exit 2 = this
# call is broken. `|| exit $?` is load-bearing: there is no `set -e` here, so without it the
# block would run the drift comparison on a value the check just rejected — the exact thing
# the check exists to prevent — while preserving the 1-vs-2 distinction.
scripts/validate-sha40.sh land_head "$LAND_HEAD" || exit $?
git ls-remote origin "refs/heads/land/<id>"   # branch must still exist on origin...
# ...and origin/land/<id>'s tip SHA must equal $LAND_HEAD
```

**Why the shape check, before the comparison.** A metadata write has no schema — a truncated or
hand-retyped value (one hex digit short) writes just as cleanly as a real one, and a malformed value
never equals a real branch tip, so without this check it reads as ordinary drift and the branch is
thrown back on that basis — which for this section means a **bounce**, superseding the ticket and
dropping the branch: a self-inflicted rebuild of work that was already correct.

**Deliberate asymmetry — do not "harmonize" it.** This check is **exact-match**, not the
ancestor-check the technical reviewer uses for `review_head`. Same predicate, different questions:
`/land` lands **without** re-reviewing, so a forward push of never-reviewed commits genuinely is
drift here; the reviewer reviews the whole branch regardless, so a forward push is harmless there.

A **missing branch** or a **SHA mismatch** is drift — treat it exactly like a review **bounce**. A
**malformed `land_head`** is a **distinct** outcome — neither drift nor a real mismatch, since there
is no well-formed value to compare — and it is an **escalate**, never a bounce and never an in-pass
repair. Concretely: **keep the branch**, land nothing from it, label `land-escalated`, and say
explicitly "malformed `land_head` metadata, not drift" plus what the human owes: re-derive the value
mechanically (`git rev-parse` / `git ls-remote`, never retyped), re-write the field, and re-enter at
`ready-for-land`.

**Why escalate rather than bounce or repair.** A corrupt hand-off record means no drift evidence
exists at all, and whether the branch is nonetheless the reviewed one is a human judgement. Bouncing
would delete the very branch whose field the remedy asks a human to re-write. Repairing in-pass is
worse: `land_head` records **what the reviewer saw**, so re-deriving it yields the *current* tip and
the comparison becomes tip == tip — deleting the drift check while leaving it green.

### 2b. Cheap conflict precheck — does it still merge?

A branch that forked long ago is fine as long as it still merges clean: `--no-ff` handles non-linear
history, and Section 3 re-gates the *merged* result. Only a **textual conflict** disqualifies it —
and discovering that at merge time means already having paid for a semantic review of contents the
rebase will change.

```bash
# $STATE_DIR is wiped once per pass in Section 1; re-derived here, fresh invocation.
STATE_DIR="$(git rev-parse --git-dir)/land-state"
CONFLICTS_DIR="$STATE_DIR/conflicts"
mkdir -p "$CONFLICTS_DIR"

# A command substitution inside an `if` condition is exempt from `set -e` — unlike a bare
# `VAR=$(cmd)` assignment, which would abort the shell before `rc=$?` is ever reached.
if CONFLICTS=$(scripts/merge-precheck.sh origin/main "origin/land/<id>"); then
  rc=0
else
  rc=$?
fi

# $CONFLICTS does NOT survive to the kick-back block below — that is a SEPARATE Bash
# invocation. Persist it now, at the only point this block actually holds it.
#
# An `if`, NOT `[ "$rc" = 1 ] && printf ...`. As this block's LAST command that AND-list makes
# the whole invocation exit 1 whenever rc is 0 — so the COMMON clean path would report failure
# and a real conflict would report success, INVERTING the only signal this block gives.
# Testing `= 1` and not `!= 0` is also load-bearing: a machine fault (rc=2) leaves $CONFLICTS
# empty and must NOT leave a file behind for a later kick-back to read as a conflict record.
if [ "$rc" = 1 ]; then
  printf '%s\n' "$CONFLICTS" > "$CONFLICTS_DIR/<id>"
fi
```

- **`rc=0`** → clean; proceed to 2c.
- **`rc=1`** → textual conflict. `$CONFLICTS` holds exactly the conflicting paths, one per line. →
  needs-rebase kick-back: skip the semantic review, leave the merge set. **Do that kick-back now, for
  this branch, while still in Section 2** — 3a computes the accepted set from outcomes that include
  "kicked back", so a branch reaching 3a un-kicked-back is out of order on its own terms.
- **`rc=2`** → **MACHINE FAULT, not a branch conflict** (old git, an unreadable ref, or the tool
  failing). I do **not** kick this branch back — a machine fault blaming an innocent branch is
  exactly the defect this precheck's extraction closed. **Stop the pass** and surface the script's
  own stderr verbatim as a human decision.

A conflict is **neither a bounce nor an escalate** — the branch's *content* may be perfectly fine, it
simply can't replay onto where the default branch now is.

### 2c. Run the semantic gate

**Dispatch `subagent_type: "land-review"` via the Agent tool — no `isolation` argument at the call
site.** Its own agent definition carries `isolation: worktree`, so the requirement travels with the
*role*: any dispatch lands isolated whether or not the call site remembers to ask.

That matters here more than anywhere. I run on the default branch, in the primary checkout — **the
same working tree Section 3 merges into.** A non-isolated review dispatch runs *in that tree*, and
nothing stops a reviewer from leaving files staged or modified there. Observed: three non-isolated
dispatches all ran in the primary checkout; one left a full branch diff staged, and the next branch's
merge aborted with "would be overwritten by merge" — with `git ls-files -u` empty, so it hit neither
the retry path nor the real-conflict path, and silently read as an unretried conflict.

Pass the ticket ID and its branch. **If 1a's direct-edge map found this ticket stacked on exactly one
live base**, also pass that base — land-review diffs against it instead of the default branch. If it
found *no* live base, or (rare) more than one direct base, hand it nothing extra — but in the
multi-base case, note in the dispatch which other live land branches this one contains, so
land-review doesn't misread their content as scope creep.

- **accept** → add the ticket to the **merge set**.
- **bounce** → handle per [Bounce](#bounce--clear-failure): new ticket carrying the findings,
  supersede the original, **drop the branch** — unless that would strand a live descendant, in which
  case it escalates instead.
- **escalate** → land **nothing** for it, **keep the branch**, label it, surface the question.

Collect verdicts for the whole queue before merging — I want the full accepted set so I can
**batch**-merge.

---

## 3. Batch-merge the accepted set, re-gate once, isolate on red

Two branches each green *in isolation* can break when **combined** (a clean git merge with broken
behaviour). So I merge the whole accepted set, then re-gate **once**.

**Every re-gate here runs in the FOREGROUND, in the same turn, and its verdict is read from its own
real exit status.** No `run_in_background`, no `Monitor`, no ending a turn on a pending gate.

**Never pipe a gate through `tail`/`head`/`grep` and read the pipeline's status as the gate's own.**
A pipeline reports its **last** element's status, so a killed gate surfaces as "completed, exit 0".
Observed: `nox -s tests 2>&1 | tail -30` hung, was killed, and was reported exit 0 — that 0 was
`tail`'s, while the captured output ended in `Session tests failed`. To trim output, capture the
real status first (`set -o pipefail`, `${PIPESTATUS[0]}`, or `cmd > file; status=$?; tail -30 file`).

Misreading a gate matters more here than anywhere: **I am the last gate.** Nothing re-checks what I
certify, so a false green pushes unverified content straight out.

**A gate that was killed or never completed is neither green nor a content red.** It must not land
anything and must not bounce anything — nothing failed on its *content*. Re-run it cleanly with the
real status captured, then decide.

### 3a. Order the accepted set — base before dependent; hold an orphaned dependent

An unordered merge set is unsafe for a stacked branch: merging a dependent *before* its base drags
the base's unreviewed content in under the wrong ticket's name, and a dependent whose base never made
it into this pass's accepted set must not land at all this pass.

Using 1a's **direct edges**, restricted to the accepted set:

- **The base is also accepted** → the dependent must merge *after* it. A plain topological sort
  (Kahn's algorithm) handles any depth of stacking from these direct edges alone.
- **The base is not accepted** → **hold** the dependent: pull it out of this pass's merge set
  entirely (it does not merge, conflict-isolate, or bounce this pass), and leave a note:

  ```bash
  bd update <id> --append-notes "HELD (/land, stacked-branch ordering): land/<id> is stacked on
  land/<B>, which is not landing this pass (<B>'s outcome: <bounced|escalated|needs-rebase|not yet
  ready-for-land>). Re-evaluated automatically once <B> lands or its outcome resolves — no action
  needed unless <B> itself needs a human decision."
  ```

  It stays `ready-for-land` and simply re-enters the semantic review next pass, by which point either
  the base has landed (so its own diff now naturally excludes the base's content) or the hold note
  explains why it's still waiting.

**The invariant that outlives this step: a base that leaves the merge set takes its dependents with
it.** Ordering up-front is not sufficient, because a base can still drop *out* later in Section 3 —
by a real merge conflict (kicked back) or by turning the gate red during isolation (bounced). In both
cases the loop would carry on to a dependent still in the set, merge it, and land the departed base's
just-rejected content under the *dependent's* ticket name. So whenever a branch leaves the merge set
**for any reason**, **drop every dependent of it too** (the *full* relation, so transitive dependents
go as well), with the same HELD note.

**Pre-compute every merge message before the first merge — no tracker call inside the merge loop, and
persisted to a FILE, never a bash variable.** The summary comes from `metadata.land_summary` or the
title.

> **What stages the export is NOT established.** Measured: neither a tracker read nor a tracker write
> dirties the tree on its own. `bd dolt pull` is *suspected* and did not reproduce. Do not upgrade
> that hedge into a settled fact, and do not remove the restore below on the strength of a causal
> story: the staged-export failure is real and observed, the export is **by invariant never work**,
> so restoring it unconditionally is correct whatever the trigger turns out to be.

```bash
STATE_DIR="$(git rev-parse --git-dir)/land-state"    # under .git/ — survives a later `git reset
MSG_DIR="$STATE_DIR/msg"                             # --hard` (that only resets index+worktree)
CONFLICTS_DIR="$STATE_DIR/conflicts"
mkdir -p "$MSG_DIR" "$CONFLICTS_DIR"   # $STATE_DIR is wiped once per pass, in Section 1

# Capture the accepted set to a file HERE, at the one moment I actually hold it. Every later
# block RE-READS this file instead of having the ids restated by hand: ids are opaque
# identifiers, and the derive-identifiers fiat rules out hand-transcribing them — doubly so
# here, where the ORDER is load-bearing and a silent slip merges a dependent before its base.
printf '%s\n' $ACCEPTED > "$STATE_DIR/accepted"
: > "$STATE_DIR/landed"    # appended to by the merge loops below; Section 4 reads it back

for id in $(cat "$STATE_DIR/accepted"); do
  SUMMARY=$(bd show "$id" --json | jq -r '.[0].metadata.land_summary // .[0].title')
  printf '%s' "Merge land/$id: $SUMMARY ($id)" > "$MSG_DIR/$id"
done
```

**Re-derive `STATE_DIR`/`MSG_DIR` at the top of every later block that needs them.** Deriving
`$(git rev-parse --git-dir)` fresh is cheap and deterministic, not "state assumed to survive"; what
persists across blocks is the **files** on disk, never the shell variables naming their location.

The accepted set genuinely cannot be *re-derived* after the fact — it encodes per-branch judgment
that is not queryable from git or the tracker — but **"cannot be re-derived" is not "cannot be
persisted"**: the block above captures it at the one moment I do hold it. The landed set is better
still: the merge loops **append** to it as each branch actually merges, so it is derived from what
happened rather than recalled.

**Every block below that loads one of these files asserts that it LOADED.** All cross-block loads go
through `scripts/land-state-load.sh`, whose two policies (default = missing fatal / empty OK;
`--require-nonempty` = both fatal) are the *only* two — a new load site picks one by argument rather
than hand-rolling a fifth `cat` spelling. What the assertion separates is not "empty" from "a real
merge", but its two **causes**: a file that was never written (3a never ran — the silent failure,
aborted loudly) from a file written empty (every branch legitimately left the set — allowed through).
**Do not re-add an emptiness test on top of that load** — in `scripts/land-merge-batch.sh` (which
owns the first-pass loop now) or anywhere else — that conflates the two again.

Before merging anything, unstage the passive export — unconditionally. A staged export means its
index blob differs from `HEAD` while the worktree matches the index, so `git diff` reads **clean** in
that state and the drift is invisible right up until `git merge --no-ff` refuses with "Your local
changes would be overwritten by merge." A bare `git checkout --` does **not** fix this: it only
overwrites the worktree, leaving the staged index entry, so a naive retry loops.

```bash
git restore --staged --worktree .beads/issues.jsonl 2>/dev/null || true
```

**A failed `git merge` is not automatically a textual conflict.** Classify on the actual failure:
`would be overwritten by merge` in stderr *with* an **empty** `git ls-files -u` is the passive-export
trap, not a conflict — restore and retry the same merge once. Only a genuinely unmerged index
(`git ls-files -u` non-empty) is a real textual conflict.

That retry-and-classify logic is `scripts/land-merge-one.sh`, not an inline bash function. The
function used to be defined in one fenced block and called again from the isolation-replay loop below
— but that loop only runs after a reset and a re-gate, each its own separate invocation, so the
function had already vanished. A script exists on disk identically for both call sites. It reads its
message from the `MSG_DIR` files, communicates a real conflict's paths back over **stdout**, and
carries the same 0/1/2 contract (0 = merged, 1 = real conflict, 2 = machine fault). It also asserts
its own primary-checkout identity internally, so this block needs no separate guard.

```bash
STATE_DIR="$(git rev-parse --git-dir)/land-state"   # re-derive — fresh Bash invocation; nothing
                                                    # from 3a persists except the FILES it wrote
MY_TOKEN="$(cat "$(git rev-parse --path-format=absolute --git-common-dir)/land-lock-token" 2>/dev/null || true)"
[ -n "$MY_TOKEN" ] || echo "land: WARNING -- no own-token available; land-lock ownership check is" \
  "DISABLED for this call " >&2
scripts/land-merge-batch.sh \
  --accepted "$STATE_DIR/accepted" --landed "$STATE_DIR/landed" \
  --msg-dir "$STATE_DIR/msg" --conflicts-dir "$STATE_DIR/conflicts" \
  --graph "$STATE_DIR/graph" --own-token "$MY_TOKEN"
```

It merges in 3a's order and prints one record per branch:

| Record | Means | What I owe it |
|---|---|---|
| `LANDED <id>` | merged cleanly, appended to the landed file | nothing |
| `CONFLICT <id>` | real textual conflict; paths in `conflicts/<id>` | a **needs-rebase kick-back** |
| `HELD <id>` | its base left the merge set, so it did too | a **HELD note** — not conflicted, not rejected, just no foundation this pass |
| `SKIPPED <id>` | already out of the set before its turn came | nothing; it carries a `HELD` record too |
| `FAULT <id>` | machine fault | **stop the pass** — never a bounce, never a kick-back |

**Exit 0** = everything merged. **Exit 1** = ran fine, some branch needs the follow-up above.
**Exit 2** = machine fault: stop, land nothing further, surface the script's own stderr.

It runs no gates and writes no tracker state — every `bd` write and every judgment stays mine. It
also enforces 3a's invariant at the point that invariant actually bites: a conflicting branch takes
its dependents out of `$STATE_DIR/accepted` **in the file**, via `scripts/drop-from-accepted.sh`,
before the loop can reach them. `--graph` is what makes that transitive; omit it only on a pass with
no stacks at all.

Re-gate the combined result. A **docs-only** merge set has no code gate — skip it, and run the
diagram validator only if a merged diff touched a diagram.

**If the loop merged NOTHING — the landed file is empty, the all-bounced / all-kicked-back pass —
skip this re-gate entirely and go straight to Section 4.** The branch is byte-identical to the origin
ref Section 1 fetched, whose content is by construction already gated, so there is nothing this pass
introduced to certify. This is a cost decision, not a correctness nicety: without it, every
all-bounced tick pays a full test run to re-certify content already carried, and any red it found
could only be pre-existing breakage this pass neither caused nor could attribute.

```bash
uv run --frozen nox -t fix && uv run --frozen nox -s tests && scripts/harness-tests-gate.sh --base-ref origin/main && uv run --frozen nox -s build_members && uv run --frozen nox -s lock_currency
```

`harness-tests-gate.sh` runs the harness's own suite (`nox -s harness_tests`, `tests/harness/`)
only when the merged set touched a harness path — `scripts/`, `.claude/`, `tests/harness/`, the
build files — against `origin/main`, the ref Section 1 fetched and nothing has pushed over yet.
Otherwise it prints one line and exits 0: those pins can only change verdict when those files do.

`build_members` actually builds each workspace member's wheel/sdist (`uv build --all-packages`) —
`nox -s tests` cannot catch a packaging misconfiguration, since `uv sync` installs every member
editable regardless of what a real build would produce. It runs here, before `lock_currency`, so
its own exit 2 (missing `uv`) is not the last word in the chain.

`lock_currency` catches a stale dependency lock here — locally, before public CI does. A branch that
bumped a dependency without regenerating the lock (or whose merge with another accepted branch
changed the resolved graph) fails it with **exit 1**, treated identically to a red test run.

**Exit 2 from any of these is NOT a red gate — it is a machine fault, and isolating on it bounces an
innocent branch.** On exit 2 I do **not** isolate, bounce, or land: I stop the pass and surface the
message verbatim. `lock_currency` is **last** in the `&&` chain for exactly this reason — an `&&`
chain reports its last-run command's status, so anything after it would mask the 2. Keep it there.

**Neither exit-2 stop restores the local branch** — deliberately; that is Section 1's job.

- **Green** → proceed to Section 4.
- **Red** → **isolate.** The combined merge is bad but I don't know which branch. `land-replay.sh`
  resets back to origin and replays the accepted set **one at a time** in 3a's order, re-gating after
  each, keeping every branch that stays green:

  ```bash
  STATE_DIR="$(git rev-parse --git-dir)/land-state"
  MY_TOKEN="$(cat "$(git rev-parse --path-format=absolute --git-common-dir)/land-lock-token" 2>/dev/null || true)"
  [ -n "$MY_TOKEN" ] || echo "land: WARNING -- no own-token available; land-lock ownership check" \
    "is DISABLED for this call " >&2
  scripts/land-replay.sh \
    --accepted "$STATE_DIR/accepted" --landed "$STATE_DIR/landed" \
    --msg-dir "$STATE_DIR/msg" --conflicts-dir "$STATE_DIR/conflicts" \
    --state "$STATE_DIR/replay-state" --graph "$STATE_DIR/graph" \
    --base-ref origin/main --own-token "$MY_TOKEN"
  ```

  **This is a loop I drive, not a single call.** The script works to a deadline (its own default,
  well under the tool cap) because the replay runs `4 + 4N` gate sessions and a straight-through run
  would hit that cap mid-attribution — leaving nothing bounced, so the next pass rebuilds the same
  set and reds again. Re-invoke it, unchanged, until it stops asking for more:

  | Exit | Record | What I do |
  |---|---|---|
  | 0 | `SURVIVOR <id>` … | done — every remaining branch is merged and green. Go to Section 4 |
  | 1 | `CULPRIT <id>` | the branch is already **backed out of the tree**. **Bounce** it (or escalate, if the descendant check finds a live dependent), then **re-invoke** |
  | 3 | `MORE <n>` | deadline reached, progress persisted. **Re-invoke immediately** — nothing else to do |
  | 2 | — | machine fault, or a baseline red that no branch can be blamed for. Stop the pass, land nothing further, surface the script's stderr |

  It also emits `CONFLICT <id>` / `HELD <id>` for a branch that conflicts with an earlier survivor —
  a kick-back, not a bounce, handled exactly as in the first pass.

  **It reports the culprit; it never bounces one.** Bouncing needs the live-descendant check, a
  supersede, and a rebuild ticket carrying land-review's findings — judgment plus tracker writes, both
  mine. It likewise does **not** drop a culprit's dependents, because I may escalate rather than
  bounce, and only I know which. When I do bounce, I drop them myself before re-invoking:

  ```bash
  scripts/drop-from-accepted.sh <culprit-id> \
    --accepted "$(git rev-parse --git-dir)/land-state/accepted" \
    --graph "$(git rev-parse --git-dir)/land-state/graph"
  ```

  Each `HELD` line it prints is a dependent that owes a HELD note. Skipping this re-merges a
  dependent whose base just failed the gate, putting the failing content back under a different
  ticket's name.

  **Why the baseline inside it is not optional.** No gate is a pure function of the tree, so a red one
  may have nothing to do with the accepted set — and this loop *deletes* what it blames. An ambient
  environment variable in the landing shell, set nowhere in the repo, once reddened the suite on a
  bare origin ref with nothing merged; trusting it would have closed an innocent ticket, opened a
  rebuild carrying a fabricated "turned the gate red" finding, and deleted a reviewed branch. The
  script baselines every gate before attributing anything, once per replay rather than once per
  resume.

  **Every "stop the pass" exit leaves the local branch exactly as it sits.** I restore none of them;
  that is Section 1's job.

---

## 4. Land the survivors

Only now — combined and green — do I write the world. **Order matters:** push first, then close, then
publish tracker state, then GC branches and local worktrees.

**Do not hoist this push above Section 3's re-gate.**

First, check whether the re-gate's formatter actually changed anything:

```bash
git status --short
```

- **Empty** → nothing to commit; skip.
- **Non-empty** → stage **only** the explicitly-named reformatted source paths. Never `-A` (it once
  swept in an unrelated pre-existing untracked directory under a misleading `style:` message), and
  never rely on a pathspec exclude to keep the passive export out — the tracker's own pre-commit hook
  re-exports and re-stages it on *every* commit regardless of what was `git add`-ed, so the commit
  must skip hooks too.

  **This commit names no ref or path at all — the one git write in this section that doesn't.** Every
  other one below is ref- or path-addressed and therefore cwd-independent. This one commits to
  whatever branch cwd's `HEAD` happens to be on, and run from the wrong directory that is not a loud
  failure: it silently commits the reformat to that directory's branch, and the push below then
  pushes without it — green all the way through. So this fence needs its own guard:

  ```bash
  scripts/assert-main-checkout.sh || exit 1   # STOP — this commit is not ref-addressed at all
  git add <path> <path> ...                     # explicit reformatted source paths only
  git commit --no-verify -q -m "style: formatter on merged main"   # --no-verify: skip the
                                                # pre-commit hook so it can't re-stage the export
  git show --stat HEAD                          # confirm only the intended paths rode along
  ```

**MISTAKES.md — narrow, explicit exception to "report the patch, not the gap."** This check is owed
**every `/land` pass that reaches a verdict on at least one branch — not conditional on this pass
having an accepted set to merge.** It lives here, in Section 4, because that is where the push
already sits and the common case (some branches landed) reaches it naturally here. But `land-review`
returns a `MISTAKES.md CANDIDATE` on every verdict, not only bounce/escalate, so a pass where every
branch bounces, kicks back `needs-rebase`, or escalates — leaving no accepted set, and on some paths
never reaching this section at all — still owes this check. If this section runs this pass, it runs
here, as below. If it does **not** — no branch was merged and nothing below this point executes — the
identical block runs instead from [Stop and report](#stop-and-report)'s own entry point for that
case, which also pushes the entry itself (`git push origin main`), since no merge push follows it
there. Before the push below, check whether this pass surfaced a qualifying mistake — either one I
noticed myself this pass, or a `MISTAKES.md CANDIDATE` block a `land-review` dispatch returned this
pass — it cannot commit from its disposable worktree, so filing is mine. "Qualifying" is the mistake
log's bar: it destroyed or risked real work, or shipped a wrong artifact, **and** a concrete
prevention rule can be derived from it — not every bounce or drift. If nothing qualifies this pass,
skip this block entirely. If something does:

```bash
scripts/assert-main-checkout.sh || exit 1     # same reason as the reformat commit above
# Dedup by INCIDENT, not exact wording -- one exact phrase would miss the same root cause
# re-described in different words. -i: case-insensitive; -E: alternate a few candidates -- the
# ticket id if one exists, the file/script/mechanism at fault, and a couple of paraphrases of the
# failure -- rather than one fixed sentence.
grep -niE "<ticket id>|<mechanism or file at fault>|<a paraphrase of the failure>" MISTAKES.md
```

- **Already present** (a prior stage — a producer in its worktree, the code-reviewer, an earlier
  `/land` pass — already filed the same incident) → skip. Entries are append-only; do not double-file.
- **Not present** → append a new entry at the **top** of the log (newest first) — what happened /
  root cause / consequence / the rule that prevents a repeat — then commit it **directly on `main`**.
  This file is append-only prose with no gate risk (no code, no tests, nothing the gates or a
  technical review would catch), so unlike an ordinary "gap" its remedy is never a decision that
  needs review:

  ```bash
  scripts/assert-main-checkout.sh || exit 1     # fresh Bash invocation; this commit names no ref or path either
  git add MISTAKES.md
  git commit --no-verify -q -m "docs: record <short incident name> in MISTAKES.md"   # --no-verify: same pre-commit hook reason as above
  git show --stat HEAD   # confirm only MISTAKES.md rode along
  ```

  This commit rides in the same push as the reformat commit above and the merge commits already on
  `main` — I do not push separately per entry.

```bash
git push origin main
git status                 # MUST show up to date with origin

# Heartbeat here so every per-ticket close, the networked publish, every branch delete, and the
# worktree-GC sweep below sit strictly BETWEEN this call and the pass-end release. That stretch
# is the ordinary GREEN path, it grows with the number of tickets landed, and it runs during the
# exact window the default branch is being written.
scripts/land-heartbeat.sh

# The ids that actually stayed merged — read back from the file Section 3's loops appended to,
# never restated by hand. On the Green path that is the accepted set minus any mid-loop
# kick-backs; on the Red path the replay loop truncated the file and re-recorded only what it
# kept, so bounced culprits and held dependents are already excluded. An EMPTY file is
# legitimate and correctly closes nothing; a MISSING one means Section 3 never ran.
STATE_DIR="$(git rev-parse --git-dir)/land-state"
LANDED=$(scripts/land-state-load.sh "$STATE_DIR/landed" -- \
  "Section 3 never ran (or never reached its end-of-loop write). Nothing to close.") || exit 1
for id in $LANDED; do
  bd close "$id" --reason "Landed via /land (merge <sha>)"
  bd update "$id" --remove-label ready-for-land
    # Tidy the queue label off the now-closed ticket — symmetric with the kick-back, escalate,
    # and bounce exits, which have always done this. Keep it AFTER the close: a crash between
    # the two leaves only a benign stale label, whereas stripping FIRST would strand an open,
    # label-less ticket outside the queue for good — the label, not the status, is the queue.
done

# Closing the last child of an epic completes it — flag it for the closing-side review. I only
# NOTICE completion here; the review itself is /epic-audit. epic-completion-check.sh walks to
# the parent epic and decides whether it is now fully child-complete and not already flagged.
# It is extracted, not inlined, so it carries its own fixture-backed tests: the inline jq this
# replaced was DEAD CODE for months (it read `bd show`'s `.dependents`, populated only with the
# opt-in --include-dependents flag, so the array was always absent and a false-positive guard
# silently ate every pass) and no gate would ever catch a markdown-embedded jq snippet
# regressing. The script is read-only; this loop is the one place that writes the label.
for id in $LANDED; do
  RESULT=$(scripts/epic-completion-check.sh "$id")
  [ -z "$RESULT" ] && continue
  PARENT=$(printf '%s' "$RESULT" | awk '{print $2}')
  bd label add "$PARENT" epic-ready-to-audit   # /epic-audit picks it up
done

scripts/bd-dolt-push.sh   # publish closes, epic flags, and any bounce tickets

for id in $LANDED; do
  git push origin --delete "land/$id"   # GC the merged remote branch — a bare ref delete, not a
                                        # worktree/uncommitted-work risk, so this stays per-ticket
done
```

### Local worktree + branch GC

**This end-of-pass sweep is the ONLY local worktree/branch reclaim**, and it discovers worktrees
live from `git worktree list --porcelain` rather than trusting per-ticket metadata that can drift.

**The contract.** Any worktree under `.claude/worktrees/` that is **unlocked**, **clean**, and
**either** has not diverged from the default branch **or** — if branch-attached — has not diverged
from its own branch's origin counterpart, is reclaimable, whoever made it. The second arm exists
because an **escalated** ticket's reviewer worktree never merges by definition; content pushed to
`origin/land/<id>` is captured just as safely.

The predicates all live in `scripts/worktree-gc-classify.sh`, extracted so they are lint-checked and
unit-tested rather than unreachable in a markdown fence. This block owns the sweep-level contract;
the script's header owns the per-arm detail.

**Why the dirty check exists on top of the ancestry arms.** A worktree freshly branched off the
default branch is trivially "merged" by **zero divergence**, so that proxy reads TRUE for a live,
uncommitted build the instant it is created. `git -C "$WT" status --porcelain` tests the actual
invariant. **A dirty tree is never reclaimed** — regardless of lock state, ancestry, or origin. An
unguarded zero-divergence read once destroyed two builds' uncommitted work outright. The check also
distinguishes "clean" (proceed) from "could not tell" (skip): `status --porcelain` prints nothing in
both cases.

The dirty check, not `locked`, is what protects the worktree classes that raise no lock — an
interactive session, a human's hand-made worktree, an exited agent's scratch. Only two things lock:
the **harness**, for the lifetime of an agent standing in a launch worktree; and the **producer**,
which locks its build worktree explicitly because it unlocks again at its first commit.

**Two accepted residuals**, both chosen so the failure direction is "remove an empty checkout" and
never "destroy uncommitted work":

- The sweep reclaims strictly **less** than the per-ticket loop it replaced. A landed builder
  worktree that is dirty, locked, or carries unpushed commits is now *kept* — the dirty case
  permanently, until a human clears it.
- A **clean** worktree at zero divergence raising no lock is still reclaimable, so the directory can
  vanish out from under whoever is standing in it. Nothing is destroyed.

**One unenforced coupling keeps this sweep reclaiming anything at all: `.gitignore`.** A finished
worktree is full of untracked build junk (`.venv/`, `.nox/`, `__pycache__/`) and reads clean ONLY
because those are ignored. Un-ignore one and every worktree reads dirty and the sweep silently
reclaims *nothing*. Re-check this whenever you touch `.gitignore`.

```bash
scripts/worktree-gc-sweep.sh
```

It prints one summary line per sweep plus one per bare-ref backstop. **"reclaimed 0 of 0" (nothing to
do) reads differently from "reclaimed 0 of N" (everything was skipped)** — a regression that zeroes
out GC must be visible here, not indistinguishable from idle. Exit 2 is a machine fault or a wrong
checkout; it never means "nothing to reclaim".

Then release the lock — the pass is fully done:

```bash
scripts/land-heartbeat.sh --release
```

`bd close` unblocks dependents — that is *why* the lander closes and the producer never does: a
closed ticket frees the next layer of `bd ready`. Closing is mine because the merge decision is mine.

The worktree GC is **best-effort and machine-local**. Builds can happen on several machines, and a
worktree on another machine simply isn't in this machine's list — that machine's own lander reclaims
it. **On a bounce or escalate the builder's worktree never satisfies either ancestry arm**, so its
**branch ref** is what survives, not its directory: once its last commit ages past the floor and its
tree is clean, the dir-only arm reclaims the directory and keeps the ref, so every commit stays
reachable but the checkout doesn't persist indefinitely. A **dirty** builder worktree is never
touched, in any bucket.

---

## Needs rebase — kick back

A **needs-rebase** is the outcome of the 2b precheck or a Section-3 textual conflict: the branch
**can't merge**, but its content was never judged bad — I never ran the semantic review on it. It is
a **third outcome, distinct from bounce and escalate**: not a rebuild (nothing is wrong with the
work), not a human decision (there's nothing to decide). So I keep everything and hand it straight
back to the producer.

**This block is its own separate Bash invocation from whichever producer detected the conflict** —
none of that block's shell state survives. Read the conflicting paths back from the file, and
refuse — loudly — rather than kick back with a blank paths section:

```bash
STATE_DIR="$(git rev-parse --git-dir)/land-state"   # re-derive — the FILE is what survived
CONFLICTS=$(scripts/land-state-load.sh "$STATE_DIR/conflicts/<id>" --require-nonempty -- \
  "the producer site did not persist the conflicting paths. Refusing to kick back with a" \
  "blank paths section.") || exit 1

bd update <id> --remove-label ready-for-land --add-label needs-rebase \
  --append-notes "NEEDS REBASE (/land): origin/land/<id> no longer merges cleanly onto
main @ $(git rev-parse --short origin/main).
Conflicting paths:
$CONFLICTS
/code's step-0 pickup merges current main into land/<id>, re-gates, commits, and pushes the
result itself (an ordinary, non-force push), then swaps needs-rebase back to ready-for-land."
scripts/bd-dolt-push.sh
# The branch is KEPT. The build worktree is KEPT. No supersede, no new ticket, no close.
```

The ticket stays `in_progress`; the `needs-rebase` label is now its state. **`/code` picks this up
automatically** on its next invocation — no human nudge needed unless the merge itself conflicts and
the two sides genuinely disagree.

## Bounce — clear failure

A **bounce** is a confident "this branch should not land as-is" (an unmet acceptance criterion,
silent scope creep, a violated invariant, a wrong approach — or drift from 2a). The original ticket
is **superseded** by a fresh ticket carrying the findings, so a producer can rebuild from a clean
brief.

**Before doing anything else: check for live descendants.** A bounce **deletes** the branch, and a
prior pass did that with no idea another live branch had already merged it in. Deleting does not
delete its commits from the dependent: the dependent went on carrying the rejected content —
including the very defect the bounce was rejecting — on a foundation that no longer existed and would
never land. So **read the descendants straight out of 1a's map** (the *full* relation). Do **not**
re-derive it with an ad-hoc `--contains` probe against the tip — that is the tip test 1a exists to
avoid.

- **No descendants** → proceed with the bounce below.
- **A live descendant found** → do **not** silently drop the branch. Escalate instead:

  ```bash
  bd update <id> --remove-label ready-for-land --add-label land-escalated \
    --append-notes "ESCALATION (/land bounce): land-review bounced this branch (findings below),
  but land/<dep> is a LIVE branch that already merged land/<id>'s commits — deleting land/<id> now
  would silently strand land/<dep>, which would carry the very defect this bounce is rejecting.
  Needs a human decision: FOLD (supersede both into one combined rebuild ticket), SEQUENCE
  (rebuild <id> alone; <dep> stays parked until the rebuild lands, then rebases onto it), or DROP
  (neither is wanted — close both).

  LAND-REVIEW FINDINGS: <verbatim>"
  scripts/bd-dolt-push.sh
  # BOTH branches are KEPT until the human resolves it.
  ```

  The dependent itself is left exactly as it is — this escalation doesn't touch it.

**The ordinary bounce.** Derive the blocks-dependent set **with its exit status tested**, THEN create
the rebuild ticket, THEN mark the original superseded:

```bash
# Derive blocks-dependents FIRST, before anything else changes state. Capture the output so the
# exit status is testable — a bare `for DEP in $(...)` discards it. If OTHER tickets depend on
# <id> via a `blocks` edge, the supersede below CLOSES <id> — so the tracker treats that blocker
# as satisfied and those dependents unblock PREMATURELY, while the real work still sits unbuilt
# in the rebuild. Re-pointing each dependent onto the rebuild is what prevents that.
#
# Extracted to a script, unlike the inline jq this replaced: that snippet was correct but
# ungated, and a dropped re-point here fails silently UNSAFE. But a derivation regression isn't
# the only way this goes wrong: a RUNTIME failure (tracker missing, DB locked, an unresolvable
# id) makes the script exit non-zero, which no gate on its internals can catch — only the caller
# reading its exit status can. That is what this `if !` does.
if ! DEPS=$(scripts/blocks-dependents.sh <id>); then
  bd update <id> --add-label land-escalated --remove-label ready-for-land \
    --append-notes "ESCALATION (bounce): scripts/blocks-dependents.sh <id> failed at runtime while
deriving blocks-dependents ahead of a supersede. Bounce does not proceed blind — superseding
without a reliable dependent list risks re-pointing nothing while blocks-dependents silently
unblock against an unbuilt rebuild. No rebuild ticket was created; land/<id> is kept. Retry once
the underlying failure clears."
  scripts/bd-dolt-push.sh
  # STOP with a STATEMENT, not a comment. A bare `# STOP` is INERT: control would fall through
  # the `fi` straight into the create/supersede/delete below, superseding the ticket anyway —
  # the exact "proceed blind" outcome this guard exists to prevent.
  exit 1
fi

NEW=$(bd create --type=<same-type-as-original> \
  --title="<original title> (rebuild after land bounce)" \
  --description="Rebuild of <id>, bounced by /land semantic review.

REBUILD BRIEF (from land-review):
<the findings + what the rebuild must satisfy that the bounced branch did not>" \
  --json | jq -r '.id')

# Preserve epic parentage BEFORE superseding — otherwise supersede closes the child and the epic
# loses it, reading falsely "complete" while the real work sits in an unlinked ticket.
# Re-parenting keeps completion accounting honest: the superseded child closes, but NEW is an
# open child, so the epic stays incomplete until the rebuild lands.
PARENT=$(bd show <id> --json | jq -r '.[0].parent // empty')
[ -n "$PARENT" ] && bd dep add "$NEW" "$PARENT" --type=parent-child

# Re-point the non-parent dependents derived above (captured before $NEW existed).
for DEP in $DEPS; do
  bd dep add "$DEP" "$NEW"   # DEP now depends on the rebuild, not the superseded original
done

bd supersede <id> --with "$NEW"   # links and AUTO-CLOSES <id> as superseded
bd update <id> --remove-label ready-for-land   # tidy the queue label off the now-closed original

git push origin --delete "land/<id>"    # drop the rejected branch
scripts/bd-dolt-push.sh
```

`bd supersede` **closes** the original — superseded means *replaced*, and the new ticket is the live
work. It is the one case where the landing side closes an `in_progress` producer ticket; a normal
accept closes via Section 4, and an escalate never closes.

### Branch disposition on a bounce — drop (default) vs. keep-for-lift

- **DROP (default)** — the finding is about the branch's *own* content: an unmet criterion, a wrong
  approach, a violated invariant, scope creep. Nothing there survives review unchanged.
- **KEEP-FOR-LIFT** — reserved for the *fold* resolution of a strand escalation: the **dependent's**
  branch (not the bounced base's) is kept when its content is judged independently sound, and the
  combined rebuild ticket says explicitly **"lift verbatim from `land/<dep>` @ `<sha>`"** rather than
  re-describing the same design from scratch. The **base's** branch is still dropped.

**A kept branch is not GC'd for free — say so in the rebuild ticket.** Section 4 deletes only the
branches in the landed set, and a kept branch belongs to a ticket that was *superseded*, not landed:
it will never appear there, so nothing deletes it automatically. The rebuild ticket must carry the
disposal instruction alongside the lift pointers — **"lift verbatim from `land/<dep>` @ `<sha>`;
delete `land/<dep>` once this ticket lands."** Until then it stays visible to 1a, which is correct: a
kept branch really does still contain its base's commits.

## Escalate — genuine decision

An **escalate** is a real question only a human can answer. I land **nothing** for it, **keep its
branch**, mark it, and surface the question — **without blocking the rest of the batch**:

```bash
bd update <id> --add-label land-escalated --remove-label ready-for-land \
  --append-notes "ESCALATION (/land semantic review): <the decision needed, with options>"
scripts/bd-dolt-push.sh
# origin/land/<id> is KEPT until the human resolves it.
```

A `land-escalated` branch is never touched by an automated sweep — only the human-driven resolutions
below remove the label and let the branch go.

## Resolving a `land-escalated` branch

`land-escalated` is **not terminal** — a human resolves it, and every resolution **removes the
label**, so the queue can reach empty. Resolution is a human action taken outside a pass; `/land`
only ever *sets* the label. There are exactly four exits.

### (a) Land as-is — materialize the decision first, then re-enter the queue

If the human decides the branch **should** land, the branch itself needs no change. What changes is
the *ticket*: swapping the label back with nothing else touched is **not a complete transition**,
because the next pass re-dispatches the review, which hits the *same* ambiguity and escalates again —
an infinite loop.

So the swap is valid only once the human has **written the decision into the ticket**. The review
stays authoritative on re-review; there is deliberately **no "human-blessed" bypass label**.

```bash
bd update <id> --acceptance="<revised, unambiguous acceptance criteria>"
  # land-review reads acceptance_criteria as the contract — this is the field that must change.
  # The BRANCH is untouched.
# If resolving this required a COMMIT to land/<id> — which it does whenever the decision belongs
# in docs/ rather than a tracker field — refresh land_head, or Section 2a reads the commit you
# just made as DRIFT and prescribes a bounce next pass. This exit is the exposed one because it
# re-enters with no reviewer in between; exits (b) and (d) route through a reviewer, which
# refreshes land_head itself.
# --set-metadata (upsert), NOT --metadata (a whole-blob replace that drops the other keys).
# Derive-then-validate before the write — a malformed value here reads as drift on a later pass.
LAND_HEAD="$(git rev-parse origin/land/<id>)"                                 # omit if nothing committed
scripts/validate-sha40.sh land_head "$LAND_HEAD" || exit $?
bd update <id> --set-metadata land_head="$LAND_HEAD"
bd update <id> --remove-label land-escalated --add-label ready-for-land
scripts/bd-dolt-push.sh
```

### (b) Rebuild — supersede into a fresh ticket, drop the branch

Resolve it exactly like a bounce: new ticket carrying the decision, supersede, drop the branch (same
epic re-parent and dependent re-point care applies).

**Before dropping the branch, run the same descendant check as Bounce** — an escalated branch can
have picked up a live stacked dependent while it sat waiting, and a *stale* graph from the pass that
escalated it is no use here, so recompute 1a's relation against the live refs. If it finds a live
descendant, apply the fold/sequence/drop framing instead of proceeding blind.

```bash
NEW=$(bd create --type=<same-type-as-original> \
  --title="<original title> (rebuild after land-escalated)" \
  --description="Rebuild of <id>. Human resolution of the escalated decision:
<the decision + what the rebuild must satisfy that the escalated branch did not>" \
  --json | jq -r '.id')
# re-parent onto the same epic / re-point blocking dependents — see Bounce for why.
bd supersede <id> --with "$NEW"
bd update <id> --remove-label land-escalated
git push origin --delete "land/<id>"
scripts/bd-dolt-push.sh
```

### (c) Drop — close with reason, GC the branch

**Same descendant check before deleting** — a dropped branch's commits are just as live inside a
dependent as a bounced branch's would be. A live descendant means dropping also strands that
dependent's foundation: surface that as part of this same decision rather than deleting silently.

```bash
bd close <id> --reason "<why this is dropped>"
bd update <id> --remove-label land-escalated
git push origin --delete "land/<id>"
scripts/bd-dolt-push.sh
```

### (d) Amend and re-gate — fix the already-landed defect, keep the branch and ticket

Two triggers:

**Trigger 1 — `/land`'s combined re-gate.** Applies when the escalation was raised by the combined
re-gate, not by the semantic review or a producer gate, and all of:

- the semantic review **accepted** this branch (the escalation happened *after* review);
- the merge precheck was clean;
- the re-gate failure is traceable to code **already on the default branch**, not to anything this
  branch introduces — the branch would have gated green before whatever landed the defect, and gates
  red now only because it is the first thing to exercise the defective landed code.

Under those conditions neither other exit fits: "land as-is" is for a branch that needs no change,
and this one does; "rebuild" discards a branch already judged sound, which is the wrong instrument
for a defect that isn't the branch's fault.

**Trigger 2 — a semantic-review escalation whose resolution requires a scoped on-branch edit.**
Trigger 1's three conditions attach to it only. This trigger's sole condition is that the human
decides the fix requires editing the branch, rather than landing as-is, rebuilding, or dropping.

Either way the human amends the branch and sends it back **one gate earlier than a normal "land
as-is"**, at `ready-for-code-review`: the amendment is new, ungated content the original accept never
saw, so it needs its own technical review before a semantic re-review is worth spending.

**Write the added scope into the acceptance criteria, not only into a note** — the re-entered branch
still has to clear `ready-for-land`, where the next pass re-runs the semantic review, which reads
acceptance criteria as the contract. An amendment recorded only in notes reads to that re-review as
scope creep on a branch it already accepted.

```bash
bd update <id> --acceptance="<original criteria + what the amendment must satisfy>"
bd update <id> --remove-label land-escalated --add-label ready-for-code-review \
  --append-notes "RESOLVED (human, amend-and-re-gate): <the landed defect + the fix>"
scripts/bd-dolt-push.sh
# The human may amend the branch before the swap, or leave it to the code-reviewer that /code's
# stranded-review sweep dispatches. Either way it re-gates and re-pushes.
```

### Re-entry per escalating source — re-enter at the gate that escalated

Whichever gate could not resolve the ambiguity is the gate that re-runs once it is resolved — the
same gate, against the now-unambiguous ticket, never a later gate taking the resolution on faith.

| escalated by | exit | re-entry label |
|---|---|---|
| `/land` semantic review, resolution needs **no** branch edit | (a) | `ready-for-land` |
| `/land` semantic review, resolution needs **a** branch edit | (d) | `ready-for-code-review` |
| `code-reviewer` technical review | (a) | `ready-for-code-review` |
| `coding` rebase-pickup conflict | (a) | `needs-rebase` |
| `coding` build-time clarification | (a) | `ready-for-code-review` |
| `/land` combined re-gate (defect already landed) | (d) | `ready-for-code-review` |
| `/land` §2a malformed `land_head` metadata | (a) | `ready-for-land` |

The two semantic-review rows are an explicit **no branch edit / a branch edit** pair so they cannot
both match one escalation. The discriminator is **not** the escalation but a property of the human's
*resolution* — whether it requires editing the branch. Every row follows the same shape: write the
decision into the ticket first, then swap the label and publish.

**Re-entering at `ready-for-code-review` MUST also (re)write `metadata.review_head` as part of the
same resolution.** This hand-edit happens outside any `/code` run, and nothing else on this path
forces the field to exist — `/code`'s stranded-review sweep will not dispatch a reviewer until a
non-empty `review_head` can be established (deliberately: it will not guess a head to review), so
omitting this step used to strand the ticket forever, `in_progress` and invisible to everything but a
repeated "needs a human" line. That sweep now derives the field itself as a **backstop**, not a
substitute. Validate before writing — an `ls-remote` that resolves nothing prints nothing, and an
unguarded write would put an *empty* value on the ticket, re-creating the state this exists to
prevent:

```bash
SHA="$(git ls-remote origin "refs/heads/land/<id>" | cut -f1)"
scripts/validate-sha40.sh review_head "$SHA" && bd update <id> --set-metadata review_head="$SHA"
```

**The build-time case is the deliberately arguable one.** A build-time escalation means the producer
stopped mid-build, so its branch is green-but-possibly **incomplete** and never reached
`ready-for-code-review` on its own. Re-entering it there hands the reviewer a branch that may not
implement the whole ticket. That trade-off is accepted — routing every trivially-answerable build
question through a full rebuild would over-charge a question the branch may already answer correctly
— under three conditions:

1. The human writes the resolved answer into the ticket **before** flipping the label.
2. The reviewer is not obliged to pass a half-built branch: **escalate** is its standing non-pass
   outcome, so a build-time re-entry asserts only that the *ambiguity* is resolved, not that the
   branch is *finished*. The semantic review's **bounce** verdict is the backstop if it slips past.
3. Re-entry means "the decision is made, re-run the pipeline from technical review" — **not** "this
   branch is done."

---

## Tracker-sync discipline (non-negotiable)

I am the system's heaviest tracker writer, and the repo runs **`import.auto: false`**: **Dolt is
authoritative; `.beads/issues.jsonl` is an export-only passive artifact, never a sync wire.**

- **Pull at the start, push after writes.** `bd dolt pull` opens the pass;
  `scripts/bd-dolt-push.sh` (retry-on-reject: backoff + re-pull between attempts, since a concurrent
  producer's write can transiently reject or lock-contend) follows *every* batch of writes.
- **Never commit the JSONL export, never `bd import` it.** Import only upserts and silently misses
  deletions. `import.auto: false` already stops the post-merge hook from re-importing a stale export
  and reverting a close; do not re-enable that path.
- **Never let the passive export block or enter a merge.** Unstage it right before the merge loop,
  every pass, on the assumption it may be staged even when `git diff` says otherwise.
- **Order so a close can't be reverted by a stale export.** Push, then close, then publish — the
  authoritative close lives in Dolt and is pushed immediately.

---

## What I never do

- **Land work I can't verify.** Drift, a textual conflict, a red re-gate, or a bounce verdict all stop
  a branch from landing this pass.
- **Rebase a producer's branch myself, or review a branch that won't merge.** A branch failing 2b is
  kicked back for the *producer* to merge in its own worktree.
- **Land on a bounce or escalate, or skip the semantic review.** The review is the *first* task per
  branch; only an accept enters the merge set.
- **Run two landers at once**, or run the loop on more than one machine.
- **Commit the passive export, or `bd import` it** in place of `bd dolt pull`.
- **Touch a producer's worktree, or record a design decision in a tracker note** instead of `docs/`.
- **Delete a branch without first checking for a live descendant.**
- **Merge a stacked dependent before its base, or land a dependent whose base isn't in this pass's
  accepted set.**
- **Trust `builds_on` metadata as the mechanism for detecting a stacked branch.** It's a breadcrumb;
  always derive from git containment.
- **File a ticket for an incidental discovery** — something I notice about `/land`'s own mechanics
  mid-pass, not a per-branch verdict. I **report** it instead; the human reading that report decides
  whether it becomes a ticket. (This rule is about **tracker tickets**; a MISTAKES.md append is a doc
  write, and its sanctioned path is in [Section 4](#4-land-the-survivors).) Nothing is lost by not filing: every pass executes this skill's own
  code, so the observation recurs on its own. (Three passes once noticed the same dead check and
  filed three duplicates.) This does not touch the two sanctioned `bd create` paths — the bounce
  rebuild ticket and exit (b) — both per-branch verdicts, my actual job. And not filing is **not**
  the same as leaving work for the human: see below.

## Stop and report

### MISTAKES.md filing on a pass that never reaches Section 4

If this pass reached [Section 4](#4-land-the-survivors) and ran its own MISTAKES.md block, that
already covers this pass — nothing further to do here. But whenever this pass ends with **no accepted
set to merge** on a path that skips Section 3 and Section 4 entirely (every branch bounced, kicked
back `needs-rebase`, or escalated; or the pass stopped early on a machine fault), the check is still
owed — `land-review` can return a `MISTAKES.md CANDIDATE` on any verdict, not only when a branch also
happens to land. Before releasing the lock, check whether this pass surfaced a qualifying mistake —
one I noticed myself, or a `MISTAKES.md CANDIDATE` a `land-review` dispatch returned this pass — using
the same bar and the same block [Section 4](#4-land-the-survivors) uses (the mistake log's bar; dedup
by incident via `grep -niE`, never one exact phrase). If nothing qualifies, skip this entirely. If
something does, run it here, verbatim:

```bash
scripts/assert-main-checkout.sh || exit 1     # same reason as Section 4's copy
grep -niE "<ticket id>|<mechanism or file at fault>|<a paraphrase of the failure>" MISTAKES.md
```

- **Already present** → skip. Entries are append-only; do not double-file.
- **Not present** → append a new entry at the **top** of the log (newest first) — what happened /
  root cause / consequence / the rule that prevents a repeat — then commit it directly on `main`,
  same as Section 4's copy:

```bash
scripts/assert-main-checkout.sh || exit 1
git add MISTAKES.md
git commit --no-verify -q -m "docs: record <short incident name> in MISTAKES.md"
git show --stat HEAD   # confirm only MISTAKES.md rode along
```

**This path has no merge push to ride in on — push it myself, right here**, since Section 4's own
`git push origin main` never runs on this path:

```bash
scripts/assert-main-checkout.sh || exit 1
# This push must carry the MISTAKES.md doc commit and NOTHING ELSE. Section 4's re-gate is the
# only thing that certifies merge output, and it did not run this pass -- so if anything other
# than the single commit just made is unpushed, this path is not the right one to push it.
# STOP and report instead; never advance origin/main past un-re-gated content.
test "$(git rev-list --count origin/main..main)" = 1 || exit 1
git push origin main
git status                 # MUST show main up to date with origin
```

When the pass ends I release the lock and report: how many branches I reviewed; which **landed**
(with the merge SHA, in merge order); which I **kicked back** (they never reached the semantic
review); which I **bounced** (and the superseding ticket IDs); which I **escalated** (and the decision
each owes a human); which I **held** as an orphaned stacked dependent and what base it's waiting on;
any **epic** I flagged for audit; anything that **drifted**; and any **incidental discovery**, named
here rather than filed. On any genuine ambiguity in the landing mechanics themselves — not a
per-branch verdict — I stop and surface it rather than guess.

### If the whole remedy is a one-line doc change, report the patch — not the gap

Naming a one-line fix as a "discovery" and stopping there hands the human a research task: re-find
the surface, re-derive the wording, decide whether it earns a ticket. For a remedy that small the
ticket costs more than the fix. So whenever I can state the remedy in a line or two, I report it as a
patch the human can apply directly, with all three of:

- **The exact replacement text**, written out in full — the words to paste, never a description of
  what they should say.
- **Where it goes** — file path plus the line number I actually **derived this pass** (`grep -n` at
  report time, never recalled or estimated) *and* the anchor line **quoted verbatim**, so the
  location survives the number going stale.
- **What it changes**, in one sentence, so the human can accept or reject without opening the file.

**I still do not apply it.** I run on the default branch in the primary checkout, and a doc edit typed
here reaches it with no branch, no technical review, no semantic review and no re-gate — bypassing
every gate this skill exists to be. The patch text is a **hand-off**.

**The escape hatch, stated so it isn't quietly stretched.** This applies only when the remedy is
purely *how to word it*. The moment the fix needs a judgment call about *what to say*, it is no
longer a one-line patch and goes back to being an ordinary reported discovery. Length is the symptom,
not the test: a two-line change that encodes a decision is a discovery; a five-line change that only
transcribes an already-settled one is still a patch.
