"""Stdin-only transport from GSD's wave adapter to the supervised worker channel."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys

try:  # ``python -m`` is preferred; retain the fixed script entry point too.
    from .worker_channel import (
        WorkerChannelRefused, _MAX_WAVE_MANIFEST_BYTES, file_request,
        parse_gsd_wave_manifest, request,
    )
except ImportError:  # pragma: no cover - exercised by installed argv wiring
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from run_state.worker_channel import (
        WorkerChannelRefused, _MAX_WAVE_MANIFEST_BYTES, file_request,
        parse_gsd_wave_manifest, request,
    )


class GsdWaveBridgeRefused(RuntimeError):
    pass


def _refuse(code: str) -> GsdWaveBridgeRefused:
    return GsdWaveBridgeRefused(code)


def _read_stdin() -> bytes:
    raw = sys.stdin.buffer.read(_MAX_WAVE_MANIFEST_BYTES + 1)
    if not 1 <= len(raw) <= _MAX_WAVE_MANIFEST_BYTES:
        raise _refuse("WAVE_MANIFEST_TOO_LARGE")
    return raw


def _directory(parent_fd: int, name: str, *, exact_mode: bool = True) -> int:
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        descriptor = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd,
        )
        info = os.fstat(descriptor)
        mode = stat.S_IMODE(info.st_mode)
        if (info.st_uid != os.getuid() or (mode != 0o700 if exact_mode else mode & 0o022)):
            os.close(descriptor)
            raise _refuse("WAVE_EVIDENCE_DIRECTORY_UNSAFE")
        return descriptor
    except GsdWaveBridgeRefused:
        raise
    except OSError as error:
        raise _refuse("WAVE_EVIDENCE_DIRECTORY_UNSAFE") from error


def _read_at(directory: int, name: str) -> bytes:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > _MAX_WAVE_MANIFEST_BYTES:
                raise _refuse("WAVE_EVIDENCE_UNSAFE")
            raw = os.read(descriptor, _MAX_WAVE_MANIFEST_BYTES + 1)
            if len(raw) > _MAX_WAVE_MANIFEST_BYTES:
                raise _refuse("WAVE_EVIDENCE_UNSAFE")
            return raw
        finally:
            os.close(descriptor)
    except GsdWaveBridgeRefused:
        raise
    except OSError as error:
        raise _refuse("WAVE_EVIDENCE_UNSAFE") from error


def persist_manifest(raw: bytes, cwd: Path) -> tuple[str, str]:
    """Atomically retain canonical stdin evidence below a non-symlink CWD."""
    manifest, canonical = parse_gsd_wave_manifest(raw)
    try:
        root_info = cwd.lstat()
        if (cwd.is_symlink() or not cwd.is_dir() or root_info.st_uid != os.getuid()
                or Path(manifest["orchestrator_root"]).resolve() != cwd.resolve()):
            raise _refuse("WAVE_WORKSPACE_MISMATCH")
        root = os.open(str(cwd), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except GsdWaveBridgeRefused:
        raise
    except OSError as error:
        raise _refuse("WAVE_WORKSPACE_MISMATCH") from error
    planning = requests = None
    digest = hashlib.sha256(canonical).hexdigest()
    name = digest + ".json"
    try:
        # GSD may already own its normal planning directory.  It must be
        # non-symlinked and non-writable by others; the FFS-only child below
        # is always an exact private directory.
        planning = _directory(root, ".planning", exact_mode=False)
        requests = _directory(planning, ".ffs-wave-requests")
        try:
            existing = _read_at(requests, name)
        except GsdWaveBridgeRefused as error:
            if error.args[0] != "WAVE_EVIDENCE_UNSAFE":
                raise
            existing = None
        if existing is not None:
            if existing != canonical:
                raise _refuse("WAVE_EVIDENCE_CONFLICT")
            return f".planning/.ffs-wave-requests/{name}", digest
        temporary = "." + digest + "." + str(os.getpid()) + "." + secrets.token_hex(8)
        descriptor = None
        try:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600, dir_fd=requests,
            )
            os.write(descriptor, canonical)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                os.link(temporary, name, src_dir_fd=requests, dst_dir_fd=requests, follow_symlinks=False)
            except FileExistsError:
                if _read_at(requests, name) != canonical:
                    raise _refuse("WAVE_EVIDENCE_CONFLICT")
            return f".planning/.ffs-wave-requests/{name}", digest
        except GsdWaveBridgeRefused:
            raise
        except OSError as error:
            raise _refuse("WAVE_EVIDENCE_WRITE_FAILED") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=requests)
            except FileNotFoundError:
                pass
    finally:
        if requests is not None:
            os.close(requests)
        if planning is not None:
            os.close(planning)
        os.close(root)


def _scope_from_environment() -> tuple[str, dict]:
    endpoint, encoded = os.environ.get("FFS_WORKER_ENDPOINT"), os.environ.get("FFS_WORKER_SCOPE")
    if (not isinstance(endpoint, str) or not 1 <= len(endpoint.encode()) <= 512 or "\0" in endpoint
            or not isinstance(encoded, str) or not 1 <= len(encoded.encode()) <= 4096):
        raise _refuse("WAVE_CHANNEL_UNAVAILABLE")
    try:
        scope = json.loads(encoded, object_pairs_hook=lambda pairs: _strict_scope(pairs))
    except (ValueError, TypeError) as error:
        raise _refuse("WAVE_CHANNEL_UNAVAILABLE") from error
    expected = {"repository_id", "run_id", "activity_id", "intent_id", "generation", "supervisor_identity"}
    if (not isinstance(scope, dict) or set(scope) != expected
            or isinstance(scope["generation"], bool) or not isinstance(scope["generation"], int)
            or any(not isinstance(scope[key], str) or not 1 <= len(scope[key].encode()) <= 256
                   for key in expected - {"generation", "supervisor_identity"})
            or not isinstance(scope["supervisor_identity"], dict)
            or set(scope["supervisor_identity"]) != {"host_id", "boot_id", "pid", "start_token"}):
        raise _refuse("WAVE_CHANNEL_UNAVAILABLE")
    return endpoint, scope


def _file_channel_from_environment() -> tuple[str, str] | None:
    encoded = os.environ.get("FFS_WORKER_FILE_CHANNEL")
    if encoded is None:
        return None
    if not isinstance(encoded, str) or not 1 <= len(encoded.encode()) <= 8192:
        raise _refuse("WAVE_CHANNEL_UNAVAILABLE")
    try:
        value = json.loads(encoded, object_pairs_hook=lambda pairs: _strict_scope(pairs))
    except (ValueError, TypeError) as error:
        raise _refuse("WAVE_CHANNEL_UNAVAILABLE") from error
    if (not isinstance(value, dict) or set(value) != {"root", "capability"}
            or not isinstance(value["root"], str) or not Path(value["root"]).is_absolute()
            or not isinstance(value["capability"], str)
            or len(value["capability"].encode()) != 43):
        raise _refuse("WAVE_CHANNEL_UNAVAILABLE")
    return value["root"], value["capability"]


def _strict_scope(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate scope key")
        result[key] = value
    return result


def main() -> int:
    if len(sys.argv) != 1:
        raise _refuse("WAVE_BRIDGE_ARGV_FORBIDDEN")
    raw = _read_stdin()
    locator, digest = persist_manifest(raw, Path.cwd())
    endpoint, scope = _scope_from_environment()
    try:
        # A wave reply follows qualification and all plan executions. There
        # is no truthful fixed ten-second deadline for that lifecycle; the
        # supervisor owns execution timeouts and the socket closes on loss.
        file_channel = _file_channel_from_environment()
        arguments = {
            "request_key": "gsd-wave:" + digest, "operation": "gsd-wave-request",
            "body": {"manifest_locator": locator, "manifest_sha256": digest}, "timeout": None,
        }
        response = (
            file_request(file_channel[0], file_channel[1], scope, **arguments)
            if file_channel is not None else request(endpoint, scope, **arguments)
        )
    except (OSError, WorkerChannelRefused) as error:
        raise _refuse(getattr(error, "code", "WAVE_CHANNEL_UNAVAILABLE")) from error
    if response.get("ok") is not True or not isinstance(response.get("result"), dict):
        raise _refuse(str(response.get("code", "WAVE_CONSUMER_UNAVAILABLE")))
    sys.stdout.buffer.write(_canonical_response(response["result"]) + b"\n")
    return 0


def _canonical_response(value: dict) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise _refuse("WAVE_CONSUMER_REPLY_INVALID") from error


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GsdWaveBridgeRefused, WorkerChannelRefused) as error:
        print(getattr(error, "code", str(error)), file=sys.stderr)
        raise SystemExit(78)
