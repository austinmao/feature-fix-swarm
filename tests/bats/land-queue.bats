#!/usr/bin/env bats
# Wave-0 lifecycle contract.  Real local Git only; no first-party PATH shadows.
# Plan 02-01 completed [PATH-003]; plan 02-02 completed [RESUME], [RED-ITEM],
# [SYSTEMIC], and [EDGE-007] into GREEN-side assertions; every other selector
# stays a typed RED contract for 02-03.

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

stub_env() {
  MOCK_BIN="$BATS_TEST_TMPDIR/boundaries"; mkdir -p "$MOCK_BIN"
  export CALL_LOG="$BATS_TEST_TMPDIR/calls"; : > "$CALL_LOG"
  export EVENTS="$BATS_TEST_TMPDIR/events"; : > "$EVENTS"
  export GH_ORIGIN="$ORIGIN"
  export GH_STATE="$BATS_TEST_TMPDIR/gh-state"; mkdir -p "$GH_STATE"
  export GATES_STORE="$BATS_TEST_TMPDIR/gates/evidence.json"; mkdir -p "$BATS_TEST_TMPDIR/gates"
}


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
@test "[RESUME] crash after merge reconciles without second merge" {
  test -f "$LINKED/.git"
  stub_env
  git checkout -qb spec/item-a main
  echo alpha > a.txt; git add -- a.txt; git commit -qm item-a; git push -q origin spec/item-a
  git checkout -qb spec/item-b main
  echo beta > b.txt; git add -- b.txt; git commit -qm item-b; git push -q origin spec/item-b
  git checkout -q main
  OID_A="$(git --git-dir "$ORIGIN" rev-parse refs/heads/spec/item-a)"
  OID_B="$(git --git-dir "$ORIGIN" rev-parse refs/heads/spec/item-b)"

  LQ="$BATS_TEST_TMPDIR/gates/land-queue"; mkdir -p "$LQ"
  JR="$ROOT/skills/land-queue/scripts/queue-journal.py"
  RUN_ID=resume1
  python3 "$JR" init --store "$LQ" --queue-id "$RUN_ID" --run-id "$RUN_ID"
  # Crash simulation (REQ-208 / 8c88ebfa): two merge intents journaled with
  # their idempotency keys, results never observed.  item-a's merge actually
  # happened at the authority; item-b's never ran.
  python3 "$JR" append --store "$LQ" --queue-id "$RUN_ID" \
    --kind intent --step merge --item spec/item-a --pr 301 --head "$OID_A"
  python3 "$JR" append --store "$LQ" --queue-id "$RUN_ID" \
    --kind intent --step merge --item spec/item-b --pr 302 --head "$OID_B"
  MERGE_A="$(git --git-dir "$ORIGIN" rev-parse refs/heads/main)"
  printf '%s\n' "$MERGE_A" > "$GH_STATE/merge-301"
  : > "$GH_STATE/merge-302"

  cat > "$MOCK_BIN/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' gh "$@" >> "${CALL_LOG:?}"
printf 'gh %s\n' "$*" >> "${EVENTS:?}"
case "${1:-} ${2:-}" in
  "pr view")
    target="$3"; shift 3
    [ "$*" = "--json mergeCommit -q .mergeCommit.oid" ] || { echo "UNSTUBBED-BOUNDARY:$*" >&2; exit 64; }
    cat "${GH_STATE:?}/merge-$target" ;;
  "pr merge")
    echo "SECOND-MERGE-FORBIDDEN" >&2; exit 97 ;;
  *) echo "UNSTUBBED-BOUNDARY:$*" >&2; exit 64 ;;
esac
STUB
  for b in codex claude feature-implement assert-merged.sh run-finalizer.sh; do
    printf '#!/usr/bin/env bash\nprintf "%%s\\0" "$0" "$@" >> "${CALL_LOG:?}"\nexit 64\n' > "$MOCK_BIN/$b"
  done
  chmod +x "$MOCK_BIN"/*

  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id "$RUN_ID" --resume "$RUN_ID"
  [ "$status" -eq 0 ]

  # Reconciled against merge authority: NO second merge attempt anywhere.
  ! grep -q "gh pr merge" "$EVENTS"
  grep -q "ITEM spec/item-a LANDED $MERGE_A" <<<"$output"
  grep -q "ITEM spec/item-b BLOCKED:resume-incomplete" <<<"$output"

  python3 - "$LQ/$RUN_ID.json" <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1]))
ev = doc["events"]
a = [f'{e["kind"]}:{e["step"]}:{e.get("status","")}' for e in ev if e.get("item") == "spec/item-a"]
assert "result:merge:reconciled" in a, a
assert a[-1].startswith("terminal:terminal:LANDED"), a
b = [f'{e["kind"]}:{e["step"]}:{e.get("status","")}' for e in ev if e.get("item") == "spec/item-b"]
assert "result:merge:never-ran" in b, b
# append-only: the crashed intents are still present and ordered
seqs = [e["seq"] for e in ev]
assert seqs == sorted(seqs), seqs
assert [e["kind"] for e in ev].count("intent") == 2
PYEOF
  # The resume held and released the single-flight owner lock.
  [ ! -f "$LQ/queue.lock" ]
}
@test "[EDGE-005] item-start precheck recognizes external landing" { red EDGE-005 "item-start authority recheck absent"; }
@test "[EDGE-007] parallel option refuses before lane launch" {
  test -f "$LINKED/.git"
  stub_env
  for b in gh codex claude feature-implement assert-merged.sh run-finalizer.sh; do
    printf '#!/usr/bin/env bash\nprintf "%%s\\0" "$0" "$@" >> "${CALL_LOG:?}"\nexit 64\n' > "$MOCK_BIN/$b"
    chmod +x "$MOCK_BIN/$b"
  done
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main --parallel 2 spec/queue
  [ "$status" -eq 2 ]
  [ "$output" = "PARALLEL-UNSUPPORTED:v1-serial-only" ]
  [ ! -s "$CALL_LOG" ]
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main --parallel=4 spec/queue
  [ "$status" -eq 2 ]
  [ "$output" = "PARALLEL-UNSUPPORTED:v1-serial-only" ]
  [ ! -s "$CALL_LOG" ]
}
@test "[EDGE-010] quarantine requeues once after base advance" { red EDGE-010 "bounded quarantine requeue absent"; }
@test "[RED-ITEM] first dispatch follows rebase and autonomous implement" {
  test -f "$LINKED/.git"
  git -C "$WORK" diff --name-only main...spec/queue | grep -qx item.txt
  stub_env
  # A dispatchable branch (spec/queue itself is held by the linked worktree).
  git checkout -qb spec/item-a main
  echo alpha > a.txt; git add -- a.txt; git commit -qm item-a; git push -q origin spec/item-a
  git checkout -q main
  cat > "$MOCK_BIN/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' gh "$@" >> "${CALL_LOG:?}"
echo "UNSTUBBED-BOUNDARY:$*" >&2; exit 64
STUB
  cat > "$MOCK_BIN/feature-implement" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' feature-implement "$@" >> "${CALL_LOG:?}"
printf 'implement %s\n' "$*" >> "${EVENTS:?}"
echo "red item: unit tests failed in suite alpha" >&2
exit 1
STUB
  cat > "$MOCK_BIN/codex" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' codex "$@" >> "${CALL_LOG:?}"
echo "UNSTUBBED-BOUNDARY:$*" >&2; exit 64
STUB
  chmod +x "$MOCK_BIN"/*

  RUN_ID=reditem
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main --run-id "$RUN_ID" spec/item-a
  [ "$status" -eq 0 ]

  # The red item is a terminal item block, never a queue abort.
  grep -q "ITEM spec/item-a BLOCKED:implement" <<<"$output"
  ! grep -q "QUEUE-ABORTED" <<<"$output"

  # Dispatch order: rebase intent+result precede the implement intent, and
  # the implement child ran exactly once, autonomously.
  grep -qx "implement spec/item-a --autonomous" "$EVENTS"
  [ "$(grep -c '^implement ' "$EVENTS")" -eq 1 ]
  python3 - "$BATS_TEST_TMPDIR/gates/land-queue/$RUN_ID.json" <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1]))
names = [f'{e["kind"]}:{e["step"]}' for e in doc["events"] if e.get("item") == "spec/item-a"]
assert names.index("intent:rebase") < names.index("result:rebase") < names.index("intent:implement"), names
assert "result:implement" in names, names
term = [e for e in doc["events"] if e["kind"] == "terminal" and e.get("item") == "spec/item-a"]
assert term and term[0]["status"] == "BLOCKED:implement", term
PYEOF
}
@test "[SYSTEMIC] only enumerated consecutive classes circuit break" {
  test -f "$LINKED/.git"
  stub_env
  git checkout -qb spec/item-a main
  echo alpha > a.txt; git add -- a.txt; git commit -qm item-a; git push -q origin spec/item-a
  git checkout -qb spec/item-b main
  echo beta > b.txt; git add -- b.txt; git commit -qm item-b; git push -q origin spec/item-b
  git checkout -qb spec/item-c main
  echo gamma > c.txt; git add -- c.txt; git commit -qm item-c; git push -q origin spec/item-c
  git checkout -q main

  cat > "$MOCK_BIN/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' gh "$@" >> "${CALL_LOG:?}"
printf 'gh %s\n' "$*" >> "${EVENTS:?}"
case "${1:-} ${2:-}" in
  "pr view")
    target="$3"; shift 3
    case "$* $target" in
      "--json number -q .number spec/item-a") echo 201 ;;
      "--json number -q .number spec/item-b")
        # second systemic observation: the gh boundary is unauthenticated
        echo "gh: Not logged in to any GitHub hosts" >&2
        exit 1 ;;
      "--json headRefOid -q .headRefOid 201")
        git --git-dir "${GH_ORIGIN:?}" rev-parse refs/heads/spec/item-a ;;
      *) echo "UNSTUBBED-BOUNDARY:view $target $*" >&2; exit 64 ;;
    esac ;;
  *) echo "UNSTUBBED-BOUNDARY:$*" >&2; exit 64 ;;
esac
STUB
  cat > "$MOCK_BIN/feature-implement" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' feature-implement "$@" >> "${CALL_LOG:?}"
printf 'implement %s\n' "$*" >> "${EVENTS:?}"
exit 0
STUB
  cat > "$MOCK_BIN/codex" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' codex "$@" >> "${CALL_LOG:?}"
printf 'codex %s\n' "$*" >> "${EVENTS:?}"
# first systemic observation: the reviewer wall-clock times out (rc 124)
exit 124
STUB
  chmod +x "$MOCK_BIN"/*

  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id sysa spec/item-a spec/item-b spec/item-c
  [ "$status" -eq 1 ]

  # 6e4616bc stricter reading: two CONSECUTIVE enumerated systemic failures
  # abort even across DIFFERENT classes (reviewer-unreachable then gh-auth).
  grep -q "QUEUE-ABORTED:systemic:gh-auth" <<<"$output"
  grep -q "ITEM spec/item-a BLOCKED:reviewer-unreachable" <<<"$output"
  grep -q "ITEM spec/item-b BLOCKED:gh-auth" <<<"$output"
  # The third item is never dispatched after the circuit breaks.
  ! grep -q "implement spec/item-c" "$EVENTS"

  # Item-local defect classes NEVER abort, however many items hit them.
  cat > "$MOCK_BIN/codex" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' codex "$@" >> "${CALL_LOG:?}"
printf 'codex %s\n' "$*" >> "${EVENTS:?}"
echo "2 blocking review findings" >&2
exit 2
STUB
  cat > "$MOCK_BIN/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' gh "$@" >> "${CALL_LOG:?}"
printf 'gh %s\n' "$*" >> "${EVENTS:?}"
case "${1:-} ${2:-}" in
  "pr view")
    target="$3"; shift 3
    case "$* $target" in
      "--json number -q .number spec/item-a") echo 201 ;;
      "--json number -q .number spec/item-b") echo 202 ;;
      "--json headRefOid -q .headRefOid 201")
        git --git-dir "${GH_ORIGIN:?}" rev-parse refs/heads/spec/item-a ;;
      "--json headRefOid -q .headRefOid 202")
        git --git-dir "${GH_ORIGIN:?}" rev-parse refs/heads/spec/item-b ;;
      *) echo "UNSTUBBED-BOUNDARY:view $target $*" >&2; exit 64 ;;
    esac ;;
  *) echo "UNSTUBBED-BOUNDARY:$*" >&2; exit 64 ;;
esac
STUB
  chmod +x "$MOCK_BIN"/*
  export GATES_STORE="$BATS_TEST_TMPDIR/gates2/evidence.json"; mkdir -p "$BATS_TEST_TMPDIR/gates2"
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id sysb spec/item-a spec/item-b
  [ "$status" -eq 0 ]
  ! grep -q "QUEUE-ABORTED" <<<"$output"
  grep -q "ITEM spec/item-a BLOCKED:review" <<<"$output"
  grep -q "ITEM spec/item-b BLOCKED:review" <<<"$output"
}
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
