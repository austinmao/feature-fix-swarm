# Ruflo function curation (v3.19.0)

FFS wires a deliberately small subset of the ruflo MCP surface. This file is the
line — future changes should extend it consciously, not cargo-cult the full API.

## Adopted

| Function | Where | Why |
|---|---|---|
| `swarm_init` + `agent_spawn` | `feature-implement`, `spec-decompose` (v1.4.0), `swarm` | coordination metadata; execution stays native `Task()` (OAuth) |
| `hooks_model-route` / `hooks_model-outcome` | `feature-implement`, `spec-decompose` specialists | cheapest-capable-model routing + bandit learning |
| `hooks_pre-task` / `hooks_post-task` | `task-swarm`, decompose merge | learning context in, outcomes out |
| `agentdb_pattern-search` / `agentdb_pattern-store` | `plan-decompose`, `feature-implement` Step 9, `swarm` | in-run + cross-run pattern memory |
| `session_save` / `session_restore` | `feature-implement` checkpoints, `task-swarm` Step 0 | overnight runs survive context resets |
| `memory_search_unified` | `feature-implement` preload | prior-run context |
| `hive-mind_init/broadcast/consensus` | `feature-implement` QA phase | multi-agent QA verdicts |
| `mcp_status` | executor pre-flight | RUFLO_REQUIRED=auto fallback decision |

## Explicitly NOT adopted (do not add without a real driver)

| Surface | Why not |
|---|---|
| `agent_execute`, `managed_agent_*` | direct API calls bypass Claude Code OAuth — hard policy ban (see feature-implement §ruflo policy) |
| `daa_*` (decentralized autonomous agents) | over-engineering for a skills pipeline; native Task() + ledger covers autonomy |
| `autopilot_*` | overlaps the autonomy-grant ledger with less auditability |
| `neural_*`, `embeddings_*`, `ruvllm_*` | model-internals tooling; no FFS phase needs it |
| `workflow_create/execute` | rejected in v1.4.0 of feature-implement — canonical path is swarm_init + agent_spawn + Task() |
| `claims_*`, `coordination_*` (raw) | swarm topologies already encode this; raw claims add state without authority |
