"""Real registered worktrees and subprocesses for the production wave seam."""
from contextlib import contextmanager
from dataclasses import replace
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

from process_identity import LIVE, probe_identity

import pytest

from run_state.gsd_wave_bridge import persist_manifest
from run_state.supervisor import (
    DispatchRequest,
    SupervisorRefused,
    _gsd_wave_completion_code,
    _managed_command_requires_wave_proof,
)

from run_state.wave_consumer import WaveConsumer, _associate_recovery_members
from run_state.worker_channel import WorkerChannelServer, WorkerChannelRefused
from test_supervised_process import setup_owner, _receipt_bound_request
from test_wave_execution import wave_manifest, git


def test_managed_command_wave_proof_policy():
    assert _managed_command_requires_wave_proof(("/gsd-execute-phase", "1")) is True
    assert _managed_command_requires_wave_proof(("feature-implement", "014")) is True
    assert _managed_command_requires_wave_proof(("feature-spec", "014")) is False
    assert _managed_command_requires_wave_proof(("fix",)) is False
    assert _managed_command_requires_wave_proof(("plan",)) is None


def test_claimed_wave_recovers_original_monitored_children_without_relaunch(tmp_path, monkeypatch):
    with wave_fixture(tmp_path, monkeypatch, plans=1) as f:
        finish = f.supervisor.finish

        def interrupted(_handle, **_kwargs):
            raise KeyboardInterrupt("supervisor interrupted after release")

        monkeypatch.setattr(f.supervisor, "finish", interrupted)
        with pytest.raises(KeyboardInterrupt):
            f.consumer(f.event)
        children = [handle for handle in f.supervisor._handles.values() if handle is not f.outer]
        assert len(children) == 1 and children[0].monitored
        children[0].process.wait(timeout=15)
        with f.store.read_transaction() as tx:
            before = tuple(tx.execute("SELECT dispatch_used,token_committed FROM authority_run_limits").fetchone())
            bindings = [tuple(row) for row in tx.execute(
                "SELECT id,generation,acknowledgement_id,permit_id,child_pid FROM authority_launch_intents ORDER BY id")]
        monkeypatch.setattr(f.supervisor, "finish", finish)
        recovered = WaveConsumer(f.supervisor, lambda *_: pytest.fail("recovery prepared a new child"), finish_timeout=15)
        reply = recovered(f.event)
        assert reply["results"][0]["status"] == "complete"
        assert (f.parent / "result-0.txt").read_text() == "done"
        with f.store.read_transaction() as tx:
            assert tx.execute("SELECT dispatch_used FROM authority_run_limits").fetchone()[0] == before[0]
            assert [tuple(row) for row in tx.execute(
                "SELECT id,generation,acknowledgement_id,permit_id,child_pid FROM authority_launch_intents ORDER BY id")] == bindings


@contextmanager
def wave_fixture(tmp_path, monkeypatch, *, plans=2, commands=None, wave=2, record=True,
                 owner=None, outer_command=None, before_launch=None, policy_tier=None,
                 request_key="managed-host:wave-fixture:launch", launch_outer=None):
    supervisor, store, request = setup_owner(tmp_path) if owner is None else owner
    # Scripted transport proofs own a private registry and explicit observed
    # fixture resources. Never consult or mutate the user's global registry.
    if supervisor.shared_resource_coordinator is None:
        import time
        from run_state.managed_admission import ManagedAdmissionQueue
        from run_state.resource_observation import ResourceObservation
        from run_state.shared_resources import SharedResourceCoordinator, cold_start_demand
        queue = ManagedAdmissionQueue(tmp_path / 'wave-shared-resources', observation_provider=lambda:
            ResourceObservation(time.monotonic_ns(), 64, 64 << 30, 64 << 30, 1000, 1000, {}, 'scripted-fixture'))
        supervisor.shared_resource_coordinator = SharedResourceCoordinator(store, supervisor.token, queue=queue)
        supervisor.resource_demand_policy = cold_start_demand
    # The managed outer process has its own durable, validated exemption. Its
    # two GSD executor children must still fit the worker capacity exactly.
    if owner is None:
        with store.transaction() as tx:
            tx.execute("UPDATE authority_run_limits SET worker_capacity=2,dispatch_limit=12")
            tx.execute("UPDATE context_runs SET writer_version='ffs-supervisor/1'")
    if policy_tier is not None:
        import time
        from process_identity import ProcessIdentity
        store.configure_run_policy_budget(supervisor.token, tier=policy_tier,
                                          clock_boot_id=ProcessIdentity.current().boot_id,
                                          clock_monotonic_ns=time.monotonic_ns())
    with tempfile.TemporaryDirectory(prefix="ffs-wave-") as socket_dir:
        channel = WorkerChannelServer(store, supervisor.token, Path(socket_dir).resolve() / "ipc")
        supervisor.worker_channel = channel
        outer_request = _receipt_bound_request(
            store, supervisor.token,
            replace(request, request_key=request_key),
        )
        if before_launch is not None:
            before_launch(outer_request)
        outer = (launch_outer or supervisor.launch_managed_outer)(replace(
            outer_request, command=outer_command or (sys.executable, "-c", "import time; time.sleep(180)"),
        ))
        scope = channel._primary_bindings[outer.intent_id].scope()
        token = replace(supervisor.token, workspace=request.workspace)
        manifest = wave_manifest(token, SimpleNamespace(activity_id=request.activity_id), request.expected_head)
        manifest["wave"] = wave
        manifest["admission"]["runtime_identity"] = outer_request.runtime_identity
        manifest["plans"] = [dict(manifest["plans"][0], id=f"plan-{i}", files_modified=[f"result-{i}.txt"], files_deleted=[])
                             for i in range(plans)]
        locator, digest = persist_manifest(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(), Path(request.workspace))
        message = {"schema_version": 1, **scope, "request_key": "gsd-wave:wave-fixture", "operation": "gsd-wave-request",
                   "body": {"manifest_locator": locator, "manifest_sha256": digest}}
        event = channel._request(outer.identity, message)["event_id"] if record else None
        prepared = []

        def prepare(context):
            child = store.create_child_activity(
                supervisor.token, parent_activity_id=context.parent_activity_id, role="inventory",
                request_key=context.request_key, candidate_hash=context.candidate_hash,
                contract_hash=context.contract_hash, runtime_identity="b" * 64,
                workspace_binding=str(context.preparation.path), workspace_preparation_id=context.preparation.id,
                activity_id=context.activity_id, admission_guard=context.admission_guard,
            )
            index = len(prepared)
            command = commands[index] if commands else (
                sys.executable, "-c", f"from pathlib import Path; Path('result-{index}.txt').write_text('done')",
            )
            dispatch = DispatchRequest(child.id, context.request_key, command, str(context.preparation.path),
                                       request.expected_head, "b" * 64, contract_hash=context.contract_hash)
            with store.transaction() as tx:
                tx.execute("UPDATE authority_child_bindings SET role='worker' WHERE activity_id=?", (child.id,))
                tx.execute("UPDATE context_workspaces SET child_role='worker' WHERE preparation_id=?", (context.preparation.id,))
            prepared.append(context)
            return _receipt_bound_request(store, supervisor.token, dispatch)

        finish = supervisor.finish
        # Only fixture Python subprocesses have known zero model usage.
        monkeypatch.setattr(supervisor, "finish", lambda handle, **kw: finish(handle, token_usage=0, **kw))
        consumer = WaveConsumer(supervisor, prepare, finish_timeout=15)
        try:
            yield SimpleNamespace(supervisor=supervisor, store=store, channel=channel, outer=outer,
                                  request=outer_request,
                                  manifest=manifest, message=message, event=event, consumer=consumer,
                                  prepared=prepared, parent=Path(request.workspace), prepare=prepare)
        finally:
            for handle in list(supervisor._handles.values()):
                if handle.process is not None and handle.process.poll() is None:
                    if handle.monitored and probe_identity(handle.identity) == LIVE:
                        os.kill(handle.identity.pid, signal.SIGTERM)
                    else:
                        handle.process.terminate()
                    handle.process.wait(timeout=5)
            channel.close()


def test_real_bridge_process_uses_workspace_transport_for_wave_cohort(tmp_path, monkeypatch):
    with wave_fixture(tmp_path, monkeypatch, plans=1, record=False) as f:
        f.channel.attach_wave_consumer(f.consumer)
        file_channel = f.channel.register_file_transport(f.outer.intent_id)
        scope = f.channel._primary_bindings[f.outer.intent_id].scope()
        f.channel.start()
        environment = os.environ.copy()
        library = str(Path(__file__).resolve().parents[1] / "lib")
        environment["PYTHONPATH"] = library + os.pathsep + environment.get("PYTHONPATH", "")
        environment["FFS_WORKER_ENDPOINT"] = str(f.channel.endpoint)
        environment["FFS_WORKER_SCOPE"] = json.dumps(scope, sort_keys=True, separators=(",", ":"))
        environment["FFS_WORKER_FILE_CHANNEL"] = json.dumps(
            file_channel, sort_keys=True, separators=(",", ":"),
        )
        completed = subprocess.run(
            [sys.executable, "-m", "run_state.gsd_wave_bridge"],
            input=json.dumps(f.manifest, sort_keys=True, separators=(",", ":")).encode(),
            cwd=f.parent, env=environment, capture_output=True, timeout=30,
        )
        assert completed.returncode == 0, completed.stderr.decode()
        result = json.loads(completed.stdout)
        assert result["wave"] == f.manifest["wave"]
        assert result["results"][0]["status"] == "complete"
        assert result["results"][0]["changed_files"] == ["result-0.txt"]
        assert (f.parent / "result-0.txt").read_text() == "done"


def test_direct_execute_completion_requires_reply_for_current_intent(tmp_path, monkeypatch):
    with wave_fixture(tmp_path, monkeypatch, plans=1, record=False) as f:
        assert _gsd_wave_completion_code(
            f.store, f.outer.activity_id, f.outer.intent_id,
        ) == "WAVE_EXECUTION_UNPROVEN"
        assert _gsd_wave_completion_code(
            f.store, f.outer.activity_id, f.outer.intent_id, require_wave=False,
        ) is None
        event = f.store.record_event_once(
            f.supervisor.token, f.outer.activity_id,
            f"worker-request:{f.outer.intent_id}:gsd-wave:fixture", {},
        )
        assert _gsd_wave_completion_code(
            f.store, f.outer.activity_id, f.outer.intent_id,
        ) == "WAVE_EXECUTION_UNPROVEN"
        f.store.record_event_once(
            f.supervisor.token, f.outer.activity_id, f"gsd-wave:{event['id']}:reply", {},
        )
        assert _gsd_wave_completion_code(f.store, f.outer.activity_id, f.outer.intent_id) == "WAVE_EXECUTION_UNPROVEN"
        f.store.record_event_once(
            f.supervisor.token, f.outer.activity_id, f"gsd-wave:{event['id']}:refused", {},
        )
        assert _gsd_wave_completion_code(
            f.store, f.outer.activity_id, f.outer.intent_id,
        ) == "WAVE_EXECUTION_UNPROVEN"


def test_capacity_two_admits_overlapping_wave_cohort_and_replay_returns_only_retained_reply(tmp_path, monkeypatch):
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    commands = []
    for i in range(2):
        commands.append((sys.executable, "-c", f"""
import time
from pathlib import Path
root=Path({str(barrier)!r})
(root / '{i}').touch()
end=time.monotonic()+10
while not all((root / str(j)).exists() for j in range(2)):
    if time.monotonic()>end: raise RuntimeError('cohort did not overlap')
    time.sleep(.01)
Path('result-{i}.txt').write_text('overlapped')
"""))
    with wave_fixture(tmp_path, monkeypatch, commands=commands) as f:
        f.channel.attach_wave_consumer(f.consumer)
        result = f.channel._request(f.outer.identity, f.message)["result"]
        assert [item["plan_id"] for item in result["results"]] == ["plan-0", "plan-1"]
        assert all(item["status"] == "complete" and "overlapped" in item["patch"] for item in result["results"])
        assert len({item.preparation.path for item in f.prepared}) == 2
        assert (f.parent / "result-0.txt").read_text() == "overlapped"
        assert (f.parent / "result-1.txt").read_text() == "overlapped"
        assert git(f.parent, "rev-parse", "HEAD") == f.manifest["initial_head"]
        with f.store.read_transaction() as tx:
            intents = tx.execute(
                "SELECT capacity_exempt FROM authority_launch_intents ORDER BY created_at,id",
            ).fetchall()
            count = len(intents)
        assert [row["capacity_exempt"] for row in intents] == [1, 0, 0]
        f.consumer.prepare_child = lambda *_: pytest.fail("replay prepared a child")
        assert WaveConsumer(f.supervisor, f.consumer.prepare_child)(f.event) == result
        with f.store.read_transaction() as tx:
            assert tx.execute("SELECT COUNT(*) FROM authority_launch_intents").fetchone()[0] == count


def test_honest_no_change_and_failed_process_results(tmp_path, monkeypatch):
    commands = [(sys.executable, "-c", "pass"), (sys.executable, "-c", "raise SystemExit(2)")]
    with wave_fixture(tmp_path, monkeypatch, commands=commands) as f:
        result = f.consumer(f.event)
        assert [item["status"] for item in result["results"]] == ["complete", "failed"]
        assert all(item["patch"] == "" and item["changed_files"] == [] for item in result["results"])
        assert "without changes" in result["results"][0]["summary"]


def test_scope_failure_returns_no_integration_material(tmp_path, monkeypatch):
    with wave_fixture(tmp_path, monkeypatch, plans=1, commands=[
        (sys.executable, "-c", "from pathlib import Path; Path('outside').write_text('bad')"),
    ]) as f:
        result = f.consumer(f.event)["results"][0]
        assert result["status"] == "failed" and result["patch"] == ""
        assert "WAVE_SCOPE_VIOLATION" in result["summary"]


def test_preparation_failure_never_launches_a_partial_cohort(tmp_path, monkeypatch):
    with wave_fixture(tmp_path, monkeypatch) as f:
        def prepare(context):
            if f.prepared:
                raise SupervisorRefused("RUNTIME_QUALIFICATION_FAILED")
            return f.prepare(context)
        consumer = WaveConsumer(f.supervisor, prepare)
        with pytest.raises(SupervisorRefused, match="RUNTIME_QUALIFICATION_FAILED"):
            consumer(f.event)
        with f.store.read_transaction() as tx:
            assert tx.execute("SELECT COUNT(*) FROM authority_launch_intents").fetchone()[0] == 1
        with pytest.raises(SupervisorRefused, match="RUNTIME_QUALIFICATION_FAILED"):
            consumer(f.event)


def test_claimed_prelaunch_refusal_replays_its_original_typed_code(tmp_path, monkeypatch):
    with wave_fixture(tmp_path, monkeypatch, plans=1) as f:
        def refuse(_context):
            raise SupervisorRefused("POLICY_STAGE_INFEASIBLE")

        consumer = WaveConsumer(f.supervisor, refuse, finish_timeout=15)
        with pytest.raises(SupervisorRefused, match="POLICY_STAGE_INFEASIBLE"):
            consumer(f.event)
        with f.store.read_transaction() as tx:
            claims = tx.execute(
                "SELECT COUNT(*) FROM authority_event_keys WHERE idempotency_key=?",
                (f"gsd-wave:{f.event}:claimed",),
            ).fetchone()[0]
            refusals = tx.execute(
                "SELECT COUNT(*) FROM authority_event_keys WHERE idempotency_key=?",
                (f"gsd-wave:{f.event}:refused",),
            ).fetchone()[0]
            launches = tx.execute("SELECT COUNT(*) FROM authority_launch_intents").fetchone()[0]
        assert (claims, refusals, launches) == (1, 1, 1)
        replay = WaveConsumer(f.supervisor, lambda *_: pytest.fail("replay relaunched"), finish_timeout=15)
        with pytest.raises(SupervisorRefused, match="POLICY_STAGE_INFEASIBLE"):
            replay(f.event)
        with f.store.read_transaction() as tx:
            assert tx.execute("SELECT COUNT(*) FROM authority_launch_intents").fetchone()[0] == launches


def test_recovery_associates_double_digit_cohort_members_by_key_and_activity():
    prefix = "gsd-wave:71"
    plans = [{"id": f"plan-{index}"} for index in range(12)]
    prepared = [{"plan_id": plan["id"], "activity_id": f"activity-{index}"}
                for index, plan in enumerate(plans)]
    # This is the durable lexical order produced by request-key normalization:
    # plan:10 and plan:11 precede plan:2, so positional zipping is unsound.
    members = sorted(
        [{"request_key": f"{prefix}:plan:{index}", "activity_id": f"activity-{index}"}
         for index in range(12)],
        key=lambda member: member["request_key"],
    )
    associated = _associate_recovery_members(prefix, plans, prepared, members)
    assert [member["activity_id"] for _plan, _retained, member, _key in associated] == [
        f"activity-{index}" for index in range(12)
    ]
    members[0] = {**members[0], "activity_id": "wrong-activity"}
    with pytest.raises(SupervisorRefused, match="WAVE_EVIDENCE_CHANGED"):
        _associate_recovery_members(prefix, plans, prepared, members)


def test_terminal_parent_between_harvest_and_apply_publishes_no_reply_or_patch(tmp_path, monkeypatch):
    import run_state.wave_consumer as wave_module

    with wave_fixture(tmp_path, monkeypatch, plans=1) as f:
        harvest = wave_module.harvest_scoped_patch

        def terminal_after_harvest(*args, **kwargs):
            staged = harvest(*args, **kwargs)
            f.store.transition_activity(
                f.supervisor.token, f.outer.activity_id, expected="active", new="failed",
                reason="terminal boundary injected after harvest",
            )
            return staged

        monkeypatch.setattr(wave_module, "harvest_scoped_patch", terminal_after_harvest)
        with pytest.raises(SupervisorRefused, match="IPC_SCOPE_MISMATCH"):
            f.consumer(f.event)
        with f.store.read_transaction() as tx:
            keys = {row[0] for row in tx.execute(
                "SELECT idempotency_key FROM authority_event_keys WHERE activity_id=?",
                (f.outer.activity_id,),
            ).fetchall()}
        assert f"gsd-wave:{f.event}:reply" not in keys
        assert f"gsd-wave:{f.event}:integrated" not in keys
        assert not (f.parent / "result-0.txt").exists()


def test_uncertain_cohort_never_relaunches_or_refunds(tmp_path, monkeypatch):
    with wave_fixture(tmp_path, monkeypatch) as f:
        original = f.supervisor.launch_cohort
        def uncertain(*args, **kwargs):
            original(*args, **kwargs)
            raise SupervisorRefused("COHORT_BINDING_UNKNOWN")
        monkeypatch.setattr(f.supervisor, "launch_cohort", uncertain)
        with pytest.raises(SupervisorRefused, match="COHORT_BINDING_UNKNOWN"):
            f.consumer(f.event)
        with f.store.read_transaction() as tx:
            before = [tuple(row) for row in tx.execute("SELECT id,generation,acknowledgement_id,permit_id,child_pid FROM authority_launch_intents ORDER BY id")]
            charged = tx.execute("SELECT dispatch_used FROM authority_run_limits").fetchone()[0]
        for handle in f.supervisor._handles.values():
            if handle is not f.outer:
                handle.process.wait(timeout=15)
        # The real cohort was released before its caller lost the response.
        # Recovery settles those monitors, never calls launch_cohort again.
        reply = f.consumer(f.event)
        assert all(result["status"] == "complete" for result in reply["results"])
        assert _gsd_wave_completion_code(f.store, f.outer.activity_id, f.outer.intent_id) is None
        with f.store.read_transaction() as tx:
            assert [tuple(row) for row in tx.execute("SELECT id,generation,acknowledgement_id,permit_id,child_pid FROM authority_launch_intents ORDER BY id")] == before
            assert tx.execute("SELECT dispatch_used FROM authority_run_limits").fetchone()[0] == charged


def test_replay_revalidates_peer_and_durable_manifest(tmp_path, monkeypatch):
    with wave_fixture(tmp_path, monkeypatch, plans=1) as f:
        f.consumer(f.event)
        locator = f.message["body"]["manifest_locator"]
        (f.parent / locator).write_text("{}")
        with pytest.raises(WorkerChannelRefused, match="IPC_WAVE_MANIFEST_HASH_MISMATCH"):
            f.consumer(f.event)


def test_completed_wave_replay_survives_originating_peer_exit(tmp_path, monkeypatch):
    with wave_fixture(tmp_path, monkeypatch, plans=1) as f:
        expected = f.consumer(f.event)
        assert probe_identity(f.outer.identity) == LIVE
        os.kill(f.outer.identity.pid, signal.SIGTERM)
        f.outer.process.wait(timeout=5)
        assert f.consumer(f.event) == expected


def test_inherited_overlay_is_available_but_not_returned_as_new_patch(tmp_path, monkeypatch):
    with wave_fixture(tmp_path, monkeypatch, plans=1) as f:
        (f.parent / "inherited.txt").write_text("prior wave\n")
        (f.parent / "src/input.txt").write_text("prior tracked change\n")
        result = f.consumer(f.event)["results"][0]
        assert result["status"] == "complete" and result["changed_files"] == ["result-0.txt"]
        assert "inherited.txt" not in result["patch"] and "src/input.txt" not in result["patch"]
        child = f.prepared[0].preparation.path
        assert (child / "inherited.txt").read_text() == "prior wave\n"
        assert (child / "src/input.txt").read_text() == "prior tracked change\n"


def test_uncertain_usage_finishes_every_member_and_never_returns_success(tmp_path, monkeypatch):
    with wave_fixture(tmp_path, monkeypatch) as f:
        original_finish = type(f.supervisor).finish
        finished = []
        def finish(handle, **kwargs):
            result = original_finish(f.supervisor, handle, **kwargs)
            finished.append(handle.intent_id)
            return result
        monkeypatch.setattr(f.supervisor, "finish", finish)
        with pytest.raises(SupervisorRefused, match="WAVE_RESULT_UNCERTAIN"):
            f.consumer(f.event)
        assert len(finished) == 2
        with f.store.read_transaction() as tx:
            before = [tuple(row) for row in tx.execute("SELECT * FROM authority_launch_accounting ORDER BY intent_id")]
        with pytest.raises(SupervisorRefused, match="WAVE_RESULT_UNCERTAIN"):
            f.consumer(f.event)
        with f.store.read_transaction() as tx:
            assert [tuple(row) for row in tx.execute("SELECT * FROM authority_launch_accounting ORDER BY intent_id")] == before


def test_capacity_one_refuses_the_wave_without_serializing_it(tmp_path, monkeypatch):
    with wave_fixture(tmp_path, monkeypatch) as f:
        with f.store.transaction() as tx:
            tx.execute("UPDATE authority_run_limits SET worker_capacity=1")
        with pytest.raises(SupervisorRefused, match="WAVE_COHORT_CAPABILITY_UNAVAILABLE"):
            f.consumer(f.event)
        with f.store.read_transaction() as tx:
            assert tx.execute("SELECT COUNT(*) FROM authority_launch_intents").fetchone()[0] == 1


def record_wave(f, manifest, request_key):
    locator, digest = persist_manifest(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(), f.parent)
    message = {**f.message, "request_key": request_key,
               "body": {"manifest_locator": locator, "manifest_sha256": digest}}
    return f.channel._request(f.outer.identity, message)["event_id"]


def test_second_wave_accepts_only_proven_completed_prior_wave_dependency(tmp_path, monkeypatch):
    with wave_fixture(tmp_path, monkeypatch, plans=1, wave=1) as f:
        first = f.consumer(f.event)
        assert (f.parent / "result-0.txt").read_text() == "done"
        second = json.loads(json.dumps(f.manifest))
        second["wave"] = 2
        second["plans"][0].update(id="dependent", depends_on=["plan-0"], files_modified=["result-1.txt"], prompt_nonce="second")
        result = f.consumer(record_wave(f, second, "second-wave"))
        assert result["wave"] == 2 and result["initial_head"] == first["initial_head"]
        assert result["results"][0]["status"] == "complete"
        assert result["results"][0]["changed_files"] == ["result-1.txt"]
        assert (f.prepared[1].preparation.path / "result-0.txt").read_text() == "done"


@pytest.mark.parametrize("dependency,expected", [
    ("missing-prior-plan", "WAVE_DEPENDENCY_UNSATISFIED"),
    ("plan-1", "WAVE_COHORT_CAPABILITY_UNAVAILABLE"),
])
def test_unproven_or_internal_dependencies_refused_before_effects(tmp_path, monkeypatch, dependency, expected):
    with wave_fixture(tmp_path, monkeypatch) as f:
        manifest = json.loads(json.dumps(f.manifest))
        manifest["plans"][0]["depends_on"] = [dependency]
        with pytest.raises(SupervisorRefused, match=expected):
            f.consumer(record_wave(f, manifest, "dependent-wave"))
        assert not f.prepared
