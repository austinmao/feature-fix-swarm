---
phase: 01-levers
plan: 06
subsystem: testing
tags: [python, pytest, gates, findings-queue, sha256, hashlib, cli]

# Dependency graph
requires: []
provides:
  - "`findings-queue add|list|resolve` CLI subcommand family in lib/gates.py"
  - "persistent findings store (top-level `findings` key in evidence.json) with stable full-sha256 signatures and dedup"
affects: [review-gate, fix-cycles]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Additive store namespace on the shared evidence.json (like `_autonomy`), reusing _load_store/_save_store/_StoreLock verbatim"
    - "Structured json.dumps([file, normalized_issue]) hash input to avoid delimiter-injection collisions; full (untruncated) sha256 hex digest"
    - "Dedup flag computed and returned from inside the locked read-modify-write, not a separate pre-read"

key-files:
  created:
    - lib/tests/test_findings_queue.py
  modified:
    - lib/gates.py

key-decisions:
  - "findings_resolve raises no write on an unknown sig (returns False) — CLI maps this to stderr + exit 1, matching the unknown-signature contract in plan.md/adversary F4"
  - "Schema conflict (`findings` key present but not a list) is surfaced via SystemExit from `_findings_ns`, caught in the CLI dispatch arm and mapped to exit 3 — keeps the shared exception path out of the library functions' return contracts"

patterns-established:
  - "Shape-guarded namespace helper (`_findings_ns`) as the reusable pattern for adding new top-level keys to the shared evidence store without an argparse subparser"

requirements-completed: [REQ-06]

coverage:
  - id: D1
    description: "findings-queue add <file> <issue> returns a stable full-sha256 signature over a structured [file, normalized_issue] encoding, stored under a findings key in the evidence store"
    requirement: "REQ-06"
    verification:
      - kind: unit
        ref: "lib/tests/test_findings_queue.py#test_add_list_resolve_function_lifecycle"
        status: pass
      - kind: unit
        ref: "lib/tests/test_findings_queue.py#test_signature_stability_whitespace_and_case_normalized"
        status: pass
      - kind: unit
        ref: "lib/tests/test_findings_queue.py#test_signature_delimiter_injection_distinct"
        status: pass
    human_judgment: false
  - id: D2
    description: "findings_add returns (sig, deduped) computed atomically inside the store lock; re-adding an existing signature dedupes to one entry"
    requirement: "REQ-06"
    verification:
      - kind: unit
        ref: "lib/tests/test_findings_queue.py#test_dedup_flag_from_locked_operation_not_a_preread"
        status: pass
      - kind: integration
        ref: "lib/tests/test_findings_queue.py#test_cli_full_lifecycle_add_list_resolve_dedup"
        status: pass
    human_judgment: false
  - id: D3
    description: "findings-queue list [--unresolved] returns queued findings as JSON; resolve <sig> marks a signature resolved and errors nonzero on an unknown sig"
    requirement: "REQ-06"
    verification:
      - kind: integration
        ref: "lib/tests/test_findings_queue.py#test_cli_full_lifecycle_add_list_resolve_dedup"
        status: pass
      - kind: unit
        ref: "lib/tests/test_findings_queue.py#test_findings_resolve_unknown_sig_returns_false"
        status: pass
      - kind: integration
        ref: "lib/tests/test_findings_queue.py#test_cli_resolve_unknown_sig_nonzero_exit_with_stderr"
        status: pass
    human_judgment: false
  - id: D4
    description: "Schema guard: a non-list `findings` store key (task-id collision) errors clearly on all three subcommands instead of being clobbered; unrelated store keys (_autonomy, gate) survive every write; concurrent add+resolve is race-free"
    requirement: "REQ-06"
    verification:
      - kind: integration
        ref: "lib/tests/test_findings_queue.py#test_schema_conflict_guard_never_clobbers"
        status: pass
      - kind: unit
        ref: "lib/tests/test_findings_queue.py#test_unrelated_store_keys_untouched"
        status: pass
      - kind: unit
        ref: "lib/tests/test_findings_queue.py#test_concurrent_add_and_resolve_no_lost_update"
        status: pass
    human_judgment: false

duration: ~20min
completed: 2026-07-10
status: complete
---

# Phase 01 Plan 06: Findings Queue Summary

**`findings-queue add|list|resolve` in lib/gates.py — full-sha256 structured signatures over [file, normalized_issue], atomic dedup under the existing store lock, schema-guarded additive namespace on evidence.json**

## Performance

- **Duration:** ~20 min
- **Completed:** 2026-07-10
- **Tasks:** 2 (TDD: RED, GREEN)
- **Files modified:** 2 (1 created, 1 modified)

## Accomplishments
- Added `findings_add` / `findings_list` / `findings_resolve` + `_normalize` / `_findings_ns` to `lib/gates.py`, reusing `_load_store`/`_save_store`/`_StoreLock`/`_now` verbatim — no second store, no new lock.
- Added a `findings-queue` CLI dispatch arm (`add|list|resolve`) with pinned exit codes: 0 ok, 1 unknown sig, 2 usage error, 3 schema conflict.
- Signature = full sha256 hex of `json.dumps([file, normalized_issue])` — structured encoding closes the delimiter-injection collision (adversary F1); no truncation.
- `findings_add` returns `(sig, deduped)` computed inside `with _StoreLock(store):` — dedup outcome is atomic with the write (adversary F2).
- `_findings_ns` shape guard: a pre-existing `findings` key that isn't a list raises before any write, so a colliding task-id entry is never clobbered (adversary F7); the CLI dispatch catches this `SystemExit` and maps it to exit 3.
- 14 new pytest tests: function-level lifecycle, full subprocess CLI lifecycle across all three dispatch branches (PATH-004), signature stability + delimiter-injection + reworded-issue + cross-file distinctness (EDGE-004), dedup atomicity, unknown-sig error (function + subprocess), usage errors (missing operand, unknown sub-verb), schema-conflict guard (byte-identical store after), store-key isolation (`_autonomy`/task-id entries untouched), and `threading.Barrier`-synchronized concurrent add+resolve (adversary F5).
- Full suite: 245 passed, 0 failed (231 baseline + 14 new) — `verify_done`/`run_gate`/`_StoreLock` bodies untouched (confirmed via diff: zero deletions in `lib/gates.py`).

## Task Commits

Each task was committed atomically:

1. **Task 1: RED — test_findings_queue.py** - `ac0471a` (test)
2. **Task 2: GREEN — findings_add/findings_list/findings_resolve + CLI arm** - `2e8e957` (feat)

_TDD gate sequence confirmed: `test(01-06): ...` then `feat(01-06): ...` in git log._

## Files Created/Modified
- `lib/tests/test_findings_queue.py` - 14 new pytest tests covering the full behavior contract (lifecycle, signature encoding, dedup atomicity, errors, schema guard, isolation, concurrency)
- `lib/gates.py` - `_normalize`, `_findings_ns`, `findings_add`, `findings_list`, `findings_resolve` functions + `findings-queue` CLI dispatch arm (additive only, near `record_pending`)

## Decisions Made
- Schema-conflict detection uses `SystemExit` raised from the shared `_findings_ns` helper, caught only at the CLI dispatch boundary and mapped to exit code 3 — keeps the library-level function signatures (`tuple[str, bool]`, `list`, `bool`) simple for programmatic callers, while the CLI still surfaces a clear stderr message and nonzero exit.
- `findings_list` reads the store directly (no lock) since it never writes — consistent with existing read-only helpers like `list_pending`/`check_grant`.

## Deviations from Plan

None - plan executed exactly as written. All ten behaviors from `<behavior>` in Task 1 were covered by name; all `<action>` requirements in Task 2 (function signatures, exit codes, `<50`-line functions, no new imports, no `_StoreLock` edits) were implemented as specified.

## Issues Encountered
None.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- `findings-queue` is ready for `/review-gate` and fix-cycle integration to persist and dedup findings across re-runs (US4).
- Store schema (`findings` list of `{sig, file, issue, resolved, recorded_at}`) is stable and additive; no migration needed for existing evidence.json files (missing key defaults to `[]` via `setdefault`).
- No blockers for downstream plans in this wave.

---
*Phase: 01-levers*
*Completed: 2026-07-10*
