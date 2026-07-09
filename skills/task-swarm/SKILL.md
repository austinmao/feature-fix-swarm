---
name: task-swarm
description: "Take ANY task description end-to-end autonomously: plan-eng-review → codex gate → gsd project seed (spec-decompose) → preflight → grant ledger → /feature-implement --autonomous (gsd execute-phase → QA → review-gate → ship → canary). Use when the operator hands you next instructions and wants planning, task creation, and execution without babysitting."
version: "2.2.0"
allowed-tools:
  - Read
  - Write
  - Bash
  - Glob
  - Skill
  - AskUserQuestion
---

# /task-swarm "<task description>"

One command from free-text instruction to shipped, canaried change. Thin
orchestrator — every stage is an existing skill; this skill owns only the
sequencing, the run-state, and the single operator stop (the grant screen).

```
/task-swarm "add rate limiting to the webhook endpoints"
     │
     ├─ 1. /plan-decompose  (plan-eng-review → codex plan gate → swarm
     │      spec-decompose → specs/NNN/tasks.md → codex score gate)
     ├─ 2. preflight        (emit specs/NNN/preflight.json → gates.py preflight → PASS)
     ├─ 3. grant screen     (enumerate typed gates from tasks.md → operator yes → ledger)
     └─ 4. /feature-implement NNN --autonomous
            (roster swarm impl → QA loop → review-gate → ship → canary, ledger-gated)
```

## Flags

```
/task-swarm "<task>"                 # full autonomous run (one stop: grant screen)
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

Same contract as `/feature-spec` Step 5: author `specs/NNN/preflight.json` from
the RUN's real footprint (env/secret NAMES + service probes — never values),
then:

```bash
# NNN = BARE NUMERIC spec id plan-decompose just created (capture from Step 1's
# output; strip any -name suffix — ledger convention is spec-057, never spec-057-name)
NNN="<bare numeric spec id from Step 1>"
GATES_PY=""
for _cand in \
  "$(git rev-parse --show-toplevel 2>/dev/null)/packages/feature-fix-swarm/lib/gates.py" \
  "$HOME/.claude/lib/feature-fix-swarm/gates.py" \
  "$(git rev-parse --show-toplevel 2>/dev/null)/lib/gates.py"; do
  [ -f "$_cand" ] && GATES_PY="$_cand" && break
done
[ -z "$GATES_PY" ] && { echo "[task-swarm] FATAL: gates.py not found — run setup.sh"; exit 1; }
RUN_ID="spec-${NNN}"
python3 "$GATES_PY" preflight "specs/${NNN}/preflight.json" --run "$RUN_ID"
```

Fail → fix now (fetch secret, start service, re-auth), re-run until
`PREFLIGHT-PASS`. `--autonomous` later refuses to start without this.

Model ladder comes from the seeded `.planning/config.json` (haiku=light /
sonnet=standard / opus=heavy tiers + fable/opus pins for planner/verifier).
`scripts/gsd/model-fallback.sh` already ran at seed time (spec-decompose Step 2)
and runs again inside feature-implement's walls — unavailable premium pins
(fable off OAuth) are rewritten to opus before any spawn. Nothing to do here;
just don't hand-pin models in prompts.

### Step 3 — grant screen (skip with --attended)

Walk tasks.md and enumerate every operator-gated action in typed form
(`push:origin/<branch>`, `merge:pr`, `deploy:<target>`, `flip:<FLAG>`,
`restart:<svc>`, `secret-use:<NAME>`, `migrate:<desc>`), each with a one-line
rollback. Present ONE screen. On explicit yes:

```bash
python3 "$GATES_PY" grant "$RUN_ID" --action <a1> --action <a2> ... --ttl-hours 24
```

Declined actions are simply not granted — the run stops+records `pending` only
there; everything else proceeds.

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
`python3 "$GATES_PY" grant $RUN_ID --action <a>` then `/feature-implement NNN --autonomous`.
Close the loop with `/gsd-extract-learnings` (fail-soft).

## Rules

- **One operator stop.** The grant screen is the only planned interruption; a
  second stop means either preflight was skipped or a gate wasn't enumerated —
  both are defects, name them in the report.
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
  extra steps; use the grant screen.
