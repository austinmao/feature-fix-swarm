#!/usr/bin/env bash
set -euo pipefail

echo "=== feature-fix-swarm setup ==="
echo ""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Non-interactive mode: --yes flag or FFS_SETUP_YES=1 env. Overwrites existing
# skill installs without prompting and skips the optional-deps prompt — needed
# for autonomous/CI deploys where stdin is not a TTY (read otherwise exits 1).
SETUP_YES="${FFS_SETUP_YES:-0}"
RECONCILE_TARGET=""
_setup_args=("$@")
for ((_i=0; _i<${#_setup_args[@]}; _i++)); do
  _a="${_setup_args[$_i]}"
  [ "$_a" = "--yes" ] || [ "$_a" = "-y" ] && SETUP_YES=1
  if [ "$_a" = "--reconcile-consumer" ]; then
    _i=$((_i + 1))
    [ "$_i" -lt "${#_setup_args[@]}" ] || { echo "ERROR: --reconcile-consumer requires a target directory" >&2; exit 2; }
    RECONCILE_TARGET="${_setup_args[$_i]}"
  fi
done
export PATH="$HOME/.local/bin:$PATH"

register_pretool_guard() {
  local config_path="$1" command_text="$2" guard_name="$3"
  mkdir -p "$(dirname "$config_path")"
  python3 - "$config_path" "$command_text" "$guard_name" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
command = sys.argv[2]
guard_name = sys.argv[3]
if path.exists():
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"refusing to overwrite malformed hook config {path}: {exc}")
else:
    data = {}
hooks = data.setdefault("hooks", {})
entries = hooks.setdefault("PreToolUse", [])
for item in entries:
    if item.get("matcher") == "Bash":
        item["hooks"] = [
            hook for hook in item.get("hooks", [])
            if guard_name not in hook.get("command", "")
        ]
entry = next((item for item in entries if item.get("matcher") == "Bash"), None)
if entry is None:
    entry = {"matcher": "Bash", "hooks": []}
    entries.append(entry)
commands = entry.setdefault("hooks", [])
commands.append({"type": "command", "command": command, "timeout": 10})
installed = [
    hook for item in entries if item.get("matcher") == "Bash"
    for hook in item.get("hooks", [])
    if guard_name in hook.get("command", "")
]
if installed != [{"type": "command", "command": command, "timeout": 10}]:
    raise SystemExit(f"failed to reconcile exact {guard_name} hook in {path}")
path.write_text(json.dumps(data, indent=2) + "\n")
PY
}

register_consumer_hooks() {
  local target="$1"
  local guard
  for guard in cli-hang-guard.sh credential-output-guard.sh; do
    register_pretool_guard "$target/.claude/settings.json" \
      "bash \"\$CLAUDE_PROJECT_DIR\"/scripts/hooks/$guard" "$guard"
    register_pretool_guard "$target/.codex/hooks.json" \
      "bash \"\$(git rev-parse --show-toplevel 2>/dev/null || pwd)/scripts/hooks/$guard\"" "$guard"
  done
}

verify_consumer_runtime() {
  local target="$1" rel
  for rel in \
    scripts/gsd/gsd-run.sh \
    scripts/gsd/requirement-ownership-gate.sh \
    scripts/gsd/adversary-host.sh \
    scripts/gsd/run-bounded.sh \
    scripts/hooks/cli-hang-guard.sh \
    scripts/hooks/credential-output-guard.sh; do
    cmp -s "$SCRIPT_DIR/$rel" "$target/$rel" || {
      echo "ERROR: consumer runtime drift remains after setup: $rel" >&2
      return 1
    }
  done
  echo "  Consumer runtime parity: VERIFIED"
}

reconcile_consumer_runtime() {
  local target="$1" rel
  [ -d "$target" ] || { echo "ERROR: reconciliation target is not a directory: $target" >&2; return 2; }
  for rel in \
    scripts/gsd/gsd-run.sh \
    scripts/gsd/requirement-ownership-gate.sh \
    scripts/gsd/adversary-host.sh \
    scripts/gsd/run-bounded.sh \
    scripts/gsd/model-equivalents.sh \
    scripts/gsd/codex-model-sync.sh \
    scripts/gsd/review-gate-command.sh \
    scripts/hooks/cli-hang-guard.sh \
    scripts/hooks/credential-output-guard.sh; do
    mkdir -p "$target/$(dirname "$rel")"
    if [ ! "$SCRIPT_DIR/$rel" -ef "$target/$rel" ]; then
      cp "$SCRIPT_DIR/$rel" "$target/$rel"
    fi
    chmod +x "$target/$rel"
  done
  register_consumer_hooks "$target"
  verify_consumer_runtime "$target"
}

if [ -n "$RECONCILE_TARGET" ]; then
  reconcile_consumer_runtime "$RECONCILE_TARGET"
  echo "=== feature-fix-swarm consumer runtime reconciled ==="
  exit 0
fi

skill_installed() {
  local skill="$1"
  local candidates=(
    "$HOME/.claude/skills/$skill/SKILL.md"
    "$HOME/.codex/skills/$skill/SKILL.md"
    "$HOME/.agents/skills/$skill/SKILL.md"
  )
  local path
  for path in "${candidates[@]}"; do
    [ -f "$path" ] && return 0
  done
  return 1
}

prompt_yes_no() {
  local prompt="$1"
  local response=""
  read -r -p "$prompt [y/N] " response || true
  case "${response,,}" in
    y|yes) return 0 ;;
    *) return 1 ;;
  esac
}

install_uv() {
  if command -v uv >/dev/null 2>&1; then
    echo "  uv: OK"
    return 0
  fi

  echo "  uv: installing via Astral standalone installer"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    echo "  uv: NOT FOUND (install manually; curl or wget is required for auto-install)"
    return 1
  fi
  export PATH="$HOME/.local/bin:$PATH"
}

check_gsd() {
  if [ -x "node_modules/.bin/gsd-tools" ]; then
    echo "  gsd-core: OK (node_modules/.bin/gsd-tools)"
    return 0
  fi
  echo "  gsd-core: NOT FOUND (pinned repo-local dep)"
  return 1
}

install_gsd() {
  if ! command -v npm >/dev/null 2>&1; then
    echo "  gsd-core: NOT FOUND (npm required)"
    return 1
  fi
  echo "  gsd-core: installing pinned repo-local dep"
  # NEVER bare `npx gsd` — that resolves to the wrong package (gsd@0.0.3)
  npm install --save-dev --save-exact @opengsd/gsd-core@1.6.1
}

install_gsd_surfaces() {
  if [ ! -x node_modules/.bin/gsd-core ]; then
    echo "  gsd-core: runtime surfaces not installed (repo-local binary missing)"
    return 1
  fi

  local installed=0 failed=0
  # node_modules/.bin/gsd-core is a shell shim, not JS — exec it directly.
  if command -v claude >/dev/null 2>&1; then
    installed=1
    node_modules/.bin/gsd-core --claude --global || failed=1
  fi
  if command -v codex >/dev/null 2>&1; then
    installed=1
    node_modules/.bin/gsd-core --codex --global || failed=1
    GSD_MODEL_CONFIG="$SCRIPT_DIR/templates/gsd-config.base.json" \
      bash "$SCRIPT_DIR/scripts/gsd/codex-model-sync.sh" "${CODEX_HOME:-$HOME/.codex}" || failed=1
  fi
  if [ "$installed" -eq 0 ]; then
    echo "  gsd-core: no supported host CLI found (install Claude Code or Codex)"
    return 1
  fi
  return "$failed"
}

check_gstack() {
  if skill_installed gstack; then
    echo "  gstack: OK"
    return 0
  fi
  echo "  gstack: NOT FOUND"
  return 1
}

install_gstack() {
  echo "  gstack: installing into ~/.claude/skills/gstack"
  mkdir -p "$HOME/.claude/skills"
  git clone https://github.com/garryslist/gstack.git "$HOME/.claude/skills/gstack"
}

install_skill_repo() {
  local repo="$1"
  shift
  if ! command -v npx >/dev/null 2>&1; then
    echo "  npx: NOT FOUND (required to install skills from $repo)"
    return 1
  fi
  local args=(skills add "$repo" -g -a claude-code -a codex -y)
  local skill
  for skill in "$@"; do
    args+=(--skill "$skill")
  done
  npx "${args[@]}"
}

check_spec_kit() {
  if command -v specify >/dev/null 2>&1; then
    echo "  Spec Kit CLI: OK"
    return 0
  fi
  echo "  Spec Kit CLI: NOT FOUND"
  return 1
}

install_spec_kit() {
  if ! command -v uv >/dev/null 2>&1; then
    install_uv || return 1
  fi

  if ! command -v uv >/dev/null 2>&1; then
    echo "  Spec Kit CLI: NOT FOUND (uv unavailable)"
    return 1
  fi

  local spec_tag=""
  if command -v gh >/dev/null 2>&1; then
    spec_tag=$(gh api repos/github/spec-kit/releases/latest --jq .tag_name 2>/dev/null || true)
  fi

  if [ -n "$spec_tag" ]; then
    echo "  Spec Kit CLI: installing latest release ${spec_tag}"
    uv tool install specify-cli --from "git+https://github.com/github/spec-kit.git@${spec_tag}"
  else
    echo "  Spec Kit CLI: installing from spec-kit main"
    uv tool install specify-cli --from git+https://github.com/github/spec-kit.git
  fi
}

check_prompt_master() {
  if skill_installed prompt-master; then
    echo "  prompt-master: OK"
    return 0
  fi
  echo "  prompt-master: NOT FOUND"
  return 1
}

install_prompt_master() {
  echo "  prompt-master: installing via npx skills"
  install_skill_repo https://github.com/nidhinjs/prompt-master prompt-master
}

check_goal_wrap() {
  if skill_installed goal-wrap && skill_installed handoff; then
    echo "  goal-wrap/handoff: OK"
    return 0
  fi
  echo "  goal-wrap/handoff: NOT FOUND"
  return 1
}

install_goal_wrap() {
  echo "  goal-wrap/handoff: installing via npx skills"
  install_skill_repo https://github.com/austinmao/goal-wrap goal-wrap handoff
}

# === external agent pack freshness checks (ECC, wshobson/agents) ===
PACK_STATE_DIR="$HOME/.feature-fix-swarm/install-state"
PACK_CACHE_DIR="$HOME/.feature-fix-swarm/install-cache"
ECC_REPO="https://github.com/affaan-m/ECC.git"
ECC_CACHE_DIR="$PACK_CACHE_DIR/ecc"
ECC_STATE_FILE="$PACK_STATE_DIR/ecc-main.sha"
WSH_REPO="https://github.com/wshobson/agents.git"
WSH_CACHE_DIR="$PACK_CACHE_DIR/wshobson-agents"
WSH_STATE_FILE="$PACK_STATE_DIR/wshobson-agents-main.sha"

remote_main_sha() {
  local repo="$1"
  git ls-remote --heads "$repo" main 2>/dev/null | awk 'NR==1 {print $1}'
}

read_trimmed_file() {
  local path="$1"
  if [ -f "$path" ]; then
    tr -d '[:space:]' < "$path"
  fi
}

write_state_file() {
  local path="$1"
  local value="$2"
  mkdir -p "$(dirname "$path")"
  printf '%s\n' "$value" > "$path"
}

install_ecc_pack() {
  if ! command -v git >/dev/null 2>&1; then
    echo "  ECC: NOT FOUND (git required to clone upstream)"
    return 1
  fi

  local upstream_sha
  upstream_sha="$(remote_main_sha "$ECC_REPO")"
  if [ -z "$upstream_sha" ]; then
    echo "  ECC: NOT FOUND (unable to resolve upstream main)"
    return 1
  fi

  echo "  ECC: syncing upstream main $upstream_sha"
  mkdir -p "$PACK_CACHE_DIR"
  if [ -d "$ECC_CACHE_DIR/.git" ] \
     && git -C "$ECC_CACHE_DIR" fetch --depth 1 origin main >/dev/null 2>&1 \
     && git -C "$ECC_CACHE_DIR" reset --hard FETCH_HEAD >/dev/null 2>&1; then
    :
  else
    # codex-gate (PR #11): a failed reset was previously swallowed by `|| true`,
    # leaving a stale/inconsistent working tree. Fall back to a fresh clone on
    # ANY failure in the incremental path (missing .git, fetch failure, or reset failure).
    rm -rf "$ECC_CACHE_DIR"
    git clone --depth 1 --branch main "$ECC_REPO" "$ECC_CACHE_DIR" >/dev/null 2>&1
  fi

  (cd "$ECC_CACHE_DIR" && bash ./install.sh --profile minimal --target claude)
  write_state_file "$ECC_STATE_FILE" "$upstream_sha"
}

install_wshobson_pack() {
  if ! command -v git >/dev/null 2>&1; then
    echo "  wshobson/agents: NOT FOUND (git required to resolve upstream)"
    return 1
  fi
  if ! command -v npx >/dev/null 2>&1; then
    echo "  wshobson/agents: NOT FOUND (npx required)"
    return 1
  fi

  local upstream_sha
  upstream_sha="$(remote_main_sha "$WSH_REPO")"
  if [ -z "$upstream_sha" ]; then
    echo "  wshobson/agents: NOT FOUND (unable to resolve upstream main)"
    return 1
  fi

  echo "  wshobson/agents: syncing upstream main $upstream_sha"
  mkdir -p "$PACK_CACHE_DIR"
  if [ -d "$WSH_CACHE_DIR/.git" ] \
     && git -C "$WSH_CACHE_DIR" fetch --depth 1 origin main >/dev/null 2>&1 \
     && git -C "$WSH_CACHE_DIR" reset --hard FETCH_HEAD >/dev/null 2>&1; then
    :
  else
    # codex-gate (PR #11): fall back to a fresh clone on ANY failure in the
    # incremental path (missing .git, fetch failure, or reset failure) —
    # a failed reset was previously swallowed by `|| true`.
    rm -rf "$WSH_CACHE_DIR"
    git clone --depth 1 --branch main "$WSH_REPO" "$WSH_CACHE_DIR" >/dev/null 2>&1
  fi

  # Run the marketplace install from this cached clone (fetched/reset or freshly
  # cloned above), not the caller's cwd, so it never writes stray project-relative
  # files into whichever repo the user happened to be in when setup.sh ran.
  (cd "$WSH_CACHE_DIR" && npx codex-marketplace add wshobson/agents)
  write_state_file "$WSH_STATE_FILE" "$upstream_sha"
}

check_ecc_pack() {
  if ! command -v git >/dev/null 2>&1; then
    echo "  ECC: NOT FOUND"
    return 1
  fi

  local upstream_sha recorded_sha
  upstream_sha="$(remote_main_sha "$ECC_REPO")"
  if [ -z "$upstream_sha" ]; then
    echo "  ECC: NOT FOUND"
    return 1
  fi

  recorded_sha="$(read_trimmed_file "$ECC_STATE_FILE")"
  if [ "$recorded_sha" = "$upstream_sha" ] && skill_installed tdd-guide; then
    echo "  ECC: OK (upstream main $upstream_sha)"
    return 0
  fi

  if [ -n "$recorded_sha" ]; then
    echo "  ECC: OUTDATED (installed $recorded_sha, upstream main $upstream_sha)"
  else
    echo "  ECC: NOT FOUND"
  fi
  return 1
}

check_wshobson_pack() {
  if ! command -v git >/dev/null 2>&1; then
    echo "  wshobson/agents: NOT FOUND"
    return 1
  fi

  local upstream_sha recorded_sha
  upstream_sha="$(remote_main_sha "$WSH_REPO")"
  if [ -z "$upstream_sha" ]; then
    echo "  wshobson/agents: NOT FOUND"
    return 1
  fi

  recorded_sha="$(read_trimmed_file "$WSH_STATE_FILE")"
  if [ "$recorded_sha" = "$upstream_sha" ] && skill_installed backend-architect; then
    echo "  wshobson/agents: OK (upstream main $upstream_sha)"
    return 0
  fi

  if [ -n "$recorded_sha" ]; then
    echo "  wshobson/agents: OUTDATED (installed $recorded_sha, upstream main $upstream_sha)"
  else
    echo "  wshobson/agents: NOT FOUND"
  fi
  return 1
}

missing=()

echo "Checking prerequisites..."
if command -v claude >/dev/null 2>&1; then
  echo "  Claude Code: OK"
else
  echo "  Claude Code: NOT FOUND (optional host; required for opposite-host review from Codex)"
fi
if command -v codex >/dev/null 2>&1; then
  echo "  Codex CLI: OK"
else
  echo "  Codex CLI: NOT FOUND (optional host; required for opposite-host review from Claude)"
fi
if ! command -v claude >/dev/null 2>&1 && ! command -v codex >/dev/null 2>&1; then
  missing+=("Claude Code or Codex CLI")
fi
check_gstack || missing+=("gstack")
check_gsd || missing+=("gsd-core")
command -v npx >/dev/null 2>&1 && echo "  npx: OK" || { echo "  npx: NOT FOUND (required for skill installs)"; missing+=("npx"); }
command -v uv >/dev/null 2>&1 && echo "  uv: OK" || { echo "  uv: NOT FOUND (required for Spec Kit)"; missing+=("uv"); }
command -v python3 >/dev/null 2>&1 && echo "  python3: OK" || echo "  python3: NOT FOUND (required for run-state)"
command -v jq >/dev/null 2>&1 && echo "  jq: OK" || echo "  jq: NOT FOUND (optional, used by some run-state output formatting)"
check_spec_kit || missing+=("spec-kit")
check_prompt_master || missing+=("prompt-master")
check_goal_wrap || missing+=("goal-wrap")
check_ecc_pack || missing+=("ECC")
check_wshobson_pack || missing+=("wshobson/agents")

echo ""

if [ "${#missing[@]}" -gt 0 ]; then
  echo "Missing dependencies detected:"
  printf '  - %s\n' "${missing[@]}"
  if prompt_yes_no "Install the missing dependencies now?" "N"; then
    echo ""
    echo "Bootstrapping missing dependencies..."
    install_uv || true
    install_gstack || true
    install_gsd || true
    install_spec_kit || true
    install_prompt_master || true
    install_goal_wrap || true
    install_ecc_pack || true
    install_wshobson_pack || true
  else
    echo "Skipping dependency installation. You can re-run setup.sh later."
  fi
  echo ""
fi

# Reconcile both installed host surfaces even when the npm dependency was
# already present. This closes the old check_gsd gap where a Claude-only prior
# install was reported as healthy inside a Codex session.
install_gsd_surfaces || echo "  WARN: one or more GSD host surfaces failed to reconcile"

# Copy the FFS skills into both supported host roots. Installing only the
# Claude copy made the advertised Codex runtime depend on consumer-specific
# symlinks and was the other half of the host leak.
for SKILLS_DIR in "$HOME/.claude/skills" "${CODEX_HOME:-$HOME/.codex}/skills"; do
  echo "Installing skills to $SKILLS_DIR/..."
  for skill in fix feature-implement feature feature-spec spec-decompose plan-decompose review-gate goal-wrap verify-review adopt-wip preflight autonomy-grant task-swarm code-uplift testing-policy; do
    TARGET="$SKILLS_DIR/$skill/SKILL.md"
    if [ -f "$TARGET" ] && [ "$SETUP_YES" != "1" ]; then
      read -p "  $TARGET exists. Overwrite? [y/N] " -n 1 -r
      echo
      [[ $REPLY =~ ^[Yy]$ ]] || continue
    fi
    mkdir -p "$SKILLS_DIR/$skill"
    cp "$SCRIPT_DIR/skills/$skill/SKILL.md" "$TARGET"
    echo "  Installed $TARGET"
  done

  # swarm skill: symlink to package canonical (edits in one place propagate everywhere)
  SWARM_TARGET="$SKILLS_DIR/swarm/SKILL.md"
  SWARM_SOURCE="$SCRIPT_DIR/skills/swarm/SKILL.md"
  mkdir -p "$SKILLS_DIR/swarm"
  rm -f "$SWARM_TARGET"
  ln -s "$SWARM_SOURCE" "$SWARM_TARGET"
  echo "  Symlinked $SWARM_TARGET -> $SWARM_SOURCE"
done

# v3.19: agent roster manifest — best-effort scan so a fresh install ships one.
# Target repo = FFS_SCAN_REPO if set, else cwd (skipped when cwd is this repo
# itself and has no roster — scan there is just the generic floor, not useful).
if [ -f "$SCRIPT_DIR/lib/agents_manifest.py" ]; then
  SCAN_REPO="${FFS_SCAN_REPO:-$PWD}"
  if [ "$SCAN_REPO" != "$SCRIPT_DIR" ] || [ -d "$SCAN_REPO/.claude/agents" ]; then
    if python3 "$SCRIPT_DIR/lib/agents_manifest.py" scan --repo "$SCAN_REPO" \
        --out "$SCAN_REPO/.feature-fix-swarm/agents.json" >/dev/null 2>&1; then
      echo "  Agent roster -> $SCAN_REPO/.feature-fix-swarm/agents.json (refresh: python3 lib/agents_manifest.py scan)"
    else
      echo "  Agent roster scan failed — rerun: python3 lib/agents_manifest.py scan --repo <repo> --out <repo>/.feature-fix-swarm/agents.json"
    fi
  else
    echo "  Build the roster in your target repo: python3 lib/agents_manifest.py scan --repo . --out .feature-fix-swarm/agents.json"
  fi
fi

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

# dispatch.py (task-parsing/routing/cost-estimate library used by feature-implement) —
# codex-gate (PR #11): standalone installs never had this installed anywhere, so the
# skill's hardcoded openclaw-monorepo path silently broke task parsing outside openclaw.
echo ""
echo "Installing dispatch.py to $LIB_DIR/feature-fix-swarm/..."
mkdir -p "$LIB_DIR/feature-fix-swarm"
cp "$SCRIPT_DIR/lib/dispatch.py" "$LIB_DIR/feature-fix-swarm/dispatch.py"
echo "  Installed $LIB_DIR/feature-fix-swarm/dispatch.py"
cp "$SCRIPT_DIR/lib/gates.py" "$LIB_DIR/feature-fix-swarm/gates.py"
echo "  Installed $LIB_DIR/feature-fix-swarm/gates.py"
cp "$SCRIPT_DIR/lib/agents_manifest.py" "$LIB_DIR/feature-fix-swarm/agents_manifest.py"
cp "$SCRIPT_DIR/lib/runtime_proof.py" "$LIB_DIR/feature-fix-swarm/runtime_proof.py"
echo "  Installed $LIB_DIR/feature-fix-swarm/agents_manifest.py (v3.19 agent roster)"
cp "$SCRIPT_DIR/hooks/tdd-gate.sh" "$LIB_DIR/feature-fix-swarm/tdd-gate.sh"
chmod +x "$LIB_DIR/feature-fix-swarm/tdd-gate.sh"
echo "  Installed $LIB_DIR/feature-fix-swarm/tdd-gate.sh (opt-in PreToolUse hook — see hooks/tdd-gate.sh header)"

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
for script in qa-swarm.sh ralph-retry.sh browser-proof.sh harness-audit.py; do
  mkdir -p scripts
  # self-copy guard: running setup.sh from the repo root makes source and
  # target the same file — cp errors "are identical" and kills the script
  if [ "$SCRIPT_DIR/scripts/$script" -ef "scripts/$script" ]; then
    echo "  scripts/$script is the repo copy — skipping self-copy"
  else
    cp "$SCRIPT_DIR/scripts/$script" "scripts/$script"
    chmod +x "scripts/$script"
    echo "  Installed scripts/$script"
  fi
done

mkdir -p scripts/gsd scripts/hooks
for gsd_script in gsd/gsd-run.sh gsd/requirement-ownership-gate.sh gsd/gates-test-command.sh gsd/review-gate-command.sh gsd/consent-check.sh gsd/state-phase.sh gsd/mempalace gsd/plan-adversary.sh gsd/canary-gate.sh gsd/qa-coverage-adversary.sh gsd/adversary-host.sh gsd/run-bounded.sh gsd/security-surface.sh gsd/review-tier.sh gsd/liveness-check.sh gsd/learnings-harvest.sh gsd/model-equivalents.sh gsd/codex-model-sync.sh gsd/model-fallback.sh gsd/security-model-fence.sh gsd/assert-merged.sh hooks/gsd-phase-evidence-gate.sh; do
  if [ "$SCRIPT_DIR/scripts/$gsd_script" -ef "scripts/$gsd_script" ]; then
    echo "  scripts/$gsd_script is the repo copy — skipping self-copy"
  else
    cp "$SCRIPT_DIR/scripts/$gsd_script" "scripts/$gsd_script"
    chmod +x "scripts/$gsd_script"
    echo "  Installed scripts/$gsd_script"
  fi
done

mkdir -p scripts/hooks
for hook in worktree-gc.sh post-implement-batch.sh delegation-enforcer.sh cli-hang-guard.sh credential-output-guard.sh; do
  if [ "$SCRIPT_DIR/scripts/hooks/$hook" -ef "scripts/hooks/$hook" ]; then
    echo "  scripts/hooks/$hook is the repo copy — skipping self-copy"
  else
    cp "$SCRIPT_DIR/scripts/hooks/$hook" "scripts/hooks/$hook"
    chmod +x "scripts/hooks/$hook"
    echo "  Installed scripts/hooks/$hook"
  fi
done

# Setup is a reconciliation operation, not a copy-if-missing install. Verify
# the host runner/adapter/bound/hook bytes and register the enforcement hook
# for both supported harnesses so a legacy consumer cannot silently persist.
verify_consumer_runtime "$PWD"
register_consumer_hooks "$PWD"

# Copy prompts
echo "Installing prompts..."
mkdir -p prompts
for prompt in qa-e2e.md qa-review.md qa-security.md qa-design.md decompose-spec.md; do
  if [ "$SCRIPT_DIR/prompts/$prompt" -ef "prompts/$prompt" ]; then
    echo "  prompts/$prompt is the repo copy — skipping self-copy"
  else
    cp "$SCRIPT_DIR/prompts/$prompt" "prompts/$prompt"
    echo "  Installed prompts/$prompt"
  fi
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
