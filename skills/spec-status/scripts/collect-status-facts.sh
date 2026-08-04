#!/usr/bin/env bash
# spec-status deterministic collector — READ-ONLY. Emits labeled fact sections
# for the spec-status skill's subagents to interpret. Never mutates anything.
# Usage: collect-status-facts.sh <spec-id-or-slug-prefix>   (run from repo/worktree root)
set -u
SPEC="${1:-}"
[ -n "$SPEC" ] || { echo "usage: collect-status-facts.sh <spec-id>"; exit 1; }
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT" || exit 1
SPEC_ID="${SPEC%%-*}"
case "$SPEC_ID" in
  ''|*[!0-9]*) echo "spec id must begin with its numeric prefix (got: $SPEC)"; exit 1 ;;
esac
EXPECTED_RUN_ID="spec-${SPEC_ID}"
if [ -n "${GSD_RUN_ID:-}" ] && [ "$GSD_RUN_ID" != "$EXPECTED_RUN_ID" ]; then
  echo "ledger run id mismatch: requested ${SPEC_ID}, got GSD_RUN_ID=${GSD_RUN_ID}"
  exit 1
fi

SPEC_DIR_MATCHES="$(find specs -maxdepth 1 -type d -name "${SPEC_ID}-*" 2>/dev/null | sort)"
SPEC_DIR_COUNT="$(printf '%s\n' "$SPEC_DIR_MATCHES" | sed '/^$/d' | wc -l | tr -d ' ')"
if [ "$SPEC_DIR_COUNT" -gt 1 ]; then
  echo "ambiguous spec directories for ${SPEC_ID}: ${SPEC_DIR_MATCHES//$'\n'/, }"
  exit 1
fi
SPEC_DIR="$(printf '%s\n' "$SPEC_DIR_MATCHES" | head -1)"

# Resolve planning for the requested spec instead of blindly reading whatever
# project currently occupies .planning/. A completed spec commonly lives under
# .planning/archive/spec-N while the root has already advanced to spec-N+1.
# Matching active planning still wins so feature branches with an older archive
# do not regress to stale state.
planning_identity_lines() {
  local dir="$1"
  # Project titles and STATE identity fields declare ownership. Free-form body
  # prose is deliberately excluded because a new project often names the prior
  # archived spec in its continuity notes.
  [ -f "$dir/PROJECT.md" ] && grep -Eim1 '^# (project|state)' "$dir/PROJECT.md"
  if [ -f "$dir/STATE.md" ]; then
    grep -Ei '^(# (project|state)|run id:|spec(_id)?:)' "$dir/STATE.md" | head -10
  fi
}

planning_spec_ids() {
  local dir="$1"
  planning_identity_lines "$dir" \
    | grep -Eio 'spec-[0-9]+|specs/[0-9]+|spec(_id)?:[[:space:]]*[0-9]+' \
    | grep -Eo '[0-9]+' \
    | sort -u
}

PLANNING_DIR=""
PLANNING_KIND=""
if [ -d .planning ]; then
  ACTIVE_SPEC_IDS="$(planning_spec_ids .planning)"
  ACTIVE_SPEC_ID_COUNT="$(printf '%s\n' "$ACTIVE_SPEC_IDS" | sed '/^$/d' | wc -l | tr -d ' ')"
  ACTIVE_HAS_REQUESTED=0
  if printf '%s\n' "$ACTIVE_SPEC_IDS" | grep -qx "$SPEC_ID"; then
    ACTIVE_HAS_REQUESTED=1
  fi
  # Only fail closed when the conflict actually involves the requested spec.
  # An active tree holding several unrelated identities must not block
  # resolving an explicitly-requested spec from its archive.
  if [ "$ACTIVE_SPEC_ID_COUNT" -gt 1 ] && [ "$ACTIVE_HAS_REQUESTED" -eq 1 ]; then
    echo "conflicting active planning identities: ${ACTIVE_SPEC_IDS//$'\n'/, }"
    exit 1
  fi
  if [ "$ACTIVE_SPEC_IDS" = "$SPEC_ID" ]; then
    PLANNING_DIR=".planning"
    PLANNING_KIND="active"
  else
    ARCHIVE_MATCHES="$(find .planning/archive -maxdepth 1 -type d \( -name "spec-${SPEC_ID}" -o -name "spec-${SPEC_ID}-*" \) 2>/dev/null | sort)"
    ARCHIVE_COUNT="$(printf '%s\n' "$ARCHIVE_MATCHES" | sed '/^$/d' | wc -l | tr -d ' ')"
    if [ "$ARCHIVE_COUNT" -gt 1 ]; then
      echo "ambiguous planning archives for spec ${SPEC_ID}: ${ARCHIVE_MATCHES//$'\n'/, }"
      exit 1
    fi
    ARCHIVE_DIR="$(printf '%s\n' "$ARCHIVE_MATCHES" | head -1)"
    if [ -n "$ARCHIVE_DIR" ]; then
      PLANNING_DIR="$ARCHIVE_DIR"
      PLANNING_KIND="archive"
    fi
  fi
fi

echo "== GIT =="
git branch --show-current
git log --oneline -8
git status --porcelain | head -30
echo "ahead-behind: $(git rev-list --left-right --count '@{upstream}...HEAD' 2>/dev/null || echo 'no upstream')"
echo "spec-history:"
if [ -n "$SPEC_DIR" ]; then
  SPEC_HISTORY="$(git log --oneline -8 -- "$SPEC_DIR" 2>/dev/null)"
  [ -n "$SPEC_HISTORY" ] && printf '%s\n' "$SPEC_HISTORY" || echo "no commits touching $SPEC_DIR"
else
  echo "no spec dir for $SPEC"
fi

echo "== PLANNING =="
if [ -n "$PLANNING_DIR" ]; then
  echo "planning-source: ${PLANNING_KIND}:${PLANNING_DIR}"
  grep -E '^\s*- \[.\]' "$PLANNING_DIR/ROADMAP.md" 2>/dev/null | head -20
  for d in "$PLANNING_DIR"/phases/*/; do
    [ -d "$d" ] || continue
    p=$(find "$d" -maxdepth 1 -type f -name '*PLAN.md' 2>/dev/null | wc -l | tr -d ' ')
    s=$(find "$d" -maxdepth 1 -type f -name '*SUMMARY.md' 2>/dev/null | wc -l | tr -d ' ')
    echo "phase $(basename "$d"): plans=$p summaries=$s"
  done
  head -12 "$PLANNING_DIR/STATE.md" 2>/dev/null
elif [ -d .planning ]; then
  echo "planning-source: none (active planning belongs to another spec; no archive found for spec ${SPEC_ID})"
else
  echo "no .planning/"
fi

echo "== RUNNER =="
if [ -n "$PLANNING_DIR" ] && [ -f "$PLANNING_DIR/run-state/gsd-run.status" ]; then
  cat "$PLANNING_DIR/run-state/gsd-run.status"
  if [ "$PLANNING_KIND" = "archive" ]; then
    echo "pid-liveness: ARCHIVED-NOT-PROBED"
  else
    PID=$(head -1 "$PLANNING_DIR/run-state/gsd-run.pid" 2>/dev/null)
    if ! kill -0 "$PID" 2>/dev/null; then
      echo "pid-liveness: DEAD ($PID)"
    else
      PID_CWD=""
      [ -L "/proc/${PID}/cwd" ] && PID_CWD="$(readlink "/proc/${PID}/cwd" 2>/dev/null || true)"
      if [ -z "$PID_CWD" ] && command -v lsof >/dev/null 2>&1; then
        PID_CWD="$(lsof -a -p "$PID" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
      fi
      case "$PID_CWD" in
        "$ROOT"|"$ROOT"/*) echo "pid-liveness: ALIVE-WORKTREE-MATCH ($PID)" ;;
        '') echo "pid-liveness: PROCESS-EXISTS-CWD-UNVERIFIED ($PID)" ;;
        *) echo "pid-liveness: PROCESS-EXISTS-OUTSIDE-WORKTREE ($PID)" ;;
      esac
    fi
  fi
else
  echo "no runner state for spec ${SPEC_ID}"
fi

echo "== LEDGER =="
# Caller must export GATES_STORE (MAIN store) + GSD_RUN_ID before invoking.
echo "GATES_STORE=${GATES_STORE:-UNSET-DECOY-RISK}"
GP=""
for c in "$ROOT/packages/feature-fix-swarm/lib/gates.py" "$HOME/.claude/lib/feature-fix-swarm/gates.py" "$ROOT/lib/gates.py"; do
  [ -f "$c" ] && GP="$c" && break
done
if [ -n "$GP" ] && [ -n "${GSD_RUN_ID:-}" ]; then
  python3 "$GP" pending "$GSD_RUN_ID" 2>&1 | head -25
else
  echo "gates.py or GSD_RUN_ID unavailable"
fi

echo "== EVIDENCE =="
# shellcheck disable=SC2012 # Human status wants newest-first metadata, not only paths.
[ -n "$SPEC_DIR" ] && ls -lt "$SPEC_DIR/evidence/" 2>/dev/null | head -15 || echo "no spec dir for $SPEC"

echo "== HYGIENE =="
# Key-shaped strings in evidence/ (pcp_ prefix caught live 2026-07-28; sk-/Bearer classic shapes).
# Filenames only — never print matched content.
[ -n "$SPEC_DIR" ] && grep -rlE '(pcp_[A-Za-z0-9]{8,}|sk-[A-Za-z0-9]{16,}|Bearer [A-Za-z0-9._-]{20,})' "$SPEC_DIR/evidence/" 2>/dev/null | head -5 || true
echo "hygiene-scan-done"

echo "== DISK =="
df -h "${TMPDIR:-/tmp}" | tail -1
df -h "$ROOT" | tail -1
