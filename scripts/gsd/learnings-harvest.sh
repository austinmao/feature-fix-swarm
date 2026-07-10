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
#
# SECURITY — prompt-injection surface: the harvested JSONL is REPO-CONTROLLED
# (any file matching .planning/**/learnings*.jsonl) and lands in the GLOBAL
# gbrain, from which future agent runs recall it into prompts. Each entry is
# therefore sanitized before storage: a strict field allowlist (unknown keys
# dropped), every string value capped at 500 chars, and a provenance marker
# ("_trust":"unverified-repo-content" + source path) prefixed onto every entry.
# RESIDUAL RISK: sanitization limits blast radius but does not authenticate
# content — consumers of recalled learnings MUST treat them as UNTRUSTED data,
# never as instructions.
set -uo pipefail

# Allowlist of content-bearing keys kept from a learnings entry (everything
# else is dropped as injection-hardening). `note` is the field the gsd
# extract output uses; the rest are plausible distilled-learning fields.
LEARNINGS_ALLOWED_KEYS="note lesson learning category phase spec tags summary title pattern context evidence severity"

PLANNING_DIR="${1:-.planning}"
ARCHIVE_DIR=".feature-fix-swarm"
ARCHIVE_FILE="$ARCHIVE_DIR/learnings-archive.jsonl"

# gbrain wrapper: shell DATABASE_URL hijacks gbrain's configured engine —
# always scrub it (memory-routing discipline, same as scripts/gsd/mempalace).
gb() { env -u DATABASE_URL gbrain "$@"; }

# sanitize_entry <raw-json-line> <source-path>
# Validates + sanitizes one entry: must be a JSON object; keeps only
# allowlisted keys; caps every string at 500 chars; prefixes provenance
# markers. Prints the sanitized single-line JSON on success, nothing (rc=1)
# for a non-object or invalid line. Never mutates the input file.
sanitize_entry() {
  ALLOWED="$LEARNINGS_ALLOWED_KEYS" python3 - "$1" "$2" <<'PY'
import json, os, sys

ALLOWED = set(os.environ.get("ALLOWED", "").split())
CAP = 500
raw, src = sys.argv[1], sys.argv[2]

try:
    obj = json.loads(raw)
except Exception:
    sys.exit(1)
if not isinstance(obj, dict):
    sys.exit(1)

def cap(s):
    return s[:CAP]

out = {}
for k, v in obj.items():
    if k not in ALLOWED:
        continue
    if isinstance(v, str):
        out[k] = cap(v)
    elif isinstance(v, bool) or v is None or isinstance(v, (int, float)):
        out[k] = v
    elif isinstance(v, list):
        clean = []
        for el in v[:50]:
            if isinstance(el, str):
                clean.append(cap(el))
            elif isinstance(el, bool) or el is None or isinstance(el, (int, float)):
                clean.append(el)
        out[k] = clean
    # dicts / deeper nesting dropped — unexpected shape is an injection risk.

# provenance: recalled memory is UNTRUSTED repo content.
out["_provenance"] = cap(str(src))
out["_trust"] = "unverified-repo-content"
sys.stdout.write(json.dumps(out))
PY
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
  # A symlinked archive dir/file is an attacker redirect (write-through to an
  # arbitrary path) — refuse to follow it. Skip the archive, still exit 0.
  if [ -L "$ARCHIVE_DIR" ] || [ -L "$ARCHIVE_FILE" ]; then
    echo "[learnings-harvest] WARN: archive path or its parent is a symlink — skipping archive" >&2
    return 0
  fi
  mkdir -p "$ARCHIVE_DIR"
  if command -v flock >/dev/null 2>&1; then
    ( flock -x 9; cat "$1" >> "$ARCHIVE_FILE"; ) 9>>"$ARCHIVE_FILE"
  else
    cat "$1" >> "$ARCHIVE_FILE"
  fi
}

write_gbrain() {
  # $1 = file of valid entries, one JSON object per line
  #
  # ALL-OR-NOTHING archive fallback (by design): this loop puts per-entry, but
  # returns a single nonzero if ANY put fails. The caller then re-archives the
  # WHOLE valid set — so entries that DID reach gbrain are also written to the
  # archive, i.e. duplicated. This is acceptable for advisory memory: learnings
  # are idempotent hints, dedup is a recall-time concern, and per-entry failure
  # bookkeeping isn't worth the complexity for a fail-soft harvester.
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
    # sanitize_entry passes "$f" to python3 as a provenance STRING only — it
    # never opens the file — so the read-and-write-same-file heuristic is a
    # false positive here.
    # shellcheck disable=SC2094
    while IFS= read -r line || [ -n "$line" ]; do
      [ -z "$line" ] && continue
      sanitized="$(sanitize_entry "$line" "$f")"
      if [ -n "$sanitized" ]; then
        printf '%s\n' "$sanitized" >> "$VALID_TMP"
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
