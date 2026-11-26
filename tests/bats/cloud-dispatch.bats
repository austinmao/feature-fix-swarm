#!/usr/bin/env bats
# cloud-dispatch.sh: wrapper resolution, exit codes and the verbs that need no provider.

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  CD="$ROOT/scripts/gsd/cloud-dispatch.sh"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO"
  git -C "$REPO" init -q -b main
  export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid
  export GATES_STORE="$BATS_TEST_TMPDIR/gates.json"
  unset FFS_LIB_DIR
}

@test "wrapper is executable and has a bash shebang" {
  [ -x "$CD" ]
  [ "$(head -n 1 "$CD")" = "#!/usr/bin/env bash" ]
}

@test "no verb is a usage error with exit 2" {
  run bash "$CD"
  [ "$status" -eq 2 ]
}

@test "unknown verb is a usage error with exit 2" {
  run bash "$CD" frobnicate
  [ "$status" -eq 2 ]
}

@test "bad run id is a usage error with exit 2" {
  run bash "$CD" status --run "../x" --root "$REPO"
  [ "$status" -eq 2 ]
}

@test "missing library is exit 78 with a named hint" {
  mkdir -p "$BATS_TEST_TMPDIR/lonely/scripts/gsd"
  cp "$CD" "$BATS_TEST_TMPDIR/lonely/scripts/gsd/cloud-dispatch.sh"
  run env HOME="$BATS_TEST_TMPDIR/nohome" FFS_LIB_DIR="" bash "$BATS_TEST_TMPDIR/lonely/scripts/gsd/cloud-dispatch.sh" doctor
  [ "$status" -eq 78 ]
  [[ "$output" == *FFS_LIB_DIR* ]]
}

@test "FFS_LIB_DIR takes precedence over the sibling lib" {
  mkdir -p "$BATS_TEST_TMPDIR/altlib"
  printf 'import sys\nprint("ALT-LIB", sys.argv[1:])\n' > "$BATS_TEST_TMPDIR/altlib/cloud_dispatch.py"
  run env FFS_LIB_DIR="$BATS_TEST_TMPDIR/altlib" bash "$CD" doctor
  [ "$status" -eq 0 ]
  [[ "$output" == "ALT-LIB ['doctor']" ]]
}

@test "env init copies the templates, commits nothing and says what to review" {
  run bash "$CD" env init --root "$REPO"
  [ "$status" -eq 0 ]
  [ -f "$REPO/.ffs/cloud/config.json" ]
  [ -x "$REPO/.ffs/cloud/setup.sh" ]
  [[ "$output" == *"git add"* ]]
  [ -z "$(git -C "$REPO" log --oneline 2>/dev/null)" ]
}

@test "env init keeps an existing file unless --force" {
  bash "$CD" env init --root "$REPO"
  echo '{"mine":true}' > "$REPO/.ffs/cloud/config.json"
  run bash "$CD" env init --root "$REPO"
  [ "$status" -eq 0 ]
  [[ "$output" == *"kept existing"* ]]
  [ "$(cat "$REPO/.ffs/cloud/config.json")" = '{"mine":true}' ]
  run bash "$CD" env init --root "$REPO" --force
  [[ "$(cat "$REPO/.ffs/cloud/config.json")" != '{"mine":true}' ]]
}

@test "env-spec prints a spec per provider and rejects an unknown one" {
  run bash "$CD" env-spec --provider claude --root "$REPO"
  [ "$status" -eq 0 ]
  [ -n "$output" ]
  run bash "$CD" env-spec --provider bogus --root "$REPO"
  [ "$status" -eq 2 ]
}

@test "doctor on an empty repo is refused (exit 3) and its lines start with CLOUD:" {
  run bash "$CD" doctor --root "$REPO"
  [ "$status" -eq 3 ]
  [[ "${lines[0]}" == CLOUD:* ]]
}

@test "an empty tasks file is a usage error" {
  printf '[]' > "$BATS_TEST_TMPDIR/tasks.json"
  run bash "$CD" start --run r1 --tasks "$BATS_TEST_TMPDIR/tasks.json" --policy auto --root "$REPO"
  [ "$status" -eq 2 ]
  [[ "$output" == *BAD_TASKS_FILE* ]]
}

@test "start in a repo with no commit is refused with a CLOUD-REFUSED line" {
  cat > "$BATS_TEST_TMPDIR/tasks.json" <<'JSON'
[{"id":"t1","title":"t","prompt":"p","tier":"execution","model_request":{"kind":"tier","name":"execution"},
  "owns":["src/**"],"owns_source":"annotated","est_minutes":10,"needs_secrets":[],"local_only":false,
  "local_reason":"","depends_on":[],"kind":"build","verify_cmd":"true"}]
JSON
  run bash "$CD" start --run r1 --tasks "$BATS_TEST_TMPDIR/tasks.json" --policy auto --root "$REPO"
  [ "$status" -eq 3 ]
  [[ "$output" == CLOUD-REFUSED:* ]]
}

@test "orchestrate accepts --wait-lock and a bad value is a usage error" {
  run bash "$CD" orchestrate --run nope --wait-lock 1 --root "$REPO"
  [ "$status" -ne 2 ]
  run bash "$CD" orchestrate --run nope --wait-lock soon --root "$REPO"
  [ "$status" -eq 2 ]
}

@test "adopt needs exactly one of --oid and --remote-id" {
  run bash "$CD" adopt --run r1 --task t1 --root "$REPO"
  [ "$status" -ne 0 ]
  [[ "$output" != *Traceback* ]]
}
