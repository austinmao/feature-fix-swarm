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

# ── Task 2: remaining store classes, loop-cap producer, run_id join ─────────

@test "tripped-rung emits exactly once, cursor is a window fingerprint not a timestamp" {
  # 20 consecutive fails = tripped (gates.py:226). Two events share an
  # identical recorded_at on purpose: the cursor must be a content
  # fingerprint of the window, never a recorded_at comparison (wall ba54308a).
  python3 - "$STORE" <<'EOF'
import json, sys
events = [{"outcome": "fail", "recorded_at": 100.0} for _ in range(20)]
events[5]["recorded_at"] = 100.0  # explicit tie
open(sys.argv[1], "w").write(json.dumps(
    {"_degradation": {"rungs": {"review-rung": {"events": events, "opportunities": 3}},
                      "invocations": [], "mappings": {}}}))
EOF
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  [ "$(echo "$output" | grep -c '^tripped-rung ')" -eq 1 ]
  echo "$output" | grep '^tripped-rung ' | grep -q 'rung=review-rung'
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  ! echo "$output" | grep -q '^tripped-rung '
}

@test "loop-cap: the real gates.py producer feeds the digest exactly once" {
  # end to end through the OQ-1 producer: cap-crossing loop-round durably
  # appends the typed event; digest emits it once.
  run python3 "$REPO/lib/gates.py" loop-round spec-008 wall:p1 --max 1
  [ "$status" -eq 0 ]
  run python3 "$REPO/lib/gates.py" loop-round spec-008 wall:p1 --max 1
  [ "$status" -eq 1 ]
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  [ "$(echo "$output" | grep -c '^loop-cap ')" -eq 1 ]
  echo "$output" | grep '^loop-cap ' | grep -q 'run_id=spec-008'
  echo "$output" | grep '^loop-cap ' | grep -q 'loop=wall:p1'
  echo "$output" | grep '^loop-cap ' | grep -q 'round=2'
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  ! echo "$output" | grep -q '^loop-cap '
}

@test "budget-breach emits exactly once via sqlite rowid cursor" {
  PYTHONPATH="$REPO/lib" python3 -m run_state.cli start --skill feature --objective x --tokens 10 > "$BATS_TEST_TMPDIR/start.json"
  RS_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_id"])' "$BATS_TEST_TMPDIR/start.json")"
  PYTHONPATH="$REPO/lib" python3 -m run_state.cli update "$RS_ID" --tokens 20 | grep -q '^BUDGET-BREACH:'
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  [ "$(echo "$output" | grep -c '^budget-breach ')" -eq 1 ]
  echo "$output" | grep '^budget-breach ' | grep -q "runstore_id=$RS_ID"
  echo "$output" | grep '^budget-breach ' | grep -q 'run_id=unmapped'
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  ! echo "$output" | grep -q '^budget-breach '
}

@test "AC-011 join: budget row and waiver row name the SAME ledger run_id via the mapping" {
  PYTHONPATH="$REPO/lib" python3 -m run_state.cli start --skill feature --objective x --tokens 10 > "$BATS_TEST_TMPDIR/start.json"
  RS_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_id"])' "$BATS_TEST_TMPDIR/start.json")"
  python3 "$REPO/lib/gates.py" map-run --ledger-run-id spec-008 --runstore-id "$RS_ID"
  PYTHONPATH="$REPO/lib" python3 -m run_state.cli update "$RS_ID" --tokens 20 | grep -q '^BUDGET-BREACH:'
  seed_waiver spec-008
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  echo "$output" | grep '^budget-breach ' | grep -q 'run_id=spec-008'
  echo "$output" | grep '^budget-breach ' | grep -q "runstore_id=$RS_ID"
  echo "$output" | grep '^waiver ' | grep -q 'run_id=spec-008'
}

@test "finisher-skipped emits with pr join key; lock-trace variant emits honestly without an invented run" {
  python3 "$REPO/lib/evidence_events.py" finisher-skipped --run-id spec-008 --pr 7
  python3 "$REPO/lib/evidence_events.py" finisher-skipped --run-id unattributed --pr 8 \
    --lock-path /tmp/finish.lock --holder-pid 5
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  [ "$(echo "$output" | grep -c '^finisher-skipped ')" -eq 2 ]
  echo "$output" | grep '^finisher-skipped ' | grep 'run_id=spec-008' | grep -q 'pr=7'
  echo "$output" | grep '^finisher-skipped ' | grep 'run_id=unattributed' | grep -q 'lock_path=/tmp/finish.lock'
  echo "$output" | grep '^finisher-skipped ' | grep 'run_id=unattributed' | grep -q 'holder_pid=5'
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  ! echo "$output" | grep -q '^finisher-skipped '
}

@test "promotion emits once and carries the artifact sha gh join key" {
  SHA="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  python3 - "$STORE" "$SHA" <<'EOF'
import json, sys
open(sys.argv[1], "w").write(json.dumps({"_promotions": {"spec-008": [
    {"from_env": "staging", "to_env": "prod", "surface": "web",
     "artifact": sys.argv[2], "evidence_ids": [], "recorded_at": 100.0,
     "expires_at": 200.0}]}}))
EOF
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  [ "$(echo "$output" | grep -c '^promotion ')" -eq 1 ]
  echo "$output" | grep '^promotion ' | grep -q 'run_id=spec-008'
  echo "$output" | grep '^promotion ' | grep -q "artifact=$SHA"
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  ! echo "$output" | grep -q '^promotion '
}

@test "rollback-dryrun emits once with its REQ-302 keys" {
  SHA="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  python3 "$REPO/lib/gates.py" rollback-dryrun --run-id spec-008 --surface web \
    --command "echo rollback" --exit-code 0 --artifact-sha "$SHA"
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  [ "$(echo "$output" | grep -c '^rollback-dryrun ')" -eq 1 ]
  echo "$output" | grep '^rollback-dryrun ' | grep -q 'run_id=spec-008'
  echo "$output" | grep '^rollback-dryrun ' | grep -q 'exit_code=0'
  echo "$output" | grep '^rollback-dryrun ' | grep -q "artifact_sha=$SHA"
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  ! echo "$output" | grep -q '^rollback-dryrun '
}

@test "mid-list class failure leaves earlier classes' cursor advances intact (Pitfall 4)" {
  seed_waiver
  run python3 "$REPO/lib/gates.py" loop-round spec-008 wall:p1 --max 1
  run python3 "$REPO/lib/gates.py" loop-round spec-008 wall:p1 --max 1
  NOTIFY_OUT="$BATS_TEST_TMPDIR/notified.txt"
  cat > "$BATS_TEST_TMPDIR/notify.sh" <<EOF
#!/usr/bin/env bash
input="\$(cat)"
if printf '%s\n' "\$input" | grep -q '^loop-cap '; then exit 1; fi
printf '%s\n' "\$input" >> "$NOTIFY_OUT"
EOF
  chmod +x "$BATS_TEST_TMPDIR/notify.sh"
  export DIGEST_NOTIFY_CMD="$BATS_TEST_TMPDIR/notify.sh"
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  echo "$output" | grep -q '^waiver '
  echo "$output" | grep -q '^loop-cap '
  # waiver advanced (delivered once), loop-cap retained and redelivered
  run bash "$SCRIPT" --immediate
  [ "$status" -eq 0 ]
  ! echo "$output" | grep -q '^waiver '
  echo "$output" | grep -q '^loop-cap '
  [ "$(grep -c '^waiver ' "$NOTIFY_OUT")" -eq 1 ]
}
