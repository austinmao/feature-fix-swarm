#!/usr/bin/env bats
# test-tier.sh — spec-007 phase 3 (plan 03-02, REQ-303): the single source of
# CI test commands. Mirrors review-tier.sh's idiom but NOT its fail-safe-exit-0
# (RESEARCH Pitfall 3): missing registry → exit 3, never a silent success.
#
# ── Case taxonomy ──────────────────────────────────────────────────────────
# T1  fast tier on an FFS-shaped fixture → exactly the registered command
#     line(s) on stdout, stderr empty (stream purity)
# T2  two rows for the same tier → both printed, declaration order (EDGE-006)
# T3  inline comment on a command scalar → stripped (_manifest_scalar
#     semantics: whitespace-then-# split)
# T4  unknown tier token → usage on stderr, exit 2, stdout empty
# T5  missing registry, explicit arg-2 shape → exit 3 + reason on stderr,
#     stdout empty
# T6  missing registry, default-path shape → exit 3 (Pitfall 3: never 0)
# T7  known tier, zero rows in registry → exit 0, stdout empty (OQ6 pin)
# T8  arg-2 registry override honored before any default resolution
# T9  block-boundary latch (wall c26a40e9): decoy `- tier:`/`command:` rows
#     inside environments: and surfaces: never leak into lookup
# T10 no args / extra args → usage, exit 2
# ───────────────────────────────────────────────────────────────────────────

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  FIXTURE="$BATS_TEST_TMPDIR/fixture"
  mkdir -p "$FIXTURE/scripts/gsd" "$FIXTURE/config"
  # Fixture-copy idiom (02-01-PLAN.md:35): the script resolves ROOT from its
  # own $BASH_SOURCE, so every case runs a COPY inside the fixture.
  cp "$ROOT/scripts/gsd/test-tier.sh" "$FIXTURE/scripts/gsd/test-tier.sh"
  chmod +x "$FIXTURE/scripts/gsd/test-tier.sh"
  git -C "$FIXTURE" init -q -b main
  git -C "$FIXTURE" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
  TT="$FIXTURE/scripts/gsd/test-tier.sh"
}

# FFS-shaped registry (mirrors config/environments.yaml at 14af77a).
write_tiers() {  # $1=path
  cat > "$1" <<'YAML'
# config/environments.yaml
# schema: ffs.environments/v1
environments:
  - name: local
    kind: local
    base_url: none
    secret_names: []
    verified: null
    test_tier: fast

test_tiers:
  - tier: fast
    command: python3 -m pytest lib/tests/test_gates.py -q
    covers:
      - lib/**
  - tier: full
    command: python3 -m pytest lib/ tests/ -q
    covers:
      - lib/**
      - tests/**

surfaces:
  - surface: release
    staging_instance: none
YAML
}

@test "T1 fast tier prints exactly the registered command, stdout only" {
  write_tiers "$FIXTURE/config/environments.yaml"
  run -0 --separate-stderr bash "$TT" fast
  [ "$output" = "python3 -m pytest lib/tests/test_gates.py -q" ]
  [ -z "$stderr" ]
}

@test "T2 two rows for one tier print both, declaration order (EDGE-006)" {
  cat > "$FIXTURE/config/environments.yaml" <<'YAML'
test_tiers:
  - tier: fast
    command: echo first
  - tier: full
    command: echo other
  - tier: fast
    command: echo second
YAML
  run -0 bash "$TT" fast
  [ "${lines[0]}" = "echo first" ]
  [ "${lines[1]}" = "echo second" ]
  [ "${#lines[@]}" -eq 2 ]
}

@test "T3 inline comment on a command scalar is stripped" {
  cat > "$FIXTURE/config/environments.yaml" <<'YAML'
test_tiers:
  - tier: fast
    command: pytest -q  # the quick loop
YAML
  run -0 bash "$TT" fast
  [ "$output" = "pytest -q" ]
}

@test "T4 unknown tier token: usage on stderr, exit 2, stdout empty" {
  write_tiers "$FIXTURE/config/environments.yaml"
  run -2 --separate-stderr bash "$TT" bogus
  [ -z "$output" ]
  [[ "$stderr" == *"Usage"* ]]
  [[ "$stderr" == *"fast|full|nightly|live"* ]]
}

@test "T5 missing registry via explicit arg 2: exit 3 + reason on stderr" {
  run -3 --separate-stderr bash "$TT" fast "$FIXTURE/config/no-such.yaml"
  [ -z "$output" ]
  [[ "$stderr" == *"registry"* ]]
}

@test "T6 missing registry via default path: exit 3, never a silent 0" {
  rm -f "$FIXTURE/config/environments.yaml"
  run -3 --separate-stderr bash "$TT" fast
  [ -z "$output" ]
  [[ "$stderr" == *"registry"* ]]
}

@test "T7 known tier with zero rows: exit 0, stdout empty (OQ6)" {
  write_tiers "$FIXTURE/config/environments.yaml"
  run -0 --separate-stderr bash "$TT" nightly
  [ -z "$output" ]
}

@test "T8 arg-2 registry override wins over the default resolution" {
  write_tiers "$FIXTURE/config/environments.yaml"
  cat > "$BATS_TEST_TMPDIR/alt.yaml" <<'YAML'
test_tiers:
  - tier: fast
    command: echo override-wins
YAML
  run -0 bash "$TT" fast "$BATS_TEST_TMPDIR/alt.yaml"
  [ "$output" = "echo override-wins" ]
}

@test "T9 block-boundary latch: decoy rows outside test_tiers never leak (wall c26a40e9)" {
  cat > "$FIXTURE/config/environments.yaml" <<'YAML'
environments:
  - name: local
    kind: local
  - tier: fast
    command: echo DECOY-ENV
test_tiers:
  - tier: fast
    command: echo REAL
surfaces:
  - tier: fast
    command: echo DECOY-SURF
YAML
  run -0 bash "$TT" fast
  [ "$output" = "echo REAL" ]
}

@test "T10 no args and extra args: usage, exit 2" {
  write_tiers "$FIXTURE/config/environments.yaml"
  run -2 --separate-stderr bash "$TT"
  [[ "$stderr" == *"Usage"* ]]
  run -2 bash "$TT" fast "$FIXTURE/config/environments.yaml" extra
}
