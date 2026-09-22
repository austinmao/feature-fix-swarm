"""Local, fail-closed resource observations used by shared admission.

This module deliberately reports measurements, not a guessed worker count.  A
caller may turn a measured envelope into reservations only after validating its
age and every field needed by the requested demand.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import os
from pathlib import Path
from pathlib import PurePosixPath
import subprocess
import resource
import sys
import time
from typing import Mapping


class ResourceObservationRefused(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ResourceDemand:
    cpu: int = 1
    memory_bytes: int = 0
    disk_bytes: int = 0
    io_units: int = 0
    processes: int = 1
    provider: str | None = None
    provider_units: int = 0

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 0
            for value in (
                self.cpu,
                self.memory_bytes,
                self.disk_bytes,
                self.io_units,
                self.processes,
                self.provider_units,
            )
        ) or (
            self.provider is not None
            and (not isinstance(self.provider, str) or not self.provider)
        ):
            raise ResourceObservationRefused("RESOURCE_DEMAND_INVALID")

    def record(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ResourceObservation:
    observed_monotonic_ns: int
    cpu_available: int | None
    memory_available_bytes: int | None
    disk_available_bytes: int | None
    io_available_units: int | None
    process_available: int | None
    provider_available: Mapping[str, int] | None = None
    source: str = "local"

    def validate(
        self, *, now_ns: int | None = None, max_age_ns: int = 30_000_000_000
    ) -> None:
        if (
            type(self.observed_monotonic_ns) is not int
            or self.observed_monotonic_ns <= 0
            or type(max_age_ns) is not int
            or max_age_ns <= 0
            or not isinstance(self.source, str)
            or not self.source
        ):
            raise ResourceObservationRefused("RESOURCE_OBSERVATION_INVALID")
        for value in (
            self.cpu_available,
            self.memory_available_bytes,
            self.disk_available_bytes,
            self.io_available_units,
            self.process_available,
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ResourceObservationRefused("RESOURCE_OBSERVATION_INVALID")
        if self.provider_available is not None:
            if not isinstance(self.provider_available, Mapping):
                raise ResourceObservationRefused("RESOURCE_OBSERVATION_INVALID")
            for key, value in self.provider_available.items():
                if (
                    not isinstance(key, str)
                    or not key
                    or type(value) is not int
                    or value < 0
                ):
                    raise ResourceObservationRefused("RESOURCE_OBSERVATION_INVALID")
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        if type(now_ns) is not int or now_ns < self.observed_monotonic_ns:
            raise ResourceObservationRefused("RESOURCE_OBSERVATION_INVALID")
        if now_ns - self.observed_monotonic_ns > max_age_ns:
            raise ResourceObservationRefused("RESOURCE_OBSERVATION_STALE")

    def age_ns(self, now_ns: int | None = None) -> int:
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        return max(0, now_ns - self.observed_monotonic_ns)


def _linux_memory_available() -> int | None:
    try:
        content = Path("/proc/meminfo").read_text(encoding="ascii")
        if not content.endswith("\n"):
            return None
        available = None
        for line in content.splitlines():
            if not line.startswith("MemAvailable:"):
                continue
            fields = line.split()
            if (
                available is not None
                or len(fields) != 3
                or fields[0] != "MemAvailable:"
                or not fields[1].isascii()
                or not fields[1].isdecimal()
                or fields[2] != "kB"
            ):
                return None
            available = int(fields[1]) * 1024
        return available
    except (OSError, UnicodeError, ValueError):
        return None


def _mac_memory_available() -> int | None:
    # vm_stat page counts are an observation, unlike a fabricated RAM quota.
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        result = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=2, check=False
        )
        if (
            result.returncode != 0
            or not isinstance(result.stdout, str)
            or not result.stdout.endswith("\n")
        ):
            return None
        output = result.stdout
        lines = output.splitlines()
        if (
            len(lines) < 4
            or lines[0]
            != "Mach Virtual Memory Statistics: (page size of %d bytes)" % page_size
        ):
            return None
        pages: dict[str, int] = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                value = value.strip()
                if (
                    key.strip() in pages
                    or not value.endswith(".")
                    or not value[:-1].isascii()
                    or not value[:-1].isdecimal()
                ):
                    return None
                pages[key.strip()] = int(value[:-1])
        required = ("Pages free", "Pages inactive", "Pages speculative")
        if not all(key in pages for key in required):
            return None
        return page_size * sum(
            pages[key] for key in required
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _linux_status_real_uid(status: str) -> int | None:
    """Return the real UID from one complete proc status record."""
    if not status.endswith("\n"):
        return None
    matches = [line for line in status.splitlines() if line.startswith("Uid:")]
    if len(matches) != 1:
        return None
    fields = matches[0].split()
    if (
        len(fields) != 5
        or fields[0] != "Uid:"
        or any(not value.isascii() or not value.isdecimal() for value in fields[1:])
    ):
        return None
    return int(fields[1])


def _linux_unambiguous_pid_namespace(status: str, proc_root: Path) -> bool:
    """Require one visible PID namespace and agreement with visible PID 1."""
    if not status.endswith("\n"):
        return False
    matches = [line for line in status.splitlines() if line.startswith("NSpid:")]
    if len(matches) != 1:
        return False
    fields = matches[0].split()
    if (
        len(fields) != 2
        or fields[0] != "NSpid:"
        or not fields[1].isascii()
        or not fields[1].isdecimal()
        or int(fields[1]) <= 0
    ):
        return False
    try:
        own_namespace = os.readlink(proc_root / "self" / "ns" / "pid")
        init_namespace = os.readlink(proc_root / "1" / "ns" / "pid")
    except OSError:
        return False
    return own_namespace == init_namespace and own_namespace.startswith("pid:[") and own_namespace.endswith("]") and own_namespace[5:-1].isdecimal()


def _linux_proc_visibility_verified(proc_root: Path) -> bool:
    """Require explicit PID-namespace and non-hidepid root-proc evidence."""
    try:
        status = (proc_root / "self" / "status").read_text(encoding="ascii")
        if not _linux_unambiguous_pid_namespace(status, proc_root):
            return False
        mountinfo = (proc_root / "self" / "mountinfo").read_text(encoding="ascii")
        if not mountinfo.endswith("\n"):
            return False
        candidates = []
        for line in mountinfo.splitlines():
            fields = line.split()
            try:
                separator = fields.index("-")
            except ValueError:
                return False
            if separator < 6 or len(fields) <= separator + 3:
                return False
            if fields[separator + 1] != "proc":
                continue
            root = _linux_mount_field(fields[3])
            mount = _linux_mount_field(fields[4])
            if root is None or mount is None:
                return False
            if root == "/" and mount == "/proc":
                candidates.append((fields[5], fields[separator + 3]))
        if len(candidates) != 1:
            return False
        options = candidates[0][0].split(",") + candidates[0][1].split(",")
        return not any(
            option == "hidepid"
            or (option.startswith("hidepid=") and option != "hidepid=0")
            for option in options
        )
    except (OSError, UnicodeError, ValueError):
        return False


def _linux_uid_thread_count(proc_root: Path = Path("/proc")) -> int | None:
    """Count every visible thread of the caller's real UID, or refuse it."""
    try:
        if not _linux_proc_visibility_verified(proc_root):
            return None
        uid = os.getuid()
        if type(uid) is not int or uid < 0:
            return None
        total = 0
        for entry in proc_root.iterdir():
            if not entry.name.isascii() or not entry.name.isdecimal() or int(entry.name) <= 0:
                continue
            real_uid = _linux_status_real_uid(
                (entry / "status").read_text(encoding="ascii")
            )
            if real_uid is None:
                return None
            if real_uid != uid:
                continue
            task_entries = list((entry / "task").iterdir())
            if not task_entries or any(
                not task.name.isascii()
                or not task.name.isdecimal()
                or int(task.name) <= 0
                for task in task_entries
            ):
                return None
            total += len(task_entries)
        return total
    except (OSError, UnicodeError, ValueError):
        # A hidden PID, permission error, or process that races its snapshot
        # means UID occupancy is not a trustworthy lower bound.
        return None


def _linux_has_rlimit_exemption(proc_root: Path = Path("/proc")) -> bool | None:
    """Check the real-UID/capability exemptions documented for RLIMIT_NPROC."""
    try:
        uid = os.getuid()
        if type(uid) is not int or uid < 0:
            return None
        if uid == 0:
            return True
        status = (proc_root / "self" / "status").read_text(encoding="ascii")
        if not status.endswith("\n"):
            return None
        matches = [line for line in status.splitlines() if line.startswith("CapEff:")]
        if len(matches) != 1:
            return None
        fields = matches[0].split()
        if len(fields) != 2 or fields[0] != "CapEff:":
            return None
        capabilities = int(fields[1], 16)
        if capabilities < 0 or any(character not in "0123456789abcdefABCDEF" for character in fields[1]):
            return None
        return bool(capabilities & ((1 << 21) | (1 << 24)))
    except (OSError, UnicodeError, ValueError):
        return None


def _linux_cgroup_member_path(content: str) -> PurePosixPath | None:
    if not content.endswith("\n"):
        return None
    records = [line for line in content.splitlines() if line]
    matches = [line for line in records if line.startswith("0::")]
    if len(matches) != 1:
        return None
    value = matches[0][3:]
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or value != str(path)
        or any(part in (".", "..") for part in path.parts)
    ):
        return None
    return path


def _linux_mount_field(value: str) -> str | None:
    """Decode mountinfo's only permitted escaped separators."""
    decoded: list[str] = []
    index = 0
    escapes = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}
    while index < len(value):
        if value[index] != "\\":
            decoded.append(value[index])
            index += 1
            continue
        escape = value[index + 1:index + 4]
        if len(escape) != 3 or escape not in escapes:
            return None
        decoded.append(escapes[escape])
        index += 4
    return "".join(decoded)


def _linux_cgroup_mount(
    mountinfo: str, member: PurePosixPath
) -> tuple[Path, Path] | None:
    if not mountinfo.endswith("\n"):
        return None
    candidates: list[tuple[PurePosixPath, Path, Path]] = []
    for line in mountinfo.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
        except ValueError:
            return None
        if separator < 6 or len(fields) <= separator + 2:
            return None
        if fields[separator + 1] != "cgroup2":
            continue
        root_value = _linux_mount_field(fields[3])
        mount_value = _linux_mount_field(fields[4])
        if root_value is None or mount_value is None:
            return None
        root = PurePosixPath(root_value)
        mount = PurePosixPath(mount_value)
        if (
            root != PurePosixPath("/")
            or
            not root.is_absolute()
            or not mount.is_absolute()
            or root_value != str(root)
            or mount_value != str(mount)
            or any(part in (".", "..") for part in root.parts + mount.parts)
        ):
            return None
        try:
            relative = member.relative_to(root)
        except ValueError:
            continue
        candidates.append(
            (root, Path(str(mount), *relative.parts), Path(str(mount)))
        )
    if not candidates:
        return None
    longest = max(len(root.parts) for root, _target, _mount in candidates)
    targets = {
        (str(target), str(mount))
        for root, target, mount in candidates
        if len(root.parts) == longest
    }
    if len(targets) != 1:
        return None
    target, mount = targets.pop()
    return Path(target), Path(mount)


def _linux_cgroup_process_headroom(proc_root: Path = Path("/proc")) -> tuple[bool, int | None]:
    """Return whether cgroup-v2 was fully observed and its tightest PID bound."""
    try:
        member = _linux_cgroup_member_path(
            (proc_root / "self" / "cgroup").read_text(encoding="ascii")
        )
        mountinfo = (proc_root / "self" / "mountinfo").read_text(encoding="ascii")
        resolved = _linux_cgroup_mount(mountinfo, member) if member is not None else None
        if member is None:
            return False, None
        if resolved is None:
            # Membership advertises v2, so a missing or ambiguous mount is an
            # unreadable constraint rather than evidence of no constraint.
            return False, None
        mount, mountpoint = resolved
        bounds: list[int] = []
        current = mount
        while True:
            maximum_path = current / "pids.max"
            try:
                maximum = maximum_path.read_text(encoding="ascii")
            except FileNotFoundError:
                maximum = None
            if maximum is not None:
                if not maximum.endswith("\n"):
                    return False, None
                value = maximum[:-1]
                if value != "max":
                    if not value.isascii() or not value.isdecimal():
                        return False, None
                    used = (current / "pids.current").read_text(encoding="ascii")
                    if (
                        not used.endswith("\n")
                        or not used[:-1].isascii()
                        or not used[:-1].isdecimal()
                    ):
                        return False, None
                    bounds.append(max(0, int(value) - int(used[:-1])))
            if current == mountpoint:
                break
            parent = current.parent
            if parent == current:
                return False, None
            current = parent
        return True, min(bounds) if bounds else None
    except (OSError, UnicodeError, ValueError):
        return False, None


def _linux_rlimit_process_headroom(proc_root: Path = Path("/proc")) -> tuple[bool, int | None]:
    try:
        exempt = _linux_has_rlimit_exemption(proc_root)
        if exempt is None:
            return False, None
        if exempt:
            return True, None
        soft, _hard = resource.getrlimit(resource.RLIMIT_NPROC)
        if soft == resource.RLIM_INFINITY:
            return True, None
        if type(soft) is not int or soft < 0:
            return False, None
        used = _linux_uid_thread_count(proc_root)
        return (False, None) if used is None else (True, max(0, soft - used))
    except (OSError, ValueError):
        return False, None


def _linux_process_available() -> int | None:
    rlimit_known, rlimit = _linux_rlimit_process_headroom()
    cgroup_known, cgroup = _linux_cgroup_process_headroom()
    if not rlimit_known or not cgroup_known:
        return None
    bounds = [value for value in (rlimit, cgroup) if value is not None]
    return min(bounds) if bounds else None


def _process_available() -> int | None:
    # Darwin counts real-UID processes. Linux RLIMIT_NPROC instead counts
    # threads and has privilege exemptions; never reuse the Darwin arithmetic.
    if sys.platform == "linux":
        return _linux_process_available()
    if sys.platform != "darwin":
        return None
    try:
        soft, _hard = resource.getrlimit(resource.RLIMIT_NPROC)
        limits = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.maxproc", "kern.maxprocperuid"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        process_rows = subprocess.run(
            ["/bin/ps", "-axo", "ruid=,pid="], capture_output=True,
            text=True, timeout=2, check=False,
        )
        if limits.returncode or process_rows.returncode:
            return None
        limit_rows = limits.stdout.splitlines()
        if len(limit_rows) != 2:
            return None
        system_limit, uid_limit = (int(value.strip()) for value in limit_rows)
        rows = [tuple(int(value) for value in line.split())
                for line in process_rows.stdout.splitlines() if line.strip()]
        if (system_limit <= 0 or uid_limit <= 0 or not rows
                or any(len(row) != 2 or not -(1 << 31) <= row[0] < (1 << 32)
                       or row[1] <= 0 for row in rows)
                or len({row[1] for row in rows}) != len(rows)):
            return None
        uid = os.getuid()
        if soft != resource.RLIM_INFINITY:
            if soft < 0:
                return None
            uid_limit = min(uid_limit, soft)
        return max(0, min(system_limit - len(rows),
                          uid_limit - sum((row[0] & 0xffffffff) == uid for row in rows)))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _linux_psi_io_available(content: str) -> int | None:
    """Translate a complete documented PSI sample into a bounded unit."""
    if not content.endswith("\n"):
        return None
    records: dict[str, float] = {}
    for line in content.splitlines():
        fields = line.split()
        if len(fields) != 5 or fields[0] not in ("some", "full") or fields[0] in records:
            return None
        values: dict[str, str] = {}
        for field in fields[1:]:
            if field.count("=") != 1:
                return None
            key, value = field.split("=", 1)
            if key in values:
                return None
            values[key] = value
        if set(values) != {"avg10", "avg60", "avg300", "total"}:
            return None
        try:
            averages = [float(values[key]) for key in ("avg10", "avg60", "avg300")]
            total = int(values["total"])
        except ValueError:
            return None
        if (
            any(not math.isfinite(value) or value < 0 or value > 100 for value in averages)
            or total < 0
            or not values["total"].isascii()
            or not values["total"].isdecimal()
        ):
            return None
        records[fields[0]] = averages[0]
    if set(records) != {"some", "full"}:
        return None
    return max(0, 100 - math.ceil(max(records.values())))


def _io_available_units() -> int | None:
    """Observe only complete Linux PSI; Darwin has no bounded adapter here."""
    try:
        if sys.platform != "linux":
            return None
        return _linux_psi_io_available(
            Path("/proc/pressure/io").read_text(encoding="ascii")
        )
    except (
        OSError,
        UnicodeError,
        ValueError,
        subprocess.SubprocessError,
    ):
        return None


def collect_local_observation(
    *, provider_available: Mapping[str, int] | None = None
) -> ResourceObservation:
    """Collect a fresh Linux/macOS measurement using stdlib/native utilities.

    Linux I/O is reported only from a complete PSI sample.  macOS and every
    unobservable path remain ``None``; requests requiring I/O units then wait
    instead of receiving invented headroom.
    """
    cpu_count = os.cpu_count()
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = None
    cpu_available = None
    if (
        type(cpu_count) is int
        and cpu_count > 0
        and type(load) in (int, float)
        and math.isfinite(load)
        and load >= 0
    ):
        cpu_available = max(0, cpu_count - math.ceil(load))
    try:
        stat = os.statvfs(Path.cwd())
        disk_available = (
            stat.f_bavail * stat.f_frsize
            if type(stat.f_bavail) is int
            and stat.f_bavail >= 0
            and type(stat.f_frsize) is int
            and stat.f_frsize > 0
            else None
        )
    except OSError:
        disk_available = None
    memory = _linux_memory_available() if sys.platform == "linux" else (
        _mac_memory_available() if sys.platform == "darwin" else None
    )
    return ResourceObservation(
        observed_monotonic_ns=time.monotonic_ns(),
        cpu_available=cpu_available,
        memory_available_bytes=memory,
        disk_available_bytes=disk_available,
        io_available_units=_io_available_units(),
        process_available=_process_available(),
        provider_available=dict(provider_available)
        if provider_available is not None
        else None,
        source=("linux" if sys.platform == "linux" else
                "macos" if sys.platform == "darwin" else "unsupported"),
    )
