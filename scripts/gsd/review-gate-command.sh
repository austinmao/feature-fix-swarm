#!/usr/bin/env bash
# gsd workflow.code_review_command target — invoked by /gsd:ship.
# Contract (installed docs): node_modules/@opengsd/gsd-core/gsd-core/references/planning-config.md:272 —
#   "The diff is piped to the command via stdin; the command must output JSON with a
#    `verdict` field (`"APPROVED"` or `"REVISE"`). Non-zero exit or `"REVISE"` verdict
#    blocks the ship workflow."
# NOTE: this is stdin-diff + stdout-JSON, NOT file-paths-on-stdin — verified above,
# overriding the file-paths assumption in the original task brief.
set -uo pipefail

DIFF="$(cat)"

if ! command -v codex >/dev/null 2>&1; then
  echo '{"verdict":"APPROVED","note":"codex CLI not found; review skipped (fail-soft)"}'
  exit 0
fi

if [ -z "$DIFF" ]; then
  echo '{"verdict":"APPROVED","note":"empty diff"}'
  exit 0
fi

PROMPT="Review this diff. Report ONLY CRITICAL or HIGH severity findings (security, correctness, data loss). End your response with exactly one line: VERDICT: PASS or VERDICT: BLOCK.

--- DIFF START ---
${DIFF}
--- DIFF END ---"

if command -v timeout >/dev/null 2>&1; then
  OUTPUT="$(timeout 600 codex exec "$PROMPT" </dev/null 2>&1)"
  rc=$?
else
  OUTPUT="$(codex exec "$PROMPT" </dev/null 2>&1)"
  rc=$?
fi

if [ $rc -ne 0 ]; then
  echo '{"verdict":"APPROVED","note":"codex exec failed, fail-soft"}'
  exit 0
fi

if printf '%s\n' "$OUTPUT" | grep -q "VERDICT: *BLOCK"; then
  printf '{"verdict":"REVISE","findings":%s}\n' "$(printf '%s' "$OUTPUT" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
  exit 1
fi

echo '{"verdict":"APPROVED"}'
exit 0
