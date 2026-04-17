---
name: feature
description: "End-to-end pipeline: autoplan → spec-decompose → feature-implement (ruflo) → qa → ship → canary. One command, 2 gates."
version: "1.0.0"
allowed-tools:
  - Read
  - Edit
  - Bash
  - Glob
  - Skill
  - AskUserQuestion
---

# /feature — End-to-end feature pipeline

One command. Runs everything from plan review through production. Only stops for 2 user approvals and any failure.

## When to invoke

- You have `specs/NNN-feature-name/plan.md` ready for review
- You want to ship the feature through the full pipeline with minimal ceremony
- User says: "run feature NNN", "ship feature NNN end-to-end", "autoplan through deploy"

## Prerequisites

- Git branch checked out (ideally `NNN-feature-name`)
- `specs/NNN-feature-name/plan.md` exists and is ready for review
- `specs/NNN-feature-name/spec.md` is optional — `/spec-decompose` handles missing spec.md
- `/office-hours` may have been run (not required, but design doc improves autoplan quality)
- Ruflo pretrain ideally completed once: `npx claude-flow@v3alpha hooks pretrain`

## Invocation

```
/feature [NNN]              # full pipeline, ruflo-backed implementation (default)
/feature [NNN] --resume     # resume after failure (picks up at last incomplete step)
/feature [NNN] --no-ruflo   # use native Agent tool for implementation instead
/feature [NNN] --no-canary  # stop after /ship (skip production canary)
/feature [NNN] --dry-run    # print the pipeline plan, don't execute
```

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

**Total gates: 2** (tasks.md approval, prod promotion). Everything else auto-runs or auto-fails.

## Step-by-step workflow

### Step 0: Resolve spec and verify prerequisites

```bash
SPEC_ARG="${ARGUMENTS:-}"
RESUME=0
NO_RUFLO=0
NO_CANARY=0
DRY_RUN=0

# BUG-1 fix (2026-04-16): word-split $SPEC_ARG explicitly via `read -ra`.
read -ra _SPEC_ARGS <<< "$SPEC_ARG"
for arg in "${_SPEC_ARGS[@]}"; do
  case "$arg" in
    --resume)    RESUME=1 ;;
    --no-ruflo)  NO_RUFLO=1 ;;
    --no-canary) NO_CANARY=1 ;;
    --dry-run)   DRY_RUN=1 ;;
    [0-9][0-9][0-9]|[0-9][0-9][0-9]-*) SPEC_ID="$arg" ;;
  esac
done

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

On APPROVED: log and continue. On REJECTED/aborted: stop, user revises plan.md, reruns `/feature NNN --resume`.

Log:
```json
{"timestamp":"<ISO>","spec":"<NNN>","step":"autoplan","status":"approved|rejected|aborted","duration_s":<n>}
```

### Step 3: /spec-decompose

Invoke the `spec-decompose` skill with argument `$SPEC_ID`.

After tasks.md is written, AskUserQuestion:

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

Default: ruflo backend. Invoke the `feature-implement` skill:

```
/feature-implement $SPEC_ID --ruflo     # if NO_RUFLO=0 (default)
/feature-implement $SPEC_ID             # if NO_RUFLO=1
```

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

### Step 10: Final report

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
| autoplan rejected | edit plan.md, `/feature NNN --resume` |
| tasks.md not approved | hand-edit tasks.md, `/feature NNN --resume` |
| task `[F]` | fix code, `/feature NNN --resume` |
| QA bugs | fix bugs, `/feature NNN --resume` |
| ship tests fail | fix tests, `/feature NNN --resume` |
| staging smoke fail | fix, re-ship, `/feature NNN --resume` |
| prod gate held | manual promotion OR `/feature NNN --resume` |
| canary rolled back | investigate, full re-run |

## Safety rules

- 2 hard gates: tasks.md approval + prod promotion. Non-negotiable.
- Any step can fail gracefully — log, stop, resume later
- No destructive actions before prod gate
- Ruflo errors fall back to native Agent transparently — no pipeline halt from ruflo issues
- All commits go through normal git hooks (never --no-verify)

## Cost considerations

Typical 50-task feature:
- autoplan: ~$1 (dual voices)
- spec-decompose: ~$0.50
- feature-implement: $15-50 (depends on model distribution)
- per-phase QA (--qa-loop): ~$0.15/phase × N phases = ~$0.75-$1.50
- qa: ~$2
- ship/canary: free

**Estimated total: $20-55 per feature.** Show estimate after step 3 (tasks.md approved, cost computable from annotations).

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
- `/qa`, `/ship`, `/canary` — steps 4-6
- `/investigate` — when resuming from failure
