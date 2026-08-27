#!/usr/bin/env bats
# Wave-0 lifecycle contract.  Real local Git only; no first-party PATH shadows.
# Plan 02-01 completed [PATH-003]; plan 02-02 completed [RESUME], [RED-ITEM],
# [SYSTEMIC], and [EDGE-007] into GREEN-side assertions; every other selector
# stays a typed RED contract for 02-03.

setup() {
  # Hermetic supported-host identity for every fixture: managed sandboxes
  # deny `ps`/`sysctl`, so the suite supplies its own deterministic process
  # and boot identity (mirrors tests/bats/takeover-check.bats).  Production
  # inertness is proven there by the fail-closed unset-seam denied-PATH case.
  export TAKEOVER_TEST_IDENTITY="bats-boot-1"
  export FFS_HOST=claude
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
  export LQ="$BATS_TEST_TMPDIR/gates/land-queue"
  # Hermetic estate source: inject an empty estate document through the
  # collector's --estate-json seam so fixture repo branches never join the
  # queue uninvited.  The default --use-estate wiring is proven by [INTAKE].
  export LAND_QUEUE_ESTATE_JSON="$BATS_TEST_TMPDIR/estate.json"
  printf '{"branches": []}\n' > "$LAND_QUEUE_ESTATE_JSON"
}

mk_branch() { # $1 branch, $2 file, $3 content — commit on a new branch off main
  git checkout -qb "$1" main
  echo "$3" > "$2"; git add -- "$2"; git commit -qm "$1"; git push -q origin "$1"
  git checkout -q main
}

write_children() { # happy logging stubs for every non-gh effect child
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
}

write_gh() { # full happy gh authority for spec/item-a=101 spec/item-b=102
  cat > "$MOCK_BIN/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' gh "$@" >> "${CALL_LOG:?}"
printf 'gh %s\n' "$*" >> "${EVENTS:?}"
br_of() { case "$1" in 101) echo spec/item-a ;; 102) echo spec/item-b ;; 103) echo spec/item-c ;; *) return 1 ;; esac; }
case "${1:-} ${2:-}" in
  "pr view")
    target="$3"; shift 3
    case "$*" in
      "--json number -q .number")
        case "$target" in
          spec/item-a) echo 101 ;;
          spec/item-b) echo 102 ;;
          spec/item-c) echo 103 ;;
          *) echo "UNSTUBBED-BOUNDARY:view $target" >&2; exit 64 ;;
        esac ;;
      "--json headRefOid -q .headRefOid")
        b="$(br_of "$target")" || { echo "UNSTUBBED-BOUNDARY:head $target" >&2; exit 64; }
        reads="${GH_STATE:?}/headreads-$target"; echo x >> "$reads"
        if [ "${GH_HEAD_DRIFT:-}" = "1" ] && [ "$(wc -l < "$reads")" -ge 2 ]; then
          o1="11111111111111111111"; echo "$o1$o1"
        else
          git --git-dir "${GH_ORIGIN:?}" rev-parse "refs/heads/$b"
        fi ;;
      "--json statusCheckRollup,mergeable"*)
        if [ "${GH_ROLLUP:-}" = "AUTHFAIL" ]; then
          echo "gh: Not logged in to any GitHub hosts" >&2; exit 1
        fi
        echo "${GH_ROLLUP:-1 MERGEABLE}" ;;
      "--json mergeCommit -q .mergeCommit.oid")
        cat "${GH_STATE:?}/merge-$target" ;;
      *) echo "UNSTUBBED-BOUNDARY:$*" >&2; exit 64 ;;
    esac ;;
  "pr checks")
    [ "$4" = "--watch" ] && [ "$5" = "--interval" ] && [ "$6" = "10" ] || { echo "UNSTUBBED-BOUNDARY:$*" >&2; exit 64; }
    [ "${STOP_AT_CI:-}" = "1" ] && touch "${LQ:?}/STOP"
    exit 0 ;;
  "pr merge")
    pr="$3"
    [ "$4" = "--squash" ] && [ "$5" = "--match-head-commit" ] || { echo "UNSTUBBED-BOUNDARY:$*" >&2; exit 64; }
    oid="$6"
    b="$(br_of "$pr")" || { echo "UNSTUBBED-BOUNDARY:merge $pr" >&2; exit 64; }
    cur="$(git --git-dir "${GH_ORIGIN:?}" rev-parse "refs/heads/$b")"
    [ "$cur" = "$oid" ] || { echo "GH-MERGE-HEAD-MISMATCH" >&2; exit 1; }
    mainsha="$(git --git-dir "$GH_ORIGIN" rev-parse refs/heads/main)"
    tree="$(git --git-dir "$GH_ORIGIN" merge-tree --write-tree refs/heads/main "$oid")" || exit 1
    msha="$(git --git-dir "$GH_ORIGIN" commit-tree "$tree" -p "$mainsha" -m "squash pr-$pr")"
    git --git-dir "$GH_ORIGIN" update-ref refs/heads/main "$msha"
    printf '%s\n' "$msha" > "${GH_STATE:?}/merge-$pr"
    exit 0 ;;
  *) echo "UNSTUBBED-BOUNDARY:$*" >&2; exit 64 ;;
esac
STUB
  chmod +x "$MOCK_BIN/gh"
}

no_reviewer_path() { # PATH with no codex/claude anywhere but sane core tools
  rm -f "$MOCK_BIN/codex" "$MOCK_BIN/claude"
  ln -sf "$(command -v python3)" "$MOCK_BIN/python3"
  ln -sf "$(command -v bash)" "$MOCK_BIN/bash"
  printf '%s' "$MOCK_BIN:/usr/bin:/bin:/usr/sbin"
}

write_vendor_stub() { # $1 vendor CLI name — happy logging reviewer stub
  cat > "$MOCK_BIN/$1" <<STUB
#!/usr/bin/env bash
printf '%s\0' $1 "\$@" >> "\${CALL_LOG:?}"
printf '$1 %s\n' "\$*" >> "\${EVENTS:?}"
exit 0
STUB
  chmod +x "$MOCK_BIN/$1"
}

same_vendor_only_path() { # $1 vendor to keep — hermetic PATH with ONLY it
  rm -f "$MOCK_BIN/codex" "$MOCK_BIN/claude"
  write_vendor_stub "$1"
  ln -sf "$(command -v python3)" "$MOCK_BIN/python3"
  ln -sf "$(command -v bash)" "$MOCK_BIN/bash"
  printf '%s' "$MOCK_BIN:/usr/bin:/bin:/usr/sbin"
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
  export LAND_QUEUE_ESTATE_JSON="$BATS_TEST_TMPDIR/estate.json"
  printf '{"branches": []}\n' > "$LAND_QUEUE_ESTATE_JSON"

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
      "--json statusCheckRollup,mergeable"*)
        echo "1 MERGEABLE" ;;
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
@test "[PATH-004] red item continues then systemic class aborts and materializes skipped" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  mk_branch spec/item-b b.txt beta
  mk_branch spec/item-c c.txt gamma
  write_gh
  write_children
  cat > "$MOCK_BIN/feature-implement" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' feature-implement "$@" >> "${CALL_LOG:?}"
printf 'implement %s\n' "$*" >> "${EVENTS:?}"
case "$1" in
  spec/item-b) echo "red gate failure in suite beta" >&2; exit 1 ;;
esac
exit 0
STUB
  chmod +x "$MOCK_BIN"/*

  # Scenario A — REQ-207: a twice-failing local gate is BLOCKED:no-progress
  # while items one and three still land (local weather never stops the queue).
  RUN_ID=p4a
  python3 "$ROOT/lib/gates.py" grant "$RUN_ID" --action merge:pr-101 --reason t >/dev/null
  python3 "$ROOT/lib/gates.py" grant "$RUN_ID" --action merge:pr-103 --reason t >/dev/null
  # First observation of the same normalized signature (round 1 history).
  python3 "$ROOT/lib/gates.py" note-failure "queue:$RUN_ID:spec/item-b" \
    --sig "implement|red gate failure in suite beta" >/dev/null
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id "$RUN_ID" spec/item-a spec/item-b spec/item-c
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-a LANDED" <<<"$output"
  grep -q "ITEM spec/item-b BLOCKED:no-progress" <<<"$output"
  grep -q "ITEM spec/item-c LANDED" <<<"$output"
  ! grep -q "QUEUE-ABORTED" <<<"$output"
  grep -qx "implement spec/item-c --autonomous" "$EVENTS"

  # Scenario B — REQ-206 integration: two consecutive systemic classes abort
  # and every untouched item is materialized SKIPPED:queue-aborted.
  export GATES_STORE="$BATS_TEST_TMPDIR/gates-b/evidence.json"; mkdir -p "$BATS_TEST_TMPDIR/gates-b"
  export LQ="$BATS_TEST_TMPDIR/gates-b/land-queue"
  : > "$EVENTS"
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
exit 124
STUB
  cat > "$MOCK_BIN/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' gh "$@" >> "${CALL_LOG:?}"
printf 'gh %s\n' "$*" >> "${EVENTS:?}"
case "${1:-} ${2:-}" in
  "pr view")
    target="$3"; shift 3
    case "$* $target" in
      "--json number -q .number spec/item-a") echo 101 ;;
      "--json number -q .number spec/item-b")
        echo "gh: Not logged in to any GitHub hosts" >&2; exit 1 ;;
      "--json headRefOid -q .headRefOid 101")
        git --git-dir "${GH_ORIGIN:?}" rev-parse refs/heads/spec/item-a ;;
      *) echo "UNSTUBBED-BOUNDARY:view $target $*" >&2; exit 64 ;;
    esac ;;
  *) echo "UNSTUBBED-BOUNDARY:$*" >&2; exit 64 ;;
esac
STUB
  chmod +x "$MOCK_BIN"/*
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id p4b spec/item-a spec/item-b spec/item-c
  [ "$status" -eq 1 ]
  grep -q "QUEUE-ABORTED:systemic:gh-auth" <<<"$output"
  grep -q "ITEM spec/item-a BLOCKED:reviewer-unreachable" <<<"$output"
  grep -q "ITEM spec/item-b BLOCKED:gh-auth" <<<"$output"
  grep -q "ITEM spec/item-c SKIPPED:queue-aborted" <<<"$output"
  ! grep -q "implement spec/item-c" "$EVENTS"

  # Scenario C — REQ-208: --resume never re-executes a LANDED item.
  : > "$EVENTS"
  mkdir -p "$LQ"
  JR="$ROOT/skills/land-queue/scripts/queue-journal.py"
  python3 "$JR" init --store "$LQ" --queue-id p4c --run-id p4c
  landed_sha="$(git --git-dir "$ORIGIN" rev-parse refs/heads/main)"
  python3 "$JR" append --store "$LQ" --queue-id p4c \
    --kind terminal --step terminal --item spec/item-a --status LANDED --detail "$landed_sha"
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id p4c --resume p4c
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-a LANDED $landed_sha" <<<"$output"
  ! grep -q "^gh " "$EVENTS"
  ! grep -q "^implement " "$EVENTS"
}
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
    --kind intent --step merge --item spec/item-a --pr 101 --head "$OID_A"
  python3 "$JR" append --store "$LQ" --queue-id "$RUN_ID" \
    --kind intent --step merge --item spec/item-b --pr 102 --head "$OID_B"
  MERGE_A="$(git --git-dir "$ORIGIN" rev-parse refs/heads/main)"
  printf '%s\n' "$MERGE_A" > "$GH_STATE/merge-101"
  : > "$GH_STATE/merge-102"

  write_gh
  write_children
  # assert-merged.sh answers per-PR from the same authority state gh serves;
  # resume must consult the merged-state authority BEFORE deciding whether
  # an effect happened (02-03 key link: resume reconciliation before retry).
  cat > "$MOCK_BIN/assert-merged.sh" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' assert-merged.sh "$@" >> "${CALL_LOG:?}"
printf 'assert-merged %s\n' "$*" >> "${EVENTS:?}"
[ -s "${GH_STATE:?}/merge-$1" ] && exit 0
exit 2
STUB
  chmod +x "$MOCK_BIN"/*
  python3 "$ROOT/lib/gates.py" grant "$RUN_ID" --action merge:pr-102 --reason t >/dev/null

  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id "$RUN_ID" --resume "$RUN_ID"
  [ "$status" -eq 0 ]

  # item-a: merge authority satisfied — reconciled LANDED, NO second merge.
  grep -q "ITEM spec/item-a LANDED $MERGE_A" <<<"$output"
  grep -q "assert-merged 101" "$EVENTS"
  ! grep -q "gh pr merge 101" "$EVENTS"

  # item-b: the merge effect provably never happened — resume re-enters the
  # normal serial lifecycle and retries it to LANDED instead of parking it.
  grep -q "assert-merged 102" "$EVENTS"
  MERGE_B="$(cat "$GH_STATE/merge-102")"
  [ -n "$MERGE_B" ]
  grep -q "ITEM spec/item-b LANDED $MERGE_B" <<<"$output"
  ! grep -q "BLOCKED:resume-incomplete" <<<"$output"
  [ "$(grep -c "gh pr merge 102" "$EVENTS")" -eq 1 ]

  python3 - "$LQ/$RUN_ID.json" <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1]))
ev = doc["events"]
a = [f'{e["kind"]}:{e["step"]}:{e.get("status","")}' for e in ev if e.get("item") == "spec/item-a"]
assert "result:merge:reconciled" in a, a
assert a[-1].startswith("terminal:terminal:LANDED"), a
b = [f'{e["kind"]}:{e["step"]}:{e.get("status","")}' for e in ev if e.get("item") == "spec/item-b"]
assert "result:merge:never-ran" in b, b
# the retry drove the FULL lifecycle: precheck through finalize, then LANDED
for step in ("precheck", "rebase", "implement", "push", "review", "ci", "merge", "finalize"):
    assert f"intent:{step}:" in b, (step, b)
assert b[-1].startswith("terminal:terminal:LANDED"), b
# append-only: the crashed intents are still present and ordered
seqs = [e["seq"] for e in ev]
assert seqs == sorted(seqs), seqs
PYEOF
  # The resume held and released the single-flight owner lock.
  [ ! -f "$LQ/queue.lock" ]
}
@test "[RESUME] non-dangling nonterminal item re-enters the lifecycle" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  mkdir -p "$LQ"
  JR="$ROOT/skills/land-queue/scripts/queue-journal.py"
  RUN_ID=resume2
  python3 "$JR" init --store "$LQ" --queue-id "$RUN_ID" --run-id "$RUN_ID"
  # Crash BETWEEN steps: rebase intent AND result journaled, no dangling
  # intent, no terminal — a dangling-intent reader alone never surfaces this
  # item, yet REQ-208 requires resume to enumerate every nonterminal item.
  python3 "$JR" append --store "$LQ" --queue-id "$RUN_ID" \
    --kind intent --step rebase --item spec/item-a
  python3 "$JR" append --store "$LQ" --queue-id "$RUN_ID" \
    --kind result --step rebase --item spec/item-a --status ok
  python3 "$ROOT/lib/gates.py" grant "$RUN_ID" --action merge:pr-101 --reason t >/dev/null
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id "$RUN_ID" --resume "$RUN_ID"
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-a LANDED" <<<"$output"
  ! grep -q "BLOCKED:resume-incomplete" <<<"$output"
  python3 - "$LQ/$RUN_ID.json" <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1]))
ev = [e for e in doc["events"] if e.get("item") == "spec/item-a"]
names = [f'{e["kind"]}:{e["step"]}' for e in ev]
# the retry re-entered the full lifecycle from its start
assert names.count("intent:precheck") == 1, names
assert "intent:merge" in names, names
assert ev[-1]["kind"] == "terminal" and ev[-1]["status"] == "LANDED", ev[-1]
PYEOF
}
@test "[EDGE-005] item-start precheck recognizes external landing" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  mk_branch spec/item-b b.txt beta
  mk_branch spec/item-c c.txt gamma
  OID_B="$(git --git-dir "$ORIGIN" rev-parse refs/heads/spec/item-b)"
  write_gh
  write_children
  export WORK_REPO="$WORK"
  # item-a's implement child simulates the outside world: item-b is merged
  # externally (merge commit, head stays reachable) and item-c simply
  # vanishes with no landing proof.
  cat > "$MOCK_BIN/feature-implement" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' feature-implement "$@" >> "${CALL_LOG:?}"
printf 'implement %s\n' "$*" >> "${EVENTS:?}"
if [ "$1" = "spec/item-a" ]; then
  ibhead="$(git --git-dir "${GH_ORIGIN:?}" rev-parse refs/heads/spec/item-b)"
  mainsha="$(git --git-dir "$GH_ORIGIN" rev-parse refs/heads/main)"
  tree="$(git --git-dir "$GH_ORIGIN" merge-tree --write-tree refs/heads/main "$ibhead")"
  msha="$(git --git-dir "$GH_ORIGIN" commit-tree "$tree" -p "$mainsha" -p "$ibhead" -m external)"
  git --git-dir "$GH_ORIGIN" update-ref refs/heads/main "$msha"
  git --git-dir "$GH_ORIGIN" update-ref -d refs/heads/spec/item-b
  git -C "${WORK_REPO:?}" branch -D spec/item-b >/dev/null
  git --git-dir "$GH_ORIGIN" update-ref -d refs/heads/spec/item-c
  git -C "$WORK_REPO" branch -D spec/item-c >/dev/null
fi
exit 0
STUB
  chmod +x "$MOCK_BIN"/*
  RUN_ID=e5a
  python3 "$ROOT/lib/gates.py" grant "$RUN_ID" --action merge:pr-101 --reason t >/dev/null
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id "$RUN_ID" spec/item-a spec/item-b spec/item-c
  [ "$status" -eq 0 ]
  # External merge is reconciled LANDED at the item-start boundary with the
  # recorded head as its proof; no lifecycle effect ran for item-b.
  grep -q "ITEM spec/item-b LANDED $OID_B" <<<"$output"
  ! grep -q "implement spec/item-b" "$EVENTS"
  # Missing proof (gone branch, unreachable head) blocks source-missing.
  grep -q "ITEM spec/item-c BLOCKED:source-missing" <<<"$output"
  ! grep -q "implement spec/item-c" "$EVENTS"
  grep -q "ITEM spec/item-a LANDED" <<<"$output"
}
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
@test "[EDGE-010] quarantine requeues once after base advance" {
  test -f "$LINKED/.git"
  stub_env
  # item-a permanently conflicts with main; item-b is clean and will land,
  # advancing the base between the first quarantine and the requeue check.
  mk_branch spec/item-a README.md alpha-side
  git checkout -q main; echo mainline > README.md
  git add -- README.md; git commit -qm mainline; git push -q origin main
  mk_branch spec/item-b b.txt beta
  write_gh
  write_children
  RUN_ID=e10a
  python3 "$ROOT/lib/gates.py" grant "$RUN_ID" --action merge:pr-102 --reason t >/dev/null
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id "$RUN_ID" spec/item-a spec/item-b
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-b LANDED" <<<"$output"
  grep -q "ITEM spec/item-a BLOCKED:conflict" <<<"$output"
  # The report shows ONE row for the quarantined item, not one per attempt.
  [ "$(grep -c "^ITEM spec/item-a " <<<"$output")" -eq 1 ]
  # Durable journal counter: exactly TWO precheck attempts (initial + the one
  # base-advance requeue), then the second quarantine parks permanently.
  python3 - "$LQ/$RUN_ID.json" <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1]))
ev = doc["events"]
pre = [e for e in ev if e.get("item") == "spec/item-a"
       and e["kind"] == "intent" and e["step"] == "precheck"]
assert len(pre) == 2, pre
terms = [e for e in ev if e["kind"] == "terminal" and e.get("item") == "spec/item-a"]
assert len(terms) == 2 and all(t["status"] == "BLOCKED:conflict" for t in terms), terms
PYEOF

  # Without base advancement there is NO requeue at all.
  RUN_ID=e10b
  : > "$EVENTS"
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id "$RUN_ID" spec/item-a
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-a BLOCKED:conflict" <<<"$output"
  python3 - "$LQ/$RUN_ID.json" <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1]))
pre = [e for e in doc["events"] if e.get("item") == "spec/item-a"
       and e["kind"] == "intent" and e["step"] == "precheck"]
assert len(pre) == 1, pre
PYEOF
}
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
@test "[GRANT-HOOK] normal completion derives a queue-bound consolidate grant" {
  # WR-02 companion pin: the same idempotent derivation runs at the normal
  # all-items-terminal boundary, minting under the queue's own run id.
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  RUN_ID=run-960
  python3 "$ROOT/lib/gates.py" grant "$RUN_ID" --action merge:pr-101 --reason t >/dev/null
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id "$RUN_ID" spec/item-a
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-a LANDED" <<<"$output"
  python3 - "$GATES_STORE" <<'PYEOF'
import json, sys
data = json.load(open(sys.argv[1]))
grants = data.get("_autonomy", {}).get("run-960", {}).get("grants", {})
scopes = [a for a in grants if a.startswith("consolidate:estate:")]
assert scopes, f"no consolidate grant at normal completion; grants={list(grants)}"
assert grants[scopes[0]].get("granted_by") == "queue", grants[scopes[0]]
PYEOF
}

@test "[RESUME] a completed queue resumed with no retries still derives the consolidate grant" {
  # WR-02: crash after the final LANDED append but before grant derivation
  # must be recoverable — the no-retry resume path re-runs the idempotent
  # post-terminal derivation, bound to the ORIGINAL journal run id.
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  # journal for a fully-terminal queue, built through the real accessor,
  # with NO consolidate grant ever minted (the simulated crash window)
  mkdir -p "$LQ"; chmod 700 "$LQ"
  JRNL="$ROOT/skills/land-queue/scripts/queue-journal.py"
  HEAD_A="$(git --git-dir "$ORIGIN" rev-parse refs/heads/spec/item-a)"
  MERGE_A="$(python3 -c "print('f'*40)")"
  python3 "$JRNL" init --store "$LQ" --queue-id resume-q1 --run-id run-961 \
    --repo "$WORK" --base main
  python3 "$JRNL" append --store "$LQ" --queue-id resume-q1 \
    --kind intent --step merge --item spec/item-a --pr 101 --head "$HEAD_A"
  python3 "$JRNL" append --store "$LQ" --queue-id resume-q1 \
    --kind result --step merge --item spec/item-a --status ok
  python3 "$JRNL" append --store "$LQ" --queue-id resume-q1 \
    --kind terminal --step terminal --item spec/item-a --status LANDED \
    --detail "$MERGE_A"
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id run-962 --resume resume-q1
  [ "$status" -eq 0 ]
  grep -q "resumed" <<<"$output"
  python3 - "$GATES_STORE" <<'PYEOF'
import json, sys
data = json.load(open(sys.argv[1]))
grants = data.get("_autonomy", {}).get("run-961", {}).get("grants", {})
scopes = [a for a in grants if a.startswith("consolidate:estate:")]
assert scopes, ("no consolidate grant derived on the no-retry resume path; "
                f"grants={list(grants)}")
entry = grants[scopes[0]]
assert entry.get("granted_by") == "queue", entry
assert entry.get("expires_at", 0) - entry.get("granted_at", 0) <= 8 * 3600 + 1, entry
resumer = data.get("_autonomy", {}).get("run-962", {}).get("grants", {})
assert not [a for a in resumer if a.startswith("consolidate:")], \
    "the grant leaked onto the resumer's run id instead of the original"
PYEOF
}

@test "[DEADLINE] a late resume with an oversized caller timeout never re-mints authority" {
  # CR-02 (round 2): the queue deadline is journal-immutable.  A resume
  # after the original absolute deadline — even with an oversized
  # QUEUE_WALL_SECONDS in the caller environment — must never derive a
  # fresh consolidate grant; the landed report stands with the typed
  # skipped-grant advisory.
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  mkdir -p "$LQ"; chmod 700 "$LQ"
  JRNL="$ROOT/skills/land-queue/scripts/queue-journal.py"
  HEAD_A="$(git --git-dir "$ORIGIN" rev-parse refs/heads/spec/item-a)"
  MERGE_A="$(python3 -c "print('f'*40)")"
  python3 "$JRNL" init --store "$LQ" --queue-id resume-q2 --run-id run-963 \
    --repo "$WORK" --base main
  python3 "$JRNL" append --store "$LQ" --queue-id resume-q2 \
    --kind intent --step merge --item spec/item-a --pr 101 --head "$HEAD_A"
  python3 "$JRNL" append --store "$LQ" --queue-id resume-q2 \
    --kind result --step merge --item spec/item-a --status ok
  python3 "$JRNL" append --store "$LQ" --queue-id resume-q2 \
    --kind terminal --step terminal --item spec/item-a --status LANDED \
    --detail "$MERGE_A"
  # age the journal past its own absolute deadline
  python3 - "$LQ/resume-q2.json" <<'PYAGE'
import json, sys, time
path = sys.argv[1]
doc = json.load(open(path))
doc["created_at"] = int(time.time()) - (28800 + 7200)
if "deadline" in doc:
    doc["deadline"] = doc["created_at"] + 28800
open(path, "w").write(json.dumps(doc, sort_keys=True))
PYAGE
  QUEUE_WALL_SECONDS=999999 PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" \
    --repo "$WORK" --base main --run-id run-964 --resume resume-q2
  [ "$status" -eq 0 ]
  grep -q "CONSOLIDATE-GRANT-SKIPPED" <<<"$output"
  python3 - "$GATES_STORE" <<'PYEOF2'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except FileNotFoundError:
    data = {}
for run in ("run-963", "run-964"):
    grants = data.get("_autonomy", {}).get(run, {}).get("grants", {})
    leaked = [a for a in grants if a.startswith("consolidate:estate:")]
    assert not leaked, f"expired-deadline resume minted authority for {run}: {leaked}"
PYEOF2
}

@test "[REVIEW] floor on a codex host refuses when only codex is installed" {
  # CR-05 / 37bc43d9: floor's defining guarantee is an OPPOSITE-vendor
  # reviewer — the producing host's own CLI never satisfies it.
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  RESTRICTED="$(same_vendor_only_path codex)"
  RUN_ID=run-951
  FFS_HOST=codex PATH="$RESTRICTED" run bash "$QUEUE" --repo "$WORK" --base main \
    --posture floor --run-id "$RUN_ID" spec/item-a
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-a BLOCKED:no-cross-vendor-reviewer" <<<"$output"
  posture_no_hit -q "gh pr merge" "$EVENTS"
  posture_no_hit -q "^codex review" "$EVENTS"
  python3 - "$GATES_STORE" <<'PYEOF'
import json, sys
data = json.load(open(sys.argv[1]))
ev = [e for e in data["_degradation"]["invocations"]
      if e.get("run_id") == "run-951"]
assert len(ev) == 1 and ev[0]["degraded"] is True, ev
PYEOF
}

@test "[REVIEW] floor on a claude host refuses when only claude is installed" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  RESTRICTED="$(same_vendor_only_path claude)"
  RUN_ID=run-952
  FFS_HOST=claude PATH="$RESTRICTED" run bash "$QUEUE" --repo "$WORK" --base main \
    --posture floor --run-id "$RUN_ID" spec/item-a
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-a BLOCKED:no-cross-vendor-reviewer" <<<"$output"
  posture_no_hit -q "gh pr merge" "$EVENTS"
  posture_no_hit -q "^claude review" "$EVENTS"
}

@test "[REVIEW] zero posture same-vendor fallback reviews but records degraded=true" {
  # CR-05: under zero the same-vendor CLI may still review, but the
  # invocation is durably recorded as DEGRADED — never as a clean review.
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  RESTRICTED="$(same_vendor_only_path codex)"
  RUN_ID=run-953
  python3 "$ROOT/lib/gates.py" grant "$RUN_ID" --action merge:pr-101 --reason t >/dev/null
  FFS_HOST=codex PATH="$RESTRICTED" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id "$RUN_ID" spec/item-a
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-a LANDED" <<<"$output"
  grep -q "^codex review" "$EVENTS"
  python3 - "$GATES_STORE" <<'PYEOF'
import json, sys
data = json.load(open(sys.argv[1]))
ev = [e for e in data["_degradation"]["invocations"]
      if e.get("run_id") == "run-953"]
assert len(ev) == 1 and ev[0]["degraded"] is True, ev
PYEOF
}

@test "[REVIEW] floor and zero reviewer modes bind degradation" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  RESTRICTED="$(no_reviewer_path)"

  # Floor + no opposite-vendor reviewer: typed block, degradation recorded,
  # no same-host substitute, no merge (37bc43d9).
  RUN_ID=run-89
  PATH="$RESTRICTED" run bash "$QUEUE" --repo "$WORK" --base main \
    --posture floor --run-id "$RUN_ID" spec/item-a
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-a BLOCKED:no-cross-vendor-reviewer" <<<"$output"
  ! grep -q "gh pr merge" "$EVENTS"
  ! grep -q "codex" "$EVENTS"
  python3 - "$GATES_STORE" <<'PYEOF'
import json, sys
data = json.load(open(sys.argv[1]))
ev = [e for e in data["_degradation"]["invocations"] if e.get("run_id") == "run-89"]
assert len(ev) == 1 and ev[0]["degraded"] is True, ev
PYEOF

  # Floor + successful bounded review of the pinned head: merge proceeds and
  # a findings artifact is recorded — executable presence alone never counts.
  RUN_ID=run-90
  : > "$EVENTS"
  write_children  # restore the reviewer stub removed by no_reviewer_path
  python3 "$ROOT/lib/gates.py" grant "$RUN_ID" --action merge:pr-101 --reason t >/dev/null
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --posture floor --run-id "$RUN_ID" spec/item-a
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-a LANDED" <<<"$output"
  OID_A="$(git --git-dir "$ORIGIN" rev-parse refs/heads/spec/item-a)" || true
  ls "$LQ"/reviews/*101* >/dev/null

  # Zero + successful review records a bound degraded=false invocation.
  RUN_ID=run-88
  : > "$EVENTS"
  git checkout -qb spec/item-b main
  echo beta > b.txt; git add -- b.txt; git commit -qm item-b; git push -q origin spec/item-b
  git checkout -q main
  python3 "$ROOT/lib/gates.py" grant "$RUN_ID" --action merge:pr-102 --reason t >/dev/null
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id "$RUN_ID" spec/item-b
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-b LANDED" <<<"$output"
  python3 - "$GATES_STORE" <<'PYEOF'
import json, sys
data = json.load(open(sys.argv[1]))
ev = [e for e in data["_degradation"]["invocations"] if e.get("run_id") == "run-88"]
assert len(ev) == 1, ev
e = ev[0]
assert e["degraded"] is False and e.get("branch") == "spec/item-b", e
assert len(e.get("head", "")) == 40, e
PYEOF
}
@test "[CI] timeout shim requires literal 1200 and gh watch interval 10" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children

  # 1. The bound is the literal 1200 and rc 124 maps only to ci-timeout.
  # The shim asserts 1200 and returns 124 IMMEDIATELY — no sleeping.
  cat > "$MOCK_BIN/timeout" <<'STUB'
#!/usr/bin/env bash
secs="$3"; shift 3
if [ "$1" = "gh" ] && [ "$2" = "pr" ] && [ "$3" = "checks" ]; then
  [ "$secs" = "1200" ] || { echo "CI-BOUND-NOT-1200:$secs" >&2; exit 64; }
  printf 'timeout %s gh pr checks\n' "$secs" >> "${EVENTS:?}"
  exit 124
fi
exec "$@"
STUB
  chmod +x "$MOCK_BIN/timeout"
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id ci1 spec/item-a
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-a BLOCKED:ci-timeout" <<<"$output"
  grep -qx "timeout 1200 gh pr checks" "$EVENTS"
  rm -f "$MOCK_BIN/timeout"

  # 2. Empty check rollup after a green watch is a named block.
  export GH_ROLLUP="0 MERGEABLE"
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id ci2 spec/item-a
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-a BLOCKED:ci-empty" <<<"$output"

  # 3. Merge conflict mergeability is a named block.
  export GH_ROLLUP="1 CONFLICTING"
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id ci3 spec/item-a
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-a BLOCKED:merge-conflict" <<<"$output"

  # 4. gh auth failure is a named block.
  export GH_ROLLUP="AUTHFAIL"
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id ci4 spec/item-a
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-a BLOCKED:gh-auth" <<<"$output"
  unset GH_ROLLUP

  # None of the four ever reached merge.
  ! grep -q "gh pr merge" "$EVENTS"
}
@test "[HEAD] merge pins review-time head and refuses drift" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  export GH_HEAD_DRIFT=1
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id hd1 spec/item-a
  unset GH_HEAD_DRIFT
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-a BLOCKED:head-moved" <<<"$output"
  # Never re-pinned to the moved head: no merge call of any shape.
  ! grep -q "gh pr merge" "$EVENTS"
}
@test "[MERGE] no merge runs after failed preconditions" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  # Grant deliberately withheld: the last precondition fails.
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id mg1 spec/item-a
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-a BLOCKED:grant-missing" <<<"$output"
  ! grep -q "gh pr merge" "$EVENTS"
  # Review and CI did run — the refusal is the grant boundary, not earlier.
  grep -q "gh pr checks" "$EVENTS"
}
@test "[GRANT] promotion authority runs after review and CI" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  RUN_ID=gr1
  python3 "$ROOT/lib/gates.py" grant "$RUN_ID" --action merge:pr-101 --reason t >/dev/null
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id "$RUN_ID" spec/item-a
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-a LANDED" <<<"$output"
  python3 - "$LQ/$RUN_ID.json" <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1]))
names = [f'{e["kind"]}:{e["step"]}' for e in doc["events"] if e.get("item") == "spec/item-a"]
assert names.index("result:review") < names.index("result:ci") \
    < names.index("intent:grant") < names.index("result:grant") \
    < names.index("intent:merge"), names
PYEOF
}
@test "[FINALIZER] finalizer result precedes LANDED terminal" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  # (a) success ordering: merge result -> finalizer intent -> finalizer
  # result -> LANDED terminal, never LANDED before finalization (865d06d4).
  RUN_ID=fz0
  python3 "$ROOT/lib/gates.py" grant "$RUN_ID" --action merge:pr-101 --reason t >/dev/null
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id "$RUN_ID" spec/item-a
  [ "$status" -eq 0 ]
  python3 - "$LQ/$RUN_ID.json" <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1]))
ev = [e for e in doc["events"] if e.get("item") == "spec/item-a"]
names = [f'{e["kind"]}:{e["step"]}' for e in ev]
assert names.index("result:merge") < names.index("intent:finalize") \
    < names.index("result:finalize") < names.index("terminal:terminal"), names
PYEOF

  # (b) crash between finalizer intent and terminal: recovery re-runs the
  # finalizer idempotently, then appends LANDED — no second merge call.
  : > "$EVENTS"
  RUN_ID=fz1
  mkdir -p "$LQ"
  JR="$ROOT/skills/land-queue/scripts/queue-journal.py"
  OID_A="$(git --git-dir "$ORIGIN" rev-parse refs/heads/spec/item-a)"
  python3 "$JR" init --store "$LQ" --queue-id "$RUN_ID" --run-id "$RUN_ID"
  python3 "$JR" append --store "$LQ" --queue-id "$RUN_ID" \
    --kind intent --step merge --item spec/item-a --pr 301 --head "$OID_A"
  python3 "$JR" append --store "$LQ" --queue-id "$RUN_ID" \
    --kind result --step merge --item spec/item-a --status ok
  python3 "$JR" append --store "$LQ" --queue-id "$RUN_ID" \
    --kind intent --step finalize --item spec/item-a --pr 301 --head "$OID_A"
  MERGE_SHA="$(git --git-dir "$ORIGIN" rev-parse refs/heads/main)"
  printf '%s\n' "$MERGE_SHA" > "$GH_STATE/merge-301"
  cat > "$MOCK_BIN/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' gh "$@" >> "${CALL_LOG:?}"
printf 'gh %s\n' "$*" >> "${EVENTS:?}"
case "${1:-} ${2:-}" in
  "pr view")
    target="$3"; shift 3
    [ "$*" = "--json mergeCommit -q .mergeCommit.oid" ] || { echo "UNSTUBBED-BOUNDARY:$*" >&2; exit 64; }
    cat "${GH_STATE:?}/merge-$target" ;;
  "pr merge") echo "SECOND-MERGE-FORBIDDEN" >&2; exit 97 ;;
  *) echo "UNSTUBBED-BOUNDARY:$*" >&2; exit 64 ;;
esac
STUB
  chmod +x "$MOCK_BIN/gh"
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id "$RUN_ID" --resume "$RUN_ID"
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-a LANDED $MERGE_SHA" <<<"$output"
  grep -q "finalize --run-id $RUN_ID 301" "$EVENTS"
  ! grep -q "gh pr merge" "$EVENTS"
  python3 - "$LQ/$RUN_ID.json" <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1]))
names = [f'{e["kind"]}:{e["step"]}:{e.get("status","")}' for e in doc["events"]
         if e.get("item") == "spec/item-a"]
assert any(n.startswith("result:finalize:") for n in names), names
assert names[-1].startswith("terminal:terminal:LANDED"), names
PYEOF
}
@test "[POSTURE] production touch is auditable through review policy" {
  # 7c59d7a0: end-to-end through the REAL landing controller — it records a
  # bound degradation event and the production authority refuses promotion.
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  RESTRICTED="$(no_reviewer_path)"
  RUN_ID=run-77
  python3 "$ROOT/lib/gates.py" grant "$RUN_ID" --action merge:pr-101 --reason t >/dev/null
  OID_A="$(git --git-dir "$ORIGIN" rev-parse refs/heads/spec/item-a)"
  PATH="$RESTRICTED" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id "$RUN_ID" spec/item-a
  [ "$status" -eq 0 ]
  # Zero posture: reviewer missing -> degradation recorded, merge permitted.
  grep -q "ITEM spec/item-a LANDED" <<<"$output"
  python3 - "$GATES_STORE" "$OID_A" <<'PYEOF'
import json, sys
data = json.load(open(sys.argv[1]))
ev = [e for e in data["_degradation"]["invocations"] if e.get("run_id") == "run-77"]
assert len(ev) == 1, ev
e = ev[0]
assert e["degraded"] is True, e
assert e.get("branch") == "spec/item-a", e
assert e.get("head") == sys.argv[2], e
assert "a.txt" in e.get("changed_files", []), e
assert e.get("production_files") == ["a.txt"], e
assert e.get("production_touch") is True, e
assert len(e.get("baseline", "")) == 40, e
PYEOF
  # The real production authority refuses at the degradation gate: a
  # degraded production-touching review blocks regardless of ratio.
  GATES_PATH="$ROOT/lib/gates.py" python3 - <<'PYEOF'
import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("g", os.environ["GATES_PATH"])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
store = Path(os.environ["GATES_STORE"])
sink = []
ok = m.check_grant_prod(store, "run-77", "deploy:prod-web", None, reason_sink=sink)
assert ok is False and sink == [], (ok, sink)
PYEOF
}
@test "[STOP] STOP marker aborts before a new item or merge" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children

  # (a) STOP present before the first item: nothing is dispatched.
  mkdir -p "$LQ"; touch "$LQ/STOP"
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id st1 spec/item-a
  [ "$status" -eq 1 ]
  grep -q "QUEUE-ABORTED:operator-stop" <<<"$output"
  ! grep -q "^implement " "$EVENTS"
  ! grep -q "gh pr merge" "$EVENTS"
  rm -f "$LQ/STOP"

  # (b) STOP appearing during CI: the in-flight effect completes but the
  # merge — the next effect — never starts.
  : > "$EVENTS"
  export STOP_AT_CI=1
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id st2 spec/item-a
  unset STOP_AT_CI
  [ "$status" -eq 1 ]
  grep -q "QUEUE-ABORTED:operator-stop" <<<"$output"
  grep -q "gh pr checks" "$EVENTS"
  ! grep -q "gh pr merge" "$EVENTS"
  rm -f "$LQ/STOP"
}
@test "[DRAIN] second-process DRAIN stops at next item boundary" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  mk_branch spec/item-b b.txt beta
  write_gh
  write_children
  export QUEUE_BIN="$QUEUE"
  # item-a's implement child plays the second process: it requests a drain
  # WITHOUT owning the queue lock (the CLI only drops the marker).
  cat > "$MOCK_BIN/feature-implement" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' feature-implement "$@" >> "${CALL_LOG:?}"
printf 'implement %s\n' "$*" >> "${EVENTS:?}"
if [ "$1" = "spec/item-a" ]; then
  bash "${QUEUE_BIN:?}" --drain >/dev/null
fi
exit 0
STUB
  chmod +x "$MOCK_BIN"/*
  RUN_ID=dr1
  python3 "$ROOT/lib/gates.py" grant "$RUN_ID" --action merge:pr-101 --reason t >/dev/null
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id "$RUN_ID" spec/item-a spec/item-b
  [ "$status" -eq 0 ]
  # The in-flight item completes; the next never starts; the marker is
  # consumed under the lock (71c46cda).
  grep -q "ITEM spec/item-a LANDED" <<<"$output"
  grep -q "QUEUE-DRAINED:operator-drain" <<<"$output"
  ! grep -q "implement spec/item-b" "$EVENTS"
  [ ! -e "$LQ/DRAIN" ]
}
@test "[HUMAN-INBOX] blocked item has reason and one-command unblock" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  cat > "$MOCK_BIN/feature-implement" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' feature-implement "$@" >> "${CALL_LOG:?}"
printf 'implement %s\n' "$*" >> "${EVENTS:?}"
echo "compile explosion in module core" >&2
exit 1
STUB
  chmod +x "$MOCK_BIN"/*
  RUN_ID=hi1
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id "$RUN_ID" spec/item-a
  [ "$status" -eq 0 ]
  grep -Eq "^HUMAN-INBOX: spec/item-a BLOCKED:implement reason: .+ unblock: .+$" <<<"$output"
  # The terminal RECORD carries separate nonempty reason and unblock fields.
  python3 - "$LQ/$RUN_ID.json" <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1]))
terms = [e for e in doc["events"] if e["kind"] == "terminal"
         and e.get("item") == "spec/item-a"]
assert len(terms) == 1, terms
t = terms[0]
assert t.get("reason") and t.get("unblock") and t["reason"] != t["unblock"], t
PYEOF
}
@test "[REVERT] landed record contains merge SHA and revert command" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  RUN_ID=rv1
  python3 "$ROOT/lib/gates.py" grant "$RUN_ID" --action merge:pr-101 --reason t >/dev/null
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id "$RUN_ID" spec/item-a
  [ "$status" -eq 0 ]
  MERGE_A="$(cat "$GH_STATE/merge-101")"
  grep -qx "ITEM spec/item-a LANDED $MERGE_A" <<<"$output"
  grep -qx "REVERT: spec/item-a git revert $MERGE_A" <<<"$output"
}
@test "[PARALLEL] any parallel value is refused exactly" {
  test -f "$LINKED/.git"
  stub_env
  for b in gh codex claude feature-implement assert-merged.sh run-finalizer.sh; do
    printf '#!/usr/bin/env bash\nprintf "%%s\\0" "$0" "$@" >> "${CALL_LOG:?}"\nexit 64\n' > "$MOCK_BIN/$b"
    chmod +x "$MOCK_BIN/$b"
  done
  for form in "--parallel 1" "--parallel=8" "--parallel true" "--parallel=0"; do
    # shellcheck disable=SC2086
    PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main $form spec/queue
    [ "$status" -eq 2 ]
    [ "$output" = "PARALLEL-UNSUPPORTED:v1-serial-only" ]
    [ ! -s "$CALL_LOG" ]
  done
}
@test "[INTAKE] new queue unions takeover records and estate dispositions" {
  test -f "$LINKED/.git"
  stub_env
  # Takeover source: a canonical record under <store-dir>/takeover/ names a
  # branch that exists ONLY on origin, so no other source can supply it.
  git checkout -qb spec/item-a main
  echo alpha > a.txt; git add -- a.txt; git commit -qm item-a; git push -q origin spec/item-a
  git checkout -q main
  OID_A="$(git --git-dir "$ORIGIN" rev-parse refs/heads/spec/item-a)"
  git branch -qD spec/item-a
  TSTORE="$(python3 "$ROOT/lib/gates.py" store-dir)"
  mkdir -p "$TSTORE/takeover"
  printf '{"branch":"spec/item-a","head":"%s","run_id":"spec-006","spec_id":"006"}\n' \
    "$OID_A" > "$TSTORE/takeover/spec-006.json"
  # Estate source: the REAL collect-estate discovery over the fixture repo
  # (local git only) finds spec/item-b and spec/queue as review-then-land.
  mk_branch spec/item-b b.txt beta
  unset LAND_QUEUE_ESTATE_JSON
  write_gh
  write_children
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main --run-id spec-006
  [ "$status" -eq 0 ]
  # No explicit inputs were given: every item below arrived through the
  # takeover glob (item-a) or the estate source (item-b, spec/queue).
  grep -q "^ITEM spec/item-a " <<<"$output"
  grep -q "^ITEM spec/item-b " <<<"$output"
  grep -q "^ITEM spec/queue " <<<"$output"
  ! grep -q "QUEUE-ABORTED" <<<"$output"
}
@test "[INTAKE] truncated intake fails with the named max-items outcome" {
  test -f "$LINKED/.git"
  stub_env
  branches=()
  for i in 01 02 03 04 05 06 07 08 09 10 11; do
    mk_branch "spec/it-$i" "f$i.txt" "x$i"
    branches+=("spec/it-$i")
  done
  write_gh
  write_children
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" --repo "$WORK" --base main \
    --run-id trunc1 "${branches[@]}"
  # An 11-item intake must trip the named max-items outcome, never proceed
  # silently with a sliced 10-item list.
  [ "$status" -eq 1 ]
  grep -q "QUEUE-ABORTED:systemic:max-items" <<<"$output"
  [ "$(grep -c "^ITEM .* SKIPPED:queue-aborted" <<<"$output")" -eq 10 ]
  # No lifecycle effect ever started.
  ! grep -q "^implement " "$EVENTS"
  ! grep -q "gh pr merge" "$EVENTS"
}

# ── Phase 3 Wave-0 RED contract: posture resolution (03-04) ───────────────
# The landed Phase 2 seam is the --posture flag only ("Phase 3 owns the
# committed configuration").  These selectors pin the Phase 3 resolution:
# default zero < config (--posture) < env (FFS_AUTONOMY_POSTURE), stricter-
# only (floor may override zero, zero may never override floor), invalid
# input advises and falls through, provenance is printed exactly once as
# `POSTURE-RESOLVED: <posture> source=<default|config|env>`, and every
# Task 1 posture consumer honors the RESOLVED posture.  Until that lands,
# each missing assertion reports the typed behavioral marker below.

posture_no_hit() { # errexit-safe negation: fail when the pattern IS present
  if grep "$@"; then return 1; fi
  return 0
}

posture_red() { # $1 why — typed marker for the missing Phase 3 seam
  echo "EXPECTED-RED:POSTURE:missing-posture-resolution"
  echo "RED: $1"
  return 1
}

@test "[POSTURE] env floor strengthens default zero on a missing-reviewer item" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  RESTRICTED="$(no_reviewer_path)"
  RUN_ID=run-301
  FFS_AUTONOMY_POSTURE=floor PATH="$RESTRICTED" run bash "$QUEUE" \
    --repo "$WORK" --base main --run-id "$RUN_ID" spec/item-a
  [ "$status" -eq 0 ]
  posture_no_hit -q "gh pr merge" "$EVENTS"
  grep -q "ITEM spec/item-a BLOCKED:no-cross-vendor-reviewer" <<<"$output" \
    || posture_red "FFS_AUTONOMY_POSTURE=floor did not strengthen the run: the missing-reviewer item was not floor-blocked"
}

@test "[POSTURE] env zero cannot weaken explicit floor and provenance names floor" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  RESTRICTED="$(no_reviewer_path)"
  RUN_ID=run-302
  FFS_AUTONOMY_POSTURE=zero PATH="$RESTRICTED" run bash "$QUEUE" \
    --repo "$WORK" --base main --posture floor --run-id "$RUN_ID" spec/item-a
  [ "$status" -eq 0 ]
  # stricter-only: the committed floor holds even when env asks for zero
  grep -q "ITEM spec/item-a BLOCKED:no-cross-vendor-reviewer" <<<"$output"
  posture_no_hit -q "gh pr merge" "$EVENTS"
  grep -q "POSTURE-RESOLVED: floor" <<<"$output" \
    || posture_red "no POSTURE-RESOLVED provenance line names the surviving floor posture"
}

@test "[POSTURE] invalid env value advises and falls through to zero" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  RESTRICTED="$(no_reviewer_path)"
  RUN_ID=run-303
  FFS_AUTONOMY_POSTURE=bananas PATH="$RESTRICTED" run bash "$QUEUE" \
    --repo "$WORK" --base main --run-id "$RUN_ID" spec/item-a
  [ "$status" -eq 0 ]
  # fell through to zero: the degraded path proceeds to the grant boundary
  grep -q "ITEM spec/item-a BLOCKED:grant-missing" <<<"$output"
  grep -q "POSTURE-INVALID: bananas" <<<"$output" \
    || posture_red "an invalid FFS_AUTONOMY_POSTURE value produced no advisory before falling through"
}

@test "[POSTURE] resolution provenance is printed exactly once for default zero" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  RESTRICTED="$(no_reviewer_path)"
  RUN_ID=run-304
  PATH="$RESTRICTED" run bash "$QUEUE" \
    --repo "$WORK" --base main --run-id "$RUN_ID" spec/item-a
  [ "$status" -eq 0 ]
  [ "$(grep -c "^POSTURE-RESOLVED:" <<<"$output")" -eq 1 ] \
    || posture_red "expected exactly one POSTURE-RESOLVED provenance line, found $(grep -c "^POSTURE-RESOLVED:" <<<"$output")"
  grep -q "POSTURE-RESOLVED: zero source=default" <<<"$output" \
    || posture_red "default resolution does not record zero with source=default"
}

@test "[POSTURE] same missing-reviewer fixture diverges under env zero and env floor" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  RESTRICTED="$(no_reviewer_path)"

  # floor first: nothing may land, so the fixture stays identical for zero
  RUN_ID=run-305
  FFS_AUTONOMY_POSTURE=floor PATH="$RESTRICTED" run bash "$QUEUE" \
    --repo "$WORK" --base main --run-id "$RUN_ID" spec/item-a
  [ "$status" -eq 0 ]
  posture_no_hit -q "gh pr merge" "$EVENTS"
  grep -q "ITEM spec/item-a BLOCKED:no-cross-vendor-reviewer" <<<"$output" \
    || posture_red "env floor did not block the missing-reviewer item that env zero is allowed to land"

  # zero second: the SAME fixture lands with a durably recorded degradation
  RUN_ID=run-306
  : > "$EVENTS"
  python3 "$ROOT/lib/gates.py" grant "$RUN_ID" --action merge:pr-101 --reason t >/dev/null
  FFS_AUTONOMY_POSTURE=zero PATH="$RESTRICTED" run bash "$QUEUE" \
    --repo "$WORK" --base main --run-id "$RUN_ID" spec/item-a
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-a LANDED" <<<"$output"
  python3 - "$GATES_STORE" <<'PYEOF'
import json, sys
data = json.load(open(sys.argv[1]))
ev = [e for e in data["_degradation"]["invocations"] if e.get("run_id") == "run-306"]
assert len(ev) == 1 and ev[0]["degraded"] is True, ev
PYEOF
}

@test "[POSTURE] resolved env floor disables the zero-only quarantine requeue" {
  test -f "$LINKED/.git"
  stub_env
  # item-a permanently conflicts with main; item-b lands and advances the
  # base — under resolved floor the conflict item must still requeue NEVER.
  mk_branch spec/item-a README.md alpha-side
  git checkout -q main; echo mainline > README.md
  git add -- README.md; git commit -qm mainline; git push -q origin main
  mk_branch spec/item-b b.txt beta
  write_gh
  write_children
  RUN_ID=run-307
  python3 "$ROOT/lib/gates.py" grant "$RUN_ID" --action merge:pr-102 --reason t >/dev/null
  FFS_AUTONOMY_POSTURE=floor PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" \
    --repo "$WORK" --base main --run-id "$RUN_ID" spec/item-a spec/item-b
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-b LANDED" <<<"$output"
  grep -q "ITEM spec/item-a BLOCKED:conflict" <<<"$output"
  rc=0
  python3 - "$LQ/$RUN_ID.json" >/dev/null 2>&1 <<'PYEOF' || rc=$?
import json, sys
doc = json.load(open(sys.argv[1]))
pre = [e for e in doc["events"] if e.get("item") == "spec/item-a"
       and e["kind"] == "intent" and e["step"] == "precheck"]
assert len(pre) == 1, pre
PYEOF
  [ "$rc" -eq 0 ] \
    || posture_red "the conflict item was requeued under env floor: quarantine auto-requeue is a zero-posture-only consumer"
}

# ── Phase 3 Task 2 (03-02): invalid-input, once-only, and no-op-knob guards ──
# The resolver accepts exact lowercase zero/floor after trimming ONLY ordinary
# surrounding whitespace; every other env/config value falls through to the
# committed policy with one bounded, sanitized advisory and zero shell side
# effects.  A structural inventory pins the single-seam property: only
# autonomy-posture.sh reads FFS_AUTONOMY_POSTURE, and every enumerated floor
# consumer uses the resolved AUTONOMY_POSTURE.

@test "[POSTURE] surrounding whitespace trims to a valid floor but mixed case never counts" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  RESTRICTED="$(no_reviewer_path)"

  # ' floor ' trims to the exact literal: the run strengthens to floor
  FFS_AUTONOMY_POSTURE=' floor ' PATH="$RESTRICTED" run bash "$QUEUE" \
    --repo "$WORK" --base main --run-id run-311 spec/item-a
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-a BLOCKED:no-cross-vendor-reviewer" <<<"$output"
  grep -q "POSTURE-RESOLVED: floor source=env" <<<"$output"

  # 'FLOOR' is not the exact lowercase literal: advisory, fall through to zero
  FFS_AUTONOMY_POSTURE='FLOOR' PATH="$RESTRICTED" run bash "$QUEUE" \
    --repo "$WORK" --base main --run-id run-312 spec/item-a
  [ "$status" -eq 0 ]
  grep -q "POSTURE-INVALID: FLOOR" <<<"$output"
  grep -q "POSTURE-RESOLVED: zero source=default" <<<"$output"
  grep -q "ITEM spec/item-a BLOCKED:grant-missing" <<<"$output"
  posture_no_hit -q "gh pr merge" "$EVENTS"
}

@test "[POSTURE] newline and shell-metacharacter env values advise sanitized and never execute" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  RESTRICTED="$(no_reviewer_path)"

  # interior newline survives surrounding-whitespace trimming: invalid
  FFS_AUTONOMY_POSTURE=$'flo\nor' PATH="$RESTRICTED" run bash "$QUEUE" \
    --repo "$WORK" --base main --run-id run-313 spec/item-a
  [ "$status" -eq 0 ]
  [ "$(grep -c "^POSTURE-INVALID:" <<<"$output")" -eq 1 ]
  grep -q "POSTURE-RESOLVED: zero source=default" <<<"$output"

  # command substitution text is data, never evaluated, never echoed raw
  FFS_AUTONOMY_POSTURE='$(touch "'"$WORK"'/pwned")' PATH="$RESTRICTED" run bash "$QUEUE" \
    --repo "$WORK" --base main --run-id run-314 spec/item-a
  [ "$status" -eq 0 ]
  [ ! -e "$WORK/pwned" ]
  [ "$(grep -c "^POSTURE-INVALID:" <<<"$output")" -eq 1 ]
  posture_no_hit -qF '$(' <<<"$output"
  grep -q "ITEM spec/item-a BLOCKED:grant-missing" <<<"$output"
}

@test "[POSTURE] invalid config type advises once and falls through to the zero default" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  write_gh
  write_children
  RESTRICTED="$(no_reviewer_path)"
  mkdir -p "$WORK/.planning"
  printf '{"autonomy": {"posture": 5}}\n' > "$WORK/.planning/config.json"
  PATH="$RESTRICTED" run bash "$QUEUE" \
    --repo "$WORK" --base main --run-id run-315 spec/item-a
  [ "$status" -eq 0 ]
  [ "$(grep -c "^POSTURE-INVALID: config" <<<"$output")" -eq 1 ]
  grep -q "POSTURE-RESOLVED: zero source=default" <<<"$output"
  grep -q "ITEM spec/item-a BLOCKED:grant-missing" <<<"$output"
}

@test "[POSTURE] a multi-item run resolves and prints posture exactly once" {
  test -f "$LINKED/.git"
  stub_env
  mk_branch spec/item-a a.txt alpha
  mk_branch spec/item-b b.txt beta
  write_gh
  write_children
  RUN_ID=run-316
  python3 "$ROOT/lib/gates.py" grant "$RUN_ID" --action merge:pr-101 --reason t >/dev/null
  python3 "$ROOT/lib/gates.py" grant "$RUN_ID" --action merge:pr-102 --reason t >/dev/null
  PATH="$MOCK_BIN:$PATH" run bash "$QUEUE" \
    --repo "$WORK" --base main --run-id "$RUN_ID" spec/item-a spec/item-b
  [ "$status" -eq 0 ]
  grep -q "ITEM spec/item-a LANDED" <<<"$output"
  grep -q "ITEM spec/item-b LANDED" <<<"$output"
  [ "$(grep -c "^POSTURE-RESOLVED:" <<<"$output")" -eq 1 ]
}

@test "[POSTURE-SEAM] only the resolver reads FFS_AUTONOMY_POSTURE and consumers use the resolved value" {
  # single production read of the env override (expansion, not prose)
  readers="$(grep -rlF '${FFS_AUTONOMY_POSTURE' "$ROOT/scripts" "$ROOT/lib" \
    "$ROOT"/skills/*/scripts 2>/dev/null)"
  [ "$readers" = "$ROOT/scripts/gsd/autonomy-posture.sh" ]
  # the queue resolves once and prints provenance once
  [ "$(grep -c 'resolve_autonomy_posture "' "$ROOT/scripts/gsd/land-queue.sh")" -eq 1 ]
  [ "$(grep -c '^echo "POSTURE-RESOLVED:' "$ROOT/scripts/gsd/land-queue.sh")" -eq 1 ]
  # no consumer kept an independent pre-resolver posture read
  posture_no_hit -F '"$POSTURE"' "$ROOT/scripts/gsd/land-queue.sh"
  # every enumerated land-queue floor consumer sits behind the resolved
  # variable; identifiers mirror 03-PHASE2-CONTRACT.json posture_consumers
  # (used directly when the gitignored contract is present)
  python3 - "$ROOT" <<'PYEOF'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
lq = (root / "scripts/gsd/land-queue.sh").read_text()
contract = (root / ".planning/phases/03-consolidate-4-a-posture-docs"
            / "03-PHASE2-CONTRACT.json")
idents = []
if contract.is_file():
    doc = json.load(open(contract))
    for group in doc["posture_consumers"].values():
        for entry in group:
            src = entry["source"]
            if src["path"] == "scripts/gsd/land-queue.sh":
                idents.append(src["identifier"])
else:  # pinned mirror for checkouts without the gitignored contract
    idents = [
        'reviewer="$(command -v codex || command -v claude || true)"',
        'block_item "BLOCKED:no-cross-vendor-reviewer"',
        "note_review_invocation() {",
        'QUAR_IDX+=("$IDX")',
    ]
assert idents, "no land-queue posture consumers enumerated"
for ident in idents:
    assert ident in lq, f"contract consumer identifier missing live: {ident!r}"
# the two posture-diverging consumer classes read AUTONOMY_POSTURE only
assert lq.count('[ "$AUTONOMY_POSTURE" = "floor" ]') == 1, "floor block guard"
assert lq.count('[ "$AUTONOMY_POSTURE" = "zero" ]') == 2, "quarantine guards"
PYEOF
}
