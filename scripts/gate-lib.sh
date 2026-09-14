#!/bin/bash
#
# Shared helpers for the "exit 2 means the GATE could not run, never that the
# CONTENT is bad" convention: gate_could_not_run, and built on
# it escalate_unless_content.
# Sourced by every scripts/*.sh gate that draws this distinction -- discover
# the current set rather than naming it here, since a named list goes stale on
# every migration -- the same way scripts/update-deps.sh already sources
# scripts/dep-churn-lib.sh. Ask for the SOURCE LINE, not the library's name:
# `grep -l gate-lib.sh scripts/*.sh` also returns this file plus any script
# that merely explains why it does NOT source the library, so use
#   grep -lE '^[^#]*\. "\$\(dirname "\$0"\)/gate-lib\.sh"' scripts/*.sh
# which is the same question tests/test_gate_lib.py's `_sources_gate_lib` asks.
#
# Extracted this exact function had reached three duplicated
# literal copies, free to drift --
# and had already started to: validate-mermaid.sh's copy printed a two-line
# domain advisory ("not a mermaid syntax error ...") that merge-precheck.sh's
# ("not a branch conflict ...") and release-bump.sh's (no advisory at all)
# each stated differently, so the "exit 2 is the machine, never the content"
# contract was spelled out three slightly different ways. Same "reaches three
# copies, extract" precedent as scripts/epic-children-closed.sh and
# scripts/recycled-worktree-guard.sh, both extracted for exactly that reason.
#
# Usage (from a sourcing script) -- the source itself MUST be guarded so a
# missing/unreadable gate-lib.sh fails CLOSED (exit 2), never falls through to
# whatever happens next when gate_could_not_run is called but was never
# defined. MEASURED: a bare, unguarded
# `. "$(dirname "$0")/gate-lib.sh"` under `set -uo pipefail` (no -e, the
# convention every consumer uses) does NOT stop the script when the source
# fails -- it just leaves gate_could_not_run undefined, and the first call
# site then resolves to a bash "command not found" whose exit code is
# whatever the surrounding logic happens to produce next: measured as 0, 1,
# and 127 across two real consumers, never the required 2. The guard cannot
# depend on this library (it doesn't exist yet at that point), so it is
# small, plain bash, duplicated verbatim in every consumer -- and pinned
# byte-for-byte across all of them by tests/test_gate_lib.py's sweep, which
# discovers the consumer set rather than listing it, so a NEW consumer that
# sources this library bare is caught the day it lands. GATE_ADVISORY (below)
# is passed on this SAME source line, at source time -- a consumer with
# advisory lines writes:
#
#   # shellcheck source=gate-lib.sh
#   if !. "$(dirname "$0")/gate-lib.sh" \
#        "advisory line 1" \
#        "advisory line 2"; then
#     echo "GATE COULD NOT RUN: scripts/gate-lib.sh is missing or unreadable" >&2
#     echo "next to $0 -- this is a machine/checkout fault, not a branch verdict." >&2
#     exit 2
#   fi
#   gate_could_not_run "one-line summary" "cause line 1" "cause line 2" ...
#
# A consumer with NO advisory trailer writes the identical block with the
# literal sentinel `--no-advisory` in place of the advisory strings --
# never with nothing there (see GATE_ADVISORY below for why "nothing" is
# unsafe):
#
#   if !. "$(dirname "$0")/gate-lib.sh" --no-advisory; then
#
# NOTE the deliberate absence of `2>/dev/null` on that source: bash's own
# message ("No such file or directory", or a syntax error with a line number
# if this file is present but corrupt) is the only evidence of WHICH failure
# occurred, and the guard's own two lines cannot reproduce it. Suppressing it
# would report a corrupt library as a missing one -- naming one confident
# cause for every failure, which is the an earlier fix bug relocated.
#
# Prints "GATE COULD NOT RUN: <summary>" to stderr, then every remaining
# argument on its own line (also stderr), then any lines in the caller's
# GATE_ADVISORY array (see below), then exits the CALLING PROCESS with status
# 2 -- NEVER 0 or 1, which are live content verdicts in every caller
# (merge-precheck.sh alone treats 0/1/2 as three distinct verdicts; this
# helper only ever produces the exit-2 arm, so that three-way split is
# unaffected). Call it only from the top-level script process, never from
# inside a command substitution: a subshell's `exit` only ends the subshell,
# not the calling script -- every existing call site already handles this
# correctly (e.g. release-bump.sh's `read_log { ...; } ... || exit $?`
# propagation out of its own command-substitution subshell).
#
# WHY THIS EXIT-2 CONVENTION BINDS scripts/*.sh AND NOT AGENT-EXECUTED SKILL
# FENCES: the 0/1/2 split above exists so that a CALLER -- another
# script, invoking one of these as a subprocess -- can classify the exit code
# programmatically without parsing stderr. Several skill markdown files (e.g.
# .claude/skills/land/SKILL.md, .claude/skills/release/SKILL.md) contain
# fenced bash blocks that print this same "GATE COULD NOT RUN:" banner and
# then exit 1, not 2, when they hit a machine/checkout fault instead of a
# genuine content verdict. That is CORRECT and not a violation of the
# convention above: those fences are executed directly by an agent working
# through the skill, one Bash-tool invocation at a time -- there is no calling
# SCRIPT to classify the exit code, only the agent itself, which reads the
# stderr banner text directly. Exit 1 there is a human/agent-readable signal
# ("stop this pass, something is broken"), not a machine-classified one, so
# the 2-vs-1 distinction this file exists to enforce does not apply. Do NOT
# change an in-skill call site to exit 2 to "match" this file; this paragraph
# is the canonical statement of the split, and docs/agents-workflow.md
# cross-references it rather than restating it.
#
# GATE_ADVISORY: this section is the OPERATIVE record of the
# contract -- correct it in place. docs/decisions.md also
# carries a dated snapshot of the decision and the alternative that was
# weighed; changing anything below means marking that entry superseded rather
# than editing it, per that file's own preamble.
#
# GATE_ADVISORY is a bash array of fixed, domain-specific advisory
# lines appended after every caller-supplied cause on a gate_could_not_run
# call -- e.g. merge-precheck.sh's "do not kick this branch back needs-rebase
# in place of diagnosing it." SET STRUCTURALLY, at source time, from the
# positional arguments passed on the source line itself (see Usage above):
# this file assigns GATE_ADVISORY from "$@" before returning control to the
# sourcing script, i.e. before a single line of that script beyond the source
# line itself has run. There is no longer a separate assignment statement for
# a call site to accidentally sit above.
#
# THIS USED TO BE AN ORDERING CONVENTION, not a structural property: the
# advisory lived in a separate `GATE_ADVISORY=(...)` statement a sourcing
# script wrote itself, and a call site placed above that assignment still
# exited 2 with a correct banner but silently emitted HALF the contract --
# invisible to `set -u` (a validly declared-empty array), to shellcheck (the
# SC2034 disable every caller needed suppressed its view), and to this
# library's own tests (which chose their own orderings). That hazard is now
# categorically impossible: the assignment is this line, and this line runs
# as part of sourcing, which necessarily precedes everything else in the file
# that sources it.
#
# A DIFFERENT discipline replaces it, smaller in scope (once per consumer
# FILE, not once per call site, and mechanically swept the same way the
# fail-closed source guard above already is) -- documented bash behaviour,
# re-verified empirically on 5.2: "If any arguments are supplied, they become
# the positional parameters when filename is executed. Otherwise the
# positional parameters are unchanged." So `source file` with NO trailing
# tokens does NOT clear $@ inside file, it inherits the CALLING script's
# CURRENT positional parameters. A bare `. "$(dirname "$0")/gate-lib.sh"` --
# no advisory strings, no sentinel -- therefore folds the CONSUMER's OWN argv
# into GATE_ADVISORY, printed as if it were a fixed advisory trailer on every
# GATE COULD NOT RUN exit. A consumer that wants no advisory trailer must
# pass the literal sentinel `--no-advisory`, never nothing (see
# release-bump.sh / release-latest-tag.sh).
#
# DO NOT rely on that leak to announce a forgotten sentinel -- it only shows
# up when the consumer happens to be holding CLI arguments at the moment it
# sources. A consumer invoked with NO arguments (validate-mermaid.sh takes
# none at all; release-latest-tag.sh's bare form takes none) leaves $# at 0,
# so a bare source yields an empty GATE_ADVISORY that is silently
# indistinguishable from a correct `--no-advisory`. What actually enforces
# this for every consumer, argv or not, is STATIC: tests/test_gate_lib.py's
# discovered sweep asserts every consumer's source line supplies either
# advisory strings or the sentinel -- never a bare source with zero trailing
# tokens -- so a NEW consumer that forgets this is caught the day it lands,
# the same enforcement shape as the source guard sweep above.
#
# `source file arg1 arg2` sets $1.. inside file for the duration of the
# source command, then restores the CALLER's own $1.. to exactly what they
# were the instant the source command returns (verified, bash 5.2) -- so a
# consumer's own arg-count check below the source line sees precisely what it
# would have seen without any of this.
#
# WEIGHED AND REJECTED: binding GATE_ADVISORY through a variable-
# assignment PREFIX on the source command instead (e.g.
# `GATE_ADVISORY_LINES=$'a\nb'. gate-lib.sh`, with this file doing
# `mapfile -t GATE_ADVISORY <<< "$GATE_ADVISORY_LINES"`) rather than
# positional args. "No advisory" becomes the natural unset default, which
# would retire the --no-advisory sentinel, its sweep, and that sweep's
# non-vacuity proof. Re-measured on bash 5.2.21: the prefix variable does NOT
# persist past the source in default mode, but DOES persist under
# `set -o posix` (`.` is a POSIX special builtin, and assignment-prefix
# persistence on a special builtin is specified behaviour) -- trading this
# file's one documented, tested subtlety for a less-known, mode-dependent
# one, and losing the bash array at the point the value is built (a
# newline-joined string reconstituted with mapfile is more machinery, not
# less). Not a demonstrated improvement over the sentinel below; full
# reasoning in docs/decisions.md -- and it is THAT
# entry, not an earlier fix's, that this paragraph's own scope note refers to:
# revisiting the rejection means marking the an earlier fix entry superseded.
if [ "$#" -eq 1 ] && [ "$1" = "--no-advisory" ]; then
  GATE_ADVISORY=()
else
  GATE_ADVISORY=("$@")
fi

gate_could_not_run() {
  echo "GATE COULD NOT RUN: $1" >&2
  shift
  for line in "$@"; do echo "$line" >&2; done
  for line in "${GATE_ADVISORY[@]}"; do echo "$line" >&2; done
  exit 2
}

# escalate_unless_content. The shared partition of a command's
# OWN exit code: 1 = CONTENT ("no match"), anything else = MACHINE fault.
# Extracted once the idiom had reached six open-coded copies, double this
# repo's own extract-at-three precedent. Find the
# current call sites with `grep -n escalate_unless_content scripts/*.sh`
# rather than trusting a list here, for the same reason this file's own
# header above refuses to name its consumer set.
#
# Only the `rc=$?`/`-ne 1` test moves here -- the caller keeps its own
# `if`/`else`, so its success arm and no-match arm stay open-coded
# byte-for-byte at the call site.
#
# Usage (caller's `else` arm, after capturing `rc=$?`):
#   escalate_unless_content "$rc" "cause line 1" "cause line 2" ...
# `rc=$?` must be the FIRST command in that arm; anything above it clobbers
# `$?`. And never `if ! cmd; then rc=$?`: `! cmd`'s `$?` is cmd's status
# LOGICALLY NEGATED (0<->1), not cmd's own status -- so a machine fault
# arrives here reading as a clean "no match", the exact inversion this
# partition exists to prevent. Measured while writing an earlier fix's tests, the
# fix that introduced the first copy of this idiom.
#
# Returns 0 on the content path rather than merely declining to escalate: a
# consumer may run under `set -e`, where a nonzero return here would abort
# that gate mid-loop with exit 1 -- which in a gate whose content verdict IS
# exit 1 is a fabricated content verdict. Stated as a property of the
# contract, not of one caller: scripts/validate-mermaid.sh was the original
# such caller and dropped `-e` in an earlier fix, but this guarantee must not
# depend on which consumers currently set it. Pinned by
# tests/test_gate_lib.py's `-e` case.
#
# Does NOT cover scripts/merge-precheck.sh's exit-code checks: that script
# has TWO live content codes (0 = clean, 1 = conflict), and its rc is
# captured from a command substitution, not an `else` arm's `$?`. More
# generally: a caller with more than one live content code, or whose rc does
# not come from an `if`/`else` arm's `$?`, does not fit.
escalate_unless_content() {
  local rc="$1"
  shift
  [ "$rc" -eq 1 ] && return 0
  gate_could_not_run "$@"
}
