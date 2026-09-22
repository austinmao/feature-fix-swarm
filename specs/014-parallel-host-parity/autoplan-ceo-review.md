# Autoplan Phase 1 — CEO Review

Date: 2026-09-12
Mode: SELECTIVE EXPANSION
Plan: `specs/014-parallel-host-parity/plan.md`
Restore point: `/Users/luminamao/.gstack/projects/feature-fix-swarm/014-parallel-host-parity-autoplan-restore-20260912-130604.md`
Voices: Claude Opus 5 (`--model opus --effort high`, safe/restricted/read-only) followed by Codex GPT-5.6 Sol (`model_reasoning_effort=high`, read-only). Both were fresh sessions and received artifact paths without producer reasoning history.

## Verdict

CONDITIONAL GO. The plan solves a real defect and retains all five requested slices, but its readiness model needs six repairs before decomposition: a stable post-upgrade reference manifest, per-path gate state, early read-only OpenClaw inventory, pre-activation runbooks, capability recertification rules, and safety-critical branch/state coverage. These repairs are accepted below. The user-mandated full-production-corpus 80% line threshold, 25 deterministic repetitions, ten-minute pair/platform soaks, M8 safe writer handoff, OpenClaw reconciliation/activation, and all 60 FR/AC pairs remain in scope.

CEO score after accepted repairs: 8.4/10. Before repairs: primary 6.8/10; Opus 7.0/10; Codex 5.8/10.

## 0A — Premise challenge

| Premise | Evidence examined | Decision |
| --- | --- | --- |
| Complete-run isolation must start before planning writes | `feature-spec` and runner ordering cited by M2 B1; current worktree creation follows earlier state/planning writes | ACCEPT. This is the correct root problem. |
| Upgrades and upgraded-baseline review precede concurrency implementation | Operator ordering plus GSD 1.13 and host-envelope dependencies | ACCEPT, with the existing cycle correction: review completion authorizes isolated repairs while affected launch paths stay blocked. |
| One supervisor-owned SQLite authority is needed | Existing RunStore/coord/gates fragmentation; atomic run/workspace/objective reservation; durable budgets/fences | ACCEPT. Add a vertical proof before expanding the full schema. |
| Worktrees are sufficient isolation | Shared Git objects/common-dir remain cooperative | QUALIFY. Preserve the cooperative boundary and test common-dir mutation denial independently. |
| One binary pass/fail state can represent M2 | Review-complete and repair-authorized are different from path-admitted and rollout-ready | REJECT. Add the four-state gate vector per affected path. |
| Existing coverage figures describe the production corpus | Earlier XML included tests and omitted unmeasured production modules; 4,765/8,351 line and 1,708/3,258 branch cover only 22 measured production modules | REJECT. No valid full-corpus baseline exists until collection is rerun with `source = .`, namespace-package and subprocess coverage, and no untested-production exclusion. |
| Final M7 evidence remains valid after M8 consumer adaptation | M8 may discover a canonical FFS change | QUALIFY. Inventory forks read-only before M6 and invalidate M6/M7 if any later canonical source changes. |
| Runtime hashes remain valid indefinitely | Claude, Codex, GSD, policy, hooks, OAuth, model catalogs change | REJECT. Add expiry/invalidation/recertification rules and old-bundle retention. |
| Full corpus line >=80 is too broad | Explicit operator requirement says actual full corpus strict | REJECT BOTH VOICES' SCOPE REDUCTION. Retain the gate and add safety-module branch/state proof rather than substitute a narrower metric. |

Premise confirmation is satisfied by the user's explicit autonomous scope and preservation requirements. No product premise is left open.

## 0B — What already exists

| Sub-problem | Existing code or evidence | Reuse decision |
| --- | --- | --- |
| Durable run storage | `lib/run_state/state.py` | Extend by migration; do not create a competing authority. |
| Short coordination transactions and liveness | `scripts/coord/coord.py` | Retain as façade; centralize identity behind a public module. |
| Grants/findings/evidence | `lib/gates.py` | Retain semantics and route authoritative mutations through supervisor ownership. |
| Worktree creation | `scripts/gsd/gsd-run.sh` | Move admission earlier; remove primary-planning synchronization. |
| Locking | `scripts/gsd/lib-lock.sh`, filelock-backed coordination | Keep filesystem locks for Git/install/credential boundaries; SQLite owns control-state atomicity. |
| Codex bundle/config/auth mechanisms | current Codex runtime builder and credential synchronizer | Extract shared primitives and implement a strict Claude peer. |
| Model request catalog | `templates/model-requests.json`, `lib/model_requests.py` | Make all shell/Python consumers delegate; preserve native tiers. |
| Upgrade evidence | `docs/upgrades/*` | Freeze a content-addressed reference manifest before relying on comparisons. |
| Consumer fork list/drift helper | OpenClaw allowlist and `sync-drift-check.sh` | Inventory early; full ownership/hash manifest remains required for rollout. |

## 0C — Dream state and delta

```text
CURRENT
  early writes + fragmented identities + repo lifetime lease + host ambiguity
       |
       v
THIS PLAN AFTER CEO REPAIRS
  content-addressed reviewed baseline
  -> isolated workspace before every write
  -> one fenced authority + per-path gate vector
  -> immutable recertifiable runtime tuples
  -> full-corpus and safety-state verification
  -> safe legacy handoff + manifest-complete consumer activation
       |
       v
12-MONTH IDEAL
  every objective is resumable, attributable, recoverable, host-honest,
  independently reviewable, and boring to operate across runtime churn
```

The plan reaches the durable control-plane foundation. It does not claim hostile-worker or distributed-machine isolation, automatic publication, or zero-downtime runtime mutation. Those exclusions keep the architecture honest.

## 0C-bis — Implementation alternatives

| Approach | Completeness | Human / CC effort | Risk | Reuse | Pros | Cons | Decision |
| --- | ---: | --- | --- | --- | --- | --- | --- | --- |
| A. Gate-vector staged program | 10/10 | XL / L | Medium | High | Keeps all slices; adds stable evidence/invalidation; permits safe fixture work after M2 | Requires explicit path-state and evidence lifecycle | ACCEPT |
| B. Current linear M0–M8 unchanged | 8/10 | XL / L | High | High | Simple phase narrative | Baseline floats; M8 can invalidate M7; docs and recertification are late | REJECT |
| C. Stateless workspace shim then authority | 5/10 now, 10/10 eventually | M then XL / S then L | Medium | Very high | Earlier B1 containment fallback | Risks temporary second authority and defers durable resume/budget requirements | HOLD as contingency only |

Recommendation: Approach A. It is the complete option and adds explicit gates without replacing the chosen architecture.

## 0D — Scope decisions

### Accepted within the five slices

1. Freeze one content-addressed post-upgrade reference manifest covering source, lockfile, installed bundles, config/policy, dependency identities, and exact suite environment. Any material change invalidates the affected review.
2. Record four separate states per affected path: `review_complete`, `repair_authorized`, `path_admitted`, and `rollout_ready`.
3. Make the first M3 delivery a vertical RunStore/supervisor proof covering atomic triple reservation, one lifecycle transition, one fenced child, restart recovery, and legacy read compatibility.
4. Run a read-only OpenClaw ownership/fork inventory before M6 source freeze. Any later canonical FFS edit invalidates and reruns affected M6/M7 evidence.
5. Publish minimum recovery, rollback, ownership-conflict, abort-condition, and evidence-location runbooks before M8a/M8b activation; retain final docs in M8c.
6. Add capability-result expiry/invalidation triggers for executable, bundle, config, policy, model catalog, host version, and platform changes; retain referenced old bundles.
7. Add ENOSPC, permission loss, corrupt DB, interrupted schema upgrade, backup/restore, and bounded evidence-retention cases for the external state root.
8. Keep full-corpus line >=80 and separately require every refusal/recovery/state-transition branch in safety-critical modules to have a test oracle.
9. Move the installed-upstream overlap/#4588 experiment into M1a as a design input while preserving PATH-020's final M7 proof.
10. Preserve the repository lifetime lease until workspace-ready admission is enforced for every entry family.
11. Define three non-publication checkpoints: fixture core after M5, frozen FFS release candidate after M7, controlled local FFS activation after M8a, then OpenClaw activation after M8b.

### Deferred

- Cosmetic docs and broad examples remain M8c; operator runbooks move earlier.
- Nonessential global-tool upgrades that are not dependencies of affected paths stay ledgered but do not falsely satisfy a host gate.
- Unsupported upstream concurrency paths remain blocked or sequential until measured; the independent FFS path is not blocked solely by issue status.

### Rejected

- Dropping or narrowing the strict full-corpus 80% line gate.
- Reducing the 25 race/fault repetitions or ten-minute soak matrix.
- Replacing M8 safe handoff with unreviewed dual writers or a drain-only semantic change.
- Splitting OpenClaw activation out of the requested fifth slice.
- Whole-orchestrator replacement or new runtime dependencies.
- Windows/network-filesystem/distributed-machine/hostile-worker scope.
- Any real-repository commit, push, release, deployment, session termination, or wait-for-session prerequisite.

## 0E — Temporal interrogation

| Time | Decision now fixed in plan |
| --- | --- |
| Human hour 1 / CC minutes 1–10 | Reference manifest identity, gate-vector schema, source freeze and invalidation rules. |
| Human hours 2–3 / CC minutes 10–25 | Vertical authority proof, resource-key canonicalization, lease-removal ordering. |
| Human hours 4–5 / CC minutes 25–45 | External state-root failures, child handshake crash points, host tuple recertification. |
| Human hour 6+ / CC minutes 45–90 | Early consumer inventory, safety branch oracles, pre-activation runbooks, final full matrix. |

## Review sections

### 1. Architecture

The selected architecture is sound after the gate-vector and invalidation additions. SQLite is justified by all-or-nothing reservation of run, workspace, and objective plus monotonic fencing/budget accounting. Git/install/credential/worktree-admin serialization stays in short filesystem locks; it must not migrate into long database transactions.

```text
entry skills / GSD activities
          |
          v
side-effect-free context resolver
          |
          v
supervisor control authority ----> immutable runtime/capability ledger
  |       |        |     |
  |       |        |     +--> grants, budgets, audit outbox
  |       |        +--------> fenced launch intent / child handshake
  |       +-----------------> workspace registry + selected inputs
  +-------------------------> run / activity / attempt state
          |
          v
ready worktree --> host adapter --> worker-owned progress/evidence
          |
          v
harvest -> explicit landing/publication boundary -> safe finalization
```

The legacy lifetime lease remains active until all entry families fail closed before workspace readiness. M8 retains one-writer handoff; no real old/new control authority overlap is accepted.

### 2. Error and rescue registry

| Codepath | Failure | Named outcome | Rescue action | Operator sees |
| --- | --- | --- | --- | --- |
| reference manifest load | missing/hash drift | `ReferenceManifestMismatch` | invalidate affected review/admission | changed identities and required rerun |
| state-root open | ENOSPC/permission/corruption | `StateRootUnavailable` / `StateStoreCorrupt` | fail before reservation; preserve backup/evidence | path, cause, recovery command |
| triple reservation | conflict/busy timeout | `OwnershipConflict` / `StoreBusy` | bounded refusal; list current owner without takeover | resource and safe next action |
| workspace preparation | collision/symlink/Git failure | `WorkspacePreparationFailed` | quarantine partial owned resources; keep primary unchanged | failed step and cleanup status |
| child handshake | pre/post-spawn crash | `LaunchReconcileRequired` | consult intent, identity and fence; no replacement on UNKNOWN | intent state and recovery route |
| capability evaluation | stale/missing/widening policy | `CapabilityUnproven` | block affected tuple/path | exact missing result and recertification trigger |
| coverage collection | tests included/production omitted/empty XML | `CoverageCorpusInvalid` | reject report; rerun full corpus | denominator defect, never PASS |
| M8 handoff | live/unknown legacy owner | `HandoffBlocked` | leave owner untouched and writer epoch unchanged | owner status without destructive action |
| OpenClaw reconcile | unowned drift/late canonical edit | `RolloutManifestConflict` | quarantine/refuse and invalidate affected M6/M7 | conflicting surface and required review |

No catch-all swallow is accepted. Every row requires structured context, a typed exit, retained evidence, and a recovery action.

### 3. Security and threat model

| Threat | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| forged/stale worker mutates control state | Medium | High | authenticated supervisor role plus nonce/generation on every mutation |
| symlink/path escape from workspace or stage | Medium | High | anchored realpath validation, no unsafe file types, hash-guarded ownership |
| ambient host config widens tools/network/hooks | High | High | explicit bundle/config/policy tuple, independent behavioral proof, fail unknown widening |
| credentials exposed or lost during sync | Low | High | regular private files, lock, atomic preservation, value redaction, loser-rotation retention test |
| process PID reuse steals ownership | Medium | High | boot identity + PID + start token; UNKNOWN blocks |
| review evidence substituted or empty | Medium | High | content-addressed producer-distinct provenance; empty/missing fails closed |
| migration mixes writers | Low | High | legacy ownership proof, explicit epoch, one writer, frozen rollback |

### 4. Data and interaction edge cases

```text
request -> validate context -> reserve triple -> prepare workspace -> ready
  |             |                  |                  |
 invalid      foreign/ambiguous   conflict/busy      collision/escape
  v             v                  v                  v
typed refusal  candidate list     one winner         quarantine + no primary write

ready -> debit+intent -> spawn blocked child -> ack/fence -> release -> evidence
                         |                |              |
                       crash          stale owner     export failure
                         v                v              v
                  reconcile only    deny effect     retry idempotently
```

Nil, empty, malformed, stale, duplicate, partial and upstream-error paths are explicitly mapped by UT-010–030 and INT-003–008. The accepted repair adds the state-root and reference-manifest shadows that were missing.

### 5. Code quality

The plan correctly centralizes context, identity, host capabilities, runtime bundle and rollout validation rather than extending fallback chains. The main quality risk is sequencing the five-family resolver refactor before the full verification net. The accepted posture keeps regression-before-fix and begins verification harness construction early; the refactor remains M6 and must run against the completed targeted harness before M7 source freeze.

### 6. Tests

The 58 unit cases, 16 integration boundaries and 24 CLI paths cover the requested behavior. Accepted additions: reference-manifest invalidation; gate-vector transitions; lease-removal refusal; state-root failures; capability expiry; early consumer inventory invalidation; exact safety refusal/recovery branch oracles. The full production corpus remains the line-coverage denominator; tests and unmeasured production modules cannot distort it.

### 7. Performance

The plan avoids long locks around model work and uses short database/Git/install/credential transactions. The main performance risks are SQLite busy behavior, evidence growth, worktree preparation latency and canary duration. Bound busy timeouts, measure p99 for reservation/preparation, retain evidence by state, and run the full soak only at final readiness or recertification triggers while preserving the mandated repetitions and durations.

### 8. Observability and debuggability

Every authoritative event needs stable IDs, run/activity/attempt/resource keys, expected and actual fence, runtime tuple, gate-vector transition, and error category. Add health output for state-root capacity/schema/corruption, last valid backup, capability expiry, stale canary identity and evidence-export backlog. Never log credentials or handshake secrets.

### 9. Deployment and rollout

```text
freeze reference -> M2 artifact review -> fixture repairs -> M6 audit/fix
       -> freeze candidate -> M7 full matrix -> pre-activation runbooks
       -> M8a safe writer handoff -> M8b manifest/canaries/activation -> M8c final evidence
                                  ^
                                  +-- canonical edit invalidates M6/M7
```

No phase label alone admits a path. Missing platform/auth/evidence remains UNMET. Existing sessions continue; no wait or termination is introduced.

### 10. Long-term trajectory

Reversibility is 4/5 until M8a, 2/5 during writer activation, then 4/5 after verified rollback. Six-month viability depends on recertification triggers, old-bundle retention, state-root maintenance and evidence growth policy; these are now explicit accepted tasks. The scope remains a cooperative, single-machine operator control plane.

### 11. Design and UX

Skipped after evidence check: the plan is a Python/Bash CLI and installed runtime plan with no screen, component, layout, form, dialog, navigation or rendered-state scope. CLI developer experience is reviewed in Phase 3.5.

## Dual voices

### Claude Opus 5

Verdict: conditional go, 7.0/10. It produced 13 findings. Accepted signals: stable reference baseline, upstream probe earlier, explicit lease-removal ordering, recertification cadence, actual SQLite justification, early verification harness, and richer failure handling. Rejected recommendations: drain-only migration, reduced corpus coverage, reduced soak burden, and removing OpenClaw activation from the fifth slice.

### Codex GPT-5.6 Sol high

Verdict: conditional replan, 5.8/10. It produced 12 findings. Accepted signals: early read-only consumer inventory, source-evidence invalidation, four-state gate vector, M1 current-state stabilization, operator runbooks before activation, usable non-publication checkpoints, vertical authority proof, capability lifecycle, risk-tiered feedback, safety branch coverage and state-root failure handling. Its claims are inputs to the consensus, not independently confirmed implementation facts.

### CEO dual voices — consensus table

| Dimension | Claude | Codex | Consensus |
| --- | --- | --- | --- |
| Premises valid | mixed | mixed | CONFIRMED: core B1 premise; baseline and lifecycle premises need repair |
| Right problem | yes | yes | CONFIRMED |
| Scope calibrated | over-serialized | over-coupled | CONFIRMED: add incremental gates; no requested slice reduction |
| Alternatives explored | migration/upstream alternatives missing | staged gate-vector/vertical proof missing | CONFIRMED: Approach A selected; DISAGREE on migration semantics |
| Dependency risk covered | upstream too late | late consumer adaptation invalidates proof | CONFIRMED: move read-only evidence earlier |
| Six-month trajectory | cadence missing | recertification/storage lifecycle missing | CONFIRMED |

Consensus: 5/6 confirmed, 1 substantive disagreement. The migration disagreement is resolved in favor of the existing safe handoff because the user explicitly retained all five slices and the alternative would materially rewrite FR-048–050.

## Failure modes registry

| Codepath | Failure mode | Rescued | Test | Operator sees | Logged | Critical gap after plan repair |
| --- | --- | --- | --- | --- | --- | --- |
| reference manifest | stale/mismatched input | yes | required | invalidated review/path | yes | no |
| state root | ENOSPC/permission/corruption | yes | added | blocked before ownership | yes | no |
| admission | partial triple reservation | yes | UT-019/INT-005 | one winner/refusal | yes | no |
| workspace | early write or collision | yes | UT-016–018/INT-003 | blocked/quarantined | yes | no |
| handshake | crash or stale fence | yes | UT-027–029/INT-006 | reconcile-required | yes | no |
| capability | stale/ambient widening | yes | UT-035–039/INT-010 | path not admitted | yes | no |
| coverage | invalid corpus | yes | UT-044/INT-012 | invalid report | yes | no |
| migration | live/unknown owner | yes | UT-048–051/INT-015 | handoff blocked | yes | no |
| rollout | consumer drift/canonical edit | yes | UT-052–055/INT-016 | reconciliation blocked; evidence invalidated | yes | no |

## NOT in scope

- Hostile-worker or security-grade Git object isolation.
- Network filesystems, distributed ownership, multi-machine locks, Windows, or GUI work.
- Whole-orchestrator replacement or a new runtime dependency.
- Automatic commits, pushes, PRs, releases, deployment or publication.
- Session termination or waiting for existing sessions as an upgrade condition.
- Coverage or soak reductions proposed by a voice.

## Implementation tasks

- [ ] CEO-T1 (P1, human ~2h / CC ~15m): add content-addressed reference manifest and invalidation rules.
- [ ] CEO-T2 (P1, human ~2h / CC ~15m): add four-state per-path gate vector.
- [ ] CEO-T3 (P1, human ~3h / CC ~20m): add vertical M3 authority proof and lease-removal ordering test.
- [ ] CEO-T4 (P1, human ~2h / CC ~15m): inventory OpenClaw forks read-only before M6 and enforce M6/M7 invalidation.
- [ ] CEO-T5 (P1, human ~2h / CC ~15m): add pre-activation operator runbooks.
- [ ] CEO-T6 (P1, human ~3h / CC ~20m): add capability expiry/recertification and old-bundle retention.
- [ ] CEO-T7 (P1, human ~3h / CC ~20m): add state-root failure/recovery/retention cases.
- [ ] CEO-T8 (P1, human ~2h / CC ~15m): define valid full-corpus coverage collection plus safety branch/state oracles.
- [ ] CEO-T9 (P2, human ~1h / CC ~10m): move the upstream overlap probe to M1a while retaining PATH-020 at M7.

## Completion summary

| Item | Result |
| --- | --- |
| Mode | SELECTIVE EXPANSION |
| Premises | 9 evaluated; no unresolved product premise |
| Architecture | 4 findings folded |
| Error paths | 9 mapped, 0 unresolved critical gaps |
| Security | 7 threats mapped |
| Data/interaction | nil/empty/stale/duplicate/partial/error paths retained |
| Code quality | resolver sequencing constrained |
| Tests | 58 UT + 16 INT + 24 PATH retained; 7 additions folded |
| Performance | full repetitions/soaks retained; earlier feedback risk-tiered |
| Observability | gate/capability/state health added |
| Deployment | candidate freeze and invalidation added |
| Future | recertification/storage lifecycle added |
| Design | skipped, no UI scope |
| Voices | Opus + Sol high, fresh read-only artifacts |
| Consensus | 5/6 confirmed; migration disagreement adjudicated |
| Critical/high open in reviewed plan | 0 after accepted plan repairs |
| User challenges | 0; voice scope reductions rejected under explicit user constraints |

