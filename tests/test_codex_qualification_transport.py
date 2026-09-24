"""Hermetic checks of owner-planned qualification, not live Codex admission."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from test_codex_runtime_observer import ROOT, _runtime, observer


def _plan(tmp_path: Path):
    runtime, binary = _runtime(tmp_path)
    worktree = tmp_path / "work"
    worktree.mkdir()
    return observer.prepare_qualification_plan(
        runtime, binary, worktree, runtime / "runtime-observation.json", 180,
        model="gpt-6-astra", effort="high",
    )


def _results(plan):
    stream = "\n".join(json.dumps(item) for item in (
        {"type": "thread.started", "thread_id": "unproven-fixture"},
        {"type": "turn.completed"},
    ))
    return tuple(observer.QualificationResult(probe.name, stream, "", 0)
                 for probe in plan.probes)


def test_plan_owns_only_a_probe_tmpdir_it_created_exclusively(tmp_path):
    plan = _plan(tmp_path)
    scratch = plan.worktree / ".ffs-observer-tmp"
    info = scratch.lstat()
    assert plan.scratch_identity == (info.st_dev, info.st_ino)
    # A second plan, or any pre-existing path, is not this plan's creation.
    again = observer.prepare_qualification_plan(
        plan.runtime, plan.binary, plan.worktree, plan.output, 180, model="gpt-6-astra", effort="high",
    )
    assert again.scratch_identity is None


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_plan_refuses_a_probe_tmpdir_that_is_not_a_real_directory(tmp_path, kind):
    runtime, binary = _runtime(tmp_path)
    worktree = tmp_path / "work"
    worktree.mkdir()
    scratch = worktree / ".ffs-observer-tmp"
    if kind == "file":
        scratch.write_text("x")
    else:
        (tmp_path / "elsewhere").mkdir()
        scratch.symlink_to(tmp_path / "elsewhere")

    with pytest.raises(ValueError, match="not a directory"):
        observer.prepare_qualification_plan(
            runtime, binary, worktree, runtime / "runtime-observation.json", 180,
            model="gpt-6-astra", effort="high",
        )


def test_preparation_launches_nothing_and_exposes_only_four_fixed_probes(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("qualification preparation launched a process")
    monkeypatch.setattr(observer.subprocess, "run", forbidden)
    monkeypatch.setattr(observer.subprocess, "Popen", forbidden)
    monkeypatch.setenv("HOSTILE_PARENT_ENV", "must-not-be-inherited")
    monkeypatch.setenv("PATH", "/untrusted-parent-bin")
    plan = _plan(tmp_path)
    assert tuple(probe.name for probe in plan.probes) == observer.QUALIFICATION_PROBES
    assert [probe.timeout_seconds for probe in plan.probes] == [45, 60, 45, 45]
    assert not plan.output.exists()
    for probe in plan.probes:
        assert probe.argv[0] == str(plan.binary)
        assert probe.argv[1:3] == ("exec", "--json")
        assert probe.argv[probe.argv.index("--cd") + 1] == str(plan.worktree)
        assert [probe.argv[index + 1] for index, value in enumerate(probe.argv)
                if value == "--disable"] == ["multi_agent", "multi_agent_v2", "plugins", "remote_plugin",
                                             "recommended_plugins", "plugin_sharing", "apps"]
        assert 'model="gpt-6-astra"' in probe.argv
        assert 'model_reasoning_effort="high"' in probe.argv
        env = dict(probe.environment)
        assert set(env) == {"HOME", "CODEX_HOME", "PATH", "TMPDIR", "LANG", "LC_ALL",
                            "NO_COLOR", "FFS_HOOK_OBSERVATION", "FFS_HOOK_NONCE"}
        assert "/untrusted-parent-bin" not in env["PATH"]
        assert env["FFS_HOOK_NONCE"] == plan.nonce
        assert not probe.transcript.exists()
        assert not probe.invocation.exists()
    assert 'web_search="live"' in plan.probes[1].argv
    assert 'web_search="disabled"' in plan.probes[2].argv
    assert "typeof tools.multi_agent" in plan.probes[3].argv[-1]


@pytest.mark.parametrize("defect", ["missing", "duplicate", "unknown", "dict", "boolean-exit",
                                    "string-exit", "bytes", "blank", "json", "array", "item", "incomplete"])
def test_malformed_or_missing_results_refuse_before_publishing_any_stream(tmp_path, defect):
    plan = _plan(tmp_path)
    results = list(_results(plan))
    if defect == "missing":
        results.pop()
    elif defect == "duplicate":
        results[-1] = results[0]
    elif defect == "unknown":
        results[-1] = results[-1]._replace(name="arbitrary-command")
    elif defect == "dict":
        results[-1] = results[-1]._asdict()
    elif defect == "boolean-exit":
        results[-1] = results[-1]._replace(exit_code=True)
    elif defect == "string-exit":
        results[-1] = results[-1]._replace(exit_code="0")
    elif defect == "bytes":
        results[-1] = results[-1]._replace(stdout=b"not text")
    else:
        malformed = {"blank": "", "json": "{", "array": "[]", "item": '{"type":"item.completed","item":1}',
                     "incomplete": '{"type":"thread.started"}'}
        results[-1] = results[-1]._replace(stdout=malformed[defect])
    with pytest.raises(ValueError):
        observer.publish_qualification_results(plan, tuple(results))
    assert not plan.output.exists()
    assert all(not probe.transcript.exists() and not probe.invocation.exists() for probe in plan.probes)


def test_explicit_gsd_additions_bind_the_same_policy_as_the_closed_host_environment(tmp_path, monkeypatch):
    runtime, binary = _runtime(tmp_path)
    worktree = tmp_path / "work"
    worktree.mkdir()
    admission = tmp_path / "admission.json"
    admission.write_text("{}")
    admission.chmod(0o600)
    bridge = tmp_path / "gsd_wave_bridge.py"
    bridge.write_text("# Hermetic bridge identity only; never executed.\n")
    additions = {
        "GSD_DISPATCH_MODE": "ffs-supervised-process",
        "FFS_SUPERVISED_COMMIT_MODE": "patches",
        "FFS_SUPERVISED_ADMISSION_FILE": str(admission),
        "FFS_SUPERVISED_DISPATCH_COMMAND_JSON": json.dumps([str(bridge)], separators=(",", ":")),
    }
    plan = observer.prepare_qualification_plan(runtime, binary, worktree,
                                              runtime / "runtime-observation.json", 10,
                                              gsd_environment=additions)
    for probe in plan.probes:
        assert dict(probe.environment).items() >= additions.items()
    monkeypatch.setattr(observer._shared, "current_supervisor_identity", lambda: {"fixture": True})
    record = observer.publish_qualification_results(plan, _results(plan))
    expected = observer._shared.codex_closed_environment(
        runtime, runtime / "different-invocation-tmp", binary,
        observer.executable_chain(binary), additions,
    )
    assert record["observation"]["environment_sha256"] == observer._shared.codex_environment_policy_hash(expected)


@pytest.mark.parametrize("changed", ["runtime", "binary", "workspace"])
def test_completed_results_cannot_qualify_changed_source_identity(tmp_path, changed):
    plan = _plan(tmp_path)
    if changed == "runtime":
        (plan.runtime / "config.toml").write_text("different config")
    elif changed == "binary":
        plan.binary.write_text("different executable")
    else:
        plan.worktree.rename(tmp_path / "old-work")
        plan.worktree.mkdir()
    with pytest.raises(ValueError, match="identity changed"):
        observer.publish_qualification_results(plan, _results(plan))
    assert not plan.output.exists()


def test_streams_alone_preserve_unmet_native_session_hook_proofs(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    monkeypatch.setattr(observer._shared, "current_supervisor_identity", lambda: {"fixture": True})
    monkeypatch.setattr(observer.subprocess, "run", lambda *a, **k: pytest.fail("publication launched a probe"))
    record = observer.publish_qualification_results(plan, _results(plan))
    assert record == json.loads(plan.output.read_text())
    assert record["observed"]["hooks"] is False
    assert record["observed"]["write_boundary"] is False
    assert record["observed"]["native_network_proof"] == "unmet"
    assert record["observed"]["native_multi_agent_proof"] == "unmet"
    assert record["telemetry"]["measurement"] == "unavailable"
    assert plan.output.stat().st_mode & 0o777 == 0o600
    for probe in plan.probes:
        assert probe.transcript.stat().st_mode & 0o777 == 0o600
        assert probe.invocation.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="already exists"):
        observer.publish_qualification_results(plan, _results(plan))


def test_real_fixture_transport_preserves_session_native_and_hook_derivation(tmp_path):
    runtime, binary = _runtime(tmp_path)
    shutil.copyfile(ROOT / "tests/fixtures/fake_codex_observer.py", binary)
    binary.chmod(0o700)
    worktree = tmp_path / "work"
    worktree.mkdir()
    plan = observer.prepare_qualification_plan(runtime, binary, worktree,
                                              runtime / "runtime-observation.json", 10,
                                              model="gpt-6-astra", effort="high")
    results = []
    for probe in plan.probes:
        completed = subprocess.run(probe.argv, env=dict(probe.environment), text=True,
                                   capture_output=True, timeout=probe.timeout_seconds, check=False)
        assert completed.returncode == 0, completed.stderr
        results.append(observer.QualificationResult(probe.name, completed.stdout,
                                                    completed.stderr, completed.returncode))
    record = observer.publish_qualification_results(plan, tuple(results))
    assert record["observed"]["hooks"] is True
    assert record["observed"]["skill_discovery"] is True
    assert record["observed"]["write_boundary"] is True
    assert record["observed"]["shell_denial_source"] == "persisted-session-paired"
    assert record["observed"]["native_network_proof"] == "persisted-session-paired"
    # Existing CLI fixture does not implement the multi-agent contract. The new
    # transport must not turn its unrelated output into qualification evidence.
    assert record["observed"]["native_multi_agent_proof"] == "unmet"
    assert not plan.allowed.exists()
    assert not plan.blocked.exists()


def test_failed_process_exit_is_retained_and_cannot_be_replaced_with_success(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    monkeypatch.setattr(observer._shared, "current_supervisor_identity", lambda: {"fixture": True})
    results = list(_results(plan))
    results[2] = results[2]._replace(exit_code=7, stdout='{"type":"turn.failed"}', stderr="failure")
    record = observer.publish_qualification_results(plan, tuple(results))
    assert record["native_exit_codes"]["negative"] == 7
    assert record["observed"]["native_network_denied"] is False
    invocation = json.loads(plan.probes[2].invocation.read_text())
    assert invocation["exit_code"] == 7
    assert invocation["stderr"] == "failure"
