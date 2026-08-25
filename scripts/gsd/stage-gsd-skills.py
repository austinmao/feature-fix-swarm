#!/usr/bin/env python3
"""Verify GSD's global Codex skills against its installer manifest and stage them."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys


GSD_VERSION = "1.11.0"
SKILL_NAME = re.compile(r"^gsd-[a-z0-9][a-z0-9-]*$")


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"gsd-run: {message}")


def regular_no_symlink(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        fail(f"{label} must be a regular non-symlink file: {path}")


def load_manifest(path: Path) -> dict[str, str]:
    regular_no_symlink(path, "GSD manifest")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"invalid GSD manifest: {exc}")
    files = data.get("files")
    if data.get("version") != GSD_VERSION or not isinstance(files, dict):
        fail(f"GSD manifest must declare version {GSD_VERSION} and a files object")
    return files


def verified_skills(manifest: Path, root: Path, requested: str) -> list[tuple[Path, Path]]:
    if not SKILL_NAME.fullmatch(requested):
        fail(f"invalid requested GSD skill: {requested}")
    if root.is_symlink() or root.parent.is_symlink() or not root.is_dir():
        fail(f"global GSD skill root must be a regular non-symlink directory: {root}")
    files = load_manifest(manifest)
    verified: list[tuple[Path, Path]] = []
    requested_found = False
    for raw, expected in sorted(files.items()):
        relative = Path(raw)
        if len(relative.parts) < 3 or relative.parts[0] != "skills":
            continue
        skill = relative.parts[1]
        if not SKILL_NAME.fullmatch(skill):
            continue
        if relative.is_absolute() or ".." in relative.parts:
            fail(f"unsafe GSD skill manifest path: {raw}")
        local = Path(*relative.parts[1:])
        source = root / local
        # Refuse symlinks at the skill directory, any nested parent, or file.
        cursor = root
        for part in local.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                fail(f"manifest-owned GSD skill path must not be a symlink: {cursor}")
        regular_no_symlink(source, "manifest-owned GSD skill")
        if not isinstance(expected, str) or len(expected) != 64:
            fail(f"invalid manifest hash for GSD skill path: {raw}")
        actual = hashlib.sha256(source.read_bytes()).hexdigest()
        if actual != expected:
            fail(f"GSD skill hash mismatch against installer manifest: {raw}")
        verified.append((source, local))
        requested_found = requested_found or skill == requested
    if not verified:
        fail("GSD installer manifest contains no global skills")
    if not requested_found:
        fail(f"requested GSD skill is not owned by the installer manifest: {requested}")
    return verified


def main() -> int:
    if len(sys.argv) not in {5, 6} or sys.argv[1] not in {"verify", "stage"}:
        print(
            "usage: stage-gsd-skills.py verify <manifest> <global-skills-root> <requested-skill>\n"
            "       stage-gsd-skills.py stage <manifest> <global-skills-root> <target-root> <requested-skill>",
            file=sys.stderr,
        )
        return 2
    command = sys.argv[1]
    manifest = Path(os.path.abspath(sys.argv[2]))
    root = Path(os.path.abspath(sys.argv[3]))
    if command == "verify":
        if len(sys.argv) != 5:
            return 2
        verified_skills(manifest, root, sys.argv[4])
        return 0
    if len(sys.argv) != 6:
        return 2
    target = Path(os.path.abspath(sys.argv[4]))
    requested = sys.argv[5]
    if target.is_symlink() or (target.exists() and not target.is_dir()) or (
        target.is_dir() and any(target.iterdir())
    ):
        fail(f"target GSD skill directory must be empty and non-symlink: {target}")
    target.mkdir(parents=True, exist_ok=True)
    for source, relative in verified_skills(manifest, root, requested):
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != hashlib.sha256(source.read_bytes()).hexdigest():
            fail(f"staged GSD skill hash mismatch: {relative}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
