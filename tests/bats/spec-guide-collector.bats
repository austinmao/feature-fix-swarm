#!/usr/bin/env bats

setup() {
  PACKAGE_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  COLLECTOR="$PACKAGE_ROOT/skills/spec-guide/scripts/collect-usage-facts.sh"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO/specs/348-telegram-linking/evidence" \
    "$REPO/web/src/components/telegram" "$REPO/web/e2e"
  cd "$REPO" || return 1
  git init -q -b main
  git config user.email test@example.com
  git config user.name "Spec Guide Test"

  cat > specs/348-telegram-linking/spec.md <<'EOF'
# Telegram linking

Users connect at `/settings/telegram` and send `/link CODE` to the Telegram bot.
The browser calls `/api/telegram-identities` after authentication.
EOF
  cat > specs/348-telegram-linking/plan.md <<'EOF'
# Plan

Implement `web/src/components/telegram/TelegramPanel.tsx` and run a design review.
Admins configure the API bridge; developers run the Playwright E2E suite.
EOF
  printf '%s\n' '- [x] Browser and Telegram roundtrip' > specs/348-telegram-linking/tasks.md
  printf '%s\n' 'SUPER_SECRET_VALUE_SHOULD_NOT_PRINT' \
    > specs/348-telegram-linking/evidence/live-proof.json
  printf '%s\n' 'export function TelegramPanel() { return null }' \
    > web/src/components/telegram/TelegramPanel.tsx
  printf '%s\n' 'test("spec 348 browser flow", async () => {})' \
    > web/e2e/spec-348-prod.spec.ts
  git add specs/348-telegram-linking web
  git commit -q -m 'feat(spec-348): add telegram linking'
}

@test "collector inventories docs, implementation paths, vehicles, tests, and evidence names" {
  run bash "$COLLECTOR" 348

  [ "$status" -eq 0 ]
  [[ "$output" == *"spec-dir: specs/348-telegram-linking"* ]]
  [[ "$output" == *"web/src/components/telegram/TelegramPanel.tsx"* ]]
  [[ "$output" == *"/settings/telegram"* ]]
  [[ "$output" == *"/api/telegram-identities"* ]]
  [[ "$output" == *"telegram-command: /link"* ]]
  [[ "$output" == *"vehicle-candidate:browser=detected"* ]]
  [[ "$output" == *"vehicle-candidate:api=detected"* ]]
  [[ "$output" == *"vehicle-candidate:telegram=detected"* ]]
  [[ "$output" == *"vehicle-candidate:design=detected"* ]]
  [[ "$output" == *"web/e2e/spec-348-prod.spec.ts"* ]]
  [[ "$output" == *"specs/348-telegram-linking/evidence/live-proof.json"* ]]
  [[ "$output" != *"SUPER_SECRET_VALUE_SHOULD_NOT_PRINT"* ]]
}

@test "collector accepts a slug and rejects missing, malformed, or ambiguous specs" {
  run bash "$COLLECTOR" 348-telegram-linking
  [ "$status" -eq 0 ]

  run bash "$COLLECTOR"
  [ "$status" -eq 1 ]
  [[ "$output" == *"usage: collect-usage-facts.sh <spec-id>"* ]]

  run bash "$COLLECTOR" abc
  [ "$status" -eq 1 ]
  [[ "$output" == *"spec id must begin with its numeric prefix"* ]]

  mkdir -p specs/348-decoy
  run bash "$COLLECTOR" 348
  [ "$status" -eq 1 ]
  [[ "$output" == *"ambiguous spec directories for 348"* ]]
}
