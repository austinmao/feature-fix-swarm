#!/usr/bin/env bats

setup() {
  cd "$BATS_TEST_DIRNAME/../.."
}

@test "setup is a strict wrapper around the scope-aware installer" {
  run bash setup.sh --doctor --json
  [ "$status" -eq 2 ]
  python3 -c 'import json,sys; assert json.load(sys.stdin)["schema"] == "ffs.doctor/v1"' <<<"$output"
}

@test "project scope requires an explicit project directory" {
  run bash setup.sh --scope project
  [ "$status" -eq 2 ]
  [[ "$output" == *"--project-dir"* ]]
}

@test "legacy consumer reconciliation remains available" {
  target="$BATS_TEST_TMPDIR/consumer"
  mkdir -p "$target"
  run bash setup.sh --reconcile-consumer "$target"
  [ "$status" -eq 0 ]
  cmp -s scripts/gsd/gsd-run.sh "$target/scripts/gsd/gsd-run.sh"
  cmp -s scripts/gsd/codex-runtime-bundle.py "$target/scripts/gsd/codex-runtime-bundle.py"
  cmp -s scripts/gsd/consume-danger-grant.py "$target/scripts/gsd/consume-danger-grant.py"
  cmp -s scripts/gsd/plan-adversary.sh "$target/scripts/gsd/plan-adversary.sh"
  cmp -s scripts/gsd/qa-coverage-adversary.sh "$target/scripts/gsd/qa-coverage-adversary.sh"
  cmp -s scripts/gsd/scope-drift-gate.sh "$target/scripts/gsd/scope-drift-gate.sh"
  cmp -s scripts/gsd/sync-drift-check.sh "$target/scripts/gsd/sync-drift-check.sh"
  cmp -s lib/model_requests.py "$target/lib/model_requests.py"
  cmp -s scripts/hooks/credential-output-guard.sh "$target/scripts/hooks/credential-output-guard.sh"
}

@test "consumer reconciliation preserves declared forks and copies every other drift surface file" {
  target="$BATS_TEST_TMPDIR/consumer"
  mkdir -p "$target/scripts/gsd"
  printf '%s\n' 'security-model-fence.sh consumer-specific no-op' > "$target/scripts/gsd/fork-allowlist.txt"
  printf '%s\n' '# preserved fork' > "$target/scripts/gsd/security-model-fence.sh"

  run bash setup.sh --reconcile-consumer "$target"
  [ "$status" -eq 0 ]
  grep -Fx '# preserved fork' "$target/scripts/gsd/security-model-fence.sh"
  for source_file in scripts/gsd/*.sh scripts/gsd/*.py; do
    name="${source_file##*/}"
    [ "$name" = security-model-fence.sh ] && continue
    cmp -s "$source_file" "$target/scripts/gsd/$name"
  done
}

@test "consumer reconciliation refuses symlinked destination ancestors" {
  target="$BATS_TEST_TMPDIR/consumer"
  outside="$BATS_TEST_TMPDIR/outside"
  mkdir -p "$target" "$outside"
  printf '%s\n' 'keep' > "$outside/sentinel"
  ln -s "$outside" "$target/scripts"

  run bash setup.sh --reconcile-consumer "$target"

  [ "$status" -eq 1 ]
  [ "$(cat "$outside/sentinel")" = keep ]
  [ ! -e "$outside/gsd/gsd-run.sh" ]
}

@test "consumer reconciliation refuses a symlinked final destination" {
  target="$BATS_TEST_TMPDIR/consumer"
  outside="$BATS_TEST_TMPDIR/outside"
  mkdir -p "$target/scripts/gsd" "$outside"
  printf '%s\n' 'keep' > "$outside/victim"
  ln -s "$outside/victim" "$target/scripts/gsd/gsd-run.sh"

  run bash setup.sh --reconcile-consumer "$target"

  [ "$status" -eq 1 ]
  [ "$(cat "$outside/victim")" = keep ]
}

@test "installer source never names legacy .codex/skills as a destination" {
  ! grep -E '(copytree|replace_tree|symlink_to).*\.codex.*/skills' lib/ffs_installer.py
}

@test "consumer reconciliation installs the finding schema with correct (non-executable) bits" {
  target="$BATS_TEST_TMPDIR/consumer"
  mkdir -p "$target"

  run bash setup.sh --reconcile-consumer "$target"
  [ "$status" -eq 0 ]

  cmp -s schemas/review-finding.schema.json "$target/schemas/review-finding.schema.json"
  # plan-wall.sh (spec-004 INT-003) is a shell script — the drift-surface
  # glob always adds +x. The schema is JSON data, not runnable, and must
  # keep the source's own (non-executable) mode instead of always gaining +x.
  stat_mode() { stat -f '%Lp' "$1" 2>/dev/null || stat -c '%a' "$1"; }
  [ "$(stat_mode "$target/schemas/review-finding.schema.json")" = "$(stat_mode schemas/review-finding.schema.json)" ]
  [ ! -x "$target/schemas/review-finding.schema.json" ]
  cmp -s scripts/gsd/plan-wall.sh "$target/scripts/gsd/plan-wall.sh"
  cmp -s scripts/gsd/model-probe-lib.sh "$target/scripts/gsd/model-probe-lib.sh"
  [ -x "$target/scripts/gsd/plan-wall.sh" ]
  [ -x "$target/scripts/gsd/model-probe-lib.sh" ]
}
