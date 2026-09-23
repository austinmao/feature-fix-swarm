"""Claude managed-wave children must have independent qualified launches."""
from __future__ import annotations

from contextlib import contextmanager
import json
from types import SimpleNamespace

import pytest

from run_state import managed_claude_qualification as managed
from run_state.claude_host import ClaudeHostRequest


def test_managed_claude_wave_child_gets_fresh_qualification_and_receipt(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ready = SimpleNamespace(
        id="outer-workspace", parent_activity_id="parent", child_request_key="managed-host:request",
        ready=True, path=workspace, input_digest="a" * 64, base_commit="b" * 40,
    )
    wave_workspace = tmp_path / "wave-workspace"
    wave_workspace.mkdir()
    wave_ready = SimpleNamespace(
        id="wave-workspace", parent_activity_id="parent", child_request_key="wave-request",
        ready=True, path=wave_workspace, input_digest="c" * 64, base_commit="b" * 40,
    )

    class Transaction:
        def execute(self, sql, *_args):
            # The session seam asks for retained replay rows before preparing anything;
            # a fresh run has none.  The root workspace row is the only retained row here.
            if any(marker in sql for marker in ("child_request_key", "a.request_key", "capacity_exempt",
                                                  "runtime_identity FROM authority_child_bindings",
                                                  "idempotency_key='frontend-operation'")):
                return SimpleNamespace(fetchone=lambda: None)
            return SimpleNamespace(fetchone=lambda: {"state": "ready", "kind": "execute"})

    class Store:
        def get_run_policy_budget(self, **_kwargs):
            return None

        def get_sealed_acceptance(self, **_kwargs):
            return None

        @contextmanager
        def read_transaction(self):
            yield Transaction()

        def runtime_tuple_hash(self, runtime):
            return "runtime:" + getattr(runtime, "marker", runtime)

        def get_activity(self, activity_id):
            return SimpleNamespace(id=activity_id, state="active")

        def transition_activity(self, *_args, **_kwargs):
            return None

    class Channel:
        def __init__(self, *_args):
            self.consumer = None

        def start(self):
            return None

        def attach_wave_consumer(self, consumer):
            self.consumer = consumer

        def close(self):
            return None

    class Supervisor:
        def _policy_timeout(self, timeout):
            return timeout, False

        def contain_revoked(self):
            return ()

        def __init__(self, *_args, **kwargs):
            self.worker_channel = kwargs["worker_channel"]

        def launch_managed_outer(self, request):
            self.request = request
            return SimpleNamespace(process=None, intent_id="intent")

        def finish(self, _handle, *, timeout):
            assert 0 < timeout <= 60
            assert captured["settled"]
            return {"returncode": 0, "evidence": {"fixture": True}, "host_receipt": {"passed": True}}

    captured = {}

    class WaveConsumer:
        def wait_for_idle(self, *, intent_id, timeout):
            assert intent_id == "intent"
            assert 0 < timeout <= 60
            captured["settled"] = True

        def __init__(self, _supervisor, prepare_child, *, finish_timeout):
            assert finish_timeout == 60
            captured["request"] = prepare_child(SimpleNamespace(
                activity_id="wave-activity", preparation=wave_ready, request_key="wave-request",
                parent_activity_id="parent", contract_hash="d" * 64,
                plan={"prompt": "wave prompt"},
            ))

    qualification_calls = []

    def qualify(_store, _token, *, activity_id, activity_request_key, parent_activity_id,
                workspace, host_request, role, evidence_root, final_contract_hash,
                supervisor, bridge_command):
        del host_request, evidence_root, supervisor, bridge_command
        qualification_calls.append({
            "activity_id": activity_id, "request_key": activity_request_key,
            "parent": parent_activity_id, "workspace": workspace, "role": role,
            "contract": final_contract_hash,
        })
        qualified = SimpleNamespace(observation=(("version", "2.1.274"),), marker=activity_id)
        return (
            SimpleNamespace(id=activity_id, state="active"), qualified,
            SimpleNamespace(receipt_sha256="receipt:" + activity_id), tmp_path / activity_id,
            SimpleNamespace(),
        )

    class Adapter:
        def __init__(self, _qualified, _binary, _version):
            pass

        def build_launch_material(self, prompt, *, attempt, session_id, gsd_environment):
            assert attempt == 1 and session_id and gsd_environment is not None
            return SimpleNamespace(argv=("claude", prompt))

        @staticmethod
        def release_launch_material(_material):
            return None

    monkeypatch.setattr(managed, "_from_row", lambda _row: SimpleNamespace(
        base_commit="b" * 40, repository_path=workspace,
    ))
    monkeypatch.setattr(managed, "load_input_snapshot", lambda *_args: SimpleNamespace(manifest={}))
    monkeypatch.setattr(managed, "_verify_snapshot_complete", lambda *_args: None)
    monkeypatch.setattr(managed, "begin_child_workspace_preparation", lambda *_args, **_kwargs: ready)
    monkeypatch.setattr(managed, "prepare_workspace", lambda *_args, **_kwargs: ready)
    monkeypatch.setattr(managed, "WorkerChannelServer", Channel)
    monkeypatch.setattr(managed, "Supervisor", Supervisor)
    monkeypatch.setattr(managed, "WaveConsumer", WaveConsumer)
    monkeypatch.setattr(managed, "qualify_managed_claude_runtime", qualify)
    monkeypatch.setattr(managed, "ClaudeHostAdapter", Adapter)
    monkeypatch.setattr(managed, "_gsd_wave_completion_code", lambda *_args, **_kwargs: None)

    request = ClaudeHostRequest(
        str(tmp_path / "candidate"), str(tmp_path / "credential"), str(tmp_path / "claude"),
        "claude-opus-5", None, "workspace-write", False, 23, 60,
    )
    token = SimpleNamespace(repository_id="repo", run_id="run", generation=1)
    context = SimpleNamespace(activity_id="parent", evidence_root=tmp_path / "evidence")
    assert managed.run_managed_claude_command(
        Store(), token, context, ("/gsd-execute-phase", "1"), "request", request,
    ) == 0

    wave_request = captured["request"]
    assert wave_request.activity_id == "wave-activity"
    assert wave_request.request_key == "wave-request"
    assert wave_request.workspace == str(wave_workspace)
    assert wave_request.contract_hash == "d" * 64
    assert wave_request.runtime_receipt_sha256 == "receipt:wave-activity"
    assert wave_request.managed_input_sha256 == "c" * 64
    assert wave_request.token_reservation == 23
    assert wave_request.claude_material.argv == ("claude", managed._managed_wave_prompt("wave prompt"))
    assert "Skip upstream GSD commit and metadata-commit steps" in wave_request.claude_material.argv[1]
    assert qualification_calls[0] == {
        "activity_id": "wave-activity", "request_key": "wave-request", "parent": "parent",
        "workspace": wave_ready, "role": "worker", "contract": "d" * 64,
    }


# A frontend operation with no sealed draft now refuses before staging or launch
# (ACCEPTANCE_DRAFT_REQUIRED); the direct execute-phase command still proves the wave rule.
@pytest.mark.parametrize(("command", "operation_payload", "expected_command", "expected_code"), [
    (("/gsd-execute-phase", "1"), None, "$gsd-execute-phase 1", "WAVE_EXECUTION_UNPROVEN"),
    (("feature-implement",), json.dumps({"data": {"invocation_text": "014 --autonomous"}}),
     "$feature-implement 014 --autonomous", "ACCEPTANCE_DRAFT_REQUIRED"),
])
def test_managed_claude_success_requires_wave_reply_when_gsd_waves_were_requested(
    tmp_path, monkeypatch, command, operation_payload, expected_command, expected_code,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ready = SimpleNamespace(
        id="outer-workspace", parent_activity_id="parent", child_request_key="managed-host:request",
        ready=True, path=workspace, input_digest="a" * 64, base_commit="b" * 40,
    )

    class Transaction:
        def execute(self, sql, *_args):
            if "idempotency_key='frontend-operation'" in sql:
                return SimpleNamespace(fetchone=lambda: None if operation_payload is None
                                       else {"payload": operation_payload})
            if any(marker in sql for marker in ("child_request_key", "a.request_key", "capacity_exempt",
                                                  "runtime_identity FROM authority_child_bindings")):
                return SimpleNamespace(fetchone=lambda: None)
            return SimpleNamespace(fetchone=lambda: {"state": "ready", "kind": "execute"})

    class Store:
        def get_run_policy_budget(self, **_kwargs):
            return None

        def get_sealed_acceptance(self, **_kwargs):
            return None

        def __init__(self):
            self.transitions = []

        @contextmanager
        def read_transaction(self):
            yield Transaction()

        def runtime_tuple_hash(self, runtime):
            return "runtime:" + getattr(runtime, "marker", runtime)

        def get_activity(self, activity_id):
            return SimpleNamespace(id=activity_id, state="active")

        def transition_activity(self, _token, activity_id, **kwargs):
            self.transitions.append((activity_id, kwargs))
            return None

    class Channel:
        def __init__(self, *_args):
            pass

        def start(self):
            return None

        def attach_wave_consumer(self, _consumer):
            return None

        def close(self):
            return None

    class Supervisor:
        def _policy_timeout(self, timeout):
            return timeout, False

        def contain_revoked(self):
            return ()

        def __init__(self, *_args, **_kwargs):
            pass

        def launch_managed_outer(self, _request):
            return SimpleNamespace(process=None, intent_id="intent")

        def finish(self, _handle, *, timeout):
            assert 0 < timeout <= 60
            assert settled
            return {"returncode": 0, "evidence": {"fixture": True}, "host_receipt": {"passed": True}}

    settled = []

    class WaveConsumer:
        def wait_for_idle(self, *, intent_id, timeout):
            assert intent_id == "intent"
            assert 0 < timeout <= 60
            settled.append(True)

        def __init__(self, _supervisor, _prepare_child, *, finish_timeout):
            assert finish_timeout == 60

    def qualify(_store, _token, *, activity_id, **_kwargs):
        qualified = SimpleNamespace(observation=(("version", "2.1.274"),), marker=activity_id)
        return (
            SimpleNamespace(id=activity_id, state="active"), qualified,
            SimpleNamespace(receipt_sha256="receipt:" + activity_id), tmp_path / activity_id,
            SimpleNamespace(),
        )

    prompts = []

    class Adapter:
        def __init__(self, _qualified, _binary, _version):
            pass

        def build_launch_material(self, prompt, *, attempt, session_id, gsd_environment):
            assert prompt and attempt == 1 and session_id and gsd_environment is not None
            prompts.append(prompt)
            return SimpleNamespace(argv=("claude", prompt))

        @staticmethod
        def release_launch_material(_material):
            return None

    monkeypatch.setattr(managed, "_from_row", lambda _row: SimpleNamespace(
        base_commit="b" * 40, repository_path=workspace,
    ))
    monkeypatch.setattr(managed, "load_input_snapshot", lambda *_args: SimpleNamespace(manifest={}))
    monkeypatch.setattr(managed, "_verify_snapshot_complete", lambda *_args: None)
    monkeypatch.setattr(managed, "begin_child_workspace_preparation", lambda *_args, **_kwargs: ready)
    monkeypatch.setattr(managed, "prepare_workspace", lambda *_args, **_kwargs: ready)
    monkeypatch.setattr(managed, "WorkerChannelServer", Channel)
    monkeypatch.setattr(managed, "Supervisor", Supervisor)
    monkeypatch.setattr(managed, "WaveConsumer", WaveConsumer)
    monkeypatch.setattr(managed, "qualify_managed_claude_runtime", qualify)
    monkeypatch.setattr(managed, "ClaudeHostAdapter", Adapter)
    monkeypatch.setattr(
        managed, "_gsd_wave_completion_code",
        lambda *_args, **_kwargs: "WAVE_EXECUTION_UNPROVEN",
    )

    request = ClaudeHostRequest(
        str(tmp_path / "candidate"), str(tmp_path / "credential"), str(tmp_path / "claude"),
        "claude-opus-5", None, "workspace-write", False, 23, 60,
    )
    token = SimpleNamespace(repository_id="repo", run_id="run", generation=1)
    context = SimpleNamespace(activity_id="parent", evidence_root=tmp_path / "evidence")
    store = Store()
    with pytest.raises(managed.SupervisorRefused, match=expected_code):
        managed.run_managed_claude_command(
            store, token, context, command, "request", request,
        )
    if expected_code == "ACCEPTANCE_DRAFT_REQUIRED":
        assert store.transitions == [] and prompts == []
        return
    assert store.transitions[-1][1]["new"] == "failed"
    assert store.transitions[-1][1]["reason"] == "GSD execution returned without supervised wave evidence"
    assert prompts[0].startswith(expected_command + "\n\n")
