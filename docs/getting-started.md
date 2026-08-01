# Getting started

By the end of this you'll have feature-fix-swarm installed and one real task
shipped through it, with per-phase QA and a cross-model review gate on the
way out.

## What you'll need

- A git repo you're willing to land a small change in
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) **or** [Codex
  CLI](https://github.com/openai/codex). Both is better — that's what enables
  cross-vendor review
- Node 22+, npm 10+, Python 3.9+, `git`

## Step 1: install

```bash
git clone https://github.com/austinmao/feature-fix-swarm.git
cd feature-fix-swarm
npm ci
bash setup.sh --scope user
```

For a reproducible repository-local install instead:

```bash
bash setup.sh --scope project --project-dir /absolute/path/to/repo
```

The installer hash-manages FFS skills for both Codex and Claude. It installs
nothing under legacy `.codex/skills`; GSD's upstream installer owns all
`gsd-*` runtime surfaces. See [Installer, migration, and
rollback](installer.md) for scope resolution, doctor, and rollback details.

## Step 2: confirm the harness is healthy

```bash
bash setup.sh --doctor --scope user
python3 scripts/harness-audit.py
```

This prints a 0-100 score for the installed harness: dangling skill links,
vendored drift, dead model pins, hook drift. It never blocks anything — it's
a readout.

A low score here is worth fixing before an unattended run. A dead model pin
means a role is routed to a model that no longer exists, and you'd rather
find that now than at 3am.

## Step 3: point it at a real task

Now the actual work. Pick something small and real from your repo — a
missing validation, a hardcoded value that should be config, a rate limit
nobody added.

Before you run anything, decide which door you're going through. The full
decision is in [Choosing a command](choosing-a-command.md); for a first run,
`task-swarm` is the right one. It plans, but skips the spec ceremony.

```text
Codex: $task-swarm "add rate limiting to the webhook endpoints" --dry-run
Claude: /task-swarm "add rate limiting to the webhook endpoints" --dry-run
```

`--dry-run` plans without executing. You'll see the decomposed task list,
the model assigned to each role, and the gates that will run. **Read it.**
This is the cheapest possible moment to notice the plan is wrong.

## Step 4: run it for real

Drop `--dry-run`:

```text
Codex: $task-swarm "add rate limiting to the webhook endpoints"
Claude: /task-swarm "add rate limiting to the webhook endpoints"
```

What happens, in order:

1. **`plan-decompose`** turns your sentence into a plan and a task graph
2. **`preflight`** checks the environment can actually support the run —
   fail-closed, so a missing credential stops you here rather than midway
3. **`autonomy-grant`** records which outward actions (push, merge, deploy)
   are pre-approved for this run
4. **`feature-implement --autonomous`** executes phase by phase, running the
   QA swarm at each boundary
5. **The finish tail** — canary gate, QA coverage critique, `/review-gate`,
   ship, canary

By default this has **zero operator stops**. That isn't recklessness: steps
2 and 3 collected the approvals up front, and any action nobody granted
stops the run and gets recorded as pending rather than guessed at.

If you'd rather be asked, add `--gated`.

## Step 5: read the review

The interesting output is `/review-gate`. It runs three passes, and it
deliberately runs on a **different model than the one that wrote the code** —
Codex reviewing Claude's work, or the reverse.

Findings come back severity-tagged. HIGH and CRITICAL block the ship;
anything that survives a refute-or-promote pass is real. If it returns
`REVISE`, the run stops and tells you why.

If you only have one host CLI installed, you'll see `DEGRADED` — review ran,
but within-vendor. Still a different model, weaker guarantee. Installing the
other CLI upgrades this permanently.

## What you built

A repo where every phase is tested before the next one starts, and nothing
ships without a hostile second model trying to prove it isn't done.

Where to go next:

- [Choosing a command](choosing-a-command.md) — `/task-swarm` was the
  training-wheels choice. Learn when you want `/feature-spec` or `/fix`
  instead
- [Model tiers](model-tiers.md) — why planning and review use the judgment
  tier while implementation normally uses the execution tier
- [Configuration](configuration.md) — every knob you can turn

## Troubleshooting

**`preflight` fails and won't let the run start.** Working as designed —
`--autonomous` refuses to start without a fresh PASS. Read what it names as
missing, fix it, re-run. Do not bypass it before an unattended run.

**The run stops with a "pending" action.** It hit an outward action that
wasn't in the grant ledger. Grant it explicitly and resume, or re-run with
`--gated` so you approve as you go.

**`/review-gate` returns `REVISE` with no findings.** Usually the reviewer
was unparseable or returned no line-anchored verdict. This fails closed on
purpose — no mandatory review fails open. Re-run it; if it persists, check
that the opposite-host CLI is logged in.

**Review says `DEGRADED`.** Only one host CLI available. Install the other
(`npm install -g @openai/codex && codex login`) for true cross-vendor review.

**A role resolved to a different model than expected.** Check the persisted
model request, effort, and adversary provenance before resuming. Tier fallback
is recorded as degraded; an exact model request fails instead of silently
substituting. See [Model tiers](model-tiers.md).
