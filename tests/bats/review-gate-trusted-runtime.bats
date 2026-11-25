#!/usr/bin/env bats
# review-gate-trusted-runtime.bats — the review gate must never execute FFS
# helper code from the tree it reviews. Each skill fence loads helpers from a
# snapshot of the base tip (origin/main, else main), so a branch that edits
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

# advance_base — main moves past the branch's fork point with newer helpers.
# The branch can choose its fork point, so the gate must not run the fork's copies.
advance_base() {
  local wt="$BATS_TEST_TMPDIR/main-wt" rt
  git -C "$FIX" worktree add -q "$wt" main
  rt="$wt/$1"
  printf '#!/usr/bin/env bash\necho "light tip-tier"\n' > "$rt/scripts/gsd/review-tier.sh"
  printf 'detect_orchestrator_host() { echo claude; }\nadversary_kind_for_host() { echo tip; }\n' \
    > "$rt/scripts/gsd/adversary-host.sh"
  printf 'print("COMPLEXITY tip-helper")\n' > "$rt/lib/clean_code_metrics.py"
  printf 'import sys\nsys.exit("evidence store x is tracked by git")\n' > "$rt/lib/gates.py"
  git -C "$wt" -c user.name=t -c user.email=t@t commit -qam tip
  git -C "$FIX" worktree remove --force "$wt"
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

@test "every fence uses the base tip, not the branch's fork point" {
  make_fixture .
  advance_base .
  run_fence '### Pre-check — choose the opposite CLI' '' 'echo "kind=$REVIEW_KIND"'
  [[ "$output" == *"kind=tip"* ]] || false
  run_fence '### Tier selection (v1.4.0)' 'DIFF_TARGET=--staged' 'echo "reason=$TIER_REASON"'
  [[ "$output" == *"reason=tip-tier"* ]] || false
  run_fence '### Capture diff' '' ''
  [[ "$output" == *"COMPLEXITY tip-helper"* ]] || false
  run_fence '### Findings queue: capability probe + resolved-sig consult' '' 'echo reached'
  [ "$status" -eq 1 ]
  [[ "$output" == *"GATE: FAIL"* ]] || false
  [ ! -e "$CANARY" ]
  # the remaining fences carry the same loader line
  [ "$(grep -c 'ffs_trusted_runtime "$FFS_REF"' "$SKILL")" -eq 6 ]
  [ "$(grep 'ffs_trusted_runtime "$FFS_REF"' "$SKILL" | sed 's/^ *//' | sort -u | wc -l)" -eq 1 ]
}

@test "the base tip is origin/main before a local main (packaged layout)" {
  make_fixture packages/feature-fix-swarm
  git -C "$FIX" update-ref refs/remotes/origin/main main
  advance_base packages/feature-fix-swarm
  run_fence '### Pre-check — choose the opposite CLI' '' 'echo "kind=$REVIEW_KIND"'
  [[ "$output" == *"kind=codex"* ]] || false
  git -C "$FIX" update-ref refs/remotes/origin/main main
  run_fence '### Pre-check — choose the opposite CLI' '' 'echo "kind=$REVIEW_KIND"'
  [[ "$output" == *"kind=tip"* ]] || false
  [ ! -e "$CANARY" ]
}

@test "a branch with no history in common with main still gets main's runtime" {
  make_fixture .
  git -C "$FIX" checkout -q --orphan 124-orphan
  git -C "$FIX" -c user.name=t -c user.email=t@t commit -qm orphan
  run_fence '### Pre-check — choose the opposite CLI' '' 'echo "kind=$REVIEW_KIND"'
  [ "$status" -eq 0 ]
  [[ "$output" == *"kind=codex"* ]] || false
  [ ! -e "$CANARY" ]
}

@test "a branch or tag named like a base ref does not replace it" {
  make_fixture .
  advance_base .
  for name in main origin/main refs/heads/main refs/remotes/origin/main; do
    git -C "$FIX" update-ref "refs/tags/$name" 123-branch
    [ "$name" = main ] || git -C "$FIX" update-ref "refs/heads/$name" 123-branch
  done
  # no origin/main: the local main answers
  run_fence '### Pre-check — choose the opposite CLI' '' 'echo "kind=$REVIEW_KIND"'
  [[ "$output" == *"kind=tip"* ]] || false
  [ ! -e "$CANARY" ]
  # origin/main only
  git -C "$FIX" update-ref refs/remotes/origin/main refs/heads/main
  git -C "$FIX" update-ref -d refs/heads/main
  run_fence '### Pre-check — choose the opposite CLI' '' 'echo "kind=$REVIEW_KIND"'
  [[ "$output" == *"kind=tip"* ]] || false
  [ ! -e "$CANARY" ]
  # neither: no look-alike is used
  git -C "$FIX" update-ref -d refs/remotes/origin/main
  fake_install
  run_fence '### Pre-check — choose the opposite CLI' '' 'echo "kind=$REVIEW_KIND"'
  [[ "$output" == *"kind=installed"* ]] || false
  [ ! -e "$CANARY" ]
}

@test "an origin/main without its object falls back to the local main" {
  make_fixture .
  mkdir -p "$FIX/.git/refs/remotes/origin"
  printf '%040d\n' 1 > "$FIX/.git/refs/remotes/origin/main"
  run_fence '### Pre-check — choose the opposite CLI' '' 'echo "kind=$REVIEW_KIND"'
  [ "$status" -eq 0 ]
  [[ "$output" == *"kind=codex"* ]] || false
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

@test "--all diffs against the main branch, not a tag named main" {
  make_fixture .
  git -C "$FIX" tag main 123-branch
  run_fence '### Capture diff' 'ARGUMENTS=--all' 'printf "%s\n" "$DIFF"'
  [ "$status" -eq 0 ]
  [[ "$output" == *"light evil"* ]] || false
  [[ "$output" != *"Nothing to review"* ]] || false
}

@test "--all diffs against origin/main first, then a local main, and fails without either" {
  make_fixture .
  git -C "$FIX" update-ref refs/remotes/origin/main refs/heads/main
  git -C "$FIX" branch -qf main 123-branch
  run_fence '### Capture diff' 'ARGUMENTS=--all' 'printf "%s\n" "$DIFF"'
  [ "$status" -eq 0 ]
  [[ "$output" == *"light evil"* ]] || false
  git -C "$FIX" branch -qf main refs/remotes/origin/main
  git -C "$FIX" update-ref -d refs/remotes/origin/main
  run_fence '### Capture diff' 'ARGUMENTS=--all' 'printf "%s\n" "$DIFF"'
  [[ "$output" == *"light evil"* ]] || false
  git -C "$FIX" branch -qD main
  git -C "$FIX" tag main 123-branch
  run_fence '### Capture diff' 'ARGUMENTS=--all' ''
  [ "$status" -eq 1 ]
  [[ "$output" == *"GATE: FAIL"* ]] || false
}

@test "a diff that git cannot compute fails the gate instead of reading as empty" {
  make_fixture .
  git -C "$FIX" checkout -q --orphan 124-orphan
  git -C "$FIX" -c user.name=t -c user.email=t@t commit -qm orphan
  for prelude in 'ARGUMENTS=--all' 'set -e; ARGUMENTS=--all'; do
    run_fence '### Capture diff' "$prelude" ''
    [ "$status" -eq 1 ]
    [[ "$output" == *"GATE: FAIL"* ]] || false
    [[ "$output" != *"Nothing to review"* ]] || false
  done
}

@test "the --all tier call resolves the same base by exact name in a fresh shell" {
  NO_RUNTIME=1 make_fixture .
  fake_install
  local tier="$HOME/.claude/lib/feature-fix-swarm/scripts/gsd/review-tier.sh" base name
  printf '#!/usr/bin/env bash\necho "light base=$REVIEW_TIER_BASE"\n' > "$tier"
  chmod +x "$tier"
  base="$(git -C "$FIX" rev-parse main)"
  for name in main origin/main refs/heads/main refs/remotes/origin/main; do
    git -C "$FIX" update-ref "refs/tags/$name" 123-branch
  done
  run_fence '### Tier selection (v1.4.0)' 'DIFF_TARGET=main' 'echo "reason=$TIER_REASON"'
  [[ "$output" == *"reason=base=$base"* ]] || false
  git -C "$FIX" update-ref refs/remotes/origin/main "$base"
  git -C "$FIX" update-ref refs/heads/main 123-branch
  run_fence '### Tier selection (v1.4.0)' 'DIFF_TARGET=main' 'echo "reason=$TIER_REASON"'
  [[ "$output" == *"reason=base=$base"* ]] || false
  git -C "$FIX" update-ref -d refs/heads/main
  run_fence '### Tier selection (v1.4.0)' 'DIFF_TARGET=main' 'echo "reason=$TIER_REASON"'
  [[ "$output" == *"reason=base=$base"* ]] || false
  # no base: never light
  git -C "$FIX" update-ref -d refs/remotes/origin/main
  run_fence '### Tier selection (v1.4.0)' 'DIFF_TARGET=main' 'echo "tier=$TIER"'
  [[ "$output" == *"tier=standard"* ]] || false
}

@test "review helpers are found next to adversary-host.sh from any directory, in bash and zsh" {
  NO_RUNTIME=1 make_fixture .
  real_install
  cp "$FIX/lib/gates.py" "$FIX/lib/model_requests.py"
  mkdir -p "$FIX/a/b"
  local sh lib="$HOME/.claude/lib/feature-fix-swarm"
  for sh in bash zsh; do
    command -v "$sh" >/dev/null || continue
    FENCE_SHELL="$sh" FENCE_CWD="$FIX/a/b" run_fence '### Pre-check — choose the opposite CLI' 'export FFS_HOST=claude' \
      'echo "g=$(adversary_gates)"; echo "m=$(adversary_model_request_helper)"'
    [ "$status" -eq 0 ]
    [[ "$output" == *"g=$lib/scripts/gsd/../../gates.py"* ]] || false
    [[ "$output" == *"m=$lib/scripts/gsd/../../model_requests.py"* ]] || false
  done
}

@test "adversary-host.sh finds its own directory however it is sourced" {
  real_install
  local sh lib="$HOME/.claude/lib/feature-fix-swarm" evil="$BATS_TEST_TMPDIR/evil" other="$BATS_TEST_TMPDIR/other" src
  mkdir -p "$evil/scripts/gsd" "$other"
  printf 'touch %q\n' "$CANARY" | tee "$evil/run-bounded.sh" > "$evil/scripts/gsd/run-bounded.sh"
  : > "$evil/gates.py"
  cp -R "$lib" "$other/lib"
  ln -s "$other/lib/scripts" "$evil/linked"
  for sh in bash zsh; do
    command -v "$sh" >/dev/null || continue
    # absolute, with a cd that prints and a zsh hook that prints
    for src in 'cd() { builtin cd "$@" && echo noise; }' 'chpwd() { echo noise; }' 'if [ -n "${ZSH_VERSION:-}" ]; then setopt posixargzero; fi' \
        'CDPATH="$2"; export CDPATH'; do
      run "$sh" -c "builtin cd \"\$2\"; $src; . \"\$1/scripts/gsd/adversary-host.sh\" && adversary_gates" _ "$lib" "$evil"
      [ "$status" -eq 0 ]
      [ "$output" = "$lib/scripts/gsd/../../gates.py" ]
    done
    # relative, then the caller moves
    run "$sh" -c 'CDPATH="$2"; builtin cd "$1"; . ./scripts/gsd/adversary-host.sh && builtin cd "$2" && adversary_gates' _ "$lib" "$evil"
    [ "$status" -eq 0 ]
    [ "$(cd "$(dirname "$output")" && pwd -P)" = "$(cd "$lib" && pwd -P)" ]
    # found on PATH
    run "$sh" -c 'builtin cd "$2"; PATH="$1/scripts/gsd:$PATH"; . adversary-host.sh && adversary_gates' _ "$lib" "$evil"
    [ "$status" -eq 0 ]
    [ "$output" = "$lib/scripts/gsd/../../gates.py" ]
    # through a symlink into another checkout: that checkout's helpers
    run "$sh" -c 'builtin cd "$1"; . ./linked/../scripts/gsd/adversary-host.sh && adversary_gates' _ "$evil"
    [ "$status" -eq 0 ]
    [ "$(cd -P "$(dirname "$output")" && pwd -P)" = "$(cd "$other/lib" && pwd -P)" ]
    [ ! -e "$CANARY" ]
  done
}

@test "capture diff from a subdirectory covers the whole tree in every mode" {
  local args
  make_fixture .
  mkdir -p "$FIX/docs" "$FIX/.planning"
  echo text > "$FIX/docs/a.md"
  echo plan-line > "$FIX/.planning/p.md"
  git -C "$FIX" add docs .planning
  git -C "$FIX" config diff.relative true
  git -C "$FIX" config diff.noprefix false
  for args in '' --all; do
    [ -z "$args" ] || git -C "$FIX" -c user.name=t -c user.email=t@t commit -qm docs
    FENCE_CWD="$FIX/docs" run_fence '### Capture diff' "ARGUMENTS=$args" 'printf "%s\n" "$DIFF"'
    [ "$status" -eq 0 ]
    [[ "$output" == *"+x = 2"* ]] || false
    [[ "$output" == *"+text"* ]] || false
    [[ "$output" == *"+++ b/docs/a.md"* ]] || false
    [[ "$output" != *plan-line* ]] || false
  done
  # a staged change outside the directory, gone from the work tree
  echo 'x = 9' > "$FIX/src/app.py" && git -C "$FIX" add src && echo 'x = 2' > "$FIX/src/app.py"
  FENCE_CWD="$FIX/docs" run_fence '### Capture diff' '' 'printf "%s\n" "$DIFF"'
  [[ "$output" == *"+x = 9"* ]] || false
  git -C "$FIX" reset -q
  # unstaged only: the HEAD fallback
  echo 'x = 3' > "$FIX/src/app.py"
  FENCE_CWD="$FIX/docs" run_fence '### Capture diff' '' 'printf "%s\n" "$DIFF"'
  [[ "$output" == *"+x = 3"* ]] || false
}

@test "capture diff excludes only the top-level run bookkeeping" {
  make_fixture .
  mkdir -p "$FIX/spike-results" "$FIX/docs/spike-results"
  echo top-line > "$FIX/spike-results/s.md"
  echo nested-line > "$FIX/docs/spike-results/s.md"
  git -C "$FIX" add spike-results docs
  for cwd in "$FIX" "$FIX/docs"; do
    FENCE_CWD="$cwd" run_fence '### Capture diff' '' 'printf "%s\n" "$DIFF"'
    [[ "$output" != *top-line* ]] || false
    [[ "$output" == *nested-line* ]] || false
  done
}

@test "a .gitmodules ignore setting cannot hide a submodule bump from the diff" {
  local old new args
  make_fixture .
  git -C "$FIX" config diff.submodule short
  old="$(git -C "$FIX" rev-parse HEAD~1)" new="$(git -C "$FIX" rev-parse HEAD)"
  git -C "$FIX" checkout -q main
  git -C "$FIX" update-index --add --cacheinfo "160000,$old,vendor/m"
  printf '[submodule "m"]\n\tpath = vendor/m\n\turl = ./m\n\tignore = all\n' > "$FIX/.gitmodules"
  git -C "$FIX" add .gitmodules
  git -C "$FIX" -c user.name=t -c user.email=t@t commit -qm sub
  git -C "$FIX" checkout -q -f -B 123-branch main
  git -C "$FIX" update-index --cacheinfo "160000,$new,vendor/m"
  [ -z "$(git -C "$FIX" diff --staged --name-only)" ]  # git itself hides it
  for args in '' --file=vendor/m --all; do
    [ "$args" != --all ] || git -C "$FIX" -c user.name=t -c user.email=t@t commit -qm bump
    run_fence '### Capture diff' "ARGUMENTS=$args" 'printf "%s\n" "$DIFF"'
    [ "$status" -eq 0 ]
    [[ "$output" == *"+Subproject commit $new"* ]] || { echo "args=$args: $output"; false; }
  done
}

@test "a file named like a bookkeeping directory is still reviewed" {
  make_fixture .
  echo planning-file > "$FIX/.planning"
  echo spike-file > "$FIX/spike-results"
  git -C "$FIX" add .planning spike-results
  run_fence '### Capture diff' '' 'printf "%s\n" "$DIFF"'
  [[ "$output" == *planning-file* ]] || false
  [[ "$output" == *spike-file* ]] || false
}

@test "inherited pathspec settings cannot empty the diff" {
  local var
  make_fixture .
  for var in GIT_LITERAL_PATHSPECS GIT_ICASE_PATHSPECS; do
    run_fence '### Capture diff' "export $var=1" 'printf "%s\n" "$DIFF"'
    [[ "$output" == *"+x = 2"* ]] || { echo "$var: $output"; false; }
  done
  git -C "$FIX" reset -q --hard
  printf '100644 %s\tSPIKE-RESULTS/x.py\0' "$(git -C "$FIX" hash-object -w "$FIX/src/app.py")" |
    git -C "$FIX" update-index -z --index-info
  run_fence '### Capture diff' 'export GIT_ICASE_PATHSPECS=1' 'printf "%s\n" "$DIFF"'
  [[ "$output" == *"SPIKE-RESULTS/x.py"* ]] || false
  git -C "$FIX" reset -q --hard
  FENCE_CWD="$FIX/src" run_fence '### Capture diff' 'export GIT_LITERAL_PATHSPECS=1 ARGUMENTS=--all' 'printf "%s\n" "$DIFF"'
  [[ "$output" == *"+++ "* ]] || false
}
