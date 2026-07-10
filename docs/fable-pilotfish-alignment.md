# FFS × Fable-5 / pilotfish alignment (2026-07-09)

Result of auditing feature-fix-swarm against two sources:

- the **Claude Fable 5 prompting guide** (platform.claude.com — fresh-context
  verifiers over self-critique, anti-early-stop guard, no reasoning-echo
  instructions, security work off Fable, Fable-orchestrator/Sonnet-worker
  topology benchmarked at 96% quality @ 46% cost);
- **pilotfish** (github.com/Nanako0129/pilotfish — role↔model decoupling,
  one-line rebinding, cheapest-role-first escalation, verifier gate before
  done, security-executor deliberately off Fable).

Verdict: the playbook was ~95% already implemented on the live gsd path.
This doc records what was already true (so it isn't re-proposed), the three
deltas shipped in v4.1.0, and what was deliberately deferred.

## Already implemented (verified against source — do not re-propose)

| Playbook item | Where it already lives |
|---|---|
| Fable-orchestrator / Sonnet-worker topology (96%@46%) | `templates/gsd-config.base.json` — planner/plan-checker=`fable`, executor=`sonnet`, verifier/reviewer=`opus`. The default, not an option. |
| Fresh-context verification beats self-critique | `/review-gate` — opposite-CLI 3-pass + refute-or-promote + verify-the-reviewer; honest-verifier/goal-backward pass (v1.3.0); gsd-verifier goal-backward at `phase.complete`. |
| No "echo/explain your reasoning" phrasing (`reasoning_extraction` refusal trap) | Zero occurrences package-wide; return contracts enforce the opposite ("Conclusion FIRST, no exploratory narration", `lib/dispatch.py`). |
| Graceful degradation / fallback chain | `scripts/gsd/model-fallback.sh` — fable→opus when unavailable, 24h probe cache, invoked at seed + run wall. |
| Role↔model one-line rebinding | gsd `.planning/config.json` `model_overrides` + `dynamic_routing.tier_models` — the single binding site; skills reference, never pin. |
| Tight sub-agent return contracts | scout≤15 / build≤20 / deep≤40 lines (`lib/dispatch.py`, `.claude/rules` mirrors). |
| Memory / lessons ("one lesson per file") | gsd `learnings.jsonl` + `/gsd-extract-learnings`. |

## Shipped deltas (v4.1.0)

1. **Security model fence** — `scripts/gsd/security-model-fence.sh` +
   `tests/bats/security-model-fence.bats`. Gap: the base template pins
   *planning* to Fable, and `model-fallback.sh` only rewrites when Fable is
   unavailable — nothing kept security-touching planning off an *available*
   Fable, whose classifiers can false-refuse benign defensive-security work
   (auth/RLS/payments/crypto) and stall the run silently. The fence greps the
   seeded planning docs (+ spec/plan files) for security keywords and forces
   `gsd-planner`/`gsd-plan-checker` fable→opus (alias and full-ID spellings).
   Executor/verifier already never run on Fable. Fail-soft, never silent.
   Wired after `model-fallback.sh` in `/spec-decompose` Step 2 and
   `/feature-implement` Step 2.
2. **Anti-early-stop reminder** — Fable's verbatim "check your last
   paragraph…" guard in the autonomous orchestrator loops
   (`/feature-implement` Step 4, `/task-swarm` Step 4). Orchestrator-level
   mitigation only: per-turn work runs in gsd-core sub-agents FFS does not
   own; deeper coverage needs a gsd-core change.
3. **Doc-drift fix** — `prompts/decompose-spec.md` banner'd LEGACY (its
   `[model:]`/`[agent:]` grammar + tasks.md output were retired by
   spec-decompose v2.0.0; live routing = gsd `.planning/config.json`), and
   the false "fable downgrades to sonnet on the Ruflo path" clause removed
   (ruflo is retired; no such downgrade exists in `dispatch.py`).

## Deferred — with reasons (don't rebuild without new evidence)

- **Fable/Sonnet routing preset** — already the default template; nothing to add.
- **Interval mid-run verifier** — gsd already verifies goal-backward at
  `phase.complete` and honestly at ship; an extra interval loop is machinery
  for marginal gain. Revisit only if phases prove too large in practice.
- **Prescriptiveness audit** of large advisory skills — only if a Fable run
  shows measured degradation; machine gates stay verbatim regardless.
- **Async long-lived sub-agents** (context-keeping) — architectural change to
  the dispatch model; real win, big lift.
- **Memory-lessons wiring** — covered by gsd learnings.

## Addendum 2026-07-10 (v4.3.0) — routing rebalance + plan-stage adversary

Second alignment pass, grounded on the same three sources plus
jnuyens/gsd-plugin's `sdk/shared/model-catalog.json` (33 agents → heavy/standard/
light tiers). Shipped:

- `gsd-plan-checker` fable→opus (checker must not share the planner's model —
  fresh-context-verifier principle), `gsd-debugger` sonnet→opus,
  `gsd-integration-checker`/`gsd-nyquist-auditor` opus→sonnet,
  `gsd-research-synthesizer`/`gsd-codebase-mapper` sonnet→haiku.
- `scripts/gsd/plan-adversary.sh` at gsd's `workflow.plan_bounce_script` seam:
  high-blast plans get a `gpt-5.6-sol` @ `xhigh` cross-model review appended;
  the opus plan-checker re-run adjudicates. This supersedes the earlier
  "interval mid-run verifier — deferred" disposition at the PLAN stage only:
  the plan is where Fable's planning strength makes an undetected error most
  expensive, and gsd already ships the seam (no new machinery). Mid-EXECUTION
  interval verification stays deferred.

## Addendum 2026-07-10 (v4.4.0) — browser-QA gate + host symmetry + GPT-5.6 family

Research pass: 2026 testing best practices (the "green tests, broken browser"
failure decomposes into mock-shape drift, jsdom's silent omissions, no
real-browser gate in the agent loop, same-author tests), Canary CLI 0.4.4
(machine-gateable `results.json`), and the GPT-5.6 release (2026-07-09).

**GPT-5.6 family + FFS tier equivalences** (developers.openai.com/api/docs/models).
Executable home (v4.5.1): `scripts/gsd/model-equivalents.sh` — sourceable lib
(`codex_equiv_model`/`codex_equiv_effort`/`claude_equiv_model`) that any
consumer (model-fallback.sh's codex-sol probe leg included) should source
rather than re-deriving this table by hand:

| OpenAI | $/1M in/out | Claude-side equivalent | FFS use |
|---|---|---|---|
| `gpt-5.6-sol` | $5/$30 | opus / fable | adversarial gates (plan-adversary, review-gate) @ `xhigh`; `max` for escalated disputes |
| `gpt-5.6-terra` | $2.50/$15 | sonnet | qa-coverage-adversary @ `high` (gap-finder, not judge) |
| `gpt-5.6-luna` | $1/$6 | haiku | cheap mechanical review passes (unused today) |

Correction (v4.5.1): the effort set stated here as `none|low|medium|high|xhigh|max`
was imprecise. Codex CLI 0.144's API-validated `model_reasoning_effort` enum is
`none|minimal|low|medium|high|xhigh` — `ultra`/`max` are CLI-accepted aliases
that do NOT appear in the enum itself; the canonical top tier is `xhigh`
(matches plan-adversary.sh's `EFFORT=xhigh` default). No dated snapshots.
Caveat: OpenAI's real-time cyber classifiers can pause/block legitimate
security-adjacent prompts mid-stream — same failure class as Fable's
`security-model-fence.sh`, now present on both vendors; adversary scripts stay
fail-soft for exactly this reason.

**Host symmetry**: `adversary-host.sh` makes every cross-model adversary detect
the orchestrating CLI and pick the opposite vendor, closing the gap where a
codex-orchestrated FFS run would have codex reviewing its own plans.

**Browser gate**: `canary-gate.sh` (fail-closed) + `qa-coverage-adversary.sh`
(advisory) + `testing-policy` skill + `code-uplift` skill — see CHANGELOG v4.4.0.
Supersedes "browser QA absent from finish tail" (the tail is now
canary-gate → qa-coverage → review-gate → ship → /canary).
