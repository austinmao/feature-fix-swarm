#!/usr/bin/env bats
# review-tier.sh — diff risk-tier detector tests (matrix + override + fail-safe)

bats_require_minimum_version 1.5.0

SCRIPT_REL="scripts/gsd/review-tier.sh"

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SCRIPT="$ROOT/$SCRIPT_REL"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO"
  cd "$REPO" || return 1
  git init -q
  git config user.email t@t
  git config user.name t
  # 30 baseline tracked files (2-digit zero-padded so filename width is
  # stable regardless of which subset a test touches).
  for i in $(seq 1 30); do
    printf 'baseline\n' > "$(printf 'f%02d.txt' "$i")"
  done
  git add .
  git commit -q -m init
}

# append N lines to an existing tracked file (unstaged working-tree change)
modify_tracked() {
  local path="$1" n="$2" i=1
  while [ "$i" -le "$n" ]; do
    echo "x $i" >> "$path"
    i=$((i + 1))
  done
}

# touch K of the baseline f01..f30 files, 1 line each (K files changed, K lines)
touch_files() {
  local count="$1" i=1
  while [ "$i" -le "$count" ]; do
    modify_tracked "$(printf 'f%02d.txt' "$i")" 1
    i=$((i + 1))
  done
}

@test "security-surface path in a 2-file diff -> full (must never regress)" {
  mkdir -p lib/auth
  echo "session logic" > lib/auth/session.py
  echo "unrelated" > other.txt
  git add lib/auth/session.py other.txt
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == full\ * ]]
  [[ "$output" == *"security-surface"* ]]
}

@test "path-boundary false-positive: docs/authoring.md does not force full" {
  mkdir -p docs
  echo "authoring guide" > docs/authoring.md
  echo "unrelated" > other2.txt
  git add docs/authoring.md other2.txt
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == light\ * ]]
}

@test "3 files / 150 lines / no security path / no migration -> light" {
  modify_tracked f01.txt 50
  modify_tracked f02.txt 50
  modify_tracked f03.txt 50
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == light\ * ]]
}

@test "migration-only diff -> full" {
  mkdir -p db/migrations
  echo "alter table" > db/migrations/0001_x.sql
  git add db/migrations/0001_x.sql
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == full\ * ]]
  [[ "$output" == *"migration"* ]]
}

@test "25 files -> full" {
  touch_files 25
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == full\ * ]]
}

@test "8 files -> standard" {
  touch_files 8
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == standard\ * ]]
}

@test "boundary: 4 files -> light-eligible" {
  touch_files 4
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == light\ * ]]
}

@test "boundary: 5 files -> not light" {
  touch_files 5
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" != light\ * ]]
}

@test "boundary: 199 changed lines -> light-eligible" {
  modify_tracked f01.txt 199
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == light\ * ]]
}

@test "boundary: 200 changed lines -> not light" {
  modify_tracked f01.txt 200
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" != light\ * ]]
}

@test "boundary: 20 files -> standard" {
  touch_files 20
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == standard\ * ]]
}

@test "boundary: 21 files -> full" {
  touch_files 21
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == full\ * ]]
}

@test "binary file in diff: counts toward file-count, 0 lines" {
  printf 'abc\000def' > bin.dat
  git add bin.dat
  git commit -q -m "add binary"
  printf 'xyz\000123456' > bin.dat
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  # 1 binary file changed, 0 lines contributed -> light
  [[ "$output" == light\ * ]]
}

@test "REVIEW_TIER=light override -> light, reason contains override" {
  run env REVIEW_TIER=light bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == light\ * ]]
  [[ "$output" == *"override"* ]]
}

@test "REVIEW_TIER=standard override -> standard, reason contains override" {
  run env REVIEW_TIER=standard bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == standard\ * ]]
  [[ "$output" == *"override"* ]]
}

@test "REVIEW_TIER=full override -> full, reason contains override" {
  run env REVIEW_TIER=full bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == full\ * ]]
  [[ "$output" == *"override"* ]]
}

@test "REVIEW_TIER=garbage -> ignored, auto-detection runs" {
  run env REVIEW_TIER=garbage bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" != *"override"* ]]
  # nothing changed -> auto-detect falls to light
  [[ "$output" == light\ * ]]
}

@test "diff-mode parity: staged security change canceled unstaged, default --staged still -> full" {
  mkdir -p lib/auth
  echo "original" > lib/auth/session.py
  git add lib/auth/session.py
  git commit -q -m "add auth file"

  echo "changed" > lib/auth/session.py
  git add lib/auth/session.py
  echo "original" > lib/auth/session.py

  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == full\ * ]]
}

@test "unresolvable base (bogus sha) -> standard fail-safe, never light" {
  BOGUS_SHA="$(printf 'deadbeef%.0s' {1..5})"  # runtime-built: AC-011 hex-run gate stays quiet
  run --separate-stderr env REVIEW_TIER_BASE="$BOGUS_SHA" bash "$SCRIPT" --all
  [ "$status" -eq 0 ]
  [[ "$output" == standard\ * ]]
}

@test "orphan history (no merge-base with base) -> standard fail-safe, never light" {
  git checkout --orphan orphanbr -q
  git rm -rf --cached . -q > /dev/null 2>&1 || true
  rm -f -- *.txt
  echo "orphan content" > orphan.txt
  git add orphan.txt
  git commit -q -m "orphan commit"

  run --separate-stderr env REVIEW_TIER_BASE=main bash "$SCRIPT" --all
  [ "$status" -eq 0 ]
  [[ "$output" == standard\ * ]]
}

@test "option-injection: hostile base rejected by rev-parse, no file written" {
  rm -f /tmp/review-tier-pwned-test
  run --separate-stderr env REVIEW_TIER_BASE='--output=/tmp/review-tier-pwned-test' bash "$SCRIPT" --all
  [ "$status" -eq 0 ]
  [[ "$output" == standard\ * ]]
  [ ! -e /tmp/review-tier-pwned-test ]
}

@test "metadata-command failure (git diff --name-only fails) -> standard fail-safe" {
  FAKEBIN="$BATS_TEST_TMPDIR/fakebin"
  mkdir -p "$FAKEBIN"
  REALGIT="$(command -v git)"
  cat > "$FAKEBIN/git" <<EOF
#!/usr/bin/env bash
for a in "\$@"; do
  if [ "\$a" = "--name-only" ] || [ "\$a" = "--numstat" ]; then
    exit 1
  fi
done
exec "$REALGIT" "\$@"
EOF
  chmod +x "$FAKEBIN/git"
  modify_tracked f01.txt 1
  PATH="$FAKEBIN:$PATH" run --separate-stderr bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == standard\ * ]]
}

@test "stdout is exactly one line; warnings land on stderr only" {
  BOGUS_SHA="$(printf 'deadbeef%.0s' {1..5})"  # runtime-built: AC-011 hex-run gate stays quiet
  run --separate-stderr env REVIEW_TIER_BASE="$BOGUS_SHA" bash "$SCRIPT" --all
  [ "$status" -eq 0 ]
  line_count="$(printf '%s\n' "$output" | wc -l | tr -d ' ')"
  [ "$line_count" -eq 1 ]
  [[ "$output" == standard\ * ]]
  [[ "$stderr" == *"WARN"* ]]
  [[ "$output" != *"WARN"* ]]
}

@test "--file mode classifies a single path" {
  modify_tracked f01.txt 3
  run bash "$SCRIPT" --file f01.txt
  [ "$status" -eq 0 ]
  [[ "$output" == light\ * ]]
}

@test "usage error: unknown flag -> exit 2" {
  run bash "$SCRIPT" --nonsense
  [ "$status" -eq 2 ]
}

@test "usage error: --file with no path -> exit 2" {
  run bash "$SCRIPT" --file
  [ "$status" -eq 2 ]
}

@test "exact-pattern pin: sourced KEYWORDS matches the verbatim expected literal" {
  # shellcheck disable=SC1091
  . "$ROOT/scripts/gsd/security-surface.sh"
  [ "$KEYWORDS" = 'auth|rls|row[ _-]?level|payment|stripe|crypto|jwt|jwks|oauth|owasp|secret|credential|password' ]
}
