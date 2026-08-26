#!/usr/bin/env bats
# Wave-0 lifecycle contract.  Real local Git only; no first-party PATH shadows.
# Plan 02-01 completed the [PATH-003] placeholder into its GREEN-side
# assertions; every other selector stays a typed RED contract for 02-02/02-03.

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

@test "[PATH-003] serial happy lifecycle drains ordered real-Git items" {
  if [ ! -f "$QUEUE" ]; then
    printf 'RED-EXPECTED: [PATH-003] ordered rebase-dispatch-review-ci-grant-merge lifecycle absent\n' >&2
    return 1
  fi
  # Original Wave-0 fixture facts stay asserted.
  test -f "$LINKED/.git"
  git -C "$WORK" diff --name-only main...spec/queue | grep -qx item.txt

  # Two explicit items; item-b carries a hostile-name file corpus.
  git checkout -qb spec/item-a main
  echo alpha > a.txt
  git add -- a.txt
  git commit -qm item-a
  git push -q origin spec/item-a
  git checkout -qb spec/item-b main
  echo beta > "b space.txt"
  echo beta > -dash.txt
  echo beta > "glob*.txt"
  echo beta > "semi;colon.txt"
  echo beta > '$(touch pwned).txt'
  nl_name=$'nl\nname.txt'
  echo beta > "$nl_name"
  git add -- "b space.txt" -dash.txt "glob*.txt" "semi;colon.txt" '$(touch pwned).txt' "$nl_name"
  git commit -qm item-b
  git push -q origin spec/item-b
  git checkout -q main

  MOCK_BIN="$BATS_TEST_TMPDIR/boundaries"; mkdir -p "$MOCK_BIN"
  export CALL_LOG="$BATS_TEST_TMPDIR/calls"; : > "$CALL_LOG"
  export EVENTS="$BATS_TEST_TMPDIR/events"; : > "$EVENTS"
  export GH_ORIGIN="$ORIGIN"
  export GH_STATE="$BATS_TEST_TMPDIR/gh-state"; mkdir -p "$GH_STATE"
  export GATES_STORE="$BATS_TEST_TMPDIR/gates/evidence.json"; mkdir -p "$BATS_TEST_TMPDIR/gates"

  # Deny-by-default vendor/effect-child boundaries; only the exact expected
  # argv shapes are serviced, everything else exits 64.
  cat > "$MOCK_BIN/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' gh "$@" >> "${CALL_LOG:?}"
printf 'gh %s\n' "$*" >> "${EVENTS:?}"
case "${1:-} ${2:-}" in
  "pr view")
    target="$3"; shift 3
    case "$*" in
      "--json number -q .number")
        case "$target" in
          spec/item-a) echo 101 ;;
          spec/item-b) echo 102 ;;
          *) echo "UNSTUBBED-BOUNDARY:view $target" >&2; exit 64 ;;
        esac ;;
      "--json headRefOid -q .headRefOid")
        case "$target" in
          101) git --git-dir "${GH_ORIGIN:?}" rev-parse refs/heads/spec/item-a ;;
          102) git --git-dir "${GH_ORIGIN:?}" rev-parse refs/heads/spec/item-b ;;
          *) echo "UNSTUBBED-BOUNDARY:head $target" >&2; exit 64 ;;
        esac ;;
      "--json mergeCommit -q .mergeCommit.oid")
        cat "${GH_STATE:?}/merge-$target" ;;
      *) echo "UNSTUBBED-BOUNDARY:$*" >&2; exit 64 ;;
    esac ;;
  "pr checks")
    [ "$4" = "--watch" ] && [ "$5" = "--interval" ] && [ "$6" = "10" ] || { echo "UNSTUBBED-BOUNDARY:$*" >&2; exit 64; }
    exit 0 ;;
  "pr merge")
    pr="$3"
    [ "$4" = "--squash" ] && [ "$5" = "--match-head-commit" ] || { echo "UNSTUBBED-BOUNDARY:$*" >&2; exit 64; }
    oid="$6"
    case "$pr" in
      101) b=spec/item-a ;;
      102) b=spec/item-b ;;
      *) echo "UNSTUBBED-BOUNDARY:merge $pr" >&2; exit 64 ;;
    esac
    cur="$(git --git-dir "${GH_ORIGIN:?}" rev-parse "refs/heads/$b")"
    [ "$cur" = "$oid" ] || { echo "GH-MERGE-HEAD-MISMATCH" >&2; exit 1; }
    mainsha="$(git --git-dir "$GH_ORIGIN" rev-parse refs/heads/main)"
    tree="$(git --git-dir "$GH_ORIGIN" merge-tree --write-tree refs/heads/main "$oid")" || exit 1
    msha="$(git --git-dir "$GH_ORIGIN" commit-tree "$tree" -p "$mainsha" -m "squash pr-$pr")"
    git --git-dir "$GH_ORIGIN" update-ref refs/heads/main "$msha"
    printf '%s\n' "$msha" > "${GH_STATE:?}/merge-$pr"
    exit 0 ;;
  *)
    echo "UNSTUBBED-BOUNDARY:$*" >&2; exit 64 ;;
esac
STUB
  cat > "$MOCK_BIN/codex" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' codex "$@" >> "${CALL_LOG:?}"
printf 'codex %s\n' "$*" >> "${EVENTS:?}"
exit 0
STUB
  cat > "$MOCK_BIN/feature-implement" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' feature-implement "$@" >> "${CALL_LOG:?}"
printf 'implement %s\n' "$*" >> "${EVENTS:?}"
exit 0
STUB
  cat > "$MOCK_BIN/assert-merged.sh" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' assert-merged.sh "$@" >> "${CALL_LOG:?}"
printf 'assert-merged %s\n' "$*" >> "${EVENTS:?}"
exit 0
STUB
  cat > "$MOCK_BIN/run-finalizer.sh" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' run-finalizer.sh "$@" >> "${CALL_LOG:?}"
printf 'finalize %s\n' "$*" >> "${EVENTS:?}"
exit 0
STUB
  chmod +x "$MOCK_BIN"/*

  RUN_ID=queue-wave0
  python3 "$ROOT/lib/gates.py" grant "$RUN_ID" --action merge:pr-101 --reason wave0 >/dev/null
  python3 "$ROOT/lib/gates.py" grant "$RUN_ID" --action merge:pr-102 --reason wave0 >/dev/null

  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main --run-id "$RUN_ID" spec/item-a spec/item-b
  [ "$status" -eq 0 ]

  OID_A="$(git --git-dir "$ORIGIN" rev-parse refs/heads/spec/item-a)"
  OID_B="$(git --git-dir "$ORIGIN" rev-parse refs/heads/spec/item-b)"
  MERGE_A="$(cat "$GH_STATE/merge-101")"
  MERGE_B="$(cat "$GH_STATE/merge-102")"

  # Report: two LANDED rows with merge SHAs and an empty Human inbox.
  grep -qx "ITEM spec/item-a LANDED $MERGE_A" <<<"$output"
  grep -qx "ITEM spec/item-b LANDED $MERGE_B" <<<"$output"
  grep -qx "HUMAN-INBOX: empty" <<<"$output"

  # Serial ordered effect log: item-a fully terminal before any item-b effect,
  # bounded CI watch is literal, and every merge pins the review-time OID.
  expected="$BATS_TEST_TMPDIR/expected-events"
  cat > "$expected" <<EOF2
implement spec/item-a --autonomous
codex review 101 $OID_A
gh pr checks 101 --watch --interval 10
gh pr merge 101 --squash --match-head-commit $OID_A
assert-merged 101
finalize --run-id $RUN_ID 101
implement spec/item-b --autonomous
codex review 102 $OID_B
gh pr checks 102 --watch --interval 10
gh pr merge 102 --squash --match-head-commit $OID_B
assert-merged 102
finalize --run-id $RUN_ID 102
EOF2
  grep -E '^(implement|codex|gh pr checks|gh pr merge|assert-merged|finalize)' "$EVENTS" | diff -u "$expected" -

  # Journal: intent precedes every result; precheck runs at item start AND
  # immediately before merge; no second-item event before the first terminal.
  python3 - "$BATS_TEST_TMPDIR/gates/land-queue/$RUN_ID.json" <<'EOF2'
import json, sys
doc = json.load(open(sys.argv[1]))
assert doc["schema"] == 1
ev = doc["events"]
seqs = [e["seq"] for e in ev]
assert seqs == sorted(seqs)
for item in ("spec/item-a", "spec/item-b"):
    names = [f'{e["kind"]}:{e["step"]}' for e in ev if e.get("item") == item]
    assert names.index("intent:precheck") < names.index("intent:rebase") < names.index("intent:implement"), names
    assert names.index("intent:precheck-merge") < names.index("intent:merge"), names
    for step in ("precheck", "rebase", "implement", "push", "review", "ci",
                 "precheck-merge", "merge", "finalize"):
        assert names.index(f"intent:{step}") < names.index(f"result:{step}"), (item, step)
terms = [e for e in ev if e["kind"] == "terminal"]
assert [t["item"] for t in terms] == ["spec/item-a", "spec/item-b"], terms
assert all(t["status"] == "LANDED" and len(t["detail"]) == 40 for t in terms), terms
first_b = min(i for i, e in enumerate(ev) if e.get("item") == "spec/item-b")
term_a = next(i for i, e in enumerate(ev) if e["kind"] == "terminal" and e.get("item") == "spec/item-a")
assert term_a < first_b, (term_a, first_b)
EOF2

  # Real content landed on origin main; hostile names arrive intact; the
  # command-substitution-shaped file name produced no side effect anywhere.
  git --git-dir "$ORIGIN" cat-file -e 'refs/heads/main:a.txt'
  git --git-dir "$ORIGIN" cat-file -e 'refs/heads/main:b space.txt'
  git --git-dir "$ORIGIN" cat-file -e 'refs/heads/main:glob*.txt'
  git --git-dir "$ORIGIN" cat-file -e 'refs/heads/main:semi;colon.txt'
  git --git-dir "$ORIGIN" cat-file -e 'refs/heads/main:$(touch pwned).txt'
  git --git-dir "$ORIGIN" cat-file -e "refs/heads/main:$nl_name"
  [ ! -e pwned ]
  [ ! -e "$BATS_TEST_TMPDIR/pwned" ]
}
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
