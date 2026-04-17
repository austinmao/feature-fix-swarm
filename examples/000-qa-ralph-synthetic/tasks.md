# Tasks — 000 QA Ralph Synthetic Test

## Phase 1: Setup (should pass)

- [X] T001 [model:haiku thinking:low] [qa:review] Create a utility function `utils/add.ts` that exports `add(a: number, b: number): number`
- [X] T002 [model:haiku thinking:low] [P] [qa:review] Write unit test for `add()` in `utils/add.test.ts`

## Phase 2: Deliberate failure (exercises retry loop)

- [ ] T003 [model:haiku thinking:low] [qa:review,security] Create `utils/divide.ts` that exports `divide(a, b)` — intentionally missing zero-division guard
  Depends-on: T001
- [ ] T004 [model:haiku thinking:low] [P] [qa:review] Write unit test for `divide()` including zero-division case — this SHOULD fail against T003's implementation
  Depends-on: T003

## Phase 3: Dependency chain (tests stop-the-line)

- [ ] T005 [model:sonnet thinking:med] [qa:review,security] Create `utils/calculator.ts` that imports add + divide — should not execute if Phase 2 QA fails
  Depends-on: T003, T004
