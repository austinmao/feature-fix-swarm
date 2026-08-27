---
name: feature-implement
description: "Execute a decomposed feature via the gsd-core loop with preflight-only host fallback, no stateful cross-vendor replay, autonomy grants, gates.py completion authority, and a fail-closed review/ship tail. --adhoc uses the same walls over gsd-quick."
version: "2.15.0"
allowed-tools:
  - Read
  - Edit
  - Bash
  - Glob
  - Skill
---

# feature-implement — Execute a feature through the gsd loop

## Host dispatch contract

- Codex: invoke skills as `$skill`; use Codex collaboration roles and GPT-5.6 model tiers.
- Claude: invoke skills as `/skill`; use Claude Agent/Skill tools and Claude model aliases.
- Examples that name both hosts are routing contracts. Never send one host's command syntax to the other.
- A bare `/skill` in this shared source denotes the Claude form; Codex dispatches the same named skill as `$skill`.

## Init gate

Before any other step, run the advisory init guard and relay its output
verbatim:

```bash
bash "$(git rev-parse --show-toplevel)/scripts/gsd/init-guard.sh" || true
```

If it printed `INIT-GUARD:` warnings, offer `/ffs-init` before proceeding in
interactive sessions (declining proceeds anyway); headless, spawned, and
autonomous runs relay the warnings once and continue. Advisory only — never
a block, never an exit-code change.

At entry, make one opportunistic, fail-soft `bash scripts/gsd/reconcile.sh` pass; never block on its result.

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

### Step 1.5: Cross-session claim (spec-009 — claim-or-stop)

Exactly one session may implement a run id. Autonomous/headless runs SKIP the
skill-level claim: `gsd-run.sh` takes, renews, and releases the claim itself
(P-22..P-24b) once `GSD_RUN_ID` is exported in Step 2 — a second claim here
would collide with the runner's own and refuse the launch.

```bash
COORD_PY="$(git rev-parse --show-toplevel 2>/dev/null)/scripts/coord/coord.py"
if [ "$AUTONOMOUS" != "1" ] && [ -f "$COORD_PY" ]; then
  # 4h TTL: an interactive session has no heartbeat daemon, so the TTL must
  # outlive a working session leg (review-gate round 2 HIGH — a 300s default
  # expires mid-run and re-opens the two-writer window). Belt: re-claim
  # (idempotent, refreshes the clock) at every step boundary below. Backstop:
  # anchor-pid staleness reclaims instantly if this session dies.
  FFS_RUN_ID="$RUN_ID" FFS_COORD_ANCHOR_PID=$PPID python3 "$COORD_PY" claim "$RUN_ID" --ttl 14400 --heartbeat 3600
  _claim_rc=$?
  case $_claim_rc in
    0) ;;  # claimed — see capture note below
    3|4) echo "[feature-implement] STOP: '$RUN_ID' is held by another live session — do not implement it here. Inspect: python3 \"$COORD_PY\" status"; exit $_claim_rc ;;
    *) echo "[feature-implement] WARN: coord store unavailable (rc=$_claim_rc) — proceeding UNCLAIMED; diagnose with: python3 \"$COORD_PY\" doctor" ;;
  esac
fi
```

On success the claim prints `session=<uuid>` and `CLAIM-OK generation=<N>`.
**Capture both**: every later coord call for this run (renew, release) must
carry `FFS_COORD_SESSION=<that uuid>` and `--generation <that N>` — a call
without them is a foreign contender, not the holder. The claim is released in
Step 7 (and on ANY stop/abort path before it); missed releases expire by TTL +
anchor-pid staleness, so a crashed session never wedges the run id.

**Renewal discipline (interactive runs):** at every step boundary that
follows (each phase start in Step 4, each gap round, the Step 6 finish tail),
re-run the SAME claim command with `FFS_COORD_SESSION=<captured uuid>` —
re-claim by the holder is idempotent and refreshes the TTL clock. If a
re-claim ever returns 3/4 (superseded after an expiry), STOP: another session
may hold the run now; inspect `coord.py status` before touching anything.

Consumer repos without `scripts/coord/coord.py` skip silently (fail-soft) —
coordination is an FFS-repo capability until the coord CLI ships in setup.sh.

### Step 2: Walls (fail-closed, --autonomous only)

```bash
if [ "$AUTONOMOUS" = "1" ]; then
  python3 "$GATES_PY" check-preflight "$RUN_ID" || {
    echo "[feature-implement] ERROR: no fresh preflight for $RUN_ID — run /preflight first."; exit 1; }
  # TAKEOVER-WALL-START: tests extract this exact autonomous seam.
  TAKEOVER_WALL="$(git rev-parse --show-toplevel)/scripts/gsd/takeover-check.sh"
  TAKEOVER_VERDICT="$(bash "$TAKEOVER_WALL" --run-id "$RUN_ID")" || {
    printf '%s\n' "$TAKEOVER_VERDICT"; exit 1; }
  printf '%s\n' "$TAKEOVER_VERDICT"
  # TAKEOVER-WALL-END
fi
# Legacy Claude config wall: rewrite unavailable non-exact Fable pins to Opus.
# Typed exact requests fail closed and never use this compatibility fallback.
[ -f scripts/gsd/model-fallback.sh ] && bash scripts/gsd/model-fallback.sh .planning || true
# Legacy Claude security fence: security-touching configs use Opus judgment.
[ -f scripts/gsd/security-model-fence.sh ] && bash scripts/gsd/security-model-fence.sh .planning specs/"$SPEC_ID"*/spec.md specs/"$SPEC_ID"*/plan.md || true
# Ledger key for gsd seams: review-gate-command.sh reads GSD_RUN_ID (no
# hardcoded default) — export it so ship grants key to THIS run.
export GSD_RUN_ID="$RUN_ID"
# Seam 1 (REQ-401/402, sig 16ac087d reading): "zero call-site edits" pins
# gates.py INTERNALS and PRE-EXISTING consumers — review-gate-command.sh
# gains no flag and inherits hard mode through gates.py's env-var read
# (lib/gates.py:2941-2945); INT-004 proves it stays byte-unchanged. This
# skill's OWN prod call sites are the exception: they pass hard mode
# explicitly (see the check-grant note below). The absent-registry advisory
# needs no code here — gates.py prints it on the first prod check-grant
# (soft mode: one stderr line, byte-identical exit codes, REQ-402).
if [ "$AUTONOMOUS" = "1" ]; then
  # Ledger-first prod detection (RESEARCH OQ1): any grant of THIS run
  # matching gates.py PROD_ACTION_PREFIXES; fallback = plan grep; both
  # miss → no export (fail-soft — phase-3 deploy-prod templates carry the
  # per-callsite backstop).
  _FFS_PROD=$(python3 - "$RUN_ID" <<'DETECT'
import sys
sys.path.insert(0, "lib")
import gates
data = gates._load_store(gates._store_path())
grants = data.get("_autonomy", {}).get(sys.argv[1], {}).get("grants", {})
print("1" if any(a.startswith(gates.PROD_ACTION_PREFIXES)
                 for a in grants if isinstance(a, str)) else "0")
DETECT
  ) || _FFS_PROD=0
  if [ "$_FFS_PROD" != "1" ] && \
     grep -qE 'deploy:prod-|flip:prod-|migrate:prod-' specs/"$SPEC_ID"*/plan.md 2>/dev/null; then
    _FFS_PROD=1
  fi
  # Belt-and-braces for subprocesses only (sig ba1efa84): no assertion
  # depends on this export surviving block boundaries — the skill's prod
  # call sites carry the explicit flag either way.
  [ "$_FFS_PROD" = "1" ] && export FFS_ENV_REGISTRY_REQUIRED=1
fi
```

At every operator-gated action mid-run (push, merge, deploy, flip, secret-use):
`check-grant "$RUN_ID" --action "<type:target>"` — for PROD actions
(`deploy:prod-*` / `flip:prod-*` / `migrate:prod-*`) under `--autonomous` the
call is `check-grant "$RUN_ID" --action "<type:prod-target>" --require-environments`
(sig ba1efa84: deterministic per-call hard mode at this skill's own call
site) — proceed on exit 0; otherwise
`pending` + STOP that action path only. Never bypass with prose; never re-ask a
granted action. (Ship itself is walled inside gsd's `code_review_command` —
`scripts/gsd/review-gate-command.sh` REVISEs without a `ship:gsd` grant.)

For a cold handoff, `scripts/gsd/takeover-check.sh --list` shows only the
discoverable record metadata. Stored resume text is display-only and is never
executed by this skill or the wall.

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
- `--autonomous` / headless on either host: `TIMEOUT=1800 bash scripts/gsd/gsd-run.sh /gsd-quick "<task>"` (the runner prefers the invoking host, may select the alternate before launch, and never replays after launch).

The headless runner performs a fixed read-only CLI/model/quota probe before it
starts the stateful drive. It prefers the invoking host and may select the other
host only at that pre-launch boundary. Once `/gsd-quick` starts, a nonzero exit
or timeout is returned with a resume command for that host; FFS never mines task
output for phrases such as “API error” and never replays a partially-started
drive across vendors.

**Spec mode:** read `.planning/ROADMAP.md` for the first unchecked phase N.

Once, immediately before entering the phase loop (NOT at run-start
bootstrap): if `.planning/ceremony-tier` exists and starts with `light`,
run the run-level wall over every already-planned phase in one invocation:

```bash
[ -f .planning/ceremony-tier ] && grep -q '^light' .planning/ceremony-tier && bash scripts/gsd/plan-wall.sh --run
```

STOP on nonzero. This pays ONE global `wall:run` round for the whole run;
per-phase re-entry at the runner's own wall seam then takes the
sha-unchanged zero-dispatch idempotence path (an edited plan still
re-dispatches). Tiers `full`/absent change nothing — the per-phase wall
below is the unchanged default.

Before a dry run or any interactive/headless execution, run:

```bash
bash scripts/gsd/requirement-ownership-gate.sh "$N"
```

STOP on nonzero. This is the fail-fast wall for gsd-core's behavior of marking
every requirement in an executed PLAN complete: every ROADMAP Phase N ID must
appear in exactly one Phase N PLAN, on the last plan that genuinely completes
it. Preparatory/enabling plans use an explicit `requirements: []`. Rechecking
an earlier invariant belongs in `must_haves`, not repeated requirement ownership.
The headless runner repeats this wall before its model probe so direct runner
calls cannot bypass it.

Immediately after the ownership gate clears, run the per-phase blocking plan
review wall (spec-004 AC-005 — every `*-PLAN.md` / bare `PLAN.md` under phase
N must clear a fresh adversarial review before execution starts):

```bash
PHASE_DIR="$(ls -d .planning/phases/*-* 2>/dev/null | grep -E "^\.planning/phases/0*${N}-" | head -1)"
[ -n "$PHASE_DIR" ] && export GSD_PHASE_ID="$(basename "$PHASE_DIR")" && bash scripts/gsd/plan-wall.sh "$PHASE_DIR"
```

STOP on nonzero. `PLAN_WALL=off` skips ONLY with a durable waiver record
(AC-008) — a skip that cannot write its waiver record fails closed. The
headless runner (`gsd-run.sh`) invokes the SAME lever again at its own
pre-execution seam (beside `requirement-ownership-gate.sh`), so a direct
runner call cannot bypass this wall either.

The wall is ONE round (policy (b) 2026-08-27): round 1 is the review.
HIGH-only findings pass immediately as `PASS-RESIDUAL` (they ride into
execution as pinned assumptions, closed at the executed-diff review); an
unresolved CRITICAL blocks (rc 1) and buys exactly one repair round.

Exit 3 = `WALL-ROUND-CAP` (durable per-phase round cap, default
`PLAN_WALL_MAX_ROUNDS=2` — review + one CRITICAL-repair round): this is
TERMINAL for the phase, not a retryable BLOCKED — do NOT start another fix
round. Quarantine the phase (record the open findings + the printed unblock
commands in the run report / pendings) and move to other runnable work; the
cap exists because uncounted wall→fix→wall loops have burned 19 rounds/2
days and 38+ turns/a week of vendor quota. Only an operator-reviewed
findings resolution (or a deliberate `PLAN_WALL_MAX_ROUNDS` raise) reopens
it.

### Fix-round mutation contract

Every fix round between a wall BLOCKED verdict and the next wall invocation
must mutate the plan/task body directly — rewrite or delete the defective
text the finding points at. Never leave the original text standing and
append a correction block after it: the wall's reviewer dispatch re-reads
the WHOLE plan file each round, so appended-but-not-removed defective prose
gets re-noticed and re-reported. A re-report of a previously RESOLVED
finding is a REOPEN — a reopened CRITICAL blocks again and there is only
one repair round before `WALL-ROUND-CAP`. The fix IS the edit to the plan;
`findings-queue resolve` records the adjudication, it does not substitute
for one.

### Autonomous rc-3 bounded auto-continue

`--autonomous` runs get exactly ONE bounded, grant-gated retry before a
quarantine becomes terminal — `gsd-run.sh` enforces this in code
(`_gsd_run_wall_gate`; it is the runner behind every `--autonomous`
invocation, and no agent turn exists between its two wall calls for an
interactive prose recipe to govern). On exit 3, before quarantine:

1. Every plan under the phase must have zero unresolved CRITICAL wall
   findings (`findings-queue list --unresolved --source wall --severity
   CRITICAL --plan <plan>`). Residual HIGHs do NOT count — under the
   one-round wall they are open on every pass-residual phase by design.
   Any unresolved CRITICAL anywhere in the phase → quarantine (unchanged).
2. Zero unresolved AND `gates.py check-grant "$RUN_ID" --action
   wall-reset:<phase-slug>` passes AND the durable per-phase budget
   (`gates.py loop-round "$RUN_ID" wall-autoreset:<phase-slug> --max
   ${PLAN_WALL_AUTO_RESET_MAX:-1}`) is not capped → proceed; either check
   failing → quarantine (unchanged).
3. `gates.py loop-round "$RUN_ID" wall:<phase-slug> --reset --max 1`, then
   re-run the wall on the phase exactly once.
4. ANY nonzero exit from that re-run — a second rc 3 OR a plain rc 1
   BLOCKED — is quarantine terminal. Post-reset the wall restarts at round
   1 of its one-round policy: a pass (including `PASS-RESIDUAL`) clears; a
   fresh CRITICAL is terminal. The `wall-autoreset:<phase-slug>` budget is spent regardless of which
   exit code comes back — at the default `PLAN_WALL_AUTO_RESET_MAX=1` this
   recipe never runs twice for the same phase in the same run; raising the
   knob is a deliberate, visible escape mirroring `PLAN_WALL_MAX_ROUNDS`,
   never a silent off-switch.

Interactive sessions never mint a `wall-reset` grant, so the gate falls
through to the unchanged terminal quarantine there — exit 3 stays the stop
described above.

## Wall await rule

Keep the plan wall in the foreground for phases with one or two plans. A
phase that may exceed the 600-second tool ceiling MAY background the wall,
but must then poll `plan-wall.sh --await 300` in the same turn for no more
than `PLAN_WALL_AWAIT_MAX=6` iterations. Its outcomes are actionable: rc 0
means every record passed, rc 20 means the wall decided blocked, rc 75
means it is still pending, and rc 76 (`WALL-AWAIT:attempts-exhausted`) means
the script-enforced pending budget for this phase is spent — stop polling and
checkpoint. The budget is enforced by `plan-wall.sh` itself (run-scoped
counter, reset on any decided outcome), not by agent self-restraint. If the
turn cannot outlast a pending wall, it MUST checkpoint a
`waiting(wall-decided)` lifecycle record with `lifecycle.sh` before ending.

- `--dry-run`: print phase N, its plans, the two config gate commands, wall status; exit 0.
- Interactive Claude session: invoke `/gsd-execute-phase N` directly.
- Interactive Codex session: invoke `$gsd-execute-phase N` directly.
- `--autonomous` / headless on either host: `TIMEOUT=3600 bash scripts/gsd/gsd-run.sh /gsd-execute-phase N` (the runner prefers the invoking host, may select the alternate before launch, and never replays after launch; Claude gets trimmed MCP/auth scrubbing, Codex gets its native skill/model surface).

The same pre-launch-only selection applies in spec mode. Cross-vendor replay
after the stateful boundary is forbidden because a phase may already have
changed the worktree, evidence ledger, or `.planning` resume state.

**Codex yielded-session contract:** `Script running with cell ID ...` is
neither success nor failure. Wait on that cell; if the result then carries a
`session_id` without an `exit_code`, poll that exact child with `write_stdin`
until it exits. Never relaunch the gate, test, build, or deploy while its
original session is alive. The headless runner injects this rule into Codex
GSD drives because confusing the outer cell with the child PTY duplicates
stateful work and can strand disposable resources. The runner also publishes
`.planning/run-state/gsd-run.pid`, a heartbeat, and an atomic status file;
`kill -0 $(head -1 .planning/run-state/gsd-run.pid)` is the fallback liveness
probe if the tool session itself is lost. A second runner refuses to launch
while that pid is live, so single-flight is mechanical rather than prompt-only.

**Anti-early-stop (autonomous orchestrator loop).** Legacy Claude Fable can stop long runs
with text-only intent; hold this line every turn of the drive loop:

> Before ending your turn, check your last paragraph. If it is a plan, an
> analysis, a question, a list of next steps, or a promise about work you have
> not done ("I'll…"), do that work now with tool calls. End your turn only when
> the task is complete or you are blocked on input only the user can provide.
> You have ample context remaining — do not stop, summarize, or suggest a new
> session on account of context limits; the harness handles compaction.

**Legacy Claude Fable dispatch discipline.** A Claude Fable orchestrator
dispatches parallel subagents readily — lean into it, don't
serialize:

> Delegate independent subtasks to subagents in parallel and keep working while
> they run — do not block on each return. Intervene only if a subagent goes off
> track or is missing context. Serial gates (plan → check → bounce) stay
> serial; everything independent (research fan-out, per-file verify, doc
> passes) runs concurrently.

Claude Fable's instruction-following is strong enough that these two short guards
replace enumerated behavior lists — do not grow them into checklists.

**Dispatch-budget refresh is a typed grant, never plan surgery (D10,
2026-08-27).** Wherever a per-round dispatch/executor budget applies (a
consumer repo's run policy, a gsd segment cap), an EXHAUSTED budget is
refreshed only by a typed grant in the run's autonomy ledger:
`gates.py grant "$RUN_ID" --action "dispatch-budget:<spec>:<phase>"
--rollback "<how to unwind>"`, checked beside the budget with
`gates.py check-grant "$RUN_ID" --action "dispatch-budget:<spec>:<phase>"`,
mirroring `wall-reset:<slug>`. The grant's justification names the NEW root
cause that earned more dispatches. NEVER amend a plan to reset a budget:
spec-381 burned two full rounds on plan surgery whose only purpose was
minting fresh budget, and the amendment triggered implicit re-review on top.
Budgets are defect-scoped, not round-scoped.

(Orchestrator-level mitigation: per-turn work inside gsd-core sub-agents is
gsd-core's to guard — deeper coverage needs a gsd-core change, out of scope.)

On a verifier FAIL, ADJUDICATE the reported gap set first — for each gap:
fix it, refute it (with evidence), or waive it (recorded residual). Never
dispatch a fresh verifier for a second opinion on the same state: verifier
runs against unchanged code return disjoint blocker sets (spec-006: 4 runs,
4 different sets), so a re-run is noise, not signal. Re-verify only AFTER
fixes land, max 1 re-verify per phase; then `/gsd-plan-phase N --gaps` +
`/gsd-execute-phase N --gaps-only` (same runner), max 2 gap rounds, then
STOP and report.

### Package-legitimacy pre-install gate (v2.10.0)

Fires inside Step 4, before ANY task runs a package install (`npm|pnpm|yarn
install/add`, `pip install`, `cargo add`) of a package **not already** in the
repo's manifest (`package.json` deps, `requirements*.txt`, `Cargo.toml`) and
not explicitly named by the operator. Threat: slopsquatting — LLM-hallucinated
package names that squatters pre-register; `npm view` success proves
*registration*, not *legitimacy*. An agent-discovered package is `[ASSUMED]`
until cleared.

Per new package:

1. **Registry existence** (registration only, ecosystem-specific):
   `npm view <pkg> version` (Node) · `pip index versions <pkg>` (Python) ·
   `cargo search <pkg>` (Rust). Absent from the registry → hallucination →
   BLOCK, surface the task as a checkpoint, never silently substitute a
   similar name.
2. **slopcheck verdict (if installed)** — `slopcheck install <pkg> --json` →
   `OK | SUS | SLOP`. `slopcheck` is an optional external tool; if the binary
   is absent, skip this step and leave the package `[ASSUMED]` — never
   hard-fail the loop on a missing optional dependency.
3. **Disposition:**
   - `SLOP` → hard block, always (even `--autonomous`). Never installable by
     the loop.
   - `[ASSUMED]` or `SUS` → operator checkpoint through the grant ledger
     (same mechanism as the Step 2 mid-run gates):
     ```bash
     python3 "$GATES_PY" check-grant "$RUN_ID" --action "install:<pkg>" \
       && npm install "<pkg>" \
       || { python3 "$GATES_PY" pending "$RUN_ID" --action "install:<pkg>" \
              --reason "unverified agent-discovered package"; }  # STOP that install path only
     ```
     Interactive (non-autonomous) mode: show the operator the registry +
     slopcheck evidence and ask before installing, instead of consulting the
     ledger.
   - `OK` and (already-in-manifest or operator-named) → install normally, no
     gate.

Enumerate expected installs in `/autonomy-grant`'s gate list up front (type
`install`, e.g. `install:left-pad`) the same as `push`/`deploy`/`merge` — an
unlisted install still stops on `check-grant`.

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

**Scope-drift gate (advisory, once per phase wall — NEVER per turn):** alongside
verify-done, run
`bash scripts/gsd/scope-drift-gate.sh --judge --plan <this phase's .planning/phases/<phase>/*-PLAN.md files>`
— deterministic diff-vs-`files_modified` classification + `PHASE GOAL:` re-anchor,
plus ONE bounded cross-vendor judge verdict (`DRIFT-VERDICT: ON-TRACK|DRIFT`).
A DRIFT warning is advisory: re-read the spec/plan success criteria before
starting the next phase, and record the correction in the phase SUMMARY.
Kill-switch `GSD_DRIFT_GATE=off`; judge seam `GSD_DRIFT_JUDGE_CMD`.

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

4. `/review-gate` → ship (grant-walled) → `/canary` (post-ship smoke). After
   ship, run `bash scripts/gsd/promote-emit.sh "$RUN_ID"` — it EMITS (never
   executes) the staging→prod `gates.py promote` command for the operator/CI
   deploy workflow when this run consumed a `deploy:staging-*` grant; silent
   otherwise (REQ-401 Seam 3 — deliberately NOT run-finalizer.sh, whose
   always-exit-0 post-merge contract would swallow the signal).
5. **Merge execution (only with a `merge:pr` grant):** if a `/land-and-deploy`
   skill is available in this session, use it to execute the granted merge
   (merge → CI/deploy wait → prod verify); else `gh pr merge` directly. EITHER
   path then runs `bash scripts/gsd/assert-merged.sh <pr-number>` as the
   machine backstop — exit 0 (MERGED) required before the merge grant is
   recorded consumed; exit 1 means the PR closed WITHOUT merging (work not
   landed — stop, report). After ship/merge completes: if a `/landing-report`
   skill is available, run it (read-only queue snapshot). Both skill references
   are fail-soft — sessions without them use the bare-`gh` path silently.
   **Run-end finalizer (after assert-merged exits 0):**
   `bash scripts/gsd/run-finalizer.sh --archive-planning <pr-number>` — removes the run's clean
   worktree (dirty → routed to `/adopt-wip`, never deleted), deletes the landed
   feature branch local+remote (squash-safe: only under merged-`headRefOid`
   proof, never a blind force-delete), prunes `gsd/phase-*` branches that are
   ancestors of the merged head, clears `.planning/run-state/`, and
   (`--archive-planning`, D8) sweeps the landed run's phase dirs to
   `.planning/archive/<run-id>/` so a later run's merge never aborts on the
   stale untracked files (spec-388: 11-file untracked-overwrite abort).
   Fail-soft,
   ALWAYS exits 0 — cleanup failure never un-merges or blocks the report.
   Kill-switch `FFS_RUN_FINALIZER=off`.
   CI re-proves the levers this step names actually exist (verify block,
   executed by `scripts/verify-skill-blocks.py`):

   ```bash verify
   test -f "$REPO_ROOT/scripts/gsd/run-finalizer.sh"
   test -f "$REPO_ROOT/scripts/gsd/assert-merged.sh"
   test -f "$REPO_ROOT/scripts/gsd/model-fallback.sh"
   test -f "$REPO_ROOT/scripts/gsd/fallback-rehearsal.sh"
   test -f "$REPO_ROOT/scripts/coord/coord.py"
   test -f "$REPO_ROOT/scripts/gsd/promote-emit.sh"
   ```

6. **Learnings harvest (fail-soft, run-end):** `bash scripts/gsd/learnings-harvest.sh`
   persists this run's `.planning/**/learnings*.jsonl` to gbrain-or-archive and
   prints `<N> harvested`. ALWAYS exits 0 — a broken/unreachable memory backend
   never blocks ship (AC-003). Its harvested count belongs in the Step 7 report.

Consumed grants + artifacts (sha/PR#) go in the report.

### Step 7: Release claim + report

Interactive claims only (the headless runner releases its own on every exit
path): release the Step 1.5 claim FIRST, so a stop mid-report never strands it:

```bash
[ "$AUTONOMOUS" = "1" ] || [ ! -f "$COORD_PY" ] || \
  FFS_RUN_ID="$RUN_ID" FFS_COORD_ANCHOR_PID=$PPID FFS_COORD_SESSION="<uuid captured at claim>" \
  python3 "$COORD_PY" release "$RUN_ID" --generation "<generation captured at claim>" || true
```

Phases executed, verifier verdicts, gap rounds, gate evidence ids, consumed grants,
pendings (for one-command morning resume), files changed. When
`specs/NNN/spec.md` carries a `## Scope ledger` with a source doc, ALWAYS
end the report with the coverage line (D7):
`DESIGN-DOC COVERAGE: N of M slices consumed; unconsumed: [list]; next:
/feature-spec "slice N: …"` — a run that completed slices 0–1 of a 7-slice
design doc must say so louder than "complete". Include the delegation
histogram `tiers={frontier:N,judgment:N,execution:N,volume:N,exact:N,inline-mechanical:N}`
(spawns by typed request and resolved model; `inline-mechanical` = host
trip-wire drains, target 0) — verify
with `python3 lib/gates.py delegation-audit <session-transcript.jsonl>`.

## Removed in v2.0.0

Ruflo executor (swarm_init/agent_spawn/session_save/memory_search/hooks_model-route),
`RUFLO_REQUIRED` plumbing, `dispatch.py` task parsing (gsd plans replace tasks.md),
DAA cognitive patterns. Model routing = gsd `model_profiles` (per-agent overrides in
`.planning/config.json`). Legacy `specs/NNN/tasks.md` files remain readable history;
new execution goes through gsd plans only.
