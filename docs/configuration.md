# Configuration reference

Every knob feature-fix-swarm reads, its default, and the code that reads it.
If a setting isn't here, this package doesn't read it.

**Canonical template:** `templates/gsd-config.base.json`. `setup.sh:202`
points `GSD_MODEL_CONFIG` at it when seeding a new project. Your project's
live copy is `.planning/config.json`.

## How to change a setting

```bash
# 1. Seed the config if the project doesn't have one yet
cp templates/gsd-config.base.json .planning/config.json

# 2. Edit the key you want
$EDITOR .planning/config.json

# 3. Confirm nothing else reads a stale pin
python3 scripts/harness-audit.py
```

Env vars are set per-run, not in the config file:

```bash
GATES_STRICT=1 REVIEW_TIER=full /feature-implement 331 --autonomous
```

## An important boundary: FFS keys vs gsd-core keys

`.planning/config.json` is shared between this package and
`@opengsd/gsd-core`. Most keys in the template are read by **gsd-core, not
by FFS**. Changing them still works — it just isn't this package that acts
on them.

FFS reads exactly one config-file key directly: `model_overrides`. Everything
else in the file is gsd-core's. The table below marks each key's real
consumer so you know which project's behavior you're changing.

## `.planning/config.json`

### `model_overrides`

The one key FFS itself reads. Maps a sub-agent role to a model tier.

| Role | Default | Line |
|---|---|---|
| `gsd-planner` | `fable` | :8 |
| `gsd-plan-checker` | `opus` | :9 |
| `gsd-executor` | `sonnet` | :10 |
| `gsd-debugger` | `opus` | :11 |
| `gsd-phase-researcher` | `sonnet` | :12 |
| `gsd-project-researcher` | `sonnet` | :13 |
| `gsd-research-synthesizer` | `haiku` | :14 |
| `gsd-codebase-mapper` | `haiku` | :15 |
| `gsd-verifier` | `opus` | :16 |
| `gsd-code-reviewer` | `opus` | :17 |
| `gsd-integration-checker` | `sonnet` | :18 |
| `gsd-nyquist-auditor` | `sonnet` | :19 |

Values accept either the short alias (`"opus"`) or the full model id
(`"claude-opus-5"`). Both forms work everywhere.

Read by:
- `scripts/hooks/delegation-enforcer.sh:67,73` — auto-pins `model` on spawns that omit it
- `scripts/gsd/security-model-fence.sh:55,62-63` — rewrites planner/plan-checker fable→opus on security specs
- `scripts/gsd/model-fallback.sh:154-161` — generic value walk, rewrites any `"fable"` value in the tree
- `scripts/gsd/codex-model-sync.sh:85` — generates Codex agent TOMLs
- `scripts/harness-audit.py:111` — dead-pin lint

### Everything else in the template

These are read by `@opengsd/gsd-core`, not by FFS. Listed so you know they
exist and that editing them is a gsd-core change.

| Key | Type | Default | Line |
|---|---|---|---|
| `mode` | enum | `yolo` | :2 |
| `model_profile` | enum | `balanced` | :3 |
| `granularity` | enum | `coarse` | :4 |
| `parallelization` | bool | `true` | :5 |
| `resolve_model_ids` | bool | `false` | :6 |
| `dynamic_routing.enabled` | bool | `true` | :22 |
| `dynamic_routing.tier_models.{light,standard,heavy}` | object | `haiku` / `sonnet` / `opus` | :24-26 |
| `dynamic_routing.escalate_on_failure` | bool | `true` | :28 |
| `dynamic_routing.max_escalations` | int | `2` | :29 |
| `workflow.{research,plan_check,verifier,tdd_mode,plan_bounce,plan_bounce_passes,context_guard_mode,plan_review_convergence,security_enforcement,security_asvs_level,security_block_on}` | mixed | see template | :32-45 |
| `workflow.plan_bounce_script` | path | `scripts/gsd/plan-adversary.sh` | :37 |
| `workflow.test_command` | string | `bash scripts/gsd/gates-test-command.sh` | :39 |
| `workflow.code_review_command` | string | `bash scripts/gsd/review-gate-command.sh` | :40 |
| `git.branching_strategy` | enum | `phase` | :48 |
| `git.{phase,milestone}_branch_template` | string | see template | :49-50 |
| `review.default_reviewers`, `review.models.codex` | array/object | see template | :53-56 |
| `features.global_learnings` | bool | `true` | :59 |
| `mempalace.*` | mixed | see template | :62-67 |
| `learnings.max_inject` | int | `10` | :70 |
| `hooks.context_warnings` | bool | `true` | :73 |
| `ship.pr_body_sections` | array | `[]` | :76 |

The three `workflow.*_command` / `*_script` keys are worth calling out: FFS
ships the scripts they name, but gsd-core is what invokes them. Repointing
them swaps which FFS lever runs at that stage.

**Not in this package:** `graphify.*` and `hooks.community` appear in some
host repos' configs and are read by that repo's own hooks. FFS has zero
references to either.

## Environment variables

### Evidence and gates

| Var | Default | Consumer | Effect |
|---|---|---|---|
| `GATES_STORE` | `.feature-fix-swarm/evidence.json` | `lib/gates.py:1129` | Path to the evidence store all `gates.py` subcommands use |
| `GATES_STRICT` | unset | `lib/gates.py:1324,1413,1458` | Rejects caller-asserted evidence; only runner-executed proof counts |
| `GATES_BYPASS` | `0` | `scripts/hooks/gsd-phase-evidence-gate.sh:17` | Skips the checkbox-flip block. Manual operator corrections only |
| `TRUTH_THRESHOLD` | `0.95` | `lib/gates.py:1415` | Minimum truth score `phase-score` requires |
| `RUNTIME_PROOF_STRICT` | unset | `lib/runtime_proof.py:399` | Rejects `driver=agent` proofs |
| `GSD_PHASE_ID` | `gsd-phase` | `scripts/gsd/gates-test-command.sh:7` | Phase id used to key evidence |
| `GSD_TEST_CMD` | falls back to `.planning/gsd-test-command`, then `python3 -m pytest lib/tests -q` | `scripts/gsd/gates-test-command.sh:29-37` | The test command run and recorded as phase evidence |

### Model routing

| Var | Default | Consumer | Effect |
|---|---|---|---|
| `GSD_LEAD_MODEL` | `sonnet` | `scripts/gsd/gsd-run.sh:64` | Lead-tier alias the stateful drive launches with |
| `GSD_LEAD_EFFORT` | tier's mapped effort, else `high` | `scripts/gsd/gsd-run.sh:319` | Codex reasoning-effort override for the lead |
| `GSD_FALLBACK_CACHE` | `~/.cache/gsd-model-probe` | `scripts/gsd/model-fallback.sh:42` | Where 24h model-availability probe results cache |
| `GSD_MODEL_PROBE_TIMEOUT` | `120` | `scripts/gsd/model-fallback.sh:51` | Wall-clock bound on availability probes |
| `GSD_MODEL_PROBE_CMD` / `_CODEX` | unset | `scripts/gsd/model-fallback.sh:64,84` | Test-only probe command overrides |
| `GSD_MODEL_CONFIG` | `$PWD/.planning/config.json`, then the template | `scripts/gsd/codex-model-sync.sh:14-20` | Model-override source when no project config exists |

### Review and adversary

| Var | Default | Consumer | Effect |
|---|---|---|---|
| `REVIEW_TIER` | auto-detect | `scripts/gsd/review-tier.sh:78` | Hard override of diff-risk tier (`light\|standard\|full`) |
| `REVIEW_TIER_BASE` | `main` | `scripts/gsd/review-tier.sh:116` | Merge-base for `--all` diffs |
| `GSD_REVIEW_TIMEOUT` | `600` | `scripts/gsd/review-gate-command.sh:88` | Budget for ship-time review, both hosts combined |
| `GSD_REVIEW_MODEL_REQUEST` | `{"kind":"tier","name":"judgment"}` | `scripts/gsd/review-gate-command.sh` | Typed ship-review request; exact requests disable all fallback |
| `PLAN_ADVERSARY` | on | `scripts/gsd/plan-adversary.sh:48` | `off` skips the cross-model plan review |
| `PLAN_ADVERSARY_KEYWORDS` | `auth\|rls\|payment\|stripe\|crypto\|jwt\|...` | `scripts/gsd/plan-adversary.sh:54` | High-blast trigger set; a plan matching none skips the costly review |
| `PLAN_ADVERSARY_MODEL_REQUEST` | `{"kind":"tier","name":"judgment"}` | `scripts/gsd/plan-adversary.sh` | Typed plan-review request; legacy raw model variables fail closed |
| `PLAN_ADVERSARY_TIMEOUT` | `480` | `scripts/gsd/plan-adversary.sh` | Wall-clock cap |
| `QA_COVERAGE` | on | `scripts/gsd/qa-coverage-adversary.sh:38` | `off` skips the advisory QA-coverage critique |
| `QA_COVERAGE_MODEL_REQUEST` / `_TIMEOUT` | `{"kind":"tier","name":"execution"}` / `300` | `scripts/gsd/qa-coverage-adversary.sh` | Typed QA-coverage adversary request and budget |
| `GSD_DRIFT_MODEL_REQUEST` | `{"kind":"tier","name":"judgment"}` | `scripts/gsd/scope-drift-gate.sh` | Typed optional drift-judge request |
| `FFS_HOST` | auto-detect | `scripts/gsd/adversary-host.sh:23` | Forces which vendor counts as the orchestrating harness (`codex\|claude`) |
| `FFS_CROSS_VENDOR_FALLBACK` | on | `scripts/gsd/adversary-host.sh:78` | `0`/`off` disables the one-shot cross-vendor fallback. Also read by the plan wall's diversity-invariant reviewer selection — its state is stamped into the wall record |
| `FFS_ADVERSARY_MODEL_PROBE` | on | `scripts/gsd/adversary-host.sh:172` | `off` skips the cheap pre-review availability probe |
| `FFS_ADVERSARY_*_TIMEOUT` | 20 / 120 / 180 / 240 | `scripts/gsd/adversary-host.sh:170-278` | Per-leg probe and review caps |
| `ADVERSARY_BIN_CODEX` / `_CLAUDE` | `codex` / `claude` | `scripts/gsd/adversary-host.sh:110,139` | Executable overrides |
| `PLAN_WALL` | on | `scripts/gsd/plan-wall.sh` | `off` skips the per-phase plan wall — only with a durable, recorded waiver; a skip that cannot record its waiver fails closed |
| `SPEC_PANEL` | off | spec-authoring panel (last spec-decompose phase) | `on` enables the dual-vendor blind-draft panel at spec authoring; default off pending an EVAL-D fixture pass |

`findings-queue` (`lib/gates.py`) resolutions now require a disposition:
`gates.py findings-queue resolve --disposition refute|fix|waive --reason "…"`.
`refute` and `fix` clear the finding; `waive` records it accepted-as-is. Adding
a finding whose signature matches a RESOLVED one reopens it (prior disposition
kept in history) — a refuted-then-recurring finding blocks again rather than
staying silently cleared. The plan wall's HIGH/CRITICAL blocking check reads
`findings-queue list --unresolved --source wall --severity HIGH,CRITICAL --plan <plan>`,
so one phase's findings never block another phase's wall.

### Browser QA

| Var | Default | Consumer | Effect |
|---|---|---|---|
| `CANARY_GATE` | on | `scripts/gsd/canary-gate.sh:56` | `off` skips the fail-closed browser-QA gate |
| `CANARY_DIFF_BASE` | `origin/main` | `scripts/gsd/canary-gate.sh:32` | Base ref for the web-touch diff |
| `CANARY_WEB_PATTERN` | fixed ERE | `scripts/gsd/canary-gate.sh:61` | What counts as a web-touching file |
| `CANARY_GATE_ALLOW_STALE` | `0` | `scripts/gsd/canary-gate.sh:112` | Bypasses only the results-newer-than-HEAD check |
| `QA_BASE_URL` | unset (probes common ports) | `scripts/browser-proof.sh:72` | Pins the app URL. An unreachable pin is a hard `NO-SERVER`, no fallback probing |
| `BROWSER_PROOF_PROBE_PORTS` | `3000 3001 5173 4321 8080 8000` | `scripts/browser-proof.sh:77` | Ports probed when `QA_BASE_URL` is unset |
| `QA_FORCE_BROWSER` | `0` | `scripts/browser-proof.sh:47` | Forces `WEB-TOUCH:yes` regardless of diff |
| `QA_ALLOW_NO_SERVER` | `0` | `scripts/browser-proof.sh:84` | Explicit waiver of the no-server requirement |
| `QA_SCENARIOS` | unset | `scripts/qa-swarm.sh:212` | scenarios.md enforcing coverage completeness |

### Run lifecycle

| Var | Default | Consumer | Effect |
|---|---|---|---|
| `GSD_RUN_ID` | derived from branch `spec-NNN` | `scripts/gsd/review-gate-command.sh:24` | Ledger key for the `ship:gsd` grant check. Underivable means fail-closed REVISE |
| `TIMEOUT` | `900` | `scripts/gsd/gsd-run.sh:45` | Wall-clock bound on the whole drive |
| `GSD_HOST_PROBE_TIMEOUT` | `45` | `scripts/gsd/gsd-run.sh:46` | Bound on the pre-launch host probe |
| `GSD_RUN_STATE_DIR` | `$REPO_ROOT/.planning/run-state` | `scripts/gsd/gsd-run.sh:53` | Pidfile, status, heartbeat, reclaim mutex |
| `GSD_MACHINE_ID` | hostname | `scripts/gsd/gsd-run.sh:60` | Identity for cross-machine run-ownership contention |
| `GSD_HEARTBEAT_SECS` | `15` | `scripts/gsd/gsd-run.sh:248` | Heartbeat refresh interval |
| `GSD_FOREIGN_LEASE_SECS` | `120` | `scripts/gsd/gsd-run.sh:153` | How long a foreign machine's lease is honored before reclaim |
| `GSD_RECLAIM_LEASE_SECS` | `30` | `scripts/gsd/gsd-run.sh:155` | TTL of the reclaim mutex during stale-owner takeover |
| `LIVENESS_WINDOW_MIN` | `30` | `scripts/gsd/liveness-check.sh:42` | Freshness window for the mtime-liveness signal |
| `RUN_BOUNDED_KILL_AFTER` | `2` | `scripts/gsd/run-bounded.sh:32` | SIGTERM→SIGKILL grace period |
| `CODEX_BIN` / `CLAUDE_BIN` | `codex` / `claude` | `scripts/gsd/gsd-run.sh:443,448` | CLI executable overrides |
| `GSD_CODEX_CONFIG_ROOT` | `${CODEX_HOME:-$HOME/.codex}` | `scripts/gsd/codex-model-sync.sh:12` | Where generated Codex agent TOMLs land |
| `GSD_CLAUDE_SKILLS_ROOT` | `$HOME/.claude/skills` | `scripts/gsd/gsd-run.sh:393` | Where the Claude-side SKILL.md surface lives |
| `GSD_PLANNING_SYNC` | unset | `scripts/gsd/gsd-run.sh:check_planning_divergence` | Which side wins when `.planning/phases/<slug>` has diverged between the repo and the run worktree. `repo` copies repo→worktree, `worktree` copies worktree→repo (and re-runs the plan wall, since it retires the reviewed repo copy). Unset fails closed with exit 78; any other value fails closed |

### Kill-switches

All default to on. Set to `off` to disable.

| Var | Consumer | Disables |
|---|---|---|
| `DELEGATION_ENFORCER` | `scripts/hooks/delegation-enforcer.sh:25` | Auto-pinning `model` on unpinned sub-agent spawns |
| `SECURITY_MODEL_FENCE` | `scripts/gsd/security-model-fence.sh` | The `fable → opus` demotion of `gsd-planner`/`gsd-plan-checker` on security-touching specs |
| `CLI_HANG_GUARD` | `scripts/hooks/cli-hang-guard.sh:22` | The block on unbounded `codex exec` / `claude -p` calls |
| `CREDENTIAL_OUTPUT_GUARD` | `scripts/hooks/credential-output-guard.sh:11` | The block on commands that would print secret values |
| `TDD_GATE_BYPASS=1` | `hooks/tdd-gate.sh:12` | The block on source edits with no paired test |
| `FFS_HOST_PROCESS_DETECT` | `scripts/gsd/adversary-host.sh:42` | The PPID-walk host-detection fallback |
| `GSD_PLANNING_GUARD` | `scripts/gsd/gsd-run.sh:check_planning_divergence` | The split-brain `.planning/phases/<slug>` check on `/gsd-plan-phase` and `/gsd-execute-phase` — `off` runs the phase against whatever each side happens to hold |

The `newer=` field on a `GSD-RUN:PLANNING-DIVERGENCE` line is an **advisory
mtime heuristic**, not a merge decision: it reports which side holds the most
recently modified differing file (ties resolve to `repo`) and degrades to
`newer=unknown` if attribution fails. It never relaxes the fail-closed
refusal — only `GSD_PLANNING_SYNC` does that, and it is you who picks the side.

### Ralph loop

| Var | Default | Consumer | Effect |
|---|---|---|---|
| `RALPH_AUTO_QA` | `1` | `scripts/hooks/post-implement-batch.sh:10` | `0` disables the debounced auto-`qa-only` hook |
| `RALPH_DEBOUNCE_SECS` | `30` | `scripts/hooks/post-implement-batch.sh:38` | Quiet time before the QA watcher fires |
| `SPEC_DIR` | `specs/unknown` | `scripts/ralph-retry.sh:130` | Spec dir passed to `qa-swarm.sh` on retry |

## Model-request migration

FFS 5.0 removes raw runtime model overrides. Use one of the typed `*_MODEL_REQUEST`
variables above with either `{"kind":"tier","name":"frontier|judgment|execution|volume"}`
or `{"kind":"exact","id":"vendor-model-id"}`. `frontier` is not reachable through
dynamic escalation and is not a legal target for any of the `*_MODEL_REQUEST`
variables above today — those all resolve within judgment/execution/volume; it
is a valid request only where a role is explicitly pinned to it (`gsd-planner`).
The retired
`PLAN_ADVERSARY_MODEL`, `PLAN_ADVERSARY_EFFORT`, `PLAN_ADVERSARY_CLAUDE_MODEL`,
`QA_COVERAGE_MODEL`, and corresponding raw review/drift variables fail closed
with remediation instead of silently selecting a billing or provenance path.
Exact IDs must target a supported host (`gpt-*`/`oN*` for Codex or `claude-*`
for Claude); the dispatcher selects that host directly and never falls back.

## Related

- [Model tiers](model-tiers.md) — why the defaults are what they are
- [Choosing a command](choosing-a-command.md) — which entry point to use
- [Getting started](getting-started.md) — a first run end to end
