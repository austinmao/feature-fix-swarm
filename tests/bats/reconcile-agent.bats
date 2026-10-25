#!/usr/bin/env bats

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  REPO="$BATS_TEST_TMPDIR/repo"; mkdir -p "$REPO/scripts/gsd"
  cp "$ROOT/scripts/gsd/reconcile-agent.sh" "$ROOT/scripts/gsd/reconcile.sh" "$REPO/scripts/gsd/"
  cd "$REPO"; git init -q -b main
  export FFS_LAUNCH_AGENTS_DIR="$BATS_TEST_TMPDIR/agents"
  # launchctl stub: records calls; `print` succeeds only after a bootstrap
  cat > "$BATS_TEST_TMPDIR/launchctl" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "${LAUNCHCTL_LOG:?}"
case "$1" in
  bootstrap) touch "${LAUNCHCTL_LOG}.loaded" ;;
  bootout) rm -f "${LAUNCHCTL_LOG}.loaded" ;;
  print) [ -f "${LAUNCHCTL_LOG}.loaded" ] ;;
esac
STUB
  chmod +x "$BATS_TEST_TMPDIR/launchctl"
  export FFS_LAUNCHCTL="$BATS_TEST_TMPDIR/launchctl" LAUNCHCTL_LOG="$BATS_TEST_TMPDIR/launchctl.log"
}

@test "status before install is a typed rc 1" {
  [ "$(uname -s)" = Darwin ] || skip "launchd only"
  run bash scripts/gsd/reconcile-agent.sh status
  [ "$status" -eq 1 ]; [[ "$output" == RECONCILE-AGENT:not-installed* ]]
}

@test "install writes a per-repo plist with interval, workdir, PATH and log, and loads it" {
  [ "$(uname -s)" = Darwin ] || skip "launchd only"
  run bash scripts/gsd/reconcile-agent.sh install --interval 120
  [ "$status" -eq 0 ]; [[ "$output" == RECONCILE-AGENT:installed* ]]
  plist="$(ls "$FFS_LAUNCH_AGENTS_DIR"/org.featurefixswarm.reconcile.*.plist)"
  [ -f "$plist" ]
  run python3 - "$plist" "$REPO" <<'PY'
import plistlib, sys
p = plistlib.load(open(sys.argv[1], "rb")); repo = sys.argv[2]
assert p["StartInterval"] == 120, p
assert p["WorkingDirectory"].rstrip("/").endswith(repo.split("/")[-1]), p
assert p["ProgramArguments"] == ["/bin/bash", "scripts/gsd/reconcile.sh"], p
assert p["EnvironmentVariables"]["PATH"], p
assert p["StandardOutPath"].endswith("/.planning/run-state/reconcile-agent.log"), p
PY
  [ "$status" -eq 0 ]
  grep -q '^bootstrap gui/' "$LAUNCHCTL_LOG"
  run bash scripts/gsd/reconcile-agent.sh status
  [ "$status" -eq 0 ]; [[ "$output" == *'installed label=org.featurefixswarm.reconcile.'* ]]; [[ "$output" == *'interval=120s'* ]]
}

@test "uninstall boots out and removes the plist; interval below 60 is refused" {
  [ "$(uname -s)" = Darwin ] || skip "launchd only"
  bash scripts/gsd/reconcile-agent.sh install >/dev/null
  run bash scripts/gsd/reconcile-agent.sh uninstall
  [ "$status" -eq 0 ]; [ -z "$(ls "$FFS_LAUNCH_AGENTS_DIR" 2>/dev/null)" ]
  grep -q '^bootout gui/' "$LAUNCHCTL_LOG"
  run bash scripts/gsd/reconcile-agent.sh install --interval 10
  [ "$status" -eq 1 ]; [[ "$output" == *invalid* ]]
}

@test "missing reconcile.sh in the target repo is refused" {
  rm scripts/gsd/reconcile.sh
  run bash scripts/gsd/reconcile-agent.sh install
  [ "$status" -eq 1 ]; [[ "$output" == *'missing'* ]]
}
