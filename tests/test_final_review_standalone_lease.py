"""F50: the final reviewer's qualification probes take standalone leases.

The outer orchestrator's prepaid resource group is ended (``closed``) when the
outer launch finishes.  Native review and its qualification run after that, so
they must cross the channel-less review supervisor (plain standalone leases),
and a child that reaches an ended group must refuse typed before any intent.

Real ControlStore, Supervisor, ManagedAdmissionQueue and coordinators throughout.
Stubbed: the Codex binary and private runtime staging, and the qualification
observer (the fixture ``qualify_managed_runtime`` seam of the lifecycle
assembly); a scripted probe stands in for the observer's real probes.
"""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

import run_state.managed_qualification as managed_qualification
from run_state.managed_admission import ManagedAdmissionQueue, ManagedAdmissionRefused
from run_state.managed_resource_group import ManagedParentResourceCoordinator
from run_state.resource_groups import ResourceGroupRefused, ResourceParentGroupRegistry
from run_state.resource_observation import ResourceObservation
from run_state.shared_resources import SharedResourceCoordinator, cold_start_demand
from run_state.state import ControlStoreRefused
from run_state.supervisor import DispatchRequest, QualificationLaunchMaterial, SupervisorRefused
from run_state.upstream import UpstreamRuntime
from run_state.workspace import (
    begin_child_workspace_preparation, inspect_workspace, load_input_snapshot, prepare_workspace,
)
from test_managed_lifecycle_assembly import (
    _draft, _facts, _fixture_host, _frontend_start, _last_envelope, _setup, requires_local_confinement,
)
from test_qualification_launch_authority import PROBES
from test_supervised_process import setup_owner


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _scripted_probe(store, token, *, parent_activity_id, base_commit, repository_path, root: Path, key: str,
                    role: str = "inventory"):
    """One scripted Codex probe as a child of ``parent_activity_id`` (python prints a fixed stream)."""
    pending = begin_child_workspace_preparation(
        store, token, parent_activity_id=parent_activity_id, request_key=key + ":workspace", role=role,
        base_commit=base_commit, selected_input_manifest={"entries": []}, repository_path=repository_path)
    ready = prepare_workspace(store, token, pending)
    runtime = root / (key + "-runtime")
    runtime.mkdir(mode=0o700)
    scratch = ready.path / ".ffs-observer-tmp"
    scratch.mkdir()
    admission = runtime / "admission.json"
    admission.write_text("{}")
    admission.chmod(0o600)
    usage = {name: 0 for name in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                                  "output_tokens", "reasoning_output_tokens")}
    stream = "\n".join(json.dumps(row) for row in (
        {"type": "thread.started", "thread_id": "scripted-probe"},
        {"type": "turn.started"}, {"type": "turn.completed", "usage": usage}))
    bridge = Path(__file__).resolve().parents[1] / "lib/run_state/gsd_wave_bridge.py"
    environment = {
        "HOME": str(runtime), "CODEX_HOME": str(runtime), "TMPDIR": str(scratch),
        "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "NO_COLOR": "1",
        "GSD_DISPATCH_MODE": "ffs-supervised-process", "FFS_SUPERVISED_COMMIT_MODE": "patches",
        "FFS_SUPERVISED_ADMISSION_FILE": str(admission),
        "FFS_SUPERVISED_DISPATCH_COMMAND_JSON": json.dumps([sys.executable, str(bridge)], separators=(",", ":")),
        "FFS_HOOK_OBSERVATION": str(runtime / "hooks.jsonl"), "FFS_HOOK_NONCE": "probe-nonce",
    }
    command = (sys.executable, "-c", "print(" + repr(stream) + ")")
    pairs = tuple(sorted(environment.items()))
    probes = {name: {"probe_name": name, "command_sha256": _digest(command),
                     "environment_sha256": _digest(pairs), "qualification_request_id": "probe:" + name}
              for name in PROBES}
    envelope = {
        "schema": "ffs.qualification-envelope/v1", "qualification_cohort_id": "probe",
        "probes": [{"probe_name": name, "probe_contract_sha256": _digest(probes[name])} for name in PROBES],
        "runtime_template_sha256": "3" * 64, "workspace_binding": str(ready.path),
        "candidate_input_sha256": ready.input_digest, "model": "fixture-model", "effort": "high",
        "sandbox": "workspace-write", "roots": [str(ready.path)], "policy_sha256": "9" * 64,
    }
    envelope_hash = _digest(envelope)
    contract = {"schema": "ffs.qualification-launch/v1", "probe_contract": probes["ordinary"],
                "qualification_envelope": envelope, "qualification_envelope_sha256": envelope_hash}
    native_hash = _digest({"schema": "ffs.codex-qualification-probe/v1", "probe_name": "ordinary",
                           "argv_sha256": _digest(command), "environment_sha256": _digest(pairs),
                           "cwd": str(ready.path), "runtime_home": str(runtime),
                           "runtime_template_sha256": "3" * 64})
    material = QualificationLaunchMaterial(
        probe_name="ordinary", argv=command, environment=pairs, cwd=str(ready.path),
        contract_sha256=native_hash, envelope_sha256=envelope_hash, runtime_home=str(runtime),
        runtime_template_sha256="3" * 64)
    child = store.create_child_activity(
        token, parent_activity_id=parent_activity_id, role=role, request_key=key + ":activity",
        candidate_hash=ready.input_digest, contract_hash=envelope_hash, runtime_identity=envelope_hash,
        workspace_binding=str(ready.path), workspace_preparation_id=ready.id, retry_budget=4)
    store.transition_activity(token, child.id, expected="pending", new="active")
    request = DispatchRequest(child.id, "probe:ordinary", command, str(ready.path), ready.base_commit,
                              envelope_hash, token_reservation=7, contract_hash=envelope_hash,
                              managed_input_sha256=ready.input_digest, qualification_material=material)
    return request, contract


def _lease_groups(store, activity_id):
    """``group_id`` of every standalone ``resource-lease:`` event recorded for this activity."""
    with store.read_transaction() as tx:
        rows = tx.execute("SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
                          "WHERE k.activity_id=? AND k.idempotency_key LIKE 'resource-lease:%'",
                          (activity_id,)).fetchall()
    return [json.loads(row[0])["data"]["group_id"] for row in rows]


@requires_local_confinement
def test_reviewer_qualification_takes_a_standalone_lease_after_the_parent_group_closed(tmp_path, monkeypatch):
    primary, authority, repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    for key in ("GSD_RUN_ID", "FFS_RUN_ID", "GSD_RESUME"):
        monkeypatch.delenv(key, raising=False)
    runtime, fake, catalog = _fixture_host(tmp_path, monkeypatch)
    import run_state.supervisor as supervisor_module
    monkeypatch.setattr(supervisor_module, "_gsd_wave_completion_code", lambda *_args, require_wave=True: None)
    fixture_qualify = managed_qualification.qualify_managed_runtime
    calls = []

    def qualify_and_probe(store, token, **kwargs):
        """The fixture seam, then one scripted probe launched through the supervisor the closure handed over."""
        bundle = fixture_qualify(store, token, **kwargs)
        supervisor = kwargs["supervisor"]
        call = {"role": kwargs["role"], "outer": supervisor.worker_channel is not None}
        calls.append(call)
        if kwargs["role"] == "reviewer":
            workspace = kwargs["workspace"]
            request, contract = _scripted_probe(
                store, token, parent_activity_id=kwargs["parent_activity_id"], base_commit=workspace.base_commit,
                repository_path=workspace.repository_path, root=tmp_path, key="reviewer-probe")
            handle = supervisor.launch_qualification(request, qualification_contract=contract)
            result = supervisor.finish(handle, timeout=15)
            assert result["returncode"] == 0
            store.transition_activity(token, request.activity_id, expected="active", new="succeeded",
                                      result=result["evidence"])
            call["probe_activity"] = request.activity_id
        return bundle

    monkeypatch.setattr(managed_qualification, "qualify_managed_runtime", qualify_and_probe)
    result = _frontend_start(env, authority, "sl", runtime, fake, catalog, _draft(tmp_path), "task-swarm",
                             "--scope", "1")
    assert result == 0
    facts = _facts(authority, repository_id, "sl")
    assert facts.stage == "DONE" and facts.native == 1 and facts.reviews == 1
    reviewer = [call for call in calls if call["role"] == "reviewer"]
    # The reviewer crossed the channel-less supervisor; everything else stayed on the outer one.
    assert len(reviewer) == 1 and reviewer[0]["outer"] is False
    assert [call["outer"] for call in calls if call["role"] != "reviewer"] == [True]
    assert _lease_groups(facts.store, reviewer[0]["probe_activity"]) == [None]


def _managed_group(tmp_path, *, demand_policy=cold_start_demand):
    """A real owner with a reserved prepaid parent group (2 frozen plans, 1 child slot)."""
    supervisor, store, parent = setup_owner(tmp_path)
    token = supervisor.token
    store.transition_activity(token, parent.activity_id, expected="pending", new="active", reason="fixture parent")
    with store.read_transaction() as tx:
        preparation_id = tx.execute("SELECT workspace_preparation_id FROM authority_child_bindings WHERE activity_id=?",
                                    (parent.activity_id,)).fetchone()[0]
    ready = inspect_workspace(store, preparation_id)
    phases = ready.path / ".planning" / "phases" / "01-fixture"
    phases.mkdir(parents=True)
    for index in (1, 2):
        (phases / f"01-0{index}-PLAN.md").write_text(f"---\nphase: 01\nplan: 0{index}\n---\nPlan\n")
    upstream = UpstreamRuntime.from_manifest(
        json.loads(Path(os.environ["FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR"]).read_bytes()))
    queue = ManagedAdmissionQueue(tmp_path / "shared", observation_provider=lambda:
        ResourceObservation(time.monotonic_ns(), 2, 4 << 30, 4 << 30, 100, 100, {}, "fixture"))
    supervisor.shared_resource_coordinator = SharedResourceCoordinator(store, token, queue=queue)
    supervisor.resource_demand_policy = demand_policy
    with store.transaction() as tx:
        tx.execute("UPDATE context_runs SET planning_scope=? WHERE repository_id=? AND run_id=?",
                   ("1", token.repository_id, token.run_id))
        root = tx.execute("SELECT activity_id,workspace FROM context_runs WHERE repository_id=? AND run_id=?",
                          (token.repository_id, token.run_id)).fetchone()
    context = SimpleNamespace(activity_id=root["activity_id"], workspace=root["workspace"],
        upstream={"runtime_digest": upstream.runtime_digest,
                  "planning_root": str(Path(root["workspace"]) / ".planning")})
    supervisor.configure_managed_parent_resources(parent, context, ready, upstream)
    return supervisor, store, parent, ready, queue


def _intent_count(store, activity_id=None):
    sql, args = "SELECT COUNT(*) FROM authority_launch_intents", ()
    if activity_id is not None:
        sql, args = sql + " WHERE activity_id=?", (activity_id,)
    with store.read_transaction() as tx:
        return tx.execute(sql, args).fetchone()[0]


def test_child_acquire_after_the_parent_group_ended_refuses_before_any_intent(tmp_path):
    supervisor, store, parent, ready, _queue = _managed_group(tmp_path)
    token, coordinator = supervisor.token, supervisor.shared_resource_coordinator
    handle = supervisor.launch(replace(parent, command=(sys.executable, "-c", "print('parent')")))
    assert supervisor.finish(handle, timeout=10, token_usage=0)["returncode"] == 0
    assert coordinator._group_state() == "closed"
    snapshot = load_input_snapshot(store, ready)
    pending = begin_child_workspace_preparation(
        store, token, parent_activity_id=parent.activity_id, request_key="late-child", role="worker",
        base_commit=ready.base_commit, selected_input_manifest=snapshot.manifest,
        repository_path=ready.repository_path)
    child_ready = prepare_workspace(store, token, pending, input_snapshot=snapshot)
    child = store.create_child_activity(
        token, parent_activity_id=parent.activity_id, role="worker", request_key="late-child-activity",
        candidate_hash=child_ready.input_digest, contract_hash=parent.contract_hash,
        runtime_identity=parent.runtime_identity, workspace_binding=str(child_ready.path),
        workspace_preparation_id=child_ready.id, retry_budget=1)
    request = replace(parent, activity_id=child.id, request_key="late-child-launch",
                      workspace=str(child_ready.path), command=(sys.executable, "-c", "print('late')"))
    before = _intent_count(store)
    with pytest.raises(ControlStoreRefused, match="RESOURCE_PARENT_GROUP_ENDED"):
        coordinator.acquire(({"activity_id": child.id, "request_key": request.request_key,
                              "material": supervisor._dispatch_material(request),
                              "demand": supervisor.resource_demand_policy(request)},))
    assert _intent_count(store) == before and _intent_count(store, child.id) == 0


def test_bind_shared_resource_types_a_resource_group_refusal(tmp_path):
    supervisor, _store, _request = setup_owner(tmp_path)

    def refuse(_reservation, _intent_id, *, consumer=None):
        raise ResourceGroupRefused("RESOURCE_GROUP_NOT_AVAILABLE")

    supervisor.shared_resource_coordinator = SimpleNamespace(bind_intent=refuse)
    with pytest.raises(SupervisorRefused) as refused:
        supervisor._bind_shared_resource("intent", ("ticket", {}))
    assert refused.value.code == "RESOURCE_GROUP_NOT_AVAILABLE"
    assert "intent" not in supervisor._shared_reservations


def test_wave_worker_qualification_still_binds_to_the_parent_group(tmp_path):
    # Providerless demands keep the fixture parent (a plain python child) and the probe compatible.
    def demand(request):
        return replace(cold_start_demand(request), provider=None, provider_units=0, memory_bytes=64 << 20)

    supervisor, store, parent, ready, queue = _managed_group(tmp_path, demand_policy=demand)
    token, coordinator = supervisor.token, supervisor.shared_resource_coordinator
    handle = supervisor.launch(replace(parent, command=(sys.executable, "-c",
        "import time; from pathlib import Path; end=time.monotonic()+30\n"
        "while not Path('release').exists():\n if time.monotonic()>end: raise RuntimeError('deadline')\n time.sleep(.01)")))
    request, contract = _scripted_probe(
        store, token, parent_activity_id=parent.activity_id, base_commit=ready.base_commit,
        repository_path=ready.repository_path, root=tmp_path, key="wave-probe")
    with store.transaction() as tx:  # qualification probes exist only under the managed writer
        tx.execute("UPDATE context_runs SET writer_version='ffs-supervisor/1'")
    probe = supervisor.launch_qualification(request, qualification_contract=contract)
    assert supervisor.finish(probe, timeout=15)["returncode"] == 0
    with queue._connection() as connection:
        claims = connection.execute("SELECT state,request_key FROM resource_parent_group_claims WHERE group_id=?",
                                    (coordinator.plan.group_id,)).fetchall()
    assert [(row["state"], row["request_key"].endswith("probe:ordinary")) for row in claims] == [("finished", True)]
    assert _lease_groups(store, request.activity_id) == []
    (ready.path / "release").touch()
    assert supervisor.finish(handle, timeout=10, token_usage=0)["returncode"] == 0


def test_claude_qualify_runtime_routes_a_given_supervisor(tmp_path, monkeypatch):
    """Claude parity: stubs mirror test_managed_claude_wave; only the supervisor routing is under test."""
    from contextlib import contextmanager
    from run_state import managed_claude_qualification as managed
    from run_state.claude_host import ClaudeHostRequest

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ready = SimpleNamespace(id="outer-workspace", parent_activity_id="parent", child_request_key="managed-host:request",
                            ready=True, path=workspace, input_digest="a" * 64, base_commit="b" * 40)

    class Transaction:
        def execute(self, sql, *_args):
            if any(marker in sql for marker in ("child_request_key", "a.request_key", "capacity_exempt", "authority_launch_intents",
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

    class Channel:
        def __init__(self, *_args):
            pass

        def start(self):
            return None

        def attach_wave_consumer(self, _consumer):
            return None

    class Supervisor:
        def __init__(self, *_args, **_kwargs):
            pass

    class WaveConsumer:
        def __init__(self, *_args, **_kwargs):
            pass

    routed = []

    def qualify(_store, _token, *, activity_id, supervisor, **_kwargs):
        routed.append(supervisor)
        qualified = SimpleNamespace(observation=(("version", managed.SUPPORTED_CLAUDE_VERSION),), marker=activity_id)
        return (SimpleNamespace(id=activity_id, state="active"), qualified,
                SimpleNamespace(receipt_sha256="receipt:" + activity_id), tmp_path / activity_id, SimpleNamespace())

    monkeypatch.setattr(managed, "_from_row", lambda _row: SimpleNamespace(base_commit="b" * 40, repository_path=workspace))
    monkeypatch.setattr(managed, "load_input_snapshot", lambda *_args: SimpleNamespace(manifest={}))
    monkeypatch.setattr(managed, "_verify_snapshot_complete", lambda *_args: None)
    monkeypatch.setattr(managed, "begin_child_workspace_preparation", lambda *_args, **_kwargs: ready)
    monkeypatch.setattr(managed, "prepare_workspace", lambda *_args, **_kwargs: ready)
    monkeypatch.setattr(managed, "WorkerChannelServer", Channel)
    monkeypatch.setattr(managed, "Supervisor", Supervisor)
    monkeypatch.setattr(managed, "WaveConsumer", WaveConsumer)
    monkeypatch.setattr(managed, "qualify_managed_claude_runtime", qualify)
    monkeypatch.setattr(managed, "ClaudeHostAdapter", lambda *_args: object())
    request = ClaudeHostRequest(str(tmp_path / "candidate"), str(tmp_path / "credential"), str(tmp_path / "claude"),
                                "claude-opus-5", None, "workspace-write", False, 23, 60)
    token = SimpleNamespace(repository_id="repo", run_id="run", generation=1, planning_scope="1")
    context = SimpleNamespace(
        activity_id="parent", evidence_root=tmp_path / "evidence", workspace=str(tmp_path / "root-workspace"),
        upstream={"project": None, "workstream": None, "session_key": None,
                  "planning_root": str(tmp_path / "root-workspace" / ".planning")})
    session = managed.prepare_managed_claude_session(
        Store(), token, context, ("/gsd-execute-phase", "1"), "request", request)
    review_supervisor = object()
    session.seam.qualify("reviewer-activity", ready, "reviewer-key", "parent", "d" * 64, "reviewer",
                         supervisor=review_supervisor)
    session.seam.qualify("worker-activity", ready, "worker-key", "parent", "d" * 64, "worker")
    assert routed == [review_supervisor, session.supervisor]


def _assembly_run(tmp_path, monkeypatch, run_id, *, at_reviewer=None):
    """The lifecycle assembly; ``at_reviewer(store, token, kwargs, outer)`` runs inside the reviewer's qualification.

    ``outer`` is the supervisor the outer orchestrator's qualification was given (its prepaid group has
    ended by the time the reviewer qualifies), ``kwargs`` the reviewer's ``qualify_managed_runtime`` call.
    """
    primary, authority, repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    for key in ("GSD_RUN_ID", "FFS_RUN_ID", "GSD_RESUME"):
        monkeypatch.delenv(key, raising=False)
    runtime, fake, catalog = _fixture_host(tmp_path, monkeypatch)
    import run_state.supervisor as supervisor_module
    monkeypatch.setattr(supervisor_module, "_gsd_wave_completion_code", lambda *_args, require_wave=True: None)
    fixture_qualify = managed_qualification.qualify_managed_runtime
    seen = {}

    def qualify(store, token, **kwargs):
        bundle = fixture_qualify(store, token, **kwargs)
        if kwargs["role"] != "reviewer":
            seen["outer"] = kwargs["supervisor"]
        elif at_reviewer is not None:
            at_reviewer(store, token, kwargs, seen["outer"])
        return bundle

    monkeypatch.setattr(managed_qualification, "qualify_managed_runtime", qualify)
    result = _frontend_start(env, authority, run_id, runtime, fake, catalog, _draft(tmp_path), "task-swarm",
                             "--scope", "1")
    return result, _facts(authority, repository_id, run_id)


def _reviewer_probe(store, token, kwargs, tmp_path, supervisor, probes):
    """Launch one scripted probe for the reviewer's parent on ``supervisor``; its activity id goes to ``probes``."""
    workspace = kwargs["workspace"]
    request, contract = _scripted_probe(
        store, token, parent_activity_id=kwargs["parent_activity_id"], base_commit=workspace.base_commit,
        repository_path=workspace.repository_path, root=tmp_path, key="boundary-probe")
    probes.append(request.activity_id)
    supervisor.launch_qualification(request, qualification_contract=contract)


@requires_local_confinement
def test_late_child_on_an_ended_group_is_a_typed_managed_refusal(tmp_path, monkeypatch, capsys):
    # The outer supervisor's group is closed by now: a late child must not reach a slot claim.
    probes = []
    result, facts = _assembly_run(tmp_path, monkeypatch, "lc", at_reviewer=lambda store, token, kwargs, outer:
                                  _reviewer_probe(store, token, kwargs, tmp_path, outer, probes))
    envelope = _last_envelope(capsys)
    assert (result, envelope["ok"], envelope["code"]) == (78, False, "RESOURCE_PARENT_GROUP_ENDED")
    assert len(probes) == 1 and _intent_count(facts.store, probes[0]) == 0


@requires_local_confinement
def test_group_close_refusal_is_a_typed_managed_refusal(tmp_path, monkeypatch, capsys):
    def refuse(_registry, _plan):
        raise ResourceGroupRefused("RESOURCE_GROUP_CAS_FAILED")

    monkeypatch.setattr(ResourceParentGroupRegistry, "close_after_parent_end", refuse)
    result, _facts_unused = _assembly_run(tmp_path, monkeypatch, "gc")
    envelope = _last_envelope(capsys)
    assert (result, envelope["ok"], envelope["code"]) == (78, False, "RESOURCE_GROUP_CAS_FAILED")


@requires_local_confinement
def test_reviewer_admission_refusal_is_a_typed_managed_refusal(tmp_path, monkeypatch, capsys):
    def refuse(_coordinator, _requests, *, group_key=None):
        raise ManagedAdmissionRefused("RESOURCE_OBSERVATION_UNAVAILABLE")

    probes = []

    def reviewer_admission(store, token, kwargs, _outer):
        monkeypatch.setattr(SharedResourceCoordinator, "acquire", refuse)
        _reviewer_probe(store, token, kwargs, tmp_path, kwargs["supervisor"], probes)

    result, facts = _assembly_run(tmp_path, monkeypatch, "ra", at_reviewer=reviewer_admission)
    envelope = _last_envelope(capsys)
    assert (result, envelope["ok"], envelope["code"]) == (78, False, "RESOURCE_OBSERVATION_UNAVAILABLE")
    assert len(probes) == 1 and _intent_count(facts.store, probes[0]) == 0


@requires_local_confinement
def test_a_generic_authority_refusal_keeps_its_own_exit_code(tmp_path, monkeypatch, capsys):
    # Guard: only resource-layer refusals move to the managed boundary; a lost fence stays exit 4.
    def lose_fence(_store, _token, _kwargs, _outer):
        raise ControlStoreRefused("FENCE_REVOKED")

    result, _facts_unused = _assembly_run(tmp_path, monkeypatch, "fr", at_reviewer=lose_fence)
    envelope = _last_envelope(capsys)
    assert (result, envelope["code"]) == (4, "FENCE_REVOKED")


@requires_local_confinement
def test_slot_claim_refusal_through_the_supervisor_names_the_admission_recovery(tmp_path, monkeypatch, capsys):
    # _bind_shared_resource types the registry refusal as a SupervisorRefused; it is still a resource refusal.
    def refuse(_registry, _plan, _binding, _demand):
        raise ResourceGroupRefused("RESOURCE_GROUP_SLOT_UNAVAILABLE")

    def child_bind(store, token, kwargs, outer):
        # A child on the outer supervisor, with the ended-group guard stood down so it reaches the claim.
        monkeypatch.setattr(ManagedParentResourceCoordinator, "_group_state", lambda _self: "reserved")
        monkeypatch.setattr(ResourceParentGroupRegistry, "claim_child", refuse)
        _reviewer_probe(store, token, kwargs, tmp_path, outer, [])

    result, _facts_unused = _assembly_run(tmp_path, monkeypatch, "sc", at_reviewer=child_bind)
    envelope = _last_envelope(capsys)
    assert (result, envelope["code"], envelope["recovery_action"]["action"]) == (
        78, "RESOURCE_GROUP_SLOT_UNAVAILABLE", "inspect_managed_admission")
