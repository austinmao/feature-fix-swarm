#!/usr/bin/env bash
# Materialize Claude-tier aliases in GSD's generated Codex agent TOMLs.
# GSD 1.6.1 supports Codex natively, but its installer preserves explicit
# `.planning/config.json` aliases (fable/opus/sonnet/haiku) literally. Codex
# cannot run those model IDs. Keep the mapping executable in one place by
# sourcing model-equivalents.sh rather than duplicating the table here.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/model-equivalents.sh"

CODEX_ROOT="${1:-${GSD_CODEX_CONFIG_ROOT:-${CODEX_HOME:-$HOME/.codex}}}"
AGENTS_DIR="$CODEX_ROOT/agents"
MODEL_CONFIG="${GSD_MODEL_CONFIG:-}"
if [ -z "$MODEL_CONFIG" ]; then
  if [ -f "$PWD/.planning/config.json" ]; then
    MODEL_CONFIG="$PWD/.planning/config.json"
  elif [ -f "$SCRIPT_DIR/../../templates/gsd-config.base.json" ]; then
    MODEL_CONFIG="$SCRIPT_DIR/../../templates/gsd-config.base.json"
  fi
fi

if [ ! -d "$AGENTS_DIR" ]; then
  echo "codex-model-sync: no generated agents at $AGENTS_DIR — skipped" >&2
  exit 0
fi

updated=0
rewrite_agent() {
  local file="$1" source_model="$2" mapped effort tmp
  if ! mapped="$(codex_equiv_model "$source_model")"; then
    return 0
  fi
  effort="$(codex_equiv_effort "$source_model")" || return 0

  tmp="$(mktemp "${file}.tmp.XXXXXX")" || exit 1
  if ! awk -v model="$mapped" -v effort="$effort" '
    BEGIN { saw_model = 0; saw_effort = 0; inserted = 0 }
    /^model = / {
      print "model = \"" model "\""
      saw_model = 1
      next
    }
    /^model_reasoning_effort = / {
      print "model_reasoning_effort = \"" effort "\""
      saw_effort = 1
      next
    }
    /^developer_instructions = / && !inserted {
      if (!saw_model) print "model = \"" model "\""
      if (!saw_effort) print "model_reasoning_effort = \"" effort "\""
      inserted = 1
    }
    { print }
    END {
      if (!inserted) {
        if (!saw_model) print "model = \"" model "\""
        if (!saw_effort) print "model_reasoning_effort = \"" effort "\""
      }
    }
  ' "$file" > "$tmp"; then
    rm -f "$tmp"
    exit 1
  fi
  chmod --reference="$file" "$tmp" 2>/dev/null || chmod "$(stat -f '%Lp' "$file" 2>/dev/null || echo 600)" "$tmp" 2>/dev/null || true
  mv "$tmp" "$file"
  updated=$((updated + 1))
}

# A clean global GSD Codex install intentionally omits model lines. Seed the
# explicit FFS agent roles from the project config (or package template) first.
if [ -n "$MODEL_CONFIG" ] && [ -f "$MODEL_CONFIG" ]; then
  while IFS=$'\t' read -r agent source_model; do
    [ -n "$agent" ] && [ -n "$source_model" ] || continue
    file="$AGENTS_DIR/$agent.toml"
    [ -f "$file" ] || continue
    rewrite_agent "$file" "$source_model"
  done < <(python3 - "$MODEL_CONFIG" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    config = json.load(handle)
for agent, model in (config.get("model_overrides") or {}).items():
    if not re.fullmatch(r"gsd-[A-Za-z0-9-]+", str(agent)):
        continue
    if not isinstance(model, str) or "\t" in model or "\n" in model:
        continue
    print(f"{agent}\t{model}")
PY
  )
fi

# Also translate aliases already emitted by a project-local GSD install.
while IFS= read -r -d '' file; do
  current="$(sed -n 's/^model = "\([^"]*\)"$/\1/p' "$file" | head -1)"
  [ -n "$current" ] || continue
  rewrite_agent "$file" "$current"
done < <(find "$AGENTS_DIR" -type f -name 'gsd-*.toml' -print0)

echo "codex-model-sync: materialized $updated agent model pin(s) under $AGENTS_DIR"
