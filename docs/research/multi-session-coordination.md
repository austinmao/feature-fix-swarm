# Multi-Session Coordination — Research Report

Date: 2026-08-07. Status: decision record (research complete, implementation = spec 009).
Method: 2 repo-exploration agents + 5 external-research agents (haiku/sonnet mix), source-verified deep dives on the two lead candidates (Agent Mail cloned @ `ec835d2`; Beads read at `gastownhall/beads` main), fable-level synthesis.

## 1. Problem

Multiple concurrent Claude Code / Codex sessions work the same repository: overlapping files, duplicate feature implementation, merges over stale bases, conflicting pushes, and dev/staging/production changing underneath sessions that are actively working or testing. Safety must not depend on the LLM "remembering to check" — critical enforcement must live below the prompting layer (hooks, git guards, wrappers, constraints).

Operator-confirmed failure modes actually hit (2026-08-07):

1. Two sessions editing overlapping files (file stomping)
2. One session merging while another sits on a stale base (merge/push races)
3. Deploy/migration collisions, incl. stale-session wake-ups
4. Two sessions implementing the same feature
5. Different branches updating dev/staging/production underneath sessions actively working/testing (env-drift-under-test)

## 2. What FFS already has (single-run coordination is largely BUILT)

| Layer | Mechanism | Location |
|---|---|---|
| Runner lease | atomic pidfile claim (`set -C`), 15s heartbeat (miss kills drive), machine identity, foreign-lease block (120s), reclaim mutex, single-flight exit 75 | `scripts/gsd/gsd-run.sh:140-470` |
| Evidence/grants ledger | one JSON at `<main-checkout>/.feature-fix-swarm/evidence.json`, git-common-dir pinned (shared across worktrees), `fcntl.flock` + atomic `os.replace`; typed grants w/ TTL 72h/168h, fail-closed exact match; prod actions need artifact-bound promote record | `lib/gates.py` |
| Liveness | ALIVE iff pidfile ∨ mtime-window ∨ grant-valid | `scripts/gsd/liveness-check.sh` |
| Run identity | `spec-NNN` / `adhoc-<slug>`, exported `GSD_RUN_ID` | feature-implement Step 1 |
| Promotion | 12-rule protocol, canary-gate, assert-merged, run-finalizer | `docs/promotion-protocol.md`, `scripts/gsd/` |
| Enforcement floor | 3 registered PreToolUse hooks (phase-evidence-gate blocks unearned checkbox flips; cli-hang-guard; credential-output-guard) + gsd test_command seam + CI verify-skill-blocks | `.claude/settings.json`, `scripts/hooks/` |

Designed but unbuilt (owned by the parallel full-autonomy program, specs 006–008): takeover records + land-queue (006), env registry (007), global finisher lock G11 + handoff fencing G9 (008).

**The gap is strictly CROSS-session:** task claiming, path reservations, env/deploy leases with fencing, cross-session journal, human-readable projection.

## 3. External survey

| Repo | Purpose | Storage | Task claim | File resv | TTL leases | Enforcement | Claude | Codex | Maturity | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| gastownhall/beads | task graph for agents | Dolt (embedded/server) | **atomic `--claim`** (CAS + retry) | ✗ | ✓ 5min TTL + heartbeat + `bd reclaim` | claim atomicity real; rest advisory | ✓ `bd setup claude` | ✓ native hooks ≥0.129 | 26k★, MIT, 1.x semver, very active | **integrate (claim pilot)** |
| Dicklesworthstone/mcp_agent_mail | agent mail + file reservations | SQLite WAL + git archive | ✗ (virtual ns only) | ✓ pathspec overlap | ✓ renew/release/sweep | **advisory by default**; git guards opt-in via env vars; bypassable; no PreToolUse block; **zero fencing tokens** | ✓ (warn-only hooks) | ✓ (notify poll) | 2k★, solo maintainer, MIT+rider (NOASSERTION) | **reject; borrow patterns** |
| gastownhall/gascity | orchestration SDK | Beads/Dolt | via beads | ✗ | via beads | controller loop | ✓ | ✓ | active, MIT | reject (replaces FFS); borrow session-as-projection |
| Dicklesworthstone/claude_code_agent_farm | parallel CC runner | JSON + lock/heartbeat files | ✗ | ✗ | 30s stale locks | thin (launch lock); race window | ✓ | ✗ | MIT, maintained | reject; pattern floor |
| nwiizo/ccswarm | multi-agent framework | in-process | ✗ | ✗ | ✗ | templates | ✓ | ✓ | 1.5k★ Apache-2.0 | reject; concepts only |
| Untrivial-ai/agent-orchestrator | worktree supervisor | SQL (sqlc) | supervisor-assigned | ✗ | ✗ | event routing | ✓ | ✓ | active Apache-2.0 | borrow event-routing + status model |
| BloopAI/vibe-kanban | agent task board | app-managed | board-based | ✗ | ✗ | UI | ✓ | ✓ | 27k★ Apache-2.0 | optional dashboard later |
| stablyai/orca | fleet ADE | proprietary-app | ✓ | ? | ? | app | ✓ | ✓ | 39k★ MIT | reject (IDE replacement) |
| smtg-ai/claude-squad | worktree+tmux mgr | tmux/fs | ✗ | ✗ | ✗ | none | ✓ | ✓ | 8k★ AGPL | reject (license, thin) |
| alexfrmn/murmur | MCP NATS agent bus | NATS JetStream | ✗ | ✗ | ✓ | transport-durable | ✓ | ✓ | 13★, days old | re-check in 6mo |

Native platform (changes the calculus): Claude Code 2.1.224 ships `EnterWorktree`/`ExitWorktree`, **`SendMessage`/`ListAgents` cross-session messaging**, and experimental Agent Teams — messaging/worktree tooling is now free (with known races: #83180, #84685, #84787). Codex has none of this. Any adopted tool must do meaningfully more than stock Claude Code.

## 4. Key technical findings

**Fencing tokens are required for deploy/migration paths** (Kleppmann). TTL alone cannot stop an expired session that wakes and acts on stale authority; the *protected resource* (deploy wrapper/gateway) must check a strictly-increasing generation counter, not just the coordinator. Neither Agent Mail (grep: zero fencing hits) nor Beads (merge slots lack generation semantics) ships this. Minimal lease schema: `(resource_key, holder, mode, acquired_at, expires_at, heartbeat_at, generation)`.

**Single-machine coordination needs no server DB.** SQLite WAL (`BEGIN IMMEDIATE` + unique constraint) or FFS's existing flock+atomic-replace JSON store is sufficient; Postgres/Dolt-server is warranted only for remote writers (CI runners, other machines). Beads' Dolt federation provides the multi-machine path later without rearchitecting.

**Agent Mail's enforcement is weaker than its README implies.** Reservations block nothing by default: git guards must be installed per-repo, are inert unless `WORKTREES_ENABLED=1`/`GIT_IDENTITY_ENABLED=1` is exported in the committing shell, and `AGENT_MAIL_BYPASS=1` skips everything. Its Claude hook only *prints* warnings. Worth borrowing: pathspec (gitignore-syntax) overlap detection, the `hooks.d` chain-runner pattern, the per-pane identity-file contract.

**Beads' claim really is atomic** (verified in `internal/storage/issueops/lease.go`: shared `row_lock` cell rewritten per mutation → guaranteed serialization conflict → retry; first claim wins, re-claim idempotent), and it ships lease TTL + heartbeat + stale reclaim + native Claude *and* Codex hook integrations. Embedded mode is single-writer; concurrent sessions need `bd init --server`.

**Practitioner failure modes** (anecdote-level): worktrees defer merge conflicts rather than removing them; per-worktree node_modules disk blowup; port/DB collisions across worktrees running services; Trigger.dev abandoned worktrees for GitButler virtual branches; native CC worktree machinery still has races.

## 5. Decision

**Hybrid: integrate Beads for cross-session task claiming; build a thin path/resource-lease + fencing layer on FFS's existing store and hook machinery; reject Agent Mail; use native Claude Code messaging; OpenWiki remains a projection.**

### Source of truth

| Concern | Authority |
|---|---|
| Task/feature ownership (cross-session) | Beads (claim-only pilot: one bead per spec run) |
| Intra-run task breakdown | gsd phases (`.planning/ROADMAP.md`) — unchanged |
| Path/file reservations | FFS lease layer (spec 009) + PreToolUse gate |
| Session/run liveness | existing gsd-run.sh pidfile/heartbeat + liveness-check.sh |
| Git state | git + assert-merged.sh |
| Env/deploy ownership | FFS leases w/ generation counter, checked by promote/deploy wrappers (post-006/008 follow-up) |
| Audit events | Dolt commits (task-level) + evidence.json (gate-level) |
| Human-readable projection | OpenWiki wiring at ship tail (existing, optional, fail-soft) |
| Semantic memory | gbrain (unchanged) |

### Beads vs speckit (DRY)

No overlap. Speckit authors design-time documents (spec.md/plan.md); Beads coordinates runtime ownership (who works what, atomic claim, reclaim-on-death). The only shared surface ever was tasks.md, which FFS already deprecated in favor of gsd phases. Real DRY tension is Beads vs gsd phases — resolved by the claim-only pilot: exactly one bead per spec run; gsd phases remain sole intra-run authority. Fallback if the pilot disappoints: a ~100-line claim namespace on the existing gates.py store (loses cross-machine sync, Codex hooks, reclaim tooling).

Integration follows FFS Pattern 3 (assumed host binary, fail-soft probe like gbrain) + version floor in `docs/dependencies.md` and a doctor check.

### Boundary with the parallel full-autonomy program (specs 006–008)

Spec 006 owns takeover/land-queue; 007 owns env registry; 008 owns finisher lock + handoff fencing. Spec 009 (this track) owns cross-session claiming + path/resource leases + the PreToolUse path gate. Deploy fencing (generation checks in `check_grant_prod`/promote wrappers) is a 009 follow-up that integrates with 006's takeover-check after it lands — not a duplicate of it.

## 6. Rejected alternatives (do not re-litigate)

- **Agent Mail integration** — advisory-by-default, persistent HTTP daemon + 31 Python deps, no fencing, MIT+rider license (NOASSERTION), solo maintainer; native SendMessage covers the messaging half.
- **Gas City / ccswarm / orca / agent-orchestrator adoption** — whole-orchestrator replacements for FFS, not layers under it.
- **Shared Markdown / OpenWiki as coordination authority** — fails concurrency trivially; FFS's OpenWiki wiring is already projection-only by construction.
- **Central Postgres/Dolt coordination service** — premature on one machine; upgrade triggers (remote CI writers, multi-host, HA) not present; Beads federation is the later path.
- **File-lock-directory coordination (Agent Farm style)** — race window between stale-check and delete; FFS's gsd-run.sh lease is already stronger.

## 7. Follow-ups

- Deploy/env fencing spec after 006/008 land (generation counter + wrapper-side check).
- Re-check murmur (MCP NATS bus) and vibe-kanban (dashboard) in ~6 months.
- Probe whether `bd merge-slot` can back path/resource leases before building a bespoke store (DRY check recorded in spec 009).
