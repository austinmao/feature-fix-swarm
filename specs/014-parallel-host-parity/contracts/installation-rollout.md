# Installation, migration, and consumer rollout contract

Version: 1 (prospective). Covers FR-001–010 and FR-048–060.

## Installation transaction and manifest

Every managed operation produces a manifest with schema/version, manager/source/package integrity, runtime, resolved config/skill/agent/hook roots, existing ownership/hash, proposed hash, backup locator, customization/adjudication status, validation/canary references, and rollback checkpoint. Resolve layout through upstream runtime mapping and validate every destination; Codex shared skills are not necessarily under `--config-dir`.

Private stage confines effective home/config/skill/GSD destinations with a tested child-only root mapping plus filesystem enforcement; no global home variables are repurposed. The successful macOS canary used a verified os.homedir preload and sandbox/private GSD/TMP/config roots, without HOME/CODEX_HOME rewrites; prove equivalent Ubuntu containment. Before and after execution, hash destination snapshots and enforce that every touched path belongs to the private stage. A config-dir-only stage is invalid. Runtime leak check inspects emitted skills/agents/hooks and actually referenced execution surfaces; source-catalog warnings are separate diagnostics.

Back up shared profile roots, external skills, manifests/config/hooks, runtime markers, customized Node/gstack integrations and manager links before activation. Recheck live hash immediately before replace/restore; concurrent or unowned changes refuse overwrite. Namespace prefixes are discovery hints, not ownership proof. Historical12proven-old/60staged state and subsequent1.13reconciliation with59retained prior divergent files remain explicitly recorded; never invent missing old bytes or delete the extra name on assumptions.

Profile1 activation → attributable canary → profile2 activation. A failed first canary prevents the second. Old sessions remain running, with old runtime identities and recovery instructions; no zero-downtime claim. Record every tool old/new/source/manager/verification/rollback/incompatibility and preserve supported dependency ranges, direct declarations, custom patches, and newer pinned skill commits.

## Migration writer epoch

Reader compatibility is separate from installation and does not enable new writers. Real legacy dual-read must compare normalized content without changing its hash. Under the existing short coordination transaction, validate released/proven-dead legacy owners, freeze old writer admission, and activate exactly one new writer epoch. LIVE/UNKNOWN never permits handoff; PID fabrication and live-owner rewrites are forbidden.

Journal key is source identity/hash plus record key. Preserve every legacy record; import once or quarantine with reason. Interrupted restart replays idempotently. Rollback freezes writes, preserves new-format evidence, and selects one compatible writer; unsupported reverse migration leaves explicit paused state rather than mixing writers or overwriting newer evidence.

## Full OpenClaw manifest

Inventory the verified isolated OpenClaw integration workspace after FFS gates. Required manifest surface classes: vendored FFS package, root wrappers, dependency/skill pins, coordination helpers, libraries, schemas, installed Claude/Codex FFS surfaces, and explicitly required repo guidance. Record absolute source/destination, owner, before/after hash, required/optional flag, fork entry/adjudication, adaptation regression reference, validation, and rollback. Consumer-owned `.claude/skills`/`.codex/skills` and separate user-global guidance are protected exclusions, not generic deletion targets.

Every actual fork-allowlist entry gets a disposition (`port_to_canonical`, `retain_consumer_owned`, or `obsolete_with_evidence`) and owner/reason. A findings-queue entry is handled if actually inventoried. No blind allowlist skip; required canonical ports have regression tests before adoption.

Activation requires full manifest completeness, canonical source identity, staged byte equality, explicit `GSD_SYNC_SRC` drift check, and repeated authenticated both-host concurrent canaries. Existing drift helper's MISSING/allowlisted-fork output alone is insufficient for full rollout. Doctor PASS alone is insufficient. Local activation is already authorized when gates pass; real-repository commits/push/releases and tenant deployments remain unauthorized.

Rollback restores only previously snapshotted owned bytes whose current hashes still match the replaced generation, preserves consumer changes/new evidence, records changed manager links, and repeats required host consistency checks. No guessed whole-directory restoration.
