#!/usr/bin/env bats
# run-finalizer.sh — run-end estate cleanup after a verified merge.
# Fixtures: real git repos in $BATS_TEST_TMPDIR (local bare origin), gh mocked.

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  LEVER="$REPO_ROOT/scripts/gsd/run-finalizer.sh"
  MOCK_BIN="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$MOCK_BIN"
  export PATH="$MOCK_BIN:$PATH"
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
  export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t \
         GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
  unset FFS_RUN_FINALIZER || true

  # repo with a bare origin, main + squash-merged feature branch
  ORIGIN="$BATS_TEST_TMPDIR/origin.git"
  WORK="$BATS_TEST_TMPDIR/work"
  git init -q --bare "$ORIGIN"
  git init -q -b main "$WORK"
  cd "$WORK"
  git remote add origin "$ORIGIN"
  echo a > a.txt; git add a.txt; git commit -qm "base"
  git push -q origin main
  git checkout -qb feat/x
  echo b > b.txt; git add b.txt; git commit -qm "feat work"
  FEAT_OID="$(git rev-parse feat/x)"
  git push -q origin feat/x
  git checkout -q main
  # simulate squash-merge: same content lands as a NEW commit on main
  git cherry-pick -qn feat/x && git commit -qm "feat: x (#1) [squash]"
  git push -q origin main
  mkdir -p .planning/run-state
  echo pid > .planning/run-state/gsd-run.pid
  echo hb  > .planning/run-state/gsd-run.heartbeat
  echo st  > .planning/run-state/gsd-run.status
  mkdir -p .feature-fix-swarm
  echo '{"k":"v"}' > .feature-fix-swarm/evidence.json
}

mock_gh_merged() {
  # gh pr view -> MERGED, headRefName=feat/x, headRefOid=<real tip>
  cat > "$MOCK_BIN/gh" <<EOF
#!/usr/bin/env bash
if [ "\$1" = "pr" ] && [ "\$2" = "view" ]; then
  echo "MERGED feat/x $FEAT_OID"; exit 0
fi
exit 64
EOF
  chmod +x "$MOCK_BIN/gh"
}

@test "kill-switch FFS_RUN_FINALIZER=off -> no-op, exit 0" {
  mock_gh_merged
  FFS_RUN_FINALIZER=off run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [[ "$output" == *"disabled"* ]]
  git show-ref --verify -q refs/heads/feat/x   # branch untouched
}

@test "missing pr arg -> WARN but exit 0 (fail-soft contract)" {
  run bash "$LEVER"
  [ "$status" -eq 0 ]
  [[ "$output" == *"usage"* ]]
}

@test "gh failure -> exit 0, nothing deleted (no proof, no action)" {
  printf '#!/usr/bin/env bash\nexit 1\n' > "$MOCK_BIN/gh"; chmod +x "$MOCK_BIN/gh"
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [[ "$output" == *"no merge proof"* ]]
  git show-ref --verify -q refs/heads/feat/x
  [ -f .planning/run-state/gsd-run.pid ]
}

@test "PR not MERGED -> exit 0, nothing deleted" {
  cat > "$MOCK_BIN/gh" <<'EOF'
#!/usr/bin/env bash
echo "OPEN feat/x deadbeef"; exit 0
EOF
  chmod +x "$MOCK_BIN/gh"
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  git show-ref --verify -q refs/heads/feat/x
}

@test "squash-merged branch deleted only with landed-tip proof" {
  mock_gh_merged
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  # -d refuses (squash), -D allowed because tip == merged PR headRefOid
  ! git show-ref --verify -q refs/heads/feat/x
  [[ "$output" == *"landed"* ]]
}

@test "tip moved past merged head -> branch KEPT with warning" {
  mock_gh_merged
  git checkout -q feat/x
  echo c > c.txt; git add c.txt; git commit -qm "post-merge work"
  git checkout -q main
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  git show-ref --verify -q refs/heads/feat/x
  [[ "$output" == *"NOT deleting"* ]]
}

@test "remote branch deleted on origin" {
  mock_gh_merged
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  ! git ls-remote --exit-code origin refs/heads/feat/x
}

@test "clean worktree on feature branch removed before branch delete" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ ! -d "$WT" ]
  ! git show-ref --verify -q refs/heads/feat/x
}

@test "dirty worktree skipped, routed to /adopt-wip, branch kept" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  echo dirty > "$WT/uncommitted.txt"
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ -d "$WT" ]
  [[ "$output" == *"adopt-wip"* ]]
  git show-ref --verify -q refs/heads/feat/x   # checked out -> not deletable
}

@test "gsd/phase-* ancestor of merged head pruned; non-ancestor kept" {
  mock_gh_merged
  # phase-1: ancestor of feat/x tip (landed content)
  git branch gsd/phase-1-x "$FEAT_OID^" 2>/dev/null || git branch gsd/phase-1-x "$FEAT_OID"
  # phase-2: divergent commit (open work)
  git checkout -qb gsd/phase-2-x main
  echo d > d.txt; git add d.txt; git commit -qm "open work"
  git checkout -q main
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  ! git show-ref --verify -q refs/heads/gsd/phase-1-x
  git show-ref --verify -q refs/heads/gsd/phase-2-x
  [[ "$output" == *"keeping"* ]]
}

@test "run-state cleared; evidence store untouched" {
  mock_gh_merged
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ ! -f .planning/run-state/gsd-run.pid ]
  [ ! -f .planning/run-state/gsd-run.heartbeat ]
  [ ! -f .planning/run-state/gsd-run.status ]
  [ -f .feature-fix-swarm/evidence.json ]
  [ "$(cat .feature-fix-swarm/evidence.json)" = '{"k":"v"}' ]
}

@test "--dry-run: prints plan, deletes nothing" {
  mock_gh_merged
  run bash "$LEVER" --dry-run 1
  [ "$status" -eq 0 ]
  [[ "$output" == *"DRY"* ]]
  git show-ref --verify -q refs/heads/feat/x
  git ls-remote --exit-code origin refs/heads/feat/x
  [ -f .planning/run-state/gsd-run.pid ]
}

# Flag position must not matter. The first live run (PR #62) was invoked as
# `<pr> --dry-run` and silently no-op'd: --dry-run landed in $2, became
# `--repo --dry-run`, and gh failed — the exact silent cleanup-skip this
# lever exists to prevent, in an autonomous finish tail nobody watches.
@test "--dry-run is accepted AFTER the pr number (flag position is free)" {
  mock_gh_merged
  run bash "$LEVER" 1 --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" == *"DRY"* ]]
  [[ "$output" != *"failed"* ]]
  git show-ref --verify -q refs/heads/feat/x
  [ -f .planning/run-state/gsd-run.pid ]
}

# gh pr merge --delete-branch normally beat us to it; a WARN on every single
# finish tail is a WARN nobody reads.
@test "already-deleted remote branch is a note, not a WARN" {
  mock_gh_merged
  git push -q origin --delete feat/x
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [[ "$output" == *"already gone"* ]]
  [[ "$output" != *"step failed"* ]]
}

@test "a flag-shaped junk arg is refused, not silently passed to --repo" {
  mock_gh_merged
  run bash "$LEVER" --dry-run 1 --bogus
  [ "$status" -eq 0 ]
  [[ "$output" == *"unknown argument"* ]]
}
