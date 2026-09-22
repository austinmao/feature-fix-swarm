"""Hermetic environment contract for the admitted upstream Node resolver."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from test_m4_upstream_binding import _runtime, _workspace


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _runtime_snapshot(descriptor: dict) -> dict[str, str]:
    descriptor_path = Path(os.environ["FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR"])
    module_root = Path(descriptor["module_root"])
    result = {
        "descriptor": _file_sha256(descriptor_path),
        "node": _file_sha256(Path(descriptor["node"]["path"])),
    }
    for relative in sorted(descriptor["modules"]):
        result[f"module:{relative}"] = _file_sha256(module_root / relative)
    return result


def _resolve_with_trace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import run_state.upstream as upstream

    runtime, descriptor = _runtime()
    original_run = upstream.subprocess.run
    calls: list[dict] = []

    def traced_run(*args, **kwargs):
        calls.append({
            "args": list(args),
            "kwargs": {**kwargs, "env": dict(kwargs["env"])},
        })
        return original_run(*args, **kwargs)

    monkeypatch.setattr(upstream.subprocess, "run", traced_run)
    workspace = _workspace(tmp_path)
    binding = upstream.resolve_upstream_binding(
        workspace,
        runtime=runtime,
        project=None,
        workstream=None,
        session_key="environment-session",
    )
    assert binding.session_key == "environment-session"
    assert len(calls) == 1
    return calls[0], descriptor, workspace


@pytest.mark.parametrize("ambient", ["absent", "hostile"])
def test_resolver_pins_empty_openssl_config_and_drops_ambient_node_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ambient: str,
) -> None:
    hostile_config = "/opt/homebrew/etc/openssl@3/openssl.cnf"
    if ambient == "hostile":
        monkeypatch.setenv("OPENSSL_CONF", hostile_config)
        monkeypatch.setenv("NODE_OPTIONS", "--require=/ambient/not-authority.cjs")
        monkeypatch.setenv("NODE_PATH", "/ambient/not-authority-modules")
    else:
        monkeypatch.delenv("OPENSSL_CONF", raising=False)
        monkeypatch.delenv("NODE_OPTIONS", raising=False)
        monkeypatch.delenv("NODE_PATH", raising=False)

    call, _, _ = _resolve_with_trace(tmp_path, monkeypatch)

    assert call["kwargs"]["env"] == {
        "GSD_SESSION_KEY": "environment-session",
        "OPENSSL_CONF": "",
        "PATH": os.defpath,
    }
    encoded_call = json.dumps(call, default=str, sort_keys=True)
    assert hostile_config not in encoded_call
    assert "NODE_OPTIONS" not in call["kwargs"]["env"]
    assert "NODE_PATH" not in call["kwargs"]["env"]


def test_resolver_execution_preserves_registered_runtime_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime_value, descriptor = _runtime()
    before = _runtime_snapshot(descriptor)

    call, observed_descriptor, workspace = _resolve_with_trace(tmp_path, monkeypatch)

    assert observed_descriptor == descriptor
    assert _runtime_snapshot(descriptor) == before
    assert call["kwargs"]["check"] is False
    assert call["kwargs"]["cwd"] == workspace
    assert call["kwargs"]["env"]["OPENSSL_CONF"] == ""
