#!/usr/bin/env bash
# run-finalizer.sh — run-end estate cleanup after a verified merge.
#
# The 2026-07-30 estate audit found 95 branches (~30 orphaned gsd/phase-*)
# and 38 worktrees (26 dirty) because nothing runs at run end: the finish
# tail stopped at merge. This lever closes the loop — call it from the
# finish tail AFTER assert-merged.sh exits 0.
#
# Safety model:
# - Destructive steps run ONLY under machine proof: gh reports the PR
#   MERGED, and a branch is deleted only when `git branch -d` accepts it
#   OR its tip is (an ancestor of) the merged PR's headRefOid — i.e. the
#   exact content GitHub recorded as landed (squash merges make -d refuse
#   even for landed branches; the headRefOid check is the squash-safe
#   equivalent of -d, never a blind -D).
# - A branch whose tip moved past the merged head is NEVER deleted.
# - A dirty worktree is NEVER removed — it is routed to /adopt-wip.
# - Before a worktree is removed, its .feature-fix-swarm/ ledger (gates
#   evidence.json + learnings-archive.jsonl, PLUS an in-worktree
#   $GATES_STORE file living outside .feature-fix-swarm/) is COPIED to a
#   durable, run-keyed archive OUTSIDE the worktree (archive_run_ledger) —
#   gates.py resolves its evidence store relative to CWD, so a worktree's
#   ledger is otherwise destroyed by `git worktree remove`. The archive is
#   complete-or-refuse: any doubt about fidelity (an unreadable source, a
#   symlink anywhere in the source tree or the destination, a destination
#   that resolves outside the archive root, a listing/size mismatch after
#   copy) WARNS and refuses rather than risk a partial copy — the worktree
#   is KEPT (preserving unarchivable data beats cleanup).
# - Run-state clearing touches three fixed files only; a denylist refuses
#   anything under .feature-fix-swarm/ or named evidence.json (the gates
#   ledger). archive_run_ledger only READS+COPIES the SOURCE ledger — it
#   never deletes it. It DOES delete exactly one thing: on a post-claim
#   failure, the partial DESTINATION leaf it just created THIS run
#   (_cleanup_partial_dest) — otherwise a failed archive would permanently
#   ratchet every future run through ever-larger disambiguation suffixes.
#   That deletion is guarded by its own strict containment re-check
#   (absolute path, real non-symlink dir, exact match to this run's own
#   claimed leaf) at the delete site, not by deny() — deny() guards the
#   source ledger's denylist, a different (and inverse) contract.
# - Fail-soft: every step warns and continues; ALWAYS exits 0. A cleanup
#   failure must never block, un-merge, or fail a finished run.
#
# Usage: run-finalizer.sh [--dry-run] <pr-number> [<owner/repo>]
# Kill-switch: FFS_RUN_FINALIZER=off
# Env: FFS_LEDGER_ARCHIVE_DIR overrides the ledger archive root (default:
#      <main-repo-root>/.feature-fix-swarm/archive). Must be absolute. If it
#      already exists (however it's reached, including through a normal
#      OS-level mount alias like macOS's /tmp -> /private/tmp or $TMPDIR
#      under /var/folders/...) it is physically resolved (`cd`+`pwd -P`) and
#      trusted as-is; if it doesn't exist yet, the deepest existing ancestor
#      is physically resolved and only the NOT-yet-existing remainder is
#      symlink-checked segment by segment (an attacker-planted symlink
#      there still refuses; a mount alias reached along the way does not).
#      GATES_STORE, if set to a regular file inside the worktree but outside
#      .feature-fix-swarm/, is archived alongside it (preserving its
#      worktree-relative path, namespaced under "_worktree/").
# Exit: always 0.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/lib-lock.sh"
note() { echo "[run-finalizer] $*"; }
warn() { echo "[run-finalizer] WARN: $*" >&2; }

# Flags are position-free and junk is refused loudly: a positional-only
# --dry-run turned a stray arg into `--repo --dry-run`, which failed gh and
# skipped cleanup SILENTLY (observed on the first live run, PR #62).
DRY=0
PR=""
REPO=""
FINALIZER_RUN_ID=""
expect_run_id=0
for arg in "$@"; do
  if [ "$expect_run_id" -eq 1 ]; then FINALIZER_RUN_ID="$arg"; expect_run_id=0; continue; fi
  case "$arg" in
    --dry-run) DRY=1 ;;
    --run-id) expect_run_id=1 ;;
    -*) warn "unknown argument '$arg' — skipping"; exit 0 ;;
    *) if [ -z "$PR" ]; then PR="$arg"
       elif [ -z "$REPO" ]; then REPO="$arg"
       else warn "unexpected extra argument '$arg' — skipping"; exit 0; fi ;;
  esac
done
[ "$expect_run_id" -eq 0 ] || { warn "--run-id requires a value"; exit 78; }
REPO_ARGS=()
[ -n "$REPO" ] && REPO_ARGS=(--repo "$REPO")

if [ "${FFS_RUN_FINALIZER:-on}" = "off" ]; then
  note "disabled via FFS_RUN_FINALIZER=off — skipping"
  exit 0
fi
if [ -z "$PR" ]; then
  warn "usage: run-finalizer.sh [--dry-run] <pr-number> [<owner/repo>] — skipping"
  exit 0
fi

# The dry run is mutation-free and deliberately does not contend on the real
# per-user finisher lock.  Every non-dry invocation owns or visibly yields
# before proof/cleanup can mutate repository state.
FINISHER_LOCK="$HOME/.cache/feature-fix-swarm/finisher.lock"
FINISHER_RECLAIM="$HOME/.cache/feature-fix-swarm/finisher.reclaim"
FINISHER_MACHINE="$(hostname 2>/dev/null || uname -n)"
FINISHER_OWNED=0
release_finisher_lock() { [ "$FINISHER_OWNED" -eq 1 ] && ffs_lock_release "$FINISHER_LOCK" "$FINISHER_MACHINE" || true; }
# A signaled owner must TERMINATE, never resume: with a release-only trap,
# bash runs the handler and then continues after the interrupted command, so
# the first finalizer kept doing cleanup WITHOUT ownership while a second
# acquired the lock (02-VERIFICATION.md GAP 3). The handler clears every
# trap (no re-entry), surrenders ownership, and re-raises the same signal at
# this shell's own pid so the process dies by the signal it received —
# status 128+N, exactly what the script did before any trap existed. The
# always-0 tail contract is unaffected: the lock phase runs strictly before
# the first cleanup mutation.
die_by_signal() {
  trap - EXIT HUP INT TERM
  release_finisher_lock
  kill -s "$1" $$
}
if [ "$DRY" -eq 0 ]; then
  [ ! -L "$HOME/.cache/feature-fix-swarm" ] || { warn "finisher lock directory is symlinked"; exit 78; }
  mkdir -p "$HOME/.cache/feature-fix-swarm" || { warn "could not create finisher lock directory"; exit 78; }
  trap release_finisher_lock EXIT
  trap 'die_by_signal HUP' HUP
  trap 'die_by_signal INT' INT
  trap 'die_by_signal TERM' TERM
  wait_secs="${FINISHER_LOCK_WAIT:-60}"
  case "$wait_secs" in *[!0-9]*|'') wait_secs=60 ;; esac
  started="$(date +%s)"
  while :; do
    lock_rc=0
    ffs_lock_acquire "$FINISHER_LOCK" "" "$FINISHER_RECLAIM" "$FINISHER_MACHINE" 120 30 "run-finalizer" || lock_rc=$?
    if [ "$lock_rc" -eq 0 ]; then FINISHER_OWNED=1; break; fi
    [ "$lock_rc" -eq 75 ] || { warn "finisher lock refused (rc=$lock_rc)"; exit 78; }
    [ $(( $(date +%s) - started )) -lt "$wait_secs" ] || {
      event_id="${FINALIZER_RUN_ID:-${GSD_RUN_ID:-}}"
      [ -n "$event_id" ] || event_id="unattributed"
      event_args=(finisher-skipped --run-id "$event_id" --pr "$PR")
      if [ "$event_id" = unattributed ]; then
        holder_pid="$(head -1 "$FINISHER_LOCK" 2>/dev/null | tr -d '[:space:]' || true)"
        event_args+=(--lock-path "$FINISHER_LOCK" --holder-pid "$holder_pid")
      fi
      PYTHONPATH="$SCRIPT_DIR/../../lib${PYTHONPATH:+:$PYTHONPATH}" python3 "$SCRIPT_DIR/../../lib/evidence_events.py" "${event_args[@]}" || { warn "finisher-skipped event write failed"; exit 78; }
      note "finisher-skipped run_id=$event_id pr=$PR"
      exit 0
    }
    sleep 1
    unset lock_rc
  done
fi

run() { # execute a step, or print it under --dry-run; warn-and-continue on failure
  if [ "$DRY" -eq 1 ]; then note "DRY: $*"; return 0; fi
  "$@" || { warn "step failed (continuing): $*"; return 1; }
}

deny() { # refuse denylisted paths (gates evidence ledger)
  case "$1" in
    *".feature-fix-swarm"*|*"evidence.json"*)
      warn "denylisted path '$1' — refusing to touch"; return 1 ;;
  esac
  return 0
}

INFO="$(gh pr view "$PR" ${REPO_ARGS[@]+"${REPO_ARGS[@]}"} \
  --json state,headRefName,headRefOid \
  --jq '.state + " " + .headRefName + " " + .headRefOid' 2>/dev/null)" || {
  warn "gh pr view $PR failed — no merge proof, doing nothing"
  exit 0
}
read -r STATE BRANCH HEAD_OID <<<"$INFO"
if [ "$STATE" != "MERGED" ] || [ -z "${BRANCH:-}" ] || [ -z "${HEAD_OID:-}" ]; then
  warn "PR #$PR state='$STATE' (need MERGED + head ref) — doing nothing"
  exit 0
fi
note "PR #$PR MERGED — finalizing branch '$BRANCH' (merged head $HEAD_OID)"

_sanitize_slug() { # keep [A-Za-z0-9._-]; collapse any run of 2+ dots to '-'
  # (charset alone still permits a literal ".." path-traversal segment since
  # '.' is allowed; the run-collapse is the explicit defense against that)
  printf '%s' "$1" | sed -E 's/[^A-Za-z0-9._-]/-/g; s/\.{2,}/-/g'
}

_scan_source_tree() { # $1=dir root; sets SCAN_ERR; returns 1 on anomaly/enum failure
  # Refuses (rather than guesses) on anything that isn't a plain file or
  # directory: a symlink, a socket/fifo/device, a path containing a literal
  # newline (would corrupt the sorted-listing comparison in _manifest_of),
  # or an enumeration failure (permission error mid-tree). The temp file is
  # cleaned up via a RETURN trap rather than an `rm` at each early-exit site,
  # so no path ever reads from and unlinks the same fd in one statement.
  local root="$1" entry tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/ffs-scan.XXXXXX" 2>/dev/null)" || {
    SCAN_ERR="mktemp failed"; return 1; }
  # Single-quoted so $tmp expands at trap-FIRE time (inside a real double-quoted
  # context, which safely protects any characters $tmp's value may contain,
  # including a literal single quote from a hostile $TMPDIR) rather than at
  # trap-SET time — a double-quoted trap string expands $tmp immediately into
  # the STORED command text, and a single quote embedded in that text breaks
  # the stored command's own quoting when bash re-parses it at fire time.
  # Cleared immediately after firing so it never lingers past this call.
  trap 'rm -f "$tmp"; trap - RETURN' RETURN
  if ! find "$root" -print0 > "$tmp" 2>/dev/null; then
    SCAN_ERR="enumeration failed under '$root' (permission error?)"; return 1
  fi
  while IFS= read -r -d '' entry; do
    case "$entry" in
      *$'\n'*) SCAN_ERR="path contains a newline character"; return 1 ;;
    esac
    if [ -L "$entry" ]; then
      SCAN_ERR="symlink at '$entry'"; return 1
    fi
    if [ ! -f "$entry" ] && [ ! -d "$entry" ]; then
      SCAN_ERR="non-regular entry (socket/fifo/device?) at '$entry'"; return 1
    fi
  done < "$tmp"
  return 0
}

_checksum_of() { # $1=file path; prints a content checksum (sha256 preferred, cksum fallback)
  # A same-size rewrite (e.g. gates.py _save_store rewriting evidence.json in
  # place) is invisible to a relpath+size-only manifest — content changes,
  # size doesn't. A checksum column catches it.
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" 2>/dev/null | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" 2>/dev/null | awk '{print $1}'
  else
    cksum "$1" 2>/dev/null | awk '{print $1"-"$2}'
  fi
}

_manifest_of() { # $1=dir root; prints "relpath<TAB>size<TAB>checksum" per regular file, sorted;
  # returns 1 if find's OWN exit status is nonzero. A mode-000 subdirectory
  # silently hides files from find's enumeration; the previous version
  # discarded that exit status (`find ... 2>/dev/null | while ...`), so an
  # enumeration failure on the source AND an identical failure on the
  # destination could hide the exact same files from BOTH manifests
  # symmetrically, leaving the listing/size/checksum comparison blind to the
  # loss. Uses the same tmp-file + RETURN-trap pattern as _scan_source_tree so
  # find's exit status can be checked directly (a `find | while` pipeline
  # loses that check to the `while`/`sort` stages even under pipefail).
  #
  # M2: this function is always invoked as `x="$(_manifest_of ...)"` — the
  # ENTIRE function body, including any variable it assigns, runs inside the
  # command-substitution SUBSHELL. A `MANIFEST_ERR="..."` assigned in here
  # never propagates back to the parent shell (subshells don't leak variable
  # assignments outward), so a caller reading `$MANIFEST_ERR` after the call
  # always sees whatever it reset it to just before calling — dead code that
  # silently always falls back to a generic message. `warn` writes directly
  # to fd 2, which passes straight through `$(...)` (that only captures fd 1)
  # to the real caller/terminal — so failures are reported by calling `warn`
  # HERE, at the point of failure, instead of via a variable that can't cross
  # the subshell boundary.
  local root="$1" f rel size sum tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/ffs-manifest.XXXXXX" 2>/dev/null)" || {
    warn "manifest build: mktemp failed for '$root'"; return 1; }
  trap 'rm -f "$tmp"; trap - RETURN' RETURN
  # HIGH-2(c): `-type f` alone silently EXCLUDES symlinks from the manifest
  # entirely (a symlink's own type is "l", never "f", whether or not it's
  # followed) — a symlink present in one tree but missing (or pointing
  # somewhere different) in the other would never even show up as a
  # manifest difference, since neither side lists it. `_scan_source_tree`
  # is the authoritative guard that refuses on ANY symlink in either tree
  # (HIGH-2a/b); this is deliberate defense-in-depth so the comparison
  # itself doesn't independently paper over the exact same class of gap if
  # one of those guards is ever bypassed or mutated away.
  if ! find "$root" \( -type f -o -type l \) -print0 > "$tmp" 2>/dev/null; then
    warn "manifest build: enumeration failed under '$root' (permission error?)"
    return 1
  fi
  while IFS= read -r -d '' f; do
    rel="${f#"$root"/}"
    if [ -L "$f" ]; then
      size="SYMLINK"
      sum="$(readlink "$f" 2>/dev/null || printf 'unreadable-symlink-target')"
    else
      size="$(wc -c < "$f" 2>/dev/null | tr -d '[:space:]')"
      sum="$(_checksum_of "$f")"
    fi
    printf '%s\t%s\t%s\n' "$rel" "$size" "$sum"
  done < "$tmp" | sort
  return 0
}

_build_expected_manifest() { # $1=have_ffs $2=ffs_src $3=have_extra $4=extra_src $5=extra_rel
  # Returns 1 if the ffs_src manifest build fails — propagated explicitly
  # (not swallowed by a brace-group `&&`/`if` sequence) so archive_run_ledger
  # can refuse the archive instead of comparing against a silently-incomplete
  # "expected" listing. _manifest_of already `warn`s the specific reason
  # (M2) before returning, so no separate error channel is needed here.
  local ffs_part="" extra_part="" sz sum
  if [ "$1" -eq 1 ]; then
    ffs_part="$(_manifest_of "$2")" || return 1
  fi
  if [ "$3" -eq 1 ]; then
    sz="$(wc -c < "$4" 2>/dev/null | tr -d '[:space:]')"
    sum="$(_checksum_of "$4")"
    extra_part="$(printf '%s\t%s\t%s' "$5" "$sz" "$sum")"
  fi
  printf '%s\n%s\n' "$ffs_part" "$extra_part" | sed '/^$/d' | sort
  return 0
}

_mkdir_contained() { # $1=base (must exist, real dir, not symlink) $2=relpath to create under it
  # Creates $2 under $1 one path segment at a time, refusing the instant any
  # existing segment turns out to be a symlink — never traverses (or writes)
  # past a symlinked intermediate segment, closing the F3 write-outside-root
  # seam that a single `mkdir -p` on the full path would silently follow.
  local base="$1" relpath="$2" cur seg
  local -a segs
  [ -d "$base" ] && [ ! -L "$base" ] || {
    warn "archive base '$base' is missing or is a symlink"; return 1; }
  IFS='/' read -r -a segs <<< "$relpath"
  cur="$base"
  # NEW-4 (cosmetic): avoid a "//seg" double-slash in messages when base is
  # exactly "/" (e.g. an FFS_LEDGER_ARCHIVE_DIR override anchor before the
  # override's own existing-ancestor is found) — "$cur/$seg" concatenates a
  # literal "/" onto a base that's ALREADY just "/", doubling it.
  [ "$cur" = "/" ] && cur=""
  # NEW-3: iterate defensively even if $relpath produced zero segments (an
  # empty relpath, or one that's entirely "/"). Bash 3.2's `set -u` treats a
  # zero-element array expansion as unbound, aborting the WHOLE script (this
  # lever's contract is ALWAYS exit 0) — the same idiom already used for
  # REPO_ARGS at the top of this file.
  for seg in ${segs[@]+"${segs[@]}"}; do
    [ -n "$seg" ] || continue
    cur="$cur/$seg"
    if [ -e "$cur" ]; then
      if [ -L "$cur" ]; then
        warn "archive path segment '$cur' is a symlink — refusing to archive"
        return 1
      fi
      [ -d "$cur" ] || {
        warn "archive path segment '$cur' exists and is not a directory"; return 1; }
    else
      mkdir "$cur" 2>/dev/null || {
        # MEDIUM-7: shared-ancestor race. Segments above the final leaf
        # (dest_root itself, and the per-run container under it) are
        # intentionally NOT claimed exclusively — many branches/phases can
        # legitimately create the SAME shared ancestor concurrently. If our
        # mkdir lost that race, re-check: a real, non-symlink directory now
        # sitting there means a concurrent creator won it, which is success
        # for our purposes too — only refuse if it's genuinely still
        # missing or turned out to be something unsafe.
        if [ -d "$cur" ] && [ ! -L "$cur" ]; then
          :
        else
          warn "could not create archive dir '$cur'"; return 1
        fi
      }
    fi
  done
  return 0
}

_dir_nonempty() { # $1=dir; true iff it exists and (conservatively) may contain content.
  # DRY-run PREVIEW ONLY — the real (non-dry) path claims the destination
  # leaf atomically via `mkdir` instead (see M3 in archive_run_ledger). A
  # find failure (e.g. permission denied) is treated as "cannot confirm
  # empty" and conservatively read as NONEMPTY, never silently as empty
  # (L4: the previous version's `2>/dev/null` discarded find's exit status,
  # so an unreadable dir read as empty — same bug class as MUT-1).
  local d="$1" hit rc
  [ -d "$d" ] || return 1
  hit="$(find "$d" -mindepth 1 -print -quit 2>/dev/null)"
  rc=$?
  [ "$rc" -eq 0 ] || return 0
  [ -n "$hit" ]
}

_try_claim_leaf() { # $1=candidate dir path; attempts an exclusive `mkdir` claim
  # NEW-4: a failed `mkdir` can mean two very different things, and the
  # previous version treated them identically — burning through all 22
  # disambiguation attempts (all doomed to fail the same way) and reporting
  # a misleading "collision" when the REAL cause was e.g. a read-only
  # run_dir. Returns: 0=claimed; 1=genuine collision (something is already
  # there — try the next candidate); 2=some OTHER mkdir failure (permission
  # denied, disk full, invalid name, ...) — not a collision, abort
  # immediately with an accurate message instead of exhausting the chain.
  if mkdir "$1" 2>/dev/null; then
    return 0
  fi
  [ -e "$1" ] && return 1
  return 2
}

_hash8() { # $1=raw string; prints first 8 hex chars of a content hash (sha256/cksum fallback)
  if command -v shasum >/dev/null 2>&1; then
    printf '%s' "$1" | shasum -a 256 | awk '{print substr($1,1,8)}'
  elif command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "$1" | sha256sum | awk '{print substr($1,1,8)}'
  else
    printf '%s' "$1" | cksum | awk '{printf "%08x", $1}'
  fi
}

_cleanup_partial_dest() { # $1=dest_dir THIS run's atomic mkdir created; best-effort remove on later failure
  # M4: without this, a refusal after the leaf was already claimed leaves
  # partial debris behind; the NEXT run sees that leaf as occupied and
  # ratchets to -hash8, then -2..-20, permanently, one step further per
  # failed run, until it exhausts the disambiguation budget entirely. Only
  # ever called on a directory THIS invocation's `mkdir` just created —
  # never a pre-existing one (see the atomic-claim block below).
  #
  # HIGH-3: a bare `rm -rf "$d"` trusts its caller completely — re-assert
  # containment immediately before deleting, independent of whatever the
  # caller already believes about $d: absolute, a real (non-symlink)
  # directory, and an EXACT match for the leaf THIS run's own claim
  # produced ($run_dir/$branch_slug_final — visible here via bash's
  # dynamic scoping, since this is only ever called from within
  # archive_run_ledger's own call stack). Refuses to delete anything else,
  # regardless of what $1 happens to be. This is a narrower, purpose-built
  # contract for the ONE directory this function may ever remove — it does
  # NOT route through deny() (that denylist guards the SOURCE ledger from
  # deletion; this is the opposite direction, a destination leaf this
  # lever itself created moments earlier).
  local d="$1"
  [ -n "$d" ] || return 0
  case "$d" in
    /*) : ;;
    *) warn "internal: refusing to clean up non-absolute path '$d'"; return 1 ;;
  esac
  [ -d "$d" ] && [ ! -L "$d" ] || {
    warn "internal: refusing to clean up '$d' — missing or a symlink"; return 1
  }
  if [ -z "${run_dir:-}" ] || [ -z "${branch_slug_final:-}" ]; then
    warn "internal: refusing to clean up '$d' — no run_dir/branch_slug_final in scope for containment check"
    return 1
  fi
  if [ "$d" != "$run_dir/$branch_slug_final" ]; then
    warn "internal: refusing to clean up '$d' — does not match this run's claimed leaf ('$run_dir/$branch_slug_final')"
    return 1
  fi
  rm -rf "$d" 2>/dev/null || warn "could not clean up partial archive dir '$d' after failure — manual cleanup may be needed"
}

archive_run_ledger() { # $1=worktree path, $2=branch name being removed
  # Copies <worktree>/.feature-fix-swarm/ (the whole tree, faithfully) PLUS
  # an in-worktree $GATES_STORE file living outside it, to a durable location
  # outside the worktree BEFORE it is removed. This ONLY reads+copies — it
  # must never route through deny()/`run rm`; deny() guards DELETION of the
  # gates ledger, which this step is the opposite of. Complete-or-refuse:
  # any doubt about fidelity WARNs and returns 1 rather than risk a partial
  # copy followed by worktree removal.
  local wt="$1" br="$2"
  # LOW: cheap sanity guard — a caller passing an empty or nonexistent
  # worktree path is a no-op, not an archive attempt (every path check
  # below assumes $wt is a real, existing directory).
  [ -n "$wt" ] && [ -d "$wt" ] || return 0
  local ffs_src="$wt/.feature-fix-swarm"
  local have_ffs=0 have_extra=0 extra_src="" extra_rel=""
  local dest_root dest_root_anchor dest_root_rel dest_dir gcd repo_root run_key branch_slug

  # --- cheap top-level checks (read-only; run even under --dry-run) ---
  if [ -e "$ffs_src" ]; then
    if [ -L "$ffs_src" ]; then
      warn "ledger source '$ffs_src' is a symlink — refusing to archive"
      return 1
    fi
    [ -d "$ffs_src" ] && have_ffs=1
  fi

  if [ -n "${GATES_STORE:-}" ]; then
    local gs="$GATES_STORE" gs_abs="" wt_abs="" ffs_abs=""
    case "$gs" in
      /*) : ;;
      *) gs="$wt/$gs" ;;
    esac
    if [ -L "$gs" ]; then
      warn "GATES_STORE path '$gs' is a symlink — refusing to archive"
      return 1
    fi
    if [ -f "$gs" ]; then
      # `basename` always exits 0 (even for a nonexistent path), so it is the
      # last command substitution in the concatenated expression and its
      # always-0 status previously masked a `cd`/`pwd -P` failure — the old
      # `|| gs_abs=""` fallback could never actually fire. Check the cd/pwd
      # outcome directly instead of relying on that dead fallback.
      local gs_dir=""
      gs_dir="$(cd "$(dirname "$gs")" 2>/dev/null && pwd -P)"
      if [ -n "$gs_dir" ]; then
        gs_abs="$gs_dir/$(basename "$gs")"
      else
        gs_abs=""
      fi
      wt_abs="$(cd "$wt" 2>/dev/null && pwd -P)" || wt_abs=""
      if [ -n "$gs_abs" ] && [ -n "$wt_abs" ]; then
        ffs_abs="$wt_abs/.feature-fix-swarm"
        case "$gs_abs" in
          "$ffs_abs"/*) : ;;   # already inside .feature-fix-swarm — no double-archive
          "$wt_abs"/*)
            extra_rel="${gs_abs#"$wt_abs"/}"
            # MEDIUM-9: _scan_source_tree screens ffs_src for a literal
            # newline in any path (it would corrupt the sorted-listing
            # comparison in _manifest_of) — extra_rel never got the same
            # screen, even though it flows into that exact same manifest
            # via _build_expected_manifest's extra-file entry.
            case "$extra_rel" in
              *$'\n'*)
                warn "GATES_STORE relative path '$extra_rel' contains a newline character — refusing to archive"
                return 1
                ;;
            esac
            have_extra=1
            extra_src="$gs_abs"
            ;;
          *) : ;;              # outside the worktree — not this lever's concern
        esac
      fi
    fi
    # GATES_STORE resolving to a directory/device/nonexistent path is not a
    # "regular file" per the archive contract — silently not included.
  fi

  if [ "$have_ffs" -eq 0 ] && [ "$have_extra" -eq 0 ]; then
    return 0   # genuine no-op: nothing to archive
  fi

  # dest_root_anchor/dest_root_rel split the root into "an already-trusted,
  # existing, real (non-symlink) starting point" + "the relative path we're
  # responsible for creating/validating one segment at a time" (H1). For the
  # default (non-override) case the anchor is repo_root — independently
  # derived from `git rev-parse`, not re-validated against ITS OWN ancestors.
  #
  # NEW-1: an explicit FFS_LEDGER_ARCHIVE_DIR override has no such
  # pre-trusted anchor of its own — the round-2 fix lstat-walked the ENTIRE
  # LOGICAL value from filesystem root unconditionally, which refuses
  # FOREVER on any path reached through a legitimate OS-level mount alias
  # (macOS's /tmp -> /private/tmp, /var -> /private/var, and therefore
  # $TMPDIR too, e.g. /var/folders/...) — the worktree becomes permanently
  # unremovable for something that isn't an attack. Fix: physically resolve
  # FIRST.
  #   - If the override value ALREADY exists as a directory (however it's
  #     reached — an OS alias or otherwise), trust it: `cd`+`pwd -P` gives
  #     its real physical location, used as-is with nothing further to
  #     create. This is the same relaxation the round-2 fix over-tightened;
  #     re-litigating an operator-supplied, ALREADY-EXISTING location isn't
  #     this lever's job.
  #   - Otherwise, walk UP (following symlinks — an OS alias is a real,
  #     existing directory, so this cannot stop ON one) to the deepest
  #     EXISTING ancestor, physically resolve THAT, and lstat-walk only the
  #     remaining NOT-yet-existing segments via _mkdir_contained. Those
  #     segments cannot be OS aliases (an alias is by definition an
  #     existing mount point) — a symlink among them can only be something
  #     an attacker planted (or a dangling leftover), and _mkdir_contained's
  #     per-segment `-L` check still refuses it exactly as before.
  if [ -n "${FFS_LEDGER_ARCHIVE_DIR:-}" ]; then
    case "$FFS_LEDGER_ARCHIVE_DIR" in
      /*) : ;;
      *) warn "FFS_LEDGER_ARCHIVE_DIR must be an absolute path ('$FFS_LEDGER_ARCHIVE_DIR') — refusing to archive"; return 1 ;;
    esac
    if [ -d "$FFS_LEDGER_ARCHIVE_DIR" ]; then
      dest_root="$(cd "$FFS_LEDGER_ARCHIVE_DIR" 2>/dev/null && pwd -P)"
      if [ -z "$dest_root" ]; then
        warn "could not physically resolve FFS_LEDGER_ARCHIVE_DIR '$FFS_LEDGER_ARCHIVE_DIR' — refusing to archive"
        return 1
      fi
      # NEW-3: never accept the filesystem root itself as an archive root —
      # this is the concrete `FFS_LEDGER_ARCHIVE_DIR=/` repro. Beyond being
      # nonsensical, it would leave dest_root_rel empty when everything
      # under it is later created relative to dest_root, and an empty
      # relpath is exactly the zero-segments case bash 3.2's `set -u`
      # aborts on without the `${segs[@]+...}` idiom (also fixed in
      # _mkdir_contained directly, belt-and-suspenders).
      if [ "$dest_root" = "/" ]; then
        warn "FFS_LEDGER_ARCHIVE_DIR resolves to the filesystem root — refusing to use it as an archive root"
        return 1
      fi
      dest_root_anchor="$dest_root"
      dest_root_rel=""
    else
      local override_parent="" existing_anc="" anc_physical="" remainder=""
      override_parent="$(dirname "$FFS_LEDGER_ARCHIVE_DIR")"
      existing_anc="$override_parent"
      while [ -n "$existing_anc" ] && [ ! -d "$existing_anc" ]; do
        existing_anc="$(dirname "$existing_anc")"
      done
      [ -n "$existing_anc" ] || existing_anc="/"
      anc_physical="$(cd "$existing_anc" 2>/dev/null && pwd -P)"
      if [ -z "$anc_physical" ]; then
        warn "could not resolve physical path of existing ancestor '$existing_anc' for FFS_LEDGER_ARCHIVE_DIR — refusing to archive"
        return 1
      fi
      if [ "$existing_anc" = "/" ]; then
        remainder="${FFS_LEDGER_ARCHIVE_DIR#/}"
      else
        remainder="${FFS_LEDGER_ARCHIVE_DIR#"$existing_anc"/}"
      fi
      if [ -z "$remainder" ] || [ "$remainder" = "$FFS_LEDGER_ARCHIVE_DIR" ]; then
        warn "could not compute a safe path remainder for FFS_LEDGER_ARCHIVE_DIR '$FFS_LEDGER_ARCHIVE_DIR' — refusing to archive"
        return 1
      fi
      dest_root_anchor="$anc_physical"
      dest_root_rel="$remainder"
      dest_root="$anc_physical/$remainder"
    fi
  else
    gcd="$(git rev-parse --git-common-dir 2>/dev/null)" || {
      warn "could not resolve git-common-dir for ledger archive"; return 1; }
    case "$gcd" in
      /*) : ;;
      *)
        # NEW-4: use `pwd -P` (not plain `pwd`) and guard the `cd` failing —
        # the previous version's `$(cd ... && pwd)/$(basename "$gcd")`
        # silently produced just "/$(basename)" if `cd` failed (empty
        # output from the failed substitution, not an error that halts
        # anything under `set -uo pipefail` alone), so `repo_root=
        # $(dirname "$gcd")` could silently become "/" with no warning.
        local gcd_dir=""
        gcd_dir="$(cd "$(dirname "$gcd")" 2>/dev/null && pwd -P)"
        if [ -z "$gcd_dir" ]; then
          warn "could not resolve absolute path for git-common-dir '$gcd' — refusing to archive"
          return 1
        fi
        gcd="$gcd_dir/$(basename "$gcd")"
        ;;
    esac
    repo_root="$(dirname "$gcd")"
    if [ -z "$repo_root" ] || [ "$repo_root" = "/" ]; then
      warn "resolved an unsafe repo_root ('$repo_root') for ledger archive — refusing to archive"
      return 1
    fi
    dest_root="$repo_root/.feature-fix-swarm/archive"
    dest_root_anchor="$repo_root"
    dest_root_rel=".feature-fix-swarm/archive"
  fi

  # review-gate HIGH-1: dest_root must never be the worktree being removed,
  # or nested under it — an operator (or provisioning script) pointing
  # FFS_LEDGER_ARCHIVE_DIR at e.g. "$wt/nested-archive" would archive INTO
  # the worktree, and the very next step (`git worktree remove`) deletes
  # the worktree — including the "archive" that was supposed to survive it.
  # Compare PHYSICALLY resolved paths (both dest_root and $wt may be
  # reached via symlinks/aliases; a raw string prefix check on the logical
  # values could miss an alias-based match in either direction).
  local wt_phys=""
  wt_phys="$(cd "$wt" 2>/dev/null && pwd -P)"
  if [ -n "$wt_phys" ]; then
    case "$dest_root" in
      "$wt_phys"|"$wt_phys"/*)
        warn "ledger archive root '$dest_root' is the worktree being removed, or nested inside it ('$wt_phys') — refusing to archive (git worktree remove would delete the archive itself)"
        return 1
        ;;
    esac
  fi

  run_key="$(_sanitize_slug "${GSD_RUN_ID:-pr${PR}}")"
  branch_slug="$(_sanitize_slug "${br//\//-}")"
  [ -n "$run_key" ] || run_key="run"
  [ -n "$branch_slug" ] || branch_slug="branch"
  # MEDIUM-8: _sanitize_slug's own dot-collapsing only fires on RUNS of 2+
  # dots (`s/\.{2,}/-/g`), so a single-character input of exactly "." (or
  # an unsanitized ".." that somehow reaches here) survives unchanged. A
  # run_key of "." collapses "$dest_root/$run_key" back down to
  # "$dest_root" itself, defeating the per-run container's own isolation.
  case "$run_key" in .|..) run_key="run" ;; esac
  case "$branch_slug" in .|..) branch_slug="branch" ;; esac

  if [ "$DRY" -eq 1 ]; then
    # DRY preview only — NEVER mutates. The real path below claims the leaf
    # atomically via `mkdir`; a preview has no such requirement (nothing is
    # written), so a cheap non-atomic probe is fine here even though a real
    # run moments later could in principle choose a different candidate
    # under concurrent load. Root/run-key containers are not created either.
    local preview_rel="$branch_slug" preview_dir="$dest_root/$run_key/$branch_slug"
    if _dir_nonempty "$preview_dir"; then
      preview_rel="${branch_slug}-$(_hash8 "$br")"
    fi
    note "DRY: archive ledger $ffs_src -> $dest_root/$run_key/$preview_rel"
    return 0
  fi

  # --- deep guards: only for the real (non-dry) archive path ---
  if [ "$have_ffs" -eq 1 ]; then
    if [ ! -r "$ffs_src" ] || [ ! -x "$ffs_src" ]; then
      warn "ledger source '$ffs_src' is not readable/traversable — refusing to archive"
      return 1
    fi
    if ! _scan_source_tree "$ffs_src"; then
      warn "ledger source '$ffs_src' failed safety scan (${SCAN_ERR:-unknown}) — refusing to archive"
      return 1
    fi
  fi
  if [ "$have_extra" -eq 1 ] && [ ! -r "$extra_src" ]; then
    warn "GATES_STORE file '$extra_src' is not readable — refusing to archive"
    return 1
  fi

  # H1: symlink-safe root creation for whatever part of dest_root doesn't
  # already exist (dest_root_rel is empty when the whole thing was already
  # physically resolved above — nothing left to create there). Everything
  # dest_root_rel DOES cover is lstat-walked one segment at a time via
  # _mkdir_contained — no `-d`-based shortcut ever skips ahead past an
  # intermediate symlink the way the round-2 "deepest existing ancestor"
  # search could.
  if [ -n "$dest_root_rel" ]; then
    _mkdir_contained "$dest_root_anchor" "$dest_root_rel" || return 1
  else
    [ -d "$dest_root_anchor" ] && [ ! -L "$dest_root_anchor" ] || {
      warn "ledger archive root '$dest_root_anchor' is missing or a symlink — refusing to archive"
      return 1
    }
  fi

  # Shared per-run container: many branches/phases can share the same
  # run_key, so this is intentionally NOT claimed exclusively — only the
  # branch leaf below is.
  _mkdir_contained "$dest_root" "$run_key" || return 1
  local run_dir="$dest_root/$run_key"

  # M3: claim the destination leaf ATOMICALLY via `mkdir` (which fails if
  # the name already exists) instead of test-then-use (_dir_nonempty, then
  # write). A test-then-use window lets two concurrent finalizers sharing
  # the same run_key+branch_slug interleave: both observe "empty", both
  # proceed, and the loser's cp -R overwrites an archive the winner already
  # verified. `mkdir` succeeding is a kernel-atomic, exclusive claim on that
  # directory name — nothing else can hold it at that instant. Falls
  # through the same disambiguation chain as before (hash suffix, then
  # numeric) on each failed claim; run_key is constant per PR/run and
  # _sanitize_slug maps distinct raw branch names onto the same slug (e.g.
  # 'gsd/phase-1+a' and 'gsd/phase-1-a' both collapse to 'gsd-phase-1-a'),
  # so collisions are a real, expected occurrence, not just a race.
  local branch_slug_final="" claimed=0 attempts=0 hash_slug candidate i claim_rc
  attempts=$((attempts + 1))
  _try_claim_leaf "$run_dir/$branch_slug"; claim_rc=$?
  if [ "$claim_rc" -eq 0 ]; then
    branch_slug_final="$branch_slug"; claimed=1
  elif [ "$claim_rc" -eq 2 ]; then
    warn "could not create ledger archive destination '$run_dir/$branch_slug' (not a collision — check permissions/disk space) — refusing to archive"
    return 1
  fi
  if [ "$claimed" -ne 1 ]; then
    hash_slug="${branch_slug}-$(_hash8 "$br")"
    attempts=$((attempts + 1))
    _try_claim_leaf "$run_dir/$hash_slug"; claim_rc=$?
    if [ "$claim_rc" -eq 0 ]; then
      branch_slug_final="$hash_slug"; claimed=1
    elif [ "$claim_rc" -eq 2 ]; then
      warn "could not create ledger archive destination '$run_dir/$hash_slug' (not a collision — check permissions/disk space) — refusing to archive"
      return 1
    else
      i=2
      while [ "$i" -le 20 ]; do
        candidate="${hash_slug}-$i"
        attempts=$((attempts + 1))
        _try_claim_leaf "$run_dir/$candidate"; claim_rc=$?
        if [ "$claim_rc" -eq 0 ]; then
          branch_slug_final="$candidate"; claimed=1
          break
        elif [ "$claim_rc" -eq 2 ]; then
          warn "could not create ledger archive destination '$run_dir/$candidate' (not a collision — check permissions/disk space) — refusing to archive"
          return 1
        fi
        i=$((i + 1))
      done
    fi
    if [ "$claimed" -eq 1 ]; then
      note "ledger archive destination collision at '$run_key/$branch_slug' for run '$run_key' — claimed disambiguated path '$run_key/$branch_slug_final'"
    fi
  fi
  if [ "$claimed" -ne 1 ]; then
    warn "ledger archive destination collision for branch '$br' (run '$run_key') could not be resolved after $attempts attempts — refusing to archive"
    return 1
  fi
  dest_dir="$run_dir/$branch_slug_final"

  # HIGH-2(a): re-assert immediately after claiming — the atomic `mkdir`
  # guarantees dest_dir was a fresh, real, non-symlink directory the
  # INSTANT it was created, but a narrow TOCTOU window exists between that
  # instant and here (some other process could in principle swap it out).
  # Cheap, and consistent with every other "just created it, verify before
  # trusting it" check elsewhere in this lever.
  [ -d "$dest_dir" ] && [ ! -L "$dest_dir" ] || {
    warn "ledger archive destination '$dest_dir' is missing or a symlink immediately after claiming it — refusing to archive"
    _cleanup_partial_dest "$dest_dir"; return 1
  }

  if [ "$have_ffs" -eq 1 ]; then
    cp -R "$ffs_src/." "$dest_dir/" 2>/dev/null || {
      warn "failed to copy $ffs_src to ledger archive"
      _cleanup_partial_dest "$dest_dir"; return 1; }
  fi
  # M5: cp -R above flattens .feature-fix-swarm/X to $dest_dir/X. The extra
  # GATES_STORE file's own worktree-relative path (extra_rel) can COLLIDE
  # with that namespace — e.g. GATES_STORE=evidence.json (worktree root)
  # collides with .feature-fix-swarm/evidence.json's own relpath the moment
  # both land at $dest_dir/evidence.json, permanently corrupting/refusing
  # this archive on every future run. Namespace it under its own
  # "_worktree/" prefix so the two source trees can never collide.
  local extra_rel_ns=""
  if [ "$have_extra" -eq 1 ]; then
    extra_rel_ns="_worktree/$extra_rel"
    local extra_dest="$dest_dir/$extra_rel_ns" extra_dest_reldir
    extra_dest_reldir="$(dirname "$extra_rel_ns")"
    # L1: route through the same symlink-safe walk as everything else,
    # instead of a bare `mkdir -p` that bypasses it.
    _mkdir_contained "$dest_dir" "$extra_dest_reldir" || {
      _cleanup_partial_dest "$dest_dir"; return 1; }
    cp "$extra_src" "$extra_dest" 2>/dev/null || {
      warn "failed to copy GATES_STORE file $extra_src to ledger archive"
      _cleanup_partial_dest "$dest_dir"; return 1; }
  fi

  # HIGH-2(b): the header/CHANGELOG claim "a symlink anywhere in the source
  # tree OR THE DESTINATION" refuses — but nothing actually scanned the
  # destination after the copy landed. _scan_source_tree is generic (its
  # name notwithstanding — it takes any dir root) and gives dest_dir the
  # identical anomaly scan ffs_src already got: any symlink, non-regular
  # entry, or newline-containing path anywhere under dest_dir refuses and
  # cleans up rather than silently passing verification.
  if ! _scan_source_tree "$dest_dir"; then
    warn "ledger archive destination '$dest_dir' failed post-copy safety scan (${SCAN_ERR:-unknown}) — refusing to archive"
    _cleanup_partial_dest "$dest_dir"; return 1
  fi

  local expected actual
  # M2: _manifest_of already `warn`s the specific failure reason directly
  # (fd 2 passes through command substitution untouched) — no MANIFEST_ERR
  # variable needed (one never actually worked: it was assigned inside the
  # command-substitution subshell and could never propagate back here).
  expected="$(_build_expected_manifest "$have_ffs" "$ffs_src" "$have_extra" "$extra_src" "$extra_rel_ns")" || {
    warn "could not build expected manifest for '$ffs_src' — refusing to archive"
    _cleanup_partial_dest "$dest_dir"; return 1
  }
  actual="$(_manifest_of "$dest_dir")" || {
    warn "could not build actual manifest for '$dest_dir' — refusing to archive"
    _cleanup_partial_dest "$dest_dir"; return 1
  }
  if [ "$expected" != "$actual" ]; then
    warn "ledger archive verification failed — destination listing/sizes/checksums do not match source"
    _cleanup_partial_dest "$dest_dir"; return 1
  fi

  note "archived run ledger: $ffs_src -> $dest_dir"
  return 0
}

remove_worktree_for_branch() { # remove the worktree checked out on $1 iff clean
  local br="$1" wt
  wt="$(git worktree list --porcelain \
        | awk -v b="refs/heads/$br" '$1=="worktree"{w=$2} $1=="branch"&&$2==b{print w}')"
  [ -n "$wt" ] || return 0
  if [ -n "$(git -C "$wt" status --porcelain 2>/dev/null)" ]; then
    warn "worktree $wt on '$br' is DIRTY — keeping it; route to /adopt-wip"
    return 1
  fi
  if ! archive_run_ledger "$wt" "$br"; then
    warn "ledger archive failed for $wt — keeping worktree (preserving unarchivable data)"
    return 1
  fi
  run git worktree remove "$wt"
}

delete_landed_branch() { # delete $1 only under landed-tip proof
  local br="$1" tip
  git show-ref --verify -q "refs/heads/$br" || return 0
  if [ "$DRY" -eq 1 ]; then note "DRY: delete local branch $br"; return 0; fi
  if git branch -d "$br" >/dev/null 2>&1; then
    note "deleted local branch '$br' (-d: merged into HEAD)"
    return 0
  fi
  tip="$(git rev-parse "refs/heads/$br" 2>/dev/null)" || return 1
  if [ "$tip" = "$HEAD_OID" ] \
     || git merge-base --is-ancestor "$tip" "$HEAD_OID" 2>/dev/null; then
    if git branch -D "$br" >/dev/null 2>&1; then
      note "deleted local branch '$br' (tip $tip landed as merged PR head — squash-safe proof)"
    else
      warn "could not delete '$br' (checked out somewhere?)"
    fi
  else
    warn "branch '$br' tip $tip is not the merged PR head ($HEAD_OID) — NOT deleting (unmerged work?)"
  fi
}

# 1. worktree first (git refuses to delete a branch checked out in any worktree)
remove_worktree_for_branch "$BRANCH" || true
# 2. local + remote feature branch
delete_landed_branch "$BRANCH"
# `gh pr merge --delete-branch` usually got here first — only push a delete if
# the remote ref still exists, else every finish tail ends on a bogus WARN.
if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  run git push origin --delete "$BRANCH"
else
  note "remote branch '$BRANCH' already gone — nothing to delete"
fi
# 3. gsd/phase-* intermediates: prune only ancestors of the merged head
while IFS= read -r pb; do
  [ -n "$pb" ] || continue
  if git merge-base --is-ancestor "$pb" "$HEAD_OID" 2>/dev/null; then
    remove_worktree_for_branch "$pb" || continue
    delete_landed_branch "$pb"
  else
    note "keeping gsd branch '$pb' — not an ancestor of the merged head (open work?)"
  fi
done < <(git for-each-ref --format='%(refname:short)' 'refs/heads/gsd/phase-*')
# 4. run-state: three fixed files, denylist-guarded
for f in .planning/run-state/gsd-run.heartbeat \
         .planning/run-state/gsd-run.status \
         .planning/run-state/gsd-run.pid; do
  deny "$f" || continue
  [ -e "$f" ] || continue
  run rm -f "$f"
done
# 4b. loop-round counters (plan-wall cap, gates.py `_loops` namespace): a
# spec's run_id is stable across runs, so a LANDED run must drop its
# counters or the next run of the same spec starts pre-capped. Fail-soft
# like everything here — a missing gates.py never blocks finalization.
_gates_py=""
for _c in "$(pwd)/packages/feature-fix-swarm/lib/gates.py" \
          "$HOME/.claude/lib/feature-fix-swarm/gates.py" \
          "$(pwd)/lib/gates.py"; do
  [ -f "$_c" ] && _gates_py="$_c" && break
done
if [ -n "$_gates_py" ]; then
  run python3 "$_gates_py" loop-round "${GSD_RUN_ID:-pr${PR}}" --reset-all \
    || note "loop-round reset failed (non-fatal)"
fi
# 5. stale worktree metadata
run git worktree prune

note "finalize complete (fail-soft: any WARN above needs manual attention)"
exit 0
