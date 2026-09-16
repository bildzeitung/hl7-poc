# Decisions

Settled decisions for this project, recorded so the next reader doesn't have to reconstruct them
from ticket history.

**This file is append-only.** A decision, once recorded, is never edited or deleted in place — a
later change supersedes an entry with a new, dated entry that carries an explicit supersession
marker pointing back at the one it replaces (`SUPERSEDES: <date> — <short description>` under the
newer entry). `scripts/check-decisions-no-silent-rewrite.sh` fails a branch that removes any
pre-existing non-blank line of this file between the merge base and the branch head; entries go on
the end, always.

## Entries

**2026-09-14 — Static version, no release tooling (`hl7-poc-izt`).** This is a PoC. Every
distribution keeps a static `version = "0.0.0"` in its `pyproject.toml`, and the git tag is the
only version identity. Nothing in the repo reads package metadata at runtime. Revisit only if the
project is productized.

**2026-09-14 — The listener transforms; no raw HL7 on the queue (epic `hl7-poc-wld`, `/challenge`).**
The listener parses each HL7 message into a full canonical model and puts only that canonical body
on the `hl7-events` queue. No raw HL7 ever travels on the queue. The worker consumes the canonical
model and never sees HL7. This reverses the epic's original "thin edge, no transform" shape and
un-defers "a real HL7 parser." README.md's existing "transform into a canonical format" wording is
correct as written and is left alone. Consequences: the worker's decision rules and tests become
canonical-model-shaped (see the next entry); the `hl7poc.hl7` segment/field helpers are no longer
needed (see `hl7-poc-5mx` below).

**2026-09-14 — python-hl7 is the parser, declared by the listener member only (`hl7-poc-wld.3`).**
The listener uses [python-hl7](https://pypi.org/project/python-hl7/) to parse incoming HL7 into the
canonical model. The `hl7` dependency is declared by the listener workspace member only — never by
`hl7poc-core` and never by the worker — so the worker's container image stays free of any HL7
parsing library. `hl7-poc-5mx`'s three hand-lifted segment/field helpers (`hl7poc.hl7`) have no
consumer once this decision stands and that ticket closed superseded rather than landing.

**2026-09-14 — Canonical model lives in core, stdlib + JSON, `schema_version` field
(`hl7-poc-wld.3`).** The canonical message model is a set of stdlib dataclasses in `hl7poc.model`
(no third-party imports), serialized to and from JSON with an explicit `schema_version` field in
the header. The model has no "raw" or "segments" escape hatch — every message type is mapped fully
or the frame is rejected. The field list itself lives in `docs/canonical-model.md`, owned by
`hl7-poc-wld.3`.

**2026-09-14 — Bare command runs the service; no subcommand (`/challenge` on `hl7-poc-wld`, applied
across `hl7-poc-abh`/`hl7-poc-hcs`/`hl7-poc-4f5`).** `hl7listener` and `hl7worker` are single-command
Typer apps: the bare command starts the service, `--help` works, and there is no `run` subcommand.
This resolved a three-way disagreement between the original Typer skeleton (`hl7-poc-c1t`, which had
a `run` subcommand under an `@app.callback()`), the listener/worker tickets, and the Dockerfiles'
`ENTRYPOINT` shape (which has no `CMD` and therefore needs a bare invocation to work).

**2026-09-14 — Python 3.14 (`hl7-poc-4f5`, verified against the full dependency tree).** The project
targets Python 3.14 end to end: the local `.python-version`, and the container images
(`ghcr.io/astral-sh/uv:python3.14-bookworm-slim` for the build stage, `python:3.14-slim-bookworm`
for the runtime stage). Verified that the full runtime dependency tree (azure-servicebus, azure-core,
typer, hl7, and their transitives) resolves for CPython 3.14 on Linux, including the one native
wheel in the tree (charset-normalizer, which ships a cp314 build).

**2026-09-14 — uv workspace of three namespace members (`hl7-poc-ouc`).** The project splits into a
uv workspace (`[tool.uv.workspace] members = ["packages/*"]`) with three members —
`packages/core` (`hl7poc-core`, provides `hl7poc.model`, stdlib-only), `packages/listener`
(`hl7poc-listener`, provides `hl7poc.listener`, depends on `hl7poc-core` + `azure-servicebus` +
`typer` + `hl7`), and `packages/worker` (`hl7poc-worker`, provides `hl7poc.worker`, depends on
`hl7poc-core` + `azure-servicebus` + `typer`, deliberately not `hl7`) — sharing the `hl7poc` PEP 420
namespace package (no `src/hl7poc/__init__.py` anywhere). Import paths (`hl7poc.model`,
`hl7poc.listener`, `hl7poc.worker`) are unchanged from the single-distribution layout; only the
on-disk location under `packages/*/src/hl7poc/...` moves. One root `uv.lock`. This is what makes
`uv sync --package <service>` install one service plus core and nothing else, which the Dockerfiles
(`hl7-poc-4f5`) depend on.

**2026-09-14 — Compose and `servicebus-config.json` are local test infrastructure; the Dockerfiles
are shipping code (epic `hl7-poc-wld`, applied in `hl7-poc-4f5`).** `docker-compose.yml` runs
SimHospital (an HL7 traffic source) and a Service Bus emulator so a developer can exercise the
listener and worker started from the checkout with `uv run`. It never builds or runs the service
container images. The two `Dockerfile`s under `packages/listener/` and `packages/worker/` are the
actual shipping artifacts, destined for a container registry and a k8s cluster — they have no
relationship to compose.

**2026-09-14 — Release/versioning machinery removed (`hl7-poc-wld.1`).** Following the static-version
decision above, the harness's leftover release tooling (`scripts/release.sh` and friends, the
`/release` skill, their harness tests) is dropped from this repo: nothing here consumes the SemVer
tags it would cut. `uv.lock` / `lock_currency` machinery is unaffected — that is dependency pinning,
not package versioning, and stays.

**2026-09-14 — Listener `/ready` does not gate on Service Bus; no probe message on `hl7-events`
(`hl7-poc-kps`).** The listener's design is spool-first: it spools and ACKs an MLLP frame before
forwarding, precisely so it keeps accepting traffic while Service Bus is down. Gating `/ready` on
the bus contradicts that — k8s would stop routing to the listener and the sending system would back
up on its own side instead of in the spool. So `/ready` returns 200 iff MLLP is listening, the spool
directory is writable, and the process is not shutting down; Service Bus reachability is not a
readiness input. `sb_healthy` is still reported in `/ready`'s JSON body and still updated by real
send outcomes and the spool drain — it only needs to be eventually accurate, so it needs no probe.
The listener's periodic `_startup_probe` (a fake `ServiceBusMessage` with body `"probe"`,
`session_id "_probe"`, `msgType=PROBE`) and the worker's matching `msgType=PROBE` completion branch
are removed. `hl7-events` carries `CanonicalMessage` JSON only, per `docs/canonical-model.md` — no
other message shape is ever put on it.

**2026-09-14 — One shared probe HTTP server in `hl7poc.probe` (`hl7-poc-kps`).** `handle_http` (the
`/live`-always, `/ready`-when-given-a-callable request handler) and the probe read timeout constant
live once, in `packages/core/src/hl7poc/probe.py`, parameterised by an optional readiness callable
so the worker (no `/ready`) and the listener (`/ready` per the decision above) share one
implementation instead of two independently-drifting copies.

**2026-09-15 — Where `nox -s build_members` runs (`hl7-poc-adr`).** `build_members` (added by
`hl7-poc-y90`) runs in `/land`'s combined re-gate and in `scripts/land-replay.sh`'s per-branch
isolation replay — both the baseline and the per-branch loop — so a red combined pass can be
attributed to the same gate set the replay re-checks. It also runs in `scripts/update-deps.sh`,
since a lock bump can change what a build resolves. It deliberately does **not** run in the
`coding` producer's or `code-reviewer`'s per-branch gates (same treatment `lock_currency` already
gets there): those gates catch it earlier only at the cost of one real build per branch, and
`/land`'s single combined pass already catches it before anything reaches `main`.

**2026-09-15 — Undecodable frame bytes NACK, never silently replace (`hl7-poc-bkk`).** The listener
guarantees wire fidelity: a frame that is not valid UTF-8 is rejected (spooled to
`<spool-dir>/rejected/`, logged with whatever control id a best-effort lossy decode can recover for
the log line only, and NACKed with an AE) rather than forwarded with `errors="replace"` munging the
bytes an AA ACK would then vouch for. This is the same "durable first, never lose data silently"
stance `hl7-poc-hob` already applied to the spool's CR-terminator round-trip; that ticket's
land-review flagged the live-path `errors="replace"` decode in `extract_frames` as the sibling gap
this decision closes. `extract_frames` no longer decodes at all -- it returns raw frame bytes, and
`process_frame` spools those bytes byte-exact before attempting the strict UTF-8 decode, so the
undecodable bytes are never lost even though they are never forwarded.

**2026-09-16 — Worker applies the same fail-closed stance to Service Bus bodies (`hl7-poc-ngr`).**
The worker is the last hop, so an undecodable message body is not recoverable there either: `handle`
now decodes strictly (`errors="strict"`, the `bytes.decode` default) instead of `errors="replace"`.
A `UnicodeDecodeError` falls into the same generic `except Exception` the worker already uses for
any other poison message -- it is dead-lettered with `reason="ProcessingError"`, not silently
processed as munged text. No new failure path was needed; the worker already had one for exactly
this shape of problem, unlike the listener, which had to add spool-then-NACK plumbing.

**2026-09-16 — `scripts/discard-beads-passive-export-churn.sh` restores each listed passive export
with its own `git checkout` call, never one call listing them all (`hl7-poc-1yh`).** `git checkout`
is atomic over its pathspecs: one call listing every entry from `scripts/beads-passive-exports.txt`
fails whole-hog and restores nothing the moment any single entry is unknown to git in this repo
(e.g. `.beads/issues.jsonl` is never tracked here). The script loops, one `git checkout HEAD --
<path>` per entry, so an unknown/untracked entry only no-ops for itself and the others still
restore. This mirrors the per-entry `git restore` rule `scripts/land-merge-one.sh` already follows
for the same list (see that script's own comment).

## Deferred, not forgotten

Decisions this project has deliberately not made yet. Each stays open until a ticket revisits it.

- **Decision table: code or config.** The worker's per-message-type notification rules
  (`hl7-poc-hcs`) are currently a dict/if-chain in code. Whether they should move to an external,
  editable config format is an open question.
- **Real push transport.** Notifications are currently a print statement or a plain webhook POST.
  FCM/APNs (or any real push provider) integration is deferred.
- **k8s manifests, registry, and CI image publishing.** The two Dockerfiles (`hl7-poc-4f5`) produce
  buildable images; nothing in this repo yet deploys them, publishes them to a registry, or runs a
  CI pipeline over them.
- **Topics/subscriptions.** The listener sets `msgType`/`event` Service Bus application properties
  with topic SQL filters in mind, but nothing in this project consumes a topic today — only the
  plain `hl7-events` queue exists.
- **Model schema evolution beyond `schema_version`.** The canonical model's header carries a
  `schema_version` integer field, but there is no migration or compatibility strategy yet for
  changing the model's shape over time.
- **HL7 structural validation.** Beyond what python-hl7's own parsing enforces, there is no
  additional HL7 structural/conformance validation layer.
