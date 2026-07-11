---
name: plan-decompose
description: "Turn a description or existing plan into tasks.md via autonomous eng review + codex gate — no speckit interview"
version: "1.2.0"
permissions:
  filesystem: write
  network: false
allowed-tools:
  - Read
  - Write
  - Bash
  - Glob
  - Grep
  - Skill
  - Agent
metadata:
  openclaw:
    requires:
      bins: []
      env: []
---

# plan-decompose — Autonomous plan → tasks.md (no speckit)

## When to invoke

- You have a description, GitHub issue, or rough plan and want tasks.md without the full speckit interview
- You've already run `/plan-eng-review` and have a plan.md but need tasks.md
- You want to skip `/feature-spec` (speckit.specify + speckit.plan + speckit.clarify) for speed

## Relationship to other skills

```
/feature-spec        — full speckit ritual (specify → plan → clarify → spec.md + plan.md)
/plan-decompose      — lightweight path (plan-eng-review autonomous → codex gate → spec.md synthetic → tasks.md)
/spec-decompose      — lowest-level: requires spec.md + plan.md already present
/feature-implement   — executes tasks.md
```

## Invocation

```
/plan-decompose NNN                        # existing specs/NNN-* dir with or without plan.md
/plan-decompose "description"             # create new spec NNN (next available slot)
/plan-decompose NNN --from-issue GH#N     # pull context from GitHub issue
/plan-decompose NNN --no-codex            # skip codex reviews (faster, less safe)
/plan-decompose NNN --dry-run             # print pipeline plan, no writes
```

## Workflow

### Step 0: Resolve spec directory

```bash
ARGUMENTS="${ARGUMENTS:-}"
NO_CODEX=0
FROM_ISSUE=""
DRY_RUN=0

read -ra _ARGS <<< "$ARGUMENTS"
for arg in "${_ARGS[@]}"; do
  case "$arg" in
    --no-codex)      NO_CODEX=1 ;;
    --from-issue=*)  FROM_ISSUE="${arg#--from-issue=}" ;;
    --dry-run)       DRY_RUN=1 ;;
    [0-9][0-9][0-9]|[0-9][0-9][0-9]-*) SPEC_ID="$arg" ;;
  esac
done

# If no spec ID: create one from next available NNN
if [ -z "${SPEC_ID:-}" ]; then
  LAST=$(ls -d specs/[0-9][0-9][0-9]-* 2>/dev/null | tail -1 | grep -oE '^specs/[0-9]+' | grep -oE '[0-9]+' || echo "000")
  SPEC_ID=$(printf "%03d" $((10#$LAST + 1)))
  SLUG=$(echo "${ARGUMENTS}" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/^-//;s/-$//' | cut -c1-40)
  mkdir -p "specs/${SPEC_ID}-${SLUG:-new}"
fi

SPEC_DIR=$(find specs -maxdepth 1 -type d -name "${SPEC_ID}-*" 2>/dev/null | head -1)
[ -z "$SPEC_DIR" ] && { echo "ERROR: specs/${SPEC_ID}-* not found"; exit 1; }

if [ "$DRY_RUN" = "1" ]; then
  echo "=== plan-decompose DRY RUN ==="
  echo "  spec dir:     $SPEC_DIR"
  echo "  from-issue:   ${FROM_ISSUE:-none}"
  echo "  codex:        $([ $NO_CODEX = 1 ] && echo disabled || echo enabled)"
  echo "  plan-eng-review: autonomous (spawned-session mode)"
  echo "  pipeline: plan.md → codex review → spec.md (synthetic) → spec-decompose → tasks.md → codex score gate"
  exit 0
fi
```

### Step 1: Recall prior decompositions (fail-soft)

**gbrain recall:** if `command -v gbrain` succeeds and
`env -u DATABASE_URL gbrain doctor` reports `[OK] connection`, run
`env -u DATABASE_URL gbrain query "<task topic>"` and feed prior decisions into
Step 2. Absent/unhealthy → fallback
`git log --oneline --grep="<topic>" | head -5`; never block (see
`docs/gbrain-optional.md`). gsd's own learnings live in `.planning/` history +
`/gsd-mempalace-recall` where enabled.

### Step 2: Run plan-eng-review (autonomous)

**Mechanism:** Invoke the `plan-eng-review` skill via the `Skill` tool. When invoked
this way, the skill runs in SPAWNED_SESSION mode — it does NOT use AskUserQuestion,
auto-decides all recommendations using the 6 Decision Principles, and runs to completion.

Pass these instructions to plan-eng-review:

> "Run in fully autonomous mode. Accept all recommendations without asking the operator.
> Use the 6 Decision Principles for every decision. Do not use AskUserQuestion. Run to
> completion and return the full engineering plan as your output.
>
> Context: ${ARGUMENTS}
> GitHub issue (if any): ${FROM_ISSUE}
> Prior decomposition patterns: ${PRIOR_PATTERNS_SUMMARY}
>
> Produce a complete engineering plan covering: phases, tech stack, risks, acceptance
> criteria per user story, dependencies, and a rough task breakdown per phase."

**If plan.md already exists** in `$SPEC_DIR`: skip Step 2, use existing plan.md.
Log: `[plan-decompose] Step 2: plan.md already present — skipping plan-eng-review`

Write the plan output to `$SPEC_DIR/plan.md`.

### Step 3: Codex adversarial review of plan.md

Skip entirely when `--no-codex` passed.

**Guarded direct invocation (v1.2.0, MANDATORY — hang prevention).** Do NOT
invoke codex bare or via a consult-skill wrapper. A bare `codex exec` with stdin
open blocks forever on "Reading additional input from stdin..." — observed as a
30+ min silent hang on a ~120-line plan (the run left a 2-line session file with
no agent message; the guarded retry finished in <9 min). Contract:

```bash
timeout 540 codex exec --sandbox read-only \
  -c model="${PLAN_ADVERSARY_MODEL:-gpt-5.6-sol}" \
  -c model_reasoning_effort="${PLAN_ADVERSARY_EFFORT:-xhigh}" \
  "$PROMPT" </dev/null >"$OUT_FILE" 2>&1
RC=$?   # bare exit code — NEVER pipe the live call (`| tail` masks the timeout kill)
```

- `</dev/null` is load-bearing (stdin-wait hang); `timeout 540` caps the call
  (`gtimeout` on macOS without coreutils `timeout`).
- `RC=124` → fail-soft: log `[plan-decompose] codex plan review TIMEOUT — advisory
  skipped` and continue; downstream gates (opus plan-checker, review-gate) still hold.
- Read findings/verdict from `$OUT_FILE` after exit — never stream-parse the live run.

Embed the full content of `plan.md` verbatim in the prompt (the scope line
prevents repo-wandering, the other major review-time multiplier):

> "You are a brutally honest senior engineer. Review this engineering plan adversarially.
> Find: logical gaps, unstated assumptions, missing error handling, overcomplexity,
> missing edge cases, unclear ownership, contradictions. Score each finding:
> CRITICAL / HIGH / MEDIUM / LOW.
> Do NOT read repository trees (.claude/, .codex/, skills/, agents/) or invoke
> any review skill — review ONLY the plan text below.
>
> THE PLAN:
> <plan.md content verbatim>
>
> Return: numbered list of findings with severity, specific location in plan, and
> recommended fix. Conclude with overall verdict: APPROVE / APPROVE-WITH-FIXES / REJECT."

**Decision:**
- Verdict REJECT, or any CRITICAL finding: fix `plan.md`, re-run codex review once (max 1 retry).
- Verdict APPROVE-WITH-FIXES with HIGH findings: apply HIGH fixes to `plan.md`, continue.
- Verdict APPROVE: continue.

Log finding counts: `[plan-decompose] codex plan review: C:{critical} H:{high} M:{medium} verdict:{verdict}`

### Step 4: Write synthetic spec.md (if absent)

Skip if `$SPEC_DIR/spec.md` already exists.

Extract from `plan.md`:
1. All user stories (look for "User Story N", "US-N", "## User Stories" sections)
2. Acceptance criteria per user story
3. Tech stack constraints

Write minimal `$SPEC_DIR/spec.md` with this structure:

```markdown
# Spec: <title from plan.md>

> Synthetic spec generated by /plan-decompose from plan.md.
> For full context see plan.md.

## User Stories

### User Story 1: <title>
**As a** <role>, **I want to** <action>, **so that** <outcome>.

**Acceptance Criteria:**
- [ ] <criterion 1>
- [ ] <criterion 2>

[... repeat for each US found in plan.md ...]

## Constraints

- <tech stack constraints from plan.md>
```

This satisfies spec-decompose's hard contract (`spec.md` required).

### Step 5: Run spec-decompose

Invoke `Skill: spec-decompose` with `$SPEC_ID`.

This writes `$SPEC_DIR/tasks.md` with annotated task list.

### Step 6: Codex score gate on tasks.md

Skip entirely when `--no-codex` passed.

Mirrors `/feature` Step 3.6 exactly. Run `codex exec` on `tasks.md` vs `spec.md`
under the same guarded invocation contract as Step 3 (`timeout 540`, `</dev/null`,
output file, bare exit code):

Check:
- US coverage (every spec.md US has at least 1 task)
- Annotation completeness (`[model:]`, `[agent:]`, `[USn]` present)
- Dependency cycles (no circular depends_on)
- US consistency (task descriptions match their US)
- Task distribution (no phase has >60% of all tasks)

Score:
- `<5` → **ABORT** — print failing checks, exit 1
- `5-6` → **WARN** — log warning, continue
- `≥7` → **PASS**

Skip gracefully if `codex` CLI absent (`which codex` fails): log warning, continue. Non-blocking.

### Step 7: Store the decision (fail-soft)

If gbrain is healthy: `env -u DATABASE_URL gbrain put spec/<NNN>-decompose
"<one-line outcome: verdict, score, plan path>"` then
`env -u DATABASE_URL gbrain sync --no-pull --no-embed`. gsd captures its own
phase learnings via `/gsd-extract-learnings`. Skip silently when absent —
storage is enhancement, not gate.

### Step 8: Report

```
┌─ plan-decompose ${SPEC_ID} complete ────────────────────────┐
│ plan.md:       ${SPEC_DIR}/plan.md                          │
│ spec.md:       ${SPEC_DIR}/spec.md (synthetic|existing)     │
│ tasks.md:      ${SPEC_DIR}/tasks.md                         │
│ codex plan:    N findings (C:0 H:1 M:2) verdict:APPROVE     │
│ tasks score:   8/10 (pass)                                  │
│ pattern:       ruflo://agentdb/<id>  (or "skipped")         │
│                                                             │
│ next: /feature-implement ${SPEC_ID}                         │
└─────────────────────────────────────────────────────────────┘
```

## Non-goals

- Does NOT run plan-eng-review interactively — always autonomous (spawned-session mode)
- Does NOT generate BDD scenarios or E2E test stubs (use `/feature-spec` for that)
- Does NOT commit or push anything
- Does NOT replace `/feature-spec` when you need a thorough spec interview
