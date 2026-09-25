"""An installed-looking bundle is insufficient evidence for host admission."""

from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_without_observed_cli_capabilities_is_not_admitted(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "ffs_host_admission", ROOT / "lib/host_capabilities.py"
    )
    assert spec and spec.loader
    admission = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = admission
    spec.loader.exec_module(admission)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    worktree = tmp_path / "workspace"
    worktree.mkdir()
    (runtime / "config.toml").write_text(
        'approval_policy = "never"\nsandbox_mode = "workspace-write"\n'
        'web_search = "disabled"\nproject_doc_max_bytes = 0\n\n'
        '[sandbox_workspace_write]\nnetwork_access = false\n'
        'exclude_slash_tmp = true\nexclude_tmpdir_env_var = true\n'
        f'writable_roots = {json.dumps([str(worktree)])}\n'
        f'\n[projects."{worktree}"]\ntrust_level = "untrusted"\n'
    )
    auth = runtime / "auth.json"
    auth.write_text('{}\n')
    auth.chmod(0o600)
    (runtime / "hooks.json").write_text(json.dumps({
        "hooks": {event: [{"hooks": [{"type": "command", "command": "true"}]}]
                  for event in ["SessionStart", "Stop", "PostToolUse",
                                "PreToolUse", "UserPromptSubmit"]}
    }))
    (runtime / "skills").mkdir()
    (runtime / "agents").mkdir()
    (runtime / "gsd-core").mkdir()
    (runtime / "scripts").mkdir()
    (runtime / "gsd-file-manifest.json").write_text("{}\n")

    # Files have the expected shape, but no real CLI has registered a skill,
    # fired a hook, demonstrated tool denial, or authenticated this bundle.
    # In particular, a JSON manifest claiming capabilities cannot replace
    # runtime-bound evidence produced by the supervisor's actual probes.
    with pytest.raises(admission.CapabilityError, match="observation"):
        admission.verify_runtime(runtime, worktree, "workspace-write", False, [str(worktree)])


def test_cli_static_admission_requires_the_native_multi_agent_disable_surface(tmp_path):
    spec = importlib.util.spec_from_file_location("ffs_host_disable_surface", ROOT / "lib/host_capabilities.py")
    assert spec and spec.loader
    admission = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = admission
    spec.loader.exec_module(admission)
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n"
                      "if [ \"$1\" = \"--version\" ]; then echo 'codex 0.154.0'; exit 0; fi\n"
                      "echo '--strict-config --ignore-user-config --ignore-rules --sandbox --add-dir --dangerously-bypass-hook-trust'\n")
    binary.chmod(0o700)
    with pytest.raises(admission.CapabilityError, match="--disable"):
        admission.admit_cli(str(binary))


def test_artifact_review_material_is_closed_and_uses_the_typed_model_resolver(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "ffs_host_admission_material", ROOT / "lib/host_capabilities.py"
    )
    assert spec and spec.loader
    admission = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = admission
    spec.loader.exec_module(admission)
    material = admission.build_artifact_review_material(
        host="claude", model_request={"kind": "tier", "name": "judgment"},
        config_sha256="a" * 64, policy_sha256="b" * 64,
        environment={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin", "TMPDIR": str(tmp_path / "tmp")},
        selected_artifacts={"input.json": hashlib.sha256(b"input\\n").hexdigest()},
        selected_contents={"input.json": "input\\n"},
        provenance={"selected_input_sha256": "d" * 64},
    )
    assert material.replay_binding()["effective_model"] == "claude-opus-5"
    assert material.replay_binding()["effective_effort"] is None
    assert "input.json" not in material.replay_binding().values()
    prompt = json.loads(material.prompt.split("\n", 1)[1])
    assert prompt["artifacts"] == [{
        "name": "input.json", "sha256": hashlib.sha256(b"input\\n").hexdigest(),
        "encoding": "utf-8", "contents": "input\\n",
    }]
    with pytest.raises(admission.CapabilityError, match="not closed"):
        admission.build_artifact_review_material(
            host="claude", model_request={"kind": "tier", "name": "judgment"},
            config_sha256="a" * 64, policy_sha256="b" * 64,
            environment={"HOME": "/private/home", "PATH": "/usr/bin:/bin", "TMPDIR": "/private/tmp", "SECRET": "no"},
            selected_artifacts={"input.json": hashlib.sha256(b"input\\n").hexdigest()},
            selected_contents={"input.json": "input\\n"}, provenance={"selected_input_sha256": "d" * 64},
        )


def test_default_additions_are_byte_identical(tmp_path):
    """F34 5.4: with no scope, as_dict() returns exactly the 4 keys and the
    policy hash matches an independently-built policy dict. Rules out
    emitting "" scope keys, which would drift every retained runtime to
    ENVIRONMENT_POLICY_DRIFT."""
    spec = importlib.util.spec_from_file_location(
        "ffs_host_default_additions", ROOT / "lib/host_capabilities.py"
    )
    assert spec and spec.loader
    admission = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = admission
    spec.loader.exec_module(admission)

    home = tmp_path.resolve() / "home"
    home.mkdir(parents=True)
    admission_file = home / "admission.json"
    admission_file.write_text('{"schema":"ffs.supervisor-admission/v1","available":true}\n')
    admission_file.chmod(0o600)
    bridge = home / "gsd_wave_bridge.py"
    bridge.write_text("#!/usr/bin/env python3\n")
    command_json = json.dumps([sys.executable, str(bridge)], ensure_ascii=True, separators=(",", ":"))

    additions = admission.GsdSupervisorEnvironment(
        "ffs-supervised-process", "patches", str(admission_file), command_json,
        project=None, workstream=None,
    )
    assert additions.project is None and additions.workstream is None
    assert set(additions.as_dict()) == {
        "GSD_DISPATCH_MODE", "FFS_SUPERVISED_COMMIT_MODE",
        "FFS_SUPERVISED_ADMISSION_FILE", "FFS_SUPERVISED_DISPATCH_COMMAND_JSON",
    }

    environment = {
        "HOME": str(home), "CODEX_HOME": str(home), "TMPDIR": str(home / "policy-tmp"),
        "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "NO_COLOR": "1",
        **additions.as_dict(),
    }
    golden_policy = {
        "HOME": str(home), "CODEX_HOME": str(home), "TMPDIR": str(home),
        "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "NO_COLOR": "1",
        "GSD_DISPATCH_MODE": "ffs-supervised-process", "FFS_SUPERVISED_COMMIT_MODE": "patches",
        "FFS_SUPERVISED_ADMISSION_FILE": str(admission_file.resolve().parent / "<admission>"),
        "FFS_SUPERVISED_DISPATCH_COMMAND_JSON": command_json,
    }
    golden_hash = hashlib.sha256(
        json.dumps(golden_policy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert admission.codex_environment_policy_hash(environment) == golden_hash

    scoped = admission.GsdSupervisorEnvironment(
        "ffs-supervised-process", "patches", str(admission_file), command_json,
        project="demo-project", workstream=None,
    )
    scoped_environment = {**environment, **scoped.as_dict()}
    assert admission.codex_environment_policy_hash(scoped_environment) != golden_hash


def test_scope_keys_validated_and_set_stays_closed(tmp_path):
    """F34 5.5: GSD_PROJECT="../x" raises; an extra GSD_SESSION_KEY raises; a
    lone GSD_WORKSTREAM is accepted. Rules out widening the check to `>=`."""
    spec = importlib.util.spec_from_file_location(
        "ffs_host_scope_validation", ROOT / "lib/host_capabilities.py"
    )
    assert spec and spec.loader
    admission = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = admission
    spec.loader.exec_module(admission)

    admission_file = tmp_path / "admission.json"
    admission_file.write_text('{"schema":"ffs.supervisor-admission/v1","available":true}\n')
    admission_file.chmod(0o600)
    bridge = tmp_path / "gsd_wave_bridge.py"
    bridge.write_text("#!/usr/bin/env python3\n")
    command_json = json.dumps([sys.executable, str(bridge)], ensure_ascii=True, separators=(",", ":"))
    base = {
        "GSD_DISPATCH_MODE": "ffs-supervised-process",
        "FFS_SUPERVISED_COMMIT_MODE": "patches",
        "FFS_SUPERVISED_ADMISSION_FILE": str(admission_file),
        "FFS_SUPERVISED_DISPATCH_COMMAND_JSON": command_json,
    }

    with pytest.raises(admission.CapabilityError):
        admission.validate_gsd_supervisor_environment({**base, "GSD_PROJECT": "../x"})

    with pytest.raises(admission.CapabilityError, match="closed"):
        admission.validate_gsd_supervisor_environment({**base, "GSD_SESSION_KEY": "s"})

    additions = admission.validate_gsd_supervisor_environment({**base, "GSD_WORKSTREAM": "demo-ws"})
    assert additions.workstream == "demo-ws"
    assert additions.project is None


def test_content_contract_accepts_empty_and_large_text_but_bounds_encoded_json(tmp_path):
    spec = importlib.util.spec_from_file_location("ffs_host_content_bounds", ROOT / "lib/host_capabilities.py")
    assert spec and spec.loader
    admission = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = admission
    spec.loader.exec_module(admission)
    common = dict(host="claude", model_request={"kind": "tier", "name": "judgment"},
                  config_sha256="a" * 64, policy_sha256="b" * 64,
                  environment={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin", "TMPDIR": str(tmp_path / "tmp")},
                  provenance={"snapshot": "d" * 64})
    for content in ("", "é" * 5000):
        material = admission.build_artifact_review_material(
            **common, selected_artifacts={"input.txt": hashlib.sha256(content.encode()).hexdigest()},
            selected_contents={"input.txt": content},
        )
        assert json.loads(material.prompt.split("\n", 1)[1])["artifacts"][0]["contents"] == content
    escaped = '"' * 17000
    with pytest.raises(admission.CapabilityError, match="bounded input limit"):
        admission.build_artifact_review_material(
            **common, selected_artifacts={"input.txt": hashlib.sha256(escaped.encode()).hexdigest()},
            selected_contents={"input.txt": escaped},
        )
