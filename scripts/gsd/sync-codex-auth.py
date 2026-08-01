#!/usr/bin/env python3
"""Compare-and-swap a refreshed Codex OAuth file under one global flock."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_private_regular(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular non-symlink file: {path}")
    metadata = os.stat(path, follow_symlinks=False)
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError(f"{label} must be owned by the current user with mode 0600: {path}")


def open_lock(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    metadata = os.fstat(fd)
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        os.close(fd)
        raise ValueError(f"auth lock must be owned by the current user with mode 0600: {path}")
    return fd


def synchronize(
    source: Path,
    refreshed: Path,
    initial_hash: str,
    lock_path: Path,
    *,
    attempts: int = 100,
) -> str:
    require_private_regular(source, "canonical Codex auth")
    require_private_regular(refreshed, "refreshed Codex auth")
    if digest(refreshed) == initial_hash:
        return "unchanged"

    lock_fd = open_lock(lock_path)
    try:
        for attempt in range(attempts):
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if attempt + 1 == attempts:
                    raise TimeoutError(f"auth lock remained busy at {lock_path}")
                time.sleep(0.05)

        require_private_regular(source, "canonical Codex auth")
        if digest(source) != initial_hash:
            return "concurrent-refresh-preserved"

        copy_fd, temporary = tempfile.mkstemp(prefix=".auth.json.", dir=source.parent)
        try:
            with os.fdopen(copy_fd, "wb") as destination, refreshed.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, source)
            directory_fd = os.open(source.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return "synchronized"
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("refreshed", type=Path)
    parser.add_argument("initial_hash")
    parser.add_argument("lock", type=Path)
    parser.add_argument("--attempts", type=int, default=100)
    args = parser.parse_args()
    if args.attempts < 1:
        parser.error("--attempts must be positive")
    try:
        status = synchronize(
            args.source,
            args.refreshed,
            args.initial_hash,
            args.lock,
            attempts=args.attempts,
        )
    except TimeoutError as exc:
        print(f"sync-codex-auth: {exc}", file=__import__("sys").stderr)
        return 75
    except (OSError, ValueError) as exc:
        print(f"sync-codex-auth: {exc}", file=__import__("sys").stderr)
        return 1
    print(json.dumps({"schema": "ffs.codex-auth-sync/v1", "status": status}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
