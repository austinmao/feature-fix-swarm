"""Durable aggregate launch limits for the production ControlStore API."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import subprocess
import sys

import pytest


LIB = Path(__file__).resolve().parents[2]
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))
TESTS = LIB.parent / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))


HASH = "a" * 64
RUNTIME = "b" * 64
_PROCESSES = []


@pytest.fixture(autouse=True)
def finish_actual_children():
    yield
    for child in _PROCESSES:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
    _PROCESSES.clear()

def _evidence(tmp_path: Path, name: str = "result") -> dict:
    path = tmp_path / f"{name}.json"
    payload = f'{{"name":"{name}"}}'.encode()
    path.write_bytes(payload)
    return {"locator": str(path), "sha256": hashlib.sha256(payload).hexdigest()}


def _mark_child_dead(store, token, intent_id: str) -> None:
    """Acknowledge a real child and observe its death before settlement."""
    child = _acknowledge_and_authorize(store, token, intent_id)
    child.terminate()
    child.wait(timeout=5)


def _mark_child_live(store, token, intent_id: str):
    return _acknowledge_and_authorize(store, token, intent_id)


def _acknowledge_and_authorize(store, token, intent_id: str):
    """Use a real direct child; do not synthesize the handshake in this test."""
    from process_identity import ProcessIdentity

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    _PROCESSES.append(child)
    identity = ProcessIdentity.from_pid(child.pid)
    acknowledgement = store.acknowledge_child(intent_id, token, identity)
    permit = store.authorize_child(acknowledgement, token)
    assert permit.allowed is True
    return child


def _owned(tmp_path: Path, *, run_id: str = "run-limits"):
    from run_state.workspace import begin_workspace_preparation, prepare_workspace
    from test_m4_workspace_acceptance import _owner, _repository, _git_text

    primary = _repository(tmp_path)
    store, ownership, workspace, _repository_id = _owner(tmp_path, primary, run_id)
    pending = begin_workspace_preparation(
        store, ownership.token, run_id=run_id, workspace=workspace,
        branch=f"ffs/runs/{run_id}", base_commit=_git_text("rev-parse", "HEAD", cwd=primary),
        selected_input_manifest={"entries": []}, repository_path=primary,
    )
    prepare_workspace(store, ownership.token, pending)
    with store.read_transaction() as tx:
        parent_id = tx.execute("SELECT activity_id FROM context_runs WHERE run_id=?", (run_id,)).fetchone()[0]
    parent = store.bind_runtime(ownership.token, parent_id, RUNTIME)
    return store, ownership, parent


def _child(store, token, parent_id: str, key: str, *, role: str = "worker"):
    from run_state.workspace import begin_child_workspace_preparation, prepare_workspace

    parent = store.get_activity(parent_id)
    if parent.state == "pending":
        store.transition_activity(token, parent_id, expected="pending", new="active")
    with store.read_transaction() as tx:
        root = tx.execute(
            "SELECT w.* FROM context_workspaces w JOIN context_runs r ON r.preparation_id=w.preparation_id "
            "WHERE r.repository_id=? AND r.run_id=?", (token.repository_id, token.run_id),
        ).fetchone()
    pending = begin_child_workspace_preparation(
        store, token, parent_activity_id=parent_id, request_key="workspace:" + key, role=role,
        base_commit=root["base_commit"], selected_input_manifest={"entries": []},
        repository_path=Path(root["repository_path"]),
    )
    ready = prepare_workspace(store, token, pending)
    return store.create_child_activity(
        token, parent_activity_id=parent_id, role=role, request_key=key,
        candidate_hash=HASH, contract_hash="d" * 64, runtime_identity=RUNTIME,
        workspace_binding=str(ready.path), workspace_preparation_id=ready.id, retry_budget=1,
    )


def test_limits_are_immutable_across_restart_revision_and_grandchildren(tmp_path: Path) -> None:
    from run_state.ownership import OwnershipRefused
    from run_state.state import ControlStore

    store, ownership, parent = _owned(tmp_path)
    configured = store.configure_run_limits(
        ownership.token, dispatch_limit=2, token_limit=7, worker_capacity=2,
    )
    assert configured["dispatch_used"] == 0
    # A new process/store instance observes the same immutable aggregate row.
    recovered = ControlStore(store.db_path)
    assert recovered.configure_run_limits(
        ownership.token, dispatch_limit=2, token_limit=7, worker_capacity=2,
    )["token_limit"] == 7
    with pytest.raises(OwnershipRefused, match="RUN_LIMITS_IMMUTABLE"):
        recovered.configure_run_limits(
            ownership.token, dispatch_limit=3, token_limit=7, worker_capacity=2,
        )

    child = _child(recovered, ownership.token, parent.id, "child")
    grandchild = _child(recovered, ownership.token, child.id, "grandchild", role="reviewer")
    one = recovered.reserve_launch(child.id, ownership.token, token_reservation=3)
    _mark_child_dead(recovered, ownership.token, one.id)
    recovered.complete_launch(one.id, ownership.token, status="failed", evidence=_evidence(tmp_path), token_usage=2)
    two = recovered.reserve_launch(grandchild.id, ownership.token, token_reservation=5)
    assert two.reused is False
    _mark_child_live(recovered, ownership.token, two.id)
    revision_child = _child(recovered, ownership.token, parent.id, "revision-child")
    with pytest.raises(OwnershipRefused, match="DISPATCH_LIMIT_EXHAUSTED"):
        recovered.reserve_launch(revision_child.id, ownership.token, token_reservation=0)


def test_reservation_is_atomic_under_concurrent_children(tmp_path: Path) -> None:
    from run_state.ownership import OwnershipRefused

    store, ownership, parent = _owned(tmp_path)
    store.configure_run_limits(ownership.token, dispatch_limit=3, token_limit=6, worker_capacity=2)
    children = [
        _child(store, ownership.token, parent.id, "child-0"),
        _child(store, ownership.token, parent.id, "child-1", role="reviewer"),
        _child(store, ownership.token, parent.id, "child-2", role="recovery"),
    ]

    def reserve(activity_id: str):
        try:
            return store.reserve_launch(activity_id, ownership.token, token_reservation=2)
        except OwnershipRefused as error:
            return error.code

    with ThreadPoolExecutor(max_workers=3) as workers:
        result = list(workers.map(lambda activity: reserve(activity.id), children))
    intents = [item for item in result if not isinstance(item, str)]
    # The first pre-acknowledgement intent is an uncertain spawn boundary, so
    # concurrent callers fail closed instead of assuming a second launch fits.
    assert len(intents) == 1
    assert result.count("INTENT_RECONCILIATION_REQUIRED") == 2
    processes = []
    try:
        processes.append(_acknowledge_and_authorize(store, ownership.token, intents[0].id))
        second = store.reserve_launch(
            next(child.id for child in children if child.id != intents[0].activity_id),
            ownership.token, token_reservation=2,
        )
        processes.append(_acknowledge_and_authorize(store, ownership.token, second.id))
        final_child = next(
            child for child in children
            if child.id not in {intents[0].activity_id, second.activity_id}
        )
        with pytest.raises(OwnershipRefused, match="WORKER_CAPACITY_EXHAUSTED"):
            store.reserve_launch(final_child.id, ownership.token, token_reservation=2)
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            process.wait(timeout=5)
    with store.read_transaction() as tx:
        row = tx.execute(
            "SELECT dispatch_used,token_committed FROM authority_run_limits WHERE repository_id=? AND run_id=?",
            (ownership.token.repository_id, ownership.token.run_id),
        ).fetchone()
    assert tuple(row) == (2, 4)


def test_only_the_managed_outer_path_can_be_capacity_exempt(tmp_path: Path) -> None:
    """A direct child cannot turn its launch into an unmetered worker slot."""
    from run_state.ownership import OwnershipRefused

    store, ownership, parent = _owned(tmp_path)
    store.configure_run_limits(ownership.token, dispatch_limit=2, token_limit=4, worker_capacity=1)
    child = _child(store, ownership.token, parent.id, "ordinary-child")
    with pytest.raises(OwnershipRefused, match="CAPACITY_EXEMPTION_INVALID"):
        store.reserve_launch(
            child.id, ownership.token, token_reservation=1,
            managed_outer_capacity_exempt=True,
        )


def test_unknown_or_malformed_telemetry_keeps_the_intent_and_allowance(tmp_path: Path) -> None:
    from run_state.ownership import OwnershipRefused

    store, ownership, parent = _owned(tmp_path)
    store.configure_run_limits(ownership.token, dispatch_limit=2, token_limit=3, worker_capacity=1)
    child = _child(store, ownership.token, parent.id, "child")
    intent = store.reserve_launch(child.id, ownership.token, token_reservation=3)
    with pytest.raises(OwnershipRefused, match="MALFORMED_TELEMETRY"):
        store.complete_launch(
            intent.id, ownership.token, status="failed", evidence=_evidence(tmp_path), token_usage=None,
        )
    uncertain = store.complete_launch(
        intent.id, ownership.token, status="uncertain", evidence=_evidence(tmp_path, "uncertain"), token_usage=None,
    )
    assert uncertain.state == "uncertain"
    replay = store.reserve_launch(child.id, ownership.token, token_reservation=3)
    assert replay.id == intent.id and replay.reused is True
    sibling = _child(store, ownership.token, parent.id, "sibling")
    with pytest.raises(OwnershipRefused, match="INTENT_RECONCILIATION_REQUIRED"):
        store.reserve_launch(sibling.id, ownership.token, token_reservation=1)


def test_reopen_keeps_preacknowledgement_intent_uncertain_without_refund(tmp_path: Path) -> None:
    from run_state.ownership import OwnershipRefused
    from run_state.state import ControlStore

    store, ownership, parent = _owned(tmp_path)
    store.configure_run_limits(ownership.token, dispatch_limit=3, token_limit=6, worker_capacity=2)
    first = _child(store, ownership.token, parent.id, "first")
    second = _child(store, ownership.token, parent.id, "second")
    intent = store.reserve_launch(first.id, ownership.token, token_reservation=2)

    # Opening after a host restart cannot infer whether the child reached its
    # acknowledgement boundary.  It must block every sibling, not only a
    # retry of the same activity.
    reopened = ControlStore(store.db_path)
    with pytest.raises(OwnershipRefused, match="INTENT_RECONCILIATION_REQUIRED"):
        reopened.reserve_launch(second.id, ownership.token, token_reservation=1)
    assert reopened.recover_intent(intent.id, ownership.token).state == "reconcile_required"
    with reopened.read_transaction() as tx:
        row = tx.execute(
            "SELECT dispatch_used,token_committed FROM authority_run_limits WHERE repository_id=? AND run_id=?",
            (ownership.token.repository_id, ownership.token.run_id),
        ).fetchone()
    assert tuple(row) == (1, 2)


def test_revision_and_full_ancestry_cannot_reset_or_bypass_limits(tmp_path: Path) -> None:
    from run_state.ownership import OwnershipRefused

    store, ownership, root = _owned(tmp_path)
    store.configure_run_limits(ownership.token, dispatch_limit=1, token_limit=2, worker_capacity=2)
    child = _child(store, ownership.token, root.id, "child")
    grandchild = _child(store, ownership.token, child.id, "grandchild")
    store.transition_activity(ownership.token, root.id, expected="active", new="failed")
    with pytest.raises(OwnershipRefused, match="PARENT_ACTIVITY_INVALID"):
        store.reserve_launch(grandchild.id, ownership.token, token_reservation=1)
    store.transition_activity(ownership.token, grandchild.id, expected="pending", new="failed")
    store.transition_activity(ownership.token, child.id, expected="active", new="failed")

    # A renamed/revised planning activity gets a new activity id, but draws
    # from the original run row and cannot configure a new allowance.
    revision = store.select_activity(
        ownership.token, kind="execute", input_digest="e" * 64, resume=False, revise=True,
    )
    revision = store.bind_runtime(ownership.token, revision.id, RUNTIME)
    revision_child = _child(store, ownership.token, revision.id, "revision-child")
    revision_intent = store.reserve_launch(revision_child.id, ownership.token, token_reservation=2)
    _mark_child_live(store, ownership.token, revision_intent.id)
    with pytest.raises(OwnershipRefused, match="RUN_LIMITS_IMMUTABLE"):
        store.configure_run_limits(ownership.token, dispatch_limit=2, token_limit=10, worker_capacity=2)
    another = _child(store, ownership.token, revision.id, "another")
    with pytest.raises(OwnershipRefused, match="DISPATCH_LIMIT_EXHAUSTED"):
        store.reserve_launch(another.id, ownership.token, token_reservation=0)


def test_terminal_completion_requires_hashed_evidence_and_dead_child(tmp_path: Path) -> None:
    from run_state.ownership import OwnershipRefused

    store, ownership, parent = _owned(tmp_path)
    store.configure_run_limits(ownership.token, dispatch_limit=2, token_limit=4, worker_capacity=1)
    child = _child(store, ownership.token, parent.id, "child")
    intent = store.reserve_launch(child.id, ownership.token, token_reservation=3)
    process = _mark_child_live(store, ownership.token, intent.id)
    with pytest.raises(OwnershipRefused, match="EVIDENCE_INVALID"):
        store.complete_launch(
            intent.id, ownership.token, status="failed",
            evidence={"locator": str(tmp_path / "missing"), "sha256": "a" * 64}, token_usage=1,
        )
    with pytest.raises(OwnershipRefused, match="CHILD_NOT_TERMINAL"):
        store.complete_launch(
            intent.id, ownership.token, status="failed", evidence=_evidence(tmp_path), token_usage=1,
        )
    process.terminate()
    process.wait(timeout=5)
    terminal_evidence = _evidence(tmp_path, "dead")
    store.complete_launch(
        intent.id, ownership.token, status="failed", evidence=terminal_evidence, token_usage=1,
    )
    assert store.complete_launch(
        intent.id, ownership.token, status="failed", evidence=terminal_evidence, token_usage=1,
    ).reused is True
    sibling = _child(store, ownership.token, parent.id, "sibling")
    # Terminal evidence releases only the unspent reservation: 3 reserved,
    # one metered, then one more reservation fits under the four-token run cap.
    assert store.reserve_launch(sibling.id, ownership.token, token_reservation=2).reused is False
