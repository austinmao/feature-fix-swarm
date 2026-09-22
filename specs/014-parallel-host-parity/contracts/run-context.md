# Run context, supervisor, and CLI contract

Version: 1 (prospective). Covers FR-011–028 and FR-048–050. Existing CLI forms keep their current positional syntax and exit meanings; new operations use the typed envelope below. This contract is implemented after M2 and operates on fixture stores until M8 safe writer cutover.

## Canonical request and selection

`ContextRequest` fields: repository/cwd identity; optional explicit run ID; optional selected activity ID; operation; objective; input digest; planning scope; explicit resume/revise flags; host/model/tier request. The resolver is side-effect-free and returns a selected registered context or typed refusal/candidates. It never chooses latest by timestamp.

Precedence: explicit selected run, compatible `GSD_RUN_ID`/`FFS_RUN_ID` legacy aliases, otherwise anonymous unique ID for a new objective. Conflicting IDs/repository/workspace bindings fail rather than overriding. `GSD_RESUME` remains a compatibility spelling of explicit resume; it cannot silently continue the wrong unfinished activity. An unfinished activity needs resume; completed identical input reuses its result; revision requires explicit request. The persisted authority, not mutable env or a copied context file, decides ownership/runtime validity.

IDs use one canonical validator and retain supported legacy spellings. New anonymous IDs are `adhoc-<full uuid hex>`; mappings from old runstore12-hex and spec IDs are explicit one-to-one rows. Invalid/overlong/colliding aliases fail, never truncate. Workspace refs/paths use validated IDs only.

## CLI façade

Keep existing `gsd-run.sh '/gsd-…' [args...]` and literal Codex `$gsd-…` command inputs; host-native output uses the selected host's syntax. Existing run-state `start/status/update/complete/abort/list/audit` and coord/gates commands remain compatible. New additive run-state options: `--run-id`, `--activity plan|execute|review`, `--resume`, `--revise`, `--json`, and a supervisor-only validated fixture/state-root override. New read-only `context` inspection and explicit `migrate` administration are justified lifecycle operations, not catalog verbs.

Every complete-run start internally prepares workspace before dispatch; callers never need to remember an optional worktree flag. Skills and walls obtain the same prepared context. Human diagnostics go to stderr in JSON mode; stdout is one JSON document. Existing consumers may continue using their stable text format unless JSON was requested.

```json
{
  "schema_version": 1,
  "ok": true,
  "code": "RUN_READY",
  "run_id": "spec-014",
  "activity_id": "uuid",
  "attempt_id": "uuid",
  "workspace": "/absolute/registered/workspace",
  "evidence_root": "/absolute/private/state/runs/spec-014",
  "generation": 2,
  "runtime_tuple_hash": "sha256",
  "reused_result": false,
  "candidates": []
}
```

New-operation status codes: 0 success/reused result; 2 malformed/invalid request; 3 conflicting ownership or explicit selection needed; 4 stale/revoked owner; 5 evidence/runtime/policy gate unmet; 6 environment/liveness uncertain. Internal codes are stable strings (`AMBIGUOUS_RUN`, `RESUME_REQUIRED`, `OWNER_LIVE`, `OWNER_UNKNOWN`, `FENCE_REVOKED`, `RUNTIME_DRIFT`, `WORKSPACE_PREPARE_FAILED`). Existing wrappers translate to their established exit taxonomy where necessary; no broad exit-code rewrite. Refusal JSON includes safe reason, candidate IDs if applicable, and recovery action; never credentials/nonce.

## Public module interfaces (prospective)

- `resolve_context(request: ContextRequest, store: ReadOnlyStore) -> RunContext`
- `reserve_resources(store: ControlStore, request: StartRequest) -> Ownership`
- `prepare_workspace(context: RunContext, selection: InputSelection) -> Workspace`
- `reserve_launch(store: ControlStore, activity_id: str, token: OwnerToken) -> LaunchIntent`
- `acknowledge_child(store, intent_id, token, process_identity) -> Acknowledgement`
- `authorize_child(store, acknowledgement, token) -> ExecutionPermit`
- `recover_intent(store, intent_id, token) -> RecoveryDecision`
- `accept_progress(store, scoped_channel, progress) -> AcceptedEvent`

Ownership tokens are private capabilities, not JSON/log fields. Each authoritative mutation validates role plus nonce/generation inside its transaction. The supervisor IPC channel limits workers to their own progress/evidence; calling a CLI directly cannot grant sibling authority.

## Required ordering

Validate selection → atomically reserve run/workspace/objective → short Git admin transaction prepares registered unique workspace → validate fence/readiness → bind upstream project/workstream/session → planning/wall/stateful preflight → debit budget and persist attempt/intent → spawn blocked child → full-identity/fence handshake → commit acknowledgement/release → host execution. Database locks never cover Git/model/network work.

All activities/child worktrees resolve one durable run partition; per-attempt subdirectories do not create independent authorities. Finalization harvests pending evidence before manifest-scoped removal. Explicit landing/publication is serialized and still subject to no-real-commit restrictions.

## Recovery and compatibility

LIVE/UNKNOWN owner refuses takeover irrespective of age or missing workspace. PID reuse requires start/boot identity comparison. A surviving child may be reattached only through the fenced recovery protocol; loss of supervisor output is not death. No replacement replenishes activity budgets. Legacy reads are nonmutating until M8 writer-epoch handoff; new writer activation requires safely released/proven-dead old owners and no conflicting writer epoch. Rollback retains new evidence and one compatible writer.
