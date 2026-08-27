#!/usr/bin/env bats
# Wave-0 RED contract for spec-006 Phase 3 autonomous consolidation (Step 4-A).
#
# Every scenario extracts and executes the ACTUAL marker-delimited Step 4-A
# block (PHASE3_CONSOLIDATE_BEGIN / PHASE3_CONSOLIDATE_END) from
# skills/git-branch-consolidate/SKILL.md inside a sandbox root whose exact
# slash-qualified command paths are deny-by-default logging stubs.  Until
# Phase 3 lands that block, extraction fails the specified-interface
# assertion and each test reports the typed behavioral marker
#   EXPECTED-RED:CONSOLIDATE:missing-production-seam
# Real local Git fixtures only; credentials cleared; no live vendor,
# no production deletion path.  UNSTUBBED-BOUNDARY is a failure sentinel:
# it must NEVER appear in successful output.

setup() {
  # Hermetic supported-host identity (mirrors tests/bats/land-queue.bats and
  # tests/bats/takeover-check.bats).
  export TAKEOVER_TEST_IDENTITY="bats-boot-1"
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  export REAL_ROOT="$ROOT"
  SKILL="$ROOT/skills/git-branch-consolidate/SKILL.md"
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
  export GIT_AUTHOR_NAME=wave0 GIT_AUTHOR_EMAIL=wave0@example.invalid
  export GIT_COMMITTER_NAME=wave0 GIT_COMMITTER_EMAIL=wave0@example.invalid
  unset GH_TOKEN GITHUB_TOKEN GIT_ASKPASS

  # Real temporary Git estate: bare file-protocol origin, working repo,
  # merged + unmerged branches, a linked (checked-out) worktree, and a
  # dirty worktree.
  ORIGIN="$BATS_TEST_TMPDIR/origin.git"; WORK="$BATS_TEST_TMPDIR/work"
  LINKED="$BATS_TEST_TMPDIR/linked"; DIRTY="$BATS_TEST_TMPDIR/dirty"
  git init -q --bare "$ORIGIN"
  git init -q -b main "$WORK"; cd "$WORK"
  echo base > README.md; git add README.md; git commit -qm base
  git remote add origin "$ORIGIN"; git push -q origin main
  git checkout -qb spec/merged; echo m > merged.txt
  git add merged.txt; git commit -qm merged; git push -q origin spec/merged
  git checkout -q main; git merge -q --no-ff -m "land spec/merged" spec/merged
  git push -q origin main
  git checkout -qb spec/unmerged main; echo u > unmerged.txt
  git add unmerged.txt; git commit -qm unmerged; git push -q origin spec/unmerged
  git checkout -q main
  git worktree add -q "$LINKED" spec/unmerged
  git checkout -qb spec/dirty main; echo d > dirty.txt
  git add dirty.txt; git commit -qm dirty; git push -q origin spec/dirty
  git checkout -q main
  git worktree add -q "$DIRTY" spec/dirty
  echo wip > "$DIRTY/uncommitted.txt"

  MERGED_OID="$(git rev-parse refs/heads/spec/merged)"
  UNMERGED_OID="$(git rev-parse refs/heads/spec/unmerged)"

  export CALL_LOG="$BATS_TEST_TMPDIR/calls"; : > "$CALL_LOG"
  export EFFECTS="$BATS_TEST_TMPDIR/effects"; : > "$EFFECTS"
  export GH_STATE="$BATS_TEST_TMPDIR/gh-state"; mkdir -p "$GH_STATE"
  printf '%s\n' "$MERGED_OID" > "$GH_STATE/head-201"
}

# ── sandbox: exact-path deny-by-default stubs + PATH shims ────────────────

write_stub() { # $1 sandbox-relative path, $2 stub body (after logging)
  local p="$SANDBOX/$1"
  mkdir -p "$(dirname "$p")"
  {
    printf '#!/usr/bin/env bash\n'
    printf 'printf "%%s\\0" %s "$@" >> "${CALL_LOG:?}"\n' "$1"
    printf '%s\n' "$2"
  } > "$p"
  chmod +x "$p"
}

write_shim() { # $1 bare command name, $2 body (after logging)
  local p="$SHIMS/$1"
  {
    printf '#!/usr/bin/env bash\n'
    printf 'printf "%%s\\0" %s "$@" >> "${CALL_LOG:?}"\n' "$1"
    printf '%s\n' "$2"
  } > "$p"
  chmod +x "$p"
}

build_sandbox() {
  SANDBOX="$BATS_TEST_TMPDIR/sandbox"; SHIMS="$BATS_TEST_TMPDIR/shims"
  mkdir -p "$SANDBOX" "$SHIMS" "$BATS_TEST_TMPDIR/home"

  # exact slash-qualified repository paths the Step 4-A block may call —
  # every one logs, obeys a narrow allow table, and denies everything else.
  write_stub "skills/land-queue/scripts/queue-journal.py" '
if [ -n "${QJ_OVERRIDE:-}" ]; then cat "$QJ_OVERRIDE"; exit "${QJ_RC:-0}"; fi
exec python3 "${REAL_ROOT:?}/skills/land-queue/scripts/queue-journal.py" "$@"'
  write_stub "skills/git-branch-consolidate/scripts/collect-estate.py" '
cat "${ESTATE_DOC:?}"'
  write_stub "lib/gates.py" '
case "$1" in
  check-grant)
    run="$2"; action=""
    while [ $# -gt 0 ]; do [ "$1" = "--action" ] && action="$2"; shift; done
    grep -qxF "$run $action" "${GRANT_FILE:?}" || exit 1
    exit 0 ;;
  *) echo "UNSTUBBED-BOUNDARY:gates $*" >&2; exit 64 ;;
esac'
  write_stub "scripts/gsd/assert-merged.sh" '
printf "assert-merged %s\n" "$*" >> "${EFFECTS_META:-/dev/null}"
exit "${AM_RC:-0}"'
  write_stub "scripts/gsd/run-finalizer.sh" '
printf "finalize %s\n" "$*" >> "${EFFECTS:?}"
exit 0'

  # PATH shims for bare commands: gh + reviewer CLIs deny-by-default; git
  # delegates an explicit read-only/local allowlist and rejects destructive
  # or network-capable argv.
  write_shim gh '
case "${1:-} ${2:-}" in
  "pr view")
    pr="$3"; shift 3
    if [ "$*" = "--json headRefOid -q .headRefOid" ]; then
      reads="${GH_STATE:?}/reads-$pr"; echo x >> "$reads"
      if [ "${GH_OID_DRIFT:-}" = "1" ] && [ "$(wc -l < "$reads")" -ge 2 ]; then
        o2="22222222222222222222"; echo "$o2$o2"
      else
        cat "${GH_STATE:?}/head-$pr"
      fi
      exit 0
    fi
    echo "UNSTUBBED-BOUNDARY:gh pr view $*" >&2; exit 64 ;;
  *) echo "UNSTUBBED-BOUNDARY:gh $*" >&2; exit 64 ;;
esac'
  write_shim codex 'echo "UNSTUBBED-BOUNDARY:codex $*" >&2; exit 64'
  write_shim claude 'echo "UNSTUBBED-BOUNDARY:claude $*" >&2; exit 64'
  write_shim git '
# command -v -p returns the shim itself here (bash 3.2 AND 5.3 ignore -p
# with -v), which would exec this shim forever; resolve real git from the
# sandbox PATH minus the shim dir instead.
real="$(PATH=/usr/bin:/bin command -v git)"
case "${1:-}" in
  rev-parse|status|log|diff|show|merge-base|for-each-ref|cat-file|ls-tree)
    exec "$real" "$@" ;;
  branch)
    for a in "$@"; do case "$a" in -D|-d|-m|-M|-c|-C)
      echo "UNSTUBBED-BOUNDARY:git $*" >&2; exit 64 ;; esac; done
    exec "$real" "$@" ;;
  worktree)
    [ "${2:-}" = "list" ] || { echo "UNSTUBBED-BOUNDARY:git $*" >&2; exit 64; }
    exec "$real" "$@" ;;
  ls-remote)
    exec "$real" "$@" ;;   # fixtures use file-protocol origins only
  *)
    echo "UNSTUBBED-BOUNDARY:git $*" >&2; exit 64 ;;
esac'

  # fresh-estate document with a REAL matching record for the default
  # target (WR-01) — never an empty branch list posing as evidence.
  export ESTATE_DOC="$BATS_TEST_TMPDIR/estate-doc.json"
  printf '{"branches": [{"branch": "spec/merged", "landed": true}]}\n' > "$ESTATE_DOC"

  # REAL durable queue journal (CR-01/WR-01): the default happy manifest is
  # built through the production accessor, never a synthetic document.
  export QJ_STORE="$BATS_TEST_TMPDIR/lqstore"
  mkdir -p "$QJ_STORE"; chmod 700 "$QJ_STORE"
  export CONSOLIDATE_QUEUE_STORE="$QJ_STORE"
  export CONSOLIDATE_QUEUE_ID="run-0304"
  make_real_journal run-0304 run-0304 \
    spec/merged "$MERGED_OID" 201 "$(git -C "$WORK" rev-parse refs/heads/main)"

  # four evidence inputs, all present by default
  export CONSOLIDATE_EVIDENCE_DIR="$BATS_TEST_TMPDIR/evidence"
  mkdir -p "$CONSOLIDATE_EVIDENCE_DIR"
  for e in grant fresh-estate target-set assert-merged; do
    printf 'ok\n' > "$CONSOLIDATE_EVIDENCE_DIR/$e"
  done
  export GRANT_FILE="$BATS_TEST_TMPDIR/grants"
  MAIN_OID="$(git -C "$WORK" rev-parse refs/heads/main)"
  TARGET_HASH="$(python3 -c "import hashlib,json,sys;print(hashlib.sha256(json.dumps([['spec/merged','$MERGED_OID','201','$MAIN_OID']],separators=(',',':')).encode()).hexdigest())")"
  export CONSOLIDATE_RUN_ID="run-0304"
  printf '%s consolidate:estate:%s\n' "$CONSOLIDATE_RUN_ID" "$TARGET_HASH" > "$GRANT_FILE"
  export CONSOLIDATE_SCOPE="consolidate:estate:$TARGET_HASH"
}

make_real_journal() { # $1 queue-id, $2 run-id, then (branch head pr merge)*
  local qid="$1" rid="$2" jrnl="$REAL_ROOT/skills/land-queue/scripts/queue-journal.py"
  shift 2
  python3 "$jrnl" init --store "$QJ_STORE" --queue-id "$qid" --run-id "$rid"
  while [ $# -gt 0 ]; do
    python3 "$jrnl" append --store "$QJ_STORE" --queue-id "$qid" \
      --kind intent --step merge --item "$1" --pr "$3" --head "$2"
    python3 "$jrnl" append --store "$QJ_STORE" --queue-id "$qid" \
      --kind terminal --step terminal --item "$1" --status LANDED --detail "$4"
    shift 4
  done
}

# ── the production seam: marker-delimited Step 4-A block ──────────────────

extract_block() {
  RUNNER="$BATS_TEST_TMPDIR/step4a.sh"
  awk '/PHASE3_CONSOLIDATE_BEGIN/{f=1;next} /PHASE3_CONSOLIDATE_END/{exit} f' \
    "$SKILL" | grep -v '^```' > "$RUNNER" || true
  if ! grep -q "PHASE3_CONSOLIDATE_BEGIN" "$SKILL" \
      || ! grep -q "PHASE3_CONSOLIDATE_END" "$SKILL" \
      || ! [ -s "$RUNNER" ]; then
    echo "EXPECTED-RED:CONSOLIDATE:missing-production-seam"
    echo "skills/git-branch-consolidate/SKILL.md has no marker-delimited," \
         "executable Step 4-A block yet (Phase 3 implementation pending)"
    return 1
  fi
  return 0
}

run_block() { # $@ -> args for the extracted block; cwd = sandbox root, never
  # the source checkout; PATH = shims + core utils only.
  local rc=0
  ( cd "$SANDBOX" && \
    PATH="$SHIMS:/usr/bin:/bin" HOME="$BATS_TEST_TMPDIR/home" \
    bash "$RUNNER" "$@" ) || rc=$?
  if [ "$rc" -eq 127 ]; then
    echo "UNSTUBBED-BOUNDARY:slash-qualified path missing from sandbox" >&2
  fi
  return "$rc"
}

no_hit() { # errexit-safe negation: fail when the pattern IS present (SC2314)
  if grep "$@"; then return 1; fi
  return 0
}

fixture_sane() { # failure here would be infrastructure, so prove it first
  test -f "$LINKED/.git"
  test -f "$DIRTY/uncommitted.txt"
  git -C "$WORK" rev-parse -q --verify refs/heads/spec/unmerged >/dev/null
  git -C "$ORIGIN" rev-parse -q --verify refs/heads/spec/merged >/dev/null
}

@test "[SEAM] marker-delimited Step 4-A block extracts as executable bash" {
  fixture_sane; build_sandbox
  extract_block
  bash -n "$RUNNER"
}

@test "[REPORT] report-only default records zero effects and exits 0" {
  fixture_sane; build_sandbox
  extract_block
  run run_block
  [ "$status" -eq 0 ]
  grep -qi "report" <<<"$output"
  [ ! -s "$EFFECTS" ]
}

@test "[EXECUTE] explicit --execute records only finalizer delegation" {
  fixture_sane; build_sandbox
  extract_block
  run run_block --execute
  [ "$status" -eq 0 ]
  grep -q "finalize" "$EFFECTS"
  # destructive truth is delegated: nothing but the finalizer touches estate
  no_hit -a "git.branch.*-D" "$CALL_LOG"
  no_hit -a "worktree.remove" "$CALL_LOG"
}

@test "[EVIDENCE] missing grant evidence refuses by name without the finalizer" {
  fixture_sane; build_sandbox
  extract_block
  rm -f "$CONSOLIDATE_EVIDENCE_DIR/grant"
  run run_block --execute
  [ "$status" -eq 1 ]
  grep -qi "grant" <<<"$output"
  [ ! -s "$EFFECTS" ]
}

@test "[EVIDENCE] missing fresh-estate evidence refuses by name without the finalizer" {
  fixture_sane; build_sandbox
  extract_block
  rm -f "$CONSOLIDATE_EVIDENCE_DIR/fresh-estate"
  run run_block --execute
  [ "$status" -eq 1 ]
  grep -qi "fresh-estate" <<<"$output"
  [ ! -s "$EFFECTS" ]
}

@test "[EVIDENCE] missing target-set evidence refuses by name without the finalizer" {
  fixture_sane; build_sandbox
  extract_block
  rm -f "$CONSOLIDATE_EVIDENCE_DIR/target-set"
  run run_block --execute
  [ "$status" -eq 1 ]
  grep -qi "target-set" <<<"$output"
  [ ! -s "$EFFECTS" ]
}

@test "[EVIDENCE] missing assert-merged evidence refuses by name without the finalizer" {
  fixture_sane; build_sandbox
  extract_block
  rm -f "$CONSOLIDATE_EVIDENCE_DIR/assert-merged"
  run run_block --execute
  [ "$status" -eq 1 ]
  grep -qi "assert-merged" <<<"$output"
  [ ! -s "$EFFECTS" ]
}

@test "[TARGET] equal-count wrong target set is refused" {
  fixture_sane; build_sandbox
  extract_block
  # same count (1), different branch tuple than the granted manifest —
  # built through the real journal so the projection itself is honest
  make_real_journal q-wrong run-0304 \
    spec/unmerged "$UNMERGED_OID" 202 "$(python3 -c "print('c'*40)")"
  export CONSOLIDATE_QUEUE_ID=q-wrong
  printf '{"branches": [{"branch": "spec/unmerged", "landed": true}]}\n' > "$ESTATE_DOC"
  run run_block --execute
  [ "$status" -eq 1 ]
  [ ! -s "$EFFECTS" ]
}

@test "[TARGET] duplicate target tuple is refused" {
  fixture_sane; build_sandbox
  extract_block
  # the real journal cannot emit duplicates, so this exercises the block's
  # own defense-in-depth validation of the projection output
  export QJ_OVERRIDE="$BATS_TEST_TMPDIR/qj-dup"
  M="$(python3 -c "print('e'*40)")"
  printf '%s\0%s\0%s\0%s\0%s\0%s\0%s\0%s\0' \
    spec/merged "$MERGED_OID" 201 "$M" \
    spec/merged "$MERGED_OID" 201 "$M" > "$QJ_OVERRIDE"
  run run_block --execute
  [ "$status" -eq 1 ]
  grep -qi "duplicate-target" <<<"$output"
  [ ! -s "$EFFECTS" ]
}

@test "[TARGET] post-proof OID drift is refused at the re-read" {
  fixture_sane; build_sandbox
  extract_block
  export GH_OID_DRIFT=1   # second headRefOid read returns a moved tip
  run run_block --execute
  [ "$status" -eq 1 ]
  [ ! -s "$EFFECTS" ]
}

@test "[GRANT] grant scope substitution is refused at the effect boundary" {
  fixture_sane; build_sandbox
  extract_block
  # a grant exists, but for a DIFFERENT queue-derived scope
  printf '%s consolidate:estate:%s\n' "$CONSOLIDATE_RUN_ID" \
    "$(printf 'other-scope' | shasum -a 256 | cut -d' ' -f1)" > "$GRANT_FILE"
  run run_block --execute
  [ "$status" -eq 1 ]
  grep -qi "grant" <<<"$output"
  [ ! -s "$EFFECTS" ]
}

@test "[GRANT] expired or absent grant is refused at the effect boundary" {
  fixture_sane; build_sandbox
  extract_block
  : > "$GRANT_FILE"   # check-grant fails closed exactly like an expired TTL
  run run_block --execute
  [ "$status" -eq 1 ]
  grep -qi "grant" <<<"$output"
  [ ! -s "$EFFECTS" ]
}

@test "[REFUSE] unmerged branch deletion is refused with branches unchanged" {
  fixture_sane; build_sandbox
  extract_block
  # a projection quad without an observed merge commit is never a target
  export QJ_OVERRIDE="$BATS_TEST_TMPDIR/qj-unmerged"
  printf '%s\0%s\0%s\0%s\0' spec/unmerged "$UNMERGED_OID" 202 "" > "$QJ_OVERRIDE"
  run run_block --execute
  [ "$status" -eq 1 ]
  grep -qi "no observed merge commit" <<<"$output"
  [ ! -s "$EFFECTS" ]
  git -C "$WORK" rev-parse -q --verify refs/heads/spec/unmerged >/dev/null
}

@test "[REFUSE] dirty worktree removal is refused with the worktree intact" {
  fixture_sane; build_sandbox
  extract_block
  # target the ACTUAL dirty-worktree branch with an otherwise valid-shaped
  # tuple; the fresh estate truthfully reports it not landed -> refusal
  make_real_journal q-dirty run-0304 \
    spec/dirty "$(git -C "$WORK" rev-parse refs/heads/spec/dirty)" 204 \
    "$(python3 -c "print('c'*40)")"
  export CONSOLIDATE_QUEUE_ID=q-dirty
  printf '{"branches": [{"branch": "spec/dirty", "landed": false, "worktree_dirty": 1}]}\n' > "$ESTATE_DOC"
  run run_block --execute
  [ "$status" -eq 1 ]
  grep -qi "fresh-estate" <<<"$output"
  [ ! -s "$EFFECTS" ]
  test -f "$DIRTY/uncommitted.txt"
  no_hit -a "worktree.remove" "$CALL_LOG"
}

@test "[REFUSE] checked-out branch deletion is refused" {
  fixture_sane; build_sandbox
  extract_block
  # the branch checked out in the linked worktree, valid-shaped evidence,
  # honest not-landed estate -> refused before any effect
  make_real_journal q-co run-0304 \
    spec/unmerged "$UNMERGED_OID" 202 "$(python3 -c "print('c'*40)")"
  export CONSOLIDATE_QUEUE_ID=q-co
  printf '{"branches": [{"branch": "spec/unmerged", "landed": false}]}\n' > "$ESTATE_DOC"
  run run_block --execute
  [ "$status" -eq 1 ]
  grep -qi "fresh-estate" <<<"$output"
  [ ! -s "$EFFECTS" ]
  test -f "$LINKED/.git"
  git -C "$WORK" rev-parse -q --verify refs/heads/spec/unmerged >/dev/null
}

@test "[REFUSE] force-delete -D is never issued for any target" {
  fixture_sane; build_sandbox
  extract_block
  run run_block --execute
  no_hit -a -- "-D" "$CALL_LOG"
  no_hit -q "UNSTUBBED-BOUNDARY" <<<"$output"
}

@test "[REFUSE] the base branch is never a deletion target" {
  fixture_sane; build_sandbox
  extract_block
  make_real_journal q-base run-0304 \
    main "$(git -C "$WORK" rev-parse refs/heads/main)" 203 \
    "$(python3 -c "print('c'*40)")"
  export CONSOLIDATE_QUEUE_ID=q-base
  run run_block --execute
  [ "$status" -eq 1 ]
  grep -qi "base branch is never a deletion target" <<<"$output"
  git -C "$ORIGIN" rev-parse -q --verify refs/heads/main >/dev/null
  [ ! -s "$EFFECTS" ]
}

@test "[ORDER] grant check, merge assertion, OID reread, finalizer — exact order" {
  fixture_sane; build_sandbox
  extract_block
  export EFFECTS_META="$BATS_TEST_TMPDIR/effects-meta"; : > "$EFFECTS_META"
  run run_block --execute
  [ "$status" -eq 0 ]
  python3 - "$CALL_LOG" <<'PYEOF'
import sys
calls = open(sys.argv[1], "rb").read().split(b"\0")
tokens = [c.decode() for c in calls if c]
def first(name):
    for i, t in enumerate(tokens):
        if t == name:
            return i
    raise AssertionError(f"{name} never called: {tokens}")
grant = first("lib/gates.py")
am = first("scripts/gsd/assert-merged.sh")
gh = first("gh")
fin = first("scripts/gsd/run-finalizer.sh")
assert grant < am < gh < fin, (grant, am, gh, fin, tokens)
PYEOF
}

@test "[JOURNAL] target manifest comes from the durable queue journal for the exact queue id" {
  # CR-01: the intake collector cannot emit pr/merge fields and returns an
  # empty item list in production — consolidation identity must come from
  # the journal read-landed-tuples projection for this exact queue id.
  fixture_sane; build_sandbox
  extract_block
  run run_block --execute
  [ "$status" -eq 0 ]
  grep -q "finalize" "$EFFECTS"
  python3 - "$CALL_LOG" <<'PYEOF'
import sys
calls = open(sys.argv[1], "rb").read().split(b"\0")
tokens = [c.decode() for c in calls if c]
jq = "skills/land-queue/scripts/queue-journal.py"
assert jq in tokens, f"queue journal accessor never consulted: {tokens}"
i = tokens.index(jq)
argv = tokens[i:i + 8]
assert "read-landed-tuples" in argv, argv
assert "run-0304" in argv, f"journal not read for the exact queue id: {argv}"
# the intake collector is never the consolidation identity source
for j, t in enumerate(tokens):
    if t == "skills/land-queue/scripts/collect-queue.py":
        assert tokens[j + 1] != "collect", \
            f"intake collector consulted for consolidation identity: {tokens}"
PYEOF
}

@test "[SANDBOX] no absolute, traversing, or unknown slash-qualified escape" {
  fixture_sane; build_sandbox
  extract_block
  # static: the extracted block may not hardcode absolute source-tree paths
  # or parent traversal
  no_hit -En '(^|[["[:space:]=])/(Users|home|private|opt)/' "$RUNNER"
  no_hit -F '../' "$RUNNER"
  run run_block --execute
  no_hit -q "UNSTUBBED-BOUNDARY" <<<"$output"
  # runtime: every logged argv0 is a bare shim or a sandbox-relative stub;
  # fixture paths under BATS_TEST_TMPDIR are stub ARGUMENTS handed through
  # the env contract (journal store, repo), not invocations
  python3 - "$CALL_LOG" "$BATS_TEST_TMPDIR" <<'PYEOF'
import sys
known = {"gh", "codex", "claude", "git",
         "skills/land-queue/scripts/queue-journal.py",
         "skills/git-branch-consolidate/scripts/collect-estate.py",
         "lib/gates.py", "scripts/gsd/assert-merged.sh",
         "scripts/gsd/run-finalizer.sh"}
tmpdir = sys.argv[2]
calls = open(sys.argv[1], "rb").read().split(b"\0")
tokens = [c.decode() for c in calls if c]
argv0s = {t for t in tokens if t in known or "/" in t}
bad = {t for t in argv0s
       if t not in known and not t.startswith(tmpdir)
       and not t.startswith("spec/")}
assert not bad, f"escaped the stub sandbox: {sorted(bad)}"
PYEOF
}
