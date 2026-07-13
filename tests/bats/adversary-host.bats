#!/usr/bin/env bats

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  LIB="$ROOT/scripts/gsd/adversary-host.sh"
  unset FFS_HOST CODEX_SESSION_ID CODEX_THREAD_ID CODEX_AGENT CODEX_CI
  unset CLAUDE_SESSION_ID CLAUDE_CODE
}

@test "explicit FFS_HOST wins over ambient markers" {
  FFS_HOST=claude CODEX_THREAD_ID=thread-1 run bash -c ". '$LIB'; detect_orchestrator_host"
  [ "$status" -eq 0 ]
  [ "$output" = "claude" ]
}

@test "CODEX_THREAD_ID detects a Codex session" {
  CODEX_THREAD_ID=thread-1 run bash -c ". '$LIB'; detect_orchestrator_host"
  [ "$status" -eq 0 ]
  [ "$output" = "codex" ]
}

@test "CODEX_HOME alone is config, not proof of a Codex session" {
  CODEX_HOME=/tmp/codex-config FFS_HOST_PROCESS_DETECT=off \
    run bash -c ". '$LIB'; detect_orchestrator_host"
  [ "$status" -eq 0 ]
  [[ "$output" == *"defaulting to claude host"* ]]
  [[ "$output" == *"claude"* ]]
}

@test "opposite host mapping is symmetric" {
  run bash -c ". '$LIB'; adversary_kind_for_host claude"
  [ "$output" = "codex" ]
  run bash -c ". '$LIB'; adversary_kind_for_host codex"
  [ "$output" = "claude" ]
}

@test "two hung read-only reviewers share one overall fallback deadline" {
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR"
  cat > "$STUB_DIR/hung-codex" <<EOF
#!/usr/bin/env bash
touch "$BATS_TEST_TMPDIR/codex-started"
sleep 30
EOF
  cat > "$STUB_DIR/hung-claude" <<EOF
#!/usr/bin/env bash
touch "$BATS_TEST_TMPDIR/claude-started"
sleep 30
EOF
  chmod +x "$STUB_DIR/hung-codex" "$STUB_DIR/hung-claude"
  export PATH="$STUB_DIR:$PATH"

  start="$(date +%s)"
  run env ADVERSARY_BIN_CODEX=hung-codex ADVERSARY_BIN_CLAUDE=hung-claude \
    RUN_BOUNDED_KILL_AFTER=1 bash -c \
    ". '$LIB'; adversary_invoke_with_fallback codex claude 4 sol xhigh opus '' review"
  elapsed=$(( $(date +%s) - start ))

  [ "$status" -eq 124 ]
  [ -f "$BATS_TEST_TMPDIR/codex-started" ]
  [ -f "$BATS_TEST_TMPDIR/claude-started" ]
  [ "$elapsed" -lt 7 ]
  [[ "$output" == *"DEGRADED"* ]]
}

@test "Codex review returns only the official last-message payload" {
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR"
  cat > "$STUB_DIR/codex-transcript" <<'EOF'
#!/usr/bin/env bash
last_message=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o|--output-last-message)
      last_message="$2"
      shift 2
      ;;
    *) shift ;;
  esac
done
printf 'VERDICT: PASS\nhook: Stop\nVERDICT: PASS\ntokens used\n42\n'
if [ -n "$last_message" ]; then
  printf 'VERDICT: PASS\n' > "$last_message"
fi
EOF
  chmod +x "$STUB_DIR/codex-transcript"

  run env ADVERSARY_BIN_CODEX=codex-transcript PATH="$STUB_DIR:$PATH" \
    bash -c ". '$LIB'; adversary_invoke codex 10 sol xhigh review"

  [ "$status" -eq 0 ]
  [ "$output" = "VERDICT: PASS" ]
}

@test "review prompts stream on stdin instead of consuming one argv entry" {
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR"
  cat > "$STUB_DIR/stream-codex" <<EOF
#!/usr/bin/env bash
last_message=""
printf '%s\n' "\$@" > "$BATS_TEST_TMPDIR/codex.args"
while [ "\$#" -gt 0 ]; do
  case "\$1" in
    -o|--output-last-message)
      last_message="\$2"
      shift 2
      ;;
    *) shift ;;
  esac
done
cat > "$BATS_TEST_TMPDIR/codex.stdin"
printf 'VERDICT: PASS\n' > "\$last_message"
EOF
  cat > "$STUB_DIR/stream-claude" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$BATS_TEST_TMPDIR/claude.args"
cat > "$BATS_TEST_TMPDIR/claude.stdin"
printf 'VERDICT: PASS\n'
EOF
  chmod +x "$STUB_DIR/stream-codex" "$STUB_DIR/stream-claude"
  prompt_file="$BATS_TEST_TMPDIR/large-review-prompt.txt"
  dd if=/dev/zero bs=1200000 count=1 2>/dev/null | tr '\0' 'p' > "$prompt_file"
  printf '\nPROMPT_ONLY_ON_STDIN_ffs_47' >> "$prompt_file"

  run env ADVERSARY_BIN_CODEX=stream-codex PATH="$STUB_DIR:$PATH" \
    bash -c '. "$1"; prompt="$(cat "$2")"; adversary_invoke codex 10 sol xhigh "$prompt"' \
    _ "$LIB" "$prompt_file"
  [ "$status" -eq 0 ]
  [ "$output" = "VERDICT: PASS" ]
  cmp -s "$prompt_file" "$BATS_TEST_TMPDIR/codex.stdin"
  ! grep -Fq 'PROMPT_ONLY_ON_STDIN_ffs_47' "$BATS_TEST_TMPDIR/codex.args"

  run env ADVERSARY_BIN_CLAUDE=stream-claude PATH="$STUB_DIR:$PATH" \
    bash -c '. "$1"; prompt="$(cat "$2")"; adversary_invoke claude 10 opus "" "$prompt"' \
    _ "$LIB" "$prompt_file"
  [ "$status" -eq 0 ]
  [ "$output" = "VERDICT: PASS" ]
  cmp -s "$prompt_file" "$BATS_TEST_TMPDIR/claude.stdin"
  ! grep -Fq 'PROMPT_ONLY_ON_STDIN_ffs_47' "$BATS_TEST_TMPDIR/claude.args"
}

@test "forensic opt-out returns the preferred failure without starting fallback" {
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR"
  cat > "$STUB_DIR/preferred-fails" <<'EOF'
#!/usr/bin/env bash
exit 127
EOF
  cat > "$STUB_DIR/fallback-must-not-start" <<EOF
#!/usr/bin/env bash
touch "$BATS_TEST_TMPDIR/fallback-started"
printf 'VERDICT: PASS\n'
EOF
  chmod +x "$STUB_DIR/preferred-fails" "$STUB_DIR/fallback-must-not-start"

  run -127 env FFS_CROSS_VENDOR_FALLBACK=off \
    ADVERSARY_BIN_CODEX=preferred-fails \
    ADVERSARY_BIN_CLAUDE=fallback-must-not-start PATH="$STUB_DIR:$PATH" \
    bash -c ". '$LIB'; adversary_invoke_with_fallback codex claude 10 sol xhigh opus '' review"

  [ "$status" -eq 127 ]
  [ ! -f "$BATS_TEST_TMPDIR/fallback-started" ]
}
