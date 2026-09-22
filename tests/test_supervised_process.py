"""Real subprocess transport checks; these do not stand in for host canaries."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from process_identity import ProcessIdentity
from run_state.ownership import ControlStore, StartRequest, assert_owner, reserve_resources
from run_state.supervisor import DispatchRequest, Supervisor, SupervisorRefused
from run_state.state import qualified_runtime_tuple_hash
from test_runtime_receipt_authority import _qualified
from run_state.workspace import (
    begin_child_workspace_preparation, inspect_workspace, load_input_snapshot,
    prepare_workspace, revalidate_ready_fence,
)
from test_m4_upstream_context_acceptance import (
    _empty_selection, _env, _register, _repository, _start, _write_manifest,
)


def _allocate_registered_child(store, token, *, key, retry_budget=2):
    with store.read_transaction() as tx:
        parent = tx.execute(
            "SELECT * FROM context_runs WHERE repository_id=? AND run_id=?",
            (token.repository_id, token.run_id),
        ).fetchone()
    preparation = inspect_workspace(store, parent["preparation_id"])
    snapshot = load_input_snapshot(store, preparation)
    pending = begin_child_workspace_preparation(
        store, token, parent_activity_id=parent["activity_id"], request_key=key + ":workspace",
        role="worker", base_commit=preparation.base_commit,
        selected_input_manifest=snapshot.manifest, repository_path=preparation.repository_path,
    )
    ready = prepare_workspace(store, token, pending, input_snapshot=snapshot)
    child = store.create_child_activity(
        token, parent_activity_id=parent["activity_id"], role="worker", request_key=key + ":activity",
        candidate_hash=ready.input_digest, contract_hash="d" * 64, runtime_identity="b" * 64,
        workspace_binding=str(ready.path), workspace_preparation_id=ready.id, retry_budget=retry_budget,
    )
    return child, ready


def setup_owner(tmp_path, *, fault=None):
    primary = _repository(tmp_path)
    authority = tmp_path / "authority"
    repository_id = _register(primary, authority)
    selection = _write_manifest(tmp_path, _empty_selection(primary, repository_id))
    result = _start(authority, primary, selection, "run", env=_env(tmp_path), activity="execute")
    assert result.returncode == 0, (result.stdout, result.stderr)
    context = json.loads(result.stdout)
    store = ControlStore(authority / "control.sqlite3")
    with store.read_transaction() as tx:
        parent = tx.execute("SELECT * FROM context_runs WHERE repository_id=? AND run_id='run'",
                            (repository_id,)).fetchone()
    owner = reserve_resources(store, StartRequest(
        "run", parent["workspace"], parent["objective_digest"], ProcessIdentity.current(),
        repository_id=repository_id, planning_scope=parent["planning_scope"],
    ))
    token = owner.token
    revalidate_ready_fence(store, token, parent["preparation_id"])
    with store.transaction() as tx:
        assert_owner(tx, token)
        changed = tx.execute(
            "UPDATE authority_activities SET generation=? WHERE id=? AND repository_id=? AND run_id=?",
            (token.generation, parent["activity_id"], repository_id, token.run_id),
        ).rowcount
        assert changed == 1
    assert context["activity_id"] == parent["activity_id"]
    store.bind_runtime(token, parent["activity_id"], "b" * 64)
    store.configure_run_limits(token, dispatch_limit=3, token_limit=100, worker_capacity=2)
    child, ready = _allocate_registered_child(store, token, key="first-child")
    assert ready.native_identity is not None
    supervisor = Supervisor(store, token, evidence_root=authority / "evidence", fault_probe=fault)
    command = (sys.executable, "-c", "from pathlib import Path; Path('ran').write_text('yes'); print('real child')")
    request = DispatchRequest(child.id, "first", command, str(ready.path), ready.base_commit,
                              "b" * 64, contract_hash="d" * 64)
    return supervisor, store, request


def test_real_child_waits_for_permit_and_evidence_is_harvested(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)
    handle = supervisor.launch(request)
    result = supervisor.finish(handle, timeout=10, token_usage=0)
    assert result["returncode"] == 0
    assert (Path(request.workspace) / "ran").read_text() == "yes"
    receipt = json.loads(Path(result["evidence"]["locator"]).read_text())
    assert receipt["initial_head"] == request.expected_head
    assert Path(receipt["streams"]["stdout"]["locator"]).read_text() == "real child\n"
    with store.read_transaction() as tx:
        intent = tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (handle.intent_id,)).fetchone()
    assert intent["child_pid"] == handle.process.pid
    assert intent["permit_id"]


@pytest.mark.parametrize("boundary", ["after_intent_commit", "after_spawn_before_ack",
                                     "after_ack_before_authorization", "after_authorization_before_release"])
def test_crash_never_executes_unpermitted_child_or_relaunches_intent(tmp_path, boundary):
    def crash(point):
        if point == boundary:
            raise RuntimeError("injected crash")
    supervisor, store, request = setup_owner(tmp_path, fault=crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        supervisor.launch(request)
    assert not (Path(request.workspace) / "ran").exists()
    reopened = ControlStore(store.db_path)
    resumed = Supervisor(reopened, supervisor.token, evidence_root=supervisor.evidence_root)
    with pytest.raises(SupervisorRefused, match="INTENT_RECONCILIATION_REQUIRED"):
        resumed.launch(request)
    with reopened.read_transaction() as tx:
        assert tx.execute("SELECT count(*) FROM authority_launch_intents").fetchone()[0] == 1
        assert tx.execute("SELECT dispatch_used FROM authority_run_limits").fetchone()[0] == 1


def test_wrong_initial_head_refuses_before_intent_or_child_write(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)
    with pytest.raises(SupervisorRefused, match="FORK_BASE_MISMATCH"):
        supervisor.launch(replace(request, expected_head="0" * 40))
    assert not (Path(request.workspace) / "ran").exists()
    with store.read_transaction() as tx:
        assert tx.execute("SELECT count(*) FROM authority_launch_intents").fetchone()[0] == 0


def test_two_children_actually_overlap(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)
    sibling, ready = _allocate_registered_child(store, supervisor.token, key="second-child")
    requests = [request, replace(request, activity_id=sibling.id, request_key="second-launch",
                                 workspace=str(ready.path), expected_head=ready.base_commit)]
    command = (sys.executable, "-c", """
import time
from pathlib import Path
print(time.monotonic(),flush=True)
Path('started').touch()
end=time.monotonic()+15
while not Path('release').exists():
    if time.monotonic()>end: raise RuntimeError('release deadline')
    time.sleep(.01)
print(time.monotonic(),flush=True)
""")
    # Both registered siblings exist before the dependency wave starts.
    children = []
    try:
        for item in requests:
            children.append(supervisor.launch(replace(item, command=command)))
        deadline = time.monotonic() + 10
        while not all((Path(item.workspace) / "started").exists() for item in requests):
            assert all(child.process.poll() is None for child in children)
            assert time.monotonic() < deadline, "both children did not start"
            time.sleep(.01)
        assert all(child.process.poll() is None for child in children)
        for item in requests:
            (Path(item.workspace) / "release").touch()
        intervals = []
        for child in children:
            result = supervisor.finish(child, timeout=10, token_usage=0)
            assert result["returncode"] == 0, child.stderr_path.read_text()
            intervals.append([float(value) for value in child.stdout_path.read_text().splitlines()])
        assert max(interval[0] for interval in intervals) < min(interval[1] for interval in intervals)
    finally:
        for item in requests:
            (Path(item.workspace) / "release").touch()
        for child in children:
            if child.process.poll() is None:
                child.process.terminate()
                child.process.wait(timeout=5)


def _receipt_bound_request(store, token, request):
    qualified = _qualified(Path(request.workspace))
    runtime_identity = qualified_runtime_tuple_hash(qualified)
    activity = store.get_activity(request.activity_id)
    if activity.state == "pending":
        store.transition_activity(
            token, request.activity_id, expected="pending", new="active",
            reason="fixture runtime qualification",
        )
    with store.transaction() as tx:
        tx.execute(
            "UPDATE authority_activities SET runtime_tuple_hash=? WHERE id=?",
            (runtime_identity, request.activity_id),
        )
        tx.execute(
            "UPDATE authority_child_bindings SET runtime_identity=? WHERE activity_id=?",
            (runtime_identity, request.activity_id),
        )
    receipt = store.commit_runtime_receipt(token, request.activity_id, qualified)
    with store.read_transaction() as tx:
        binding = tx.execute(
            "SELECT candidate_hash FROM authority_child_bindings WHERE activity_id=?",
            (request.activity_id,),
        ).fetchone()
    return replace(
        request, runtime_identity=runtime_identity,
        runtime_receipt_sha256=receipt.receipt_sha256,
        managed_input_sha256=binding["candidate_hash"],
    )


def test_cohort_ack_barrier_releases_complete_wave_and_overlaps(tmp_path):
    supervisor, store, first = setup_owner(tmp_path)
    sibling, ready = _allocate_registered_child(store, supervisor.token, key="cohort-second")
    second = replace(
        first, activity_id=sibling.id, request_key="cohort-second-launch",
        workspace=str(ready.path), expected_head=ready.base_commit,
    )
    requests = tuple(_receipt_bound_request(store, supervisor.token, item) for item in (first, second))
    command = (sys.executable, "-c", """
import time
from pathlib import Path
Path('started').touch()
end=time.monotonic()+15
while not Path('release').exists():
    if time.monotonic()>end: raise RuntimeError('release deadline')
    time.sleep(.01)
""")
    handles = []
    try:
        handles = list(supervisor.launch_cohort(
            tuple(replace(item, command=command) for item in requests), request_key="wave-1",
        ))
        deadline = time.monotonic() + 10
        while not all((Path(item.workspace) / "started").exists() for item in requests):
            assert all(handle.process.poll() is None for handle in handles)
            assert time.monotonic() < deadline, "cohort members did not overlap"
            time.sleep(.01)
        with store.read_transaction() as tx:
            cohort = tx.execute("SELECT * FROM authority_launch_cohorts").fetchone()
            intents = tx.execute("SELECT * FROM authority_launch_intents ORDER BY id").fetchall()
        assert cohort["state"] == "released_to_execute"
        assert all(row["permit_id"] and row["state"] == "released_to_execute" for row in intents)
        for item in requests:
            (Path(item.workspace) / "release").touch()
        assert all(supervisor.finish(handle, timeout=10, token_usage=0)["returncode"] == 0
                   for handle in handles)
    finally:
        for item in requests:
            (Path(item.workspace) / "release").touch()
        for handle in handles:
            if handle.process.poll() is None:
                handle.process.terminate()
                handle.process.wait(timeout=5)
