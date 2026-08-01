# Typed model requests and host resolution

feature-fix-swarm requests the kind of work a role performs, resolves that
request for the selected host, and never lets a model review its own work.
This page explains those rules and the compatibility aliases retained for
older FFS configurations.

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

## The approach: three workload tiers

| Request | Codex resolution | Default effort | Runs |
|---|---|---|---|
| **judgment** | `gpt-5.6-sol` | high | Planning, checking, debugging, verification, code/security review |
| **execution** | `gpt-5.6-terra` | medium | Thin orchestration, implementation, research, integration, Nyquist work |
| **volume** | `gpt-5.6-luna` | low | Mapping, synthesis, and status collection |

Defaults are declared in `templates/model-requests.json`. A request is either
`{"kind":"tier","name":"judgment|execution|volume"}` or
`{"kind":"exact","id":"…"}`. Raw vendor IDs outside an exact request
fail validation. Exact requests never silently substitute another model;
optional fallback is available only for tier requests and records degraded
adversary provenance. FFS infers the supported CLI from the exact ID (`gpt-*`
and `oN*` use Codex; `claude-*` uses Claude); any other vendor fails before a
CLI is launched.

Read the assignments as the producer/reviewer rule in action: executors use
the execution tier while their verification and security reviewers use
judgment. Never use the same resolved model on both sides of a check.

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

## Legacy host aliases

Older FFS configs and Claude-native levers still use the historical aliases.
`scripts/gsd/model-equivalents.sh` maps them without letting a raw alias become
an untyped model request:

| Claude | Codex | Effort |
|---|---|---|
| fable, opus | `gpt-5.6-sol` | `high` |
| sonnet | `gpt-5.6-terra` | `medium` |
| haiku | `gpt-5.6-luna` | `low` |

The reverse map collapses `sol → opus`, never `sol → fable`. An explicit exact
Fable request is not this compatibility mapping: it never falls back and its
provenance gate cannot be satisfied by Sol or Opus.

## Legacy Fable fallback lever

`scripts/gsd/model-fallback.sh` remains for pre-5.0 Claude configurations.
It does not apply to a typed exact Fable request, whose contract is fail
closed.

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

## Fable-era operating notes

Three practices from Anthropic's Fable 5 prompting guide that this package
relies on (they inform the skill prose, not new config knobs):

- **Parallel dispatch.** Fable dispatches subagents readily; the orchestrator
  should fan out independent work and keep going, not block per return. Only
  the plan gauntlet (plan → check → bounce) is inherently serial.
- **Short guards over checklists.** Fable's instruction-following makes a
  one-sentence steer as effective as an enumerated behavior list. Skill guard
  blocks stay short on purpose — resist growing them.
- **Effort, not model swaps, for routine work.** On runtimes exposing effort,
  lower effort on Fable still outperforms prior-generation `xhigh`; prefer
  dialing effort down for mechanical passes before repinning models.

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
