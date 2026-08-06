<!-- /autoplan restore point: /Users/luminamao/.gstack/projects/austinmao-feature-fix-swarm/main-autoplan-restore-20260805-025400.md -->
# Plan 005 — Vendor socratic + 3-point integration

## Prior-art decision

**ADOPT (vendor, pinned)** per `specs/005-socratic-integration/prior-art.md`:
no ≥200★ alternative exists in the niche; socratic (89★, MIT, active, solo
maintainer) uniquely fits; content directly reviewed in-session. Bus-factor-1
risk mitigated by commit pin + MIT fork-and-carry. Build-fresh rejected: the
question bank + packs are exactly the deliverable, already written and
maintained upstream; the FFS work is wiring, not content.

## Architecture

One deterministic helper, three callers — the arming logic exists exactly once:

```
vendor/socratic/                      (pin.json [+ optional patch]; tree NOT committed —
                                       installed by scripts/install-socratic.sh, like prompt-master)
        │
        ▼ installed to .agents/skills/socratic (canonical) by lib/ffs_installer.py
        │
specs/NNN/socratic.md                 (LLM-authored at spec time; machine-readable frontmatter:
        │                              domains/depth/packs + ASSUME-NNN ledger)
        ▼
scripts/gsd/socratic-slice.sh <spec_dir> [--mode plan|verify]
        │   parses frontmatter (embedded python3, mirroring the
        │   requirement-ownership-gate.sh:104-145 approach but FAIL-SOFT:
        │   malformed input → empty stdout + stderr warn, exit 0)
        │   → cats matching question files (+ ≤2 pack cores;
        │   --mode verify extracts each FULL domain file's ## Verification block)
        │   → emits SOCRATIC_DATA_START … SOCRATIC_DATA_END (incl. ASSUME ledger)
        │   → absent socratic.md OR vendor tree OR SOCRATIC=off → exit 0, EMPTY stdout
        │   → ALWAYS one stderr status line: `socratic: armed domains=… packs=…`
        │     or `socratic: skipped (<reason>)` — silent degradation is not allowed
        ▼
  caller 1: scripts/gsd/plan-wall.sh    — plan-wall has NO spec context today; add the
            same branch-NNN→specs/NNN-* resolution review-gate already uses
            (skills/review-gate/SKILL.md:429-430), pass the slice as a NEW 2nd arg to
            _pw_build_prompt (:282, called at :582); non-NNN branch/detached HEAD →
            empty slice, fail-soft. CONDITIONAL append — empty slice must leave the
            prompt BYTE-IDENTICAL (no stray newline). Idempotence: fold socratic.md's
            sha256 into the PW_PLAN_SHA cache key (:449,:527) when the slice is
            non-empty, so editing socratic.md invalidates the zero-dispatch fast path.
  caller 2: skills/plan-decompose Step 3 — spec dir already in scope ($SPEC_DIR);
            append slice to opposite-host review prompt (plan mode), same
            conditional-append rule.
  caller 3: skills/review-gate honest-verifier — HV_SPEC already resolves
            (SKILL.md:429-430); inject `--mode verify` slice as a third delimited
            block between spec resolution (:430) and HV_PROMPT construction
            (:442-461) + per-ASSUME held/violated/unverifiable verdicts.
```

Producer side: `skills/feature-spec/SKILL.md` gains a pre-Phase-1 "Step 0 —
socratic self-interrogation" (LLM work, core depth default, full on
auth/payments/PII/production signals) writing `specs/NNN/socratic.md`.
`skills/feature-implement/SKILL.md` needs NO producer change — it consumes via
plan-wall (already in its Step 4) and review-gate (already in its Step 6 tail).

### socratic.md contract (machine-readable head, prose body)

```markdown
---
domains: [requirements, testing, security]
depth: core
packs: [operations]
---
## Self-answered highlights
## Assumed (flag if wrong)
- ASSUME-001: <default taken> — <why defensible>
## Open questions → grants
- (autonomous: recorded as ASSUME or typed grant action — never an operator stop)
## Top risks
```

### Autonomy invariant (AC-004, operator-mandated)

The socratic step self-answers everything. No AskUserQuestion, no interactive
interview mode, no new stop points. Authority decisions → ASSUME entries and/or
typed grant actions consumed by the EXISTING Step-6 MAX-AUTH/--gated machinery.
Surfaced post-hoc in the completion summary.

### Prompt-injection posture

Vendored question files are reviewed-at-pin data, but the slice still enters
reviewer prompts as DELIMITED UNTRUSTED DATA (`SOCRATIC_DATA_START/END`),
matching the existing `PLAN_DATA_END` / `DIFF_DATA_START` convention. The
helper never executes vendored content.

## Implementation phases

### Phase 1 — Vendoring (US-1: AC-001, AC-002)
1. `vendor/socratic/pin.json` — repository, commit `862b52e8…f2a28a`, no patch
   initially (`patch` key omitted).
2. `scripts/install-socratic.sh` — start from install-prompt-master.sh shape
   (--dest guardrails, clone at pin, `.ffs-socratic.json` marker with schema
   `ffs.external-skill/v1`, strip `.git`, refuse existing dest) BUT the
   optional-patch handling is genuinely NEW logic, not mirrored:
   install-prompt-master.sh hardcodes its patch path (:9) and never reads
   pin.json's `patch` key (`read_pin` :30-35 fetches repository/commit only —
   the declared `"patch"` key is dead there). install-socratic.sh must read the
   optional key, conditionally apply, and write `patch_sha256: null` when
   unpatched. (Follow-up, out of scope: the dead `patch` key in
   prompt-master's pin.json/installer pair.)
   Wire the new script into the existing installer syntax checks:
   `.github/workflows/ci.yml:102,106` (`bash -n`/`zsh -n` lines) and
   `CONTRIBUTING.md:69`.
3. `lib/ffs_installer.py` — `stage_socratic()` mirroring
   `stage_prompt_master()` (:1506), then thread it through the ~8 install()
   call sites where prompt-master staging flows (:1660, :1673-1684, :1731-1751,
   :1757-1759, :1818-1819) for BOTH project and global scope. Do NOT touch
   `legacy_skill_names()` (:1262) — that is the legacy-migration allowlist and
   socratic has no legacy copies. Uninstall/doctor are manifest-driven
   generically (:1833-1881) and come for free once install() populates
   `planned`. Because ffs_installer stages it on the default setup.sh path,
   socratic is DEFAULT-ON for installed consumers; fail-soft covers only
   checkouts that haven't rerun setup.

### Phase 2 — Slice helper + producer (US-2: AC-003, AC-004, AC-009)
4. `scripts/gsd/socratic-slice.sh` — as architected above. Domain-name → file
   mapping table inline (requirements→00 … team-maintenance→14). Unknown domain
   names skipped with stderr warn; `SOCRATIC=off` env kill switch (same pattern
   as `PLAN_WALL=off`, plan-wall.sh:21) maps to the empty-stdout fail-soft path;
   one mandatory stderr status line per invocation (armed/skipped + reason).
5. `skills/feature-spec/SKILL.md` — Step 0 socratic self-interrogation
   (fail-soft: vendor tree absent → skip silently). AUTHORING is fail-closed
   where CONSUMPTION is fail-soft: after writing socratic.md, validate
   `domains`/`depth`/`packs` against the known enum and fix unknown values
   before proceeding (a typo like `scurity` must not silently drop a domain
   from all downstream arming). Generated socratic.md carries a header comment
   listing the valid domain enum + depth values (consumer-facing contract).
   Verify-read mirrors the existing per-step verify pattern; feed
   highlights/assumptions into specify/plan context; open questions → Step-6
   grant enumeration; skipped/unknown domains surfaced in the completion
   summary next to the ASSUME ledger.

### Phase 3 — Arming the three consumers (US-3/US-4: AC-005, AC-006, AC-007, AC-008)
6. `scripts/gsd/plan-wall.sh` — hoist the existing branch-NNN extraction
   (:76-79) above the `RUN_ID` conditional so it runs unconditionally under
   the feature-implement caller, which always exports `GSD_RUN_ID`; add a
   memoized `specs/NNN-*` resolution reusing it (sorted `find | head -1`,
   lexically-first adjacency); pass the slice as a new 2nd argument to
   `_pw_build_prompt` (:282); CONDITIONAL append (empty slice → byte-identical
   prompt, the unchanged printf statement runs). Fold socratic.md's sha256
   into the LOCAL `sha` variable in `_pw_dispatch_path`, immediately after the
   sha compute and before both the `PW_PLAN_SHA` assignment (:538) and the
   idempotence comparison (:551-554) — one variable feeds both sites, so
   folding only at the `PW_PLAN_SHA` assignment (as this item originally
   implied) would store a folded key and compare against an unfolded one,
   permanently losing the zero-dispatch fast path rather than targeting the
   invalidation AC-005 asks for. The `PLAN_WALL=off` waiver path (:443-499)
   is out of scope: it never calls `_pw_build_prompt`, so it is never armed
   and its stored key is never compared against anything. Non-NNN branch /
   detached HEAD → empty slice, fail-soft (same fragility review-gate already
   accepts).
7. `skills/plan-decompose/SKILL.md` Step 3 — same conditional append to the
   adversary prompt via the helper (`$SPEC_DIR` already in scope).
8. `skills/review-gate/SKILL.md` — honest-verifier: inject `--mode verify`
   slice as a third delimited block between HV_SPEC resolution (:430) and
   HV_PROMPT construction (:442-461); instruct per-ASSUME verdict lines
   (held/violated/unverifiable); violated → normal findings-queue entry with
   severity.

### Final — Docs + evidence (AC-010, AC-011)
9. `docs/dependencies.md` socratic section incl. a concrete pin-bump runbook
   (bump commit in pin.json → re-run installer → re-review changed question
   files → fingerprint diff via stage_socratic); CHANGELOG entry.
10. A/B evidence pass (advisory, AC-011): run the plan-wall adversary on one
    historical phase plan with and without the slice; record the findings diff
    in `evals/socratic-ab.md`. Not a ship gate — evidence for the retro.

## Unit Test List

Sequenced design-critical first:

- [ ] install-socratic: installs pinned commit into empty dest, writes `.ffs-socratic.json` with commit + null patch_sha256
- [ ] install-socratic: refuses existing destination, dest unchanged (exit 1)
- [ ] install-socratic: refuses unsafe destinations `/`, `$HOME`, `.` (exit 2)
- [ ] install-socratic: applies patch + records its sha when pin.json names one
- [ ] ffs_installer.stage_socratic: stages to canonical `.agents/skills/socratic` with fingerprint
- [ ] ffs_installer: uninstall removes managed socratic copy, preserves edited copy (existing convention)
- [ ] socratic-slice: emits only frontmatter-declared domains' core files
- [ ] socratic-slice: depth=full switches to full question files
- [ ] socratic-slice: --mode verify emits `## Verification` blocks from full files
- [ ] socratic-slice: includes ASSUME ledger lines inside delimiters
- [ ] socratic-slice: caps packs at 2, warns on stderr for the rest
- [ ] socratic-slice: unknown domain name skipped with warn, exit 0
- [ ] socratic-slice: missing socratic.md → empty stdout, exit 0
- [ ] socratic-slice: missing vendor tree → empty stdout, exit 0
- [ ] socratic-slice: output wrapped in SOCRATIC_DATA_START/END exactly once
- [ ] socratic-slice: SOCRATIC=off → empty stdout, exit 0, status line says skipped
- [ ] socratic-slice: every invocation emits exactly one stderr status line (armed/skipped + reason)
- [ ] socratic-slice: malformed frontmatter → empty stdout + warn, exit 0 (fail-soft parser)
- [ ] plan-wall arming: prompt contains slice when present; BYTE-identical when helper empty (no stray newline)
- [ ] plan-wall: branch-NNN→specs/NNN-* resolution; non-NNN branch → empty slice, no error
- [ ] plan-wall: socratic.md edit invalidates the zero-dispatch idempotence fast path when armed
- [ ] installer syntax checks: install-socratic.sh added to ci.yml bash -n/zsh -n lines
- [ ] host-dispatch lint stays green across all SKILL.md edits (existing test)

## TDD Unit Test Map

| Source file | Test file | Functions to test + atomic behaviors |
|-------------|-----------|--------------------------------------|
| scripts/install-socratic.sh | tests/test_installer.py (extend) | pin clone, optional-patch, marker json, dest refusals |
| lib/ffs_installer.py | tests/test_installer.py (extend) | stage_socratic() staging, fingerprint, uninstall/doctor parity |
| scripts/gsd/socratic-slice.sh | tests/bats/socratic-slice.bats | domain mapping, depth, verify mode, caps, fail-soft, delimiters |
| scripts/gsd/plan-wall.sh | tests/bats/socratic-plan-wall.bats | _pw_build_prompt armed vs byte-identical |
| skills/{feature-spec,plan-decompose,review-gate}/SKILL.md | tests/test_host_dispatch_lint.py (existing) | dispatch-contract lint green |

## Integration Tests

- INT-001: fixture repo → install-socratic → author fixture socratic.md →
  socratic-slice → plan-wall prompt-build: slice present, delimited, capped
  (tests/bats/socratic-plan-wall.bats, chained case).
- INT-002: review-gate verify arming over a fixture ASSUME ledger + stub diff:
  every ASSUME-NNN gets a verdict token; a violated fixture entry produces a
  findings-queue add (tests/bats/socratic-review-gate.bats).
- INT-003: fail-soft sweep — same two chains with vendor tree deleted: outputs
  byte-identical to pre-feature baselines captured in fixtures (AC-008).

## Phase Test Gates

| Phase   | Gate condition                  | Command                                                        |
|---------|---------------------------------|----------------------------------------------------------------|
| Phase 1 | Installer tests pass            | python3 -m pytest tests/test_installer.py -q                   |
| Phase 2 | Slice helper bats pass          | bats tests/bats/socratic-slice.bats                            |
| Phase 3 | Arming bats + dispatch lint     | bats tests/bats/socratic-plan-wall.bats tests/bats/socratic-review-gate.bats && python3 -m pytest tests/test_host_dispatch_lint.py -q |
| Final   | Full suite green                | python3 -m pytest tests/ -q && bats tests/bats                 |

## Risks

- Core question files may lack `## Verification` blocks (only full files
  confirmed to carry them) → verify mode always reads full files; cheap, still
  bounded to declared domains.
- SKILL.md token growth: three skills gain instructions; keep each addition
  ≤25 lines, helper does the heavy lifting.
- Upstream churn irrelevant post-pin; bump = pin.json PR with re-review
  (runbook in docs/dependencies.md, Final phase).
- Quality lift unproven until the AC-011 A/B pass runs — if the findings diff
  is empty, the retro decision is to un-arm (delete three ≤25-line appends),
  not to keep dead machinery.

## Ledger separation (why ASSUME ≠ grants)

`ASSUME-NNN` entries are spec-time engineering defaults audited at review time
(held/violated); grant-ledger entries are TTL'd operator authorizations for
outward actions, consumed at execution. Different lifecycle, different
consumer, different failure mode — folding them into one schema would make the
review-gate audit read authorization records and the grant wall read
engineering assumptions. Kept separate deliberately.

> Voices: `[subagent-only]` — codex attempt 1 completed but final message was
> not captured; attempt 2 timed out at 600s. Cross-vendor review is still
> enforced downstream (plan-decompose opposite-host gate, plan-wall,
> review-gate), so this plan does not ship without an opposite-family pass.

## Decision Audit Trail (/autoplan, 2026-08-05, autonomous — operator pre-authorized full autonomy)

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|-------|----------|----------------|-----------|-----------|----------|
| 1 | CEO | Keep vendoring (pin+installer) vs copy-once into lib/ | USER CHALLENGE — surfaced, default stands | — | Operator explicitly directed "repo as dependency"; convention + installer partially reused; upgrade path retained | Copy-once (CEO voice rec) |
| 2 | CEO | Add A/B evidence pass (AC-011, advisory) | Auto | P1 completeness | Central quality-lift premise was unmeasured | Ship-gating on the A/B (too heavy) |
| 3 | CEO | Default-on via ffs_installer staging; no extra forcing | Auto | P3 | AC-002 already stages on default setup.sh path — finding partly misread | setup.sh hard-require |
| 4 | CEO | Ship all 3 seams in one spec (no staged rollout) | Auto | P6 bias-to-action | Each seam fail-soft + independently tested; pipeline walls re-review | review-gate-only pilot |
| 5 | CEO | Keep ASSUME ledger separate from grant ledger | Auto | P5 explicit | Different lifecycle/consumer (see Ledger separation) | fold into grant schema |
| 6 | Eng | Plan-wall seam redesigned: branch-NNN resolution + 2nd arg + conditional append | Auto | P5 | plan-wall has no $SPEC_DIR; original seam was wrong as-written | env-var handoff from caller |
| 7 | Eng | Fold socratic.md sha into PW_PLAN_SHA cache key | Auto | P1 | stale-slice reuse under adjudicated-pass otherwise | document-as-accepted-staleness |
| 8 | Eng | Optional-patch = new logic; don't claim mirror; CI syntax-check wiring added | Auto | P5 | prompt-master's pin patch key is dead code; mirror claim was false | — |
| 9 | Eng | Frontmatter parser = embedded python3, fail-soft wrapper | Auto | P4 reuse | requirement-ownership-gate.sh:104-145 precedent, inverted failure mode | naive grep/sed parsing |
| 10 | DX | Mandatory stderr status line; SOCRATIC=off kill switch | Auto | P1 | silent fail-soft degradation undebuggable; PLAN_WALL=off precedent | silent-empty contract |
| 11 | DX | Authoring-time enum validation (fail-closed) + socratic.md header contract | Auto | P1 | typo'd domain silently dropping arming recreates the vanishing-assumptions bug | consumption-time-only warn |
