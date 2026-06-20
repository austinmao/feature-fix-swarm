---
name: codex-gate
description: "Cross-model adversarial code review on the staged diff. 3-pass: general quality → adversarial → test-coverage gap. Blocks shipping on HIGH/CRITICAL findings."
version: 1.0.0
requires:
  bins:
    - codex
---

# /codex-gate

Cross-model 3-pass review of the current git diff. Uses Codex (OpenAI) as an independent
adversarial reviewer alongside Claude. HIGH/CRITICAL findings block the next phase.

> **Requires:** `codex` CLI (`npm install -g @openai/codex`). If missing, this skill exits with
> a clear error and install instructions — it does not silently skip.

## When to run

- End of every implementation phase (enforced by `/spec-decompose` task format)
- Before `/ship` on high-blast-radius PRs (multi-tenant, infra, auth, payments, RLS, cron)
- Any time you want a second opinion from a different model family

Cost: ~$2 · Time: ~13 min · Returns: structured findings list with severity

## Pipeline

```
┌─────────────────────────────────────────────────────────────────┐
│  /codex-gate                                                    │
│                                                                 │
│  Pre-check                                                      │
│    └─ verify codex CLI available; exit with install hint if not │
│                                                                 │
│  Pass 1 — General quality (Claude)                              │
│    └─ bugs, naming, error handling, style, docs gaps            │
│                                                                 │
│  Pass 2 — Adversarial (Codex / GPT-4o)                         │
│    └─ security, edge cases, injection, auth bypass, race conds  │
│                                                                 │
│  Pass 3 — Test-coverage gap (Claude)                            │
│    └─ uncovered branches, missing unit/integration/E2E tests    │
│                                                                 │
│  Merge → deduplicate → rank by severity                         │
│  EXIT 0 if 0 CRITICAL + 0 HIGH; EXIT 1 otherwise               │
└─────────────────────────────────────────────────────────────────┘
```

## Usage

```
/codex-gate              # review staged diff (default)
/codex-gate --all        # review all changes vs main branch
/codex-gate --file path  # review a single file
/codex-gate --dry-run    # print what would be reviewed, no API calls
```

## Implementation

### Pre-check — verify codex CLI

```bash
if ! command -v codex >/dev/null 2>&1; then
  echo "ERROR: codex CLI not found."
  echo "Install: npm install -g @openai/codex"
  echo "Then re-run: /codex-gate"
  exit 1
fi

if ! command -v claude >/dev/null 2>&1; then
  echo "ERROR: claude CLI not found. Install from https://claude.ai/code"
  exit 1
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
  echo "codex-gate: no diff found. Nothing to review."
  exit 0
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "[dry-run] Would review $(echo "$DIFF" | wc -l) lines of diff across 3 passes"
  exit 0
fi
```

### Pass 1 — General quality (Claude)

Invoke a sub-agent (model: sonnet) with the diff and this prompt:

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

### Pass 2 — Adversarial (Codex)

Run codex as an independent adversarial reviewer:

```bash
echo "$DIFF" | codex review \
  --system "You are an adversarial security and correctness reviewer. Find: SQL injection, XSS, auth bypass, race conditions, insecure defaults, privilege escalation, secret exposure, SSRF, path traversal, OWASP Top 10. Format each finding as SEVERITY/FILE/LINE/ISSUE/FIX. Findings only." \
  --model gpt-4o \
  2>/dev/null || echo "[codex pass skipped — API error]"
```

If codex API call fails (no key, rate limit, network), log the skip and continue with passes 1 and 3 only. Never hard-fail on a codex API error — the gate still runs with 2/3 passes.

### Pass 3 — Test-coverage gap (Claude)

Invoke a sub-agent (model: sonnet) with the diff:

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
║ codex-gate — Phase review                                    ║
╠══════════════════════════════════════════════════════════════╣
║ CRITICAL: N  HIGH: N  MEDIUM: N  LOW: N                      ║
╠══════════════════════════════════════════════════════════════╣
║ Passes: general(Claude) · adversarial(Codex/GPT-4o) · tests  ║
╚══════════════════════════════════════════════════════════════╝

[CRITICAL] path/to/file.ts:42
  SQL query built with string concatenation → SQL injection
  Fix: use parameterized query: db.query(sql, [param])

[HIGH] path/to/auth.ts:87
  Missing rate-limit on /api/login endpoint
  Fix: add rateLimiter middleware before handler
...
```

Then:

```bash
CRITICAL_COUNT=<count from findings>
HIGH_COUNT=<count from findings>

if [ "$CRITICAL_COUNT" -gt 0 ] || [ "$HIGH_COUNT" -gt 0 ]; then
  echo ""
  echo "GATE: FAIL — $CRITICAL_COUNT CRITICAL + $HIGH_COUNT HIGH findings"
  echo "Fix all CRITICAL and HIGH issues before proceeding to the next phase."
  echo "Re-run /codex-gate after fixing to confirm gate passes."
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
| `codex: command not found` | Codex CLI not installed | `npm install -g @openai/codex` |
| Codex pass skipped | API key missing or rate limit | Set `OPENAI_API_KEY` env var; gate still runs 2/3 passes |
| Empty diff | Nothing staged | `git add <files>` or use `--all` flag |
| Gate times out | Diff too large | Split into smaller phases; use `--file` to scope |
