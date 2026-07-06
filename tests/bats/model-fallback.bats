#!/usr/bin/env bats
# model-fallback.sh — probe-and-substitute lever tests (synthetic probes only)

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  LEVER="$REPO_ROOT/scripts/gsd/model-fallback.sh"
  TMP="$(mktemp -d)"
  export GSD_FALLBACK_CACHE="$TMP/cache"
  mkdir -p "$TMP/.planning"
  cat > "$TMP/.planning/config.json" <<'JSON'
{
  "model_overrides": {
    "gsd-planner": "claude-fable-5",
    "gsd-plan-checker": "claude-fable-5",
    "gsd-verifier": "claude-opus-4-8"
  },
  "dynamic_routing": {
    "enabled": true,
    "tier_models": { "light": "claude-haiku-4-5-20251001", "standard": "claude-sonnet-5", "heavy": "claude-opus-4-8" }
  }
}
JSON
  cat > "$TMP/probe-ok.sh" <<'SH'
#!/bin/sh
exit 0
SH
  cat > "$TMP/probe-fail.sh" <<'SH'
#!/bin/sh
exit 1
SH
  chmod +x "$TMP/probe-ok.sh" "$TMP/probe-fail.sh"
}

teardown() { rm -rf "$TMP"; }

@test "FALLBACK-001: fable unavailable -> rewritten to opus in all overrides" {
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"UNAVAILABLE -> claude-opus-4-8 (2 override(s) rewritten)"* ]]
  ! grep -q "claude-fable-5" "$TMP/.planning/config.json"
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-planner'])")" = "claude-opus-4-8" ]
}

@test "FALLBACK-002: fable available -> config byte-identical" {
  cp "$TMP/.planning/config.json" "$TMP/before.json"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"available — config unchanged"* ]]
  diff -q "$TMP/before.json" "$TMP/.planning/config.json"
}

@test "FALLBACK-003: probe cached — second run does not re-probe" {
  PROBE_LOG="$TMP/probe.log"
  cat > "$TMP/probe-log.sh" <<SH
#!/bin/sh
echo probed >> "$PROBE_LOG"
exit 1
SH
  chmod +x "$TMP/probe-log.sh"
  GSD_MODEL_PROBE_CMD="$TMP/probe-log.sh" bash "$LEVER" "$TMP/.planning"
  GSD_MODEL_PROBE_CMD="$TMP/probe-log.sh" bash "$LEVER" "$TMP/.planning" || true
  [ "$(wc -l < "$PROBE_LOG" | tr -d ' ')" -eq 1 ]
}

@test "FALLBACK-004: config without fable is a no-op (no probe)" {
  cat > "$TMP/.planning/config.json" <<'JSON'
{ "model_overrides": { "gsd-verifier": "claude-opus-4-8" } }
JSON
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [ ! -f "$GSD_FALLBACK_CACHE/claude-fable-5.status" ]
}

@test "FALLBACK-005: missing config errors" {
  run bash "$LEVER" "$TMP/nope"
  [ "$status" -eq 1 ]
  [[ "$output" == *"not found"* ]]
}
