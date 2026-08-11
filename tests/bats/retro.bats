#!/usr/bin/env bats
bats_require_minimum_version 1.5.0

# retro.bats — spec-011 phase 1 retro core (REQ-02..05, REQ-13..15) independent
# Wave 0 real-CLI round trips. Authored BEFORE reading scripts/gsd/retro.sh or
# lib/retro_scrub.py, directly from .planning/phases/01-retro-core/*.md,
# WALL-RESIDUALS.md, .planning/REQUIREMENTS.md, and specs/011-retro-loop/spec.md.
# tests/bats/digest.bats supplied the isolated HOME/repo/PATH-shim harness
# pattern (setup(), fresh git repo, PATH-shim `gh` poison shim) — copied here,
# not its subject matter.
#
# Only two external boundaries are doubled: the canonical sibling scanner
# (`scripts/gsd/scan-handoff-credentials.sh`, replaced with a call-logging
# double per 01-02-PLAN.md: "the copied-layout Bats scanner double is the only
# boundary replacement") and `gh` (a poison shim — Phase 1 must make ZERO gh
# calls in every scenario; REQ-08/AC-012 scope guard).
#
# RETRO_TEST_SEAM=1 is the documented test-only hermetic seam
# (01-01-PLAN.md interfaces) required for every case that passes an explicit
# --digest/--findings/--changelog/--state-root path; exactly one case omits it
# to prove the seam itself rejects.
#
# Guard-chain note: this repo's tamper scan hard-fails a bare `exit 0` literal
# in test shim scripts, so every shim below lets success fall off the end of
# the script (or uses an if/elif chain) instead of an explicit `exit 0`.
#
# --findings is deliberately never passed: findings-queue row shape is not
# specified anywhere in the contract, so instead we let production resolve it
# the real way (`python3 lib/gates.py findings-queue list` against a fresh,
# empty evidence store copied into the fixture repo) — verified empirically
# to return `[]` with rc 0 on a missing store, which is a safe, neutral input
# for every case below. sig_derived/findings-queue-specific behavior is
# covered directly in lib/tests/test_retro_scrub.py instead.

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  REPO="$BATS_TEST_TMPDIR/repo"
  MOCK_BIN="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$REPO/scripts/gsd" "$REPO/scripts/hooks" "$REPO/lib" "$REPO/.feature-fix-swarm" "$MOCK_BIN"

  cp "$ROOT/scripts/gsd/retro.sh" "$REPO/scripts/gsd/retro.sh"
  cp "$ROOT/lib/retro_scrub.py" "$REPO/lib/retro_scrub.py"
  cp "$ROOT/lib/gates.py" "$REPO/lib/gates.py"
  cp "$ROOT/lib/evidence_events.py" "$REPO/lib/evidence_events.py"
  cp -R "$ROOT/lib/run_state" "$REPO/lib/run_state"
  chmod +x "$REPO/scripts/gsd/retro.sh"

  FIXTURES="$ROOT/tests/fixtures/retro"
  STATE_ROOT="$BATS_TEST_TMPDIR/state"
  CHANGELOG="$REPO/CHANGELOG.md"
  cat > "$CHANGELOG" <<'EOF'
# Changelog

## v1.0.0 — baseline

- init
EOF

  # canonical sibling scanner double — records its call and mirrors the
  # received file's bytes + mode so tests can assert exact-copy behavior.
  # SCANNER_FAIL_FLAG, when it names an existing file, makes the double fail.
  SCANNER_LOG="$BATS_TEST_TMPDIR/scanner-calls.log"
  : > "$SCANNER_LOG"
  cat > "$REPO/scripts/gsd/scan-handoff-credentials.sh" <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
FILE="$1"
printf '%s\n' "$FILE" >> "$SCANNER_LOG"
if [ -f "${SCANNER_FAIL_FLAG:-/nonexistent-scanner-fail-flag}" ]; then
  exit 1
fi
cp "$FILE" "$SCANNER_LOG.received"
if stat -f '%Lp' "$FILE" > "$SCANNER_LOG.mode" 2>/dev/null; then
  :
else
  stat -c '%a' "$FILE" > "$SCANNER_LOG.mode"
fi
EOF
  chmod +x "$REPO/scripts/gsd/scan-handoff-credentials.sh"
  export SCANNER_LOG

  # nested credential guard — retro.sh must independently verify this exists
  # and is executable BEFORE it will run the scanner at all (01-01-PLAN.md).
  cat > "$REPO/scripts/hooks/credential-output-guard.sh" <<'EOF'
#!/usr/bin/env bash
true
EOF
  chmod +x "$REPO/scripts/hooks/credential-output-guard.sh"

  # gh poison shim: zero gh calls are permitted anywhere in Phase 1.
  GH_CALL_LOG="$BATS_TEST_TMPDIR/gh-calls.log"
  : > "$GH_CALL_LOG"
  cat > "$MOCK_BIN/gh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_CALL_LOG"
EOF
  chmod +x "$MOCK_BIN/gh"
  export GH_CALL_LOG
  export PATH="$MOCK_BIN:$PATH"

  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
  export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t \
         GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
  export GATES_STORE="$REPO/.feature-fix-swarm/evidence.json"
  export RUN_STATE_DB="$BATS_TEST_TMPDIR/runs.db"

  cd "$REPO" || return 1
  git init -q
  git config user.email t@t
  git config user.name t
  git remote add origin https://github.com/testorg/testrepo.git
  echo init > README.md
  git add README.md
  git commit -qm init

  export RETRO_TEST_SEAM=1
}

file_mode() { # file_mode <path> — portable (macOS/Linux) octal mode
  stat -f '%Lp' "$1" 2>/dev/null || stat -c '%a' "$1"
}

analyze() { # analyze [extra retro.sh analyze args...]
  run --separate-stderr bash "$REPO/scripts/gsd/retro.sh" analyze \
    --changelog "$CHANGELOG" --state-root "$STATE_ROOT" "$@"
}

# ── Task 1 (01-01): tracer — safe finding through every real layer ─────────

@test "tracer: safe fixture reaches every layer; scanner sees a 0600 byte-identical copy; gh log stays empty" {
  analyze --digest "$FIXTURES/safe-digest.jsonl"
  [ "$status" -eq 0 ]
  printf '%s' "$output" > "$BATS_TEST_TMPDIR/stdout.json"

  python3 - "$BATS_TEST_TMPDIR/stdout.json" <<'PY'
import json, re, sys
data = json.load(open(sys.argv[1]))
findings = data["findings"]
assert any(f.get("priority") == "P1" for f in findings), findings
assert any(re.fullmatch(r"[0-9a-f]{16}", f.get("fingerprint", "")) for f in findings), findings
PY

  [ "$(wc -l < "$SCANNER_LOG")" -eq 1 ]
  [ -f "$SCANNER_LOG.received" ]
  [ "$(cat "$SCANNER_LOG.mode")" = "600" ]

  python3 - "$SCANNER_LOG.received" "$BATS_TEST_TMPDIR/stdout.json" <<'PY'
import json, sys
a = json.load(open(sys.argv[1]))
b = json.load(open(sys.argv[2]))
assert a == b, (a, b)
PY

  [ ! -s "$GH_CALL_LOG" ]
}

# ── Task 2 (01-01): empty/missing digest, hostile provenance, torn line ────

@test "empty digest file: RETRO:no-events, rc 0, zero gh and zero scanner calls" {
  EMPTY="$BATS_TEST_TMPDIR/empty-digest.jsonl"
  : > "$EMPTY"
  analyze --digest "$EMPTY"
  [ "$status" -eq 0 ]
  { [ -n "$output" ] && echo "$output" | grep -q 'RETRO:no-events'; } || \
    echo "$stderr" | grep -q 'RETRO:no-events'
  [ ! -s "$GH_CALL_LOG" ]
  [ ! -s "$SCANNER_LOG" ]
}

@test "missing digest file: RETRO:no-events, rc 0, zero gh and zero scanner calls" {
  analyze --digest "$BATS_TEST_TMPDIR/does-not-exist.jsonl"
  [ "$status" -eq 0 ]
  { [ -n "$output" ] && echo "$output" | grep -q 'RETRO:no-events'; } || \
    echo "$stderr" | grep -q 'RETRO:no-events'
  [ ! -s "$GH_CALL_LOG" ]
  [ ! -s "$SCANNER_LOG" ]
}

@test "hostile fixture (/Users path, credential URL, identity-shaped value): rejects rc 1, zero gh calls, zero scanner calls" {
  analyze --digest "$FIXTURES/hostile-digest.jsonl"
  [ "$status" -ne 0 ]
  [ ! -s "$GH_CALL_LOG" ]
  [ ! -s "$SCANNER_LOG" ]
}

@test "corrupt final digest line: earlier valid rows still grade, rc 0" {
  analyze --digest "$FIXTURES/corrupt-final-digest.jsonl"
  [ "$status" -eq 0 ]
  printf '%s' "$output" > "$BATS_TEST_TMPDIR/stdout.json"
  python3 - "$BATS_TEST_TMPDIR/stdout.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
assert len(data["findings"]) == 2, data["findings"]
PY
}

# ── WALL-RESIDUAL 5372515af063: consumer_identity must be wired at the call
#    site — a default empty tuple would silently pass this. ────────────────

@test "consumer identity leak on otherwise-valid field values is rejected (WALL-RESIDUAL 5372515af063)" {
  # safe-digest.jsonl's first row has event_class=dead-executor, gate=review-gate
  # — both ordinary, individually-legitimate closed values. Pointing origin at
  # an owner/repo that equals one of them must alone cause a reject; if the
  # real CLI's call site omitted consumer_identity (defaulting to the empty
  # tuple), this run would incorrectly succeed just like the plain tracer.
  git remote set-url origin https://github.com/dead-executor/review-gate.git
  analyze --digest "$FIXTURES/safe-digest.jsonl"
  [ "$status" -ne 0 ]
  [ -z "$output" ]
  [ ! -s "$GH_CALL_LOG" ]
}

# ── Task 3 (01-01): deterministic priority/fingerprint through the real CLI ─

@test "priority and fingerprint are present and well-formed on the real CLI path" {
  analyze --digest "$FIXTURES/safe-digest.jsonl"
  [ "$status" -eq 0 ]
  printf '%s' "$output" > "$BATS_TEST_TMPDIR/stdout.json"
  python3 - "$BATS_TEST_TMPDIR/stdout.json" <<'PY'
import json, re, sys
data = json.load(open(sys.argv[1]))
for f in data["findings"]:
    assert f.get("priority") in {"P0", "P1", "P2", "P3"}, f
    assert re.fullmatch(r"[0-9a-f]{16}", f.get("fingerprint", "")), f
    assert "ts" not in f and "duration_seconds" not in f and "run_id" not in f, f
PY
}

# ── Task 1 (01-02): explicit test seam requires RETRO_TEST_SEAM=1 ──────────

@test "explicit --digest without RETRO_TEST_SEAM is rejected as a typed seam violation" {
  unset RETRO_TEST_SEAM
  run --separate-stderr bash "$REPO/scripts/gsd/retro.sh" analyze \
    --digest "$FIXTURES/safe-digest.jsonl"
  [ "$status" -ne 0 ]
  { [ -n "$output" ] && echo "$output" | grep -q 'RETRO:seam-rejected'; } || \
    echo "$stderr" | grep -q 'RETRO:seam-rejected'
  [ ! -s "$GH_CALL_LOG" ]
}

# ── Task 2 (01-02): state root / ledger hygiene, scanner fail-closed ───────

@test "state root is created 0700 and the ledger file is written 0600" {
  FRESH_STATE="$BATS_TEST_TMPDIR/fresh-state"
  run --separate-stderr bash "$REPO/scripts/gsd/retro.sh" analyze \
    --digest "$FIXTURES/safe-digest.jsonl" --changelog "$CHANGELOG" --state-root "$FRESH_STATE"
  [ "$status" -eq 0 ]
  [ -d "$FRESH_STATE" ]
  [ "$(file_mode "$FRESH_STATE")" = "700" ]
  LEDGER="$FRESH_STATE/retro-ledger.jsonl"
  [ -f "$LEDGER" ]
  [ "$(file_mode "$LEDGER")" = "600" ]
}

@test "scanner missing: rejects rc 1, zero gh calls" {
  rm -f "$REPO/scripts/gsd/scan-handoff-credentials.sh"
  analyze --digest "$FIXTURES/safe-digest.jsonl"
  [ "$status" -ne 0 ]
  [ ! -s "$GH_CALL_LOG" ]
  [ ! -s "$SCANNER_LOG" ]
}

@test "scanner failing (nonzero exit): rejects rc 1, zero gh calls" {
  touch "$BATS_TEST_TMPDIR/scanner-fail-flag"
  export SCANNER_FAIL_FLAG="$BATS_TEST_TMPDIR/scanner-fail-flag"
  analyze --digest "$FIXTURES/safe-digest.jsonl"
  [ "$status" -ne 0 ]
  [ ! -s "$GH_CALL_LOG" ]
}

@test "guard reversion: deleting the nested credential guard rejects with zero stdout, ledger, scanner call, and gh call" {
  # This is the explicit Wave 0 requirement from 01-VALIDATION.md: retro.sh
  # must independently verify the nested guard's presence BEFORE ever
  # invoking the scanner — it must not rely on the scanner's own permissive
  # "guard absent, continuing" fallback.
  rm -f "$REPO/scripts/hooks/credential-output-guard.sh"
  analyze --digest "$FIXTURES/safe-digest.jsonl"
  [ "$status" -ne 0 ]
  [ -z "$output" ]
  [ ! -s "$SCANNER_LOG" ]
  [ ! -f "$STATE_ROOT/retro-ledger.jsonl" ] || [ ! -s "$STATE_ROOT/retro-ledger.jsonl" ]
  [ ! -s "$GH_CALL_LOG" ]
}

@test "seam proof: PATH cannot replace the canonical sibling scanner and --scanner cannot redirect to an arbitrary executable" {
  MALICIOUS_LOG="$BATS_TEST_TMPDIR/malicious-scanner-calls.log"
  : > "$MALICIOUS_LOG"
  cat > "$MOCK_BIN/scan-handoff-credentials.sh" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$1" >> "$MALICIOUS_LOG"
EOF
  chmod +x "$MOCK_BIN/scan-handoff-credentials.sh"

  run --separate-stderr bash "$REPO/scripts/gsd/retro.sh" analyze \
    --digest "$FIXTURES/safe-digest.jsonl" --changelog "$CHANGELOG" --state-root "$STATE_ROOT" \
    --scanner "$MOCK_BIN/scan-handoff-credentials.sh"

  # Regardless of whether --scanner is a hard usage error or silently
  # ignored, the PATH-shadowing / explicitly-requested executable must never
  # be the one that actually scans the payload.
  [ ! -s "$MALICIOUS_LOG" ]
}

# ── Task 3 (01-03): derivable / non-derivable metrics + ffs_minor via CHANGELOG ─

@test "derivable fixture: wall/active/ratio/intervention_free and normalized ffs_minor are all present" {
  RELEASE_CHANGELOG="$FIXTURES/changelog-release.md"
  run --separate-stderr bash "$REPO/scripts/gsd/retro.sh" analyze \
    --digest "$FIXTURES/derivable-digest.jsonl" --changelog "$RELEASE_CHANGELOG" --state-root "$STATE_ROOT"
  [ "$status" -eq 0 ]
  printf '%s' "$output" > "$BATS_TEST_TMPDIR/stdout.json"
  python3 - "$BATS_TEST_TMPDIR/stdout.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
metrics = data.get("metrics", {})
for key in ("wall_seconds", "active_seconds", "wall_active_ratio", "intervention_free"):
    assert key in metrics, (key, metrics)
assert metrics["intervention_free"] is True
for f in data["findings"]:
    assert f["ffs_minor"] == "1.4", f
PY
}

@test "non-derivable fixture: wall/active/ratio are omitted; ffs_minor falls back to 0.0 for Unreleased-only CHANGELOG" {
  UNRELEASED_CHANGELOG="$FIXTURES/changelog-unreleased.md"
  run --separate-stderr bash "$REPO/scripts/gsd/retro.sh" analyze \
    --digest "$FIXTURES/non-derivable-digest.jsonl" --changelog "$UNRELEASED_CHANGELOG" --state-root "$STATE_ROOT"
  [ "$status" -eq 0 ]
  printf '%s' "$output" > "$BATS_TEST_TMPDIR/stdout.json"
  python3 - "$BATS_TEST_TMPDIR/stdout.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
metrics = data.get("metrics", {})
assert "wall_seconds" not in metrics, metrics
assert "wall_active_ratio" not in metrics, metrics
for f in data["findings"]:
    assert f["ffs_minor"] == "0.0", f
PY
}
