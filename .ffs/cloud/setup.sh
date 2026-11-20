#!/usr/bin/env bash
# FFS cloud provisioning (runs inside a provider cloud VM; bash 3.2+ safe).
# Idempotent: stamps $HOME/.ffs-cloud-setup.<sha256 of this script>.
# Never writes a secret to disk, never uses set -x, never prints env values.
#
# SECRET ISOLATION LIMIT (status JSON: "secret_isolation":"env-i-only").
# The re-exec below gives installers a clean ENVIRONMENT only. It does NOT hide
# tokens from the installers: on Linux an installer (npm/pip lifecycle scripts run
# here) can read /proc/<pid>/environ of every same-uid ancestor, for example the
# Claude Code process that started this hook, and that process still holds the
# provider tokens. A parent cannot unset its own environment before this re-exec,
# and `setsid`/double forks only change the session, not /proc readability.
# Treat every dependency installed here as able to read the cloud session's
# secrets; the real boundary is the provider VM and the secrets you expose to it.
set -u

# Pinned secret-provider CLI. Fill these at `env init`; while any value is the
# placeholder the Doppler CLI is NOT installed (recorded as "doppler: unpinned").
DOPPLER_VERSION="REPLACE_AT_ENV_INIT"
DOPPLER_SHA256_LINUX_AMD64="REPLACE_AT_ENV_INIT"
DOPPLER_SHA256_LINUX_ARM64="REPLACE_AT_ENV_INIT"
PLACEHOLDER="REPLACE_AT_ENV_INIT"

# First action: re-exec with a scrubbed ENVIRONMENT (not a scrubbed process tree: see the
# SECRET ISOLATION LIMIT above).
if [ "${1:-}" != "--clean" ]; then
  exec env -i PATH="$PATH" HOME="$HOME" LANG=C bash "$0" --clean
fi

umask 077
SELF="$0"
SCRIPT_DIR="$(cd -- "$(dirname -- "$SELF")" && pwd -P)"
ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"
STATUS_FILE="$HOME/.ffs-cloud-env-status"
CONFIG="$SCRIPT_DIR/config.json"

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    cksum "$1" | cut -d' ' -f1
  fi
}

SELF_SHA="$(sha256_of "$SELF")"
STAMP="$HOME/.ffs-cloud-setup.$SELF_SHA"
[ -f "$STAMP" ] && exit 0

STEPS=""
FAILED=0

# record <name> <ok|failed|skipped|unpinned>
record() {
  case "$2" in failed | unpinned) FAILED=$((FAILED + 1)) ;; esac
  if [ -n "$STEPS" ]; then STEPS="$STEPS,"; fi
  STEPS="$STEPS\"$1\":\"$2\""
}

# run_step <name> <cmd...>: run in the repo root, record the result, continue.
run_step() {
  local name="$1"
  shift
  if (cd "$ROOT" && "$@") >/dev/null 2>&1; then record "$name" ok; else record "$name" failed; fi
}

tool_version() {
  local t="$1" v=""
  command -v "$t" >/dev/null 2>&1 || return 1
  case "$t" in
    shellcheck) v="$("$t" --version 2>/dev/null | sed -n 's/^version: //p' | head -n 1)" ;;
    *) v="$("$t" --version 2>/dev/null | head -n 1)" ;;
  esac
  printf '%s' "$v" | tr -cd 'A-Za-z0-9._+ ()-'
}

install_dependencies() {
  [ -f "$ROOT/package-lock.json" ] && command -v npm >/dev/null 2>&1 && run_step npm-ci npm ci
  [ -f "$ROOT/pnpm-lock.yaml" ] && command -v pnpm >/dev/null 2>&1 && run_step pnpm-install pnpm install --frozen-lockfile
  [ -f "$ROOT/yarn.lock" ] && command -v yarn >/dev/null 2>&1 && run_step yarn-install yarn install --frozen-lockfile
  [ -f "$ROOT/uv.lock" ] && command -v uv >/dev/null 2>&1 && run_step uv-sync uv sync --frozen
  local req
  if command -v python3 >/dev/null 2>&1; then
    for req in "$ROOT"/requirements*.txt; do
      [ -f "$req" ] || continue
      run_step "pip-$(basename "$req")" python3 -m pip install -r "$req"
    done
  fi
  return 0
}

install_apt_tools() {
  command -v apt-get >/dev/null 2>&1 || return 0
  local sudo_cmd=""
  if [ "$(id -u)" != "0" ]; then
    command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1 || return 0
    sudo_cmd="sudo -n"
  fi
  local missing="" t
  for t in jq bats shellcheck; do
    command -v "$t" >/dev/null 2>&1 || missing="$missing $t"
  done
  [ -n "$missing" ] || return 0
  # shellcheck disable=SC2086 # intentional word splitting of fixed package names
  if $sudo_cmd apt-get update >/dev/null 2>&1 && $sudo_cmd apt-get install -y $missing >/dev/null 2>&1; then
    record "apt:${missing# }" ok
  else
    record "apt:${missing# }" failed
  fi
}

# config_value <python expr over dict c> -> prints a string, empty on any problem.
config_value() {
  command -v python3 >/dev/null 2>&1 && [ -f "$CONFIG" ] || return 0
  python3 -c 'import json,sys
try:
    c = json.load(open(sys.argv[1]))
    v = eval(sys.argv[2], {"__builtins__": {}}, {"c": c})
    sys.stdout.write(v if isinstance(v, str) else "")
except Exception:
    pass' "$CONFIG" "$1" 2>/dev/null
}

install_doppler() {
  command -v doppler >/dev/null 2>&1 && { record doppler ok; return 0; }
  if [ "$DOPPLER_VERSION" = "$PLACEHOLDER" ] || [ "$DOPPLER_SHA256_LINUX_AMD64" = "$PLACEHOLDER" ] ||
    [ "$DOPPLER_SHA256_LINUX_ARM64" = "$PLACEHOLDER" ]; then
    record doppler unpinned
    return 0
  fi
  local arch want tmp url dest
  case "$(uname -m)" in
    x86_64 | amd64) arch=amd64 want="$DOPPLER_SHA256_LINUX_AMD64" ;;
    aarch64 | arm64) arch=arm64 want="$DOPPLER_SHA256_LINUX_ARM64" ;;
    *) record doppler failed; return 0 ;;
  esac
  command -v curl >/dev/null 2>&1 || { record doppler failed; return 0; }
  tmp="$(mktemp -d)" || { record doppler failed; return 0; }
  url="https://github.com/DopplerHQ/cli/releases/download/${DOPPLER_VERSION}/doppler_${DOPPLER_VERSION}_linux_${arch}.tar.gz"
  if curl -fsSL --max-time 120 -o "$tmp/doppler.tgz" "$url" &&
    [ "$(sha256_of "$tmp/doppler.tgz")" = "$want" ] &&
    tar -xzf "$tmp/doppler.tgz" -C "$tmp" doppler; then
    dest="$HOME/.local/bin"
    mkdir -p "$dest" && install -m 0755 "$tmp/doppler" "$dest/doppler" && record doppler ok || record doppler failed
  else
    record doppler failed
  fi
  rm -rf -- "$tmp"
}

install_secret_provider() {
  local provider cmd
  provider="$(config_value 'c["secrets"]["provider"]')"
  case "$provider" in
    doppler) install_doppler ;;
    command)
      cmd="$(config_value 'c["secrets"]["install"]')"
      if [ -n "$cmd" ]; then run_step secrets-install bash -c "$cmd"; fi
      ;;
    *) ;;
  esac
}

write_status() {
  local tools="" t v
  for t in git node npm python3 jq bats shellcheck doppler; do
    v="$(tool_version "$t")" || continue
    if [ -n "$tools" ]; then tools="$tools,"; fi
    tools="$tools\"$t\":\"$v\""
  done
  local ok=true
  [ "$FAILED" -eq 0 ] || ok=false
  printf '{"schema":1,"ok":%s,"secret_isolation":"env-i-only","tools":{%s},"steps":{%s}}\n' "$ok" "$tools" "$STEPS" >"$STATUS_FILE.tmp.$$" &&
    mv -f "$STATUS_FILE.tmp.$$" "$STATUS_FILE"
}

install_dependencies
install_apt_tools
install_secret_provider
write_status
# Stamp only a fully successful run so a failed install is retried next session.
[ "$FAILED" -eq 0 ] && : >"$STAMP"
exit 0
