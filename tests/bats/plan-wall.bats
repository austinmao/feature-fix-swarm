#!/usr/bin/env bats
# plan-wall.sh — spec-004 AC-005/006/007/008 per-phase blocking plan review
# wall. PATH-shim journeys (spec-003 precedent, no browser surface): vendor
# CLIs are stubbed via ADVERSARY_BIN_CLAUDE/ADVERSARY_BIN_CODEX + FFS_HOST;
# the lever under test (plan-wall.sh) is never mocked.

bats_require_minimum_version 1.5.0

setup() {
  LEVER="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/scripts/gsd/plan-wall.sh"
  FENCE_LEVER="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/scripts/gsd/security-model-fence.sh"
  REAL_GATES_PY="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/lib/gates.py"
  REAL_SCHEMA="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/schemas/review-finding.schema.json"
  REPO="$BATS_TEST_TMPDIR/repo"
  # highest-priority GATES_PY candidate — guarantees the fixture's own (v2)
  # gates.py wins over any machine-global FFS install, on any test runner.
  mkdir -p "$REPO/packages/feature-fix-swarm/lib" "$REPO/schemas" "$REPO/bin"
  cp "$REAL_GATES_PY" "$REPO/packages/feature-fix-swarm/lib/gates.py"
  cp "$REAL_SCHEMA" "$REPO/schemas/review-finding.schema.json"
  cd "$REPO"
  git init -q -b main
  git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
  mkdir -p .planning/phases/1-foo
  write_config fable
  echo "Phase 1: build a plain widget, nothing sensitive here" > .planning/phases/1-foo/PLAN.md
  export PATH="$REPO/bin:$PATH"
  export FFS_ADVERSARY_MODEL_PROBE=off
  export GATES_PY="$REPO/packages/feature-fix-swarm/lib/gates.py"
  # Default the OTHER vendor's binary to a name that resolves nowhere, so a
  # claude-only test's rule-1 opposite-vendor ladder fails FAST (rc=127)
  # instead of shelling out to a REAL codex/claude CLI that may be installed
  # on the machine running these tests. Tests that intentionally exercise
  # both vendors (e.g. PATH-016) override this per-invocation.
  export ADVERSARY_BIN_CODEX=nonexistent-codex-binary-xyz
  export ADVERSARY_BIN_CLAUDE=nonexistent-claude-binary-xyz
}

write_config() {
  cat > .planning/config.json <<JSON
{"model_overrides": {"gsd-planner": "$1"}, "dynamic_routing": {"escalate_on_failure": true}}
JSON
}

record_for() {
  # record_for <phase-slug> <plan-slug>
  echo ".planning/run-state/plan-wall-$1-$2.json"
}

stub_claude_json() {
  # stub_claude_json <output-json>
  cat > bin/stub-claude <<EOF
#!/usr/bin/env bash
cat >/dev/null
printf '%s\n' '$1'
EOF
  chmod +x bin/stub-claude
}

stub_fail() {
  # a bin that always fails (unavailable CLI simulation)
  cat > "bin/$1" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
  chmod +x "bin/$1"
}

queue_list() {
  python3 "$GATES_PY" findings-queue list "$@"
}

# ── PATH-001: HIGH finding blocks; refute + reason unblocks re-run ──────────

@test "PATH-001: HIGH finding blocks, resolve --disposition refute unblocks re-run with zero dispatch" {
  stub_claude_json '[{"severity":"HIGH","file":"a.py","claim":"missing null check","line":3}]'
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 1 ]
  [[ "$output" == *"BLOCKED"* ]]
  record="$(record_for 1-foo plan)"
  [ "$(jq -r '.verdict' "$record")" = "blocked" ]

  sig="$(queue_list --unresolved --source wall --severity HIGH,CRITICAL \
    --plan .planning/phases/1-foo/PLAN.md | jq -r '.[0].sig')"
  [ -n "$sig" ] && [ "$sig" != "null" ]
  python3 "$GATES_PY" findings-queue resolve "$sig" --disposition refute --reason "false positive"

  # reviewer must NOT be re-dispatched on the unchanged plan
  MARKER="$BATS_TEST_TMPDIR/should-not-run"
  cat > bin/stub-claude <<EOF
#!/usr/bin/env bash
touch "$MARKER"
cat >/dev/null
printf '[]\n'
EOF
  chmod +x bin/stub-claude
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 0 ]
  [[ "$output" == *"ADJUDICATED-PASS"* ]]
  [ ! -f "$MARKER" ]
  [ "$(jq -r '.verdict' "$record")" = "adjudicated-pass" ]
}

# ── PATH-002: MEDIUM/LOW-only findings advisory, exit 0 ─────────────────────

@test "PATH-002: MEDIUM/LOW-only findings are advisory — exit 0, findings still recorded" {
  stub_claude_json '[{"severity":"MEDIUM","file":"b.py","claim":"minor style issue"},{"severity":"LOW","file":"c.py","claim":"nit"}]'
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 0 ]
  [[ "$output" == *"REVIEWED-PASS"* ]]
  all="$(queue_list --source wall --plan .planning/phases/1-foo/PLAN.md)"
  [ "$(printf '%s' "$all" | jq 'length')" = "2" ]
}

# ── PATH-015: zero-findings [] is a clean pass, not a rung failure ──────────

@test "PATH-015: reviewer returns [] -> reviewed-pass, not a rung failure" {
  stub_claude_json '[]'
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 0 ]
  record="$(record_for 1-foo plan)"
  [ "$(jq -r '.verdict' "$record")" = "reviewed-pass" ]
}

# ── PATH-003: claude-only -> same-vendor relation ────────────────────────────

@test "PATH-003: claude-only shim, planner fable -> opus reviews, relation same-vendor" {
  write_config fable
  stub_claude_json '[]'
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 0 ]
  record="$(record_for 1-foo plan)"
  [ "$(jq -r '.relation' "$record")" = "same-vendor" ]
  [ "$(jq -r '.reviewer_model' "$record")" = "claude-opus-5" ]
  [ "$(jq -r '.planner_model' "$record")" = "claude-fable-5" ]
}

# ── PATH-014: fence case (planner opus) -> fable reviews; fable refusal -> sonnet ──

@test "PATH-014: fence case, planner opus -> fable rung reviews (relation same-vendor)" {
  write_config opus
  stub_claude_json '[]'
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 0 ]
  record="$(record_for 1-foo plan)"
  [ "$(jq -r '.reviewer_model' "$record")" = "claude-fable-5" ]
  [ "$(jq -r '.relation' "$record")" = "same-vendor" ]
}

@test "PATH-014: fable rung refuses -> sonnet reviews, rung trail shows both" {
  write_config opus
  cat > bin/stub-claude <<'EOF'
#!/usr/bin/env bash
cat >/dev/null
model=""
for a in "$@"; do
  case "$prev" in --model) model="$a" ;; esac
  prev="$a"
done
case "$model" in
  *fable*) exit 1 ;;
  *sonnet*) printf '[]\n' ;;
  *) exit 1 ;;
esac
EOF
  chmod +x bin/stub-claude
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 0 ]
  record="$(record_for 1-foo plan)"
  [ "$(jq -r '.reviewer_model' "$record")" = "claude-sonnet-5" ]
  trail="$(jq -r '.rung_trail | join(";")' "$record")"
  [[ "$trail" == *"rule2"* ]]
}

# ── PATH-009: reviewer exhaustion -> WALL-UNREVIEWED, blocking ──────────────

@test "PATH-009: every rung fails -> WALL-UNREVIEWED, non-zero" {
  write_config fable
  stub_fail stub-claude
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 1 ]
  [[ "$output" == *"WALL-UNREVIEWED"* ]]
  record="$(record_for 1-foo plan)"
  [ "$(jq -r '.verdict' "$record")" = "WALL-UNREVIEWED" ]
}

@test "spec-004 fix round finding 2: a prior WALL-UNREVIEWED record on an UNCHANGED plan does NOT take the zero-dispatch idempotence path" {
  write_config fable
  stub_fail stub-claude
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 1 ]
  [[ "$output" == *"WALL-UNREVIEWED"* ]]

  # plan is UNCHANGED, findings queue is trivially empty (no reviewer ever
  # ran to report anything) — the old bug let this launder into a zero-
  # dispatch "adjudicated-pass" on the next run. A real reviewer must be
  # dispatched again; prove it by making the fresh dispatch observable.
  MARKER="$BATS_TEST_TMPDIR/reviewer-ran"
  cat > bin/stub-claude <<EOF
#!/usr/bin/env bash
touch "$MARKER"
cat >/dev/null
printf '[]\n'
EOF
  chmod +x bin/stub-claude
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 0 ]
  [ -f "$MARKER" ]
  [[ "$output" != *"ADJUDICATED-PASS"* ]]
  record="$(record_for 1-foo plan)"
  [ "$(jq -r '.verdict' "$record")" = "reviewed-pass" ]
}

# ── PATH-016: cross-vendor exhaustion falls through to same-vendor rule 2 ──

@test "PATH-016: both CLIs installed, every opposite-vendor rung fails -> falls through to same-vendor" {
  write_config fable
  stub_fail stub-codex
  stub_claude_json '[]'
  FFS_HOST=claude ADVERSARY_BIN_CODEX=stub-codex ADVERSARY_BIN_CLAUDE=stub-claude \
    run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 0 ]
  record="$(record_for 1-foo plan)"
  [ "$(jq -r '.relation' "$record")" = "same-vendor" ]
  trail="$(jq -r '.rung_trail | join(";")' "$record")"
  [[ "$trail" == *"rule1-opposite-vendor"* ]]
  [[ "$trail" == *"rule2-same-vendor-ordered"* ]]
}

# ── PATH-010: multi-plan phase dir aggregate blocking ───────────────────────

@test "PATH-010: multi-plan phase dir, one dirty -> aggregate block, per-plan records" {
  rm -f .planning/phases/1-foo/PLAN.md
  echo "clean plan" > .planning/phases/1-foo/01-PLAN.md
  echo "dirty plan" > .planning/phases/1-foo/02-PLAN.md
  cat > bin/stub-claude <<'EOF'
#!/usr/bin/env bash
input="$(cat)"
case "$input" in
  *"dirty plan"*) printf '[{"severity":"CRITICAL","file":"x.py","claim":"sql injection"}]\n' ;;
  *) printf '[]\n' ;;
esac
EOF
  chmod +x bin/stub-claude
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 1 ]
  [ "$(jq -r '.verdict' "$(record_for 1-foo 01-plan)")" = "reviewed-pass" ]
  [ "$(jq -r '.verdict' "$(record_for 1-foo 02-plan)")" = "blocked" ]
}

@test "PATH-010: multi-plan phase dir, both clean -> pass" {
  rm -f .planning/phases/1-foo/PLAN.md
  echo "clean plan one" > .planning/phases/1-foo/01-PLAN.md
  echo "clean plan two" > .planning/phases/1-foo/02-PLAN.md
  stub_claude_json '[]'
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 0 ]
}

# ── PATH-012: PLAN_WALL=off waiver ───────────────────────────────────────────

@test "PATH-012: PLAN_WALL=off -> WAIVED record with waiver metadata + queue waive entry" {
  PLAN_WALL=off PLAN_WALL_REASON="deliberate skip for testing" run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 0 ]
  [[ "$output" == *"WAIVED"* ]]
  record="$(record_for 1-foo plan)"
  [ "$(jq -r '.verdict' "$record")" = "WAIVED" ]
  [ "$(jq -r '.waiver.reason' "$record")" = "deliberate skip for testing" ]
  [ "$(jq -r '.waiver.plan_sha256' "$record")" != "null" ]
  disp="$(queue_list --source wall --plan .planning/phases/1-foo/PLAN.md | jq -r '.[0].disposition')"
  [ "$disp" = "waive" ]
}

@test "PATH-012: waiver on a security-matching plan flags waived_security true" {
  echo "Phase 1: rotate the OAuth token and JWT secret" > .planning/phases/1-foo/PLAN.md
  PLAN_WALL=off PLAN_WALL_REASON="skip" run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 0 ]
  record="$(record_for 1-foo plan)"
  [ "$(jq -r '.security_match' "$record")" = "true" ]
  [ "$(jq -r '.waiver.waived_security' "$record")" = "true" ]
}

@test "PATH-012/EDGE-007: waiver record write failure fails closed" {
  mkdir -p .planning/run-state
  chmod 555 .planning/run-state
  PLAN_WALL=off PLAN_WALL_REASON="skip" run bash "$LEVER" .planning/phases/1-foo
  chmod 755 .planning/run-state
  [ "$status" -ne 0 ]
  [[ "$output" == *"FATAL"* ]]
}

# ── PATH-017: record durability — reviewed-pass and WAIVED under read-only dir ──

@test "PATH-017: record write failure on a reviewed-pass path fails closed" {
  stub_claude_json '[]'
  mkdir -p .planning/run-state
  chmod 555 .planning/run-state
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
  chmod 755 .planning/run-state
  [ "$status" -ne 0 ]
  [[ "$output" == *"FATAL"* ]]
}

# ── EDGE-001: missing/unreadable PLAN.md -> NO-PLAN, fail-closed ───────────

@test "EDGE-001: missing plan file -> NO-PLAN, record written, fail-closed" {
  run bash "$LEVER" .planning/phases/1-foo/NOPE-PLAN.md
  [ "$status" -eq 1 ]
  [[ "$output" == *"NO-PLAN"* ]]
  [ -f ".planning/run-state/plan-wall-1-foo-nope-plan.json" ]
  [ "$(jq -r '.verdict' ".planning/run-state/plan-wall-1-foo-nope-plan.json")" = "NO-PLAN" ]
}

# ── path containment: a plan path must resolve inside the repo ─────────────

@test "path containment: symlink escaping the repo is FATAL-refused, never read" {
  echo "TOP_SECRET_OUTSIDE_REPO_CONTENT" > "$BATS_TEST_TMPDIR/outside-secret.md"
  ln -s "$BATS_TEST_TMPDIR/outside-secret.md" .planning/phases/1-foo/escape-PLAN.md
  cat > bin/stub-claude <<'EOF'
#!/usr/bin/env bash
cat > "$BATS_TEST_TMPDIR/should-not-be-dispatched"
printf '[]\n'
EOF
  chmod +x bin/stub-claude
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude \
    run bash "$LEVER" .planning/phases/1-foo/escape-PLAN.md
  [ "$status" -eq 1 ]
  [[ "$output" == *"FATAL"* ]]
  [[ "$output" == *"outside the repo"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/should-not-be-dispatched" ]
  [ ! -f ".planning/run-state/plan-wall-1-foo-escape-plan.json" ]
}

@test "path containment: a plain in-repo plan file is unaffected" {
  stub_claude_json '[]'
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 0 ]
}

# ── missing schema file -> FATAL, never a silent unvalidated run ───────────

@test "missing review-finding schema file -> FATAL before any dispatch" {
  rm -f schemas/review-finding.schema.json
  run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -ne 0 ]
  [[ "$output" == *"FATAL"* ]]
  [[ "$output" == *"schema"* ]]
}

# ── queue I/O fail-closed (fault injection) ─────────────────────────────────

@test "queue I/O failure (unwritable store) -> blocked verdict + queue_error stamp" {
  stub_claude_json '[{"severity":"HIGH","file":"a.py","claim":"leak"}]'
  export GATES_STORE="$REPO/.feature-fix-swarm/evidence.json"
  mkdir -p "$(dirname "$GATES_STORE")"
  : > "$GATES_STORE"
  chmod 444 "$GATES_STORE"
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
  chmod 644 "$GATES_STORE"
  [ "$status" -eq 1 ]
  record="$(record_for 1-foo plan)"
  [ "$(jq -r '.verdict' "$record")" = "blocked" ]
  [ "$(jq -r '.queue_error' "$record")" = "true" ]
}

# ── fresh-context contract: payload = brief + plan content ONLY ────────────

@test "fresh-context: dispatch payload contains the plan content and no run/host metadata" {
  echo "UNIQUE_PLAN_MARKER_xyz789 build the thing" > .planning/phases/1-foo/PLAN.md
  cat > bin/stub-claude <<EOF
#!/usr/bin/env bash
cat > "$BATS_TEST_TMPDIR/captured-stdin.txt"
printf '[]\n'
EOF
  chmod +x bin/stub-claude
  GSD_RUN_ID=my-secret-run-id-42 FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude \
    run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 0 ]
  grep -q "UNIQUE_PLAN_MARKER_xyz789" "$BATS_TEST_TMPDIR/captured-stdin.txt"
  ! grep -q "my-secret-run-id-42" "$BATS_TEST_TMPDIR/captured-stdin.txt"
  ! grep -q "$REPO" "$BATS_TEST_TMPDIR/captured-stdin.txt"
}

# ── planner identity from live config (never templates) ────────────────────

@test "planner identity resolved from live config, not a hardcoded default" {
  write_config sonnet
  stub_claude_json '[]'
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 0 ]
  record="$(record_for 1-foo plan)"
  [ "$(jq -r '.planner_model' "$record")" = "claude-sonnet-5" ]
}

# ── fence marker + escalation/cross-vendor-fallback stamps ─────────────────

@test "fence marker stamped when the fence fired for this run; security_match independent" {
  write_config fable
  echo "Phase 1: rls policy and jwt verification" > .planning/phases/1-foo/PLAN.md
  GSD_RUN_ID=fence-run-1 bash "$FENCE_LEVER" .planning .planning/phases/1-foo/PLAN.md
  stub_claude_json '[]'
  GSD_RUN_ID=fence-run-1 FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude \
    run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 0 ]
  record="$(record_for 1-foo plan)"
  [ "$(jq -r '.fence_marker' "$record")" = "true" ]
  [ "$(jq -r '.security_match' "$record")" = "true" ]
}

@test "no fence marker for this run -> fence_marker false even on a security-matching plan" {
  write_config fable
  echo "Phase 1: rls policy and jwt verification" > .planning/phases/1-foo/PLAN.md
  stub_claude_json '[]'
  GSD_RUN_ID=no-fence-run FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude \
    run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 0 ]
  record="$(record_for 1-foo plan)"
  [ "$(jq -r '.fence_marker' "$record")" = "false" ]
  [ "$(jq -r '.security_match' "$record")" = "true" ]
}

@test "SECURITY_MODEL_FENCE=off kill-switch state stamped as fence_enabled: false" {
  stub_claude_json '[]'
  SECURITY_MODEL_FENCE=off FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude \
    run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 0 ]
  record="$(record_for 1-foo plan)"
  [ "$(jq -r '.fence_enabled' "$record")" = "false" ]
}

@test "escalation_enabled and cross_vendor_fallback are stamped from config/env" {
  stub_claude_json '[]'
  # No codex binary configured -> the opposite-vendor rung (rule 1) fails.
  # FFS_CROSS_VENDOR_FALLBACK=off means selection must NOT fall back to the
  # same-vendor claude stub (rule 2) — off is stricter, not looser (spec-004
  # fix round finding 8) — so this now exhausts straight to WALL-UNREVIEWED
  # rather than silently passing on a same-vendor reviewer.
  FFS_CROSS_VENDOR_FALLBACK=off FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude \
    run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 1 ]
  [[ "$output" == *"WALL-UNREVIEWED"* ]]
  record="$(record_for 1-foo plan)"
  [ "$(jq -r '.verdict' "$record")" = "WALL-UNREVIEWED" ]
  [ "$(jq -r '.escalation_enabled' "$record")" = "true" ]
  [ "$(jq -r '.cross_vendor_fallback' "$record")" = "off" ]
  trail="$(jq -r '.rung_trail | join(";")' "$record")"
  [[ "$trail" == *"rule2-skipped:cross-vendor-fallback-off"* ]]
}

@test "FFS_CROSS_VENDOR_FALLBACK=off with a working opposite-vendor rung still passes normally" {
  # host=codex -> opposite vendor (rule 1) is claude, which the simple
  # stub_claude_json helper can satisfy directly (unlike a codex stub, which
  # would need to replicate --output-last-message file semantics). Rule 1
  # succeeds on the FIRST attempt, so cvf=off never even needs to matter —
  # it only changes behavior once rule 1 is exhausted.
  stub_claude_json '[]'
  FFS_CROSS_VENDOR_FALLBACK=off FFS_HOST=codex ADVERSARY_BIN_CLAUDE=stub-claude \
    run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 0 ]
  record="$(record_for 1-foo plan)"
  [ "$(jq -r '.verdict' "$record")" = "reviewed-pass" ]
  trail="$(jq -r '.rung_trail | join(";")' "$record")"
  [[ "$trail" == *"rule1-opposite-vendor"* ]]
  [[ "$trail" != *"rule2-skipped"* ]]
}

# ── EDGE-003: schema-invalid / zero-byte rung output is a rung failure ─────

@test "EDGE-003: zero-byte rung output is a rung failure, valid lower rung accepted" {
  write_config fable
  cat > bin/stub-claude <<'EOF'
#!/usr/bin/env bash
cat >/dev/null
model=""
for a in "$@"; do
  case "$prev" in --model) model="$a" ;; esac
  prev="$a"
done
case "$model" in
  *fable*) printf '' ;;
  *sonnet*) printf '[]\n' ;;
  *) exit 1 ;;
esac
EOF
  chmod +x bin/stub-claude
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 0 ]
  record="$(record_for 1-foo plan)"
  [ "$(jq -r '.reviewer_model' "$record")" = "claude-sonnet-5" ]
}

@test "EDGE-003: schema-invalid (non-array) rung output is a rung failure" {
  write_config fable
  cat > bin/stub-claude <<'EOF'
#!/usr/bin/env bash
cat >/dev/null
model=""
for a in "$@"; do
  case "$prev" in --model) model="$a" ;; esac
  prev="$a"
done
case "$model" in
  *fable*) printf '{"not":"an array"}\n' ;;
  *sonnet*) printf '[]\n' ;;
  *) exit 1 ;;
esac
EOF
  chmod +x bin/stub-claude
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 0 ]
  record="$(record_for 1-foo plan)"
  [ "$(jq -r '.reviewer_model' "$record")" = "claude-sonnet-5" ]
}

@test "EDGE-003: wrong-typed field (line as a string) is a rung failure, not a laundered finding" {
  write_config fable
  cat > bin/stub-claude <<'EOF'
#!/usr/bin/env bash
cat >/dev/null
model=""
for a in "$@"; do
  case "$prev" in --model) model="$a" ;; esac
  prev="$a"
done
case "$model" in
  *fable*) printf '[{"severity":"HIGH","file":"a.py","claim":"x","line":"not-a-number"}]\n' ;;
  *sonnet*) printf '[]\n' ;;
  *) exit 1 ;;
esac
EOF
  chmod +x bin/stub-claude
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 0 ]
  record="$(record_for 1-foo plan)"
  [ "$(jq -r '.reviewer_model' "$record")" = "claude-sonnet-5" ]
}

@test "EDGE-003: out-of-range confidence (>1) is a rung failure" {
  write_config fable
  cat > bin/stub-claude <<'EOF'
#!/usr/bin/env bash
cat >/dev/null
model=""
for a in "$@"; do
  case "$prev" in --model) model="$a" ;; esac
  prev="$a"
done
case "$model" in
  *fable*) printf '[{"severity":"HIGH","file":"a.py","claim":"x","confidence":1.5}]\n' ;;
  *sonnet*) printf '[]\n' ;;
  *) exit 1 ;;
esac
EOF
  chmod +x bin/stub-claude
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 0 ]
  record="$(record_for 1-foo plan)"
  [ "$(jq -r '.reviewer_model' "$record")" = "claude-sonnet-5" ]
}

# ── PATH-013: reopen on a changed plan re-reporting the same finding ────────

@test "PATH-013: resolved signature re-reported after the plan CHANGES -> reopens and blocks" {
  stub_claude_json '[{"severity":"HIGH","file":"a.py","claim":"leaky query","line":9}]'
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 1 ]
  sig="$(queue_list --unresolved --source wall --plan .planning/phases/1-foo/PLAN.md | jq -r '.[0].sig')"
  python3 "$GATES_PY" findings-queue resolve "$sig" --disposition refute --reason "not real"

  echo "Phase 1: a DIFFERENT plan body, forces a fresh dispatch" > .planning/phases/1-foo/PLAN.md
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 1 ]
  [[ "$output" == *"BLOCKED"* ]]
  entry="$(queue_list | jq --arg sig "$sig" '.[] | select(.sig == $sig)')"
  [ "$(printf '%s' "$entry" | jq -r '.resolved')" = "false" ]
  [ "$(printf '%s' "$entry" | jq '.history | length')" -ge 1 ]
}
