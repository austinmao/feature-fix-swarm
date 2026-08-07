# Prior art — spec 009 (cross-session coordination)

Search executed 2026-08-07 during the planning session (2 repo-exploration agents +
5 external-research agents, haiku/sonnet mix; deep dives source-verified). Full
findings: `docs/research/multi-session-coordination.md` (PR #92). This file is the
adjudicated summary the plan must cite.

| Candidate | Type | Stars/traction | Applicability verdict | Evidence |
|---|---|---|---|---|
| gastownhall/beads (`bd`) | repo/CLI | 26k★, MIT, 1.x semver, brew | **ADOPT (claim pilot)** — atomic `--claim` verified in `internal/storage/issueops/lease.go` (row_lock CAS + retry); lease TTL + heartbeat + `bd reclaim`; native `bd setup claude` + `bd setup codex`; merge-slot exclusive primitive candidate for resource leases | research doc §3-4 |
| Dicklesworthstone/mcp_agent_mail | repo/MCP server | 2k★, MIT+rider (NOASSERTION) | REJECT — advisory-by-default (guards inert without env opt-in, `AGENT_MAIL_BYPASS=1` skips all), zero fencing tokens, HTTP daemon + 31 deps, solo maintainer. Borrow pathspec overlap-detection approach only | cloned @ ec835d2, guard.py read in full |
| gastownhall/gascity | repo/SDK | active, MIT | REJECT — whole-orchestrator; would replace FFS, not serve it | research doc §3 |
| Dicklesworthstone/claude_code_agent_farm | repo | MIT | REJECT — file-lock floor with stale-check race; FFS gsd-run.sh already stronger | research doc §3 |
| nwiizo/ccswarm | repo | 1.5k★ Apache-2.0 | REJECT — framework concepts only | research doc §3 |
| Untrivial-ai/agent-orchestrator | repo | active Apache-2.0 | REJECT integrate; borrow event-routing/status-model ideas | research doc §3 |
| Claude Code native (SendMessage/ListAgents/EnterWorktree) | platform | vendor-shipped 2.1.224 | ADOPT (free) — covers messaging + worktree isolation; does NOT cover claims/leases (and Codex lacks all of it) | research doc §3 |
| stablyai/orca, BloopAI/vibe-kanban, smtg-ai/claude-squad, alexfrmn/murmur | repos | 39k/27k/8k/13★ | REJECT for this spec (IDE replacement / dashboard-later / AGPL+thin / too young) | research doc §3 |
| local skills (`find-skills` sweep) | skills | — | No existing FFS/host skill provides cross-session claiming or path leases; nearest are gsd-run.sh (single-run lease, reused as pattern) and gates.py grants (kept as-is, owned by parallel program) | repo exploration |

## v2 adjudication (2026-08-07, post-/autoplan review + operator direction)

/autoplan dual voices (codex REJECT-as-written + independent opus, converging) found
beads oversized for the declared single-Mac scope; operator agreed ("beads handles
quite a lot of what gsd does... too much") but held the external-first constraint.
Targeted re-search (sonnet agent, source-read) over lock/lease libraries:

| Candidate | Verdict | Key facts |
|---|---|---|
| **tox-dev/filelock ≥3.30** | **ADOPT** | MIT, daemonless, pip/tox-grade supply chain, v3.32.2 (2026-07-29, active). Ships `SoftFileLease` (TTL lease + heartbeat thread + stale reclaim via pid + process-start token), `ReadWriteLock` (shared/exclusive, SQLite-backed), `StrictSoftFileLock`, holder-metadata read API (`lease.owner`). Docs explicit: token "names a claim; it does not fence one" → generation counter stays ours. |
| grantjenks/python-diskcache | fallback | Apache-2.0, atomic `cache.add` + expire; no RW modes, no heartbeat, no holder API — more DIY. Reconsider if SQL-queryable coord state wanted. |
| portalocker / oslo.concurrency / fasteners | reject | pure mutexes (no TTL lease) / OpenStack baggage / RW-lock but stale (2025) + no TTL. |
| mcp_agent_mail_rust | **hard reject** | daemon-shaped AND license = MIT + rider voiding rights for "OpenAI, L.L.C.; Anthropic, PBC; ...any person or entity acting on behalf of the foregoing" — coordinating Claude/Codex sessions plausibly falls in the carve-out. |
| agent-coordinator (4★) / lock-master (1★) | reject | unproven toys; lock-master worth reading for shape only. |
| Fresh sweep ("lease TTL file lock", "agent file reservation", etc.) | nothing new | no standalone coordination lib extracted from the agent-tooling wave besides the above. |

FFS builds on top of filelock ONLY: claim naming + registry, glob→resource mapping,
monotonic generation (fencing), CLI wrapper, PreToolUse guard, gsd-run.sh wiring.
**API verified locally 2026-08-07** against `filelock==3.32.2` in a scratch venv
(python 3.14): `SoftFileLease(lock_file, lease_duration, heartbeat_interval,
on_compromise)` with `.owner`, `.force_break()`, `.is_lock_held_by_us` — present;
`ReadWriteLock.{acquire_read, acquire_write, read_lock, write_lock}` — present;
`StrictSoftFileLock`, `LeaseCompromise` — present. Host system python ships 3.29.0
(has ReadWriteLock, lacks SoftFileLease) → version floor `>=3.30` is real and goes
in requirements-dev.txt + preflight probe.

## Decision input (v1 — superseded on the beads point by the v2 adjudication above)

**Adjudication (judgment-tier, recorded): ADOPT beads via FFS Pattern 3** (assumed
host binary, fail-soft probe, version floor in docs/dependencies.md) for cross-session
task claiming — claim-only pilot: exactly one bead per spec run; gsd phases remain the
sole intra-run task authority (DRY vs speckit resolved: no overlap — speckit authors
design-time documents, beads coordinates runtime ownership).

Resource/path leases: probe `bd merge-slot` as the primitive first (exclusive-access,
already shipped, DRY win); build a bespoke store ONLY if merge-slots prove unfit
(that finding must be recorded here). Enforcement layer (PreToolUse path gate) is
FFS-built either way — no candidate ships sub-LLM enforcement worth adopting
(Agent Mail's is opt-in-inert; verified).

Deploy fencing (generation counters): NO prior art ships it (Agent Mail: zero hits;
beads merge-slots: no generation semantics). Deferred follow-up spec after 006/008.

License/maintenance: beads MIT, 94 releases, very active, multi-channel install.
Coupling: loose — CLI probed at runtime, kill-switch `FFS_COORD=off`, absent binary
degrades to warn (advisory) per Pattern 3.
