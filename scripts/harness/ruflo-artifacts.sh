#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ruflo-artifacts.sh init --run-dir DIR --spec-id NNN [--swarm-id ID] [--host HOST] [--capabilities-file FILE]
  ruflo-artifacts.sh agent-log --run-dir DIR --agent-id ID --log-file FILE
  ruflo-artifacts.sh task-event --run-dir DIR --task-id T001 --status STATUS [--agent-id ID] [--host HOST] [--model MODEL] [--prompt-file FILE]

Creates stable Ruflo coordination artifacts for feature-fix-swarm. MCP calls are made by
the skill runtime; this script only writes the evidence files.
EOF
}

validate_agent_id() {
  local agent_id="$1"

  # codex-gate: D2 - agent-log IDs become filenames, so accept safe slugs only.
  if [[ ! "$agent_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ || "$agent_id" == *..* ]]; then
    echo "ERROR: --agent-id must be a non-empty safe slug with no '/', '..', or path characters: $agent_id" >&2
    return 2
  fi
}

cmd="${1:-}"
[ -n "$cmd" ] || { usage >&2; exit 2; }
shift

case "$cmd" in
  init)
    RUN_DIR=""
    SPEC_ID=""
    SWARM_ID=""
    HOST=""
    CAPABILITIES_FILE=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --run-dir) RUN_DIR="$2"; shift 2 ;;
        --spec-id) SPEC_ID="$2"; shift 2 ;;
        --swarm-id) SWARM_ID="$2"; shift 2 ;;
        --host) HOST="$2"; shift 2 ;;
        --capabilities-file) CAPABILITIES_FILE="$2"; shift 2 ;;
        *) echo "Unknown arg: $1" >&2; usage >&2; exit 2 ;;
      esac
    done
    [ -n "$RUN_DIR" ] || { echo "ERROR: --run-dir required" >&2; exit 2; }
    [ -n "$SPEC_ID" ] || { echo "ERROR: --spec-id required" >&2; exit 2; }
    mkdir -p "$RUN_DIR/ruflo/agents" "$RUN_DIR/ruflo/tasks"
    CAPABILITIES_JSON="{}"
    if [ -n "$CAPABILITIES_FILE" ] && [ -f "$CAPABILITIES_FILE" ]; then
      CAPABILITIES_JSON="$(cat "$CAPABILITIES_FILE")"
    fi
    python3 - "$RUN_DIR/ruflo/manifest.json" "$SPEC_ID" "$SWARM_ID" "$HOST" "$CAPABILITIES_JSON" <<'PY'
import json, sys, datetime
path, spec, swarm, host, caps = sys.argv[1:6]
try:
    capabilities = json.loads(caps)
except Exception:
    capabilities = {"raw": caps}
data = {
    "created_at": datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "spec_id": spec,
    "swarm_id": swarm or None,
    "host_executor": host or None,
    "capabilities": capabilities,
    "agents": [],
    "tasks": [],
}
with open(path, "w") as f:
    json.dump(data, f, indent=2, sort_keys=True)
    f.write("\n")
PY
    echo "$RUN_DIR/ruflo/manifest.json"
    ;;

  agent-log)
    RUN_DIR=""
    AGENT_ID=""
    LOG_FILE=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --run-dir) RUN_DIR="$2"; shift 2 ;;
        --agent-id) AGENT_ID="$2"; shift 2 ;;
        --log-file) LOG_FILE="$2"; shift 2 ;;
        *) echo "Unknown arg: $1" >&2; usage >&2; exit 2 ;;
      esac
    done
    [ -n "$RUN_DIR" ] || { echo "ERROR: --run-dir required" >&2; exit 2; }
    [ -n "$AGENT_ID" ] || { echo "ERROR: --agent-id required" >&2; exit 2; }
    [ -n "$LOG_FILE" ] || { echo "ERROR: --log-file required" >&2; exit 2; }
    validate_agent_id "$AGENT_ID"
    mkdir -p "$RUN_DIR/ruflo/agents"
    cp "$LOG_FILE" "$RUN_DIR/ruflo/agents/$AGENT_ID.json"
    echo "$RUN_DIR/ruflo/agents/$AGENT_ID.json"
    ;;

  task-event)
    RUN_DIR=""
    TASK_ID=""
    STATUS=""
    AGENT_ID=""
    HOST=""
    MODEL=""
    PROMPT_FILE=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --run-dir) RUN_DIR="$2"; shift 2 ;;
        --task-id) TASK_ID="$2"; shift 2 ;;
        --status) STATUS="$2"; shift 2 ;;
        --agent-id) AGENT_ID="$2"; shift 2 ;;
        --host) HOST="$2"; shift 2 ;;
        --model) MODEL="$2"; shift 2 ;;
        --prompt-file) PROMPT_FILE="$2"; shift 2 ;;
        *) echo "Unknown arg: $1" >&2; usage >&2; exit 2 ;;
      esac
    done
    [ -n "$RUN_DIR" ] || { echo "ERROR: --run-dir required" >&2; exit 2; }
    [ -n "$TASK_ID" ] || { echo "ERROR: --task-id required" >&2; exit 2; }
    [ -n "$STATUS" ] || { echo "ERROR: --status required" >&2; exit 2; }
    mkdir -p "$RUN_DIR/ruflo/tasks"
    python3 - "$RUN_DIR/ruflo/tasks/events.jsonl" "$TASK_ID" "$STATUS" "$AGENT_ID" "$HOST" "$MODEL" "$PROMPT_FILE" <<'PY'
import json, sys, datetime
path, task, status, agent, host, model, prompt = sys.argv[1:8]
event = {
    "timestamp": datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "task_id": task,
    "status": status,
    "agent_id": agent or None,
    "host_executor": host or None,
    "model": model or None,
    "prompt_file": prompt or None,
}
with open(path, "a") as f:
    f.write(json.dumps(event, sort_keys=True) + "\n")
PY
    echo "$RUN_DIR/ruflo/tasks/events.jsonl"
    ;;

  -h|--help)
    usage
    ;;

  *)
    echo "Unknown command: $cmd" >&2
    usage >&2
    exit 2
    ;;
esac
