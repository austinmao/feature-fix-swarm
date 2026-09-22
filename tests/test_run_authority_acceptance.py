"""Independent durable activity, child-permit, accounting, and gate oracles."""
from __future__ import annotations

import errno
import hashlib
import inspect
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "lib"
sys.path.insert(0, str(LIB))
INPUT_V1 = "1" * 64
INPUT_V2 = "2" * 64
RUNTIME_V1 = "3" * 64
RUNTIME_V2 = "4" * 64
GATE_INPUTS = {
    "candidate": "a" * 64,
    "runtime": "b" * 64,
    "config": "c" * 64,
    "policy": "d" * 64,
    "dependencies": "e" * 64,
}
DECISION_EXPIRY = "2099-01-01T00:00:00Z"
_OWNED_CHILDREN: set[subprocess.Popen[str]] = set()
FAULT_CHECKPOINTS = tuple(
    f"{operation}.{boundary}"
    for operation in ("reserve_launch", "acknowledge_child", "authorize_child")
    for boundary in ("after_write_before_commit", "after_commit_before_return")
)


_ADMISSION_CRASH_OWNER = textwrap.dedent(
    """
    from dataclasses import asdict, replace
    from pathlib import Path
    import json, os, signal, subprocess, sys
    from process_identity import ProcessIdentity
    from test_supervised_process import setup_owner

    root, checkpoint, metadata, reached = map(Path, sys.argv[1:])
    root.mkdir(parents=True)
    supervisor, store, request = setup_owner(root)
    request = replace(request, monitor_result=True, token_reservation=7,
                      command=(sys.executable, '-c', "from pathlib import Path; Path('forbidden-effect').write_text('executed')"))
    payload = {'db': str(store.db_path), 'workspace': request.workspace,
               'activity_id': request.activity_id, 'request_key': request.request_key,
               'generation': supervisor.token.generation, 'monitors': []}
    def save():
        stage = metadata.with_suffix('.stage')
        with stage.open('w') as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stage, metadata)
    with store.read_transaction() as tx:
        payload['remaining_retry_budget'] = tx.execute(
            'SELECT remaining_retry_budget FROM authority_activities WHERE id=?',
            (request.activity_id,),
        ).fetchone()[0]
    save()
    original = subprocess.Popen
    def observed_spawn(argv, *args, **kwargs):
        process = original(argv, *args, **kwargs)
        if argv[:4] == [sys.executable, '-m', 'run_state.supervisor', '_monitor']:
            payload['monitors'].append(asdict(ProcessIdentity.from_pid(process.pid)))
            save()
        return process
    subprocess.Popen = observed_spawn
    def crash(observed):
        if observed == str(checkpoint):
            with reached.open('w') as stream:
                stream.write(observed)
                stream.flush()
                os.fsync(stream.fileno())
            os.kill(os.getpid(), signal.SIGKILL)
    store.fault_probe = crash
    supervisor.launch(request)
    raise AssertionError('selected admission checkpoint was not reached')
    """
)


@pytest.fixture(autouse=True)
def _cleanup_owned_children():
    yield
    for child in tuple(_OWNED_CHILDREN):
        _stop_child(child)
        _OWNED_CHILDREN.discard(child)


def _env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("FFS_RUN_ID", "GSD_RUN_ID", "GSD_RESUME", "GSD_WORKSTREAM",
                "GSD_PROJECT", "GSD_SESSION_KEY", "CLAUDE_CONFIG_DIR", "GSD_HOME"):
        env.pop(key, None)
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    env.update(HOME=str(home), TMPDIR=str(home / "tmp"),
               PYTHONPATH=f"{LIB}:{env.get('PYTHONPATH', '')}")
    return env


def _owned_child(tmp_path: Path) -> subprocess.Popen[str]:
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import json,os,sys; print(json.dumps({'pid':os.getpid()}),flush=True); "
         "command=sys.stdin.readline().strip(); print(command,flush=True)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=_env(tmp_path),
    )
    _OWNED_CHILDREN.add(child)
    return child


def _line(child: subprocess.Popen[str], timeout: float = 10) -> str:
    assert child.stdout is not None
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ)
        assert selector.select(timeout), f"child output deadline exceeded pid={child.pid}"
    line = child.stdout.readline()
    if line:
        return line.rstrip("\n")
    diagnostic = child.stderr.read() if child.poll() is not None and child.stderr else "child closed stdout"
    raise AssertionError(diagnostic)


def _stop_child(child: subprocess.Popen[str]) -> None:
    if child.poll() is not None:
        _OWNED_CHILDREN.discard(child)
        return
    if child.stdin:
        try:
            child.stdin.write("stop\n"); child.stdin.flush()
        except BrokenPipeError:
            pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill(); child.wait(timeout=5)
    _OWNED_CHILDREN.discard(child)


def _stop_observed_fixture_process(identity: object) -> None:
    from process_identity import LIVE, probe_identity

    if probe_identity(identity) != LIVE:
        return
    os.kill(identity.pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while probe_identity(identity) == LIVE and time.monotonic() < deadline:
        time.sleep(0.01)
    if probe_identity(identity) == LIVE:
        os.kill(identity.pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while probe_identity(identity) == LIVE and time.monotonic() < deadline:
            time.sleep(0.01)
    assert probe_identity(identity) != LIVE


def _owned_store(tmp_path: Path, *, run_id: str = "authority-run", budget: int = 2):
    from run_state.ownership import ControlStore, ProcessIdentity, StartRequest, reserve_resources

    store = ControlStore(tmp_path / "authority" / "control.sqlite3")
    ownership = reserve_resources(store, StartRequest(
        run_id, str(tmp_path / f"workspace-{run_id}"), f"objective-{run_id}",
        ProcessIdentity.current(), repository_id="fixture-repository", planning_scope="fixture-scope",
    ))
    activity = store.create_activity(
        ownership.token, kind="plan", input_digest=INPUT_V1,
        retry_budget=budget, request_key=f"activity:{run_id}",
    )
    store.bind_runtime(ownership.token, activity.id, RUNTIME_V1)
    return store, ownership, activity


def _record_decision_contract(store, token, **kwargs):
    """Reach the current decision behavior before the additive expiry API exists."""
    if "expires_at" not in inspect.signature(store.record_decision).parameters:
        kwargs.pop("expires_at", None)
    return store.record_decision(token, **kwargs)


def _capacity_fixture_root(tmp_path: Path) -> Path:
    raw = os.environ.get("FFS_TEST_CAPACITY_ROOT")
    if raw is None:
        pytest.skip("requires root-provided FFS_TEST_CAPACITY_ROOT native fixture")
    supplied = Path(raw)
    assert supplied.is_absolute()
    assert not supplied.is_symlink()
    root = supplied.resolve(strict=True)
    info = root.stat()
    assert stat.S_ISDIR(info.st_mode)
    assert info.st_uid == os.getuid()
    assert stat.S_IMODE(info.st_mode) & 0o077 == 0
    assert info.st_dev != tmp_path.resolve().stat().st_dev
    filesystem = os.statvfs(root)
    capacity = filesystem.f_frsize * filesystem.f_blocks
    assert 0 < capacity <= 64 * 1024 * 1024
    return root


def _fill_owned_capacity(filler: Path) -> None:
    descriptor = os.open(filler, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        for size in (1024 * 1024, 4096, 1):
            saw_enospc = False
            block = b"x" * size
            while True:
                try:
                    written = os.write(descriptor, block)
                    assert written > 0
                except OSError as error:
                    assert error.errno == errno.ENOSPC
                    saw_enospc = True
                    break
            assert saw_enospc
        with pytest.raises(OSError) as full:
            os.write(descriptor, b"x")
        assert full.value.errno == errno.ENOSPC
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("mutation", ["reserve_launch", "record_event_once"])
def test_native_capacity_exhaustion_is_typed_and_atomic(
    tmp_path: Path,
    mutation: str,
) -> None:
    capacity_root = _capacity_fixture_root(tmp_path)
    from run_state.ownership import ControlStore, ControlStoreRefused, reserve_launch

    owned_root = Path(tempfile.mkdtemp(prefix="ffs-authority-enospc-", dir=capacity_root))
    filler = owned_root / "owned-filler.bin"
    store = ownership = activity = None
    try:
        store, ownership, activity = _owned_store(
            owned_root, run_id=f"capacity-{mutation}", budget=2,
        )
        before_activity = store.get_activity(activity.id)
        before_events = list(store.enumerate_events(run_id=ownership.run_id))
        _fill_owned_capacity(filler)

        with pytest.raises(ControlStoreRefused) as exhausted:
            if mutation == "reserve_launch":
                reserve_launch(store, activity.id, ownership.token)
            else:
                store.record_event_once(
                    ownership.token,
                    activity.id,
                    "capacity:event",
                    {"mutation": mutation},
                )
        assert exhausted.value.code == "STORE_IO"

        filler.unlink()
        reopened = ControlStore(store.db_path)
        assert reopened.get_activity(activity.id) == before_activity
        assert list(reopened.enumerate_events(run_id=ownership.run_id)) == before_events
        if mutation == "reserve_launch":
            intent = reserve_launch(reopened, activity.id, ownership.token)
            assert intent.id
            assert reopened.get_activity(activity.id).remaining_retry_budget == 1
        else:
            recorded = reopened.record_event_once(
                ownership.token,
                activity.id,
                "capacity:event",
                {"mutation": mutation},
            )
            assert reopened.record_event_once(
                ownership.token,
                activity.id,
                "capacity:event",
                {"mutation": mutation},
            ) == recorded
            matching = [
                event
                for event in reopened.enumerate_events(run_id=ownership.run_id)
                if event["event_type"] == "capacity:event"
            ]
            assert len(matching) == 1
    finally:
        if filler.exists():
            filler.unlink()
        shutil.rmtree(owned_root)


def test_fenced_child_restart_tracer(tmp_path: Path) -> None:
    from run_state.ownership import (
        ControlStore, OwnershipRefused, ProcessIdentity, acknowledge_child,
        authorize_child, recover_intent, reserve_launch,
    )

    store, ownership, activity = _owned_store(tmp_path)
    intent = reserve_launch(store, activity.id, ownership.token)
    assert store.get_activity(activity.id).remaining_retry_budget == 1

    child = _owned_child(tmp_path)
    try:
        assert json.loads(_line(child))["pid"] == child.pid
        child_identity = ProcessIdentity.from_pid(child.pid)
        with pytest.raises(OwnershipRefused) as wrong_child:
            acknowledge_child(store, intent.id, ownership.token, ProcessIdentity.current())
        assert wrong_child.value.code == "CHILD_IDENTITY_MISMATCH"

        acknowledgement = acknowledge_child(store, intent.id, ownership.token, child_identity)
        permit = authorize_child(store, acknowledgement, ownership.token)
        assert permit.allowed is True
        assert permit.intent_id == intent.id

        reopened = ControlStore(store.db_path)
        recovered = recover_intent(reopened, intent.id, ownership.token)
        assert recovered.state == "released_to_execute"
        assert recovered.child_identity == child_identity
        assert authorize_child(reopened, acknowledgement, ownership.token) == permit
        assert reserve_launch(reopened, activity.id, ownership.token).id == intent.id
        assert reopened.get_activity(activity.id).remaining_retry_budget == 1

        assert child.stdin is not None
        child.stdin.write("execute\n"); child.stdin.flush()
        assert _line(child) == "execute"
        assert child.wait(timeout=10) == 0
        assert reopened.get_run_control(ownership.run_id).state != "complete"
    finally:
        _stop_child(child)


def test_first_child_binding_requires_native_direct_parent_relation(tmp_path: Path) -> None:
    from run_state.ownership import (
        OwnershipRefused, ProcessIdentity, acknowledge_child, reserve_launch,
    )

    store, ownership, activity = _owned_store(tmp_path, run_id="direct-child")
    intent = reserve_launch(store, activity.id, ownership.token)
    helper_program = textwrap.dedent(
        """
        import json, subprocess, sys
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print(json.dumps({"grandchild_pid": child.pid}), flush=True)
        sys.stdin.readline()
        child.terminate()
        child.wait(timeout=5)
        """
    )
    helper = subprocess.Popen(
        [sys.executable, "-c", helper_program],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=_env(tmp_path / "grandchild-helper"),
    )
    _OWNED_CHILDREN.add(helper)
    direct = None
    try:
        grandchild_pid = json.loads(_line(helper))["grandchild_pid"]
        with pytest.raises(OwnershipRefused) as unrelated:
            acknowledge_child(
                store, intent.id, ownership.token,
                ProcessIdentity.from_pid(grandchild_pid),
            )
        assert unrelated.value.code == "CHILD_IDENTITY_MISMATCH"

        direct = _owned_child(tmp_path / "direct-child-process")
        assert json.loads(_line(direct))["pid"] == direct.pid
        accepted = acknowledge_child(
            store, intent.id, ownership.token, ProcessIdentity.from_pid(direct.pid),
        )
        assert accepted.intent_id == intent.id
    finally:
        _stop_child(helper)
        if direct is not None:
            _stop_child(direct)


@pytest.mark.parametrize(
    ("operation", "boundary"),
    [tuple(checkpoint.split(".", 1)) for checkpoint in FAULT_CHECKPOINTS],
    ids=FAULT_CHECKPOINTS,
)
def test_supervisor_sigkill_retains_committed_admission_without_duplicate_effect(
    tmp_path: Path, operation: str, boundary: str,
) -> None:
    """Exercise the production Supervisor and its actual transactional kill points."""
    from process_identity import DEAD, ProcessIdentity, probe_identity
    from run_state.ownership import ControlStore, StartRequest, reserve_resources

    checkpoint = f"{operation}.{boundary}"
    metadata = tmp_path / "owner-metadata.json"
    reached = tmp_path / "checkpoint-reached"
    environment = _env(tmp_path / "owner-environment")
    environment["PYTHONPATH"] = str(LIB) + os.pathsep + str(ROOT / "tests")
    owner = subprocess.Popen(
        [sys.executable, "-c", _ADMISSION_CRASH_OWNER, str(tmp_path / "owner"),
         checkpoint, str(metadata), str(reached)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment,
    )
    _OWNED_CHILDREN.add(owner)
    payload = None
    native_identity = None
    try:
        stdout, stderr = owner.communicate(timeout=30)
        assert owner.returncode == -signal.SIGKILL, (stdout, stderr)
        assert reached.read_text() == checkpoint
        payload = json.loads(metadata.read_text())
        # Killing an owner before permit delivery never executes native work.
        for monitor in payload["monitors"]:
            identity = ProcessIdentity(**monitor)
            deadline = time.monotonic() + 10
            while probe_identity(identity) != DEAD and time.monotonic() < deadline:
                time.sleep(.02)
            assert probe_identity(identity) == DEAD, "monitor did not close after owner channel EOF"
        assert not (Path(payload["workspace"]) / "forbidden-effect").exists()
        store = ControlStore(Path(payload["db"]))
        with store.read_transaction() as tx:
            run = tx.execute("SELECT * FROM context_runs").fetchone()
            rows = [dict(row) for row in tx.execute("SELECT * FROM authority_launch_intents")]
            before_limits = dict(tx.execute("SELECT * FROM authority_run_limits").fetchone())
            before_accounting = [tuple(row) for row in tx.execute("SELECT * FROM authority_launch_accounting")]
        committed = checkpoint != "reserve_launch.after_write_before_commit"
        assert len(rows) == len(before_accounting) == int(committed)
        assert before_limits["dispatch_used"] == int(committed)
        assert before_limits["token_committed"] == (7 if committed else 0)
        assert before_limits["token_used"] == 0
        assert store.get_activity(payload["activity_id"]).remaining_retry_budget == payload["remaining_retry_budget"] - int(committed)
        if operation == "reserve_launch":
            assert payload["monitors"] == []
        else:
            assert len(payload["monitors"]) == 1
        successor = reserve_resources(store, StartRequest(
            run["run_id"], run["workspace"], run["objective_digest"], ProcessIdentity.current(),
            repository_id=run["repository_id"], planning_scope=run["planning_scope"],
        )).token
        assert successor.generation > payload["generation"]
        if committed:
            row = rows[0]
            expected_stage = {
                "reserve_launch.after_commit_before_return": "reserved",
                "acknowledge_child.after_write_before_commit": "reserved",
                "acknowledge_child.after_commit_before_return": "acknowledged",
                "authorize_child.after_write_before_commit": "acknowledged",
                "authorize_child.after_commit_before_return": "released_to_execute",
            }[checkpoint]
            assert row["state"] == expected_stage
            assert row["generation"] == payload["generation"]
            assert bool(row["permit_id"]) == (checkpoint == "authorize_child.after_commit_before_return")
            assert bool(row["acknowledgement_id"]) == (expected_stage != "reserved")
            if row["child_pid"] is not None:
                native_identity = ProcessIdentity(row["child_host_id"], row["child_boot_id"], row["child_pid"], row["child_start_token"])
                deadline = time.monotonic() + 10
                while probe_identity(native_identity) != DEAD and time.monotonic() < deadline:
                    time.sleep(.02)
                assert probe_identity(native_identity) == DEAD, "unreleased native child survived channel EOF"
            with store.read_transaction() as tx:
                event = tx.execute(
                    "SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
                    "WHERE k.activity_id=? AND k.idempotency_key=?",
                    (payload["activity_id"], "dispatch-request:" + payload["request_key"]),
                ).fetchone()
                before_events = [tuple(event) for event in tx.execute("SELECT * FROM control_events")]
            request_data = json.loads(event["payload"])["data"]["request"]
            recovered = store.recover_intent(row["id"], successor)
            assert recovered.id == row["id"]
            assert recovered.state in {"reconcile_required", "closed_dead"}
            replay = store.reserve_launch(payload["activity_id"], successor, token_reservation=7,
                                         request_key=payload["request_key"], request_payload=request_data)
            assert replay.id == row["id"] and replay.reused
            with store.read_transaction() as tx:
                retained = dict(tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (row["id"],)).fetchone())
                after_events = [tuple(event) for event in tx.execute("SELECT * FROM control_events")]
            for key in ("generation", "child_host_id", "child_boot_id", "child_pid",
                        "child_start_token", "acknowledgement_id", "permit_id"):
                assert retained[key] == row[key]
            assert after_events == before_events
        with store.read_transaction() as tx:
            assert dict(tx.execute("SELECT * FROM authority_run_limits").fetchone()) == before_limits
            assert [tuple(row) for row in tx.execute("SELECT * FROM authority_launch_accounting")] == before_accounting
            assert tx.execute("SELECT count(*) FROM authority_launch_intents").fetchone()[0] == int(committed)
        assert not (Path(payload["workspace"]) / "forbidden-effect").exists()
    finally:
        _stop_child(owner)
        if payload is not None:
            for monitor in payload["monitors"]:
                _stop_observed_fixture_process(ProcessIdentity(**monitor))
        if native_identity is not None:
            _stop_observed_fixture_process(native_identity)


def test_restart_before_spawn_retains_spent_intent_and_budget(tmp_path: Path) -> None:
    from run_state.ownership import ControlStore, recover_intent, reserve_launch

    store, ownership, activity = _owned_store(tmp_path, run_id="pre-spawn")
    intent = reserve_launch(store, activity.id, ownership.token)
    reopened = ControlStore(store.db_path)
    recovered = recover_intent(reopened, intent.id, ownership.token)
    # An intent with no acknowledged process is uncertain after reconciliation;
    # reopening the connection cannot restore permission to spawn it.
    assert recovered.state == "reconcile_required"
    assert recovered.id == intent.id and recovered.child_identity is None
    with reopened.read_transaction() as tx:
        before = tuple(tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (intent.id,)).fetchone())
    assert reserve_launch(reopened, activity.id, ownership.token).id == intent.id
    with reopened.read_transaction() as tx:
        after = tuple(tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (intent.id,)).fetchone())
    assert after == before
    assert reopened.get_activity(activity.id).remaining_retry_budget == 1


def test_restart_after_acknowledgement_keeps_exact_child_and_no_replacement(
    tmp_path: Path,
) -> None:
    from run_state.ownership import (
        ControlStore, OwnershipRefused, ProcessIdentity, acknowledge_child,
        recover_intent, reserve_launch,
    )

    store, ownership, activity = _owned_store(tmp_path, run_id="post-ack")
    intent = reserve_launch(store, activity.id, ownership.token)
    child = _owned_child(tmp_path)
    try:
        assert json.loads(_line(child))["pid"] == child.pid
        identity = ProcessIdentity.from_pid(child.pid)
        acknowledge_child(store, intent.id, ownership.token, identity)
        reopened = ControlStore(store.db_path)
        recovered = recover_intent(reopened, intent.id, ownership.token)
        # A live child with no permit remains retained but cannot be re-ACKed
        # or replaced after reconciliation.
        assert recovered.state == "reconcile_required"
        assert recovered.child_identity == identity
        with reopened.read_transaction() as tx:
            before = tuple(tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (intent.id,)).fetchone())
        for candidate in (ProcessIdentity.current(), identity):
            with pytest.raises(OwnershipRefused) as replacement:
                acknowledge_child(reopened, intent.id, ownership.token, candidate)
            assert replacement.value.code == "OWNER_UNKNOWN"
        with reopened.read_transaction() as tx:
            after = tuple(tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (intent.id,)).fetchone())
        assert after == before
        assert reopened.get_activity(activity.id).remaining_retry_budget == 1
    finally:
        _stop_child(child)


def test_dead_preexecution_child_closes_spent_intent_before_new_attempt(tmp_path: Path) -> None:
    from run_state.ownership import (
        ControlStore, ProcessIdentity, acknowledge_child, recover_intent, reserve_launch,
    )

    store, ownership, activity = _owned_store(tmp_path, run_id="dead-preexec", budget=2)
    first = reserve_launch(store, activity.id, ownership.token)
    child = _owned_child(tmp_path)
    assert json.loads(_line(child))["pid"] == child.pid
    acknowledge_child(store, first.id, ownership.token, ProcessIdentity.from_pid(child.pid))
    _stop_child(child)
    reopened = ControlStore(store.db_path)
    recovered = recover_intent(reopened, first.id, ownership.token)
    assert recovered.state == "closed_dead"
    assert reopened.get_activity(activity.id).remaining_retry_budget == 1
    second = reserve_launch(reopened, activity.id, ownership.token)
    assert second.id != first.id
    assert reopened.get_activity(activity.id).remaining_retry_budget == 0


def test_unknown_child_liveness_requires_reconciliation_and_blocks_replacement(
    tmp_path: Path,
) -> None:
    from run_state.ownership import (
        ControlStore, OwnershipRefused, ProcessIdentity, acknowledge_child,
        recover_intent, reserve_launch,
    )

    store, ownership, activity = _owned_store(tmp_path, run_id="unknown-child")
    intent = reserve_launch(store, activity.id, ownership.token)
    child = _owned_child(tmp_path)
    try:
        assert json.loads(_line(child))["pid"] == child.pid
        acknowledge_child(store, intent.id, ownership.token, ProcessIdentity.from_pid(child.pid))
        uncertain = ControlStore(store.db_path, liveness_probe=lambda _identity: "UNKNOWN")
        decision = recover_intent(uncertain, intent.id, ownership.token)
        assert decision.state == "reconcile_required"
        assert decision.code == "OWNER_UNKNOWN"
        with pytest.raises(OwnershipRefused) as replacement:
            acknowledge_child(
                uncertain, intent.id, ownership.token, ProcessIdentity.current(),
            )
        assert replacement.value.code == "OWNER_UNKNOWN"
        assert uncertain.get_activity(activity.id).remaining_retry_budget == 1
    finally:
        _stop_child(child)


def test_revoked_owner_progress_changes_no_public_state(tmp_path: Path) -> None:
    from run_state.ownership import OwnershipRefused, release_owner

    store, ownership, activity = _owned_store(tmp_path, run_id="revoked-run")
    accepted = store.record_event_once(
        ownership.token, activity.id, "progress:accepted", {"status": "landed"},
    )
    assert accepted is not None
    assert any(
        event["event_type"] == "progress:accepted"
        and event["payload"] == {"status": "landed"}
        for event in store.enumerate_events(run_id=ownership.run_id)
    )
    before_activity = store.get_activity(activity.id)
    with store.transaction() as tx:
        release_owner(tx, ownership.token)
    before_stale_events = list(store.enumerate_events(run_id=ownership.run_id))
    with pytest.raises(OwnershipRefused) as stale:
        store.record_event_once(
            ownership.token, activity.id, "progress:revoked", {"status": "should-not-land"},
        )
    assert stale.value.code == "FENCE_REVOKED"
    assert list(store.enumerate_events(run_id=ownership.run_id)) == before_stale_events
    assert not any(
        event["event_type"] == "progress:revoked"
        for event in before_stale_events
    )
    assert store.get_activity(activity.id) == before_activity


def test_event_and_budget_idempotency_survive_restart(tmp_path: Path) -> None:
    from run_state.ownership import ControlStore, OwnershipRefused

    store, ownership, activity = _owned_store(tmp_path, run_id="idempotent-run", budget=3)
    first = store.record_event_once(
        ownership.token, activity.id, "event:key", {"value": 1},
    )
    reopened = ControlStore(store.db_path)
    assert reopened.record_event_once(
        ownership.token, activity.id, "event:key", {"value": 1},
    ) == first
    with pytest.raises(OwnershipRefused) as changed:
        reopened.record_event_once(
            ownership.token, activity.id, "event:key", {"value": 2},
        )
    assert changed.value.code == "IDEMPOTENCY_CONFLICT"

    debit = reopened.debit_budget(ownership.token, activity.id, 2, idempotency_key="debit:key")
    assert reopened.debit_budget(
        ownership.token, activity.id, 2, idempotency_key="debit:key",
    ) == debit
    assert reopened.get_activity(activity.id).remaining_retry_budget == 1
    with pytest.raises(OwnershipRefused) as changed_amount:
        reopened.debit_budget(
            ownership.token, activity.id, 1, idempotency_key="debit:key",
        )
    assert changed_amount.value.code == "IDEMPOTENCY_CONFLICT"
    assert reopened.get_activity(activity.id).remaining_retry_budget == 1

    reopened.transition_activity(
        ownership.token, activity.id, expected="pending", new="active",
    )
    reopened.transition_activity(
        ownership.token, activity.id, expected="active", new="succeeded",
        result={"locator": "fixture://first", "sha256": "a" * 64},
    )
    second = reopened.create_activity(
        ownership.token, kind="execute", input_digest=INPUT_V2,
        retry_budget=2, request_key="activity:idempotent-run:second",
    )
    independent = reopened.debit_budget(
        ownership.token, second.id, 1, idempotency_key="debit:key",
    )
    assert independent is not None
    assert reopened.get_activity(second.id).remaining_retry_budget == 1


@pytest.mark.parametrize("amount", [-1, 2**63])
def test_budget_rejects_negative_or_overflow_without_mutation(tmp_path: Path, amount: int) -> None:
    from run_state.ownership import OwnershipRefused

    store, ownership, activity = _owned_store(tmp_path, run_id=f"budget-{amount}", budget=2)
    before = store.get_activity(activity.id)
    with pytest.raises(OwnershipRefused) as invalid:
        store.debit_budget(ownership.token, activity.id, amount,
                           idempotency_key=f"invalid:{amount}")
    assert invalid.value.code == "INVALID_BUDGET_DEBIT"
    assert store.get_activity(activity.id) == before


def test_runtime_binding_is_immutable(tmp_path: Path) -> None:
    from run_state.ownership import OwnershipRefused

    store, ownership, activity = _owned_store(tmp_path, run_id="runtime-run")
    assert store.bind_runtime(ownership.token, activity.id, RUNTIME_V1).runtime_tuple_hash == RUNTIME_V1
    with pytest.raises(OwnershipRefused) as drift:
        store.bind_runtime(ownership.token, activity.id, RUNTIME_V2)
    assert drift.value.code == "RUNTIME_DRIFT"


def test_quota_pause_and_resume_never_replenish_activity_budget(tmp_path: Path) -> None:
    from run_state.ownership import ControlStore, OwnershipRefused

    store, ownership, activity = _owned_store(tmp_path, run_id="quota-run", budget=1)
    store.debit_budget(ownership.token, activity.id, 1, idempotency_key="quota:spent")
    store.transition_activity(
        ownership.token, activity.id, expected="pending", new="paused",
        reason="quota",
    )
    reopened = ControlStore(store.db_path)
    resumed = reopened.select_activity(
        ownership.token, kind="plan", input_digest=INPUT_V1,
        resume=True, revise=False,
    )
    assert resumed.id == activity.id
    assert reopened.get_activity(activity.id).remaining_retry_budget == 0
    with pytest.raises(OwnershipRefused) as exhausted:
        reopened.debit_budget(
            ownership.token, activity.id, 1, idempotency_key="quota:replacement",
        )
    assert exhausted.value.code == "BUDGET_EXHAUSTED"


def test_activity_resume_reuse_and_revision_are_distinct(tmp_path: Path) -> None:
    from run_state.ownership import OwnershipRefused

    store, ownership, first = _owned_store(tmp_path, run_id="activity-run", budget=2)
    store.transition_activity(
        ownership.token, first.id, expected="pending", new="active",
    )
    with pytest.raises(OwnershipRefused) as unfinished:
        store.create_activity(
            ownership.token, kind="execute", input_digest=INPUT_V2,
            retry_budget=2, request_key="activity:second",
        )
    assert unfinished.value.code == "RESUME_REQUIRED"

    store.transition_activity(
        ownership.token, first.id, expected="active", new="succeeded",
        result={"locator": "fixture://result", "sha256": "a" * 64},
    )
    reused = store.select_activity(
        ownership.token, kind="plan", input_digest=INPUT_V1, resume=False, revise=False,
    )
    assert reused.id == first.id and reused.reused_result is True
    revised = store.select_activity(
        ownership.token, kind="plan", input_digest=INPUT_V2, resume=False, revise=True,
    )
    assert revised.id != first.id and revised.revision == first.revision + 1


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "aborted"])
def test_terminal_activity_refuses_transition_debit_and_new_launch(
    tmp_path: Path, terminal: str,
) -> None:
    from run_state.ownership import OwnershipRefused, recover_intent, reserve_launch

    store, ownership, activity = _owned_store(
        tmp_path, run_id=f"terminal-{terminal}", budget=3,
    )
    existing_intent = reserve_launch(store, activity.id, ownership.token)
    result = (
        {"locator": f"fixture://terminal/{terminal}", "sha256": "9" * 64}
        if terminal == "succeeded" else None
    )
    terminal_activity = store.transition_activity(
        ownership.token, activity.id, expected="pending", new=terminal, result=result,
    )
    before_events = list(store.enumerate_events(run_id=ownership.run_id))
    before_budget = terminal_activity.remaining_retry_budget

    with pytest.raises(OwnershipRefused) as transition:
        store.transition_activity(
            ownership.token, activity.id, expected=terminal, new="active",
        )
    assert transition.value.code == "ACTIVITY_TERMINAL"
    with pytest.raises(OwnershipRefused) as debit:
        store.debit_budget(
            ownership.token, activity.id, 1,
            idempotency_key=f"terminal-debit:{terminal}",
        )
    assert debit.value.code == "ACTIVITY_TERMINAL"
    with pytest.raises(OwnershipRefused) as launch:
        reserve_launch(store, activity.id, ownership.token)
    assert launch.value.code == "ACTIVITY_TERMINAL"

    assert recover_intent(store, existing_intent.id, ownership.token).id == existing_intent.id
    assert store.get_activity(activity.id).state == terminal
    assert store.get_activity(activity.id).remaining_retry_budget == before_budget
    assert list(store.enumerate_events(run_id=ownership.run_id)) == before_events


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "aborted"])
def test_terminal_activity_refuses_release_and_preserves_original_permit_evidence(
    tmp_path: Path, terminal: str,
) -> None:
    """Primitive terminal fences; actual owner-crash completion is tested separately."""
    from run_state.ownership import (
        OwnershipRefused, ProcessIdentity, acknowledge_child, authorize_child,
        recover_intent, reserve_launch,
    )

    for stage in ("reserved", "acknowledged", "released_to_execute", "reconcile_required"):
        case_root = tmp_path / stage
        store, owner, activity = _owned_store(
            case_root, run_id=f"terminal-child-{stage}-{terminal}",
        )
        # The committed intent must precede every child creation.
        intent = reserve_launch(store, activity.id, owner.token)
        child = _owned_child(case_root / "child")
        try:
            assert json.loads(_line(child))["pid"] == child.pid
            identity = ProcessIdentity.from_pid(child.pid)
            acknowledgement = None
            permit = None
            if stage != "reserved":
                acknowledgement = acknowledge_child(store, intent.id, owner.token, identity)
            if stage in {"released_to_execute", "reconcile_required"}:
                permit = authorize_child(store, acknowledgement, owner.token)
            if stage == "reconcile_required":
                with store.transaction() as tx:
                    tx.execute("UPDATE authority_launch_intents SET state='reconcile_required' WHERE id=?", (intent.id,))
            with store.read_transaction() as tx:
                original = dict(tx.execute(
                    "SELECT * FROM authority_launch_intents WHERE id=?", (intent.id,),
                ).fetchone())
            # Retain actual observed transition evidence, never a fixture:// hash.
            evidence_path = case_root / "observed-intent.json"
            evidence_path.write_text(json.dumps(original, sort_keys=True))
            result = {"locator": str(evidence_path),
                      "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest()}
            expected_activity = "active" if stage in {"released_to_execute", "reconcile_required"} else "pending"
            assert store.get_activity(activity.id).state == expected_activity
            terminal_activity = store.transition_activity(
                owner.token, activity.id, expected=expected_activity, new=terminal,
                result=result if terminal == "succeeded" else None,
            )
            events = list(store.enumerate_events(run_id=owner.run_id))
            with pytest.raises(OwnershipRefused, match="ACTIVITY_TERMINAL"):
                reserve_launch(store, activity.id, owner.token)
            if acknowledgement is None:
                with pytest.raises(OwnershipRefused, match="ACTIVITY_TERMINAL"):
                    acknowledge_child(store, intent.id, owner.token, identity)
            else:
                with pytest.raises(OwnershipRefused, match="ACTIVITY_TERMINAL"):
                    authorize_child(store, acknowledgement, owner.token)
            recovered = recover_intent(store, intent.id, owner.token)
            assert recovered.id == intent.id
            assert recovered.state == "reconcile_required"
            with store.read_transaction() as tx:
                retained = dict(tx.execute(
                    "SELECT * FROM authority_launch_intents WHERE id=?", (intent.id,),
                ).fetchone())
            for key in ("generation", "child_host_id", "child_boot_id", "child_pid",
                        "child_start_token", "acknowledgement_id"):
                assert retained[key] == original[key]
            assert retained["permit_id"] is None
            if permit is not None:
                assert json.loads(evidence_path.read_text())["permit_id"] == permit.id
            assert store.get_activity(activity.id).state == terminal
            assert store.get_activity(activity.id).remaining_retry_budget == terminal_activity.remaining_retry_budget
            assert list(store.enumerate_events(run_id=owner.run_id)) == events
            assert hashlib.sha256(evidence_path.read_bytes()).hexdigest() == result["sha256"]
        finally:
            _stop_child(child)


def test_run_control_reads_are_repository_scoped_for_equal_run_ids(tmp_path: Path) -> None:
    from run_state.ownership import (
        ControlStore, ProcessIdentity, StartRequest, reserve_resources,
    )

    store = ControlStore(tmp_path / "authority" / "control.sqlite3")
    owners = []
    activities = []
    for suffix in ("a", "b"):
        owner = reserve_resources(store, StartRequest(
            "shared-run", str(tmp_path / f"workspace-{suffix}"), f"objective-{suffix}",
            ProcessIdentity.current(), repository_id=f"repository-{suffix}",
            planning_scope="scope",
        ))
        activity = store.create_activity(
            owner.token, kind="plan", input_digest=f"input-{suffix}", retry_budget=1,
            request_key=f"activity-{suffix}",
        )
        owners.append(owner)
        activities.append(activity)
    store.transition_activity(
        owners[0].token, activities[0].id, expected="pending", new="succeeded",
        result={"locator": "fixture://repo-a", "sha256": "a" * 64},
    )
    def scoped_control(repository_id: str):
        if "repository_id" in inspect.signature(store.get_run_control).parameters:
            return store.get_run_control("shared-run", repository_id=repository_id)
        return store.get_run_control("shared-run")

    assert scoped_control("repository-a").state == "complete"
    assert scoped_control("repository-b").state == "active"


def test_reservation_events_are_repository_scoped_for_equal_run_ids(tmp_path: Path) -> None:
    from run_state.ownership import (
        ControlStore, ProcessIdentity, StartRequest, release_owner, reserve_resources,
    )

    store = ControlStore(tmp_path / "authority" / "control.sqlite3")
    for suffix in ("a", "b"):
        owner = reserve_resources(store, StartRequest(
            "shared-run", str(tmp_path / f"events-workspace-{suffix}"),
            f"events-objective-{suffix}", ProcessIdentity.current(),
            repository_id=f"events-repository-{suffix}", planning_scope="events-scope",
        ))
        with store.transaction() as tx:
            release_owner(tx, owner.token)

    for suffix in ("a", "b"):
        repository_id = f"events-repository-{suffix}"
        if "repository_id" in inspect.signature(store.enumerate_events).parameters:
            events = list(store.enumerate_events(
                run_id="shared-run", repository_id=repository_id,
            ))
        else:
            events = list(store.enumerate_events(run_id="shared-run"))
        ownership_events = [
            event for event in events
            if event["event_type"] in {"resources_reserved", "resources_released"}
        ]
        assert [event["event_type"] for event in ownership_events] == [
            "resources_reserved", "resources_released",
        ]
        assert all(
            event["payload"] == {
                "repository_id": repository_id,
                "run_id": "shared-run",
            }
            for event in ownership_events
        )


def test_writable_store_read_apis_do_not_enter_schema_writer(tmp_path: Path) -> None:
    store, ownership, activity = _owned_store(tmp_path, run_id="pure-read-apis")
    before = (store.db_path.read_bytes(), store.db_path.stat().st_mtime_ns)

    def forbidden_writer():
        raise AssertionError("read API entered ControlStore.transaction")

    store.transaction = forbidden_writer
    assert store.get_activity(activity.id).id == activity.id
    if "repository_id" in inspect.signature(store.get_run_control).parameters:
        control = store.get_run_control(
            ownership.run_id, repository_id=ownership.token.repository_id,
        )
    else:
        control = store.get_run_control(ownership.run_id)
    assert control.run_id == ownership.run_id
    assert list(store.enumerate_decisions(gate="review_complete")) == []
    assert store.project_gates(
        GATE_INPUTS, run_id=ownership.run_id,
        repository_id=ownership.token.repository_id,
    )["review_complete"] is False
    if "repository_id" in inspect.signature(store.enumerate_events).parameters:
        events = list(store.enumerate_events(
            run_id=ownership.run_id, repository_id=ownership.token.repository_id,
        ))
    else:
        events = list(store.enumerate_events(run_id=ownership.run_id))
    assert events
    assert (store.db_path.read_bytes(), store.db_path.stat().st_mtime_ns) == before


def test_four_gate_projections_remain_independent_on_input_drift(tmp_path: Path) -> None:
    store, ownership, _activity = _owned_store(tmp_path, run_id="gates-run")
    inputs = {"candidate": "a" * 64, "runtime": "b" * 64, "config": "c" * 64,
              "policy": "d" * 64, "dependencies": "e" * 64}
    for gate in ("review_complete", "repair_authorized", "path_admitted", "rollout_ready"):
        store.record_decision(
            ownership.token, gate=gate, status=True, input_hashes=inputs,
            evidence={"locator": f"fixture://{gate}", "sha256": "f" * 64},
            provenance={"author": "independent-fixture"}, expires_at=DECISION_EXPIRY,
        )
    projection_scope = {
        "run_id": ownership.token.run_id,
        "repository_id": ownership.token.repository_id,
    }
    assert store.project_gates(inputs, **projection_scope) == {
        "review_complete": True, "repair_authorized": True,
        "path_admitted": True, "rollout_ready": True,
    }
    drifted = {**inputs, "policy": "0" * 64}
    projection = store.project_gates(drifted, **projection_scope)
    assert projection["review_complete"] is True
    assert projection["repair_authorized"] is True
    assert projection["path_admitted"] is False
    assert projection["rollout_ready"] is False


def test_gate_projection_cannot_borrow_identical_hashes_across_runs_or_repositories(
    tmp_path: Path,
) -> None:
    store, first, _activity = _owned_store(tmp_path, run_id="gate-owner-a")
    inputs = dict(GATE_INPUTS)
    store.record_decision(
        first.token, gate="review_complete", status=True, input_hashes=inputs,
        evidence={"locator": "fixture://run-a-review", "sha256": "c" * 64},
        provenance={"author": "independent-fixture"}, expires_at=DECISION_EXPIRY,
    )
    assert store.project_gates(
        inputs, run_id=first.token.run_id,
        repository_id=first.token.repository_id,
    )["review_complete"] is True
    assert store.project_gates(
        inputs, run_id="gate-owner-b",
        repository_id=first.token.repository_id,
    )["review_complete"] is False
    assert store.project_gates(
        inputs, run_id=first.token.run_id,
        repository_id="different-repository",
    )["review_complete"] is False


def test_empty_successful_gate_evidence_is_rejected(tmp_path: Path) -> None:
    from run_state.ownership import OwnershipRefused

    store, ownership, _activity = _owned_store(tmp_path, run_id="empty-gate")
    with pytest.raises(OwnershipRefused) as empty:
        store.record_decision(
            ownership.token, gate="review_complete", status=True,
            input_hashes=dict(GATE_INPUTS), evidence={},
            provenance={"author": "fixture"}, expires_at=DECISION_EXPIRY,
        )
    assert empty.value.code == "EVIDENCE_REQUIRED"


@pytest.mark.parametrize(
    ("gate", "required"),
    [
        ("review_complete", {"candidate", "runtime", "config"}),
        ("repair_authorized", {"candidate", "runtime", "config"}),
        ("path_admitted", {"candidate", "runtime", "config", "policy", "dependencies"}),
        ("rollout_ready", {"candidate", "runtime", "config", "policy", "dependencies"}),
    ],
)
def test_gate_decision_requires_every_typed_input_hash(
    tmp_path: Path, gate: str, required: set[str],
) -> None:
    from run_state.ownership import OwnershipRefused

    store, ownership, _activity = _owned_store(tmp_path, run_id=f"complete-hashes-{gate}")
    before = list(store.enumerate_decisions())
    invalid_inputs = [{}]
    invalid_inputs.extend(
        {key: value for key, value in GATE_INPUTS.items() if key != missing}
        for missing in required
    )
    invalid_inputs.extend(
        {**GATE_INPUTS, required_key: bad_value}
        for required_key in sorted(required)
        for bad_value in ("", None, 7)
    )
    for index, hashes in enumerate(invalid_inputs):
        with pytest.raises(OwnershipRefused) as invalid:
            _record_decision_contract(
                store,
                ownership.token, gate=gate, status=True, input_hashes=hashes,
                evidence={"locator": f"fixture://invalid/{index}", "sha256": "f" * 64},
                provenance={"author": "independent-fixture"},
                expires_at=DECISION_EXPIRY,
            )
        assert invalid.value.code == "INVALID_DECISION"
        assert list(store.enumerate_decisions()) == before


def test_gate_dependency_and_expiry_are_recursively_enforced(tmp_path: Path) -> None:
    from run_state.ownership import OwnershipRefused

    store, ownership, _activity = _owned_store(tmp_path, run_id="gate-dependency-expiry")
    observed_now = ["2030-01-01T00:00:00Z"]
    store._now = lambda: observed_now[0]
    review = _record_decision_contract(
        store,
        ownership.token, gate="review_complete", status=True,
        input_hashes=dict(GATE_INPUTS),
        evidence={"locator": "fixture://review", "sha256": "1" * 64},
        provenance={"author": "independent-fixture"},
        expires_at="2030-01-01T01:00:00Z",
    )
    repair = _record_decision_contract(
        store,
        ownership.token, gate="repair_authorized", status=True,
        input_hashes=dict(GATE_INPUTS),
        evidence={"locator": "fixture://repair", "sha256": "2" * 64},
        provenance={"author": "independent-fixture"}, dependencies=[review.id],
        expires_at="2030-01-01T02:00:00Z",
    )
    assert repair.dependencies == [review.id]
    assert store.project_gates(
        GATE_INPUTS, run_id=ownership.run_id,
        repository_id=ownership.token.repository_id,
    )["repair_authorized"] is True

    with pytest.raises(OwnershipRefused) as missing_dependency:
        _record_decision_contract(
            store,
            ownership.token, gate="path_admitted", status=True,
            input_hashes=dict(GATE_INPUTS),
            evidence={"locator": "fixture://path", "sha256": "3" * 64},
            provenance={"author": "independent-fixture"},
            dependencies=["missing-decision-id"],
            expires_at="2030-01-01T02:00:00Z",
        )
    assert missing_dependency.value.code == "INVALID_DECISION"

    observed_now[0] = "2030-01-01T01:00:01Z"
    projection = store.project_gates(
        GATE_INPUTS, run_id=ownership.run_id,
        repository_id=ownership.token.repository_id,
    )
    assert projection["review_complete"] is False
    assert projection["repair_authorized"] is False


def test_newer_negative_decision_invalidates_dependents_of_superseded_positive(
    tmp_path: Path,
) -> None:
    from run_state.ownership import ControlStore

    store, ownership, _activity = _owned_store(
        tmp_path, run_id="superseded-gate-dependency",
    )
    store._now = lambda: "2030-01-01T00:00:00Z"
    scope = {
        "run_id": ownership.run_id,
        "repository_id": ownership.token.repository_id,
    }
    review = _record_decision_contract(
        store,
        ownership.token, gate="review_complete", status=True,
        input_hashes=dict(GATE_INPUTS),
        evidence={"locator": "fixture://review/accepted", "sha256": "1" * 64},
        provenance={"author": "independent-fixture"},
        expires_at="2030-01-01T02:00:00Z",
    )
    repair = _record_decision_contract(
        store,
        ownership.token, gate="repair_authorized", status=True,
        input_hashes=dict(GATE_INPUTS),
        evidence={"locator": "fixture://repair/accepted", "sha256": "2" * 64},
        provenance={"author": "independent-fixture"}, dependencies=[review.id],
        expires_at="2030-01-01T02:00:00Z",
    )
    assert store.project_gates(GATE_INPUTS, **scope)["repair_authorized"] is True

    veto = _record_decision_contract(
        store,
        ownership.token, gate="review_complete", status=False,
        input_hashes=dict(GATE_INPUTS),
        evidence={"locator": "fixture://review/rejected", "sha256": "3" * 64},
        provenance={"author": "independent-fixture"},
        expires_at="2030-01-01T02:00:00Z",
    )
    expected_history = [review.id, repair.id, veto.id]
    assert [row.id for row in store.enumerate_decisions()] == expected_history

    writable = store.project_gates(GATE_INPUTS, **scope)
    assert writable["review_complete"] is False
    assert writable["repair_authorized"] is False
    read_store = ControlStore.open_read_only(store.db_path)
    readonly = read_store.project_gates(GATE_INPUTS, **scope)
    assert readonly == writable
    assert [row.id for row in read_store.enumerate_decisions()] == expected_history


@pytest.mark.parametrize(
    "expires_at",
    ["not-a-time", "2029-12-31T23:59:59Z", "2030-01-08T00:00:01Z"],
)
def test_grant_rejects_malformed_past_or_overlong_expiry_without_writes(
    tmp_path: Path, expires_at: str,
) -> None:
    from run_state.ownership import OwnershipRefused

    store, ownership, _activity = _owned_store(tmp_path, run_id="invalid-grant-expiry")
    store._now = lambda: "2030-01-01T00:00:00Z"
    before = (store.db_path.read_bytes(), store.db_path.stat().st_mtime_ns)
    with pytest.raises(OwnershipRefused) as invalid:
        store.create_grant(
            ownership.token, action="checkpoint", target="refs/heads/ffs/run",
            provenance={"issuer": "fixture"}, expires_at=expires_at,
            idempotency_key=f"invalid:{expires_at}",
        )
    assert invalid.value.code == "INVALID_GRANT"
    assert (store.db_path.read_bytes(), store.db_path.stat().st_mtime_ns) == before


def test_grant_consume_binds_action_target_expiry_and_idempotency(tmp_path: Path) -> None:
    from run_state.ownership import OwnershipRefused

    store, ownership, _activity = _owned_store(tmp_path, run_id="grant-binding")
    observed_now = ["2030-01-01T00:00:00Z"]
    store._now = lambda: observed_now[0]
    grant = store.create_grant(
        ownership.token, action="checkpoint", target="refs/heads/ffs/run",
        provenance={"issuer": "fixture"}, expires_at="2030-01-01T01:00:00Z",
        idempotency_key="grant:create",
    )
    for action, target in (
        ("push", "refs/heads/ffs/run"),
        ("checkpoint", "refs/heads/other"),
    ):
        before = (store.db_path.read_bytes(), store.db_path.stat().st_mtime_ns)
        with pytest.raises(OwnershipRefused) as mismatch:
            store.consume_grant(
                ownership.token, grant.id, expected_action=action,
                expected_target=target, idempotency_key="grant:consume",
            )
        assert mismatch.value.code == "GRANT_MISMATCH"
        assert (store.db_path.read_bytes(), store.db_path.stat().st_mtime_ns) == before

    consumed = store.consume_grant(
        ownership.token, grant.id, expected_action="checkpoint",
        expected_target="refs/heads/ffs/run", idempotency_key="grant:consume",
    )
    assert consumed.consumed is True
    assert store.consume_grant(
        ownership.token, grant.id, expected_action="checkpoint",
        expected_target="refs/heads/ffs/run", idempotency_key="grant:consume",
    ) == consumed

    second = store.create_grant(
        ownership.token, action="checkpoint", target="refs/heads/ffs/expired",
        provenance={"issuer": "fixture"}, expires_at="2030-01-01T01:00:00Z",
        idempotency_key="grant:expired:create",
    )
    observed_now[0] = "2030-01-01T01:00:01Z"
    before = (store.db_path.read_bytes(), store.db_path.stat().st_mtime_ns)
    with pytest.raises(OwnershipRefused) as expired:
        store.consume_grant(
            ownership.token, second.id, expected_action="checkpoint",
            expected_target="refs/heads/ffs/expired", idempotency_key="grant:expired:consume",
        )
    assert expired.value.code == "GRANT_EXPIRED"
    assert (store.db_path.read_bytes(), store.db_path.stat().st_mtime_ns) == before


@pytest.mark.parametrize(
    "category",
    ["event", "budget", "runtime", "activity", "decision", "grant", "launch"],
)
def test_stale_owner_cannot_mutate_any_control_category(
    tmp_path: Path, category: str,
) -> None:
    from run_state.ownership import OwnershipRefused, release_owner, reserve_launch

    store, ownership, activity = _owned_store(tmp_path, run_id=f"stale-{category}", budget=3)
    store._now = lambda: "2030-01-01T00:00:00Z"
    grant = store.create_grant(
        ownership.token, action="fixture-action", target="fixture-target",
        provenance={"issuer": "fixture"}, expires_at="2030-01-02T00:00:00Z",
        idempotency_key="grant:create",
    )
    with store.transaction() as tx:
        release_owner(tx, ownership.token)
    before_activity = store.get_activity(activity.id)
    before_events = list(store.enumerate_events(run_id=ownership.run_id))
    gate_scope = {
        "run_id": ownership.token.run_id,
        "repository_id": ownership.token.repository_id,
    }
    before_gates = store.project_gates({"candidate": "a" * 64}, **gate_scope)

    def mutate() -> object:
        if category == "event":
            return store.record_event_once(ownership.token, activity.id, "stale:event", {"x": 1})
        if category == "budget":
            return store.debit_budget(ownership.token, activity.id, 1, idempotency_key="stale:debit")
        if category == "runtime":
            return store.bind_runtime(ownership.token, activity.id, RUNTIME_V1)
        if category == "activity":
            return store.transition_activity(
                ownership.token, activity.id, expected="pending", new="active",
            )
        if category == "decision":
            return store.record_decision(
                ownership.token, gate="review_complete", status=True,
                input_hashes=dict(GATE_INPUTS),
                evidence={"locator": "fixture://stale", "sha256": "b" * 64},
                provenance={"author": "fixture"}, expires_at=DECISION_EXPIRY,
            )
        if category == "grant":
            return store.consume_grant(
                ownership.token, grant.id, expected_action="fixture-action",
                expected_target="fixture-target", idempotency_key="grant:consume",
            )
        return reserve_launch(store, activity.id, ownership.token)

    with pytest.raises(OwnershipRefused) as stale:
        mutate()
    assert stale.value.code == "FENCE_REVOKED"
    assert store.get_activity(activity.id) == before_activity
    assert list(store.enumerate_events(run_id=ownership.run_id)) == before_events
    assert store.project_gates({"candidate": "a" * 64}, **gate_scope) == before_gates


def test_decisions_are_append_only_and_preserve_dependency_history(tmp_path: Path) -> None:
    store, ownership, _activity = _owned_store(tmp_path, run_id="decision-history")
    first = store.record_decision(
        ownership.token, gate="review_complete", status=True,
        input_hashes=dict(GATE_INPUTS),
        evidence={"locator": "fixture://review-a", "sha256": "b" * 64},
        provenance={"author": "reviewer-a"}, expires_at=DECISION_EXPIRY,
    )
    second = store.record_decision(
        ownership.token, gate="review_complete", status=False,
        input_hashes=dict(GATE_INPUTS),
        evidence={"locator": "fixture://review-b", "sha256": "d" * 64},
        provenance={"author": "reviewer-b"}, dependencies=[first.id],
        expires_at=DECISION_EXPIRY,
    )
    decisions = list(store.enumerate_decisions(gate="review_complete"))
    assert [row.id for row in decisions] == [first.id, second.id]
    assert decisions[0].input_hashes == GATE_INPUTS
    assert decisions[1].dependencies == [first.id]


def test_read_only_context_facades_cannot_borrow_legacy_claim_or_grant_authority(
    tmp_path: Path,
) -> None:
    import gates
    from run_context import RunContext
    from run_state.ownership import ControlStore, OwnershipRefused
    from scripts.coord.coord import project_control_context as project_coord_context
    from gates import project_control_context as project_gate_context

    store, ownership, activity = _owned_store(tmp_path, run_id="facade-run")
    inputs = dict(GATE_INPUTS)
    store.record_decision(
        ownership.token, gate="review_complete", status=True, input_hashes=inputs,
        evidence={"locator": "fixture://facade-review", "sha256": "c" * 64},
        provenance={"author": "independent-fixture"}, expires_at=DECISION_EXPIRY,
    )
    context = RunContext(
        repository_id=ownership.token.repository_id,
        run_id=ownership.run_id,
        activity_id=activity.id,
        workspace=ownership.token.workspace,
        evidence_root=str(tmp_path / "evidence" / ownership.run_id),
        generation=ownership.generation,
        workspace_state="ready",
        ready=True,
        selected_input_manifest_hash="d" * 64,
    )
    read_store = ControlStore.open_read_only(store.db_path)

    gates_store = tmp_path / "legacy-gates.json"
    action = "push:origin/main"
    assert gates.grant_actions(gates_store, "legacy-run", [action]) is True
    assert gates.check_grant(gates_store, "legacy-run", action) is True

    anchor = _owned_child(tmp_path / "coord-anchor")
    assert json.loads(_line(anchor))["pid"] == anchor.pid
    coord_root = tmp_path / "legacy-coord"
    coord_env = _env(tmp_path / "coord-claim")
    coord_env.update(
        FFS_COORD_STORE=str(coord_root),
        FFS_RUN_ID="legacy-run",
        FFS_COORD_ANCHOR_PID=str(anchor.pid),
    )
    claim = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "coord" / "coord.py"),
         "claim", "spec-014"],
        cwd=ROOT, env=coord_env, capture_output=True, text=True, timeout=10,
    )
    assert claim.returncode == 0, claim.stderr
    assert f"CLAIM-OK generation={ownership.generation}" in claim.stdout
    registry = coord_root / "registry.json"
    legacy_before = {
        "coord": (registry.read_bytes(), registry.stat().st_mtime_ns),
        "gates": (gates_store.read_bytes(), gates_store.stat().st_mtime_ns),
    }

    with pytest.raises(ValueError, match="READ_ONLY_STORE_REQUIRED"):
        project_coord_context(context, store)
    with pytest.raises(ValueError, match="READ_ONLY_STORE_REQUIRED"):
        project_gate_context(context, store, inputs)
    with pytest.raises(TypeError):
        project_coord_context(context.as_payload(), read_store)
    with pytest.raises(TypeError):
        project_gate_context(context.as_payload(), read_store, inputs)

    coord_projection = project_coord_context(context, read_store)
    gate_projection = project_gate_context(context, read_store, inputs)
    base_keys = {
        "activity_id", "attempt_id", "authority", "evidence_root", "generation",
        "repository_id", "run_id", "workspace",
    }
    assert set(coord_projection) == base_keys
    assert set(gate_projection) == base_keys | {"gates", "input_hashes"}
    assert coord_projection["authority"] == gate_projection["authority"] == "read_only"
    assert coord_projection["generation"] == gate_projection["generation"] == (
        f"control:{context.generation}"
    )
    assert gate_projection["input_hashes"] == inputs
    assert gate_projection["gates"] == {
        "review_complete": True,
        "repair_authorized": False,
        "path_admitted": False,
        "rollout_ready": False,
    }
    serialized = json.dumps([coord_projection, gate_projection], sort_keys=True).lower()
    for capability_name in (
        "nonce", "token", "session_uuid", "claim_generation", "grant_id",
    ):
        assert capability_name not in serialized
    assert {
        "coord": (registry.read_bytes(), registry.stat().st_mtime_ns),
        "gates": (gates_store.read_bytes(), gates_store.stat().st_mtime_ns),
    } == legacy_before
    assert gates.check_grant(gates_store, context.run_id, action) is False

    with pytest.raises(OwnershipRefused) as inert:
        store.create_grant(
            coord_projection, action="fixture-action", target="fixture-target",
            provenance={"issuer": "borrowed-projection"},
            expires_at="2099-01-01T00:00:00Z", idempotency_key="borrowed:grant",
        )
    assert inert.value.code == "FENCE_REVOKED"

    borrower_env = _env(tmp_path / "coord-borrower")
    borrower_env.update(FFS_COORD_STORE=str(coord_root), FFS_RUN_ID="borrower-run")
    borrowed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "coord" / "coord.py"),
         "claim-check", "spec-014", "--generation",
         str(coord_projection["generation"])],
        cwd=ROOT, env=borrower_env, capture_output=True, text=True, timeout=10,
    )
    assert borrowed.returncode != 0
    assert "CLAIM-OK" not in borrowed.stdout
    assert registry.read_bytes() == legacy_before["coord"][0]


def test_legacy_sqlite_projection_hash_binds_concurrent_same_inode_snapshot(
    tmp_path: Path,
) -> None:
    from run_state.state import RunStore

    legacy = tmp_path / "legacy-runs.db"
    old = RunStore(legacy)
    original_id = old.create_run(skill="fix", objective="original snapshot")
    before_identity = (legacy.stat().st_dev, legacy.stat().st_ino)
    before_hash = hashlib.sha256(legacy.read_bytes()).hexdigest()
    ready = tmp_path / "legacy-reader-ready"
    start = tmp_path / "legacy-reader-start"
    reader_program = textwrap.dedent(
        """
        import json, sys, time
        from pathlib import Path
        import run_state.state as state

        source, ready, start = map(Path, sys.argv[1:])
        real_hash = state._hash_regular_file
        armed = True
        def hash_with_observable_boundary(path):
            global armed
            result = real_hash(path)
            if armed:
                armed = False
                ready.touch()
                while not start.exists():
                    time.sleep(0.001)
            return result
        state._hash_regular_file = hash_with_observable_boundary
        try:
            rows = list(state.ControlStore.read_legacy_run_store(source))
            print(json.dumps({
                "status": "mapped",
                "ids": [row.source_run_id for row in rows],
                "hashes": sorted({row.source_sha256 for row in rows}),
            }, sort_keys=True), flush=True)
        except state.ControlStoreRefused as error:
            print(json.dumps({"status": "refused", "code": error.code}), flush=True)
        """
    )
    reader = subprocess.Popen(
        [sys.executable, "-c", reader_program, str(legacy), str(ready), str(start)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=_env(tmp_path / "legacy-reader-home"),
    )
    _OWNED_CHILDREN.add(reader)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and reader.poll() is None:
            assert time.monotonic() < deadline, "legacy reader barrier deadline exceeded"
            time.sleep(0.005)
        assert ready.exists()
        concurrent_id = old.create_run(skill="fix", objective="concurrent writer")
        assert reader.poll() is None
        committed = (legacy.read_bytes(), legacy.stat().st_mtime_ns)
        after_hash = hashlib.sha256(committed[0]).hexdigest()
        assert (legacy.stat().st_dev, legacy.stat().st_ino) == before_identity
        start.touch()

        observed = json.loads(_line(reader, timeout=20))
        assert reader.wait(timeout=10) == 0
        assert (legacy.read_bytes(), legacy.stat().st_mtime_ns) == committed
        if observed["status"] == "refused":
            assert observed == {"status": "refused", "code": "LEGACY_SOURCE_CHANGED"}
        else:
            ids = set(observed["ids"])
            assert original_id in ids
            if concurrent_id in ids:
                assert observed["hashes"] == [after_hash]
            else:
                assert observed["hashes"] == [before_hash]
    finally:
        _stop_child(reader)
