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
