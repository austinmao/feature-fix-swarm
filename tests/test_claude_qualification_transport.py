"""Hermetic checks for the owner-planned Claude qualification transport."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from run_state import claude_qualification as qualification
from run_state import managed_claude_qualification as managed
from run_state.claude_host import ClaudeHostRequest
from run_state.claude_runtime_staging import STAGE_MANIFEST_NAME, STAGE_SCHEMA


def _fixture(tmp_path: Path, *, version="2.1.274"):
    runtime, workspace = tmp_path / "runtime", tmp_path / "work"
    runtime.mkdir(mode=0o700)
    workspace.mkdir()
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    (runtime / "settings.json").write_text(json.dumps({"sandbox": {"filesystem": {
        "denyRead": [str(runtime / ".credentials.json")],
        "denyWrite": [str(runtime / ".credentials.json")],
    }}}))
    (runtime / "settings.json").chmod(0o600)
    (runtime / ".credentials.json").write_text('{"claudeAiOauth":{}}')
    (runtime / ".credentials.json").chmod(0o600)
    stage = {"schema": STAGE_SCHEMA, "source": {}, "target": {}}
    (runtime / STAGE_MANIFEST_NAME).write_text(json.dumps(stage))
    (runtime / STAGE_MANIFEST_NAME).chmod(0o600)
    plan = qualification.prepare_claude_qualification_plan(
        runtime, binary, workspace, runtime / "qualification.json", version=version,
        model="claude-opus-5", effort=None,
    )
    return plan


def test_native_review_cli_can_qualify_ordinary_runtime(tmp_path):
    """Native review must accept the same version as its prerequisite qualification."""
    from run_state.native_review_runtime import CLAUDE_CLI_VERSION

    plan = _fixture(tmp_path, version=CLAUDE_CLI_VERSION)
    assert plan.version == CLAUDE_CLI_VERSION
    assert tuple(probe.name for probe in plan.probes) == qualification.QUALIFICATION_PROBES


def _stream(plan, probe, *, hook=False, nested=False):
    records = [{"type": "system", "subtype": "init", "session_id": probe.session_id,
                "model": plan.model, "claude_code_version": "2.1.274"}]
    if hook:
        records.append({"type": "system", "subtype": "hook_started", "hook_event": "PreToolUse:Bash"})
        records.append({"type": "assistant", "message": {"content": [{
            "text": plan.sandbox_command + "\nFFS_WRITE_RC=1 FFS_NETWORK_RC=6"}]}})
        records.append({"type": "system", "subtype": "hook_response", "hook_event": "PostToolUse:Bash"})
    if nested:
        records.append({"type": "assistant", "message": {"content": [{"text": '{"loggedIn":false}'}]}})
    records.append({"type": "result", "subtype": "success", "is_error": False,
                    "session_id": probe.session_id, "usage": {"input_tokens": 1,
                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                    "output_tokens": 1}})
    return "\n".join(json.dumps(item) for item in records) + "\n"


def _results(plan):
    by_name = {probe.name: probe for probe in plan.probes}
    return (
        qualification.QualificationResult("auth-negative", '{"loggedIn":false}', "", 1),
        qualification.QualificationResult("session-model", _stream(plan, by_name["session-model"]), "", 0),
        qualification.QualificationResult("sandbox-hooks", _stream(plan, by_name["sandbox-hooks"], hook=True), "", 0),
        qualification.QualificationResult("nested-auth", _stream(plan, by_name["nested-auth"], nested=True), "", 0),
    )


def _consume_probe_credentials(plan):
    for probe in plan.probes[1:]:
        Path(str(probe.credential_path)).unlink()


def test_preparation_launches_nothing_and_exposes_only_fixed_probes(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("qualification launched a process"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("qualification launched a process"))
    plan = _fixture(tmp_path)
    assert tuple(item.name for item in plan.probes) == qualification.QUALIFICATION_PROBES
    assert plan.probes[0].argv[1:] == ("auth", "status", "--json")
    assert ".credentials.json" not in {path.name for path in
                                       (plan.runtime / ".ffs-claude-noauth").iterdir()}
    for probe in plan.probes[1:]:
        assert "--output-format" in probe.argv and "stream-json" in probe.argv
        assert probe.argv[probe.argv.index("--model") + 1] == "claude-opus-5"
        assert "--fallback-model" not in probe.argv
        probe_home = Path(dict(probe.environment)["CLAUDE_CONFIG_DIR"])
        assert probe_home.parent == plan.runtime
        assert probe.credential_path == str(probe_home / ".credentials.json")
        assert probe.credential_sha256
        assert probe.credential_inode
    assert plan.probes[0].credential_path is None
    assert str(plan.binary) + " auth status --json" in " ".join(plan.probes[-1].argv)


@pytest.mark.parametrize("defect", ["content", "mode", "hardlink", "symlink"])
def test_final_admission_replacement_rejects_unsafe_placeholder(tmp_path, defect):
    path = tmp_path / "supervisor-admission.json"
    expected = {"schema": "ffs.supervisor-admission/v1", "runtime_identity": "qualification"}
    admitted = {**expected, "runtime_identity": "a" * 64}
    path.write_text(json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n")
    path.chmod(0o600)
    if defect == "content":
        path.write_text("{}\n")
    elif defect == "mode":
        path.chmod(0o644)
    elif defect == "hardlink":
        os.link(path, tmp_path / "second-link")
    else:
        target = tmp_path / "target"
        path.replace(target)
        path.symlink_to(target)
    with pytest.raises(managed.ManagedClaudeQualificationRefused, match="ADMISSION_CONFLICT"):
        managed._replace_qualification_admission(path, expected, admitted)


def test_results_publish_qualified_runtime_only_after_all_boundaries(tmp_path, monkeypatch):
    plan = _fixture(tmp_path)
    plan.inside_sentinel.write_text("FFS_INSIDE")
    _consume_probe_credentials(plan)
    monkeypatch.setattr(qualification, "current_supervisor_identity", lambda: {
        "host_id": "fixture", "boot_id": "fixture", "pid": 1, "start_token": "1"})
    runtime = qualification.publish_claude_qualification_results(plan, _results(plan))
    assert runtime.status == "admitted"
    assert runtime.to_dict()["execution"]["model"] == "claude-opus-5"
    assert runtime.to_dict()["observation"]["auth_negative"] is True
    assert runtime.to_dict()["observation"]["nested_auth_denied"] is True
    assert plan.output.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("defect", ["missing", "order", "ambient-auth", "nested-auth", "outside", "model"])
def test_incomplete_or_unsafe_results_fail_closed(tmp_path, monkeypatch, defect):
    plan = _fixture(tmp_path)
    plan.inside_sentinel.write_text("FFS_INSIDE")
    _consume_probe_credentials(plan)
    results = list(_results(plan))
    if defect == "missing":
        results.pop()
    elif defect == "order":
        results[1], results[2] = results[2], results[1]
    elif defect == "ambient-auth":
        results[0] = results[0]._replace(stdout='{"loggedIn":true}', exit_code=0)
    elif defect == "nested-auth":
        results[3] = results[3]._replace(stdout=results[3].stdout.replace("false", "true"))
    elif defect == "outside":
        plan.outside_sentinel.write_text("FFS_OUTSIDE")
    else:
        results[1] = results[1]._replace(stdout=results[1].stdout.replace("claude-opus-5", "claude-sonnet-5"))
    monkeypatch.setattr(qualification, "current_supervisor_identity", lambda: {"fixture": True})
    with pytest.raises(ValueError):
        qualification.publish_claude_qualification_results(plan, tuple(results))
    assert not plan.output.exists()


def test_managed_probe_material_binds_each_isolated_claude_profile(tmp_path, monkeypatch):
    """The supervisor validates the profile that actually carries each probe credential."""
    candidate, workspace, evidence = tmp_path / "candidate", tmp_path / "work", tmp_path / "evidence"
    candidate.mkdir()
    workspace.mkdir()
    evidence.mkdir()
    owned = candidate / "gsd-core" / "bin" / "gsd-tools.cjs"
    owned.parent.mkdir(parents=True)
    owned.write_text("fixture")
    (candidate / "settings.json").write_text("{}")
    (candidate / "gsd-file-manifest.json").write_text(json.dumps({
        "version": "1.14.0", "runtime": "claude",
        "files": {"gsd-core/bin/gsd-tools.cjs": qualification._digest(owned)},
    }))
    credential = tmp_path / "credential.json"
    credential.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "fixture-access", "refreshToken": "excluded-refresh",
        "expiresAt": 9999999999999, "refreshTokenExpiresAt": 9999999999999,
        "scopes": ["user:inference"], "subscriptionType": "fixture",
        "rateLimitTier": "fixture",
    }}))
    credential.chmod(0o600)
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    bridge = tmp_path / "gsd_wave_bridge.py"
    bridge.write_text("# fixture\n")

    class Store:
        def get_run_policy_budget(self, **_kwargs):
            return None

        def create_child_activity(self, _token, **kwargs):
            self.created = kwargs
            return SimpleNamespace(id=kwargs["activity_id"], state="pending")

        def transition_activity(self, _token, activity_id, **_kwargs):
            return SimpleNamespace(id=activity_id, state="active")

        def runtime_tuple_hash(self, _runtime):
            return "7" * 64

        def promote_qualified_activity(self, _token, activity_id, **_kwargs):
            return SimpleNamespace(id=activity_id, state="pending")

        def commit_runtime_receipt(self, _token, _activity_id, _runtime):
            return SimpleNamespace(receipt_sha256="8" * 64)

    class Supervisor:
        def __init__(self):
            self.requests = []
            self.consumed_credentials = []

        def launch_qualification(self, request, *, qualification_contract):
            self.requests.append((request, qualification_contract))
            return request

        def finish(self, request, *, timeout):
            material = request.claude_qualification_material
            root = evidence / material.probe_name
            root.mkdir()
            stdout, stderr = root / "stdout", root / "stderr"
            stdout.write_text("{}\n")
            stderr.write_text("")
            if material.credential_path is not None:
                credential_path = Path(material.credential_path)
                info = credential_path.stat()
                self.consumed_credentials.append(
                    (str(credential_path), info.st_dev, info.st_ino),
                )
                credential_path.unlink()
            return {"returncode": 0, "streams": {"stdout": {"locator": str(stdout)},
                    "stderr": {"locator": str(stderr)}}, "host_receipt": {"passed": True}}

    qualified = object()
    def publish(plan, _results):
        plan.output.write_text("{}\n")
        plan.output.chmod(0o600)
        return qualified

    monkeypatch.setattr(managed, "publish_claude_qualification_results", publish)
    token = SimpleNamespace(repository_id="repo", run_id="run", generation=1)
    prepared = SimpleNamespace(id="workspace", parent_activity_id="parent", child_request_key="request",
                               ready=True, path=workspace, input_digest="a" * 64, base_commit="b" * 40)
    supervisor = Supervisor()
    store = Store()
    _activity, _qualified, _receipt, _runtime, additions = managed.qualify_managed_claude_runtime(
        store, token, activity_id="11111111-1111-4111-8111-111111111111",
        activity_request_key="request", parent_activity_id="parent", workspace=prepared,
        host_request=ClaudeHostRequest(str(candidate), str(credential), str(binary), "claude-opus-5", None,
                                       "workspace-write", False, 0, 60),
        role="worker", evidence_root=evidence, final_contract_hash="c" * 64,
        supervisor=supervisor, bridge_command=json.dumps([str(bridge)], separators=(",", ":")),
    )
    admission = json.loads(Path(additions.admission_file).read_text())
    assert admission["activity_id"] == "11111111-1111-4111-8111-111111111111"
    assert admission["workspace"] == str(workspace)
    assert admission["runtime_identity"] == "7" * 64
    assert admission["runtime_identity"] == store.runtime_tuple_hash(qualified)
    assert len(supervisor.requests) == len(qualification.QUALIFICATION_PROBES)
    assert len(supervisor.consumed_credentials) == len(qualification.QUALIFICATION_PROBES) - 1
    assert len(set(supervisor.consumed_credentials)) == len(supervisor.consumed_credentials)
    for request, _contract in supervisor.requests:
        material = request.claude_qualification_material
        assert material.runtime_home == dict(material.environment)["CLAUDE_CONFIG_DIR"]
        assert material.runtime_home != str(evidence / "runtimes" / request.activity_id)
