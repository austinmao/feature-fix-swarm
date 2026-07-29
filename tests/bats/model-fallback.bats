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
    "gsd-verifier": "claude-opus-5"
  },
  "dynamic_routing": {
    "enabled": true,
    "tier_models": { "light": "claude-haiku-4-5-20251001", "standard": "claude-sonnet-5", "heavy": "claude-opus-5" }
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
  [[ "$output" == *"UNAVAILABLE -> claude-opus-5 (2 override(s) rewritten)"* ]]
  ! grep -q "claude-fable-5" "$TMP/.planning/config.json"
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-planner'])")" = "claude-opus-5" ]
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
{ "model_overrides": { "gsd-verifier": "claude-opus-5" } }
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
  [[ "$output" == *"UNAVAILABLE -> claude-opus-5 (2 override(s) rewritten)"* ]]
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
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-verifier'])")" = "claude-opus-5" ]

  # simulate the 24h cache expiring so the "fable is back" probe actually re-runs
  rm -f "$GSD_FALLBACK_CACHE/claude-fable-5.status"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"restored 2 path(s) — marker deleted"* ]]
  [ ! -f "$TMP/.planning/fable-fallback.json" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-planner'])")" = "claude-fable-5" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-plan-checker'])")" = "claude-fable-5" ]
  # intentional opus pin untouched by recovery — not blanket-flipped back to fable
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-verifier'])")" = "claude-opus-5" ]
}

@test "FALLBACK-009: no-fable config -> still no marker written" {
  cat > "$TMP/.planning/config.json" <<'JSON'
{ "model_overrides": { "gsd-verifier": "claude-opus-5" } }
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
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-plan-checker'])")" = "claude-opus-5" ]

  rm -f "$GSD_FALLBACK_CACHE/claude-fable-5.status"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [ ! -f "$TMP/.planning/fable-fallback.json" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-planner'])")" = "fable" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-plan-checker'])")" = "claude-fable-5" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-verifier'])")" = "opus" ]
}

@test "FALLBACK-013: fable inside a JSON array is substituted, not a TypeError crash" {
  # Regression: sub() recursed only into dicts, so a list value fell through to
  # `v in forms` -> hashing a list -> TypeError. The rewrite aborted entirely and
  # left a live fable pin in config.json, silently defeating the fallback chain.
  # Every FALLBACK-001..012 fixture is a flat dict, which is why this shipped green.
  cat > "$TMP/.planning/config.json" <<'JSON'
{
  "model_overrides": { "gsd-planner": "fable" },
  "review": { "default_reviewers": ["claude-fable-5", "opus", "fable"] },
  "nested": { "deep": [{ "model": "claude-fable-5" }, ["fable"]] }
}
JSON
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" != *"TypeError"* ]]
  [[ "$output" == *"(5 override(s) rewritten)"* ]]
  ! grep -q "fable" "$TMP/.planning/config.json"
}

@test "FALLBACK-014: array substitution is form-preserving and marker indexes list elements" {
  cat > "$TMP/.planning/config.json" <<'JSON'
{
  "review": { "default_reviewers": ["claude-fable-5", "opus", "fable"] },
  "nested": { "deep": [{ "model": "claude-fable-5" }, ["fable"]] }
}
JSON
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" bash "$LEVER" "$TMP/.planning" >/dev/null
  # alias stays alias, full ID stays full ID — even inside arrays
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['review']['default_reviewers'][0])")" = "claude-opus-5" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['review']['default_reviewers'][2])")" = "opus" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['nested']['deep'][1][0])")" = "opus" ]
  # an untouched non-fable array element must survive verbatim
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['review']['default_reviewers'][1])")" = "opus" ]
  # list elements are addressed by integer index, including list-inside-list
  [ "$(python3 -c "import json;p=json.load(open('$TMP/.planning/fable-fallback.json'))['paths'];print('review.default_reviewers.0' in p and 'nested.deep.1.0' in p)")" = "True" ]
}

@test "FALLBACK-015: recovery round-trips an array-bearing config byte-identical" {
  cat > "$TMP/.planning/config.json" <<'JSON'
{
  "model_overrides": { "gsd-planner": "fable", "gsd-verifier": "opus" },
  "review": { "default_reviewers": ["claude-fable-5", "opus", "fable"] },
  "nested": { "deep": [{ "model": "claude-fable-5" }, ["fable"]] }
}
JSON
  python3 -c "import json;json.dump(json.load(open('$TMP/.planning/config.json')),open('$TMP/before.json','w'),indent=2)"
  GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" bash "$LEVER" "$TMP/.planning" >/dev/null

  rm -f "$GSD_FALLBACK_CACHE/claude-fable-5.status"
  GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"restored 5 path(s) — marker deleted"* ]]
  [ ! -f "$TMP/.planning/fable-fallback.json" ]
  run python3 -c "
import json
a=json.load(open('$TMP/before.json')); b=json.load(open('$TMP/.planning/config.json'))
assert a==b, ('restore mismatch', a, b)
print('ok')"
  [ "$status" -eq 0 ]
}

@test "FALLBACK-012: real-CLI probes are wall-clock bounded (dead-CLI hang guard)" {
  # No GSD_MODEL_PROBE_CMD override -> the lever hits its real-CLI branch.
  # Shim claude/codex hang (sleep 30); GSD_MODEL_PROBE_TIMEOUT=1 must reap them
  # and read the probes as "unavailable" (fallback applied), never block.
  # This wall runs at the top of /feature-implement — pre-fix it had NO timeout
  # on any branch (2026-07-12 dead-codex forensics).
  mkdir -p "$TMP/shim"
  cat > "$TMP/shim/claude" <<'SH'
#!/bin/sh
sleep 30
SH
  cp "$TMP/shim/claude" "$TMP/shim/codex"
  chmod +x "$TMP/shim/claude" "$TMP/shim/codex"
  run env PATH="$TMP/shim:$PATH" GSD_MODEL_PROBE_TIMEOUT=1 bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"UNAVAILABLE"* ]]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['gsd-planner'])")" = "claude-opus-5" ]
}
