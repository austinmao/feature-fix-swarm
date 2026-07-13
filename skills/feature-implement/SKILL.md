---
name: feature-implement
description: "Execute a decomposed feature via the host-native gsd-core loop (plan-phase → execute-phase → verify), wrapped in FFS walls: preflight PASS + autonomy-grant ledger for --autonomous, gates.py as sole completion authority (test_command + phase-evidence hook), review-gate grant wall at ship. Codex invokes Codex models; Claude invokes Claude models; bounded availability failures fall back once across vendors without waiting."
version: "2.9.0"
allowed-tools:
  - Read
  - Edit
  - Bash
  - Glob
  - Skill
---

# feature-implement — Execute a feature through the gsd loop

## When to invoke

- After `/feature-spec` or `/spec-decompose` produced a seeded gsd project (`.planning/`)
  or a legacy `specs/NNN/tasks.md`
- "implement NNN", "run tasks for NNN", "execute feature NNN"
- **Adhoc (v2.6.0):** a bounded task with NO spec/plan — `--adhoc "<task>"` runs the
  same walls + gsd loop + finish tail over `/gsd-quick`. `/fix` and `/task-swarm`
  route here; the delegation machinery lives ONLY in this skill.
- Resumes gracefully — gsd `STATE.md` is the resume point (`/gsd-resume-work`)

## Invocation

```
/feature-implement [NNN]                    # execute the current gsd phase for spec NNN
/feature-implement [NNN] --autonomous       # unattended: preflight PASS + grant ledger required
/feature-implement [NNN] --dry-run          # print resolved phase + gates, don't execute
/feature-implement --adhoc "<task>"         # v2.6.0: no spec/plan — gsd-quick + walls + finish tail
/feature-implement --adhoc "<task>" --autonomous   # unattended adhoc (same fail-closed walls)
```

## Workflow

### Step 1: Resolve spec + run id

```bash
SPEC_ARG="${ARGUMENTS:-}"
AUTONOMOUS=0; DRY_RUN=0; ADHOC=0; ADHOC_TASK=""
for arg in $(printf '%s\n' "$SPEC_ARG"); do
  case "$arg" in
    --autonomous) AUTONOMOUS=1 ;;
    --dry-run)    DRY_RUN=1 ;;
    --adhoc)      ADHOC=1 ;;   # the quoted task text follows; capture it whole, not word-split
    [0-9][0-9][0-9]|[0-9][0-9][0-9]-*) SPEC_ID="$arg" ;;
  esac
done
if [ $ADHOC -eq 1 ]; then
  # ADHOC_TASK = everything after --adhoc except trailing flags (Claude: extract
  # the quoted task from the invocation — it is the gsd-quick task verbatim).
  # Ledger key: kebab slug of the first ~4 task words, prefixed adhoc-.
  ADHOC_SLUG=$(printf '%s' "$ADHOC_TASK" | tr '[:upper:]' '[:lower:]' \
    | tr -cs 'a-z0-9' '-' | cut -c1-40 | sed 's/^-//;s/-$//')
  RUN_ID="adhoc-${ADHOC_SLUG:-task}"
else
  if [ -z "${SPEC_ID:-}" ]; then
    SPEC_ID=$(git branch --show-current 2>/dev/null | grep -oE '^[0-9]{3}' | head -1)
  fi
  [ -z "$SPEC_ID" ] && { echo "ERROR: no spec ID. Usage: /feature-implement NNN | --adhoc \"<task>\""; exit 1; }
  RUN_ID="spec-${SPEC_ID%%-*}"   # ledger key — same as /feature-spec + /task-swarm
fi

# gates.py resolver (3 install shapes)
GATES_PY=""
for _c in \
  "$(git rev-parse --show-toplevel 2>/dev/null)/packages/feature-fix-swarm/lib/gates.py" \
  "$HOME/.claude/lib/feature-fix-swarm/gates.py" \
  "$(git rev-parse --show-toplevel 2>/dev/null)/lib/gates.py"; do
  [ -f "$_c" ] && GATES_PY="$_c" && break
done
[ -z "$GATES_PY" ] && { echo "ERROR: gates.py not found — run setup.sh"; exit 1; }
```

### Step 2: Walls (fail-closed, --autonomous only)

```bash
if [ "$AUTONOMOUS" = "1" ]; then
  python3 "$GATES_PY" check-preflight "$RUN_ID" || {
    echo "[feature-implement] ERROR: no fresh preflight for $RUN_ID — run /preflight first."; exit 1; }
fi
# Model availability wall (all modes): rewrite dead premium pins (fable→opus)
# before any spawn — an overnight run must not die on an OAuth catalog change.
[ -f scripts/gsd/model-fallback.sh ] && bash scripts/gsd/model-fallback.sh .planning || true
# Security fence (all modes): security-touching specs plan on opus, not fable —
# Fable classifiers can false-refuse benign defensive-security work mid-run.
[ -f scripts/gsd/security-model-fence.sh ] && bash scripts/gsd/security-model-fence.sh .planning specs/"$SPEC_ID"*/spec.md specs/"$SPEC_ID"*/plan.md || true
# Ledger key for gsd seams: review-gate-command.sh reads GSD_RUN_ID (no
# hardcoded default) — export it so ship grants key to THIS run.
export GSD_RUN_ID="$RUN_ID"
```

At every operator-gated action mid-run (push, merge, deploy, flip, secret-use):
`check-grant "$RUN_ID" --action "<type:target>"` — proceed on exit 0; otherwise
`pending` + STOP that action path only. Never bypass with prose; never re-ask a
granted action. (Ship itself is walled inside gsd's `code_review_command` —
`scripts/gsd/review-gate-command.sh` REVISEs without a `ship:gsd` grant.)

### Step 3: Ensure gsd project (spec mode) / skip (adhoc mode)

**Adhoc mode skips this step** — no seeded project is required; `/gsd-quick` plans,
executes, and verifies the single task itself. If `.planning/config.json` exists its
gate seams apply as below; if not, gsd-quick runs on gsd defaults (the test-command
gate in Step 5 still holds).

Spec mode: if `.planning/ROADMAP.md` is missing, the spec was never seeded — run
`/feature-spec NNN` (or `/spec-decompose NNN`) first; ERROR out, do not improvise
a project.

Config contract (seeded by `/feature-spec` from `templates/gsd-config.base.json`):
`workflow.test_command = bash scripts/gsd/gates-test-command.sh`,
`workflow.code_review_command = bash scripts/gsd/review-gate-command.sh`.
Verify both keys are present in `.planning/config.json` before executing; ERROR if not —
without them gsd runs ungated.

### Step 4: Execute

**Adhoc mode:** run the single task through gsd-quick (plan → execute → verify on
one quick task). TDD applies: failing repro/behavior test first (RED), then the
change (GREEN) — the gsd executor's commit trail must show both.

- Interactive Claude session: invoke `/gsd-quick "<task>"` directly.
- Interactive Codex session: invoke `$gsd-quick "<task>"` directly.
- `--autonomous` / headless on either host: `TIMEOUT=1800 bash scripts/gsd/gsd-run.sh /gsd-quick "<task>"` (the runner preserves the invoking host).

**Spec mode:** read `.planning/ROADMAP.md` for the first unchecked phase N.

- `--dry-run`: print phase N, its plans, the two config gate commands, wall status; exit 0.
- Interactive Claude session: invoke `/gsd-execute-phase N` directly.
- Interactive Codex session: invoke `$gsd-execute-phase N` directly.
- `--autonomous` / headless on either host: `TIMEOUT=3600 bash scripts/gsd/gsd-run.sh /gsd-execute-phase N` (the runner preserves the invoking host; Claude gets trimmed MCP/auth scrubbing, Codex gets its native skill/model surface).

**Host-native availability contract:** the runner always tries the invoking
host first (Codex → Codex/Terra; Claude → Claude/Sonnet) with a bounded,
read-only, no-tools model probe. If that probe fails for any reason, the runner
probes the equivalent tier on the other vendor exactly once. Only after a probe
passes does it launch the mutating GSD drive—and it launches exactly one.
Every post-launch failure (ordinary executor/test error, exit 126/127, timeout,
or provider error) keeps its original exit code and NEVER crosses vendors;
resume from durable phase/gate state instead. Read-only review timeouts may
safely fall back once. Every call uses `run_bounded`; there is no wait-for-quota
loop. Set `FFS_CROSS_VENDOR_FALLBACK=off` only for forensic isolation.

**Anti-early-stop (autonomous orchestrator loop).** Fable early-stops long runs
with text-only intent; hold this line every turn of the drive loop:

> Before ending your turn, check your last paragraph. If it is a plan, an
> analysis, a question, a list of next steps, or a promise about work you have
> not done ("I'll…"), do that work now with tool calls. End your turn only when
> the task is complete or you are blocked on input only the user can provide.

(Orchestrator-level mitigation: per-turn work inside gsd-core sub-agents is
gsd-core's to guard — deeper coverage needs a gsd-core change, out of scope.)

On verifier gaps: `/gsd-plan-phase N --gaps` then `/gsd-execute-phase N --gaps-only`
(same runner), max 2 gap rounds, then STOP and report.

### Step 5: Completion authority

See `docs/promotion-protocol.md` for the full 12-rule dev→staging→production
promotion protocol; the completion authority below enforces rules 7, 8, and 12 of it.

gsd's verifier gates `phase.complete`, but the checkbox authority is gates.py:
- `GATES_STRICT=1 python3 "$GATES_PY" verify-done gsd-phase` must exit 0
- the `gsd-phase-evidence-gate.sh` PreToolUse hook blocks ROADMAP/STATE
  phase-complete flips without that evidence (`GATES_BYPASS=1` = operator only)

**Adhoc mode:** there is no phase evidence — the completion authority is the
`workflow.test_command` gate (`scripts/gsd/gates-test-command.sh`) that gsd-quick's
verify step runs: gates.py evidence, not self-report. A quick task whose test
gate did not run is NOT done.

### Step 6: Finish tail (default; `--no-finish` opts out)

browser gate → openwiki stage → `/review-gate` → ship (grant-walled) → `/canary`. Applies to BOTH
modes — an adhoc fix gets the same review-gate + grant-walled ship as a spec run
(this is where `/fix`'s old inline verify/review steps now live). In order:

1. **Browser gate (fail-closed on web-touch):** `bash scripts/gsd/canary-gate.sh`
   — diffs touching web surfaces require a fresh headless Canary session whose
   `results.json` shows `status=="passed"`, `consoleErrors==0`,
   `networkFailures==0` (testing-policy §2). Non-web diffs exit 0 (`NOT-NEEDED`).
   If the spec carries browser-proof criteria, `lib/runtime_proof.py verify` too.
2. **QA-coverage second opinion (advisory, cross-vendor):**
   `bash scripts/gsd/qa-coverage-adversary.sh <results.json>` — opposite-CLI model
   lists user-facing flows the QA session missed; triage `MISSED:` lines before ship
   (fix or record as pendings — never silently drop).
3. **OpenWiki ship-stage (conditional — warn+continue, re-ported from v3.21.0
   after being dropped in the v4.0 rewrite):** if the consumer repo keeps
   `openwiki/` at repo root, refresh the affected wiki pages from the run's
   diff BEFORE the ship commit so the wiki lands in the same branch/PR. Repos
   without `openwiki/` skip silently.
   - Affected pages: `git diff --name-only <base>...HEAD` → map changed paths
     to wiki pages exactly as `/openwiki-update` does (spec-index + Reality
     refs); refresh Reality claims + meta stamps for THIS run's changes only.
     The page refresh itself is YOUR (LLM) work in this step — the bash block
     below does NOT author content, it only stages whatever wiki edits exist.
   - Then stage via the block below. **Any failure here warns and continues —
     the wiki stage never blocks PR creation (EDGE-007/008).**

<!-- openwiki-wiring:ship-stage:begin -->
```bash
# warn+continue: wiki staging must NEVER block the ship/PR path
ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
[ -d "$ROOT/openwiki" ] || exit 0   # consumer repo without a wiki: silent no-op
if [ -n "$(git status --porcelain -- "$ROOT/openwiki" 2>/dev/null)" ]; then
  if git add "$ROOT/openwiki" 2>/dev/null; then
    echo "openwiki: staged wiki updates for the ship commit"
  else
    echo "openwiki: stage failed — continuing without wiki update" >&2
  fi
fi
exit 0
```
<!-- openwiki-wiring:ship-stage:end -->

4. `/review-gate` → ship (grant-walled) → `/canary` (post-ship smoke).
5. **Merge execution (only with a `merge:pr` grant):** if a `/land-and-deploy`
   skill is available in this session, use it to execute the granted merge
   (merge → CI/deploy wait → prod verify); else `gh pr merge` directly. EITHER
   path then runs `bash scripts/gsd/assert-merged.sh <pr-number>` as the
   machine backstop — exit 0 (MERGED) required before the merge grant is
   recorded consumed; exit 1 means the PR closed WITHOUT merging (work not
   landed — stop, report). After ship/merge completes: if a `/landing-report`
   skill is available, run it (read-only queue snapshot). Both skill references
   are fail-soft — sessions without them use the bare-`gh` path silently.
6. **Learnings harvest (fail-soft, run-end):** `bash scripts/gsd/learnings-harvest.sh`
   persists this run's `.planning/**/learnings*.jsonl` to gbrain-or-archive and
   prints `<N> harvested`. ALWAYS exits 0 — a broken/unreachable memory backend
   never blocks ship (AC-003). Its harvested count belongs in the Step 7 report.

Consumed grants + artifacts (sha/PR#) go in the report.

### Step 7: Report

Phases executed, verifier verdicts, gap rounds, gate evidence ids, consumed grants,
pendings (for one-command morning resume), files changed. Include the delegation
histogram `models={opus:N,sonnet:N,haiku:N,fable:N,inline-mechanical:N}` (spawns
by pinned model; `inline-mechanical` = host trip-wire drains, target 0) — verify
with `python3 lib/gates.py delegation-audit <session-transcript.jsonl>`.

## Removed in v2.0.0

Ruflo executor (swarm_init/agent_spawn/session_save/memory_search/hooks_model-route),
`RUFLO_REQUIRED` plumbing, `dispatch.py` task parsing (gsd plans replace tasks.md),
DAA cognitive patterns. Model routing = gsd `model_profiles` (per-agent overrides in
`.planning/config.json`). Legacy `specs/NNN/tasks.md` files remain readable history;
new execution goes through gsd plans only.
