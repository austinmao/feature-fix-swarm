"""Small, stdlib-only fd helpers for takeover artifact writes.

The writer resolves the evidence store once through ``gates.py``.  This module
only turns that resolved path into a held, no-follow directory descriptor; it
does not participate in store selection.
"""
from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path


class UnsafeTakeoverPath(RuntimeError):
    pass


def _dir_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _trusted(st: os.stat_result) -> bool:
    return stat.S_ISDIR(st.st_mode) and (
        st.st_uid == os.getuid()
        or (st.st_uid == 0 and not (st.st_mode & 0o022))
    )


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
