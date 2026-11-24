#!/usr/bin/env bats
# review-gate-trusted-runtime.bats — the review gate must never execute FFS
# helper code from the tree it reviews. Each skill fence loads helpers from a
# snapshot of the base ref (merge-base with main), so a branch that edits
# gates.py, review-tier.sh, adversary-host.sh or clean_code_metrics.py cannot
# change how its own review runs. Every fence runs in a fresh bash process,
# because hosts may run fences in separate shells.

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SKILL="$ROOT/skills/review-gate/SKILL.md"
  FIX="$(cd "$BATS_TEST_TMPDIR" && pwd -P)/repo"  # canonical, so path assertions bite on macOS
  CANARY="$BATS_TEST_TMPDIR/canary"
  export HOME="$BATS_TEST_TMPDIR/home"
  mkdir -p "$HOME"
}

# fence <heading> — the first ```bash block after an exact heading line.
fence() {
  awk -v h="$1" '
    $0 == h { found = 1; next }
    found && !inb && $0 ~ /^ *```bash$/ { inb = 1; next }
    inb && $0 ~ /^ *```$/ { exit }
    inb { print }
  ' "$SKILL"
}

# make_fixture <runtime-root> — main holds benign helpers; the checked-out
# branch replaces each with a version that writes $CANARY.
make_fixture() {
  local rt="$FIX/$1"
  mkdir -p "$rt/lib" "$rt/scripts/gsd" "$FIX/src"
  git -C "$FIX" init -q -b main
  echo 'x = 1' > "$FIX/src/app.py"
  if [ -z "${NO_RUNTIME:-}" ]; then
    cp "$ROOT/scripts/gsd/trusted-runtime.sh" "$rt/scripts/gsd/"
    printf 'import sys\nprint("[]")\n' > "$rt/lib/gates.py"
    [ -n "${NO_METRICS:-}" ] || printf 'import sys\nprint("COMPLEXITY base-helper")\n' > "$rt/lib/clean_code_metrics.py"
    printf '#!/usr/bin/env bash\necho "light base-tier"\n' > "$rt/scripts/gsd/review-tier.sh"
    printf 'detect_orchestrator_host() { echo claude; }\nadversary_kind_for_host() { echo codex; }\n' \
      > "$rt/scripts/gsd/adversary-host.sh"
    chmod +x "$rt/scripts/gsd/review-tier.sh"
  fi
  git -C "$FIX" add -A && git -C "$FIX" -c user.name=t -c user.email=t@t commit -qm base
  git -C "$FIX" checkout -qb 123-branch
  local evil="open('$CANARY','w').write('ran')"
  printf 'print("[]")\n%s\n' "$evil" > "$rt/lib/gates.py"
  printf 'print("COMPLEXITY evil")\n%s\n' "$evil" > "$rt/lib/clean_code_metrics.py"
  printf '#!/usr/bin/env bash\ntouch %q\necho "light evil"\n' "$CANARY" > "$rt/scripts/gsd/review-tier.sh"
  printf 'detect_orchestrator_host() { echo claude; }\nadversary_kind_for_host() { echo codex; }\ntouch %q\n' \
    "$CANARY" > "$rt/scripts/gsd/adversary-host.sh"
  chmod +x "$rt/scripts/gsd/review-tier.sh"
  git -C "$FIX" add -A && git -C "$FIX" -c user.name=t -c user.email=t@t commit -qm evil
  echo 'x = 2' > "$FIX/src/app.py"
  git -C "$FIX" add src/app.py
}

# fake_install — a user-scope FFS install under the sandboxed HOME.
fake_install() {
  local d="$HOME/.claude/lib/feature-fix-swarm"
  mkdir -p "$d/scripts/gsd"
  printf 'print("[]")\n' > "$d/gates.py"
  printf 'detect_orchestrator_host() { echo claude; }\nadversary_kind_for_host() { echo installed; }\n' \
    > "$d/scripts/gsd/adversary-host.sh"
}

run_fence() {  # run_fence <heading> <bash prelude>
  local body
  body="$(fence "$1")"
  [ -n "$body" ]
  run "${FENCE_SHELL:-bash}" -c "cd $(printf '%q' "${FENCE_CWD:-$FIX}") && $2
$body
$3"
}

@test "pre-check sources adversary-host.sh from the base ref, not the branch" {
  make_fixture .
  run_fence '### Pre-check — choose the opposite CLI' '' 'echo "kind=$REVIEW_KIND"'
  [ "$status" -eq 0 ]
  [[ "$output" == *"kind=codex"* ]]
  [ ! -e "$CANARY" ]
}

@test "pre-check and probe also work under zsh" {
  command -v zsh >/dev/null || skip "zsh not installed"
  make_fixture .
  FENCE_SHELL=zsh run_fence '### Pre-check — choose the opposite CLI' '' 'echo "kind=$REVIEW_KIND"'
  [ "$status" -eq 0 ]
  [[ "$output" == *"kind=codex"* ]]
  FENCE_SHELL=zsh run_fence '### Findings queue: capability probe + resolved-sig consult' '' 'echo "gates=$GATES_PY"'
  [[ "$output" == *"gates="*"/lib/gates.py"* ]]
  [ ! -e "$CANARY" ]
}

@test "tier selection runs review-tier.sh from the base ref" {
  make_fixture .
  run_fence '### Tier selection (v1.4.0)' 'DIFF_TARGET=--staged' 'echo "tier=$TIER reason=$TIER_REASON"'
  [ "$status" -eq 0 ]
  [[ "$output" == *"tier=light reason=base-tier"* ]]
  [ ! -e "$CANARY" ]
}

@test "gates.py probe never runs a branch copy (flat layout)" {
  make_fixture .
  run_fence '### Findings queue: capability probe + resolved-sig consult' '' 'echo "gates=$GATES_PY"'
  [ "$status" -eq 0 ]
  [[ "$output" == *"gates="*"/lib/gates.py"* ]]
  [[ "$output" != *"gates=$FIX/"* || "$output" == *"gates=$FIX/.git/ffs-trusted/"* ]]
  [ ! -e "$CANARY" ]
}

@test "gates.py probe never runs a branch copy (packaged layout)" {
  make_fixture packages/feature-fix-swarm
  run_fence '### Findings queue: capability probe + resolved-sig consult' '' 'echo "gates=$GATES_PY"'
  [ "$status" -eq 0 ]
  [[ "$output" == *"gates="*"/lib/gates.py"* ]]
  [[ "$output" != *"gates=$FIX/"* || "$output" == *"gates=$FIX/.git/ffs-trusted/"* ]]
  [ ! -e "$CANARY" ]
}

@test "capture diff prints the metrics advisory from the base helper in one fence" {
  make_fixture .
  run_fence '### Capture diff' '' ''
  [ "$status" -eq 0 ]
  [[ "$output" == *"COMPLEXITY base-helper"* ]]
  [ ! -e "$CANARY" ]
}

@test "capture diff exits 0 when there is no metrics output" {
  NO_METRICS=1 make_fixture .
  run_fence '### Capture diff' '' ''
  [ "$status" -eq 0 ]
  [[ "$output" != *"METRICS_BLOCK"* ]]
}

@test "fences work from a subdirectory" {
  make_fixture .
  FENCE_CWD="$FIX/src" run_fence '### Findings queue: capability probe + resolved-sig consult' '' 'echo "gates=$GATES_PY"'
  [[ "$output" == *"gates="*"/lib/gates.py"* ]]
  [[ "$output" != *"gates=$FIX/"* || "$output" == *"gates=$FIX/.git/ffs-trusted/"* ]]
  [[ "$output" != *"gates=$HOME/"* ]]
  [ ! -e "$CANARY" ]
}

@test "base ref without an FFS runtime falls back to the user-scope install" {
  NO_RUNTIME=1 make_fixture .
  fake_install
  run_fence '### Pre-check — choose the opposite CLI' '' 'echo "kind=$REVIEW_KIND"'
  [ "$status" -eq 0 ]
  [[ "$output" == *"kind=installed"* ]]
  run_fence '### Findings queue: capability probe + resolved-sig consult' '' 'echo "gates=$GATES_PY"'
  [[ "$output" == *"gates=$HOME/.claude/lib/feature-fix-swarm/gates.py"* ]]
  [ ! -e "$CANARY" ]
}

@test "no trusted runtime anywhere blocks the review" {
  NO_RUNTIME=1 make_fixture .
  run_fence '### Pre-check — choose the opposite CLI' '' 'echo reached'
  [ "$status" -ne 0 ]
  [[ "$output" == *"BLOCKED"* ]]
  [ ! -e "$CANARY" ]
}

@test "no fence executes a repo-relative FFS helper" {
  # Allowed: the trusted loader line itself and finding strings that name a file.
  run awk '
    /^ *```bash$/ { inb = 1; next }
    /^ *```$/ { inb = 0; next }
    inb && /(^|[^\/A-Za-z_$.-])scripts\/gsd\/[A-Za-z_-]+\.(sh|py)/ && !/HIGH\/scripts\/gsd/ && !/:scripts\/gsd\/trusted-runtime\.sh/ { print NR": "$0 }
    inb && /REPO_ROOT[^ ]*(lib\/gates\.py|scripts\/gsd\/)/ { print NR": "$0 }
    inb && /dirname "\$GATES_PY"/ { print NR": "$0 }
  ' "$SKILL"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

# real_install — copy MANAGED_LIB_FILES to the user-scope layout, as setup.sh does.
real_install() {
  python3 -I - "$ROOT" "$HOME/.claude/lib/feature-fix-swarm" <<'PY'
import shutil, sys
from pathlib import Path
root, dest = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root / "lib"))
import ffs_installer
for src, rel in ffs_installer.MANAGED_LIB_FILES:
    (dest / rel).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / src, dest / rel)
PY
}

@test "user-scope install ships working review helpers" {
  NO_RUNTIME=1 make_fixture .
  real_install
  FFS_HOST=claude run_fence '### Pre-check — choose the opposite CLI' '' \
    'echo "kind=$REVIEW_KIND"; adversary_gates; adversary_model_request_helper; "$FFS_T/scripts/gsd/review-tier.sh" --staged'
  [ "$status" -eq 0 ]
  [[ "$output" == *"kind=codex"* ]]
  [[ "$output" == *"$HOME/.claude/lib/feature-fix-swarm/scripts/gsd/../../gates.py"* ]]
  [[ "$output" == *"/model_requests.py"* ]]
  [[ "$output" == *"light "* ]]
}

@test "fences reuse one base-ref snapshot kept outside the work tree" {
  make_fixture .
  heading='### Findings queue: capability probe + resolved-sig consult'
  run_fence "$heading" '' 'echo "rt=$FFS_T"'
  first="$(printf '%s\n' "$output" | sed -n 's/^rt=//p')"
  run_fence "$heading" '' 'echo "rt=$FFS_T"'
  second="$(printf '%s\n' "$output" | sed -n 's/^rt=//p')"
  [ -n "$first" ] && [ "$first" = "$second" ]
  [[ "$first" == "$(cd "$FIX" && pwd -P)/.git/ffs-trusted/"* ]]
  [ ! -e "$CANARY" ]
}

@test "capture diff shows files a branch .gitattributes marks -diff" {
  make_fixture .
  printf '*.py -diff\n' > "$FIX/.gitattributes"
  git -C "$FIX" add .gitattributes
  run_fence '### Capture diff' '' 'printf "%s\n" "$DIFF"'
  [ "$status" -eq 0 ]
  [[ "$output" == *"+x = 2"* ]]
}

@test "gate-path python never runs with the reviewed repo on sys.path" {
  # python3 -c and python3 - put the cwd first on sys.path; -I drops it.
  run grep -nE 'python3 +(-c|-)( |$)' "$SKILL" \
    "$ROOT/scripts/gsd/adversary-host.sh" "$ROOT/scripts/gsd/run-bounded.sh" \
    "$ROOT/scripts/gsd/socratic-slice.sh" "$ROOT/scripts/gsd/review-gate-command.sh"
  [ -z "$output" ]
}

@test "capture diff names a real binary in one line, not its bytes" {
  make_fixture .
  head -c 300000 /dev/urandom > "$FIX/a.png"
  git -C "$FIX" add a.png
  run_fence '### Capture diff' '' 'printf "%s" "$DIFF" | wc -c'
  [ "$status" -eq 0 ]
  [ "$(printf '%s\n' "$output" | tail -1)" -lt 10000 ]
}

@test "an unwritable git dir falls back to a temporary snapshot" {
  make_fixture .
  chmod a-w "$FIX/.git"
  run_fence '### Findings queue: capability probe + resolved-sig consult' '' 'echo "gates=$GATES_PY"'
  chmod u+w "$FIX/.git"
  [[ "$output" == *"gates="*"/lib/gates.py"* ]]
  [[ "$output" != *"gates=$HOME/"* ]]
  [ ! -e "$CANARY" ]
}
