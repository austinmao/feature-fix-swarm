#!/usr/bin/env bats
# credential-output-guard.sh — PreToolUse blocker for commands that print
# credential values into an agent transcript. Mutations/injection through
# `doppler run` stay available; secret-name-only inventory stays available.

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  HOOK="$REPO_ROOT/scripts/hooks/credential-output-guard.sh"
}

envelope() {
  python3 -c 'import json,sys; print(json.dumps({"tool_name":"Bash","tool_input":{"command":sys.argv[1]}}))' "$1"
}

@test "CG-001: doppler value output forms are blocked" {
  for command in \
    "doppler secrets --json" \
    "doppler secrets get API_TOKEN --plain" \
    "doppler secrets download --no-file --format json"; do
    run bash -c "$(declare -f envelope); envelope '$command' | bash '$HOOK'"
    [ "$status" -eq 2 ]
    [[ "$output" == *"credential values"* ]]
  done
}

@test "CG-002: railway variable and JSON config output forms are blocked" {
  for command in \
    "railway variables" \
    "railway variables --json" \
    "railway --service production variables --json" \
    "railway config --json"; do
    run bash -c "$(declare -f envelope); envelope '$command' | bash '$HOOK'"
    [ "$status" -eq 2 ]
  done
}

@test "CG-003: names-only inventory and env injection pass" {
  run bash -c "$(declare -f envelope); envelope 'doppler secrets --only-names' | bash '$HOOK'"
  [ "$status" -eq 0 ]
  run bash -c "$(declare -f envelope); envelope 'doppler run -p app -c prd -- ./deploy.sh' | bash '$HOOK'"
  [ "$status" -eq 0 ]
  run bash -c "$(declare -f envelope); envelope 'railway whoami && railway status' | bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "CG-004: nested shell payload cannot bypass the guard" {
  for command in \
    "bash -lc 'railway variables --json'" \
    "bash -c -- 'doppler secrets --json'" \
    "bash -ec 'doppler secrets --json'" \
    "zsh -fc 'doppler secrets get API_TOKEN --plain'"; do
    run bash -c "$(declare -f envelope); envelope \"$command\" | bash '$HOOK'"
    [ "$status" -eq 2 ]
  done
}

@test "CG-005: Codex cmd envelopes are inspected" {
  python3 -c 'import json; print(json.dumps({"tool_name":"exec_command","tool_input":{"cmd":"doppler secrets --json"}}))' \
    > "$BATS_TEST_TMPDIR/envelope.json"
  run bash "$HOOK" < "$BATS_TEST_TMPDIR/envelope.json"
  [ "$status" -eq 2 ]
}

@test "CG-006: kill-switch and malformed input fail open" {
  # The kill-switch branch reaches waiver-record.sh via
  # "$SCRIPT_DIR/../gsd/waiver-record.sh" — script-relative, ignoring
  # cwd/GATES_STORE — so running $REPO_ROOT's real hook would write into the
  # developer's real canonical evidence store. Isolate via a fixture-local
  # copy (see waiver-record.bats WR-140).
  fixture="$BATS_TEST_TMPDIR/cg006-fixture"
  mkdir -p "$fixture/scripts/hooks" "$fixture/scripts/gsd" "$fixture/lib"
  cp "$REPO_ROOT/scripts/hooks/credential-output-guard.sh" "$fixture/scripts/hooks/credential-output-guard.sh"
  cp "$REPO_ROOT/scripts/gsd/waiver-record.sh" "$fixture/scripts/gsd/waiver-record.sh"
  cp "$REPO_ROOT/lib/gates.py" "$fixture/lib/gates.py"
  chmod +x "$fixture/scripts/hooks/credential-output-guard.sh" "$fixture/scripts/gsd/waiver-record.sh"
  git -C "$fixture" init -q
  fixture_hook="$fixture/scripts/hooks/credential-output-guard.sh"
  run bash -c "$(declare -f envelope); envelope 'railway variables' | CREDENTIAL_OUTPUT_GUARD=off bash '$fixture_hook'"
  [ "$status" -eq 0 ]
  run bash -c "printf 'not-json' | bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "CG-010: deterministic scan-file mode accepts clean content and blocks credentials" {
  clean="$BATS_TEST_TMPDIR/clean-handoff.md"
  secret="$BATS_TEST_TMPDIR/secret-handoff.md"
  printf 'Progress: tests passed\n' > "$clean"
  printf 'API_TOKEN=sk_123456789012345678\n' > "$secret"
  run bash "$HOOK" --scan-file "$clean"
  [ "$status" -eq 0 ]
  run bash "$HOOK" --scan-file "$secret"
  [ "$status" -eq 2 ]
  run bash "$HOOK" --scan-file "$BATS_TEST_TMPDIR/missing"
  [ "$status" -eq 2 ]
}

@test "CG-007: eval and common execution wrappers cannot bypass the guard" {
  for command in \
    "eval 'doppler secrets --json'" \
    "sudo doppler secrets --json" \
    "nice railway variables" \
    "nohup doppler secrets download --no-file --format json"; do
    run bash -c "$(declare -f envelope); envelope \"$command\" | bash '$HOOK'"
    [ "$status" -eq 2 ]
  done
}

@test "CG-008: command substitution cannot hide credential-value reads" {
  commands=(
    'echo `doppler secrets get API_TOKEN --plain`'
    'echo "$(doppler secrets --json)"'
    'printf "%s" "$(railway --service production variables --json)"'
  )
  for command in "${commands[@]}"; do
    run bash -c "$(declare -f envelope); envelope \"\$1\" | bash '$HOOK'" _ "$command"
    [ "$status" -eq 2 ]
  done
}

# ── AC-007: writer-seam execution coverage ───────────────────────────────────
# scripts/gsd/publish-scanned-handoff.sh — the single copy-then-scan-then-
# publish harness every handoff writer routes through. Each of the three
# skills carries an extractable ```bash handoff-scan block that these cases
# extract from the REAL SKILL.md and EXECUTE against a fixture-local copy of
# the harness (never the real repo — see WR-140 for why a script-relative
# resolver requires a fixture, not GATES_STORE/cwd redirection).

extract_handoff_scan_block() {
  # extract_handoff_scan_block <skill-md-path> — mirrors the fence-extraction
  # shape of scripts/verify-skill-blocks.py's BLOCK_RE, but for the
  # `bash handoff-scan` marker, which is deliberately a different fence than
  # `bash verify` so that gate stays inert to it.
  python3 -c '
import re, sys, pathlib
text = pathlib.Path(sys.argv[1]).read_text()
pat = re.compile(r"^[ \t]*```bash handoff-scan\n(.*?)^[ \t]*```", re.M | re.S)
blocks = pat.findall(text)
assert len(blocks) == 1, f"expected exactly one handoff-scan block in {sys.argv[1]}, found {len(blocks)}"
sys.stdout.write(blocks[0])
' "$1"
}

build_handoff_fixture() {
  # build_handoff_fixture <dir> — a git repo carrying its own copy of the
  # harness + scanner + guard, so REPO_ROOT (passed to the extracted block)
  # never resolves to the developer's real repo.
  local dir="$1"
  mkdir -p "$dir/scripts/gsd" "$dir/scripts/hooks"
  cp "$REPO_ROOT/scripts/gsd/publish-scanned-handoff.sh" "$dir/scripts/gsd/"
  cp "$REPO_ROOT/scripts/gsd/scan-handoff-credentials.sh" "$dir/scripts/gsd/"
  cp "$REPO_ROOT/scripts/hooks/credential-output-guard.sh" "$dir/scripts/hooks/"
  chmod +x "$dir"/scripts/gsd/*.sh "$dir"/scripts/hooks/*.sh
  git -C "$dir" init -q -b main
  # Repo-local identity: the handoff-scan block runs publish-scanned-handoff
  # inside this fixture, and its commit needs an author on hosts with no
  # global gitconfig (PR #103 CI: "Author identity unknown").
  git -C "$dir" config user.email t@t
  git -C "$dir" config user.name t
  git -C "$dir" commit -q --allow-empty -m init
}

@test "CG-020: continue-compact.sh handoff-scan block extracts and executes — clean, finding, absent-guard" {
  local skill block fixture before
  skill="$REPO_ROOT/skills/continue-compact/SKILL.md"
  block="$(extract_handoff_scan_block "$skill")"
  [ -n "$block" ]

  # clean-passes
  fixture="$BATS_TEST_TMPDIR/cg020-clean"
  build_handoff_fixture "$fixture"
  echo "clean handoff body" > "$fixture/handoff.md"
  before="$(git -C "$fixture" rev-list --count HEAD)"
  run env REPO_ROOT="$fixture" HANDOFF_PATH="$fixture/handoff.md" bash -c "$block"
  [ "$status" -eq 0 ]
  [ "$(git -C "$fixture" rev-list --count HEAD)" -eq "$((before + 1))" ]
  [ "$(git -C "$fixture" show HEAD:handoff.md)" = "clean handoff body" ]

  # finding-blocks
  fixture="$BATS_TEST_TMPDIR/cg020-finding"
  build_handoff_fixture "$fixture"
  echo "API_TOKEN=sk_123456789012345678" > "$fixture/handoff.md"
  before="$(git -C "$fixture" rev-list --count HEAD)"
  run env REPO_ROOT="$fixture" HANDOFF_PATH="$fixture/handoff.md" bash -c "$block"
  [ "$status" -ne 0 ]
  [ -z "$(git -C "$fixture" diff --cached --name-only)" ]
  [ "$(git -C "$fixture" rev-list --count HEAD)" -eq "$before" ]

  # absent-guard-warns
  fixture="$BATS_TEST_TMPDIR/cg020-absent"
  build_handoff_fixture "$fixture"
  rm -f "$fixture/scripts/hooks/credential-output-guard.sh"
  echo "clean handoff body" > "$fixture/handoff.md"
  before="$(git -C "$fixture" rev-list --count HEAD)"
  run env REPO_ROOT="$fixture" HANDOFF_PATH="$fixture/handoff.md" bash -c "$block"
  [ "$status" -eq 0 ]
  [[ "$output" == *WARN* ]]
  [ "$(git -C "$fixture" rev-list --count HEAD)" -eq "$((before + 1))" ]
}

@test "CG-021: goal-wrap.sh handoff-scan block extracts and executes — clean, finding, absent-guard" {
  local skill block fixture
  skill="$REPO_ROOT/skills/goal-wrap/SKILL.md"
  block="$(extract_handoff_scan_block "$skill")"
  [ -n "$block" ]

  # clean-passes
  fixture="$BATS_TEST_TMPDIR/cg021-clean"
  build_handoff_fixture "$fixture"
  echo "clean handoff body" > "$fixture/handoff.md"
  run env REPO_ROOT="$fixture" HANDOFF_PATH="$fixture/handoff.md" HANDOFF_DEST="$fixture/dest.md" bash -c "$block"
  [ "$status" -eq 0 ]
  [ "$(cat "$fixture/dest.md")" = "clean handoff body" ]

  # finding-blocks — destination left absent
  fixture="$BATS_TEST_TMPDIR/cg021-finding"
  build_handoff_fixture "$fixture"
  echo "API_TOKEN=sk_123456789012345678" > "$fixture/handoff.md"
  run env REPO_ROOT="$fixture" HANDOFF_PATH="$fixture/handoff.md" HANDOFF_DEST="$fixture/dest.md" bash -c "$block"
  [ "$status" -ne 0 ]
  [ ! -e "$fixture/dest.md" ]

  # absent-guard-warns
  fixture="$BATS_TEST_TMPDIR/cg021-absent"
  build_handoff_fixture "$fixture"
  rm -f "$fixture/scripts/hooks/credential-output-guard.sh"
  echo "clean handoff body" > "$fixture/handoff.md"
  run env REPO_ROOT="$fixture" HANDOFF_PATH="$fixture/handoff.md" HANDOFF_DEST="$fixture/dest.md" bash -c "$block"
  [ "$status" -eq 0 ]
  [[ "$output" == *WARN* ]]
  [ "$(cat "$fixture/dest.md")" = "clean handoff body" ]
}

@test "CG-022: spec-status.sh handoff-scan block extracts and executes — clean, finding, absent-guard, --no-handoff" {
  local skill block fixture before
  skill="$REPO_ROOT/skills/spec-status/SKILL.md"
  block="$(extract_handoff_scan_block "$skill")"
  [ -n "$block" ]

  # clean-passes: status + handoff both present -> two commits
  fixture="$BATS_TEST_TMPDIR/cg022-clean"
  build_handoff_fixture "$fixture"
  echo "status body" > "$fixture/status.md"
  echo "handoff body" > "$fixture/handoff.md"
  before="$(git -C "$fixture" rev-list --count HEAD)"
  run env REPO_ROOT="$fixture" STATUS_PATH="$fixture/status.md" HANDOFF_PATH="$fixture/handoff.md" bash -c "$block"
  [ "$status" -eq 0 ]
  [ "$(git -C "$fixture" rev-list --count HEAD)" -eq "$((before + 2))" ]

  # --no-handoff preserved: HANDOFF_PATH unset -> only status committed
  fixture="$BATS_TEST_TMPDIR/cg022-nohandoff"
  build_handoff_fixture "$fixture"
  echo "status body" > "$fixture/status.md"
  before="$(git -C "$fixture" rev-list --count HEAD)"
  run env -u HANDOFF_PATH REPO_ROOT="$fixture" STATUS_PATH="$fixture/status.md" bash -c "$block"
  [ "$status" -eq 0 ]
  [ "$(git -C "$fixture" rev-list --count HEAD)" -eq "$((before + 1))" ]

  # finding-blocks (status artifact)
  fixture="$BATS_TEST_TMPDIR/cg022-finding"
  build_handoff_fixture "$fixture"
  echo "API_TOKEN=sk_123456789012345678" > "$fixture/status.md"
  before="$(git -C "$fixture" rev-list --count HEAD)"
  run env -u HANDOFF_PATH REPO_ROOT="$fixture" STATUS_PATH="$fixture/status.md" bash -c "$block"
  [ "$status" -ne 0 ]
  [ -z "$(git -C "$fixture" diff --cached --name-only)" ]
  [ "$(git -C "$fixture" rev-list --count HEAD)" -eq "$before" ]

  # absent-guard-warns
  fixture="$BATS_TEST_TMPDIR/cg022-absent"
  build_handoff_fixture "$fixture"
  rm -f "$fixture/scripts/hooks/credential-output-guard.sh"
  echo "status body" > "$fixture/status.md"
  before="$(git -C "$fixture" rev-list --count HEAD)"
  run env -u HANDOFF_PATH REPO_ROOT="$fixture" STATUS_PATH="$fixture/status.md" bash -c "$block"
  [ "$status" -eq 0 ]
  [[ "$output" == *WARN* ]]
  [ "$(git -C "$fixture" rev-list --count HEAD)" -eq "$((before + 1))" ]
}

@test "CG-023: publish-scanned-handoff.sh source never reads a git index blob as its scan target" {
  local hits
  hits="$(grep -v '^[[:space:]]*#' "$REPO_ROOT/scripts/gsd/publish-scanned-handoff.sh" | grep -cE 'git (show|cat-file)' || true)"
  [ "$hits" -eq 0 ]
}

# ── COMMIT-WINDOW CLOSURE pins ───────────────────────────────────────────────

@test "CG-024: a working-tree swap after staging does not change the committed bytes" {
  local fixture blob committed
  fixture="$BATS_TEST_TMPDIR/cg024"
  build_handoff_fixture "$fixture"
  mkdir -p "$fixture/docs"
  echo "scanned content" > "$fixture/docs/handoff.md"
  git -C "$fixture" add docs/handoff.md
  git -C "$fixture" -c user.email=t@t -c user.name=t commit -q -m seed
  # Stage the SCANNED blob directly (the harness's own mechanism), then
  # swap the working-tree file to tampered content before committing — the
  # exact race this design closes: a plain `git commit -- docs/handoff.md`
  # would re-read the (now tampered) working tree; committing the index
  # with no pathspec must not.
  echo "scanned content v2" > /tmp/cg024-scanned-copy.txt
  blob="$(git -C "$fixture" hash-object -w /tmp/cg024-scanned-copy.txt)"
  git -C "$fixture" update-index --add --cacheinfo 100644,"$blob",docs/handoff.md
  echo "TAMPERED after staging" > "$fixture/docs/handoff.md"
  git -C "$fixture" -c user.email=t@t -c user.name=t commit --no-verify -q -m "no-pathspec commit"
  committed="$(git -C "$fixture" rev-parse HEAD:docs/handoff.md)"
  rm -f /tmp/cg024-scanned-copy.txt
  [ "$committed" = "$blob" ]
  [ "$(git -C "$fixture" show HEAD:docs/handoff.md)" = "scanned content v2" ]
}

@test "CG-025: publish-scanned-handoff.sh refuses to commit alongside unrelated staged content instead of sweeping it in" {
  local fixture block
  fixture="$BATS_TEST_TMPDIR/cg025"
  build_handoff_fixture "$fixture"
  mkdir -p "$fixture/docs"
  echo "unrelated" > "$fixture/docs/other.md"
  git -C "$fixture" add docs/other.md
  echo "clean handoff body" > "$fixture/docs/handoff.md"
  local before
  before="$(git -C "$fixture" rev-list --count HEAD)"
  run bash "$fixture/scripts/gsd/publish-scanned-handoff.sh" "$fixture/docs/handoff.md" --commit "test: refuse"
  [ "$status" -ne 0 ]
  [ "$(git -C "$fixture" rev-list --count HEAD)" -eq "$before" ]
  # the unrelated staged content is untouched (still staged, not committed)
  [ "$(git -C "$fixture" diff --cached --name-only)" = "docs/other.md" ]
}

@test "CG-009: timeout and nested env wrappers cannot bypass the guard" {
  for command in \
    "timeout 10 doppler secrets --json" \
    "gtimeout 10 railway variables" \
    "sudo env SAFE=1 doppler secrets get API_TOKEN --plain" \
    "nice timeout 10 railway --service production variables --json"; do
    run bash -c "$(declare -f envelope); envelope \"$command\" | bash '$HOOK'"
    [ "$status" -eq 2 ]
  done
}

@test "CG-010: host pipe left open does not make the hook wait for EOF" {
  run python3 -c '
import json
import subprocess
import sys

hook = sys.argv[1]
payload = json.dumps({"tool_input": {"command": "git status"}}) + "\n"
proc = subprocess.Popen(
    ["bash", hook],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
proc.stdin.write(payload)
proc.stdin.flush()
try:
    rc = proc.wait(timeout=1)
except subprocess.TimeoutExpired:
    proc.kill()
    proc.wait()
    raise SystemExit("hook waited for EOF instead of consuming the JSON line")
raise SystemExit(rc)
' "$HOOK"
  [ "$status" -eq 0 ]
}
