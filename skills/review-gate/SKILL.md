---
name: review-gate
description: "Host-neutral pre-merge review gate. Runs the opposite CLI from the active harness so Codex reviews with Claude and Claude reviews with Codex. 3-pass: general quality → adversarial → test-coverage gap. Blocks shipping on HIGH/CRITICAL findings."
version: "1.0.0"
---

# /review-gate

Cross-model 3-pass review of the current git diff. Uses the opposite CLI from the
active harness so the reviewer is always an independent model family.

> **Harness rule:** if you are already in Codex, `review-gate` runs with `claude`.
> If you are already in Claude, `review-gate` runs with `codex`.

## When to run

- End of every implementation phase
- Before `/ship` on high-blast-radius PRs (multi-tenant, infra, auth, payments, RLS, cron)
- Any time you want a second opinion from a different model family

Cost: ~$2 · Time: ~13 min · Returns: structured findings list with severity

## Pipeline

```
┌─────────────────────────────────────────────────────────────────┐
│  /review-gate                                                   │
│                                                                 │
│  Pre-check                                                      │
│    └─ choose opposite CLI for the active harness               │
│                                                                 │
│  Pass 1 — General quality                                       │
│    └─ bugs, naming, error handling, style, docs gaps            │
│                                                                 │
│  Pass 2 — Adversarial                                           │
│    └─ security, edge cases, injection, auth bypass, race conds  │
│                                                                 │
│  Pass 3 — Test-coverage gap                                     │
│    └─ uncovered branches, missing unit/integration/E2E tests    │
│                                                                 │
│  Merge → deduplicate → rank by severity                         │
│  EXIT 0 if 0 CRITICAL + 0 HIGH; EXIT 1 otherwise               │
└─────────────────────────────────────────────────────────────────┘
```

## Usage

```
/review-gate             # review staged diff (default)
/review-gate --all       # review all changes vs main branch
/review-gate --file path # review a single file
/review-gate --dry-run   # print what would be reviewed, no API calls
```

## Implementation

### Pre-check — choose the opposite CLI

```bash
ACTIVE_HARNESS="claude"
if [ -n "${CODEX_SESSION_ID:-}" ] || [ -n "${CODEX_HOME:-}" ] || [ -n "${CODEX_AGENT:-}" ]; then
  ACTIVE_HARNESS="codex"
elif [ -n "${CLAUDE_SESSION_ID:-}" ] || [ -n "${CLAUDE_CODE:-}" ]; then
  ACTIVE_HARNESS="claude"
fi

if [ "$ACTIVE_HARNESS" = "codex" ]; then
  REVIEW_BIN="${CLAUDE_BIN:-claude}"
else
  REVIEW_BIN="${CODEX_BIN:-codex}"
fi

if ! command -v "$REVIEW_BIN" >/dev/null 2>&1; then
  echo "ERROR: $REVIEW_BIN CLI not found."
  if [ "$REVIEW_BIN" = "claude" ]; then
    echo "Install: https://claude.ai/code"
  else
    echo "Install: npm install -g @openai/codex"
  fi
  echo "Then re-run: /review-gate"
  exit 1
fi

if [ "$REVIEW_BIN" = "claude" ]; then
  if ! command -v codex >/dev/null 2>&1; then
    echo "[review-gate] WARN: codex CLI not found; review will run with Claude only."
  fi
else
  if ! command -v claude >/dev/null 2>&1; then
    echo "[review-gate] WARN: claude CLI not found; review will run with Codex only."
  fi
fi
```

### Capture diff

```bash
ARGS="${ARGUMENTS:-}"
DRY_RUN=0
DIFF_TARGET="--staged"

for arg in $ARGS; do
  case "$arg" in
    --all)        DIFF_TARGET="main" ;;
    --dry-run)    DRY_RUN=1 ;;
    --file*)      DIFF_TARGET="$arg" ;;
  esac
done

if [ "$DIFF_TARGET" = "--staged" ]; then
  DIFF=$(git diff --staged 2>/dev/null)
  [ -z "$DIFF" ] && DIFF=$(git diff HEAD 2>/dev/null)
else
  DIFF=$(git diff "$DIFF_TARGET"...HEAD 2>/dev/null)
fi

if [ -z "$DIFF" ]; then
  echo "review-gate: no diff found. Nothing to review."
  exit 0
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "[dry-run] Would review $(echo "$DIFF" | wc -l) lines of diff across 3 passes"
  exit 0
fi
```

### Pass 1 — General quality

Run a sub-agent with the diff and this prompt:

```
You are a senior code reviewer. Review this git diff for:
- Logic bugs and correctness issues
- Naming clarity (variables, functions, types)
- Error handling gaps (unhandled exceptions, missing validation)
- Code style and consistency with surrounding code
- Documentation gaps (missing/wrong comments, confusing public API)

For each finding output:
  SEVERITY: [CRITICAL|HIGH|MEDIUM|LOW]
  FILE: <path>
  LINE: <approximate line number>
  ISSUE: <one sentence>
  FIX: <one sentence>

Do not praise. Do not summarize. Findings only.
```

### Pass 2 — Adversarial

Run the opposite CLI as the independent adversarial reviewer.

```bash
if [ "$REVIEW_BIN" = "claude" ]; then
  echo "$DIFF" | env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u CLAUDE_API_KEY timeout 480 "$REVIEW_BIN" -p \
    --system "You are an adversarial security and correctness reviewer. Find: SQL injection, XSS, auth bypass, race conditions, insecure defaults, privilege escalation, secret exposure, SSRF, path traversal, OWASP Top 10. Format each finding as SEVERITY/FILE/LINE/ISSUE/FIX. Findings only." \
    2>/dev/null || echo "[review-gate pass skipped — API error]"
else
  echo "$DIFF" | timeout 480 "$REVIEW_BIN" review \
    --system "You are an adversarial security and correctness reviewer. Find: SQL injection, XSS, auth bypass, race conditions, insecure defaults, privilege escalation, secret exposure, SSRF, path traversal, OWASP Top 10. Format each finding as SEVERITY/FILE/LINE/ISSUE/FIX. Findings only." \
    --model gpt-5.4 \
    2>/dev/null || echo "[review-gate pass skipped — API error]"
fi
```

### Pass 3 — Test-coverage gap

Run a sub-agent with the diff:

```
You are a test coverage analyst. Review this diff for testing gaps:
- New functions/methods with no corresponding test
- Branches (if/else, switch, try/catch) not covered by existing tests
- Missing integration tests for new API endpoints
- Missing E2E tests for new user-facing flows
- Edge cases not tested (nulls, empty inputs, max values, concurrency)

For each gap output:
  SEVERITY: [HIGH|MEDIUM|LOW]  (no CRITICAL for coverage — only for missing security tests)
  FILE: <source file with gap>
  TEST_FILE: <where the test should go>
  GAP: <what is not tested>
  TEST: <one-line description of the test to write>

Do not praise. Gaps only.
```

### Merge and rank

Collect all findings from passes 1-3. Deduplicate by (file, line, issue-text similarity). Rank:

1. CRITICAL (any pass)
2. HIGH (any pass)
3. MEDIUM
4. LOW

### Output and exit

Print findings in severity order:

```
╔══════════════════════════════════════════════════════════════╗
║ review-gate — Phase review                                   ║
╠══════════════════════════════════════════════════════════════╣
║ CRITICAL: N  HIGH: N  MEDIUM: N  LOW: N                      ║
╠══════════════════════════════════════════════════════════════╣
║ Passes: general(Claude/Codex) · adversarial(opposite CLI) · tests ║
╚══════════════════════════════════════════════════════════════╝
```

Then:

```bash
CRITICAL_COUNT=<count from findings>
HIGH_COUNT=<count from findings>

if [ "$CRITICAL_COUNT" -gt 0 ] || [ "$HIGH_COUNT" -gt 0 ]; then
  echo ""
  echo "GATE: FAIL — $CRITICAL_COUNT CRITICAL + $HIGH_COUNT HIGH findings"
  echo "Fix all CRITICAL and HIGH issues before proceeding to the next phase."
  echo "Re-run /review-gate after fixing to confirm gate passes."
  exit 1
fi

echo ""
echo "GATE: PASS — 0 CRITICAL, 0 HIGH"
echo "Proceed to next phase."
exit 0
```

## Addressing findings

- **CRITICAL**: fix immediately, re-run gate
- **HIGH**: fix before next phase; document in PR if deferring with justification
- **MEDIUM**: fix in this phase or file as follow-up task
- **LOW**: optional; document if skipping

## Failure modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| CLI not found | Opposite-harness CLI not installed | Install the missing CLI and re-run |
| API error | Rate limit or network issue | Re-run after a short delay; gate still reports partial findings |
| Empty diff | Nothing staged | `git add <files>` or use `--all` flag |
| Gate times out | Diff too large | Split into smaller phases; use `--file` to scope |
