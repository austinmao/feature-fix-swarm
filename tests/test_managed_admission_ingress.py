"""Production ingress queues before per-run effects and releases on failure."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time

import pytest

from run_state.managed import prepare_managed_run
from run_state.state import ControlStore
from run_state.tests.admission_fixture import queue as fixture_queue, release as fixture_release
from test_managed_production_ingress import _setup, ROOT, _tree
from test_run_context_acceptance import INHERITED_CONTEXT_KEYS

# DEFERRED (spec-014 Release B ledger): prepare_managed_run does not yet take a
# global admission ticket at production ingress, and gsd-run.sh managed ingress
# stays opt-in refusal only (operator ruling 2). Strict, so wiring it fails CI.
deferred_ingress_admission = pytest.mark.xfail(
    reason="DEFERRED: global admission at managed production ingress (spec-014 Release B ledger)",
    raises=(AssertionError, IndexError), strict=True,
)


@deferred_ingress_admission
def test_waiting_production_run_has_no_state_or_workspace_effects(tmp_path, monkeypatch):
    primary, authority, _, env = _setup(tmp_path)
    global_root = tmp_path / "global"
    monkeypatch.setenv("FFS_MANAGED_ADMISSION_ROOT", str(global_root))
    env["FFS_MANAGED_ADMISSION_ROOT"] = str(global_root)
    queue = fixture_queue()
    tickets = [queue.acquire(state_root=tmp_path / str(index), run_id=str(index), timeout=10) for index in range(2)]
    before = _tree(authority)
    checkout_before = _tree(primary)
    child = subprocess.Popen(
        ["bash", str(ROOT / "scripts/gsd/gsd-run.sh"), "/gsd-plan-phase", "1"],
        cwd=primary, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 20
        while len(queue.snapshot()) < 3 and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert child.poll() is None
        assert queue.snapshot()[-1]["status"] == "waiting"
        assert _tree(authority) == before
        assert _tree(primary) == checkout_before
        assert not (primary.parent / ".ffs-workspaces").exists()
        fixture_release(queue, tickets.pop(0))
        stdout, stderr = child.communicate(timeout=60)
        assert child.returncode == 78, (stdout, stderr)
        assert json.loads(stdout)["code"] == "HOST_CAPABILITY_UNQUALIFIED"
        assert queue.snapshot()[-1]["status"] == "released"
        store = ControlStore(authority / "control.sqlite3")
        with store.read_transaction() as connection:
            assert connection.execute("SELECT worker_capacity FROM authority_run_limits").fetchone()[0] == 3
            assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='managed_admissions'").fetchone()
    finally:
        # Kill the whole session: grandchildren hold the pipes open past a bash-only kill.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.communicate(timeout=10)
        for ticket in tickets:
            fixture_release(queue, ticket)


@deferred_ingress_admission
def test_callback_exception_releases_global_admission_after_ownership(tmp_path, monkeypatch):
    primary, authority, _, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    for key in INHERITED_CONTEXT_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FFS_MANAGED_ADMISSION_ROOT", str(tmp_path / "global"))
    queue = fixture_queue()

    def fail(store, token, context):
        assert queue.snapshot()[-1]["status"] == "active"
        with store.read_transaction() as connection:
            assert connection.execute("SELECT worker_capacity FROM authority_run_limits").fetchone()[0] == 3
        raise RuntimeError("managed callback failed")

    with pytest.raises(RuntimeError, match="managed callback failed"):
        prepare_managed_run(
            objective="failed callback", state_root=authority,
            selection_manifest=env["FFS_SELECTION_MANIFEST"],
            upstream_runtime_manifest=env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
            upstream_runtime_sha256=env["FFS_UPSTREAM_RUNTIME_SHA256"],
            request_key="exception", command=("/gsd-plan-phase", "1"),
            activity="plan", scope="1", dispatch_limit=3, token_limit=100,
            on_ready=fail, run_id="admission-failure",
        )
    assert queue.snapshot()[-1]["status"] == "released"
    store = ControlStore(authority / "control.sqlite3")
    with store.read_transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM control_reservations WHERE held=1").fetchone()[0] == 0


def test_suite_never_uses_the_per_user_admission_root(tmp_path_factory):
    from run_state.managed_admission import global_admission_root
    assert global_admission_root().is_relative_to(tmp_path_factory.getbasetemp().resolve())
