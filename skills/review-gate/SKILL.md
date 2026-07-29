---
name: review-gate
description: "Host-neutral pre-merge review gate. Runs the opposite CLI from the active harness so Codex reviews with Claude and Claude reviews with Codex. 3-pass: general quality → adversarial → test-coverage gap. Blocks shipping on HIGH/CRITICAL findings."
version: "1.3.0"
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

Then act as a TEST-GATE CRITIC on the tests this diff ADDS or CHANGES
(review the ruler, not just the coverage):
- Would each changed/added test FAIL under the old broken behavior? A test
  that passes either way proves nothing — flag it HIGH.
- Does the test exercise the real production path, or a reimplementation
  inside the test?
- Was any existing gate weakened to turn green (loosened assert, added skip,
  widened tolerance)?
- Name the "easy fake pass" for this task and check the tests rule it out.

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

### Verify-the-reviewer (v3.17.0)

A review is a signal, not ground truth — reviewers read stale code, invent
line numbers, and misread intent. Before ACTING on the merged verdict:

1. List the verdict's load-bearing claims (the 1-3 findings that decide
   block-vs-pass).
2. Open the cited files at CURRENT HEAD (the code may have moved since the
   reviewer ran).
3. Classify each claim: `REAL_BLOCKER` (fix before merge) ·
   `REAL_NON_BLOCKING` (land small follow-up or file it) · `STALE` (reviewer
   read old code) · `WRONG` (claim does not match current code) ·
   `CONFIRMED_PASS` (for a clean PASS, spot-check the 1-2 claims that would
   hurt most if wrong).
4. `STALE`/`WRONG` findings do not block — record WHY they were discarded in
   the findings report so the discard is auditable.

This composes with refute-or-promote: refute-or-promote kills false positives
BEFORE they block; verify-the-reviewer catches stale/wrong claims (and false
PASSes) at the moment of decision. The `/verify-review` skill is the
standalone operator-invocable form.

### Honest-verifier pass (v1.3.0 — gsd borrow)

The 3 passes above find DEFECTS in the diff. They do not answer "did this diff
achieve the phase GOAL, and if the spec can't tell, do I abstain instead of
false-passing?" That is `gsd-verifier`'s job — goal-backward verification with an
**abstain** disposition (`insufficient_spec` → `human_needed`, **never a false
`passed`**). Measured: confident-false-pass on a non-inferable check drops
100% → 17% — but ONLY when fed edge-probe `backstop` tags (the trigger is
exogenous; "abstain if unsure" alone gets 100% → 67%). See
`references/honest-verifier.md`.

**Runs only when a spec is resolvable** (review-gate is otherwise spec-agnostic);
gsd is hard-required (`GSD_REQUIRED=1`) when it does run:

```bash
# resolve gates.py across the three install shapes (same order as elsewhere)
GATES_PY=""
for _cand in \
  "$(git rev-parse --show-toplevel 2>/dev/null)/packages/feature-fix-swarm/lib/gates.py" \
  "$HOME/.claude/lib/feature-fix-swarm/gates.py" \
  "$(git rev-parse --show-toplevel 2>/dev/null)/lib/gates.py"; do
  [ -f "$_cand" ] && GATES_PY="$_cand" && break
done

# resolve the spec for this diff (branch NNN → specs/NNN-*/spec.md)
HV_SPEC=""
_NNN=$(git branch --show-current 2>/dev/null | grep -oE '^[0-9]{3}' | head -1)
[ -n "$_NNN" ] && HV_SPEC=$(find specs -maxdepth 2 -name spec.md -path "*${_NNN}-*" 2>/dev/null | head -1)

VERIFIER_STATE="SKIPPED"
if [ -z "$HV_SPEC" ] || [ -z "$GATES_PY" ]; then
  echo "[review-gate] honest-verifier: no spec resolvable for this diff — skipped (diff-only passes stand)."
else
  python3 "$GATES_PY" check-gsd --agent gsd-verifier || {
    echo "[review-gate] gsd-verifier unavailable — install gsd-core "
         "(npx @opengsd/gsd-core@latest --claude --global) or GSD_REQUIRED=0 to skip."
    exit 1
  }
  # spawn the verifier (below); capture its verdict line into VERIFIER_STATE
fi
```

`GSD_REQUIRED=0` prints `GSD-SKIP` (exit 0) and this pass is bypassed.

**Spawn with an override prompt** (gsd-verifier is gsd-`.planning`-native — same
redirect posture as the plan-checker gate in spec-decompose):

```
Task({ subagent_type: "gsd-verifier", model: "sonnet", prompt: `
You are verifying that executed work achieved the phase goal. This is a
feature-fix-swarm repo, NOT a gsd .planning/ repo.

DO NOT run gsd-tools.cjs / init.phase-op / verify.* queries. DO NOT read
ROADMAP.md / PLAN.md / VERIFICATION.md — they do not exist here. Skip your
gsd-tools loading steps.

Inputs:
- Goal + acceptance criteria (the truths to verify): ${HV_SPEC}
- The work under review: the current git diff. Run it yourself:
  '${DIFF_TARGET}' = '--staged' → git diff --staged (if empty, git diff HEAD);
  '${DIFF_TARGET}' = 'main'     → git diff main...HEAD;
  a --file target                → git diff for that path.

Apply the honest-verifier disposition (references/honest-verifier.md):
- INFERABLE criterion (determinable from the spec) → grade ✓ VERIFIED / ✗ FAILED
  as usual. NEVER abstain on these (over-abstention guard).
- NON-INFERABLE criterion — one tagged 'verification: backstop' in the spec, OR
  one whose correct answer is not derivable from the spec text alone (merge
  semantics, grapheme-vs-codeunit, tie-breaking) — verify ONLY if there is
  EXPLICIT evidence (a held-out/property test that passes, or a behavior you
  directly observed in the diff). Symbol presence + wiring is NOT explicit
  evidence. With no explicit evidence → ABSTAIN:
  'ABSTAIN: <criterion> — insufficient_spec, held-out test recommended'.
  NEVER emit passed for an abstained criterion.

End with EXACTLY ONE of:
  VERIFIER: PASS      — all inferable criteria VERIFIED, no unresolved abstains
  VERIFIER: FAIL      — one or more criteria ✗ FAILED (list them)
  VERIFIER: ABSTAIN   — N non-inferable criteria unverified (list them);
                        route to human_needed, do NOT auto-pass
` })
```

**Effect on the gate verdict:** capture the verifier's final line as
`VERIFIER_STATE` (PASS | FAIL | ABSTAIN | SKIPPED). It composes with the defect
counts below: **FAIL or ABSTAIN means the gate does NOT auto-PASS even at
0 CRITICAL / 0 HIGH** — surface the abstained/failed criteria for the operator
(`human_needed`). The ceiling is honest: without edge-probe `backstop` tags in
`${HV_SPEC}` (spec-decompose edge-probe gate, stream 3) this runs the weak
abstain-if-unsure form; wiring those tags upgrades it to the measured 100% → 17%.

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
