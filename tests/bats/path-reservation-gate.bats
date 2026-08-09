#!/usr/bin/env bats
# path-reservation-gate.sh — PreToolUse (Edit|Write|MultiEdit) BLOCK (exit 2)
# on a foreign live EXCLUSIVE path lease. See scripts/hooks/path-reservation-gate.sh
# and .planning/phases/03-pretooluse-guard/03-01-PLAN.md for the full contract.

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  COORD="$ROOT/scripts/coord/coord.py"
  HOOK="$ROOT/scripts/hooks/path-reservation-gate.sh"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO"
  git -C "$REPO" init -q -b main
  git -C "$REPO" config user.email t@t
  git -C "$REPO" config user.name t
  git -C "$REPO" commit -q --allow-empty -m init
  STORE="$REPO/.feature-fix-swarm/coord"
}

teardown() {
  chmod -R u+rwx "$REPO" 2>/dev/null || true
}

# ── shared helpers ───────────────────────────────────────────────────────
envelope() {
  # envelope <tool_name> <file_path>
  python3 -c 'import json,sys; print(json.dumps({"tool_name":sys.argv[1],"tool_input":{"file_path":sys.argv[2]}}))' "$1" "$2"
}

acquire_as() {
  # acquire_as <run_id> <resource> <mode> [ttl]
  local run_id="$1" resource="$2" mode="$3" ttl="${4:-}"
  if [ -n "$ttl" ]; then
    env -C "$REPO" FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID="$run_id" \
      python3 "$COORD" lease-acquire --resource "$resource" --mode "$mode" --ttl "$ttl"
  else
    env -C "$REPO" FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID="$run_id" \
      python3 "$COORD" lease-acquire --resource "$resource" --mode "$mode"
  fi
}

session_of() {
  # session_of <run_id> — the session uuid a prior acquire_as minted for run_id
  env -C "$REPO" FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID="$1" python3 "$COORD" status >/dev/null
  # not used directly; tests capture session uuid from acquire output instead
  true
}

mkstub_python3() {
  # mkstub_python3 <exit_code> [marker_file] — PATH-shadow python3 with a
  # stub that (optionally) touches marker_file and exits <exit_code>.
  local rc="$1" marker="${2:-}"
  STUBDIR="$BATS_TEST_TMPDIR/stubbin-$BATS_TEST_NUMBER-$RANDOM"
  mkdir -p "$STUBDIR"
  {
    echo '#!/usr/bin/env bash'
    if [ -n "$marker" ]; then
      printf 'touch %q\n' "$marker"
    fi
    printf 'exit %s\n' "$rc"
  } >"$STUBDIR/python3"
  chmod +x "$STUBDIR/python3"
}

# ── Task 1: tracer — one path through every layer ───────────────────────

@test "T1: a foreign exclusive lease blocks an Edit, exit exactly 2" {
  run acquire_as holder "path:skills/**" exclusive
  [ "$status" -eq 0 ]
  holder_sid="${lines[0]#session=}"
  mkdir -p "$REPO/skills/feature-implement"
  touch "$REPO/skills/feature-implement/SKILL.md"

  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills/feature-implement/SKILL.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]
  [[ "$output" == *"${holder_sid:0:8}"* ]]
  [[ "$output" != *"$holder_sid"* ]]
}

@test "T1: block message carries the four mandated elements as real values" {
  run acquire_as holder "path:skills/**" exclusive
  [ "$status" -eq 0 ]
  holder_sid="${lines[0]#session=}"
  mkdir -p "$REPO/skills/feature-implement"
  touch "$REPO/skills/feature-implement/SKILL.md"

  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills/feature-implement/SKILL.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]
  [[ "$output" == *"${holder_sid:0:8}"* ]]
  [[ "$output" != *"$holder_sid"* ]]
  [[ "$output" =~ anchor_pid=[0-9]+ ]]
  [[ "$output" =~ worktree=/ ]]
  [[ "$output" =~ expires_at=[0-9] ]]
  [[ "$output" == *"path:skills/**"* ]]
  [[ "$output" == *"coord.py status"* ]]
  [[ "$output" == *"lease-release"* ]]
  [[ "$output" == *"--generation"* ]]
}

@test "T1: block message is RUNNABLE — real generation, held_by matches, no placeholders, release actually works" {
  run acquire_as holder "path:skills/**" exclusive
  [ "$status" -eq 0 ]
  holder_sid="${lines[0]#session=}"
  mkdir -p "$REPO/skills/feature-implement"
  touch "$REPO/skills/feature-implement/SKILL.md"

  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills/feature-implement/SKILL.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]
  [[ "$output" =~ --generation\ [0-9]+ ]]
  # truncated prefix only — the FULL uuid is the FFS_COORD_SESSION
  # impersonation token and must never appear in the block message
  [[ "$output" == *"held_by=${holder_sid:0:8}"* ]]
  [[ "$output" != *"$holder_sid"* ]]
  [[ "$output" != *"<N>"* ]]
  [[ "$output" != *"<key>"* ]]
  [[ "$output" != *"<uuid>"* ]]

  release_cmd="$(echo "$output" | grep -o 'lease-release --resource [^[:space:]]* --generation [0-9]*')"
  [ -n "$release_cmd" ]
  # shellcheck disable=SC2086
  run env -C "$REPO" FFS_RUN_ID=holder python3 "$COORD" $release_cmd
  [ "$status" -eq 0 ]

  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills/feature-implement/SKILL.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=someone-else bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "T1: sanctioned exits survive the handler — plain ALLOW and plain BLOCK, enforce and audit" {
  run acquire_as holder "path:skills/**" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/skills/feature-implement" "$REPO/docs"
  touch "$REPO/skills/feature-implement/SKILL.md" "$REPO/docs/pipeline.md"

  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/pipeline.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 0 ]
  [[ "$output" != *"COORD-GATE-FAIL"* ]]
  [[ "$output" != *"COORD-AUDIT"* ]]

  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills/feature-implement/SKILL.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]
  [[ "$output" == *"PATH-RESERVED"* ]]
  [[ "$output" != *"COORD-GATE-FAIL"* ]]

  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/pipeline.md' | FFS_COORD_MODE=audit CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 0 ]
  [[ "$output" != *"COORD-GATE-FAIL"* ]]
  [[ "$output" != *"COORD-AUDIT"* ]]

  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills/feature-implement/SKILL.md' | FFS_COORD_MODE=audit CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 0 ]
  [[ "$output" == *"COORD-AUDIT"* ]]
  [[ "$output" == *"PATH-RESERVED"* ]]
}

@test "T1: an uncovered path exits 0 with empty stderr" {
  run acquire_as holder "path:skills/**" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/docs"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/pipeline.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK' 2>&1 1>/dev/null"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "T1: hook reads stdin not argv — invoked with zero args; >256KB file_path still decides correctly" {
  run acquire_as holder "path:skills/**" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/skills/feature-implement"
  # Build the giant envelope in a FILE, not argv: Linux MAX_ARG_STRLEN caps a
  # single argument at 128KiB, so passing the >256KB path through the shell
  # killed the TEST harness itself on ubuntu ("Argument list too long")
  # before the hook ever ran. The hook reads stdin; feed it from the file.
  python3 - "$REPO" > "$BATS_TEST_TMPDIR/big-envelope.json" <<'PYEOF'
import json, sys
repo = sys.argv[1]
fp = repo + "/skills/feature-implement/" + ("x" * 300000) + ".md"
json.dump({"tool_name": "Edit", "tool_input": {"file_path": fp}}, sys.stdout)
PYEOF
  run bash -c "FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK' < '$BATS_TEST_TMPDIR/big-envelope.json'"
  [ "$status" -eq 2 ]
}

@test "T1: the envelope reaches json.load(sys.stdin) — store-present, non-Edit event exits 0, no COORD-GATE-FAIL" {
  run acquire_as holder "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  run bash -c "printf '%s' '{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"ls\"}}' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 0 ]
  [[ "$output" != *"COORD-GATE-FAIL"* ]]
}

@test "T1: no store at all exits 0 in enforce mode" {
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "T1: FFS_COORD_MODE=off exits 0 with a live blocking lease" {
  run acquire_as holder "path:skills/**" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/skills/feature-implement"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills/feature-implement/SKILL.md' | FFS_COORD_MODE=off CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "P-10: store-resolution parity, plain repo" {
  run env CLAUDE_PROJECT_DIR="$REPO" PATH_RESERVATION_GATE_RESOLVE_ONLY=1 bash "$HOOK"
  [ "$status" -eq 0 ]
  probe_store="$(echo "$output" | sed -n 's/^STORE=//p')"
  probe_wt="$(echo "$output" | sed -n 's/^WORKTREE=//p')"
  git_common="$(git -C "$REPO" rev-parse --path-format=absolute --git-common-dir)"
  expected_store="$(dirname "$git_common")/.feature-fix-swarm/coord"
  expected_wt="$(git -C "$REPO" rev-parse --show-toplevel)"
  [ "$probe_store" = "$expected_store" ]
  [ "$probe_wt" = "$expected_wt" ]
}

@test "P-10: store-resolution parity, linked worktree — shares the primary's store" {
  git -C "$REPO" worktree add -q "$BATS_TEST_TMPDIR/wt" -b wtbranch
  run env CLAUDE_PROJECT_DIR="$BATS_TEST_TMPDIR/wt" PATH_RESERVATION_GATE_RESOLVE_ONLY=1 bash "$HOOK"
  [ "$status" -eq 0 ]
  probe_store="$(echo "$output" | sed -n 's/^STORE=//p')"
  probe_wt="$(echo "$output" | sed -n 's/^WORKTREE=//p')"

  run env CLAUDE_PROJECT_DIR="$REPO" PATH_RESERVATION_GATE_RESOLVE_ONLY=1 bash "$HOOK"
  primary_store="$(echo "$output" | sed -n 's/^STORE=//p')"

  expected_wt="$(git -C "$BATS_TEST_TMPDIR/wt" rev-parse --show-toplevel)"
  [ "$probe_store" = "$primary_store" ]
  [ "$probe_wt" = "$expected_wt" ]
}

@test "P-10: store-resolution parity in a path containing a SPACE, plain and linked worktree" {
  SP="$BATS_TEST_TMPDIR/with space"
  mkdir -p "$SP"
  git -C "$SP" init -q -b main "$SP/repo" 2>/dev/null || git init -q -b main "$SP/repo"
  git -C "$SP/repo" config user.email t@t
  git -C "$SP/repo" config user.name t
  git -C "$SP/repo" commit -q --allow-empty -m init
  git -C "$SP/repo" worktree add -q "$SP/wt" -b wtsp

  run env CLAUDE_PROJECT_DIR="$SP/repo" PATH_RESERVATION_GATE_RESOLVE_ONLY=1 bash "$HOOK"
  [ "$status" -eq 0 ]
  primary_store="$(echo "$output" | sed -n 's/^STORE=//p')"
  git_common="$(git -C "$SP/repo" rev-parse --path-format=absolute --git-common-dir)"
  expected_store="$(dirname "$git_common")/.feature-fix-swarm/coord"
  [ "$primary_store" = "$expected_store" ]

  run env CLAUDE_PROJECT_DIR="$SP/wt" PATH_RESERVATION_GATE_RESOLVE_ONLY=1 bash "$HOOK"
  [ "$status" -eq 0 ]
  wt_store="$(echo "$output" | sed -n 's/^STORE=//p')"
  [ "$wt_store" = "$primary_store" ]
}

@test "P-10: walk resolves from a subdirectory with no .git of its own" {
  mkdir -p "$REPO/a/b/c"
  run env CLAUDE_PROJECT_DIR="$REPO/a/b/c" PATH_RESERVATION_GATE_RESOLVE_ONLY=1 bash "$HOOK"
  [ "$status" -eq 0 ]
  sub_store="$(echo "$output" | sed -n 's/^STORE=//p')"

  run env CLAUDE_PROJECT_DIR="$REPO" PATH_RESERVATION_GATE_RESOLVE_ONLY=1 bash "$HOOK"
  root_store="$(echo "$output" | sed -n 's/^STORE=//p')"
  [ "$sub_store" = "$root_store" ]
}

@test "P-11: not a git repo at all -> exit 0, marker-stub proof not required" {
  OUTSIDE="$BATS_TEST_TMPDIR/not-a-repo"
  mkdir -p "$OUTSIDE"
  run bash -c "$(declare -f envelope); envelope Edit '$OUTSIDE/x.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$OUTSIDE' bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "P-10/P-11: the delegation path still blocks — empty argv[2], invoked from OUTSIDE the repo" {
  run acquire_as holder "path:skills/**" exclusive
  [ "$status" -eq 0 ]
  holder_sid="${lines[0]#session=}"
  mkdir -p "$REPO/skills/feature-implement" "$REPO/docs"
  touch "$REPO/skills/feature-implement/SKILL.md" "$REPO/docs/pipeline.md"

  cd "$BATS_TEST_TMPDIR"
  [ "$PWD" != "$REPO" ]

  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills/feature-implement/SKILL.md' | PATH_RESERVATION_GATE_FORCE_DELEGATE=1 FFS_COORD_MODE=enforce FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]
  [[ "$output" == *"${holder_sid:0:8}"* ]]
  [[ "$output" != *"$holder_sid"* ]]
  [[ "$output" != *"COORD-GATE-FAIL"* ]]
  [[ "$output" != *"not a git repository"* ]]

  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/pipeline.md' | PATH_RESERVATION_GATE_FORCE_DELEGATE=1 FFS_COORD_MODE=enforce FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "P-12: no session minting — five guard invocations add zero files to sessions/" {
  run acquire_as holder "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  before=$(find "$STORE/sessions" -maxdepth 1 -name '*.json' | wc -l)
  for i in 1 2 3 4 5; do
    run bash -c "$(declare -f envelope); envelope Edit '$REPO/other-$i.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  done
  after=$(find "$STORE/sessions" -maxdepth 1 -name '*.json' | wc -l)
  [ "$before" -eq "$after" ]
}

@test "P-12: own-session exemption via the run pointer" {
  run acquire_as holder "path:skills/**" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/skills/feature-implement"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills/feature-implement/SKILL.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=holder bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "P-12: own-session exemption via FFS_COORD_SESSION" {
  run acquire_as holder "path:skills/**" exclusive
  [ "$status" -eq 0 ]
  holder_sid="${lines[0]#session=}"
  mkdir -p "$REPO/skills/feature-implement"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills/feature-implement/SKILL.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_COORD_SESSION=$holder_sid bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "P-12: unresolvable identity blocks and the message says how to recover WITHOUT leaking the impersonation token" {
  run acquire_as holder "path:skills/**" exclusive
  [ "$status" -eq 0 ]
  holder_sid="${lines[0]#session=}"
  mkdir -p "$REPO/skills/feature-implement"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills/feature-implement/SKILL.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=nobody bash '$HOOK'"
  [ "$status" -eq 2 ]
  [[ "$output" == *"FFS_COORD_SESSION"* ]]
  # recovery guidance names the env var and the truncated holder prefix,
  # but the FULL uuid is the impersonation token — must never be printed
  [[ "$output" == *"held_by=${holder_sid:0:8}"* ]]
  [[ "$output" != *"$holder_sid"* ]]
}

@test "P-15: a corrupt registry.json in enforce mode exits EXACTLY 2, never 69" {
  run acquire_as holder "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  printf 'not json' >"$STORE/registry.json"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 2 ]
}

# ── Task 2: REQ-07 full decision matrix ─────────────────────────────────

@test "REQ-07: shared never blocks" {
  run acquire_as holder "path:docs/**" shared
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/docs"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/pipeline.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "REQ-07: a shared entry with two foreign holders still exits 0" {
  run acquire_as a "path:docs/**" shared
  [ "$status" -eq 0 ]
  run acquire_as b "path:docs/**" shared
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/docs"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/pipeline.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=c bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "REQ-07: a shared co-holder elsewhere is not exempt from a DIFFERENT session's unrelated exclusive lease" {
  # A co-holds a SHARED lease on an unrelated key -- that must not grant a
  # blanket exemption from B's exclusive lease on a different key A never
  # touched (own_uuid exemption is per-holder, not per-session-globally).
  run acquire_as a "path:other/**" shared
  [ "$status" -eq 0 ]
  run acquire_as b "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/x.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=a bash '$HOOK'"
  [ "$status" -eq 2 ]
}

@test "REQ-07: overlap forms — prefix blocks descendant, exact blocks only itself, prefix does not block itself as a bare file, segment-wise not char-prefix" {
  run acquire_as a "path:skills/**" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/skills/x"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills/x/b.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]

  run acquire_as b "path:docs/a.md" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/docs"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md.bak' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 0 ]
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md/x' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 0 ]

  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 0 ]

  run acquire_as c "path:ab/**" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/a"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/a/b.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "REQ-07/P-07: casefold blocks including the non-ASCII pair" {
  run acquire_as a "path:Docs/**" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/docs"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]

  strasse_lower=$(python3 -c "print('straße')")
  strasse_upper=$(python3 -c "print('STRASSE')")
  run acquire_as b "path:${strasse_lower}/**" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/$strasse_upper"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/$strasse_upper/x.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]
}

@test "REQ-07/P-19: NFC/NFD equivalence, both directions" {
  nfd=$(python3 -c "import unicodedata; print(unicodedata.normalize('NFD', 'café'))")
  nfc=$(python3 -c "import unicodedata; print(unicodedata.normalize('NFC', 'café'))")

  run acquire_as a "path:${nfd}/**" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/$nfc"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/$nfc/x.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]
}

@test "P-17: a provably-dead holder does NOT block, and the guard writes nothing" {
  ( sleep 60 ) &
  anchor_pid=$!
  run env -C "$REPO" FFS_COORD_ANCHOR_PID="$anchor_pid" FFS_RUN_ID=dying \
    python3 "$COORD" lease-acquire --resource "path:docs/**" --mode exclusive
  [ "$status" -eq 0 ]
  kill "$anchor_pid" 2>/dev/null || true
  wait "$anchor_pid" 2>/dev/null || true

  before="$(cat "$STORE/registry.json")"
  mkdir -p "$REPO/docs"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 0 ]
  after="$(cat "$STORE/registry.json")"
  [ "$before" = "$after" ]
}

@test "P-17: a live holder DOES block, and the guard never crashes on the (reclaimable, verdict) tuple" {
  ( sleep 60 ) &
  anchor_pid=$!
  run env -C "$REPO" FFS_COORD_ANCHOR_PID="$anchor_pid" FFS_RUN_ID=live \
    python3 "$COORD" lease-acquire --resource "path:docs/**" --mode exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/docs"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]
  kill "$anchor_pid" 2>/dev/null || true
  wait "$anchor_pid" 2>/dev/null || true
}

@test "P-19: a symlinked edit path resolves into the lease and is blocked" {
  run acquire_as a "path:skills/**" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/skills/feature-implement"
  touch "$REPO/skills/feature-implement/SKILL.md"
  ln -s "$REPO/skills/feature-implement/SKILL.md" "$REPO/shortcut.md"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/shortcut.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]
}

@test "P-19: a glob-charset filename and an embedded-newline filename are both blocked by a covering prefix lease" {
  run acquire_as a "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/docs"
  touch "$REPO/docs/a[1].md"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a[1].md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]

  nl_name="docs/a"$'\n'"b.md"
  touch "$REPO/$nl_name"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/$nl_name' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]
}

@test "P-19: a symlink escaping outward exits 0 with no traceback" {
  # Acquire the lease while out.md is still a normal in-tree file (the
  # acquire-time containment check would refuse a resource argument that is
  # already an outward-escaping symlink) -- THEN redirect it outside. No
  # repo-relative lease key can name the escaped target.
  mkdir -p "$BATS_TEST_TMPDIR/elsewhere"
  touch "$BATS_TEST_TMPDIR/elsewhere/secret.md"
  touch "$REPO/out.md"
  run acquire_as a "path:out.md" exclusive
  [ "$status" -eq 0 ]
  rm -f "$REPO/out.md"
  ln -s "$BATS_TEST_TMPDIR/elsewhere/secret.md" "$REPO/out.md"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/out.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 0 ]
  [[ "$output" != *"Traceback"* ]]
}

@test "REQ-07: audit mode warns with the same body and exits 0" {
  run acquire_as holder "path:skills/**" exclusive
  [ "$status" -eq 0 ]
  holder_sid="${lines[0]#session=}"
  mkdir -p "$REPO/skills/feature-implement"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills/feature-implement/SKILL.md' | FFS_COORD_MODE=audit CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 0 ]
  [[ "$output" == *"COORD-AUDIT"* ]]
  [[ "$output" == *"${holder_sid:0:8}"* ]]
  [[ "$output" != *"$holder_sid"* ]]
  [[ "$output" == *"path:skills/**"* ]]
}

@test "mode precedence: env beats file; file alone; neither -> enforce" {
  run acquire_as holder "path:skills/**" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/skills/feature-implement"
  mkdir -p "$STORE"
  printf 'enforce' >"$STORE/mode"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills/feature-implement/SKILL.md' | FFS_COORD_MODE=audit CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 0 ]
  [[ "$output" == *"COORD-AUDIT"* ]]

  printf 'audit' >"$STORE/mode"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills/feature-implement/SKILL.md' | CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 0 ]
  [[ "$output" == *"COORD-AUDIT"* ]]

  rm -f "$STORE/mode"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills/feature-implement/SKILL.md' | CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]
}

@test "an invalid mode value (env and file) is treated as enforce, quoting the offending value" {
  run acquire_as holder "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/docs"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=Enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]

  mkdir -p "$STORE"
  printf 'enfroce' >"$STORE/mode"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]
  rm -f "$STORE/mode"
}

# ── Task 2: REQ-08 failure modes ────────────────────────────────────────

@test "REQ-08: P-21 envelope taxonomy — malformed rows fail per mode, benign rows always pass" {
  run acquire_as holder "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  fp="$REPO/docs/a.md"

  malformed=(
    'not json'
    '[]'
    '"x"'
    '5'
    'null'
    '{"tool_name":"Edit","tool_input":"nope"}'
    '{"tool_name":"Edit"}'
    '{"tool_name":"Edit","tool_input":{"file_path":123}}'
    '{"tool_name":"Edit","tool_input":{"file_path":null}}'
    '{"tool_name":"Edit","tool_input":{"file_path":""}}'
  )
  for m in "${malformed[@]}"; do
    run bash -c "printf '%s' '$m' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
    [ "$status" -eq 2 ]
    [[ "$output" == *"COORD-GATE-FAIL"* ]]
    run bash -c "printf '%s' '$m' | FFS_COORD_MODE=audit CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
    [ "$status" -eq 0 ]
    [[ "$output" == *"COORD-AUDIT"* ]]
  done

  benign=(
    '{}'
    '{"tool_name":"Bash","tool_input":"anything"}'
    '{"tool_name":123,"tool_input":{}}'
    '{"tool_input":{"file_path":"'"$fp"'"}}'
    '{"tool_name":"Edit","tool_input":{"old_string":"a","new_string":"b"}}'
  )
  for b in "${benign[@]}"; do
    run bash -c "printf '%s' '$b' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
    [ "$status" -eq 0 ]
    [[ "$output" != *"COORD-GATE-FAIL"* ]]
    [[ "$output" != *"COORD-AUDIT"* ]]
    run bash -c "printf '%s' '$b' | FFS_COORD_MODE=audit CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
    [ "$status" -eq 0 ]
    [[ "$output" != *"COORD-GATE-FAIL"* ]]
    [[ "$output" != *"COORD-AUDIT"* ]]
  done
}

@test "REQ-08: the discriminator pin — Bash+non-dict tool_input passes, Edit+non-dict tool_input fails" {
  run acquire_as holder "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  run bash -c "printf '%s' '{\"tool_name\":\"Bash\",\"tool_input\":\"nope\"}' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 0 ]
  run bash -c "printf '%s' '{\"tool_name\":\"Edit\",\"tool_input\":\"nope\"}' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 2 ]
}

@test "REQ-08: unreadable store shape (i) — unparseable registry.json, enforce 2 / audit warns via coord's own WARNING line" {
  run acquire_as holder "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  printf 'not json' >"$STORE/registry.json"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 2 ]
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=audit CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 0 ]
  [[ "$output" == *"coord: WARNING unparseable registry.json"* ]]
}

@test "REQ-08: unreadable store shape (ii) — a structurally corrupt lease entry, enforce 2 / audit COORD-AUDIT" {
  run acquire_as holder "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  python3 -c "
import json
p = '$STORE/registry.json'
d = json.load(open(p))
d['leases'] = {'path:a': {'mode': 'shared', 'holders': []}}
json.dump(d, open(p, 'w'))
"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 2 ]
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=audit CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 0 ]
  [[ "$output" == *"COORD-AUDIT"* ]]
}

@test "REQ-08: unreadable store shape (iii) — chmod 000 registry.json, enforce 2 / audit COORD-AUDIT" {
  [ "$(id -u)" -eq 0 ] && skip "root cannot be denied read permission"
  run acquire_as holder "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  chmod 000 "$STORE/registry.json"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 2 ]
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=audit CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 0 ]
  [[ "$output" == *"COORD-AUDIT"* ]]
  chmod 644 "$STORE/registry.json"
}

@test "REQ-08: an unsearchable store directory (bash fast path) — chmod 000 .feature-fix-swarm, enforce 2 / audit 0" {
  [ "$(id -u)" -eq 0 ] && skip "root cannot be denied search permission"
  run acquire_as holder "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  chmod 000 "$REPO/.feature-fix-swarm"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 2 ]
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=audit CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 0 ]
  chmod 755 "$REPO/.feature-fix-swarm"
}

@test "REQ-08: the SAME chmod 000 store on the DELEGATION path — enforce 2 / audit 0, never 'not a git repository'" {
  [ "$(id -u)" -eq 0 ] && skip "root cannot be denied search permission"
  run acquire_as holder "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  chmod 000 "$REPO/.feature-fix-swarm"
  cd "$BATS_TEST_TMPDIR"

  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | PATH_RESERVATION_GATE_FORCE_DELEGATE=1 FFS_COORD_MODE=enforce bash '$HOOK'"
  [ "$status" -eq 2 ]
  [[ "$output" != *"not a git repository"* ]]

  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | PATH_RESERVATION_GATE_FORCE_DELEGATE=1 FFS_COORD_MODE=audit bash '$HOOK'"
  [ "$status" -eq 0 ]

  chmod 755 "$REPO/.feature-fix-swarm"
}

@test "REQ-08/P-16: filelock import failure exits 2 in enforce, cold and warm cache" {
  run acquire_as holder "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/other"
  FAKELIB="$BATS_TEST_TMPDIR/fakelib"
  mkdir -p "$FAKELIB"
  printf 'raise ImportError("stub: filelock unavailable")\n' >"$FAKELIB/filelock.py"

  # cold cache, uncovered path -- would ALLOW if filelock were fine; the
  # probe (before any cache consult) must still fail it closed.
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/other/x.md' | PYTHONPATH='$FAKELIB' FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 2 ]

  # warm the real cache on that same uncovered path (no fake filelock) --
  # a real ALLOW, then re-run with filelock broken: the probe runs BEFORE
  # any cache consultation, so a warm cache must not let this slip through.
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/other/x.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 0 ]
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/other/x.md' | PYTHONPATH='$FAKELIB' FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 2 ]
}

@test "REQ-08/P-16: filelock failure in audit, via BOTH the env-mode route and the store-mode-file-only route" {
  run acquire_as holder "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  FAKELIB="$BATS_TEST_TMPDIR/fakelib2"
  mkdir -p "$FAKELIB"
  printf 'raise ImportError("stub: filelock unavailable")\n' >"$FAKELIB/filelock.py"

  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | PYTHONPATH='$FAKELIB' FFS_COORD_MODE=audit CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 0 ]

  printf 'audit' >"$STORE/mode"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | PYTHONPATH='$FAKELIB' CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 0 ]
  rm -f "$STORE/mode"
}

@test "REQ-08: non-Edit-family events pass in every mode with a live blocking lease" {
  run acquire_as holder "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  for tool in Bash Read Grep NotebookEdit; do
    run bash -c "$(declare -f envelope); envelope $tool '$REPO/docs/a.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
    [ "$status" -eq 0 ]
  done
  run bash -c "printf '%s' '{\"tool_input\":{\"file_path\":\"$REPO/docs/a.md\"}}' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "P-20: Write, MultiEdit and Edit all reach the same verdict; tool_input with no file_path key passes" {
  run acquire_as holder "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  fp="$REPO/docs/a.md"
  run bash -c "printf '%s' '{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$fp\",\"content\":\"x\"}}' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 2 ]
  run bash -c "printf '%s' '{\"tool_name\":\"MultiEdit\",\"tool_input\":{\"file_path\":\"$fp\",\"edits\":[]}}' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 2 ]
  run bash -c "printf '%s' '{\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$fp\",\"old_string\":\"a\",\"new_string\":\"b\"}}' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 2 ]
  run bash -c "printf '%s' '{\"tool_name\":\"Edit\",\"tool_input\":{}}' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "P-15: no code leakage — a python core that dies without its handler still translates to exactly 2 / 0" {
  run acquire_as holder "path:docs/**" shared
  [ "$status" -eq 0 ]
  mkstub_python3 69
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | PATH=\"$STUBDIR:\$PATH\" FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 2 ]
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | PATH=\"$STUBDIR:\$PATH\" FFS_COORD_MODE=audit CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "REQ-07 last clause: the header states the Bash-tool gap and the kill switch honestly" {
  grep -q "Bash tool" "$HOOK"
  grep -q "FFS_COORD_MODE=off" "$HOOK"
  grep -q "NotebookEdit" "$HOOK"
}

# ── Task 3: cache (P-18) ─────────────────────────────────────────────────

@test "cache: the no-hole property — an acquire between two invocations blocks the second immediately" {
  mkdir -p "$REPO/docs"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 0 ]
  run acquire_as holder "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]
}

@test "cache: the reverse hole — a release is visible on the very next invocation" {
  run acquire_as holder "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/docs"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]
  run env -C "$REPO" FFS_RUN_ID=holder python3 "$COORD" lease-release --resource path:docs/** --generation 1
  [ "$status" -eq 0 ]
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "cache: differential equivalence across five cache states, blocked and unblocked paths" {
  run acquire_as holder "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/other"
  # warm the cache
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/other/x.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 0 ]
  [ -f "$STORE/guard-cache.json" ]

  check_both() {
    run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
    [ "$status" -eq 2 ]
    run bash -c "$(declare -f envelope); envelope Edit '$REPO/other/x.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
    [ "$status" -eq 0 ]
  }

  rm -f "$STORE/guard-cache.json"
  check_both

  run bash -c "$(declare -f envelope); envelope Edit '$REPO/other/x.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ -f "$STORE/guard-cache.json" ]
  check_both

  python3 -c "
import json
p = '$STORE/guard-cache.json'
d = json.load(open(p))
d['tag'] = [0, 0]
json.dump(d, open(p, 'w'))
"
  check_both

  printf 'x' >"$STORE/guard-cache.json"
  check_both

  python3 -c "
import os, json
p = '$STORE/guard-cache.json'
st = os.stat('$STORE/registry.json')
json.dump({'tag': [st.st_mtime, st.st_size], 'exclusive_index': 'nope'}, open(p, 'w'))
"
  check_both
}

@test "cache: poisoned cache — four-payload matrix (wrong digest, invalid index, missing key, honest warm)" {
  run acquire_as holder "path:skills/**" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/skills/feature-implement"
  fp="$REPO/skills/feature-implement/SKILL.md"

  current_tag() {
    python3 -c "
import os, json
st = os.stat('$STORE/registry.json')
print(json.dumps([st.st_mtime, st.st_size]))
"
  }
  current_digest() {
    python3 -c "
import hashlib
print(hashlib.sha256(open('$STORE/registry.json','rb').read()).hexdigest())
"
  }

  # 1. structurally complete, WRONG digest -> discarded, exit 2, no COORD-GATE-FAIL
  tag="$(current_tag)"
  python3 -c "
import json
json.dump({'tag': $tag, 'registry_sha256': '0'*64, 'exclusive_index': []}, open('$STORE/guard-cache.json', 'w'))
"
  run bash -c "$(declare -f envelope); envelope Edit '$fp' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]
  [[ "$output" != *"COORD-GATE-FAIL"* ]]

  # 2. structurally invalid index, correct tag+digest -> discarded, exit 2
  tag="$(current_tag)"
  digest="$(current_digest)"
  python3 -c "
import json
d = {'tag': $tag, 'registry_sha256': '$digest',
     'exclusive_index': [{'key': 'path:skills/**', 'holders': {'u': {'generation': 'not-an-int'}}}]}
json.dump(d, open('$STORE/guard-cache.json', 'w'))
"
  run bash -c "$(declare -f envelope); envelope Edit '$fp' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]
  [[ "$output" != *"COORD-GATE-FAIL"* ]]

  # 3. missing required key, correct tag -> shape rejection, exit 2
  tag="$(current_tag)"
  python3 -c "
import json
json.dump({'tag': $tag, 'exclusive_index': []}, open('$STORE/guard-cache.json', 'w'))
"
  run bash -c "$(declare -f envelope); envelope Edit '$fp' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]
  [[ "$output" != *"COORD-GATE-FAIL"* ]]

  # 4. honest warm cache -> byte-identical verdict to no-cache
  rm -f "$STORE/guard-cache.json"
  run bash -c "$(declare -f envelope); envelope Edit '$fp' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  no_cache_output="$output"
  no_cache_status="$status"
  [ -f "$STORE/guard-cache.json" ]
  run bash -c "$(declare -f envelope); envelope Edit '$fp' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq "$no_cache_status" ]
  [ "$output" = "$no_cache_output" ]
}

@test "cache: write refused when guard-cache.json is a symlink — verdict-invisible, enforce+audit x blocked+unblocked" {
  run acquire_as holder "path:skills/**" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/skills/feature-implement" "$REPO/docs"
  touch "$REPO/skills/feature-implement/SKILL.md"
  ln -s "$BATS_TEST_TMPDIR/elsewhere.json" "$STORE/guard-cache.json"

  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills/feature-implement/SKILL.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]
  [[ "$output" != *"COORD-GATE-FAIL"* ]]
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 0 ]
  [ -z "$output" ]

  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills/feature-implement/SKILL.md' | FFS_COORD_MODE=audit CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 0 ]
  [[ "$output" == *"COORD-AUDIT"* ]]
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=audit CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 0 ]

  [ ! -e "$BATS_TEST_TMPDIR/elsewhere.json" ]
}

@test "cache: a symlinked registry.json fails ELOOP, tag and digest cannot diverge" {
  run acquire_as holder "path:skills/**" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/skills/feature-implement"
  touch "$REPO/skills/feature-implement/SKILL.md"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills/feature-implement/SKILL.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]

  cp "$STORE/registry.json" "$BATS_TEST_TMPDIR/decoy.json"
  python3 -c "
import json
d = json.load(open('$BATS_TEST_TMPDIR/decoy.json'))
d['leases'] = {}
json.dump(d, open('$BATS_TEST_TMPDIR/decoy.json', 'w'))
"
  rm -f "$STORE/registry.json"
  ln -s "$BATS_TEST_TMPDIR/decoy.json" "$STORE/registry.json"

  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills/feature-implement/SKILL.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills/feature-implement/SKILL.md' | FFS_COORD_MODE=audit CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "cache: never load-bearing for a failure — a read-only store still blocks and still allows" {
  [ "$(id -u)" -eq 0 ] && skip "root can write a read-only directory"
  run acquire_as holder "path:skills/**" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/skills/feature-implement" "$REPO/docs"
  touch "$REPO/skills/feature-implement/SKILL.md"
  chmod 555 "$STORE"

  run bash -c "$(declare -f envelope); envelope Edit '$REPO/skills/feature-implement/SKILL.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 0 ]

  chmod 755 "$STORE"
}

@test "cache: staleness is not cached — a warm block stops blocking the invocation after the anchor dies" {
  ( sleep 60 ) &
  anchor_pid=$!
  run env -C "$REPO" FFS_COORD_ANCHOR_PID="$anchor_pid" FFS_RUN_ID=dying \
    python3 "$COORD" lease-acquire --resource "path:docs/**" --mode exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/docs"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 2 ]
  [ -f "$STORE/guard-cache.json" ]

  kill "$anchor_pid" 2>/dev/null || true
  wait "$anchor_pid" 2>/dev/null || true
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=other bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "cache: written atomically inside the store — 10 concurrent invocations leave no torn cache and no temp files" {
  run acquire_as holder "path:docs/**" shared
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/other"
  pids=()
  for i in $(seq 1 10); do
    ( bash -c "$(declare -f envelope); envelope Edit '$REPO/other/x-$i.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=r$i bash '$HOOK'" >/dev/null 2>&1 ) &
    pids+=("$!")
  done
  for p in "${pids[@]}"; do
    wait "$p" || true
  done
  run python3 -c "import json; json.load(open('$STORE/guard-cache.json'))"
  [ "$status" -eq 0 ]
  leftover=$(find "$STORE" -maxdepth 1 -name '.guard-cache.json.*.tmp' | wc -l)
  [ "$leftover" -eq 0 ]
}

@test "cache: the guard writes nothing else — registry.json, sessions/, and registry.lock are untouched by a mixed workload" {
  run acquire_as holder "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  before_registry="$(cat "$STORE/registry.json")"
  before_sessions="$(find "$STORE/sessions" -maxdepth 1 -type f | sort)"
  lock_existed_before=0
  [ -e "$STORE/registry.lock" ] && lock_existed_before=1
  mkdir -p "$REPO/other"
  for i in 1 2 3 4 5; do
    run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=w$i bash '$HOOK'"
    run bash -c "$(declare -f envelope); envelope Edit '$REPO/other/x-$i.md' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' FFS_RUN_ID=w$i bash '$HOOK'"
  done
  after_registry="$(cat "$STORE/registry.json")"
  after_sessions="$(find "$STORE/sessions" -maxdepth 1 -type f | sort)"
  [ "$before_registry" = "$after_registry" ]
  [ "$before_sessions" = "$after_sessions" ]
  if [ "$lock_existed_before" -eq 0 ]; then
    [ ! -e "$STORE/registry.lock" ]
  fi
}

# ── Task 3: off/no-store marker-stub proof + latency ────────────────────

@test "REQ-09: FFS_COORD_MODE=off never launches python" {
  MARKER="$BATS_TEST_TMPDIR/python-was-invoked-off"
  mkstub_python3 1 "$MARKER"
  run acquire_as holder "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | PATH=\"$STUBDIR:\$PATH\" FFS_COORD_MODE=off CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 0 ]
  [ ! -e "$MARKER" ]
}

@test "REQ-09: the no-store fast path never launches python" {
  MARKER="$BATS_TEST_TMPDIR/python-was-invoked-nostore"
  mkstub_python3 1 "$MARKER"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | PATH=\"$STUBDIR:\$PATH\" FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 0 ]
  [ ! -e "$MARKER" ]
}

@test "REQ-09: a mode file of 'off' never launches python" {
  run acquire_as holder "path:docs/**" exclusive
  [ "$status" -eq 0 ]
  printf 'off' >"$STORE/mode"
  MARKER="$BATS_TEST_TMPDIR/python-was-invoked-modefile"
  mkstub_python3 1 "$MARKER"
  run bash -c "$(declare -f envelope); envelope Edit '$REPO/docs/a.md' | PATH=\"$STUBDIR:\$PATH\" CLAUDE_PROJECT_DIR='$REPO' bash '$HOOK'"
  [ "$status" -eq 0 ]
  [ ! -e "$MARKER" ]
  rm -f "$STORE/mode"
}

@test "REQ-09: off/no-store latency — 20-rep median under 30ms" {
  starts=()
  ends=()
  for _ in $(seq 1 20); do
    starts+=("$EPOCHREALTIME")
    printf '%s' '{"tool_name":"Edit","tool_input":{"file_path":"'"$REPO"'/docs/a.md"}}' | FFS_COORD_MODE=off CLAUDE_PROJECT_DIR="$REPO" bash "$HOOK" >/dev/null 2>&1
    ends+=("$EPOCHREALTIME")
  done
  median_ms=$(python3 -c "
import sys
n = 20
starts = [float(x) for x in sys.argv[1:1+n]]
ends = [float(x) for x in sys.argv[1+n:1+2*n]]
deltas = sorted(e - s for s, e in zip(starts, ends))
median = deltas[n//2] if n % 2 else (deltas[n//2-1] + deltas[n//2]) / 2
print(median * 1000)
" "${starts[@]}" "${ends[@]}")
  echo "measured median: ${median_ms}ms (budget 30ms)"
  python3 -c "import sys; sys.exit(0 if float(sys.argv[1]) < 30.0 else 1)" "$median_ms"
}

# P3-W2: the test above only exercises the off-mode limb; this one pins the
# no-store limb (enforce mode, store never created). Budget 60ms: P3-W3
# measured p95 38.1ms on this limb — 60ms catches a python-launch regression
# (~100ms+) without flaking on CI jitter.
@test "REQ-09: no-store latency — 20-rep median under 60ms" {
  [ ! -e "$STORE" ]
  starts=()
  ends=()
  for _ in $(seq 1 20); do
    starts+=("$EPOCHREALTIME")
    printf '%s' '{"tool_name":"Edit","tool_input":{"file_path":"'"$REPO"'/docs/a.md"}}' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR="$REPO" bash "$HOOK" >/dev/null 2>&1
    ends+=("$EPOCHREALTIME")
  done
  median_ms=$(python3 -c "
import sys
n = 20
starts = [float(x) for x in sys.argv[1:1+n]]
ends = [float(x) for x in sys.argv[1+n:1+2*n]]
deltas = sorted(e - s for s, e in zip(starts, ends))
median = deltas[n//2] if n % 2 else (deltas[n//2-1] + deltas[n//2]) / 2
print(median * 1000)
" "${starts[@]}" "${ends[@]}")
  echo "measured median: ${median_ms}ms (budget 60ms)"
  python3 -c "import sys; sys.exit(0 if float(sys.argv[1]) < 60.0 else 1)" "$median_ms"
}

@test "REQ-09: enforce warm-path latency — 20-rep median under 240ms" {
  run acquire_as holder "path:elsewhere/**" exclusive
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/docs"
  # warm the cache
  printf '%s' '{"tool_name":"Edit","tool_input":{"file_path":"'"$REPO"'/docs/a.md"}}' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR="$REPO" bash "$HOOK" >/dev/null 2>&1
  starts=()
  ends=()
  for _ in $(seq 1 20); do
    starts+=("$EPOCHREALTIME")
    printf '%s' '{"tool_name":"Edit","tool_input":{"file_path":"'"$REPO"'/docs/a.md"}}' | FFS_COORD_MODE=enforce CLAUDE_PROJECT_DIR="$REPO" bash "$HOOK" >/dev/null 2>&1
    ends+=("$EPOCHREALTIME")
  done
  median_ms=$(python3 -c "
import sys
n = 20
starts = [float(x) for x in sys.argv[1:1+n]]
ends = [float(x) for x in sys.argv[1+n:1+2*n]]
deltas = sorted(e - s for s, e in zip(starts, ends))
median = deltas[n//2] if n % 2 else (deltas[n//2-1] + deltas[n//2]) / 2
print(median * 1000)
" "${starts[@]}" "${ends[@]}")
  echo "measured median: ${median_ms}ms (budget 240ms)"
  python3 -c "import sys; sys.exit(0 if float(sys.argv[1]) < 240.0 else 1)" "$median_ms"
}

# ── settings.json (AC-009's last clause) ────────────────────────────────

@test "settings.json: Edit|Write|MultiEdit entry exists once with timeout 10, three pre-existing hooks intact" {
  run python3 -c "
import json
d = json.load(open('$ROOT/.claude/settings.json'))
hooks = d['hooks']['PreToolUse']
entries = [m for m in hooks if m['matcher'] == 'Edit|Write|MultiEdit']
assert len(entries) == 1, entries
h = entries[0]['hooks'][0]
assert h['timeout'] == 10, h
assert h['type'] == 'command', h
all_cmds = [x['command'] for m in hooks for x in m['hooks']]
assert sum('gsd-phase-evidence-gate' in c for c in all_cmds) == 1
assert sum('cli-hang-guard' in c for c in all_cmds) == 1
assert sum('credential-output-guard' in c for c in all_cmds) == 1
assert sum('path-reservation-gate' in c for c in all_cmds) == 1
"
  [ "$status" -eq 0 ]
}
