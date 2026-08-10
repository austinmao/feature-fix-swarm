#!/usr/bin/env bats
setup() {
 ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"; CI="$ROOT/scripts/gsd/ci-watch.sh"; REPO="$BATS_TEST_TMPDIR/repo"; BIN="$BATS_TEST_TMPDIR/bin"
 mkdir -p "$REPO/.planning/run-state" "$REPO/scripts/gsd" "$BIN"; cp "$ROOT/scripts/gsd/lifecycle.sh" "$ROOT/scripts/gsd/run-bounded.sh" "$REPO/scripts/gsd/"; cd "$REPO"; git init -q -b main; git -c user.email=t -c user.name=t commit -q --allow-empty -m init; export PATH="$BIN:$PATH"
 bash scripts/gsd/lifecycle.sh checkpoint ci waiting ci ci-completed '{"databaseId":42}' '["scripts/gsd/plan-wall.sh"]' '{"ci_reruns":2}'
}
mock(){ cat > "$BIN/gh" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" == *--json* ]]; then echo '{"databaseId":42,"status":"completed","conclusion":"failure","attempt":1}'; elif [[ "$*" == *--log-failed* ]]; then echo runner-lost; elif [[ "$*" == *rerun* ]]; then echo "$*" >> "$BATS_TEST_TMPDIR/reruns"; fi
EOF
chmod +x "$BIN/gh"; }
@test "uses pinned id and rerun budget is durable" { mock; run bash "$CI" evaluate --run-id ci; [ "$status" -eq 0 ]; [[ "$output" == *rerun:42* ]]; [ "$(jq -r .budgets.ci_reruns .planning/run-state/lifecycle-ci.json)" = 1 ]; }
@test "success becomes runnable" {
 cat > "$BIN/gh" <<'EOF'
#!/usr/bin/env bash
echo '{"databaseId":42,"status":"completed","conclusion":"success","attempt":1}'
EOF
 chmod +x "$BIN/gh"; run bash "$CI" evaluate --run-id ci; [ "$status" -eq 0 ]; [[ "$output" == *pass* ]]; [ "$(jq -r .state .planning/run-state/lifecycle-ci.json)" = runnable ]
}
