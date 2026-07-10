---
name: testing-policy
description: "Canonical FFS testing doctrine — mock-minimization ladder, real-browser verification, console/network tripwires, independent test authorship, BDD-as-executable-input, coverage floor, smoke design. Other skills reference this instead of restating it."
version: "1.0.0"
allowed-tools:
  - Read
  - Bash
  - Grep
  - Glob
---

# testing-policy — one home for how FFS tests

## When to invoke

- Referenced by `/feature-spec`, `/spec-decompose`, `/feature-implement`, `/code-uplift`,
  `/fix` when writing or judging tests. Load it before authoring any test plan.
- Directly: "what's our testing policy", "how should this be mocked", "what does the
  browser gate require".

## Why this exists

The recurring failure is *green tests, broken browser*: manual testing keeps catching
issues automated tests should have caught. Root causes, in observed order: over-mocked
modules whose fake shapes drift from reality; jsdom silently dropping layout/CSS/
navigation; no real-browser gate in the agent loop; the agent that wrote the code also
grading it. Every rule below exists to close one of those.

## 1. Mock-minimization ladder (stop at the first rung that holds)

1. **Real thing.** Real DB (testcontainers / local Postgres / SQLite where the dialect
   allows), real filesystem in a tmpdir, real clock unless the test is about time.
2. **Own adapter over anything you don't own.** Third-party API/SDK → wrap in a thin
   adapter; tests mock YOUR adapter interface; a separate contract test exercises the
   adapter against the real service (or its sandbox) — "don't mock what you don't own".
3. **Network boundary only (MSW-style).** When HTTP must be faked, intercept at the
   network layer so the entire client stack (fetch wrapper, query cache, serialization)
   runs real. NEVER mock your own modules (`vi.mock('./api')` on first-party code is a
   policy violation — that fake shape is exactly what drifts).
4. **jsdom is for logic, not UI truth.** Component/integration tests that assert
   layout, visibility, hover/focus, navigation, or CSS run in a real browser
   (Playwright component tests / e2e). jsdom-only suites may not claim UI coverage.

## 2. Real-browser gate (fail-closed)

Any diff touching web surfaces must pass `scripts/gsd/canary-gate.sh` before ship:
a headless Canary session over the affected flows, gated mechanically on
`results.json`: `status=="passed"` AND `consoleErrors==0` AND `networkFailures==0`,
with staleness check against HEAD. No canary results on a web-touch diff = FAIL.
Zero console errors / zero failed network requests are the cheapest tripwires that
catch most "obviously broken" states — never waive them to make a run green.
Evidence ledger: `python3 lib/runtime_proof.py verify <bundle>` where a spec carries browser-proof
success criteria. Coverage second opinion: `scripts/gsd/qa-coverage-adversary.sh`
(cross-vendor, advisory) lists user-facing flows the QA session missed.

## 3. Independent test authorship

The agent that implemented a change does not author its acceptance/verification
tests. In gsd runs this is structural (executor vs nyquist-auditor vs verifier are
separate spawns) — keep it that way: never collapse test-writing into the executor's
plan "for speed". Same-author tests validate what the code *does*, not what it *should*.

## 4. BDD — executable input, not ceremony

Given/When/Then scenarios are written at spec time (`/feature-spec`) for user-facing
stories ONLY, and they earn their keep by being consumed: each scenario maps to a
Canary step (or Playwright e2e spec) at QA time. No Gherkin for internal/library code
nobody non-technical will read. Scenario names should be greppable from spec.md to the
step scripts that execute them.

## 5. Coverage floor + smoke

- Coverage floor **80%** (unit+integration, per repo rule); measure with the repo's
  real runner (`vitest run --coverage`, `pytest --cov`); never assert coverage from a
  narrowed test selection.
- Post-deploy smoke (≤60s): health endpoint + ONE real critical path (login or the
  product's money path) + the console/network tripwires. `/canary` runs it post-ship.

## 6. Test failures

Fix the implementation, not the test — unless the test asserts a stale contract, in
which case say so out loud in the commit body. Flake protocol: run alone, re-run the
group, clean tree, then name it flake or regression with the reason.

## Anti-patterns (reject in review)

- First-party module mocks (`vi.mock`/`jest.mock`/`monkeypatch` on own code) where a
  boundary intercept works.
- UI assertions in jsdom claiming browser truth.
- Tests asserting mock-call counts instead of observable behavior.
- Coverage runs scoped to the changed files only, presented as suite coverage.
- Sleeping/timeout-based e2e waits where a deterministic wait exists.
