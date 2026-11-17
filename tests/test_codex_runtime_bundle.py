"""scripts/gsd/codex-runtime-bundle.py stages the operator-only guard beside the GSD hooks."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

from run_state.tests.installed_home_fixture import (
    GUARD, GUARD_ALLOW, GUARD_DENY, guard_command_of, install_user_home, run_guard_command, sha256,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/gsd/codex-runtime-bundle.py"
PACKAGE = ROOT / "node_modules/@opengsd/gsd-core"
SPEC = importlib.util.spec_from_file_location("codex_runtime_bundle", SCRIPT)
assert SPEC and SPEC.loader
bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundle)


def _trusted_node(tmp_path: Path) -> Path:
    """The PATH node, or a private 0755 copy when the toolcache copy is group/other-writable (CI)."""
    node = Path(os.path.realpath(shutil.which("node") or "node"))
    metadata = node.stat()
    if metadata.st_uid in {0, os.getuid()} and not metadata.st_mode & 0o022:
        return node
    copy = tmp_path / "trusted-node" / "bin" / "node"
    copy.parent.mkdir(parents=True)
    if (node.parent.parent / "lib").is_dir():  # keep @loader_path/../lib dylibs resolvable
        (copy.parent.parent / "lib").symlink_to(node.parent.parent / "lib")
    shutil.copyfile(node, copy)
    copy.chmod(0o755)
    return copy


def _installed_codex_home(tmp_path: Path, *, keep_guard: bool = True) -> tuple[Path, Path]:
    """Installer-built ~/.codex, completed only with the verified GSD hook subset and registration."""
    home = install_user_home(tmp_path)
    source = home / ".codex"
    node = _trusted_node(tmp_path)
    for name in bundle.CODEX_HOOK_DEPENDENCIES:
        (source / "hooks" / name).write_bytes(bundle.expected_codex_hook(PACKAGE / "hooks", name))
    (source / "agents").mkdir()
    (source / "agents/gsd-executor.toml").write_text("agent\n")
    (source / "scripts").mkdir()
    (source / "scripts/gsd-run.sh").write_text("#!/bin/sh\n")
    files = {rel: sha256(source / rel) for rel in ("agents/gsd-executor.toml", "gsd-core/VERSION", "scripts/gsd-run.sh")}
    (source / "gsd-file-manifest.json").write_text(json.dumps({"version": bundle.GSD_VERSION, "files": files}))
    document = json.loads((source / "hooks.json").read_text())
    if not keep_guard:
        document["hooks"].pop("PreToolUse")  # an install that predates the guard
        (source / "hooks" / GUARD).unlink()
    session = f"{shlex.quote(str(node))} {shlex.quote(str(source / 'hooks/gsd-check-update.js'))}"
    document["hooks"]["SessionStart"] = [{"hooks": [{"type": "command", "command": session}]}]
    (source / "hooks.json").write_text(json.dumps(document))
    return source, node


def _stage(source: Path, node: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(source), str(PACKAGE), str(node), str(tmp_path / "stage"), str(worktree)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )


def test_bundle_stages_the_installed_guard_and_registers_the_staged_copy(tmp_path: Path) -> None:
    source, node = _installed_codex_home(tmp_path)

    result = _stage(source, node, tmp_path)

    assert result.returncode == 0, result.stderr
    stage = tmp_path / "stage"
    staged = stage / "hooks" / GUARD
    assert staged.read_bytes() == (source / "hooks" / GUARD).read_bytes()
    registered = json.loads((stage / "hooks.json").read_text())["hooks"]
    assert set(registered) == {"SessionStart", "PreToolUse"}
    command = guard_command_of(stage / "hooks.json")
    assert command == f"bash {staged}"
    denied = run_guard_command(command, {"cmd": GUARD_DENY}, tmp_path)
    assert (denied.returncode, "operator-only" in denied.stderr) == (2, True)
    assert run_guard_command(command, {"cmd": GUARD_ALLOW}, tmp_path).returncode == 0


def test_bundle_without_an_installed_guard_stages_as_before(tmp_path: Path) -> None:
    source, node = _installed_codex_home(tmp_path, keep_guard=False)

    result = _stage(source, node, tmp_path)

    assert result.returncode == 0, result.stderr
    assert set(json.loads((tmp_path / "stage/hooks.json").read_text())["hooks"]) == {"SessionStart"}
    assert not (tmp_path / "stage/hooks" / GUARD).exists()
