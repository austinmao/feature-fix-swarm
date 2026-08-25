# Edge coverage — spec 010 (decompose Step 2.5 edge-probe, 2026-08-10)

One line per applicable edge: `REQ · category · status · reason/AC`.

- REQ-01 · boundary · resolved · deadline exactly reached with all-pass on same poll → rc 0 wins (final poll runs before deadline check); test in plan-wall-await.bats
- REQ-01 · adjacency · resolved · record whose sha matches but run_id differs = pending (EDGE-002); both must match
- REQ-01 · empty · resolved · empty plan set rc 1 typed (EDGE-011, AC-001)
- REQ-01 · idempotency · resolved · read-only proven byte-identical round counter (AC-001 test)
- REQ-01 · concurrency · resolved · record written mid-poll → next poll sees it (atomic write side); plan set pinned at start (EDGE-010)
- REQ-02 · empty · resolved · corrupt/truncated JSON → validate rc 1, reconciler skips typed (EDGE-003)
- REQ-02 · boundary · resolved · budget at 0 → transition to failed + typed line (AC-006); budgets never negative
- REQ-02 · idempotency · resolved · same-state transition (waiting→waiting refresh) allowed only via checkpoint verb; transition verb rejects no-op transitions typed — AC folded into lifecycle.bats
- REQ-02 · concurrency · resolved · two writers → atomic tmp+mv last-write-wins, never torn (unit test)
- REQ-04 · boundary · resolved · rc exactly 124 vs 125 (timeout vs cannot-run) — only 124 is the timeout shape; 125+ takes the zero-commit path; noted for implementation
- REQ-04 · idempotency · resolved · durable counter spans resumes (AC-004)
- REQ-04 · concurrency · dismissed · single gsd-run process owns the drive; coord claim already excludes a second runner (spec-009)
- REQ-05 · boundary · resolved · reset == now → wake_at = now, no rollover (EDGE-005); reset > cap → capped (AC-005)
- REQ-05 · encoding · resolved · strict HH:MM + am/pm/tz digit-anchored validation before arithmetic (AC-005, eng H3); non-ASCII banner text never reaches arithmetic
- REQ-05 · empty · resolved · empty capture / rc 0 drive → no checkpoint (EDGE-006)
- REQ-05 · idempotency · resolved · wake attempts durable, cap 4 (EDGE-012)
- REQ-06 · idempotency · resolved · pass is idempotent: satisfied-and-relaunched record is now running (not waiting) — second pass skips it; claim race typed rc 0 (EDGE-004)
- REQ-06 · concurrency · resolved · coord claim; loser exits typed (AC-006)
- REQ-06 · empty · resolved · no lifecycle records → pass exits 0 typed nothing-to-do
- REQ-07 · boundary · resolved · rerun counter at exactly 2 → exhausted path (EDGE-007)
- REQ-07 · adjacency · resolved · same run-id, attempt unchanged → no reclassification (AC-007, eng M1)
- REQ-07 · concurrency · resolved · second run on same branch → pinned databaseId unaffected (AC-007)
- REQ-08 · boundary · resolved · exactly 300 lines → rc 0 (limit is inclusive); 301 → rc 1; both tested
- REQ-08 · empty · resolved · empty phase dir rc 1 typed (AC-008)
- REQ-08 · ordering · dismissed · violation list order = glob order; stability irrelevant to the gate's rc contract
- REQ-11 · empty · dismissed · docs page — no data shape
- REQ-12 · idempotency · resolved · stall-free run byte-identical gate behavior (AC-012, suite-delta proof)

No unresolved edges. Categories not listed per REQ were inapplicable by shape
(pure-text/doc requirements probed only where a data shape intersects).
