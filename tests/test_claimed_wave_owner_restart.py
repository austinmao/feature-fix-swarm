"""A successor settles a claimed wave after SIGKILL of its actual owner."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from process_identity import DEAD, LIVE, ProcessIdentity, probe_identity
from run_state.ownership import ControlStore, StartRequest, reserve_resources
from run_state.supervisor import Supervisor
from run_state.wave_consumer import WaveConsumer
from run_state.worker_channel import WorkerChannelServer


def test_successor_settles_claimed_wave_without_changing_issuing_bindings(tmp_path):
    metadata = tmp_path / "metadata.json"
    release = tmp_path / "release"
    script = tmp_path / "native.py"
    script.write_text("from pathlib import Path\nimport sys,time\n"
                      "while not Path(sys.argv[1]).exists(): time.sleep(.01)\n"
                      "Path('result-0.txt').write_text('once')\n")
    program = r'''
from pathlib import Path
from dataclasses import asdict
import json,sys,time,pytest
from test_wave_consumer import wave_fixture
root=Path(sys.argv[1]);root.mkdir()
with wave_fixture(root, pytest.MonkeyPatch(), plans=1, policy_tier="small",
                  commands=[(sys.executable,sys.argv[3],sys.argv[4])]) as f:
    def stop(handle, **kwargs):
        f.store.begin_policy_work(f.supervisor.token, kind="recovery")
        with f.store.read_transaction() as tx:
            bindings=[dict(row) for row in tx.execute("SELECT id,generation,acknowledgement_id,permit_id,child_pid FROM authority_launch_intents ORDER BY id")]
            debits=tx.execute("SELECT dispatch_used FROM authority_run_limits").fetchone()[0]
        Path(sys.argv[2]).write_text(json.dumps({'db':str(f.store.db_path),
          'event':f.event,'evidence_root':str(f.supervisor.evidence_root),'parent':str(f.parent),
          'bindings':bindings,'debits':debits,'generation':f.supervisor.token.generation,
          'processes':[{'native':asdict(h.identity),'monitor':asdict(h.monitor_identity)}
                       for h in f.supervisor._handles.values()],
          'child_monitor':asdict(handle.monitor_identity)}))
        while True:time.sleep(1)
    f.supervisor.finish=stop
    f.consumer(f.event)
'''
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "lib") + os.pathsep + str(Path(__file__).parent)
    owner = subprocess.Popen([sys.executable, "-c", program, str(tmp_path / "owner"),
                              str(metadata), str(script), str(release)], env=environment)
    payload = None
    try:
        deadline = time.monotonic() + 40
        while not metadata.exists():
            assert owner.poll() is None and time.monotonic() < deadline
            time.sleep(.02)
        payload = json.loads(metadata.read_text())
        owner.kill()
        owner.wait(timeout=10)
        store = ControlStore(Path(payload["db"]))
        with store.read_transaction() as tx:
            run = tx.execute("SELECT * FROM context_runs").fetchone()
        token = reserve_resources(store, StartRequest(run["run_id"], run["workspace"],
            run["objective_digest"], ProcessIdentity.current(), repository_id=run["repository_id"],
            planning_scope=run["planning_scope"])).token
        assert token.generation > payload["generation"]
        release.write_text("go")
        monitor = ProcessIdentity(**payload["child_monitor"])
        deadline = time.monotonic() + 20
        while probe_identity(monitor) != DEAD:
            assert time.monotonic() < deadline
            time.sleep(.02)
        with tempfile.TemporaryDirectory(prefix="ffs-replay-") as socket_directory:
            channel = WorkerChannelServer(store, token, Path(socket_directory).resolve() / "ipc")
            supervisor = Supervisor(store, token, evidence_root=Path(payload["evidence_root"]), worker_channel=channel)
            finish = supervisor.finish
            supervisor.finish = lambda handle, **kw: finish(handle, token_usage=0, **kw)

            def forbidden(_context):
                raise AssertionError("successor attempted to prepare another child")

            try:
                result = WaveConsumer(supervisor, forbidden, finish_timeout=15)(payload["event"])
            finally:
                channel.close()
        assert result["results"][0]["status"] == "complete"
        assert (Path(payload["parent"]) / "result-0.txt").read_text() == "once"
        budget = store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id)
        assert budget.tier == "small" and budget.launch_charged == payload["debits"] == 2
        assert budget.active_ns > 0 and not budget.clock_uncertain
        with store.read_transaction() as tx:
            bindings = [dict(row) for row in tx.execute(
                "SELECT id,generation,acknowledgement_id,permit_id,child_pid FROM authority_launch_intents ORDER BY id")]
            assert bindings == payload["bindings"]
            assert tx.execute("SELECT dispatch_used FROM authority_run_limits").fetchone()[0] == payload["debits"]
            assert tx.execute("SELECT COUNT(*) FROM authority_policy_work_intervals WHERE state='active' AND generation<>?",
                              (token.generation,)).fetchone()[0] == 0
            assert tx.execute("SELECT COUNT(*) FROM control_events WHERE event_type='policy_local_work_reconciled'").fetchone()[0] == 1
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
        if payload is not None:
            for process in payload["processes"]:
                for kind in ("native", "monitor"):
                    identity = ProcessIdentity(**process[kind])
                    if probe_identity(identity) == LIVE:
                        try:
                            os.kill(identity.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
