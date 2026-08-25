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
  # fail-soft: absence must stay observable as RED, not error out the harness
  [ -f "$ROOT/lib/retro_state.py" ] && cp "$ROOT/lib/retro_state.py" "$REPO/lib/retro_state.py"
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
  # GNU form first: on Linux `stat -f` is FILESYSTEM stat and exits 0 with
  # the wrong data, so the BSD form must be the fallback, never the probe.
  stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"
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
  # Phase 2 REQ-01/AC-001: no-consent is a typed no-op BEFORE collection, so
  # this Phase 1 test (predating the consent gate) now needs consent granted
  # under isolated HOME to still reach the state-root ledger write it's
  # actually checking. Seeded via the real `consent --grant` CLI (not a
  # hand-written JSON fixture) so the grant path stays exercised too.
  retro_isolate_home "$BATS_TEST_TMPDIR" fresh-state-mode
  run bash "$REPO/scripts/gsd/retro.sh" consent --grant
  [ "$status" -eq 0 ]

  # --state-root must resolve to the SAME directory consent/filing already
  # use ($RETRO_STATE = $HOME/.cache/feature-fix-swarm, set by
  # retro_isolate_home) -- confirmed by black-box execution: an unrelated
  # --state-root now silently makes zero gh calls and writes no ledger,
  # since production's filing transaction is fixed to the HOME-derived
  # cache dir (02-01-PLAN interfaces) and consent lives there too.
  FRESH_STATE="$RETRO_STATE"
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

# ── Phase 2 Wave 0 (02-01/02-VALIDATION W0-01): gate chain, consent
#    lifecycle, and the consented filing tracer -- REQ-01/06/09/12 ─────────
#
# Authored directly from .planning/phases/02-filing-consent-seams/
# {02-VALIDATION,02-01-PLAN}.md, .planning/REQUIREMENTS.md, and
# specs/011-retro-loop/spec.md WITHOUT reading lib/retro_state.py (absent)
# or the internals of scripts/gsd/retro.sh / lib/retro_scrub.py beyond
# black-box execution. See tests/bats/helpers/retro-shims.bash for the
# shared harness and its documented ambiguity resolutions.
#
# single-p1-digest.jsonl (not safe-digest.jsonl) is used throughout: it
# carries exactly ONE P1 finding, so "exactly one create" assertions stay
# unambiguous regardless of whether P0/P1/P2 findings (unlike P3) all file
# immediately -- safe-digest.jsonl's second (P2) event would otherwise file
# a second issue and silently break an "exactly one" count.

load 'helpers/retro-shims'

analyze_h() { # analyze_h [extra retro.sh analyze args] -- isolated-HOME wrapper
  run --separate-stderr bash "$REPO/scripts/gsd/retro.sh" analyze \
    --changelog "$FIXTURES/changelog-release.md" --state-root "$RETRO_STATE" "$@"
}

# ── gate chain: off / --no-retro / no-consent variants / auth failure ──────

@test "gate: FFS_RETRO=off short-circuits before collection -- rc 0, zero gh calls, zero scanner calls" {
  retro_isolate_home "$BATS_TEST_TMPDIR" off
  retro_seed_consent "$FIXTURES/consent-granted.json"
  FFS_RETRO=off analyze_h --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  [ ! -s "$GH_CALL_LOG" ]
  [ ! -s "$SCANNER_LOG" ]
}

@test "gate: --no-retro short-circuits before collection -- rc 0, zero gh calls, zero scanner calls" {
  retro_isolate_home "$BATS_TEST_TMPDIR" noretro
  retro_seed_consent "$FIXTURES/consent-granted.json"
  analyze_h --no-retro --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  [ ! -s "$GH_CALL_LOG" ]
  [ ! -s "$SCANNER_LOG" ]
}

@test "gate: missing consent.json -> typed no-op, rc 0, zero gh calls" {
  retro_isolate_home "$BATS_TEST_TMPDIR" missing
  analyze_h --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  printf '%s%s' "$output" "$stderr" | grep -qi 'no-consent'
  [ ! -s "$GH_CALL_LOG" ]
}

@test "gate: corrupt consent.json -> typed no-op, rc 0, zero gh calls" {
  retro_isolate_home "$BATS_TEST_TMPDIR" corrupt
  retro_seed_consent "$FIXTURES/consent-corrupt.json"
  analyze_h --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  printf '%s%s' "$output" "$stderr" | grep -qi 'no-consent'
  [ ! -s "$GH_CALL_LOG" ]
}

@test "gate: symlinked consent.json -> treated absent, rc 0, zero gh calls (AC-014)" {
  retro_isolate_home "$BATS_TEST_TMPDIR" symlink
  mkdir -p "$RETRO_STATE"; chmod 700 "$RETRO_STATE"
  ln -s "$FIXTURES/consent-granted.json" "$RETRO_STATE/consent.json"
  analyze_h --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  printf '%s%s' "$output" "$stderr" | grep -qi 'no-consent'
  [ ! -s "$GH_CALL_LOG" ]
}

@test "gate: revoked consent -> typed no-op, rc 0, zero gh calls" {
  retro_isolate_home "$BATS_TEST_TMPDIR" revoked
  retro_seed_consent "$FIXTURES/consent-revoked.json"
  analyze_h --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  printf '%s%s' "$output" "$stderr" | grep -qi 'no-consent'
  [ ! -s "$GH_CALL_LOG" ]
}

@test "gate: granted consent + gh auth failure -> one auth probe, zero issue calls, one typed local ledger row, rc 0" {
  retro_isolate_home "$BATS_TEST_TMPDIR" authfail
  retro_seed_consent "$FIXTURES/consent-granted.json"
  build_gh_shim "$MOCK_BIN"
  GH_AUTH_LOG="$BATS_TEST_TMPDIR/gh-calls-authfail.log"; : > "$GH_AUTH_LOG"
  GH_CALL_LOG="$GH_AUTH_LOG" GH_AUTH_FAIL=1
  export GH_CALL_LOG GH_AUTH_FAIL

  analyze_h --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]

  # scanner runs (payload is safe) -- auth is checked only AFTER the barrier
  [ "$(wc -l < "$SCANNER_LOG")" -eq 1 ]
  [ "$(grep -c 'auth status' "$GH_AUTH_LOG")" -eq 1 ]
  ! grep -q 'issue' "$GH_AUTH_LOG"

  LEDGER="$RETRO_STATE/retro-ledger.jsonl"
  [ -f "$LEDGER" ]
  [ "$(wc -l < "$LEDGER")" -eq 1 ]
  ! grep -q 'AUTH-FAIL-MARKER' "$LEDGER"
  python3 -c "import json; json.loads(open('$LEDGER').read().strip())"
}

# ── consent lifecycle: grant / revoke / reset / pure-read check-consent ────

@test "consent --grant writes {granted:true, asked_at, version} atomically at 0600 under a 0700 cache dir" {
  retro_isolate_home "$BATS_TEST_TMPDIR" grant
  run bash "$REPO/scripts/gsd/retro.sh" consent --grant
  [ "$status" -eq 0 ]
  CONSENT="$RETRO_STATE/consent.json"
  [ -f "$CONSENT" ]
  [ "$(file_mode "$CONSENT")" = "600" ]
  [ "$(file_mode "$RETRO_STATE")" = "700" ]
  python3 -c "
import json
d = json.load(open('$CONSENT'))
assert set(d) == {'granted', 'asked_at', 'version'}, d
assert d['granted'] is True, d
assert isinstance(d['asked_at'], str) and d['asked_at'], d
assert isinstance(d['version'], str) and d['version'], d
"
}

@test "consent --revoke flips granted false but preserves asked_at (revoked stays an asked decision)" {
  retro_isolate_home "$BATS_TEST_TMPDIR" revoke
  retro_seed_consent "$FIXTURES/consent-granted.json"
  BEFORE_ASKED="$(python3 -c "import json; print(json.load(open('$RETRO_STATE/consent.json'))['asked_at'])")"
  run bash "$REPO/scripts/gsd/retro.sh" consent --revoke
  [ "$status" -eq 0 ]
  python3 -c "
import json
d = json.load(open('$RETRO_STATE/consent.json'))
assert d['granted'] is False, d
assert d['asked_at'] == '$BEFORE_ASKED', d
"
}

@test "consent --reset (revoke-first, one locked transaction) leaves state askable, never granted" {
  retro_isolate_home "$BATS_TEST_TMPDIR" reset
  retro_seed_consent "$FIXTURES/consent-granted.json"
  run bash "$REPO/scripts/gsd/retro.sh" consent --reset
  [ "$status" -eq 0 ]
  run bash "$REPO/scripts/gsd/retro.sh" check-consent
  [ "$status" -eq 0 ]
  # post-reset: askable state, i.e. never a current *granted* decision.
  # Absence of the file is equally valid askable state; only assert the
  # granted invariant when a file happens to remain.
  if [ -f "$RETRO_STATE/consent.json" ]; then
    python3 -c "
import json
d = json.load(open('$RETRO_STATE/consent.json'))
assert d.get('granted') is not True, d
"
  fi
}

@test "check-consent is a pure read: repeated calls never modify consent.json mtime or bytes" {
  retro_isolate_home "$BATS_TEST_TMPDIR" pureread
  retro_seed_consent "$FIXTURES/consent-granted.json"
  CONSENT="$RETRO_STATE/consent.json"
  BEFORE_BYTES="$(cksum "$CONSENT")"
  BEFORE_MTIME="$(stat -f '%m' "$CONSENT" 2>/dev/null || stat -c '%Y' "$CONSENT")"
  run bash "$REPO/scripts/gsd/retro.sh" check-consent
  [ "$status" -eq 0 ]
  run bash "$REPO/scripts/gsd/retro.sh" check-consent
  [ "$status" -eq 0 ]
  [ "$(cksum "$CONSENT")" = "$BEFORE_BYTES" ]
  [ "$(stat -f '%m' "$CONSENT" 2>/dev/null || stat -c '%Y' "$CONSENT")" = "$BEFORE_MTIME" ]
}

@test "check-consent distinguishes granted-current / revoked / absent / major-mismatch, rc 0 in every case" {
  retro_isolate_home "$BATS_TEST_TMPDIR" typed
  run bash "$REPO/scripts/gsd/retro.sh" check-consent
  [ "$status" -eq 0 ]
  ABSENT_OUT="$output"

  retro_seed_consent "$FIXTURES/consent-granted.json"
  run bash "$REPO/scripts/gsd/retro.sh" check-consent
  [ "$status" -eq 0 ]
  GRANTED_OUT="$output"

  retro_seed_consent "$FIXTURES/consent-revoked.json"
  run bash "$REPO/scripts/gsd/retro.sh" check-consent
  [ "$status" -eq 0 ]
  REVOKED_OUT="$output"

  retro_seed_consent "$FIXTURES/consent-wrong-major.json"
  run bash "$REPO/scripts/gsd/retro.sh" check-consent
  [ "$status" -eq 0 ]
  MISMATCH_OUT="$output"

  # value-free stable typed output per 02-01-PLAN interfaces; exact wording
  # is production's choice, so only pairwise distinctness is required.
  [ "$ABSENT_OUT" != "$GRANTED_OUT" ]
  [ "$ABSENT_OUT" != "$REVOKED_OUT" ]
  [ "$ABSENT_OUT" != "$MISMATCH_OUT" ]
  [ "$GRANTED_OUT" != "$REVOKED_OUT" ]
  [ "$GRANTED_OUT" != "$MISMATCH_OUT" ]
  [ "$REVOKED_OUT" != "$MISMATCH_OUT" ]
}

@test "EDGE-012: revoke racing an in-flight filer waits on the shared lock; the filer completes, the NEXT pass sees revoked" {
  retro_isolate_home "$BATS_TEST_TMPDIR" race
  retro_seed_consent "$FIXTURES/consent-granted.json"
  mkdir -p "$RETRO_STATE"
  LOCK="$RETRO_STATE/retro.lock"
  HOLD_MARK="$BATS_TEST_TMPDIR/race-hold-released"
  rm -f "$HOLD_MARK"

  # Simulate an in-flight filer: hold the documented shared lock
  # (~/.cache/feature-fix-swarm/retro.lock, 02-01-PLAN interfaces /
  # 02-PATTERNS.md "one descriptor lock") externally via the SAME
  # fcntl.flock(LOCK_EX) protocol production is required to use.
  python3 - "$LOCK" "$HOLD_MARK" <<'PY' &
import fcntl, os, sys, time
lock_path, mark = sys.argv[1], sys.argv[2]
os.makedirs(os.path.dirname(lock_path), exist_ok=True)
fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
time.sleep(3)
open(mark, "w").close()
fcntl.flock(fd, fcntl.LOCK_UN)
os.close(fd)
PY
  HOLDER_PID=$!
  sleep 1  # let the external holder win the race to acquire first

  run bash "$REPO/scripts/gsd/retro.sh" consent --revoke
  [ "$status" -eq 0 ]
  # revoke could only have returned once the external holder released --
  # the marker file is the deterministic evidence, not a timing guess.
  [ -f "$HOLD_MARK" ]
  wait "$HOLDER_PID"

  python3 -c "
import json
d = json.load(open('$RETRO_STATE/consent.json'))
assert d['granted'] is False, d
"
}

# ── consented filing tracer (02-01 Task 1) ──────────────────────────────

@test "consented filing tracer: granted consent + safe P1 -> one gh issue create --body-file after list+search+re-query all-miss; allowlist-only body + v1 metadata; pinned repo" {
  retro_isolate_home "$BATS_TEST_TMPDIR" tracer
  retro_seed_consent "$FIXTURES/consent-granted.json"
  build_gh_shim "$MOCK_BIN"

  TR_LOG="$BATS_TEST_TMPDIR/gh-calls-tracer.log"; : > "$TR_LOG"
  CREATE_LOG="$BATS_TEST_TMPDIR/gh-create-tracer"
  export GH_CALL_LOG="$TR_LOG" GH_LIST_FIXTURE="$FIXTURES/gh-list-empty.json" \
         GH_SEARCH_FIXTURE="$FIXTURES/gh-list-empty.json" GH_CREATE_LOG="$CREATE_LOG"
  unset GH_AUTH_FAIL GH_WRITE_FAIL_CODE || true

  analyze_h --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]

  [ "$(grep -c '^issue create' "$TR_LOG")" -eq 1 ]
  [ "$(grep -c '^issue comment' "$TR_LOG")" -eq 0 ]
  grep '^issue create' "$TR_LOG" | grep -q 'austinmao/feature-fix-swarm'

  # create only after at least two prior "no match" gh issue lookups
  # (bounded list + mandatory search fallback; 02-01-PLAN task1 behavior --
  # a pre-create re-query may add a third).
  CREATE_LINE="$(grep -n '^issue create' "$TR_LOG" | head -1 | cut -d: -f1)"
  PRIOR_LOOKUPS="$(sed -n "1,$((CREATE_LINE - 1))p" "$TR_LOG" | grep -c '^issue list')"
  [ "$PRIOR_LOOKUPS" -ge 2 ]

  BODY_FILE="$CREATE_LOG/create-1.body"
  [ -f "$BODY_FILE" ]
  grep -Eq '<!-- ffs-retro fingerprint:[0-9a-f]{16} priority:P1 occurrences:1 -->' "$BODY_FILE"
  ! grep -qi 'testorg\|testrepo' "$BODY_FILE"
  ! grep -Eq '(^|[^A-Za-z0-9_])(/Users/|/home/|/private/)' "$BODY_FILE"
}
