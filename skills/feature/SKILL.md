---
name: feature
description: "End-to-end pipeline: autoplan → spec-decompose → per-wedge implement+phase-audit → qa → review-gate → ship → canary. Non-interactive by default. v2.1.1: skill PREPARES native /goal condition (with run_id baked in) and prints it for operator to paste — UI commands like /goal cannot be invoked via Skill tool, so auto-invoke is architecturally impossible. Per-phase audit + /review-gate remain mandatory."
version: "2.4.0"
allowed-tools:
  - Read
  - Edit
  - Bash
  - Glob
  - Skill
  - AskUserQuestion
---

# /feature — End-to-end feature pipeline

## Native /goal — operator-set, skill-prepared (v2.1.1)

**Why not fully auto-managed?** UI commands like `/goal` cannot be invoked from a skill via the `Skill` tool — the runtime explicitly refuses with `"goal is a UI command, not a skill"`. v2.1.0 attempted to auto-invoke; that path is architecturally impossible. v2.1.1 changes the contract: the skill **prepares** the goal condition (bakes the `run_id` into it, references the right audit log paths) and **prints** it for the operator to paste.

### How to use

1. Run `/feature 130` (or whatever spec id)
2. Step 0.5 prints a banner like:

   ```
   ┌─────────────────────────────────────────────────────────────────┐
   │ COPY THIS AND PASTE AS YOUR NEXT MESSAGE TO ENABLE AUTO-LOOP:   │
   ├─────────────────────────────────────────────────────────────────┤
   │ /goal "spec 130 complete via /feature run a1b2c3: every audit   │
   │ line in ~/.claude/state/audits.jsonl with run_id=a1b2c3 has     │
   │ verdict=pass, AND .ralph/feature-run-130-20260512-160505.jsonl  │
   │ contains a review-gate row with status in {PASS, skipped}, AND  │
   │ /canary returned 200 in .ralph/feature-run-130-..."             │
   └─────────────────────────────────────────────────────────────────┘
   ```

3. Operator copies the `/goal "..."` line and pastes it as their next message
4. Native `/goal` engages; Claude loops the pipeline until the condition holds

Native `/goal` (Anthropic-maintained, Claude Code 2.1.139+):
- Small/fast model checks the condition after every turn
- Auto-continues if condition false; clears automatically when met
- One goal per session — pasting `/goal` replaces any existing goal
- `/goal clear` cancels manually mid-run
- See https://code.claude.com/docs/en/goal

### Skip the goal banner

If operator pre-set a custom `/goal` and wants to keep it, or running under `claude -p` non-interactive where /goal is moot:

```bash
/feature 130 --no-goal     # skill skips the prepare-and-print step
```

### Skill output

- `~/.claude/state/runs.db` — SQLite run record with `run_id` (captured in Step 0.5)
  - `~/.claude/state/audits.jsonl` — one line per phase audit + review-gate result (greppable by /goal checker)
- `.ralph/feature-run-<NNN>-<TS>.jsonl` — local pipeline event log (includes `goal-prepared` line)

## Flags

| Flag | Effect |
|---|---|
| `--interactive` | Restore manual gates (autoplan premise, taste decisions). Default = non-interactive. |
| `--skip-codex-gate` | Emergency-merge fallback. Skip the mandatory cross-model `/review-gate` review before `/ship`. **NOT recommended** — bypasses the strongest pre-prod safety net. Per-phase audits remain in force. |
| `--no-goal` | Skip auto-setting native `/goal` at entry. Use if operator pre-set a custom `/goal` condition they want preserved, or running under `claude -p` non-interactive mode where /goal is moot. |

## When to invoke

- You have `specs/NNN-feature-name/plan.md` ready for review
- You want to ship the feature through the full pipeline with minimal ceremony
- User says: "run feature NNN", "ship feature NNN end-to-end", "autoplan through deploy"

## Prerequisites

- Git branch checked out (ideally `NNN-feature-name`)
- `specs/NNN-feature-name/plan.md` exists and is ready for review
- `specs/NNN-feature-name/spec.md` is optional — `/spec-decompose` handles missing spec.md
- `/office-hours` may have been run (not required, but design doc improves autoplan quality)
- **Ruflo MCP must be available.** Pretrain once: `npx claude-flow@v3alpha hooks pretrain`. The pipeline hard-fails if `mcp__ruflo__*` tools are not reachable. Pretrain also enables intelligent model routing in `/feature-implement` — without it, `hooks_model-route` returns `opus` for every task (all tasks upgrade to opus cost, which is wrong).
- Ruflo coordinates swarms and memory only. Do not use `mcp__ruflo__agent_execute` or `mcp__ruflo__managed_agent_*` in this pipeline. Feature work runs through the active host CLI wrapper (`scripts/harness/ruflo-host-executor.sh`), which uses `codex exec` or `claude -p` under the user's logged-in session.

## Invocation

```
/feature [NNN]              # DEFAULT: --auto mode, ruflo-backed, fully non-interactive
                            #   - autoplan premise gate: auto-approved
                            #   - autoplan taste decisions: auto-approved (recommended option)
                            #   - tasks.md approval: auto-approved
                            #   - prod promotion gate: STILL ASKS (irreversible action)
/feature [NNN] --interactive   # ESCAPE HATCH: restore legacy gate behavior
                               #   - autoplan premise gate: prompts user
                               #   - autoplan taste decisions: prompts user
                               #   - tasks.md approval: prompts user
                               #   - prod promotion gate: prompts user (same as --auto)
/feature [NNN] --resume     # resume after failure (picks up at last incomplete step)
/feature [NNN] --no-canary  # stop after /ship (skip production canary)
/feature [NNN] --skip-codex-gate  # emergency-merge: skip cross-model gate (see Step 5.7, NOT recommended)
/feature [NNN] --dry-run    # print the pipeline plan, don't execute
```

**Removed in v1.1.0:** `--no-ruflo` flag. Ruflo is now mandatory (no silent fallback to native Agent). If ruflo MCP is unavailable, the pipeline hard-fails with a structured error directing the user to fix the MCP connection. To debug ruflo issues, set `RUFLO_REQUIRED=0` in env (escape hatch — falls back to native, logs WARNING).

## Operating disciplines (Fable-mode)

Three disciplines from [Fable-mode](https://github.com/mrtooher/fable-mode) bracket the pipeline. They cost a few lines of output and save whole re-runs.

1. **Stage map first.** Before executing, print the numbered pipeline below with a one-line expected output per step (spec id, branch, wedge count once known). Surfacing the plan up front is how you catch a wrong assumption at Step 2 instead of discovering it at Step 9. `--dry-run` already emits this; in a real run, emit it once at entry too.
2. **Verify before advancing.** No step advances on red. The per-phase adversarial audit (Step 4b) and `/review-gate` (Step 5.7) *are* this discipline — the pipeline's core, not an afterthought. Never skip an audit to "save time": a bug compounded across later wedges costs more than every audit combined.
3. **Self-critique before delivery.** Before the final report, re-read the run as a skeptical reviewer (Step 9.9). Name at least one residual risk, gap, or untested path. Fix it or surface it in the report — never emit a silent "all green."

## The pipeline

```
┌─────────────────────────────────────────────────────────────┐
│  /feature NNN                                               │
│                                                             │
│  Native /goal condition (set by operator before invoking)   │
│                                                             │
│  Step 1: /autoplan on specs/NNN/plan.md                     │
│    └─ /autoplan owns the planning-time Codex audit          │
│    └─ GATE: user approves review (taste decisions, etc.)    │
│                                                             │
│  Step 2: /spec-decompose NNN → N wedges                     │
│    └─ GATE: user approves tasks.md (spot-check quality)     │
│                                                             │
│  Step 2.5: speckit.analyze (read-only consistency check)    │
│    └─ CRITICAL findings block; HIGH warn+continue           │
│                                                             │
│  Step 2.6: Task quality gate (Codex on tasks.md)            │
│    └─ Score < 5 → abort. 5-6 → warn. ≥7 → proceed          │
│                                                             │
│  Step 3: /feature-implement <wedge-1>                       │
│    └─ Auto-stops on first task failure                      │
│    └─ Per-phase QA hooks inside /feature-implement          │
│                                                             │
│  Step 3b: run-state audit --kind phase (wedge-1)            │
│    └─ Codex GPT-5 hostile audit of THIS wedge only          │
│    └─ verdict=fail → loop back into /feature-implement      │
│                                                             │
│  Steps 3+3b repeat per wedge until all wedges complete      │
│  (one wedge = one implement cycle + one phase audit)        │
│                                                             │
│  Step 5: /qa (full-suite browser test after ALL wedges)     │
│    └─ Auto-stops if bugs found; user fixes, /feature --resume│
│                                                             │
│  Step 5.7: /review-gate (cross-model full-branch review)    │
│    └─ 3 Codex GPT-5 passes: review + adversarial + gaps     │
│    └─ Mandatory before /ship (same gate /fix uses)          │
│    └─ Skip-gracefully if codex CLI absent (warn, continue)  │
│    └─ Emergency opt-out: --skip-codex-gate (NOT recommended)│
│                                                             │
│  Step 6: /ship (creates PR, merges to staging branch)       │
│    └─ Staging deploy via Vercel preview / Railway staging   │
│    └─ Staging smoke test runs automatically                 │
│                                                             │
│  Step 9: /canary (production promotion)                     │
│    └─ FINAL GATE: user approves prod promotion              │
│    └─ Merge to main → Vercel prod deploy                    │
│    └─ 1h canary monitor (error rate < 1%)                   │
│    └─ Auto-rollback if SLO breached                         │
│    └─ On success: native /goal condition auto-clears        │
└─────────────────────────────────────────────────────────────┘
```

**Total gates (default --auto):** 1 — prod promotion only (irreversible). Per-phase audits, review-gate BLOCK verdicts, and other failure modes auto-stop the pipeline but produce structured artifacts (no human prompt) — user fixes and resumes.
**Total gates (--interactive):** 4 — autoplan premise, autoplan taste decisions, tasks.md approval, prod promotion.
Everything else auto-runs or auto-fails.

## Step-by-step workflow

### Step 0: Resolve spec and verify prerequisites

```bash
SPEC_ARG="${ARGUMENTS:-}"
RESUME=0
NO_CANARY=0
DRY_RUN=0
# v1.4.0: codex-gate is mandatory before /ship. --skip-codex-gate is an
# emergency-merge opt-out (NOT recommended; per-phase audits still run).
SKIP_CODEX_GATE=0
# v2.1.0: skill auto-sets native /goal at entry. --no-goal opts out.
NO_GOAL=0
# v1.1.0: --auto is default. --interactive opts back into manual gates.
INTERACTIVE=0
# v1.1.0: ruflo is mandatory by default. RUFLO_REQUIRED=0 env override allows
# native fallback for debugging only (logs WARNING).
RUFLO_REQUIRED="${RUFLO_REQUIRED:-1}"

# BUG-1 fix (2026-04-16): word-split $SPEC_ARG explicitly via `read -ra`.
read -ra _SPEC_ARGS <<< "$SPEC_ARG"
for arg in "${_SPEC_ARGS[@]}"; do
  case "$arg" in
    --resume)      RESUME=1 ;;
    --auto)        INTERACTIVE=0 ;;   # explicit --auto (already default)
    --interactive) INTERACTIVE=1 ;;
    --no-canary)   NO_CANARY=1 ;;
    --skip-codex-gate) SKIP_CODEX_GATE=1 ;;
    --no-goal)     NO_GOAL=1 ;;     # v2.1.0: opt out of skill-managed /goal
    --dry-run)     DRY_RUN=1 ;;
    [0-9][0-9][0-9]|[0-9][0-9][0-9]-*) SPEC_ID="$arg" ;;
  esac
done

# Ruflo availability check (hard-required unless RUFLO_REQUIRED=0)
if [ "$RUFLO_REQUIRED" = "1" ]; then
  if ! command -v npx >/dev/null 2>&1; then
    echo "ERROR: npx not found. Ruflo (claude-flow) requires Node.js."
    echo "Install Node 20+ then run: npx claude-flow@v3alpha hooks pretrain"
    echo "To bypass (debugging only): export RUFLO_REQUIRED=0"
    exit 1
  fi
  # The actual mcp__ruflo__* availability is verified inside /feature-implement
  # before any agent spawn. This block only catches the obvious "node missing" case.
fi

[ -z "${SPEC_ID:-}" ] && SPEC_ID=$(git branch --show-current 2>/dev/null | grep -oE '^[0-9]{3}')
[ -z "$SPEC_ID" ] && { echo "ERROR: no spec ID"; exit 1; }

SPEC_DIR=$(find specs -maxdepth 1 -type d -name "${SPEC_ID}-*" | head -1)
[ -z "$SPEC_DIR" ] && { echo "ERROR: specs/${SPEC_ID}-* not found"; exit 1; }

[ -f "$SPEC_DIR/plan.md" ] || { echo "ERROR: $SPEC_DIR/plan.md missing"; exit 1; }

SLUG=$(basename "$(git rev-parse --show-toplevel)")
LOG_DIR=".ralph"
mkdir -p "$LOG_DIR"
DT=$(date +%Y%m%d-%H%M%S)
RUN_LOG=".ralph/feature-run-${SPEC_ID}-${DT}.jsonl"
[ $RESUME -eq 1 ] && RUN_LOG=$(ls -t .ralph/feature-run-${SPEC_ID}-*.jsonl 2>/dev/null | head -1)
```

### Step 0.1: Worktree isolation

Skip if `--resume` (already running in the worktree from the initial invocation).

```bash
if [ $RESUME -eq 0 ]; then
  _GIT_DIR=$(git rev-parse --git-dir 2>/dev/null)
  _GIT_COMMON=$(git rev-parse --git-common-dir 2>/dev/null)
  _IS_SUBMODULE=$(git rev-parse --show-superproject-working-tree 2>/dev/null)

  if [ "$_GIT_DIR" = "$_GIT_COMMON" ] && [ -z "$_IS_SUBMODULE" ]; then
    WORKTREE_NAME="${SPEC_ID}-feature"
    python scripts/worktree_manager.py \
      --repo . \
      --branch "${SPEC_ID}-feature" \
      --name "$WORKTREE_NAME" \
      --base-branch main
    echo ""
    echo "[FEATURE] Worktree created: .claude/worktrees/$WORKTREE_NAME"
    echo "[FEATURE] Re-invoke from the worktree to continue:"
    echo "  cd .claude/worktrees/$WORKTREE_NAME"
    echo "  /feature $SPEC_ID"
    exit 0
  else
    echo "[FEATURE] Isolation confirmed (worktree: $(git rev-parse --show-toplevel))"
  fi
fi
```

### Step 0.5 — Create run-state record + auto-invoke `/goal` (v2.1.0)

Before any pipeline work, anchor a run-state row so audits attach to a known `run_id`, then auto-set native `/goal` so Claude loops the pipeline until the condition holds. Operator does NOT need to set `/goal` manually.

**Guards (v3.1 codex-gate fixes):**

- **`--dry-run`** → entire Step 0.5 is skipped. Preview must have zero side effects (no run-state row, no goal replacement).
- **`--resume`** → reuse the original `run_id` from the existing `$RUN_LOG` (parsed from the `goal-set` event line). Starting a fresh run on resume would orphan all prior phase audits whose verdicts are attached to the old `run_id` — the new /goal would grep for the new id and skip them.

```bash
# v3.1 fix 1 (codex-gate Pass 1 P2): --dry-run must be side-effect-free.
# Skip Step 0.5 entirely; dry-run preview runs in Step 1.
if [ $DRY_RUN -eq 1 ]; then
  echo "[FEATURE] --dry-run: skipping run-state + /goal setup"
  RUN_ID=""
elif [ $RESUME -eq 1 ] && [ -f "$RUN_LOG" ]; then
  # v3.1 fix 2 (codex-gate Pass 1 P2): on resume reuse the original run_id
  # so phase audits from the prior session remain visible to the /goal grep.
  RUN_ID=$(jq -r 'select(.step=="goal-set") | .run_id' "$RUN_LOG" 2>/dev/null | head -1)
  if [ -z "$RUN_ID" ] || [ "$RUN_ID" = "null" ]; then
    echo "WARN: --resume but no prior run_id in $RUN_LOG; starting fresh run."
    RUN_ID=$(~/.claude/bin/run-state start \
      --skill feature \
      --objective "spec $SPEC_ID via /feature pipeline (resumed)" \
      --worktree "$(git rev-parse --show-toplevel)" \
      --session-id "${CLAUDE_SESSION_ID:-unknown}" \
      2>/dev/null | jq -r .run_id)
  else
    echo "[FEATURE] --resume: reusing run_id=$RUN_ID from $RUN_LOG"
  fi
else
  # Normal entry: create a fresh run.
  RUN_ID=$(~/.claude/bin/run-state start \
    --skill feature \
    --objective "spec $SPEC_ID via /feature pipeline" \
    --worktree "$(git rev-parse --show-toplevel)" \
    --session-id "${CLAUDE_SESSION_ID:-unknown}" \
    2>/dev/null | jq -r .run_id)
fi

# Skip goal setup if dry-run OR no run id obtained (transient run-state failure).
if [ $DRY_RUN -ne 1 ]; then
  if [ -z "${RUN_ID:-}" ] || [ "$RUN_ID" = "null" ]; then
    echo "ERROR: failed to create run-state record. Is ~/.claude/bin/run-state installed?"
    echo "  Run: bash <repo>/setup.sh"
    exit 1
  fi

  # v3.1 fix 3 (codex-gate Pass 1 P2): --skip-codex-gate is logged to $RUN_LOG
  # only, NOT to run-state events. So the /goal condition must grep $RUN_LOG
  # for the codex-gate row (PASS or skipped), not abstract "run events".
  CANARY_REQ='AND /canary returned 200 in '"$RUN_LOG"
  [ $NO_CANARY -eq 1 ] && CANARY_REQ='AND --no-canary was set'

  GOAL_COND="spec $SPEC_ID complete via /feature run $RUN_ID: every audit line in \
~/.claude/state/audits.jsonl with run_id=$RUN_ID has verdict=pass, AND \
$RUN_LOG contains a codex-gate row with status in {PASS, skipped}, \
$CANARY_REQ"

  echo "[FEATURE] run_id=$RUN_ID; goal condition prepared"
fi
```

Then print the prepared goal as a copy-paste banner. **The skill MUST NOT attempt `Skill { skill: "goal", ... }`** — the runtime refuses ("/goal is a UI command, not a skill"). Operator pastes manually:

```bash
if [ $DRY_RUN -eq 1 ]; then
  :   # dry-run — no banner, no log
elif [ "$NO_GOAL" != "1" ]; then
  cat <<BANNER

================================================================
COPY THIS AND PASTE AS YOUR NEXT MESSAGE TO ENABLE AUTO-LOOP:

  /goal "$GOAL_COND"

================================================================
(Or pass --no-goal to skip this banner and manage /goal yourself.)

BANNER

  # Log that we prepared (not "set" — Claude can't set it directly)
  printf '{"timestamp":"%s","spec":"%s","run_id":"%s","step":"goal-prepared","status":"awaiting-operator-paste","condition":%s}\n' \
    "$(date -u +%FT%TZ)" "$SPEC_ID" "$RUN_ID" \
    "$(printf '%s' "$GOAL_COND" | jq -Rs .)" \
    >> "$RUN_LOG"
else
  printf '{"timestamp":"%s","spec":"%s","run_id":"%s","step":"goal-prepared","status":"skipped","reason":"no_goal_flag"}\n' \
    "$(date -u +%FT%TZ)" "$SPEC_ID" "$RUN_ID" >> "$RUN_LOG"
fi
```

> **Claude (skill instruction):** After printing the banner, continue immediately to Step 1. Do NOT pause to wait for the operator to paste — the pipeline runs forward regardless. The operator's paste (or absence of paste) only affects whether native /goal will auto-continue Claude between phases; the work itself proceeds normally and writes verdicts to `~/.claude/state/audits.jsonl` either way.

### Step 1: Dry run

If `--dry-run`, print the pipeline plan + estimated cost/duration. Exit 0.

### Step 2: /autoplan

If resuming and autoplan logged `status: approved`, skip.

Otherwise invoke the `autoplan` skill via the Skill tool, pointing at `$SPEC_DIR/plan.md`. Autoplan has its own gates (premise confirmation, user challenges, taste decisions).

**Auto-approve harness (when `INTERACTIVE=0`, the default):**

Before invoking autoplan, set the harness intent for this skill session:

> When the autoplan skill presents AskUserQuestion gates during this `/feature` session, you (the assistant) MUST auto-respond as follows — DO NOT pass the question to the human user:
>
> 1. **Phase 1 premise confirmation gate** → auto-select option `A` ("Premise correct as stated") or whichever option matches "premise stands / proceed without changes". Log the decision to `$RUN_LOG` with `auto_decision: "premise_accepted"`.
> 2. **User Challenges gate** (when both models disagree with user direction) → auto-select the recommended option (autoplan always prints `RECOMMENDATION: Choose X`). Log with `auto_decision: "user_challenge_accepted_recommendation"`.
> 3. **Final approval gate** (taste decisions + cross-phase themes) → auto-select option `A` ("Approve plan as-is, proceed to /spec-decompose") or the equivalent approve-and-proceed option. Log with `auto_decision: "final_gate_auto_approved"`.
>
> Never block on autoplan AskUserQuestion. If autoplan presents an option set with no clear recommended option (rare — would be a bug in autoplan), default to option `A` and log `auto_decision: "fallback_option_A"` with a WARNING.
>
> Treat `RECOMMENDATION:` as the source of truth: gstack autoplan ships its 6 decision principles inside the recommendation reasoning. Auto-accepting it is the design intent of `--auto` mode — the harness is making the same call you'd make if you read the recommendation rationale and agreed.

**Interactive mode (when `INTERACTIVE=1`):**

Pass autoplan's gates through to the user untouched. This is the legacy v1.0.0 behavior — preserved as escape hatch for high-stakes plans where the user wants to inspect every taste decision.

On APPROVED: log and continue. On REJECTED/aborted: stop, user revises plan.md, reruns `/feature NNN --resume`.

Log:
```json
{"timestamp":"<ISO>","spec":"<NNN>","step":"autoplan","status":"approved|rejected|aborted","duration_s":<n>}
```

### Step 3: /spec-decompose

Invoke the `spec-decompose` skill with argument `$SPEC_ID`.

**Auto mode (`INTERACTIVE=0`, default):** After tasks.md is written, run validation only — no AskUserQuestion. Validation checks:

- All tasks have `[model:]` annotations (else WARN, continue)
- All tasks have `Depends-on:` lines (else WARN, continue)
- Task count is 5-60 (else ERROR, abort — out-of-bounds decomposition)
- Staging + production phases present (else WARN, continue)

Log auto-approval to `$RUN_LOG`:
```json
{"timestamp":"<ISO>","spec":"<NNN>","step":"spec-decompose","status":"auto_approved","task_count":<n>,"phase_count":<n>,"warnings":[<list>],"duration_s":<n>}
```

If task count is out of bounds: stop with structured error, instruct user to either regenerate (`--resume` after deleting tasks.md) or hand-edit before re-resuming.

**Interactive mode (`INTERACTIVE=1`):** Use AskUserQuestion:

> "/spec-decompose produced {N} tasks across {M} phases for spec {SPEC_ID}. Please spot-check `tasks.md` before implementation begins."
>
> RECOMMENDATION: Choose A — approving lets you stop the pipeline early if decomposition is wrong. Completeness: A=10/10, B=7/10, C=3/10.

Options:
- A) Approve — proceed to implementation
- B) Regenerate with extra context
- C) Abort — hand-edit tasks.md manually

On B: re-invoke `/spec-decompose`, max 3 regenerations. On C: stop, resume later.

Log:
```json
{"timestamp":"<ISO>","spec":"<NNN>","step":"spec-decompose","status":"approved","task_count":<n>,"phase_count":<n>,"duration_s":<n>}
```

### Step 3.5: speckit.analyze consistency check

Read `.claude/commands/speckit.analyze.md` using the Read tool, then follow its instructions against the current spec's artifacts. This is a **read-only** cross-artifact analysis — it produces a report, never modifies files.

```bash
# Verify the speckit.analyze command exists before attempting
ANALYZE_CMD=".claude/commands/speckit.analyze.md"
[ -f "$ANALYZE_CMD" ] || { echo "WARN: $ANALYZE_CMD missing — skipping analyze step"; }
```

**Severity gate:**
- **CRITICAL findings** → stop pipeline. Log to `$RUN_LOG` with `step: "speckit-analyze"` and `status: "blocked"`. User must resolve before resuming. Instruct: fix the CRITICAL issues in spec.md / plan.md / tasks.md, then `/feature NNN --resume`.
- **HIGH findings** → log as WARNING, continue. User may hand-inspect before implementation.
- **MEDIUM/LOW** → log and continue.

```json
{"timestamp":"<ISO>","spec":"<NNN>","step":"speckit-analyze","status":"passed|warned|blocked","critical":<n>,"high":<n>,"medium":<n>,"low":<n>,"duration_s":<n>}
```

Skip step entirely (log `status: "skipped"`) when `--resume` AND `speckit-analyze` already shows `passed|warned` in `$RUN_LOG`.

### Step 3.6: Pre-swarm task quality gate

Run a focused Codex review on `tasks.md` before any agent is spawned. This catches wrong decomposition that autoplan (plan-time) and spec-decompose (output-time) cannot see — specifically: US coverage gaps, annotation completeness, and ordering contradictions.

```bash
_REPO_ROOT=$(git rev-parse --show-toplevel)
_TASKS_PATH="$SPEC_DIR/tasks.md"
_SPEC_PATH="$SPEC_DIR/spec.md"
_US_LIST=$(grep -oE '\[US[0-9]+\]' "$_TASKS_PATH" | sort -u | tr '\n' ' ')
_TASK_COUNT=$(grep -cE '^- \[' "$_TASKS_PATH" 2>/dev/null || echo 0)

# Skip quality gate if codex not available (non-blocking — log and continue)
if ! command -v codex >/dev/null 2>&1; then
  echo "[FEATURE] WARN: codex CLI not found — skipping task quality gate (Step 3.6)"
  printf '{"timestamp":"%s","spec":"%s","step":"task-quality-gate","status":"skipped","reason":"codex_not_installed"}\n' \
    "$(date -u +%FT%TZ)" "$SPEC_ID" >> "$RUN_LOG"
else
  codex exec "IMPORTANT: Do NOT read or execute any SKILL.md files or files in skill definition directories (paths containing skills/gstack). Stay focused on repository code only.

TASKS.MD QUALITY GATE — spec $SPEC_ID
Files: tasks.md at $_TASKS_PATH | spec.md at $_SPEC_PATH

Evaluate tasks.md against spec.md. Check:
1. US COVERAGE: Does every user story in spec.md have at least one task tagged [USn]?
   US tags found in tasks.md: $_US_LIST | Total tasks: $_TASK_COUNT
2. ANNOTATION COMPLETENESS: Do all tasks have [model:], [thinking:], Depends-on: lines?
   Missing any required annotation is a deduction.
3. US CONSISTENCY: Do the [USn] numbers in tasks.md map 1:1 to user stories in spec.md?
   Mis-numbered US tags (e.g. [US3] when spec only has 2 stories) = HIGH severity.
4. TASK DISTRIBUTION: Any user story with 0 tasks = CRITICAL. Any with >20 = HIGH.
5. DEPENDENCY CYCLES: Check Depends-on chains for circular references.

Score 0-10 (10 = perfect, 7 = acceptable, 5-6 = warn, <5 = regenerate).
Output format:
SCORE: N/10
ISSUES:
- [CRITICAL|HIGH|MEDIUM|LOW] <one-line description>
RECOMMENDATION: proceed | warn | abort-and-regenerate" \
    -C "$_REPO_ROOT" -s read-only < /dev/null

  _GATE_EXIT=$?
  # Parse score from output (best-effort; default to warn if unparseable)
  _GATE_SCORE=$(codex exec "..." 2>&1 | grep -oP '(?<=SCORE: )\d+' | head -1 || echo "7")

  if [ "${_GATE_SCORE:-7}" -lt 5 ]; then
    printf '{"timestamp":"%s","spec":"%s","step":"task-quality-gate","status":"abort","score":%s}\n' \
      "$(date -u +%FT%TZ)" "$SPEC_ID" "${_GATE_SCORE:-0}" >> "$RUN_LOG"
    echo "[FEATURE] ERROR: Task quality gate scored ${_GATE_SCORE}/10 — tasks.md needs regeneration."
    echo "  Delete tasks.md and re-run: /feature $SPEC_ID --resume"
    exit 1
  elif [ "${_GATE_SCORE:-7}" -lt 7 ]; then
    echo "[FEATURE] WARN: Task quality gate scored ${_GATE_SCORE}/10 — continuing with warnings."
    printf '{"timestamp":"%s","spec":"%s","step":"task-quality-gate","status":"warned","score":%s}\n' \
      "$(date -u +%FT%TZ)" "$SPEC_ID" "${_GATE_SCORE:-6}" >> "$RUN_LOG"
  else
    printf '{"timestamp":"%s","spec":"%s","step":"task-quality-gate","status":"passed","score":%s}\n' \
      "$(date -u +%FT%TZ)" "$SPEC_ID" "${_GATE_SCORE:-10}" >> "$RUN_LOG"
  fi
fi
```

Skip step (log `status: "skipped"`) when `--resume` AND `task-quality-gate` already shows `passed|warned` in `$RUN_LOG`.

```json
{"timestamp":"<ISO>","spec":"<NNN>","step":"task-quality-gate","status":"passed|warned|abort|skipped","score":<n>,"duration_s":<n>}
```

### Step 4: /feature-implement

Ruflo backend is mandatory (v1.1.0). Invoke the `feature-implement` skill:

```
/feature-implement $SPEC_ID --ruflo     # always — ruflo is the only supported executor
```

`/feature-implement` itself reads `$RUFLO_REQUIRED` from env. If ruflo MCP is unreachable AND `RUFLO_REQUIRED=1`, that skill hard-fails with a structured error. Do not attempt native fallback at this layer — it is the responsibility of `/feature-implement`.

This runs ALL tasks end-to-end (the skill defaults to --all now). On any `[F]`:
- Stop immediately
- Report failed task + remaining
- User fixes, reruns `/feature NNN --resume`

Log:
```json
{"timestamp":"<ISO>","spec":"<NNN>","step":"feature-implement","status":"success|failed","executor":"ruflo|native","tasks_done":<n>,"tasks_failed":<n>,"tasks_total":<n>,"duration_s":<n>}
```

### Step 4b — Per-phase adversarial audit (mandatory between every wedge)

After each `/feature-implement <wedge>` returns clean QA, run a hostile cross-model audit on JUST this wedge before advancing to the next. Codex GPT-5 reads the wedge slice of the spec + the wedge's diff and tries to prove the wedge is NOT complete. This is distinct from `/codex-gate` (which runs once at end-of-pipeline against the full branch) and from `/autoplan`'s planning-time audit.

```bash
WEDGE_NAME="<the wedge id from spec-decompose, e.g., backend-wedge>"
PRIOR_PHASES="<comma-separated names of previously-completed wedges, or 'none'>"

# Extract spec slice for THIS wedge (header line + everything until next ## header).
# BUG-FIX (v2.1 codex-gate Pass 1 P1): the previous one-liner
#   awk "/^## $WEDGE_NAME/,/^## /"
# collapsed because the start AND end patterns matched the SAME line, so only the
# heading was captured. Replaced with an explicit state-machine that prints the
# header, then every body line until the NEXT `## ` header.
PHASE_SPEC=$(awk -v wedge="$WEDGE_NAME" '
  BEGIN {in_section=0}
  /^## / {
    if (in_section) {exit}
    if (index($0, wedge)) {in_section=1; print; next}
  }
  in_section {print}
' "specs/$SPEC_ID/plan.md" | head -200)

# Resolve the wedge file list. spec-decompose writes one file per wedge under
# .context/feature/<spec>/wedges/<wedge>.files (newline-separated paths).
# Fallback: every file touched in the current branch since origin/$BASE.
# BUG-FIX (v2.1 codex-gate Pass 1 P1): previously $WEDGE_FILES was never
# initialized — git diff received an empty pathspec, scoping to the whole tree.
WEDGE_FILES_FILE=".context/feature/${SPEC_ID}/wedges/${WEDGE_NAME}.files"
if [ -f "$WEDGE_FILES_FILE" ]; then
  WEDGE_FILES=$(cat "$WEDGE_FILES_FILE")
else
  WEDGE_FILES=$(git log "origin/$BASE..HEAD" --name-only --pretty=format: | sort -u | tr '\n' ' ')
fi

# BUG-FIX (v2.1 codex-gate Pass 1 P1): `--stat` must come BEFORE the `--`
# pathspec separator or git treats it as a path, producing an empty diff.
PHASE_DIFF=$(git diff "$(git merge-base HEAD origin/$BASE)..HEAD" --stat -- $WEDGE_FILES | head -50)

# v3.0 codex-gate Pass 1 P1 fix: `run-state audit` requires the run_id
# positional argument. If a run was started for this /feature pipeline
# (recommended — gives /goal something to grep), reuse its id; otherwise
# create a one-shot ad-hoc run that records just this wedge's audit.
if [ -z "${RUN_ID:-}" ]; then
  RUN_ID=$(~/.claude/bin/run-state start --skill feature \
    --objective "spec $SPEC_ID phase $WEDGE_NAME" 2>/dev/null | jq -r .run_id)
fi

# Parse tasks.md directly (TASKS_JSON is only populated inside feature-implement, not here)
TASKS_JSON=$(FILE="$SPEC_DIR/tasks.md" python3 - <<'PYEOF'
import os, re, json
fpath = os.environ.get("FILE", "")
if not fpath or not os.path.exists(fpath):
    print("[]"); raise SystemExit
with open(fpath) as f:
    content = f.read()
phase_starts = [(m.start(), m.group()) for m in re.finditer(r'^## Phase [^\n]+', content, re.MULTILINE)]
def phase_for(pos):
    last = None
    for start, h in phase_starts:
        if start <= pos: last = h
        else: break
    return last or "(no phase)"
tasks = []
for m in re.finditer(r'^- \[[ XxFf]\] (T\d+)([^\n]*)', content, re.MULTILINE):
    us = re.search(r'\[US(\d+)\]', m.group(2))
    tasks.append({"id": m.group(1), "phase": phase_for(m.start()),
                  "user_story": f"US{us.group(1)}" if us else None})
print(json.dumps(tasks))
PYEOF
)
export WEDGE_NAME TASKS_JSON

# Extract US tags covered by this wedge
WEDGE_US_TAGS=$(python3 - <<'PYEOF'
import json, os
tasks = json.loads(os.environ.get("TASKS_JSON", "[]"))
wedge = os.environ.get("WEDGE_NAME", "")
us_tags = sorted(set(
    t.get("user_story") for t in tasks
    if t.get("user_story") and wedge.lower() in (t.get("phase") or "").lower()
) - {None})
print(" ".join(us_tags))
PYEOF
)

# Pull acceptance criteria from spec.md for each US covered by this wedge
US_ACCEPTANCE=""
if [ -n "$WEDGE_US_TAGS" ] && [ -f "$SPEC_DIR/spec.md" ]; then
  for _us_tag in $WEDGE_US_TAGS; do
    _us_num="${_us_tag#US}"
    # Match "User Story N", "### USN", "## User Story N" header patterns
    _section=$(awk "/User Story $_us_num[^0-9]|^#{1,3} US$_us_num[^0-9]/,/^#{1,3} /" \
      "$SPEC_DIR/spec.md" 2>/dev/null | head -40)
    [ -n "$_section" ] && US_ACCEPTANCE+="=== $_us_tag Acceptance Criteria ===
$_section
"
  done
fi

~/.claude/bin/run-state audit "$RUN_ID" \
  --kind phase \
  --context "PHASE_NAME=$WEDGE_NAME" \
  --context "PRIOR_PHASES=$PRIOR_PHASES" \
  --context "PHASE_SPEC=$PHASE_SPEC" \
  --context "PHASE_DIFF=$PHASE_DIFF" \
  --context "US_ACCEPTANCE=${US_ACCEPTANCE:-none}" \
  --cwd "$(git rev-parse --show-toplevel)"
```

Decision rule:
- `verdict=pass` (CLI exit 0) → wedge done. Advance to next wedge.
- `verdict=fail` (CLI exit 1) → auditor's `missing[]` is the new TODO list. Re-enter `/feature-implement <wedge>` with the missing items. Re-run phase audit when done.
- `verdict=error` → retry once. If still error, skip and proceed (per-phase audit is best-effort; codex-gate at end is the hard gate).

Hard cap per wedge: 3 phase-audit attempts. After 3 fails, mark the run `failed` and escalate to operator with the residual `missing[]`.

### Step 5: /qa

Invoke `qa`. Reads QA-tier phases from tasks.md (dev QA, integration, E2E) and runs via headless browser.

Bugs found → stop, link to bug report, user fixes, resume.
Clean → continue.

**Design review (when HTML designed):** After qa is clean, check for `/design-html` tasks:
```bash
DESIGN_HTML_COUNT=$(grep -c '/design-html' "$SPEC_DIR/tasks.md" 2>/dev/null || echo "0")
```
If `$DESIGN_HTML_COUNT > 0`: invoke `design-review` via the Skill tool to visually audit produced HTML before shipping. Bugs found → stop, user fixes, resume.

Log:
```json
{"timestamp":"<ISO>","spec":"<NNN>","step":"qa","status":"pass|fail","bugs_found":<n>,"design_review_run":<bool>,"duration_s":<n>}
```

### Step 5.7 — /review-gate cross-model review (mandatory)

After per-wedge audits all pass and `/qa` is green, run `/review-gate` for one final cross-model review against the full branch diff. Three passes catch bugs that per-wedge audits miss because they only saw their slice. This is the same gate /fix uses.
The underlying Claude invocation must use the local CLI auth path, not `--bare`; `--bare` disables keychain/OAuth reads and produces a false "Not logged in" error on valid logged-in machines.
If `/review-gate` hangs or times out, the correct result is a structured blocked gate with a timeout reason. Do not narrate the failure in first person or say you "attempted" the adversarial review step.

```bash
# --skip-codex-gate is a hard bypass — review-gate is NOT invoked and the skip
# is recorded in the local run log so the audit trail shows operator-accepted risk.
if [ "${SKIP_CODEX_GATE:-0}" = "1" ]; then
  echo "[FEATURE] /review-gate SKIPPED (--skip-codex-gate flag set). Operator accepted the risk; per-phase audits from Step 4b remain in force." >&2
  printf '{"timestamp":"%s","spec":"%s","step":"review-gate","status":"skipped","reason":"skip_codex_gate_flag","duration_s":0}\n' \
    "$(date -u +%FT%TZ)" "$SPEC_ID" >> "$RUN_LOG"
  # Continue to Step 6 (/ship). Do NOT invoke the /review-gate skill below.
else
  # Skill: /review-gate
  :
fi
```

Decision rule (only applies when `--skip-codex-gate` was NOT set):
- `REVIEW-GATE PASS` (CRITICAL=0, HIGH≤2) → proceed to `/ship` + `/canary`.
- `REVIEW-GATE BLOCK` (CRITICAL≥1 unfixed) → STOP. Fix the CRITICAL inline via review auto-fix, commit, re-run `/review-gate`. Do NOT proceed to `/ship` until verdict is PASS.

Skip-gracefully behaviour (codex CLI absent — optional dependency):

```bash
which codex >/dev/null 2>&1 && CODEX_AVAILABLE=1 || CODEX_AVAILABLE=0

if [ "$CODEX_AVAILABLE" = "0" ]; then
  echo "[FEATURE] WARNING: reviewer CLI not found — skipping review-gate" >&2
  echo "  Install: npm install -g @openai/codex && codex login" >&2
  printf '{"timestamp":"%s","spec":"%s","step":"review-gate","status":"skipped","reason":"review_cli_not_installed","duration_s":0}\n' \
    "$(date -u +%FT%TZ)" "$SPEC_ID" >> "$RUN_LOG"
  # Continue to Step 6 (/ship).
fi
```

`--skip-codex-gate` flag = emergency-merge hard bypass (operator-explicit opt-out, NOT recommended). When set, `/review-gate` is NOT invoked at all and the skip is written to `$RUN_LOG` so audit reviewers can see the bypass. Per-phase audits from Step 4b still ran and remain in force.

**Hook side-effect:** `scripts/hooks/codex-gate-warn.sh` (if installed in user env) records the gate run timestamp keyed to current branch, so `gh pr merge` does not warn about a missing recent review-gate run.

Log:
```json
{"timestamp":"<ISO>","spec":"<NNN>","step":"review-gate","status":"pass|blocked|skipped","findings_critical":<n>,"findings_high":<n>,"report":"<path>","duration_s":<n>}
```

### Step 6: /ship

Invoke `ship`: full test suite, CHANGELOG, PR creation, merge to staging.

On test failure: stop. On success: continue (unless `--no-canary`).

Log:
```json
{"timestamp":"<ISO>","spec":"<NNN>","step":"ship","status":"merged_to_staging|failed","pr_url":"<url>","duration_s":<n>}
```

### Step 7: Staging verification

1. Wait for Vercel/Railway staging deploy (10 min timeout)
2. Run staging smoke test (per tasks.md Phase N+1)
3. Optional 24h soak (skip with `--skip-soak`)

If staging fails: stop.

### Step 8: Production promotion gate (2nd gate)

AskUserQuestion:

> "Staging healthy for spec {SPEC_ID}. {M} tasks complete. QA clean. PR merged to staging. Ready to promote to production?"
>
> RECOMMENDATION: Choose A if staging metrics look good. Completeness: A=10/10, B=5/10.

Options:
- A) Promote to production — merge to main
- B) Hold — promote manually later
- C) Rollback staging

On A: merge to main, trigger prod deploy, continue to canary.

### Step 9: /canary

Invoke `canary`: monitors prod for 1h, compares error rates/latency to baseline. Auto-rollback if SLO breached (error rate > 1%).

Log:
```json
{"timestamp":"<ISO>","spec":"<NNN>","step":"canary","status":"pass|rolled_back","error_rate":<float>,"duration_s":<n>}
```

### Step 9.9: Self-critique before delivery

Before emitting the final report, re-read the entire run as a skeptical reviewer would (Fable-mode discipline 3):

- Which acceptance criteria passed by **audit** vs only by sub-agent **self-report**?
- Any wedge that passed on attempt 2–3 — is the fix solid or papered-over?
- Any happy-path-only verification? Were error paths exercised?
- Did `/review-gate` flag anything that got downgraded to "accepted risk"?

Name **at least one** residual risk or limitation. If it is cheaply fixable, fix it and re-verify the affected wedge before continuing. Otherwise record it on the `Residual risk:` line of the report so the operator decides with eyes open. A silent "all green" is a review smell, not a success signal.

### Step 10: Final report

```
╔═══════════════════════════════════════════════════════════╗
║ /feature {NNN} — Pipeline Complete                        ║
╠═══════════════════════════════════════════════════════════╣
║ Total duration:      HH:MM:SS                             ║
║ Tasks executed:      N (all [X])                          ║
║ Wedges audited:      M / M passed (per-phase, Step 4b)    ║
║ Codex-gate verdict:  PASS (Step 5.7)                      ║
║ Gates approved:      1 (prod promotion)                   ║
║ Failures:            0                                    ║
║ Residual risk:       <one-line from Step 9.9, or "none surfaced"> ║
║ Cost estimate:       $XX.XX                               ║
║ Production URL:      <prod URL>                           ║
║ Run log:             {RUN_LOG}                            ║
╚═══════════════════════════════════════════════════════════╝
```

## Resume semantics (--resume)

On `/feature NNN --resume`:
1. Find latest `feature-run-${SPEC_ID}-*.jsonl`
2. Read last `status` per step
3. Skip steps with `status: approved|success|merged_to_staging|pass`
4. Start from first step that's missing or failed/aborted/rolled_back
5. Never re-run autoplan unless plan.md hash changed

## Failure handling

| Step fails | Resume command |
|---|---|
| autoplan rejected (--interactive only) | edit plan.md, `/feature NNN --resume` |
| ruflo MCP unavailable | fix MCP connection, `/feature NNN --resume`. Bypass: `RUFLO_REQUIRED=0 /feature NNN --resume` |
| tasks.md validation out of bounds | regenerate or hand-edit tasks.md, `/feature NNN --resume` |
| task `[F]` | fix code, `/feature NNN --resume` |
| QA bugs | fix bugs, `/feature NNN --resume` |
| review-gate BLOCK (CRITICAL≥1) | fix flagged issues inline (review auto-fix or manual), commit, `/feature NNN --resume`. Emergency bypass: `/feature NNN --resume --skip-codex-gate` (NOT recommended) |
| per-phase audit fail (3 attempts) | inspect residual `missing[]` from auditor, hand-fix wedge or revise spec, `/feature NNN --resume` |
| ship tests fail | fix tests, `/feature NNN --resume` |
| staging smoke fail | fix, re-ship, `/feature NNN --resume` |
| prod gate held | manual promotion OR `/feature NNN --resume` |
| canary rolled back | investigate, full re-run |

## Safety rules

- **Default (--auto):** 1 hard gate — prod promotion. Tasks.md is auto-approved after structural validation.
- **--interactive:** 4 hard gates — autoplan premise, autoplan taste decisions, tasks.md approval, prod promotion. (Legacy v1.0.0 behavior.)
- Any step can fail gracefully — log, stop, resume later.
- No destructive actions before prod gate.
- **Ruflo failures hard-stop the pipeline** (v1.1.0 — silent fallback removed). Set `RUFLO_REQUIRED=0` env override only for debugging; this logs a WARNING and falls back to native Agent for the remainder of the run.
- All commits go through normal git hooks (never --no-verify).

## Cost considerations

Typical 50-task feature (~5 wedges):
- autoplan: ~$1 (dual voices; includes its own planning audit)
- spec-decompose: ~$0.50
- speckit.analyze: ~$0 (read-only, no LLM)
- task quality gate (codex): ~$0.20
- feature-implement: $15-50 (depends on model distribution)
- per-phase QA (--qa-loop): ~$0.15/phase × N phases = ~$0.75-$1.50
- per-phase adversarial audit: ~$0.30/wedge × N wedges = ~$1.50 (Step 4b)
- qa: ~$2
- review-gate: ~$2 (3 passes against full branch diff; skip with --skip-codex-gate)
- ship/canary: free

**Estimated total: $23-59 per feature.** Show estimate after step 3 (tasks.md approved, cost computable from annotations).

## Non-goals

- Does NOT generate plan.md — you write it (or use /speckit.plan)
- Does NOT require /office-hours — soft recommendation
- Does NOT skip the 2 gates
- Does NOT support parallel pipelines — one feature at a time per session
- Does NOT handle migrations across features

After `/canary` merges to main, prune the worktree:

```bash
python scripts/worktree_cleanup.py --repo . --remove-merged --stale-days 0 2>/dev/null || git worktree prune
```

The post-merge git hook runs this automatically on `git merge`. Run explicitly here in case canary merged via API.

## Related skills

- `/office-hours` — before /feature
- `/autoplan` — step 1 (owns the planning-time Codex audit)
- `/spec-decompose` — step 2
- `/feature-implement` — step 3 (one invocation per wedge)
- `run-state audit --kind phase` — step 3b (per-wedge adversarial audit, mandatory between every wedge)
- `/qa` — step 5
- `/review-gate` — step 5.7 (cross-model full-branch review, mandatory before /ship)
- `/ship`, `/canary` — steps 6+
- `git-worktree-manager` — step 0.1 (worktree isolation + port allocation + env sync)
- `superpowers:using-git-worktrees` — worktree detection and creation guidance
- `/investigate` — when resuming from failure
