---
name: fix
description: "Investigate a bug, fix it with ruflo agents (mandatory in v1.2.0), verify with qa-only then full qa, then run review-gate cross-model adversarial review. Ralph loop until green. Non-interactive — aborts with structured artifacts on uncertainty or CRITICAL findings. v2.0.0 integrates Claude native /goal for continuation; adversarial audit + /review-gate remain mandatory."
version: "2.0.0"
allowed-tools:
  - Read
  - Edit
  - Write
  - Bash
  - Glob
  - Grep
  - Agent
  - Skill
---

# /fix — Investigate, fix, and verify a bug with Ralph loop

One command. Takes a bug description or symptom, traces root cause, fixes it with ruflo agent swarm, verifies the fix, then verifies no regressions. Loops until green or max retries.

## Native /goal entry (recommended)

Before invoking `/fix`, set a native `/goal` condition so Claude auto-continues until the bug is verified fixed. Example:

```
/goal "bug fixed: latest run-state audit --kind fix verdict=pass on this issue AND /qa green AND /review-gate PASS"
```

Native `/goal` (Anthropic-maintained, Claude Code 2.1.139+):
- Small/fast model checks the condition after every turn
- Auto-continues if false; clears when met
- One goal per session; `/goal clear` cancels
- See https://code.claude.com/docs/en/goal

`/fix` writes the adversarial audit verdict to `~/.claude/state/audits.jsonl`. Your `/goal` condition can grep that file for the verdict + check `npx vitest run` exit code.

If you skip `/goal`, the pipeline runs but you advance it manually.

## When to invoke

- User reports a bug: "auth redirect is broken", "the form doesn't submit", "500 on /api/foo"
- Claude discovers a bug during QA or code review
- Production monitoring surfaces an issue
- After `/qa` or `/qa-only` reports failures that need fixing

## Flags

| Flag | Effect |
|---|---|
| `--auto-fix` | Forwarded to ruflo agents and review-gate auto-fix loop. |
| `--interactive` | Restore manual gates between phases. Default = non-interactive. |
| `--no-audit` | **Operator-accepted-risk bypass** of the adversarial completion audit. Use only for trivial typo-class fixes. Not a "skip mandatory step" — the operator owns the risk. |
| `--no-codex-gate` | **Operator-accepted-risk bypass** of cross-model adversarial review. Use only when blast radius is provably minimal (typo, comment, log-message). |

## Invocation

```
/fix "description of the bug or symptom"
/fix "description" --plan          # use /plan-eng-review for complex bugs (heavier)
/fix "description" --no-qa         # skip full /qa, only run /qa-only on affected area
/fix "description" --no-codex-gate # skip cross-model adversarial review (default-on, see Step 5.5)
/fix "description" --dry-run       # investigate + plan but don't apply fix
/fix "description" --scope=file1.ts,file2.ts   # manually scope-lock to specific files
```

## Step 0: Parse arguments and set up

```bash
SPEC_ARG="${ARGUMENTS:-}"
DRY_RUN=0
USE_PLAN_REVIEW=0
SKIP_FULL_QA=0
NO_CODEX_GATE=0
SCOPE_FILES=""
BUG_DESC=""
RALPH_MAX_RETRIES="${RALPH_MAX_RETRIES:-3}"

read -ra _ARGS <<< "$SPEC_ARG"
for arg in "${_ARGS[@]}"; do
  case "$arg" in
    --dry-run)        DRY_RUN=1 ;;
    --plan)           USE_PLAN_REVIEW=1 ;;
    --no-qa)          SKIP_FULL_QA=1 ;;
    --no-codex-gate)  NO_CODEX_GATE=1 ;;
    --scope=*)        SCOPE_FILES="${arg#--scope=}" ;;
    *)                BUG_DESC="$BUG_DESC $arg" ;;
  esac
done

BUG_DESC=$(echo "$BUG_DESC" | sed 's/^ //')
[ -z "$BUG_DESC" ] && { echo "ERROR: no bug description. Usage: /fix \"description of the bug\""; exit 1; }

RALPH_DIR=".ralph/fix-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RALPH_DIR"
LOG_FILE="$RALPH_DIR/fix-log.jsonl"
START_TIME=$(date +%s)
```

Print banner:
```
[FIX] Ralph fix loop starting
  Bug: {BUG_DESC}
  Max retries: {RALPH_MAX_RETRIES}
  Artifacts: {RALPH_DIR}/
```

## Step 0.5: Pattern memory lookup (before investigating)

Search for prior fixes to similar bugs. This is the learning system — each successful `/fix` stores its pattern (Step 6b), so future similar bugs start with context instead of from scratch.

**Search ruflo memory:**
```
mcp__ruflo__memory_search({ query: "{BUG_DESC}", topK: 5 })
```

**Search gstack learnings:**
```bash
gstack-learnings-search --query "{BUG_DESC}" --type pitfall,pattern --limit 5 2>/dev/null || true
```

**If matches found (confidence >= 7):**
```
[FIX] Prior fix patterns found:
  1. [pattern] {key} (confidence {N}/10, {date}) — {insight}
  2. [pitfall] {key} (confidence {N}/10, {date}) — {insight}
  Feeding to investigate as prior context.
```

Write matches to `$RALPH_DIR/prior-patterns.md`. Pass this file to `/investigate` in Step 1 so the investigation starts with known patterns, not from zero.

**If no matches:** Continue silently — this is the first occurrence of this bug type.

## Step 1: Investigate (5 Whys root cause analysis)

Invoke `/investigate` via the Skill tool with the bug description as the argument. If prior patterns were found in Step 0.5, append them to the argument: "Bug: {BUG_DESC}. Prior patterns found — see {RALPH_DIR}/prior-patterns.md"

If `--scope` was provided, prefix the investigate argument with the scope: "Scope-lock to {SCOPE_FILES}. Bug: {BUG_DESC}"

**What investigate produces:**
- Root cause identification (the actual broken code, not symptoms)
- Affected files list (scope-lock for the fix)
- Severity assessment
- Recommended fix approach

Capture the investigation output. Write a structured summary to `$RALPH_DIR/investigation.md`:

```markdown
# Investigation Report
Bug: {BUG_DESC}
Date: {ISO timestamp}

## Root Cause
{root cause from investigate, with file:line references}

## Affected Files
{list of files that need to change}

## Severity
{CRITICAL / HIGH / MEDIUM / LOW}

## Recommended Fix
{1-3 sentences describing the minimal fix}
```

**Non-interactive failure handling (v1.2.0):** If investigate cannot determine root cause after 2 attempts, do NOT prompt the user. Write the partial investigation findings to `$RALPH_DIR/investigation-incomplete.md` with this structure:

```markdown
# Investigation INCOMPLETE
Bug: {BUG_DESC}
Date: {ISO timestamp}
Attempts: 2

## What was explored
{summary of files/symbols/log-lines investigated}

## What's unclear
{1-3 bullets — the specific gaps that blocked root-cause identification}

## Suggested narrow scope
{recommend a `--scope=` flag value for the rerun, e.g. --scope=auth/middleware.ts,auth/session.ts}

## To resume
/fix "{BUG_DESC}" --scope={suggested scope}
```

Then print structured terminal error and exit 1:
```
[FIX] ERROR: investigation could not determine root cause after 2 attempts
  Bug:        {BUG_DESC}
  Artifacts:  {RALPH_DIR}/investigation-incomplete.md
  Resume:     /fix "{BUG_DESC}" --scope={suggested scope}
```

The user reads the artifact, narrows the scope, reruns. No human prompt during the run.

## Step 2: Plan the fix

**Default (inline plan):** Based on the investigation report, generate a minimal fix plan inline:

```markdown
## Fix Plan
Complexity: {trivial / moderate / complex}

### Changes
1. {file1.ts:line} — {what to change and why}
2. {file2.ts:line} — {what to change and why}

### Regression Test
- {test file path} — {what the test asserts}
```

Write the plan to `$RALPH_DIR/fix-plan.md`.

**With `--plan` flag:** For complex bugs (5+ files, architectural implications), invoke `/plan-eng-review` via Skill tool. The eng review catches issues like "the fix introduces a new coupling" or "this needs a migration."

**Dry-run exit:** If `--dry-run`, print the investigation + plan and stop:
```
[FIX] DRY RUN — investigation + plan only
  Bug: {BUG_DESC}
  Root cause: {root cause}
  Affected files: {file list}
  Fix plan: {RALPH_DIR}/fix-plan.md
  To apply: /fix "{BUG_DESC}" (without --dry-run)
```

## Step 3: Spawn fix agent(s) via ruflo swarm

**Ruflo is mandatory (v1.2.0).** No silent native fallback.

Pre-flight check:
```bash
RUFLO_REQUIRED="${RUFLO_REQUIRED:-1}"
EXECUTOR=$(bash scripts/harness/executor-detect.sh 2>/dev/null || echo "")

if [ -z "$EXECUTOR" ] || [ "$EXECUTOR" = "native" ]; then
  if [ "$RUFLO_REQUIRED" = "1" ]; then
    cat >&2 <<EOF
[FIX] ERROR: ruflo MCP unavailable and RUFLO_REQUIRED=1 (default).
  Detector: scripts/harness/executor-detect.sh
  Detected: ${EXECUTOR:-(detection failed)}
Resolve:
  - Verify mcp__ruflo__* tools are reachable in this session
  - Re-run: npx claude-flow@v3alpha hooks pretrain
  - To bypass for debugging: RUFLO_REQUIRED=0 /fix "{BUG_DESC}"
EOF
    exit 1
  else
    echo "[FIX] WARNING: RUFLO_REQUIRED=0 — falling back to native Agent (debug mode)" >&2
    EXECUTOR="native"
  fi
fi
```

**Complexity routing:**
- Trivial (1 file, <10 lines change): single haiku agent
- Moderate (2-5 files): single sonnet agent
- Complex (5+ files): up to 3 sonnet agents via ruflo swarm, each owning a non-overlapping file subset

**Fix agent prompt:**

```
You are fixing a bug. You have no prior context.

## Bug
{BUG_DESC}

## Root Cause (from investigation)
{content of $RALPH_DIR/investigation.md}

## Your Fix Plan
{content of $RALPH_DIR/fix-plan.md, filtered to this agent's files}

## Context
Read the affected files listed in the fix plan, then read CLAUDE.md for project conventions.

## Your job
1. Write a regression test FIRST that reproduces the bug (RED)
2. Apply the minimal fix described in the plan (GREEN)
3. Verify the regression test now passes
4. Report SUCCESS with: files changed, test file path, one-line summary
   Or FAILURE with: what you tried, what blocked you

## Absolute rules
- Do NOT modify files outside your fix plan
- Do NOT commit or push
- Do NOT refactor or clean up unrelated code
- Minimal change — fix the bug, nothing more
```

**Model selection:**
- Trivial bugs: `model: "haiku"` (fast, cheap)
- Moderate bugs: `model: "sonnet"` (good judgment)
- Complex bugs or security-related: `model: "sonnet"` with all agents

After agent(s) complete, write results to `$RALPH_DIR/fix-results.md`.

## Step 3.5: Post-fix specialist review + codex adversarial

Before running the QA suite, get a quick review of the fix itself. This catches fixes that introduce new problems (trading one bug for another).

**Specialist review (parallel, via Agent tool):**

Determine which specialists to spawn based on files the fix touched:
- Fix touches `auth/`, `login`, `session`, `token`, `password` → spawn **security** specialist
- Fix touches `api/`, `route`, `endpoint` → spawn **api-contract** specialist
- Fix touches `migration`, `schema`, `model` → spawn **data-migration** specialist
- Fix touches any file → always spawn **testing** specialist (verify the regression test is sufficient)

Read the specialist checklist from `review/specialists/{name}.md` and spawn each as a subagent with the fix diff:

```
Agent: "You are a {specialist} reviewer. Run `git diff HEAD~1` to see the fix.
Apply the checklist below against the diff. Report findings as:
{severity} {file:line} {description}
If no findings: NO FINDINGS.
CHECKLIST: {checklist content}"
```

Spawn all selected specialists in parallel (single message, multiple Agent calls).

**Codex adversarial (if available):**

```bash
which codex 2>/dev/null && CODEX_AVAILABLE=1 || CODEX_AVAILABLE=0
```

If codex is available, run a quick adversarial review:
```bash
codex exec "Review the most recent git diff (run git diff HEAD~1). This is a bug fix.
Think like an attacker: does this fix introduce new edge cases, race conditions,
or failure modes? Does it handle all the paths the original bug affected?
5 most important concerns only." -C "$REPO_ROOT" -s read-only 2>/dev/null | tail -50
```

**Handling findings (v1.2.0 — non-interactive):**
- **CRITICAL from any specialist or codex** → write all CRITICAL findings to `$RALPH_DIR/critical-findings.md` (structure below) and exit 1. Do NOT prompt the user. Do NOT proceed to QA.
- **HIGH** → log as concern in `fix-results.md`, continue (qa-only will catch regressions).
- **No findings** → continue silently.

`critical-findings.md` structure:

```markdown
# CRITICAL findings — fix aborted before QA
Bug: {BUG_DESC}
Date: {ISO timestamp}
Files changed by fix: {list}

## Findings
| Severity | Source | File:line | Description |
|---|---|---|---|
| CRITICAL | {specialist|codex} | {path}:{line} | {one-line description} |
| ... | ... | ... | ... |

## Why this aborted
The fix introduced new CRITICAL-severity issues. v1.2.0 policy: never auto-proceed
through CRITICAL findings. The user must review each finding and decide:
  (a) Roll back the fix (`git restore .`) and rerun /fix with adjusted scope.
  (b) Override and proceed manually (skip /fix; run /qa-only directly).
  (c) Apply a follow-up patch addressing each CRITICAL finding, then run /qa.

## To resume
After the user resolves the findings:
  /fix "{BUG_DESC}" --scope={files known good}    # if scope can be narrowed
  OR run /qa-only manually if proceeding without re-running /fix
```

Print structured terminal error and exit 1:
```
[FIX] ERROR: CRITICAL findings detected — fix aborted before QA
  Findings:   {N} CRITICAL across {sources}
  Artifacts:  {RALPH_DIR}/critical-findings.md
  Files:      {files changed by fix}
Action required: review {RALPH_DIR}/critical-findings.md, then choose to roll back, override, or patch.
```

## Step 4: /qa-only on affected area

Invoke `/qa-only` via Skill tool, mentioning the files modified by the fix agent(s) so QA scopes to the affected area.

**Dimensions that run:**
- Unit tests (vitest/pytest on changed files) — always
- qa-review (code review on the fix diff) — always
- qa-security — if the bug involved auth, user input, data handling, or fix touches security-sensitive code
- qa-e2e — if dev server is running AND the bug is user-facing

**On pass:** Continue to Step 5 (or Step 6 if `--no-qa`).

**On fail:** Enter retry loop (Step 4b).

### Step 4b: Retry loop

1. Capture which QA dimensions failed and the failure output
2. Write failure context to `$RALPH_DIR/retry-{N}/`
3. Spawn a new fix agent with ADDITIONAL context:
   - Original investigation report
   - Prior fix attempt (what was changed)
   - QA failure output (why it's still broken)
   - Instruction: "Your prior fix attempt didn't fully resolve the issue. Here's what QA found..."
4. Re-run /qa-only on failed dimensions only
5. Loop up to RALPH_MAX_RETRIES

**On retry exhaustion:**
```
[FIX] Fix verification failed after {MAX} retries
  Bug: {BUG_DESC}
  Root cause: {root cause}
  Fix attempted: {files changed}
  Still failing: {QA dimensions}
  Artifacts: {RALPH_DIR}/
  Next: /fix "{BUG_DESC}" --plan  (try with eng review)
        or inspect {RALPH_DIR}/ manually
```

## Step 5: Full /qa (regression check)

Skip if `--no-qa` flag was set.

Invoke `/qa` via Skill tool. Full browser-based QA suite to verify the fix didn't break anything else.

**On pass:** Continue to Step 5.5.

**On fail:** Determine if the regression is related to the fix:
- **Related** (file overlap with fix diff): feed back into Step 4b as additional context
- **Unrelated** (different files, pre-existing): log as informational, continue to Step 5.5 with a note

## Step 5.5: /review-gate (cross-model adversarial review)

Skip if `--no-codex-gate` flag was set.

**Why this step exists:** Step 3.5 already runs codex *adversarial* on the fresh fix. Step 5.5 runs the full `/review-gate` skill (3 passes — review + adversarial + test-coverage gap analysis) against the *final post-QA diff* including any retry-loop changes. Single-model review has blind spots; cross-model adversarial review catches CRITICAL issues that pass Claude-only quality gates. Cost: ~$2 + ~13 min — cheap insurance for any fix landing in production blast radius (auth, payments, RLS, multi-tenant, cron).
The underlying Claude invocation must use the local CLI auth path, not `--bare`; `--bare` disables keychain/OAuth reads and produces a false "Not logged in" error on valid logged-in machines.
If `/review-gate` hangs or times out, emit a structured blocked gate with a timeout reason. Do not narrate the failure in first person or say you "attempted" the adversarial review step.

**Pre-flight:**
```bash
which codex >/dev/null 2>&1 && CODEX_AVAILABLE=1 || CODEX_AVAILABLE=0

if [ "$CODEX_AVAILABLE" = "0" ]; then
  echo "[FIX] WARNING: reviewer CLI not found — skipping review-gate" >&2
  echo "  Install: npm install -g @openai/codex && codex login" >&2
  printf '{"timestamp":"%s","bug":"%s","step":"review-gate","status":"skipped","reason":"review_cli_not_installed","duration_s":0}\n' \
    "$(date -u +%FT%TZ)" "$BUG_DESC" >> "$LOG_FILE"
  # Continue to Step 6.
fi
```

**Invoke:** Call the `review-gate` skill via the Skill tool. Review-gate runs 3 passes against the staged diff, produces a findings report.

**Findings policy:**
- **CRITICAL** → STOP. Write findings to `$RALPH_DIR/codex-critical-findings.md`, exit 1. Do NOT prompt the user. Structured artifact directs the user to roll back, override, or patch (mirror Step 3.5 abort path).
- **HIGH** → log as concern in `fix-results.md`, continue to Step 6 (user reviews report before merging PR).
- **MEDIUM/LOW or no findings** → continue silently.

**Hook side-effect:** `scripts/hooks/codex-gate-warn.sh` (if installed in user env) records the gate run timestamp keyed to current branch, so subsequent `gh pr merge` does not warn about a missing recent review-gate run.

`codex-critical-findings.md` structure:

```markdown
# CRITICAL findings — fix aborted at review-gate (Step 5.5)
Bug: {BUG_DESC}
Date: {ISO timestamp}
Files changed by fix (final): {list}
QA status before gate: PASS

## Findings
| Severity | Pass | File:line | Description |
|---|---|---|---|
| CRITICAL | review|adversarial|coverage | {path}:{line} | {one-line description} |
| ... | ... | ... | ... |

## Why this aborted
The fix passed all Claude-side QA but codex (cross-model) found CRITICAL issues.
v1.3.0 policy: never auto-proceed through review-gate CRITICAL findings.
The user must review each finding and decide:
  (a) Apply a follow-up patch addressing each CRITICAL finding, then rerun /fix.
  (b) Roll back the fix (`git restore .`) and rerun /fix with adjusted scope.
  (c) Override and proceed manually (skip gate via --no-codex-gate; risk acknowledged).

## To resume
After the user resolves the findings:
  /fix "{BUG_DESC}"                              # rerun full pipeline including review-gate
  /fix "{BUG_DESC}" --no-codex-gate              # skip gate (NOT recommended)
```

Print structured terminal error and exit 1:
```
[FIX] ERROR: review-gate CRITICAL findings — fix aborted before report
  Findings:   {N} CRITICAL across {passes}
  Artifacts:  {RALPH_DIR}/codex-critical-findings.md
  Bypass:     /fix "{BUG_DESC}" --no-codex-gate (NOT recommended)
Action required: review {RALPH_DIR}/codex-critical-findings.md, then patch/roll-back/override.
```

Log:
```json
{"timestamp":"<ISO>","bug":"<desc>","step":"review-gate","status":"pass|failed|skipped","findings_critical":<n>,"findings_high":<n>,"report":"<path>","duration_s":<n>}
```

### Step 4 — Adversarial completion audit (mandatory unless `--no-audit`)

Before declaring the fix complete, run a hostile cross-model audit that tries to prove the bug still exists. Codex GPT-5 runs in read-only sandbox; verdict is appended to `~/.claude/state/audits.jsonl` and drives the next-turn decision via native `/goal` (see "Native /goal entry" above).

`run-state` is now invoked only to create an audit record — there is no prior lifecycle to extend.

```bash
# Create an ad-hoc audit record (no lifecycle pretense)
RUN_ID=$(~/.claude/bin/run-state start \
  --skill fix \
  --objective "$BUG_DESC" \
  | jq -r .run_id)

MODIFIED=$(git diff "origin/${BASE:-main}"...HEAD --name-only | head -30)

~/.claude/bin/run-state audit "$RUN_ID" \
  --kind fix \
  --context "BUG_DESCRIPTION=$BUG_DESC" \
  --context "MODIFIED_FILES=$MODIFIED" \
  --cwd "$(git rev-parse --show-toplevel)"
```

Decision rule (verdict appended to `~/.claude/state/audits.jsonl`, keyed by `$RUN_ID`):
- `verdict=pass` → proceed to `/ship` or stop. If a native `/goal` is set, it clears.
- `verdict=fail` → re-enter investigation with the auditor's repro as the new symptom. Native `/goal`, if set, auto-continues the loop.
- `verdict=error` → investigate why audit failed (timeout, codex auth, malformed output). Retry once. If still error, escalate manually.

Hard cap: 3 audit attempts per bug. Grep the audit log to check:

```bash
ATTEMPTS=$(grep -c "\"run_id\":\"$RUN_ID\"" ~/.claude/state/audits.jsonl 2>/dev/null || echo 0)
if [ "$ATTEMPTS" -ge 3 ]; then
  echo "FIX FAILED — 3 audit attempts exhausted. Run-id: $RUN_ID"
  exit 1
fi
```

## Step 6: Report

```
╔═══════════════════════════════════════════════════════════════╗
║ /fix complete                                                  ║
╠═══════════════════════════════════════════════════════════════╣
║ Bug:           {BUG_DESC}                                     ║
║ Root cause:    {one-line root cause}                          ║
║ Files changed: {N} ({file list})                              ║
║ Tests added:   {N} ({test file list})                         ║
║ QA-only:       PASS ({dimensions that ran})                   ║
║ Full QA:       PASS / SKIPPED (--no-qa)                       ║
║ Review-gate:   PASS / SKIPPED (--no-codex-gate / no reviewer) ║
║ Retries used:  {N}/{MAX}                                      ║
║ Artifacts:     {RALPH_DIR}/                                   ║
║ Duration:      {HH:MM:SS}                                    ║
╚═══════════════════════════════════════════════════════════════╝

Next: /ship to create PR with the fix
```

## Step 6b: Store fix pattern (learning system)

After a successful fix, store the pattern so future similar bugs are resolved faster. This is the feedback loop that makes the system smarter over time.

**Store to ruflo memory (semantic vector search):**
```
mcp__ruflo__memory_store({
  content: "Fixed {bug_category} in {primary_file}: {root_cause_summary}. Fix: {fix_summary}",
  metadata: {
    type: "fix-pattern",
    files: ["{changed_files}"],
    bug_category: "{category}",
    root_cause: "{root_cause}",
    retries: {N},
    timestamp: "{ISO}"
  }
})
```

**Store to ruflo agentdb (pattern search with confidence):**
```
mcp__ruflo__agentdb_pattern-store({
  pattern: "bug:{bug_category}:{primary_file_area}",
  content: "Root cause: {root_cause}. Fix: {fix_approach}. Regression test: {test_file}.",
  confidence: {8-10 based on retries: 0 retries=10, 1=9, 2=8, 3=7}
})
```

**Store to gstack learnings (cross-session, cross-project):**
```bash
gstack-learnings-log '{
  "skill": "fix",
  "type": "pattern",
  "key": "{bug_category}-{primary_file_slug}",
  "insight": "Bug: {BUG_DESC}. Root cause: {root_cause}. Fix: {fix_summary}. Test: {test_file}.",
  "confidence": {confidence_score},
  "source": "observed",
  "files": ["{changed_files}"]
}'
```

**Train ruflo autopilot (route prediction):**
```
mcp__ruflo__autopilot_learn({
  task_type: "fix",
  input: "{bug_category}",
  outcome: "success",
  model_used: "{model}",
  duration_s: {duration}
})
```

This step is automatic and non-blocking. Failures in any storage call are logged but don't affect the fix outcome.

**What this enables:** Next time someone hits a similar bug, Step 0.5 finds the pattern:
```
[FIX] Prior fix patterns found:
  1. [pattern] auth-redirect-middleware (confidence 9/10, 2026-04-17)
     Root cause: session token not refreshed after OAuth callback.
     Fix: add token refresh in auth/callback.ts:42.
```

The investigate step starts with this context instead of from scratch. Over time, common bug patterns accumulate and fix times drop.

## Step 6c: Before/after evidence (UI bugs)

For user-facing bugs, capture visual proof the fix worked.

**Detection:** If the bug description mentions UI terms (page, button, form, modal, screen, layout, render, display, visible, click, hover, scroll) OR if any changed file matches `*.tsx`, `*.jsx`, `components/`, `app/**/page.*`:

**Before screenshot** (captured in Step 1, before fix):
If a dev server is running (`curl -sf localhost:3000`), use the browse tool to screenshot the broken state. Save to `$RALPH_DIR/before.png`.

**After screenshot** (captured here, after fix verified):
Use `$B` to screenshot the same page/component. Save to `$RALPH_DIR/after.png`.

**Include in report:**
```
[FIX] Visual evidence:
  Before: {RALPH_DIR}/before.png
  After:  {RALPH_DIR}/after.png
```

Skip silently if no dev server or not a UI bug.

Append to fix log:
```json
{"timestamp":"<ISO>","bug":"<desc>","root_cause":"<cause>","files_changed":["<paths>"],"tests_added":["<paths>"],"retries":<N>,"qa_only":"pass|fail","full_qa":"pass|fail|skip","duration_s":<N>}
```

## Integration with existing skills

| Skill | How /fix uses it |
|-------|-----------------|
| `/investigate` | Step 1 — root cause analysis (always) |
| `/plan-eng-review` | Step 2 — architectural fix review (with --plan) |
| `/qa-only` | Step 4 — focused verification of fix |
| `/qa` | Step 5 — full regression check |
| `/review-gate` | Step 5.5 — cross-model adversarial review (default-on; skip with --no-codex-gate or absent reviewer CLI) |
| `scripts/harness/executor-detect.sh` | Step 3 — ruflo vs native decision |
| `scripts/ralph-retry.sh` | Step 4b — retry loop orchestration |

## Cost estimation

| Bug complexity | Typical cost |
|---------------|-------------|
| Trivial (1 file, <10 lines) | ~$0.10 (1 haiku agent + qa-only) — add ~$2 if review-gate runs |
| Moderate (2-5 files) | ~$0.50 (1 sonnet agent + qa-only + qa) — add ~$2 if review-gate runs |
| Complex (5+ files, --plan) | ~$2.00 (eng review + 2-3 sonnet agents + qa) — add ~$2 if review-gate runs |

Review-gate adds ~$2 + ~13 min per fix. Disable with `--no-codex-gate` for trivial fixes where blast radius is minimal (typo, log-message tweak, comment fix).

## Safety rules

- Never modify files outside the investigation's scope-lock (unless retry expands scope)
- Never commit or push — use `/ship` after /fix
- Never skip the regression test — every fix must have a test that would have caught the bug
- On retry exhaustion, STOP and escalate to user (via structured artifact, not AskUserQuestion)
- **v1.2.0 non-interactive policy:** never block on AskUserQuestion. Uncertainty (incomplete investigation, CRITICAL findings) writes a structured artifact and exits 1.
- **v1.2.0 ruflo policy:** ruflo is mandatory. Set `RUFLO_REQUIRED=0` only for debugging.
- **v1.3.0 codex-gate policy:** review-gate runs by default before Step 6 (report) on the final post-QA diff. Skip with `--no-codex-gate` only when blast radius is provably minimal (typo, comment, log-message). Skip-gracefully if the opposite-harness CLI is not installed (warn, continue).
- **v2.0.0 continuation policy:** continuation is delegated to native Claude `/goal`. `/fix` no longer manages a run-state lifecycle (no start/update/complete, no Stop-hook continuation, no token budget, no marker file). The adversarial audit + review-gate are still mandatory bypasses-via-flag; `/goal` is what loops the pipeline until verdict=pass.

## Relationship to QA Ralph Loop

The QA Ralph Loop (`/feature-implement --qa-loop`) catches bugs DURING implementation.
`/fix` catches bugs AFTER — reported by users, found in QA, or surfaced in production.

Same Ralph pattern, different trigger:
- Ralph Loop: phase gate fails → investigate → fix → re-qa → loop
- /fix: bug reported → investigate → fix → qa-only → full qa → loop

Both use `scripts/ralph-retry.sh` for retries. Both store artifacts in `.ralph/`.
Both respect `RALPH_MAX_RETRIES`. Same muscle, different entry point.
