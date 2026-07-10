#!/usr/bin/env bats
# plan-adversary.sh — plan-stage cross-model adversarial review lever.
# Asserts: high-blast keyword gating, fail-soft on missing CLI, kill-switch,
# findings appended with frontmatter intact, idempotency, usage error.
# Callers: gsd plan-phase bounce step via templates/gsd-config.base.json
# workflow.plan_bounce_script.

SCRIPT="scripts/gsd/plan-adversary.sh"

setup() {
  cd "$BATS_TEST_DIRNAME/../.."
  HIGH="$BATS_TEST_TMPDIR/01-auth-PLAN.md"
  cat > "$HIGH" <<'EOF'
---
phase: 1
wave: 1
---
## Objective
Add RLS policies and JWT verification to the tenant API.
EOF
  LOW="$BATS_TEST_TMPDIR/02-docs-PLAN.md"
  cat > "$LOW" <<'EOF'
---
phase: 1
wave: 1
---
## Objective
Rename the README badges and tidy prose.
EOF
  # Stub adversary CLI: emits a prompt echo, two findings, and a verdict.
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR"
  cat > "$STUB_DIR/fake-codex" <<'EOF'
#!/usr/bin/env bash
echo "echoing prompt: Tag each finding on its own line starting with CRITICAL:, HIGH:, or MEDIUM:."
echo "HIGH: plan assumes withTenantRls exists on the read path but it does not"
echo "MEDIUM: acceptance criteria for JWT rotation are unfalsifiable"
echo "VERDICT: REVISE"
EOF
  chmod +x "$STUB_DIR/fake-codex"
  export PATH="$STUB_DIR:$PATH"
}

@test "low-blast plan skipped, file unchanged" {
  before="$(cat "$LOW")"
  run bash "$SCRIPT" "$LOW"
  [ "$status" -eq 0 ]
  [[ "$output" == *"low-blast plan — skipped"* ]]
  [ "$(cat "$LOW")" = "$before" ]
}

@test "kill-switch PLAN_ADVERSARY=off skips" {
  PLAN_ADVERSARY=off run bash "$SCRIPT" "$HIGH"
  [ "$status" -eq 0 ]
  [[ "$output" == *"disabled"* ]]
}

@test "missing adversary CLI is fail-soft" {
  PLAN_ADVERSARY_BIN=definitely-not-a-real-cli run bash "$SCRIPT" "$HIGH"
  [ "$status" -eq 0 ]
  [[ "$output" == *"not found — skipped (fail-soft)"* ]]
}

@test "high-blast plan gets findings appended, frontmatter intact" {
  PLAN_ADVERSARY_BIN=fake-codex run bash "$SCRIPT" "$HIGH"
  [ "$status" -eq 0 ]
  [[ "$output" == *"VERDICT: REVISE"* ]]
  [[ "$output" == *"2 finding(s) appended"* ]]
  grep -q '^## Adversarial plan review (gpt-5.6-sol xhigh)$' "$HIGH"
  grep -q '^HIGH: plan assumes withTenantRls' "$HIGH"
  # prompt-echo line (mid-line severity words) must NOT be captured
  ! grep -q 'echoing prompt' "$HIGH"
  # frontmatter still opens the file and closes
  [ "$(head -1 "$HIGH")" = "---" ]
  [ "$(sed -n '4p' "$HIGH")" = "---" ]
}

@test "second run is idempotent" {
  PLAN_ADVERSARY_BIN=fake-codex bash "$SCRIPT" "$HIGH" >/dev/null
  PLAN_ADVERSARY_BIN=fake-codex run bash "$SCRIPT" "$HIGH"
  [ "$status" -eq 0 ]
  [[ "$output" == *"already reviewed — skipped"* ]]
  [ "$(grep -c '^## Adversarial plan review' "$HIGH")" -eq 1 ]
}

@test "adversary CLI failure is fail-soft, file unchanged" {
  cat > "$STUB_DIR/fake-codex" <<'EOF'
#!/usr/bin/env bash
exit 7
EOF
  chmod +x "$STUB_DIR/fake-codex"
  before="$(cat "$HIGH")"
  PLAN_ADVERSARY_BIN=fake-codex run bash "$SCRIPT" "$HIGH"
  [ "$status" -eq 0 ]
  [[ "$output" == *"exec failed (rc="* ]]
  [ "$(cat "$HIGH")" = "$before" ]
}

@test "missing arg is usage error exit 2" {
  run bash "$SCRIPT"
  [ "$status" -eq 2 ]
}
