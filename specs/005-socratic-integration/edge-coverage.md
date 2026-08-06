# Edge coverage — spec 005

One line per applicable edge: `REQ · category · status · reason/AC`.

- REQ-01 · idempotency · resolved · re-run against existing dest refuses with exit 1, dest unchanged (BDD scenario 2; installer test)
- REQ-01 · concurrency · resolved · "destination appeared concurrently" guard copied from install-prompt-master.sh (:74) into new installer
- REQ-01 · empty · resolved · pin.json missing required key → read_pin python KeyError → nonzero exit before any clone (fail-closed)
- REQ-02 · idempotency · resolved · same-release re-install preserves manifest bytes — existing installer test pattern (test_same_release_project_reinstall_preserves_manifest_bytes) extended to socratic
- REQ-03 · empty · resolved · empty/missing `domains` list = malformed at authoring time → enum validation fails the step (fail-closed); hand-edited domains: [] at consumption yields the no-domains skip ONLY when the mode's emittable set is empty — plan mode may still arm on valid packs, verify mode on the ASSUME ledger (EDGE-001/002)
- REQ-03 · encoding · dismissed · frontmatter values are a closed ASCII enum; body freetext never parsed, only cat'd inside delimiters
- REQ-03 · ordering · resolved · slice emits domains in canonical NN-file order regardless of declaration order (deterministic output for byte-compare tests) — folded into slice unit tests
- REQ-04 · idempotency · dismissed · socratic.md regenerated whole-file per spec run; ASSUME ids only referenced within the same file and consumed atomically by the same-run verifier — no cross-run external refs to break
- REQ-05 · adjacency · resolved · two `specs/NNN-*` dirs sharing a prefix → `head -1` first match, matching review-gate's existing behavior; documented in plan
- REQ-05 · idempotency · resolved · socratic.md sha folded into PW_PLAN_SHA key when armed (plan Phase 3.6; unit test listed)
- REQ-05 · concurrency · dismissed · parallel plan-wall invocations share no new mutable state beyond today's; slice helper is read-only
- REQ-05 · empty · resolved · absent socratic.md/vendor/spec-dir → byte-identical prompt proven by `cmp` against captured baseline (AC-005/008, INT-003)
- REQ-07 · empty · resolved · zero ASSUME entries → verify-mode slice still emits Verification blocks; verifier instructed "0 assumptions recorded" — no fabricated verdicts; folded into socratic-review-gate.bats
- REQ-07 · ordering · resolved · verdicts emitted in ledger order (stable, diffable)
- REQ-09 · boundary · resolved · first two SURVIVING in-enum packs honored (enum filter then missing-file filter run before the cap); additional survivors skipped with warn (EDGE-007; unit test listed)
- REQ-12 · boundary · resolved · exactly one status line per successfully-parsed invocation (exit 0/3) even when warns also fire — warns are separate stderr lines, status line count asserted in bats; usage errors (exit 2) emit usage text only

Unresolved: none.
