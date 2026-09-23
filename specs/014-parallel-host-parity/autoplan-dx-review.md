# /autoplan Developer Experience Review

Date: 2026-09-12  
Product surface: CLI orchestration and operator runbooks; no UI design phase  
Voices: Claude Opus 5 high, read-only artifact review; Codex GPT-5.6 Sol high, read-only artifact review

## Personas

| Persona | Goal | Required first surface |
| --- | --- | --- |
| Primary local operator/supervisor | install, configure, run, resume, diagnose, migrate and locally activate safely | operator quickstart plus read-only doctor/status |
| Contributor/test/review worker | own one phase boundary, run its exact gate, inspect evidence | phase task, command registry, shard/status output |
| Migration/OpenClaw administrator | preview handoff, wait safely, activate locally and roll back | explicit nonmutating preview and supervisor-owned admin verbs |
| Accidental wrong-checkout/inherited-env user | get a safe, actionable refusal | repository/root diagnosis and typed recovery |
| Unauthenticated/partially provisioned operator | understand UNMET without exposing credentials | doctor plus problem/cause/fix/docs error |
| Returning future maintainer | reconstruct current state after a gap | gate-vector status and evidence index |

## Nine-stage journey

| Stage | Current plan | Accepted target |
| --- | --- | --- |
| Discover | architecture and modes are spread across plan/contracts | one doc map, mode/command registry and help contract |
| Evaluate | scope, safety, DAG and gates are clear | add wall-clock/resource expectations and local-only boundary |
| Install | test dependencies and mandatory `rtk` prefix are assumed | prerequisites section and executable read-only preflight |
| Configure | absolute state/evidence/consumer roots lack one validation flow | documented defaults/precedence plus root health/shape doctor |
| Hello World | prospective fixture exercise; no clean installed-user path | disposable-repository first green JSON in target ≤5 minutes |
| Real Usage | context/start/resume/revise and internal workspace preparation are strong | task-oriented recipes and stable text/JSON behavior |
| Debug/Recover | typed codes and fail-closed semantics are strong | every error includes problem, cause, fix, docs and recovery action |
| Upgrade/Migrate | invariants and rollback outcomes are strong | explicit preview/status/handoff/resume/abort/rollback verbs |
| Scale/Parallel | sharding/deadlines/25/600 constraints are strong | progress, partial evidence, ETA/deadline and gate status without weakening failure |

## Time to Hello World

- Current installed-user TTHW: **UNMEASURABLE**. The relevant commands are prospective and the current quickstart is an implementation handoff rather than a clean install walkthrough.
- Target: **≤5 minutes** from a supported clean environment to one green fixture-backed JSON result, measured independently on macOS and Ubuntu. Warm-auth and auth-unavailable paths are reported separately.
- Full-gate time remains distinct: engineering estimates 75–150 minutes in parallel and 3–5 hours serial. Six required host soaks alone contain 60 pairing-minutes.

## Eight passes

1. **Onboarding:** add prerequisites, define `rtk`, name supported environments, supply a disposable hello-world and glossary.
2. **CLI consistency:** publish one command/flag/exit registry for façade, verification and administrative verbs; preserve JSON stdout and stderr diagnostics.
3. **Errors:** stable typed subcauses; problem/cause/fix/docs/recovery in every refusal; UNMET is distinct from crash and FAIL.
4. **Documentation:** reconcile historical versus current Bats/coverage figures; label the existing quickstart as implementation handoff and add an operator guide.
5. **Upgrade/migration:** keep strong one-writer invariants and specify the actual supervisor-owned mutation surface.
6. **Environment:** one read-only doctor covers repository identity, roots, permissions, capacity, filesystem/schema, binaries and auth availability without asking for secrets.
7. **Support:** redacted diagnostic bundle and consumer fork/drift escalation path; evidence schema version policy.
8. **Measurement:** gate-vector status, progress/partial evidence for long runs, and measured first-run/recovery timings.

## Scorecard

| Dimension | Claude | Codex | Adjudicated plan target |
| --- | ---: | ---: | ---: |
| Discoverability | 3 | 3 | 8 |
| Onboarding/install | 2 | 3 | 8 |
| Hello World | 2 | 3 | 8 |
| CLI/API consistency | 6 | 6 | 8 |
| Errors/recovery | 7 | 5 | 9 |
| Documentation | 4 | 6 | 8 |
| Environment/config | 4 | 4 | 8 |
| Upgrade/migration | 6 | 7 | 9 |
| Observability/parallel ops | 4 | 9 | 8 |
| Support/measurement | 3 | 6 | 7 |
| **Overall before edits** | **4.1** | **5.2** | **8.1 planned** |

## Error contract

Every public error includes stable `code`, `problem`, `cause`, `fix`, `docs`, and typed `recovery_action`. Verification additionally includes a stable `unmet_code`; legitimate UNMET and process/runner crash use distinct status/exit classes. Required examples cover malformed input, ambiguous selection, resume required, LIVE/UNKNOWN owner, revoked fence, runtime drift, workspace preparation child causes, lock timeout, ENOSPC/permission/corruption/schema, missing auth, host timeout, exact request unavailable, incomplete evidence, installer collision, migration refusal/quarantine, OpenClaw drift and missing platform.

The error copy names safe observed values and never credentials/nonces. Coverage failure prints line numerator/denominator, required floor and lowest-covered owned modules. Runtime drift identifies the changed tuple component. `OWNER_UNKNOWN` never suggests force takeover.

## Ranked friction and consensus

| Severity | Friction | Voice consensus | Plan disposition |
| --- | --- | --- | --- |
| Critical | Actual M8 handoff/recovery/rollback/OpenClaw activation mutation surface is unspecified | Codex critical; Claude high/medium migration gap | Accepted: define supervisor-owned admin verbs and dry-run/status/rollback evidence |
| High | No clean prerequisite-to-Hello-World path; `rtk` is undefined in product docs | Both | Accepted as M8c docs/measurement contract, drafted before M0 usage |
| High | Error envelopes lack uniform problem/cause/fix/docs and typed UNMET subcause | Both | Accepted in M-1/M3 schema ownership |
| High | Verification exit taxonomy and full CLI/help compatibility registry are absent | Both | Accepted before M-1 closes |
| High | State/evidence root configuration lacks one doctor/preflight journey | Both | Accepted in M3 and operator docs |
| High | Quickstart carries stale/conflicting baseline counts | Claude explicit; Codex notes historical mix | Accepted: single-source from upgrade ledger and mark superseded data |
| High | Locked 25/600/80 floors must be aggregator invariants, not trusted invocation flags | Claude | Accepted: aggregate independently rejects 24/599/0.799 evidence |
| Medium | No status command prints the four-state per-PATH gate vector | Both | Accepted read-only `status/gates` surface |
| Medium | Long runs lack progress, partial evidence and `--shard` examples | Both | Accepted without retrying failures into PASS |

## Documentation and command changes

- Label `quickstart.md` as an implementation handoff and add an operator quickstart with supported platforms, prerequisites, `rtk` definition, environment validation, disposable hello-world, expected JSON, status/resume/abort/evidence/cleanup and local-only constraint.
- Reconcile Bats and coverage figures to the current ledger; label invalid/superseded percentages and partial runs.
- Add one authoritative command registry with enums, output defaults, exit taxonomies, wrapper translations, public admin verbs and help snapshot tests.
- Add stable troubleshooting anchors and require every error code to resolve to one.
- Define read-only `doctor`, `context/status`, `gates`, `migrate plan/status`, rollout dry-run/status, redacted diagnostic-bundle and supervisor-owned handoff/resume/abort/rollback/local-activate commands.
- The `aggregate` command independently enforces all locked floors and complete provenance, regardless of the flags used to create inputs.

## DX tasks

1. **DX-001 P0 — CLI/contracts owner:** publish the command/flag/exit registry, wrapper translations, output rules and help snapshots before M-1 exit.
2. **DX-002 P0 — Verification owner:** pin 25 repetitions, 600-second per pairing/platform overlap and 0.80 XML line rate inside aggregate; distinguish UNMET from crash.
3. **DX-003 P0 — Context/evidence owner:** require problem/cause/fix/docs/recovery and stable `unmet_code`; test every docs anchor.
4. **DX-004 P0 — State authority owner:** implement one read-only doctor for repository/state/evidence roots, environment, binaries and auth availability.
5. **DX-005 P0 — Migration owner:** specify and test preview/status/handoff/resume/abort/rollback public admin commands using the same M8a protocol.
6. **DX-006 P0 — OpenClaw owner:** specify dry-run/local-activate/status/rollback commands; stale evidence blocks and no publication/sync occurs.
7. **DX-007 P0 — Documentation owner:** reconcile baselines, define prerequisites/`rtk`, add operator hello-world and keep implementation handoff distinct.
8. **DX-008 P1 — Verification UX owner:** expose read-only per-PATH gate vector, progress, shard examples, deadline and partial-evidence location.
9. **DX-009 P1 — Independent acceptance owner:** E2E help/text/JSON/error/wrapper/admin/docs-link compatibility tests.
10. **DX-010 P1 — DX/QA owner:** record median/worst clean TTHW and recovery timing on both platforms without telemetry.
11. **DX-011 P2 — Support/docs owner:** redacted diagnostic bundle, glossary/doc map, OpenClaw escalation path and schema-version policy.

## Independent voice verdicts and final disposition

- Claude Opus 5 high: **REQUEST CHANGES, 4.1/10**, zero critical and six high plan gaps.
- Codex GPT-5.6 Sol high: **NEEDS REVISION, 5.2/10**, one critical and five high plan gaps.

All critical/high findings are accepted into the plan as explicit phase requirements, public surfaces, acceptance evidence and owners. They are closed as plan defects. Runtime behavior remains UNMET until implementation evidence passes. Final DX disposition after plan edits: **APPROVE FOR IMPLEMENTATION**.
