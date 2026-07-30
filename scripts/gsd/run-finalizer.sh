#!/usr/bin/env bash
# run-finalizer.sh — run-end estate cleanup after a verified merge.
#
# The 2026-07-30 estate audit found 95 branches (~30 orphaned gsd/phase-*)
# and 38 worktrees (26 dirty) because nothing runs at run end: the finish
# tail stopped at merge. This lever closes the loop — call it from the
# finish tail AFTER assert-merged.sh exits 0.
#
# Safety model:
# - Destructive steps run ONLY under machine proof: gh reports the PR
#   MERGED, and a branch is deleted only when `git branch -d` accepts it
#   OR its tip is (an ancestor of) the merged PR's headRefOid — i.e. the
#   exact content GitHub recorded as landed (squash merges make -d refuse
#   even for landed branches; the headRefOid check is the squash-safe
#   equivalent of -d, never a blind -D).
# - A branch whose tip moved past the merged head is NEVER deleted.
# - A dirty worktree is NEVER removed — it is routed to /adopt-wip.
# - Run-state clearing touches three fixed files only; a denylist refuses
#   anything under .feature-fix-swarm/ or named evidence.json (the gates
#   ledger).
# - Fail-soft: every step warns and continues; ALWAYS exits 0. A cleanup
#   failure must never block, un-merge, or fail a finished run.
#
# Usage: run-finalizer.sh [--dry-run] <pr-number> [<owner/repo>]
# Kill-switch: FFS_RUN_FINALIZER=off
# Exit: always 0.
set -uo pipefail

note() { echo "[run-finalizer] $*"; }
warn() { echo "[run-finalizer] WARN: $*" >&2; }

# Flags are position-free and junk is refused loudly: a positional-only
# --dry-run turned a stray arg into `--repo --dry-run`, which failed gh and
# skipped cleanup SILENTLY (observed on the first live run, PR #62).
DRY=0
PR=""
REPO=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    -*) warn "unknown argument '$arg' — skipping"; exit 0 ;;
    *) if [ -z "$PR" ]; then PR="$arg"
       elif [ -z "$REPO" ]; then REPO="$arg"
       else warn "unexpected extra argument '$arg' — skipping"; exit 0; fi ;;
  esac
done
REPO_ARGS=()
[ -n "$REPO" ] && REPO_ARGS=(--repo "$REPO")

if [ "${FFS_RUN_FINALIZER:-on}" = "off" ]; then
  note "disabled via FFS_RUN_FINALIZER=off — skipping"
  exit 0
fi
if [ -z "$PR" ]; then
  warn "usage: run-finalizer.sh [--dry-run] <pr-number> [<owner/repo>] — skipping"
  exit 0
fi

run() { # execute a step, or print it under --dry-run; warn-and-continue on failure
  if [ "$DRY" -eq 1 ]; then note "DRY: $*"; return 0; fi
  "$@" || { warn "step failed (continuing): $*"; return 1; }
}

deny() { # refuse denylisted paths (gates evidence ledger)
  case "$1" in
    *".feature-fix-swarm"*|*"evidence.json"*)
      warn "denylisted path '$1' — refusing to touch"; return 1 ;;
  esac
  return 0
}

INFO="$(gh pr view "$PR" ${REPO_ARGS[@]+"${REPO_ARGS[@]}"} \
  --json state,headRefName,headRefOid \
  --jq '.state + " " + .headRefName + " " + .headRefOid' 2>/dev/null)" || {
  warn "gh pr view $PR failed — no merge proof, doing nothing"
  exit 0
}
read -r STATE BRANCH HEAD_OID <<<"$INFO"
if [ "$STATE" != "MERGED" ] || [ -z "${BRANCH:-}" ] || [ -z "${HEAD_OID:-}" ]; then
  warn "PR #$PR state='$STATE' (need MERGED + head ref) — doing nothing"
  exit 0
fi
note "PR #$PR MERGED — finalizing branch '$BRANCH' (merged head $HEAD_OID)"

remove_worktree_for_branch() { # remove the worktree checked out on $1 iff clean
  local br="$1" wt
  wt="$(git worktree list --porcelain \
        | awk -v b="refs/heads/$br" '$1=="worktree"{w=$2} $1=="branch"&&$2==b{print w}')"
  [ -n "$wt" ] || return 0
  if [ -n "$(git -C "$wt" status --porcelain 2>/dev/null)" ]; then
    warn "worktree $wt on '$br' is DIRTY — keeping it; route to /adopt-wip"
    return 1
  fi
  run git worktree remove "$wt"
}

delete_landed_branch() { # delete $1 only under landed-tip proof
  local br="$1" tip
  git show-ref --verify -q "refs/heads/$br" || return 0
  if [ "$DRY" -eq 1 ]; then note "DRY: delete local branch $br"; return 0; fi
  if git branch -d "$br" >/dev/null 2>&1; then
    note "deleted local branch '$br' (-d: merged into HEAD)"
    return 0
  fi
  tip="$(git rev-parse "refs/heads/$br" 2>/dev/null)" || return 1
  if [ "$tip" = "$HEAD_OID" ] \
     || git merge-base --is-ancestor "$tip" "$HEAD_OID" 2>/dev/null; then
    if git branch -D "$br" >/dev/null 2>&1; then
      note "deleted local branch '$br' (tip $tip landed as merged PR head — squash-safe proof)"
    else
      warn "could not delete '$br' (checked out somewhere?)"
    fi
  else
    warn "branch '$br' tip $tip is not the merged PR head ($HEAD_OID) — NOT deleting (unmerged work?)"
  fi
}

# 1. worktree first (git refuses to delete a branch checked out in any worktree)
remove_worktree_for_branch "$BRANCH" || true
# 2. local + remote feature branch
delete_landed_branch "$BRANCH"
# `gh pr merge --delete-branch` usually got here first — only push a delete if
# the remote ref still exists, else every finish tail ends on a bogus WARN.
if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  run git push origin --delete "$BRANCH"
else
  note "remote branch '$BRANCH' already gone — nothing to delete"
fi
# 3. gsd/phase-* intermediates: prune only ancestors of the merged head
while IFS= read -r pb; do
  [ -n "$pb" ] || continue
  if git merge-base --is-ancestor "$pb" "$HEAD_OID" 2>/dev/null; then
    remove_worktree_for_branch "$pb" || continue
    delete_landed_branch "$pb"
  else
    note "keeping gsd branch '$pb' — not an ancestor of the merged head (open work?)"
  fi
done < <(git for-each-ref --format='%(refname:short)' 'refs/heads/gsd/phase-*')
# 4. run-state: three fixed files, denylist-guarded
for f in .planning/run-state/gsd-run.heartbeat \
         .planning/run-state/gsd-run.status \
         .planning/run-state/gsd-run.pid; do
  deny "$f" || continue
  [ -e "$f" ] || continue
  run rm -f "$f"
done
# 5. stale worktree metadata
run git worktree prune

note "finalize complete (fail-soft: any WARN above needs manual attention)"
exit 0
