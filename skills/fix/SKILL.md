---
name: fix
description: "Investigate a bug (root cause, not symptom), then fix it through host-native /feature-implement --adhoc; only review gates cross to the opposite model family."
version: "4.2.0"
allowed-tools:
  - Read
  - Edit
  - Write
  - Bash
  - Glob
  - Grep
  - Skill
---

# /fix — Investigate, then fix via feature-implement --adhoc

## Host dispatch contract

- Codex: `$skill`, Codex collaboration roles, and GPT-5.6 tiers.
- Claude: `/skill`, Agent/Skill tools, and Claude aliases.
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

One command. Takes a bug description or symptom, traces root cause, then hands the
fix to `/feature-implement --adhoc` — the ONE home for walls (model-fallback,
security fence, preflight/grant checks), the gsd execution loop, the finish tail
(review-gate → grant-walled ship), and the delegation report. This skill owns only
the investigation and the fix-specific QA loop.

## When to invoke

- User reports a bug: "auth redirect is broken", "the form doesn't submit", "500 on /api/foo"
- Claude discovers a bug during QA or code review
- After `/qa` or `/qa-only` reports failures that need fixing

## Flags

| Flag | Effect |
|---|---|
| `--interactive` | Restore manual gates between phases. Default = non-interactive. |
| `--autonomous` | Pass through to `/feature-implement --adhoc --autonomous` (fail-closed walls: fresh preflight + grant ledger required). |
| `--no-finish` | Pass through — skip the finish tail (review-gate/ship). **Operator-accepted-risk**; provably minimal blast radius only. Replaces v3.x `--no-review-gate`/`--no-audit` (retired). |

## Workflow

Cross-session coordination (spec-009) is inherited, not re-implemented: the
execute step routes through `/feature-implement --adhoc`, whose Step 1.5
claim-or-stop claims `adhoc-<slug>` (interactive) or defers to `gsd-run.sh`'s
own claim (headless). If that claim is REFUSED, another live session owns this
fix — stop and inspect `python3 scripts/coord/coord.py status`.

### Step 1: Investigate (root cause, not symptom)

Invoke `/investigate` (or trace inline for small bugs): reproduce, grep every caller
of the function to be touched, identify the shared root-cause site. Write the
finding as a one-paragraph statement with file:line refs before editing anything.

**Route to GSD debug when the reproduction is non-obvious** — explicit
criteria: (no failing test yet) AND (no single-command repro exists). Invoke
gsd's scientific-method hypothesis→experiment loop and consume its root-cause
report before editing anything. When a failing test or a single-command
repro already exists, stay on this `/investigate` path — do not route to GSD
debug. Use the invoking host's native command surface: Claude `/gsd-debug`,
Codex `$gsd-debug`; for headless execution use
`TIMEOUT=1800 bash scripts/gsd/gsd-run.sh /gsd-debug <args>`. Ordinary debug
work never crosses vendors; only the downstream review gate does.

### Step 2: Fix via feature-implement --adhoc

Invoke the `feature-implement` skill with the root-cause statement as the task:

```
/feature-implement --adhoc "<root-cause statement with file:line refs>" [--autonomous] [--no-finish]
```

That skill owns everything from here: the model-availability + security-fence
walls, the gsd-quick plan→execute→verify loop (TDD RED/GREEN commit trail
required), the `workflow.test_command` completion authority, the finish tail
(browser gate → review-gate → grant-walled ship → merge backstop → learnings
harvest), and the delegation histogram. Do NOT reimplement any of it here —
drift between /fix and the spec pipeline is the defect this version removed.
The delegated run prefers the invoking host and uses its native model ladder. If
that host is unavailable, the runner may select the alternate before launch; it
never replays a stateful drive after launch. Its review gate deliberately prefers
the opposite host.

### Step 3: Fix-specific QA loop

After the adhoc run returns:

1. `/qa-only` on the affected surface — fail → back to Step 2 with the failure
   report appended to the task (max 2 loops, then STOP + report).
2. Full `/qa` when browser-touchable (the tail's canary-gate covers headless
   browser proof; full `/qa` covers the interactive surface).

### Step 4: Report

Root cause, diff summary, and the feature-implement report (gate evidence id,
review-gate verdict, consumed grants, delegation histogram) — one report, not two.
On abort: structured artifact (what was tried, failing signature, next hypothesis).

## Removed in v4.0.0

Inline model-fallback preflight, inline gsd-run/gsd-quick invocation, inline
review-gate step, `--no-audit` + `--no-review-gate` flags — all superseded by
`/feature-implement --adhoc` (v2.6.0), which /task-swarm also fronts. Earlier
removals (v3.0.0): Ruflo swarm coordination, Ralph retry loop, audits.jsonl
run-state coupling, `--auto-fix` forwarding.
