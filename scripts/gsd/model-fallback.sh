#!/usr/bin/env bash
# model-fallback.sh — 3-leg cross-vendor fallback for unavailable premium
# models in .planning/config.json, WITH recovery.
#
# gsd-core's model_overrides are returned verbatim (model-resolver.cjs step 1) —
# no availability check. When claude-fable-5 drops off the OAuth subscription,
# every spawn of that agent errors. This lever probes availability once
# (cached 24h) and rewrites model_overrides + dynamic_routing.tier_models
# BEFORE a run starts — then RESTORES them once fable comes back.
#
# Chain: fable -> [probe gpt-5.6-sol cross-vendor compensation, for
# marker-mode only] -> opus. Codex models can NEVER be Claude subagent pins,
# so the actual config rewrite is always fable->opus; the codex-sol probe
# only decides whether we record mode=codex-sol (cross-vendor xhigh
# compensation available, e.g. via plan-adversary.sh) or mode=opus-only.
#
# Both value forms are matched and the substitution PRESERVES form — gsd-core
# aliases ("fable" -> "opus", the form templates/gsd-config.base.json and real
# consumer configs pin) and full IDs ("claude-fable-5" -> "claude-opus-4-8").
#
# Recovery: once fable is available again, restore ONLY the JSON paths this
# lever itself rewrote — the marker (.planning/fable-fallback.json) records
# each path's exact prior value: {"mode": ..., "paths": {"<json.path>":
# "<original value>"}}, so mixed-form configs restore per-path. A blanket
# opus->fable substitution would incorrectly flip intentional opus pins
# (gsd-verifier etc.) that were never fable to begin with — this is the
# correctness crux, see tests/bats/model-fallback.bats.
#
# Usage: model-fallback.sh [<planning-dir>]   (default .planning)
#   GSD_MODEL_PROBE_CMD        override the claude probe command (tests)
#   GSD_MODEL_PROBE_CMD_CODEX  override the codex-sol probe command (tests)
#   GSD_FALLBACK_CACHE         override cache dir (tests; default ~/.cache/gsd-model-probe)
#
# Fail-soft: if the probe MECHANISM breaks, config stays unchanged and we warn —
# an unavailable model then fails loudly at first spawn, same as without this lever.
set -euo pipefail

PLANNING_DIR="${1:-.planning}"
CONFIG="$PLANNING_DIR/config.json"
[ -f "$CONFIG" ] || { echo "[model-fallback] ERROR: $CONFIG not found" >&2; exit 1; }

CACHE_DIR="${GSD_FALLBACK_CACHE:-$HOME/.cache/gsd-model-probe}"
mkdir -p "$CACHE_DIR"

# Wall-clock bound for the real-CLI probes. This lever runs as a WALL at the
# top of /feature-implement — an unbounded probe hang stalls the run before
# phase 1 (2026-07-12 dead-codex forensics: this was the only site with no
# timeout on ANY branch). A bounded/refused probe reads as "unavailable",
# which is the lever's existing fail-soft direction.
. "$(dirname "${BASH_SOURCE[0]}")/run-bounded.sh"
PROBE_TIMEOUT="${GSD_MODEL_PROBE_TIMEOUT:-120}"

MARKER="$PLANNING_DIR/fable-fallback.json"
FABLE="claude-fable-5"
OPUS="claude-opus-4-8"
CODEX_SOL="gpt-5.6-sol"

probe_claude_model() {
  # exit 0 = available, 1 = unavailable. 24h cache per model (cache file name).
  local model="$1" cache="$CACHE_DIR/$1.status"
  if [ -f "$cache" ] && [ -n "$(find "$cache" -mmin -1440 2>/dev/null)" ]; then
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
  if [ -f "$cache" ] && [ -n "$(find "$cache" -mmin -1440 2>/dev/null)" ]; then
    [ "$(cat "$cache")" = "ok" ]; return
  fi
  local cmd="${GSD_MODEL_PROBE_CMD_CODEX:-}"
  if [ -n "$cmd" ]; then
    if $cmd "$model" >/dev/null 2>&1; then echo ok > "$cache"; else echo fail > "$cache"; fi
  else
    if run_bounded "$PROBE_TIMEOUT" env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN \
        codex exec -c "model=\"$model\"" -c 'sandbox_mode="read-only"' "ok" </dev/null >/dev/null 2>&1; then
      echo ok > "$cache"
    else
      echo fail > "$cache"
    fi
  fi
  [ "$(cat "$cache")" = "ok" ]
}

# Match BOTH value forms: gsd-core alias "fable" and full ID "claude-fable-5"
# (the alias grep is exact-quoted, so it does NOT match inside the full ID).
HAS_FABLE_LITERAL=false
if grep -qE '"(fable|claude-fable-5)"' "$CONFIG"; then HAS_FABLE_LITERAL=true; fi

if [ ! -f "$MARKER" ] && [ "$HAS_FABLE_LITERAL" = false ]; then
  echo "[model-fallback] $FABLE not present in config — no-op"
  exit 0
fi

if probe_claude_model "$FABLE"; then
  if [ -f "$MARKER" ]; then
    RESULT="$(MARKER="$MARKER" CONFIG="$CONFIG" python3 - <<'EOF'
import json, os
marker_path = os.environ["MARKER"]; config_path = os.environ["CONFIG"]
marker = json.load(open(marker_path))
cfg = json.load(open(config_path))
# paths: {"<json.path>": "<exact original value>"} — form-preserving restore
for path, original in marker["paths"].items():
    keys = path.split(".")
    d = cfg
    for k in keys[:-1]:
        d = d[k]
    d[keys[-1]] = original
json.dump(cfg, open(config_path, "w"), indent=2)
os.remove(marker_path)
print(f"restored {len(marker['paths'])} path(s) — marker deleted")
EOF
)"
    echo "[model-fallback] $FABLE available — $RESULT"
  else
    echo "[model-fallback] $FABLE available — config unchanged"
  fi
  exit 0
fi

# fable unavailable
if [ "$HAS_FABLE_LITERAL" = false ]; then
  echo "[model-fallback] $FABLE unavailable — already on fallback (marker present), nothing to rewrite"
  exit 0
fi

if probe_codex_model "$CODEX_SOL"; then
  MODE="codex-sol"
else
  MODE="opus-only"
fi

RESULT="$(FABLE="$FABLE" OPUS="$OPUS" CONFIG="$CONFIG" MARKER="$MARKER" MODE="$MODE" python3 - <<'EOF'
import json, os
config_path = os.environ["CONFIG"]; fable = os.environ["FABLE"]; opus = os.environ["OPUS"]
marker_path = os.environ["MARKER"]; mode = os.environ["MODE"]
cfg = json.load(open(config_path))
# Form-preserving substitution: alias stays alias, full ID stays full ID.
forms = {"fable": "opus", fable: opus}
paths = {}
def sub(d, prefix=""):
    for k, v in d.items():
        p = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            sub(v, p)
        elif v in forms:
            paths[p] = v
            d[k] = forms[v]
sub(cfg)
json.dump(cfg, open(config_path, "w"), indent=2)
marker = {"mode": mode, "paths": paths}
json.dump(marker, open(marker_path, "w"), indent=2)
print(f"{fable} UNAVAILABLE -> {opus} ({len(paths)} override(s) rewritten)")
EOF
)"
echo "[model-fallback] $RESULT"
echo "[model-fallback] fallback mode: $MODE (marker: $MARKER)"
