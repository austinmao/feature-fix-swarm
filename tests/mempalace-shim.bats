#!/usr/bin/env bats
# TDD RED for scripts/gsd/mempalace — gbrain-backed shim for gsd-core's
# headless MemPalace CLI seam (spec 002 Phase D). Subcommands used by
# gsd-mempalace-capture/-recall: mine, wake-up, search.

setup() {
  SHIM="$BATS_TEST_DIRNAME/../scripts/gsd/mempalace"
  # fake gbrain on PATH recording argv; emits canned search output
  FAKEBIN="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$FAKEBIN"
  cat > "$FAKEBIN/gbrain" <<'EOF'
#!/usr/bin/env bash
echo "$@" >> "$GBRAIN_LOG"
case "$1" in
  search|query) echo "hit: prior decision about $2" ;;
  doctor) echo "[OK] connection: Connected, 3 pages" ;;
esac
exit 0
EOF
  chmod +x "$FAKEBIN/gbrain"
  export PATH="$FAKEBIN:$PATH"
  export GBRAIN_LOG="$BATS_TEST_TMPDIR/gbrain.log"
  : > "$GBRAIN_LOG"
  ART="$BATS_TEST_TMPDIR/CONTEXT.md"
  printf 'Decision: adopt X because Y\n' > "$ART"
}

@test "mine files artifact via gbrain put with deterministic key" {
  run bash "$SHIM" mine "$ART" --wing ffs --room decisions
  [ "$status" -eq 0 ]
  grep -q "^put mempalace/ffs/decisions/" "$GBRAIN_LOG"
  # sync after put (index update discipline)
  grep -q "^sync" "$GBRAIN_LOG"
}

@test "mine is idempotent — same content twice = same key" {
  bash "$SHIM" mine "$ART" --wing ffs --room decisions
  bash "$SHIM" mine "$ART" --wing ffs --room decisions
  keys=$(grep "^put " "$GBRAIN_LOG" | awk '{print $2}' | sort -u | wc -l | tr -d ' ')
  [ "$keys" -eq 1 ]
}

@test "search queries gbrain scoped to wing" {
  run bash "$SHIM" search "auth tokens" --wing ffs
  [ "$status" -eq 0 ]
  [[ "$output" == *"hit:"* ]]
  grep -q "mempalace/ffs" "$GBRAIN_LOG"
}

@test "wake-up returns brief wing summary" {
  run bash "$SHIM" wake-up --wing ffs
  [ "$status" -eq 0 ]
  grep -q "mempalace/ffs" "$GBRAIN_LOG"
}

@test "missing artifact exits 1 with message" {
  run bash "$SHIM" mine "$BATS_TEST_TMPDIR/nope.md" --wing ffs --room decisions
  [ "$status" -eq 1 ]
  [[ "$output" == *"not found"* ]]
}

@test "gbrain absent exits 1 with unavailable message (capture is onError:skip upstream)" {
  rm "$FAKEBIN/gbrain"
  run env PATH="/usr/bin:/bin" bash "$SHIM" mine "$ART" --wing ffs --room decisions
  [ "$status" -eq 1 ]
  [[ "$output" == *"unavailable"* ]]
}

@test "usage error exits 2" {
  run bash "$SHIM" bogus-subcommand
  [ "$status" -eq 2 ]
}
