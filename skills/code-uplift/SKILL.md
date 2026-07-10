---
name: code-uplift
description: "Review + refactor code that's already written — same gsd machinery as feature-spec/feature-implement but findings-driven instead of spec-driven. Adversarial cross-model review, refactor phases, test uplift to the coverage floor, smoke/e2e via canary-gate, review-gate finish tail."
version: "1.1.0"
allowed-tools:
  - Read
  - Write
  - Bash
  - Glob
  - Grep
  - Skill
  - Agent
---

# code-uplift — review, refactor, and test-uplift existing code

## When to invoke

- "review and refactor <path/module>", "uplift tests for X", "harden this code",
  "bring <dir> to coverage floor", "clean up module Y and its tests"
- After inheriting/adopting code that never went through `/feature-spec` →
  `/feature-implement`
- NOT for new features (use `/feature-spec`) and NOT for a single named bug (use `/fix`)

## Relationship to other skills

```
/feature-spec        — spec-driven: requirements come from an interview
/code-uplift         — findings-driven: requirements come from reviewing existing code
/feature-implement   — both hand off to the same gsd execute loop
/fix                 — single-bug surgical path
testing-policy       — the doctrine every test decision here follows (load it first)
```

## Invocation

```
/code-uplift <path|module>                 # review + refactor + test uplift
/code-uplift <path> --review-only         # findings ledger only, no changes
/code-uplift <path> --no-refactor         # test uplift only (keep code shape)
/code-uplift <path> --coverage-floor 90   # override the 80% default
/code-uplift <path> --autonomous          # headless drive incl. finish tail
/code-uplift <path> --slop-only <diff-base>  # deslop fast path, see Step 0b
```

## Workflow

### Step 0: Resolve target + baseline

Resolve `$TARGET` (must exist; exit 1 otherwise). Record the baseline BEFORE any
change — you cannot claim "no regressions" without it:

```bash
git rev-parse HEAD                                  # base commit
# real suite + real coverage number for $TARGET (runner auto-detect:
# package.json → vitest/jest; pyproject/pytest.ini → pytest --cov)
```

Log: `[code-uplift] baseline: <pass/fail counts> coverage=<N>% base=<sha>`.

### Step 0b: --slop-only fast path (optional)

`/code-uplift <path> --slop-only <diff-base>` — a green-baseline-walled,
deletion-first, diff-scoped cleanup pass that runs BEFORE `/review-gate` sees
the diff. Not a new script — a documented invocation mode of this skill.

1. **Green-baseline WALL:** run the full suite first. If it is NOT green,
   REFUSE to edit and report the failing baseline — do not attempt cleanup on
   unproven code.
2. **Deletion-first:** prefer removing dead/redundant code over rewriting it.
3. **Diff-only scope:** limited to the files present in `git diff <diff-base>`
   — never touch a file outside that diff.
4. Re-run the full suite AFTER; report the net line delta (success = a
   strictly smaller / non-positive delta).
5. **EDGE-008:** `<diff-base>` == HEAD (empty diff) is a no-op success,
   reporting `0 files in scope`.

### Step 1: Review sweep (parallel, cross-model)

Dispatch in ONE message (explicit model pins; scout/deep return contracts per
`.claude/rules/common/agents.md`):

1. **opus code reviewer** — correctness/security/maintainability findings on
   `$TARGET`, severity-tagged CRITICAL/HIGH/MEDIUM/LOW, `file:line` anchored.
2. **Cross-vendor adversary** — opposite-CLI review of the same files (host-aware,
   same convention as `/review-gate`: claude host → `codex exec` gpt-5.6-sol xhigh;
   codex host → `claude -p --model opus`). Findings, not prose.
3. **sonnet test auditor** — judge the EXISTING tests against `testing-policy`:
   first-party mocks, jsdom-claiming-UI-truth, mock-call-count assertions,
   coverage gaps per file, missing e2e for user-facing flows.
4. **haiku dead-code scout** — unused exports/files/deps in `$TARGET` (candidates
   only; deletion decisions stay with the opus reviewer).

Merge into `specs/NNN-uplift-<slug>/REVIEW.md` (next free NNN): deduped findings
table + per-file coverage + test-policy violations. `--review-only` stops here.

### Step 2: Seed gsd project from findings

Follow `/spec-decompose` Step 2 verbatim (same `.planning/` seeding, same
`templates/gsd-config.base.json` copy, same `model-fallback.sh` +
`security-model-fence.sh` runs, same `gsd-test-command` requirement) with these
substitutions:

- REQUIREMENTS.md: one `REQ-NN` per CRITICAL/HIGH finding + one per test-policy
  violation class + `REQ` for "coverage(`$TARGET`) ≥ floor" + `REQ` for
  "smoke/e2e exists for each user-facing flow in `$TARGET`". MEDIUM/LOW → only
  when cheap to bundle; note the rest as non-goals.
- ROADMAP.md phases, in this order (later phases depend on earlier ones being green):
  1. **fix-critical** — CRITICAL/HIGH correctness+security findings (TDD: failing
     test reproducing each finding first)
  2. **refactor** — structure/duplication/size findings; behavior-preserving,
     characterization tests before moving code (skipped under `--no-refactor`)
  3. **test-uplift** — rewrite policy-violating tests (mock ladder!), fill coverage
     to the floor; test authorship by a different agent than whoever changed the
     code in phases 1-2 (testing-policy §3)
  4. **e2e-smoke** — BDD scenarios for user-facing flows → Canary steps;
     `scripts/gsd/canary-gate.sh` as a literal ROADMAP success criterion when
     `$TARGET` is web-touching
- Success criteria are commands that exit 0, including the coverage floor check
  and (web-touch) the canary gate.

### Step 3: Execute

Same as `/feature-implement` — for `--autonomous`, FIRST reuse its complete
Steps 1-3 setup verbatim (fresh `/preflight` PASS, `GSD_RUN_ID`
derivation/export, autonomy-grant ledger checks); THEN its Steps 4-5 execute
loop: `/gsd-plan-phase N` → `/gsd-execute-phase N` per phase (headless:
`scripts/gsd/gsd-run.sh`), plan-bounce adversary fires on high-blast plans
automatically, gates.py is completion authority. An unattended uplift without
those walls is a policy violation. Re-run the FULL baseline suite after each
phase and report the delta against Step 0's numbers.

### Step 4: Finish tail (default; `--no-finish` opts out)

Identical to `/feature-implement` Step 6: canary-gate + qa-coverage-adversary
(web-touch) → `/review-gate` → ship (grant-walled) → `/canary`, including the
fail-soft `scripts/gsd/learnings-harvest.sh` learnings step (always exits 0,
never blocks ship — AC-003).

### Step 5: Report

Baseline vs final: findings closed by severity, coverage before→after vs floor,
tests rewritten (policy violations closed), e2e/smoke added, suite delta
("baseline N failing {…} → still N failing {…}"), consumed grants, delegation
histogram.

## Non-goals

- New features or behavior changes beyond what a finding demands
- Fixing findings in files outside `$TARGET` (record as follow-ups in REVIEW.md)
- Committing/pushing outside the grant-walled finish tail
