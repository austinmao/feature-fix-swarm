"""Deterministic admission capacity for tests and their child processes.

Admission is resource-derived, so a queue left on the production collector
admits by live host load.  Every queue a test builds, in-process or in a
``python -c`` child, takes its envelope from here instead.
"""
from __future__ import annotations

import time

from run_state.managed_admission import ManagedAdmissionQueue
from run_state.resource_observation import ResourceObservation

CPU = 2  # default demand is one CPU, so this admits exactly two runs


def observe(cpu: int = CPU) -> ResourceObservation:
    return ResourceObservation(
        observed_monotonic_ns=time.monotonic_ns(),
        cpu_available=cpu,
        memory_available_bytes=1 << 40,
        disk_available_bytes=1 << 40,
        io_available_units=100,
        process_available=4096,
        source="fixture",
    )


def queue(root=None, *, cpu: int = CPU, **kwargs) -> ManagedAdmissionQueue:
    return ManagedAdmissionQueue(
        root, observation_provider=lambda: observe(cpu), **kwargs
    )
