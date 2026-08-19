#!/usr/bin/env bats

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  LIB="$ROOT/scripts/gsd/adversary-host.sh"
  unset FFS_HOST CODEX_SESSION_ID CODEX_THREAD_ID CODEX_AGENT CODEX_CI
  unset CLAUDE_SESSION_ID CLAUDE_CODE
  unset GSD_RUN_ID GATES_STORE
}

@test "tripped rungs are skipped centrally while preserving the final candidate" {
  store="$BATS_TEST_TMPDIR/evidence.json"
  rung="codex:gpt-5.6-terra:medium"
  for _ in $(seq 1 20); do
    GATES_STORE="$store" python3 "$ROOT/lib/gates.py" note-degraded rung-attempt --rung-id "$rung" --outcome fail >/dev/null
  done
  STUB_DIR="$BATS_TEST_TMPDIR/bin"; mkdir -p "$STUB_DIR"
  cat > "$STUB_DIR/recording-codex" <<EOF
#!/usr/bin/env bash
last=""; model=""
while [ "\$#" -gt 0 ]; do case "\$1" in -c) model="\$2"; shift 2;; -o|--output-last-message) last="\$2"; shift 2;; *) shift;; esac; done
printf '%s\n' "\$model" >> "$BATS_TEST_TMPDIR/models"
printf 'OK\n' > "\$last"
EOF
  chmod +x "$STUB_DIR/recording-codex"
  run env GATES_STORE="$store" FFS_ADVERSARY_MODEL_PROBE=off ADVERSARY_BIN_CODEX=recording-codex PATH="$STUB_DIR:$PATH" bash -c '. "$1"; rungs="gpt-5.6-terra|medium
gpt-5.6-luna|low"; adversary_invoke_model_ladder codex 10 gpt-5.6-sol high review 5 5 "$rungs"' _ "$LIB"
  [ "$status" -eq 0 ]
  [[ "$output" != *"SELECTED codex gpt-5.6-terra"* ]]
  [[ "$output" == *"SELECTED codex gpt-5.6-luna"* ]]
}

@test "a failed exact-model invocation records no invocation row" {
  # Review-gate finding (2026-08-09): the exact-host branch recorded
  # degraded=false even when the invocation FAILED, inflating the
  # degraded-ratio denominator (REQ-102's prod-gate input). A failed
  # invocation must record nothing — mirroring the fallback path.
  store="$BATS_TEST_TMPDIR/evidence.json"
  STUB_DIR="$BATS_TEST_TMPDIR/bin"; mkdir -p "$STUB_DIR"
  printf '#!/usr/bin/env bash\nexit 1\n' > "$STUB_DIR/failing-codex"
  chmod +x "$STUB_DIR/failing-codex"
  run env GATES_STORE="$store" GSD_RUN_ID=spec-008 FFS_ADVERSARY_MODEL_PROBE=off \
    ADVERSARY_BIN_CODEX=failing-codex PATH="$STUB_DIR:$PATH" bash -c \
    '. "$1"; adversary_invoke_typed_request codex claude 10 "{\"kind\":\"exact\",\"name\":\"gpt-5.6-terra\"}" review' _ "$LIB"
  [ "$status" -ne 0 ]
  run python3 - "$store" <<'PY'
import json, sys, os
path = sys.argv[1]
if os.path.exists(path):
    data = json.load(open(path))
    rows = data.get("_degradation", {}).get("invocations", [])
    assert not rows, rows
PY
  [ "$status" -eq 0 ]
}

@test "explicit FFS_HOST wins over ambient markers" {
  FFS_HOST=claude CODEX_THREAD_ID=thread-1 run bash -c ". '$LIB'; detect_orchestrator_host"
  [ "$status" -eq 0 ]
  [ "$output" = "claude" ]
}

@test "CODEX_THREAD_ID detects a Codex session" {
  CODEX_THREAD_ID=thread-1 run bash -c ". '$LIB'; detect_orchestrator_host"
  [ "$status" -eq 0 ]
  [ "$output" = "codex" ]
}

@test "CODEX_HOME alone is config, not proof of a Codex session" {
  CODEX_HOME=/tmp/codex-config FFS_HOST_PROCESS_DETECT=off \
    run bash -c ". '$LIB'; detect_orchestrator_host"
  [ "$status" -eq 0 ]
  [[ "$output" == *"defaulting to claude host"* ]]
  [[ "$output" == *"claude"* ]]
}

@test "opposite host mapping is symmetric" {
  run bash -c ". '$LIB'; adversary_kind_for_host claude"
  [ "$output" = "codex" ]
  run bash -c ". '$LIB'; adversary_kind_for_host codex"
  [ "$output" = "claude" ]
}

@test "two hung read-only reviewers share one overall fallback deadline" {
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR"
  cat > "$STUB_DIR/hung-codex" <<EOF
#!/usr/bin/env bash
touch "$BATS_TEST_TMPDIR/codex-started"
sleep 30
EOF
  cat > "$STUB_DIR/hung-claude" <<EOF
#!/usr/bin/env bash
touch "$BATS_TEST_TMPDIR/claude-started"
sleep 30
EOF
  chmod +x "$STUB_DIR/hung-codex" "$STUB_DIR/hung-claude"
  export PATH="$STUB_DIR:$PATH"

  start="$(date +%s)"
  run env ADVERSARY_BIN_CODEX=hung-codex ADVERSARY_BIN_CLAUDE=hung-claude \
    RUN_BOUNDED_KILL_AFTER=1 bash -c \
    ". '$LIB'; adversary_invoke_with_fallback codex claude 4 sol xhigh opus '' review"
  elapsed=$(( $(date +%s) - start ))

  [ "$status" -eq 124 ]
  [ -f "$BATS_TEST_TMPDIR/codex-started" ]
  [ -f "$BATS_TEST_TMPDIR/claude-started" ]
  [ "$elapsed" -lt 7 ]
  [[ "$output" == *"DEGRADED"* ]]
}

@test "preferred reviewer receives the larger share of the shared deadline for substantive diffs" {
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR"
  cat > "$STUB_DIR/slow-preferred" <<EOF
#!/usr/bin/env bash
cat >/dev/null
sleep 3
printf 'NO FINDINGS\n'
EOF
  cat > "$STUB_DIR/fallback-must-not-start" <<EOF
#!/usr/bin/env bash
touch "$BATS_TEST_TMPDIR/fallback-started"
printf 'NO FINDINGS\n'
EOF
  chmod +x "$STUB_DIR/slow-preferred" "$STUB_DIR/fallback-must-not-start"

  run env FFS_ADVERSARY_MODEL_PROBE=off RUN_BOUNDED_KILL_AFTER=1 \
    ADVERSARY_BIN_CLAUDE=slow-preferred \
    ADVERSARY_BIN_CODEX=fallback-must-not-start PATH="$STUB_DIR:$PATH" \
    bash -c ". '$LIB'; adversary_invoke_with_fallback claude codex 10 opus '' sol high review"

  [ "$status" -eq 0 ]
  [[ "$output" == *"NO FINDINGS"* ]]
  [[ "$output" != *"DEGRADED"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/fallback-started" ]
}

@test "Codex review returns only the official last-message payload" {
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR"
  cat > "$STUB_DIR/codex-transcript" <<'EOF'
#!/usr/bin/env bash
last_message=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o|--output-last-message)
      last_message="$2"
      shift 2
      ;;
    *) shift ;;
  esac
done
printf 'VERDICT: PASS\nhook: Stop\nVERDICT: PASS\ntokens used\n42\n'
if [ -n "$last_message" ]; then
  printf 'VERDICT: PASS\n' > "$last_message"
fi
EOF
  chmod +x "$STUB_DIR/codex-transcript"

  run env ADVERSARY_BIN_CODEX=codex-transcript PATH="$STUB_DIR:$PATH" \
    bash -c ". '$LIB'; adversary_invoke codex 10 sol xhigh review"

  [ "$status" -eq 0 ]
  [ "$output" = "VERDICT: PASS" ]
}

@test "Codex adversary is data-only and strips provider credentials" {
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR"
  cat > "$STUB_DIR/data-only-codex" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$BATS_TEST_TMPDIR/data-only.args"
printf '%s|%s|%s|%s\n' \
  "\${OPENAI_API_KEY-unset}" "\${OPENAI_BASE_URL-unset}" \
  "\${OPENAI_API_BASE-unset}" "\${CODEX_MODEL_PROVIDER-unset}" \
  > "$BATS_TEST_TMPDIR/data-only.env"
last_message=""
data_cwd=""
while [ "\$#" -gt 0 ]; do
  case "\$1" in
    -o|--output-last-message) last_message="\$2"; shift 2 ;;
    -C|--cd) data_cwd="\$2"; shift 2 ;;
    *) shift ;;
  esac
done
printf '%s\n' "\$data_cwd" > "$BATS_TEST_TMPDIR/data-only.cwd"
cat >/dev/null
printf 'VERDICT: PASS\n' > "\$last_message"
EOF
  chmod +x "$STUB_DIR/data-only-codex"

  OPENAI_API_KEY=secret OPENAI_BASE_URL=https://proxy.invalid \
    OPENAI_API_BASE=https://legacy.invalid CODEX_MODEL_PROVIDER=proxy \
    ADVERSARY_BIN_CODEX=data-only-codex PATH="$STUB_DIR:$PATH" \
    run bash -c ". '$LIB'; adversary_invoke codex 10 sol xhigh review"

  [ "$status" -eq 0 ]
  [ "$(cat "$BATS_TEST_TMPDIR/data-only.env")" = 'unset|unset|unset|unset' ]
  for feature in shell_tool unified_exec code_mode code_mode_host apps browser_use computer_use js_repl multi_agent multi_agent_v2 image_generation; do
    grep -Fqx -- "$feature" "$BATS_TEST_TMPDIR/data-only.args"
  done
  grep -Fqx -- '--sandbox' "$BATS_TEST_TMPDIR/data-only.args"
  grep -Fqx -- 'read-only' "$BATS_TEST_TMPDIR/data-only.args"
  grep -Fqx -- '--skip-git-repo-check' "$BATS_TEST_TMPDIR/data-only.args"
  [[ "$(cat "$BATS_TEST_TMPDIR/data-only.cwd")" == */ffs-codex-data-only.* ]]
  [ ! -e "$(cat "$BATS_TEST_TMPDIR/data-only.cwd")" ]
}

@test "review prompts stream on stdin instead of consuming one argv entry" {
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR"
  cat > "$STUB_DIR/stream-codex" <<EOF
#!/usr/bin/env bash
last_message=""
printf '%s\n' "\$@" > "$BATS_TEST_TMPDIR/codex.args"
while [ "\$#" -gt 0 ]; do
  case "\$1" in
    -o|--output-last-message)
      last_message="\$2"
      shift 2
      ;;
    *) shift ;;
  esac
done
cat > "$BATS_TEST_TMPDIR/codex.stdin"
printf 'VERDICT: PASS\n' > "\$last_message"
EOF
  cat > "$STUB_DIR/stream-claude" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$BATS_TEST_TMPDIR/claude.args"
cat > "$BATS_TEST_TMPDIR/claude.stdin"
printf 'VERDICT: PASS\n'
EOF
  chmod +x "$STUB_DIR/stream-codex" "$STUB_DIR/stream-claude"
  prompt_file="$BATS_TEST_TMPDIR/large-review-prompt.txt"
  dd if=/dev/zero bs=1200000 count=1 2>/dev/null | tr '\0' 'p' > "$prompt_file"
  printf '\nPROMPT_ONLY_ON_STDIN_ffs_47' >> "$prompt_file"

  run env ADVERSARY_BIN_CODEX=stream-codex PATH="$STUB_DIR:$PATH" \
    bash -c '. "$1"; prompt="$(cat "$2")"; adversary_invoke codex 10 sol xhigh "$prompt"' \
    _ "$LIB" "$prompt_file"
  [ "$status" -eq 0 ]
  [ "$output" = "VERDICT: PASS" ]
  cmp -s "$prompt_file" "$BATS_TEST_TMPDIR/codex.stdin"
  codex_args_count="$(grep -Fc -- 'PROMPT_ONLY_ON_STDIN_ffs_47' "$BATS_TEST_TMPDIR/codex.args" || true)"
  [ "$codex_args_count" -eq 0 ]

  run env ADVERSARY_BIN_CLAUDE=stream-claude PATH="$STUB_DIR:$PATH" \
    bash -c '. "$1"; prompt="$(cat "$2")"; adversary_invoke claude 10 opus "" "$prompt"' \
    _ "$LIB" "$prompt_file"
  [ "$status" -eq 0 ]
  [ "$output" = "VERDICT: PASS" ]
  cmp -s "$prompt_file" "$BATS_TEST_TMPDIR/claude.stdin"
  ! grep -Fq 'PROMPT_ONLY_ON_STDIN_ffs_47' "$BATS_TEST_TMPDIR/claude.args"
}

@test "forensic opt-out returns the preferred failure without starting fallback" {
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR"
  cat > "$STUB_DIR/preferred-fails" <<'EOF'
#!/usr/bin/env bash
exit 127
EOF
  cat > "$STUB_DIR/fallback-must-not-start" <<EOF
#!/usr/bin/env bash
touch "$BATS_TEST_TMPDIR/fallback-started"
printf 'VERDICT: PASS\n'
EOF
  chmod +x "$STUB_DIR/preferred-fails" "$STUB_DIR/fallback-must-not-start"

  run -127 env FFS_CROSS_VENDOR_FALLBACK=off \
    ADVERSARY_BIN_CODEX=preferred-fails \
    ADVERSARY_BIN_CLAUDE=fallback-must-not-start PATH="$STUB_DIR:$PATH" \
    bash -c ". '$LIB'; adversary_invoke_with_fallback codex claude 10 sol xhigh opus '' review"

  [ "$status" -eq 127 ]
  [ ! -f "$BATS_TEST_TMPDIR/fallback-started" ]
}

# BASH_SOURCE is bash-only: under zsh ${BASH_SOURCE[0]} is empty, so
# `dirname ""` yields "." and the sibling source resolved to ./run-bounded.sh
# -- which does not exist at the repo root. run_bounded then stayed undefined
# and every bounded reviewer call failed. Measured under zsh before the fix:
#   scripts/gsd/adversary-host.sh:.:20: no such file or directory: ./run-bounded.sh
@test "sources under zsh from an unrelated cwd (run_bounded defined)" {
  command -v zsh >/dev/null 2>&1 || skip "zsh not installed"
  cd "$BATS_TEST_TMPDIR"
  run zsh -c ". '$LIB' && typeset -f run_bounded >/dev/null && echo DEFINED"
  [ "$status" -eq 0 ]
  [ "$output" = "DEFINED" ]
}

@test "sources under bash from an unrelated cwd (run_bounded defined)" {
  cd "$BATS_TEST_TMPDIR"
  run bash -c ". '$LIB' && type run_bounded >/dev/null && echo DEFINED"
  [ "$status" -eq 0 ]
  [ "$output" = "DEFINED" ]
}

@test "a missing sibling run-bounded.sh fails loudly, not silently" {
  ISOLATED="$BATS_TEST_TMPDIR/isolated"
  mkdir -p "$ISOLATED"
  cp "$LIB" "$ISOLATED/adversary-host.sh"   # deliberately WITHOUT run-bounded.sh
  run bash -c ". '$ISOLATED/adversary-host.sh'"
  [ "$status" -ne 0 ]
  [[ "$output" == *"cannot locate run-bounded.sh"* ]]
}

# ── spec-004 AC-016: ordered-rungs + schema validation extension ────────────

@test "ordered-rungs overrides the built-in ladder (terra first, not sol)" {
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR"
  cat > "$STUB_DIR/ordered-codex" <<EOF
#!/usr/bin/env bash
model=""
last_message=""
while [ "\$#" -gt 0 ]; do
  case "\$1" in
    -c) case "\$2" in model=*) model="\$2" ;; esac; shift 2 ;;
    -o|--output-last-message) last_message="\$2"; shift 2 ;;
    *) shift ;;
  esac
done
printf '%s\n' "\$model" >> "$BATS_TEST_TMPDIR/tried.log"
case "\$model" in
  *terra*) exit 1 ;;
  *luna*) printf 'VERDICT: PASS\n' > "\$last_message" ;;
esac
EOF
  chmod +x "$STUB_DIR/ordered-codex"

  run env FFS_ADVERSARY_MODEL_PROBE=off ADVERSARY_BIN_CODEX=ordered-codex \
    PATH="$STUB_DIR:$PATH" bash -c \
    ". '$LIB'; adversary_invoke_model_ladder codex 10 gpt-5.6-sol xhigh review 5 5 \$'gpt-5.6-terra|medium\ngpt-5.6-luna|low'"

  [ "$status" -eq 0 ]
  [[ "$output" == *"VERDICT: PASS"* ]]
  [[ "$output" == *"SELECTED codex gpt-5.6-luna"* ]]
  tried="$(cat "$BATS_TEST_TMPDIR/tried.log")"
  # terra tried before luna; sol (the preferred/producer model) never tried —
  # ordered-rungs REPLACES the built-in ladder entirely.
  first_line="$(echo "$tried" | head -1)"
  [[ "$first_line" == *terra* ]]
  ! echo "$tried" | grep -q 'model="gpt-5.6-sol"'
}

@test "schema validation: rc=0 output failing the validator is a rung failure, ladder continues" {
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR"
  cat > "$STUB_DIR/schema-codex" <<EOF
#!/usr/bin/env bash
model=""
last_message=""
while [ "\$#" -gt 0 ]; do
  case "\$1" in
    -c) case "\$2" in model=*) model="\$2" ;; esac; shift 2 ;;
    -o|--output-last-message) last_message="\$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "\$model" in
  *terra*) printf 'NOT VALID JSON\n' > "\$last_message" ;;
  *luna*) printf '[]\n' > "\$last_message" ;;
esac
EOF
  chmod +x "$STUB_DIR/schema-codex"
  cat > "$STUB_DIR/reject-non-array" <<'EOF'
#!/usr/bin/env bash
# $1 = schema file (ignored, structural check only). stdin = candidate JSON.
out="$(cat)"
printf '%s' "$out" | head -c1 | grep -q '\[' && exit 0
exit 1
EOF
  chmod +x "$STUB_DIR/reject-non-array"

  run env FFS_ADVERSARY_MODEL_PROBE=off ADVERSARY_BIN_CODEX=schema-codex \
    PATH="$STUB_DIR:$PATH" bash -c \
    ". '$LIB'; adversary_invoke_model_ladder codex 10 gpt-5.6-sol xhigh review 5 5 \
      \$'gpt-5.6-terra|medium\ngpt-5.6-luna|low' /dev/null reject-non-array"

  [ "$status" -eq 0 ]
  [ "$output" = "adversary-host: MODEL_FALLBACK — codex gpt-5.6-sol unavailable; selected gpt-5.6-luna
adversary-host: SELECTED codex gpt-5.6-luna low
[]" ] || [[ "$output" == *"SELECTED codex gpt-5.6-luna"* ]]
  [[ "$output" == *"[]"* ]]
}

@test "schema args absent: byte-compatible with pre-AC-016 built-in ladder" {
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR"
  cat > "$STUB_DIR/plain-codex" <<EOF
#!/usr/bin/env bash
last_message=""
while [ "\$#" -gt 0 ]; do
  case "\$1" in
    -o|--output-last-message) last_message="\$2"; shift 2 ;;
    *) shift ;;
  esac
done
printf 'VERDICT: PASS\n' > "\$last_message"
EOF
  chmod +x "$STUB_DIR/plain-codex"
  run env FFS_ADVERSARY_MODEL_PROBE=off ADVERSARY_BIN_CODEX=plain-codex \
    PATH="$STUB_DIR:$PATH" bash -c \
    ". '$LIB'; adversary_invoke_model_ladder codex 10 gpt-5.6-sol xhigh review"
  [ "$status" -eq 0 ]
  [[ "$output" == *"SELECTED codex gpt-5.6-sol"* ]]
}

@test "--output-schema is passed to codex exec when a schema file is given" {
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR"
  cat > "$STUB_DIR/capture-args-codex" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$BATS_TEST_TMPDIR/argv.log"
last_message=""
while [ "\$#" -gt 0 ]; do
  case "\$1" in
    -o|--output-last-message) last_message="\$2"; shift 2 ;;
    *) shift ;;
  esac
done
printf '[]\n' > "\$last_message"
EOF
  chmod +x "$STUB_DIR/capture-args-codex"
  SCHEMA="$BATS_TEST_TMPDIR/finding.schema.json"
  echo '{}' > "$SCHEMA"

  run env ADVERSARY_BIN_CODEX=capture-args-codex PATH="$STUB_DIR:$PATH" \
    bash -c ". '$LIB'; adversary_invoke codex 10 sol xhigh review '$SCHEMA'"

  [ "$status" -eq 0 ]
  grep -Fqx -- '--output-schema' "$BATS_TEST_TMPDIR/argv.log"
  grep -Fqx -- "$SCHEMA" "$BATS_TEST_TMPDIR/argv.log"
}

@test "adversary_invoke without schema_file omits --output-schema (backward-compat)" {
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR"
  cat > "$STUB_DIR/capture-args-codex" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$BATS_TEST_TMPDIR/argv.log"
last_message=""
while [ "\$#" -gt 0 ]; do
  case "\$1" in
    -o|--output-last-message) last_message="\$2"; shift 2 ;;
    *) shift ;;
  esac
done
printf '[]\n' > "\$last_message"
EOF
  chmod +x "$STUB_DIR/capture-args-codex"

  run env ADVERSARY_BIN_CODEX=capture-args-codex PATH="$STUB_DIR:$PATH" \
    bash -c ". '$LIB'; adversary_invoke codex 10 sol xhigh review"

  [ "$status" -eq 0 ]
  ! grep -Fqx -- '--output-schema' "$BATS_TEST_TMPDIR/argv.log"
}

@test "preferred (cross-vendor) rung defaults are never starved below the fallback rung" {
  # The independent, opposite-vendor reviewer's per-attempt/per-review timeout
  # floors must not be starved below the same-vendor fallback's — otherwise a
  # loaded box silently degrades "cross-vendor review" into self-review by
  # timing out the honest reviewer before the dishonest fallback. Observed at
  # the WIRING level: override the ladder and read the caps
  # adversary_invoke_with_fallback actually hands the preferred rung, with no
  # FFS_ADVERSARY_PREFERRED_* set. A source grep would pass even if the
  # defaults were computed and then dropped on the way to the rung.
  # BOTH rungs are observed in one run: the stub fails the first (preferred)
  # invocation so the fallback rung actually executes, then succeeds. Reading
  # the fallback's caps from a source grep instead would keep passing if the
  # two arguments were swapped on the way to the call.
  log="$BATS_TEST_TMPDIR/ladder.log"
  run env -u FFS_ADVERSARY_PREFERRED_ATTEMPT_TIMEOUT \
          -u FFS_ADVERSARY_PREFERRED_REVIEW_TIMEOUT \
          -u FFS_ADVERSARY_FALLBACK_ATTEMPT_TIMEOUT \
          -u FFS_ADVERSARY_FALLBACK_REVIEW_TIMEOUT \
          LADDER_LOG="$log" \
    bash -c '
      . "$1"
      adversary_invoke_model_ladder() {
        printf "%s attempt=%s review=%s\n" "$1" "${6:-unset}" "${7:-unset}" >> "$LADDER_LOG"
        [ "$(wc -l < "$LADDER_LOG")" -gt 1 ] || return 7   # fail the preferred rung only
        printf "REVIEW-OK\n"
        return 0
      }
      adversary_invoke_with_fallback claude codex 600 opus "" gpt-5.6-sol high review
    ' _ "$LIB"

  [ "$status" -eq 0 ]
  [[ "$output" == *REVIEW-OK* ]]
  # both rungs ran, preferred first
  [ "$(wc -l < "$log" | tr -d ' ')" -eq 2 ]
  [ "$(sed -n 1p "$log")" = "claude attempt=120 review=480" ]
  [ "$(sed -n 2p "$log")" = "codex attempt=120 review=240" ]
  # Assert the INVARIANT, not the literals: whatever the two rungs are tuned
  # to, the independent reviewer must never get less than the self-review
  # fallback. Both sides come from the observed wiring.
  pref_attempt="$(sed -n '1s/.*attempt=\([0-9]*\).*/\1/p' "$log")"
  pref_review="$(sed -n '1s/.*review=\([0-9]*\).*/\1/p' "$log")"
  fb_attempt="$(sed -n '2s/.*attempt=\([0-9]*\).*/\1/p' "$log")"
  fb_review="$(sed -n '2s/.*review=\([0-9]*\).*/\1/p' "$log")"
  [ -n "$fb_attempt" ] && [ -n "$fb_review" ]
  [ "$pref_attempt" -ge "$fb_attempt" ]
  [ "$pref_review" -ge "$fb_review" ]

  # Host symmetry: FFS runs on Codex as well as Claude, so the same caps must
  # reach the preferred rung when the orchestrating host is Claude (preferred
  # = codex). The caps are keyed on preferred-vs-fallback POSITION, never on
  # vendor identity — a vendor-keyed regression would starve Codex-hosted runs
  # only, and would be invisible to the Claude-direction assertion above.
  log2="$BATS_TEST_TMPDIR/ladder-codex.log"
  run env -u FFS_ADVERSARY_PREFERRED_ATTEMPT_TIMEOUT \
          -u FFS_ADVERSARY_PREFERRED_REVIEW_TIMEOUT \
          LADDER_LOG="$log2" \
    bash -c '
      . "$1"
      adversary_invoke_model_ladder() {
        printf "%s attempt=%s review=%s\n" "$1" "${6:-unset}" "${7:-unset}" >> "$LADDER_LOG"
        printf "REVIEW-OK\n"
        return 0
      }
      adversary_invoke_with_fallback codex claude 600 gpt-5.6-sol high opus "" review
    ' _ "$LIB"

  [ "$status" -eq 0 ]
  [ "$(cat "$log2")" = "codex attempt=$pref_attempt review=$pref_review" ]
}

@test "an env-override that starves the preferred rung below the fallback is announced" {
  # Overrides stay authoritative — an operator may deliberately starve a rung —
  # but the inversion must not be silent: from the outside a starved preferred
  # rung is indistinguishable from a healthy cross-vendor review that simply
  # fell back, which is exactly how the original inversion survived so long.
  run env FFS_ADVERSARY_PREFERRED_REVIEW_TIMEOUT=1 \
          FFS_ADVERSARY_FALLBACK_REVIEW_TIMEOUT=240 \
          LADDER_LOG="$BATS_TEST_TMPDIR/inv.log" \
    bash -c '
      . "$1"
      adversary_invoke_model_ladder() {
        printf "%s attempt=%s review=%s\n" "$1" "${6:-unset}" "${7:-unset}" >> "$LADDER_LOG"
        printf "REVIEW-OK\n"
        return 0
      }
      adversary_invoke_with_fallback claude codex 600 opus "" gpt-5.6-sol high review
    ' _ "$LIB"

  [ "$status" -eq 0 ]
  [[ "$output" == *"WARN"*"BELOW"* ]]
  # the override is still honoured, not clamped
  [ "$(cat "$BATS_TEST_TMPDIR/inv.log")" = "claude attempt=120 review=1" ]
}

@test "a within-vendor tier descent is recorded as DEGRADED, not as a clean review" {
  # A judgment-tier request resolves to sol high. When sol fails the ladder
  # descends to terra medium on the SAME vendor and returns rc=0, so the
  # caller's record call ran with degraded=false: the run's evidence claimed
  # judgment-tier adversarial review while an execution-tier model wrote the
  # verdict, and degraded_ratio() — the REQ-102 gate's input — never saw it.
  rec="$BATS_TEST_TMPDIR/record.log"
  run env ADVERSARY_RECORD_LOG="$rec" \
    bash -c '
      . "$1"
      # descend: fail the requested rung, succeed on the next
      adversary_invoke() {
        case "$3" in
          gpt-5.6-sol) return 124 ;;
          *) printf "FFS_ADVERSARY_MODEL_READY\n"; printf "VERDICT-BODY\n"; return 0 ;;
        esac
      }
      adversary_note_rung() { return 0; }
      adversary_record_invocation() { printf "degraded=%s\n" "$1" >> "$ADVERSARY_RECORD_LOG"; return 0; }
      adversary_invoke_with_fallback codex claude 600 gpt-5.6-sol high claude-opus-5 "" review
    ' _ "$LIB"

  [ "$status" -eq 0 ]
  [[ "$output" == *"SELECTED codex gpt-5.6-terra"* ]]
  [ "$(cat "$rec")" = "degraded=true" ]
}

@test "a tier descent emits a typed, caller-gateable signal naming requested and answered models" {
  run bash -c '
    . "$1"
    adversary_invoke() {
      case "$3" in
        gpt-5.6-sol) return 124 ;;
        *) printf "FFS_ADVERSARY_MODEL_READY\n"; printf "VERDICT-BODY\n"; return 0 ;;
      esac
    }
    adversary_note_rung() { return 0; }
    adversary_record_invocation() { return 0; }
    adversary_invoke_model_ladder codex 600 gpt-5.6-sol high review >/dev/null
    printf "flag=%s\n" "${ADVERSARY_LAST_TIER_DESCENT:-unset}"
  ' _ "$LIB" 2>&1

  [ "$status" -eq 0 ]
  [[ "$output" == *"TIER-DESCENT"*"requested=gpt-5.6-sol:high"*"answered=gpt-5.6-terra:medium"* ]]
  # in-process callers gate on the variable, not on parsing stdout
  [[ "$output" == *"flag=1"* ]]
}

@test "a rung that answers as requested leaves the descent flag clear" {
  run bash -c '
    . "$1"
    adversary_invoke() { printf "VERDICT-BODY\n"; return 0; }
    adversary_note_rung() { return 0; }
    adversary_record_invocation() { return 0; }
    ADVERSARY_LAST_TIER_DESCENT=1   # stale value from an earlier call must be cleared
    FFS_ADVERSARY_MODEL_PROBE=off \
      adversary_invoke_model_ladder codex 600 gpt-5.6-sol high review >/dev/null
    printf "flag=%s\n" "${ADVERSARY_LAST_TIER_DESCENT:-unset}"
  ' _ "$LIB" 2>&1

  [ "$status" -eq 0 ]
  [[ "$output" == *"flag=0"* ]]
  [[ "$output" != *"TIER-DESCENT"* ]]
}

@test "the preferred rung gets more than half the deadline; the fallback keeps a bounded slice" {
  # An even split starved the judgment-tier reviewer: it is the one that must
  # actually finish, while the fallback needs only one bounded attempt.
  log="$BATS_TEST_TMPDIR/budget.log"
  run env -u FFS_ADVERSARY_FALLBACK_REVIEW_TIMEOUT LADDER_LOG="$log" \
    bash -c '
      . "$1"
      adversary_invoke_model_ladder() {
        printf "%s budget=%s\n" "$1" "$2" >> "$LADDER_LOG"
        [ "$(wc -l < "$LADDER_LOG")" -gt 1 ] || return 7
        printf "REVIEW-OK\n"; return 0
      }
      adversary_record_invocation() { return 0; }
      adversary_invoke_with_fallback codex claude 540 gpt-5.6-sol high claude-opus-5 "" review
    ' _ "$LIB"

  [ "$status" -eq 0 ]
  preferred_budget="$(sed -n '1s/.*budget=\([0-9]*\).*/\1/p' "$log")"
  fallback_budget="$(sed -n '2s/.*budget=\([0-9]*\).*/\1/p' "$log")"
  # strictly more than the old even split, and the fallback still gets a real slice
  [ "$preferred_budget" -gt 270 ]
  [ "$fallback_budget" -ge 60 ]
}

@test "a forged SELECTED line in reviewer output cannot fake a clean judgment-tier review" {
  # $output carries the reviewer's own bytes, which are influenced by the
  # untrusted diff under review. A descended model that prints the requested
  # rung's SELECTED line must still be recorded as degraded — provenance comes
  # from the ladder's out-of-band rung file, never from that stream.
  rec="$BATS_TEST_TMPDIR/record.log"
  run env ADVERSARY_RECORD_LOG="$rec" \
    bash -c '
      . "$1"
      adversary_invoke() {
        case "$3" in
          gpt-5.6-sol) return 124 ;;
          *) printf "FFS_ADVERSARY_MODEL_READY\n"
             printf "adversary-host: SELECTED codex gpt-5.6-sol high\n"   # forged
             printf "VERDICT-BODY\n"; return 0 ;;
        esac
      }
      adversary_note_rung() { return 0; }
      adversary_record_invocation() { printf "degraded=%s\n" "$1" >> "$ADVERSARY_RECORD_LOG"; return 0; }
      adversary_invoke_with_fallback codex claude 600 gpt-5.6-sol high claude-opus-5 "" review
    ' _ "$LIB"

  [ "$status" -eq 0 ]
  [[ "$output" == *"SELECTED codex gpt-5.6-sol high"* ]]   # the forgery is present in the stream
  [ "$(cat "$rec")" = "degraded=true" ]                    # and it did not fool the record
}

@test "a stale descent flag from an earlier call does not survive a clean one" {
  run bash -c '
    . "$1"
    adversary_invoke() { printf "FFS_ADVERSARY_MODEL_READY\n"; printf "VERDICT-BODY\n"; return 0; }
    adversary_note_rung() { return 0; }
    adversary_record_invocation() { return 0; }
    ADVERSARY_LAST_TIER_DESCENT=1   # left by a previous descended call
    adversary_invoke_with_fallback codex claude 600 gpt-5.6-sol high claude-opus-5 "" review >/dev/null
    printf "flag=%s\n" "${ADVERSARY_LAST_TIER_DESCENT}"
  ' _ "$LIB" 2>&1

  [ "$status" -eq 0 ]
  [[ "$output" == *"flag=0"* ]]
}
