"""Production-shaped contracts for Spec 014's migration writer epoch."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from run_state.migration import (
    LegacyOwner,
    LegacySource,
    MigrationCoordinator,
    MigrationRefused,
    assert_managed_epoch,
    source_manifest,
)
from run_state.cli import main as cli_main
from process_identity import ProcessIdentity
from run_state.state import ControlStore, RunStore


CAPABILITY = "a" * 64
REGISTRATION = "spec-014-migration"


def _authority(
    tmp_path: Path, sources: list[LegacySource]
) -> tuple[ControlStore, MigrationCoordinator]:
    store = ControlStore(tmp_path / "authority" / "control.sqlite3")
    store.initialize_migration_fixture(
        registration_id=REGISTRATION,
        capability_sha256=CAPABILITY,
        source_identity_json=source_manifest(sources),
    )
    return store, MigrationCoordinator(
        store,
        registration_id=REGISTRATION,
        capability_sha256=CAPABILITY,
    )


def _legacy_store(
    path: Path, *, objective: str = "migrate this"
) -> tuple[RunStore, str]:
    legacy = RunStore(path)
    return legacy, legacy.create_run(skill="fix", objective=objective)


def _owner(state: str) -> LegacyOwner:
    return LegacyOwner(
        state=state,  # type: ignore[arg-type]
        identity={
            "host_id": "fixture-host",
            "boot_id": "fixture-boot",
            "pid": 4242,
            "start_token": "observed-start",
        },
        proof={"probe": "fixture-liveness", "observed_state": state},
    )


def _capability_file(tmp_path: Path) -> Path:
    path = tmp_path / "migration-capability"
    path.write_bytes(b"fixture-private-migration-capability\n")
    path.chmod(0o600)
    return path


def _events(store: ControlStore, kind: str) -> list[dict]:
    return [
        event["payload"]
        if isinstance(event["payload"], dict)
        else json.loads(event["payload"])
        for event in store.enumerate_events()
        if event["event_type"] == kind
    ]


def test_compatibility_readers_import_once_and_quarantine_without_blocking(
    tmp_path: Path,
) -> None:
    legacy_path = tmp_path / "legacy-runs.db"
    _, run_id = _legacy_store(legacy_path)
    conflict = tmp_path / "legacy-context.json"
    conflict.write_text(
        json.dumps({"FFS_RUN_ID": run_id, "GSD_RUN_ID": "other-run"}) + "\n"
    )
    sources = [
        LegacySource(legacy_path, "run-store"),
        LegacySource(conflict, "context"),
    ]
    store, migration = _authority(tmp_path, sources)
    before = {path: path.read_bytes() for path in (legacy_path, conflict)}

    reports = migration.import_sources(sources)

    assert reports[0].imported == (run_id,)
    assert reports[1].quarantined == (("quarantine-00000000", "ALIAS_CONFLICT"),)
    assert migration.writer_for(run_id).writer == "legacy"
    assert migration.journal(run_id)[0]["import_target"] == f"run/{run_id}"
    assert {path: path.read_bytes() for path in (legacy_path, conflict)} == before
    with store.read_transaction() as tx:
        assert (
            tx.execute("SELECT readers_ready FROM migration_fixture").fetchone()[0] == 1
        )
        assert tx.execute("SELECT count(*) FROM migration_journal").fetchone()[0] == 2

    replay = migration.import_sources(sources)
    assert replay[0].replayed == (run_id,)
    assert replay[1].replayed == ("quarantine-00000000",)
    with store.read_transaction() as tx:
        assert tx.execute("SELECT count(*) FROM migration_journal").fetchone()[0] == 2


def test_malformed_registered_context_is_quarantined_without_blocking_valid_source(
    tmp_path: Path,
) -> None:
    legacy_path = tmp_path / "legacy-runs.db"
    _, run_id = _legacy_store(legacy_path)
    malformed = tmp_path / "malformed-context.json"
    malformed.write_text('{"FFS_RUN_ID":')
    sources = [
        LegacySource(legacy_path, "run-store"),
        LegacySource(malformed, "context"),
    ]
    store, migration = _authority(tmp_path, sources)

    reports = migration.import_sources(sources)

    assert reports[0].imported == (run_id,)
    assert reports[1].quarantined == (
        ("quarantine-00000000", "MIGRATION_SOURCE_INVALID"),
    )
    assert migration.writer_for(run_id).writer == "legacy"
    malformed_row = next(
        row for row in migration.journal() if row["source_id"] == sources[1].source_id
    )
    assert malformed_row["disposition"] == "quarantined"
    assert malformed_row["conflict_reason"] == "MIGRATION_SOURCE_INVALID"
    record = json.loads(malformed_row["record_json"])
    assert record["source_error"] == {
        "code": "MIGRATION_SOURCE_INVALID",
        "source_kind": "context",
        "source_path": str(malformed.resolve()),
        "source_sha256": reports[1].source_sha256,
        "exception_class": "JSONDecodeError",
        "exception_message": "Expecting value; line=1; column=15",
    }

    restarted = MigrationCoordinator(
        store,
        registration_id=REGISTRATION,
        capability_sha256=CAPABILITY,
    )
    replay = restarted.import_sources(sources)
    assert replay[0].replayed == (run_id,)
    assert replay[1].replayed == ("quarantine-00000000",)
    replayed_row = next(
        row for row in restarted.journal() if row["source_id"] == sources[1].source_id
    )
    assert replayed_row["record_json"] == malformed_row["record_json"]
    with store.read_transaction() as tx:
        assert tx.execute("SELECT count(*) FROM migration_snapshots").fetchone()[0] == 2
        assert tx.execute("SELECT count(*) FROM migration_journal").fetchone()[0] == 2


def test_live_or_unknown_owner_keeps_legacy_writer_unchanged(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy-runs.db"
    _, run_id = _legacy_store(legacy_path)
    sources = [LegacySource(legacy_path, "run-store")]
    store, migration = _authority(tmp_path, sources)
    migration.import_sources(sources)

    live = migration.handoff(run_id, _owner("live"))
    unknown = migration.handoff(run_id, _owner("unknown"))

    assert live.writer == unknown.writer == "legacy"
    assert live.epoch == unknown.epoch == 0
    with pytest.raises(MigrationRefused, match="WRITER_HANDOFF_REQUIRED"):
        migration.assert_new_writer(run_id)
    blocked = _events(store, "migration_handoff_blocked")
    assert [entry["legacy_owner"]["state"] for entry in blocked] == ["live", "unknown"]
    assert all(entry["legacy_owner"]["identity"]["pid"] == 4242 for entry in blocked)


def test_released_handoff_restart_and_rollback_select_exactly_one_writer(
    tmp_path: Path,
) -> None:
    legacy_path = tmp_path / "legacy-runs.db"
    _, run_id = _legacy_store(legacy_path)
    sources = [LegacySource(legacy_path, "run-store")]
    store, migration = _authority(tmp_path, sources)
    migration.import_sources(sources)

    active = migration.handoff(run_id, _owner("released"))
    assert (active.writer, active.epoch, active.checkpoint) == (
        "new",
        1,
        "new_writer_activated",
    )
    assert migration.assert_new_writer(run_id) == active

    # Simulates restart after a committed handoff: the same source has no
    # duplicate import and a repeated handoff cannot advance the epoch.
    restarted = MigrationCoordinator(
        store, registration_id=REGISTRATION, capability_sha256=CAPABILITY
    )
    assert restarted.import_sources(sources)[0].replayed == (run_id,)
    assert restarted.handoff(run_id, _owner("released")) == active

    restored = restarted.rollback(
        run_id,
        legacy_writer_compatible=True,
        proof={"legacy_protocol": "v1", "drill": "passed"},
    )
    assert (restored.writer, restored.epoch, restored.checkpoint) == (
        "legacy",
        2,
        "legacy_writer_reinstated",
    )
    assert restarted.writer_for(run_id) == restored
    epochs = _events(store, "migration_writer_epoch")
    assert [(row["epoch"], row["writer"]) for row in epochs] == [
        (1, "new"),
        (2, "legacy"),
    ]


def test_interrupted_multisource_import_restarts_without_duplicate_records(
    tmp_path: Path, monkeypatch
) -> None:
    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"
    _, first_run = _legacy_store(first_path, objective="first")
    _, second_run = _legacy_store(second_path, objective="second")
    sources = [
        LegacySource(first_path, "run-store"),
        LegacySource(second_path, "run-store"),
    ]
    store, migration = _authority(tmp_path, sources)
    original = migration._journal_source
    calls = 0

    def interrupt_after_first(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated interruption")
        return original(*args, **kwargs)

    monkeypatch.setattr(migration, "_journal_source", interrupt_after_first)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        migration.import_sources(sources)
    with store.read_transaction() as tx:
        assert tx.execute("SELECT count(*) FROM migration_journal").fetchone()[0] == 1
        assert (
            tx.execute("SELECT readers_ready FROM migration_fixture").fetchone()[0] == 0
        )

    monkeypatch.setattr(migration, "_journal_source", original)
    reports = MigrationCoordinator(
        store,
        registration_id=REGISTRATION,
        capability_sha256=CAPABILITY,
    ).import_sources(sources)
    assert reports[0].replayed == (first_run,)
    assert reports[1].imported == (second_run,)
    with store.read_transaction() as tx:
        assert tx.execute("SELECT count(*) FROM migration_journal").fetchone()[0] == 2
        assert (
            tx.execute("SELECT readers_ready FROM migration_fixture").fetchone()[0] == 1
        )


def test_incompatible_rollback_pauses_and_preserves_new_epoch_evidence(
    tmp_path: Path,
) -> None:
    legacy_path = tmp_path / "legacy-runs.db"
    _, run_id = _legacy_store(legacy_path)
    sources = [LegacySource(legacy_path, "run-store")]
    store, migration = _authority(tmp_path, sources)
    migration.import_sources(sources)
    migration.handoff(run_id, _owner("dead"))

    paused = migration.rollback(
        run_id,
        legacy_writer_compatible=False,
        proof={"legacy_protocol": "unsupported", "drill": "recorded"},
    )

    assert (paused.writer, paused.checkpoint) == ("none", "paused_incompatible")
    with pytest.raises(MigrationRefused, match="MIGRATION_WRITER_PAUSED"):
        migration.writer_for(run_id)
    with store.read_transaction() as tx:
        row = tx.execute(
            "SELECT epoch,writer,checkpoint,proof_json FROM migration_epochs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    assert (row["epoch"], row["writer"], row["checkpoint"]) == (
        2,
        "none",
        "paused_incompatible",
    )
    assert json.loads(row["proof_json"])["from_epoch"] == 1


def test_conflicting_legacy_source_is_quarantined_while_other_run_imports(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"
    _, first_run = _legacy_store(first_path, objective="first")
    _, second_run = _legacy_store(second_path, objective="second")
    # These independent source files now assert the same legacy run identity.
    connection = sqlite3.connect(second_path)
    connection.execute("UPDATE runs SET id=? WHERE id=?", (first_run, second_run))
    connection.execute(
        "UPDATE events SET run_id=? WHERE run_id=?", (first_run, second_run)
    )
    connection.commit()
    connection.close()
    third_path = tmp_path / "third.db"
    _, third_run = _legacy_store(third_path, objective="unrelated")
    sources = [
        LegacySource(first_path, "run-store"),
        LegacySource(second_path, "run-store"),
        LegacySource(third_path, "run-store"),
    ]
    _store, migration = _authority(tmp_path, sources)

    reports = migration.import_sources(sources)

    assert reports[0].imported == (first_run,)
    assert reports[1].quarantined == ((first_run, "CANONICAL_RUN_CONFLICT"),)
    assert reports[2].imported == (third_run,)
    assert migration.writer_for(third_run).writer == "legacy"


def test_invalid_legacy_identifier_is_quarantined_without_silent_rewrite(
    tmp_path: Path,
) -> None:
    legacy_path = tmp_path / "invalid.db"
    _, run_id = _legacy_store(legacy_path)
    connection = sqlite3.connect(legacy_path)
    connection.execute("UPDATE runs SET id='bad legacy id' WHERE id=?", (run_id,))
    connection.execute(
        "UPDATE events SET run_id='bad legacy id' WHERE run_id=?", (run_id,)
    )
    connection.commit()
    connection.close()
    source = LegacySource(legacy_path, "run-store")
    _store, migration = _authority(tmp_path, [source])

    report = migration.import_sources([source])[0]

    assert report.imported == ()
    assert report.quarantined == (("bad legacy id", "INVALID_RUN_ID"),)
    assert migration.journal()[0]["run_id"] == "bad legacy id"


def test_registration_manifest_and_owner_proof_fail_closed(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy-runs.db"
    _, run_id = _legacy_store(legacy_path)
    source = LegacySource(legacy_path, "run-store")
    _store, migration = _authority(tmp_path, [source])
    with pytest.raises(MigrationRefused, match="MIGRATION_SOURCE_MANIFEST_MISMATCH"):
        migration.import_sources([])
    migration.import_sources([source])
    with pytest.raises(MigrationRefused, match="LEGACY_OWNER_IDENTITY_REQUIRED"):
        migration.handoff(run_id, LegacyOwner("dead", None, {"observed_state": "dead"}))
    with pytest.raises(MigrationRefused, match="INVALID_LEGACY_OWNER_IDENTITY"):
        migration.handoff(
            run_id,
            LegacyOwner(
                "dead",
                {"pid": 1},
                {"observed_state": "dead"},
            ),
        )
    with pytest.raises(MigrationRefused, match="INVALID_ROLLBACK_PROOF"):
        migration.rollback(run_id, legacy_writer_compatible=True, proof={})


def test_public_cli_uses_fenced_adapter_and_binds_managed_epoch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The public command has no caller-owned liveness/rollback JSON seam."""
    legacy_path = tmp_path / "legacy-runs.db"
    _, run_id = _legacy_store(legacy_path)
    identity = ProcessIdentity.current()
    connection = sqlite3.connect(legacy_path)
    connection.execute("UPDATE runs SET tokens_budget=321 WHERE id=?", (run_id,))
    connection.execute(
        "CREATE TABLE migration_legacy_owner_fence ("
        "run_id TEXT PRIMARY KEY,state TEXT NOT NULL,host_id TEXT NOT NULL,"
        "boot_id TEXT NOT NULL,pid INTEGER NOT NULL,start_token TEXT NOT NULL,"
        "legacy_start_fence TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO migration_legacy_owner_fence VALUES(?,?,?,?,?,?,?)",
        (run_id, "released", identity.host_id, identity.boot_id, identity.pid,
         identity.start_token, "legacy-release-v1"),
    )
    connection.commit()
    connection.close()
    authority = tmp_path / "authority"
    capability_file = _capability_file(tmp_path)
    base = [
        "--state-root", str(authority), "--registration-id", REGISTRATION,
        "--capability-file", str(capability_file),
    ]
    source = f"run-store:{legacy_path}"

    assert cli_main([
        "migration", "enroll", *base, "--repository-id", "repo-fixture", "--source", source,
    ]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert cli_main([
        "migration", "import", *base, "--repository-id", "repo-fixture", "--source", source,
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["reports"][0]["imported"] == [run_id]

    assert cli_main(["migration", "handoff", *base, "--run-id", run_id]) == 0
    handoff = json.loads(capsys.readouterr().out)
    assert handoff["writer"] == "new"
    store = ControlStore(authority / "control.sqlite3")
    assert assert_managed_epoch(store, run_id) == 1

    # This public path has no --compatible/--proof escape hatch.  Missing a
    # verified reverse adapter leaves a durable paused epoch.
    assert cli_main(["migration", "rollback", *base, "--run-id", run_id]) == 5
    rollback = json.loads(capsys.readouterr().out)
    assert rollback["code"] == "MIGRATION_ROLLBACK_PAUSED"
    with pytest.raises(MigrationRefused, match="MIGRATION_WRITER_PAUSED"):
        assert_managed_epoch(store, run_id)


def test_public_handoff_refuses_unmaterialized_budget(tmp_path: Path, capsys) -> None:
    legacy_path = tmp_path / "legacy-runs.db"
    _, run_id = _legacy_store(legacy_path)
    authority = tmp_path / "authority"
    capability_file = _capability_file(tmp_path)
    base = [
        "--state-root", str(authority), "--registration-id", REGISTRATION,
        "--capability-file", str(capability_file),
    ]
    source = f"run-store:{legacy_path}"
    assert cli_main([
        "migration", "enroll", *base, "--repository-id", "repo-fixture", "--source", source,
    ]) == 0
    capsys.readouterr()
    assert cli_main([
        "migration", "import", *base, "--repository-id", "repo-fixture", "--source", source,
    ]) == 0
    capsys.readouterr()
    assert cli_main(["migration", "handoff", *base, "--run-id", run_id]) == 5
    assert json.loads(capsys.readouterr().out)["code"] == "MIGRATION_MANAGED_CONTEXT_INCOMPLETE"


def test_trusted_handoff_refuses_sidecar_created_after_import(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy-runs.db"
    _, run_id = _legacy_store(legacy_path)
    identity = ProcessIdentity.current()
    connection = sqlite3.connect(legacy_path)
    connection.execute("UPDATE runs SET tokens_budget=321 WHERE id=?", (run_id,))
    connection.execute(
        "CREATE TABLE migration_legacy_owner_fence ("
        "run_id TEXT PRIMARY KEY,state TEXT NOT NULL,host_id TEXT NOT NULL,"
        "boot_id TEXT NOT NULL,pid INTEGER NOT NULL,start_token TEXT NOT NULL,"
        "legacy_start_fence TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO migration_legacy_owner_fence VALUES(?,?,?,?,?,?,?)",
        (run_id, "released", identity.host_id, identity.boot_id, identity.pid,
         identity.start_token, "legacy-release-v1"),
    )
    connection.commit()
    connection.close()
    source = LegacySource(legacy_path, "run-store")
    _store, migration = _authority(tmp_path, [source])
    migration.import_sources([source])

    # A WAL which did not exist in the recorded source snapshot is enough to
    # refuse before the owner-fence adapter reads any legacy state.
    Path(f"{legacy_path}-wal").write_bytes(b"uncaptured-sidecar")
    with pytest.raises(MigrationRefused, match="MIGRATION_SOURCE_CHANGED"):
        migration.handoff_trusted(run_id)


def test_raw_cli_cannot_mutate_any_epoch_in_enrolled_execution_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    legacy_path = tmp_path / "legacy-runs.db"
    _, run_id = _legacy_store(legacy_path)
    source = LegacySource(legacy_path, "run-store")
    store, migration = _authority(tmp_path, [source])
    migration.import_sources([source])
    monkeypatch.setenv("RUN_STATE_DB", str(store.db_path))

    def raw(command: list[str]) -> None:
        assert cli_main(command) == 5
        assert json.loads(capsys.readouterr().out)["code"] == "MIGRATION_RAW_MUTATION_REFUSED"

    # Unlike a plain legacy database, an enrolled execution store cannot
    # accept a fresh unbounded RunStore row through the raw start façade.
    raw(["start", "--skill", "fix", "--objective", "raw migration bypass"])
    raw(["update", run_id, "--phase", "blocked"])
    migration.handoff(run_id, _owner("released"))
    raw(["complete", run_id])
    migration.rollback(
        run_id, legacy_writer_compatible=False,
        proof={"legacy_protocol": "unsupported", "drill": "recorded"},
    )
    raw(["abort", run_id])
    with store.read_transaction() as tx:
        row = tx.execute("SELECT state,current_phase FROM runs WHERE id=?", (run_id,)).fetchone()
    assert (row["state"], row["current_phase"]) == ("active", None)

    standalone = tmp_path / "standalone.db"
    run = RunStore(standalone).create_run(skill="fix", objective="still standalone")
    monkeypatch.setenv("RUN_STATE_DB", str(standalone))
    assert cli_main(["start", "--skill", "fix", "--objective", "still standalone start"]) == 0
    capsys.readouterr()
    assert cli_main(["update", run, "--phase", "allowed"]) == 0
    assert RunStore(standalone).get_run(run).current_phase == "allowed"


def test_migration_capability_file_must_be_private(tmp_path: Path, capsys) -> None:
    legacy_path = tmp_path / "legacy-runs.db"
    _legacy_store(legacy_path)
    capability_file = _capability_file(tmp_path)
    capability_file.chmod(0o644)
    assert cli_main([
        "migration", "enroll", "--state-root", str(tmp_path / "authority"),
        "--registration-id", REGISTRATION, "--capability-file", str(capability_file),
        "--repository-id", "repo-fixture", "--source", f"run-store:{legacy_path}",
    ]) == 5
    assert json.loads(capsys.readouterr().out)["code"] == "MIGRATION_CAPABILITY_FILE_UNSAFE"
