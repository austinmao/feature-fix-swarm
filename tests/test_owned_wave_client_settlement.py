"""An exited client cannot settle authority ahead of its claimed wave."""
from types import SimpleNamespace
from pathlib import Path
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from run_state.supervisor import SupervisorRefused, finish_owned_wave_client
from run_state.wave_consumer import WaveConsumer


def test_exited_client_waits_for_wave_publication_before_launch_settlement():
    consumer = object.__new__(WaveConsumer)
    consumer._lock = threading.Lock()
    claimed, publish, settled = threading.Event(), threading.Event(), threading.Event()
    events = []
    consumer._settled_requests = lambda intent_id: intent_id == "outer" and bool(events)

    def wave():
        with consumer._lock:
            claimed.set()
            assert publish.wait(5)
            events.append("durable-wave-reply")

    def finish(_handle, *, timeout):
        assert 0 < timeout <= 5
        events.append("settle-client-intent")
        settled.set()
        return {"returncode": 0}

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=5)
    wave_thread = threading.Thread(target=wave)
    wave_thread.start()
    assert claimed.wait(5)
    result = []
    owner = threading.Thread(target=lambda: result.append(finish_owned_wave_client(
        SimpleNamespace(finish=finish, _wait_admitted=lambda handle, timeout: handle.process.wait(timeout=timeout),
                        _policy_timeout=lambda timeout: (timeout, False)),
        SimpleNamespace(process=child, intent_id="outer"), consumer, timeout=5,
    )))
    owner.start()
    try:
        assert not settled.wait(0.05)
        publish.set()
        owner.join(5)
        wave_thread.join(5)
        assert not owner.is_alive()
        assert result == [{"returncode": 0}]
        assert events == ["durable-wave-reply", "settle-client-intent"]
    finally:
        publish.set()
        owner.join(5)
        wave_thread.join(5)


def test_wave_deadline_never_settles_issuer_while_consumer_owns_wave():
    consumer = object.__new__(WaveConsumer)
    consumer._lock = threading.Lock()
    consumer._settled_requests = lambda _intent_id: False
    settled = []
    expired = []
    with consumer._lock:
        with pytest.raises(SupervisorRefused, match="WAVE_SETTLEMENT_TIMEOUT"):
            finish_owned_wave_client(
                SimpleNamespace(finish=lambda *_a, **_k: settled.append(True),
                                _policy_timeout=lambda timeout: (timeout, False),
                                expire_launch=lambda *_a, **_k: expired.append(True)),
                SimpleNamespace(process=None, intent_id="outer"), consumer, timeout=0.01,
            )
    assert settled == []
    assert expired == [True]
