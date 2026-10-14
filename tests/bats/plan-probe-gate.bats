#!/usr/bin/env bats

bats_require_minimum_version 1.5.0

# The gate shells out to gsd-tools. Every case uses a stub that replays a
# fixture payload per probe, so the suite never depends on the host's
# gsd-core install (HOME is hermetic) and never runs plan command text.

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  # Run a copy so the runner-layout candidate ($SCRIPT_DIR/../../node_modules)
  # is this temp layout, not whatever node_modules the checkout happens to have.
  LAYOUT="$BATS_TEST_TMPDIR/layout"
  mkdir -p "$LAYOUT/scripts/gsd"
  cp "$ROOT/scripts/gsd/plan-probe-gate.sh" "$LAYOUT/scripts/gsd/plan-probe-gate.sh"
  SCRIPT="$LAYOUT/scripts/gsd/plan-probe-gate.sh"
  REPO="$BATS_TEST_TMPDIR/repo"
  FIXTURES="$BATS_TEST_TMPDIR/fixtures"
  export HOME="$BATS_TEST_TMPDIR/hermetic-home"
  mkdir -p "$REPO" "$FIXTURES" "$HOME"
  git -C "$REPO" init -q
  unset GSD_PROJECT
}

# write_stub <path>: a gsd-tools stand-in. `check <probe> <phase> --raw`
# prints the file named by STUB_<PROBE>; anything else fails like an older
# gsd-core that lacks the subcommand.
write_stub() {
  mkdir -p "$(dirname "$1")"
  cat > "$1" <<'JS'
#!/usr/bin/env node
const fs = require('fs');
const [, , verb, probe, phase] = process.argv;
if (process.env.STUB_RECORD) {
  fs.appendFileSync(process.env.STUB_RECORD,
    `${verb} ${probe} ${phase} project=${process.env.GSD_PROJECT || ''}\n`);
}
const key = 'STUB_' + String(probe || '').replace(/-/g, '_').toUpperCase();
if (verb !== 'check' || !process.env[key]) {
  process.stderr.write(`Unknown command: ${verb} ${probe}\n`);
  process.exit(1);
}
process.stdout.write(fs.readFileSync(process.env[key], 'utf8'));
JS
  chmod +x "$1"
}

write_payload() { # $1 = file name, $2 = JSON body
  printf '%s\n' "$2" > "$FIXTURES/$1"
}

ok_payloads() {
  write_payload paths.json '{"status":"ok","commands":[{"command":"npm test","status":"ok","severity":"none"}],"counts":{"blocker":0,"warning":0,"total":1},"readError":null}'
  write_payload directions.json '{"status":"ok","commands":[{"command":"npm test","statement":"non-zero exit","status":"ok","severity":"none","plan":"02-01-PLAN.md","task":"Task 1"}],"counts":{"blocker":0,"warning":0,"total":1},"readError":null}'
}

run_gate() { # $@ = gate args
  run bash -c "cd $(printf '%q' "$REPO") && STUB_VERIFY_COMMAND_PATHS=$(printf '%q' "$FIXTURES/paths.json") STUB_VERIFY_FAILURE_DIRECTIONS=$(printf '%q' "$FIXTURES/directions.json") bash $(printf '%q' "$SCRIPT") $*"
}

@test "both probes clean passes and reports the command counts" {
  write_stub "$REPO/node_modules/.bin/gsd-tools"
  ok_payloads

  run_gate 2

  [ "$status" -eq 0 ]
  [[ "$output" == *"plan-probe-gate: PASS phase=02 command_paths=1 failure_directions=1"* ]]
}

@test "a command without fails_when blocks and names the plan and task" {
  write_stub "$REPO/node_modules/.bin/gsd-tools"
  ok_payloads
  write_payload directions.json '{"status":"blocked","commands":[{"command":"npx vitest run src/a.test.ts","statement":null,"status":"missing","severity":"blocker","plan":"02-03-PLAN.md","task":"Task 2: wire the cron"}],"counts":{"blocker":1,"warning":0,"total":1},"readError":null}'

  run_gate 2

  [ "$status" -eq 1 ]
  [[ "$output" == *"BLOCKER: verify-failure-directions: 02-03-PLAN.md / Task 2: wire the cron: missing: npx vitest run src/a.test.ts"* ]]
  [[ "$output" == *"FAIL phase=02 blockers=1"* ]]
  [[ "$output" == *"<fails_when>"* ]]
}

@test "an unresolvable command target blocks" {
  write_stub "$REPO/node_modules/.bin/gsd-tools"
  ok_payloads
  write_payload paths.json '{"status":"blocked","commands":[{"command":"cd nowhere && npm test","status":"unresolved","severity":"blocker","reason":"target directory does not exist","plan":"02-01-PLAN.md"}],"counts":{"blocker":1,"warning":0,"total":1},"readError":null}'

  run_gate 2

  [ "$status" -eq 1 ]
  [[ "$output" == *"verify-command-paths: 02-01-PLAN.md: target directory does not exist: cd nowhere && npm test"* ]]
}

@test "an unresolvable phase blocks with the probe's read error" {
  write_stub "$REPO/node_modules/.bin/gsd-tools"
  ok_payloads
  write_payload directions.json '{"status":"unresolvable","commands":[],"counts":{"blocker":0,"warning":0,"total":0},"readError":"could not resolve phase directory for phase 9"}'

  run_gate 9

  [ "$status" -eq 1 ]
  [[ "$output" == *"phase 9 is unresolvable (could not resolve phase directory for phase 9)"* ]]
}

@test "an unknown probe status fails closed" {
  write_stub "$REPO/node_modules/.bin/gsd-tools"
  ok_payloads
  write_payload paths.json '{"status":"degraded","commands":[],"counts":{"blocker":0,"warning":0,"total":0},"readError":null}'

  run_gate 2

  [ "$status" -eq 1 ]
  [[ "$output" == *"unknown probe status 'degraded'"* ]]
}

@test "not_applicable payloads pass" {
  write_stub "$REPO/node_modules/.bin/gsd-tools"
  ok_payloads
  write_payload paths.json '{"status":"not_applicable","commands":[],"counts":{"blocker":0,"warning":0,"total":5},"readError":null}'

  run_gate 1

  [ "$status" -eq 0 ]
  [[ "$output" == *"PASS phase=01 command_paths=5 failure_directions=1"* ]]
}

@test "a gsd-tools without the probes falls through to the host install" {
  # Repo-pinned gsd-tools answers nothing for these probes (older gsd-core).
  mkdir -p "$REPO/node_modules/.bin"
  printf '%s\n' '#!/usr/bin/env node' 'process.stderr.write("Unknown command\n"); process.exit(1);' \
    > "$REPO/node_modules/.bin/gsd-tools"
  write_stub "$HOME/.claude/gsd-core/bin/gsd-tools.cjs"
  ok_payloads

  run_gate 2

  [ "$status" -eq 0 ]
  [[ "$output" == *"PASS phase=02"* ]]
}

@test "the FFS runner-layout gsd-tools is used when the repo has none" {
  write_stub "$LAYOUT/node_modules/@opengsd/gsd-core/gsd-core/bin/gsd-tools.cjs"
  ok_payloads

  run_gate 2

  [ "$status" -eq 0 ]
  [[ "$output" == *"PASS phase=02"* ]]
}

@test "GSD_PROJECT reaches gsd-tools and the verdict line" {
  write_stub "$REPO/node_modules/.bin/gsd-tools"
  ok_payloads
  record="$BATS_TEST_TMPDIR/calls.log"

  run bash -c "cd $(printf '%q' "$REPO") && GSD_PROJECT=431 STUB_RECORD=$(printf '%q' "$record") STUB_VERIFY_COMMAND_PATHS=$(printf '%q' "$FIXTURES/paths.json") STUB_VERIFY_FAILURE_DIRECTIONS=$(printf '%q' "$FIXTURES/directions.json") bash $(printf '%q' "$SCRIPT") 3"

  [ "$status" -eq 0 ]
  [[ "$output" == *"PASS phase=03 project=431"* ]]
  grep -qx 'check verify-command-paths 3 project=431' "$record"
  grep -qx 'check verify-failure-directions 3 project=431' "$record"
}

@test "no gsd-tools anywhere fails closed with exit 78" {
  run_gate 2

  [ "$status" -eq 78 ]
  [[ "$output" == *"no gsd-tools found"* ]]
}

@test "usage errors exit 2" {
  run bash "$SCRIPT"
  [ "$status" -eq 2 ]
  [[ "$output" == *"usage: plan-probe-gate.sh <phase-number>"* ]]

  run bash "$SCRIPT" 2a
  [ "$status" -eq 2 ]
  [[ "$output" == *"phase must be numeric: 2a"* ]]
}
