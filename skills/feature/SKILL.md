---
name: feature
description: "End-to-end pipeline: autoplan → spec-decompose → feature-implement (ruflo) → qa → codex-gate → ship → canary. Non-interactive by default. Use --interactive to restore gates. Persistent run-state via /run-state lib in v1.3.0."
version: "1.3.0"
allowed-tools:
  - Read
  - Edit
  - Bash
  - Glob
  - Skill
  - AskUserQuestion
---

# /feature — End-to-end feature pipeline

## Run-state lifecycle (mandatory)

Every `/feature` invocation MUST:

1. **Start a run** — capture run_id BEFORE autoplan:

   ```bash
   SPEC_ID="<the spec NNN-name passed by user>"
   RUN_ID=$(~/.claude/bin/run-state start \
     --skill feature \
     --objective "ship $SPEC_ID through feature pipeline" \
     --tokens "${FEATURE_TOKENS_BUDGET:-1500000}" \
     --session-id "${CLAUDE_SESSION_ID:-}" \
     --worktree "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" \
     | jq -r .run_id)
   echo "Run: $RUN_ID"
   ```

2. **Update phase at every pipeline boundary:**

   ```bash
   ~/.claude/bin/run-state update "$RUN_ID" --phase <stage>
   # stages: autoplan | spec-decompose | implement | qa | audit | ship | canary
   ```

3. **Run spec-completion audit BEFORE the canary phase** (see Step 6).

4. **Mark complete only after canary success.**

Token budget default 1.5M (vs /fix's 300K) because /feature spans more phases. Override with `FEATURE_TOKENS_BUDGET` env or `--tokens` flag. Stop hook auto-continues if state=active.

## Flags

| Flag | Effect |
|---|---|
| `--tokens N` | Token budget. Default 1.5M. When exceeded → `budget_limited`; operator must `run-state resume`. |
| `--interactive` | Restore manual gates (autoplan premise, taste decisions). Default = non-interactive. |
| `--no-audit` | Skip spec-completion audit before canary. **NOT recommended.** |

## When to invoke

- You have `specs/NNN-feature-name/plan.md` ready for review
- You want to ship the feature through the full pipeline with minimal ceremony
- User says: "run feature NNN", "ship feature NNN end-to-end", "autoplan through deploy"

## Prerequisites

- Git branch checked out (ideally `NNN-feature-name`)
- `specs/NNN-feature-name/plan.md` exists and is ready for review
- `specs/NNN-feature-name/spec.md` is optional — `/spec-decompose` handles missing spec.md
- `/office-hours` may have been run (not required, but design doc improves autoplan quality)
- **Ruflo MCP must be available.** Pretrain once: `npx claude-flow@v3alpha hooks pretrain`. The pipeline hard-fails if `mcp__ruflo__*` tools are not reachable.

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
/feature [NNN] --no-codex-gate  # skip cross-model adversarial review (default-on, see Step 4.5)
/feature [NNN] --dry-run    # print the pipeline plan, don't execute
```

**Removed in v1.1.0:** `--no-ruflo` flag. Ruflo is now mandatory (no silent fallback to native Agent). If ruflo MCP is unavailable, the pipeline hard-fails with a structured error directing the user to fix the MCP connection. To debug ruflo issues, set `RUFLO_REQUIRED=0` in env (escape hatch — falls back to native, logs WARNING).

## The pipeline

```
┌─────────────────────────────────────────────────────────────┐
│  /feature NNN                                               │
│                                                             │
│  Step 1: /autoplan on specs/NNN/plan.md                     │
│    └─ GATE: user approves review (taste decisions, etc.)    │
│                                                             │
│  Step 2: /spec-decompose NNN                                │
│    └─ GATE: user approves tasks.md (spot-check quality)     │
│                                                             │
│  Step 3: /feature-implement NNN --ruflo (or --no-ruflo)     │
│    └─ Auto-stops on first task failure                      │
│    └─ Runs ALL tasks end-to-end on success                  │
│                                                             │
│  Step 3.5: Per-phase QA (inside /feature-implement)         │
│    └─ 2 deterministic hooks (vitest/pytest) + 3 LLM agents  │
│    └─ Investigate → fix → re-qa loop (max 3 retries)        │
│                                                             │
│  Step 4: /qa (full-suite browser test after ALL phases)     │
│    └─ Auto-stops if bugs found; user fixes, /feature --resume│
│                                                             │
│  Step 4.5: /codex-gate (cross-model adversarial review)     │
│    └─ Codex GPT-5 in 3 passes: review + adversarial + gaps  │
│    └─ Auto-stops on CRITICAL findings (writes artifact)     │
│    └─ Skip with --no-codex-gate (opt out, NOT recommended)  │
│    └─ Skip-gracefully if codex CLI absent (warn, continue)  │
│                                                             │
│  Step 5: /ship (creates PR, merges to staging branch)       │
│    └─ Staging deploy via Vercel preview / Railway staging   │
│    └─ Staging smoke test runs automatically                 │
│                                                             │
│  Step 6: /canary (production promotion)                     │
│    └─ FINAL GATE: user approves prod promotion              │
│    └─ Merge to main → Vercel prod deploy                    │
│    └─ 1h canary monitor (error rate < 1%)                   │
│    └─ Auto-rollback if SLO breached                         │
└─────────────────────────────────────────────────────────────┘
```

**Total gates (default --auto):** 1 — prod promotion only (irreversible). Codex-gate CRITICAL findings auto-stop the pipeline but produce structured artifacts (no human prompt) — user fixes and resumes.
**Total gates (--interactive):** 4 — autoplan premise, autoplan taste decisions, tasks.md approval, prod promotion.
Everything else auto-runs or auto-fails.

## Step-by-step workflow

### Step 0: Resolve spec and verify prerequisites

```bash
SPEC_ARG="${ARGUMENTS:-}"
RESUME=0
NO_CANARY=0
DRY_RUN=0
# v1.2.0: codex-gate is default-on. --no-codex-gate opts out.
NO_CODEX_GATE=0
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
    --no-codex-gate) NO_CODEX_GATE=1 ;;
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

### Step 5: /qa

Invoke `qa`. Reads QA-tier phases from tasks.md (dev QA, integration, E2E) and runs via headless browser.

Bugs found → stop, link to bug report, user fixes, resume.
Clean → continue.

Log:
```json
{"timestamp":"<ISO>","spec":"<NNN>","step":"qa","status":"pass|fail","bugs_found":<n>,"duration_s":<n>}
```

### Step 5.5: /codex-gate (cross-model adversarial review)

Skip if `--no-codex-gate` flag was set OR `RESUME=1` and prior `step:"codex-gate"` log entry already has `status:"pass"`.

**Why this step exists:** Single-model review has blind spots. Codex (OpenAI GPT-5) reads the same diff Claude wrote and runs 3 adversarial passes (general review + attacker mindset + test-coverage gap analysis). Documented to catch CRITICAL bugs that pass clean through Claude-only quality gates. Cost: ~$2 + ~13 min — cheap insurance vs hotfix-cycle days for any PR with production blast radius (auth, payments, RLS, multi-tenant, cron, infra scripts, disaster-recovery).

**Pre-flight:**
```bash
which codex >/dev/null 2>&1 && CODEX_AVAILABLE=1 || CODEX_AVAILABLE=0

if [ "$CODEX_AVAILABLE" = "0" ]; then
  echo "[FEATURE] WARNING: codex CLI not found — skipping codex-gate" >&2
  echo "  Install: npm install -g @openai/codex && codex login" >&2
  # Log skip and continue — do NOT hard-fail (codex is optional dependency).
  printf '{"timestamp":"%s","spec":"%s","step":"codex-gate","status":"skipped","reason":"codex_not_installed","duration_s":0}\n' \
    "$(date -u +%FT%TZ)" "$SPEC_ID" >> "$RUN_LOG"
  # Continue to Step 6.
else
  : # Run gate.
fi
```

**Invoke:** Call the `codex-gate` skill via the Skill tool. Codex-gate runs 3 passes against the staged diff (`git diff main...HEAD`), produces a findings report at `~/.gstack/projects/$SLUG/codex-gate-${SPEC_ID}-${DT}.md`.

**Findings policy:**
- **CRITICAL** → STOP. Write findings to `.ralph/feature-run-${SPEC_ID}-codex-critical.md`, log status:`failed`, exit 1. User fixes, reruns `/feature NNN --resume`.
- **HIGH** → log as concern, continue (user reviews report before merging PR).
- **MEDIUM/LOW or no findings** → continue silently.

**Hook side-effect:** `scripts/hooks/codex-gate-warn.sh` (if installed in user env) records the gate run timestamp keyed to current branch, so `gh pr merge` does not warn about a missing recent codex-gate run.

Log:
```json
{"timestamp":"<ISO>","spec":"<NNN>","step":"codex-gate","status":"pass|failed|skipped","findings_critical":<n>,"findings_high":<n>,"report":"<path>","duration_s":<n>}
```

### Step 6 — Spec-completion audit (mandatory unless `--no-audit`)

Before canary deploy, run a hostile audit that tries to prove the spec is NOT complete. Codex GPT-5 reads the spec and the diff.

```bash
SPEC_PATH="specs/${SPEC_ID}/plan.md"
SPEC_CONTENT=$(cat "$SPEC_PATH")
MODIFIED=$(git diff "origin/${BASE:-main}"...HEAD --name-only)

~/.claude/bin/run-state audit "$RUN_ID" \
  --kind feature \
  --context "SPEC_PATH=$SPEC_PATH" \
  --context "SPEC_CONTENT=$SPEC_CONTENT" \
  --context "MODIFIED_FILES=$MODIFIED" \
  --cwd "$(git rev-parse --show-toplevel)"
```

Decision rule:
- `verdict=pass` → audit CLI auto-marks `complete` and clears marker. Override: re-set state to `active` before canary so the Stop hook continues until canary succeeds, then mark complete explicitly:

  ```bash
  ~/.claude/bin/run-state update "$RUN_ID" --state active
  # ... run /canary ...
  ~/.claude/bin/run-state complete "$RUN_ID"
  ```

- `verdict=fail` → re-enter implement phase with the auditor's `missing` list as the new TODO. Stop hook continues the loop.
- `verdict=error` → retry once. Then mark `failed` if still error.

Hard cap: 3 audit attempts → `failed`.

### Step 7: /ship

Invoke `ship`: full test suite, CHANGELOG, PR creation, merge to staging.

On test failure: stop. On success: continue (unless `--no-canary`).

Log:
```json
{"timestamp":"<ISO>","spec":"<NNN>","step":"ship","status":"merged_to_staging|failed","pr_url":"<url>","duration_s":<n>}
```

### Step 8: Staging verification

1. Wait for Vercel/Railway staging deploy (10 min timeout)
2. Run staging smoke test (per tasks.md Phase N+1)
3. Optional 24h soak (skip with `--skip-soak`)

If staging fails: stop.

### Step 9: Production promotion gate (2nd gate)

AskUserQuestion:

> "Staging healthy for spec {SPEC_ID}. {M} tasks complete. QA clean. PR merged to staging. Ready to promote to production?"
>
> RECOMMENDATION: Choose A if staging metrics look good. Completeness: A=10/10, B=5/10.

Options:
- A) Promote to production — merge to main
- B) Hold — promote manually later
- C) Rollback staging

On A: merge to main, trigger prod deploy, continue to canary.

### Step 10: /canary

Invoke `canary`: monitors prod for 1h, compares error rates/latency to baseline. Auto-rollback if SLO breached (error rate > 1%).

Log:
```json
{"timestamp":"<ISO>","spec":"<NNN>","step":"canary","status":"pass|rolled_back","error_rate":<float>,"duration_s":<n>}
```

### Step 11: Final report

```
╔═══════════════════════════════════════════════════════════╗
║ /feature {NNN} — Pipeline Complete                        ║
╠═══════════════════════════════════════════════════════════╣
║ Total duration:      HH:MM:SS                             ║
║ Tasks executed:      N (all [X])                          ║
║ Gates approved:      2 (tasks.md, prod promotion)         ║
║ Failures:            0                                    ║
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
| codex-gate CRITICAL | fix flagged issues (see `.ralph/feature-run-${SPEC_ID}-codex-critical.md`), `/feature NNN --resume`. Bypass: `/feature NNN --resume --no-codex-gate` |
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

Typical 50-task feature:
- autoplan: ~$1 (dual voices)
- spec-decompose: ~$0.50
- feature-implement: $15-50 (depends on model distribution)
- per-phase QA (--qa-loop): ~$0.15/phase × N phases = ~$0.75-$1.50
- qa: ~$2
- codex-gate: ~$2 (3 passes against final diff; skip with --no-codex-gate)
- ship/canary: free

**Estimated total: $22-57 per feature.** Show estimate after step 3 (tasks.md approved, cost computable from annotations).

## Non-goals

- Does NOT generate plan.md — you write it (or use /speckit.plan)
- Does NOT require /office-hours — soft recommendation
- Does NOT skip the 2 gates
- Does NOT support parallel pipelines — one feature at a time per session
- Does NOT handle migrations across features

## Related skills

- `/office-hours` — before /feature
- `/autoplan` — step 1
- `/spec-decompose` — step 2
- `/feature-implement` — step 3
- `/qa` — step 5
- `/codex-gate` — step 5.5 (cross-model adversarial review, default-on)
- `/ship`, `/canary` — steps 6+
- `/investigate` — when resuming from failure
