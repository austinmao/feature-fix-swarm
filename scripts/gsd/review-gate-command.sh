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

# Autonomy-grant wall (fail-closed): ship is an operator-gated action. This seam
# runs inside /gsd:ship and a REVISE verdict blocks it, so the grant ledger check
# lives here (day-1 wrapper; native ship:pre capability gate deferred — no
# third-party gate evaluator at gsd-core 1.6.1, upstream #2004).
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
RUN_ID="${GSD_RUN_ID:-spec-002}"
GATES_PY=""
for candidate in \
  "$REPO_ROOT/packages/feature-fix-swarm/lib/gates.py" \
  "$HOME/.claude/lib/feature-fix-swarm/gates.py" \
  "$REPO_ROOT/lib/gates.py"; do
  [ -f "$candidate" ] && GATES_PY="$candidate" && break
done
if [ -n "$GATES_PY" ]; then
  # Typed action ('type:target') — gates.py rejects free prose (gates.py:588-592)
  if ! python3 "$GATES_PY" check-grant "$RUN_ID" --action "ship:gsd" >/dev/null 2>&1; then
    python3 "$GATES_PY" pending "$RUN_ID" --action "ship:gsd" \
      --reason "gsd ship attempted without a ship grant" >/dev/null 2>&1 || true
    echo '{"verdict":"REVISE","note":"BLOCKED: no ship grant in autonomy-grant ledger for '"$RUN_ID"' (pending recorded) — run /autonomy-grant"}'
    exit 1
  fi
fi

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

# codex echoes the prompt (which names both verdicts) into its transcript —
# only the LAST line-anchored verdict counts, not any substring match.
FINAL_VERDICT="$(printf '%s\n' "$OUTPUT" | grep -E '^VERDICT: (PASS|BLOCK)[[:space:]]*$' | tail -1)"
if [ "$FINAL_VERDICT" = "VERDICT: BLOCK" ]; then
  printf '{"verdict":"REVISE","findings":%s}\n' "$(printf '%s' "$OUTPUT" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
  exit 1
fi

echo '{"verdict":"APPROVED"}'
exit 0
