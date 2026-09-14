#!/usr/bin/env bash
#
# Select the SemVer-greatest `vX.Y.Z` git tag (NOT simply the most recently
# created one), and optionally check whether a candidate version strictly
# exceeds it, for /release's Section 1 tag lookup
# (.claude/skills/release/SKILL.md#1-find-the-latest-tag) and
# scripts/release.sh's tag-monotonicity gate.
#
# Extracted this exact tag-selection loop + version_gt
# SemVer comparator used to be duplicated as raw, untested inline shell in
# BOTH .claude/skills/release/SKILL.md (~lines 32-57) and scripts/release.sh
# (~lines 67-86) -- free to drift between the two copies, meaning the skill
# could propose a bump against one baseline tag while release.sh's own
# monotonicity check ran against a DIFFERENT one. Same "ungated inline shell
# in a SKILL.md rots silently" lesson as scripts/release-bump.sh
# and scripts/merge-precheck.sh.
#
# Usage:
#   scripts/release-latest-tag.sh                 # print the latest vX.Y.Z tag (or nothing)
#   scripts/release-latest-tag.sh --gt VERSION     # exit 0 iff VERSION (bare X.Y.Z, no
#                                                   # leading 'v') is SemVer-strictly-greater
#                                                   # than the latest tag
#
# Mode 1 (no args): prints the latest matching tag to stdout WITH its
# leading "v" (e.g. "v1.2.0"), or prints nothing at all (empty stdout,
# still exit 0) when no `vX.Y.Z` tag exists yet -- that is the legitimate
# first-release answer, not a fault.
#
# Mode 2 (--gt VERSION): no stdout either way. Exit 0 means VERSION exceeds
# the latest tag, OR no tag exists at all (first release -- anything
# exceeds "nothing"). Exit 1 means it does not.
#
# Exit 2 in EITHER mode -> MACHINE FAULT (git failure, bad usage,
# malformed VERSION argument). Diagnostic to stderr, nothing to stdout --
# same "exit 2 is the machine, never the content" convention as
# scripts/release-bump.sh / scripts/merge-precheck.sh.
#
# Tag selection is SemVer-greatest, NOT most-recently-created: a tag can be
# created out of order (e.g. a backport release cut after a later one), so
# sorting `git tag -l` by name or creation date is not equivalent to sorting
# by SemVer value. Every candidate tag is compared NUMERICALLY
# component-by-component (never as a string), so multi-digit components
# compare correctly: v0.10.0 > v0.9.0. A tag is only a candidate at all if
# it is EXACTLY `vX.Y.Z` -- three all-numeric, dot-separated components and
# nothing else. This is stricter than the glob the inline snippets used
# (`[0-9]*.[0-9]*.[0-9]*`, a case pattern where the bare `*` matches ANY
# trailing text): that glob let "v1.2.3-rc1", "v1.2.3.4", and "v1.2.3beta"
# all through as if they were plain releases. Verified by hand while writing
# this script -- all three matched the old case pattern.
#
# Read-only: runs `git tag`, never touches the working tree, never writes bd.

set -uo pipefail

# The source itself must fail CLOSED -- see gate-lib.sh's Usage
# section for the measurement and why the guard can't use the library it loads.
# --no-advisory: this gate carries no domain-specific trailer, the
# same shape as scripts/release-bump.sh -- but the sentinel is REQUIRED, never
# omitted. Omitting it would fold this script's own `--gt VERSION` arguments
# into GATE_ADVISORY instead; see gate-lib.sh's GATE_ADVISORY contract for why
# the bare `scripts/release-latest-tag.sh` form would NOT make that visible.
# shellcheck source=gate-lib.sh
if ! . "$(dirname "$0")/gate-lib.sh" --no-advisory; then
  echo "GATE COULD NOT RUN: scripts/gate-lib.sh is missing or unreadable" >&2
  echo "next to $0 -- this is a machine/checkout fault, not a branch verdict." >&2
  exit 2
fi

# $1 > $2, both bare X.Y.Z (no leading "v"). NUMERIC per-component
# comparison, never string comparison, so multi-digit components compare
# correctly (v0.10.0 > v0.9.0). Both arguments are guaranteed by the callers
# below to already be exactly three numeric dot-separated components.
version_gt() {
  local -a a b
  IFS=. read -ra a <<< "$1"
  IFS=. read -ra b <<< "$2"
  for i in 0 1 2; do
    if [ "${a[i]}" -gt "${b[i]}" ]; then return 0; fi
    if [ "${a[i]}" -lt "${b[i]}" ]; then return 1; fi
  done
  return 1
}

# Exactly three all-numeric, dot-separated components (see the header comment
# for why this is stricter than the glob the inline snippets used). ONE
# definition, applied both to the --gt argument and to every candidate tag --
# a script whose whole purpose is ending duplicated SemVer logic should not
# carry two copies of the rule internally.
is_semver() { [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; }

MODE="latest"
CANDIDATE=""
case "${1:-}" in
  --gt)
    if [ "$#" -ne 2 ]; then
      gate_could_not_run \
        "usage: release-latest-tag.sh --gt VERSION" \
        "Got $# argument(s), expected exactly 2 (--gt and a bare X.Y.Z version)."
    fi
    MODE="gt"
    CANDIDATE="$2"
    if ! is_semver "$CANDIDATE"; then
      gate_could_not_run \
        "release-latest-tag.sh --gt: version must be bare X.Y.Z (no leading 'v'), got '$CANDIDATE'"
    fi
    ;;
  "")
    ;;
  *)
    gate_could_not_run \
      "usage: release-latest-tag.sh [--gt VERSION]" \
      "Got unrecognized argument '$1'."
    ;;
esac

errfile="$(mktemp 2>/dev/null)" || gate_could_not_run \
  "could not create a temporary file (mktemp failed)" \
  "Usual causes: TMPDIR points at a nonexistent, full, or read-only filesystem."
trap 'rm -f "$errfile"' EXIT

if ! TAGS="$(git tag -l 'v*' 2>"$errfile")"; then
  lines=("git tag -l 'v*' failed." "This is never a statement about which tag is latest.")
  err="$(<"$errfile")"
  if [ -n "$err" ]; then
    lines+=("git's own error output:")
    while IFS= read -r errline; do lines+=("$errline"); done <<< "$err"
  fi
  gate_could_not_run "git tag failed" "${lines[@]}"
fi

LATEST=""
while IFS= read -r t; do
  [ -n "$t" ] || continue
  tv="${t#v}"
  if ! is_semver "$tv"; then
    continue
  fi
  if [ -z "$LATEST" ] || version_gt "$tv" "$LATEST"; then
    LATEST="$tv"
  fi
done <<< "$TAGS"

if [ "$MODE" = "latest" ]; then
  if [ -n "$LATEST" ]; then
    echo "v$LATEST"
  fi
  exit 0
fi

# --gt mode: CANDIDATE strictly exceeds LATEST, or no LATEST exists at all
# (first release -- anything exceeds "nothing").
if [ -z "$LATEST" ] || version_gt "$CANDIDATE" "$LATEST"; then
  exit 0
else
  exit 1
fi
