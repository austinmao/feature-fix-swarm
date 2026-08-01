#!/usr/bin/env python3
"""Scope-aware FFS installer, migration, rollback, and doctor.

GSD source artifacts deliberately remain upstream-owned. This installer only
materializes FFS-owned skills and records enough state to undo every mutation.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
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


def cache_root() -> Path:
    return Path.home() / ".cache" / "feature-fix-swarm"


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


class Backup:
    def __init__(self, operation: str, scope: str, project: Path | None = None) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.backup_id = f"{stamp}-{os.getpid()}-{secrets.token_hex(3)}"
        self.directory = cache_root() / "backups" / self.backup_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self.entries: list[dict[str, Any]] = []
        self.seen: set[str] = set()
        self.operation = operation
        self.scope = scope
        self.project = project

    def before(self, path: Path) -> None:
        absolute = str(path.absolute())
        if absolute in self.seen:
            return
        self.seen.add(absolute)
        entry: dict[str, Any] = {"path": absolute, "before": fingerprint(path)}
        if lexists(path):
            metadata = path.lstat()
            entry["mode"] = f"{stat.S_IMODE(metadata.st_mode):04o}"
            if stat.S_ISLNK(metadata.st_mode):
                entry["type"] = "symlink"
                entry["target"] = os.readlink(path)
            elif stat.S_ISDIR(metadata.st_mode):
                entry["type"] = "directory"
            elif stat.S_ISREG(metadata.st_mode):
                entry["type"] = "file"
                entry["sha256"] = sha256_file(path)
            else:
                entry["type"] = "other"
            backup_rel = f"objects/{len(self.entries):04d}"
            copy_path(path, self.directory / backup_rel)
            entry["backup"] = backup_rel
        self.entries.append(entry)

    def assume_created(self, path: Path) -> None:
        """Record an upstream-manifest path proven absent from the old manifest."""
        absolute = str(path.absolute())
        if absolute in self.seen:
            return
        self.seen.add(absolute)
        self.entries.append({"path": absolute, "before": "missing"})

    def finish(self) -> None:
        for entry in self.entries:
            entry["after"] = fingerprint(Path(entry["path"]))
        atomic_json(
            self.directory / "manifest.json",
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
        for entry in reversed(self.entries):
            destination = Path(entry["path"])
            if lexists(destination):
                remove_path(destination)
            backup_rel = entry.get("backup")
            if backup_rel:
                copy_path(self.directory / backup_rel, destination)


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


def manifest_path(scope: str, project: Path | None) -> Path:
    if scope == "project":
        assert project is not None
        return safe_project_destination(
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


def replace_tree(source: Path, destination: Path, backup: Backup) -> None:
    if fingerprint(source) == fingerprint(destination):
        return
    backup.before(destination)
    if lexists(destination):
        remove_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True)


def replace_link(destination: Path, target: Path, backup: Backup) -> None:
    link_text = os.path.relpath(target, destination.parent)
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
    for root in roots:
        if project is not None:
            relative = root.absolute().relative_to(project.resolve())
            safe_project_destination(project, relative)
        if not root.exists():
            continue
        for skill_dir in sorted(root.iterdir()):
            if project is not None:
                # Recheck after every directory iteration so a concurrent
                # ancestor swap cannot silently redirect later deletions.
                relative = root.absolute().relative_to(project.resolve())
                safe_project_destination(project, relative)
            if skill_dir.name not in legacy_names:
                continue
            if skill_dir.is_symlink():
                safe = False
                if skill_dir.exists() and skill_dir.is_dir():
                    target_files = [item for item in skill_dir.rglob("*") if item.is_file()]
                    safe = bool(target_files) and all(
                        sha256_file(item)
                        in known.get(f"skills/{skill_dir.name}/{item.relative_to(skill_dir).as_posix()}", set())
                        for item in target_files
                    )
                elif not skill_dir.exists():
                    safe = True
                if safe:
                    backup.before(skill_dir)
                    remove_path(skill_dir)
                else:
                    preserved.append(skill_dir)
                continue
            if not skill_dir.is_dir():
                preserved.append(skill_dir)
                continue
            for item in sorted(skill_dir.rglob("*"), reverse=True):
                if item.is_dir() and not item.is_symlink():
                    with contextlib.suppress(OSError):
                        item.rmdir()
                    continue
                if item.is_symlink():
                    if not item.exists():
                        backup.before(item)
                        item.unlink()
                    else:
                        preserved.append(item)
                    continue
                rel = f"skills/{skill_dir.name}/{item.relative_to(skill_dir).as_posix()}"
                if item.is_file() and sha256_file(item) in known.get(rel, set()):
                    backup.before(item)
                    item.unlink()
                else:
                    preserved.append(item)
            with contextlib.suppress(OSError):
                skill_dir.rmdir()
        with contextlib.suppress(OSError):
            root.rmdir()
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
    destination_manifest = manifest_path(scope, project)
    previous = read_json(destination_manifest)
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
    for path, expected, broken_ok in planned:
        if scope == "project":
            assert project is not None
            try:
                relative = path.absolute().relative_to(project.resolve())
            except ValueError as exc:
                raise ActionableError(f"project install path escapes checkout: {path}") from exc
            safe_project_destination(project, relative)
        ensure_replaceable(path, expected, managed, broken_link_ok=broken_ok)

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
                replace_tree(source_dir, target, backup)
                for host in (".agents", ".claude"):
                    link = safe_project_destination(project, Path(host) / "skills" / name)
                    replace_link(link, target, backup)
            if prompt_master:
                canonical = safe_project_destination(
                    project, Path(".agents") / "skills" / "prompt-master"
                )
                replace_tree(prompt_master, canonical, backup)
                claude_link = safe_project_destination(
                    project, Path(".claude") / "skills" / "prompt-master"
                )
                replace_link(claude_link, canonical, backup)
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
        backup.before(destination_manifest)
        atomic_json(
            destination_manifest,
            {
                "schema": INSTALL_SCHEMA,
                "version": source_version(source),
                "scope": scope,
                "installed_at": datetime.now(timezone.utc).isoformat(),
                "source": str(source),
                "paths": paths,
                "gsd": {
                    "owner": "upstream-installer",
                    "version": GSD_VERSION,
                    "profiles": {"claude": "full", "codex": "full"},
                },
            },
        )
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
        if not lexists(destination):
            continue
        if fingerprint(destination) != expected:
            preserved.append(destination)
            continue
        backup.before(destination)
        remove_path(destination)
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
    directory = cache_root() / "backups" / backup_id
    manifest = read_json(directory / "manifest.json")
    if not manifest or manifest.get("schema") != BACKUP_SCHEMA:
        raise ActionableError(f"backup not found or invalid: {backup_id}")
    project_text = manifest.get("project_dir")
    project = Path(project_text) if project_text else None
    lock_path = project_lock_path(project) if project else cache_root() / "setup.lock"
    with exclusive_lock(lock_path):
        conflicts: list[Path] = []
        for entry in reversed(manifest.get("entries", [])):
            destination = Path(entry["path"])
            current = fingerprint(destination)
            if current != entry.get("after"):
                conflicts.append(destination)
                continue
            if lexists(destination):
                remove_path(destination)
            backup_rel = entry.get("backup")
            if backup_rel:
                copy_path(directory / backup_rel, destination)
        if conflicts:
            for destination in conflicts:
                print(f"preserved path changed since backup: {destination}", file=sys.stderr)
            return 1
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
    if not target.is_dir():
        raise InvocationError(f"reconciliation target is not a directory: {target}")
    relative_files = [
        "scripts/gsd/gsd-run.sh",
        "scripts/gsd/requirement-ownership-gate.sh",
        "scripts/gsd/adversary-host.sh",
        "scripts/gsd/run-bounded.sh",
        "scripts/gsd/model-equivalents.sh",
        "scripts/gsd/codex-model-sync.sh",
        "scripts/gsd/codex-runtime-bundle.py",
        "scripts/gsd/consume-danger-grant.py",
"scripts/gsd/hash-ffs-skills.py",
"scripts/gsd/sanitize-codex-config.py",
"scripts/gsd/sync-codex-auth.py",
        "scripts/gsd/review-gate-command.sh",
        "scripts/hooks/cli-hang-guard.sh",
        "scripts/hooks/credential-output-guard.sh",
        "lib/model_requests.py",
    ]
    for relative in relative_files:
        source_file = source / relative
        target_file = target / relative
        target_file.parent.mkdir(parents=True, exist_ok=True)
        if source_file.absolute() != target_file.absolute():
            shutil.copy2(source_file, target_file)
        target_file.chmod(target_file.stat().st_mode | 0o111)
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
