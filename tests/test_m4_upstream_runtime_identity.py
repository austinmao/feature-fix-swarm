"""Execution-policy identity for the pinned upstream resolver runtime.

The controller descriptor remains ``ffs.gsd-upstream-runtime/v1``.  Its
effective identity additionally binds the fixed resolver launch policy, so a
resume cannot silently change how the same registered Node and module bytes
are executed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from test_m4_upstream_binding import _runtime, _workspace
from test_m4_upstream_context_acceptance import (
    _assert_refused,
    _empty_selection,
    _env,
    _payload,
    _register,
    _registered_runtime,
    _repository,
    _runtime_flags,
    _start,
    _tree,
    _write_manifest,
)
from test_m4_upstream_environment import _resolve_with_trace


BRIDGE_SHA256 = "sha256:9310a47eedaf66ed6f547bd1090aa7e89046abb85fcf0128b98f9f3abc6d077c".removeprefix("sha256:")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _expected_identity(descriptor: dict) -> dict:
    canonical_descriptor = {
        "schema": "ffs.gsd-upstream-runtime/v1",
        "version": "1.14.0",
        "node": {
            "path": descriptor["node"]["path"],
            "sha256": descriptor["node"]["sha256"],
        },
        "module_root": descriptor["module_root"],
        "modules": dict(sorted(descriptor["modules"].items())),
    }
    return {
        "schema": "ffs.gsd-upstream-runtime-identity/v1",
        "descriptor": canonical_descriptor,
        "execution_policy": {
            "schema": "ffs.gsd-upstream-execution-policy/v1",
            "argv_prefix": ["--jitless"],
            "environment": {"OPENSSL_CONF": "", "PATH": os.defpath},
            "session_environment": ["GSD_SESSION_KEY"],
            "unset_environment": ["NODE_OPTIONS", "NODE_PATH"],
            "bridge": {
                "path": "upstream_bridge.cjs",
                "sha256": BRIDGE_SHA256,
            },
        },
    }


def test_runtime_digest_binds_descriptor_and_exact_execution_policy() -> None:
    runtime, descriptor = _runtime()

    expected = hashlib.sha256(_canonical(_expected_identity(descriptor))).hexdigest()

    assert runtime.runtime_digest == expected
    assert len(runtime.runtime_digest) == 64


def test_resolver_uses_pinned_bridge_jitless_argv_and_exact_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import run_state.upstream as upstream

    call, descriptor, _ = _resolve_with_trace(tmp_path, monkeypatch)
    bridge = Path(upstream.__file__).with_name("upstream_bridge.cjs")

    assert hashlib.sha256(bridge.read_bytes()).hexdigest() == BRIDGE_SHA256
    assert call["args"] == [[
        descriptor["node"]["path"], "--jitless", str(bridge),
    ]]
    assert call["kwargs"]["env"] == {
        "GSD_SESSION_KEY": "environment-session",
        "OPENSSL_CONF": "",
        "PATH": os.defpath,
    }


def test_changed_bridge_bytes_refuse_before_node_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import run_state.upstream as upstream

    runtime, _ = _runtime()
    workspace = _workspace(tmp_path)
    redirected_module = tmp_path / "redirected-module" / "upstream.py"
    redirected_module.parent.mkdir()
    redirected_module.write_bytes(b"# path anchor only\n")
    changed_bridge = redirected_module.with_name("upstream_bridge.cjs")
    changed_bridge.write_bytes(b"// harmless changed bridge fixture\n")
    monkeypatch.setattr(upstream, "__file__", str(redirected_module))
    monkeypatch.setattr(
        upstream.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("changed bridge reached Node launch"),
    )

    with pytest.raises(upstream.UpstreamRefused) as refused:
        upstream.resolve_upstream_binding(
            workspace,
            runtime=runtime,
            project=None,
            workstream=None,
            session_key="bridge-drift-session",
        )

    assert refused.value.code == "UPSTREAM_RUNTIME_DRIFT"


def test_same_descriptor_execution_policy_drift_refuses_resume_before_effects(
    tmp_path: Path,
) -> None:
    primary = _repository(tmp_path)
    state_root = tmp_path / "authority"
    repository_id = _register(primary, state_root)
    run_id = "resolver-execution-policy-drift"
    selection = _write_manifest(tmp_path, _empty_selection(primary, repository_id))
    env = _env(tmp_path)
    started = _start(state_root, primary, selection, run_id, env=env)
    assert started.returncode == 0, (
        f"stdout={started.stdout!r}; stderr={started.stderr!r}"
    )
    workspace = Path(_payload(started)["workspace"])
    state_before = _tree(state_root)
    workspace_before = _tree(workspace)
    runtime_path, runtime_sha256 = _registered_runtime()
    program = textwrap.dedent(
        """
        from dataclasses import replace
        import sys
        import run_state.upstream as upstream

        original_from_manifest = upstream.UpstreamRuntime.from_manifest

        def changed_policy_digest(cls, mapping):
            runtime = original_from_manifest(mapping)
            changed = "f" * 64 if runtime.runtime_digest != "f" * 64 else "e" * 64
            return replace(runtime, runtime_digest=changed)

        upstream.UpstreamRuntime.from_manifest = classmethod(changed_policy_digest)

        def resolver_must_not_run(*args, **kwargs):
            raise AssertionError("policy drift reached resolver execution")

        upstream.resolve_upstream_binding = resolver_must_not_run
        from run_state.cli import main
        raise SystemExit(main(sys.argv[1:]))
        """
    )
    resumed = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            "start",
            "--skill",
            "fix",
            "--objective",
            f"upstream {run_id}",
            "--activity",
            "plan",
            "--run-id",
            run_id,
            "--scope",
            "m4-upstream",
            "--selection-manifest",
            str(selection),
            "--resume",
            "--json",
            *_runtime_flags(runtime_path, runtime_sha256),
            "--state-root",
            str(state_root),
        ],
        cwd=primary,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    _assert_refused(resumed, "UPSTREAM_RUNTIME_CHANGED", 3)
    assert _tree(state_root) == state_before
    assert _tree(workspace) == workspace_before


def test_descriptor_schema_does_not_accept_execution_policy_from_caller() -> None:
    from run_state.upstream import UpstreamRefused, UpstreamRuntime

    _, descriptor = _runtime()
    supplied = dict(descriptor)
    supplied["execution_policy"] = {"argv_prefix": []}

    with pytest.raises(UpstreamRefused) as refused:
        UpstreamRuntime.from_manifest(supplied)

    assert refused.value.code == "UPSTREAM_INVALID"
