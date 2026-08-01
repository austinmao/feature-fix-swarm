#!/usr/bin/env python3
"""Hash the resolved manifest-owned FFS skill tree deterministically."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys


def manifest_skills(manifest_path: Path, project: Path | None) -> list[Path]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if manifest.get("schema") != "ffs.install/v1":
        return []
    paths: list[Path] = []
    for raw in manifest.get("paths", {}):
        candidate = Path(raw)
        destination = project / candidate if project is not None and not candidate.is_absolute() else candidate
        normalized = destination.as_posix()
        if "/.agents/skills/" not in normalized:
            continue
        if destination.name.startswith("gsd-"):
            continue
        if destination.exists():
            paths.append(destination)
    return paths


def hash_paths(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for root in sorted(paths, key=lambda item: item.name):
        resolved = root.resolve()
        files = [resolved] if resolved.is_file() else [p for p in resolved.rglob("*") if p.is_file()]
        for path in sorted(files, key=lambda item: item.name if resolved.is_file() else item.relative_to(resolved).as_posix()):
            relative = root.name + "/" + (path.name if resolved.is_file() else path.relative_to(resolved).as_posix())
            name = relative.encode()
            content = path.read_bytes()
            digest.update(len(name).to_bytes(8, "big"))
            digest.update(name)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    return digest.hexdigest()


def main() -> int:
    if len(sys.argv) != 5:
        print("usage: hash-ffs-skills.py <project-root> <source-skills> <project-manifest> <user-manifest>", file=sys.stderr)
        return 2
    project = Path(sys.argv[1])
    source_skills = Path(sys.argv[2])
    project_manifest = Path(sys.argv[3])
    user_manifest = Path(sys.argv[4])
    # Resolution is per skill: user scope supplies the baseline and project
    # scope overrides matching names without hiding unrelated user skills.
    merged = {path.name: path for path in manifest_skills(user_manifest, None)}
    merged.update({path.name: path for path in manifest_skills(project_manifest, project)})
    paths = list(merged.values())
    if not paths and source_skills.is_dir():
        paths = [path for path in source_skills.iterdir() if path.is_dir() and (path / "SKILL.md").is_file()]
    print(hash_paths(paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
