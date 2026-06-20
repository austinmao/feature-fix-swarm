# TDD & BDD Guide

Research-backed reference. Sources: Martin Fowler bliki, arXiv MSR 2026 (2602.00409, 2411.04141), TestRail, Codecademy, enfuse-io. Claims verified adversarially — refuted claims excluded.

---

## TDD: Test-Driven Development

### Definition

Write tests FIRST, then use failing tests to drive design and implementation. Tests are not a verification layer — they are a design tool.

### Step 0: Write your test list

Before writing any test, list ALL anticipated test cases for the feature. Sequence them to hit design-critical paths first.

> Fowler: "Writing out a test list first is a vital initial step. Sequencing the tests properly is a skill."

This prevents tunnel vision on the first test you thought of and ensures coverage of edge cases up front.

### The Red-Green-Refactor Cycle

```
RED      → Write ONE failing test. Think WHAT to develop, not HOW.
GREEN    → Write MINIMUM code to pass. No optimization. Hardcoding legal.
REFACTOR → Clean up structure. Remove duplication. Tests stay green.
```

Each cycle targets **one atomic behavior** and should complete in **a few minutes**. If it's taking longer, the test scope is too large — split it.

**RED phase rules:**
- The test MUST fail before writing any implementation
- Running a test that was never red = no confidence it tests anything
- One failing test at a time

**GREEN phase rules:**
- Write the least code that makes the test pass — nothing more
- Hardcoding the expected return value is legal at this stage
- Resist the urge to design or optimize; that's the refactor's job

**REFACTOR phase rules:**
- This step is **NOT optional** — it's where design emerges
- Most common TDD failure: skipping refactor → "messy aggregation of code fragments" (Fowler)
- Run the test suite after every refactor change
- Refactor = improve structure, not add functionality

### What Tests Must Verify

**Test behavior, not implementation details.**

| Context | Wrong (implementation) | Right (behavior) |
|---------|----------------------|------------------|
| Unit | `assert this._count === 3` | `assert result === expected` |
| Service | `assert sendEmail.called` | `assert response.status === 200` |
| React | `assert state.isOpen === true` | `assert screen.getByRole('dialog')` |
| API | `assert db.save.calledWith(x)` | `assert GET /items returns the saved item` |

Reason: behavior-tests survive implementation refactors. Implementation-tests break on every refactor, making developers afraid to clean code — the opposite of what TDD is for.

---

## BDD: Behavior-Driven Development

### Definition

BDD extends TDD's intent to cross-functional teams. It expresses system behavior in plain language (Gherkin) accessible to developers, testers, AND non-technical business stakeholders.

**Key distinction from TDD:**
- TDD = unit-level, developer-facing, code-centric
- BDD = behavior-level, stakeholder-facing, language-centric

BDD is NOT a replacement for TDD. It operates at a higher level — BDD scenarios drive E2E and integration tests; TDD unit tests fill the base.

### Gherkin Given-When-Then

```gherkin
Feature: <feature name in plain English>

  Scenario: <observable behavior — stakeholder outcome, not a test case name>
    Given <system state BEFORE the action>
    When  <the ONE action the user takes or event that occurs>
    Then  <the observable outcome the stakeholder cares about>

  Scenario: <error path name>
    Given <state that leads to error>
    When  <the invalid action>
    Then  <the observable error response>
```

### Gherkin Rules (research-verified)

| Rule | Correct | Incorrect |
|------|---------|----------|
| One `When` per scenario | `When the user clicks Submit` | `When the user fills the form / And clicks Submit` → split into two scenarios |
| `Given` = precondition, not action | `Given the user is logged in` | `Given the user logs in` |
| `Then` = stakeholder-observable | `Then a confirmation email is sent` | `Then sendEmail() is called` |
| Scenario title = observable outcome | `User receives a confirmation email` | `Test email sending logic` |
| Cover both paths | One happy-path + one error-path per user story | Happy-path only |

### Stakeholder participation

Run a **"three amigos" session** (developer + tester + product) to write scenarios before implementation.

Practical note (arXiv 2411.04141): BDD adoption often falls short of prescribed stakeholder involvement in practice. Mitigate by:
- Keeping scenarios to ≤5 steps in plain business language
- Reviewing scenarios in sprint review, not just in code review
- Treating failing scenarios as product bugs, not just test failures

---

## TDD + BDD Together

```
BDD scenario (Given/When/Then)
  └─ E2E test (Playwright) — one test per PATH-NNN
       └─ Integration test — one per API/DB boundary (INT-NNN)
            └─ Unit tests (TDD red-green-refactor) — one per function
```

**Practical flow for a user story:**
1. Write BDD scenario → captures WHAT the feature must do for the stakeholder
2. Write E2E Playwright stub for each scenario path (PATH-NNN)
3. Write integration test for each API boundary (INT-NNN)
4. Write unit test list for every function that will be added or changed
5. TDD each function: red → green → refactor, one atomic behavior at a time
6. Integration tests go green
7. E2E tests go green → BDD scenario is fulfilled

### Test pyramid

```
        ┌──────────────────────┐
        │  E2E / BDD scenarios │  few, slow, high confidence
        │  (Playwright)        │  1 per user journey PATH
        ├──────────────────────┤
        │  Integration tests   │  medium count
        │  (API, DB, service)  │  1 per system boundary INT
        ├──────────────────────┤
        │  Unit tests / TDD    │  many, fast, cheap
        │  (functions, logic)  │  1 per atomic behavior
        └──────────────────────┘
```

---

## Anti-Patterns (research-verified)

| Anti-pattern | Why it fails |
|--------------|-------------|
| Skip the refactor step | Messy code accumulates; tests give false confidence (Fowler) |
| Test implementation details | Suite breaks on refactors; developers stop refactoring |
| Write tests after implementation | Tests are green from start; no confidence they catch bugs |
| Giant test cases (multiple behaviors) | Hard to locate failures; false coupling between behaviors |
| Tests that are never red | Can't tell if the test actually catches the target bug |
| Over-mocking (especially AI agents) | 36% of agent commits add mocks vs 26% human (MSR 2026) |
| Mocking the thing under test | Test verifies the mock, not the code |
| Gherkin `When` with multiple actions | Hides which action caused the outcome |
| `Then` that asserts internal state | Not stakeholder-observable; couples BDD to implementation |

---

## Agent Over-Mocking Warning

**Critical for AI-assisted TDD/BDD (MSR 2026, arXiv 2602.00409):**

> "36% of commits made by coding agents add mocks to tests, compared with 26% by non-agents. 68% of repositories with agent test activity also contain agent mock activity."
> — Hora & Robbes, *Are Coding Agents Generating Over-Mocked Tests?*, MSR 2026 (1.2M commits, 2,168 repos)

Over-mocked tests pass while real integration paths fail. This directly undermines TDD/BDD confidence — the test suite is green but the product is broken.

**After every agent-generated test, verify:**
1. Does this test call the real thing, or mock the thing being tested?
2. If the implementation had a bug, would this test catch it?
3. Is this mock at a true system boundary or in the middle of business logic?

**Mock only at true system boundaries:**
- External HTTP APIs (network)
- Database — in UNIT tests only; integration tests use a real DB
- System clock / randomness
- Filesystem — in unit tests only

Never mock the module you're testing. Never mock internal business logic.

---

## Sources

| Source | Confidence | Key finding |
|--------|-----------|-------------|
| Martin Fowler, [TestDrivenDevelopment](https://martinfowler.com/bliki/TestDrivenDevelopment.html) | Primary | Red-Green-Refactor definition; test list first; refactor anti-pattern |
| Hora & Robbes, [arXiv 2602.00409](https://arxiv.org/pdf/2602.00409) (MSR 2026) | Primary/peer-reviewed | Agent over-mocking: 36% vs 26%, 1.2M commits |
| [arXiv 2411.04141](https://arxiv.org/pdf/2411.04141) | Primary/peer-reviewed | BDD cross-functional collaboration; theory-vs-practice gap |
| TestRail, [TDD blog](https://www.testrail.com/blog/test-driven-development/) | Blog | TDD vs BDD scope/stakeholder distinction |
| enfuse-io, [Red-Green-Refactor](https://medium.com/enfuse-io/red-green-refactor-an-introduction-to-tdd-d71a7eca0645) | Blog | Behavior-not-implementation principle |
| cleancodeguy, [TDD](https://cleancodeguy.com/blog/tdd-red-green-refactor) | Blog | Atomic behavior per cycle; timing guidance |
