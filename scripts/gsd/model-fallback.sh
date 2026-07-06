#!/usr/bin/env bash
# model-fallback.sh — substitute unavailable premium models in .planning/config.json
#
# gsd-core's model_overrides are returned verbatim (model-resolver.cjs step 1) —
# no availability check. When a pinned model (claude-fable-5) drops off the OAuth
# subscription, every spawn of that agent errors. This lever probes availability
# once (cached 24h) and rewrites model_overrides + dynamic_routing.tier_models to
# the fallback BEFORE a run starts.
#
# Usage: model-fallback.sh [<planning-dir>]   (default .planning)
#   GSD_MODEL_PROBE_CMD  override probe command (tests)
#   GSD_FALLBACK_CACHE   override cache dir (tests; default ~/.cache/gsd-model-probe)
#
# Fallback chain (edit here when the catalog changes):
#   claude-fable-5 -> claude-opus-4-8
#
# Fail-soft: if the probe MECHANISM breaks, config stays unchanged and we warn —
# an unavailable model then fails loudly at first spawn, same as without this lever.
set -euo pipefail

PLANNING_DIR="${1:-.planning}"
CONFIG="$PLANNING_DIR/config.json"
[ -f "$CONFIG" ] || { echo "[model-fallback] ERROR: $CONFIG not found" >&2; exit 1; }

CACHE_DIR="${GSD_FALLBACK_CACHE:-$HOME/.cache/gsd-model-probe}"
mkdir -p "$CACHE_DIR"

# chain: "candidate fallback" pairs
CHAIN="claude-fable-5 claude-opus-4-8"

probe_model() {
  # exit 0 = available, 1 = unavailable. 24h cache per model.
  local model="$1" cache="$CACHE_DIR/$1.status"
  if [ -f "$cache" ] && [ -n "$(find "$cache" -mmin -1440 2>/dev/null)" ]; then
    [ "$(cat "$cache")" = "ok" ]; return
  fi
  local cmd="${GSD_MODEL_PROBE_CMD:-}"
  if [ -n "$cmd" ]; then
    if $cmd "$model" >/dev/null 2>&1; then echo ok > "$cache"; else echo fail > "$cache"; fi
  else
    if env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN \
        claude -p "ok" --model "$model" --max-turns 1 >/dev/null 2>&1; then
      echo ok > "$cache"
    else
      echo fail > "$cache"
    fi
  fi
  [ "$(cat "$cache")" = "ok" ]
}

# shellcheck disable=SC2086  # intentional word-split: CHAIN is a space-separated model list
set -- $CHAIN
while [ "$#" -ge 2 ]; do
  CANDIDATE="$1"; FALLBACK="$2"; shift 2
  grep -q "\"$CANDIDATE\"" "$CONFIG" || continue
  if probe_model "$CANDIDATE"; then
    echo "[model-fallback] $CANDIDATE available — config unchanged"
    continue
  fi
  CANDIDATE="$CANDIDATE" FALLBACK="$FALLBACK" CONFIG="$CONFIG" python3 - <<'EOF'
import json, os
path = os.environ["CONFIG"]; cand = os.environ["CANDIDATE"]; fb = os.environ["FALLBACK"]
cfg = json.load(open(path))
n = 0
def sub(d):
    global n
    for k, v in d.items():
        if isinstance(v, dict): sub(v)
        elif v == cand: d[k] = fb; n += 1
sub(cfg)
json.dump(cfg, open(path, "w"), indent=2)
print(f"[model-fallback] {cand} UNAVAILABLE -> {fb} ({n} override(s) rewritten)")
EOF
done
