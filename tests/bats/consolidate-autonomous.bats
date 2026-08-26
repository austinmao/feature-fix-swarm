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
  write_stub "skills/land-queue/scripts/collect-queue.py" '
case "$1" in
  collect|precheck) cat "${QUEUE_DOC:?}" ;;
  *) echo "UNSTUBBED-BOUNDARY:collect-queue $*" >&2; exit 64 ;;
esac'
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
        echo "2222222222222222222222222222222222222222"
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
real="$(command -v -p git)"
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

  # queue-derived canonical target manifest from the REAL fixture estate
  export QUEUE_DOC="$BATS_TEST_TMPDIR/queue-doc.json"
  cat > "$QUEUE_DOC" <<JSON
{"schema": 1, "items": [{"branch": "spec/merged", "head": "$MERGED_OID", "pr": 201, "merge_sha": "$(git -C "$WORK" rev-parse refs/heads/main)"}], "count": 1}
JSON
  export ESTATE_DOC="$BATS_TEST_TMPDIR/estate-doc.json"
  printf '{"branches": []}\n' > "$ESTATE_DOC"

  # four evidence inputs, all present by default
  export CONSOLIDATE_EVIDENCE_DIR="$BATS_TEST_TMPDIR/evidence"
  mkdir -p "$CONSOLIDATE_EVIDENCE_DIR"
  for e in grant fresh-estate target-set assert-merged; do
    printf 'ok\n' > "$CONSOLIDATE_EVIDENCE_DIR/$e"
  done
  export GRANT_FILE="$BATS_TEST_TMPDIR/grants"
  TARGET_HASH="$(python3 -c "import hashlib,json,sys;print(hashlib.sha256(json.dumps([['spec/merged','$MERGED_OID']],separators=(',',':')).encode()).hexdigest())")"
  export CONSOLIDATE_RUN_ID="run-0304"
  printf '%s consolidate:estate:%s\n' "$CONSOLIDATE_RUN_ID" "$TARGET_HASH" > "$GRANT_FILE"
  export CONSOLIDATE_SCOPE="consolidate:estate:$TARGET_HASH"
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
  # same count (1), different branch tuple than the granted manifest
  cat > "$QUEUE_DOC" <<JSON
{"schema": 1, "items": [{"branch": "spec/unmerged", "head": "$UNMERGED_OID", "pr": 202, "merge_sha": "deadbeef"}], "count": 1}
JSON
  run run_block --execute
  [ "$status" -eq 1 ]
  [ ! -s "$EFFECTS" ]
}

@test "[TARGET] duplicate target tuple is refused" {
  fixture_sane; build_sandbox
  extract_block
  cat > "$QUEUE_DOC" <<JSON
{"schema": 1, "items": [{"branch": "spec/merged", "head": "$MERGED_OID", "pr": 201, "merge_sha": "x"}, {"branch": "spec/merged", "head": "$MERGED_OID", "pr": 201, "merge_sha": "x"}], "count": 2}
JSON
  run run_block --execute
  [ "$status" -eq 1 ]
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
  cat > "$QUEUE_DOC" <<JSON
{"schema": 1, "items": [{"branch": "spec/unmerged", "head": "$UNMERGED_OID", "pr": 202, "merge_sha": null}], "count": 1}
JSON
  run run_block --execute
  [ "$status" -eq 1 ]
  [ ! -s "$EFFECTS" ]
  git -C "$WORK" rev-parse -q --verify refs/heads/spec/unmerged >/dev/null
}

@test "[REFUSE] dirty worktree removal is refused with the worktree intact" {
  fixture_sane; build_sandbox
  extract_block
  run run_block --execute
  test -f "$DIRTY/uncommitted.txt"
  no_hit -a "worktree.remove" "$CALL_LOG"
}

@test "[REFUSE] checked-out branch deletion is refused" {
  fixture_sane; build_sandbox
  extract_block
  cat > "$QUEUE_DOC" <<JSON
{"schema": 1, "items": [{"branch": "spec/unmerged", "head": "$UNMERGED_OID", "pr": 202, "merge_sha": "x"}], "count": 1}
JSON
  run run_block --execute
  [ "$status" -eq 1 ]
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
  cat > "$QUEUE_DOC" <<JSON
{"schema": 1, "items": [{"branch": "main", "head": "$(git -C "$WORK" rev-parse refs/heads/main)", "pr": 203, "merge_sha": "x"}], "count": 1}
JSON
  run run_block --execute
  [ "$status" -eq 1 ]
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

@test "[SANDBOX] no absolute, traversing, or unknown slash-qualified escape" {
  fixture_sane; build_sandbox
  extract_block
  # static: the extracted block may not hardcode absolute source-tree paths
  # or parent traversal
  no_hit -En '(^|[["[:space:]=])/(Users|home|private|opt)/' "$RUNNER"
  no_hit -F '../' "$RUNNER"
  run run_block --execute
  no_hit -q "UNSTUBBED-BOUNDARY" <<<"$output"
  # runtime: every logged argv0 is a bare shim or a sandbox-relative stub
  python3 - "$CALL_LOG" <<'PYEOF'
import sys
known = {"gh", "codex", "claude", "git",
         "skills/land-queue/scripts/collect-queue.py",
         "skills/git-branch-consolidate/scripts/collect-estate.py",
         "lib/gates.py", "scripts/gsd/assert-merged.sh",
         "scripts/gsd/run-finalizer.sh"}
calls = open(sys.argv[1], "rb").read().split(b"\0")
tokens = [c.decode() for c in calls if c]
argv0s = {t for t in tokens if t in known or "/" in t}
bad = {t for t in argv0s if t not in known}
assert not bad, f"escaped the stub sandbox: {sorted(bad)}"
PYEOF
}
