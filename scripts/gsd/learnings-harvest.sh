#!/usr/bin/env bash
# learnings-harvest.sh — fail-soft learnings harvester (gbrain-or-archive)
#
# Reads a finished gsd run's .planning/**/learnings*.jsonl, writes each valid
# entry to gbrain when the backend is healthy (mirrors scripts/gsd/mempalace's
# `gb put` + `gb sync` shape), else appends the entries atomically to
# .feature-fix-swarm/learnings-archive.jsonl. Always exits 0 and prints
# "<N> harvested" — the learnings step must never block ship (AC-003).
#
# Usage: learnings-harvest.sh [<planning-dir>]   (default: .planning)
#
# Fail-soft + never silent (same convention as security-model-fence.sh):
# WARN on stderr, exit 0 — a broken/absent/unreachable backend never blocks
# a run.
set -uo pipefail

PLANNING_DIR="${1:-.planning}"
ARCHIVE_DIR=".feature-fix-swarm"
ARCHIVE_FILE="$ARCHIVE_DIR/learnings-archive.jsonl"

# gbrain wrapper: shell DATABASE_URL hijacks gbrain's configured engine —
# always scrub it (memory-routing discipline, same as scripts/gsd/mempalace).
gb() { env -u DATABASE_URL gbrain "$@"; }

is_valid_json() {
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$1" | jq -e . >/dev/null 2>&1
  else
    printf '%s' "$1" | python3 -c 'import json,sys; json.loads(sys.stdin.read())' >/dev/null 2>&1
  fi
}

gbrain_healthy() {
  command -v gbrain >/dev/null 2>&1 && gb doctor >/dev/null 2>&1
}

hash_of() {
  if command -v shasum >/dev/null 2>&1; then
    printf '%s' "$1" | shasum -a 256 2>/dev/null | cut -c1-12
  else
    printf '%s' "$1" | cksum | cut -d' ' -f1
  fi
}

append_archive() {
  # $1 = file of valid entries, one JSON object per line
  mkdir -p "$ARCHIVE_DIR"
  if command -v flock >/dev/null 2>&1; then
    ( flock -x 9; cat "$1" >> "$ARCHIVE_FILE"; ) 9>>"$ARCHIVE_FILE"
  else
    cat "$1" >> "$ARCHIVE_FILE"
  fi
}

write_gbrain() {
  # $1 = file of valid entries, one JSON object per line
  local entry hash ok=0
  while IFS= read -r entry || [ -n "$entry" ]; do
    [ -z "$entry" ] && continue
    hash="$(hash_of "$entry")"
    gb put "learnings/$hash" "$entry" >/dev/null 2>&1 || ok=1
  done < "$1"
  gb sync --no-pull --no-embed >/dev/null 2>&1 || true
  return "$ok"
}

VALID_TMP="$(mktemp)"
trap 'rm -f "$VALID_TMP"' EXIT

VALID_COUNT=0
MALFORMED_COUNT=0

if [ -d "$PLANNING_DIR" ]; then
  while IFS= read -r f; do
    [ -f "$f" ] || continue
    while IFS= read -r line || [ -n "$line" ]; do
      [ -z "$line" ] && continue
      if is_valid_json "$line"; then
        printf '%s\n' "$line" >> "$VALID_TMP"
        VALID_COUNT=$((VALID_COUNT + 1))
      else
        MALFORMED_COUNT=$((MALFORMED_COUNT + 1))
      fi
    done < "$f"
  done < <(find "$PLANNING_DIR" -name 'learnings*.jsonl' 2>/dev/null)
fi

if [ "$MALFORMED_COUNT" -gt 0 ]; then
  echo "[learnings-harvest] WARN: skipped $MALFORMED_COUNT malformed line(s)" >&2
fi

if [ "$VALID_COUNT" -eq 0 ]; then
  echo "0 harvested"
  exit 0
fi

if gbrain_healthy; then
  if ! write_gbrain "$VALID_TMP"; then
    echo "[learnings-harvest] WARN: gbrain write failed — falling back to archive" >&2
    append_archive "$VALID_TMP"
  fi
else
  echo "[learnings-harvest] WARN: gbrain unavailable/unreachable — using archive fallback" >&2
  append_archive "$VALID_TMP"
fi

echo "$VALID_COUNT harvested"
exit 0
