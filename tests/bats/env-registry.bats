#!/usr/bin/env bats
# env-registry.sh — spec-007 phase 2 (plans 02-01): the sole deterministic,
# atomic writer behind /ffs-init. Full case taxonomy lives HERE, not in the
# plan (plan-shape rule).
#
# ── Case taxonomy ──────────────────────────────────────────────────────────
# Group A (Task 1 — check tracer):
#   A1  live: `check` from the FFS checkout → rc 0, empty output (AC-005;
#       the ONLY live-repo case — everything else runs a fixture copy)
#   A2  fixture default resolution: flag-absent → $ROOT/config/environments.yaml
#   A3  schema: unknown `kind` → rc 1, names key+line+expected vocabulary,
#       never the got-bytes (wall 6e10a021 value-free contract)
#   A4  schema: duplicate environment name → rc 1
#   A5  schema: tier row missing `command` → rc 1 naming the key + shape
#   A6  schema: missing v1 marker line → rc 1 naming the expected marker
#   A7  referential (audit row 18): staging_instance → declared kind: staging
#       env; broken names BOTH sides + remedy; correct → rc 0
#   A8  delegation proof: fixture gates.py `_load_manifest_text` monkeypatched
#       to raise a sentinel ValueError → renderer consumed it (typed reason +
#       line ref present, sentinel bytes ABSENT — wall 6e10a021)
#   A9  stale `verified:` >90d → advisory line, rc 0 (PATH-005, never a gate)
#   A10 `check --manifest ""` → rc 1 + remedy, distinguished from flag-absent
#   A11 `render` → rc 3 + "render lands in phase 3 (REQ-302)" (RESEARCH A2)
#   A12 missing/unknown verb → usage on stderr, nonzero
#   A13 gh probe OPT-IN (wall 959b7514): plain check makes ZERO gh calls even
#       with an authed stub on PATH; --probe-gh authed → advisory line;
#       unauthed → silent skip; rc IDENTICAL in both shapes (EDGE-004)
# Group B (Task 2 — leak scan) and Group C (Task 3 — detect/apply) follow.
# ───────────────────────────────────────────────────────────────────────────

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  FIXTURE="$BATS_TEST_TMPDIR/fixture"
  mkdir -p "$FIXTURE/scripts/gsd" "$FIXTURE/lib" "$FIXTURE/config"
  # Fixture-copy idiom (waiver-record.bats / WR-140): the script resolves ROOT
  # from its own $BASH_SOURCE, so every case runs a COPY inside the fixture,
  # never the repo checkout.
  cp "$ROOT/scripts/gsd/env-registry.sh" "$FIXTURE/scripts/gsd/env-registry.sh"
  cp "$ROOT/lib/gates.py" "$FIXTURE/lib/gates.py"
  chmod +x "$FIXTURE/scripts/gsd/env-registry.sh"
  cp "$ROOT/config/environments.yaml" "$FIXTURE/config/environments.yaml"
  git -C "$FIXTURE" init -q -b main
  git -C "$FIXTURE" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
  ER="$FIXTURE/scripts/gsd/env-registry.sh"
}

teardown() {
  # chmod-locking cases (.env.production EDGE-003) restore perms first so
  # bats can clean BATS_TEST_TMPDIR.
  chmod -R u+rwx "$FIXTURE" 2>/dev/null
  return 0
}

# Minimal schema-valid registry; args allow poisoning single fields.
write_registry() {  # $1=path
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
  - name: stag
    kind: staging
test_tiers:
  - tier: fast
    command: true
surfaces:
  - surface: release
    staging_instance: stag
YAML
}

make_gh_stub() {  # $1 = rc for `gh auth status`
  mkdir -p "$FIXTURE/bin"
  cat > "$FIXTURE/bin/gh" <<STUB
#!/usr/bin/env bash
printf '%s\n' "\$1" >> "$FIXTURE/gh-calls.log"
case "\$1" in
  auth) exit $1 ;;
  api) printf '%s\n' '{"environments":[{"name":"prod","protection_rules":[]}]}'; exit 0 ;;
  *) exit 0 ;;
esac
STUB
  chmod +x "$FIXTURE/bin/gh"
  git -C "$FIXTURE" remote add origin https://github.com/example/fixture.git 2>/dev/null || true
}

# ── Group A: check tracer ──────────────────────────────────────────────────

@test "A1 live: check on FFS's own committed registry is green and silent" {
  run -0 bash "$ROOT/scripts/gsd/env-registry.sh" check
  [ -z "$output" ]
}

@test "A2 flag-absent --manifest resolves ROOT/config/environments.yaml" {
  run -0 bash "$ER" check
}

@test "A3 unknown kind: value-free refusal names key, line, expected vocabulary" {
  write_registry "$FIXTURE/config/environments.yaml"
  sed -i.bak 's/kind: staging/kind: bogusvalue123/' "$FIXTURE/config/environments.yaml"
  rm -f "$FIXTURE/config/environments.yaml.bak"
  run -1 bash "$ER" check
  [[ "$output" == *"kind"* ]]
  [[ "$output" == *"line"* ]]
  [[ "$output" == *"local|dev|staging|prod|preview"* ]]
  [[ "$output" != *"bogusvalue123"* ]]
}

@test "A4 duplicate environment name rejects" {
  write_registry "$FIXTURE/config/environments.yaml"
  sed -i.bak 's/name: stag$/name: local/; s/kind: staging/kind: local/; s/staging_instance: stag/staging_instance: none/' \
    "$FIXTURE/config/environments.yaml"
  rm -f "$FIXTURE/config/environments.yaml.bak"
  run -1 bash "$ER" check
  [[ "$output" == *"duplicate"* ]]
  [[ "$output" == *"name"* ]]
}

@test "A5 tier row missing command names the key and expected shape" {
  write_registry "$FIXTURE/config/environments.yaml"
  sed -i.bak '/command: true/d' "$FIXTURE/config/environments.yaml"
  rm -f "$FIXTURE/config/environments.yaml.bak"
  run -1 bash "$ER" check
  [[ "$output" == *"command"* ]]
  [[ "$output" == *"remedy"* ]]
}

@test "A6 missing v1 marker line rejects naming the expected marker" {
  write_registry "$FIXTURE/config/environments.yaml"
  sed -i.bak '/schema: ffs.environments\/v1/d' "$FIXTURE/config/environments.yaml"
  rm -f "$FIXTURE/config/environments.yaml.bak"
  run -1 bash "$ER" check
  [[ "$output" == *"ffs.environments/v1"* ]]
}

@test "A7 referential: staging_instance must reference a declared kind: staging env" {
  write_registry "$FIXTURE/config/environments.yaml"
  run -0 bash "$ER" check
  sed -i.bak 's/kind: staging/kind: dev/' "$FIXTURE/config/environments.yaml"
  rm -f "$FIXTURE/config/environments.yaml.bak"
  run -1 bash "$ER" check
  [[ "$output" == *"stag"* ]]
  [[ "$output" == *"staging_instance"* ]]
  [[ "$output" == *"remedy"* ]]
}

@test "A8 delegation: sentinel ValueError from gates._load_manifest_text is value-stripped" {
  cat >> "$FIXTURE/lib/gates.py" <<'PY'
def _load_manifest_text(text):
    raise ValueError("tab-indented line in surfaces block at line 42: 'SENTINEL_SECRET_BYTES'")
PY
  write_registry "$FIXTURE/config/environments.yaml"
  run -1 bash "$ER" check
  [[ "$output" == *"line 42"* ]]
  [[ "$output" == *"tab"* ]]
  [[ "$output" != *"SENTINEL_SECRET_BYTES"* ]]
}

@test "A9 stale verified >90d is an advisory, never a gate" {
  write_registry "$FIXTURE/config/environments.yaml"
  sed -i.bak 's/verified: null/verified: 2025-01-01/' "$FIXTURE/config/environments.yaml"
  rm -f "$FIXTURE/config/environments.yaml.bak"
  run -0 bash "$ER" check
  [[ "$output" == *"ADVISORY"* ]]
  [[ "$output" == *"90"* ]]
}

@test "A10 check --manifest '' rejects with remedy, distinct from flag-absent" {
  run -1 bash "$ER" check --manifest ""
  [[ "$output" == *"remedy"* ]]
  [[ "$output" == *"config/environments.yaml"* ]]
}

@test "A11 render is a stub naming phase 3" {
  run -3 bash "$ER" render
  [[ "$output" == *"render lands in phase 3 (REQ-302)"* ]]
}

@test "A12 missing and unknown verbs print usage on stderr, nonzero" {
  run -1 --separate-stderr bash "$ER"
  [[ "$stderr" == *"usage"* ]]
  [[ "$stderr" == *"detect|check|render|apply"* ]]
  run -1 --separate-stderr bash "$ER" frobnicate
  [[ "$stderr" == *"usage"* ]]
}

@test "A13a plain check makes ZERO gh calls even with an authed stub on PATH" {
  make_gh_stub 0
  PATH="$FIXTURE/bin:$PATH" run -0 bash "$ER" check
  [ ! -f "$FIXTURE/gh-calls.log" ]
}

@test "A13b --probe-gh: authed advisory vs unauthed silent skip, rc identical" {
  make_gh_stub 0
  PATH="$FIXTURE/bin:$PATH" run -0 bash "$ER" check --probe-gh
  [[ "$output" == *"ADVISORY"* ]]
  [[ "$output" == *"reviewer"* ]]
  rm -f "$FIXTURE/gh-calls.log"
  make_gh_stub 1
  PATH="$FIXTURE/bin:$PATH" run -0 bash "$ER" check --probe-gh
  [[ "$output" != *"ADVISORY"* ]]
}
