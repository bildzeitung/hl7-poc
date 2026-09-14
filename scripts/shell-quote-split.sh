#!/usr/bin/env bash
#
# SCAN LENGTH CAP. `_split_unquoted` below is a per-character bash
# loop; `local LC_ALL=C` fixed its O(n^2) *indexing* under a
# UTF-8 locale, but the loop itself is still O(n) iterations of bash's own
# interpreter overhead per character (~30us/byte measured -- 25 KB -> ~740ms),
# with no further per-character algorithmic fix available short of a language
# change. Cost is UNBOUNDED in the input: a 200 KB command (a file catted into
# a commit message, a giant heredoc) would cost ~6s on a hot path both
# PreToolUse(Bash) guards run on EVERY Bash call. Both guards cap the length of
# the string they hand to `_split_unquoted` and FAIL CLOSED (deny) past the
# cap, rather than let cost grow without bound.
#
# 16 KiB. The cap is a COST ceiling, not a "nothing real ever gets this big"
# claim -- 16 KiB already costs ~500ms, so raising it to clear every observed
# input would defeat the point. Measured against this repo's own traffic
# (review -- the pre-review figure here sampled only the ~20 OPEN bd
# issues and read as far more headroom than there is): git commit messages sit
# well under it (largest of the last 300: ~4.9 KB, >3x headroom), but across
# the FULL 761-issue bd DB the largest single notes field is ~36 KB, with four
# more between 14 KB and 19 KB. Those accumulated over many `--append-notes`
# calls, so it is not established that any single command ever exceeded the
# cap -- but a big one-shot `bd update ... --notes` plainly can, and past the
# cap it now DENIES. That is the deliberate trade: a denied command is a cheap,
# recoverable cost (append in smaller pieces, or surface it to a human to widen
# the cap), while silently scanning for seconds on every Bash call is not, and
# silently NOT scanning would be a false ALLOW. Whether 16 KiB is still the
# right number now the premise is corrected is filed as its own follow-up.
# Full argument and the (a)/(b)/(c) options weighed: docs/agents-workflow.md.
SHELL_QUOTE_SPLIT_MAX_LEN=16384
#
# Shared quote-aware shell scanning primitives for the PreToolUse(Bash) guards
#. SOURCED, never executed directly -- no `set -euo pipefail` here,
# since that would leak into whichever guard sources this file; each caller
# owns its own shell options. Deliberately NOT marked executable: in scripts/ the `+x` bit means "entry point", and running this
# file would be a silent no-op. The shebang stays -- shellcheck uses it for
# dialect detection, and this file is bash-only. Both properties are pinned by
# tests/test_shell_quote_split_lib.py.
#
# Extracted from scripts/gh-write-guard.sh, where both
# functions were first written and are still sabotage-verified by
# tests/test_gh_write_guard.py. scripts/sha-fabrication-guard.sh
# used to carry its OWN, quoting-UNAWARE segment split (`tr ';&|{}\`' '\n'`)
# -- byte-identical in shape to gh-write-guard.sh's pre-an earlier fix splitter --
# which meant a 40-hex SHA appearing inside a quoted string argument or a
# quoted heredoc body (a commit message, a doc quoting a real SHA) could
# manufacture a fake segment start and be scanned as if it sat at the start of
# a bd/git invocation. an earlier fix fixed quoted ARGUMENTS and an earlier fix fixed
# quoted HEREDOC BODIES, both privately to gh-write-guard.sh -- this ticket
# is the sibling guard catching up, via ONE shared library instead
# of a second, hand-ported copy that would only re-open the same drift: the
# two guards already carried a byte-identical defect once, because they
# carried byte-identical code once. Every future refinement to either
# function now reaches both callers by construction.
#
# Both callers MUST fail CLOSED (deny) if this file cannot be resolved or
# sourced -- see the fail-closed check each guard script runs immediately
# before `source`-ing this file. A missing/unreadable copy of this file must
# never silently disable either guard's quoting fix.
#
# strip_quoted_heredoc_bodies: removes the BODY of any QUOTED
# heredoc (<<'EOF', <<"EOF", <<\EOF) from a command string before it is
# scanned -- a quoted heredoc body is inert text (the shell performs NO
# substitution in it), so a worked example inside one (e.g. a command
# substitution shown as prose, or -- this guard's own concern -- a 40-hex
# string quoted as a real SHA in a commit-message example) must not
# manufacture a fake segment start or a fake token. An UNQUOTED heredoc
# (<<EOF) is the opposite: substitution IS real there, so its body is kept
# (still scanned) untouched. Every deviation from real shell heredoc parsing
# is deliberately biased toward stripping LESS than the shell would, because
# stripping MORE is a false ALLOW (a live write/SHA hidden from the scan) --
# the unrecoverable failure for a default-deny guard. Rules enforcing that
# bias (each pinned by its own test in tests/test_gh_write_guard.py):
#   1. A `<<<` HERESTRING is not a heredoc and consumes no body.
#   2. An UNQUOTED heredoc's body is tracked but never inspected for
#      operators, so a quoted-heredoc lookalike written INSIDE it cannot
#      start a strip.
#   3. A quoted heredoc that is never CLOSED strips nothing -- its held lines
#      are emitted at end of input, rather than swallowing the remainder of
#      the command.
strip_quoted_heredoc_bodies() {
  local mode=none delim="" strip_tabs=0 line check d
  local -a held=()
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$mode" != none ]; then
      check="$line"
      if [ "$strip_tabs" -eq 1 ]; then
        while [[ "$check" == $'\t'* ]]; do
          check="${check:1}"
        done
      fi
      if [ "$mode" = unquoted ]; then
        printf '%s\n' "$line"
      else
        held+=("$line")
      fi
      if [ "$check" = "$delim" ]; then
        [ "$mode" = quoted ] && held=()
        mode=none
      fi
      continue
    fi
    printf '%s\n' "$line"
    [[ "$line" == *'<<'* ]] || continue
    if [[ "$line" =~ (^|[^<])\<\<(-)?[[:space:]]*(\'([A-Za-z_][A-Za-z0-9_]*)\'|\"([A-Za-z_][A-Za-z0-9_]*)\"|\\([A-Za-z_][A-Za-z0-9_]*)) ]]; then
      mode=quoted
      d="${BASH_REMATCH[4]}${BASH_REMATCH[5]}${BASH_REMATCH[6]}"
    elif [[ "$line" =~ (^|[^<])\<\<(-)?[[:space:]]*([A-Za-z_][A-Za-z0-9_-]*) ]]; then
      mode=unquoted
      d="${BASH_REMATCH[3]}"
    else
      continue
    fi
    delim="$d"
    if [ -n "${BASH_REMATCH[2]}" ]; then strip_tabs=1; else strip_tabs=0; fi
  done <<<"$1"
  [ "${#held[@]}" -eq 0 ] || printf '%s\n' "${held[@]}"
}

# _split_unquoted: emits $1 with every UNQUOTED occurrence of
#; & |  { } ` replaced by a newline; occurrences inside '...' or "..."
# (and any backslash-escaped character, in or out of quotes) are left
# untouched -- EXCEPT `$(` and a bare backtick inside "...", which the shell
# really does execute (command substitution is live inside double quotes).
#
# RESIDUAL (fail-OPEN, accepted): an UNBALANCED quote leaves the tail "inside"
# a quote, so nothing after it splits -- the PERMISSIVE direction, not the
# conservative one. Accepted rather than fixed; documented alongside the
# other residuals in docs/agents-workflow.md.
# PERFORMANCE. `local LC_ALL=C` is load-bearing, not a
# micro-optimization. Under a UTF-8 locale bash must walk the string to find
# CHARACTER i, so `${s:i:1}` is O(i) and this loop is O(n^2) -- measured 4.0s on
# a 25 KB command. Byte indexing makes it O(n). It is behaviour-preserving: the
# loop only ever compares against ASCII operator characters, and every UTF-8
# non-ASCII byte is >= 0x80, so no byte of a multibyte character can collide
# with one; every other byte is concatenated through unchanged, so the output
# is byte-identical. `local` keeps the C locale out of the callers' grep and
# [[:space:]] semantics -- pinned by a test.
_split_unquoted() {
  local LC_ALL=C
  local s="$1" out="" c state=none i=0 len
  len=${#s}
  while ((i < len)); do
    c="${s:i:1}"
    # shellcheck disable=SC1003  # a literal backslash, not a mis-escaped quote
    if [[ "$state" != "single" && "$c" == '\' ]]; then
      out+="$c"
      i=$((i + 1))
      ((i < len)) && out+="${s:i:1}"
    elif [[ "$state" == "none" && "$c" == "'" ]]; then
      state=single
      out+="$c"
    elif [[ "$state" == "single" && "$c" == "'" ]]; then
      state=none
      out+="$c"
    elif [[ "$state" == "none" && "$c" == '"' ]]; then
      state=double
      out+="$c"
    elif [[ "$state" == "double" && "$c" == '"' ]]; then
      state=none
      out+="$c"
    elif [[ "$state" == "double" && "$c" == '`' ]]; then
      out+=$'\n'
    elif [[ "$state" == "double" && "$c" == '$' && "${s:i+1:1}" == '(' ]]; then
      out+=$'\n'
      i=$((i + 1))
    elif [[ "$state" == "none" && ';&|(){}`' == *"$c"* ]]; then
      out+=$'\n'
    else
      out+="$c"
    fi
    i=$((i + 1))
  done
  printf '%s' "$out"
}

# deny_if_over_scan_cap <string> <calling-script-name>
#
# The ENFORCEMENT half of the scan-length cap declared at the top of this file
#. Both callers
# ran a byte-identical copy of this block, differing only in which variable
# they measured and which script name the message named -- the exact
# copy-that-must-stay-in-lockstep shape this library exists to eliminate, and
# which the two guards have already drifted into once (see the header). Sharing
# the constant but not its enforcement left the interesting half duplicated.
#
# Emits the PreToolUse deny JSON and returns 1 when <string> is over the cap;
# returns 0 (silent) otherwise, so the caller reads:
#
#     deny_if_over_scan_cap "$CMD" "scripts/foo.sh" || exit 0
#
# Callers MUST measure the string they are about to hand to `_split_unquoted`,
# not the raw command -- the cap exists to bound THAT loop's input, and
# measuring a longer, pre-transform string would deny commands whose actual
# scanned length is under the cap.
deny_if_over_scan_cap() {
  local s="$1" who="$2"
  [ "${#s}" -gt "$SHELL_QUOTE_SPLIT_MAX_LEN" ] || return 0
  jq -n --arg len "${#s}" --arg cap "$SHELL_QUOTE_SPLIT_MAX_LEN" --arg who "$who" \
    '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny",
      permissionDecisionReason: ("this command is " + $len + " characters, past the " + $cap + "-character scan cap " + $who + " enforces before running its quote-aware split -- denying rather than scanning an oversized command for an unbounded amount of time, or worse, silently skipping the scan. If this is a legitimate command, split it into smaller pieces (e.g. several `bd update --append-notes` calls instead of one) or surface this to a human to widen the cap.")}}'
  return 1
}
