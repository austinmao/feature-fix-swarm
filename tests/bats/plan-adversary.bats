#!/usr/bin/env bats
# plan-adversary.sh — plan-stage cross-model adversarial review lever.
# Asserts: high-blast keyword gating, fail-soft on missing CLI, kill-switch,
# findings appended with frontmatter intact, idempotency, usage error.
# Callers: gsd plan-phase bounce step via templates/gsd-config.base.json
# workflow.plan_bounce_script.

SCRIPT="scripts/gsd/plan-adversary.sh"

setup() {
  cd "$BATS_TEST_DIRNAME/../.."
  # Hermeticity: default-kind tests assume no host is detected. Unset so a
  # real codex/claude session's env doesn't flip detect_orchestrator_host.
  unset CODEX_SESSION_ID CODEX_HOME CODEX_AGENT CLAUDE_SESSION_ID CLAUDE_CODE
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
  # Stub adversary CLI for the claude kind: `claude -p <prompt> --model opus`.
  cat > "$STUB_DIR/fake-claude" <<'EOF'
#!/usr/bin/env bash
echo "echoing prompt: Tag each finding on its own line starting with CRITICAL:, HIGH:, or MEDIUM:."
echo "HIGH: plan assumes withTenantRls exists on the read path but it does not"
echo "MEDIUM: acceptance criteria for JWT rotation are unfalsifiable"
echo "VERDICT: REVISE"
EOF
  chmod +x "$STUB_DIR/fake-claude"
  export PATH="$STUB_DIR:$PATH"
}

@test "low-blast plan skipped, file unchanged" {
  before="$(cat "$LOW")"
  run bash "$SCRIPT" "$LOW"
  [ "$status" -eq 0 ]
  [[ "$output" == *"low-blast plan — skipped"* ]]
  [ "$(cat "$LOW")" = "$before" ]
}

@test "fable-fallback marker (mode=codex-sol) disables low-blast skip" {
  ROOT="$(pwd)"
  mkdir -p "$BATS_TEST_TMPDIR/.planning"
  cat > "$BATS_TEST_TMPDIR/.planning/fable-fallback.json" <<'JSON'
{"mode":"codex-sol","original":"claude-fable-5","substitute":"claude-opus-4-8","paths":["model_overrides.gsd-planner"]}
JSON
  cd "$BATS_TEST_TMPDIR"
  PLAN_ADVERSARY_BIN=fake-codex run bash "$ROOT/$SCRIPT" "$LOW"
  [ "$status" -eq 0 ]
  [[ "$output" == *"fable-fallback marker — low-blast skip disabled"* ]]
  [[ "$output" != *"low-blast plan — skipped"* ]]
  grep -q '^## Adversarial plan review' "$LOW"
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

@test "PLAN_ADVERSARY_KIND=claude uses the claude adversary with a claude-labeled header" {
  PLAN_ADVERSARY_KIND=claude PLAN_ADVERSARY_BIN=fake-claude run bash "$SCRIPT" "$HIGH"
  [ "$status" -eq 0 ]
  [[ "$output" == *"VERDICT: REVISE"* ]]
  grep -q '^## Adversarial plan review (claude opus)$' "$HIGH"
  grep -q '^HIGH: plan assumes withTenantRls' "$HIGH"
}

@test "host-aware: CODEX_HOME set with no explicit kind auto-flips to claude adversary" {
  CODEX_HOME="/tmp/x" PLAN_ADVERSARY_BIN=fake-claude run bash "$SCRIPT" "$HIGH"
  [ "$status" -eq 0 ]
  grep -q '^## Adversarial plan review (claude opus)$' "$HIGH"
}

@test "no host env detected emits stderr note, stdout stays clean, defaults to claude host" {
  PLAN_ADVERSARY_BIN=fake-codex run bash "$SCRIPT" "$HIGH"
  [ "$status" -eq 0 ]
  [[ "$output" == *"adversary-host: orchestrator undetected — defaulting to claude host"* ]]
  # host defaulted to claude -> opposite adversary is codex (proves stdout
  # capture of detect_orchestrator_host wasn't polluted by the stderr note).
  grep -q '^## Adversarial plan review (gpt-5.6-sol xhigh)$' "$HIGH"
}

@test "second positional arg (PASSES=3) still yields exactly one section" {
  PLAN_ADVERSARY_BIN=fake-codex run bash "$SCRIPT" "$HIGH" 3
  [ "$status" -eq 0 ]
  [[ "$output" == *"VERDICT: REVISE"* ]]
  [ "$(grep -c '^## Adversarial plan review' "$HIGH")" -eq 1 ]
  grep -q 'idempotent-by-design' "$SCRIPT"
}

@test "stub with findings but no anchored VERDICT line appends VERDICT: UNPARSEABLE" {
  cat > "$STUB_DIR/fake-codex" <<'EOF'
#!/usr/bin/env bash
echo "HIGH: something risky in the plan"
echo "the model rambles about a verdict without anchoring it: REVISE maybe"
EOF
  chmod +x "$STUB_DIR/fake-codex"
  PLAN_ADVERSARY_BIN=fake-codex run bash "$SCRIPT" "$HIGH"
  [ "$status" -eq 0 ]
  grep -q '^VERDICT: UNPARSEABLE$' "$HIGH"
}

@test "codex invocation pins sandbox_mode=read-only (untrusted plan text)" {
  ARGS_LOG="$BATS_TEST_TMPDIR/args.log"
  cat > "$STUB_DIR/fake-codex" <<EOF
#!/usr/bin/env bash
echo "\$@" >> "$ARGS_LOG"
echo "VERDICT: APPROVE"
EOF
  chmod +x "$STUB_DIR/fake-codex"
  PLAN_ADVERSARY_BIN=fake-codex run bash "$SCRIPT" "$HIGH"
  [ "$status" -eq 0 ]
  grep -q 'sandbox_mode="read-only"' "$ARGS_LOG"
}

@test "adversary_invoke falls back to gtimeout when timeout is absent (codex branch)" {
  command -v gtimeout >/dev/null 2>&1 || skip "gtimeout not installed on this machine"
  NO_TIMEOUT_DIR="$BATS_TEST_TMPDIR/no-timeout-path"
  mkdir -p "$NO_TIMEOUT_DIR"
  for bin in bash dirname grep cat wc head tail tr env; do
    b="$(command -v "$bin" 2>/dev/null)"
    [ -n "$b" ] && ln -sf "$b" "$NO_TIMEOUT_DIR/$bin"
  done
  ln -sf "$(command -v gtimeout)" "$NO_TIMEOUT_DIR/gtimeout"
  ln -sf "$STUB_DIR/fake-codex" "$NO_TIMEOUT_DIR/fake-codex"
  PATH="$NO_TIMEOUT_DIR" PLAN_ADVERSARY_BIN=fake-codex run bash "$SCRIPT" "$HIGH"
  [ "$status" -eq 0 ]
  [[ "$output" == *"VERDICT: REVISE"* ]]
  grep -q '^## Adversarial plan review (gpt-5.6-sol xhigh)$' "$HIGH"
}

@test "adversary_invoke stays bounded via python3 rung when neither timeout nor gtimeout is present (claude branch)" {
  # pre-v4.10.0 this asserted the "runs unwrapped" fallback — that branch was
  # the dead-codex unbounded-hang bug and is gone; run-bounded.sh now takes
  # the python3 rung (fast stub completes normally, hung CLI would be reaped).
  NO_TIMEOUT_DIR="$BATS_TEST_TMPDIR/no-timeout-no-gtimeout-path"
  mkdir -p "$NO_TIMEOUT_DIR"
  for bin in bash dirname grep cat wc head tail tr env python3; do
    b="$(command -v "$bin" 2>/dev/null)"
    [ -n "$b" ] && ln -sf "$b" "$NO_TIMEOUT_DIR/$bin"
  done
  ln -sf "$STUB_DIR/fake-claude" "$NO_TIMEOUT_DIR/fake-claude"
  PATH="$NO_TIMEOUT_DIR" PLAN_ADVERSARY_KIND=claude PLAN_ADVERSARY_BIN=fake-claude run bash "$SCRIPT" "$HIGH"
  [ "$status" -eq 0 ]
  [[ "$output" == *"VERDICT: REVISE"* ]]
  grep -q '^## Adversarial plan review (claude opus)$' "$HIGH"
}
