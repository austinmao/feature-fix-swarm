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
- REQ-05 · adjacency · resolved · two `specs/NNN-*` dirs sharing a prefix → plan-wall's resolution sorts before taking `head -1`, so the accepted first-match behavior is LEXICALLY first and identical on every machine, not readdir-ordered; this is a scoped divergence from skills/review-gate/SKILL.md:430's unsorted `find | head -1` pipeline, deliberately left unchanged because altering a second consumer's resolution is outside REQ-05
- REQ-05 · idempotency · resolved · socratic.md sha folded into the LOCAL `sha` variable in `_pw_dispatch_path` when armed (plan Phase 3.6; unit test listed)
- REQ-05 · idempotency · accepted · the fold covers socratic.md's sha ONLY, so a vendor pin bump moves the slice's content without invalidating the key; AC-005 scopes invalidation to editing socratic.md, a pin bump is a reviewed change with its own re-review runbook, and hashing the whole vendor tree on every wall run would charge every run for an event that happens about once per pin
- REQ-05 · concurrency · dismissed · parallel plan-wall invocations share no new mutable state beyond today's; slice helper is read-only
- REQ-05 · empty · resolved · absent socratic.md/vendor/spec-dir → byte-identical prompt proven by `cmp` against captured baseline (AC-005/008, INT-003)
- REQ-05 · boundary · resolved · GSD_RUN_ID being set short-circuits the branch-NNN extraction's RUN_ID derivation, so the extraction is hoisted above the RUN_ID conditional to run unconditionally — arming works under the feature-implement caller, which always exports GSD_RUN_ID — while RUN_ID's own derivation semantics are unchanged
- REQ-06 · empty · resolved · the plan-decompose Step 3 seam is LLM-followed prose, not a script, so "byte-identical when the helper is silent" means identical INSTRUCTED behavior; its evidence is grep-pin ordering assertions (tests/bats/int-plan-wall-skill.bats's pattern), not prompt byte-comparison
- REQ-07 · empty · resolved · zero ASSUME entries → verify-mode slice still emits Verification blocks; verifier instructed "0 assumptions recorded" — no fabricated verdicts; folded into socratic-review-gate.bats
- REQ-07 · ordering · resolved · verdicts emitted in ledger order (stable, diffable)
- REQ-09 · boundary · resolved · first two SURVIVING in-enum packs honored (enum filter then missing-file filter run before the cap); additional survivors skipped with warn (EDGE-007; unit test listed)
- REQ-12 · boundary · resolved · exactly one status line per successfully-parsed invocation (exit 0/3) even when warns also fire — warns are separate stderr lines, status line count asserted in bats; usage errors (exit 2) emit usage text only

Unresolved: none.
