"""Wire limits for the native-review bootstrap transport."""
from __future__ import annotations

import socket
import threading
from io import BytesIO

import pytest

from run_state.supervisor import (
    SupervisorRefused, _NATIVE_REVIEW_BOOTSTRAP_LIMIT, _SUPERVISOR_FRAME_LIMIT,
    _bootstrap_frame_limit, _canonical, _receive, _send,
)


class _Capture:
    def sendall(self, value: bytes) -> None:
        self.value = value


def _payload_of_size(size: int, *, escaped: bool = False) -> dict[str, str]:
    """Build a canonical object with exactly ``size`` encoded bytes."""
    empty = _canonical({"prompt": ""})
    character = "\\" if escaped else "x"
    width, remainder = divmod(size - len(empty), len(_canonical(character)[1:-1]))
    value = {"prompt": character * width + "x" * remainder}
    assert len(_canonical(value)) == size
    return value


def _escaped_bootstrap() -> dict[str, str]:
    # Backslashes expand to two bytes in the canonical JSON frame. This is
    # large enough to cross the generic cap while remaining under the fixed
    # native bootstrap cap.
    value = _payload_of_size(_SUPERVISOR_FRAME_LIMIT + 1, escaped=True)
    assert _SUPERVISOR_FRAME_LIMIT < len(_canonical(value)) <= _NATIVE_REVIEW_BOOTSTRAP_LIMIT
    return value


@pytest.mark.parametrize("native_review", [False, True])
def test_bootstrap_sender_enforces_exact_frame_boundary(native_review: bool) -> None:
    limit = _bootstrap_frame_limit(native_review=native_review)
    capture = _Capture()
    _send(capture, _payload_of_size(limit), max_bytes=limit)
    assert len(capture.value) == limit + 4
    with pytest.raises(SupervisorRefused, match="MESSAGE_TOO_LARGE"):
        _send(_Capture(), _payload_of_size(limit + 1), max_bytes=limit)


@pytest.mark.parametrize("native_review", [False, True])
def test_bootstrap_receiver_enforces_exact_frame_boundary(native_review: bool) -> None:
    limit = _bootstrap_frame_limit(native_review=native_review)
    payload = _payload_of_size(limit)
    capture = _Capture()
    _send(capture, payload, max_bytes=limit)
    class BufferedChannel:
        recv = BytesIO(capture.value).read
    assert _receive(BufferedChannel(), max_bytes=limit) == payload


class _HeaderOnly:
    def __init__(self, size: int) -> None:
        self._header = size.to_bytes(4, "big")
        self.body_reads = 0

    def recv(self, size: int) -> bytes:
        if self._header:
            value, self._header = self._header, b""
            return value
        self.body_reads += 1
        raise AssertionError("receiver read an over-limit body")


@pytest.mark.parametrize("native_review", [False, True])
def test_bootstrap_receiver_rejects_overlimit_header_before_body(native_review: bool) -> None:
    limit = _bootstrap_frame_limit(native_review=native_review)
    channel = _HeaderOnly(limit + 1)
    with pytest.raises(SupervisorRefused, match="INVALID_MESSAGE"):
        _receive(channel, max_bytes=limit)
    assert channel.body_reads == 0


def test_generic_transport_refuses_native_sized_bootstrap() -> None:
    with pytest.raises(SupervisorRefused, match="MESSAGE_TOO_LARGE"):
        _send(_Capture(), _escaped_bootstrap())


def test_ack_and_permit_receiver_default_refuses_native_sized_header() -> None:
    channel = _HeaderOnly(_SUPERVISOR_FRAME_LIMIT + 1)
    with pytest.raises(SupervisorRefused, match="INVALID_MESSAGE"):
        _receive(channel)
    assert channel.body_reads == 0


def test_native_bootstrap_round_trips_above_generic_cap() -> None:
    parent, child = socket.socketpair()
    received: dict[str, object] = {}

    def receiver() -> None:
        try:
            received["value"] = _receive(
                child, max_bytes=_bootstrap_frame_limit(native_review=True),
            )
        except BaseException as error:  # surfaced by the test thread
            received["error"] = error
        finally:
            child.close()

    thread = threading.Thread(target=receiver)
    thread.start()
    try:
        payload = _escaped_bootstrap()
        _send(parent, payload, max_bytes=_bootstrap_frame_limit(native_review=True))
    finally:
        parent.close()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert "error" not in received
    assert received["value"] == payload
