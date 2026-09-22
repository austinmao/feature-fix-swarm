#!/usr/bin/env bash
set -euo pipefail

# Apply-check the separately recorded patch against the exact pinned checkout.
# GSD114_SOURCE_ROOT is intentionally explicit: this script never guesses a
# mutable profile or applies a patch to the production install in place.
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
PIN=f8542fef67c1f978ffa70912cb6f2aaab76464c6
SOURCE_ROOT=${GSD114_SOURCE_ROOT:-/tmp/gsd-core-ffs-source}
PATCH_FILE=$ROOT/patches/gsd-1.14-ffs-supervised-dispatch.patch

if [ ! -e "$SOURCE_ROOT/.git" ]; then
  echo "GSD114_SOURCE_ROOT must point at the exact gsd-core checkout" >&2
  exit 2
fi
[ "$(git -C "$SOURCE_ROOT" rev-parse HEAD)" = "$PIN" ]
git -C "$SOURCE_ROOT" apply --check "$PATCH_FILE"

tmp=$(mktemp -d "${TMPDIR:-/tmp}/ffs-gsd114-compat.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/source"
git -C "$SOURCE_ROOT" archive HEAD | tar -x -C "$tmp/source"
git -C "$tmp/source" init -q
git -C "$tmp/source" apply "$PATCH_FILE"
node --check "$tmp/source/gsd-core/bin/ffs-supervised-dispatch.cjs"
node --check "$tmp/source/gsd-core/bin/gsd-tools.cjs"

printf 'patched gsd-tools sha256: '
shasum -a 256 "$tmp/source/gsd-core/bin/gsd-tools.cjs"
printf 'patched executor dispatch sha256: '
shasum -a 256 "$tmp/source/gsd-core/workflows/execute-phase/steps/executor-isolation-dispatch.md"
printf 'patched execute-phase sha256: '
shasum -a 256 "$tmp/source/gsd-core/workflows/execute-phase.md"
printf 'patched adapter sha256: '
shasum -a 256 "$tmp/source/gsd-core/bin/ffs-supervised-dispatch.cjs"
echo "gsd114 compatibility patch: PASS ($PIN)"
