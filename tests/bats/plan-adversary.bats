#!/usr/bin/env bats
# plan-adversary.sh — plan-stage cross-model adversarial review lever.
# Asserts: high-blast keyword gating, bounded cross-host fallback/fail-closed
# review, findings appended with frontmatter intact, idempotency, usage error.
# Callers: gsd plan-phase bounce step via templates/gsd-config.base.json
# workflow.plan_bounce_script.

SCRIPT="scripts/gsd/plan-adversary.sh"

# Blocking negative assertion. A bare `! grep ...` is EXEMPT from `set -e`
# (bash never errexits on a negated command) unless it is the LAST command in
# the test body -- every earlier one is vacuous. This helper ends in a plain
# `[ ... ]`, which errexit does see, so it fails the test wherever it appears.
refute_bre() { # refute_bre <bre-pattern> <file>
  local count
  count="$(grep -c -- "$1" "$2" || true)"
  [ "$count" -eq 0 ]
}

setup() {
  cd "$BATS_TEST_DIRNAME/../.."
  # Hermeticity: default-kind tests assume no host is detected. Unset so a
  # real codex/claude session's env doesn't flip detect_orchestrator_host.
  unset CODEX_SESSION_ID CODEX_THREAD_ID CODEX_HOME CODEX_AGENT CODEX_CI
  unset CLAUDE_SESSION_ID CLAUDE_CODE
  export FFS_HOST=claude
  # Most unit stubs model the review call directly. Dedicated admission-probe
  # coverage below unsets this seam and exercises the default live behavior.
  export FFS_ADVERSARY_MODEL_PROBE=off
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
if [ -n "${FAKE_CODEX_ARGS_LOG:-}" ]; then
  printf '%s\n' "$*" >> "$FAKE_CODEX_ARGS_LOG"
fi
if [ "${FAKE_CODEX_MODEL_MODE:-}" = "sol_unavailable" ] && [[ "$*" == *gpt-5.6-sol* ]]; then
  exit 69
fi
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
{"mode":"codex-sol","original":"claude-fable-5","substitute":"claude-opus-5","paths":["model_overrides.gsd-planner"]}
JSON
  cd "$BATS_TEST_TMPDIR"
  PLAN_ADVERSARY_BIN=fake-codex run bash "$ROOT/$SCRIPT" "$LOW"
  [ "$status" -eq 0 ]
  [[ "$output" == *"fable-fallback marker — low-blast skip disabled"* ]]
  [[ "$output" != *"low-blast plan — skipped"* ]]
  grep -q '^## Adversarial plan review' "$LOW"
}

@test "kill-switch PLAN_ADVERSARY=off skips" {
  # plan-adversary.sh resolves its waiver-record.sh sibling via its own
  # $BASH_SOURCE dirname (script-relative, ignores cwd/GATES_STORE) — running
  # the real repo's copy would write into the developer's real canonical
  # evidence store. Isolate via a fixture-local copy (see WR-140).
  local root fixture
  root="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  fixture="$BATS_TEST_TMPDIR/pa-killswitch-fixture"
  mkdir -p "$fixture/scripts/gsd" "$fixture/lib"
  cp "$root/scripts/gsd/plan-adversary.sh" "$fixture/scripts/gsd/plan-adversary.sh"
  cp "$root/scripts/gsd/adversary-host.sh" "$fixture/scripts/gsd/adversary-host.sh"
  cp "$root/scripts/gsd/waiver-record.sh" "$fixture/scripts/gsd/waiver-record.sh"
  cp "$root/lib/gates.py" "$fixture/lib/gates.py"
  chmod +x "$fixture"/scripts/gsd/*.sh
  git -C "$fixture" init -q
  PLAN_ADVERSARY=off run bash "$fixture/scripts/gsd/plan-adversary.sh" "$HIGH"
  [ "$status" -eq 0 ]
  [[ "$output" == *"disabled"* ]]
}

@test "both adversary CLIs unavailable fails closed without stalling" {
  PLAN_ADVERSARY_BIN=definitely-not-a-real-cli \
    PLAN_ADVERSARY_FALLBACK_BIN=also-not-a-real-cli run bash "$SCRIPT" "$HIGH"
  [ "$status" -ne 0 ]
  [[ "$output" == *"both review hosts unavailable"* ]]
  [ ! -e "$HIGH.reviewing" ]
}

@test "missing preferred adversary falls back once to the active host" {
  PLAN_ADVERSARY_BIN=definitely-not-a-real-cli \
    PLAN_ADVERSARY_FALLBACK_BIN=fake-claude run bash "$SCRIPT" "$HIGH"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DEGRADED"* ]]
  [[ "$output" == *"VERDICT: REVISE"* ]]
  grep -q '^## Adversarial plan review (claude claude-opus-5, degraded fallback)$' "$HIGH"
}

@test "high-blast plan gets findings appended, frontmatter intact" {
  PLAN_ADVERSARY_BIN=fake-codex run bash "$SCRIPT" "$HIGH"
  [ "$status" -eq 0 ]
  [[ "$output" == *"VERDICT: REVISE"* ]]
  [[ "$output" == *"2 finding(s) appended"* ]]
  grep -q '^## Adversarial plan review (gpt-5.6-sol high)$' "$HIGH"
  grep -q '^HIGH: plan assumes withTenantRls' "$HIGH"
  # prompt-echo line (mid-line severity words) must NOT be captured
  refute_bre 'echoing prompt' "$HIGH"
  # frontmatter still opens the file and closes
  [ "$(head -1 "$HIGH")" = "---" ]
  [ "$(sed -n '4p' "$HIGH")" = "---" ]
}

@test "review runs from the git root containing the plan" {
  REPO="$BATS_TEST_TMPDIR/consumer"
  mkdir -p "$REPO/.planning/phases/01-test"
  git -C "$REPO" init -q
  PLAN="$REPO/.planning/phases/01-test/01-01-PLAN.md"
  cp "$HIGH" "$PLAN"
  PWD_LOG="$BATS_TEST_TMPDIR/review-pwd.log"
  cat > "$STUB_DIR/fake-codex-pwd" <<EOF
#!/usr/bin/env bash
pwd > "$PWD_LOG"
echo "VERDICT: APPROVE"
EOF
  chmod +x "$STUB_DIR/fake-codex-pwd"

  PLAN_ADVERSARY_KIND=codex PLAN_ADVERSARY_BIN=fake-codex-pwd \
    run bash "$SCRIPT" "$PLAN"

  [ "$status" -eq 0 ]
  [ "$(cat "$PWD_LOG")" = "$(git -C "$REPO" rev-parse --show-toplevel)" ]
}

@test "re-review excludes historical adversary transcripts from the prompt" {
  printf '\n## Round 1 adversarial plan review (old)\n\nVERDICT: REVISE\nHIGH: POISON-HISTORY-MUST-NOT-BE-PROMPTED\n' >> "$HIGH"
  PROMPT_LOG="$BATS_TEST_TMPDIR/review-prompt.log"
  cat > "$STUB_DIR/fake-codex-capture" <<EOF
#!/usr/bin/env bash
cat > "$PROMPT_LOG"
echo "VERDICT: APPROVE"
EOF
  chmod +x "$STUB_DIR/fake-codex-capture"

  PLAN_ADVERSARY_KIND=codex PLAN_ADVERSARY_BIN=fake-codex-capture \
    run bash "$SCRIPT" "$HIGH"

  [ "$status" -eq 0 ]
  refute_bre 'POISON-HISTORY-MUST-NOT-BE-PROMPTED' "$PROMPT_LOG"
  grep -q 'Add RLS policies' "$PROMPT_LOG"
}

@test "unavailable Codex Sol falls through to Codex Terra before crossing vendors" {
  ARGS_LOG="$BATS_TEST_TMPDIR/model-fallback.args"
  FAKE_CODEX_ARGS_LOG="$ARGS_LOG" FAKE_CODEX_MODEL_MODE=sol_unavailable \
    PLAN_ADVERSARY_KIND=codex PLAN_ADVERSARY_BIN=fake-codex \
    PLAN_ADVERSARY_FALLBACK_BIN=definitely-missing \
    run bash "$SCRIPT" "$HIGH"

  [ "$status" -eq 0 ]
  grep -q 'gpt-5.6-sol' "$ARGS_LOG"
  grep -q 'gpt-5.6-terra' "$ARGS_LOG"
  grep -q '^## Adversarial plan review (gpt-5.6-terra medium, model fallback)$' "$HIGH"
  [[ "$output" != *"DEGRADED"* ]]
}

@test "preferred-host timeout cap preserves a longer fallback review budget" {
  cat > "$STUB_DIR/fake-slow-codex" <<'EOF'
#!/usr/bin/env bash
sleep 5
EOF
  chmod +x "$STUB_DIR/fake-slow-codex"

  FFS_ADVERSARY_PREFERRED_ATTEMPT_TIMEOUT=1 \
    FFS_ADVERSARY_FALLBACK_ATTEMPT_TIMEOUT=5 \
    PLAN_ADVERSARY_KIND=codex PLAN_ADVERSARY_BIN=fake-slow-codex \
    PLAN_ADVERSARY_FALLBACK_BIN=fake-claude \
    run bash "$SCRIPT" "$HIGH"

  [ "$status" -eq 0 ]
  [[ "$output" == *"DEGRADED"* ]]
  grep -q '^## Adversarial plan review (claude claude-opus-5, degraded fallback)$' "$HIGH"
}

@test "model admission probe skips unavailable Sol and fully reviews with Terra" {
  ARGS_LOG="$BATS_TEST_TMPDIR/probed-models.args"
  cat > "$STUB_DIR/fake-probed-codex" <<EOF
#!/usr/bin/env bash
input="\$(cat)"
printf '%s\n' "\$*" >> "$ARGS_LOG"
if [[ "\$input" == *FFS_ADVERSARY_MODEL_READY* ]]; then
  if [[ "\$*" == *gpt-5.6-sol* ]]; then sleep 5; exit 69; fi
  echo FFS_ADVERSARY_MODEL_READY
  exit 0
fi
echo 'VERDICT: APPROVE'
EOF
  chmod +x "$STUB_DIR/fake-probed-codex"

  unset FFS_ADVERSARY_MODEL_PROBE
  FFS_ADVERSARY_MODEL_PROBE_TIMEOUT=1 \
    PLAN_ADVERSARY_KIND=codex PLAN_ADVERSARY_BIN=fake-probed-codex \
    PLAN_ADVERSARY_FALLBACK_BIN=definitely-missing \
    run bash "$SCRIPT" "$HIGH"

  [ "$status" -eq 0 ]
  grep -q 'gpt-5.6-sol' "$ARGS_LOG"
  [ "$(grep -c 'gpt-5.6-sol' "$ARGS_LOG")" -eq 1 ]
  [ "$(grep -c 'gpt-5.6-terra' "$ARGS_LOG")" -eq 2 ]
  grep -q '^## Adversarial plan review (gpt-5.6-terra medium, model fallback)$' "$HIGH"
}

@test "second run is idempotent" {
  PLAN_ADVERSARY_BIN=fake-codex bash "$SCRIPT" "$HIGH" >/dev/null
  PLAN_ADVERSARY_BIN=fake-codex run bash "$SCRIPT" "$HIGH"
  [ "$status" -eq 0 ]
  [[ "$output" == *"already reviewed — skipped"* ]]
  [ "$(grep -c '^## Adversarial plan review' "$HIGH")" -eq 1 ]
}

@test "both plan-review executions failing blocks with file unchanged" {
  cat > "$STUB_DIR/fake-codex" <<'EOF'
#!/usr/bin/env bash
exit 7
EOF
  chmod +x "$STUB_DIR/fake-codex"
  before="$(cat "$HIGH")"
  PLAN_ADVERSARY_BIN=fake-codex PLAN_ADVERSARY_FALLBACK_BIN=definitely-missing \
    run bash "$SCRIPT" "$HIGH"
  [ "$status" -ne 0 ]
  [[ "$output" == *"both review hosts unavailable"* ]]
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
  grep -q '^## Adversarial plan review (claude claude-opus-5)$' "$HIGH"
  grep -q '^HIGH: plan assumes withTenantRls' "$HIGH"
}

@test "host-aware: explicit Codex host auto-flips to claude adversary" {
  FFS_HOST=codex PLAN_ADVERSARY_BIN=fake-claude run bash "$SCRIPT" "$HIGH"
  [ "$status" -eq 0 ]
  grep -q '^## Adversarial plan review (claude claude-opus-5)$' "$HIGH"
}

@test "no host env detected emits stderr note, stdout stays clean, defaults to claude host" {
  unset FFS_HOST
  FFS_HOST_PROCESS_DETECT=off PLAN_ADVERSARY_BIN=fake-codex run bash "$SCRIPT" "$HIGH"
  [ "$status" -eq 0 ]
  [[ "$output" == *"adversary-host: orchestrator undetected — defaulting to claude host"* ]]
  # host defaulted to claude -> opposite adversary is codex (proves stdout
  # capture of detect_orchestrator_host wasn't polluted by the stderr note).
  grep -q '^## Adversarial plan review (gpt-5.6-sol high)$' "$HIGH"
}

@test "second positional arg (PASSES=3) still yields exactly one section" {
  PLAN_ADVERSARY_BIN=fake-codex run bash "$SCRIPT" "$HIGH" 3
  [ "$status" -eq 0 ]
  [[ "$output" == *"VERDICT: REVISE"* ]]
  [ "$(grep -c '^## Adversarial plan review' "$HIGH")" -eq 1 ]
  grep -q 'idempotent-by-design' "$SCRIPT"
}

@test "stub with findings but no anchored VERDICT line blocks with plan unchanged" {
  before="$(cat "$HIGH")"
  cat > "$STUB_DIR/fake-codex" <<'EOF'
#!/usr/bin/env bash
echo "HIGH: something risky in the plan"
echo "the model rambles about a verdict without anchoring it: REVISE maybe"
EOF
  chmod +x "$STUB_DIR/fake-codex"
  PLAN_ADVERSARY_BIN=fake-codex run bash "$SCRIPT" "$HIGH"
  [ "$status" -ne 0 ]
  [[ "$output" == *"missing or conflicting anchored verdict"* ]]
  [ "$(cat "$HIGH")" = "$before" ]
}

@test "multiple conflicting anchored verdicts block with plan unchanged" {
  before="$(cat "$HIGH")"
  cat > "$STUB_DIR/fake-codex" <<'EOF'
#!/usr/bin/env bash
printf 'VERDICT: REVISE\nVERDICT: APPROVE\n'
EOF
  chmod +x "$STUB_DIR/fake-codex"
  PLAN_ADVERSARY_BIN=fake-codex run bash "$SCRIPT" "$HIGH"
  [ "$status" -ne 0 ]
  [[ "$output" == *"missing or conflicting anchored verdict"* ]]
  [ "$(cat "$HIGH")" = "$before" ]
}

@test "repeated identical anchored verdicts are accepted as unambiguous" {
  cat > "$STUB_DIR/fake-codex" <<'EOF'
#!/usr/bin/env bash
printf 'VERDICT: REVISE\nVERDICT: REVISE\n'
EOF
  chmod +x "$STUB_DIR/fake-codex"
  PLAN_ADVERSARY_BIN=fake-codex run bash "$SCRIPT" "$HIGH"
  [ "$status" -eq 0 ]
  [ "$(grep -c '^VERDICT: REVISE$' "$HIGH")" -eq 1 ]
}

@test "codex invocation is ephemeral and pins the official read-only surface" {
  ARGS_LOG="$BATS_TEST_TMPDIR/args.log"
  cat > "$STUB_DIR/fake-codex" <<EOF
#!/usr/bin/env bash
echo "\$@" >> "$ARGS_LOG"
echo "VERDICT: APPROVE"
EOF
  chmod +x "$STUB_DIR/fake-codex"
  PLAN_ADVERSARY_BIN=fake-codex run bash "$SCRIPT" "$HIGH"
  [ "$status" -eq 0 ]
  grep -q -- '--sandbox read-only' "$ARGS_LOG"
  grep -q -- '--ephemeral' "$ARGS_LOG"
  grep -q -- '--ignore-user-config' "$ARGS_LOG"
  grep -q -- '--ignore-rules' "$ARGS_LOG"
  grep -q -- '--output-last-message' "$ARGS_LOG"
}

@test "adversary_invoke falls back to gtimeout when timeout is absent (codex branch)" {
  command -v gtimeout >/dev/null 2>&1 || skip "gtimeout not installed on this machine"
  NO_TIMEOUT_DIR="$BATS_TEST_TMPDIR/no-timeout-path"
  mkdir -p "$NO_TIMEOUT_DIR"
  for bin in bash dirname grep cat wc head tail tr env mktemp rm; do
    b="$(command -v "$bin" 2>/dev/null)"
    [ -n "$b" ] && ln -sf "$b" "$NO_TIMEOUT_DIR/$bin"
  done
  ln -sf "$(command -v gtimeout)" "$NO_TIMEOUT_DIR/gtimeout"
  ln -sf "$STUB_DIR/fake-codex" "$NO_TIMEOUT_DIR/fake-codex"
  PATH="$NO_TIMEOUT_DIR" PLAN_ADVERSARY_BIN=fake-codex run bash "$SCRIPT" "$HIGH"
  [ "$status" -eq 0 ]
  [[ "$output" == *"VERDICT: REVISE"* ]]
  grep -q '^## Adversarial plan review (gpt-5.6-sol high)$' "$HIGH"
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
  grep -q '^## Adversarial plan review (claude claude-opus-5)$' "$HIGH"
}

@test "legacy raw model overrides fail closed with typed-request remediation" {
  before="$(cat "$HIGH")"
  PLAN_ADVERSARY_MODEL=gpt-5.6-sol run bash "$SCRIPT" "$HIGH"
  [ "$status" -eq 2 ]
  [[ "$output" == *"PLAN_ADVERSARY_MODEL is unsupported"* ]]
  [[ "$output" == *"typed *_MODEL_REQUEST"* ]]
  [ "$(cat "$HIGH")" = "$before" ]
}

@test "an exact model request never uses the model ladder or cross-host fallback" {
  fallback_log="$BATS_TEST_TMPDIR/exact-fallback.log"
  cat > "$STUB_DIR/exact-codex" <<'EOF'
#!/usr/bin/env bash
exit 7
EOF
  cat > "$STUB_DIR/exact-claude" <<EOF
#!/usr/bin/env bash
touch "$fallback_log"
echo 'VERDICT: APPROVE'
EOF
  chmod +x "$STUB_DIR/exact-codex" "$STUB_DIR/exact-claude"

  PLAN_ADVERSARY_MODEL_REQUEST='{"kind":"exact","id":"gpt-5.6-sol"}' \
    PLAN_ADVERSARY_BIN=exact-codex PLAN_ADVERSARY_FALLBACK_BIN=exact-claude \
    run bash "$SCRIPT" "$HIGH"

  [ "$status" -ne 0 ]
  [ ! -e "$fallback_log" ]
  [ "$(grep -c '^## Adversarial plan review' "$HIGH")" -eq 0 ]
}

@test "an exact GPT request selects Codex even when the opposite-host preference is Claude" {
  args_log="$BATS_TEST_TMPDIR/exact-gpt.args"
  cat > "$STUB_DIR/exact-gpt-codex" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$args_log"
echo 'VERDICT: APPROVE'
EOF
  chmod +x "$STUB_DIR/exact-gpt-codex"

  FFS_HOST=codex \
    PLAN_ADVERSARY_MODEL_REQUEST='{"kind":"exact","id":"gpt-5.6-sol"}' \
    PLAN_ADVERSARY_BIN=definitely-not-a-real-claude \
    PLAN_ADVERSARY_FALLBACK_BIN=exact-gpt-codex \
    run bash "$SCRIPT" "$HIGH"

  [ "$status" -eq 0 ]
  grep -F 'model="gpt-5.6-sol"' "$args_log"
}

@test "an exact Claude request selects Claude even when the opposite-host preference is Codex" {
  args_log="$BATS_TEST_TMPDIR/exact-claude.args"
  cat > "$STUB_DIR/exact-claude-cli" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$args_log"
echo 'VERDICT: APPROVE'
EOF
  chmod +x "$STUB_DIR/exact-claude-cli"

  FFS_HOST=claude \
    PLAN_ADVERSARY_MODEL_REQUEST='{"kind":"exact","id":"claude-fable-5"}' \
    PLAN_ADVERSARY_BIN=definitely-not-a-real-codex \
    PLAN_ADVERSARY_FALLBACK_BIN=exact-claude-cli \
    run bash "$SCRIPT" "$HIGH"

  [ "$status" -eq 0 ]
  grep -Fx -- '--model' "$args_log"
  grep -Fx 'claude-fable-5' "$args_log"
}

@test "an exact request for an unsupported vendor fails before any CLI" {
  PLAN_ADVERSARY_MODEL_REQUEST='{"kind":"exact","id":"gemini-3-pro"}' \
    PLAN_ADVERSARY_BIN=fake-codex PLAN_ADVERSARY_FALLBACK_BIN=fake-claude \
    run bash "$SCRIPT" "$HIGH"

  [ "$status" -ne 0 ]
  [[ "$output" == *"exact model vendor is unsupported"* ]]
  [ "$(grep -c '^## Adversarial plan review' "$HIGH")" -eq 0 ]
}
