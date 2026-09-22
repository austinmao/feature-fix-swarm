"""Durable authority for qualified-runtime receipts and managed dispatch."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from host_capabilities import QualifiedCodexRuntime, TELEMETRY_SCHEMA  # noqa: E402
from run_state.claude_host import QualifiedClaudeRuntime  # noqa: E402
from process_identity import ProcessIdentity  # noqa: E402
from run_state.ownership import OwnershipRefused, StartRequest, reserve_resources  # noqa: E402
from run_state.state import ControlStore, qualified_runtime_tuple_hash  # noqa: E402


INPUT_SHA = "a" * 64


def _qualified(workspace, *, age: float = 0, boot_id: str | None = None):
    identity = ProcessIdentity.current()
    info = workspace.stat()
    return QualifiedCodexRuntime(
        binary=(("launcher_sha256", "b" * 64),),
        runtime=(("agents_sha256", "1" * 64), ("config_sha256", "c" * 64),
                 ("device", info.st_dev), ("hooks_sha256", "2" * 64),
                 ("inode", info.st_ino), ("path", str(workspace)),
                 ("skills_sha256", "3" * 64), ("gsd_core_sha256", "4" * 64),
                 ("scripts_sha256", "5" * 64), ("gsd_manifest_sha256", "6" * 64)),
        workspace=(("device", info.st_dev), ("inode", info.st_ino), ("path", str(workspace))),
        supervisor=(("boot_id", boot_id or identity.boot_id), ("host_id", identity.host_id),
                    ("pid", identity.pid), ("start_token", identity.start_token)),
        execution=(("disabled_features", ["multi_agent", "multi_agent_v2"]),
                   ("effort", "medium"), ("model", "fixture"),
                   ("network_enabled", False), ("roots", [str(workspace)]),
                   ("sandbox", "workspace-write")),
        observation=(("created_at_unix", time.time() - age),
                     ("environment_sha256", "d" * 64), ("id", "e" * 32),
                     ("telemetry_schema", TELEMETRY_SCHEMA)),
    )


def _qualified_claude(workspace, *, boot_id: str | None = None):
    identity, info = ProcessIdentity.current(), workspace.stat()
    return QualifiedClaudeRuntime(
        binary=(("launcher_sha256", "b" * 64),),
        runtime=(("device", info.st_dev), ("inode", info.st_ino), ("path", str(workspace)),
                 ("settings_sha256", "c" * 64), ("stage_sha256", "d" * 64)),
        workspace=(("device", info.st_dev), ("inode", info.st_ino), ("path", str(workspace))),
        supervisor=(("boot_id", boot_id or identity.boot_id), ("host_id", identity.host_id),
                    ("pid", identity.pid), ("start_token", identity.start_token)),
        execution=(("effort", None), ("model", "claude-opus-5"),
                   ("network_enabled", False), ("roots", [str(workspace)]),
                   ("sandbox", "workspace-write"),
                   ("tools", "Bash,Edit,Glob,Grep,Read,Skill,Write")),
        observation=(
            ("auth_negative", True), ("environment_sha256", "e" * 64),
            ("envelope_sha256", "f" * 64), ("evidence_sha256", "0" * 64),
            ("hook_events", ["PostToolUse:Bash", "PreToolUse:Bash"]), ("nested_auth_denied", True),
            ("probe_contracts", {name: char * 64 for name, char in (
                ("auth-negative", "1"), ("session-model", "2"),
                ("sandbox-hooks", "3"), ("nested-auth", "4"),
            )}),
            ("sandbox_write_boundary", True), ("schema", "ffs.claude-runtime-qualification/v1"),
            ("stream_sha256", {name: char * 64 for name, char in (
                ("session-model", "5"), ("sandbox-hooks", "6"), ("nested-auth", "7"),
            )}), ("version", "2.1.274"),
        ),
    )


def _managed_store(tmp_path):
    authority = tmp_path / "authority"
    workspace = tmp_path / "workspace"
    authority.mkdir(mode=0o700, parents=True)
    workspace.mkdir(mode=0o700)
    store = ControlStore(authority / "control.sqlite3")
    store.ensure_context_schema()
    store.ensure_authority_schema()
    with store.transaction() as tx:
        tx.execute(
            "INSERT INTO context_repositories "
            "(repository_id,marker_id,common_dir,filesystem_id,primary_root,workspace_root,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("repository", "marker", "/common", "filesystem", str(workspace), str(workspace), "now"),
        )
    owned = reserve_resources(store, StartRequest(
        "run", str(workspace), "objective", ProcessIdentity.current(),
        repository_id="repository", planning_scope="scope",
    ))
    info = workspace.stat()
    with store.transaction() as tx:
        tx.execute(
            "INSERT INTO authority_activities "
            "(id,repository_id,run_id,kind,input_digest,revision,state,retry_budget,"
            "remaining_retry_budget,runtime_tuple_hash,request_key,generation,created_at,updated_at) "
            "VALUES(?,?,?,?,?,1,'active',2,2,NULL,?,?,?,?)",
            ("activity", "repository", "run", "execute", INPUT_SHA,
             "activity-request", owned.token.generation, "now", "now"),
        )
        tx.execute(
            "INSERT INTO context_workspaces "
            "(preparation_id,repository_id,run_id,path,path_key,branch,branch_key,base_commit,"
            "repository_path,common_dir,selected_manifest_json,selected_manifest_hash,"
            "path_existed_before,branch_existed_before,registered_before,created_by_ffs,"
            "generation,state,owned_manifest,parent_preparation_id,parent_activity_id,child_role,"
            "child_request_key,native_identity_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,1,?,'ready',NULL,NULL,NULL,NULL,NULL,?,?,?)",
            ("preparation", "repository", "run", str(workspace), str(workspace),
             "branch", "branch", "f" * 40, str(workspace), "/common", "{}", INPUT_SHA,
             owned.token.generation, json.dumps([info.st_dev, info.st_ino]), "now", "now"),
        )
        tx.execute(
            "INSERT INTO context_runs "
            "(repository_id,run_id,objective_digest,objective_text,planning_scope,workspace,"
            "workspace_key,evidence_root,state,generation,activity_id,activity_kind,input_digest,"
            "request_key,request_digest,writer_version,result_json,upstream_json,preparation_id,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("repository", "run", "objective", "objective", "scope", str(workspace),
             str(workspace), str(tmp_path / "evidence"), "ready", owned.token.generation,
             "activity", "execute", INPUT_SHA, "run-request", INPUT_SHA,
             "ffs-supervisor/1", None, "{}", "preparation", "now", "now"),
        )
    store.configure_run_limits(
        owned.token, dispatch_limit=4, token_limit=100, worker_capacity=2,
    )
    return store, owned.token, workspace


def _reserve(store, token, receipt, *, request_key="dispatch"):
    return store.reserve_launch(
        "activity", token, token_reservation=7, request_key=request_key,
        request_payload={"command_sha256": "9" * 64},
        runtime_receipt_sha256=receipt.receipt_sha256,
        managed_input_sha256=INPUT_SHA,
    )


def test_stable_tuple_omits_observation_nonce_time_and_supervisor_process(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = _qualified(workspace)
    changed = replace(
        original,
        supervisor=tuple((key, value + 1 if key == "pid" else
                          "different" if key == "start_token" else value)
                         for key, value in original.supervisor),
        observation=tuple((key, value - 1 if key == "created_at_unix" else
                           "f" * 32 if key == "id" else value)
                          for key, value in original.observation),
    )
    assert qualified_runtime_tuple_hash(original) == qualified_runtime_tuple_hash(changed)
    assert original.to_dict() != changed.to_dict()


@pytest.mark.parametrize("defect", ["stale", "workspace", "boot"])
def test_commit_refuses_stale_or_wrong_runtime_identity(tmp_path, defect):
    store, token, workspace = _managed_store(tmp_path)
    if defect == "stale":
        qualified = _qualified(workspace, age=901)
    elif defect == "workspace":
        other = tmp_path / "other"
        other.mkdir()
        qualified = _qualified(other)
    else:
        qualified = _qualified(workspace, boot_id="wrong-boot")
    expected = {
        "stale": "RUNTIME_RECEIPT_STALE",
        "workspace": "WORKSPACE_BINDING_MISMATCH",
        "boot": "RUNTIME_HOST_MISMATCH",
    }[defect]
    with pytest.raises(OwnershipRefused, match=expected):
        store.commit_runtime_receipt(token, "activity", qualified)


def test_commit_is_idempotent_and_binds_canonical_receipt(tmp_path):
    store, token, workspace = _managed_store(tmp_path)
    qualified = _qualified(workspace)
    first = store.commit_runtime_receipt(token, "activity", qualified)
    second = store.commit_runtime_receipt(token, "activity", qualified)
    assert second == replace(first, reused=True)
    assert len(first.receipt_json.encode()) <= 64 * 1024
    assert json.dumps(json.loads(first.receipt_json), sort_keys=True, separators=(",", ":")) == first.receipt_json
    assert hashlib.sha256(first.receipt_json.encode()).hexdigest() == first.receipt_sha256
    with store.read_transaction() as tx:
        count = tx.execute("SELECT COUNT(*) FROM authority_runtime_receipts").fetchone()[0]
        activity = tx.execute(
            "SELECT runtime_tuple_hash FROM authority_activities WHERE id='activity'"
        ).fetchone()
    assert count == 1
    assert activity["runtime_tuple_hash"] == first.runtime_tuple_hash


def test_claude_receipt_requires_exact_runtime_type_and_establishes_durable_window(tmp_path):
    store, token, workspace = _managed_store(tmp_path)
    qualified = _qualified_claude(workspace)
    first = store.commit_runtime_receipt(token, "activity", qualified)
    assert store.commit_runtime_receipt(token, "activity", qualified).reused is True
    assert first.runtime_tuple_hash == qualified_runtime_tuple_hash(qualified)
    assert json.loads(first.receipt_json)["schema"] == "ffs.qualified-claude-runtime/v1"
    with store.read_transaction() as tx:
        row = tx.execute(
            "SELECT observed_at,expires_at FROM authority_runtime_receipts WHERE receipt_sha256=?",
            (first.receipt_sha256,),
        ).fetchone()
    observed = datetime.strptime(row["observed_at"], "%Y-%m-%dT%H:%M:%SZ")
    expires = datetime.strptime(row["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert (expires - observed).total_seconds() == 15 * 60

    with pytest.raises(OwnershipRefused, match="RUNTIME_RECEIPT_INVALID"):
        store.commit_runtime_receipt(token, "activity", qualified.to_dict())


def test_managed_dispatch_refuses_stale_and_tampered_receipts(tmp_path):
    store, token, workspace = _managed_store(tmp_path)
    receipt = store.commit_runtime_receipt(token, "activity", _qualified(workspace))
    with pytest.raises(OwnershipRefused, match="RUNTIME_RECEIPT_REQUIRED"):
        store.reserve_launch(
            "activity", token, token_reservation=7, request_key="missing-receipt",
            request_payload={},
        )
    with pytest.raises(OwnershipRefused, match="MANAGED_INPUT_MISMATCH"):
        store.reserve_launch(
            "activity", token, token_reservation=7, request_key="wrong-input",
            request_payload={}, runtime_receipt_sha256=receipt.receipt_sha256,
            managed_input_sha256="0" * 64,
        )
    expires = datetime.strptime(receipt.expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    store._now = lambda: (expires + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with pytest.raises(OwnershipRefused, match="RUNTIME_RECEIPT_STALE"):
        _reserve(store, token, receipt)

    store, token, workspace = _managed_store(tmp_path / "tamper")
    receipt = store.commit_runtime_receipt(token, "activity", _qualified(workspace))
    payload = json.loads(receipt.receipt_json)
    payload["observation"]["environment_sha256"] = "0" * 64
    with store.transaction() as tx:
        tx.execute(
            "UPDATE authority_runtime_receipts SET receipt_json=? WHERE receipt_sha256=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), receipt.receipt_sha256),
        )
    with pytest.raises(OwnershipRefused, match="RUNTIME_RECEIPT_INVALID"):
        _reserve(store, token, receipt)


def test_exact_dispatch_replay_survives_receipt_expiry_and_retains_hash_evidence(tmp_path):
    store, token, workspace = _managed_store(tmp_path)
    receipt = store.commit_runtime_receipt(token, "activity", _qualified(workspace))
    first = _reserve(store, token, receipt)
    expires = datetime.strptime(receipt.expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    store._now = lambda: (expires + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    replay = _reserve(store, token, receipt)
    assert replay.id == first.id
    assert replay.reused is True
    with store.read_transaction() as tx:
        event = tx.execute(
            "SELECT payload FROM control_events WHERE event_type='dispatch-request:dispatch'"
        ).fetchone()
    evidence = json.loads(event["payload"])["data"]
    assert evidence["runtime_receipt_sha256"] == receipt.receipt_sha256
    assert evidence["managed_input_sha256"] == INPUT_SHA


def test_concurrent_exact_reservation_debits_once(tmp_path):
    store, token, workspace = _managed_store(tmp_path)
    receipt = store.commit_runtime_receipt(token, "activity", _qualified(workspace))
    barrier = threading.Barrier(2)
    results = []

    def reserve():
        barrier.wait()
        results.append(_reserve(store, token, receipt))

    threads = [threading.Thread(target=reserve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({intent.id for intent in results}) == 1
    assert sorted(intent.reused for intent in results) == [False, True]
    with store.read_transaction() as tx:
        limits = tx.execute(
            "SELECT dispatch_used,token_committed FROM authority_run_limits"
        ).fetchone()
    assert tuple(limits) == (1, 7)
