---
phase: 01-levers
plan: 05
subsystem: infra
tags: [python, stdlib, harness-audit, preflight, advisory-scoring]

requires: []
provides:
  - "scripts/harness-audit.py — 0-100 harness health scorer with --json output"
  - "tests/test_harness_audit.py — pytest coverage for all 4 scoring dimensions + EDGE-007"
affects: [preflight, harness-hardening-phase-2]

tech-stack:
  added: []
  patterns:
    - "regex-based frontmatter reader reused from lib/agents_manifest.py (no PyYAML)"
    - "subprocess-driven pytest fixtures for CLI-contract end-to-end coverage"

key-files:
  created:
    - scripts/harness-audit.py
    - tests/test_harness_audit.py
  modified: []

key-decisions:
  - "CLI contract kept to exactly `harness-audit.py [--json]` per artifacts_produced — harness dir resolved via Path.home()/.claude (HOME env), packaged skills via cwd-relative skills/, config via cwd-relative .planning/config.json; no extra flags added"
  - "KNOWN_MODEL_ALIASES = {opus, sonnet, haiku} — matches the short-alias values actually used in .planning/config.json model_overrides across this repo (01-01-PLAN.md, RESEARCH)"
  - "Score deducts 10 points per finding, floored at 0 — simple linear scheme, no per-dimension weighting requested by the plan"

patterns-established:
  - "Advisory scorer pattern: always exit 0, absence of harness dir is a skip-note not a deduction (EDGE-007) — reusable for any future harness-health check"

requirements-completed: [REQ-08]

coverage:
  - id: D1
    description: "Dangling skill symlink detection — installed SKILL.md symlinked to a missing target scores a `dangling` finding and score < 100"
    requirement: "REQ-08"
    verification:
      - kind: unit
        ref: "tests/test_harness_audit.py::test_dangling_symlink_scores_below_100"
        status: pass
    human_judgment: false
  - id: D2
    description: "Vendored-copy version drift detection — installed vs packaged SKILL.md version mismatch scores a `drift` finding"
    requirement: "REQ-08"
    verification:
      - kind: unit
        ref: "tests/test_harness_audit.py::test_version_drift_reported"
        status: pass
    human_judgment: false
  - id: D3
    description: "Clean fixture scores 100 with empty findings and exit 0"
    requirement: "REQ-08"
    verification:
      - kind: unit
        ref: "tests/test_harness_audit.py::test_clean_fixture_scores_100"
        status: pass
    human_judgment: false
  - id: D4
    description: "--json output is a stable schema {score:int, findings:[{kind,path,detail}]}"
    requirement: "REQ-08"
    verification:
      - kind: unit
        ref: "tests/test_harness_audit.py::test_json_schema_stable"
        status: pass
    human_judgment: false
  - id: D5
    description: "No ~/.claude/skills present scores 100 with a `skipped: no harness dir` note — absence is not drift (EDGE-007)"
    requirement: "REQ-08"
    verification:
      - kind: unit
        ref: "tests/test_harness_audit.py::test_no_harness_dir_is_not_drift"
        status: pass
    human_judgment: false

duration: 25min
completed: 2026-07-10
status: complete
---

# Phase 01 Plan 05: Harness Audit Scorer Summary

**Standalone stdlib-only `scripts/harness-audit.py` scores the installed agent harness 0-100 across dangling skill symlinks, vendored-copy version drift, dead model pins, and unregistered-hook drift, with a `--json` CLI contract and an always-exit-0 advisory posture.**

## Performance

- **Duration:** 25 min
- **Started:** 2026-07-10T00:00:00Z (approx, see git log timestamps)
- **Completed:** 2026-07-10
- **Tasks:** 2/2 completed
- **Files modified:** 2 (both new)

## Accomplishments
- Built `scripts/harness-audit.py`: 100-point scorer, 4 dimensions (dangling links, version drift, dead model pins, unregistered hooks), `--json` output, always exits 0
- Built `tests/test_harness_audit.py`: 5 subprocess-driven pytest fixtures covering every `<behavior>` line in the plan (dangling, drift, clean, json schema, no-harness-dir/EDGE-007)
- Reused `lib/agents_manifest.py::_parse_frontmatter`'s regex approach verbatim (copied, since harness-audit.py is a standalone script with no import dependency on `lib/`) — zero new third-party dependencies
- Full suite green: 236 passed (231 baseline + 5 new), 0 failures

## Task Commits

Each task was committed atomically (TDD RED/GREEN):

1. **Task 1: RED — test_harness_audit.py fixtures** - `728c4d1` (test) — confirmed failing with script absent (exit 2), not a bats/pytest syntax error
2. **Task 2: GREEN — harness-audit.py scorer** - `c974664` (feat) — all 5 tests pass; full suite 236 passed

_No plan-metadata commit made by this executor — worktree mode excludes STATE.md/ROADMAP.md; orchestrator commits those centrally after merge._

## Files Created/Modified
- `scripts/harness-audit.py` - 0-100 scorer: `check_dangling_links`, `check_version_drift`, `check_dead_model_pins`, `check_hook_drift`, `run_audit`, `main` (argparse `--json` only)
- `tests/test_harness_audit.py` - 5 `@test`-equivalent pytest functions, each invoking the script as a subprocess with `HOME`/`cwd` fixture overrides

## Decisions Made
- CLI kept minimal: only `--json` flag, matching `<artifacts_produced>` contract literally. Harness dir, packaged-skills dir, and config path are NOT flags — they resolve from `HOME` env and `cwd`, so tests drive them via subprocess `env=`/`cwd=` instead of adding executor-only test hooks to the CLI surface.
- `KNOWN_MODEL_ALIASES = {"opus", "sonnet", "haiku"}` chosen as the recognized-alias set for dead-pin detection — this is the exact value space seen in `.planning/config.json` `model_overrides` and in the sibling 01-01-PLAN.md interfaces section. Not exhaustive of every possible Claude model ID, but matches this repo's actual usage; a `dead-pin` finding fires only for values outside this set (typos, deprecated hardcoded model IDs).
- Linear score deduction (10 points/finding, floor 0) — no weighting scheme requested by plan.md; simplest scheme that satisfies "clean=100" and "any finding <100".
- Chose subprocess invocation over importlib-by-path for ALL 5 tests (plan offered either) — the `--json` schema assertion needed subprocess per the plan's own guidance, and using one invocation style for every fixture keeps the test file uniform (YAGNI: no need for two loading mechanisms).

## Deviations from Plan

None - plan executed exactly as written. Both tasks' `<action>` and `<verify>` blocks were followed literally; no Rule 1-4 triggers encountered.

## Issues Encountered
None.

## User Setup Required

None - no external service configuration required. This is a standalone advisory script; preflight wiring is explicitly out of scope for this plan (Phase 2, per `<prohibitions>`).

## Next Phase Readiness

`scripts/harness-audit.py --json` is ready to be invoked from a future preflight advisory section (Phase 2) or any other consumer. No `skills/preflight/SKILL.md` was touched, no PyYAML was added (verified: `grep -in "pyyaml\|^import yaml\|from yaml" scripts/harness-audit.py` matches only a comment). REQ-08's "lever half" (the scorer itself) is fully satisfied; the preflight-wiring half remains for a later phase.

---
*Phase: 01-levers*
*Completed: 2026-07-10*
