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

echo ""

# Copy skills
echo "Installing skills to .claude/skills/..."
for skill in fix feature-implement feature spec-decompose; do
  TARGET=".claude/skills/$skill/SKILL.md"
  if [ -f "$TARGET" ]; then
    read -p "  $TARGET exists. Overwrite? [y/N] " -n 1 -r
    echo
    [[ $REPLY =~ ^[Yy]$ ]] || continue
  fi
  mkdir -p ".claude/skills/$skill"
  cp "$SCRIPT_DIR/skills/$skill/SKILL.md" "$TARGET"
  echo "  Installed $TARGET"
done

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
echo "=== feature-fix-swarm installed ==="
echo ""
echo "Try it:"
echo "  /feature-implement NNN --qa-loop --dry-run    # see the QA plan"
echo "  /feature-implement NNN --qa-loop              # run with QA enforcement"
echo '  /fix "description of the bug"                 # investigate + fix + verify'
echo ""
echo "Docs: packages/feature-fix-swarm/docs/"
