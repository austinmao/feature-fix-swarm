#!/usr/bin/env bats
# waiver-record.sh — AC-003 five-seam kill-switch matrix: every switch that
# bypasses a gate (CANARY_GATE=off, CANARY_GATE_ALLOW_STALE=1,
# PLAN_ADVERSARY=off, QA_COVERAGE=off, CREDENTIAL_OUTPUT_GUARD=off) must
# write a durable waiver row BEFORE the gate takes its skip, and must refuse
# the skip entirely when the recorder cannot persist. Every gate script
# resolves scripts/gsd/waiver-record.sh via its OWN $BASH_SOURCE dirname —
# script-relative, not cwd- or GATES_STORE-relative — so isolation requires
# running a fixture-local COPY of the gate script, never the real repo's
# checkout (see WR-140).

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  FIXTURE="$BATS_TEST_TMPDIR/waiver-fixture"
  mkdir -p "$FIXTURE/lib" "$FIXTURE/scripts"
  cp "$ROOT/lib/gates.py" "$FIXTURE/lib/gates.py"
  cp -r "$ROOT/scripts/gsd" "$FIXTURE/scripts/gsd"
  cp -r "$ROOT/scripts/hooks" "$FIXTURE/scripts/hooks"
  chmod +x "$FIXTURE"/scripts/gsd/*.sh "$FIXTURE"/scripts/hooks/*.sh 2>/dev/null || true
  git -C "$FIXTURE" init -q -b main
  git -C "$FIXTURE" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
}

teardown() {
  # Fail-closed cases lock $FIXTURE/.feature-fix-swarm to chmod 500; restore
  # write permission so bats can clean up BATS_TEST_TMPDIR afterward.
  [ -d "$FIXTURE/.feature-fix-swarm" ] && chmod -R u+rwx "$FIXTURE/.feature-fix-swarm" 2>/dev/null
  return 0
}

store_path() {
  printf '%s\n' "$FIXTURE/.feature-fix-swarm/evidence.json"
}

# assert_waiver_row <gate> <env_var> — exactly one durable row for this seam.
assert_waiver_row() {
  run python3 -c '
import json, sys
gate, env_var, path = sys.argv[1], sys.argv[2], sys.argv[3]
data = json.load(open(path))
rows = [w for w in data["waivers"] if w["gate"] == gate and w["env_var"] == env_var]
assert len(rows) == 1, rows
' "$1" "$2" "$(store_path)"
  [ "$status" -eq 0 ]
}

# assert_no_waiver_rows — the fail-closed cases must leave the store either
# absent or, if present, with zero rows added.
assert_no_waiver_rows() {
  local store
  store="$(store_path)"
  if [ ! -f "$store" ]; then
    return 0
  fi
  run python3 -c '
import json, sys
data = json.load(open(sys.argv[1]))
assert len(data.get("waivers", [])) == 0, data["waivers"]
' "$store"
  [ "$status" -eq 0 ]
}

# lock_store / unlock_store — force the recorder's write to fail. Root is
# never blocked by a chmod 500 directory, so fail-closed cases skip there.
lock_store() {
  if [ "$(id -u)" -eq 0 ]; then
    skip "chmod 500 does not block root — cannot force a recorder write failure"
  fi
  mkdir -p "$FIXTURE/.feature-fix-swarm"
  chmod 500 "$FIXTURE/.feature-fix-swarm"
}

unlock_store() {
  chmod 755 "$FIXTURE/.feature-fix-swarm" 2>/dev/null || true
}

add_web_commit() {
  mkdir -p "$FIXTURE/web/app"
  echo "export default function Page(){}" > "$FIXTURE/web/app/page.tsx"
  git -C "$FIXTURE" add web/app/page.tsx
  git -C "$FIXTURE" -c user.email=t@t -c user.name=t commit -q -m "web change"
}

write_passing_results() { # $1=path
  # createdAt/endedAt mirror the real @usecanary/cli results.json shape —
  # phase-3's typed evidence recorder fail-closes on their absence.
  cat > "$1" <<'EOF'
{"status":"passed","createdAt":"2026-06-13T08:43:44.814Z","endedAt":"2026-06-13T08:48:02.991Z","summary":{"stepsTotal":5,"stepsPassed":5,"stepsFailed":0,"consoleErrors":0,"networkFailures":0},"steps":[]}
EOF
}

@test "WR-001: shared recorder writes unattributed row in fixture canonical store" {
  run env -u GSD_RUN_ID "$FIXTURE/scripts/gsd/waiver-record.sh" canary-gate CANARY_GATE=off
  [ "$status" -eq 0 ]
  run python3 -c 'import json,sys; r=json.load(open(sys.argv[1]))["waivers"][0]; assert r["run_id"] == "unattributed"; assert r["gate"] == "canary-gate"' "$FIXTURE/.feature-fix-swarm/evidence.json"
  [ "$status" -eq 0 ]
}

@test "WR-002: concurrent recorders preserve distinct durable rows" {
  "$FIXTURE/scripts/gsd/waiver-record.sh" canary-gate CANARY_GATE=off &
  first=$!
  "$FIXTURE/scripts/gsd/waiver-record.sh" qa-coverage-adversary QA_COVERAGE=off &
  second=$!
  wait "$first"
  wait "$second"
  run python3 -c 'import json,sys; assert len(json.load(open(sys.argv[1]))["waivers"]) == 2' "$FIXTURE/.feature-fix-swarm/evidence.json"
  [ "$status" -eq 0 ]
}

# ── WR-101..105: success — durable row AND the gate's own skip output/exit ──

@test "WR-101: canary-gate.sh CANARY_GATE=off records a waiver row and takes the skip" {
  run env -u GSD_RUN_ID CANARY_GATE=off bash "$FIXTURE/scripts/gsd/canary-gate.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"canary-gate: disabled (CANARY_GATE=off)"* ]]
  assert_waiver_row canary-gate CANARY_GATE=off
}

@test "WR-102: canary-gate.sh CANARY_GATE_ALLOW_STALE=1 records a waiver row and bypasses a genuinely stale check" {
  add_web_commit
  local base_sha results
  base_sha="$(git -C "$FIXTURE" rev-parse HEAD~1)"
  results="$BATS_TEST_TMPDIR/wr102-results.json"
  write_passing_results "$results"
  # mtime predates HEAD's commit time, so the recorded branch is a real
  # bypass of a real staleness failure, not a fresh-result no-op.
  touch -t 202001010000 "$results"
  # canary-gate.sh's git calls (diff-base resolution, WEB-TOUCH diff) run
  # against cwd's repo, not a script-relative one — cd into the fixture repo.
  cd "$FIXTURE"
  run env -u GSD_RUN_ID CANARY_GATE_ALLOW_STALE=1 \
    bash "$FIXTURE/scripts/gsd/canary-gate.sh" --diff-base "$base_sha" "$results"
  [ "$status" -eq 0 ]
  [[ "$output" == *"canary-gate: PASS"* ]]
  assert_waiver_row canary-gate CANARY_GATE_ALLOW_STALE=1
}

@test "WR-103: plan-adversary.sh PLAN_ADVERSARY=off records a waiver row and takes the skip" {
  local plan="$BATS_TEST_TMPDIR/wr103-PLAN.md"
  echo "# plan" > "$plan"
  run env -u GSD_RUN_ID PLAN_ADVERSARY=off bash "$FIXTURE/scripts/gsd/plan-adversary.sh" "$plan"
  [ "$status" -eq 0 ]
  [[ "$output" == *"disabled"* ]]
  assert_waiver_row plan-adversary PLAN_ADVERSARY=off
}

@test "WR-104: qa-coverage-adversary.sh QA_COVERAGE=off records a waiver row and takes the skip" {
  run env -u GSD_RUN_ID QA_COVERAGE=off \
    bash "$FIXTURE/scripts/gsd/qa-coverage-adversary.sh" "$BATS_TEST_TMPDIR/wr104-results.json"
  [ "$status" -eq 0 ]
  [[ "$output" == *"disabled"* ]]
  assert_waiver_row qa-coverage-adversary QA_COVERAGE=off
}

@test "WR-105: credential-output-guard.sh CREDENTIAL_OUTPUT_GUARD=off records a waiver row and takes the skip" {
  run env -u GSD_RUN_ID CREDENTIAL_OUTPUT_GUARD=off \
    bash "$FIXTURE/scripts/hooks/credential-output-guard.sh" < /dev/null
  [ "$status" -eq 0 ]
  assert_waiver_row credential-output-guard CREDENTIAL_OUTPUT_GUARD=off
}

# ── WR-111..115: fail-closed — recorder cannot persist, so the gate returns
# its typed nonzero and never takes the skip ─────────────────────────────────

@test "WR-111: canary-gate.sh fails closed when the recorder cannot persist (CANARY_GATE=off)" {
  lock_store
  run env -u GSD_RUN_ID CANARY_GATE=off bash "$FIXTURE/scripts/gsd/canary-gate.sh"
  unlock_store
  [ "$status" -ne 0 ]
  [[ "$output" != *"canary-gate: disabled (CANARY_GATE=off)"* ]]
  [[ "$output" == *"WAIVER-UNRECORDED"* ]]
  assert_no_waiver_rows
}

@test "WR-112: canary-gate.sh fails closed when the recorder cannot persist (CANARY_GATE_ALLOW_STALE=1)" {
  add_web_commit
  local base_sha results
  base_sha="$(git -C "$FIXTURE" rev-parse HEAD~1)"
  results="$BATS_TEST_TMPDIR/wr112-results.json"
  write_passing_results "$results"
  touch -t 202001010000 "$results"
  cd "$FIXTURE"
  lock_store
  run env -u GSD_RUN_ID CANARY_GATE_ALLOW_STALE=1 \
    bash "$FIXTURE/scripts/gsd/canary-gate.sh" --diff-base "$base_sha" "$results"
  unlock_store
  [ "$status" -ne 0 ]
  [[ "$output" != *"canary-gate: PASS"* ]]
  [[ "$output" == *"WAIVER-UNRECORDED"* ]]
  assert_no_waiver_rows
}

@test "WR-113: plan-adversary.sh fails closed when the recorder cannot persist (PLAN_ADVERSARY=off)" {
  local plan="$BATS_TEST_TMPDIR/wr113-PLAN.md"
  echo "# plan" > "$plan"
  lock_store
  run env -u GSD_RUN_ID PLAN_ADVERSARY=off bash "$FIXTURE/scripts/gsd/plan-adversary.sh" "$plan"
  unlock_store
  [ "$status" -ne 0 ]
  [[ "$output" != *"disabled (PLAN_ADVERSARY=off)"* ]]
  [[ "$output" == *"WAIVER-UNRECORDED"* ]]
  assert_no_waiver_rows
}

@test "WR-114: qa-coverage-adversary.sh fails closed when the recorder cannot persist (QA_COVERAGE=off)" {
  lock_store
  run env -u GSD_RUN_ID QA_COVERAGE=off \
    bash "$FIXTURE/scripts/gsd/qa-coverage-adversary.sh" "$BATS_TEST_TMPDIR/wr114-results.json"
  unlock_store
  [ "$status" -ne 0 ]
  [[ "$output" != *"disabled (QA_COVERAGE=off)"* ]]
  [[ "$output" == *"WAIVER-UNRECORDED"* ]]
  assert_no_waiver_rows
}

@test "WR-115: credential-output-guard.sh fails closed with exit 2 exactly when the recorder cannot persist" {
  lock_store
  run env -u GSD_RUN_ID CREDENTIAL_OUTPUT_GUARD=off \
    bash "$FIXTURE/scripts/hooks/credential-output-guard.sh" < /dev/null
  unlock_store
  # Exactly 2 — this hook runs as a PreToolUse blocker where only exit 2
  # blocks the guarded call; any other nonzero would let it through.
  [ "$status" -eq 2 ]
  [[ "$output" == *"WAIVER-UNRECORDED"* ]]
  assert_no_waiver_rows
}

# ── WR-120: attribution fallback ─────────────────────────────────────────────

@test "WR-120: unset GSD_RUN_ID records the unattributed literal and still takes the skip" {
  run env -u GSD_RUN_ID CANARY_GATE=off bash "$FIXTURE/scripts/gsd/canary-gate.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"canary-gate: disabled (CANARY_GATE=off)"* ]]
  run python3 -c '
import json, sys
r = json.load(open(sys.argv[1]))["waivers"][0]
assert r["run_id"] == "unattributed", r["run_id"]
' "$(store_path)"
  [ "$status" -eq 0 ]
}

# ── WR-121: single-recorder gate ─────────────────────────────────────────────

@test "WR-121: every live gate reaches waiver-record.sh; delegation-enforcer.sh does not" {
  local f hits
  for f in canary-gate.sh plan-adversary.sh qa-coverage-adversary.sh plan-wall.sh; do
    hits="$(grep -v '^[[:space:]]*#' "$ROOT/scripts/gsd/$f" | grep -c 'waiver-record\.sh')"
    [ "$hits" -ge 1 ]
  done
  hits="$(grep -v '^[[:space:]]*#' "$ROOT/scripts/hooks/credential-output-guard.sh" | grep -c 'waiver-record\.sh')"
  [ "$hits" -ge 1 ]
  hits="$(grep -v '^[[:space:]]*#' "$ROOT/scripts/hooks/delegation-enforcer.sh" | grep -c 'waiver-record\.sh' || true)"
  [ "$hits" -eq 0 ]
}

# ── WR-130: plan-wall pre-migration byte-compatibility ──────────────────────

# build_plan_wall_env <scripts-source-root> <workdir> — materializes a
# git-repo fixture with .planning/phases/1-foo/PLAN.md, config.json, and a
# copy of scripts/gsd + lib/gates.py + schemas/review-finding.schema.json
# from <scripts-source-root>, so plan-wall.sh's own $SCRIPT_DIR-relative and
# $REPO_ROOT-relative resolution both stay inside the fixture.
build_plan_wall_env() {
  local src="$1" work="$2"
  mkdir -p "$work/lib" "$work/schemas" "$work/packages/feature-fix-swarm/lib" \
    "$work/.planning/phases/1-foo"
  cp "$src/lib/gates.py" "$work/lib/gates.py"
  cp "$src/lib/gates.py" "$work/packages/feature-fix-swarm/lib/gates.py"
  cp "$src/schemas/review-finding.schema.json" "$work/schemas/review-finding.schema.json"
  cp -r "$src/scripts" "$work/scripts"
  chmod +x "$work"/scripts/gsd/*.sh 2>/dev/null || true
  echo "Phase 1: build a plain widget, nothing sensitive here" > "$work/.planning/phases/1-foo/PLAN.md"
  cat > "$work/.planning/config.json" <<'JSON'
{"model_overrides": {"gsd-planner": "fable"}, "dynamic_routing": {"escalate_on_failure": true}}
JSON
  git -C "$work" init -q -b main
  git -C "$work" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
}

# run_plan_wall_waiver <workdir> — runs the PLAN_WALL=off path with a pinned
# RUN_ID (eliminating the date+pid fallback as a source of nondeterminism)
# and captures stdout/stderr/exit for later comparison.
run_plan_wall_waiver() {
  local work="$1"
  ( cd "$work" && GSD_RUN_ID=wr130-fixed PLAN_WALL=off \
      PLAN_WALL_REASON="deliberate skip for testing" \
      bash scripts/gsd/plan-wall.sh .planning/phases/1-foo \
      >"$work/STDOUT" 2>"$work/STDERR" )
  echo "$?" > "$work/EXITCODE"
}

# normalize_wr130 <file> — blank sha256-shaped and ISO-8601-timestamp-shaped
# substrings before the byte-compat diff, per the plan's normalization
# instruction (defensive: neither field is expected to actually differ here
# since RUN_ID is pinned and PLAN.md content is byte-identical).
normalize_wr130() {
  sed -E 's/"[0-9a-f]{64}"/"SHA256"/g; s/[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]+Z/TIMESTAMP/g' "$1"
}

@test "WR-130: plan-wall's successful PLAN_WALL=off waiver path is byte-identical to the pre-migration baseline" {
  local pw_first old_extract old_work new_work
  # The byte-compat comparison needs real history back to the pinned
  # pre-migration anchor; a shallow CI clone (fetch-depth 1) cannot resolve
  # it — skip rather than fail on an environment that lacks the baseline
  # (first hit: PR #103 CI, `fatal: bad revision`).
  git -C "$ROOT" rev-parse -q --verify 'e3ec94b^{commit}' >/dev/null 2>&1 \
    || skip "baseline commit e3ec94b unavailable (shallow clone)"
  pw_first="$(git -C "$ROOT" rev-list --reverse HEAD --not e3ec94b -- scripts/gsd/plan-wall.sh | head -1)"
  [ -n "$pw_first" ]

  old_extract="$BATS_TEST_TMPDIR/pw-old-extract"
  mkdir -p "$old_extract"
  local p
  for p in scripts lib schemas; do
    git -C "$ROOT" archive "${pw_first}^" -- "$p" 2>/dev/null | tar -x -C "$old_extract" 2>/dev/null || true
  done
  # Self-check: the baseline genuinely predates the waiver-record.sh
  # migration — a stale hardcoded sha would otherwise silently make this a
  # migrated-vs-migrated (vacuous) comparison.
  ! grep -q 'waiver-record\.sh' "$old_extract/scripts/gsd/plan-wall.sh"

  old_work="$BATS_TEST_TMPDIR/pw-old-work"
  new_work="$BATS_TEST_TMPDIR/pw-new-work"
  build_plan_wall_env "$old_extract" "$old_work"
  build_plan_wall_env "$ROOT" "$new_work"

  run_plan_wall_waiver "$old_work"
  run_plan_wall_waiver "$new_work"

  [ "$(cat "$old_work/EXITCODE")" -eq 0 ]
  [ "$(cat "$new_work/EXITCODE")" -eq 0 ]
  [ "$(cat "$old_work/EXITCODE")" = "$(cat "$new_work/EXITCODE")" ]
  [[ "$(cat "$old_work/STDOUT")" == *WAIVED* ]]
  [[ "$(cat "$new_work/STDOUT")" == *WAIVED* ]]

  diff <(normalize_wr130 "$old_work/STDOUT") <(normalize_wr130 "$new_work/STDOUT")
  diff <(normalize_wr130 "$old_work/.planning/run-state/plan-wall-1-foo-plan.json") \
       <(normalize_wr130 "$new_work/.planning/run-state/plan-wall-1-foo-plan.json")
}

# ── WR-140: isolation gate ───────────────────────────────────────────────────

setup_file() {
  local root store
  root="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  store="$root/.feature-fix-swarm/evidence.json"
  if [ -f "$store" ]; then
    WR140_BEFORE="$(shasum -a 256 "$store" | awk '{print $1}')"
  else
    WR140_BEFORE="ABSENT"
  fi
  export WR140_BEFORE
  export WR140_STORE_PATH="$store"
}

@test "WR-140: this file's own real canonical evidence store is unchanged by the recorder-reaching cases above" {
  local after
  if [ -f "$WR140_STORE_PATH" ]; then
    after="$(shasum -a 256 "$WR140_STORE_PATH" | awk '{print $1}')"
  else
    after="ABSENT"
  fi
  [ "$after" = "$WR140_BEFORE" ]
}
