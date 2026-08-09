# Digest — run observability without a daemon

`scripts/gsd/digest.sh` is the G12 notification seam (spec-008 REQ-701/702/703):
a read-only poll over the evidence store and run-state DB that emits each
contracted event class **exactly once**, plus a daily summary that degrades
honestly. Observability never gates — every degraded input exits 0; the only
nonzero exits are usage errors and a failed `--record-baseline`.

## Modes

```sh
scripts/gsd/digest.sh --immediate        # new events since the last poll
scripts/gsd/digest.sh --daily            # seven-field summary (cursor-free)
scripts/gsd/digest.sh --record-baseline  # operator step: record the drift baseline
```

Event lines go to stdout; diagnostics to stderr.

## Event classes (`--immediate`)

Nine classes: `waiver`, `tripped-rung`, `loop-cap`, `budget-breach`,
`finisher-skipped`, `promotion`, `rollback-dryrun`, `scan-tamper`, `drift`.
One line per event, `class key=value ...` shaped. Join keys (REQ-703): the
ledger `run_id` is canonical — budget rows join runstore→ledger via the
phase-1 `_degradation.mappings` record; `promotion` lines carry the artifact
sha and `finisher-skipped` lines the PR number for gh-side joins. Rows with
no mapping emit their native id honestly (`run_id=unmapped` /
`run_id=unattributed`) — never a fabricated join.

## Cursor semantics

The cursor lives at `.feature-fix-swarm/digest-cursor.json` beside the
resolved evidence store (`$GATES_STORE` honored), written atomically per
class after successful emission. Keys are tie-safe positions, never
timestamps: append-only evidence lists use `{count, last_fp}` (sha256 of the
last emitted row; a mismatch means rewritten history → conservative re-init
to end-of-list plus one stderr note); run-state uses the sqlite rowid;
scan-tamper a git sha; drift the last-emitted fingerprint.

**scan-tamper initialization rule:** when its cursor is absent the class
records the current `HEAD` and emits nothing — history that predates the
first poll is never scanned (no unbounded scan). Subsequent polls scan each
new first-parent commit's diff through `python3 lib/gates.py scan-tamper`.

## Invocation seams (no daemon)

1. **Run end:** `run-finalizer.sh` invokes `digest.sh --immediate` fail-soft
   just before its final note (presence-guarded; can never change finalizer
   exit status).
2. **Operator cron:**

```cron
*/15 * * * * cd /path/to/repo && scripts/gsd/digest.sh --immediate
0 9 * * *   cd /path/to/repo && scripts/gsd/digest.sh --daily
```

## Drift baseline

```sh
scripts/gsd/digest.sh --record-baseline   # writes .feature-fix-swarm/drift-baseline.json
```

Baseline = `gh api repos/{owner}/{repo}/branches/main/protection` plus the
`.github/workflows` contents listing (per-file blob shas) — both verified
live 2026-08-09 (the org audit-log API 404s on personal repos, locked row
17). This is the one loud command: it fails nonzero if gh fails. The
immediate `drift` class then compares current state against the baseline;
baseline absent or gh unreachable is a stderr note, exit 0, cursor retained.

## DIGEST_NOTIFY_CMD (trust boundary)

If set, each class's new lines are piped on stdin to
`sh -c "$DIGEST_NOTIFY_CMD"`, invoked once **per class** — one transport
failure retains only that class's cursor (guaranteed redelivery) and never
blocks the others. This is an operator-configured environment variable
executed on the operator's own machine: it is the operator's existing shell
authority, not an injection surface (T-04-05, accepted). It is never invoked
in tests.

```sh
DIGEST_NOTIFY_CMD='slack-post #ops' scripts/gsd/digest.sh --immediate
```

## Daily fields (`--daily`)

`specs` (completed/quarantined), `merges` (local git, with shas),
`degraded-review` (ratio), `tokens` (used vs budget), `pending`,
`stranded` (branch/worktree counts), `oldest-pr`. gh-backed fields render
`unavailable` when gh fails; git-backed fields are always local.
