# Spec 008 edge coverage (post-gauntlet)

| REQ | category | status | reason/AC |
|---|---|---|---|
| REQ-103 sampling | boundary | resolved | trips only at 20/20 with ≥20 samples; 19/20 and <20 all-fail selectable (AC-002, EDGE-001) |
| REQ-103 open-set | identity | resolved | unknown rung ids recorded (EDGE-002) |
| REQ-103 last-rung | fail-closed | resolved | every-rung-tripped → NO filtering; existing REVISE/unavailable behavior stands — restated per gauntlet (EDGE-013, AC-002) |
| REQ-103 recovery | liveness | resolved | half-open probe every 10th opportunity + operator reset — auto-clear reachable (AC-002) |
| REQ-102 boundary | boundary | resolved | exactly 50% allowed, strict > refuses (EDGE-003, AC-001) |
| REQ-102 seams | completeness | resolved | rule at check_grant_prod AND record_promotion; hotfix documented-exempt with dissent recorded (AC-001) |
| REQ-101 run-binding | identity | resolved | invocation events require run_id; rung attempts cross-run by design (EDGE-014) |
| REQ-201 multiplicity | concurrency | resolved | two switches → two rows; _StoreLock serialized (EDGE-004, EDGE-005) |
| REQ-201 fail-closed | availability | resolved | unwritable store → skip NOT honored, nonzero typed reason (AC-003) |
| REQ-201 attribution | identity | resolved | GSD_RUN_ID unset → `unattributed` row, skip honored (EDGE-016) |
| REQ-301 identity+freshness | identity | resolved | sha binds IN ADDITION to freshness; stale sha refused by freshness independently (EDGE-006, AC-004) |
| REQ-301 typing | fail-closed | resolved | missing sha or untyped legacy evidence → refuse for canary surfaces (AC-004) |
| REQ-302 scope | identity | resolved | other-surface/other-run/failed dry-run refused; undeclared surface no-ops (EDGE-007, AC-005) |
| REQ-401 delimiter | injection-boundary | resolved | counterfeit PRIMARY delimiter neutralized in every branch; claims are fence-presence + store-authority, not model behavior (EDGE-008, AC-006) |
| REQ-501 idempotence | boundary | resolved | breach fires exactly once at crossing; repeated over-limit updates silent (EDGE-015, AC-008); daily budget DESCOPED (EDGE-009 retired) |
| REQ-601 liveness | concurrency | resolved | live lock → wait then yield WITH finisher-skipped mark; dead pid reclaimed; symlink refused (EDGE-010, AC-009) |
| REQ-701/702 availability | fail-soft | resolved | empty store → exit 0 "no events"; gh unreachable → `unavailable` fields, exit 0; notify failure → cursor retained (EDGE-011, EDGE-012, AC-010) |
| REQ-703 join | identity | resolved | ledger id canonical; mapping record joins run-state; gh joins by PR/sha in events (AC-011) |
