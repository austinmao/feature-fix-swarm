#!/usr/bin/env bats
# learnings-harvest.sh — fail-soft learnings harvester (gbrain-or-archive)
# Asserts: healthy-backend write, archive fallback (no gbrain / unreachable
# gbrain), malformed-line skip+count (EDGE-006), empty/missing (AC-003).

SCRIPT="scripts/gsd/learnings-harvest.sh"

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  cd "$REPO_ROOT"
  TMP="$(mktemp -d)"
  MARKER="$TMP/gbrain-calls.log"

  # Minimal PATH: symlink only the core utils the lever needs, and
  # deliberately OMIT gbrain (this dev machine has a real gbrain on PATH,
  # e.g. ~/.bun/bin/gbrain — the "no gbrain" scenario must not see it).
  MINPATH="$TMP/minpath"
  mkdir -p "$MINPATH"
  for bin in bash find cat mkdir python3 jq shasum cksum mv rm sed grep wc tr dirname basename mktemp env; do
    b="$(command -v "$bin" 2>/dev/null)"
    [ -n "$b" ] && ln -sf "$b" "$MINPATH/$bin"
  done

  # Stub dir layered in front of MINPATH for tests that need a fake gbrain.
  STUB_DIR="$TMP/bin"
  mkdir -p "$STUB_DIR"
}

teardown() { rm -rf "$TMP"; }

seed_learnings() {
  # $1 = subdir under .planning, $2.. = JSONL lines to write
  local dir="$TMP/planning/$1"
  mkdir -p "$dir"
  local file="$dir/learnings.jsonl"
  : > "$file"
  shift
  for line in "$@"; do
    printf '%s\n' "$line" >> "$file"
  done
}

@test "healthy gbrain on PATH: N entries harvested, backend actually invoked" {
  seed_learnings "phase-01" '{"note":"a"}' '{"note":"b"}'
  cat > "$STUB_DIR/gbrain" <<EOF
#!/usr/bin/env bash
case "\$1" in
  doctor) exit 0 ;;
  put) echo "PUT \$2 \$3" >> "$MARKER"; exit 0 ;;
  sync) exit 0 ;;
  *) exit 1 ;;
esac
EOF
  chmod +x "$STUB_DIR/gbrain"
  PATH="$STUB_DIR:$MINPATH" run bash "$SCRIPT" "$TMP/planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"2 harvested"* ]]
  [ -f "$MARKER" ]
  [ "$(grep -c '^PUT ' "$MARKER")" -eq 2 ]
}

@test "no gbrain on PATH: entries appended atomically to archive fallback" {
  seed_learnings "phase-01" '{"note":"a"}' '{"note":"b"}' '{"note":"c"}'
  ARCHIVE="$TMP/.feature-fix-swarm/learnings-archive.jsonl"
  mkdir -p "$TMP/.feature-fix-swarm"
  : > "$ARCHIVE"
  cd "$TMP"
  PATH="$MINPATH" run bash "$REPO_ROOT/$SCRIPT" "$TMP/planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"3 harvested"* ]]
  [ "$(wc -l < "$ARCHIVE" | tr -d ' ')" -eq 3 ]
}

@test "gbrain present but unreachable (doctor fails): warn + archive fallback, exit 0" {
  seed_learnings "phase-01" '{"note":"a"}'
  cat > "$STUB_DIR/gbrain" <<EOF
#!/usr/bin/env bash
case "\$1" in
  doctor) exit 1 ;;
  *) echo "UNEXPECTED \$*" >> "$MARKER"; exit 1 ;;
esac
EOF
  chmod +x "$STUB_DIR/gbrain"
  ARCHIVE="$TMP/.feature-fix-swarm/learnings-archive.jsonl"
  mkdir -p "$TMP/.feature-fix-swarm"
  : > "$ARCHIVE"
  cd "$TMP"
  PATH="$STUB_DIR:$MINPATH" run bash "$REPO_ROOT/$SCRIPT" "$TMP/planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"1 harvested"* ]]
  [ "$(wc -l < "$ARCHIVE" | tr -d ' ')" -eq 1 ]
  [ ! -f "$MARKER" ]
}

@test "malformed JSONL line mixed with valid: valid harvested, malformed skipped, exit 0 (EDGE-006)" {
  seed_learnings "phase-01" '{"note":"a"}' 'not-json-garbage{{' '{"note":"b"}'
  cd "$TMP"
  PATH="$MINPATH" run bash "$REPO_ROOT/$SCRIPT" "$TMP/planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"2 harvested"* ]]
  ARCHIVE="$TMP/.feature-fix-swarm/learnings-archive.jsonl"
  [ -f "$ARCHIVE" ]
  [ "$(wc -l < "$ARCHIVE" | tr -d ' ')" -eq 2 ]
}

@test "zero entries / missing .planning: exit 0, 0 harvested" {
  cd "$TMP"
  PATH="$MINPATH" run bash "$REPO_ROOT/$SCRIPT" "$TMP/nonexistent-planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"0 harvested"* ]]
}
