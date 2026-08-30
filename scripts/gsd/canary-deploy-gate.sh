#!/usr/bin/env bash
# canary-deploy-gate.sh — GH-153: the sole legitimate producer of canary
# evidence rows for image-digest deploy surfaces.
#
# scripts/gsd/canary-gate.sh mints rows only for web-touch diffs, keyed by
# `git rev-parse HEAD`. A non-web image-digest deploy surface has nothing
# sanctioned that can mint a row bound to the digest actually deployed, so
# once the canary namespace is armed those promotions become structurally
# unsatisfiable. This wrapper closes that gap the same way canary-gate.sh
# closes the commit-sha gap.
#
# OBSERVE, NEVER ACCEPT (D-01): the digest is taken SOLELY from the stdout
# of a consumer-supplied query command (FFS_DEPLOY_DIGEST_CMD). There is no
# --digest flag and no env var by which a caller supplies the digest
# directly — any argument at all is a usage error. This is the digest
# equivalent of canary-gate.sh's `git rev-parse HEAD` discipline: a
# --digest flag would reproduce the caller-fabrication hole one layer out.
#
# DOUBLE-OBSERVATION (TOCTOU): the digest is observed TWICE through the same
# FFS_DEPLOY_DIGEST_CMD seam — once before the probe, once again immediately
# after it completes — with identical single-line/shape validation both
# times. A deployment that flips from digest A to digest B mid-probe would
# otherwise let B's healthy response mint a PASS row for A, making an
# unprobed artifact promotable. On any mismatch between the two observations
# the wrapper refuses with CANARY-DEPLOY-DIGEST-CHANGED (exit 2, zero rows)
# regardless of the probe's own outcome — a moved deployment is neither a
# pass nor a fail for either digest. Each observation's own validation
# failures keep their existing UNOBSERVED/INVALID refusal semantics.
#
# D-02: one row per observed digest, not per deploy surface. Rows key by
# the observed digest, matching _canary_bound_ok's exact-string semantics.
# One digest serving N surfaces needs one passing row; an operator wanting
# per-surface proof runs this wrapper once per surface and the extra
# same-digest rows are harmless.
#
# THE PROBE IS A CONSUMER SEAM TOO (D-03): FFS_DEPLOY_PROBE_CMD is a real
# post-deploy health/smoke command run under a bounded timeout. Its exit
# code IS the pass/fail recorded — deliberately, a failed probe records
# pass=false ON PURPOSE, because a FAILED row is evidence and arms binding
# (D-04) — that is the desired semantics, not a bug.
#
# NO KILL-SWITCH BY DESIGN: this is a producer, not a blocking gate —
# declining to run it is already the bypass, so there is nothing to waive.
# Do not add a CANARY_DEPLOY_GATE=off / waiver-record call by pattern-match
# from canary-gate.sh; there is no equivalent case here.
#
# TRUST BOUNDARY: FFS_DEPLOY_DIGEST_CMD and FFS_DEPLOY_PROBE_CMD are
# consumer-supplied command strings executed via `bash -c` — the same
# operator-configuration trust boundary as every other command seam in
# scripts/gsd.
#
# Vendor-neutral (D-05): consumers wire their own digest-query and probe
# commands; FFS ships no platform-specific defaults.
#
# Usage: canary-deploy-gate.sh   (zero arguments; any argument is a usage
# error, exit 2 — including --digest, see D-01 above)
#
# Env:
#   FFS_DEPLOY_DIGEST_CMD       (required) stdout is the deployed digest
#   FFS_DEPLOY_PROBE_CMD        (required) post-deploy health/smoke command
#   FFS_DEPLOY_DIGEST_TIMEOUT   default 60  — wall-clock bound, digest query
#   FFS_DEPLOY_PROBE_TIMEOUT    default 300 — wall-clock bound, probe
#   GSD_RUN_ID                  default unattributed — run_id on the row
#   GATES_STORE                 PRESERVED, exactly as canary-gate.sh does
#
# Exit codes: 0 recorded pass, 1 recorded fail, 2 refusal (zero rows),
# 3 evidence unrecorded. 1 and 3 stay distinct on purpose — "the deploy is
# unhealthy" and "the evidence system is broken" are different operator
# situations and must not share a code.
set -euo pipefail

if [ $# -gt 0 ]; then
  echo "canary-deploy-gate: usage: canary-deploy-gate.sh (zero arguments — env seams only, see docs/configuration.md; no --digest, see D-01)" >&2
  exit 2
fi

DEPLOY_DIGEST_CMD="${FFS_DEPLOY_DIGEST_CMD:-}"
DEPLOY_PROBE_CMD="${FFS_DEPLOY_PROBE_CMD:-}"
if [ -z "${DEPLOY_DIGEST_CMD//[[:space:]]/}" ] || [ -z "${DEPLOY_PROBE_CMD//[[:space:]]/}" ]; then
  echo "canary-deploy-gate: CANARY-DEPLOY-SEAM-MISSING — both FFS_DEPLOY_DIGEST_CMD and FFS_DEPLOY_PROBE_CMD are required" >&2
  exit 2
fi

DIGEST_TIMEOUT="${FFS_DEPLOY_DIGEST_TIMEOUT:-60}"
PROBE_TIMEOUT="${FFS_DEPLOY_PROBE_TIMEOUT:-300}"

# shellcheck disable=SC1091 source=./run-bounded.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/run-bounded.sh"

DEPLOY_DIGEST_ERE='^[a-z0-9]+([._/-][a-z0-9]+)*(:[A-Za-z0-9_][A-Za-z0-9._-]*)?@sha256:[0-9a-f]{64}$'
# Mirror of lib/gates.py:1195 ARTIFACT_DIGEST_PAT — POSIX-ERE transcription
# with (?:...) non-capturing groups rewritten as plain groups (identical
# matching). A bats vector table pins the two together.

# Observe the digest once — bounded, stdout captured, set -e suspended so a
# nonzero rc (including 124 timeout) is handled explicitly, never a hard
# script exit. Prints the digest on stdout and returns 0 on success; on
# failure prints the typed refusal to stderr itself and returns 2 (a
# command-substitution call site can't read variables this function sets —
# it runs in a subshell — so the message is emitted here, not signaled
# back). Called twice (pre- and post-probe, see DOUBLE-OBSERVATION above)
# so the validation logic lives in exactly one place; $3 is an optional
# message suffix distinguishing the post-probe confirmation call.
_observe_digest() {
  local _timeout="$1" _cmd="$2" _suffix="${3:-}" _digest _rc
  set +e
  _digest="$(run_bounded "$_timeout" bash -c "$_cmd")"
  _rc=$?
  set -e
  if [ "$_rc" -ne 0 ]; then
    echo "canary-deploy-gate: CANARY-DEPLOY-DIGEST-UNOBSERVED — digest-query command failed or timed out (rc $_rc)${_suffix}" >&2
    return 2
  fi
  # Validate, IN THIS ORDER. The newline check MUST precede the pattern
  # check — grep -E matches per line, so a two-line output whose second
  # line happens to be well-formed digest-shaped would otherwise slip
  # through.
  if [ -z "$_digest" ]; then
    echo "canary-deploy-gate: CANARY-DEPLOY-DIGEST-INVALID — digest-query produced empty output${_suffix}" >&2
    return 2
  fi
  case "$_digest" in
    *$'\n'*)
      echo "canary-deploy-gate: CANARY-DEPLOY-DIGEST-INVALID — digest-query produced multi-line output${_suffix}" >&2
      return 2
      ;;
  esac
  if ! echo "$_digest" | grep -Eq "$DEPLOY_DIGEST_ERE"; then
    echo "canary-deploy-gate: CANARY-DEPLOY-DIGEST-INVALID — digest-query output '$_digest' is not digest-shaped (a bare commit sha is canary-gate.sh's surface, not this wrapper's)${_suffix}" >&2
    return 2
  fi
  printf '%s' "$_digest"
  return 0
}

# Step 3-4: pre-probe observation.
if ! OBSERVED_DIGEST="$(_observe_digest "$DIGEST_TIMEOUT" "$DEPLOY_DIGEST_CMD")"; then
  exit 2
fi

# Step 5: bounded probe run; its rc IS the recorded pass/fail (D-03/D-04).
# Timestamps bracket the probe run itself, not wrapper entry.
CREATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
set +e
run_bounded "$PROBE_TIMEOUT" bash -c "$DEPLOY_PROBE_CMD"
PROBE_RC=$?
set -e
ENDED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if [ "$PROBE_RC" -eq 0 ]; then
  PASS_VAL="true"
else
  PASS_VAL="false"
fi

# Step 5b (TOCTOU): re-observe immediately after the probe completes, same
# seam, same validation. Mismatch refuses regardless of the probe's own
# outcome (see DOUBLE-OBSERVATION above) — checked BEFORE recording, so a
# flipped deployment records nothing for either digest.
if ! CONFIRM_DIGEST="$(_observe_digest "$DIGEST_TIMEOUT" "$DEPLOY_DIGEST_CMD" " (post-probe confirmation)")"; then
  exit 2
fi
if [ "$CONFIRM_DIGEST" != "$OBSERVED_DIGEST" ]; then
  echo "canary-deploy-gate: CANARY-DEPLOY-DIGEST-CHANGED — digest moved from ${OBSERVED_DIGEST} to ${CONFIRM_DIGEST} during the probe window; refusing (neither a pass nor a fail for either digest)" >&2
  exit 2
fi

# Step 6: resolve this script's OWN repo root and lib/gates.py — mirrors
# canary-gate.sh:197-202 exactly. GATES_STORE is deliberately NOT stripped.
CANARY_DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
CANARY_DEPLOY_ROOT="$(git -C "$CANARY_DEPLOY_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$CANARY_DEPLOY_ROOT" ] || [ ! -f "$CANARY_DEPLOY_ROOT/lib/gates.py" ]; then
  echo "canary-deploy-gate: CANARY-DEPLOY-EVIDENCE-UNRECORDED — cannot resolve lib/gates.py from the gate's own repo; refusing" >&2
  exit 3
fi
if ! (cd "$CANARY_DEPLOY_ROOT" && env -u GIT_DIR -u GIT_COMMON_DIR -u GIT_WORK_TREE \
    python3 "$CANARY_DEPLOY_ROOT/lib/gates.py" canary-evidence \
    --run-id "${GSD_RUN_ID:-unattributed}" --sha "$OBSERVED_DIGEST" \
    --pass "$PASS_VAL" --created-at "$CREATED_AT" --ended-at "$ENDED_AT"); then
  echo "canary-deploy-gate: CANARY-DEPLOY-EVIDENCE-UNRECORDED — recorder failed; refusing" >&2
  exit 3
fi

# Step 8: report. PASS/FAIL is decided by the probe rc recorded above.
if [ "$PROBE_RC" -eq 0 ]; then
  echo "canary-deploy-gate: PASS — digest ${OBSERVED_DIGEST} healthy (pass=true recorded)"
  exit 0
else
  echo "canary-deploy-gate: FAIL — probe exited ${PROBE_RC} for digest ${OBSERVED_DIGEST} (pass=false recorded)" >&2
  exit 1
fi
