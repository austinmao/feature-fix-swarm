"""Private, manifest-bound staging for one Claude Code workspace runtime.

The installer profile remains read-only.  A staged runtime gets its own
configuration tree and a private access-only subscription projection.
Only hashes and filesystem identities are returned; credential bytes never
cross the API boundary.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
from typing import Final


STAGE_SCHEMA: Final = "ffs.private-claude-runtime-stage/v1"
STAGE_MANIFEST_NAME: Final = "runtime-stage-manifest.json"
_COPY_ROOTS: Final = ("agents", "gsd-core", "hooks", "lib/feature-fix-swarm", "scripts", "skills")


class ClaudeRuntimeStagingError(ValueError):
    pass


class RetainedClaudeRuntimeNotReusable(ClaudeRuntimeStagingError):
    """A retained private stage is not exact, e.g. a launch consumed its credential."""


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _directory(path: Path, label: str) -> Path:
    try:
        info = path.lstat()
    except OSError as error:
        raise ClaudeRuntimeStagingError(f"{label} is unavailable") from error
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ClaudeRuntimeStagingError(f"{label} is unsafe")
    return path.resolve()


def _regular(path: Path, label: str, *, private: bool = False) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise ClaudeRuntimeStagingError(f"{label} is unavailable") from error
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ClaudeRuntimeStagingError(f"{label} is unsafe")
    if private and (info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
        raise ClaudeRuntimeStagingError(f"{label} is not a private 0600 file")
    return info


def _json(path: Path, label: str) -> dict[str, object]:
    _regular(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ClaudeRuntimeStagingError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise ClaudeRuntimeStagingError(f"{label} must be a JSON object")
    return value


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _identity(path: Path) -> dict[str, object]:
    info = path.lstat()
    return {"path": str(path.resolve()), "device": info.st_dev, "inode": info.st_ino,
            "mode": stat.S_IMODE(info.st_mode)}


def _safe_relative(value: str) -> Path:
    pure = PurePosixPath(value)
    if (not value or pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts)
            or "\0" in value or str(pure) != value):
        raise ClaudeRuntimeStagingError("installer manifest contains an unsafe path")
    return Path(*pure.parts)


def _tree(source: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    if not source.exists():
        return files
    _directory(source, "candidate runtime root")
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        info = path.lstat()
        if path.is_symlink() or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ClaudeRuntimeStagingError(f"candidate runtime contains an unsafe node: {relative}")
        if info.st_uid != os.getuid():
            raise ClaudeRuntimeStagingError(f"candidate runtime contains a foreign node: {relative}")
        if stat.S_ISREG(info.st_mode):
            files[relative] = path
    return files


def _copy(source: Path, destination: Path) -> None:
    info = _regular(source, "candidate runtime file")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.parent.chmod(0o700)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(source.read_bytes())
    except BaseException:
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    destination.chmod(0o700 if info.st_mode & 0o111 else 0o600)


def _settings(source: Path, target: Path, workspace: Path) -> dict[str, object]:
    value = _json(source, "candidate Claude settings")
    # Preserve installer-owned hooks, but remove every ambient execution and
    # network extension.  GSD dispatch reaches its supervisor through the
    # inherited four-variable bridge contract rather than an MCP/plugin.
    allowed = {"hooks": value["hooks"]} if "hooks" in value else {}
    raw = json.dumps(allowed, sort_keys=True)
    rewritten = json.loads(raw.replace(str(source.parent), str(target)))
    rewritten.update({
        "permissions": {
            "defaultMode": "acceptEdits",
            "allow": ["Bash", "Edit", "Glob", "Grep", "Read", "Skill", "Write"],
            "deny": ["Agent", "Task", "WebFetch", "WebSearch"],
        },
        "sandbox": {
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "enableWeakerNestedSandbox": False,
            "network": {"allowedDomains": [], "deniedDomains": ["*"], "allowUnixSockets": []},
            "filesystem": {
                "denyRead": [str(target / ".credentials.json")],
                "denyWrite": [str(target / ".credentials.json")],
            },
        },
    })
    return rewritten


def _access_only_oauth(value: object) -> dict[str, object]:
    """Remove every refresh bearer before material reaches a child runtime."""
    if not isinstance(value, dict):
        raise ClaudeRuntimeStagingError("subscription credential lacks Claude OAuth material")
    access = value.get("accessToken")
    if not isinstance(access, str) or not access:
        raise ClaudeRuntimeStagingError("subscription credential lacks Claude access material")
    allowed = {
        key: value[key]
        for key in ("accessToken", "expiresAt", "scopes", "subscriptionType", "rateLimitTier")
        if key in value
    }
    # Keep the host schema stable while making refresh capability impossible.
    allowed["refreshToken"] = None
    allowed["refreshTokenExpiresAt"] = None
    return allowed


def _encoded(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _credential_bytes(credential_value: dict[str, object]) -> bytes:
    """The exact access-only projection staging writes; reuse derives its expectation from the same bytes."""
    return _encoded({"claudeAiOauth": _access_only_oauth(credential_value["claudeAiOauth"])})


def _source_closure(candidate_home: Path, credential_source: Path, workspace: Path):
    """Validate what a stage is built from: the installed candidate, the workspace and the credential.

    Returns the candidate, the workspace, the credential document, the candidate files to copy
    (relative path to source) and the ``source`` record a stage manifest binds.
    """
    candidate = _directory(Path(candidate_home), "candidate Claude home")
    worktree = _directory(Path(workspace), "workspace")
    credential = Path(credential_source)
    _regular(credential, "subscription credential", private=True)
    credential_value = _json(credential, "subscription credential")
    if not isinstance(credential_value.get("claudeAiOauth"), dict) or not credential_value["claudeAiOauth"]:
        raise ClaudeRuntimeStagingError("subscription credential lacks Claude OAuth material")
    manifest = _json(candidate / "gsd-file-manifest.json", "GSD installer manifest")
    if manifest.get("version") != "1.15.0" or manifest.get("runtime") != "claude":
        raise ClaudeRuntimeStagingError("candidate is not the pinned Claude GSD 1.15.0 runtime")
    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, dict) or not manifest_files:
        raise ClaudeRuntimeStagingError("GSD installer manifest has no file closure")
    for relative, expected in manifest_files.items():
        if not isinstance(relative, str) or not isinstance(expected, str) or len(expected) != 64:
            raise ClaudeRuntimeStagingError("GSD installer manifest is malformed")
        path = candidate / _safe_relative(relative)
        if _digest(path) != expected:
            raise ClaudeRuntimeStagingError(f"candidate installer file has drifted: {relative}")

    files: dict[str, Path] = {}
    for root_name in _COPY_ROOTS:
        root = candidate / root_name
        for relative, path in _tree(root).items():
            files[f"{root_name}/{relative}"] = path
    if not files:
        raise ClaudeRuntimeStagingError("candidate runtime closure is empty")
    source = {"candidate": str(candidate), "candidate_manifest_sha256": _digest(candidate / "gsd-file-manifest.json"),
              "credential_sha256": _digest(credential)}
    return candidate, worktree, credential_value, files, source


def stage_private_claude_runtime(candidate_home: Path, credential_source: Path,
                                 target_home: Path, workspace: Path) -> dict[str, object]:
    """Materialize a private Claude config home from an installed candidate."""
    candidate, worktree, credential_value, files, source = _source_closure(
        candidate_home, credential_source, workspace)
    target = Path(target_home)
    if not target.is_absolute() or target.exists() or target.is_symlink():
        raise ClaudeRuntimeStagingError("target Claude home must be a new absolute path")
    parent = _directory(target.parent, "target Claude home parent")
    target = parent / target.name
    try:
        target.mkdir(mode=0o700)
        target.chmod(0o700)
        for relative, origin in sorted(files.items()):
            _copy(origin, target / relative)
        _copy(candidate / "gsd-file-manifest.json", target / "gsd-file-manifest.json")
        auth = target / ".credentials.json"
        # MCP OAuth grants, refresh bearers, and unrelated credential families
        # never enter the execution runtime.
        auth_fd = os.open(auth, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(auth_fd, "wb") as output:
            output.write(_credential_bytes(credential_value))
        settings = _settings(candidate / "settings.json", target, worktree)
        settings_path = target / "settings.json"
        descriptor = os.open(settings_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(_encoded(settings))
        for directory in (path for path in target.rglob("*") if path.is_dir()):
            directory.chmod(0o700)
        records = {relative: _digest(target / relative) for relative in sorted(files)}
        records.update({"gsd-file-manifest.json": _digest(target / "gsd-file-manifest.json"),
                        "settings.json": _digest(settings_path),
                        ".credentials.json": _digest(auth)})
        result = {
            "schema": STAGE_SCHEMA,
            "source": source,
            "target": {"home": _identity(target), "workspace": _identity(worktree), "files": records},
        }
        stage_path = target / STAGE_MANIFEST_NAME
        fd = os.open(stage_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(result, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
        return result
    except BaseException:
        if target.exists() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
        raise


def _staged_mode(source: Path) -> int:
    return 0o700 if source.lstat().st_mode & 0o111 else 0o600


def _private_directory_chain(path: Path, home: Path) -> None:
    for directory in (path.parent, *path.parent.parents):
        if directory == home:
            return
        info = directory.lstat()
        if (directory.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ClaudeRuntimeStagingError(f"retained staged directory is not private: {directory.name}")


def _retained_stage(candidate_home: Path, credential_source: Path, target: Path,
                    workspace: Path) -> dict[str, object]:
    """Prove a retained stage is still what staging would write now, without writing a byte."""
    home = _directory(target, "retained Claude home")
    if stat.S_IMODE(home.lstat().st_mode) != 0o700:
        raise ClaudeRuntimeStagingError("retained Claude home is not private mode 0700")
    candidate, worktree, credential_value, files, source = _source_closure(
        candidate_home, credential_source, workspace)
    manifest_path = home / STAGE_MANIFEST_NAME
    _regular(manifest_path, "retained stage manifest", private=True)
    manifest = _json(manifest_path, "retained stage manifest")
    bound = manifest.get("target")
    if (manifest.get("schema") != STAGE_SCHEMA or manifest.get("source") != source or not isinstance(bound, dict)
            or bound.get("home") != _identity(home) or bound.get("workspace") != _identity(worktree)):
        raise ClaudeRuntimeStagingError("retained stage does not bind this candidate, credential and workspace")
    modes = {relative: _staged_mode(path) for relative, path in files.items()}
    modes.update({"gsd-file-manifest.json": _staged_mode(candidate / "gsd-file-manifest.json"),
                  "settings.json": 0o600, ".credentials.json": 0o600})
    records = bound.get("files")
    if not isinstance(records, dict) or set(records) != set(modes):
        raise ClaudeRuntimeStagingError("retained stage file inventory is malformed")
    # Every expected digest is derived from the present source, never from the retained manifest,
    # which lives in the same home and could be rewritten together with the bytes it records.
    expected = {relative: _digest(path) for relative, path in files.items()}
    expected.update({
        "gsd-file-manifest.json": _digest(candidate / "gsd-file-manifest.json"),
        "settings.json": hashlib.sha256(_encoded(_settings(candidate / "settings.json", home, worktree))).hexdigest(),
        ".credentials.json": hashlib.sha256(_credential_bytes(credential_value)).hexdigest(),
    })
    for relative, recorded in sorted(records.items()):
        path = home / _safe_relative(relative)
        info = _regular(path, f"retained staged file {relative}")
        if info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != modes[relative]:
            raise ClaudeRuntimeStagingError(f"retained staged file is not private: {relative}")
        _private_directory_chain(path, home)
        if recorded != expected[relative] or _digest(path) != expected[relative]:
            raise ClaudeRuntimeStagingError(f"retained staged file does not match its source: {relative}")
    return manifest


def stage_or_reuse_private_claude_runtime(candidate_home: Path, credential_source: Path,
                                          target_home: Path, workspace: Path) -> dict[str, object]:
    """Create the private stage once, or prove the retained one is still exact.

    A retained target is never repaired or replaced: a runtime whose staged
    credential a launch consumed, whose bytes, modes or links drifted, or whose
    candidate or source credential changed since it was staged, is non-reusable.
    """
    target = Path(target_home)
    if not target.is_absolute():
        raise ClaudeRuntimeStagingError("target Claude home must be an absolute path")
    if not os.path.lexists(target):
        return stage_private_claude_runtime(candidate_home, credential_source, target, workspace)
    try:
        return _retained_stage(Path(candidate_home), Path(credential_source), target, Path(workspace))
    except ClaudeRuntimeStagingError as error:
        raise RetainedClaudeRuntimeNotReusable(str(error)) from error
