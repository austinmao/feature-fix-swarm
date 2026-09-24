"""Fail-closed construction of a private, one-worktree Codex runtime.

The source profile is a candidate produced by the pinned GSD installer.  It is
never used in place: this module copies a verified closure into a newly-created
private directory, rewrites the few configuration references which are meant to
be private, and records non-secret staging evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Final, NoReturn

from host_capabilities import CapabilityError, render_runtime_config


GSD_VERSION: Final = "1.14.0"
STAGE_MANIFEST_NAME: Final = "runtime-stage-manifest.json"
_GSD_SKILL = re.compile(r"^gsd-[a-z0-9][a-z0-9-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONFIG_SUFFIXES: Final = frozenset((".cfg", ".conf", ".ini", ".json", ".toml", ".yaml", ".yml"))
_BUNDLE_ROOTS: Final = ("agents", "gsd-core", "scripts", "hooks")
_SESSION_START_HOOK: Final = "hooks/gsd-check-update.js"
_SESSION_START_MARKER: Final = "// ffs-supervised-session-start-observer/v1"
# A source-root spelling counts only as a complete path root: followed by a
# (JSON-escaped) separator, the end, or a character which cannot extend a path.
_ROOT_END: Final = r"""(?=/|\\/|$|["'\s:,)\]}])"""


class RuntimeStagingError(ValueError):
    """The candidate profile cannot safely form an isolated runtime."""


class RetainedRuntimeNotReusable(RuntimeStagingError):
    """A retained private stage is not exact, e.g. its qualification consumed it."""


def _fail(message: str) -> NoReturn:
    raise RuntimeStagingError(message)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _real_directory(path: Path, label: str) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise RuntimeStagingError(f"{label} is unavailable") from exc
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
        _fail(f"{label} must be a real directory")
    return path.resolve()


def _regular(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise RuntimeStagingError(f"{label} is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        _fail(f"{label} must be a regular non-symlink file")
    return info


def _walk_regular_tree(root: Path, label: str) -> dict[str, Path]:
    """Return a complete tree inventory, refusing every link and special node."""
    _real_directory(root, label)
    files: dict[str, Path] = {}

    def visit(directory: Path, relative: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise RuntimeStagingError(f"{label} is unreadable") from exc
        for entry in entries:
            child_relative = relative / entry.name
            child = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise RuntimeStagingError(f"{label} contains an unreadable member") from exc
            if stat.S_ISLNK(info.st_mode):
                _fail(f"{label} contains a symlink: {child_relative.as_posix()}")
            if stat.S_ISDIR(info.st_mode):
                visit(child, child_relative)
            elif stat.S_ISREG(info.st_mode):
                files[child_relative.as_posix()] = child
            else:
                _fail(f"{label} contains a special file: {child_relative.as_posix()}")

    visit(root, Path())
    if not files:
        _fail(f"{label} is empty")
    return files


def _json_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("GSD manifest contains duplicate JSON keys")
        result[key] = value
    return result


def _load_manifest(path: Path) -> dict[str, str]:
    _regular(path, "GSD manifest")
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_json_no_duplicates)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RuntimeStagingError("GSD manifest is not valid UTF-8 JSON") from exc
    files = value.get("files") if isinstance(value, dict) else None
    if value.get("version") != GSD_VERSION or not isinstance(files, dict) or not files:
        _fail(f"GSD manifest must declare version {GSD_VERSION} and nonempty files")
    checked: dict[str, str] = {}
    for raw, expected in files.items():
        if not isinstance(raw, str) or not isinstance(expected, str) or _SHA256.fullmatch(expected) is None:
            _fail("GSD manifest has an unsafe path or hash")
        relative = Path(raw)
        if (not raw or relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 2
                or relative.parts[0] not in {*_BUNDLE_ROOTS, "skills"}):
            _fail(f"GSD manifest has an unsupported path: {raw}")
        if relative.parts[0] == "skills" and (
                len(relative.parts) < 3 or _GSD_SKILL.fullmatch(relative.parts[1]) is None):
            _fail(f"GSD manifest has a non-GSD skill path: {raw}")
        checked[relative.as_posix()] = expected
    return checked


def _validate_auth(path: Path) -> tuple[os.stat_result, str]:
    info = _regular(path, "source auth")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        _fail("source auth must be current-user owned, unlinked, and mode 0600")
    if not 1 <= info.st_size <= 2 * 1024 * 1024:
        _fail("source auth has an unsafe size")
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_json_no_duplicates)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RuntimeStagingError("source auth is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        _fail("source auth must be a JSON object")
    return info, _sha256(path)


def _access_only_auth_bytes(path: Path) -> bytes:
    """Project Codex OAuth without a refresh-capable bearer.

    The installed profile remains the sole refresh authority. Children receive
    only the current access/id tokens and account binding. A missing or expired
    access token is a capability failure; it is never repaired inside a worker.
    """
    _validate_auth(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_json_no_duplicates)
        tokens = value.get("tokens")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RuntimeStagingError("source auth is not valid UTF-8 JSON") from exc
    if (
        not isinstance(value, dict) or not isinstance(tokens, dict)
        or not isinstance(tokens.get("access_token"), str) or not tokens["access_token"]
        or not isinstance(tokens.get("id_token"), str) or not tokens["id_token"]
        or not isinstance(tokens.get("account_id"), str) or not tokens["account_id"]
    ):
        _fail("source auth lacks access-only Codex OAuth material")
    projected = {
        "auth_mode": value.get("auth_mode"),
        "OPENAI_API_KEY": value.get("OPENAI_API_KEY"),
        "tokens": {
            "id_token": tokens["id_token"],
            "access_token": tokens["access_token"],
            # Codex 0.154 rejects a null refresh field before using the still
            # valid access token. This fixed non-secret sentinel preserves the
            # host schema without granting refresh capability.
            "refresh_token": "ffs-access-only-no-refresh",
            "account_id": tokens["account_id"],
        },
        "last_refresh": value.get("last_refresh"),
    }
    return (json.dumps(projected, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _write_private_bytes(destination: Path, content: bytes) -> None:
    try:
        descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600,
        )
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
        destination.chmod(0o600)
    except OSError as exc:
        raise RuntimeStagingError("cannot create private runtime file") from exc


def _identity(path: Path) -> dict[str, object]:
    info = path.lstat()
    return {
        "path": str(path.resolve()), "device": info.st_dev, "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
    }


def _file_record(path: Path, label: str) -> dict[str, object]:
    info = _regular(path, label)
    if info.st_uid != os.getuid() or info.st_nlink != 1:
        _fail(f"{label} is not a private unlinked file")
    return {"identity": {**_identity(path), "links": info.st_nlink}, "sha256": _sha256(path)}


def _copy_regular(source: Path, destination: Path) -> None:
    info = _regular(source, "source runtime file")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.parent.chmod(0o700)
    try:
        content = source.read_bytes()
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
        # The profile is private, so preserve whether a file can execute while
        # deliberately dropping group/world readability and executability.
        destination.chmod(0o700 if info.st_mode & 0o111 else 0o600)
    except OSError as exc:
        raise RuntimeStagingError("cannot create private runtime file") from exc


def _is_config_like(relative: Path) -> bool:
    return relative.suffix.lower() in _CONFIG_SUFFIXES


def _source_spellings(source_home: Path, source_skills: Path) -> tuple[tuple[Path, Path], ...]:
    """(spelling, canonical root) for each source root, plus $HOME aliases resolving to it.

    With a symlinked ``~/.codex`` the installer writes hook commands under the
    $HOME alias while staging only accepts the canonical source path.
    """
    spellings = [(source_skills, source_skills), (source_home, source_home)]
    for alias, canonical in ((Path.home() / ".agents" / "skills", source_skills),
                             (Path.home() / ".codex", source_home)):
        if alias != canonical and alias.resolve() == canonical:
            spellings.append((alias, canonical))
    return tuple(spellings)


def _rewritten_config_text(text: str, source_home: Path, source_skills: Path,
                           target_home: Path, target_skills: Path) -> str:
    targets = {source_skills: str(target_skills), source_home: str(target_home)}
    for spelling, canonical in _source_spellings(source_home, source_skills):
        old, new = str(spelling), targets[canonical]
        for before, after in ((old, new), (old.replace("/", r"\/"), new.replace("/", r"\/"))):
            text = re.sub(re.escape(before) + _ROOT_END, lambda _match, value=after: value, text)
    return text


def _rewrite_config_paths(path: Path, source_home: Path, source_skills: Path,
                          target_home: Path, target_skills: Path) -> None:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return
    rewritten = _rewritten_config_text(text, source_home, source_skills, target_home, target_skills)
    if rewritten != text:
        try:
            path.write_text(rewritten, encoding="utf-8")
            path.chmod(0o700 if path.stat().st_mode & 0o100 else 0o600)
        except OSError as exc:
            raise RuntimeStagingError("cannot rewrite private runtime configuration") from exc


def _instrumented_session_start_bytes(raw: bytes) -> bytes:
    """Chain the pinned GSD SessionStart hook with nonce-bound observation."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeStagingError("GSD SessionStart hook is not UTF-8") from exc
    anchor = "const fs = require('fs');"
    if text.count(anchor) != 1 or _SESSION_START_MARKER in text:
        _fail("GSD SessionStart hook cannot be safely instrumented")
    observer = "\n".join((
        _SESSION_START_MARKER,
        "const ffsObservationChunks = [];",
        "process.stdin.on('data', chunk => ffsObservationChunks.push(chunk));",
        "process.stdin.on('end', () => {",
        "  try {",
        "    const payload = JSON.parse(Buffer.concat(ffsObservationChunks).toString() || '{}');",
        "    const output = process.env.FFS_HOOK_OBSERVATION;",
        "    const nonce = process.env.FFS_HOOK_NONCE;",
        "    if (output && nonce && payload.hook_event_name === 'SessionStart')",
        "      fs.appendFileSync(output, nonce + ' SessionStart\\n', { mode: 0o600 });",
        "  } catch (_) {}",
        "});",
    ))
    return text.replace(anchor, anchor + "\n" + observer, 1).encode("utf-8")


def _instrument_session_start_hook(target: Path) -> None:
    hook = target / _SESSION_START_HOOK
    if not hook.is_file() or hook.is_symlink():
        return
    try:
        hook.write_bytes(_instrumented_session_start_bytes(hook.read_bytes()))
        hook.chmod(0o700)
    except OSError as exc:
        raise RuntimeStagingError("cannot instrument GSD SessionStart hook") from exc


def _assert_no_source_reference(target: Path, source_home: Path, source_skills: Path) -> None:
    needles = tuple(re.compile(re.escape(value.encode()) + _ROOT_END.encode())
                    for spelling, _ in _source_spellings(source_home, source_skills)
                    for value in (str(spelling), str(spelling).replace("/", r"\/")))
    for path in sorted(target.rglob("*")):
        if path.is_dir():
            continue
        _regular(path, "staged runtime file")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise RuntimeStagingError("cannot validate staged runtime") from exc
        if any(needle.search(content) for needle in needles):
            _fail(f"staged runtime still references the source profile: {path.relative_to(target).as_posix()}")


def _remove_created_target(target: Path) -> None:
    """Best-effort cleanup of the directory this invocation created itself."""
    try:
        if target.exists() and not target.is_symlink():
            shutil.rmtree(target)
    except OSError:
        pass


@dataclass(frozen=True)
class _SourceClosure:
    home: Path
    skills: Path
    hashes: dict[str, str]
    bundle_files: dict[str, Path]
    skill_files: dict[str, Path]

    def source_file(self, relative: str) -> Path:
        if relative == "auth.json" or relative == "hooks.json" or relative == "gsd-file-manifest.json":
            return self.home / relative
        source = self.bundle_files.get(relative) or self.skill_files.get(relative)
        if source is None:
            _fail(f"source closure has no file record: {relative}")
        return source

    def staged_digest_and_mode(self, relative: str, target: Path) -> tuple[str, int]:
        """Derive copied bytes from the present source, never from stage evidence."""
        source = self.source_file(relative)
        info = _regular(source, "source closure file")
        mode = 0o700 if info.st_mode & 0o111 else 0o600
        if relative == "auth.json":
            return hashlib.sha256(_access_only_auth_bytes(source)).hexdigest(), 0o600
        if relative == _SESSION_START_HOOK:
            return hashlib.sha256(_instrumented_session_start_bytes(source.read_bytes())).hexdigest(), 0o700
        if relative != "gsd-file-manifest.json" and _is_config_like(Path(relative)):
            try:
                content = _rewritten_config_text(
                    source.read_text(encoding="utf-8"), self.home, self.skills, target, target / "skills",
                ).encode("utf-8")
            except UnicodeDecodeError:
                content = source.read_bytes()
            return hashlib.sha256(content).hexdigest(), mode
        return _sha256(source), mode


def _source_closure(template_codex_home: Path) -> _SourceClosure:
    """Validate and inventory the only source closure eligible for staging."""
    source_home = _real_directory(Path(template_codex_home), "source Codex home")
    source_skills = _real_directory(source_home.parent / ".agents" / "skills", "source GSD skills")
    manifest_source = source_home / "gsd-file-manifest.json"
    manifest_files = _load_manifest(manifest_source)
    _regular(source_home / "hooks.json", "source hooks configuration")
    _validate_auth(source_home / "auth.json")

    bundle_files: dict[str, Path] = {}
    for name in _BUNDLE_ROOTS:
        for relative, source in _walk_regular_tree(source_home / name, f"source {name} bundle").items():
            bundle_files[f"{name}/{relative}"] = source
    skill_files: dict[str, Path] = {}
    for relative, source in _walk_regular_tree(source_skills, "source GSD skills").items():
        parts = Path(relative).parts
        if parts and _GSD_SKILL.fullmatch(parts[0]):
            skill_files[f"skills/{relative}"] = source
    if not skill_files:
        _fail("source GSD skills are missing")

    for relative, expected in manifest_files.items():
        source = skill_files.get(relative) if relative.startswith("skills/") else bundle_files.get(relative)
        if source is None or _sha256(source) != expected:
            _fail(f"GSD manifest closure is missing or changed: {relative}")
    for relative in (*bundle_files, *skill_files):
        if not relative.startswith("hooks/") and relative not in manifest_files:
            _fail(f"GSD manifest closure does not own: {relative}")
    hashes = {
        "auth.json": _sha256(source_home / "auth.json"),
        "hooks.json": _sha256(source_home / "hooks.json"),
        "gsd-file-manifest.json": _sha256(manifest_source),
        **{relative: _sha256(path) for relative, path in sorted(bundle_files.items())},
        **{relative: _sha256(path) for relative, path in sorted(skill_files.items())},
    }
    return _SourceClosure(source_home, source_skills, hashes, bundle_files, skill_files)


def _load_stage_manifest(path: Path) -> dict[str, object]:
    _regular(path, "retained stage manifest")
    info = path.lstat()
    if info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
        _fail("retained stage manifest is not private mode 0600")
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_json_no_duplicates)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RuntimeStagingError("retained stage manifest is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or set(value) != {"schema", "source_hashes", "target"}:
        _fail("retained stage manifest has an invalid shape")
    if value["schema"] != "ffs.private-codex-runtime-stage/v1":
        _fail("retained stage manifest has an unknown schema")
    return value


def _same_identity(value: object, path: Path, label: str) -> None:
    if not isinstance(value, dict) or value != _identity(path):
        _fail(f"retained {label} identity has drifted")


def _validate_private_runtime_tree(target: Path) -> dict[str, Path]:
    _real_directory(target, "retained target Codex home")
    root = target.lstat()
    if root.st_uid != os.getuid() or stat.S_IMODE(root.st_mode) != 0o700:
        _fail("retained target Codex home is not private mode 0700")
    files = _walk_regular_tree(target, "retained target Codex home")
    for path in target.rglob("*"):
        info = path.lstat()
        if path.is_dir():
            if path.is_symlink() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                _fail("retained target Codex home contains an unsafe directory")
        elif path.is_file():
            if path.is_symlink() or info.st_uid != os.getuid() or info.st_nlink != 1:
                _fail("retained target Codex home contains an unsafe file")
        else:
            _fail("retained target Codex home contains a special file")
    return {relative: target / relative for relative in files}


def validate_staged_private_codex_runtime(
    target: Path, workspace: Path, *, allow_additional_evidence: bool = False,
) -> dict[str, object]:
    """Prove a staged runtime's manifest, owned files, GSD closure, and policy.

    This is the consumer-side validation used immediately before qualification.
    It does not need the installer template, but it does require every retained
    byte to match the private staging manifest and the embedded pinned GSD
    manifest.  Source-template equivalence remains the stronger reuse check.
    """
    target = _real_directory(Path(target), "staged target Codex home")
    workspace = _real_directory(Path(workspace), "staged runtime workspace")
    files = _validate_private_runtime_tree(target)
    manifest = _load_stage_manifest(target / STAGE_MANIFEST_NAME)
    source_hashes = manifest["source_hashes"]
    target_data = manifest["target"]
    if (
        not isinstance(source_hashes, dict) or not source_hashes
        or any(not isinstance(path, str) or not isinstance(digest, str)
               or _SHA256.fullmatch(digest) is None
               for path, digest in source_hashes.items())
        or not {"auth.json", "hooks.json", "gsd-file-manifest.json"}.issubset(source_hashes)
    ):
        _fail("retained stage source closure is malformed")
    if not isinstance(target_data, dict) or set(target_data) != {"home", "workspace", "config_sha256", "files"}:
        _fail("retained stage target record is malformed")
    _same_identity(target_data["home"], target, "target home")
    _same_identity(target_data["workspace"], workspace, "workspace")
    records = target_data["files"]
    expected_paths = set(source_hashes) | {"config.toml"}
    if not isinstance(records, dict) or set(records) != expected_paths:
        _fail("retained stage file inventory is malformed")
    owned_paths = expected_paths | {STAGE_MANIFEST_NAME}
    if (not owned_paths.issubset(files)
            or (not allow_additional_evidence and set(files) != owned_paths)):
        _fail("retained stage contains an unowned or missing file")
    for relative in sorted(expected_paths):
        if records[relative] != _file_record(target / relative, f"retained staged file {relative}"):
            _fail(f"retained staged file has drifted: {relative}")
    config = target / "config.toml"
    if target_data["config_sha256"] != _sha256(config):
        _fail("retained strict runtime config has drifted")
    try:
        from host_capabilities import _parse_toml, _runtime_policy
        parsed = _parse_toml(config.read_text(encoding="utf-8"))
        expected_policy = _runtime_policy(workspace, "workspace-write", False, [str(workspace)])
    except (CapabilityError, OSError, UnicodeDecodeError) as exc:
        raise RuntimeStagingError("retained strict runtime config is invalid") from exc
    if parsed != expected_policy:
        _fail("retained strict runtime config is not one-worktree no-network policy")
    _validate_auth(target / "auth.json")

    # The installed manifest is itself stage-owned and must describe the exact
    # pinned GSD closure now present below this runtime home.
    installed = _load_manifest(target / "gsd-file-manifest.json")
    manifest_owned = set(installed) | {"auth.json", "hooks.json", "gsd-file-manifest.json"}
    extras = set(source_hashes) - manifest_owned
    if (not manifest_owned.issubset(source_hashes)
            or any(not relative.startswith("hooks/") for relative in extras)):
        _fail("retained stage source closure differs from the pinned GSD manifest")
    for relative, digest in {**installed, **{name: source_hashes[name] for name in extras}}.items():
        path = target / relative
        if relative not in records or not path.is_file() or path.is_symlink():
            _fail(f"retained GSD closure is missing: {relative}")
        if source_hashes[relative] != digest:
            _fail(f"retained GSD source closure has drifted: {relative}")
        # Staging may rewrite only config-like files to replace installer-home
        # paths with the private runtime paths. Every other GSD byte remains
        # identical to the pinned installed manifest.
        if relative != _SESSION_START_HOOK and not _is_config_like(Path(relative)) and _sha256(path) != digest:
            _fail(f"retained GSD source closure has drifted: {relative}")
    return manifest


def _validate_reusable_runtime(closure: _SourceClosure, target: Path, workspace: Path) -> dict[str, object]:
    """Validate a retained stage without writing a byte or repairing drift."""
    if target.is_symlink() or not target.exists():
        _fail("retained target Codex home is unavailable")
    auth = target / "auth.json"
    if not os.path.lexists(auth):
        _fail("staged auth has been revoked and runtime is not reusable")
    manifest = validate_staged_private_codex_runtime(target, workspace)
    source_hashes = manifest["source_hashes"]
    target_data = manifest["target"]
    if not isinstance(source_hashes, dict) or source_hashes != closure.hashes:
        _fail("retained stage does not bind this exact source closure")
    records = target_data["files"]
    expected_paths = set(closure.hashes) | {"config.toml"}
    for relative in sorted(expected_paths):
        expected = records[relative]
        actual = _file_record(target / relative, f"retained staged file {relative}")
        if expected != actual:
            _fail(f"retained staged file has drifted: {relative}")
        if relative != "config.toml":
            expected_digest, expected_mode = closure.staged_digest_and_mode(relative, target)
            identity = actual["identity"]
            if actual["sha256"] != expected_digest or identity.get("mode") != expected_mode:
                _fail(f"retained staged file no longer matches the source closure: {relative}")
    _assert_no_source_reference(target, closure.home, closure.skills)
    return manifest


def stage_private_codex_runtime(template_codex_home: Path, target_home: Path, worktree: Path) -> dict[str, object]:
    """Stage a source GSD profile in a brand-new private runtime directory.

    The output manifest intentionally contains paths, identities, and digests
    only.  It never serializes the copied authentication content.
    """
    closure = _source_closure(template_codex_home)
    source_home, source_skills = closure.home, closure.skills
    workspace = _real_directory(Path(worktree), "worktree")
    requested_target = Path(target_home)
    if not requested_target.is_absolute():
        _fail("target Codex home must be absolute")
    if requested_target.is_symlink() or requested_target.exists():
        _fail("target Codex home must not already exist or be a symlink")
    target_parent = _real_directory(requested_target.parent, "target Codex home parent")
    target = target_parent / requested_target.name
    if target != requested_target:
        _fail("target Codex home parent changed during validation")

    try:
        target.mkdir(mode=0o700)
        target.chmod(0o700)
        info = target.lstat()
        if target.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            _fail("new target Codex home is not private mode 0700")

        for relative, source in sorted(closure.bundle_files.items()):
            _copy_regular(source, target / relative)
        for relative, source in sorted(closure.skill_files.items()):
            _copy_regular(source, target / relative)
        _copy_regular(source_home / "gsd-file-manifest.json", target / "gsd-file-manifest.json")
        _copy_regular(source_home / "hooks.json", target / "hooks.json")
        _write_private_bytes(target / "auth.json", _access_only_auth_bytes(source_home / "auth.json"))
        for directory in (path for path in target.rglob("*") if path.is_dir()):
            directory.chmod(0o700)

        for path in sorted(target.rglob("*")):
            if path.is_file() and not path.is_symlink() and path.name != "gsd-file-manifest.json" and _is_config_like(path.relative_to(target)):
                _rewrite_config_paths(path, source_home, source_skills, target, target / "skills")
        _instrument_session_start_hook(target)
        try:
            render_runtime_config(target / "config.toml", workspace, "workspace-write", False, [str(workspace)])
        except CapabilityError as exc:
            raise RuntimeStagingError("cannot render strict private runtime configuration") from exc
        (target / "config.toml").chmod(0o600)
        _assert_no_source_reference(target, source_home, source_skills)

        manifest = {
            "schema": "ffs.private-codex-runtime-stage/v1",
            "source_hashes": closure.hashes,
            "target": {
                "home": _identity(target), "workspace": _identity(workspace),
                "config_sha256": _sha256(target / "config.toml"),
                "files": {
                    relative: _file_record(target / relative, f"staged file {relative}")
                    for relative in sorted(set(closure.hashes) | {"config.toml"})
                },
            },
        }
        manifest_path = target / STAGE_MANIFEST_NAME
        encoded = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        descriptor = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
        manifest_path.chmod(0o600)
        return manifest
    except Exception:
        _remove_created_target(target)
        raise


def stage_or_reuse_private_codex_runtime(template_codex_home: Path, target_home: Path,
                                         worktree: Path) -> dict[str, object]:
    """Create a private stage once, or prove a retained stage is still exact.

    Existing targets are never repaired or replaced.  In particular, a runtime
    whose private ``auth.json`` was consumed by a prior launch is deliberately
    non-reusable and must be staged under a fresh target path.
    """
    requested_target = Path(target_home)
    if not requested_target.is_absolute():
        _fail("target Codex home must be absolute")
    if not os.path.lexists(requested_target):
        return stage_private_codex_runtime(template_codex_home, requested_target, worktree)
    closure = _source_closure(template_codex_home)
    workspace = _real_directory(Path(worktree), "worktree")
    target_parent = _real_directory(requested_target.parent, "target Codex home parent")
    target = target_parent / requested_target.name
    if target != requested_target:
        _fail("target Codex home parent changed during validation")
    try:
        return _validate_reusable_runtime(closure, target, workspace)
    except RuntimeStagingError as exc:
        raise RetainedRuntimeNotReusable(str(exc)) from exc
