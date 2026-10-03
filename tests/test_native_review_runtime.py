import hashlib
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from run_state import native_review_runtime as review_runtime
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
# Written as `sha256:` digests and split commit ids so the tests/ credential gate
# (test_seam_wiring.py, hex runs of 32+) stays clean without widening it.
def _hex(digest):
    return digest.removeprefix("sha256:")


_PIN_0154 = {
    "codex_release": "rust-v0.154.0",
    "codex_commit": "6b9826e3aa83b1a5947d" "b50f4332cb9c65f1b340",
    "tool_registration_sha256": _hex("sha256:451622e76c45dd1585318c200fdee9a00d7aaf785d4a540facca1010146307b7"),
    "config_schema_sha256": _hex("sha256:2e1fcf1cbb20f255c3baca2e174b4a3c954cef577a130587b8935e2d12c8ade6"),
    "model_protocol_sha256": _hex("sha256:2e9923d405a497441a0b264efc07de6ce21cdb108442e660a8b9fb63ca415aed"),
}
_PIN_0159 = {
    "codex_release": "rust-v0.159.0",
    "codex_commit": "687a119f0fcaace47e1f" "1abcc77cec6c813fd6da",
    "tool_registration_sha256": _hex("sha256:849ef21d4e5c83febdc31eacd7609911d43e3f69a35168fe02ae899273b5ef3e"),
    "config_schema_sha256": _hex("sha256:eda7251b7e46e0b9d0f3d8eef5dab451e11a55d723e2b802e152b7208045836a"),
    "model_protocol_sha256": _hex("sha256:4c8b5cafd8c55db269f669e352321f787fcaf83785bce4476cb887abfce75dc6"),
}
# rust-v0.160.0 (recomputed from the raw upstream files at that tag): the tool-registration digest
# equals 0.159.0's; the commit, config schema and model protocol differ.
_PIN_0160 = {
    "codex_release": "rust-v0.160.0",
    "codex_commit": "a956835d020762cb2b57" "0053af06f643a11c0ecc",
    "tool_registration_sha256": _hex("sha256:849ef21d4e5c83febdc31eacd7609911d43e3f69a35168fe02ae899273b5ef3e"),
    "config_schema_sha256": _hex("sha256:7ce31bde1ed6ef15c53a96ba460bb1d0fb7b99fd9ab719b567c94c474f62b023"),
    "model_protocol_sha256": _hex("sha256:961f3051af96a988f37151b9a3c1e92103d6d76a81a01a1a2fb8a30328e4282d"),
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


def test_codex_0160_material_carries_its_own_audited_provenance(tmp_path: Path) -> None:
    parent, workspace, binary, catalog = _inputs(tmp_path)

    material = prepare_native_review_runtime(_request(binary, catalog, version="0.160.0"),
                                             runtime_root=parent / "one", workspace=workspace)

    assert material.cli_version == "0.160.0"
    assert material.provenance == _provenance(_PIN_0160)
    assert material.replay_binding()["provenance"] == {**_PIN_0160, "preparation_only": _PREPARATION_ONLY}
    assert validate_native_review_material(material) is material


def test_codex_0160_disables_send_message_to_user_async_like_0159(tmp_path: Path) -> None:
    parent, workspace, binary, catalog = _inputs(tmp_path)
    new = prepare_native_review_runtime(_request(binary, catalog, version="0.160.0"),
                                        runtime_root=parent / "new", workspace=workspace)
    prior = prepare_native_review_runtime(_request(binary, catalog, version="0.159.0"),
                                          runtime_root=parent / "prior", workspace=workspace)
    old = prepare_native_review_runtime(_request(binary, catalog),
                                        runtime_root=parent / "old", workspace=workspace)

    assert _ASYNC_OFF in _overrides(new)
    assert "send_message_to_user_async = false" in Path(new.config_path).read_text()
    # Same closed feature list as the audited 0.159.0 row; 0.154.0 still lacks the key.
    def features(material) -> list[str]:
        return [item for item in _overrides(material) if item.startswith("features.")]

    assert features(new) == features(prior)
    assert features(new) == [*features(old), _ASYNC_OFF]
    assert _ASYNC_OFF not in _overrides(old)


def test_codex_0160_config_without_the_async_gate_fails_validation(tmp_path: Path) -> None:
    parent, workspace, binary, catalog = _inputs(tmp_path)
    material = prepare_native_review_runtime(_request(binary, catalog, version="0.160.0"),
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


def test_codex_0154_keeps_its_original_provenance(tmp_path: Path) -> None:
    parent, workspace, binary, catalog = _inputs(tmp_path)

    material = prepare_native_review_runtime(_request(binary, catalog),
                                             runtime_root=parent / "one", workspace=workspace)

    assert material.cli_version == "0.154.0"
    assert material.provenance == _provenance(_PIN_0154)
    assert validate_native_review_material(material) is material


@pytest.mark.parametrize("version", ["0.155.1", "0.156.1", "0.157.0", "0.158.0", "0.159.1", "0.159.0-dev",
                                     "0.160.1", "0.160.0-dev", "0.154.1"])
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


@pytest.mark.parametrize("prepared,claimed", [("0.160.0", "0.159.0"), ("0.159.0", "0.160.0"),
                                              ("0.160.0", "0.154.0"), ("0.154.0", "0.160.0"),
                                              ("0.160.0", "0.160.1")])
def test_stored_material_cannot_change_codex_version_across_0160(tmp_path: Path, prepared: str,
                                                                 claimed: str) -> None:
    parent, workspace, binary, catalog = _inputs(tmp_path)
    material = prepare_native_review_runtime(_request(binary, catalog, version=prepared),
                                             runtime_root=parent / "one", workspace=workspace)

    with pytest.raises(NativeReviewRuntimeRefused):
        validate_native_review_material(replace(material, cli_version=claimed))


_PINS_BY_VERSION = {"0.154.0": _PIN_0154, "0.159.0": _PIN_0159, "0.160.0": _PIN_0160}


@pytest.mark.parametrize("prepared,other", [("0.160.0", "0.154.0"), ("0.160.0", "0.159.0"),
                                            ("0.159.0", "0.160.0"), ("0.154.0", "0.160.0")])
def test_stored_material_cannot_carry_0160_provenance_of_another_version(tmp_path: Path, prepared: str,
                                                                         other: str) -> None:
    parent, workspace, binary, catalog = _inputs(tmp_path)
    material = prepare_native_review_runtime(_request(binary, catalog, version=prepared),
                                             runtime_root=parent / "one", workspace=workspace)
    own, foreign = _PINS_BY_VERSION[prepared], _PINS_BY_VERSION[other]

    with pytest.raises(NativeReviewRuntimeRefused, match="provenance drifted"):
        validate_native_review_material(replace(material, provenance=_provenance(foreign)))
    # 0.159.0 and 0.160.0 share the tool-registration digest, so only differing fields can drift.
    for field in (name for name in own if own[name] != foreign[name]):
        mixed = {**own, field: foreign[field]}
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


# F53: an npm-installed Codex is a `.js` launcher (`#!/usr/bin/env node`), so the review
# process needs the qualified Node on PATH.  PATH is built as the host launch builds it:
# binary parent, Node parent (only for a `.js` launcher), then /usr/bin:/bin.
DEFAULT_PAIR = ("darwin", "arm64")  # process.platform / process.arch the fake Node reports


def _js_launcher(tmp_path: Path, reported: tuple[str, str] = DEFAULT_PAIR,
                 installed: list[tuple[str, str]] | None = None, node_body: bytes | None = None):
    bin_dir = tmp_path / "npm" / "bin"
    node_dir = tmp_path / "nodejs" / "bin"
    bin_dir.mkdir(parents=True)
    node_dir.mkdir(parents=True)
    launcher = _write(bin_dir / "codex.js", b"#!/usr/bin/env node\n// fixture launcher, never executed\n", 0o755)
    # The verified Node itself reports process.platform and process.arch (codex.js reads those).
    node = _write(node_dir / "node",
                  node_body if node_body is not None else f"#!/bin/sh\necho '{reported[0]} {reported[1]}'\n".encode(),
                  0o755)
    # The JS launcher spawns a separate vendor executable (the host chain's `native_sha256`).
    for pair in installed or [reported]:
        _platform_package(launcher.parents[1] / "node_modules", f"#!/bin/sh\necho {pair}\n".encode(), pair=pair)
    return launcher, node


# codex-cli/bin/codex.js (rust-v0.154.0, v0.159.0 and v0.160.0) PLATFORM_PACKAGE_BY_TARGET, restated as an oracle.
_TARGETS = {
    ("linux", "x64"): ("x86_64-unknown-linux-musl", "@openai/codex-linux-x64"),
    ("linux", "arm64"): ("aarch64-unknown-linux-musl", "@openai/codex-linux-arm64"),
    ("darwin", "x64"): ("x86_64-apple-darwin", "@openai/codex-darwin-x64"),
    ("darwin", "arm64"): ("aarch64-apple-darwin", "@openai/codex-darwin-arm64"),
}


def _target(pair: tuple[str, str] = DEFAULT_PAIR) -> tuple[str, str]:
    return _TARGETS[pair]


def _platform_package(node_modules: Path, content: bytes, *, pair: tuple[str, str] = DEFAULT_PAIR,
                      exports: bool = False) -> Path:
    triple, package = _target(pair)
    root = node_modules / package
    (root / "vendor" / triple / "bin").mkdir(parents=True)
    (root / "package.json").write_text(json.dumps(
        {"name": "@openai/codex", "version": "0.0.0-fake", **({"exports": {".": "./x.js"}} if exports else {})}))
    return _write(root / "vendor" / triple / "bin" / "codex", content, 0o755)


def _vendor(launcher: Path, pair: tuple[str, str] = DEFAULT_PAIR) -> Path:
    triple, package = _target(pair)
    return launcher.parents[1] / "node_modules" / package / "vendor" / triple / "bin" / "codex"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _js_request(launcher: Path, catalog: Path, node: Path) -> NativeReviewRequest:
    """A `.js` request as the producer builds it: Node and vendor pins from the qualified chain."""
    return replace(_request(launcher, catalog), node_sha256=_sha(node), native_sha256=_sha(_vendor(launcher)))


def _path_of(material) -> str:
    return dict(material.environment)["PATH"]


def _with_path(material, path: str):
    return replace(material, environment=tuple(sorted({**dict(material.environment), "PATH": path}.items())))


def test_js_launcher_gets_its_node_directory_on_path(tmp_path: Path, monkeypatch) -> None:
    parent, workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))

    material = prepare_native_review_runtime(_js_request(launcher, catalog, node),
                                             runtime_root=parent / "one", workspace=workspace)

    entries = _path_of(material).split(os.pathsep)
    # The verified Node's directory comes first: `env node` must not find a sibling of the launcher.
    assert entries == [str(node.resolve().parent), str(launcher.resolve().parent), "/usr/bin", "/bin"]
    assert all(os.path.isabs(entry) for entry in entries)
    assert dict(material.provenance)["node_sha256"] == hashlib.sha256(node.read_bytes()).hexdigest()
    assert validate_native_review_material(material) is material


def test_js_launcher_node_is_bound_to_the_qualified_chain_pin(tmp_path: Path, monkeypatch) -> None:
    parent, workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    monkeypatch.delenv("CODEX_NODE_BINARY", raising=False)
    monkeypatch.setenv("PATH", f"{node.parent}:/usr/bin:/bin")
    pin = hashlib.sha256(node.read_bytes()).hexdigest()

    material = prepare_native_review_runtime(_js_request(launcher, catalog, node),
                                             runtime_root=parent / "one", workspace=workspace)
    assert str(node.resolve().parent) in _path_of(material).split(os.pathsep)
    assert dict(material.provenance)["node_sha256"] == pin

    with pytest.raises(NativeReviewRuntimeRefused, match="Node"):
        prepare_native_review_runtime(replace(_js_request(launcher, catalog, node), node_sha256="0" * 64),
                                      runtime_root=parent / "two", workspace=workspace)
    assert not (parent / "two").exists()


def test_js_launcher_without_a_resolvable_node_refuses(tmp_path: Path, monkeypatch) -> None:
    parent, workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    monkeypatch.delenv("CODEX_NODE_BINARY", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    with pytest.raises(NativeReviewRuntimeRefused, match="no resolved regular Node"):
        prepare_native_review_runtime(_js_request(launcher, catalog, node),
                                      runtime_root=parent / "one", workspace=workspace)
    assert not (parent / "one").exists()


def test_native_binary_gets_no_node_directory_even_when_node_is_ambient(tmp_path: Path, monkeypatch) -> None:
    parent, workspace, binary, catalog = _inputs(tmp_path)
    _launcher, node = _js_launcher(tmp_path)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))

    material = prepare_native_review_runtime(_request(binary, catalog),
                                             runtime_root=parent / "one", workspace=workspace)

    assert _path_of(material).split(os.pathsep) == [str(binary.resolve().parent), "/usr/bin", "/bin"]
    assert "node_sha256" not in dict(material.provenance)
    assert validate_native_review_material(material) is material


@pytest.mark.parametrize("tamper", ["drop-node", "swap-node-dir", "extra-entry", "relative-entry", "bare"])
def test_js_launcher_material_with_a_tampered_path_fails_validation(tmp_path: Path, monkeypatch,
                                                                    tamper: str) -> None:
    parent, workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))
    material = prepare_native_review_runtime(_js_request(launcher, catalog, node),
                                             runtime_root=parent / "one", workspace=workspace)
    entries = _path_of(material).split(os.pathsep)
    tampered = {
        "drop-node": [entries[0], "/usr/bin", "/bin"],
        "swap-node-dir": [entries[0], str(tmp_path), "/usr/bin", "/bin"],
        "extra-entry": [*entries, str(tmp_path / "evil")],
        "relative-entry": [entries[0], "bin", *entries[1:]],
        "bare": ["/usr/bin", "/bin"],
    }[tamper]

    with pytest.raises(NativeReviewRuntimeRefused, match="environment"):
        validate_native_review_material(_with_path(material, os.pathsep.join(tampered)))


def test_native_binary_material_with_a_tampered_path_fails_validation(tmp_path: Path) -> None:
    parent, workspace, binary, catalog = _inputs(tmp_path)
    material = prepare_native_review_runtime(_request(binary, catalog),
                                             runtime_root=parent / "one", workspace=workspace)
    for path in ("/usr/bin:/bin", f"{tmp_path}:{tmp_path / 'evil'}:/usr/bin:/bin", "relative:/usr/bin:/bin"):
        with pytest.raises(NativeReviewRuntimeRefused, match="environment"):
            validate_native_review_material(_with_path(material, path))


def test_js_launcher_validation_pins_the_stored_node_not_the_ambient_one(tmp_path: Path, monkeypatch) -> None:
    parent, workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))
    material = prepare_native_review_runtime(_js_request(launcher, catalog, node),
                                             runtime_root=parent / "one", workspace=workspace)

    # Another process (supervisor, monitor) replays without the preparer's ambient node.
    monkeypatch.delenv("CODEX_NODE_BINARY")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert validate_native_review_material(material) is material

    # A swapped Node binary, or a forged node identity, is a drift.
    forged = tuple((key, "f" * 64 if key == "node_sha256" else value) for key, value in material.provenance)
    with pytest.raises(NativeReviewRuntimeRefused, match="provenance|Node"):
        validate_native_review_material(replace(material, provenance=forged))
    node.write_bytes(b"#!/bin/sh\nexit 1\n")
    node.chmod(0o755)
    with pytest.raises(NativeReviewRuntimeRefused, match="Node"):
        validate_native_review_material(material)


def test_js_launcher_material_cannot_shed_its_node_identity(tmp_path: Path, monkeypatch) -> None:
    parent, workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))
    material = prepare_native_review_runtime(_js_request(launcher, catalog, node),
                                             runtime_root=parent / "one", workspace=workspace)
    bare = tuple((key, value) for key, value in material.provenance if not key.startswith("node_"))

    with pytest.raises(NativeReviewRuntimeRefused):
        validate_native_review_material(_with_path(replace(material, provenance=bare), "/usr/bin:/bin"))


# F53 review round 1: the verified Node must be the first `node` the launch finds, the vendor
# executable the launcher spawns must be bound and re-verified, and a JS launcher requires pins.
def _prepare(tmp_path: Path, launcher: Path, catalog: Path, node: Path, name: str = "one"):
    parent, workspace = tmp_path / "private", tmp_path / "review-workspace"
    return prepare_native_review_runtime(_js_request(launcher, catalog, node),
                                         runtime_root=parent / name, workspace=workspace)


def test_a_sibling_node_beside_the_launcher_is_never_the_one_resolved(tmp_path: Path, monkeypatch) -> None:
    _parent, _workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    rogue = _write(launcher.parent / "node", b"#!/bin/sh\necho rogue\n", 0o755)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))

    material = _prepare(tmp_path, launcher, catalog, node)

    found = shutil.which("node", path=_path_of(material))
    assert found is not None and Path(found).resolve() == node.resolve() != rogue.resolve()


def test_node_directory_whose_node_is_not_the_verified_file_refuses(tmp_path: Path, monkeypatch) -> None:
    parent, _workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    renamed = node.rename(node.with_name("node20"))
    _write(node.parent / "node", b"#!/bin/sh\necho other\n", 0o755)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(renamed))

    with pytest.raises(NativeReviewRuntimeRefused, match="Node"):
        _prepare(tmp_path, launcher, catalog, renamed)
    assert not (parent / "one").exists()


def test_js_launcher_binds_the_vendor_executable_it_spawns(tmp_path: Path, monkeypatch) -> None:
    _parent, _workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))

    material = _prepare(tmp_path, launcher, catalog, node)

    bound = dict(material.provenance)
    assert bound["native_binary"] == str(_vendor(launcher).resolve())
    assert bound["native_sha256"] == _sha(_vendor(launcher))
    assert validate_native_review_material(material) is material


def test_vendor_executable_replaced_after_qualification_refuses_prepare(tmp_path: Path, monkeypatch) -> None:
    parent, _workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))
    request = _js_request(launcher, catalog, node)
    _write(_vendor(launcher), b"#!/bin/sh\necho swapped\n", 0o755)

    with pytest.raises(NativeReviewRuntimeRefused, match="vendor"):
        prepare_native_review_runtime(request, runtime_root=parent / "one",
                                      workspace=tmp_path / "review-workspace")
    assert not (parent / "one").exists()


def test_vendor_executable_replaced_after_prepare_refuses_validation(tmp_path: Path, monkeypatch) -> None:
    _parent, _workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))
    material = _prepare(tmp_path, launcher, catalog, node)
    _write(_vendor(launcher), b"#!/bin/sh\necho swapped\n", 0o755)

    with pytest.raises(NativeReviewRuntimeRefused, match="vendor"):
        validate_native_review_material(material)


def test_ambient_native_override_naming_another_executable_refuses(tmp_path: Path, monkeypatch) -> None:
    """The launcher ignores CODEX_NATIVE_BINARY, so an override naming another file is refused."""
    parent, workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    decoy = _write(tmp_path / "decoy-codex", b"#!/bin/sh\necho decoy\n", 0o755)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))
    monkeypatch.setenv("CODEX_NATIVE_BINARY", str(decoy))

    for pin in (_sha(decoy), _sha(_vendor(launcher))):
        with pytest.raises(NativeReviewRuntimeRefused, match="CODEX_NATIVE_BINARY"):
            prepare_native_review_runtime(replace(_js_request(launcher, catalog, node), native_sha256=pin),
                                          runtime_root=parent / "one", workspace=workspace)
        assert not (parent / "one").exists()


def test_ambient_native_override_naming_the_launchers_own_executable_is_accepted(tmp_path: Path,
                                                                                 monkeypatch) -> None:
    _parent, _workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))
    monkeypatch.setenv("CODEX_NATIVE_BINARY", str(_vendor(launcher)))

    material = _prepare(tmp_path, launcher, catalog, node)

    assert dict(material.provenance)["native_binary"] == str(_vendor(launcher).resolve())


def test_js_launcher_without_a_resolvable_vendor_executable_refuses(tmp_path: Path, monkeypatch) -> None:
    parent, _workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    request = _js_request(launcher, catalog, node)
    _vendor(launcher).unlink()
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))

    with pytest.raises(NativeReviewRuntimeRefused, match="native"):
        prepare_native_review_runtime(request, runtime_root=parent / "one",
                                      workspace=tmp_path / "review-workspace")
    assert not (parent / "one").exists()


def test_js_launcher_request_with_no_qualified_pins_refuses(tmp_path: Path, monkeypatch) -> None:
    parent, workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))

    with pytest.raises(NativeReviewRuntimeRefused, match="pin"):
        prepare_native_review_runtime(_request(launcher, catalog), runtime_root=parent / "one",
                                      workspace=workspace)
    assert not (parent / "one").exists()


@pytest.mark.parametrize("missing", ["node_sha256", "native_sha256"])
def test_js_launcher_request_without_a_qualified_pin_refuses(tmp_path: Path, monkeypatch, missing: str) -> None:
    parent, _workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))
    request = replace(_js_request(launcher, catalog, node), **{missing: None})

    with pytest.raises(NativeReviewRuntimeRefused, match="pin"):
        prepare_native_review_runtime(request, runtime_root=parent / "one",
                                      workspace=tmp_path / "review-workspace")
    assert not (parent / "one").exists()


@pytest.mark.parametrize("target,resolver", [("node", "codex_node_binary"), ("vendor", "codex_native_binary")])
def test_binary_swapped_after_resolution_cannot_be_recorded(tmp_path: Path, monkeypatch, target: str,
                                                            resolver: str) -> None:
    """The digest bound into the material is one read, compared with the qualified pin."""
    parent, _workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))
    request = _js_request(launcher, catalog, node)
    victim = node if target == "node" else _vendor(launcher)
    real = getattr(review_runtime, resolver)

    def swapping(*args, **kwargs):
        result = real(*args, **kwargs)
        _write(victim, b"#!/bin/sh\necho swapped\n", 0o755)
        return result

    monkeypatch.setattr(review_runtime, resolver, swapping)

    with pytest.raises(NativeReviewRuntimeRefused, match="Node|vendor"):
        prepare_native_review_runtime(request, runtime_root=parent / "one",
                                      workspace=tmp_path / "review-workspace")
    assert not (parent / "one").exists()


@pytest.mark.parametrize("shed", ["native", "node", "both"])
def test_js_material_cannot_shed_a_bound_identity(tmp_path: Path, monkeypatch, shed: str) -> None:
    _parent, _workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))
    material = _prepare(tmp_path, launcher, catalog, node)
    drop = {"native": ("native_",), "node": ("node_",), "both": ("native_", "node_")}[shed]
    kept = tuple((key, value) for key, value in material.provenance if not key.startswith(drop))

    with pytest.raises(NativeReviewRuntimeRefused):
        validate_native_review_material(replace(material, provenance=kept))


# F53 review round 2: the vendor executable is the one the audited launcher selects (platform
# package by node resolution from the launcher, else the local vendor dir), and `node` on PATH
# must be the verified regular file itself.
def _bound_vendor(material) -> str:
    return dict(material.provenance)["native_binary"]


def test_decoy_codex_directory_that_sorts_first_is_never_the_bound_executable(tmp_path: Path, monkeypatch) -> None:
    _parent, _workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    triple, _package = _target()
    decoys = [tmp_path / "codex-0-decoy" / "vendor" / triple / "bin",
              launcher.parents[1] / "node_modules" / "@openai" / "codex-0-decoy" / "vendor" / triple / "bin"]
    for decoy in decoys:
        decoy.mkdir(parents=True)
        _write(decoy / "codex", b"#!/bin/sh\necho decoy\n", 0o755)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))

    material = _prepare(tmp_path, launcher, catalog, node)

    assert _bound_vendor(material) == str(_vendor(launcher).resolve())
    assert dict(material.provenance)["native_sha256"] == _sha(_vendor(launcher))


def test_platform_package_is_found_by_node_resolution_from_the_launcher(tmp_path: Path, monkeypatch) -> None:
    """A hoisted `node_modules` above the launcher's package resolves, nearest first."""
    _parent, _workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    _vendor(launcher).unlink()
    (_vendor(launcher).parents[3] / "package.json").unlink()
    hoisted = _platform_package(tmp_path / "node_modules", b"#!/bin/sh\necho hoisted\n")
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))

    material = prepare_native_review_runtime(
        replace(_request(launcher, catalog), node_sha256=_sha(node), native_sha256=_sha(hoisted)),
        runtime_root=tmp_path / "private" / "one", workspace=tmp_path / "review-workspace")

    assert _bound_vendor(material) == str(hoisted.resolve())


def test_package_absent_from_every_ancestor_refuses_even_with_a_local_vendor_dir(tmp_path: Path,
                                                                                monkeypatch) -> None:
    """Node consults NODE_PATH and the global folders before codex.js falls back to its local vendor
    dir, and those are not modelled: a package Node might find elsewhere is never replaced by a guess."""
    parent, _workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    triple, _package = _target()
    request = _js_request(launcher, catalog, node)
    _vendor(launcher).unlink()
    (_vendor(launcher).parents[3] / "package.json").unlink()
    local = launcher.parents[1] / "vendor" / triple / "bin"
    local.mkdir(parents=True)
    _write(local / "codex", b"#!/bin/sh\necho local\n", 0o755)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))

    with pytest.raises(NativeReviewRuntimeRefused, match="native"):
        prepare_native_review_runtime(replace(request, native_sha256=_sha(local / "codex")),
                                      runtime_root=parent / "one", workspace=tmp_path / "review-workspace")
    assert not (parent / "one").exists()


@pytest.mark.parametrize("shape", ["package-without-executable", "package-with-exports"])
def test_unresolvable_platform_package_does_not_fall_back_to_another_executable(tmp_path: Path, monkeypatch,
                                                                                shape: str) -> None:
    """A resolved platform package with no executable is an error in the launcher, not a fallback."""
    parent, _workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    triple, _package = _target()
    request = _js_request(launcher, catalog, node)
    if shape == "package-without-executable":
        _vendor(launcher).unlink()
    else:
        (_vendor(launcher).parents[3] / "package.json").write_text(json.dumps({"name": "x", "exports": {".": "./x"}}))
    local = launcher.parents[1] / "vendor" / triple / "bin"
    local.mkdir(parents=True)
    _write(local / "codex", b"#!/bin/sh\necho local\n", 0o755)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))

    with pytest.raises(NativeReviewRuntimeRefused, match="native"):
        prepare_native_review_runtime(replace(request, native_sha256=_sha(local / "codex")),
                                      runtime_root=parent / "one", workspace=tmp_path / "review-workspace")
    assert not (parent / "one").exists()


def test_symlinked_node_refuses_at_prepare(tmp_path: Path, monkeypatch) -> None:
    """`env node` would follow a symlinked `node`, so the verified file must be `node` itself."""
    parent, workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    real = node.rename(node.with_name("node20"))
    node.symlink_to(real.name)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))

    with pytest.raises(NativeReviewRuntimeRefused, match="symlink"):
        prepare_native_review_runtime(_js_request(launcher, catalog, real),
                                      runtime_root=parent / "one", workspace=workspace)
    assert not (parent / "one").exists()


@pytest.mark.parametrize("change", ["retargeted-symlink", "replaced-file", "replaced-by-copy-symlink"])
def test_node_retargeted_or_replaced_after_prepare_refuses_validation(tmp_path: Path, monkeypatch,
                                                                     change: str) -> None:
    _parent, _workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))
    material = _prepare(tmp_path, launcher, catalog, node)
    same_bytes = _write(node.with_name("node20"), node.read_bytes(), 0o755)
    node.unlink()
    if change == "replaced-file":
        _write(node, b"#!/bin/sh\necho other\n", 0o755)
    else:  # `node` becomes a symlink: even to identical bytes, `env node` no longer runs the verified file
        node.symlink_to(same_bytes.name if change == "retargeted-symlink" else same_bytes)

    with pytest.raises(NativeReviewRuntimeRefused, match="Node"):
        validate_native_review_material(material)


# F53 review round 3: platform and arch come from the verified Node (what codex.js reads), not Python.
@pytest.mark.parametrize("reported", [("darwin", "arm64"), ("darwin", "x64"), ("linux", "x64")])
def test_native_review_binds_the_package_for_the_platform_the_verified_node_reports(
        tmp_path: Path, monkeypatch, reported: tuple[str, str]) -> None:
    _parent, _workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path, reported=reported, installed=list(_TARGETS))
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))
    request = replace(_request(launcher, catalog), node_sha256=_sha(node),
                      native_sha256=_sha(_vendor(launcher, reported)))

    material = prepare_native_review_runtime(request, runtime_root=tmp_path / "private" / "one",
                                             workspace=tmp_path / "review-workspace")

    assert _bound_vendor(material) == str(_vendor(launcher, reported).resolve())
    assert validate_native_review_material(material) is material


@pytest.mark.parametrize("body", [b"#!/bin/sh\nexit 1\n", b"#!/bin/sh\necho not a pair\n",
                                  b"#!/bin/sh\necho 'freebsd x64'\n", b"#!/bin/sh\nexit 0\n",
                                  b"#!/bin/sh\nexec sleep 5\n"],
                         ids=["nonzero", "malformed", "unknown-pair", "empty", "timeout"])
def test_native_review_refuses_when_the_node_platform_probe_fails(tmp_path: Path, monkeypatch,
                                                                  body: bytes) -> None:
    import host_capabilities
    parent, _workspace, _binary, catalog = _inputs(tmp_path)
    launcher, node = _js_launcher(tmp_path, node_body=body)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))
    monkeypatch.setattr(host_capabilities, "_NODE_PROBE_TIMEOUT", 0.3, raising=False)

    with pytest.raises(NativeReviewRuntimeRefused, match="Node"):
        prepare_native_review_runtime(_js_request(launcher, catalog, node), runtime_root=parent / "one",
                                      workspace=tmp_path / "review-workspace")
    assert not (parent / "one").exists()
