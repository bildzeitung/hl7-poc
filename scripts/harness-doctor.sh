#!/usr/bin/env bash
#
# harness-doctor -- preflight check for the agent harness.
#
# Verifies the things the pipeline assumes and that fail confusingly when absent:
# prerequisites on PATH, the guard scripts present and executable, the hooks wired,
# the tracker's auto-import invariant, and the worktree directory.
#
# Read-only: it inspects and reports, it never installs or repairs anything. Fixing
# is a human decision, and a doctor that silently mutates state is one more thing to
# distrust when the pipeline misbehaves.
#
# Exit codes: 0 = everything required is present; 1 = at least one REQUIRED check
# failed; 2 = could not run (not in a git repo). Warnings never change the exit code.
#
# Deliberately does NOT source scripts/gate-lib.sh: this is a human-facing
# preflight, not a pipeline gate. Nothing consumes its exit code, so the
# `GATE COULD NOT RUN:` banner and the --no-advisory sentinel would be addressed
# to a caller that does not exist. The 0/1/2 numbering matches the gate contract
# on purpose, so the two never mean opposite things.
set -u

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "harness-doctor: not inside a git repository -- cannot check anything." >&2
  exit 2
}
cd "$ROOT" || exit 2

fails=0
warns=0

ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fails=$((fails + 1)); }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$1"; warns=$((warns + 1)); }

echo "harness-doctor: $ROOT"
echo
echo "prerequisites"
for tool in git jq bd uv; do
  if command -v "$tool" >/dev/null 2>&1; then
    ok "$tool on PATH"
  else
    bad "$tool NOT on PATH (required)"
  fi
done
# Docker gates only the diagram validator, so its absence is a warning, not a failure:
# a project with no diagrams never invokes it. Probe with `docker info`, not
# `command -v docker` -- a Docker Desktop / WSL shim satisfies the PATH check
# while every container run fails, so a PATH probe prints "available" for the
# one docker failure mode actually observed in practice.
if docker info >/dev/null 2>&1; then
  ok "docker responds (diagram validation available)"
else
  warn "docker not usable -- scripts/validate-mermaid.sh will exit 2 (gate could not run)"
fi

echo
echo "guard scripts"
# Every script an agent or skill invokes by path, plus every script the
# .claude/settings.json hooks shell out to. Two different failure shapes, and the
# second is why this list is not just the agent-facing ones: a missing agent-facing
# guard is a BOOTSTRAP GAP that halts the pipeline loudly, but the PreToolUse hook
# wrappers are written as `[ -x "$SCRIPT" ] && bash "$SCRIPT"`, so a missing hook
# script FAILS OPEN -- the guard silently stops guarding and nothing anywhere says
# so. That one is invisible without a check like this.
required_scripts="
scripts/isolation-guard.sh
scripts/recycled-worktree-guard.sh
scripts/assert-main-checkout.sh
scripts/land-lock.sh
scripts/land-merge-one.sh
scripts/land-state-load.sh
scripts/merge-precheck.sh
scripts/validate-sha40.sh
scripts/worktree-gc-classify.sh
scripts/worktree-lock-stale.sh
scripts/blocks-dependents.sh
scripts/epic-children-closed.sh
scripts/epic-completion-check.sh
scripts/epic-debate-gate.sh
scripts/sweep-digest-id.sh
scripts/code-concurrency-cap.sh
scripts/bd-dolt-push.sh
scripts/validate-mermaid.sh
scripts/check-decisions-no-silent-rewrite.sh
scripts/gh-write-guard.sh
scripts/sha-fabrication-guard.sh
scripts/default-branch-write-guard.sh
scripts/bd-deps-blocks-guard.sh
scripts/discard-beads-passive-export-churn.sh
scripts/beads-passive-exports.sh
scripts/drop-from-accepted.sh
scripts/land-heartbeat.sh
scripts/land-merge-batch.sh
scripts/land-replay.sh
scripts/harness-tests-gate.sh
scripts/stacked-graph.sh
scripts/worktree-gc-sweep.sh
"
for s in $required_scripts; do
  if [ ! -f "$s" ]; then
    bad "$s missing"
  elif [ ! -x "$s" ]; then
    bad "$s present but NOT executable (chmod +x it)"
  else
    ok "$s"
  fi
done

echo
echo "agents and skills"
for a in coding code-reviewer land-review; do
  if [ -f ".claude/agents/$a.md" ]; then ok ".claude/agents/$a.md"; else bad ".claude/agents/$a.md missing"; fi
done
for s in code land challenge epic-audit sweep; do
  if [ -f ".claude/skills/$s/SKILL.md" ]; then ok ".claude/skills/$s/SKILL.md"; else bad ".claude/skills/$s/SKILL.md missing"; fi
done

echo
echo "settings"
if [ -f .claude/settings.json ]; then
  if jq -e . .claude/settings.json >/dev/null 2>&1; then
    ok ".claude/settings.json is valid JSON"
    # worktree.baseRef drives where every agent worktree branches from. Anything
    # other than "fresh" means agents start from local HEAD, which can carry
    # uncommitted-adjacent state the pipeline does not expect.
    baseref="$(jq -r '.worktree.baseRef // empty' .claude/settings.json)"
    if [ "$baseref" = "fresh" ]; then
      ok "worktree.baseRef = fresh"
    else
      warn "worktree.baseRef = '${baseref:-<unset>}' (harness assumes \"fresh\")"
    fi
    hooks="$(jq -r '[.hooks // {} | to_entries[] | .key] | join(", ")' .claude/settings.json)"
    if [ -n "$hooks" ]; then ok "hooks configured: $hooks"; else bad "no hooks configured"; fi
  else
    bad ".claude/settings.json is not valid JSON"
  fi
else
  bad ".claude/settings.json missing"
fi

echo
echo "tracker"
if [ -d .beads ]; then
  ok ".beads/ present"
  if [ -f .beads/config.yaml ]; then
    # THE invariant. With auto-import on, a pull or merge replays a stale committed
    # export back into Dolt and silently reverts recent closes. beads accepts either
    # a dotted single-line key ("import.auto: false") or a nested block ("import:"
    # then an indented "auto: false"), so both forms must be read -- but the nested
    # one has to be BLOCK-SCOPED. Two independent greps ("is there an import: block"
    # AND "is there an auto: false anywhere") report ok on a config that sets
    # import.auto TRUE and some other block's auto to false, which is a silent false
    # ALL-CLEAR on the one invariant this check exists to protect. So parse the
    # effective value instead of pattern-matching for reassurance.
    auto="$(awk '
      /^[ \t]*#/ { next }
      /^import\.auto:/ { print $2; exit }
      /^import:[ \t]*$/ { inblk = 1; next }
      inblk && /^[ \t]+auto:/ { print $2; exit }
      inblk && /^[^ \t]/ { inblk = 0 }
    ' .beads/config.yaml)"
    if [ "$auto" = "false" ]; then
      ok "import.auto is false (Dolt stays authoritative)"
    else
      bad ".beads/config.yaml does not set import.auto: false -- a pull can silently revert closes"
    fi
    # The ONE id-shape assumption the harness makes. /land's bare-ref backstop maps a
    # local `land/<id>--<worktree-dir>` name back to its remote `land/<id>` with
    # `${BR%%--*}`, which is only correct while an id contains no double hyphen. A
    # prefix carrying one would silently truncate the comparison, making the
    # "remote still exists -- keep" arm unreachable and force-deleting in-flight refs
    # (and their unpushed commits) the moment a worktree goes away.
    prefix="$(sed -n 's/^[[:space:]]*issue-prefix:[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}[[:space:]]*$/\1/p' .beads/config.yaml | head -1)"
    if [ -z "$prefix" ]; then
      warn "could not read issue-prefix from .beads/config.yaml -- could not check the no-'--' rule"
    elif printf '%s' "$prefix" | grep -q -- '--'; then
      bad "issue-prefix '$prefix' contains '--'; /land's ref backstop would force-delete in-flight refs. Pick a prefix with no double hyphen."
    else
      ok "issue-prefix '$prefix' contains no '--' (required by /land's ref backstop)"
    fi
  else
    bad ".beads/config.yaml missing (run bd init)"
  fi
else
  bad ".beads/ missing -- run 'bd init'"
fi

echo
echo "gate tests"
set -- tests/harness/test_*.py
if [ -e "$1" ]; then
  ok "tests/harness/ present ($# gate test modules) -- run: uv run --frozen nox -s harness_tests"
else
  # Not a hard failure: a project may deliberately drop them. But say so loudly,
  # because without them every mechanism below is prose-enforced only.
  warn "tests/harness/ has no gate tests -- the harness's own invariants are unenforced"
fi

echo
echo "worktrees"
if [ -d .claude/worktrees ]; then
  n="$(git worktree list --porcelain 2>/dev/null | grep -c '^worktree .*/\.claude/worktrees/' || true)"
  ok ".claude/worktrees/ present ($n registered)"
else
  # Not a failure: the directory is created on first use by the harness.
  warn ".claude/worktrees/ does not exist yet (created on the first agent dispatch)"
fi
# The GC sweep reads clean ONLY because build junk is ignored. Un-ignore one and
# every worktree reads dirty and nothing is ever reclaimed.
if [ -f .gitignore ] && grep -qE '(^|/)\.venv/?$|(^|/)\.nox/?$' .gitignore; then
  ok ".gitignore covers .venv/.nox (worktree GC can read a finished tree as clean)"
else
  warn ".gitignore may not ignore .venv/ and .nox/ -- worktree GC will read every worktree as dirty"
fi

echo
if [ "$fails" -gt 0 ]; then
  echo "harness-doctor: $fails required check(s) FAILED, $warns warning(s)."
  exit 1
fi
echo "harness-doctor: all required checks passed ($warns warning(s))."
exit 0
