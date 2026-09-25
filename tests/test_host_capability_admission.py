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


def _normalized_policy_hash(policy: dict, root: str) -> str:
    """Normalize away machine-specific values (the tmp root, the interpreter
    path) before hashing, so a literal golden stays valid across machines
    and OSes (review round 4: the prior literals embedded sys.executable and
    a macOS-only /private/tmp path, so they broke on Linux CI)."""
    # The placeholders must not already occur in the raw policy, or a policy
    # that emitted them literally would normalize to the same golden.
    assert not any("<ROOT>" in value or "<PY>" in value for value in policy.values())
    normalized = {
        key: value.replace(root, "<ROOT>").replace(sys.executable, "<PY>")
        for key, value in policy.items()
    }
    # "sha256:<hex>" is the credential gate's audited pinned-digest shape
    # (tests/test_seam_wiring.py _WHITELIST_SHAPES); a bare 64-hex literal
    # would trip its hex-run family.
    return "sha256:" + hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def test_default_additions_are_byte_identical(tmp_path):
    """F34 5.4: with no scope, as_dict() returns exactly the 4 keys and the
    normalized policy hash (tmp root -> "<ROOT>", sys.executable -> "<PY>")
    matches a LITERAL sha256 computed at origin/main 59bff1d (pre-F34) with
    the SAME normalization (review round 3 item 4, made machine-independent
    per review round 4) -- not a value recomputed by the current code under
    test -- so this proves byte-identity with the pre-F34 policy across
    machines, not just internal self-consistency. Rules out emitting ""
    scope keys, which would drift every retained runtime to
    ENVIRONMENT_POLICY_DRIFT. Recomputed identical at 3e8f422/HEAD.
    Computation: compute_golden_normalized.py (session scratchpad), run
    against a detached worktree of 59bff1d and against HEAD."""
    spec = importlib.util.spec_from_file_location(
        "ffs_host_default_additions", ROOT / "lib/host_capabilities.py"
    )
    assert spec and spec.loader
    admission = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = admission
    spec.loader.exec_module(admission)

    root = str(tmp_path.resolve())
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
    assert "GSD_PROJECT" not in environment and "GSD_WORKSTREAM" not in environment

    policy = admission.codex_environment_policy(environment)
    golden_hash = "sha256:d78700f4efa3756d5ab9823e80825c6832e4b562288a3d8437b330e0a3708e65"
    assert _normalized_policy_hash(policy, root) == golden_hash
    # The normalized-and-hand-hashed policy must still be the exact same
    # dict the real production wrapper hashes.
    assert admission.codex_environment_policy_hash(environment) == admission.closed_environment_hash(policy)

    scoped = admission.GsdSupervisorEnvironment(
        "ffs-supervised-process", "patches", str(admission_file), command_json,
        project="demo-project", workstream=None,
    )
    scoped_environment = {**environment, **scoped.as_dict()}
    scoped_policy = admission.codex_environment_policy(scoped_environment)
    assert _normalized_policy_hash(scoped_policy, root) != golden_hash


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


def _scope_fixture(tmp_path, spec_name):
    spec = importlib.util.spec_from_file_location(spec_name, ROOT / "lib/host_capabilities.py")
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
    return admission, base


def test_present_scope_key_with_a_none_value_refuses(tmp_path):
    """Review round 1 item 1: a PRESENT GSD_PROJECT/GSD_WORKSTREAM key whose
    value is not a str (including None) must refuse CapabilityError, not be
    silently treated as an omitted key. None is only valid for a key that is
    genuinely absent (the dataclass field default), never for a key that IS
    present in the dict with a None value."""
    admission, base = _scope_fixture(tmp_path, "ffs_host_scope_none_value")
    with pytest.raises(admission.CapabilityError):
        admission.validate_gsd_supervisor_environment({**base, "GSD_PROJECT": None})
    with pytest.raises(admission.CapabilityError):
        admission.validate_gsd_supervisor_environment({**base, "GSD_WORKSTREAM": None})
    with pytest.raises(admission.CapabilityError):
        admission.validate_gsd_supervisor_environment({**base, "GSD_PROJECT": 7})


def test_preview_hash_refuses_a_present_scope_key_with_a_none_value(tmp_path):
    """Review round 1 item 1, applied at preview_gsd_codex_environment_policy_hash:
    it must iterate PRESENT scope keys, not silently drop a None value."""
    admission, base = _scope_fixture(tmp_path, "ffs_host_preview_scope_none_value")
    codex_base = {
        "HOME": str(tmp_path), "CODEX_HOME": str(tmp_path), "TMPDIR": str(tmp_path / "tmp"),
        "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "NO_COLOR": "1",
    }
    with pytest.raises(admission.CapabilityError):
        admission.preview_gsd_codex_environment_policy_hash({**codex_base, **base, "GSD_PROJECT": None})


def test_scope_segment_rejects_a_lone_surrogate_without_crashing(tmp_path):
    """Review round 1 item 2: the ASCII segment regex must run BEFORE the
    byte-length check, so a lone surrogate refuses CapabilityError instead of
    letting UnicodeEncodeError escape from value.encode("utf-8")."""
    admission, base = _scope_fixture(tmp_path, "ffs_host_scope_surrogate")
    with pytest.raises(admission.CapabilityError):
        admission.validate_gsd_supervisor_environment({**base, "GSD_PROJECT": "\ud800"})


def test_process_environment_carries_optional_scope_when_present(tmp_path, monkeypatch):
    """Review round 1 item 6: gsd_supervisor_environment_from_process must
    carry an ambient GSD_PROJECT/GSD_WORKSTREAM into the returned object
    (validated), instead of silently dropping them."""
    admission, base = _scope_fixture(tmp_path, "ffs_host_process_scope")
    for key, value in base.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GSD_PROJECT", "demo-project")
    monkeypatch.delenv("GSD_WORKSTREAM", raising=False)
    result = admission.gsd_supervisor_environment_from_process()
    assert result is not None
    assert result.project == "demo-project"
    assert result.workstream is None

    monkeypatch.setenv("GSD_PROJECT", "../x")
    with pytest.raises(admission.CapabilityError):
        admission.gsd_supervisor_environment_from_process()


def test_process_environment_partial_required_set_refuses_typed(tmp_path, monkeypatch):
    """Review round 3 item 1: gsd_supervisor_environment_from_process must
    raise CapabilityError, not KeyError, when the process env has only SOME
    of the 4 required keys set (e.g. only GSD_DISPATCH_MODE). 899a5fa used
    os.environ.get for the required keys (missing -> None, caught downstream
    as CapabilityError); round 2 regressed this to direct os.environ[key]
    indexing, which raises a raw KeyError for a partial set."""
    spec = importlib.util.spec_from_file_location(
        "ffs_host_partial_process_env", ROOT / "lib/host_capabilities.py"
    )
    assert spec and spec.loader
    admission = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = admission
    spec.loader.exec_module(admission)
    for key in ("GSD_DISPATCH_MODE", "FFS_SUPERVISED_COMMIT_MODE", "FFS_SUPERVISED_ADMISSION_FILE",
                "FFS_SUPERVISED_DISPATCH_COMMAND_JSON", "GSD_PROJECT", "GSD_WORKSTREAM"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GSD_DISPATCH_MODE", "ffs-supervised-process")
    with pytest.raises(admission.CapabilityError):
        admission.gsd_supervisor_environment_from_process()


def test_codex_policy_closed_set_lower_bound_and_unsafe_scope(tmp_path):
    """Review round 1 items 12+14 (Codex half): a lone GSD_PROJECT (missing
    the 4 required GSD keys), and GSD_PROJECT="../x" with all 4 required keys
    present, both refuse CapabilityError at codex_environment_policy and at
    preview_gsd_codex_environment_policy_hash -- never KeyError."""
    admission, base = _scope_fixture(tmp_path, "ffs_host_codex_lower_bound")
    codex_base = {
        "HOME": str(tmp_path), "CODEX_HOME": str(tmp_path), "TMPDIR": str(tmp_path / "tmp"),
        "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "NO_COLOR": "1",
    }

    lone_scope = {**codex_base, "GSD_PROJECT": "demo-project"}
    with pytest.raises(admission.CapabilityError):
        admission.codex_environment_policy(lone_scope)
    with pytest.raises(admission.CapabilityError):
        admission.preview_gsd_codex_environment_policy_hash(lone_scope)

    unsafe_scope = {**codex_base, **base, "GSD_PROJECT": "../x"}
    with pytest.raises(admission.CapabilityError):
        admission.codex_environment_policy(unsafe_scope)
    with pytest.raises(admission.CapabilityError):
        admission.preview_gsd_codex_environment_policy_hash(unsafe_scope)


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
