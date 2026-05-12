#!/usr/bin/env bash
set -euo pipefail

echo "=== feature-fix-swarm setup ==="
echo ""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Check prerequisites
echo "Checking prerequisites..."
command -v claude >/dev/null 2>&1 && echo "  Claude Code: OK" || echo "  Claude Code: NOT FOUND (required) — install from https://claude.ai/code"

if [ -d "$HOME/.claude/skills/gstack" ]; then
  echo "  gstack: OK"
else
  echo "  gstack: NOT FOUND (required for /investigate, /qa, /review, /ship)"
  echo "    Install: https://github.com/garryslist/gstack"
fi

command -v npx >/dev/null 2>&1 && echo "  npx: OK" || echo "  npx: NOT FOUND (needed for ruflo)"
command -v python3 >/dev/null 2>&1 && echo "  python3: OK" || echo "  python3: NOT FOUND (required for run-state)"
command -v jq >/dev/null 2>&1 && echo "  jq: OK" || echo "  jq: NOT FOUND (optional, used by some run-state output formatting)"
command -v codex >/dev/null 2>&1 && echo "  codex CLI: OK" || echo "  codex CLI: NOT FOUND (optional, for adversarial audit — 'npm install -g @openai/codex')"

echo ""

# Copy skills
SKILLS_DIR="$HOME/.claude/skills"
echo "Installing skills to $SKILLS_DIR/..."
for skill in fix feature-implement feature spec-decompose; do
  TARGET="$SKILLS_DIR/$skill/SKILL.md"
  if [ -f "$TARGET" ]; then
    read -p "  $TARGET exists. Overwrite? [y/N] " -n 1 -r
    echo
    [[ $REPLY =~ ^[Yy]$ ]] || continue
  fi
  mkdir -p "$SKILLS_DIR/$skill"
  cp "$SCRIPT_DIR/skills/$skill/SKILL.md" "$TARGET"
  echo "  Installed $TARGET"
done

# === v2.0: run-state library (global ~/.claude/) ===
LIB_DIR="$HOME/.claude/lib"
echo ""
echo "Installing run-state library to $LIB_DIR/run_state/..."
mkdir -p "$LIB_DIR"
if [ -d "$LIB_DIR/run_state" ]; then
  echo "  $LIB_DIR/run_state exists — overwriting (preserving ~/.claude/state/)"
  rm -rf "$LIB_DIR/run_state"
fi
cp -R "$SCRIPT_DIR/lib/run_state" "$LIB_DIR/run_state"
find "$LIB_DIR/run_state" -type d \( -name __pycache__ -o -name .pytest_cache \) -exec rm -rf {} + 2>/dev/null || true
echo "  Installed $LIB_DIR/run_state/"

BIN_DIR="$HOME/.claude/bin"
mkdir -p "$BIN_DIR"
cp "$SCRIPT_DIR/bin/run-state" "$BIN_DIR/run-state"
chmod +x "$BIN_DIR/run-state"
echo "  Installed $BIN_DIR/run-state"

mkdir -p "$HOME/.claude/state"
echo "  Ensured $HOME/.claude/state/"

# v3.0 codex-gate Pass 2 #2 fix: actively remove stale v2.x Stop/SessionStart
# hook registrations from ~/.claude/settings.json. Leaving them fights the new
# native /goal lifecycle (Stop hook would still try to inject continuation
# prompts from a hook file we just deleted). Also remove orphaned hook scripts.
SETTINGS="$HOME/.claude/settings.json"
if [ -f "$SETTINGS" ] && command -v jq >/dev/null 2>&1; then
  if jq -e '.hooks // empty | (.Stop // []) + (.SessionStart // []) | map(.hooks[]?.command) | flatten | any(test("run-state-(stop|session)\\.py"))' "$SETTINGS" >/dev/null 2>&1; then
    echo "  Detected stale v2.x run-state hooks in $SETTINGS — removing..."
    TMP=$(mktemp)
    jq '
      .hooks.Stop |= (
        if . == null then null
        else map(select((.hooks // []) | map(.command // "") | any(test("run-state-stop\\.py")) | not))
        end
      ) |
      .hooks.SessionStart |= (
        if . == null then null
        else map(select((.hooks // []) | map(.command // "") | any(test("run-state-session\\.py")) | not))
        end
      )
    ' "$SETTINGS" > "$TMP" && mv "$TMP" "$SETTINGS"
    echo "  Removed v2.x hook registrations"
  fi
fi
# Remove orphaned hook script files left by v2.x install
for old_hook in run-state-stop.py run-state-session.py; do
  if [ -f "$HOME/.claude/hooks/$old_hook" ]; then
    rm -f "$HOME/.claude/hooks/$old_hook"
    echo "  Removed stale $HOME/.claude/hooks/$old_hook"
  fi
done
# Remove orphaned marker file
[ -f "$HOME/.claude/state/.active-run" ] && rm -f "$HOME/.claude/state/.active-run" && echo "  Removed stale .active-run marker"
echo ""

# Copy scripts
echo "Installing scripts..."
for script in qa-swarm.sh ralph-retry.sh; do
  mkdir -p scripts
  cp "$SCRIPT_DIR/scripts/$script" "scripts/$script"
  chmod +x "scripts/$script"
  echo "  Installed scripts/$script"
done

mkdir -p scripts/harness
cp "$SCRIPT_DIR/scripts/harness/executor-detect.sh" "scripts/harness/executor-detect.sh"
chmod +x "scripts/harness/executor-detect.sh"
echo "  Installed scripts/harness/executor-detect.sh"

mkdir -p scripts/hooks
for hook in worktree-gc.sh post-implement-batch.sh; do
  cp "$SCRIPT_DIR/scripts/hooks/$hook" "scripts/hooks/$hook"
  chmod +x "scripts/hooks/$hook"
  echo "  Installed scripts/hooks/$hook"
done

# Copy prompts
echo "Installing prompts..."
mkdir -p prompts
for prompt in qa-e2e.md qa-review.md qa-security.md decompose-spec.md; do
  cp "$SCRIPT_DIR/prompts/$prompt" "prompts/$prompt"
  echo "  Installed prompts/$prompt"
done

# Update .gitignore
if ! grep -q '.feature-fix-swarm/' .gitignore 2>/dev/null; then
  echo '.feature-fix-swarm/' >> .gitignore
  echo "  Added .feature-fix-swarm/ to .gitignore"
fi

echo ""
echo "Smoke testing run-state CLI..."
if "$BIN_DIR/run-state" list >/dev/null 2>&1; then
  echo "  run-state CLI: OK"
else
  echo "  run-state CLI: FAILED — investigate $BIN_DIR/run-state"
fi

echo ""
echo "=== feature-fix-swarm installed ==="
echo ""
echo "Try it:"
echo "  /feature-implement NNN --qa-loop --dry-run    # see the QA plan"
echo "  /feature-implement NNN --qa-loop              # run with QA enforcement"
echo '  /fix "description of the bug"                 # investigate + fix + verify'
echo ""
echo "Run-state CLI:"
echo "  ~/.claude/bin/run-state list                  # show all runs"
echo "  ~/.claude/bin/run-state list --state active   # currently-active"
echo "  ~/.claude/bin/run-state status <run_id>       # detailed status"
echo ""
echo "v3.0 uses Claude's native /goal command for continuation loops."
echo "  Start a long-running pipeline with:"
echo '    /goal "<condition that should hold when work is done>"'
echo "  See https://code.claude.com/docs/en/goal"
echo ""
echo "Docs: ./docs/ + ./lib/run_state/README.md"
