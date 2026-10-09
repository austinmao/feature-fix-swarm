"""The test suite must never wait on the real host's load.

Production admission reads the live ``ResourceObservation`` (``cpu_available``
is ``cpu_count - ceil(load1)``) and ``SharedResourceCoordinator.acquire`` waits
without a deadline by design.  A test that builds a ``Supervisor`` without its
own coordinator inherits that default, so on a loaded host the whole suite
parks at the next launch (spec-014 E8 Suite row on 2026-10-08: 203 minutes at
load1 25 to 142).  The autouse fixture in ``tests/conftest.py`` gives every
default coordinator a fixture observation instead; this test pins that seam.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from run_state import shared_resources
from run_state.resource_watchdog import LocalObservationCollector


def test_default_shared_resource_queue_never_samples_the_real_host(tmp_path: Path, monkeypatch) -> None:
    def real_host_sampled(self) -> None:
        raise AssertionError("the default admission queue sampled the real host")

    monkeypatch.setattr(LocalObservationCollector, "__call__", real_host_sampled)
    queue = shared_resources.ManagedAdmissionQueue(tmp_path / "admission")
    observation = queue._observe()
    assert observation.source == "fixture"
    assert observation.cpu_available is not None and observation.cpu_available >= 1


def test_explicit_observation_provider_is_kept(tmp_path: Path) -> None:
    from run_state.resource_observation import ResourceObservation
    import time

    mine = ResourceObservation(time.monotonic_ns(), 1, 1 << 30, 1 << 30, 1, 1, {}, "mine")
    queue = shared_resources.ManagedAdmissionQueue(tmp_path / "admission", observation_provider=lambda: mine)
    assert queue._observe() is mine
