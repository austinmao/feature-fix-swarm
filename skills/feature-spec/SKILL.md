---
name: feature-spec
description: "Spec-first pipeline: speckit.specify → speckit.plan → speckit.clarify, each phase enforcing TDD unit tests, BDD Given/When/Then scenarios, and E2E test stubs before any implementation begins."
version: 1.0.0
---

# /feature-spec [NNN | "description"]

Spec-first feature definition pipeline. Chains three speckit phases in sequence and
enforces that TDD unit tests, BDD behavior scenarios, and E2E test definitions are
present in every artifact before implementation begins.

## Why

Features built without upfront test contracts drift: unit tests get retrofitted, BDD
scenarios never get written, and E2E coverage is added last (or not at all). This
skill bakes test contracts into the spec so they gate implementation, not follow it.

## Pipeline

```
┌─────────────────────────────────────────────────────────────────┐
│  /feature-spec [NNN]                                            │
│                                                                 │
│  Phase 1: /speckit.specify                                      │
│    ├─ Writes specs/NNN/spec.md                                  │
│    ├─ ENFORCED: BDD scenarios (Given/When/Then per user story)  │
│    ├─ ENFORCED: Acceptance criteria (numbered, testable)        │
│    └─ ENFORCED: E2E test path definitions                       │
│                                                                 │
│  Phase 2: /speckit.plan                                         │
│    ├─ Writes specs/NNN/plan.md                                  │
│    ├─ ENFORCED: TDD unit test file map + function signatures    │
│    ├─ ENFORCED: Integration test cases                          │
│    └─ ENFORCED: Per-phase test gate commands                    │
│                                                                 │
│  Phase 3: /speckit.clarify                                      │
│    ├─ Resolves ambiguities, captures edge cases                 │
│    ├─ ENFORCED: E2E Playwright test stubs                       │
│    └─ ENFORCED: Test contract summary (counts by layer)         │
└─────────────────────────────────────────────────────────────────┘
```

> **Note on command naming:** speckit uses `speckit.specify` (not `speckit.spec`).
> Invoke via the Skill tool with name `speckit.specify`.

## Usage

```
/feature-spec NNN              # full pipeline: specify + plan + clarify
/feature-spec NNN --no-clarify # stop after plan (skip clarify phase)
/feature-spec NNN --dry-run    # preview what would be generated, no writes
```

## Required Sections Per Phase

Each phase MUST include the following sections. Verify their presence and add
them if speckit did not generate them.

### Phase 1 — speckit.specify must produce

```markdown
## BDD Scenarios

Feature: <feature name>

Scenario: <happy path name>
  Given <precondition>
  When  <action>
  Then  <expected outcome>

Scenario: <error case name>
  Given <error precondition>
  When  <invalid action>
  Then  <expected error outcome>

## Acceptance Criteria
- AC-001: <specific, testable, unambiguous criterion>
- AC-002: …

## E2E Test Paths
- PATH-001: <critical user journey in one sentence>
- PATH-002: …
```

### Phase 2 — speckit.plan must produce

```markdown
## TDD Unit Test Map
| Source file | Test file | Functions/methods to test |
|-------------|-----------|---------------------------|
| src/foo.ts  | tests/unit/foo.test.ts | doThing(), validateInput() |

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
- EDGE-002: …

## E2E Playwright Stubs
```typescript
// tests/e2e/feature-NNN.spec.ts
import { test, expect } from '@playwright/test'

test('<PATH-001 description>', async ({ page }) => {
  // arrange: <setup>
  // act:     <user action>
  // assert:  await expect(page.locator('...')).toBeVisible()
})
```

## Test Contract Summary
| Layer             | Count | Status  |
|-------------------|-------|---------|
| BDD Scenarios     | N     | draft   |
| Unit test files   | N     | mapped  |
| Integration tests | N     | defined |
| E2E paths         | N     | stubbed |
```

## TDD/BDD Best Practices

### TDD (Test-Driven Development)
- Write the test file skeleton BEFORE any implementation file
- Each unit test targets one function — no multi-concern tests
- Test boundary behavior: nulls, empty inputs, max values, concurrent calls
- Use the AAA pattern: **Arrange** → **Act** → **Assert**

### BDD (Behavior-Driven Development — Gherkin)
- One scenario per distinct user outcome — not per code path
- `Given` = system state before the action
- `When` = the single user action being tested
- `Then` = observable outcome (UI change, API response, DB state)
- Write at least one happy-path scenario and one error scenario per user story
- BDD scenarios become the acceptance test suite: if all scenarios pass, the feature ships

### E2E tests (Playwright)
- Map one `test()` per PATH-NNN defined in the spec
- Stubs are sufficient at spec time — fill in selectors during implementation
- Always test the full user journey, not just the happy path
- Use `data-testid` attributes for stable selectors; avoid CSS class or position selectors
- Test auth-gated flows: include login/session setup in the test

## Implementation

```bash
SPEC_ARG="${ARGUMENTS:-}"
NO_CLARIFY=0
DRY_RUN=0
SPEC_ID=""

for arg in $SPEC_ARG; do
  case "$arg" in
    --no-clarify) NO_CLARIFY=1 ;;
    --dry-run)    DRY_RUN=1 ;;
    [0-9]*)       SPEC_ID="$arg" ;;
  esac
done

if [ $DRY_RUN -eq 1 ]; then
  echo "[feature-spec] --dry-run: would run speckit.specify → speckit.plan → speckit.clarify"
  echo "[feature-spec] Spec: ${SPEC_ID:-<new>}"
  exit 0
fi
```

---

**Claude: execute these steps in sequence. Do not skip any step.**

### Step 1 — speckit.specify

Invoke the `speckit.specify` skill via the Skill tool.
Pass the spec number or description from `$ARGUMENTS`.

After it completes, open `specs/${SPEC_ID}/spec.md` and verify these sections exist:
- `## BDD Scenarios` — at least one `Given/When/Then` block
- `## Acceptance Criteria` — numbered ACs (AC-001, AC-002, …)
- `## E2E Test Paths` — at least one PATH-NNN entry

If any section is missing, **add it now** before proceeding to Step 2.

### Step 2 — speckit.plan

Invoke the `speckit.plan` skill via the Skill tool.

After it completes, open `specs/${SPEC_ID}/plan.md` and verify:
- `## TDD Unit Test Map` — table mapping source files to test files with function names
- `## Integration Tests` — at least one INT-NNN entry
- `## Phase Test Gates` — test gate per implementation phase with the exact command to run

If any section is missing, **add it now** before proceeding to Step 3.

### Step 3 — speckit.clarify

Skip this step if `--no-clarify` was passed.

Invoke the `speckit.clarify` skill via the Skill tool.

After it completes, verify the spec contains:
- `## Edge Cases` — at least one EDGE-NNN entry
- `## E2E Playwright Stubs` — at least one `test()` stub with a comment plan
- `## Test Contract Summary` — table with counts by layer

If any section is missing, **add it now**.

### Completion summary

Print:

```
✓ /feature-spec complete for spec NNN

Artifacts:
  specs/NNN/spec.md  — requirements + BDD scenarios + acceptance criteria + E2E paths
  specs/NNN/plan.md  — architecture + TDD unit test map + integration tests + phase gates

Test contracts baked in:
  BDD Scenarios:     N defined  (acceptance tests — pass = feature ships)
  Unit test files:   N mapped   (TDD — write tests before implementing)
  Integration tests: N cases    (API/DB boundary tests)
  E2E paths:         N stubbed  (Playwright stubs in spec.md, fill selectors during impl)

Next:
  /feature NNN           — full pipeline (autoplan → implement → qa → codex-gate → ship)
  /spec-decompose NNN    — break plan.md into tasks.md first
```
