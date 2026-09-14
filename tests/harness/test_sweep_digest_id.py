"""Tests for scripts/sweep-digest-id.sh (proj-x495 technical review).

/sweep runs each fenced bash block as its own Bash tool invocation, so the
`$DIGEST_ID` section 4 resolves cannot reach section 5 (reads the digest body) or
section 6 (rewrites it wholesale). Both re-derive it, and both must refuse unless
exactly one `sweep-digest` issue exists -- section 4's own rule for the `N > 1`
anomaly is "do not guess which is authoritative and do not write anything", and a
bare `jq -r '.[0].id'` does exactly that guessing.

The refusal is the part under test. It was briefly inline in the markdown, in two
byte-for-byte copies already drifting in their diagnostics; nothing gates markdown
against markdown, which is the same way scripts/epic-children-closed.sh's three
inline copies (proj-v4rk) stayed silently broken. Same fake-`bd`-on-PATH pattern as
tests/test_epic_children_closed.py and tests/test_epic_debate_gate.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from conftest import SWEEP_SKILL_BLOCKS, bash_fence_blocks, fake_bin_env

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "sweep-digest-id.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the script shells out to jq"
)


def _run(tmp_path: Path, rows: object) -> subprocess.CompletedProcess[str]:
    """Run the script with a fake `bd` on PATH that serves `rows` as the JSON
    body of `bd list --label sweep-digest --all --limit 0 --json`."""
    payload = tmp_path / "rows.json"
    payload.write_text(json.dumps(rows) if rows is not None else "null")

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    fake_bd = bin_dir / "bd"
    fake_bd.write_text(
        textwrap.dedent(f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            [ "$1" = "list" ] || {{ echo "unsupported: $*" >&2; exit 1; }}
            cat {payload}
        """)
    )
    fake_bd.chmod(0o755)

    return subprocess.run(
        [str(SCRIPT)],
        capture_output=True,
        text=True,
        env=fake_bin_env(bin_dir),
        cwd=REPO_ROOT,
        check=False,
    )


def test_one_digest_prints_the_id(tmp_path: Path) -> None:
    r = _run(tmp_path, [{"id": "proj-dig1", "title": "Human-decision digest"}])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "proj-dig1"


def test_no_digest_refuses_with_exit_1(tmp_path: Path) -> None:
    """N == 0 -- section 4's bootstrap/no-op path owns it. Must NOT print the
    string "null", which is what `jq -r '.[0].id'` yields on an empty array and
    which would then be passed to `bd show`/`bd update` as though it were an id."""
    r = _run(tmp_path, [])
    assert r.returncode == 1
    assert r.stdout == ""
    assert "found 0" in r.stderr
    assert "null" not in r.stdout


def test_null_result_set_is_treated_as_empty(tmp_path: Path) -> None:
    """bd serializes an empty result set as `null`, not `[]` -- the `(. // [])`
    coalesce. Without it `jq length` errors on null and this would exit 2 (machine
    fault) rather than 1 (a legitimate empty state)."""
    r = _run(tmp_path, None)
    assert r.returncode == 1, r.stderr
    assert "found 0" in r.stderr


def test_duplicate_digests_refuse_and_never_pick_one(tmp_path: Path) -> None:
    """N > 1 -- the anomaly section 4 forbids guessing on. The regression this
    pins: `jq -r '.[0].id'` would happily print `harness-dupA`, and section 6 would
    overwrite it."""
    r = _run(
        tmp_path,
        [
            {"id": "harness-dupA", "title": "digest A"},
            {"id": "harness-dupB", "title": "digest B"},
        ],
    )
    assert r.returncode == 1
    assert r.stdout == ""
    assert "found 2" in r.stderr
    # Both ids are reported so a human can consolidate, but neither is selected.
    assert "harness-dupA" in r.stderr
    assert "harness-dupB" in r.stderr


def test_arguments_are_rejected_as_a_machine_fault(tmp_path: Path) -> None:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    r = subprocess.run(
        [str(SCRIPT), "unexpected"],
        capture_output=True,
        text=True,
        env=fake_bin_env(bin_dir),
        cwd=REPO_ROOT,
        check=False,
    )
    assert r.returncode == 2
    assert r.stdout == ""


def test_bd_failure_is_exit_2_not_exit_1(tmp_path: Path) -> None:
    """A machine fault must stay distinguishable from "no digest": exit 2 vs 1.
    Collapsing them would let a broken bd read as a clean empty queue -- the
    "a failed query is indistinguishable from an empty one" hazard section 5's
    hard precondition exists for."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    fake_bd = bin_dir / "bd"
    fake_bd.write_text("#!/usr/bin/env bash\nexit 3\n")
    fake_bd.chmod(0o755)
    r = subprocess.run(
        [str(SCRIPT)],
        capture_output=True,
        text=True,
        env=fake_bin_env(bin_dir),
        cwd=REPO_ROOT,
        check=False,
    )
    assert r.returncode == 2
    assert r.stdout == ""


def test_both_sweep_call_sites_use_the_script_not_an_inline_query() -> None:
    """Fence-scanning pin, same shape as tests/test_land_lock.py's: the whole
    point of extracting this was that markdown copies drift silently. If a
    later edit re-inlines `jq -r '.[0].id'` into sweep/SKILL.md's fenced bash,
    that is the regression -- section 4's own `DIGEST_ROWS`/`N` branch is prose
    with no `.[0].id` in it, so any occurrence is a re-inlined selection.

    Two call sites as of proj-fm7t (was three, briefly, as of proj-o7ai): §5's
    read and §6's write. §7 used to re-derive `$DIGEST_ID` the same sanctioned
    way (it cannot reuse §5's across fenced blocks, proj-sfnb) purely to `bd
    show` the digest body for a delta computation -- but that delta is always
    wrong there (proj-fm7t: §7 runs after §6 has already rewritten the body it
    would read). The fix removes that read entirely; §7 now consumes
    `$SWEEP_TMP/new_ids`, which §5 persists before §6 runs, so it no longer
    needs `$DIGEST_ID` at all. The "both" in this test's name predates both
    count changes; the property under test -- every fenced-bash selection of
    the digest goes through the script -- is unchanged by the count.

    Fence-partitioning itself is `conftest.bash_fence_blocks` (proj-kjei), not a
    private state machine here -- and proj-k5qb makes that a mechanical gate
    (tests/test_no_private_fence_state_machine.py) rather than a claim asserted
    only in this prose. See that helper's docstring, and `fence_scan`'s behind
    it, for the rules. Deliberately NOT re-listed here: `bash_fence_blocks`
    states outright that the rule set is "stated once, there, not duplicated
    here", and a fourth copy is one more place to go stale when a fence rule
    changes."""
    # Comments are not executed, and these blocks deliberately EXPLAIN the
    # `.[0].id` guess they no longer make -- scanning that prose would make
    # this pin fire on its own rationale.
    body = "\n".join(
        line
        for block in SWEEP_SKILL_BLOCKS
        for line in block.splitlines()
        if not line.strip().startswith("#")
    )

    assert body.count("scripts/sweep-digest-id.sh") == 2, (
        "expected exactly the two call sites (§5 read, §6 write) -- §7 no longer "
        "reads the digest at all as of proj-fm7t"
    )
    assert ".[0].id" not in body, (
        "a fenced bash block selects the digest with `.[0].id` again -- that is the "
        "guess scripts/sweep-digest-id.sh exists to refuse (§4's N>1 anomaly)"
    )


def test_sweep_gate_still_sees_a_blockquoted_fence() -> None:
    """This gate's own pin on the shared helper's blockquote handling -- the
    capability the proj-jm4a rewrite buys, which the pin above cannot exercise
    because `.claude/skills/sweep/SKILL.md` carries zero blockquoted fences
    today (measured). Same per-consumer pattern, and for the same reason, as
    `test_skill_bash_state.py`'s and `test_bd_list_limit_gate.py`'s: a change
    to the shared helper must not silently take THIS gate's coverage with it.
    Mechanism (`_BLOCKQUOTE_MARKER`, proj-wroz) is `fence_scan`'s to document,
    not restated here. Sabotage-verified: dropping the blockquote strip from
    `fence_scan` reddens this test and nothing else in this module.

    What it deliberately does NOT pin: that this module still calls the shared
    helper. Re-inlining a private state machine into the test above would leave
    this one green -- the generic "no private fence state machine under tests/"
    gate that would catch that is proj-k5qb."""
    markdown = textwrap.dedent(
        """\
        > ```bash
        > echo hello
        > ```
        """
    )
    assert bash_fence_blocks(markdown) == ["echo hello"]
