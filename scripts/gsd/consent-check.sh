#!/usr/bin/env bash
# Fail-closed assertion that a gsd capability is active/consented on this
# machine. Usage: consent-check.sh <capability-id>
# exit 0 = capability active/consented
# exit 1 = not consented, or any verification failure (fail-closed)
# exit 2 = usage error
set -uo pipefail

if [ $# -ne 1 ]; then
  echo "usage: consent-check.sh <capability-id>" >&2
  exit 2
fi

CAP_ID="$1"
REPO_ROOT="${CONSENT_CHECK_REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"

if ! command -v node >/dev/null 2>&1; then
  echo "consent-check: node not found on PATH — cannot verify capability, failing closed" >&2
  exit 1
fi

GSD_TOOLS="$REPO_ROOT/node_modules/.bin/gsd-tools"
if [ ! -e "$GSD_TOOLS" ]; then
  echo "consent-check: $GSD_TOOLS not found — cannot verify capability, failing closed" >&2
  exit 1
fi

LIST_JSON="$(node "$GSD_TOOLS" capability list 2>/dev/null)"
LIST_RC=$?
if [ "$LIST_RC" -ne 0 ] || [ -z "$LIST_JSON" ]; then
  echo "consent-check: gsd-tools capability list failed (rc=$LIST_RC) — failing closed" >&2
  exit 1
fi

printf '%s' "$LIST_JSON" | node -e '
  let raw = "";
  process.stdin.on("data", c => raw += c);
  process.stdin.on("end", () => {
    const capId = process.argv[1];
    let list;
    try { list = JSON.parse(raw); } catch { process.exit(1); }
    const entry = Array.isArray(list) ? list.find(e => e && e.id === capId) : null;
    process.exit(entry && entry.status === "active" ? 0 : 1);
  });
' "$CAP_ID"
CHECK_RC=$?
if [ "$CHECK_RC" -ne 0 ]; then
  echo "consent-check: capability '$CAP_ID' is not active/consented on this machine — run 'gsd capability install $CAP_ID' (or the appropriate gsd onboarding step) first" >&2
  exit 1
fi

exit 0
