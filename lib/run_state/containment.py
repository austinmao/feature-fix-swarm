"""Contain only the verified session created for an admitted native child."""
from __future__ import annotations

from dataclasses import asdict
import os
import signal
import subprocess
import time

from process_identity import DEAD, LIVE, ProcessIdentity, probe_identity


def contain_session(identity: ProcessIdentity, *, grace_seconds: float = 1.0) -> dict:
    """Return explicit uncertainty when the original session cannot be proven.

    A dead leader alone is not proof that its descendants disappeared.  Once
    the live session is observed, every member is retained by full identity so
    escalation cannot target a recycled PID or unrelated process group.
    """
    report = {"leader": asdict(identity), "members": [], "status": "uncertain"}
    if probe_identity(identity) != LIVE:
        return report
    try:
        if os.getpgid(identity.pid) != identity.pid or os.getsid(identity.pid) != identity.pid:
            return report
        rows = subprocess.run(["/bin/ps", "-axo", "pid=,pgid="],
            capture_output=True, text=True, check=True, timeout=2,
            env={"PATH": "/usr/bin:/bin"}).stdout.splitlines()
        members = []
        for row in rows:
            pid, group = map(int, row.split())
            if group != identity.pid:
                continue
            try:
                member = ProcessIdentity.from_pid(pid)
                status = probe_identity(member)
                if status == DEAD:
                    continue
                if status != LIVE or os.getsid(pid) != identity.pid:
                    report.setdefault("unverified_members", []).append(pid)
                    continue
            except ProcessLookupError:
                continue
            members.append(member)
        if identity not in members or probe_identity(identity) != LIVE:
            return report
        report["members"] = [asdict(member) for member in members]
        # Recheck group/session at the signal boundary; never PID-only fallback.
        if os.getpgid(identity.pid) != identity.pid or os.getsid(identity.pid) != identity.pid:
            return report
        # Signal only individually verified members; an unknown or exiting
        # sibling must not prevent containment of the known live children.
        for member in members:
            if probe_identity(member) == LIVE:
                try:
                    os.kill(member.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + max(0.0, grace_seconds)
        while time.monotonic() < deadline and any(probe_identity(member) == LIVE for member in members):
            time.sleep(.02)
        for member in members:
            if probe_identity(member) == LIVE:
                try:
                    os.kill(member.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        remaining = subprocess.run(["/bin/ps", "-axo", "pid=,pgid="],
            capture_output=True, text=True, check=True, timeout=2,
            env={"PATH": "/usr/bin:/bin"}).stdout.splitlines()
        live_group = False
        for row in remaining:
            pid, group = map(int, row.split())
            if group == identity.pid:
                try:
                    live_group |= probe_identity(ProcessIdentity.from_pid(pid)) != DEAD
                except ProcessLookupError:
                    pass
        report["status"] = "terminated" if not report.get("unverified_members") and not live_group and all(
            probe_identity(member) == DEAD for member in members) else "reaping_pending"
        return report
    except (OSError, ValueError, subprocess.SubprocessError):
        return report
