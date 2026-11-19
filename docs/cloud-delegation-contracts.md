# Cloud delegation contracts (PR 1a, `/swarm --cloud`)

Frozen interface for builders B1–B8. It folds in every rev 6 review finding (Fable PASS-WITH-FIXES, Astra REJECT with fold-in findings). Where this document and plan rev 6 differ, this document wins. Base: FeatureFix Swarm origin/main 919c045 plus PR 0. Section 15 records where the built code differs from sections 1 to 14 after the fix round. Code is the source of truth, and section 15 overrides any earlier section it conflicts with.

Operator decisions that bind this document: `--cloud` is opt-in; only `/swarm` gets it in 1a; a connected group of tasks goes to the cloud only if every task in it is cloud-eligible, otherwise the whole group runs locally as today; cloud secrets are an enabled path; a SessionStart hook is committed in the consumer repo; Claude results bind by nonce without a per-result confirm; OAuth only; no merge, no deploy.

## 1. Identifiers and paths

| Thing | Rule |
|---|---|
| run id `R` | `^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$` (40 max, so lifecycle id and reconcile claim fit 64 bytes) |
| task id `T` | `^[a-z0-9][a-z0-9-]{0,23}$`, unique in the run, must pass `git check-ref-format --branch` |
| generation `G` | integer ≥ 1, bumped by `abandon` |
| epoch `E` | integer ≥ 1 in the run ledger, bumped only while holding the orchestrator lock |
| lifecycle id | `cloud-<R>-e<E>` |
| task branch | `refs/heads/ffs/cloud/<R>/<T>-g<G>` (pushed, immutable after creation) |
| integration ref | `refs/heads/ffs/cloud/<R>/integration` (local; pushed only as a successor's base under the task-branch name) |
| candidate ref | `refs/ffs/cloud/<R>/cand/<T>-g<G>` (local only, keeps objects alive) |
| Claude result branch (requested) | `claude/ffs-<R>-<T>-<nonce8>` |
| manifest path on a result | `.ffs-result/<T>.json` |
| state dir `D` | `<main worktree>/.feature-fix-swarm/cloud/` |
| run ledger | `D/<R>.json`, lock `D/<R>.lock` |
| slots ledger | `D/providers.json`, lock `D/providers.lock` (one lock serializes all runs) |
| orchestrator lock | `D/<R>.orch.lock` (content: pid) |
| logs | `D/<R>/launch-<T>-g<G>.log`, `D/<R>/verify-<T>-g<G>.log`, `D/<R>/orchestrate.log` (mode 0600) |
| consumer config (committed) | `.ffs/cloud/config.json`, `.ffs/cloud/setup.sh`, `.ffs/cloud/env-check.sh`, `.ffs/cloud/session-start.sh` |

All ledger writes: `flock` on the sibling lock, read, check `revision`, write temp in the same directory, `fsync` file, `os.replace`, `fsync` directory, `revision += 1`. Implemented once in `lib/cloud_ledger.py` (`locked_update(path, fn)`); no other module writes these files.

## 2. Data shapes

### 2.1 Normalized task (router input; produced by `/swarm` Step 1 inside the cloud block)

```json
{"id":"t1","title":"…","prompt":"…","tier":"execution|volume|judgment|frontier",
 "model_request":{"kind":"tier","name":"execution"},
 "owns":["src/a/**"],"owns_source":"annotated|estimated|missing",
 "est_minutes":45,"needs_secrets":["NAME"],"local_only":false,
 "local_reason":"","depends_on":["t0"],"kind":"build|review|verify|search",
 "verify_cmd":"npm test -- a"}
```

Tasks-file annotations: `[owns: a/**, b.py] [est: 45m] [needs-secret: NAME] [local] [after: t0] [verify: cmd]`.

### 2.2 Route plan (router output; `plan` prints it; `--dry-run` shows it)

```json
{"run":"R","policy":"auto|prefer|required","groups":[{"id":"g1","tasks":["t1","t2"],"route":"cloud|local","reasons":["…"]}],
 "tasks":{"t1":{"route":"cloud|local","provider":"claude|codex|null","reasons":["code"],"model":"…","effort":"…","provenance":"requested-unverified|provider-default-unverified","wave":0}},
 "budgets":{"claude":3,"codex":3,"total":6,"max_local_verifiers":1},
 "refused":[{"task":"t3","code":"CLOUD_REQUIRED_NOT_ELIGIBLE","reasons":["…"]}],
 "secret_needs":{"t1":["NAME"]},"local_slots_saved":2}
```

Router rules, in order:
1. Per-task eligibility. Not eligible when: `local_only`; `kind` is review or verify; `model_request.kind == "exact"`; tier frontier; `owns_source == "missing"`; `owns` intersects the denylist (§7); no non-blank `verify_cmd` from the task or `config.verify.cmd` (both are stripped; whitespace-only is blank); a needed secret name is not declared for any provider, or lacks a `secret-use:<NAME>` grant (autonomous) or operator yes (interactive).
2. Run-level refusals (everything stays local; `required` fails): dirty base; missing `cloud-dispatch:<provider>` or push grant; `doctor` not green; `FFS_MANAGED_INGRESS=1`; a run token budget is set; lifecycle disabled.
3. Groups are connected components of the `depends_on` graph. A group is `cloud` only if every task is eligible **and** at least one task has `est_minutes ≥ threshold` (20; `prefer` 5; host pressure `high` 5). Otherwise the whole group is `local`.
4. Within a cloud group, two tasks whose `owns` overlap get an implicit dependency edge in file order (they serialize).
5. Provider: tasks with `needs_secrets` → the first provider (claude, then codex) declaring all names; judgment tier → claude; otherwise the provider with more free slots, alternating on ties. Codex only takes execution and volume tiers.
6. `required`: any ineligible task → the command exits 3 and launches nothing.

### 2.3 Run ledger

```json
{"schema":1,"revision":7,"run":"R","state":"active|parked|done","epoch":1,"policy":"auto",
 "base_sha":"…","origin":"<remote name>","created_at":0,
 "integration":{"tip":"<oid>","history":[{"task":"t1","generation":1,"publish_oid":"…","candidate_oid":"…","tree":"…","test_rc":0,"test_log_sha":"…"}]},
 "journal":[{"seq":1,"op":"integrate|reserve|admit|bind|park","task":"t1","generation":1,"data":{},"done":false}],
 "tasks":{"t1":{"state":"reserved","generation":1,"provider":"claude","depends_on":[],"owns":[],"verify_cmd":"…",
   "nonce":"<32 hex>","task_branch":"…","task_branch_oid":"…","base_oid":"…",
   "remote_id":null,"url":null,"launched_at":0,"deadline_at":0,"poll_errors":0,
   "bound_oid":null,"seen_oid":null,"seen_count":0,"candidate_oid":null,
   "review":null,"verify":null,"blocker":null,"decisions_needed":[],"env_report":null,
   "rejected_candidates":[["ref","oid"]]}}}
```

Task states: `admitted → reserved → submitting → launched → running → result-ready → collected → reviewed → verified → integrating → integrated`. Side states: `uncertain`, `needs-operator`, `superseded`, `rejected`, `failed`, `abandoned`. Terminal: `integrated`, `rejected`, `failed`, `abandoned`, `superseded`. `needs-operator` and `uncertain` are not terminal. A task whose dependency is terminal and not `integrated` becomes `failed` with `blocker: dependency-<state>`.

The ledger never stores raw provider output, prompts, or secret values. `remote_id` and `url` are validated against `^[A-Za-z0-9_.:/-]{1,200}$` and scanned before storage.

### 2.4 Slots ledger

```json
{"schema":1,"revision":3,"limits":{"claude":3,"codex":3,"total":6},
 "slots":[{"key":"R/t1/g1","run":"R","provider":"claude","reserved_at":0,"deadline_at":0}]}
```

Reservation is journaled: run journal `reserve` (done false) → slots ledger add (idempotent on `key`) → run journal done. Any orchestrator may reap a slot when `now > deadline_at + 3600` or when the owning run ledger is missing or `done`.

### 2.5 Manifest v1 (written by the worker at `.ffs-result/<T>.json`)

```json
{"schema":1,"run":"R","task":"t1","generation":1,"nonce":"<32 hex>","base_sha":"<oid>",
 "status":"complete|blocked","summary":"≤2000 chars","decisions_needed":["…"],
 "blockers":["…"],"env_report":{"tools":{"node":"v22"},"present":["NAME"],"missing":["NAME"]},
 "tests_run":[{"cmd":"…","rc":0}]}
```

Validator: exact key set, types, lengths (every string ≤ 4000, lists ≤ 50), `env_report` values are names and version strings only. Every string is scanned (§8) and is untrusted data. Each `tests_run` item is an object with exactly the keys `cmd` (string) and `rc` (integer). The worker packet states this shape (`lib/handoff_packet.py`); a live worker wrote strings before the packet did. Codex results have no worker manifest; the dispatcher synthesizes one with `status: complete` and no nonce check.

### 2.6 `.ffs/cloud/config.json` (committed in the consumer repo; names only)

```json
{"schema":1,"limits":{"claude":3,"codex":3,"total":6,"max_local_verifiers":1},
 "verify":{"cmd":"","timeout_s":900,"deps":["node_modules",".venv"]},
 "secrets":{"provider":"none|provider-env|doppler|command","wrapper":"doppler run --","install":"",
            "names":{"claude":["NAME"],"codex":["NAME"]},"network_secret_hosts":["api.example.com"]},
 "codex":{"env_id_var":"FFS_CODEX_CLOUD_ENV"},"deadline_hours":12}
```

## 3. CLI: `scripts/gsd/cloud-dispatch.sh` (thin wrapper; `exec python3 <lib>/cloud_dispatch.py "$@"`)

Library lookup: `$FFS_LIB_DIR`, then `<script>/../../lib`, then the managed lib root. Output lines start with `CLOUD:`. Exit codes: 0 ok; 2 usage; 3 refused (`CLOUD-REFUSED: <CODE> <detail>`); 4 ok but operator items exist; 75 the orchestrator lock stayed held when `orchestrate --wait-lock` gave up (nothing else returns 75); 78 store or config error.

| Verb | Arguments | Effect |
|---|---|---|
| `plan` | `--tasks FILE.json --policy P [--run R]` | Pure. Prints the route plan JSON. No writes, no network. A task id the scanner flags is `SECRET_IN_PROMPT` (exit 3) before any output, as in `start`. |
| `start` | `--run R --tasks FILE.json --policy P --root ABS [--foreground]` | Runs `doctor`, checks grants, writes the run ledger with cloud tasks as `admitted` or waiting, then spawns the detached orchestrator. Prints `CLOUD:started run=R tasks=N` and `CLOUD:local tasks=<ids>` (always; empty when no task is local). `start` holds the run's orchestrator lock while it reserves the run, creates the integration ref and commits the initialization record (with `integration_ref_created`). An existing integration ref is never adopted: `INTEGRATION_REF`, or `INTEGRATION_REF_UNCONFIRMED` (exit 78) for an identical retry of a crashed start. It scans every task id (local ones too) and the task text first: a match is `SECRET_IN_PROMPT`, naming only the scanner class. A run id that already has a ledger ends `RUN_EXISTS ... batch=same\|other\|unknown`: `batch=same` only for an exact match of the first batch (task ids, content, `depends_on` and input order); `local-claimed=<ids>` lists the local tasks the first attempt claimed (never offered as `local=` again); `orchestrator=restarted` means an identical retry restarted an active run that no orchestrator owned; `orchestrator=restart-failed` means it tried and the spawn raised `OSError` (same exit 3, the claims stand, the same retry tries again). A run with no initialization record (also one made by an earlier build) makes `orchestrate` refuse with `RUN_NOT_INITIALIZED`. |
| `orchestrate` | `--run R --root ABS [--resume] [--detach] [--once] [--wait-lock SECONDS]` | §4. `--once` runs a single tick (tests). `--wait-lock` blocks on the orchestrator lock for up to that many seconds and exits 75 if it stays held. |
| `status` | `--run R [--json]` | Read-only: per task state, provider, URL, branch, generation, review, blockers, decisions; orchestrator pid and heartbeat; integration tip. |
| `doctor` | `[--root ABS] [--json]` | §10. Exit 0 green, 3 not green. |
| `env init` | `[--register-hook] [--force]` | Writes the four `.ffs/cloud/` files from templates; with `--register-hook`, merges the SessionStart entry into `.claude/settings.json`. Prints what to commit. Never commits. |
| `env-spec` | `--provider claude\|codex` | Prints the one-time UI steps (§9). |
| `cleanup` | `--run R [--remote] [--discard-unmerged]` | Deletes the local refs the ledger accounts for, each only at its recorded OID, and the state of a `done` run. A ref with no recorded OID, or with no ledger proof the run created it (the admit journal's `created` for a task branch, `integration_ref_created` for the integration ref), is kept and printed as `CLOUD:cleanup-kept ref=... (creation not confirmed)` without failing the cleanup; a ref that moved off its OID gives `CLEANUP_INCOMPLETE` and keeps the ledger; a ref name the scanner flags prints as `[withheld sha256:<12 hex>]`. `--remote` deletes the pushed task branches with a lease on the recorded OID (needs the push grant) and never deletes a remote `integration` branch. Refuses with `INTEGRATION_UNMERGED` while the integration ref holds commits no other branch contains, unless `--discard-unmerged`; also when git fails reading the integration ref (an absent ref is fine). |
| `adopt` (operator-only) | `--run R --task T (--oid OID \| --remote-id ID)` | Exactly one of the two. `--oid` binds a specific Claude result OID to a `needs-operator` or `uncertain` task; it re-enters at `result-ready`; refused for Codex tasks. `--remote-id` binds a Codex session id; the task re-enters at `launched` with a new deadline; refused for Claude tasks. |
| `abandon` (operator-only) | `--run R --task T [--retry]` | Bumps the generation; state `abandoned`, or `waiting` again with `--retry`. Refused (`INTEGRATION_IN_PROGRESS`) while the task is `integrating`. `--retry` on a `rejected` or `failed` task of a `done` run makes the run `active` again. |
| `resolve` (operator-only) | `--run R --task T --action retest\|accept-deps\|reject` | Clears a `needs-operator` blocker of the matching kind. `accept-deps` is an action of `resolve`, not a verb. |

`adopt`, `abandon` and `resolve` end by starting `orchestrate --resume --detach` if no orchestrator holds the lock.

## 4. Orchestrator

Startup order (strict):
1. Open `D/<R>.orch.lock`, `flock(LOCK_EX|LOCK_NB)`. Failure → read the holder pid, run `lifecycle.sh set-pid cloud-<R>-e<E> <holder>` (ignore errors), print `CLOUD:orchestrator-running pid=<holder>`, exit 0. The lock is held until the process exits. `cloud-dispatch.sh` uses `exec`, so the lock owner is the registered pid.
2. `--detach`: before step 1, re-exec self without `--detach` through the `nohup python3 -c 'os.setsid(); os.execvp(…)'` pattern (`drive.sh:115-122`), stdin `/dev/null`, output to `orchestrate.log`; the parent prints the child pid and exits.
3. Write own pid into the lock file. Load the ledger.
4. Lifecycle registration, from `--root` (main worktree): `bash scripts/gsd/lifecycle.sh launch cloud-<R>-e<E> '["scripts/gsd/cloud-dispatch.sh","orchestrate","--run","<R>","--root","<ABS>","--resume"]' '{"respawns":0,"env_deaths":8}' <pid>`.
   - stdout must contain `LIFECYCLE:launched`. `LIFECYCLE:disabled` → `CLOUD-REFUSED: LIFECYCLE_DISABLED`, exit 3.
   - exit 3 with `LIFECYCLE:terminal` → bump `epoch` in the ledger (under the lock), retry once with the new id.
   - `resume_argv[0]` is the script path with no `bash` prefix and no `--detach` (reconcile requires argv[0] under `scripts/gsd/`, `reconcile.sh:149-153`).
5. Tick loop.

Tick: `poll → bind → collect → integrate → admit → reserve+launch → finish`, then sleep (60 s if any task is `launched`, `running` or `result-ready`; else doubling to 600 s; ±10% jitter). Each tick first replays unfinished journal entries (§6) and updates `heartbeat_at` in the ledger.

Finish rules:
- Every task terminal → ledger `state: done` → `lifecycle.sh transition cloud-<R>-e<E> done complete` → exit 0.
- No task can progress and at least one is `needs-operator` or `uncertain` → **park**: journal `park` → ledger `state: parked` → notify → `lifecycle.sh transition … done parked` → exit 0. A later `--resume` finds lifecycle terminal and opens a new epoch (step 4).
- Notify: if `DIGEST_NOTIFY_CMD` is set, pipe one line `CLOUD-PARKED run=<R> items=<n> see: cloud-dispatch.sh status --run <R>` to `sh -c "$DIGEST_NOTIFY_CMD"`; always append the line to `orchestrate.log`. `digest.sh` is not modified.

Local verification concurrency is `max_local_verifiers` (default 1): `collect` handles at most that many tasks per tick.

## 5. Provider backends (`lib/cloud_host.py`)

```python
class Backend(Protocol):
    name: str                                   # "claude" | "codex"
    def available(self) -> tuple[bool, str]: ...
    def launch(self, ctx: LaunchCtx) -> LaunchResult: ...   # one exec, timeout 180 s
    def poll(self, ctx: PollCtx) -> PollResult: ...
    def fetch(self, ctx: PollCtx, quarantine_repo: Path) -> str: ...   # returns result OID inside quarantine
```

- `LaunchCtx`: `run, task, generation, prompt, task_branch, clone_url, model, effort, log_path, env_id`.
- `LaunchResult`: `rc, remote_id|None, url|None, outcome: "launched"|"uncertain"|"failed"`.
- `PollResult`: `state: "running"|"result-ready"|"failed"|"unknown", candidates: [(ref, oid)], detail`.
- API-key backends raise `UnsupportedBackend("API_KEY_BACKEND")`.
- Binaries come from `FFS_CLOUD_BIN_CLAUDE` / `FFS_CLOUD_BIN_CODEX` (tests) or PATH.
- The child environment is an allowlist: `PATH, HOME, LANG, LC_ALL, TERM, CODEX_HOME, CLAUDE_CONFIG_DIR, XDG_CONFIG_HOME, SSH_AUTH_SOCK`.

Claude:
- Launch from a **fresh single-ref clone**: `git clone --no-checkout --single-branch --branch ffs/cloud/<R>/<T>-g<G> --no-tags <origin url> <tmp>`. The clone holds only the scanned task branch, so the documented bundle fallback cannot upload other local refs. The pinned task-branch OID is verified before any checkout (`BRANCH_OID_MISMATCH` leaves the work tree empty); that commit is then checked out with `core.hooksPath` disabled and user/system git config disabled, so no hook or filter runs. With `FFS_CLOUD_CLONE_PARENT` the dispatcher builds the clone directory in a private staging directory in the parent (`.ffs-cloud-new-*`, mode 0700), writes the marker `<parent>/ffs-cloud-launch-claude.ffs-owned` (the directory's device and inode) atomically and exclusively (a temporary file renamed into place without replacing anything), publishes the directory with a no-replace rename, and only then clones; only a directory that marker names is ever deleted. To delete it, the dispatcher renames it into a `.ffs-cloud-old-*` staging directory and compares its device and inode with the marker; a mismatch is put back (`CLONE_PATH_INVALID MOVED_AWAY`). Recovery after a killed launch removes only an empty own staging directory (`rmdir`), an empty own marker and its own marker temporary files; a non-empty `.ffs-cloud-new-*` directory is kept and needs manual removal. A file at the marker path that is not a dispatcher marker ends `CLONE_PATH_INVALID MARKER_UNRECOGNIZED` or `MARKER_EXISTS` and is never removed or truncated. Where the libc has no no-replace rename, the rename falls back to a check followed by a plain rename. Then `cd <tmp> && claude --cloud "<prompt>" --model M --effort E`. Amendment (live finding, Claude Code 2.1.329): the real client refuses `--cloud` unless stdout is a terminal, so this command runs on a pseudo-terminal that is only read, never written (undocumented provider behaviour). A session URL or id (`session_…` or `cse_…`, with a digit) means `launched`. An exit with the known not-a-terminal text is `failed` (`CLI_EXIT NOT_A_TTY`). An exit with no id/URL and no known error, including exit 0, is `launched` with `launch_note: "exit-no-session-url"`, and a timeout with the client alive and no URL is `launched` with `launch_note: "no-session-url"`; in both cases polling continues by nonce (`uncertain` is not used: Claude gets only 3 recovery polls there). A prompt that would itself match the id/URL detector, the not-a-terminal text or the first-run dialog detector is refused before launch (`PROMPT_AMBIGUOUS`). With `FFS_CLOUD_CLONE_PARENT` set, the clone is the fixed path `<parent>/ffs-cloud-launch-claude` under an `flock`, validated and removed after every launch (see docs/cloud-delegation.md). See docs/cloud-delegation.md, "How Claude is launched".
- Launch stdout is undocumented. Live, after the trust dialog was accepted (Claude Code 2.1.329), the client printed `Created cloud session: <title>`, a `View: https://claude.ai/code/session_...` line and a `Resume with: claude --teleport session_...` line, then exited on its own after about 8 seconds, and both the id and the URL were parsed. `remote_id`/`url` are parsed if a session id or `https://claude.ai/…` URL is present; their absence is **not** `uncertain` and **not** `failed` for Claude, because binding uses the nonce; it records a `launch_note` (`LaunchResult.note`, validated against a fixed set: `no-session-url`, `exit-no-session-url`). Only the known not-a-terminal text after exit, a recognised blocking dialog, or a failure before the client starts (clone, snapshot, spawn) is `failed`.
- Poll: `git ls-remote <origin> 'refs/heads/claude/*'`. Candidates: refs whose name contains `<nonce8>`, then refs absent from or changed since the launch snapshot (`claude_refs_at_launch` in the log dir). Pairs in `rejected_candidates` are skipped. At most 20 candidates are examined per tick.

Codex:
- `codex cloud exec --env "$FFS_CODEX_CLOUD_ENV" --branch ffs/cloud/<R>/<T>-g<G> "<prompt>"`. No remote id in stdout → `uncertain`.
- Poll: `codex cloud status <id>` (and `list --json`). Fetch: `codex cloud diff <id>` → `git apply --index` at `base_oid` in the quarantine repo → dispatcher commit. Output shapes are to be captured as fixtures from a live smoke. The Codex backend has not run live (no environment id was supplied), so until then parsers accept JSON with `id`/`status` keys or the text forms and return `unknown` otherwise.

## 6. Journal and recovery

Every multi-store action writes a journal entry first (`done: false`), acts, then marks it done. On startup and each tick, unfinished entries are replayed:

| op | data | recovery |
|---|---|---|
| `reserve` | `key, provider` | slot present → mark done; absent → add, mark done |
| `admit` | `task, base_oid, task_branch` | branch on origin at `base_oid` → state `reserved`; absent → push again; present at another OID → `needs-operator` |
| `bind` | `task, generation, oid` | `bound_oid` unset → set it; set to another OID → drop the entry (first bind wins) |
| `integrate` | `task, generation, expected_tip, candidate_oid, publish_oid, tree` | integration ref equals `publish_oid` or has it as an ancestor → `integrated`; ref equals `expected_tip` → `update-ref` again; otherwise → back to `verified` to re-plan the merge |
| `park` | — | ledger not `parked` → set it and notify once |

`publish_oid` is the exact commit that will be published: the candidate commit for a fast-forward, or the merge commit, created in the main repo's object store **before** the journal entry is written. `submitting` is never replayed: a task found in `submitting` at startup becomes `uncertain` (Codex) or stays pollable as `launched` (Claude, nonce binding makes a duplicate launch detectable but FFS still does not relaunch).

## 7. Collect pipeline (per result; order is fixed)

1. **Quarantine.** Temp repo: `git init`, `git fetch --no-tags --filter=blob:limit=5m <origin> <oid>` for Claude, or the applied diff for Codex. No remotes are configured. Refuse (`rejected`) on: missing blob over the limit, symlink (mode 120000), submodule (160000), or an OID that does not descend from `base_oid`.
2. **Secret scan** of every commit `base_oid..oid` (`git log -p`), commit messages, pathnames, and the raw manifest bytes. This runs before any other early return. A hit → `rejected`, `blocker: secret-leak`, and the runbook text is printed to the log and `status`.
3. **Manifest.** Claude: validate v1; `run`, `task`, `generation`, `nonce`, `base_sha` must match the ledger; `status: blocked` → `needs-operator` with its blockers. The single path `.ffs-result/<T>.json` is exempt from step 4.
4. **Paths.** Changed paths = adds, modifications, deletions, and both sides of renames (`git diff --name-status -M base..oid`). Each must match `owns` and must not match the denylist. Outside → `needs-operator`, `blocker: path-outside-owns` or `denylist`.
5. **Dependency gate.** If any changed path is a dependency file (`package.json`, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `bun.lockb`, `requirements*.txt`, `pyproject.toml`, `uv.lock`, `poetry.lock`, `Pipfile*`, `Cargo.toml`, `Cargo.lock`, `go.mod`, `go.sum`, `Gemfile*`): `needs-operator`, `blocker: dependency-change`, listing added or changed packages and any non-dependency key changed (for example `scripts`). `resolve --action accept-deps` requires `gates.py check-grant <R> --action install:<pkg>` to pass for each package and then continues; the operator installs dependencies locally first.
6. **Candidate.** Create a commit on top of `oid` that removes `.ffs-result/<T>.json`; bring it into the main repo and install it as the candidate ref: the fetch names no destination ref (objects only, and the pinned OID is checked with `rev-parse`), then `update-ref --no-deref` installs the ref as a compare-and-swap. A symbolic ref or a ref that moved to another OID gives `candidate-ref-conflict` and is never written through. `candidate_oid` and its tree are frozen: review, tests and integration all use exactly this commit. State `collected`.
7. **Review.** `scripts/gsd/cloud-review.sh <R> <T> <producer> <diff file> <manifest summary file>`: sources `adversary-host.sh`, sets `FFS_CROSS_VENDOR_FALLBACK=off`, `GSD_RUN_ID=<R>`, `ADVERSARY_SEAM=cloud-review`, and calls `adversary_invoke_typed_request <kind> <kind> 480 '{"kind":"tier","name":"judgment"}' <prompt>` with `<kind>` = `codex` when the producer is `claude`, `claude` when it is `codex` (same kind for preferred and fallback, so no cross-vendor fallback is possible). Before the call: `gates.py loop-round <R> "review:cloud:<T>:g<G>" --max 2 --durable`; exit 1 (cap) → `needs-operator`. The prompt puts the diff and all worker text inside an untrusted-data fence. Verdict: exactly one line `^VERDICT: (APPROVE|REVISE)$`; anything else, or rc ≠ 0 → `needs-operator`, `blocker: review-unavailable`. `REVISE` → `rejected`. Record `{family, verdict, diff_sha, patch_id, tree}` plus `model` and `requested_model` (optional in the ledger, so older records stay valid); an `APPROVE` whose answering model is not the requested judgment-tier model, or is unknown, goes to `needs-operator` instead (15.5). State `reviewed`.
8. **Tests** (§8.2) on the candidate tree. rc 0 → `verified` with `{test_rc, test_log_sha}`; else `rejected`, `blocker: tests-failed`. No sandbox → `needs-operator`.

Integration: target is the local integration ref. Fast-forward when its tip is `candidate_oid`'s base lineage tip; else merge `tip` + `candidate_oid` in a temp worktree: conflict → `needs-operator`; `git patch-id --stable` of the candidate's change must equal the reviewed `patch_id`; tests re-run on the merged tree. Then journal `integrate` (its data carries `test_rc` and `test_log_sha`, the evidence for the tree being published, so a replay records the merged-tree retest and not the candidate's own log) → `git update-ref --no-deref <ref> <publish_oid> <expected_tip>` (a symbolic ref is refused; creation of a dispatcher-owned ref compares against an absent ref) → state `integrated`, history appended. The orchestrator never touches the operator's branch or working tree.

Admission of a successor: all dependencies `integrated` → journal `admit` → create the task branch at the integration tip → outbound scan of the commits not yet on origin → grant check → non-force push → `ls-remote` verify → `reserved`.

Denylist (glob, matched on every changed path): `.ffs/**`, `.ffs-result/**`, `.planning/**`, `.claude/**`, `.codex/**`, `.github/**`, `.feature-fix-swarm/**`, `.git*`, `.gitmodules`, `scripts/hooks/**`, `hooks/**`, `.husky/**`, `.circleci/**`, `.gitlab-ci.yml`, `.mcp.json`, `CLAUDE.md`, `AGENTS.md`, `.envrc`, `.npmrc`, `.env*`, `*.pem`, `id_rsa*`, `setup.sh`.

## 8. Scanner and sandbox

### 8.1 `lib/cloud_scan.py`

`scan_bytes(data, *, label) -> list[Finding]`, `scan_git_range(repo, base, tip) -> list[Finding]`, `scan_text_fields(obj)`. Patterns: PEM blocks; `sk-`, `sk-ant-`, `sk-proj-`; `AKIA[0-9A-Z]{16}`; `gh[pousr]_…`, `github_pat_…`; `xox[abpr]-…`; URL credentials; `dp\.(st|ct|sa|pt)\.…`; JWTs; secret-named key assigned a literal ≥ 12 chars; filenames `.env*`, `id_rsa*`, `*.pem`. Binary blob in an outbound scan → finding (`binary-blob`); inbound binary blobs are allowed only inside `owns` and are size-capped. Any exception → the scan result is "refuse". `self_test()` runs a canary of each pattern class on every invocation; a failed canary refuses. Findings carry the pattern class and location, never the matched value.

Outbound scans cover: every commit and message about to be pushed that origin does not have, pathnames, and the prompt. Inbound: §7 step 2.

### 8.2 `lib/cloud_sandbox.py` (new; `worker_policy.py` is not changed)

The profile below is the first design. Section 14.7 replaced it, and section 15.2 describes the built profile.

`run_verify(tree_oid, cmd, *, repo, deps, timeout_s, log_path) -> int`:
- Materialize the tree with `git archive <oid> | tar -x` into a fresh temp dir `W`. For each `verify.deps` entry that exists in the main worktree, symlink it into `W` (read-only by profile).
- Profile (macOS `sandbox-exec`):
  ```
  (version 1)
  (allow default)
  (deny network*)
  (deny file-write* (subpath "/"))
  (allow file-write* (subpath "<W>") (subpath "<TMP>") (literal "/dev/null") (literal "/dev/dtracehelper") (subpath "/dev/fd"))
  (deny file-read* (subpath "<HOME>/.ssh") (subpath "<HOME>/.aws") (subpath "<HOME>/.config") (subpath "<HOME>/.codex") (subpath "<HOME>/.claude") (subpath "<HOME>/.doppler") (subpath "<HOME>/.gnupg") (subpath "<HOME>/.netrc") (subpath "<HOME>/Library/Keychains") (subpath "<STATE DIR D>"))
  ```
  Fork and exec are allowed, so ordinary test runners work. Network is denied, so nothing is installed or exfiltrated during tests.
- Environment: `PATH` (current PATH with entries under `HOME` dot-directories kept), `HOME=<TMP>/home`, `TMPDIR=<TMP>`, `LANG`, `LC_ALL`, `CI=1`. Nothing else.
- Command: `/bin/bash -c "<cmd>"` with cwd `W`, wall timeout, output to `log_path` (scanned before any excerpt is shown).
- `/usr/bin/sandbox-exec` missing or not darwin → raise `SandboxUnavailable` → `needs-operator`.
- The published commit is the candidate, never anything from `W`.

## 9. Provisioning and secrets

`env init` output, all under `.ffs/cloud/` (the operator reviews and commits them):
- `setup.sh`: idempotent. Re-execs itself as `env -i PATH="$PATH" HOME="$HOME" LANG=C bash "$0" --clean`, so dependency installers never see tokens. Installs repo dependencies (lockfile-driven: `npm ci`, `pnpm i --frozen-lockfile`, `uv sync --frozen`, `pip install -r`), `jq`/`bats`/`shellcheck` when `apt-get` exists, and the secret-provider CLI from `config.secrets.install` or the pinned Doppler installer (version + sha256 checked). Writes no secret to disk. Writes a stamp `$HOME/.ffs-cloud-setup.<sha256 of setup.sh>`.
- `env-check.sh`: prints JSON `{"tools":{…},"present":[names],"missing":[names]}` for the names in `config.secrets.names.<provider>`. Names only.
- `session-start.sh`: acts only when `CLAUDE_CODE_REMOTE=true` **and** (current branch matches `ffs/cloud/*` **or** `FFS_CLOUD_PROVISION=1`). Otherwise exit 0 silently. When acting: if the stamp is missing, run `setup.sh` with a 240 s cap; always exit 0; write the outcome to `$HOME/.ffs-cloud-env-status`.
- `--register-hook` adds to the committed `.claude/settings.json`: `{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"bash \"$CLAUDE_PROJECT_DIR/.ffs/cloud/session-start.sh\"","timeout":300}]}]}}`. Merge and removal use one generic function `merge_hook_entry(document, event, entry, previous=None)` / `remove_hook_entry(document, event, entry)` in `lib/operator_guard_hook.py` (exact-entry ownership; `merge_guard`/`remove_guard` become thin wrappers with unchanged behavior). Only `env init` writes this entry; the installer does not.

Trust statement (answers the "hook runs checkout code" finding):
- The hook runs code from the checked-out branch. FFS launches sessions only on task branches it created from the operator's base or from an integration commit that passed the denylist, so `.ffs/` and `.claude/` there are byte-identical to the operator's base. `ls-remote` verifies the branch OID immediately before each launch.
- Anyone who can push to the repo (including another cloud worker through the git proxy) could move a task branch between that check and the provider's clone. The control is server-side: a GitHub ruleset on `ffs/cloud/**` that blocks updates and deletions for non-admins. `doctor` reads rulesets with `gh api` and reports `WARN` when none covers `ffs/cloud/**`; when `secrets.provider != none`, a missing ruleset is `FAIL` (cloud stays off).
- A result built on a tampered branch is rejected by the denylist. That does not undo the session having run. This residual risk is documented.
- An ordinary cloud session that the operator starts on an `ffs/cloud/*` branch gets provisioned. That is accepted.

Secrets:
- FFS never reads, stores or sends a secret value. Values live in the provider environment, placed by the operator.
- Exposure of each name to the cloud requires a `secret-use:<NAME>` grant (autonomous) or the interactive yes that lists the names.
- `provider-env` (a label only: the code special-cases `none`, `doppler` and `command`, and treats any other value like this one): Claude → environment variables, or network secrets for HTTP keys (hosts listed in `network_secret_hosts`); Codex → environment variables (the Secrets store is setup-only).
- `doppler`: `DOPPLER_TOKEN` as an environment variable; worker commands run as `doppler run --no-fallback -- <cmd>` (the builder confirms the exact flag against `doppler run --help` and records it in the tests' fixture); `env-spec` prints: create branch config `<env>_agent`, mint `doppler configs tokens create ffs-cloud -p <project> -c <env>_agent --max-age 720h --plain`, paste into the provider UI, allowlist `api.doppler.com` and the CLI download host, use a private environment. FFS cannot select or verify the Claude environment from the CLI; the doc says so.
- `command`: `wrapper` and `install` from config.
- Worker contract text (in every prompt, from `lib/handoff_packet.py`): never ask the user; run `bash .ffs/cloud/env-check.sh` first and put its JSON in the manifest; wrap test and runtime commands with the wrapper; never print, echo, log, write or commit a secret value; commit work; write the manifest with the nonce; push to the requested branch name; stay inside `owns`; do not touch denylisted paths; at most 4 nested subagents; stop at the time budget and write `status: blocked` with reasons.

## 10. Grants, guard, doctor

Grants (exact keys, `lib/gates.py` `check-grant <R> --action <A>`; exit 0 granted, 1 not, 75 store error → refuse):
- `cloud-dispatch:claude`, `cloud-dispatch:codex`
- `push:origin/ffs/cloud/<R>` — one key authorizes pushes only to refs that equal `refs/heads/ffs/cloud/<R>/<name>` where `<name>` is `integration` or `<T>-g<G>`. The validator rejects any other ref, any `..`, and force refspecs. Pushes use `git push --no-force <remote> <oid>:<ref>`.
- `secret-use:<NAME>` per exposed secret name; `install:<pkg>` in the dependency gate.
- Interactive `/swarm --cloud`: the session shows the task list, providers and secret names, and on the operator's yes runs `gates.py grant <R> --action … --ttl-hours 24 --reason "interactive /swarm --cloud"` for exactly those keys. Grants are re-checked at every push and submit.

Guard (`scripts/hooks/operator-only-guard.sh`, new `elif` branches): segment contains `claude` and `--cloud`; segment contains `codex`, `cloud` and one of `exec|apply|diff`; segment contains `cloud-dispatch.sh` (or `cloud_dispatch.py`) and one of `adopt|abandon|resolve`. The dispatcher's own provider calls are subprocesses of the orchestrator, not agent Bash tool calls, so the guard does not see them. The guard fails open and matches by token membership; it is an anti-accident control.

`doctor` checks (each `OK|WARN|FAIL` with a code): `claude` and `codex` on PATH and logged in with OAuth (no API key variables set: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` present → `FAIL API_KEY_BACKEND`); origin is a GitHub remote; lifecycle and reconcile kill switches; `sandbox-exec` present; base clean and pushed; `.ffs/cloud/*` present in `git ls-tree <base_sha>`; SessionStart entry present in the committed settings (WARN if absent); installed `reconcile.sh` contains `mapfile` (WARN: restarts need manual `--resume`); reconcile agent installed (WARN); `FFS_CODEX_CLOUD_ENV` set when codex is enabled; ruleset on `ffs/cloud/**`; every `needs_secrets` name declared; `config.verify.cmd` set (WARN). Green = no `FAIL`.

## 11. `/swarm` skill contract and the no-flag guarantee

- All cloud text in `skills/swarm/SKILL.md` sits between `<!-- ffs:cloud:begin -->` and `<!-- ffs:cloud:end -->`. Outside that block the only permitted differences from the base file are the `version` line, `permissions.network`, and one line in the invocation list naming the flag.
- Test (`tests/test_swarm_cloud_noflag.py`): SKILL.md with the block and those three lines removed equals the base text with the same three lines removed. `skills/feature-implement/SKILL.md` and `skills/task-swarm/SKILL.md` equal the base outside their `ffs:cloud-deferred` refusal block and the version line.
- The cloud block is entered only when `--cloud` or `FFS_CLOUD` is present. Inside it: with `--dry-run`, skip the entry reconcile pass and run only `plan`; otherwise normalize tasks to JSON (§2.1), run `plan`, show the plan, get grants, run `start` for cloud groups, and run local groups exactly as Step 2 does today. Step 3 reports cloud tasks from `status` and prints the integration ref and a merge command. Nothing is merged.
- Documented exceptions to "nothing changes without the flag": the guard gains deny patterns for every session; the installer ships new files; `network` permission for the skill.

## 12. File ownership

| Builder | Files (creates or edits; nobody else touches them) |
|---|---|
| B1 | `lib/delegation_router.py`, `lib/handoff_packet.py`, `tests/test_delegation_router.py`, `tests/test_handoff_packet.py` |
| B2 | `lib/cloud_ledger.py`, `tests/test_cloud_ledger.py` |
| B3 | `lib/cloud_host.py`, `tests/fixtures/cloud/fake-claude`, `tests/fixtures/cloud/fake-codex`, `tests/test_cloud_host.py` |
| B4 | `lib/cloud_scan.py`, `lib/cloud_sandbox.py`, `lib/cloud_collect.py`, `scripts/gsd/cloud-review.sh`, their tests |
| B5 (after B1–B4) | `lib/cloud_dispatch.py`, `scripts/gsd/cloud-dispatch.sh`, `tests/test_cloud_dispatch.py`, `tests/bats/cloud-dispatch.bats` |
| B6 | `templates/cloud/{setup.sh,env-check.sh,session-start.sh,config.json}`, `scripts/hooks/operator-only-guard.sh`, `tests/bats/operator-only-guard.bats`, `tests/bats/cloud-templates.bats` |
| B7 (after B5) | `lib/operator_guard_hook.py`, `lib/ffs_installer.py` (both lib lists gain the new lib files; `templates/cloud/*` ships as `cloud-templates/*`), `skills/swarm/SKILL.md`, `skills/autonomy-grant/SKILL.md`, `skills/preflight/SKILL.md`, `skills/spec-status/SKILL.md`, `skills/spec-status/scripts/collect-status-facts.sh`, `tests/test_swarm_cloud_noflag.py`, installer tests |
| B8 | `docs/cloud-delegation.md`, `CHANGELOG.md` |

Shared imports are by the signatures in §2–§8. Python is stdlib only, flat `lib/`, Python 3.9-compatible. Shell must run under `/bin/bash` 3.2.

## 13. Stated limits (go in the docs and the final report)

- Nonce binding is correlation, not authentication. Anyone with push access who reads a published manifest can copy it. FFS therefore treats any content that matches `(run, task, generation, base)` and independently passes every gate as acceptable, binds the first OID seen unchanged on two consecutive polls, records it once, and ignores later candidates.
- Secrets held by a worker can be printed or pushed before FFS scans. Scope, expiry, non-production values, the inbound scan and the runbook bound the damage.
- The guard is fail-open. Model and effort in the cloud are unverified. The orchestrator sleeps with the host. Local review and tests remain local load. Mixed cloud/local task groups do not offload. FFS cannot choose the Claude cloud environment. Sandboxed tests cannot reach the network, so tests that need live services fail and should be marked `[local]`.

## 14. Amendments (these override any earlier section they conflict with)

Source: Astra contract check (1 build-blocker, 7 fix-inline, 7 undefined contracts). Operator chose to fold these in and review the built code.

### 14.1 Paths and namespaces

- Run ledger: `D/runs/<R>.json` (+ `.lock`). Slots: `D/providers.json`. Orchestrator lock: `D/runs/<R>.orch.lock`. Per-run directory: `D/runs/<R>/` (logs, `tasks.json`, `claude_refs_at_launch.json`). A run id can therefore never collide with `providers`.
- Epoch `E` ≤ 999999, so `reconcile-cloud-<R>-e<E>` ≤ 64 bytes.
- Publication ref: `refs/ffs/cloud/<R>/pub/<T>-g<G>` (local) pins `publish_oid` (including merge commits) before the journal entry is written.
- `origin_url` is recorded in the ledger at `start`. Every push and every `ls-remote` uses that exact URL; a remote whose URL differs from the pinned one → `CLOUD-REFUSED: ORIGIN_CHANGED`.

### 14.2 Durable task inputs

- `start` scans each task prompt (outbound scan), then writes `D/runs/<R>/tasks.json` (mode 0600): the list of normalized tasks (§2.1) including `prompt`. The ledger stores `tasks_sha256` of that file. Prompts never enter the ledger.
- The orchestrator loads `tasks.json` at startup and refuses (`TASK_INPUTS_CHANGED`, exit 78) if the hash differs. The handoff packet for any task, at any time, is rebuilt from `tasks.json` + ledger fields (nonce, generation, base).

### 14.3 Full task transition table (replaces the state list in §2.3)

Initial state of every cloud task is `waiting`. "Active set" = `{reserved, submitting, launched, running, uncertain}`: exactly the states that hold a provider slot.

| From | Event | To | Side effects (journaled where marked J) |
|---|---|---|---|
| waiting | all `depends_on` are `integrated` (or none) | admitted | J `admit`: create task branch at base (run base, or integration tip), outbound scan, grant check, push, verify |
| waiting | a dependency is terminal and not `integrated` | failed | `blocker: dependency-<state>` |
| admitted | capacity free on the chosen provider and total | reserved | J `reserve`: add slot |
| admitted | no capacity | admitted | retry next tick |
| reserved | exposure and grants re-checked (14.5), prompt rebuilt | submitting | ledger write + fsync **before** the exec |
| submitting | launch rc 0 | launched | set `launched_at`, `deadline_at`, optional `remote_id`/`url` |
| submitting | launch rc ≠ 0 | failed | J `release` |
| submitting | found at orchestrator startup (crash mid-launch) | uncertain | never relaunched automatically |
| launched / running | poll says running | running | — |
| launched / running | bind condition met (§13, 14.6) | result-ready | J `bind`; J `release` |
| launched / running | `now > deadline_at` | failed | `blocker: deadline`; J `release` |
| launched / running | `poll_errors > 6` | needs-operator | `blocker: poll-errors`; slot kept until deadline |
| uncertain | one re-parse of the launch log finds an id, or (Claude) a nonce candidate appears | launched | — |
| uncertain | otherwise, after one tick | needs-operator | `blocker: uncertain-launch`; slot kept until deadline, then J `release` |
| result-ready | collect steps 1–6 pass | collected | candidate ref created |
| collected | review APPROVE | reviewed | |
| reviewed | tests rc 0 | verified | |
| reviewed or verified | `review-stale`: the `review` record lacks matching `model` and `requested_model` (an approval from before the reviewer-model check) | collected | `review` and `verify` cleared, `review_stale` += 1 (its own round meter; `review_attempts` is untouched), and on the first one `review_stale_base` = `review_attempts` |
| verified | merge planned, `publish_oid` pinned | integrating | J `integrate` |
| integrating | `update-ref` succeeded | integrated | one atomic ledger write: state, `integration.tip`, history entry, journal entry done |
| any collect/review/test step | gate fails | rejected or needs-operator | per the blocker table (14.8) |
| any non-terminal | operator `abandon` | abandoned (or `waiting` with `--retry`) | generation += 1, new nonce; J `release` if in the active set |

Generation fence: every journal entry, candidate, poll result and side effect carries `(task, generation)`. Anything whose generation is not the task's current generation is dropped (results become `superseded` records in the log, not state changes). `bind` replay checks the generation.

Slots: `release` removes the slot `R/T/gG` (idempotent). A task holds at most one slot. `admit` replay leaves the task `admitted`; it never marks it `reserved`.

### 14.4 Park and wake handshake

- The ledger has `wake_seq` (integer). `adopt`, `abandon` and `resolve` mutate the ledger under the ledger lock and increment `wake_seq`.
- The orchestrator reads `wake_seq` at tick start. To park it takes the ledger lock, checks that `wake_seq` is unchanged and that the park condition still holds, and only then writes `state: parked`. If `wake_seq` changed, it does not park and runs another tick.
- After mutating, an operator verb tries to start `orchestrate --resume --detach`. If the orchestrator lock is held, it waits up to 30 s for release, re-reads the ledger, and starts the orchestrator if the run is `parked` or the lock is free. It prints which of the two happened.

### 14.5 Secret exposure covers the whole environment

- `config.secrets.exposure.<provider>` is the operator-attested list of **every** secret name reachable from that provider's environment: environment variables, network-secret hosts (as `host:<name>`), and every name in the Doppler config the token can read. `env-spec` tells the operator how to list them (`doppler secrets --only-names`). `config.secrets.names` is removed; use `exposure`.
- Before **every** submit to a provider, including tasks that request no secrets, each name in `exposure.<provider>` must have a `secret-use:<NAME>` grant (or be in the interactive yes). Missing → the task stays `reserved`→`needs-operator` with `blocker: exposure-not-granted` (autonomous) or the run refuses at `start`.
- Router: `needs_secrets ⊆ exposure.<provider>` is required for that provider.
- Diagnostics: launch stdout/stderr is scanned and truncated to 64 KB (start and end are both kept, with a marker between) before it is written to the 0600 log; matches are replaced with `[REDACTED:<class>]`. Only allowlisted fields reach the ledger.

### 14.6 Ruleset evidence, scanning, binding

- When `exposure.<provider>` is non-empty: after pushing a task branch and before submit, `gh api repos/{owner}/{repo}/rules/branches/<branch>` must show active `update` and `deletion` rules for that exact branch. Any error, empty result, or a ruleset in `evaluate` mode → `needs-operator`, `blocker: ruleset-unverified`. `doctor` also lists bypass actors of the matching ruleset and FAILs when any bypass actor other than the repository admin role exists or the list cannot be read. With no exposure, a missing ruleset is a WARN.
- Provisioning evidence is worker-reported (`env_report`); FFS has no independent channel into the cloud VM. The ledger marks it `env_report_source: worker`. This is a stated limit.
- Collect step order (replaces §7 steps 1–2): fetch into quarantine → **scan every new object** (`git rev-list --objects <base_oid>..<oid>`: all blobs, all commit messages, all paths; this covers merge-only content) → only then ancestry, mode and size checks. A scan hit is reported even if a later check would also reject.
- Outbound, Claude only (its transport may bundle the checked-out branch): the full ancestry of the task branch must have a clean scan. Results are cached by commit OID in `D/scan-cache.json` (`{oid: {"clean": bool, "findings": n}}`), so each base is scanned once. Findings → `doctor` FAIL `HISTORY_SECRET` listing class and path; the operator may allow entries in `config.scan.allow` as `{"blob":"<oid>","class":"<pattern class>"}`.
- Binding: a candidate is bound only if its manifest generation equals the task's current generation and the same OID was seen on two consecutive polls. `PollCtx` = `run, task, generation, nonce, base_oid, task_branch, remote_id, launched_at, refs_snapshot_path, rejected, clone_url, env_id`. `fetch(ctx, candidate: tuple[str, str], quarantine_repo) -> str`.

### 14.7 Sandbox reads and materialization (replaces the profile in §8.2)

```
(version 1)
(allow default)
(deny network*)
(deny file-write* (subpath "/"))
(allow file-write* (subpath "<W>") (subpath "<TMP>") (literal "/dev/null") (literal "/dev/dtracehelper") (subpath "/dev/fd"))
(deny file-read* (subpath "<HOME>") (subpath "/Volumes") (subpath "<MAIN WORKTREE>") (subpath "<every other git worktree root>") (subpath "<STATE DIR D>"))
(allow file-read* (subpath "<W>") (subpath "<TMP>") <one (subpath …) per resolved verify.deps root> <one (subpath …) per toolchain root> <one per config.verify.read_roots>)
(allow file-read-metadata (literal "<each ancestor directory of every allowed root>"))
```

- Later rules win in SBPL, so the allow list re-opens only the named roots.
- Toolchain roots: for each absolute `PATH` entry that lies under `HOME` (and is not `HOME`), the bin directory itself, never its parent. When the entry sits in a known toolchain layout the root of that layout is granted instead: `~/.nvm/versions/node/<v>`, `~/.nodenv/versions/<v>`, `~/.pyenv/versions/<v>`, `~/.rbenv/versions/<v>`, `~/.rustup/toolchains/<t>`, `~/.asdf/installs/*/*`, `~/.volta/tools/image/*/*`, `~/.fnm/node-versions/*/installation`, `~/.sdkman/candidates/*/*`, `~/.local/share/uv/{tools,python}/*`, `~/.local/share/pipx/venvs/*` and `~/.local/share/mise/installs/*/*`. Targets of symlinks inside such a directory are granted by the same layout rule (two hops). `~/.cargo/bin` grants only itself plus each directory in `~/.rustup/toolchains`, never `~/.cargo`. `~/.local/share` and `~/.cache` are never granted whole. A root that equals or contains `HOME`, a worktree, the state dir, a credential path or a temp area, or that lies inside a credential path, is dropped. `~/Library` and `~/.config` are not readable. Section 15.2 lists the rest of the built profile.
- Dependency roots: `realpath` of each `verify.deps` path in the main worktree; the symlink in `W` points at it. Nothing else of the operator checkout (for example `.env`) is readable.
- Materialization: no `git archive`. Walk `git ls-tree -r -z <tree>` and write each blob with `git cat-file blob`, set the executable bit from the mode, skip nothing, apply no filters or attributes.

### 14.8 Blockers and how they clear

| `blocker` | Set by | Clears with |
|---|---|---|
| `uncertain-launch`, `poll-errors`, `ambiguous-candidates`, `candidate-moved` | launch, poll, bind | `adopt --oid`, or `abandon [--retry]` |
| `manifest-blocked` (worker said blocked) | collect 3 | `abandon [--retry]` |
| `path-outside-owns`, `denylist` | collect 4 | `resolve --action reject`, or `abandon --retry` |
| `dependency-change` | collect 5 | `resolve --action accept-deps` (needs `install:<pkg>` grants), or `reject` |
| `review-unavailable`, `review-cap`, `review-model-downgraded`, `review-model-unknown` | collect 7 | `resolve --action retest` (re-runs from review), or `reject` |
| `verify-cmd-missing` | tests | `resolve --action reject`, or `abandon --retry` |
| `manifest-invalid` | collect 3 | `abandon [--retry]` |
| `sandbox-unavailable`, `merge-conflict`, `patch-id-mismatch` | tests, integrate | `resolve --action retest` after the operator fixes the cause, or `reject` |
| `exposure-not-granted`, `ruleset-unverified`, `grant-missing` | submit, admit | grant or fix, then `resolve --action retest` (re-runs the failed check) |
| `secret-leak`, `tests-failed`, review `REVISE` | collect | none: state is `rejected`. `abandon --retry` starts a new generation |

`resolve --action X` is refused (exit 3) unless the task's current blocker is in the row for X.

Secret-leak runbook (printed verbatim to the log and `status`): "A cloud result contained a secret-shaped value. 1. Revoke or rotate the credential now (Doppler: `doppler configs tokens revoke`, then rotate the leaked secret itself). 2. Delete the remote branch that carries it: `git push <origin> --delete <branch>`. 3. Check the provider session transcript and delete or unshare the session. 4. Re-mint the agent token and update the provider environment. FFS has not integrated this result."

### 14.9 Routing order (replaces §2.2 rules 3–6)

1. Validate: unknown `depends_on` id → exit 3 `DEP_UNKNOWN`; cycle → exit 3 `DEP_CYCLE`.
2. Add an edge between every pair of tasks whose `owns` overlap (earlier input order → later), across **all** tasks.
3. Compute connected components on that graph. These are the groups.
4. Per-task provider set = providers allowed for the tier (codex: execution, volume; claude: execution, volume, judgment) ∩ providers whose `exposure` covers `needs_secrets` ∩ available providers. Empty → the task is ineligible.
5. A group is `cloud` iff every task is eligible and at least one meets the duration threshold. Then pick each task's provider from its set by free slots, alternating on ties.
6. `required`: if any group is `local`, exit 3 and launch nothing.
7. Task ids: `t1…tN` in input order, or `[id: x]` from the tasks file. `--sequential` adds a dependency edge from each task to the next, so everything is one group.
8. `plan` is pure and always exits 0 or 3 for the reasons above. It adds `"preconditions": {"doctor": "green|not-green|unknown", "grants_missing": ["…"], "consent_required": ["NAME"]}`; `plan` runs `doctor` only with `--with-doctor`, else reports `unknown`. `start` enforces all preconditions.

### 14.10 Remaining contracts

- **Verifier limit** is global across runs: `D/verify.<i>.lock` for `i < max_local_verifiers`; a collector takes one with a non-blocking `flock` and skips verification this tick if none is free.
- **Codex manifest:** the Codex prompt also asks for `.ffs-result/<T>.json`. If the applied diff contains it, it is validated like a Claude manifest (nonce included) and `status: blocked` is honored. If absent, the dispatcher synthesizes `status: complete` with `manifest_source: synthesized`.
- **Template lookup** (`env init`): `<directory of cloud_dispatch.py>/cloud-templates/`, else `<that directory>/../templates/cloud/`. B7 ships `templates/cloud/*` to `<lib root>/cloud-templates/*` in both installer lists.
- **`/swarm` dry-run:** the cloud block is placed directly after the init-guard paragraph and before the entry reconcile line. With `--cloud` and `--dry-run` it normalizes tasks, runs `plan`, prints the result and stops; the reconcile pass does not run. The no-flag test compares against the post-PR-0 base file.
- **Deferred skills:** `/feature-implement` and `/task-swarm` carry one marked `ffs:cloud-deferred` block ahead of the init gate (plus a version bump). With `--cloud`, `--cloud=auto|prefer|required` or `FFS_CLOUD` set they print one line (`Cloud delegation for /<skill> arrives in a later release; /swarm --cloud is available now.`) and stop. A test asserts they are identical to the base outside that block and the version line, and that the block carries the message.
- **Guard at install:** an installer test asserts the installed guard script denies the three new patterns and still allows `git status`.
- **Integrate check:** `git diff <expected_tip> <publish_oid>` must have the same `patch-id --stable` as the reviewed candidate change; otherwise `needs-operator`, `blocker: patch-id-mismatch`.
- **Replay:** every replayed push repeats the outbound scan (cache allowed) and the grant check.

## 15. Built behavior after the fix round (overrides sections 1 to 14 where they conflict)

Source of truth: `lib/cloud_*.py`, `lib/delegation_router.py`, `lib/handoff_packet.py`, `scripts/gsd/cloud-*.sh`, `templates/cloud/*`, `scripts/hooks/operator-only-guard.sh`. Names and placeholders only.

### 15.1 Dispatcher, operators, origin

- Journal ops are `integrate`, `reserve`, `admit`, `park` and `release`. There is no `bind` op. A bind is a ledger transition fenced by the task generation.
- `abandon` is refused with `INTEGRATION_IN_PROGRESS` (exit 3) while the task is `integrating` or has an unfinished `integrate` journal entry. The journal may show that `update-ref` already moved the ref, and a generation bump would leave the ref and the ledger in disagreement. The verb still wakes an orchestrator.
- `abandon --retry` of a `rejected` or `failed` task also reopens a `done` run: the run becomes `active` and the next orchestrator start opens a new epoch. `adopt`, `resolve` and a plain `abandon` on a `done` run are refused with `RUN_DONE`.
- `adopt --remote-id <id>` applies to Codex tasks in `uncertain`, or in `needs-operator` with `uncertain-launch`, `poll-errors`, `ambiguous-candidates` or `candidate-moved`. It sets `remote_id`, a new `launched_at`, and `deadline_at = now + deadline_hours`, resets poll counters, journals a `reserve` for the slot (the slot may have been released at the deadline), and returns the task to `launched`. `adopt --oid` is refused for Codex tasks and `--remote-id` is refused for Claude tasks. Exactly one of the two flags is required.
- `uncertain` launches get recovery polls before they become `needs-operator` with `uncertain-launch`: 3 for Claude, 1 for Codex.
- After an operator verb mutates the ledger: run `done` → print `CLOUD:orchestrator-not-needed`; lock free → spawn `orchestrate --resume`; lock held → spawn `orchestrate --resume --wait-lock 300`. This replaces the 30 second wait of 14.4. The holder re-checks `wake_seq` just before it exits and un-parks if it moved.
- Origin URLs that embed credentials are refused with `ORIGIN_HAS_CREDENTIALS`: by `start` (before any subprocess receives the URL), at orchestrator startup (before the `ORIGIN_CHANGED` comparison), and by `doctor`, which then returns only that one check.
- `orchestrate` tick order: heartbeat, replay journal, expire slots, poll, collect, integrate, admit, launch (reap slots, reclaim lost slots, reserve, submit), finish. A step about one task that fails 3 consecutive times moves that task to `needs-operator` with `internal-error`. A step with no task (heartbeat, slot handling, finish) that fails 5 consecutive ticks ends the orchestrator with `STEP_FAILING` (exit 78).
- A Claude candidate that was bound from polling and then fails collect with a missing or invalid manifest is not a verdict on the task. It is recorded in `rejected_candidates`, the bind is cleared, and the task returns to `running`. The exception is a candidate that is provably this task generation's own result: its ref is exactly `claude/ffs-<run>-<task>-<nonce8>`, or its manifest carries this generation's nonce, and the manifest is present but rejected by the validator. That task goes to `needs-operator` with blocker `manifest-invalid` (a `RESULT_READY` row; it held no slot, so none is released) and polling stops. The recorded detail carries the validator's error code (`step_manifest` returns it as `validator_code`, with `nonce_matches`), never the validator's free-text detail or any worker text. Only `abandon` clears it. A missing manifest, and a foreign ref that merely is new, are unchanged. Refs named for another task of the same run are never candidates. An operator-adopted result is not unbound.
- Slots: a `needs-operator` or `uncertain` task that may hold a slot releases it exactly once when `now > deadline_at` (journaled `release`). The reaper additionally drops slots of missing or `done` runs, slots of `parked` runs past their deadline, and any slot more than 3600 s past its deadline. The reaper runs only in an orchestrator tick and in `cleanup`.
- A recovered live task always holds a slot. When replay of a `reserve` entry finds the task in `launched` or `running` (after an unbind, or after `adopt --remote-id`), the slot is added even above the provider or total limit (`force`). New admissions wait until it ends. Any other `reserve` that no longer fits is dropped and the task stays `admitted`.
- A task found in `submitting` becomes `uncertain` in the heartbeat step of the next tick (14.3 said startup), and is never relaunched automatically.
- `cleanup` classifies every local ref first (nothing is deleted until all are classified), then deletes only refs the ledger accounts for, each with a compare-and-delete on the OID it saw. It runs entirely inside the run ledger lock, including remote branch deletion, ref deletion, slot reaping and removal of the run files. A concurrent operator verb waits for the lock and then reports `LEDGER_MISSING`. `cleanup` refuses with `RUN_NOT_DONE` for a run that is not `done` unless the run has no task inputs file and no task that ever started (a `start` that crashed early); only such a run is removed.
- Scripts the dispatcher runs (`cloud-review.sh`, `adversary-host.sh`, `lifecycle.sh`, `reconcile.sh`, `reconcile-agent.sh`, `host-pressure.sh`) are resolved only from the dispatcher's own install location: `<lib dir>/scripts/gsd/<name>`, then `<lib dir>/../scripts/gsd/<name>`. Never from the target repository's `scripts/gsd/`.

### 15.2 Sandbox (`lib/cloud_sandbox.py`; amends 14.7)

Profile order: `(allow default)`, `(deny network*)`, IPC hardening, write denies, read denies, read allows, read denies again for credential paths, other worktrees and the state dir, then a read deny for `/dev/tty*`.

- IPC hardening: `mach-lookup` denied except `com.apple.system.opendirectoryd.libinfo`; `mach-register`, `lsopen`, `appleevent-send`, `iokit-open`, `system-socket` and `job-creation` denied; `signal` and `process-info*` allowed only for `(target same-sandbox)`.
- Read denies: operator terminals (`/dev/tty*`), `HOME`, `/Volumes`, the main worktree, every other worktree, the state dir, credential paths under `HOME` (`.ssh .aws .config .codex .claude .doppler .gnupg .netrc .docker .kube .npmrc .pypirc .orbstack .git-credentials .gitconfig .pgpass .azure .vercel .railway .wrangler .cargo/credentials .cargo/credentials.toml Library`) and the temp areas `/tmp`, `/private/tmp`, `/var/folders`, `/private/var/folders` plus the directory that holds the run temp dir. The run's own temp dir, which holds the work tree and the fake home, is re-opened by the read allows.
- Roots: a `verify.deps` or `verify.read_roots` root that overlaps a denied area (equals it, contains it, or sits inside a credential path, another worktree or the state dir) raises `SandboxError` (→ `sandbox-unavailable`). A dependency root may sit inside the main worktree. A toolchain root that overlaps a denied area, or sits inside the main worktree, is dropped. Toolchain roots follow the rule in 14.7 (bin directory or known layout root).
- Child `PATH` keeps entries outside `HOME`, and entries under a granted root. It drops entries under `/Volumes`, a temp area or a worktree. Other environment: `HOME=<TMP>/home`, `TMPDIR=<TMP>`, `LANG`, `LC_ALL`, `CI=1`.
- Startup self-check (`sandbox_available`): the hardening rules must be accepted, a `kill -0` of a process outside the sandbox must fail under them, and `/usr/bin/pbpaste` (when present and working outside) must fail under them. A failing check, or a profile that `sandbox-exec` rejects, raises `SandboxUnavailable`. Verification never runs without the rules.
- Output passes through a writer that keeps at most 8 MB in memory, redacts with `cloud_scan.redact`, and writes the 0600 log once. A scanner failure replaces the log with a withheld notice. A truncated log ends with `[log truncated]`.
- Child limits: `RLIMIT_CORE` 0, `RLIMIT_FSIZE` 512 MB, `RLIMIT_NOFILE` 4096, `RLIMIT_NPROC` = the uid's process count + 512. Timeout kills the process group (rc 124). While the command runs, a tracker polls the process table and records every descendant of the launched process (pid plus start time). After every run a sweep kills only: the launched process, its observed descendants, members of the group or session of those that are still alive, and descendants of all of these. It takes a fresh process listing and re-checks pid and start time right before each SIGKILL. Argv and cwd never select a victim.
- Residual gap: a double-fork child that no poll saw while its parent chain was alive is not swept.
- Residual risk: `com.apple.system.opendirectoryd.libinfo` stays allowed because user lookups (`getpass`, Node `os.userInfo()`, git author identity) need it. Whether it can carry DNS lookups for outside names is not tested.

### 15.3 Collect and integrate (`lib/cloud_collect.py`; amends 7 and 14.10)

- Step 6 builds a fresh commit from the validated tree minus `.ffs-result/<T>.json`. Its only parent is the task base commit, it has a fixed identity, date and message, and worker history is dropped. It is imported as the candidate ref.
- Step 4 checks the net diff and every commit of `base..oid`: a forbidden path touched by any commit blocks even when a later commit restores it. A changed path that holds a Unicode format or control character (category Cf or Cc, for example a zero-width character) is refused as `denylist` (`matches_denylist`), because some filesystems ignore such characters when they compare names. Paths that collide with each other or with base paths once case is folded are also reported as `denylist` with the detail `case-colliding paths`. There is no `case-collision` blocker.
- Step 5: dependency files are the lockfiles, `package.json`, `requirements*.txt`, and manifests of ecosystems that are not parsed (`pyproject.toml`, `Cargo.toml`, `go.mod`, `go.work`, `Pipfile`, `composer.json`, `pnpm-workspace.yaml`, `setup.cfg`, `setup.py`, `Gemfile*`, `pom.xml`, `build.gradle`, `build.gradle.kts`, `Package.swift`, `deno.json`, `deno.jsonc`, `bunfig.toml`, `environment.yml`, `NuGet.config`, `Podfile`, `mix.exs`, `pubspec.yaml`). Any change to one of these is unattributed, and removal-only changes need `install:unattributed:<digest12>`. The report lists added, changed and removed packages and other changed keys, and sets `unattributed` when any change cannot be pinned to named registry packages: lockfile changes, `scripts` or other install-time keys, non-registry sources, unparsed ecosystems, unparsable files, a changed repeated declaration of one package (in `package.json` sections or a requirements file), removal-only changes (no package added or changed), and files touched only in worker history. A change to `package.json` or a requirements file that touches no dependency declaration and no install-time key (for example only `version`) leaves `packages` empty and `unattributed` false, and the step passes with no grant. The report has a `report_digest` bound to the result OID. `resolve --action accept-deps` needs `install:<pkg>` for each package and, for an unattributed report, `install:unattributed:<digest12>` (first 12 hex characters of the digest). It refuses with `DEPENDENCY_REPORT_MISSING`, `DEPENDENCY_REPORT_INVALID`, `STALE_APPROVAL` (the stored digest is not the one recorded for this result, or the generation, bound OID or digest changed after the grants were checked) or `GRANT_MISSING`.
- Review diff: deleted and context lines are redacted before they are sent (they come from the operator's base). Added lines and the review prompt text are scanned again. A hit there returns `secret-leak` from the review stage. `TRANSITIONS` has a `collected` + reject row for `secret-leak`, so the task ends as `rejected` with `secret-leak`; the detail carries the findings, and `status` prints the leak runbook. A diff over 2,000,000 bytes is `review-unavailable`. Any binary change is `needs-operator` with `binary-change`. There is no allowlist; `config.review.binary_allow` was removed.
- Integration: a candidate whose base is the integration tip is fast-forwarded. Otherwise it is merged only when the tip and the candidate changed disjoint paths since their merge base (compared with case folded and with parent and child paths counted as overlapping). The merge tree is built from a temporary index with `read-tree -m --aggressive`: no worktree, no hooks, no content merge. It must equal the tip with exactly the candidate's changed paths taken from the candidate. Overlap, an unresolved index, a different tree, or a case collision between the two results in the merged tree (detail `case-colliding paths`) gives `needs-operator` with `merge-conflict` (`B_MERGE_CONFLICT`) and the paths listed. The patch id is computed with `git patch-id --verbatim` (`--stable` only when git lacks the flag) and must equal the reviewed patch id. Tests re-run on a merged tree. A merge gets no second review.
- Replay of an unfinished `integrate` entry accepts only the exact `publish_oid` of the open intent, or an OID the ledger recorded as published (`integration.history`). An intent alone is not proof of publication. The ref equals `publish_oid` → complete. The ref equals `expected_tip` → apply the compare-and-swap again. `publish_oid` is an ancestor of the ref and the ref tip is another `publish_oid` recorded in `integration.history` → complete with that tip. `publish_oid` is an ancestor and the tip is not recorded → `needs-operator` with `integration-ref-foreign`, and the tip is never adopted. Anything else → plan the merge again. If the ref already holds the published result (the complete cases above) but the task's approval has no reviewer-model evidence (an older process moved it), the move is not recorded: the task becomes `needs-operator` with `merge-conflict`, and the operator resets the integration ref to the ledger tip and runs `resolve --action retest`. With no such evidence, the apply-again case plans the merge again instead.
- Before every integrate the integration ref must equal the ledger tip. If it moved outside FFS the task becomes `needs-operator` with `merge-conflict`.

### 15.4 Blockers (amends 14.8)

| `blocker` | State | Clears with |
|---|---|---|
| `scan-refused` | `needs-operator` (admit outbound scan, submit packet scan) | `resolve --action retest` |
| `branch-conflict` | `needs-operator` (task branch at another OID locally or on origin, at admit or submit) | `resolve --action retest` |
| `push-failed` | `needs-operator` (the admit push failed 5 times) | `resolve --action retest` |
| `internal-error` | `needs-operator` (a task step failed 3 times in a row, an unknown collect blocker, or a collect blocker whose mapped move is not legal from the stage's source state) | `resolve --action retest` |
| `integration-ref-foreign` | `needs-operator` (replay found commits FFS did not publish) | `resolve --action reject`, or `abandon` |
| `binary-change` | `needs-operator` (review stage; always, no allowlist) | `resolve --action reject`, or `abandon --retry` |

- The `case-collision` blocker name was removed. Case-colliding paths are `denylist` at collect and `merge-conflict` at integration. A `secret-leak` found at review is `rejected` (see 15.3).
- A scanner failure during collect maps to `quarantine-refused`, not `scan-refused`.
- `grant-missing` is set only for a missing push or `cloud-dispatch` grant. `ruleset-unverified` is set only when active `update` and `deletion` rules cannot be confirmed. A task branch moved or deleted on origin at submit is `branch-conflict`.
- `review-unavailable`, `review-cap`, `review-model-downgraded` and `review-model-unknown` clear with `resolve --action retest` (fresh meter, see 15.5) or `reject`.
- `verify-cmd-missing`: `cloud_collect.verify` refuses an empty or whitespace-only command before any sandbox work (`bash -c ''` exits 0). The router refuses such a task first (`VERIFY_CMD_MISSING`; a task command that is blank falls back to a non-blank `config.verify.cmd`), so this only fires for a hand-edited or older ledger.

### 15.5 Review (`scripts/gsd/cloud-review.sh`; amends 7 step 7)

- Arguments: `<run> <task> <producer> <diff-file> <summary-file> <generation> [attempt]`. The last output line is `CLOUD-REVIEW: APPROVE|REVISE|UNAVAILABLE|cap` with exit 0, 1, 3 or 4 (2 usage). The verdict is the last non-empty line of the reviewer output, exactly `VERDICT: APPROVE` or `VERDICT: REVISE`. Worker lines that start with `VERDICT:` are prefixed with `[quoted-worker-text]`.
- Model check: `adversary-host.sh` sets `ADVERSARY_LAST_REQUESTED_MODEL`, `ADVERSARY_LAST_ANSWERED_MODEL` (from its out-of-band rung file) and `ADVERSARY_LAST_TIER_DESCENT` in the caller's shell. `cloud-review.sh` calls it without a subshell and, when the reviewer ran, prints `CLOUD-REVIEW-MODEL: requested=<m|-> answered=<m|-> descent=<0|1>` as the line directly before the verdict line. `cloud_collect.review` reads only that position. `APPROVE` becomes `reviewed` only when both models are named, equal, and `descent=0`; the review record then also carries `model` and `requested_model`. Otherwise the task goes to `needs-operator` with `review-model-downgraded` (answered model differs, or a descent was reported) or `review-model-unknown` (line absent, `-`, or malformed). Ledger validation keeps accepting `review` records without the two fields.
- Round accounting runs first: `gates.py loop-round <R> "review:cloud:<T>:g<G>[:a<N>]" --max 2 --durable`. Attempt 0 has no `:a` suffix. `resolve --action retest` on a review blocker increments `review_attempts`; the dispatcher passes it as `attempt`, capped at 2. After a `review-stale` the attempt is offset by 3 (so 3 to 5): a stale re-review runs on its own bounded meters and never spends the operator's retests. A third retest is refused (`BLOCKER_MISMATCH`), counted from `review_stale_base` once a stale re-review has begun: a failed stale re-review leaves the operator the retests they had before it. A task generation therefore has at most six meters of two rounds (attempts 0 to 2, and 3 to 5 after a stale review).
- The dispatcher starts the script with an allowlisted environment (`PATH`, `HOME`, locale, `TMPDIR`, CLI config dirs, and the `FFS_*`, `GSD_*`, `ADVERSARY_*`, `GATES_*` variables the script reads). No API keys.
- Lookup: `<lib dir>/scripts/gsd/`, then `<lib dir>/../scripts/gsd/`. Never `<root>/scripts/gsd/` (the target repository). The script sources `adversary-host.sh` and `fence-data.sh` from its own directory. `adversary-host.sh` sources `run-bounded.sh` and resolves `<script dir>/../../lib/gates.py` and `<script dir>/../../lib/model_requests.py`. The closure is `scripts/gsd/{cloud-review.sh,adversary-host.sh,run-bounded.sh,fence-data.sh}` and `lib/{gates.py,model_requests.py}` under one lib root. The installer ships all of it to the user-scope managed lib (`~/.claude/lib/feature-fix-swarm`), with `gates.py` and `model_requests.py` shipped a second time under `lib/`.
- `doctor` reports `FAIL REVIEW_UNAVAILABLE` when `cloud-review.sh` or `adversary-host.sh` is not found, and `OK REVIEW_SCRIPTS` otherwise. It does not check the other closure files.

### 15.6 Doctor, scan, guard (amends 8.1 and 10)

- Added checks: `LOGIN_CLAUDE` / `LOGIN_CODEX` (OK) or `LOGIN_UNVERIFIED` (WARN), a best-effort probe of `claude auth status` and `codex login status` when `--help` lists the subcommand (15 s timeout); `ORIGIN_HAS_CREDENTIALS` (FAIL); `REVIEW_SCRIPTS` / `REVIEW_UNAVAILABLE`. `SANDBOX` fails when `sandbox-exec` is missing or the startup self-check fails.
- `config.scan.allow` entries are `{"blob": "<oid>", "class": "<class>"}` or `{"path": "<repo path>", "class": "<class>"}`. A path entry is what allows a `secret-filename` finding, whose location carries a path id. A binary-blob finding can be allowed only by blob id. The allow list is part of the scan cache identity: each cache entry carries the allow-list signature, and an entry with another or no signature is ignored, so changing the allow list forces one full rescan. The JWT pattern needs a character outside `[A-Za-z0-9_-]` (or the start of the text) before `eyJ`, so a JWT directly after `-` is not matched.
- `config.verify.read_roots` is a list of absolute directories. `config.review.binary_allow` was removed.
- The guard's cloud patterns (`claude --cloud`, `codex cloud exec|apply|diff`, `cloud-dispatch.sh|cloud_dispatch.py adopt|abandon|resolve`) match by executable position: the first token of a segment after leading `VAR=value` assignments, wrappers (`env`, `command`, `exec`, `sudo`, `nohup`, `time`, `rtk`, `nice`, `stdbuf`) and shell or Python launchers, compared by base name. The older operator-only patterns match by token membership. The guard still fails open.

### 15.7 Provisioning and secrets (amends 9)

- The `SessionStart` entry is `bash "$CLAUDE_PROJECT_DIR/.ffs/cloud/session-start.sh"` with a 300 s timeout.
- `env-check.sh <claude|codex>` exits 2 without a provider and prints exactly one JSON object `{"tools":{…},"present":[…],"missing":[…]}` with one version token per tool. `env-check.sh <provider> --status` prints the setup status JSON or `null`. The worker packet appends the provider (`lib/handoff_packet.py`).
- `setup.sh` re-execs itself under `env -i`. That gives installers a clean environment only. On Linux an installer can read `/proc/<pid>/environ` of every same-uid ancestor, including the Claude Code process that started the hook. `env -i` is the only isolation, and the `/proc` exposure is a documented residual risk. The setup status file records it as `"secret_isolation":"env-i-only"`.
- `config.secrets.network_secret_hosts` is in the template, and no code reads it.

### 15.8 Skill exceptions (amends 11)

The three differences without the flag of section 11 are unchanged. `skills/spec-status/scripts/collect-status-facts.sh` prints its read-only `== CLOUD ==` section only when at least one run ledger exists under the main worktree's `.feature-fix-swarm/cloud/runs/`. A repository that never used `--cloud` sees no change.

### 15.9 Stated limits (extends 13)

- Nonce binding is correlation, not authentication.
- A worker that holds a secret can leak it before FFS scans. Encoded secrets can evade the scanner: it matches patterns in raw bytes and does not decode.
- The operator-only guard fails open.
- Model and effort are unverified in the cloud.
- The sandbox residual gap and the Mach service residual risk of 15.2, and the Linux `/proc` exposure of 15.7.
- A lone `parked` run keeps an expired slot until any orchestrator resumes or `cleanup` runs (slots are reaped only in an orchestrator tick and in `cleanup`). Workaround: `cloud-dispatch.sh orchestrate --run <id> --resume`.
- `cleanup` during a concurrent `start` of the same run id can delete that run.
- The JWT scanner gap of 15.6.
- Live coverage is limited to the Claude backend on one trivial task. See 15.10.

### 15.10 Live verification (2026-11-20, Claude Code 2.1.329; Claude backend only)

One trivial docs task, driven through `cloud-dispatch.sh` directly. The operator view is in `docs/cloud-delegation.md`, "Live verification".

Seen live:

- `claude --cloud "<prompt>"` refuses to run without an interactive terminal, so the backend starts it on a pseudo-terminal. This is undocumented provider behaviour and can break on a client update.
- The client shows its folder trust dialog for the launch clone. Trust is per git root, so a trusted parent directory does not cover a new clone. FFS never answers the dialog. A launch that meets it ends `failed` (`CLI_EXIT BLOCKED_DIALOG`) and creates no session. The operator sets `FFS_CLOUD_CLONE_PARENT`, which fixes the clone path at `<parent>/ffs-cloud-launch-claude`, clones the repository there, and accepts trust once by running the interactive client in that directory in a real terminal. `probe-claude --dir <path>` shows the first screen without creating a session.
- After trust: the launch took about 8 seconds, the client printed the session lines of section 5, exited by itself, and the session id and URL were parsed.
- The cloud worker pushed `claude/ffs-<R>-<T>-<nonce8>` about 2.5 minutes after launch. The committed `SessionStart` provisioning ran: the manifest `env_report` listed tool versions.
- Killing the orchestrator while the task was polled, then one `orchestrate --resume`, produced no second launch (launch count 1, generation 1).
- The first bound attempt failed: the worker wrote `tests_run` as strings, and the packet had not stated the item shape. The contract text now states it (2.5).
- The next run passed every gate: nonce binding, isolation, path and secret checks, review by the opposite family (verdict `APPROVE`, recorded with the diff sha and patch id), the sandboxed verify command (rc 0) and integration to `ffs/cloud/<R>/integration`. Launch to `integrated` took under 3 minutes.

Not verified live: the Codex backend; whether `--model` and `--effort` take effect under `--cloud` (still `requested-unverified`); more than one task; dependencies between tasks; the installed `/swarm --cloud` skill path; secrets modes; restart driven by `reconcile`; a client stuck on a screen other than the trust dialog.

Findings that stay open:

- `cleanup --remote` does not delete the provider-created `claude/ffs-*` branch. The operator deletes it by hand.
- A run whose task never binds stays `active`, and `cleanup` refuses it (`RUN_NOT_DONE`) until the operator abandons the task.
- The review output itself is not kept in the run directory. Only the verdict record is in the ledger.
