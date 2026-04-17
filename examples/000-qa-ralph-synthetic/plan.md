# Synthetic QA Ralph Loop Test Spec

Purpose: Exercise the QA Ralph loop with deterministic pass/fail scenarios.

## Design

3 phases, 5 tasks. Designed to test:
- Phase 1: All tasks pass, QA passes → normal flow
- Phase 2: Task fails vitest deliberately → retry loop exercises
- Phase 3: Depends on Phase 2 → tests stop-the-line behavior
