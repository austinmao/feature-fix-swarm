#!/usr/bin/env bash
# base-branch.sh — ONE resolver for the repo's default branch (borrowed:
# buildomator `gsd-tools base-branch`). Levers that hardcode `origin/main`
# silently diff against a nonexistent ref on `master`/`trunk` repos; every
# gsd lever resolves through this instead.
#
# Chain (offline-only, first hit wins):
#   1. $GSD_BASE_BRANCH                    explicit operator override
#   2. origin/HEAD symref                  what the remote calls default
#   3. local `main`, then local `master`   common defaults, main preferred
#   4. literal `main`                      last-resort (pre-first-commit repos)
#
# Deliberately NO `git remote show origin` leg: it is a network call, and this
# resolver runs inside pre-run walls — a hung network lookup would stall the
# run before phase 1 (same rationale as the model-probe timeout).
#
# Prints the bare branch name (no origin/ prefix). Always exits 0.
set -euo pipefail

if [ -n "${GSD_BASE_BRANCH:-}" ]; then
  echo "$GSD_BASE_BRANCH"
  exit 0
fi

sym="$(git symbolic-ref -q refs/remotes/origin/HEAD 2>/dev/null || true)"
if [ -n "$sym" ]; then
  echo "${sym#refs/remotes/origin/}"
  exit 0
fi

for b in main master; do
  if git show-ref -q --verify "refs/heads/$b" 2>/dev/null; then
    echo "$b"
    exit 0
  fi
done

echo main
