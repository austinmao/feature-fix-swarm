#!/usr/bin/env python3
"""Scope-aware FFS installer, migration, rollback, and doctor.

GSD source artifacts deliberately remain upstream-owned. This installer only
materializes FFS-owned skills and records enough state to undo every mutation.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Iterator


INSTALL_SCHEMA = "ffs.install/v1"
DOCTOR_SCHEMA = "ffs.doctor/v1"
BACKUP_SCHEMA = "ffs.backup/v1"
GSD_VERSION = "1.9.1"
CODEX_MIN_VERSION = (0, 137, 0)
CODEX_MAX_VERSION = (0, 147, 0)


class InvocationError(Exception):
    pass


class ActionableError(Exception):
    pass


def lexists(path: Path) -> bool:
    return os.path.lexists(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(path: Path) -> str:
    if path.is_symlink():
        return "symlink:" + os.readlink(path)
    if not path.exists():
        return "missing"
    if path.is_file():
        return "file:" + sha256_file(path)
    if path.is_dir():
        digest = hashlib.sha256()
        for child in sorted(path.rglob("*"), key=lambda item: item.as_posix()):
            rel = child.relative_to(path).as_posix().encode()
            if child.is_symlink():
                value = b"L\0" + rel + b"\0" + os.readlink(child).encode()
            elif child.is_file():
                value = b"F\0" + rel + b"\0" + sha256_file(child).encode()
            elif child.is_dir():
                value = b"D\0" + rel
            else:
                value = b"O\0" + rel
            digest.update(value + b"\n")
        return "dir:" + digest.hexdigest()
    return "other"


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def copy_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination)


def copy_entry_between_fds(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> None:
    """Recursively copy one entry without resolving either parent by path."""
    source_stat = os.stat(
        source_name, dir_fd=source_parent_fd, follow_symlinks=False
    )
    mode = source_stat.st_mode
    if stat.S_ISLNK(mode):
        os.symlink(
            os.readlink(source_name, dir_fd=source_parent_fd),
            destination_name,
            dir_fd=destination_parent_fd,
        )
        return
    if stat.S_ISREG(mode):
        try:
            source_fd = os.open(
                source_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=source_parent_fd,
            )
        except OSError as exc:
            raise ActionableError(
                f"source changed during backup: {source_name}: {exc}"
            ) from exc
        opened_source = os.fstat(source_fd)
        if (opened_source.st_dev, opened_source.st_ino) != (
            source_stat.st_dev,
            source_stat.st_ino,
        ):
            os.close(source_fd)
            raise ActionableError(f"source changed during backup: {source_name}")
        try:
            destination_fd = os.open(
                destination_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                stat.S_IMODE(mode),
                dir_fd=destination_parent_fd,
            )
        except BaseException:
            os.close(source_fd)
            raise
        try:
            while block := os.read(source_fd, 1024 * 1024):
                view = memoryview(block)
                while view:
                    written = os.write(destination_fd, view)
                    if written == 0:
                        raise OSError("zero-byte write while copying backup")
                    view = view[written:]
            os.fchmod(destination_fd, stat.S_IMODE(mode))
        finally:
            os.close(destination_fd)
            os.close(source_fd)
        return
    if not stat.S_ISDIR(mode):
        raise ActionableError(f"cannot back up special file: {source_name}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        source_fd = os.open(source_name, flags, dir_fd=source_parent_fd)
    except OSError as exc:
        raise ActionableError(
            f"source changed during backup: {source_name}: {exc}"
        ) from exc
    opened_source = os.fstat(source_fd)
    if (opened_source.st_dev, opened_source.st_ino) != (
        source_stat.st_dev,
        source_stat.st_ino,
    ):
        os.close(source_fd)
        raise ActionableError(f"source changed during backup: {source_name}")
    try:
        os.mkdir(destination_name, stat.S_IMODE(mode), dir_fd=destination_parent_fd)
        destination_fd = os.open(
            destination_name, flags, dir_fd=destination_parent_fd
        )
    except BaseException:
        os.close(source_fd)
        raise
    try:
        for child in sorted(os.listdir(source_fd)):
            copy_entry_between_fds(source_fd, child, destination_fd, child)
        os.fchmod(destination_fd, stat.S_IMODE(mode))
    finally:
        os.close(destination_fd)
        os.close(source_fd)


def secure_backup_object(path: Path) -> None:
    """Make copied backup payloads private without following stored symlinks."""
    if path.is_symlink():
        return
    if path.is_file():
        path.chmod(0o600)
        return
    if not path.is_dir():
        return
    path.chmod(0o700)
    for directory, names, files in os.walk(path, followlinks=False):
        current = Path(directory)
        if not current.is_symlink():
            current.chmod(0o700)
        for name in names:
            child = current / name
            if not child.is_symlink():
                child.chmod(0o700)
        for name in files:
            child = current / name
            if not child.is_symlink() and child.is_file():
                child.chmod(0o600)


def capture_modes(path: Path) -> dict[str, str]:
    """Record regular file/directory modes without dereferencing symlinks."""
    modes: dict[str, str] = {}
    if path.is_symlink():
        return modes
    if path.is_file() or path.is_dir():
        modes[""] = f"{stat.S_IMODE(path.stat().st_mode):04o}"
    if path.is_dir():
        for child in path.rglob("*"):
            if child.is_symlink():
                continue
            if child.is_file() or child.is_dir():
                modes[child.relative_to(path).as_posix()] = (
                    f"{stat.S_IMODE(child.stat().st_mode):04o}"
                )
    return modes


def restore_modes(path: Path, modes: dict[str, str] | None, root_mode: str | None) -> None:
    """Restore recorded modes after copying a private backup payload."""
    recorded = dict(modes or {})
    if not recorded and root_mode:
        recorded[""] = root_mode
    # Children first leaves the root traversable until restoration is complete.
    for relative, rendered in sorted(
        recorded.items(), key=lambda item: len(Path(item[0]).parts), reverse=True
    ):
        destination = path if not relative else path / relative
        if destination.is_symlink() or not lexists(destination):
            continue
        try:
            destination.chmod(int(rendered, 8))
        except (OSError, ValueError) as exc:
            raise ActionableError(f"could not restore mode for {destination}: {exc}") from exc


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ActionableError(f"invalid managed manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ActionableError(f"invalid managed manifest {path}: expected object")
    return value


def atomic_json(path: Path, value: dict[str, Any], mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json_file(path: Path, value: dict[str, Any], mode: int = 0o644) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(path, flags, mode)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        os.fchmod(handle.fileno(), mode)


@contextlib.contextmanager
def private_cache_root_fd() -> Iterator[tuple[int, Path]]:
    """Create/open the private cache root without following mutable links."""
    home = Path.home().absolute()
    cache_link = home / ".cache"
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        home_fd = os.open(home, flags)
    except OSError as exc:
        raise ActionableError(f"private home root is unsafe: {home}: {exc}") from exc
    cache_fd: int | None = None
    root_fd: int | None = None
    try:
        if cache_link.is_symlink():
            try:
                cache_path = cache_link.resolve(strict=True)
            except OSError as exc:
                raise ActionableError(
                    f"private cache parent is unsafe: {cache_link}: {exc}"
                ) from exc
            cache_fd = os.open(cache_path, flags)
        else:
            try:
                os.mkdir(".cache", 0o700, dir_fd=home_fd)
            except FileExistsError:
                pass
            cache_fd = os.open(".cache", flags, dir_fd=home_fd)
            cache_path = cache_link
        root = cache_path / "feature-fix-swarm"
        try:
            os.mkdir("feature-fix-swarm", 0o700, dir_fd=cache_fd)
        except FileExistsError:
            pass
        root_fd = os.open("feature-fix-swarm", flags, dir_fd=cache_fd)
        os.fchmod(root_fd, 0o700)
        yield root_fd, root
    except OSError as exc:
        raise ActionableError(
            f"private cache root is unsafe: {cache_link / 'feature-fix-swarm'}: {exc}"
        ) from exc
    finally:
        if root_fd is not None:
            os.close(root_fd)
        if cache_fd is not None:
            os.close(cache_fd)
        os.close(home_fd)


def cache_root() -> Path:
    with private_cache_root_fd() as (_root_fd, root):
        return root


def open_backup_directory(backup_id: str) -> tuple[int, Path]:
    """Open one backup directory through the private cache descriptor chain."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    with private_cache_root_fd() as (cache_fd, cache_path):
        try:
            backups_fd = os.open("backups", flags, dir_fd=cache_fd)
        except OSError as exc:
            raise ActionableError("backup store is missing or unsafe") from exc
        try:
            try:
                directory_fd = os.open(backup_id, flags, dir_fd=backups_fd)
            except OSError as exc:
                raise ActionableError(
                    f"backup not found or unsafe: {backup_id}"
                ) from exc
        finally:
            os.close(backups_fd)
    return directory_fd, cache_path / "backups" / backup_id


def project_lock_path(project: Path) -> Path:
    process = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "--git-common-dir"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode != 0:
        raise ActionableError(f"project scope requires a Git repository: {project}")
    common = Path(process.stdout.strip())
    if not common.is_absolute():
        common = project / common
    return common.resolve() / "feature-fix-swarm.setup.lock"


@contextlib.contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def anchored_working_directory(directory_fd: int) -> Iterator[None]:
    """Resolve relative filesystem operations from a stable open directory."""
    saved_fd = os.open(".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fchdir(directory_fd)
        yield
    finally:
        os.fchdir(saved_fd)
        os.close(saved_fd)


@contextlib.contextmanager
def project_parent_fd(
    project: Path, destination: Path
) -> Iterator[tuple[int, str]]:
    """Open/create a project destination parent without following symlinks."""
    root = project.resolve()
    try:
        relative = destination.absolute().relative_to(root)
    except ValueError as exc:
        raise ActionableError(
            f"project managed path escapes checkout: {destination}"
        ) from exc
    if not relative.parts or ".." in relative.parts:
        raise ActionableError(f"unsafe project destination: {destination}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    root_fd = os.open(root, flags)
    directory_fd = root_fd
    try:
        for part in relative.parts[:-1]:
            try:
                os.mkdir(part, 0o755, dir_fd=directory_fd)
            except FileExistsError:
                pass
            next_fd = os.open(part, flags, dir_fd=directory_fd)
            if directory_fd != root_fd:
                os.close(directory_fd)
            directory_fd = next_fd
        yield directory_fd, relative.name
    except OSError as exc:
        raise ActionableError(
            f"unsafe project destination {destination}: {exc}"
        ) from exc
    finally:
        if directory_fd != root_fd:
            os.close(directory_fd)
        os.close(root_fd)


@contextlib.contextmanager
def relative_parent_fd(root_fd: int, relative: Path) -> Iterator[tuple[int, str]]:
    """Open/create a relative parent beneath an already verified root fd."""
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ActionableError(f"unsafe anchored destination: {relative}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    directory_fd = os.dup(root_fd)
    try:
        for part in relative.parts[:-1]:
            try:
                os.mkdir(part, 0o755, dir_fd=directory_fd)
            except FileExistsError:
                pass
            next_fd = os.open(part, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        yield directory_fd, relative.name
    except OSError as exc:
        raise ActionableError(
            f"unsafe anchored destination {relative}: {exc}"
        ) from exc
    finally:
        os.close(directory_fd)


def rename_no_replace(
    source: str,
    destination: str,
    *,
    source_fd: int,
    destination_fd: int,
) -> None:
    """Atomically rename one entry without replacing an existing destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    result: int
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        renameatx_np = libc.renameatx_np
        renameatx_np.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameatx_np.restype = ctypes.c_int
        result = renameatx_np(
            source_fd,
            source_bytes,
            destination_fd,
            destination_bytes,
            0x00000004,  # RENAME_EXCL
        )
    elif hasattr(libc, "renameat2"):
        renameat2 = libc.renameat2
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            source_fd,
            source_bytes,
            destination_fd,
            destination_bytes,
            0x00000001,  # RENAME_NOREPLACE
        )
    else:
        raise ActionableError(
            "atomic no-replace rename is unsupported on this platform"
        )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), destination)
    raise OSError(error, os.strerror(error), destination)


class Backup:
    def __init__(self, operation: str, scope: str, project: Path | None = None) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.backup_id = f"{stamp}-{os.getpid()}-{secrets.token_hex(3)}"
        with private_cache_root_fd() as (cache_fd, cache_path):
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                os.mkdir("backups", 0o700, dir_fd=cache_fd)
            except FileExistsError:
                pass
            try:
                backups_fd = os.open("backups", flags, dir_fd=cache_fd)
            except OSError as exc:
                raise ActionableError(
                    f"private backup root is unsafe: {cache_path / 'backups'}: {exc}"
                ) from exc
            try:
                os.fchmod(backups_fd, 0o700)
                os.mkdir(self.backup_id, 0o700, dir_fd=backups_fd)
                directory_fd = os.open(self.backup_id, flags, dir_fd=backups_fd)
                try:
                    os.fchmod(directory_fd, 0o700)
                except BaseException:
                    os.close(directory_fd)
                    raise
            finally:
                os.close(backups_fd)
        self.directory = cache_path / "backups" / self.backup_id
        self.directory_fd = directory_fd
        self.entries: list[dict[str, Any]] = []
        self.seen: set[str] = set()
        self.operation = operation
        self.scope = scope
        self.project = project

    def _record_before(
        self,
        path: Path,
        source: Path | None,
        *,
        source_parent_fd: int | None = None,
        source_name: str | None = None,
    ) -> dict[str, Any] | None:
        absolute = str(path.absolute())
        if absolute in self.seen:
            return None
        self.seen.add(absolute)
        entry: dict[str, Any] = {"path": absolute, "before": "missing"}

        def inspect_source(source_path: Path) -> bool:
            if not lexists(source_path):
                return False
            entry["before"] = fingerprint(source_path)
            metadata = source_path.lstat()
            entry["mode"] = f"{stat.S_IMODE(metadata.st_mode):04o}"
            if stat.S_ISLNK(metadata.st_mode):
                entry["type"] = "symlink"
                entry["target"] = os.readlink(source_path)
            elif stat.S_ISDIR(metadata.st_mode):
                entry["type"] = "directory"
            elif stat.S_ISREG(metadata.st_mode):
                entry["type"] = "file"
                entry["sha256"] = sha256_file(source_path)
            else:
                entry["type"] = "other"
            entry["modes"] = capture_modes(source_path)
            return True

        anchored_source = source_parent_fd is not None and source_name is not None
        if anchored_source:
            assert source_parent_fd is not None and source_name is not None
            with anchored_working_directory(source_parent_fd):
                source_exists = inspect_source(Path(source_name))
        else:
            source_exists = source is not None and inspect_source(source)

        if source_exists:
            backup_rel = f"objects/{len(self.entries):04d}"
            if anchored_source:
                assert source_parent_fd is not None and source_name is not None
                try:
                    os.mkdir("objects", 0o700, dir_fd=self.directory_fd)
                except FileExistsError:
                    pass
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                objects_fd = os.open("objects", flags, dir_fd=self.directory_fd)
                try:
                    copy_entry_between_fds(
                        source_parent_fd,
                        source_name,
                        objects_fd,
                        f"{len(self.entries):04d}",
                    )
                finally:
                    os.close(objects_fd)
            else:
                assert source is not None
                with anchored_working_directory(self.directory_fd):
                    copy_path(source, Path(backup_rel))
            with anchored_working_directory(self.directory_fd):
                backup_path = Path(backup_rel)
                if fingerprint(backup_path) != entry["before"]:
                    remove_path(backup_path)
                    raise ActionableError(
                        f"source changed while creating backup: {path}"
                    )
                backup_path.parent.chmod(0o700)
                secure_backup_object(backup_path)
            entry["backup"] = backup_rel
        self.entries.append(entry)
        return entry

    def before(self, path: Path) -> dict[str, Any] | None:
        if not lexists(path):
            return self._record_before(path, None)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            canonical_parent = path.absolute().parent.resolve(strict=True)
            parent_fd = os.open(canonical_parent, flags)
        except (OSError, RuntimeError) as exc:
            raise ActionableError(
                f"backup source parent is unsafe: {path.parent}: {exc}"
            ) from exc
        try:
            return self._record_before(
                path,
                None,
                source_parent_fd=parent_fd,
                source_name=path.name,
            )
        finally:
            os.close(parent_fd)

    def before_anchored(
        self, path: Path, parent_fd: int, source_name: str | None
    ) -> dict[str, Any] | None:
        """Snapshot a project entry relative to an already verified parent fd."""
        return self._record_before(
            path,
            None,
            source_parent_fd=parent_fd if source_name is not None else None,
            source_name=source_name,
        )

    def assume_created(self, path: Path) -> None:
        """Record an upstream-manifest path proven absent from the old manifest."""
        absolute = str(path.absolute())
        if absolute in self.seen:
            return
        self.seen.add(absolute)
        self.entries.append({"path": absolute, "before": "missing"})

    def finish(self) -> None:
        for entry in self.entries:
            entry.setdefault("after", fingerprint(Path(entry["path"])))
        with anchored_working_directory(self.directory_fd):
            atomic_json(
                Path("manifest.json"),
                {
                    "schema": BACKUP_SCHEMA,
                    "backup_id": self.backup_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "operation": self.operation,
                    "scope": self.scope,
                    "project_dir": str(self.project) if self.project else None,
                    "entries": self.entries,
                },
                mode=0o600,
            )

    def restore_uncommitted(self) -> None:
        """Best-effort transaction rollback before control returns to caller."""
        conflicts: list[Path] = []
        for entry in reversed(self.entries):
            destination = Path(entry["path"])
            backup_rel = entry.get("backup")
            if self.project is not None:
                try:
                    destination.absolute().relative_to(self.project.resolve())
                except ValueError:
                    pass
                else:
                    try:
                        restore_project_entry(
                            self.project,
                            destination,
                            None,
                            expected_current=entry.get("after"),
                            modes=entry.get("modes"),
                            root_mode=entry.get("mode"),
                            source_parent_fd=(
                                self.directory_fd if backup_rel else None
                            ),
                            source_name=backup_rel,
                        )
                    except ActionableError:
                        conflicts.append(destination)
                    continue
            if entry.get("restore_root") and entry.get("restore_relative"):
                try:
                    restore_anchored_backup_entry(
                        entry,
                        destination,
                        None,
                        source_parent_fd=(self.directory_fd if backup_rel else None),
                        source_name=backup_rel,
                    )
                except ActionableError:
                    conflicts.append(destination)
                continue
            if lexists(destination):
                remove_path(destination)
            if backup_rel:
                with anchored_working_directory(self.directory_fd):
                    copy_path(Path(backup_rel), destination)
                    restore_modes(
                        destination, entry.get("modes"), entry.get("mode")
                    )
        if conflicts:
            rendered = ", ".join(str(path) for path in conflicts)
            raise ActionableError(
                f"managed path changed during rollback; preserved: {rendered}"
            )


def source_version(source: Path) -> str:
    override = os.environ.get("FFS_VERSION")
    if override:
        return override
    changelog = source / "CHANGELOG.md"
    if changelog.exists():
        match = re.search(r"^## v([^\s]+)", changelog.read_text(), re.MULTILINE)
        if match:
            return match.group(1)
    return "0.0.0-dev"


def manifest_source(source: Path, scope: str, project: Path | None) -> str:
    rendered = str(source)
    if scope == "project" and project is not None:
        try:
            rendered = source.resolve().relative_to(project.resolve()).as_posix()
        except ValueError:
            pass
    return rendered


def source_skills(source: Path) -> dict[str, Path]:
    skills = {
        item.name: item
        for item in (source / "skills").iterdir()
        if item.is_dir() and (item / "SKILL.md").is_file()
    }
    if not skills:
        raise ActionableError(f"no FFS skills found under {source / 'skills'}")
    return dict(sorted(skills.items()))


def safe_project_destination(project: Path, key: str | Path) -> Path:
    relative = Path(key)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ActionableError(f"unsafe project manifest path: {key}")
    root = project.resolve()
    destination = root / relative
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise ActionableError(
                f"project install path has a symlinked ancestor: {current}"
            )
    return destination


def safe_project_directory(project: Path, key: str | Path) -> Path:
    destination = safe_project_destination(project, key)
    if destination.is_symlink():
        raise ActionableError(f"symlinked project directory is unsafe: {destination}")
    if lexists(destination) and not destination.is_dir():
        raise ActionableError(f"project directory is not a directory: {destination}")
    return destination


def safe_project_file(project: Path, key: str | Path) -> Path:
    destination = safe_project_destination(project, key)
    if destination.is_symlink():
        raise ActionableError(f"symlinked project file is unsafe: {destination}")
    if lexists(destination) and not destination.is_file():
        raise ActionableError(f"project file is not a regular file: {destination}")
    return destination


def atomic_copy_inside(
    root: Path, relative: str | Path, source: Path, *, executable: bool = True
) -> None:
    """Atomically replace a regular file beneath root without following links.

    ``executable`` defaults to True (existing scripts/gsd/*.sh|.py drift-surface
    behaviour, pinned by setup-install.bats). Pass False for data files (e.g.
    schemas/*.json) so the copy keeps the source's own bits instead of always
    gaining +x (spec-004 INT-003: "correct bits").
    """
    key = Path(relative)
    if key.is_absolute() or not key.parts or ".." in key.parts:
        raise ActionableError(f"unsafe consumer path: {relative}")
    if source.is_symlink() or not source.is_file():
        raise ActionableError(f"consumer source is not a regular file: {source}")

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(root, directory_flags)
    directory_fd = root_fd
    temporary = f".{key.name}.ffs-{os.getpid()}-{secrets.token_hex(4)}"
    try:
        for part in key.parts[:-1]:
            try:
                os.mkdir(part, 0o755, dir_fd=directory_fd)
            except FileExistsError:
                pass
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            if directory_fd != root_fd:
                os.close(directory_fd)
            directory_fd = next_fd

        try:
            existing = os.stat(key.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise ActionableError(f"consumer destination is not a regular file: {root / key}")

        source_mode = stat.S_IMODE(source.stat().st_mode)
        if executable:
            source_mode |= 0o111
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        output_fd = os.open(temporary, flags, source_mode, dir_fd=directory_fd)
        try:
            with source.open("rb") as input_handle, os.fdopen(output_fd, "wb") as output_handle:
                shutil.copyfileobj(input_handle, output_handle)
                output_handle.flush()
                os.fsync(output_handle.fileno())
                os.fchmod(output_handle.fileno(), source_mode)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            raise
        os.rename(
            temporary,
            key.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
    except OSError as exc:
        raise ActionableError(f"unsafe consumer destination {root / key}: {exc}") from exc
    finally:
        if directory_fd != root_fd:
            os.close(directory_fd)
        os.close(root_fd)


def manifest_path(scope: str, project: Path | None) -> Path:
    if scope == "project":
        assert project is not None
        return safe_project_file(
            project, Path(".feature-fix-swarm") / "install-manifest.json"
        )
    return cache_root() / "install-manifest.json"


def manifest_key(path: Path, scope: str, project: Path | None) -> str:
    if scope == "project":
        assert project is not None
        try:
            relative = path.absolute().relative_to(project.resolve())
        except ValueError as exc:
            raise ActionableError(f"project managed path escapes checkout: {path}") from exc
        return safe_project_destination(project, relative).relative_to(project.resolve()).as_posix()
    return str(path.absolute())


def manifest_destination(key: str, scope: str, project: Path | None) -> Path:
    if scope == "project":
        assert project is not None
        return safe_project_destination(project, key)
    return Path(key)


def managed_fingerprints(manifest: dict[str, Any] | None, scope: str, project: Path | None) -> dict[str, str]:
    if not manifest:
        return {}
    result: dict[str, str] = {}
    for key, metadata in manifest.get("paths", {}).items():
        if isinstance(metadata, dict) and isinstance(metadata.get("fingerprint"), str):
            result[str(manifest_destination(key, scope, project).absolute())] = metadata["fingerprint"]
    return result


def ensure_replaceable(path: Path, expected: str, managed: dict[str, str], *, broken_link_ok: bool = False) -> None:
    if not lexists(path):
        return
    current = fingerprint(path)
    if current == expected or managed.get(str(path.absolute())) == current:
        return
    if broken_link_ok and path.is_symlink() and not path.exists():
        return
    raise ActionableError(
        f"preserved edited/unmanaged collision at {path}; move it aside or restore the installed bytes, then retry"
    )


def replace_anchored_entry(
    destination: Path,
    backup: Backup | None,
    expected_before: str | None,
    materialize: Any | None,
    parent_fd: int,
    name: str,
    *,
    scope_label: str,
) -> None:
    """Replace one entry through an already verified no-follow parent fd."""
    temporary = f".{destination.name}.ffs-new-{os.getpid()}-{secrets.token_hex(4)}"
    quarantine = f".{destination.name}.ffs-old-{os.getpid()}-{secrets.token_hex(4)}"
    moved_old = False
    installed_new = False
    backup_entry: dict[str, Any] | None = None
    try:
        try:
            os.rename(
                name,
                quarantine,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            moved_old = True
        except FileNotFoundError:
            pass
        with anchored_working_directory(parent_fd):
            current = fingerprint(Path(quarantine)) if moved_old else "missing"
        if expected_before is not None and current != expected_before:
            if moved_old:
                rename_no_replace(
                    quarantine,
                    name,
                    source_fd=parent_fd,
                    destination_fd=parent_fd,
                )
                moved_old = False
            raise ActionableError(
                f"{scope_label} destination changed during install: {destination}"
            )
        if backup is not None:
            backup_entry = backup.before_anchored(
                destination, parent_fd, quarantine if moved_old else None
            )
        if materialize is not None:
            with anchored_working_directory(parent_fd):
                materialize(Path(temporary), parent_fd)
                if lexists(Path(name)):
                    raise ActionableError(
                        f"{scope_label} destination changed during install: {destination}"
                    )
            rename_no_replace(
                temporary,
                name,
                source_fd=parent_fd,
                destination_fd=parent_fd,
            )
            installed_new = True
        if backup_entry is not None:
            with anchored_working_directory(parent_fd):
                backup_entry["after"] = fingerprint(Path(name))
        if moved_old:
            with anchored_working_directory(parent_fd):
                remove_path(Path(quarantine))
            moved_old = False
    except BaseException:
        with anchored_working_directory(parent_fd):
            if lexists(Path(temporary)):
                remove_path(Path(temporary))
            if installed_new and lexists(Path(name)):
                remove_path(Path(name))
        if moved_old:
            try:
                rename_no_replace(
                    quarantine,
                    name,
                    source_fd=parent_fd,
                    destination_fd=parent_fd,
                )
            except FileExistsError as exc:
                raise ActionableError(
                    f"{scope_label} destination changed during rollback; "
                    f"preserved prior entry as {destination.parent / quarantine}"
                ) from exc
        raise


def replace_project_entry(
    destination: Path,
    backup: Backup | None,
    project: Path,
    expected_before: str | None,
    materialize: Any | None,
) -> None:
    """Replace one project entry through a no-follow parent descriptor."""
    with project_parent_fd(project, destination) as (parent_fd, name):
        replace_anchored_entry(
            destination,
            backup,
            expected_before,
            materialize,
            parent_fd,
            name,
            scope_label="project",
        )


def restore_project_entry(
    project: Path,
    destination: Path,
    source: Path | None,
    *,
    expected_current: str | None = None,
    modes: dict[str, str] | None = None,
    root_mode: str | None = None,
    source_parent_fd: int | None = None,
    source_name: str | None = None,
) -> None:
    def materialize(temporary: Path, destination_parent_fd: int) -> None:
        if source_parent_fd is not None and source_name is not None:
            copy_entry_between_fds(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                temporary.name,
            )
        else:
            assert source is not None
            copy_path(source, temporary)
        restore_modes(temporary, modes, root_mode)

    has_source = source is not None or (
        source_parent_fd is not None and source_name is not None
    )

    replace_project_entry(
        destination,
        None,
        project,
        expected_current,
        materialize if has_source else None,
    )


def restore_anchored_backup_entry(
    entry: dict[str, Any],
    destination: Path,
    source: Path | None,
    *,
    source_parent_fd: int | None = None,
    source_name: str | None = None,
) -> None:
    """Restore a user legacy entry only beneath its original directory inode."""
    root = Path(entry["restore_root"])
    relative = Path(entry["restore_relative"])
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        root_fd = os.open(root, flags)
    except OSError as exc:
        raise ActionableError(
            f"user legacy root changed during rollback: {root}: {exc}"
        ) from exc
    try:
        current_root = os.fstat(root_fd)
        if (
            current_root.st_dev != entry.get("restore_root_dev")
            or current_root.st_ino != entry.get("restore_root_ino")
        ):
            raise ActionableError(
                f"user legacy root changed during rollback: {root}"
            )

        def materialize(temporary: Path, destination_parent_fd: int) -> None:
            if source_parent_fd is not None and source_name is not None:
                copy_entry_between_fds(
                    source_parent_fd,
                    source_name,
                    destination_parent_fd,
                    temporary.name,
                )
            else:
                assert source is not None
                copy_path(source, temporary)
            restore_modes(
                temporary,
                entry.get("modes"),
                entry.get("mode"),
            )

        with relative_parent_fd(root_fd, relative) as (parent_fd, name):
            replace_anchored_entry(
                destination,
                None,
                entry.get("after"),
                materialize
                if source is not None
                or (source_parent_fd is not None and source_name is not None)
                else None,
                parent_fd,
                name,
                scope_label="user legacy",
            )
    finally:
        os.close(root_fd)


def replace_tree(
    source: Path,
    destination: Path,
    backup: Backup,
    *,
    project: Path | None = None,
    expected_before: str | None = None,
) -> None:
    if project is not None:
        if expected_before is None:
            raise ActionableError(
                f"missing project precondition for destination: {destination}"
            )
        replace_project_entry(
            destination,
            backup,
            project,
            expected_before,
            lambda temporary, _parent_fd: copy_path(source, temporary),
        )
        return
    if fingerprint(source) == fingerprint(destination):
        return
    backup.before(destination)
    if lexists(destination):
        remove_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True)


def replace_link(
    destination: Path,
    target: Path,
    backup: Backup,
    *,
    project: Path | None = None,
    expected_before: str | None = None,
) -> None:
    link_text = os.path.relpath(target, destination.parent)
    if project is not None:
        if expected_before is None:
            raise ActionableError(
                f"missing project precondition for destination: {destination}"
            )
        replace_project_entry(
            destination,
            backup,
            project,
            expected_before,
            lambda temporary, _parent_fd: temporary.symlink_to(link_text),
        )
        return
    if destination.is_symlink() and os.readlink(destination) == link_text:
        return
    backup.before(destination)
    if lexists(destination):
        remove_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.symlink_to(link_text)


def known_legacy_hashes(source: Path) -> dict[str, set[str]]:
    catalog_path = source / "data" / "installer" / "legacy-skill-hashes.json"
    catalog = json.loads(catalog_path.read_text())
    if catalog.get("schema") != "ffs.legacy-hashes/v1":
        raise ActionableError(f"unsupported legacy hash catalog: {catalog_path}")
    known = {path: set(values) for path, values in catalog["hashes_by_path"].items()}
    for skill, directory in source_skills(source).items():
        for file in directory.rglob("*"):
            if file.is_file() and not file.is_symlink():
                rel = f"skills/{skill}/{file.relative_to(directory).as_posix()}"
                known.setdefault(rel, set()).add(sha256_file(file))
    return known


def legacy_skill_names(source: Path) -> set[str]:
    known = known_legacy_hashes(source)
    names = {path.split("/", 2)[1] for path in known if path.startswith("skills/")}
    names.add("prompt-master")
    return names


def migrate_legacy(
    source: Path,
    roots: list[Path],
    backup: Backup,
    *,
    project: Path | None = None,
) -> list[Path]:
    known = known_legacy_hashes(source)
    legacy_names = legacy_skill_names(source)
    preserved: list[Path] = []

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )

    def legacy_changed(path: Path, detail: str | None = None) -> ActionableError:
        suffix = f": {detail}" if detail else ""
        return ActionableError(f"legacy skill changed during migration: {path}{suffix}")

    def same_inode(left: os.stat_result, right: os.stat_result) -> bool:
        return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)

    def hash_open_file(file_fd: int) -> str:
        digest = hashlib.sha256()
        os.lseek(file_fd, 0, os.SEEK_SET)
        while block := os.read(file_fd, 1024 * 1024):
            digest.update(block)
        return digest.hexdigest()

    def restore_quarantine(
        parent_fd: int, quarantine: str, original: str, shown: Path
    ) -> None:
        try:
            rename_no_replace(
                quarantine,
                original,
                source_fd=parent_fd,
                destination_fd=parent_fd,
            )
        except FileExistsError as exc:
            raise legacy_changed(
                shown,
                f"a concurrent entry won the destination; preserved as {quarantine}",
            ) from exc

    def process_anchored_root(root_fd: int, display_root: Path) -> None:
        """Migrate legacy files without resolving mutable path components."""
        root_identity = os.fstat(root_fd)

        def process_directory(
            directory_fd: int,
            skill_name: str,
            relative: tuple[str, ...],
        ) -> None:
            try:
                names = sorted(os.listdir(directory_fd), reverse=True)
            except OSError as exc:
                raise legacy_changed(
                    display_root / skill_name / Path(*relative), str(exc)
                ) from exc
            for name in names:
                shown = display_root / skill_name / Path(*relative, name)
                try:
                    before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError as exc:
                    raise legacy_changed(shown, str(exc)) from exc
                mode = before.st_mode
                if stat.S_ISDIR(mode):
                    try:
                        child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                    except OSError as exc:
                        raise legacy_changed(shown, str(exc)) from exc
                    try:
                        opened = os.fstat(child_fd)
                        if not same_inode(before, opened):
                            raise legacy_changed(shown)
                        process_directory(
                            child_fd, skill_name, (*relative, name)
                        )
                        try:
                            current = os.stat(
                                name,
                                dir_fd=directory_fd,
                                follow_symlinks=False,
                            )
                        except OSError as exc:
                            raise legacy_changed(shown, str(exc)) from exc
                        if not same_inode(opened, current):
                            raise legacy_changed(shown)
                        with contextlib.suppress(OSError):
                            os.rmdir(name, dir_fd=directory_fd)
                    finally:
                        os.close(child_fd)
                    continue
                if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                    preserved.append(shown)
                    continue

                quarantine = f".ffs-legacy-{secrets.token_hex(12)}"
                try:
                    rename_no_replace(
                        name,
                        quarantine,
                        source_fd=directory_fd,
                        destination_fd=directory_fd,
                    )
                except (FileExistsError, OSError) as exc:
                    raise legacy_changed(shown, str(exc)) from exc
                try:
                    try:
                        file_fd = os.open(
                            quarantine,
                            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=directory_fd,
                        )
                    except OSError as exc:
                        restore_quarantine(directory_fd, quarantine, name, shown)
                        raise legacy_changed(shown, str(exc)) from exc
                    try:
                        quarantined = os.fstat(file_fd)
                        if not same_inode(before, quarantined) or not stat.S_ISREG(
                            quarantined.st_mode
                        ):
                            restore_quarantine(directory_fd, quarantine, name, shown)
                            raise legacy_changed(shown)
                        digest = hash_open_file(file_fd)
                    finally:
                        os.close(file_fd)

                    catalog_path = (
                        f"skills/{skill_name}/"
                        f"{Path(*relative, name).as_posix()}"
                    )
                    if digest not in known.get(catalog_path, set()):
                        restore_quarantine(directory_fd, quarantine, name, shown)
                        preserved.append(shown)
                        continue
                    entry = backup.before_anchored(shown, directory_fd, quarantine)
                    try:
                        current = os.stat(
                            quarantine,
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                    except OSError as exc:
                        raise legacy_changed(shown, str(exc)) from exc
                    if not same_inode(before, current):
                        raise legacy_changed(shown)
                    os.unlink(quarantine, dir_fd=directory_fd)
                    if entry is not None:
                        entry["after"] = "missing"
                        if project is None:
                            entry["restore_root"] = str(display_root.absolute())
                            entry["restore_relative"] = shown.relative_to(
                                display_root
                            ).as_posix()
                            entry["restore_root_dev"] = root_identity.st_dev
                            entry["restore_root_ino"] = root_identity.st_ino
                except Exception:
                    try:
                        os.stat(
                            quarantine,
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        restore_quarantine(directory_fd, quarantine, name, shown)
                    raise

        try:
            skill_names = sorted(os.listdir(root_fd))
        except OSError as exc:
            raise legacy_changed(display_root, str(exc)) from exc
        for skill_name in skill_names:
            if skill_name not in legacy_names:
                continue
            shown_skill = display_root / skill_name
            try:
                before = os.stat(
                    skill_name, dir_fd=root_fd, follow_symlinks=False
                )
            except OSError as exc:
                raise legacy_changed(shown_skill, str(exc)) from exc
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                preserved.append(shown_skill)
                continue
            try:
                skill_fd = os.open(skill_name, directory_flags, dir_fd=root_fd)
            except OSError as exc:
                raise legacy_changed(shown_skill, str(exc)) from exc
            try:
                opened = os.fstat(skill_fd)
                if not same_inode(before, opened):
                    raise legacy_changed(shown_skill)
                process_directory(skill_fd, skill_name, ())
                try:
                    current = os.stat(
                        skill_name, dir_fd=root_fd, follow_symlinks=False
                    )
                except OSError as exc:
                    raise legacy_changed(shown_skill, str(exc)) from exc
                if not same_inode(opened, current):
                    raise legacy_changed(shown_skill)
                with contextlib.suppress(OSError):
                    os.rmdir(skill_name, dir_fd=root_fd)
            finally:
                os.close(skill_fd)

    for root in roots:
        if project is not None:
            relative = root.absolute().relative_to(project.resolve())
            safe_project_directory(project, relative)
        if not lexists(root):
            continue
        if project is None:
            try:
                root_fd = os.open(root, directory_flags)
            except OSError as exc:
                raise ActionableError(
                    f"unsafe legacy skill root {root}: {exc}"
                ) from exc
            try:
                process_anchored_root(root_fd, root)
            finally:
                os.close(root_fd)
            continue
        # Open the final legacy root itself with O_NOFOLLOW and keep all
        # traversal/deletions anchored to that descriptor.
        with project_parent_fd(project, root / ".ffs-legacy-anchor") as (
            directory_fd,
            _anchor,
        ):
            process_anchored_root(directory_fd, root)
    return preserved


def stage_prompt_master(source: Path, backup: Backup) -> Path | None:
    """Materialize the pinned external skill without giving it ownership."""
    if os.environ.get("FFS_SKIP_PROMPT_MASTER") == "1":
        return None
    installer = source / "scripts" / "install-prompt-master.sh"
    if not installer.is_file():
        raise ActionableError(f"pinned prompt-master installer is missing: {installer}")
    staged = backup.directory / "prompt-master-stage"
    command = ["bash", str(installer), "--dest", str(staged)]
    external_source = os.environ.get("FFS_PROMPT_MASTER_SOURCE")
    if external_source:
        command.extend(["--source", external_source])
    process = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "unknown failure"
        raise ActionableError(f"pinned prompt-master installation failed: {detail}")
    return staged


def verify_gsd_package(source: Path) -> None:
    package_path = source / "package.json"
    installed_path = source / "node_modules" / "@opengsd" / "gsd-core" / "package.json"
    package = read_json(package_path)
    installed = read_json(installed_path)
    declared = (package or {}).get("devDependencies", {}).get("@opengsd/gsd-core")
    actual = (installed or {}).get("version")
    if declared != GSD_VERSION:
        raise ActionableError(
            f"package metadata must pin @opengsd/gsd-core exactly to {GSD_VERSION}; found {declared!r}"
        )
    if actual != GSD_VERSION:
        raise ActionableError(
            f"installed @opengsd/gsd-core must be {GSD_VERSION}; found {actual!r}; run npm install"
        )


def install_gsd_profiles(source: Path) -> None:
    """Delegate complete host surfaces to the pinned upstream installer."""
    verify_gsd_package(source)
    override = os.environ.get("FFS_GSD_INSTALLER")
    installer = Path(override) if override else source / "node_modules" / ".bin" / "gsd-core"
    if not installer.is_file():
        raise ActionableError(f"GSD upstream installer is missing: {installer}")
    for runtime in ("claude", "codex"):
        process = subprocess.run(
            [str(installer), f"--{runtime}", "--global", "--profile=full"],
            cwd=source,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if process.returncode != 0:
            detail = process.stderr.strip() or process.stdout.strip() or "unknown failure"
            raise ActionableError(f"GSD {runtime} full-profile installation failed: {detail}")


def gsd_manifest_owned_paths(runtime: str) -> set[Path]:
    root = gsd_config_roots()[runtime]
    result = {root / "gsd-file-manifest.json", root / "gsd-core"}
    manifest = read_json(root / "gsd-file-manifest.json")
    if manifest and isinstance(manifest.get("files"), dict):
        for relative in manifest["files"]:
            rel = Path(relative)
            if runtime == "codex" and rel.parts and rel.parts[0] == "skills":
                result.add(Path.home() / ".agents" / rel)
            else:
                result.add(root / rel)
    # These are shared configuration surfaces the upstream installer may merge.
    # Snapshot exact prior bytes; do not infer ownership of unrelated siblings.
    for name in ("config.toml", "hooks.json", "settings.json"):
        candidate = root / name
        if lexists(candidate):
            result.add(candidate)
    return result


def existing_gsd_namespace_paths() -> set[Path]:
    """Capture pre-manifest GSD paths that a newer installer may overwrite."""
    claude = gsd_config_roots()["claude"]
    codex = gsd_config_roots()["codex"]
    result: set[Path] = set()
    for root, patterns in (
        (claude, ("commands/gsd-*", "agents/gsd-*", "skills/gsd-*", "gsd-core", "hooks")),
        (codex, ("agents/gsd-*", "scripts/gsd-*", "gsd-core", "hooks")),
        (Path.home() / ".agents", ("skills/gsd-*",)),
    ):
        for pattern in patterns:
            result.update(path for path in root.glob(pattern) if lexists(path))
    for root in (claude, codex):
        for name in ("gsd-file-manifest.json", "config.toml", "hooks.json", "settings.json"):
            candidate = root / name
            if lexists(candidate):
                result.add(candidate)
    return result


def collapse_paths(paths: set[Path]) -> set[Path]:
    """Remove descendants already protected by an owned ancestor snapshot."""
    collapsed: set[Path] = set()
    for candidate in sorted(paths, key=lambda item: (len(item.parts), str(item))):
        if any(parent in collapsed for parent in candidate.parents):
            continue
        collapsed.add(candidate)
    return collapsed


def install_gsd_with_rollback(source: Path, backup: Backup) -> None:
    with exclusive_lock(cache_root() / "gsd-install.lock"):
        before = collapse_paths(
            existing_gsd_namespace_paths()
            | set().union(
                *(gsd_manifest_owned_paths(runtime) for runtime in ("claude", "codex"))
            )
        )
        for path in sorted(before, key=str):
            backup.before(path)
        try:
            install_gsd_profiles(source)
        except Exception:
            # A failed upstream installer can leave a truncated manifest. Do
            # not let discovery of that damaged output bypass restoration of
            # the snapshots we already hold.
            after_failure: set[Path] = set()
            with contextlib.suppress(Exception):
                after_failure.update(existing_gsd_namespace_paths())
            for runtime in ("claude", "codex"):
                with contextlib.suppress(Exception):
                    after_failure.update(gsd_manifest_owned_paths(runtime))
            for path in sorted(collapse_paths(after_failure) - before, key=str):
                backup.assume_created(path)
            try:
                backup.restore_uncommitted()
            finally:
                backup.finish()
            raise
        after = collapse_paths(
            existing_gsd_namespace_paths()
            | set().union(
                *(gsd_manifest_owned_paths(runtime) for runtime in ("claude", "codex"))
            )
        )
        for path in sorted(after - before, key=str):
            backup.assume_created(path)


def install(source: Path, scope: str, project: Path | None) -> int:
    skills = source_skills(source)
    release_version = source_version(source)
    destination_manifest = manifest_path(scope, project)
    previous = read_json(destination_manifest)
    destination_manifest_before = fingerprint(destination_manifest)
    managed = managed_fingerprints(previous, scope, project)
    backup = Backup("install", scope, project)
    prompt_master = stage_prompt_master(source, backup)

    planned: list[tuple[Path, str, bool]] = []
    if scope == "project":
        assert project is not None
        vendor = project / ".feature-fix-swarm" / "vendor" / "skills"
        for name, source_dir in skills.items():
            target = vendor / name
            planned.append((target, fingerprint(source_dir), False))
            for host in (".agents", ".claude"):
                link = project / host / "skills" / name
                expected = "symlink:" + os.path.relpath(target, link.parent)
                planned.append((link, expected, True))
        if prompt_master:
            canonical = project / ".agents" / "skills" / "prompt-master"
            planned.append((canonical, fingerprint(prompt_master), False))
            claude_link = project / ".claude" / "skills" / "prompt-master"
            planned.append((claude_link, "symlink:" + os.path.relpath(canonical, claude_link.parent), True))
    else:
        for name, source_dir in skills.items():
            for host in (Path.home() / ".agents", Path.home() / ".claude"):
                planned.append((host / "skills" / name, fingerprint(source_dir), False))
        if prompt_master:
            for host in (Path.home() / ".agents", Path.home() / ".claude"):
                planned.append((host / "skills" / "prompt-master", fingerprint(prompt_master), False))
    preflight_fingerprints: dict[str, str] = {}
    for path, expected, broken_ok in planned:
        if scope == "project":
            assert project is not None
            try:
                relative = path.absolute().relative_to(project.resolve())
            except ValueError as exc:
                raise ActionableError(f"project install path escapes checkout: {path}") from exc
            safe_project_destination(project, relative)
        ensure_replaceable(path, expected, managed, broken_link_ok=broken_ok)
        preflight_fingerprints[str(path.absolute())] = fingerprint(path)

    try:
        # GSD owns every artifact this command writes. FFS invokes the exact
        # pinned installer but never copies, patches, or records individual
        # GSD files. All later FFS writes share this transaction snapshot.
        install_gsd_with_rollback(source, backup)

        if scope == "project":
            assert project is not None
            # The upstream installer can take long enough for checkout paths
            # to change. Re-prove every ancestor immediately before writing.
            for path, _, _ in planned:
                relative = path.absolute().relative_to(project.resolve())
                safe_project_destination(project, relative)
            legacy_root = safe_project_destination(project, Path(".codex") / "skills")
            for name, source_dir in skills.items():
                target = safe_project_destination(
                    project, Path(".feature-fix-swarm") / "vendor" / "skills" / name
                )
                replace_tree(
                    source_dir,
                    target,
                    backup,
                    project=project,
                    expected_before=preflight_fingerprints[str(target.absolute())],
                )
                for host in (".agents", ".claude"):
                    link = safe_project_destination(project, Path(host) / "skills" / name)
                    replace_link(
                        link,
                        target,
                        backup,
                        project=project,
                        expected_before=preflight_fingerprints[str(link.absolute())],
                    )
            if prompt_master:
                canonical = safe_project_destination(
                    project, Path(".agents") / "skills" / "prompt-master"
                )
                replace_tree(
                    prompt_master,
                    canonical,
                    backup,
                    project=project,
                    expected_before=preflight_fingerprints[str(canonical.absolute())],
                )
                claude_link = safe_project_destination(
                    project, Path(".claude") / "skills" / "prompt-master"
                )
                replace_link(
                    claude_link,
                    canonical,
                    backup,
                    project=project,
                    expected_before=preflight_fingerprints[str(claude_link.absolute())],
                )
            legacy_roots = [legacy_root]
        else:
            for name, source_dir in skills.items():
                for host in (Path.home() / ".agents", Path.home() / ".claude"):
                    replace_tree(source_dir, host / "skills" / name, backup)
            if prompt_master:
                for host in (Path.home() / ".agents", Path.home() / ".claude"):
                    replace_tree(prompt_master, host / "skills" / "prompt-master", backup)
            codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
            legacy_roots = [codex_home / "skills"]

        preserved = migrate_legacy(
            source,
            legacy_roots,
            backup,
            project=project if scope == "project" else None,
        )
        paths: dict[str, dict[str, str]] = {}
        for path, _, _ in planned:
            paths[manifest_key(path, scope, project)] = {"fingerprint": fingerprint(path)}
        installed_at = datetime.now(timezone.utc).isoformat()
        if (
            previous
            and previous.get("schema") == INSTALL_SCHEMA
            and previous.get("scope") == scope
            and previous.get("version") == release_version
            and isinstance(previous.get("installed_at"), str)
        ):
            installed_at = previous["installed_at"]
        rendered_source = manifest_source(source, scope, project)
        manifest_value = {
            "schema": INSTALL_SCHEMA,
            "version": release_version,
            "scope": scope,
            "installed_at": installed_at,
            "source": rendered_source,
            "paths": paths,
            "gsd": {
                "owner": "upstream-installer",
                "version": GSD_VERSION,
                "profiles": {"claude": "full", "codex": "full"},
            },
        }
        if scope == "project":
            assert project is not None
            replace_project_entry(
                destination_manifest,
                backup,
                project,
                destination_manifest_before,
                lambda temporary, _parent_fd: write_json_file(
                    temporary, manifest_value
                ),
            )
        else:
            backup.before(destination_manifest)
            atomic_json(destination_manifest, manifest_value)
        backup.finish()
    except Exception:
        # install_gsd_with_rollback already restores on failures inside the
        # upstream phase. This outer transaction covers every subsequent FFS
        # mutation, including legacy cleanup and manifest creation.
        backup.restore_uncommitted()
        backup.finish()
        raise
    finally:
        if prompt_master and prompt_master.exists():
            shutil.rmtree(prompt_master)
    print(f"backup_id={backup.backup_id}")
    print(f"installed_scope={scope}")
    print(f"gsd=upstream-installer@{GSD_VERSION}:claude-full,codex-full")
    if preserved:
        for path in preserved:
            print(
                f"preserved unknown legacy file {path}; remove or migrate it manually, then run --doctor",
                file=sys.stderr,
            )
        return 1
    return 0


def uninstall(scope: str, project: Path | None) -> int:
    path = manifest_path(scope, project)
    manifest = read_json(path)
    if not manifest or manifest.get("schema") != INSTALL_SCHEMA:
        raise ActionableError(f"no managed {scope} installation found at {path}")
    backup = Backup("uninstall", scope, project)
    preserved: list[Path] = []
    for key, metadata in manifest.get("paths", {}).items():
        destination = manifest_destination(key, scope, project)
        expected = metadata.get("fingerprint") if isinstance(metadata, dict) else None
        if not isinstance(expected, str):
            preserved.append(destination)
            continue
        if scope == "project":
            assert project is not None
            try:
                replace_project_entry(
                    destination,
                    backup,
                    project,
                    expected,
                    None,
                )
            except ActionableError:
                preserved.append(destination)
            continue
        if not lexists(destination):
            continue
        if fingerprint(destination) != expected:
            preserved.append(destination)
            continue
        backup.before(destination)
        remove_path(destination)
    if scope == "project":
        assert project is not None
        replace_project_entry(
            path,
            backup,
            project,
            fingerprint(path),
            None,
        )
    else:
        backup.before(path)
        path.unlink()
    backup.finish()
    print(f"backup_id={backup.backup_id}")
    if preserved:
        for destination in preserved:
            print(f"preserved edited managed path {destination}; remove it manually if desired", file=sys.stderr)
        return 1
    return 0


def rollback(backup_id: str) -> int:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", backup_id):
        raise InvocationError("invalid backup id")
    directory_fd, _directory = open_backup_directory(backup_id)
    try:
        with anchored_working_directory(directory_fd):
            manifest = read_json(Path("manifest.json"))
        if not manifest or manifest.get("schema") != BACKUP_SCHEMA:
            raise ActionableError(f"backup not found or invalid: {backup_id}")
        project_text = manifest.get("project_dir")
        project = Path(project_text) if project_text else None
        lock_path = project_lock_path(project) if project else cache_root() / "setup.lock"
        with exclusive_lock(lock_path):
            conflicts: list[Path] = []
            for entry in reversed(manifest.get("entries", [])):
                destination = Path(entry["path"])
                backup_rel = entry.get("backup")
                if project is not None:
                    try:
                        destination.absolute().relative_to(project.resolve())
                    except ValueError:
                        pass
                    else:
                        try:
                            restore_project_entry(
                                project,
                                destination,
                                None,
                                expected_current=entry.get("after"),
                                modes=entry.get("modes"),
                                root_mode=entry.get("mode"),
                                source_parent_fd=(directory_fd if backup_rel else None),
                                source_name=backup_rel,
                            )
                        except ActionableError:
                            conflicts.append(destination)
                        continue
                if entry.get("restore_root") and entry.get("restore_relative"):
                    try:
                        restore_anchored_backup_entry(
                            entry,
                            destination,
                            None,
                            source_parent_fd=(directory_fd if backup_rel else None),
                            source_name=backup_rel,
                        )
                    except ActionableError:
                        conflicts.append(destination)
                    continue
                current = fingerprint(destination)
                if current != entry.get("after"):
                    conflicts.append(destination)
                    continue
                if lexists(destination):
                    remove_path(destination)
                if backup_rel:
                    with anchored_working_directory(directory_fd):
                        copy_path(Path(backup_rel), destination)
                        restore_modes(
                            destination, entry.get("modes"), entry.get("mode")
                        )
            if conflicts:
                for destination in conflicts:
                    print(
                        f"preserved path changed since backup: {destination}",
                        file=sys.stderr,
                    )
                return 1
    finally:
        os.close(directory_fd)
    print(f"rolled_back={backup_id}")
    return 0


def check_entry(checks: list[dict[str, str]], identifier: str, status: str, message: str, remediation: str | None = None) -> None:
    item = {"id": identifier, "status": status, "message": message}
    if remediation:
        item["remediation"] = remediation
    checks.append(item)


def gsd_config_roots() -> dict[str, Path]:
    return {
        "claude": Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))),
        "codex": Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))),
    }


def add_gsd_doctor_checks(checks: list[dict[str, str]], source: Path) -> None:
    try:
        verify_gsd_package(source)
    except ActionableError as exc:
        check_entry(checks, "gsd-package", "fail", str(exc), "install the exact pinned package from the lockfile")
    else:
        check_entry(checks, "gsd-package", "pass", f"@opengsd/gsd-core is exactly {GSD_VERSION}")

    errors: list[str] = []
    for runtime, root in gsd_config_roots().items():
        path = root / "gsd-file-manifest.json"
        try:
            manifest = read_json(path)
        except ActionableError as exc:
            errors.append(str(exc))
            continue
        if not manifest:
            errors.append(f"{runtime} upstream manifest missing: {path}")
            continue
        if manifest.get("version") != GSD_VERSION:
            errors.append(f"{runtime} manifest version is {manifest.get('version')!r}, expected {GSD_VERSION}")
        if manifest.get("mode") != "full":
            errors.append(f"{runtime} manifest mode is {manifest.get('mode')!r}, expected 'full'")
        if not isinstance(manifest.get("files"), dict) or not manifest["files"]:
            errors.append(f"{runtime} manifest has no upstream-owned files")
        version_file = root / "gsd-core" / "VERSION"
        if not version_file.is_file() or version_file.read_text().strip() != GSD_VERSION:
            errors.append(f"{runtime} GSD VERSION marker does not match {GSD_VERSION}: {version_file}")
    if errors:
        check_entry(
            checks,
            "gsd-manifests",
            "fail",
            "; ".join(errors),
            "rerun setup for both upstream full profiles",
        )
    else:
        check_entry(checks, "gsd-manifests", "pass", "Claude and Codex full-profile manifests are upstream-owned at 1.9.1")


def _load_sibling_module(path: Path, name: str) -> Any:
    """importlib.util load, not ``import`` — ffs_installer.py runs both as a
    package member (pytest) and as a bare script (setup.sh), and those two
    contexts disagree on whether ``lib/`` sibling modules are importable by
    bare name. Loading by explicit file path sidesteps that entirely."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ActionableError(f"cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _catalog_model_ids(catalog: dict[str, Any]) -> set[str]:
    """Every string found under a 'model' key anywhere in gsd-core's catalog."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "model" and isinstance(value, str):
                    found.add(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(catalog)
    return found


def add_stale_bake_doctor_check(checks: list[dict[str, str]], source: Path, cwd: Path) -> None:
    """AC-009(a): surface gsd-core's stale-bake-guard state (advisory)."""
    guard = (
        source / "node_modules" / "@opengsd" / "gsd-core" / "gsd-core" / "bin" / "lib" / "stale-bake-guard.cjs"
    )
    if not guard.is_file():
        check_entry(checks, "stale-bake-guard", "warn", f"gsd-core stale-bake-guard module not found: {guard}")
        return
    script = (
        "const g = require(process.argv[1]);"
        "const fs = require('fs');"
        "let config = {};"
        "try { config = JSON.parse(fs.readFileSync(process.argv[2] + '/.planning/config.json', 'utf8')); } catch (e) {}"
        "const runtime = g.resolveRuntimeFromConfig(config);"
        "if (!g.STATIC_FRONTMATTER_RUNTIMES.includes(runtime)) { console.log(JSON.stringify({applicable: false})); process.exit(0); }"
        "const newest = g.findNewestConfigMtime(process.argv[2]);"
        "const oldest = g.findOldestAgentMtime(runtime);"
        "if (!newest || !oldest) { console.log(JSON.stringify({applicable: false})); process.exit(0); }"
        "const signal = g.detectStaleBake({runtime, configMtimeMs: newest.mtimeMs, agentMtimeMs: oldest.mtimeMs});"
        "console.log(JSON.stringify({applicable: true, runtime, stale: !!signal}));"
    )
    node = shutil.which("node")
    if not node:
        check_entry(checks, "stale-bake-guard", "pass", "node is not installed; stale-bake check skipped")
        return
    process = subprocess.run(
        [node, "-e", script, str(guard), str(cwd)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    try:
        result = json.loads(process.stdout) if process.returncode == 0 else None
    except json.JSONDecodeError:
        result = None
    if result is None:
        check_entry(checks, "stale-bake-guard", "warn", f"stale-bake probe failed: {process.stderr.strip() or 'unknown error'}")
    elif not result.get("applicable"):
        check_entry(checks, "stale-bake-guard", "pass", "no static-frontmatter runtime (codex/kilo/opencode) configured; stale-bake check not applicable")
    elif result.get("stale"):
        check_entry(
            checks,
            "stale-bake-guard",
            "warn",
            f"{result['runtime']} agents were baked before the current model config changed",
            f"rerun 'gsd install --{result['runtime']}' (or 'gsd update')",
        )
    else:
        check_entry(checks, "stale-bake-guard", "pass", f"{result['runtime']} agent bake is current with model config")


def add_model_routing_doctor_checks(checks: list[dict[str, str]], source: Path) -> None:
    """AC-009(b)/(c): canonical-tier probe + per-surface catalog/resolver warnings."""
    model_requests = _load_sibling_module(source / "lib" / "model_requests.py", "ffs_doctor_model_requests")
    lint_mod = _load_sibling_module(source / "scripts" / "lint_model_routing.py", "ffs_doctor_lint_model_routing")

    # (b) probe every canonical-tier exact id, deduped by (vendor, id), for
    # each host whose CLI is installed. Force a fresh probe past the 24h
    # cache TTL (EDGE-006) via model-probe-lib.sh's cached probe functions.
    hosts = [host for host in ("claude", "codex") if shutil.which(host)]
    if not hosts:
        check_entry(checks, "model-resolvability", "pass", "no host CLI (claude/codex) installed; probe skipped")
    else:
        probe_lib = source / "scripts" / "gsd" / "model-probe-lib.sh"
        targets: dict[tuple[str, str], str] = {}
        for host in hosts:
            for tier in lint_mod.CANONICAL_TIERS:
                info = model_requests.resolve_request({"kind": "tier", "name": tier}, host=host)
                targets[(host, info["model"])] = tier
        unreachable: list[str] = []
        for (vendor, model_id), tier in sorted(targets.items()):
            probe_fn = "probe_claude_model" if vendor == "claude" else "probe_codex_model"
            result = subprocess.run(
                ["bash", "-c", f'. "$1" && {probe_fn} "$2"', "_", str(probe_lib), model_id],
                env={**os.environ, "GSD_MODEL_PROBE_FORCE": "1"},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if result.returncode != 0:
                unreachable.append(f"{vendor}/{tier}={model_id}")
        if unreachable:
            check_entry(
                checks,
                "model-resolvability",
                "warn",
                f"unreachable canonical-tier models: {', '.join(unreachable)}",
                "verify CLI auth/availability for these models",
            )
        else:
            check_entry(checks, "model-resolvability", "pass", f"all canonical-tier models resolved for {', '.join(hosts)}")

    # (c) per-surface catalog + resolver cross-checks (advisory, independent —
    # a surface can be resolver-known but catalog-absent, e.g. claude-opus-5
    # today, or vice versa).
    catalog_path = (
        source / "node_modules" / "@opengsd" / "gsd-core" / "gsd-core" / "bin" / "shared" / "model-catalog.json"
    )
    catalog_ids = _catalog_model_ids(read_json(catalog_path) or {}) if catalog_path.is_file() else set()

    surfaces: dict[str, str] = {}  # "<source-file>:<role>" -> tier name (or raw alias if unrecognized)
    canonical_path = source / "templates" / "model-requests.json"
    if canonical_path.is_file():
        for role, request in (read_json(canonical_path) or {}).items():
            if isinstance(request, dict) and request.get("kind") == "tier":
                surfaces[f"model-requests.json:{role}"] = request.get("name")
    config_path = source / "templates" / "gsd-config.base.json"
    if config_path.is_file():
        overrides = (read_json(config_path) or {}).get("model_overrides", {})
        alias_to_tier = {alias: tier for tier, alias in lint_mod.TIER_ALIAS.items()}
        for role, alias in overrides.items():
            surfaces[f"gsd-config.base.json:{role}"] = alias_to_tier.get(alias, alias)

    resolver_absent: list[str] = []
    catalog_absent: list[str] = []
    for surface, tier in surfaces.items():
        if tier not in lint_mod.CANONICAL_TIERS:
            resolver_absent.append(f"{surface}={tier!r}")
            continue
        model_id = model_requests.resolve_request({"kind": "tier", "name": tier}, host="claude")["model"]
        if model_id not in catalog_ids:
            catalog_absent.append(f"{surface}={model_id}")

    if resolver_absent:
        check_entry(
            checks,
            "model-routing-resolver",
            "warn",
            f"surfaces absent from the FFS resolver: {', '.join(sorted(resolver_absent))}",
            "add the tier to lib/model_requests.py or fix the surface",
        )
    else:
        check_entry(checks, "model-routing-resolver", "pass", "every configured surface resolves via the FFS resolver")

    if catalog_absent:
        check_entry(
            checks,
            "model-routing-catalog",
            "warn",
            f"surfaces absent from the gsd-core catalog: {', '.join(sorted(catalog_absent))}",
            "expected until upstream gsd-core adds these model ids (tracked follow-up)",
        )
    else:
        check_entry(checks, "model-routing-catalog", "pass", "every configured surface's model is present in the gsd-core catalog")


def parse_cli_version(output: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", output)
    return tuple(map(int, match.groups())) if match else None


def add_codex_version_check(checks: list[dict[str, str]]) -> None:
    executable = shutil.which("codex")
    if not executable:
        check_entry(checks, "codex-cli-version", "pass", "Codex CLI is not installed; version gate not applicable")
        return
    process = subprocess.run(
        [executable, "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    parsed = parse_cli_version(process.stdout + "\n" + process.stderr) if process.returncode == 0 else None
    if parsed is None:
        check_entry(
            checks,
            "codex-cli-version",
            "fail",
            f"could not parse Codex CLI version from {executable}",
            "install Codex CLI >=0.137.0,<0.147.0",
        )
    elif not (CODEX_MIN_VERSION <= parsed < CODEX_MAX_VERSION):
        rendered = ".".join(map(str, parsed))
        check_entry(
            checks,
            "codex-cli-version",
            "fail",
            f"Codex CLI {rendered} is outside supported range >=0.137.0,<0.147.0",
            "install a supported Codex CLI release; 0.146.x is the tested line",
        )
    else:
        check_entry(checks, "codex-cli-version", "pass", f"Codex CLI {'.'.join(map(str, parsed))} is supported")


def doctor(scope: str, project: Path | None, as_json: bool) -> int:
    checks: list[dict[str, str]] = []
    source = Path(__file__).resolve().parents[1]
    add_gsd_doctor_checks(checks, source)
    add_codex_version_check(checks)
    add_stale_bake_doctor_check(checks, source, project or Path.cwd())
    add_model_routing_doctor_checks(checks, source)
    path = manifest_path(scope, project)
    try:
        manifest = read_json(path)
    except ActionableError as exc:
        manifest = None
        check_entry(checks, "manifest", "fail", str(exc), "reinstall after moving the malformed manifest aside")
    if not manifest:
        check_entry(checks, "manifest", "fail", f"managed installation not found: {path}", f"run setup.sh --scope {scope}")
    elif manifest.get("schema") != INSTALL_SCHEMA:
        check_entry(checks, "manifest", "fail", f"unsupported install manifest schema in {path}", "reinstall this scope")
    else:
        check_entry(checks, "manifest", "pass", f"managed {scope} manifest is valid")
        for key, metadata in manifest.get("paths", {}).items():
            destination = manifest_destination(key, scope, project)
            expected = metadata.get("fingerprint") if isinstance(metadata, dict) else None
            if fingerprint(destination) != expected:
                check_entry(checks, "managed-path", "fail", f"managed path drift: {destination}", "restore or reinstall this scope")
        if not any(item["id"] == "managed-path" and item["status"] == "fail" for item in checks):
            check_entry(checks, "managed-path", "pass", "all managed skill hashes and links match")
        if scope == "project":
            assert project is not None
            bad_links = []
            for host in (".agents", ".claude"):
                for link in (project / host / "skills").glob("*"):
                    if link.is_symlink() and os.path.isabs(os.readlink(link)):
                        bad_links.append(link)
            if bad_links:
                check_entry(checks, "portable-links", "fail", f"absolute project skill links: {', '.join(map(str, bad_links))}", "reinstall project scope")
            else:
                check_entry(checks, "portable-links", "pass", "project skill links are relative")

    other_manifest: dict[str, Any] | None = None
    if scope == "project":
        other_manifest = read_json(cache_root() / "install-manifest.json")
    else:
        candidate_project = project or Path.cwd()
        other_manifest = read_json(candidate_project / ".feature-fix-swarm" / "install-manifest.json")
    if manifest and other_manifest:
        if manifest.get("version") == other_manifest.get("version"):
            check_entry(checks, "duplicate-version", "warn", "project and user installs have the same version; project scope takes precedence")
        else:
            check_entry(
                checks,
                "duplicate-version",
                "fail",
                f"project/user versions differ ({manifest.get('version')} vs {other_manifest.get('version')})",
                "upgrade or uninstall one scope",
            )
    else:
        check_entry(checks, "duplicate-version", "pass", "no conflicting project/user installation")

    legacy_root = (project / ".codex" / "skills") if scope == "project" and project else Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "skills"
    legacy_names = legacy_skill_names(Path(__file__).resolve().parents[1])
    legacy_files: list[Path] = []
    if legacy_root.exists():
        for name in legacy_names:
            candidate = legacy_root / name
            if candidate.is_symlink() or candidate.is_file():
                legacy_files.append(candidate)
            elif candidate.is_dir():
                legacy_files.extend(
                    item for item in candidate.rglob("*") if item.is_file() or item.is_symlink()
                )
    if legacy_files:
        check_entry(checks, "legacy-codex-skills", "fail", f"legacy .codex skill files remain under {legacy_root}", "move edited files aside; rerun setup to remove catalogued copies")
    else:
        check_entry(checks, "legacy-codex-skills", "pass", "legacy .codex/skills is clear")

    failed = any(item["status"] == "fail" for item in checks)
    warned = any(item["status"] == "warn" for item in checks)
    exit_code = 1 if failed else 0
    report: dict[str, Any] = {
        "schema": DOCTOR_SCHEMA,
        "status": "incompatible" if failed else "degraded" if warned else "healthy",
        "exit_code": exit_code,
        "scope": scope,
        "version": manifest.get("version") if manifest else None,
        "checks": checks,
    }
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"FFS doctor: {report['status']}")
        for item in checks:
            print(f"  {item['status'].upper():4} {item['id']}: {item['message']}")
            if item.get("remediation"):
                print(f"       remediation: {item['remediation']}")
    return exit_code


def reconcile_consumer(source: Path, target: Path) -> int:
    if target.is_symlink() or not target.is_dir():
        raise InvocationError(f"reconciliation target is not a directory: {target}")
    target = target.resolve()
    packaged_gsd = source / "scripts" / "gsd"
    fork_allowlist = safe_project_destination(
        target, Path("scripts") / "gsd" / "fork-allowlist.txt"
    )
    preserved_forks: set[str] = set()
    if fork_allowlist.is_symlink():
        raise ActionableError(f"consumer fork allowlist must not be a symlink: {fork_allowlist}")
    if fork_allowlist.is_file():
        for line in fork_allowlist.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                preserved_forks.add(stripped.split(maxsplit=1)[0])
    relative_files: list[str] = []
    for path in sorted(packaged_gsd.iterdir()):
        if path.is_symlink() or not path.is_file() or path.suffix not in {".sh", ".py"}:
            continue
        relative = path.relative_to(source)
        target_file = safe_project_destination(target, relative)
        if target_file.is_symlink():
            raise ActionableError(f"consumer destination must not be a symlink: {target_file}")
        if path.name in preserved_forks and target_file.exists():
            if not target_file.is_file():
                raise ActionableError(f"declared consumer fork is not a regular file: {target_file}")
            continue
        relative_files.append(str(relative))
    relative_files += [
        "scripts/hooks/cli-hang-guard.sh",
        "scripts/hooks/credential-output-guard.sh",
        "lib/model_requests.py",
    ]
    # spec-004 INT-003: plan-wall.sh (already covered by the scripts/gsd/*.sh
    # glob above) resolves its schema at $REPO_ROOT/schemas/*.json — a
    # consumer missing this file loses wall finding validation. Not a script,
    # so it travels through the non-executable branch below.
    data_files = ["schemas/review-finding.schema.json"]
    for relative in relative_files + data_files:
        source_file = source / relative
        target_file = safe_project_destination(target, relative)
        if target_file.is_symlink():
            raise ActionableError(f"consumer destination must not be a symlink: {target_file}")
        if source_file.resolve() != target_file.resolve():
            atomic_copy_inside(target, relative, source_file, executable=relative not in data_files)
    print("consumer_runtime=reconciled")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="setup.sh", add_help=True)
    result.add_argument("--scope", choices=("project", "user"))
    result.add_argument("--project-dir", type=Path)
    result.add_argument("--doctor", action="store_true")
    result.add_argument("--json", action="store_true")
    result.add_argument("--rollback")
    result.add_argument("--uninstall", action="store_true")
    result.add_argument("--reconcile-consumer", type=Path)
    result.add_argument("--yes", "-y", action="store_true", help="compatibility no-op; installs are non-interactive")
    return result


def parse(argv: list[str]) -> tuple[argparse.Namespace, bool]:
    no_arguments = not argv
    try:
        args = parser().parse_args(argv)
    except SystemExit as exc:
        if exc.code == 0:
            raise
        raise InvocationError("invalid command line; run setup.sh --help") from None
    actions = sum(bool(value) for value in (args.doctor, args.rollback, args.uninstall, args.reconcile_consumer))
    if actions > 1:
        raise InvocationError("choose only one of --doctor, --rollback, --uninstall, or --reconcile-consumer")
    if args.rollback:
        if args.scope or args.project_dir or args.json:
            raise InvocationError("--rollback accepts only a backup id")
        return args, no_arguments
    if args.reconcile_consumer:
        if args.scope or args.project_dir or args.json:
            raise InvocationError("--reconcile-consumer cannot be combined with scope options")
        return args, no_arguments
    if not args.scope:
        if no_arguments:
            args.scope = "user"
        else:
            raise InvocationError("--scope project|user is required")
    if args.scope == "project" and args.project_dir is None:
        raise InvocationError("--project-dir is required for project scope")
    if args.scope == "user" and args.project_dir is not None:
        raise InvocationError("--project-dir is valid only for project scope")
    if args.json and not args.doctor:
        raise InvocationError("--json is valid only with --doctor")
    return args, no_arguments


def invalid_report(message: str, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"schema": DOCTOR_SCHEMA, "status": "error", "exit_code": 2, "error": message, "checks": []}, indent=2, sort_keys=True))
    else:
        print(f"ERROR: {message}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    actual = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in actual
    try:
        args, deprecated_no_args = parse(actual)
        source = Path(__file__).resolve().parents[1]
        if deprecated_no_args:
            print("DEPRECATED: setup.sh without arguments implies --scope user for one transition release", file=sys.stderr)
        if args.rollback:
            return rollback(args.rollback)
        if args.reconcile_consumer:
            return reconcile_consumer(source, args.reconcile_consumer.resolve())
        project = args.project_dir.resolve() if args.project_dir else None
        lock_path = project_lock_path(project) if args.scope == "project" else cache_root() / "setup.lock"
        with exclusive_lock(lock_path):
            if args.doctor:
                return doctor(args.scope, project, args.json)
            if args.uninstall:
                return uninstall(args.scope, project)
            return install(source, args.scope, project)
    except InvocationError as exc:
        return invalid_report(str(exc), as_json)
    except ActionableError as exc:
        if as_json and "--doctor" in actual:
            report = {"schema": DOCTOR_SCHEMA, "status": "incompatible", "exit_code": 1, "error": str(exc), "checks": []}
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # fail closed with the documented internal-error code
        return invalid_report(f"internal installer failure: {exc}", as_json)


if __name__ == "__main__":
    raise SystemExit(main())
