#!/usr/bin/env bats
# scripts/ci/apt-install.sh — bounded retrying apt install.
#
# The defect this guards: CI's plain `apt-get update && apt-get install` HANGS
# (three job-timeout kills across PRs #117 and #119). A retry loop alone does
# not fix a hang, so the per-attempt timeout is the load-bearing part and is
# tested as such.

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  LEVER="$ROOT/scripts/ci/apt-install.sh"
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR"
  COUNTER="$BATS_TEST_TMPDIR/attempts"
  : > "$COUNTER"
  export APT_SUDO=""            # no sudo in the fixture
  export APT_ATTEMPTS=3
  export APT_UPDATE_TIMEOUT=2
  export APT_INSTALL_TIMEOUT=2
}

@test "no packages given is a usage error, not a silent success" {
  run bash "$LEVER"
  [ "$status" -eq 2 ]
  [[ "$output" == *"no packages given"* ]]
}

@test "a clean install runs update then install exactly once" {
  cat > "$STUB_DIR/apt-get" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$1" >> "$COUNTER"
exit 0
EOF
  chmod +x "$STUB_DIR/apt-get"
  PATH="$STUB_DIR:$PATH" run bash "$LEVER" shellcheck
  [ "$status" -eq 0 ]
  [ "$(grep -c update "$COUNTER")" -eq 1 ]
  [ "$(grep -c install "$COUNTER")" -eq 1 ]
}

@test "a HANGING update is reaped by the per-attempt timeout and retried" {
  # This is the real-world failure: apt-get update never returns. Without the
  # per-attempt timeout the job sits until the runner kills it.
  cat > "$STUB_DIR/apt-get" <<EOF
#!/usr/bin/env bash
if [ "\$1" = update ]; then
  n=\$(( \$(wc -l < "$COUNTER") + 1 ))
  printf 'update\n' >> "$COUNTER"
  [ "\$n" -eq 1 ] && sleep 30    # first attempt hangs
fi
exit 0
EOF
  chmod +x "$STUB_DIR/apt-get"
  PATH="$STUB_DIR:$PATH" run bash "$LEVER" bats
  [ "$status" -eq 0 ]
  [ "$(grep -c update "$COUNTER")" -ge 2 ]
  [[ "$output" == *"attempt 1/3 failed or timed out"* ]]
}

@test "a persistently failing install gives up nonzero rather than looping forever" {
  cat > "$STUB_DIR/apt-get" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$1" >> "$COUNTER"
exit 1
EOF
  chmod +x "$STUB_DIR/apt-get"
  PATH="$STUB_DIR:$PATH" run bash "$LEVER" nosuchpkg
  [ "$status" -eq 1 ]
  [[ "$output" == *"giving up after 3 attempts"* ]]
  [ "$(grep -c update "$COUNTER")" -eq 3 ]
}
