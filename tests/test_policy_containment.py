"""A dead group member cannot shield its verified live siblings."""
from types import SimpleNamespace

from process_identity import DEAD, LIVE, ProcessIdentity
from run_state import containment


def test_zombie_and_disappearing_member_do_not_skip_live_containment(monkeypatch):
    leader = ProcessIdentity("host", "boot", 41001, "leader-start")
    sibling = ProcessIdentity("host", "boot", 41002, "sibling-start")
    zombie = ProcessIdentity("host", "boot", 41003, "zombie-start")
    identities = {item.pid: item for item in (leader, sibling, zombie)}
    states = {leader.pid: LIVE, sibling.pid: LIVE, zombie.pid: DEAD}
    signals = []

    def from_pid(_cls, pid):
        if pid not in identities:
            raise ProcessLookupError(pid)
        return identities[pid]

    def signal_member(pid, signal):
        signals.append((pid, signal))
        states[pid] = DEAD

    monkeypatch.setattr(ProcessIdentity, "from_pid", classmethod(from_pid))
    monkeypatch.setattr(containment, "probe_identity", lambda identity: states[identity.pid])
    monkeypatch.setattr(containment.os, "getpgid", lambda _pid: leader.pid)
    monkeypatch.setattr(containment.os, "getsid", lambda _pid: leader.pid)
    monkeypatch.setattr(containment.os, "kill", signal_member)
    monkeypatch.setattr(containment.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        stdout="41001 41001\n41002 41001\n41003 41001\n41004 41001\n",
    ))
    report = containment.contain_session(leader, grace_seconds=0)
    assert report["status"] == "terminated"
    assert {pid for pid, _signal in signals} == {leader.pid, sibling.pid}
    assert {value["start_token"] for value in report["members"]} == {"leader-start", "sibling-start"}
