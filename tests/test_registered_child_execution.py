"""Registered process children overlap; this is not GSD host qualification."""
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import time

import pytest

from run_state.cli import _cmd_fixture_start
from run_state.supervisor import DispatchRequest, Supervisor, SupervisorRefused
from run_state.workspace import begin_child_workspace_preparation, prepare_workspace
from test_managed_preparation import _args
from test_m4_upstream_context_acceptance import _repository
from test_run_context_acceptance import INHERITED_CONTEXT_KEYS


def git(path, *args):
    return subprocess.check_output(["git", *args], cwd=path, text=True).strip()


def test_registered_children_overlap_on_exact_ahead_base(tmp_path, monkeypatch):
    primary = _repository(tmp_path)
    default = git(primary, "rev-parse", "HEAD")
    git(primary, "checkout", "-qb", "ahead")
    (primary / "src/input.txt").write_text("ahead\n")
    git(primary, "commit", "-am", "ahead fixture", "-q")
    ahead = git(primary, "rev-parse", "HEAD")
    assert ahead != default
    monkeypatch.chdir(primary)
    for key in INHERITED_CONTEXT_KEYS:
        monkeypatch.delenv(key, raising=False)

    def execute(store, token, context):
        store.configure_run_limits(token, dispatch_limit=3, token_limit=100, worker_capacity=2)
        store.bind_runtime(token, context.activity_id, "b" * 64)
        supervisor = Supervisor(store, token, evidence_root=tmp_path / "authority/evidence")
        with store.read_transaction() as tx:
            parent_before = dict(tx.execute("SELECT * FROM context_runs").fetchone())
        handles = []
        paths = []
        requests = []
        for index in range(2):
            pending = begin_child_workspace_preparation(
                store, token, parent_activity_id=context.activity_id,
                request_key=f"child-{index}", role="worker", base_commit=ahead,
                selected_input_manifest={"entries": []}, repository_path=primary,
            )
            ready = prepare_workspace(store, token, pending)
            assert ready.ready and git(ready.path, "rev-parse", "HEAD") == ahead
            child = store.create_child_activity(
                token, parent_activity_id=context.activity_id, role="worker",
                request_key=f"activity-{index}", candidate_hash=ready.input_digest,
                contract_hash="d" * 64, runtime_identity="b" * 64,
                workspace_binding=str(ready.path), workspace_preparation_id=ready.id,
            )
            command = (
                sys.executable, "-c",
                "import time; from pathlib import Path; print(time.time(),flush=True); "
                f"Path('src/input.txt').write_text('worker-{index}'); "
                "\nend=time.monotonic()+30\n"
                "while not Path('release-fixture').exists():\n"
                " if time.monotonic()>end: raise TimeoutError('fixture release missing')\n"
                " time.sleep(.01)\n"
                "print(time.time(),flush=True)",
            )
            request = DispatchRequest(
                child.id, f"launch-{index}", command, str(ready.path), ahead, "b" * 64,
                contract_hash="d" * 64,
            )
            with pytest.raises(SupervisorRefused):
                supervisor.launch(replace(request, workspace=context.workspace))
            requests.append(request)
            paths.append(ready.path)
        # Allocate the whole dependency wave before launching its executors.
        # Git preparation latency is not part of the executor overlap window.
        handles = [supervisor.launch(request) for request in requests]
        deadline = time.monotonic() + 15
        while not all(handle.stdout_path.read_text().strip() for handle in handles):
            assert all(handle.process.poll() is None for handle in handles)
            assert time.monotonic() < deadline, "both fixture executors must start before release"
            time.sleep(.01)
        for path in paths:
            (path / "release-fixture").touch()
        intervals = []
        for handle in handles:
            assert supervisor.finish(handle, timeout=10, token_usage=0)["returncode"] == 0
            intervals.append([float(x) for x in handle.stdout_path.read_text().splitlines()])
        assert max(x[0] for x in intervals) < min(x[1] for x in intervals)
        assert (paths[0] / "src/input.txt").read_text() == "worker-0"
        assert (paths[1] / "src/input.txt").read_text() == "worker-1"
        assert (Path(context.workspace) / "src/input.txt").read_text() == "ahead\n"
        assert (primary / "src/input.txt").read_text() == "ahead\n"
        with store.read_transaction() as tx:
            parent_after = dict(tx.execute("SELECT * FROM context_runs").fetchone())
        assert parent_after == parent_before
        return 0

    assert _cmd_fixture_start(_args(tmp_path / "authority", activity="execute"), on_ready=execute) == 0
