# Verification evidence and gate contract

Version: 1 (prospective). All 60 ACs and 24 PATHs are mandatory. Authenticated results are separate from hermetic CI. A missing command/file/platform/auth capability is UNMET, not PASS; no placeholder artifact satisfies readiness.

## Prospective runner interface

`scripts/verification/parallel_host_parity.py` supports:

- `baseline --output ABS.json`: inventory/read-only evidence aggregation of actual before-state, local/CI results, backups and coverage; report observed failures without calling baseline green.
- `installation --mode private --output ABS.json`: contained real installer fixture and emitted-runtime scans; never active install.
- `upgrade --baseline ABS.json --output ABS.json`: compare actual upgraded ledger/results to before-state; unexplained new failures fail.
- `review --stage upgraded --output ABS.json`: execute the configured producer-distinct review adapter against actual upgraded artifacts; no empty fallback success.
- `matrix --mode hermetic --repetitions 25 --output ABS.json`: real local process/Git/store fault tests with external host responses stubbed.
- `hosts --authenticated --pairings claude-claude,claude-codex,codex-codex --review-directions claude-codex,codex-claude --soak-seconds 600 [--consumer ABS] --output ABS.json`: actual subscribed host canaries and bounded overlap. Fails unmet if existing auth/platform is unavailable; does not request or print credentials.
- `audit --output ABS.json`: validate findings/regression/refactor/reviewer/pin evidence.
- `coverage --xml ABS.xml --line-min 80 --output ABS.json`: compute first-party line/branch separately with explicit excluded-file inventory.
- `migration --mode verify-legacy --output ABS.json`: nonmutating real dual-read checks plus referenced fixture restart/rollback results; does not activate writers.
- `rollout --consumer ABS --output ABS.json`: full ownership/hash/fork/source/byte/drift/canary validation; does not activate or publish by itself.
- `aggregate --evidence ABS --require-all-paths --output ABS.json`: require every matrix row/AC/PATH and authorization evidence, reject missing/empty/stale/mismatched provenance.

Every command emits JSON to stdout or the explicitly selected output and sends diagnostics to stderr; output file is atomic/private and outside removable worktrees. External programs are invoked as argument arrays with bounded deadlines. Actual activation/migration operations remain supervisor-owned separate commands consuming these results and the current owner fence.

## Result envelope

Required fields: schema version, gate id, run/activity/attempt binding, AC/PATH/INT IDs, status (`PASS/FAIL/UNMET`), platform, actual host/model, authenticated/hermetic label, source/binary/bundle/config hashes, command argv, started/completed UTC, exit status, evidence locators/hashes, reviewer provenance if applicable, errors/unmet reasons. Secrets/owner capability tokens are forbidden. Artifact existence without nonempty validated content does not pass. Aggregate binds results to the actual candidate source/runtime, not a previous checkout's green run.

## Phase01 review authority and exact result pairs

The implemented Phase01 gate IDs are `baseline-capture`, `installation:private`, `upgrade-comparison`, `review-admission:upgraded` and `review-completion:upgraded`. Default review purpose is admission. `--manifest` selects imported review evidence and requires explicit `--purpose review-completion`; imported default admission is `UNMET`/2 with all four gate states false. Imported provenance is labeled `evidence_origin: imported`, `identity_assurance: asserted`; hashes do not prove reviewer identity.

Admission requires a fresh configured adapter actually invoked by this verifier, with observed reviewer process/executable identity bound to its result, candidate and distinct producer. A reviewer-identity attestation mechanism is not defined or implemented in Phase01. Fresh admission also requires an explicitly selected supervisor adapter pin; imported evidence can never become admission through that pin. An imported complete review may establish only review completion and properly assigned isolated repair authorization; it never establishes path admission or rollout readiness.

All Phase01 manifests require closed `binding` (`run`, `activity`, `attempt`) and unique string arrays `ac_ids`, `path_ids`, and `int_ids`. Fresh admission requires each array to be nonempty. Inputs are read through held no-follow directory/file descriptors with per-file, cumulative-byte, artifact-count, and JSON-depth bounds. Physical aliases, hard-linked inputs, FIFOs, devices, changed descriptors, duplicate JSON keys, and unknown fields are refused. A digest is always computed over the same in-memory bytes that are parsed.

Consumers and acceptance tests assert the **status/exit pair**, purpose and vector together:

| Condition / purpose | Status | Exit | Gate-vector consequence |
|---|---|---:|---|
| Complete baseline capture, including faithfully recorded failing suites | PASS | 0 | All authority states false; suite_passed remains false when failures exist. |
| Valid private installer observation with containment assertions passing | PASS | 0 | No launch/rollout authorization. |
| Complete upgrade comparison, no unexplained new failure, valid retained baseline exceptions | PASS | 0 | Remaining failures stay visible; no launch/rollout authorization. |
| Fresh observed clean review for admission | PASS | 0 | review_complete=true, path_admitted=true; rollout_ready=false. |
| Explicit complete review with all findings adjudicated | PASS | 0 | review_complete=true; repair_authorized only for valid assigned repairs; path_admitted=false, rollout_ready=false. |
| Imported ordinary admission, missing evidence/adapter or unavailable capability | UNMET | 2 | No authority granted. |
| Known failed assertion, negative ordinary review/open severe finding, malformed input, runtime escape, new failure or removed test | FAIL | 1 | No path/rollout admission. |
| Unexpected runner failure | FAIL | 3 | code=RUNNER_ERROR; no authority granted. |

Help exits 0 without a gate decision. For all expected refusals, stdout contains one JSON envelope and diagnostics use stderr; output persistence failures never print successful evidence.

Phase01 configuration comes from explicit `--manifest` or `FFS_VERIFICATION_MANIFEST`; for fresh review only the environment input is used. The common object has schema, binding, label, candidate locator/hash and provenance hashes; referenced bytes must validate against the full envelope contract above. Baseline uses `baseline.suite_artifacts` entries with id/locator/sha256/argv/exit_status/started_utc/completed_utc. Upgrade uses the exact CLI `--baseline` file plus `current.suite_artifacts` and `ledger_artifact`. Private installation uses `installation.setup_argv`, explicit fixture roots (root/home/codex_home/cache/state/project), allowlisted child env and timeout_seconds. Fresh review uses `review_adapter` as an argv array and a bounded timeout. These are command inputs, not trusted PASS/completed claims. Closed schemas and dependency-free validator/schema conformance tests enforce agreement.

## Matrix and timing

- Pairings: Claude/Claude, Claude/Codex, Codex/Codex; simultaneous planning walls plus same-relative-path edits.
- Deterministic barriers: before triple reservation, after reservation before worktree, before spawn, after spawn before acknowledgement, after committed release before signal, before fenced mutation, before safe legacy handoff.
- Faults: expired heartbeat, paused owner, reused PID/start identity, revoked fence, quota pause, parent/child crash, interrupted migration, full disk/partial manifest where applicable.
- Repeat each contested/fault variant 25 times per platform with a finite test deadline; one failure fails that row. Bounded live soak is 600 seconds per pairing/platform; process overlap must be observed, not inferred from launch timestamps alone.
- Lifecycle: six operations × two scopes × macOS/Ubuntu. Platform unavailable remains an unmet row; no paid/provisioned unrelated infrastructure inferred.
- Routing: all four native tiers on both hosts, both review directions, exact unavailable request, bounded degraded fallback, subscription auth, tuple drift.
- Security: actual shell/native-tool denial, trusted/ambient hook controls, settings tampering, sibling state/authority, own/shared Git permission boundary, concurrent credential updates.
- Coverage: full first-party Python suite including production scripts; line ≥80%; separate branch numerator/denominator; only vendored/generated exclusions. Existing test files are not production denominators, and production modules may not be omitted to raise the score.

## Independence and authorization

Acceptance-test author differs from implementation owner; reviewers receive artifacts without producer reasoning. Test harness may simulate external model/network boundaries in hermetic mode, but cannot mock ownership/fencing/storage/Git operations it is proving. Host canaries are explicitly authenticated real execution.

Fixtures that require commits create separate disposable repositories and verify their Git common-dir is not either real repository. No gate runs a real-repository commit/push/release/tenant deployment. Existing grants never imply those excluded actions. Evidence records these boundaries and preserved unrelated work.

### Supervisor inputs for Phase01

`FFS_VERIFICATION_AUTHORITY` is an explicit supervisor-selected owner-only regular JSON file beneath a private non-symlink parent outside run workspaces. Workers must never select or write it in the production launcher. Its closed schema `ffs.verification-repair-authority/v1` binds `run`, `candidate_sha256`, exact `assignments`, and optional `adapters`. Assignment keys are `finding_id`, `action` (`assign-repair`), `owner`, `path_ids`, `owning_phase`, `regression_contract`, `authority_id`; every field must match the proposed disposition. Severe assignments keep `blocked_paths` visible. No authority file or mismatch means no repair authorization.

Fresh admission requires an adapter pin with exact `argv`, `label`, `host`, `model`, `executable` locator/hash and `artifacts` locator/hash entries covering interpreted scripts. Validate the actual resolved executable and script bytes before and after invocation, and match observed returned identity to the pin. A hermetic adapter proves only a hermetic gate; authenticated rollout still requires actual host canaries. This private filesystem selection is a cooperative supervisor trust boundary, not cryptographic proof against a hostile same-user process. The manifest itself cannot choose or replace the authority file.

Pinned executable and interpreted-script bytes are copied into a new private review directory before execution. The adapter receives an allowlisted environment and no producer history. Stdout plus stderr are capped at 1 MiB, the deadline is at most 60 seconds, and the verifier owns a new process session. On timeout, overflow, or parse failure it terminates and reaps the process group before returning. A missing executable is `ADAPTER_UNAVAILABLE` / `UNMET` / 2 rather than an unexpected runner failure.

Every review finding has a unique ID, known severity, and state. Open high/critical findings require a one-to-one closed `assign-repair` object in both the manifest disposition and supervisor authority; assignment paths must exactly cover the finding's affected paths. Open low/medium findings require an accountable owner, allowed disposition, rationale, and separately verified evidence. A `resolved` or `refuted` string never suffices: it needs an owner and separately verified adjudication evidence. These adjudicated lower findings do not become active blocked paths. A completion result may authorize only the assigned severe repairs; admission still requires a PASS verdict and no open severe finding.

Fresh admission also requires `authority.producer` to exactly match the producer identity in the review manifest before the verifier compares it with the observed reviewer identity. A manifest cannot relabel its producer to turn self-review into independent review. Completion-only repair evaluation does not require this admission-only producer pin.

Malformed inputs return FAIL/1 before purpose-specific evaluation. Only otherwise valid imported default-admission evidence returns UNMET/2. Result `command` is the actual argv array; `provenance.source_sha256` is computed from candidate bytes, while runtime/configuration observations are recorded separately from imported asserted metadata.

### Private installation prelaunch confinement

Private mode establishes a real OS write boundary before launching candidate code: macOS sandbox-exec denies writes outside fixture and networking; Ubuntu bubblewrap binds only fixture writable over read-only system mounts and unshares networking. A denial probe must pass first; unavailable containment returns UNMET without installer execution. All child home/config/cache/state/temp roots are explicit fixture paths. The child env allowlist is PATH, those private root variables, FFS_SKIP_PROMPT_MASTER/FFS_SKIP_SOCRATIC, and in hermetic mode only the verified first-party FFS_GSD_INSTALLER stub (or an explicit byte-bound installation.stub_artifact beneath the confined fixture) plus fixture-contained FFS_GSD_STUB_LOG. No fallback to upstream network install. Fixture key is codex_home. A task-owned process group is stopped and reaped on timeout before stable inventory can be claimed.

Fixture root values are normalized absolute physical paths. Dot and parent traversal, symlink ancestors, non-directory ancestors, and any child whose physical path is not strictly below the authorized root are refused before directory creation. `setup_argv` is exactly a Bash interpreter followed by the selected candidate locator at index 1; the candidate occurs once, is replaced by the checked staged bytes, and must remain present in the final sandbox command. Probe launch errors and probe deadlines are `CONFINEMENT_UNAVAILABLE` / `UNMET`, with canary cleanup preserved.

The installation manifest is closed: it provides `setup_argv`, the six
explicit fixture roots, a bounded `timeout_seconds`, a byte-bound
`stub_artifact`, an optional byte-bound `nested_stub_artifact`, and the hermetic child environment. A custom stub that delegates to the selected source's first-party stub may execute only its separately checked nested artifact staged inside the fixture; an undeclared delegation is refused. The supervisor authority
separately binds the complete six-path fixture object, canonical root, root device/inode, initial no-link inventory, setup argv,
stub artifact, and optional nested stub artifact. The authority file itself uses the same policy as review authority: it is outside repositories and registered workspaces, is owned by the effective user, has mode 0600, resides in an owner-controlled 0700 directory, and retains the descriptor identity that was checked. The verifier stages selected source in that root, records
actual exit/output digests and a no-follow inventory, and scans partial output
after nonzero exit. Claimed PASS/doctor output never replaces observations.

### Baseline capture scope

Baseline permits optional absolute `repository` and captures the explicitly selected suite artifacts. A disposable repository without origin/main records it unavailable. Capture PASS means that selected collection was observed; `full_baseline_complete` stays false with named `full_baseline_unmet` entries unless production CI/suites/backups/runtime/tool/coverage/environment inventory is complete. M0/M2 production admission cannot use a selected-only capture as a full baseline. Upgrade accepts only a complete emitted baseline envelope, not inline tests/PASS claims. All modes parse the same bytes they hash through the common checked reader. The raw comparison helper retains failures; the CLI may PASS its separate comparison purpose only with no new/missing failures and verified retained exceptions.

Suite descriptors are closed objects containing unique suite ID, result locator/hash, argv, exit status, and UTC start/end. The referenced JSON repeats those execution fields, contains a nonempty test-name to `PASS|FAIL` map, and, when stdout is present, its parsed test observation must match. Exit zero is required for all-pass rows and exit one for a row containing failures. Baseline records missing environment/dependency/config identity as `unavailable`; upgrade cannot PASS a comparison while those current identities are unavailable.

The closed full inventory has exactly these categories: `ci`, `python`, `bats`, `backups_recovery`, `source_runtime`, `tools_customizations`, `coverage`, and `environment`. Each category record is `status: PASS` plus one or more artifact descriptors. A category artifact is `ffs.full-inventory-evidence/v1`, binds the exact manifest provenance and UTC interval, and contains category-specific references to underlying bytes:

- `ci` carries suite artifacts and equal `head`/`origin_main` object IDs; `python` and `bats` carry suite artifacts.
- `backups_recovery` carries named source/restore bytes and a checked verification record whose expected, backup, and restored hashes agree.
- `source_runtime` carries exactly source, binary, bundle, and config artifacts matching the four provenance digests.
- `tools_customizations` carries unique named artifact bytes.
- `coverage` carries coverage XML, explicit production-file inventory, and producing suite artifacts. XML headline line/branch integers must reconcile with unique per-file/per-line data for every listed first-party `lib/`, `scripts/`, and `skills/` Python file. DTD/entity input, traversal, excluded files, duplicates, missing production files, and zero line denominator are refused. Zero branch opportunities are represented by `branch_percent: null`.
- `environment` carries exactly platform, Python, dependencies, and config observations, each as checked `ffs.environment-observation/v1` JSON.

`verified: true`, `complete: true`, nonempty arrays, and hashes of claim-only wrappers do not establish a category. A supplied malformed category is `UNMET`; omitted categories stay named in `full_baseline_unmet`. A complete inventory can still contain observed suite failures, so `full_baseline_complete` and `suite_passed` remain separate.

### M0 canonical profile

`baseline_profile: "m0/v1"` is a compatibility-preserving extension. A legacy manifest without that field retains selected/full inventory behavior, emits `canonical_source_verified: false`, and cannot claim M0 canonical-source proof. M0 emits the selected profile and `canonical_source_verified`; a malformed source proof returns `SOURCE_CANONICAL` / `UNMET`, and malformed or incomplete pinned recovery evidence returns `BACKUP_EVIDENCE` / `UNMET`.

For M0, `source_runtime` and `backups_recovery` artifacts are exactly `ffs.full-inventory-evidence/v2`; all other inventory categories retain v1. The published v2 observation objects, manifest rows, repository identities, mappings, directory-entry variants, backup entries, and plural recovery surfaces are closed schemas. M0 backup observations declare and recover each of these IDs exactly once: `activation-snapshot`, `claude-profile`, `codex-profile`, `gstack-customizations`, `gstack-original`, `homebrew-snapshot`, `node-custom-tap`, `node-global-npm`, `node-old-keg`, and `shared-skill-incident`. A surface may carry multiple recovery byte pairs. Backup and restored files have distinct physical identities and distinct roots, each locator is surface-bound, and the same physical file cannot satisfy another recovery row. This digest-reconciliation record does not replace the separately retained recovery execution report with argv, exit status, and UTC interval.

M0 source observations carry `canonical_source.manifests`; the reserved `upstream_mappings` and `current_dirty_inventory` arrays must remain empty until their closed record formats are versioned. Every install manifest resides at its scope-defined location and maps every normalized installed path once. Relative install sources resolve from the project or user profile root. Project stages equal `project_root / managed_path`; user stages equal the absolute managed path and remain below the profile root. The canonical repository root is observed with `git rev-parse --show-toplevel`; its Git common directory, HEAD, and declared generation must equal the verifier's read-only observation. A candidate may be in that worktree or a Git-declared linked worktree sharing the exact common repository and generation, but its checked bytes must equal its own `generation:path` blob. Each file mapping binds checked source and staged bytes, the install fingerprint, and the immutable blob at `generation:path`. Doctor evidence accepts the legacy `doctor: PASS` marker or structured `ffs.doctor/v1` output with exit zero and only pass/warn checks; copied consumer bytes and matching dirty source/stage bytes remain insufficient.

Directory install paths use the published `dir:<sha256>` FFS recursive fingerprint. Their mapping names checked source and staged roots plus an exact recursive entry list. The verifier enumerates the immutable generation with bounded `git ls-tree`, derives every required parent directory, and requires that inventory to equal the source, stage, and declaration. File leaves bind checked byte artifacts plus Git blob type and `100644|100755` mode; symlink leaves bind identical intended targets and Git `120000` mode. Missing committed leaves, untracked empty directories, type changes, executable-bit changes, traversal, aliases, symlink escapes, and staged-only leaves are refused. Each mapping permits at most 128 entries and each regular leaf at most 2 MiB. Canonical source files use a separate 288-artifact, 512 MiB cumulative budget; Git tree and blob results are bounded and cached by repository/generation/path. This directory form is the required representation for an FFS install-manifest directory fingerprint; arbitrary directory hashing is not accepted as an artifact substitute.

The upgrade ledger is read from the exact `ledger_artifact`. A complete ledger has at least one closed target entry with old/new version, source, manager, argv, rollback, backup, runtime, recovery, incompatible status, and one or more checked evidence artifacts. Each evidence JSON must reproduce every target field. Optional profile progression is exactly profile/canary/profile; every row has byte-bound `{kind,status,candidate_sha256}` evidence and the first canary must PASS. `complete: true` with no entries is incomplete. Each retained baseline failure also needs a unique candidate-bound owner/evidence record in `baseline_exceptions`; it remains visible and keeps `suite_passed=false`. New failures and missing tests take FAIL precedence so an incomplete ledger cannot hide the observed regressions.

### Attributable changed-tuple upgrades

An upgrade with different provenance must preserve both tuples. Its closed `comparison_binding` has exactly `id`, `run`, `repository`, `before`, `after`, and `transition_artifact`. The before/after records have exactly binding, provenance, and a checked `ffs.upgrade-environment/v1` identity artifact. Each identity carries exactly source, binary, bundle, config, dependencies, and environment artifact descriptors. The four named provenance digests reconcile with their corresponding component bytes; dependency and environment observations must be complete.

The checked `ffs.upgrade-transition/v1` artifact binds the exact baseline SHA-256, both identity SHA-256 values, exact ledger SHA-256, logical run, and canonical repository. Its closed changes array lists exactly every difference across source, binary, bundle, config, dependencies, and environment, with matching before/after identity-artifact digests and nonempty checked raw evidence. A foreign run/repository, a missing or extra component, digest mismatch, malformed identity, or absent evidence is FAIL. Every historical and current suite observation must be an object with nonempty environment, dependencies, and config identity fields; null, empty, unavailable, or count-mismatched metadata cannot establish a comparison. A missing historical observation or incomplete full baseline is UNMET. Equal tuples may omit this object for compatibility, but still require the complete historical baseline and ledger checks. Results retain `before_provenance`, `after_provenance`, and nullable `comparison_binding_sha256`; factual `provenance_drift` may be true when `comparison_passed` is true.

The repository name in every attributable binding, identity, and transition is exactly the baseline's observed `repository.common_dir`. The before environment cannot be re-declared: source/binary/bundle/config identity artifact digests each match the unique retained `source_runtime[].validated.entries` digest; dependencies matches the retained checked `environment[].validated.entries.dependencies.sha256` (`ffs.environment-observation/v1`, name `dependencies`); and environment matches the retained environment inventory wrapper descriptor digest. Multiple baseline rows are valid only if every required anchor digest agrees; conflicting observations are `UNMET` rather than selecting an ordering-dependent row. These baseline anchors apply to before only, so the after identity can represent an accountable transition.

The after identity is an observation of the current tuple, not a reusable before artifact. Its config descriptor must equal after provenance and the `config_sha256` value of every current suite. Its dependencies descriptor is exactly `ffs.environment-observation/v1` with name `dependencies` and a value equal to every current suite dependency value. Its environment descriptor is a complete passing `ffs.full-inventory-evidence/v1` environment wrapper carrying after provenance and typed platform/dependencies/config observations equal to the current suite metadata. A no-binding legacy comparison is readable only where each suite ID's environment/dependencies/config tuple and all four provenance digests exactly equal the retained baseline; any tuple drift is `COMPARISON_BINDING`.

### Output ownership and bootstrap trust boundary

Output publication is create-only and atomic: every existing destination is refused, including private regular files. Distinct attempts select distinct outputs. A write/path failure still prints a failure JSON on stdout with no authority states; it never prints a successful result before persistence.

Only the output parent is canonicalized for platform aliases. The selected final component remains literal, so an existing or dangling symlink is refused in place and cannot redirect publication. Successful installation results expose closed `invocation` and `fixture` records in the published result schema, including actual argv, exit status, UTC interval, output digests, root, and typed no-follow inventory.

The output path is walked from a held root directory descriptor. Its parent must be owned by the effective user with mode 0700; the target is published from a same-directory 0600 temporary file with a complete-write loop, file fsync, no-clobber link, byte comparison, and directory fsync. Repository and registered-workspace destinations (including aliases) are refused before creation. Missing nested parents, symlink swaps, existing targets, short writes, and filesystem errors yield one refusal JSON on stdout and leave no published partial result.

This standalone gate is invoked by the trusted supervisor, whose environment chooses the authority file. Manifest/artifact producers cannot choose that environment. A caller replacing the supervisor or binary can fabricate JSON and is outside this cooperative boundary; the result itself never launches workers or mutates control stores. M4 must prevent workers selecting/writing supervisor inputs before product admission. Artifact-only review refers to supplied fresh review context, not a hostile-program OS read boundary; trusted host adapters disable native tools and omit producer history. M5 owns full host confinement.

Read-only Git inventory disables fsmonitor/hooks, external filters/diff/textconv, optional locks, prompts and pagers, clears inherited Git config/trace/redirection variables, and refuses unknown execution-widening local includes/config before working-tree inspection. Git documents fsmonitor as an executable hook and status as optionally index-writing: [config](https://git-scm.com/docs/git-config), [status](https://git-scm.com/docs/git-status). A real fixture canary proves the hook did not run.

Private installation also excludes host process/IPC/socket control. Linux uses private user/PID/IPC/network namespaces, dropped capabilities, fresh proc/dev and a read-only runtime allowlist; host /run, temporary/home sockets and host proc are not mounted. Stage selected source inside the fixture. macOS denies external signals, networking/Unix sockets and Mach IPC. Check descendant quiescence after every exit, including successful exit; any surviving owned process invalidates the observation until bounded cleanup, never a PASS from unstable files.

## Phase-01 executed-diff constraints: installation ownership

An existing same-owner temp directory is not proof of ownership. Before granting any installer write boundary, the supervisor-selected `FFS_VERIFICATION_AUTHORITY` must contain an exact `installations` record binding `fixture_root` (canonical), the complete `fixture` path object, `device`, `inode`, `setup_argv`, `stub_artifact`, optional `nested_stub_artifact`, and `initial_entries`. Each initial entry has relative `path`, `type` (directory or file), and a file `sha256`; unknown, changed or missing inputs reject launch. This extends the existing authority schema, not manifest self-authorization. Protected-root and child containment checks remain mandatory even with a grant. Preexisting entries must be preserved, and a different attempt cannot be claimed based on temporary location or uid alone.

The installer must prevent session/process-group escape as well as stop ordinary descendants. On macOS the independently probed Seatbelt syscall deny set includes `SYS_setsid`, `SYS_setpgid`, and `SYS_posix_spawn`; ordinary fork/exec remains possible. Runtime preparation must use a verified native Python executable rather than launcher wrappers that use posix_spawn. Linux uses the private PID namespace. Each platform must actually exercise the boundary before setup; inability to establish it yields UNMET before launch. A successful or failed setup does not authorize stable-inventory claims while children remain.


### Verified core policy refinements

Fresh unpinned review completion records executed origin but asserted reviewer identity and authenticated=false. Admission requires a native executable pin; shebang executables and directory arguments are refused until their interpreter/tree execution closure can be pinned.

Evidence artifacts remain bounded at2MiB each and16MiB aggregate. Native executable pins use a separate64MiB per-file/aggregate budget. Read-only repository inventory permits empty regular files and uses64MiB per-file/512MiB aggregate limits; these do not relax the evidence JSON parser. Coverage completeness requires the repository-observed first-party Python corpus, excluding only tests/vendor/generated/cache locations; matching a producer-supplied subset is insufficient. CI evidence must match the repository-observed origin/main identity.

Private installation executes the selected unchanged staged setup.sh through the trusted interpreter wrapper. The staged source is read-only under both OS policies. The same bounded runner handles review and installation deadlines/output/process-group cleanup, with an explicit supervisor-built installation environment. A successful parent exit with a remaining owned process group is refused and cleaned up.

The implemented Phase 01 command, flag, gate, exit, and refusal registry is [documented in `docs/verification-cli.md`](../../../docs/verification-cli.md).
