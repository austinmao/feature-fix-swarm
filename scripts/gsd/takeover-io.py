"""Small, stdlib-only fd helpers for takeover artifacts.

The writer resolves the evidence store once through ``gates.py``.  This module
only turns that resolved path into a held, no-follow directory descriptor; it
does not participate in store selection.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from contextlib import contextmanager
from pathlib import Path


class UnsafeTakeoverPath(RuntimeError):
    pass


MAX_BYTES = 1024 * 1024


def _dir_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _trusted(st: os.stat_result) -> bool:
    return stat.S_ISDIR(st.st_mode) and (
        st.st_uid == os.getuid()
        or (st.st_uid == 0 and not (st.st_mode & 0o022))
    )


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
    data = os.read(fd, MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise UnsafeTakeoverPath("oversized file")
    return data


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
    """Atomically replace one regular sibling through the held directory fd."""
    validate_final(directory_fd, name)
    tmp = f".{name}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600, dir_fd=directory_fd)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        validate_final(directory_fd, name)
        os.replace(tmp, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise


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
        if not name.startswith("spec-") or not name.endswith(".json"):
            continue
        try:
            fd = open_regular(ns.takeover_fd, name)
            try:
                data = json.loads(read_regular(fd).decode("utf-8"))
            finally:
                os.close(fd)
            rid = data.get("ids", {}).get("run_id")
            created = data.get("created_at")
            branch = data.get("git_state", {}).get("branch", "")
            command = data.get("resume", {}).get("command", "")
            if not isinstance(rid, str) or not isinstance(created, (int, float)) or not isinstance(branch, str) or not isinstance(command, str):
                continue
            inert = "".join(" " if ord(ch) < 32 or 127 <= ord(ch) <= 159 else ch for ch in command)
            rows.append((rid, str(max(0, int(time.time() - created))), branch.replace("\n", " "), inert))
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError, UnsafeTakeoverPath):
            continue
    for row in sorted(rows):
        print("\t".join(row))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_cli())
    except (OSError, UnsafeTakeoverPath):
        raise SystemExit(1)
