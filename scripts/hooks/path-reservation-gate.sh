#!/usr/bin/env bash
# path-reservation-gate.sh — PreToolUse (Edit|Write|MultiEdit) BLOCK (exit 2)
#
# WHY: Phase 2 (scripts/coord/coord.py) made "who owns this path" atomic —
# lease-acquire/renew/release are race-free and fencing-safe. Nothing before
# this hook READS that answer at the moment an edit is about to land. This
# guard is that check-at-use: it blocks a Claude Code Edit/Write/MultiEdit
# whose target path falls inside another session's live EXCLUSIVE path
# lease (`path:<glob-or-exact>` in scripts/coord/coord.py's registry).
#
# SCOPE, STATED HONESTLY (REQ-07's disclosure clause):
#   - Covers the Claude Code Edit tool family ONLY: Edit, Write, MultiEdit.
#   - Writes performed through the Bash tool are NOT intercepted at all — a
#     Bash matcher would have to parse arbitrary shell to find write
#     targets, which this hook does not attempt. That is a recorded v1
#     limitation (spec.md:260), not an oversight.
#   - NotebookEdit (its file target is `notebook_path`, not `file_path`) is
#     likewise NOT intercepted — a known remaining gap alongside the Bash
#     tool gap.
#   - Kill switch: FFS_COORD_MODE=off short-circuits every entry point in
#     this script, in bash, before any Python is launched.
#
# THE GUARD IS A READER. It never writes registry.json, never mints a
# session, never takes the registry filelock, never prunes a stale holder,
# and never brings a coordination store into existence in a repo that had
# none. The only file it may write is its own guard-cache.json. Cache
# read/write ERRORS never change a verdict (they skip the cache); the cache
# CONTENT is trusted after tag+digest+structural validation, so a same-UID
# writer who forges a correctly-digested cache can flip a verdict — accepted
# residual, that writer can edit registry.json directly (plan P-18).
#
# Fail-closed in enforce mode on malformed hook JSON, an unreadable store,
# or a broken filelock install (REQ-08). Fail-open (warn + exit 0) in audit
# mode for the same triggers. `off` mode never launches Python at all.
set -uo pipefail

[ "${FFS_COORD_MODE:-}" = "off" ] && exit 0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# ── pure-bash upward walk (P-10) — zero subprocesses on the hot path ────────
# Sets globals GIT_STATE ("found" | "notfound" | "unparseable"),
# COMMON_DIR (the git common dir, only when GIT_STATE=found) and
# WORKTREE_ROOT (the directory the walk found .git in, only when found).
_resolve_git_topology() {
  GIT_STATE="notfound"
  COMMON_DIR=""
  WORKTREE_ROOT=""
  local start dir
  start="${CLAUDE_PROJECT_DIR:-$PWD}"
  # -P: physical path. git resolves --path-format=absolute the same way, and
  # a logical (symlink-preserving) pwd would silently diverge from git's
  # answer on any host where an ancestor is a symlink (macOS /var ->
  # /private/var is the common case) -- exactly T-03-11's failure shape.
  dir="$(cd "$start" 2>/dev/null && pwd -P)" || return 0
  while :; do
    if [ -d "$dir/.git" ]; then
      COMMON_DIR="$dir/.git"
      WORKTREE_ROOT="$dir"
      GIT_STATE="found"
      return 0
    elif [ -e "$dir/.git" ]; then
      # A FILE (linked worktree) or something odd at .git — read the
      # pointer WHOLE, never word-split (plan-wall round 3 FINDING 3): a
      # gitdir path containing a space must not be truncated.
      local line=""
      # NOTE: `read` returns non-zero when the file's last line has no
      # trailing newline (a common shape for git's own `.git` pointer
      # files) even though it DID populate `line` correctly -- so this
      # must NOT be `|| line=""`, which would silently discard a
      # successful read and mis-route a normal linked worktree to
      # "unparseable" (T-03-11's exact failure shape, one layer up).
      IFS= read -r line <"$dir/.git" 2>/dev/null
      case "$line" in
        "gitdir: "*)
          local gitdir="${line#gitdir: }"
          case "$gitdir" in
            /*) : ;;
            *) gitdir="$dir/$gitdir" ;;
          esac
          COMMON_DIR="${gitdir%/worktrees/*}"
          WORKTREE_ROOT="$dir"
          GIT_STATE="found"
          return 0
          ;;
        *)
          # unparseable — delegate to python's authoritative resolver,
          # never guess (P-10).
          GIT_STATE="unparseable"
          return 0
          ;;
      esac
    fi
    [ "$dir" = "/" ] && return 0
    dir="$(dirname "$dir")"
  done
}

# ── test-only resolver probe (P-10 parity tests) ────────────────────────────
# Prints the resolved store root and worktree root and exits 0 BEFORE
# touching stdin at all.
if [ -n "${PATH_RESERVATION_GATE_RESOLVE_ONLY:-}" ]; then
  _resolve_git_topology
  PROBE_STORE=""
  if [ "$GIT_STATE" = "found" ]; then
    PROBE_REPO_ROOT="$(dirname "$COMMON_DIR")"
    PROBE_STORE="$PROBE_REPO_ROOT/.feature-fix-swarm/coord"
  fi
  printf 'STORE=%s\n' "$PROBE_STORE"
  printf 'WORKTREE=%s\n' "$WORKTREE_ROOT"
  exit 0
fi

INPUT=$(cat 2>/dev/null || true)
[ -z "$INPUT" ] && exit 0

# ── resolve store + worktree root (P-10, P-11) ──────────────────────────────
GIT_STATE="notfound"
COMMON_DIR=""
WORKTREE_ROOT=""
if [ -n "${PATH_RESERVATION_GATE_FORCE_DELEGATE:-}" ]; then
  # test-only: select the same branch an unparseable .git pointer selects —
  # no store/worktree hints, python resolves everything itself.
  GIT_STATE="unparseable"
else
  _resolve_git_topology
fi

# Walk found no .git anywhere -> not a git repo -> nothing to guard (P-11).
[ "$GIT_STATE" = "notfound" ] && exit 0

STORE=""
if [ "$GIT_STATE" = "found" ]; then
  REPO_ROOT="$(dirname "$COMMON_DIR")"
  STORE="$REPO_ROOT/.feature-fix-swarm/coord"

  # Downward component walk (checker round 1 BLOCKER 3): absence may only be
  # concluded under a PARENT PROVEN SEARCHABLE. `[ -d ]`/`[ -r ]` alone
  # cannot distinguish an unsearchable ancestor (EACCES) from a genuinely
  # missing path (ENOENT) — both read as a plain false.
  DELEGATE=0
  walk_dir="$REPO_ROOT"
  for comp in .feature-fix-swarm coord; do
    if [ ! -x "$walk_dir" ]; then
      DELEGATE=1
      break
    fi
    if [ ! -d "$walk_dir/$comp" ]; then
      exit 0 # parent searchable -> truly absent
    fi
    walk_dir="$walk_dir/$comp"
  done
  if [ "$DELEGATE" -eq 0 ]; then
    if [ ! -r "$STORE" ] || [ ! -x "$STORE" ]; then
      DELEGATE=1
    elif [ ! -e "$STORE/registry.json" ]; then
      exit 0 # authoritative: parent proven searchable
    fi
  fi
fi

# ── resolve mode (P-16) ──────────────────────────────────────────────────
MODE="${FFS_COORD_MODE:-}"
if [ -z "$MODE" ] && [ -n "$STORE" ] && [ -r "$STORE/mode" ]; then
  # same non-trailing-newline pitfall as the .git pointer read above --
  # `read`'s exit status is not a proxy for "did MODE get populated".
  IFS= read -r MODE <"$STORE/mode" 2>/dev/null
fi
case "$MODE" in
  off | audit | enforce) : ;;
  *) MODE="enforce" ;;
esac
[ "$MODE" = "off" ] && exit 0

# ── invoke the python core — STDIN, never argv (AC-008) ─────────────────────
# The core MUST read the envelope from fd 0, so it cannot use the
# `python3 - ... <<'PY'` heredoc form (checker round 1 BLOCKER 1): a heredoc
# redirects fd 0 onto the PROGRAM TEXT, so python would read the heredoc as
# its own source and then decode EOF from `json.load(sys.stdin)` as
# malformed JSON on every single Edit. Hoisting the program into a variable
# keeps this a single file while leaving fd 0 free for the envelope pipe.
CORE=$(cat <<'PY'
import hashlib
import json
import os
import sys
import time
import unicodedata

_env_mode = os.environ.get("FFS_COORD_MODE")
if _env_mode not in ("off", "audit", "enforce"):
    _env_mode = None
_argv_mode = sys.argv[3] if len(sys.argv) > 3 else ""
if _argv_mode not in ("off", "audit", "enforce"):
    _argv_mode = None
MODE = _env_mode or _argv_mode or "enforce"


def _pinned_read(store):
    """One O_NOFOLLOW file description -> (tag, tag2, digest) (P-13, P-18
    plan-wall round 2 FINDING C). tag/digest/text are all properties of the
    SAME fd so they can never describe different objects."""
    fd = os.open("registry.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=store.coord_fd)
    try:
        st1 = os.fstat(fd)
        with os.fdopen(os.dup(fd), "r") as f:
            text = f.read()
        st2 = os.fstat(fd)
    finally:
        os.close(fd)
    tag = [st1.st_mtime, st1.st_size]
    tag2 = [st2.st_mtime, st2.st_size]
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return tag, tag2, digest


def _cached_exclusive_leases(coord, store, tag, digest):
    """A cache entry is trusted only when bound to registry CONTENT, never
    on its (mtime,size) tag alone (P-18 plan-wall round 1 FINDING 2)."""
    try:
        cache_text = coord._read_text_fd(store.coord_fd, "guard-cache.json")
        cache = json.loads(cache_text)
    except (OSError, ValueError):
        return None
    if not isinstance(cache, dict):
        return None
    if cache.get("tag") != tag:
        return None
    if cache.get("registry_sha256") != digest:
        return None
    index = cache.get("exclusive_index")
    if not isinstance(index, list):
        return None
    result = {}
    try:
        for item in index:
            if not isinstance(item, dict):
                return None
            key = item.get("key")
            holders = item.get("holders")
            coord._validate_lease_key(key)
            if not isinstance(holders, dict):
                return None
            for h in holders.values():
                coord._validate_lease_holder(key, h)
            result[key] = {"mode": "exclusive", "holders": holders}
    except coord.CoordExit:
        return None
    return result


def _write_cache(coord, store, tag, digest, exclusive_leases):
    """Cache write is verdict-invisible: the union (OSError, CoordExit) is
    caught (P-18 plan-wall round 1 FINDING 3 — _refuse_if_symlink raises
    CoordExit, not OSError) and any failure here silently skips caching."""
    try:
        coord._refuse_if_symlink(store.coord_fd, "guard-cache.json")
        payload = {
            "tag": tag,
            "registry_sha256": digest,
            "exclusive_index": [
                {"key": k, "holders": v["holders"]} for k, v in exclusive_leases.items()
            ],
        }
        tmp = f".guard-cache.json.{os.getpid()}.tmp"
        fd = os.open(
            tmp,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
            dir_fd=store.coord_fd,
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
        except Exception:
            try:
                os.unlink(tmp, dir_fd=store.coord_fd)
            except OSError:
                pass
            raise
        os.replace(tmp, "guard-cache.json", src_dir_fd=store.coord_fd, dst_dir_fd=store.coord_fd)
    except (OSError, coord.CoordExit):
        pass


def _exclusive_leases(coord, store):
    """Registry read, lock-free (P-13), with the mtime+size + content-bound
    cache (P-18). Returns {key: {"mode": "exclusive", "holders": {...}}}."""
    try:
        tag, tag2, digest = _pinned_read(store)
    except (OSError, UnicodeDecodeError):
        # Any failure to read the text -- including ELOOP on a symlinked
        # registry.json -- skips the cache entirely and falls through to
        # coord._load_registry, which raises/self-handles per its own
        # contract (plan-wall round 2 FINDING C).
        tag = tag2 = digest = None

    if tag is not None:
        cached = _cached_exclusive_leases(coord, store, tag, digest)
        if cached is not None:
            return cached

    registry = coord._load_registry(store)
    all_leases = coord._lease_entries(registry)
    exclusive = {k: v for k, v in all_leases.items() if v["mode"] == "exclusive"}

    if tag is not None:
        # Tag re-pin: only write the cache if registry.json's stat still
        # matches what we pinned. ponytail: single-shot re-check (the plan
        # sketches a one-time retry-on-mismatch); a mismatch here just
        # skips today's cache write -- correctness is unaffected because
        # the NEXT read re-binds on content (registry_sha256), never on
        # the tag alone.
        try:
            st_now = os.stat("registry.json", dir_fd=store.coord_fd)
            cur_tag = [st_now.st_mtime, st_now.st_size]
        except OSError:
            cur_tag = None
        if cur_tag == tag2:
            _write_cache(coord, store, cur_tag, digest, exclusive)

    return exclusive


try:
    # ── step 3(a): envelope taxonomy (P-21, the one normative table) ───────
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed hook envelope: unparseable JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"malformed hook envelope: top-level JSON is {type(data).__name__}, not object"
        )
    tool_name = data.get("tool_name")
    if tool_name not in ("Edit", "Write", "MultiEdit"):
        sys.exit(0)  # row 4: benign non-event (includes {} and non-str tool_name)
    ti = data.get("tool_input")
    if not isinstance(ti, dict):
        raise ValueError(
            f"malformed hook envelope: tool_input is {type(ti).__name__}, not object"
        )
    if "file_path" not in ti:
        sys.exit(0)  # row 6: benign non-event
    fp = ti["file_path"]
    if not isinstance(fp, str) or fp == "":
        raise ValueError(f"malformed hook envelope: file_path is {fp!r}")
    # row 8: fp is THE guarded event

    # ── step 3(b): import coord from the hook's own directory ──────────────
    script_dir = sys.argv[1] if len(sys.argv) > 1 else ""
    coord_dir = os.path.normpath(os.path.join(script_dir, "..", "coord"))
    sys.path.insert(0, coord_dir)
    import coord

    # ── step 3(c0): anchor at the edited file (plan-wall round 3 FINDING 2)
    # coord's resolvers are CWD-relative git subprocesses; on the
    # delegation path there is no other anchor. `real` is computed ONCE,
    # here, and consumed (never recomputed) below.
    real = os.path.realpath(os.path.abspath(fp))
    anchor_dir = os.path.dirname(real)
    while True:
        try:
            os.chdir(anchor_dir)
            break
        except OSError:
            parent = os.path.dirname(anchor_dir)
            if parent == anchor_dir:
                break  # reached the root -- proceed anchorless
            anchor_dir = parent

    # ── step 3(c): store resolution, proven-ENOENT absence only ────────────
    root = coord._coord_store_root()
    try:
        os.stat(root)
    except FileNotFoundError:
        sys.exit(0)  # proven absent -> P-11 absent-is-a-pass
    # every other OSError (PermissionError, NotADirectoryError, ELOOP, ...)
    # PROPAGATES -> unreadable, never absent (plan-wall round 3 FINDING 1).

    store = coord._open_store()
    try:
        MODE = coord._resolve_mode(store)  # authoritative, overwrites the seed
        coord._require_filelock()  # the SOLE filelock gate on this path (P-16)

        # ── step 3(d): non-minting identity read (P-12) ─────────────────
        own_uuid = None
        session_env = os.environ.get("FFS_COORD_SESSION")
        run_id_env = os.environ.get("FFS_RUN_ID")
        try:
            if session_env:
                rec = coord._load_session_record(store, session_env)
                if rec is not None:
                    own_uuid = rec.get("session_uuid")
            elif run_id_env:
                own_uuid = coord._read_by_run_pointer(store, run_id_env)
        except coord.CoordExit:
            own_uuid = None  # malformed identity degrades, never blocks (P-12)

        # ── step 3(e): worktree root (hint, validated) + target key ─────
        wt = sys.argv[2] if len(sys.argv) > 2 else ""
        if not (isinstance(wt, str) and wt and os.path.isdir(wt)):
            wt = coord._worktree_root()
        rel = os.path.relpath(real, os.path.realpath(wt))
        if rel == ".." or rel.startswith(".." + os.sep):
            sys.exit(0)  # outside the worktree -- no lease can cover it
        rel_posix = rel.replace(os.sep, "/")
        rel_nfc = unicodedata.normalize("NFC", rel_posix)
        target = coord._fold_lease_key(rel_nfc, False)  # NOT the argument grammar (P-19)

        # ── step 3(f): registry read, lock-free, cached (P-13, P-18) ─────
        exclusive_leases = _exclusive_leases(coord, store)

        # ── step 3(g): scan (structurally cmd_lease_acquire's, read-only)
        now = time.time()
        blockers = []
        for key, entry in exclusive_leases.items():
            if not coord._lease_keys_overlap(key, target):
                continue
            for h_uuid, holder in entry["holders"].items():
                if own_uuid is not None and h_uuid == own_uuid:
                    continue  # P-06
                reclaimable, _verdict = coord._is_reclaimable(holder, now)  # P-17
                if reclaimable:
                    continue
                blockers.append((key, h_uuid, holder))

        if not blockers:
            sys.exit(0)

        # ── step 3(h): the message (REQ-07) ──────────────────────────────
        # NOT coord._print_lease_held: that helper prints the FULL holder
        # uuid, which is the FFS_COORD_SESSION impersonation token this
        # stderr must never carry (review-gate HIGH). Redacted local form.
        for _k, _u, _h in blockers:
            print(
                "LEASE-HELD "
                f"holder={_u[:8]}... "
                f"anchor_pid={_h.get('holder_anchor_pid')} "
                f"worktree={_h.get('holder_worktree')} "
                f"expires_at={_h.get('expires_at')}",
                file=sys.stderr,
            )
        key, h_uuid, holder = blockers[0]
        gen = holder["generation"]
        # held_by is TRUNCATED on purpose: the full session uuid IS the
        # FFS_COORD_SESSION impersonation token, and this stderr is read by
        # the very agent that was just blocked (review-gate HIGH finding).
        # The 8-char prefix identifies the holder in `status` output; the
        # true holder recovers its own full uuid from its own session env
        # or its sessions/ file, never from this message.
        body = "\n".join(
            [
                f"PATH-RESERVED {fp}",
                f"lease={key} held_by={h_uuid[:8]}... generation={gen}",
                "inspect:  python3 scripts/coord/coord.py status",
                f"release:  python3 scripts/coord/coord.py lease-release --resource {key} --generation {gen}  (holder only)",
                "or, if this lease is YOURS and the identity env is not visible to Claude Code:",
                "  re-export the FFS_COORD_SESSION value from the session that acquired it and retry",
            ]
        )
        if MODE == "audit":
            print(f"COORD-AUDIT: {body}", file=sys.stderr)
            sys.exit(0)
        print(body, file=sys.stderr)
        sys.exit(2)
    finally:
        coord._close_store(store)
except SystemExit:
    # Sanctioned control flow -- every sys.exit() above carries a literal 0
    # or 2. MUST precede the broad handler below: SystemExit subclasses
    # BaseException, and a bare `except BaseException` would swallow every
    # allow and re-emit it as an enforce-mode block (plan-wall round 1
    # FINDING 1 -- a repository-wide lockout on the happy path).
    raise
except BaseException as exc:
    # The two-outcome translation. Never forwards exc.code -- coord.py's
    # internal exit-code contract is CONSUMED here, never re-exported
    # (P-15). coord.CoordExit is a plain Exception, so 3/4/64/69/75/78
    # land here and nowhere else.
    reason = str(exc) or type(exc).__name__
    if MODE == "audit":
        print(f"COORD-AUDIT: {reason}", file=sys.stderr)
        sys.exit(0)
    print(f"COORD-GATE-FAIL: {reason}", file=sys.stderr)
    sys.exit(2)
PY
)

if [ -n "$STORE" ]; then
  export FFS_COORD_STORE="$STORE"
fi

printf '%s' "$INPUT" | python3 -c "$CORE" "$SCRIPT_DIR" "$WORKTREE_ROOT" "$MODE"
STATUS=$?

# ── translate python's status (P-15, half b) — the {0,2} boundary again ────
case "$STATUS" in
  0) exit 0 ;;
  2) exit 2 ;;
  *)
    if [ "$MODE" = "audit" ]; then
      echo "COORD-AUDIT: guard core exited $STATUS" >&2
      exit 0
    fi
    echo "COORD-GATE-FAIL: guard core exited $STATUS" >&2
    exit 2
    ;;
esac
