#!/usr/bin/env bash
# security-model-fence.sh — keep security-touching PLANNING off Fable
#
# Fable's safety classifiers can false-refuse benign defensive-security work
# (auth, RLS, payments, crypto), which stalls an autonomous run silently.
# model-fallback.sh only swaps fable->opus when fable is UNAVAILABLE; this
# fence rewrites the two planning roles (gsd-planner, gsd-plan-checker)
# fable->opus when the spec is security-touching — even when fable is
# available. Executor (sonnet) and verifier/reviewer (opus) never run on
# Fable in the base template, so the planning tier is the only exposure.
#
# Usage: security-model-fence.sh [<planning-dir>] [extra-spec-files...]
#   Scans <planning-dir>/{PROJECT,REQUIREMENTS,ROADMAP}.md plus any extra
#   files passed (e.g. specs/NNN/spec.md specs/NNN/plan.md) for security
#   keywords. Match -> rewrite; no match -> config unchanged.
#
# Fail-soft + never silent (same convention as model-fallback.sh): a broken
# mechanism warns and exits 0 so a run is never blocked by the fence itself.
# False-positive direction is safe: worst case security-adjacent wording
# routes planning to opus (costlier, never wrong).
set -uo pipefail

PLANNING_DIR="${1:-.planning}"
shift 2>/dev/null || true
CONFIG="$PLANNING_DIR/config.json"

if [ ! -f "$CONFIG" ]; then
  echo "[security-model-fence] WARN: $CONFIG not found — skipping (fail-soft)" >&2
  exit 0
fi

# shellcheck source=scripts/gsd/security-surface.sh
. "$(dirname "${BASH_SOURCE[0]}")/security-surface.sh"

SCAN_FILES=""
for f in "$PLANNING_DIR/PROJECT.md" "$PLANNING_DIR/REQUIREMENTS.md" "$PLANNING_DIR/ROADMAP.md" "$@"; do
  [ -f "$f" ] && SCAN_FILES="$SCAN_FILES $f"
done

if [ -z "$SCAN_FILES" ]; then
  echo "[security-model-fence] WARN: no planning docs to scan — config unchanged" >&2
  exit 0
fi

# shellcheck disable=SC2086  # intentional word-split: SCAN_FILES is a checked file list
if ! grep -Eiq "$KEYWORDS" $SCAN_FILES 2>/dev/null; then
  echo "[security-model-fence] no security keywords — config unchanged"
  exit 0
fi

CONFIG="$CONFIG" python3 - <<'EOF' || { echo "[security-model-fence] WARN: rewrite failed — config unchanged (fail-soft)" >&2; exit 0; }
import json, os
path = os.environ["CONFIG"]
cfg = json.load(open(path))
overrides = cfg.get("model_overrides", {})
n = 0
# planning roles only — executor/verifier bindings stay untouched.
# Both spellings: template pins the "fable" alias; a resolved config
# (resolve_model_ids) holds the full "claude-fable-5" ID.
FENCE = {"fable": "opus", "claude-fable-5": "claude-opus-5"}
for role in ("gsd-planner", "gsd-plan-checker"):
    if overrides.get(role) in FENCE:
        overrides[role] = FENCE[overrides[role]]
        n += 1
if n:
    json.dump(cfg, open(path, "w"), indent=2)
    print(f"[security-model-fence] security-touching spec: {n} planning role(s) fable -> opus")
else:
    print("[security-model-fence] security-touching spec, but no fable planning pins — config unchanged")
EOF
