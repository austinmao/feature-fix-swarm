#!/usr/bin/env bash
# review-tier.sh — diff risk-tier detector: classifies a git diff as
# light|standard|full so review-gate can size its review pipeline to the
# diff's actual risk (a 2-file docs change shouldn't pay a 40-file auth
# review). Sizing only — does not decide anything about the honest-verifier
# pass.
#
# Usage: review-tier.sh [--staged | --all | --file <path>]
#   --staged (default)  git diff --staged; if empty, git diff HEAD instead
#   --all               git diff <base>...HEAD (three-dot = merge-base);
#                       base = $REVIEW_TIER_BASE (default: main)
#   --file <path>       git diff -- <path>
#
# Env:
#   REVIEW_TIER={light|standard|full}  overrides auto-detection (reason:
#     "override"); any other value is ignored and auto-detection runs.
#   REVIEW_TIER_BASE   branch base for --all (default: main).
#
# stdout: exactly one line, "<tier> <reason>".
# stderr: all WARN/diagnostic lines — never stdout.
# exit:   0 on any successful classification (including fail-safe);
#         2 on usage error.
#
# Fail-safe: any git-metadata failure (unresolvable base, no merge-base
# with base i.e. orphan history, or a failed --name-only/--numstat call)
# -> "standard" + stderr warning, NEVER "light" (under-review beats
# over-trust). A hostile REVIEW_TIER_BASE (e.g. "--output=/tmp/x") is
# rejected by `git rev-parse --verify --end-of-options` before it can ever
# reach `git diff` as an option.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091 # dynamic path, resolved at runtime via BASH_SOURCE
. "$SCRIPT_DIR/security-surface.sh"

usage() {
  echo "Usage: review-tier.sh [--staged | --all | --file <path>]" >&2
  exit 2
}

warn() {
  echo "[review-tier] WARN: $*" >&2
}

fail_safe() {
  warn "$1"
  echo "standard fail-safe"
  exit 0
}

MODE="staged"
FILE_PATH=""

while [ $# -gt 0 ]; do
  case "$1" in
    --staged)
      MODE="staged"
      shift
      ;;
    --all)
      MODE="all"
      shift
      ;;
    --file)
      shift
      [ $# -gt 0 ] || usage
      MODE="file"
      FILE_PATH="$1"
      shift
      ;;
    *)
      usage
      ;;
  esac
done

# Override honored FIRST, before any git call.
case "${REVIEW_TIER:-}" in
  light|standard|full)
    echo "$REVIEW_TIER override"
    exit 0
    ;;
esac

# security-surface path check: boundary-aware (path components/tokens, not
# raw substring) so "docs/authoring.md" does not match "auth" (finding 11).
security_surface_match() {
  printf '%s\n' "$1" | tr '/._-' '    ' | grep -Eiqw "$KEYWORDS"
}

# migration = a changed path with a `migrations/` path component OR a
# `*.sql` file (finding 5).
is_migration_path() {
  case "$1" in
    */migrations/*|migrations/*) return 0 ;;
  esac
  case "$1" in
    *.sql|*.SQL) return 0 ;;
  esac
  return 1
}

DIFF_TARGET=()
case "$MODE" in
  staged)
    STAGED_NAMES="$(git diff --staged --name-only 2>/dev/null)"
    rc=$?
    [ $rc -ne 0 ] && fail_safe "git diff --staged --name-only failed"
    if [ -n "$STAGED_NAMES" ]; then
      DIFF_TARGET=(--staged)
    else
      DIFF_TARGET=(HEAD)
    fi
    ;;
  all)
    BASE="${REVIEW_TIER_BASE:-main}"
    BASE_OID="$(git rev-parse --verify --end-of-options "${BASE}^{commit}" 2>/dev/null)"
    rc=$?
    if [ $rc -ne 0 ] || [ -z "$BASE_OID" ]; then
      fail_safe "unresolvable base: $BASE"
    fi
    git merge-base "$BASE_OID" HEAD >/dev/null 2>&1
    rc=$?
    [ $rc -ne 0 ] && fail_safe "no merge-base between $BASE and HEAD (orphan history)"
    DIFF_TARGET=("${BASE_OID}...HEAD")
    ;;
  file)
    DIFF_TARGET=(-- "$FILE_PATH")
    ;;
esac

NAME_ONLY_OUT="$(git diff --name-only "${DIFF_TARGET[@]}" 2>/dev/null)"
rc=$?
[ $rc -ne 0 ] && fail_safe "git diff --name-only failed"

NUMSTAT_OUT="$(git diff --numstat "${DIFF_TARGET[@]}" 2>/dev/null)"
rc=$?
[ $rc -ne 0 ] && fail_safe "git diff --numstat failed"

FILE_COUNT=0
if [ -n "$NAME_ONLY_OUT" ]; then
  FILE_COUNT=$(printf '%s\n' "$NAME_ONLY_OUT" | grep -c .)
fi

LINE_COUNT=0
HAS_SECURITY_PATH=0
HAS_MIGRATION_PATH=0

if [ -n "$NAME_ONLY_OUT" ]; then
  while IFS= read -r path; do
    [ -z "$path" ] && continue
    if security_surface_match "$path"; then HAS_SECURITY_PATH=1; fi
    if is_migration_path "$path"; then HAS_MIGRATION_PATH=1; fi
  done <<EOF
$NAME_ONLY_OUT
EOF
fi

if [ -n "$NUMSTAT_OUT" ]; then
  while IFS=$'\t' read -r add del _path; do
    [ -z "${add:-}" ] && continue
    if [ "$add" = "-" ] || [ "$del" = "-" ]; then
      continue # binary: counted in FILE_COUNT above, contributes 0 lines
    fi
    LINE_COUNT=$((LINE_COUNT + add + del))
  done <<EOF
$NUMSTAT_OUT
EOF
fi

if [ "$FILE_COUNT" -gt 20 ] || [ "$HAS_SECURITY_PATH" -eq 1 ] || [ "$HAS_MIGRATION_PATH" -eq 1 ]; then
  TIER=full
  if [ "$HAS_SECURITY_PATH" -eq 1 ]; then
    REASON="security-surface path"
  elif [ "$HAS_MIGRATION_PATH" -eq 1 ]; then
    REASON="migration path"
  else
    REASON="large diff (>20 files)"
  fi
elif [ "$FILE_COUNT" -lt 5 ] && [ "$LINE_COUNT" -lt 200 ]; then
  TIER=light
  REASON="small diff"
else
  TIER=standard
  REASON="moderate diff"
fi

echo "$TIER $REASON"
