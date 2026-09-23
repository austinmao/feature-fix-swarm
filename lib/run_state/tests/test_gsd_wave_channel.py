"""Focused containment tests for the GSD descendant-to-owner wave transport."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from process_identity import LIVE, ProcessIdentity
import run_state.worker_channel as channel_module
from run_state.gsd_wave_bridge import (
    GsdWaveBridgeRefused, _file_channel_from_environment, persist_manifest,
)
from run_state.worker_channel import WorkerBinding, WorkerChannelRefused, WorkerChannelServer, _live_descendant
from run_state.ownership import OwnershipRefused


def _manifest(root: Path, *, prompt: str = "fresh prompt") -> bytes:
    value = {
        "schema": "ffs.gsd-supervised-dispatch/v1", "mode": "ffs-supervised-process",
        "phase": "1", "wave": 1, "initial_head": "a" * 40, "commit_mode": "patches",
        "apply_between_waves": True, "orchestrator_root": str(root),
        "admission": {"schema": "ffs.supervisor-admission/v1", "available": True,
                      "repository_id": "repo", "run_id": "run", "activity_id": "activity",
                      "generation": 1, "workspace": str(root), "runtime_identity": "runtime"},
        "plans": [{
            "id": "01-01", "initial_head": "a" * 40, "prompt": prompt, "prompt_fresh": True,
            "prompt_nonce": "nonce", "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "files_modified": ["src/a.py"], "files_deleted": [],
            "depends_on": [],
        }],
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _binding(root: Path) -> WorkerBinding:
    identity = ProcessIdentity("host", "boot", 101, "start")
    return WorkerBinding("repo", "run", "activity", "intent", 1, identity, str(root), "runtime",
                         "c" * 64, "d" * 64, ("worker",), (str(root),), identity)


def test_bridge_persists_only_canonical_bounded_workspace_evidence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    raw = _manifest(tmp_path)
    locator, digest = persist_manifest(raw, tmp_path)
    target = tmp_path / locator
    assert target.read_bytes() == raw
    assert digest == hashlib.sha256(raw).hexdigest()
    assert stat_mode(target) == 0o600
    assert persist_manifest(raw, tmp_path) == (locator, digest)
    malicious = json.loads(raw)
    malicious["orchestrator_root"] = str(tmp_path.parent)
    with pytest.raises(GsdWaveBridgeRefused, match="WAVE_WORKSPACE_MISMATCH"):
        persist_manifest(json.dumps(malicious, sort_keys=True, separators=(",", ":")).encode(), tmp_path)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_bridge_accepts_only_the_bounded_file_channel_capability(monkeypatch, tmp_path):
    root = tmp_path.resolve() / "channel"
    monkeypatch.setenv("FFS_WORKER_FILE_CHANNEL", json.dumps({
        "root": str(root), "capability": "x" * 43,
    }, sort_keys=True, separators=(",", ":")))
    assert _file_channel_from_environment() == (str(root), "x" * 43)
    monkeypatch.setenv(
        "FFS_WORKER_FILE_CHANNEL",
        '{"root":"' + str(root) + '","root":"/different","capability":"' + "x" * 43 + '"}',
    )
    with pytest.raises(GsdWaveBridgeRefused, match="WAVE_CHANNEL_UNAVAILABLE"):
        _file_channel_from_environment()


def test_server_rejects_escape_symlink_and_hash_tamper(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    raw = _manifest(root)
    locator, digest = persist_manifest(raw, root)
    binding = _binding(root)
    assert WorkerChannelServer._read_wave_manifest(binding, Path(locator), digest)["wave"] == 1
    with pytest.raises(WorkerChannelRefused, match="IPC_WAVE_LOCATOR_INVALID"):
        WorkerChannelServer._read_wave_manifest(binding, Path("../outside.json"), digest)
    (root / "linked.json").symlink_to(root / locator)
    with pytest.raises(WorkerChannelRefused, match="IPC_WAVE_LOCATOR_INVALID"):
        WorkerChannelServer._read_wave_manifest(binding, Path("linked.json"), digest)
    (root / locator).write_bytes(raw + b" ")
    with pytest.raises(WorkerChannelRefused, match="IPC_WAVE_MANIFEST_HASH_MISMATCH"):
        WorkerChannelServer._read_wave_manifest(binding, Path(locator), digest)


class _Store:
    def __init__(self):
        self.events = {}
        self.fence_active = False

    @contextmanager
    def fenced_operation(self, _token):
        self.fence_active = True
        try:
            yield
        finally:
            self.fence_active = False

    def transaction(self):
        return nullcontext(self)

    def execute(self, _query, parameters=()):
        self.parameters = parameters
        return self

    def fetchone(self):
        return object() if len(getattr(self, "parameters", ())) > 1 and self.parameters[1] in self.events else None

    def record_event_once(self, _token, _activity, key, payload):
        previous = self.events.get(key)
        if previous is not None:
            if previous["payload"] != payload:
                raise OwnershipRefused("IDEMPOTENCY_CONFLICT")
            return previous
        event = {"id": len(self.events) + 1, "payload": payload}
        self.events[key] = event
        return event


def test_wave_event_replays_conflicts_and_calls_consumer_after_admission(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    raw = _manifest(root)
    locator, digest = persist_manifest(raw, root)
    binding = _binding(root)
    server = object.__new__(WorkerChannelServer)
    server.store, server.token = _Store(), object()
    server._lock, server._primary_bindings, server._bindings = threading.RLock(), {binding.intent_id: binding}, {}
    server._delegate_consumer, server._wave_consumer = None, None
    server._verify_binding = lambda _tx, _binding: None
    calls = []
    def consume(event_id):
        assert not server.store.fence_active and not server._lock._is_owned()
        calls.append(event_id)
        return {"wave_event": event_id}
    server.attach_wave_consumer(consume)
    descendant = ProcessIdentity("host", "boot", 202, "descendant")
    monkeypatch.setattr(channel_module, "probe_identity", lambda _identity: LIVE)
    monkeypatch.setattr(channel_module, "_live_descendant", lambda peer, primary: peer == descendant and primary == binding.identity)
    server.assert_authorized_wave_peer(binding.intent_id, descendant)
    message = {"schema_version": 1, **binding.scope(), "request_key": "wave-1", "operation": "gsd-wave-request",
               "body": {"manifest_locator": locator, "manifest_sha256": digest}}
    first = server._request(descendant, message)
    replay = server._request(descendant, message)
    assert first["replayed"] is False and replay["replayed"] is True
    assert first["result"] == replay["result"] == {"wave_event": 1}
    assert calls == [1, 1]
    alternate = ".planning/.ffs-wave-requests/alternate.json"
    (root / alternate).write_bytes(raw)
    with pytest.raises(OwnershipRefused, match="IDEMPOTENCY_CONFLICT"):
        server._request(descendant, {**message, "body": {"manifest_locator": alternate, "manifest_sha256": digest}})


def test_full_native_ancestry_rejects_pid_reuse_and_orphan(tmp_path):
    child_code = "import time; time.sleep(20)"
    parent_code = (
        "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c',sys.argv[1]]); "
        "print(p.pid, flush=True); time.sleep(20)"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_code, child_code], stdout=subprocess.PIPE, text=True)
    child = None
    try:
        line = parent.stdout.readline().strip()
        child = ProcessIdentity.from_pid(int(line))
        parent_identity = ProcessIdentity.from_pid(parent.pid)
        assert _live_descendant(child, parent_identity)
        assert not _live_descendant(replace(child, start_token="reused"), parent_identity)
        parent.terminate()
        parent.wait(timeout=5)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and _live_descendant(child, parent_identity):
            time.sleep(.01)
        assert not _live_descendant(child, parent_identity)
    finally:
        if parent.poll() is None:
            parent.terminate()
            parent.wait(timeout=5)
        if child is not None:
            try:
                os.kill(child.pid, 15)
            except ProcessLookupError:
                pass
