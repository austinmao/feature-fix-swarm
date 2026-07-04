---
name: feature-spec
description: "Spec-first pipeline: speckit.specify → speckit.plan → speckit.clarify → spec-decompose (swarm) → preflight (default) → autonomy-grant (default). Produces spec.md + plan.md + tasks.md + a proven preflight + a grant ledger, ready for /feature-implement NNN --autonomous."
version: 1.2.0
---

# /feature-spec [NNN | "description"]

Spec-first feature definition pipeline. Chains three speckit phases in sequence and
enforces that TDD unit tests, BDD behavior scenarios, and E2E test definitions are
present in every artifact before implementation begins.

## Why

Features built without upfront test contracts drift: unit tests get retrofitted, BDD
scenarios never get written, and E2E coverage is added last (or not at all). This
skill bakes test contracts into the spec so they gate implementation, not follow it.

See `docs/tdd-bdd-guide.md` for the full research-backed TDD/BDD reference.

## Pipeline

```
┌─────────────────────────────────────────────────────────────────┐
│  /feature-spec [NNN]                                            │
│                                                                 │
│  Phase 1: /speckit.specify                                      │
│    ├─ Writes specs/NNN/spec.md                                  │
│    ├─ ENFORCED: BDD scenarios (Given/When/Then per user story)  │
│    ├─ ENFORCED: Acceptance criteria (numbered, testable)        │
│    └─ ENFORCED: E2E test path definitions (PATH-NNN)            │
│                                                                 │
│  Phase 2: /speckit.plan                                         │
│    ├─ Writes specs/NNN/plan.md                                  │
│    ├─ ENFORCED: Unit test list (all anticipated cases, in order)│
│    ├─ ENFORCED: TDD unit test file map + function signatures    │
│    ├─ ENFORCED: Integration test cases (INT-NNN)                │
│    └─ ENFORCED: Per-phase test gate commands                    │
│                                                                 │
│  Phase 3: /speckit.clarify                                      │
│    ├─ Resolves ambiguities, captures edge cases                 │
│    ├─ ENFORCED: E2E Playwright test stubs (one per PATH-NNN)    │
│    └─ ENFORCED: Test contract summary (counts by layer)         │
│                                                                 │
│  Phase 4: /spec-decompose (v1.2.0 — swarm default)              │
│    └─ Writes specs/NNN/tasks.md (roster [agent:] tags + gates)  │
│                                                                 │
│  Phase 5: preflight (v1.2.0 — DEFAULT)                          │
│    └─ specs/NNN/preflight.json proven PASS while operator here  │
│                                                                 │
│  Phase 6: autonomy grant (v1.2.0 — DEFAULT)                     │
│    └─ typed gate ledger recorded → --autonomous never stalls    │
└─────────────────────────────────────────────────────────────────┘
```

> **Note on command naming:** speckit uses `speckit.specify` (not `speckit.spec`).
> Invoke via the Skill tool with name `speckit.specify`.

> **Optional recall (fail-soft):** before Phase 1, if `command -v gbrain` succeeds and
> `env -u DATABASE_URL gbrain doctor` is healthy, run
> `env -u DATABASE_URL gbrain query "<feature topic>"` and feed prior decisions into
> specify/plan. Absent/unhealthy → `git log --oneline --grep="<topic>"` fallback; never block.

## Usage

```
/feature-spec NNN                # full pipeline through decompose + preflight + grant
/feature-spec NNN --no-clarify   # skip clarify phase
/feature-spec NNN --no-preflight # skip preflight (NOT recommended before --autonomous)
/feature-spec NNN --no-grant     # skip grant ledger (attended runs)
/feature-spec NNN --no-swarm     # single-planner decomposition
/feature-spec NNN --dry-run      # preview what would be generated, no writes
```

---

## Required Sections Per Phase

Each phase MUST include the following sections. Verify their presence and add
them if speckit did not generate them.

### Phase 1 — speckit.specify must produce

```markdown
## BDD Scenarios

Feature: <feature name in plain English>

Scenario: <observable outcome — stakeholder language, not test-case language>
  Given <system state BEFORE the action — precondition, not an action>
  When  <the ONE action the user takes or event that occurs>
  Then  <the observable outcome the stakeholder cares about>

Scenario: <error path name>
  Given <state that leads to error>
  When  <the invalid action>
  Then  <the observable error response>

## Acceptance Criteria
- AC-001: <specific, testable, unambiguous criterion>
- AC-002: …

## E2E Test Paths
- PATH-001: <critical user journey in one sentence>
- PATH-002: …
```

**BDD scenario rules:**
- One `When` per scenario — if you need two `When`s, write two scenarios
- `Given` = precondition ("the user is logged in"), never an action ("the user logs in")
- `Then` = stakeholder-observable outcome, not internal state or method calls
- Write at least one happy-path + one error-path per user story

### Phase 2 — speckit.plan must produce

```markdown
## Unit Test List
All anticipated test cases, sequenced to hit design-critical paths first.
(Fowler: "Writing out a test list first is a vital initial step.")
- [ ] <function>: <atomic behavior> (e.g., validateEmail: returns false for missing @)
- [ ] <function>: <edge case>
- [ ] …

## TDD Unit Test Map
| Source file | Test file | Functions to test + atomic behaviors |
|-------------|-----------|--------------------------------------|
| src/foo.ts  | tests/unit/foo.test.ts | doThing() — happy path, null input, max value |

## Integration Tests
- INT-001: POST /api/foo → 201 + {id, ...}
- INT-002: GET /api/foo/:id with bad id → 404 + error envelope

## Phase Test Gates
| Phase   | Gate condition            | Command                            |
|---------|--------------------------|------------------------------------|
| Phase 1 | Unit tests pass          | npx vitest run tests/unit/         |
| Phase 2 | Integration tests pass   | npx vitest run tests/integration/  |
| Final   | E2E tests pass           | pnpm test:e2e                      |
```

### Phase 3 — speckit.clarify must produce

```markdown
## Edge Cases
- EDGE-001: <scenario that could fail> → <expected behavior>

## E2E Playwright Stubs
```typescript
// tests/e2e/feature-NNN.spec.ts
import { test, expect } from '@playwright/test'

test('<PATH-001: observable user journey>', async ({ page }) => {
  // arrange: <setup — auth, seed data, navigation>
  // act:     <user action — click, fill, submit>
  // assert:  await expect(page.getByRole('...')).toBeVisible()
  //          use getByRole/getByLabel/getByText — not CSS selectors or data-testid by default
})
```

## Test Contract Summary
| Layer             | Count | Status  |
|-------------------|-------|---------|
| BDD Scenarios     | N     | draft   |
| Unit test cases   | N     | listed  |
| Unit test files   | N     | mapped  |
| Integration tests | N     | defined |
| E2E paths         | N     | stubbed |
```

---

## TDD/BDD Best Practices

Research-backed. Sourced from Fowler, MSR 2026 (arXiv 2602.00409), arXiv 2411.04141.
See `docs/tdd-bdd-guide.md` for full citations and refuted claims.

### Foundation: write the test list first

Before writing any test, list ALL anticipated unit test cases for the feature.
Sequence them to drive quickly to design-critical paths.

> Fowler: "Writing out a test list first is a vital initial step. Sequencing the tests properly is a skill."

This is what the `## Unit Test List` section in Phase 2 captures.

### TDD: Red-Green-Refactor

Each cycle targets **one atomic behavior**. Each cycle completes in **a few minutes**.

```
RED      → Write ONE failing test. Must be red before any implementation.
GREEN    → Write MINIMUM code to pass. Hardcoding the result is legal.
REFACTOR → Clean structure. Remove duplication. DO NOT SKIP THIS STEP.
```

- **Most common TDD failure:** skipping the refactor step → "messy aggregation of code fragments" (Fowler)
- **Tests verify BEHAVIOR, not implementation.** A test that breaks when you rename a private method is testing the wrong thing.
- If a test was never red, you can't trust it catches the bug.

### BDD: Gherkin rules

- One `When` per scenario — multiple actions = multiple scenarios
- `Given` = precondition (state), never an action
- `Then` = what a non-technical stakeholder can observe
- Scenario titles = observable outcomes, not test case labels
- At minimum: one happy-path + one error-path per user story

### Mocking rules

Mock **only** at true system boundaries:
- External HTTP APIs
- Database — in unit tests only (integration tests use a real DB)
- System clock / randomness
- Filesystem — in unit tests only

**Never** mock the module under test. **Never** mock internal business logic.

### Agent over-mocking warning

AI coding agents add mocks to tests at **36% of commits** vs 26% for humans
(Hora & Robbes, MSR 2026, 1.2M commits — arXiv 2602.00409).
68% of repos with agent test activity contain agent mock activity.

Over-mocked tests pass while real integration paths fail — the suite is green but the product is broken.

**After every agent-generated test, check:**
1. Does this test call the real implementation, or mock the thing being tested?
2. Would this test catch an actual bug in the implementation?
3. Is each mock at a true system boundary (HTTP, DB, time) or inside business logic?

If any answer is wrong, rewrite the test before committing.

### Test pyramid

```
        ┌──────────────────────────┐
        │  E2E / BDD (Playwright)  │  few, slow — 1 per PATH-NNN
        ├──────────────────────────┤
        │  Integration (API/DB)    │  medium — 1 per INT-NNN boundary
        ├──────────────────────────┤
        │  Unit (TDD)              │  many, fast — 1 per atomic behavior
        └──────────────────────────┘
```

BDD scenarios → E2E + integration tests.
TDD red-green-refactor → unit tests.

---

## Implementation

```bash
SPEC_ARG="${ARGUMENTS:-}"
NO_CLARIFY=0
DRY_RUN=0
NO_PREFLIGHT=0   # v1.2.0: preflight is DEFAULT-ON (--no-preflight to skip)
NO_GRANT=0       # v1.2.0: autonomy-grant enumeration is DEFAULT-ON (--no-grant to skip)
NO_SWARM=0       # v1.2.0: pass-through to /spec-decompose (swarm default-on there)
SPEC_ID=""

# zsh-safe: parameter expansion does not word-split in zsh, but
# command-substitution output does (bash + zsh + dash) — no subshell,
# so assignments inside the loop persist.
for arg in $(printf '%s\n' "${SPEC_ARG}"); do
  case "$arg" in
    --no-clarify)   NO_CLARIFY=1 ;;
    --no-preflight) NO_PREFLIGHT=1 ;;
    --no-grant)     NO_GRANT=1 ;;
    --no-swarm)     NO_SWARM=1 ;;
    --dry-run)      DRY_RUN=1 ;;
    [0-9]*)         SPEC_ID="$arg" ;;
  esac
done

if [ $DRY_RUN -eq 1 ]; then
  echo "[feature-spec] --dry-run: would run speckit.specify → speckit.plan → speckit.clarify → spec-decompose → preflight → grant"
  echo "[feature-spec] Spec: ${SPEC_ID:-<new>}  preflight=$((1-NO_PREFLIGHT)) grant=$((1-NO_GRANT)) swarm=$((1-NO_SWARM))"
  exit 0
fi
```

---

**Claude: execute these steps in sequence. Do not skip any step.**

### Step 1 — speckit.specify

Invoke the `speckit.specify` skill via the Skill tool.
Pass the spec number or description from `$ARGUMENTS`.

After it completes, open `specs/${SPEC_ID}/spec.md` and verify:
- `## BDD Scenarios` — at least one `Given/When/Then` block with one happy-path + one error-path
- `## Acceptance Criteria` — numbered ACs (AC-001, AC-002, …)
- `## E2E Test Paths` — at least one PATH-NNN entry
- Each BDD scenario has exactly ONE `When` clause
- Each `Given` is a precondition (state), not an action
- Each `Then` is stakeholder-observable, not an internal assertion

If any check fails, **fix it now** before proceeding to Step 2.

### Step 2 — speckit.plan

Invoke the `speckit.plan` skill via the Skill tool.

After it completes, open `specs/${SPEC_ID}/plan.md` and verify:
- `## Unit Test List` — all anticipated test cases, sequenced by design-criticality
- `## TDD Unit Test Map` — table mapping source files to test files with atomic behaviors listed
- `## Integration Tests` — at least one INT-NNN entry per API/DB boundary
- `## Phase Test Gates` — test gate command per implementation phase

If any section is missing, **add it now** before proceeding to Step 3.

### Step 3 — speckit.clarify

Skip if `--no-clarify` was passed.

Invoke the `speckit.clarify` skill via the Skill tool.

After it completes, verify the spec contains:
- `## Edge Cases` — at least one EDGE-NNN entry
- `## E2E Playwright Stubs` — one `test()` stub per PATH-NNN, using `getByRole`/`getByLabel`/`getByText` (not raw CSS selectors)
- `## Test Contract Summary` — table with counts by layer

If any section is missing, **add it now**.

### Step 4 — spec-decompose (v1.2.0)

Invoke the `spec-decompose` skill via the Skill tool with `${SPEC_ID}` (append
`--no-swarm` if `NO_SWARM=1`). It runs the roster-specialist swarm decomposition
(orchestrator merge + `gates.py analyze` + `agents_manifest.py check` gates) and
writes `specs/${SPEC_ID}/tasks.md`.

If decompose fails its gates, STOP — fix the spec/plan and re-run. Do not hand a
failing tasks.md to preflight/grant.

### Step 5 — preflight (v1.2.0, DEFAULT — skip only with --no-preflight)

Requirements are proven NOW, while the operator is present — never discovered at 3am.

1. **Author the manifest** `specs/${SPEC_ID}/preflight.json` from the RUN's real
   footprint: scan tasks.md + plan.md for env/secret NAMES the tasks read
   (`process.env.*`, `os.environ`, `doppler secrets get` names) and every external
   service touched (DB, gateway, deploy target, MCP server) as a cheap real probe.
   Names only — a secret VALUE never enters the manifest. Format per the
   `/preflight` skill:

   ```json
   [
     {"kind": "env",   "name": "DATABASE_URL"},
     {"kind": "probe", "name": "db-reachable",
      "cmd": "psql \"$DATABASE_URL\" -c 'select 1' -qtA"}
   ]
   ```

2. **Run it, fail closed** (resolver inlined — do not assume `$GATES_PY` exists):

   ```bash
   GATES_PY=""
   for _cand in \
     "$(git rev-parse --show-toplevel 2>/dev/null)/packages/feature-fix-swarm/lib/gates.py" \
     "$HOME/.claude/lib/feature-fix-swarm/gates.py" \
     "$(git rev-parse --show-toplevel 2>/dev/null)/lib/gates.py"; do
     [ -f "$_cand" ] && GATES_PY="$_cand" && break
   done
   [ -z "$GATES_PY" ] && { echo "[feature-spec] FATAL: gates.py not found — run setup.sh"; exit 1; }
   # ledger key convention: ALWAYS bare numeric spec id (spec-057, never
   # spec-057-name) — feature-implement + task-swarm normalize the same way
   RUN_ID="spec-${SPEC_ID%%-*}"
   python3 "$GATES_PY" preflight "specs/${SPEC_ID}/preflight.json" --run "$RUN_ID" || {
     echo "[feature-spec] PREFLIGHT-FAIL — fix while the operator is present, then re-run"
     exit 1
   }
   ```

   Missing var → fetch from the secret manager now. Dead service → start/re-auth now.
   Re-run until `PREFLIGHT-PASS`. An unattended run later requires this recorded PASS
   (<24h) via `check-preflight`.

### Step 6 — autonomy grant (v1.2.0, DEFAULT — skip only with --no-grant)

The whole point of the pipeline is `/feature-implement ${SPEC_ID} --autonomous` not
stalling. Build the ledger NOW, one screen, one decision:

1. Walk tasks.md + plan.md and enumerate EVERY operator-gated action the run will
   perform, in typed `type:target` form (`push:origin/main`, `merge:pr`,
   `deploy:vercel-web`, `flip:FLAG`, `restart:svc`, `secret-use:NAME`,
   `migrate:desc`). Walk the artifacts — never enumerate from memory.
2. Present the list to the operator with a one-line rollback per action. Wait for
   explicit yes. (This is the ONE stop that buys the unattended night.)
3. Record:

   ```bash
   python3 "$GATES_PY" grant "$RUN_ID" --action <a1> --action <a2> ... --ttl-hours 24
   ```

If the operator declines some actions, grant the rest — the run will stop+record
`pending` only at the declined ones.

### Completion summary

Print:

```
✓ /feature-spec complete for spec NNN

Artifacts:
  specs/NNN/spec.md          — requirements + BDD scenarios + acceptance criteria + E2E paths
  specs/NNN/plan.md          — unit test list + TDD test map + integration tests + phase gates
  specs/NNN/tasks.md         — swarm-decomposed tasks with roster [agent:] tags + review-gates
  specs/NNN/preflight.json   — env/service manifest, PREFLIGHT-PASS recorded for run spec-NNN
  grant ledger               — N typed actions granted, TTL 24h (run spec-NNN)

Test contracts baked in:
  BDD Scenarios:     N defined  (pass = feature ships; one happy + one error per story)
  Unit test cases:   N listed   (write these RED before any implementation)
  Unit test files:   N mapped   (TDD target files)
  Integration tests: N cases    (API/DB boundary verification)
  E2E paths:         N stubbed  (Playwright stubs — fill selectors during impl)

Agent over-mocking check: verify no agent-generated test mocks the thing it's testing.
See docs/tdd-bdd-guide.md for full TDD/BDD reference.

Next:
  /feature-implement NNN --autonomous   — unattended: swarm impl → QA → review-gate → ship → canary
  /feature-implement NNN                — attended (operator prompts at gates)
```

(`/feature` is deprecated — this skill + `/feature-implement` replaced it.)
