# Quickstart for implementing spec 014

Work only in `/Users/luminamao/Documents/Github/feature-fix-swarm-runs/upgrade-parallel-20260912`, branch `014-parallel-host-parity`. Read spec, plan, research, data model, contracts, socratic decisions, and current `docs/upgrades` evidence. These instructions describe the implementation handoff, not a completed rollout.

## Current checkpoint

Initial Python1330pass/5fail, contract add-on42pass; first-party line76.12% (11487/15090), branch54.70% (1885/3446). Latest corrected Bats progress was778pass/1fail at test598 (`gsd-run.bats` persistently failing renew/CLAIM-STALE with drive never started), still pending final result. Do not substitute the invalid prior protective-home/filelock3.29 run.

Claude2.1.269/Codex0.154 are recorded current; current FFS ceiling rejects Codex0.154 until actual behavior proof. GSD profiles are now1.13 with Claude866/Codex868 validated hashes and72 shared skills reconciled;59 divergent prior skills were retained by installer. Historical staging incident restored12 known-old files/preserved60 staged files before this activation; no guessed full rollback occurred. Later activation analysis distinguishes source-catalog warning from emitted runtime surfaces.

Recorded manager upgrades: gh2.96, tmux3.7b, coreutils9.11, required libevent2.1.13, with552-file backup, retained old kegs, linkage checks, isolated tmux canary. Python Playwright1.62/pytest9.1.1/cov7.1/filelock3.32.6 updates and a real Chromium151 DOM canary passed. npm refreshed12existing+1nested lock entries; npm ci105/packages tree/audit0 passed. Full upgraded suites remain pending. Node/npm/custom gstack are still staged work; gbrain0.50 is staged but active0.47 remains because upstream requires stopping/draining existing DB workers, conflicting with session preservation.

## Start with the required sequence

1. Complete M0 actual baselines/backups. Read final logs/current progress; preserve pre-existing worktrees/unrelated files.
2. Independently author installer/current-host regression tests. Repair private-stage containment/all-destination manifests and actual Codex capability prerequisites. No concurrency implementation yet.
3. Complete compatible upgrades and profile→canary→profile sequence without waiting for or terminating live sessions; retain exact rollback/runtime recovery.
4. Run fresh upgraded-baseline artifact review. Empty output and upgrade/admission blockers prevent unsafe admission. Accepted M3–M6 findings receive owners/regressions and remain rollout-blocking while planned repair proceeds. Two rounds trigger escalation/adjudication without waiver or abandonment.
5. Only then implement/test M3–M6 against isolated fixture control stores; new production writer protocol stays disabled until M8.
6. Pass full M7 platform/host/security/lifecycle/coverage matrix and real canaries.
7. Execute safe legacy ownership handoff; reconcile isolated OpenClaw with full manifest/fork adjudication/bytes/drift and repeated both-host canaries before local activation; update docs and final evidence.

## Existing verification commands

Run from the isolated root with its supported Python environment:

```bash
rtk python3 -m pytest lib/ tests/ -q
rtk python3 -m pytest tests/contracts/land_queue_gates_contract.py tests/contracts/consolidate_gates_contract.py -q
rtk python3 scripts/verify-skill-blocks.py
rtk python3 scripts/lint_host_dispatch.py skills/*/SKILL.md
rtk python3 scripts/lint_model_routing.py
rtk python3 lib/model_requests.py lint templates/model-requests.json
rtk bash scripts/gsd/env-registry.sh check
rtk git diff --check
```

Use the plan's recursive Bats command to include root/scripts suites; do not run only `tests/bats`. Existing installed-host probes are read-only until their explicit canary phase; no full installer call against a live root from a supposed private fixture.

## Prospective CLI exercise after implementation

The following additive options/runner paths do not exist yet and must not be reported as already working. Use separate disposable Git repositories and a private supervisor-configured fixture state root. Set task-specific `FFS_ROOT`, `FFS_EVIDENCE_DIR`, and `FFS_OPENCLAW_WORKTREE` to verified absolute paths; do not repurpose global home variables. Private stage root mapping is enforced by the tested helper/sandbox (the successful macOS canary used os.homedir preload without HOME/CODEX_HOME changes).

```bash
rtk env PYTHONPATH="$FFS_ROOT/lib" python3 -m run_state.cli start --skill feature --objective "fixture objective" --activity plan --run-id adhoc-fixture-one --json
rtk env PYTHONPATH="$FFS_ROOT/lib" python3 -m run_state.cli status adhoc-fixture-one --json
rtk python3 scripts/verification/parallel_host_parity.py matrix --mode hermetic --repetitions 25 --output "$FFS_EVIDENCE_DIR/matrix.json"
rtk python3 scripts/verification/parallel_host_parity.py hosts --authenticated --pairings claude-claude,claude-codex,codex-codex --review-directions claude-codex,codex-claude --soak-seconds 600 --output "$FFS_EVIDENCE_DIR/hosts.json"
rtk python3 scripts/verification/parallel_host_parity.py rollout --consumer "$FFS_OPENCLAW_WORKTREE" --output "$FFS_EVIDENCE_DIR/rollout.json"
rtk python3 scripts/verification/parallel_host_parity.py hosts --authenticated --pairings claude-claude,claude-codex,codex-codex --review-directions claude-codex,codex-claude --soak-seconds 600 --consumer "$FFS_OPENCLAW_WORKTREE" --output "$FFS_EVIDENCE_DIR/consumer-hosts.json"
rtk env GSD_SYNC_SRC="$FFS_ROOT/scripts/gsd" bash scripts/gsd/sync-drift-check.sh "$FFS_OPENCLAW_WORKTREE/scripts/gsd" --allowlist "$FFS_OPENCLAW_WORKTREE/scripts/gsd/fork-allowlist.txt"
rtk python3 scripts/verification/parallel_host_parity.py aggregate --evidence "$FFS_EVIDENCE_DIR" --require-all-paths --output "$FFS_EVIDENCE_DIR/final.json"
```

Expected: JSON selection/ownership/context, unique workspace before planning, durable evidence, no silent ambiguous resume or repeated completed activity, no duplicate child/budget reset after injected crashes. Missing existing authentication is UNMET; never ask for credentials or replace real proof with a fixture.

## Authorization and recovery

No real-repository commits on any branch, pushes, releases, tenant deployment, unrelated cleanup, or discarded local work. Disposable fixture commits only. Existing upgrades/implementation/testing/local rollout are authorized subject to their concrete gates; no new inferred approval prompts.

Keep backups/evidence outside removable worktrees. Failed activation uses hash-guarded owned-file restoration, never guessed old skill bytes or overwrite of concurrent edits. Live legacy owners remain intact until safe handoff. Rollback preserves new evidence and one writer protocol. Linked worktrees share Git objects and are cooperative boundaries, not hostile-worker isolation.

Spec Kit1.0.6 setup-plan ran once. Optional update-agent-context extension is absent; this handoff supplies context without fabricating a script, changing user-global instructions, or auto-committing. Next parent steps remain independent plan review, clarify, decomposition, and preflight before implementation.
