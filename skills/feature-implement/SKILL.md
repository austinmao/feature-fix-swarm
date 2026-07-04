---
name: feature-implement
description: "Execute tasks.md via ruflo swarm (strict default). Intelligent model routing via hooks_model-route overrides sonnet-default annotations (only the default `sonnet` tier is ever routed; explicit haiku/opus/fable annotations always win). Exact agent delegation uses the hybrid ECC + wshobson catalog via dispatch.py. DAA cognitive pattern selection for thinking:high/max tasks. Fable supported on native Agent path; ruflo path maps host-native tiers to haiku/sonnet/opus coordination tiers (fable itself falls back to sonnet on the ruflo path). RUFLO_REQUIRED=1 (strict default) | 0 (force native) | auto (graceful fallback). Session checkpoint auto-saved; use --resume to continue after context reset."
version: "1.13.0"
allowed-tools:
  - Read
  - Edit
  - Bash
  - Glob
  - Agent
---

# feature-implement — Execute a decomposed feature task list

## When to invoke

- After `/spec-decompose` produces `specs/NNN/tasks.md` with annotations
- When the user says "implement NNN", "run tasks for NNN", "execute feature NNN"
- Resumes gracefully — picks up at the first `[ ]` task if some are already `[X]`

## Invocation

```
/feature-implement [NNN]              # DEFAULT: ruflo swarm + auto mode, run-all until done or first failure
/feature-implement [NNN] --one        # execute only the next unchecked task (single task)
/feature-implement [NNN] --dry-run    # print what would execute, don't spawn
/feature-implement [NNN] --task T042  # execute a specific task ID only
/feature-implement [NNN] --ruflo      # no-op (already default in v1.1.0+, kept for explicitness)
/feature-implement [NNN] --auto       # no-op (already default in v1.3.0+, kept for explicitness)
/feature-implement [NNN] --no-auto    # disable auto mode: show cost estimate and prompt for confirmation
/feature-implement [NNN] --no-qa-loop          # skip per-phase QA (faster, less safe)
/feature-implement [NNN] --no-phase-audit      # skip per-phase codex hostile audit (audit only; QA still runs)
/feature-implement [NNN] --qa-skip e2e         # skip specific QA dimensions
/feature-implement [NNN] --qa-only review,security  # run only these QA dimensions
/feature-implement [NNN] --autonomous  # unattended mode (v3.18.0): requires a fresh
                                       # preflight PASS + reads the autonomy-grant
                                       # ledger at operator gates instead of asking
```

**v1.3.0 defaults:** Both `--ruflo` and `--auto` are on by default. You never need to pass them.
- `--auto`: skip cost confirmation prompt, run all tasks without pausing. Opt out with `--no-auto`.
- `--ruflo`: ruflo MCP swarm executor. Opt out with `RUFLO_REQUIRED=0` env override (debug only).

**v1.1.0 ruflo policy:**
- Ruflo is the default and only supported executor.
- If `mcp__ruflo__*` tools are unreachable AND `RUFLO_REQUIRED=1` (default): hard-fail with structured error. No silent fallback.
- Escape hatch: `RUFLO_REQUIRED=0 /feature-implement NNN` falls back to native Agent for debugging. Logs WARNING on every spawn.
- The `--no-ruflo` flag from v1.0.0 has been removed. To run native, use the env override above.
- Do not use `mcp__ruflo__agent_execute` or `mcp__ruflo__managed_agent_*` for task execution. Those are API-backed paths. Ruflo coordinates; the active host CLI executes via `scripts/harness/ruflo-host-executor.sh`.

**Default is run-all.** The skill loops through every `[ ]` task until either:
- All tasks are `[X]` (success)
- Any task returns `[F]` (failure — stop, report, user fixes and reruns)
- A dependency cycle is detected (abort)

## Operating disciplines (Fable-mode)

Three disciplines from [Fable-mode](https://github.com/mrtooher/fable-mode) wrap task execution:

1. **Stage map first.** After parsing tasks.md (Step 2), print the phase plan — phases in order, task count and `[P]` parallel groups per phase, expected exit per phase — before spawning anything. A wrong dependency read is cheap to fix before Step 5, expensive after.
2. **Verify before advancing.** With `--qa-loop` (default) no phase advances on red: deterministic hooks + the LLM QA swarm gate every phase boundary (Step 5.5), and failures trigger the investigate-fix-retest loop (Step 5.5b) before the next phase starts. This is the package's reason to exist — do not disable it to go faster.
3. **Self-critique before delivery.** Before the final report (Step 8), re-read the session as a skeptic: which `[X]` tasks are "done" only by self-report, which acceptance criteria are unproven by QA? Name at least one and surface it.

## Machine gates (v1.9.0/v1.10.0 — completion authority is never self-report)

`lib/gates.py` (installed next to dispatch.py) is the run's ground truth. Six rules,
all enforced in the loop below:

1. **Evidence before checkbox.** `record-gate` + `verify-done` (exit 0) before any
   `[X]` flip. Evidence store: `GATES_STORE` (default `.feature-fix-swarm/evidence.json`).
   Append every outcome to `.feature-fix-swarm/results.md` — append-only, never rewrite.
2. **RED proof before GREEN.** An implementation task whose sibling test task exists
   must pass `check-red` first: the RED task pipes its failing run through
   `record-red --exit $EXIT` (rejected if the log shows no real failure). GREEN task
   blocked until `check-red` exits 0.
3. **Gate ladder, cheap→expensive.** Per task/phase: compile/typecheck → lint → unit →
   integration → e2e smoke → LLM review. A rung failure skips later rungs and retries
   the task. LLM review rounds are capped at 2 per phase; deterministic rungs retry up
   to `RALPH_MAX_RETRIES`.
4. **Tamper scan.** After each impl task: `git diff | python3 gates.py scan-tamper`.
   Findings (deleted asserts, added skips, `exit 0`, CI edits) = CRITICAL, phase blocked.
   Impl tasks may not touch test files (`check_test_separation`) — test edits belong to
   the paired test-author task.
5. **No-progress stop (WIRED v1.10.0).** On every task failure, record the failure
   signature: `gates.py note-failure "$TASK_ID" --sig "$SIG"` (SIG = last failing
   line of the gate output). Exit 1 = same signature twice in a row → STOP the loop
   and report instead of burning retries. Truth score is computed per phase:
   `gates.py phase-score T040 T041 T042` (weights compile .35 / tests .25 / lint .20 /
   typecheck .20); exit 1 (< 0.95) after max retries → roll back to the phase-start
   checkpoint, do not limp forward.
6. **Every gate outcome is a training signal.** After each task: `hooks_model-outcome`
   (success/fail) so the Thompson-sampling router learns; `agentdb_pattern-search`
   before non-trivial tasks, `agentdb_pattern-store` after novel successes.
7. **Strict evidence provenance (v1.10.0).** The loop exports `GATES_STRICT=1`:
   `verify-done` rejects caller-recorded evidence (`record-gate`) — only
   runner-executed `run-gate` evidence can flip a checkbox. `record-gate`/`record-red`
   now warn at runtime; they remain available for humans, never for the loop.

## Autonomous mode (v3.18.0 — unattended runs)

`--autonomous` front-loads every run-time decision to plan-time so an
overnight run never stalls waiting for the operator. Two mechanical
preconditions, both fail-closed:

Both checks use the LEDGER run id `RUN_ID="spec-${SPEC_ID%%-*}"` (set in Step 1
AFTER the branch-derived SPEC_ID fallback, so no-arg invocations still key
correctly) — the SAME key `/feature-spec` Step 5/6 and `/task-swarm`
Steps 2/3 wrote the preflight PASS + grants under. And both go through the
resolved `$GATES_PY` (3-shape resolver, Step 6 block — run it BEFORE these
checks in autonomous mode); a bare `lib/gates.py` breaks in the vendored
`packages/feature-fix-swarm/` install shape.

1. **Preflight proven.** Before the first spawn:
   `python3 "$GATES_PY" check-preflight "$RUN_ID"` must exit 0 — a recorded
   PASSING `/preflight` run (< 24h old) covering the run's env vars + service
   probes. Exit 1 → REFUSE to start:
   `[feature-implement] ERROR: no fresh preflight for $RUN_ID — run /preflight first.`
   Dying at 11pm while the operator is present beats stalling at 3am.

2. **Gates read the ledger, never ask.** At every operator-gated action
   (push, merge, deploy, flip, restart, secret-use, migrate — the typed
   vocabulary in `/autonomy-grant`):

   ```bash
   if python3 "$GATES_PY" check-grant "$RUN_ID" --action "$ACTION"; then
     # proceed; log the consumed grant + its artifact (sha/URL/PR#) in the report
   else
     python3 "$GATES_PY" pending "$RUN_ID" --action "$ACTION" \
       --reason "unlisted gate hit mid-run"
     # STOP this action path only — independent tasks continue; the final
     # report lists pendings so the morning resume is one `grant` command
   fi
   ```

   Never bypass with prose reasoning ("the operator clearly meant to allow
   this") — an unlisted action is by definition unreviewed. Never re-ask for
   a granted action — that is the interruption this mode exists to remove.

Without `--autonomous`, behavior is unchanged: operator gates prompt as
before. `SKIP_OPERATOR_GATES=1` (blanket, no floor) remains available for
trusted CI but is NOT what this mode does — the ledger keeps the
novel-action safety floor.

## Workflow

### Step 1: Resolve spec directory

```bash
SPEC_ARG="${ARGUMENTS:-}"
DRY_RUN=0
LOOP_ALL=1           # DEFAULT: run all tasks
ONE_TASK=0           # --one opts into single-task mode
# v1.1.0: ruflo is the default executor. RUFLO_REQUIRED=0 env var falls back
# to native Agent (debugging only — logs WARNING every spawn).
USE_RUFLO=1
RUFLO_REQUIRED="${RUFLO_REQUIRED:-1}"   # "1"=strict (default): hard-fail if ruflo unreachable; "0"=force native; "auto"=graceful fallback
AUTONOMOUS=0         # v3.18.0: --autonomous opts into ledger-gated unattended mode
NO_FINISH=0          # v1.12.0: finish tail (review-gate→ship→canary) default ON; --no-finish skips
# v1.3.0: auto mode is the default. Skips cost confirmation, runs without pauses.
# Disable with --no-auto.
AUTO_MODE=1
SPECIFIC_TASK=""
QA_LOOP=1
PHASE_AUDIT=1        # default on; --no-phase-audit sets to 0
QA_SKIP=""
QA_ONLY=""
RALPH_MAX_RETRIES="${RALPH_MAX_RETRIES:-3}"
# v1.3.1: US acceptance criteria injection for RALPH fix sub-agents
TASKS_JSON=""   # populated in Step 2; exported for sub-shells

# BUG-1 fix (2026-04-16): $SPEC_ARG may arrive as a single quoted string.
# zsh has no `read -a` (bash-only) — command-substitution output word-splits
# in bash AND zsh, with no subshell, so assignments in the loop persist.
for arg in $(printf '%s\n' "$SPEC_ARG"); do
  case "$arg" in
    --dry-run)    DRY_RUN=1 ;;
    --one)        ONE_TASK=1; LOOP_ALL=0 ;;
    --all)        LOOP_ALL=1 ;;
    --ruflo)      USE_RUFLO=1 ;;      # no-op; kept for explicitness
    --auto)       AUTO_MODE=1 ;;      # no-op; kept for explicitness
    --no-auto)    AUTO_MODE=0 ;;      # opt out: show cost estimate + confirm
    --task=*)     SPECIFIC_TASK="${arg#--task=}"; LOOP_ALL=0 ;;
    --qa-loop)         QA_LOOP=1 ;;
    --no-qa-loop)      QA_LOOP=0 ;;
    --no-phase-audit)  PHASE_AUDIT=0 ;;
    --qa-skip=*)  QA_SKIP="${arg#--qa-skip=}" ;;
    --qa-only=*)  QA_ONLY="${arg#--qa-only=}" ;;
    --autonomous) AUTONOMOUS=1 ;;    # v3.18.0: unattended; ledger-checked gates
    --no-finish)  NO_FINISH=1 ;;     # v1.12.0: skip Step 10 finish tail
    --resume)     : ;;   # handled in session checkpoint block
    [0-9][0-9][0-9]|[0-9][0-9][0-9]-*) SPEC_ID="$arg" ;;
  esac
done

# RUFLO_REQUIRED controls executor selection (code default = "1", line above —
# strict; fail FAST at start, not silently degraded mid-overnight-run):
#   "1" (default)   : hard-fail if ruflo unreachable
#   "auto"          : pre-flight mcp__ruflo__mcp_status; if unreachable → auto-switch to native parallel
#   "0"             : skip pre-flight, force native parallel Agent path immediately
if [ "$RUFLO_REQUIRED" = "0" ]; then
  USE_RUFLO=0
  echo "[feature-implement] INFO: RUFLO_REQUIRED=0 — native parallel Agent path forced." >&2
fi
# "auto" pre-flight happens at Step 5 (first task spawn attempt)

if [ -z "${SPEC_ID:-}" ]; then
  BRANCH=$(git branch --show-current 2>/dev/null)
  SPEC_ID=$(echo "$BRANCH" | grep -oE '^[0-9]{3}' | head -1)
fi
[ -z "$SPEC_ID" ] && { echo "ERROR: no spec ID. Usage: /feature-implement NNN"; exit 1; }

# v1.12.2: LEDGER run id — MUST equal what /feature-spec + /task-swarm used when
# recording preflight + grants (gates.py keys everything on this). Distinct from
# AUDIT_RUN_ID (run-state UUID for audits.jsonl) — do NOT overload. Set AFTER the
# branch-derived SPEC_ID fallback above (codex round 2: assigning before it left
# RUN_ID="spec-" on no-arg invocations, so ledger checks keyed the wrong run).
RUN_ID="spec-${SPEC_ID%%-*}"

SPEC_DIR=$(find specs -maxdepth 1 -type d -name "${SPEC_ID}-*" 2>/dev/null | head -1)
[ -z "$SPEC_DIR" ] && { echo "ERROR: specs/${SPEC_ID}-* not found"; exit 1; }

TASKS_FILE="$SPEC_DIR/tasks.md"
[ -f "$TASKS_FILE" ] || { echo "ERROR: $TASKS_FILE missing. Run /spec-decompose first."; exit 1; }

LOG_FILE="$SPEC_DIR/.implement-log.jsonl"
PATTERN_STORE_COUNT=0   # counter: triggers neural_train at 10
PRIOR_CONTEXT=""        # populated by memory_search_unified below
SESSION_ID_FILE="$SPEC_DIR/.implement-session-id"

# Session checkpoint — survives Claude context resets (Ruflo persists it)
SESSION_ID=$(mcp__ruflo__session_save({
  name: "feature-implement-" + SPEC_ID,
  metadata: { spec_id: SPEC_ID, tasks_file: TASKS_FILE, started_at: "<ISO timestamp>" }
}) 2>/dev/null | jq -r '.sessionId // empty' 2>/dev/null || echo "")
if [ -n "$SESSION_ID" ]; then
  echo "$SESSION_ID" > "$SESSION_ID_FILE"
  echo "[feature-implement] Session checkpoint: $SESSION_ID (resume: /feature-implement $SPEC_ID --resume)" >&2
fi

# On --resume: restore prior session state from Ruflo
if echo "$SPEC_ARG" | grep -q -- '--resume'; then
  _SAVED_ID=$(cat "$SESSION_ID_FILE" 2>/dev/null || echo "")
  if [ -n "$_SAVED_ID" ]; then
    mcp__ruflo__session_restore({ sessionId: "$_SAVED_ID" }) 2>/dev/null \
      && echo "[feature-implement] Restored session $_SAVED_ID" >&2 \
      || echo "[feature-implement] WARN: session restore failed — starting fresh" >&2
  fi
fi
```

### Step 1.5: Session prime (memory_search_unified)

Before parsing tasks, pull everything Ruflo knows about this spec across all memory namespaces (patterns, tasks, feedback, claude-memories). Inject into every sub-agent prompt this session.

```
PRIOR_CONTEXT = mcp__ruflo__memory_search_unified({
  query: "spec " + SPEC_ID + " implementation patterns approach"
})
# PRIOR_CONTEXT = top 3 results formatted as:
# "Prior pattern [{i}]: {content}" joined by \n
# Truncate to 800 chars total — context enrichment, not context flood
# If mcp unavailable or returns empty: PRIOR_CONTEXT = ""

# v1.5.0: Check if Ruflo model router has been pretrained.
# hooks_model-route returns "opus" for every task until pretrained — without this
# check, all sonnet-default tasks would be mis-routed to opus.
statsResult = mcp__ruflo__hooks_model-stats() 2>/dev/null
RUFLO_ROUTING_TRUSTED = (
  statsResult != null
  && statsResult.trained == true
  && (statsResult.sampleCount || 0) > 20
)
if !RUFLO_ROUTING_TRUSTED:
  echo "[feature-implement] WARN: Ruflo model router not pretrained — [model:] annotations used as-is."
  echo "  Run once to enable intelligent routing: npx claude-flow@v3alpha hooks pretrain"
  echo "  Until then: sonnet-default tasks stay sonnet (no dynamic upgrade/downgrade)."
# RUFLO_ROUTING_TRUSTED is checked in Step 5 before every hooks_model-route call.
```

### Step 2: Parse tasks.md

Extract all tasks into structured data. Each has:
- `id` (T001, T002, ...)
- `status` (`todo` / `done` / `failed` / `skipped`)
- `parallel` (boolean from `[P]`)
- `user_story` (from `[USn]`)
- `model` (from `[model:X]`; default `sonnet`)
- `thinking` (from `[thinking:Y]`; default `med`)
- `agent` (from `[agent:exact-agent]` or dispatch.py hybrid routing; defaults to `general-purpose` when neither an explicit annotation nor a catalog match is found)
- `description` (trailing text + backticked paths)
- `depends_on` (list from `Depends-on:` line)
- `phase` (current `## Phase N:` heading)

Python parsing via Bash heredoc:

```bash
# codex-gate (PR #11): resolve dispatch.py across all three install shapes —
# openclaw-monorepo vendored copy, standalone `setup.sh` install, and a plain
# git clone of this repo run directly from its own root.
DISPATCH=""
for _candidate in \
  "$(git rev-parse --show-toplevel 2>/dev/null)/packages/feature-fix-swarm/lib/dispatch.py" \
  "$HOME/.claude/lib/feature-fix-swarm/dispatch.py" \
  "$(git rev-parse --show-toplevel 2>/dev/null)/lib/dispatch.py"; do
  [ -f "$_candidate" ] && DISPATCH="$_candidate" && break
done
if [ -z "$DISPATCH" ]; then
  echo "ERROR: dispatch.py not found. Run setup.sh to install feature-fix-swarm." >&2
  exit 1
fi
TASKS_JSON=$(FILE="$TASKS_FILE" python3 "$DISPATCH" parse)
# Export for RALPH context extraction in Step 5.5b
export TASKS_JSON
```

### Step 3: Select next executable task

If `--task T###` specified → select that task (error if done or not found).

Otherwise, first task where:
- `status == "todo"`
- All `depends_on` IDs have `status == "done"`

Resolutions:
- No todos → print "All tasks complete for $SPEC_ID" and exit 0
- All blocked → print "Blocked: <task> waits for <deps>"; exit 1
- Prior `[F]` in deps chain → "Stopped on failure: <task>. Fix then reset to `[ ]`."; exit 1

### Step 4: Dry-run mode

If `--dry-run`, print the selected task and exit 0:

```
╔═══════════════════════════════════════════════════════════╗
║ DRY RUN — no agent will be spawned                        ║
╠═══════════════════════════════════════════════════════════╣
║ Task:     T042                                            ║
║ Phase:    Phase 3: User Story 1 — Launch Form             ║
║ Model:    sonnet / thinking: med                          ║
║ Agent:    backend-architect                                ║
║ US:       US1                                             ║
║ Depends:  T010, T011                                      ║
║ Desc:     Implement POST /api/onboarding/start in ...     ║
╚═══════════════════════════════════════════════════════════╝
```

### Step 5: Spawn the sub-agent

**Executor selection (v1.4.1):**
- **Default (ruflo MCP swarm):** `mcp__ruflo__swarm_init` + `mcp__ruflo__agent_spawn` (role registration) + native `Task()` calls (execution) + `SendMessage` (pipeline handoffs). Ruflo adds coordination, memory, and model routing on top of native Task execution. `workflow_execute` is NOT used — see v1.4.0 note below.
- **Codex tool discovery:** If this is a Codex session and Ruflo tools are not visible, first use tool discovery for `ruflo swarm_init agent_spawn mcp_status`. Codex lazy-loads MCP tools; absence before discovery is not proof that Ruflo is unavailable.
- **Pre-flight check (v1.4.1 — auto mode):** When `RUFLO_REQUIRED=auto` (default), call `mcp__ruflo__mcp_status` once before the first spawn — it is a swarm-agnostic health probe that works before any swarm exists (`mcp__ruflo__swarm_status` expects a live `swarmId` and is not a fit for pre-flight). If it fails, log `ruflo_unavailable` and **auto-switch to native parallel** — no exit, no user prompt. If `RUFLO_REQUIRED=1`, hard-fail on unreachable (legacy strict mode).
- **Native parallel fallback:** Active when ruflo is unavailable or `RUFLO_REQUIRED=0`. Runs [P] groups as concurrent Agent calls and respects `[model:]` annotations. See "Native Agent path" below.

**Native Agent path (parallel-capable):**

Model mapping — `[model:]` annotation → Agent `model` param:
| Annotation | Agent model param |
|---|---|
| `haiku` | `"haiku"` |
| `sonnet` (default) | `"sonnet"` |
| `opus` | `"opus"` |
| `fable` | `"fable"` _(native Agent only — Ruflo model enum is `haiku\|sonnet\|opus\|inherit`; `[model:fable]` on the Ruflo path silently maps to `sonnet` via Step B)_ |
| `gpt-5.4-mini` | Codex host: `"gpt-5.4-mini"`; Claude host: `"haiku"` |
| `gpt-5.4` | Codex host: `"gpt-5.4"`; Claude host: `"sonnet"` |
| `gpt-5.5` | Codex host: `"gpt-5.5"`; Claude host: `"opus"` |

Canonical `tasks.md` output should use `haiku` / `sonnet` / `opus`. The `gpt-5.4*` rows remain legacy aliases for old task files and hand-edited specs.

Parallel group dispatch: tasks with `[P]` in the same phase that share no mutual dependencies are spawned as **concurrent Agent calls** (sent in one message, not sequentially). Tasks without `[P]`, or with unresolved dependencies, run sequentially.

**Delegation discipline (Fable-mode):** parallelize only when stages are *genuinely* independent — disjoint file sets, no shared mutable state, no ordering dependency. Good signals: `[P]` tasks touching different files, independent verification, "build X while Y builds." Bad signal: splitting one coherent task across agents just to parallelize — handoff cost and merge ambiguity then exceed the speedup. When unsure whether two tasks are independent, run them sequentially; a wrong parallel split surfaces as a flaky merge, not a clean failure.

Algorithm per executable batch:
1. Collect all `todo` tasks whose `depends_on` are all `done`.
2. Split into `[P]` group (concurrent) and non-`[P]` singles (sequential).
3. For `[P]` group: issue all Agent calls in a single response turn with `model=task.model` on each.
4. For sequential tasks: issue one Agent call at a time, wait for completion, update checkbox, continue.

Agent tool params per task:
- `model`: task's `[model:]` annotation (canonical `haiku/sonnet/opus`, plus legacy Codex aliases and `fable`)
- `subagent_type`: `task.agent` (v2; `[agent:]` should already be an ECC agent type, and `lib/dispatch.py` backfills matches when missing; falls back to `general-purpose` when dispatch.py has no catalog match)
- `description`: task ID + first 3 description words
- `prompt`: template below

**Ruflo path (canonical v1.4.0 pattern):**

> v1.4.0 (2026-06-10): Rewrote Ruflo path to match actual canonical pattern from Ruflo docs.
> Prior versions used `workflow_create/execute` which is NOT the canonical Ruflo orchestration
> pattern. Ruflo orchestrates via `swarm_init` + `agent_spawn` + **concurrent native Task() calls**
> + `SendMessage` for pipeline handoffs. `workflow_create/execute` is a secondary API surface.
> `task_create` is for tracking only, not spawning.

**Step A — session start + initial memory prime (MANDATORY before first spawn):**
```
# Fire session-start hook (background workers, intelligence init)
mcp__ruflo__hooks_session-start({ context: "spec:" + SPEC_ID })

# Initial broad search — injected into all sub-agent prompts this session
# (required per Ruflo docs: search for prior patterns before every task run)
priorPatterns = mcp__ruflo__memory_search({
  query: "spec " + SPEC_ID + " feature implementation",
  limit: 5
})
# Inject top match into sub-agent prompts as "Prior pattern: ..."
```

**Step B — init swarm + register roles:**
```
# 1. Init coordination scaffolding
swarmId = mcp__ruflo__swarm_init({
  topology: "hierarchical",
  maxAgents: min(unique_agent_roles, 8),
  strategy: "specialized"
})

# 2. Register one agent per (role, model) tuple from tasks.md
#    v1.5.0: hooks_model-route overrides sonnet-default annotations when pretrained.
#    Explicit haiku/opus/fable annotations always win — only sonnet defaults get routed.
#    Ruflo's agent_spawn enum remains haiku|sonnet|opus|inherit. Codex-native
#    task annotations (gpt-5.4-mini/gpt-5.4/gpt-5.5) are mapped to equivalent Ruflo
#    tiers for coordination; the host executor maps back to Codex model ids for
#    actual codex exec.
#    fable not in Ruflo enum → use sonnet in Ruflo path (fable supported via native Agent).
COGNITIVE_MAP = {
  "ecc:tdd-guide":                "convergent",
  "test-automator":               "convergent",
  "ecc:typescript-reviewer":      "convergent",
  "frontend-developer":           "convergent",
  "ui-ux-designer":               "divergent",
  "accessibility-expert":         "critical",
  "python-pro":                   "convergent",
  "fastapi-pro":                  "convergent",
  "django-pro":                   "convergent",
  "typescript-pro":               "convergent",
  "javascript-pro":               "convergent",
  "security-auditor":             "critical",
  "backend-security-coder":       "critical",
  "frontend-security-coder":      "critical",
  "ecc:code-reviewer":            "convergent",
  "ecc:architect":                "systems",
  "backend-architect":            "systems",
  "graphql-architect":            "systems",
  "database-architect":           "systems",
  "database-optimizer":           "lateral",
  "database-admin":               "convergent",
  "ecc:refactor-cleaner":         "convergent",
  "ecc:build-error-resolver":     "lateral",
  "performance-engineer":         "lateral",
  "debugger":                     "lateral",
  "error-detective":              "lateral",
  "deployment-engineer":          "convergent",
  "cloud-architect":              "systems",
  "kubernetes-architect":         "systems",
  "terraform-specialist":         "convergent",
  "observability-engineer":       "systems",
  "docs-architect":               "convergent",
  "api-documenter":               "convergent",
  "reference-builder":            "convergent",
  "tutorial-engineer":             "convergent",
  "reverse-engineer":              "divergent",
  "context-manager":               "convergent",
  "prompt-engineer":               "divergent",
  "business-analyst":              "convergent",
  "sales-automator":               "convergent",
  "customer-support":              "convergent",
  "seo-meta-optimizer":            "convergent",
  "ecc:python-reviewer":           "convergent",
  "ecc:security-reviewer":         "critical",
  "ecc:performance-optimizer":     "lateral",
  "ecc:database-reviewer":         "systems",
  "analysis-research":             "divergent",
  "security":                      "critical",
  "architecture":                  "systems",
  "debugging":                     "lateral",
  "documentation":                 "convergent",
  # --- folded in from GitHub canonical v3.11.0 (2026-07-03 reconciliation) ---
  # Additional catalog roles not yet covered above. Where a role above overlaps
  # in spirit with one below (e.g. seo-meta-optimizer / ecc:seo-specialist),
  # both are kept as distinct dispatch.py catalog keys.
  "ecc:spec-miner":                "divergent",
  "ecc:doc-updater":               "convergent",
  "ecc:silent-failure-hunter":     "lateral",
  "ecc:pr-test-analyzer":          "divergent",
  "ecc:agent-evaluator":           "systems",
  "ecc:comment-analyzer":          "divergent",
  "ecc:conversation-analyzer":     "divergent",
  "ecc:loop-operator":             "systems",
  "ecc:harness-optimizer":         "systems",
  "ecc:seo-specialist":            "convergent",
  "ui-designer":                   "convergent",
  "ui-visual-validator":           "convergent",
  "java-pro":                      "convergent",
  "golang-pro":                    "convergent",
  "rust-pro":                      "convergent",
  "csharp-pro":                    "convergent",
  "php-pro":                       "convergent",
  "sql-pro":                       "systems",
  "incident-responder":            "critical",
  "network-engineer":              "systems",
  "mobile-developer":              "convergent",
  "ios-developer":                 "convergent",
  "flutter-expert":                "convergent",
  "flutter-reviewer":              "critical",
  "react-reviewer":                "critical",
  "vue-reviewer":                  "critical",
}
for (role, annotatedModel) in unique_role_model_pairs:
  // Resolve effective model: route only when annotation is default sonnet
  if annotatedModel == "sonnet" && RUFLO_ROUTING_TRUSTED:
    routeResult = mcp__ruflo__hooks_model-route({
      task: "spec " + SPEC_ID + " [" + role + "] tasks",
      preferCost: true
    })
    effectiveModel = (routeResult.confidence > 0.75) ? routeResult.model : "sonnet"
  elif annotatedModel == "gpt-5.4-mini":
    effectiveModel = "haiku"
  elif annotatedModel == "gpt-5.4":
    effectiveModel = "sonnet"
  elif annotatedModel == "gpt-5.5":
    effectiveModel = "opus"
  elif annotatedModel == "fable":
    effectiveModel = "sonnet"  // Ruflo enum: haiku|sonnet|opus|inherit only
  else:
    effectiveModel = annotatedModel  // haiku/opus/explicit annotation: trust as-is

  agentId = mcp__ruflo__agent_spawn({
    agentType: role,
    model: effectiveModel,
    task: "Execute spec " + SPEC_ID + " tasks tagged [agent:" + role + "]",
    swarmId: swarmId
  })
  # store agentId → (role, effectiveModel) map
```

**Step C — execute tasks (concurrent Task() + SendMessage):**

Ruflo's canonical orchestration runs named agents concurrently via the native Task tool, then
uses `SendMessage` for pipeline handoffs. This is the same mechanism the native parallel path
uses — Ruflo adds tracking and memory on top.

**Pre-spawn per task: hooks + memory + effort + DAA (v1.5.0)**

Before spawning each task:

```
# 1. Fire pre-task hook (automated learning, coverage routing, intelligence update)
mcp__ruflo__hooks_pre-task({
  taskId: task.id,
  description: task.description,
  phase: task.phase_n,
  spec: SPEC_ID
})

# 2. Per-task memory search (docs: "before EVERY task" — not just session start)
taskPatterns = mcp__ruflo__memory_search({
  query: task.description + " spec:" + SPEC_ID,
  limit: 3
})
# Merge with session-level priorPatterns; inject both into sub-agent prompt

# 3. Resolve effective model + thinking tier
# Look up effectiveModel registered for this task's role in Step B (roleModelMap built there)
effectiveModel = roleModelMap[task.agent] || task.model

# Effort correlation: align thinking budget with the resolved model tier.
# Mismatched pairs waste cost or under-utilize capability.
if effectiveModel in ["opus", "gpt-5.5"] && task.thinking == "med":
  effectiveThinking = "high"    # opus + med wastes capability; bump up
elif effectiveModel in ["haiku", "gpt-5.4-mini"] && task.thinking in ["high", "max"]:
  effectiveThinking = "med"     # haiku can't utilize max budget; cap down
else:
  effectiveThinking = task.thinking

# DAA cognitive pattern — only for high-effort tasks (thinking:high or :max).
# Uses COGNITIVE_MAP from Step B to select the richest cognitive context.
# Skip silently if daa_agent_create is unavailable.
daaAgentId = null
if effectiveThinking in ["high", "max"]:
  cogPattern = COGNITIVE_MAP[task.agent] || "convergent"
  daaResult = mcp__ruflo__daa_agent_create({
    cognitivePattern: cogPattern,
    taskContext: "spec " + SPEC_ID + " task " + task.id,
    model: effectiveModel
  }) 2>/dev/null
  daaAgentId = daaResult?.agentId || null
# daaAgentId injected into sub_agent_prompt when non-null:
#   "DAA cognitive context: {cogPattern} (id: {daaAgentId})"
```

Dispatch with the resolved pair (`effectiveModel`, `effectiveThinking`):

```
# For [P] task group: spawn all concurrently in ONE message turn
Task({ prompt: sub_agent_prompt(task1, effectiveThinking1), model: effectiveModel1,
       name: task1.id, run_in_background: true,
       subagent_type: task1.agent or "general-purpose" })
Task({ prompt: sub_agent_prompt(task2, effectiveThinking2), model: effectiveModel2,
       name: task2.id, run_in_background: true,
       subagent_type: task2.agent or "general-purpose" })
# ... all [P] siblings in the same turn

# For sequential tasks: one Task() at a time, wait, then next
Task({ prompt: sub_agent_prompt(task, effectiveThinking), model: effectiveModel,
       name: task.id, subagent_type: task.agent or "general-purpose" })

# Pipeline handoffs: when a task completes, route to next stage
SendMessage({ to: next_task_id, message: "Prior task complete. Proceed." })
```

**Step D — task tracking (optional but recommended):**
```
# Track tasks in Ruflo for status visibility (does NOT drive execution)
mcp__ruflo__task_create({
  type: "feature",
  description: task.description,
  priority: {P1→"high", P2→"normal", P3→"low"},
  tags: ["task_id:" + task.id, "model:" + task.model,
         "us:" + task.user_story, "phase:" + task.phase_n]
})
```

**Step E — post-task hook + store successful patterns (MANDATORY after each success):**
```
# 1. Fire post-task hook (automated learning, background workers, intelligence update)
mcp__ruflo__hooks_post-task({
  taskId: task.id,
  outcome: "success",
  spec: SPEC_ID,
  model: task.model,
  phase: task.phase_n
})

# 2. Persist to memory + agentdb (hooks don't replace explicit storage — both run;
#    required per Ruflo docs: store pattern after every successful task)
mcp__ruflo__memory_store({
  content: "spec " + SPEC_ID + " " + task.id + ": " + task.description + " [SUCCESS]",
  namespace: "patterns",
  metadata: { spec: SPEC_ID, task: task.id, model: task.model, phase: task.phase_n }
})
```

**Simpler alternative — `task_orchestrate`:**
For straightforward multi-agent coordination without full swarm scaffolding:
```
mcp__ruflo__task_orchestrate({
  tasks: [ { id: T.id, description: T.description, dependencies: T.depends_on,
              parallel: T.parallel, model: T.model } for T in todo_tasks ],
  strategy: "parallel"
})
# Handles batching, dependency ordering, and model routing internally
```

**Annotation→ruflo field mapping cheat-sheet (v1.4.0):**

| tasks.md annotation     | ruflo destination                                    |
| ----------------------- | ---------------------------------------------------- |
| `[P]` (same phase)      | Concurrent Task() calls in same message turn         |
| `[USn]`                 | Tag `us:USn` on `task_create`; injected in prompt    |
| `[model:X]`             | `model: X` on Task() + `agent_spawn` (per role)      |
| `[model:fable]`         | Falls back to `sonnet` in Ruflo router               |
| `[thinking:Y]`          | Injected into sub-agent prompt thinking budget line  |
| `[agent:exact-agent]`   | `agentType` on `agent_spawn`; `subagent_type` on Task|
| `[qa:dim1,dim2]`        | Tag `qa:dim1,dim2` read by QA phase gate             |
| `[return:X]`            | Return-contract section of sub-agent prompt (default derives from model tier) |
| `Depends-on: T###`      | Dependency ordering enforced before Task() spawn     |
| Phase heading           | `SendMessage` pipeline stage boundary                |

**Other Ruflo tools worth using:**

| Tool | When |
|---|---|
| `mcp__ruflo__hooks_session-start` | Once at session start — init background workers + intelligence |
| `mcp__ruflo__hooks_pre-task` | Before EVERY task spawn — learning + coverage routing |
| `mcp__ruflo__hooks_post-task` | After EVERY task completes — automated learning + pattern capture |
| `mcp__ruflo__memory_search` | Before EVERY task — reuse prior patterns (per-task, not just session) |
| `mcp__ruflo__memory_store` | After EVERY success — build knowledge base |
| `mcp__ruflo__neural_train` | After 10+ stored patterns — improve model |
| `mcp__ruflo__agentdb_pattern-search` | Find prior agent solutions by semantic query |
| `mcp__ruflo__agentdb_pattern-store` | Store reusable agent patterns |
| `mcp__ruflo__mcp_status` | Pre-flight availability check (swarm-agnostic; works before any swarm exists) |
| `mcp__ruflo__task_orchestrate` | Simple alternative to full swarm init |
| `mcp__ruflo__hive-mind_init` | Byzantine fault-tolerant consensus (high-stakes tasks) |
| `mcp__ruflo__memory_search_unified` | Cross-namespace search (patterns + tasks + feedback) |

**v1.4.0 failure policy:** On any `mcp__ruflo__*` failure (auth, timeout, schema mismatch, MCP unreachable):

**Pre-flight failure (RUFLO_REQUIRED=auto, default):**
1. Log `ruflo_unavailable` to `$LOG_FILE`:
   ```json
   {"timestamp":"<ISO>","event":"ruflo_unavailable","tool":"mcp__ruflo__mcp_status","error":"<msg>","fallback":"native_parallel"}
   ```
2. Print one-line notice: `[feature-implement] INFO: ruflo unreachable — switching to native parallel Agent path.`
3. Set `USE_RUFLO=0` and continue. No exit.

**Pre-flight failure (RUFLO_REQUIRED=1, strict):**
1. Log `ruflo_hard_fail` to `$LOG_FILE`:
   ```json
   {"timestamp":"<ISO>","event":"ruflo_hard_fail","tool":"<mcp_name>","error":"<msg>","task_id":null,"ruflo_required":"1"}
   ```
2. Print structured error:
   ```
   [feature-implement] ERROR: ruflo MCP unreachable (RUFLO_REQUIRED=1)
     tool:    mcp__ruflo__mcp_status
     error:   {message}
     log:     {LOG_FILE}
   Resolve:
     - Verify MCP server: `npx claude-flow@v3alpha --version`
     - Re-run: `npx claude-flow@v3alpha hooks pretrain`
     - Auto-fallback: unset RUFLO_REQUIRED (default is "auto")
   Resume: /feature-implement {SPEC_ID} --resume
   ```
3. Exit 1.

**Mid-run ruflo failure (task already started):**
1. Log the failure with task context.
2. Retry the task once via native Agent (single task, not the whole workflow).
3. If retry fails: mark task `failed`, continue to next task (do not abort the run).

**Pre-spawn: pattern search (both Ruflo + native paths)**

Before constructing the sub-agent prompt, search for prior solutions:

```
TASK_PATTERN = mcp__ruflo__agentdb_pattern-search({
  query: task.description[:80] + " spec " + SPEC_ID,
  limit: 1
})
# If result found: TASK_PRIOR = "Prior solution: " + result.content[:400]
# If no result or MCP unavailable: TASK_PRIOR = ""
```

**Sub-agent prompt:**

```
You are implementing a single task from a decomposed feature spec. You have no prior context for this conversation.

## Task
**ID:** {task_id}
**Phase:** {phase}
**User story:** {user_story or "N/A"}

{description}

## Context (read these files first, in order)
1. `{repo_root}/specs/{spec_dir}/plan.md` — architecture + chosen approach
2. `{repo_root}/specs/{spec_dir}/spec.md` (if exists) — user stories + acceptance
3. `{repo_root}/specs/{spec_dir}/tasks.md` — full task list (for dependencies)
4. `{repo_root}/specs/{spec_dir}/data-model.md` (if exists)
5. `{repo_root}/specs/{spec_dir}/contracts/` (if exists)
6. `{repo_root}/CLAUDE.md` — project conventions

## Prior patterns from similar tasks
{TASK_PRIOR or "(none — first time solving this type of task)"}

{PRIOR_CONTEXT or ""}

## Dependencies (must already be done — verify in tasks.md)
{depends_on_list or "(none)"}

## Thinking budget
Allocate {thinking} effort. {thinking_guidance}

## Return contract ({return_contract})
{return_contract_text}

Your final message IS the report — the orchestrator reads reports, not
transcripts. Exceeding the contract is a task failure even if the work is
correct.

## Context discipline
- Grep before read. Never open a file to find something searchable.
- Read ranges, not whole files: open the ~40 lines around the target.
- Never re-read a file already in your context unless you edited it.
- Never paste file contents into your report — cite file:line instead.

## Your job
1. Read the context files
2. Execute THIS task only — no scope creep, no "while I'm here" cleanups
3. If the task involves tests, write them first (RED), then implement (GREEN), then verify
4. Follow TDD rules from CLAUDE.md
5. If the task has a `Run: /skill-name` line, invoke that skill via the Skill tool (`Skill("skill-name")`) — do NOT try to reimplement the skill's logic manually
6. Report at the end: SUCCESS with a one-line summary, or FAILURE with what you tried and what blocked you

## Absolute rules
- Do NOT modify tasks.md (the orchestrator handles that)
- Do NOT execute tasks other than {task_id}
- Do NOT commit or push
- Do NOT create files outside what the task description specifies
- If the description is ambiguous, report FAILURE rather than guessing

Begin by reading the context files.
```

**Post-success: store patterns (both Ruflo + native paths)**

After each task completes `[X]` and checkbox is updated:

```
# 1. Structured pattern graph (agentdb)
mcp__ruflo__agentdb_pattern-store({
  pattern: task.id + ": " + task.description[:120],
  context: "spec:" + SPEC_ID + " phase:" + task.phase_n + " model:" + task.model,
  outcome: "success",
  metadata: { spec_id: SPEC_ID, task_id: task.id, user_story: task.user_story }
})

# 2. Vector search namespace (memory)
mcp__ruflo__memory_store({
  content: "SPEC " + SPEC_ID + " " + task.id + " SUCCESS: " + task.description[:150],
  namespace: "patterns",
  metadata: { spec: SPEC_ID, task: task.id, model: task.model, phase: task.phase_n }
})

# 3. Increment counter; trigger neural_train at threshold
PATTERN_STORE_COUNT=$((PATTERN_STORE_COUNT + 1))
if [ "$PATTERN_STORE_COUNT" -ge 10 ]; then
  mcp__ruflo__neural_train({ focus: "patterns" }) 2>/dev/null || true
  PATTERN_STORE_COUNT=0
fi
```

Skip silently if either MCP tool is unavailable — pattern storage is enhancement, not gate.

Where `thinking_guidance`:
- `low`: "Execute directly. Don't deliberate on obvious choices."
- `med`: "Think through the approach, consider 1-2 edge cases, implement carefully."
- `high`: "Think thoroughly. Consider edge cases, error paths, concurrency. Design before coding."
- `max`: "Maximum deliberation. Explore alternatives, adversarially challenge your approach, consider failure modes before implementing."

Where `{return_contract}` / `{return_contract_text}` come from `dispatch.py` parse
output (`return_contract` field) and `RETURN_CONTRACTS[kind]`. Default derives
from model tier (low→`scout`, mid→`build`, high→`deep`); a task can override
with `[return:scout|build|deep]`. Host-neutral — tiers, not model names.

### Task-failure escalation ladder (v3.16.0)

> **REFUTED is NOT a failure (v3.17.0).** If the sub-agent reports REFUTED —
> the task's premise/diagnosis checked against current HEAD is wrong — skip
> this ladder entirely: run
> `python3 lib/gates.py note-refuted {task_id} --reason "<evidence>"`, route
> the refutation through review-gate's refute-or-promote (one adversarial
> pass: "prove this refutation wrong"), and ONLY if it survives run
> `python3 lib/gates.py confirm-refuted {task_id}` — strict `verify-done`
> fails closed on an unconfirmed refutation, so the checkbox cannot flip
> before the adversarial check. Then flip `[X]` with a trailing
> `(REFUTED: <reason>)` annotation. Zero diff ships. If the refutation is
> itself refuted, do NOT confirm — treat as a normal FAILURE (first rung of
> the ladder).
> Retrying or escalating a refuted task burns budget forcing a fix onto a
> wrong premise.

When a task reports FAILURE (either path — ruflo or native):

1. **First failure → retry once on the SAME model** with a tighter prompt:
   append the failure report verbatim under `## Prior attempt (failed)` and
   narrow the scope to what blocked it.
2. **Second failure → escalate ONE tier up** (`python3 lib/dispatch.py` →
   `escalate_model(model)`: haiku→sonnet→opus; gpt-5.4-mini→gpt-5.4→gpt-5.5).
   The escalated prompt MUST carry BOTH prior failure reports forward — the
   next model never rediscovers what already failed. Record the failure
   signature via `gates.py note-failure` so `no_progress` sees the history.
3. **Top-tier failure (escalate_model → None) → mark task `failed`**, log, and
   continue to the next task (existing behavior). `no_progress` (same
   signature twice) still STOPs the run regardless of tier.

Never retry a third time on the same tier. Never skip the evidence carry-forward.

### Step 5.5: QA phase gate (when --qa-loop enabled)

After ALL tasks in the current `## Phase N:` heading complete with `[X]`:

1. **Deterministic hooks** (always run, $0 cost):
   - If vitest available: `npx vitest run --changed` on files modified this phase
   - If pytest available: `pytest -x` on changed Python files

2. **Browser context resolution — BEFORE any LLM QA** (v3.20.0, $0):

   ```bash
   mkdir -p ".ralph/${PHASE_SLUG}"
   if ! bash scripts/browser-proof.sh --diff "$PHASE_DIFF_FILES" \
        > ".ralph/${PHASE_SLUG}/browser-proof.txt" 2>&1; then
     cat ".ralph/${PHASE_SLUG}/browser-proof.txt"
     # NO-SERVER on a web-touching diff = phase QA FAIL (fail-not-skip).
     # Enter the RALPH retry loop with dimension=e2e, finding="no reachable
     # app". Fix = start the app or set QA_BASE_URL to a preview/prod URL
     # (preview/prod preferred — dev servers mask build failures).
     # QA_ALLOW_NO_SERVER=1 is the ONLY waiver, and it is recorded.
   fi
   # On success the file carries WEB-TOUCH:/DRIVER:/BASE-URL: directives that
   # the QA agents read (canary > playwright > agent, trust-descending).
   ```

3. **LLM QA swarm — hive-mind consensus** (~$0.15-0.20/phase):

   Spawn the QA agents under Byzantine fault-tolerant consensus. Each
   dimension is an independent broadcast; the hive aggregates verdicts before
   declaring pass/fail. **Maker/checker rule: QA agents are fresh-context
   evaluators — never the agent that implemented the phase.**

   ```
   # qa-design trigger: phase diff touches visual surfaces, OR the spec
   # carries a design-intent contract extracted from the plan's
   # /plan-design-review report (specs/NNN/design-intent.md)
   UI_RE='\.(tsx|jsx|vue|svelte|astro|html|css|scss|less)$|(^|/)(emails|templates)/'
   PHASE_HAS_UI=$(echo "$PHASE_DIFF_FILES" | tr ' ' '\n' | grep -cE "$UI_RE" || echo "0")

   N_AGENTS=3; [ "$PHASE_HAS_UI" -gt 0 ] && N_AGENTS=4

   hiveId = mcp__ruflo__hive-mind_init({
     name: "qa-phase-" + CURRENT_PHASE + "-spec-" + SPEC_ID,
     consensusThreshold: 0.67,
     maxAgents: N_AGENTS
   })

   # Core QA agents (always)
   Task({ name: "qa-e2e",      model: "sonnet", run_in_background: true,
          prompt: QA_E2E_PROMPT })      # prompts/qa-e2e.md — evidence-backed:
                                        # writes .ralph/<phase>/proof.json
   Task({ name: "qa-review",   model: "sonnet", run_in_background: true,
          prompt: QA_REVIEW_PROMPT })
   Task({ name: "qa-security", model: "sonnet", run_in_background: true,
          prompt: QA_SECURITY_PROMPT })
   # Design QA (when PHASE_HAS_UI) — prompts/qa-design.md drives the browser
   # itself and writes .ralph/<phase>/design-proof.json; grades against
   # specs/NNN/design-intent.md when present
   if (PHASE_HAS_UI > 0)
     Task({ name: "qa-design", model: "sonnet", run_in_background: true,
            prompt: QA_DESIGN_PROMPT })

   # Broadcast the phase diff + resolved browser context to all agents
   mcp__ruflo__hive-mind_broadcast({
     hiveId: hiveId,
     message: { phase: CURRENT_PHASE, diff: PHASE_DIFF_FILES, spec_dir: SPEC_DIR,
                browser_proof: ".ralph/" + PHASE_SLUG + "/browser-proof.txt",
                scenarios: SPEC_DIR + "/scenarios.md",
                qa_skip: QA_SKIP, qa_only: QA_ONLY }
   })

   verdict = mcp__ruflo__hive-mind_consensus({
     hiveId: hiveId,
     question: "Did all required QA dimensions pass?"
   })
   # verdict.result: "pass" | "fail" | "inconclusive"

   # Orchestrator-level gstack enhancement (Skill tool unavailable inside
   # ruflo agents): when /design-review exists in this environment, ALSO run
   # it on UI phases — its design-baseline.json artifacts strengthen the
   # design-proof bundle. Optional; the qa-design agent is the portable path.
   if (PHASE_HAS_UI > 0 && skillAvailable("design-review") && verdict.result === "pass") {
     Skill("design-review")
   }
   ```

   Fallback to `bash scripts/qa-swarm.sh` if hive-mind MCP unavailable.

   Dimensions (skip/only controlled by QA_SKIP / QA_ONLY):
   - **qa-e2e** (sonnet) — REAL-browser runthroughs of `specs/NNN/scenarios.md`
     BDD scenarios via the resolved driver (canary records trace/video/HAR;
     playwright; agent-driven as last resort). Functional coverage — buttons,
     forms, auth, navigation — not just page loads.
   - **qa-review** (sonnet) — code review on the diff (CRITICAL/HIGH = fail)
   - **qa-security** (sonnet) — OWASP scan on the diff (CRITICAL = fail)
   - **qa-design** (sonnet, UI phases) — visual review at 1440+375 against
     `specs/NNN/design-intent.md` / DESIGN.md tokens (CRITICAL/HIGH = fail)

4. **Proof enforcement + aggregation** (MANDATORY, both paths): after the
   agents report, run

   ```bash
   # --autonomous runs: export RUNTIME_PROOF_STRICT=1 first — unattended
   # verification must reject driver=agent (self-reported evidence tier)
   QA_SCENARIOS="$SPEC_DIR/scenarios.md" \
   bash scripts/qa-swarm.sh --aggregate --phase "$CURRENT_PHASE" \
     --diff "$PHASE_DIFF_FILES" --spec-dir "$SPEC_DIR"
   ```

   This re-reads `.ralph/<phase>/*-result.json` and REJECTS any e2e/design
   "pass" whose proof bundle fails `python3 lib/runtime_proof.py verify` —
   an agent claiming success without verified browser evidence (curl-200,
   wrong-page screenshot, soft-404, unread console, zero interactions, stale
   or non-image artifacts, a claimed driver that doesn't match the resolved
   one, or a bundle covering fewer scenarios than scenarios.md defines)
   fails here regardless of the hive verdict. A hive "pass" + aggregate
   exit 1 = phase QA FAIL. Then record the browser gate into the evidence
   ledger so the phase's `[qa:browser]` task checkbox is legal:

   ```bash
   python3 lib/gates.py run-gate T0XX -- \
     python3 lib/runtime_proof.py verify ".ralph/${PHASE_SLUG}/proof.json" \
       --scenarios "$SPEC_DIR/scenarios.md"
   ```

   ALL dimensions must pass (hive verdict = "pass" AND aggregate exit 0). Any failure triggers:
   - Capture artifacts to `.ralph/P{N}/` (logs, screenshots, diff)
   - Invoke `/investigate` with scope locked to changed files
   - Apply fix via sub-agent with investigation report
   - Re-run `/qa-only` on affected area
   - Retry up to RALPH_MAX_RETRIES (default 3)
   - On final retry fail: mark all remaining phase tasks `[F]`, stop

5. **Structured failure output** (printed to terminal on any QA fail):
   ```
   [RALPH] Phase {N} FAIL (retry {R}/{MAX})
     dimension: {dim}
     file: {path}:{line}
     message: {one-line summary}
     artifacts: .ralph/P{N}/
     resume: /feature-implement {NNN} --resume
   ```

6. **First-run banner** (shown once per session when QA_LOOP=1):
   ```
   [RALPH] QA loop enabled: 2 test hooks + 3 LLM agents run after each phase.
           Disable: --no-qa-loop | Skip dims: --qa-skip e2e,security
           Docs: docs/harness/qa-ralph-loop.md
   ```

Skip the QA phase gate entirely when `QA_LOOP=0`.

### Step 5.5b: Retry loop on QA failure

When `scripts/qa-swarm.sh` exits non-zero (any dimension failed):

1. **Capture failed dimensions** from `$ARTIFACT_DIR/*-result.json` (parse each JSON, collect dims with status "fail")

2. **Invoke `scripts/ralph-retry.sh`** with:
   ```bash
   bash scripts/ralph-retry.sh \
     --phase "$CURRENT_PHASE" \
     --failed-dims "$FAILED_DIMS" \
     --spec-dir "$SPEC_DIR" \
     --artifact-dir "$ARTIFACT_DIR" \
     --max-retries "$RALPH_MAX_RETRIES" \
     --diff-files "$PHASE_DIFF_FILES"
   ```

3. **Read retry-signal.txt** — the retry script writes `INVESTIGATE:<dir>:<context>:<dims>`

4. **Invoke `/investigate`** via Skill tool with scope-lock:
   - Pass the context file from ralph-retry.sh as input
   - Scope: only files changed in this phase (from `$PHASE_DIFF_FILES`)
   - The investigate skill uses 5 Whys methodology and produces a root cause report

5. **Extract US acceptance criteria** for the failing task before spawning the fix agent:

   ```bash
   # Get the failing task's [USn] annotation
   _FAIL_TASK_ID=$(grep -oP '(?<=task_id:)\S+' "$ARTIFACT_DIR/retry-state.json" 2>/dev/null | head -1 || echo "")
   _FAIL_TASK_US=$(python3 -c "
   import json, os
   tasks = json.loads(os.environ.get('TASKS_JSON', '[]'))
   tid = '$_FAIL_TASK_ID'
   t = next((t for t in tasks if t['id'] == tid), {})
   print(t.get('user_story') or '')
   " 2>/dev/null || echo "")

   # Pull acceptance criteria from spec.md for that US
   _FIX_US_CRITERIA=""
   if [ -n "$_FAIL_TASK_US" ] && [ -f "$SPEC_DIR/spec.md" ]; then
     _US_NUM="${_FAIL_TASK_US#US}"
     _FIX_US_CRITERIA=$(awk "/User Story $_US_NUM[^0-9]|^#{1,3} US$_US_NUM[^0-9]/,/^#{1,3} /" \
       "$SPEC_DIR/spec.md" 2>/dev/null | head -50)
   fi
   ```

6. **Search for prior fix patterns** before spawning fix agent:

   ```
   FIX_PRIOR = mcp__ruflo__agentdb_pattern-search({
     query: "QA fix " + FAILED_DIMS + " " + _FAIL_TASK_ID + " " + task.description[:60],
     limit: 1
   })
   # FIX_PRIOR_TEXT = result.content[:400] if found, else ""
   ```

7. **Spawn fix sub-agent** with the investigation report + spec context + prior fix pattern:
   - Same model as the original implementation sub-agent
   - Prompt includes: root cause report, original task description, failed test output, user story acceptance criteria, **and** prior fix pattern from agentdb

   **Fix sub-agent prompt addition** — append this section to the standard fix prompt:

   ```
   ## Prior fix pattern (from agentdb — similar past QA failures)
   {FIX_PRIOR_TEXT or "(none found — no prior fix pattern for this failure type)"}

   ## User Story Acceptance Criteria (from spec.md)
   Task {task_id} belongs to {user_story}.
   The fix must satisfy these acceptance criteria — not just make the test pass:

   {_FIX_US_CRITERIA or "spec.md acceptance criteria unavailable — rely on task description only"}

   ## What success looks like
   The test was: {failed test name / assertion}
   Root cause: {investigate report summary}
   Fix scope: minimal — do not change behavior outside this US acceptance criterion.
   ```

8. **Store fix pattern after success** (so future RALPH retries learn from it):

   ```
   mcp__ruflo__agentdb_pattern-store({
     pattern: "QA fix " + FAILED_DIMS + " for " + _FAIL_TASK_ID + ": " + fix_summary[:120],
     context: "spec:" + SPEC_ID + " retry:" + current_retry,
     outcome: "success"
   })
   ```

9. **Write fixed-signal.txt** to let ralph-retry.sh re-run QA on failed dims only

10. **Loop** until either:
   - All dimensions pass → continue to next phase
   - Max retries exhausted → mark remaining phase tasks `[F]`, print structured failure, stop

**Structured output on retry exhaustion:**
```
[RALPH] Phase {N} FAILED after {MAX} retries
  Still failing: {dims}
  Root causes investigated: {N}
  Fixes attempted: {N}
  Artifacts: .ralph/{phase-slug}/
  Next: /feature-implement {NNN} --resume
```

**Orphaned-WIP adoption (v3.17.0):** before rebuilding a task from scratch on
resume, check whether a stalled parallel worktree already holds coherent
uncommitted work for it: confirm the worktree is stale (mtimes, no live
session), read the uncommitted diff, and assess coherence (compiles or
plausibly compiles, uses existing APIs correctly, sound design). Adopt when
coherent and faster than rebuilding — rebase onto current main, fill gaps, add
the RED proof, and run the normal gates; the adopted diff gets NO gate
discount. Rebuild when incoherent; wait when the session may still be active.

**Resume semantics:** On `--resume` after a retry failure, the skill reads `.ralph/{phase-slug}/retry-state.json` to determine:
- Which phase failed
- Which dimensions were still failing
- How many retries were used
- Whether to restart the phase or just re-run failed QA dims

### Step 5.6: Per-phase codex hostile audit

Runs after Step 5.5 QA gate passes (all dims green). This is the DRY equivalent of
`/feature` Step 4b — having it here means standalone `feature-implement` calls get the
same per-phase adversarial coverage as `/feature`-orchestrated runs.

Skip entirely when:
- `PHASE_AUDIT=0` (`--no-phase-audit` flag set)
- `~/.claude/bin/run-state` binary absent (log WARNING, continue)
- `QA_LOOP=0` (phase audit requires QA gate context to be meaningful)

```bash
if [ "$PHASE_AUDIT" = "1" ] && [ "$QA_LOOP" = "1" ] && command -v ~/.claude/bin/run-state &>/dev/null; then

  # 1. Extract spec slice for this phase (awk state-machine — same pattern as /feature Step 4b)
  PHASE_SPEC=$(awk -v phase="$CURRENT_PHASE" '
    BEGIN {in_section=0}
    /^## / {
      if (in_section) {exit}
      if (index($0, phase)) {in_section=1; print; next}
    }
    in_section {print}
  ' "$SPEC_DIR/plan.md" 2>/dev/null | head -200)

  # 2. Resolve changed files for this phase
  WEDGE_FILES_FILE=".context/feature/${SPEC_ID}/wedges/${CURRENT_PHASE}.files"
  if [ -f "$WEDGE_FILES_FILE" ]; then
    PHASE_FILES=$(cat "$WEDGE_FILES_FILE")
  else
    TASKS_IN_PHASE=$(python3 -c "
import json, os
tasks = json.loads(os.environ.get('TASKS_JSON','[]'))
phase = os.environ.get('CURRENT_PHASE','')
print(sum(1 for t in tasks if phase.lower() in (t.get('phase') or '').lower()))
" 2>/dev/null || echo "1")
    PHASE_FILES=$(git diff --name-only "HEAD~${TASKS_IN_PHASE}" 2>/dev/null || git diff --name-only HEAD~1)
  fi

  # 3. Extract US acceptance criteria for this phase
  PHASE_US_TAGS=$(python3 -c "
import json, os
tasks = json.loads(os.environ.get('TASKS_JSON','[]'))
phase = os.environ.get('CURRENT_PHASE','')
us = sorted(set(t.get('user_story') for t in tasks
    if t.get('user_story') and phase.lower() in (t.get('phase') or '').lower()) - {None})
print(' '.join(us))
" 2>/dev/null || echo "")

  PHASE_US_CRITERIA=""
  if [ -n "$PHASE_US_TAGS" ] && [ -f "$SPEC_DIR/spec.md" ]; then
    while IFS= read -r _us_tag; do
      _us_num="${_us_tag#US}"
      _section=$(awk "/User Story $_us_num[^0-9]|^#{1,3} US$_us_num[^0-9]/,/^#{1,3} /" \
        "$SPEC_DIR/spec.md" 2>/dev/null | head -40)
      [ -n "$_section" ] && PHASE_US_CRITERIA+="=== $_us_tag Acceptance Criteria ===
$_section
"
    done <<< "$(echo "$PHASE_US_TAGS" | tr ' ' '\n')"
  fi

  # 4. Ensure a run-state id exists for audits.jsonl tracking
  # (v1.12.1: AUDIT_RUN_ID, NOT $RUN_ID — that is the gates.py ledger key spec-NNN;
  # overloading them sent ledger lookups to a run-state UUID and stalled --autonomous)
  if [ -z "${AUDIT_RUN_ID:-}" ]; then
    AUDIT_RUN_ID=$(~/.claude/bin/run-state start --skill feature-implement \
      --objective "spec $SPEC_ID phase $CURRENT_PHASE" 2>/dev/null | jq -r .run_id 2>/dev/null || echo "")
  fi

  # 5. Run the hostile audit
  PHASE_AUDIT_EXIT=0
  if [ -n "$AUDIT_RUN_ID" ]; then
    ~/.claude/bin/run-state audit "$AUDIT_RUN_ID" \
      --kind phase \
      --context "PHASE_NAME=$CURRENT_PHASE" \
      --context "PHASE_SPEC=${PHASE_SPEC:-none}" \
      --context "PHASE_DIFF=$(git diff --stat HEAD~1 2>/dev/null | head -50)" \
      --context "US_ACCEPTANCE=${PHASE_US_CRITERIA:-none}" \
      --cwd "$(git rev-parse --show-toplevel)" \
      || PHASE_AUDIT_EXIT=$?
  else
    echo "[feature-implement] WARNING: could not get run_id for phase audit — skipping" >&2
    PHASE_AUDIT_EXIT=0
  fi

  # 6. Decision
  # exit 0 = pass, exit 1 = fail (re-enter ralph), exit 2+ = error (retry once then skip)
  if [ "$PHASE_AUDIT_EXIT" = "1" ]; then
    echo "[feature-implement] Phase audit FAIL for $CURRENT_PHASE — re-entering ralph retry loop" >&2
    # Re-enter Step 5.5b with FAILED_DIMS="phase-audit"
    FAILED_DIMS="phase-audit"
    # (ralph retry loop handles from here — same path as QA failure)
  elif [ "$PHASE_AUDIT_EXIT" -ge 2 ]; then
    echo "[feature-implement] WARNING: phase audit error (exit $PHASE_AUDIT_EXIT) — retrying once" >&2
    ~/.claude/bin/run-state audit "$AUDIT_RUN_ID" --kind phase \
      --context "PHASE_NAME=$CURRENT_PHASE" --context "retry=1" \
      --cwd "$(git rev-parse --show-toplevel)" 2>/dev/null \
      || echo "[feature-implement] WARNING: phase audit retry also failed — skipping (codex-gate remains the hard gate)" >&2
  fi
  # Hard cap: max 3 phase-audit attempts per phase (tracked via ralph retry counter)

fi

# 7. Phase truth score (v1.10.0 — wires the documented 0.95 rollback for real).
# PHASE_TASK_IDS = the task IDs completed in this phase (from the stage map).
# zsh-safe split: unquoted scalars do NOT word-split in zsh; command-substitution
# output splits in both shells (same pattern as the arg loops elsewhere).
# GATES_STRICT=1: caller-recorded evidence counts as missing here too.
if ! GATES_STRICT=1 python3 "$GATES_PY" phase-score $(printf '%s\n' "$PHASE_TASK_IDS"); then
  echo "[feature-implement] TRUTH-SCORE below 0.95 for $CURRENT_PHASE — rolling back" >&2
  # Roll back to the phase-start checkpoint (recorded before the phase began):
  #   git stash push -m "phase-rollback-$CURRENT_PHASE"  (preserves work for autopsy)
  # then revert the phase's tasks.md flips [X] → [ ] and re-enter planning for
  # this phase. Do NOT proceed to the next phase on a sub-threshold score.
  exit 1
fi
```

### Step 6: Capture result

Record start time. When Agent returns:
- **SUCCESS**: record machine evidence FIRST, then flip the checkbox. The checkbox
  flip is only legal when `verify-done` exits 0 — agent self-report is never
  completion authority:

  ```bash
# resolve gates.py across the three install shapes (same order as dispatch.py)
  GATES_PY=""
  for _cand in \
    "$(git rev-parse --show-toplevel 2>/dev/null)/packages/feature-fix-swarm/lib/gates.py" \
    "$HOME/.claude/lib/feature-fix-swarm/gates.py" \
    "$(git rev-parse --show-toplevel 2>/dev/null)/lib/gates.py"; do
    [ -f "$_cand" ] && GATES_PY="$_cand" && break
  done
  if [ -z "$GATES_PY" ]; then
    echo "[gates] FATAL: gates.py not found — run setup.sh. Refusing to mark tasks done unverified."
    exit 1
  fi
  # PREFERRED: let gates.py execute the gate itself so the exit code is real,
  # not caller-supplied (an agent cannot fabricate evidence this way).
  # zsh-safe: $GATE_CMD unquoted does not word-split in zsh — split via
  # command substitution (pre-existing on main; fixed in the v3.14 gate round):
  python3 "$GATES_PY" run-gate "$TASK_ID" -- $(printf '%s\n' "$GATE_CMD")
  # HARD STOP: no passing evidence → the checkbox MUST stay [ ]. Mark the task
  # [F] and enter the retry path — do NOT continue to the [X] flip.
  # GATES_STRICT=1 (v1.10.0): caller-recorded evidence (record-gate) is rejected;
  # only run-gate/run-red runner-executed evidence flips a checkbox.
  GATES_STRICT=1 python3 "$GATES_PY" verify-done "$TASK_ID" || {
    echo "[feature-implement] BLOCK: no runner-verified gate evidence for $TASK_ID"
    # no-progress wiring (v1.10.0): same failure signature twice = stuck loop.
    # Signature = the gate's stored failure_sig (the discriminating FAILED/
    # Error lines run-gate extracted — NOT the final summary line, which
    # different failures share). Fallback is nonempty; gates.py additionally
    # ignores blank signatures. Never re-run anything here.
    SIG=$(python3 - "$TASK_ID" <<'PY'
import json, os, sys
store = os.environ.get("GATES_STORE", ".feature-fix-swarm/evidence.json")
try:
    gate = json.load(open(store)).get(sys.argv[1], {}).get("gate", {})
except Exception:
    gate = {}
# failure_sig only — tests_after is the generic summary footer shared by
# unrelated failures (codex round 3 P3); missing sig → task-scoped sentinel.
sig = (gate.get("failure_sig") or "").strip()
print(sig[:400] or f"{sys.argv[1]} gate nonzero")
PY
)
    if ! python3 "$GATES_PY" note-failure "$TASK_ID" --sig "$SIG"; then
      echo "[feature-implement] NO-PROGRESS on $TASK_ID — stopping run for human review"
      mark_task_failed "$TASK_ID"
      break   # stop the whole loop: burning retries on an identical failure
    fi
    mark_task_failed "$TASK_ID"   # [ ] → [F]; retries per RALPH_MAX_RETRIES
    continue
  }
  echo "$TASK_ID gate exit=$GATE_EXIT $TESTS_BEFORE → $TESTS_AFTER" >> .feature-fix-swarm/results.md
  ```

  Then Edit tasks.md `- [ ] {task_id}` → `- [X] {task_id}`
- **FAILURE** (error, timeout, or sub-agent reported failure): Edit tasks.md `- [ ] {task_id}` → `- [F] {task_id}`
- **REFUTED** (sub-agent proved the task's diagnosis wrong at HEAD): `gates.py note-refuted {task_id} --reason "..."` → adversarial check via review-gate refute-or-promote → if it survives, `gates.py confirm-refuted {task_id}` → Edit tasks.md `- [ ] {task_id}` → `- [X] {task_id} (REFUTED: <short reason>)`. `verify-done` accepts a refutation (prints `DONE-REFUTED`) — under `GATES_STRICT=1` only a CONFIRMED one — so the checkbox-evidence hook passes exactly when the protocol was followed. If the refutation is itself refuted, do NOT confirm; treat as a normal FAILURE (first rung of the escalation ladder).

Append to `.implement-log.jsonl`:

```json
{"timestamp":"<ISO 8601 UTC>","task_id":"<id>","status":"success|failed","model":"<model>","thinking":"<level>","agent":"<agent>","duration_s":<seconds>,"summary":"<one-line summary>","qa_results":{"unit":"pass|fail|skip","integration":"pass|fail|skip","e2e":"pass|fail|skip","review":"pass|fail|skip","security":"pass|fail|skip"}}
```

Note: `qa_results` only present on the LAST task in each phase (the one that triggers the phase gate). Individual task entries don't have it.

### Step 7: Loop or stop

- Default (run-all + auto): success + more todos → Step 3
- Default (run-all + auto): any `[F]` → stop, print summary, exit 1
- `--one`: stop after 1 task regardless of outcome
- `--task T###`: stop after that specific task

### Step 8: Final report

**Self-critique first (Fable-mode discipline 3):** before printing the box, name at least one residual risk — a task marked `[X]` on self-report alone, an acceptance criterion not exercised by QA, or a fix that passed only on retry. Put it on the `Residual risk:` line; never emit a silent all-green.

```
╔═══════════════════════════════════════════════════════════╗
║ /feature-implement report — spec {NNN}                    ║
╠═══════════════════════════════════════════════════════════╣
║ Tasks executed this session:  N                           ║
║   SUCCESS:  X                                             ║
║   FAILED:   Y                                             ║
║ Total in tasks.md:                                        ║
║   [X] done:    A / T (N%)                                 ║
║   [F] failed:  B                                          ║
║   [ ] todo:    C                                          ║
║ Residual risk: <one-line from self-critique, or "none surfaced"> ║
║ Log: {LOG_FILE}                                           ║
║ Next: {next id} / "all done" / "blocked on X"             ║
╚═══════════════════════════════════════════════════════════╝
```

**Proof artifact (v1.11.0):** emit the machine-readable proof of the run before
the box — one claim per phase task, live-vs-structural evidence kind, go/no-go:

```bash
# Deferral reasons contain spaces — build argv as an ARRAY; command
# substitution word-splits %q output and corrupts multi-word reasons
# (codex v3.15 round 1 P2).
PROOF_ARGS=("$RUN_ID")
while IFS= read -r tid; do [ -n "$tid" ] && PROOF_ARGS+=("$tid"); done \
  <<< "$(printf '%s\n' "$ALL_TASK_IDS" | tr ' ' '\n')"
for d in "${DEFERRALS[@]}"; do PROOF_ARGS+=(--defer "$d"); done
GATES_STRICT=1 python3 "$GATES_PY" proof "${PROOF_ARGS[@]}"
# writes .feature-fix-swarm/proof-<run>.json; exit 1 = no-go (report FAILED, not done)
```

**Deferral records (v1.11.0):** anything intentionally NOT verified this run
(a live send, an operator-gated flip, an env the harness can't reach) MUST be
a named deferral — passed via `--defer "name: reason"` above AND appended to
`.feature-fix-swarm/residuals.md` as `- [ ] {date} {name}: {reason} (run {RUN_ID})`.
A deferral that isn't named in both places is a silent pass — forbidden, and
enforced mechanically: `proof` reads `residuals.md` next to the evidence store
and returns **no-go** (`DEFERRAL-UNRECORDED`) for any `--defer` whose name is
absent from it. Append the residuals.md line BEFORE running `proof`. The
final report's `Residual risk:` line references residuals.md when non-empty.

### Step 9: Retro — consolidate learning (v1.10.0)

Per-task signals (`hooks_model-outcome`, `agentdb_pattern-store`) fire during the run;
this step consolidates them so the NEXT run starts smarter (ReasoningBank pattern —
distill trajectories into reusable strategy, not just raw outcomes):

1. Aggregate: tasks run, gate pass/fail counts per rung, retries used, phase scores,
   any NO-PROGRESS stops (all read from `.feature-fix-swarm/evidence.json` +
   `results.md` — never from memory of the session).
2. Distill at most 3 patterns worth keeping (a failure mode + its fix, a routing
   win, a gate that caught a real bug) → `mcp__ruflo__agentdb_pattern-store`, one
   entry each, tagged with the spec ID.
3. Append one summary line to `.feature-fix-swarm/results.md`:
   `RUN {spec} done={X}/{T} retries={R} phase_scores={...} patterns_stored={N}`.
4. If ruflo is unreachable, skip 2 silently (retro must never fail the run) — but
   still write 3.

   **Optional durable recall (v1.12.0, fail-soft):** if `command -v gbrain` succeeds
   and `env -u DATABASE_URL gbrain doctor` is healthy, also store the distilled
   patterns durably: `env -u DATABASE_URL gbrain put spec/{NNN}-retro "<one-line
   distilled pattern>"` then `env -u DATABASE_URL gbrain sync --no-pull --no-embed`.
   gbrain absent/unhealthy → skip silently; agentdb + results.md remain the record.

### Step 10: Finish tail — review-gate → ship → canary (v1.12.0)

`/feature` is retired; this skill is now the terminal executor. After the final
report shows a **go** proof artifact and QA green, run the finish tail. Mechanical
entry check — all three must hold, else print the skip reason and END:
`[ "$NO_FINISH" = "0" ]` AND zero `[ ]`/`[F]` tasks remain AND proof verdict = go
(never ship a partial run).

Each stage that takes an OUTWARD action is gated by the autonomy ledger in
`--autonomous` mode, or by an operator prompt in attended mode:

```bash
gate() {  # gate <action> — 0=proceed, 1=stop this path (pending recorded)
  local ACTION="$1"
  if [ "$AUTONOMOUS" = "1" ]; then
    if python3 "$GATES_PY" check-grant "$RUN_ID" --action "$ACTION"; then
      return 0   # proceed; log consumed grant + artifact (sha/PR#/URL) in report
    fi
    python3 "$GATES_PY" pending "$RUN_ID" --action "$ACTION" \
      --reason "finish-tail gate not granted"
    echo "[finish-tail] STOP at $ACTION — recorded pending; resume after grant"
    return 1
  fi
  # attended: ask the operator now (AskUserQuestion / prompt), record their yes
  return 0  # only after explicit yes
}
```

1. **Review gate:** invoke the `review-gate` skill (full-run diff). HIGH/CRITICAL
   findings block the tail — fix or stop. No ledger action needed (read-only).
2. **Ship:** `gate "push:origin/<branch>"` (and `gate "merge:pr"` if the flow
   merges) → invoke the `ship` skill. Not granted → STOP tail, everything stays
   local and committed-not-pushed; morning resume = grant + re-run tail.
3. **Canary:** after ship lands, `gate "deploy:<target>"` if canary exercises a
   deploy; then invoke the `canary` skill. Failure → report FAILED loudly with the
   rollback line from the grant screen — never auto-rollback without a
   `rollback:` grant.
4. Consumed grants + artifacts (commit sha, PR number, deploy URL) go in the final
   report; the proof artifact already recorded the evidence chain.

Hosts without `/ship`//`canary` installed (bare OSS consumers): print the manual
equivalent (`git push`, open PR, smoke command) and stop — never improvise an
outward action a skill would have gated.

## Edge cases

- **Malformed `[model:]`**: default sonnet/med, log warning in JSONL (`"warning":"annotation defaulted"`).
- **Sub-agent timeout (>15 min)**: mark `[F]`, log `reason: "timeout"`.
- **Depends-on cycle**: topological sort fails at parse; abort with `ERROR: cycle T042 → T043 → T042`.
- **User interrupt mid-execution**: task stays `[ ]`. Next invocation picks up fresh.
- **tasks.md edited between invocations**: re-read on every invoke. New tasks handled, removed tasks ignored.
- **`[F]` blocking progress**: user reverts to `[ ]` to retry or manually sets `[X]` if complete.
- **Zero tasks**: print "No tasks defined. Run /spec-decompose first."

## Non-goals (v1)

- `[P]` parallel groups: Ruflo path runs them concurrently (concurrent Task() calls per group). Native fallback also dispatches `[P]` tasks concurrently (all `[P]` siblings in one message turn). Non-`[P]` tasks are always sequential.
- Task execution does NOT commit/push; outward actions happen ONLY in the Step 10
  finish tail via the `ship`/`canary` child skills, each behind a `check-grant`
  (autonomous) or explicit operator yes (attended). `--no-finish` restores the old
  stop-at-QA behavior.
- With `--qa-loop` (default): validates correctness per phase via 2 deterministic hooks + 3 LLM agents. Without: assumes sub-agent self-report. Full-suite `/qa` still recommended before shipping.
- Does NOT update Linear — `post-spec-write.sh` handles that on tasks.md save.

## Safety rules

- Never modify files outside the project directory
- Never push, deploy, or delete branches DURING task execution — those actions are
  legal only inside the Step 10 finish tail, through the ship/canary skills, after
  their `gate()` check passes (v1.12.0)
- Sub-agents inherit rules via CLAUDE.md
- Destructive action requests re-surface to user (Agent tool respects Claude Code permissions)
- **Ruflo failures hard-stop under the default `RUFLO_REQUIRED=1`.** Fallback to
  native happens ONLY via explicit opt-in: `RUFLO_REQUIRED=auto` (pre-flighted,
  logged fallback) or `RUFLO_REQUIRED=0` (forced native, debugging). Never a
  silent unrequested fallback (v1.12.1 — reconciled with the auto policy).

## Cost estimation

Per-task cost (values in USD, backticked to avoid markdown `$` expansion issues):
- `[model:haiku thinking:low]`: `~$0.02`
- `[model:sonnet thinking:med]`: `~$0.30`
- `[model:sonnet thinking:high]`: `~$0.60`
- `[model:fable thinking:med]`: `~$0.50` _(estimated; native Agent path only — Ruflo maps fable→sonnet)_
- `[model:opus thinking:max]`: `~$2.00`

Before `--all` on a large spec, estimate:
```bash
h=$(grep -c '\[model:haiku' "$TASKS_FILE" || echo 0)
sm=$(grep -c '\[model:sonnet thinking:med' "$TASKS_FILE" || echo 0)
sh=$(grep -c '\[model:sonnet thinking:high' "$TASKS_FILE" || echo 0)
o=$(grep -c '\[model:opus' "$TASKS_FILE" || echo 0)
echo "Estimated cost: \$$(FILE="$TASKS_FILE" python3 "$DISPATCH" cost)"
```

Display and prompt for confirmation only when `AUTO_MODE=0` (`--no-auto` flag). In default auto mode, print the estimate but proceed without waiting.
</content>
