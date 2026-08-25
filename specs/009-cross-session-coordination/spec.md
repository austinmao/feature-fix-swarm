# Spec 009 — Cross-session coordination: claims + resource leases on filelock, with a PreToolUse path guard

> Multiple concurrent Claude Code / Codex sessions work this repo simultaneously.
> FFS coordinates WITHIN one run (gsd-run.sh lease, gates.py ledger) but nothing
> stops two sessions claiming the same spec, editing overlapping paths from
> different worktrees, or racing each other. This spec adds the cross-session
> layer: atomic per-spec claims and shared/exclusive resource leases built on the
> external `filelock` library (tox-dev/filelock ≥3.30 — SoftFileLease TTL +
> heartbeat + stale reclaim, ReadWriteLock shared/exclusive), plus a Claude Code
> PreToolUse guard that blocks Edit/Write inside another session's exclusive
> path lease.

## Context

- Decision record: `docs/research/multi-session-coordination.md` (PR #92).
- v1 of this spec proposed beads (`bd`) for claiming. /autoplan dual-voice review
  (codex: REJECT-as-written; independent opus: 26 findings) + operator adjudication
  dropped beads as oversized for single-Mac scope (it also overlaps gsd task
  tracking — DRY). Operator constraint: still external-first → re-search selected
  **filelock** (MIT, pip/tox-grade supply chain, daemonless, active — v3.32.x):
  ships `SoftFileLease` (TTL lease + heartbeat thread + stale-holder reclaim keyed
  on pid + process-start token), `ReadWriteLock` (shared/exclusive, SQLite-backed),
  `StrictSoftFileLock`, and a holder-metadata read API. Full adjudication:
  `specs/009-cross-session-coordination/prior-art.md`.
- FFS builds only the thin layer filelock doesn't ship: claim naming + registry,
  glob→resource mapping, monotonic generation (fencing) counter, CLI wrapper,
  PreToolUse guard, gsd-run.sh lifecycle wiring. Filelock's docs are explicit that
  its token "names a claim; it does not fence one" — the generation counter is ours.
- Live incident driving this: 2026-08-07, two FFS sessions planned overlapping
  specs unknowingly; caught only by a human-relayed handoff doc.

## Scope boundary (operator-locked)

- Specs 006-008 (parallel session) own takeover/land-queue, env registry,
  finisher lock, handoff fencing. NOT here.
- Deploy/env fencing for promote wrappers: deferred follow-up after 006/008.
  (Claim-generation fencing IS in scope here — it protects claims, not deploys.)
- Must NOT modify: `lib/gates.py`, `scripts/gsd/plan-wall.sh`,
  `scripts/gsd/run-finalizer.sh`. `scripts/gsd/gsd-run.sh` IS editable (claim
  lifecycle wiring — reviewer-directed, removes any spec-006 ordering dependency).

## Architecture summary

- **Store root**: `$(git rev-parse --path-format=absolute --git-common-dir)/..`
  `/.feature-fix-swarm/coord/` — ONE store per repo regardless of worktree
  (registry.json + per-resource lock files + session identity files). Symlinked
  store root refused (mirrors gsd-run.sh:340).
- **Mode**: `FFS_COORD_MODE=off|audit|enforce`, persisted default in
  `.feature-fix-swarm/coord/mode` (installed default: `enforce`); env var
  overrides per-invocation. `off` = every entry point exits 0 before touching
  Python. `audit` = warn, never block. `enforce` = fail closed: coordination
  failure (filelock missing, store unreadable, malformed hook input) blocks.
- **Identity**: opaque session UUID minted once per run and persisted to
  `coord/sessions/<uuid>` (fields: run_id, pid, pid_start_token, host, worktree
  path, created_at). Hook and CLI read the UUID from `$FFS_COORD_SESSION` or the
  run-state dir — never derived from a subprocess pid.
- **Claims**: `coord.py claim <spec-id>` — deterministic resource name
  (`claim:spec-NNN`), acquisition under the registry write-lock (single flocked
  transaction), `SoftFileLease` semantics (TTL default 300s, heartbeat renewed by
  the long-lived runner), monotonic **generation** incremented on every
  acquisition; release requires holder UUID + generation.
- **Leases**: `coord.py lease acquire|release|check <resource> [--shared]` —
  ReadWriteLock modes; `path:<glob>` resources restricted to two documented
  forms: `path:<exact-repo-relative-path>` and `path:<prefix>/**`. Overlap
  detection for path leases happens inside the same registry transaction
  (no check-then-acquire race).
- **Lifecycle**: gsd-run.sh acquires the claim after its pidfile claim, renews it
  in the existing heartbeat loop, revalidates generation at each phase boundary
  (superseded → abort run), and releases in the EXIT trap. Sessions without the
  runner use the CLI directly; TTL reclaim (pid + start-token staleness, not age
  alone) is crash recovery, not the normal path.
- **Guard**: `scripts/hooks/path-reservation-gate.sh` (PreToolUse Edit|Write) —
  bash fast-path exits for mode=off / store absent; otherwise Python reads hook
  JSON from **stdin**, realpath-normalizes + case-folds the target, rejects paths
  escaping the repo root, matches against the lease registry (cache invalidated
  by registry mtime+size, no time-based TTL), exit 2 on foreign exclusive match.

## User stories

- US1: As a session starting `/feature-implement NNN`, I claim spec NNN
  atomically so a second session starting the same spec stops instead of
  duplicating work.
- US2: As a session about to edit files, I am blocked when another live session
  holds an exclusive path lease covering that path.
- US3: As a session whose peer died mid-run, I can reclaim its spec after its
  lease goes stale, without a human untangling ownership — and the revived peer
  cannot keep acting with stale authority.
- US4: As an operator, `coord.py status` shows every claim + lease + holder in
  one command; `FFS_COORD_MODE=off` kills the whole layer.

## BDD Scenarios

Feature: cross-session claiming and path reservations

Scenario: second session is refused an already-claimed spec
  Given session A holds the claim on spec 009 with a fresh lease
  When session B runs the claim step for spec 009
  Then session B receives CLAIM-HELD naming session A's identity and expiry, and the run stops

Scenario: claim is granted on unclaimed spec
  Given no session holds a claim on spec 009
  When session A runs the claim step for spec 009
  Then session A receives CLAIM-OK with generation 1 and the registry records session A as holder

Scenario: re-claim by the same session is idempotent
  Given session A holds the claim on spec 009
  When session A runs the claim step for spec 009 again
  Then session A receives CLAIM-OK and the generation is unchanged

Scenario: dead session's claim is reclaimable and the old holder is fenced
  Given session A claimed spec 009 at generation 1 and its process is gone past the stale threshold
  When session B runs the claim step for spec 009
  Then session B receives CLAIM-OK at generation 2

Scenario: revived stale holder detects supersession at the next phase boundary
  Given session A's claim was superseded at generation 2 while session A was suspended
  When session A's runner revalidates its claim at the next phase boundary
  Then the runner reports CLAIM-SUPERSEDED and aborts instead of continuing

Scenario: edit inside another session's exclusive path lease is blocked
  Given session A holds an exclusive lease on path:skills/feature-implement/**
  When session B's harness evaluates an Edit targeting skills/feature-implement/SKILL.md
  Then the PreToolUse guard exits 2 naming session A, the lease, its expiry, and the release command

Scenario: edit outside any lease passes
  Given session A holds an exclusive lease on path:skills/feature-implement/**
  When session B's harness evaluates an Edit targeting docs/pipeline.md
  Then the PreToolUse guard exits 0 silently

Scenario: shared lease does not block edits
  Given session A holds a shared lease on path:docs/**
  When session B's harness evaluates an Edit targeting docs/pipeline.md
  Then the PreToolUse guard exits 0

Scenario: worktree topology shares one store
  Given session A claimed spec 009 from worktree A of the repo
  When session B runs the claim step for spec 009 from worktree B of the same repo
  Then session B receives CLAIM-HELD

Scenario: enforce mode fails closed on coordination failure
  Given FFS_COORD_MODE=enforce and the filelock library is not importable
  When session A runs the claim step or the guard evaluates an Edit
  Then the entry point exits non-zero with COORD-UNAVAILABLE and the action is blocked

Scenario: audit mode warns and proceeds
  Given FFS_COORD_MODE=audit and session A holds an exclusive lease on path:skills/**
  When session B's harness evaluates an Edit targeting skills/x.md
  Then the guard prints a COORD-AUDIT warning and exits 0

Scenario: kill-switch disables the whole layer
  Given FFS_COORD_MODE=off
  When any coord entry point runs
  Then it exits 0 immediately without invoking Python

## Acceptance Criteria

- AC-001: claim acquisition is atomic — a 2-process race (real processes, real
  store, runnable in CI with no external binary) yields exactly one CLAIM-OK and
  one CLAIM-HELD; exercised additionally from two git worktrees of one repo.
- AC-002: all coord state resolves through git-common-dir to ONE store per repo;
  a symlinked store root is refused (exit 78, mirroring gsd-run.sh).
- AC-003: identity is a persisted session UUID (+ pid, pid start token, host,
  worktree, run_id as metadata); re-claim by same UUID is idempotent (exit 0,
  generation unchanged); claim by different UUID against a fresh lease exits 3
  with holder identity + expiry.
- AC-004: stale-holder reclaim uses pid + process-start-token staleness (filelock
  `owner_is_stale` semantics), not wall-clock age alone; reclaim bumps the
  generation; a superseded holder's revalidation (`coord.py claim-check
  <spec-id> --generation N`) exits non-zero, and gsd-run.sh aborts the run on
  that signal at phase boundaries.
- AC-005: leases support exclusive and shared modes with the full conflict
  matrix (excl/excl block, excl/shared block both directions, shared/shared
  pass); release requires holder UUID + generation, refusing stale or foreign
  release; all claim+lease mutations and path-overlap checks run inside one
  flocked registry transaction.
- AC-006: `path:` resources accept exactly two forms — exact repo-relative path
  and `<prefix>/**` — matched after realpath normalization, repo-root
  containment check (reject escapes), and case-folding on case-insensitive
  filesystems. No other glob syntax is accepted (validation error at acquire).
- AC-007: the PreToolUse guard blocks (exit 2) Edit/Write targeting a foreign
  exclusive path lease in enforce mode; own-session and shared leases never
  block; audit mode warns only; the block message names holder, lease, expiry,
  and the exact status/release commands. Honest scope: this guards Claude Code
  Edit/Write only — Bash-mediated writes are NOT intercepted (documented; Bash
  matcher recorded as follow-up).
- AC-008: guard fail-open/fail-closed is mode-dependent: enforce → malformed
  hook JSON, unreadable store, or filelock import failure exits 2 (fail closed)
  with COORD-GATE-FAIL; audit → warns and exits 0. Hook JSON is read from
  stdin (never argv). Non-Edit/Write events always pass.
- AC-009: `FFS_COORD_MODE=off` short-circuits every entry point in bash before
  any Python launch; guard overhead in off/no-store cases is a stat + exit
  (measured, p95 < 15ms); enforce warm-path budget p95 < 120ms with the
  registry-mtime cache; settings.json hook entry carries a timeout.
- AC-010: no diff touches `lib/gates.py`, `scripts/gsd/plan-wall.sh`,
  `scripts/gsd/run-finalizer.sh` — enforced by
  `scripts/coord/forbidden-paths-check.sh` over merge-base diff + index +
  working tree, wired into the phase gates.
- AC-011: gsd-run.sh wiring — claim after pidfile acquisition, renew inside the
  existing heartbeat loop, revalidate generation at phase boundaries, release in
  the EXIT trap; a run whose claim is superseded mid-flight stops with
  CLAIM-SUPERSEDED. Interactive (non-runner) sessions get the documented manual
  CLI path in docs/coordination.md.
- AC-012: `filelock>=3.30` declared in docs/dependencies.md (version floor,
  install channel, license note) and probed by preflight (import +
  SoftFileLease/ReadWriteLock availability); `coord.py doctor` reports library
  version, store path, mode, and live claims/leases.
- AC-013: all new shell passes shellcheck; every suite runs headless in CI with
  no external binary; pytest baseline (171) and bats baseline unchanged.

## E2E Test Paths

- PATH-001: two concurrent `coord.py claim 009` processes → exactly one CLAIM-OK
  (repeated 20× in the bats race harness); repeated across two git worktrees.
- PATH-002: claim → SIGKILL holder → staleness satisfied → second session claims
  at generation+1 → revived first session's claim-check exits CLAIM-SUPERSEDED.
- PATH-003: exclusive `path:skills/**` lease as A → synthesized PreToolUse Edit
  JSON (stdin) for `skills/feature-implement/SKILL.md` as B → exit 2 with
  holder+remedy message; `docs/x.md` → exit 0; audit mode → warn + exit 0;
  off → exit 0 with no Python launch.
- PATH-004: enforce mode with filelock uninstalled (venv without it) → claim and
  guard both fail closed with COORD-UNAVAILABLE/COORD-GATE-FAIL.
- PATH-005: full gsd-run.sh lifecycle — start run (claim acquired), phase
  boundary (revalidated), supersede externally, next boundary aborts; normal
  completion releases claim (registry empty after EXIT trap).

## Edge Cases

- EDGE-001: laptop sleep suspends holder → heartbeat stale → peer reclaims →
  original wakes → generation mismatch at next boundary → abort (never two
  authoritative writers past a boundary).
- EDGE-002: worktree deleted while claim held → claim carries worktree path;
  `coord.py status` flags MISSING-WORKTREE; staleness check treats nonexistent
  worktree as immediately reclaimable.
- EDGE-003: registry corrupt/unparseable → enforce: fail closed + `coord.py
  doctor` names the file; audit: warn.
- EDGE-004: path with `..`, absolute path, or symlink escaping repo root →
  acquire-time validation error; guard-time containment reject (blocked in
  enforce).
- EDGE-005: TTL floor — TTL below 4× heartbeat interval rejected at acquire.
- EDGE-006: identity env leakage — `FFS_COORD_SESSION` pointing at another
  session's UUID file is detectable (pid start token mismatch) → refused.
  Cooperative trust boundary documented: single-UID machine, coordination is
  not a security boundary.
- EDGE-007: same store, two repos (consumer-repo scope) — out of scope v1;
  store is per-repo; framing narrowed accordingly.

## Test Contract Summary

| Layer | Count | Status |
|---|---|---|
| BDD Scenarios | 12 | draft |
| Unit test cases | 24 (plan.md list) | listed |
| Unit test files | 4 bats suites + 1 pytest module | mapped |
| Integration tests | 5 (INT-001..005) | defined |
| E2E paths | 5 (PATH-001..005) | defined (bats, CI-runnable) |

## Non-goals

- Deploy/staging/prod fencing in promote wrappers (follow-up after 006/008).
- Bash-write interception (recorded follow-up; v1 documents the gap honestly).
- Cross-repo / multi-machine coordination (beads federation remains the
  designed later path; see prior-art.md).
- Messaging (Claude Code native SendMessage; Codex gap accepted).
- Replacing gsd phases or speckit artifacts.
