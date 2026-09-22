# Data model and state transitions: spec 014

Status: prospective implementation contract. Legacy data remains unchanged until M8 safe cutover.

## Storage and trust boundaries

One supervisor-owned SQLite control database at `<state_root>/control.sqlite3`; `<state_root>/runs/<run_id>/` stores durable evidence outside repositories/worktrees. `<state_root>` is private, canonical, symlink-checked, local-filesystem-only, and resolved by trusted configuration; inherited worker overrides cannot select a sibling authority. Workers can write only `<run>/activities/<activity>/attempts/<attempt>/progress/` and their own evidence; supervisor accepts validated progress through an authenticated local channel and writes authoritative state. Sandbox controls enforce the advertised tool boundary; shared-UID hostile processes are explicitly outside the security claim.

SQLite foreign keys enabled; short `BEGIN IMMEDIATE` transactions, rollback journal, FULL synchronous durability, bounded busy refusal. No agent/model/network/Git spawn under a database transaction. Auxiliary manifests use exclusive temporary creation, flush/fsync, anchored atomic replacement, and directory fsync. Mutations append audit/outbox rows in the same transaction; evidence export is replayable/idempotent from that outbox. No success response before control commit.

## Entities

| Entity | Fields and keys | Validation/invariants |
| --- | --- | --- |
| Repository | `repository_id` UUID, canonical Git common-dir, filesystem identity, registration time, workspace root, legacy source mappings | Repository identity is independent of invoking checkout; common-dir/path changes require explicit verified rebind, never guessed by basename. Workspace root cannot be primary or symlink escape. |
| Run | `run_id`, repository FK, objective digest, objective text, planning scope, state, workspace FK, evidence root, created/updated UTC, operator host request | One validator accepts existing supported IDs unchanged (ASCII alphanumeric start/end, interior dash/underscore, ≤64 bytes); anonymous IDs use `adhoc-` plus full random UUID hex. Legacy 12-hex/spec IDs map explicitly without truncation. Unique active objective reservation per repository/scope/digest. |
| Activity | UUID, run FK, kind `plan/execute/review`, input digest, revision number, state, result locator/hash, remaining retry/respawn/quota budgets, token counters | One unfinished activity at a time per run; explicit revision advances revision, repeated successful identical input reuses result. Budget belongs here, attempts cannot reset it. |
| Attempt | UUID, activity FK, ordinal, state, owner FK, runtime-tuple FK, timestamps, exit code, failure category, log root, token increments | Unique activity/ordinal and immutable runtime binding. Token events use idempotency keys; negative/increment-overflow and duplicate debit rejected. |
| Workspace | UUID, repository/run FKs, canonical absolute path, Git worktree identity, branch/ref, base commit, selected-input manifest hash, upstream project/workstream/session key, readiness state | Unique canonical path and run binding; branch `ffs/runs/<run_id>` owned by run. Selected-input manifest lists path/content hash/provenance, never unrelated dirty files. |
| Resource owner | resource kind/key (`run/workspace/objective`), generation, random nonce, supervisor/child host+boot+PID+start identity, heartbeat, state | Unique resource key; admission reserves all three keys in one transaction. Generation persists after release and only increases. Full identity determines LIVE/DEAD/UNKNOWN; age/missing workspace alone never establishes death. |
| Launch intent | UUID, attempt FK, reservation nonce/generation, budget-debit id, child identity, state, acknowledgement/release timestamps, supervisor identity | Unique intent per attempt; create/debit before spawn, child validates fence before host entry. Replaying an intent cannot create a second side-effect-capable child. |
| Runtime tuple | UUID/content hash, host, exact model/tier/effort, binary identity/version, FFS/GSD bundle hashes, config hash, capability result ids, policy/network/tool profile | Immutable; no credentials or bearer tokens stored. Resume compares all material fields; credential value rotation may occur only via verified adapter without altering permission profile. |
| Grant | run FK, exact typed action/target, issuer/provenance, expiry, consumed/idempotency key | Existing authorization preserved; migration never expands action patterns. Real commits/push/releases/tenant deployment remain unauthorized for this run. Only supervisor creates/consumes grants. |
| Evidence index/outbox | run/activity/attempt FKs, stable event id, kind, source hash, locator, auth/hermetic label, reviewer provenance, payload, export status | No empty successful required gate; body stored outside removable workspace. Append/record belongs to same fenced control transaction; exports may retry without duplicates. |
| Migration journal | source identity/hash, schema/writer epoch, record key, disposition/import target, conflict reason, handoff proof, checkpoint, rollback state | Unique source-hash/record key; no duplicate import; preserve source; conflicts never overwrite. New writer epoch cannot activate with live/unknown old ownership. |
| Installation/rollout manifest | runtime/consumer, source identity/hash, destination layout, prior/current hash, ownership, modified flag, backup locator, fork adjudication, canary refs, activation checkpoint | Resolve every destination including external skill root; no namespace-prefix ownership assumption. Before replacement, recheck live hash; concurrent modifications refuse replacement. |

## State machines

Run: `preparing → idle → active → idle`; terminal `complete/failed/aborted`, recoverable `blocked`. Completion is explicit after required activities/gates, not merely one child exit. Failed/blocked runs remain inspectable. A completed run may be explicitly revised by a new activity; immutable prior results remain retained.

Activity: `pending → active → succeeded`; interruptions become `paused/failed`, recovery requires explicit resume. `succeeded` returns stored result for identical input. Revised input receives new revision/activity after explicit intent; no implicit rerun.

Attempt: `reserved → starting → running → succeeded/failed/paused`; terminal attempts remain immutable except replay-safe accounting/evidence completion. New attempt requires a remaining budget debit and a new intent. Quota pause retains original counters.

Workspace: `reserved → preparing → ready → finalizing → removed`; failed preparation becomes `blocked` with owned-resource manifest. No planning/wall/evidence producer or stateful preflight runs before ready. Supervisor preparation records are control/audit bookkeeping, not activity evidence writes in the primary checkout.

Ownership: `reserved → held → released`; revoked increments generation. Reclaim requires recorded release or full-identity DEAD; LIVE/UNKNOWN blocks even after expiry. Missing worktree is a repair signal only.

Launch intent: `reserved → spawned → acknowledged → released_to_execute → finished`; ambiguity becomes `reconcile_required`. Child waits behind the handshake until committed release. No timeout means automatic replacement. A dead pre-execution child closes its spent intent; retry creates a separately counted attempt.

Migration: `legacy → readers_ready → dual_read_verified → handoff_ready → new_writer_active`; rollback `new_writer_active → writes_frozen → compatible_writer_selected`. Never both writers; rollback may leave run paused with new evidence readable if old code cannot safely interpret new state.

## Atomic admission and recovery invariants

1. Canonical resolver validates run/repository/input request without mutation; request ambiguity returns candidates.
2. One transaction checks/reserves run, workspace, and objective keys and captures monotonically fenced ownership; conflicting active reservations refuse.
3. Git workspace preparation runs outside the DB lock under a short admin lock. Revalidate fence before publishing readiness. Crash recovery reconciles actual Git registration and manifest before retry/removal; it does not allocate a second workspace silently.
4. Persist attempt, budget debit, and launch intent atomically. Spawn child with an unforgeable per-attempt channel nonce kept out of logs. Child returns full identity and blocks.
5. Supervisor verifies nonce, parent reservation, current fence, runtime tuple, and child identity; commits acknowledged/released state before signaling execution. On interrupted signalling, child asks the supervisor protocol for the committed state rather than inferring permission from output freshness.
6. On takeover, no signal or control write targets an existing child until full-identity liveness and current fence are established. A living orphan can be adopted only by a fenced explicit recovery handshake; UNKNOWN remains blocked. Expiry cannot replenish budgets.
7. Each authoritative mutation takes expected generation/nonce and atomically rejects mismatch. Workers cannot bypass through direct grants/gates/coord commands; adapters enforce authenticated supervisor role.
8. Finalizer exports pending evidence first, verifies ownership and resource hashes, removes only listed workspaces/resources, and retains control/evidence. Landing/publishing remains a separate serialized authorized operation.

## Legacy compatibility

Preserve and inventory current `~/.claude/state/runs.db`, Git-common-dir `.ffs-coordination`, `.git/ffs/gsd-run`, primary `.feature-fix-swarm/evidence.json`, and `.planning/run-state` records. Exact actual locations are resolved by existing readers and recorded, not assumed from this list. Schema adapters produce comparable canonical views without writes. Existing `GSD_RUN_ID`, `GSD_RESUME`, `FFS_RUN_ID`, coord IDs, and gate run mappings retain explicit one-to-one aliases. A conflicting alias is quarantined and cannot authorize takeover.

M3–M7 implement/test new authority only in isolated fixture stores. During rollout, M8 writer-epoch activation occurs under the existing short coordination lock after safe handoff; previously launched sessions retain legacy reader/runtime recovery information. Compatibility-reader installation can precede dependency activation only when required; neither changes live leases.
