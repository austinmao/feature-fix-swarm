---
name: task-swarm
description: "Take ANY task description end-to-end autonomously: plan-decompose → /preflight → /autonomy-grant (MAX-AUTH) → /feature-implement --autonomous. v3.0.0: pure sequencing front-end over feature-implement — like /fix but with full planning; zero machinery of its own. Use when the operator hands you next instructions and wants planning, task creation, and execution without babysitting."
version: "3.0.0"
allowed-tools:
  - Read
  - Write
  - Bash
  - Glob
  - Skill
  - AskUserQuestion
---

# /task-swarm "<task description>"

One command from free-text instruction to shipped, canaried change. Pure
sequencing front-end — every stage IS an existing skill (`plan-decompose`,
`preflight`, `autonomy-grant`, `feature-implement`); this skill owns only the
ordering, the spec-id capture, and the results.md wrapper line. `/fix` is the
sibling front-end for the no-planning case (`/feature-implement --adhoc`).
MAX-AUTH auto-grant by default — zero planned stops; `--gated` restores the
review stop.

```
/task-swarm "add rate limiting to the webhook endpoints"
     │
     ├─ 1. /plan-decompose  (plan-eng-review → codex plan gate → swarm
     │      spec-decompose → specs/NNN/tasks.md → codex score gate)
     ├─ 2. preflight        (emit specs/NNN/preflight.json → gates.py preflight → PASS)
     ├─ 3. grant ledger     (enumerate typed gates from tasks.md → AUTO-GRANT
     │                       [--gated: operator yes first] → ledger)
     └─ 4. /feature-implement NNN --autonomous
            (roster swarm impl → QA loop → review-gate → ship → canary, ledger-gated)
```

## Flags

```
/task-swarm "<task>"                 # full autonomous run (ZERO stops — MAX-AUTH auto-grant)
/task-swarm "<task>" --gated         # review + approve the gate list at Step 3
/task-swarm "<task>" --attended     # skip grant; gates prompt during implement
/task-swarm "<task>" --no-swarm     # single-planner decomposition passthrough
/task-swarm "<task>" --dry-run     # print the stage plan, execute nothing
```

## Procedure

### Step 0 — continuity + recall (fail-soft)

- Continuity is gsd-native: `.planning/STATE.md` survives context resets;
  resume with `/gsd-resume-work`. No external checkpoint call needed.
- If gbrain is present (`command -v gbrain` + `env -u DATABASE_URL gbrain doctor`
  healthy): `env -u DATABASE_URL gbrain query "<task topic>"` for prior
  decisions. Fail-soft — absence never blocks.

### Step 1 — plan + decompose

Invoke the `plan-decompose` skill with the task description. It runs
`/plan-eng-review` (autonomous mode), the codex plan gate, then
`/spec-decompose` (gsd project seed + `/gsd-plan-phase`). Output: a seeded
`.planning/` project keyed to spec ID NNN.

STOP on any gate failure — surface findings; do not push a broken plan forward.

### Step 2 — preflight (always; while the operator is still around)

Capture the spec id from Step 1's output: `NNN` = BARE NUMERIC id (strip any
`-name` suffix — ledger convention is `spec-057`, never `spec-057-name`);
`RUN_ID="spec-${NNN}"`.

Then invoke the `preflight` skill for `RUN_ID` — it owns the whole contract
(author `specs/NNN/preflight.json` from the RUN's real footprint, env/secret
NAMES + real probes — never values; `gates.py preflight` fail-closed). Fail →
fix now (fetch secret, start service, re-auth), re-run until `PREFLIGHT-PASS`.
`--autonomous` later refuses to start without this.

Model ladder comes from the seeded `.planning/config.json`; feature-implement's
walls re-run `model-fallback.sh` before any spawn. Nothing to do here — just
don't hand-pin models in prompts.

### Step 3 — grant ledger (MAX-AUTH auto-grant DEFAULT; --gated to review; skip with --attended)

Invoke the `autonomy-grant` skill for `RUN_ID` — it owns enumeration (typed
`type:target` actions walked from the plan, each with a one-line rollback),
MAX-AUTH mode (default here: record ALL immediately, list + rollbacks go in the
Step 5 report), the `--gated` review screen, and the recording command. In BOTH
modes the safety floor holds: entries are exact typed actions, and an action
not enumerated still stops + records `pending`.

### Step 4 — autonomous implement + finish tail

Invoke the `feature-implement` skill: `NNN --autonomous` (or bare `NNN` when
`--attended`). It enforces `check-preflight`, drives `/gsd-execute-phase` with
gates.py as completion authority, then runs the finish tail (review-gate →
ship → canary), each outward action behind `check-grant`.

**Anti-early-stop (hold every turn of the autonomous loop):** before ending
your turn, check your last paragraph. If it is a plan, an analysis, a question,
a list of next steps, or a promise about work you have not done ("I'll…"), do
that work now with tool calls. End your turn only when the task is complete or
you are blocked on input only the user can provide.

### Step 5 — report + retro

feature-implement's proof artifact + final report are the record. Add this
skill's wrapper line to `.feature-fix-swarm/results.md`:
`TASK-SWARM "<task slug>" spec={NNN} outcome={shipped|stopped:<action>|failed} models={opus:N,sonnet:N,haiku:N,fable:N,inline-mechanical:N}`.
The `models=` histogram counts Task/Agent spawns by pinned model over the
whole run (`inline-mechanical` = trip-wire work the host drained inline —
see `feature-spec` SKILL.md § Orchestrator self-discipline; target 0).
Cross-check with `python3 lib/gates.py delegation-audit <transcript>`.
Report `pending` actions (if any) with the exact resume command:
`python3 lib/gates.py grant $RUN_ID --action <a>` (resolve gates.py per
feature-implement Step 1's 3 install shapes) then `/feature-implement NNN --autonomous`.
Close the loop with `/gsd-extract-learnings` (fail-soft).

## Rules

- **Zero planned stops (MAX-AUTH default); one with `--gated`.** Any
  unplanned stop means either preflight was skipped or a gate wasn't
  enumerated — both are defects, name them in the report.
- **Never bypass a child skill's gate.** This skill sequences; it holds no
  authority of its own. `SKIP_OPERATOR_GATES=1` is not this skill's business.
- **Stops are cheap, silent failure is not.** An ungranted gate stops that path
  and records `pending`; independent tasks continue. Morning resume is one
  `grant` command.

## Anti-patterns

- Re-implementing planning/decompose/implement logic inline — invoke the skills.
- Granting `push:*` style blankets — typed exact actions only (ledger rejects
  wildcards by design).
- Running with `--attended` overnight — that's just an unattended stall with
  extra steps; use the grant ledger (default auto-grant, or `--gated`).
