#!/usr/bin/env python3
"""scripts/coord/coord.py — cross-session claim coordination CLI.

A git-common-dir anchored store, a persisted session-UUID identity, a
monotonic generation (fencing) counter, and the
`claim | claim-check | claim-renew | release | status | doctor` CLI on top of
the external `filelock` library.

See .planning/phases/01-claims-core/01-01-PLAN.md for the full design and
the exit-code contract this module implements.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import errno
import json
import math
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import time
import unicodedata
import uuid
from pathlib import Path

# ── Exit-code contract (fixed for the whole coord layer) ────────────────────
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_SUPERSEDED = 4
EXIT_ENV_INVALID = 64
EXIT_UNAVAILABLE = 69
EXIT_CONTENTION = 75
EXIT_STORE_REFUSED = 78

# ── Constants ─────────────────────────────────────────────────────────────
DEFAULT_TTL_SECS = 300.0
DEFAULT_HEARTBEAT_SECS = 60.0
MAX_TTL_SECS = 86400.0
LEASE_ACQUIRE_TIMEOUT_SECS = 5.0
MAX_GENERATION = 2**63 - 1  # oversized --generation is a usage error, never compared

LEASE_PREFIX = "path:"
_GLOB_SUFFIX = "/**"

CLAIM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ENV_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
SESSION_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class CoordExit(Exception):
    """Raised to unwind to main() with an exact exit code and stderr message."""

    def __init__(self, code: int, message: str | None = None):
        super().__init__(message or "")
        self.code = code
        self.message = message


# ── filelock gate (REQ-12) — every subcommand routes through this ──────────
def _require_filelock():
    """Import filelock and enforce the >=3.30 floor via symbol presence.

    SoftFileLease and ReadWriteLock are NEVER instantiated anywhere in this
    module (P-04: no lease markers; P-08: ReadWriteLock retired on evidence
    from the installed 3.32.2 source) — they are the VERSION-FLOOR PROBE
    only, since SoftFileLease landed in 3.30 and ReadWriteLock in 3.21.
    Their absence is how a below-floor 3.29.x install is detected without
    parsing a version string. Do not remove this probe.
    """
    try:
        import filelock
    except ImportError as exc:
        raise CoordExit(
            EXIT_UNAVAILABLE, "coord: COORD-UNAVAILABLE filelock is not installed"
        ) from exc
    if not hasattr(filelock, "SoftFileLease") or not hasattr(filelock, "ReadWriteLock"):
        version = getattr(filelock, "__version__", "unknown")
        raise CoordExit(
            EXIT_UNAVAILABLE,
            f"coord: COORD-UNAVAILABLE filelock {version} is below the 3.30 floor",
        )
    return filelock


# ── identity module (start-token source) — degrades fail-closed ────────────
_IDENTITY_MOD_CACHE = "unset"


def _identity_module():
    """filelock's private start-token source. None => staleness=degraded."""
    global _IDENTITY_MOD_CACHE
    if _IDENTITY_MOD_CACHE == "unset":
        try:
            from filelock import _identity as mod

            _IDENTITY_MOD_CACHE = mod
        except ImportError:
            _IDENTITY_MOD_CACHE = None
    return _IDENTITY_MOD_CACHE


def _host_name() -> str:
    mod = _identity_module()
    if mod is not None:
        return mod.host_name()
    return socket.gethostname()


def _capture_start_token(pid: int | None):
    if pid is None:
        return None
    mod = _identity_module()
    if mod is None:
        return None
    try:
        return mod.process_start_token(pid)
    except Exception:
        return None


def _anchor_is_stale(pid, host, token) -> str:
    """Three-valued verdict: 'stale' | 'live' | 'unprobeable'.

    Never a boolean — collapsing "we could not tell" into either boolean is
    the bug this function exists to prevent (REQ-04, threat T-01-06).
    """
    if pid is None or host is None or token is None:
        return "unprobeable"
    mod = _identity_module()
    if mod is None:
        return "unprobeable"  # staleness=degraded, no token source at all
    if host != mod.host_name():
        return "unprobeable"  # a remote pid cannot be probed (Pitfall 7)
    if not mod.process_alive(pid):
        return "stale"
    current = mod.process_start_token(pid)
    if current is None:
        return "unprobeable"  # token unreadable for that pid specifically
    if current != token:
        return "stale"  # recycled pid
    return "live"


# ── store location + descriptor chain (REQ-02, threat T-01-04) ─────────────
@dataclasses.dataclass
class StoreFds:
    root_fd: int
    coord_fd: int
    sessions_fd: int
    by_run_fd: int
    all_fds: list
    store_root: Path
    lock_path: Path


def _git_common_dir() -> Path:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CoordExit(EXIT_STORE_REFUSED, "coord: not a git repository") from exc
    if result.returncode != 0:
        raise CoordExit(EXIT_STORE_REFUSED, "coord: not a git repository")
    return Path(result.stdout.strip())


def _resolve_store_location() -> tuple[Path, list[str]]:
    """Returns (trusted multi-component anchor, single-name components to
    openat in order down to the coord directory itself)."""
    env = os.environ.get("FFS_COORD_STORE")
    if env:
        p = Path(env)
        return p.parent, [p.name]
    common = _git_common_dir()
    repo_root = common.parent
    return repo_root, [".feature-fix-swarm", "coord"]


def _coord_store_root() -> Path:
    """The resolved store root as a plain Path, without opening descriptors —
    for callers that just need to know WHERE the store is (REQ-02)."""
    anchor, components = _resolve_store_location()
    return anchor.joinpath(*components)


def _openat_component(parent_fd: int, name: str, *, create: bool) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise CoordExit(EXIT_STORE_REFUSED, f"coord: store path missing: {name}")
        try:
            os.mkdir(name, dir_fd=parent_fd)
        except FileExistsError:
            pass
        try:
            return os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise CoordExit(
                    EXIT_STORE_REFUSED,
                    f"coord: refusing symlinked store path component: {name!r}",
                ) from exc
            raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise CoordExit(
                EXIT_STORE_REFUSED,
                f"coord: refusing symlinked store path component: {name!r}",
            ) from exc
        raise


def _refuse_if_symlink(dir_fd: int, name: str) -> None:
    try:
        st = os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise CoordExit(
            EXIT_STORE_REFUSED, f"coord: refusing symlinked store path: {name!r}"
        )


def _open_store() -> StoreFds:
    anchor, components = _resolve_store_location()
    try:
        anchor.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CoordExit(
            EXIT_STORE_REFUSED, f"coord: cannot create store anchor {anchor}: {exc}"
        ) from exc
    try:
        root_fd = os.open(str(anchor), os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise CoordExit(
            EXIT_STORE_REFUSED, f"coord: cannot open store anchor {anchor}: {exc}"
        ) from exc
    all_fds = [root_fd]
    try:
        cur = root_fd
        for name in components:
            cur = _openat_component(cur, name, create=True)
            all_fds.append(cur)
        coord_fd = cur
        sessions_fd = _openat_component(coord_fd, "sessions", create=True)
        all_fds.append(sessions_fd)
        by_run_fd = _openat_component(sessions_fd, "by-run", create=True)
        all_fds.append(by_run_fd)
        for leaf in ("registry.json", "registry.lock", "mode"):
            _refuse_if_symlink(coord_fd, leaf)
    except Exception:
        for fd in all_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    store_root = anchor.joinpath(*components)
    return StoreFds(
        root_fd=root_fd,
        coord_fd=coord_fd,
        sessions_fd=sessions_fd,
        by_run_fd=by_run_fd,
        all_fds=all_fds,
        store_root=store_root,
        lock_path=store_root / "registry.lock",
    )


def _close_store(store: StoreFds) -> None:
    for fd in store.all_fds:
        try:
            os.close(fd)
        except OSError:
            pass


# ── low-level fd-anchored I/O (no path strings, dir_fd + basename only) ────
def _read_text_fd(dir_fd: int, name: str) -> str:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    with os.fdopen(fd, "r") as f:
        return f.read()


def _read_json_fd(dir_fd: int, name: str):
    return json.loads(_read_text_fd(dir_fd, name))


def _atomic_write_json(dir_fd: int, name: str, data) -> None:
    token = secrets.token_hex(8)
    tmp_name = f".{name}-{token}.tmp"
    fd = os.open(
        tmp_name,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
        0o644,
        dir_fd=dir_fd,
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except Exception:
        try:
            os.unlink(tmp_name, dir_fd=dir_fd)
        except OSError:
            pass
        raise


# ── validation helpers ──────────────────────────────────────────────────────
def _validate_claim_id(raw: str) -> str:
    if raw is None or ".." in raw or not CLAIM_ID_RE.match(raw):
        raise CoordExit(EXIT_USAGE, f"coord: invalid spec-id: {raw!r}")
    return raw


def _validate_env_token(name: str, value: str | None) -> None:
    if value is None:
        return
    if not ENV_TOKEN_RE.match(value):
        raise CoordExit(EXIT_ENV_INVALID, f"coord: malformed {name}: {value!r}")


def _validate_positive_number(raw, label: str) -> float:
    try:
        val = float(raw)
    except (TypeError, ValueError) as exc:
        raise CoordExit(EXIT_USAGE, f"coord: invalid --{label}: {raw!r}") from exc
    if not math.isfinite(val) or val <= 0:
        raise CoordExit(
            EXIT_USAGE, f"coord: --{label} must be a positive finite number: {raw!r}"
        )
    return val


def _validate_ttl(ttl_arg, heartbeat_arg=None) -> float:
    heartbeat = DEFAULT_HEARTBEAT_SECS
    if heartbeat_arg is not None:
        heartbeat = _validate_positive_number(heartbeat_arg, "heartbeat")
    ttl = DEFAULT_TTL_SECS if ttl_arg is None else _validate_positive_number(ttl_arg, "ttl")
    if ttl > MAX_TTL_SECS:
        raise CoordExit(EXIT_USAGE, f"coord: --ttl exceeds MAX_TTL_SECS ({MAX_TTL_SECS})")
    if ttl < 4 * heartbeat:
        raise CoordExit(
            EXIT_USAGE, f"coord: --ttl must be >= 4x heartbeat ({4 * heartbeat})"
        )
    return ttl


def _validate_generation(raw) -> int:
    try:
        val = int(raw)
    except (TypeError, ValueError) as exc:
        raise CoordExit(EXIT_USAGE, f"coord: invalid --generation: {raw!r}") from exc
    if val < 0:
        raise CoordExit(EXIT_USAGE, f"coord: --generation must be non-negative: {raw!r}")
    if val > MAX_GENERATION:
        raise CoordExit(
            EXIT_USAGE, f"coord: --generation exceeds MAX_GENERATION ({MAX_GENERATION})"
        )
    return val


def _claim_key(claim_id: str) -> str:
    return f"claim:{claim_id}"


def _worktree_root() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return os.getcwd()


# ── identity (REQ-03) ────────────────────────────────────────────────────
def _resolve_anchor_pid() -> int:
    raw = os.environ.get("FFS_COORD_ANCHOR_PID")
    if raw:
        try:
            return int(raw)
        except ValueError as exc:
            raise CoordExit(
                EXIT_ENV_INVALID, f"coord: malformed FFS_COORD_ANCHOR_PID: {raw!r}"
            ) from exc
    return os.getppid()


def _load_session_record(store: StoreFds, session_uuid: str):
    name = f"{session_uuid}.json"
    _refuse_if_symlink(store.sessions_fd, name)
    try:
        return _read_json_fd(store.sessions_fd, name)
    except FileNotFoundError:
        return None


def _read_by_run_pointer(store: StoreFds, run_id: str):
    # A loser of the O_EXCL publish race can read the winner's pointer in the
    # open()-to-write() window and see it empty/partial (CI-observed torn
    # read). That state is TRANSIENT — retry briefly before declaring the
    # pointer corrupt. Permanent garbage still exits 69 after the retries.
    deadline_attempts = 5
    for attempt in range(deadline_attempts):
        _refuse_if_symlink(store.by_run_fd, run_id)
        try:
            raw = _read_text_fd(store.by_run_fd, run_id)
        except FileNotFoundError:
            return None
        content = raw[:-1] if raw.endswith("\n") else raw
        if SESSION_UUID_RE.match(content):
            return content
        if attempt < deadline_attempts - 1:
            time.sleep(0.01)
    raise CoordExit(
        EXIT_UNAVAILABLE,
        f"coord: COORD-UNAVAILABLE malformed by-run pointer sessions/by-run/{run_id}",
    )


def _publish_by_run_pointer(store: StoreFds, run_id: str, session_uuid: str) -> bool:
    try:
        fd = os.open(
            run_id,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o644,
            dir_fd=store.by_run_fd,
        )
    except FileExistsError:
        return False
    # Single unbuffered write: with fdopen the payload only hit the file at
    # close, leaving the pointer visibly EMPTY for the whole with-block —
    # the larger half of the torn-read window the reader retry covers.
    try:
        os.write(fd, (session_uuid + "\n").encode("ascii"))
    finally:
        os.close(fd)
    return True


def _mint_session(store: StoreFds, run_id, anchor_pid, anchor_start_token) -> dict:
    session_uuid = str(uuid.uuid4())
    record = {
        "session_uuid": session_uuid,
        "run_id": run_id,
        "anchor_pid": anchor_pid,
        "anchor_start_token": anchor_start_token,
        "host": _host_name(),
        "worktree": _worktree_root(),
        "created_at": time.time(),
        "cli_pid": os.getpid(),
    }
    _atomic_write_json(store.sessions_fd, f"{session_uuid}.json", record)
    if run_id:
        won = _publish_by_run_pointer(store, run_id, session_uuid)
        if not won:
            winner_uuid = _read_by_run_pointer(store, run_id)
            winner_record = _load_session_record(store, winner_uuid) if winner_uuid else None
            if winner_record is not None:
                return winner_record
    return record


def _adopt_with_rebind(store: StoreFds, record: dict, anchor_pid, anchor_start_token) -> dict:
    """ANCHOR REBIND on adoption (threat T-01-13/T-01-16).

    Registry is the authoritative anchor source: propagate the new anchor
    onto every claims entry this session holds INSIDE one registry
    transaction FIRST, then rewrite the session-record cache. A crash
    between the two is harmless because reclaim decisions only ever read
    the registry entry's holder_anchor_* fields.
    """
    recorded_pid = record.get("anchor_pid")
    if recorded_pid == anchor_pid:
        return record  # this invocation's own anchor — nothing to do
    verdict = _anchor_is_stale(
        recorded_pid, record.get("host"), record.get("anchor_start_token")
    )
    if verdict != "stale":
        # live or unprobeable: not provably dead — adopt unchanged, never steal
        # a foreign anchor we cannot prove is gone.
        return record
    session_uuid = record["session_uuid"]
    with registry_transaction(store) as registry:
        changed = False
        for entry in registry.get("claims", {}).values():
            if entry.get("holder_uuid") == session_uuid:
                entry["holder_anchor_pid"] = anchor_pid
                entry["holder_anchor_start_token"] = anchor_start_token
                entry["holder_host"] = _host_name()
                changed = True
        if changed:
            _save_registry(store, registry)
    record = dict(record)
    record["anchor_pid"] = anchor_pid
    record["anchor_start_token"] = anchor_start_token
    record["host"] = _host_name()
    record["cli_pid"] = os.getpid()
    _atomic_write_json(store.sessions_fd, f"{session_uuid}.json", record)
    return record


def _refuse_borrowed_identity(record: dict, anchor_pid) -> None:
    """EDGE-006/T-01-02: applies to resolution step (i) ONLY —
    $FFS_COORD_SESSION can name anywhere, including a live peer. Refuse
    only when the recorded anchor is alive, its token still matches, and it
    is not this invocation's own anchor. A dead or recycled anchor, or our
    own, is ordinary reuse and resolves normally."""
    recorded_pid = record.get("anchor_pid")
    if recorded_pid == anchor_pid:
        return
    verdict = _anchor_is_stale(
        recorded_pid, record.get("host"), record.get("anchor_start_token")
    )
    if verdict == "live":
        raise CoordExit(
            EXIT_STORE_REFUSED,
            f"coord: refusing borrowed identity — session "
            f"{record.get('session_uuid')} has a live anchor",
        )


def resolve_identity(store: StoreFds) -> dict:
    run_id = os.environ.get("FFS_RUN_ID")
    session_env = os.environ.get("FFS_COORD_SESSION")
    _validate_env_token("FFS_RUN_ID", run_id)
    _validate_env_token("FFS_COORD_SESSION", session_env)

    anchor_pid = _resolve_anchor_pid()
    anchor_start_token = _capture_start_token(anchor_pid)

    if session_env:
        record = _load_session_record(store, session_env)
        if record is None:
            raise CoordExit(
                EXIT_STORE_REFUSED,
                f"coord: FFS_COORD_SESSION names no known session: {session_env}",
            )
        _refuse_borrowed_identity(record, anchor_pid)  # step (i) ONLY
        return _adopt_with_rebind(store, record, anchor_pid, anchor_start_token)

    if run_id:
        pointer_uuid = _read_by_run_pointer(store, run_id)
        if pointer_uuid is not None:
            record = _load_session_record(store, pointer_uuid)
            if record is None:
                raise CoordExit(
                    EXIT_UNAVAILABLE,
                    f"coord: by-run pointer names no known session: {pointer_uuid}",
                )
            return _adopt_with_rebind(store, record, anchor_pid, anchor_start_token)

    return _mint_session(store, run_id, anchor_pid, anchor_start_token)


# ── registry transaction (REQ-01, decision P-01) ────────────────────────────
def _load_registry(store: StoreFds) -> dict:
    _refuse_if_symlink(store.coord_fd, "registry.json")
    try:
        registry = _read_json_fd(store.coord_fd, "registry.json")
    except FileNotFoundError:
        return {"version": 1, "claims": {}, "leases": {}, "generations": {}}
    except json.JSONDecodeError as exc:
        mode = _resolve_mode(store)
        if mode == "audit":
            print(
                f"coord: WARNING unparseable registry.json: {exc}", file=sys.stderr
            )
            return {"version": 1, "claims": {}, "leases": {}, "generations": {}}
        raise CoordExit(
            EXIT_UNAVAILABLE,
            f"coord: COORD-UNAVAILABLE unparseable {store.store_root / 'registry.json'}",
        ) from exc

    # NAMESPACE INIT (Phase 2, plan step 3a) — a registry loaded from disk is
    # history-influenced (older build, hand-edit, torn write) and is not
    # guaranteed to match the schema this function mints on a missing file.
    # setdefault covers ABSENT only; wrong TYPE survives it untouched and
    # would reach mutation code as a TypeError/AttributeError one level up
    # (T-02-10) — so every namespace is also asserted to be a dict here, the
    # ONE place every subcommand (claims included) routes through.
    for _ns in ("claims", "leases", "generations"):
        registry.setdefault(_ns, {})
        if not isinstance(registry[_ns], dict):
            raise CoordExit(
                EXIT_UNAVAILABLE,
                f"coord: COORD-UNAVAILABLE namespace not a dict offending_key={_ns}",
            )
    return registry


def _save_registry(store: StoreFds, data: dict) -> None:
    _atomic_write_json(store.coord_fd, "registry.json", data)


@contextlib.contextmanager
def registry_transaction(store: StoreFds):
    filelock = _require_filelock()
    _refuse_if_symlink(store.coord_fd, "registry.lock")
    lock = filelock.FileLock(str(store.lock_path), timeout=LEASE_ACQUIRE_TIMEOUT_SECS)
    try:
        lock.acquire()
    except filelock.Timeout as exc:
        raise CoordExit(
            EXIT_CONTENTION,
            f"coord: COORD-CONTENTION acquiring {store.lock_path}",
        ) from exc
    try:
        # T-01-17: the lock is addressed BY PATH, every write BY DESCRIPTOR.
        # After an ancestor rename/swap the two can silently diverge — verify
        # identity before touching the registry.
        coord_stat = os.fstat(store.coord_fd)
        lock_dir_stat = os.stat(os.path.dirname(str(store.lock_path)))
        if (coord_stat.st_dev, coord_stat.st_ino) != (
            lock_dir_stat.st_dev,
            lock_dir_stat.st_ino,
        ):
            raise CoordExit(
                EXIT_STORE_REFUSED,
                f"coord: store directory identity mismatch — moved/swapped "
                f"underneath this invocation ({store.store_root} vs "
                f"{os.path.dirname(str(store.lock_path))})",
            )
        registry = _load_registry(store)
        yield registry
    finally:
        lock.release()


def _resolve_mode_with_source(store: StoreFds) -> tuple[str, str]:
    env = os.environ.get("FFS_COORD_MODE")
    if env:
        if env not in ("off", "audit", "enforce"):
            raise CoordExit(EXIT_USAGE, f"coord: invalid FFS_COORD_MODE: {env!r}")
        return env, "env"
    try:
        content = _read_text_fd(store.coord_fd, "mode").strip()
    except FileNotFoundError:
        content = ""
    if content:
        if content not in ("off", "audit", "enforce"):
            raise CoordExit(EXIT_USAGE, f"coord: invalid mode file content: {content!r}")
        return content, "file"
    return "enforce", "default"


def _resolve_mode(store: StoreFds) -> str:
    mode, _source = _resolve_mode_with_source(store)
    return mode


# ── CLI subcommands ──────────────────────────────────────────────────────
def _grant_entry(gen_entry: dict, identity: dict, ttl_secs: float, now: float) -> dict:
    """A fresh grant OR a staleness-based reclaim (Task 3) — both mint a new
    generation and overwrite every holder_* field. `gen_entry` is the
    permanent generations[key] record; its `gen` is bumped in place."""
    new_gen = gen_entry["gen"] + 1
    gen_entry["gen"] = new_gen
    return {
        "holder_uuid": identity["session_uuid"],
        "holder_anchor_pid": identity["anchor_pid"],
        "holder_anchor_start_token": identity["anchor_start_token"],
        "holder_host": identity["host"],
        "holder_worktree": identity["worktree"],
        "generation": new_gen,
        "acquired_at": now,
        "last_renewed_at": now,
        "ttl_secs": ttl_secs,
        "expires_at": now + ttl_secs,
        "cli_pid": os.getpid(),
    }


def _print_claim_held(entry: dict) -> None:
    print(
        "CLAIM-HELD "
        f"holder={entry.get('holder_uuid')} "
        f"anchor_pid={entry.get('holder_anchor_pid')} "
        f"worktree={entry.get('holder_worktree')} "
        f"expires_at={entry.get('expires_at')}",
        file=sys.stderr,
    )


def _is_reclaimable(entry: dict, now: float) -> tuple[bool, str]:
    """THE RECLAIM DECISION (REQ-04) — one ordered list, first match wins.
    Returns (reclaimable, anchor_verdict) for callers that also want the
    verdict for diagnostics/tests."""
    worktree = entry.get("holder_worktree")
    if worktree and not os.path.exists(worktree):
        return True, "worktree-gone"  # spec EDGE-002, no TTL wait

    verdict = _anchor_is_stale(
        entry.get("holder_anchor_pid"),
        entry.get("holder_host"),
        entry.get("holder_anchor_start_token"),
    )
    if verdict == "stale":
        return True, verdict
    if verdict == "live":
        return False, verdict  # never reclaimable at any age

    # unprobeable: the SOLE bounded recovery route, gated on the entry's OWN
    # ttl_secs — never DEFAULT_TTL_SECS directly (the reclaiming process's
    # compiled-in default is not the interval the holder was granted under).
    ttl = entry.get("ttl_secs")
    if ttl is None:
        ttl = DEFAULT_TTL_SECS
    last = entry.get("last_renewed_at")
    if last is None:
        last = entry.get("acquired_at")
    if last is None:
        return True, verdict  # fully torn record, no clock at all
    return (now - last) >= ttl, verdict


# ── lease resource validation (REQ-06, P-07, threats T-02-01/02/03) ────────
def _lease_parse_raw(raw) -> tuple[str, bool]:
    """Stages (a)-(d): prefix, NFC, form/glob-charset, lexical. Pure — no
    filesystem call of any kind. Returns (literal, is_prefix_form) where
    `literal` is NFC-normalized but NOT yet casefolded (containment must run
    against the real case; folding is stage (f), applied by the caller)."""
    if raw is None or not isinstance(raw, str) or not raw.startswith(LEASE_PREFIX):
        raise CoordExit(
            EXIT_USAGE, f"coord: lease resource must start with 'path:': {raw!r}"
        )
    body = raw[len(LEASE_PREFIX):]
    # NFC FIRST — before any charset/form check (Pitfall 2): normalization
    # changes the bytes those checks inspect.
    body = unicodedata.normalize("NFC", body)
    is_prefix_form = body.endswith(_GLOB_SUFFIX)
    literal = body[: -len(_GLOB_SUFFIX)] if is_prefix_form else body
    if not literal or any(c in literal for c in "*?[]"):
        raise CoordExit(
            EXIT_USAGE, f"coord: unsupported path resource syntax: {raw!r}"
        )
    # control characters — not covered by the glob-charset check above;
    # os.path.realpath raises ValueError on an embedded NUL, which would
    # otherwise unwind past CoordExit to a traceback and exit 1.
    if any(ord(ch) < 0x20 or ch == "\x7f" for ch in body):
        raise CoordExit(
            EXIT_USAGE, f"coord: control character in path resource: {raw!r}"
        )
    if literal.startswith("/") or os.path.isabs(literal):
        raise CoordExit(EXIT_USAGE, f"coord: absolute paths not accepted: {raw!r}")
    parts = literal.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise CoordExit(
            EXIT_USAGE, f"coord: invalid path segment in resource: {raw!r}"
        )
    return literal, is_prefix_form


def _fold_lease_key(literal: str, is_prefix_form: bool) -> str:
    """Stage (f) — casefold for STORAGE/COMPARISON only, never for display.
    Per-segment fold, then join: casefold can change string LENGTH, so
    index-based slicing across a partially-folded pair is unsound."""
    folded = "/".join(seg.casefold() for seg in literal.split("/"))
    return LEASE_PREFIX + folded + (_GLOB_SUFFIX if is_prefix_form else "")


def _lease_key_from_arg(raw: str) -> str:
    """The LEXICAL entry point — stages (a)-(d)+(f). No filesystem call.
    Deterministic and idempotent: feeding it an already-canonical key
    returns that key unchanged. Used by lease-renew/lease-release (a
    granted key is a registry lookup, not a path) and by every read-side
    key-grammar check in `_lease_entries`."""
    literal, is_prefix_form = _lease_parse_raw(raw)
    return _fold_lease_key(literal, is_prefix_form)


def _validate_lease_resource(raw: str, root: str) -> str:
    """`_lease_key_from_arg` THEN stage (e), the realpath containment check.
    Used ONLY by lease-acquire — once granted, a lease key is a registry
    lookup and containment is diagnostic-only afterwards (T-02-11)."""
    literal, is_prefix_form = _lease_parse_raw(raw)
    candidate = os.path.join(root, literal)
    real = os.path.realpath(candidate)
    real_root = os.path.realpath(root)
    if real != real_root and not real.startswith(real_root + os.sep):
        raise CoordExit(
            EXIT_STORE_REFUSED, f"coord: path resource escapes worktree root: {raw!r}"
        )
    return _fold_lease_key(literal, is_prefix_form)


def _parse_lease_resource(key: str) -> tuple[tuple[str, ...], bool]:
    """`key` is an already-validated, already-folded 'path:<form>' string."""
    body = key[len(LEASE_PREFIX):]
    is_prefix_form = body.endswith(_GLOB_SUFFIX)
    literal = body[: -len(_GLOB_SUFFIX)] if is_prefix_form else body
    return tuple(literal.split("/")), is_prefix_form


def _lease_keys_overlap(key_a: str, key_b: str) -> bool:
    """Tuple-prefix comparison (RESEARCH Pattern 3) — no fnmatch, no regex
    glob engine, no pathlib glob matching (Don't Hand-Roll)."""
    a, a_is_prefix = _parse_lease_resource(key_a)
    b, b_is_prefix = _parse_lease_resource(key_b)
    if not a_is_prefix and not b_is_prefix:
        return a == b
    if a_is_prefix and not b_is_prefix:
        return len(b) > len(a) and b[: len(a)] == a
    if b_is_prefix and not a_is_prefix:
        return len(a) > len(b) and a[: len(b)] == b
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return longer[: len(shorter)] == shorter


# ── lease store structural validation (T-02-10) ─────────────────────────────
def _is_plain_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_plain_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate_lease_key(key) -> None:
    """Re-derive the key through the lexical validator and require it to
    EQUAL the stored key — catches both a non-conforming key (claim-shaped,
    bad charset) and a merely non-canonical one (unfolded, NFD-encoded),
    which would silently miss every overlap comparison. Works because
    stages (a)-(d)+(f) are idempotent on an already-canonical key."""
    if not isinstance(key, str):
        raise CoordExit(
            EXIT_UNAVAILABLE,
            f"coord: COORD-UNAVAILABLE malformed lease key offending_key={key!r}",
        )
    try:
        canonical = _lease_key_from_arg(key)
    except CoordExit as exc:
        raise CoordExit(
            EXIT_UNAVAILABLE,
            f"coord: COORD-UNAVAILABLE malformed lease key offending_key={key!r}",
        ) from exc
    if canonical != key:
        raise CoordExit(
            EXIT_UNAVAILABLE,
            f"coord: COORD-UNAVAILABLE non-canonical lease key offending_key={key!r}",
        )


def _validate_lease_holder(key: str, holder) -> None:
    """PER-FIELD CLASSIFICATION (plan step 3b table). `generation` is the
    one field promoted to REQUIRED — every other field is torn-tolerant
    (absent/None routes to the UNPROBEABLE/torn path); WRONG TYPE anywhere
    is corruption, exit 69, naming the key, never repaired."""
    if not isinstance(holder, dict):
        raise CoordExit(
            EXIT_UNAVAILABLE,
            f"coord: COORD-UNAVAILABLE malformed lease holder offending_key={key}",
        )
    gen = holder.get("generation")
    if not _is_plain_int(gen) or gen < 1:
        raise CoordExit(
            EXIT_UNAVAILABLE,
            f"coord: COORD-UNAVAILABLE malformed lease holder generation offending_key={key}",
        )
    pid = holder.get("holder_anchor_pid")
    if pid is not None and not _is_plain_int(pid):
        raise CoordExit(
            EXIT_UNAVAILABLE,
            f"coord: COORD-UNAVAILABLE malformed lease holder anchor_pid offending_key={key}",
        )
    token = holder.get("holder_anchor_start_token")
    if token is not None and not isinstance(token, (int, str)):
        raise CoordExit(
            EXIT_UNAVAILABLE,
            f"coord: COORD-UNAVAILABLE malformed lease holder start_token offending_key={key}",
        )
    for field in ("last_renewed_at", "acquired_at", "ttl_secs", "expires_at"):
        val = holder.get(field)
        if val is not None and not _is_plain_number(val):
            raise CoordExit(
                EXIT_UNAVAILABLE,
                f"coord: COORD-UNAVAILABLE malformed lease holder {field} offending_key={key}",
            )
    worktree = holder.get("holder_worktree")
    if worktree is not None and not isinstance(worktree, str):
        raise CoordExit(
            EXIT_UNAVAILABLE,
            f"coord: COORD-UNAVAILABLE malformed lease holder worktree offending_key={key}",
        )
    host = holder.get("holder_host")
    if host is not None and not isinstance(host, str):
        raise CoordExit(
            EXIT_UNAVAILABLE,
            f"coord: COORD-UNAVAILABLE malformed lease holder host offending_key={key}",
        )


def _validate_generations_record(key: str, record) -> None:
    """Generations records read by LEASE code (P-05) get the same
    discipline: a bare int (pre-Phase-1 shape) or string is corruption."""
    if not isinstance(record, dict):
        raise CoordExit(
            EXIT_UNAVAILABLE,
            f"coord: COORD-UNAVAILABLE malformed generations record offending_key={key}",
        )
    gen = record.get("gen")
    if gen is not None and (not _is_plain_int(gen) or gen < 0):
        raise CoordExit(
            EXIT_UNAVAILABLE,
            f"coord: COORD-UNAVAILABLE malformed generations record offending_key={key}",
        )
    released = record.get("released_holders")
    if released is not None:
        if not isinstance(released, dict):
            raise CoordExit(
                EXIT_UNAVAILABLE,
                f"coord: COORD-UNAVAILABLE malformed generations record offending_key={key}",
            )
        for rec in released.values():
            if not isinstance(rec, dict):
                raise CoordExit(
                    EXIT_UNAVAILABLE,
                    f"coord: COORD-UNAVAILABLE malformed generations record offending_key={key}",
                )
            rgen = rec.get("generation")
            if not _is_plain_int(rgen):
                raise CoordExit(
                    EXIT_UNAVAILABLE,
                    f"coord: COORD-UNAVAILABLE malformed generations record offending_key={key}",
                )
            rat = rec.get("released_at")
            if rat is not None and not _is_plain_number(rat):
                raise CoordExit(
                    EXIT_UNAVAILABLE,
                    f"coord: COORD-UNAVAILABLE malformed generations record offending_key={key}",
                )


def _lease_entries(registry: dict) -> dict:
    """The SOLE read accessor for `registry["leases"]` — every lease
    subcommand and `status`/`doctor` route through this. Validates every
    key and entry it yields; raises exit 69 COORD-UNAVAILABLE naming the
    offending key on any structural corruption (T-02-10). No repair."""
    leases = registry.get("leases", {})
    if not isinstance(leases, dict):
        raise CoordExit(
            EXIT_UNAVAILABLE, "coord: COORD-UNAVAILABLE leases namespace not a dict"
        )
    for key, entry in leases.items():
        _validate_lease_key(key)
        if not isinstance(entry, dict):
            raise CoordExit(
                EXIT_UNAVAILABLE,
                f"coord: COORD-UNAVAILABLE malformed lease entry offending_key={key}",
            )
        if entry.get("mode") not in ("shared", "exclusive"):
            raise CoordExit(
                EXIT_UNAVAILABLE,
                f"coord: COORD-UNAVAILABLE malformed lease mode offending_key={key}",
            )
        holders = entry.get("holders")
        if not isinstance(holders, dict):
            raise CoordExit(
                EXIT_UNAVAILABLE,
                f"coord: COORD-UNAVAILABLE malformed lease holders offending_key={key}",
            )
        for holder in holders.values():
            _validate_lease_holder(key, holder)
    return leases


def _lease_gen_entry(registry: dict, key: str) -> dict:
    """generations[key] for a LEASE key — created lazily, same shape as the
    claim path, validated with the lease-side discipline (P-05)."""
    gen_entry = registry["generations"].setdefault(
        key, {"gen": 0, "last_holder_uuid": None, "released_at": None}
    )
    _validate_generations_record(key, gen_entry)
    gen_entry.setdefault("gen", 0)  # absent gen reads as 0 (torn OK, coord.py:711 precedent)
    return gen_entry


def cmd_claim(store: StoreFds, args) -> int:
    claim_id = _validate_claim_id(args.spec_id)
    ttl_secs = _validate_ttl(args.ttl, args.heartbeat)
    identity = resolve_identity(store)
    print(f"session={identity['session_uuid']}")
    with registry_transaction(store) as registry:
        key = _claim_key(claim_id)
        gen_entry = registry["generations"].setdefault(
            key, {"gen": 0, "last_holder_uuid": None, "released_at": None}
        )
        now = time.time()
        entry = registry["claims"].get(key)

        if entry is None:
            new_entry = _grant_entry(gen_entry, identity, ttl_secs, now)
            registry["claims"][key] = new_entry
            _save_registry(store, registry)
            print(f"CLAIM-OK generation={new_entry['generation']}")
            return EXIT_OK

        if entry["holder_uuid"] == identity["session_uuid"]:
            # idempotent re-claim: generation byte-identical, refresh the
            # clock and TTL (carried forward unless this invocation passed
            # an explicit --ttl), re-copy CURRENT anchor fields (post-rebind).
            eff_ttl = args.ttl if args.ttl is not None else entry.get("ttl_secs", DEFAULT_TTL_SECS)
            if args.ttl is not None:
                eff_ttl = _validate_ttl(args.ttl, args.heartbeat)
            entry["last_renewed_at"] = now
            entry["ttl_secs"] = eff_ttl
            entry["expires_at"] = now + eff_ttl
            entry["holder_anchor_pid"] = identity["anchor_pid"]
            entry["holder_anchor_start_token"] = identity["anchor_start_token"]
            entry["holder_host"] = identity["host"]
            entry["holder_worktree"] = identity["worktree"]
            _save_registry(store, registry)
            print(f"CLAIM-OK generation={entry['generation']}")
            return EXIT_OK

        reclaimable, _verdict = _is_reclaimable(entry, now)
        if not reclaimable:
            _print_claim_held(entry)
            return EXIT_REFUSED

        new_entry = _grant_entry(gen_entry, identity, ttl_secs, now)
        registry["claims"][key] = new_entry
        _save_registry(store, registry)
        print(f"CLAIM-OK generation={new_entry['generation']}")
        return EXIT_OK


def cmd_claim_check(store: StoreFds, args) -> int:
    claim_id = _validate_claim_id(args.spec_id)
    generation = _validate_generation(args.generation)
    identity = resolve_identity(store)
    print(f"session={identity['session_uuid']}")
    with registry_transaction(store) as registry:
        key = _claim_key(claim_id)
        entry = registry["claims"].get(key)
        current_gen = entry["generation"] if entry else registry["generations"].get(key, {}).get("gen", 0)
        if entry is not None and entry["generation"] == generation:
            print("CLAIM-OK")
            return EXIT_OK
        print(
            f"CLAIM-SUPERSEDED caller_generation={generation} current_generation={current_gen}",
            file=sys.stderr,
        )
        return EXIT_SUPERSEDED


def cmd_claim_renew(store: StoreFds, args) -> int:
    claim_id = _validate_claim_id(args.spec_id)
    generation = _validate_generation(args.generation)
    identity = resolve_identity(store)
    print(f"session={identity['session_uuid']}")
    with registry_transaction(store) as registry:
        key = _claim_key(claim_id)
        entry = registry["claims"].get(key)

        if entry is None:
            # the authority is gone — same news as CLAIM-SUPERSEDED.
            print(
                f"CLAIM-SUPERSEDED (no such claim) caller_generation={generation}",
                file=sys.stderr,
            )
            return EXIT_SUPERSEDED

        if entry["holder_uuid"] != identity["session_uuid"]:
            _print_claim_held(entry)
            return EXIT_REFUSED

        # HOLDER UUID IS NOT PROOF OF GENERATION: the caller must state the
        # generation it believes it holds, and be fenced if that authority
        # has been revoked (a stale process from an earlier incarnation
        # shares the same holder_uuid across a reclaim-and-re-acquire cycle).
        if entry["generation"] != generation:
            print(
                f"CLAIM-SUPERSEDED caller_generation={generation} "
                f"current_generation={entry['generation']}",
                file=sys.stderr,
            )
            return EXIT_SUPERSEDED  # entry byte-identical: no write below this line

        now = time.time()
        eff_ttl = entry.get("ttl_secs", DEFAULT_TTL_SECS)
        if args.ttl is not None:
            eff_ttl = _validate_ttl(args.ttl, getattr(args, "heartbeat", None))
        entry["last_renewed_at"] = now
        entry["ttl_secs"] = eff_ttl
        entry["expires_at"] = now + eff_ttl
        entry["holder_anchor_pid"] = identity["anchor_pid"]
        entry["holder_anchor_start_token"] = identity["anchor_start_token"]
        entry["holder_host"] = identity["host"]
        _save_registry(store, registry)
        print(f"CLAIM-OK generation={entry['generation']}")
        return EXIT_OK


def cmd_release(store: StoreFds, args) -> int:
    claim_id = _validate_claim_id(args.spec_id)
    generation = _validate_generation(args.generation)
    identity = resolve_identity(store)
    print(f"session={identity['session_uuid']}")
    with registry_transaction(store) as registry:
        key = _claim_key(claim_id)
        entry = registry["claims"].get(key)
        gen_entry = registry["generations"].setdefault(
            key, {"gen": 0, "last_holder_uuid": None, "released_at": None}
        )
        now = time.time()

        if entry is not None:
            if entry["holder_uuid"] != identity["session_uuid"]:
                print("RELEASE-REFUSED: foreign holder", file=sys.stderr)
                return EXIT_REFUSED
            if entry["generation"] != generation:
                print("RELEASE-REFUSED: stale generation", file=sys.stderr)
                return EXIT_REFUSED
            del registry["claims"][key]
            gen_entry["last_holder_uuid"] = identity["session_uuid"]
            gen_entry["released_at"] = now
            _save_registry(store, registry)
            print("RELEASE-OK")
            return EXIT_OK

        # entry ABSENT: the tombstone is the only thing that can tell an
        # idempotent second release by the real holder apart from a foreign
        # release — entry-absence alone cannot (T-01-11).
        if gen_entry.get("last_holder_uuid") == identity["session_uuid"]:
            print("RELEASE-OK")
            return EXIT_OK
        print("RELEASE-REFUSED: not last holder", file=sys.stderr)
        return EXIT_REFUSED


def _print_lease_held(blockers: list[tuple[str, dict]]) -> None:
    """Names EVERY surviving foreign holder, not just the first — a shared
    lease can block an exclusive request with N holders."""
    for _key, holder in blockers:
        print(
            "LEASE-HELD "
            f"holder={holder.get('holder_uuid')} "
            f"anchor_pid={holder.get('holder_anchor_pid')} "
            f"worktree={holder.get('holder_worktree')} "
            f"expires_at={holder.get('expires_at')}",
            file=sys.stderr,
        )


def cmd_lease_acquire(store: StoreFds, args) -> int:
    root = _worktree_root()
    key = _validate_lease_resource(args.resource, root)
    mode = args.mode
    ttl_secs = _validate_ttl(args.ttl, args.heartbeat)
    identity = resolve_identity(store)
    print(f"session={identity['session_uuid']}")
    with registry_transaction(store) as registry:
        leases = _lease_entries(registry)
        now = time.time()

        overlapping = [(k, e) for k, e in leases.items() if _lease_keys_overlap(k, key)]

        # P-06: evaluate FOREIGN holders only — a session never conflicts
        # with itself, in any mode combination, same-key or cross-key.
        blocking: list[tuple[str, dict]] = []
        to_prune: list[tuple[str, str]] = []
        for k, entry in overlapping:
            for h_uuid, holder in entry["holders"].items():
                if h_uuid == identity["session_uuid"]:
                    continue
                reclaimable, _verdict = _is_reclaimable(holder, now)
                if reclaimable:
                    to_prune.append((k, h_uuid))
                    continue
                if entry["mode"] == "exclusive" or mode == "exclusive":
                    blocking.append((k, holder))

        if blocking:
            _print_lease_held(blocking)
            return EXIT_REFUSED  # registry byte-identical: no write below this line

        # GRANT. Prune reclaimable foreign holders found above, delete any
        # overlapping entry whose holders became empty.
        for k, h_uuid in to_prune:
            del leases[k]["holders"][h_uuid]
        for k, _snapshot in overlapping:
            if k in leases and not leases[k]["holders"]:
                del leases[k]

        entry = leases.get(key)
        if entry is not None and identity["session_uuid"] in entry["holders"]:
            # idempotent self re-acquire (mirrors cmd_claim, coord.py
            # same-uuid branch): refresh clock/TTL/anchor, generation
            # UNCHANGED, mode may flip (a self-only entry, or one where
            # every foreign holder was just pruned above).
            holder = entry["holders"][identity["session_uuid"]]
            holder["last_renewed_at"] = now
            holder["ttl_secs"] = ttl_secs
            holder["expires_at"] = now + ttl_secs
            holder["holder_anchor_pid"] = identity["anchor_pid"]
            holder["holder_anchor_start_token"] = identity["anchor_start_token"]
            holder["holder_host"] = identity["host"]
            holder["holder_worktree"] = identity["worktree"]
            entry["mode"] = mode
            leases[key] = entry
            _save_registry(store, registry)
            print(f"LEASE-OK generation={holder['generation']}")
            return EXIT_OK

        gen_entry = _lease_gen_entry(registry, key)
        new_holder = _grant_entry(gen_entry, identity, ttl_secs, now)
        if entry is None:
            entry = {"mode": mode, "holders": {}}
        entry["mode"] = mode
        entry["holders"][identity["session_uuid"]] = new_holder  # MUTATE in place
        leases[key] = entry
        _save_registry(store, registry)
        print(f"LEASE-OK generation={new_holder['generation']}")
        return EXIT_OK


def cmd_lease_renew(store: StoreFds, args) -> int:
    # LEXICAL entry point only — a granted key is a registry lookup, never a
    # filesystem path; re-running containment here would strand an escaped
    # lease as unrenewable until TTL expiry (T-02-11).
    key = _lease_key_from_arg(args.resource)
    generation = _validate_generation(args.generation)
    identity = resolve_identity(store)
    print(f"session={identity['session_uuid']}")
    with registry_transaction(store) as registry:
        leases = _lease_entries(registry)
        entry = leases.get(key)
        holder = entry["holders"].get(identity["session_uuid"]) if entry else None

        if holder is None or holder["generation"] != generation:
            current = holder["generation"] if holder is not None else None
            print(
                f"LEASE-SUPERSEDED caller_generation={generation} "
                f"current_generation={current}",
                file=sys.stderr,
            )
            return EXIT_SUPERSEDED  # entry byte-identical: no write below this line

        now = time.time()
        eff_ttl = holder.get("ttl_secs", DEFAULT_TTL_SECS)
        if args.ttl is not None:
            eff_ttl = _validate_ttl(args.ttl, getattr(args, "heartbeat", None))
        holder["last_renewed_at"] = now
        holder["ttl_secs"] = eff_ttl
        holder["expires_at"] = now + eff_ttl
        holder["holder_anchor_pid"] = identity["anchor_pid"]
        holder["holder_anchor_start_token"] = identity["anchor_start_token"]
        holder["holder_host"] = identity["host"]
        _save_registry(store, registry)
        print(f"LEASE-OK generation={holder['generation']}")
        return EXIT_OK


def cmd_lease_release(store: StoreFds, args) -> int:
    key = _lease_key_from_arg(args.resource)
    generation = _validate_generation(args.generation)
    identity = resolve_identity(store)
    print(f"session={identity['session_uuid']}")
    with registry_transaction(store) as registry:
        leases = _lease_entries(registry)
        entry = leases.get(key)
        now = time.time()

        # ORDERED LADDER, first match wins (P-05/T-02-04):
        # 1. caller IS a current holder (presence in holders{} IS the uuid check)
        if entry is not None and identity["session_uuid"] in entry["holders"]:
            holder = entry["holders"][identity["session_uuid"]]
            if holder["generation"] != generation:
                print("RELEASE-REFUSED: stale generation", file=sys.stderr)
                return EXIT_REFUSED
            del entry["holders"][identity["session_uuid"]]
            if not entry["holders"]:
                del leases[key]
            gen_entry = _lease_gen_entry(registry, key)
            gen_entry.setdefault("released_holders", {})[identity["session_uuid"]] = {
                "generation": generation,
                "released_at": now,
            }
            _save_registry(store, registry)
            print("RELEASE-OK")
            return EXIT_OK

        # 2. caller is NOT a current holder, but has a tombstone — consulted
        # BEFORE the refusal in branch 3 (a shared co-holder's idempotent
        # re-release while the entry still exists is not "foreign").
        gen_entry = registry["generations"].get(key)
        if gen_entry is not None:
            _validate_generations_record(key, gen_entry)
        tombstone = (gen_entry or {}).get("released_holders", {}).get(
            identity["session_uuid"]
        )
        if tombstone is not None:
            if tombstone.get("generation") == generation:
                print("RELEASE-OK")
                return EXIT_OK
            print("RELEASE-REFUSED: stale generation", file=sys.stderr)
            return EXIT_REFUSED

        # 3. neither — stranger, or a holder pruned by an overlapping
        # acquire (reclaimed, never tombstoned). Phase 1's "foreign holder"
        # string is NOT reused here — misleading to a reclaimed holder.
        held_by = ",".join(sorted(entry["holders"].keys())) if entry else ""
        suffix = f" held_by={held_by}" if held_by else ""
        print(f"RELEASE-REFUSED: not a recorded holder{suffix}", file=sys.stderr)
        return EXIT_REFUSED


def cmd_status(store: StoreFds, args) -> int:
    with registry_transaction(store) as registry:
        claims = dict(registry.get("claims", {}))
        leases = dict(_lease_entries(registry))
    now = time.time()
    for key, entry in sorted(claims.items()):
        flags = []
        worktree = entry.get("holder_worktree")
        if worktree and not os.path.exists(worktree):
            flags.append("MISSING-WORKTREE")
        holder_host = entry.get("holder_host")
        if holder_host and holder_host != _host_name():
            flags.append("FOREIGN-HOST")
        if (
            entry.get("holder_anchor_pid") is None
            or entry.get("last_renewed_at") is None
            or entry.get("ttl_secs") is None
        ):
            flags.append("INCOMPLETE-ENTRY")
        line = (
            f"{key} holder={entry.get('holder_uuid')} "
            f"generation={entry.get('generation')} "
            f"anchor_pid={entry.get('holder_anchor_pid')} "
            f"cli_pid={entry.get('cli_pid')} "
            f"worktree={worktree} "
            f"last_renewed_at={entry.get('last_renewed_at')} "
            f"ttl_secs={entry.get('ttl_secs')} "
            f"expires_at={entry.get('expires_at')}"
        )
        if flags:
            line += " flags=" + ",".join(flags)
        print(line)
    for key, lease in sorted(leases.items()):
        mode = lease.get("mode")
        for holder_uuid, entry in sorted(lease.get("holders", {}).items()):
            flags = []
            worktree = entry.get("holder_worktree")
            if worktree and not os.path.exists(worktree):
                flags.append("MISSING-WORKTREE")
            holder_host = entry.get("holder_host")
            if holder_host and holder_host != _host_name():
                flags.append("FOREIGN-HOST")
            if (
                entry.get("holder_anchor_pid") is None
                or entry.get("last_renewed_at") is None
                or entry.get("ttl_secs") is None
            ):
                flags.append("INCOMPLETE-ENTRY")
            line = (
                f"{key} mode={mode} holder={holder_uuid} "
                f"generation={entry.get('generation')} "
                f"anchor_pid={entry.get('holder_anchor_pid')} "
                f"cli_pid={entry.get('cli_pid')} "
                f"worktree={worktree} "
                f"last_renewed_at={entry.get('last_renewed_at')} "
                f"ttl_secs={entry.get('ttl_secs')} "
                f"expires_at={entry.get('expires_at')}"
            )
            if flags:
                line += " flags=" + ",".join(flags)
            print(line)
    return EXIT_OK


def cmd_doctor(store: StoreFds, args) -> int:
    """REQ-12: the surface Phase 4's preflight probes. Never raises — names
    the offending state and returns the contract exit code instead."""
    try:
        filelock = _require_filelock()
    except CoordExit as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    version = getattr(filelock, "__version__", "unknown")

    staleness = "ok" if _identity_module() is not None else "degraded"
    try:
        mode, mode_source = _resolve_mode_with_source(store)
    except CoordExit as exc:
        print(exc.message, file=sys.stderr)
        return exc.code

    try:
        with registry_transaction(store) as registry:
            live_claims = len(registry.get("claims", {}))
    except CoordExit as exc:
        print(exc.message, file=sys.stderr)
        return exc.code

    live_leases = 0
    lease_holders = 0
    escaped: list[str] = []
    try:
        with registry_transaction(store) as registry:
            leases = _lease_entries(registry)
            live_leases = len(leases)
            lease_holders = sum(len(e["holders"]) for e in leases.values())
            # T-02-11: DIAGNOSTIC ONLY, never enforced. Re-run the
            # acquisition-time containment resolution against every live
            # lease key; degrade a single key to "unknown" on OSError
            # rather than taking down doctor, which by contract never
            # raises.
            root = _worktree_root()
            real_root = os.path.realpath(root)
            for key in leases:
                try:
                    segments, _is_prefix = _parse_lease_resource(key)
                    candidate = os.path.join(root, *segments)
                    real = os.path.realpath(candidate)
                    if real != real_root and not real.startswith(real_root + os.sep):
                        escaped.append(key)
                except OSError:
                    pass
    except CoordExit as exc:
        print(f"lease_store=corrupt {exc.message}", file=sys.stderr)
        return exc.code

    print(f"filelock_version={version}")
    print(f"store_path={store.store_root}")
    print(f"mode={mode} mode_source={mode_source}")
    print(f"staleness={staleness}")
    print(f"live_claims={live_claims}")
    print(f"live_leases={live_leases}")
    print(f"lease_holders={lease_holders}")
    if escaped:
        print(f"escaped_leases={','.join(escaped)}")
    return EXIT_OK


COMMANDS = {
    "claim": cmd_claim,
    "claim-check": cmd_claim_check,
    "claim-renew": cmd_claim_renew,
    "release": cmd_release,
    "lease-acquire": cmd_lease_acquire,
    "lease-renew": cmd_lease_renew,
    "lease-release": cmd_lease_release,
    "status": cmd_status,
    "doctor": cmd_doctor,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coord.py")
    sub = parser.add_subparsers(dest="command", required=True)

    claim = sub.add_parser("claim")
    claim.add_argument("spec_id")
    claim.add_argument("--ttl")
    claim.add_argument("--heartbeat")

    check = sub.add_parser("claim-check")
    check.add_argument("spec_id")
    check.add_argument("--generation", required=True)

    renew = sub.add_parser("claim-renew")
    renew.add_argument("spec_id")
    renew.add_argument("--generation", required=True)
    renew.add_argument("--ttl")

    release = sub.add_parser("release")
    release.add_argument("spec_id")
    release.add_argument("--generation", required=True)

    lease_acquire = sub.add_parser("lease-acquire")
    lease_acquire.add_argument("--resource", required=True)
    lease_acquire.add_argument("--mode", required=True, choices=("shared", "exclusive"))
    lease_acquire.add_argument("--ttl")
    lease_acquire.add_argument("--heartbeat")

    lease_renew = sub.add_parser("lease-renew")
    lease_renew.add_argument("--resource", required=True)
    lease_renew.add_argument("--generation", required=True)
    lease_renew.add_argument("--ttl")
    lease_renew.add_argument("--heartbeat")

    lease_release = sub.add_parser("lease-release")
    lease_release.add_argument("--resource", required=True)
    lease_release.add_argument("--generation", required=True)

    sub.add_parser("status")
    sub.add_parser("doctor")

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = None
    try:
        store = _open_store()
        if args.command != "doctor" and _resolve_mode(store) == "off":
            print("COORD-OFF")
            return EXIT_OK
        handler = COMMANDS[args.command]
        return handler(store, args)
    except CoordExit as exc:
        if exc.message:
            print(exc.message, file=sys.stderr)
        return exc.code
    except OSError as exc:
        print(f"coord: unexpected OS error: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    finally:
        if store is not None:
            _close_store(store)


if __name__ == "__main__":
    sys.exit(main())
