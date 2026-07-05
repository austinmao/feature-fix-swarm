# Tasks: 001-gsd-core-eval — evaluate gsd-core as FFS pipeline spine

<!--
Gating rule: Phase 1 is HARD-GATED on Phase 0 criteria (a) deterministic-gate, (c) scoped-commit-discipline,
and (d) subagent env-fit ALL passing. (b) model-overrides passes on ANY of opus/sonnet/haiku (bonus: fable).
Any of a/c/d FAIL → NO-GO, write report, STOP — do not enter Phase 1.
Isolation contract: the spike runs entirely inside $SB=$(mktemp -d /private/tmp/gsd-spike.XXXX) (a sandbox
OUTSIDE this repo); evidence accumulates in $SB/evidence/ during the run. The FFS repo receives only
docs-only curated evidence copies + report.md at phase end (spike-results/gsd-core-eval/) — nothing from
the sandbox (node_modules, .planning/, executables) lands in the repo tree.
-->

## Phase 0 — spike (sandbox)

- [X] T001 [model:haiku] [agent:tester] [US1] Create sandbox `SB=$(mktemp -d /private/tmp/gsd-spike.XXXX)`, `$SB/evidence/`, and a `git` PATH shim at `$SB/bin/git` that appends full argv + timestamp to `$SB/evidence/git-invocations.log` then execs the real git; prepend `$SB/bin` to PATH. Record `uname -a`, `node -v`, `npx -v` to `$SB/evidence/install.txt`.

- [X] T002 [model:sonnet] [agent:coder] Install `npx @opengsd/gsd-core@latest` in the sandbox, select the Claude Code runtime, run `/gsd-new-project`, and seed a minimal Node feature project (`package.json` with a `test` script, one exported function, one test file) as the target for the Phase 0 drive.
      Depends-on: T001

- [X] T003 [P] [model:haiku] [agent:tester] [US1] Pin the installed version (`npm ls @opengsd/gsd-core >> $SB/evidence/install.txt`) and snapshot the seeded `.planning/config.json` to `$SB/evidence/config-seed.json`.
      Depends-on: T002

- [X] T004 [P] [model:sonnet] [agent:tester] [US1] RED wiring check: confirm `gate.sh` (authored to `exit 1`) is wired to `workflow.test_command` in `.planning/config.json` BEFORE driving the feature to the post-merge build/test gate.
      Depends-on: T002

- [X] T005 [model:opus] [agent:tester] [US1] Verify BLOCK: drive the seeded feature through Discuss→Plan→Execute to the post-merge gate with `gate.sh` exiting 1; confirm `WAVE_FAILURE_COUNT` increments; copy `.planning/STATE.md` to `$SB/evidence/state-before.md`. If the run short-circuits before reaching the gate, record INCONCLUSIVE (= FAIL for criterion a).
      Depends-on: T004

- [X] T006 [model:sonnet] [agent:tester] [US1] [qa:e2e] Flip `gate.sh` to `exit 0`, re-run the same phase, confirm gsd proceeds, copy `.planning/STATE.md` to `$SB/evidence/state-after.md`.
      Depends-on: T005

- [X] T007 [model:sonnet] [agent:researcher] [US2] Add `model_overrides` entries for opus/sonnet/haiku to `$SB/.planning/config.json`; run gsd once per override; save each run's transcript path to `$SB/evidence/model-override-runs.txt`.
      Depends-on: T002, T003

- [X] T008 [model:sonnet] [agent:researcher] [US2] Extract the OBSERVED model per run: `message.model` from `~/.claude/projects/<sandbox-slug>/*.jsonl` (grep/jq — config-file contents do not count as observation).
      Depends-on: T007

- [X] T009 [model:sonnet] [agent:researcher] [US2] Fallback path: if JSONL extraction fails for any run, prompt the spawned agent to state its own model ID and mark that row fallback-sourced in the evidence table.
      Depends-on: T008

- [X] T010 [model:opus] [agent:tester] [US2] Classify each of the 3 override attempts as exactly one of APPLIED (requested==observed) / WRONG-MODEL (spawn ok, requested!=observed) / NO-SPAWN (spawn failed, capture error) / IGNORED (config accepted, default model observed). Only APPLIED passes criterion b.
      Depends-on: T009

- [X] T011 [P] [model:sonnet] [agent:researcher] [US2] Bonus: attempt a fully-qualified `fable-5…` model override; record spawned vs silently-ignored to `$SB/evidence/fable-override.txt`.
      Depends-on: T007

- [X] T012 [model:opus] [agent:tester] [US2] Classify the bonus fable-5 attempt using the same APPLIED/WRONG-MODEL/NO-SPAWN/IGNORED taxonomy as T010.
      Depends-on: T011

- [X] T013 [model:haiku] [agent:researcher] [US2] Write `$SB/evidence/model-overrides.txt`: requested→observed→classification 4-row table (opus, sonnet, haiku, fable-5 bonus) with transcript-excerpt refs.
      Depends-on: T010, T012

- [X] T014 [model:opus] [agent:tester] [US3] Grep `$SB/evidence/git-invocations.log` for `add (-A|\.|--all)|push` — zero matches required across the ENTIRE run (US1 + US2 activity); every logged `add` must name explicit paths.
      Depends-on: T001, T006, T013

- [X] T015 [model:opus] [agent:tester] [US3] Corroborate: `git for-each-ref refs/remotes` empty, `git remote -v` empty, per-commit `git show --stat --name-only` path lists consistent with the feature's files; write `$SB/evidence/git-foreachref.txt`, `git-remote.txt`, `git-log.txt`.
      Depends-on: T014

- [X] T016 [P] [model:opus] [agent:tester] [US4] Env-fit: observe the gsd Claude Code subagent spawn (first spawn occurs during the T005 drive) and capture completion vs 200k-window overflow error verbatim to `$SB/evidence/env-fit.txt`.
      Depends-on: T002

- [X] T017 [model:haiku] [agent:researcher] [US2] Copy `$SB/evidence/model-overrides.txt` (and other curated evidence files: `install.txt`, `state-before.md`, `state-after.md`, `git-*.txt`, `env-fit.txt`) into `spike-results/gsd-core-eval/evidence/` in the FFS repo (docs-only — no `.planning/`, no `node_modules`).
      Depends-on: T013

- [X] T018 [model:opus] [agent:tester] [US1] [US3] [US4] Write the Phase 0 section of `spike-results/gsd-core-eval/report.md`: a/b/c/d verdicts with evidence-file references; gate the Phase 1 decision on a+c+d ALL PASS (b passes if ≥1 override APPLIED).
      Depends-on: T006, T015, T016, T017

- [X] T019 [model:sonnet] [agent:reviewer] /review-gate — review Phase 0 diff (evidence + docs under `spike-results/gsd-core-eval/`). HIGH/CRITICAL findings block Phase 1. [qa:review-gate]
      Depends-on: T018
      Run: /review-gate
      Gate: no phase transition until exit code 0

## Phase 1 — parallel-run + port design (gated on Phase 0 a+c+d PASS)

- [X] T020 [model:haiku] [agent:reviewer] [US5] Phase-0-verdict gate check: read `spike-results/gsd-core-eval/report.md`, confirm a+c+d = PASS. Abort Phase 1 (do not proceed to T021) if any of a/c/d is FAIL.
      Depends-on: T019

- [X] T021 [model:sonnet] [agent:coder] [US5] [qa:e2e] Create a throwaway branch + `git worktree add .worktrees/gsd-parallel-run`; run gsd end-to-end (Discuss→Plan→Execute→Verify→Ship, no push) on ONE small real FFS task in this repo.
      Depends-on: T020

- [X] T022 [model:opus] [agent:architect] [US5] Draft the enforcement port-design mapping table to `spike-results/gsd-core-eval/port-design.md`: `lib/gates.py` evidence store re-key from `specs/**/tasks.md` to `.planning` artifacts; openclaw's `checkbox-evidence-gate.sh` PreToolUse hook script re-key to `.planning/STATE.md`; `lib/runtime_proof.py verify` (exit 0 iff proof OK) wired as `workflow.test_command`; `gates.py grant`/`check-grant` (existing shipped ledger semantics, reused verbatim) inserted around the gsd ship stage. Rows: source symbol, target, open question.
      Depends-on: T021

- [X] T023 [model:opus] [agent:architect] [US6] Write the final GO/NO-GO + a/b/c/d criteria table + evidence refs to `spike-results/gsd-core-eval/report.md`; cite the pinned gsd version (from T003) next to every doc-derived claim.
      Depends-on: T022

- [X] T024 [model:haiku] [agent:coder] [US6] Purge the sandbox ($SB) and the throwaway branch/worktree (`git worktree remove`, `git branch -D`); confirm `git for-each-ref` shows zero new remote refs before/after; append the confirmation to `report.md`.
      Depends-on: T023

- [X] T025 [model:sonnet] [agent:reviewer] /review-gate — review Phase 1 diff (port-design.md, final report.md, sandbox-purge confirmation). HIGH/CRITICAL findings block sign-off. [qa:review-gate]
      Depends-on: T024
      Run: /review-gate
      Gate: no phase transition until exit code 0

## Dependencies & Execution Order

Phase 0 runs as one linear evidence chain per criterion, with two independent side-lanes:
`T001 → T002 → {T003, T004} → T005 → T006` (criterion a) in parallel with
`T007 → T008 → T009 → T010` + `T007 → T011 → T012` → `T013 → T017` (criterion b), and
`T016` (criterion d, parallel, dep only on T002). `T014 → T015` (criterion c) depends on the
git log accumulated by BOTH lanes, so it waits on T006 and T013. `T018` synthesizes all of it,
then `T019` (review-gate) closes Phase 0. Phase 1 is a strict chain: `T020 → T021 → T022 → T023
→ T024 → T025`.

## Parallel Execution Groups

- Phase 0: T003, T004 (both dep on T002 only, different evidence files)
- Phase 0: T011, T016 (independent side-lanes; T011 dep T007, T016 dep T002)
- Phase 1: none — T020-T024 form a strict sequential chain (single sandbox/branch, single report file)

## Implementation Strategy

**Phase 0 (T001-T019):** throwaway sandbox spike. Exit gate is hard: a+c+d must ALL PASS with
captured evidence or the run stops at T018/T019 with a NO-GO — Phase 1 never starts.

**Phase 1 (T020-T025):** only entered if T020's gate check confirms PASS. Produces the port-design
mapping and the final go/no-go; ends with sandbox+branch purge and a second review-gate.

**Review-gate count:** 2 — one per phase (T019 Phase 0, T025 Phase 1). No Setup/Foundational/
per-story phases exist in this decomposition (two-phase spike structure per plan.md); no
scenarios.md/browser/design tasks (CLI-only sandbox spike, nothing browser-touchable).
