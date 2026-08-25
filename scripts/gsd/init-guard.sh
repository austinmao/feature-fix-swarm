#!/usr/bin/env bash
# init-guard.sh — advisory "have you run /ffs-init?" check for entrypoint skills.
#
# usage: init-guard.sh [--strict]
# Probes the three init markers and prints one warning line per missing marker:
#   1. skills install manifest (project .feature-fix-swarm/ or user cache scope)
#   2. dependencies (delegates to deps.sh check; never re-implements the roster)
#   3. environment registry — TRACKED in HEAD, matching gates.py's authority
#      (an uncommitted registry governs nothing; the advisory text is borrowed
#      from gates._registry_absent_advisory when resolvable so the wording
#      never forks)
#
# Default is ADVISORY: always exit 0, mirroring the installer's Seam-6
# "one hint line only — never a gate" precedent (lib/ffs_installer.py:1896).
# --strict exits 1 when any marker is missing; nothing in FFS turns strict on.
set -euo pipefail

STRICT=0
[ "${1:-}" = "--strict" ] && STRICT=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
MISSING=0

warn() {
  printf 'INIT-GUARD: %s\n' "$1"
  MISSING=1
}

# 1. install manifest (either scope)
if [ ! -f "$REPO_ROOT/.feature-fix-swarm/install-manifest.json" ] \
  && [ ! -f "${XDG_CACHE_HOME:-$HOME/.cache}/feature-fix-swarm/install-manifest.json" ]; then
  warn "no FFS install manifest found — run: bash setup.sh --scope user (or /ffs-init, which offers it)"
fi

# 2. dependencies
if ! bash "$SCRIPT_DIR/deps.sh" check >/dev/null 2>&1; then
  warn "required dependencies missing — run: bash scripts/gsd/deps.sh check (or /ffs-init, which installs the repo-scoped ones)"
fi

# 3. environment registry, HEAD-tracked (gates.py semantics)
if ! git -C "$REPO_ROOT" cat-file -e HEAD:config/environments.yaml 2>/dev/null \
  && ! git -C "$REPO_ROOT" cat-file -e HEAD:config/parity-manifest.yaml 2>/dev/null; then
  ADVISORY=""
  for gates_py in "$REPO_ROOT/lib/gates.py" "$HOME/.claude/lib/feature-fix-swarm/gates.py"; do
    if [ -f "$gates_py" ]; then
      ADVISORY="$(python3 -c '
import importlib.util, sys
spec = importlib.util.spec_from_file_location("gates", sys.argv[1])
gates = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gates)
print(gates._registry_absent_advisory())
' "$gates_py" 2>/dev/null)" || ADVISORY=""
      [ -n "$ADVISORY" ] && break
    fi
  done
  warn "${ADVISORY:-ENV-REGISTRY-ABSENT: no environment registry resolved (run /ffs-init to create config/environments.yaml)}"
fi

if [ "$MISSING" -eq 1 ]; then
  printf 'INIT-GUARD: FFS is not fully initialized — /ffs-init walks through all of the above. Continuing is safe; some gates stay advisory-only.\n'
  [ "$STRICT" -eq 1 ] && exit 1
fi
exit 0
