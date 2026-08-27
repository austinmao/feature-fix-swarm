"""Small, stdlib-only fd helpers for takeover artifacts.

The writer resolves the evidence store once through ``gates.py``.  This module
only turns that resolved path into a held, no-follow directory descriptor; it
does not participate in store selection.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class UnsafeTakeoverPath(RuntimeError):
    pass


MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int | None
    boot_session_id: str | None
    pid_start_time: str | None
    process_state: str


def boot_session_id() -> str:
    """Return the current boot identity without treating an error as a reboot."""
    value = _boot_id()
    if value == "unknown":
        raise UnsafeTakeoverPath("unobservable boot identity")
    return value


def process_identity(pid: int | None) -> ProcessIdentity:
    if pid is None:
        return ProcessIdentity(None, None, None, "missing")
    if pid <= 0:
        return ProcessIdentity(None, None, None, "malformed")
    # A definitive ESRCH is safe to classify before asking the platform for
    # richer identity.  EPERM and other outcomes remain unobservable/fatal.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return ProcessIdentity(pid, boot_session_id(), None, "dead")
    except PermissionError:
        raise UnsafeTakeoverPath("unobservable positive process identity")
    seam = os.environ.get("TAKEOVER_TEST_IDENTITY")
    if seam:
        # Test-only hermeticity seam (see _boot_id): a kill(0)-live pid gets a
        # deterministic supported-host identity so fixtures never shell to
        # `ps`.  Unset in production, this branch is inert.
        return ProcessIdentity(pid, seam, seam, "S")
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text()
        tail = stat_text.rsplit(") ", 1)[1].split()
        state, started = tail[0], tail[19]
        return ProcessIdentity(pid, boot_session_id(), started, "zombie" if state == "Z" else state)
    except FileNotFoundError:
        # macOS has no procfs.  `ps` provides a state code and lstart gives a
        # stable per-process incarnation value without parsing locale prose.
        try:
            out = subprocess.run(["ps", "-o", "state=", "-o", "lstart=", "-p", str(pid)],
                                 text=True, capture_output=True, check=False)
        except OSError as exc:
            raise UnsafeTakeoverPath("unobservable positive process identity") from exc
        if out.returncode or not out.stdout.strip():
            return ProcessIdentity(pid, boot_session_id(), None, "dead")
        fields = out.stdout.strip().split(maxsplit=1)
        if len(fields) != 2:
            raise UnsafeTakeoverPath("unobservable positive process identity")
        state, started = fields
        return ProcessIdentity(pid, boot_session_id(), started, "zombie" if state.startswith("Z") else state)
    except (OSError, IndexError):
        raise UnsafeTakeoverPath("unobservable positive process identity")


def process_liveness(identity: ProcessIdentity) -> bool:
    return identity.pid is not None and identity.process_state not in ("dead", "zombie", "missing", "malformed")


def _dir_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _trusted(st: os.stat_result) -> bool:
    # Full safe_path: every component must be a directory owned by the current
    # user or root and carry no group or world write bit, regardless of owner
    # (WALL-RESIDUALS cacd9f1a), with ONE adjudicated carve-out (2026-08-27
    # ship round, recorded in phase-1 WALL-RESIDUALS): a ROOT-owned sticky
    # world-writable directory (POSIX shared tmp — /tmp is 1777) is a trusted
    # PARENT component. Sticky semantics already block the rename/replace
    # attack this walk guards (non-owners cannot unlink or rename another
    # user's entries), and any attacker-owned directory injected below it
    # still fails the ownership check at its own level. Without the carve-out
    # every store under a standard linux tmpdir is refused outright.
    if not stat.S_ISDIR(st.st_mode):
        return False
    if st.st_uid == 0 and (st.st_mode & stat.S_ISVTX) and (st.st_mode & 0o022):
        return True
    return st.st_uid in (os.getuid(), 0) and not (st.st_mode & 0o022)


def _regular_current_user(st: os.stat_result) -> bool:
    return stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and st.st_size <= MAX_BYTES


def same_identity(a: os.stat_result, b: os.stat_result) -> bool:
    return a.st_dev == b.st_dev and a.st_ino == b.st_ino


def open_regular(directory_fd: int, name: str) -> int:
    """Open a bounded regular child without following a hostile name."""
    if not name or "/" in name or name in (".", ".."):
        raise UnsafeTakeoverPath("unsafe child name")
    fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                 getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
    try:
        if not _regular_current_user(os.fstat(fd)):
            raise UnsafeTakeoverPath("unsafe regular file")
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_regular(fd: int) -> bytes:
    st = os.fstat(fd)
    if not _regular_current_user(st):
        raise UnsafeTakeoverPath("unsafe regular file")
    os.lseek(fd, 0, os.SEEK_SET)
    # Full-read loop to the exact fstat size: a short kernel read is retried,
    # early EOF is a hard failure, and growth past the validated size is
    # rejected — short data is never silently accepted (01-VERIFICATION gap).
    chunks: list[bytes] = []
    total = 0
    while total < st.st_size:
        chunk = os.read(fd, min(65536, st.st_size - total))
        if not chunk:
            raise UnsafeTakeoverPath("short read")
        chunks.append(chunk)
        total += len(chunk)
    if os.read(fd, 1):
        raise UnsafeTakeoverPath("oversized file")
    return b"".join(chunks)


def open_store_directory(store_path: str) -> int:
    """Walk an absolute store parent component-by-component with no follow."""
    path = Path(store_path)
    if not path.is_absolute() or path.name != "evidence.json":
        raise UnsafeTakeoverPath("unsafe store path")
    fd = os.open("/", _dir_flags())
    try:
        for part in path.parent.parts[1:]:
            nxt = os.open(part, _dir_flags(), dir_fd=fd)
            os.close(fd)
            fd = nxt
            if not _trusted(os.fstat(fd)):
                raise UnsafeTakeoverPath("untrusted store path component")
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_takeover(directory_fd: int) -> int | None:
    try:
        fd = os.open("takeover", _dir_flags(), dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    if not _trusted(os.fstat(fd)):
        os.close(fd)
        raise UnsafeTakeoverPath("unsafe takeover directory")
    return fd


def entry_matches(directory_fd: int, name: str, fd: int) -> bool:
    try:
        return same_identity(os.stat(name, dir_fd=directory_fd, follow_symlinks=False), os.fstat(fd))
    except (FileNotFoundError, OSError):
        return False


def consume_exact(directory_fd: int, name: str, fd: int) -> str:
    """Rename one revalidated record and verify the resulting entry identity."""
    if not entry_matches(directory_fd, name, fd):
        raise UnsafeTakeoverPath("record-mismatch")
    consumed = f"{name[:-5]}.consumed.{time.time_ns()}.json"
    os.rename(name, consumed, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    try:
        if not entry_matches(directory_fd, consumed, fd):
            raise UnsafeTakeoverPath("record-mismatch")
        os.fsync(directory_fd)
        return consumed
    except BaseException:
        raise


def _boot_id() -> str:
    # Test-only hermeticity seam: managed sandboxes deny `ps`/`sysctl`, so
    # acceptance fixtures export TAKEOVER_TEST_IDENTITY to supply one
    # deterministic boot/start identity.  It is honored only when explicitly
    # set; unset (production) the real platform probes below decide and any
    # unobservable identity still fails closed via boot_session_id().
    seam = os.environ.get("TAKEOVER_TEST_IDENTITY")
    if seam:
        return seam
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if value:
            return value
    except OSError:
        pass
    try:
        value = subprocess.run(["sysctl", "-n", "kern.boottime"], text=True,
                               capture_output=True, check=True).stdout.strip()
        return value or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _process_start(pid: int) -> str:
    try:
        # Field 22, after the comm field which may itself contain spaces.
        tail = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return tail[19]
    except (OSError, IndexError):
        return "unknown"


class LockBusy(RuntimeError):
    pass


class ReclaimElection:
    """Serialized stale-reclaim election on one stable kernel-lock inode.

    The inode is inert: it carries no ownership state of its own and is never
    unlinked.  Ownership is the kernel flock, which is released on close,
    exit, or SIGKILL, so a crashed reclaimer can never strand the election.
    """
    name = ".takeover-check.reclaim.lock"

    def __init__(self, directory_fd: int):
        self.directory_fd = directory_fd
        self.fd: int | None = None

    def __enter__(self):
        fd = os.open(self.name, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                     0o600, dir_fd=self.directory_fd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise UnsafeTakeoverPath("unsafe reclaim election inode")
            os.fchmod(fd, 0o600)
            deadline = time.monotonic() + 1.5
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise LockBusy("runner-live")
                    time.sleep(0.02)
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)  # the kernel releases the flock; the inode stays
            self.fd = None


class OwnerLock:
    """Link-published, descriptor-relative owner lock for the wall lifetime.

    Liveness judgments reuse Plan 01-06's portable identity provider
    (``process_identity``/``process_liveness``): zombie owners are dead,
    same-PID/different-start owners are stale, prior-boot owners are gone,
    and any unprovable identity is treated as live, never stale.
    """
    name = ".takeover-check.lock"

    def __init__(self, directory_fd: int, run_id: str):
        self.directory_fd = directory_fd
        self.run_id = run_id
        self.identity: tuple[int, int] | None = None
        self._pre_move_hook = None  # test seam: runs inside the held election

    def _payload(self) -> bytes:
        try:
            identity = process_identity(os.getpid())
            start = identity.pid_start_time or "unknown"
            boot = identity.boot_session_id or boot_session_id()
        except UnsafeTakeoverPath:
            # An unobservable self-identity still publishes: an unknown start
            # is judged live by every contender, never stale.
            start = "unknown"
            boot = boot_session_id()
        payload = {"pid": os.getpid(), "pid_start_time": start,
                   "boot_session_id": boot, "claimed_at": int(time.time()),
                   "run_id": self.run_id}
        return json.dumps(payload, separators=(",", ":")).encode()

    def _live(self, payload: dict) -> bool:
        try:
            pid = int(payload["pid"])
            start = str(payload["pid_start_time"])
            boot = str(payload["boot_session_id"])
        except (KeyError, TypeError, ValueError):
            return True  # malformed locks receive grace before a later retry
        try:
            if boot != boot_session_id():
                return False  # a prior boot cannot hold a live owner
            identity = process_identity(pid)
        except UnsafeTakeoverPath:
            return True  # unprovable owners are live, never stale
        if not process_liveness(identity):
            return False  # dead and zombie owners are reclaimable
        if start == "unknown" or identity.pid_start_time is None:
            return True  # unknown start identity is never treated as stale
        return identity.pid_start_time == start  # start drift means PID reuse

    def _publish(self) -> bool:
        temp = f".takeover-lock.{os.getpid()}.{time.time_ns()}"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                     0o600, dir_fd=self.directory_fd)
        try:
            try:
                # Same full-write discipline as replace_bytes(): the canonical
                # lock name is linked only from a completely written, fsynced
                # payload, and zero progress is a hard failure.
                view = memoryview(self._payload())
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("short write made no progress")
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
        except BaseException:
            try:
                os.unlink(temp, dir_fd=self.directory_fd)
            except OSError:
                pass
            raise
        try:
            os.link(temp, self.name, src_dir_fd=self.directory_fd, dst_dir_fd=self.directory_fd)
        except FileExistsError:
            os.unlink(temp, dir_fd=self.directory_fd)
            return False
        st = os.stat(self.name, dir_fd=self.directory_fd, follow_symlinks=False)
        self.identity = (st.st_dev, st.st_ino)
        os.unlink(temp, dir_fd=self.directory_fd)
        os.fsync(self.directory_fd)
        return True

    def _reclaim(self, judged: os.stat_result, judged_raw: bytes) -> None:
        """Move one judged-stale inode aside under the serialized election."""
        with ReclaimElection(self.directory_fd):
            if self._pre_move_hook is not None:
                self._pre_move_hook()
            try:
                current = os.stat(self.name, dir_fd=self.directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                return  # already reclaimed; retry publication
            if not same_identity(current, judged):
                return  # a replacement owner appeared; re-judge it next round
            tomb = f".takeover-check.tombstone.{os.getpid()}.{time.time_ns()}"
            os.rename(self.name, tomb, src_dir_fd=self.directory_fd, dst_dir_fd=self.directory_fd)
            tomb_st = os.stat(tomb, dir_fd=self.directory_fd, follow_symlinks=False)
            # (dev, ino) alone is NOT proof on inode-recycling filesystems
            # (linux tmpfs reuses a freed inode number immediately, so a
            # replacement published after the stale lock's unlink can carry
            # the judged identity). The judged PAYLOAD BYTES are the
            # authority: a tomb whose content differs from what was judged
            # stale is a replacement, whatever its inode says.
            try:
                tomb_fd = open_regular(self.directory_fd, tomb)
                try:
                    tomb_raw = read_regular(tomb_fd)
                finally:
                    os.close(tomb_fd)
            except (UnsafeTakeoverPath, OSError):
                tomb_raw = None
            if not same_identity(tomb_st, judged) or tomb_raw != judged_raw:
                # A replacement published inside the move window was moved by
                # mistake: restore it by link and never delete its inode.
                try:
                    os.link(tomb, self.name, src_dir_fd=self.directory_fd,
                            dst_dir_fd=self.directory_fd)
                except FileExistsError:
                    return  # the name was reoccupied; the inode stays linked
                os.unlink(tomb, dir_fd=self.directory_fd)
                os.fsync(self.directory_fd)
                return
            os.unlink(tomb, dir_fd=self.directory_fd)
            os.fsync(self.directory_fd)

    def acquire(self) -> None:
        for _ in range(4):
            if self._publish():
                return
            try:
                judged_fd = open_regular(self.directory_fd, self.name)
            except FileNotFoundError:
                continue  # the owner vanished between attempts; republish
            except (UnsafeTakeoverPath, OSError):
                raise LockBusy("runner-live")
            try:
                try:
                    judged = os.fstat(judged_fd)
                    raw = read_regular(judged_fd)
                finally:
                    os.close(judged_fd)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                    stale = not isinstance(payload, dict) or not self._live(payload)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    stale = time.time() - judged.st_mtime > 1
                if not stale:
                    raise LockBusy("runner-live")
                self._reclaim(judged, raw)
            except LockBusy:
                raise
            except (UnsafeTakeoverPath, OSError):
                raise LockBusy("runner-live")
        raise LockBusy("runner-live")

    def cleanup(self) -> None:
        if self.identity is None:
            return
        try:
            current = os.stat(self.name, dir_fd=self.directory_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != self.identity:
                return
            # Inode identity alone is spoofable by recycling (linux tmpfs
            # hands a freed inode number to the next creation) — only a lock
            # whose PAYLOAD is provably ours is ours to remove. Anything
            # unreadable or foreign is spared.
            try:
                fd = open_regular(self.directory_fd, self.name)
                try:
                    payload = json.loads(read_regular(fd).decode("utf-8"))
                finally:
                    os.close(fd)
            except (UnsafeTakeoverPath, OSError, UnicodeDecodeError, json.JSONDecodeError):
                return
            if (isinstance(payload, dict)
                    and payload.get("pid") == os.getpid()
                    and payload.get("run_id") == self.run_id):
                os.unlink(self.name, dir_fd=self.directory_fd)
        except FileNotFoundError:
            pass


@contextmanager
def takeover_directory(store_path: str):
    """Yield a held fd for ``<resolved-store-parent>/takeover``.

    Every component is opened relative to the prior held descriptor with
    O_NOFOLLOW.  The final child is created and validated relative to its
    parent, so no later writer operation needs to follow a pathname.
    """
    parent = Path(store_path).resolve(strict=False).parent
    if not parent.is_absolute():
        raise UnsafeTakeoverPath("store parent is not absolute")
    fd = os.open("/", _dir_flags())
    try:
        for part in parent.parts[1:]:
            try:
                next_fd = os.open(part, _dir_flags(), dir_fd=fd)
            except FileNotFoundError:
                # This is the writer's deliberately narrow first-run write:
                # create only missing components below the already canonical
                # absolute store path, then immediately reopen no-follow.
                os.mkdir(part, 0o700, dir_fd=fd)
                next_fd = os.open(part, _dir_flags(), dir_fd=fd)
            os.close(fd)
            fd = next_fd
            if not _trusted(os.fstat(fd)):
                raise UnsafeTakeoverPath("untrusted store path component")
        try:
            child = os.open("takeover", _dir_flags(), dir_fd=fd)
        except FileNotFoundError:
            os.mkdir("takeover", 0o700, dir_fd=fd)
            child = os.open("takeover", _dir_flags(), dir_fd=fd)
        if not _trusted(os.fstat(child)):
            os.close(child)
            raise UnsafeTakeoverPath("unsafe takeover directory")
        try:
            yield child
        finally:
            os.close(child)
    finally:
        os.close(fd)


def validate_final(directory_fd: int, name: str) -> None:
    try:
        row = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(row.st_mode):
        raise UnsafeTakeoverPath(f"unsafe output path: {name}")


def replace_bytes(directory_fd: int, name: str, payload: bytes) -> None:
    """Atomically replace one regular sibling through the held directory fd.

    The stage is unique per attempt (strong randomness plus O_EXCL), written by
    a full-write loop, and removed by the single outer ``finally`` after any
    write, fsync, validation, or replace failure.  Per WALL-RESIDUALS b80300b4
    a directory-fsync error AFTER the atomic rename is a typed FSYNC-FAIL
    nonzero failure while the completed rename stands; the no-mutation
    contract covers pre-rename failures only.
    """
    validate_final(directory_fd, name)
    stage = f".{name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(stage, flags, 0o600, dir_fd=directory_fd)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write made no progress")
            view = view[written:]
        os.fchmod(fd, 0o600)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        validate_final(directory_fd, name)
        os.replace(stage, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            raise OSError(f"FSYNC-FAIL: directory fsync after publishing {name}: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(stage, dir_fd=directory_fd)
        except OSError:
            pass


def _inert(text: str) -> str:
    """Map C0/C1 control bytes to spaces so displayed fields stay inert."""
    return "".join(" " if ord(ch) < 32 or 127 <= ord(ch) <= 159 else ch for ch in text)


def _cli() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("command", choices=("consume", "list"))
    ap.add_argument("--takeover-fd", type=int, required=True)
    ap.add_argument("--record-fd", type=int)
    ap.add_argument("--name")
    ns = ap.parse_args()
    if ns.command == "consume":
        if ns.record_fd is None or ns.name is None:
            return 2
        print(consume_exact(ns.takeover_fd, ns.name, ns.record_fd))
        return 0
    rows: list[tuple[str, str, str, str]] = []
    for name in os.listdir(ns.takeover_fd):
        if not re.fullmatch(r"spec-[0-9]{3}\.json", name):
            continue
        try:
            fd = open_regular(ns.takeover_fd, name)
            try:
                data = json.loads(read_regular(fd).decode("utf-8"))
            finally:
                os.close(fd)
            if not isinstance(data, dict):
                continue
            ids = data.get("ids")
            git_state = data.get("git_state")
            resume = data.get("resume")
            if not isinstance(ids, dict) or not isinstance(git_state, dict) or not isinstance(resume, dict):
                continue
            rid = ids.get("run_id")
            created = data.get("created_at")
            branch = git_state.get("branch", "")
            command = resume.get("command", "")
            if not isinstance(rid, str) or not isinstance(branch, str) or not isinstance(command, str):
                continue
            # WR-02: the active filename is the displayed identity; a record
            # whose embedded run_id disagrees is deceptive and is skipped.
            if rid != name.removesuffix(".json"):
                continue
            # CR-04: booleans and non-finite timestamps must never reach the
            # age arithmetic — one hostile record killed the whole listing.
            if (isinstance(created, bool) or not isinstance(created, (int, float))
                    or not math.isfinite(created)):
                continue
            try:
                age = str(max(0, int(time.time() - created)))
            except OverflowError:
                continue
            rows.append((_inert(rid), age, _inert(branch), _inert(command)))
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError,
                UnsafeTakeoverPath, OverflowError):
            continue
    for row in sorted(rows):
        print("\t".join(row))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_cli())
    except (OSError, UnsafeTakeoverPath):
        raise SystemExit(1)
