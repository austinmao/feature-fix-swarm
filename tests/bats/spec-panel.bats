#!/usr/bin/env bats
# spec-panel.sh — spec-004 AC-010 spec-authoring panel (US7). PATH-shim
# journeys (spec-003 precedent, no browser surface): vendor CLIs are
# stubbed via ADVERSARY_BIN_CLAUDE/ADVERSARY_BIN_CODEX; the lever under
# test (spec-panel.sh) is never mocked.

bats_require_minimum_version 1.5.0

setup() {
  LEVER="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/scripts/gsd/spec-panel.sh"
  WORK="$BATS_TEST_TMPDIR/work"
  mkdir -p "$WORK/bin"
  cd "$WORK"
  BRIEF="$WORK/brief.txt"
  printf 'Fixture brief: bound a retry on a flaky call.\n' > "$BRIEF"
  OUT="$WORK/out"
  export PATH="$WORK/bin:$PATH"
  # Default both vendor binaries to nonexistent names — a test that wants
  # one or both reachable overrides ADVERSARY_BIN_CLAUDE/CODEX explicitly
  # (same convention as tests/bats/plan-wall.bats / adversary-host.bats).
  export ADVERSARY_BIN_CLAUDE=nonexistent-claude-binary-xyz
  export ADVERSARY_BIN_CODEX=nonexistent-codex-binary-xyz
  unset SPEC_PANEL SPEC_PANEL_TIMEOUT
}

# stub_vendor <bin-name> <sentinel> <stdin-capture-file>
# Reads argv for -o/--output-last-message (codex shape) or falls through to
# stdout (claude shape). Body carries the sentinel plus the four fixed axis
# headers with content, so happy-path drafts always score 4-of-4 axes. A
# prompt containing "You are refuting" (spec-panel.sh's refute prompt) gets
# a distinct refuter-shaped body instead of the author-shaped one.
stub_vendor() {
  local name="$1" sentinel="$2" capture="$3"
  cat > "bin/$name" <<EOF
#!/usr/bin/env bash
last_message=""
while [ "\$#" -gt 0 ]; do
  case "\$1" in
    -o|--output-last-message) last_message="\$2"; shift 2 ;;
    *) shift ;;
  esac
done
input="\$(cat)"
printf '%s' "\$input" > "$capture"
body="## Coverage

${sentinel} covers the primary flow.

## Assumptions

${sentinel} assumes well-formed input.

## Failure modes

${sentinel} names the empty-input case.

## Testability

${sentinel} needs one check per branch."
case "\$input" in
  *"You are refuting"*)
    body="## Coverage

${sentinel} REFUTE: add retry-path coverage.

## Assumptions

${sentinel} REFUTE: add network-availability assumption.

## Failure modes

${sentinel} REFUTE: add timeout failure mode.

## Testability

${sentinel} REFUTE: add one retry-path test."
    ;;
esac
if [ -n "\$last_message" ]; then printf '%s\n' "\$body" > "\$last_message"; else printf '%s\n' "\$body"; fi
EOF
  chmod +x "bin/$name"
}

@test "SPEC_PANEL defaults off: no-op, no artifacts, exit 0" {
  run "$LEVER" "$BRIEF" "$OUT"
  [ "$status" -eq 0 ]
  [ ! -d "$OUT" ]
}

@test "SPEC_PANEL=off explicit is byte-identical to unset: no-op" {
  SPEC_PANEL=off run "$LEVER" "$BRIEF" "$OUT"
  [ "$status" -eq 0 ]
  [ ! -d "$OUT" ]
}

@test "spec-004 fix round finding 15: a pre-planted symlink at panel/ is refused, never written through" {
  mkdir -p "$WORK/outside-target"
  mkdir -p "$OUT"
  ln -s "$WORK/outside-target" "$OUT/panel"
  stub_vendor stub-claude CLAUDE_VOICE "$WORK/claude.stdin"
  ADVERSARY_BIN_CLAUDE=stub-claude SPEC_PANEL=on run "$LEVER" "$BRIEF" "$OUT"
  [ "$status" -ne 0 ]
  [[ "$output" == *"FATAL"* ]]
  [[ "$output" == *"symlink"* ]]
  [ ! -e "$WORK/outside-target/draft-anthropic.md" ]
  [ ! -e "$WORK/outside-target/synthesis.md" ]
}

@test "spec-004 fix round finding 15: a pre-planted symlink at an artifact path is refused" {
  mkdir -p "$OUT/panel"
  ln -s "$WORK/brief.txt" "$OUT/panel/record.json"
  stub_vendor stub-claude CLAUDE_VOICE "$WORK/claude.stdin"
  ADVERSARY_BIN_CLAUDE=stub-claude SPEC_PANEL=on run "$LEVER" "$BRIEF" "$OUT"
  [ "$status" -ne 0 ]
  [[ "$output" == *"FATAL"* ]]
  [[ "$output" == *"symlink"* ]]
  # the symlink's TARGET (brief.txt) must never have been overwritten
  grep -q "bound a retry on a flaky call" "$WORK/brief.txt"
}

@test "panel mode: two blind drafts with no cross-visibility, principal never drafts" {
  stub_vendor stub-claude CLAUDE_VOICE "$WORK/claude.stdin"
  stub_vendor stub-codex CODEX_VOICE "$WORK/codex.stdin"
  ADVERSARY_BIN_CLAUDE=stub-claude ADVERSARY_BIN_CODEX=stub-codex \
    SPEC_PANEL=on run "$LEVER" "$BRIEF" "$OUT"
  [ "$status" -eq 0 ]
  [ -f "$OUT/panel/draft-anthropic.md" ]
  [ -f "$OUT/panel/draft-openai.md" ]

  # Blind: neither dispatch's stdin (the prompt it received) carries the
  # other vendor's voice — each draft is an independent, non-inheriting
  # submission, not a review of the other's output.
  ! grep -q CODEX_VOICE "$WORK/claude.stdin"
  ! grep -q CLAUDE_VOICE "$WORK/codex.stdin"
  # Both received the SAME fixed brief (same brief, not divergent tasks).
  grep -q "bound a retry on a flaky call" "$WORK/claude.stdin"
  grep -q "bound a retry on a flaky call" "$WORK/codex.stdin"

  grep -q CLAUDE_VOICE "$OUT/panel/draft-anthropic.md"
  grep -q CODEX_VOICE "$OUT/panel/draft-openai.md"
  ! grep -q CODEX_VOICE "$OUT/panel/draft-anthropic.md"
  ! grep -q CLAUDE_VOICE "$OUT/panel/draft-openai.md"

  [ "$(jq -r .mode "$OUT/panel/record.json")" = panel ]
  [ "$(jq -r .relation "$OUT/panel/record.json")" = cross-vendor ]
}

@test "degrade pair: Claude-only reachable runs same-vendor author+refuter, relation self" {
  stub_vendor stub-claude CLAUDE_VOICE "$WORK/claude.stdin"
  ADVERSARY_BIN_CLAUDE=stub-claude SPEC_PANEL=on run "$LEVER" "$BRIEF" "$OUT"
  [ "$status" -eq 0 ]
  [ -f "$OUT/panel/draft-anthropic.md" ]
  [ -f "$OUT/panel/refute-anthropic.md" ]
  [ ! -f "$OUT/panel/draft-openai.md" ]
  grep -q REFUTE "$OUT/panel/refute-anthropic.md"
  ! grep -q REFUTE "$OUT/panel/draft-anthropic.md"

  [ "$(jq -r .mode "$OUT/panel/record.json")" = degrade ]
  [ "$(jq -r .vendor "$OUT/panel/record.json")" = anthropic ]
  [ "$(jq -r .relation "$OUT/panel/record.json")" = self ]
}

@test "degrade pair: Codex-only reachable runs same-vendor author+refuter, relation self" {
  stub_vendor stub-codex CODEX_VOICE "$WORK/codex.stdin"
  ADVERSARY_BIN_CODEX=stub-codex SPEC_PANEL=on run "$LEVER" "$BRIEF" "$OUT"
  [ "$status" -eq 0 ]
  [ -f "$OUT/panel/draft-openai.md" ]
  [ -f "$OUT/panel/refute-openai.md" ]
  [ ! -f "$OUT/panel/draft-anthropic.md" ]

  [ "$(jq -r .mode "$OUT/panel/record.json")" = degrade ]
  [ "$(jq -r .vendor "$OUT/panel/record.json")" = openai ]
  [ "$(jq -r .relation "$OUT/panel/record.json")" = self ]
}

@test "no vendor CLI reachable: fails closed, no synthesis written" {
  SPEC_PANEL=on run "$LEVER" "$BRIEF" "$OUT"
  [ "$status" -ne 0 ]
  [ ! -f "$OUT/panel/synthesis.md" ]
}

@test "artifact shape: synthesis.md grafts all four rubric axes with per-idea [voice: ...] attribution" {
  stub_vendor stub-claude CLAUDE_VOICE "$WORK/claude.stdin"
  stub_vendor stub-codex CODEX_VOICE "$WORK/codex.stdin"
  ADVERSARY_BIN_CLAUDE=stub-claude ADVERSARY_BIN_CODEX=stub-codex \
    SPEC_PANEL=on run "$LEVER" "$BRIEF" "$OUT"
  [ "$status" -eq 0 ]
  syn="$OUT/panel/synthesis.md"
  [ -s "$syn" ]
  for axis in "## Coverage" "## Assumptions" "## Failure modes" "## Testability"; do
    grep -qF "$axis" "$syn"
  done
  grep -q '\[voice: anthropic\]' "$syn"
  grep -q '\[voice: openai\]' "$syn"
}

@test "usage error: missing args exits non-zero with usage message" {
  SPEC_PANEL=on run "$LEVER"
  [ "$status" -eq 2 ]
  [[ "$output" == *"usage: spec-panel.sh"* ]]
}

@test "unreadable brief file fails closed" {
  SPEC_PANEL=on run "$LEVER" "$WORK/does-not-exist.txt" "$OUT"
  [ "$status" -ne 0 ]
  [ ! -d "$OUT" ]
}
