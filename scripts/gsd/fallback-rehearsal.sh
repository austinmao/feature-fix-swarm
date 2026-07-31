#!/usr/bin/env bash
# fallback-rehearsal.sh — smoke-run the model-fallback rungs for real.
#
# "A backup you never ran is a hope": model-fallback.sh's chain
# (fable -> gpt-5.6-sol -> opus) is structurally verified by bats, but a rung
# that has never carried a LIVE call can still fail at the exact moment the
# primary is down. This lever runs one bounded trivial call through each
# fallback rung's CLI, records tested_on + per-rung results, and exits nonzero
# on any failed rung. model-fallback.sh reads the record at fallback-ENGAGE
# time and WARNs when it is missing or >30 days old.
#
# Subscription-only: both vendors' API keys are stripped so the smoke bills
# the logged-in plan, never a metered key (same contract as the probes).
#
# Usage: fallback-rehearsal.sh [--dry-run]
#   GSD_REHEARSAL_CMD_CLAUDE  override the claude smoke command (tests)
#   GSD_REHEARSAL_CMD_CODEX   override the codex smoke command (tests)
#   GSD_FALLBACK_CACHE        override cache dir (default ~/.cache/gsd-model-probe)
#   GSD_MODEL_PROBE_TIMEOUT   wall-clock bound per call (default 120s)
#
# Operator-invoked (monthly is fine) — never wired into CI: CI has no OAuth
# session, so a CI run would only ever record "fail".
set -euo pipefail

. "$(dirname "${BASH_SOURCE[0]:-$0}")/run-bounded.sh"

OPUS="claude-opus-5"
CODEX_SOL="gpt-5.6-sol"
CACHE_DIR="${GSD_FALLBACK_CACHE:-$HOME/.cache/gsd-model-probe}"
REHEARSAL_FILE="$CACHE_DIR/rehearsal.json"
PROBE_TIMEOUT="${GSD_MODEL_PROBE_TIMEOUT:-120}"

if [ "${1:-}" = "--dry-run" ]; then
  echo "[fallback-rehearsal] would smoke-run: $OPUS (claude CLI), $CODEX_SOL (codex CLI)"
  echo "[fallback-rehearsal] would record: $REHEARSAL_FILE"
  exit 0
fi

mkdir -p "$CACHE_DIR"

smoke_claude() {
  local cmd="${GSD_REHEARSAL_CMD_CLAUDE:-}"
  if [ -n "$cmd" ]; then $cmd "$OPUS" >/dev/null 2>&1; return; fi
  run_bounded "$PROBE_TIMEOUT" env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN \
    claude -p "ok" --model "$OPUS" --max-turns 1 </dev/null >/dev/null 2>&1
}

smoke_codex() {
  local cmd="${GSD_REHEARSAL_CMD_CODEX:-}"
  if [ -n "$cmd" ]; then $cmd "$CODEX_SOL" >/dev/null 2>&1; return; fi
  run_bounded "$PROBE_TIMEOUT" env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u OPENAI_API_KEY \
    codex exec -c "model=\"$CODEX_SOL\"" -c 'sandbox_mode="read-only"' "ok" \
    </dev/null >/dev/null 2>&1
}

OPUS_RESULT=fail
CODEX_RESULT=fail
if smoke_claude; then OPUS_RESULT=ok; fi
if smoke_codex; then CODEX_RESULT=ok; fi

TESTED_ON="$(date +%Y-%m-%d)"
# Record even a failed rehearsal: a recorded failure is loud in the file and in
# this exit code; hiding it would leave a stale-but-green record standing.
printf '{"tested_on":"%s","results":{"%s":"%s","%s":"%s"}}\n' \
  "$TESTED_ON" "$OPUS" "$OPUS_RESULT" "$CODEX_SOL" "$CODEX_RESULT" \
  > "$REHEARSAL_FILE"

echo "[fallback-rehearsal] $OPUS: $OPUS_RESULT, $CODEX_SOL: $CODEX_RESULT (recorded $TESTED_ON -> $REHEARSAL_FILE)"
if [ "$OPUS_RESULT" != ok ] || [ "$CODEX_RESULT" != ok ]; then
  echo "[fallback-rehearsal] FAIL: a fallback rung did not carry a live call — fix it BEFORE the primary goes down" >&2
  exit 1
fi
