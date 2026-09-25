"""Focused E5 watchdog tests; integration wiring remains the owner's work."""
from __future__ import annotations

import subprocess
import sys
import threading
import time

from run_state.resource_observation import ResourceDemand, ResourceObservation
from run_state.resource_watchdog import (
    CAPABILITY_FAILURE, HEALTHY, RESOURCE_WAIT, LocalObservationCollector,
    ResourceWatchdog, ResourceWatchdogPolicy, WatchdogTarget,
)


class Registry:
    def __init__(self, targets):
        self.targets = targets
        self.persisted = []
        self.started = threading.Event()

    def resource_watchdog_targets(self):
        self.started.set()
        return self.targets

    def persist_resource_watchdog_status(self, status):
        self.persisted.append(status)


def observation(now, **values):
    return ResourceObservation(now, values.get("cpu", 1), values.get("memory", 1),
                               values.get("disk", 1), values.get("io", 1),
                               values.get("processes", 1), values.get("providers"), "fixture")


def policy(**changes):
    values = dict(version="resource-watchdog/v1", sample_interval_ns=1_000_000,
                  freshness_ns=100, validation_attempts=2,
                  validation_interval_ns=0, clock_uncertainty_ns=0,
                  collector_timeout_ns=50_000_000)
    values.update(changes)
    return ResourceWatchdogPolicy(**values)


def test_background_checks_while_caller_is_blocked():
    now = 10
    registry = Registry([WatchdogTarget("waiting", ResourceDemand(cpu=1), 3)])
    watchdog = ResourceWatchdog(lambda: observation(now, cpu=0), registry,
                                policy=policy(), clock_ns=lambda: now)
    watchdog.start()
    assert registry.started.wait(1)
    assert registry.persisted and registry.persisted[0].code == RESOURCE_WAIT
    assert watchdog.stop(1)
    assert not watchdog.running


def test_valid_busy_capacity_stays_resource_wait_across_long_elapsed():
    now = 10
    registry = Registry([WatchdogTarget("busy", ResourceDemand(cpu=2), 7)])
    watchdog = ResourceWatchdog(lambda: observation(now, cpu=1), registry,
                                policy=policy(), clock_ns=lambda: now)
    first = watchdog.check_once()[0]
    now = 10**15
    second = watchdog.check_once()[0]
    assert first.code == second.code == RESOURCE_WAIT
    assert first.limiting_resources == ("cpu",)
    assert not first.structural_evidence and second.observation_age_ns == 0


def test_broken_collector_is_bounded_then_scoped_capability_failure():
    calls = 0
    registry = Registry([WatchdogTarget("cpu-work", ResourceDemand(cpu=1))])

    def broken():
        nonlocal calls
        calls += 1
        raise RuntimeError("broken")

    status = ResourceWatchdog(broken, registry, policy=policy(validation_attempts=3),
                              clock_ns=lambda: 10).check_once()[0]
    assert calls == 3 and status.scope == "cpu-work" and status.code == CAPABILITY_FAILURE
    assert status.structural_evidence == ("COLLECTOR_EXCEPTION:RuntimeError",)


def test_stale_and_clock_uncertainty_fail_closed_after_validation():
    target = WatchdogTarget("cpu-work", ResourceDemand(cpu=1))
    stale = ResourceWatchdog(lambda: observation(1), Registry([target]), policy=policy(freshness_ns=3),
                             clock_ns=lambda: 10).check_once()[0]
    future = ResourceWatchdog(lambda: observation(20), Registry([target]), policy=policy(),
                              clock_ns=lambda: 10).check_once()[0]
    assert stale.code == future.code == CAPABILITY_FAILURE
    assert stale.structural_evidence == ("RESOURCE_OBSERVATION_STALE",)
    assert future.structural_evidence == ("RESOURCE_CLOCK_UNCERTAIN",)


def test_unneeded_io_and_unknown_provider_do_not_become_structural_failure():
    target = WatchdogTarget("provider", ResourceDemand(cpu=1, provider="codex", provider_units=1))
    status = ResourceWatchdog(lambda: observation(10, io=None), Registry([target]),
                              policy=policy(), clock_ns=lambda: 10).check_once()[0]
    assert status.code == RESOURCE_WAIT
    assert status.limiting_resources == ("provider:codex",)
    assert status.structural_evidence == ("PROVIDER_EXPLORATION",)


def test_legacy_opaque_wait_reports_legacy_opaque_not_provider():
    # Without the override this demand+observation combination is HEALTHY:
    # cpu is satisfied and the provider has known available capacity. The
    # admission_verdict must still surface the real block reason.
    target = WatchdogTarget("legacy", ResourceDemand(cpu=1, provider="codex", provider_units=1),
                            admission_verdict="legacy-opaque")
    status = ResourceWatchdog(lambda: observation(10, providers={"codex": 1}), Registry([target]),
                              policy=policy(), clock_ns=lambda: 10).check_once()[0]
    assert status.code == RESOURCE_WAIT
    assert status.limiting_resources == ("legacy-opaque",)
    assert status.structural_evidence == ("LEGACY_ADMISSION_OPAQUE",)


def test_known_provider_capacity_is_not_reported_as_unknown():
    target = WatchdogTarget("provider", ResourceDemand(cpu=1, provider="codex", provider_units=1))
    status = ResourceWatchdog(lambda: observation(10, providers={"codex": 1}), Registry([target]),
                              policy=policy(), clock_ns=lambda: 10).check_once()[0]
    assert status.code == HEALTHY


def test_default_fixed_collector_is_available_to_integrator():
    watchdog = ResourceWatchdog(registry=Registry([]), policy=policy(), clock_ns=lambda: 10)
    assert isinstance(watchdog._collector, LocalObservationCollector)


def test_default_fixed_collector_returns_an_exact_resource_observation():
    value = LocalObservationCollector(timeout_ns=2_000_000_000)()
    assert type(value) is ResourceObservation
    assert type(value.observed_monotonic_ns) is int
    assert value.observed_monotonic_ns > 0


def test_real_blocked_fixture_child_meets_total_deadline_and_is_reaped():
    created = []

    def blocked_fixture_transport():
        process = subprocess.Popen(
            (sys.executable, "-c", "import time; time.sleep(60)"),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        created.append(process)
        return process

    collector = LocalObservationCollector(timeout_ns=30_000_000,
                                          _transport=blocked_fixture_transport)
    started = time.monotonic()
    try:
        collector()
    except Exception as error:
        assert getattr(error, "code", None) == "COLLECTOR_DEADLINE_EXCEEDED"
    else:
        raise AssertionError("blocked child was accepted")
    assert time.monotonic() - started < 1
    assert created and created[0].poll() is not None


def test_oversized_child_output_is_bounded_and_reaped():
    created = []

    def noisy_fixture_transport():
        process = subprocess.Popen(
            (sys.executable, "-c", "import sys; sys.stdout.write('x' * 20000)"),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        created.append(process)
        return process

    try:
        LocalObservationCollector(timeout_ns=1_000_000_000,
                                  _transport=noisy_fixture_transport)()
    except Exception as error:
        assert getattr(error, "code", None) == "COLLECTOR_OUTPUT_TOO_LARGE"
    else:
        raise AssertionError("oversized child output was accepted")
    assert created[0].poll() is not None


def test_stop_timeout_is_latched_for_a_contract_violating_injected_callback():
    entered = threading.Event()
    release = threading.Event()

    def blocked_callback():
        entered.set()
        release.wait(1)
        return observation(10)

    watchdog = ResourceWatchdog(blocked_callback, Registry([WatchdogTarget("scope", ResourceDemand(cpu=1))]),
                                policy=policy(), clock_ns=lambda: 10)
    watchdog.start()
    assert entered.wait(1)
    assert not watchdog.stop(0.01)
    assert watchdog.fatal_status is not None
    assert watchdog.fatal_status.structural_evidence == ("WATCHDOG_STOP_TIMEOUT",)
    release.set()
    assert watchdog.stop(1)


def test_registry_callback_failures_are_latched_for_dispatch_consumer():
    class BrokenRegistry(Registry):
        def persist_resource_watchdog_status(self, status):
            raise OSError("disk")

    watchdog = ResourceWatchdog(lambda: observation(10), BrokenRegistry([
        WatchdogTarget("scope", ResourceDemand(cpu=1))
    ]), policy=policy(), clock_ns=lambda: 10)
    result = watchdog.check_once()
    assert result == watchdog.latest_statuses
    assert watchdog.fatal_status == result[0]
    assert result[0].structural_evidence == ("REGISTRY_PERSIST_EXCEPTION:OSError",)


def test_target_generator_is_bounded_before_validation():
    seen = 0

    def targets():
        nonlocal seen
        while True:
            seen += 1
            yield WatchdogTarget(str(seen), ResourceDemand(cpu=1))

    result = ResourceWatchdog(lambda: observation(10), Registry(targets()),
                              policy=policy(), clock_ns=lambda: 10).check_once()
    assert seen == 1025
    assert result[0].code == CAPABILITY_FAILURE
    assert result[0].structural_evidence == ("REGISTRY_STATUS_EXCEPTION:ValueError",)


def test_clock_callback_failure_is_latched_without_fabricated_timestamps():
    def bad_clock():
        raise RuntimeError("clock")

    watchdog = ResourceWatchdog(lambda: observation(10), Registry([]),
                                policy=policy(), clock_ns=bad_clock)
    result = watchdog.check_once()
    assert result == watchdog.latest_statuses
    assert watchdog.fatal_status == result[0]
    assert result[0].last_check_ns is result[0].next_check_ns is None
    assert result[0].structural_evidence == ("RESOURCE_CLOCK_UNCERTAIN:RuntimeError",)


def test_negative_configured_clock_movement_latches_failure_and_inhibits_work():
    values = iter((10, 9))
    calls = 0

    def collector():
        nonlocal calls
        calls += 1
        return observation(10)

    watchdog = ResourceWatchdog(collector, Registry([WatchdogTarget("scope", ResourceDemand(cpu=1))]),
                                policy=policy(), clock_ns=lambda: next(values))
    result = watchdog.check_once()
    assert calls == 1
    assert result == watchdog.latest_statuses
    assert watchdog.fatal_status == result[0]
    assert result[0].last_check_ns is result[0].next_check_ns is None
    assert result[0].structural_evidence == ("RESOURCE_CLOCK_UNCERTAIN:CLOCK_REGRESSION",)


def test_status_callback_failure_is_fail_closed_and_latched():
    class BrokenRegistry(Registry):
        def resource_watchdog_targets(self):
            raise OSError("registry unavailable")

    watchdog = ResourceWatchdog(lambda: observation(10), BrokenRegistry([]),
                                policy=policy(), clock_ns=lambda: 10)
    result = watchdog.check_once()
    assert result == watchdog.latest_statuses
    assert watchdog.fatal_status == result[0]
    assert result[0].structural_evidence == ("REGISTRY_STATUS_EXCEPTION:OSError",)
