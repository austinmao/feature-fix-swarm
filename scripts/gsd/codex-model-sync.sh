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
native_codex_pin() {
  case "$1" in
    gpt-5.6-sol) printf '%s\t%s\n' gpt-5.6-sol high ;;
    gpt-5.6-terra) printf '%s\t%s\n' gpt-5.6-terra medium ;;
    gpt-5.6-luna) printf '%s\t%s\n' gpt-5.6-luna low ;;
    *) return 1 ;;
  esac
}

native_codex_effort_is_valid() {
  case "$1:$2" in
    gpt-5.6-sol:high|gpt-5.6-sol:xhigh|gpt-5.6-terra:medium|gpt-5.6-terra:high|gpt-5.6-luna:low|gpt-5.6-luna:medium) return 0 ;;
    *) return 1 ;;
  esac
}

rewrite_agent() {
  local file="$1" source_model="$2" native_mode="${3:-preserve-native-effort}" mapped effort pin existing_effort tmp
  if mapped="$(codex_equiv_model "$source_model")"; then
    effort="$(codex_equiv_effort "$source_model")" || return 0
  elif pin="$(native_codex_pin "$source_model")"; then
    IFS=$'\t' read -r mapped effort <<EOF
$pin
EOF
    # A generated agent may already carry an allowed native effort (notably
    # the fable alias's intentional Sol/xhigh distinction). Preserve that
    # pin on the ordinary existing-TOML pass. An explicit config override is
    # the authoritative request and resets to its canonical native effort.
    if [ "$native_mode" = preserve-native-effort ]; then
      existing_effort="$(sed -n 's/^model_reasoning_effort = "\([^"]*\)"$/\1/p' "$file" | head -1)"
      if native_codex_effort_is_valid "$source_model" "$existing_effort"; then
        effort="$existing_effort"
      fi
    fi
  else
    return 0
  fi

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
  if cmp -s "$file" "$tmp"; then
    rm -f "$tmp"
    return 0
  fi
  chmod --reference="$file" "$tmp" 2>/dev/null || chmod "$(stat -f '%Lp' "$file" 2>/dev/null || echo 600)" "$tmp" 2>/dev/null || true
  mv "$tmp" "$file"
  updated=$((updated + 1))
}

# Translate models already emitted by a project-local GSD install before
# applying explicit role overrides. This lets known native Codex IDs acquire a
# canonical effort, while a configured fable alias retains its deliberate
# Sol/xhigh distinction instead of being rewritten to the native Sol/high pin.
while IFS= read -r -d '' file; do
  current="$(sed -n 's/^model = "\([^"]*\)"$/\1/p' "$file" | head -1)"
  [ -n "$current" ] || continue
  rewrite_agent "$file" "$current"
done < <(find "$AGENTS_DIR" -type f -name 'gsd-*.toml' -print0)

# A clean global GSD Codex install intentionally omits model lines. Apply the
# explicit FFS agent roles from the project config (or package template) last:
# source aliases (notably fable -> Sol/xhigh) remain the authoritative pin.
if [ -n "$MODEL_CONFIG" ] && [ -f "$MODEL_CONFIG" ]; then
  while IFS=$'\t' read -r agent source_model; do
    [ -n "$agent" ] && [ -n "$source_model" ] || continue
    file="$AGENTS_DIR/$agent.toml"
    [ -f "$file" ] || continue
    rewrite_agent "$file" "$source_model" force-native-effort
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

echo "codex-model-sync: materialized $updated agent model pin(s) under $AGENTS_DIR"
