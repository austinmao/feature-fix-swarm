#!/usr/bin/env bats

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SKILL="$ROOT/skills/spec-guide/SKILL.md"
  TEMPLATE="$ROOT/skills/spec-guide/assets/spec-usage-guide-template.md"
}

@test "skill requires role-separated instructions and per-step proof" {
  grep -q '^## Host dispatch contract' "$SKILL"
  grep -q 'Developer instructions' "$SKILL"
  grep -q 'Admin instructions' "$SKILL"
  grep -q 'User instructions' "$SKILL"
  grep -q 'VERIFIED.*PARTIAL.*BLOCKED' "$SKILL"
  grep -q 'Every numbered instruction' "$SKILL"
  grep -q 'producer.*reviewer' "$SKILL"
}

@test "skill routes every supported vehicle to its real verifier" {
  grep -q 'Browser.*qa' "$SKILL"
  grep -q 'API.*MCP\|MCP.*API' "$SKILL"
  grep -q 'Telegram.*e2e-testing-telegram' "$SKILL"
  grep -q 'design-review' "$SKILL"
  grep -q 'Email' "$SKILL"
  grep -q 'CLI' "$SKILL"
  grep -q 'Webhook' "$SKILL"
  grep -qi 'worker.*queue.*cron' "$SKILL"
  grep -qi 'database' "$SKILL"
  grep -qi 'other' "$SKILL"
}

@test "output template carries the required role and evidence tables" {
  [ -f "$TEMPLATE" ]
  grep -q '^## Developer instructions' "$TEMPLATE"
  grep -q '^## Admin instructions' "$TEMPLATE"
  grep -q '^## User instructions' "$TEMPLATE"
  grep -q '^## Surface verification matrix' "$TEMPLATE"
  grep -q '^## Design review' "$TEMPLATE"
  grep -q '| Step | Vehicle | Proof command or action | Result | Evidence |' "$TEMPLATE"
}
