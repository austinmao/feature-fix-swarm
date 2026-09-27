"""The actual permit delivers only request routing metadata to its child."""
from dataclasses import asdict, replace
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import time

import pytest

from process_identity import ProcessIdentity
from test_supervised_process import setup_owner
from run_state.supervisor import Supervisor
from run_state.worker_channel import WorkerChannelRefused, WorkerChannelServer, file_request


def test_admitted_child_uses_kernel_authenticated_request_channel(tmp_path):
    original, store, request = setup_owner(tmp_path)
    # Darwin Unix sockets have a short pathname limit; keep the private
    # endpoint separate from pytest's descriptive workspace path.
    with tempfile.TemporaryDirectory(prefix="ffs-ipc-", dir="/tmp") as directory:
        endpoint = Path(directory).resolve() / "worker.sock"
        server = WorkerChannelServer(store, original.token, endpoint).start()
        supervisor = Supervisor(store, original.token, evidence_root=original.evidence_root,
                                worker_channel=server)
        command = (sys.executable, "-c", """
import json,os
from run_state.worker_channel import request
scope=json.loads(os.environ['FFS_WORKER_SCOPE'])
assert set(scope)=={'repository_id','run_id','activity_id','intent_id','generation','supervisor_identity'}
assert 'RUN_STATE_DB' not in os.environ
results=[request(os.environ['FFS_WORKER_ENDPOINT'],scope,request_key='progress-1',
 operation='progress',body={'sequence':1,'message':'actual admitted child'}) for _ in range(2)]
print(json.dumps(results))
""")
        try:
            handle = supervisor.launch(replace(request, command=command))
            result = supervisor.finish(handle, timeout=10, token_usage=0)
            assert result["returncode"] == 0, handle.stderr_path.read_text()
            first, replay = json.loads(handle.stdout_path.read_text())
            assert first["ok"] and replay["ok"]
            assert first["replayed"] is False and replay["replayed"] is True
            assert first["event_id"] == replay["event_id"]
        finally:
            server.close()


def test_workspace_file_transport_enters_same_fenced_request_handler(tmp_path):
    original, store, request = setup_owner(tmp_path)
    with tempfile.TemporaryDirectory(prefix="ffs-ipc-", dir="/tmp") as directory:
        server = WorkerChannelServer(
            store, original.token, Path(directory).resolve() / "worker.sock",
        ).start()
        supervisor = Supervisor(
            store, original.token, evidence_root=original.evidence_root,
            worker_channel=server,
        )
        handle = supervisor.launch(replace(
            request, command=(sys.executable, "-c", "import time; time.sleep(20)"),
        ))
        try:
            channel = server.register_file_transport(handle.intent_id)
            scope = server._primary_bindings[handle.intent_id].scope()
            first = file_request(
                channel["root"], channel["capability"], scope,
                request_key="file-progress", operation="progress",
                body={"sequence": 1, "message": "sandboxed bridge"}, timeout=5,
            )
            replay = file_request(
                channel["root"], channel["capability"], scope,
                request_key="file-progress", operation="progress",
                body={"sequence": 1, "message": "sandboxed bridge"}, timeout=5,
            )
            assert first["ok"] and replay["ok"]
            assert first["replayed"] is False and replay["replayed"] is True
            with pytest.raises(WorkerChannelRefused, match="IPC_FILE_CAPABILITY_REFUSED"):
                response = file_request(
                    channel["root"], "x" * 43, scope,
                    request_key="forged", operation="progress",
                    body={"sequence": 2, "message": "forged"}, timeout=5,
                )
                if response.get("ok") is not True:
                    raise WorkerChannelRefused(response["code"])
        finally:
            handle.process.terminate()
            handle.process.wait(timeout=5)
            supervisor.finish(handle, timeout=5, token_usage=0)
            server.close()


def test_file_request_stops_waiting_once_the_supervisor_is_dead(tmp_path):
    # F37b: a detached wave adapter must not wait forever on a reply file
    # that a dead supervisor can never write.
    root = tmp_path.resolve() / "channel"
    for directory in (root, root / "requests", root / "responses"):
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    supervisor = ProcessIdentity.from_pid(sleeper.pid)
    sleeper.kill()
    sleeper.wait(timeout=5)
    scope = {"repository_id": "repo", "run_id": "run", "activity_id": "activity",
             "intent_id": "intent", "generation": 1, "supervisor_identity": asdict(supervisor)}
    started = time.monotonic()
    with pytest.raises(WorkerChannelRefused, match="IPC_SUPERVISOR_GONE"):
        file_request(root, "x" * 43, scope, request_key="orphaned", operation="progress",
                     body={"sequence": 1, "message": "waiting"}, timeout=10)
    assert time.monotonic() - started < 5
