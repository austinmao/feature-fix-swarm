"""Large patch replies do not expand worker request or ordinary reply limits."""
import io
import json
from dataclasses import asdict

import pytest

from process_identity import ProcessIdentity
from run_state import worker_channel as channel


class Wire:
    def __init__(self, value=None, size=None):
        raw = json.dumps(value, separators=(",", ":")).encode() if size is None else b""
        self.input = io.BytesIO((len(raw) if size is None else size).to_bytes(4, "big") + raw)
        self.sent = bytearray()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def settimeout(self, timeout):
        pass

    def connect(self, endpoint):
        pass

    def recv(self, size):
        return self.input.read(size)

    def sendall(self, raw):
        self.sent.extend(raw)


def test_only_wave_client_accepts_large_reply(monkeypatch):
    reply = {"ok": True, "result": {"patch": "x" * 100_000}}
    identity = ProcessIdentity.current()
    monkeypatch.setattr(channel, "peer_identity", lambda _: identity)
    monkeypatch.setattr(channel.socket, "socket", lambda *_: Wire(reply))
    scope = {"supervisor_identity": asdict(identity)}
    assert channel.request("/fixture", scope, request_key="wave", operation="gsd-wave-request", body={}) == reply
    with pytest.raises(channel.WorkerChannelRefused, match="IPC_MESSAGE_TOO_LARGE"):
        channel.request("/fixture", scope, request_key="normal", operation="progress", body={})


def test_large_wave_requests_and_oversized_wave_replies_are_refused(monkeypatch):
    identity = ProcessIdentity.current()
    monkeypatch.setattr(channel, "peer_identity", lambda _: identity)
    monkeypatch.setattr(channel.socket, "socket", lambda *_: Wire({"ok": True}))
    with pytest.raises(channel.WorkerChannelRefused, match="IPC_MESSAGE_TOO_LARGE"):
        channel.request("/fixture", {"supervisor_identity": asdict(identity)}, request_key="wave",
                        operation="gsd-wave-request", body={"oversize": "x" * 100_000})
    with pytest.raises(channel.WorkerChannelRefused, match="IPC_MESSAGE_TOO_LARGE"):
        channel._receive(Wire(size=channel._MAX_WAVE_REPLY_BYTES + 1), max_bytes=channel._MAX_WAVE_REPLY_BYTES)


def test_server_wave_response_uses_large_frame_only_after_small_request(monkeypatch):
    wire = Wire({"operation": "gsd-wave-request"})
    server = object.__new__(channel.WorkerChannelServer)
    server._socket = type("Listener", (), {"accept": lambda _: (wire, None)})()
    server._request = lambda *_: {"ok": True, "result": {"patch": "x" * 100_000}}
    monkeypatch.setattr(channel, "peer_identity", lambda _: ProcessIdentity.current())
    assert server.serve_once()
    assert int.from_bytes(wire.sent[:4], "big") > 65536
    oversized = Wire(size=65537)
    server._socket = type("Listener", (), {"accept": lambda _: (oversized, None)})()
    assert server.serve_once()
    assert json.loads(oversized.sent[4:])["code"] == "IPC_MESSAGE_TOO_LARGE"
