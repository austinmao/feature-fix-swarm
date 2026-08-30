#!/usr/bin/env bats
# canary-deploy-gate.sh — GH-153: the sole legitimate producer of canary
# evidence rows for image-digest deploy surfaces. Observes a digest from a
# consumer-supplied query command (never accepts one as input), runs a
# consumer-supplied post-deploy probe under a bound, and records a typed
# {run_id, sha, pass, created_at, ended_at, ts} row via lib/gates.py
# canary-evidence. Asserts: happy path + binding-satisfiable end-to-end,
# probe fail/timeout, digest-query invalid/unobserved/timeout, missing
# seams, the forbidden --digest/positional-arg surface, unrecordable-store
# fail-closed, and a pattern-drift vector table pinned against
# gates.ARTIFACT_DIGEST_PAT.

SCRIPT_REL="scripts/gsd/canary-deploy-gate.sh"

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO/scripts/gsd" "$REPO/lib"
  # WR-140: run a FIXTURE-LOCAL copy, never $ROOT's — a recorder regression
  # must not write into the developer's canonical evidence store. Mirrors
  # tests/bats/canary-gate.bats setup() verbatim.
  cp "$ROOT/scripts/gsd/canary-deploy-gate.sh" "$REPO/scripts/gsd/canary-deploy-gate.sh" 2>/dev/null || true
  cp "$ROOT/scripts/gsd/run-bounded.sh" "$REPO/scripts/gsd/run-bounded.sh"
  cp "$ROOT/lib/gates.py" "$REPO/lib/gates.py"
  chmod +x "$REPO/scripts/gsd/canary-deploy-gate.sh" 2>/dev/null || true
  chmod +x "$REPO/scripts/gsd/run-bounded.sh"
  SCRIPT="$REPO/scripts/gsd/canary-deploy-gate.sh"
  cd "$REPO" || return 1
  git init -q
  git config user.email t@t
  git config user.name t
  echo "init" > README.md
  git add README.md
  git commit -q -m init
  STORE="$BATS_TEST_TMPDIR/evidence.json"
}

# $1=digest-query command string $2=probe command string [$3.. extra env
# already exported by the caller before invoking this helper]
run_wrapper() {
  local digest_cmd="$1" probe_cmd="$2"
  GATES_STORE="$STORE" FFS_DEPLOY_DIGEST_CMD="$digest_cmd" \
    FFS_DEPLOY_PROBE_CMD="$probe_cmd" run bash "$SCRIPT"
}

row_count() {
  jq -r '.canary | length' "$STORE" 2>/dev/null || echo 0
}

# ── happy path + binding ──────────────────────────────────────────────────

@test "happy path: passing probe records one PASS row keyed by the observed digest" {
  DIGEST="app@sha256:$(printf 'a%.0s' $(seq 1 64))"
  GSD_RUN_ID="run-99" run_wrapper "printf '%s\n' '$DIGEST'" "true"
  [ "$status" -eq 0 ]
  [[ "$output" == *"canary-deploy-gate: PASS"* ]]
  [ "$(row_count)" = "1" ]
  [ "$(jq -r '.canary[0].sha' "$STORE")" = "$DIGEST" ]
  [ "$(jq -r '.canary[0].pass' "$STORE")" = "true" ]
  [ "$(jq -r '.canary[0].run_id' "$STORE")" = "run-99" ]
  [[ "$(jq -r '.canary[0].created_at' "$STORE")" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]]
  [[ "$(jq -r '.canary[0].ended_at' "$STORE")" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]]
  [ "$(jq -r '.canary[0].ts | type' "$STORE")" = "number" ]
}

@test "binding satisfiable: the recorded row BINDs check_promotion for that digest end-to-end" {
  DIGEST="app@sha256:$(printf 'b%.0s' $(seq 1 64))"
  run_wrapper "printf '%s\n' '$DIGEST'" "true"
  [ "$status" -eq 0 ]
  [ "$(row_count)" = "1" ]
  run python3 - "$REPO/lib/gates.py" "$DIGEST" "$STORE" <<'PYEOF'
import importlib.util
import sys
from pathlib import Path

gates_path, artifact, store = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("gates", gates_path)
gates = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gates)

store = Path(store)
try:
    gates.grant_actions(store, "run-1", ["deploy:prod-web"])
    gates.record_promotion(
        store, "run-1", from_env="staging", to_env="prod",
        surface="web", artifact=artifact, evidence_ids=[],
    )
    bound = gates.check_promotion(store, "run-1", "prod", "web", artifact)
    print("BOUND" if bound else "REFUSED")
except Exception as exc:
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(1)
PYEOF
  [ "$status" -eq 0 ]
  [ "$output" = "BOUND" ]
}

# ── probe outcomes ─────────────────────────────────────────────────────────

@test "probe failure: nonzero probe rc FAILs, records pass=false" {
  DIGEST="app@sha256:$(printf 'c%.0s' $(seq 1 64))"
  run_wrapper "printf '%s\n' '$DIGEST'" "exit 7"
  [ "$status" -eq 1 ]
  [[ "$output" == *"canary-deploy-gate: FAIL"* ]]
  [[ "$output" == *"7"* ]]
  [ "$(row_count)" = "1" ]
  [ "$(jq -r '.canary[0].pass' "$STORE")" = "false" ]
}

@test "probe timeout: hung probe FAILs, records pass=false" {
  DIGEST="app@sha256:$(printf 'd%.0s' $(seq 1 64))"
  GATES_STORE="$STORE" FFS_DEPLOY_DIGEST_CMD="printf '%s\n' '$DIGEST'" \
    FFS_DEPLOY_PROBE_CMD="sleep 5" FFS_DEPLOY_PROBE_TIMEOUT=1 \
    run bash "$SCRIPT"
  [ "$status" -eq 1 ]
  [ "$(row_count)" = "1" ]
  [ "$(jq -r '.canary[0].pass' "$STORE")" = "false" ]
}

@test "timestamps bracket the probe run, not the entry point" {
  DIGEST="app@sha256:$(printf 'e%.0s' $(seq 1 64))"
  GATES_STORE="$STORE" FFS_DEPLOY_DIGEST_CMD="printf '%s\n' '$DIGEST'" \
    FFS_DEPLOY_PROBE_CMD="sleep 2 && true" run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  CREATED="$(jq -r '.canary[0].created_at' "$STORE")"
  ENDED="$(jq -r '.canary[0].ended_at' "$STORE")"
  [[ "$ENDED" > "$CREATED" ]]
}

# ── digest-query invalid output (ordering + shape) ─────────────────────────

@test "digest-query emits nothing -> DIGEST-INVALID, zero rows" {
  run_wrapper "true" "true"
  [ "$status" -eq 2 ]
  [[ "$output" == *"CANARY-DEPLOY-DIGEST-INVALID"* ]]
  [ "$(row_count)" = "0" ]
}

@test "digest-query emits two lines (well-formed second line) -> DIGEST-INVALID, zero rows" {
  DIGEST="app@sha256:$(printf 'f%.0s' $(seq 1 64))"
  run_wrapper "printf 'garbage\n%s\n' '$DIGEST'" "true"
  [ "$status" -eq 2 ]
  [[ "$output" == *"CANARY-DEPLOY-DIGEST-INVALID"* ]]
  [ "$(row_count)" = "0" ]
}

@test "digest-query emits a bare 40-hex commit sha -> DIGEST-INVALID, zero rows" {
  SHA40="$(printf '0%.0s' $(seq 1 40))"
  run_wrapper "printf '%s\n' '$SHA40'" "true"
  [ "$status" -eq 2 ]
  [[ "$output" == *"CANARY-DEPLOY-DIGEST-INVALID"* ]]
  [ "$(row_count)" = "0" ]
}

@test "digest-query emits garbage -> DIGEST-INVALID, zero rows" {
  run_wrapper "printf '%s\n' 'not a digest'" "true"
  [ "$status" -eq 2 ]
  [[ "$output" == *"CANARY-DEPLOY-DIGEST-INVALID"* ]]
  [ "$(row_count)" = "0" ]
}

@test "digest-query exits nonzero -> DIGEST-UNOBSERVED, zero rows" {
  run_wrapper "exit 9" "true"
  [ "$status" -eq 2 ]
  [[ "$output" == *"CANARY-DEPLOY-DIGEST-UNOBSERVED"* ]]
  [ "$(row_count)" = "0" ]
}

@test "digest-query hangs past its timeout -> DIGEST-UNOBSERVED, zero rows" {
  GATES_STORE="$STORE" FFS_DEPLOY_DIGEST_CMD="sleep 5" \
    FFS_DEPLOY_PROBE_CMD="true" FFS_DEPLOY_DIGEST_TIMEOUT=1 \
    run bash "$SCRIPT"
  [ "$status" -eq 2 ]
  [[ "$output" == *"CANARY-DEPLOY-DIGEST-UNOBSERVED"* ]]
  [ "$(row_count)" = "0" ]
}

# ── missing seams ────────────────────────────────────────────────────────

@test "FFS_DEPLOY_DIGEST_CMD unset -> SEAM-MISSING, zero rows" {
  GATES_STORE="$STORE" FFS_DEPLOY_PROBE_CMD="true" \
    run env -u FFS_DEPLOY_DIGEST_CMD bash "$SCRIPT"
  [ "$status" -eq 2 ]
  [[ "$output" == *"CANARY-DEPLOY-SEAM-MISSING"* ]]
  [ "$(row_count)" = "0" ]
}

@test "FFS_DEPLOY_PROBE_CMD unset -> SEAM-MISSING, zero rows" {
  GATES_STORE="$STORE" FFS_DEPLOY_DIGEST_CMD="printf 'x\n'" \
    run env -u FFS_DEPLOY_PROBE_CMD bash "$SCRIPT"
  [ "$status" -eq 2 ]
  [[ "$output" == *"CANARY-DEPLOY-SEAM-MISSING"* ]]
  [ "$(row_count)" = "0" ]
}

@test "both seams set but one empty-string -> SEAM-MISSING, zero rows" {
  run_wrapper "printf 'x\n'" ""
  [ "$status" -eq 2 ]
  [[ "$output" == *"CANARY-DEPLOY-SEAM-MISSING"* ]]
  [ "$(row_count)" = "0" ]
}

# ── forbidden input surface (D-01) ──────────────────────────────────────────

@test "--digest with a valid digest is a usage error, zero rows" {
  DIGEST="app@sha256:$(printf 'a%.0s' $(seq 1 64))"
  GATES_STORE="$STORE" FFS_DEPLOY_DIGEST_CMD="printf '%s\n' '$DIGEST'" \
    FFS_DEPLOY_PROBE_CMD="true" run bash "$SCRIPT" --digest "$DIGEST"
  [ "$status" -eq 2 ]
  [ "$(row_count)" = "0" ]
}

@test "any positional argument is a usage error, zero rows" {
  GATES_STORE="$STORE" FFS_DEPLOY_DIGEST_CMD="printf 'x\n'" \
    FFS_DEPLOY_PROBE_CMD="true" run bash "$SCRIPT" bogus
  [ "$status" -eq 2 ]
  [ "$(row_count)" = "0" ]
}

# ── unrecordable store (fail-closed) ────────────────────────────────────────

@test "unwritable GATES_STORE dir -> EVIDENCE-UNRECORDED, no PASS token" {
  DIGEST="app@sha256:$(printf '1%.0s' $(seq 1 64))"
  RO="$BATS_TEST_TMPDIR/ro"
  mkdir -p "$RO"
  chmod 555 "$RO"
  GATES_STORE="$RO/evidence.json" FFS_DEPLOY_DIGEST_CMD="printf '%s\n' '$DIGEST'" \
    FFS_DEPLOY_PROBE_CMD="true" run bash "$SCRIPT"
  chmod 755 "$RO"
  [ "$status" -eq 3 ]
  [[ "$output" == *"CANARY-DEPLOY-EVIDENCE-UNRECORDED"* ]]
  [[ "$output" != *"canary-deploy-gate: PASS"* ]]
}

# ── pattern-drift vector table (anti-drift link to ARTIFACT_DIGEST_PAT) ────

@test "pattern-drift vector table agrees with gates.ARTIFACT_DIGEST_PAT vector-for-vector" {
  local vectors=(
    "app@sha256:$(printf 'a%.0s' $(seq 1 64))"
    "registry.example.com/ns/app:v1@sha256:$(printf 'f%.0s' $(seq 1 64))"
    "app:latest"
    "$(printf '0%.0s' $(seq 1 40))"
    "app@sha256:$(printf 'a%.0s' $(seq 1 63))"
    "App@sha256:$(printf 'a%.0s' $(seq 1 64))"
    "app@sha256:$(printf 'a%.0s' $(seq 1 64)) extra"
  )
  for vector in "${vectors[@]}"; do
    expected="$(python3 - "$REPO/lib/gates.py" "$vector" <<'PYEOF'
import importlib.util
import sys

gates_path, vector = sys.argv[1:3]
spec = importlib.util.spec_from_file_location("gates", gates_path)
gates = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gates)
print("accept" if gates.ARTIFACT_DIGEST_PAT.fullmatch(vector) else "refuse")
PYEOF
)"
    GATES_STORE="$BATS_TEST_TMPDIR/vec-evidence.json" \
      FFS_DEPLOY_DIGEST_CMD="printf '%s\n' '$vector'" \
      FFS_DEPLOY_PROBE_CMD="true" run bash "$SCRIPT"
    if [ "$expected" = "accept" ]; then
      [ "$status" -eq 0 ] || { echo "expected accept for: $vector (got status $status: $output)"; return 1; }
    else
      [ "$status" -eq 2 ] || { echo "expected refuse for: $vector (got status $status: $output)"; return 1; }
    fi
  done
}
