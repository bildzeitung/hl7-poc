---
name: release
description: Propose the next SemVer version from commit history since the latest vX.Y.Z tag, compile the release notes from the resolved epics/tickets in that window (itemized + categorized, delivered as the annotated tag body that CI publishes), get both confirmed (or take an explicit version override), and drive scripts/release.sh to cut the release. Thin wrapper — no build logic of its own; scripts/release.sh owns the actual gate + tag + push. Parses conventional-commit prefixes (feat -> minor, fix -> patch, `!`/BREAKING CHANGE -> major, but pre-1.0 a breaking change bumps MINOR) and defaults to a PATCH proposal when no commit carries a recognized prefix. Examples — "/release", "/release patch", "/release minor", "/release major", "/release 0.2.0", "cut a release", "what's the next version".
---

# release

I am a **thin wrapper** over `scripts/release.sh` — I compute *what* the next version should be and
get a human to confirm it; the script owns the actual gate, tag, and push. I have **no build logic of
my own**: I never tag, push, or run the test gate directly, and the script re-checks everything
itself regardless of what I found. See [`docs/release.md`](../../../docs/release.md) for the full
release design; where this skill and that doc disagree, the doc wins.

I run on the **primary checkout, on the default branch** — same as the script requires (it refuses to
run anywhere else). I am not a producer task: no worktree, no ticket, no hand-off.

## How to use me

- **`/release`** — derive the proposal from commit history since the latest tag (the common case).
- **`/release patch|minor|major`** — skip history parsing, bump the latest tag by that part.
- **`/release X.Y.Z`** — propose exactly that version (still confirmed, still gated by the script's
  own monotonicity check).

## What I do

### 1. Find the latest tag

Delegate to `scripts/release-latest-tag.sh` — the SemVer-**greatest** `vX.Y.Z` tag, not the most
recently created one. Tag selection and SemVer comparison live in exactly one tested implementation,
called by both this skill and `scripts/release.sh`; ungated inline shell in a skill file rots
silently.

```bash
LATEST_TAG="$(scripts/release-latest-tag.sh)" || {
  echo "GATE COULD NOT RUN: scripts/release-latest-tag.sh failed" >&2
  # Exits 2 for a machine fault (git failure), never for a statement about which tag is
  # latest. Its stderr already surfaced the detail. Stop — do NOT guess a baseline tag.
  exit 1
}
```

**Empty `LATEST_TAG`** (no matching tag yet) → this is the first release. Propose the project's
pinned first version directly (no commit parsing) and skip to confirmation.

### 2. Derive the proposal

**Explicit override given** — skip history parsing and go straight to confirmation with that target.
(An explicit bump word only makes sense once a baseline tag exists; on a first release it still
proposes the pinned first version and says so.)

**Otherwise derive the bump via `scripts/release-bump.sh`.** This was once an inline snippet in this
file, and it silently under-detected commits due to a leading-newline bug in how it split `git log`'s
NUL-delimited record stream — only the newest commit in the range was read correctly, which shipped a
wrong version number. It is now an extracted, tested script:

```bash
# Re-derive LATEST_TAG — this is a fresh Bash invocation; nothing carries over from Section 1.
# Cheap and deterministic (a single git-tag lookup), so re-running beats persisting it.
LATEST_TAG="$(scripts/release-latest-tag.sh)" || { echo "GATE COULD NOT RUN" >&2; exit 1; }
BUMP="$(scripts/release-bump.sh "${LATEST_TAG}..HEAD")" || {
  echo "GATE COULD NOT RUN: scripts/release-bump.sh failed on ${LATEST_TAG}..HEAD" >&2
  # Exits 2 for a machine fault and never for a statement about the commits, so ANY
  # non-zero means "no verdict was produced". Stop — do NOT fall back to a guessed bump.
  exit 1
}
```

Precedence when several kinds are present: **breaking > feat > fix**. Then bump the latest tag:

- `breaking` — pre-1.0 (`MAJOR` is `0`) bumps **MINOR** and resets `PATCH`; past `1.0.0` it bumps
  **MAJOR** and resets the rest.
- `feat` — bumps **MINOR**, resets `PATCH`.
- `fix` — bumps **PATCH**.
- `none` — no commit carried a recognized prefix. Default to a **PATCH** bump rather than guessing
  higher, and say plainly that nothing matched so the human can confirm or override.

### 2a. Compile the release notes from the ticket record

The notes are **compiled, not composed**: I don't write freeform prose — I gather the resolved epics
and tickets in the release window and turn them into an itemized, categorized list. The confirmed
list becomes the annotated tag's **body**, which CI publishes as the release notes.

1. **Collect the ticket IDs that landed in the window** — first-parent history since the latest tag,
   or the whole history on a first release:

   ```bash
   LATEST_TAG="$(scripts/release-latest-tag.sh)" || { echo "GATE COULD NOT RUN" >&2; exit 1; }
   if [ -n "$LATEST_TAG" ]; then RANGE="${LATEST_TAG}..HEAD"; else RANGE="HEAD"; fi
   git log --first-parent --format='%s' "$RANGE"
   ```

   A landed unit is identified by its subject: `Merge land/<id>: …` or a trailing `(<id>)` marker on
   a direct commit. Dedupe — the same ticket can appear both ways.

2. **Resolve each ID** — `bd show <id>` for title, type, parent epic, status. Only **closed** tickets
   get a notes line; an ID that landed but is still open gets *flagged to the human* at confirmation
   instead — that's a bookkeeping smell, not a delivered feature. If the tracker doesn't know an ID at
   all, fall back to the merge subject rather than silently dropping landed work.

3. **Group and categorize:**
   - **Child tickets group under their parent epic**, epic title as the heading. Check
     `bd list --status=closed --type=epic --limit 0` for epics that closed in the window. An epic gets
     a one-line summary, then its landed children as sub-items — don't re-list children as top-level
     bullets.
     - **`--limit 0` is load-bearing here specifically:** this query returns every closed epic *ever*,
       and `bd list` sorts **priority-major, not by date** — so a cap keeps old high-priority epics
       and drops recent lower-priority ones, precisely the wrong half for a release window. Omitting
       `--json` is no protection: the human-readable form reports the *truncated* count as
       `Total: N issues`.
   - **Standalone tickets** categorize by type: **Features**, **Fixes**, **Internal / workflow** — in
     that order.
   - One line per item: what it delivers, phrased for a reader of the release page (not a commit
     subject), with the ticket ID in parentheses.

4. **Write the notes to a fixed, re-derivable path** — body only, no version heading (the script owns
   the tag subject). A **fixed** path, not `$(mktemp)`, because a random path can't be re-derived by
   a later block.

   **Wipe the directory first — fresh per release.** A fixed path outlives the run that wrote it, so
   the previous release's notes are still sitting there. If this step is skipped or its write fails,
   Section 4 finds a *non-empty* file and hands the last release's notes to the script as this tag's
   body — and the script's own `[ ! -s ]` guard cannot tell stale content from fresh:

   ```bash
   NOTES_FILE="$(git rev-parse --git-dir)/release-state/notes.md"
   rm -rf "$(dirname "$NOTES_FILE")" && mkdir -p "$(dirname "$NOTES_FILE")"
   # …write the itemized list into "$NOTES_FILE"…
   ```

An explicit version override skips the *version* derivation, never this step — every release gets
compiled notes.

### 3. Always confirm before touching anything

```
Latest tag: v<LATEST>  (or: no tag yet — this is the first release)
Proposing:  v<PROPOSED>   (<breaking|feat|fix|none, and the reasoning>)

Release notes (<N> resolved tickets, <M> epics since v<LATEST>):
  <the compiled notes body, verbatim>

<flags, if any: landed-but-still-open tickets, IDs unknown to the tracker>

Confirm v<PROPOSED>, or override: /release patch|minor|major|X.Y.Z
```

Confirming the version also confirms the notes — if the human wants a line reworded or dropped, I
apply that and re-show before proceeding. **I do not proceed past this point without an explicit
go-ahead: a bare `/release` never cuts a tag unattended.** If the human overrides, I recompute and
re-confirm rather than silently substituting.

### 3a. The export-only dirty tree — discard, don't block

`scripts/release.sh` requires a clean tree, but the one modification that recurs constantly is a lone
`M .beads/issues.jsonl` — the **passive tracker export**. It carries no release-relevant content and
must never gate a release. When that is the *only* dirty path, discard it and proceed:

```bash
git restore --staged .beads/issues.jsonl 2>/dev/null
git checkout -- .beads/issues.jsonl
git status --porcelain                                  # confirm now clean
```

If anything *else* is dirty, stop and surface it — that's a real tree the operator must decide on.

### 4. On confirm, invoke the script — nothing else

```bash
# Fresh Bash invocation; nothing from Section 2a's shell survives. $PROPOSED is the literal
# version confirmed in Section 3, filled in here like a placeholder — no bash here computes it.
NOTES_FILE="$(git rev-parse --git-dir)/release-state/notes.md"
scripts/release.sh "$PROPOSED" "$NOTES_FILE"   # bare X.Y.Z, no leading 'v' — the script adds it
```

The script embeds the notes file as the annotated tag's body and refuses a missing or empty file, so
a confirmed release always carries its compiled notes. That refusal covers *absent*, not *stale* — it
is §2a step 4's `rm -rf` that makes a skipped write show up as a missing file rather than as the
previous release's notes.

I do **not** run the test suite myself first, and I do **not** re-implement the clean-tree /
on-default-branch / up-to-date / tag-monotonicity checks — the script gates all of that before it
tags and pushes. My job ends at handing it the confirmed version string. If it exits non-zero, I
surface its exact error and stop — I do not retry with a different version on its behalf.

## What I don't do

- Never tag, push, or run the test suite directly — that's entirely the script.
- Never write freeform release notes — they are compiled from the resolved ticket record, and
  anything that can't be traced to a ticket is flagged, not narrated.
- Never proceed past a bare confirmation — no auto-release, ever, even when the derivation is
  obvious.
- Never touch a producer worktree, a ticket, or the merge queue — this is an operator utility over
  the release script, not a build task.
