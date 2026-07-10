---
name: spec-decompose
description: "Decompose an approved feature spec into an executable gsd-core phase plan: seed .planning/{PROJECT,REQUIREMENTS,ROADMAP}.md from specs/NNN/{spec,plan}.md, then drive /gsd-plan-phase (research → wave-parallel plans → plan-checker). Replaces the ruflo specialist-swarm tasks.md decomposition (v2.0.0, spec 002)."
version: "2.3.0"
allowed-tools:
  - Read
  - Write
  - Glob
  - Bash
  - Skill
---

# spec-decompose — Turn an approved spec into gsd phase plans

## When to invoke

- After `/speckit.plan` writes `specs/NNN/plan.md` (or `/plan-decompose` produced one)
- "decompose spec NNN", "generate tasks for NNN", "break this into tasks"
- Before `/feature-implement` — a seeded, plan-checked `.planning/` project is its input

## Workflow

### Step 1: Resolve target spec

```bash
SPEC_ARG="${ARGUMENTS:-}"
if [ -z "$SPEC_ARG" ]; then
  SPEC_ARG=$(git branch --show-current 2>/dev/null | grep -oE '^[0-9]{3}' | head -1)
fi
[ -z "$SPEC_ARG" ] && { echo "ERROR: no spec ID. Usage: /spec-decompose NNN"; exit 1; }
SPEC_DIR=$(find specs -maxdepth 1 -type d -name "${SPEC_ARG%%-*}-*" 2>/dev/null | head -1)
[ -z "$SPEC_DIR" ] && { echo "ERROR: specs/${SPEC_ARG}-* not found"; exit 1; }
[ -f "$SPEC_DIR/plan.md" ] || { echo "ERROR: $SPEC_DIR/plan.md missing — run /speckit.plan"; exit 1; }
```

### Step 2: Seed the gsd project (idempotent)

If `.planning/PROJECT.md` already exists for this feature, skip to Step 3.
Otherwise translate the FFS spec into gsd's planning inputs:

- `.planning/PROJECT.md` — what/why from `spec.md` intro + repo context + constraints
  (worktree discipline, scoped `git add`, never push, shellcheck/bats/pytest conventions).
  Include a **Testing Policy** constraints block referencing the `testing-policy` skill:
  mock-minimization ladder (boundary-only mocks, never first-party module mocks),
  real-browser over jsdom for UI truth, independent test authorship, coverage floor 80%.
- `.planning/REQUIREMENTS.md` — one `REQ-NN` per user story / acceptance criterion
  from `spec.md` (verbatim ACs; testable phrasing)
- `.planning/ROADMAP.md` — phases from `plan.md`'s phase breakdown; each phase lists
  its REQ ids + literal success criteria (commands that must exit 0)
- `.planning/config.json` — copy `templates/gsd-config.base.json`; MUST carry
  `workflow.test_command = bash scripts/gsd/gates-test-command.sh` and
  `workflow.code_review_command = bash scripts/gsd/review-gate-command.sh`
- Then run `bash scripts/gsd/model-fallback.sh .planning` — probes premium-model
  availability (claude-fable-5 on OAuth comes and goes) and rewrites unavailable
  pins to their fallback (fable→opus) so spawns never error on a dead model.
- Then run `bash scripts/gsd/security-model-fence.sh .planning specs/NNN-*/spec.md specs/NNN-*/plan.md`
  — security-touching specs (auth/RLS/payments/crypto/…) force the planning
  roles fable→opus even when fable is available: Fable's classifiers can
  false-refuse benign defensive-security work and stall the run silently.
- Write `.planning/gsd-test-command` — ONE shell line running the repo's real
  test suite for this spec (e.g. `bash scripts/tests/specNNN-*.test.sh && bats
  tests/bats/specNNN-*.bats`). `gates-test-command.sh` refuses to run without
  it in repos lacking `lib/tests` — vacuous evidence is worse than no evidence.

Pilot-proven reference shapes: spec 002's `.planning/` on branch `002-gsd-replaces-ruflo`.

### Step 3: Drive plan-phase

- Interactive: invoke `/gsd-plan-phase <N>` (first unchecked ROADMAP phase).
- Headless: `TIMEOUT=3600 bash scripts/gsd/gsd-run.sh /gsd-plan-phase <N>`.

gsd runs research → writes `NN-*-PLAN.md` files (wave/`depends_on` annotations are the
parallelism contract `/gsd-execute-phase` executes) → plan-checker verifies. The seeded
config also wires the plan-bounce seam (`workflow.plan_bounce_script =
scripts/gsd/plan-adversary.sh`): high-blast plans (auth/RLS/payments/migrations/…)
get a cross-model adversarial review (default `gpt-5.6-sol` @ `xhigh`, same adversary
tier as review-gate) appended as findings, which the opus plan-checker re-run
adjudicates. Low-blast plans skip it (zero cost); kill-switch `PLAN_ADVERSARY=off`.
Plans with
UI-touchable stories must carry browser-proof success criteria — inject into ROADMAP
criteria at seed time as literal gate commands: `bash scripts/gsd/canary-gate.sh`
(fail-closed headless browser QA; testing-policy §2) and, where the spec declares
proof scenarios, `python3 lib/runtime_proof.py verify`. BDD scenarios from spec.md
are the Canary step sources (testing-policy §4).

### Step 4: Coherence gate

`python3 lib/gates.py analyze` against the seeded project where applicable; ERROR on
incoherent handoffs. Then report: phases, plans per phase, wave widths, REQ coverage
(every REQ appears in exactly one phase), gate commands present in config.

### Step 5: Next step

```
Next: /feature-implement NNN [--autonomous]
Phase completion authority: gates.py (test_command + gsd-phase-evidence-gate hook).
```

## Removed in v2.0.0

Ruflo swarm decomposition (swarm_init + per-domain specialist fan-out), agent roster
(`agents.json` / `agents_manifest.py`) task tagging, `[model:X]`/`[agent:Y]` task
grammar, tasks.md output. gsd plans carry executor/model assignment natively
(`model_profiles` in config); wave annotations replace `[P]` markers.
