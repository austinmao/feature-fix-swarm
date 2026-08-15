# Edge coverage — spec 006 (8-category probe over REQ-101..306)

Format: REQ · category · status · reason/AC. Categories probed only where the
data/behavior shape intersects (collection/stateful/io/text/numeric).

| REQ | category | status | reason / AC |
|---|---|---|---|
| REQ-101 record | empty | resolved | no grants/pendings → empty arrays, keys still present (bats key assert) |
| REQ-101 record | encoding | resolved | REQ-213 hostile-record tests: control chars, oversize, traversal |
| REQ-102 forbid[] | empty | resolved | scout prose → ZERO entries is itself the AC |
| REQ-103 wall | idempotency | resolved | wall is read-only + O_EXCL lock; second run refused (EDGE-004) |
| REQ-103 wall | concurrency | resolved | EDGE-004 two-wall bats case, exactly one proceeds |
| EDGE-001 staleness | boundary | resolved | exactly-72h boundary case: age ≥ TTL refuses (test at 72h and 72h−ε) |
| EDGE-001 staleness | precision | resolved | min(created_at, mtime) + granted_at cross-check kills clock-skew forgery |
| REQ-104 refusal | adjacency | resolved | descoped 2026-08-07: no re-issue path exists; grant-expired refusal prints manual re-grant remedy (bats) |
| REQ-105 wall wiring | empty | resolved | no-record no-op has its own test (M4a) |
| REQ-201 intake | empty | resolved | empty sources → empty queue exit 0 (unit test) |
| REQ-201 intake | adjacency | resolved | same branch from 2 sources: equal head → dedupe; differing → BLOCKED:identity-conflict |
| REQ-202 ordering | ordering | resolved | equal-compare items: oldest-first pinned; transitive-overlap test |
| REQ-202 ordering | boundary | resolved | single-item queue = trivial group (covered by happy path) |
| REQ-204 caps | boundary | resolved | round 2 passes / round 3 trips; clocks at exact bound trip (tests) |
| REQ-205 sigs | empty | resolved | blank signature ignored — inherited from note_failure (existing gates.py test) |
| REQ-206 breaker | adjacency | resolved | 2 same-class consecutive trips; interleaved different-class does NOT (test) |
| REQ-208 resume | idempotency | resolved | LANDED never re-executed; crash-at-merge reconciles (crash-injection bats) |
| REQ-210 CI wait | boundary | resolved | rc-124 at bound → BLOCKED:ci-timeout, no merge (hanging stub) |
| REQ-210 head pin | concurrency | resolved | head moved between CI pass and merge → BLOCKED:head-moved |
| REQ-211 STOP | concurrency | resolved | STOP mid-item: current side effect completes, nothing new starts (test) |
| REQ-212 queue lock | concurrency | resolved | second queue refused; stale-lock takeover only via dead-pid proof |
| REQ-213 journal | io | resolved | corrupt/unwritable json → QUEUE-ERROR:store, distinct verdict (EDGE-006) |
| REQ-214 inbox | empty | resolved | happy path = empty inbox (PATH-003); unhappy schema asserted (PATH-004) |
| REQ-302 set proof | adjacency | resolved | equal-count wrong-set substitution case; duplicate-target case |
| REQ-302 set proof | concurrency | resolved | ref advanced after proof → refuse + drift report |
| REQ-303 refusals | idempotency | resolved | refusals are executed paths, re-runnable, never unlock (bats matrix) |
| REQ-306 posture | encoding | resolved | garbage env value → advisory + fall through (EDGE-008) |
| REQ-306 posture | ordering | resolved | env may only strict-en; loosening ignored with advisory (test) |
| EDGE-010 requeue | boundary | resolved | exactly-once auto-requeue: counter 0→1 requeues, 1→ parks (test) |
| REQ-209 degradation | precision | resolved | denominator = current run's reviews; any degraded prod-touching review blocks — no ratio rounding ambiguity |
| REQ-209 degradation | boundary | dismissed | exactly-50% case: rule is "exceed 50%" — >50% strict; pinned in test name; no half-open ambiguity remains |

Unresolved: none. All applicable edges resolved into REQ text or the plan's
Unit Test List; no entries ride forward as assumptions.
