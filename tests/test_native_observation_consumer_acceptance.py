"""Retained native evidence consumer regressions; fixtures are not live canaries."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import time
import uuid

import pytest

import host_capabilities as admission

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "native_consumer_fixture_observer", ROOT / "scripts/gsd/codex-runtime-observer.py"
)
assert SPEC and SPEC.loader
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)


def _rows(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    path.chmod(0o600)


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path):
    home = tmp_path / "runtime"
    home.mkdir(mode=0o700)
    work = tmp_path / "workspace"
    work.mkdir(mode=0o700)
    admission.render_runtime_config(home / "config.toml", work, "workspace-write", False, [str(work)])
    (home / "auth.json").write_text("{}\n")
    (home / "auth.json").chmod(0o600)
    (home / "hooks.json").write_text(json.dumps({"hooks": {name: [] for name in observer.HOOKS}}))
    for directory in ("skills", "agents", "gsd-core", "scripts"):
        (home / directory).mkdir(mode=0o700)
        (home / directory / "fixture").write_text(directory)
    (home / "gsd-file-manifest.json").write_text("{}\n")
    binary = tmp_path / "inert-codex"
    binary.write_text("inert binary-hash fixture; never executed\n")
    binary.chmod(0o700)
    sessions = home / "sessions" / "2026" / "09" / "15"
    sessions.mkdir(parents=True, mode=0o700)
    nonce = uuid.uuid4().hex
    transcripts, rollouts = {}, {}
    for mode in ("positive", "negative", "multi_agent"):
        thread, turn = str(uuid.uuid4()), str(uuid.uuid4())
        call = "fixture-call-" + mode
        transcript = home / f"native-{mode.replace('_', '-')}-{nonce}.jsonl"
        _rows(transcript, [{"type": "thread.started", "thread_id": thread}, {"type": "turn.completed"}])
        if mode == "positive":
            program = "text({nonce:" + json.dumps(nonce) + ",available:typeof tools.web__run==='function'})"
            machine, output = {"nonce": nonce, "available": True}, "Script completed\n"
        elif mode == "negative":
            program = "text({nonce:" + json.dumps(nonce) + ",attempt:true});await tools.web__run({time:[{utc_offset:'+00:00'}]})"
            machine, output = {"nonce": nonce, "attempt": True}, "Script failed\n"
        else:
            program = ("text({nonce:" + json.dumps(nonce) + ",multi_agent:typeof tools.multi_agent==='function',"
                       "multi_agent_v2:typeof tools.multi_agent_v2==='function'})")
            machine, output = {"nonce": nonce, "multi_agent": False, "multi_agent_v2": False}, "Script completed\n"
        output += json.dumps(machine)
        if mode == "negative":
            output += "\nTypeError: tools.web__run is not a function"
        rollout = sessions / f"rollout-2026-09-15-{thread}.jsonl"
        _rows(rollout, [
            {"type": "session_meta", "payload": {"id": thread, "cwd": str(work)}},
            {"type": "turn_context", "payload": {"turn_id": turn, "cwd": str(work), "model": "fixture-model", "effort": "medium"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "namespace": "functions", "call_id": call, "input": program,
                "internal_chat_message_metadata_passthrough": {"turn_id": turn}}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": call,
                "output": [{"type": "input_text", "text": output}], "internal_chat_message_metadata_passthrough": {"turn_id": turn}}},
        ])
        transcripts[mode], rollouts[mode] = transcript, rollout
    proved, artifacts = observer._native_proof(
        transcripts["positive"], transcripts["negative"], runtime=home, nonce=nonce,
        worktree=work, model="fixture-model", effort="medium",
    )
    assert proved and artifacts, "Fixture must satisfy the real producer proof before adversarial mutation"
    multi_proved, multi_artifacts = observer._native_multi_agent_proof(
        transcripts["multi_agent"], runtime=home, nonce=nonce,
        worktree=work, model="fixture-model", effort="medium",
    )
    assert multi_proved and multi_artifacts, "Fixture must prove disabled native multi-agent tools"
    artifacts.update(multi_artifacts)
    shell_probe = observer.prepare_shell_probe(home)
    artifacts["shell_probe_sha256"] = _hash(shell_probe)
    for name in ("ordinary", "native-positive", "native-negative", "native-multi-agent"):
        invocation = home / f"observer-{name}-invocation-{nonce}.json"
        disabled = [item for feature in observer.DISABLED_FEATURES for item in ("--disable", feature)]
        observer.write_private(invocation, {"argv": [str(binary), "exec", *disabled]})
        artifacts[name.replace("-", "_") + "_invocation_sha256"] = _hash(invocation)
    record = {
        "schema": observer.SCHEMA,
        "runtime": observer.runtime_hashes(home), "binary": observer.executable_chain(binary),
        "workspace": observer.workspace_identity(work), "supervisor": admission.current_supervisor_identity(),
        "execution": {"model": "fixture-model", "effort": "medium", "sandbox": "workspace-write",
                      "network_enabled": False, "roots": [str(work)],
                      "disabled_features": list(observer.DISABLED_FEATURES)},
        "observation": {"id": nonce, "created_at_unix": time.time(),
                        "environment_sha256": admission.closed_environment_hash({}),
                        "telemetry_schema": admission.TELEMETRY_SCHEMA},
        "telemetry": {"schema": admission.TELEMETRY_SCHEMA, "measurement": "unavailable"},
        "artifacts": artifacts,
        # Structural envelope isolates native evidence validation. These labels
        # do not assert that this fixture ran auth, hooks, or a native process.
        "observed": {"auth": True, "skill_discovery": True, "hooks": True, "write_boundary": True,
            "shell_denied": True, "sandbox_policy": "workspace-write", "hook_events": sorted(observer.HOOKS),
            "native_network_denied": True, "native_network_proof": "persisted-session-paired",
            "native_multi_agent_denied": True, "native_multi_agent_proof": "persisted-session-paired"},
    }
    observation = home / "runtime-observation.json"
    observer.write_private(observation, record)
    return SimpleNamespace(home=home, work=work, binary=binary, nonce=nonce, transcripts=transcripts,
                           rollouts=rollouts, record=record, observation=observation)


def _verify(f):
    return admission.verify_runtime(f.home, f.work, "workspace-write", False, [str(f.work)],
                                    str(f.binary), "fixture-model", "medium")


def _save(f):
    observer.write_private(f.observation, f.record)


def test_public_consumer_accepts_fully_linked_retained_fixture(tmp_path):
    qualified = _verify(_fixture(tmp_path))
    assert isinstance(qualified, admission.QualifiedCodexRuntime)
    assert qualified["status"] == "admitted"
    assert qualified.to_dict()["execution"]["disabled_features"] == list(observer.DISABLED_FEATURES)


@pytest.mark.parametrize("defect", ["stale", "workspace-inode", "host-boot", "observation-id", "disabled-outcome", "disabled-invocation"])
def test_public_consumer_refuses_unfresh_or_unbound_qualified_runtime_fields(tmp_path, defect):
    f = _fixture(tmp_path)
    if defect == "stale":
        f.record["observation"]["created_at_unix"] -= admission.OBSERVATION_FRESHNESS_SECONDS + 1
    elif defect == "workspace-inode":
        f.record["workspace"]["inode"] += 1
    elif defect == "host-boot":
        f.record["supervisor"]["boot_id"] = "another-boot"
    elif defect == "observation-id":
        f.record["observation"]["id"] = uuid.uuid4().hex
    elif defect == "disabled-outcome":
        f.record["observed"]["native_multi_agent_denied"] = False
    else:
        path = f.home / f"observer-native-multi-agent-invocation-{f.nonce}.json"
        invocation = json.loads(path.read_text())
        invocation["argv"] = [item for item in invocation["argv"] if item != "multi_agent_v2"]
        observer.write_private(path, invocation)
        f.record["artifacts"]["native_multi_agent_invocation_sha256"] = _hash(path)
    _save(f)
    with pytest.raises(admission.CapabilityError):
        _verify(f)


@pytest.mark.parametrize("kind", ["transcript", "session"])
@pytest.mark.parametrize("mode", ["positive", "negative"])
@pytest.mark.parametrize("defect", ["missing", "stale", "symlink", "hardlink", "oversize"])
def test_public_consumer_refuses_missing_stale_or_unsafe_retained_file(tmp_path, kind, mode, defect):
    f = _fixture(tmp_path)
    path = (f.transcripts if kind == "transcript" else f.rollouts)[mode]
    if defect == "missing":
        path.unlink()
    elif defect == "stale":
        path.write_bytes(path.read_bytes() + b'\n{"unrelated":"changed after observation"}\n')
    elif defect in ("symlink", "hardlink"):
        outside = tmp_path / "outside-artifact"
        path.rename(outside)
        path.symlink_to(outside) if defect == "symlink" else os.link(outside, path)
    else:
        path.write_bytes(b" " * (2 * 1024 * 1024 + 1))
        field = "sha256" if kind == "transcript" else "session_sha256"
        f.record["artifacts"][f"native_{mode}_{field}"] = _hash(path)
        _save(f)
    with pytest.raises(admission.CapabilityError):
        _verify(f)


@pytest.mark.parametrize("defect", ["model", "effort", "workspace", "thread", "turn", "call", "output", "nonce", "program",
                                   "duplicate-output", "extra-tool", "agent-only", "malformed", "nonobject", "utf8"])
def test_public_consumer_revalidates_semantics_even_with_fresh_session_hash(tmp_path, defect):
    f = _fixture(tmp_path)
    path = f.rollouts["negative"]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if defect in ("model", "effort"):
        rows[1]["payload"][defect] = "another-value"
    elif defect == "workspace":
        rows[1]["payload"]["cwd"] = str(tmp_path / "sibling")
    elif defect == "thread":
        rows[0]["payload"]["id"] = "another-thread"
    elif defect in ("turn", "call"):
        if defect == "turn":
            rows[3]["payload"]["internal_chat_message_metadata_passthrough"]["turn_id"] = "another-turn"
        else:
            rows[3]["payload"]["call_id"] = "another-call"
    elif defect == "output":
        rows[3]["payload"]["output"] = [{"type": "input_text", "text": "agent claims denial"}]
    elif defect in ("nonce", "program"):
        rows[2]["payload"]["input"] = rows[2]["payload"]["input"].replace(f.nonce, uuid.uuid4().hex) if defect == "nonce" else "text('claimed denial')"
    elif defect == "duplicate-output":
        rows.append(copy.deepcopy(rows[3]))
    elif defect == "extra-tool":
        rows.append({"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "call_id": "extra",
                     "internal_chat_message_metadata_passthrough": {"turn_id": rows[1]["payload"]["turn_id"]}}})
    elif defect == "agent-only":
        rows[3]["payload"]["type"] = "message"
    _rows(path, rows)
    if defect == "malformed":
        path.write_bytes(b'{"type":')
    elif defect == "nonobject":
        path.write_bytes(b'[]\n')
    elif defect == "utf8":
        path.write_bytes(b'\xff\n')
    f.record["artifacts"]["native_negative_session_sha256"] = _hash(path)
    _save(f)
    with pytest.raises(admission.CapabilityError):
        _verify(f)


@pytest.mark.parametrize("defect", ["pair", "rollout", "same-thread", "claimed-turn", "claimed-call", "wildcard-thread", "missing-digest", "malformed-digest"])
def test_public_consumer_refuses_ambiguous_or_unbound_native_claims(tmp_path, defect):
    f = _fixture(tmp_path)
    if defect == "pair":
        other_nonce = uuid.uuid4().hex
        for mode, path in f.transcripts.items():
            _rows(f.home / f"native-{mode}-{other_nonce}.jsonl", [json.loads(line) for line in path.read_text().splitlines()])
    elif defect == "rollout":
        path = f.rollouts["negative"]
        _rows(path.with_name("rollout-duplicate-" + path.name.removeprefix("rollout-2026-09-15-")),
              [json.loads(line) for line in path.read_text().splitlines()])
    elif defect == "same-thread":
        f.record["artifacts"]["native_negative_thread_id"] = f.record["artifacts"]["native_positive_thread_id"]
    elif defect in ("claimed-turn", "claimed-call", "wildcard-thread"):
        field = {"claimed-turn": "turn_id", "claimed-call": "call_id", "wildcard-thread": "thread_id"}[defect]
        f.record["artifacts"]["native_negative_" + field] = "*" if defect == "wildcard-thread" else "unrelated-id"
    elif defect == "missing-digest":
        del f.record["artifacts"]["native_negative_sha256"]
    else:
        f.record["artifacts"]["native_negative_session_sha256"] = "not-a-digest"
    _save(f)
    with pytest.raises(admission.CapabilityError):
        _verify(f)



@pytest.mark.parametrize("defect", ["public-transcript", "writable-session", "duplicate-machine", "boolean-machine",
                                   "metadata-array", "metadata-null", "output-array-member", "output-nonlist"])
def test_public_consumer_refuses_private_mode_and_nested_machine_defects(tmp_path, defect):
    f = _fixture(tmp_path)
    path = f.rollouts["negative"]
    if defect == "public-transcript":
        f.transcripts["negative"].chmod(0o644)
    elif defect == "writable-session":
        path.chmod(0o664)
    else:
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        if defect == "duplicate-machine":
            rows[3]["payload"]["output"][0]["text"] = ("Script failed\n{\"nonce\":\"unrelated\",\"nonce\":"
                + json.dumps(f.nonce) + ",\"attempt\":true}\nTypeError: tools.web__run is not a function")
        elif defect == "boolean-machine":
            rows[3]["payload"]["output"][0]["text"] = ("Script failed\n" + json.dumps({"nonce": f.nonce, "attempt": 1})
                + "\nTypeError: tools.web__run is not a function")
        elif defect.startswith("metadata-"):
            rows[3]["payload"]["internal_chat_message_metadata_passthrough"] = [] if defect == "metadata-array" else None
        elif defect == "output-array-member":
            rows[3]["payload"]["output"] = [None]
        else:
            rows[3]["payload"]["output"] = {"type": "input_text", "text": "claimed denial"}
        _rows(path, rows)
        f.record["artifacts"]["native_negative_session_sha256"] = _hash(path)
        _save(f)
    with pytest.raises(admission.CapabilityError):
        _verify(f)


def test_public_consumer_refuses_session_parent_replacement_during_anchored_read(tmp_path, monkeypatch):
    import shutil

    f = _fixture(tmp_path)
    path = f.rollouts["negative"]
    target = path.stat()
    native_fstat = os.fstat
    calls = 0
    swapped = False
    def swap_after_read(fd):
        nonlocal calls, swapped
        info = native_fstat(fd)
        if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
            calls += 1
            if calls == 2:
                swapped = True
                old_parent = tmp_path / "old-session-parent"
                path.parent.rename(old_parent)
                path.parent.mkdir(mode=0o700)
                for old in old_parent.iterdir():
                    shutil.copy2(old, path.parent / old.name)
                path.write_text('[]\n')
                path.chmod(0o600)
        return info
    monkeypatch.setattr(os, "fstat", swap_after_read)
    rejected = None
    try:
        _verify(f)
    except admission.CapabilityError as error:
        rejected = error
    assert swapped, "Race oracle must actually replace the consumed session parent after its read"
    assert _hash(path) != f.record["artifacts"]["native_negative_session_sha256"]
    assert rejected is not None, "Consumer admitted stale anchored bytes after visible parent replacement"



@pytest.mark.parametrize("layout", ["source", "flat-installed"])
@pytest.mark.parametrize("retained", [True, False], ids=["valid-pair", "missing-session"])
def test_real_cli_replays_native_evidence_in_source_and_installed_layout(tmp_path, layout, retained):
    import shutil
    import subprocess
    import sys

    f = _fixture(tmp_path)
    if layout == "source":
        host = ROOT / "lib/host_capabilities.py"
        observer_path = ROOT / "scripts/gsd/codex-runtime-observer.py"
    else:
        # Actual installer shape: helpers are flat at the managed library root,
        # while runner/observer scripts retain scripts/gsd below that root.
        managed = tmp_path / "managed-root"
        managed.mkdir(mode=0o700)
        host = managed / "host_capabilities.py"
        observer_path = managed / "scripts/gsd/codex-runtime-observer.py"
        observer_path.parent.mkdir(parents=True, mode=0o700)
        shutil.copy2(ROOT / "lib/host_capabilities.py", host)
        shutil.copy2(ROOT / "lib/model_requests.py", managed / "model_requests.py")
        shutil.copy2(ROOT / "lib/process_identity.py", managed / "process_identity.py")
        shutil.copy2(ROOT / "scripts/gsd/codex-runtime-observer.py", observer_path)
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
           "LANG", "LC_ALL", "PYTHONDONTWRITEBYTECODE", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL") if key in os.environ}
    # No checkout PYTHONPATH can rescue a broken installed-relative import.
    load = subprocess.run([sys.executable, str(observer_path), "--help"], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=10)
    assert load.returncode == 0, load.stderr
    if not retained:
        f.rollouts["negative"].unlink()
    result = subprocess.run([sys.executable, str(host), "runtime", str(f.home), str(f.work),
                             "workspace-write", "false", str(f.work), "--binary", str(f.binary),
                             "--model", "fixture-model", "--effort", "medium"],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    if retained:
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["schema"] == admission.QUALIFIED_RUNTIME_SCHEMA
        assert payload["status"] == "admitted"
        assert payload["execution"]["disabled_features"] == list(observer.DISABLED_FEATURES)
    else:
        assert result.returncode == 78, (result.stdout, result.stderr)
        assert result.stdout == ""
        assert "host-capabilities:" in result.stderr and "native" in result.stderr
