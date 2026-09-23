"""Credential-copy/material fixtures only; no native authentication or launch."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import uuid

import pytest

from host_capabilities import build_artifact_review_material
from run_state.claude_host import ClaudeLaunchMaterial
from run_state.codex_host import CodexLaunchMaterial
from run_state.native_review_runtime import (
    CLAUDE_CLI_VERSION, CODEX_CLI_VERSION, NativeReviewRequest, prepare_native_review_runtime,
)
from run_state.native_review_transport import (
    NativeReviewTransportRefused, prepare_native_review_launch,
    read_native_review_launch, validate_native_review_launch_material,
)
from run_state.state import qualified_runtime_tuple_hash
from test_runtime_receipt_authority import _qualified, _qualified_claude


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _write(path, data, mode=0o600):
    path.write_bytes(data)
    path.chmod(mode)
    return path


def _inputs(tmp_path, host, *, output_contract=None):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    ordinary_home = private / "ordinary"
    ordinary_home.mkdir(mode=0o700)
    binary = _write(tmp_path / host, b"#!/bin/sh\nexit 0\n", 0o700)
    binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    model = "gpt-5.6-terra" if host == "codex" else "claude-opus-5"
    version = CODEX_CLI_VERSION if host == "codex" else CLAUDE_CLI_VERSION
    artifact = build_artifact_review_material(
        host=host, model_request={"kind": "exact", "id": model},
        config_sha256="a" * 64, policy_sha256="b" * 64,
        environment={"HOME": str(private), "PATH": "/usr/bin:/bin", "TMPDIR": str(private)},
        selected_artifacts={"candidate.txt": hashlib.sha256(b"fixture candidate").hexdigest()},
        selected_contents={"candidate.txt": "fixture candidate"}, provenance={"fixture": "yes"},
        output_contract=output_contract,
    )
    qualified = (_qualified if host == "codex" else _qualified_claude)(workspace)
    runtime = dict(qualified.runtime)
    runtime.update(path=str(ordinary_home), device=ordinary_home.stat().st_dev,
                   inode=ordinary_home.stat().st_ino)
    execution = dict(qualified.execution)
    execution.update(model=model, effort=artifact.effective_effort)
    qualified = replace(qualified, binary=(("launcher_sha256", binary_hash),),
                        runtime=tuple(sorted(runtime.items())), execution=tuple(sorted(execution.items())))
    if host == "claude":
        observation = dict(qualified.observation)
        observation["version"] = version
        qualified = replace(qualified, observation=tuple(sorted(observation.items())))
    tuple_hash = qualified_runtime_tuple_hash(qualified)
    ordinary_sha = _hash(qualified.to_dict())
    source = _write(ordinary_home / ("auth.json" if host == "codex" else ".credentials.json"),
                    b'{"fixture":"dummy credential only"}')
    identity = source.stat()
    common = dict(binary=qualified.binary, version=version, argv=(str(binary),),
                  environment=(), cwd=str(workspace), model=model, effort=artifact.effective_effort,
                  runtime=qualified, attempt=0, temporary_dir=str(ordinary_home),
                  temporary_device=ordinary_home.stat().st_dev,
                  temporary_inode=ordinary_home.stat().st_ino, runtime_sha256=ordinary_sha)
    guard = dict(path=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                 device=identity.st_dev, inode=identity.st_ino)
    session = str(uuid.uuid4()) if host == "claude" else None
    if host == "codex":
        ordinary = CodexLaunchMaterial(**common, config_sha256="a" * 64,
                                       **{"auth_" + key: value for key, value in guard.items()})
        catalog = _write(private / "models.json", json.dumps({"models": [{"slug": model}]}).encode())
        catalog_fields = dict(catalog_path=str(catalog),
                              catalog_sha256=hashlib.sha256(catalog.read_bytes()).hexdigest())
    else:
        ordinary = ClaudeLaunchMaterial(**common, session_id=session, environment_sha256="a" * 64,
                                        **{"credential_" + key: value for key, value in guard.items()})
        catalog_fields = {}
    native = prepare_native_review_runtime(NativeReviewRequest(
        host=host, requested_model=model, cli_version=version, binary=str(binary),
        binary_sha256=binary_hash, runtime_identity=tuple_hash, prompt=artifact.prompt,
        effort=artifact.effective_effort, session_id=session, **catalog_fields),
        runtime_root=private / "native", workspace=workspace)
    return native, artifact, ordinary, source


def _stage(inputs):
    native, artifact, ordinary, _source = inputs
    return prepare_native_review_launch(native=native, artifact=artifact, ordinary=ordinary,
                                        runtime_receipt_sha256=_hash(ordinary.runtime.to_dict()))


def _retained_material(tmp_path, host="codex"):
    launch = _stage(_inputs(tmp_path, host))
    raw = json.dumps(launch.to_dict(), sort_keys=True, separators=(",", ":")).encode() + b"\n"
    return launch, _write(tmp_path / "private" / "launch.json", raw)


@pytest.mark.parametrize("host", ["codex", "claude"])
def test_retained_launch_round_trip_before_and_after_revocation(tmp_path, host):
    launch, path = _retained_material(tmp_path, host)
    expected = launch.material_sha256()
    assert read_native_review_launch(path, expected_material_sha256=expected, credential_required=True) == launch
    with pytest.raises(NativeReviewTransportRefused):
        read_native_review_launch(path, expected_material_sha256=expected)
    Path(launch.credential_path).unlink()
    assert read_native_review_launch(path, expected_material_sha256=expected) == launch
    with pytest.raises(NativeReviewTransportRefused):
        read_native_review_launch(path, expected_material_sha256=expected, credential_required=True)


@pytest.mark.parametrize("mutation", ["wrong-sha", "indent", "newline", "mode", "symlink", "oversize",
                                    "null", "scalar", "list", "invalid-utf8"])
def test_retained_launch_rejects_changed_or_malformed_file(tmp_path, mutation):
    launch, path = _retained_material(tmp_path)
    expected = launch.material_sha256()
    if mutation == "wrong-sha":
        expected = "0" * 64
    elif mutation == "indent":
        path.write_text(json.dumps(launch.to_dict(), indent=2) + "\n")
    elif mutation == "newline":
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
    elif mutation == "mode":
        path.chmod(0o644)
    elif mutation == "symlink":
        target = path.with_name("target.json")
        path.rename(target)
        path.symlink_to(target)
    else:
        path.write_bytes({"oversize": b" " * (2 * 1024 * 1024 + 1), "null": b"null",
                          "scalar": b"42", "list": b"[]", "invalid-utf8": b"\xff"}[mutation])
    with pytest.raises(NativeReviewTransportRefused):
        read_native_review_launch(path, expected_material_sha256=expected, credential_required=True)


@pytest.mark.parametrize("host", ["codex", "claude"])
def test_typed_runtime_fixture_stages_exact_private_copy_and_full_material_binding(tmp_path, host):
    inputs = _inputs(tmp_path, host)
    native, _artifact, ordinary, source = inputs
    launch = _stage(inputs)
    target = Path(launch.credential_path)
    assert target.read_bytes() == source.read_bytes()
    assert target.stat().st_mode & 0o777 == 0o600
    assert target != source
    assert launch.runtime_tuple_hash == native.runtime_identity
    assert launch.ordinary_runtime_sha256 == _hash(ordinary.runtime.to_dict())
    assert launch.runtime_tuple_hash != launch.ordinary_runtime_sha256
    serialized = asdict(launch)
    serialized['artifact'].pop('review_context_json')
    assert launch.to_dict() == serialized
    assert launch.material_sha256() == _hash(serialized)
    assert "dummy credential" not in json.dumps(launch.replay_binding())
    assert validate_native_review_launch_material(
        launch, expected_material_sha256=launch.material_sha256()) is launch
    assert launch.native.effort == ("high" if host == "codex" else None)
    if host == "claude":
        assert "--effort" not in launch.native.argv
        assert target.parent == Path(native.runtime_root) / "claude-config"


@pytest.mark.parametrize("host", ["codex", "claude"])
@pytest.mark.parametrize("field", ["runtime_tuple_hash", "artifact_prompt_sha256",
                                   "source_credential_inode", "credential_path"])
def test_material_substitution_cannot_self_certify(tmp_path, host, field):
    launch = _stage(_inputs(tmp_path, host))
    expected = launch.material_sha256()
    changed = str(tmp_path / "outside") if field == "credential_path" else (
        launch.source_credential_inode + 1 if field == "source_credential_inode" else "0" * 64)
    changed = replace(launch, **{field: changed})
    with pytest.raises(NativeReviewTransportRefused, match="binding drifted"):
        validate_native_review_launch_material(changed, expected_material_sha256=expected)
    if field in {"runtime_tuple_hash", "artifact_prompt_sha256"}:
        with pytest.raises(NativeReviewTransportRefused, match="bindings differ"):
            validate_native_review_launch_material(changed, expected_material_sha256=changed.material_sha256())


@pytest.mark.parametrize("host", ["codex", "claude"])
@pytest.mark.parametrize("fault", ["hash", "inode", "mode", "symlink", "foreign_path", "runtime_hash",
                                   "duplicate_binary"])
def test_source_guard_drift_refuses_without_credential_copy(tmp_path, host, fault):
    native, artifact, ordinary, source = _inputs(tmp_path, host)
    if fault == "hash":
        source.write_bytes(b"changed fixture credential")
    elif fault == "inode":
        other = _write(source.with_name("replacement"), source.read_bytes())
        other.replace(source)
    elif fault == "mode":
        source.chmod(0o644)
    elif fault == "symlink":
        other = _write(source.with_name("replacement"), source.read_bytes())
        source.unlink()
        source.symlink_to(other)
    elif fault == "foreign_path":
        other = _write(tmp_path / "foreign-credential", source.read_bytes())
        info = other.stat()
        prefix = "auth_" if host == "codex" else "credential_"
        ordinary = replace(ordinary, **{prefix + "path": str(other),
                                       prefix + "device": info.st_dev, prefix + "inode": info.st_ino})
    elif fault == "duplicate_binary":
        ordinary = replace(ordinary, binary=ordinary.binary + ordinary.binary)
    else:
        ordinary = replace(ordinary, runtime_sha256="0" * 64)
    with pytest.raises(NativeReviewTransportRefused):
        _stage((native, artifact, ordinary, source))
    target = Path(native.runtime_root) / ("auth.json" if host == "codex" else "claude-config/.credentials.json")
    assert not target.exists()


@pytest.mark.parametrize("host", ["codex", "claude"])
@pytest.mark.parametrize("fault", ["hash", "inode", "mode", "symlink", "missing"])
def test_target_guard_drift_refuses_before_launch(tmp_path, host, fault):
    launch = _stage(_inputs(tmp_path, host))
    target = Path(launch.credential_path)
    if fault == "hash":
        target.write_bytes(b"modified")
    elif fault == "inode":
        other = _write(target.with_name("replacement"), target.read_bytes())
        other.replace(target)
    elif fault == "mode":
        target.chmod(0o400)
    elif fault == "symlink":
        other = _write(target.with_name("replacement"), target.read_bytes())
        target.unlink()
        target.symlink_to(other)
    else:
        target.unlink()
    with pytest.raises(NativeReviewTransportRefused):
        validate_native_review_launch_material(launch, expected_material_sha256=launch.material_sha256())


@pytest.mark.parametrize("host", ["codex", "claude"])
def test_postlaunch_requires_revocation_and_preserves_source(tmp_path, host):
    inputs = _inputs(tmp_path, host)
    launch = _stage(inputs)
    expected = launch.material_sha256()
    with pytest.raises(NativeReviewTransportRefused, match="revocation"):
        validate_native_review_launch_material(launch, expected_material_sha256=expected,
                                              credential_required=False)
    Path(launch.credential_path).unlink()
    assert validate_native_review_launch_material(
        launch, expected_material_sha256=expected, credential_required=False) is launch
    assert inputs[-1].read_bytes() == b'{"fixture":"dummy credential only"}'
