#!/bin/bash
#
# Validate every Mermaid diagram in docs/ with mermaid-cli in Docker.
#
# mmdc parses each ```mermaid block against the same mermaid.js parser GitHub
# renders with, so syntax errors are caught before they ship. Runs in Docker so
# no Node/Chromium toolchain is needed on the host. Pull the image first with
# scripts/update-images.sh (or let `docker run` fetch it on demand).
#
# Usage: scripts/validate-mermaid.sh

# AUDIT (an earlier fix, 2026-07-29, decided by measurement -- supersedes an earlier fix
# Finding D and an earlier fix, both of which kept `-e` and guarded sites one
# at a time). Shebang form: `#!/bin/bash`, matching gate-lib.sh's own form --
# NOT `#!/usr/bin/env bash`, which the other gate-lib.sh consumers use and
# which the consistency argument for dropping `-e` cites, but which turned out
# NOT to be free here: `env bash` does its own PATH lookup for the `bash` binary,
# and tests/test_validate_mermaid_gate.py's pre-flight fixtures (`fake_bin`)
# deliberately run this script with a PATH containing only a `dirname` shim --
# they assume the interpreter itself is reached by the shebang's own absolute
# path, never searched for. Measured while building this ticket, and again
# independently in review: switching to `env bash` breaks EVERY test that runs
# this gate on a hermetic PATH (`_run_gate(fake_bin)` with no `inherit_path`)
# with exit 127 ("env: 'bash': No such file or directory") -- not a content or
# machine-fault exit at all. Stated as a predicate, not a count, so it stays
# true as fixtures are added. `#!/bin/bash` does no PATH search for the
# interpreter and keeps them green unmodified, so it is the deliberate pick
# here: it is the form that does not require touching a pinned test fixture.
#
# WHY DROP -e: `-e` was kept here on the theory that this script's
# machine-fault-vs-content split lives entirely in `if`/`else` arms, which bash
# exempts from `-e` by its own rules, so `-e` couldn't short-circuit the split.
# True, but incomplete: `-e` still governed every command NOT inside an `if`,
# and most commands fail with status 1 -- which in THIS script means "invalid
# mermaid". Two review passes each found sites the
# previous pass's audit missed (mktemp, then REPO=/printf/chmod x2, then the
# EXIT trap's `rm -rf` and `basename`), instance-fixing the same defect class
# open one route at a time. The pattern itself is the argument for removing
# the class instead: any command added below without a guard is a new,
# silent route onto exit 1, and no audit can prove there are no more of them
# for a class it's still possible to add to by omission.
# Measured (bash 5.2, shebang honoured): `mktemp -d` failing under -e exited
# 1, and so did the REPO= assignment, the printf into $CFG/puppeteer.json, and
# both chmod calls -- the exact an earlier fix inversion, a machine fault blamed on
# a doc. All five keep their own `gate_could_not_run` guards below.
#
# THE ONE FAILURE NO PER-SITE GUARD COULD CLOSE (bash 5.2, shebang honoured),
# and why the trap keeps `|| :` anyway: a clean run whose EXIT trap's
# `rm -rf "$CFG"` then fails (its parent went read-only, say) exits with THAT
# command's status under `-e` -- 1, this script's CONTENT verdict -- rewriting
# the status of EVERY exit already decided below it, a guard's correct 2
# included. No `||` guard on a body command can reach this: the trap fires
# after the body is done. This is the single strongest argument FOR dropping `-e` (no per-site
# guard closes it), and it is why the trap itself carries `|| :` below --
# without `-e` that failure can no longer rewrite anything, but the `|| :`
# stays so a broken cleanup is never mistaken for a verdict either way.
#
# THE ONE PLACE THE DECISION EXPECTED DROPPING -e NOT TO BE FREE -- and the
# re-measurement that overturned that expectation (2026-07-29):
# `REPO="$(cd ... && pwd)"` below. With `-e`: a failing `cd` aborts -> exit 1 ->
# fabricated "invalid mermaid" (the known-bad route). WITHOUT `-e` and
# WITHOUT a guard, the decision reasoned: the assignment would continue with
# REPO="", the `for f in "$REPO"/docs/*.md` glob would match nothing, `found`
# would stay 0, and the script would print "no mermaid diagrams found" and
# exit 0 -- a gate reporting GREEN because it validated NOTHING, worse than a
# false red. RE-VERIFIED during this build by temporary sabotage (guard
# removed, REPO forced empty, not left in the code): the false green does NOT
# reproduce against current main. `an earlier fix` (landed 2026-08-03, after the
# decision above was written) added its own escalation inside the per-doc
# loop -- grep scanning the unmatched glob `"$REPO"/docs/*.md` (nullglob is
# deliberately unset, so it passes through as the literal string `docs/*.md`)
# fails with "No such file or directory" (grep exit 2, not 1), and that is now
# caught as a machine fault by the very guard an earlier fix added for a different
# reason -- observed: "GATE COULD NOT RUN: grep failed scanning docs/*.md ...
# (exit 2)", exit 2, not the fabricated "nothing to validate" exit 0 the
# original reasoning predicted. So the false-green route this REPO= guard was
# written to close is independently closed today by an earlier fix's grep guard,
# discovered later and for an unrelated reason. The REPO= guard stays anyway
# -- it is not redundant, just no longer the ONLY barrier: it fails faster
# (before reaching docker or the loop) and with a message that names the
# actual cause ("could not resolve the repo root") instead of grep's
# generic, one-hop-removed complaint about a literal glob string.
# Separately re-verified with the guard in place and a real `cd` failure
# (broken `dirname` on PATH): exit 2 with the GATE COULD NOT RUN banner and
# this gate's advisory trailer, unchanged by the shebang -- the existing
# test_repo_root_resolution_failure_is_gate_could_not_run in
# tests/test_validate_mermaid_gate.py pins that arm permanently. Since
# an earlier fix moved the gate-lib.sh source above this assignment, the REPO=
# guard is an ordinary `gate_could_not_run` call like the others below it --
# NOT a pre-library hardcoded fallback. The only hardcoded pre-library
# fallback left in this file is the guard on the gate-lib.sh source itself
# (see the comment there), which genuinely cannot call the helper it is
# checking for.
#
# REJECTED ALTERNATIVE, stated so a future auditor does not re-derive it:
# renumber the CONTENT verdict from exit 1 to exit 3, freeing exit 1 so any
# future unguarded command is safe by construction in both directions.
# Rejected because noxfile.py's `GATE_MACHINE_FAULT = 2` and its
# `lock_currency` session, plus .claude/agents/coding.md, code-reviewer.md and
# .claude/skills/land/SKILL.md, all key on the repo-wide 1=content / 2=machine
# contract -- desyncing this one script from it is worse than the class of bug
# it would close.
#
# THE FULL SITE INVENTORY, every command `-e` used to be able to abort on:
#
#   GUARDED (guard is independent of `-e`; kept regardless of the shebang):
#     - `mktemp -d`            -> gate_could_not_run
#     - `REPO=` (`cd`/`pwd`)   -> gate_could_not_run -- kept for a faster,
#                                 clearer exit, though an earlier fix's grep guard
#                                 independently closes the same false-green
#                                 route today
#     - `printf` into puppeteer.json -> gate_could_not_run (disk full / $CFG
#                                 gone read-only after mktemp created it)
#     - `chmod 755 "$CFG"`     -> gate_could_not_run
#     - `chmod 644 "$CFG/puppeteer.json"` -> gate_could_not_run
#     - EXIT trap `rm -rf "$CFG"` -> `|| :` (cleanup failure is not a verdict;
#                                 also what makes dropping `-e` safe here, since
#                                 nothing upstream can be rewritten by it now)
#
#   DELETED, NOT GUARDED (parameter expansion cannot fail, so the site stops
#   existing rather than needing a guard):
#     - `basename "$f"` -> `${f##*/}` in the per-doc loop
#
#   SHOWN SAFE TO CONTINUE PAST -- deliberately unguarded:
#     - `set -uo pipefail` itself, and the literal/parameter-expansion
#       assignments `IMAGE="minlag/mermaid-cli:10.9.1"` / `fail=0` /
#       `found=0` / `rel="docs/${f##*/}"` / `found=1` / `fail=1` / `rc=$?` --
#       none of these can fail: a literal assignment to a plain variable
#       can't, and parameter expansion (`${f##*/}`) can't either.
#     - the `trap '...' EXIT` REGISTRATION statement -- registering a trap on
#       a literal, always-valid signal spec cannot fail; only the trap BODY
#       (guarded above with `|| :`) can.
#     - the TESTED CONDITION of every `if` in this file -- always exempt from
#       `-e` by bash's own rules (that's what makes an `if` an `if`), unlike
#       the *body* of that same `if`, which -e does still govern absent a
#       trailing `exit` (see the echo entries below). Enumerated rather than
#       gestured at, since most of them are COMMANDS, not `[ ... ]` tests, and
#       a reader scanning for unguarded external commands will otherwise trip
#       over them: `. gate-lib.sh` (under `if !`), `docker info` (under
#       `if !`), `command -v docker`, `grep -q`, `docker run`, and the two
#       `[ ... ]` tests on `$found` / `$fail`.
#     - the `gate_could_not_run` and `escalate_unless_content` call sites --
#       neither can hand `-e` a nonzero status. Why is gate-lib.sh's contract
#       to state, not this file's: see those functions' own headers there
#       (that `escalate_unless_content` returns 0 on the content path is a
#       contract guarantee, not an accident of this caller).
#
#   ALL SEVEN `echo` STATEMENTS in this file (enumerated mechanically --
#   `grep -n '^[[:space:]]*echo' scripts/validate-mermaid.sh` -- rather than
#   re-derived from a prior version of this header, which is what let an
#   earlier rewrite miss five of them):
#     - the two-line "GATE COULD NOT RUN ... gate-lib.sh is missing or
#       unreadable" / "next to $0 ..." pair, immediately above the hardcoded
#       pre-library `exit 2` on the gate-lib.sh source guard -- a failing
#       echo here does not change that exit: the `exit 2` is a separate,
#       unconditional statement below both echoes, not their status.
#     - "OK    $rel" / "FAIL  $rel" in the per-doc loop -- a failing echo
#       means stdout is gone (closed or full); the exit status the script
#       reports still carries the real verdict either way.
#     - "no mermaid diagrams found in docs/ -- nothing to validate", in the
#       `if [ "$found" -eq 0 ]` body, immediately followed by an explicit
#       `exit 0` -- safe today because that `exit 0` is a separate statement,
#       not the echo's own status. Noted explicitly rather than folded into
#       "inside an `if`," since the exemption that actually matters here is
#       the trailing `exit`, not the surrounding `if`.
#     - "mermaid validation failed" >&2, in the `if [ "$fail" -ne 0 ]` body,
#       immediately followed by an explicit `exit 1` -- same shape and same
#       reasoning as the "no mermaid diagrams found" case above: safe because
#       of the trailing `exit`, not because of the `if`.
#     - "all mermaid diagrams valid" -- DIFFERENT from every echo above: it is
#       the script's LAST command, at the top level, with no trailing `exit`
#       of its own. Before this ticket, the script's own exit status WAS this
#       echo's status -- so a failing echo on an otherwise fully-green run
#       (stdout gone) would report exit 1, this gate's CONTENT verdict
#       "invalid mermaid," on a run where every diagram validated. That is
#       the an earlier fix inversion, reachable on the fully-green path. Decision:
#       closed outright with a trailing `exit 0` immediately below it -- it
#       costs nothing and removes the last vanishing case in this file,
#       rather than leaving it as an accepted one.
#
# What was missing, unlike every other gate-script consumer, was any
# top-level `-u`/`pipefail`; added below for parity. Inert today:
# this script contains no pipeline at all, and references no variable meant
# to expand unset. `-u` firing also exits 1 -- the same route as `-e` used to
# open -- worth remembering if a new variable reference is added here.
set -uo pipefail

# Pinned to the SAME tag scripts/update-images.sh pulls (previously a
# floating `:latest`, which let the pulled image and the validated parser
# version drift apart across machines). Bump the two together.
IMAGE="minlag/mermaid-cli:10.9.1"

# The ONE owner of the gate-could-not-run contract: the banner callers key on,
# the caller's cause lines, the standing instruction to a reader, and exit 2.
# Callers supply only the cause — the part that genuinely differs. Sourced
# from scripts/gate-lib.sh so this contract cannot drift out of
# sync with the other gate scripts — they, plus .claude/agents/coding.md,
# code-reviewer.md and .claude/skills/land/SKILL.md, key on exactly this
# stderr shape.
# The source itself must fail CLOSED -- see gate-lib.sh's Usage
# section for the measurement and why the guard can't use the library it loads.
# This gate's own advisory trailer, bound at source time (an earlier fix; see
# gate-lib.sh's GATE_ADVISORY contract for the mechanism and why it is not a
# separate assignment). tests/test_validate_mermaid_gate.py's
# _assert_gate_could_not_run pins the advisory TEXT below on an exit-2 path,
# which no static sweep can see -- route any new exit-2 test through it.
#
# Sourced ABOVE REPO= (an earlier fix; moved from below the docker probe, where
# an earlier fix had left it deliberately -- see that ticket's comment, now
# obsolete, and an earlier fix's own description for the ordering argument): the
# only thing between here and REPO= is IMAGE=, which does not depend on
# $REPO and cannot fail, so nothing forces gate_could_not_run to stay
# undefined at the point REPO= needs it. That leaves exactly one hardcoded
# pre-library fallback in this file -- the guard for the source itself,
# immediately below, which genuinely cannot call the helper it is checking
# for.
# shellcheck source=gate-lib.sh
if ! . "$(dirname "$0")/gate-lib.sh" \
     "This is a machine fault a human must fix, not a mermaid syntax error —" \
     "do not hand-verify diagrams or hand off in place of this gate."; then
  echo "GATE COULD NOT RUN: scripts/gate-lib.sh is missing or unreadable" >&2
  echo "next to $0 -- this is a machine/checkout fault, not a branch verdict." >&2
  exit 2
fi

# Guarded rather than left to -e: a failing `cd`/`pwd` here --
# this script's own checkout moved or was deleted out from under it after it
# started running -- would otherwise abort with -e's own exit 1, which in
# this script means "invalid mermaid", blaming a fabricated content verdict
# on a checkout/machine fault. gate-lib.sh is now sourced above, so this
# routes through gate_could_not_run like every other guard in this file,
# and gets the full GATE_ADVISORY trailer (an earlier fix; previously a
# hardcoded exit-2 block that emitted the banner and cause but not the
# trailer -- a half-contract emission tests/test_gate_lib.py's ordering
# sweep could not see, since it was not a call site at all).
REPO="$(cd "$(dirname "$0")/.." && pwd)" || gate_could_not_run \
  "could not resolve the repo root from \"\$0\" ($0)" \
  "-- its parent directory is missing or inaccessible."

# `command -v docker` is a PROXY — it only proves some binary named `docker`
# is on PATH. When Docker Desktop's engine is stopped (e.g. Resource Saver
# mode, or WSL integration switched off for this distro), the Windows shim
# at .../Docker/resources/bin/docker still satisfies that check, then fails
# every container run with "The command docker could not be found in this
# WSL 2 distro" — indistinguishable, to a caller, from real per-doc syntax
# failures. Guard on the INVARIANT instead: can docker actually
# reach a running engine? `docker info` is the cheapest such probe. Exit 2
# (distinct from exit 1's "invalid mermaid") and print NO per-doc FAIL
# lines — an unusable engine is a machine fault, not content, and only a
# human can fix it.
#
# Both failure shapes exit 2, but they get DIFFERENT messages: naming one
# confident cause for every possible failure is the same bug this fix exists
# to kill, just relocated. `docker info` also fails when docker isn't
# installed at all, or when the socket denies permission — telling that user
# to "check Resource Saver mode" is exactly the plausible-but-false
# machine-level story that started this ticket.
#
# NOTE ON STRUCTURE: this probe is NOT what enforces the invariant — the
# per-doc exit-code check in the loop below is, and it subsumes this one (an
# engine that is down before the loop also makes `docker run` fail, which that
# check catches). This probe is a MESSAGE-QUALITY + FAIL-FAST layer on top: it
# can tell "no docker at all" apart from "binary present, engine unreachable",
# which the loop check — seeing only an integer — structurally cannot, and that
# distinction is the whole an earlier fix lesson. It also aborts before the setup
# work below. Add new gate-could-not-run conditions to the LOOP check; add to
# this probe only to say something the exit code alone cannot.

if ! docker info >/dev/null 2>&1; then
  if command -v docker >/dev/null 2>&1; then
    gate_could_not_run \
      "docker engine unreachable — a docker binary is on" \
      "PATH but cannot reach a running engine. Usual causes: Docker Desktop is" \
      "stopped (Resource Saver mode), WSL integration is off for this distro," \
      "or the docker socket denies permission. Diagnose with: docker info"
  else
    gate_could_not_run \
      "no docker on PATH — mermaid validation runs the" \
      "parser in a container and needs Docker installed. See CLAUDE.md and" \
      "scripts/update-images.sh."
  fi
fi

# The image ships chromium at /usr/bin/chromium-browser, but puppeteer hunts for
# its own download; point it at the bundled binary. --no-sandbox is required
# because chromium cannot sandbox inside the container.
# Guarded rather than left to the former -e: under -e a failing `mktemp -d` aborted with
# mktemp's own exit 1, which is this script's CONTENT verdict ("invalid
# mermaid") -- a machine fault blamed on a doc. It routes to exit 2 instead,
# the same way scripts/merge-precheck.sh guards its own mktemp. (mktemp prints
# its own error; suppress it so the diagnostic below is the authoritative one.)
CFG="$(mktemp -d 2>/dev/null)" || gate_could_not_run \
  "could not create a temporary directory (mktemp failed)" \
  "Usual causes: TMPDIR points at a nonexistent, full, or read-only" \
  "filesystem. Diagnose with: mktemp -d"
# `|| :` is load-bearing, not defensive noise.
# MEASURED (bash 5.2): under -e, a command that fails inside an EXIT trap
# makes the shell exit with THAT command's status, clobbering the status the
# script had already chosen. So an unguarded `rm -rf` here silently rewrites
# every exit below -- including a guard's correct 2 and a clean run's 0 --
# into rm's own 1, this script's CONTENT verdict. Reproduced end-to-end
# against the real script: $CFG read-only fires the printf guard's exit 2,
# then the trap's failing rm turns it into "invalid mermaid" -- the an earlier fix
# inversion re-entering through the one command no guard below can reach.
# Cleanup failure is not a verdict about anything, so swallow the status;
# rm's own stderr still names it.
trap 'rm -rf "$CFG" || :' EXIT
# Guarded rather than left to the former -e: a write failure here
# (disk full, or $CFG's filesystem gone read-only after mktemp created it)
# would otherwise have aborted with -e's own exit 1 -- this script's CONTENT
# verdict -- and today would simply continue past a broken config.
printf '{"executablePath":"/usr/bin/chromium-browser","args":["--no-sandbox","--disable-setuid-sandbox"]}' \
  > "$CFG/puppeteer.json" || gate_could_not_run \
  "could not write $CFG/puppeteer.json" \
  "Usual causes: the filesystem backing \$TMPDIR is full or went" \
  "read-only after mktemp created \$CFG. Diagnose with: df -h $CFG"
# tempfile dirs are 0700; the container's non-root user must read the mount.
# Both chmods guarded for the same reason as the printf above.
chmod 755 "$CFG" || gate_could_not_run \
  "could not chmod $CFG to 755" \
  "the container's non-root user needs read+traverse access to the" \
  "mounted config dir. Diagnose with: ls -ld $CFG"
chmod 644 "$CFG/puppeteer.json" || gate_could_not_run \
  "could not chmod $CFG/puppeteer.json to 644" \
  "the container's non-root user needs read access to the mounted" \
  "config file. Diagnose with: ls -l $CFG/puppeteer.json"

# THIS is where the invariant "a broken tool is never mistakable for broken
# content" is enforced. The partition is drawn around mmdc's ONE content
# verdict, not around docker's failure codes: exit 1 means "mmdc ran, parsed,
# and rejected the diagram" — the only exit allowed to print FAIL — and EVERY
# other nonzero exit means the gate could not run.
#
# Inverting it that way is the point. It fails SAFE (an exit code nobody
# anticipated escalates to a human rather than silently blaming an innocent
# doc), and it needs no list of docker's codes kept in sync as docker evolves.
# An allowlist of docker's 125-127 was this fix's first pass and was WRONG: the
# engine dying mid-run — the very flake that opened the ticket — kills a RUNNING
# container, which exits 128+SIGKILL = 137, not a pre-start code, so the
# allowlist printed FAIL for exactly the case it was written to catch.
#
# Measured 2026-07-14 against a live engine, not assumed:
#
#   CONTENT (exit 1 — the only verdict that may print FAIL):
#     a genuine mermaid syntax error, through mmdc              -> 1
#     mmdc handed a missing input file                          -> 1
#   TOOL (everything else — gate could not run, exit 2):
#     docker run <image absent, pull denied>                    -> 125
#     docker run <bad docker CLI flag>                          -> 125
#     docker run --entrypoint <not executable>                  -> 126
#     docker run --entrypoint <nonexistent cmd>                 -> 127
#     engine kills the container (docker kill / Resource Saver) -> 137
#     container OOM-killed (chromium is memory-hungry)          -> 137
#
# Docker does not *reserve* 125-127 either — a contained command exiting 125
# itself has that code passed through verbatim (also measured). Under this
# partition that ambiguity is harmless: it lands on the tool side, where a human
# looks, instead of being pinned on a doc.
fail=0
found=0
for f in "$REPO"/docs/*.md; do
  # Parameter expansion, not `basename`: identical result for a
  # glob match, and it removed the last unguarded external command under the
  # then-current -e -- a missing basename would abort the loop with its own status
  # rather than this script's contract. One fewer fork per doc, besides.
  # Computed before the grep check below so a machine-fault message can name
  # the file it happened on.
  rel="docs/${f##*/}"
  # grep exits 1 for "no match" -- a CONTENT answer: this doc genuinely has
  # no mermaid block, so skip it exactly as before. Anything else (2: file
  # unreadable, an I/O error, or -- if $REPO/docs vanished mid-run -- the
  # unmatched glob passed through literally) is a MACHINE fault, not a
  # content answer, and must not be treated the same way: a bare
  # `|| continue` here cannot tell the two apart, so an unreadable doc was
  # silently skipped as though it simply had no diagram -- and if every doc
  # errored this way, `found` stayed 0 and the gate printed a clean "nothing
  # to validate" PASS on a completely broken run. This is the mirror of the
  # `docker run` partition below: only ONE exit code is a real content
  # answer, everything else escalates; the grep sits inside an `if` so the
  # `else` arm can capture `$?` for escalate_unless_content (see below).
  #
  # Second, unintended consequence, verified in an earlier fix's review by
  # sabotage: this escalation also closes the false-green route the REPO=
  # guard above was written for. With that guard removed and REPO empty, the
  # glob passes through literally, grep exits 2, and THIS check escalates to
  # exit 2 -- reverting just this check to a bare `|| continue` restores the
  # old "nothing to validate" exit 0. Do not weaken it on the theory that the
  # REPO= guard covers that case; the coverage runs the other way too.
  #
  # That vanished-$REPO/docs route reaches grep ONLY because this script
  # leaves `nullglob` unset, which is what passes the unmatched glob through
  # as a literal filename for grep to fail on. Do not set it here: with
  # `nullglob` the loop body would never run at all, `found` would stay 0,
  # and the gate would go straight back to a clean "nothing to validate" exit
  # 0 on a missing docs tree -- this exact bug, restored, with every test
  # below still green (they run against the real docs/, which always matches).
  #
  # The `rc=$?`-capture-then-escalate here (and at the docker run site below)
  # is scripts/gate-lib.sh's escalate_unless_content -- see that
  # function's own header for the rationale, including why `rc=$?` must be the
  # first command in the `else` arm and why the command is never tested with
  # `!`.
  if grep -q '```mermaid' "$f"; then
    found=1
  else
    rc=$?
    escalate_unless_content "$rc" \
      "grep failed scanning $rel for a mermaid block (exit $rc) --" \
      "grep's exit 1 means \"no match\" (a content answer: no diagram in" \
      "this doc), so anything else is a machine fault, not content -- the" \
      "file may be unreadable, hit an I/O error, or \$REPO/docs may have" \
      "vanished mid-run. Diagnose with: grep -q -- '\`\`\`mermaid' $rel"
    continue
  fi
  if docker run --rm -v "$REPO:/data:ro" -v "$CFG:/cfg:ro" -w /data "$IMAGE" \
       -p /cfg/puppeteer.json -i "$rel" -o /tmp/out.md --quiet; then
    echo "OK    $rel"
  else
    rc=$?
    escalate_unless_content "$rc" \
      "docker run failed with exit $rc while validating" \
      "$rel. mmdc reports invalid mermaid as exit 1 and nothing else, so this" \
      "is a tool failure, not a syntax error — the diagram was never judged." \
      "Usual causes: the image is missing and the network is unreachable (125)," \
      "or the engine killed the container mid-run — Docker Desktop stopping" \
      "(Resource Saver mode), or an out-of-memory kill (137). Diagnose with:" \
      "docker info"
    echo "FAIL  $rel"
    fail=1
  fi
done

if [ "$found" -eq 0 ]; then
  echo "no mermaid diagrams found in docs/ — nothing to validate"
  exit 0
fi

if [ "$fail" -ne 0 ]; then
  echo "mermaid validation failed" >&2
  exit 1
fi
echo "all mermaid diagrams valid"
exit 0
