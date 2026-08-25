# Feature Fix Swarm

[![CI](https://github.com/austinmao/feature-fix-swarm/actions/workflows/ci.yml/badge.svg)](https://github.com/austinmao/feature-fix-swarm/actions/workflows/ci.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/austinmao/feature-fix-swarm/badge)](https://scorecard.dev/viewer/?uri=github.com/austinmao/feature-fix-swarm)
[![Latest release](https://img.shields.io/github/v/release/austinmao/feature-fix-swarm)](https://github.com/austinmao/feature-fix-swarm/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## Ship agent-built features without letting early mistakes compound

Feature Fix Swarm (FFS) is a safety and orchestration layer for developers
using Claude Code or Codex on work that spans many tasks. It tests every phase
before the next phase begins, records evidence for completion claims, and asks
a fresh reviewer to challenge the result before anything ships.

It is for maintainers who want the speed of coding agents without accepting a
single end-of-run “looks good” as proof.

[Get started](docs/getting-started.md) · [Choose a workflow](docs/choosing-a-command.md) · [Read the security model](SECURITY.md)

## The problem it solves

Long agent runs often decompose a feature into dozens of tasks and postpone QA
until the end. If task 3 introduces a bad assumption, tasks 4–30 can build on
it. The final review then has to untangle both the original defect and every
decision downstream of it.

FFS puts a boundary around each phase:

- Deterministic tests must pass before progress advances.
- Every phase's plan clears an adversarial plan wall before implementation
  starts, reviewed by a model distinct from the one that wrote it.
- Review findings are severity-tagged, persisted, and rechecked.
- High-risk completion claims receive an adversarial review from a fresh model
  and, when available, the opposite host vendor.
- Interrupted runs resume from recorded state instead of replaying a model's
  hidden context.
- Push, merge, deploy, and unsandboxed actions remain explicit, run-bound
  operator decisions.

FFS is the full development pipeline, not a single check: spec → plan → phase
execution → deterministic QA gates → adversarial review → gated ship. It
runs on two disciplines throughout — orchestration (typed workflows, resumable
state) and delegation (typed model tiers routing each seat to the model suited
to it, producer never equal to reviewer).

## How it works

```text
idea or bug
    │
    ▼
specify and plan ──► preflight environment and approvals
    │
    ▼
execute one phase ──► deterministic tests + focused QA
    │                         │
    │                    failure: investigate, fix, retest
    ▼
next phase
    │
    ▼
completion proof ──► adversarial review ──► approved ship action
```

Claude and Codex are host adapters over the same skills and state. Examples in
this README show both invocation forms: `$skill` in Codex and `/skill` in
Claude Code.

## Quick start

You need macOS or Linux, Git, Bash or zsh, Node.js 22+ with npm 10+, Python
3.11+, and at least one supported host: [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
or [Codex CLI](https://github.com/openai/codex).

```bash
git clone https://github.com/austinmao/feature-fix-swarm.git
cd feature-fix-swarm
npm ci
bash setup.sh --scope user
bash setup.sh --doctor --scope user
```

For a portable, repository-local installation:

```bash
bash setup.sh --scope project --project-dir /absolute/path/to/your/repo
bash setup.sh --doctor --scope project --project-dir /absolute/path/to/your/repo
```

Then start with a bounded dry run:

```text
Codex:  $task-swarm "add rate limiting to webhook endpoints" --dry-run
Claude: /task-swarm "add rate limiting to webhook endpoints" --dry-run
```

Inspect the proposed tasks and gates, then remove `--dry-run` when the plan is
right. The full walkthrough is in [Getting started](docs/getting-started.md).

## Pick the right entry point

| You have… | Codex | Claude | Result |
| --- | --- | --- | --- |
| A fuzzy idea | `$office-hours` | `/office-hours` | Clarifies audience, problem, and scope; provided by optional gstack |
| A reproducible bug | `$fix "symptom"` | `/fix "symptom"` | Diagnose, test, repair, and regression-check |
| A bounded task | `$task-swarm "task"` | `/task-swarm "task"` | Lightweight plan, phase execution, and finish gates |
| A substantial feature | `$feature-spec "feature"` | `/feature-spec "feature"` | Durable spec, plan, tasks, preflight, and implementation |
| An existing FFS/GSD plan | `$feature-implement NNN` | `/feature-implement NNN` | Resume phase-by-phase execution |
| A shipped feature people must learn | `$spec-guide NNN` | `/spec-guide NNN` | Verified developer, admin, and user instructions across every delivery vehicle |
| More branches than anyone can track | `$git-branch-consolidate` | `/git-branch-consolidate` | Read-only estate audit: what already landed, what still owes work or tests, and the ordered path back to one `origin/main` |
| An audited cleanup set to execute | `$git-branch-cleanup` | `/git-branch-cleanup` | CI-gated merges of non-gated PRs, then pruning merged refs and their worktrees |

See [Choosing a command](docs/choosing-a-command.md) for trade-offs and output
artifacts.

## Why FFS uses GSD

[Open GSD Core](https://github.com/open-gsd/gsd-core) is the upstream planning
and execution engine. It owns the `gsd-*` skills, agents, hooks, manifests,
plan/execute/verify loop, and `.planning` state format.

FFS does not fork or rewrite that engine. It exact-pins
`@opengsd/gsd-core@1.10.0`, invokes GSD's own installer for complete Claude and
Codex profiles, and layers controls around it:

| GSD provides | FFS adds |
| --- | --- |
| Plan, execute, and verify primitives | Host-neutral feature and fix workflows |
| Phase state and resumable orchestration | Deterministic completion evidence and phase gates |
| Upstream skills, agents, hooks, and manifests | Hash-managed install, migration, doctor, backup, and rollback |
| Worktree-aware execution | Shared locking, skill/model/CLI drift refusal, and bounded resume |
| Model-driven implementation | Typed model tiers and fresh cross-model adversarial review |
| Runtime hooks | Sandbox, network-mode, credential-refresh, and operator-grant safety walls |

That boundary is deliberate: GSD can evolve upstream while FFS stays focused
on integration, safety, and proof.

## Dependencies and trust boundaries

| Dependency | Required? | Installed by FFS? | Why it exists |
| --- | ---: | ---: | --- |
| Claude Code or Codex CLI | One required | No | The interactive agent host |
| Open GSD Core `1.10.0` | Yes, exact pin | Yes, through its upstream installer | Planning, execution, verification, and runtime manifests |
| Python 3.11+, Node 22+/npm 10+, Git, Bash/zsh | Yes | No | Installer, gates, tests, and worktrees |
| `prompt-master` at commit `d15eab…` | Yes | Yes, pinned and compatibility-patched | Host-aware prompt refinement without copying an unreviewed moving branch |
| [`socratic`](https://github.com/m4vic/socratic) at commit `8c7e1f…` | Yes | Yes, pinned by default; skippable with `FFS_SKIP_SOCRATIC=1` | Pinned question bank the spec, plan-wall, plan-decompose, and review-gate seams slice from ([how it is used](docs/socratic.md)) |
| The opposite host CLI | Recommended | No | Stronger cross-vendor adversarial review; otherwise review is marked degraded |
| [gstack](https://github.com/garrytan/gstack) | Optional integration | No | Idea shaping, browser QA, investigation, review, and shipping skills |
| [GitHub Spec Kit](https://github.com/github/spec-kit) | Needed for the full spec bootstrap | No | Produces durable feature specs and plans before FFS decomposition |
| External agent catalogs | Optional | No | Adds specialized roles; FFS discovers local roles and retains a generic floor |
| GitHub CLI (`gh`) | Yes | No | Ship/finalize tail (`assert-merged`, run watching) has no fallback without it |
| `jq` | Yes | No | Canary gate and QA-coverage adversary JSON handling |
| POSIX tool floor: `shasum`/`sha256sum`, `ps`; optional `curl`, `flock`, `timeout`/`gtimeout`, `tmux` | Required floor; optional extras degrade fail-soft | No | Evidence hashing, host detection, readiness probes, locking, bounded runs, worktree GC |
| `filelock` (Python, `>=3.30,<4`) | Yes | Yes, via `deps.sh install` | Coordination-layer locks and liveness probes (`scripts/coord/coord.py`) |
| Optional QA lane: `npx`/vitest, `playwright`, `canary`; optional tooling: `bats`, `shellcheck`, `slopcheck`, `gbrain` | Optional | No | Browser-proof QA, contributor tests/lint, package-legitimacy verdicts, workspace memory |

`bash scripts/gsd/deps.sh check` is the executable form of this table — it
probes every row and prints the exact install command for anything missing;
`/ffs-init` runs it first and auto-installs the repo-scoped rows. See
[Initialization](docs/initialization.md).

The complete ownership, pinning, update, and degradation rules are documented
in [Dependencies and integrations](docs/dependencies.md). Installation details
and rollback semantics are in [Installer, migration, and rollback](docs/installer.md).

## Safety defaults

- No mandatory completion or ship review fails open.
- No default dangerous sandbox bypass is used.
- Network access is binary (`none` or `enabled`) and is recorded honestly;
  purpose labels are audit metadata, not claimed domain enforcement.
- Unsandboxed execution requires a 72-hour, run-bound
  `sandbox:danger-full-access` grant that is consumed at launch.
- Codex OAuth state is copied writable into an isolated runtime and synchronized
  back only with compare-and-swap protection against concurrent refreshes.
- Exact model requests do not silently fall back; degraded tier fallback cannot
  satisfy an exact-provenance gate.
- Every phase plan clears an always-on adversarial wall (producer never equal
  to reviewer) before that phase can be marked complete; a skip requires a
  durable, recorded waiver.
- Project/user installs are manifest- and hash-managed. Edited or unknown files
  are preserved instead of overwritten; `--adopt-collisions` opts into backing
  up and replacing a specific colliding path instead of hard-blocking the
  whole install.

See [Security policy](SECURITY.md) and the
[public-launch security review](docs/security-audit-2026-08-01.md).

## Model routing

FFS requests workload intent rather than scattering vendor IDs through skills:

| Tier | Claude default | Codex default | Effort | Typical work |
| --- | --- | --- | ---: | --- |
| `frontier` | Claude Fable | GPT-5.6 Sol | xhigh | Planning — the low-volume, highest-leverage seat |
| `judgment` | Claude Opus | GPT-5.6 Sol | high | Checking, debugging, verification, code/security review |
| `execution` | Claude Sonnet | GPT-5.6 Terra | medium | Implementation, research, integration, orchestration |
| `volume` | Claude Haiku | GPT-5.6 Luna | low | Mapping, synthesis, status collection (bounded-context inputs only) |

The producer and reviewer must resolve to different models — enforced end to
end, including a per-phase plan wall that reviews every plan before its phase
can execute. `frontier` is deliberately unreachable via dynamic escalation; it
is assigned only where a role is explicitly pinned to it. Raw vendor model IDs
are allowed only through an explicit exact request. See
[Model tiers](docs/model-tiers.md) for host mappings, fallback provenance, and
the legacy alias bridge.

## Supported environments

- macOS with Bash or zsh
- Ubuntu with Bash
- Codex CLI `>=0.137.0,<0.148.0` (`0.146.x` and `0.147.x` are the tested lines)
- Claude Code using the corresponding host-native skills
- Windows is not currently supported

Run `setup.sh --doctor` after installation and before upgrading or resuming an
important run. A different FFS version in project and user scope, a changed
skill hash, or Codex CLI version drift is an actionable failure.

## Documentation

| Guide | What it answers |
| --- | --- |
| [Getting started](docs/getting-started.md) | How do I reach a first dry run? |
| [Initialization](docs/initialization.md) | What does /ffs-init set up, and what if I skip it? |
| [Choosing a command](docs/choosing-a-command.md) | Which workflow fits this job? |
| [Dependencies and integrations](docs/dependencies.md) | What is installed, who owns it, and why? |
| [Pipeline](docs/pipeline.md) | How do the stages connect? |
| [Commands](docs/commands.md) | What commands and gates are available? |
| [Configuration](docs/configuration.md) | What can I tune? |
| [Installer, migration, and rollback](docs/installer.md) | How is FFS installed, migrated, and undone? |
| [Environment registry](docs/environment-registry.md) | What environments exist, and how does `/ffs-init` declare them? |
| [CI templates and test tiers](docs/ci-templates-and-tiers.md) | Which workflows does FFS propose, and where do CI test commands come from? |
| [Coordination](docs/coordination.md) | How do sessions avoid colliding on the same spec? |
| [Cross-session messaging](docs/cross-session-messaging.md) | How does one session hand work to another? |
| [Healing](docs/healing.md) | How do waiting runs wake, recover, and remain bounded? |
| [Retro loop](docs/retro.md) | What may the consent-gated diagnostic loop file, and how is it triaged? |
| [Model tiers](docs/model-tiers.md) | Which model runs what, and how is provenance enforced? |
| [Socratic](docs/socratic.md) | What is the pinned question bank, and where does it reach a reviewer? |
| [Browser proof](docs/browser-proof.md) | What counts as browser-QA evidence? |
| [Digest](docs/digest.md) | How do I see what happened without watching a run? |
| [Promotion protocol](docs/promotion-protocol.md) | How does staging evidence authorize production? |
| [Security audit](docs/security-audit-2026-08-01.md) | What was checked before public promotion? |
| [Public launch checklist](docs/public-launch-checklist.md) | What remains before announcing broadly? |

## Contributing and support

Bug reports and focused pull requests are welcome. Start with
[CONTRIBUTING.md](CONTRIBUTING.md), use the issue forms, and follow the
[Code of Conduct](CODE_OF_CONDUCT.md). Usage questions belong in GitHub
Discussions once enabled; suspected vulnerabilities must use the private
reporting path in [SECURITY.md](SECURITY.md), not a public issue.

## License

[MIT](LICENSE) © Austin Mao and contributors.
