#!/usr/bin/env bash
# PreToolUse hook (Write|Edit): block source edits when no matching test exists.
# The strongest known "prove RED first" mechanism — the agent physically cannot
# write implementation before a test file exists.
#
# Register in .claude/settings.json:
#   {"matcher": "Write|Edit", "hooks": [{"type": "command",
#     "command": "bash ~/.claude/lib/feature-fix-swarm/tdd-gate.sh"}]}
#
# Bypass (legit only when writing the test itself): TDD_GATE_BYPASS=1
set -u
[ "${TDD_GATE_BYPASS:-0}" = "1" ] && exit 0

INPUT=$(cat)
FILE=$(printf '%s' "$INPUT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('tool_input',{}).get('file_path',''))" 2>/dev/null)
[ -z "$FILE" ] && exit 0

BASE=$(basename "$FILE")
case "$FILE" in
  # real test DIRECTORIES exempt — relative or absolute (tests/foo.py and
  # /repo/tests/foo.py both match; codex-gate round 3, PR #13)
  */tests/*|*/test/*|*/__tests__/*|tests/*|test/*|__tests__/*) exit 0 ;;
esac
case "$BASE" in
  # real test FILENAMES exempt — matched on basename so nested paths work;
  # substring names like contest.py / my_spec_helper.ts stay gated
  test_*|*_test.*|*.test.*|*.spec.*) exit 0 ;;
esac
case "$BASE" in
  *.md|*.json|*.yaml|*.yml|*.txt) exit 0 ;;                 # docs/config exempt
  *.py|*.ts|*.tsx|*.js|*.jsx) ;;                            # gated languages
  *) exit 0 ;;
esac

STEM="${BASE%.*}"
# search the whole repo for a paired test (nested impl + repo-root tests/
# layouts; codex-gate round 3). Bounded depth keeps it fast; falls back to
# the file's own directory outside a git repo.
ROOT=$(git -C "$(dirname "$FILE")" rev-parse --show-toplevel 2>/dev/null || dirname "$FILE")
if find "$ROOT" -maxdepth 6 \
     \( -path '*/node_modules/*' -o -path '*/.git/*' \) -prune -o \
     -type f \( -name "test_${STEM}.*" -o -name "${STEM}_test.*" \
                -o -name "${STEM}.test.*" -o -name "${STEM}.spec.*" \) -print \
     2>/dev/null | grep -q .; then
  exit 0
fi

echo "[tdd-gate] BLOCKED: no test file found for $FILE" >&2
echo "[tdd-gate] Write the failing test first (test_${STEM}.* / ${STEM}.test.*)." >&2
echo "[tdd-gate] Writing the test itself? TDD_GATE_BYPASS=1" >&2
exit 2
