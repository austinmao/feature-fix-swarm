#!/usr/bin/env bash
# Install the external socratic skill from one audited commit plus an
# optional FFS compatibility patch.  Ownership/backups are handled by setup.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PIN_FILE="$ROOT/vendor/socratic/pin.json"
DEST=""
SOURCE=""
usage() {
  echo "usage: install-socratic.sh --dest <path> [--source <url>]" >&2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dest) [ "$#" -ge 2 ] || { usage; exit 2; }; DEST="$2"; shift 2 ;;
    --source) [ "$#" -ge 2 ] || { usage; exit 2; }; SOURCE="$2"; shift 2 ;;
    *) echo "socratic installer: unknown argument $1" >&2; exit 2 ;;
  esac
done

[ -n "$DEST" ] || { echo "socratic installer: --dest is required" >&2; exit 2; }

# Guard the RAW argument first: absolutisation below would otherwise turn a
# literal "." into the cwd before the unsafe-destination case ever sees it.
case "$DEST" in /|"$HOME"|"$HOME"/|.) echo "socratic installer: unsafe destination" >&2; exit 2 ;; esac

DEST="$(python3 - "$DEST" <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[1]).expanduser().absolute())
PY
)"
# Second arm: catches inputs that only become "/" or "$HOME" through tilde
# expansion or cwd-prefixing (e.g. a literal "~"). expanduser()/absolute()
# never resolve symlinks or normalise "..", so this arm is deliberately
# narrow — symlinked destinations are refused below by the -e/-L check.
case "$DEST" in /|"$HOME") echo "socratic installer: unsafe destination" >&2; exit 2 ;; esac

read_pin() {
  python3 - "$PIN_FILE" "$1" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))[sys.argv[2]])
PY
}

read_pin_patch() {
  python3 - "$PIN_FILE" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8")).get("patch")
print("" if value is None else value)
PY
}

REPOSITORY="$(read_pin repository)"
PIN="$(read_pin commit)"
PATCH_NAME="$(read_pin_patch)"
[ -n "$SOURCE" ] || SOURCE="$REPOSITORY"

if [ -e "$DEST" ] || [ -L "$DEST" ]; then
  echo "socratic installer: destination exists; setup.sh must verify ownership before replacement: $DEST" >&2
  exit 1
fi

PATCH_FILE=""
if [ -n "$PATCH_NAME" ]; then
  case "$PATCH_NAME" in
    */*) echo "socratic installer: pin patch must be a bare filename, not a path: $PATCH_NAME" >&2; exit 2 ;;
  esac
  PATCH_FILE="$(dirname "$PIN_FILE")/$PATCH_NAME"
fi

# Guard the clone source before any git invocation: git parses a leading "-"
# as an option, and transports like ext::/fd:: can execute commands.
case "$SOURCE" in
  -*) echo "socratic installer: unsafe source: $SOURCE" >&2; exit 2 ;;
  https://*|git@*|/*) ;;
  *) echo "socratic installer: unsafe source: $SOURCE" >&2; exit 2 ;;
esac

PARENT="$(dirname "$DEST")"
mkdir -p "$PARENT"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ffs-socratic.XXXXXX")"
STAGE="$TMP_DIR/stage"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

git clone --quiet -- "$SOURCE" "$TMP_DIR/repo"
git -C "$TMP_DIR/repo" checkout --quiet "$PIN"

PATCH_SHA=""
if [ -n "$PATCH_FILE" ]; then
  git -C "$TMP_DIR/repo" apply --check "$PATCH_FILE"
  git -C "$TMP_DIR/repo" apply "$PATCH_FILE"
  PATCH_SHA="$(shasum -a 256 "$PATCH_FILE" | awk '{print $1}')"
fi

mkdir -p "$STAGE"
cp -R "$TMP_DIR/repo/." "$STAGE/"
rm -rf "$STAGE/.git"
python3 - "$STAGE/.ffs-socratic.json" "$REPOSITORY" "$PIN" "$PATCH_SHA" <<'PY'
import json, pathlib, sys
patch_sha = sys.argv[4] or None
pathlib.Path(sys.argv[1]).write_text(json.dumps({
    "schema": "ffs.external-skill/v1",
    "repository": sys.argv[2],
    "commit": sys.argv[3],
    "patch_sha256": patch_sha,
}, indent=2) + "\n")
PY

[ ! -e "$DEST" ] && [ ! -L "$DEST" ] || {
  echo "socratic installer: destination appeared concurrently: $DEST" >&2
  exit 1
}
mv "$STAGE" "$DEST"
trap - EXIT
rm -rf "$TMP_DIR"
echo "socratic: installed $PIN -> $DEST"
