---
name: task-swarm
description: "Take ANY task description end-to-end autonomously: plan-eng-review → codex gate → swarm spec-decompose → preflight → grant ledger → /feature-implement --autonomous (swarm impl → QA → review-gate → ship → canary). Use when the operator hands you next instructions and wants planning, task creation, and swarm execution without babysitting."
version: "1.0.0"
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

- `mcp__ruflo__session_save` a checkpoint tag `task-swarm:<slug>` now and after
  every stage — an overnight run must survive a context reset
  (long-run-continuity: the durable save is the guarantee, not in-context notes).
- `mcp__ruflo__hooks_pre-task` on the task description (learning context) and
  `mcp__ruflo__agentdb_pattern-search` for prior similar runs. If gbrain is
  present (`command -v gbrain` + `env -u DATABASE_URL gbrain doctor` healthy):
  `env -u DATABASE_URL gbrain query "<task topic>"` for prior decisions. All
  three are fail-soft — absence never blocks.

### Step 1 — plan + decompose

Invoke the `plan-decompose` skill with the task description (append `--no-swarm`
if passed). It runs `/plan-eng-review` (autonomous mode), the codex plan gate,
then `/spec-decompose` (roster swarm, v1.4.0) and its `gates.py analyze` +
`agents_manifest.py check` gates. Output: `specs/NNN/tasks.md` with a spec ID.

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

Invoke the `feature-implement` skill: `NNN --autonomous` (or `NNN --auto` when
`--attended`). It enforces `check-preflight`, executes the roster swarm with the
QA loop, then runs the Step 10 finish tail (review-gate → ship → canary), each
outward action behind `check-grant`.

### Step 5 — report + retro

feature-implement's proof artifact + final report are the record. Add this
skill's wrapper line to `.feature-fix-swarm/results.md`:
`TASK-SWARM "<task slug>" spec={NNN} outcome={shipped|stopped:<action>|failed}`.
Report `pending` actions (if any) with the exact resume command:
`python3 "$GATES_PY" grant $RUN_ID --action <a>` then `/feature-implement NNN --autonomous`.
`mcp__ruflo__hooks_post-task` closes the learning loop (fail-soft).

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
