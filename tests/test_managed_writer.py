"""Per-run writer binding stays immutable and rejects legacy handoff."""
from __future__ import annotations

import json

import pytest

from process_identity import ProcessIdentity
from run_state.ownership import OwnershipRefused, StartRequest, assert_owner, reserve_resources
from run_state.state import ControlStore


WRITER = "managed-gsd-v1"


def _registration(tmp_path, *, bind_writer: bool):
    authority = tmp_path / "authority"
    workspace = tmp_path / "workspace"
    authority.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    store = ControlStore(authority / "control.sqlite3")
    store.ensure_context_schema()
    store.ensure_authority_schema()
    with store.transaction() as tx:
        tx.execute(
            "INSERT INTO context_repositories "
            "(repository_id,marker_id,common_dir,filesystem_id,primary_root,workspace_root,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("repository", "marker", "/common", "filesystem", "/primary", "/workspaces", "now"),
        )
    owned = reserve_resources(store, StartRequest(
        "managed-run", str(workspace), "objective", ProcessIdentity.current(),
        repository_id="repository", planning_scope="scope",
    ))
    with store.transaction() as tx:
        assert_owner(tx, owned.token)
        tx.execute(
            "INSERT INTO authority_activities "
            "(id,repository_id,run_id,kind,input_digest,revision,state,retry_budget,"
            "remaining_retry_budget,runtime_tuple_hash,request_key,generation,created_at,updated_at) "
            "VALUES(?,?,?,?,?,1,'active',1,1,?,?,?,?,?)",
            ("activity", "repository", "managed-run", "execute", "a" * 64,
             None, "request", owned.token.generation, "now", "now"),
        )
        context_values = {
            "repository_id": "repository", "run_id": "managed-run", "objective_digest": "objective",
            "objective_text": "objective text", "planning_scope": "scope", "workspace": str(workspace),
            "workspace_key": str(workspace), "evidence_root": "/evidence", "state": "preparing",
            "generation": owned.token.generation, "activity_id": "activity", "activity_kind": "execute",
            "input_digest": "a" * 64, "request_key": "request", "request_digest": "digest",
            "upstream_json": "{}", "created_at": "now", "updated_at": "now",
        }
        if bind_writer:
            assert store.insert_context_run_with_writer(
                tx, owned.token, context_values=context_values, writer_version=WRITER,
            ) == WRITER
        else:
            tx.execute(
                "INSERT INTO context_runs "
                "(repository_id,run_id,objective_digest,objective_text,planning_scope,workspace,workspace_key,"
                "evidence_root,state,generation,activity_id,activity_kind,input_digest,request_key,request_digest,"
                "upstream_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(context_values.values()),
            )
    return store, owned


def test_registration_binds_immutable_writer_and_records_event(tmp_path):
    store, owned = _registration(tmp_path, bind_writer=True)
    assert store.assert_writer_version_before_ownership(
        repository_id="repository", run_id="managed-run", writer_version=WRITER,
    ) == WRITER
    with store.read_transaction() as tx:
        row = tx.execute(
            "SELECT writer_version FROM context_runs WHERE repository_id=? AND run_id=?",
            ("repository", "managed-run"),
        ).fetchone()
        event = tx.execute(
            "SELECT payload FROM control_events WHERE event_type='writer_version_bound'",
        ).fetchone()
    assert row["writer_version"] == WRITER
    assert json.loads(event["payload"])["data"] == {"writer_version": WRITER}
    with store.transaction() as tx:
        with pytest.raises(OwnershipRefused, match="WRITER_HANDOFF_REQUIRED"):
            store.insert_context_run_with_writer(
                tx, owned.token,
                context_values={
                    "repository_id": "repository", "run_id": "managed-run", "objective_digest": "objective",
                    "objective_text": "objective text", "planning_scope": "scope", "workspace": "/workspace",
                    "workspace_key": "/workspace", "evidence_root": "/evidence", "state": "preparing",
                    "generation": owned.token.generation, "activity_id": "activity", "activity_kind": "execute",
                    "input_digest": "a" * 64, "request_key": "request", "request_digest": "digest",
                    "upstream_json": "{}", "created_at": "now", "updated_at": "now",
                }, writer_version=WRITER,
            )


def test_existing_unbound_context_run_requires_writer_handoff_before_ownership(tmp_path):
    store, _owned = _registration(tmp_path, bind_writer=False)
    with pytest.raises(OwnershipRefused, match="WRITER_HANDOFF_REQUIRED"):
        store.assert_writer_version_before_ownership(
            repository_id="repository", run_id="managed-run", writer_version=WRITER,
        )


def test_crashed_pre_request_legacy_row_cannot_be_adopted(tmp_path):
    store, owned = _registration(tmp_path, bind_writer=False)
    with store.transaction() as tx:
        with pytest.raises(OwnershipRefused, match="WRITER_HANDOFF_REQUIRED"):
            store.insert_context_run_with_writer(
                tx, owned.token,
                context_values={
                    "repository_id": "repository", "run_id": "managed-run", "objective_digest": "objective",
                    "objective_text": "objective text", "planning_scope": "scope", "workspace": "/workspace",
                    "workspace_key": "/workspace", "evidence_root": "/evidence", "state": "preparing",
                    "generation": owned.token.generation, "activity_id": "activity", "activity_kind": "execute",
                    "input_digest": "a" * 64, "request_key": "request", "request_digest": "digest",
                    "upstream_json": "{}", "created_at": "now", "updated_at": "now",
                }, writer_version=WRITER,
            )


def test_legacy_context_schema_is_readable_before_additive_writer_migration(tmp_path):
    authority = tmp_path / "authority"
    authority.mkdir(mode=0o700)
    store = ControlStore(authority / "control.sqlite3")
    store.ensure_context_schema()
    with store.transaction() as tx:
        tx.execute("DROP INDEX context_active_objective")
        tx.execute("ALTER TABLE context_runs RENAME TO context_runs_legacy")
        tx.execute(
            "CREATE TABLE context_runs ("
            "repository_id TEXT NOT NULL,run_id TEXT NOT NULL,objective_digest TEXT NOT NULL,"
            "objective_text TEXT NOT NULL,planning_scope TEXT NOT NULL,workspace TEXT NOT NULL,"
            "workspace_key TEXT NOT NULL UNIQUE,evidence_root TEXT NOT NULL,state TEXT NOT NULL,"
            "generation INTEGER NOT NULL,activity_id TEXT NOT NULL,activity_kind TEXT NOT NULL,"
            "input_digest TEXT NOT NULL,request_key TEXT,request_digest TEXT,result_json TEXT,"
            "upstream_json TEXT NOT NULL,preparation_id TEXT,created_at TEXT NOT NULL,"
            "updated_at TEXT NOT NULL,PRIMARY KEY(repository_id,run_id))"
        )
        tx.execute("DROP TABLE context_runs_legacy")
    with pytest.raises(OwnershipRefused, match="WRITER_HANDOFF_REQUIRED"):
        store.assert_writer_version_before_ownership(
            repository_id="repository", run_id="legacy", writer_version=WRITER,
        )
    before = store.db_path.read_bytes()
    store.validate_context_schema()
    assert store.db_path.read_bytes() == before
    store.ensure_context_schema()
    with store.read_transaction() as tx:
        columns = {row[1] for row in tx.execute("PRAGMA table_info(context_runs)")}
    assert "writer_version" in columns
