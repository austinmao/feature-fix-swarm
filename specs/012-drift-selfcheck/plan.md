# spec-012 plan — sync-drift-check self-compare guard

## Prior-art decision

build-fresh (see `specs/012-drift-selfcheck/prior-art.md`): zero vindicated
external candidates; the only applicable prior is the openclaw consumer fork
of this same file, whose realpath/symlink idiom is reused but whose block is
left untouched so the fork re-merges (AC-006).

## Architecture (one phase)

Single file, single block. `scripts/gsd/sync-drift-check.sh` lines 24–33
(SRC/CONSUMER resolution + the existing missing-dir usage error) become:

```bash
SRC="${GSD_SYNC_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)}"
CONSUMER="${1:-}"
ALLOWLIST=""
[ "${2:-}" = "--allowlist" ] && ALLOWLIST="${3:-}"

if [ -z "$CONSUMER" ] || [ ! -d "$CONSUMER" ]; then
  # (existing usage error, exit 2 — unchanged, evaluated FIRST: AC-005)
fi

# Self-compare guard (spec-012): comparing a dir to itself prints IN-SYNC for
# every file and STALE-ALLOWLIST for every real fork — a false green on the
# exact question this tool answers. Realpath both sides so symlink aliases
# and ./ or trailing-slash spellings collapse (AC-003). A SRC that does not
# exist falls through to today's behaviour (EDGE-001).
_src_real="$(cd "$SRC" 2>/dev/null && pwd -P || printf '%s' "$SRC")"
_dst_real="$(cd "$CONSUMER" && pwd -P)"
if [ "$_src_real" = "$_dst_real" ]; then
  echo "[sync-drift-check] ERROR: SELF-COMPARE — source and consumer are the same directory ($_dst_real); every file would report IN-SYNC" >&2
  echo "usage: GSD_SYNC_SRC=<packaged scripts/gsd dir> sync-drift-check.sh <consumer-scripts-dir> [--allowlist FILE]" >&2
  exit 2
fi
```

Nothing below `allow_reason()` changes.

### Component → file map

| Component | File | Change |
|---|---|---|
| guard | `scripts/gsd/sync-drift-check.sh` | +~12 lines in the resolution block |
| tests | `tests/bats/sync-drift-check.bats` | +5 cases, existing 7 untouched |
| changelog | `CHANGELOG.md` | one Fixed entry |
| memory | (operator's memory dir, out of repo) | retire `sync-drift-check-src-defaults-to-itself` trap note |

## Unit Test List

Sequenced design-critical first:

- [ ] guard: implicit self-compare (GSD_SYNC_SRC unset, consumer == script dir) → exit 2, stderr `SELF-COMPARE`, stdout has no `IN-SYNC:` (AC-001, AC-002)
- [ ] guard: explicit self-compare (GSD_SYNC_SRC == consumer) → exit 2, same error (AC-002)
- [ ] guard: symlinked consumer dir aliasing SRC → exit 2 (AC-003)
- [ ] guard: recipe line contains `GSD_SYNC_SRC=` (AC-001)
- [ ] ordering: nonexistent consumer dir → existing "not found" error, exit 2, no SELF-COMPARE text (AC-005)
- [ ] regression: existing 7 cases (identical / drift / py drift / fork / missing / stale / missing-dir) pass byte-for-byte with distinct SRC (AC-004)

## TDD Unit Test Map

| Source file | Test file | Functions to test + atomic behaviors |
|---|---|---|
| `scripts/gsd/sync-drift-check.sh` | `tests/bats/sync-drift-check.bats` | resolution block — implicit self-compare refused; explicit refused; symlink alias refused; recipe printed; missing-dir error precedes guard; distinct dirs unchanged |

## Integration Tests

- INT-001: from a fixture "consumer" that holds its own copy of the lever,
  run that copy against its own dir with no env → exit 2 + recipe; then with
  `GSD_SYNC_SRC=<fixture packaged dir>` → real verdicts (PATH-001 round trip).
- INT-002: openclaw-shaped fixture (root `scripts/gsd` + `packages/…/scripts/gsd`,
  allowlist with one fork) with explicit SRC → `FORKED` line with reason,
  exit 0, output byte-identical to a pre-change run of the same fixture (PATH-002).

## Phase Test Gates

| Phase | Gate condition | Command |
|---|---|---|
| Phase 1 | guard + regression bats green, shellcheck clean | `bats tests/bats/sync-drift-check.bats && shellcheck -x scripts/gsd/sync-drift-check.sh` |
| Final | full bats consumers of the lever green | `bats tests/bats/sync-drift-check.bats tests/bats/setup-install.bats` |

## Threat model (carried into decompose)

- T1 — false green on a consumer sync (the defect itself): closed by AC-001.
- T2 — the guard introduces a false NEGATIVE (refuses a legitimate distinct
  compare): bounded by realpath equality only; AC-004 regression suite proves
  distinct dirs are untouched.
- T3 — consumer-fork merge conflict: bounded by AC-006 (diff confined to the
  resolution block).

## Rollout / rollback

Single PR to FFS main; openclaw picks it up at its next re-sync (its fork's
allowlist block is below the changed region). Rollback: revert the commit —
no state, no data.
