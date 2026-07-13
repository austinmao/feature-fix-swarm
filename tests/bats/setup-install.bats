#!/usr/bin/env bats
# setup.sh installer manifest — static assertions that the installer ships
# every skill + gsd script the doctrine references. A skill/script that works
# in this repo but never installs into consumer repos is a silent gap.
# ponytail: grep-based static checks, not an install e2e — upgrade to a
# sandboxed-run test if the installer grows conditional logic per entry.

setup() {
  cd "$BATS_TEST_DIRNAME/../.."
}

@test "setup.sh skill manifest includes code-uplift and testing-policy" {
  SKILL_LINE="$(grep '^[[:space:]]*for skill in ' setup.sh)"
  [[ "$SKILL_LINE" == *"code-uplift"* ]]
  [[ "$SKILL_LINE" == *"testing-policy"* ]]
}

@test "setup.sh gsd-script manifest includes the adversary + canary scripts" {
  GSD_LINE="$(grep '^for gsd_script in ' setup.sh)"
  for s in plan-adversary.sh canary-gate.sh qa-coverage-adversary.sh adversary-host.sh; do
    [[ "$GSD_LINE" == *"gsd/$s"* ]]
  done
}

@test "setup.sh gsd-script manifest includes the v4.5.0 orchestration-hardening levers (INT-003)" {
  GSD_LINE="$(grep '^for gsd_script in ' setup.sh)"
  for s in security-surface.sh review-tier.sh liveness-check.sh learnings-harvest.sh; do
    [[ "$GSD_LINE" == *"gsd/$s"* ]]
  done
}

@test "setup.sh hook manifest includes delegation-enforcer.sh (INT-003)" {
  HOOK_LINE="$(grep '^for hook in ' setup.sh)"
  [[ "$HOOK_LINE" == *"delegation-enforcer.sh"* ]]
}

@test "setup.sh gsd-script manifest includes model-equivalents.sh (spec 004 cross-vendor routing)" {
  GSD_LINE="$(grep '^for gsd_script in ' setup.sh)"
  [[ "$GSD_LINE" == *"gsd/model-equivalents.sh"* ]]
}

@test "setup.sh installs GSD for both Claude and Codex hosts" {
  grep -F -- '--claude --global' setup.sh
  grep -F -- '--codex --global' setup.sh
}

@test "setup.sh installs FFS skills into both host roots" {
  grep -F '"$HOME/.claude/skills" "${CODEX_HOME:-$HOME/.codex}/skills"' setup.sh
}

@test "setup.sh installs and invokes the Codex model materializer" {
  GSD_LINE="$(grep '^for gsd_script in ' setup.sh)"
  [[ "$GSD_LINE" == *"gsd/codex-model-sync.sh"* ]]
  grep -F 'codex-model-sync.sh' setup.sh
}

@test "setup.sh gsd-script manifest includes model-fallback.sh + security-model-fence.sh (feature-implement Step 2 deps)" {
  GSD_LINE="$(grep '^for gsd_script in ' setup.sh)"
  [[ "$GSD_LINE" == *"gsd/model-fallback.sh"* ]]
  [[ "$GSD_LINE" == *"gsd/security-model-fence.sh"* ]]
}

@test "setup.sh gsd-script manifest includes assert-merged.sh (spec 005 merge backstop)" {
  GSD_LINE="$(grep '^for gsd_script in ' setup.sh)"
  [[ "$GSD_LINE" == *"gsd/assert-merged.sh"* ]]
}

@test "setup.sh installs and reconciles requirement-ownership-gate.sh" {
  GSD_LINE="$(grep '^for gsd_script in ' setup.sh)"
  [[ "$GSD_LINE" == *"gsd/requirement-ownership-gate.sh"* ]]
  grep -F 'scripts/gsd/requirement-ownership-gate.sh' setup.sh
}

@test "setup.sh script manifest includes harness-audit.py (INT-003)" {
  SCRIPT_LINE="$(grep '^for script in ' setup.sh)"
  [[ "$SCRIPT_LINE" == *"harness-audit.py"* ]]
}

@test "setup reconciliation overwrites legacy consumer runtime with exact host-native files" {
  TARGET="$BATS_TEST_TMPDIR/consumer"
  mkdir -p "$TARGET/scripts/gsd" "$TARGET/.claude" "$TARGET/.codex"
  printf 'legacy runner\n' > "$TARGET/scripts/gsd/gsd-run.sh"
  printf 'legacy adversary\n' > "$TARGET/scripts/gsd/adversary-host.sh"
  printf 'legacy ownership gate\n' > "$TARGET/scripts/gsd/requirement-ownership-gate.sh"
  printf '{}\n' > "$TARGET/.claude/settings.json"
  printf '{}\n' > "$TARGET/.codex/hooks.json"

  run env HOME="$BATS_TEST_TMPDIR/home" bash setup.sh --reconcile-consumer "$TARGET"

  [ "$status" -eq 0 ]
  cmp -s scripts/gsd/gsd-run.sh "$TARGET/scripts/gsd/gsd-run.sh"
  cmp -s scripts/gsd/adversary-host.sh "$TARGET/scripts/gsd/adversary-host.sh"
  cmp -s scripts/gsd/run-bounded.sh "$TARGET/scripts/gsd/run-bounded.sh"
  cmp -s scripts/gsd/requirement-ownership-gate.sh "$TARGET/scripts/gsd/requirement-ownership-gate.sh"
  cmp -s scripts/hooks/cli-hang-guard.sh "$TARGET/scripts/hooks/cli-hang-guard.sh"
  python3 - "$TARGET" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
for path in (root / ".claude/settings.json", root / ".codex/hooks.json"):
    data = json.loads(path.read_text())
    hooks = data["hooks"]["PreToolUse"]
    assert any(entry.get("matcher") == "Bash" and any(
        "cli-hang-guard.sh" in hook.get("command", "")
        for hook in entry.get("hooks", [])
    ) for entry in hooks), path
PY
}
