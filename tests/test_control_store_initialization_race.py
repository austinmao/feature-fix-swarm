"""A second initializer cannot anchor the DB before its lock protocol exists."""
import json
import fcntl
import hashlib
import os
from pathlib import Path
import selectors
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))


def test_database_and_lock_protocol_are_observed_as_one_initialization(tmp_path):
    authority = tmp_path / "authority"
    authority.mkdir(mode=0o700)
    reached, release = tmp_path / "published", tmp_path / "release"
    script = """
import json,pathlib,sys,time
import run_state.state as state
path,reached,release,mode=map(str,sys.argv[1:])
if mode=='publisher':
 original=state._provision_authority_lock_protocol
 def hold(*args):
  pathlib.Path(reached).touch()
  end=time.monotonic()+10
  while not pathlib.Path(release).exists():
   if time.monotonic()>end: raise RuntimeError('release deadline')
   time.sleep(.01)
  return original(*args)
 state._provision_authority_lock_protocol=hold
store=state.ControlStore(pathlib.Path(path))
print(json.dumps({'initialized':True,'sidecar_bound':store._anchor.sidecar is not None}),flush=True)
store.held_reservations([('objective','same-objective')])
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "lib"))
    children = []
    try:
        first = subprocess.Popen([sys.executable, "-c", script, str(authority / "control.sqlite3"),
                                  str(reached), str(release), "publisher"],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        children.append(first)
        import time
        deadline = time.monotonic() + 5
        while not reached.exists() and time.monotonic() < deadline:
            assert first.poll() is None
            time.sleep(.01)
        assert reached.exists()
        second = subprocess.Popen([sys.executable, "-c", script, str(authority / "control.sqlite3"),
                                   str(reached), str(release), "contender"],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        children.append(second)
        with selectors.DefaultSelector() as selector:
            selector.register(second.stdout, selectors.EVENT_READ)
            assert not selector.select(timeout=.2), "contender observed partially initialized authority"
        release.touch()
        for child in children:
            out, err = child.communicate(timeout=10)
            assert child.returncode == 0, err
            assert json.loads(out) == {"initialized": True, "sidecar_bound": True}
    finally:
        release.touch()
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)


def test_interrupted_protocol_is_not_adopted_and_bootstrap_lock_is_released(tmp_path, monkeypatch):
    import run_state.state as state

    authority = tmp_path / "authority"
    authority.mkdir(mode=0o700)
    path = authority / "control.sqlite3"
    original = state._provision_authority_lock_protocol

    def interrupt(*args):
        raise RuntimeError("interrupted after database publication")

    monkeypatch.setattr(state, "_provision_authority_lock_protocol", interrupt)
    with pytest.raises(RuntimeError, match="interrupted"):
        state.ControlStore(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(state, "_provision_authority_lock_protocol", original)
    reopened = state.ControlStore(path)
    assert reopened._anchor.sidecar is None
    with pytest.raises(state.ControlStoreRefused, match="UNSUPPORTED_SCHEMA"):
        with reopened.transaction():
            pytest.fail("interrupted protocol must not admit a writer")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert not (authority / ".control-locks").exists()
    # A separate database under the same parent can initialize after failure.
    other = state.ControlStore(authority / "other.sqlite3")
    assert other._anchor.sidecar is not None


def test_bootstrap_contention_is_bounded_and_does_not_publish_database(tmp_path):
    authority = tmp_path / "authority"
    authority.mkdir(mode=0o700)
    fd = os.open(authority, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        script = """
from pathlib import Path
import sys
from run_state.state import ControlStore,ControlStoreRefused
try: ControlStore(Path(sys.argv[1]))
except ControlStoreRefused as error: print(error.code)
"""
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "lib"))
        result = subprocess.run([sys.executable, "-c", script, str(authority / "control.sqlite3")],
                                env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "STORE_BUSY"
        assert not (authority / "control.sqlite3").exists()
    finally:
        os.close(fd)
