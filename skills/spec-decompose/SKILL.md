---
name: spec-decompose
description: "Decompose an approved feature spec into an executable gsd-core phase plan: seed .planning/{PROJECT,REQUIREMENTS,ROADMAP}.md from specs/NNN/{spec,plan}.md, then drive /gsd-plan-phase (research → wave-parallel plans → plan-checker). Replaces the ruflo specialist-swarm tasks.md decomposition (v2.0.0, spec 002)."
version: "2.6.0"
allowed-tools:
  - Read
  - Write
  - Glob
  - Bash
  - Skill
---

# spec-decompose — Turn an approved spec into gsd phase plans

## Host dispatch contract

- Codex: `$skill`, Codex collaboration roles, and GPT-5.6 tiers.
- Claude: `/skill`, Agent/Skill tools, and Claude aliases.
- A bare `/skill` in this shared source denotes the Claude form; Codex dispatches the same named skill as `$skill`.

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
- Then run `bash scripts/gsd/model-fallback.sh .planning` only for legacy
  Claude alias configs. Typed tier requests use the bounded runtime fallback;
  an exact Claude Fable request fails closed and is never rewritten.
- Then run `bash scripts/gsd/security-model-fence.sh .planning specs/NNN-*/spec.md specs/NNN-*/plan.md`
  — security-touching specs (auth/RLS/payments/crypto/…) force the planning
  legacy Claude planning aliases to Opus judgment. Typed configs already map
  security planning to the judgment tier.
- Write `.planning/gsd-test-command` — ONE shell line running the repo's real
  test suite for this spec (e.g. `bash scripts/tests/specNNN-*.test.sh && bats
  tests/bats/specNNN-*.bats`). `gates-test-command.sh` refuses to run without
  it in repos lacking `lib/tests` — vacuous evidence is worse than no evidence.

Pilot-proven reference shapes: spec 002's `.planning/` on branch `002-gsd-replaces-ruflo`.

### Step 2.5: Spec-completeness gate (edge-probe)

`gsd-plan-checker` (Step 3) verifies a plan against the requirements that got
written down — a data/behavior-shape edge the spec never surfaced is invisible
to it, and it will be confidently silent about the omission. Close that gap
before plan-phase, not after: walk every `REQ-NN` just written into
`.planning/REQUIREMENTS.md` through a closed 8-category taxonomy and force each
applicable edge to a resolution.

For each REQ, first classify its data/behavior shape, then raise only the
categories whose shapes intersect (relevance filter — a pure-text requirement
is never asked about overflow):

| category | applies to shape | probe question |
|---|---|---|
| boundary | numeric-range | value exactly at each min/max/threshold — and one step either side? |
| adjacency | collection | when two things are exactly equal or just touch — merge, collide, or separate? |
| empty | collection, text | result for empty / single-element / null input? |
| encoding | text | bytes, code points, grapheme clusters, or normalized form? |
| ordering | collection | when elements compare equal, is output order specified and stable? |
| precision | numeric-range | where can precision loss / overflow / rounding occur — exact contract (half-up vs half-even, ceil/floor/truncate)? |
| idempotency | stateful | what happens if this runs twice on the same input? |
| concurrency | stateful, io | if interrupted or run in parallel, what is guaranteed? |

Resolve each raised edge to one **status**: `resolved` (a checkable AC now
exists — fold it into `REQUIREMENTS.md` or a ROADMAP phase's success criteria),
`dismissed` (REQUIRES a non-empty reason, e.g. "N/A — bounded enum, no boundary
exists"; silence is not a dismissal), or `unresolved` (carried forward,
flagged).

Write `${SPEC_DIR}/edge-coverage.md` — one line per edge: `REQ · category ·
status · reason/AC`. **Soft gate**: any `unresolved` applicable edge → WARN and
surface it before Step 3 runs; the operator resolves or dismisses it, or it
rides into the plan as an explicit assumption `gsd-plan-checker` can then judge
on its own terms — never silently dropped.

### Step 3: Drive plan-phase

- Interactive: invoke `/gsd-plan-phase <N>` (first unchecked ROADMAP phase).
- Interactive Claude: `/gsd-plan-phase <N>`; interactive Codex: `$gsd-plan-phase <N>`.
- Headless on either host: `TIMEOUT=3600 bash scripts/gsd/gsd-run.sh /gsd-plan-phase <N>` (the runner prefers the invoking host, may select the alternate before launch, and never replays after launch).

gsd runs research → writes `NN-*-PLAN.md` files (wave/`depends_on` annotations are the
parallelism contract `/gsd-execute-phase` executes) → plan-checker verifies. The seeded
config also wires the plan-bounce seam (`workflow.plan_bounce_script =
scripts/gsd/plan-adversary.sh`): high-blast plans (auth/RLS/payments/migrations/…)
get a cross-model adversarial review (default `gpt-5.6-sol` @ `xhigh`, same adversary
tier as review-gate) appended as findings, which the judgment plan-checker re-run
adjudicates. Low-blast plans skip it (zero cost); kill-switch `PLAN_ADVERSARY=off`.
Plans with
UI-touchable stories must carry browser-proof success criteria — inject into ROADMAP
criteria at seed time as literal gate commands: `bash scripts/gsd/canary-gate.sh`
(fail-closed headless browser QA; testing-policy §2) and, where the spec declares
proof scenarios, `python3 lib/runtime_proof.py verify`. BDD scenarios from spec.md
are the Canary step sources (testing-policy §4).

Plan requirement ownership is completion ownership, not traceability repetition:
each requirement in a ROADMAP phase belongs in exactly one PLAN. Put it on the
last plan that genuinely completes it. Preparatory or enabling plans use an explicit
`requirements: []`; later plans recheck earlier invariants through `must_haves`
without repeating the requirement ID. This intentionally overrides gsd-core's
stale template prose that says PLAN requirements cannot be empty—its executor
otherwise marks repeated IDs complete after the first plan.

### Step 4: Coherence gate

`python3 lib/gates.py analyze` against the seeded project where applicable; ERROR on
incoherent handoffs. For every planned phase N, run
`bash scripts/gsd/requirement-ownership-gate.sh N` once; ERROR immediately on
nonzero with its bounded diagnostics—do not enter an open-ended repair loop.
Then report: phases, plans per phase, wave widths, REQ coverage (every REQ appears
in exactly one phase and one genuinely-completing plan), gate commands present
in config.

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
