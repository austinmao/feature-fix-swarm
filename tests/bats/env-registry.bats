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
# Group C (Task 3 — detect/apply/declines; REQ-202/203; audit rows
# 25/28/30/32 LOCKED):
#   C1-C6  six fixture trees (vercel/wrangler/fly/compose/k8s/bare) →
#          proposal rows with per-row confidence: + evidence:; bare →
#          local-only, surfaces OMITTED
#   C7     monorepo (vercel+wrangler+fly): all rows, own evidence, none
#          auto-accepted (verified: null everywhere, EDGE-009)
#   C8     .env.staging/.env.production → rows + secret KEY NAMES only;
#          values absent from output; chmod-000 → evidence
#          "file present, unread" (EDGE-003)
#   C9     workflow environment: + doppler provider/config → rows
#   C10    tier classification per REQ-202's four-way rule
#   C11    porcelain: `git status --porcelain` EMPTY after detect (Pitfall 4)
#   C12    shared name-safety guard (wall a173dd6d): secret-shaped workflow
#          env name + secret-shaped registry env name absent from BOTH
#          streams of detect AND check
#   C13    apply happy path: registry + .ffs-init.json written; fresh
#          registry passes check; v1 marker on line 2; surfaces LAST; no tabs
#   C14    all-or-nothing: missing test_tiers → names key + expected shape,
#          NEITHER file exists (EDGE-008)
#   C15    leak-bearing answers → rc 2, neither file changed
#   C16    declines lifecycle: suppress exactly the keyed (heuristic,
#          evidence); ONE advisory naming count + --reset-declines; NEW
#          evidence re-proposed; --reset-declines empties atomically
#   C17    --update preserves verified: dates + operator-edited scalars,
#          adds only NEW rows
#   C18    regenerate guard: apply --yes over existing refuses naming
#          --update and --force; --force proceeds
#   C19    lock-before-validate (wall ca301d30): held lock → typed
#          ENV-REGISTRY-BUSY fail-fast, nothing interleaved
#   C20    write-target containment (wall 2965346c): config symlinked
#          outside the fixture → typed refusal, nothing at the link target
# Group D (03-02 Task 2 — render verb, REQ-302; audit rows 20/27 LOCKED):
#   D1  fresh render: exactly 5 ffs-*.yml + .github/dependabot.yml; six-token
#       substitution; zero {{ residue; ${{ ... }} expressions intact (OQ9)
#   D2  collision → proposal at .github/ffs-proposals/ (identical basename,
#       row 20), diff -u on stdout, every original byte-identical, no
#       proposal-suffixed filename in the workflows dir
#   D3  byte-identical existing target → up-to-date line, no proposal
#       (EDGE-007)
#   D4  dependabot present without github-actions ecosystem → advisory +
#       untouched; with it → silent skip (wall 3c6cb2e7)
#   D5  registry missing staging/prod kinds → 3 test workflows + typed skip
#       advisory naming the missing kind(s), rc 0 (OQ4)
#   D6  hostile registry value (credential-shaped env name, runtime-
#       generated) → value-free rc 2, nothing written (walls a173dd6d/6e10a021)
#   D7  rendered-candidate leak scan: bare hex in a generated template →
#       rc 2, NOTHING written (walls 9a586ab7 + REQ-202a)
#   D8  missing registry / missing templates dir → rc 1 typed refusal (OQ7)
#   D9  proposal-path collision (wall 969c0f3d): existing REGULAR file at the
#       proposal path is overwritten atomically; a SYMLINK there refuses via
#       the containment guard, nothing at the link target
#   D10 validate-ALL-then-write-ALL (walls 9a586ab7 + 1a28ec98): LAST
#       template carries a bad placeholder → refusal, ZERO files created
#   D11 both-sides leak scan (wall d118f505): existing workflow carries a
#       credential-shaped literal → content diff SUPPRESSED, value-free
#       notice naming finding class + proposal path, secret on NEITHER stream
#   D12 workflows-dir symlink (walls 6b6bd8bc/2965346c): typed refusal,
#       nothing at the link target
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

@test "A11 render usage: real synopsis, exit legend drops 'not implemented' (03-02/OQ7)" {
  # Phase 2 pinned render as a stub (rc 3); 03-02 implements it — this case
  # now pins the UPDATED usage contract instead.
  run -1 --separate-stderr bash "$ER"
  [[ "$stderr" == *"render"* ]]
  [[ "$stderr" == *"ffs-proposals"* ]]
  [[ "$stderr" != *"not implemented"* ]]
  [[ "$stderr" != *"stub"* ]]
  [[ "$stderr" == *"0 ok"* ]]
  [[ "$stderr" == *"2 leak finding"* ]]
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
    rc=0
    bash "$ER" check --manifest "$BATS_TEST_TMPDIR/$fam.txt" \
      > "$BATS_TEST_TMPDIR/scan-out" 2> "$BATS_TEST_TMPDIR/scan-err" || rc=$?
    echo "$rc" > "$BATS_TEST_TMPDIR/scan-rc"
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
  rc=0
  bash "$ER" check --manifest "$BATS_TEST_TMPDIR/secret-key.txt" \
    > "$BATS_TEST_TMPDIR/sk-out" 2> "$BATS_TEST_TMPDIR/sk-err" || rc=$?
  echo "$rc" > "$BATS_TEST_TMPDIR/sk-rc"
  [ "$(cat "$BATS_TEST_TMPDIR/sk-rc")" -eq 2 ]
  grep -q "non-identifier key at line" "$BATS_TEST_TMPDIR/sk-out"
  ! grep -qF "$secret" "$BATS_TEST_TMPDIR/sk-out"
  ! grep -qF "$secret" "$BATS_TEST_TMPDIR/sk-err"
}

@test "B7 credential-shaped registry scalar: refusal carries no substring of it" {
  secret="$(cat "$(leak_dir)/credential-kind.secret")"
  cp "$(leak_dir)/credential-kind.txt" "$BATS_TEST_TMPDIR/credential-kind.txt"
  rc=0
  bash "$ER" check --manifest "$BATS_TEST_TMPDIR/credential-kind.txt" \
    > "$BATS_TEST_TMPDIR/ck-out" 2> "$BATS_TEST_TMPDIR/ck-err" || rc=$?
  echo "$rc" > "$BATS_TEST_TMPDIR/ck-rc"
  [ "$(cat "$BATS_TEST_TMPDIR/ck-rc")" -ne 0 ]
  ! grep -qF "$secret" "$BATS_TEST_TMPDIR/ck-out"
  ! grep -qF "$secret" "$BATS_TEST_TMPDIR/ck-err"
}

@test "B8 boundary negatives: hex run of 31 and base64 run of 39 are not findings" {
  run -0 bash "$ER" check --manifest "$(leak_dir)/boundary-hex31.txt"
  run -0 bash "$ER" check --manifest "$(leak_dir)/boundary-b64-39.txt"
}

# ── Group C: detect proposals + apply atomic writer + declines ─────────────

commit_fixture() {
  git -C "$FIXTURE" add .
  git -C "$FIXTURE" -c user.email=t@t -c user.name=t commit -q -m fixture
}

build_vercel() { printf '{}\n' > "$FIXTURE/vercel.json"; }
build_wrangler() {
  printf 'name = "app"\n[env.staging]\nroute = "s"\n[env.prod]\nroute = "p"\n' \
    > "$FIXTURE/wrangler.toml"
}
build_fly() {
  printf 'app = "x"\n' > "$FIXTURE/fly.toml"
  printf 'app = "x"\n' > "$FIXTURE/fly.staging.toml"
}
build_compose() { printf 'services: {}\n' > "$FIXTURE/docker-compose.yml"; }
build_k8s() {
  mkdir -p "$FIXTURE/k8s/overlays/staging" "$FIXTURE/k8s/overlays/prod"
  touch "$FIXTURE/k8s/overlays/staging/kustomization.yaml" \
        "$FIXTURE/k8s/overlays/prod/kustomization.yaml"
}

write_answers() {  # $1=path
  cat > "$1" <<'YAML'
environments:
  - name: local
    kind: local
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

@test "C1 vercel fixture proposes prod + preview with evidence" {
  build_vercel
  run -0 bash "$ER" detect
  [[ "$output" == *"- name: prod"* ]]
  [[ "$output" == *"- name: preview"* ]]
  [[ "$output" == *"evidence: vercel.json"* ]]
  [[ "$output" == *"confidence:"* ]]
}

@test "C2 wrangler fixture proposes an env per [env.X] section with file:section evidence" {
  build_wrangler
  run -0 bash "$ER" detect
  [[ "$output" == *"- name: staging"* ]]
  [[ "$output" == *"- name: prod"* ]]
  [[ "$output" == *"evidence: wrangler.toml:[env.staging]"* ]]
  [[ "$output" == *"evidence: wrangler.toml:[env.prod]"* ]]
}

@test "C3 fly fixture proposes an env per file" {
  build_fly
  run -0 bash "$ER" detect
  [[ "$output" == *"- name: prod"* ]]
  [[ "$output" == *"- name: staging"* ]]
  [[ "$output" == *"fly.toml"* ]]
  [[ "$output" == *"fly.staging.toml"* ]]
}

@test "C4 compose fixture proposes local" {
  build_compose
  run -0 bash "$ER" detect
  [[ "$output" == *"- name: local"* ]]
  [[ "$output" == *"kind: local"* ]]
  [[ "$output" == *"evidence: docker-compose.yml"* ]]
}

@test "C5 k8s overlay dirs propose an env per dir" {
  build_k8s
  run -0 bash "$ER" detect
  [[ "$output" == *"- name: staging"* ]]
  [[ "$output" == *"- name: prod"* ]]
  [[ "$output" == *"k8s/overlays/staging/"* ]]
}

@test "C6 bare fixture: local-only proposal, surfaces omitted" {
  run -0 bash "$ER" detect
  [[ "$output" == *"- name: local"* ]]
  [[ "$output" != *$'\n'"surfaces:"* ]]
  [[ "$output" != *"- name: prod"* ]]
}

@test "C7 monorepo: all rows with their own evidence, none auto-accepted" {
  build_vercel; build_wrangler; build_fly
  run -0 bash "$ER" detect
  [[ "$output" == *"evidence: vercel.json"* ]]
  [[ "$output" == *"wrangler.toml:[env.staging]"* ]]
  [[ "$output" == *"fly.staging.toml"* ]]
  [[ "$output" == *"- name: preview"* ]]
  # every proposed row stays a proposal: verified is null everywhere
  ! grep -E 'verified: [0-9]' <<<"$output"
}

@test "C8 .env detection: key NAMES only; unreadable file present, unread (EDGE-003)" {
  printf 'DB_HOST=localhost\nAPI_NAME=stagingvalue1\n' > "$FIXTURE/.env.staging"
  printf 'PROD_KEY_NAME=prodvalue1\n' > "$FIXTURE/.env.production"
  chmod 000 "$FIXTURE/.env.production"
  run -0 bash "$ER" detect
  [[ "$output" == *"- name: staging"* ]]
  [[ "$output" == *"- DB_HOST"* ]]
  [[ "$output" == *"- API_NAME"* ]]
  [[ "$output" != *"stagingvalue1"* ]]
  [[ "$output" != *"prodvalue1"* ]]
  [[ "$output" == *"- name: production"* ]]
  [[ "$output" == *"file present, unread"* ]]
}

@test "C9 workflow environment: keys and doppler config names propose rows" {
  mkdir -p "$FIXTURE/.github/workflows"
  printf 'jobs:\n  deploy:\n    environment: production\n' \
    > "$FIXTURE/.github/workflows/deploy.yml"
  printf 'setup:\n  - project: app\n    config: stg\n' > "$FIXTURE/doppler.yaml"
  run -0 bash "$ER" detect
  [[ "$output" == *"- name: production"* ]]
  [[ "$output" == *".github/workflows/deploy.yml:3"* ]]
  [[ "$output" == *"- name: stg"* ]]
  [[ "$output" == *"doppler.yaml"* ]]
}

@test "C10 tier classification: e2e dir → live; GSD_ self-skip → nightly; fast+full always" {
  mkdir -p "$FIXTURE/tests/api-e2e"
  touch "$FIXTURE/tests/api-e2e/test_smoke.py"
  cat > "$FIXTURE/tests/test_vendor.py" <<'PY'
import os, pytest
if not os.environ.get("GSD_VENDOR_TREE"):
    pytest.skip("missing vendor tree", allow_module_level=True)
PY
  run -0 bash "$ER" detect
  [[ "$output" == *"- tier: fast"* ]]
  [[ "$output" == *"- tier: full"* ]]
  [[ "$output" == *"- tier: live"* ]]
  [[ "$output" == *"- tier: nightly"* ]]
}

@test "C11 detect porcelain purity: zero writes inside the target repo" {
  build_wrangler
  commit_fixture
  run -0 bash "$ER" detect
  run -0 git -C "$FIXTURE" status --porcelain
  [ -z "$output" ]
}

@test "C12 shared name-safety guard: secret-shaped names absent from detect AND check" {
  secret="$(cat "$(leak_dir)/wf-envname.secret")"
  mkdir -p "$FIXTURE/.github/workflows"
  printf 'jobs:\n  deploy:\n    environment: %s\n' "$secret" \
    > "$FIXTURE/.github/workflows/deploy.yml"
  rc=0
  bash "$ER" detect > "$BATS_TEST_TMPDIR/d-out" 2> "$BATS_TEST_TMPDIR/d-err" || rc=$?
  [ "$rc" -eq 0 ]
  grep -q "non-conforming name" "$BATS_TEST_TMPDIR/d-out"
  ! grep -qF "$secret" "$BATS_TEST_TMPDIR/d-out"
  ! grep -qF "$secret" "$BATS_TEST_TMPDIR/d-err"
  # check half: a registry env row whose NAME is secret-shaped
  write_registry "$FIXTURE/config/environments.yaml"
  sed -i.bak "s/name: stag\$/name: $secret/" "$FIXTURE/config/environments.yaml"
  rm -f "$FIXTURE/config/environments.yaml.bak"
  rc=0
  bash "$ER" check > "$BATS_TEST_TMPDIR/c-out" 2> "$BATS_TEST_TMPDIR/c-err" || rc=$?
  [ "$rc" -ne 0 ]
  ! grep -qF "$secret" "$BATS_TEST_TMPDIR/c-out"
  ! grep -qF "$secret" "$BATS_TEST_TMPDIR/c-err"
}

@test "C13 apply happy path: both files written, phase-1 byte shape, check green" {
  rm -f "$FIXTURE/config/environments.yaml"
  write_answers "$FIXTURE/answers.yaml"
  run -0 bash "$ER" apply --answers "$FIXTURE/answers.yaml" --yes
  [ -f "$FIXTURE/config/environments.yaml" ]
  [ -f "$FIXTURE/.ffs-init.json" ]
  run -0 bash "$ER" check
  [ "$(sed -n 2p "$FIXTURE/config/environments.yaml")" = "# schema: ffs.environments/v1" ]
  # surfaces block LAST
  surf_line="$(grep -n '^surfaces:' "$FIXTURE/config/environments.yaml" | cut -d: -f1)"
  tier_line="$(grep -n '^test_tiers:' "$FIXTURE/config/environments.yaml" | cut -d: -f1)"
  [ "$surf_line" -gt "$tier_line" ]
  ! grep -q "$(printf '\t')" "$FIXTURE/config/environments.yaml"
  run -0 python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert list(d)[0]=="schema" and d["schema"]=="ffs.init/v1", d' "$FIXTURE/.ffs-init.json"
}

@test "C14 all-or-nothing: missing test_tiers names key + shape, NEITHER file exists" {
  rm -f "$FIXTURE/config/environments.yaml" "$FIXTURE/.ffs-init.json"
  cat > "$FIXTURE/answers.yaml" <<'YAML'
environments:
  - name: local
    kind: local
YAML
  run -1 bash "$ER" apply --answers "$FIXTURE/answers.yaml" --yes
  [[ "$output" == *"test_tiers"* ]]
  [[ "$output" == *"expected"* ]]
  [ ! -f "$FIXTURE/config/environments.yaml" ]
  [ ! -f "$FIXTURE/.ffs-init.json" ]
}

@test "C15 leak-bearing answers: rc 2, neither file changed" {
  rm -f "$FIXTURE/config/environments.yaml" "$FIXTURE/.ffs-init.json"
  cp "$(leak_dir)/leak-answers.yaml" "$FIXTURE/answers.yaml"
  secret="$(cat "$(leak_dir)/leak-answers.secret")"
  rc=0
  bash "$ER" apply --answers "$FIXTURE/answers.yaml" --yes \
    > "$BATS_TEST_TMPDIR/la-out" 2> "$BATS_TEST_TMPDIR/la-err" || rc=$?
  [ "$rc" -eq 2 ]
  [ ! -f "$FIXTURE/config/environments.yaml" ]
  [ ! -f "$FIXTURE/.ffs-init.json" ]
  ! grep -qF "$secret" "$BATS_TEST_TMPDIR/la-out"
  ! grep -qF "$secret" "$BATS_TEST_TMPDIR/la-err"
}

@test "C16 declines lifecycle: keyed suppression, ONE advisory, re-proposal, reset" {
  build_wrangler
  rm -f "$FIXTURE/config/environments.yaml"
  write_answers "$FIXTURE/answers.yaml"
  cat >> "$FIXTURE/answers.yaml" <<'YAML'
declines:
  - heuristic: wrangler-env
    evidence: wrangler.toml:[env.staging]
YAML
  run -0 bash "$ER" apply --answers "$FIXTURE/answers.yaml" --yes
  run -0 --separate-stderr bash "$ER" detect
  [[ "$output" != *"wrangler.toml:[env.staging]"* ]]
  [[ "$output" == *"wrangler.toml:[env.prod]"* ]]
  [ "$(grep -c 'ADVISORY' <<<"$stderr")" -eq 1 ]
  [[ "$stderr" == *"1 proposal"* ]]
  [[ "$stderr" == *"--reset-declines"* ]]
  run -0 bash "$ER" apply --reset-declines
  run -0 python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["declines"]==[], d' "$FIXTURE/.ffs-init.json"
  run -0 --separate-stderr bash "$ER" detect
  [[ "$output" == *"wrangler.toml:[env.staging]"* ]]
}

@test "C17 --update preserves verified dates and operator-edited scalars, adds NEW rows" {
  cat > "$FIXTURE/config/environments.yaml" <<'YAML'
# config/environments.yaml
# schema: ffs.environments/v1
environments:
  - name: local
    kind: local
    base_url: operator.example
    verified: 2026-08-01
test_tiers:
  - tier: fast
    command: true
surfaces:
  - surface: release
    staging_instance: none
YAML
  cat > "$FIXTURE/answers.yaml" <<'YAML'
environments:
  - name: local
    kind: local
  - name: stag2
    kind: staging
test_tiers:
  - tier: fast
    command: false-should-not-clobber
YAML
  run -0 bash "$ER" apply --answers "$FIXTURE/answers.yaml" --update --yes
  grep -q "verified: 2026-08-01" "$FIXTURE/config/environments.yaml"
  grep -q "base_url: operator.example" "$FIXTURE/config/environments.yaml"
  grep -q "name: stag2" "$FIXTURE/config/environments.yaml"
  grep -q "command: true" "$FIXTURE/config/environments.yaml"
  ! grep -q "false-should-not-clobber" "$FIXTURE/config/environments.yaml"
}

@test "C18 regenerate guard: apply --yes over an existing registry refuses; --force proceeds" {
  write_answers "$FIXTURE/answers.yaml"
  run -1 bash "$ER" apply --answers "$FIXTURE/answers.yaml" --yes
  [[ "$output" == *"--update"* ]]
  [[ "$output" == *"--force"* ]]
  run -0 bash "$ER" apply --answers "$FIXTURE/answers.yaml" --yes --force
  grep -q "name: stag" "$FIXTURE/config/environments.yaml"
}

@test "C19 lock-before-validate: a held lock makes a second apply fail fast, typed busy" {
  write_answers "$FIXTURE/answers.yaml"
  python3 - "$FIXTURE/.ffs-init.lock" "$FIXTURE/.locked" <<'PY' &
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX)
open(sys.argv[2], "w").write("locked")
time.sleep(15)
PY
  LOCKER=$!
  for _ in $(seq 100); do [ -f "$FIXTURE/.locked" ] && break; sleep 0.1; done
  [ -f "$FIXTURE/.locked" ]
  run -1 bash "$ER" apply --answers "$FIXTURE/answers.yaml" --yes --force
  kill "$LOCKER" 2>/dev/null || true
  [[ "$output" == *"ENV-REGISTRY-BUSY"* ]]
}

@test "C20 write-target containment: symlinked config/ refuses, nothing at link target" {
  rm -rf "$FIXTURE/config"
  mkdir -p "$BATS_TEST_TMPDIR/outside"
  ln -s "$BATS_TEST_TMPDIR/outside" "$FIXTURE/config"
  write_answers "$FIXTURE/answers.yaml"
  run -1 bash "$ER" apply --answers "$FIXTURE/answers.yaml" --yes
  [[ "$output" == *"ENV-REGISTRY-REFUSED"* ]]
  [[ "$output" == *"symlink"* ]]
  [ ! -e "$BATS_TEST_TMPDIR/outside/environments.yaml" ]
  [ ! -f "$FIXTURE/.ffs-init.json" ]
}

# ── Group D: render (REQ-302, 03-02 Task 2) ────────────────────────────────

# Full-shape registry: both staging and prod kinds so all 5 templates render.
write_render_registry() {  # $1=path
  cat > "$1" <<'YAML'
# config/environments.yaml
# schema: ffs.environments/v1
environments:
  - name: local
    kind: local
  - name: stag
    kind: staging
  - name: production
    kind: prod
test_tiers:
  - tier: fast
    command: true
  - tier: full
    command: true
  - tier: nightly
    command: true
YAML
}

setup_render() {
  # Pinned fixture idiom: templates/ci/* COPIED into the fixture — render
  # always runs against the fixture's own tree, never the checkout's.
  mkdir -p "$FIXTURE/templates/ci"
  cp "$ROOT"/templates/ci/*.yml "$FIXTURE/templates/ci/"
  write_render_registry "$FIXTURE/config/environments.yaml"
}

@test "D1 fresh render: exactly 5 ffs-*.yml + dependabot; substitution clean; \${{ }} intact" {
  setup_render
  run -0 bash "$ER" render
  # exactly 5 workflow files, all ffs-<name>.yml (structural anti-clobber)
  [ "$(find "$FIXTURE/.github/workflows" -name '*.yml' | wc -l)" -eq 5 ]
  for n in pr-fast main-full nightly-deep deploy-staging deploy-prod; do
    [ -f "$FIXTURE/.github/workflows/ffs-$n.yml" ]
  done
  # dependabot target sits OUTSIDE workflows/ (wall 3c6cb2e7)
  [ -f "$FIXTURE/.github/dependabot.yml" ]
  # six-token substitution with the fixture's registry values
  grep -q "environment: stag" "$FIXTURE/.github/workflows/ffs-deploy-staging.yml"
  grep -q "environment: production" "$FIXTURE/.github/workflows/ffs-deploy-prod.yml"
  grep -q "requirements.txt" "$FIXTURE/.github/workflows/ffs-pr-fast.yml"
  grep -q "test-tier.sh fast" "$FIXTURE/.github/workflows/ffs-pr-fast.yml"
  grep -q "test-tier.sh full" "$FIXTURE/.github/workflows/ffs-main-full.yml"
  grep -q "test-tier.sh nightly" "$FIXTURE/.github/workflows/ffs-nightly-deep.yml"
  # zero residual {{ }} runs outside ${{ ... }} expressions (wall 1a28ec98)
  run -1 bash -c 'sed "s/\${{/GHEXPR/g" "$1"/.github/workflows/*.yml | grep -q "{{"' _ "$FIXTURE"
  # ${{ ... }} expressions survive substitution unmangled (OQ9)
  grep -q 'ffs-${{ github.run_id }}' "$FIXTURE/.github/workflows/ffs-deploy-prod.yml"
}

@test "D2 collision: proposal outside workflows dir, diff -u, originals byte-identical" {
  setup_render
  mkdir -p "$FIXTURE/.github/workflows"
  printf 'name: consumer-pr\non: pull_request\n' \
    > "$FIXTURE/.github/workflows/ffs-pr-fast.yml"
  printf 'name: consumer-ci\non: push\n' > "$FIXTURE/.github/workflows/ci.yml"
  printf 'name: consumer-release\non: push\n' \
    > "$FIXTURE/.github/workflows/release.yml"
  cp "$FIXTURE/.github/workflows/ffs-pr-fast.yml" "$BATS_TEST_TMPDIR/orig-collide.yml"
  cp "$FIXTURE/.github/workflows/ci.yml" "$BATS_TEST_TMPDIR/orig-ci.yml"
  run -0 bash "$ER" render
  # proposal: IDENTICAL basename under .github/ffs-proposals/ (row 20, OQ8)
  [ -f "$FIXTURE/.github/ffs-proposals/ffs-pr-fast.yml" ]
  grep -q "test-tier.sh fast" "$FIXTURE/.github/ffs-proposals/ffs-pr-fast.yml"
  # diff -u markers on stdout
  [[ "$output" == *"---"* ]]
  [[ "$output" == *"+++"* ]]
  # EVERY pre-existing workflow byte-identical
  cmp -s "$FIXTURE/.github/workflows/ffs-pr-fast.yml" "$BATS_TEST_TMPDIR/orig-collide.yml"
  cmp -s "$FIXTURE/.github/workflows/ci.yml" "$BATS_TEST_TMPDIR/orig-ci.yml"
  # workflows dir never contains a proposal-suffixed filename (Pitfall 1)
  run -1 bash -c 'ls "$1"/.github/workflows/*proposed* 2>/dev/null | grep -q .' _ "$FIXTURE"
}

@test "D3 byte-identical existing target: up-to-date line, no proposal (EDGE-007)" {
  setup_render
  run -0 bash "$ER" render
  run -0 bash "$ER" render
  [[ "$output" == *"up-to-date"* ]]
  [ ! -e "$FIXTURE/.github/ffs-proposals" ]
}

@test "D4 dependabot: missing github-actions ecosystem → advisory + untouched; present → silent" {
  setup_render
  mkdir -p "$FIXTURE/.github"
  printf 'version: 2\nupdates:\n  - package-ecosystem: npm\n    directory: /\n' \
    > "$FIXTURE/.github/dependabot.yml"
  cp "$FIXTURE/.github/dependabot.yml" "$BATS_TEST_TMPDIR/orig-dependabot.yml"
  run -0 bash "$ER" render
  [[ "$output" == *"ADVISORY"* ]]
  [[ "$output" == *"github-actions"* ]]
  cmp -s "$FIXTURE/.github/dependabot.yml" "$BATS_TEST_TMPDIR/orig-dependabot.yml"
  # WITH the ecosystem (differing bytes) → silent skip, still untouched
  printf 'version: 2\nupdates:\n  - package-ecosystem: github-actions\n    directory: /\n    schedule:\n      interval: daily\n' \
    > "$FIXTURE/.github/dependabot.yml"
  cp "$FIXTURE/.github/dependabot.yml" "$BATS_TEST_TMPDIR/orig-dependabot2.yml"
  run -0 bash "$ER" render
  [[ "$output" != *"dependabot"*"ADVISORY"* ]]
  [[ "$output" != *"ADVISORY: .github/dependabot.yml"* ]]
  cmp -s "$FIXTURE/.github/dependabot.yml" "$BATS_TEST_TMPDIR/orig-dependabot2.yml"
}

@test "D5 registry without staging/prod kinds: 3 test workflows + typed skip advisory, rc 0" {
  setup_render
  cat > "$FIXTURE/config/environments.yaml" <<'YAML'
# config/environments.yaml
# schema: ffs.environments/v1
environments:
  - name: local
    kind: local
test_tiers:
  - tier: fast
    command: true
YAML
  run -0 bash "$ER" render
  [ "$(find "$FIXTURE/.github/workflows" -name '*.yml' | wc -l)" -eq 3 ]
  [ ! -e "$FIXTURE/.github/workflows/ffs-deploy-staging.yml" ]
  [ ! -e "$FIXTURE/.github/workflows/ffs-deploy-prod.yml" ]
  [[ "$output" == *"ADVISORY"* ]]
  [[ "$output" == *"kind: staging"* ]]
  [[ "$output" == *"kind: prod"* ]]
}

@test "D6 hostile registry value: value-free refusal, nothing written (runtime-generated)" {
  setup_render
  secret="ghp_$(python3 -c 'import secrets; print(secrets.token_hex(12))')"
  cat > "$FIXTURE/config/environments.yaml" <<YAML
# config/environments.yaml
# schema: ffs.environments/v1
environments:
  - name: local
    kind: local
  - name: $secret
    kind: staging
  - name: production
    kind: prod
test_tiers:
  - tier: fast
    command: true
YAML
  rc=0
  bash "$ER" render > "$BATS_TEST_TMPDIR/r-out" 2> "$BATS_TEST_TMPDIR/r-err" || rc=$?
  [ "$rc" -eq 2 ]
  ! grep -qF "$secret" "$BATS_TEST_TMPDIR/r-out"
  ! grep -qF "$secret" "$BATS_TEST_TMPDIR/r-err"
  [ ! -e "$FIXTURE/.github" ]
}

@test "D7 rendered-candidate leak scan: bare hex outside uses: shape → rc 2, nothing written" {
  setup_render
  hexlit="$(python3 -c 'import secrets; print(secrets.token_hex(20))')"
  printf 'name: evil\non: push\njobs:\n  x:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo %s\n' \
    "$hexlit" > "$FIXTURE/templates/ci/aa-evil.yml"
  rc=0
  bash "$ER" render > "$BATS_TEST_TMPDIR/l-out" 2> "$BATS_TEST_TMPDIR/l-err" || rc=$?
  [ "$rc" -eq 2 ]
  ! grep -qF "$hexlit" "$BATS_TEST_TMPDIR/l-out"
  ! grep -qF "$hexlit" "$BATS_TEST_TMPDIR/l-err"
  # wall 9a586ab7: NOTHING written — not even the clean templates
  [ ! -e "$FIXTURE/.github" ]
}

@test "D8 missing registry and missing templates dir: rc 1 typed refusals (OQ7)" {
  setup_render
  rm -f "$FIXTURE/config/environments.yaml"
  run -1 bash "$ER" render
  [[ "$output" == *"registry"* ]]
  [[ "$output" == *"remedy"* ]]
  write_render_registry "$FIXTURE/config/environments.yaml"
  rm -rf "$FIXTURE/templates/ci"
  run -1 bash "$ER" render
  [[ "$output" == *"templates"* ]]
  [ ! -e "$FIXTURE/.github" ]
}

@test "D9 proposal-path collision (wall 969c0f3d): regular file overwritten; symlink refuses" {
  setup_render
  mkdir -p "$FIXTURE/.github/workflows" "$FIXTURE/.github/ffs-proposals"
  printf 'name: consumer-pr\n' > "$FIXTURE/.github/workflows/ffs-pr-fast.yml"
  # (a) EXISTING REGULAR FILE at the proposal path: it is a proposal, not an
  # authority — overwritten atomically (mkstemp+os.replace)
  printf 'stale proposal bytes\n' > "$FIXTURE/.github/ffs-proposals/ffs-pr-fast.yml"
  run -0 bash "$ER" render
  ! grep -q "stale proposal bytes" "$FIXTURE/.github/ffs-proposals/ffs-pr-fast.yml"
  grep -q "test-tier.sh fast" "$FIXTURE/.github/ffs-proposals/ffs-pr-fast.yml"
  # (b) SYMLINK at the proposal path: containment guard refuses, nothing
  # written at the link target
  mkdir -p "$BATS_TEST_TMPDIR/outside"
  rm -f "$FIXTURE/.github/ffs-proposals/ffs-pr-fast.yml"
  ln -s "$BATS_TEST_TMPDIR/outside/steal.yml" "$FIXTURE/.github/ffs-proposals/ffs-pr-fast.yml"
  run -1 bash "$ER" render
  [[ "$output" == *"ENV-REGISTRY-REFUSED"* ]]
  [[ "$output" == *"symlink"* ]]
  [ ! -e "$BATS_TEST_TMPDIR/outside/steal.yml" ]
}

@test "D10 validate-ALL-then-write-ALL: bad placeholder in the LAST template → zero files" {
  setup_render
  # zz- sorts LAST; malformed lowercase + unknown-uppercase tokens both refuse
  # (wall 1a28ec98 residual scan covers both drift directions)
  printf 'name: last\non: push\njobs:\n  x:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo {{NOT_A_TOKEN}} {{lower_case}}\n' \
    > "$FIXTURE/templates/ci/zz-last.yml"
  run -1 bash "$ER" render
  [[ "$output" == *"placeholder"* ]]
  [[ "$output" == *"nothing written"* ]]
  [ ! -e "$FIXTURE/.github" ]
}

@test "D11 both-sides leak scan (wall d118f505): existing-side finding suppresses the diff" {
  setup_render
  mkdir -p "$FIXTURE/.github/workflows"
  secret="ghp_$(python3 -c 'import secrets; print(secrets.token_hex(12))')"
  printf 'name: consumer-pr\nenv:\n  TOKEN: %s\n' "$secret" \
    > "$FIXTURE/.github/workflows/ffs-pr-fast.yml"
  cp "$FIXTURE/.github/workflows/ffs-pr-fast.yml" "$BATS_TEST_TMPDIR/orig-leaky.yml"
  rc=0
  bash "$ER" render > "$BATS_TEST_TMPDIR/s-out" 2> "$BATS_TEST_TMPDIR/s-err" || rc=$?
  [ "$rc" -eq 2 ]
  # the secret crosses NEITHER stream — the content diff is suppressed
  ! grep -qF "$secret" "$BATS_TEST_TMPDIR/s-out"
  ! grep -qF "$secret" "$BATS_TEST_TMPDIR/s-err"
  # value-free notice names the finding class + the proposal path instead
  grep -q "suppressed" "$BATS_TEST_TMPDIR/s-out"
  grep -q "ffs-proposals/ffs-pr-fast.yml" "$BATS_TEST_TMPDIR/s-out"
  [ -f "$FIXTURE/.github/ffs-proposals/ffs-pr-fast.yml" ]
  # the existing consumer workflow stays byte-identical
  cmp -s "$FIXTURE/.github/workflows/ffs-pr-fast.yml" "$BATS_TEST_TMPDIR/orig-leaky.yml"
}

@test "D12 symlinked workflows dir (wall 6b6bd8bc): typed refusal, nothing at link target" {
  setup_render
  mkdir -p "$FIXTURE/.github" "$BATS_TEST_TMPDIR/outside-wf"
  ln -s "$BATS_TEST_TMPDIR/outside-wf" "$FIXTURE/.github/workflows"
  run -1 bash "$ER" render
  [[ "$output" == *"ENV-REGISTRY-REFUSED"* ]]
  [[ "$output" == *"symlink"* ]]
  run -1 bash -c 'ls "$1"/*.yml 2>/dev/null | grep -q .' _ "$BATS_TEST_TMPDIR/outside-wf"
}
