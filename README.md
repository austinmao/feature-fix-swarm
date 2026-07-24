# feature-fix-swarm

**Per-phase QA enforcement for Claude Code and Codex harnesses, with
cross-model adversarial completion audit.**

Agent harnesses decompose a feature into 20-50 tasks, execute them all, then
run QA at the end — and discover that task 3 broke something and tasks 4-50
built on top of it. By then, 47 tasks of context separate you from the root
cause.

feature-fix-swarm makes that impossible. Every phase is tested before the
next one starts. No phase advances on red. And every completion claim is
gated by a hostile cross-model audit that tries to prove the work is *not*
done — because a model that grades its own homework passes itself.

The workflow is host-neutral. Claude and Codex are runtime adapters over the
same task state.

---

## Start here

**New to this?** → [Getting started](docs/getting-started.md) — install to
first shipped task.

**Know what you want to build?** → [Choosing a
command](docs/choosing-a-command.md) — the decision between the four entry
points. Read this before running anything.

The short version:

```
/office-hours          shape a fuzzy idea into a scoped task   (gstack, manual first step)
      ↓
/fix "<symptom>"       something is broken, no spec exists
/task-swarm "<task>"   bounded task, plan it, skip the ceremony
/feature-spec "<x>"    real feature that deserves a spec on disk
      ↓
/feature-implement NNN --autonomous     ← all three converge here
```

**Want to change how it behaves?** → [Configuration](docs/configuration.md)
— every knob, its default, and the code that reads it.

**Wondering why it picked that model?** → [Model tiers](docs/model-tiers.md)
— fable / opus / sonnet / haiku, and the producer≠reviewer rule.

---

## The two disciplines

**Build mode** (`/feature-implement`) — after every phase of tasks completes,
a QA swarm runs deterministic tests plus LLM review agents. Bugs are caught
at the phase boundary, not at the end. Failures trigger an automatic
investigate-fix-retest loop before the next phase starts.

**Fix mode** (`/fix "bug description"`) — post-ship remediation. Takes a bug
report, searches prior fix patterns, investigates root cause, applies the
fix through the executor, verifies with focused QA, then runs full
regression QA. Loops until green.

## How a phase gate works

```
Phase N tasks complete
        |
        v
+-- Deterministic hooks -------+
|  vitest run (TS/JS files)    |    $0 cost
|  pytest -x (Python files)    |    deterministic
+----------+-------------------+
           |
           v
+-- LLM QA swarm --------------+
|  qa-e2e (browser via $B)     |    ~$0.05 each
|  qa-review (code review)     |    parallel
|  qa-security (OWASP scan)    |
+----------+-------------------+
           |
      pass? --yes--> Phase N+1
           |
          no
           |
           v
+-- Investigate + fix ---------+
|  /investigate (5 Whys)       |    scope-locked
|  fix sub-agent               |    TDD: test first
|  /qa-only (re-verify area)   |    targeted
+----------+-------------------+
           |
      retry < 3? --yes--> Re-run QA swarm
           |
          no
           |
           v
      Mark [F], stop pipeline
      Print artifacts + resume command
```

### The QA dimensions

| Dimension | Type | Model | Checks | Passes when |
|---|---|---|---|---|
| unit | deterministic | — | `vitest run` / `pytest -x` on changed files | tests green |
| integration | deterministic | — | API contract tests on changed endpoints | contracts hold |
| e2e | LLM agent | sonnet | Browser tests via `$B` (gstack browse) | journeys complete |
| review | LLM agent | sonnet | Code review for logic errors | no CRITICAL/HIGH |
| security | LLM agent | sonnet | OWASP Top 10 scan on diff | no exploitable vulns |

Unit and integration are free. The three LLM agents cost ~$0.15/phase total.

## Model routing in one paragraph

Claude Code uses `fable` for planning and orchestration, `sonnet` for
execution, `opus` for architecture and verification, `haiku` for mechanical
work. Codex uses the equivalent ladder: fable/opus → `gpt-5.6-sol`, sonnet →
`gpt-5.6-terra`, haiku → `gpt-5.6-luna`. Whatever model produces an artifact,
a **different** model with fresh context reviews it — `/review-gate`
deliberately tries the opposite vendor first. Full detail, including the
fable-unavailable fallback and the security fence: [Model
tiers](docs/model-tiers.md).

## Cost

| Scenario | Typical |
|---|---|
| Per-phase QA (deterministic hooks) | $0.00 |
| Per-phase QA (3 LLM agents) | ~$0.15 |
| Full feature, 20 tasks, 5 phases | ~$0.75 QA overhead |
| Bug fix, trivial (1 file) | ~$0.10 |
| Bug fix, moderate (2-5 files) | ~$0.50 |
| Bug fix, complex (5+ files + eng review) | ~$2.00 |
| `/review-gate` per invocation | ~$2 + ~13 min |

---

## Installation

```bash
git clone https://github.com/austinmao/feature-fix-swarm.git
cd feature-fix-swarm
bash setup.sh
```

The installer copies skills, scripts, and prompts into your project, checks
prerequisites, and offers to bootstrap anything missing. It verifies the
installed runner, adversary adapter, hard-bound helper, and hang guard
byte-for-byte, and registers the guard for both Claude and Codex.

To reconcile only the runtime levers in an existing consumer repo:

```bash
bash setup.sh --reconcile-consumer /absolute/path/to/repo
```

### Prerequisites

**At least one host** — [Claude
Code](https://docs.anthropic.com/en/docs/claude-code) or [Codex
CLI](https://github.com/openai/codex). Install both to enable opposite-host
review in either direction.

**[gstack](https://github.com/garryslist/gstack)** by Garry Tan — provides
the skills this package orchestrates: `/office-hours` (shape an idea),
`/investigate` (5 Whys), `/qa` and `/qa-only`, `/review`, `/ship`.

```bash
cd ~/.claude/skills && git clone https://github.com/garryslist/gstack.git
```

**[Open GSD Core](https://github.com/open-gsd/gsd-core)** — exact-pinned by
this package and installed by `setup.sh` for every available host runtime.
GSD owns plan/execute/verify orchestration.

**[Spec Kit](https://github.com/github/spec-kit)** by GitHub — the
spec/plan/tasks bootstrap `/feature-spec` builds on.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install specify-cli --from git+https://github.com/github/spec-kit.git
specify version
```

**[ECC](https://github.com/affaan-m/ECC)** and
**[wshobson/agents](https://github.com/wshobson/agents)** — the
exact hybrid catalog that decomposition and execution route
`[agent:exact-agent]` tags against. `setup.sh` installs and refreshes both
against upstream `main`.

**[goal-wrap](https://github.com/austinmao/goal-wrap)** — the
handoff/continuation layer for `/goal-wrap`.

```bash
npx skills add austinmao/goal-wrap --skill goal-wrap --skill handoff -g -a claude-code -a codex -y
npx skills add nidhinjs/prompt-master --skill prompt-master -g -a claude-code -a codex -y
```

### Opposite-host review runtime

Install the other host CLI to enable cross-vendor adversarial review:

```bash
npm install -g @openai/codex
codex login
```

The review gate is part of the default finish tail for `/feature-implement`
and `/fix`; `--no-finish` is the explicit accepted-risk opt-out. When the
opposite CLI is unavailable, review tries the active host once and labels
the result `DEGRADED`. Both unavailable, an unparseable reviewer, or a ship
reviewer without exactly one line-anchored verdict returns `REVISE`. **No
mandatory ship review fails open.**

---

## Native `/goal` integration (v3.0+)

Claude Code 2.1.139+ ships native `/goal`
([docs](https://code.claude.com/docs/en/goal)), which owns the continuation
loop. v3.0 stripped this package's Stop hook, marker file, and continuation
tracking — roughly 300 lines of dead code. What remains is what native
`/goal` doesn't do: cross-model adversarial audit and `/review-gate`.

```
Operator: /goal "spec NNN done: every phase audit verdict=pass, review-gate PASS, canary 200"
Skill:    /feature-spec → /spec-decompose → /feature-implement → /qa → /review-gate → /ship → /canary
Native /goal: condition holds → auto-clears. Done.
```

After `bash setup.sh`:

```bash
~/.claude/bin/run-state list                  # show all runs
~/.claude/bin/run-state status <run_id>       # detailed status
~/.claude/bin/run-state audit <run_id> --kind <fix|feature|phase> --context K=V
~/.claude/bin/run-state abort <run_id>        # kill a stuck run
```

Full details: [lib/run_state/README.md](lib/run_state/README.md).

**Upgrading from v2.x?** `setup.sh` no longer registers Stop / SessionStart
hooks. Remove leftover v2.x entries from `~/.claude/settings.json` by
filtering out commands containing `run-state-stop` and `run-state-session`.

---

## Flags

Verified against the shipped SKILL.md files.

| Flag | Works with | Effect |
|---|---|---|
| `--autonomous` | feature-implement, feature-spec, fix | Unattended run. Fail-closed walls replace operator prompts |
| `--adhoc "<task>"` | feature-implement | Run a bounded task with no spec dir |
| `--no-finish` | feature-implement, fix, task-swarm | Skip the finish tail (canary → review-gate → ship → canary) |
| `--dry-run` | all four entry points | Plan without executing |
| `--gated` | feature-spec, task-swarm | Stop once to review the enumerated gate list before granting |
| `--attended` | task-swarm | Skip the grant ledger; prompt during implement instead |
| `--interactive` | fix | Restore manual gates (default is non-interactive) |
| `--no-clarify` / `--no-preflight` / `--no-grant` / `--no-swarm` | feature-spec | Skip that pipeline stage |
| `--resume` | feature-implement | Pick up from last failure |
| `--one` | feature-implement | Execute only the next task |

## Configuration

The one config-file key this package reads directly is `model_overrides` in
`.planning/config.json`. Everything else in that file belongs to
`@opengsd/gsd-core`.

Behavior is tuned per-run through environment variables:

| Var | Default | Effect |
|---|---|---|
| `GATES_STRICT` | unset | Only runner-executed evidence counts; rejects caller-asserted proof |
| `REVIEW_TIER` | auto | Force `light` / `standard` / `full` review depth |
| `RALPH_AUTO_QA` | `1` | `0` disables the debounced auto-QA hook |
| `RALPH_DEBOUNCE_SECS` | `30` | Quiet window before auto-QA fires |
| `DELEGATION_ENFORCER` | on | `off` disables auto-pinning `model` on sub-agent spawns |
| `CANARY_GATE` | on | `off` skips the fail-closed browser-QA gate |

That table is the short list. **Every** knob, with its consumer and default:
[Configuration](docs/configuration.md).

---

## Documentation

| Doc | What it answers |
|---|---|
| [Getting started](docs/getting-started.md) | Install to first shipped task |
| [Choosing a command](docs/choosing-a-command.md) | Which entry point, and when |
| [Model tiers](docs/model-tiers.md) | Which model runs what, and why |
| [Configuration](docs/configuration.md) | Every knob and its default |
| [Pipeline](docs/pipeline.md) | Stage-by-stage architecture |
| [Commands](docs/commands.md) | Full command reference |
| [TDD/BDD guide](docs/tdd-bdd-guide.md) | Research-backed testing practices |
| [QA Ralph loop](docs/qa-ralph-loop.md) | The investigate-fix-retest loop |
| [Browser proof](docs/browser-proof.md) | Browser-QA evidence gate |
| [Promotion protocol](docs/promotion-protocol.md) | Staging→prod promotion rules |
| [Fable alignment](docs/fable-pilotfish-alignment.md) | The longer Fable rationale |
| [gbrain (optional)](docs/gbrain-optional.md) | Cross-session learning store |

## What's in the box

```
feature-fix-swarm/
  skills/           Claude Code SKILL.md files
    feature-spec/     spec-first pipeline (speckit specify → plan → clarify, TDD/BDD/E2E)
    feature-implement/  the executor — task execution with per-phase QA gates
    fix/              investigate + fix + verify loop
    task-swarm/       free-text task → plan → ship, no speckit ceremony
    spec-decompose/   spec → tasks.md (mandatory review-gate phase gates)
    plan-decompose/   description/plan → tasks.md, no speckit interview
    preflight/        fail-closed environment check at plan time
    autonomy-grant/   typed, run-bound, TTL'd operator approval ledger
    review-gate/      host-neutral cross-model pre-merge review (3 passes)
    code-uplift/      review + refactor + test-uplift for existing code
    testing-policy/   canonical testing doctrine — mock ladder, browser gate, coverage floor
    swarm/            ad-hoc task swarm, no spec dir required
    goal-wrap/        bundle current work into a tracked, anti-drift /goal prompt
  scripts/
    qa-swarm.sh       QA manifest builder (2 hooks + 3 LLM agent prompts)
    ralph-retry.sh    investigate → fix → re-qa retry loop
    browser-proof.sh  browser-QA evidence gate
    harness-audit.py  0-100 installed-harness scorer (dangling symlinks, drift)
    gsd/              gsd-run lifecycle + adversary/canary/liveness/security levers
    hooks/            debounced auto-QA, delegation-enforcer auto-pin, credential guard
  prompts/          LLM agent prompts (qa-e2e, qa-review, qa-security, decompose-spec)
  lib/              shared Python library — gates.py (evidence), dispatch.py (routing),
                    runtime_proof.py (proof verification)
  templates/        gsd-config.base.json — the canonical config template
  examples/         synthetic test spec for dogfooding
```

## License

MIT

## Credits

Built on [Claude Code](https://docs.anthropic.com/en/docs/claude-code) by
Anthropic, [gstack](https://github.com/garryslist/gstack) by Garry Tan, and
[Open GSD Core](https://github.com/open-gsd/gsd-core).
