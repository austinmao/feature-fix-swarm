---
name: adopt-wip
description: "Salvage coherent uncommitted work left in a stalled worktree or abandoned agent session. Use when resuming a run, when a parallel worktree contains valuable WIP, or when deciding whether to adopt, finish, and ship existing work instead of rebuilding from scratch."
version: "1.0.0"
---

# /adopt-wip

Adopt stalled work only after confirming it is not active, and give the
adopted diff NO gate discount.

Ported from the fable-agent-orchestration `orphaned-wip-adopter` skill
(Apache-2.0).

## When to run

- `/feature-implement --resume` finds a worktree with uncommitted changes for
  a task it is about to rebuild.
- A parallel spec worktree under `.claude/worktrees/` went quiet mid-run.
- An agent session died (context exhaustion, kill, crash) leaving a dirty
  tree.

## Procedure

1. **Confirm the worktree is stale — never touch active work:**
   - file mtimes (`find <worktree> -newer <ref> -type f` — anything touched in
     the last few minutes suggests a live session);
   - running processes / session status where observable;
   - when in doubt, WAIT.
2. **Read the uncommitted diff** (`git -C <worktree> diff` + `status`). Whole
   thing, not a skim.
3. **Assess coherence:**
   - does it compile / plausibly compile?
   - does it use existing APIs correctly (spot-check 2-3 call sites against
     current signatures)?
   - is the design sound, or a dead-end the session was already backing out
     of?
4. **Exercise the load-bearing claim** — run the one test/command that shows
   whether the WIP's core change works.
5. **Decide:**
   - **Adopt** when coherent and faster than rebuilding: rebase or replay
     onto current main, fill gaps, add the fail-under-broken (RED) proof, run
     the FULL normal gate chain (`gates.py run-red` / `run-gate`,
     review-gate). Adopted code earns zero trust — it skipped no gates, it
     merely skipped retyping.
   - **Rebuild** when incoherent or unsalvageable — delete-or-stash the WIP
     with a one-line note so the next reader doesn't re-litigate it.
   - **Wait** when the session may still be active.
6. **Credit the adoption** in the PR/commit body ("adopts WIP from
   <worktree/branch>") so history explains where the diff came from.

## Anti-patterns

- Adopting a diff because it's big ("so much work, must be valuable") —
  coherence and correctness decide, not volume.
- Skipping the RED proof because "the WIP already had tests" — verify those
  tests fail under broken behavior before trusting them.
- Merging adopted work without review-gate because the original task "already
  passed review" — that review saw different code.
