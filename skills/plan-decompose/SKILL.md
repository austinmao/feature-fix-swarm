---
name: plan-decompose
description: "Create and decompose a lightweight plan with opposite-first review, one bounded read-only active-host fallback, and bounded self-repair; returns success or an evidence-backed terminal block."
version: "1.5.1"
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
/plan-decompose      — lightweight path (plan-eng-review autonomous → opposite-host gate → spec.md synthetic → tasks.md)
/spec-decompose      — lowest-level: requires spec.md + plan.md already present
/feature-implement   — executes tasks.md
```

## Invocation

```
/plan-decompose NNN                        # existing specs/NNN-* dir with or without plan.md
/plan-decompose "description"             # create new spec NNN (next available slot)
/plan-decompose NNN --from-issue GH#N     # pull context from GitHub issue
/plan-decompose NNN --no-review           # skip cross-host reviews (faster, less safe)
/plan-decompose NNN --no-codex            # deprecated alias for --no-review
/plan-decompose NNN --dry-run             # print pipeline plan, no writes
```

## Workflow

### Ownership and bounded repair contract

This skill is the single owner of lightweight planning quality. Callers such as
`/task-swarm` sequence its result; they do not reimplement review, scoring, or
repair loops.

- `PLAN_GATE_MAX_REPAIRS=${PLAN_GATE_MAX_REPAIRS:-2}`: maximum host-native plan
  rewrites after the initial opposite-host review.
- `TASK_GATE_MAX_REPAIRS=${TASK_GATE_MAX_REPAIRS:-2}`: maximum
  `spec-decompose` repair passes after the initial task score.
- Each repair consumes the prior findings as inert data, edits only the owned
  artifact, then reruns the same opposite-first gate with a fresh output file.
- A timeout, missing model, or transport failure gets one bounded read-only
  active-host fallback. Both hosts unavailable is a terminal evidence-backed
  block; a parseable quality rejection is repaired.
- Exhaustion writes `$SPEC_DIR/plan-decompose-blocked.md` and returns the
  terminal marker `PLAN-DECOMPOSE-BLOCKED spec=<NNN> stage=<plan|tasks>
  findings=<path> resume="/plan-decompose <NNN>"` with exit 1.

Operator grants never bypass this quality contract; likewise, quality rejection
never asks for an operator permission grant.

### Step 0: Resolve spec directory

```bash
ARGUMENTS="${ARGUMENTS:-}"
NO_REVIEW=0
FROM_ISSUE=""
DRY_RUN=0

read -ra _ARGS <<< "$ARGUMENTS"
for arg in "${_ARGS[@]}"; do
  case "$arg" in
    --no-review|--no-codex) NO_REVIEW=1 ;;
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
  echo "  review:       $([ $NO_REVIEW = 1 ] && echo disabled || echo opposite-host)"
  echo "  plan-eng-review: autonomous (spawned-session mode)"
  echo "  pipeline: plan.md → opposite-host review → spec.md (synthetic) → spec-decompose → tasks.md → opposite-host score gate"
  exit 0
fi
```

### Step 1: Recall prior decompositions (fail-soft)

**gbrain recall:** if `command -v gbrain` succeeds and
`env -u DATABASE_URL gbrain doctor` reports `[OK] connection`, run
`env -u DATABASE_URL gbrain query "<task topic>"` and feed prior decisions into
Step 2. Absent/unhealthy → fallback
`git log --oneline --grep="<topic>" | head -5`; never block (see
`docs/gbrain-optional.md`). GSD's own learnings live in `.planning/` history.
Recall with Claude `/gsd-mempalace-recall` or Codex `$gsd-mempalace-recall`
where enabled; headless calls go through `scripts/gsd/gsd-run.sh` and preserve
the invoking host.

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

### Step 3: Opposite-host adversarial review of plan.md

Skip entirely when `--no-review` (or deprecated `--no-codex`) is passed.

Use the shared host adapter; do not hard-code a vendor. The reviewer is always
the opposite family from the orchestrator: Claude host → Codex Sol/xhigh;
Codex host → Claude Opus. The adapter owns stdin closure and portable timeout
handling, including process-group cleanup on macOS:

```bash
. scripts/gsd/adversary-host.sh
HOST_KIND="$(detect_orchestrator_host)"
REVIEW_KIND="$(adversary_kind_for_host "$HOST_KIND")"
FALLBACK_KIND="$HOST_KIND"
if [ "$REVIEW_KIND" = codex ]; then
  REVIEW_MODEL="${PLAN_ADVERSARY_MODEL_CODEX:-gpt-5.6-sol}"
  REVIEW_EFFORT="${PLAN_ADVERSARY_EFFORT_CODEX:-xhigh}"
else
  REVIEW_MODEL="${PLAN_ADVERSARY_MODEL_CLAUDE:-opus}"
  REVIEW_EFFORT=""
fi
if [ "$FALLBACK_KIND" = codex ]; then
  FALLBACK_MODEL="${PLAN_ADVERSARY_MODEL_CODEX:-gpt-5.6-sol}"
  FALLBACK_EFFORT="${PLAN_ADVERSARY_EFFORT_CODEX:-xhigh}"
else
  FALLBACK_MODEL="${PLAN_ADVERSARY_MODEL_CLAUDE:-opus}"
  FALLBACK_EFFORT=""
fi
adversary_invoke_with_fallback "$REVIEW_KIND" "$FALLBACK_KIND" 540 \
  "$REVIEW_MODEL" "$REVIEW_EFFORT" "$FALLBACK_MODEL" "$FALLBACK_EFFORT" \
  "$PROMPT" >"$OUT_FILE" 2>&1
RC=$?   # bare exit code — NEVER pipe the live call (`| tail` masks the timeout kill)
```

- A preferred-host failure prints `DEGRADED` and makes one active-host attempt.
- Any nonzero `RC` after that bounded fallback writes
  `PLAN-DECOMPOSE-BLOCKED` with the exact resume command and stops the mandatory
  gate; unavailable review is never converted into an advisory PASS.
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
> recommended fix. End with exactly one anchored line:
> `VERDICT: APPROVE`, `VERDICT: APPROVE-WITH-FIXES`, or `VERDICT: REJECT`."

**Decision:**
- Parse exactly one anchored `VERDICT:` line after the bounded call exits.
  Missing, duplicate, or conflicting verdict lines are a failed mandatory
  review: consume one repair attempt, then write `PLAN-DECOMPOSE-BLOCKED` and
  exit 1 if the last allowed attempt is still unparseable. Never infer approval
  from process exit 0 or from prose mentioning a verdict.
- Verdict REJECT, or any CRITICAL finding: invoke `plan-eng-review` in
  host-native spawned-session mode with the current `plan.md` plus the findings
  as data, write the revised `plan.md`, and re-run the opposite-host review.
  Repeat up to `PLAN_GATE_MAX_REPAIRS`; do not return to `/task-swarm` between
  attempts.
- Verdict APPROVE-WITH-FIXES with HIGH findings: apply HIGH fixes to `plan.md`, continue.
- Verdict APPROVE: continue.

If the final allowed review still returns REJECT or CRITICAL, write the terminal
blocked artifact defined in the ownership contract and exit 1. Never emit a
generic “mandatory plan gate” message without the surviving findings.

Log finding counts: `[plan-decompose] opposite-host plan review: C:{critical} H:{high} M:{medium} verdict:{verdict}`

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

### Step 6: Opposite-host score gate on tasks.md

Skip entirely when `--no-review` (or deprecated `--no-codex`) is passed.

Run the same `adversary_invoke_with_fallback` contract from Step 3 on
`tasks.md` vs `spec.md`,
preserving producer≠reviewer across both Claude and Codex hosts:

Check:
- US coverage (every spec.md US has at least 1 task)
- Annotation completeness (`[model:]`, `[agent:]`, `[USn]` present)
- Dependency cycles (no circular depends_on)
- US consistency (task descriptions match their US)
- Task distribution (no phase has >60% of all tasks)

Score:
- `<5` → invoke `spec-decompose` again with the score findings as repair input,
  then rescore; repeat up to `TASK_GATE_MAX_REPAIRS`
- `5-6` → **WARN** — log warning, continue
- `≥7` → **PASS**

After the final task repair, a score `<5` writes the same terminal blocked
artifact with `stage=tasks` and exits 1. The artifact includes every failed
check, score history, artifact paths, attempts consumed, and the exact resume
command. `/task-swarm` receives one terminal result—not intermediate failures.

The opposite host is preferred and the active host is the one bounded fallback.
If both are unavailable, or the score response is missing/duplicate/conflicting,
write the terminal blocked artifact with `stage=tasks` and exit 1. This score
gate is mandatory; reviewer absence is never converted into a warning or PASS.

### Step 7: Store the decision (fail-soft)

If gbrain is healthy: `env -u DATABASE_URL gbrain put spec/<NNN>-decompose
"<one-line outcome: verdict, score, plan path>"` then
`env -u DATABASE_URL gbrain sync --no-pull --no-embed`. GSD captures its own
phase learnings via Claude `/gsd-extract-learnings`, Codex
`$gsd-extract-learnings`, or the host-preserving headless runner. Skip silently when absent —
storage is enhancement, not gate.

### Step 8: Report

```
┌─ plan-decompose ${SPEC_ID} complete ────────────────────────┐
│ plan.md:       ${SPEC_DIR}/plan.md                          │
│ spec.md:       ${SPEC_DIR}/spec.md (synthetic|existing)     │
│ tasks.md:      ${SPEC_DIR}/tasks.md                         │
│ cross-host plan: N findings (C:0 H:1 M:2) verdict:APPROVE  │
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
