<!-- socratic.md — spec 010 self-interrogation ledger. Enums (closed sets from scripts/gsd/socratic-slice.sh): domains ∈ {requirements, frontend, backend, data, api, security, infra, testing, observability, ai-llm, mobile, product-ux, cost-performance, compliance, team-maintenance}; depth ∈ {core, full}; packs (≤2) ∈ {software-design, domain-modeling, data-systems, operations, threat-modeling, ai-engineering, agent-design, legacy-change, testing-design, product-discovery}. -->
<!-- depth full: the spec exists to automate unattended/autonomous run resumption (respawn, auto-wake, auto-rerun) — the "autonomous tools" escalation trigger applies. -->
---
domains: [requirements, testing, infra, observability, cost-performance]
depth: full
packs: [operations, agent-design]
---

## Self-answered highlights

- Restated (req Q1): five bounded liveness mechanisms so an autonomous FFS run stays awake
  (await wall, respawn dead executor, wake after session limit, watch CI, cap plan length)
  instead of idling 86% of wall-clock. Production posture (req Q2): ships in the repo's
  product surface (`scripts/gsd/` + skills), used by every consumer repo's runs.
- Cost of doing nothing (req Q8): measured — 29h idle per multi-day run; the dominant
  stall class recurs every autonomous run that hits a wall near a turn boundary.
- Coexists-with (req Q5): extends plan-wall (new verb, zero policy change), gsd-run
  (post-drive branch), spec-decompose (one new required gate). No replacement, no migration.
- Success metric (req Q12, obs Q2): future run digests show active-time ≈ wall-clock; the
  spec-011 retro will report exactly this ratio — 010 is instrumented by 011's metric.
- First-look-on-failure (obs Q4): the run log's typed lines (`WALL-AWAIT:`, `GSD-RUN:RESPAWN`,
  `SESSION-WAKE:`, `CI-WATCH:`, `PLAN-LENGTH:`) — every mechanism announces both action and
  cap-hit on one greppable prefix; silent healing is forbidden by AC design.
- Zero-metric detection (obs Q18): a silently-stopped watcher degrades to today's exact
  behavior (stalled run, stale run-status mtime) — no NEW silent failure mode is introduced;
  cap-hit exits are typed and nonzero.
- CI cost (infra Q43-44, cost-perf): rerun bounded ≤2 per invocation; watch bounded 2h;
  await polls are local file stats (no API spend); respawn doubles one drive's token cost
  at most once — accepted, real usage is real (see ASSUME-006).
- Agent-design pack (bounded autonomy): every mechanism has a hard cap AND a kill switch;
  no mechanism can retry a *verdict*, only liveness; a second consecutive zero-commit death
  is treated as deterministic and stops. The healing layer widens no authority — grants,
  gates, and verdicts are byte-identical with all five features active (AC-012).
- Testing (test Q1-Q5): bats with stubbed `gh`/drives/reviewers per repo convention; CI
  blocks merge; each mechanism's cap-hit path gets an explicit test, not just happy path.

## Assumed (flag if wrong)

ASSUME-001: the claude CLI usage-limit banner stays greppable via the `(limit reached|hit your limit).*resets` shape; the codex variant is added from a captured fixture during implementation. Banner drift fails safe: typed `SESSION-WAKE:unparseable`, nonzero, run stops exactly as today.
ASSUME-002: `FFS_RESPAWN_MAX=1` default — one respawn distinguishes transient from deterministic death; a second zero-commit death propagates the failure.
ASSUME-003: plan length counts TOTAL lines (blanks/comments included) — the 2-vs-8-round convergence evidence was measured on total lines, so the gate mirrors the measurement.
ASSUME-004: 15s default poll for `--await` (local file reads only) balances latency vs noise; `PLAN_WALL_AWAIT_POLL` overrides.
ASSUME-005: the infra-failure keyword table (runner lost, system cancellation, startup_failure, network/dns/timeout/429/disk shapes) is sufficient classification; a misclassified test failure costs at most 2 wasted reruns, bounded.
ASSUME-006: a respawned drive's tokens double-count in budget accounting deliberately — the budget ledger records real usage, and the respawn cap bounds the overcount.
ASSUME-007 (revised at /autoplan gate — v2 reframe): session-wake never sleeps in-process; the run checkpoints `waiting(time, wake_at)` and exits. Reconciliation cadence without the operator cron line = "next FFS entrypoint invocation" — a fully idle machine stays asleep until touched, accepted and documented.
ASSUME-008 (revised at /autoplan review): `--await` distinguishes decided-blocked from decided-pass at the EXIT-CODE level (rc 0 pass-class only, rc 20 decided-blocked, rc 75 pending) — "the caller reads the verdict" was judged an unenforced interface; zero is never overloaded to mean "finished somehow".

## Open questions → grants

None — every open question resolved to an engineering default recorded in the ASSUME ledger above; no action here requires operator authorization beyond what feature-implement's own Step-6 enumeration (push/merge for the implementation run) already covers.

## Top risks

1. Respawn masking a real defect — bounded to 1 attempt, typed line, original rc propagates on the second death; a productive-but-failed drive is never respawned.
2. Banner drift silently disabling session-wake — impossible-by-design to be silent: unparseable is typed + nonzero; the degraded state equals today's behavior.
3. ci-watch misclassifying a real test failure as infra — bounded to 2 reruns then the failure surfaces with the full classification line for audit.
4. `--await` polling the wrong run-state directory (decoy-store class) — the verb must resolve records via the same path derivation plan-wall's write side uses, and the bats fixture asserts a foreign-cwd invocation still finds them.
5. (v2) Reconciler never runs — no cron installed + operator never touches FFS → a waiting run waits indefinitely; mitigated by the documented cron line, the entrypoint opportunistic pass, and the lifecycle record making the wait visible (`lifecycle.sh show`).
6. (v2) Lifecycle store as forgery target — a drive could write its own `runnable` record; bounded: relaunch argv comes from the record the ORCHESTRATOR wrote at checkpoint, budgets are durable, and the wall evaluator's record trust (run_id+sha) still gates execution; reconciler never bypasses gates.
