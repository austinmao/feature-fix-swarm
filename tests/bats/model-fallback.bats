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

@test "FALLBACK-006: fable down + codex sol up -> opus substituted, marker mode=codex-sol with correct paths" {
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"UNAVAILABLE -> claude-opus-4-8 (2 override(s) rewritten)"* ]]
  [ -f "$TMP/.planning/fable-fallback.json" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/fable-fallback.json'))['mode'])")" = "codex-sol" ]
  # paths maps each rewritten JSON path to its exact prior value (form-preserving restore)
  [ "$(python3 -c "import json;print(sorted(json.load(open('$TMP/.planning/fable-fallback.json'))['paths'].items()))")" = "[('model_overrides.gsd-plan-checker', 'claude-fable-5'), ('model_overrides.gsd-planner', 'claude-fable-5')]" ]
}

@test "FALLBACK-007: both fable and codex sol down -> opus substituted, marker mode=opus-only" {
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-fail.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/fable-fallback.json'))['mode'])")" = "opus-only" ]
}

@test "FALLBACK-008: fable back + marker -> restores only marker paths, intentional opus pins untouched, marker deleted" {
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" bash "$LEVER" "$TMP/.planning" >/dev/null
  [ -f "$TMP/.planning/fable-fallback.json" ]
  # gsd-verifier was an intentional pre-existing opus pin, not a fable rewrite
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-verifier'])")" = "claude-opus-4-8" ]

  # simulate the 24h cache expiring so the "fable is back" probe actually re-runs
  rm -f "$GSD_FALLBACK_CACHE/claude-fable-5.status"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"restored 2 path(s) — marker deleted"* ]]
  [ ! -f "$TMP/.planning/fable-fallback.json" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-planner'])")" = "claude-fable-5" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-plan-checker'])")" = "claude-fable-5" ]
  # intentional opus pin untouched by recovery — not blanket-flipped back to fable
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-verifier'])")" = "claude-opus-4-8" ]
}

@test "FALLBACK-009: no-fable config -> still no marker written" {
  cat > "$TMP/.planning/config.json" <<'JSON'
{ "model_overrides": { "gsd-verifier": "claude-opus-4-8" } }
JSON
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-fail.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [ ! -f "$TMP/.planning/fable-fallback.json" ]
}

@test "FALLBACK-010: alias-form config (template-shaped 'fable') -> substituted to 'opus', marker records path->'fable'" {
  cat > "$TMP/.planning/config.json" <<'JSON'
{ "model_overrides": { "gsd-planner": "fable", "gsd-verifier": "opus" } }
JSON
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"(1 override(s) rewritten)"* ]]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-planner'])")" = "opus" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/fable-fallback.json'))['paths']['model_overrides.gsd-planner'])")" = "fable" ]
}

@test "FALLBACK-011: mixed-form config -> both forms substituted, recovery restores exact form per path, opus pins untouched" {
  cat > "$TMP/.planning/config.json" <<'JSON'
{ "model_overrides": { "gsd-planner": "fable", "gsd-plan-checker": "claude-fable-5", "gsd-verifier": "opus" } }
JSON
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" bash "$LEVER" "$TMP/.planning" >/dev/null
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-planner'])")" = "opus" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-plan-checker'])")" = "claude-opus-4-8" ]

  rm -f "$GSD_FALLBACK_CACHE/claude-fable-5.status"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [ ! -f "$TMP/.planning/fable-fallback.json" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-planner'])")" = "fable" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-plan-checker'])")" = "claude-fable-5" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-verifier'])")" = "opus" ]
}
