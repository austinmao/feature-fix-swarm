#!/usr/bin/env bash
# Enter managed frontend admission before ambient skill writes or delegation.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "$#" -lt 1 ]; then
  echo "usage: ffs-frontend.sh <feature-spec|fix|code-uplift|feature-implement> [selection options]" >&2
  exit 2
fi
case "$1" in
  feature-spec|fix|code-uplift|feature-implement|task-swarm) ;;
  *) echo "ffs-frontend: unsupported frontend" >&2; exit 2 ;;
esac
_frontend_args=(frontend-start --frontend "$1"
  --invocation-text "${FFS_INVOCATION_TEXT:-}"
  --objective "${FFS_OBJECTIVE:-}"
  --state-root "${FFS_STATE_ROOT:-}"
  --upstream-runtime-manifest "${FFS_UPSTREAM_RUNTIME_MANIFEST:-}"
  --upstream-runtime-sha256 "${FFS_UPSTREAM_RUNTIME_SHA256:-}"
  --request-key "${FFS_REQUEST_KEY:-}"
  --dispatch-limit "${FFS_DISPATCH_LIMIT:-32}"
  --token-limit "${GSD_TOKEN_BUDGET:-250K}")
if [ -n "${FFS_PROCESS_CAPACITY:-}" ]; then
  _frontend_args+=(--worker-capacity "$FFS_PROCESS_CAPACITY")
fi
[ -z "${FFS_PHASE_SCOPE:-}" ] || _frontend_args+=(--scope "$FFS_PHASE_SCOPE")
[ -z "${FFS_ACCEPTANCE_DRAFT:-}" ] || _frontend_args+=(--acceptance-draft "$FFS_ACCEPTANCE_DRAFT")
[ -z "${FFS_REVIEW_MODEL_CATALOG:-}" ] || _frontend_args+=(--review-model-catalog "$FFS_REVIEW_MODEL_CATALOG")
if [ -n "${FFS_HOST_KIND:-}" ]; then
  # A host request is all-or-none; name the missing budget instead of letting
  # the CLI reject an empty token count.
  if [ -z "${FFS_HOST_TOKEN_RESERVATION:-}" ]; then
    echo "ffs-frontend: FFS_HOST_TOKEN_RESERVATION is required with FFS_HOST_KIND (e.g. 100K)" >&2
    exit 2
  fi
  _frontend_args+=(--host "$FFS_HOST_KIND"
    --host-runtime-home "${FFS_HOST_RUNTIME_HOME:-${FFS_CODEX_RUNTIME_HOME:-}}"
    --host-binary "${FFS_HOST_BINARY:-${CODEX_BIN:-}}"
    --host-model-request "${GSD_MODEL_REQUEST:-}"
    --host-sandbox "${GSD_SANDBOX_MODE:-workspace-write}"
    --host-network "${GSD_NETWORK_MODE:-disabled}"
    --host-token-reservation "$FFS_HOST_TOKEN_RESERVATION"
    --host-timeout "${FFS_HOST_TIMEOUT:-600}")
  # Claude only: an all-or-none host request refuses a credential source on Codex.
  [ -z "${FFS_HOST_CREDENTIAL_SOURCE:-}" ] || _frontend_args+=(--host-credential-source "$FFS_HOST_CREDENTIAL_SOURCE")
fi
shift
_selection_args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --select-file|--delete-file|--required-context|--project|--workstream|--session-key)
      if [ "$#" -lt 2 ]; then
        echo "ffs-frontend: selection option requires a value" >&2
        exit 2
      fi
      _selection_args+=("$1" "$2")
      shift 2 ;;
    *) echo "ffs-frontend: unsupported selection option" >&2; exit 2 ;;
  esac
done
[ -z "${GSD_RUN_ID:-}" ] || _frontend_args+=(--run-id "$GSD_RUN_ID")
# Same contract as gsd-run.sh: GSD_RESUME=0 is an explicit fresh start.
case "${GSD_RESUME:-}" in
  ''|0) ;;
  1) _frontend_args+=(--resume) ;;
  *) echo "ffs-frontend: GSD_RESUME must be 0 or 1 when set" >&2; exit 2 ;;
esac
PYTHONPATH="$SCRIPT_DIR/../../lib${PYTHONPATH:+:$PYTHONPATH}" \
  exec python3 -m run_state.cli "${_frontend_args[@]}" "${_selection_args[@]}"
exit 78
