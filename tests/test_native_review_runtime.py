import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from run_state.native_review_runtime import (
    CLAUDE_CLI_VERSION,
    CODEX_CLI_VERSION,
    NativeReviewRequest,
    NativeReviewRuntimeRefused,
    prepare_native_review_runtime,
    validate_native_review_material,
)


def _write(path: Path, content: bytes, mode: int = 0o644) -> Path:
    path.write_bytes(content)
    path.chmod(mode)
    return path


def _request(binary: Path, catalog: Path, *, model: str = "gpt-5.6-terra",
             version: str = CODEX_CLI_VERSION) -> NativeReviewRequest:
    return NativeReviewRequest(
        host="codex", requested_model=model, cli_version=version, binary=str(binary),
        binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(), runtime_identity="codex154-test",
        prompt="Review only the supplied artifacts; do not modify files.", catalog_path=str(catalog),
        catalog_sha256=hashlib.sha256(catalog.read_bytes()).hexdigest(),
    )


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    workspace = tmp_path / "review-workspace"
    workspace.mkdir(mode=0o755)
    binary = _write(tmp_path / "codex", b"#!/bin/sh\nexit 0\n", 0o755)
    catalog = _write(tmp_path / "models.json", json.dumps({
        "models": [{
            "slug": "gpt-5.6-terra", "display_name": "Terra", "context_window": 12345,
            "apply_patch_tool_type": "freeform",
            "experimental_supported_tools": ["clock", "async", "request_user_input"],
            "tool_mode": "code_mode_only",
            "metadata": {"provider": "openai", "subscription": True},
        }, {"slug": "other", "apply_patch_tool_type": "freeform"}],
        "default_model": "other",
    }, sort_keys=True).encode())
    return parent, workspace, binary, catalog


def test_codex_unspecified_effort_still_refuses(tmp_path):
    parent, workspace, binary, catalog = _inputs(tmp_path)
    with pytest.raises(NativeReviewRuntimeRefused, match="effort"):
        prepare_native_review_runtime(replace(_request(binary, catalog), effort=None),
                                      runtime_root=parent / "unspecified", workspace=workspace)


def test_codex_material_is_closed_hash_bound_and_keeps_exact_model(tmp_path: Path) -> None:
    parent, workspace, binary, catalog = _inputs(tmp_path)

    material = prepare_native_review_runtime(_request(binary, catalog), runtime_root=parent / "one", workspace=workspace)

    assert material.requested_model == "gpt-5.6-terra"
    assert material.argv[material.argv.index('--model') + 1] == 'gpt-5.6-terra'
    overrides = [material.argv[i + 1] for i, item in enumerate(material.argv) if item == '-c']
    assert 'features.view_image=false' in overrides
    assert 'features.code_mode_host=false' in overrides
    assert 'features.current_time_reminder=false' in overrides
    assert 'tools.experimental_request_user_input.enabled=false' in overrides
    assert f'model_catalog_json={json.dumps(material.catalog_path)}' in overrides
    assert dict(material.environment)["CODEX_HOME"] == material.runtime_root
    assert oct(Path(material.runtime_root).stat().st_mode & 0o777) == "0o700"
    private_catalog = json.loads(Path(material.catalog_path).read_text())
    assert private_catalog["default_model"] == "gpt-5.6-terra"
    assert [model["slug"] for model in private_catalog["models"]] == ["gpt-5.6-terra"]
    assert private_catalog["models"][0]["metadata"] == {"provider": "openai", "subscription": True}
    assert private_catalog["models"][0]["apply_patch_tool_type"] is None
    assert private_catalog["models"][0]["experimental_supported_tools"] == []
    assert private_catalog['models'][0]['tool_mode'] == 'direct'
    config = Path(material.config_path).read_text()
    assert 'web_search = "disabled"' in config
    assert "shell_tool = false" in config
    assert "view_image = false" in config
    assert "[tools.experimental_request_user_input]\nenabled = false" in config
    assert "[tools.update_plan]\nenabled = false" in config
    assert validate_native_review_material(material) is material
    assert material.replay_binding()["operation"] == "native-artifact-review-preparation"


def test_tamper_or_replay_refuses_before_a_future_host_can_launch(tmp_path: Path) -> None:
    parent, workspace, binary, catalog = _inputs(tmp_path)
    material = prepare_native_review_runtime(_request(binary, catalog), runtime_root=parent / "one", workspace=workspace)

    Path(material.config_path).write_text("web_search = 'live'\n")
    Path(material.config_path).chmod(0o600)
    with pytest.raises(NativeReviewRuntimeRefused, match="closure drifted"):
        validate_native_review_material(material)

    second = prepare_native_review_runtime(_request(binary, catalog), runtime_root=parent / "two", workspace=workspace)
    Path(second.catalog_path).write_text(json.dumps({"models": [{"slug": "gpt-5.6-terra", "apply_patch_tool_type": "freeform", "experimental_supported_tools": ["clock"]}], "default_model": "gpt-5.6-terra"}))
    Path(second.catalog_path).chmod(0o600)
    with pytest.raises(NativeReviewRuntimeRefused, match="closure drifted"):
        validate_native_review_material(second)


@pytest.mark.parametrize("model,version", [("missing", CODEX_CLI_VERSION), ("gpt-5.6-terra", "0.154.1")])
def test_wrong_caller_resolved_model_or_version_refuses(tmp_path: Path, model: str, version: str) -> None:
    parent, workspace, binary, catalog = _inputs(tmp_path)
    with pytest.raises(NativeReviewRuntimeRefused):
        prepare_native_review_runtime(_request(binary, catalog, model=model, version=version), runtime_root=parent / "one", workspace=workspace)


def test_catalog_identity_and_unsafe_paths_refuse(tmp_path: Path) -> None:
    parent, workspace, binary, catalog = _inputs(tmp_path)
    request = _request(binary, catalog)
    _write(catalog, b"{}")
    with pytest.raises(NativeReviewRuntimeRefused, match="differs from caller-resolved identity"):
        prepare_native_review_runtime(request, runtime_root=parent / "one", workspace=workspace)

    link = tmp_path / "linked-models.json"
    os.symlink(catalog, link)
    unsafe = _request(binary, catalog)
    unsafe = NativeReviewRequest(**{**unsafe.__dict__, "catalog_path": str(link),
                                    "catalog_sha256": hashlib.sha256(catalog.read_bytes()).hexdigest()})
    with pytest.raises(NativeReviewRuntimeRefused, match="model catalog is unsafe"):
        prepare_native_review_runtime(unsafe, runtime_root=parent / "two", workspace=workspace)


def test_claude_argv_disables_tools_without_dropping_subscription_auth(tmp_path: Path) -> None:
    parent, workspace, binary, _catalog = _inputs(tmp_path)
    request = NativeReviewRequest(
        host="claude", requested_model="claude-sonnet-4-5", cli_version=CLAUDE_CLI_VERSION,
        binary=str(binary), binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        runtime_identity="claude-2.1.274", prompt="Review bound artifacts only.",
        session_id="643b3a28-33d2-4000-b983-a18c5da41bbd",
    )
    material = prepare_native_review_runtime(request, runtime_root=parent / "claude", workspace=workspace)
    assert material.argv[material.argv.index('--tools') + 1] == ''
    assert material.argv[material.argv.index('--output-format') + 1] == 'stream-json'
    profile = Path(material.runtime_root) / 'claude-config'
    assert dict(material.environment)['HOME'] == material.runtime_root
    assert dict(material.environment)['CLAUDE_CONFIG_DIR'] == str(profile)
    assert profile.stat().st_mode & 0o777 == 0o700
    assert (material.claude_config_device, material.claude_config_inode) == (
        profile.stat().st_dev, profile.stat().st_ino,
    )
    assert Path(material.mcp_path).parent == profile
    assert "--bare" not in material.argv
    assert json.loads(Path(material.mcp_path).read_text()) == {"mcpServers": {}}
    assert material.argv.count('--session-id') == 1
    assert material.argv[material.argv.index('--session-id') + 1] == request.session_id
    assert material.replay_binding()['session_id'] == request.session_id
    assert validate_native_review_material(material) is material


@pytest.mark.parametrize("session_id", [None, "", "not-a-uuid", "643B3A28-33D2-4000-B983-A18C5DA41BBD"])
def test_claude_review_requires_caller_bound_canonical_session(tmp_path, session_id):
    parent, workspace, binary, catalog = _inputs(tmp_path)
    request = replace(_request(binary, catalog), host="claude", cli_version=CLAUDE_CLI_VERSION,
                      session_id=session_id)
    with pytest.raises(NativeReviewRuntimeRefused, match="session"):
        prepare_native_review_runtime(request, runtime_root=parent / "invalid", workspace=workspace)
    assert not (parent / "invalid").exists()


def test_native_review_session_cannot_be_retargeted_or_added_to_codex(tmp_path):
    parent, workspace, binary, catalog = _inputs(tmp_path)
    request = replace(_request(binary, catalog), session_id="643b3a28-33d2-4000-b983-a18c5da41bbd")
    with pytest.raises(NativeReviewRuntimeRefused, match="session"):
        prepare_native_review_runtime(request, runtime_root=parent / "codex", workspace=workspace)
    material = prepare_native_review_runtime(
        replace(request, host="claude", cli_version=CLAUDE_CLI_VERSION),
        runtime_root=parent / "claude", workspace=workspace,
    )
    with pytest.raises(NativeReviewRuntimeRefused, match="retargeted"):
        validate_native_review_material(replace(material, session_id="11111111-1111-4111-8111-111111111111"))


@pytest.mark.parametrize("mutation", ["replacement", "symlink", "mode"])
def test_claude_review_config_directory_identity_is_pinned(tmp_path, mutation):
    parent, workspace, binary, catalog = _inputs(tmp_path)
    request = replace(_request(binary, catalog), host="claude", cli_version=CLAUDE_CLI_VERSION,
                      session_id="643b3a28-33d2-4000-b983-a18c5da41bbd")
    material = prepare_native_review_runtime(request, runtime_root=parent / "claude", workspace=workspace)
    profile = Path(material.runtime_root) / "claude-config"
    if mutation == "mode":
        profile.chmod(0o755)
    else:
        retained = profile.with_name("retained-config")
        profile.rename(retained)
        if mutation == "symlink":
            profile.symlink_to(retained, target_is_directory=True)
        else:
            profile.mkdir(mode=0o700)
            _write(profile / "empty-mcp.json", (retained / "empty-mcp.json").read_bytes(), 0o600)
    with pytest.raises(NativeReviewRuntimeRefused, match="Claude config"):
        validate_native_review_material(material)


def test_large_executable_and_forged_material(tmp_path):
    parent, workspace, binary, catalog = _inputs(tmp_path)
    binary.write_bytes(b'x' * (3 * 1024 * 1024))
    material = prepare_native_review_runtime(_request(binary, catalog), runtime_root=parent / 'large', workspace=workspace)
    assert validate_native_review_material(material) is material
    for changed in (replace(material, argv=()), replace(material, provenance=()),
                    replace(material, claude_config_device=1, claude_config_inode=2)):
        with pytest.raises(NativeReviewRuntimeRefused):
            validate_native_review_material(changed)


def test_ancestor_symlink_binary_refused(tmp_path):
    parent, workspace, binary, catalog = _inputs(tmp_path)
    alias = tmp_path / 'alias'
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(NativeReviewRuntimeRefused):
        prepare_native_review_runtime(_request(alias / binary.name, catalog),
                                      runtime_root=parent / 'alias-test', workspace=workspace)
