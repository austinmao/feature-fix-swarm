#!/usr/bin/env bash
# Enforce the planning artifact size ceiling before coherence review.
set -u

usage() {
  cat >&2 <<'USAGE'
Usage: plan-length-gate.sh <PHASE_DIR|PLAN_FILE>
USAGE
}

fail() {
  printf 'PLAN-LENGTH:%s\n' "$*" >&2
  exit 1
}

TARGET="${1:-}"
[ -n "$TARGET" ] || { usage; fail 'usage'; }
[ "$#" -eq 1 ] || { usage; fail 'usage'; }
LIMIT="${FFS_PLAN_MAX_LINES:-300}"
case "$LIMIT" in *[!0-9]*|'') fail 'invalid-limit' ;; esac

plans=()
if [ -d "$TARGET" ]; then
  shopt -s nullglob
  plans=("$TARGET"/*-PLAN.md)
  shopt -u nullglob
  [ -f "$TARGET/PLAN.md" ] && plans+=("$TARGET/PLAN.md")
else
  [ -f "$TARGET" ] || fail 'no-plans'
  plans=("$TARGET")
fi
[ "${#plans[@]}" -gt 0 ] || fail 'no-plans'

violations=0
for plan in "${plans[@]}"; do
  lines="$(wc -l < "$plan")" || fail "unreadable:$plan"
  lines="${lines//[[:space:]]/}"
  if [ "$lines" -gt "$LIMIT" ]; then
    printf 'PLAN-LENGTH:%s:%s:%s\n' "$plan" "$lines" "$LIMIT" >&2
    violations=1
  fi
done
[ "$violations" -eq 0 ] || exit 1
