---
name: review-gate
description: "Host-neutral pre-merge review gate. Runs the opposite CLI from the active harness so Codex reviews with Claude and Claude reviews with Codex. 3-pass: general quality → adversarial → test-coverage gap. Blocks shipping on HIGH/CRITICAL findings."
version: "1.5.0"
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

Cost: ~$2 · Time: ~13 min at STANDARD (less at LIGHT, more at FULL) · Returns: structured findings list with severity

## Pipeline

```
┌─────────────────────────────────────────────────────────────────┐
│  /review-gate                                                   │
│                                                                 │
│  Pre-check                                                      │
│    └─ choose opposite CLI for the active harness               │
│                                                                 │
│  Tier selection (v1.4.0)                                        │
│    └─ review-tier.sh sizes passes to diff risk (light/std/full) │
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

`REVIEW_TIER=light|standard|full` overrides auto-tier-detection (see Tier selection below).

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
FILE_PATH=""
_next_is_file=0

# zsh-safe: parameter expansion does not word-split in zsh, but
# command-substitution output does (bash + zsh + dash) — no subshell,
# so assignments inside the loop persist.
for arg in $(printf '%s\n' "${ARGS}"); do
  if [ "$_next_is_file" -eq 1 ]; then
    FILE_PATH="$arg"
    _next_is_file=0
    continue
  fi
  case "$arg" in
    --all)        DIFF_TARGET="main" ;;
    --dry-run)    DRY_RUN=1 ;;
    --file=*)     DIFF_TARGET="file"; FILE_PATH="${arg#--file=}" ;;
    --file)       DIFF_TARGET="file"; _next_is_file=1 ;;
  esac
done

# Fixed --file parity (finding: the old `--file*` case retained the flag
# itself as DIFF_TARGET and discarded the path, so `git diff --file...HEAD`
# silently produced an empty diff and bypassed review). Both `--file <path>`
# (two tokens, above) and `--file=<path>` are parsed into FILE_PATH; file
# mode diffs via `git diff HEAD -- "$FILE_PATH"`.
if [ "$DIFF_TARGET" = "--staged" ]; then
  DIFF=$(git diff --staged 2>/dev/null)
  [ -z "$DIFF" ] && DIFF=$(git diff HEAD 2>/dev/null)
elif [ "$DIFF_TARGET" = "file" ]; then
  DIFF=$(git diff HEAD -- "$FILE_PATH" 2>/dev/null)
else
  DIFF=$(git diff "$DIFF_TARGET"...HEAD 2>/dev/null)
fi

if [ -z "$DIFF" ]; then
  echo "review-gate: no diff found. Nothing to review."
  exit 0
fi
```

### Adversarial prompt (defined once — used by Pass 2 and the FULL-tier adversary)

Defined BEFORE tier selection because the FULL-tier extra adversary (in the tier
block below) references `$ADVERSARIAL_PROMPT`; a forward reference would feed it an
empty prompt. Pass 2 reuses the same value — one definition, no drift.

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
ADVERSARIAL_PROMPT="You are an adversarial security and correctness reviewer. $SCOPE_CLAUSE Find: SQL injection, XSS, auth bypass, race conditions, insecure defaults, privilege escalation, secret exposure, SSRF, path traversal, OWASP Top 10. Format each finding as SEVERITY/FILE/LINE/ISSUE/CAUSE/PROVENANCE(introduced-by-diff or pre-existing, confidence clear|likely|unknown)/FIX/PROOF. Findings only. If there are no findings, output exactly: NO FINDINGS."
```

### Tier selection (v1.4.0)

`scripts/gsd/review-tier.sh` classifies the SAME diff this run is about to review as
light/standard/full, so a 2-file docs diff doesn't pay a 40-file auth review. The mode
passed MIRRORS `DIFF_TARGET` above and classifies the SAME bytes: `--staged` (default) /
`--all` with `REVIEW_TIER_BASE=main` PINNED so its base matches the gate's hardcoded
`main...HEAD` / `--file "$FILE_PATH"`.

```bash
case "$DIFF_TARGET" in
  --staged)
    TIER_LINE="$(scripts/gsd/review-tier.sh --staged 2>/dev/null)"
    ;;
  main)
    TIER_LINE="$(REVIEW_TIER_BASE=main scripts/gsd/review-tier.sh --all 2>/dev/null)"
    ;;
  file)
    TIER_LINE="$(scripts/gsd/review-tier.sh --file "$FILE_PATH" 2>/dev/null)"
    ;;
esac
_tier_rc=$?

TIER="standard"
TIER_REASON="fail-safe"
if [ "$_tier_rc" -eq 0 ] && [ -n "$TIER_LINE" ]; then
  _tier_token="${TIER_LINE%% *}"
  case "$_tier_token" in
    light|standard|full)
      TIER="$_tier_token"
      TIER_REASON="${TIER_LINE#* }"
      ;;
    *)
      echo "[review-gate] WARN: review-tier.sh returned an unrecognized tier token — falling back to standard (fail-safe: under-review beats over-trust)." >&2
      ;;
  esac
else
  echo "[review-gate] WARN: review-tier.sh missing, non-executable, or failed (incl. empty stdout) — falling back to standard (fail-safe: under-review beats over-trust)." >&2
fi
```

Tier scopes the DEFECT passes (Pass 1-3 + the FULL extra adversary) ONLY, with IMPERATIVE
gating — a prose table saying "LIGHT runs Pass 1" is not enough; these are literal SKIP
directives to the executor:

- **LIGHT**: run Pass 1 ONLY. SKIP Pass 2. SKIP Pass 3.
- **STANDARD**: run Pass 1, Pass 2, Pass 3 (current default behavior).
- **FULL**: run Pass 1, Pass 2, Pass 3, THEN a MANDATORY refute-or-promote round on EVERY
  HIGH/CRITICAL finding (see "### Refute-or-promote" below), PLUS one extra cross-model
  adversary sourced from `scripts/gsd/adversary-host.sh` — invoke it, do NOT hand-roll an
  unsandboxed reviewer against attacker-influenceable diff/prompt text:

  ```bash
  if [ "$TIER" = "full" ]; then
    . scripts/gsd/adversary-host.sh
    _adv_host="$(detect_orchestrator_host)"
    _adv_kind="$(adversary_kind_for_host "$_adv_host")"
    # adversary_invoke's 3rd arg is the MODEL, not the review bin. Mirror
    # adversary-host.sh's own conventions: codex-kind → gpt-5.6-sol @ xhigh;
    # claude-kind → opus (effort ignored for claude).
    if [ "$_adv_kind" = "codex" ]; then
      _adv_model="gpt-5.6-sol"; _adv_effort="xhigh"
    else
      _adv_model="opus"; _adv_effort=""
    fi
    if ! adversary_invoke "$_adv_kind" 480 "$_adv_model" "$_adv_effort" \
      "$ADVERSARIAL_PROMPT (FULL-tier extra cross-model adversary — feed findings into the SAME ### Merge and rank as Pass 1-3)"; then
      echo "[review-gate] WARN: FULL-tier adversary unavailable — findings incomplete." >&2
    fi
  fi
  ```

| Tier | Pass 1 | Pass 2 | Pass 3 | Extra adversary (adversary-host.sh) |
|------|--------|--------|--------|--------------------------------------|
| light | run | SKIP | SKIP | — |
| standard | run | run | run | — |
| full | run | run | run | run (xhigh, read-only sandbox) |

The honest-verifier pass below is NOT tier-scoped: tier selection sizes the DEFECT passes
only and never suppresses an otherwise-eligible honest-verifier — its existing skip
conditions (no spec resolvable, `GSD_REQUIRED=0`) are unchanged at every tier.

### Findings queue: capability probe + resolved-sig consult

`lib/gates.py findings-queue` persists every merged finding across runs so a fix round
works the WHOLE queue instead of re-litigating what a prior run already resolved. Resolve
GATES_PY via a CAPABILITY PROBE, not first-exists — an installed `~/.claude` copy of
gates.py can lack findings-queue support and would win a naive first-exists loop:

```bash
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
GATES_PY=""
DEGRADED_PERSISTENCE=0
for candidate in \
  "$REPO_ROOT/packages/feature-fix-swarm/lib/gates.py" \
  "$HOME/.claude/lib/feature-fix-swarm/gates.py" \
  "$REPO_ROOT/lib/gates.py"; do
  [ -f "$candidate" ] || continue
  if (cd "$REPO_ROOT" && python3 "$candidate" findings-queue list >/dev/null 2>&1); then
    GATES_PY="$candidate"
    break
  fi
done

RESOLVED_SIGS=""
if [ -z "$GATES_PY" ]; then
  echo "[review-gate] WARN: findings persistence unavailable — no gates.py candidate supports findings-queue." >&2
  DEGRADED_PERSISTENCE=1
else
  _fq_list="$(cd "$REPO_ROOT" && python3 "$GATES_PY" findings-queue list 2>/dev/null)"
  if [ $? -ne 0 ]; then
    echo "[review-gate] WARN: findings-queue list failed — proceeding without a resolved-sig skip-list." >&2
    DEGRADED_PERSISTENCE=1
  else
    RESOLVED_SIGS="$(printf '%s' "$_fq_list" | python3 -c 'import json,sys; print(" ".join(f["sig"] for f in json.load(sys.stdin) if f.get("resolved")))' 2>/dev/null)"
  fi
fi
```

RESOLVED_SIGS is built from the FULL queue (`findings-queue list`), never
`list --unresolved` — that flag EXCLUDES exactly the resolved records this skip-list
needs. A finding whose recorded sig lands in RESOLVED_SIGS is dropped after recording
below (see "### Record findings") — resolved findings never re-enter ranking, no
re-litigation. Capability-probe + best-effort: an unresolved/broken GATES_PY WARNs, sets
`DEGRADED_PERSISTENCE=1`, and proceeds — recording NEVER blocks the verdict.

```bash
if [ "$DRY_RUN" -eq 1 ]; then
  echo "[dry-run] Tier: $TIER ($TIER_REASON) — $(echo "$DIFF" | wc -l) lines of diff"
  case "$TIER" in
    light)    echo "[dry-run] Would run: Pass 1 only (SKIP Pass 2, SKIP Pass 3)" ;;
    standard) echo "[dry-run] Would run: Pass 1, Pass 2, Pass 3" ;;
    full)     echo "[dry-run] Would run: Pass 1, Pass 2, Pass 3 + refute-or-promote + adversary-host.sh adversary" ;;
  esac
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
  ISSUE: <one sentence — the symptom>
  CAUSE: <root cause, not the symptom restated>
  PROVENANCE: <introduced-by this diff | made-visible-by this diff (pre-existing)> (confidence: clear|likely|unknown)
  FIX: <best fix at the root cause, one sentence>
  PROOF: <how to verify the fix worked — a command or observable>

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
# ADVERSARIAL_PROMPT + SCOPE_CLAUSE are defined once above (### Adversarial
# prompt), before tier selection, so the FULL-tier adversary reuses the same
# value with no forward reference.
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
    file)     DIFF_DESCRIPTION="the changes in ${FILE_PATH} (run: git diff HEAD -- '${FILE_PATH}')" ;;
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

### Honest-verifier pass (v1.3.0)

The 3 passes above find DEFECTS in the diff. They do not answer "did this diff
achieve the phase GOAL, and if the spec can't tell, do I abstain instead of
false-passing?" That is `gsd-verifier`'s job — goal-backward verification with an
**abstain** disposition: an unresolvable criterion routes to `human_needed`
instead of a false `passed`.

**Runs only when a spec is resolvable** (review-gate is otherwise spec-agnostic).
`GSD_REQUIRED=0` skips this pass (prints `GSD-SKIP`, exit 0):

```bash
# resolve the spec for this diff (branch NNN → specs/NNN-*/spec.md)
HV_SPEC=""
_NNN=$(git branch --show-current 2>/dev/null | grep -oE '^[0-9]{3}' | head -1)
[ -n "$_NNN" ] && HV_SPEC=$(find specs -maxdepth 2 -name spec.md -path "*${_NNN}-*" 2>/dev/null | head -1)

VERIFIER_STATE="SKIPPED"
if [ -z "$HV_SPEC" ]; then
  echo "[review-gate] honest-verifier: no spec resolvable for this diff — skipped (diff-only passes stand)."
elif [ "${GSD_REQUIRED:-1}" = "0" ]; then
  echo "GSD-SKIP"
else
  # gsd-verifier is installed by gsd-core's `--claude` install step (spec 002)
  if [ ! -f "$HOME/.claude/agents/gsd-verifier.md" ] && [ ! -f ".claude/agents/gsd-verifier.md" ]; then
    echo "[review-gate] gsd-verifier agent not installed — run 'node_modules/.bin/gsd-core install --claude' or GSD_REQUIRED=0 to skip."
    exit 1
  fi
  # spawn the verifier (below); capture its verdict line into VERIFIER_STATE
fi
```

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
  '${DIFF_TARGET}' = 'file'     → git diff HEAD -- '${FILE_PATH}'.

Disposition:
- INFERABLE criterion (determinable from the spec) → grade ✓ VERIFIED / ✗ FAILED
  as usual. NEVER abstain on these (over-abstention guard).
- NON-INFERABLE criterion — one whose correct answer is not derivable from the
  spec text alone (merge semantics, grapheme-vs-codeunit, tie-breaking) — verify
  ONLY if there is EXPLICIT evidence (a held-out/property test that passes, or a
  behavior you directly observed in the diff). Symbol presence + wiring is NOT
  explicit evidence. With no explicit evidence → ABSTAIN:
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
(`human_needed`).

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
   block-vs-pass). Order the check by PROVENANCE confidence: `unknown` first,
   then `likely`, then `clear` — an unknown-provenance claim is the most
   likely to be stale/wrong, so it gets the freshest eyes.
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

### Merge and rank

Collect all findings from passes 1-3 (+ the FULL-tier extra adversary, when it ran).
Deduplicate by (file, line, issue-text similarity). Rank:

1. CRITICAL (any pass)
2. HIGH (any pass)
3. MEDIUM
4. LOW

### Record findings (findings-queue)

For EACH finding surviving "### Merge and rank" above, persist it so future runs (fix
rounds, re-reviews) dedup against it instead of re-litigating:

```bash
DEDUPED_COUNT=0
if [ -n "$GATES_PY" ]; then
  # for each merged finding (FINDING_FILE, FINDING_ISSUE):
  _fq_add_out="$(cd "$REPO_ROOT" && python3 "$GATES_PY" findings-queue add "$FINDING_FILE" "$FINDING_ISSUE" 2>/dev/null)"
  if [ $? -ne 0 ]; then
    echo "[review-gate] WARN: findings-queue add failed for $FINDING_FILE — proceeding without persistence for this finding." >&2
    DEGRADED_PERSISTENCE=1
  else
    _sig="$(printf '%s' "$_fq_add_out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sig"])' 2>/dev/null)"
    case " $RESOLVED_SIGS " in
      *" $_sig "*)
        # resolved in a prior run — DROP from ranking, never re-litigate.
        DEDUPED_COUNT=$((DEDUPED_COUNT + 1))
        ;;
      *)
        : # new or still-unresolved — keep in ranking.
        ;;
    esac
  fi
else
  echo "[review-gate] WARN: findings-queue unavailable — this run's findings are NOT persisted." >&2
fi
echo "deduped: $DEDUPED_COUNT"
```

Invariants:
- Capability-probe + best-effort: unresolved/broken GATES_PY → WARN, `DEGRADED_PERSISTENCE=1`,
  proceed. Every add/list/resolve nonzero exit gets its own stderr warn + sets
  `DEGRADED_PERSISTENCE=1` — recording is NEVER a block condition beyond the existing
  CRITICAL/HIGH + honest-verifier logic.
- `deduped: N` counts RESOLVED-SIG skips ONLY. `findings-queue add` also reports
  `deduped:true` for an existing UNRESOLVED sig (add-idempotent — one entry, never
  re-appended) but an unresolved re-add is NOT counted in `deduped: N`; only a
  RESOLVED-sig match drops a finding from ranking.
- EDGE-004: a reworded issue on the same file yields a DISTINCT signature — dedup is
  exact-normalized (whitespace-collapse + lowercase), not fuzzy/semantic.
- Additive telemetry only: recording does not change the CRITICAL/HIGH + honest-verifier
  PASS/FAIL exit logic below.

**Resolve lifecycle:** after a fix round, when a re-run's gate is green on a finding that
was previously recorded (it no longer surfaces in this run's merged findings), mark it
resolved so subsequent re-runs skip it:

```bash
if [ -n "$GATES_PY" ] && [ -n "${FIXED_FINDING_SIG:-}" ]; then
  if ! (cd "$REPO_ROOT" && python3 "$GATES_PY" findings-queue resolve "$FIXED_FINDING_SIG" >/dev/null 2>&1); then
    echo "[review-gate] WARN: findings-queue resolve failed for $FIXED_FINDING_SIG." >&2
    DEGRADED_PERSISTENCE=1
  fi
fi
```

### Output and exit

Print findings in severity order:

```
╔══════════════════════════════════════════════════════════════╗
║ review-gate — Phase review                                   ║
╠══════════════════════════════════════════════════════════════╣
║ Tier: <tier> (<reason>)                                       ║
║ CRITICAL: N  HIGH: N  MEDIUM: N  LOW: N                      ║
║ deduped: N (resolved-sig skips)                               ║
╠══════════════════════════════════════════════════════════════╣
║ Passes: <passes actually run for this tier — see the tier table> ║
╚══════════════════════════════════════════════════════════════╝
```

If `DEGRADED_PERSISTENCE=1`, print one more footer line — the verdict/exit logic below is
otherwise UNCHANGED; this is additive telemetry only:

```bash
if [ "$DEGRADED_PERSISTENCE" -eq 1 ]; then
  echo "findings persistence: DEGRADED"
fi
```

Then:

```bash
CRITICAL_COUNT=<count from findings>
HIGH_COUNT=<count from findings>
# VERIFIER_STATE set by the honest-verifier pass above: PASS | FAIL | ABSTAIN | SKIPPED

if [ "$CRITICAL_COUNT" -gt 0 ] || [ "$HIGH_COUNT" -gt 0 ]; then
  echo ""
  echo "GATE: FAIL — $CRITICAL_COUNT CRITICAL + $HIGH_COUNT HIGH findings"
  echo "Fix all CRITICAL and HIGH issues before proceeding to the next phase."
  echo "Re-run /review-gate after fixing to confirm gate passes."
  exit 1
fi

if [ "$VERIFIER_STATE" = "FAIL" ] || [ "$VERIFIER_STATE" = "ABSTAIN" ]; then
  echo ""
  echo "GATE: FAIL — honest-verifier: $VERIFIER_STATE (0 CRITICAL/HIGH, but goal not confirmed)"
  echo "Route to human_needed — do not auto-pass on an abstained or failed criterion."
  exit 1
fi

echo ""
echo "GATE: PASS — 0 CRITICAL, 0 HIGH, honest-verifier: ${VERIFIER_STATE:-SKIPPED}"
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
