#!/usr/bin/env bash
#
# Derives /code's CODE_MAX_CONCURRENT_AGENTS cap and echoes it to
# stdout. Extracted out of .claude/skills/code/SKILL.md, which
# used to embed this ~30-line derivation verbatim in the prompt -- ungated
# inline shell in a SKILL.md is exactly where this repo already shipped
# silent, undetected-for-months bugs. This
# script is testable (tests/test_code_concurrency_cap.py); the SKILL.md
# prompt is not.
#
# THE FORMULA AND ITS MEASURED COEFFICIENTS ARE FROZEN HERE. This script does
# not retune anything -- full rationale, the measured memory table, and the
# re-measurement trigger for when the suite grows all live in
# docs/agents-workflow.md#concurrency-cap-an earlier fix; that doc's pseudo-code
# block is explanation and may lag, this script is the source of truth on any
# disagreement.
#
#     workers        = HARNESS_TEST_WORKERS if it is a positive integer;
#                       else, if HARNESS_TEST_WORKERS is genuinely UNSET,
#                       noxfile.py's own _xdist_workers default (read from
#                       its SOURCE TEXT below -- never `import noxfile`,
#                       which needs the project venv and fails silently to
#                       empty otherwise);
#                       else (HARNESS_TEST_WORKERS is set but not a clean
#                       positive integer -- "auto", xdist's "logical", a
#                       typo, exported-but-empty, or literal 0; also a
#                       noxfile.py extraction miss) nproc -- the widest the
#                       gate can plausibly get, so the cap errs TIGHT, never
#                       optimistic. Bash arithmetic silently evaluates a
#                       non-numeric string to 0, which would collapse
#                       per_agent_gib to its 2GiB floor and OVER-dispatch;
#                       over-dispatch is what crashed the host.
#     per_agent_gib  = 2 + workers / 8        # 3GiB @ 8 workers, 5GiB @ 24
#     by_mem         = MemAvailable_GiB / per_agent_gib
#                       (MemTotal if MemAvailable is absent; 4 -- a
#                       conservative fallback -- if /proc/meminfo itself is
#                       unreadable, e.g. non-Linux)
#     by_cpu         = nproc / 2, floored at 1
#     cap            = max(1, min(by_mem, by_cpu))
#
# CODE_MAX_CONCURRENT_AGENTS, if set, wins outright: no clamping, no
# derivation below even runs. Set it durably via .claude/settings.local.json's
# "env" block (gitignored, per-machine) -- see CLAUDE.md's New machine setup.
#
# HARNESS_CAP_MEMINFO / HARNESS_CAP_NPROC are TEST SEAMS, not tuning knobs. They let
# tests/test_code_concurrency_cap.py reach branches (meminfo absent, the
# by_cpu clamp dominating, floor-at-1) that are unreachable on whatever
# physical machine happens to run the suite. Do not set them to "tune" this
# script -- use CODE_MAX_CONCURRENT_AGENTS for that.
#
# Resolves its own repo root (like scripts/sweep-digest-id.sh) rather than
# trusting the caller's cwd, since it reads noxfile.py by path.

set -euo pipefail

if [ -n "${CODE_MAX_CONCURRENT_AGENTS:-}" ]; then
  echo "$CODE_MAX_CONCURRENT_AGENTS"
  exit 0
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
NOXFILE="$REPO/noxfile.py"
MEMINFO="${HARNESS_CAP_MEMINFO:-/proc/meminfo}"
NPROC_N="${HARNESS_CAP_NPROC:-$(nproc 2>/dev/null || echo 4)}"

# noxfile.py's _xdist_workers default, read from its SOURCE TEXT -- no
# `import noxfile` (nox is only importable from the project venv; a silent
# ModuleNotFoundError there would yield an empty extraction and undershoot on
# any machine where ./.venv had not been synced yet, a new bug class).
# A parse miss (the literal moved, or its surrounding line was refactored)
# yields empty here and falls through the case below to nproc: fail-tight,
# same as every other unparseable-width case.
noxfile_default="$(grep -oE 'os\.environ\.get\("HARNESS_TEST_WORKERS"\) or "[0-9]+"' "$NOXFILE" 2>/dev/null \
  | grep -oE '"[0-9]+"' | tr -d '"' || true)"

# Distinguish "genuinely unset" (use noxfile.py's own default) from "set but
# not a clean positive integer, including exported-but-empty" (fail tight to
# nproc) -- `${HARNESS_TEST_WORKERS:-...}` would collapse both cases together,
# since bash's `:-` treats an exported-empty var the same as unset.
if [ -z "${HARNESS_TEST_WORKERS+x}" ]; then
  workers_n="$noxfile_default"
else
  workers_n="$HARNESS_TEST_WORKERS"
fi
case "$workers_n" in ''|*[!0-9]*|0) workers_n=$NPROC_N ;; esac

mem_kib=""
if [ -r "$MEMINFO" ]; then
  mem_kib=$(awk '/^MemAvailable:/{print $2; exit}' "$MEMINFO" 2>/dev/null || true)
  [ -z "$mem_kib" ] && mem_kib=$(awk '/^MemTotal:/{print $2; exit}' "$MEMINFO" 2>/dev/null || true)
fi

if [ -n "$mem_kib" ]; then
  per_agent_kib=$(( 2 * 1024 * 1024 + workers_n * 1024 * 1024 / 8 ))
  by_mem=$(( mem_kib / per_agent_kib ))
else
  by_mem=4   # /proc/meminfo unavailable (non-Linux) -- conservative fallback
fi

by_cpu=$(( NPROC_N / 2 ))
[ "$by_cpu" -lt 1 ] && by_cpu=1

cap=$by_mem
[ "$cap" -gt "$by_cpu" ] && cap=$by_cpu
[ "$cap" -lt 1 ] && cap=1

echo "$cap"
