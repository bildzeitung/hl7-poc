#!/usr/bin/env bash
#
# PreToolUse(Bash) guard body: deny a Bash call that creates a bd
# ticket with `--deps blocks:<id>`, which INVERTS the edge -- it makes <id>
# blocked_by the NEW ticket, not the reverse. The correct form is two steps:
# create with no --deps, then `bd dep add <new-id> <id> --type blocks`.
#
# Usage: scripts/bd-deps-blocks-guard.sh '<bash command string>'
#
# Prints nothing (allow) or one PreToolUse hookSpecificOutput JSON object with
# permissionDecision "deny" -- same contract as the an earlier fix/an earlier fix guards.
# ALWAYS exits 0: a PreToolUse hook exiting non-zero is itself a defect.
#
# Extracted from .claude/settings.json (an earlier fix's acceptance criterion, applied
# to the remaining two inline guards: "the guard logic lives in a tested script,
# not untested inline shell" -- ungated inline shell in config is where this repo
# has already shipped silent undetected-for-months bugs / an earlier fix).
# Behaviour is unchanged by the extraction; tests/test_bd_deps_guard.py drives
# both this script and the shipped wrapper.
#
# NOTE: this file may use bash-only syntax -- it runs under `bash "$SCRIPT"`, not
# under the harness's dash /bin/sh. The WRAPPER in settings.json is what must stay
# POSIX, and its test pins that.

set -euo pipefail

CMD="${1:-}"
[ -n "$CMD" ] || exit 0

# Collapse backslash-newline continuations so a real multi-line
# `bd create --title=... \` call is still seen as ONE invocation segment
#.
CMD=$(printf '%s' "$CMD" | sed -e :a -e '/\\$/N; s/\\\n/ /; ta')

# Match only at a COMMAND POSITION (line start, or after ;/&&/||/|/$ so prose
# that merely quotes the bad form -- a commit message, a bd note, this repo's own
# docs -- is not denied. Covers the `bd new` alias and bd's global
# -C/--directory/--db flags. The interior `.*` deliberately OVER-matches across
# quoted metacharacters: a regex cannot tell a ';' inside a --title from a real
# separator, and the accepted false denies are catalogued in the tests.
PATTERN='(^|[;&|(])[[:space:]]*bd([[:space:]]+(-C|--directory|--db)([[:space:]]+|=)[^[:space:]]+)*[[:space:]]+(create|new)\b.*--deps[= ]+[^[:space:]]*blocks:'

printf '%s' "$CMD" | grep -qE "$PATTERN" || exit 0

jq -n '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:"bd create --deps blocks:<id> inverts the edge: it makes <id> blocked_by this NEW ticket, not the reverse. Create the ticket with no --deps, then run: bd dep add <new-id> <id> --type blocks"}}'

exit 0
