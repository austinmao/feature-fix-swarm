"""Independent recovery evidence guards against real completed monitor output."""
from dataclasses import replace
from pathlib import Path
import os
import signal
import sys
import time

import pytest

from run_state.ownership import OwnershipRefused
from process_identity import DEAD, LIVE, probe_identity
from run_state.supervisor import SupervisorRefused
from test_host_review_dispatch_material import _authority_snapshot, _material
from test_supervised_process import setup_owner


@pytest.mark.parametrize("change", ["stream_bytes", "stream_symlink", "stream_hardlink", "result_symlink"])
def test_monitored_evidence_tamper_cannot_settle_allowance(tmp_path, change):
    supervisor, store, request = setup_owner(tmp_path)
    handle = supervisor.launch(replace(request, monitor_result=True, token_reservation=7))
    handle.process.wait(timeout=15)
    result_path = handle.stdout_path.parent / "result.json"
    assert result_path.is_file()
    target = result_path if change == "result_symlink" else handle.stdout_path
    if change == "stream_bytes":
        target.write_bytes(b"replaced output after monitor publication\n")
    else:
        retained = tmp_path / "retained-output"
        if change == "stream_hardlink":
            os.link(target, retained)
        else:
            target.rename(retained)
            target.symlink_to(retained)
    before = _authority_snapshot(store)
    with pytest.raises((SupervisorRefused, OwnershipRefused)):
        supervisor.finish(handle, timeout=5, token_usage=0)
    assert _authority_snapshot(store) == before


def test_lost_monitor_cannot_refund_or_relaunch_an_unobserved_result(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)
    command = (sys.executable, "-c", "from pathlib import Path; import os,time; Path('native-ready').write_text(str(os.getpid())); time.sleep(60)")
    request = replace(request, command=command, monitor_result=True, token_reservation=7)
    handle = supervisor.launch(request)
    ready = Path(request.workspace) / "native-ready"
    try:
        deadline = time.monotonic() + 15
        while not ready.exists():
            assert time.monotonic() < deadline
            time.sleep(.02)
        assert int(ready.read_text()) == handle.identity.pid
        assert probe_identity(handle.identity) == LIVE
        handle.process.kill()
        handle.process.wait(timeout=5)
        before = _authority_snapshot(store)
        with pytest.raises((SupervisorRefused, OwnershipRefused)):
            supervisor.finish(handle, timeout=5, token_usage=0)
        with pytest.raises(SupervisorRefused, match="INTENT_RECONCILIATION_REQUIRED"):
            supervisor.launch(request)
        assert _authority_snapshot(store) == before
        assert not (handle.stdout_path.parent / "result.json").exists()
    finally:
        if handle.process.poll() is None:
            handle.process.kill()
            handle.process.wait(timeout=5)
        if probe_identity(handle.identity) == LIVE:
            os.kill(handle.identity.pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while probe_identity(handle.identity) != DEAD and time.monotonic() < deadline:
            time.sleep(.02)


def test_monitored_transport_cannot_silently_discard_closed_host_material(tmp_path, monkeypatch):
    supervisor, store, request = setup_owner(tmp_path)
    before = _authority_snapshot(store)
    from run_state import supervisor as module
    original = module.subprocess.Popen

    def guarded_spawn(argv, *args, **kwargs):
        if argv[:3] == [sys.executable, "-m", "run_state.supervisor"]:
            raise AssertionError("unsupported host monitor transport reached process spawn")
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(module.subprocess, "Popen", guarded_spawn)
    with pytest.raises((SupervisorRefused, OwnershipRefused)):
        supervisor.launch(replace(request, monitor_result=True, host_material=_material()))
    assert _authority_snapshot(store) == before
