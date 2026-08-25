#!/usr/bin/env bash
# Install the external prompt-master skill from one audited commit plus the
# small FFS compatibility patch.  Ownership/backups are handled by setup.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PIN_FILE="$ROOT/vendor/prompt-master/pin.json"
PATCH_FILE="$ROOT/vendor/prompt-master/codex-gpt56.patch"
DEST=""
SOURCE=""
usage() {
  echo "usage: install-prompt-master.sh --dest <path> [--source <url>]" >&2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dest) [ "$#" -ge 2 ] || { usage; exit 2; }; DEST="$2"; shift 2 ;;
    --source) [ "$#" -ge 2 ] || { usage; exit 2; }; SOURCE="$2"; shift 2 ;;
    *) echo "prompt-master installer: unknown argument $1" >&2; exit 2 ;;
  esac
done

[ -n "$DEST" ] || { echo "prompt-master installer: --dest is required" >&2; exit 2; }
DEST="$(python3 - "$DEST" <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[1]).expanduser().absolute())
PY
)"
case "$DEST" in /|"$HOME"|"$HOME"/|.) echo "prompt-master installer: unsafe destination" >&2; exit 2 ;; esac

read_pin() {
  python3 - "$PIN_FILE" "$1" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))[sys.argv[2]])
PY
}

REPOSITORY="$(read_pin repository)"
PIN="$(read_pin commit)"
[ -n "$SOURCE" ] || SOURCE="$REPOSITORY"

if [ -e "$DEST" ] || [ -L "$DEST" ]; then
  echo "prompt-master installer: destination exists; setup.sh must verify ownership before replacement: $DEST" >&2
  exit 1
fi

PARENT="$(dirname "$DEST")"
mkdir -p "$PARENT"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ffs-prompt-master.XXXXXX")"
STAGE="$TMP_DIR/stage"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

git clone --quiet "$SOURCE" "$TMP_DIR/repo"
git -C "$TMP_DIR/repo" checkout --quiet "$PIN"
git -C "$TMP_DIR/repo" apply --check "$PATCH_FILE"
git -C "$TMP_DIR/repo" apply "$PATCH_FILE"
mkdir -p "$STAGE"
cp -R "$TMP_DIR/repo/." "$STAGE/"
rm -rf "$STAGE/.git"
PATCH_SHA="$(shasum -a 256 "$PATCH_FILE" | awk '{print $1}')"
python3 - "$STAGE/.ffs-prompt-master.json" "$REPOSITORY" "$PIN" "$PATCH_SHA" <<'PY'
import json, pathlib, sys
pathlib.Path(sys.argv[1]).write_text(json.dumps({
    "schema": "ffs.external-skill/v1",
    "repository": sys.argv[2],
    "commit": sys.argv[3],
    "patch_sha256": sys.argv[4],
}, indent=2) + "\n")
PY

[ ! -e "$DEST" ] && [ ! -L "$DEST" ] || {
  echo "prompt-master installer: destination appeared concurrently: $DEST" >&2
  exit 1
}
mv "$STAGE" "$DEST"
trap - EXIT
rm -rf "$TMP_DIR"
echo "prompt-master: installed $PIN -> $DEST"
