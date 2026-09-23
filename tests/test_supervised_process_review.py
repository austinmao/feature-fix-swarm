"""Independent regressions for process transport; no native-host qualification."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json
import re
import sys

import pytest

from test_supervised_process import _allocate_registered_child, setup_owner
from test_managed_preparation import _args
from test_m4_upstream_context_acceptance import _repository
from test_run_context_acceptance import INHERITED_CONTEXT_KEYS
from run_state.cli import _cmd_fixture_start
from run_state.ownership import OwnershipRefused, assert_owner, release_owner
from run_state.supervisor import (
    DispatchRequest, Supervisor, SupervisorRefused, _head, run_managed_command,
)
from run_state.workspace import begin_child_workspace_preparation, prepare_workspace


@pytest.mark.parametrize("host", ["codex", "claude"])
@pytest.mark.parametrize("valid_usage", [True, False])
def test_direct_host_receipt_status_matches_review_consumer(tmp_path, host, valid_usage):
    """Scripted host stream tests settlement only, not native admission/auth."""
    from run_state.sealed_review import _review_output

    supervisor, store, request = setup_owner(tmp_path)
    output = {"schema": "fixture-review", "candidate_hash": "a" * 64}
    text = json.dumps(output)
    if host == "codex":
        usage = dict(input_tokens=3, cached_input_tokens=1, output_tokens=4,
                     cache_write_input_tokens=2, reasoning_output_tokens=1)
        records = [
            {"type": "thread.started", "thread_id": "fixture-thread"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
            {"type": "turn.completed", "usage": usage},
        ]
    else:
        usage = dict(input_tokens=3, cache_creation_input_tokens=2,
                     cache_read_input_tokens=1, output_tokens=4)
        records = [
            {"type": "system", "subtype": "init", "session_id": "fixture-session",
             "model": "fixture-model", "claude_code_version": "fixture-version"},
            {"type": "result", "subtype": "success", "is_error": False,
             "session_id": "fixture-session", "usage": usage, "result": text},
        ]
    if not valid_usage:
        del usage["output_tokens"]
    raw = "\n".join(json.dumps(record) for record in records) + "\n"
    request = replace(request, token_reservation=20,
                      command=(sys.executable, "-c", "print(" + repr(raw) + ", end='')"))
    handle = supervisor.launch(request)
    # Inject only finish-time material after the ordinary scripted fixture
    # launch. This does not exercise or bypass native qualification admission.
    material = SimpleNamespace(
        binary=(("path", sys.executable),), version="fixture-version", argv=request.command,
        environment=(), environment_sha256="e" * 64, cwd=request.workspace,
        model="fixture-model", effort="high", runtime_sha256="b" * 64,
        config_sha256="c" * 64, attempt=1, auth_path=str(tmp_path / "absent-auth"),
        auth_sha256="d" * 64, credential_path=str(tmp_path / "absent-credential"),
        credential_sha256="d" * 64, session_id="fixture-session",
    )
    setattr(handle, host + "_material", material)
    result = supervisor.finish(handle, timeout=10)
    assert result["host_receipt"]["status"] == ("complete" if valid_usage else "uncertain")
    with store.read_transaction() as tx:
        intent = tx.execute("SELECT state,token_usage FROM authority_launch_intents WHERE id=?",
                            (handle.intent_id,)).fetchone()
    if valid_usage:
        assert intent["state"] == "completed_succeeded"
        assert intent["token_usage"] == (10 if host == "codex" else 9)
        assert _review_output(result, handle.stdout_path.read_bytes()) == output
    else:
        assert intent["state"] == "uncertain" and intent["token_usage"] is None
        with pytest.raises(SupervisorRefused, match="FINAL_REVIEW_OUTPUT_INVALID"):
            _review_output(result, handle.stdout_path.read_bytes())


def _child_activity(supervisor, store):
    return _allocate_registered_child(store, supervisor.token, key="review-child")


def _must_refuse(supervisor, request, pattern):
    """Reap unexpected fixture launches before exposing the failed assertion."""
    try:
        handle = supervisor.launch(request)
    except (SupervisorRefused, OwnershipRefused) as error:
        assert re.search(pattern, str(error)), str(error)
    else:
        try:
            supervisor.finish(handle, timeout=10, token_usage=0)
        finally:
            if handle.process.poll() is None:
                handle.process.terminate()
                handle.process.wait(timeout=5)
        pytest.fail("request executed despite unproven admission")


@pytest.mark.parametrize("boundary", ["after_intent_commit", "after_spawn_before_ack"])
def test_identityless_uncertain_intent_blocks_a_different_sibling(tmp_path, boundary):
    def crash(point):
        if point == boundary:
            raise RuntimeError("lost handshake")

    supervisor, store, request = setup_owner(tmp_path, fault=crash)
    with pytest.raises(RuntimeError, match="lost handshake"):
        supervisor.launch(request)
    child, ready = _child_activity(supervisor, store)
    reopened = type(store)(store.db_path)
    next_supervisor = Supervisor(reopened, supervisor.token, evidence_root=supervisor.evidence_root)
    _must_refuse(
        next_supervisor, replace(request, activity_id=child.id, request_key="sibling",
                                 workspace=str(ready.path), expected_head=ready.base_commit),
        "RECONCILIATION|UNKNOWN|UNCERTAIN",
    )
    assert not (Path(request.workspace) / "ran").exists()


def test_child_workspace_binding_cannot_be_replaced_with_parent_workspace(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)
    _must_refuse(supervisor, replace(request, workspace=supervisor.token.workspace), "WORKSPACE")
    assert not (Path(request.workspace) / "ran").exists()


def test_revoked_fence_between_ack_and_authorization_cannot_execute(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)

    def revoke(point):
        if point == "after_ack_before_authorization":
            with store.transaction() as tx:
                release_owner(tx, supervisor.token)

    supervisor.fault_probe = revoke
    with pytest.raises(OwnershipRefused, match="FENCE_REVOKED"):
        supervisor.launch(request)
    assert not (Path(request.workspace) / "ran").exists()


def test_missing_meter_harvests_evidence_and_records_uncertainty(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)
    handle = supervisor.launch(request)
    result = supervisor.finish(handle, timeout=10, token_usage=None)
    assert Path(result["evidence"]["locator"]).is_file()
    with store.read_transaction() as tx:
        row = tx.execute("SELECT state FROM authority_launch_intents WHERE id=?", (handle.intent_id,)).fetchone()
    assert row["state"] == "uncertain"


def test_retry_after_receipt_publication_can_finish_without_overwriting_evidence(tmp_path, monkeypatch):
    supervisor, store, request = setup_owner(tmp_path)
    handle = supervisor.launch(request)
    complete = store.complete_launch

    def fail_once(*args, **kwargs):
        monkeypatch.setattr(store, "complete_launch", complete)
        raise RuntimeError("completion store interrupted")

    monkeypatch.setattr(store, "complete_launch", fail_once)
    with pytest.raises(RuntimeError, match="completion store interrupted"):
        supervisor.finish(handle, timeout=10, token_usage=0)
    receipt = handle.stdout_path.parent / "result.json"
    retained = receipt.read_bytes()
    result = supervisor.finish(handle, timeout=10, token_usage=0)
    assert result["returncode"] == 0
    assert receipt.read_bytes() == retained


def test_replaced_output_symlink_cannot_be_published_as_child_evidence(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)
    handle = supervisor.launch(request)
    handle.process.wait(timeout=10)
    unrelated = tmp_path / "stale-output.txt"
    unrelated.write_text("stale output from an unrelated process\n")
    handle.stdout_path.unlink()
    handle.stdout_path.symlink_to(unrelated)
    with pytest.raises((SupervisorRefused, OSError)):
        supervisor.finish(handle, timeout=10, token_usage=0)
    assert not (handle.stdout_path.parent / "result.json").exists()


def test_production_callback_requires_capability_before_executing_arbitrary_command(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)
    context = SimpleNamespace(
        upstream={"runtime_digest": request.runtime_identity}, activity_id=request.activity_id,
        evidence_root=str(supervisor.evidence_root), workspace=request.workspace,
        run_id=supervisor.token.run_id,
    )
    try:
        run_managed_command(store, supervisor.token, context, request.command, "production",
                            dispatch_limit=3, token_limit=100)
    except (SupervisorRefused, OwnershipRefused):
        pass
    assert not (Path(request.workspace) / "ran").exists(), "capability refusal must precede execution"


def test_callback_exception_does_not_release_owner_while_authorized_child_lives(tmp_path, monkeypatch):
    monkeypatch.chdir(_repository(tmp_path))
    for key in INHERITED_CONTEXT_KEYS:
        monkeypatch.delenv(key, raising=False)
    retained = {}

    def callback(store, token, context):
        store.configure_run_limits(token, dispatch_limit=3, token_limit=100)
        store.bind_runtime(token, context.activity_id, "b" * 64)
        supervisor = Supervisor(store, token, evidence_root=Path(context.evidence_root) / "review")
        pending = begin_child_workspace_preparation(
            store, token, parent_activity_id=context.activity_id, request_key="live-workspace",
            role="worker", base_commit=_head(Path(context.workspace)),
            repository_path=Path(context.workspace), selected_input_manifest={"entries": []},
        )
        ready = prepare_workspace(store, token, pending)
        child = store.create_child_activity(
            token, parent_activity_id=context.activity_id, role="worker", request_key="live-activity",
            candidate_hash=ready.input_digest, contract_hash="d" * 64, runtime_identity="b" * 64,
            workspace_binding=str(ready.path), workspace_preparation_id=ready.id,
        )
        handle = supervisor.launch(DispatchRequest(
            child.id, "live-child", (sys.executable, "-c", "import time; time.sleep(30)"),
            str(ready.path), ready.base_commit, "b" * 64, contract_hash="d" * 64,
        ))
        retained.update(store=store, token=token, handle=handle)
        raise RuntimeError("callback interrupted")

    try:
        with pytest.raises(RuntimeError, match="callback interrupted"):
            _cmd_fixture_start(_args(tmp_path / "authority"), on_ready=callback)
        if retained["handle"].process.poll() is None:
            with retained["store"].transaction() as tx:
                assert_owner(tx, retained["token"])
    finally:
        if retained:
            process = retained["handle"].process
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            with retained["store"].transaction() as tx:
                try:
                    release_owner(tx, retained["token"])
                except OwnershipRefused:
                    pass
