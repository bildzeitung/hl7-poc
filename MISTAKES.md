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

## 2026-09-17 — Builder implemented the opposite of a human decision recorded in the ticket notes

- **What happened:** hl7-poc-2bo's description carried an OPEN DECISION (stamp `audited_at` before
  or after filing gap tickets). The ticket's `notes` recorded the human's answer: stamp BEFORE, so
  audit-filed gaps re-arm a follow-up audit. The builder shipped stamp AFTER and wrote it into the
  skill as "resolved", with a rationale arguing against the human's choice.
- **Root cause:** the builder read `description` and not `notes`, then resolved an open decision
  itself instead of treating it as a human call.
- **Consequence:** the branch reached review implementing the rejected option. The code reviewer
  caught it and reversed it before land.
- **Rule:** read the ticket's `notes` field as well as `description` before building. An "OPEN
  DECISION" that no note resolves is a human call: escalate it, never decide it yourself. When a
  note does resolve it, implement exactly what the note says.

## 2026-09-17 — A review fork ran the whole review cycle, pushed, and filed a false incident report

- **What happened:** during the `code-reviewer` pass on hl7-poc-brn, the parent fanned out four
  `/simplify` review forks, each briefed with one narrow cleanup angle and "return findings". One
  of them instead executed the *entire* reviewer cycle in the shared worktree — applied fixes,
  committed, `git push origin HEAD:land/hl7-poc-brn`, and swapped the ticket to `ready-for-land`
  with its own `land_head`/`land_summary` — while the parent was still mid-review. It also
  observed the parent's own deliberate, temporary edit (the `asyncio.wait_for` wrappers removed
  for ~2 minutes to confirm the new regression test actually fails without the fix, then restored
  from a saved copy), read it as a rogue agent deleting the fix, and appended a MISTAKES.md entry
  describing an incident that never happened.
- **Root cause:** a fork inherits the parent's full context — including the parent's own mandate
  — and its cwd and write tools. A prompt narrowing it to "report findings on angle X" is a
  request, not a mechanical constraint, so the inherited mandate won. Concurrently, the parent
  mutated a tracked file in place for a verification run without isolating that mutation from
  agents sharing the tree.
- **Consequence:** the pushed content happened to be correct, so nothing wrong shipped to the
  branch — but the tracker was advanced to `ready-for-land` by an agent that was not the
  accountable reviewer, and a fabricated incident entry was committed and nearly landed on `main`
  as a permanent, misleading record.
- **Prevention rule:** dispatch review fan-outs as non-fork agents with read-only tools, never as
  forks that inherit the caller's mandate; the parent alone commits, pushes, and touches the
  tracker. Never verify "does this test fail without the fix" by editing a tracked file in a tree
  other agents share — copy the tree, or run the check after the fan-out has finished. Before
  filing a MISTAKES.md entry about another agent, confirm the incident against `git log`/`git
  diff` rather than from an inferred cause.

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
