---
name: feature-implement
description: "Execute tasks.md one task at a time via Agent tool, respecting [model:] and [agent:] annotations. Updates checkboxes on completion."
version: "1.0.0"
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
/feature-implement [NNN]              # DEFAULT: execute ALL tasks until done or first failure
/feature-implement [NNN] --one        # execute only the next unchecked task (single task)
/feature-implement [NNN] --dry-run    # print what would execute, don't spawn
/feature-implement [NNN] --task T042  # execute a specific task ID only
/feature-implement [NNN] --ruflo      # use ruflo swarm executor (parallel [P] groups, experimental)
/feature-implement [NNN] --no-qa-loop          # skip per-phase QA (faster, less safe)
/feature-implement [NNN] --qa-skip e2e         # skip specific QA dimensions
/feature-implement [NNN] --qa-only review,security  # run only these QA dimensions
```

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
USE_RUFLO=0          # --ruflo opts into swarm executor
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
    --ruflo)      USE_RUFLO=1 ;;
    --task=*)     SPECIFIC_TASK="${arg#--task=}"; LOOP_ALL=0 ;;
    --qa-loop)    QA_LOOP=1 ;;
    --no-qa-loop) QA_LOOP=0 ;;
    --qa-skip=*)  QA_SKIP="${arg#--qa-skip=}" ;;
    --qa-only=*)  QA_ONLY="${arg#--qa-only=}" ;;
    [0-9][0-9][0-9]|[0-9][0-9][0-9]-*) SPEC_ID="$arg" ;;
  esac
done

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

**Executor selection:**
- **Default (native Agent tool):** one task at a time via Claude Code's Agent tool. Sequential even when tasks have `[P]`. Proven, low-risk.
- **`--ruflo` flag:** spawn via ruflo MCP swarm. Supports parallel `[P]` groups via `mcp__ruflo__swarm_init` + `mcp__ruflo__task_create` + `mcp__ruflo__workflow_execute`. Experimental — requires `npx claude-flow@v3alpha hooks pretrain` to have been run at least once. Falls back to native Agent on ruflo unavailability.

**Native Agent path (default):**

Agent tool invocation:
- `model`: task's `[model:]` annotation
- `subagent_type`: `general-purpose` (v1; `[agent:]` is informational until mapped to specialized subagent_types in v2)
- `description`: task ID + first 3 description words
- `prompt`: template below

**Ruflo path (`--ruflo` flag):**

On first task in session:
```
mcp__ruflo__swarm_init({
  topology: "hierarchical",  // deps between tasks → hierarchical makes sense
  max_agents: {count unique [agent:] values in tasks.md, capped at 8},
  consensus: "raft"
})
```

For each task group (a `[P]` block or a single sequential task):
```
mcp__ruflo__task_create({
  description: {task description},
  model: {task's [model:] value},
  agent_role: {task's [agent:] value},
  depends_on: {list of task_ids from Depends-on:}
})
```

Then:
```
mcp__ruflo__workflow_execute({workflow_id})
# poll mcp__ruflo__task_status per task
# when each task completes: Edit tasks.md [ ] → [X] or [F]
```

On any `mcp__ruflo__*` failure (auth, timeout, schema mismatch), log the error and fall back to native Agent tool for the remaining tasks. Do NOT abort — fallback is transparent.

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

- `--all` + success + more todos → Step 3
- `--all` + failure → stop, print summary, exit 1
- Default (no `--all`) → stop after 1 task

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

- **Malformed `[model:]`**: default sonnet/med, log warning in JSONL (`"warning":"annotation defaulted"`).
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

## Cost estimation

Per-task cost (values in USD, backticked to avoid markdown `$` expansion issues):
- `[model:haiku thinking:low]`: `~$0.02`
- `[model:sonnet thinking:med]`: `~$0.30`
- `[model:sonnet thinking:high]`: `~$0.60`
- `[model:opus thinking:max]`: `~$2.00`

Before `--all` on a large spec, estimate:
```bash
h=$(grep -c '\[model:haiku' "$TASKS_FILE" || echo 0)
sm=$(grep -c '\[model:sonnet thinking:med' "$TASKS_FILE" || echo 0)
sh=$(grep -c '\[model:sonnet thinking:high' "$TASKS_FILE" || echo 0)
o=$(grep -c '\[model:opus' "$TASKS_FILE" || echo 0)
echo "Estimated cost: \$$(python3 -c "print(f'{$h*0.02 + $sm*0.30 + $sh*0.60 + $o*2.00:.2f}')")"
```

Display before spawning in `--all` mode. User confirms before proceeding.
