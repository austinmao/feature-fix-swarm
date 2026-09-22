"""Bounded, local liveness checks for resource observation.

This module only observes and reports. In particular, the watchdog never
acquires a lease or changes a reservation: a registry integration owns those
authority-bearing actions. Registry callbacks are expected to use the
integration's finite lock timeout.
"""

from __future__ import annotations

from dataclasses import dataclass
import itertools
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import threading
import time
from typing import BinaryIO, Callable, Iterable, Mapping, Protocol

from .resource_observation import ResourceDemand, ResourceObservation


RESOURCE_WAIT = "RESOURCE_WAIT"
CAPABILITY_FAILURE = "CAPABILITY_FAILURE"
HEALTHY = "HEALTHY"

_MAX_TARGETS = 1024
_MAX_COLLECTOR_OUTPUT_BYTES = 16 * 1024
_NANOSECONDS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True)
class ResourceWatchdogPolicy:
    """Versioned, bounded sampling and observation-validation policy."""

    version: str
    sample_interval_ns: int = 5_000_000_000
    freshness_ns: int = 30_000_000_000
    validation_attempts: int = 3
    validation_interval_ns: int = 100_000_000
    clock_uncertainty_ns: int = 0
    collector_timeout_ns: int = 2_000_000_000

    def __post_init__(self) -> None:
        if (
            not isinstance(self.version, str)
            or not self.version
            or type(self.sample_interval_ns) is not int
            or self.sample_interval_ns <= 0
            or type(self.freshness_ns) is not int
            or self.freshness_ns <= 0
            or type(self.validation_attempts) is not int
            or not 1 <= self.validation_attempts <= 32
            or type(self.validation_interval_ns) is not int
            or self.validation_interval_ns < 0
            or type(self.clock_uncertainty_ns) is not int
            or self.clock_uncertainty_ns < 0
            or type(self.collector_timeout_ns) is not int
            or self.collector_timeout_ns <= 0
        ):
            raise ValueError("RESOURCE_WATCHDOG_POLICY_INVALID")


@dataclass(frozen=True)
class WatchdogTarget:
    """A durable queue/lease scope to inspect; this does not authorize it."""

    scope: str
    demand: ResourceDemand
    last_progress_ns: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, str) or not self.scope:
            raise ValueError("RESOURCE_WATCHDOG_TARGET_INVALID")
        if not isinstance(self.demand, ResourceDemand):
            raise ValueError("RESOURCE_WATCHDOG_TARGET_INVALID")
        if self.last_progress_ns is not None and (
            type(self.last_progress_ns) is not int or self.last_progress_ns < 0
        ):
            raise ValueError("RESOURCE_WATCHDOG_TARGET_INVALID")


@dataclass(frozen=True)
class WatchdogStatus:
    """Status suitable for a registry's durable visibility record.

    Clock-failure statuses deliberately contain no invented timeline. The
    optional timestamps let a dispatch consumer distinguish that case from a
    normal resource wait.
    """

    scope: str
    code: str
    limiting_resources: tuple[str, ...]
    last_check_ns: int | None
    last_progress_ns: int | None
    next_check_ns: int | None
    observation_age_ns: int | None
    policy_version: str
    structural_evidence: tuple[str, ...]


class ResourceWatchdogRegistry(Protocol):
    """Typed, finite-time hooks owned by the registry/supervisor layer."""

    def resource_watchdog_targets(self) -> Iterable[WatchdogTarget]:
        """Return a finite snapshot without retaining a writer lock."""

    def persist_resource_watchdog_status(self, status: WatchdogStatus) -> None:
        """Persist one computed status with the integration's finite lock timeout."""


ObservationCollector = Callable[[], ResourceObservation | None]
Clock = Callable[[], int]


class CollectorChild(Protocol):
    """The small supervised-child surface used by the bounded collector."""

    stdout: BinaryIO | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


CollectorTransport = Callable[[], CollectorChild]


class LocalObservationCollectorError(RuntimeError):
    """A bounded fixed-child collector refusal with a stable evidence code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_LOCAL_FIELDS = (
    ("cpu", "cpu_available"),
    ("memory_bytes", "memory_available_bytes"),
    ("disk_bytes", "disk_available_bytes"),
    ("io_units", "io_available_units"),
    ("processes", "process_available"),
)

_CHILD_PROGRAM = """
import json
import sys
sys.path.insert(0, sys.argv[1])
from run_state.resource_observation import collect_local_observation
value = collect_local_observation()
record = {
    \"observed_monotonic_ns\": value.observed_monotonic_ns,
    \"cpu_available\": value.cpu_available,
    \"memory_available_bytes\": value.memory_available_bytes,
    \"disk_available_bytes\": value.disk_available_bytes,
    \"io_available_units\": value.io_available_units,
    \"process_available\": value.process_available,
    \"provider_available\": value.provider_available,
    \"source\": value.source,
}
sys.stdout.write(json.dumps(record, separators=(\",\", \":\"), sort_keys=True))
"""


class _PosixSpawnChild:
    """A reaped child handle created without forking this Python process."""

    def __init__(self, pid: int, stdout: BinaryIO) -> None:
        self.pid = pid
        self.stdout: BinaryIO | None = stdout
        self._returncode: int | None = None

    def poll(self) -> int | None:
        if self._returncode is not None:
            return self._returncode
        waited_pid, status = os.waitpid(self.pid, os.WNOHANG)
        if waited_pid == 0:
            return None
        self._returncode = os.waitstatus_to_exitcode(status)
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        if self._returncode is not None:
            return self._returncode
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("fixed-local-collector", timeout)
            time.sleep(0.001)
        return self._returncode

    def kill(self) -> None:
        if self.poll() is None:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _fixed_local_collector_transport() -> CollectorChild:
    """Start the one permitted child command with a closed environment.

    There is no shell, command argument, provider argument, or model command
    in this transport. ``-I`` ignores ambient Python configuration; the
    package directory is supplied as a fixed positional child argument.
    """
    package_root = str(Path(__file__).resolve().parents[1])
    executable = str(Path(sys.executable).resolve())
    if not hasattr(os, "posix_spawn"):
        raise LocalObservationCollectorError("COLLECTOR_TRANSPORT_UNAVAILABLE")
    read_fd, write_fd = os.pipe()
    null_fd = os.open(os.devnull, os.O_RDWR)
    try:
        actions = [
            (os.POSIX_SPAWN_DUP2, null_fd, 0),
            (os.POSIX_SPAWN_DUP2, write_fd, 1),
            (os.POSIX_SPAWN_DUP2, null_fd, 2),
            (os.POSIX_SPAWN_CLOSE, read_fd),
            (os.POSIX_SPAWN_CLOSE, write_fd),
            (os.POSIX_SPAWN_CLOSE, null_fd),
        ]
        pid = os.posix_spawn(
            executable, (executable, "-I", "-S", "-c", _CHILD_PROGRAM, package_root),
            {"PATH": os.defpath, "LC_ALL": "C", "PYTHONIOENCODING": "utf-8"},
            file_actions=actions, setpgroup=0,
        )
    except Exception:
        os.close(read_fd)
        raise
    finally:
        os.close(write_fd)
        os.close(null_fd)
    return _PosixSpawnChild(pid, os.fdopen(read_fd, "rb", buffering=0))


def _decode_child_observation(payload: bytes) -> ResourceObservation:
    try:
        if not payload or len(payload) > _MAX_COLLECTOR_OUTPUT_BYTES:
            raise ValueError
        record = json.loads(payload.decode("utf-8"))
        if not isinstance(record, dict) or set(record) != {
            "observed_monotonic_ns", "cpu_available", "memory_available_bytes",
            "disk_available_bytes", "io_available_units", "process_available",
            "provider_available", "source",
        }:
            raise ValueError
        providers = record["provider_available"]
        if providers is not None and not isinstance(providers, dict):
            raise ValueError
        observation = ResourceObservation(
            record["observed_monotonic_ns"], record["cpu_available"],
            record["memory_available_bytes"], record["disk_available_bytes"],
            record["io_available_units"], record["process_available"], providers,
            record["source"],
        )
        observation.validate(
            now_ns=observation.observed_monotonic_ns, max_age_ns=1
        )
        return observation
    except (TypeError, ValueError, UnicodeError):
        raise LocalObservationCollectorError("COLLECTOR_SERIALIZATION_INVALID") from None


class LocalObservationCollector:
    """Production collector with a total wall deadline and no orphan child.

    It runs only ``collect_local_observation`` in a fixed, isolated subprocess.
    Injected transports exist solely for deterministic fixture tests. This is
    intentionally distinct from the watchdog's injected callback seam: custom
    callbacks *must* themselves be bounded by ``collector_timeout_ns``.
    """

    def __init__(
        self, *, timeout_ns: int = 2_000_000_000,
        _transport: CollectorTransport | None = None,
    ) -> None:
        if type(timeout_ns) is not int or timeout_ns <= 0:
            raise ValueError("RESOURCE_WATCHDOG_COLLECTOR_INVALID")
        self._timeout_ns = timeout_ns
        self._transport = _fixed_local_collector_transport if _transport is None else _transport
        self._unsettled_child: CollectorChild | None = None
        self._collection_lock = threading.Lock()

    def __call__(self) -> ResourceObservation:
        # Admission and its independent watchdog share this collector. Never
        # race its retained-child fence or allow an unbounded lock wait.
        if not self._collection_lock.acquire(timeout=self._timeout_ns / _NANOSECONDS_PER_SECOND):
            raise LocalObservationCollectorError('COLLECTOR_BUSY')
        try:
            return self._collect()
        finally:
            self._collection_lock.release()

    def _collect(self) -> ResourceObservation:
        if self._unsettled_child is not None:
            if self._unsettled_child.poll() is None:
                raise LocalObservationCollectorError('COLLECTOR_CHILD_UNSETTLED')
            self._unsettled_child = None
        started_ns = time.monotonic_ns()
        process: CollectorChild | None = None
        try:
            process = self._transport()
            if process.stdout is None:
                raise LocalObservationCollectorError("COLLECTOR_TRANSPORT_INVALID")
            payload = self._read_bounded(process, started_ns)
            if process.wait(timeout=self._remaining_seconds(started_ns)) != 0:
                raise LocalObservationCollectorError("COLLECTOR_CHILD_FAILED")
            return _decode_child_observation(payload)
        except LocalObservationCollectorError:
            raise
        except (OSError, subprocess.SubprocessError) as error:
            raise LocalObservationCollectorError(
                "COLLECTOR_TRANSPORT_EXCEPTION:" + type(error).__name__
            ) from None
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=1)
                except subprocess.SubprocessError:
                    self._unsettled_child = process
                    raise LocalObservationCollectorError('COLLECTOR_CHILD_UNSETTLED') from None
            if process is not None and process.stdout is not None:
                process.stdout.close()

    def _remaining_seconds(self, started_ns: int) -> float:
        remaining_ns = self._timeout_ns - (time.monotonic_ns() - started_ns)
        if remaining_ns <= 0:
            raise LocalObservationCollectorError("COLLECTOR_DEADLINE_EXCEEDED")
        return remaining_ns / _NANOSECONDS_PER_SECOND

    def _read_bounded(self, process: CollectorChild, started_ns: int) -> bytes:
        assert process.stdout is not None
        descriptor = process.stdout.fileno()
        payload = bytearray()
        selector = selectors.DefaultSelector()
        try:
            os.set_blocking(descriptor, False)
            selector.register(descriptor, selectors.EVENT_READ)
            while True:
                events = selector.select(self._remaining_seconds(started_ns))
                if not events:
                    raise LocalObservationCollectorError("COLLECTOR_DEADLINE_EXCEEDED")
                chunk = os.read(descriptor, min(4096, _MAX_COLLECTOR_OUTPUT_BYTES + 1))
                if not chunk:
                    return bytes(payload)
                payload.extend(chunk)
                if len(payload) > _MAX_COLLECTOR_OUTPUT_BYTES:
                    raise LocalObservationCollectorError("COLLECTOR_OUTPUT_TOO_LARGE")
        except OSError as error:
            raise LocalObservationCollectorError(
                "COLLECTOR_TRANSPORT_EXCEPTION:" + type(error).__name__
            ) from None
        finally:
            selector.close()


def local_observation_collector(*, timeout_ns: int = 2_000_000_000) -> ObservationCollector:
    """Return the bounded production collector offered to watchdog integrators."""
    return LocalObservationCollector(timeout_ns=timeout_ns)


class ResourceWatchdog:
    """Independent, stoppable observer with no dispatch-side authority effects."""

    def __init__(
        self,
        collector: ObservationCollector | None = None,
        registry: ResourceWatchdogRegistry | None = None,
        *,
        policy: ResourceWatchdogPolicy,
        clock_ns: Clock = time.monotonic_ns,
    ) -> None:
        if registry is None or not callable(clock_ns):
            raise ValueError("RESOURCE_WATCHDOG_CALLBACK_INVALID")
        if collector is not None and not callable(collector):
            raise ValueError("RESOURCE_WATCHDOG_CALLBACK_INVALID")
        self._collector = (
            local_observation_collector(timeout_ns=policy.collector_timeout_ns)
            if collector is None else collector
        )
        self._registry = registry
        self._policy = policy
        self._clock_ns = clock_ns
        self._stop = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._clock_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_clock_ns: int | None = None
        self._latest_statuses: tuple[WatchdogStatus, ...] = ()
        self._fatal_status: WatchdogStatus | None = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def latest_statuses(self) -> tuple[WatchdogStatus, ...]:
        """A thread-safe latest result snapshot for a dispatch consumer."""
        with self._status_lock:
            return self._latest_statuses

    @property
    def fatal_status(self) -> WatchdogStatus | None:
        """The current fail-closed result, if the most recent check has one."""
        with self._status_lock:
            return self._fatal_status

    def start(self) -> None:
        """Start one non-daemon worker. Calling it twice never spawns a twin."""
        with self._lifecycle_lock:
            if self.running:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="resource-watchdog", daemon=False
            )
            self._thread.start()

    def stop(self, timeout: float | None = None) -> bool:
        """Signal and join; a contract-violating injected callback is latched."""
        self._stop.set()
        thread = self._thread
        if thread is None or thread is threading.current_thread():
            return True
        thread.join(timeout)
        stopped = not thread.is_alive()
        if not stopped:
            self._publish((self._callback_failure(
                "resource-watchdog", None, "WATCHDOG_STOP_TIMEOUT"
            ),))
        return stopped

    def _run(self) -> None:
        while not self._stop.is_set():
            self.check_once()
            self._stop.wait(self._policy.sample_interval_ns / _NANOSECONDS_PER_SECOND)

    def _safe_now(self) -> tuple[int | None, str | None]:
        try:
            value = self._clock_ns()
        except Exception as error:
            return None, "RESOURCE_CLOCK_UNCERTAIN:" + type(error).__name__
        if type(value) is not int or value < 0:
            return None, "RESOURCE_CLOCK_UNCERTAIN:INVALID_CLOCK"
        with self._clock_lock:
            if self._last_clock_ns is not None and value < self._last_clock_ns:
                return None, "RESOURCE_CLOCK_UNCERTAIN:CLOCK_REGRESSION"
            self._last_clock_ns = value
        return value, None

    def _publish(self, statuses: tuple[WatchdogStatus, ...]) -> tuple[WatchdogStatus, ...]:
        with self._status_lock:
            self._latest_statuses = statuses
            self._fatal_status = next(
                (status for status in statuses if status.code == CAPABILITY_FAILURE), None
            )
        return statuses

    @staticmethod
    def _required_local(target: WatchdogTarget) -> tuple[tuple[str, str], ...]:
        return tuple(
            (demand_name, observation_name)
            for demand_name, observation_name in _LOCAL_FIELDS
            if getattr(target.demand, demand_name) > 0
        )

    def _inspect(
        self, observation: ResourceObservation | None, target: WatchdogTarget, now_ns: int
    ) -> tuple[bool, tuple[str, ...], tuple[str, ...], int | None]:
        """Return validated, evidence, limiting fields, and age for one sample."""
        required = self._required_local(target)
        if not required:
            return True, (), (), None
        if observation is None:
            return False, ("COLLECTOR_RETURNED_NONE",), (), None
        if not isinstance(observation, ResourceObservation):
            return False, ("COLLECTOR_INVALID_TYPE",), (), None
        if (
            type(observation.observed_monotonic_ns) is not int
            or observation.observed_monotonic_ns <= 0
            or not isinstance(observation.source, str)
            or not observation.source
        ):
            return False, ("RESOURCE_OBSERVATION_INVALID",), (), None
        delta = now_ns - observation.observed_monotonic_ns
        if delta < -self._policy.clock_uncertainty_ns:
            return False, ("RESOURCE_CLOCK_UNCERTAIN",), (), None
        age_ns = max(0, delta)
        if age_ns > self._policy.freshness_ns + self._policy.clock_uncertainty_ns:
            return False, ("RESOURCE_OBSERVATION_STALE",), (), age_ns
        invalid: list[str] = []
        limiting: list[str] = []
        for demand_name, observation_name in required:
            value = getattr(observation, observation_name)
            if type(value) is not int or value < 0:
                invalid.append(demand_name)
            elif value < getattr(target.demand, demand_name):
                limiting.append(demand_name)
        if invalid:
            return False, tuple("REQUIRED_" + name.upper() + "_INVALID" for name in invalid), (), age_ns
        return True, (), tuple(limiting), age_ns

    def _sample_targets(
        self, targets: tuple[WatchdogTarget, ...]
    ) -> tuple[
        dict[int, tuple[tuple[str, ...], int | None, int | None]],
        dict[int, tuple[tuple[str, ...], int | None]],
        str | None,
    ]:
        pending = set(range(len(targets)))
        resolved: dict[int, tuple[tuple[str, ...], int | None, int | None]] = {}
        failed: dict[int, tuple[tuple[str, ...], int | None]] = {}
        for attempt in range(self._policy.validation_attempts):
            if self._stop.is_set():
                break
            try:
                observation = self._collector()
            except LocalObservationCollectorError as error:
                observation = None
                error_evidence = error.code
            except Exception as error:
                observation = None
                error_evidence = "COLLECTOR_EXCEPTION:" + type(error).__name__
            else:
                error_evidence = "COLLECTOR_RETURNED_NONE"
            sample_now, clock_error = self._safe_now()
            if clock_error is not None or sample_now is None:
                return resolved, failed, clock_error
            for index in tuple(pending):
                valid, evidence, limiting, age = self._inspect(observation, targets[index], sample_now)
                if valid:
                    provider_available = None
                    provider = targets[index].demand.provider
                    if provider is not None and observation is not None:
                        values = observation.provider_available
                        candidate = values.get(provider) if values is not None else None
                        if type(candidate) is int and candidate >= 0:
                            provider_available = candidate
                    resolved[index] = (limiting, age, provider_available)
                    pending.remove(index)
                else:
                    failed[index] = ((error_evidence,) if observation is None else evidence, age)
            if not pending:
                break
            if attempt + 1 < self._policy.validation_attempts:
                self._stop.wait(self._policy.validation_interval_ns / _NANOSECONDS_PER_SECOND)
        return resolved, failed, None

    def _callback_failure(
        self, scope: str, now_ns: int | None, evidence: str
    ) -> WatchdogStatus:
        return WatchdogStatus(
            scope, CAPABILITY_FAILURE, (), now_ns, None,
            None if now_ns is None else now_ns + self._policy.sample_interval_ns,
            None, self._policy.version, (evidence,),
        )

    def _clock_failures(
        self, targets: tuple[WatchdogTarget, ...], evidence: str
    ) -> tuple[WatchdogStatus, ...]:
        if not targets:
            return (self._callback_failure("resource-watchdog", None, evidence),)
        return tuple(self._callback_failure(target.scope, None, evidence) for target in targets)

    def _statuses(
        self,
        targets: tuple[WatchdogTarget, ...],
        resolved: Mapping[int, tuple[tuple[str, ...], int | None, int | None]],
        failed: Mapping[int, tuple[tuple[str, ...], int | None]],
        check_ns: int,
    ) -> tuple[WatchdogStatus, ...]:
        statuses: list[WatchdogStatus] = []
        for index, target in enumerate(targets):
            if index in resolved:
                limiting, age, provider_available = resolved[index]
                provider = target.demand.provider
                provider_unknown = (
                    provider is not None and target.demand.provider_units > 0
                    and provider_available is None
                )
                provider_limited = (
                    provider is not None and provider_available is not None
                    and provider_available < target.demand.provider_units
                )
                status_code = RESOURCE_WAIT if limiting or provider_unknown or provider_limited else HEALTHY
                status_limiting = limiting + (
                    ("provider:" + provider,) if provider_unknown or provider_limited else ()
                )
                evidence = ("PROVIDER_EXPLORATION",) if provider_unknown else ()
            else:
                status_code = CAPABILITY_FAILURE
                status_limiting, (evidence, age) = (), failed.get(
                    index, (("WATCHDOG_STOPPED_DURING_VALIDATION",), None)
                )
            statuses.append(WatchdogStatus(
                target.scope, status_code, status_limiting, check_ns,
                target.last_progress_ns, check_ns + self._policy.sample_interval_ns,
                age, self._policy.version, evidence,
            ))
        return tuple(statuses)

    def check_once(self) -> tuple[WatchdogStatus, ...]:
        """Sample at most ``validation_attempts`` times and publish all outcomes."""
        now_ns, clock_error = self._safe_now()
        if clock_error is not None or now_ns is None:
            return self._publish(self._clock_failures((), clock_error or "RESOURCE_CLOCK_UNCERTAIN"))
        try:
            targets = tuple(itertools.islice(self._registry.resource_watchdog_targets(), _MAX_TARGETS + 1))
            if len(targets) > _MAX_TARGETS or any(
                not isinstance(target, WatchdogTarget) for target in targets
            ):
                raise ValueError("TARGET_SNAPSHOT_INVALID")
            if len({target.scope for target in targets}) != len(targets):
                raise ValueError("TARGET_SNAPSHOT_INVALID")
        except Exception as error:
            return self._publish((self._callback_failure(
                "resource-watchdog", now_ns,
                "REGISTRY_STATUS_EXCEPTION:" + type(error).__name__,
            ),))
        resolved, failed, clock_error = self._sample_targets(targets)
        if clock_error is not None:
            return self._publish(self._clock_failures(targets, clock_error))
        check_ns, clock_error = self._safe_now()
        if clock_error is not None or check_ns is None:
            return self._publish(self._clock_failures(targets, clock_error or "RESOURCE_CLOCK_UNCERTAIN"))
        statuses = self._statuses(targets, resolved, failed, check_ns)
        delivered: list[WatchdogStatus] = []
        for status in statuses:
            try:
                self._registry.persist_resource_watchdog_status(status)
            except Exception as error:
                delivered.append(WatchdogStatus(
                    status.scope, CAPABILITY_FAILURE, (), status.last_check_ns,
                    status.last_progress_ns, status.next_check_ns,
                    status.observation_age_ns, status.policy_version,
                    ("REGISTRY_PERSIST_EXCEPTION:" + type(error).__name__,),
                ))
            else:
                delivered.append(status)
        return self._publish(tuple(delivered))
