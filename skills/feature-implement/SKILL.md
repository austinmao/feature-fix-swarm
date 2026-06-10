---
name: feature-implement
description: "Execute tasks.md via ruflo swarm + auto mode (both default in v1.3.0), respecting [model:] [agent:] annotations. v1.4.0: Fable 5 support + provider-agnostic model routing (Anthropic/OpenAI) with full model IDs passed to Ruflo. Updates checkboxes on completion. --auto skips cost confirmation and runs without pauses. Native Agent fallback only via RUFLO_REQUIRED=0 env override (debugging only)."
version: "1.4.0"
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
/feature-implement [NNN] --qa-skip e2e         # skip specific QA dimensions
/feature-implement [NNN] --qa-only review,security  # run only these QA dimensions
```

**v1.3.0 defaults:** Both `--ruflo` and `--auto` are on by default. You never need to pass them.
- `--auto`: skip cost confirmation prompt, run all tasks without pausing. Opt out with `--no-auto`.
- `--ruflo`: ruflo MCP swarm executor. Opt out with `RUFLO_REQUIRED=0` env override (debug only).

**v1.1.0 ruflo policy:**
- Ruflo is the default and only supported executor.
- If `mcp__ruflo__*` tools are unreachable AND `RUFLO_REQUIRED=1` (default): hard-fail with structured error. No silent fallback.
- Escape hatch: `RUFLO_REQUIRED=0 /feature-implement NNN` falls back to native Agent for debugging. Logs WARNING on every spawn.
- The `--no-ruflo` flag from v1.0.0 has been removed. To run native, use the env override above.

**Default is run-all.** The skill loops through every `[ ]` task until either:
- All tasks are `[X]` (success)
- Any task returns `[F]` (failure — stop, report, user fixes and reruns)
- A dependency cycle is detected (abort)

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
RUFLO_REQUIRED="${RUFLO_REQUIRED:-1}"
# v1.3.0: auto mode is the default. Skips cost confirmation, runs without pauses.
# Disable with --no-auto.
AUTO_MODE=1
SPECIFIC_TASK=""
QA_LOOP=1
QA_SKIP=""
QA_ONLY=""
RALPH_MAX_RETRIES="${RALPH_MAX_RETRIES:-3}"

# BUG-1 fix (2026-04-16): $SPEC_ARG may arrive as a single quoted string.
# Use `read -ra` to explicitly word-split into an array so the for-loop iterates.
read -ra _SPEC_ARGS <<< "$SPEC_ARG"
for arg in "${_SPEC_ARGS[@]}"; do
  case "$arg" in
    --dry-run)    DRY_RUN=1 ;;
    --one)        ONE_TASK=1; LOOP_ALL=0 ;;
    --all)        LOOP_ALL=1 ;;
    --ruflo)      USE_RUFLO=1 ;;      # no-op; kept for explicitness
    --auto)       AUTO_MODE=1 ;;      # no-op; kept for explicitness
    --no-auto)    AUTO_MODE=0 ;;      # opt out: show cost estimate + confirm
    --task=*)     SPECIFIC_TASK="${arg#--task=}"; LOOP_ALL=0 ;;
    --qa-loop)    QA_LOOP=1 ;;
    --no-qa-loop) QA_LOOP=0 ;;
    --qa-skip=*)  QA_SKIP="${arg#--qa-skip=}" ;;
    --qa-only=*)  QA_ONLY="${arg#--qa-only=}" ;;
    [0-9][0-9][0-9]|[0-9][0-9][0-9]-*) SPEC_ID="$arg" ;;
  esac
done

# v1.1.0: respect RUFLO_REQUIRED=0 escape hatch. When set, switch executor to
# native and emit a clear WARNING so the user sees the degraded mode every run.
if [ "$RUFLO_REQUIRED" = "0" ]; then
  USE_RUFLO=0
  echo "[feature-implement] WARNING: RUFLO_REQUIRED=0 — running native Agent (debug mode). Hardened ruflo path is bypassed." >&2
fi

if [ -z "${SPEC_ID:-}" ]; then
  BRANCH=$(git branch --show-current 2>/dev/null)
  SPEC_ID=$(echo "$BRANCH" | grep -oE '^[0-9]{3}' | head -1)
fi
[ -z "$SPEC_ID" ] && { echo "ERROR: no spec ID. Usage: /feature-implement NNN"; exit 1; }

SPEC_DIR=$(find specs -maxdepth 1 -type d -name "${SPEC_ID}-*" 2>/dev/null | head -1)
[ -z "$SPEC_DIR" ] && { echo "ERROR: specs/${SPEC_ID}-* not found"; exit 1; }

TASKS_FILE="$SPEC_DIR/tasks.md"
[ -f "$TASKS_FILE" ] || { echo "ERROR: $TASKS_FILE missing. Run /spec-decompose first."; exit 1; }

LOG_FILE="$SPEC_DIR/.implement-log.jsonl"

# v1.2.0: Provider detection + full model ID resolution.
# Priority: CLAUDE_MODEL_PROVIDER env > ANTHROPIC_API_KEY > OPENAI_API_KEY > default anthropic.
PROVIDER="${CLAUDE_MODEL_PROVIDER:-}"
if [ -z "$PROVIDER" ]; then
  [ -n "${ANTHROPIC_API_KEY:-}" ] && PROVIDER="anthropic"
  [ -n "${OPENAI_API_KEY:-}" ]    && PROVIDER="${PROVIDER:-openai}"
  PROVIDER="${PROVIDER:-anthropic}"
fi

# resolve_model SHORTHAND → full model ID for the active provider.
# Valid shorthands: haiku | sonnet | opus | fable
resolve_model() {
  local s="$1"
  case "${PROVIDER}:${s}" in
    anthropic:haiku)  echo "claude-haiku-4-5" ;;
    anthropic:sonnet) echo "claude-sonnet-4-6" ;;
    anthropic:opus)   echo "claude-opus-4-8" ;;
    anthropic:fable)  echo "claude-fable-5" ;;
    openai:haiku)     echo "gpt-4o-mini" ;;
    openai:sonnet)    echo "gpt-4o" ;;
    openai:opus)      echo "o1" ;;
    openai:fable)     echo "gpt-5.4" ;;
    *)                echo "claude-sonnet-4-6" ;;
  esac
}
echo "[feature-implement] Provider: $PROVIDER"
```

### Step 2: Parse tasks.md

Extract all tasks into structured data. Each has:
- `id` (T001, T002, ...)
- `status` (`todo` / `done` / `failed` / `skipped`)
- `parallel` (boolean from `[P]`)
- `user_story` (from `[USn]`)
- `model` (from `[model:X]`; default `sonnet`)
- `thinking` (from `[thinking:Y]`; default `med`)
- `agent` (from `[agent:dept/role]`; default `engineering/backend-engineer`)
- `description` (trailing text + backticked paths)
- `depends_on` (list from `Depends-on:` line)
- `phase` (current `## Phase N:` heading)

Python parsing via Bash heredoc:

```bash
TASKS_JSON=$(FILE="$TASKS_FILE" python3 <<'PYEOF'
import os, re, json
with open(os.environ["FILE"]) as f:
    content = f.read()

task_pattern = re.compile(
    r'^- \[([ XxFfSs])\] (T\d+)([^\n]*)$(?:\n[ ]{2,}Depends-on:\s*([^\n]*))?',
    re.MULTILINE
)
phase_pattern = re.compile(r'^## Phase [^\n]*$', re.MULTILINE)

phase_starts = [(m.start(), m.group()) for m in phase_pattern.finditer(content)]

def phase_for(pos):
    last = None
    for start, header in phase_starts:
        if start <= pos: last = header
        else: break
    return last or "(no phase)"

def parse_annotations(rest):
    parallel = bool(re.search(r'\[P\]', rest))
    us_match = re.search(r'\[US(\d+)\]', rest)
    user_story = f"US{us_match.group(1)}" if us_match else None
    model, thinking = "sonnet", "med"
    m = re.search(r'\[model:([a-z]+)(?:\s+thinking:([a-z]+))?\]', rest)
    if m:
        model = m.group(1)
        if m.group(2): thinking = m.group(2)
    m2 = re.search(r'\[thinking:([a-z]+)\]', rest)
    if m2: thinking = m2.group(1)
    agent = "engineering/backend-engineer"
    m = re.search(r'\[agent:([a-z/\-]+)\]', rest)
    if m: agent = m.group(1)
    qa_match = re.search(r'\[qa:([a-z,]+)\]', rest)
    qa_dims = qa_match.group(1).split(",") if qa_match else ["e2e","review","security"]
    desc = re.sub(r'\[(?:P|US\d+|model:[^\]]+|thinking:[^\]]+|agent:[^\]]+|qa:[^\]]+)\]', '', rest).strip()
    return {"parallel": parallel, "user_story": user_story, "model": model,
            "thinking": thinking, "agent": agent, "qa_dims": qa_dims, "description": desc}

tasks = []
status_map = {' ':'todo','X':'done','x':'done','F':'failed','f':'failed','S':'skipped','s':'skipped'}
for m in task_pattern.finditer(content):
    deps_raw = m.group(4) or ""
    deps = [d.strip() for d in deps_raw.split(",") if d.strip().startswith("T")]
    ann = parse_annotations(m.group(3))
    tasks.append({
        "id": m.group(2),
        "status": status_map.get(m.group(1), "todo"),
        "phase": phase_for(m.start()),
        "depends_on": deps,
        **ann,
    })

print(json.dumps(tasks, indent=2))
PYEOF
)
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
║ Agent:    engineering/backend-engineer                    ║
║ US:       US1                                             ║
║ Depends:  T010, T011                                      ║
║ Desc:     Implement POST /api/onboarding/start in ...     ║
╚═══════════════════════════════════════════════════════════╝
```

### Step 5: Spawn the sub-agent

**Executor selection (v1.1.0):**
- **Default (ruflo MCP swarm):** spawn via `mcp__ruflo__swarm_init` + `mcp__ruflo__task_create` + `mcp__ruflo__workflow_execute`. Supports parallel `[P]` groups. Requires `npx claude-flow@v3alpha hooks pretrain` to have been run at least once.
- **Pre-flight check (v1.1.0 — REQUIRED):** Before spawning the first task, call `mcp__ruflo__swarm_status` (or any `mcp__ruflo__*` tool) once to verify the MCP server is reachable. If the call fails AND `RUFLO_REQUIRED=1` (default): print structured error and exit 1. **No silent fallback.**
- **Native fallback (DEBUG ONLY):** Set `RUFLO_REQUIRED=0` in env. The skill will run native Agent sequentially (no `[P]` parallelism). Every task spawn emits a WARNING to stderr.

**Native Agent path (default):**

Agent tool invocation:
- `model`: task's `[model:]` annotation
- `subagent_type`: `general-purpose` (v1; `[agent:]` is informational until mapped to specialized subagent_types in v2)
- `description`: task ID + first 3 description words
- `prompt`: template below

**Ruflo path (`--ruflo` flag):**

> v1.2.0 (2026-05-27): Aligned with actual ruflo MCP schemas. Earlier versions
> referenced `task_create({model, agent_role, depends_on})` which doesn't exist —
> ruflo `task_create` accepts only `{type, description, priority, assignTo, tags}`.
> Model + dependencies are now plumbed through `agent_spawn` + tags + workflow steps.

On first task in session, initialize the swarm with one agent per unique `[agent:]` role:

```
# 1. Init the swarm
mcp__ruflo__swarm_init({
  topology: "hierarchical",       // deps between tasks → hierarchical
  maxAgents: {unique [agent:] count, capped at 8},
  strategy: "specialized",
  config: { consensus: "raft" }   // consensus rides in config, not top-level
})
```

<<<<<<< HEAD
Then, for each unique `[agent:]` value found in tasks.md, spawn one tracked agent
that owns that role for the rest of the workflow:
=======
For each task group (a `[P]` block or a single sequential task):

> v1.2.0: `task_create` tags now carry both `model:<shorthand>` and
> `model_id:<full-id>` so `mcp__ruflo__hooks_model-route` can consume the
> exact model string without needing to resolve shorthands itself.
>>>>>>> 2a879bc (feat(model-routing): Fable 5 + provider-agnostic model routing v1.2.0/v1.1.0)

```
# 2. Spawn one agent per role (model from the first task that needs it)
for role in unique_agents:
  mcp__ruflo__agent_spawn({
    agentType: role,                                  // e.g. "engineering/backend-engineer"
    model: {model from first task using this agent},  // "haiku" | "sonnet" | "opus"
    task: {short brief — "Execute spec NNN tasks tagged [agent:{role}]"},
    swarmId: {swarmId from step 1}
  })
  # capture agentId
```

For each task in tasks.md, create the task with metadata in `tags`, then assign to
the matching agent:

```
# 3a. Create task (model + thinking + phase + US ride in tags)
taskId = mcp__ruflo__task_create({
  type: "feature",                            // "feature" | "bugfix" | "research" | "refactor"
  description: {task description},
<<<<<<< HEAD
  priority: {priority_map[task.priority] or "normal"},
  // priority_map: P1 → "high", P2 → "normal", P3 → "low", P0/critical → "critical"
  assignTo: [{agentId for task's [agent:] role}],
  tags: [
    "task_id:" + task.id,                     // e.g. "task_id:T015"
    "model:" + task.model,
    "thinking:" + task.thinking,
    "phase:" + task.phase_n,
    "us:" + (task.user_story or "none"),
    "qa:" + ",".join(task.qa_dims),
  ]
})

# 3b. Express dependencies through a workflow (task_create has no depends_on field)
mcp__ruflo__workflow_create({
  name: "spec-" + SPEC_ID + "-feature-implement",
  description: "feature-implement orchestration for spec " + SPEC_ID,
  steps: [
    // One step per task in tasks.md, in topological order
    // [P] tasks in the same phase become a single "parallel" step
    { type: "task", name: "T001", config: { taskId: "<id from create>" } },
    { type: "parallel", name: "phase-1-audits", config: {
        children: [
          { type: "task", name: "T002", config: { taskId: "..." } },
          { type: "task", name: "T003", config: { taskId: "..." } },
        ]
      }
    },
    { type: "task", name: "T015", config: { taskId: "...", dependsOn: ["T019"] } },
    // ... rest of phases
  ]
=======
  model: resolve_model({task's [model:] value}),   // full ID: "claude-fable-5" | "claude-opus-4-8" | "claude-sonnet-4-6" | "claude-haiku-4-5" | openai equivalents
  agent_role: {task's [agent:] value},
  depends_on: {list of task_ids from Depends-on:},
  tags: ["model:" + task.model, "model_id:" + resolve_model(task.model), "provider:" + PROVIDER]
>>>>>>> 2a879bc (feat(model-routing): Fable 5 + provider-agnostic model routing v1.2.0/v1.1.0)
})
```

Then execute and poll:

```
# 4. Run + poll
mcp__ruflo__workflow_execute({ workflowId: {id from workflow_create} })
# poll mcp__ruflo__task_status({taskId}) for each task
# when each task completes: Edit tasks.md [ ] → [X] or [F]
```

<<<<<<< HEAD
**Annotation→ruflo field mapping cheat-sheet:**

| tasks.md annotation     | ruflo destination                         |
| ----------------------- | ----------------------------------------- |
| `[P]` (same phase)      | Sibling under `parallel` step in workflow |
| `[USn]`                 | Tag `us:USn` on `task_create`             |
| `[model:X]`             | `model: X` on `agent_spawn` (per role)    |
| `[thinking:Y]`          | Tag `thinking:Y` (passed into prompt)     |
| `[agent:dept/role]`     | `agentType` for `agent_spawn`             |
| `[qa:dim1,dim2]`        | Tag `qa:dim1,dim2` (read by QA gate)      |
| `Depends-on: T###`      | `dependsOn` in workflow step `config`     |
| Phase heading           | Tag `phase:N` + workflow step grouping    |

**Single-model-per-agent constraint:** Because model selection happens at
`agent_spawn` time (not per task), if two tasks share an `[agent:]` role but
specify different `[model:]` values, the skill MUST spawn one agent per
(role, model) tuple — agent IDs become `{role}-{model}` (e.g.
`engineering-backend-engineer-haiku` vs `-sonnet`). The `assignTo` for each
task picks the right tuple.
=======
**Annotation→ruflo field mapping cheat-sheet (v1.2.0):**

| tasks.md annotation     | ruflo destination                                                     |
| ----------------------- | --------------------------------------------------------------------- |
| `[P]` (same phase)      | Parallel group in workflow                                            |
| `[USn]`                 | Informational (pass in description)                                   |
| `[model:X]`             | `model: resolve_model(X)` (full ID) on `task_create`; tag `model:X` (shorthand) + `model_id:<full-id>` for `hooks_model-route` |
| `[thinking:Y]`          | Informational (pass in description / prompt)                          |
| `[agent:dept/role]`     | `agent_role` on `task_create`                                         |
| `[qa:dim1,dim2]`        | Informational (read by QA gate)                                       |
| `Depends-on: T###`      | `depends_on` list on `task_create`                                    |
>>>>>>> 2a879bc (feat(model-routing): Fable 5 + provider-agnostic model routing v1.2.0/v1.1.0)

**v1.1.0 failure policy:** On any `mcp__ruflo__*` failure (auth, timeout, schema mismatch, MCP unreachable):

1. Capture the failure detail (tool name, error message, current task ID, timestamp).
2. Append a structured error log to `$LOG_FILE` (`.implement-log.jsonl`):
   ```json
   {"timestamp":"<ISO>","event":"ruflo_hard_fail","tool":"<mcp_name>","error":"<msg>","task_id":"<id|null>","ruflo_required":"1"}
   ```
3. Print the structured terminal error:
   ```
   [feature-implement] ERROR: ruflo MCP failure during {phase}
     tool:    {mcp__ruflo__xxx}
     error:   {message}
     task:    {current task id}
     log:     {LOG_FILE}
   Resolve:
     - Verify MCP server is running and reachable
     - Re-run `npx claude-flow@v3alpha hooks pretrain` if pretrain state is corrupt
     - Check ruflo version: `npx claude-flow@v3alpha --version`
     - To bypass for debugging: `RUFLO_REQUIRED=0 /feature-implement {SPEC_ID} --resume`
   Resume after fix: /feature-implement {SPEC_ID} --resume
   ```
4. **Exit 1.** Do NOT fall back to native Agent. The user must explicitly opt into degraded mode via `RUFLO_REQUIRED=0`.

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

## Dependencies (must already be done — verify in tasks.md)
{depends_on_list or "(none)"}

## Thinking budget
Allocate {thinking} effort. {thinking_guidance}

## Your job
1. Read the context files
2. Execute THIS task only — no scope creep, no "while I'm here" cleanups
3. If the task involves tests, write them first (RED), then implement (GREEN), then verify
4. Follow TDD rules from CLAUDE.md
5. Report at the end: SUCCESS with a one-line summary, or FAILURE with what you tried and what blocked you

## Absolute rules
- Do NOT modify tasks.md (the orchestrator handles that)
- Do NOT execute tasks other than {task_id}
- Do NOT commit or push
- Do NOT create files outside what the task description specifies
- If the description is ambiguous, report FAILURE rather than guessing

Begin by reading the context files.
```

Where `thinking_guidance`:
- `low`: "Execute directly. Don't deliberate on obvious choices."
- `med`: "Think through the approach, consider 1-2 edge cases, implement carefully."
- `high`: "Think thoroughly. Consider edge cases, error paths, concurrency. Design before coding."
- `max`: "Maximum deliberation. Explore alternatives, adversarially challenge your approach, consider failure modes before implementing."

### Step 5.5: QA phase gate (when --qa-loop enabled)

After ALL tasks in the current `## Phase N:` heading complete with `[X]`:

1. **Deterministic hooks** (always run, $0 cost):
   - If vitest available: `npx vitest run --changed` on files modified this phase
   - If pytest available: `pytest -x` on changed Python files

2. **LLM QA swarm** (3 agents via ruflo, ~$0.15/phase):
   Run `bash scripts/qa-swarm.sh` with args:
   - `--phase "$CURRENT_PHASE"` — which phase just completed
   - `--diff "$(git diff --name-only HEAD~$TASKS_IN_PHASE)"` — changed files
   - `--spec-dir "$SPEC_DIR"` — for user story context
   - `--qa-skip "$QA_SKIP"` — dimensions to skip (from CLI flag)
   - `--qa-only "$QA_ONLY"` — dimensions to run exclusively
   - `--max-retries "$RALPH_MAX_RETRIES"` — retry budget

   The swarm spawns up to 3 LLM agents in parallel via ruflo:
   - **qa-e2e** (sonnet) — browser tests via $B if dev server detected (`curl -sf localhost:3000`)
   - **qa-review** (sonnet) — code review on the diff (CRITICAL/HIGH = fail)
   - **qa-security** (sonnet) — OWASP scan on the diff (CRITICAL = fail)

3. **Aggregation**: ALL dimensions must pass. Any failure triggers:
   - Capture artifacts to `.ralph/P{N}/` (logs, screenshots, diff)
   - Invoke `/investigate` with scope locked to changed files
   - Apply fix via sub-agent with investigation report
   - Re-run `/qa-only` on affected area
   - Retry up to RALPH_MAX_RETRIES (default 3)
   - On final retry fail: mark all remaining phase tasks `[F]`, stop

4. **Structured failure output** (printed to terminal on any QA fail):
   ```
   [RALPH] Phase {N} FAIL (retry {R}/{MAX})
     dimension: {dim}
     file: {path}:{line}
     message: {one-line summary}
     artifacts: .ralph/P{N}/
     resume: /feature-implement {NNN} --resume
   ```

5. **First-run banner** (shown once per session when QA_LOOP=1):
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

5. **Spawn fix sub-agent** with the investigation report:
   - Same model as the original implementation sub-agent
   - Prompt includes: root cause report, original task description, failed test output
   - Sub-agent applies the minimal fix and reports SUCCESS/FAILURE

6. **Write fixed-signal.txt** to let ralph-retry.sh re-run QA on failed dims only

7. **Loop** until either:
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

**Resume semantics:** On `--resume` after a retry failure, the skill reads `.ralph/{phase-slug}/retry-state.json` to determine:
- Which phase failed
- Which dimensions were still failing
- How many retries were used
- Whether to restart the phase or just re-run failed QA dims

### Step 6: Capture result

Record start time. When Agent returns:
- **SUCCESS**: Edit tasks.md `- [ ] {task_id}` → `- [X] {task_id}`
- **FAILURE** (error, timeout, or sub-agent reported failure): Edit tasks.md `- [ ] {task_id}` → `- [F] {task_id}`

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
║ Log: {LOG_FILE}                                           ║
║ Next: {next id} / "all done" / "blocked on X"             ║
╚═══════════════════════════════════════════════════════════╝
```

## Edge cases

- **Malformed `[model:]`**: default `sonnet`/`med`, log warning in JSONL (`"warning":"annotation defaulted"`). Valid shorthands: `haiku` | `sonnet` | `opus` | `fable`.
- **Sub-agent timeout (>15 min)**: mark `[F]`, log `reason: "timeout"`.
- **Depends-on cycle**: topological sort fails at parse; abort with `ERROR: cycle T042 → T043 → T042`.
- **User interrupt mid-execution**: task stays `[ ]`. Next invocation picks up fresh.
- **tasks.md edited between invocations**: re-read on every invoke. New tasks handled, removed tasks ignored.
- **`[F]` blocking progress**: user reverts to `[ ]` to retry or manually sets `[X]` if complete.
- **Zero tasks**: print "No tasks defined. Run /spec-decompose first."

## Non-goals (v1)

- Does NOT run `[P]` parallel groups in parallel — sequential only. v2 feature.
- Does NOT commit — use `/ship`.
- With `--qa-loop` (default): validates correctness per phase via 2 deterministic hooks + 3 LLM agents. Without: assumes sub-agent self-report. Full-suite `/qa` still recommended before shipping.
- Does NOT update Linear — `post-spec-write.sh` handles that on tasks.md save.

## Safety rules

- Never modify files outside the project directory
- Never push, deploy, or delete branches
- Sub-agents inherit rules via CLAUDE.md
- Destructive action requests re-surface to user (Agent tool respects Claude Code permissions)
- **Ruflo failures hard-stop (v1.1.0).** No silent native fallback. Use `RUFLO_REQUIRED=0` env override for debugging only.

## Cost estimation

Per-task cost (values in USD, backticked to avoid markdown `$` expansion issues):
- `[model:haiku thinking:low]`: `~$0.02` (claude-haiku-4-5 / gpt-4o-mini)
- `[model:sonnet thinking:med]`: `~$0.30` (claude-sonnet-4-6 / gpt-4o)
- `[model:sonnet thinking:high]`: `~$0.60`
- `[model:opus thinking:max]`: `~$2.00` (claude-opus-4-8 / o1)
- `[model:fable thinking:max]`: `~$5.00` (claude-fable-5 / gpt-5.4 — highest capability, use sparingly)

Before `--all` on a large spec, estimate:
```bash
h=$(grep -c '\[model:haiku' "$TASKS_FILE" || echo 0)
sm=$(grep -c '\[model:sonnet thinking:med' "$TASKS_FILE" || echo 0)
sh=$(grep -c '\[model:sonnet thinking:high' "$TASKS_FILE" || echo 0)
o=$(grep -c '\[model:opus' "$TASKS_FILE" || echo 0)
f=$(grep -c '\[model:fable' "$TASKS_FILE" || echo 0)
echo "Estimated cost: \$$(python3 -c "print(f'{$f*5.00 + $o*2.00 + $sh*0.60 + $sm*0.30 + $h*0.02:.2f}')")"
```

Display and prompt for confirmation only when `AUTO_MODE=0` (`--no-auto` flag). In default auto mode, print the estimate but proceed without waiting.
