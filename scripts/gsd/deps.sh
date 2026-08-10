#!/usr/bin/env bash
# deps.sh — the single executable dependency roster for FFS.
#
# usage: deps.sh <check|install> [--json] [--optional] [--yes]
#   check    probe every roster entry; print present/MISSING with a remedy per
#            row; exit 1 when any REQUIRED entry is missing (0 otherwise)
#   install  repo-scoped installs ONLY: `npm ci` (only when @opengsd/gsd-core
#            is absent or off-version) and `python3 -m pip install --requirement
#            requirements-dev.txt` (only when the filelock floor probe fails;
#            --optional installs the full contributor set unconditionally).
#            NEVER runs a system package manager, never sudo. System tools are
#            reported with their exact brew/apt command instead.
# flags:
#   --json      (check) emit a JSON array instead of text rows
#   --optional  (install) also install contributor tooling (pytest, bandit)
#   --yes       (install) skip the npm-ci confirmation prompt
# exit codes: 0 ok · 1 required-missing / usage · 2 install failure
#
# The roster below is the source of truth the docs are tested against
# (tests/test_docs_dependency_roster.py): every REQUIRED row's name must
# appear in docs/dependencies.md.
set -euo pipefail

fail() {
  printf 'DEPS: %s\n' "$1" >&2
  exit "${2:-1}"
}

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || fail "not inside a git repository"
GSD_VERSION="1.10.0"

# roster rows: name|kind|required|remedy
# kinds: binary (command -v; comma = any-of), npm, pip, pin
ROSTER='python3|binary|required|install Python 3.11+ (brew install python@3.12 / apt install python3)
node|binary|required|install Node.js 22+ (brew install node / apt install nodejs)
npm|binary|required|install npm 10+ (ships with Node.js 22+)
git|binary|required|install git (brew install git / apt install git)
claude,codex|binary|required|install Claude Code (https://docs.anthropic.com/en/docs/claude-code) or Codex CLI (https://github.com/openai/codex)
gh|binary|required|brew install gh / apt install gh — required by the ship/finalize tail
jq|binary|required|brew install jq / apt install jq — required by the canary gate
shasum,sha256sum|binary|required|part of perl (macOS) or coreutils (apt install coreutils)
ps|binary|required|part of the base system (procps on minimal Linux images)
@opengsd/gsd-core|npm|required|run: deps.sh install (npm ci from the committed lockfile)
filelock|pip|required|run: deps.sh install (python3 -m pip install --requirement requirements-dev.txt)
prompt-master|pin|required|bash setup.sh --scope user (or --scope project --project-dir <repo>)
socratic|pin|required|bash setup.sh --scope user (skippable with FFS_SKIP_SOCRATIC=1)
timeout,gtimeout|binary|optional|brew install coreutils / apt install coreutils — python3 fallback exists
flock|binary|optional|apt install util-linux — shell fallback exists on macOS
curl|binary|optional|brew install curl / apt install curl — browser-proof readiness probe
tmux|binary|optional|brew install tmux / apt install tmux — worktree-GC session check
bats|binary|optional|brew install bats-core / apt install bats — contributor test suite
shellcheck|binary|optional|brew install shellcheck / apt install shellcheck — contributor lint
pytest|binary|optional|deps.sh install --optional — contributor test runner
npx|binary|optional|ships with npm — JS/TS QA lane (vitest)
playwright|binary|optional|npm install -g playwright — browser-proof QA lane
canary|binary|optional|npm install -g @usecanary/cli — browser QA recordings
slopcheck|binary|optional|optional package-legitimacy verdict; absent degrades to [ASSUMED]
gbrain|binary|optional|optional workspace memory; absent is fail-soft everywhere'

probe_binary() { # any-of comma list
  local IFS=','
  local candidate
  for candidate in $1; do
    if command -v "$candidate" >/dev/null 2>&1; then
      return 0
    fi
  done
  return 1
}

probe_npm() {
  [ "$(python3 - "$REPO_ROOT" <<'PY' 2>/dev/null
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]) / "node_modules/@opengsd/gsd-core/package.json"
try:
    print(json.loads(p.read_text()).get("version", ""))
except OSError:
    print("")
PY
)" = "$GSD_VERSION" ]
}

probe_pip() {
  # symbol-presence floor probe, same contract as scripts/coord/coord.py
  python3 -c 'import filelock; assert hasattr(filelock, "SoftFileLease")' >/dev/null 2>&1
}

probe_pin() {
  local name="$1" dir
  for dir in "$REPO_ROOT/.agents/skills/$name" "$HOME/.agents/skills/$name" "$HOME/.claude/skills/$name"; do
    if [ -f "$dir/.ffs-$name.json" ] || [ -f "$dir/SKILL.md" ]; then
      return 0
    fi
  done
  return 1
}

probe() { # $1=name $2=kind
  case "$2" in
    binary) probe_binary "$1" ;;
    npm)    probe_npm ;;
    pip)    probe_pip ;;
    pin)    probe_pin "$1" ;;
    *)      return 1 ;;
  esac
}

cmd_check() {
  local as_json=0 missing_required=0 first=1
  local name kind required remedy status
  [ "${1:-}" = "--json" ] && as_json=1
  [ "$as_json" -eq 1 ] && printf '['
  while IFS='|' read -r name kind required remedy; do
    [ -z "$name" ] && continue
    if probe "$name" "$kind"; then status=ok; else status=missing; fi
    if [ "$status" = missing ] && [ "$required" = required ]; then
      missing_required=1
    fi
    if [ "$as_json" -eq 1 ]; then
      [ "$first" -eq 0 ] && printf ','
      first=0
      python3 -c 'import json, sys; print(json.dumps({"name": sys.argv[1], "kind": sys.argv[2], "required": sys.argv[3] == "required", "status": sys.argv[4], "remedy": sys.argv[5]}), end="")' \
        "$name" "$kind" "$required" "$status" "$remedy"
    elif [ "$status" = ok ]; then
      printf 'ok       %s\n' "$name"
    else
      printf 'MISSING  %s (%s) — remedy: %s\n' "$name" "$required" "$remedy"
    fi
  done <<< "$ROSTER"
  [ "$as_json" -eq 1 ] && printf ']\n'
  if [ "$missing_required" -eq 1 ]; then
    [ "$as_json" -eq 0 ] && printf 'DEPS: required dependency missing — run the remedies above, then re-run deps.sh check\n' >&2
    return 1
  fi
  return 0
}

cmd_install() {
  local with_optional=0 assume_yes=0 arg
  for arg in "$@"; do
    case "$arg" in
      --optional) with_optional=1 ;;
      --yes)      assume_yes=1 ;;
      *)          fail "unknown install flag $arg" ;;
    esac
  done

  probe_binary npm || fail "npm is not installed — install Node.js 22+ first (deps.sh cannot install system tools)"
  probe_binary python3 || fail "python3 is not installed — install Python 3.11+ first (deps.sh cannot install system tools)"

  if probe_npm; then
    printf 'ok       @opengsd/gsd-core %s already installed\n' "$GSD_VERSION"
  else
    if [ "$assume_yes" -ne 1 ]; then
      printf 'about to run: npm ci (replaces node_modules from the committed lockfile). Continue? [y/N] '
      local answer
      read -r answer
      case "$answer" in y|Y|yes|YES) ;; *) fail "npm ci declined — re-run with --yes to skip this prompt" ;; esac
    fi
    (cd "$REPO_ROOT" && npm ci) || fail "npm ci failed" 2
    probe_npm || fail "npm ci completed but @opengsd/gsd-core $GSD_VERSION is still not resolvable" 2
    printf 'installed @opengsd/gsd-core %s via npm ci\n' "$GSD_VERSION"
  fi

  if [ "$with_optional" -eq 1 ] || ! probe_pip; then
    (cd "$REPO_ROOT" && python3 -m pip install --quiet --requirement requirements-dev.txt) \
      || fail "pip install --requirement requirements-dev.txt failed" 2
    probe_pip || fail "pip install completed but the filelock >=3.30 floor probe still fails" 2
    printf 'installed python requirements (requirements-dev.txt)\n'
  else
    printf 'ok       filelock floor probe passes; pip install skipped (use --optional for the full contributor set)\n'
  fi

  printf 'note: system tools are never installed by this script — run deps.sh check for per-tool commands\n'
  return 0
}

case "${1:-}" in
  check)   shift; cmd_check "$@" ;;
  install) shift; cmd_install "$@" ;;
  *)       sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
