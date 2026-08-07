# Edge coverage — spec 009 (Step 2.5 edge-probe, 2026-08-07)

One line per applicable edge: `REQ · category · status · reason/AC`.

- REQ-01 · concurrency · resolved · the requirement IS the concurrency contract; AC-001 race harness (20 reps, real processes)
- REQ-01 · idempotency · resolved · same-UUID re-claim exit 0 (REQ-03)
- REQ-02 · concurrency · resolved · store-dir creation race: coord.py uses mkdir -p (EEXIST-tolerant) then flock on registry; folded into Phase 1 tests
- REQ-02 · empty · resolved · absent store → created on first use; guard treats no-registry as pass (bash fast-path)
- REQ-03 · empty · resolved · no FFS_COORD_SESSION and no run-state → mint new UUID + identity file; documented in docs/coordination.md
- REQ-03 · encoding · dismissed · UUID4 hex, ASCII by construction
- REQ-04 · boundary · dismissed · generation is a Python int — no overflow boundary exists
- REQ-04 · precision · resolved · staleness comparison contract: reclaim requires stale marker AND (pid dead OR start-token mismatch); exact-threshold tick belongs to filelock's owner_is_stale; our tests pin the pid/start-token half
- REQ-04 · concurrency · resolved · reclaim vs heartbeat renewal serialized by the registry write-lock txn; two racing reclaimers → one wins (unit test)
- REQ-05 · ordering · **unresolved → explicit assumption** · no waiter queue/fairness in v1: acquire is non-blocking (exit 3); a stream of shared holders can starve an exclusive waiter. Documented limitation in docs/coordination.md; `--wait` backoff recorded as follow-up. Rides into plan for plan-checker adjudication.
- REQ-05 · adjacency · resolved · same-session duplicate shared acquire → idempotent
- REQ-05 · empty · resolved · release with nothing held → idempotent exit 0
- REQ-06 · encoding · resolved · paths NFC-normalized before compare (APFS NFD variance); added to Phase 2 unit tests
- REQ-06 · empty · resolved · empty path / empty pattern → validation error at acquire
- REQ-06 · boundary · resolved · `<prefix>/**` matches strictly UNDER prefix; the literal prefix path itself requires the exact form; specified in docs + unit test
- REQ-07 · empty · resolved · zero leases → guard exit 0 via fast-path
- REQ-08 · concurrency · resolved · guard reads registry via atomic-replace snapshot — never a torn read
- REQ-09 · boundary · resolved · latency asserted with 2× margin in bats (20-rep median) to avoid CI flake; budgets are p95 targets, gate asserts p50 < budget
- REQ-10 · empty · resolved · empty diff → check passes (unit test)
- REQ-10 · adjacency · dismissed · rename of a forbidden file still appears in name-status diff → caught
- REQ-11 · concurrency · resolved · EXIT-trap release racing a reclaimer: release carries generation; stale release refused (REQ-05)
- REQ-11 · idempotency · resolved · double release idempotent
- REQ-12 · boundary · resolved · floor is >=3.30 (3.30.0 accepted); probe asserts import + SoftFileLease presence, not version string parsing
- REQ-13 · boundary · resolved · baselines recorded at preflight (pytest 171 / bats current) and diffed at Final gate

Summary: 23 applicable edges → 21 resolved, 2 dismissed (with reasons),
1 unresolved carried as explicit assumption (REQ-05 fairness — v1 limitation,
follow-up recorded).
