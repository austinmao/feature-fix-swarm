<!-- /autoplan restore point: ~/.gstack/projects/feature-fix-swarm/docs-multi-session-coordination-research-autoplan-restore-20260807-151741.md -->
# Plan 009 — Cross-session coordination (v2, post-review)

Spec: `specs/009-cross-session-coordination/spec.md` (v2).
Research: `docs/research/multi-session-coordination.md` (PR #92).
v1→v2: /autoplan dual-voice review rejected the beads-based v1 (see review report
below); operator kept the external-first constraint; targeted re-search selected
**filelock** as the external core. All 26+25 findings adjudicated below.

## Prior-art decision (required citation)

Per `specs/009-cross-session-coordination/prior-art.md` (v2 adjudication):
**ADOPT `filelock` (tox-dev/filelock) ≥3.30** — MIT, daemonless, pip/tox-grade
supply chain, active (3.32.2, 2026-07-29). Ships the hard parts: `SoftFileLease`
(TTL + heartbeat thread + stale reclaim keyed on pid + process-start token),
`ReadWriteLock` (shared/exclusive, SQLite-backed), `StrictSoftFileLock`, holder
metadata API. REJECT beads for this scope (oversized: binary + Dolt + server mode
for one claim op; overlaps gsd task tracking — operator-adjudicated 2026-08-07).
REJECT mcp_agent_mail_rust (daemon-shaped AND license rider explicitly voids
rights for OpenAI/Anthropic-affiliated use — hard blocker). diskcache = fallback
if queryable SQL state is later wanted. FFS builds only what filelock lacks:
claim naming/registry, glob→resource mapping, generation (fencing) counter, CLI,
PreToolUse guard, gsd-run.sh wiring.

## Architecture

```
session (Claude or Codex; runner or interactive)
  │
  ▼
scripts/coord/coord.py  (single Python module; CLI: claim | claim-check |
  │                      lease acquire|release|check | status | doctor | release)
  │   store: <git-common-dir>/../.feature-fix-swarm/coord/
  │     registry.json      — claims+leases+generations (atomic replace,
  │                          ALL mutations + overlap checks under one
  │                          filelock write-lock transaction)
  │     sessions/<uuid>    — identity records (run_id, pid, pid_start_token,
  │                          host, worktree, created_at)
  │     mode               — persisted FFS_COORD_MODE default (enforce)
  │   engine: filelock SoftFileLease (claim TTL 300s default, heartbeat by
  │           runner), ReadWriteLock (shared/exclusive leases)
  ▼
scripts/hooks/path-reservation-gate.sh  (PreToolUse Edit|Write)
  bash fast-path: mode=off or no registry → exit 0 (no Python)
  python: stdin JSON → realpath+containment+casefold → registry match
          (cache keyed on registry mtime+size) → exit 2 foreign-exclusive
          (enforce) | warn (audit); parse/read failure → fail closed (enforce)

scripts/gsd/gsd-run.sh wiring (permitted file):
  after claim_pidfile → coord.py claim "$RUN_ID"   (abort on CLAIM-HELD)
  heartbeat loop     → coord.py claim-renew
  phase boundary     → coord.py claim-check --generation "$GEN"  (abort on supersede)
  EXIT trap          → coord.py release --claim "$RUN_ID"
```

Env/config: `FFS_COORD_MODE=off|audit|enforce` (env > `coord/mode` file >
`enforce`), `FFS_COORD_SESSION` (identity file override), `FFS_COORD_TTL_SECS`
(floor: 4× heartbeat). Python: repo already requires python3; new dep
`filelock>=3.30` (docs/dependencies.md + preflight probe; NOT vendored — plain
pip dependency, mirrors pytest handling).

## Unit Test List

Design-critical first:

- [ ] coord.py claim: 2-process race → exactly one CLAIM-OK (real store, 20 reps)
- [ ] coord.py claim: same race from two git worktrees of one repo (store shared)
- [ ] coord.py claim: unclaimed → CLAIM-OK gen=1; registry holder recorded
- [ ] coord.py claim: same-UUID re-claim → exit 0, gen unchanged
- [ ] coord.py claim: foreign fresh claim → exit 3, prints holder+expiry
- [ ] coord.py reclaim: dead pid + start-token mismatch → CLAIM-OK gen+1
- [ ] coord.py reclaim: live pid, stale mtime only → NOT reclaimable (age alone insufficient)
- [ ] coord.py claim-check: matching gen → 0; superseded gen → non-zero CLAIM-SUPERSEDED
- [ ] coord.py release: requires UUID+gen; stale gen refused; foreign UUID refused; idempotent for holder
- [ ] lease conflict matrix: excl/excl, excl/shared (both orders), shared/shared
- [ ] lease overlap: `path:skills/**` vs `path:skills/feature-implement/**` conflict detected inside txn
- [ ] path validation: reject `..`, absolute, symlink-escape, unsupported glob syntax
- [ ] TTL floor: TTL < 4× heartbeat rejected
- [ ] identity: FFS_COORD_SESSION pointing at foreign UUID w/ mismatched start token → refused
- [ ] store: symlinked store root → exit 78
- [ ] store: corrupt registry → enforce fail-closed, audit warn
- [ ] mode resolution: env > file > default enforce
- [ ] gate: foreign exclusive path → exit 2 w/ holder+remedy (enforce)
- [ ] gate: own lease / shared lease / unrelated path → exit 0
- [ ] gate: audit mode → warn, exit 0
- [ ] gate: off mode → exit 0 before Python (invocation-log assert)
- [ ] gate: malformed stdin JSON → enforce exit 2, audit exit 0
- [ ] gate: casefold match on case-insensitive fs (skills vs Skills)
- [ ] gate: cache invalidated on registry mtime/size change (acquire → immediate block, no 5s hole)

## TDD Unit Test Map

| Source file | Test file | Behaviors |
|---|---|---|
| scripts/coord/coord.py | lib/tests/test_coord.py (pytest) | registry txn, generations, identity, validation, staleness — fast in-process |
| scripts/coord/coord.py | tests/bats/coord-claim.bats | real 2-process races, worktree topology, CLI exit codes |
| scripts/coord/coord.py | tests/bats/coord-lease.bats | conflict matrix, overlap, release auth |
| scripts/hooks/path-reservation-gate.sh | tests/bats/path-reservation-gate.bats | block/pass/audit/off/fail-closed/casefold/cache |
| scripts/coord/forbidden-paths-check.sh | tests/bats/coord-forbidden-paths.bats | merge-base + index + worktree diff guard |

No stubs for the core: filelock is pip-installed in CI, every race test runs the
real engine. (v1's stub-only atomicity testing was review finding #4/#20.)

## Integration Tests

- INT-001: PATH-001 race harness (real processes, real store, CI).
- INT-002: PATH-002 kill → reclaim → fenced-revival, end to end.
- INT-003: PATH-003 guard matrix via synthesized hook JSON on stdin.
- INT-004: PATH-005 gsd-run.sh lifecycle (claim/renew/revalidate/release) using
  the runner's own functions in a bats sandbox repo.
- INT-005: forbidden-paths-check green on the spec branch (AC-010).

## Phase Test Gates

| Phase | Gate condition | Command |
|---|---|---|
| Phase 1 (coord.py claims) | pytest module + claim bats + shellcheck | `python3 -m pytest lib/tests/test_coord.py -q && bats tests/bats/coord-claim.bats` |
| Phase 2 (leases + overlap) | lease bats + conflict matrix | `bats tests/bats/coord-lease.bats` |
| Phase 3 (guard + settings) | gate bats | `bats tests/bats/path-reservation-gate.bats` |
| Phase 4 (runner wiring + docs) | lifecycle INT-004 + forbidden-paths + full suites | `bats tests/bats/coord-*.bats tests/bats/path-reservation-gate.bats && bash scripts/coord/forbidden-paths-check.sh` |
| Final | baselines unchanged | `python3 -m pytest lib/tests -q && bats tests/bats/` |

## Implementation phases

1. **Phase 1 — coord.py claims + identity + store + generations.** New files only
   + docs/dependencies.md filelock section. Verify filelock API claims
   (SoftFileLease, ReadWriteLock, owner metadata) against the installed version
   FIRST — record `pip show filelock` + import evidence in prior-art.md; if the
   research overstated the API, fall back to building lease semantics on
   `filelock.FileLock` + our registry (decision recorded).
2. **Phase 2 — leases: ReadWriteLock modes, path validation, overlap-in-txn.**
3. **Phase 3 — path-reservation-gate.sh + settings.json registration (timeout,
   Edit|Write matcher) + coord.py doctor/status.**
4. **Phase 4 — gsd-run.sh wiring (claim/renew/revalidate/release) +
   docs/coordination.md (protocol, interactive-session manual path, cooperative
   trust boundary, Bash-write gap) + forbidden-paths-check.sh.**

## GSTACK REVIEW REPORT (/autoplan, 2026-08-07)

Dual voices ran independently: Codex (adversarial CEO+eng+DX, verdict: REJECT v1)
+ Claude subagent (opus, independent eng, 26 findings / 5 critical).
Consensus (both agreed unless noted):

| Dimension | Consensus → v2 disposition |
|---|---|
| Beads justified for single-Mac scope? | NO → dropped; filelock adopted (operator kept external-first) |
| State shared across worktrees? | broken in v1 → git-common-dir anchor (AC-002, worktree tests) |
| Holder identity sound? | broken in v1 → persisted session UUID + start token (AC-003) |
| Heartbeat/release lifecycle owned? | unowned in v1 → gsd-run.sh wiring (AC-011) |
| First-claim creation race | fixed: deterministic resource name + single registry txn (AC-001/005) |
| Degraded-warn default | replaced: mode model, default enforce, fail closed (AC-008; operator-approved) |
| Edit\|Write-only enforcement honest? | reframed: Claude Edit/Write guard, Bash gap documented (AC-007) |
| 5s cache hole | replaced: mtime+size invalidation (AC-009, unit test) |
| Live-engine tests optional | moot: filelock in CI, all races real (AC-001/013) |
| Stale-authority two-owners | claim generations + boundary revalidation (AC-004, EDGE-001) |
| Overlap check-then-acquire race | overlap inside registry txn (AC-005) |
| Env-var UX | FFS_COORD_MODE=off\|audit\|enforce + doctor/status (AC-009/012) |
| Glob semantics trap | two documented forms only + containment + casefold (AC-006) |
| argv ARG_MAX bypass | stdin JSON (AC-008) — NOTE: same flaw exists in gsd-phase-evidence-gate.sh; recorded as follow-up finding, file owned by parallel program |
| No release/complete op | release wired into EXIT trap + CLI (AC-011) |
| Blocked-dev recovery UX | block message carries holder/expiry/commands (AC-007) |
| TTL floor | 4× heartbeat clamp (EDGE-005) |
| Worktree-loss orphan | EDGE-002 |
| flock CLI not on macOS | moot: python filelock library |
| Latency budget unmeasurable | AC-009 p95 budgets + measured |

### Decision Audit Trail

| # | Decision | Class | Principle | Rationale |
|---|---|---|---|---|
| 1 | git-common-dir store anchor | Mechanical | P1 | both voices critical |
| 2 | persisted session UUID identity | Mechanical | P5 | pid-in-subprocess provably wrong |
| 3 | lifecycle in gsd-run.sh (permitted file) | Mechanical | P4 | reuses heartbeat+EXIT trap; kills spec-006 ordering dep |
| 4 | default enforce, mode model | Mechanical→operator-approved | P1 | warn-default reproduces founding incident |
| 5 | stdin hook JSON + bash fast-path + fail-closed enforce | Mechanical | P1/P5 | ARG_MAX bypass, latency, silent-fail |
| 6 | two glob forms + containment + casefold | Mechanical | P5 | hand-rolled gitignore = trap |
| 7 | claim generations (fencing) + boundary revalidation | Mechanical | P1 | sleep/revive two-owner |
| 8 | single flocked registry txn incl. overlap | Mechanical | P5 | check-then-acquire race |
| 9 | honest enforcement framing; Bash matcher follow-up | Taste→resolved | P6 | ship honest scope |
| 10 | real-engine tests in CI (no stubs for core) | Mechanical | P1 | headline property must be tested |
| 11 | DROP beads | User Challenge → operator: agreed | P4 | oversized; overlaps gsd |
| 12 | ADOPT filelock as external core | Operator-directed (external-first) + re-search | P4 | daemonless, MIT, ships lease/RW-lock/staleness |

## Risks

- filelock API surface as researched (SoftFileLease/ReadWriteLock) is
  agent-reported, not yet locally verified — Phase 1 task 1 verifies and records;
  fallback path defined.
- gsd-run.sh is edited by this spec — low but nonzero conflict surface with the
  parallel program (their G11 references it as a pattern, not an edit target);
  wiring is additive (new function + 4 call sites) to keep merges trivial.
- ReadWriteLock is SQLite-backed per research — same-store contention behavior
  under 5+ sessions unmeasured; AC-009 budgets catch regressions.
- `.claude/settings.json` + `docs/dependencies.md` appends: line-level merge risk
  only.
