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

# ONE `git checkout` PER ENTRY, deliberately -- NOT one call listing them all.
# `git checkout HEAD --` is atomic over its pathspecs: if any single listed
# path is unknown to git in this repo state (e.g. no tracked
# .beads/issues.jsonl here), the whole call fails and restores NOTHING,
# silently, via the `2>/dev/null` below -- see docs/decisions.md. Per entry,
# an unknown path is a harmless no-op and the others still restore.
for export_path in "${paths[@]}"; do
  [ -n "$export_path" ] || continue
  git -C "$root" checkout HEAD -- "$export_path" 2>/dev/null
done
exit 0
