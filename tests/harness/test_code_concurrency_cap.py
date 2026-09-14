"""Tests for scripts/code-concurrency-cap.sh (proj-54mo).

`.claude/skills/code/SKILL.md` used to embed the ~30-line derivation of
`CODE_MAX_CONCURRENT_AGENTS` (proj-2cf) verbatim in the prompt -- ungated
inline shell in a SKILL.md, exactly where this repo already shipped a silent,
undetected-for-months bug once before (proj-mh9g's `merge-tree` snippet).
This script extracts that derivation so it is testable; the formula and its
measured coefficients are FROZEN (see the script's own header and
docs/agents-workflow.md#concurrency-cap-proj-2cf) -- these tests pin the
documented table, they do not retune anything.

All tests run the ACTUAL `scripts/code-concurrency-cap.sh`, never a
reimplementation, via subprocess -- sabotage-provable per the proj-verb bar.
The one deliberate departure from that bar's usual "no fakes" house style
(see tests/test_merge_precheck.py) is `HARNESS_CAP_MEMINFO` / `HARNESS_CAP_NPROC`:
the branches worth testing (meminfo absent, the by_cpu clamp dominating,
floor-at-1) are unreachable on the physical machine actually running the
suite, and the script's own header comment says outright these are TEST
SEAMS, not tuning knobs.

Every subprocess call explicitly scrubs `CODE_MAX_CONCURRENT_AGENTS`
(and, where the derivation path is under test, `HARNESS_TEST_WORKERS`) from the
inherited environment rather than trusting it to be absent -- this repo's own
`.claude/settings.local.json` pins `CODE_MAX_CONCURRENT_AGENTS` on dev
machines (CLAUDE.md's "New machine setup" step 4), and without the scrub
every test here would just echo that pinned value back, green regardless of
whether the derivation logic underneath is correct.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "code-concurrency-cap.sh"
NOXFILE = REPO_ROOT / "noxfile.py"

# Base env: the real environment, minus anything that would let a pinned
# per-machine override or an ambient HARNESS_TEST_WORKERS leak into a test that
# means to exercise the derivation path.
_SCRUBBED = {
    k: v
    for k, v in os.environ.items()
    if k not in ("CODE_MAX_CONCURRENT_AGENTS", "HARNESS_TEST_WORKERS")
}


def _meminfo(tmp_path: Path, available_kib: int, total_kib: int = 99_999_999) -> Path:
    path = tmp_path / "meminfo"
    path.write_text(
        f"MemTotal:       {total_kib} kB\nMemAvailable:   {available_kib} kB\n"
    )
    return path


def _run(env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    env = {**_SCRUBBED, **env_overrides}
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )


def _cap(
    tmp_path: Path,
    *,
    available_kib: int,
    nproc: int,
    workers: str | None,
) -> str:
    """Run the script with meminfo/nproc test seams and an explicit
    HARNESS_TEST_WORKERS setting (`None` means genuinely unset, not empty)."""
    env: dict[str, str] = {
        "HARNESS_CAP_MEMINFO": str(_meminfo(tmp_path, available_kib)),
        "HARNESS_CAP_NPROC": str(nproc),
    }
    if workers is not None:
        env["HARNESS_TEST_WORKERS"] = workers
    result = _run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# The documented table (ticket's acceptance criteria), hand-computed against
# the formula in docs/agents-workflow.md#concurrency-cap-proj-2cf:
#   per_agent_gib = 2 + workers/8 ; by_mem = MemAvailable_GiB/per_agent_gib
#   by_cpu = nproc/2 (floor 1) ; cap = max(1, min(by_mem, by_cpu))
# ---------------------------------------------------------------------------


def test_15gib_8core_unset_is_4(tmp_path: Path) -> None:
    """15GiB/8-core, HARNESS_TEST_WORKERS unset -> workers=8 (noxfile default),
    per_agent_gib=3, by_cpu=4 clamps by_mem (~4.6) down to 4 -- the original
    reference crash-box number, unchanged (docs table)."""
    assert _cap(tmp_path, available_kib=14_680_064, nproc=8, workers=None) == "4"


def test_31gib_24core_unset_is_9(tmp_path: Path) -> None:
    """31GiB/24-core, HARNESS_TEST_WORKERS unset -> workers=8 (noxfile default,
    not nproc -- proj-bv6y), per_agent_gib=3, by_mem=29/3~=9, by_cpu=12 ->
    cap 9 (docs table: "cap 9, not 5")."""
    assert _cap(tmp_path, available_kib=30_408_704, nproc=24, workers=None) == "9"


def test_31gib_24core_auto_is_5(tmp_path: Path) -> None:
    """Same box, HARNESS_TEST_WORKERS=auto -> non-numeric, so workers=nproc=24,
    per_agent_gib=5, by_mem=29/5~=5, by_cpu=12 -> cap 5 (docs table: "the old
    nproc-scaled budget", the historical number from before proj-bv6y)."""
    assert _cap(tmp_path, available_kib=30_408_704, nproc=24, workers="auto") == "5"


@pytest.mark.parametrize("bad_width", ["auto", "logical", "not-a-number", "0", ""])
def test_non_numeric_widths_are_nproc_scaled_never_the_floor(
    tmp_path: Path, bad_width: str
) -> None:
    """The fail-tight case this ticket exists for: every shape of an
    unparseable width -- 'auto', xdist's 'logical', a plain typo, literal
    '0', and an exported-but-empty var -- must resolve identically to the
    nproc-scaled cap (5, matching the `auto` case above), and must NEVER
    silently collapse per_agent_gib to its 2GiB floor. That floor-collapse
    bug would raise the cap to 12 (by_mem=29/2~=14, clamped by by_cpu=12)
    exactly when the gate is at its widest and heaviest -- over-dispatch is
    what crashed the host twice (proj-2cf). So this asserts both: equal to
    the known-correct nproc-scaled value, and not equal to the floor-bug
    value.

    Parametrized rather than looped so a regression names the exact width
    that broke, and so xdist can spread the five subprocess spawns."""
    cap = _cap(tmp_path, available_kib=30_408_704, nproc=24, workers=bad_width)
    assert cap == "5", (
        f"HARNESS_TEST_WORKERS={bad_width!r} -> {cap}, want nproc-scaled 5"
    )
    assert cap != "12", (
        f"HARNESS_TEST_WORKERS={bad_width!r} collapsed to the 2GiB floor bug"
    )


def test_memtotal_used_when_memavailable_absent(tmp_path: Path) -> None:
    """A /proc/meminfo carrying MemTotal but no MemAvailable (older kernels,
    some containers) must fall back to MemTotal rather than to the
    meminfo-unreadable by_mem=4 branch. 30GiB MemTotal / 24-core, workers=8
    -> by_mem=10, by_cpu=12 -> cap 10; the 4-fallback would be visibly
    different, so this pins that the fallback chain is actually wired."""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       31457280 kB\nSwapTotal:  0 kB\n")
    result = _run({"HARNESS_CAP_MEMINFO": str(meminfo), "HARNESS_CAP_NPROC": "24"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "10"


def test_tiny_box_floors_at_1(tmp_path: Path) -> None:
    """nproc=2 (by_cpu=1) and a tiny MemAvailable (100MiB) drives by_mem to 0
    -- cap must floor at 1, never go to (or through) 0."""
    assert _cap(tmp_path, available_kib=102_400, nproc=2, workers=None) == "1"


def test_override_wins_outright_unclamped(tmp_path: Path) -> None:
    """CODE_MAX_CONCURRENT_AGENTS, when set, is echoed straight back --
    no clamping against by_cpu/by_mem, even when it plainly exceeds what a
    2-core/tiny-memory box could otherwise support."""
    result = _run(
        {
            "CODE_MAX_CONCURRENT_AGENTS": "17",
            "HARNESS_CAP_MEMINFO": str(_meminfo(tmp_path, 102_400)),
            "HARNESS_CAP_NPROC": "2",
        }
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "17"


def test_meminfo_unreadable_is_4(tmp_path: Path) -> None:
    """/proc/meminfo itself unreadable (non-Linux) falls back to the
    documented conservative by_mem=4; with nproc=8 (by_cpu=4) that's also the
    binding clamp, so cap=4."""
    result = _run(
        {
            "HARNESS_CAP_MEMINFO": str(tmp_path / "does-not-exist"),
            "HARNESS_CAP_NPROC": "8",
        }
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "4"


def test_workers_default_extraction_matches_noxfile(tmp_path: Path) -> None:
    """The `workers` default has ONE source: noxfile.py's own
    `_xdist_workers()` return literal, read from its SOURCE TEXT (never
    imported -- `import noxfile` needs the project venv, which the script
    cannot assume). This test independently regexes that same literal out of
    the LIVE noxfile.py (ground truth, not a hardcoded "8") and confirms the
    script's unset-HARNESS_TEST_WORKERS path resolves to an *identical* cap as
    explicitly passing that literal as HARNESS_TEST_WORKERS. A refactor that
    moves or reformats the literal makes the script's own regex miss, fall
    through to nproc, and (since nproc=24 != workers=8 here) diverge from
    this test's independently-derived expectation -- loudly, not silently."""
    match = re.search(
        r'os\.environ\.get\("HARNESS_TEST_WORKERS"\) or "([0-9]+)"',
        NOXFILE.read_text(),
    )
    assert match, (
        "noxfile.py's _xdist_workers() default literal was not found by this "
        "test's own regex -- either the literal moved/changed shape (update "
        "this test's pattern) or scripts/code-concurrency-cap.sh's matching "
        "regex needs the same update"
    )
    nox_default = match.group(1)

    meminfo = _meminfo(tmp_path, 30_408_704)
    common = {"HARNESS_CAP_MEMINFO": str(meminfo), "HARNESS_CAP_NPROC": "24"}

    unset_result = _run(common)
    explicit_result = _run({**common, "HARNESS_TEST_WORKERS": nox_default})

    assert unset_result.returncode == 0, unset_result.stdout + unset_result.stderr
    assert explicit_result.returncode == 0, (
        explicit_result.stdout + explicit_result.stderr
    )
    assert unset_result.stdout == explicit_result.stdout
    # Pins today's documented value (docs/agents-workflow.md's table): with
    # noxfile.py's current default of 8 workers, this box's cap is 9.
    assert unset_result.stdout.strip() == "9"
