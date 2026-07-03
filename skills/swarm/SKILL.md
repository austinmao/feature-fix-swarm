---
name: swarm
description: "Ad-hoc task swarm executor. Pass natural language tasks directly (or --tasks-file), get model/agent/thinking classification, then execute via Ruflo coordination + native Task() (OAuth-only — mcp__ruflo__agent_execute NEVER called). No spec directory required. RUFLO_REQUIRED=auto (graceful fallback to native parallel if ruflo unreachable)."
version: "1.1.0"
permissions:
  filesystem: write
  network: false
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Agent
metadata:
  openclaw:
    requires:
      bins: []
      env: []
---

# /swarm — Ad-hoc task swarm executor

One command. Pass task descriptions, get classification, parallel execution, and a structured result.
No spec pipeline. No tasks.md prerequisite. No `specs/NNN/` directory.

## When to invoke

- User says "swarm these tasks", "run these in parallel", "execute these with the right agents"
- Multi-task batch execution without the full `/feature` → `/spec-decompose` → `/feature-implement` pipeline
- Ad-hoc parallel work spanning different domains (TS, Python, security, DB, docs, etc.)
- Quick automation: lint + test + docs + cleanup all at once
- Any time the user gives 2+ tasks and says "do all of these"

## Invocation

```
/swarm "task1" "task2" "task3"              # classify + execute inline tasks
/swarm --tasks-file PATH                    # read tasks from file (md checklist or one-per-line)
/swarm "task1" "task2" --dry-run            # classify + print annotated plan, exit without executing
/swarm "task1" "task2" --sequential         # disable [P] parallel dispatch — all tasks run serially
/swarm "task1" "task2" --model-override opus  # override ALL model assignments
/swarm "task1" "task2" --no-memory          # skip all mcp__ruflo__memory_* and agentdb_* calls
/swarm "task1" "task2" --no-auto            # print cost estimate + 10-second countdown before executing
/swarm --tasks-file PATH --dry-run          # read file + print plan, no agents
/swarm --swarm-id EXISTING_ID              # resume an existing swarm run (skip classification if tasks.md exists)
```

## Flags

| Flag | Default | Behavior |
|---|---|---|
| `--dry-run` | off | Classify tasks and print annotated plan. Exit 0. No agents spawned. |
| `--sequential` | off | Disable `[P]` parallel dispatch — every task runs serially in annotation order |
| `--model-override MODEL` | none | Override all model assignments (haiku/sonnet/opus/fable) — annotations ignored |
| `--no-memory` | off | Skip all `mcp__ruflo__memory_*`, `agentdb_*`, and `neural_train` calls |
| `--no-auto` | off | Print cost estimate, then pause 10 seconds before executing |
| `--tasks-file PATH` | none | Read raw tasks from file (one per line OR md checklist `- [ ] Task desc`) |
| `--swarm-id ID` | auto | Join existing run; skip classification if `.context/swarm/ID/tasks.md` exists |

## Operating disciplines (Fable-mode)

1. **Classify before dispatch.** The Step 1 classification agent always runs first. Never dispatch tasks with guessed model/agent assignments.
2. **Parallelize only genuinely independent tasks.** `[P]` dispatch requires disjoint file sets, no shared mutable state, no ordering dependency. When uncertain, omit `[P]`.
3. **Self-critique before reporting.** Before the final report, name at least one task that completed by self-report only (no file artifact). Never emit a silent all-green.

## OAuth-only constraint (CRITICAL)

`mcp__ruflo__agent_execute` and `mcp__ruflo__managed_agent_*` make direct HTTP calls to `api.anthropic.com`, bypassing Claude Code OAuth. They are NEVER called in this skill.

All LLM execution uses the native `Task()` tool (Claude Code OAuth — no per-token API cost).
Ruflo tools used: `swarm_init`, `agent_spawn` (metadata only), `memory_*`, `agentdb_*`, `hooks_*`, `task_create` (tracking only), `neural_train`.

If a run reports `No LLM provider configured`, the caller is on the wrong execution path. Fix the caller to use the host CLI wrapper (`scripts/harness/ruflo-host-executor.sh`) and native `Task()`, not `agent_execute`.

In Codex sessions, Ruflo tools may be lazy-loaded. If `mcp__ruflo__swarm_init` or `mcp__ruflo__agent_spawn` is not visible, use tool discovery for `ruflo swarm_init agent_spawn mcp_status` before declaring Ruflo unavailable. A worktree without the project `.codex/config.toml` can also miss the Ruflo MCP registration; run from the repo root or copy the project MCP config into that worktree.

---

## Workflow

### Step 0: Parse args and initialize run state

```bash
# ── Defaults ──────────────────────────────────────────────────────────────────
DRY_RUN=0
SEQUENTIAL=0
MODEL_OVERRIDE=""
NO_MEMORY=0
NO_AUTO=0
TASKS_FILE_IN=""
SWARM_ID_ARG=""
RAW_TASKS=()

# ── Arg parsing ───────────────────────────────────────────────────────────────
# Parse $ARGUMENTS string:
#   Quoted strings (or unquoted words) → RAW_TASKS[]
#   --tasks-file PATH  → TASKS_FILE_IN
#   --dry-run          → DRY_RUN=1
#   --sequential       → SEQUENTIAL=1
#   --model-override X → MODEL_OVERRIDE=X
#   --no-memory        → NO_MEMORY=1
#   --no-auto          → NO_AUTO=1
#   --swarm-id ID      → SWARM_ID_ARG=ID
#
# Implementation: use a bash read-ra loop or python shlex.split on $ARGUMENTS.

# ── Load tasks from file ──────────────────────────────────────────────────────
if [ -n "$TASKS_FILE_IN" ]; then
  [ -f "$TASKS_FILE_IN" ] || { echo "[swarm] ERROR: --tasks-file not found: $TASKS_FILE_IN"; exit 1; }
  # Strip md checklist prefix (- [ ] / - [x] / - [X] ), skip blank lines and # comments
  while IFS= read -r line; do
    # strip checklist prefix
    line="${line#"${line%%[![:space:]]*}"}"   # ltrim
    line="${line#- \[[ xX]\] }"
    line="${line#- \[\] }"
    [[ -z "$line" || "$line" == \#* ]] && continue
    RAW_TASKS+=("$line")
  done < <(grep -v '^[[:space:]]*#' "$TASKS_FILE_IN" | grep -v '^[[:space:]]*$')
fi

# ── Validate ──────────────────────────────────────────────────────────────────
if [ "${#RAW_TASKS[@]}" -eq 0 ]; then
  echo "[swarm] ERROR: no tasks provided."
  echo "  Usage: /swarm \"task1\" \"task2\"  OR  /swarm --tasks-file PATH"
  exit 1
fi
[ "${#RAW_TASKS[@]}" -gt 50 ] && echo "[swarm] WARN: ${#RAW_TASKS[@]} tasks — classification quality degrades above 50. Consider batching."

# ── Run-state setup ───────────────────────────────────────────────────────────
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
SWARM_TS=$(date +%Y%m%d-%H%M%S)
SWARM_RUN_ID="${SWARM_ID_ARG:-swarm-${SWARM_TS}-$$}"
SWARM_DIR="$REPO_ROOT/.context/swarm/$SWARM_RUN_ID"
mkdir -p "$SWARM_DIR"
LOG_FILE="$SWARM_DIR/run.log"
TASKS_FILE_OUT="$SWARM_DIR/tasks.md"

printf '{"timestamp":"%s","event":"start","swarm_id":"%s","task_count":%d}\n' \
  "$(date -u +%FT%TZ)" "$SWARM_RUN_ID" "${#RAW_TASKS[@]}" >> "$LOG_FILE"

echo "[swarm] Run ID:  $SWARM_RUN_ID"
echo "[swarm] Log:     $LOG_FILE"
echo "[swarm] Tasks:   ${#RAW_TASKS[@]} to classify"
echo ""
```

If `--swarm-id` was provided AND `$TASKS_FILE_OUT` exists: skip Step 1 (resume from prior classification). Log `event: "resume"`.

---

### Step 1: Classification agent (host middle-tier Task())

Spawn ONE `Task()` call using the active host's middle-tier model to classify all raw tasks. The agent writes an annotated `tasks.md` to `$TASKS_FILE_OUT`.

```
Task({
  model: ACTIVE_HOST == "codex" ? "gpt-5.4" : "sonnet",
  subagent_type: "planner",
  description: "swarm-classify-" + SWARM_RUN_ID,
  prompt: CLASSIFICATION_PROMPT   # see full text below
})
```

**Full classification agent prompt** (substitute bracketed values at runtime):

```
You are classifying a list of ad-hoc tasks for swarm execution. You have no prior context.

## Your input: raw task list

{RAW_TASK_LIST}
(one task per line, {N} total)

## Your job

Write an annotated task list to EXACTLY this path:
  {TASKS_FILE_OUT}

Use this format — one task per item:
  - [ ] [model:X] [thinking:Y] [agent:TYPE] T{NNN} {original description} {[P] if parallel-safe}
    - Depends-on: none | T{id}, T{id}

Rules:
- Number tasks T001, T002, ... in declaration order
- [P] means safe to run concurrently with other [P] tasks in this batch
- NEVER mark [P] when: DB migrations, config writes, git operations, sequential logic steps
- Add Depends-on ONLY when one task produces an artifact another task consumes
- No cross-task dependency → Depends-on: none

---

## Model assignment table

| Tier   | Assign when                                                   | Keyword signals                                                   |
|--------|---------------------------------------------------------------|---------------------------------------------------------------------|
| haiku  | Trivial/mechanical — single-file, no logic decisions          | lint, format, rename, move, delete, comment, typo, update docs, bump version, add blank line |
| sonnet | Standard implementation — default tier                        | implement, build, create, add, refactor, test, write, migrate     |
| opus   | Security-critical or deep analysis                            | security audit, threat model, auth, architecture decision, CVE, cryptographic, critical path |
| fable  | Multi-file narrative coherence                                | cross-module synthesis, end-to-end integration narrative           |

Default to sonnet when no clear signal.

## Host runtime mapping

The emitted `tasks.md` stays canonical (`haiku` / `sonnet` / `opus` / `fable`).
The active host maps those tiers at execution time:

| Host runtime | haiku | sonnet | opus |
|--------------|-------|--------|------|
| Claude Code  | haiku | sonnet | opus |
| Codex        | gpt-5.4-mini | gpt-5.4 | gpt-5.5 |

`fable` has no host-runtime mapping — it only resolves on the native Claude Code `Task()` path (see the Ruflo enum note in Step 3). On the Ruflo coordination path it downgrades to `sonnet`.

---

## Agent type routing table

| Domain keyword signals                                                      | Agent type                  | Model note          |
|-----------------------------------------------------------------------------|-----------------------------|---------------------|
| playwright, browser automation, test automation, visual regression          | test-automator              | —                   |
| test, TDD, spec, unit test, integration test, coverage, jest, pytest        | ecc:tdd-guide               | —                   |
| code review, PR review, quality, conventions, lint rules                    | ecc:code-reviewer           | —                   |
| security audit, threat model, OWASP compliance, vulnerability assessment    | security-auditor            | FORCE opus          |
| backend security, auth middleware, JWT, OAuth, CSRF, XSS, CSP              | backend-security-coder      | FORCE opus          |
| frontend security, browser security, WebView security                       | frontend-security-coder     | FORCE opus          |
| architecture, ADR, system design, data model, service boundary              | ecc:architect               | FORCE opus          |
| backend architecture, microservice, event sourcing, API design              | backend-architect           | FORCE opus          |
| TypeScript, TS, .tsx, type system, generics, compiler                        | typescript-pro              | —                   |
| React, Next.js, component, landing page, CSS, Tailwind, UI                  | frontend-developer          | —                   |
| UI/UX, wireframe, mockup, design system, user flow                          | ui-ux-designer              | —                   |
| accessibility, WCAG, a11y                                                  | accessibility-expert        | —                   |
| Python, async Python, .py file                                              | python-pro                  | —                   |
| Django                                                                       | django-pro                  | —                   |
| FastAPI                                                                      | fastapi-pro                 | —                   |
| SQL, migration, schema, query, index, PostgreSQL, Supabase, DB              | database-architect          | —                   |
| query optimization, slow query, index tuning, DB performance                | database-optimizer          | —                   |
| cleanup, dead code, remove unused, simplify, rename, small refactor         | ecc:refactor-cleaner        | prefer haiku        |
| build error, CI failure, dependency conflict, compile error                 | ecc:build-error-resolver    | prefer haiku        |
| performance, latency, throughput, bottleneck, profiling, caching            | performance-engineer        | —                   |
| debugging, stack trace, crash, hang, intermittent failure                   | error-detective             | —                   |
| deploy, release, rollout, CI/CD, containers, Vercel                         | deployment-engineer         | —                   |
| cloud, AWS, Azure, GCP, infrastructure                                      | cloud-architect             | —                   |
| Kubernetes, GitOps, service mesh, mTLS                                      | kubernetes-architect        | —                   |
| Terraform, IaC, state management                                            | terraform-specialist        | —                   |
| observability, metrics, tracing, logs, monitoring, SLOs                     | observability-engineer      | —                   |
| docs, documentation, README, tutorial, guide                                | docs-architect              | —                   |
| OpenAPI, Swagger, API docs                                                  | api-documenter              | —                   |
| reference, cheat sheet, lookup                                              | reference-builder           | —                   |
| code explorer, reverse engineer, search                                     | reverse-engineer            | —                   |
| context management, swarm context                                           | context-manager             | —                   |
| prompt engineering                                                          | prompt-engineer             | —                   |
| business, metrics, KPI, reporting                                           | business-analyst            | —                   |
| sales, cold email, follow-up, proposal                                      | sales-automator             | —                   |
| support, FAQ, customer communication                                        | customer-support            | —                   |
| SEO, search engine, meta description, meta title, snippet                   | seo-meta-optimizer          | —                   |
| (no domain match)                                                           | general-purpose             | —                   |

"FORCE opus" = set [model:opus] regardless of complexity signals.
"prefer haiku" = use haiku unless the description implies multi-file or design decisions.

---

## Thinking budget table

| Level | Assign when                                           |
|-------|-------------------------------------------------------|
| low   | haiku tasks; single-file substitutions                |
| med   | sonnet tasks; standard feature work (default)         |
| high  | Complex analysis; cross-cutting concerns              |
| max   | opus security/architecture tasks only                 |

---

## Parallel safety checklist

Before marking a task [P], ALL of the following must be true:
1. No other [P] task in this batch produces an artifact this task consumes
2. This task operates on different files from other [P] tasks
3. No shared mutable state (DB, config file, lock file, git index)
4. Order does not matter for correctness

When in doubt, omit [P].

---

## After writing tasks.md

Print exactly ONE JSON line to stdout (the caller parses this):
CLASSIFY_RESULT: {"tasks":N,"parallel":K,"models":{"haiku":H,"sonnet":S,"opus":O,"fable":F},"agents":{"ecc:tdd-guide":N}}

Then stop.
```

After the Task() call:

```bash
if [ ! -f "$TASKS_FILE_OUT" ]; then
  echo "[swarm] ERROR: classification agent did not write $TASKS_FILE_OUT"
  printf '{"timestamp":"%s","event":"classify_failed","swarm_id":"%s"}\n' \
    "$(date -u +%FT%TZ)" "$SWARM_RUN_ID" >> "$LOG_FILE"
  exit 1
fi

TASK_COUNT=$(grep -cE '^- \[' "$TASKS_FILE_OUT" 2>/dev/null || echo 0)
[ "$TASK_COUNT" -eq 0 ] && { echo "[swarm] ERROR: tasks.md is empty or malformed"; exit 1; }

printf '{"timestamp":"%s","event":"classify_done","swarm_id":"%s","task_count":%d}\n' \
  "$(date -u +%FT%TZ)" "$SWARM_RUN_ID" "$TASK_COUNT" >> "$LOG_FILE"
```

Parse tasks.md into structured data via shared `lib/dispatch.py`:

```bash
# codex-gate (PR #11): resolve dispatch.py across all three install shapes.
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
TASKS_JSON=$(FILE="$TASKS_FILE_OUT" python3 "$DISPATCH" parse)
```

---

### Step 2: Display annotated plan + dry-run exit

```bash
echo "=== SWARM PLAN: $SWARM_RUN_ID ==="
echo ""
cat "$TASKS_FILE_OUT"
echo ""

# ── Cost estimate ─────────────────────────────────────────────────────────────
H=$(grep -c '\[model:haiku'  "$TASKS_FILE_OUT" 2>/dev/null || echo 0)
S=$(grep -c '\[model:sonnet' "$TASKS_FILE_OUT" 2>/dev/null || echo 0)
O=$(grep -c '\[model:opus'   "$TASKS_FILE_OUT" 2>/dev/null || echo 0)
F=$(grep -c '\[model:fable'  "$TASKS_FILE_OUT" 2>/dev/null || echo 0)
P=$(grep -c '\[P\]'          "$TASKS_FILE_OUT" 2>/dev/null || echo 0)
EST=$(FILE="$TASKS_FILE_OUT" python3 "$DISPATCH" cost)
# Warn if any [model:fable] tasks will silently downgrade to sonnet on Ruflo path
python3 "$DISPATCH" fable-warn "$TASKS_FILE_OUT"

echo "Cost estimate:   ~\$$EST"
echo "Task breakdown:  $TASK_COUNT total | haiku:$H sonnet:$S opus:$O fable:$F"
echo "Parallel tasks:  $P (dispatched concurrently)"
echo ""

# ── Dry-run exit ──────────────────────────────────────────────────────────────
if [ "$DRY_RUN" = "1" ]; then
  printf '{"timestamp":"%s","event":"dry_run_exit","swarm_id":"%s","estimated_cost":"%s"}\n' \
    "$(date -u +%FT%TZ)" "$SWARM_RUN_ID" "$EST" >> "$LOG_FILE"
  echo "[swarm] --dry-run: plan printed. No agents spawned."
  exit 0
fi

# ── No-auto confirmation ──────────────────────────────────────────────────────
if [ "$NO_AUTO" = "1" ]; then
  echo "[swarm] --no-auto: review the plan above."
  echo "  Proceeding in 10 seconds. Ctrl+C to abort."
  sleep 10
fi
```

---

### Step 3: Ruflo coordination setup

```python
# ── Pre-flight: check if Ruflo is available ───────────────────────────────────
USE_RUFLO = True
try:
    mcp__ruflo__swarm_status()
except Exception:
    USE_RUFLO = False
    log({"event": "ruflo_unavailable", "fallback": "native_parallel"})
    print("[swarm] INFO: ruflo unreachable — switching to native parallel Agent path.")

# ── Session init + memory prime ───────────────────────────────────────────────
if USE_RUFLO and not NO_MEMORY:
    mcp__ruflo__hooks_session-start({"context": "swarm:" + SWARM_RUN_ID})

    priorPatterns = mcp__ruflo__memory_search_unified({
        "query": "swarm ad-hoc task execution patterns",
        "limit": 5
    })
    # top matches injected into sub-agent prompts as session context

# ── Swarm init ────────────────────────────────────────────────────────────────
if USE_RUFLO:
    swarmId = mcp__ruflo__swarm_init({
        "topology": "parallel",
        "maxAgents": min(len(unique_roles), 8),
        "strategy": "specialized"
    })

    # Register one agent per unique (role, model) tuple — metadata ONLY, no LLM
    for (role, model) in unique_role_model_tuples(TASKS):
        effectiveModel = MODEL_OVERRIDE or model
        if effectiveModel == "fable":
            effectiveModel = "sonnet"  # Ruflo enum: haiku|sonnet|opus|inherit only

        mcp__ruflo__agent_spawn({
            "agentType": role,
            "model": effectiveModel,
            "task": "Execute swarm " + SWARM_RUN_ID + " tasks tagged [agent:" + role + "]",
            "swarmId": swarmId
        })

    # Tracking only (does NOT drive execution)
    for task in TASKS:
        mcp__ruflo__task_create({
            "type": "swarm",
            "description": task.description,
            "tags": ["swarm:" + SWARM_RUN_ID, "task:" + task.id, "model:" + task.model]
        })
```

---

### Step 4: Execute via Task() — dependency-ordered, parallel-aware

**Thinking alignment** (prevents model/budget mismatch) — resolved via `lib/dispatch.py`:

```bash
# effective_thinking = resolve_thinking(effective_model, task["thinking"])
effective_thinking=$(python3 "$DISPATCH" resolve --model "$effective_model" --thinking "${task_thinking}")
```

Alignment rules (implemented in `lib/dispatch.py::resolve_thinking`):
- `opus + med` → `high` (opus + med wastes capability)
- `haiku + high/max` → `med` (haiku can't utilize max budget)
- all others → unchanged

THINKING_GUIDANCE lives in `lib/dispatch.py::THINKING_GUIDANCE` — import for sub-agent prompt construction:

```python
import sys, importlib.util
spec = importlib.util.spec_from_file_location("dispatch", DISPATCH)
dispatch = importlib.util.module_from_spec(spec); spec.loader.exec_module(dispatch)
THINKING_GUIDANCE = dispatch.THINKING_GUIDANCE
```

**Main dispatch loop** (dependency-ordered):

```python
todo       = [t for t in TASKS if t["status"] == "todo"]
done_ids   = {t["id"] for t in TASKS if t["status"] == "done"}
failed_ids = set()

while todo:
    # Tasks whose dependencies are all satisfied
    executable = [t for t in todo if all(dep in done_ids for dep in t["depends_on"])]

    if not executable:
        print("[swarm] ERROR: dependency deadlock — remaining tasks cannot be scheduled")
        log({"event": "deadlock", "remaining": [t["id"] for t in todo]})
        break

    # Split: [P] group (parallel) vs sequential (--sequential flag disables [P])
    p_group    = [t for t in executable if t["parallel"] and not SEQUENTIAL]
    sequential = [t for t in executable if not t["parallel"] or SEQUENTIAL]

    # ── Parallel group: ALL spawned in ONE message turn ───────────────────────
    if p_group:
        print(f"[swarm] Dispatching {len(p_group)} parallel tasks: {[t['id'] for t in p_group]}")
        for task in p_group:
            effective_model    = MODEL_OVERRIDE or task["model"]
            effective_thinking = resolve_thinking(effective_model, task["thinking"])
            task_patterns = pre_task_memory(task, USE_RUFLO, NO_MEMORY)

            # run_in_background=true → concurrent execution within this message turn
            Task({
                "model":             effective_model,
                "subagent_type":     task["agent"],
                "description":       task["id"] + " " + task["description"][:40],
                "run_in_background": True,
                "prompt":            task_prompt(task, effective_model, effective_thinking, task_patterns)
            })
        # Wait for all [P] results, then process
        for result in parallel_results:
            process_result(result, task_by_id[result.task_id])

    # ── Sequential tasks: one at a time ──────────────────────────────────────
    for task in sequential:
        effective_model    = MODEL_OVERRIDE or task["model"]
        effective_thinking = resolve_thinking(effective_model, task["thinking"])
        task_patterns = pre_task_memory(task, USE_RUFLO, NO_MEMORY)

        result = Task({
            "model":         effective_model,
            "subagent_type": task["agent"],
            "description":   task["id"] + " " + task["description"][:40],
            "prompt":        task_prompt(task, effective_model, effective_thinking, task_patterns)
        })
        process_result(result, task)

    # Update scheduling state
    todo = [t for t in todo if t["id"] not in done_ids and t["id"] not in failed_ids]
```

**`pre_task_memory(task, use_ruflo, no_memory)` helper:**

```python
if use_ruflo and not no_memory:
    mcp__ruflo__hooks_pre-task({"taskId": task.id, "description": task.description, "swarmId": swarmId})
    hits = mcp__ruflo__memory_search({"query": task.description, "limit": 3})
    prior = mcp__ruflo__agentdb_pattern-search({"query": task.description[:80], "limit": 1})
    mem = hits[0]["content"][:400] if hits else ""
    pat = prior[0]["solution"][:400] if prior else ""
    return (mem + "\n" + pat).strip()
return ""
```

**`process_result(result, task)` helper:**

```python
status   = parse_field(result, "STATUS")   # success|partial|blocked
summary  = parse_field(result, "SUMMARY")
files    = parse_field(result, "FILES_CHANGED")
blockers = parse_field(result, "BLOCKERS")

if status in ("success", "partial"):
    task.status = "done"
    done_ids.add(task.id)
    log({"event": "task_done", "task_id": task.id, "status": status, "summary": summary})

    if use_ruflo and not no_memory:
        mcp__ruflo__hooks_post-task({"taskId": task.id, "outcome": "success", "swarmId": swarmId})
        mcp__ruflo__memory_store({
            "content":   "swarm " + SWARM_RUN_ID + " " + task.id + ": " + task.description + " [SUCCESS]\n" + summary,
            "namespace": "patterns",
            "metadata":  {"swarm": SWARM_RUN_ID, "task": task.id, "model": effective_model}
        })
        mcp__ruflo__agentdb_pattern-store({
            "pattern":  task.description,
            "solution": summary,
            "context":  {"agent": task.agent, "model": effective_model}
        })
        pattern_store_count += 1
        if pattern_store_count >= 10:
            mcp__ruflo__neural_train({"namespace": "patterns"})
            pattern_store_count = 0
else:  # blocked or error
    task.status = "failed"
    failed_ids.add(task.id)
    log({"event": "task_failed", "task_id": task.id, "blockers": blockers})
    if use_ruflo and not no_memory:
        mcp__ruflo__hooks_post-task({"taskId": task.id, "outcome": "failed", "swarmId": swarmId})
    # Continue to remaining tasks — unlike /feature-implement, /swarm does not stop on first failure
```

**Sub-agent prompt template:**

```
You are executing a single task in an ad-hoc swarm. You have no prior context.

## Task
ID:           {task.id}
Description:  {task.description}
Agent type:   {task.agent}
Model tier:   {effective_model}
Thinking:     {effective_thinking} — {THINKING_GUIDANCE[effective_thinking]}

## Swarm context
Swarm ID: {SWARM_RUN_ID}

## Prior patterns from memory (if any)
{task_patterns or "(none)"}

## Instructions
1. Execute the task completely. Do not ask clarifying questions.
2. Make minimal, correct, idiomatic changes. Prefer editing over full rewrites.
3. If the task involves tests: write them first (RED), then implement (GREEN).
4. Follow TDD and immutability rules from CLAUDE.md.
5. If genuinely blocked (missing dependency, ambiguous requirement, permission error):
   set STATUS: blocked and describe the blocker. Do not guess or hallucinate an action.

## Required output (LAST lines of your response — caller parses these)
STATUS: success|partial|blocked
SUMMARY: <one sentence — what was actually done>
FILES_CHANGED: <comma-separated relative paths, or "none">
BLOCKERS: <description if blocked, else "none">
```

**Ruflo session end:**

```python
if use_ruflo:
    mcp__ruflo__hooks_session-end({
        "swarmId": swarmId,
        "outcome": "complete" if not failed_ids else "partial"
    })
```

---

### Step 5: Self-critique + final report

**Self-critique** (Fable-mode discipline 3):
Before printing results, identify tasks completed by sub-agent self-report only (no `FILES_CHANGED`). Name at least one uncertainty or "none verified by artifact."

**Final report:**

```bash
DONE_COUNT=$(echo "$TASKS_JSON" | python3 -c "
import sys, json; t=json.load(sys.stdin); print(sum(1 for x in t if x['status']=='done'))")
FAIL_COUNT=$(echo "$TASKS_JSON" | python3 -c "
import sys, json; t=json.load(sys.stdin); print(sum(1 for x in t if x['status']=='failed'))")

echo ""
echo "=== SWARM COMPLETE: $SWARM_RUN_ID ==="
echo ""
echo "  done:    $DONE_COUNT"
echo "  failed:  $FAIL_COUNT"
echo ""
```

If any tasks failed, print them with blockers. Then self-critique:

```
[swarm] Self-critique:
  Self-report only (no artifact): <list task IDs or "none">
  One concern: <one uncertainty, or "none surfaced">
```

Final log entry:

```json
{"timestamp":"<ISO>","event":"complete","swarm_id":"<ID>","done":<N>,"failed":<N>,"executor":"ruflo|native"}
```

Print:

```
  Run log:    .context/swarm/{SWARM_RUN_ID}/run.log
  Task plan:  .context/swarm/{SWARM_RUN_ID}/tasks.md
```

---

## Model assignment heuristics

| Model | Rough cost/task | Best for |
|---|---|---|
| haiku | ~$0.02 | Lint, format, rename, delete dead code, comment updates |
| sonnet | ~$0.30–0.60 | Feature implementation, refactors, test writing (default) |
| opus | ~$2.00 | Security audits, architecture decisions, complex analysis |
| fable | ~$0.50 | Multi-file coherence, narrative synthesis |

Cost formula: `haiku×$0.02 + sonnet×$0.30 + opus×$2.00 + fable×$0.50`

---

## Agent type routing

| Agent type | Domain | Default model |
|---|---|---|
| `test-automator` | Playwright, browser automation | sonnet |
| `ecc:tdd-guide` | test, TDD, coverage | sonnet |
| `ecc:code-reviewer` | PR review, quality | sonnet |
| `security-auditor` | security audits, threat models | **opus** |
| `backend-security-coder` | auth, CSRF, XSS, API security | **opus** |
| `frontend-security-coder` | client-side security, WebView hardening | **opus** |
| `ecc:architect` | architecture, ADR | **opus** |
| `backend-architect` | API design, microservices | **opus** |
| `graphql-architect` | GraphQL schemas, resolvers | **opus** |
| `frontend-developer` | React, Next.js, responsive UI | sonnet |
| `ui-ux-designer` | wireframes, design systems, user flow | sonnet |
| `ui-designer` | visual design, design systems | **opus** |
| `accessibility-expert` | WCAG, a11y | **opus** |
| `ui-visual-validator` | visual regression, screenshot diffs | sonnet |
| `typescript-pro` | TypeScript, TSX, generics | sonnet |
| `python-pro` | Python, async Python | sonnet |
| `fastapi-pro` | FastAPI | sonnet |
| `django-pro` | Django | sonnet |
| `javascript-pro` | JavaScript, Node.js | sonnet |
| `database-architect` | SQL, schema, migrations | **opus** |
| `database-optimizer` | query tuning, indexes | sonnet |
| `database-admin` | backup, replication, failover | sonnet |
| `performance-engineer` | profiling, bottlenecks | **opus** |
| `debugger` | stack traces, crashes | sonnet |
| `error-detective` | intermittent failures, log analysis | sonnet |
| `incident-responder` | outages, production incidents | **opus** |
| `deployment-engineer` | deploy, release, rollout | sonnet |
| `cloud-architect` | cloud infrastructure | **opus** |
| `kubernetes-architect` | Kubernetes, GitOps | **opus** |
| `terraform-specialist` | IaC, Terraform state | sonnet |
| `observability-engineer` | tracing, logs, SLOs | **opus** |
| `docs-architect` | docs, README, tutorials | **opus** |
| `api-documenter` | OpenAPI, Swagger | sonnet |
| `reference-builder` | concise technical references | haiku |
| `reverse-engineer` | code exploration, unknown systems | sonnet |
| `context-manager` | multi-agent context management | haiku |
| `prompt-engineer` | prompt pipelines | **opus** |
| `business-analyst` | metrics, KPI tracking | sonnet |
| `sales-automator` | cold email, follow-ups | haiku |
| `customer-support` | support tickets, FAQ responses | sonnet |
| `seo-meta-optimizer` | metadata optimization | haiku |
| `ecc:refactor-cleaner` | cleanup, dead code | haiku |
| `ecc:build-error-resolver` | CI failures, build errors | haiku |
| `ecc:performance-optimizer` | perf, profiling | sonnet |
| `ecc:database-reviewer` | SQL, schema, migrations | sonnet |
| `general-purpose` | catch-all | sonnet |

---

## Failure handling

| Failure point | Behavior |
|---|---|
| No tasks provided | Exit 1 with usage hint |
| `--tasks-file` not found | Exit 1 with path |
| Classification agent fails to write tasks.md | Exit 1 with log reference |
| Ruflo pre-flight fails | Auto-switch to native path; no exit |
| `mcp__ruflo__memory_store` fails | Log and skip — advisory storage, not a gate |
| Task sub-agent returns STATUS: blocked | Mark `[F]`, continue to remaining tasks |
| Dependency deadlock (circular deps) | Log deadlock, print remaining, exit 1 |
| >50 tasks | WARN only; continue |

Structured error format on exit 1:

```
[swarm] ERROR: {error_type}
  detail:  {message}
  log:     .context/swarm/{SWARM_RUN_ID}/run.log
Recover:
  {recovery_instruction}
```

---

## Safety rules

- Never modify files outside the project directory
- Never push, deploy, or delete branches without explicit task instruction
- Sub-agents inherit CLAUDE.md rules via the Agent tool's permission system
- `mcp__ruflo__agent_execute` and `managed_agent_*` are NEVER called
- `task_create` is tracking-only; it does NOT drive execution
- `--model-override opus` on a large batch can be expensive (cost estimate warns before executing)

---

## Non-goals (v1)

- No QA loop — use `/qa` as a follow-up
- No review-gate — use `/review-gate` as a follow-up
- No commit/push — use `/ship`
- No task-level retries (blocked = failed; re-run or fix manually)
- No persistent `specs/NNN/tasks.md` — use `/spec-decompose` for that
- No Linear or external tracker updates

## Related skills

- `/spec-decompose` — structured decomposition with spec pipeline; produces persistent tasks.md
- `/feature-implement` — executes a tasks.md produced by `/spec-decompose`
- `/feature` — full pipeline (autoplan → decompose → implement → qa → ship)
- `/fix` — investigation + fix for a single bug with QA loop
