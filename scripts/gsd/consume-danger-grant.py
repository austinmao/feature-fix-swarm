#!/usr/bin/env python3
"""Issue or atomically consume a trusted, user-global danger grant."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import time


MAX_TTL_SECONDS = 72 * 60 * 60
ACTION = "sandbox:danger-full-access"
SCHEMA = "ffs.danger-grants/v1"
RUN_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
SKILL = re.compile(r"^gsd-[a-z0-9][a-z0-9-]*$")


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def secure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise ValueError(f"danger grant parent must not be a symlink: {path.parent}")


def validate_store(path: Path, *, allow_missing: bool) -> None:
    if not os.path.lexists(path):
        if allow_missing:
            return
        raise ValueError(f"trusted danger grant store is missing: {path}")
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"danger grant store must be a regular file: {path}")
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError(f"danger grant store must be owned by the current user with mode 0600: {path}")


def load(path: Path, *, allow_missing: bool = False) -> dict:
    validate_store(path, allow_missing=allow_missing)
    if not path.exists():
        return {"schema": SCHEMA, "grants": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"danger grant store is invalid: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("grants"), dict):
        raise ValueError(f"danger grant store must use schema {SCHEMA}")
    return data


def save_atomic(path: Path, data: dict) -> None:
    secure_parent(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".danger-grants-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class StoreLock:
    def __init__(self, store: Path) -> None:
        self.path = store.with_name(store.name + ".lock")
        self.handle = None

    def __enter__(self) -> None:
        secure_parent(self.path)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        os.chmod(fd, 0o600)
        self.handle = os.fdopen(fd, "r+")
        fcntl.flock(self.handle, fcntl.LOCK_EX)

    def __exit__(self, *_args: object) -> None:
        assert self.handle is not None
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()


def binding(common_dir_raw: str, skill: str, network_mode: str) -> dict:
    common_dir = Path(common_dir_raw)
    if not common_dir.is_absolute() or not common_dir.is_dir() or common_dir.is_symlink():
        raise ValueError("git common directory must be an absolute regular directory")
    canonical = common_dir.resolve(strict=True)
    if not SKILL.fullmatch(skill):
        raise ValueError("skill binding must be a canonical gsd-* skill name")
    if network_mode not in {"none", "enabled"}:
        raise ValueError("network binding must be none or enabled")
    identity = hashlib.sha256(str(canonical).encode()).hexdigest()
    return {
        "git_common_dir": str(canonical),
        "repo_identity": identity,
        "skill": skill,
        "network_mode": network_mode,
        "sandbox_action": ACTION,
    }


def issue(
    store: Path,
    run_id: str,
    ttl_hours: str,
    common_dir: str,
    skill: str,
    network_mode: str,
) -> int:
    if not RUN_ID.fullmatch(run_id):
        return fail("run id contains unsupported characters or exceeds 128 bytes")
    try:
        ttl_seconds = float(ttl_hours) * 60 * 60
    except ValueError:
        return fail("ttl-hours must be numeric")
    if not 0 < ttl_seconds <= MAX_TTL_SECONDS:
        return fail("ttl-hours must be greater than zero and no more than 72")
    try:
        exact_binding = binding(common_dir, skill, network_mode)
        with StoreLock(store):
            data = load(store, allow_missing=True)
            now = time.time()
            issuance_id = secrets.token_hex(16)
            data["grants"][run_id] = {
                "action": ACTION,
                "granted_at": now,
                "expires_at": now + ttl_seconds,
                "issuance_id": issuance_id,
                "consumed_at": None,
                "binding": exact_binding,
            }
            save_atomic(store, data)
    except (OSError, ValueError) as exc:
        return fail(str(exc))
    print(issuance_id)
    return 0


def consume(store: Path, run_id: str, common_dir: str, skill: str, network_mode: str) -> int:
    if not RUN_ID.fullmatch(run_id):
        return fail("run id contains unsupported characters or exceeds 128 bytes")
    try:
        exact_binding = binding(common_dir, skill, network_mode)
        with StoreLock(store):
            data = load(store)
            entry = data["grants"].get(run_id)
            now = time.time()
            if not isinstance(entry, dict) or entry.get("action") != ACTION:
                return fail(f"exact grant {ACTION} is missing for run {run_id}")
            if entry.get("binding") != exact_binding:
                return fail("danger grant binding does not match repo, skill, network mode, and sandbox action")
            granted_at = entry.get("granted_at")
            expires_at = entry.get("expires_at")
            if not isinstance(granted_at, (int, float)) or not isinstance(expires_at, (int, float)):
                return fail("danger grant timestamps are invalid")
            if granted_at > now + 60 or now - granted_at > MAX_TTL_SECONDS:
                return fail("danger grant timestamp is in the future or older than 72 hours")
            if expires_at <= now or expires_at - granted_at > MAX_TTL_SECONDS:
                return fail("danger grant is expired or exceeds the 72-hour maximum")
            if entry.get("consumed_at") is not None:
                return fail("danger grant was already consumed; reuse is forbidden")
            issuance_id = entry.get("issuance_id")
            if not isinstance(issuance_id, str) or len(issuance_id) < 16:
                return fail("danger grant issuance id is invalid")
            consumption_id = hashlib.sha256(
                f"{run_id}\0{ACTION}\0{issuance_id}\0{granted_at}\0{expires_at}".encode()
            ).hexdigest()
            entry.update(
                consumed_at=now,
                consumed_by="gsd-run",
                consumption_id=consumption_id,
            )
            save_atomic(store, data)
    except (OSError, ValueError) as exc:
        return fail(str(exc))
    print(consumption_id)
    return 0


def resume(store: Path, run_id: str, common_dir: str, skill: str, network_mode: str) -> int:
    """Validate a still-live consumed grant for the same run-bound resume."""
    if not RUN_ID.fullmatch(run_id):
        return fail("run id contains unsupported characters or exceeds 128 bytes")
    try:
        exact_binding = binding(common_dir, skill, network_mode)
        with StoreLock(store):
            data = load(store)
            entry = data["grants"].get(run_id)
            now = time.time()
            if not isinstance(entry, dict) or entry.get("action") != ACTION:
                return fail(f"exact grant {ACTION} is missing for run {run_id}")
            if entry.get("binding") != exact_binding:
                return fail("danger grant binding does not match repo, skill, network mode, and sandbox action")
            granted_at = entry.get("granted_at")
            expires_at = entry.get("expires_at")
            if not isinstance(granted_at, (int, float)) or not isinstance(expires_at, (int, float)):
                return fail("danger grant timestamps are invalid")
            if granted_at > now + 60 or now - granted_at > MAX_TTL_SECONDS:
                return fail("danger grant timestamp is in the future or older than 72 hours")
            if expires_at <= now or expires_at - granted_at > MAX_TTL_SECONDS:
                return fail("danger grant is expired or exceeds the 72-hour maximum")
            consumption_id = entry.get("consumption_id")
            if entry.get("consumed_by") != "gsd-run" or not isinstance(consumption_id, str):
                return fail("danger grant was not consumed by the original gsd-run launch")
    except (OSError, ValueError) as exc:
        return fail(str(exc))
    print(consumption_id)
    return 0


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[1] not in {"issue", "consume", "resume"}:
        print(
            "usage: consume-danger-grant.py issue <store> <run-id> <ttl-hours> <git-common-dir> <skill> <network-mode>\n"
            "       consume-danger-grant.py consume <store> <run-id> <git-common-dir> <skill> <network-mode>\n"
            "       consume-danger-grant.py resume <store> <run-id> <git-common-dir> <skill> <network-mode>",
            file=sys.stderr,
        )
        return 2
    command, store, run_id = sys.argv[1], Path(sys.argv[2]).expanduser(), sys.argv[3]
    if command == "issue":
        if len(sys.argv) != 8:
            return 2
        return issue(store, run_id, sys.argv[4], sys.argv[5], sys.argv[6], sys.argv[7])
    if len(sys.argv) != 7:
        return 2
    if command == "consume":
        return consume(store, run_id, sys.argv[4], sys.argv[5], sys.argv[6])
    return resume(store, run_id, sys.argv[4], sys.argv[5], sys.argv[6])


if __name__ == "__main__":
    raise SystemExit(main())
