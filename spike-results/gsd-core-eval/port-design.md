# Enforcement port design — FFS machine-gates → gsd-core

> How FFS's completion-authority stack (`lib/gates.py`, the openclaw
> `checkbox-evidence-gate.sh` PreToolUse hook, `lib/runtime_proof.py`, the
> autonomy-grant ledger) re-homes onto `@opengsd/gsd-core@1.6.1`. Grounded in
> the Phase-0 spike (`report.md`): criterion (a) proved `workflow.test_command`
> is a real fail-closed external seam; (c) proved the executor's git discipline;
> (d) proved subagent env-fit under a trimmed MCP config.

## Central finding that makes the port cheap

**`gates.py` is already tasks.md-agnostic.** `run-gate`/`verify-done`/`check-grant`
key evidence on a bare **id string** in `GATES_STORE` (`.feature-fix-swarm/evidence.json`,
`gates.py:121` — `data.setdefault(task_id, {})`, bare-id keyed); it has zero coupling to `specs/**/tasks.md`. The ONLY thing that
binds FFS's completion authority to tasks.md checkboxes is the openclaw hook
`checkbox-evidence-gate.sh` (it intercepts `[ ]`→`[X]` flips and calls
`gates.py verify-done <Tnnn>`). So the port is not a rewrite of the gate engine —
it is **re-pointing the completion signal** from a tasks.md checkbox to a gsd
STATE.md phase transition, and feeding `gates.py`/`runtime_proof.py` gsd phase ids.

## Mapping table

| # | FFS source symbol | gsd-core target (@1.6.1) | Change | Open question |
|---|---|---|---|---|
| 1 | `gates.py run-gate/verify-done <Tnnn>` — evidence keyed by task id → `.feature-fix-swarm/evidence.json` (`gates.py:121` — `data.setdefault(task_id, {})`, bare-id keyed) | Same engine, keyed by **gsd phase/plan id** (`phase-01`, `01-01`) instead of `Tnnn` | **None to gates.py.** Callers pass gsd ids. Keep `GATES_STORE` in the gsd repo root. | Which gsd id granularity is the unit of completion — phase (`01`) or plan (`01-01`)? Plan-level is finer but gsd auto-commits per plan; phase-level matches the `workflow.test_command` firing point. Recommend **phase-level** (that's where the gate fires — criterion a). |
| 2 | `checkbox-evidence-gate.sh` PreToolUse hook — blocks `[ ]`→`[X]` in `specs/**/tasks.md` unless `verify-done` passes | Re-key to **gsd STATE.md phase-complete transition**: block the write that flips `status: executing`→(complete) or increments `progress.completed_phases` unless `gates.py verify-done <phase-id>` passes | Rewrite the hook's trigger: match a Write/Edit to `.planning/STATE.md` whose diff advances `completed_phases`, extract the phase id, gate on it. Same fail-closed exit-2 contract. | gsd writes STATE.md many times per phase (progress bumps). The hook must fire ONLY on the completion-advancing write, not every progress tick — key on the `completed_phases` delta, not any STATE.md touch. Also: gsd runs the executor in a **worktree it merges back** (criterion c, commit `a5c60e9`) — the hook must see the merge-target STATE.md, not the worktree copy. |
| 3 | `runtime_proof.py verify <proof.json>` — exit 0 iff browser/runtime proof OK (`runtime_proof.py:262`) | gsd `workflow.test_command` = `python3 lib/runtime_proof.py verify .planning/phases/<phase>/proof.json` | Set the config key. **Proven seam** — criterion (a) showed a non-zero `test_command` fail-closes phase advancement, exit 0 proceeds. | gsd calls `test_command` with what cwd / phase context? Need to confirm gsd substitutes the phase dir or passes it via env, else the proof path can't be phase-parameterized. If gsd gives no phase interpolation, wrap in a `gsd-test-command.sh` that reads `current_phase` from STATE.md and builds the path. |
| 4 | `gates.py grant`/`check-grant`/`pending` — typed, TTL'd autonomy ledger in `GATES_STORE` | Insert `check-grant <run-id> --action <typed>` **before the outward action in `/gsd-ship`** (push/PR/tag); on non-zero → `pending` + abort ship, leave phase built-not-shipped | **None to gates.py** (reused verbatim per plan). Wire the call into gsd's ship command — either gsd's `code_review_command`/hook slot, or a `gsd-ship` wrapper. | Does gsd-core expose a pre-ship hook, or must the ship stage be wrapped externally? @1.6.1 config has `ship.pr_body_sections` + `hooks.context_warnings` but no visible pre-ship command slot — likely needs an external `/gsd-ship`-wrapping runner that calls `check-grant` then delegates. Run-id = gsd milestone (`v1.0`). |
| 5 | Host-session MCP surface (env-fit, criterion d) | Any FFS→gsd runner MUST launch gsd drives with `--strict-mcp-config --mcp-config '{"mcpServers":{}}'` | New: the runner script hard-codes the trimmed MCP flag. | Non-negotiable on this machine — the full user MCP config overflowed the 200k window at the top level (`env-fit.txt`). Runner must own this flag; document it as a hard requirement, not an option. |

## What does NOT need to change

- **`gates.py` engine** — unchanged; called with gsd ids. Its `GATES_STRICT=1`
  runner-executed-evidence discipline transfers verbatim.
- **`runtime_proof.py`** — unchanged; it already `verify`s a `proof.json` and
  exits 0/1, which is exactly what `workflow.test_command` consumes.
- **Grant ledger semantics** — reused verbatim (typed actions, TTL, `pending`
  on miss). Only the *insertion point* (gsd ship stage) is new.

## Net port surface

Three artifacts change, two are net-new, the engine is untouched:

1. **Rewrite** `checkbox-evidence-gate.sh` → `gsd-state-gate.sh` (trigger on
   STATE.md `completed_phases` delta instead of tasks.md checkbox). *(table #2)*
2. **Config** `.planning/config.json` `workflow.test_command` →
   `runtime_proof.py verify`. *(table #3, proven seam)*
3. **New** `gsd-run.sh` — the FFS→gsd driver: trimmed MCP config (#5), passes
   gsd phase ids to `gates.py run-gate`, wraps `/gsd-ship` with `check-grant` (#4).
4. **New** `gsd-test-command.sh` (only if #3's open question resolves "no phase
   interpolation") — resolves the current phase's `proof.json` path from STATE.md.

## Risk carried from Phase 0 into the port

- **STATE.md bookkeeping inconsistency** (report §a caveat: frontmatter
  `percent:100` vs body `0%` under a blocked phase). Gate #2 keys on
  `completed_phases`, which was *correct* under the block, so the port is not
  fooled — but the port should assert on `completed_phases`/`status`, never the
  cosmetic `percent`.
- **Criterion (b) still unproven live** — the port assumes per-agent model
  routing works (needed to keep haiku/sonnet/opus cost ladder). Mechanism is in
  source but the observed matrix is pending a quota-reset re-run. If (b) comes
  back IGNORED, the port loses cost-tiering (functional, but every gsd agent runs
  one model) — a cost regression, not a correctness one.
