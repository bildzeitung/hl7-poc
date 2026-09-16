#!/usr/bin/env bash
#
# Discards working-tree churn to the beads passive exports by checking them
# back out of HEAD. Called from the `Stop` hook in `.claude/settings.json`,
# extracted out of that JSON string so the relpaths live in exactly one place:
# `scripts/beads-passive-exports.txt`.
#
# Usage: discard-beads-passive-export-churn.sh <repo-root>
#
# Best-effort hygiene, NOT a gate -- every failure mode is swallowed and the
# script always exits 0, so it can never fail the Stop hook itself. Its
# fail-loud counterpart is scripts/worktree-gc-classify.sh, which reads the
# same list.

root="${1:-}"
[ -n "$root" ] || exit 0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
list="$SCRIPT_DIR/beads-passive-exports.txt"
[ -r "$list" ] || exit 0

mapfile -t paths < "$list"
[ "${#paths[@]}" -gt 0 ] || exit 0

# ONE `git checkout` PER ENTRY, never one call listing them all: it is atomic
# over its pathspecs, so a single path unknown to git (e.g. an untracked
# .beads/issues.jsonl) would silently restore NOTHING. See docs/decisions.md.
for export_path in "${paths[@]}"; do
  [ -n "$export_path" ] || continue
  git -C "$root" checkout HEAD -- "$export_path" 2>/dev/null
done
exit 0
