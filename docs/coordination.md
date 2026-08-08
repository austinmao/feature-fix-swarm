# Coordination layer

Cross-session claims, resource leases, and the runner wiring that takes them
automatically. This is the reference the coordination layer has been
accumulating obligations toward since Phase 1.

## Overview

Two sessions can plan overlapping specs at the same time with nothing
stopping them — the founding incident behind this whole layer: two sessions
planned overlapping work on the same spec, and it was caught only by a
human-relayed handoff document, after both sessions had already done real
work. `scripts/coord/coord.py` gives every session one shared registry to
claim a spec, lease a resource, and revalidate that claim as work proceeds.
Phases 1-3 built the claims, the leases, and an edit-time guard
(`scripts/hooks/path-reservation-gate.sh`); Phase 4 (this guide) is what
makes `scripts/gsd/gsd-run.sh` take a claim automatically instead of relying
on a human remembering to run the CLI.

## Store and modes

The registry lives at exactly one path per repository, regardless of how
many linked worktrees exist: `<git-common-dir's-parent>/.feature-fix-swarm/coord/registry.json`.
Every worktree of the same repository resolves to that one directory, so a
claim taken from a linked worktree is visible to a session running from the
main checkout and vice versa. `.feature-fix-swarm/` is gitignored — no
tracked source lives there.

Three modes govern enforcement, resolved with `FFS_COORD_MODE` (environment)
first, then a persisted `mode` file inside the store, then the built-in
default:

| Mode | Behavior |
|---|---|
| `enforce` (default) | Every command applies its full contract — a held claim refuses a second claimant, a lease conflict blocks. |
| `audit` | The same decisions are computed and logged, but nothing is refused. |
| `off` | Every coord command prints `COORD-OFF` and exits 0 without touching the registry. |

## CLI reference

Exit codes below are this layer's own extended contract (Phases 1-2, extended
never redefined by Phase 4): `0` success, `2` usage/validation, `3` REFUSED
(a live foreign holder, or a refused release), `4` SUPERSEDED (your
generation was revoked), `64` malformed environment input, `69`
COORD-UNAVAILABLE (filelock missing/below floor, unreadable store), `75`
COORD-CONTENTION (lock timeout, retryable), `78` store/config refusal
(symlinked store, not a git repository, borrowed identity).

| Subcommand | Arguments | Success (stdout) | Refusal (stderr) | Exit |
|---|---|---|---|---|
| `claim <spec-id>` | `[--ttl N] [--heartbeat N]` | `session=<uuid>` then `CLAIM-OK generation=<n>` | `CLAIM-HELD holder=... anchor_pid=... worktree=... expires_at=...` | 0 / 2 / 3 / 64 / 69 / 75 / 78 |
| `claim-check <spec-id>` | `--generation N` | `CLAIM-OK` | `CLAIM-SUPERSEDED caller_generation=N current_generation=M` | 0 / 4 |
| `claim-renew <spec-id>` | `--generation N [--ttl N]` | `CLAIM-OK generation=<n>` | `CLAIM-SUPERSEDED ...` (generation mismatch or gone) or `CLAIM-HELD ...` (foreign holder) | 0 / 3 / 4 (plus 69 / 75 / 78 store-failure arms — the runner budgets these, see Runner lifecycle) |
| `release <spec-id>` | `--generation N` | `RELEASE-OK` | `RELEASE-REFUSED: foreign holder` / `: stale generation` | 0 / 3 |
| `lease-acquire` | `--resource <key> --mode shared\|exclusive [--ttl N] [--heartbeat N]` | `session=<uuid>` then `LEASE-OK generation=<n>` | lease-held listing | 0 / 3 |
| `lease-renew` | `--resource <key> --generation N [--ttl N]` | `LEASE-OK generation=<n>` | `LEASE-SUPERSEDED caller_generation=N current_generation=M` | 0 / 4 |
| `lease-release` | `--resource <key> --generation N` | `RELEASE-OK` | `RELEASE-REFUSED: stale generation` / `: not a recorded holder` | 0 / 3 |
| `status` | (none) | one line per live claim and lease holder (holder, generation, anchor_pid, worktree, `last_renewed_at`, `ttl_secs`, `expires_at`, and any `flags=`) | — | 0 |
| `doctor` | (none) | see [Troubleshooting](#troubleshooting) | — | 0 / 69 |

## Identity and environment variables

| Var | Sets what | Read by | Absent |
|---|---|---|---|
| `FFS_RUN_ID` | The spec-stable identity a run resolves to across separate invocations, via the `sessions/by-run/<run_id>` pointer | `coord.py`'s identity resolution | A fresh, unrelated session UUID is minted every invocation |
| `FFS_COORD_SESSION` | An explicit session UUID to act as | `coord.py`'s identity resolution (checked before `FFS_RUN_ID`) | Falls through to the `FFS_RUN_ID` pointer, then to minting fresh |
| `FFS_COORD_ANCHOR_PID` | The long-lived process whose liveness anchors this claim | `coord.py`'s reclaim decision | Falls back to `os.getppid()`, which for a transient subshell is already dead by the time a peer checks — a claim stale-on-arrival |
| `FFS_COORD_MODE` | `off` \| `audit` \| `enforce` | `coord.py`'s mode resolution (env beats the persisted mode file) | Falls to the persisted mode file, then the `enforce` default |
| `FFS_COORD_STORE` | An explicit store path, overriding git resolution | `coord.py`'s store resolution | Store anchors at git-common-dir's parent |
| `GSD_RUN_ID` | The runner's own spec-stable run id — becomes `FFS_RUN_ID` inside `gsd-run.sh` | `scripts/gsd/gsd-run.sh` | The runner takes NO coord claim at all (see [Runner lifecycle](#runner-lifecycle)) |

## Runner lifecycle

`gsd-run.sh` touches the claim at four points, all additive to the existing
runner:

1. **Claim, after taking its own run-state pidfile.** The claim call runs
   inside `acquire_run_state`, after the pidfile ownership confirmation and
   before the heartbeat subshell forks. A caller that exported an explicit
   `GSD_RUN_ID` gets an attempted claim on that id; a caller that did not
   gets NO claim at all — the runner's own auto-derived run id
   (`date+pid`) is unique per invocation by construction, so claiming it
   could never return CLAIM-HELD and would only cost a registry write while
   showing the run as "coordinated" with zero collision protection. A
   launch-time coordination failure fails the launch closed, propagating
   coord.py's own exit code (2/3/4/64/69/75/78) verbatim — never remapped to
   the runner's own pidfile-contention 75.
2. **Renew and revalidate on every heartbeat tick.** One `claim-renew` call
   per tick (default 15s, four times more frequent than the claim's own
   60s heartbeat assumption) serves both purposes — a successful renew
   extends the claim's expiry, and its exit code IS the revalidation
   verdict.
3. **Abort the drive on a revocation.** `claim-renew` returning 3 or 4 means
   the claim's authority has been revoked (a foreign session took it over,
   or the generation was bumped out from under this run); the runner stops
   the drive with `CLAIM-SUPERSEDED` on stderr. Any OTHER non-zero (a
   transient lock timeout, a momentary unreadable store) is logged and the
   drive continues — but only for as long as the claim is still plausibly
   ours: see [Staleness and reclaim](#staleness-and-reclaim).
4. **Release in the exit trap.** Every exit path — normal completion, a
   non-zero drive, a `run_bounded` timeout, an external SIGTERM, and the
   revocation self-kill — unwinds through the same `cleanup_runner` trap,
   which releases the claim.

An ad hoc invocation without an explicit `GSD_RUN_ID` (a bare `/gsd-quick`,
for instance) takes no automatic claim and is not protected by this layer at
all — see [Manual CLI path](#manual-cli-path) below for how to protect it.

## Manual CLI path

Any interactive session, and any runner invocation without an explicit
`GSD_RUN_ID`, gets no automatic protection. Claim manually before starting
work on a spec:

```bash
export FFS_RUN_ID=spec-NNN   # or whatever spec-stable id this session uses
python3 scripts/coord/coord.py claim spec-NNN
```

The first line of output is `session=<uuid>`; the second is
`CLAIM-OK generation=<n>` on success, or `CLAIM-HELD ...` on stderr with a
non-zero exit if another live session already holds it. Revalidate
periodically while the work is ongoing (this is what the runner's heartbeat
does automatically):

```bash
python3 scripts/coord/coord.py claim-renew spec-NNN --generation <n>
```

Release when done:

```bash
python3 scripts/coord/coord.py release spec-NNN --generation <n>
```

## Staleness and reclaim

Reclaim keys on process liveness and a start-token check, not wall-clock age
alone. A holder's `holder_anchor_pid` recorded at claim time is the process
whose life the claim tracks:

- If that pid is dead, or alive but its start token no longer matches (a
  recycled pid), the holder is reclaimable immediately — a peer's claim
  succeeds at generation+1 with no TTL wait.
- If the pid is alive and its start token matches, the holder is NOT
  reclaimable regardless of age, in the normal (probeable) case.
- Only when the anchor state is UNPROBEABLE (no token source module-wide,
  a foreign host recorded, null anchor fields, or a `PermissionError`
  reading that specific pid's token) does age become the deciding factor —
  reclaimable once the claim's own `ttl_secs` has elapsed since its last
  renewal, and not sooner.

The exit trap releasing the claim is the NORMAL path — every ordinary run,
successful or not, ends there. Reclaim exists for the CRASH path: a `kill
-9`'d holder, a machine that vanished, a process whose exit trap never ran.

## Rollout

`filelock>=3.30,<4` is a genuine runtime dependency of the coordination
layer, but it ships through the contributor-only `requirements-dev.txt`
channel (`docs/dependencies.md`'s stated convention is that contributor-only
tools never reach a consumer repository). That means a repository that has
not run `python -m pip install --requirement requirements-dev.txt` will see
coordinated launches refused under the `enforce` default the first time this
ships, with exit 69 (COORD-UNAVAILABLE).

Two escape hatches, both printed inline on that failure so an operator does
not need to find this document first:

1. Install the dependency: `python3 -m pip install --requirement requirements-dev.txt`.
2. Or turn coordination off for this invocation: `FFS_COORD_MODE=off`.

Run `python3 scripts/coord/coord.py doctor` first to see exactly which of
these applies — it never raises, and it names the offending state instead.

## Trust boundary and known limitations

Four things this layer does NOT do, stated plainly rather than absorbed
silently:

1. **Writes made through the shell tool are not intercepted by the edit
   guard.** `scripts/hooks/path-reservation-gate.sh` only sees `Edit` /
   `Write` / `MultiEdit` tool calls; a `Bash` heredoc or `sed -i` bypasses it
   entirely.
2. **Lease fairness has no waiter queue in this version.** A steady stream
   of shared holders can starve an exclusive waiter indefinitely — there is
   no first-come-first-served ordering.
3. **Coordination is cooperative on a single-UID machine, not a security
   boundary.** Any process running as the same user can edit
   `registry.json` directly, or export a borrowed identity. This layer
   prevents accidental collision between cooperating sessions; it is not a
   defense against an adversarial one.
4. **A headless sandboxed drive can write the shared coordination and
   evidence directory at the main checkout, not just its own worktree
   copy.** Phase 4 widens the Codex sandbox's `writable_roots` to include
   `PROJECT_PRIMARY_ROOT/.feature-fix-swarm` (P-29) — needed because the
   store anchors there, not in the run worktree, and a sandboxed claim write
   was unconditionally denied before this. The grant is bounded to that one
   gitignored subtree; no tracked source becomes writable. This is an
   accepted, bounded widening over the existing cooperative single-UID
   model (item 3 above), not a new trust boundary.

## Troubleshooting

`coord.py doctor` never raises — it names the offending state and returns
this layer's own exit-code contract instead of a traceback. Field-by-field:

| Field | Meaning | Bad value → what to do |
|---|---|---|
| `filelock_version` | The installed `filelock` package version | Missing entirely, or exit 69 before this line even prints: install per [Rollout](#rollout) |
| `store_path` | The resolved registry directory | Points somewhere unexpected: check `FFS_COORD_STORE` and which git worktree you are in |
| `mode` / `mode_source` | Effective enforcement mode and where it came from (`env` / `file` / `default`) | Unexpectedly `off`: check `FFS_COORD_MODE` and the store's `mode` file |
| `staleness` | `ok` if the process-liveness/start-token module resolved, `degraded` otherwise | `degraded`: reclaim falls back to TTL-only age for every holder on this machine — see [Staleness and reclaim](#staleness-and-reclaim) |
| `live_claims` | Count of currently held claims | Unexpectedly non-zero after work should be done: a claim leaked (crash without exit-trap release) — it will self-heal at TTL, or release it manually |
| `live_leases` / `lease_holders` | Count of live lease keys / total holders across them | Same as above, for leases |
| `escaped_leases` | Lease keys whose resolved path no longer resolves inside the worktree root | Diagnostic only, never enforced — investigate the specific key named |

One mid-run arm deserves its own note: a `claim-renew` returning 78 (the
store was moved, or its path symlink-swapped, out from under a live run) is
logged and the drive continues for the moment — 78 is not in the immediate
revocation set {3, 4}. But it counts against the staleness budget like every
other non-success: if renews keep failing, the runner presumes the claim
reclaimed once `ttl_secs` elapses since the last successful renew and kills
the drive (`CLAIM-STALE`). The budget acts on elapsed-time-without-success;
it does not need to read the claim back. The interim window is safe rather
than lucky: a vanished store blocks every peer from claiming too, so there
is no second writer to race against while the budget runs down.
