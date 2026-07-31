#!/usr/bin/env bats
# fallback-rehearsal.sh — "a backup you never ran is a hope" (tested-fallback
# discipline). The fallback chain fable -> gpt-5.6-sol -> opus is only real if
# its rungs have actually been RUN recently. This lever smoke-runs both rungs,
# records tested_on + per-rung results, and model-fallback.sh WARNs at
# fallback-ENGAGE time when the rehearsal record is missing or >30d stale.

REHEARSAL="scripts/gsd/fallback-rehearsal.sh"
FALLBACK="scripts/gsd/model-fallback.sh"

setup() {
  cd "$BATS_TEST_DIRNAME/../.."
  export GSD_FALLBACK_CACHE="$BATS_TEST_TMPDIR/cache"
  mkdir -p "$GSD_FALLBACK_CACHE"
  REHEARSAL_FILE="$GSD_FALLBACK_CACHE/rehearsal.json"
}

@test "both rungs ok: exit 0, rehearsal.json records tested_on + results" {
  run env GSD_REHEARSAL_CMD_CLAUDE=true GSD_REHEARSAL_CMD_CODEX=true \
    bash "$REHEARSAL"
  [ "$status" -eq 0 ]
  [ -f "$REHEARSAL_FILE" ]
  grep -q '"tested_on"' "$REHEARSAL_FILE"
  grep -q '"claude-opus-5": *"ok"' "$REHEARSAL_FILE"
  grep -q '"gpt-5.6-sol": *"ok"' "$REHEARSAL_FILE"
}

@test "a failed rung: exit 1, failure recorded not hidden" {
  run env GSD_REHEARSAL_CMD_CLAUDE=true GSD_REHEARSAL_CMD_CODEX=false \
    bash "$REHEARSAL"
  [ "$status" -eq 1 ]
  [ -f "$REHEARSAL_FILE" ]
  grep -q '"gpt-5.6-sol": *"fail"' "$REHEARSAL_FILE"
  [[ "$output" == *"FAIL"* ]]
}

@test "--dry-run prints the plan and writes nothing" {
  run env GSD_REHEARSAL_CMD_CLAUDE=true GSD_REHEARSAL_CMD_CODEX=true \
    bash "$REHEARSAL" --dry-run
  [ "$status" -eq 0 ]
  [ ! -f "$REHEARSAL_FILE" ]
  [[ "$output" == *"claude-opus-5"* ]]
  [[ "$output" == *"gpt-5.6-sol"* ]]
}

# ── model-fallback.sh engage-time staleness WARN ─────────────────────────────

engage_fallback() {
  # fable pinned, both live probes unavailable -> fallback engages (marker written)
  PLANNING="$BATS_TEST_TMPDIR/planning"
  mkdir -p "$PLANNING"
  printf '{"model_overrides":{"gsd-planner":"fable"}}\n' > "$PLANNING/config.json"
  run env GSD_MODEL_PROBE_CMD=false GSD_MODEL_PROBE_CMD_CODEX=false \
    bash "$FALLBACK" "$PLANNING"
}

@test "engaging fallback with NO rehearsal record warns: never rehearsed" {
  engage_fallback
  [ "$status" -eq 0 ]
  [[ "$output" == *"REHEARSAL"* ]]
  [[ "$output" == *"never"* ]]
}

@test "engaging fallback with fresh rehearsal record: no rehearsal WARN" {
  printf '{"tested_on":"2026-07-31","results":{"claude-opus-5":"ok","gpt-5.6-sol":"ok"}}\n' \
    > "$REHEARSAL_FILE"
  engage_fallback
  [ "$status" -eq 0 ]
  [[ "$output" != *"REHEARSAL"* ]]
}

@test "engaging fallback with >30d-old rehearsal record warns: stale" {
  printf '{"tested_on":"2026-01-01","results":{"claude-opus-5":"ok","gpt-5.6-sol":"ok"}}\n' \
    > "$REHEARSAL_FILE"
  touch -t 202601010000 "$REHEARSAL_FILE"
  engage_fallback
  [ "$status" -eq 0 ]
  [[ "$output" == *"REHEARSAL"* ]]
  [[ "$output" == *"stale"* ]]
}
