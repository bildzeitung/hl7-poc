"""Nox sessions the agent harness invokes as its quality gates.

Invoked through uv, never bare: `uv run --frozen nox -t fix`. `uv run` builds
./.venv from uv.lock if it is missing or stale and puts that environment's
interpreter first, so nox and the tools below need no activation. `--frozen`
makes a gate honour the COMMITTED lock rather than silently rewriting it when
pyproject.toml changed -- that drift is `lock_currency`'s verdict to give, not
a side effect for a gate to paper over.

The harness treats these as opaque commands behind a 0/1/2 exit contract
(0 = passed, 1 = found a real problem, 2 = COULD NOT RUN). A session that lets
nox report a failed command exits 1, which is correct for `fix` and `tests`: a
missing formatter is a setup error the agent should surface, not a gate verdict.
Sessions that shell out to something that can be *absent* (docker, the network,
uv) must distinguish exit 2 themselves, by raising it through `sys.exit(2)`,
which nox passes through unchanged -- `lock_currency` below is the worked
example, and scripts/validate-mermaid.sh the shell-side reference.

Replace the bodies with your project's real tooling; keep the NAMES and the `fix`
tag, since the agent files invoke `nox -t fix`, `nox -s tests`,
`nox -s harness_tests`, `nox -s lock_currency`, `nox -s shellcheck` and
`nox -s build_members` by those exact handles.

Two test sessions, two audiences. `tests` is YOUR suite: it runs on every gate
and ignores tests/harness/. `harness_tests` is the harness's own suite under
tests/harness/, which only changes verdict when scripts/, .claude/,
tests/harness/, noxfile.py or pyproject.toml change -- so the agents reach it
through scripts/harness-tests-gate.sh, which runs it only when the branch
touched one of those paths, and the installer / harness-doctor run it whole.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import nox

nox.options.default_venv_backend = "none"  # uv owns ./.venv

#: Test-worker count. scripts/code-concurrency-cap.sh greps THIS FILE for the
#: literal below to size its per-agent memory budget, so keep the shape
#: `os.environ.get("HARNESS_TEST_WORKERS") or "<n>"` if you change the default.
TEST_WORKERS = os.environ.get("HARNESS_TEST_WORKERS") or "8"


def _venv_tool(name: str) -> str:
    """Resolve a tool from the environment this nox is running in.

    `uv run` hands nox the project venv's interpreter, so the tool lives beside
    ``sys.executable``. Resolving there (rather than a bare name on PATH) keeps
    the gate pinned to uv.lock's versions however nox itself was launched.
    """
    return str(Path(sys.executable).parent / name)


@nox.session(tags=["fix"])
def format_and_lint(session: nox.Session) -> None:
    """Format and lint, fixing in place."""
    session.run(_venv_tool("ruff"), "format", ".", external=True)
    session.run(_venv_tool("ruff"), "check", "--fix", ".", external=True)


#: pytest's "no tests collected" status. The project suite is empty on a fresh
#: install (tests/ holds only the harness's own tests/harness/, which `tests`
#: ignores), and an empty suite is a pass, not a red gate -- the agents run
#: `nox -s tests` before the project has written its first test.
_PYTEST_NO_TESTS_COLLECTED = 5


def _run_partitioned(session: nox.Session, *paths: str, empty_ok: bool) -> None:
    """Two invocations that exhaustively partition a suite on
    ``@pytest.mark.serial`` (registered in tests/harness/conftest.py):
    everything else in the pytest-xdist parallel pool, then the serial tests
    with no workers at all. A ``serial`` test asserts a wall-clock budget that
    sibling workers' scheduler noise would make flaky. No test is skipped and
    none runs twice -- the partition is the only thing the marker changes.
    """
    pytest = _venv_tool("pytest")
    ok = [0, _PYTEST_NO_TESTS_COLLECTED] if empty_ok else [0]
    session.run(
        pytest,
        "-q",
        "-m",
        "not serial",
        "-n",
        TEST_WORKERS,
        *paths,
        *session.posargs,
        external=True,
        success_codes=ok,
    )
    session.run(
        pytest,
        "-q",
        "-m",
        "serial",
        "-n",
        "0",
        *paths,
        *session.posargs,
        external=True,
        success_codes=ok,
    )


@nox.session
def tests(session: nox.Session) -> None:
    """The PROJECT's test suite -- everything pytest collects from the repo
    root except tests/harness/, which is the harness's own suite and has its
    own session below. Runs on every gate.

    Exit 5 (nothing collected) passes: a fresh install has no project tests
    yet, and the gates must be green before the first one is written.
    """
    _run_partitioned(session, "--ignore=tests/harness", empty_ok=True)


#: Workspace member distribution names, as declared by each packages/*/pyproject.toml's
#: [project] name -- not directory names, since `uv build --package` takes the dist name.
WORKSPACE_MEMBERS = ("hl7poc-core", "hl7poc-listener", "hl7poc-worker")


@nox.session
def build_members(session: nox.Session) -> None:
    """Actually BUILD each workspace member's wheel/sdist with `uv build`.

    `nox -s tests` cannot catch a packaging misconfiguration (e.g. a
    [tool.uv.build-backend] module-name naming a file instead of a package
    directory): `uv sync` installs every workspace member editable, so tests
    import straight off packages/*/src regardless of what a real build would
    produce. This session builds each member for real, into a throwaway temp
    dir that is always removed -- never into the repo.
    """
    uv = shutil.which("uv")
    if uv is None:
        session.log("build_members: 'uv' is not on PATH -- install uv and re-run")
        sys.exit(2)
    with tempfile.TemporaryDirectory(prefix="nox-build-members-") as tmp:
        for member in WORKSPACE_MEMBERS:
            session.run(uv, "build", "--package", member, "-o", tmp, external=True)


@nox.session
def harness_tests(session: nox.Session) -> None:
    """The harness's own gate tests under tests/harness/ -- guard-script
    behaviour, the skill-markdown scanners, hook wiring. See
    tests/harness/README.md for what they pin and why.

    Not run on every gate: `scripts/harness-tests-gate.sh` invokes this
    session only when the branch touched a harness path (scripts/, .claude/,
    tests/harness/, noxfile.py, pyproject.toml). The installer and
    `harness-doctor.sh` point at it whole. An empty tests/harness/ is a
    finding here, not a pass -- a project that dropped the suite should also
    drop the gate script's call sites.
    """
    _run_partitioned(session, "tests/harness", empty_ok=False)


@nox.session
def shellcheck(session: nox.Session) -> None:
    """Lint every tracked shell script (and the sourceable libraries).

    The set is discovered from git, never listed here: the harness's guards
    and gates are shell, and a script that fell outside a hand-kept roster
    would be exactly the one nobody linted. `-co --exclude-standard` takes
    untracked-but-not-ignored files too, so a fresh install lints its scripts
    before the first commit instead of finding an empty set. `-x` follows the `. "$(dirname
    "$0")/lib.sh"` sources the scripts use; the `shellcheck source=` comments
    in each file tell it where to look.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "-co", "--exclude-standard", "--", "*.sh"],
        capture_output=True,
        check=True,
    ).stdout.decode()
    files = [f for f in tracked.split("\0") if f]
    if not files:
        session.log("shellcheck: no tracked .sh files")
        return
    session.run(
        _venv_tool("shellcheck"),
        "-x",
        "--source-path=SCRIPTDIR",
        *files,
        external=True,
    )


@nox.session
def lock_currency(session: nox.Session) -> None:
    """Fail if uv.lock is stale against pyproject.toml.

    `uv lock --check` re-resolves without writing: it keeps every committed pin
    that still satisfies pyproject.toml, so only a changed intent moves the
    result; an upstream release alone does not (that is scripts/update-deps.sh's
    job, on purpose). Exit 1 = stale, a real finding. Exit 2 = could not
    answer: no `uv` on PATH, no uv.lock, or a resolve that failed for some
    other reason (network, an unsatisfiable range). Raised with sys.exit so it
    reaches the caller intact.

    /land runs this LAST in its re-gate `&&` chain: an `&&` chain reports its
    last-run command's status, so anything after it would mask an exit 2.
    """
    uv = shutil.which("uv")
    if uv is None:
        session.log("lock_currency: 'uv' is not on PATH -- install uv and re-run")
        sys.exit(2)
    if not Path("uv.lock").exists():
        session.log("lock_currency: uv.lock is missing -- run `uv lock` and commit it")
        sys.exit(2)
    proc = subprocess.run(
        [uv, "lock", "--check"], capture_output=True, text=True, check=False
    )
    if proc.returncode == 0:
        return
    if "needs to be updated" in proc.stderr:
        session.error(
            "uv.lock is stale against pyproject.toml -- regenerate: uv lock "
            "(then commit the result)"
        )
    session.log(
        "lock_currency: `uv lock --check` failed (exit %d):\n%s",
        proc.returncode,
        proc.stderr.strip(),
    )
    sys.exit(2)
