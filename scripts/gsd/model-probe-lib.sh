#!/usr/bin/env bash
# model-probe-lib.sh — sourceable lib: side-effect-free-on-source cached model
# availability probes, extracted from model-fallback.sh (spec-004 AC-009
# prerequisite).
#
# model-fallback.sh mixed probing with top-level config mutation; `setup.sh
# --doctor` (AC-009) needs the SAME probe machinery without any config
# rewrite, so the probe functions move here and model-fallback.sh re-sources
# them — behaviour pinned by the existing tests/bats/model-fallback.bats
# (cache filenames, env var names, and TTL semantics are UNCHANGED).
#
# Sourcing this file has NO side effects beyond defining functions and
# sourcing run-bounded.sh (also side-effect-free on source) — safe under
# `set -u`, safe for doctor to source read-only.
#
# Cache: one status file per model under CACHE_DIR, TTL 24h (1440 min).
# doctor (AC-009) needs to force a re-probe past the cache — pass
# GSD_MODEL_PROBE_FORCE=1 to bypass the TTL check on both probe functions
# (EDGE-006: a stale cached "ok" must never be trusted past TTL, and doctor's
# whole point is to catch exactly that staleness).
#
#   GSD_MODEL_PROBE_CMD        override the claude probe command (tests)
#   GSD_MODEL_PROBE_CMD_CODEX  override the codex-sol probe command (tests)
#   GSD_FALLBACK_CACHE         override cache dir (tests; default ~/.cache/gsd-model-probe)
#   GSD_MODEL_PROBE_TIMEOUT    wall-clock bound per real-CLI probe (default 120s)
#   GSD_MODEL_PROBE_FORCE=1    bypass the 24h cache TTL (doctor re-probe, AC-009)

_mpl_dir="$(dirname "${BASH_SOURCE[0]:-$0}")"
if [ ! -f "${_mpl_dir}/run-bounded.sh" ]; then
  echo "model-probe-lib: FATAL: cannot locate run-bounded.sh next to model-probe-lib.sh (looked in '${_mpl_dir}')" >&2
  unset _mpl_dir
  return 1 2>/dev/null || exit 1
fi
# shellcheck source=scripts/gsd/run-bounded.sh
. "${_mpl_dir}/run-bounded.sh"
unset _mpl_dir

CACHE_DIR="${GSD_FALLBACK_CACHE:-$HOME/.cache/gsd-model-probe}"
mkdir -p "$CACHE_DIR"

PROBE_TIMEOUT="${GSD_MODEL_PROBE_TIMEOUT:-120}"

probe_claude_model() {
  # exit 0 = available, 1 = unavailable. 24h cache per model (cache file name).
  local model="$1" cache="$CACHE_DIR/$1.status"
  if [ "${GSD_MODEL_PROBE_FORCE:-0}" != "1" ] && [ -f "$cache" ] \
     && [ -n "$(find "$cache" -mmin -1440 2>/dev/null)" ]; then
    [ "$(cat "$cache")" = "ok" ]; return
  fi
  local cmd="${GSD_MODEL_PROBE_CMD:-}"
  if [ -n "$cmd" ]; then
    if $cmd "$model" >/dev/null 2>&1; then echo ok > "$cache"; else echo fail > "$cache"; fi
  else
    if run_bounded "$PROBE_TIMEOUT" env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN \
        claude -p "ok" --model "$model" --max-turns 1 </dev/null >/dev/null 2>&1; then
      echo ok > "$cache"
    else
      echo fail > "$cache"
    fi
  fi
  [ "$(cat "$cache")" = "ok" ]
}

probe_codex_model() {
  # Separate cache key from probe_claude_model (distinct cache filename per model).
  local model="$1" cache="$CACHE_DIR/$1.status"
  if [ "${GSD_MODEL_PROBE_FORCE:-0}" != "1" ] && [ -f "$cache" ] \
     && [ -n "$(find "$cache" -mmin -1440 2>/dev/null)" ]; then
    [ "$(cat "$cache")" = "ok" ]; return
  fi
  local cmd="${GSD_MODEL_PROBE_CMD_CODEX:-}"
  if [ -n "$cmd" ]; then
    if $cmd "$model" >/dev/null 2>&1; then echo ok > "$cache"; else echo fail > "$cache"; fi
  else
    # Subscription-only: strip BOTH vendors' API keys. The codex CLI prefers
    # OPENAI_API_KEY over the logged-in ChatGPT session when one is present,
    # which silently bills the probe to a metered key instead of the plan.
    if run_bounded "$PROBE_TIMEOUT" env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u OPENAI_API_KEY \
        codex exec -c "model=\"$model\"" -c 'sandbox_mode="read-only"' "ok" </dev/null >/dev/null 2>&1; then
      echo ok > "$cache"
    else
      echo fail > "$cache"
    fi
  fi
  [ "$(cat "$cache")" = "ok" ]
}
