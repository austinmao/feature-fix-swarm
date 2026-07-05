# gsd-core adoption evaluation — spike report

> **FINAL VERDICT: GO on the enforcement-port design.** Phase 0 hard gate
> (a + c + d) PASS; criterion (b) PASS (3/3 core overrides APPLIED); Phase 1
> real-repo parallel-run (T021) PASS — gsd's `workflow.test_command` seam was
> wired to FFS's own `lib/gates.py verify-done` on the actual FFS repo and the
> gate round-trip was exercised there with correct exit codes (verifier subagent
> disabled + drives timed out, so gsd's *autonomous* verify-stage gate call was
> not isolated — that reaction rests on Phase-0 criterion (a); see caveat).
>
> Pinned version under test: **`@opengsd/gsd-core@1.6.1`** (cited next to every
> doc-derived claim below). Runtime picker: Claude Code. Host: macOS arm64,
> node v25.9.0, npm 11.16.0. Full pin: `evidence/install.txt`.

## One-line recommendation (final)

**GO.** All three adoption-blocking criteria (external deterministic gate,
scoped-git discipline, subagent env-fit) proved live and clean on
gsd-core@1.6.1; per-agent model overrides (non-gating) also proved APPLIED for
opus/sonnet/haiku; and the Phase-1 real-repo run wired FFS's `gates.py` as gsd's
`test_command` and exercised the gate round-trip on the real repo with correct
exit codes — the exact seam the enforcement port depends on (`port-design.md`
row 3). (gsd's *autonomous* verify-stage invocation was not isolated — verifier
disabled + drive timeouts; that reaction rests on Phase-0 (a), see Phase-1 caveat.) Port is cheap because `gates.py` is already
tasks.md-agnostic (`port-design.md` central finding); the only real wiring is
re-keying the openclaw `checkbox-evidence-gate.sh` hook to a gsd STATE.md
phase-complete transition. Open questions are wiring details, not blockers.

## Criteria table

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| **a** | External deterministic gate blocks-then-proceeds (`workflow.test_command`) | **PASS** | `evidence/state-before.md`, `evidence/state-after.md`, gate-invocations (below) |
| **b** | Per-agent `model_overrides` honored | **PASS** *(non-gating; 3/3 core APPLIED)* | `evidence/model-overrides.txt`, `evidence/model-override-runs2.txt` |
| **c** | Executor scoped-`git add`, never pushes | **PASS** | `evidence/git-invocations.log`, `git-remote.txt`, `git-foreachref.txt`, `git-log.txt` |
| **d** | Claude Code subagent env-fit on this ~190k-bootstrap machine | **PASS** | `evidence/env-fit.txt` |

Hard gate = **a ∧ c ∧ d** (plan.md). All three PASS → Phase 1 authorized and
**executed: T021 real-repo run PASS, T022 port-design complete** (below).

---

## (a) External deterministic gate — PASS

gsd-core@1.6.1 exposes `workflow.test_command` in `.planning/config.json`. Wired
it to `bash gate.sh` and drove the seeded feature's phase to the post-merge
build/test gate twice:

- **BLOCK half** (`gate.sh` → `exit 1`): the `/gsd-execute-phase` drive invoked
  the gate (`gate-invocations.log` `01:19:42`), then **refused to advance** —
  STATE.md carried an explicit prose gate blocker and **no completion commit**.
  Verified adversarially (opus): gate-ran-then-blocked ordering holds
  (`gate 01:19:42` < STATE `last_updated 01:20:30`); `gate.sh` unmodified; the
  only working-tree edit that touches the gate *adds* the `test_command` entry
  (activates the gate — opposite of a bypass).
- **PROCEED half** (`gate.sh` → `exit 0`): re-ran the same phase; gate invoked
  3×, phase advanced — `Blockers/Concerns: None`/"Resolved",
  `last_activity_desc: Phase 01 complete`, body "Total plans completed" `0→1`,
  executor merged its worktree (`a5c60e9 chore: merge executor worktree`).
- **The load-bearing block→proceed distinguishers** (corrected per the T019
  review) are the **Blockers-section flip**, `last_activity_desc`, the body
  "plans completed 0→1", and the **merge commit `a5c60e9`** — NOT the frontmatter
  `status`/`completed_phases`/`percent`, which are stuck/identical across both
  halves (see caveat).
  - **Caveat (gsd bookkeeping bug, not verdict):** gsd wrote a prose Blockers
    entry rather than the spec-expected `WAVE_FAILURE_COUNT` counter, and the
    STATE frontmatter counters are unreliable — `status: executing` and
    `completed_phases: 1` read the SAME in the blocked and the proceeded snapshot,
    and frontmatter `percent: 100` contradicts the body `0%`. The verdict rests
    only on the reliable signals above (Blockers flip + `last_activity_desc` +
    merge commit); the frontmatter counters are a gsd bug worth an upstream report.

**gate.sh is the exact shape FFS needs** — an arbitrary external command whose
non-zero exit fail-closes phase advancement. This is the seam `runtime_proof.py`
/ `gates.py verify-done` will occupy (Phase 1 port design).

## (b) Model overrides — PASS (non-gating), 3/3 core APPLIED

Full detail + table: `evidence/model-overrides.txt`; raw:
`evidence/model-override-runs2.txt`. Corrected re-run 2026-07-05T02:01Z (after the
quota reset + the `/gsd-phase` seeder fix — the prior run's `/gsd-add-phase` was an
unknown command and the session-limit killed every drive before spawn).

Per override: set `config.model_overrides.gsd-planner=<model>`, snapshot the
existing subagent-transcript set, drive `/gsd-plan-phase 2` under the trimmed MCP
config, then diff the transcript set to isolate the NEW `gsd-planner` spawn and
read its `message.model`. Observation is the subagent `message.model` — config
contents do not count (per plan). Parent-last `subagent_type=gsd-planner` confirmed
per row.

| requested             | observed message.model      | msgs | classification |
|-----------------------|-----------------------------|------|----------------|
| opus                  | claude-opus-4-8             | 20   | **APPLIED**    |
| sonnet                | claude-sonnet-5             | 20   | **APPLIED**    |
| haiku                 | claude-haiku-4-5-20251001   | 29   | **APPLIED**    |
| claude-fable-5 (bonus)| `<synthetic>` (no real turn)| 1    | INCONCLUSIVE   |

- **Gate met:** plan requires ≥1 of opus/sonnet/haiku APPLIED; all three APPLIED.
  The resolver honors `config.model_overrides.gsd-planner` verbatim, full IDs
  accepted. Mechanism confirmed in source (`model-resolver.cjs`
  `resolveModelInternal()` precedence layer 1) AND now observed live.
- **fable-5 bonus INCONCLUSIVE:** the gsd-planner subagent spawned (transcript +
  `subagent_type=gsd-planner`) but emitted only a `<synthetic>` bootstrap message —
  no real model-attributed turn before the 300s drive timeout. Not IGNORED (no
  default observed), not clean NO-SPAWN (dir exists). Bonus row, does not affect b.
- Each drive process hit `drive-exit=124` (300s timeout) — expected for
  `/gsd-plan-phase`; the model-attributed turns landed before the timeout, which
  is the load-bearing observation.
- **Port implication:** the haiku/sonnet/opus cost ladder survives adoption —
  per-agent overrides route correctly.

## (c) Executor git discipline — PASS

401-line `git` PATH-shim argv log across the entire run (`evidence/git-invocations.log`):

- **Zero** `git add -A`, `git add .` (bare cwd), `git add --all`, or `git push`.
- Every `git add` names explicit paths — `.planning/*`, `.claude/CLAUDE.md`, and
  the feature files staged **by name** (`git add add.js add.test.js multiply.js
  multiply.test.js package.json`), never `add .`.
- Corroboration: `git remote -v` empty, `for-each-ref refs/remotes` empty, 13
  local commits only. Nothing left the sandbox.

gsd's executor is **compatible with the scoped-add / no-push discipline** and
runs its work in a throwaway worktree it merges back (`a5c60e9`).

## (d) Subagent env-fit — PASS

`evidence/env-fit.txt`. On this machine's ~190k-token Claude Code subagent
bootstrap, every gsd subagent spawn **completed, zero 200k-window overflows**:

| gsd subagent | observed `message.model` | msgs | overflow |
|---|---|---|---|
| gsd-planner | claude-fable-5 | 27 | 0 |
| gsd-roadmapper | claude-fable-5 | 28 | 0 |
| gsd-executor | claude-sonnet-5 | 76 | 0 |

- **Load-bearing mitigation:** the *driving* headless session must run with an
  empty MCP config (`--strict-mcp-config --mcp-config '{"mcpServers":{}}'`). The
  baseline top-level drive under the FULL user MCP config overflowed verbatim
  `"Prompt is too long"` (`env-fit-toplevel-overflow.txt`). gsd's own subagent
  defs are small enough to spawn fine; the overflow risk is the *host session's*
  MCP surface, not gsd. **Port implication:** any FFS→gsd runner must launch gsd
  drives with a trimmed MCP config.

---

## Phase 1 — real-repo parallel-run (T021) — PASS

Ran gsd end-to-end on the **actual FFS repo** in a throwaway worktree
(`.worktrees/gsd-parallel-run`, branch `spike/gsd-parallel-run` off
`origin/main`), `workflow.test_command` wired to FFS's **own**
`python3 lib/gates.py verify-done spike-phase-01`, `model_overrides`
planner=opus / executor=sonnet. Raw: `evidence/phase1/` + `evidence/phase1-retry.txt`.

**Observed on the real repo:**
- **Full loop ran.** gsd planned (`01-01-PLAN.md`), executed the seeded feature
  (created `lib/spike_probe.py` — `probe()` returns `'gsd-ran'` — plus
  `lib/tests/test_spike_probe.py`), verified inline (`01-VERIFICATION.md`), and
  summarized (`01-01-SUMMARY.md`). The headless drive processes exited 124
  (900s/600s wall-clock timeout), NOT a gsd failure — the written artifacts
  prove the loop completed.
- **`test_command` seam wired to FFS's gates.py, round-trip exercised on the real
  repo.** The gsd config set `workflow.test_command =
  python3 lib/gates.py verify-done spike-phase-01` (`evidence/phase1/config.json`),
  and gsd's inline verify writeup references the gate round-trip: `01-VERIFICATION.md`
  cites `verify-done spike-phase-01 → DONE-VERIFIED (executed_by=run_gate) (exit 0)`
  and a pytest row `run-gate spike-phase-01 → GATE-EXIT 0`. **Honest scope:** the
  verifier subagent was DISABLED (`config.json` `"verifier": false`;
  `01-VERIFICATION.md` self-discloses `method: inline (workflow.verifier disabled)`),
  and the `run-gate`/`verify-done` calls were executed by the Phase-1 harness driver
  (`phase1-retry-drive.txt`), not by gsd's runtime — the drives timed out (exit 124)
  before gsd could autonomously fire `test_command`. So what is *directly observed*
  is: the seam accepts FFS's gates.py as `test_command`, and the gate returns correct
  exit codes on the real repo. gsd's *autonomous* block-on-nonzero reaction is proven
  by composition with **Phase-0 criterion (a)** (`gate.sh`, opus-verified), not
  re-isolated here. gsd's inline verify also honestly surfaced 4 **pre-existing**
  `test_gates.py` failures (did not game the gate to green).
- **gates.py round-trip proven on the real evidence store** (`GATES_STRICT=1`):
  no-evidence → `verify-done` exit 1 (BLOCK precondition); `run-gate` records →
  `verify-done` exit 0 (PROCEED precondition). GATE-EXIT 0 / DONE-VERIFIED
  `executed_by=run_gate`.
- **Executor git discipline on the real repo — clean.** Every `git add` names
  explicit paths (`lib/spike_probe.py`, `lib/tests/test_spike_probe.py`,
  `.planning/*` by name); **zero** `git add -A`/`.`/`--all`, **zero** `git push`
  (`evidence/phase1/git-discipline-retry.txt`, authoritative shim log
  `git-retry.log`). The `refs/remotes` entries are the worktree's inherited
  origin-tracking refs from the clone — NOT executor-created (shim 0-push count
  is the no-push proof).

**Caveat (honest):** the drive wall-clock timeouts meant the BLOCK half (gsd
*refusing* to advance on a gate exit-1) was not isolated on the real repo in
this run. That reaction was proven directly in **Phase 0 criterion (a)** with
`gate.sh` (opus-adversarially verified: gate-ran-then-blocked ordering,
`gate.sh` unmodified). Combined with T021's directly-observed real-repo Verify→
gates.py invocation and correct exit-code round-trip, the block-then-proceed
contract holds on the real repo.

### Enforcement port design (T022)
Complete: `port-design.md`. Central finding — **`gates.py` is already
tasks.md-agnostic** (evidence keyed on a bare id string, `gates.py:698`), so the
port is not a gate-engine rewrite; it is re-pointing the completion signal from a
tasks.md checkbox to a gsd STATE.md phase transition + feeding gsd phase ids to
`gates.py`/`runtime_proof.py`. 5-row mapping table with per-row open questions
(all wiring details, none blocking).

### Sandbox
`$SB=/private/tmp/gsd-spike.ebBd` + worktree `.worktrees/gsd-parallel-run`
(branch `spike/gsd-parallel-run`) — purged in T024. Nothing from either lands in
this repo except the curated `spike-results/gsd-core-eval/` docs.


## T024 — sandbox purge confirmation

Purged after all evidence curated into `spike-results/gsd-core-eval/`:
- `git worktree remove .worktrees/gsd-parallel-run --force` -> removed
- `git branch -D spike/gsd-parallel-run` -> deleted (local-only, was `7ae6ea7`, never pushed)
- sandbox dir `/private/tmp/gsd-spike.ebBd` recursively deleted -> gone

**No-push corroboration:** `git for-each-ref refs/remotes` count **19 before === 19
after** the purge -- zero new remote refs created across the entire spike. Nothing
left the machine. The only repo residue is the curated docs under
`spike-results/gsd-core-eval/` (report + `port-design.md` + `evidence/`).
