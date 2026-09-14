#!/usr/bin/env bash
#
# PreToolUse(Bash) guard body: deny any `gh` invocation
# that does not match a small, explicit READ-ONLY allowlist -- DEFAULT-DENY,
# not a write-verb denylist. `gh` is authed as the user, so a
# false ALLOW spends the user's public identity irrecoverably (the
# notification already went out); a false DENY costs a reword. Full
# rationale: docs/agents-workflow.md ("Never write to an external tracker
# under the user's identity").
#
# Usage: scripts/gh-write-guard.sh '<bash command string>'
#
# Prints nothing (allow -- fall through to normal permission handling) or one
# PreToolUse hookSpecificOutput JSON object with permissionDecision "deny".
# ALWAYS exits 0: a PreToolUse hook exiting non-zero is itself a defect.
#
# Extracted from .claude/settings.json so the scanning logic can be driven directly by
# tests/test_gh_write_guard.py, and so the quote-aware split below -- which
# needs bash array/substring primitives dash does not have -- has somewhere to live.
#
# NOTE: this file may use bash-only syntax -- it runs under `bash "$SCRIPT"`, not
# under the harness's dash /bin/sh. The WRAPPER in settings.json is what must stay
# POSIX, and its test pins that.
#
# QUOTE-AWARE SEGMENT SPLIT and QUOTED-HEREDOC PRE-PASS.
# The command is split into candidate "invocation segments" at shell
# control-operator characters (; & |  { } `) so a `gh ...` phrase
# mid-command (after `&&`, inside `$(...)`, ...) is still caught at a command
# position -- and a quoted heredoc body (a commit-message worked example, a
# doc quoting real shell) is stripped first so it cannot manufacture a fake
# segment start either. Both primitives (`_split_unquoted`,
# `strip_quoted_heredoc_bodies`) now live in scripts/shell-quote-split.sh
#, shared with scripts/sha-fabrication-guard.sh -- see that file's
# header for the full rationale, the false-positive shapes each fix closes,
# and the fail-open residuals each deliberately accepts. Differential and
# mutation coverage for both: tests/test_gh_write_guard.py.

set -euo pipefail

CMD="${1:-}"
[ -n "$CMD" ] || exit 0

# This guard runs on EVERY Bash tool call, and the split + five greps below cost
# ~45ms each time -- but the quote-aware _split_unquoted char loop below them
# is O(n^2), so a plain `*gh*` substring test here is far too loose
# a gate: it lets through any LONG command that merely contains "gh" inside an
# ordinary word (through/highlight/night/eight/brought/high/light/straight --
# i.e. most commit messages, `bd update --notes`, and heredoc prose this repo
# writes constantly), paying the O(n^2) cost on gh-free text (# measured 469ms-3.5s on an 8-25 KB command containing "through", vs ~3ms for a
# command without "gh" anywhere at all).
#
# Tightened to a command-POSITION test: `gh` must be preceded by start-of-string
# or a non-identifier character, and followed by whitespace. This is NOT a
# narrowing of the deny surface: P below -- the regex that actually
# decides what counts as a `gh` command position -- only ever matches `gh`
# preceded by start-of-string, whitespace, or a literal `/` (the sole optional
# path-prefix group, which itself must end in `/`), and always followed
# immediately by whitespace (every alternative in P's grammar reduces to
# `gh(...)?[[:space:]]+`, and the optional flag-group repetition, when present,
# itself starts with whitespace). Every segment START `_split_unquoted` can ever
# produce is a shell control character (`;&|{}\``, or `$(` inside double
# quotes) -- itself non-alnum-non-underscore -- or the string start. So every
# position P could ever match already satisfies this test; the pre-filter
# cannot skip a case P would have caught. Verified against every DENIED case in
# tests/test_gh_write_guard.py at both script and hook level: zero deny-side
# regressions.
#
# CASE-INSENSITIVE (`[Gg][Hh]`), deliberately, and NOT the old `*gh*` filter's
# case-sensitivity. Every grep below runs `-i`, so P really does match `GH `/`Gh `
# at a command position; a case-SENSITIVE pre-filter would therefore skip a case P
# would have caught -- exactly the narrowing this block claims not to do. Measured
# differentially during review: with a lowercase `gh` present incidentally
# elsewhere, `git commit -m "walking through"; GH issue create --title x` went
# DENY (old filter) -> ALLOW (case-sensitive tightened filter). The old filter's
# behaviour on this axis was incoherent rather than protective -- a STANDALONE
# `GH issue create` was already allowed by it, since the command contains no
# lowercase `gh` at all -- so matching it exactly would have preserved an
# accidental half-catch and dropped the other half. Matching P's own case-folding
# instead makes the pre-filter a strict superset of P in both directions: nothing
# P can catch is skipped, and the one behaviour change versus main is
# allow -> DENY on a standalone uppercase invocation (the conservative direction,
# and a live write on a case-insensitive filesystem).
[[ "$CMD" =~ (^|[^A-Za-z0-9_])[Gg][Hh][[:space:]] ]] || exit 0

# Fail CLOSED (deny) if the shared quote-aware split library cannot be
# resolved -- a missing/unreadable copy here would silently disable both the
# quoted-argument and quoted-heredoc fixes, reopening
# the exact false-positive class this guard exists to avoid. Resolved via
# this script's OWN directory (not $ROOT/PWD) so it works regardless of the
# caller's cwd. Placed AFTER the cheap early-outs above so a gh-free command
# (the overwhelmingly common case) never pays even the cost of resolving it.
#
# Plain `dirname`, deliberately NOT `$(cd "$(dirname ...)" && pwd)`: under `set -e` a failed `cd` in an assignment aborts the script
# BEFORE the fail-closed check below can run, and the wrapper's trailing
# `exit 0` then turns that into a silent ALLOW -- a fail-OPEN in the one block
# whose whole job is to fail closed. Absolutizing bought nothing anyway: bash
# resolved this very script from the same cwd, so the sibling path resolves
# identically, and this drops a subshell fork per gh-position command.
_LIB="$(dirname "${BASH_SOURCE[0]}")/shell-quote-split.sh"
if [ ! -r "$_LIB" ]; then
  jq -n '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny",
    permissionDecisionReason: "scripts/shell-quote-split.sh (the shared quote-aware split library scripts/gh-write-guard.sh depends on) could not be resolved -- denying this Bash call rather than silently scanning with the split disabled, since a false ALLOW here is unrecoverable. Surface this to a human; do not retry."}}'
  exit 0
fi
# `-r` proves the file is READABLE, not that it LOADED. A present-but-broken
# library -- truncated write, partial checkout, bad merge, syntax error --
# sources "successfully enough" and then `_split_unquoted` is undefined, `set -e`
# kills this script at the call site with rc=127 and NO stdout, and the
# wrapper's trailing `exit 0` turns that into a silent ALLOW: a fail-OPEN in
# the one block whose entire job is to fail closed. So `source` under
# `|| true` -- a syntax error must reach the check below, not abort ahead of it
# -- and then assert the CONTRACT (both functions defined), not the file.
#
# The contract is FOUR names, not two: the scan-length cap
# adds `SHELL_QUOTE_SPLIT_MAX_LEN` and `deny_if_over_scan_cap`, and an older
# library copy -- a partial checkout, or a revert of just this one file, both
# reachable since an earlier fix changed the library and the guards together --
# defines the two original functions but neither of those. The constant is read
# under `set -u` (abort, no stdout, wrapper's `exit 0` -> silent ALLOW), and the
# call site is `deny_if_over_scan_cap ... || exit 0`, whose `||` SWALLOWS the
# rc=127 of an undefined function into a silent ALLOW even without `set -u`.
# Both are the exact fail-OPEN this block closed for the functions. Verified by
# sabotage: stripping only the constant reproduced rc=1 + empty stdout in both
# guards. Assert the whole contract here, where failing is still a DENY.
# shellcheck source=scripts/shell-quote-split.sh
source "$_LIB" || true
if ! declare -F _split_unquoted >/dev/null 2>&1 ||
  ! declare -F strip_quoted_heredoc_bodies >/dev/null 2>&1 ||
  ! declare -F deny_if_over_scan_cap >/dev/null 2>&1 ||
  [ -z "${SHELL_QUOTE_SPLIT_MAX_LEN:-}" ]; then
  jq -n '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny",
    permissionDecisionReason: "scripts/shell-quote-split.sh was found but did not define the quote-aware split functions and scan-length cap scripts/gh-write-guard.sh depends on (a truncated, partially-checked-out, out-of-date, or syntactically broken copy) -- denying this Bash call rather than silently scanning with the split disabled, since a false ALLOW here is unrecoverable. Surface this to a human; do not retry."}}'
  exit 0
fi

# This hook runs on EVERY Bash call, so skip the fork entirely when there is no
# heredoc operator to find: with no `<<` present the function is provably an
# identity transform (it can only ever delete lines a `<<` match opened).
case "$CMD" in
  *'<<'*) CMD_SANITIZED=$(strip_quoted_heredoc_bodies "$CMD") ;;
  *) CMD_SANITIZED="$CMD" ;;
esac

# SCAN LENGTH CAP -- fail CLOSED (deny) rather than pay `_split_unquoted`'s
# per-character cost without bound. Enforcement lives in the shared library alongside the
# constant, so this guard and sha-fabrication-guard.sh cannot drift.
# Measured on `$CMD_SANITIZED`, i.e. exactly the string handed to `_split_unquoted` below --
# see that function's header, and docs/agents-workflow.md for the (a)/(b)/(c) decision.
deny_if_over_scan_cap "$CMD_SANITIZED" "scripts/gh-write-guard.sh" || exit 0

SEG=$(_split_unquoted "$CMD_SANITIZED")

# `gh` at a command position: through a leading VAR=x assignment, a fixed wrapper
# list, an absolute/relative path to the binary, and gh's global -R/--repo/
# --hostname flags inserted before the subcommand.
P='^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*((if|then|else|do|env|command|sudo|nohup|time|xargs)[[:space:]]+)*([^[:space:]]*/)?gh([[:space:]]+(-R|--repo|--hostname)([[:space:]]+|=)[^[:space:]]+)*[[:space:]]+'
API='api\b'
# The read-only allowlist: everything NOT matching this is presumed a write.
R='(issue[[:space:]]+(view|list|status)\b|pr[[:space:]]+(view|list|checks|diff)\b|run[[:space:]]+(list|view)\b|release[[:space:]]+(list|view)\b|repo[[:space:]]+view\b|label[[:space:]]+list\b|workflow[[:space:]]+(list|view)\b|secret[[:space:]]+list\b|variable[[:space:]]+list\b|ssh-key[[:space:]]+list\b|gpg-key[[:space:]]+list\b|cache[[:space:]]+list\b)'
# `gh api` is judged separately: field flags trigger an IMPLICIT POST with no -X
# on the line at all, which is gh's documented default and NOT a way around this.
APIWRITE='api[[:space:]]+(.*[[:space:]])?(-[fF]|(--field|--raw-field|--input)([[:space:]]|=)|(-X[[:space:]]*|--method[[:space:]=]+)[A-Za-z])'
# ...but fields on an EXPLICIT GET are query params, not a body -- gh documents
# this as the way to send a GET query string. Scoped to the SAME segment, so a
# read-then-write chain cannot let the read half exempt the write half.
APIGET='api[[:space:]]+(.*[[:space:]])?(-X[[:space:]]*|--method[[:space:]=]+)GET\b'

GH=$(printf '%s' "$SEG" | grep -iE "$P" || true)
# No segment sits at a `gh` command position -- the remaining four greps would all
# be operating on an empty string. (`gh` appears inside ordinary words like
# "through"/"highlight", so the substring pre-filter above lets plenty through here.)
[ -n "$GH" ] || exit 0
API_LINES=$(printf '%s' "$GH" | grep -iE "$P$API" || true)
NONAPI_LINES=$(printf '%s' "$GH" | grep -ivE "$P$API" || true)
UNSAFE_API=$(printf '%s' "$API_LINES" | grep -iE "$P$APIWRITE" | grep -ivE "$P$APIGET" || true)
UNSAFE_NONAPI=$(printf '%s' "$NONAPI_LINES" | grep -ivE "$P$R" || true)

if [ -n "$UNSAFE_API" ] || [ -n "$UNSAFE_NONAPI" ]; then
  REASON='this gh guard is DEFAULT-DENY -- your call does not match the read-only ALLOWLIST, so it is presumed a WRITE under your public identity and denied. gh is authed as YOU: a false allow files/comments/closes/merges/reviews/publishes publicly under your name (unrecoverable -- the notification already went out); a false deny costs you seconds to surface and unblock. DRAFT the issue/PR/comment text instead, record it PENDING A HUMAN in the hand-off, and stop -- the human files it manually. NOTE: gh api is allowed ONLY on a positive read test (an explicit -X GET/--method GET, or no -f/-F/--field/--raw-field/--input at all); any other form -- including the IMPLICIT POST that field flags trigger with no method flag at all -- is denied. Read-only gh calls (view/list/status/checks/diff, gh api GET) remain allowed when they match the allowlist. If a legitimate read is denied, do NOT retry or work around this -- surface it to a human so they can widen the allowlist. All internal bd filing is unaffected.'
  jq -n --arg reason "$REASON" \
    '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: $reason}}'
fi

exit 0
