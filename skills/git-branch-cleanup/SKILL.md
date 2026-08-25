---
name: git-branch-cleanup
description: "Triage branches against the base branch, merge only CI-green non-gated ones, prune merged refs + worktrees. Squash-merge + operator-gate aware."
version: "1.0.0"
user-invocable: true
---

# Git Branch Cleanup

## Host dispatch contract

- Codex: `$skill`, Codex collaboration roles, and GPT-5.6 tiers.
- Claude: `/skill`, Agent/Skill tools, and Claude aliases.
- A bare `/skill` in this shared source denotes the Claude form; Codex dispatches the same named skill as `$skill`.

Reconcile local branches with the base branch. Most "unmerged" branches are already
squash-merged — this is mostly pruning, not merging. **Triage first, act second.**
Never blind-merge: open PRs are operator-gated and CI can be red even when
conflict-free.

Pairs with `/git-branch-consolidate`, which audits the whole estate for
spec/plan/test completeness and hands its `delete-safe` set here. This skill owns
the deletion approval flow; consolidate owns the audit.

## Golden rules

- **Assess before acting.** PR state is authoritative, not `git branch --merged`
  (misses squash-merges).
- **Conflict-free != ready.** A clean merge-tree probe says nothing about CI. Check both.
- **Open PR = operator decision.** Never auto-merge an open PR without explicit go.
  High-blast-radius changes (infra, multi-tenant, auth, row-level security, scheduled
  jobs) go through `/review-gate` first.
- **Worktree before branch.** A branch with a worktree cannot be deleted; remove the
  worktree first.
- **Never touch:** the base branch (`main`/`master`/`develop`), the current checkout, or
  any branch whose worktree was committed to in the last hour (active work).
- **Preserve dirty state.** Uncommitted work in the main checkout is unrelated to
  cleanup — leave it.

## Workflow

### 1. Triage — gather, don't mutate

```bash
git fetch --prune
BASE="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||')"
BASE="${BASE:-main}"

# Unmerged-locally branches (the candidate set)
git branch --no-merged "$BASE"

# Per branch: PR state (authoritative) + ahead + conflict probe
for b in <branches>; do
  pr=$(gh pr list --state all --head "$b" --json number,state,isDraft \
        --jq '.[0] | "PR#\(.number) \(.state) draft=\(.isDraft)"' 2>/dev/null)
  ahead=$(git rev-list --count "origin/$BASE..$b")
  conflicts=$(git merge-tree --write-tree "origin/$BASE" "$b" 2>/dev/null | grep -c '^CONFLICT')
  printf '%-34s %-28s ahead=%-3s conflicts=%s\n' "$b" "${pr:-<no PR>}" "$ahead" "$conflicts"
done

# [gone] = remote deleted -> squash-merged, safe local delete
git for-each-ref --format='%(refname:short) %(upstream:track)' refs/heads/ | awk '$2=="[gone]"{print $1}'

# No-PR branches: cherry-equiv (- = unique to branch, + = already in base)
git cherry "origin/$BASE" <branch>
```

### 2. Categorize

- **A — Merged** (`PR MERGED` / `[gone]` / `git cherry` all `+`): prune. No merge needed.
- **B — Open PR**: operator-gated. Check CI (step 4). Merge only on explicit go + green.
- **C — No PR**: if `ahead>0` and commits are unique+wanted -> push/PR; if superseded
  dupes -> delete; if worktree touched <1h ago -> **active, leave**.

### 3. Prune merged (Category A)

```bash
# Worktree first (a branch with a worktree can't be deleted)
git worktree remove --force <path>     # only if path != repo root
git branch -D <branch>                 # -D: squash-merged branches aren't --merged
```

Bulk `[gone]` prune (worktree-aware):

```bash
git for-each-ref --format='%(refname:short) %(upstream:track)' refs/heads/ \
  | awk '$2=="[gone]"{print $1}' \
  | while IFS= read -r b; do
      wt=$(git worktree list | grep "\[$b\]" | awk '{print $1}')
      [ -n "$wt" ] && [ "$wt" != "$(git rev-parse --show-toplevel)" ] && git worktree remove --force "$wt"
      git branch -D "$b"
    done
```

**Branch deletion safety.** Every remote branch gets exactly one of six classes:
`MERGED_CLEAN` (exactly ONE qualifying merged PR whose `headRefOid` == the
current remote tip) / `OPEN` / `CLOSED_UNMERGED` / `NO_PR` / `AMBIGUOUS`
(multiple PRs, reused branch, or post-merge commits) / `ERROR` (query failed).
Only `MERGED_CLEAN` is deletable — every other class is retained.

- **Fail-closed:** any `gh` query that exits non-zero or returns empty/malformed
  output classifies the branch `ERROR` — retain; never default to `NO_PR`.
- **Pre-delete re-read, per branch:** `git ls-remote origin <branch>` — the SHA
  must still equal both the snapshot value and the merged PR's `headRefOid` —
  then `git fetch origin <sha>` so the object stays locally recoverable.
- **Compare-and-delete:**
  ```bash
  git push --force-with-lease=refs/heads/<branch>:<sha> origin :refs/heads/<branch>
  ```
  the push fails if the ref moved after the re-read, closing the
  read-then-delete race. This is the only sanctioned remote-branch deletion —
  a bare `git push origin --delete <branch>` has no such guard and must not be used.

### 4. CI gate before merging any open PR (Category B)

```bash
gh pr view <n> --json mergeable,mergeStateStatus,statusCheckRollup \
  --jq '{mergeable, state:.mergeStateStatus,
         red:[.statusCheckRollup[]?|select((.conclusion//.state)|IN("FAILURE","ERROR","CANCELLED"))|(.name//.context)]}'
```

Classify the `red` list against the repo's own check inventory:

- **Benign noise — ignore:** checks that are red for reasons unrelated to the diff,
  such as a quota-exhausted free-tier scanner or a deploy check pointing at a
  retired environment. A PR red *only* on these is effectively green. Each carve-out
  must be named and justified in the report; an unexplained red check is never benign.
- **Real failure — BLOCK merge:** unit/integration suites, type and build checks,
  schema/migration checks, and any repo-specific guard. Report as needs-fix. Do **not** merge.

Merge a cleared PR (match the repo's merge convention — squash shown):

```bash
git fetch origin && git -c rebase.autostash=true rebase "origin/$BASE" <branch>   # or update via gh
# run tests for the branch's surface before merge
HEAD_OID=$(gh pr view <n> --json headRefOid -q .headRefOid)
gh pr merge <n> --squash --match-head-commit "$HEAD_OID"
```

**Merge protocol (pinned).** For any merge that needs an auditable, race-free
guarantee, follow this in order — it supersedes the ad-hoc snippet above:

1. **Pin the head:** `HEAD_OID=$(gh pr view N --json headRefOid -q .headRefOid)`.
2. **CI green ON that head:** `gh pr checks N` exits 0 AND `statusCheckRollup`
   is NON-EMPTY — an empty rollup is a FAIL, not a pass — and `mergeable` is
   not `CONFLICTING`. This pinned protocol supersedes the benign-noise
   classification above: for a pinned merge, `gh pr checks N` must exit 0 with
   no carve-outs.
3. **Review evidence exists** for that head (a recorded `/review-gate` run).
4. **Merge pinned:** `gh pr merge N --squash --match-head-commit "$HEAD_OID"` —
   server-side abort if the head moved (TOCTOU guard).
5. **Bounded merged-poll:** `gh pr view N --json state,mergeCommit`, at most
   10 tries x 30s, until `state=MERGED` with non-null `mergeCommit`; on
   timeout STOP as pending — never blindly retry the merge.
6. **Backstop:** `scripts/gsd/assert-merged.sh N` exits 0.
7. **Evidence:** `gates.py run-gate merge-pr-N -- bash scripts/gsd/assert-merged.sh N`
   — the CLI form is `run-gate <evidence-id> -- <command>`; there is NO `--gate`
   flag. Record `headRefOid`, `mergeCommit`, CI rollup summary, review artifact
   ref, and timestamp alongside in the ledger note.
8. **Gate-form note:** normative gates use value-comparison form, e.g.
   `[ "$(gh pr view N --json state -q .state)" = "MERGED" ]` — NEVER pipe `gh`
   output through `tail`/`grep` in a gate (the pipe exit-status trap yields false-green).
9. **Deployment-touching merge → post-merge smoke:** verify the deployed surface
   answers as expected before calling the merge done, e.g.
   `[ "$(curl -sL -o /dev/null -w '%{http_code}' "$DEPLOY_URL")" = "200" ]`.
   Follow redirects — an apex that 30x's to a canonical host is still healthy.

High-blast-radius changes (infra, multi-tenant, auth, row-level security, scheduled
jobs) run `/review-gate` before merge regardless of CI color.

### 5. Push the base branch + report

```bash
git rev-list --count "origin/$BASE..$BASE"   # if >0, push the clean local commits
git push origin "$BASE"
```

Report: pruned branches/worktrees, merged PRs, **held** PRs with the real failing
check named, branches needing operator disposition, and any preserved dirty state.

## Parallelism

Read-only triage is faster as one `gh`/`git` batch than as orchestrated agents.
Reserve parallel sub-agents for the **execute** phase only — independent rebase +
test-per-branch across several green PRs. Pin a model on every spawn.
