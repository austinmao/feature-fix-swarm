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

    Phase 1 never calls SoftFileLease or ReadWriteLock (P-04: no lease
    markers) — they are the VERSION-FLOOR PROBE only, since SoftFileLease
    landed in 3.30 and ReadWriteLock in 3.21. Their absence is how a
    below-floor 3.29.x install is detected without parsing a version string.
    Phase 2 will use ReadWriteLock for real; do not remove this probe.
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
    _refuse_if_symlink(store.by_run_fd, run_id)
    try:
        raw = _read_text_fd(store.by_run_fd, run_id)
    except FileNotFoundError:
        return None
    content = raw[:-1] if raw.endswith("\n") else raw
    if not SESSION_UUID_RE.match(content):
        raise CoordExit(
            EXIT_UNAVAILABLE,
            f"coord: COORD-UNAVAILABLE malformed by-run pointer sessions/by-run/{run_id}",
        )
    return content


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
    with os.fdopen(fd, "w") as f:
        f.write(session_uuid + "\n")
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
        return _read_json_fd(store.coord_fd, "registry.json")
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
def cmd_claim(store: StoreFds, args) -> int:
    """Task 1: happy path only — grant an unheld spec at generation 1.
    Contention (idempotent re-claim, foreign-live refusal, staleness-based
    reclaim) is added in Task 2 / Task 3."""
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
        new_gen = gen_entry["gen"] + 1
        gen_entry["gen"] = new_gen
        registry["claims"][key] = {
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
        _save_registry(store, registry)
        print(f"CLAIM-OK generation={new_gen}")
        return EXIT_OK


def cmd_status(store: StoreFds, args) -> int:
    with registry_transaction(store) as registry:
        claims = dict(registry.get("claims", {}))
    for key, entry in sorted(claims.items()):
        line = (
            f"{key} holder={entry.get('holder_uuid')} "
            f"generation={entry.get('generation')} "
            f"anchor_pid={entry.get('holder_anchor_pid')} "
            f"cli_pid={entry.get('cli_pid')} "
            f"worktree={entry.get('holder_worktree')} "
            f"last_renewed_at={entry.get('last_renewed_at')} "
            f"ttl_secs={entry.get('ttl_secs')} "
            f"expires_at={entry.get('expires_at')}"
        )
        print(line)
    return EXIT_OK


def _stub(name: str):
    def _cmd(store: StoreFds, args) -> int:
        print(f"coord: {name} not implemented in task 1", file=sys.stderr)
        return EXIT_USAGE

    return _cmd


cmd_claim_check = _stub("claim-check")
cmd_claim_renew = _stub("claim-renew")
cmd_release = _stub("release")
cmd_doctor = _stub("doctor")


COMMANDS = {
    "claim": cmd_claim,
    "claim-check": cmd_claim_check,
    "claim-renew": cmd_claim_renew,
    "release": cmd_release,
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

    sub.add_parser("status")
    sub.add_parser("doctor")

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = None
    try:
        store = _open_store()
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
