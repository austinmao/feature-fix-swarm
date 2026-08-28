# spec-012 — sync-drift-check.sh self-compare guard

## Context

`scripts/gsd/sync-drift-check.sh` compares a consumer repo's live copies of the
packaged gsd levers against the packaged source dir and reports IN-SYNC / DRIFT /
FORKED / MISSING per file. The source dir defaults to the script's own directory
(`SRC="${GSD_SYNC_SRC:-<dirname of this script>}"`, line 24).

Observed defect (memory `sync-drift-check-src-defaults-to-itself`, hit again
2026-08-27 during the openclaw #1841 sync): an operator who runs the CONSUMER's
own copy of the script against the consumer's own `scripts/gsd` dir — the
natural invocation from inside a consumer repo — gets `SRC == CONSUMER`. The
loop then `cmp`s every file against itself: every file prints `IN-SYNC`, every
real fork prints `STALE-ALLOWLIST`, and the run exits 0 with
`[sync-drift-check] OK`. That is a false green on the exact question the tool
exists to answer, and the STALE-ALLOWLIST noise actively invites pruning
real fork entries.

This spec makes the self-compare a loud, non-zero refusal that prints the
correct invocation, without changing any verdict the script gives when
`SRC != CONSUMER`.

Consumer-fork constraint: openclaw carries a declared fork of this file
(`fork-allowlist.txt` entry "consumer fork-allowlist auto-discovery + symlink
refusal hardening"). The change is confined to the SRC-resolution block
(lines 24–33) so the fork's later additions re-merge cleanly.

## User Stories

- US1 — As an operator running the drift check from inside a consumer repo, I
  want an invocation that would compare a directory to itself to be refused
  with the correct command shown, so I never mistake a self-compare for a
  clean sync.
- US2 — As a CI author wiring the check into a consumer pipeline, I want an
  explicit distinct `GSD_SYNC_SRC` to keep working exactly as today, so the
  guard adds no false negatives.

## BDD Scenarios

Feature: sync-drift-check refuses to compare a directory to itself

Scenario: implicit self-compare is refused
  Given GSD_SYNC_SRC is unset and the consumer dir argument is the directory the script lives in
  When  the operator runs sync-drift-check.sh against that dir
  Then  the run exits non-zero with a SELF-COMPARE error naming both paths and printing the GSD_SYNC_SRC recipe, and no per-file IN-SYNC lines are printed

Scenario: explicit self-compare is refused too
  Given GSD_SYNC_SRC is set to the same directory as the consumer dir argument
  When  the operator runs sync-drift-check.sh
  Then  the run exits non-zero with the same SELF-COMPARE error

Scenario: symlinked alias of the source dir is caught
  Given the consumer dir argument is a symlink whose realpath is the source dir
  When  the operator runs sync-drift-check.sh
  Then  the run exits non-zero with the SELF-COMPARE error

Scenario: distinct source and consumer behave as before
  Given GSD_SYNC_SRC points at a directory different from the consumer dir
  When  the operator runs sync-drift-check.sh
  Then  per-file IN-SYNC / DRIFT / FORKED / MISSING verdicts and the exit code are unchanged from today

Scenario: missing consumer dir still reports the existing usage error
  Given the consumer dir argument does not exist
  When  the operator runs sync-drift-check.sh
  Then  the existing "consumer scripts dir not found" usage error is printed and the exit code is 2

## Acceptance Criteria

- AC-001: When the resolved `SRC` and `CONSUMER` directories have the same
  realpath (`cd … && pwd -P`), the script prints a single stderr line beginning
  `[sync-drift-check] ERROR: SELF-COMPARE` that names both paths, prints the
  recipe `GSD_SYNC_SRC=<packaged scripts/gsd dir> sync-drift-check.sh <consumer-dir>`,
  and exits 2 — before the per-file loop runs (zero `IN-SYNC:` lines on stdout).
- AC-002: AC-001 holds whether the collision comes from the `GSD_SYNC_SRC`
  default (unset) or from an explicit `GSD_SYNC_SRC` equal to the consumer dir.
- AC-003: Realpath comparison catches symlink aliases: a consumer dir that is a
  symlink to the source dir (or vice versa) is refused under AC-001.
- AC-004: When `SRC` and `CONSUMER` differ, every verdict line
  (`IN-SYNC`/`DRIFT`/`FORKED`/`MISSING`/`STALE-ALLOWLIST`), the summary line,
  and the exit code are byte-identical to the current behaviour — the existing
  7 cases in `tests/bats/sync-drift-check.bats` pass unchanged.
- AC-005: The existing missing-consumer-dir usage error (exit 2) is evaluated
  BEFORE the self-compare check, so a nonexistent dir never reaches realpath
  resolution.
- AC-006: The diff is confined to the SRC/CONSUMER resolution block (between
  `set -euo pipefail` and `allow_reason()`); no line of the per-file loop or
  the summary changes. Verifiable: `git diff main -- scripts/gsd/sync-drift-check.sh`
  touches no line ≥ the `allow_reason()` definition.
- AC-007: `shellcheck -x scripts/gsd/sync-drift-check.sh` stays clean.

## E2E Test Paths

- PATH-001: operator inside a consumer repo runs the vendored script against
  the vendored dir with no env → refused with recipe; re-runs with
  `GSD_SYNC_SRC=<packaged dir>` → real verdicts.
- PATH-002: CI job with explicit distinct `GSD_SYNC_SRC` and an allowlist →
  verdicts identical to pre-change baseline.

## Scope ledger
source: none
slices:
- slice 0: self-compare guard in sync-drift-check.sh — CONSUMED (this spec)

## Out of scope

- Auto-discovering the packaged source dir from a consumer layout (e.g. walking
  up to `packages/feature-fix-swarm/scripts/gsd`). The openclaw fork already
  does allowlist auto-discovery; source auto-discovery is a separate decision.
- Any change to verdict semantics or the allowlist format.

## Edge Cases

- EDGE-001: `GSD_SYNC_SRC` set to a path that does not exist → the per-file
  glob matches nothing today and the script prints OK. Unchanged by this spec
  (out of scope), but the realpath step must not crash on it: resolve with
  `cd … 2>/dev/null && pwd -P || echo "$SRC"` so a bad SRC falls through to
  existing behaviour.
- EDGE-002: consumer dir given with a trailing slash or `./` prefix → realpath
  normalises; still refused when it aliases SRC.
- EDGE-003: `BASH_SOURCE` unset (script piped into bash) → `$0` fallback
  already handled on line 24; the guard uses the resolved `$SRC`, not
  `BASH_SOURCE`, so nothing new.
- EDGE-004: macOS `/private/var` vs `/var` — `pwd -P` canonicalises both
  sides the same way, so tmpdir-based tests compare consistently (see memory
  `linux-ci-parity-traps` / `local-bats-known-failures` WR-130).

## E2E Test Stubs (CLI repo — bats round trips, one per PATH-NNN)

```bash
# tests/bats/sync-drift-check.bats — additions
@test "PATH-001: self-compare from inside a consumer refused, recipe printed; explicit SRC then works" {
  # arrange: fixture consumer dir holding its own copy of the lever
  # act:     run env -u GSD_SYNC_SRC bash "$CONSUMER/sync-drift-check.sh" "$CONSUMER"
  # assert:  status 2; stderr has SELF-COMPARE + GSD_SYNC_SRC=; stdout has no IN-SYNC
  # act 2:   run env GSD_SYNC_SRC="$SRC" bash "$CONSUMER/sync-drift-check.sh" "$CONSUMER"
  # assert:  status 0; IN-SYNC lines present
}
@test "PATH-002: distinct SRC with allowlist — verdicts byte-identical to baseline" {
  # arrange: SRC + CONSUMER fixture with one fork + allowlist
  # act:     run with GSD_SYNC_SRC set
  # assert:  FORKED line with reason, exit 0 (existing case, re-pinned)
}
```

## Test Contract Summary

| Layer             | Count | Status  |
|-------------------|-------|---------|
| BDD Scenarios     | 5     | draft   |
| Unit test cases   | 6     | listed  |
| Unit test files   | 1     | mapped  |
| Integration tests | 2     | defined |
| E2E paths         | 2     | stubbed |
