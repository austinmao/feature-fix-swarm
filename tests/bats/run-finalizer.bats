#!/usr/bin/env bats
# run-finalizer.sh — run-end estate cleanup after a verified merge.
# Fixtures: real git repos in $BATS_TEST_TMPDIR (local bare origin), gh mocked.
# Callers (`feature-implement` and the finish tail) tolerate a pre-mutation
# nonzero: lock tampering and evidence persistence intentionally fail closed.

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
  # ambient GSD_RUN_ID (exported by feature-implement autonomous runs) rekeys
  # the archive dir away from the pr${PR} default these fixtures assert on
  unset GSD_RUN_ID || true

  # repo with a bare origin, main + squash-merged feature branch
  ORIGIN="$BATS_TEST_TMPDIR/origin.git"
  WORK="$BATS_TEST_TMPDIR/work"
  git init -q --bare "$ORIGIN"
  git init -q -b main "$WORK"
  cd "$WORK"
  git remote add origin "$ORIGIN"
  echo a > a.txt
  echo ".feature-fix-swarm/" > .gitignore  # matches real repos: ledger is gitignored,
                                            # so a worktree carrying one is NOT "dirty"
  git add a.txt .gitignore; git commit -qm "base"
  git push -q origin main
  git checkout -qb feat/x
  echo b > b.txt; git add b.txt; git commit -qm "feat work"
  FEAT_OID="$(git rev-parse feat/x)"
  git push -q origin feat/x
  git checkout -q main
  # simulate squash-merge: same content lands as a NEW commit on main.
  # `-q` is NOT a valid short flag for cherry-pick in this git version (only
  # `--quiet`/`-n` are recognized standalone; `-qn` fails with a usage error
  # and, because of the `&&`, silently skips the commit too) — this used to
  # leave main permanently one commit behind feat/x, which meant EVERY test's
  # squash-branch always fell through to the `-D`-via-headRefOid path and the
  # plain `git branch -d` (ff-merge) success path never actually ran.
  git cherry-pick --no-commit --quiet feat/x && git commit -qm "feat: x (#1) [squash]"
  git push -q origin main
  mkdir -p .planning/run-state
  echo pid > .planning/run-state/gsd-run.pid
  echo hb  > .planning/run-state/gsd-run.heartbeat
  echo st  > .planning/run-state/gsd-run.status
  mkdir -p .feature-fix-swarm
  echo '{"k":"v"}' > .feature-fix-swarm/evidence.json
  CHMOD_RESTORE_PATHS=()
}

# LOW: permission tests that `chmod 000`/`555` a fixture dir previously
# restored it with a bare `chmod 755 "$DIR"` line right after the `run`
# call — if any assertion between there and the end of the test aborted
# the test early (a bats `[ ... ]` failure), that restore line never ran,
# leaving a permission-denied directory behind for bats' own cleanup (or a
# later test sharing $BATS_TEST_TMPDIR-adjacent paths) to trip over.
# teardown() runs unconditionally after every test regardless of pass/fail
# — tests register paths via CHMOD_RESTORE_PATHS+=("$DIR") instead of
# restoring inline.
teardown() {
  local p
  for p in ${CHMOD_RESTORE_PATHS[@]+"${CHMOD_RESTORE_PATHS[@]}"}; do
    chmod 755 "$p" 2>/dev/null || true
  done
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

@test "finisher-skipped evidence CLI validates inputs before one atomic event row" {
  EVENT="$REPO_ROOT/lib/evidence_events.py"
  STORE="$BATS_TEST_TMPDIR/evidence.json"
  run env GATES_STORE="$STORE" python3 "$EVENT" finisher-skipped --run-id spec-008 --pr 12
  [ "$status" -eq 0 ]
  run python3 -c "import json; d=json.load(open('$STORE')); e=d['events']; assert len(e)==1; assert set(e[0]) == {'kind','run_id','pr','ts'}; assert e[0]['pr'] == 12 and isinstance(e[0]['ts'], (int,float))"
  [ "$status" -eq 0 ]
  before="$(cksum "$STORE")"
  run env GATES_STORE="$STORE" python3 "$EVENT" finisher-skipped --run-id $'bad\nrun' --pr +12
  [ "$status" -ne 0 ]
  [ "$(cksum "$STORE")" = "$before" ]
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

# F-T5: the broken `git cherry-pick -qn` fixture used to make main permanently
# one commit behind feat/x in every test, so `git branch -d` (plain
# fast-forward delete) NEVER actually succeeded anywhere in this suite — every
# scenario fell through to the `-D`-via-headRefOid fallback instead. A
# separate, genuinely fast-forward-merged branch exercises the `-d` success
# path directly.
@test "F-T5: plain fast-forward merge -> git branch -d succeeds directly (not via -D fallback)" {
  git checkout -qb feat/ff main
  echo ff > ff.txt; git add ff.txt; git commit -qm "feat: ff work"
  FF_OID="$(git rev-parse feat/ff)"
  git push -q origin feat/ff
  git checkout -q main
  git merge -q --ff-only feat/ff
  git push -q origin main
  cat > "$MOCK_BIN/gh" <<EOF
#!/usr/bin/env bash
if [ "\$1" = "pr" ] && [ "\$2" = "view" ]; then
  echo "MERGED feat/ff $FF_OID"; exit 0
fi
exit 64
EOF
  chmod +x "$MOCK_BIN/gh"
  run bash "$LEVER" 2
  [ "$status" -eq 0 ]
  ! git show-ref --verify -q refs/heads/feat/ff
  [[ "$output" == *"-d: merged into HEAD"* ]]
  # the squash branch fixture is untouched by this separate PR run
  git show-ref --verify -q refs/heads/feat/x
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

# --- Ledger archival (evidence survives worktree removal) ---
# gates.py resolves its evidence store relative to CWD (.feature-fix-swarm/),
# so a worktree's ledger lives ONLY inside that worktree. Before the worktree
# is removed, archive_run_ledger() must copy it to a durable, run-keyed
# location outside the worktree.

@test "archived ledger: worktree's evidence.json survives worktree removal" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ ! -d "$WT" ]
  DEST="$WORK/.feature-fix-swarm/archive/pr1/feat-x/evidence.json"
  [ -f "$DEST" ]
  [ "$(cat "$DEST")" = '{"w":"1"}' ]
}

@test "learnings-archive.jsonl is archived alongside evidence.json" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  echo '{"l":"1"}' > "$WT/.feature-fix-swarm/learnings-archive.jsonl"
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  DEST_DIR="$WORK/.feature-fix-swarm/archive/pr1/feat-x"
  [ -f "$DEST_DIR/evidence.json" ]
  [ -f "$DEST_DIR/learnings-archive.jsonl" ]
  [ "$(cat "$DEST_DIR/learnings-archive.jsonl")" = '{"l":"1"}' ]
}

# NEW-1: this is the real repro. $BATS_TEST_TMPDIR itself lives under a
# macOS OS-level mount alias (/tmp -> /private/tmp, and $TMPDIR is typically
# /var/folders/... -- /var -> /private/var is the same class of alias). The
# round-2 fix lstat-walked the raw LOGICAL value from filesystem root
# unconditionally and refused FOREVER on exactly this path shape, even
# though nothing here is attacker-controlled. No `pwd -P` pre-normalization
# here on purpose -- that would mask the bug this test exists to catch.
@test "FFS_LEDGER_ARCHIVE_DIR override is honored" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  CUSTOM="$BATS_TEST_TMPDIR/custom-archive"
  FFS_LEDGER_ARCHIVE_DIR="$CUSTOM" run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ -f "$CUSTOM/pr1/feat-x/evidence.json" ]
}

# NEW-2: mode 000 on $LOCKED would (correctly, and separately) refuse
# earlier at the `cd`+`pwd -P` physical-resolution step (cd needs execute
# permission, which 000 also denies) -- that's a DIFFERENT guard than the
# one this test is meant to exercise. Mode 555 (read+execute, no write)
# lets `cd`/traversal succeed while still making `mkdir` inside it fail,
# reaching the actual _mkdir_contained "could not create archive dir" path
# this test targets. Bare "WARN" was also too weak a substring (matched by
# ANY refusal reason) -- tightened to the specific message.
@test "archive destination unwritable -> worktree KEPT, WARN, exit 0" {
  if [ "$(id -u)" -eq 0 ]; then skip "root ignores chmod restrictions"; fi
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  LOCKED="$BATS_TEST_TMPDIR/locked"
  mkdir -p "$LOCKED"
  chmod 555 "$LOCKED"
  CHMOD_RESTORE_PATHS+=("$LOCKED")
  FFS_LEDGER_ARCHIVE_DIR="$LOCKED/archive" run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ -d "$WT" ]
  [[ "$output" == *"could not create archive dir"* ]]
  git show-ref --verify -q refs/heads/feat/x
}

# NEW-4: a non-EEXIST mkdir failure in the claim loop (read-only run_dir) must
# abort immediately with an accurate diagnosis, not burn 21 "collision"
# attempts. The "not a collision" message only prints on the non-EEXIST path,
# so this test fails if _try_claim_leaf reverts to an undifferentiated return 1.
@test "NEW-4: read-only run_dir -> immediate accurate refusal, worktree KEPT, exit 0" {
  if [ "$(id -u)" -eq 0 ]; then skip "root ignores chmod restrictions"; fi
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  ARCH="$BATS_TEST_TMPDIR/arch-ro"
  mkdir -p "$ARCH/pr1"
  chmod 555 "$ARCH/pr1"
  CHMOD_RESTORE_PATHS+=("$ARCH/pr1")
  FFS_LEDGER_ARCHIVE_DIR="$ARCH" run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ -d "$WT" ]
  [[ "$output" == *"not a collision"* ]]
  [[ "$output" != *"after"*"attempts"* ]]
  git show-ref --verify -q refs/heads/feat/x
}

@test "worktree's .feature-fix-swarm is a symlink -> refused, worktree KEPT, exit 0" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  REAL_TARGET="$BATS_TEST_TMPDIR/somewhere-else"
  mkdir -p "$REAL_TARGET"
  # A symlink named .feature-fix-swarm is untracked and NOT matched by the
  # repo's trailing-slash ".feature-fix-swarm/" gitignore rule (that pattern
  # only matches real directories), so an untracked symlink alone would just
  # trip the earlier dirty-worktree check. To exercise the symlink guard
  # INSIDE archive_run_ledger specifically, commit the symlink as tracked
  # content — the scenario the guard is actually defending against (a
  # write-through/read-through redirect landing via committed history).
  ( cd "$WT" && ln -s "$REAL_TARGET" .feature-fix-swarm \
    && git add .feature-fix-swarm && git commit -qm "tracked symlink (fixture)" )
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ -d "$WT" ]
  # F-T3: a bare "symlink" substring match is satisfied by TWO different
  # guards -- this top-level check (`if [ -L "$ffs_src" ]`, cheap top-level
  # check, runs first) AND _scan_source_tree's own per-entry symlink check
  # (find lists the root path argument itself as an entry, so it also flags
  # $ffs_src when the top-level check is deleted). A bare substring match
  # stays green either way, so deleting THIS guard alone previously produced
  # 0 failing tests. Assert the exact top-level message and explicitly rule
  # out the scan_source_tree fallback wording to make the two guards
  # distinguishable.
  [[ "$output" == *"is a symlink — refusing to archive"* ]]
  [[ "$output" != *"failed safety scan"* ]]
}

@test "worktree without .feature-fix-swarm archives nothing, removed normally (no regression)" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ ! -d "$WT" ]
  [ ! -d "$WORK/.feature-fix-swarm/archive" ]
}

@test "--dry-run: ledger archive skipped, worktree kept" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  run bash "$LEVER" --dry-run 1
  [ "$status" -eq 0 ]
  # F6: assert on the SPECIFIC line naming the ledger archive, not just any
  # "DRY" substring (a bare "DRY" match is also satisfied by the unrelated
  # "DRY: git worktree remove" note, so it stays green even if the
  # archive_run_ledger call is deleted entirely).
  [[ "$output" == *"DRY: archive ledger"* ]]
  [ -d "$WT" ]
  [ ! -d "$WORK/.feature-fix-swarm/archive" ]
}

# --- Findings F1-F5 remediation coverage ---

# F1: with F-A's collision-avoidance fix, a non-empty destination leaf (this
# poisoned-symlink scenario is one way it can be non-empty; a legitimate prior
# archive under the same run/branch slug is another, see the F-A test below)
# is now NEVER written into or compared against — archive_run_ledger
# disambiguates to a fresh, distinct path instead. That is a strictly better
# outcome than the original "refuse and keep the worktree forever" behavior:
# the poisoned entry is left completely untouched (main ledger never read
# through it, never at risk), the worktree's own ledger is still safely
# archived (just at a disambiguated path), and the worktree can be removed
# normally instead of requiring manual cleanup.
@test "F1: destination leaf pre-exists as symlink to main ledger -> disambiguated elsewhere, main ledger unchanged, worktree removed" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  mkdir -p "$WORK/.feature-fix-swarm"
  echo '{"main":"live"}' > "$WORK/.feature-fix-swarm/evidence.json"
  DEST_DIR="$WORK/.feature-fix-swarm/archive/pr1/feat-x"
  mkdir -p "$DEST_DIR"
  ln -s "$WORK/.feature-fix-swarm/evidence.json" "$DEST_DIR/evidence.json"
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ ! -d "$WT" ]
  [[ "$output" == *"collision"* ]]
  # the poisoned symlink at the original leaf is completely untouched
  [ -L "$DEST_DIR/evidence.json" ]
  [ "$(cat "$WORK/.feature-fix-swarm/evidence.json")" = '{"main":"live"}' ]
  # the worktree's OWN ledger content is still safely archived, just at a
  # disambiguated sibling directory
  found=0
  for d in "$WORK/.feature-fix-swarm/archive/pr1"/feat-x-*; do
    [ -d "$d" ] || continue
    if [ -f "$d/evidence.json" ] && [ "$(cat "$d/evidence.json")" = '{"w":"1"}' ]; then
      found=1
    fi
  done
  [ "$found" -eq 1 ]
}

# F-T4: these scenarios are entirely benign (a plain subdirectory / dotfile,
# no symlinks, no permission issues) -- there is no legitimate reason for the
# archive to refuse. The original `if [ -d "$WT" ]; then ... else ... fi`
# branching accepted EITHER outcome as passing, which means a real regression
# (e.g. the archive spuriously refusing on a subdirectory it should handle
# fine) would silently take the "worktree kept" branch and still show green.
# Assert the one deterministically-correct outcome instead: worktree removed,
# ledger content moved into the archive.
@test "F2a: ledger subdirectory (findings/queue.json) is archived, worktree removed" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm/findings"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  echo '{"q":"1"}' > "$WT/.feature-fix-swarm/findings/queue.json"
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ ! -d "$WT" ]
  DEST_DIR="$WORK/.feature-fix-swarm/archive/pr1/feat-x"
  [ -f "$DEST_DIR/findings/queue.json" ]
  [ "$(cat "$DEST_DIR/findings/queue.json")" = '{"q":"1"}' ]
}

@test "F2b: ledger dotfile (.runstate) is archived, worktree removed" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  echo 'runstate-content' > "$WT/.feature-fix-swarm/.runstate"
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ ! -d "$WT" ]
  DEST_DIR="$WORK/.feature-fix-swarm/archive/pr1/feat-x"
  [ -f "$DEST_DIR/.runstate" ]
  [ "$(cat "$DEST_DIR/.runstate")" = 'runstate-content' ]
}

@test "F2c: unreadable ledger source dir -> worktree kept, WARN, exit 0" {
  if [ "$(id -u)" -eq 0 ]; then skip "root ignores chmod restrictions"; fi
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  chmod 000 "$WT/.feature-fix-swarm"
  CHMOD_RESTORE_PATHS+=("$WT/.feature-fix-swarm")
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ -d "$WT" ]
  # Assert the SPECIFIC guard message, not a bare "WARN" substring: an
  # unreadable subdirectory also makes `git worktree remove` itself fail
  # (it can't recurse into .feature-fix-swarm to delete it), which prints
  # its own generic "WARN: step failed" line regardless of whether our
  # readability guard ever ran — a bare "WARN" match stays green even with
  # archive_run_ledger's call site deleted.
  [[ "$output" == *"not readable/traversable"* ]]
}

@test "F3: intermediate archive segment is a symlink -> refused, nothing written outside archive root" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  ARCHIVE_ROOT="$WORK/.feature-fix-swarm/archive"
  OUTSIDE="$BATS_TEST_TMPDIR/outside-pr1"
  mkdir -p "$ARCHIVE_ROOT" "$OUTSIDE"
  ln -s "$OUTSIDE" "$ARCHIVE_ROOT/pr1"
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ -d "$WT" ]
  # LOW: tightened from a bare *"symlink"* substring (matched by several
  # different guards in this lever) to the exact _mkdir_contained message.
  [[ "$output" == *"archive path segment"*"is a symlink — refusing to archive"* ]]
  [ ! -e "$OUTSIDE/feat-x" ]
}

# F4/F-C: the original version of this test pre-populated the destination
# with a truncated stub and asserted it got silently overwritten. With F-A's
# collision-avoidance fix in place, ANY pre-existing non-empty destination
# dir is now redirected to a fresh, disambiguated path instead of ever being
# compared against or written into — so the original scenario ("stale debris
# at the exact computed dest_dir") can no longer be exercised end-to-end; it
# is architecturally unreachable now that F-A always routes around existing
# content. What F-C actually needs proven is narrower and truer to the real
# gates.py failure mode: a same-size content rewrite is invisible to a
# relpath+size-only manifest. Within a single synchronous script run, source
# and its freshly-made copy are always byte-identical (nothing else touches
# them in between) — the exploitable window is an EXTERNAL process (gates.py
# _save_store) rewriting evidence.json mid-flight, which cannot be
# reproduced deterministically without timing games. So this proves the
# mechanism directly: extract the pure manifest helpers (no top-level
# side-effecting code, safe to source standalone) and confirm the checksum
# column distinguishes two same-size, different-content files where a
# size-only manifest would see them as identical.
@test "F4/F-C: manifest checksum column detects a same-size content change (size-only would miss it)" {
  FUNCS="$BATS_TEST_TMPDIR/manifest-funcs.sh"
  awk '/^_mkdir_contained\(\) \{/{exit} /^_checksum_of\(\) \{/{p=1} p{print}' "$LEVER" > "$FUNCS"
  [ -s "$FUNCS" ]   # sanity: extraction actually found the functions
  CHECK="$BATS_TEST_TMPDIR/check.sh"
  cat > "$CHECK" <<'SCRIPT'
#!/usr/bin/env bash
set -uo pipefail
source "$1"
A="$2"; B="$3"
mkdir -p "$A" "$B"
printf 'AAAAAAAA' > "$A/evidence.json"   # 8 bytes
printf 'BBBBBBBB' > "$B/evidence.json"   # 8 bytes, different content
ma="$(_manifest_of "$A")"
mb="$(_manifest_of "$B")"
if [ "$ma" = "$mb" ]; then
  echo "MANIFESTS-MATCH (checksum missing or broken -- same-size rewrite would go undetected)"
  exit 1
fi
echo "MANIFESTS-DIFFER (checksum column is present and working)"
SCRIPT
  chmod +x "$CHECK"
  run bash "$CHECK" "$FUNCS" "$BATS_TEST_TMPDIR/csum-a" "$BATS_TEST_TMPDIR/csum-b"
  [ "$status" -eq 0 ]
  [[ "$output" == *"MANIFESTS-DIFFER"* ]]
}

# F-T4: same rationale as F2a/F2b above -- this GATES_STORE scenario is
# benign (nothing anomalous about it), so the `if [ -d "$WT" ]` branching
# accepted either outcome and would silently mask a spurious refusal here
# too. Assert the one correct outcome deterministically.
@test "F5: GATES_STORE outside .feature-fix-swarm is archived, worktree removed" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  # Commit the fixture file (rather than leaving it untracked) so the
  # worktree stays CLEAN — an untracked custom-gates/ would trip the
  # pre-existing dirty-worktree gate before archive_run_ledger ever runs,
  # which would make this test pass vacuously regardless of the fix.
  ( cd "$WT" && mkdir -p custom-gates && echo '{"g":"1"}' > custom-gates/evidence.json \
    && git add custom-gates && git commit -qm "custom gates fixture" )
  GATES_STORE="$WT/custom-gates/evidence.json" run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ ! -d "$WT" ]
  # M5: the extra GATES_STORE file is namespaced under "_worktree/" so its
  # worktree-relative path can never collide with anything cp -R flattens
  # from .feature-fix-swarm/ into the same destination tree.
  DEST_DIR="$WORK/.feature-fix-swarm/archive/pr1/feat-x"
  [ -f "$DEST_DIR/_worktree/custom-gates/evidence.json" ]
  [ "$(cat "$DEST_DIR/_worktree/custom-gates/evidence.json")" = '{"g":"1"}' ]
}

@test "branch name with '/' produces flat, contained destination (no path escape)" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  ARCHIVE_ROOT="$WORK/.feature-fix-swarm/archive"
  [ -f "$ARCHIVE_ROOT/pr1/feat-x/evidence.json" ]
  [ ! -d "$ARCHIVE_ROOT/pr1/feat" ]
  case "$(cd "$ARCHIVE_ROOT/pr1/feat-x" && pwd)" in
    "$ARCHIVE_ROOT"/*) : ;;
    *) false ;;
  esac
}

# --- Findings F-A through F-E remediation coverage ---

# F-A: run_key is constant per PR/run, and _sanitize_slug collapses distinct
# raw branch names onto the same slug (e.g. '+' and '-' both become '-').
# Two gsd/phase-* branches whose tips are both the merged head sanitize to
# the identical slug 'gsd-phase-1-a'. Without disambiguation, the SECOND
# archive would silently overwrite the FIRST worktree's ledger and both
# worktrees would be removed -- the first ledger becomes irrecoverable.
@test "F-A: two branches sanitizing to the identical slug get distinct, collision-safe archive dirs" {
  mock_gh_merged
  git branch gsd/phase-1+a "$FEAT_OID"
  git branch gsd/phase-1-a "$FEAT_OID"
  WT1="$BATS_TEST_TMPDIR/wt-plus"
  WT2="$BATS_TEST_TMPDIR/wt-dash"
  git worktree add -q "$WT1" gsd/phase-1+a
  git worktree add -q "$WT2" gsd/phase-1-a
  mkdir -p "$WT1/.feature-fix-swarm" "$WT2/.feature-fix-swarm"
  echo '{"which":"plus"}' > "$WT1/.feature-fix-swarm/evidence.json"
  echo '{"which":"dash"}' > "$WT2/.feature-fix-swarm/evidence.json"
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ ! -d "$WT1" ]
  [ ! -d "$WT2" ]
  [[ "$output" == *"collision"* ]]
  ARCHIVE_ROOT="$WORK/.feature-fix-swarm/archive/pr1"
  # first-to-process branch (ordered by for-each-ref: '+' < '-') claims the
  # plain slug untouched
  [ -f "$ARCHIVE_ROOT/gsd-phase-1-a/evidence.json" ]
  [ "$(cat "$ARCHIVE_ROOT/gsd-phase-1-a/evidence.json")" = '{"which":"plus"}' ]
  # the SECOND branch's content must land in a genuinely DIFFERENT directory,
  # never overwriting the first
  found_second=0
  for d in "$ARCHIVE_ROOT"/gsd-phase-1-a-*; do
    [ -d "$d" ] || continue
    if [ -f "$d/evidence.json" ] && [ "$(cat "$d/evidence.json")" = '{"which":"dash"}' ]; then
      found_second=1
    fi
  done
  [ "$found_second" -eq 1 ]
  # the first archive remains untouched after the second one lands
  [ "$(cat "$ARCHIVE_ROOT/gsd-phase-1-a/evidence.json")" = '{"which":"plus"}' ]
}

# F-B: a double-quoted `trap "rm -f '$tmp'" RETURN` expands $tmp at trap-SET
# time, baking its value into the stored command text. A $TMPDIR containing
# a literal single quote then breaks that stored command's own quoting when
# bash re-parses it at trap-FIRE time -- at best a leaked scan tmp file, at
# worst arbitrary command injection via a crafted $TMPDIR. Prove the fixed
# (single-quoted, expand-at-fire-time) trap tolerates this cleanly: the
# archive still succeeds AND the scan tmp file is actually cleaned up.
@test "F-B: TMPDIR containing a single quote does not break trap cleanup, archive still succeeds" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  WEIRD_TMP="$BATS_TEST_TMPDIR/it's-tmp"
  mkdir -p "$WEIRD_TMP"
  TMPDIR="$WEIRD_TMP" run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ ! -d "$WT" ]
  DEST="$WORK/.feature-fix-swarm/archive/pr1/feat-x/evidence.json"
  [ -f "$DEST" ]
  [ "$(cat "$DEST")" = '{"w":"1"}' ]
  # the RETURN trap must have fired and removed both scan + manifest tmp
  # files -- a broken (expand-at-set-time) trap leaves them behind because
  # the re-parsed command fails on the embedded quote.
  ! ls "$WEIRD_TMP"/ffs-scan.* >/dev/null 2>&1
  ! ls "$WEIRD_TMP"/ffs-manifest.* >/dev/null 2>&1
}

# M1: the previous version of this test asserted no specific message and
# was actually satisfied by _scan_source_tree's pre-existing SOURCE-side
# enumeration guard refusing first -- _manifest_of's OWN find-exit-status
# check (this test's nominal subject) was never reached, let alone proven.
# _scan_source_tree only ever scans the SOURCE tree; _manifest_of is called
# on $dest_dir too, with no equivalent guard preceding it there, so this is
# genuinely a separate code path. Isolate it directly: extract the pure
# manifest helpers (no top-level side-effecting code, safe to source
# standalone -- same technique as the F4/F-C test above) and call
# _manifest_of on a mode-000 subdirectory (MUT-1's own repro: "mode-000
# subdir under dest_dir"), with NO _scan_source_tree or archive_run_ledger
# anywhere in the call path to mask the result.
@test "M1/MUT-1: _manifest_of's own find-exit-status guard is isolated and catches a mode-000 subdir under dest_dir" {
  if [ "$(id -u)" -eq 0 ]; then skip "root ignores chmod restrictions"; fi
  FUNCS="$BATS_TEST_TMPDIR/manifest-funcs.sh"
  awk '/^_mkdir_contained\(\) \{/{exit} /^_checksum_of\(\) \{/{p=1} p{print}' "$LEVER" > "$FUNCS"
  [ -s "$FUNCS" ]
  DEST="$BATS_TEST_TMPDIR/dest-side"
  mkdir -p "$DEST/locked-sub"
  echo visible > "$DEST/visible.txt"
  echo hidden > "$DEST/locked-sub/secret.txt"
  chmod 000 "$DEST/locked-sub"
  CHMOD_RESTORE_PATHS+=("$DEST/locked-sub")
  CHECK="$BATS_TEST_TMPDIR/check-manifest-of.sh"
  cat > "$CHECK" <<'SCRIPT'
#!/usr/bin/env bash
set -uo pipefail
warn() { echo "WARN: $*" >&2; }
source "$1"
_manifest_of "$2"
echo "RC=$?"
SCRIPT
  chmod +x "$CHECK"
  run bash "$CHECK" "$FUNCS" "$DEST"
  [ "$status" -eq 0 ]
  [[ "$output" == *"RC=1"* ]]
  [[ "$output" == *"enumeration failed"* ]]
}

# MUT-3: _build_expected_manifest's OWN internal `_manifest_of "$2" ||
# return 1` (not the outer archive_run_ledger call sites) must propagate a
# source-side enumeration failure. Isolated the same way as above, calling
# _build_expected_manifest directly with have_ffs=1 pointing at a directory
# containing a mode-000 subdirectory (MUT-3's own repro: "expected-manifest
# build failure") -- no _scan_source_tree in the call path, so reverting
# JUST this `|| return 1` to `|| true` is what flips this test.
@test "MUT-3: _build_expected_manifest propagates its own internal _manifest_of failure" {
  if [ "$(id -u)" -eq 0 ]; then skip "root ignores chmod restrictions"; fi
  FUNCS="$BATS_TEST_TMPDIR/manifest-funcs.sh"
  awk '/^_mkdir_contained\(\) \{/{exit} /^_checksum_of\(\) \{/{p=1} p{print}' "$LEVER" > "$FUNCS"
  [ -s "$FUNCS" ]
  SRC="$BATS_TEST_TMPDIR/expected-src"
  mkdir -p "$SRC/locked-sub"
  echo visible > "$SRC/visible.txt"
  echo hidden > "$SRC/locked-sub/secret.txt"
  chmod 000 "$SRC/locked-sub"
  CHMOD_RESTORE_PATHS+=("$SRC/locked-sub")
  CHECK="$BATS_TEST_TMPDIR/check-expected-manifest.sh"
  cat > "$CHECK" <<'SCRIPT'
#!/usr/bin/env bash
set -uo pipefail
warn() { echo "WARN: $*" >&2; }
source "$1"
_build_expected_manifest 1 "$2" 0 "" ""
echo "RC=$?"
SCRIPT
  chmod +x "$CHECK"
  run bash "$CHECK" "$FUNCS" "$SRC"
  [ "$status" -eq 0 ]
  [[ "$output" == *"RC=1"* ]]
}

# F-D (end-to-end regression): the same mode-000-subdirectory scenario,
# exercised through the real archive_run_ledger flow. _scan_source_tree's
# SOURCE-side guard fires first here (both guards legitimately defend the
# same root cause for a SOURCE-side repro) -- the isolating proof of
# _manifest_of's OWN guard is the M1/MUT-1 test above; this one just
# confirms the overall required outcome (archive refused, worktree kept)
# still holds end-to-end.
@test "F-D: unreadable subdirectory inside the ledger hides files from enumeration -> archive refused, worktree kept" {
  if [ "$(id -u)" -eq 0 ]; then skip "root ignores chmod restrictions"; fi
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm/locked-sub"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  echo 'hidden' > "$WT/.feature-fix-swarm/locked-sub/secret.txt"
  chmod 000 "$WT/.feature-fix-swarm/locked-sub"
  CHMOD_RESTORE_PATHS+=("$WT/.feature-fix-swarm/locked-sub")
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ -d "$WT" ]
  git show-ref --verify -q refs/heads/feat/x
}

# F-T1: the manifest-verification block is the ONLY guard that catches this.
# M5 namespaces the extra GATES_STORE file under "_worktree/<wt-relative>" so
# it can no longer collide with an ordinary .feature-fix-swarm/X relpath —
# but the ledger tree itself can coincidentally ALSO contain a "_worktree/"
# subdirectory (e.g. some future gates.py layout), which reintroduces the
# identical collision one level deeper: cp -R writes ffs_src's own
# ".feature-fix-swarm/_worktree/evidence.json" first, and the extra-file cp
# (GATES_STORE=evidence.json at the worktree root -> namespaced to
# "_worktree/evidence.json") overwrites that SAME destination path
# afterward. _build_expected_manifest independently lists BOTH source
# entries at that relpath (different size/checksum); the actual on-disk
# destination has only the last-written file. Deleting the final
# expected-vs-actual comparison would let this land and remove the worktree
# with a silently-corrupted archive (missing the shadowed file's true
# content).
@test "F-T1: an in-ledger '_worktree/' subdir colliding with the namespaced GATES_STORE path makes destination diverge from source -> verification catches it, worktree kept" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  ( cd "$WT" && mkdir -p .feature-fix-swarm/_worktree \
    && echo 'FFS-VERSION' > .feature-fix-swarm/_worktree/evidence.json \
    && echo 'GATES-VERSION' > evidence.json \
    && git add evidence.json && git commit -qm "worktree-root evidence.json fixture" )
  GATES_STORE="$WT/evidence.json" run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ -d "$WT" ]
  [[ "$output" == *"verification failed"* ]]
  git show-ref --verify -q refs/heads/feat/x
}

# F-T2: a symlink NESTED inside .feature-fix-swarm (not the top-level dir
# itself) is caught ONLY by _scan_source_tree's own per-entry `[ -L "$entry" ]`
# check during its find traversal -- the earlier "cheap top-level checks"
# only look at $ffs_src itself, and would happily accept a real top-level
# directory that merely CONTAINS a symlink somewhere inside it. Deleting the
# _scan_source_tree call entirely removes the only guard exercised here.
@test "F-T2: symlink nested inside .feature-fix-swarm (not the top-level dir) is refused by the source tree scan, worktree kept" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  REAL_TARGET="$BATS_TEST_TMPDIR/nested-symlink-target"
  mkdir -p "$REAL_TARGET"
  # .feature-fix-swarm/ is gitignored -- force-add just the nested symlink so
  # the worktree stays clean and this exercises archive_run_ledger, not the
  # earlier dirty-worktree gate.
  ( cd "$WT" && ln -s "$REAL_TARGET" .feature-fix-swarm/nested-link \
    && git add -f .feature-fix-swarm/nested-link \
    && git commit -qm "nested symlink fixture" )
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ -d "$WT" ]
  [[ "$output" == *"failed safety scan"* ]]
  [[ "$output" == *"symlink at"* ]]
  git show-ref --verify -q refs/heads/feat/x
}

# --- H1 / NEW-1: symlinked-ancestor dest_root handling ---
# NEW-1 relaxed round-2's H1 fix: refusing on ANY symlinked ancestor,
# unconditionally, refused FOREVER on legitimate OS-level mount aliases
# (macOS's /tmp -> /private/tmp, $TMPDIR under /var/folders/...) since
# those are symlinks too, just not attacker-planted ones. The new contract:
# if the override value ALREADY exists (reached via ANY symlink, alias or
# otherwise), it is physically resolved and TRUSTED as-is -- re-litigating
# an operator-supplied, already-existing location isn't this lever's job.
# Only the NOT-yet-existing remainder is still lstat-walked, so a symlink
# planted specifically in that remainder still refuses (the H1b/MUT-2 test
# below).

@test "H1a: FFS_LEDGER_ARCHIVE_DIR through a symlinked ancestor (whole value pre-exists) is honored, physically resolved" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  OUTSIDE="$BATS_TEST_TMPDIR/outside-target-a"
  BASE="$BATS_TEST_TMPDIR/base-a"
  mkdir -p "$OUTSIDE/arch" "$BASE"
  ln -s "$OUTSIDE" "$BASE/link"
  FFS_LEDGER_ARCHIVE_DIR="$BASE/link/arch" run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ ! -d "$WT" ]
  # archived at the PHYSICALLY resolved location (through the symlink),
  # never refused just because a symlink sits somewhere in the path
  [ -f "$OUTSIDE/arch/pr1/feat-x/evidence.json" ]
}

@test "H1b/MUT-2: a symlink planted in the NOT-yet-existing remainder still refuses, nothing written through it" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  # A REAL existing directory, then a symlink to a FILE (not a directory)
  # as the immediate parent of the still-missing final segment. The whole
  # override value does NOT yet exist ("target" can't exist under a file),
  # so this exercises the not-yet-existing-remainder walk specifically:
  # `-d` on "evil-link" is false (its target is a file, not a directory) --
  # it is never absorbed as a trusted "already exists" anchor -- and
  # _mkdir_contained's own `-L` check on it (true regardless of what it
  # points to) is what refuses it.
  REALDIR="$BATS_TEST_TMPDIR/real-dir-b"
  OUTSIDE_FILE="$BATS_TEST_TMPDIR/some-file-target-b"
  mkdir -p "$REALDIR"
  touch "$OUTSIDE_FILE"
  ln -s "$OUTSIDE_FILE" "$REALDIR/evil-link"
  FFS_LEDGER_ARCHIVE_DIR="$REALDIR/evil-link/target" run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ -d "$WT" ]
  [[ "$output" == *"is a symlink"* ]]
  # nothing named "target" was ever created anywhere under the real dir
  ! find "$REALDIR" -name target 2>/dev/null | grep -q .
}

# --- M3: atomic claim never writes into a pre-existing leaf ---

@test "M3: atomic claim — a pre-existing dest leaf's content is never overwritten" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  DEST_DIR="$WORK/.feature-fix-swarm/archive/pr1/feat-x"
  mkdir -p "$DEST_DIR"
  echo 'PRE-EXISTING-CONTENT-DO-NOT-TOUCH' > "$DEST_DIR/sentinel.txt"
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  # the pre-existing leaf and its content are completely untouched
  [ -f "$DEST_DIR/sentinel.txt" ]
  [ "$(cat "$DEST_DIR/sentinel.txt")" = 'PRE-EXISTING-CONTENT-DO-NOT-TOUCH' ]
  [ ! -f "$DEST_DIR/evidence.json" ]   # our content was never written into it
  # our own content lands at a disambiguated sibling instead
  found=0
  for d in "$WORK/.feature-fix-swarm/archive/pr1"/feat-x-*; do
    [ -d "$d" ] || continue
    if [ -f "$d/evidence.json" ] && [ "$(cat "$d/evidence.json")" = '{"w":"1"}' ]; then
      found=1
    fi
  done
  [ "$found" -eq 1 ]
}

# --- M4: a failed archive leaves no ratcheting debris ---

@test "M4: a failed archive leaves no partial debris — dest leaf is reusable by a subsequent successful run" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  # First run: force a post-claim failure via the same in-ledger '_worktree/'
  # collision as F-T1 (manifest verification fails AFTER dest_dir is created
  # and populated).
  ( cd "$WT" && mkdir -p .feature-fix-swarm/_worktree \
    && echo 'FFS-VERSION' > .feature-fix-swarm/_worktree/evidence.json \
    && echo 'GATES-VERSION' > evidence.json \
    && git add evidence.json && git commit -qm "collision fixture" )
  GATES_STORE="$WT/evidence.json" run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ -d "$WT" ]   # kept after the verification failure
  DEST_DIR="$WORK/.feature-fix-swarm/archive/pr1/feat-x"
  [ ! -e "$DEST_DIR" ]   # M4: no partial debris left behind by the failure
  # Remove the collision and retry — must land at the SAME original leaf,
  # not a ratcheted -hash8/-2 sibling (which is what would happen if the
  # first failure's partial dest_dir had been left behind).
  ( cd "$WT" && git rm -f evidence.json >/dev/null && git commit -qm "remove collision fixture" )
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ ! -d "$WT" ]
  [ -f "$DEST_DIR/_worktree/evidence.json" ]
  [ "$(cat "$DEST_DIR/_worktree/evidence.json")" = 'FFS-VERSION' ]
  # no ratcheted sibling directories exist from the earlier failed attempt
  for d in "$WORK/.feature-fix-swarm/archive/pr1"/feat-x-*; do
    [ -e "$d" ] && false
  done
  true
}

# --- NEW-3: FFS_LEDGER_ARCHIVE_DIR=/ must not crash under bash 3.2 set -u ---
# `dest_root_rel="${dest_root#/}"` on an input of literally "/" produces an
# EMPTY string; `IFS='/' read -r -a segs <<< ""` then yields a ZERO-element
# array, and `for seg in "${segs[@]}"` on an empty array is an
# unbound-variable abort under bash 3.2's `set -u` (bash 5's array
# expansion semantics changed and do NOT reproduce this — it must run under
# real 3.2, not just macOS's default `bash` shim, to catch a regression).
# This breaks the lever's own always-exit-0 contract. Run via /bin/bash
# explicitly (macOS system bash, still 3.2.57) rather than the bare `bash`
# on PATH (Homebrew, 5.x) to actually exercise this.
@test "NEW-3: FFS_LEDGER_ARCHIVE_DIR=/ exits 0 and keeps the worktree under real bash 3.2" {
  if [ ! -x /bin/bash ]; then skip "/bin/bash not present on this system"; fi
  /bin/bash -c '[ "${BASH_VERSINFO[0]}" -ge 4 ] && exit 1; exit 0' \
    || skip "/bin/bash on this system is not actually 3.x (already upgraded)"
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  FFS_LEDGER_ARCHIVE_DIR="/" run /bin/bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ -d "$WT" ]
  [[ "$output" == *"filesystem root"* ]]
  git show-ref --verify -q refs/heads/feat/x
}

# --- review-gate round: HIGH-1 dest-inside-worktree self-defeat ---
@test "HIGH-1: FFS_LEDGER_ARCHIVE_DIR pointing INSIDE the worktree being removed is refused, worktree kept" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  # Without HIGH-1's fix, this archives INTO the worktree and then
  # `git worktree remove` deletes the "archive" it just wrote —
  # self-defeating the entire point of the lever.
  FFS_LEDGER_ARCHIVE_DIR="$WT/nested-archive" run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ -d "$WT" ]
  [[ "$output" == *"is the worktree being removed, or nested inside it"* ]]
  git show-ref --verify -q refs/heads/feat/x
}

# --- review-gate round: HIGH-2 destination-side symlink scan ---
# A symlink can't naturally reach dest_dir via normal archive flow: source
# is scanned (_scan_source_tree) BEFORE cp -R ever runs, so anything
# scan-worthy in the source never gets copied. To isolate the DEST-side
# scan specifically (independent of the source-side one), `cp` itself is
# mocked to plant a symlink in the destination AFTER a normal copy —
# simulating "the destination somehow ends up with a symlink despite a
# clean, already-scanned source" (e.g. a TOCTOU race with an external
# writer). Mutation: deleting the `_scan_source_tree "$dest_dir"` call
# after the copy is what this test exists to catch.
@test "HIGH-2: a symlink appearing in the destination after copy is caught by the post-copy scan, worktree kept" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  REAL_CP="$(command -v cp)"
  cat > "$MOCK_BIN/cp" <<EOF
#!/usr/bin/env bash
"$REAL_CP" "\$@"
rc=\$?
if [ "\$1" = "-R" ]; then
  last="\${@: -1}"
  ln -sf /etc/hosts "\${last%/}/planted-symlink" 2>/dev/null
fi
exit \$rc
EOF
  chmod +x "$MOCK_BIN/cp"
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ -d "$WT" ]
  [[ "$output" == *"failed post-copy safety scan"* ]]
  git show-ref --verify -q refs/heads/feat/x
}

# --- review-gate round: HIGH-3 cleanup containment ---
# Whitebox: extracts the pure helpers (_cleanup_partial_dest depends only
# on warn(), which is stubbed) and calls _cleanup_partial_dest with a
# target OUTSIDE run_dir/branch_slug_final -- must refuse and leave it
# untouched, regardless of what the caller passes.
@test "HIGH-3: _cleanup_partial_dest refuses to delete anything outside this run's claimed leaf" {
  FUNCS="$BATS_TEST_TMPDIR/cleanup-funcs.sh"
  awk '/^archive_run_ledger\(\) \{/{exit} /^_cleanup_partial_dest\(\) \{/{p=1} p{print}' "$LEVER" > "$FUNCS"
  [ -s "$FUNCS" ]
  OUTSIDE_TARGET="$BATS_TEST_TMPDIR/totally-unrelated-dir"
  mkdir -p "$OUTSIDE_TARGET"
  echo sentinel > "$OUTSIDE_TARGET/sentinel.txt"
  CHECK="$BATS_TEST_TMPDIR/check-cleanup.sh"
  cat > "$CHECK" <<'SCRIPT'
#!/usr/bin/env bash
set -uo pipefail
warn() { echo "WARN: $*" >&2; }
source "$1"
# Simulate an archive_run_ledger call stack: run_dir/branch_slug_final
# describe THIS run's own claimed leaf, which does NOT match $2.
run_dir="/some/other/run/dir"
branch_slug_final="some-other-leaf"
_cleanup_partial_dest "$2"
echo "RC=$?"
SCRIPT
  chmod +x "$CHECK"
  run bash "$CHECK" "$FUNCS" "$OUTSIDE_TARGET"
  [ "$status" -eq 0 ]
  [[ "$output" == *"RC=1"* ]]
  [[ "$output" == *"does not match this run's claimed leaf"* ]]
  # never deleted
  [ -d "$OUTSIDE_TARGET" ]
  [ -f "$OUTSIDE_TARGET/sentinel.txt" ]
}

# --- review-gate round: coverage gap 4/5 — _sanitize_slug traversal + GSD_RUN_ID ---
# A branch name literally cannot contain ".." (git's own check-ref-format
# rejects it outright — verified directly: `git branch "x..y"` fails with
# "not a valid branch name"), so the only realistic path-traversal vector
# here is GSD_RUN_ID, a free-form env var not constrained by git ref rules.
@test "coverage: GSD_RUN_ID containing '../../' path-traversal is sanitized, archive stays under the root" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  ARCHIVE_ROOT="$WORK/.feature-fix-swarm/archive"
  GSD_RUN_ID="../../../../tmp/evil-run" run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ ! -d "$WT" ]
  # no ".." segment anywhere under the archive root
  ! find "$ARCHIVE_ROOT" -mindepth 1 2>/dev/null | grep -q '\.\.'
  find "$ARCHIVE_ROOT" -name evidence.json 2>/dev/null | grep -q .
  # stays contained under .feature-fix-swarm/
  case "$(cd "$ARCHIVE_ROOT" && pwd)" in
    "$(cd "$WORK/.feature-fix-swarm" && pwd)"/*) : ;;
    *) false ;;
  esac
}

@test "coverage: GSD_RUN_ID=spec-005 lands under its own rekeyed run-key dir, not pr<N>" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  GSD_RUN_ID="spec-005" run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ ! -d "$WT" ]
  ARCHIVE_ROOT="$WORK/.feature-fix-swarm/archive"
  [ ! -d "$ARCHIVE_ROOT/pr1" ]
  [ -d "$ARCHIVE_ROOT/spec-005" ]
  [ -f "$ARCHIVE_ROOT/spec-005/feat-x/evidence.json" ]
}

@test "coverage: GSD_RUN_ID with slashes and dots is sanitized into its own run-key dir, not pr<N>" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  GSD_RUN_ID="spec/123.review" run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ ! -d "$WT" ]
  ARCHIVE_ROOT="$WORK/.feature-fix-swarm/archive"
  [ ! -d "$ARCHIVE_ROOT/pr1" ]
  [ -d "$ARCHIVE_ROOT/spec-123.review" ]
  [ -f "$ARCHIVE_ROOT/spec-123.review/feat-x/evidence.json" ]
}

# --- review-gate round: coverage gap 6 — numeric disambiguation loop ---
@test "coverage: numeric disambiguation loop is entered when both the plain slug and hash8 leaf are occupied" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  ARCHIVE_ROOT="$WORK/.feature-fix-swarm/archive/pr1"
  mkdir -p "$ARCHIVE_ROOT/feat-x"
  echo stub > "$ARCHIVE_ROOT/feat-x/stub.txt"
  HASH8="$(printf '%s' 'feat/x' | shasum -a 256 2>/dev/null | awk '{print substr($1,1,8)}')"
  [ -n "$HASH8" ] || HASH8="$(printf '%s' 'feat/x' | cksum | awk '{printf "%08x", $1}')"
  mkdir -p "$ARCHIVE_ROOT/feat-x-$HASH8"
  echo stub > "$ARCHIVE_ROOT/feat-x-$HASH8/stub.txt"
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ ! -d "$WT" ]
  [[ "$output" == *"collision"* ]]
  [ -f "$ARCHIVE_ROOT/feat-x-$HASH8-2/evidence.json" ]
}

@test "coverage: numeric disambiguation exhaustion (all 21 candidates occupied) refuses accurately, worktree kept" {
  mock_gh_merged
  WT="$BATS_TEST_TMPDIR/wt-feat"
  git worktree add -q "$WT" feat/x
  mkdir -p "$WT/.feature-fix-swarm"
  echo '{"w":"1"}' > "$WT/.feature-fix-swarm/evidence.json"
  ARCHIVE_ROOT="$WORK/.feature-fix-swarm/archive/pr1"
  mkdir -p "$ARCHIVE_ROOT/feat-x"
  HASH8="$(printf '%s' 'feat/x' | shasum -a 256 2>/dev/null | awk '{print substr($1,1,8)}')"
  [ -n "$HASH8" ] || HASH8="$(printf '%s' 'feat/x' | cksum | awk '{printf "%08x", $1}')"
  mkdir -p "$ARCHIVE_ROOT/feat-x-$HASH8"
  for n in $(seq 2 20); do
    mkdir -p "$ARCHIVE_ROOT/feat-x-$HASH8-$n"
  done
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [ -d "$WT" ]
  [[ "$output" == *"could not be resolved after"* ]]
  git show-ref --verify -q refs/heads/feat/x
}

# ---------------------------------------------------------------------------
# AC-009 / GAP 3 — signal semantics of the finisher lock owner (RF-201/RF-202)
#
# Every case below sets HOME to a per-test tmp directory: the finisher lock
# path is derived from HOME at runtime, so this is the only thing standing
# between the suite and the invoking user's real
# ~/.cache/feature-fix-swarm/finisher.lock.
#
# Launch discipline: the finalizer runs in its OWN process group (python3
# os.setsid exec wrapper; macOS ships no setsid binary) and signals are
# delivered to the WHOLE GROUP. Bash defers a trapped signal handler until
# the foreground child (the parked gh stub) exits, so a TERM to the
# finalizer pid alone would sit deferred for the stub's entire sleep; the
# group TERM kills the stub too, the foreground call returns, and the
# deferred handler then fires promptly.
# ---------------------------------------------------------------------------

_rf_setsid_launch() { # $1=output file; launches $LEVER 1 in its own pgid; sets RF_PID
  python3 -c 'import os,sys
os.setsid()
os.execvp(sys.argv[1], sys.argv[1:])' bash "$LEVER" 1 > "$1" 2>&1 &
  RF_PID=$!
}

_rf_parked_gh_stub() { # gh stub: record entry, then park well past the test horizon
  cat > "$MOCK_BIN/gh" <<EOF
#!/usr/bin/env bash
echo entered > "$BATS_TEST_TMPDIR/gh-entered"
sleep 120
exit 0
EOF
  chmod +x "$MOCK_BIN/gh"
}

@test "RF-201: signaled lock owner releases and dies by the signal instead of resuming cleanup" {
  export HOME="$BATS_TEST_TMPDIR/home"
  mkdir -p "$HOME"
  LOCK="$HOME/.cache/feature-fix-swarm/finisher.lock"
  _rf_parked_gh_stub
  OUT="$BATS_TEST_TMPDIR/rf201.out"
  _rf_setsid_launch "$OUT"
  # park point: inside the first gh call, strictly after lock acquisition
  for _ in $(seq 1 100); do
    [ -f "$BATS_TEST_TMPDIR/gh-entered" ] && [ -f "$LOCK" ] && break
    sleep 0.2
  done
  [ -f "$BATS_TEST_TMPDIR/gh-entered" ]
  [ -f "$LOCK" ]
  kill -TERM -- "-$RF_PID"
  st=0; wait "$RF_PID" || st=$?
  [ "$st" -eq 143 ]                       # 128+15: died BY the signal, not exit 0
  ! grep -q "MERGED — finalizing" "$OUT"  # no post-gh cleanup output
  [ ! -f "$LOCK" ]                        # ownership released on the way out
}

@test "RF-202: after the owner is signaled away, a second finalizer acquires and is the only cleanup actor" {
  export HOME="$BATS_TEST_TMPDIR/home"
  mkdir -p "$HOME"
  LOCK="$HOME/.cache/feature-fix-swarm/finisher.lock"
  _rf_parked_gh_stub
  OUT="$BATS_TEST_TMPDIR/rf202-first.out"
  _rf_setsid_launch "$OUT"
  for _ in $(seq 1 100); do
    [ -f "$BATS_TEST_TMPDIR/gh-entered" ] && [ -f "$LOCK" ] && break
    sleep 0.2
  done
  [ -f "$LOCK" ]
  kill -TERM -- "-$RF_PID"
  st=0; wait "$RF_PID" || st=$?
  [ ! -f "$LOCK" ]
  # first finalizer performed no cleanup: branch survives
  git show-ref --verify -q refs/heads/feat/x
  ! grep -q "deleted local branch" "$OUT"
  # second finalizer with a normal gh stub is the only cleanup actor
  mock_gh_merged
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [[ "$output" == *"finalize complete"* ]]
  [[ "$output" == *"deleted local branch 'feat/x'"* ]]
  [ ! -f "$LOCK" ]
}

# ---------------------------------------------------------------------------
# AC-009 / GAP 5 — the finalizer-adapter lock matrix (RF-210..RF-216).
# lib-lock.bats proves the helper in isolation; these prove the same
# transitions THROUGH run-finalizer.sh: bounded wait, marked yield, default
# and override bounds, stale reclaim, tamper refusal, event-failure refusal,
# and the attribution chain.
#
# Caller inventory (grep of skills/ and scripts/, 2026-08-08):
#   - skills/feature-implement/SKILL.md finish tail — invokes
#     `bash scripts/gsd/run-finalizer.sh <pr-number>` fail-soft; tolerates a
#     pre-mutation nonzero (78 is a new exit class on that seam).
#   - scripts/coord/forbidden-paths-check.sh — path REFERENCE only, no exec.
#   - scripts/gsd/plan-wall.sh — comment reference only, no exec.
# Every case sets HOME to a per-test tmp dir (the lock path derives from
# HOME at runtime) so the invoking user's real finisher.lock is untouched.
# ---------------------------------------------------------------------------

_rf_fixture_home() { # fixture HOME + lock/holder helpers for the matrix
  export HOME="$BATS_TEST_TMPDIR/home"
  mkdir -p "$HOME/.cache/feature-fix-swarm"
  LOCK="$HOME/.cache/feature-fix-swarm/finisher.lock"
  MACHINE="$(hostname 2>/dev/null || uname -n)"
}

_rf_hold_lock_live() { # seed the lock with a live holder process; sets HOLDER_PID
  sleep 120 &
  HOLDER_PID=$!
  printf '%s\nmachine=%s\nclaimed_epoch=%s\n' "$HOLDER_PID" "$MACHINE" "$(date +%s)" > "$LOCK"
}

_rf_sentinel_gh_stub() { # gh stub that records any invocation — cleanup marker
  cat > "$MOCK_BIN/gh" <<EOF
#!/usr/bin/env bash
echo invoked >> "$BATS_TEST_TMPDIR/gh-sentinel"
exit 64
EOF
  chmod +x "$MOCK_BIN/gh"
}

teardown_rf_holder() { [ -n "${HOLDER_PID:-}" ] && kill "$HOLDER_PID" 2>/dev/null; wait "$HOLDER_PID" 2>/dev/null || true; }

@test "RF-210: contender against a live holder yields at its bound with one marked row and zero cleanup" {
  _rf_fixture_home
  _rf_hold_lock_live
  _rf_sentinel_gh_stub
  STORE="$BATS_TEST_TMPDIR/rf210-store.json"
  run env GATES_STORE="$STORE" FINISHER_LOCK_WAIT=1 bash "$LEVER" --run-id spec-rf210 7
  teardown_rf_holder
  [ "$status" -eq 0 ]
  [ ! -f "$BATS_TEST_TMPDIR/gh-sentinel" ]                # no gh cleanup ran
  git show-ref --verify -q refs/heads/feat/x              # no branch mutation
  [ -f .planning/run-state/gsd-run.pid ]                  # no run-state cleanup
  [ -d "$BATS_TEST_TMPDIR/work/.feature-fix-swarm" ]      # no archive mutation
  [[ "$output" == *"finisher-skipped run_id=spec-rf210 pr=7"* ]]
  run python3 -c "
import json
d = json.load(open('$STORE'))
rows = [e for e in d['events'] if e['kind'] == 'finisher-skipped']
assert len(rows) == 1, rows
assert rows[0]['run_id'] == 'spec-rf210' and rows[0]['pr'] == 7, rows"
  [ "$status" -eq 0 ]
}

@test "RF-211: the wait bound defaults to 60 — pinned in source and by a contender still waiting seconds in" {
  _rf_fixture_home
  _rf_hold_lock_live
  _rf_sentinel_gh_stub
  # source pin: the comment-stripped default in the wait normalization line
  grep -vE '^\s*#' "$LEVER" | grep -q 'FINISHER_LOCK_WAIT:-60'
  # behavioral pin: no override -> still waiting (not yielded) several seconds in
  env GATES_STORE="$BATS_TEST_TMPDIR/rf211-store.json" bash "$LEVER" 7 > "$BATS_TEST_TMPDIR/rf211.out" 2>&1 &
  CONTENDER=$!
  sleep 3
  kill -0 "$CONTENDER" 2>/dev/null
  alive=$?
  kill "$CONTENDER" 2>/dev/null; wait "$CONTENDER" 2>/dev/null || true
  teardown_rf_holder
  [ "$alive" -eq 0 ]
  [ ! -f "$BATS_TEST_TMPDIR/gh-sentinel" ]
}

@test "RF-212: a non-numeric FINISHER_LOCK_WAIT normalizes to the default instead of yielding immediately" {
  _rf_fixture_home
  _rf_hold_lock_live
  _rf_sentinel_gh_stub
  env GATES_STORE="$BATS_TEST_TMPDIR/rf212-store.json" FINISHER_LOCK_WAIT=abc bash "$LEVER" 7 > "$BATS_TEST_TMPDIR/rf212.out" 2>&1 &
  CONTENDER=$!
  sleep 3
  kill -0 "$CONTENDER" 2>/dev/null
  alive=$?
  kill "$CONTENDER" 2>/dev/null; wait "$CONTENDER" 2>/dev/null || true
  teardown_rf_holder
  [ "$alive" -eq 0 ]
  ! grep -q "finisher-skipped" "$BATS_TEST_TMPDIR/rf212.out"
}

@test "RF-213: a dead-pid lock is reclaimed and the finalizer proceeds normally" {
  _rf_fixture_home
  # dead holder: a real pid that has provably exited
  sleep 0.01 &
  DEAD_PID=$!
  wait "$DEAD_PID" 2>/dev/null || true
  printf '%s\nmachine=%s\nclaimed_epoch=%s\n' "$DEAD_PID" "$MACHINE" "$(date +%s)" > "$LOCK"
  mock_gh_merged
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  [[ "$output" == *"finalize complete"* ]]
  [ ! -f "$LOCK" ]
}

@test "RF-214: a symlinked lock path returns 78 with no cleanup" {
  _rf_fixture_home
  touch "$BATS_TEST_TMPDIR/decoy"
  ln -s "$BATS_TEST_TMPDIR/decoy" "$LOCK"
  _rf_sentinel_gh_stub
  run bash "$LEVER" 7
  [ "$status" -eq 78 ]
  [ ! -f "$BATS_TEST_TMPDIR/gh-sentinel" ]
  git show-ref --verify -q refs/heads/feat/x
}

@test "RF-215: a failed finisher-skipped event write returns 78 with no cleanup and no marked-exit notice" {
  [ "$(id -u)" -ne 0 ] || skip "root can write anywhere; unwritable-store fixture is meaningless"
  _rf_fixture_home
  _rf_hold_lock_live
  _rf_sentinel_gh_stub
  RO="$BATS_TEST_TMPDIR/ro-store"
  mkdir -p "$RO"
  chmod 555 "$RO"
  CHMOD_RESTORE_PATHS+=("$RO")
  run env GATES_STORE="$RO/store.json" FINISHER_LOCK_WAIT=1 bash "$LEVER" --run-id spec-rf215 7
  teardown_rf_holder
  [ "$status" -eq 78 ]
  [ ! -f "$BATS_TEST_TMPDIR/gh-sentinel" ]
  ! grep -q "finisher-skipped run_id=" <<<"$output"
  git show-ref --verify -q refs/heads/feat/x
}

@test "RF-216: yield attribution resolves --run-id, then GSD_RUN_ID, then the unattributed trace fallback" {
  _rf_fixture_home
  _rf_hold_lock_live
  _rf_sentinel_gh_stub
  SA="$BATS_TEST_TMPDIR/rf216-a.json"; SB="$BATS_TEST_TMPDIR/rf216-b.json"; SC="$BATS_TEST_TMPDIR/rf216-c.json"
  run env GATES_STORE="$SA" FINISHER_LOCK_WAIT=1 bash "$LEVER" --run-id spec-rfa 7
  [ "$status" -eq 0 ]
  run env GATES_STORE="$SB" FINISHER_LOCK_WAIT=1 GSD_RUN_ID=spec-rfb bash "$LEVER" 7
  [ "$status" -eq 0 ]
  run env GATES_STORE="$SC" FINISHER_LOCK_WAIT=1 bash "$LEVER" 7
  teardown_rf_holder
  [ "$status" -eq 0 ]
  run python3 -c "
import json
a = json.load(open('$SA'))['events'][0]
b = json.load(open('$SB'))['events'][0]
c = json.load(open('$SC'))['events'][0]
assert a['run_id'] == 'spec-rfa', a
assert b['run_id'] == 'spec-rfb', b
assert c['run_id'] == 'unattributed', c
assert c['lock_path'] == '$LOCK', c
assert c['holder_pid'] == $HOLDER_PID, c"
  [ "$status" -eq 0 ]
}

# ── spec-008 04-02: G12 digest seam at the finalizer tail (REQ-701) ─────────

@test "finalizer tail emits the immediate digest before finalize complete (presence-guarded, fail-soft)" {
  mock_gh_merged
  export RUN_STATE_DB="$BATS_TEST_TMPDIR/digest-runs.db"
  # seed one waiver into the fixture repo's common-dir store — digest.sh
  # resolves the store from cwd's git-common-dir, so this stays fixture-local
  python3 - "$WORK/.feature-fix-swarm/evidence.json" <<'EOF'
import json, sys
open(sys.argv[1], "w").write(json.dumps({"waivers": [
    {"run_id": "spec-008", "gate": "g", "env_var": "E=1", "ts": 1.0}]}))
EOF
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  echo "$output" | grep -q '^waiver run_id=spec-008'
  echo "$output" | grep -q 'finalize complete'
}
