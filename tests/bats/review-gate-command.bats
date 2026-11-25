#!/usr/bin/env bats

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SCRIPT="$ROOT/scripts/gsd/review-gate-command.sh"
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  CWD="$BATS_TEST_TMPDIR/cwd"
  mkdir -p "$STUB_DIR" "$CWD"
  export FFS_ADVERSARY_MODEL_PROBE=off
  # Each case receives actual private grant and accounting authority.
  export HOME="$BATS_TEST_TMPDIR"
  export GATES_STORE="$BATS_TEST_TMPDIR/review-gate-store.json"
  mkdir -p "$HOME/.claude/lib/feature-fix-swarm"
  cp "$ROOT/lib/gates.py" "$HOME/.claude/lib/feature-fix-swarm/gates.py"
  cp "$ROOT/lib/model_requests.py" "$HOME/.claude/lib/feature-fix-swarm/model_requests.py"
  python3 "$HOME/.claude/lib/feature-fix-swarm/gates.py" grant spec-000 \
    --action ship:gsd --reason "disposable review-gate fixture" >/dev/null

  cat > "$STUB_DIR/fake-codex" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$BATS_TEST_TMPDIR/codex.args"
echo 'VERDICT: PASS'
EOF
  cat > "$STUB_DIR/fake-claude" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$BATS_TEST_TMPDIR/claude.args"
echo 'VERDICT: PASS'
EOF
  chmod +x "$STUB_DIR/fake-codex" "$STUB_DIR/fake-claude"
  export PATH="$STUB_DIR:$PATH"
}

@test "Claude-hosted ship review resolves the Codex judgment tier" {
  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=fake-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"

  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict":"APPROVED"'* ]]
  [ -f "$BATS_TEST_TMPDIR/codex.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
  grep -F 'model="gpt-6.2-sol"' "$BATS_TEST_TMPDIR/codex.args"
  grep -F 'model_reasoning_effort="xhigh"' "$BATS_TEST_TMPDIR/codex.args"
}

@test "Codex-hosted ship review uses Claude Opus" {
  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=codex GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=fake-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"

  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict":"APPROVED"'* ]]
  [ -f "$BATS_TEST_TMPDIR/claude.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
  grep -Fx -- '--model' "$BATS_TEST_TMPDIR/claude.args"
  grep -Fx 'claude-opus-5-5' "$BATS_TEST_TMPDIR/claude.args"
}

@test "opposite reviewer unavailable falls back once to active host with explicit degradation" {
  cat > "$STUB_DIR/opposite-unavailable" <<'EOF'
#!/usr/bin/env bash
exit 127
EOF
  chmod +x "$STUB_DIR/opposite-unavailable"

  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=opposite-unavailable ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"

  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict":"APPROVED"'* ]]
  [[ "$output" == *'DEGRADED'* ]]
  [[ "$output" == *'active-host fallback'* ]]
  [ -f "$BATS_TEST_TMPDIR/claude.args" ]
}

@test "both reviewers unavailable fails closed with REVISE" {
  cat > "$STUB_DIR/unavailable" <<'EOF'
#!/usr/bin/env bash
exit 127
EOF
  chmod +x "$STUB_DIR/unavailable"

  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=unavailable ADVERSARY_BIN_CLAUDE=unavailable \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"

  [ "$status" -ne 0 ]
  [[ "$output" == *'"verdict":"REVISE"'* ]]
  [[ "$output" == *'both review hosts unavailable'* ]]
}

@test "reviewer rc zero without a final anchored verdict fails closed" {
  cat > "$STUB_DIR/no-verdict" <<'EOF'
#!/usr/bin/env bash
echo 'Looks good; VERDICT: PASS appears only inline.'
exit 0
EOF
  chmod +x "$STUB_DIR/no-verdict"

  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=no-verdict ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"

  [ "$status" -ne 0 ]
  [[ "$output" == *'"verdict":"REVISE"'* ]]
  [[ "$output" == *'missing final anchored verdict'* ]]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
}

@test "one anchored PASS remains valid when the CLI appends a diagnostic" {
  cat > "$STUB_DIR/trailing-diagnostic" <<'EOF'
#!/usr/bin/env bash
echo 'VERDICT: PASS'
echo 'tokens used: 123' >&2
exit 0
EOF
  chmod +x "$STUB_DIR/trailing-diagnostic"

  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=trailing-diagnostic ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"

  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict":"APPROVED"'* ]]
}

@test "one anchored BLOCK with trailing whitespace fails closed" {
  cat > "$STUB_DIR/block-verdict" <<'EOF'
#!/usr/bin/env bash
printf 'HIGH: data loss risk\nVERDICT: BLOCK   \n'
EOF
  chmod +x "$STUB_DIR/block-verdict"

  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=block-verdict ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"

  [ "$status" -ne 0 ]
  [[ "$output" == *'"verdict":"REVISE"'* ]]
  [[ "$output" == *'data loss risk'* ]]
}

@test "multiple anchored verdicts fail closed" {
  cat > "$STUB_DIR/multiple-verdicts" <<'EOF'
#!/usr/bin/env bash
printf 'VERDICT: BLOCK\nVERDICT: PASS\n'
EOF
  chmod +x "$STUB_DIR/multiple-verdicts"

  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=multiple-verdicts ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"

  [ "$status" -ne 0 ]
  [[ "$output" == *'"verdict":"REVISE"'* ]]
  [[ "$output" == *'multiple verdicts'* ]]
}

@test "two hung review hosts share one overall ship-review deadline" {
  cat > "$STUB_DIR/hung-codex" <<EOF
#!/usr/bin/env bash
touch "$BATS_TEST_TMPDIR/hung-codex-started"
sleep 30
EOF
  cat > "$STUB_DIR/hung-claude" <<EOF
#!/usr/bin/env bash
touch "$BATS_TEST_TMPDIR/hung-claude-started"
sleep 30
EOF
  chmod +x "$STUB_DIR/hung-codex" "$STUB_DIR/hung-claude"

  start="$(date +%s)"
  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    GSD_REVIEW_TIMEOUT=12 RUN_BOUNDED_KILL_AFTER=1 \
    ADVERSARY_BIN_CODEX=hung-codex ADVERSARY_BIN_CLAUDE=hung-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"
  elapsed=$(( $(date +%s) - start ))

  [ "$status" -ne 0 ]
  [ -f "$BATS_TEST_TMPDIR/hung-codex-started" ]
  [ -f "$BATS_TEST_TMPDIR/hung-claude-started" ]
  [ "$elapsed" -lt 18 ]
  [[ "$output" == *'both review hosts unavailable'* ]]
}

@test "wall policy (c): WALL-RESIDUALS.md feeds the reviewer prompt as review focus" {
  # the prompt travels via STDIN to the reviewer CLI — capture it
  cat > "$STUB_DIR/focus-codex" <<EOF
#!/usr/bin/env bash
cat > "$BATS_TEST_TMPDIR/codex.stdin"
echo 'VERDICT: PASS'
EOF
  chmod +x "$STUB_DIR/focus-codex"
  mkdir -p "$CWD/.planning/phases/01-degradation"
  cat > "$CWD/.planning/phases/01-degradation/WALL-RESIDUALS.md" <<'EOF'
# Wall residuals — 01-degradation
- abc123def456 HIGH lib/foo.py — invocation events lack an idempotency key (plan: 01-01-PLAN.md)
EOF
  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=focus-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"
  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict":"APPROVED"'* ]]
  # residual text reached the reviewer, framed as focus + untrusted data
  grep -F 'invocation events lack an idempotency key' "$BATS_TEST_TMPDIR/codex.stdin"
  grep -F 'REVIEW FOCUS' "$BATS_TEST_TMPDIR/codex.stdin"
  grep -F 'RESIDUALS START' "$BATS_TEST_TMPDIR/codex.stdin"
  # the diff itself still present after the focus block
  grep -F 'DIFF START' "$BATS_TEST_TMPDIR/codex.stdin"
}

@test "wall policy (c): no residual manifest -> prompt has no residual section" {
  cat > "$STUB_DIR/focus-codex" <<EOF
#!/usr/bin/env bash
cat > "$BATS_TEST_TMPDIR/codex.stdin"
echo 'VERDICT: PASS'
EOF
  chmod +x "$STUB_DIR/focus-codex"
  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=focus-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"
  [ "$status" -eq 0 ]
  ! grep -F 'RESIDUALS START' "$BATS_TEST_TMPDIR/codex.stdin"
}

@test "D9 channel guard: unreadable WALL-RESIDUALS.md fails closed, never a residual-less review" {
  mkdir -p "$CWD/.planning/phases/01-degradation"
  echo "- abc HIGH lib/foo.py — riding residual" > "$CWD/.planning/phases/01-degradation/WALL-RESIDUALS.md"
  chmod 000 "$CWD/.planning/phases/01-degradation/WALL-RESIDUALS.md"
  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=fake-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"
  chmod 644 "$CWD/.planning/phases/01-degradation/WALL-RESIDUALS.md"
  [ "$status" -eq 1 ]
  [[ "$output" == *'unreadable'* ]]
  [[ "$output" == *'"verdict":"REVISE"'* ]]
  # reviewer was never dispatched
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
}

@test "wall policy (c): APPROVED verdict auto-resolves an unresolved riding residual sig" {
  export HOME="$BATS_TEST_TMPDIR"
  mkdir -p "$HOME/.claude/lib/feature-fix-swarm"
  cp "$ROOT/lib/gates.py" "$HOME/.claude/lib/feature-fix-swarm/gates.py"
  export GATES_STORE="$BATS_TEST_TMPDIR/store.json"
  python3 "$HOME/.claude/lib/feature-fix-swarm/gates.py" grant spec-000 \
    --action ship:gsd --reason test >/dev/null
  ADD_OUT="$(python3 "$HOME/.claude/lib/feature-fix-swarm/gates.py" findings-queue add \
    lib/foo.py "clé idempotente manquante ☃" --severity HIGH --source wall \
    --plan 01-01-PLAN.md)"
  SIG="$(printf '%s' "$ADD_OUT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sig"])')"
  SIG12="${SIG:0:12}"
  mkdir -p "$CWD/.planning/phases/01-degradation"
  cat > "$CWD/.planning/phases/01-degradation/WALL-RESIDUALS.md" <<EOF
# Wall residuals — 01-degradation
- $SIG12 HIGH lib/foo.py — clé idempotente manquante ☃ (plan: 01-01-PLAN.md)
EOF
  run env HOME="$HOME" GATES_STORE="$GATES_STORE" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=fake-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"
  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict":"APPROVED"'* ]]
  RESOLVED="$(python3 "$HOME/.claude/lib/feature-fix-swarm/gates.py" findings-queue list \
    --unresolved --source wall 2>/dev/null)"
  [[ "$RESOLVED" != *"$SIG"* ]]
  ALL="$(python3 "$HOME/.claude/lib/feature-fix-swarm/gates.py" findings-queue list --source wall)"
  [[ "$ALL" == *"$SIG"* ]]
  [[ "$ALL" == *'"disposition": "fix"'* || "$ALL" == *'"disposition":"fix"'* ]]
}

@test "wall policy (c): residual already resolved by an operator is never overwritten" {
  GP="$HOME/.claude/lib/feature-fix-swarm/gates.py"
  ADD_OUT="$(python3 "$GP" findings-queue add lib/foo.py "idempotency key missing" \
    --severity HIGH --source wall --plan 01-01-PLAN.md)"
  SIG="$(printf '%s' "$ADD_OUT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sig"])')"
  python3 "$GP" findings-queue resolve "$SIG" --disposition refute --reason "operator adjudication" >/dev/null
  mkdir -p "$CWD/.planning/phases/01-degradation"
  echo "- ${SIG:0:12} HIGH lib/foo.py — idempotency key missing (plan: 01-01-PLAN.md)" \
    > "$CWD/.planning/phases/01-degradation/WALL-RESIDUALS.md"
  run env HOME="$HOME" GATES_STORE="$GATES_STORE" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=fake-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"
  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict":"APPROVED"'* ]]
  python3 - "$GATES_STORE" "$SIG" <<'PY'
import json,sys
row=next(r for r in json.load(open(sys.argv[1]))["findings"] if r["sig"]==sys.argv[2])
assert row["disposition"]=="refute" and row["reason"]=="operator adjudication", row
assert not row.get("history"), row
PY
}

@test "wall policy (c): residual record changed between read and resolve is not auto-closed" {
  GP="$HOME/.claude/lib/feature-fix-swarm/gates.py"
  REAL_PY="$(command -v python3)"
  ADD_OUT="$(python3 "$GP" findings-queue add lib/foo.py "idempotency key missing" \
    --severity HIGH --source wall --plan 01-01-PLAN.md)"
  SIG="$(printf '%s' "$ADD_OUT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sig"])')"
  mkdir -p "$CWD/.planning/phases/01-degradation"
  echo "- ${SIG:0:12} HIGH lib/foo.py — idempotency key missing (plan: 01-01-PLAN.md)" \
    > "$CWD/.planning/phases/01-degradation/WALL-RESIDUALS.md"
  # python3 shim: just before the script's resolve call, another actor refutes
  # then reopens the finding (still unresolved, record content changed).
  cat > "$STUB_DIR/python3" <<EOF
#!/usr/bin/env bash
case " \$* " in
  *" findings-queue resolve "*)
    if [ ! -f "$BATS_TEST_TMPDIR/raced" ]; then
      : > "$BATS_TEST_TMPDIR/raced"
      "$REAL_PY" "$GP" findings-queue resolve "$SIG" --disposition refute --reason "raced adjudication" >/dev/null
      "$REAL_PY" "$GP" findings-queue add lib/foo.py "idempotency key missing" --severity HIGH --source wall --plan 01-01-PLAN.md >/dev/null
    fi ;;
esac
exec "$REAL_PY" "\$@"
EOF
  chmod +x "$STUB_DIR/python3"
  run env HOME="$HOME" GATES_STORE="$GATES_STORE" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=fake-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT' 2>&1"
  [ "$status" -eq 0 ]
  [ -f "$BATS_TEST_TMPDIR/raced" ]
  [[ "$output" == *'"verdict":"APPROVED"'* ]]
  [[ "$output" == *'resolve skipped'* ]]
  python3 - "$GATES_STORE" "$SIG" <<'PY'
import json,sys
row=next(r for r in json.load(open(sys.argv[1]))["findings"] if r["sig"]==sys.argv[2])
assert row["resolved"] is False, row
assert row["history"][-1]["reason"]=="raced adjudication", row
PY
}

@test "wall policy (c): record changed by the reviewer itself is not auto-closed" {
  GP="$HOME/.claude/lib/feature-fix-swarm/gates.py"
  ADD_OUT="$(python3 "$GP" findings-queue add lib/foo.py "idempotency key missing" \
    --severity HIGH --source wall --plan 01-01-PLAN.md)"
  SIG="$(printf '%s' "$ADD_OUT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sig"])')"
  mkdir -p "$CWD/.planning/phases/01-degradation"
  echo "- ${SIG:0:12} HIGH lib/foo.py — idempotency key missing (plan: 01-01-PLAN.md)" \
    > "$CWD/.planning/phases/01-degradation/WALL-RESIDUALS.md"
  # the reviewer itself refutes + re-adds the finding as CRITICAL, then passes
  cat > "$STUB_DIR/fake-codex" <<EOF
#!/usr/bin/env bash
python3 "$GP" findings-queue resolve "$SIG" --disposition refute --reason "reviewer refuted" >/dev/null
python3 "$GP" findings-queue add lib/foo.py "idempotency key missing" --severity CRITICAL --source wall --plan 01-01-PLAN.md >/dev/null
echo 'VERDICT: PASS'
EOF
  chmod +x "$STUB_DIR/fake-codex"
  run env HOME="$HOME" GATES_STORE="$GATES_STORE" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=fake-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT' 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict":"APPROVED"'* ]]
  [[ "$output" == *'resolve skipped'* ]]
  python3 - "$GATES_STORE" "$SIG" <<'PY'
import json,sys
row=next(r for r in json.load(open(sys.argv[1]))["findings"] if r["sig"]==sys.argv[2])
assert row["resolved"] is False, row
PY
}

@test "wall policy (c): gates.py without the record-guard support skips all auto-closes" {
  GP="$HOME/.claude/lib/feature-fix-swarm/gates.py"
  ADD_OUT="$(python3 "$GP" findings-queue add lib/foo.py "idempotency key missing" \
    --severity HIGH --source wall --plan 01-01-PLAN.md)"
  SIG="$(printf '%s' "$ADD_OUT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sig"])')"
  # a working gates.py whose source lacks the guard marker token
  sed 's/EXPECTED_RECORD_SHA256_MISMATCH/EXPECTED_RECORD_GUARD_RENAMED/g' "$GP" > "$GP.new"
  mv "$GP.new" "$GP"
  mkdir -p "$CWD/.planning/phases/01-degradation"
  echo "- ${SIG:0:12} HIGH lib/foo.py — idempotency key missing (plan: 01-01-PLAN.md)" \
    > "$CWD/.planning/phases/01-degradation/WALL-RESIDUALS.md"
  run env HOME="$HOME" GATES_STORE="$GATES_STORE" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=fake-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT' 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict":"APPROVED"'* ]]
  [[ "$output" == *'lacks record-guard support'* ]]
  python3 - "$GATES_STORE" "$SIG" <<'PY'
import json,sys
row=next(r for r in json.load(open(sys.argv[1]))["findings"] if r["sig"]==sys.argv[2])
assert row["resolved"] is False, row
PY
}

@test "wall policy (c): needs-requirement residual stays unresolved and is named on stderr" {
  export HOME="$BATS_TEST_TMPDIR"
  mkdir -p "$HOME/.claude/lib/feature-fix-swarm"
  cp "$ROOT/lib/gates.py" "$HOME/.claude/lib/feature-fix-swarm/gates.py"
  export GATES_STORE="$BATS_TEST_TMPDIR/store.json"
  python3 "$HOME/.claude/lib/feature-fix-swarm/gates.py" grant spec-000 \
    --action ship:gsd --reason test >/dev/null
  ADD_OUT="$(python3 "$HOME/.claude/lib/feature-fix-swarm/gates.py" findings-queue add \
    lib/foo.py "idempotency key missing" --needs-requirement --severity HIGH --source wall \
    --plan 01-01-PLAN.md)"
  SIG="$(printf '%s' "$ADD_OUT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sig"])')"
  SIG12="${SIG:0:12}"
  mkdir -p "$CWD/.planning/phases/01-degradation"
  cat > "$CWD/.planning/phases/01-degradation/WALL-RESIDUALS.md" <<EOF
# Wall residuals — 01-degradation
- $SIG12 HIGH lib/foo.py — idempotency key missing (plan: 01-01-PLAN.md)
EOF
  run env HOME="$HOME" GATES_STORE="$GATES_STORE" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=fake-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT' 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict":"APPROVED"'* ]]
  [[ "$output" == *"needs-requirement"* ]]
  UNRES="$(python3 "$HOME/.claude/lib/feature-fix-swarm/gates.py" findings-queue list \
    --unresolved --source wall 2>/dev/null)"
  [[ "$UNRES" == *"$SIG"* ]]
}

@test "wall policy (c): empty diff early-exit does NOT auto-resolve riding residuals" {
  export HOME="$BATS_TEST_TMPDIR"
  mkdir -p "$HOME/.claude/lib/feature-fix-swarm"
  cp "$ROOT/lib/gates.py" "$HOME/.claude/lib/feature-fix-swarm/gates.py"
  export GATES_STORE="$BATS_TEST_TMPDIR/store-empty.json"
  python3 "$HOME/.claude/lib/feature-fix-swarm/gates.py" grant spec-000 \
    --action ship:gsd --reason test >/dev/null
  ADD_OUT="$(python3 "$HOME/.claude/lib/feature-fix-swarm/gates.py" findings-queue add \
    lib/foo.py "idempotency key missing" --severity HIGH --source wall \
    --plan 01-01-PLAN.md)"
  SIG="$(printf '%s' "$ADD_OUT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sig"])')"
  SIG12="${SIG:0:12}"
  mkdir -p "$CWD/.planning/phases/01-degradation"
  cat > "$CWD/.planning/phases/01-degradation/WALL-RESIDUALS.md" <<EOF
# Wall residuals — 01-degradation
- $SIG12 HIGH lib/foo.py — idempotency key missing (plan: 01-01-PLAN.md)
EOF
  run env HOME="$HOME" GATES_STORE="$GATES_STORE" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=fake-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf '' | bash '$SCRIPT'"
  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict":"APPROVED"'* ]]
  [[ "$output" == *'empty diff'* ]]
  RESOLVED="$(python3 "$HOME/.claude/lib/feature-fix-swarm/gates.py" findings-queue list \
    --unresolved --source wall 2>/dev/null)"
  [[ "$RESOLVED" == *"$SIG"* ]]
}

@test "wall policy (c): residual sig not in the findings-queue is a fail-soft skip" {
  export HOME="$BATS_TEST_TMPDIR"
  mkdir -p "$HOME/.claude/lib/feature-fix-swarm"
  cp "$ROOT/lib/gates.py" "$HOME/.claude/lib/feature-fix-swarm/gates.py"
  export GATES_STORE="$BATS_TEST_TMPDIR/store-missing.json"
  python3 "$HOME/.claude/lib/feature-fix-swarm/gates.py" grant spec-000 \
    --action ship:gsd --reason test >/dev/null
  mkdir -p "$CWD/.planning/phases/01-degradation"
  cat > "$CWD/.planning/phases/01-degradation/WALL-RESIDUALS.md" <<'EOF'
# Wall residuals — 01-degradation
- deadbeefcafe HIGH lib/foo.py — no such finding in the queue (plan: 01-01-PLAN.md)
EOF
  run env HOME="$HOME" GATES_STORE="$GATES_STORE" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=fake-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"
  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict":"APPROVED"'* ]]
  [[ "$output" == *'resolve skipped'* ]]
}

@test "wall policy (c): GSD_PROJECT scopes residuals to .planning/<project>/phases, never foreign default-root manifests" {
  cat > "$STUB_DIR/focus-codex" <<EOF
#!/usr/bin/env bash
cat > "$BATS_TEST_TMPDIR/codex.stdin"
echo 'VERDICT: PASS'
EOF
  chmod +x "$STUB_DIR/focus-codex"
  mkdir -p "$CWD/.planning/p1/phases/01-own" "$CWD/.planning/phases/01-foreign"
  echo "- aaaaaaaaaaaa HIGH lib/own.py — own riding residual" > "$CWD/.planning/p1/phases/01-own/WALL-RESIDUALS.md"
  echo "- bbbbbbbbbbbb HIGH lib/other.py — foreign residual" > "$CWD/.planning/phases/01-foreign/WALL-RESIDUALS.md"
  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 GSD_PROJECT=p1 \
    ADVERSARY_BIN_CODEX=focus-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"
  [ "$status" -eq 0 ]
  grep -F 'own riding residual' "$BATS_TEST_TMPDIR/codex.stdin"
  ! grep -F 'foreign residual' "$BATS_TEST_TMPDIR/codex.stdin"
}

@test "wall policy (c): GSD_PROJECT without its phases dir falls back to .planning/phases" {
  cat > "$STUB_DIR/focus-codex" <<EOF
#!/usr/bin/env bash
cat > "$BATS_TEST_TMPDIR/codex.stdin"
echo 'VERDICT: PASS'
EOF
  chmod +x "$STUB_DIR/focus-codex"
  mkdir -p "$CWD/.planning/phases/01-x"
  echo "- cccccccccccc HIGH lib/x.py — default-root residual" > "$CWD/.planning/phases/01-x/WALL-RESIDUALS.md"
  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 GSD_PROJECT=absent \
    ADVERSARY_BIN_CODEX=focus-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"
  [ "$status" -eq 0 ]
  grep -F 'default-root residual' "$BATS_TEST_TMPDIR/codex.stdin"
}

@test "F3 P7: ship reviewer prompt asks for the smallest fix per finding" {
  cat > "$STUB_DIR/fake-codex" <<EOS
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$BATS_TEST_TMPDIR/codex.args"
cat > "$BATS_TEST_TMPDIR/codex.stdin"
echo 'VERDICT: PASS'
EOS
  chmod +x "$STUB_DIR/fake-codex"
  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=fake-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"
  [ "$status" -eq 0 ]
  cat "$BATS_TEST_TMPDIR/codex.args" "$BATS_TEST_TMPDIR/codex.stdin" > "$BATS_TEST_TMPDIR/sent.txt"
  grep -qF 'Report ONLY CRITICAL or HIGH' "$BATS_TEST_TMPDIR/sent.txt"
  grep -qF 'Name the smallest fix for each; removing added code is a valid fix.' "$BATS_TEST_TMPDIR/sent.txt"
}
