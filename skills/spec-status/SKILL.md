---
name: spec-status
description: "Status check for the current spec run: what's done, running, tested, blocked, and next. Fan out volume, execution, and judgment roles over git, .planning, gates, runner state, evidence, and hygiene; write a status report and host-native handoff."
version: "1.1.0"
---

# /spec-status [NNN] [--continue-compact] [--no-handoff]

## Host dispatch contract

- Codex: invoke skills as `$skill`; use Codex collaboration roles and GPT-5.6 model tiers.
- Claude: invoke skills as `/skill`; use Claude Agent/Skill tools and Claude model aliases.
- Examples that name both hosts are routing contracts. Never send one host's command syntax to the other.
- A bare `/skill` in this shared source denotes the Claude form; Codex dispatches the same named skill as `$skill`.

Answers "where are we?" for a spec run with evidence, not recall. Read-only
except the report + handoff files it writes.

## Step 1 — Resolve

Spec id from argument, else from branch (`git branch --show-current | grep -oE '^[0-9]{3}'`).
Export the run pins BEFORE any ledger read — decoy stores lie:

```bash
export GSD_RUN_ID="spec-${SPEC_ID%%-*}"
# GATES_STORE must point at the MAIN evidence store, never a worktree-cwd copy.
```

## Step 2 — Deterministic facts (no LLM)

```bash
bash "$(dirname <this skill>)/scripts/collect-status-facts.sh" "$SPEC_ID"
```

Sections: GIT · PLANNING (plans vs SUMMARYs per phase) · RUNNER (status file +
pid liveness — never trust shell rc or `state=completed` alone) · LEDGER
(pendings; grants checked via `check-grant`) · EVIDENCE (files + mtimes) ·
HYGIENE (key-shaped strings in evidence/, filenames only) · DISK.

## Step 3 — Fan-out (task-swarm style; every spawn model-pinned)

| Agent | Model | Contract | Job |
|---|---|---|---|
| planning scout | volume | scout ≤15 lines | ROADMAP flips vs SUMMARY presence; dirty-file classification (spec's vs foreign); branch drift vs upstream |
| ledger scout | volume | scout ≤15 lines | pendings by class (operator-reserved vs drainable); grant TTLs near expiry (<4h = flag); evidence-store decoy check |
| runner analyst | execution | build ≤20 lines | classify runner log tail: progressing / stopped-at-gate / stalled / dead; name the exact stop rule or gate if stopped |
| test/gate analyst | execution | build ≤20 lines | last gate evidence per phase (verify-done ids), suite counts from evidence not re-runs; flag fixtures older than the code they test |
| risk assessor | judgment | deep ≤40 lines | conclusion first: top blockers ranked by likelihood, what only the operator can decide, what the next 3 actions are |

Resolve `volume` to Codex Luna low / Claude Haiku, `execution` to Codex Terra
medium / Claude Sonnet, and `judgment` to Codex Sol high / Claude Opus.
Synthesis uses judgment unless the thin orchestrator can reconcile the reports
inline. Producer ≠ reviewer holds: the synthesizer never re-litigates scouts;
it reconciles contradictions and says which report won.

## Step 4 — Report

Write `.planning/STATUS-<UTC yyyymmdd-HHMM>.md`:

- **Headline** — one sentence: phase N of M, running/stopped/blocked.
- **Done** — phases/plans complete, with gate evidence ids.
- **In flight** — runner state + what it is executing right now.
- **Tested** — what has machine evidence vs what is only self-reported.
- **Blocked / operator-reserved** — each pending with its reason + the
  one-command unblock.
- **Hygiene/risk** — secret-shaped strings, TTL expiries, disk, drift.
- **Next 3 actions** — from the risk assessor, reconciled.

Print the headline + Next 3 inline; the file carries the rest.

## Step 5 — Handoff / compact tail

- Default: invoke `/handoff` with focus "resume <spec> — <headline>".
- `--no-handoff`: skip.
- `--continue-compact`: invoke `/continue-compact` INSTEAD of bare `/handoff`
  (it chains handoff → context-save → compact command block; never trigger
  `/compact` yourself — present the block for the operator).

## Constraints

- READ-ONLY toward the run: never restart/kill the runner, never flip
  checkboxes, never grant/consume ledger entries.
- Secrets: names only; hygiene findings report filenames, never content.
- A live runner (pid ALIVE) means analysts read logs, not `.planning` files
  mid-write — note "snapshot while running" in the headline.
