# Why four models, and which one runs what

feature-fix-swarm runs every role on the cheapest model that can do that
role's job, and it never lets a model review its own work. This page
explains both rules and shows where they live in code.

## The problem

A naive agent harness runs everything on the best available model. That
fails two ways at once.

It burns money on work that doesn't need thinking. Renaming a symbol across
forty files is mechanical. Running it on a frontier model costs 10-20x what
it should and produces the same diff.

It also produces bad reviews. A model that just wrote a plan and is then
asked to check that plan will agree with itself. The measured effect is
large: a fresh-context reviewer improves an artifact roughly 6x more than
self-review. Worse, a reviewer *shown the author's reasoning* scores no
better than self-review (F1 23.8% vs 24.6%) — statistically indistinguishable
— while a fully-fresh reviewer scores 28.6%. Context poisons the review.

## The approach: four tiers, assigned by job

| Tier | Model | Runs |
|---|---|---|
| **haiku** | `claude-haiku-4-5-20251001` | Mechanical edits, search, parallel-fanned workers, research synthesis, codebase mapping |
| **sonnet** | `claude-sonnet-5` | The executor. Feature coding, most implementation, integration checks |
| **fable** | `claude-fable-5` | Planning and orchestration. Owns the plan, delegates execution to sonnet workers |
| **opus** | `claude-opus-5` | Architecture, security audit, adversarial verify, final review |

Fable's role is specific and worth stating plainly: it plans, then hands
execution to sonnet. That split lands around **96% of all-Fable quality at
roughly 46% of the cost**. It is a router, not a worker.

The defaults are declared in `templates/gsd-config.base.json`:

| Role | Tier | Line |
|---|---|---|
| `gsd-planner` | fable | :8 |
| `gsd-plan-checker` | opus | :9 |
| `gsd-executor` | sonnet | :10 |
| `gsd-debugger` | opus | :11 |
| `gsd-phase-researcher` | sonnet | :12 |
| `gsd-project-researcher` | sonnet | :13 |
| `gsd-research-synthesizer` | haiku | :14 |
| `gsd-codebase-mapper` | haiku | :15 |
| `gsd-verifier` | opus | :16 |
| `gsd-code-reviewer` | opus | :17 |
| `gsd-integration-checker` | sonnet | :18 |
| `gsd-nyquist-auditor` | sonnet | :19 |

Read that table as the rule in action: planner is fable, its checker is
opus. Executor is sonnet, its reviewer is opus. Never the same tier on both
sides of a check.

## Producer never reviews producer

This is **enforced by code**, not convention.

`scripts/gsd/adversary-host.sh:36-50` detects which vendor is orchestrating
the session (`detect_orchestrator_host()`), then dispatches the adversarial
review to the *other* vendor. Claude orchestrating means the Codex CLI
reviews; Codex orchestrating means Claude reviews. The dispatch happens in
`adversary_invoke_with_fallback()` (:236+).

When cross-vendor isn't possible, it falls back down a quality-descending
ladder within the available vendor (`adversary-host.sh:89-102`):

- Claude side: `opus → sonnet → haiku`
- Codex side: `sol → terra → luna`

Duplicates are removed, so a review already preferring opus doesn't try opus
twice.

The corollary matters as much as the rule: **fresh means no author reasoning
trail, not less artifact.** The reviewer gets the diff, the spec, the whole
repo. It never gets the producer's conversation. Do not paste author context
into a reviewer prompt, and do not starve the reviewer of the artifact
either.

## Codex equivalents

Every Claude tier has a pinned Codex counterpart, so a run can cross vendors
without changing its routing intent (`scripts/gsd/model-equivalents.sh:11-14, 26-28`):

| Claude | Codex | Effort |
|---|---|---|
| fable, opus | `gpt-5.6-sol` | `xhigh` |
| sonnet | `gpt-5.6-terra` | `high` |
| haiku | `gpt-5.6-luna` | `medium` |

The reverse map collapses `sol → opus`, never `sol → fable`. Fable
availability flaps; opus is the stable pin, so it is the safe landing spot
coming back from Codex.

Codex effort enum is `none|minimal|low|medium|high|xhigh`. `ultra` and `max`
are accepted CLI aliases; `xhigh` is canonical.

## When fable is unavailable

Fable's OAuth availability is not guaranteed. `scripts/gsd/model-fallback.sh`
handles that without a human in the loop.

**Trigger** — a probe finds fable unavailable (`:136`). Probe results cache
for 24h in `$GSD_FALLBACK_CACHE` (default `~/.cache/gsd-model-probe`), bounded
by `GSD_MODEL_PROBE_TIMEOUT` (default 120s).

**Substitution** — `fable → opus` (`:11`). It rewrites both the short alias
form (`"fable"`) and the full id form (`"claude-fable-5"`), so it works
whichever way your config pins models.

**Marker** — every rewrite is recorded to `.planning/fable-fallback.json`
(`:53`) as `{"mode": "codex-sol"|"opus-only", "paths": {<json.path>: <original>}}`.

**Restore** — when fable returns, only the exact JSON paths this lever
rewrote are restored (`:112-124`). An opus pin you set deliberately is never
flipped back to fable, because it was never in the marker.

## The security fence

Independent of availability, `scripts/gsd/security-model-fence.sh` forces
planning off fable when the spec is security-touching.

- **Trigger**: security keywords in the spec (`:51`)
- **Affects**: `gsd-planner` and `gsd-plan-checker` only (`:7, :61`)
- **Change**: `fable → opus` (`:61`)
- **Untouched**: executor and verifier (`:9`)

The reasoning is cost-asymmetric, not capability-based. Fable handles
security work fine (<5% classifier false-refusal after Anthropic's 2026-06
redeployment). But a false refusal mid-run on a security spec is expensive,
and opus is never *wrong* here — only costlier. The fence pays that premium
on exactly the specs where a stall hurts most.

Adversarial security *review* stays cross-model regardless, per the
producer≠reviewer rule above.

## Trade-offs

**Cheap tiers can be wrong.** Haiku on a mechanical task is the right call
until the task turns out not to be mechanical. The escalation path is
explicit: fail once, retry the same tier with the failure report appended;
fail twice, go one tier up carrying *both* reports; never retry a third time
on the same tier.

**The fence over-spends on purpose.** Some security-keyword matches are
false positives and will route a trivial plan to opus. Accepted: a wasted
opus plan is cheaper than a stalled overnight run.

**Cross-vendor review needs both CLIs.** If only one vendor is installed,
producer≠reviewer degrades to the within-vendor ladder — still a different
model, but a weaker guarantee than cross-vendor.

## Changing the assignments

Every tier above is a default, not a law. See
[Configuration](configuration.md#model_overrides) for how to repin a role,
and the kill-switches for the fence and fallback.

One rule to preserve when you edit: **every sub-agent spawn carries an
explicit `model` pin.** Relying on inherit is a bug — an unpinned build agent
under a premium orchestrator silently runs at premium cost. The
`delegation-enforcer.sh` PreToolUse hook auto-pins spawns that omit `model`,
reading from `model_overrides`. Kill-switch: `DELEGATION_ENFORCER=off`.

## Related

- [Configuration](configuration.md) — every model knob and its default
- [Choosing a command](choosing-a-command.md) — which entry point to use
- [Fable alignment](fable-pilotfish-alignment.md) — the longer Fable rationale
