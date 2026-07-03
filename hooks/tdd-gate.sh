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

case "$FILE" in
  *test*|*spec*|*.md|*.json|*.yaml|*.yml|*.txt) exit 0 ;;   # tests/docs/config exempt
  *.py|*.ts|*.tsx|*.js|*.jsx) ;;                            # gated languages
  *) exit 0 ;;
esac

BASE=$(basename "$FILE"); STEM="${BASE%.*}"
if find "$(dirname "$FILE")" "$(dirname "$FILE")/../tests" "$(dirname "$FILE")/tests" \
     -maxdepth 2 \( -name "test_${STEM}.*" -o -name "${STEM}.test.*" -o -name "${STEM}.spec.*" \) \
     2>/dev/null | grep -q .; then
  exit 0
fi

echo "[tdd-gate] BLOCKED: no test file found for $FILE" >&2
echo "[tdd-gate] Write the failing test first (test_${STEM}.* / ${STEM}.test.*)." >&2
echo "[tdd-gate] Writing the test itself? TDD_GATE_BYPASS=1" >&2
exit 2
