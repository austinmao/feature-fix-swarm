---
name: fix
description: "Investigate a bug, fix it through gsd-core's /gsd-quick loop (plan → execute → verify on a single quick task), verify with qa-only then full qa, then run review-gate cross-model adversarial review. Non-interactive — aborts with structured artifacts on uncertainty or CRITICAL findings. Ruflo coordinator removed in v3.0.0 (spec 002). Wires the fable→opus model-availability preflight (scripts/gsd/model-fallback.sh) before the gsd-quick loop in v3.1.0."
version: "3.2.0"
allowed-tools:
  - Read
  - Edit
  - Write
  - Bash
  - Glob
  - Grep
  - Skill
---

# /fix — Investigate, fix, and verify a bug via gsd-quick

One command. Takes a bug description or symptom, traces root cause, fixes it through
gsd's quick loop, verifies the fix, then verifies no regressions.

## When to invoke

- User reports a bug: "auth redirect is broken", "the form doesn't submit", "500 on /api/foo"
- Claude discovers a bug during QA or code review
- After `/qa` or `/qa-only` reports failures that need fixing

## Flags

| Flag | Effect |
|---|---|
| `--interactive` | Restore manual gates between phases. Default = non-interactive. |
| `--no-audit` | **Operator-accepted-risk bypass** of the adversarial completion audit. Trivial typo-class fixes only. |
| `--no-review-gate` | **Operator-accepted-risk bypass** of cross-model adversarial review. Provably minimal blast radius only. |

## Workflow

### Step 1: Investigate (root cause, not symptom)

Invoke `/investigate` (or trace inline for small bugs): reproduce, grep every caller
of the function to be touched, identify the shared root-cause site. Write the
finding as a one-paragraph statement with file:line refs before editing anything.

**Route to `/gsd-debug` when the reproduction is non-obvious** — explicit
criteria: (no failing test yet) AND (no single-command repro exists). Invoke
gsd's scientific-method hypothesis→experiment loop and consume its root-cause
report before editing anything. When a failing test or a single-command
repro already exists, stay on this `/investigate` + `/gsd-quick` path — do
not route to `/gsd-debug`.

### Step 2: Fix via gsd-quick

Invoke `/gsd-quick` with the root-cause statement as the task. gsd runs its
plan → execute → verify loop on the single task; the repo config's
`workflow.test_command` (`scripts/gsd/gates-test-command.sh`) gates completion —
gates.py evidence, not self-report.

Run the model-availability preflight first: gsd-quick's subagent tiers come from
`.planning/config.json` (read by gsd-core's resolver at spawn time — `gsd-run.sh`
launches only the sonnet-class lead), so a dead premium pin (Fable dropped off
OAuth) would error the planner/plan-checker spawn. The same lever
`/feature-implement` runs rewrites fable→opus before any spawn.

Best-effort, not a hard gate: it stays fail-soft (`.planning/config.json` may not
be seeded when it runs — same reason `feature-implement`'s call is fail-soft), but
it must NOT no-op *silently* — warn on skip/failure so `gsd-run.sh` surfacing the
real config error later isn't the first sign. Headless variant:

```bash
[ -f scripts/gsd/model-fallback.sh ] && bash scripts/gsd/model-fallback.sh .planning \
  || echo "[fix] model-availability preflight skipped/failed (best-effort) — gsd-run will surface any config error"
TIMEOUT=1800 bash scripts/gsd/gsd-run.sh /gsd-quick "<root-cause task>"
```

TDD applies: failing repro test first (RED), fix (GREEN); the gsd executor's
commit trail must show both.

### Step 3: Verify

1. `/qa-only` on the affected surface — fail → back to Step 2 (max 2 loops, then STOP + report).
2. Full `/qa` when browser-touchable.
3. `/review-gate` (unless `--no-review-gate`): HIGH/CRITICAL findings block done —
   fix or explicitly defer with operator sign-off.

### Step 4: Report

Root cause, diff summary, gate evidence id, QA results, review-gate verdict.
On abort: structured artifact (what was tried, failing signature, next hypothesis).

## Removed in v3.0.0

Ruflo swarm coordination (swarm_init/agent_spawn), Ralph retry loop
(gsd-quick's own verify loop + the 2-loop QA cap replaces it), audits.jsonl
run-state coupling, `--auto-fix` forwarding.
