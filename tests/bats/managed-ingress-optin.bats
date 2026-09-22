#!/usr/bin/env bats
# Release B opt-in: the legacy runner refuses under FFS_MANAGED_INGRESS and the
# frontend shell forwards the managed ingress options to run_state.cli.

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  STUB="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB"
  # Capture the exact argv the frontend shell hands to the CLI module.
  cat > "$STUB/python3" <<'EOF'
#!/bin/sh
printf '%s\n' "$@" > "$CAPTURE"
exit 0
EOF
  chmod +x "$STUB/python3"
  export CAPTURE="$BATS_TEST_TMPDIR/argv.txt"
}

@test "gsd-run refuses the legacy runner when the managed ingress opt-in is set" {
  run env FFS_MANAGED_INGRESS=1 bash "$ROOT/scripts/gsd/gsd-run.sh" /gsd-quick "task"
  [ "$status" -eq 78 ]
  [[ "$output" == *"ffs-frontend.sh"* ]]
  [[ "$output" == *"describe-upstream-runtime"* ]]
}

@test "gsd-run opt-in disabled with 0 does not trip the guard" {
  run env FFS_MANAGED_INGRESS=0 bash "$ROOT/scripts/gsd/gsd-run.sh" /not-a-gsd-command
  [ "$status" -eq 2 ]
  [[ "$output" == *"unsupported GSD command"* ]]
}

@test "ffs-frontend forwards task-swarm, scope, draft, catalog and host options" {
  run env PATH="$STUB:$PATH" FFS_PHASE_SCOPE=3 FFS_ACCEPTANCE_DRAFT=/tmp/draft.json \
    FFS_REVIEW_MODEL_CATALOG=/tmp/models.json FFS_HOST_KIND=codex FFS_CODEX_RUNTIME_HOME=/tmp/home \
    CODEX_BIN=/tmp/codex GSD_MODEL_REQUEST='{"kind":"tier","name":"execution"}' FFS_HOST_TOKEN_RESERVATION=100K \
    bash "$ROOT/scripts/gsd/ffs-frontend.sh" task-swarm --select-file src/a.txt
  [ "$status" -eq 0 ]
  grep -A1 -x -- '--host-token-reservation' "$CAPTURE" | grep -qx '100K'
  grep -qx -- '--frontend' "$CAPTURE"
  grep -qx -- 'task-swarm' "$CAPTURE"
  grep -A1 -x -- '--scope' "$CAPTURE" | grep -qx '3'
  grep -A1 -x -- '--acceptance-draft' "$CAPTURE" | grep -qx '/tmp/draft.json'
  grep -A1 -x -- '--review-model-catalog' "$CAPTURE" | grep -qx '/tmp/models.json'
  grep -A1 -x -- '--host-binary' "$CAPTURE" | grep -qx '/tmp/codex'
  ! grep -qx -- '--host-credential-source' "$CAPTURE"
}

@test "ffs-frontend adds the Claude credential source only when set" {
  run env PATH="$STUB:$PATH" FFS_HOST_KIND=claude FFS_HOST_RUNTIME_HOME=/tmp/home FFS_HOST_BINARY=/tmp/claude \
    FFS_HOST_CREDENTIAL_SOURCE=/tmp/cred GSD_MODEL_REQUEST='{"kind":"tier","name":"execution"}' FFS_HOST_TOKEN_RESERVATION=100K \
    bash "$ROOT/scripts/gsd/ffs-frontend.sh" fix --select-file src/a.txt
  [ "$status" -eq 0 ]
  grep -A1 -x -- '--host-credential-source' "$CAPTURE" | grep -qx '/tmp/cred'
  grep -A1 -x -- '--host-runtime-home' "$CAPTURE" | grep -qx '/tmp/home'
}

@test "ffs-frontend names a missing host token reservation before invoking the CLI" {
  run env PATH="$STUB:$PATH" FFS_HOST_KIND=codex FFS_CODEX_RUNTIME_HOME=/tmp/home CODEX_BIN=/tmp/codex \
    GSD_MODEL_REQUEST='{"kind":"tier","name":"execution"}' \
    bash "$ROOT/scripts/gsd/ffs-frontend.sh" task-swarm --select-file src/a.txt
  [ "$status" -eq 2 ]
  [[ "$output" == *"FFS_HOST_TOKEN_RESERVATION is required"* ]]
  [ ! -e "$CAPTURE" ]
}

@test "the real CLI parser accepts the documented task-swarm argv (no python stub)" {
  # Empty objective makes frontend-start refuse INVALID_REQUEST after parsing and
  # before any state is touched; an argv the parser rejects never reaches it.
  run env FFS_HOST_KIND=codex FFS_CODEX_RUNTIME_HOME=/tmp/home CODEX_BIN=/tmp/codex \
    GSD_MODEL_REQUEST='{"kind":"tier","name":"execution"}' FFS_HOST_TOKEN_RESERVATION=100K \
    FFS_PHASE_SCOPE=1 FFS_ACCEPTANCE_DRAFT=/tmp/draft.json FFS_REVIEW_MODEL_CATALOG=/tmp/models.json \
    bash "$ROOT/scripts/gsd/ffs-frontend.sh" task-swarm --select-file src/a.txt
  [ "$status" -eq 2 ]
  [[ "$output" == *"INVALID_REQUEST"* ]]
  [[ "$output" != *"invalid token count"* ]]
  [[ "$output" != *"unrecognized arguments"* ]]
}

@test "ffs-frontend: GSD_RESUME=0 is a fresh start, 1 resumes, anything else refuses" {
  run env PATH="$STUB:$PATH" GSD_RESUME=0 bash "$ROOT/scripts/gsd/ffs-frontend.sh" fix --select-file src/a.txt
  [ "$status" -eq 0 ]
  ! grep -qx -- '--resume' "$CAPTURE"
  run env PATH="$STUB:$PATH" GSD_RESUME=1 bash "$ROOT/scripts/gsd/ffs-frontend.sh" fix --select-file src/a.txt
  [ "$status" -eq 0 ]
  grep -qx -- '--resume' "$CAPTURE"
  rm -f "$CAPTURE"
  run env PATH="$STUB:$PATH" GSD_RESUME=yes bash "$ROOT/scripts/gsd/ffs-frontend.sh" fix --select-file src/a.txt
  [ "$status" -eq 2 ]
  [[ "$output" == *"GSD_RESUME must be 0 or 1"* ]]
  [ ! -e "$CAPTURE" ]
}
