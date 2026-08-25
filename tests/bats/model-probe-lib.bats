#!/usr/bin/env bats
# model-probe-lib.sh — extracted cached model-probe functions (spec-004 AC-009
# prerequisite). Side-effect-free sourcing, cache TTL, force-reprobe (AC-009
# EDGE-006), cache filename/env-var contract pinned identical to the
# pre-extraction model-fallback.sh behaviour.

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  LIB="$ROOT/scripts/gsd/model-probe-lib.sh"
  TMP="$BATS_TEST_TMPDIR"
  export GSD_FALLBACK_CACHE="$TMP/cache"
  cat > "$TMP/probe-ok.sh" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  cat > "$TMP/probe-fail.sh" <<'SH'
#!/usr/bin/env bash
exit 1
SH
  chmod +x "$TMP/probe-ok.sh" "$TMP/probe-fail.sh"
}

@test "sourcing the lib has NO side effects beyond defining functions (genuinely, not just cache-dir setup)" {
  # spec-004 fix round finding 15: the header's "no side effects beyond
  # defining functions" claim used to be false — mkdir ran unconditionally
  # at source time. mkdir now happens lazily inside each probe function.
  run bash -c ". '$LIB'; type probe_claude_model >/dev/null && type probe_codex_model >/dev/null && echo DEFINED"
  [ "$status" -eq 0 ]
  [ "$output" = "DEFINED" ]
  [ ! -d "$GSD_FALLBACK_CACHE" ]
}

@test "calling a probe function creates the cache dir lazily" {
  run env GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" bash -c ". '$LIB'; probe_claude_model claude-fable-5"
  [ "$status" -eq 0 ]
  [ -d "$GSD_FALLBACK_CACHE" ]
}

@test "probe_claude_model: ok probe caches ok, matches model-fallback.sh cache filename" {
  run env GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" bash -c ". '$LIB'; probe_claude_model claude-fable-5"
  [ "$status" -eq 0 ]
  [ -f "$GSD_FALLBACK_CACHE/claude-fable-5.status" ]
  [ "$(cat "$GSD_FALLBACK_CACHE/claude-fable-5.status")" = "ok" ]
}

@test "probe_claude_model: fail probe caches fail, function returns nonzero" {
  run env GSD_MODEL_PROBE_CMD="$TMP/probe-fail.sh" bash -c ". '$LIB'; probe_claude_model claude-fable-5"
  [ "$status" -eq 1 ]
  [ "$(cat "$GSD_FALLBACK_CACHE/claude-fable-5.status")" = "fail" ]
}

@test "probe_codex_model: separate cache key from probe_claude_model" {
  run env GSD_MODEL_PROBE_CMD_CODEX="$TMP/probe-ok.sh" bash -c ". '$LIB'; probe_codex_model gpt-5.6-sol"
  [ "$status" -eq 0 ]
  [ -f "$GSD_FALLBACK_CACHE/gpt-5.6-sol.status" ]
}

@test "TTL cache: second call within 24h does not re-probe" {
  PROBE_LOG="$TMP/probe.log"
  cat > "$TMP/probe-log.sh" <<SH
#!/usr/bin/env bash
echo probed >> "$PROBE_LOG"
exit 0
SH
  chmod +x "$TMP/probe-log.sh"
  GSD_MODEL_PROBE_CMD="$TMP/probe-log.sh" bash -c ". '$LIB'; probe_claude_model claude-fable-5"
  GSD_MODEL_PROBE_CMD="$TMP/probe-log.sh" bash -c ". '$LIB'; probe_claude_model claude-fable-5" || true
  [ "$(wc -l < "$PROBE_LOG" | tr -d ' ')" -eq 1 ]
}

@test "GSD_MODEL_PROBE_FORCE=1 bypasses the TTL cache (AC-009 EDGE-006 doctor re-probe)" {
  PROBE_LOG="$TMP/probe.log"
  cat > "$TMP/probe-log.sh" <<SH
#!/usr/bin/env bash
echo probed >> "$PROBE_LOG"
exit 0
SH
  chmod +x "$TMP/probe-log.sh"
  GSD_MODEL_PROBE_CMD="$TMP/probe-log.sh" bash -c ". '$LIB'; probe_claude_model claude-fable-5"
  GSD_MODEL_PROBE_CMD="$TMP/probe-log.sh" GSD_MODEL_PROBE_FORCE=1 \
    bash -c ". '$LIB'; probe_claude_model claude-fable-5"
  [ "$(wc -l < "$PROBE_LOG" | tr -d ' ')" -eq 2 ]
}

@test "expired cache (mtime beyond TTL) forces a re-probe" {
  PROBE_LOG="$TMP/probe.log"
  cat > "$TMP/probe-log.sh" <<SH
#!/usr/bin/env bash
echo probed >> "$PROBE_LOG"
exit 0
SH
  chmod +x "$TMP/probe-log.sh"
  GSD_MODEL_PROBE_CMD="$TMP/probe-log.sh" bash -c ". '$LIB'; probe_claude_model claude-fable-5"
  # simulate the 24h cache expiring
  touch -t 202001010000 "$GSD_FALLBACK_CACHE/claude-fable-5.status"
  GSD_MODEL_PROBE_CMD="$TMP/probe-log.sh" bash -c ". '$LIB'; probe_claude_model claude-fable-5"
  [ "$(wc -l < "$PROBE_LOG" | tr -d ' ')" -eq 2 ]
}

@test "a missing sibling run-bounded.sh fails loudly, not silently" {
  ISOLATED="$TMP/isolated"
  mkdir -p "$ISOLATED"
  cp "$LIB" "$ISOLATED/model-probe-lib.sh"   # deliberately WITHOUT run-bounded.sh
  run bash -c ". '$ISOLATED/model-probe-lib.sh'"
  [ "$status" -ne 0 ]
  [[ "$output" == *"cannot locate run-bounded.sh"* ]]
}

@test "GSD_FALLBACK_CACHE override is honored" {
  ALT="$TMP/alt-cache"
  run env GSD_FALLBACK_CACHE="$ALT" GSD_MODEL_PROBE_CMD="$TMP/probe-ok.sh" \
    bash -c ". '$LIB'; probe_claude_model claude-opus-5"
  [ "$status" -eq 0 ]
  [ -f "$ALT/claude-opus-5.status" ]
}
