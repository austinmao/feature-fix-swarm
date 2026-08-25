#!/usr/bin/env python3
"""Verify and stage a pinned GSD Codex bundle into an isolated CODEX_HOME."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
from typing import NoReturn


GSD_VERSION = "1.11.0"
CANONICAL_HOOKS = {
    "SessionStart": "gsd-check-update.js",
    "SubagentStart": "gsd-context-monitor.js",
    "Stop": "gsd-context-monitor.js",
    "PostToolUse": "gsd-context-monitor.js",
    "PreToolUse": "gsd-context-monitor.js",
    "PermissionRequest": "gsd-context-monitor.js",
    "PreCompact": "gsd-context-monitor.js",
    "PostCompact": "gsd-context-monitor.js",
    "SubagentStop": "gsd-context-monitor.js",
    "UserPromptSubmit": "gsd-context-monitor.js",
}
CODEX_HOOK_DEPENDENCIES = {
    "gsd-check-update.js",
    "gsd-check-update-worker.js",
    "managed-hooks-registry.cjs",
    "gsd-context-monitor.js",
}


def fail(message: str) -> NoReturn:
    raise SystemExit(f"gsd-run: {message}")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_node(node: Path) -> Path:
    if not node.is_absolute() or node.is_symlink() or not node.is_file():
        fail(f"trusted Node must resolve to an absolute regular file: {node}")
    metadata = node.stat()
    if metadata.st_uid not in {0, os.getuid()} or stat.S_IMODE(metadata.st_mode) & 0o022:
        fail(f"trusted Node has unsafe ownership or permissions: {node}")
    if not os.access(node, os.X_OK):
        fail(f"trusted Node is not executable: {node}")
    return node


def verify_package(package_root: Path) -> Path:
    try:
        package = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"invalid pinned GSD package metadata: {exc}")
    if package.get("name") != "@opengsd/gsd-core" or package.get("version") != GSD_VERSION:
        fail(f"pinned GSD package must be @opengsd/gsd-core@{GSD_VERSION}")
    hooks = package_root / "hooks"
    if not hooks.is_dir() or hooks.is_symlink():
        fail("pinned GSD package has no regular hooks tree")
    files = [path for path in hooks.rglob("*") if not path.is_dir()]
    if not files:
        fail("pinned GSD package hooks tree is empty")
    for path in files:
        if path.is_symlink() or not path.is_file():
            fail(f"pinned GSD hooks tree contains a non-regular file: {path}")
    return hooks


def copy_manifest_bundle(source: Path, target: Path) -> None:
    manifest_path = source / "gsd-file-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"invalid GSD Codex manifest: {exc}")
    if manifest.get("version") != GSD_VERSION or not isinstance(manifest.get("files"), dict):
        fail(f"GSD Codex manifest must declare version {GSD_VERSION}")
    copied = 0
    for rel, expected in manifest["files"].items():
        if not rel.startswith(("agents/", "gsd-core/", "scripts/")):
            continue
        relative = Path(rel)
        if relative.is_absolute() or ".." in relative.parts:
            fail(f"unsafe manifest path: {rel}")
        src = source / relative
        if not src.is_file() or src.is_symlink():
            fail(f"manifest-owned file missing or symlinked: {rel}")
        if digest(src) != expected:
            fail(f"manifest hash mismatch: {rel}")
        dst = target / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    if copied == 0:
        fail("manifest contains no Codex bundle files")
    shutil.copy2(manifest_path, target / manifest_path.name)


def expected_codex_hook(package_hooks: Path, name: str) -> bytes:
    source = package_hooks / "dist" / name
    if not source.is_file() or source.is_symlink():
        fail(f"pinned package is missing Codex hook dependency: {name}")
    content = source.read_bytes()
    if name.endswith(".js"):
        text = content.decode("utf-8")
        text = text.replace("'.claude'", "'.codex'")
        text = text.replace("/.claude/", "/.codex/")
        text = text.replace(".claude/", ".codex/")
        text = text.replace("{{GSD_VERSION}}", GSD_VERSION)
        content = text.encode()
    return content


def verify_installed_hook_subset(installed: Path, package_hooks: Path) -> dict[str, Path]:
    if not installed.is_dir() or installed.is_symlink():
        fail("installed GSD hook tree is missing or symlinked")
    verified: dict[str, Path] = {}
    for name in CODEX_HOOK_DEPENDENCIES:
        path = installed / name
        if path.is_symlink() or not path.is_file():
            fail(f"installed GSD hook dependency is missing or non-regular: {name}")
        if path.read_bytes() != expected_codex_hook(package_hooks, name):
            fail(f"hook dependency hash mismatch against pinned package transform: {name}")
        verified[name] = path
    return verified


def command_nodes(value: object) -> list[dict]:
    found: list[dict] = []
    if isinstance(value, dict):
        if value.get("type") == "command" and isinstance(value.get("command"), str):
            found.append(value)
        for child in value.values():
            found.extend(command_nodes(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(command_nodes(child))
    return found


def canonical_registration(
    event: str,
    entries: object,
    expected_name: str,
    installed_hooks: Path,
    verified_hooks: dict[str, Path],
    staged_hooks: Path,
    safe_node: Path,
) -> list[dict]:
    matches: list[tuple[dict, Path]] = []
    for node in command_nodes(entries):
        argv = shlex.split(str(node["command"]))
        if len(argv) != 2:
            continue
        interpreter = Path(argv[0])
        target = Path(argv[1])
        if target.name != expected_name:
            continue
        if not interpreter.is_absolute() or Path(os.path.realpath(interpreter)) != safe_node:
            fail(f"{event} GSD hook does not use the trusted Node binary")
        if not target.is_absolute() or Path(os.path.realpath(target)).parent != installed_hooks.resolve():
            fail(f"{event} GSD hook targets outside the installed hook root")
        if expected_name not in verified_hooks or target.resolve() != verified_hooks[expected_name].resolve():
            fail(f"{event} GSD hook does not target the verified installed dependency")
        matches.append((node, staged_hooks / target.name))
    if len(matches) != 1:
        fail(f"{event} must contain exactly one canonical {expected_name} registration")
    _, staged_target = matches[0]
    return [{"hooks": [{"type": "command", "command": f"{shlex.quote(str(safe_node))} {shlex.quote(str(staged_target))}"}]}]


def stage_hooks(
    source: Path,
    target: Path,
    package_hooks: Path,
    safe_node: Path,
    worktree: Path,
) -> None:
    installed_hooks = source / "hooks"
    hooks_json = source / "hooks.json"
    if not hooks_json.is_file() or hooks_json.is_symlink():
        fail("installed hooks.json is missing or symlinked")
    verified_hooks = verify_installed_hook_subset(installed_hooks, package_hooks)
    staged_hooks = target / "hooks"
    shutil.copytree(package_hooks, staged_hooks)
    for pinned in package_hooks.rglob("*"):
        if pinned.is_file() and digest(pinned) != digest(staged_hooks / pinned.relative_to(package_hooks)):
            fail(f"staged hook dependency hash mismatch: {pinned.relative_to(package_hooks)}")
    # Overlay the installer's verified Codex-specific transforms on the full
    # pinned tree. This keeps every sibling dependency while preserving the
    # runtime's stamped version and `.codex` path semantics.
    for name, installed in verified_hooks.items():
        shutil.copy2(installed, staged_hooks / name)
    try:
        document = json.loads(hooks_json.read_text(encoding="utf-8"))
        registrations = document["hooks"]
    except Exception as exc:
        fail(f"invalid hooks.json: {exc}")
    if not isinstance(registrations, dict):
        fail("hooks.json hooks must be an object")
    canonical = {
        event: canonical_registration(
            event,
            registrations.get(event),
            expected,
            installed_hooks,
            verified_hooks,
            staged_hooks,
            safe_node,
        )
        for event, expected in CANONICAL_HOOKS.items()
    }
    (target / "hooks.json").write_text(
        json.dumps({"hooks": canonical}, indent=2) + "\n", encoding="utf-8"
    )
    smoke_env = {
        "PATH": str(safe_node.parent),
        "HOME": str(target),
        "CODEX_HOME": str(target),
        "GSD_SKIP_UPDATE_CHECK": "1",
    }
    for event, expected in CANONICAL_HOOKS.items():
        result = subprocess.run(
            [str(safe_node), str(staged_hooks / expected)],
            input=json.dumps({"hook_event_name": event}) + "\n",
            text=True,
            cwd=worktree,
            env=smoke_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            fail(f"hook smoke failed for {event} rc={result.returncode}")


def main() -> int:
    if len(sys.argv) != 6:
        print(
            "usage: codex-runtime-bundle.py <installed-codex-home> <pinned-package> <safe-node> <target-home> <worktree>",
            file=sys.stderr,
        )
        return 2
    source = Path(os.path.abspath(sys.argv[1]))
    package = Path(os.path.abspath(sys.argv[2]))
    safe_node = verify_node(Path(os.path.realpath(sys.argv[3])))
    target = Path(os.path.abspath(sys.argv[4]))
    worktree = Path(os.path.abspath(sys.argv[5]))
    target.mkdir(parents=True, exist_ok=True)
    package_hooks = verify_package(package)
    copy_manifest_bundle(source, target)
    stage_hooks(source, target, package_hooks, safe_node, worktree)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
