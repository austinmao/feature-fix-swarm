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
| `GATES_STORE` | `.feature-fix-swarm/evidence.json` | `lib/gates.py:2557` | Path to the evidence store all `gates.py` subcommands use |
| `GATES_STRICT` | unset | `lib/gates.py:4168,4267,4420` | Rejects caller-asserted evidence; only runner-executed proof counts |
| `GATES_BYPASS` | `0` | `scripts/hooks/gsd-phase-evidence-gate.sh:17` | Skips the checkbox-flip block. Manual operator corrections only |
| `TRUTH_THRESHOLD` | `0.95` | `lib/gates.py:4269` | Minimum truth score `phase-score` requires |
| `RUNTIME_PROOF_STRICT` | unset | `lib/runtime_proof.py:628` | Rejects `driver=agent` proofs |
| `GSD_PHASE_ID` | `gsd-phase` | `scripts/gsd/gates-test-command.sh:7` | Phase id used to key evidence |
| `GSD_TEST_CMD` | falls back to `.planning/gsd-test-command`, then `python3 -m pytest lib/tests -q` | `scripts/gsd/gates-test-command.sh:83-88` | The test command run and recorded as phase evidence |

### Model routing

| Var | Default | Consumer | Effect |
|---|---|---|---|
| `GSD_LEAD_MODEL` | `sonnet` | `scripts/gsd/gsd-run.sh:281` | Lead-tier alias the stateful drive launches with |
| `GSD_LEAD_EFFORT` | tier's mapped effort, else `high` | `scripts/gsd/gsd-run.sh:1360` | Codex reasoning-effort override for the lead |
| `GSD_FALLBACK_CACHE` | `~/.cache/gsd-model-probe` | `scripts/gsd/model-probe-lib.sh:38` | Where 24h model-availability probe results cache |
| `GSD_MODEL_PROBE_TIMEOUT` | `120` | `scripts/gsd/model-probe-lib.sh:40` | Wall-clock bound on availability probes |
| `GSD_MODEL_PROBE_CMD` / `_CODEX` | unset | `scripts/gsd/model-probe-lib.sh:53,75` | Test-only probe command overrides |
| `GSD_MODEL_CONFIG` | `$PWD/.planning/config.json`, then the template | `scripts/gsd/codex-model-sync.sh:14-20` | Model-override source when no project config exists |

### Review and adversary

| Var | Default | Consumer | Effect |
|---|---|---|---|
| `REVIEW_TIER` | auto-detect | `scripts/gsd/review-tier.sh:78` | Hard override of diff-risk tier (`light\|standard\|full`) |
| `REVIEW_TIER_BASE` | `main` | `scripts/gsd/review-tier.sh:116` | Merge-base for `--all` diffs |
| `GSD_REVIEW_TIMEOUT` | `600` | `scripts/gsd/review-gate-command.sh:151` | Budget for ship-time review, both hosts combined |
| `GSD_REVIEW_MODEL_REQUEST` | `{"kind":"tier","name":"judgment"}` | `scripts/gsd/review-gate-command.sh` | Typed ship-review request; exact requests disable all fallback |
| `PLAN_ADVERSARY` | on | `scripts/gsd/plan-adversary.sh:48` | `off` skips the cross-model plan review |
| `PLAN_ADVERSARY_KEYWORDS` | `auth\|rls\|payment\|stripe\|crypto\|jwt\|...` | `scripts/gsd/plan-adversary.sh:58` | High-blast trigger set; a plan matching none skips the costly review |
| `PLAN_ADVERSARY_MODEL_REQUEST` | `{"kind":"tier","name":"judgment"}` | `scripts/gsd/plan-adversary.sh` | Typed plan-review request; legacy raw model variables fail closed |
| `PLAN_ADVERSARY_TIMEOUT` | `480` | `scripts/gsd/plan-adversary.sh` | Wall-clock cap |
| `QA_COVERAGE` | on | `scripts/gsd/qa-coverage-adversary.sh:39` | `off` skips the advisory QA-coverage critique |
| `QA_COVERAGE_MODEL_REQUEST` / `_TIMEOUT` | `{"kind":"tier","name":"execution"}` / `300` | `scripts/gsd/qa-coverage-adversary.sh` | Typed QA-coverage adversary request and budget |
| `GSD_DRIFT_MODEL_REQUEST` | `{"kind":"tier","name":"judgment"}` | `scripts/gsd/scope-drift-gate.sh` | Typed optional drift-judge request |
| `FFS_HOST` | auto-detect | `scripts/gsd/adversary-host.sh:176` | Forces which vendor counts as the orchestrating harness (`codex\|claude`) |
| `FFS_CROSS_VENDOR_FALLBACK` | on | `scripts/gsd/adversary-host.sh:231` | `0`/`off` disables the one-shot cross-vendor fallback. Also read by the plan wall's diversity-invariant reviewer selection — its state is stamped into the wall record |
| `FFS_ADVERSARY_MODEL_PROBE` | on | `scripts/gsd/adversary-host.sh:372` | `off` skips the cheap pre-review availability probe |
| `FFS_ADVERSARY_*_TIMEOUT` | 60 / 120 / 180 / 240 / 480 | `scripts/gsd/adversary-host.sh:370-378,569-572` | Per-leg probe and review caps (ceilings — always clamped to the call's overall deadline). The preferred rung is the independent opposite-vendor reviewer and gets the 480 review ceiling; the same-vendor fallback keeps 240. The invariant `preferred >= fallback` holds for the DEFAULTS, asserted on both host directions in `tests/bats/adversary-host.bats`. An explicit env override stays authoritative and CAN invert it — adversary-host prints a `WARN ... BELOW ...` line naming both rungs' caps when it does, rather than clamping |
| `ADVERSARY_BIN_CODEX` / `_CLAUDE` | `codex` / `claude` | `scripts/gsd/adversary-host.sh:270,321` | Executable overrides |
| `ADVERSARY_LAST_TIER_DESCENT` | `0` | `scripts/gsd/adversary-host.sh` | Read-only signal, not an input. Set to `1` when the reviewer that answered sat on a LOWER rung than the one requested (e.g. a judgment-tier ask answered by `gpt-5.6-terra` medium). Such a review is recorded as **degraded** and prints `adversary-host: TIER-DESCENT kind=… requested=… answered=…` to stderr; in-process callers that source this lib can gate on the variable |
| `PLAN_WALL` | on | `scripts/gsd/plan-wall.sh` | `off` skips the per-phase plan wall — only with a durable, recorded waiver; a skip that cannot record its waiver fails closed |
| `PLAN_WALL_TIMEOUT` | `180` | `scripts/gsd/plan-wall.sh` | Per-plan reviewer dispatch budget (seconds) |
| `PLAN_WALL_MAX_ROUNDS` | `2` | `scripts/gsd/plan-wall.sh` | Round cap per phase (2026-08-27 one-round policy: round 1 = review, round 2 exists only to repair a CRITICAL; HIGH-only passes round 1 as PASS-RESIDUAL). A hard block on the final allowed round exits 3 with the distinct verdict `WALL-ROUND-CAP` (not `BLOCKED`, which would invite another fix round) and prints the one-command unblock: resolve the open findings, then `gates.py loop-round <RUN_ID> wall:<PHASE> --reset --max 1`. `plan-wall.sh --run` applies the same cap to the global `wall:run` counter |
| `PLAN_GATE_MAX_REPAIRS` | `1` | `skills/plan-decompose/SKILL.md` | Plan-gate repair rounds after the round-1 review. The budget is DURABLE (`gates.py loop-round "spec-<NNN>" plangate:plan`, max `1+PLAN_GATE_MAX_REPAIRS`) — re-invoking after a terminal block re-emits the block with zero dispatch; fresh budget only via `gates.py loop-round "spec-<NNN>" plangate:plan --reset --max 1` |
| `FFS_CEREMONY_TIER` | unset | `scripts/gsd/seed-ceremony-tier.sh` | Hard override (`full`\|`light`\|`adhoc`) of the seed-time ceremony classifier; unset lets the classifier decide (security keywords -> full; >20 files or >1500 est-LOC -> full; <5 files and <200 LOC -> adhoc; else light) |
| `FFS_PLAN_LENGTH_ENFORCE` | `0` | `scripts/gsd/plan-length-gate.sh` | `1` restores the blocking plan-length gate; default is advisory `PLAN-LENGTH:WARN` and non-blocking (rc 0) |
| `PLAN_WALL_REASON` | operator waiver text | `scripts/gsd/plan-wall.sh` | Waiver reason recorded when `PLAN_WALL=off`; must be non-empty |
| `PLAN_WALL_AWAIT_MAX` | `6` | `scripts/gsd/plan-wall.sh` | Caps how many `--await` calls may end pending per phase; resets on any decided outcome |
| `PLAN_WALL_AWAIT_POLL` | `15` | `scripts/gsd/plan-wall.sh` | Poll interval (seconds) while backgrounded awaiting a decided wall outcome |
| `PLAN_WALL_AWAIT_COUNT` | on | `scripts/gsd/plan-wall.sh` | `off` makes an `--await` probe budget-neutral (for evaluators) — does not consume `PLAN_WALL_AWAIT_MAX` |
| `PLAN_WALL_AUTO_RESET_MAX` | `1` | `scripts/gsd/gsd-run.sh` (`_gsd_run_wall_gate`) | Per-phase-per-run budget for the `--autonomous` rc-3 bounded auto-continue; consumed via the durable `wall-autoreset:<phase-slug>` loop-round counter, spent regardless of the re-run's outcome, never replenished mid-run. Requires an operator `wall-reset:<phase-slug>` grant |
| `SPEC_PANEL` | off | spec-authoring panel (last spec-decompose phase) | `on` enables the dual-vendor blind-draft panel at spec authoring; default off pending an EVAL-D fixture pass |
| `FFS_ENV_REGISTRY` | unset | `lib/gates.py:3724` | Path to the environment registry, ahead of `config/environments.yaml` in the resolution order. See [Environment registry](environment-registry.md) |
| `FFS_ENV_REGISTRY_REQUIRED` | unset | `lib/gates.py:3679` | `1` is the same hard mode as `--require-environments`: a registry becomes mandatory and a caller-supplied one is judged on its **HEAD** bytes, so a dirty registry can only refuse, never widen a gate |

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
| `CANARY_GATE` | on | `scripts/gsd/canary-gate.sh:67` | `off` skips the fail-closed browser-QA gate |
| `CANARY_DIFF_BASE` | `origin/main` | `scripts/gsd/canary-gate.sh:43` | Base ref for the web-touch diff |
| `CANARY_WEB_PATTERN` | fixed ERE | `scripts/gsd/canary-gate.sh:77` | What counts as a web-touching file |
| `CANARY_GATE_ALLOW_STALE` | `0` | `scripts/gsd/canary-gate.sh:133` | Bypasses only the results-newer-than-HEAD check |
| `FFS_DEPLOY_DIGEST_CMD` | unset (required) | `scripts/gsd/canary-deploy-gate.sh:99` | Shell command whose stdout is the digest actually deployed. Run TWICE — once before the probe, once again immediately after — and the two observations must be byte-identical or the run is refused |
| `FFS_DEPLOY_PROBE_CMD` | unset (required) | `scripts/gsd/canary-deploy-gate.sh:100` | Post-deploy health/smoke command; its exit code is the recorded pass/fail |
| `FFS_DEPLOY_PROBE_DIGEST_FILE` | unset (optional) | `scripts/gsd/canary-deploy-gate.sh:226` | Path the probe writes the digest it actually tested (single line, same shape rule as the query seam). Truncated before the probe runs so stale content can never satisfy it; missing/empty/malformed content after the probe, or a mismatch against the observed digest, refuses and records nothing — even with a passing probe. Closes an A→B→A flip entirely inside the probe window, which double-observation alone cannot see. The path is refused if it is a symlink, a non-regular file, or not owned by this process (checked before truncating AND before reading — `CANARY-DEPLOY-PROBE-DIGEST-UNSAFE`); the read is capped at 4096 bytes and its content is never echoed in any error |
| `FFS_DEPLOY_DIGEST_TIMEOUT` | `60` | `scripts/gsd/canary-deploy-gate.sh:106` | Wall-clock bound on each digest-query call (applies to both observations) |
| `FFS_DEPLOY_PROBE_TIMEOUT` | `300` | `scripts/gsd/canary-deploy-gate.sh:107` | Wall-clock bound on the probe |
| `QA_BASE_URL` | unset (probes common ports) | `scripts/browser-proof.sh:76` | Pins the app URL. An unreachable pin is a hard `NO-SERVER`, no fallback probing |
| `BROWSER_PROOF_PROBE_PORTS` | `3000 3001 5173 4321 8080 8000` | `scripts/browser-proof.sh:81` | Ports probed when `QA_BASE_URL` is unset |
| `QA_FORCE_BROWSER` | `0` | `scripts/browser-proof.sh:51` | Forces `WEB-TOUCH:yes` regardless of diff |
| `QA_ALLOW_NO_SERVER` | `0` | `scripts/browser-proof.sh:88` | Explicit waiver of the no-server requirement |
| `QA_SCENARIOS` | unset | `scripts/qa-swarm.sh:212` | scenarios.md enforcing coverage completeness |

`scripts/gsd/canary-deploy-gate.sh` (GH-153) is the sanctioned producer of
canary evidence for image-digest deploy surfaces — the digest equivalent of
`canary-gate.sh`'s commit-sha binding. Both `FFS_DEPLOY_DIGEST_CMD` and
`FFS_DEPLOY_PROBE_CMD` are consumer-supplied; FFS ships no platform-specific
defaults for either seam. The wrapper *observes* the digest from the query
command's stdout rather than accepting one as input — there is no `--digest`
flag or equivalent env override. It observes the digest TWICE — once before
the probe and once again immediately after — to close a TOCTOU window where
a deployment flips to a different digest mid-probe; a mismatch refuses
(`CANARY-DEPLOY-DIGEST-CHANGED`) and records nothing, regardless of the
probe's own outcome. Double-observation still admits an A→B→A flip entirely
*inside* the probe window (both observations see A while the probe tested
B); consumers whose probe can attest the digest it actually tested should
set the optional `FFS_DEPLOY_PROBE_DIGEST_FILE` to close that residual gap.
Because that path can live in a writable shared directory, the wrapper never
truncates or reads through a symlink there and never echoes the file's raw
content in an error — see the table row above.

### Run lifecycle

| Var | Default | Consumer | Effect |
|---|---|---|---|
| `GSD_RUN_ID` | derived from branch `spec-NNN` | `scripts/gsd/review-gate-command.sh:37` | Ledger key for the `ship:gsd` grant check. Underivable means fail-closed REVISE |
| `TIMEOUT` | `900` | `scripts/gsd/gsd-run.sh:254` | Wall-clock bound on the whole drive |
| `GSD_HOST_PROBE_TIMEOUT` | `45` | `scripts/gsd/gsd-run.sh:255` | Bound on the pre-launch host probe |
| `GSD_RUN_STATE_DIR` | `<git-common-dir>/ffs/gsd-run` (shared by linked worktrees); `$REPO_ROOT/.planning/run-state` outside git | `scripts/gsd/gsd-run.sh:67-73,262` | Pidfile, status, heartbeat, reclaim mutex |
| `GSD_MACHINE_ID` | hostname | `scripts/gsd/gsd-run.sh:270` | Identity for cross-machine run-ownership contention |
| `GSD_HEARTBEAT_SECS` | `15` | `scripts/gsd/gsd-run.sh:771` | Heartbeat refresh interval |
| `GSD_FOREIGN_LEASE_SECS` | `120` | `scripts/gsd/gsd-run.sh:762` | How long a foreign machine's lease is honored before reclaim |
| `GSD_RECLAIM_LEASE_SECS` | `30` | `scripts/gsd/gsd-run.sh:762` | TTL of the reclaim mutex during stale-owner takeover |
| `LIVENESS_WINDOW_MIN` | `30` | `scripts/gsd/liveness-check.sh:42` | Freshness window for the mtime-liveness signal |
| `RUN_BOUNDED_KILL_AFTER` | `2` | `scripts/gsd/run-bounded.sh:32` | SIGTERM→SIGKILL grace period |
| `CODEX_BIN` / `CLAUDE_BIN` | `codex` / `claude` | `scripts/gsd/gsd-run.sh:1943,1949` | CLI executable overrides |
| `GSD_CODEX_CONFIG_ROOT` | `${CODEX_HOME:-$HOME/.codex}` | `scripts/gsd/codex-model-sync.sh:12` | Where generated Codex agent TOMLs land |
| `GSD_CLAUDE_SKILLS_ROOT` | `$HOME/.claude/skills` | `scripts/gsd/gsd-run.sh:1532` | Where the Claude-side SKILL.md surface lives |
| `GSD_PLANNING_SYNC` | unset | `scripts/gsd/gsd-run.sh:check_planning_divergence` | Which side wins when `.planning/phases/<slug>` has diverged between the repo and the run worktree. `repo` copies repo→worktree, `worktree` copies worktree→repo (and re-runs the plan wall, since it retires the reviewed repo copy). Unset fails closed with exit 78; any other value fails closed |

### Managed run-state (opt-in, spec-014 Release B)

Release B adds a controller-owned run path next to the `gsd-run.sh` drive.
It is **off by default**, and nothing in a normal install turns it on. It is
proven on fixture hosts and one operator smoke test only. No native host
qualification has run, so treat it as experimental. Known limits today:

- `/feature-spec`, `/fix`, and `/code-uplift` have no staged `gsd-*` command
  mapping yet (the private host runtime stages only `gsd-*` skills,
  `lib/run_state/runtime_staging.py:25`), so they refuse
  `MANAGED_FRONTEND_COMMAND_UNSTAGED`.
- `/feature-implement` and `/task-swarm` map to `$gsd-execute-phase <scope>`,
  where `<scope>` is the frontend's already-selected planning scope
  (`frontend-start --scope`). No scope refuses `PRELAUNCH_PHASE_SCOPE_REQUIRED`
  before any staging. The operator's invocation text is parsed once, only to
  catch a malformed operation payload and to refuse a mode flag
  (`--dry-run` or `--adhoc`) the mapped command cannot honor
  (`MANAGED_FRONTEND_MODE_UNSUPPORTED`); it is never forwarded to the
  executor, as a command argument or otherwise. A non-default project or
  workstream never reaches the qualified host process env, so it refuses
  `MANAGED_PROJECT_SCOPE_UNSUPPORTED` instead of silently running unscoped.
- A run cannot resume after its real outer launch. A replay refuses instead
  (see the refusals below).
- A failed mapped check has no repair producer yet. It hands back and refuses
  `RECOVERY_PRODUCER_UNAVAILABLE`.

| Var | Default | Consumer | Effect |
|---|---|---|---|
| `FFS_MANAGED_INGRESS` | unset | `scripts/gsd/gsd-run.sh:22` | Any value other than `0` makes the legacy runner refuse with exit 78 and point at `scripts/gsd/ffs-frontend.sh`. Affects only runs started after it is set |
| `FFS_UPSTREAM_RUNTIME_MANIFEST` / `_SHA256` | unset (required) | `scripts/gsd/ffs-frontend.sh:17-18` | The GSD runtime descriptor written by `python3 -m run_state.cli describe-upstream-runtime`, and the SHA-256 it prints. Bound at start and rechecked on every resume (`UPSTREAM_RUNTIME_DRIFT`, exit 2) |
| `FFS_STATE_ROOT` | unset (required) | `scripts/gsd/ffs-frontend.sh:16` | Control-store root for the run (`--state-root`). Empty refuses `INVALID_REQUEST`, exit 2 (`lib/run_state/cli.py:373-382`) |
| `FFS_OBJECTIVE` | unset (required) | `scripts/gsd/ffs-frontend.sh:15` | Objective sealed into the run. Empty refuses `INVALID_REQUEST`, exit 2 |
| `FFS_INVOCATION_TEXT` | empty | `scripts/gsd/ffs-frontend.sh:14` | Invocation text sealed into the run; at most 16 KiB and no NUL bytes |
| `FFS_REQUEST_KEY` | unset (required) | `scripts/gsd/ffs-frontend.sh:19` | Idempotency key. Empty refuses `INVALID_REQUEST`, exit 2. A replay of the same key returns its recorded success or refuses; it never relaunches |
| `FFS_DISPATCH_LIMIT` | `32` | `scripts/gsd/ffs-frontend.sh:20` | Dispatch budget for the run |
| `GSD_TOKEN_BUDGET` | `250K` | `scripts/gsd/ffs-frontend.sh:21` | Token budget (`--token-limit`; accepts `K`/`M`/`B`/`T` suffixes) |
| `FFS_PROCESS_CAPACITY` | unset | `scripts/gsd/ffs-frontend.sh:22-24` | Worker capacity for the run |
| `FFS_PHASE_SCOPE` | unset | `scripts/gsd/ffs-frontend.sh:25` | Phase to run. Without it the run refuses `PRELAUNCH_PHASE_SCOPE_REQUIRED` |
| `FFS_ACCEPTANCE_DRAFT` | unset | `scripts/gsd/ffs-frontend.sh:26` | Operator-supplied acceptance draft (JSON). Its criterion ids must be the run's accepted requirement ids. Missing: `ACCEPTANCE_DRAFT_REQUIRED` |
| `FFS_REVIEW_MODEL_CATALOG` | unset | `scripts/gsd/ffs-frontend.sh:27` | Model catalog for the native final review. There is no built-in production catalog yet, so the caller supplies it |
| `FFS_HOST_KIND` | unset | `scripts/gsd/ffs-frontend.sh:28-45` | `codex` or `claude`. Needed for any run that does work: with no host the managed run refuses `HOST_CAPABILITY_UNQUALIFIED`, exit 78 (`lib/run_state/supervisor.py:3108`). Requires `FFS_HOST_TOKEN_RESERVATION` (the wrapper exits 2 without it); any other missing host field refuses `HOST_REQUEST_INCOMPLETE`, exit 2 (`lib/run_state/cli.py:345`) |
| `FFS_HOST_TOKEN_RESERVATION` | unset | `scripts/gsd/ffs-frontend.sh:31-41` | Tokens reserved per host launch, e.g. `100K` |
| `FFS_HOST_RUNTIME_HOME` | `FFS_CODEX_RUNTIME_HOME` | `scripts/gsd/ffs-frontend.sh:36` | Host runtime home. Must be an absolute, already-canonical path (`HOST_REQUEST_INVALID` otherwise; `lib/run_state/host_request.py:55-60`) |
| `FFS_HOST_BINARY` | `CODEX_BIN` | `scripts/gsd/ffs-frontend.sh:37` | Host CLI executable. Absolute, canonical path |
| `FFS_HOST_TIMEOUT` | `600` | `scripts/gsd/ffs-frontend.sh:42` | Host launch timeout in seconds, `1`-`3600` |
| `FFS_HOST_CREDENTIAL_SOURCE` | unset | `scripts/gsd/ffs-frontend.sh:44` | Required for a Claude host and refused for a Codex host; either mistake refuses `HOST_REQUEST_INCOMPLETE`, exit 2 (`lib/run_state/cli.py:346-355`) |
| `GSD_MODEL_REQUEST` / `GSD_SANDBOX_MODE` / `GSD_NETWORK_MODE` | unset / `workspace-write` / `disabled` | `scripts/gsd/ffs-frontend.sh:38-40` | Typed model request (valid JSON, else `HOST_MODEL_REQUEST_INVALID`), sandbox (`read-only`, `workspace-write`, or `danger-full-access`), and network mode for the host launch |
| `GSD_RUN_ID` / `FFS_RUN_ID` | unset | `scripts/gsd/ffs-frontend.sh:60`, `lib/run_context.py:102-112` | Run id to select. The two names are aliases; different values refuse `CONFLICTING_RUN_ID`, exit 2 |
| `GSD_RESUME` | unset | `scripts/gsd/ffs-frontend.sh:62-66` | `1` resumes; unset or `0` starts fresh; any other value exits 2 |
| `GSD_PROJECT` / `GSD_WORKSTREAM` / `GSD_SESSION_KEY` | unset | `lib/run_state/cli.py:1973-1975` | Defaults for `--project`, `--workstream`, and `--session-key` |
| `FFS_MANAGED_ADMISSION_ROOT` | `~/.local/state/feature-fix-swarm/managed-admission` | `lib/run_state/managed_admission.py:25,116-122` | Host-wide admission store shared by every managed run for this user. Must be absolute and not a symlink (`MANAGED_ADMISSION_ROOT_UNSAFE`). Test suites point it at a temporary directory |

Entry point: `bash scripts/gsd/ffs-frontend.sh <feature-spec|fix|code-uplift|feature-implement|task-swarm> [--select-file P] [--delete-file P] [--required-context P] [--project N] [--workstream N] [--session-key K]`.
It runs `python3 -m run_state.cli frontend-start` with the package's own
`lib/` first on `PYTHONPATH`. The lifecycle is execute, then mapped checks,
then one native final review, then `DONE`. A worked invocation is in
`skills/task-swarm/SKILL.md` under "Managed ingress".

**Refusals.** The wrapper's own argument checks print a plain message on
stderr and exit 2. Every refusal from `run_state.cli` prints one JSON object
on stdout:
`{"ok": false, "code": …, "cause": …, "recovery_action": {"action": …}, …}`.
Request, admission, and selection refusals use their own exit codes (for
example `INVALID_REQUEST` and `UPSTREAM_RUNTIME_DRIFT` exit 2,
`lib/run_state/cli.py:24-35`). A refusal raised inside a managed run exits 78
(`lib/run_state/cli.py:146-164`); when it has an underlying cause, it adds
`detail` with that error's type and typed code, never its message. Inside a
managed run, the recovery action depends on the code:

| Code | Meaning | `recovery_action` |
|---|---|---|
| `REQUEST_ALREADY_COMPLETED` | A launch under this request key already settled and may have done its work. It is never resumed, and a new key could repeat it | `inspect_completed_launch` |
| `INTENT_RECONCILIATION_REQUIRED` | A launch under this request key has not settled; only owner-fence reconciliation may settle it | `reconcile_intent` |
| `RETAINED_RUNTIME_NOT_REUSABLE` | The retained outer runtime cannot be resumed, and no outer launch ran under it | `resume_with_new_request_key` |
| `CHILD_RUNTIME_NOT_REUSABLE` | A wave child or final-reviewer runtime cannot be resumed. A new key would start a new outer run | `inspect_retained_child` |
| `MANAGED_FRONTEND_COMMAND_UNSTAGED` | `feature-spec`/`fix`/`code-uplift` have no staged `gsd-*` command mapping yet; only `feature-implement` and `task-swarm` do | `select_a_staged_frontend` |
| `PRELAUNCH_PHASE_SCOPE_REQUIRED` | The frontend has no selected planning scope (`frontend-start --scope`) to stage as the command argument | `supply_a_phase_scope` |
| `PRELAUNCH_PLAN_PATH_UNSAFE` | The persisted upstream planning root could not be rebased onto this run's prepared workspace | `reconcile_upstream_binding` |
| `MANAGED_FRONTEND_MODE_UNSUPPORTED` | The invocation text names a mode (`--dry-run` or `--adhoc`) the staged `gsd-execute-phase` command cannot honor | `drop_the_unsupported_mode_flag` |
| `MANAGED_PROJECT_SCOPE_UNSUPPORTED` | A non-default project or workstream never reaches the qualified host process env, so it cannot be honored | `use_the_default_project` |
| `MANAGED_PROMPT_VALUE_UNSAFE` | The planning root or project carries a control character and cannot be placed in the host prompt | `rename_the_planning_path` |
| any other supervisor code, e.g. `HOST_CAPABILITY_UNQUALIFIED`, `WAVE_EXECUTION_UNPROVEN` | The selected host backend has not demonstrated managed admission | `qualify_host_adapter` |
| a policy code, e.g. `FRONTEND_CHECK_CANDIDATE_STALE` | The managed run policy refused the transition | `correct_request` |

A replay of a run whose lifecycle already reached `DONE` returns its recorded
success without relaunching. There is no dedicated inspect or reconcile
command yet for `inspect_*` and `reconcile_intent`; they name the step an
operator takes by hand. Contract: `specs/014-parallel-host-parity/contracts/run-context.md`.

#### `run-state admission inspect|reconcile` (spec-014 Release C, F25)

The `ManagedAdmissionRefused` recovery action `inspect_managed_admission`
(above) names this CLI. It exists because a legacy (`writer_version=1`)
admission row keeps `try_admit`'s legacy-opaque gate armed for every managed
run sharing the store — including a row whose owning process died on a
prior boot, or a `waiting` v2 row whose owner died with no child ever
bound — and neither case self-heals from the normal admission path.

```bash
python3 -m run_state.cli admission inspect [--root PATH]
python3 -m run_state.cli admission reconcile [--root PATH] [--apply]
```

`--root` defaults to `global_admission_root()` (the `FFS_MANAGED_ADMISSION_ROOT`
row above); the raw, unresolved path is checked for a symlink before it is
ever resolved, so a symlinked `--root` refuses `MANAGED_ADMISSION_ROOT_UNSAFE`
rather than silently following it. **Neither command ever creates a store**:
if `<root>/admission.sqlite3` does not exist, both refuse
`MANAGED_ADMISSION_STORE_MISSING` (exit 2) without touching the filesystem —
unlike `ManagedAdmissionQueue`'s own constructor, which creates one on first
open. `reconcile` is dry-run unless `--apply` is given; there is no `--force`.

`inspect` and dry-run `reconcile` open the store strictly read-only
(`open_read_only`/`read_only_report`: a genuine sqlite `mode=ro` connection,
the same root/file safety checks as the writable path, never a create).
Neither ever migrates a v1 store or installs a fence trigger — a store that
started as `writer_version=1`/`admission_policy.version=1` stays exactly
that after any number of `inspect` or dry-run `reconcile` calls; only
`--apply` may migrate it. A schema this reader cannot make sense of
(including an `admission_policy` table with zero rows, or a file shorter
than the 100-byte sqlite header) refuses `MANAGED_ADMISSION_SCHEMA_INVALID`
instead of crashing. Before either read-only command ever opens a sqlite
connection, `_refuse_unless_legacy_journal_format` reads the file's header
bytes 18-19 directly (`O_RDONLY|O_NOFOLLOW`) and refuses
`MANAGED_ADMISSION_STORE_UNSAFE` unless they are `\x01\x01` (legacy
rollback-journal format): opening a WAL-mode database even `mode=ro`
creates `-wal`/`-shm` sidecar files as a side effect (SQLite's WAL reader
needs them to read consistently), which a strictly read-only path must
never do. A store this package creates is always DELETE-journal, so this
only fires against a tampered or foreign file. Any other sqlite-level
failure opening or reading the store (a corrupt file, a locked file) maps
to `MANAGED_ADMISSION_SCHEMA_INVALID` or `MANAGED_ADMISSION_STORE_UNAVAILABLE`
rather than a raw traceback.

Every row is probed outside SQLite (boot id and process liveness), never
assumed. `--apply` runs these same probes while holding the write lock
(below); each probe is a few local syscalls, a few milliseconds per row,
but it still counts against every *other* writer's 2-second busy timeout
for the whole duration of the apply. A row is only ever reclaimed on one
of two proofs: the recorded
owner is dead **and** its boot no longer exists (`boot-changed` — the host
rebooted, so nothing from that incarnation survives, v1 or v2, any status),
or it is a childless v2 `waiting` row whose owner died on the *same* boot
(`dead-waiter` — `try_admit` only ever grants `waiting -> active` to the
live ticket owner, so a dead-owned waiting row can never be granted by any
other path either). Everything else is kept, with a reason:
`LEGACY_SAME_BOOT_UNPROVABLE` (a same-boot v1 row has no child identity to
fall back on), `RELEASED_RETAINED` (a v2 `released` row may still be
requeued by group retry), `ACTIVE_LEASE_UNPROVABLE` (a childless v2 `active`
row's descendant cannot be ruled out), `OWNER_LIVE`, or `OWNER_UNKNOWN`.
`reclaimed` is terminal: a database trigger aborts any later write, including
raw legacy SQL, that tries to move a reclaimed row to any other status.

JSON output (stdout, one object):

```json
{
  "schema_version": 1, "ok": true, "mode": "inspect|dry-run|apply",
  "database": "…/admission.sqlite3",
  "gate_armed_before": true, "gate_armed_after": false,
  "counts": {"1/waiting": 1, "2/waiting": 3},
  "rows": [{"sequence": 1, "writer_version": 1, "status": "released",
             "boot": "…", "owner": {"host_id": "…", "boot_id": "…", "pid": 7, "start_token": "…"},
             "child": null,
             "decision": "reclaim", "proof": "boot-changed", "reason": null}],
  "backup": {"path": "…/admission.sqlite3.reconcile-<utc>-<hex>.bak", "sha256": "…"},
  "reclaimed": [1], "row_changed": []
}
```

The output **never includes a ticket value** (the row's admission ticket is a
capability secret, not diagnostic data). `gate_armed_before`/`_after` are the
same gate query try_admit itself runs (`LEGACY_OPAQUE_GATE_SQL` once the
store is v2; a v1-only, still-unmigrated store has no `writer_version`
column at all, so every row on it counts as legacy). `counts` keys are
`"<writer_version>/<status>"`. `backup` is `null` for `inspect` and for a
dry-run `reconcile`. `reclaimed`/`row_changed` are always present (sequence
numbers); both are empty for `inspect` and dry-run `reconcile`, since
neither ever applies anything.

`--apply` order (`apply_reconcile_at_root`, the one `--apply` entrypoint):
open the store directly by path (never create); take the writer's own lock
(`BEGIN IMMEDIATE`) **before any migration** — a store that started as
`writer_version=1` is migrated only after this point, so its backup (below)
still shows `admission_policy.version=1`; back the live database up with
`Connection.backup`, from a SEPARATE read-only connection to the same file
while this connection holds the writer's lock (readers are still allowed
under a `RESERVED` lock; a concurrent writer's own `BEGIN IMMEDIATE` blocks
or fails busy against it for the whole window), to an exclusively created
`0600` file named `admission.sqlite3.reconcile-<utc>-<hex>.bak` next to it;
verify the backup with `PRAGMA integrity_check` plus a row-count match
against the live table; migrate the schema if it was v1, still inside the
same lock; exact-snapshot compare-and-swap each `reclaim` decision
(`sequence`, ticket, `host_id`/`boot_id`/`pid`/`start_token`,
`writer_version`, `status`, and NULL-safe `child_host_id`/`child_boot_id`/
`child_pid`/`child_start_token` must all still match, or the row is left
alone and its sequence lands in `row_changed`, never overwritten); commit.
Any failure after the backup file is created removes it — a failed backup,
verify, migrate, or CAS leaves the live database byte-for-byte untouched and
refuses; nothing is ever applied without a verified backup on disk first,
and no `.bak` file survives a failed apply. This includes a close() failure
on the backup file's own exclusive create (the file it just made is
unlinked before the failure is reported) and any raw `sqlite3.Error` from
the writer connection itself — a contended lock, a missing table, a failed
connect — which is chained into `MANAGED_ADMISSION_STORE_UNAVAILABLE`
rather than escaping as a traceback. Once the gate has disarmed (the last
`writer_version=1` row is reclaimed), the same transaction also clears any
`'legacy-opaque'` tag left on a waiting row, the same cleanup `try_admit`
performs when it notices the gate is clear — an apply that clears the last
legacy row leaves no stale tag behind for the next `try_admit` to find.

Exit codes: `0` ok; `2` `MANAGED_ADMISSION_STORE_MISSING` / `INVALID_REQUEST`;
`3` `LEGACY_OPAQUE_REMAINS` — `--apply` ran but at least one row is still
kept, so the gate is still armed; `4` `ROW_CHANGED` — `--apply` ran but at
least one `reclaim` decision's exact snapshot no longer matched. In normal
use this should not occur: the CLI builds the plan from the same rows it
CASes, under the one continuous writer lock it holds from before the
backup through the commit, so there is no external window for anything to
change the snapshot in between. It exists as a defensive guard (a stale
snapshot is reported, never silently overwritten) rather than a plan that
is ever expected to go stale in practice; `5` `RECONCILE_BACKUP_FAILED`
(rolled back, database untouched, no `.bak` left); `6`
`MANAGED_ADMISSION_STORE_UNSAFE` / `_STORE_UNAVAILABLE` / `_SCHEMA_INVALID`
/ `_ROOT_UNSAFE` / `_IDENTITY_UNKNOWN` (the caller's own process identity
could not be captured). Both exit 3 and exit 4 payloads carry `code`,
`cause`, and `recovery_action` alongside `ok: true`, the same shape as a
`ManagedAdmissionRefused` envelope, even though the operation itself did
not raise.

**Operator order:** `inspect` first (read-only; confirms which rows are
actually reclaimable and why the rest are kept) -> `reconcile` with no flags
(dry run; same report, still no write) -> `reconcile --apply` only once the
dry-run plan looks right. Never run `--apply` against a shared per-user store
without reading the dry-run plan first — reclaiming a row is a one-way,
terminal decision.

**Backup-file TOCTOU window (F25 review round 1, finding 7):** the backup
file is created via an exclusive `O_EXCL|O_NOFOLLOW` open, then reopened by
pathname (for the sqlite backup call, the integrity-check read, and the
sha256 hash) rather than held open across all three. Chosen response:
**document the existing same-UID trust boundary rather than add a private
`mkdtemp` staging directory.** `_lstat_root_and_path` already requires the
admission root to be a `0700` directory owned by the calling UID before
either command does anything; only that same UID can create, replace, or
symlink anything inside it during the reopen window. That UID already has
full read/write access to the live `admission.sqlite3` and to the calling
process itself, so closing the window would not remove any privilege a
same-UID actor doesn't already have — it would only harden against a
different-UID attacker, who is already excluded by the root's `0700` check.

**Trust boundary.** The store is always opened by pathname (`root /
"admission.sqlite3"`), never by an already-held file descriptor passed in
from outside. `_lstat_root_and_path` checks only the leaf root directory:
owned by the calling UID, mode `0700`, and (per `_read_root_and_path`,
checked on the raw path before it is ever resolved) not a symlink. Nothing
above that leaf is re-checked — every ancestor directory is trusted exactly
the way the rest of the calling user's home directory already is; this
package adds no additional isolation above the leaf. Consequently, **a
multi-user host must never point `FFS_MANAGED_ADMISSION_ROOT`
(`GLOBAL_ROOT_ENV` in `managed_admission.py`) at a path under a shared,
writable ancestor directory** (for example anything under a
world-or-group-writable `/tmp`, or a shared project directory writable by
more than the intended UID) — a leaf directory can be `0700` and
owner-correct while an untrusted party still controls an ancestor and could
replace the leaf itself between checks. The default,
`~/.local/state/feature-fix-swarm/managed-admission`, is safe because
`$HOME` itself is expected to be single-user.

**`managed_resource_group.py`'s parent-ticket watchdog caller is still
unwired** (F25 design section 4's `admission_verdict` plumbing): the
`WatchdogTarget` it constructs for a prepaid parent group's pre-reservation
polling loop never carries an `admission_verdict`, unlike
`shared_resources.py`'s `_AdmissionWatchdogRegistry`. Deferred, not a
regression — no ticket exists yet at that call site for most of the loop's
iterations, and wiring it without a covering test risked a silent bug in an
already-complex prepaid-group path.

### Kill-switches

All default to on. Set to `off` to disable.

| Var | Consumer | Disables |
|---|---|---|
| `DELEGATION_ENFORCER` | `scripts/hooks/delegation-enforcer.sh:25` | Auto-pinning `model` on unpinned sub-agent spawns |
| `SECURITY_MODEL_FENCE` | `scripts/gsd/security-model-fence.sh` | The `fable → opus` demotion of `gsd-planner`/`gsd-plan-checker` on security-touching specs |
| `CLI_HANG_GUARD` | `scripts/hooks/cli-hang-guard.sh:22` | The block on unbounded `codex exec` / `claude -p` calls |
| `CREDENTIAL_OUTPUT_GUARD` | `scripts/hooks/credential-output-guard.sh:13` | The block on commands that would print secret values |
| `TDD_GATE_BYPASS=1` | `hooks/tdd-gate.sh:12` | The block on source edits with no paired test |
| `FFS_HOST_PROCESS_DETECT` | `scripts/gsd/adversary-host.sh:195` | The PPID-walk host-detection fallback |
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
