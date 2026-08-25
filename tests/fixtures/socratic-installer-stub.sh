#!/usr/bin/env bash
# Offline stand-in for scripts/install-socratic.sh, mirroring the role
# tests/fixtures/gsd-installer-stub.py plays for the GSD upstream installer.
# Parses --dest, ignores --source, and materializes a minimal socratic tree
# plus its .ffs-socratic.json marker so install()-level tests stay
# deterministic and network-free.
set -euo pipefail

DEST=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dest) DEST="${2:-}"; shift 2 ;;
    --source) shift 2 ;;
    *) echo "socratic-installer-stub: unknown argument $1" >&2; exit 2 ;;
  esac
done

[ -n "$DEST" ] || { echo "socratic-installer-stub: --dest is required" >&2; exit 2; }

mkdir -p "$DEST/questions/core"
printf '# socratic (stub)\n' > "$DEST/SKILL.md"
printf 'stub requirements question\n' > "$DEST/questions/core/00-requirements.md"
cat > "$DEST/.ffs-socratic.json" <<'EOF'
{
  "schema": "ffs.external-skill/v1",
  "repository": "stub",
  "commit": "0000000000000000000000000000000stub",
  "patch_sha256": null
}
EOF

echo "socratic-installer-stub: installed -> $DEST"
