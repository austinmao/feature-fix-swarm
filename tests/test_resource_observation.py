from __future__ import annotations

import errno
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest


from run_state.resource_observation import (
    ResourceDemand,
    ResourceObservation,
)
from run_state.resource_scheduler import ResourceScheduler


def _observation(**changes):
    value = dict(
        observed_monotonic_ns=time.monotonic_ns(),
        cpu_available=4,
        memory_available_bytes=4096,
        disk_available_bytes=4096,
        io_available_units=2,
        process_available=8,
        provider_available={"model": 2},
    )
    value.update(changes)
    return ResourceObservation(**value)


def test_stale_and_structurally_invalid_observations_wait_closed():
    scheduler = ResourceScheduler(max_age_ns=10)
    demand = ResourceDemand(cpu=1)
    stale = _observation(observed_monotonic_ns=time.monotonic_ns() - 11)
    assert scheduler.decide(demand, [], stale).code == "RESOURCE_OBSERVATION_STALE"
    invalid = _observation(cpu_available=-1)
    assert scheduler.decide(demand, [], invalid).code == "RESOURCE_OBSERVATION_INVALID"


def test_healthy_saturation_is_resource_wait_and_rechecks():
    scheduler = ResourceScheduler(cooldown_ns=5_000_000)
    active = [
        {
            "demand_json": '{"cpu":4,"memory_bytes":0,"disk_bytes":0,"io_units":0,"processes":0,"provider":null,"provider_units":0}'
        }
    ]
    decision = scheduler.decide(ResourceDemand(cpu=1), active, _observation())
    assert (decision.admitted, decision.code, decision.limiting_resource) == (
        False,
        "RESOURCE_WAIT",
        "cpu",
    )
    assert decision.next_recheck_ns > time.monotonic_ns()


def test_unknown_provider_is_not_invented_and_recovery_admits():
    scheduler = ResourceScheduler()
    demand = ResourceDemand(cpu=0, processes=0, provider="model", provider_units=1)
    assert (
        scheduler.decide(demand, [], _observation(provider_available=None)).code
        == "RESOURCE_WAIT"
    )
    assert scheduler.decide(demand, [], _observation()).admitted


@pytest.mark.parametrize("limits,rows,soft,expected", [
    ("10\n6\n", "502 1\n502 2\n0 3\n", 4, 2),
    ("10\n6\n", "502 1\n502 2\n-2 3\n", 4, 2),
    ("3\n6\n", "502 1\n502 2\n0 3\n", 4, 0),
    ("10\n6\n", "502 1\n502 1\n", 4, None),
    ("10\n6\n", "malformed\n", 4, None),
    ("10\n", "502 1\n", 4, None),
])
def test_darwin_process_headroom_uses_observed_limits_and_real_uid(
    monkeypatch, limits, rows, soft, expected,
):
    from run_state import resource_observation as observation
    monkeypatch.setattr(observation.sys, "platform", "darwin")
    monkeypatch.setattr(observation.os, "getuid", lambda: 502)
    monkeypatch.setattr(observation.resource, "getrlimit", lambda _kind: (soft, 100))
    monkeypatch.setattr(observation.subprocess, "run", lambda argv, **_kwargs:
                        SimpleNamespace(returncode=0, stdout=limits if "sysctl" in argv[0] else rows))
    assert observation._process_available() == expected


def _write_status(root, pid, uid, threads):
    process = root / str(pid)
    (process / "task").mkdir(parents=True)
    (process / "status").write_text(
        "Name:\tfixture\nUid:\t%d\t%d\t%d\t%d\n" % ((uid,) * 4),
        encoding="ascii",
    )
    for thread in threads:
        (process / "task" / str(thread)).mkdir()


def _write_cgroup_fixture(proc_root, mount, member="/parent/child"):
    (proc_root / "self").mkdir(parents=True)
    (proc_root / "self" / "cgroup").write_text("0::%s\n" % member, encoding="ascii")
    (proc_root / "self" / "mountinfo").write_text(
        "36 25 0:32 / %s rw - cgroup2 cgroup rw\n" % mount,
        encoding="ascii",
    )


def _write_proc_visibility_fixture(proc_root):
    _write_status(proc_root, 1, 0, (1,))
    (proc_root / "self" / "ns").mkdir()
    (proc_root / "1" / "ns").mkdir()
    (proc_root / "self" / "ns" / "pid").symlink_to("pid:[4026531836]")
    (proc_root / "1" / "ns" / "pid").symlink_to("pid:[4026531836]")
    (proc_root / "self" / "mountinfo").write_text(
        "1 0 0:1 / /proc rw - proc proc rw\n", encoding="ascii"
    )


def test_linux_rlimit_counts_real_uid_threads_and_honors_exemptions(
    tmp_path, monkeypatch
):
    from run_state import resource_observation as observation

    proc_root = tmp_path / "proc"
    _write_status(proc_root, 101, 42, (101, 102))
    _write_status(proc_root, 202, 43, (202, 203, 204))
    (proc_root / "self").mkdir()
    _write_proc_visibility_fixture(proc_root)
    (proc_root / "self" / "status").write_text(
        "CapEff:\t0000000000000000\nNSpid:\t101\n", encoding="ascii"
    )
    monkeypatch.setattr(observation.os, "getuid", lambda: 42)
    monkeypatch.setattr(
        observation.resource, "getrlimit", lambda _kind: (5, 5)
    )

    assert observation._linux_uid_thread_count(proc_root) == 2
    assert observation._linux_rlimit_process_headroom(proc_root) == (True, 3)

    (proc_root / "self" / "status").write_text(
        "CapEff:\t0000000000200000\nNSpid:\t101\n", encoding="ascii"
    )
    assert observation._linux_rlimit_process_headroom(proc_root) == (True, None)


def test_linux_process_collection_refuses_hidden_or_malformed_proc_data(
    tmp_path, monkeypatch
):
    from run_state import resource_observation as observation

    proc_root = tmp_path / "proc"
    _write_status(proc_root, 101, 42, (101,))
    (proc_root / "self").mkdir()
    _write_proc_visibility_fixture(proc_root)
    (proc_root / "self" / "status").write_text(
        "CapEff:\t0000000000000000\nNSpid:\t101\n", encoding="ascii"
    )
    monkeypatch.setattr(observation.os, "getuid", lambda: 42)
    monkeypatch.setattr(
        observation.resource, "getrlimit", lambda _kind: (5, 5)
    )
    (proc_root / "self" / "mountinfo").write_text(
        "1 0 0:1 / /proc rw,hidepid=2 - proc proc rw\n", encoding="ascii"
    )
    assert observation._linux_rlimit_process_headroom(proc_root) == (False, None)
    (proc_root / "self" / "mountinfo").write_text(
        "1 0 0:1 / /proc rw - proc proc rw\n", encoding="ascii"
    )
    (proc_root / "self" / "status").write_text(
        "CapEff:\t0000000000000000\nNSpid:\t22\t101\n", encoding="ascii"
    )
    assert observation._linux_rlimit_process_headroom(proc_root) == (False, None)
    (proc_root / "self" / "status").write_text(
        "CapEff:\t0000000000000000\nNSpid:\t101\n", encoding="ascii"
    )
    (proc_root / "101" / "status").write_text("Uid:\t42\t42", encoding="ascii")
    assert observation._linux_rlimit_process_headroom(proc_root) == (False, None)

    monkeypatch.setattr(observation.os, "getuid", lambda: 0)
    assert observation._linux_rlimit_process_headroom(proc_root) == (True, None)


def test_unprivileged_pid_namespace_proof_needs_no_ptrace_access_to_pid_1(
    tmp_path, monkeypatch
):
    from run_state import resource_observation as observation

    proc_root = tmp_path / "proc"
    _write_status(proc_root, 101, 42, (101, 102))
    (proc_root / "self").mkdir()
    _write_proc_visibility_fixture(proc_root)
    (proc_root / "self" / "status").write_text(
        "CapEff:\t0000000000000000\nNSpid:\t101\n", encoding="ascii"
    )
    real_readlink = os.readlink

    def ptrace_guarded_readlink(path, *args, **kwargs):
        # The kernel answers EACCES for root-owned /proc/1/ns/* to non-root.
        if Path(path) == proc_root / "1" / "ns" / "pid":
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_readlink(path, *args, **kwargs)

    monkeypatch.setattr(observation.os, "readlink", ptrace_guarded_readlink)
    monkeypatch.setattr(observation.os, "getuid", lambda: 42)
    monkeypatch.setattr(observation.resource, "getrlimit", lambda _kind: (5, 5))
    assert observation._linux_proc_visibility_verified(proc_root) is True
    assert observation._linux_rlimit_process_headroom(proc_root) == (True, 3)

    # The caller's own namespace link must still be present and well formed.
    (proc_root / "self" / "ns" / "pid").unlink()
    (proc_root / "self" / "ns" / "pid").symlink_to("pid:[not-a-number]")
    assert observation._linux_rlimit_process_headroom(proc_root) == (False, None)

    # A nested namespace (a container) cannot see the UID's threads elsewhere,
    # so its count would undercount RLIMIT_NPROC usage: unknown, not headroom.
    (proc_root / "self" / "ns" / "pid").unlink()
    (proc_root / "self" / "ns" / "pid").symlink_to("pid:[4026532001]")
    assert observation._linux_rlimit_process_headroom(proc_root) == (False, None)


def _write_unlimited_linux_fixture(proc_root, mount, monkeypatch):
    from run_state import resource_observation as observation

    _write_cgroup_fixture(proc_root, mount)
    (mount / "parent" / "child").mkdir(parents=True)
    (mount / "parent" / "child" / "pids.max").write_text("max\n", encoding="ascii")
    (proc_root / "self" / "status").write_text(
        "CapEff:\t0000000000000000\nNSpid:\t101\n", encoding="ascii"
    )
    monkeypatch.setattr(observation.os, "getuid", lambda: 42)
    infinity = observation.resource.RLIM_INFINITY
    monkeypatch.setattr(
        observation.resource, "getrlimit", lambda _kind: (infinity, infinity)
    )


def test_linux_unlimited_uid_and_cgroup_use_measured_kernel_thread_cap(
    tmp_path, monkeypatch
):
    from run_state import resource_observation as observation

    proc_root = tmp_path / "proc"
    _write_unlimited_linux_fixture(proc_root, tmp_path / "cgroup", monkeypatch)
    # Neither per-UID nor cgroup limits exist and the kernel cap is unreadable:
    # still unknown, never invented headroom.
    assert observation._linux_process_available(proc_root) is None

    (proc_root / "sys" / "kernel").mkdir(parents=True)
    (proc_root / "sys" / "kernel" / "threads-max").write_text("1000\n", encoding="ascii")
    (proc_root / "sys" / "kernel" / "pid_max").write_text("4194304\n", encoding="ascii")
    (proc_root / "loadavg").write_text("0.50 0.40 0.30 3/234 9999\n", encoding="ascii")
    assert observation._linux_process_available(proc_root) == 766
    # pid_max loses the kernel's reserved low PIDs: min(1000, 800 - 300) - 234.
    (proc_root / "sys" / "kernel" / "pid_max").write_text("800\n", encoding="ascii")
    assert observation._linux_process_available(proc_root) == 266

    (proc_root / "sys" / "kernel" / "pid_max").write_text("200\n", encoding="ascii")
    assert observation._linux_process_available(proc_root) == 0
    (proc_root / "loadavg").write_text("0.50 0.40 0.30 3/234 9999", encoding="ascii")
    assert observation._linux_process_available(proc_root) is None


def test_linux_cgroup_headroom_uses_the_tightest_ancestor_limit(tmp_path):
    from run_state import resource_observation as observation

    proc_root = tmp_path / "proc"
    mount = tmp_path / "cgroup"
    _write_cgroup_fixture(proc_root, mount)
    child = mount / "parent" / "child"
    parent = mount / "parent"
    child.mkdir(parents=True)
    (child / "pids.max").write_text("10\n", encoding="ascii")
    (child / "pids.current").write_text("8\n", encoding="ascii")
    (parent / "pids.max").write_text("20\n", encoding="ascii")
    (parent / "pids.current").write_text("4\n", encoding="ascii")

    assert observation._linux_cgroup_process_headroom(proc_root) == (True, 2)

    (parent / "pids.current").write_text("4", encoding="ascii")
    assert observation._linux_cgroup_process_headroom(proc_root) == (False, None)


def test_linux_cgroup_ambiguous_mount_or_malformed_membership_is_unknown(tmp_path):
    from run_state import resource_observation as observation

    proc_root = tmp_path / "proc"
    mount = tmp_path / "cgroup"
    _write_cgroup_fixture(proc_root, mount)
    (proc_root / "self" / "mountinfo").write_text(
        "36 25 0:32 / %s rw - cgroup2 cgroup rw\n"
        "37 25 0:33 / %s rw - cgroup2 cgroup rw\n" % (mount, tmp_path / "other"),
        encoding="ascii",
    )
    assert observation._linux_cgroup_process_headroom(proc_root) == (False, None)

    (proc_root / "self" / "cgroup").write_text("0::/parent/child", encoding="ascii")
    assert observation._linux_cgroup_process_headroom(proc_root) == (False, None)


def test_linux_cgroup_nonroot_mount_cannot_prove_hidden_ancestor_limits(tmp_path):
    from run_state import resource_observation as observation

    proc_root = tmp_path / "proc"
    mount = tmp_path / "cgroup"
    _write_cgroup_fixture(proc_root, mount, "/hidden/ancestor/child")
    (proc_root / "self" / "mountinfo").write_text(
        "36 25 0:32 /hidden/ancestor %s rw - cgroup2 cgroup rw\n" % mount,
        encoding="ascii",
    )
    child = mount / "child"
    child.mkdir(parents=True)
    (child / "pids.max").write_text("10\n", encoding="ascii")
    (child / "pids.current").write_text("8\n", encoding="ascii")

    assert observation._linux_cgroup_process_headroom(proc_root) == (False, None)


@pytest.mark.parametrize(
    "sample, expected",
    [
        (
            "some avg10=1.01 avg60=0.50 avg300=0.25 total=9\n"
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n",
            98,
        ),
        ("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n", None),
        (
            "some avg10=101.00 avg60=0.00 avg300=0.00 total=0\n"
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n",
            None,
        ),
        (
            "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=no\n",
            None,
        ),
        (
            "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=0",
            None,
        ),
    ],
)
def test_linux_psi_requires_complete_bounded_documented_fields(sample, expected):
    from run_state import resource_observation as observation

    assert observation._linux_psi_io_available(sample) == expected


def test_darwin_io_success_cannot_be_treated_as_headroom(monkeypatch):
    from run_state import resource_observation as observation

    monkeypatch.setattr(observation.sys, "platform", "darwin")
    monkeypatch.setattr(
        observation.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("iostat must not be used as capacity"),
    )
    assert observation._io_available_units() is None


def test_mac_memory_requires_successful_complete_vm_stat(monkeypatch):
    from run_state import resource_observation as observation

    monkeypatch.setattr(observation.os, "sysconf", lambda _name: 4096)
    valid = (
        "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
        "Pages free: 2.\nPages inactive: 3.\nPages speculative: 4.\n"
    )
    monkeypatch.setattr(
        observation.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=valid),
    )
    assert observation._mac_memory_available() is None

    monkeypatch.setattr(
        observation.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=valid[:-1]),
    )
    assert observation._mac_memory_available() is None

    monkeypatch.setattr(
        observation.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=valid),
    )
    assert observation._mac_memory_available() == 9 * 4096


def test_malformed_cpu_disk_and_timestamp_observations_fail_closed(monkeypatch):
    from run_state import resource_observation as observation

    monkeypatch.setattr(observation.sys, "platform", "unsupported")
    monkeypatch.setattr(observation.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(observation.os, "getloadavg", lambda: (float("nan"), 0, 0))
    monkeypatch.setattr(
        observation.os,
        "statvfs",
        lambda _path: SimpleNamespace(f_bavail=-1, f_frsize=4096),
    )
    monkeypatch.setattr(observation.time, "monotonic_ns", lambda: 0)

    collected = observation.collect_local_observation()
    assert collected.cpu_available is None
    assert collected.disk_available_bytes is None
    with pytest.raises(observation.ResourceObservationRefused):
        collected.validate(now_ns=0)
