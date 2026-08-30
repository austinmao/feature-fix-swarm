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
# RESIDUAL (by design): an A->B->A double flip entirely INSIDE the probe
# window defeats double-observation — both observations see A while the
# probe itself tested B. A deployment lease would close this but is out of
# reach for a vendor-neutral wrapper. Consumers whose probe can attest the
# digest it tested SHOULD set FFS_DEPLOY_PROBE_DIGEST_FILE (optional) to
# close it: the wrapper truncates that path before running the probe (so
# stale content can never satisfy it), then after the probe requires its
# content to byte-match both surrounding observations — mismatch or
# missing/malformed content refuses and records nothing, same as above.
# When unset, current double-observation behavior stands unchanged.
#
# PROBE-DIGEST-FILE PATH SAFETY: that path can live in a writable shared
# directory, so it is checked — BEFORE truncating and AGAIN before reading
# — for a symlink (dangling or not), a non-regular file, or an existing
# file not owned by this process; any of those refuses with
# CANARY-DEPLOY-PROBE-DIGEST-UNSAFE and records nothing. The read is capped
# (head -c 4096) and its raw content is NEVER echoed in any error/output —
# only the path and the violation class are.
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
#   FFS_DEPLOY_PROBE_DIGEST_FILE (optional) path the probe writes the digest
#                                it actually tested; closes the in-probe-window
#                                flip double-observation alone cannot see
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

# Classify a candidate digest string against DEPLOY_DIGEST_ERE, IN THIS
# ORDER: empty check, then newline check, then pattern. The newline check
# MUST precede the pattern check — grep -E matches per line, so a two-line
# value whose second line happens to be well-formed digest-shaped would
# otherwise slip through. Prints one of ok|empty|multiline|badshape to
# stdout. Shared by both the digest-query seam and the optional
# FFS_DEPLOY_PROBE_DIGEST_FILE seam so the shape rule lives in one place.
_digest_shape_check() {
  local _d="$1"
  if [ -z "$_d" ]; then
    printf 'empty'
    return
  fi
  case "$_d" in
    *$'\n'*)
      printf 'multiline'
      return
      ;;
  esac
  if echo "$_d" | grep -Eq "$DEPLOY_DIGEST_ERE"; then
    printf 'ok'
  else
    printf 'badshape'
  fi
}

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
  local _timeout="$1" _cmd="$2" _suffix="${3:-}" _digest _rc _shape
  set +e
  _digest="$(run_bounded "$_timeout" bash -c "$_cmd")"
  _rc=$?
  set -e
  if [ "$_rc" -ne 0 ]; then
    echo "canary-deploy-gate: CANARY-DEPLOY-DIGEST-UNOBSERVED — digest-query command failed or timed out (rc $_rc)${_suffix}" >&2
    return 2
  fi
  _shape="$(_digest_shape_check "$_digest")"
  case "$_shape" in
    empty)
      echo "canary-deploy-gate: CANARY-DEPLOY-DIGEST-INVALID — digest-query produced empty output${_suffix}" >&2
      return 2
      ;;
    multiline)
      echo "canary-deploy-gate: CANARY-DEPLOY-DIGEST-INVALID — digest-query produced multi-line output${_suffix}" >&2
      return 2
      ;;
    badshape)
      echo "canary-deploy-gate: CANARY-DEPLOY-DIGEST-INVALID — digest-query output '$_digest' is not digest-shaped (a bare commit sha is canary-gate.sh's surface, not this wrapper's)${_suffix}" >&2
      return 2
      ;;
  esac
  printf '%s' "$_digest"
  return 0
}

# The optional probe-digest-file path could live in a writable shared
# directory — same operator-configuration trust class as the command seams,
# but a symlink there would redirect our truncate/read to an arbitrary file
# this process can touch, and a swapped-in regular file's raw bytes could
# otherwise leak through an error message (mirrors publish-scanned-handoff.sh's
# `[ -L ]` symlink refusal on artifact paths). Checked BEFORE truncating and
# AGAIN before reading — the untrusted probe runs between those two calls and
# could swap the path out from under the first check. Refuses on: the path
# is a symlink (dangling or not); an existing path that is not a plain
# regular file; an existing path not owned by this process's uid. A path
# that does not exist yet is safe (the truncate is about to create it).
# Prints the path and the violation class ONLY — never file content.
_probe_digest_file_safe() {
  local _p="$1"
  if [ -L "$_p" ]; then
    echo "canary-deploy-gate: CANARY-DEPLOY-PROBE-DIGEST-UNSAFE — FFS_DEPLOY_PROBE_DIGEST_FILE is a symlink: $_p" >&2
    return 1
  fi
  if [ -e "$_p" ]; then
    if [ ! -f "$_p" ]; then
      echo "canary-deploy-gate: CANARY-DEPLOY-PROBE-DIGEST-UNSAFE — FFS_DEPLOY_PROBE_DIGEST_FILE is not a regular file: $_p" >&2
      return 1
    fi
    if [ ! -O "$_p" ]; then
      echo "canary-deploy-gate: CANARY-DEPLOY-PROBE-DIGEST-UNSAFE — FFS_DEPLOY_PROBE_DIGEST_FILE is not owned by this process: $_p" >&2
      return 1
    fi
  fi
  return 0
}

# Step 3-4: pre-probe observation.
if ! OBSERVED_DIGEST="$(_observe_digest "$DIGEST_TIMEOUT" "$DEPLOY_DIGEST_CMD")"; then
  exit 2
fi

# Optional probe-side attestation seam (see RESIDUAL above). Truncate BEFORE
# the probe runs — a stale value left from a prior invocation must never be
# able to satisfy the post-probe check.
PROBE_DIGEST_FILE="${FFS_DEPLOY_PROBE_DIGEST_FILE:-}"
if [ -n "$PROBE_DIGEST_FILE" ]; then
  if ! _probe_digest_file_safe "$PROBE_DIGEST_FILE"; then
    exit 2
  fi
  : > "$PROBE_DIGEST_FILE"
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

# Step 5c (optional, see RESIDUAL above): probe-attested digest must be
# present, shape-valid, and byte-match the (already agreeing) observations.
# This is full probe-to-artifact binding — the recorded digest is the one
# the probe itself tested, closing the in-window flip double-observation
# alone cannot see.
if [ -n "$PROBE_DIGEST_FILE" ]; then
  if ! _probe_digest_file_safe "$PROBE_DIGEST_FILE"; then
    exit 2
  fi
  # head -c caps the read — a swapped-in large file can't be slurped into
  # this process even though the safety check above already rejects
  # anything the truncate itself didn't create as a plain regular file.
  PROBE_DIGEST="$(head -c 4096 "$PROBE_DIGEST_FILE" 2>/dev/null || true)"
  PROBE_SHAPE="$(_digest_shape_check "$PROBE_DIGEST")"
  case "$PROBE_SHAPE" in
    empty)
      echo "canary-deploy-gate: CANARY-DEPLOY-PROBE-DIGEST-INVALID — FFS_DEPLOY_PROBE_DIGEST_FILE was empty after the probe; a consumer that opts in must have the probe write the digest it tested" >&2
      exit 2
      ;;
    multiline)
      echo "canary-deploy-gate: CANARY-DEPLOY-PROBE-DIGEST-INVALID — FFS_DEPLOY_PROBE_DIGEST_FILE contained multi-line output" >&2
      exit 2
      ;;
    badshape)
      # Never echo the file's content here — it can be arbitrary bytes from
      # whatever was actually at this path (see _probe_digest_file_safe
      # above for why that's a real, not hypothetical, risk).
      echo "canary-deploy-gate: CANARY-DEPLOY-PROBE-DIGEST-INVALID — FFS_DEPLOY_PROBE_DIGEST_FILE content is not digest-shaped" >&2
      exit 2
      ;;
  esac
  if [ "$PROBE_DIGEST" != "$OBSERVED_DIGEST" ]; then
    echo "canary-deploy-gate: CANARY-DEPLOY-DIGEST-CHANGED — probe-attested digest ${PROBE_DIGEST} does not match the observed digest ${OBSERVED_DIGEST}; refusing (neither a pass nor a fail for either digest)" >&2
    exit 2
  fi
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
