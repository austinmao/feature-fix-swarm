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


# F52: the native review pin is a per-version table, each row audited against its own
# openai/codex tag (spec_plan.rs, config.schema.json, openai_models.rs).  The expected
# rows are literals here so a wrong constant in the module cannot vouch for itself.
_PREPARATION_ONLY = "qualification-and-receipts-required"
_PIN_0154 = {
    "codex_release": "rust-v0.154.0",
    "codex_commit": "6b9826e3aa83b1a5947db50f4332cb9c65f1b340",
    "tool_registration_sha256": "451622e76c45dd1585318c200fdee9a00d7aaf785d4a540facca1010146307b7",
    "config_schema_sha256": "2e1fcf1cbb20f255c3baca2e174b4a3c954cef577a130587b8935e2d12c8ade6",
    "model_protocol_sha256": "2e9923d405a497441a0b264efc07de6ce21cdb108442e660a8b9fb63ca415aed",
}
_PIN_0159 = {
    "codex_release": "rust-v0.159.0",
    "codex_commit": "687a119f0fcaace47e1f1abcc77cec6c813fd6da",
    "tool_registration_sha256": "849ef21d4e5c83febdc31eacd7609911d43e3f69a35168fe02ae899273b5ef3e",
    "config_schema_sha256": "eda7251b7e46e0b9d0f3d8eef5dab451e11a55d723e2b802e152b7208045836a",
    "model_protocol_sha256": "4c8b5cafd8c55db269f669e352321f787fcaf83785bce4476cb887abfce75dc6",
}
_ASYNC_OFF = "features.send_message_to_user_async=false"


def _provenance(pin: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted({**pin, "preparation_only": _PREPARATION_ONLY}.items()))


def _overrides(material) -> list[str]:
    return [material.argv[i + 1] for i, item in enumerate(material.argv[:-1]) if item == "-c"]


def test_codex_0159_material_carries_its_own_audited_provenance(tmp_path: Path) -> None:
    parent, workspace, binary, catalog = _inputs(tmp_path)

    material = prepare_native_review_runtime(_request(binary, catalog, version="0.159.0"),
                                             runtime_root=parent / "one", workspace=workspace)

    assert material.cli_version == "0.159.0"
    assert material.provenance == _provenance(_PIN_0159)
    assert material.replay_binding()["provenance"] == {**_PIN_0159, "preparation_only": _PREPARATION_ONLY}
    assert validate_native_review_material(material) is material


def test_codex_0159_disables_send_message_to_user_async_and_0154_does_not(tmp_path: Path) -> None:
    parent, workspace, binary, catalog = _inputs(tmp_path)
    new = prepare_native_review_runtime(_request(binary, catalog, version="0.159.0"),
                                        runtime_root=parent / "new", workspace=workspace)
    old = prepare_native_review_runtime(_request(binary, catalog),
                                        runtime_root=parent / "old", workspace=workspace)

    assert _ASYNC_OFF in _overrides(new)
    assert "send_message_to_user_async = false" in Path(new.config_path).read_text()
    # 0.154.0's audited schema has no such key and --strict-config rejects unknown features.
    assert _ASYNC_OFF not in _overrides(old)
    assert "send_message_to_user_async" not in Path(old.config_path).read_text()
    # The closed list is otherwise identical: exactly one more feature override.
    def features(material) -> list[str]:
        return [item for item in _overrides(material) if item.startswith("features.")]

    assert features(new) == [*features(old), _ASYNC_OFF]
    for feature in ("shell_tool", "multi_agent_v2", "code_mode_only", "context_management"):
        assert f"features.{feature}=false" in _overrides(new)


def test_codex_0154_keeps_its_original_provenance(tmp_path: Path) -> None:
    parent, workspace, binary, catalog = _inputs(tmp_path)

    material = prepare_native_review_runtime(_request(binary, catalog),
                                             runtime_root=parent / "one", workspace=workspace)

    assert material.cli_version == "0.154.0"
    assert material.provenance == _provenance(_PIN_0154)
    assert validate_native_review_material(material) is material


@pytest.mark.parametrize("version", ["0.155.1", "0.156.1", "0.157.0", "0.158.0", "0.159.1", "0.159.0-dev",
                                     "0.160.0", "0.154.1"])
def test_every_other_codex_version_still_refuses(tmp_path: Path, version: str) -> None:
    parent, workspace, binary, catalog = _inputs(tmp_path)
    with pytest.raises(NativeReviewRuntimeRefused, match="pinned native review version"):
        prepare_native_review_runtime(_request(binary, catalog, version=version),
                                      runtime_root=parent / "one", workspace=workspace)
    assert not (parent / "one").exists()


@pytest.mark.parametrize("prepared,claimed", [("0.154.0", "0.159.0"), ("0.159.0", "0.154.0"),
                                              ("0.159.0", "0.158.0"), ("0.154.0", "0.155.1")])
def test_stored_material_cannot_change_codex_version(tmp_path: Path, prepared: str, claimed: str) -> None:
    parent, workspace, binary, catalog = _inputs(tmp_path)
    material = prepare_native_review_runtime(_request(binary, catalog, version=prepared),
                                             runtime_root=parent / "one", workspace=workspace)

    with pytest.raises(NativeReviewRuntimeRefused):
        validate_native_review_material(replace(material, cli_version=claimed))


@pytest.mark.parametrize("prepared,other", [("0.159.0", _PIN_0154), ("0.154.0", _PIN_0159)])
def test_stored_material_cannot_carry_another_versions_provenance(tmp_path: Path, prepared: str,
                                                                  other: dict[str, str]) -> None:
    parent, workspace, binary, catalog = _inputs(tmp_path)
    material = prepare_native_review_runtime(_request(binary, catalog, version=prepared),
                                             runtime_root=parent / "one", workspace=workspace)

    with pytest.raises(NativeReviewRuntimeRefused, match="provenance drifted"):
        validate_native_review_material(replace(material, provenance=_provenance(other)))
    for field in ("codex_commit", "tool_registration_sha256", "config_schema_sha256", "model_protocol_sha256"):
        mixed = {**(_PIN_0159 if prepared == "0.159.0" else _PIN_0154), field: other[field]}
        with pytest.raises(NativeReviewRuntimeRefused, match="provenance drifted"):
            validate_native_review_material(replace(material, provenance=_provenance(mixed)))


def test_codex_0159_config_without_the_async_gate_fails_validation(tmp_path: Path) -> None:
    parent, workspace, binary, catalog = _inputs(tmp_path)
    material = prepare_native_review_runtime(_request(binary, catalog, version="0.159.0"),
                                             runtime_root=parent / "one", workspace=workspace)
    stripped = "".join(line for line in Path(material.config_path).read_text().splitlines(keepends=True)
                       if "send_message_to_user_async" not in line).encode()
    Path(material.config_path).write_bytes(stripped)
    Path(material.config_path).chmod(0o600)

    with pytest.raises(NativeReviewRuntimeRefused, match="tool restriction proof"):
        validate_native_review_material(replace(material, config_sha256=hashlib.sha256(stripped).hexdigest()))
    with pytest.raises(NativeReviewRuntimeRefused):
        validate_native_review_material(replace(
            material, argv=tuple(item for item in material.argv if item != _ASYNC_OFF)))
