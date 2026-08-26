#!/usr/bin/env bash
# Deterministic, data-only takeover wall.  Persisted strings are never eval'd.
set -euo pipefail

SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GP="$SCRIPT_ROOT/lib/gates.py"
[ -f "$GP" ] || { echo "TAKEOVER-REFUSED:gates-unavailable"; exit 1; }
RUN_ID="${GSD_RUN_ID:-}"
MODE=text
LIST=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --run-id) RUN_ID="${2:-}"; shift 2 ;;
    --json) MODE=json; shift ;;
    --list) LIST=1; shift ;;
    *) echo "usage: takeover-check.sh --run-id spec-NNN [--json] | --list" >&2; exit 2 ;;
  esac
done
if [ "$LIST" -eq 0 ] && [[ ! "$RUN_ID" =~ ^spec-[0-9]{3}$ ]]; then
  echo "invalid run id" >&2; exit 2
fi

CANON_STORE="$(env -u GATES_STORE python3 "$GP" store-path)"
if [ -n "${GATES_STORE:-}" ] && [ "$(python3 "$GP" store-path)" != "$CANON_STORE" ]; then
  echo "TAKEOVER-REFUSED:env-mismatch"; exit 1
fi
STORE_DIR="$(env -u GATES_STORE python3 "$GP" store-dir)"
DIR="$STORE_DIR/takeover"
if [ -L "$DIR" ]; then echo "TAKEOVER-REFUSED:unsafe-directory"; exit 1; fi

emit() {
  local verdict="$1" reason="${2:-}"
  if [ "$MODE" = json ]; then
    python3 - "$RUN_ID" "$verdict" "$reason" <<'PY'
import json, sys
print(json.dumps({"schema_version": 1, "run_id": sys.argv[1], "verdict": sys.argv[2],
                  "reason": sys.argv[3] or None, "remedy": None if sys.argv[2] == "TAKEOVER-OK" else "re-run /spec-status"}, separators=(",", ":")))
PY
  else
    [ -n "$reason" ] && printf '%s:%s\n' "$verdict" "$reason" || printf '%s\n' "$verdict"
  fi
}

if [ "$LIST" -eq 1 ]; then
  [ -d "$DIR" ] || exit 0
  [ -L "$DIR" ] && { echo "TAKEOVER-REFUSED:unsafe-directory"; exit 1; }
  for path in "$DIR"/*.json; do
    [ -f "$path" ] && [ ! -L "$path" ] || continue
    python3 - "$path" <<'PY'
import json, sys, time
try:
    d=json.load(open(sys.argv[1])); print("%s\t%s\t%s\t%s" % (d["ids"]["run_id"], int(time.time()-d["created_at"]), d["git_state"].get("branch", ""), d.get("resume",{}).get("command", "").replace("\n", " ").replace("\r", " ")))
except (OSError, ValueError, KeyError, TypeError): pass
PY
  done | sort
  exit 0
fi

RECORD="$DIR/$RUN_ID.json"
if [ ! -e "$RECORD" ]; then
  STATE="$(env -u GATES_STORE python3 "$GP" takeover-state "$RUN_ID")"
  if python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("takeover_expected") else 1)' <<<"$STATE"; then
    emit TAKEOVER-REFUSED missing-record; exit 1
  fi
  emit TAKEOVER-NONE; exit 0
fi
[ ! -L "$RECORD" ] && [ -f "$RECORD" ] || { emit TAKEOVER-REFUSED unsafe-record; exit 1; }
if ! python3 - "$RECORD" "$CANON_STORE" <<'PY'
import hashlib, json, sys
try:
    d=json.load(open(sys.argv[1])); expected=sys.argv[2]
    ok=(d["ids"]["run_id"].startswith("spec-") and d["gates_store"] == expected and d["gates_store_anchor"] == hashlib.sha256(expected.encode()).hexdigest())
except (OSError, ValueError, KeyError, TypeError): ok=False
raise SystemExit(0 if ok else 1)
PY
then emit TAKEOVER-REFUSED decoy-store; exit 1; fi
mv "$RECORD" "$DIR/$RUN_ID.consumed.$(date +%s).json"
emit TAKEOVER-OK
