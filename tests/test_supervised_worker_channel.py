"""The actual permit delivers only request routing metadata to its child."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile

import pytest

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
