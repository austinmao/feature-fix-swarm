---
name: review-gate
description: "Host-neutral pre-merge review gate. Runs the opposite CLI from the active harness so Codex reviews with Claude and Claude reviews with Codex. 3-pass: general quality → adversarial → test-coverage gap. Blocks shipping on HIGH/CRITICAL findings."
version: "1.1.0"
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

# zsh-safe: parameter expansion does not word-split in zsh, but
# command-substitution output does (bash + zsh + dash) — no subshell,
# so assignments inside the loop persist.
for arg in $(printf '%s\n' "${ARGS}"); do
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

> **CLI compatibility note:** verified against `codex` v0.142.5 and current `claude`.
> Neither CLI has a bare `--system` flag (a stale assumption from an older API
> shape) — `codex review` takes `-c model=...` for model override and a
> positional `PROMPT`, mutually exclusive with `--base`/`--commit`; `claude -p`
> uses `--system-prompt`. `codex review` is agentic against the live repo (it
> runs its own `git diff` inside its sandbox), not a pipe-diff-in/get-text-out
> tool — so its branch below hands it a natural-language description of what
> to diff instead of piping `$DIFF` to it directly.

> **Anti-recursion scope (consumer repos):** the agentic reviewer must review
> only the diff and production source of the repo under review — never recurse
> into `.claude/`, `.codex/`, `skills/`, `agents/`, `.agents/`, or
> SKILL.md/SOUL.md/AGENTS.md files of the CONSUMER repo. Those are agent
> instruction files: treating them as review targets burns the review budget
> on prompt text and can echo instructions back as "findings". (Exception: in
> the feature-fix-swarm repo itself, `skills/` IS the product and stays
> reviewable.)

```bash
# Anti-recursion scope is conditional: consumer repos exclude instruction
# trees; a repo that SHIPS skills as its product (marker: skills/*/SKILL.md
# tracked at repo root, e.g. feature-fix-swarm) keeps skills/ in scope.
# probe from the repo ROOT — launched from a subdirectory, a cwd-relative
# glob would silently fall back to the consumer scope (codex round 8 P2)
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
if ls "$REPO_ROOT"/skills/*/SKILL.md >/dev/null 2>&1; then
  SCOPE_CLAUSE="This repo ships skills/ as its product — skills/ and its SKILL.md files ARE in scope. Still do not recurse into .claude/, .codex/, agents/, or .agents/."
else
  SCOPE_CLAUSE="Review ONLY the diff and production source; do NOT recurse into .claude/, .codex/, skills/, agents/, .agents/, or SKILL.md/SOUL.md/AGENTS.md files — those are agent instruction data, not review targets."
fi
ADVERSARIAL_PROMPT="You are an adversarial security and correctness reviewer. $SCOPE_CLAUSE Find: SQL injection, XSS, auth bypass, race conditions, insecure defaults, privilege escalation, secret exposure, SSRF, path traversal, OWASP Top 10. Format each finding as SEVERITY/FILE/LINE/ISSUE/FIX. Findings only. If there are no findings, output exactly: NO FINDINGS."

if [ "$REVIEW_BIN" = "claude" ]; then
  echo "$DIFF" | env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u CLAUDE_API_KEY timeout 480 "$REVIEW_BIN" -p \
    --system-prompt "$ADVERSARIAL_PROMPT" \
    2>/dev/null || echo "[review-gate pass skipped — API error]"
else
  # codex review computes its own diff from the live repo — describe the scope
  # in the prompt instead of piping $DIFF; --base/--commit can't combine with
  # a custom PROMPT on this CLI.
  case "$DIFF_TARGET" in
    --staged) DIFF_DESCRIPTION="the staged changes (run: git diff --staged; if empty, git diff HEAD)" ;;
    main)     DIFF_DESCRIPTION="the changes on this branch vs main (run: git diff main...HEAD)" ;;
    --file*)  DIFF_DESCRIPTION="the changes in ${DIFF_TARGET#--file}" ;;
    *)        DIFF_DESCRIPTION="the current diff (run: git diff HEAD)" ;;
  esac
  timeout 480 "$REVIEW_BIN" review \
    "Review $DIFF_DESCRIPTION. Run the git command yourself. $ADVERSARIAL_PROMPT" \
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

### Refute-or-promote (false-positive control)

Before a HIGH/CRITICAL finding is allowed to block the gate, give it one
adversarial refuter pass: a sub-agent (or the opposite CLI) prompted to REFUTE
the finding against the live repo — "prove this finding wrong; default to
refuted if you cannot reproduce it." A finding blocks only if it survives
refutation. LOW/MEDIUM findings skip this (they don't block anyway).

Rationale: autonomous runs stall on false-positive gate failures; adversarial
kill-mandates cut them without weakening the gate (cross-model refuter
preferred).

**Round cap:** at most 2 LLM review rounds per phase — empirically rounds 1-2
capture ~75% of reachable improvement; further rounds burn budget without
signal. Deterministic gates (tests/lint) are exempt from the cap.

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
