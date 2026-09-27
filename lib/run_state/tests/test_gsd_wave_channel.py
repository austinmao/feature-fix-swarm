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
import tempfile
import threading
import time

import pytest

from process_identity import DEAD, LIVE, UNKNOWN, ProcessIdentity
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


@pytest.mark.parametrize("host_id, recorded", [("host", "requester"), ("host:pidns:9:9", "orchestrator")])
def test_file_requester_from_another_pid_namespace_records_the_orchestrator(tmp_path, host_id, recorded):
    # A sandboxed bridge in its own PID namespace names a pid the supervisor
    # cannot probe; the request falls back to the orchestrator, as before F37b.
    binding = _binding(tmp_path)
    requester = {"host_id": host_id, "boot_id": "boot", "pid": 202, "start_token": "bridge"}
    wrapped = {"capability": "c", "requester": requester,
               "message": {"operation": "gsd-wave-request", "intent_id": binding.intent_id}}
    expected = ProcessIdentity(**requester) if recorded == "requester" else binding.identity
    assert WorkerChannelServer._file_requester(binding, wrapped) == expected


@pytest.mark.parametrize("orchestrator, accepted", [(DEAD, True), (UNKNOWN, False)])
def test_delivery_to_admitted_peer_requires_a_dead_orchestrator(tmp_path, monkeypatch, orchestrator, accepted):
    # F37b: only an orchestrator that probes DEAD lets the admitted peer
    # collect its reply outside the live ancestry; UNKNOWN is not proof.
    binding = _binding(tmp_path)
    server = object.__new__(WorkerChannelServer)
    server._lock, server._primary_bindings = threading.RLock(), {binding.intent_id: binding}
    peer = ProcessIdentity("host", "boot", 202, "peer")
    monkeypatch.setattr(channel_module, "_live_descendant", lambda _peer, _ancestor: False)
    monkeypatch.setattr(channel_module, "probe_identity",
                        lambda identity: orchestrator if identity == binding.identity else LIVE)
    if accepted:
        server.assert_authorized_wave_peer(binding.intent_id, peer)
    else:
        with pytest.raises(WorkerChannelRefused, match="IPC_DESCENDANT_ANCESTRY_MISMATCH"):
            server.assert_authorized_wave_peer(binding.intent_id, peer)


@contextmanager
def _file_channel_server(tmp_path):
    """A real server whose file channels use a stubbed request handler."""
    workspace = tmp_path.resolve() / "workspace"
    workspace.mkdir()
    binding = _binding(workspace)
    with tempfile.TemporaryDirectory(prefix="ffs-lock-", dir="/tmp") as directory:
        server = WorkerChannelServer(_Store(), object(), Path(directory).resolve() / "worker.sock")
        server._verify_binding = lambda _tx, _binding: None
        server._request = lambda _peer, _message: {"ok": True}
        try:
            yield server, binding
        finally:
            server.close()


def _register(server, binding, intent_id="intent"):
    binding = replace(binding, intent_id=intent_id)
    server._primary_bindings[intent_id] = binding
    channel = server.register_file_transport(intent_id)
    return binding, Path(channel["root"]), channel["capability"]


def _file_request_file(root, capability, binding, operation, key):
    message = {"schema_version": 1, **binding.scope(), "request_key": key,
               "operation": operation, "body": {}}
    name = key + ".json"
    descriptor = os.open(root / "requests" / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, channel_module._canonical({"capability": capability, "message": message}))
    finally:
        os.close(descriptor)
    return root / "responses" / name


def _held(root):
    return (root / "supervisor.lock").is_file() and not channel_module._supervisor_lock_released(root)


def test_supervisor_lock_is_held_from_registration(tmp_path):
    # F37b: a client whose supervisor dies before its first pickup must
    # still find the lock, so it exists from registration on.
    with _file_channel_server(tmp_path) as (server, binding):
        _binding, root, _capability = _register(server, binding)
        assert _held(root)
    assert channel_module._supervisor_lock_released(root)


def test_registrations_on_one_filesystem_share_one_held_lock(tmp_path):
    with _file_channel_server(tmp_path) as (server, binding):
        roots = [_register(server, binding, f"intent-{index}")[1] for index in range(20)]
        assert len(server._lock_fds) == 1
        assert len({(root / "supervisor.lock").stat().st_ino for root in roots}) == 1
        assert all(_held(root) for root in roots)


def test_other_filesystem_gets_its_own_held_lock(tmp_path, monkeypatch):
    import errno

    def cross_device(*_args, **_kwargs):
        raise OSError(errno.EXDEV, "cross-device link")

    with _file_channel_server(tmp_path) as (server, binding):
        monkeypatch.setattr(channel_module.os, "link", cross_device)
        _binding, root, _capability = _register(server, binding)
        assert len(server._lock_fds) == 2
        assert (root / "supervisor.lock").stat().st_ino != server._supervisor_lock.stat().st_ino
        assert _held(root)


def test_removed_device_lock_is_replaced_for_later_registrations(tmp_path, monkeypatch):
    # The root holding a device's lock may be removed (a finished worktree);
    # later registrations on that device hold a fresh lock instead of failing.
    import errno
    real_link = os.link

    def cross_device_from_master(source, target, **kwargs):
        if Path(source) == server._supervisor_lock:
            raise OSError(errno.EXDEV, "cross-device link")
        return real_link(source, target, **kwargs)

    with _file_channel_server(tmp_path) as (server, binding):
        monkeypatch.setattr(channel_module.os, "link", cross_device_from_master)
        _first, first_root, _capability = _register(server, binding, "intent-a")
        # Another channel still links the removed root's lock, so the server
        # keeps that descriptor: existing clients probe that inode.
        _other, other_root, _capability = _register(server, binding, "intent-a2")
        (first_root / "supervisor.lock").unlink()
        _second, second_root, _capability = _register(server, binding, "intent-b")
        assert len(server._lock_fds) == 3
        assert _held(second_root)
        assert _held(other_root)


def test_failed_lock_leaves_no_descriptor_and_refuses_registration(tmp_path, monkeypatch):
    def refused(*_args, **_kwargs):
        raise BlockingIOError("lock held elsewhere")

    with _file_channel_server(tmp_path) as (server, binding):
        before = len(os.listdir("/dev/fd"))
        monkeypatch.setattr(channel_module.fcntl, "flock", refused)
        with pytest.raises(WorkerChannelRefused, match="IPC_FILE_CHANNEL_UNSAFE"):
            _register(server, binding)
        monkeypatch.undo()
        assert server._lock_fds == []
        assert len(os.listdir("/dev/fd")) == before


def test_lock_outlives_a_busy_close_until_the_reply_is_written(tmp_path):
    # close() during a wave keeps the lock so the waiting bridge does not see
    # its supervisor gone; the serving thread releases it once it has
    # published that reply and stopped, so later clients are not stranded.
    with _file_channel_server(tmp_path) as (server, binding):
        binding, root, capability = _register(server, binding)
        entered, release = threading.Event(), threading.Event()

        def busy(_peer, _message):
            entered.set()
            release.wait(timeout=30)
            return {"ok": True}

        server._request = busy
        server.start()
        response = _file_request_file(root, capability, binding, "gsd-wave-request", "wave-1")
        assert entered.wait(timeout=10)
        server.close()
        assert server._thread.is_alive()
        assert _held(root)
        release.set()
        server._thread.join(timeout=10)
        assert json.loads(response.read_text()) == {"ok": True}
        assert channel_module._supervisor_lock_released(root)



def _cross_device_from_master(monkeypatch, server):
    import errno
    real_link = os.link

    def link(source, target, **kwargs):
        if Path(source) == server._supervisor_lock:
            raise OSError(errno.EXDEV, "cross-device link")
        return real_link(source, target, **kwargs)

    monkeypatch.setattr(channel_module.os, "link", link)


@pytest.mark.parametrize("source", ["device", "master"])
def test_replaced_unheld_lock_source_is_never_linked(tmp_path, monkeypatch, source):
    # A lock path replaced behind the server's back names a file it does not
    # hold; linking it would let a client see a live supervisor as gone.
    with _file_channel_server(tmp_path) as (server, binding):
        if source == "device":
            _cross_device_from_master(monkeypatch, server)
        _first, first_root, _capability = _register(server, binding, "intent-a")
        replaced = first_root / "supervisor.lock" if source == "device" else server._supervisor_lock
        replaced.unlink()
        os.close(os.open(replaced, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        _second, second_root, _capability = _register(server, binding, "intent-b")
        assert _held(second_root)


def test_busy_close_removes_the_master_lock_once_the_thread_stops(tmp_path):
    # Otherwise a new server at the same endpoint could never create its own.
    with _file_channel_server(tmp_path) as (server, binding):
        binding, root, capability = _register(server, binding)
        master = server._supervisor_lock
        entered, release = threading.Event(), threading.Event()

        def busy(_peer, _message):
            entered.set()
            release.wait(timeout=30)
            return {"ok": True}

        server._request = busy
        server.start()
        _file_request_file(root, capability, binding, "gsd-wave-request", "wave-1")
        assert entered.wait(timeout=10)
        server.close()
        assert master.exists()
        release.set()
        server._thread.join(timeout=10)
        assert not master.exists()
        successor = WorkerChannelServer(_Store(), object(), server.endpoint)
        try:
            successor._verify_binding = lambda _tx, _binding: None
            _later, later_root, _capability = _register(successor, binding, "intent-later")
            assert _held(later_root)
        finally:
            successor.close()


def test_replacing_an_unlinked_device_lock_releases_its_descriptor(tmp_path, monkeypatch):
    # Repeatedly removed device locks must not grow the held descriptors
    # once no channel links the old inode.
    with _file_channel_server(tmp_path) as (server, binding):
        _cross_device_from_master(monkeypatch, server)
        previous = _register(server, binding, "intent-0")[1]
        assert len(server._lock_fds) == 2
        for index in range(1, 4):
            (previous / "supervisor.lock").unlink()
            previous = _register(server, binding, f"intent-{index}")[1]
            assert len(server._lock_fds) == 2
            assert _held(previous)
