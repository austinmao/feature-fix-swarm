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
  cmp -s lib/model_requests.py "$target/lib/model_requests.py"
  cmp -s scripts/hooks/credential-output-guard.sh "$target/scripts/hooks/credential-output-guard.sh"
}

@test "installer source never names legacy .codex/skills as a destination" {
  ! grep -E '(copytree|replace_tree|symlink_to).*\.codex.*/skills' lib/ffs_installer.py
}
