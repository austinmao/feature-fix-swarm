# Autonomy Posture

Two postures govern how much promotion privilege an unattended run keeps:
`zero` (the committed default) and `floor` (strictly stricter). Posture never
raises privilege — each layer may only preserve or increase strictness
(zero < floor).

## Resolution and provenance

- Default is `zero`, committed as `autonomy.posture` in
  `templates/gsd-config.base.json`.
- Config layer: a validated `--posture` flag, else `.autonomy.posture` from
  `.planning/config.json`.
- Env layer: `FFS_AUTONOMY_POSTURE` may strengthen to `floor` but can never
  weaken a committed `floor` back to `zero` (a `POSTURE-WEAKEN-IGNORED`
  advisory is emitted); any invalid value emits one sanitized
  `POSTURE-INVALID:` advisory and falls through.
- The effective posture and its provenance print exactly once per run:
  `POSTURE-RESOLVED: <posture> source=<default|config|env>`.
- The single resolver is `scripts/gsd/autonomy-posture.sh` — the only
  production reader of `FFS_AUTONOMY_POSTURE`.

## What each posture does

- `floor` restores the no-cross-vendor-reviewer block: a missing
  opposite-vendor reviewer blocks the item
  (`BLOCKED:no-cross-vendor-reviewer`). `floor` also forbids the
  `hotfix:prod-*` emergency bypass outright (Rule 12a).
- `zero` keeps merge throughput: same-vendor review is allowed but recorded
  as a counted degradation; promotion privilege shrinks instead of the merge.

## Production degradation limits (REQ-209)

- The degradation percentage is computed over THIS run's reviews only.
- If more than half (>50%) of the run's reviews were degraded, production
  promotion is refused.
- ANY degraded review on a production-touching change refuses that promotion
  regardless of the ratio. Merge itself may still proceed in `zero`.

## Quarantine

A conflict-quarantined item is auto-requeued at most once after the base
branch advances (`zero` posture only); a second failure is terminal.

## Consumers

- `scripts/gsd/land-queue.sh` — the cross-vendor block, degradation
  recording (`note_review_invocation`), and zero-only quarantine capture.
- `lib/gates.py` — `note_degraded` (degradation ledger) and
  `check_grant_prod` (production grant authority enforcing the limits above).
- `skills/land-queue/scripts/queue-journal.py` — `cmd_count_terminals`
  (once-only requeue counter authority).

## Accepted credential risk

The loop credential's scope risk is accepted as-is by operator decision; see
"Accepted risk: loop credential scope" in `docs/promotion-protocol.md`.
