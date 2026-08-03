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
#
# spec-004 AC-007: SECURITY_MODEL_FENCE=off is a kill-switch — config stays
# untouched and no marker is written, the lever still exits 0 (this fence has
# no fail-closed direction of its own; F7 noted there was no documented
# in-file kill-switch even though docs implied one). On trigger, an
# additional RUN-SCOPED marker is written under .planning/run-state/ — never
# under PLANNING_DIR itself — consumed by plan-wall.sh for record-stamping
# (`fence_marker`) and waiver flagging. Run-scoped by construction (keyed to
# RUN_ID): no separate clearing lever is needed (a stale marker only ever
# matches ITS OWN run, EDGE-005 handles a same-run false match).
set -uo pipefail

PLANNING_DIR="${1:-.planning}"
shift 2>/dev/null || true
CONFIG="$PLANNING_DIR/config.json"

if [ "${SECURITY_MODEL_FENCE:-on}" = "off" ]; then
  echo "[security-model-fence] SECURITY_MODEL_FENCE=off — kill-switch active, config unchanged"
  exit 0
fi

if [ ! -f "$CONFIG" ]; then
  echo "[security-model-fence] WARN: $CONFIG not found — skipping (fail-soft)" >&2
  exit 0
fi

# RUN_ID derivation copied from the established pattern (review-gate-command.sh,
# liveness-check.sh): env > branch-derived spec-NNN > timestamp-pid fallback.
# This lever is fail-soft, not fail-closed, so an underivable RUN_ID falls
# back rather than blocking (unlike ship's grant-ledger key, nothing here is
# looked up BY run id — it only needs to be a stable, collision-safe name for
# THIS invocation's marker file).
RUN_ID="${GSD_RUN_ID:-}"
if [ -z "$RUN_ID" ]; then
  BRANCH_NNN="$(git branch --show-current 2>/dev/null | grep -oE '^[0-9]{3}' | head -1)"
  [ -n "$BRANCH_NNN" ] && RUN_ID="spec-${BRANCH_NNN}"
fi
[ -n "$RUN_ID" ] || RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
SAFE_RUN_ID="$(printf '%s' "$RUN_ID" | LC_ALL=C tr -c 'A-Za-z0-9_.-' '_' | cut -c1-128)"
RUN_STATE_DIR="$PLANNING_DIR/run-state"
FENCE_MARKER="$RUN_STATE_DIR/security-fence-${SAFE_RUN_ID}.json"

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

CONFIG="$CONFIG" RUN_STATE_DIR="$RUN_STATE_DIR" FENCE_MARKER="$FENCE_MARKER" \
  RUN_ID="$RUN_ID" python3 - <<'EOF' || { echo "[security-model-fence] WARN: rewrite failed — config unchanged (fail-soft)" >&2; exit 0; }
import json, os, time
path = os.environ["CONFIG"]
cfg = json.load(open(path))
overrides = cfg.get("model_overrides", {})
n = 0
roles = []
# planning roles only — executor/verifier bindings stay untouched.
# Both spellings: template pins the "fable" alias; a resolved config
# (resolve_model_ids) holds the full "claude-fable-5" ID.
FENCE = {"fable": "opus", "claude-fable-5": "claude-opus-5"}
for role in ("gsd-planner", "gsd-plan-checker"):
    if overrides.get(role) in FENCE:
        overrides[role] = FENCE[overrides[role]]
        n += 1
        roles.append(role)
if n:
    json.dump(cfg, open(path, "w"), indent=2)
    print(f"[security-model-fence] security-touching spec: {n} planning role(s) fable -> opus")
    # AC-007: run-scoped marker for plan-wall.sh record-stamping + waiver
    # flagging. Written best-effort — a marker write failure never blocks
    # this fail-soft lever; plan-wall.sh treats an absent marker as
    # fence_marker: false (same signal as "fence never fired").
    try:
        marker_dir = os.environ["RUN_STATE_DIR"]
        os.makedirs(marker_dir, exist_ok=True)
        marker_path = os.environ["FENCE_MARKER"]
        tmp = f"{marker_path}.tmp"
        with open(tmp, "w") as fh:
            json.dump({"run_id": os.environ["RUN_ID"], "roles": roles,
                       "triggered_at": time.time()}, fh, indent=2)
        os.replace(tmp, marker_path)
    except OSError as exc:
        print(f"[security-model-fence] WARN: marker write failed ({exc}) — "
              "fence still applied to config", file=__import__("sys").stderr)
else:
    print("[security-model-fence] security-touching spec, but no fable planning pins — config unchanged")
EOF
