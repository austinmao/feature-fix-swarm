#!/usr/bin/env bats
#
# Subscription-only auth guard.
#
# FFS runs on OAuth/subscription CLI sessions, never on a metered API key.
# Both vendor CLIs prefer an API key over the logged-in session when one is
# present in the environment, so an ambient OPENAI_API_KEY / ANTHROPIC_API_KEY
# (Doppler injection, a stale export, a CI secret) silently redirects billing
# away from the subscription with no error and no log line.
#
# The defense is to strip those vars at every call site. These tests assert
# the strip is actually there, because the failure mode is invisible at
# runtime: the call succeeds either way.

setup() {
  REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)"
}

# --- Codex side --------------------------------------------------------------

@test "model-fallback codex probe strips OPENAI_API_KEY" {
  run grep -n -B2 'codex exec' "$REPO_ROOT/scripts/gsd/model-fallback.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"-u OPENAI_API_KEY"* ]]
}

@test "gsd-run codex drive strips OPENAI_API_KEY" {
  run grep -nF -- 'CODEX_BIN" exec' "$REPO_ROOT/scripts/gsd/gsd-run.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"-u OPENAI_API_KEY"* ]]
}

@test "run_state audit does not inherit OPENAI_API_KEY into codex exec" {
  run grep -nF -- 'OPENAI_API_KEY' "$REPO_ROOT/lib/run_state/audit.py"
  [ "$status" -eq 0 ]
  # the codex spawn must pass an explicit scrubbed env, not the ambient one
  run grep -nA8 -- 'proc = subprocess.run' "$REPO_ROOT/lib/run_state/audit.py"
  [[ "$output" == *"env=_subscription_env()"* ]]
}

# --- Claude side (already guarded — lock it in) -------------------------------

@test "model-fallback claude probe strips ANTHROPIC_API_KEY" {
  run grep -n 'claude' "$REPO_ROOT/scripts/gsd/model-fallback.sh"
  [ "$status" -eq 0 ]
  run grep -c 'env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN' \
    "$REPO_ROOT/scripts/gsd/model-fallback.sh"
  [ "$output" -ge 1 ]
}

@test "gsd-run claude drive strips ANTHROPIC_API_KEY" {
  run grep -c 'env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN' \
    "$REPO_ROOT/scripts/gsd/gsd-run.sh"
  [ "$output" -ge 1 ]
}

# --- Blanket prohibitions ----------------------------------------------------

@test "no OpenRouter reference anywhere in active code" {
  run grep -rni 'openrouter' \
    "$REPO_ROOT/scripts" "$REPO_ROOT/lib" "$REPO_ROOT/templates" "$REPO_ROOT/setup.sh"
  [ "$status" -ne 0 ]
}

@test "no metered SDK is imported" {
  run grep -rnE '^[[:space:]]*(import|from)[[:space:]]+(anthropic|openai|litellm)\b' \
    "$REPO_ROOT/lib" "$REPO_ROOT/scripts"
  [ "$status" -ne 0 ]
}

@test "no direct model-endpoint HTTP calls" {
  run grep -rn 'api\.anthropic\.com\|api\.openai\.com\|/v1/chat/completions' \
    "$REPO_ROOT/scripts" "$REPO_ROOT/lib"
  [ "$status" -ne 0 ]
}

@test "no BASE_URL override that would redirect off the subscription endpoint" {
  run grep -rn 'ANTHROPIC_BASE_URL\|OPENAI_BASE_URL' \
    "$REPO_ROOT/scripts" "$REPO_ROOT/lib" "$REPO_ROOT/templates"
  [ "$status" -ne 0 ]
}
