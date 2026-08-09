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
# Group B (Task 2 — leak scan, REQ-202a; audit rows 10-12; fixtures ONLY
# under tests/fixtures/leak-scan/ per AC-011):
#   B1  family-fires ×8: one fixture per family → rc 2, fixed output contract
#       `line N, key <name>, shape <class> — remedy: replace the literal
#       with a NAME in secret_names`
#   B2  substring-absence: stdout and stderr captured to SEPARATE files; the
#       fixture's secret value appears in NEITHER (REQ-202a mandated test)
#   B3  whitelist-holds: uses:@40hex pins + sha256:64hex digests interleaved
#       with registry lines → rc 0, zero findings (ordering pin, row 11)
#   B4  span-not-line (wall a8f9af82): whitelisted digest AND a second
#       credential on ONE line → the second credential still flags
#   B5  committed-finding: fixture repo COMMITS the secret-bearing file →
#       remedy states rotate-then-rewrite-history (row 10)
#   B6  key-identifier guard (wall 2f77cf3f): secret-shaped KEY prints as
#       <non-identifier key at line N>, its bytes absent from both streams
#   B7  value-free refusal (wall 6e10a021): credential-shaped scalar in a
#       registry → refusal carries no substring of it on either stream
#   B8  boundary negatives: hex run of 31 and base64 run of 39 → no finding
# Group C (Task 3 — detect/apply) follows.
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

# ── Group B: leak scan (REQ-202a) ──────────────────────────────────────────

LEAK_FAMILIES="hex base64 assignment url aws prefix jwt pem"

leak_dir() {
  printf '%s\n' "$ROOT/tests/fixtures/leak-scan"
}

shape_for() {
  case "$1" in
    hex) echo hex-run ;;
    base64) echo base64-run ;;
    assignment) echo secret-assignment ;;
    url) echo credential-url ;;
    aws) echo aws-access-key ;;
    prefix) echo provider-token-prefix ;;
    jwt) echo jwt ;;
    pem) echo pem-block ;;
  esac
}

@test "B1 family-fires x8: each REQ-202a family flags with the fixed contract" {
  for fam in $LEAK_FAMILIES; do
    echo "family: $fam"
    cp "$(leak_dir)/$fam.txt" "$BATS_TEST_TMPDIR/$fam.txt"
    run -2 bash "$ER" check --manifest "$BATS_TEST_TMPDIR/$fam.txt"
    [[ "$output" == *"line "* ]]
    [[ "$output" == *"key "* ]]
    [[ "$output" == *"shape $(shape_for "$fam")"* ]]
    [[ "$output" == *"remedy: replace the literal with a NAME in secret_names"* ]]
  done
}

@test "B2 substring-absence: secret value in NEITHER stdout NOR stderr" {
  for fam in $LEAK_FAMILIES; do
    echo "family: $fam"
    secret="$(cat "$(leak_dir)/$fam.secret")"
    cp "$(leak_dir)/$fam.txt" "$BATS_TEST_TMPDIR/$fam.txt"
    bash "$ER" check --manifest "$BATS_TEST_TMPDIR/$fam.txt" \
      > "$BATS_TEST_TMPDIR/scan-out" 2> "$BATS_TEST_TMPDIR/scan-err"
    echo $? > "$BATS_TEST_TMPDIR/scan-rc"
    [ "$(cat "$BATS_TEST_TMPDIR/scan-rc")" -eq 2 ]
    ! grep -qF "$secret" "$BATS_TEST_TMPDIR/scan-out"
    ! grep -qF "$secret" "$BATS_TEST_TMPDIR/scan-err"
  done
}

@test "B3 whitelist holds under interleaving: pins and digests are not findings" {
  run -0 bash "$ER" check --manifest "$(leak_dir)/whitelist-clean.txt"
  [[ "$output" != *"remedy: replace the literal"* ]]
}

@test "B4 span-not-line: second credential beside a whitelisted digest still flags" {
  cp "$(leak_dir)/mixed-span.txt" "$BATS_TEST_TMPDIR/mixed-span.txt"
  run -2 bash "$ER" check --manifest "$BATS_TEST_TMPDIR/mixed-span.txt"
  [[ "$output" == *"shape provider-token-prefix"* ]]
  [ "$(grep -c 'remedy:' <<<"$output")" -eq 1 ]
  secret="$(cat "$(leak_dir)/mixed-span.secret")"
  [[ "$output" != *"$secret"* ]]
}

@test "B5 committed finding: remedy states rotate-then-rewrite-history" {
  cp "$(leak_dir)/hex.txt" "$FIXTURE/leaky.yaml"
  git -C "$FIXTURE" add leaky.yaml
  git -C "$FIXTURE" -c user.email=t@t -c user.name=t commit -q -m leaky
  run -2 bash "$ER" check --manifest "$FIXTURE/leaky.yaml"
  [[ "$output" == *"rotate"* ]]
  [[ "$output" == *"rewrite history"* ]]
  # uncommitted copy of the same file gets the base remedy only
  cp "$(leak_dir)/hex.txt" "$BATS_TEST_TMPDIR/hex.txt"
  run -2 bash "$ER" check --manifest "$BATS_TEST_TMPDIR/hex.txt"
  [[ "$output" != *"rotate"* ]]
}

@test "B6 secret-shaped KEY renders as a placeholder, bytes absent from both streams" {
  secret="$(cat "$(leak_dir)/secret-key.secret")"
  cp "$(leak_dir)/secret-key.txt" "$BATS_TEST_TMPDIR/secret-key.txt"
  bash "$ER" check --manifest "$BATS_TEST_TMPDIR/secret-key.txt" \
    > "$BATS_TEST_TMPDIR/sk-out" 2> "$BATS_TEST_TMPDIR/sk-err"
  echo $? > "$BATS_TEST_TMPDIR/sk-rc"
  [ "$(cat "$BATS_TEST_TMPDIR/sk-rc")" -eq 2 ]
  grep -q "non-identifier key at line" "$BATS_TEST_TMPDIR/sk-out"
  ! grep -qF "$secret" "$BATS_TEST_TMPDIR/sk-out"
  ! grep -qF "$secret" "$BATS_TEST_TMPDIR/sk-err"
}

@test "B7 credential-shaped registry scalar: refusal carries no substring of it" {
  secret="$(cat "$(leak_dir)/credential-kind.secret")"
  cp "$(leak_dir)/credential-kind.txt" "$BATS_TEST_TMPDIR/credential-kind.txt"
  bash "$ER" check --manifest "$BATS_TEST_TMPDIR/credential-kind.txt" \
    > "$BATS_TEST_TMPDIR/ck-out" 2> "$BATS_TEST_TMPDIR/ck-err"
  echo $? > "$BATS_TEST_TMPDIR/ck-rc"
  [ "$(cat "$BATS_TEST_TMPDIR/ck-rc")" -ne 0 ]
  ! grep -qF "$secret" "$BATS_TEST_TMPDIR/ck-out"
  ! grep -qF "$secret" "$BATS_TEST_TMPDIR/ck-err"
}

@test "B8 boundary negatives: hex run of 31 and base64 run of 39 are not findings" {
  run -0 bash "$ER" check --manifest "$(leak_dir)/boundary-hex31.txt"
  run -0 bash "$ER" check --manifest "$(leak_dir)/boundary-b64-39.txt"
}
