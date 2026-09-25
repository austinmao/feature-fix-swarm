"""F25: `run-state admission inspect|reconcile` -- the managed admission wedge."""
from __future__ import annotations

import json
import sqlite3

import run_state.cli as cli
from run_state import managed_admission
from run_state.managed_admission import ManagedAdmissionQueue

from test_resource_admission import OWNER, _observation, _raw_v1_root


def _last_payload(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_inspect_counts_gate_armed_exit0_no_ticket_values(tmp_path, monkeypatch, capsys):
    root = _raw_v1_root(
        tmp_path, ticket="legacy-secret-ticket", host_id=OWNER.host_id, boot_id=OWNER.boot_id,
        pid=55, start_token="same-boot-start", status="waiting",
    )
    monkeypatch.setattr(managed_admission.ProcessIdentity, "current", staticmethod(lambda: OWNER))
    monkeypatch.setattr(managed_admission, "probe_identity", lambda _: "LIVE")
    queue = ManagedAdmissionQueue(root, observation_provider=lambda: _observation())
    ticket = queue.enqueue(state_root=tmp_path / "waiting", run_id="waiting")

    returncode = cli.main(["admission", "inspect", "--root", str(root)])
    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])

    assert returncode == 0
    assert payload["ok"] is True and payload["mode"] == "inspect"
    assert payload["gate_armed_before"] is True and payload["gate_armed_after"] is True
    assert payload["counts"] == {"1/waiting": 1, "2/waiting": 1}
    assert payload["backup"] is None
    assert all("ticket" not in row for row in payload["rows"])
    assert "legacy-secret-ticket" not in out
    assert ticket.ticket not in out


def test_inspect_missing_store_exit2_creates_nothing(tmp_path, capsys):
    root = tmp_path / "admission"

    returncode = cli.main(["admission", "inspect", "--root", str(root)])
    payload = _last_payload(capsys)

    assert returncode == 2
    assert payload["code"] == "MANAGED_ADMISSION_STORE_MISSING"
    assert not root.exists()


def test_reconcile_apply_exit3_when_gate_still_armed(tmp_path, monkeypatch, capsys):
    root = _raw_v1_root(
        tmp_path, ticket="old", host_id=OWNER.host_id, boot_id=OWNER.boot_id,
        pid=55, start_token="same-boot-start", status="waiting",
    )
    monkeypatch.setattr(managed_admission.ProcessIdentity, "current", staticmethod(lambda: OWNER))
    monkeypatch.setattr(managed_admission, "probe_identity", lambda _: "LIVE")

    returncode = cli.main(["admission", "reconcile", "--root", str(root), "--apply"])
    payload = _last_payload(capsys)

    assert returncode == 3
    assert payload["ok"] is True and payload["mode"] == "apply"
    assert payload["gate_armed_before"] is True and payload["gate_armed_after"] is True
    row = next(row for row in payload["rows"] if row["sequence"] == 1)
    assert row["decision"] == "keep" and row["reason"] == "LEGACY_SAME_BOOT_UNPROVABLE"

    with sqlite3.connect(root / "admission.sqlite3") as connection:
        status = connection.execute("SELECT status FROM managed_admissions WHERE sequence=1").fetchone()[0]
    assert status == "waiting", "an unprovable row must never be mutated"


def test_reconcile_backup_failure_exit5_db_untouched(tmp_path, monkeypatch, capsys):
    legacy = managed_admission.ProcessIdentity("fixture-host", "prior-boot", 7, "prior-start")
    root = _raw_v1_root(
        tmp_path, ticket="old", host_id=legacy.host_id, boot_id=legacy.boot_id,
        pid=legacy.pid, start_token=legacy.start_token, status="waiting",
    )
    monkeypatch.setattr(managed_admission.ProcessIdentity, "current", staticmethod(lambda: OWNER))
    monkeypatch.setattr(managed_admission, "probe_identity", lambda identity: "LIVE" if identity == OWNER else "DEAD")
    monkeypatch.setattr(
        managed_admission.ManagedAdmissionQueue, "_perform_backup",
        lambda self, source, destination_path: (_ for _ in ()).throw(OSError("simulated disk failure")),
    )
    before = (root / "admission.sqlite3").read_bytes()

    returncode = cli.main(["admission", "reconcile", "--root", str(root), "--apply"])
    payload = _last_payload(capsys)

    assert returncode == 5
    assert payload["code"] == "RECONCILE_BACKUP_FAILED"
    assert (root / "admission.sqlite3").read_bytes() == before
    assert not any(entry.name.endswith(".bak") for entry in root.iterdir())
