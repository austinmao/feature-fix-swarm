"""Focused containment tests for private Codex runtime staging."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat

import pytest

from run_state.runtime_staging import (
    _instrumented_session_start_bytes,
    RuntimeStagingError,
    STAGE_MANIFEST_NAME,
    stage_private_codex_runtime,
    stage_or_reuse_private_codex_runtime,
    validate_staged_private_codex_runtime,
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file() and not path.is_symlink()}


def _profile(tmp_path: Path, *, auth_mode: int = 0o600) -> tuple[Path, Path, Path]:
    source = tmp_path / ".codex"
    skills = tmp_path / ".agents" / "skills"
    worktree = tmp_path / "worktree"
    source.mkdir(); skills.mkdir(parents=True); worktree.mkdir()
    (source / "agents").mkdir()
    (source / "agents" / "gsd-executor.toml").write_text(
        f'home = "{source}"\nskills = "{skills}"\n', encoding="utf-8"
    )
    (source / "gsd-core").mkdir()
    (source / "gsd-core" / "VERSION").write_text("1.14.0\n", encoding="utf-8")
    (source / "scripts").mkdir()
    script = source / "scripts" / "gsd-run.sh"
    script.write_text("#!/bin/sh\necho staged\n", encoding="utf-8"); script.chmod(0o755)
    (source / "hooks").mkdir()
    hook = source / "hooks" / "gsd-hook.js"
    hook.write_text("#!/usr/bin/env node\n", encoding="utf-8"); hook.chmod(0o755)
    (skills / "gsd-quick").mkdir()
    (skills / "gsd-quick" / "SKILL.md").write_text("# GSD quick\n", encoding="utf-8")
    (source / "hooks.json").write_text(json.dumps({
        "hook_path": str(hook), "skill_path": str(skills / "gsd-quick" / "SKILL.md"),
    }), encoding="utf-8")
    auth = source / "auth.json"
    auth.write_text(json.dumps({
        "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
        "tokens": {"id_token": "fixture-id", "access_token": "source-only-secret",
                   "refresh_token": "must-not-enter-child", "account_id": "fixture-account"},
        "last_refresh": "2026-09-16T00:00:00Z",
    }) + "\n", encoding="utf-8"); auth.chmod(auth_mode)
    owned = {
        "agents/gsd-executor.toml": _digest(source / "agents" / "gsd-executor.toml"),
        "gsd-core/VERSION": _digest(source / "gsd-core" / "VERSION"),
        "scripts/gsd-run.sh": _digest(script),
        "skills/gsd-quick/SKILL.md": _digest(skills / "gsd-quick" / "SKILL.md"),
    }
    (source / "gsd-file-manifest.json").write_text(
        json.dumps({"version": "1.14.0", "files": owned}), encoding="utf-8"
    )
    return source, skills, worktree


def test_stage_isolated_runtime_rewrites_paths_and_never_mutates_source(tmp_path: Path) -> None:
    source, skills, worktree = _profile(tmp_path)
    before = _tree_bytes(tmp_path)
    target = tmp_path / "private-runtime"

    result = stage_private_codex_runtime(source, target, worktree)

    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert stat.S_IMODE((target / "auth.json").stat().st_mode) == 0o600
    staged_auth = json.loads((target / "auth.json").read_text())
    assert staged_auth["tokens"]["access_token"] == "source-only-secret"
    assert staged_auth["tokens"]["refresh_token"] == "ffs-access-only-no-refresh"
    assert "must-not-enter-child" not in (target / "auth.json").read_text()
    assert os.access(target / "scripts" / "gsd-run.sh", os.X_OK)
    assert os.access(target / "hooks" / "gsd-hook.js", os.X_OK)
    assert str(source) not in (target / "hooks.json").read_text(encoding="utf-8")
    assert str(skills) not in (target / "hooks.json").read_text(encoding="utf-8")
    assert str(target / "hooks" / "gsd-hook.js") in (target / "hooks.json").read_text(encoding="utf-8")
    assert str(target / "skills" / "gsd-quick" / "SKILL.md") in (target / "hooks.json").read_text(encoding="utf-8")
    config = (target / "config.toml").read_text(encoding="utf-8")
    assert 'sandbox_mode = "workspace-write"' in config
    assert "network_access = false" in config and str(worktree) in config
    evidence = json.loads((target / STAGE_MANIFEST_NAME).read_text(encoding="utf-8"))
    assert evidence == result
    assert "source-only-secret" not in (target / STAGE_MANIFEST_NAME).read_text(encoding="utf-8")
    assert str(source) not in (target / STAGE_MANIFEST_NAME).read_text(encoding="utf-8")
    after = _tree_bytes(tmp_path)
    assert all(after[key] == value for key, value in before.items())


def test_stage_prebinds_codex_linked_worktree_canonical_trust_entry(tmp_path: Path) -> None:
    source, _skills, worktree = _profile(tmp_path)
    primary = tmp_path / "primary"
    gitdir = primary / ".git" / "worktrees" / "child"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    target = tmp_path / "private-runtime"

    stage_private_codex_runtime(source, target, worktree)

    config = (target / "config.toml").read_text(encoding="utf-8")
    assert f'[projects."{worktree}"]\ntrust_level = "untrusted"' in config
    assert f'[projects."{primary}"]\ntrust_level = "trusted"' in config
    validate_staged_private_codex_runtime(target, worktree)


def test_session_start_instrumentation_chains_without_replacing_pinned_hook() -> None:
    source = b"#!/usr/bin/env node\nconst fs = require('fs');\nconst marker = 'pinned-body';\n"
    rendered = _instrumented_session_start_bytes(source)
    assert b"ffs-supervised-session-start-observer/v1" in rendered
    assert b"payload.hook_event_name === 'SessionStart'" in rendered
    assert b"const marker = 'pinned-body';" in rendered


def test_stage_refuses_source_symlinks_without_following_them(tmp_path: Path) -> None:
    source, _skills, worktree = _profile(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("must never be copied", encoding="utf-8")
    (source / "scripts" / "escape").symlink_to(outside)
    target = tmp_path / "private-runtime"

    with pytest.raises(RuntimeStagingError, match="symlink"):
        stage_private_codex_runtime(source, target, worktree)

    assert outside.read_text(encoding="utf-8") == "must never be copied"
    assert not target.exists()


@pytest.mark.parametrize("mode", (0o644, 0o400))
def test_stage_refuses_unsafe_auth_modes(tmp_path: Path, mode: int) -> None:
    source, _skills, worktree = _profile(tmp_path, auth_mode=mode)
    target = tmp_path / "private-runtime"

    with pytest.raises(RuntimeStagingError, match="source auth"):
        stage_private_codex_runtime(source, target, worktree)

    assert not target.exists()


def test_stage_refuses_a_preexisting_target_without_touching_it(tmp_path: Path) -> None:
    source, _skills, worktree = _profile(tmp_path)
    target = tmp_path / "private-runtime"
    target.mkdir(mode=0o700)
    sentinel = target / "sentinel"
    sentinel.write_text("preserve", encoding="utf-8")

    with pytest.raises(RuntimeStagingError, match="must not already exist"):
        stage_private_codex_runtime(source, target, worktree)

    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_stage_or_reuse_returns_the_retained_exact_stage_without_rewriting_it(tmp_path: Path) -> None:
    source, _skills, worktree = _profile(tmp_path)
    target = tmp_path / "private-runtime"
    staged = stage_or_reuse_private_codex_runtime(source, target, worktree)
    before = _tree_bytes(target)
    identities = {
        path.relative_to(target).as_posix(): (path.stat().st_dev, path.stat().st_ino)
        for path in target.rglob("*") if path.is_file()
    }

    reused = stage_or_reuse_private_codex_runtime(source, target, worktree)

    assert reused == staged
    assert _tree_bytes(target) == before
    assert {
        path.relative_to(target).as_posix(): (path.stat().st_dev, path.stat().st_ino)
        for path in target.rglob("*") if path.is_file()
    } == identities


@pytest.mark.parametrize("tamper", ("content", "config", "symlink", "source-closure"))
def test_stage_or_reuse_refuses_drift_without_repairing_the_stage(tmp_path: Path, tamper: str) -> None:
    source, _skills, worktree = _profile(tmp_path)
    target = tmp_path / "private-runtime"
    stage_or_reuse_private_codex_runtime(source, target, worktree)
    if tamper == "content":
        changed = target / "scripts" / "gsd-run.sh"
        changed.write_text("#!/bin/sh\necho tampered\n", encoding="utf-8")
    elif tamper == "config":
        changed = target / "config.toml"
        changed.write_text('sandbox_mode = "danger-full-access"\n', encoding="utf-8")
    elif tamper == "symlink":
        changed = target / "hooks" / "escape"
        changed.symlink_to(source / "auth.json")
    else:
        changed = source / "scripts" / "gsd-run.sh"
        changed.write_text("#!/bin/sh\necho source-drift\n", encoding="utf-8")
    before = changed.read_bytes() if not changed.is_symlink() else os.readlink(changed).encode()

    with pytest.raises(RuntimeStagingError):
        stage_or_reuse_private_codex_runtime(source, target, worktree)

    after = changed.read_bytes() if not changed.is_symlink() else os.readlink(changed).encode()
    assert after == before


def test_stage_or_reuse_refuses_consumed_auth_and_a_different_workspace(tmp_path: Path) -> None:
    source, _skills, worktree = _profile(tmp_path)
    target = tmp_path / "private-runtime"
    stage_or_reuse_private_codex_runtime(source, target, worktree)
    (target / "auth.json").unlink()

    with pytest.raises(RuntimeStagingError, match="auth has been revoked"):
        stage_or_reuse_private_codex_runtime(source, target, worktree)

    # A valid stage is also bound to exactly one workspace identity.
    target = tmp_path / "another-private-runtime"
    stage_or_reuse_private_codex_runtime(source, target, worktree)
    another_worktree = tmp_path / "another-worktree"
    another_worktree.mkdir()
    with pytest.raises(RuntimeStagingError, match="workspace"):
        stage_or_reuse_private_codex_runtime(source, target, another_worktree)


def test_stage_or_reuse_does_not_trust_a_tampered_manifest_to_bless_changed_bytes(tmp_path: Path) -> None:
    source, _skills, worktree = _profile(tmp_path)
    target = tmp_path / "private-runtime"
    stage_or_reuse_private_codex_runtime(source, target, worktree)
    script = target / "scripts" / "gsd-run.sh"
    script.write_text("#!/bin/sh\necho forged\n", encoding="utf-8")
    staged_manifest = target / STAGE_MANIFEST_NAME
    evidence = json.loads(staged_manifest.read_text(encoding="utf-8"))
    info = script.stat()
    evidence["target"]["files"]["scripts/gsd-run.sh"] = {
        "identity": {
            "path": str(script.resolve()), "device": info.st_dev, "inode": info.st_ino,
            "mode": stat.S_IMODE(info.st_mode), "links": info.st_nlink,
        },
        "sha256": _digest(script),
    }
    staged_manifest.write_text(json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    staged_manifest.chmod(0o600)

    with pytest.raises(RuntimeStagingError, match="source closure"):
        stage_or_reuse_private_codex_runtime(source, target, worktree)

    assert script.read_text(encoding="utf-8") == "#!/bin/sh\necho forged\n"
