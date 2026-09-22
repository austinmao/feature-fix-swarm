"""Precise local process identities used by the fixture control authority."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import sys
from dataclasses import dataclass

try:  # filelock deliberately keeps these helpers private; pin the seam here.
    from filelock._identity import process_alive, process_start_token
except ImportError:  # pragma: no cover - supported runtimes provide filelock
    process_alive = None
    process_start_token = None


LIVE = "LIVE"
DEAD = "DEAD"
UNKNOWN = "UNKNOWN"


def _boot_identity() -> str | None:
    if sys.platform.startswith("linux"):
        try:
            value = open("/proc/sys/kernel/random/boot_id", encoding="ascii").read().strip()
        except OSError:
            return None
        return value or None
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
                capture_output=True, text=True, timeout=1, check=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() or None
    return None


def _host_identity(_pid: int) -> str | None:
    """Return a stable machine identity, plus Linux's PID namespace identity."""
    if sys.platform.startswith("linux"):
        machine_id = None
        for source in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
            try:
                machine_id = source.read_text(encoding="ascii").strip()
            except OSError:
                continue
            if machine_id:
                break
        try:
            # Capture the observer's namespace. A target that exits must not
            # erase the namespace component needed to prove its death.
            namespace = os.stat(f"/proc/{os.getpid()}/ns/pid")
        except OSError:
            return None
        if not machine_id:
            return None
        return f"linux:{machine_id}:pidns:{namespace.st_dev}:{namespace.st_ino}"
    if sys.platform == "darwin":
        class Timespec(ctypes.Structure):
            _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

        host_uuid = (ctypes.c_ubyte * 16)()
        timeout = Timespec(1, 0)
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            result = libc.gethostuuid(host_uuid, ctypes.byref(timeout))
        except (AttributeError, OSError):
            return None
        if result != 0:
            return None
        return f"darwin:{bytes(host_uuid).hex()}"
    return None


@dataclass(frozen=True)
class ProcessIdentity:
    host_id: str
    boot_id: str
    pid: int
    start_token: str

    @classmethod
    def current(cls) -> "ProcessIdentity":
        return capture_identity(os.getpid())

    @classmethod
    def from_pid(cls, pid: int) -> "ProcessIdentity":
        return capture_identity(pid)


def capture_identity(pid: int) -> ProcessIdentity:
    """Capture a PID together with the current host boot and precise start token."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("pid must be positive")
    if process_start_token is None:
        raise ProcessLookupError(pid)
    try:
        host_before = _host_identity(pid)
        boot_before = _boot_identity()
        token_before = process_start_token(pid)
        token_after = process_start_token(pid)
        boot_after = _boot_identity()
        host_after = _host_identity(pid)
    except (OSError, ValueError, TypeError) as error:
        raise ProcessLookupError(pid) from error
    if (
        not host_before
        or not boot_before
        or token_before is None
        or token_after is None
        or host_before != host_after
        or boot_before != boot_after
        or token_before != token_after
    ):
        raise ProcessLookupError(pid)
    return ProcessIdentity(host_before, boot_before, pid, str(token_before))


def probe_identity(expected: ProcessIdentity) -> str:
    """Return LIVE only for the exact local process incarnation; otherwise fail closed."""
    if (not isinstance(expected, ProcessIdentity) or isinstance(expected.pid, bool)
            or not isinstance(expected.pid, int) or expected.pid <= 0
            or not all(isinstance(value, str) and value for value in (expected.host_id, expected.boot_id, expected.start_token))):
        return UNKNOWN
    host = _host_identity(os.getpid())
    if not host:
        return UNKNOWN
    boot = _boot_identity()
    if not boot:
        return UNKNOWN
    if expected.host_id != host:
        if sys.platform.startswith("linux"):
            expected_machine = expected.host_id.rsplit(":pidns:", 1)[0]
            observed_machine = host.rsplit(":pidns:", 1)[0]
            if expected_machine == observed_machine and boot != expected.boot_id:
                return DEAD
        return UNKNOWN
    if boot != expected.boot_id:
        return DEAD
    if process_alive is None or process_start_token is None:
        return UNKNOWN
    try:
        if not process_alive(expected.pid):
            return DEAD
        token = process_start_token(expected.pid)
    except OSError:
        return UNKNOWN
    if token is None:
        return UNKNOWN
    return LIVE if str(token) == expected.start_token else DEAD


def probe_direct_parent(child: ProcessIdentity, parent: ProcessIdentity) -> str:
    """Prove that two live identities have a native direct-parent relationship."""
    child_status = probe_identity(child)
    parent_status = probe_identity(parent)
    if child_status != LIVE or parent_status != LIVE:
        return UNKNOWN if UNKNOWN in (child_status, parent_status) else DEAD
    try:
        if sys.platform.startswith("linux"):
            # The command name is parenthesized and may contain spaces or ')'.
            stat_line = Path(f"/proc/{child.pid}/stat").read_text(encoding="ascii")
            fields = stat_line.rsplit(")", 1)[1].strip().split()
            observed_parent = int(fields[1])
        elif sys.platform == "darwin":
            result = subprocess.run(
                ["/bin/ps", "-o", "ppid=", "-p", str(child.pid)],
                capture_output=True, text=True, timeout=1, check=True,
                env={"PATH": "/usr/bin:/bin"},
            )
            observed_parent = int(result.stdout.strip())
        else:
            return UNKNOWN
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return UNKNOWN
    # Recheck both incarnations around the relationship observation.
    if probe_identity(child) != LIVE or probe_identity(parent) != LIVE:
        return UNKNOWN
    return LIVE if observed_parent == parent.pid else DEAD
