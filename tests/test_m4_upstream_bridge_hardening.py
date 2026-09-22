"""Pure hardening contracts for the pinned upstream resolver bridge.

These tests never execute Node or the GSD resolver. They exercise only path,
file-type, and response-validation boundaries with disposable filesystem data.
"""
from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest


def _fifo_reader(path: str, connection) -> None:
    import run_state.upstream as upstream

    original_open = upstream.os.open

    def observed_open(target, flags, *args, **kwargs):
        if os.fspath(target) == Path(path).name and kwargs.get("dir_fd") is not None:
            connection.send({"event": "fifo-open", "flags": flags})
        return original_open(target, flags, *args, **kwargs)

    upstream.os.open = observed_open
    try:
        upstream._anchored_regular(Path(path))
    except upstream.UpstreamRefused as error:
        connection.send({"event": "result", "kind": "refused", "code": error.code})
    except BaseException as error:
        connection.send({"event": "result", "kind": type(error).__name__})
    else:
        connection.send({"event": "result", "kind": "accepted"})
    finally:
        connection.close()


def test_runtime_fifo_is_opened_nonblocking_and_refused_without_a_writer(tmp_path: Path) -> None:
    """Synchronize at the real open boundary; setup time cannot consume the oracle."""
    fifo = tmp_path / "runtime.cjs"
    os.mkfifo(fifo, 0o600)
    context = multiprocessing.get_context("fork")
    receive, send = context.Pipe(duplex=False)
    child = context.Process(target=_fifo_reader, args=(str(fifo), send))
    child.start()
    send.close()
    writer = -1
    try:
        assert receive.poll(2.0), "child never reached the actual FIFO open boundary"
        opened = receive.recv()
        assert opened["event"] == "fifo-open"
        assert opened["flags"] & os.O_NONBLOCK, "FIFO read could block before fstat refusal"
        assert receive.poll(2.0), "nonblocking FIFO refusal did not return"
        assert receive.recv() == {
            "event": "result", "kind": "refused", "code": "UPSTREAM_RUNTIME_DRIFT",
        }
    finally:
        if child.is_alive():
            try:
                writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                pass
        if writer >= 0:
            os.close(writer)
        child.join(2.0)
        if child.is_alive():
            child.terminate()
            child.join(2.0)
        receive.close()
    assert child.exitcode == 0


@pytest.mark.parametrize("escape", ["project", "workstreams"])
def test_response_binding_refuses_symlink_at_every_planning_root_component(
    tmp_path: Path, escape: str,
) -> None:
    from run_state.upstream import UpstreamRefused, UpstreamRuntime, _response_binding

    workspace = tmp_path / "workspace"
    planning = workspace / ".planning"
    planning.mkdir(parents=True)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    sentinel = foreign / "sentinel.bin"
    sentinel.write_bytes(b"outside unchanged\n")
    if escape == "project":
        (planning / "project").symlink_to(foreign, target_is_directory=True)
        workstream = None
        expected = planning / "project"
    else:
        project = planning / "project"
        project.mkdir()
        (project / "workstreams").symlink_to(foreign, target_is_directory=True)
        workstream = "stream"
        expected = project / "workstreams" / workstream
    runtime = UpstreamRuntime(Path("/registered/node"), "a" * 64, Path("/modules"), (), "b" * 64)
    response = {
        "project": "project", "workstream": workstream,
        "session_key": "session", "effective_session_key": "gsd-session-key-session",
        "planning_root": str(expected),
    }
    with pytest.raises(UpstreamRefused) as refused:
        _response_binding(response, runtime, workspace)
    assert refused.value.code == "UPSTREAM_ESCAPE"
    assert sentinel.read_bytes() == b"outside unchanged\n"


@pytest.mark.parametrize("value", ["/tmp/./runtime", "/tmp//runtime", "/tmp/a/../runtime"])
def test_absolute_runtime_paths_refuse_noncanonical_literal_aliases(value: str) -> None:
    from run_state.upstream import UpstreamRefused, _absolute_path

    with pytest.raises(UpstreamRefused) as refused:
        _absolute_path(value)
    assert refused.value.code == "UPSTREAM_INVALID"


@pytest.mark.parametrize(
    "value", [".", "has space", "ümlaut", "_mapped_"],
)
def test_scope_and_session_segments_refuse_values_the_resolver_would_remap(value: str) -> None:
    from run_state.upstream import UpstreamRefused, _validate_segment

    with pytest.raises(UpstreamRefused) as refused:
        _validate_segment(value, allow_none=False)
    assert refused.value.code == "UPSTREAM_INVALID"
