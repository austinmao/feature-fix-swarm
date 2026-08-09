#!/usr/bin/env bats
# digest.sh — G12 observability digest (spec-008 REQ-701/702/703, AC-010/AC-011).
#
# Behavioral taxonomy for the nine contracted event classes lives HERE, not in
# plan prose. Fixtures are WR-140 fixture-local copies: digest.sh + lib/ are
# copied into a throwaway git repo and GATES_STORE/RUN_STATE_DB pin every
# store to the fixture — nothing resolves to the developer's real ledger.
# Zero live network (AC-010): a failing `gh` stub shadows any real gh first
# on PATH; DIGEST_NOTIFY_CMD is only ever a fixture script.
#
# Cursor contract (wall a376e0b4, amended OQ-4): tie-safe POSITIONS, never
# timestamps — evidence lists {count, last_fp}; run-state sqlite rowid;
# scan-tamper git sha; drift last-emitted fingerprint.

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  REPO="$BATS_TEST_TMPDIR/repo"
  MOCK_BIN="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$REPO/scripts/gsd" "$REPO/lib" "$MOCK_BIN" "$REPO/.feature-fix-swarm"
  cp "$ROOT/scripts/gsd/digest.sh" "$REPO/scripts/gsd/digest.sh"
  cp "$ROOT/lib/gates.py" "$REPO/lib/gates.py"
  cp "$ROOT/lib/evidence_events.py" "$REPO/lib/evidence_events.py"
  cp -R "$ROOT/lib/run_state" "$REPO/lib/run_state"
  chmod +x "$REPO/scripts/gsd/digest.sh"
  SCRIPT="$REPO/scripts/gsd/digest.sh"
  STORE="$REPO/.feature-fix-swarm/evidence.json"
  CURSOR="$REPO/.feature-fix-swarm/digest-cursor.json"
  BASELINE="$REPO/.feature-fix-swarm/drift-baseline.json"
  DB="$BATS_TEST_TMPDIR/runs.db"
  export GATES_STORE="$STORE" RUN_STATE_DB="$DB"
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
  export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t \
         GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
  unset DIGEST_NOTIFY_CMD || true
  # AC-010 zero live network: gh always resolves to a failing stub unless a
  # test overwrites it with a canned-response stub.
  printf '#!/usr/bin/env bash\nexit 64\n' > "$MOCK_BIN/gh"
  chmod +x "$MOCK_BIN/gh"
  export PATH="$MOCK_BIN:$PATH"
  cd "$REPO" || return 1
  git init -q
  git config user.email t@t
  git config user.name t
  echo init > README.md
  git add README.md
  git commit -qm init
}

seed_waiver() { # seed_waiver [run_id] [gate] [env_var] — the real producer path
  python3 "$REPO/lib/gates.py" waiver --run-id "${1:-spec-008}" \
    --gate "${2:-canary-gate}" --env-var "${3:-CANARY_GATE=off}"
}

snapshot_store() { cp "$STORE" "$BATS_TEST_TMPDIR/store.before"; }
assert_store_unchanged() { cmp -s "$STORE" "$BATS_TEST_TMPDIR/store.before"; }

# ── Task 1: waiver class end to end (tracer) ────────────────────────────────

@test "seeded waiver emits exactly one line, creates cursor, second run silent (PATH-002)" {
  seed_waiver
  snapshot_store
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  [ "$(echo "$output" | grep -c '^waiver ')" -eq 1 ]
  echo "$output" | grep '^waiver ' | grep -q 'run_id=spec-008'
  echo "$output" | grep '^waiver ' | grep -q 'gate=canary-gate'
  [ -f "$CURSOR" ]
  assert_store_unchanged
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  ! echo "$output" | grep -q '^waiver '
  assert_store_unchanged
}

@test "empty store prints no events and exits 0" {
  [ ! -f "$STORE" ]
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  echo "$output" | grep -q '^no events$'
}

@test "store bytes are byte-identical after any digest run (read-only, T-04-06)" {
  seed_waiver
  snapshot_store
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  assert_store_unchanged
  run bash "$SCRIPT" --daily
  [ "$status" -eq 0 ]
  assert_store_unchanged
}

@test "DIGEST_NOTIFY_CMD delivers the class lines on stdin and cursor advances" {
  seed_waiver
  NOTIFY_OUT="$BATS_TEST_TMPDIR/notified.txt"
  cat > "$BATS_TEST_TMPDIR/notify.sh" <<EOF
#!/usr/bin/env bash
cat >> "$NOTIFY_OUT"
EOF
  chmod +x "$BATS_TEST_TMPDIR/notify.sh"
  export DIGEST_NOTIFY_CMD="$BATS_TEST_TMPDIR/notify.sh"
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  grep -q '^waiver ' "$NOTIFY_OUT"
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  # cursor advanced: notify file still holds exactly one waiver line
  [ "$(grep -c '^waiver ' "$NOTIFY_OUT")" -eq 1 ]
}

@test "notify failure retains cursor, exits 0, next run redelivers (T-04-08)" {
  seed_waiver
  printf '#!/usr/bin/env bash\nexit 1\n' > "$BATS_TEST_TMPDIR/notify.sh"
  chmod +x "$BATS_TEST_TMPDIR/notify.sh"
  export DIGEST_NOTIFY_CMD="$BATS_TEST_TMPDIR/notify.sh"
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  echo "$output" | grep -q '^waiver '
  # redelivery: failed notify must NOT have advanced the waiver cursor
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  echo "$output" | grep -q '^waiver '
  # once transport recovers, delivery happens exactly once more
  NOTIFY_OUT="$BATS_TEST_TMPDIR/notified.txt"
  cat > "$BATS_TEST_TMPDIR/notify.sh" <<EOF
#!/usr/bin/env bash
cat >> "$NOTIFY_OUT"
EOF
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  [ "$(grep -c '^waiver ' "$NOTIFY_OUT")" -eq 1 ]
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  [ "$(grep -c '^waiver ' "$NOTIFY_OUT")" -eq 1 ]
}

@test "unattributed waiver emits with its literal label, no fabricated join (Pitfall 5)" {
  seed_waiver unattributed some-gate SOME=off
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  echo "$output" | grep '^waiver ' | grep -q 'run_id=unattributed'
  ! echo "$output" | grep '^waiver ' | grep -q 'run_id=spec-'
}

@test "tie-safety: identical-ts rows neither drop nor double-deliver (wall a376e0b4)" {
  python3 - "$STORE" <<'EOF'
import json, sys
rows = [{"run_id": "spec-008", "gate": "g1", "env_var": "A=1", "ts": 100.0},
        {"run_id": "spec-008", "gate": "g2", "env_var": "B=1", "ts": 100.0}]
open(sys.argv[1], "w").write(json.dumps({"waivers": rows}))
EOF
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  [ "$(echo "$output" | grep -c '^waiver ')" -eq 2 ]
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  ! echo "$output" | grep -q '^waiver '
  # a third row appended after (any ts, even an EARLIER one) emits exactly once
  python3 - "$STORE" <<'EOF'
import json, sys
data = json.load(open(sys.argv[1]))
data["waivers"].append({"run_id": "spec-008", "gate": "g3", "env_var": "C=1", "ts": 50.0})
open(sys.argv[1], "w").write(json.dumps(data))
EOF
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  [ "$(echo "$output" | grep -c '^waiver ')" -eq 1 ]
  echo "$output" | grep '^waiver ' | grep -q 'gate=g3'
}

@test "cursor fingerprint mismatch re-inits to end-of-list with one stderr note, no re-emission storm" {
  seed_waiver
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  # rewrite history: replace the emitted row with a different one
  python3 - "$STORE" <<'EOF'
import json, sys
open(sys.argv[1], "w").write(json.dumps(
    {"waivers": [{"run_id": "spec-008", "gate": "rewritten", "env_var": "X=1", "ts": 1.0}]}))
EOF
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  ! echo "$output" | grep -q '^waiver '
  echo "$output" | grep -qi 'mismatch'
}

@test "usage error is the only nonzero exit" {
  run bash "$SCRIPT" --bogus
  [ "$status" -eq 2 ]
  run bash "$SCRIPT"
  [ "$status" -eq 2 ]
}
