#!/usr/bin/env bats
# assert-merged.sh — terminal-state assertion (MERGED / CLOSED / OPEN / gh error)

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  LEVER="$REPO_ROOT/scripts/gsd/assert-merged.sh"
  MOCK_BIN="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$MOCK_BIN"
  export PATH="$MOCK_BIN:$PATH"
}

mock_gh() {
  # mock_gh <state> <mergedAt-or-null>
  local state="$1" merged_at="$2"
  cat > "$MOCK_BIN/gh" <<EOF
#!/usr/bin/env bash
# minimal gh mock: only supports 'pr view ... --jq <expr>' as the lever calls it
if [ "\$1" = "pr" ] && [ "\$2" = "view" ]; then
  echo "$state $merged_at"
  exit 0
fi
exit 64
EOF
  chmod +x "$MOCK_BIN/gh"
}

@test "MERGED -> exit 0" {
  mock_gh "MERGED" "2026-07-11T00:00:00Z"
  run bash "$LEVER" 123
  [ "$status" -eq 0 ]
  [[ "$output" == *"PR #123 MERGED"* ]]
}

@test "CLOSED without merge -> exit 1, loud" {
  mock_gh "CLOSED" "null"
  run bash "$LEVER" 123
  [ "$status" -eq 1 ]
  [[ "$output" == *"CLOSED WITHOUT merge"* ]]
}

@test "still OPEN -> exit 2" {
  mock_gh "OPEN" "null"
  run bash "$LEVER" 123
  [ "$status" -eq 2 ]
  [[ "$output" == *"still OPEN"* ]]
}

@test "gh failure -> exit 3" {
  cat > "$MOCK_BIN/gh" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
  chmod +x "$MOCK_BIN/gh"
  run bash "$LEVER" 123
  [ "$status" -eq 3 ]
  [[ "$output" == *"gh pr view 123 failed"* ]]
}

@test "missing pr-number arg -> usage error" {
  run bash "$LEVER"
  [ "$status" -ne 0 ]
  [[ "$output" == *"usage:"* ]]
}

@test "repo arg passes through" {
  cat > "$MOCK_BIN/gh" <<'EOF'
#!/usr/bin/env bash
# assert --repo owner/name was forwarded
found=0
for a in "$@"; do [ "$a" = "acme/widgets" ] && found=1; done
[ "$found" -eq 1 ] || exit 64
echo "MERGED 2026-07-11T00:00:00Z"
EOF
  chmod +x "$MOCK_BIN/gh"
  run bash "$LEVER" 7 acme/widgets
  [ "$status" -eq 0 ]
}
