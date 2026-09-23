"""Verified process-session containment preserves unrelated processes."""
from dataclasses import replace
import subprocess
import sys
import time

from process_identity import ProcessIdentity, probe_identity, LIVE, DEAD
from run_state.containment import contain_session


def test_containment_targets_only_verified_session_and_preserves_unknown_identity():
    target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        identity = ProcessIdentity.from_pid(target.pid)
        refused = contain_session(replace(identity, start_token="not-this-incarnation"), grace_seconds=.01)
        assert refused["status"] == "uncertain" and probe_identity(identity) == LIVE
        report = contain_session(identity, grace_seconds=.1)
        target.wait(timeout=5)
        assert report["status"] in {"terminated", "reaping_pending"}
        assert report["members"]
        assert unrelated.poll() is None
    finally:
        for process in (target, unrelated):
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)


def test_containment_escalates_verified_stubborn_descendant(tmp_path):
    marker = tmp_path / "child"
    program = (
        "import signal,subprocess,sys,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "p=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)']); "
        "Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)"
    )
    target = subprocess.Popen([sys.executable, "-c", program, str(marker)], start_new_session=True)
    child = None
    try:
        deadline = time.monotonic() + 5
        while not marker.exists():
            assert time.monotonic() < deadline
            time.sleep(.01)
        child = ProcessIdentity.from_pid(int(marker.read_text()))
        report = contain_session(ProcessIdentity.from_pid(target.pid), grace_seconds=.1)
        target.wait(timeout=5)
        assert any(member["pid"] == child.pid for member in report["members"])
        deadline = time.monotonic() + 5
        while probe_identity(child) != DEAD:
            assert time.monotonic() < deadline
            time.sleep(.02)
    finally:
        if target.poll() is None:
            target.kill()
            target.wait(timeout=5)
        if child is not None and probe_identity(child) == LIVE:
            import os
            import signal
            os.kill(child.pid, signal.SIGKILL)
