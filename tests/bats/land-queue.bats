#!/usr/bin/env bats
# Wave-0 lifecycle contract.  Real local Git only; no first-party PATH shadows.

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"; QUEUE="$ROOT/scripts/gsd/land-queue.sh"
  ORIGIN="$BATS_TEST_TMPDIR/origin.git"; WORK="$BATS_TEST_TMPDIR/work"; LINKED="$BATS_TEST_TMPDIR/linked"
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
  export GIT_AUTHOR_NAME=wave0 GIT_AUTHOR_EMAIL=wave0@example.invalid GIT_COMMITTER_NAME=wave0 GIT_COMMITTER_EMAIL=wave0@example.invalid
  unset GH_TOKEN GITHUB_TOKEN GIT_ASKPASS
  git init -q --bare "$ORIGIN"; git init -q -b main "$WORK"; cd "$WORK"
  echo base > README.md; git add README.md; git commit -qm base; git remote add origin "$ORIGIN"; git push -q origin main
  git checkout -qb spec/queue; echo work > item.txt; git add item.txt; git commit -qm item; git push -q origin spec/queue
  git checkout -q main; git worktree add -q "$LINKED" spec/queue
}

red() { local tag="$1"; shift; test -f "$LINKED/.git"; git -C "$WORK" diff --name-only main...spec/queue | grep -qx item.txt; printf 'RED-EXPECTED: [%s] %s\n' "$tag" "$*" >&2; [ -f "$QUEUE" ] && bash "$QUEUE" --contract-probe; return 1; }

@test "[PATH-003] serial happy lifecycle drains ordered real-Git items" { red PATH-003 "ordered rebase-dispatch-review-ci-grant-merge lifecycle absent"; }
@test "[PATH-004] red item continues then systemic class aborts and materializes skipped" { red PATH-004 "per-item terminal and systemic continuation contract absent"; }
@test "[RESUME] crash after merge reconciles without second merge" { red RESUME "journal authority resume reconciliation absent"; }
@test "[EDGE-005] item-start precheck recognizes external landing" { red EDGE-005 "item-start authority recheck absent"; }
@test "[EDGE-007] parallel option refuses before lane launch" { red EDGE-007 "PARALLEL-UNSUPPORTED:v1-serial-only absent"; }
@test "[EDGE-010] quarantine requeues once after base advance" { red EDGE-010 "bounded quarantine requeue absent"; }
@test "[RED-ITEM] first dispatch follows rebase and autonomous implement" { red RED-ITEM "rebase then feature-implement ordering absent"; }
@test "[SYSTEMIC] only enumerated consecutive classes circuit break" { red SYSTEMIC "systemic class table absent"; }
@test "[REVIEW] floor and zero reviewer modes bind degradation" { red REVIEW "cross-vendor degradation policy absent"; }
@test "[CI] timeout shim requires literal 1200 and gh watch interval 10" { red CI "bounded CI watch contract absent"; }
@test "[HEAD] merge pins review-time head and refuses drift" { red HEAD "review-time OID head drift refusal absent"; }
@test "[MERGE] no merge runs after failed preconditions" { red MERGE "merge refusal boundary absent"; }
@test "[GRANT] promotion authority runs after review and CI" { red GRANT "grant ordering and authority absent"; }
@test "[FINALIZER] finalizer result precedes LANDED terminal" { red FINALIZER "finalizer intent/result completion absent"; }
@test "[POSTURE] production touch is auditable through review policy" { red POSTURE "posture binding absent"; }
@test "[STOP] STOP marker aborts before a new item or merge" { red STOP "operator STOP boundary absent"; }
@test "[DRAIN] second-process DRAIN stops at next item boundary" { red DRAIN "QUEUE-DRAINED:operator-drain absent"; }
@test "[HUMAN-INBOX] blocked item has reason and one-command unblock" { red HUMAN-INBOX "Human inbox schema absent"; }
@test "[REVERT] landed record contains merge SHA and revert command" { red REVERT "revert evidence schema absent"; }
@test "[PARALLEL] any parallel value is refused exactly" { red PARALLEL "serial-only grammar absent"; }
