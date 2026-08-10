#!/usr/bin/env bats

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  # Hermetic with respect to an active parent GSD drive: the suite must
  # produce identical results whether or not it runs inside a live drive, so
  # scrub every runtime variable gsd-run.sh reads (plus the git redirection
  # trio) BEFORE any fixture runs. Scrubbing here rather than relying on an
  # `env -u` invocation incantation is deliberate: the evidence must be
  # trustworthy for whoever runs the suite next, not only for the person who
  # remembers the flags. Cases that need one of these set it explicitly.
  unset GSD_ACTIVE_DRIVE GSD_RUN_ID GSD_RUN_STATE_DIR GSD_MACHINE_ID \
        GSD_RESUME GSD_TOKEN_BUDGET GSD_HEARTBEAT_SECS GSD_FOREIGN_LEASE_SECS \
        GSD_RECLAIM_LEASE_SECS GSD_SANDBOX_MODE GSD_NETWORK_MODE \
        GATES_STORE GIT_DIR GIT_COMMON_DIR GIT_WORK_TREE || true
  RF_REAL_HOME="${HOME:-}"
  export HOME="$BATS_TEST_TMPDIR/hermetic-home"
  mkdir -p "$HOME"
  HARNESS_ROOT="$BATS_TEST_TMPDIR/runner-layout"
  mkdir -p "$HARNESS_ROOT/scripts"
  cp -R "$ROOT/scripts/gsd" "$HARNESS_ROOT/scripts/gsd"
  cp -R "$ROOT/lib" "$HARNESS_ROOT/lib"
  SCRIPT="$HARNESS_ROOT/scripts/gsd/gsd-run.sh"
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  CODEX_SOURCE_ROOT="$BATS_TEST_TMPDIR/codex-root"
  PROJECT_AGENTS_ROOT="$BATS_TEST_TMPDIR/.agents"
  USER_AGENTS_ROOT="$BATS_TEST_TMPDIR/user-agents"
  CLAUDE_SKILLS_ROOT="$BATS_TEST_TMPDIR/claude-skills"
  GSD_PACKAGE_ROOT="$BATS_TEST_TMPDIR/gsd-package"
  TRUSTED_GRANT_DIR="$BATS_TEST_TMPDIR/danger"
  git -C "$BATS_TEST_TMPDIR" init -q
  git -C "$BATS_TEST_TMPDIR" config user.email test@example.com
  git -C "$BATS_TEST_TMPDIR" config user.name Test
  printf '%s\n' seed > "$BATS_TEST_TMPDIR/seed"
  git -C "$BATS_TEST_TMPDIR" add seed
  git -C "$BATS_TEST_TMPDIR" commit -qm seed
  mkdir -p "$STUB_DIR" \
    "$PROJECT_AGENTS_ROOT/skills" "$USER_AGENTS_ROOT/skills/gsd-quick" \
    "$CODEX_SOURCE_ROOT/agents" "$CODEX_SOURCE_ROOT/gsd-core" \
    "$CODEX_SOURCE_ROOT/hooks" \
    "$GSD_PACKAGE_ROOT/hooks/dist" "$GSD_PACKAGE_ROOT/hooks/sibling" "$TRUSTED_GRANT_DIR" \
    "$CLAUDE_SKILLS_ROOT/gsd-quick"
  printf '%s\n' '---' 'name: gsd-quick' '---' > "$USER_AGENTS_ROOT/skills/gsd-quick/SKILL.md"
  printf '%s\n' 'name = "gsd-executor"' 'model = "sonnet"' > "$CODEX_SOURCE_ROOT/agents/gsd-executor.toml"
  printf '%s\n' '# executor' > "$CODEX_SOURCE_ROOT/agents/gsd-executor.md"
  printf '%s\n' '1.10.0' > "$CODEX_SOURCE_ROOT/gsd-core/VERSION"
  cat > "$CODEX_SOURCE_ROOT/hooks/gsd-context-monitor.js" <<EOF
process.stdin.resume();
process.stdin.on('end', () => require('fs').writeFileSync('$BATS_TEST_TMPDIR/hook.smoked', 'yes\n'));
EOF
  cat > "$CODEX_SOURCE_ROOT/hooks/gsd-check-update.js" <<'EOF'
process.stdin.resume();
process.stdin.on('end', () => process.exit(0));
EOF
  printf '%s\n' 'module.exports = true;' > "$CODEX_SOURCE_ROOT/hooks/gsd-check-update-worker.js"
  printf '%s\n' 'module.exports = true;' > "$CODEX_SOURCE_ROOT/hooks/managed-hooks-registry.cjs"
  printf '%s\n' '{"type":"commonjs"}' > "$CODEX_SOURCE_ROOT/hooks/package.json"
  cp "$CODEX_SOURCE_ROOT/hooks/gsd-context-monitor.js" "$GSD_PACKAGE_ROOT/hooks/dist/gsd-context-monitor.js"
  cp "$CODEX_SOURCE_ROOT/hooks/gsd-check-update.js" "$GSD_PACKAGE_ROOT/hooks/dist/gsd-check-update.js"
  cp "$CODEX_SOURCE_ROOT/hooks/gsd-check-update-worker.js" "$GSD_PACKAGE_ROOT/hooks/dist/gsd-check-update-worker.js"
  cp "$CODEX_SOURCE_ROOT/hooks/managed-hooks-registry.cjs" "$GSD_PACKAGE_ROOT/hooks/dist/managed-hooks-registry.cjs"
  printf '%s\n' 'module.exports = true;' > "$GSD_PACKAGE_ROOT/hooks/sibling/dependency.js"
  printf '%s\n' '{"name":"@opengsd/gsd-core","version":"1.10.0"}' > "$GSD_PACKAGE_ROOT/package.json"
  NODE_ON_PATH="$(command -v node)"
  [ -x "$NODE_ON_PATH" ]
  SAFE_NODE="$BATS_TEST_TMPDIR/trusted-node"
  cat > "$SAFE_NODE" <<EOF
#!/bin/sh
exec "$NODE_ON_PATH" "\$@"
EOF
  chmod 700 "$SAFE_NODE"
  python3 - "$CODEX_SOURCE_ROOT/hooks.json" "$CODEX_SOURCE_ROOT/hooks" "$SAFE_NODE" <<'PY'
import json, sys
path, hooks, node = sys.argv[1:]
events = {
    "SessionStart": "gsd-check-update.js",
    "SubagentStart": "gsd-context-monitor.js", "Stop": "gsd-context-monitor.js",
    "PostToolUse": "gsd-context-monitor.js", "PreToolUse": "gsd-context-monitor.js",
    "PermissionRequest": "gsd-context-monitor.js", "PreCompact": "gsd-context-monitor.js",
    "PostCompact": "gsd-context-monitor.js", "SubagentStop": "gsd-context-monitor.js",
    "UserPromptSubmit": "gsd-context-monitor.js",
}
data = {"hooks": {event: [{"hooks": [{"type": "command", "command": f'{node} "{hooks}/{target}"'}]}] for event, target in events.items()}}
open(path, "w").write(json.dumps(data))
PY
  TRUSTED_NODE="$SAFE_NODE"
  python3 - "$SCRIPT" "$CODEX_SOURCE_ROOT" "$USER_AGENTS_ROOT" "$GSD_PACKAGE_ROOT" "$TRUSTED_GRANT_DIR/danger-grants.json" "$BATS_TEST_TMPDIR/auth.lock" "$TRUSTED_NODE" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
text = path.read_text()
replacements = {
    'CODEX_SOURCE_ROOT_FIXED="$REAL_USER_HOME/.codex"': f'CODEX_SOURCE_ROOT_FIXED="{sys.argv[2]}"',
    'USER_AGENTS_ROOT_FIXED="$REAL_USER_HOME/.agents"': f'USER_AGENTS_ROOT_FIXED="{sys.argv[3]}"',
    'GSD_PACKAGE_ROOT_FIXED="$SCRIPT_DIR/../../node_modules/@opengsd/gsd-core"': f'GSD_PACKAGE_ROOT_FIXED="{sys.argv[4]}"',
    'DANGER_GRANT_STORE_FIXED="$REAL_USER_HOME/.cache/feature-fix-swarm/danger-grants.json"': f'DANGER_GRANT_STORE_FIXED="{sys.argv[5]}"',
    'AUTH_LOCK_DIR_FIXED="$REAL_USER_HOME/.cache/feature-fix-swarm/codex-auth.lock"': f'AUTH_LOCK_DIR_FIXED="{sys.argv[6]}"',
    'TRUSTED_NODE_BIN_FIXED=""': f'TRUSTED_NODE_BIN_FIXED="{sys.argv[7]}"',
    'FFS_USER_MANIFEST_FIXED="$REAL_USER_HOME/.cache/feature-fix-swarm/install-manifest.json"': f'FFS_USER_MANIFEST_FIXED="{sys.argv[3]}/install-manifest.json"',
}
for old, new in replacements.items():
    if text.count(old) != 1:
        raise SystemExit(f"runner fixture patch target missing: {old}")
    text = text.replace(old, new)
path.write_text(text)
PY
  cat > "$CODEX_SOURCE_ROOT/config.toml" <<'EOF'
# GSD Agent Configuration — managed by gsd-core installer
[features]
hooks = true
[agents]
max_depth = 1
EOF
  printf '%s\n' '{"refresh_token":"initial"}' > "$CODEX_SOURCE_ROOT/auth.json"
  chmod 600 "$CODEX_SOURCE_ROOT/auth.json"
  agent_toml_hash="$(shasum -a 256 "$CODEX_SOURCE_ROOT/agents/gsd-executor.toml" | awk '{print $1}')"
  agent_md_hash="$(shasum -a 256 "$CODEX_SOURCE_ROOT/agents/gsd-executor.md" | awk '{print $1}')"
  version_hash="$(shasum -a 256 "$CODEX_SOURCE_ROOT/gsd-core/VERSION" | awk '{print $1}')"
  quick_skill_hash="$(shasum -a 256 "$USER_AGENTS_ROOT/skills/gsd-quick/SKILL.md" | awk '{print $1}')"
  cat > "$CODEX_SOURCE_ROOT/gsd-file-manifest.json" <<EOF
{"version":"1.10.0","files":{"agents/gsd-executor.toml":"$agent_toml_hash","agents/gsd-executor.md":"$agent_md_hash","gsd-core/VERSION":"$version_hash","skills/gsd-quick/SKILL.md":"$quick_skill_hash"}}
EOF
  printf '%s\n' '---' 'name: gsd-quick' '---' > "$CLAUDE_SKILLS_ROOT/gsd-quick/SKILL.md"

  cat > "$STUB_DIR/fake-codex" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = "--version" ]; then
  echo "codex-cli \${FAKE_CODEX_VERSION:-0.146.1}"
  exit 0
fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then
  touch "$BATS_TEST_TMPDIR/codex.probed"
  printf '%s\n' "\${OPENAI_API_KEY-unset}" > "$BATS_TEST_TMPDIR/codex.probe-api-key"
  case "\${FAKE_CODEX_PROBE_MODE:-ok}" in
    bad_ack) echo 'probe responded without acknowledgement'; exit 0 ;;
    fail) echo 'native quota exhausted API_TOKEN=super-secret-value-123456789 api_key=xYz bearer tiny' >&2; exit 69 ;;
    sol_unavailable)
      if [[ "\$*" == *gpt-5.6-sol* ]]; then
        echo 'requested Codex model unavailable' >&2
        exit 69
      fi
      ;;
  esac
  echo FFS_HOST_PROBE_READY
  exit 0
fi
printf '%s\n' "\$@" > "$BATS_TEST_TMPDIR/codex.args"
printf '%s\n' "\${GSD_ACTIVE_DRIVE-unset}" > "$BATS_TEST_TMPDIR/codex.active-drive"
printf '%s\n' "\${CODEX_HOME:-}" > "$BATS_TEST_TMPDIR/codex.home"
pwd -P > "$BATS_TEST_TMPDIR/codex.cwd"
printf '%s\n' "\${CODEX_HOME:-}"/skills/*/SKILL.md > "$BATS_TEST_TMPDIR/codex.skills"
cat "\${CODEX_HOME:-}"/skills/*/SKILL.md > "$BATS_TEST_TMPDIR/codex.skill-content"
printf '%s\n' "\${OPENAI_API_KEY-unset}" > "$BATS_TEST_TMPDIR/codex.api-key"
python3 -c 'import os,stat,sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode))[2:])' \
  "\${CODEX_HOME:-}/auth.json" > "$BATS_TEST_TMPDIR/codex.auth-mode"
cp "\${CODEX_HOME:-}/config.toml" "$BATS_TEST_TMPDIR/codex.config"
cp "\${CODEX_HOME:-}/hooks.json" "$BATS_TEST_TMPDIR/codex.hooks-json"
[ -f "\${CODEX_HOME:-}/hooks/sibling/dependency.js" ] && touch "$BATS_TEST_TMPDIR/complete-hooks-copied"
if [ "\${FAKE_CODEX_REFRESH_AUTH:-0}" = 1 ]; then
  if [ -n "\${FAKE_CODEX_CONCURRENT_AUTH_FILE:-}" ]; then
    printf '%s\n' '{"refresh_token":"concurrent"}' > "\$FAKE_CODEX_CONCURRENT_AUTH_FILE"
  fi
  printf '%s\n' '{"refresh_token":"refreshed"}' > "\${CODEX_HOME:-}/auth.json"
fi
echo CODEX_OK
exit "\${FAKE_CODEX_DRIVE_RC:-0}"
EOF
  cat > "$STUB_DIR/fake-claude" <<EOF
#!/usr/bin/env bash
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then
  touch "$BATS_TEST_TMPDIR/claude.probed"
  case "\${FAKE_CLAUDE_PROBE_MODE:-ok}" in
    bad_ack) echo 'probe responded without acknowledgement'; exit 0 ;;
    fail) echo 'alternate model unavailable' >&2; exit 69 ;;
  esac
  echo FFS_HOST_PROBE_READY
  exit 0
fi
printf '%s\n' "\$@" > "$BATS_TEST_TMPDIR/claude.args"
echo CLAUDE_OK
EOF
  chmod +x "$STUB_DIR/fake-codex" "$STUB_DIR/fake-claude"
  export PATH="$STUB_DIR:$PATH"
  export GSD_CLAUDE_SKILLS_ROOT="$CLAUDE_SKILLS_ROOT"
  export GSD_NETWORK_MODE=enabled
  export GSD_NETWORK_PURPOSE=general
}

teardown() {
  :
}

@test "tail token trailer is accounted after a successful drive" {
  cat > "$STUB_DIR/token-codex" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = --version ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then echo FFS_HOST_PROBE_READY; exit 0; fi
echo 'tokens used: 999999'
echo DRIVE_OK
echo 'tokens used: 42'
EOF
  chmod +x "$STUB_DIR/token-codex"
  run env -u GSD_ACTIVE_DRIVE FFS_HOST=codex CODEX_BIN=token-codex CLAUDE_BIN=fake-claude GSD_RUN_ID=spec-008 \
    RUN_STATE_DB="$BATS_TEST_TMPDIR/run-state.sqlite" GATES_STORE="$BATS_TEST_TMPDIR/evidence.json" \
    bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick tokens"
  [ "$status" -eq 0 ]
  run python3 -c "import sqlite3; c=sqlite3.connect('$BATS_TEST_TMPDIR/run-state.sqlite'); print(c.execute('select tokens_used from runs').fetchone()[0])"
  [ "$output" = 42 ]
}

@test "real codex two-line comma trailer (tokens used / 124,988) is accounted" {
  # The live codex CLI prints the trailer as TWO lines — 'tokens used' then
  # a comma-grouped count — not the single-line colon form. First
  # integration drive WARNed BUDGET-ACCOUNTING-UNAVAILABLE on every real
  # run while the colon-only fixture stayed green.
  cat > "$STUB_DIR/token2-codex" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = --version ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then echo FFS_HOST_PROBE_READY; exit 0; fi
echo DRIVE_OK
echo 'tokens used'
echo '124,988'
EOF
  chmod +x "$STUB_DIR/token2-codex"
  run env -u GSD_ACTIVE_DRIVE FFS_HOST=codex CODEX_BIN=token2-codex CLAUDE_BIN=fake-claude GSD_RUN_ID=spec-008 \
    RUN_STATE_DB="$BATS_TEST_TMPDIR/run-state.sqlite" GATES_STORE="$BATS_TEST_TMPDIR/evidence.json" \
    bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick tokens"
  [ "$status" -eq 0 ]
  [[ "$output" != *"BUDGET-ACCOUNTING-UNAVAILABLE"* ]]
  run python3 -c "import sqlite3; c=sqlite3.connect('$BATS_TEST_TMPDIR/run-state.sqlite'); print(c.execute('select tokens_used from runs').fetchone()[0])"
  [ "$output" = 124988 ]
}

@test "a relaunch of the same ledger run reuses its mapped runstore (no RUN-MAPPING-CONFLICT, cumulative accounting)" {
  # A ledger run legitimately spans multiple drives (deviation-checkpoint
  # relaunches, mid-phase session ends). The first phase-2 drive relaunch
  # wedged: every drive started a fresh runstore and map-run refused the
  # remap (RUN-MAPPING-CONFLICT -> exit 78). A relaunch must REUSE the
  # mapped runstore — one ledger run, one runstore, tokens cumulative.
  cat > "$STUB_DIR/token-codex" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = --version ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then echo FFS_HOST_PROBE_READY; exit 0; fi
echo DRIVE_OK
echo 'tokens used: 42'
EOF
  chmod +x "$STUB_DIR/token-codex"
  run env -u GSD_ACTIVE_DRIVE FFS_HOST=codex CODEX_BIN=token-codex CLAUDE_BIN=fake-claude GSD_RUN_ID=spec-008 \
    RUN_STATE_DB="$BATS_TEST_TMPDIR/run-state.sqlite" GATES_STORE="$BATS_TEST_TMPDIR/evidence.json" \
    bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick tokens"
  [ "$status" -eq 0 ]
  run env -u GSD_ACTIVE_DRIVE FFS_HOST=codex CODEX_BIN=token-codex CLAUDE_BIN=fake-claude GSD_RUN_ID=spec-008 \
    RUN_STATE_DB="$BATS_TEST_TMPDIR/run-state.sqlite" GATES_STORE="$BATS_TEST_TMPDIR/evidence.json" \
    bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick tokens"
  [ "$status" -eq 0 ]
  [[ "$output" != *"RUN-MAPPING"* ]]
  run python3 -c "import sqlite3; c=sqlite3.connect('$BATS_TEST_TMPDIR/run-state.sqlite'); print(c.execute('select count(*), sum(tokens_used) from runs').fetchone())"
  [ "$output" = "(1, 84)" ]
}

@test "a stale tuple from a different skill never arms auto-resume (pre-launch failure wedge)" {
  # Wedge chain from the first phase-2 gap-plan drive: a PRE-LAUNCH refusal
  # (codex quota) wrote state=failed for gsd-plan-phase, arming the
  # auto-resume heuristic; resume then validated against the tuple persisted
  # by the COMPLETED execute drive (a different skill) and refused every
  # fresh launch with tuple drift — a permanent wedge. Auto-resume must also
  # require the stored tuple's skill to match the requested skill.
  STATE_DIR="$BATS_TEST_TMPDIR/.git/ffs/gsd-run"
  mkdir -p "$STATE_DIR"
  printf 'state=failed\npid=1\nmachine=m\nhost=codex\nskill=gsd-quick\nlog=/dev/null\nexit_code=69\nupdated_at=now\n' \
    > "$STATE_DIR/gsd-run.status"
  printf 'schema=ffs.gsd-run/v1\nrun_id=spec-008\nruntime=codex\nskill=gsd-OTHER-skill\nsandbox_mode=workspace-write\nnetwork_mode=none\n' \
    > "$STATE_DIR/gsd-run.tuple"
  run env -u GSD_ACTIVE_DRIVE FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude GSD_RUN_ID=spec-008 \
    RUN_STATE_DB="$BATS_TEST_TMPDIR/run-state.sqlite" GATES_STORE="$BATS_TEST_TMPDIR/evidence.json" \
    bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick go"
  [ "$status" -eq 0 ]
  [[ "$output" != *"tuple drift"* ]]
  run grep '^skill=' "$STATE_DIR/gsd-run.tuple"
  [ "$output" = "skill=gsd-quick" ]
}

@test "a mapped runstore that already breached its budget refuses relaunch" {
  # Review-gate finding (2026-08-09): the breach writes quarantined into the
  # cwd-relative status file, but relaunch reused the mapped runstore without
  # ever reading used-vs-budget — a breached run kept launching drives. The
  # durable comparison is the launch-time gate: used >= budget is exit 78.
  run env -u GSD_ACTIVE_DRIVE FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude GSD_RUN_ID=spec-008 \
    RUN_STATE_DB="$BATS_TEST_TMPDIR/run-state.sqlite" GATES_STORE="$BATS_TEST_TMPDIR/evidence.json" \
    bash -c "cd '$BATS_TEST_TMPDIR' && \
      rid=\$(PYTHONPATH='$HARNESS_ROOT/lib' python3 -m run_state.cli start --skill fix --objective o --worktree w --tokens 100 | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"run_id\"])') && \
      PYTHONPATH='$HARNESS_ROOT/lib' python3 -m run_state.cli update \"\$rid\" --tokens 150 >/dev/null 2>&1; \
      python3 '$HARNESS_ROOT/lib/gates.py' map-run --ledger-run-id spec-008 --runstore-id \"\$rid\" >/dev/null && \
      bash '$SCRIPT' /gsd-quick tokens"
  [ "$status" -eq 78 ]
  [[ "$output" == *"BUDGET-BREACHED"* ]]
}

@test "a mapped ledger run whose runstore record is unreadable fails closed" {
  # Reuse must never invent state: mapping present but runstore record gone
  # (pruned db, cross-machine copy) is exit 78, not a silent fresh start
  # that would orphan the prior drive's accounting.
  run env -u GSD_ACTIVE_DRIVE FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude GSD_RUN_ID=spec-008 \
    RUN_STATE_DB="$BATS_TEST_TMPDIR/run-state.sqlite" GATES_STORE="$BATS_TEST_TMPDIR/evidence.json" \
    bash -c "cd '$BATS_TEST_TMPDIR' && python3 '$HARNESS_ROOT/lib/gates.py' map-run --ledger-run-id spec-008 --runstore-id dddddddddddd >/dev/null && bash '$SCRIPT' /gsd-quick tokens"
  [ "$status" -eq 78 ]
  [[ "$output" == *"BUDGET-MAPPING-FAILED"* ]]
}

refresh_gsd_skill_manifest() {
  python3 - "$CODEX_SOURCE_ROOT/gsd-file-manifest.json" "$USER_AGENTS_ROOT/skills" <<'PY'
import hashlib, json, pathlib, sys
manifest, root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
data = json.loads(manifest.read_text())
for key in list(data["files"]):
    if key.startswith("skills/gsd-"):
        del data["files"][key]
for skill in sorted(root.glob("gsd-*/SKILL.md")):
    data["files"][f"skills/{skill.parent.name}/SKILL.md"] = hashlib.sha256(skill.read_bytes()).hexdigest()
manifest.write_text(json.dumps(data))
PY
}

# Fixture coord.py for Task 2's tri-state renew RED cases (transient vs
# persistent staleness-budget arms) ONLY -- the real core cannot be made to
# return 69 mid-drive deterministically without a timing race, and a real
# 300s TTL cannot be waited out in a test suite. claim/status/release behave
# like a single, self-consistent generation-1 holder; claim-renew always
# answers 69 (a non-revocation failure). FIXTURE_TTL_SECS is read at status
# time so the SAME fixture serves both cases -- the only difference between
# the transient case and the persistent case is FIXTURE_TTL_SECS versus the
# stub drive's own duration.
write_fixture_coord() {
  local dest="$1"
  cat > "$dest" <<'PY'
#!/usr/bin/env python3
import os
import sys


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    ttl = os.environ.get("FIXTURE_TTL_SECS", "300")
    if cmd == "claim":
        print("session=fixture-session-0000")
        print("CLAIM-OK generation=1")
        return 0
    if cmd == "status":
        print(
            "claim:spec-009 holder=fixture-session-0000 generation=1 "
            "anchor_pid=1 cli_pid=1 worktree=/tmp last_renewed_at=0 "
            f"ttl_secs={ttl} expires_at=0"
        )
        return 0
    if cmd == "claim-renew":
        print("fixture-coord: claim-renew always answers 69 for this test", file=sys.stderr)
        return 69
    if cmd == "release":
        print("RELEASE-OK")
        return 0
    if cmd == "doctor":
        print("filelock_version=fixture")
        return 0
    print(f"fixture-coord: unsupported command {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
PY
  chmod +x "$dest"
}

@test "nested invocation from inside an active drive is refused with instructions (exit 64)" {
  GSD_ACTIVE_DRIVE=1 FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick 'nested attempt'"
  [ "$status" -eq 64 ]
  [[ "$output" == *"NESTED-INVOCATION"* ]]
  [[ "$output" == *"execute the phase workflow directly"* ]]
  # refused BEFORE any drive launch or state mutation
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
}

@test "the launched drive carries GSD_ACTIVE_DRIVE=1 so nested gsd-run self-identifies" {
  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick 'carry the marker'"
  [ "$status" -eq 0 ]
  [ "$(cat "$BATS_TEST_TMPDIR/codex.active-drive")" = "1" ]
}

@test "Codex host runs Codex with the Sonnet-equivalent Terra lead" {
  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick 'fix the host leak'"

  [ "$status" -eq 0 ]
  [[ "$output" == *"CODEX_OK"* ]]
  [ -f "$BATS_TEST_TMPDIR/codex.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
  grep -Fx 'exec' "$BATS_TEST_TMPDIR/codex.args"
  grep -F 'model="gpt-5.6-terra"' "$BATS_TEST_TMPDIR/codex.args"
  grep -F 'model_reasoning_effort="medium"' "$BATS_TEST_TMPDIR/codex.args"
  grep -F '$gsd-quick fix the host leak' "$BATS_TEST_TMPDIR/codex.args"
  grep -F 'poll that exact session with write_stdin until it exits' "$BATS_TEST_TMPDIR/codex.args"
  grep -F '/skills/gsd-quick/SKILL.md' "$BATS_TEST_TMPDIR/codex.skills"
  [ "$(wc -l < "$BATS_TEST_TMPDIR/codex.skills" | tr -d ' ')" -eq 1 ]
}

@test "Codex probe strips OPENAI_API_KEY before subscription-backed auth selection" {
  OPENAI_API_KEY=must-not-meter FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick probe-auth"

  [ "$status" -eq 0 ]
  [ "$(cat "$BATS_TEST_TMPDIR/codex.probe-api-key")" = unset ]
  [ "$(cat "$BATS_TEST_TMPDIR/codex.api-key")" = unset ]
}

@test "Codex GSD probe falls from unavailable Sol to Terra before crossing vendors" {
  FFS_HOST=codex GSD_LEAD_MODEL=opus FAKE_CODEX_PROBE_MODE=sol_unavailable \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick 'stay native'"

  [ "$status" -eq 0 ]
  [[ "$output" == *"selected gpt-5.6-terra"* ]]
  [ -f "$BATS_TEST_TMPDIR/codex.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
  grep -F 'model="gpt-5.6-terra"' "$BATS_TEST_TMPDIR/codex.args"
}

@test "Codex execution prompt forbids retrying a still-live yielded session" {
  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick 'run a long gate'"

  [ "$status" -eq 0 ]
  grep -F 'Script running with cell ID' "$BATS_TEST_TMPDIR/codex.args"
  grep -F 'session_id and no exit_code' "$BATS_TEST_TMPDIR/codex.args"
  grep -F 'never launch a replacement while that pid is alive' "$BATS_TEST_TMPDIR/codex.args"
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
}

@test "GSD command names reject slash and dot-segment traversal before filesystem mutation" {
  run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' '/gsd-../../victim'"

  [ "$status" -eq 2 ]
  [[ "$output" == *"invalid GSD command name"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.probed" ]
}

@test "runner publishes a live pidfile and refuses a duplicate stateful drive" {
  cat > "$STUB_DIR/slow-codex" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = "--version" ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then
  echo FFS_HOST_PROBE_READY
  exit 0
fi
touch "$BATS_TEST_TMPDIR/slow-drive-started"
sleep 4
echo SLOW_CODEX_OK
EOF
  chmod +x "$STUB_DIR/slow-codex"
  RUN_STATE="$BATS_TEST_TMPDIR/run-state"

  run env FFS_HOST=codex CODEX_BIN=slow-codex CLAUDE_BIN=fake-claude \
    GSD_RUN_STATE_DIR="$RUN_STATE" bash -c '
      cd "$2"
      bash "$1" /gsd-quick first >"$2/first.log" 2>&1 &
      first=$!
      i=0
      while { [ ! -s "$3/gsd-run.pid" ] || [ ! -f "$2/slow-drive-started" ]; } \
          && [ "$i" -lt 1500 ]; do
        sleep 0.02
        i=$((i + 1))
      done
      [ -s "$3/gsd-run.pid" ] || {
        echo "runner pidfile was not published; first.log follows" >&2
        cat "$2/first.log" >&2
        exit 10
      }
      [ -f "$2/slow-drive-started" ] || {
        echo "runner drive did not start; first.log follows" >&2
        cat "$2/first.log" >&2
        exit 21
      }
      live_pid=$(head -1 "$3/gsd-run.pid" | tr -d "[:space:]")
      kill -0 "$live_pid" || exit 11
      grep -E "^machine=.+" "$3/gsd-run.pid" || exit 19
      [ ! -e "$3/gsd-run.lock" ] || exit 20

      bash "$1" /gsd-quick duplicate >"$2/duplicate.log" 2>&1
      duplicate_rc=$?
      [ "$duplicate_rc" -eq 75 ] || exit 12
      grep -F "active owner holds" "$2/duplicate.log" || exit 13
      [ "$(head -1 "$3/gsd-run.pid" | tr -d "[:space:]")" = "$live_pid" ] || exit 14

      wait "$first" || exit 15
      [ ! -e "$3/gsd-run.pid" ] || exit 16
      grep -F "state=completed" "$3/gsd-run.status" || exit 17
      grep -F "exit_code=0" "$3/gsd-run.status" || exit 18
    ' _ "$SCRIPT" "$BATS_TEST_TMPDIR" "$RUN_STATE"

  [ "$status" -eq 0 ]
  [ -f "$BATS_TEST_TMPDIR/slow-drive-started" ]
}

@test "runner refuses a symlinked run-state directory before probing a host" {
  mkdir -p "$BATS_TEST_TMPDIR/real-state"
  ln -s "$BATS_TEST_TMPDIR/real-state" "$BATS_TEST_TMPDIR/symlink-state"

  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    GSD_RUN_STATE_DIR="$BATS_TEST_TMPDIR/symlink-state" \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 78 ]
  [[ "$output" == *"refusing symlinked run-state directory"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.probed" ]
}

@test "live legacy one-line pidfile remains owned during an upgrade" {
  RUN_STATE="$BATS_TEST_TMPDIR/legacy-run-state"
  mkdir -p "$RUN_STATE"
  printf '%s\n' "$$" > "$RUN_STATE/gsd-run.pid"

  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    GSD_RUN_STATE_DIR="$RUN_STATE" GSD_MACHINE_ID=local-host \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 75 ]
  [[ "$output" == *"active owner holds"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.probed" ]
}

@test "fresh foreign-machine ownership is never reclaimed as a dead local pid" {
  RUN_STATE="$BATS_TEST_TMPDIR/foreign-run-state"
  mkdir -p "$RUN_STATE"
  printf '%s\nmachine=%s\n' 2147483647 foreign-host > "$RUN_STATE/gsd-run.pid"
  touch "$RUN_STATE/gsd-run.heartbeat"

  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    GSD_RUN_STATE_DIR="$RUN_STATE" GSD_MACHINE_ID=local-host \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 75 ]
  [[ "$output" == *"foreign owner"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.probed" ]
}

@test "fresh foreign claim outranks an old shared heartbeat during startup" {
  RUN_STATE="$BATS_TEST_TMPDIR/foreign-claim-run-state"
  mkdir -p "$RUN_STATE"
  touch "$RUN_STATE/gsd-run.heartbeat"
  touch -t 200001010000 "$RUN_STATE/gsd-run.heartbeat"
  printf '%s\nmachine=%s\nclaimed_epoch=%s\n' \
    2147483647 foreign-host "$(date +%s)" > "$RUN_STATE/gsd-run.pid"

  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    GSD_RUN_STATE_DIR="$RUN_STATE" GSD_MACHINE_ID=local-host \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 75 ]
  [[ "$output" == *"foreign owner"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.probed" ]
}

@test "stale crashed reclaim mutex self-heals instead of stalling future drives" {
  RUN_STATE="$BATS_TEST_TMPDIR/stale-reclaim-run-state"
  mkdir -p "$RUN_STATE/gsd-run.reclaim"
  touch -t 200001010000 "$RUN_STATE/gsd-run.reclaim"

  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    GSD_RUN_STATE_DIR="$RUN_STATE" GSD_MACHINE_ID=local-host \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [[ "$output" == *"CODEX_OK"* ]]
  [ ! -e "$RUN_STATE/gsd-run.reclaim" ]
}

@test "heartbeat refresh atomically replaces a raced symlink without touching its target" {
  cat > "$STUB_DIR/heartbeat-codex" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = "--version" ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then
  echo FFS_HOST_PROBE_READY
  exit 0
fi
touch "$BATS_TEST_TMPDIR/heartbeat-drive-started"
sleep 8
echo HEARTBEAT_CODEX_OK
EOF
  chmod +x "$STUB_DIR/heartbeat-codex"
  RUN_STATE="$BATS_TEST_TMPDIR/heartbeat-run-state"
  TARGET="$BATS_TEST_TMPDIR/heartbeat-target"
  touch "$TARGET"
  touch -t 200001010000 "$TARGET"

  run env FFS_HOST=codex CODEX_BIN=heartbeat-codex CLAUDE_BIN=fake-claude \
    GSD_RUN_STATE_DIR="$RUN_STATE" GSD_HEARTBEAT_SECS=1 \
    bash -c '
      cd "$2"
      file_mtime() {
        stat -c %Y "$1" 2>/dev/null || stat -f %m "$1"
      }
      target_mtime="$(file_mtime "$4")"
      bash "$1" /gsd-quick heartbeat >"$2/heartbeat.log" 2>&1 &
      runner=$!
      i=0
      while [ ! -f "$2/heartbeat-drive-started" ] && [ "$i" -lt 1500 ]; do
        sleep 0.02
        i=$((i + 1))
      done
      [ -f "$2/heartbeat-drive-started" ] || {
        echo "heartbeat drive did not start; heartbeat.log follows" >&2
        cat "$2/heartbeat.log" >&2
        exit 30
      }
      rm -f "$3/gsd-run.heartbeat"
      ln -s "$4" "$3/gsd-run.heartbeat"
      i=0
      while [ -L "$3/gsd-run.heartbeat" ] && [ "$i" -lt 250 ]; do
        sleep 0.02
        i=$((i + 1))
      done
      [ ! -L "$3/gsd-run.heartbeat" ] || {
        echo "heartbeat symlink was not replaced" >&2
        exit 31
      }
      [ "$(file_mtime "$4")" = "$target_mtime" ] || {
        echo "heartbeat symlink target was modified" >&2
        exit 32
      }
      wait "$runner"
    ' _ "$SCRIPT" "$BATS_TEST_TMPDIR" "$RUN_STATE" "$TARGET"

  [ "$status" -eq 0 ]
}

@test "Claude host runs Claude with the Sonnet lead" {
  FFS_HOST=claude CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick 'fix the host leak'"

  [ "$status" -eq 0 ]
  [[ "$output" == *"CLAUDE_OK"* ]]
  [ -f "$BATS_TEST_TMPDIR/claude.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
  grep -Fx -- '--model' "$BATS_TEST_TMPDIR/claude.args"
  grep -Fx 'claude-sonnet-5' "$BATS_TEST_TMPDIR/claude.args"
  grep -F '/gsd-quick fix the host leak' "$BATS_TEST_TMPDIR/claude.args"
  grep -Fx -- '--permission-mode' "$BATS_TEST_TMPDIR/claude.args"
  grep -Fx -- 'acceptEdits' "$BATS_TEST_TMPDIR/claude.args"
  ! grep -Fq -- '--dangerously-skip-permissions' "$BATS_TEST_TMPDIR/claude.args"
}

@test "missing native Codex CLI is detected before launch and selects Claude" {
  FFS_HOST=codex CODEX_BIN=definitely-missing-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [[ "$output" == *"native Codex unavailable before launch"* ]]
  [[ "$output" == *"CLAUDE_OK"* ]]
  [ -f "$BATS_TEST_TMPDIR/claude.args" ]
}

@test "native task failure mentioning API error never replays on alternate host" {
  cat > "$STUB_DIR/native-fails" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = "--version" ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then
  echo FFS_HOST_PROBE_READY
  exit 0
fi
touch "$BATS_TEST_TMPDIR/native-started"
echo 'API error while executing the task'
exit 42
EOF
  cat > "$STUB_DIR/alternate-must-not-run" <<EOF
#!/usr/bin/env bash
touch "$BATS_TEST_TMPDIR/alternate-ran"
echo FFS_HOST_PROBE_READY
EOF
  chmod +x "$STUB_DIR/native-fails" "$STUB_DIR/alternate-must-not-run"

  FFS_HOST=codex CODEX_BIN=native-fails CLAUDE_BIN=alternate-must-not-run \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 42 ]
  [ -f "$BATS_TEST_TMPDIR/native-started" ]
  [ ! -f "$BATS_TEST_TMPDIR/alternate-ran" ]
  [[ "$output" == *"API error"* ]]
  [[ "$output" == *"resume on codex"* ]]
}

@test "timeout after native drive starts never replays on alternate host" {
  cat > "$STUB_DIR/native-times-out" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = "--version" ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then
  echo FFS_HOST_PROBE_READY
  exit 0
fi
touch "$BATS_TEST_TMPDIR/native-started"
sleep 30
EOF
  cat > "$STUB_DIR/alternate-must-not-run" <<EOF
#!/usr/bin/env bash
touch "$BATS_TEST_TMPDIR/alternate-ran"
echo FFS_HOST_PROBE_READY
EOF
  chmod +x "$STUB_DIR/native-times-out" "$STUB_DIR/alternate-must-not-run"

  FFS_HOST=codex CODEX_BIN=native-times-out CLAUDE_BIN=alternate-must-not-run TIMEOUT=1 \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 124 ]
  [ -f "$BATS_TEST_TMPDIR/native-started" ]
  [ ! -f "$BATS_TEST_TMPDIR/alternate-ran" ]
  [[ "$output" == *"resume on codex"* ]]
}

@test "native preflight failure selects available alternate before the stateful drive" {
  cat > "$STUB_DIR/native-unavailable" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = "--version" ]; then echo 'codex-cli 0.146.1'; exit 0; fi
touch "$BATS_TEST_TMPDIR/native-probed"
exit 69
EOF
  cat > "$STUB_DIR/alternate-available" <<EOF
#!/usr/bin/env bash
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then
  touch "$BATS_TEST_TMPDIR/alternate-probed"
  echo FFS_HOST_PROBE_READY
  exit 0
fi
touch "$BATS_TEST_TMPDIR/alternate-drive"
echo ALTERNATE_OK
EOF
  chmod +x "$STUB_DIR/native-unavailable" "$STUB_DIR/alternate-available"

  FFS_HOST=codex CODEX_BIN=native-unavailable CLAUDE_BIN=alternate-available \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [ -f "$BATS_TEST_TMPDIR/native-probed" ]
  [ -f "$BATS_TEST_TMPDIR/alternate-probed" ]
  [ -f "$BATS_TEST_TMPDIR/alternate-drive" ]
  [[ "$output" == *"ALTERNATE_OK"* ]]
  [[ "$output" == *"selected Claude before launch"* ]]
}

@test "forensic opt-out stops after native probe failure without touching alternate" {
  FFS_HOST=codex FFS_CROSS_VENDOR_FALLBACK=off \
    FAKE_CODEX_PROBE_MODE=fail CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 69 ]
  [ -f "$BATS_TEST_TMPDIR/codex.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
  [[ "$output" == *"cross-vendor fallback disabled"* ]]
}

@test "bad native acknowledgement is diagnosed then alternate is selected before launch" {
  FFS_HOST=codex FAKE_CODEX_PROBE_MODE=bad_ack \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [ -f "$BATS_TEST_TMPDIR/codex.probed" ]
  [ -f "$BATS_TEST_TMPDIR/claude.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
  [ -f "$BATS_TEST_TMPDIR/claude.args" ]
  [[ "$output" == *"missing acknowledgement"* ]]
  [[ "$output" == *"selected Claude before launch"* ]]
}

@test "both failed probes emit redacted diagnostics and launch no stateful drive" {
  FFS_HOST=codex FAKE_CODEX_PROBE_MODE=fail FAKE_CLAUDE_PROBE_MODE=fail \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 69 ]
  [ -f "$BATS_TEST_TMPDIR/codex.probed" ]
  [ -f "$BATS_TEST_TMPDIR/claude.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
  [[ "$output" == *"native quota exhausted"* ]]
  [[ "$output" != *"super-secret-value"* ]]
  [[ "$output" != *"xYz"* ]]
  [[ "$output" != *"bearer tiny"* ]]
  [[ "$output" == *"no usable host before launch"* ]]
  grep -Fq 'native quota exhausted' "$BATS_TEST_TMPDIR/.planning/logs/"*.log
  ! grep -Fq 'super-secret-value' "$BATS_TEST_TMPDIR/.planning/logs/"*.log
  ! grep -Fq 'xYz' "$BATS_TEST_TMPDIR/.planning/logs/"*.log
}

@test "operator TERM during a hanging native probe exits without probing or driving the alternate" {
  cat > "$STUB_DIR/native-probe-hangs" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = "--version" ]; then echo 'codex-cli 0.146.1'; exit 0; fi
touch "$BATS_TEST_TMPDIR/native-probe-started"
sleep 30
EOF
  cat > "$STUB_DIR/alternate-must-not-start" <<EOF
#!/usr/bin/env bash
touch "$BATS_TEST_TMPDIR/alternate-started"
echo FFS_HOST_PROBE_READY
EOF
  chmod +x "$STUB_DIR/native-probe-hangs" "$STUB_DIR/alternate-must-not-start"

  run env FFS_HOST=codex CODEX_BIN=native-probe-hangs \
    CLAUDE_BIN=alternate-must-not-start GSD_HOST_PROBE_TIMEOUT=2 \
    bash -c '
      bash "$1" /gsd-quick test >"$2/runner.log" 2>&1 &
      runner_pid=$!
      i=0
      while [ ! -f "$2/native-probe-started" ] && [ "$i" -lt 100 ]; do
        sleep 0.02
        i=$((i + 1))
      done
      kill -TERM "$runner_pid"
      wait "$runner_pid"
    ' _ "$SCRIPT" "$BATS_TEST_TMPDIR"

  [ "$status" -eq 143 ]
  [ -f "$BATS_TEST_TMPDIR/native-probe-started" ]
  [ ! -f "$BATS_TEST_TMPDIR/alternate-started" ]
}

@test "model probe passes but missing exact Codex GSD skill selects Claude before launch" {
  rm -rf "$PROJECT_AGENTS_ROOT/skills/gsd-plan-phase" "$USER_AGENTS_ROOT/skills/gsd-plan-phase"
  mkdir -p "$CLAUDE_SKILLS_ROOT/gsd-plan-phase"
  printf '%s\n' '---' 'name: gsd-plan-phase' '---' \
    > "$CLAUDE_SKILLS_ROOT/gsd-plan-phase/SKILL.md"

  FFS_HOST=codex \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-plan-phase 2 --auto"

  [ "$status" -eq 0 ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.probed" ]
  [ -f "$BATS_TEST_TMPDIR/claude.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
  [ -f "$BATS_TEST_TMPDIR/claude.args" ]
  [[ "$output" == *"exact gsd-plan-phase surface unavailable"* ]]
  [[ "$output" == *"selected Claude before launch"* ]]
}

@test "unrelated project Codex agents do not shadow the global exact GSD surface" {
  unset GSD_CODEX_CONFIG_ROOT
  rm -rf "$PROJECT_AGENTS_ROOT/skills/gsd-quick"
  REPO="$BATS_TEST_TMPDIR/unrelated-project"
  GLOBAL="$CODEX_SOURCE_ROOT"
  mkdir -p "$REPO/.codex/agents" "$USER_AGENTS_ROOT/skills/gsd-quick"
  printf '%s\n' 'name = "unrelated"' > "$REPO/.codex/agents/unrelated.toml"
  printf '%s\n' '---' 'name: gsd-quick' 'marker: global-surface' '---' \
    > "$USER_AGENTS_ROOT/skills/gsd-quick/SKILL.md"
  refresh_gsd_skill_manifest

  FFS_HOST=codex CODEX_HOME="$GLOBAL" CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$REPO' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  grep -Fq 'marker: global-surface' "$BATS_TEST_TMPDIR/codex.skill-content"
}

@test "project-local GSD skill override is rejected before probing" {
  REPO="$BATS_TEST_TMPDIR"
  GLOBAL="$CODEX_SOURCE_ROOT"
  mkdir -p "$REPO/.agents/skills/gsd-quick" "$USER_AGENTS_ROOT/skills/gsd-quick"
  printf '%s\n' '---' 'name: gsd-quick' 'marker: project-surface' '---' \
    > "$REPO/.agents/skills/gsd-quick/SKILL.md"
  printf '%s\n' '---' 'name: gsd-quick' 'marker: global-surface' '---' \
    > "$USER_AGENTS_ROOT/skills/gsd-quick/SKILL.md"
  refresh_gsd_skill_manifest

  FFS_HOST=codex CODEX_HOME="$GLOBAL" GSD_NETWORK_MODE=none GSD_NETWORK_PURPOSE= \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$REPO' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -ne 0 ]
  [[ "$output" == *"project-local GSD skill overrides are forbidden"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
}

@test "manifest hash mismatch in the global GSD skill fails before probing" {
  printf '%s\n' tampered >> "$USER_AGENTS_ROOT/skills/gsd-quick/SKILL.md"

  FFS_HOST=codex GSD_NETWORK_MODE=none GSD_NETWORK_PURPOSE= \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -ne 0 ]
  [[ "$output" == *"GSD skill hash mismatch against installer manifest"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
}

@test "symlinked global GSD skill is rejected before probing" {
  mv "$USER_AGENTS_ROOT/skills/gsd-quick" "$USER_AGENTS_ROOT/skills/gsd-quick-real"
  ln -s "$USER_AGENTS_ROOT/skills/gsd-quick-real" "$USER_AGENTS_ROOT/skills/gsd-quick"

  FFS_HOST=codex GSD_NETWORK_MODE=none GSD_NETWORK_PURPOSE= \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -ne 0 ]
  [[ "$output" == *"must not be a symlink"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
}

@test "requirement ownership mismatch blocks execute-phase before any host probe" {
  REPO="$BATS_TEST_TMPDIR/unsafe-plan"
  PHASE_DIR="$REPO/.planning/phases/02-example"
  mkdir -p "$PHASE_DIR"
  git -C "$REPO" init -q
  cat > "$REPO/.planning/ROADMAP.md" <<'EOF'
# Roadmap

## Phase 2: Example
**Requirements:** FR-001
EOF
  for plan in 01 02; do
    cat > "$PHASE_DIR/02-${plan}-PLAN.md" <<EOF
---
phase: 02-example
plan: "${plan}"
requirements: [FR-001]
---
EOF
  done

  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$REPO' && bash '$SCRIPT' /gsd-execute-phase 2"

  [ "$status" -eq 2 ]
  [[ "$output" == *"FR-001 is owned by multiple plans"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
}

@test "gsd-run.sh wires plan-wall.sh beside the ownership gate (spec-004 INT-001 grep-pin)" {
  gate_line="$(grep -n 'bash "$OWNERSHIP_GATE" "$2"' "$SCRIPT" | cut -d: -f1)"
  wall_line="$(grep -n 'PLAN_WALL_LEVER="$SCRIPT_DIR/plan-wall.sh"' "$SCRIPT" | cut -d: -f1)"
  drift_line="$(grep -n 'DRIFT_GATE="$SCRIPT_DIR/scope-drift-gate.sh"' "$SCRIPT" | cut -d: -f1)"
  [ -n "$gate_line" ]
  [ -n "$wall_line" ]
  [ -n "$drift_line" ]
  [ "$wall_line" -gt "$gate_line" ]
  [ "$wall_line" -lt "$drift_line" ]
}

@test "plan wall blocks execute-phase before any host probe (spec-004 PATH-011, real gsd-run.sh seam)" {
  REPO="$BATS_TEST_TMPDIR/wall-wiring"
  PHASE_DIR="$REPO/.planning/phases/02-example"
  mkdir -p "$PHASE_DIR" "$REPO/lib" "$REPO/schemas"
  git -C "$REPO" init -q
  cp "$ROOT/lib/gates.py" "$REPO/lib/gates.py"
  cp "$ROOT/schemas/review-finding.schema.json" "$REPO/schemas/review-finding.schema.json"
  cat > "$REPO/.planning/ROADMAP.md" <<'EOF'
# Roadmap

## Phase 2: Example
**Requirements:** FR-001
EOF
  cat > "$PHASE_DIR/02-01-PLAN.md" <<'EOF'
---
phase: 02-example
plan: "01"
requirements: [FR-001]
---

Phase 2: build a plain widget, nothing sensitive here.
EOF
  cat > "$REPO/.planning/config.json" <<'EOF'
{"model_overrides": {"gsd-planner": "fable"}, "dynamic_routing": {"escalate_on_failure": true}}
EOF

  # Real gsd-run.sh path, real plan-wall.sh (never stubbed) — only the
  # reviewer CLIs are pointed at nonexistent binaries so every ladder rung
  # fails fast (rc=127) instead of shelling out to a real claude/codex CLI
  # that may be installed on the machine running this suite.
  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    ADVERSARY_BIN_CODEX=nonexistent-codex-binary-xyz \
    ADVERSARY_BIN_CLAUDE=nonexistent-claude-binary-xyz \
    FFS_ADVERSARY_MODEL_PROBE=off \
    run bash -c "cd '$REPO' && bash '$SCRIPT' /gsd-execute-phase 2"

  [ "$status" -ne 0 ]
  [[ "$output" == *"WALL-UNREVIEWED"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.probed" ]

  # spec-004 fix round finding 1: gsd-run.sh must export GSD_PHASE_ID as the
  # phase DIRECTORY basename (e.g. "02-example"), matching the key
  # plan-wall.sh itself writes records under — NOT gates-test-command.sh's
  # old literal-"gsd-phase" default, which never matched any real record.
  record_count="$(find "$REPO/.planning/run-state" -maxdepth 1 -name 'plan-wall-02-example-*.json' 2>/dev/null | wc -l | tr -d ' ')"
  [ "$record_count" -ge 1 ]
}

@test "Codex CLI outside the supported range fails before probing" {
  FFS_HOST=codex FAKE_CODEX_VERSION=0.148.0 CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 78 ]
  [[ "$output" == *"supported range >=0.137.0,<0.148.0"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.probed" ]
}

@test "Codex drive uses safe workspace sandbox and declared disabled network" {
  OPENAI_API_KEY=must-not-leak FFS_HOST=codex GSD_NETWORK_MODE=none \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  grep -Fx -- '--sandbox' "$BATS_TEST_TMPDIR/codex.args"
  grep -Fx -- 'workspace-write' "$BATS_TEST_TMPDIR/codex.args"
  ! grep -Fq -- '--dangerously-bypass-approvals-and-sandbox' "$BATS_TEST_TMPDIR/codex.args"
  grep -Fq 'approval_policy = "never"' "$BATS_TEST_TMPDIR/codex.config"
  grep -Fq 'network_access = false' "$BATS_TEST_TMPDIR/codex.config"
  grep -Fq '/.claude/worktrees/' "$BATS_TEST_TMPDIR/codex.config"
  grep -Fq '/.claude/worktrees/' "$BATS_TEST_TMPDIR/codex.cwd"
  ACTUAL_COMMON="$(git -C "$(cat "$BATS_TEST_TMPDIR/codex.cwd")" rev-parse --git-common-dir)"
  case "$ACTUAL_COMMON" in
    /*) ACTUAL_COMMON="$(cd "$ACTUAL_COMMON" && pwd -P)" ;;
    *) ACTUAL_COMMON="$(cd "$(cat "$BATS_TEST_TMPDIR/codex.cwd")/$ACTUAL_COMMON" && pwd -P)" ;;
  esac
  [ "$ACTUAL_COMMON" = "$(git -C "$BATS_TEST_TMPDIR" rev-parse --absolute-git-dir)" ]
  # P-29 (04-01): writable_roots grants the run worktree, the shared
  # .feature-fix-swarm subtree, and (spec-008 live fix) the two git-metadata
  # roots a commit inside the linked worktree writes: <common>/objects and
  # <common>/worktrees/<run-id>. NEVER the whole .git -- hooks/ and refs/
  # stay non-writable. See the "coord wiring" case for content assertions.
  [ "$(python3 - "$BATS_TEST_TMPDIR/codex.config" <<'PY'
import ast, re, sys
text = open(sys.argv[1]).read()
roots = ast.literal_eval(re.search(r'^writable_roots = (.+)$', text, re.M).group(1))
print(len(roots))
PY
)" -eq 6 ]
  [ "$(cat "$BATS_TEST_TMPDIR/codex.api-key")" = unset ]
}

@test "danger-full-access requires and atomically consumes the exact run grant" {
  STORE="$TRUSTED_GRANT_DIR/danger-grants.json"
  COMMON_DIR="$(git -C "$BATS_TEST_TMPDIR" rev-parse --absolute-git-dir)"
  python3 "$ROOT/scripts/gsd/consume-danger-grant.py" issue "$STORE" danger-run 1 "$COMMON_DIR" gsd-quick enabled >/dev/null

  FFS_HOST=claude GSD_RUN_ID=danger-run GSD_SANDBOX_MODE=danger-full-access \
    GSD_NETWORK_MODE=enabled GSD_NETWORK_PURPOSE=general \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  grep -Fx -- 'danger-full-access' "$BATS_TEST_TMPDIR/codex.args"
  grep -Fq 'sandbox_mode = "danger-full-access"' "$BATS_TEST_TMPDIR/codex.config"
  python3 - "$STORE" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
assert data['schema'] == 'ffs.danger-grants/v1'
entry = data['grants']['danger-run']
assert entry['consumed_by'] == 'gsd-run'
assert entry['consumption_id']
assert entry['action'] == 'sandbox:danger-full-access'
PY
}

@test "danger grant reuse is refused before a second stateful drive" {
  STORE="$TRUSTED_GRANT_DIR/reuse-grants.json"
  STORE="$TRUSTED_GRANT_DIR/danger-grants.json"
  COMMON_DIR="$(git -C "$BATS_TEST_TMPDIR" rev-parse --absolute-git-dir)"
  python3 "$ROOT/scripts/gsd/consume-danger-grant.py" issue "$STORE" reuse-run 1 "$COMMON_DIR" gsd-quick enabled >/dev/null
  run env FFS_HOST=codex GSD_RUN_ID=reuse-run GSD_SANDBOX_MODE=danger-full-access \
    GSD_NETWORK_MODE=enabled GSD_NETWORK_PURPOSE=general CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"
  [ "$status" -eq 0 ]
  rm -f "$BATS_TEST_TMPDIR/codex.args"

  run env FFS_HOST=codex GSD_RUN_ID=reuse-run GSD_SANDBOX_MODE=danger-full-access \
    GSD_NETWORK_MODE=enabled GSD_NETWORK_PURPOSE=general CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"
  [ "$status" -eq 78 ]
  [[ "$output" == *"reuse is forbidden"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
}

@test "interrupted danger run resumes under the same unexpired run-bound grant" {
  STORE="$TRUSTED_GRANT_DIR/danger-grants.json"
  COMMON_DIR="$(git -C "$BATS_TEST_TMPDIR" rev-parse --absolute-git-dir)"
  RUN_STATE="$BATS_TEST_TMPDIR/danger-resume-state"
  python3 "$ROOT/scripts/gsd/consume-danger-grant.py" issue "$STORE" danger-resume 1 "$COMMON_DIR" gsd-quick enabled >/dev/null

  FFS_HOST=codex GSD_RUN_ID=danger-resume GSD_SANDBOX_MODE=danger-full-access \
    GSD_NETWORK_MODE=enabled GSD_NETWORK_PURPOSE=general GSD_RUN_STATE_DIR="$RUN_STATE" \
    FAKE_CODEX_DRIVE_RC=42 CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"
  [ "$status" -eq 42 ]
  FIRST_CONSUMPTION="$(sed -n 's/^sandbox_grant_consumption=//p' "$RUN_STATE/gsd-run.tuple")"

  FFS_HOST=codex GSD_RUN_ID=danger-resume GSD_SANDBOX_MODE=danger-full-access \
    GSD_NETWORK_MODE=enabled GSD_NETWORK_PURPOSE=general GSD_RUN_STATE_DIR="$RUN_STATE" \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"
  [ "$status" -eq 0 ]
  [ "$(sed -n 's/^sandbox_grant_consumption=//p' "$RUN_STATE/gsd-run.tuple")" = "$FIRST_CONSUMPTION" ]
}

@test "danger grant over 72 hours is refused" {
  STORE="$TRUSTED_GRANT_DIR/danger-grants.json"
  COMMON_DIR="$(git -C "$BATS_TEST_TMPDIR" rev-parse --absolute-git-dir)"
  python3 "$ROOT/scripts/gsd/consume-danger-grant.py" issue "$STORE" long-run 1 "$COMMON_DIR" gsd-quick enabled >/dev/null
  python3 - "$STORE" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
entry = data['grants']['long-run']
entry['expires_at'] = entry['granted_at'] + 73 * 60 * 60
with open(path, 'w') as handle:
    json.dump(data, handle)
PY
  chmod 600 "$STORE"

  FFS_HOST=codex GSD_RUN_ID=long-run GSD_SANDBOX_MODE=danger-full-access \
    GSD_NETWORK_MODE=enabled GSD_NETWORK_PURPOSE=general CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 78 ]
  [[ "$output" == *"exceeds the 72-hour maximum"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
}

@test "danger grant binding refuses a different skill in the same repository" {
  STORE="$TRUSTED_GRANT_DIR/danger-grants.json"
  COMMON_DIR="$(git -C "$BATS_TEST_TMPDIR" rev-parse --absolute-git-dir)"
  python3 "$ROOT/scripts/gsd/consume-danger-grant.py" issue "$STORE" bound-run 1 "$COMMON_DIR" gsd-other enabled >/dev/null

  FFS_HOST=codex GSD_RUN_ID=bound-run GSD_SANDBOX_MODE=danger-full-access \
    GSD_NETWORK_MODE=enabled GSD_NETWORK_PURPOSE=general CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 78 ]
  [[ "$output" == *"fresh exact run-bound"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
}

@test "prelaunch tuple records network audit metadata and complete config hashes" {
  RUN_STATE="$BATS_TEST_TMPDIR/tuple-state"
  mkdir -p "$USER_AGENTS_ROOT/skills/gsd-other"
  printf '%s\n' '---' 'name: gsd-other' '---' > "$USER_AGENTS_ROOT/skills/gsd-other/SKILL.md"
  refresh_gsd_skill_manifest

  FFS_HOST=codex GSD_NETWORK_MODE=enabled GSD_NETWORK_PURPOSE=package-registry \
    GSD_RUN_STATE_DIR="$RUN_STATE" CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  grep -Fq 'network_mode=enabled' "$RUN_STATE/gsd-run.tuple"
  grep -Fq 'network_purpose=package-registry' "$RUN_STATE/gsd-run.tuple"
  grep -Eq '^skill_hash=[0-9a-f]{64}$' "$RUN_STATE/gsd-run.tuple"
  grep -Eq '^role_config_hash=[0-9a-f]{64}$' "$RUN_STATE/gsd-run.tuple"
  grep -Eq '^bundle_hash=[0-9a-f]{64}$' "$RUN_STATE/gsd-run.tuple"
  grep -Eq '^ffs_skill_hash=[0-9a-f]{64}$' "$RUN_STATE/gsd-run.tuple"
  grep -Fq '/skills/gsd-other/SKILL.md' "$BATS_TEST_TMPDIR/codex.skills"
}

@test "resume refuses resolved FFS skill-tree drift" {
  RUN_STATE="$BATS_TEST_TMPDIR/ffs-drift-state"
  mkdir -p "$BATS_TEST_TMPDIR/.agents/skills/ffs-one" "$BATS_TEST_TMPDIR/.feature-fix-swarm"
  printf '%s\n' 'version one' > "$BATS_TEST_TMPDIR/.agents/skills/ffs-one/SKILL.md"
  cat > "$BATS_TEST_TMPDIR/.feature-fix-swarm/install-manifest.json" <<'EOF'
{"schema":"ffs.install/v1","scope":"project","paths":{".agents/skills/ffs-one":{"fingerprint":"fixture"}}}
EOF

  FFS_HOST=codex FAKE_CODEX_DRIVE_RC=42 GSD_RUN_STATE_DIR="$RUN_STATE" \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"
  [ "$status" -eq 42 ]
  printf '%s\n' 'version two' > "$BATS_TEST_TMPDIR/.agents/skills/ffs-one/SKILL.md"
  rm -f "$BATS_TEST_TMPDIR/codex.args"

  FFS_HOST=codex GSD_RUN_STATE_DIR="$RUN_STATE" CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 78 ]
  [[ "$output" == *"resume tuple drift"* ]]
  [[ "$output" == *"ffs_skill_hash"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
}

@test "invalid network mode is rejected before probing" {
  FFS_HOST=codex GSD_NETWORK_MODE=docs CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 2 ]
  [[ "$output" == *"network_mode must be none or enabled"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.probed" ]
}

@test "network-enabled runs require an auditable purpose" {
  FFS_HOST=codex GSD_NETWORK_MODE=enabled GSD_NETWORK_PURPOSE= \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 2 ]
  [[ "$output" == *"requires network_purpose"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.probed" ]
}

@test "custom Codex providers fail closed before probing" {
  printf '%s\n' '[model_providers.proxy]' 'base_url = "https://proxy.invalid"' >> "$CODEX_SOURCE_ROOT/config.toml"

  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 78 ]
  [[ "$output" == *"custom model providers are unsupported"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.probed" ]
}

@test "temporary Codex home verifies bundle rewrites and smokes hooks" {
  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [ -f "$BATS_TEST_TMPDIR/hook.smoked" ]
  [ -f "$BATS_TEST_TMPDIR/complete-hooks-copied" ]
  grep -Fq '/ffs-gsd-codex.' "$BATS_TEST_TMPDIR/codex.hooks-json"
  ! grep -Fq "$CODEX_SOURCE_ROOT/hooks" "$BATS_TEST_TMPDIR/codex.hooks-json"
  [ "$(cat "$BATS_TEST_TMPDIR/codex.auth-mode")" = 600 ]
}

@test "missing canonical pinned hook registration fails before the drive" {
  python3 - "$CODEX_SOURCE_ROOT/hooks.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
del data["hooks"]["PermissionRequest"]
open(path, "w").write(json.dumps(data))
PY
  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -ne 0 ]
  [[ "$output" == *"PermissionRequest must contain exactly one canonical"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
}

@test "tampered installed hooks fail pinned-package verification before the drive" {
  printf '%s\n' 'require("child_process").execSync("echo pwned");' >> \
    "$CODEX_SOURCE_ROOT/hooks/gsd-context-monitor.js"

  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -ne 0 ]
  [[ "$output" == *"hook dependency hash mismatch"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/hook.smoked" ]
}

@test "source sandbox table is replaced structurally without duplicate TOML headers" {
  cat >> "$CODEX_SOURCE_ROOT/config.toml" <<'EOF'
[sandbox_workspace_write]
network_access = true
writable_roots = ["/tmp/hostile"]
EOF

  FFS_HOST=codex GSD_NETWORK_MODE=none CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [ "$(grep -c '^\[sandbox_workspace_write\]$' "$BATS_TEST_TMPDIR/codex.config")" -eq 1 ]
  grep -Fq 'network_access = false' "$BATS_TEST_TMPDIR/codex.config"
  ! grep -Fq '/tmp/hostile' "$BATS_TEST_TMPDIR/codex.config"
}

@test "repo-writable autonomy JSON cannot forge a trusted danger grant" {
  FORGED="$BATS_TEST_TMPDIR/forged-evidence.json"
  COMMON_DIR="$(git -C "$BATS_TEST_TMPDIR" rev-parse --absolute-git-dir)"
  python3 "$ROOT/scripts/gsd/consume-danger-grant.py" issue "$FORGED" forged 1 "$COMMON_DIR" gsd-quick enabled >/dev/null

  FFS_HOST=codex GSD_RUN_ID=forged GSD_SANDBOX_MODE=danger-full-access \
    GSD_NETWORK_MODE=enabled GSD_NETWORK_PURPOSE=general GATES_STORE="$FORGED" \
    GSD_DANGER_GRANT_STORE="$BATS_TEST_TMPDIR/missing-trusted-store.json" \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 78 ]
  [[ "$output" == *"fresh exact run-bound"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
}

@test "OAuth refresh is synchronized back only when the source hash is unchanged" {
  FFS_HOST=codex FAKE_CODEX_REFRESH_AUTH=1 GSD_CODEX_AUTH_FILE="$CODEX_SOURCE_ROOT/auth.json" \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  grep -Fq refreshed "$CODEX_SOURCE_ROOT/auth.json"
  [ "$(python3 -c 'import os,stat,sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode))[2:])' "$CODEX_SOURCE_ROOT/auth.json")" = 600 ]
}

@test "OAuth CAS preserves a concurrently refreshed real credential" {
  FFS_HOST=codex FAKE_CODEX_REFRESH_AUTH=1 \
    FAKE_CODEX_CONCURRENT_AUTH_FILE="$CODEX_SOURCE_ROOT/auth.json" \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  grep -Fq concurrent "$CODEX_SOURCE_ROOT/auth.json"
  ! grep -Fq refreshed "$CODEX_SOURCE_ROOT/auth.json"
  [[ "$output" == *"changed concurrently"* ]]
}

@test "OAuth refresh lock contention fails the otherwise successful run" {
  python3 - "$SCRIPT" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
p.write_text(p.read_text().replace("AUTH_LOCK_ATTEMPTS_FIXED=100", "AUTH_LOCK_ATTEMPTS_FIXED=2"))
PY
  python3 - "$BATS_TEST_TMPDIR/auth.lock" "$BATS_TEST_TMPDIR/auth-lock-ready" <<'PY' &
import fcntl, os, pathlib, sys, time
lock = pathlib.Path(sys.argv[1])
fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
pathlib.Path(sys.argv[2]).touch()
time.sleep(30)
PY
  lock_pid=$!
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -f "$BATS_TEST_TMPDIR/auth-lock-ready" ] && break
    sleep 0.05
  done

  FFS_HOST=codex FAKE_CODEX_REFRESH_AUTH=1 CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
  run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  kill "$lock_pid" 2>/dev/null || true
  wait "$lock_pid" 2>/dev/null || true

  [ "$status" -eq 75 ]
  [[ "$output" == *"auth lock remained busy"* ]]
}

@test "Codex auth with group-readable mode fails before the drive" {
  chmod 640 "$CODEX_SOURCE_ROOT/auth.json"
  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"
  [ "$status" -eq 78 ]
  [[ "$output" == *"mode 0600"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
}

@test "a legitimate OAuth refresh does not create false resume drift" {
  RUN_STATE="$BATS_TEST_TMPDIR/oauth-resume-state"
  FFS_HOST=codex FAKE_CODEX_REFRESH_AUTH=1 FAKE_CODEX_DRIVE_RC=42 \
    GSD_RUN_STATE_DIR="$RUN_STATE" CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"
  [ "$status" -eq 42 ]
  grep -Fq refreshed "$CODEX_SOURCE_ROOT/auth.json"

  FFS_HOST=codex GSD_RUN_STATE_DIR="$RUN_STATE" CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"
  [ "$status" -eq 0 ]
  [[ "$output" == *"CODEX_OK"* ]]
}

@test "an exact Fable request never probes or drives another model" {
  FFS_HOST=codex GSD_MODEL_REQUEST='{"kind":"exact","id":"claude-fable-5"}' FAKE_CLAUDE_PROBE_MODE=fail \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 69 ]
  [ -f "$BATS_TEST_TMPDIR/claude.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
}

@test "raw vendor model ids are rejected unless typed exact" {
  FFS_HOST=codex GSD_LEAD_MODEL=gpt-5.6-sol CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"
  [ "$status" -eq 2 ]
  [[ "$output" == *"raw vendor model ids require"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.probed" ]
}

@test "openclaw consumer layout resolves the vendored typed model helper" {
  CONSUMER="$BATS_TEST_TMPDIR/openclaw-consumer"
  mkdir -p "$CONSUMER/scripts" "$CONSUMER/packages/feature-fix-swarm/lib"
  cp -R "$ROOT/scripts/gsd" "$CONSUMER/scripts/gsd"
  cp "$ROOT/lib/model_requests.py" "$CONSUMER/packages/feature-fix-swarm/lib/model_requests.py"

  GSD_MODEL_REQUEST='{"kind":"tier","name":"invalid"}' \
    run bash "$CONSUMER/scripts/gsd/gsd-run.sh" /gsd-quick test
  [ "$status" -eq 2 ]
  [[ "$output" == *"model-request: tier request"* ]]
  [[ "$output" != *"typed model request helper is missing"* ]]
}

@test "typed exact Codex request drives only the exact model" {
  FFS_HOST=claude GSD_MODEL_REQUEST='{"kind":"exact","id":"gpt-5.6-sol"}' \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"
  [ "$status" -eq 0 ]
  grep -F 'model="gpt-5.6-sol"' "$BATS_TEST_TMPDIR/codex.args"
  [ ! -f "$BATS_TEST_TMPDIR/claude.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
}

@test "frontier tier resolves gpt-5.6-sol at xhigh on the Codex host" {
  FFS_HOST=codex GSD_MODEL_REQUEST='{"kind":"tier","name":"frontier"}' \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [ -f "$BATS_TEST_TMPDIR/codex.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
  grep -F 'model="gpt-5.6-sol"' "$BATS_TEST_TMPDIR/codex.args"
  grep -F 'model_reasoning_effort="xhigh"' "$BATS_TEST_TMPDIR/codex.args"
}

@test "frontier tier resolves claude-fable-5 on the Claude host" {
  FFS_HOST=claude GSD_MODEL_REQUEST='{"kind":"tier","name":"frontier"}' \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [ -f "$BATS_TEST_TMPDIR/claude.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
  grep -Fx -- '--model' "$BATS_TEST_TMPDIR/claude.args"
  grep -Fx 'claude-fable-5' "$BATS_TEST_TMPDIR/claude.args"
}

@test "GSD_LEAD_MODEL=fable keeps exact claude-fable-5 semantics and requires network_mode=enabled" {
  FFS_HOST=claude GSD_LEAD_MODEL=fable GSD_NETWORK_MODE=none GSD_NETWORK_PURPOSE= \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 78 ]
  [[ "$output" == *"exact Fable requires network_mode=enabled"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
}

@test "GSD_LEAD_MODEL=fable exact request host-pins Claude even when FFS_HOST requests Codex" {
  FFS_HOST=codex GSD_LEAD_MODEL=fable GSD_NETWORK_MODE=enabled GSD_NETWORK_PURPOSE=general \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [ -f "$BATS_TEST_TMPDIR/claude.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
  grep -Fx 'claude-fable-5' "$BATS_TEST_TMPDIR/claude.args"
}

@test "GSD_LEAD_MODEL=fable is incompatible with danger-full-access sandbox" {
  FFS_HOST=codex GSD_LEAD_MODEL=fable GSD_RUN_ID=fable-danger-run \
    GSD_SANDBOX_MODE=danger-full-access GSD_NETWORK_MODE=enabled GSD_NETWORK_PURPOSE=general \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 78 ]
  [[ "$output" == *"exact Fable and Codex danger-full-access are incompatible"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
}

@test "unknown tier is rejected with the 4-tier error message" {
  GSD_MODEL_REQUEST='{"kind":"tier","name":"premium"}' \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 2 ]
  [[ "$output" == *"frontier|judgment|execution|volume"* ]]
}

@test "resume refuses Codex CLI drift before a second stateful drive" {
  RUN_STATE="$BATS_TEST_TMPDIR/resume-state"
  FFS_HOST=codex FAKE_CODEX_VERSION=0.146.1 FAKE_CODEX_DRIVE_RC=42 GSD_RUN_STATE_DIR="$RUN_STATE" \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"
  [ "$status" -eq 42 ]
  rm -f "$BATS_TEST_TMPDIR/codex.args" "$BATS_TEST_TMPDIR/codex.probed"

  FFS_HOST=codex FAKE_CODEX_VERSION=0.145.0 GSD_RUN_STATE_DIR="$RUN_STATE" \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 78 ]
  [[ "$output" == *"resume tuple drift"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
}

@test "linked worktrees resolve the default runner lock through the common git directory" {
  REPO="$BATS_TEST_TMPDIR/common-lock-repo"
  LINKED="$BATS_TEST_TMPDIR/common-lock-linked"
  mkdir -p "$REPO"
  git -C "$REPO" init -q
  git -C "$REPO" config user.email test@example.com
  git -C "$REPO" config user.name Test
  printf '%s\n' seed > "$REPO/seed"
  git -C "$REPO" add seed
  git -C "$REPO" commit -qm seed
  git -C "$REPO" worktree add -q -b linked "$LINKED"

  FFS_HOST=codex GSD_PROJECT_SKILLS_ROOT="$PROJECT_AGENTS_ROOT/skills" \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$LINKED' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  COMMON_DIR="$(git -C "$REPO" rev-parse --absolute-git-dir)"
  [ -f "$COMMON_DIR/ffs/gsd-run/gsd-run.status" ]
  grep -Fq "pidfile=$COMMON_DIR/ffs/gsd-run/gsd-run.pid" <<<"$output"
}

# ── Phase 4 Task 1: coord claim/release wiring (P-22, P-24, P-25) ──────────
# P-27: scripts/coord is copied into HARNESS_ROOT inside each case's OWN
# body, never in setup() — every pre-existing case above stays on the
# fail-soft no-coord-layer path byte-identically.

@test "explicit GSD_RUN_ID holds a coord claim during the drive and releases it after (coord wiring)" {
  export HOME="$RF_REAL_HOME"  # coord.py needs the real python user-site (filelock >= 3.30)
  cp -R "$ROOT/scripts/coord" "$HARNESS_ROOT/scripts/coord"
  REGISTRY="$BATS_TEST_TMPDIR/.feature-fix-swarm/coord/registry.json"
  SNAPSHOT="$BATS_TEST_TMPDIR/registry.mid-drive.json"
  cat > "$STUB_DIR/coord-codex" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = "--version" ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then echo FFS_HOST_PROBE_READY; exit 0; fi
cp "$REGISTRY" "$SNAPSHOT" 2>/dev/null || true
touch "$BATS_TEST_TMPDIR/coord.args"
echo CODEX_OK
exit 0
EOF
  chmod +x "$STUB_DIR/coord-codex"

  FFS_HOST=codex GSD_RUN_ID=spec-009 CODEX_BIN=coord-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [ -f "$BATS_TEST_TMPDIR/coord.args" ]

  # mid-drive snapshot: before-and-after alone would pass against a no-op,
  # so the discriminating half is that the claim is visible WHILE the drive
  # (the stub) is running.
  [ -f "$SNAPSHOT" ]
  run python3 -c "import json; d=json.load(open('$SNAPSHOT')); assert 'claim:spec-009' in d['claims'], d['claims']"
  [ "$status" -eq 0 ]

  # after the EXIT trap completes, the claim is gone.
  [ -f "$REGISTRY" ]
  run python3 -c "import json; d=json.load(open('$REGISTRY')); assert 'claim:spec-009' not in d.get('claims', {}), d['claims']"
  [ "$status" -eq 0 ]
}

@test "a live foreign coord claim holder refuses the launch with coord.py's own exit 3, never gsd-run.sh's pidfile 75" {
  export HOME="$RF_REAL_HOME"  # coord.py needs the real python user-site (filelock >= 3.30)
  cp -R "$ROOT/scripts/coord" "$HARNESS_ROOT/scripts/coord"
  ( sleep 30 ) &
  anchor_pid=$!
  env -C "$BATS_TEST_TMPDIR" FFS_COORD_ANCHOR_PID="$anchor_pid" FFS_RUN_ID=foreign \
    python3 "$ROOT/scripts/coord/coord.py" claim spec-009 >/dev/null

  FFS_HOST=codex GSD_RUN_ID=spec-009 CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  kill "$anchor_pid" 2>/dev/null || true
  wait "$anchor_pid" 2>/dev/null || true

  [ "$status" -eq 3 ]
  [ "$status" -ne 75 ]
  [[ "$output" == *"CLAIM-HELD"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
}

@test "a runner with no explicit GSD_RUN_ID takes no coord claim and still completes (P-22)" {
  cp -R "$ROOT/scripts/coord" "$HARNESS_ROOT/scripts/coord"
  REGISTRY="$BATS_TEST_TMPDIR/.feature-fix-swarm/coord/registry.json"

  run env -u GSD_RUN_ID FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [[ "$output" == *"CODEX_OK"* ]]
  if [ -f "$REGISTRY" ]; then
    run python3 -c "import json; d=json.load(open('$REGISTRY')); assert not d.get('claims'), d['claims']"
    [ "$status" -eq 0 ]
  fi
}

@test "a repo without the coord layer runs the drive byte-identically to today (P-25 fail-soft)" {
  # Non-ledger run id: a LEDGER-shaped id (spec-NNN) now legitimately
  # writes the REQ-703 budget mapping into the evidence store, which this
  # test's no-store assertion predates. P-25's claim is about the COORD
  # layer specifically, so the fixture id stays outside the ledger shape.
  FFS_HOST=codex GSD_RUN_ID=coordless-fixture CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [[ "$output" == *"CODEX_OK"* ]]
  [ ! -d "$BATS_TEST_TMPDIR/.feature-fix-swarm" ]
}

@test "a GSD_RUN_ID over coord.py's 64-byte CLAIM_ID_RE cap is refused verbatim, never truncated to fit" {
  cp -R "$ROOT/scripts/coord" "$HARNESS_ROOT/scripts/coord"
  REGISTRY="$BATS_TEST_TMPDIR/.feature-fix-swarm/coord/registry.json"
  LONG_ID="$(python3 -c 'print("a" * 65)')"

  FFS_HOST=codex GSD_RUN_ID="$LONG_ID" CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 2 ]
  [[ "$output" == *"CLAIM_ID_RE"* ]]
  [[ "$output" == *"64"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
  if [ -f "$REGISTRY" ]; then
    run python3 -c "import json; d=json.load(open('$REGISTRY')); assert not d.get('claims'), d['claims']"
    [ "$status" -eq 0 ]
  fi
}

@test "a GSD_RUN_ID valid to the runner's sanitizer but invalid to coord.py's CLAIM_ID_RE is refused verbatim" {
  cp -R "$ROOT/scripts/coord" "$HARNESS_ROOT/scripts/coord"
  REGISTRY="$BATS_TEST_TMPDIR/.feature-fix-swarm/coord/registry.json"

  FFS_HOST=codex GSD_RUN_ID=_leading-underscore CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 2 ]
  [[ "$output" == *"CLAIM_ID_RE"* ]]
  [[ "$output" == *"64"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
  if [ -f "$REGISTRY" ]; then
    run python3 -c "import json; d=json.load(open('$REGISTRY')); assert not d.get('claims'), d['claims']"
    [ "$status" -eq 0 ]
  fi
}

# ── Phase 4 Task 2: renew, revalidate, abort-on-revocation, release-on-every-
# exit-path, sandbox write grant (P-24, P-24b, P-26, P-29) ─────────────────

@test "the heartbeat renews the coord claim; expires_at strictly increases across ticks (coord wiring)" {
  export HOME="$RF_REAL_HOME"  # coord.py needs the real python user-site (filelock >= 3.30)
  cp -R "$ROOT/scripts/coord" "$HARNESS_ROOT/scripts/coord"
  REGISTRY="$BATS_TEST_TMPDIR/.feature-fix-swarm/coord/registry.json"
  cat > "$STUB_DIR/renew-codex" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = "--version" ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then echo FFS_HOST_PROBE_READY; exit 0; fi
cp "$REGISTRY" "$BATS_TEST_TMPDIR/renew.snap1.json" 2>/dev/null || true
sleep 3
cp "$REGISTRY" "$BATS_TEST_TMPDIR/renew.snap2.json" 2>/dev/null || true
echo RENEW_OK
exit 0
EOF
  chmod +x "$STUB_DIR/renew-codex"

  FFS_HOST=codex GSD_RUN_ID=spec-009 GSD_HEARTBEAT_SECS=1 \
    CODEX_BIN=renew-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [ -f "$BATS_TEST_TMPDIR/renew.snap1.json" ]
  [ -f "$BATS_TEST_TMPDIR/renew.snap2.json" ]
  # This is also the ONLY mechanical proof of Task 1's call-site ordering: a
  # claim minted after acquire_run_state returns leaves the forked
  # subshell's RUN_COORD_GENERATION empty, coord_renew_run early-returns
  # every tick, and expires_at never moves.
  run python3 -c "
import json
a = json.load(open('$BATS_TEST_TMPDIR/renew.snap1.json'))['claims']['claim:spec-009']['expires_at']
b = json.load(open('$BATS_TEST_TMPDIR/renew.snap2.json'))['claims']['claim:spec-009']['expires_at']
assert b > a, (a, b)
"
  [ "$status" -eq 0 ]
}

@test "a generation bump on the live claim aborts a running drive with CLAIM-SUPERSEDED (coord wiring)" {
  export HOME="$RF_REAL_HOME"  # coord.py needs the real python user-site (filelock >= 3.30)
  cp -R "$ROOT/scripts/coord" "$HARNESS_ROOT/scripts/coord"
  REGISTRY="$BATS_TEST_TMPDIR/.feature-fix-swarm/coord/registry.json"
  cat > "$STUB_DIR/gen-bump-codex" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = "--version" ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then echo FFS_HOST_PROBE_READY; exit 0; fi
touch "$BATS_TEST_TMPDIR/gen-bump-drive-started"
sleep 20
echo GEN_BUMP_SHOULD_NEVER_FINISH
EOF
  chmod +x "$STUB_DIR/gen-bump-codex"

  run env FFS_HOST=codex GSD_RUN_ID=spec-009 GSD_HEARTBEAT_SECS=1 \
    CODEX_BIN=gen-bump-codex CLAUDE_BIN=fake-claude \
    bash -c '
      cd "$1"
      bash "$2" /gsd-quick test >"$1/gen-bump.log" 2>&1 &
      runner=$!
      i=0
      while [ ! -f "$1/gen-bump-drive-started" ] && [ "$i" -lt 1500 ]; do
        sleep 0.02
        i=$((i + 1))
      done
      [ -f "$1/gen-bump-drive-started" ] || { echo "drive did not start" >&2; cat "$1/gen-bump.log" >&2; exit 30; }
      python3 -c "
import json
p = \"$3\"
d = json.load(open(p))
d[\"claims\"][\"claim:spec-009\"][\"generation\"] = 2
json.dump(d, open(p, \"w\"))
"
      wait "$runner"
      exit $?
    ' _ "$BATS_TEST_TMPDIR" "$SCRIPT" "$REGISTRY"

  [ "$status" -ne 0 ]
  grep -Fq "CLAIM-SUPERSEDED" "$BATS_TEST_TMPDIR/gen-bump.log"
  [ -f "$BATS_TEST_TMPDIR/gen-bump-drive-started" ]
}

@test "a foreign holder_uuid takeover on the live claim also aborts mid-flight (P-24 exit-3 arm, coord wiring)" {
  export HOME="$RF_REAL_HOME"  # coord.py needs the real python user-site (filelock >= 3.30)
  cp -R "$ROOT/scripts/coord" "$HARNESS_ROOT/scripts/coord"
  REGISTRY="$BATS_TEST_TMPDIR/.feature-fix-swarm/coord/registry.json"
  cat > "$STUB_DIR/foreign-take-codex" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = "--version" ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then echo FFS_HOST_PROBE_READY; exit 0; fi
touch "$BATS_TEST_TMPDIR/foreign-take-drive-started"
sleep 20
echo FOREIGN_TAKE_SHOULD_NEVER_FINISH
EOF
  chmod +x "$STUB_DIR/foreign-take-codex"

  run env FFS_HOST=codex GSD_RUN_ID=spec-009 GSD_HEARTBEAT_SECS=1 \
    CODEX_BIN=foreign-take-codex CLAUDE_BIN=fake-claude \
    bash -c '
      cd "$1"
      bash "$2" /gsd-quick test >"$1/foreign-take.log" 2>&1 &
      runner=$!
      i=0
      while [ ! -f "$1/foreign-take-drive-started" ] && [ "$i" -lt 1500 ]; do
        sleep 0.02
        i=$((i + 1))
      done
      [ -f "$1/foreign-take-drive-started" ] || { echo "drive did not start" >&2; cat "$1/foreign-take.log" >&2; exit 30; }
      python3 -c "
import json, uuid
p = \"$3\"
d = json.load(open(p))
d[\"claims\"][\"claim:spec-009\"][\"holder_uuid\"] = str(uuid.uuid4())
json.dump(d, open(p, \"w\"))
"
      wait "$runner"
      exit $?
    ' _ "$BATS_TEST_TMPDIR" "$SCRIPT" "$REGISTRY"

  [ "$status" -ne 0 ]
  grep -Fq "CLAIM-SUPERSEDED" "$BATS_TEST_TMPDIR/foreign-take.log"
  [ -f "$BATS_TEST_TMPDIR/foreign-take-drive-started" ]
}

@test "a transient non-revocation renew failure inside the staleness budget does not abort the drive (P-24b, coord wiring)" {
  mkdir -p "$HARNESS_ROOT/scripts/coord"
  write_fixture_coord "$HARNESS_ROOT/scripts/coord/coord.py"
  cat > "$STUB_DIR/transient-codex" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = "--version" ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then echo FFS_HOST_PROBE_READY; exit 0; fi
sleep 2
echo TRANSIENT_OK
exit 0
EOF
  chmod +x "$STUB_DIR/transient-codex"

  # ttl_secs=30 vs a ~2s drive is what makes this the transient case rather
  # than the persistent one below -- changing either number silently
  # converts one case into the other.
  FFS_HOST=codex GSD_RUN_ID=spec-009 GSD_HEARTBEAT_SECS=1 FIXTURE_TTL_SECS=30 \
    CODEX_BIN=transient-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [[ "$output" == *"TRANSIENT_OK"* ]]
}

@test "a persistently failing renew past the claim's own ttl_secs DOES abort with CLAIM-STALE (P-24b CRITICAL arm, coord wiring)" {
  mkdir -p "$HARNESS_ROOT/scripts/coord"
  write_fixture_coord "$HARNESS_ROOT/scripts/coord/coord.py"
  RUN_STATE="$BATS_TEST_TMPDIR/persist-state"
  cat > "$STUB_DIR/persist-codex" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = "--version" ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then echo FFS_HOST_PROBE_READY; exit 0; fi
touch "$BATS_TEST_TMPDIR/persist-drive-started"
sleep 30
echo PERSIST_SHOULD_NEVER_FINISH
EOF
  chmod +x "$STUB_DIR/persist-codex"

  # Anchor the elapsed measurement at the drive-started sentinel, not the
  # pre-launch epoch -- runner startup (probe, seeding) runs BEFORE the ttl=3
  # budget clock matters, and measuring it too made this case flake under
  # load (phase-4 verifier W2). The budget guarantee is kill within
  # ttl_secs + one tick of the claim being held, which the sentinel bounds.
  run env FFS_HOST=codex GSD_RUN_ID=spec-009 GSD_HEARTBEAT_SECS=1 FIXTURE_TTL_SECS=3 \
    GSD_RUN_STATE_DIR="$RUN_STATE" CODEX_BIN=persist-codex CLAUDE_BIN=fake-claude \
    bash -c '
      cd "$1"
      bash "$2" /gsd-quick test >"$1/persist.log" 2>&1 &
      runner=$!
      i=0
      while [ ! -f "$1/persist-drive-started" ] && [ "$i" -lt 3000 ]; do
        sleep 0.02
        i=$((i + 1))
      done
      [ -f "$1/persist-drive-started" ] || { echo "drive did not start" >&2; cat "$1/persist.log" >&2; exit 30; }
      started="$(date +%s)"
      wait "$runner"
      rc=$?
      ended="$(date +%s)"
      echo "PERSIST_ELAPSED=$((ended - started))"
      exit "$rc"
    ' _ "$BATS_TEST_TMPDIR" "$SCRIPT"

  [ "$status" -ne 0 ]
  elapsed="$(printf '%s\n' "$output" | sed -n 's/^PERSIST_ELAPSED=//p' | tail -1)"
  # far below the stub's own 30s -- an implementation that returns 0 from
  # coord_renew_run on every 69 passes every other case and hangs here.
  [ -n "$elapsed" ]
  [ "$elapsed" -lt 10 ]
  grep -Fq "CLAIM-STALE" "$BATS_TEST_TMPDIR/persist.log"
  [ -f "$BATS_TEST_TMPDIR/persist-drive-started" ]
  grep -Fx 'coord_abort=CLAIM-STALE' "$RUN_STATE/gsd-run.status"
}

@test "the claim is released on the non-zero-drive exit path (coord wiring)" {
  export HOME="$RF_REAL_HOME"  # coord.py needs the real python user-site (filelock >= 3.30)
  cp -R "$ROOT/scripts/coord" "$HARNESS_ROOT/scripts/coord"
  REGISTRY="$BATS_TEST_TMPDIR/.feature-fix-swarm/coord/registry.json"

  FFS_HOST=codex GSD_RUN_ID=spec-009 FAKE_CODEX_DRIVE_RC=1 \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 1 ]
  [ -f "$REGISTRY" ]
  run python3 -c "import json; d=json.load(open('$REGISTRY')); assert 'claim:spec-009' not in d.get('claims', {}), d['claims']"
  [ "$status" -eq 0 ]
}

@test "the claim is released on the run_bounded timeout exit path (coord wiring)" {
  export HOME="$RF_REAL_HOME"  # coord.py needs the real python user-site (filelock >= 3.30)
  cp -R "$ROOT/scripts/coord" "$HARNESS_ROOT/scripts/coord"
  REGISTRY="$BATS_TEST_TMPDIR/.feature-fix-swarm/coord/registry.json"
  cat > "$STUB_DIR/timeout-codex" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = "--version" ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then echo FFS_HOST_PROBE_READY; exit 0; fi
sleep 30
EOF
  chmod +x "$STUB_DIR/timeout-codex"

  FFS_HOST=codex GSD_RUN_ID=spec-009 TIMEOUT=1 \
    CODEX_BIN=timeout-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 124 ]
  [ -f "$REGISTRY" ]
  run python3 -c "import json; d=json.load(open('$REGISTRY')); assert 'claim:spec-009' not in d.get('claims', {}), d['claims']"
  [ "$status" -eq 0 ]
}

@test "the claim is released when the runner is SIGTERMed externally mid-drive (coord wiring)" {
  export HOME="$RF_REAL_HOME"  # coord.py needs the real python user-site (filelock >= 3.30)
  cp -R "$ROOT/scripts/coord" "$HARNESS_ROOT/scripts/coord"
  REGISTRY="$BATS_TEST_TMPDIR/.feature-fix-swarm/coord/registry.json"
  cat > "$STUB_DIR/sigterm-codex" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = "--version" ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then echo FFS_HOST_PROBE_READY; exit 0; fi
touch "$BATS_TEST_TMPDIR/sigterm-drive-started"
sleep 30
EOF
  chmod +x "$STUB_DIR/sigterm-codex"

  run env FFS_HOST=codex GSD_RUN_ID=spec-009 CODEX_BIN=sigterm-codex CLAUDE_BIN=fake-claude \
    bash -c '
      cd "$1"
      bash "$2" /gsd-quick test >"$1/sigterm.log" 2>&1 &
      runner=$!
      i=0
      while [ ! -f "$1/sigterm-drive-started" ] && [ "$i" -lt 1500 ]; do
        sleep 0.02
        i=$((i + 1))
      done
      [ -f "$1/sigterm-drive-started" ] || { echo "drive did not start" >&2; cat "$1/sigterm.log" >&2; exit 30; }
      kill -TERM "$runner"
      wait "$runner"
      exit $?
    ' _ "$BATS_TEST_TMPDIR" "$SCRIPT"

  [ "$status" -ne 0 ]
  [ -f "$REGISTRY" ]
  run python3 -c "import json; d=json.load(open('$REGISTRY')); assert 'claim:spec-009' not in d.get('claims', {}), d['claims']"
  [ "$status" -eq 0 ]
}

@test "the sandboxed Codex drive can write both the run worktree and the shared coord store (P-29, coord wiring)" {
  FFS_HOST=codex GSD_NETWORK_MODE=none CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  run python3 - "$BATS_TEST_TMPDIR/codex.config" <<'PY'
import ast, re, sys
text = open(sys.argv[1]).read()
roots = ast.literal_eval(re.search(r'^writable_roots = (.+)$', text, re.M).group(1))
assert len(roots) == 6, roots
assert any(r.endswith('/.feature-fix-swarm') for r in roots), roots
assert any('/.claude/worktrees/' in r for r in roots), roots
assert any(r.endswith('/objects') for r in roots), roots
assert any('/.git/worktrees/' in r for r in roots), roots
assert any(r.endswith('/refs/heads/gsd') for r in roots), roots
assert any(r.endswith('/logs/refs/heads/gsd') for r in roots), roots
assert not any(r.rstrip('/').endswith('/.git') for r in roots), roots
assert not any(r.rstrip('/').endswith('/refs/heads') for r in roots), roots
import os
for r in roots:
    if r.endswith('/refs/heads/gsd') or r.endswith('/logs/refs/heads/gsd'):
        assert os.path.isdir(r), f"granted root must be pre-created: {r}"
PY
  [ "$status" -eq 0 ]
}

@test "respawn: rc 124 respawns exactly once regardless of commits" {
  cat > "$STUB_DIR/respawn-rc124" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = --version ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then echo FFS_HOST_PROBE_READY; exit 0; fi
printf 'x\n' >> "$BATS_TEST_TMPDIR/respawn-rc124.count"
exit 124
EOF
  chmod +x "$STUB_DIR/respawn-rc124"

  FFS_HOST=codex CODEX_BIN=respawn-rc124 CLAUDE_BIN=fake-claude FFS_RESPAWN_MIN_SECS=1 \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 124 ]
  [ "$(wc -l < "$BATS_TEST_TMPDIR/respawn-rc124.count")" -eq 2 ]
  respawn_lines="$(printf '%s\n' "$output" | grep -c 'GSD-RUN:RESPAWN attempt=2/2 rc=124')"
  [ "$respawn_lines" -eq 1 ]
}

@test "respawn: nonzero rc with zero commits respawns once" {
  cat > "$STUB_DIR/respawn-zero-commits" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = --version ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then echo FFS_HOST_PROBE_READY; exit 0; fi
printf 'x\n' >> "$BATS_TEST_TMPDIR/respawn-zero-commits.count"
exit 1
EOF
  chmod +x "$STUB_DIR/respawn-zero-commits"

  FFS_HOST=codex CODEX_BIN=respawn-zero-commits CLAUDE_BIN=fake-claude FFS_RESPAWN_MIN_SECS=1 \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 1 ]
  [ "$(wc -l < "$BATS_TEST_TMPDIR/respawn-zero-commits.count")" -eq 2 ]
  respawn_lines="$(printf '%s\n' "$output" | grep -c 'GSD-RUN:RESPAWN attempt=2/2 rc=1')"
  [ "$respawn_lines" -eq 1 ]
}

@test "respawn: nonzero rc with a new commit does not respawn" {
  cat > "$STUB_DIR/respawn-with-commit" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = --version ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then echo FFS_HOST_PROBE_READY; exit 0; fi
printf 'x\n' >> "$BATS_TEST_TMPDIR/respawn-with-commit.count"
printf 'progress\n' > progress.txt
git add progress.txt
git commit -qm 'attempt progress'
exit 1
EOF
  chmod +x "$STUB_DIR/respawn-with-commit"

  FFS_HOST=codex CODEX_BIN=respawn-with-commit CLAUDE_BIN=fake-claude FFS_RESPAWN_MIN_SECS=1 \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 1 ]
  [ "$(wc -l < "$BATS_TEST_TMPDIR/respawn-with-commit.count")" -eq 1 ]
  [[ "$output" != *"GSD-RUN:RESPAWN"* ]]
}

@test "respawn: git probe failure fails closed" {
  mkdir -p "$STUB_DIR/gitshim"
  cat > "$STUB_DIR/gitshim/git" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do
  if [ "$a" = "rev-list" ]; then
    exit 1
  fi
done
exec /usr/bin/git "$@"
EOF
  chmod +x "$STUB_DIR/gitshim/git"

  cat > "$STUB_DIR/respawn-git-probe" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = --version ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then echo FFS_HOST_PROBE_READY; exit 0; fi
printf 'x\n' >> "$BATS_TEST_TMPDIR/respawn-git-probe.count"
exit 1
EOF
  chmod +x "$STUB_DIR/respawn-git-probe"

  PATH="$STUB_DIR/gitshim:$PATH" FFS_HOST=codex CODEX_BIN=respawn-git-probe CLAUDE_BIN=fake-claude FFS_RESPAWN_MIN_SECS=1 \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 1 ]
  [ "$(wc -l < "$BATS_TEST_TMPDIR/respawn-git-probe.count")" -eq 1 ]
  [[ "$output" != *"GSD-RUN:RESPAWN"* ]]
}

@test "respawn: FFS_RESPAWN_MAX=0 disables" {
  cat > "$STUB_DIR/respawn-disabled" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = --version ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then echo FFS_HOST_PROBE_READY; exit 0; fi
printf 'x\n' >> "$BATS_TEST_TMPDIR/respawn-disabled.count"
exit 124
EOF
  chmod +x "$STUB_DIR/respawn-disabled"

  FFS_HOST=codex CODEX_BIN=respawn-disabled CLAUDE_BIN=fake-claude FFS_RESPAWN_MAX=0 FFS_RESPAWN_MIN_SECS=1 \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 124 ]
  [ "$(wc -l < "$BATS_TEST_TMPDIR/respawn-disabled.count")" -eq 1 ]
  [[ "$output" != *"GSD-RUN:RESPAWN"* ]]
}

@test "respawn: quarantined run status never respawns" {
  RUN_STATE="$BATS_TEST_TMPDIR/run-state-quarantine"
  cat > "$STUB_DIR/respawn-quarantine" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = --version ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then echo FFS_HOST_PROBE_READY; exit 0; fi
printf 'x\n' >> "$BATS_TEST_TMPDIR/respawn-quarantine.count"
printf 'state=quarantined\n' >> "$RUN_STATE/gsd-run.status"
exit 124
EOF
  chmod +x "$STUB_DIR/respawn-quarantine"

  FFS_HOST=codex CODEX_BIN=respawn-quarantine CLAUDE_BIN=fake-claude \
    GSD_RUN_STATE_DIR="$RUN_STATE" FFS_RESPAWN_MIN_SECS=1 \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 124 ]
  [ "$(wc -l < "$BATS_TEST_TMPDIR/respawn-quarantine.count")" -eq 1 ]
  [[ "$output" != *"GSD-RUN:RESPAWN"* ]]
}

@test "respawn: attempt 1 log lines survive attempt 2" {
  cat > "$STUB_DIR/respawn-log-survival" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = --version ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then echo FFS_HOST_PROBE_READY; exit 0; fi
n=0
[ -f "$BATS_TEST_TMPDIR/respawn-log-survival.count" ] && n=\$(wc -l < "$BATS_TEST_TMPDIR/respawn-log-survival.count")
printf 'x\n' >> "$BATS_TEST_TMPDIR/respawn-log-survival.count"
echo "MARKER-ATTEMPT-\$((n + 1))"
exit 124
EOF
  chmod +x "$STUB_DIR/respawn-log-survival"

  FFS_HOST=codex CODEX_BIN=respawn-log-survival CLAUDE_BIN=fake-claude FFS_RESPAWN_MIN_SECS=1 \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 124 ]
  log_file="$(ls "$BATS_TEST_TMPDIR"/.planning/logs/gsd-run-*.log)"
  grep -q 'MARKER-ATTEMPT-1' "$log_file"
  grep -q 'MARKER-ATTEMPT-2' "$log_file"
  grep -q 'GSD-RUN:RESPAWN attempt=2/2 rc=124' "$log_file"
}

@test "respawn: attempt 2 argv is byte-identical to attempt 1 (no cross-vendor replay)" {
  # Argv (via CODEX_SESSION_CONTRACT) legitimately contains embedded
  # newlines, so each attempt's full "$@" is recorded to its OWN file
  # (named by attempt index) rather than appended as a line to a shared
  # file -- a line-oriented record would miscount here.
  cat > "$STUB_DIR/respawn-argv-identical" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = --version ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then echo FFS_HOST_PROBE_READY; exit 0; fi
n=0
[ -f "$BATS_TEST_TMPDIR/respawn-argv-identical.count" ] && n=\$(wc -l < "$BATS_TEST_TMPDIR/respawn-argv-identical.count")
printf 'x\n' >> "$BATS_TEST_TMPDIR/respawn-argv-identical.count"
printf '%s' "\$@" > "$BATS_TEST_TMPDIR/respawn-argv-identical.attempt-\$((n + 1))"
exit 124
EOF
  chmod +x "$STUB_DIR/respawn-argv-identical"

  FFS_HOST=codex CODEX_BIN=respawn-argv-identical CLAUDE_BIN=fake-claude FFS_RESPAWN_MIN_SECS=1 \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 124 ]
  [ "$(wc -l < "$BATS_TEST_TMPDIR/respawn-argv-identical.count")" -eq 2 ]
  [ -s "$BATS_TEST_TMPDIR/respawn-argv-identical.attempt-1" ]
  [ -s "$BATS_TEST_TMPDIR/respawn-argv-identical.attempt-2" ]
  cmp -s "$BATS_TEST_TMPDIR/respawn-argv-identical.attempt-1" "$BATS_TEST_TMPDIR/respawn-argv-identical.attempt-2"
}

@test "respawn: rc 124 attempt 1 then success ends completed" {
  RUN_STATE="$BATS_TEST_TMPDIR/run-state-success"
  cat > "$STUB_DIR/respawn-then-success" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = --version ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then echo FFS_HOST_PROBE_READY; exit 0; fi
n=0
[ -f "$BATS_TEST_TMPDIR/respawn-then-success.count" ] && n=\$(wc -l < "$BATS_TEST_TMPDIR/respawn-then-success.count")
printf 'x\n' >> "$BATS_TEST_TMPDIR/respawn-then-success.count"
if [ "\$n" -eq 0 ]; then
  exit 124
fi
echo DRIVE_OK
exit 0
EOF
  chmod +x "$STUB_DIR/respawn-then-success"

  FFS_HOST=codex CODEX_BIN=respawn-then-success CLAUDE_BIN=fake-claude \
    GSD_RUN_STATE_DIR="$RUN_STATE" FFS_RESPAWN_MIN_SECS=1 \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [ "$(wc -l < "$BATS_TEST_TMPDIR/respawn-then-success.count")" -eq 2 ]
  respawn_lines="$(printf '%s\n' "$output" | grep -c 'GSD-RUN:RESPAWN attempt=2/2 rc=124')"
  [ "$respawn_lines" -eq 1 ]
  grep -qx 'state=completed' "$RUN_STATE/gsd-run.status"
}

@test "respawn: durable lifecycle respawn budget at zero blocks respawn" {
  run_id="lifecycle-budget-test"
  primary_root="$(cd "$BATS_TEST_TMPDIR" && /usr/bin/git rev-parse --show-toplevel)"
  worktree_root="$primary_root/.claude/worktrees/$run_id"
  mkdir -p "$primary_root/.claude/worktrees"
  /usr/bin/git -C "$BATS_TEST_TMPDIR" worktree add --detach "$worktree_root" HEAD >/dev/null
  run_state="$worktree_root/.planning/run-state"
  resume_json="[\"bash\",\"$worktree_root/scripts/gsd/gsd-run.sh\",\"/gsd-quick\",\"test\"]"
  ( cd "$worktree_root" && bash "$HARNESS_ROOT/scripts/gsd/lifecycle.sh" checkpoint "$run_id" running seed manual '{}' "$resume_json" '{"respawns":0,"wakes":0,"ci_reruns":0}' )

  cat > "$STUB_DIR/respawn-lifecycle" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = --version ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then echo FFS_HOST_PROBE_READY; exit 0; fi
printf 'x\n' >> "$BATS_TEST_TMPDIR/respawn-lifecycle.count"
exit 124
EOF
  chmod +x "$STUB_DIR/respawn-lifecycle"

  FFS_HOST=codex CODEX_BIN=respawn-lifecycle CLAUDE_BIN=fake-claude \
    GSD_RUN_ID="$run_id" GSD_RUN_STATE_DIR="$run_state" FFS_RESPAWN_MIN_SECS=1 \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 124 ]
  [ "$(wc -l < "$BATS_TEST_TMPDIR/respawn-lifecycle.count")" -eq 1 ]
  [[ "$output" == *"GSD-RUN:RESPAWN budget-exhausted run=$run_id"* ]]
  [[ "$output" != *"GSD-RUN:RESPAWN attempt="* ]]
}

@test "respawn: session-limit banner checkpoints waiting(time) instead of respawning" {
  cat > "$STUB_DIR/wake-banner-drive" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = --version ]; then echo 'codex-cli 0.146.1'; exit 0; fi
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then echo FFS_HOST_PROBE_READY; exit 0; fi
printf 'x\n' >> "$BATS_TEST_TMPDIR/wake-banner-drive.count"
echo "You have hit your usage limit. Your limit resets 3:30 pm"
exit 1
EOF
  chmod +x "$STUB_DIR/wake-banner-drive"

  FFS_HOST=codex CODEX_BIN=wake-banner-drive CLAUDE_BIN=fake-claude \
    GSD_RUN_ID=wake-banner-run FFS_RESPAWN_MIN_SECS=1 \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  # exactly one drive attempt: the banner path yields, it never respawns
  [ "$(wc -l < "$BATS_TEST_TMPDIR/wake-banner-drive.count")" -eq 1 ]
  [[ "$output" == *"GSD-RUN:SESSION-WAKE checkpointed run=wake-banner-run"* ]]
  [[ "$output" != *"GSD-RUN:RESPAWN attempt="* ]]
  record="$BATS_TEST_TMPDIR/.claude/worktrees/wake-banner-run/.planning/run-state/lifecycle-wake-banner-run.json"
  [ -f "$record" ]
  [ "$(jq -r .state "$record")" = waiting ]
  [ "$(jq -r .wake_condition.type "$record")" = time ]
  [ "$(jq -r '.resume_argv[0]' "$record")" = "scripts/gsd/gsd-run.sh" ]
  [ "$(jq -r '.resume_argv[1]' "$record")" = "/gsd-quick" ]

  # FFS_SESSION_WAKE=off skips the banner path and restores the respawn decision
  rm -f "$record" "$BATS_TEST_TMPDIR/wake-banner-drive.count"
  FFS_HOST=codex CODEX_BIN=wake-banner-drive CLAUDE_BIN=fake-claude \
    GSD_RUN_ID=wake-banner-run FFS_SESSION_WAKE=off FFS_RESPAWN_MIN_SECS=1 \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"
  [ ! -f "$record" ]
  [[ "$output" != *"GSD-RUN:SESSION-WAKE"* ]]
}
