# Engineering Test Plan

This plan supplements the 58 unit, 16 integration and 24 PATH contracts in `plan.md`. It preserves the locked 25-repetition and 600-second host-soak bounds.

| Gate | Required proof | Failure rule |
| --- | --- | --- |
| M-1 | Read-only verifier baseline/install/upgrade/review modes, nonempty schema, atomic private output | Missing command/output/schema is UNMET |
| M1a | Exact Bats capability expectation; producer/validator observation schema; current binary/bundle binding | Any observation mismatch or missing real outcome blocks affected launch |
| M3 | repository/objective/idempotency identity; state-root shape/health; atomic resource uniqueness | Symlink/case/linked-worktree collision or unsafe root fails closed |
| M4 | PREPARING/READY/ABORTED compensation; IPC spoof/replay/reconnect; revocation race; primary/Git-root denial | M4 collects INT-003–008 only; any partial side effect fails |
| M5 | both-host tier/exact routing; shell/native/auth/hook/skill controls; tuple drift/expiry; concurrent immutable bundles | File presence/help text alone cannot pass |
| M6 | regression-before-fix; five resolver families; per-module coverage uplift checkpoint | zero unresolved critical/high before candidate freeze |
| M7 | complete row manifest, `--shard i/n`, 25 executions/row/platform, six 600s soaks, exact production XML inventory, line ≥80 | one failure/missing shard/auth/platform/evidence row fails |
| M8a | live/unknown refusal, old-writer restart, interrupted handoff, both rollback outcomes | never two compatible writers |
| M8b/c | ownership/hash/fork completeness, canonical feedback reruns, consumer skill preservation, repeated host canaries, docs examples | stale or self-compared evidence cannot activate |

Coverage release sequence uses pytest-cov once, then `coverage report` and `coverage xml` against its combined base file. A separate focused contract test may use raw `coverage run` plus `coverage combine --keep` to prove subprocess shard behavior. The release sequence never invokes a redundant combine.

Performance controls are part of evidence: per-operation, per-iteration and per-row deadlines; 720–750 second outer soak deadline; quota/auth precondition; isolated state root/disposable Git repository per row shard; deterministic seed and execution count recorded. A failed attempt remains a failed row.
