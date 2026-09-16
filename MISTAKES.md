# MISTAKES.md — the mistake log

Append-only record of qualifying mistakes, **newest first**. The qualifying bar, dedup rule, and
who-writes-what live in CLAUDE.md directive 9 — this file does not restate them. In short: an entry
is warranted when a mistake destroyed or risked real work, or shipped a wrong artifact, **and** a
concrete prevention rule can be derived from it. Before appending, grep this file for an existing
entry on the same root cause/incident.

Entry shape (one `##` heading per entry, newest at the top):

```markdown
## YYYY-MM-DD — <one-line summary>

- **What happened:** ...
- **Root cause:** ...
- **Consequence:** ...
- **Prevention rule:** ...
```

<!-- entries below, newest first -->

## 2026-09-16 — Rebase pickups overwrote land_summary, so merges on main describe the rebase

- **What happened:** the `coding` agent's needs-rebase pickup set
  `land_summary="Merged main @ <sha> into the branch"`, and `/code`'s SKILL.md told it to refresh
  `land_summary`. Merges e7d267c (hl7-poc-08v) and 0aae1c4 (hl7-poc-5fu) landed on `main` with
  that text as their message (fixed by hl7-poc-dyy).
- **Root cause:** the pickup treated `land_summary` as per-hand-off state to refresh alongside
  `land_head`, when it is the code-reviewer's description of the work, which `/land` consumes
  verbatim as the merge commit message.
- **Consequence:** two pushed merge commits on `main` whose messages say nothing about the change;
  not rewritten.
- **Prevention rule:** a stage writes only the bd metadata it owns. A stage that does not change
  what a branch *does* (rebase pickup) refreshes `land_head` only and never overwrites a summary
  another stage authored for a downstream consumer.

## 2026-09-15 — Two tickets and a bounce spent fixing a checker no gate ever ran

- **What happened:** `scripts/check_docstring_refs.py` was improved across a scan-root rewrite
  (hl7-poc-k3t), a prefix fix (hl7-poc-kke), and left two more follow-ups open (hl7-poc-psk,
  hl7-poc-0ah) plus a review bounce (hl7-poc-66x) — all changes to a checker that had never run in
  any gate. `git grep` for its name hit only the script itself and its own harness test module
  (`tests/harness/test_check_docstring_refs_scan_roots.py`); no `noxfile.py` session, no
  `scripts/*.sh` gate, and no `.claude/` hook, skill, or agent file ever invoked it.
- **Root cause:** the script was written and repeatedly refined without first confirming it was
  wired into any gate. Its only executor was its own harness test, which runs solely when a
  harness path changes — so producer gates, `/land`'s re-gate, and replay all stayed green
  regardless of dangling `hl7poc.*` docstring refs in the tree.
- **Consequence:** real engineering effort (two build tickets plus a code-review round) was spent
  improving a checker that could never fail a build, catch a regression, or block a land — pure
  sunk cost with zero enforcement value produced.
- **Prevention rule:** a ticket titled as a fix to an existing gate/checker must first prove the
  gate runs somewhere — name the exact nox session, gate script, or hook that invokes it — before
  any work changes what it checks. If no invocation point can be named, the ticket is either
  "wire this in" or "remove this," not "improve this."
