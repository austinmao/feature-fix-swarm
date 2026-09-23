#!/usr/bin/env bash
# CI integration proof for the exact npm package: install both real upstream
# profiles into an isolated home, run FFS doctor, and exercise the manifest-
# verified staging seam used by headless Codex runs.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
live_root="$(mktemp -d "${TMPDIR:-/tmp}/ffs-live-gsd.XXXXXX")"
trap 'rm -rf "$live_root"' EXIT
live_home="$live_root/home"
live_bin="$live_root/bin"
mkdir -p "$live_home" "$live_bin"

# Doctor requires a supported Codex version and the static isolated-runtime
# `exec --help` surface. CI is validating the installed package/runtime
# surface here, not launching a provider process (runtime stays UNMET).
cat > "$live_bin/codex" <<'STUB'
#!/usr/bin/env bash
case "$1" in
  exec) printf '%s\n' --strict-config --ignore-user-config --ignore-rules \
          --sandbox --add-dir --disable --dangerously-bypass-hook-trust ;;
  *) printf 'codex-cli 0.147.0\n' ;;
esac
STUB
chmod +x "$live_bin/codex"

export HOME="$live_home"
export CODEX_HOME="$live_home/.codex"
export PATH="$live_bin:$PATH"
export FFS_SKIP_PROMPT_MASTER=1
export FFS_SKIP_SOCRATIC=1

bash "$repo_root/setup.sh" --scope user
bash "$repo_root/setup.sh" --doctor --scope user --json > "$live_root/doctor.json"
python3 - "$live_root/doctor.json" <<'PY'
import json, pathlib, sys
report = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert report.get("exit_code") == 0, report
assert any(row.get("id") == "gsd-manifests" and row.get("status") == "pass" for row in report.get("checks", [])), report
PY

manifest="$CODEX_HOME/gsd-file-manifest.json"
skills="$HOME/.agents/skills"
stage="$live_root/staged-skills"
python3 "$repo_root/scripts/gsd/stage-gsd-skills.py" verify \
  "$manifest" "$skills" gsd-execute-phase
python3 "$repo_root/scripts/gsd/stage-gsd-skills.py" stage \
  "$manifest" "$skills" "$stage" gsd-execute-phase
test -f "$stage/gsd-execute-phase/SKILL.md"
