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
