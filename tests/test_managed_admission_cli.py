"""F25: `run-state admission inspect|reconcile` -- the managed admission wedge."""
from __future__ import annotations

import errno
import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest

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
    assert payload["code"] == "LEGACY_OPAQUE_REMAINS"
    assert payload["recovery_action"]["action"] == "inspect_managed_admission"
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
    before = (root / "admission.sqlite3").read_bytes()
    monkeypatch.setattr(
        managed_admission, "_perform_backup",
        lambda source, destination_path: (_ for _ in ()).throw(OSError("simulated disk failure")),
    )

    returncode = cli.main(["admission", "reconcile", "--root", str(root), "--apply"])
    payload = _last_payload(capsys)

    assert returncode == 5
    assert payload["code"] == "RECONCILE_BACKUP_FAILED"
    assert (root / "admission.sqlite3").read_bytes() == before
    assert not any(entry.name.endswith(".bak") for entry in root.iterdir())


# --- Round 2 (review findings) -----------------------------------------


def test_inspect_and_dry_run_never_migrate_a_raw_v1_store(tmp_path, monkeypatch):
    root = _raw_v1_root(
        tmp_path, ticket="old", host_id=OWNER.host_id, boot_id=OWNER.boot_id,
        pid=55, start_token="same-boot-start", status="waiting",
    )
    monkeypatch.setattr(managed_admission.ProcessIdentity, "current", staticmethod(lambda: OWNER))
    monkeypatch.setattr(managed_admission, "probe_identity", lambda _: "LIVE")
    path = root / "admission.sqlite3"
    before = path.read_bytes()
    before_listing = sorted(entry.name for entry in root.iterdir())

    for argv in (["admission", "inspect", "--root", str(root)],
                 ["admission", "reconcile", "--root", str(root)]):
        assert cli.main(argv) == 0
        assert path.read_bytes() == before
        assert sorted(entry.name for entry in root.iterdir()) == before_listing
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT version FROM admission_policy").fetchone()[0] == 1
            assert not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name LIKE 'admission_fence%'"
            ).fetchall()


def test_apply_backup_shows_pre_migration_version_when_store_started_v1(tmp_path, monkeypatch, capsys):
    root = _raw_v1_root(
        tmp_path, ticket="old", host_id=OWNER.host_id, boot_id="prior-boot",
        pid=7, start_token="prior-start", status="released",
    )
    monkeypatch.setattr(managed_admission.ProcessIdentity, "current", staticmethod(lambda: OWNER))
    monkeypatch.setattr(managed_admission, "probe_identity", lambda identity: "LIVE" if identity == OWNER else "DEAD")

    returncode = cli.main(["admission", "reconcile", "--root", str(root), "--apply"])
    payload = _last_payload(capsys)

    assert returncode == 0
    with sqlite3.connect(payload["backup"]["path"]) as backup_connection:
        assert backup_connection.execute("SELECT version FROM admission_policy").fetchone()[0] == 1
    with sqlite3.connect(root / "admission.sqlite3") as connection:
        assert connection.execute("SELECT version FROM admission_policy").fetchone()[0] == 2


def test_apply_blocks_a_concurrent_writer_for_the_whole_backup_and_cas_window(tmp_path, monkeypatch):
    root = _raw_v1_root(
        tmp_path, ticket="old", host_id=OWNER.host_id, boot_id="prior-boot",
        pid=7, start_token="prior-start", status="waiting",
    )
    monkeypatch.setattr(managed_admission.ProcessIdentity, "current", staticmethod(lambda: OWNER))
    monkeypatch.setattr(managed_admission, "probe_identity", lambda identity: "LIVE" if identity == OWNER else "DEAD")

    entered = threading.Event()
    release = threading.Event()
    real_backup = managed_admission._perform_backup

    def paused_backup(source, destination_path):
        entered.set()
        release.wait(5)
        real_backup(source, destination_path)

    monkeypatch.setattr(managed_admission, "_perform_backup", paused_backup)

    results = {}

    def run_apply():
        results["report"] = managed_admission.apply_reconcile_at_root(root)

    worker = threading.Thread(target=run_apply)
    worker.start()
    assert entered.wait(2), "backup never started"

    contender = sqlite3.connect(str(root / "admission.sqlite3"), isolation_level=None, timeout=1)
    try:
        contender.execute("PRAGMA busy_timeout=500")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            contender.execute("BEGIN IMMEDIATE")
    finally:
        contender.close()

    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert results["report"]["reclaimed"] == [1]


def _pin_backup_filename(monkeypatch):
    monkeypatch.setattr(managed_admission.secrets, "token_hex", lambda n: "pinned")
    monkeypatch.setattr(managed_admission.time, "strftime", lambda *a, **k: "20260101T000000Z")
    return "admission.sqlite3.reconcile-20260101T000000Z-pinned.bak"


def test_apply_backup_target_pre_created_is_untouched_exit5(tmp_path, monkeypatch, capsys):
    root = _raw_v1_root(
        tmp_path, ticket="old", host_id=OWNER.host_id, boot_id="prior-boot",
        pid=7, start_token="prior-start", status="waiting",
    )
    monkeypatch.setattr(managed_admission.ProcessIdentity, "current", staticmethod(lambda: OWNER))
    monkeypatch.setattr(managed_admission, "probe_identity", lambda identity: "LIVE" if identity == OWNER else "DEAD")
    name = _pin_backup_filename(monkeypatch)
    pinned = root / name
    pinned.write_bytes(b"pre-existing content, must survive")
    before_db = (root / "admission.sqlite3").read_bytes()

    returncode = cli.main(["admission", "reconcile", "--root", str(root), "--apply"])
    payload = _last_payload(capsys)

    assert returncode == 5
    assert payload["code"] == "RECONCILE_BACKUP_FAILED"
    assert pinned.read_bytes() == b"pre-existing content, must survive"
    assert (root / "admission.sqlite3").read_bytes() == before_db


def test_apply_backup_target_symlink_is_untouched_exit5(tmp_path, monkeypatch, capsys):
    root = _raw_v1_root(
        tmp_path, ticket="old", host_id=OWNER.host_id, boot_id="prior-boot",
        pid=7, start_token="prior-start", status="waiting",
    )
    monkeypatch.setattr(managed_admission.ProcessIdentity, "current", staticmethod(lambda: OWNER))
    monkeypatch.setattr(managed_admission, "probe_identity", lambda identity: "LIVE" if identity == OWNER else "DEAD")
    name = _pin_backup_filename(monkeypatch)
    pinned = root / name
    elsewhere = tmp_path / "elsewhere.bak"
    elsewhere.write_bytes(b"symlink target content")
    pinned.symlink_to(elsewhere)
    before_db = (root / "admission.sqlite3").read_bytes()

    returncode = cli.main(["admission", "reconcile", "--root", str(root), "--apply"])
    payload = _last_payload(capsys)

    assert returncode == 5
    assert payload["code"] == "RECONCILE_BACKUP_FAILED"
    assert pinned.is_symlink()
    assert elsewhere.read_bytes() == b"symlink target content"
    assert (root / "admission.sqlite3").read_bytes() == before_db


def test_apply_backup_create_enospc_is_exit5_no_bak_left(tmp_path, monkeypatch, capsys):
    root = _raw_v1_root(
        tmp_path, ticket="old", host_id=OWNER.host_id, boot_id="prior-boot",
        pid=7, start_token="prior-start", status="waiting",
    )
    monkeypatch.setattr(managed_admission.ProcessIdentity, "current", staticmethod(lambda: OWNER))
    monkeypatch.setattr(managed_admission, "probe_identity", lambda identity: "LIVE" if identity == OWNER else "DEAD")

    real_open = managed_admission.os.open

    def enospc_on_backup_file(path, flags, mode=0o777, *a, **k):
        if str(path).endswith(".bak"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(path, flags, mode, *a, **k)

    monkeypatch.setattr(managed_admission.os, "open", enospc_on_backup_file)

    returncode = cli.main(["admission", "reconcile", "--root", str(root), "--apply"])
    payload = _last_payload(capsys)

    assert returncode == 5
    assert payload["code"] == "RECONCILE_BACKUP_FAILED"
    assert not any(entry.name.endswith(".bak") for entry in root.iterdir())


def test_reconcile_apply_row_changed_exit4_through_cli(tmp_path, monkeypatch, capsys):
    root = _raw_v1_root(
        tmp_path, ticket="old", host_id=OWNER.host_id, boot_id="prior-boot",
        pid=7, start_token="prior-start", status="waiting",
    )
    monkeypatch.setattr(managed_admission.ProcessIdentity, "current", staticmethod(lambda: OWNER))
    monkeypatch.setattr(managed_admission, "probe_identity", lambda identity: "LIVE" if identity == OWNER else "DEAD")

    real_migrate_locked = managed_admission.ManagedAdmissionQueue._migrate_locked

    def racing_migrate_locked(c):
        real_migrate_locked(c)
        # A resume_legacy that landed inside the writer's own lock window:
        # the CAS below still carries the PRE-migration writer_version=1
        # snapshot, so it must miss, not overwrite.
        c.execute("UPDATE managed_admissions SET writer_version=2 WHERE sequence=1")

    monkeypatch.setattr(
        managed_admission.ManagedAdmissionQueue, "_migrate_locked", staticmethod(racing_migrate_locked)
    )

    returncode = cli.main(["admission", "reconcile", "--root", str(root), "--apply"])
    payload = _last_payload(capsys)

    assert returncode == 4
    assert payload["code"] == "ROW_CHANGED"
    assert payload["row_changed"] == [1] and payload["reclaimed"] == []
    with sqlite3.connect(root / "admission.sqlite3") as connection:
        row = connection.execute(
            "SELECT status,writer_version FROM managed_admissions WHERE sequence=1"
        ).fetchone()
    assert row == ("waiting", 2)


def test_symlinked_root_refuses_root_unsafe_not_followed(tmp_path, capsys):
    real_root = tmp_path / "real-admission"
    real_root.mkdir(mode=0o700)
    symlinked = tmp_path / "symlinked-admission"
    symlinked.symlink_to(real_root)

    returncode = cli.main(["admission", "inspect", "--root", str(symlinked)])
    payload = _last_payload(capsys)

    assert returncode == 6
    assert payload["code"] == "MANAGED_ADMISSION_ROOT_UNSAFE"


def _empty_admission_policy_root(tmp_path):
    root = tmp_path / "admission"
    root.mkdir(mode=0o700)
    path = root / "admission.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE admission_policy (singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL CHECK(version=1),capacity INTEGER NOT NULL CHECK(capacity=2))"
        )
        connection.execute(
            "CREATE TABLE managed_admissions (sequence INTEGER PRIMARY KEY AUTOINCREMENT,ticket TEXT NOT NULL UNIQUE,state_root TEXT NOT NULL,run_id TEXT NOT NULL,host_id TEXT NOT NULL,boot_id TEXT NOT NULL,pid INTEGER NOT NULL CHECK(pid>0),start_token TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('waiting','active','released','reclaimed')))"
        )
        # admission_policy deliberately left with zero rows.
    path.chmod(0o600)
    return root


def test_empty_admission_policy_table_inspect_refuses_schema_invalid(tmp_path, capsys):
    root = _empty_admission_policy_root(tmp_path)

    returncode = cli.main(["admission", "inspect", "--root", str(root)])
    payload = _last_payload(capsys)

    assert returncode == 6
    assert payload["code"] == "MANAGED_ADMISSION_SCHEMA_INVALID"


def test_empty_admission_policy_table_apply_refuses_schema_invalid_no_bak_left(tmp_path, capsys):
    root = _empty_admission_policy_root(tmp_path)

    returncode = cli.main(["admission", "reconcile", "--root", str(root), "--apply"])
    payload = _last_payload(capsys)

    assert returncode == 6
    assert payload["code"] == "MANAGED_ADMISSION_SCHEMA_INVALID"
    assert not any(entry.name.endswith(".bak") for entry in root.iterdir())


def test_reconcile_dry_run_mode_through_cli_leaves_db_untouched(tmp_path, monkeypatch, capsys):
    root = _raw_v1_root(
        tmp_path, ticket="old", host_id=OWNER.host_id, boot_id="prior-boot",
        pid=7, start_token="prior-start", status="waiting",
    )
    monkeypatch.setattr(managed_admission.ProcessIdentity, "current", staticmethod(lambda: OWNER))
    monkeypatch.setattr(managed_admission, "probe_identity", lambda identity: "LIVE" if identity == OWNER else "DEAD")
    before = (root / "admission.sqlite3").read_bytes()

    returncode = cli.main(["admission", "reconcile", "--root", str(root)])
    payload = _last_payload(capsys)

    assert returncode == 0
    assert payload["mode"] == "dry-run"
    assert payload["backup"] is None
    assert (root / "admission.sqlite3").read_bytes() == before
    assert any(row["decision"] == "reclaim" for row in payload["rows"])


def test_reconcile_apply_success_exit0_no_ticket_value_and_backup_sha256_matches_disk(
    tmp_path, monkeypatch, capsys,
):
    root = _raw_v1_root(
        tmp_path, ticket="super-secret-ticket", host_id=OWNER.host_id, boot_id="prior-boot",
        pid=7, start_token="prior-start", status="waiting",
    )
    monkeypatch.setattr(managed_admission.ProcessIdentity, "current", staticmethod(lambda: OWNER))
    monkeypatch.setattr(managed_admission, "probe_identity", lambda identity: "LIVE" if identity == OWNER else "DEAD")

    returncode = cli.main(["admission", "reconcile", "--root", str(root), "--apply"])
    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])

    assert returncode == 0
    assert "code" not in payload
    assert "super-secret-ticket" not in out
    backup_bytes = Path(payload["backup"]["path"]).read_bytes()
    assert payload["backup"]["sha256"] == hashlib.sha256(backup_bytes).hexdigest()
    assert payload["reclaimed"] == [1] and payload["row_changed"] == []


def test_unsafe_store_permissions_refuse_exit6(tmp_path, capsys):
    root = tmp_path / "admission"
    root.mkdir(mode=0o700)
    path = root / "admission.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE admission_policy (singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL CHECK(version=2),writer_nonce TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO admission_policy VALUES(1,2,'nonce')")
        connection.execute(
            "CREATE TABLE managed_admissions (sequence INTEGER PRIMARY KEY AUTOINCREMENT,ticket TEXT NOT NULL UNIQUE,state_root TEXT NOT NULL,run_id TEXT NOT NULL,host_id TEXT NOT NULL,boot_id TEXT NOT NULL,pid INTEGER NOT NULL CHECK(pid>0),start_token TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('waiting','active','released','reclaimed')))"
        )
    path.chmod(0o644)  # world-readable -- not the required 0600

    returncode = cli.main(["admission", "inspect", "--root", str(root)])
    payload = _last_payload(capsys)

    assert returncode == 6
    assert payload["code"] == "MANAGED_ADMISSION_STORE_UNSAFE"


def test_default_root_resolves_via_ffs_managed_admission_root_env(tmp_path, monkeypatch, capsys):
    root = _raw_v1_root(
        tmp_path, ticket="old", host_id=OWNER.host_id, boot_id=OWNER.boot_id,
        pid=55, start_token="same-boot-start", status="waiting",
    )
    monkeypatch.setenv("FFS_MANAGED_ADMISSION_ROOT", str(root))
    monkeypatch.setattr(managed_admission.ProcessIdentity, "current", staticmethod(lambda: OWNER))
    monkeypatch.setattr(managed_admission, "probe_identity", lambda _: "LIVE")

    returncode = cli.main(["admission", "inspect"])
    payload = _last_payload(capsys)

    assert returncode == 0
    assert payload["database"] == str(root / "admission.sqlite3")
