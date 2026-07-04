# Browser-proof QA — evidence-backed runthroughs + design review per phase

v3.20.0. The failure this kills: an agent reports "page works, returns 200,"
the operator opens the browser and finds a 404, a dead button, or a hydration
crash. HTTP status and agent prose are not evidence; recorded browser
artifacts are.

## The contract

Every web-touching phase in `/feature-implement` must produce a verified
**proof bundle** before its QA gate passes:

```
.ralph/<phase>/proof.json          functional runthroughs (BDD scenarios)
.ralph/<phase>/design-proof.json   visual review (UI phases)
```

`python3 lib/runtime_proof.py verify <bundle>` is the completion authority.
Each check defeats a named anti-pattern:

| Check | Anti-pattern it kills |
|---|---|
| `content_assert` + `dom_excerpt` required | curl-200-as-proof (200 can render a 404 page) |
| soft-404 marker scan on `dom_excerpt` | "Not Found"/"Application error" rendered with status 200 |
| `url_final` must contain `expect_url` | screenshot-of-wrong-page (login redirect, stale tab) |
| `console_errors` must be present AND empty | SSR looks fine, hydration throws, page is dead |
| `interactions >= 1` for functional scenarios | static frame passed off as "works" (buttons/auth untested) |
| screenshot exists + non-empty + fresh (mtime) | fabricated or recycled artifacts |
| `--strict` rejects `driver: agent` | self-reported evidence tier |

Route the verify through the evidence ledger so the tasks.md checkbox flip is
legal: `python3 lib/gates.py run-gate T0XX -- python3 lib/runtime_proof.py
verify .ralph/<phase>/proof.json`.

## Driver ladder (trust-descending, detect-and-fallback)

`scripts/browser-proof.sh --diff "<phase files>"` resolves:

1. **canary** — [wizenheimer/canary](https://github.com/wizenheimer/canary)
   CLI. Recorded sessions: each BDD scenario = one step
   (`canary session start` → `canary run step.js --step US1-S1` →
   `canary session end`); artifacts per session under
   `~/.canary/sessions/<id>/` — `results.json` (cross-checked by the
   verifier), `report.html`, `trace.zip`, video, HAR, console. Strongest
   evidence; a human can scrub to the failing moment.
   OSS quickstart: `npm i -g @usecanary/cli && canary install`.
2. **playwright** — committed e2e specs or scripted run; screenshots +
   console captured per scenario.
3. **agent** — LLM-driven browser (last resort). Must still fill every
   proof.json field; rejected under `--strict` / `RUNTIME_PROOF_STRICT=1`.

## Fail-not-skip

The old behavior skipped e2e when nothing answered on `:3000` — a UI phase
could pass QA with zero browser verification. Now a **web-touching diff with
no reachable app fails the phase**. Resolution order:

- `QA_BASE_URL` env (authoritative when set; unreachable pin = failure, no
  silent fallback probing). Prefer a preview/prod build URL — dev servers
  mask build failures.
- Probe list `3000 3001 5173 4321 8080 8000` (override:
  `BROWSER_PROOF_PROBE_PORTS`).
- `QA_ALLOW_NO_SERVER=1` is the only waiver — explicit and printed, never
  silent.

## BDD scenarios drive the runthroughs

`/spec-decompose` writes `specs/NNN/scenarios.md` (`## US<N>-S<M>: <title>` +
Given/When/Then) covering functional flows — buttons, forms, auth,
navigation, error states — not just page loads. The browser gate executes
them 1:1; `lib/runtime_proof.py skeleton specs/NNN/scenarios.md --out
.ralph/<phase>/proof.json` emits the UNFILLED template (which intentionally
fails verify until a real run fills it).

## Design review per phase

The qa-design dimension runs whenever a phase touches visual surfaces
(components/pages/CSS/emails/templates) — no longer only on `/design-html`
tasks. It grades against `specs/NNN/design-intent.md` when the plan carried a
`/plan-design-review` report (decompose extracts the checklist), else
DESIGN.md/design tokens. Output = `design-proof.json` (kind: visual
scenarios; screenshots at 1440 + 375), verified like any bundle. Environments
with the gstack `/design-review` skill get it as an orchestrator-level
enhancement on top.

## Maker/checker

QA agents are fresh-context evaluators, never the implementing agent — a
generator grading its own work confidently praises broken pages. The verifier
makes even the evaluator's optimism harmless: no valid bundle, no pass.

## Enforcement points

1. `scripts/qa-swarm.sh` (fallback path) — e2e fail-not-skip + design dim +
   `--aggregate` proof rejection.
2. `/feature-implement` Step 5.5 (hive path) — browser-proof resolution
   before QA, qa-design agent on UI phases, MANDATORY
   `qa-swarm.sh --aggregate` after agent verdicts.
3. `gates.py run-gate` on the `[qa:browser]` task — checkbox-evidence-gate
   blocks the checkbox flip without a recorded verify pass.
