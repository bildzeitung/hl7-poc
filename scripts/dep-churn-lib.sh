#!/bin/bash
#
# Sourceable helpers for scripts/update-deps.sh's VERSION DIFF and the filing
# decision for its churn-evaluation stub ticket. Extracted so the
# parsing, the rendering and the noise gate are covered by `nox -s shellcheck`
# and tests/test_dep_churn_lib.py -- the same shape scripts/gate-lib.sh and
# scripts/shell-quote-split.sh already use.
#
# CONTRACT: every function here returns its data on STDOUT and holds no
# cross-call state. Callers assign the result themselves. That is deliberate
# and load-bearing -- see docs/stack.md: update-deps.sh consumes these through
# command substitution, which runs in a subshell, so a function that "returned"
# a value by setting a global would have it silently discarded.
#
# No side effects, no top-level statements: safe to source from anywhere.

# name<TAB>version for every [[package]] in the uv.lock at $1, on stdout.
# Reads only each package table's own `name`/`version` header keys -- the
# nested [package.*] tables (sdist/wheel hashes, dependency lists, markers)
# are what make `git diff -- uv.lock` unreadable, and the whole reason this
# helper exists. Awk, not tomllib: this runs before any Python environment
# is guaranteed to exist.
_dep_pins() {
  awk '
    /^\[\[package\]\]$/ { inpkg = 1; name = ""; next }
    /^\[/ { inpkg = 0 }
    inpkg && /^name = "/ { name = $3; gsub(/"/, "", name) }
    inpkg && /^version = "/ && name != "" {
      v = $3; gsub(/"/, "", v); print name "\t" v; name = ""
    }
  ' "$1" | sort -t $'\t' -k1,1 -u
}

# Emit the raw, unformatted "name<TAB>old<TAB>new" change lines for the two
# lock files $1 and $2 -- moved packages only ($old != $new). An addition
# carries "(none)" as $old, a removal carries "(none)" as $new.
dep_changes_raw() {
  join -a1 -a2 -e '(none)' -o 0,1.2,2.2 -t $'\t' \
    <(_dep_pins "$1") <(_dep_pins "$2") | awk -F'\t' '$2 != $3'
}

# The complete human-readable VERSION DIFF block for the raw change lines in
# $2, labelled against the lock name $1. Sole renderer of this text: it is the
# artifact the whole script exists to produce, and it is embedded verbatim in
# both the failure report and the stub ticket body.
dep_version_diff_text() {
  local label="$1" raw="$2" changes
  changes="$(printf '%s\n' "$raw" | awk -F'\t' '
      NF < 3 { next }
      $2 == "(none)" { printf "  + %-30s %s\n", $1, $3; next }
      $3 == "(none)" { printf "  - %-30s %s (removed)\n", $1, $2; next }
      { printf "    %-30s %s -> %s\n", $1, $2, $3 }
    ')"
  if [ -z "$changes" ]; then
    printf 'VERSION DIFF: no change -- candidate lock is identical to %s' "$label"
  else
    printf 'VERSION DIFF (%s -> candidate):\n%s' "$label" "$changes"
  fi
}

# The churn-stub noise gate: true iff EVERY moved package in the raw change
# lines in $1 kept its major.minor pair, i.e. only the patch component (or a
# suffix below it) moved. False for no changes at all, for an addition or a
# removal ("(none)" on either side), and for any version whose leading
# major.minor does not parse (an epoch like `1!2.3.4`, a bare `2024` date
# version) -- everything unrecognized is conservatively NOT patch-only, so it
# still triggers filing. Regex-over-strings rather than real version ordering
# is a deliberate floor: every shape it cannot read falls into the catch-all
# and files a ticket, so being wrong here can only ever cost noise, never a
# missed evaluation.
dep_all_patch_level_only() {
  local raw="$1"
  [ -n "$raw" ] || return 1   # no changes at all -- nothing to gate
  local name old new old_mm new_mm
  while IFS=$'\t' read -r name old new; do
    [ -n "$name" ] || continue
    # Bash-native, so this stays fork-free on a lock with a hundred moves.
    # A non-match also covers "(none)": additions and removals are never
    # patch-only.
    [[ $old =~ ^([0-9]+\.[0-9]+) ]] || return 1
    old_mm="${BASH_REMATCH[1]}"
    [[ $new =~ ^([0-9]+\.[0-9]+) ]] || return 1
    new_mm="${BASH_REMATCH[1]}"
    [ "$old_mm" = "$new_mm" ] || return 1
  done <<<"$raw"
  return 0
}

# Why filing the churn stub should be SKIPPED, on stdout (rc 0), given the
# --no-file flag $1 (0/1) and the raw change lines $2. Empty output with rc 1
# means "file it". Holds the whole skip policy and its ORDER in one testable
# place; update-deps.sh keeps only the `bd` invocation itself.
dep_stub_skip_reason() {
  local no_file="$1" raw="$2"
  if [ "$no_file" -eq 1 ]; then
    echo "--no-file set -- skipping churn-evaluation stub ticket."
  elif [ -z "$raw" ]; then
    echo "no version changes -- skipping churn-evaluation stub ticket."
  elif dep_all_patch_level_only "$raw"; then
    echo "every moved package is patch-level only -- skipping churn-evaluation stub ticket (noise gate)."
  else
    return 1
  fi
  return 0
}
