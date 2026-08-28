<!-- socratic.md — spec 012 self-interrogation ledger. Enums (closed sets from scripts/gsd/socratic-slice.sh): domains ∈ {requirements, frontend, backend, data, api, security, infra, testing, observability, ai-llm, mobile, product-ux, cost-performance, compliance, team-maintenance}; depth ∈ {core, full}; packs (≤2) ∈ {software-design, domain-modeling, data-systems, operations, threat-modeling, ai-engineering, agent-design, legacy-change, testing-design, product-discovery}. -->
<!-- depth core: a local CLI guard in a repo-owned script — no production system, external users, PII, money, or irreversible action; the only escalation-adjacent trait is that the file is forked downstream, covered by the legacy-change pack. -->
---
domains: [requirements, testing, infra, team-maintenance]
depth: core
packs: [legacy-change, testing-design]
---

## Self-answered highlights

- Restated (req Q1): success = an operator can no longer get `[sync-drift-check] OK`
  from comparing a directory to itself; measurable as AC-001's exit 2 +
  zero `IN-SYNC:` lines on the self-compare fixture. Observed twice in the
  field (2026-08-21 memory note, 2026-08-27 openclaw sync), so this is a real
  false-green, not a hypothetical.
- Maintained/production-critical (req Q2): it gates every consumer re-sync
  and openclaw wires it into the sync procedure — a wrong answer here silently
  destroys a consumer fork on the next blind sync. Confirmed the change is a
  refusal, never a new verdict, so no downstream parser learns a new line.
- Constraints that cannot change (req Q5): the per-file verdict vocabulary
  (`IN-SYNC`/`DRIFT`/`FORKED`/`MISSING`/`STALE-ALLOWLIST`), exit 1 on drift,
  exit 2 on usage error, and the `--allowlist` seam. Confirmed by AC-004 and
  AC-006; the new error reuses exit 2 (usage class) rather than minting a
  new code, so consumer CI that treats 2 as "bad invocation" is already right.
- Highest-risk assumption + cheapest test (req Q6): that realpath equality is
  the right identity — not inode, not string. Tested first by the symlink
  case (AC-003) and the trailing-slash/`./` case (EDGE-002) in the same bats
  file, before the behaviour cases.
- Pinned current behaviour before changing it (legacy-change): the existing
  7 bats cases ARE the pin; they run with explicit distinct `GSD_SYNC_SRC` and
  must stay byte-identical (AC-004). Changed the spec to say so explicitly
  rather than "add tests after".
- One change per commit (legacy-change): guard + its tests in one commit
  (behaviour change), CHANGELOG in the same PR; no reformatting of the file,
  no touching the openclaw-forked allowlist block (AC-006 made explicit).
- Most expensive failure to ship (testing Q1): a false NEGATIVE — refusing a
  legitimate distinct compare — because it would block every consumer sync.
  Covered by keeping realpath EQUALITY as the only refusal predicate and by
  the unchanged regression suite; the missing-dir error is ordered first so a
  bad path never reaches the guard (AC-005).
- Deterministic tests (testing Q4): fixtures under `$BATS_TEST_TMPDIR`; the
  macOS `/private/var` canonicalisation is symmetric under `pwd -P`
  (EDGE-004), so no host-specific branch.
- Where it runs (infra Q1): in FFS CI (`bats` job) and in consumer CI after a
  vendor sync; nothing deploys. Rollback is `git revert` (plan.md).

## Assumed (flag if wrong)

- ASSUME-001: exit code 2 (existing usage-error class) is the right code for
  the self-compare refusal; no consumer distinguishes "bad dir" from
  "same dir" and none should need to.
- ASSUME-002: a `GSD_SYNC_SRC` that does not exist keeps today's behaviour
  (empty glob → OK). Fixing that is out of scope; the guard only must not
  crash on it (EDGE-001).
- ASSUME-003: the openclaw fork's additions all sit at or below the
  `allow_reason()` definition, so a change confined above it re-merges
  without conflict. (Verified 2026-08-28 against openclaw main `cff4c4b`:
  the fork's delta is the allowlist auto-discovery + symlink refusal block,
  which sits below the resolution block.)
- ASSUME-004: no consumer invokes the script through a symlinked SCRIPTS dir
  whose realpath differs from the consumer dir it checks — i.e. realpath
  equality is neither too strict nor too loose for real layouts.

## Open questions → grants

- none — no operator-gated action beyond the ordinary PR merge, which the
  grant ledger enumerates from plan.md.

## Top risks

- R1 — false negative on an exotic consumer layout (ASSUME-004). Detection:
  the openclaw re-sync after this lands runs the guard on both consumer
  trees; a refusal there is the signal.
- R2 — the fork-merge assumption (ASSUME-003) is wrong and the next openclaw
  sync conflicts. Bounded: the conflict is in a 12-line block with an
  obvious resolution; no verdict logic involved.
- R3 — the guard is added below the missing-dir check but a future edit
  reorders them, making a nonexistent dir crash in `cd`. Pinned by the
  AC-005 test.
