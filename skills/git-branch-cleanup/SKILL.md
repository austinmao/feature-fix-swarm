---
name: git-branch-cleanup
description: "Triage branches against the base branch, merge only CI-green non-gated ones, prune merged refs + worktrees. Squash-merge + operator-gate aware."
version: "1.1.0"
user-invocable: true
---

# Git Branch Cleanup

## Host dispatch contract

- Codex: `$skill`, Codex collaboration roles, and GPT-6 tiers.
- Claude: `/skill`, Agent/Skill tools, and Claude aliases.
- A bare `/skill` in this shared source denotes the Claude form; Codex dispatches the same named skill as `$skill`.

Reconcile local branches with the base branch. Most "unmerged" branches are already
squash-merged — this is mostly pruning, not merging. **Triage first, act second.**
Never blind-merge: open PRs are operator-gated and CI can be red even when
conflict-free.

Pairs with `/git-branch-consolidate`, which audits the whole estate for
spec/plan/test completeness and hands its `delete-safe` set here. This skill owns
the deletion approval flow; consolidate owns the audit.

## Host, storage, and repository identity

- Discover the executing host and checkout from the current directory. A MacBook
  Pro client connected to a Mac mini executes on the mini; report both when known.
  Never infer a hostname, username, drive label, or checkout from remembered paths.
- Resolve `git rev-parse --show-toplevel` and
  `git rev-parse --path-format=absolute --git-common-dir` through `realpath`.
  Identify a local lineage by executing host plus physical common directory and
  its device/inode. Deduplicate compatibility aliases against that identity.
- Enumerate paths with `git worktree list --porcelain -z` and a NUL-aware parser;
  never split human-readable output with whitespace, grep, or awk. Preserve lock
  and prunable markers. Missing/unmounted storage, broken aliases, unreadable
  metadata, or an ownership mismatch are **blocked**, never empty/clean estates.
- Linked worktrees share one lineage; independent primary and preserved rolling
  clones remain separate estates even when their origins or branch names match.
  Discover additional roots only from explicit caller paths, trusted local
  migration records, or registry pointers, then validate them. Audit each lineage
  separately and include host/common-directory identity in every proposed action.
- The current host is the default scope. A laptop request does not authorize SSH
  to a desktop, or the reverse. Remote execution requires an explicitly targeted
  host; otherwise report that host as uninspected. Never start automatic SSH.
- Migration/recovery archives, session histories, protected recovery refs, and
  stopped/resumable session work are retained. A stopped PID, old commit date,
  absent remote ref, detached HEAD, or compatibility alias is not deletion proof.

## Golden rules

- **Triage before mutation.** Pin the local tip and base, then prove ancestry,
  exact content equivalence (including docs/plans), or an exact merged PR whose
  head matches this tip and whose base matches the selected base. An old merged
  PR for a reused branch and an upstream marked `[gone]` prove nothing about the
  current tip. `git cherry BASE BRANCH`: `-` means patch-equivalent in base;
  `+` means unique to the branch. Patch equivalence is review evidence, not a
  worktree-deletion authorization.
- **Conflict-free does not prove readiness.** Require pinned green CI and review.
  Open PR merges, pushes, and local/remote deletions need their applicable explicit
  authorization or valid typed grant. Running this skill grants no blanket push.
- **Protect work.** Retain the primary checkout, the current checkout, base refs,
  locked/prunable/unreadable worktrees, active writers, open cwd/file handles, and
  session-reserved branches/worktrees, including stopped sessions awaiting resume.
  Check actual processes and session metadata without reading transcript bodies.
- **Inspect all payload.** Staged/unstaged changes, untracked files, ignored user
  files, environment files, nested repositories, and local history block removal.
  `git status` alone omits ignored payload. Count with NUL-aware
  `git ls-files --others --ignored --exclude-standard -z`; never print secrets.
  Known disposable caches still need explicit identification/approval. If unclear,
  retain the whole worktree; never auto-stash, reset, clean, or discard files.

## Workflow

### 1. Triage — gather without mutation

Use `/git-branch-consolidate` and its sibling `scripts/collect-estate.py` from the
actual installed skill location. Collect each validated physical lineage once.
Use the available local base and disclose its age; a fetch is a separate,
explicitly authorized refresh. Do not run `git fetch --prune` during read-only
triage. Record branch tip, base tip, PR/head/base identity, worktree ownership,
locked/prunable flags, dirty/untracked/ignored counts, and session/process holds.

### 2. Categorize

- **Landed candidate:** content proof exists for the pinned current tip. It is
  cleanup-eligible only after the separate payload, activity, identity, and
  authorization checks pass. `[gone]` alone never enters this class.
- **Open PR:** retain pending pinned green checks, review, and merge authorization.
- **Unique, ambiguous, closed-unmerged, query failure, or unavailable storage:**
  retain and report the next review/recovery action. Preserve unique specs/docs.

### 3. Remove only approved, verified candidates

Give the operator or grant checker concrete host, common directory, worktree path,
branch ref, expected OID, evidence, and payload/activity checks. Re-read these
immediately before each effect; refuse drift. Preserve recoverable refs and
approved backup evidence before any sanctioned squash-merge finalization.

Use ordinary `git worktree remove "<exact path>"` only after every check passes;
never `--force`, `rm -rf`, or a bulk `[gone]` loop. Then use `git branch -d` for
ancestry-merged refs. If Git refuses, retain and report. Squash-merged branch
finalization delegates to the repository's grant-gated `run-finalizer.sh` only
when its exact merged-head proof and payload/activity protections are verified;
this skill never supplies an ad-hoc `git branch -D` fallback. Detached worktrees
need the same payload/activity checks as named branches.

**Remote branch deletion safety.** Classify each exact current remote tip as
`MERGED_CLEAN`, `OPEN`, `CLOSED_UNMERGED`, `NO_PR`, `AMBIGUOUS`, or `ERROR`.
`MERGED_CLEAN` requires exactly one qualifying merged PR with matching current
head OID and selected base; query errors or reused names retain the ref.
Immediately before an approved deletion, re-read the full remote ref with
`git ls-remote origin refs/heads/<branch>`, verify its OID against the snapshot
and PR, fetch that OID for recovery, and use compare-and-delete:

```bash
git push --force-with-lease=refs/heads/<branch>:<expected-oid> origin :refs/heads/<branch>
```

A failed lease retains the ref. Missing remote refs do not authorize local removal.

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
# Update only an approved, clean, non-session-reserved branch; never autostash unrelated work.
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

### 5. Report and separately authorized pushes

Report each lineage independently: proposed and completed actions, pinned OIDs,
retained session work/payload, blocked storage, and held PRs with failing checks.
Do not push the base branch merely because it is ahead. Push only under explicit
session authorization or the exact valid grant, after its required checks.

## Parallelism

Read-only triage is faster as one `gh`/`git` batch than as orchestrated agents.
Reserve parallel sub-agents for the **execute** phase only — independent rebase +
test-per-branch across several green PRs. Pin a model on every spawn.
