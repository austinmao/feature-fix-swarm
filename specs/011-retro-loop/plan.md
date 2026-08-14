# spec-011 plan — retro loop implementation

## Prior-art decision

Per `specs/011-retro-loop/prior-art.md` adjudication: **BUILD-FRESH**, zero
code ported. Borrowed as patterns only: Homebrew notice-before-first-send +
published allowlist + revoke/status subcommand; Next.js user-scope persistent
state file; Sentry stable-field client-side fingerprint. Rejected: post-ingest
grouping, machine ids, retry queues. Discipline: do not read Sentry
(FSL/BUSL) source while implementing; python3 STDLIB only (`hashlib`, `json`,
`re`, `difflib`, `tempfile`) — no jsonschema, no rapidfuzz.

## Architecture (three phases)

Phase 1 — collector core: `scripts/gsd/retro.sh`
(verbs `check-consent | collect | grade | file | consent`) +
`lib/retro_scrub.py` (schema allowlist + deny-layer, fail-closed) + bats/pytest.
Phase 2 — wiring: `/ffs-init` consent interview question, `run-finalizer.sh`
G12-adjacent tail block, `skills/feature-spec/SKILL.md` tail line,
`skills/retro-analyze/SKILL.md`.
Phase 3 — maintainer side: `.github/workflows/retro-label.yml`,
`skills/retro-triage/SKILL.md`, `docs/retro.md`, README row, CHANGELOG.

### Component → file map

| Component | File | Notes |
|---|---|---|
| Gate chain + collector + filer | `scripts/gsd/retro.sh` | verbs `analyze \| check-consent \| collect \| grade \| file \| consent` — `analyze` orchestrates check-consent→collect→grade→scrub→file and is the ONLY entry the tails invoke; every path rc 0 from the finalizer tail's perspective except scrub-reject rc 1 internal (tail wraps in `run()`) |
| Concurrency lock | `~/.cache/feature-fix-swarm/retro.lock` | one flock serializes the whole `file` verb: create cap + ledger RMW + persisted last-write pacing timestamp (AC-007); cross-MACHINE races narrowed by pre-create re-query, surviving dups merged upstream (AC-006) |
| Scrub (fail-closed) | `lib/retro_scrub.py` | allowlist dict {key: regex}, 500-char cap, deny-layer (`/Users/`,`/home/`, non-FFS URLs, consumer remote name); rc 0 clean / 1 reject |
| Fingerprint | inside `retro.sh` (python3 -c hashlib) | `sha256(script\|event_class\|gate\|exit_code\|ffs_minor)[:16]` |
| Dedup | inside `retro.sh` | `gh issue list --label source/ffs-retro --json` → exact fingerprint in body → comment; `difflib` title ratio ≥ RETRO_TITLE_SIM → comment; else create |
| Local ledger | `~/.cache/feature-fix-swarm/retro-ledger.jsonl` | P3 accrual + failure rows; append-only |
| Consent store | `~/.cache/feature-fix-swarm/consent.json` | `{granted, asked_at, version}`; written ONLY by `retro.sh consent` / ffs-init interview |
| Consent interview | `skills/ffs-init/SKILL.md` | one recommended-yes question in the existing Phase 0 interview; headless = skip = no consent |
| Finalizer seam | `scripts/gsd/run-finalizer.sh` | one tail block after the digest call (~:920), same fail-soft `run()` wrapper; covers feature-implement/fix/task-swarm |
| feature-spec tail | `skills/feature-spec/SKILL.md` | one line: opportunistic `bash scripts/gsd/retro.sh analyze` fail-soft |
| Analysis skill | `skills/retro-analyze/SKILL.md` | operator-invocable form of the same pass |
| Labeler | `.github/workflows/retro-label.yml` | issues:write; parses metadata comment; creates labels idempotently; bumps occurrences |
| Triage skill | `skills/retro-triage/SKILL.md` | maintainer-only; origin-remote guard; clusters → prioritized `/feature-spec` briefs |
| Docs | `docs/retro.md` + README row + CHANGELOG | published allowlist = the Homebrew-pattern disclosure page |

## Unit Test List

Sequenced design-critical first:

- [ ] retro_scrub: clean allowlisted payload passes (rc 0, unchanged)
- [ ] retro_scrub: absolute path `/Users/x` in any value → rc 1
- [ ] retro_scrub: `/home/x` → rc 1
- [ ] retro_scrub: non-FFS URL in value → rc 1
- [ ] retro_scrub: consumer remote name in value → rc 1
- [ ] retro_scrub: non-allowlisted key → rc 1
- [ ] retro_scrub: value over 500 chars → rc 1
- [ ] retro_scrub: `suggested_fix` referencing a non-FFS file → rc 1
- [ ] retro_scrub: empty payload → rc 1 (nothing to file is a reject, not a pass)
- [ ] fingerprint: same facts across two runs/machines → identical 16-hex
- [ ] fingerprint: differing timestamps/durations/run-ids → identical (excluded)
- [ ] fingerprint: differing exit_code → different
- [ ] grade: scrub-failure event → P0; dead-executor event → P1; gate WARN → P2; unknown class → P3
- [ ] grade: wall-clock ≥2× active on one class → P1
- [ ] title similarity: ratio ≥0.8 → comment path; <0.8 → create path (difflib)
- [ ] consent: missing file → not granted; corrupt JSON → not granted; granted=false → not granted
- [ ] consent: symlinked consent.json → treated absent + typed warning (AC-014)
- [ ] ledger: P3 first + second occurrence accrue; third occurrence marks fileable
- [ ] ledger: symlinked ledger → treated empty; writes atomic 0600; cache dir 0700
- [ ] scrub bypass representations: `/private/…`, `/tmp/…`, `C:\`, `file://`, percent-encoded path, credential-bearing URL → all rc 1 (AC-005)
- [ ] scrub: missing or erroring scan-handoff-credentials.sh → reject, zero gh (AC-005)
- [ ] provenance (AC-002): hostile digest fixture carrying free text, an absolute path, a repo name, and code → collected payload contains only enumerated FFS-owned fields
- [ ] metrics (AC-013): derivable fixture → both metrics present with expected values; non-derivable fixture → fields omitted
- [ ] ffs_minor (AC-016): release heading `vX.Y.Z` → `X.Y`; missing/dev CHANGELOG → `0.0`

## TDD Unit Test Map

| Source file | Test file | Functions + atomic behaviors |
|---|---|---|
| lib/retro_scrub.py | lib/tests/test_retro_scrub.py | validate(): all 9 scrub cases above; deny-layer precedence over allowlist |
| scripts/gsd/retro.sh (fingerprint/grade helpers) | tests/bats/retro.bats | fingerprint stability trio; grade table; similarity threshold |
| scripts/gsd/retro.sh (gate chain) | tests/bats/retro.bats | FFS_RETRO=off / --no-retro / no-consent / no-gh-auth → typed no-op, zero gh stub calls |
| scripts/gsd/retro.sh (file verb) | tests/bats/retro.bats | create/comment/cap/pacing against a gh stub call log |
| run-finalizer.sh tail | tests/bats/retro-seam.bats | retro failure → finalizer rc unchanged; retro runs after digest; feature-spec line present (lint) |
| .github/workflows/retro-label.yml | tests/test_retro_workflow.py | YAML parses; permissions block exactly issues:write; label set matches spec |
| skills/retro-triage/SKILL.md | tests/test_host_dispatch_lint.py (additions) | origin-remote refusal text present; host dispatch contract present |

## Integration Tests

- INT-001: full pipeline against gh stub — digest fixture with 2 P1 + 1 P2 +
  4 P3 events → exactly 3 creates (cap), 1 ledger accrual, call log shows ≥2s
  spacing tokens.
- INT-002: same digest fixture twice → run 2 produces comments only (dedup by
  fingerprint), zero creates.
- INT-003: consent revoke mid-sequence → run after revoke makes zero gh calls.
- INT-004: scrub-reject payload → rc 1 propagated inside retro.sh, finalizer
  wrapper still exits 0, zero gh calls, ledger records the reject.
- INT-005: retro-triage over a stubbed `gh issue list` JSON of 6 issues
  (2 clusters × priorities) → brief orders P1 cluster before P2; brief body
  contains ONLY validated metadata + allowlisted fields (a prompt-injection
  title in the fixture must NOT survive into the brief — AC-015).
- INT-006: two CONCURRENT same-machine filers over the same payload → flock
  serializes: exactly 1 create, caps and pacing hold, ledger uncorrupted
  (AC-007).
- INT-007: scrub-order barrier (AC-005): stubbed scanner + gh assert the
  0600 copy's mode, the scanner runs on that exact path BEFORE any gh call,
  and every scanner-failure path makes zero gh calls.
- INT-008: consent lifecycle in an isolated HOME (AC-009): interview-accept
  writes user-scope 0600; headless run records nothing; revoke works;
  re-ask only on --reset/major bump; no consumer-repo file ever appears.
- INT-009: gh write authorization fail-soft (AC-007b): stubbed 403/404/422
  responses → typed line + ledger row + rc 0, no retry.
- INT-010: retro-triage outside the FFS repo with read/gh traps → typed
  rc 1 refusal, zero file reads, zero gh calls (AC-010).
- INT-011: retro-label.yml against `issues:opened` + `issue_comment:created`
  event fixtures → labels applied idempotently, own-actor events skipped,
  occurrence count edited in the metadata comment (AC-011).
- INT-012: dedup pagination (AC-006): duplicate fingerprint on an issue
  beyond the first page of a small stubbed page size → still found via the
  search fallback, no create.

## Phase Test Gates

| Phase | Gate condition | Command |
|---|---|---|
| Phase 1 | scrub + core green | `python3 -m pytest lib/tests/test_retro_scrub.py -q && bats tests/bats/retro.bats` |
| Phase 2 | seam + skills green | `bats tests/bats/retro.bats tests/bats/retro-seam.bats && python3 -m pytest tests/test_host_dispatch_lint.py -q` |
| Phase 3 | full suite + docs sweep | `python3 -m pytest -q && bats tests/bats/ && bash scripts/gsd/gates-test-command.sh` |

## Threat model (carried into decompose)

| Threat | Mitigation |
|---|---|
| Consumer data exfiltration into public issues | allowlist-first assembly; deny-layer; credential scan on 0600 copy; fail-closed zero-gh (AC-005) |
| Prompt injection via digest text reaching issue bodies | free text never crosses — only enum event classes + numeric facts |
| Issue-body injection back into maintainer triage | triage treats issue text as inert data; mechanical clustering by fingerprint/labels |
| Fingerprint spam / DoS on the repo | volatile fields excluded; RETRO_MAX_NEW_ISSUES; similarity net; workflow-side occurrence bump |
| Self-reporting loop | retro runs last, excludes its own events, fails only to local ledger |

## Rollout / rollback

All consumer-side behavior is triple-gated (consent + FFS_RETRO + --no-retro);
rollback = revoke consent or set FFS_RETRO=off; upstream artifacts are plain
issues (close them) and one workflow file (revert the file). No migrations, no
data stores beyond two user-scope JSON/JSONL files.

## Plan review round 1 (cross-vendor, folded)

gpt-5.6-terra medium (sol unavailable rc 124, degradation printed) returned 20
findings; ALL folded into spec ACs (AC-005 broadened deny-layer +
scanner-missing reject; AC-006 explicit fields/pagination/search + pre-create
re-query; AC-007 flock serialization incl. pacing timestamp; AC-007b gh write
auth fail-soft; AC-011 dual triggers + actor guard + workflow-owned occurrence
edit; AC-013 deterministic metric derivation; AC-014 state hygiene; AC-015
injection-proof briefs; AC-016 ffs_minor normalization) and the test lists
(INT-006..012, provenance/bypass/lifecycle unit rows). The `analyze` verb is
now the single tail entrypoint. CRITICAL dedup race dispositioned: same-machine
serialization by flock; cross-machine narrowed by re-query, surviving
duplicates accepted + merged upstream (no cross-machine lock exists without a
server, which is a non-goal).

## Edge resolutions (Step 2.5 probe)

- Grade-rule collision: an event matching multiple rules takes the HIGHEST
  severity (P0>P1>P2>P3).
- Multiple exact-fingerprint issue matches: comment on the LOWEST issue
  number (oldest, stable).
- Similarity threshold is inclusive (ratio == RETRO_TITLE_SIM → comment).
- Metrics ratio: rounded half-even to 2 decimals; wall<active → omit.
- Triage with zero issues: typed "no issues to triage" brief, rc 0; equal
  priorities order by lowest issue number.
