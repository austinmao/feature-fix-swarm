---
name: git-branch-consolidate
description: "Audit every git worktree and branch for spec/plan/test completeness, then plan the consolidation down to one origin/main. Use for 'audit all worktrees', 'what's unmerged', 'consolidate branches', 'get everything to main', 'branch estate'."
version: "1.0.0"
user-invocable: true
---

# Git Branch Consolidate

## Host dispatch contract

- Codex: `$skill`, Codex collaboration roles, and GPT-5.6 tiers.
- Claude: `/skill`, Agent/Skill tools, and Claude aliases.
- A bare `/skill` in this shared source denotes the Claude form; Codex dispatches the same named skill as `$skill`.

Answers two questions with evidence, never recall:

1. **What is actually unfinished?** Per branch: does its content already live in
   the base branch, what does it still owe, does it have tests, and is its
   spec/plan/tasks trail complete.
2. **What is the shortest ordered path to one `origin/main`?** Merge set, cleanup
   set, and the testing gaps that must close first.

Read-only toward every branch. Never checks out, fetches, merges, or deletes —
it emits the commands and stops. Execution of the deletion set belongs to
`/git-branch-cleanup`, which owns the approval flow.

## Step 1 — Collect (deterministic, ~30s)

```bash
python3 skills/git-branch-consolidate/scripts/collect-estate.py \
  --repo "$(git rev-parse --show-toplevel)" --base main \
  > "${TMPDIR:-/tmp}/estate-$(date -u +%Y%m%d-%H%M).json"
```

Per branch: `landed` · `residual_files` / `residual_code` · `src_files` /
`test_files` / `test_gap` · `spec` artifacts + `tasks.md` counts · `.planning`
ROADMAP+STATE · worktree dirt · PR state · `disposition`.

**`landed` is the load-bearing field.** `git merge-base --is-ancestor` alone
reports every squash-merged branch as unmerged — on a repo with a squash-merge
convention this yields dozens of false positives. `landed` is true when ancestry
OR a merged PR OR zero residual code says the base branch already carries the
content. Trust `landed`, not `ahead`.

`--no-gh` skips the PR lookup (offline / rate-limited). Dispositions then lose
`merge-ready` and `stale-abandoned` — say so in the report.

## Step 2 — Fan out (every spawn model-pinned; contracts per `docs/model-tiers.md`)

| Agent | Tier | Contract | Job |
|---|---|---|---|
| spec-trail scout | volume | scout ≤15 | For each `review-then-land` spec: spec.md/plan.md/tasks.md present? open checkboxes? evidence dir populated? |
| testing analyst | execution | build ≤20 | Every `test_gap` branch + every branch whose residual touches an API surface, auth, access-control, or schema migrations. Name the missing suite, not "add tests". |
| runner analyst | execution | build ≤20 | Worktrees with a live `.planning/run-state/gsd-run.pid` or `status != complete`: progressing / stopped-at-gate / stalled / dead. |
| conflict scout | volume | scout ≤15 | For the proposed merge set only: which branches touch the same files (collision map + suggested order). |
| risk assessor | judgment | deep ≤40 | Conclusion first: what blocks consolidation, what only the operator can decide, the ordered next 3. |

Synthesis reconciles contradictions and names which report won. Producer ≠
reviewer: the synthesizer never re-runs a scout's search.

Run `/spec-status NNN` only for specs the collector flags as **not landed with an
open tasks trail** — it is expensive and most specs are already closed.

## Step 3 — Report

Write `.planning/ESTATE-<UTC yyyymmdd-HHMM>.md`:

- **Headline** — N branches, N landed, N owing work, N testing gaps.
- **Merge set** — ordered, with the reason each is safe to land and its gate command.
- **Testing gaps** — branch → the specific suite missing → the command that would prove it.
- **Cleanup set** — `delete-safe` branches + their worktrees, with the exact
  `git worktree remove` / `git branch -d` lines. Never `-D`.
- **Blocked / operator-reserved** — each with its one-command unblock.
- **Next 3** — from the risk assessor.

Print headline + merge set + Next 3 inline; the file carries the rest.

## Step 4 — Execute (only on explicit approval, one step at a time)

Order matters: **land before cleanup**, or you delete the only copy.

1. Merge the `merge-ready` set (grant-gated; `/review-gate` first on
   auth/access-control/payments/infra diffs).
2. Re-run Step 1. `landed` must have grown by exactly the merged count.
3. Hand the refreshed `delete-safe` set to `/git-branch-cleanup` — it owns the
   deletion approval flow. Remove worktrees before their branches.
4. Detached-HEAD build worktrees whose `head` is an ancestor of the base branch
   are pure scratch: `git worktree remove`, no branch to delete.

## Constraints

- READ-ONLY until Step 4, which requires explicit per-step approval.
- Never `git add -A`; never `-D`; never delete an unmerged branch or one checked
  out in a worktree; never delete a worktree with uncommitted changes without
  showing the diff first.
- A dirty worktree is a finding, not an obstacle — report the file count, and
  route stalled work to `/adopt-wip` rather than discarding it.
- Secrets: filenames only, never content.
