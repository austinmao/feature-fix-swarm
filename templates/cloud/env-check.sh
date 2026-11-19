#!/usr/bin/env bash
# Prints EXACTLY one JSON object: {"tools":{},"present":[NAMES],"missing":[NAMES]}.
# That is the manifest `env_report` shape and nothing else: the dispatcher's
# manifest validator rejects any extra key. The setup status lives behind a
# separate flag: `env-check.sh <provider> --status` prints the setup status
# JSON (or null) instead.
# Usage: env-check.sh <claude|codex> [--status]. Names only: a value is never
# printed. bash 3.2 safe.
set -u

PROVIDER="${1:-}"
case "$PROVIDER" in
  claude | codex) ;;
  *) printf 'usage: env-check.sh <claude|codex> [--status]\n' >&2; exit 2 ;;
esac
MODE="${2:-}"
case "$MODE" in
  "" | --status) ;;
  *) printf 'usage: env-check.sh <claude|codex> [--status]\n' >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
CONFIG="$SCRIPT_DIR/config.json"
STATUS_FILE="$HOME/.ffs-cloud-env-status"

# Valid env-var names only, host: entries skipped, one per line.
names() {
  command -v python3 >/dev/null 2>&1 && [ -f "$CONFIG" ] || return 0
  python3 -c 'import json,re,sys
try:
    c = json.load(open(sys.argv[1]))
    for n in c["secrets"]["exposure"].get(sys.argv[2], []):
        if isinstance(n, str) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", n):
            print(n)
except Exception:
    pass' "$CONFIG" "$PROVIDER" 2>/dev/null
}

# One version token that fits the manifest validator ([A-Za-z0-9][A-Za-z0-9._+~:-]{0,63}),
# never a banner: "git version 2.50.1 (Apple Git-155)" -> 2.50.1; "unknown" if none.
tool_version() {
  local t="$1" v=""
  command -v "$t" >/dev/null 2>&1 || return 1
  case "$t" in
    shellcheck) v="$("$t" --version 2>/dev/null | sed -n 's/^version: //p' | head -n 1)" ;;
    *) v="$("$t" --version 2>/dev/null | head -n 1)" ;;
  esac
  v="$(printf '%s\n' "$v" | grep -Eo '[0-9]+(\.[0-9A-Za-z_+~-]+)+' | head -n 1 | cut -c1-64)"
  printf '%s' "${v:-unknown}"
}

if [ "$MODE" = "--status" ]; then
  setup=null
  if [ -f "$STATUS_FILE" ]; then
    first="$(head -c 1 "$STATUS_FILE" 2>/dev/null)"
    if [ "$first" = "{" ]; then setup="$(tr -d '\n' <"$STATUS_FILE")"; fi
  fi
  printf '%s\n' "$setup"
  exit 0
fi

tools="" present="" missing=""
for t in git node npm python3 jq bats shellcheck doppler; do
  v="$(tool_version "$t")" || continue
  if [ -n "$tools" ]; then tools="$tools,"; fi
  tools="$tools\"$t\":\"$v\""
done

while IFS= read -r name; do
  [ -n "$name" ] || continue
  # Presence only: the expansion is tested, never printed.
  if [ -n "${!name:-}" ]; then
    if [ -n "$present" ]; then present="$present,"; fi
    present="$present\"$name\""
  else
    if [ -n "$missing" ]; then missing="$missing,"; fi
    missing="$missing\"$name\""
  fi
done <<EOF2
$(names)
EOF2

printf '{"tools":{%s},"present":[%s],"missing":[%s]}\n' "$tools" "$present" "$missing"
exit 0
