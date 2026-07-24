# How to pick the right command

feature-fix-swarm gives you four ways in. Picking wrong costs you either
ceremony you didn't need or structure you did. This page is the decision.

## The short answer

```
                  Do you know what to build?
                             │
              ┌──────────────┴──────────────┐
             NO                             YES
              │                              │
       /office-hours                Is something broken?
    (gstack — shape it)                      │
              │                  ┌───────────┴───────────┐
              └──────────────▶  YES                      NO
                                 │                        │
                               /fix          Does it need a durable spec?
                                              (formal BDD / AC / E2E contracts)
                                                  │                 │
                                                 YES               NO
                                                  │                 │
                                           /feature-spec      /task-swarm
                                                  │                 │
                                                  └────────┬────────┘
                                                           ▼
                                              /feature-implement NNN --autonomous
```

Everything converges on `/feature-implement`. The three front doors differ
only in how much planning they do before they get there.

## Step 0: shape the idea first — `/office-hours`

`/office-hours` is a **gstack** skill, not part of this package. It is the
step before any of the four commands below, and it is the one people skip.

Run it when you have a goal but not a plan: "I want users to be able to
share a report" is an idea, not a task. `/office-hours` interrogates it into
something with a scope, a user, and a success condition. Only then does a
spec pipeline have something real to chew on.

```bash
/office-hours
```

Skip it when the work is already specified — a bug with a stack trace, a
task someone else already scoped, a spec that exists.

**Note:** because `/office-hours` ships with gstack rather than this package,
no SKILL.md here invokes it automatically. It is a manual first move. If you
don't have gstack installed, do the equivalent by hand: write down the user,
the outcome, and how you'll know it worked, before running anything below.

## Step 1: pick your front door

### `/fix` — something is broken, no spec exists

```bash
/fix "webhook retries fire twice when the first attempt times out"
```

Use it when you have a **symptom**. `/fix` owns the part the others don't:
finding the root cause before changing anything. It routes to `/investigate`
when you already have a repro, and to `/gsd-debug` (scientific-method debug
loop) when you don't.

Wrong choice when: you already know the root cause and just want it built.
Go straight to `/feature-implement --adhoc` instead.

- Produces: a root-cause statement with `file:line` refs, then the fix
- Chains: `/investigate` or `/gsd-debug` → `/feature-implement --adhoc` → `/qa-only` (max 2 loops) → `/qa` if browser-touchable
- Operator stops: 0 by default; `--interactive` restores manual gates
- Flags: `--interactive`, `--autonomous`, `--no-finish`

### `/task-swarm` — a bounded task, plan it but skip the ceremony

```bash
/task-swarm "add rate limiting to the webhook endpoints"
```

Use it when the work is real enough to need a plan but not big enough to
deserve a spec document. One sentence in, shipped change out. It runs a
single `plan-decompose` pass (eng review + decompose) and hands off.

Wrong choice when: the feature needs durable spec artifacts other people
will read — formal acceptance criteria, BDD scenarios, an E2E contract.
That's `/feature-spec`.

- Produces: seeded `.planning/` project, `specs/NNN/preflight.json`, grant ledger
- Chains: `plan-decompose` → `preflight` → `autonomy-grant` → `feature-implement NNN --autonomous`
- Operator stops: 0 by default (MAX-AUTH); 1 with `--gated`
- Flags: `--gated`, `--attended`, `--no-swarm`, `--dry-run`

### `/feature-spec` — a real feature that needs a spec on disk

```bash
/feature-spec "team members can share a saved report by link"
/feature-spec 331          # or resume an existing spec number
```

Use it when the feature is substantial enough that the spec is itself an
artifact — something to review, argue about, and come back to in three
months. It bakes TDD/BDD/E2E test contracts into `spec.md` and `plan.md`
**before** implementation, so the tests aren't an afterthought.

Wrong choice when: the task fits in a sentence and nobody will ever reread
the spec. That's `/task-swarm`.

- Produces: `specs/NNN/{spec.md, plan.md, tasks.md, prior-art.md, preflight.json}`, `.planning/` seed, grant ledger
- Chains: `speckit.specify` → `speckit.plan` → (optional `/autoplan`) → `speckit.clarify` → `spec-decompose` → `preflight` → autonomy-grant → `feature-implement`
- Operator stops: 0 by default (MAX-AUTH auto-grants at Step 6); 1 with `--gated`
- Flags: `--gated`, `--no-clarify`, `--no-preflight`, `--no-grant`, `--no-swarm`, `--dry-run`

### `/feature-implement` — the executor

```bash
/feature-implement 331 --autonomous
/feature-implement --adhoc "bump the connection pool ceiling to 40"
```

This is where the delegation machinery lives. The other three all end by
calling it. Invoke it directly when a `.planning/` project is already seeded,
or with `--adhoc` for a bounded task that needs no plan at all.

There is **no behavioral difference** between invoking it yourself and
having `/feature-spec` or `/task-swarm` invoke it. Same skill, same walls.

- Chains (Step 6 finish tail, on by default): canary-gate → qa-coverage-adversary → openwiki ship-stage → `/review-gate` → ship → `/canary` → learnings-harvest
- Operator stops: 0 with `--autonomous` (fail-closed walls replace asking: it refuses to start without a fresh preflight PASS and a grant ledger)
- Flags: `[NNN]`, `--autonomous`, `--adhoc "<task>"`, `--no-finish`, `--dry-run`

## Deprecated

`/feature` is deprecated. It is a stub that chains `/feature-spec` →
`/feature-implement`. Call those two directly.

## The two gates every path crosses

Both front doors and the executor route through the same two primitives.
Understanding them explains most "why did it stop?" questions.

**`/preflight`** — fail-closed environment check at plan time. Verifies the
services, credentials, and tooling the run will need actually work *now*,
before an unattended run discovers it at 3am. `--autonomous` refuses to
start without a fresh PASS.

**`/autonomy-grant`** — a typed, run-bound, TTL'd ledger of operator
approvals. An autonomous run that reaches an action nobody granted (push,
deploy, merge) stops and records it as pending rather than guessing. This is
why `--autonomous` can have zero operator stops without being reckless: the
approvals were all collected up front.

## Related

- [Model tiers](model-tiers.md) — which model runs each role, and why
- [Configuration](configuration.md) — every knob, its default, and what reads it
- [Getting started](getting-started.md) — a first run end to end
- [Pipeline](pipeline.md) — the full stage-by-stage flow
