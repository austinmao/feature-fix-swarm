# Plan: 001-gsd-core-eval — evaluate gsd-core as FFS pipeline spine

**Summary (read first):** Two-phase evaluation of adopting `@opengsd/gsd-core` (5.9k⭐) as the
pipeline spine over FFS custom skills. Phase 0 is a throwaway spike in a sandbox dir OUTSIDE this
repo that must *prove live* — not trust docs — that gsd (a) enforces a deterministic external gate
via `workflow.test_command`, (b) honors `model_overrides`, (c) auto-commits with scoped `git add`
and never pushes, and (d) survives this machine's ~190k-token Claude Code subagent bootstrap. Phase 1
is HARD-GATED on Phase 0 passing a+c+d (b may pass via an opus/sonnet/haiku fallback matrix); it
parallel-runs gsd on one small real FFS feature and drafts the enforcement-port plan (gates.py
evidence store, checkbox gate re-key, runtime_proof.py as test_command, autonomy-grant TTL around
ship). Deliverable: `spike-results/gsd-core-eval/report.md` + evidence, with a go/no-go. NON-GOALS:
no FFS skill migration/removal, no pushes/merges (all operator-gated).

## Tech Stack
- `@opengsd/gsd-core@latest` (npm, docs branch `next`), runtime picker = Claude Code
- gsd artifacts: `.planning/{config.json,STATE.md}`, `CONTEXT.md`
- FFS enforcement (Phase 1 port targets): `lib/gates.py` (evidence store `.feature-fix-swarm/evidence.json`),
  `lib/runtime_proof.py`, openclaw `scripts/hooks/checkbox-evidence-gate.sh`
- Host: macOS, Claude Code subagents; Python 3.11 stdlib-only for any glue

## Phases

### Phase 0 — spike (sandbox OUTSIDE repo)
- **Entry:** branch `gsd-core-eval` checked out; sandbox dir created (`SB=$(mktemp -d /private/tmp/gsd-spike.XXXX)`).
- **Isolation contract:** the spike RUNS entirely in `$SB`; all evidence is collected in `$SB/evidence/`
  during the run. Only at Phase 0 end are curated evidence copies + report.md written into the FFS repo's
  `spike-results/gsd-core-eval/` (a docs-only dir; nothing from the sandbox executes in the repo).
- **Work:** install gsd, `/gsd-new-project`, drive ONE tiny feature Discuss→Plan→Execute→Verify→Ship.
  Minimal feature shape (so the run actually traverses the build/test gate): a Node project with
  `package.json` (`test` script), one exported function + one test file; the feature adds a second
  function + test. Before scoring criterion (a), confirm from `.planning/STATE.md` that execute-phase
  reached the post-merge build/test gate (step 5.6); if the run short-circuits before the gate,
  criterion (a) is INCONCLUSIVE and counts as FAIL for Phase 1 gating.
- **Exit gate (hard):** criteria (a) gate-blocks-then-proceeds, (c) scoped-add-no-push, (d) env-fit
  ALL pass with captured evidence. (b) model-override passes on ≥1 of opus/sonnet/haiku (fable = bonus).
  Any of a/c/d fails → **NO-GO**, write report, STOP (do not enter Phase 1).

### Phase 1 — parallel-run + port design (only if Phase 0 a+c+d pass)
- **Entry:** Phase 0 report records PASS on a+c+d.
- **Work:** run gsd on one small real FFS feature *in this repo* (throwaway branch, no push); draft the
  enforcement port design mapping FFS gates onto gsd hooks/commands.
- **Exit gate:** report.md carries go/no-go + concrete port design; no commits pushed; sandbox purged.

## User Stories

**US1 — Deterministic external gate proven live** *(criterion a)*
1. `workflow.test_command` set to `bash gate.sh`; `gate.sh` exits 1.
   - Given a phase reaches the post-merge build/test gate, When `gate.sh` exits 1, Then gsd BLOCKS
     advancement and `WAVE_FAILURE_COUNT` increments (evidence: transcript + STATE.md before-snapshot).
2. Flip `gate.sh` to exit 0.
   - Given the same phase re-run, When `gate.sh` exits 0, Then gsd proceeds (evidence: STATE.md after-snapshot).
3. Evidence: two `.planning/STATE.md` snapshots + the driving transcript saved under evidence/.

**US2 — Model overrides honored** *(criterion b, fallback matrix)*
1. Set `model_overrides.<planner-agent>` = `opus`; run; capture the spawned agent's OBSERVED model ID.
   - **Observed model ID is defined as:** the `message.model` field in the subagent's transcript JSONL
     under `~/.claude/projects/<sandbox-slug>/*.jsonl` (fallback: the model line the agent reports when
     asked to state its model). Config-file contents do NOT count as observation.
2. Repeat for `sonnet` and `haiku` on a cheap agent. Real criterion = ≥1 override observably applied.
3. Bonus: attempt a fully-qualified `fable-5…` ID; record whether Claude Code spawns it or silently ignores.
4. Classify every attempt as exactly one of: APPLIED (requested==observed) / WRONG-MODEL (spawn ok,
   requested!=observed) / NO-SPAWN (spawn failed, capture error) / IGNORED (config accepted, default
   model observed). Only APPLIED passes.
5. Evidence: per-override transcript excerpt with model ID; requested→observed→classification table.

**US3 — Executor commit discipline proven** *(criterion c)*
1. **Command-level instrumentation (primary proof):** before the run, install a `git` PATH shim in the
   sandbox (`$SB/bin/git` prepended to PATH) that appends full argv + timestamp to
   `$SB/evidence/git-invocations.log` then execs the real git. After the run: log MUST contain zero
   `add -A`/`add .`/`add --all` and zero `push` invocations; every `add` names explicit paths.
2. **Corroborating state checks:** `git for-each-ref refs/remotes` empty, `git remote -v` empty,
   per-commit `git show --stat --name-only` path lists consistent with the feature's files.
3. Evidence: `git-invocations.log`, `git-foreachref.txt`, `git-remote.txt`, `git-log.txt`.

**US4 — Subagent env-fit** *(criterion d, adoption blocker)*
1. Given gsd spawns its agents as Claude Code subagents, When a spawn occurs on this ~190k-bootstrap
   machine, Then record whether the spawn completes or overflows the 200k window.
2. Evidence: spawn success/failure + any overflow error text. Fail = documented NO-GO reason.

**US5 — Enforcement port design** *(Phase 1)*
1. Given Phase 0 passed a+c+d, When drafting the port, Then report.md specifies: gates.py evidence store
   keyed off `.planning` artifacts instead of `specs/**/tasks.md`; `checkbox-evidence-gate.sh` re-keyed to
   `.planning/STATE.md`; `runtime_proof.py verify` wired as `workflow.test_command`; autonomy-grant
   wrapped around the gsd ship stage.
2. **Grant semantics are NOT designed fresh** — the port reuses `lib/gates.py`'s existing shipped
   ledger verbatim: operator issues via `gates.py grant <run-id> --action <typed-action> --ttl-hours N`;
   grants persist in the evidence store (`GATES_STORE`, default `.feature-fix-swarm/evidence.json`)
   with wall-clock expiry; `gates.py check-grant` exits non-zero for absent/expired/mismatched actions
   and the caller MUST abort the ship step and record `pending` on that exit. Port design only specifies
   WHERE in the gsd ship stage the `check-grant` call is inserted.
3. Evidence: port design section + a mapping table (FFS mechanism → gsd hook/config key).

**US6 — Go/No-Go recommendation**
1. Given all evidence collected, When writing report.md, Then it opens with a one-line GO or NO-GO and
   lists the criteria results (a/b/c/d) with evidence-file references.

## Risks (with mitigations)
- **gsd `next` branch churn** — docs/behavior may shift under us. Mitigation: pin the exact installed
  version (`npm ls @opengsd/gsd-core`) in report.md; cite version alongside every doc claim.
- **npx supply chain** — `npx @opengsd/gsd-core@latest` pulls arbitrary code. Mitigation: run only in the
  throwaway sandbox; never on the FFS repo tree during Phase 0; capture installed version + integrity.
- **Sandbox pollution** — gsd writes `.planning/`, auto-commits. Mitigation: sandbox is `mktemp -d`
  OUTSIDE the repo; Phase 1 in-repo run uses a throwaway branch, purged (`git worktree remove` / branch -D)
  and never pushed.
- **Subagent context overflow** — the known ~190k bootstrap may break gsd spawns. Mitigation: criterion (d)
  makes this a first-class pass/fail; NO-GO if it fails, before any port work.
- **Model-override silently ignored** — override may be accepted syntactically but not applied.
  Mitigation: US2 verifies the *observed* model ID from the agent transcript, not the config value.

## Dependencies
- Node/npx available; Claude Code runtime present. Network egress for npm install.
- Read access to `lib/gates.py`, `lib/runtime_proof.py` (grounded), openclaw `checkbox-evidence-gate.sh`.
- No operator gate for Phase 0 (sandbox). Phase 1 in-repo run: throwaway branch, no push (operator-gated).

## Task Breakdown

### Phase 0
- T001 [haiku] create sandbox dir + `$SB/evidence/` + git PATH shim; record host + `node -v`/`npx -v`
- T002 [sonnet] `npx @opengsd/gsd-core@latest` install, pick Claude Code runtime, `/gsd-new-project`,
  seed minimal Node feature project (package.json test script + one function + test)
- T003 [haiku] pin + record installed version (`npm ls @opengsd/gsd-core`) and `.planning/config.json` seed
- T004 [sonnet] author `gate.sh` (exit 1), wire `workflow.test_command`, drive feature to the gate
- T005 [opus] observe BLOCK + `WAVE_FAILURE_COUNT`; snapshot STATE.md (US1.1)
- T006 [sonnet] flip `gate.sh` exit 0, re-run, snapshot STATE.md, confirm proceed (US1.2)
- T007 [sonnet] set `model_overrides` opus/sonnet/haiku; run; capture agent model IDs (US2)
- T008 [sonnet] bonus fable-5 override attempt; record accepted/ignored (US2.3)
- T009 [opus] verify git-invocations.log (zero add -A/./--all, zero push) + for-each-ref/remote/log checks (US3)
- T010 [opus] record subagent spawn success/overflow for env-fit (US4)
- T011 [opus] write Phase 0 section of report.md with a/b/c/d verdicts; gate the Phase 1 decision

### Phase 1 (only if a+c+d pass)
- T012 [sonnet] create throwaway FFS branch/worktree; run gsd on one small real FFS feature (no push)
- T013 [opus] draft enforcement port design (gates.py evidence re-key, checkbox re-key, runtime_proof
  as test_command, autonomy-grant TTL around ship) — mapping table (US5)
- T014 [opus] write final go/no-go + criteria table to report.md (US6)
- T015 [haiku] purge sandbox + throwaway branch; confirm no remote refs created

## Evidence & Reporting contract
- During the run, ALL evidence accumulates in `$SB/evidence/` (sandbox). At Phase 0 end, curated copies
  + report.md are copied into `spike-results/gsd-core-eval/` in the FFS repo (docs only — no executables,
  no `.planning/` tree, no node_modules):
  - `report.md` — opens with GO/NO-GO one-liner, then a/b/c/d criteria table with evidence refs, then
    Phase 1 port design (if reached). Pinned gsd version cited next to every doc claim.
  - `evidence/state-before.md`, `evidence/state-after.md` — STATE.md snapshots (US1)
  - `evidence/model-overrides.txt` — requested→observed model-ID table (US2)
  - `evidence/git-log.txt`, `evidence/git-reflog.txt`, `evidence/git-remote.txt` (US3)
  - `evidence/env-fit.txt` — spawn success/overflow (US4)
  - `evidence/install.txt` — `npm ls` version pin + npx invocation (supply-chain risk)
- Reports, not transcripts: excerpts only, conclusion first. No pushes, no merges — operator-gated.
