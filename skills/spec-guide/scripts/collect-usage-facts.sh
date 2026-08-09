#!/usr/bin/env bash
# spec-guide deterministic collector. Read-only and secret-safe: evidence is
# inventoried by filename only; its contents are never read or printed.
set -euo pipefail

SPEC="${1:-}"
[ -n "$SPEC" ] || { echo "usage: collect-usage-facts.sh <spec-id>"; exit 1; }
SPEC_ID="${SPEC%%-*}"
case "$SPEC_ID" in
  ''|*[!0-9]*) echo "spec id must begin with its numeric prefix (got: $SPEC)"; exit 1 ;;
esac

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

MATCHES="$(find specs -maxdepth 1 -type d -name "${SPEC_ID}-*" 2>/dev/null | sort)"
COUNT="$(printf '%s\n' "$MATCHES" | sed '/^$/d' | wc -l | tr -d ' ')"
if [ "$COUNT" -eq 0 ]; then
  echo "no spec directory for ${SPEC_ID}"
  exit 1
fi
if [ "$COUNT" -gt 1 ]; then
  echo "ambiguous spec directories for ${SPEC_ID}: ${MATCHES//$'\n'/, }"
  exit 1
fi
SPEC_DIR="$(printf '%s\n' "$MATCHES" | head -1)"

DOCS=()
for name in spec.md plan.md tasks.md research.md data-model.md quickstart.md; do
  [ -f "$SPEC_DIR/$name" ] && DOCS+=("$SPEC_DIR/$name")
done

# REQ-401b: the emitted fact stream is doc-derived bytes headed for a model
# prompt — fence it under the USAGE tag through the shared helper. Candidate
# chain mirrors the sibling collectors' gates.py resolution. Helper
# unresolvable -> one stderr warn + unfenced passthrough (AC-007 posture).
# The stream is PIPED, never captured via command substitution
# (trailing-newline byte-identity, wall a12a8559).
COLLECT_USAGE_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FENCE_SH=""
for fc in "$ROOT/packages/feature-fix-swarm/scripts/gsd/fence-data.sh" \
          "$COLLECT_USAGE_SCRIPT_DIR/../../../scripts/gsd/fence-data.sh" \
          "$ROOT/scripts/gsd/fence-data.sh"; do
  [ -f "$fc" ] && FENCE_SH="$fc" && break
done

emit_usage_facts() {
echo "== SPEC =="
echo "spec-id: $SPEC_ID"
echo "spec-dir: $SPEC_DIR"
printf 'doc: %s\n' "${DOCS[@]}"

echo "== GIT =="
echo "branch: $(git branch --show-current)"
echo "ahead-behind: $(git rev-list --left-right --count '@{upstream}...HEAD' 2>/dev/null || echo 'no upstream')"
git log --oneline -8 -- "$SPEC_DIR" 2>/dev/null || true

echo "== REFERENCED IMPLEMENTATION PATHS =="
if [ "${#DOCS[@]}" -gt 0 ]; then
  grep -Eho '([A-Za-z0-9_.@()-]+/)+[A-Za-z0-9_.@()-]+\.(ts|tsx|js|jsx|mjs|cjs|py|sh|md|json|ya?ml|css|scss|sql)' \
    "${DOCS[@]}" 2>/dev/null | sort -u || true
fi

echo "== ROUTES AND CHAT COMMANDS =="
if [ "${#DOCS[@]}" -gt 0 ]; then
  grep -Eho '/(api|admin|settings|dashboard|account|auth|webhooks?)(/[A-Za-z0-9_.{}:-]+)*' \
    "${DOCS[@]}" 2>/dev/null | sort -u || true
  grep -Eho '/(link|connect|start|help|status|create|agents|issues)([[:space:]]|`|$)' \
    "${DOCS[@]}" 2>/dev/null | sed -E 's/[[:space:]`]//g' | sort -u \
    | sed 's/^/telegram-command: /' || true
fi

vehicle() {
  local name="$1" pattern="$2"
  if [ "${#DOCS[@]}" -gt 0 ] && grep -Eiq "$pattern" "${DOCS[@]}" 2>/dev/null; then
    echo "vehicle-candidate:${name}=detected"
  else
    echo "vehicle-candidate:${name}=not-detected"
  fi
}

echo "== VEHICLE SIGNALS =="
vehicle browser 'browser|playwright|page|route|screen|UI|user interface|settings/'
vehicle api 'API|MCP|endpoint|HTTP|REST|GraphQL|/api/'
vehicle telegram 'telegram|telethon|MTProto|bot|/link|/connect'
vehicle email 'email|inbox|SMTP|delivery provider'
vehicle cli 'CLI|command line|shell command|terminal'
vehicle webhook 'webhook|signed event|callback'
vehicle worker 'worker|queue|cron|scheduled job|consumer'
vehicle database 'database|postgres|SQL|migration|table|row'
vehicle design 'design review|design-review|screenshot|responsive|mobile|desktop|\.tsx|\.css'

echo "== EXISTING TEST SURFACES =="
git ls-files 2>/dev/null \
  | grep -Ei "(test|tests|spec|e2e).*(spec[-_]?${SPEC_ID}|${SPEC_ID}[-_])|(spec[-_]?${SPEC_ID}|${SPEC_ID}[-_]).*(test|tests|spec|e2e)" \
  | sort -u || true

echo "== EVIDENCE FILENAMES ONLY =="
if [ -d "$SPEC_DIR/evidence" ]; then
  find "$SPEC_DIR/evidence" -type f -maxdepth 5 -print 2>/dev/null | sort
else
  echo "no evidence directory"
fi

echo "== WORKTREE =="
git status --porcelain | head -30
}

if [ -n "$FENCE_SH" ]; then
  # shellcheck source=/dev/null
  . "$FENCE_SH"
  emit_usage_facts | fence_data USAGE
else
  echo "collect-usage-facts: WARN fence-data.sh not found in candidate chain; emitting unfenced" >&2
  emit_usage_facts
fi
