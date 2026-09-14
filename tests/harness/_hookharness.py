"""Shared harness for driving the committed `PreToolUse(Bash)` guards in `.claude/settings.json`
*as actually shipped* -- never a reimplementation (proj-zlg8).

`test_bd_deps_guard.py` (proj-ij24), `test_gh_write_guard.py` (proj-o29m), and
`test_sha_fabrication_guard.py` (proj-fpmi) each independently defined a near-identical pair of
helpers -- one to pull a hook's shell one-liner out of the committed settings.json by matching a
substring, one to run it as a subprocess against a synthetic PreToolUse payload and unwrap the
`hookSpecificOutput`. Three copies of the harness contract (payload shape, exit-0 assertion,
`hookSpecificOutput` unwrap) meant every future change to it was an N-file edit, and let one copy
(`test_gh_write_guard.py`) silently drift onto `bash` instead of `/bin/sh` unnoticed -- see below.

CRITICAL (proj-9gm2): the Claude Code harness runs every PreToolUse hook under its `/bin/sh`,
which on Linux is **dash**, not bash -- and dash rejects bash-only constructs (`${var//pat/repl}`
pattern substitution, `$'...'` ANSI-C quoting) with a hard "Bad substitution" error that bricks
the Bash tool for the rest of the session (proj-m6px's first attempt at one of these guards
shipped exactly that, verified green by a test suite that drove the hook through `bash -c` and so
could not see the defect). `run_hook` below therefore always drives the hook through `/bin/sh -c`,
never `bash -c` -- that is the whole point: a test using bash cannot catch this class of bug,
which is exactly how it shipped once already, and exactly how `test_gh_write_guard.py`'s own
`shutil.which("bash")` call went unnoticed until this file existed to make the choice explicit and
shared (proj-zlg8).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

SETTINGS = Path(__file__).resolve().parent.parent.parent / ".claude" / "settings.json"
SH = shutil.which("sh") or "/bin/sh"


def pretooluse_hook(match_substring: str, *, matcher: str = "Bash") -> str:
    """The single `PreToolUse(<matcher>)` hook whose command contains `match_substring`, read from
    the committed `.claude/settings.json`. Fails loudly unless exactly one hook matches -- this
    is a test precondition, not a runtime guard, so an ambiguous or missing match is a bug in the
    test itself, not something to tolerate silently.

    `matcher` defaults to `"Bash"` -- harness's first three PreToolUse guards were all Bash-matched.
    `scripts/main-write-guard.sh` (proj-p8zl) is matched on `"Edit|Write"` instead, since it
    needs no `tool_input` at all and fires on Edit/Write tool calls directly.
    """
    settings = json.loads(SETTINGS.read_text())
    pre_tool_use = settings["hooks"]["PreToolUse"]
    matching = [
        h["command"]
        for entry in pre_tool_use
        if entry.get("matcher") == matcher
        for h in entry["hooks"]
        if match_substring in h.get("command", "")
    ]
    assert len(matching) == 1, (
        f"expected exactly one hook matching {match_substring!r}, got {len(matching)}: {matching}"
    )
    return matching[0]


def run_hook(
    hook: str,
    command: str | None = None,
    *,
    tool_name: str = "Bash",
    tool_input: dict | None = None,
    path: str | None = None,
    cwd: Path | None = None,
    project_dir: str | None = None,
) -> dict | None:
    """Run `hook` (a PreToolUse shell one-liner, as returned by `pretooluse_hook`) against a
    synthetic tool-call payload; return its `hookSpecificOutput`, or `None` if the hook fell
    through silently (no decision).

    Driven through **`/bin/sh -c`** (dash on Linux), never `bash -c` -- see the module docstring.

    `command`, when given, builds a `Bash` tool payload with `tool_input: {"command": command}` --
    the original, still-default shape all three Bash-matched guards use. `tool_name`/`tool_input`,
    when given instead, build an arbitrary payload -- used by `scripts/main-write-guard.sh`'s
    tests (proj-p8zl), whose guard reads no `tool_input` at all and only needs a plausible
    Edit/Write payload shape to drive.

    `path`, when given, overrides `PATH` for the subprocess only -- used to simulate a jq-less
    machine (proj-oii9) without touching the real `PATH` of the process running the test. `sh`
    itself is invoked by its resolved path so a stripped `PATH` cannot make it unresolvable.

    `cwd`, when given, overrides the subprocess's working directory -- used by guards (like
    proj-fpmi's and proj-p8zl's) whose logic depends on `git rev-parse --show-toplevel` resolving
    a real repo.

    `project_dir`, when given, sets `CLAUDE_PROJECT_DIR` -- used by the guards that resolve a
    helper script under it (proj-obox), to point them at a directory where that script is
    missing or non-executable and check they fail closed.
    """
    if tool_input is None:
        tool_input = {"command": command if command is not None else ""}
    payload = json.dumps(
        {"session_id": "t", "tool_name": tool_name, "tool_input": tool_input}
    )
    env: dict[str, str] | None = None
    if path is not None or project_dir is not None:
        env = dict(os.environ) if path is None else {"PATH": path}
        if project_dir is not None:
            env["CLAUDE_PROJECT_DIR"] = project_dir
    proc = subprocess.run(
        [SH, "-c", hook],
        input=payload,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )
    # A PreToolUse hook must always exit 0; a nonzero exit is itself a defect.
    assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr}"
    if not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)["hookSpecificOutput"]
