#!/usr/bin/env bash
#
# Sourced helper owning the load+validate+pathspec-build idiom for
# scripts/beads-passive-exports.txt. Before this, FOUR consumers
# each hand-rolled the same mapfile+validate+":(exclude)" transform:
# scripts/worktree-gc-classify.sh, scripts/land-replay.sh,
# scripts/land-merge-one.sh, and scripts/discard-beads-passive-export-churn.sh
# -- two of them (gc-classify, land-replay) byte-for-byte identical apart from
# the failure reporter. The repo's own precedent (scripts/gate-lib.sh;
# also scripts/epic-children-closed.sh,
# scripts/recycled-worktree-guard.sh) is "reaches three copies, extract."
#
# TWO OF THOSE FOUR ARE DELIBERATELY NOT CONVERTED, and this is not unfinished
# work -- neither can adopt this function without a behaviour change:
#   - scripts/land-merge-one.sh tolerates a blank line by design (it `continue`s
#     past one); this function rejects one as a hard failure, which would turn a
#     currently-survivable list into an exit-2 in the merge-retry path.
#   - scripts/discard-beads-passive-export-churn.sh is silent by contract (a
#     Stop hook that always exits 0 and swallows every failure); this function
#     writes an unconditional diagnostic to stderr.
# Convert either one only by first giving it the behaviour this function has.
#
# This file is a LIBRARY -- source it, never execute it directly. It performs
# no validation and sets no globals at source time; call
# load_beads_passive_exports() explicitly once sourced.
#
# Usage:
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"    # or $(dirname "$0")
#   # shellcheck source=beads-passive-exports.sh
#   if ! . "$SCRIPT_DIR/beads-passive-exports.sh"; then
#     <caller's own "helper missing" failure behaviour here>
#   fi
#   if ! load_beads_passive_exports "$SCRIPT_DIR/beads-passive-exports.txt"; then
#     <caller's own "list unreadable/empty/malformed" failure behaviour here --
#      load_beads_passive_exports has already printed one diagnostic line to
#      stderr naming the list path and the cause>
#   fi
#   # $BEADS_PASSIVE_EXPORTS and $BEADS_PASSIVE_EXPORTS_EXCLUDE_PATHSPECS are
#   # now set for the rest of the caller's script.
#
# THE SOURCE LINE ITSELF MUST BE GUARDED so a missing/unreadable copy of this
# file fails CLOSED. Why an unguarded source does NOT stop the sourcing script
# -- measured in the source project -- is stated ONCE, in scripts/gate-lib.sh's Usage
# block, and deliberately NOT restated here; it applies verbatim to this
# library, with load_beads_passive_exports in place of gate_could_not_run.
#
# load_beads_passive_exports <list-path>
#   Reads <list-path>, validates it (must be readable, non-empty, and contain
#   no blank line), and on success sets two globals:
#
#     BEADS_PASSIVE_EXPORTS                    -- the raw array of
#                                                  repo-relative paths, as read
#                                                  from <list-path>.
#     BEADS_PASSIVE_EXPORTS_EXCLUDE_PATHSPECS  -- the same paths, each
#                                                  prefixed ":(exclude)" --
#                                                  ready to append to a `git
#                                                  status`/`git diff` pathspec
#                                                  list so passive-export
#                                                  churn never reads as real
#                                                  work.
#
#   Returns 0 on success. Returns 1 and prints exactly one diagnostic line to
#   stderr (naming <list-path> and the cause) on failure -- this function
#   NEVER exits the calling script, so each caller keeps whatever failure
#   semantics its own domain calls for on top of that return code. On the
#   failure return the two globals are UNDEFINED, not merely unset: the
#   blank-line path leaves BEADS_PASSIVE_EXPORTS already populated with the bad
#   list and BEADS_PASSIVE_EXPORTS_EXCLUDE_PATHSPECS stale or unset. A caller
#   that does not abort on a non-zero return must not read either one.
#
#   <list-path> is a REQUIRED argument rather than a default alongside this
#   file, even though every call site passes the same
#   "$SCRIPT_DIR/beads-passive-exports.txt": defaulting it would hollow out
#   tests/test_beads_passive_exports.py's
#   test_every_bash_consumer_names_the_canonical_list, which proves each
#   consumer still reads the canonical list by finding its filename in the
#   consumer's own source.
load_beads_passive_exports() {
  local list_path="$1"
  if [ ! -r "$list_path" ]; then
    echo "load_beads_passive_exports: cannot read $list_path" >&2
    return 1
  fi
  mapfile -t BEADS_PASSIVE_EXPORTS < "$list_path"
  if [ "${#BEADS_PASSIVE_EXPORTS[@]}" -eq 0 ] \
    || printf '%s\n' "${BEADS_PASSIVE_EXPORTS[@]}" | grep -qx ''; then
    echo "load_beads_passive_exports: $list_path is empty or contains a blank line" >&2
    return 1
  fi
  # shellcheck disable=SC2034  # consumed by the sourcing script, invisible here
  BEADS_PASSIVE_EXPORTS_EXCLUDE_PATHSPECS=("${BEADS_PASSIVE_EXPORTS[@]/#/:(exclude)}")
  return 0
}
