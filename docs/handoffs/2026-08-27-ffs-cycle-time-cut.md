# Handoff — FFS cycle-time cut (spec-less, operator-directed)

**Session:** `ffs-shortening` · **Date:** 2026-08-27
**Phase:** PR authored + verified → **merge and propagate**

## State

**PR #134**, branch `ffs-cycle-time-cut`, 18 commits, `mergeable=MERGEABLE`.
Working tree clean, branch pushed, nothing uncommitted.

All six **required** checks pass: pytest, bats, shellcheck,
host-shell-matrix (ubuntu + macos), Analyze Python.

`tamper` fails and is **not a required check**. One finding, deliberately
left for human adjudication: the old byte-pin's `assert r.returncode == 0`
in `tests/test_seam_wiring.py`, deleted because it asserted
`review-gate-command.sh` never changes and this PR changes it on purpose.
It is replaced in-place by a stronger live sha256 content pin. The
`tamper-reviewed` label downgrades enforcement — **do not self-apply it**;
that is the operator's call.

## Verification evidence

- pytest **1209 passed**, no skips (baseline parity).
- full local `bats tests/bats/*.bats` → **1297 pass / 2 fail**.
  Both failures reproduce on a worktree at unmodified `origin/main`:
  `FALLBACK-017` (installed-copy drift, environmental) and `WR-130`
  (macOS `/private/var` canonicalization). Neither appears in CI.
  See memory `local-bats-known-failures`.

## What this PR changes

One-round wall (deletes the unsatisfiable strictly-fewer-NEW predicate;
`PLAN_WALL_MAX_ROUNDS` 3→2; HIGH-only → PASS-RESIDUAL; CRITICAL blocks and
buys one repair round), CRITICAL-only auto-continue precondition, durable
cross-invocation plan-gate budget, size-aware ceremony tier +
`plan-wall.sh --run`, advisory plan-length gate, verifier single-run +
adjudicate, design-doc coverage ledger, typed `dispatch-budget` grant,
residual-channel splice guard, `run-finalizer --archive-planning`.
Full rationale in the PR body and its adjudication comment.

## Remaining work (operator-directed order)

1. **Merge PR #134** (main is PROTECTED — never direct-push; branch + PR only).
2. **Deploy / runtime restage** — see command block below.
3. `/git-branch-cleanup`
4. `/git-branch-consolidate`
5. **openclaw sync** — see pre-computed analysis below.
6. **Runtime restage** (same as 2 if not already done).

## openclaw sync — analysis already done, do not redo

Target: `/Users/luminamao/Documents/Github/openclaw`, main `d8f566927c`
(post-#1834). Sync **from FFS main after the merge**, never from the branch
and never from openclaw's own vendored tree (it is older than FFS main).

3-way merge results (FFS base `15e1c62` / openclaw main / FFS new), verified:

| file | vendored | root mirror |
|---|---|---|
| `gsd-run.sh` | CLEAN | CLEAN |
| `plan-length-gate.sh` | CLEAN | CLEAN |
| `review-gate-command.sh` | CLEAN | CLEAN |
| `run-finalizer.sh` | CLEAN | CLEAN |
| `plan-wall.sh` | 4 conflicts | 4 conflicts |

`gsd-run.sh` merges clean **around** openclaw's three lever forks landed in
`32ecf3fb83` (GSD_PROJECT phase resolution, run_state/gates.py
consumer-layout fallbacks, `GSD_CLAUDE_PERMISSION_MODE` knob) — none touch
`_gsd_run_wall_gate`. Preserve them.

**`plan-wall.sh` adjudication: take upstream bytes for all 4 conflicts.**
openclaw's fork (#1784 cluster) has a `PW_ANY_DISPATCH` "F1" guard and a
blocked/residual verdict helper that exist *solely* to patch the
diminishing-returns comparison this PR deletes. Its one piece of
independent value — the reviewer-prompt CRITICAL severity definition — is
already ported upstream (commit `d3dc996`). So its `fork-allowlist.txt`
entry can be dropped.

Mirror map (four locations, all confirmed to exist):
- `scripts/gsd/*.sh` → `packages/feature-fix-swarm/scripts/gsd/` **and** root `scripts/gsd/`
- `skills/<S>/SKILL.md` → `packages/feature-fix-swarm/skills/`, `.claude/skills/`, `.codex/skills/` (not all skills exist in all three)
- `docs/`, `CHANGELOG.md`, `tests/` → `packages/feature-fix-swarm/` **only**
  (root `CHANGELOG.md` and root `skills/` are openclaw's OWN namespace — do not touch)
- New file `scripts/gsd/seed-ceremony-tier.sh` is ABSENT downstream — add to vendored + root.

## Runtime restage — use the real installer, never hand-copies

`sync-drift-check` covers `scripts/gsd` **only, never `skills/`**, so skill
drift is invisible to it. A prior restage did hand-copies and left the
install manifest stale while doctor FAILed managed-path on every touched
skill. This PR touches six SKILL.md files, so it is exactly that case.

```bash
cd packages/feature-fix-swarm    # or the FFS repo root
GSD_ALLOW_SYMLINKED_DEST=1 bash setup.sh --scope user --adopt-collisions
bash setup.sh --doctor --scope user
```

Expect manifest / managed-path / legacy-codex-skills **PASS**.
`model-routing-catalog` WARN is known-expected (upstream gsd-core lacks
those model ids). Pre-change doctor baseline was `degraded` with **only**
that WARN, and `managed-path: all managed skill hashes and links match`
PASSING — so a post-restage managed-path FAIL means the restage was wrong.

Blast radius: the staged runtime at `~/.claude/skills` + `~/.agents/skills`
(52 managed paths, manifest at `~/.cache/feature-fix-swarm/install-manifest.json`)
is shared by **every** session on this box.

## Peer coordination (all cleared, do not re-ask)

- `tenant-readiness` — spec-381 finalized, PR #1834 MERGED. No live consumer.
- `dutch-translation` — spec-388 shipped; adhoc run worktree-pinned. No hold.
- `consolidate-branches-fix-autonomous-wall` — owns the live spec-006 drive
  (worktree-pinned, immune to main-side changes). Cleared the merge. Its 3
  spec-006 handoff-doc commits ride in #134 by its own explicit choice.
- `371-shared-tenant-blogs`, `all-aos-tasks`, `site-architecture-seo-geo` — clear.

Corrections accepted from peers and already applied: spec-371 removed from
the non-convergence evidence (it never ran a wall); probe reproductions
attributed to the reviewer, not the operating session.

## Traps

- `main` is protected. Branch + PR always.
- Never `git add -A`; scoped adds only.
- Do not write in another session's worktree or the shared openclaw checkout
  (it sits on branch `388-locale-contract-slices-0-1` with untracked files).
  Work in a fresh worktree off `origin/main`.
- After changing any version, default, or reviewer-prompt text: grep the
  whole test tree for the old literal and run the FULL bats suite. Two
  rounds of failures in this session came from hand-picking suites by name.
- The credential scanner flags 32+ char hex; the sanctioned form for a real
  digest literal is the `sha256:` prefix.

## Resume Prompt

```
Merge FFS PR #134 (all 6 required checks green; tamper is non-required and its one finding is yours to adjudicate — do not self-apply tamper-reviewed). Then restage the runtime with the real installer + doctor, run /git-branch-cleanup and /git-branch-consolidate, and execute the openclaw sync using the pre-computed adjudication in docs/handoffs/2026-08-27-ffs-cycle-time-cut.md (take upstream for plan-wall.sh's 4 conflicts, preserve openclaw's 3 gsd-run.sh lever forks, sync from FFS main not the branch).
```
