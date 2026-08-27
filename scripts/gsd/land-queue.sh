#!/usr/bin/env bash
# land-queue.sh — serial production landing queue (spec-006 phase 2, plan 01).
#
# One item at a time, every side effect journaled intent-before-effect, all
# privileged truths delegated to existing authorities:
#   collect-queue.py   intake + authority-only item prechecks (same code path)
#   queue-journal.py   durable event journal + queue-wide OwnerLock
#   queue-guard.sh     allow/stop + drain decisions (STOP before EVERY effect)
#   gates.py           exact merge:pr-N grant truth
#   assert-merged.sh   terminal merged-state truth
#   run-finalizer.sh   deletion/cleanup truth
#
# Binding residuals honored here: 71193d25 (queue-wide OwnerLock held for the
# run; contention = QUEUE-REFUSED:queue-live), f1bc7cad (push prepared head;
# REVIEWED_OID must equal pushed sha AND PR head at pin time), e846ec0c (STOP
# checked before starting every external effect), 71c46cda (DRAIN per store,
# honored once, consumed under the lock).
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COLLECTOR="$ROOT/skills/land-queue/scripts/collect-queue.py"
JOURNAL="$ROOT/skills/land-queue/scripts/queue-journal.py"
GUARD="$SCRIPT_DIR/queue-guard.sh"
GATES="$ROOT/lib/gates.py"
# shellcheck source=scripts/gsd/run-bounded.sh
. "$SCRIPT_DIR/run-bounded.sh"
# shellcheck source=scripts/gsd/autonomy-posture.sh
. "$SCRIPT_DIR/autonomy-posture.sh"
# shellcheck source=scripts/gsd/adversary-host.sh
. "$SCRIPT_DIR/adversary-host.sh"

usage() {
  echo "usage: land-queue.sh [--repo DIR] [--base NAME] [--run-id ID] [--posture zero|floor] [--drain] [--resume QUEUE-ID] [BRANCH...]" >&2
  exit 2
}

REPO="$PWD" BASE="main" RUN_ID="" DRAIN=0 RESUME="" POSTURE_CLI=""
EXPLICIT=()
while [ $# -gt 0 ]; do
  case "$1" in
    --contract-probe) exit 0 ;;
    --repo) REPO="${2:?--repo requires a value}"; shift 2 ;;
    --base) BASE="${2:?--base requires a value}"; shift 2 ;;
    --run-id) RUN_ID="${2:?--run-id requires a value}"; shift 2 ;;
    --drain) DRAIN=1; shift ;;
    --resume) RESUME="${2:?--resume requires a value}"; shift 2 ;;
    --posture)
      # Research-resolved narrow input seam: zero default, floor stricter.
      # Phase 3 owns the committed configuration; nothing is read from disk.
      case "${2:-}" in
        zero|floor) POSTURE_CLI="$2"; shift 2 ;;
        *) usage ;;
      esac ;;
    --parallel|--parallel=*)
      # EDGE-007: parsed but always refused, before any lane could launch.
      echo "PARALLEL-UNSUPPORTED:v1-serial-only"
      exit 2 ;;
    --*) usage ;;
    *) EXPLICIT+=("$1"); shift ;;
  esac
done
[ -n "$RUN_ID" ] || RUN_ID="land-queue-$(date +%s)-$$"

STORE_DIR="$(python3 "$GATES" store-dir)"
LQ="$STORE_DIR/land-queue"
mkdir -p "$LQ"

if [ "$DRAIN" -eq 1 ]; then
  : > "$LQ/DRAIN"
  echo "DRAIN-REQUESTED: a running queue will drain at its next item boundary"
  exit 0
fi

# 71193d25: exactly one queue per store — a queue-wide OwnerLock bound to
# this runner's pid, held for the whole run.
if ! python3 "$JOURNAL" lock-acquire --store "$LQ" --run-id "$RUN_ID" --pid $$; then
  echo "QUEUE-REFUSED:queue-live"
  exit 75
fi

WORKTMP="$(mktemp -d "${TMPDIR:-/tmp}/land-queue.XXXXXX")"
cleanup() {
  python3 "$JOURNAL" lock-release --store "$LQ" --pid $$ >/dev/null 2>&1 || true
  rm -rf "$WORKTMP"
}
on_signal() { # phase-01 signal pattern: owner-only release, then terminate
  # immediately with exit 128+signum — no journal/effect/report code runs
  # after a signal-path release.
  local signum="$1"
  trap - EXIT
  cleanup
  exit $((128 + signum))
}
trap cleanup EXIT
trap 'on_signal 1' HUP
trap 'on_signal 2' INT
trap 'on_signal 15' TERM

QUEUE_ID="$RUN_ID"

# ── REQ-306: single monotonic posture resolution (spec-006 phase 3) ───────
# default zero < config (validated --posture flag, else the committed
# .planning/config.json autonomy.posture) < FFS_AUTONOMY_POSTURE, stricter-
# only.  Resolved exactly once per run; every posture consumer below reads
# AUTONOMY_POSTURE, never the env/config inputs.
resolve_autonomy_posture "$REPO/.planning/config.json" "$POSTURE_CLI"
echo "POSTURE-RESOLVED: $AUTONOMY_POSTURE source=$AUTONOMY_POSTURE_SOURCE"
# CR-01 (round 2): persist the resolved posture + provenance under the run
# id in the gates evidence ledger BEFORE any queue effect.  Later processes
# (promotion checks, hotfix bypass) read this durable record — never a
# caller environment variable.  An unwritable record fails the queue closed.
if ! python3 "$GATES" note-posture "$RUN_ID" --posture "$AUTONOMY_POSTURE" \
    --source "$AUTONOMY_POSTURE_SOURCE" >/dev/null; then
  echo "QUEUE-ERROR:store posture evidence write refused"
  exit 70
fi
# ── CR-05: producing host + opposite vendor, resolved once per run ────────
# Floor's defining guarantee (37bc43d9) is a review by the OPPOSITE vendor;
# the host that produced the change can never review itself into a floor
# pass.  Detection follows scripts/gsd/adversary-host.sh conventions.
PRODUCING_HOST="$(detect_orchestrator_host)"
OPPOSITE_VENDOR="$(adversary_kind_for_host "$PRODUCING_HOST")"
echo "REVIEW-VENDOR: host=$PRODUCING_HOST opposite=$OPPOSITE_VENDOR"
# EDGE-006: any journal validation/write failure is immediate infrastructure
# QUEUE-ERROR:store — it bypasses classification and stops the queue before
# another effect runs.
journal() {
  python3 "$JOURNAL" append --store "$LQ" --queue-id "$QUEUE_ID" "$@" \
    || { echo "QUEUE-ERROR:store"; exit 70; }
}

emit_report() { # typed ITEM/REVERT/HUMAN-INBOX rows from the journal
  # REQ-211/REQ-214: LANDED rows carry the merge SHA plus a concrete revert
  # command; every other terminal renders its reason and one-command unblock
  # into the Human inbox. Rows come deduplicated (last terminal per item).
  local rows inbox t_item t_status t_detail t_reason t_unblock
  rows="$WORKTMP/report-rows"
  python3 "$JOURNAL" read-report --store "$LQ" --queue-id "$QUEUE_ID" > "$rows" \
    || { echo "QUEUE-ERROR:store"; exit 70; }
  inbox=0
  while IFS= read -r -d '' t_item && IFS= read -r -d '' t_status \
      && IFS= read -r -d '' t_detail && IFS= read -r -d '' t_reason \
      && IFS= read -r -d '' t_unblock; do
    if [ -z "$t_item" ]; then
      continue  # queue-level terminal; echoed by the caller
    fi
    case "$t_status" in
      LANDED)
        printf 'ITEM %s LANDED %s\n' "$t_item" "$t_detail"
        printf 'REVERT: %s git revert %s\n' "$t_item" "$t_detail"
        ;;
      *)
        printf 'ITEM %s %s %s\n' "$t_item" "$t_status" "$t_reason"
        inbox=$((inbox + 1))
        printf 'HUMAN-INBOX: %s %s reason: %s unblock: %s\n' \
          "$t_item" "$t_status" "$t_reason" "$t_unblock"
        ;;
    esac
  done < "$rows"
  if [ "$inbox" -eq 0 ]; then
    echo "HUMAN-INBOX: empty"
  fi
}

# ── spec-006 Phase 3 (REQ-301/302, CR-03/WR-02): consolidate grant ────────
# Idempotent post-terminal derivation, invoked at every all-items-terminal
# boundary.  gates.py grant-consolidate loads the named queue journal
# ITSELF, verifies the run/queue binding and that every item is terminal,
# and derives the canonical tuples from the journal's read-landed-tuples
# projection — never estate prose, scouts, resume strings, shell arguments,
# or stdin.  The grant is bound to the journal's ORIGINAL run id and its
# TTL is clipped to the journal-recorded immutable deadline, so a resumed
# queue or a caller environment value can never extend authority
# (WR-02/CR-02: no caller timeout is ever forwarded to the mint).  Grant derivation never rewrites
# the queue outcome: on any refusal the landed report stands and the
# consolidation simply has no grant to run under (fail closed, REQ-302).
derive_consolidate_grant() {
  local orig_run
  orig_run="$(python3 "$JOURNAL" read-run-id --store "$LQ" --queue-id "$QUEUE_ID" 2>/dev/null)" || return 0
  [ -n "$orig_run" ] || return 0
  if ! python3 "$GATES" grant-consolidate "$orig_run" --queue-id "$QUEUE_ID" \
      --journal-store "$LQ" --repo "$REPO" --base "$BASE" --ttl-hours 8; then
    echo "CONSOLIDATE-GRANT-SKIPPED: queue-derived grant refused; landed report stands"
  fi
}

RESUME_RETRY=()
resume_reconcile() {
  # REQ-208 / 8c88ebfa (01-08 two-phase intent): before any new effect,
  # enumerate EVERY nonterminal queue item (LANDED and other terminals are
  # skipped) and classify each against merged authority — assert-merged.sh
  # plus gh — by its recorded idempotency key.  A satisfied key is NEVER
  # re-executed; an effect proven not to have happened re-enters the normal
  # serial lifecycle below instead of parking.
  local rows item step pr head merge_sha errf am rf am_rc
  rows="$WORKTMP/nonterminal"
  python3 "$JOURNAL" read-nonterminal --store "$LQ" --queue-id "$QUEUE_ID" > "$rows" \
    || { echo "QUEUE-ERROR:store"; exit 70; }
  am="$(command -v assert-merged.sh || printf '%s\n' "$SCRIPT_DIR/assert-merged.sh")"
  while IFS= read -r -d '' item && IFS= read -r -d '' step \
      && IFS= read -r -d '' pr && IFS= read -r -d '' head; do
    merge_sha=""
    if [ -n "$pr" ]; then
      # assert-merged.sh is the merged-state authority for the resume path:
      # rc 0 = MERGED, rc 1/2 = provably not landed, anything else = unknown.
      am_rc=0
      "$am" "$pr" || am_rc=$?
      case "$am_rc" in
        0)
          errf="$WORKTMP/err-resume"
          merge_sha="$(gh pr view "$pr" --json mergeCommit -q .mergeCommit.oid 2>"$errf")" || merge_sha=""
          ;;
        1|2) merge_sha="" ;;
        *)
          # Authority unreachable: never guess, never retry a maybe-merged
          # effect — a typed terminal with a one-command unblock.
          journal --kind terminal --step terminal --item "$item" \
            --status "BLOCKED:resume-incomplete" \
            --reason "merge authority unavailable for PR $pr during resume" \
            --unblock "bash scripts/gsd/assert-merged.sh $pr, then re-run --resume $QUEUE_ID"
          continue ;;
      esac
    fi
    if [ -n "$merge_sha" ]; then
      if [ "$step" = "finalize" ]; then
        # 865d06d4: a finalizer intent without a terminal means the merge is
        # already authority-confirmed.  Recovery re-runs the finalizer
        # idempotently and only then appends LANDED — never a second merge.
        rf="$(command -v run-finalizer.sh || printf '%s\n' "$SCRIPT_DIR/run-finalizer.sh")"
        if "$rf" --run-id "$RUN_ID" "$pr"; then
          journal --kind result --step finalize --item "$item" --status reconciled
          journal --kind terminal --step terminal --item "$item" --status LANDED --detail "$merge_sha"
        else
          journal --kind result --step finalize --item "$item" --status failed
          journal --kind terminal --step terminal --item "$item" --status "BLOCKED:finalizer" \
            --reason "recovery finalizer failed for PR $pr" \
            --unblock "bash scripts/gsd/run-finalizer.sh --run-id $RUN_ID $pr"
        fi
      else
        # effect-already-happened: adopt the authority's outcome — no re-merge.
        if [ -n "$step" ]; then
          journal --kind result --step "$step" --item "$item" --status reconciled
        fi
        journal --kind terminal --step terminal --item "$item" --status LANDED --detail "$merge_sha"
      fi
      continue
    fi
    # Effect provably never ran (no recorded key, or the authority denies
    # the merge): close any dangling intent, then re-enter the serial
    # lifecycle for this item instead of parking it (REQ-208).
    if [ -n "$step" ]; then
      journal --kind result --step "$step" --item "$item" --status never-ran
    fi
    RESUME_RETRY+=("$item")
  done < "$rows"
}

RESUMING=0
if [ -n "$RESUME" ]; then
  RESUMING=1
  QUEUE_ID="$RESUME"
  resume_reconcile
  if [ "${#RESUME_RETRY[@]}" -eq 0 ]; then
    # WR-02: every item already terminal — re-run the idempotent grant
    # derivation so a crash between LANDED and minting is recoverable.
    derive_consolidate_grant
    echo "LAND-QUEUE REPORT queue=$QUEUE_ID resumed"
    emit_report
    exit 0
  fi
  # Items whose effects provably did not happen re-enter the normal serial
  # lifecycle as this resumed queue's only inputs; the takeover/estate
  # intake sources stay closed so no new item can join a resumed queue.
  EXPLICIT=(${RESUME_RETRY[@]+"${RESUME_RETRY[@]}"})
else
  # CR-04: the journal binds the creating repository root + base at init;
  # grant-consolidate reads that binding as its sole repo/base authority.
  python3 "$JOURNAL" init --store "$LQ" --queue-id "$QUEUE_ID" --run-id "$RUN_ID" \
      --repo "$REPO" --base "$BASE" \
    || { echo "QUEUE-ERROR:store"; exit 70; }
fi

QUEUE_TERMINAL=""

# 71c46cda: a stale DRAIN marker at startup is honored once, then consumed
# under the held lock.
if bash "$GUARD" drain-consume --store "$LQ" >/dev/null; then
  journal --kind terminal --step terminal --status "QUEUE-DRAINED:operator-drain" \
    --detail "stale drain marker consumed at startup"
  echo "QUEUE-DRAINED:operator-drain"
  exit 0
fi

# ── accessor discipline ───────────────────────────────────────────────────
# Collector/journal data crosses into shell ONLY through the closed scalar /
# NUL-array accessors, captured in owner-private temp files (umask 077) and
# read with quoted `IFS= read -r`.  Never eval, never word-split serialized
# data.

read_scalar() { # $1 doc-file, $2 item-index-or-empty, $3 field
  local tmp value
  tmp="$(mktemp "$WORKTMP/scalar.XXXXXX")"
  if [ -n "$2" ]; then
    python3 "$COLLECTOR" get-scalar --doc "$1" --item "$2" --field "$3" > "$tmp" \
      || { rm -f "$tmp"; return 1; }
  else
    python3 "$COLLECTOR" get-scalar --doc "$1" --field "$3" > "$tmp" \
      || { rm -f "$tmp"; return 1; }
  fi
  IFS= read -r value < "$tmp" || true
  rm -f "$tmp"
  printf '%s\n' "$value"
}

CHANGED_FILES=()
load_changed_files() { # $1 doc-file, $2 item-index
  local tmp value
  tmp="$(mktemp "$WORKTMP/array.XXXXXX")"
  python3 "$COLLECTOR" emit-array0 --doc "$1" --item "$2" --field changed_files > "$tmp" \
    || { rm -f "$tmp"; return 1; }
  CHANGED_FILES=()
  while IFS= read -r -d '' value; do CHANGED_FILES+=("$value"); done < "$tmp"
  rm -f "$tmp"
}

# ── bounded intake: the queue clock starts here ───────────────────────────
QUEUE_STARTED="$(date +%s)"
ITEM_STARTED="$QUEUE_STARTED"
journal --kind intent --step collect --detail "bounded three-source intake"
DOC="$WORKTMP/queue-doc.json"
COLLECT_ARGS=(collect --repo "$REPO" --base "$BASE")
# REQ-201 three-source union: every NEW intake passes the canonical takeover
# record glob (takeover-record.py writes <resolved-store-parent>/takeover/
# <run-id>.json, and STORE_DIR is exactly that resolved parent) plus the
# collect-estate source.  LAND_QUEUE_ESTATE_JSON is a hermetic-test seam that
# feeds the SAME estate source through the collector's --estate-json input.
# A resumed queue re-collects only its own retry items (REQ-208).
if [ "$RESUMING" -eq 0 ]; then
  COLLECT_ARGS+=(--takeover-glob "$STORE_DIR/takeover/*.json")
  if [ -n "${LAND_QUEUE_ESTATE_JSON:-}" ]; then
    COLLECT_ARGS+=(--estate-json "$LAND_QUEUE_ESTATE_JSON")
  else
    COLLECT_ARGS+=(--use-estate)
  fi
fi
for branch in ${EXPLICIT[@]+"${EXPLICIT[@]}"}; do
  COLLECT_ARGS+=(--explicit "$branch")
done
if ! run_bounded 300 python3 "$COLLECTOR" "${COLLECT_ARGS[@]}" > "$DOC" </dev/null; then
  journal --kind result --step collect --status failed
  journal --kind terminal --step terminal --status "QUEUE-ABORTED:systemic:collect" \
    --detail "bounded intake collection failed"
  echo "QUEUE-ABORTED:systemic:collect"
  exit 1
fi
journal --kind result --step collect --status ok
COUNT="$(read_scalar "$DOC" "" count)"
GUARD_ITEMS="$COUNT"
TRUNCATED="$(read_scalar "$DOC" "" truncated)" || TRUNCATED="false"
if [ "$TRUNCATED" = "true" ]; then
  # REQ-201/REQ-204: the collector observed more items than its cap and
  # sliced the list; restore the over-cap truth so the guard's named
  # max-items outcome trips before any lifecycle effect, instead of the
  # queue silently landing a sliced list.
  GUARD_ITEMS=$((COUNT + 1))
fi

# CR-03 (round 2): persist the COMPLETE validated intake manifest atomically
# after collection and before the first item effect.  Resume and grant
# derivation enumerate these declared items and require a terminal for
# every one, so a crash between items can never silently drop an unstarted
# item or mint a partial consolidate grant.  A resumed queue keeps the
# original journal's immutable manifest.
if [ "$RESUMING" -eq 0 ]; then
  MANIFEST_ARGS=(record-manifest --store "$LQ" --queue-id "$QUEUE_ID")
  j=0
  while [ "$j" -lt "$COUNT" ]; do
    MANIFEST_ARGS+=(--item "$(read_scalar "$DOC" "$j" branch)")
    j=$((j + 1))
  done
  python3 "$JOURNAL" "${MANIFEST_ARGS[@]}" \
    || { echo "QUEUE-ERROR:store"; exit 70; }
fi

# e846ec0c: consulted immediately before STARTING every external effect.
require_go() { # $1 step label; nonzero return records a queue terminal
  local verdict
  verdict="$(bash "$GUARD" allow --store "$LQ" --items "$GUARD_ITEMS" \
    --queue-started "$QUEUE_STARTED" --item-started "$ITEM_STARTED" \
    --round "${ITEM_ROUND:-1}")" || true
  case "$verdict" in
    ALLOW) return 0 ;;
    STOP:operator-stop)
      journal --kind terminal --step terminal --status "QUEUE-ABORTED:operator-stop" --detail "$1"
      QUEUE_TERMINAL="QUEUE-ABORTED:operator-stop"
      return 1 ;;
    *)
      # ponytail: wall/cap verdicts map to a queue-level systemic abort in
      # 02-01; the 02-02 failure machine refines per-item wall handling.
      journal --kind terminal --step terminal \
        --status "QUEUE-ABORTED:systemic:${verdict#STOP:}" --detail "$1"
      QUEUE_TERMINAL="QUEUE-ABORTED:systemic:${verdict#STOP:}"
      return 1 ;;
  esac
}

# ── failure classification, no-progress, and breaker accounting ───────────
# REQ-205/REQ-206: every observed effect failure is classified through the
# guard's closed boundary table and recorded durably so consecutive systemic
# failures trip the breaker (6e4616bc: class-agnostic, two consecutive).

record_class() { # $1 class; returns 2 when the breaker trips
  local verdict rrc
  verdict="$(bash "$GUARD" record --store "$LQ" --queue-id "$QUEUE_ID" --class "$1")"
  rrc=$?
  if [ "$rrc" -eq 5 ]; then
    journal --kind terminal --step terminal --status "$verdict" \
      --detail "two consecutive systemic failures (6e4616bc)"
    QUEUE_TERMINAL="$verdict"
    return 2
  fi
  [ "$rrc" -eq 0 ] || echo "QUEUE-GUARD-WARN: record rc $rrc" >&2
  return 0
}

# ── typed terminal writer (REQ-214) ───────────────────────────────────────
# EVERY item terminal flows through term_item: LANDED carries the merge SHA
# as detail; every non-LANDED terminal carries a separate nonempty reason and
# a one-command unblock. Resume, precheck, guard, and abort paths share it.
LAST_TERMINAL_STATUS=""
term_item() { # $1 status, $2 detail, $3 reason, $4 unblock — for CURRENT item
  term_for "$ITEM_BRANCH" "$@"
}
term_for() { # $1 item, $2 status, $3 detail, $4 reason, $5 unblock
  local args=(--kind terminal --step terminal --item "$1" --status "$2")
  [ -n "${3:-}" ] && args+=(--detail "$3")
  if [ "$2" != "LANDED" ]; then
    args+=(--reason "${4:-$2}")
    args+=(--unblock "${5:-re-run scripts/gsd/land-queue.sh}")
  fi
  journal "${args[@]}"
  LAST_TERMINAL_STATUS="$2"
}

block_item() { # $1 status, $2 reason, $3 unblock — an item-local terminal
  term_item "$1" "" "$2" "$3"
  record_class local
}

fail_item() { # $1 boundary, $2 rc, $3 stderr-file, $4 gate, $5 status, $6 unblock
  # Returns 1 (item terminal, queue continues) or 2 (breaker tripped).
  local class np_rc status="$5" reason
  cat -- "$3" >&2 || true
  reason="$(head -n 1 -- "$3" 2>/dev/null | head -c 200 | tr -d '\0\r')"
  [ -n "$reason" ] || reason="$1 boundary failed rc $2"
  class="$(bash "$GUARD" classify-subprocess --boundary "$1" --rc "$2" \
    --stderr-file "$3" 2>/dev/null)" || class=local
  bash "$GUARD" note-failure --queue-id "$QUEUE_ID" --item "$ITEM_BRANCH" \
    --gate "$4" --stderr-file "$3" >/dev/null 2>"$WORKTMP/np.err"
  np_rc=$?
  if [ "$np_rc" -eq 6 ]; then status="BLOCKED:no-progress"; fi
  if [ "$np_rc" -eq 75 ]; then cat -- "$WORKTMP/np.err" >&2 || true; class="store-error"; fi
  case "$class" in
    reviewer-unreachable|store-error|gh-auth|network) status="BLOCKED:$class" ;;
  esac
  term_item "$status" "" "$reason" "$6"
  record_class "$class" || return 2
  return 1
}

# PATH-004: after a systemic queue abort every untouched item still gets a
# durable, reportable terminal instead of silently vanishing.
materialize_skipped() { # $1 first untouched item index
  local j branch
  j="$1"
  while [ "$j" -lt "$COUNT" ]; do
    branch="$(read_scalar "$DOC" "$j" branch)" || break
    term_for "$branch" "SKIPPED:queue-aborted" "" \
      "queue aborted before this item started" \
      "re-run scripts/gsd/land-queue.sh after clearing the systemic failure"
    j=$((j + 1))
  done
}

base_sha() { git -C "$REPO" ls-remote origin "refs/heads/$BASE" 2>/dev/null | cut -f1; }

# REQ-209: every review invocation is durably recorded against gates.py with
# its full Git binding. gates.py recomputes the changed-file set itself from
# the merge-base diff (5d794fab) — the collector's list rides along advisory
# and widen-only.
note_review_invocation() { # $1 degraded true|false, $2 branch, $3 head, $4 baseline
  local inv args f
  inv="review-$QUEUE_ID-$IDX-r${ITEM_ROUND:-1}"
  args=(note-degraded invocation --run-id "$RUN_ID" --seam land-queue-review
    --degraded "$1" --invocation-id "$inv")
  if [ -n "$4" ]; then
    args+=(--repo "$REPO" --branch "$2" --head "$3" --baseline "$4")
    for f in ${CHANGED_FILES[@]+"${CHANGED_FILES[@]}"}; do
      args+=(--changed-file "$f")
    done
  fi
  python3 "$GATES" "${args[@]}" >/dev/null
}

drop_worktree() { # $1 worktree path
  git -C "$REPO" worktree remove --force "$1" >/dev/null 2>&1 || true
}

land_one_item() {
  # Returns 0 LANDED, 1 item terminal recorded (queue continues),
  # 2 queue terminal recorded (queue stops).  Runs with errexit inert
  # (invoked as an `if` condition), so every failure is checked explicitly.
  local branch="$ITEM_BRANCH" head="$ITEM_HEAD" spec_id="$ITEM_SPEC"
  local probe status reason unblock merge_sha wt prepared pr remote_head reviewed
  local now_head reviewer fi_bin am rf ci_rc errf eff_rc
  local baseline findings ci_state rollup_count mergeable

  # ponytail: v1 runs exactly one round per item; the guard still sees the
  # real round number, and REQ-204's cap trips if a retry loop ever appears.
  require_go "round $ITEM_ROUND $branch" || return 2

  # 1. item-start precheck — the SAME collector code path as intake, never a
  #    shell reimplementation; authority-only, zero model calls.
  journal --kind intent --step precheck --item "$branch" \
    --detail "files=${#CHANGED_FILES[@]}"
  probe="$WORKTMP/precheck-$IDX.json"
  if ! python3 "$COLLECTOR" precheck --repo "$REPO" --base "$BASE" \
      --branch "$branch" --head "$head" > "$probe"; then
    journal --kind result --step precheck --item "$branch" --status error
    block_item "BLOCKED:precheck-error" "collect-queue.py precheck failed for $branch" \
      "python3 skills/land-queue/scripts/collect-queue.py precheck --repo $REPO --base $BASE --branch $branch"
    return 1
  fi
  status="$(read_scalar "$probe" "" status)" || status=""
  journal --kind result --step precheck --item "$branch" --status "$status"
  case "$status" in
    OK) ;;
    LANDED)
      # EDGE-005: an external landing reconciles here with authority proof.
      merge_sha="$(read_scalar "$probe" "" merge_sha)" || merge_sha=""
      term_item "LANDED" "$merge_sha" "" ""
      record_class success
      return 1 ;;
    SKIPPED:*|BLOCKED:*)
      reason="$(read_scalar "$probe" "" reason)" || reason=""
      unblock="$(read_scalar "$probe" "" unblock)" || unblock=""
      block_item "$status" "$reason" "$unblock"
      return 1 ;;
    *)
      block_item "BLOCKED:precheck-error" "unrecognized precheck status" \
        "python3 skills/land-queue/scripts/collect-queue.py precheck --repo $REPO --base $BASE --branch $branch"
      return 1 ;;
  esac

  # 2. rebase onto the fetched base, in a private worktree, with real Git.
  require_go "rebase $branch" || return 2
  journal --kind intent --step rebase --item "$branch"
  wt="$WORKTMP/wt-$IDX"
  if ! git -C "$REPO" worktree add -q "$wt" "$branch"; then
    journal --kind result --step rebase --item "$branch" --status error
    block_item "BLOCKED:worktree" "git worktree add failed for $branch" \
      "git -C $REPO worktree add <dir> $branch"
    return 1
  fi
  if ! git -C "$wt" -c rebase.autostash=true rebase -q "origin/$BASE"; then
    git -C "$wt" rebase --abort >/dev/null 2>&1 || true
    journal --kind result --step rebase --item "$branch" --status conflict
    block_item "BLOCKED:conflict" "rebase of $branch onto origin/$BASE conflicts" \
      "git -C $REPO rebase origin/$BASE $branch"
    drop_worktree "$wt"
    return 1
  fi
  journal --kind result --step rebase --item "$branch" --status ok

  # 3. autonomous implementation child.
  require_go "implement $branch" || { drop_worktree "$wt"; return 2; }
  journal --kind intent --step implement --item "$branch"
  fi_bin="$(command -v feature-implement || true)"
  if [ -z "$fi_bin" ]; then
    journal --kind result --step implement --item "$branch" --status missing
    block_item "BLOCKED:implement-missing" "no feature-implement dispatcher on PATH" \
      "install a feature-implement dispatcher on PATH, then re-run the queue"
    drop_worktree "$wt"
    return 1
  fi
  errf="$WORKTMP/err-implement-$IDX"
  (cd "$wt" && "$fi_bin" "$spec_id" --autonomous) 2>"$errf"
  eff_rc=$?
  if [ "$eff_rc" -ne 0 ]; then
    journal --kind result --step implement --item "$branch" --status failed
    drop_worktree "$wt"
    fail_item implement "$eff_rc" "$errf" implement "BLOCKED:implement" \
      "re-run feature-implement $spec_id --autonomous and fix its failure"
    return $?
  fi
  journal --kind result --step implement --item "$branch" --status ok

  # 4. f1bc7cad: push the prepared local head to the PR branch BEFORE
  #    review/CI; the reviewed OID must equal the pushed sha AND the PR head.
  require_go "push $branch" || { drop_worktree "$wt"; return 2; }
  journal --kind intent --step push --item "$branch"
  prepared="$(git -C "$wt" rev-parse HEAD)"
  errf="$WORKTMP/err-push-$IDX"
  git -C "$wt" push -q --force-with-lease origin "HEAD:refs/heads/$branch" 2>"$errf"
  eff_rc=$?
  if [ "$eff_rc" -ne 0 ]; then
    journal --kind result --step push --item "$branch" --status failed
    drop_worktree "$wt"
    fail_item git "$eff_rc" "$errf" push "BLOCKED:push" \
      "git push --force-with-lease origin HEAD:refs/heads/$branch failed"
    return $?
  fi
  journal --kind result --step push --item "$branch" --status ok
  drop_worktree "$wt"

  errf="$WORKTMP/err-prview-$IDX"
  pr="$(gh pr view "$branch" --json number -q .number 2>"$errf")"
  eff_rc=$?
  if [ "$eff_rc" -ne 0 ]; then
    fail_item gh "$eff_rc" "$errf" pr-view "BLOCKED:pr-missing" \
      "open a PR for $branch, then re-run the queue"
    return $?
  fi
  errf="$WORKTMP/err-prhead-$IDX"
  remote_head="$(gh pr view "$pr" --json headRefOid -q .headRefOid 2>"$errf")"
  eff_rc=$?
  if [ "$eff_rc" -ne 0 ]; then
    fail_item gh "$eff_rc" "$errf" pr-head "BLOCKED:pr-head" \
      "gh pr view $pr --json headRefOid failed"
    return $?
  fi
  if [ "$remote_head" != "$prepared" ]; then
    block_item "BLOCKED:head-drift" "PR $pr head $remote_head is not the pushed $prepared" \
      "re-run the queue so review pins the current PR head"
    return 1
  fi
  reviewed="$prepared"

  # 5. cross-vendor review of the pinned head (REQ-209 / 37bc43d9 / CR-05).
  require_go "review $branch" || return 2
  journal --kind intent --step review --item "$branch" --detail "$reviewed"
  # CR-05: only the OPPOSITE vendor's CLI satisfies floor; under zero a
  # same-vendor CLI may still review but is recorded as DEGRADED.
  local review_degraded=false
  reviewer="$(command -v "$OPPOSITE_VENDOR" || true)"
  if [ -z "$reviewer" ] && [ "$AUTONOMY_POSTURE" != "floor" ]; then
    reviewer="$(command -v codex || command -v claude || true)"
    [ -z "$reviewer" ] || review_degraded=true
  fi
  baseline="$(git -C "$REPO" rev-parse "refs/remotes/origin/$BASE" 2>/dev/null)" || baseline=""
  if [ -z "$reviewer" ]; then
    journal --kind result --step review --item "$branch" --status missing
    # e846ec0c: STOP is consulted before the degradation-recording effect.
    require_go "degrade $branch" || return 2
    if [ "$AUTONOMY_POSTURE" = "floor" ]; then
      # Floor: no same-host substitute, ever — the degraded posture is
      # recorded AND the item blocks (37bc43d9/CR-05), whether the host's
      # own CLI is installed or not.
      note_review_invocation true "$branch" "$reviewed" "$baseline" || true
      block_item "BLOCKED:no-cross-vendor-reviewer" \
        "floor posture requires an opposite-vendor ($OPPOSITE_VENDOR) reviewer and none is on PATH; the producing host's own CLI never counts" \
        "install the opposite-vendor reviewer CLI ($OPPOSITE_VENDOR), then re-run the queue"
      return 1
    fi
    # Zero: merge stays permitted ONLY with a durably recorded degradation
    # event bound to run/invocation/branch/head/baseline; an unrecordable
    # degradation fails closed rather than silently raising privilege.
    if ! note_review_invocation true "$branch" "$reviewed" "$baseline"; then
      block_item "BLOCKED:degradation-unrecorded" \
        "zero posture requires a recorded degradation event and gates.py refused it" \
        "re-run with a ledger-shaped --run-id (spec-NNN, run-N, or adhoc-*)"
      return 1
    fi
    journal --kind result --step review --item "$branch" --status degraded
  else
    errf="$WORKTMP/err-review-$IDX"
    findings="$LQ/reviews/$QUEUE_ID-pr-$pr.txt"
    mkdir -p "$LQ/reviews"
    run_bounded 900 "$reviewer" review "$pr" "$reviewed" </dev/null >"$findings" 2>"$errf"
    eff_rc=$?
    if [ "$eff_rc" -ne 0 ]; then
      journal --kind result --step review --item "$branch" --status failed
      fail_item reviewer "$eff_rc" "$errf" review "BLOCKED:review" \
        "address reviewer findings for PR $pr, then re-run the queue"
      return $?
    fi
    # 37bc43d9: a satisfied review is rc 0 PLUS its findings artifact.
    journal --kind result --step review --item "$branch" --status ok --detail "$findings"
    note_review_invocation "$review_degraded" "$branch" "$reviewed" "$baseline" || true
  fi

  # 6. bounded CI watch — the exact REQ-210 contract.
  require_go "ci $branch" || return 2
  journal --kind intent --step ci --item "$branch"
  errf="$WORKTMP/err-ci-$IDX"
  run_bounded 1200 gh pr checks "$pr" --watch --interval 10 </dev/null 2>"$errf"
  ci_rc=$?
  if [ "$ci_rc" -ne 0 ]; then
    if [ "$ci_rc" -eq 124 ]; then
      journal --kind result --step ci --item "$branch" --status timeout
      fail_item ci-watch "$ci_rc" "$errf" ci "BLOCKED:ci-timeout" \
        "gh pr checks $pr --watch (bounded 1200s) timed out"
    else
      journal --kind result --step ci --item "$branch" --status failed
      fail_item ci-watch "$ci_rc" "$errf" ci "BLOCKED:ci" \
        "fix failing checks on PR $pr, then re-run the queue"
    fi
    return $?
  fi
  # REQ-210: a green watch alone is not enough — the rollup must be nonempty
  # and the PR must not be CONFLICTING before merge may be considered.
  errf="$WORKTMP/err-cistate-$IDX"
  ci_state="$(gh pr view "$pr" --json statusCheckRollup,mergeable \
    -q '(.statusCheckRollup | length | tostring) + " " + .mergeable' 2>"$errf")"
  eff_rc=$?
  if [ "$eff_rc" -ne 0 ]; then
    journal --kind result --step ci --item "$branch" --status error
    fail_item gh "$eff_rc" "$errf" ci-state "BLOCKED:ci-state" \
      "gh pr view $pr --json statusCheckRollup,mergeable failed"
    return $?
  fi
  rollup_count="${ci_state%% *}"
  mergeable="${ci_state#* }"
  if [ "$rollup_count" = "0" ]; then
    journal --kind result --step ci --item "$branch" --status empty
    block_item "BLOCKED:ci-empty" "PR $pr reports an empty check suite" \
      "configure required checks for PR $pr, then re-run the queue"
    return 1
  fi
  if [ "$mergeable" = "CONFLICTING" ]; then
    journal --kind result --step ci --item "$branch" --status conflict
    block_item "BLOCKED:merge-conflict" "PR $pr is CONFLICTING against $BASE" \
      "git -C $REPO rebase origin/$BASE $branch, then re-run the queue"
    return 1
  fi
  journal --kind result --step ci --item "$branch" --status ok

  # 7. pre-merge precheck — the same collector code path, again.
  journal --kind intent --step precheck-merge --item "$branch"
  probe="$WORKTMP/precheck-merge-$IDX.json"
  if ! python3 "$COLLECTOR" precheck --repo "$REPO" --base "$BASE" \
      --branch "$branch" --head "$reviewed" > "$probe"; then
    journal --kind result --step precheck-merge --item "$branch" --status error
    block_item "BLOCKED:precheck-error" "pre-merge collect-queue.py precheck failed" \
      "python3 skills/land-queue/scripts/collect-queue.py precheck --repo $REPO --base $BASE --branch $branch"
    return 1
  fi
  status="$(read_scalar "$probe" "" status)" || status=""
  journal --kind result --step precheck-merge --item "$branch" --status "$status"
  case "$status" in
    OK) ;;
    LANDED)
      merge_sha="$(read_scalar "$probe" "" merge_sha)" || merge_sha=""
      term_item "LANDED" "$merge_sha" "" ""
      record_class success
      return 1 ;;
    SKIPPED:*|BLOCKED:*)
      reason="$(read_scalar "$probe" "" reason)" || reason=""
      unblock="$(read_scalar "$probe" "" unblock)" || unblock=""
      block_item "$status" "$reason" "$unblock"
      return 1 ;;
    *)
      block_item "BLOCKED:precheck-error" "unrecognized pre-merge precheck status" \
        "python3 skills/land-queue/scripts/collect-queue.py precheck --repo $REPO --base $BASE --branch $branch"
      return 1 ;;
  esac

  # 8. head stability re-read: any drift is a typed abort — never a re-pin
  #    under the existing grant (f1bc7cad).
  errf="$WORKTMP/err-prhead2-$IDX"
  now_head="$(gh pr view "$pr" --json headRefOid -q .headRefOid 2>"$errf")"
  eff_rc=$?
  if [ "$eff_rc" -ne 0 ]; then
    fail_item gh "$eff_rc" "$errf" pr-head "BLOCKED:pr-head" \
      "gh pr view $pr --json headRefOid failed"
    return $?
  fi
  if [ "$now_head" != "$reviewed" ]; then
    # REQ-210: never re-pin to the moved head under the existing grant.
    block_item "BLOCKED:head-moved" "PR $pr head moved to $now_head after review of $reviewed" \
      "re-run the queue so the new head is reviewed before merge"
    return 1
  fi

  # 9. exact grant, pinned merge, merged assertion, finalization.
  require_go "merge $branch" || return 2
  journal --kind intent --step grant --item "$branch" --detail "merge:pr-$pr"
  if ! python3 "$GATES" check-grant "$RUN_ID" --action "merge:pr-$pr" >/dev/null; then
    journal --kind result --step grant --item "$branch" --status missing
    block_item "BLOCKED:grant-missing" "no exact merge grant for merge:pr-$pr in run $RUN_ID" \
      "python3 lib/gates.py grant $RUN_ID --action merge:pr-$pr --reason '<operator reason>'"
    return 1
  fi
  journal --kind result --step grant --item "$branch" --status ok

  journal --kind intent --step merge --item "$branch" --pr "$pr" --head "$reviewed"
  errf="$WORKTMP/err-merge-$IDX"
  gh pr merge "$pr" --squash --match-head-commit "$reviewed" 2>"$errf"
  eff_rc=$?
  if [ "$eff_rc" -ne 0 ]; then
    journal --kind result --step merge --item "$branch" --status failed
    fail_item gh "$eff_rc" "$errf" merge "BLOCKED:merge" \
      "inspect gh pr merge output for PR $pr, then re-run the queue"
    return $?
  fi
  journal --kind result --step merge --item "$branch" --status ok

  am="$(command -v assert-merged.sh || printf '%s\n' "$SCRIPT_DIR/assert-merged.sh")"
  if ! "$am" "$pr"; then
    block_item "BLOCKED:not-merged" "assert-merged.sh does not confirm PR $pr as MERGED" \
      "bash scripts/gsd/assert-merged.sh $pr"
    return 1
  fi
  if ! merge_sha="$(gh pr view "$pr" --json mergeCommit -q .mergeCommit.oid)"; then
    block_item "BLOCKED:merge-sha" "gh pr view $pr --json mergeCommit failed" \
      "gh pr view $pr --json mergeCommit -q .mergeCommit.oid"
    return 1
  fi
  if [ -z "$merge_sha" ]; then
    block_item "BLOCKED:merge-sha" "PR $pr reports no merge commit" \
      "gh pr view $pr --json mergeCommit -q .mergeCommit.oid"
    return 1
  fi

  require_go "finalize $branch" || return 2
  # 865d06d4: the finalizer intent carries the idempotency key so crash
  # recovery can complete it and only then append LANDED.
  journal --kind intent --step finalize --item "$branch" --pr "$pr" --head "$reviewed"
  rf="$(command -v run-finalizer.sh || printf '%s\n' "$SCRIPT_DIR/run-finalizer.sh")"
  if ! "$rf" --run-id "$RUN_ID" "$pr"; then
    journal --kind result --step finalize --item "$branch" --status failed
    block_item "BLOCKED:finalizer" "run-finalizer failed for PR $pr" \
      "bash scripts/gsd/run-finalizer.sh --run-id $RUN_ID $pr"
    return 1
  fi
  journal --kind result --step finalize --item "$branch" --status ok

  # LANDED only after finalization completes; success resets the breaker.
  term_item "LANDED" "$merge_sha" "" ""
  record_class success
  return 0
}

# ── strictly serial item loop ─────────────────────────────────────────────
QUAR_IDX=()
QUAR_BASE=()
i=0
while [ "$i" -lt "$COUNT" ]; do
  IDX="$i"
  i=$((i + 1))

  # 71c46cda: DRAIN honored once at the item boundary, consumed under the lock.
  if bash "$GUARD" drain-consume --store "$LQ" >/dev/null; then
    journal --kind terminal --step terminal --status "QUEUE-DRAINED:operator-drain" \
      --detail "drained at item boundary before index $IDX"
    QUEUE_TERMINAL="QUEUE-DRAINED:operator-drain"
    break
  fi

  ITEM_BRANCH="$(read_scalar "$DOC" "$IDX" branch)"
  ITEM_HEAD="$(read_scalar "$DOC" "$IDX" head)"
  ITEM_SPEC="$(read_scalar "$DOC" "$IDX" spec_id)" || ITEM_SPEC="$ITEM_BRANCH"
  load_changed_files "$DOC" "$IDX" || CHANGED_FILES=()
  ITEM_STARTED="$(date +%s)"
  ITEM_ROUND=1

  if ! require_go "item $ITEM_BRANCH"; then
    case "$QUEUE_TERMINAL" in
      QUEUE-ABORTED:systemic:*) materialize_skipped "$IDX" ;;
    esac
    break
  fi

  if land_one_item; then
    :
  else
    item_rc=$?
    if [ "$item_rc" -eq 2 ]; then
      case "$QUEUE_TERMINAL" in
        QUEUE-ABORTED:systemic:*) materialize_skipped "$i" ;;
      esac
      break
    fi
    # EDGE-010: a conflict quarantine is eligible for exactly one
    # zero-posture requeue after the base advances.
    if [ "$AUTONOMY_POSTURE" = "zero" ] && [ "$LAST_TERMINAL_STATUS" = "BLOCKED:conflict" ]; then
      QUAR_IDX+=("$IDX")
      QUAR_BASE+=("$(base_sha)")
    fi
  fi
done

# ── EDGE-010: bounded quarantine requeue (zero posture only) ──────────────
# Requeue once, only when the base advanced since the quarantine, and only
# while the durable journal counter shows fewer than two conflict terminals
# — the second quarantine parks permanently.
if [ -z "$QUEUE_TERMINAL" ] && [ "$AUTONOMY_POSTURE" = "zero" ] && [ "${#QUAR_IDX[@]}" -gt 0 ]; then
  k=0
  while [ "$k" -lt "${#QUAR_IDX[@]}" ]; do
    IDX="${QUAR_IDX[$k]}"
    quar_base="${QUAR_BASE[$k]}"
    k=$((k + 1))
    now_base="$(base_sha)"
    [ -n "$now_base" ] && [ "$now_base" != "$quar_base" ] || continue
    ITEM_BRANCH="$(read_scalar "$DOC" "$IDX" branch)"
    conflicts="$(python3 "$JOURNAL" count-terminals --store "$LQ" --queue-id "$QUEUE_ID" \
      --item "$ITEM_BRANCH" --status "BLOCKED:conflict")" || conflicts=2
    [ "$conflicts" -lt 2 ] || continue
    ITEM_HEAD="$(read_scalar "$DOC" "$IDX" head)"
    ITEM_SPEC="$(read_scalar "$DOC" "$IDX" spec_id)" || ITEM_SPEC="$ITEM_BRANCH"
    load_changed_files "$DOC" "$IDX" || CHANGED_FILES=()
    ITEM_STARTED="$(date +%s)"
    ITEM_ROUND=2
    require_go "requeue $ITEM_BRANCH" || break
    if ! land_one_item; then
      item_rc=$?
      if [ "$item_rc" -eq 2 ]; then
        break
      fi
    fi
  done
fi

if [ -z "$QUEUE_TERMINAL" ]; then
  derive_consolidate_grant
fi

# ── report ────────────────────────────────────────────────────────────────
echo "LAND-QUEUE REPORT queue=$QUEUE_ID items=$COUNT"
emit_report

if [ -n "$QUEUE_TERMINAL" ]; then
  echo "$QUEUE_TERMINAL"
  case "$QUEUE_TERMINAL" in
    QUEUE-DRAINED:*) exit 0 ;;
    *) exit 1 ;;
  esac
fi
exit 0
