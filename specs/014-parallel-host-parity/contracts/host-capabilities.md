# Host capability and launch contract

Version: 1 (prospective). Covers FR-029–035, FR-043–046. M1 implements only installer/current-Codex compatibility prerequisites; full concurrent-host isolation follows M2.

## Canonical catalog and evidence

`templates/host-capabilities.json` is the sole versioned host contract; `lib/host_capabilities.py` loads/validates it. Installer doctor, runner, bundle construction, probes, and resume consume it. Each entry defines supported CLI command shapes, required capabilities, strict configuration controls, emitted runtime roots, auth adapter, known version eligibility, and proof schema. A version range is not proof of behavior. Current observed versions are Claude2.1.269/Codex0.154.0; refresh identities if installation changes.

A capability result binds host/executable path and SHA-256, version, platform, bundle/configuration hashes, requested model/tier/effort, operation, allow/deny expected/observed behavior, exit code, bounded timing, artifact locators, timestamp, and `authenticated` or `hermetic` provenance. Missing/empty/mismatched results do not pass. Tests against installed hosts must prove actual hooks, skills, policy, auth, and routing.

| Native tier | Codex | Claude |
| --- | --- | --- |
| Frontier planning | GPT-6 Astra / xhigh | Fable |
| Judgment/review | GPT-5.6 Sol / high | Opus |
| Implementation | GPT-5.6 Terra / medium | Sonnet |
| Bounded inventory/docs/synthesis | GPT-5.6 Luna / low | Haiku |

Preserve invoking host; unspecified means Claude. Exact unavailable requests fail without substitution. Automatic fallback never reaches frontier; actual fallback host/model and degraded provenance are mandatory. Acceptance authors differ from implementers; review input contains artifacts/provenance, not producer reasoning history. Opposite-vendor preferred, distinct-model fallback degraded, self-review never independent.

## Runtime bundle

Immutable manifest contains FFS source/runtime and GSD package hashes, generated skill/agent/hook hashes, host executable identity, selected policy/config hash, and model request. Build in a private staging directory; verify every included byte; atomically publish content-addressed bundle. Read-only reuse requires matching manifest. Do not copy unverified active source/config or mutable arbitrary hook paths into a trusted bundle.

Strict Claude: exclude ambient user/project settings, explicitly load trusted config, preserve supported subscription authentication, refuse prompt-hanging command shapes, and independently enforce shell networking/native web/file/hook controls. Managed policy is inspected; unknown execution-widening policy refuses preflight. A Bash sandbox does not establish native-tool denial. Choose actual flags from installed CLI/help and verified canaries before adding their version to the supported contract; never guess flags based solely on a newer version.

Codex: verify actual hook registration/execution, sandbox denials, skill discovery, native routing, OAuth synchronization, and effective working-directory/tool roots. Existing `codex-runtime-bundle.py`, `sanitize-codex-config.py`, `sync-codex-auth.py`, and `stage-gsd-skills.py` remain thin compatible wrappers around shared audited logic.

## File and control authority

Worker write roots: its registered source/planning workspace, own attempt progress/evidence, and only necessary own Git administrative files. Shared control database, grants, sibling stores, installed bundles/config, and primary checkout are denied. Do not grant whole Git-common-dir write permission. Objects remain shared; hostile same-user Git isolation is not promised. Credential sync is supervisor-controlled with private regular-file checks and short lock; logs contain names/hashes, never secret values.

## Resume/review gate

Resume compares immutable bundle, runtime/model/effort, CLI identity, configuration/policy, and workspace/upstream bindings. Unapproved drift yields `RUNTIME_DRIFT` and recovery instructions; no silent tuple rewrite. Existing authorized credential refresh is verified separately from policy changes.

Required review gate accepts only nonempty attributable artifacts, actual reviewer identity, verdict, rounds, and finding state. At most two fix rounds/set precede written adjudication. Confirmed critical/high defects cannot be waived. Upgraded review must be complete before planned concurrency repair; upgrade/admission blockers gate their dependent launches, while accepted later-phase findings remain assigned and rollout-blocking. Final readiness cannot inherit advisory wall HIGH residual policy. Two rounds trigger escalation/adjudication, never abandonment.

Observed current-host gap: subscription Luna-low marker invocation passed, but three malformed ambient agent TOMLs and449 dropped skills appeared despite ignore-user-config. This is evidence that config-ignore alone does not isolate discovery. The capability fixture must assert no such diagnostics and prove the exact required skills/agents/hooks through the CLI.
