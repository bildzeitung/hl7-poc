#!/usr/bin/env bash
#
# Single-lander lock for /land, extracted out of an inline
# Section 0 snippet in .claude/skills/land/SKILL.md that relied on
# `trap 'rm -f "$LOCK"' EXIT` to release the lock at the end of a /land pass.
#
# THE BUG THIS REPLACES: an agent running this skill executes every fenced
# `bash` block as its own, separate Bash tool invocation -- nothing carries
# over between them (the governing rule at the top of SKILL.md: "no fenced
# block may depend on shell state from another"). A `trap ... EXIT`
# set inside Section 0's block fires the instant THAT block's shell exits --
# which is before Section 1 even runs, not at the end of the pass. The lock
# was therefore held for the duration of one Bash call, not the pass it was
# supposed to serialize.
#
# It failed doubly open: the old reclaim path checked `kill -0 $OWNER_PID`
# against the PID recorded by whichever block last wrote the lock file. In
# this per-block-invocation architecture that check is not merely broken, it
# is STRUCTURALLY MEANINGLESS -- a Bash tool invocation's shell process has,
# by definition, already exited by the time a LATER invocation runs (control
# only returns to the caller once the shell exits), so `$OWNER_PID` recorded
# by any prior block is *always* dead by the time a later block reads it,
# whether or not a /land pass is still genuinely in progress (just running a
# later block/section). PID liveness cannot distinguish "the pass is still
# running, just between Bash calls" from "the pass crashed" here.
#
# THE FIX: no trap, no PID-liveness check. Liveness is instead a wall-clock
# STALENESS TOKEN -- the lock records when it was acquired, and a later
# `acquire` reclaims it only once that timestamp is older than
# LAND_LOCK_STALE_SECONDS (default 1800s / 30min -- see CAVEAT 1 for why the
# `heartbeat` subcommand did NOT buy a reduction). This staleness reclaim is
# the SOLE mechanism that is guaranteed to release an abandoned lock: it needs
# no cooperation from any
# particular exit site, so it cannot be silently broken by a future editor
# adding a new "stop the pass" exit to SKILL.md and forgetting to release.
# SKILL.md also calls `release` explicitly at two sites (Section 1's
# empty-queue exit, the end of Section 4) purely to keep the common
# `/loop 5m /land` cadence tight; MANY other exits reach neither and wait out
# the TTL instead -- see the two caveats below, and
# docs/agents-workflow.md's single-lander-lock bullet, which is the design
# home for this mechanism and for LAND_LOCK_STALE_SECONDS.
#
# CAVEAT 1 -- the TTL measures IDLE time rather than acquisition age ACROSS
# THE TWO LOOPS the `heartbeat` subcommand brackets, and
# acquisition age everywhere else. Read the gap list below before relying on
# the idle-time reading. Previously nothing re-stamped the
# token mid-pass, so the window had to exceed the TOTAL wall-clock duration of
# the longest legitimate pass (N land-review Opus dispatches, a combined
# re-gate, per-branch isolation replay on red, `validate-mermaid.sh`'s docker
# run, `lock_currency`'s network resolve) -- summed across the WHOLE pass, not
# merely the longest gap between two Bash calls. A pass that genuinely ran
# longer than the window had its own lock reclaimed by the next tick,
# mid-`main`-merge -- the dangerous direction -- which is why the window is
# large (1800s) and has never been reduced.
#
# `heartbeat` re-stamps the SAME record `acquire` wrote, with no atomicity
# contest (see its own comment below) -- so as long as SOMETHING calls it
# periodically during a pass, the token's age never reflects more than the
# GAP since the last call, not the pass's total duration. FOUR call sites make
# that periodic, by construction rather than by a future editor remembering a
# new one per section (the exact rot this design has avoided from the start):
#   - `.claude/skills/land/SKILL.md`, right before Section 1a --
#     fires once per pass, immediately after Section 1's two networked calls
#     (`bd dolt pull`, `git fetch origin`) and immediately before Section 1a's
#     O(n^2) stacked-branch-graph computation, so that O(n^2) work sits
#     between this call and the next (Section 2a's, below) rather than
#     unheartbeated all the way from `acquire`.
#   - `.claude/skills/land/SKILL.md` Section 2a (the top of the per-ticket
#     "vet each branch" loop) -- fires once per ticket, immediately before
#     that ticket's `land-review` Opus dispatch (2c), bounding that gap to
#     roughly one dispatch's duration, not the sum of N.
#   - `scripts/land-merge-one.sh` -- fires on every invocation,
#     which covers both Section 3's first merge loop (once per accepted
#     branch) AND its isolation-replay copy (once per branch being re-tested
#     after a red combined re-gate) with a single call site inside the
#     script, needing no second SKILL.md edit for the replay loop.
#   - `.claude/skills/land/SKILL.md`, at the top of Section 4's main block
# -- fires once per pass, right after `git push origin main`
#     and before the per-ticket `bd close` loop, so every per-ticket `bd
#     close` / `epic-completion-check.sh` / the networked
#     `scripts/bd-dolt-push.sh` / every per-ticket branch delete / the
#     worktree-GC sweep sits between this call and the pass-end `release`,
#     rather than unheartbeated all the way from the LAST `land-merge-one.sh`
#     call in Section 3.
# All four are pinned by tests the same way `acquire`/`release` are (see
# tests/test_land_lock.py and tests/test_land_merge_one.py) -- a heartbeat
# call site that quietly stops being called is exactly as dangerous as the
# original inert lock, just slower to notice.
#
# ONE stretch of a pass remains uncovered post-an earlier fix (down from three) --
# the four call sites above bracket the two per-ticket LOOPS plus the two
# named boundary points, not literally every line of the pass. Do not read
# "heartbeat exists" as "the whole pass is covered":
#   1. Section 3's single COMBINED re-gate (`nox -t fix && nox -s tests &&
#      nox -s build_members && nox -s lock_currency`, plus `validate-mermaid.sh`
#      on a docs change), which runs once, between the merge loop and the
#      isolation-replay loop. MEASURED on the 2026-07-28 dev machine at ~60s
#      total (tests ~50s, fix ~0.4s, lock_currency ~1s, mermaid ~10s); the
#      figure excludes build_members -- comfortably small, but it is
#      wall-clock on one machine, not a bound. Unlike the two gaps an earlier fix
#      closed, this one does NOT grow with the size of the `ready-for-land`
#      queue -- it runs exactly once per pass regardless of how many tickets
#      are being landed -- which is why it was left uncovered rather than
#      folded into this ticket; re-deriving whether it is worth a call site of
#      its own is a separate decision, not implied by this one.
#
# an earlier fix (2026-08-07) closed the other two of the original three -- both of
# which DID grow with queue size, unlike the one above -- by adding the first
# and fourth call sites listed above. Neither new call site individually
# BOUNDS the per-pass-or-per-ticket work it sits in front of (each is a single
# interval, not itself a per-iteration heartbeat re-fired inside Section 1a's
# loop or inside Section 4's per-ticket loops) -- what it buys is isolating
# that work as the SOLE remaining contributor to its stretch, rather than
# that work summed with adjacent networked/fixed-cost calls. This does not
# change LAND_LOCK_STALE_SECONDS and must not be read as though it does --
# see "WHY THE DEFAULT STAYS AT 1800s" below, unchanged by this ticket.
#
# WHY THE DEFAULT STAYS AT 1800s. The heartbeat shrinks the exposure a lot: it
# is the whole fix for "a long pass has its OWN lock reclaimed mid-merge", and
# at 1800s that failure is now essentially unreachable. It does NOT license
# lowering the window, because the two failure directions remain as asymmetric
# as they were before it existed: too LOW reclaims a live lock and puts two
# landers on `main` at once (unbounded damage), while too HIGH only delays
# landing by a few `/loop 5m` ticks (bounded, self-healing, and explicitly not
# latency-critical). Lowering trades the safe side for the dangerous one, and
# nothing here measures the binding gap. That gap is NOT the re-gate above: it
# is one `land-review` Opus dispatch (the 2a->2a interval) plus, on a bounce,
# the lander's own `bd` bookkeeping. Agent dispatches in this repo are
# routinely minutes long -- an earlier fix's own `coding` builder took 14m10s
# (bd `started_at` 03:00:24Z -> `updated_at` 03:14:34Z, 2026-07-28) -- i.e.
# the same order of magnitude as a 600s window, not comfortably under it. So
# the reduction to 600s that this subcommand was expected to unlock is held
# back until either the gaps above are covered or a real distribution of
# `land-review` dispatch times exists to size it against;.
# Overriding via the env var stays available for anyone who has measured
# their own machine.
#
# CAVEAT 2 -- the stale-lock RECLAIM (and the fresh-vs-reclaim race beside
# it) is now closed OUTRIGHT via `flock(1)`, not merely narrowed.
#
# READ THE EXPOSURE FIRST, or this will read as far more urgent than it is.
# Under the documented operating convention -- ONE `/loop 5m /land` on one
# machine -- acquires are issued SERIALLY, and a pass that is still running
# holds a FRESH lock. So the reclaim branch is crash-recovery only: reaching
# it concurrently at all needs two independent /land invocations arriving
# within milliseconds of each other, 30+ minutes after a crash. This is
# hardening against the convention being violated.
#
# HISTORY, for whoever finds this via a bisect or a stale mental model. Two
# prior fixes serialized reclaim behind an `mkdir`-based
# gate (`$LOCK.reclaiming`) with a self-heal, plus a gate-ownership re-check
# immediately before the destructive `rm -f "$LOCK"`. That closed the original
# two-step race and the alive-but-stalled-holder displacement it left open,
# but a live evaluation MEASURED two further, STALL-FREE routes
# into the same two-winner outcome, both check-then-act races on the gate
# directory itself that no amount of additional shell-level verification can
# close (POSIX shell has no "unlink only if this is still the same object"):
#   1. The self-heal's `rm -rf` can remove a gate it never judged abandoned --
#      by the time it runs, that path may be a FRESH gate another racer just
#      won. Observed as two RECLAIM winners in one round.
#   2. The FRESH (non-reclaim) acquire path took NO gate at all, so it was
#      never serialized against a gate winner's `rm -f "$LOCK"` + `write_lock`
#      -- a top-level `write_lock` landing in that two-step gap could succeed
#      alongside a reclaim. Observed as one reclaim + one FRESH winner.
# MEASURED live against the mkdir-gate script: 2 of 150 rounds at 32-way
# contention under 28-way CPU saturation, starting from an already-abandoned
# gate, with NO stall injected -- i.e. reachable under ordinary crash-recovery
# contention, not only a machine already stalled. That is the evidence an earlier fix
# weighed against a gate-ownership check narrowing the window further (the
# alternative the ticket also considered, and which cannot close either route
# above -- both are races on the gate OBJECT, not on which token currently
# claims to own it).
#
# THE FIX: `flock(1)` wraps the ENTIRE acquire decision -- fresh-lock attempt,
# staleness check, and reclaim -- in one exclusive lock held on a dedicated
# file (`$LOCK.flock`, never the lock-record file itself) for the lifetime of
# the flock'd file descriptor. This is a genuine capability upgrade over the
# mkdir gate, not a narrower version of the same idea: a kernel `flock` is
# released the instant the holding PROCESS exits, by ANY means (normal exit,
# crash, `kill -9`) -- there is no "abandoned but not yet aged out" state for
# a competitor to race against, because there is no separate object (a gate
# directory) whose lifecycle can be judged wrong. A second acquire attempted
# while the first is inside this section BLOCKS (bounded by
# `LAND_LOCK_FLOCK_TIMEOUT_SECONDS`, small -- the section it guards is a
# handful of forks, not a /land pass) rather than being handed a chance to
# race; on timeout it skips the tick exactly like "lock still held", which is
# the same safe direction every other undecided read in this script takes.
# A stalled-but-alive holder cannot be displaced (it still holds the flock);
# a crashed holder releases it immediately (no permanent-wedge risk, and no
# second staleness window of its own to size). This also makes the SECOND
# reachable route above -- the FRESH path racing a reclaim -- structurally
# impossible, not merely improbable, since both paths now execute inside the
# same mutex rather than only the reclaim half of it.
#
# PORTABILITY TRADEOFF, spelled out rather than hidden: `flock(1)` ships in
# util-linux, present on essentially every Linux distribution but ABSENT on
# macOS and stock git-bash, both of which CLAUDE.md's "New machine setup"
# contemplates. `acquire` checks for it explicitly (`command -v flock`) and,
# if missing, reports a MACHINE FAULT and skips the tick -- landing stays
# blocked rather than silently reverting to the two-winner-capable behaviour
# this ticket closed. `/land` is documented to run on ONE machine, so this is
# a one-time environment gap per machine, not a per-tick cost; a human
# choosing to run `/land` on a flock-less machine is expected to install
# util-linux (Homebrew ships a `flock` formula on macOS) rather than the
# script silently downgrading its own guarantee. `heartbeat` and `release`
# need no such check -- neither one contends against a concurrent acquire the
# way the reclaim decision does.
#
# `acquire`'s own O_EXCL create (the FRESH-lock path) was always atomic on its
# own and remains so, unchanged (`write_lock`'s `noclobber`) -- it is now ALSO
# inside the flock'd section, which is what closes route 2 above.
#
# NOTE: this remains a DIFFERENT object and a DIFFERENT concern from
# an earlier fix. q9pm is about `heartbeat` refusing to re-stamp the MAIN LOCK's
# own record once this pass no longer owns it (the self-concealing gap
# described further below) -- unaffected by how `acquire`'s OWN internal race
# is closed. an earlier fix's conclusion does retire the "shares machinery with
# an earlier fix" framing the gate-ownership-check alternative would have created:
# there is no reclaim gate any more for a shared token-comparison helper to
# live next to, so q9pm's owner-token check is scoped entirely to `$LOCK`
# itself and `heartbeat`, with nothing here for it to coordinate with.
#
# OWNERSHIP CHECK.
# `heartbeat` and `release` require the calling pass's own remembered token as
# their final argument (documented in `heartbeat`'s Usage entry below) and
# refuse to touch the lock record on a mismatch, rather than silently
# concealing an overlap with a displaced holder (`heartbeat` exits 1 without
# re-stamping; `release` exits 0 without removing $LOCK). `heartbeat`
# PRESERVES the record's existing owner token rather than
# regenerating it, so the field stays meaningful to compare call to call --
# see the MERGE RESOLUTION note below for why that specific behaviour was the
# merge outcome, not a redesign.
#
# an earlier fix (2026-08-07): the argument is REQUIRED, not optional. an earlier fix
# originally shipped it as an OPTIONAL final argument, purely for backward
# compatibility with a caller that had not yet been updated to thread its own
# token through. That compatibility need never actually existed -- there is
# no external caller of this script; every caller lives in this repo, and
# every real call site was updated to supply its token in the same change
# that added the check. Leaving the argument optional meant the safety
# property was opt-in per call site rather than an invariant of the script
# itself, and a future call site that simply forgot the argument would
# silently degrade to the pre-an earlier fix blind behaviour with nothing to catch
# it. Requiring it turns "forgot to thread the token" into a loud, immediate
# usage error (exit 2) instead.
#
# TWO call sites are sanctioned to pass the explicit `--land-lock-blind`
# sentinel instead of a real token -- explicitly, so the opt-out is visible at
# the call site rather than looking like an oversight, and greppable as
# `land-lock-blind-ok:`:
#   - `.claude/skills/land/SKILL.md` Section 0's parse-failure bail path,
#     which must release the lock it just acquired before it has had any
#     chance to persist its own token to disk;
#   - `scripts/land-merge-one.sh`, whose own third positional argument stays
#     OPTIONAL (a direct invocation must still run) and which substitutes the
#     sentinel when it is empty, after warning.
# Each carries its own comment; both are pinned by tests/test_land_lock.py.
#
# an earlier fix ALSO considered, and explicitly REJECTED, making the check a
# self-reading invariant of this script (`acquire` writes a token file that
# `heartbeat`/`release` read back themselves, collapsing every call-site
# argument to zero): there is no self-reading form that keeps `release`'s
# "a caller that never held the lock can call it harmlessly" contract, so
# per-call-site threading is the correct end state, not a stopgap. Full
# reasoning lives in docs/decisions.md and
# docs/agents-workflow.md -- deliberately not re-expanded here.
#
# For WHY this check exists (the self-concealing-overlap hazard: a displaced
# pass that keeps blindly re-stamping a reclaimed lock makes a genuine
# overlap look like one continuous holder) and HOW the token threads across
# `.claude/skills/land/SKILL.md`'s separate Bash invocations to reach
# `heartbeat`/`release` here at all, see docs/agents-workflow.md's canonical
# paragraph (search that file for "Threading mechanism.").
#
# MERGE RESOLUTION. This branch was built against a
# main with no `heartbeat`; an earlier fix landed on main afterward, and this
# file conflicted with it on merge. The conflict was SEMANTIC, not just
# textual -- taking either side wholesale would have been wrong in a way no
# test caught at the time:
#   * main's `heartbeat` called `lock_record` with NO argument. This
#     branch's `lock_record` reads the owner token from `$1`. Under
#     `set -u` that call fails outright, so taking this branch's side
#     wholesale would have broken heartbeat -- loudly, against main's
#     heartbeat tests. That loudness was deliberate: the mandatory
#     positional is a merge tripwire, and it was NOT "fixed" with a default.
#   * Taking main's 4-field `lock_record` wholesale would have silently
#     dropped the owner token and reverted the reclaim to the non-atomic
#     `rm`-then-create form.
#   * The resolution kept that branch's reclaim gate AND main's
#     `heartbeat` (the gate is gone again as of an earlier fix -- only the
#     `heartbeat` half of this resolution still constrains the code below),
#     and made `heartbeat` PRESERVE the existing token (via
#     `token_of`, see below) rather than regenerate or blank it -- a
#     heartbeat that minted a fresh token every tick would have destroyed
#     the one thing an earlier fix needs to compare against, turning the field
#     into decoration while looking correct.
#   `tests/test_land_lock.py::test_heartbeat_preserves_the_existing_owner_token`
#   pins this.
#
# Usage: scripts/land-lock.sh acquire
#        scripts/land-lock.sh heartbeat <own-token>|--land-lock-blind
#        scripts/land-lock.sh release <own-token>|--land-lock-blind
#
# acquire: exit 0 -> lock acquired (fresh, or reclaimed from a stale prior
#                     holder). Caller proceeds with its /land pass. Prints
#                     "land-lock: acquired (token <token>)" (or "... via
#                     reclaim ...") to stdout -- the token is THIS pass's own,
#                     to be captured and re-supplied to `heartbeat`/`release`
#                     as their `<own-token>` argument.
#          exit 1 -> another /land is still (plausibly) running on this
#                     machine, or the lock file could not be created at all --
#                     including when the lock PATH itself could not even be
#                     determined, e.g. run from outside any git repository
#. Caller must
#                     skip this tick cleanly (exit 0 of its OWN, per the
#                     "single lander" convention) -- do not queue, do not run
#                     in parallel. Diagnostic on STDERR.
#
#                     PERSISTENT MACHINE FAULT ESCALATION: every
#                     MACHINE FAULT branch below (`flock` missing, the lock
#                     path itself undeterminable, the lock file/flock-mutex
#                     unwritable) bumps a persisted consecutive-fault counter
#                     -- never a transient "another /land is still running"
#                     skip, which is proof the machine itself is fine and
#                     resets the counter same as a clean acquire. Once that
#                     counter reaches `LAND_LOCK_FAULT_ESCALATE_THRESHOLD`
#                     (default 3), the MACHINE FAULT stderr line is followed
#                     by a second, distinctly-prefixed line -- "land-lock:
#                     ESCALATE -- ..." -- for a caller to grep for and act on
#                     (SKILL.md Section 0 does; see there). No new exit code:
#                     the exit-code contract stays exactly the three values
#                     documented here (an earlier fix already declined to teach a
#                     third code to every collapsing caller for no
#                     behavioural gain; this file follows the same call).
#                     land-lock.sh itself makes NO bd call and creates no
#                     ticket -- it stays dependency-free the same way the rest
#                     of this script already is; escalating to a human is
#                     entirely the caller's business.
#          exit 2 -> usage error (a caller bug, never a lock verdict). NOT
#                     where a rev-parse/machine failure lands -- see below.
# heartbeat <own-token>: re-stamps the lock this pass already holds, so the
#            staleness check measures idle time from the LAST heartbeat
#            rather than the original `acquire` (CAVEAT 1). Call it
#            periodically from inside a still-running pass -- never as a
#            substitute for `acquire`.
#
#            `<own-token>` (also required by `release` below) is REQUIRED
# -- see the OWNERSHIP
#            CHECK section above for the full reasoning. There is no external
#            caller of this script to stay backward compatible with, so an
#            absent or empty argument is now a CALLER BUG, not a supported
#            degraded mode: it is rejected with exit 2 before any lock file
#            is even touched (see the arg-parsing block below). This entry is
#            where that semantics is stated for this file; `release`'s own
#            `<own-token>`, the OWNERSHIP CHECK section, and the `OWN_TOKEN=`
#            assignment below point back here rather than repeating it.
#
#            The ONLY sanctioned way to skip the ownership comparison on
#            purpose is the literal sentinel `--land-lock-blind` in place of
#            a real token, from one of the two call sites named in the
#            OWNERSHIP CHECK section above. Using the sentinel from anywhere
#            else defeats the point of this check and is not sanctioned.
#            exit 0 -> re-stamped (or created fresh, if the file was somehow
#                       already gone -- see the subcommand's own comment).
#            exit 1 -> could not write the lock file -- including when the
#                       lock path itself could not be determined,
#                       OR `<own-token>` does not match the
#                       record's current owner token: this pass no longer
#                       holds the lock (another /land reclaimed it) and
#                       heartbeat REFUSES to overwrite the new holder's
#                       record. NOT fatal to the caller's own step by itself
#                       either way (this is bookkeeping, not the work) -- log
#                       and continue; on the mismatch case specifically, the
#                       caller should also stop treating itself as the lock
#                       holder. Diagnostic on STDERR.
#          exit 2 -> `<own-token>` was absent, empty, and not the
#                       `--land-lock-blind` sentinel -- a caller bug, never a
#                       lock verdict. Diagnostic on STDERR.
# release <own-token>: always exit 0 when the argument was valid -- `rm -f`
#           is idempotent, and a caller that never held the lock (e.g. it
#           just skipped the tick above) must be able to call this
#           harmlessly too. Still exit 0, with a diagnostic on STDERR rather
#           than silence, when even the lock PATH could not be determined
#: with no repository here, there is by definition
#           nothing to release either way.
#
#           `<own-token>` is REQUIRED, same as `heartbeat`'s -- see its own
#           Usage entry above, including the `--land-lock-blind` sentinel and
#           its one sanctioned call site. When a real token is supplied and it
#           does NOT match the record's current owner token,
#           `release` refuses to remove $LOCK -- deleting it would destroy
#           another pass's live record, not this pass's own -- and says so on
#           STDERR, still exiting 0 (there is nothing left for THIS pass to
#           clean up either way).
#           exit 2 -> `<own-token>` was absent, empty, and not the
#                       `--land-lock-blind` sentinel -- a caller bug, never a
#                       lock verdict. Diagnostic on STDERR. (The prior
#                       "release always exits 0" promise is about a VALID
#                       call whose lock is simply not held -- not about a
#                       malformed call.)
#
# Lock file lives under .git/ (per-machine, never committed) -- the shared,
# repo-global .git, not a worktree-private one.

set -euo pipefail

# The literal sentinel that opts a heartbeat/release call OUT of the
# ownership check on purpose, distinct from an omitted/empty argument. Chosen to
# be unambiguous against a real token: `new_token` below only ever produces
# 16 lowercase hex characters, so a leading `--` can never collide.
BLIND_SENTINEL="--land-lock-blind"

# a persisted, consecutive-MACHINE-FAULT counter, so a permanent
# per-machine fault (flock missing, an unwritable lock dir, ...) escalates
# past per-tick output nobody reads under `/loop 5m /land`, instead of
# reading forever as just another overrunning tick.
#
# Deliberately NOT repo-scoped (never under $GIT_COMMON_DIR, unlike $LOCK
# itself): the most common MACHINE FAULT this counts is an unwritable/missing
# git dir -- keying the counter off that SAME directory would make it fail to
# persist for precisely the fault it exists to track, silently defeating
# escalation rather than driving it. A FIXED, git-independent location sidesteps
# that, and also means the one fault branch that fires before $GIT_COMMON_DIR
# is even derived (the `git rev-parse --git-common-dir` failure itself) has
# somewhere to count to as well, with no special case.
#
# The NAME has to be predictable (the next invocation must find the same file),
# so `mktemp` -- how every other script here reaches $TMPDIR -- is not an
# option. Two accepted consequences: a fixed name in a world-writable dir is
# squattable on a multi-user box (out of scope for this repo's single-user
# threat model, and nothing else here defends against it either), and ONE
# counter is shared by every clone on the machine -- fine, since SKILL.md
# already documents "run the /land loop on one machine only", and a healthy
# second clone's reset only ever SUPPRESSES an escalation, never invents one.
LAND_LOCK_FAULT_COUNT_FILE="${TMPDIR:-/tmp}/harness-land-lock-fault-count"
LAND_LOCK_FAULT_ESCALATE_THRESHOLD="${LAND_LOCK_FAULT_ESCALATE_THRESHOLD:-3}"

# Any successful acquire, or a transient "lock genuinely held" skip, is proof
# the machine itself is fine -- both call this to zero the count, so only
# CONSECUTIVE machine faults ever accumulate.
reset_fault_count() {
  rm -f "$LAND_LOCK_FAULT_COUNT_FILE" 2>/dev/null || true
}

# The one call every MACHINE FAULT branch of `acquire` makes: bump the
# consecutive-fault count, then announce an ESCALATE line on stderr once it has
# reached the threshold -- every tick from then on, not just the first
# crossing, so a caller that only reads the most recent tick's output still
# sees it. Kept as ONE function so "bumped but never announced" is not
# reachable. An absent or malformed count file reads as 0, the same
# "unreadable means unknown" stance as `epoch_of`/`token_of` below; the write
# is best-effort, because a $TMPDIR that is itself unwritable must cost this
# tick's bump, never abort `acquire`.
note_machine_fault() {
  local n
  n=""
  read -r n < "$LAND_LOCK_FAULT_COUNT_FILE" 2>/dev/null || true
  case "$n" in ''|*[!0-9]*) n=0 ;; esac
  n=$(( n + 1 ))
  printf '%s\n' "$n" > "$LAND_LOCK_FAULT_COUNT_FILE" 2>/dev/null || true
  if [ "$n" -ge "$LAND_LOCK_FAULT_ESCALATE_THRESHOLD" ]; then
    echo "land-lock: ESCALATE -- $n consecutive MACHINE FAULT acquires" \
      "(threshold $LAND_LOCK_FAULT_ESCALATE_THRESHOLD reached). This is not" \
      "self-healing; a human should investigate, or it will keep repeating" \
      "every tick until fixed." >&2
  fi
}

# acquire takes no further argument; heartbeat/release now each REQUIRE
# exactly one further argument -- their own token, or the explicit
# $BLIND_SENTINEL opt-out. Stated as the three legal
# <argc>:<subcommand> shapes rather than as a chain of negated conditions, so
# adding a fourth shape is one more `case` arm and nothing else.
case "$#:${1:-}" in
  1:acquire | 2:heartbeat | 2:release) ;;
  *)
    echo "usage: $0 acquire" >&2
    echo "       $0 heartbeat <own-token>|$BLIND_SENTINEL" >&2
    echo "       $0 release <own-token>|$BLIND_SENTINEL" >&2
    exit 2
    ;;
esac
cmd="$1"
# This pass's own remembered token -- empty for acquire (never reaches here,
# $# is 1) and, for heartbeat/release, either a real token or the literal
# $BLIND_SENTINEL (both required, see the arg-count check above). Resolved to
# the ownership-check-skipping empty string just below when the sentinel was
# supplied; rejected outright when it is truly absent or empty.
OWN_TOKEN="${2:-}"
if [ "$cmd" = "heartbeat" ] || [ "$cmd" = "release" ]; then
  if [ "$OWN_TOKEN" = "$BLIND_SENTINEL" ]; then
    OWN_TOKEN=""
  elif [ -z "$OWN_TOKEN" ]; then
    echo "land-lock: $cmd requires a non-empty own-token argument, or the" \
      "explicit $BLIND_SENTINEL opt-out -- got empty/absent. This is a" \
      "caller bug: every caller of this script lives in this repo" \
      ", so there is no external caller left to stay silently" \
      "backward compatible with. See this script's OWNERSHIP CHECK header" \
      "section." >&2
    exit 2
  fi
fi

# `git rev-parse --git-dir` is NOT repo-global: from a LINKED worktree it
# returns that worktree's PRIVATE gitdir (.git/worktrees/<name>), and only from
# the main checkout does it return the shared .git. Two /land passes -- one in
# the main checkout, one (mis)dispatched into a worktree -- would then take two
# DIFFERENT lockfiles and neither would see the other, silently defeating the
# single-lander guarantee this whole script exists to provide. `--git-common-dir` returns the ONE shared .git from every worktree of
# the repo, including the main checkout.
#
# `--path-format=absolute` is load-bearing too, for a SECOND, non-worktree
# reason: bare `--git-common-dir` is CWD-RELATIVE inside the main checkout --
# `.git` from the root, but `../../.git` from a subdirectory. Those resolve to
# the same file (so mutual exclusion holds either way), but the path STRING
# differs by cwd, which is not the "byte-identical from every worktree" this
# ticket asked for, and it leaves the `$LOCK` path printed in the diagnostics
# below dependent on the reader's cwd -- directly against
# the operator-inspects-the-right-file reasoning at the bottom of this script.
# Forcing absolute makes the path identical from every cwd AND every worktree.
# Same flag pair, for the same reason, as scripts/assert-main-checkout.sh
#, which reads the shared .git for its own identity check -- and,
# as of an earlier fix, the same failure discipline too: the `rev-parse` below is
# wrapped rather than left to `set -e`, so a git failure (most likely: cwd is
# outside any git repository at all) cannot escape as git's bare, undocumented
# 128. Unlike assert-main-checkout.sh, that maps onto exit 1, NOT exit 2 --
# the header above reserves exit 2 for a caller/usage bug, so this is folded
# into the same MACHINE FAULT class as the "cannot create $LOCK" branch below.
# Mutual exclusion was never at risk (SKILL.md's caller collapses any non-zero
# to "skip the tick"); what this closes is observability -- the diagnostic now
# names the cause, and which subcommand hit it, instead of a bare `fatal:`.
#
# STILL LATENT rather than live -- but NOT because assert-main-checkout.sh
# covers it. That guard runs in land/SKILL.md **Section 1**, and this lock is
# acquired in **Section 0**, before it: nothing asserts where the pass is
# running until after the lockfile has already been written, so a misdispatched
# pass would write a worktree-private lock and only then be stopped. What
# actually keeps this latent is the operating convention that /land runs in the
# main checkout at all. That ordering gap is precisely why fixing the path here
# is worth more than a cosmetic tidy -- do not "simplify" this back on the
# theory that the Section 1 guard already handles it.
if ! GIT_COMMON_DIR="$(git rev-parse --path-format=absolute --git-common-dir)"; then
  # git's own diagnostic already went to stderr above (this command
  # substitution captures only stdout). `$cmd` is validated to exactly these
  # three values above, so no `*)` arm is reachable.
  case "$cmd" in
    acquire)
      echo "land-lock: MACHINE FAULT -- 'git rev-parse --git-common-dir'" \
        "failed (git's own error is above); cannot derive the lock path." \
        "This is not another lander; landing stays blocked until it is" \
        "fixed. Skipping this tick." >&2
      note_machine_fault
      exit 1
      ;;
    heartbeat)
      echo "land-lock: heartbeat could not derive the lock path -- 'git" \
        "rev-parse --git-common-dir' failed (git's own error is above)." \
        "NOT fatal to this step by itself -- but a human should check why" \
        "this ran outside a git repository if it repeats every tick." >&2
      exit 1
      ;;
    release)
      echo "land-lock: release -- 'git rev-parse --git-common-dir' failed" \
        "(git's own error is above); cwd is most likely not inside any git" \
        "repository. Nothing to release either way -- treating this as a" \
        "harmless no-op." >&2
      exit 0
      ;;
  esac
fi
LOCK="$GIT_COMMON_DIR/land.lock"
STALE_SECONDS="${LAND_LOCK_STALE_SECONDS:-1800}"

# The dedicated file `acquire`'s critical section is `flock`'d on (CAVEAT 2)
# -- never `$LOCK` itself, which is the lock-record data file, not a mutex.
# The wait bound is deliberately small and NOT the `LAND_LOCK_STALE_SECONDS`
# env var (that governs the MAIN lock's staleness and must stay large); this
# one only has to outlast the handful of forks inside the flock'd section
# plus ordinary scheduler jitter, not a whole /land pass.
FLOCK_FILE="$LOCK.flock"
FLOCK_TIMEOUT_SECONDS="${LAND_LOCK_FLOCK_TIMEOUT_SECONDS:-10}"

new_token() {
  # An opaque, effectively-unique identifier for THIS acquisition -- not a
  # secret, just distinct across concurrent acquirers -- so a future
  # ownership check can tell "the record's current owner" from
  # "was I the one who wrote it". /dev/urandom + od are both part of every
  # base image this repo runs nox on, so there is no fallback branch: one
  # that fired only where `od` is missing would hard-fail at the same
  # `set -euo pipefail` anyway, while minting a SECOND token shape that
  # an earlier fix's comparison would then have to tolerate for no gain.
  od -An -N8 -tx1 /dev/urandom | tr -d ' \n'
}

lock_record() {
  # The one definition of the record's shape. Field order is load-bearing:
  # the reclaim path below reads field 3 (epoch) and nothing else, so fields
  # 1-4 (pid, host, epoch, ISO stamp) keep their original positions -- only
  # field 5 (owner token) is new, appended rather than inserted, so nothing
  # that reads field 3 needs to change. Fields 1, 2 and 4 are for a human
  # reading the file by hand; field 5 (via `token_of`) is read back and
  # threaded straight through by `heartbeat` below, so the field survives
  # repeated heartbeat calls unchanged, and is also what `heartbeat`/
  # `release`'s own ownership check compares an `[own-token]`
  # argument against.
  #
  # The token is a MANDATORY positional, and that is deliberate -- see the
  # MERGE RESOLUTION note in the header. Defaulting it (`${1:-...}`) would
  # have let main's original argument-less `heartbeat` call keep working
  # through the merge while either blanking the token or minting a fresh one
  # every tick, silently destroying the ownership continuity the field
  # exists to provide, with main's five heartbeat tests still green.
  # Requiring it makes that mistake fail loudly instead. Do not "fix" this
  # by adding a default -- `heartbeat` below supplies its own via `token_of`.
  printf '%s %s %s %s %s\n' "$$" "$(hostname)" "$(date -u +%s)" "$(date -u +%FT%TZ)" "$1"
}

epoch_of() {
  # Field 3 of a lock record if it is a legible epoch, empty otherwise
  # ("age unknown"). Both readers below parse identically -- only their
  # VERDICTS differ, and each keeps its own at its own call site. Confining
  # the positional-param split to a function also keeps it from clobbering
  # the script's own "$@" twice at top level.
  # shellcheck disable=SC2086  # deliberate word-split of the record
  set -- $1
  case "${3:-}" in
    ''|*[!0-9]*) ;;
    *) printf '%s' "$3" ;;
  esac
}

token_of() {
  # Field 5 (owner token) of a lock record, empty if the record predates the
  # field or is otherwise malformed. Same
  # word-split-in-a-function shape as `epoch_of`, and for the same reason --
  # confines the split so it cannot clobber the script's own "$@" at top
  # level. Used by `heartbeat`, to PRESERVE whatever token is currently on
  # disk rather than regenerate or blank it (see MERGE RESOLUTION above), and
  # by BOTH `heartbeat` and `release` as the left-hand side of the an earlier fix
  # ownership comparison against the caller's `[own-token]`.
  # shellcheck disable=SC2086  # deliberate word-split of the record
  set -- $1
  printf '%s' "${5:-}"
}

skip_lock_still_held() {
  # The lock is present and not (yet) reclaimable. One wording, two callers.
  # TRANSIENT, never a MACHINE FAULT -- the flock, the lock dir, and the lock
  # file all work fine; something else genuinely holds the lock.
  reset_fault_count
  echo "land-lock: another /land appears to still be running on this machine" \
    "(lock: $1) -- skipping this tick." >&2
  exit 1
}

write_lock() {
  # Atomic create: `set -o noclobber` makes the `>` redirect fail if the file
  # already exists, so two concurrent FRESH attempts can't both think they
  # got it. Takes the new owner token as $1. This guarantee covers THIS
  # function only -- the reclaim path below never calls it without first
  # holding the exclusive `flock` (CAVEAT 2), which is what extends the same
  # single-winner property to the reclaim case.
  ( set -o noclobber
    lock_record "$1" > "$LOCK" ) 2>/dev/null
}

if [ "$cmd" = "release" ]; then
  # Ownership check: only when the caller supplied its own
  # token. An empty CUR_TOKEN (record predates the field, or the lock is
  # already gone) is "nothing to compare against" -- not a mismatch -- so
  # this still falls through to the unconditional `rm -f` below, same as
  # ever. A non-empty CUR_TOKEN that does not match $OWN_TOKEN means another
  # /land now owns this lock: removing it would destroy a LIVE record, not
  # this pass's own, so refuse -- diagnostic on stderr, still exit 0 (this
  # pass's own release contract; there is nothing further for it to do).
  if [ -n "$OWN_TOKEN" ]; then
    CUR_RECORD=""
    read -r CUR_RECORD < "$LOCK" 2>/dev/null || true
    CUR_TOKEN="$(token_of "$CUR_RECORD")"
    if [ -n "$CUR_TOKEN" ] && [ "$OWN_TOKEN" != "$CUR_TOKEN" ]; then
      echo "land-lock: release REFUSING to remove $LOCK -- its current owner" \
        "token ($CUR_TOKEN) does not match this pass's own ($OWN_TOKEN)." \
        "Another /land holds the lock now; leaving its record in place" \
        "rather than deleting a live holder's lock. Still exiting 0 (release's" \
        "own always-exit-0 contract) -- there is nothing further for this" \
        "pass to clean up." >&2
      exit 0
    fi
  fi
  rm -f "$LOCK"
  exit 0
fi

if [ "$cmd" = "heartbeat" ]; then
  # Best-effort re-stamp of the SAME record `acquire` wrote -- refreshes the
  # recorded time so a later tick's staleness check measures idle time since
  # the LAST heartbeat, not the age of the original `acquire`. Deliberately NOT `write_lock`'s atomic/noclobber create:
  # overwriting the existing record is exactly the point here, and the
  # documented one-lander-per-machine convention means no other WRITER should
  # be touching this file while a pass holds it. Concurrent READERS are a
  # different matter and are routine -- every `/loop 5m /land` tick's own
  # `acquire` reads this file, and `>` truncates before it writes, so a reader
  # CAN catch a half-written record. That is safe by construction, not by
  # luck: the reclaim path below treats an unparseable record as "age unknown"
  # and skips the tick rather than reclaiming. Keep that path conservative --
  # making it guess an age would turn this benign interleaving into a reclaim
  # of a live lock. If the file is somehow already gone (heartbeat called
  # without ever acquiring -- should not happen at either documented call
  # site), this creates it fresh rather than erroring: either way the caller's
  # intent ("the pass is still alive right now") is the same.
  #
  # PRESERVES the owner token rather than regenerating or
  # blanking it -- `lock_record`'s token positional is mandatory specifically
  # so a heartbeat that forgot this would fail loudly under `set -u`, not
  # silently mint a fresh token every tick (see the MERGE RESOLUTION note in
  # the header). Reads whatever token is CURRENTLY on disk via `token_of` and
  # threads it straight through; a record predating the field (four fields,
  # no token) or a lock somehow already gone yields an empty `token_of` read,
  # in which case this mints a fresh one -- matching `acquire`'s own shape,
  # since there is no prior token left to preserve either way.
  CUR_RECORD=""
  read -r CUR_RECORD < "$LOCK" 2>/dev/null || true
  CUR_TOKEN="$(token_of "$CUR_RECORD")"

  # Ownership check: only when the caller supplied its own
  # token AND the record already has one to compare against. An empty
  # CUR_TOKEN means "nothing to compare" (a legacy four-field record, or the
  # lock is simply gone) -- not a mismatch -- so that case falls through to
  # the mint-or-preserve logic below exactly as it always has. A non-empty
  # CUR_TOKEN that does not match $OWN_TOKEN means another /land has
  # reclaimed this lock since this pass last checked: re-stamping now would
  # overwrite the NEW holder's live record with this pass's stale identity,
  # concealing the overlap rather than surfacing it (see the header's
  # OWNERSHIP CHECK section). Refuse instead -- loud, non-fatal to the
  # caller's own step (heartbeat's existing contract), same exit code (1) as
  # every other heartbeat-write failure below.
  if [ -n "$OWN_TOKEN" ] && [ -n "$CUR_TOKEN" ] && [ "$OWN_TOKEN" != "$CUR_TOKEN" ]; then
    echo "land-lock: heartbeat REFUSING to overwrite $LOCK -- its current" \
      "owner token ($CUR_TOKEN) does not match this pass's own ($OWN_TOKEN)." \
      "Another /land has reclaimed this lock; this pass no longer holds it," \
      "and re-stamping would silently conceal that overlap. Not fatal to" \
      "this step by itself, but this pass should stop treating itself as" \
      "the lock holder." >&2
    exit 1
  fi

  if [ -z "$CUR_TOKEN" ]; then
    CUR_TOKEN="${OWN_TOKEN:-$(new_token)}"
  fi
  if lock_record "$CUR_TOKEN" > "$LOCK" 2>/dev/null; then
    exit 0
  fi
  echo "land-lock: heartbeat could not write $LOCK (unwritable or missing git" \
    "dir, or no space). Not fatal to this step by itself -- the token simply" \
    "ages from its last successful stamp -- but a human should check" \
    "disk/permissions if this repeats every tick." >&2
  exit 1
fi

# acquire
#
# Portability check FIRST: `flock(1)` is util-linux, absent on
# macOS/git-bash. Fail loudly and skip the tick rather than silently falling
# back to the old, two-winner-capable behaviour -- see CAVEAT 2 in the header
# for the full tradeoff.
if ! command -v flock >/dev/null 2>&1; then
  echo "land-lock: MACHINE FAULT -- 'flock' (util-linux) not found on PATH." \
    "This platform cannot serialize a reclaim safely; landing stays blocked" \
    "until flock is installed (see CAVEAT 2 in this script's header for the" \
    "portability tradeoff). Skipping this tick." >&2
  note_machine_fault
  exit 1
fi

# Open the dedicated mutex file on an unused fd and take an exclusive flock
# on it, bounded by FLOCK_TIMEOUT_SECONDS. Everything from here to the end of
# this script's `acquire` path -- the fresh-lock attempt, the staleness
# check, and the reclaim -- runs inside this one mutex (CAVEAT 2): a kernel
# `flock` is released the instant this process exits, by any means, so there
# is no gate object whose lifecycle a competitor can misjudge. `exec {fd}>`
# (bash's automatic-fd-allocation form) sidesteps picking a fixed fd number
# that might collide with one already in use by the caller's own shell.
# Wrapped in `if !` rather than left bare: under `set -e` a bare failing
# `exec` redirection (e.g. an unwritable git dir -- the same MACHINE FAULT
# `write_lock` below is built to report) would abort the script immediately
# with bash's own raw, unattributed diagnostic, before this script's MACHINE
# FAULT branch ever runs. Folding the open into the same class as "cannot
# create $LOCK" keeps that failure observable and consistently worded.
if ! { exec {FLOCK_FD}>"$FLOCK_FILE"; } 2>/dev/null; then
  echo "land-lock: MACHINE FAULT -- cannot open $FLOCK_FILE (unwritable or" \
    "missing git dir, or no space). This is not another lander; landing" \
    "stays blocked until it is fixed. Skipping this tick." >&2
  note_machine_fault
  exit 1
fi
if ! flock -x -w "$FLOCK_TIMEOUT_SECONDS" "$FLOCK_FD"; then
  # Transient, not a MACHINE FAULT -- the mutex itself works; another acquire
  # is just genuinely mid-flight.
  reset_fault_count
  echo "land-lock: another /land appears to be mid-acquire on this machine" \
    "(held the flock on $FLOCK_FILE for ${FLOCK_TIMEOUT_SECONDS}s+) --" \
    "skipping this tick." >&2
  exit 1
fi

TOKEN="$(new_token)"
if write_lock "$TOKEN"; then
  reset_fault_count
  echo "land-lock: acquired (token $TOKEN)"
  exit 0
fi

# `write_lock` failed. Distinguish the two reasons, because they need opposite
# remedies and the misleading one blocks landing indefinitely: the lock file
# genuinely exists (another pass, or an abandoned one), OR it could not be
# created at all (unwritable/missing .git, a full disk). `write_lock` discards
# its own stderr to keep the ordinary "already exists" case quiet, so the file
# itself is the only signal left -- name the machine fault explicitly rather
# than reporting it as "another /land is running" for as long as it persists
#.
if [ ! -e "$LOCK" ]; then
  echo "land-lock: MACHINE FAULT -- cannot create $LOCK (unwritable or missing" \
    "git dir, or no space). This is not another lander; landing stays blocked" \
    "until it is fixed. Skipping this tick." >&2
  note_machine_fault
  exit 1
fi

# Read the recorded acquire time (3rd field: epoch seconds) to judge staleness
# -- never the PID (1st field), which is human-only (see header). One `read`
# serves both the staleness check and the diagnostics below. A malformed or
# unreadable record (truncated write, hand-edited, ...) is treated as "age
# unknown" rather than crashing: stay conservative and skip rather than guess.
RECORD=""
read -r RECORD < "$LOCK" || true
RECORDED_EPOCH="$(epoch_of "$RECORD")"

if [ -z "$RECORDED_EPOCH" ]; then
  skip_lock_still_held "$RECORD"
fi

AGE=$(( $(date -u +%s) - RECORDED_EPOCH ))
if [ "$AGE" -lt "$STALE_SECONDS" ]; then
  skip_lock_still_held "$RECORD"
fi

# Stale, and this pass holds the flock -- no other racer can be anywhere in
# this section, so the reclaim itself needs no further coordination: no
# retry loop, no gate object, no ownership re-check. This IS the atomic
# reclaim (CAVEAT 2).
echo "land-lock: reclaiming stale lock (age ${AGE}s >= ${STALE_SECONDS}s)," \
  "previously held by: $RECORD"
rm -f "$LOCK"
if write_lock "$TOKEN"; then
  reset_fault_count
  echo "land-lock: acquired via reclaim (token $TOKEN)"
  exit 0
fi
echo "land-lock: MACHINE FAULT -- could not write the lock after reclaiming" \
  "it (unwritable or missing git dir, or no space). This is not another" \
  "lander; landing stays blocked until it is fixed. Skipping this tick." >&2
note_machine_fault
exit 1
