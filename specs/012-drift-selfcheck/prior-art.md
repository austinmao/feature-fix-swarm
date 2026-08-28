# spec-012 prior art

| candidate | type | stars/downloads | applicability verdict | evidence link |
|---|---|---|---|---|
| austinmao/openclaw `scripts/gsd/sync-drift-check.sh` (consumer fork) | repo | n/a (own consumer) | PARTIAL — adds allowlist auto-discovery + symlink refusal for the ALLOWLIST path, not the SRC/CONSUMER identity check; the symlink-refusal idiom (`[ -L … ]` before use) is the same class of fix and its realpath discipline is reused here | `gh search code GSD_SYNC_SRC` → openclaw `scripts/gsd/sync-drift-check.sh` |
| buildomator check-drift ratchet | pattern | cited in the script header | n/a — the ratchet pattern is what the script already implements; it has no self-compare guard to port | header comment, `scripts/gsd/sync-drift-check.sh:3` |

`gh search repos "drift check vendored scripts self compare"` → zero
vindicated candidates above `PRIOR_ART_MIN_STARS` (200). Searched:
`gh search repos`, `gh search code GSD_SYNC_SRC` (5 hits, all in this
repo or its openclaw consumer).

## Decision input

**build-fresh** — the guard is ~8 lines of realpath comparison inside a
script this repo owns. The one applicable prior is our own consumer fork,
whose symlink-refusal idiom is adopted (realpath both sides via
`cd … && pwd -P`) rather than ported wholesale, because the fork's changes
live in the allowlist block that this spec deliberately leaves untouched so
the fork re-merges cleanly (AC-006). No judge dispatched: zero vindicated
external candidates.
