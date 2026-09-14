#!/usr/bin/env bash
#
# Shared missing-vs-empty policy for a skill's scratch-file loads. Despite the name -- kept as-is rather than renamed, see the
# an earlier fix entry in docs/decisions.md -- this script is NOT $STATE_DIR-
# specific: it takes a plain path argument and is used by both `/land`
# (`$STATE_DIR`, `.git/land-state/`) and `/sweep`.
#
# `/land`'s SKILL.md originally read files under $STATE_DIR at four call
# sites, encoding exactly TWO policies with FOUR different hand-rolled
# spellings:
#
#   1. Section 3 first-pass accepted:  cat || { <diagnostic>; exit 1; }   missing fatal, empty OK
#   2. Section 3 isolation accepted:   cat || exit 1  +  [ -n ... ] || exit 1   both fatal
#   3. Section 4 landed:               cat || exit 1                          missing fatal, empty OK -- but SILENT
#   4. Kick-back conflicts:            cat 2>/dev/null + [ -n ... ]            both fatal
#
# Site 3 has the SAME policy as site 1 but no diagnostic at all -- the
# loud/silent asymmetry an earlier fix fixed at site 1 still exists one section
# later. This script makes the policy a visible ARGUMENT instead of a
# spelling a future editor has to pick from by guess, and gives it shellcheck
# + unit tests instead of living only in a markdown fence no gate parses.
#
# an earlier fix adopted this for five more sites in `/sweep`'s SKILL.md, all on
# the "default" policy. A caller that does NOT adopt it explains why at its
# own call site -- that reasoning is about the caller, not about this script,
# so do not accumulate it here.
#
# Usage: scripts/land-state-load.sh <file> [--require-nonempty] [-- <context line>...]
#
# Reads <file> and prints its content to stdout, normalized to exactly one
# trailing newline (not byte-exact passthrough: the content is captured into a
# shell variable, which strips trailing newlines, and one is re-added). Every
# call site word-splits the result, so that normalization is invisible to all
# of them -- but do not describe this as a `cat` passthrough to a future
# consumer that does not word-split.
#
# Exit 0 -> the file was readable, and (unless --require-nonempty) it may be
#           empty or whitespace-only. Content on stdout.
# Exit 1 -> the file could not be read at all (missing, unreadable, or a
#           directory in its place -- `cat`'s OWN error goes straight to this
#           script's stderr, i.e. the caller's, immediately BEFORE the
#           diagnostic below; that is exactly what the inline `cat`s this
#           replaces did, and why nothing here captures or re-prints it), OR
#           --require-nonempty was given and the file read clean but came
#           back empty. Either way: a diagnostic to stderr, nothing to stdout.
#
# WHAT "EMPTY" MEANS UNDER --require-nonempty, precisely: the content with
# trailing newlines stripped (command substitution does that) must be a
# non-empty string. So a file of only newlines IS empty; a file containing
# spaces or tabs is NOT -- it passes. That is deliberately the exact
# behaviour of the `[ -n "$(cat ...)" ]` this replaces at both --require-
# nonempty call sites, so the retrofit is a pure refactor; do not "tighten"
# it to trim spaces without going and checking what each call site would
# then start rejecting. Pinned by tests/test_land_state_load.py.
#
# This is deliberately exit 1, never 2, and this script deliberately does NOT
# source scripts/gate-lib.sh. Every call site is an agent-executed skill fence
# in .claude/skills/land/SKILL.md, so there is no calling SCRIPT to classify
# the code programmatically. The canonical statement of that split is
# gate-lib.sh's own header ("WHY THIS EXIT-2 CONVENTION BINDS scripts/*.sh AND
# NOT AGENT-EXECUTED SKILL FENCES"), cross-referenced from
# docs/agents-workflow.md -- not restated here.
#
# Two policies, one flag:
#   (default)           missing -> fatal, empty -> OK (prints nothing, exit 0)
#   --require-nonempty   missing -> fatal, empty -> ALSO fatal
#
# Any arguments after a literal `--` are appended to the diagnostic, one per
# line, so a call site can still explain WHY this particular load mattered
# (e.g. "3a's precompute did not run", "nothing to attribute this red to")
# without the policy itself needing to vary per site.

set -uo pipefail   # deliberately NOT -e -- see merge-precheck.sh's identical
                   # note: this script's job is to inspect a command's exit
                   # code, which -e would short-circuit.

if [ "$#" -lt 1 ]; then
  echo "usage: land-state-load.sh <file> [--require-nonempty] [-- <context line>...]" >&2
  exit 1
fi

file="$1"
shift

require_nonempty=false
if [ "$#" -ge 1 ] && [ "$1" = "--require-nonempty" ]; then
  require_nonempty=true
  shift
fi

context=()
if [ "$#" -ge 1 ]; then
  if [ "$1" = "--" ]; then
    shift
    context=("$@")
  else
    echo "usage: land-state-load.sh <file> [--require-nonempty] [-- <context line>...]" >&2
    echo "unexpected argument: $1" >&2
    exit 1
  fi
fi

fail() {
  echo "STATE LOAD FAILED: $1" >&2
  for line in "${context[@]}"; do echo "$line" >&2; done
  exit 1
}

# NOT redirected: `cat`'s own stderr ("No such file or directory", "Is a
# directory", a permission message) is the only text naming WHICH of the three
# read failures happened, and it reaches the operator by simply being left
# alone -- this script's stderr is the caller's. Deliberately not captured into
# a temp file and re-printed the way scripts/merge-precheck.sh does: that
# script has to fold git's stderr into a structured banner a CALLING SCRIPT
# classifies, which is exactly the situation the exit-1 note above says does
# not exist here. The four inline `cat`s this replaces let it through the same
# way ("cat prints the specific reason to this call's stderr, so the operator
# sees which" -- SKILL.md, pre-an earlier fix), so the operator's view is unchanged
# apart from cat's line now sitting immediately above the banner instead of
# inside it.
content="$(cat "$file")"
rc=$?
if [ "$rc" -ne 0 ]; then
  fail "$file could not be read (missing, unreadable, or a directory in its place -- see cat's own error just above)."
fi

if [ "$require_nonempty" = true ] && [ -z "$content" ]; then
  fail "$file is missing or empty."
fi

printf '%s\n' "$content"
