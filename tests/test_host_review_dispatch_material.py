"""Artifact-review material reaches only the permitted child execve."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys

import pytest


LIB = Path(__file__).resolve().parents[1] / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from host_capabilities import build_artifact_review_material  # noqa: E402
from run_state.ownership import OwnershipRefused  # noqa: E402
from run_state.supervisor import SupervisorRefused  # noqa: E402
from run_state.workspace import (  # noqa: E402
    begin_child_workspace_preparation, inspect_workspace, parse_input_selection,
    prepare_workspace, snapshot_inputs, load_input_snapshot, WorkspaceRefused,
)
from test_m4_workspace_acceptance import _copy, _delete, _selection  # noqa: E402
from test_supervised_process import setup_owner  # noqa: E402


def _material(*, config_sha256: str = "a" * 64):
    content = "retained review input\\n"
    return build_artifact_review_material(
        host="claude", model_request={"kind": "tier", "name": "judgment"},
        config_sha256=config_sha256, policy_sha256="b" * 64,
        environment={"HOME": "/private/runtime", "PATH": "/usr/bin:/bin", "TMPDIR": "/private/scratch"},
        selected_artifacts={"selected-input.json": hashlib.sha256(content.encode()).hexdigest()},
        selected_contents={"selected-input.json": content},
        provenance={"workspace_identity": "1:2", "input_snapshot_sha256": "d" * 64},
    )


def _retained_selected_request(tmp_path, supervisor, store, request, content=b'exact "review" content\\n', *, entries=None, required_context=None):
    """Allocate a real selected capture for the positive transport oracle."""
    with store.read_transaction() as tx:
        row = tx.execute("SELECT workspace_preparation_id FROM authority_child_bindings WHERE activity_id=?", (
            request.activity_id,
        )).fetchone()
    parent = inspect_workspace(store, row["workspace_preparation_id"])
    primary = parent.repository_path
    (primary / "src" / "input.txt").write_bytes(content)
    snapshot = snapshot_inputs(
        primary, parse_input_selection(_selection(
            primary, supervisor.token.repository_id,
            entries=[_copy("src/input.txt", content)] if entries is None else entries,
            required_context=required_context,
        )), tmp_path / "retained-capture",
    )
    pending = begin_child_workspace_preparation(
        store, supervisor.token, parent_activity_id=parent.parent_activity_id,
        request_key="review-content-workspace", role="worker", base_commit=parent.base_commit,
        selected_input_manifest=snapshot.manifest, repository_path=primary,
    )
    ready = prepare_workspace(store, supervisor.token, pending, input_snapshot=snapshot)
    child = store.create_child_activity(
        supervisor.token, parent_activity_id=parent.parent_activity_id, role="worker",
        request_key="review-content-child", candidate_hash=ready.input_digest,
        contract_hash="d" * 64, runtime_identity="b" * 64, workspace_binding=str(ready.path),
        workspace_preparation_id=ready.id,
    )
    return replace(request, activity_id=child.id, request_key="review-content-launch",
                   workspace=str(ready.path), expected_head=ready.base_commit), content


def _unresolved(content, path="src/input.txt"):
    return build_artifact_review_material(
        host="claude", model_request={"kind": "tier", "name": "judgment"},
        config_sha256="a" * 64, policy_sha256="b" * 64,
        environment={"HOME": "/private/runtime", "PATH": "/usr/bin:/bin", "TMPDIR": "/private/scratch"},
        selected_artifacts={path: hashlib.sha256(content).hexdigest()}, provenance={"caller": "untrusted"},
    )


def _snapshot_for(store, request):
    with store.read_transaction() as tx:
        row = tx.execute("SELECT workspace_preparation_id FROM authority_child_bindings WHERE activity_id=?", (request.activity_id,)).fetchone()
    return load_input_snapshot(store, inspect_workspace(store, row["workspace_preparation_id"]))


def _durable_request(store, request):
    with store.read_transaction() as tx:
        row = tx.execute(
            "SELECT e.payload FROM control_events e JOIN authority_event_keys k ON e.id=k.event_id "
            "WHERE k.activity_id=? AND k.idempotency_key=?",
            (request.activity_id, "dispatch-request:" + request.request_key),
        ).fetchone()
    return json.loads(row["payload"])["data"]["request"]


def _authority_snapshot(store):
    with store.read_transaction() as tx:
        return {
            table: [tuple(row) for row in tx.execute("SELECT * FROM " + table)]
            for table in ("control_events", "authority_launch_intents",
                          "authority_launch_accounting", "authority_run_limits")
        }


def test_permitted_child_receives_only_closed_review_environment_and_prompt(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)
    material = _material()
    forged = replace(material, environment=material.environment + (("PYTHONPATH", "/unsafe"),))
    with pytest.raises((SupervisorRefused, WorkspaceRefused)):
        supervisor.launch(replace(request, host_material=forged))
    with store.read_transaction() as tx:
        limits = tx.execute("SELECT dispatch_used FROM authority_run_limits").fetchone()[0]
        intents = tx.execute("SELECT count(*) FROM authority_launch_intents").fetchone()[0]
    assert (limits, intents) == (0, 0)
    request, raw_content = _retained_selected_request(tmp_path, supervisor, store, request)
    content = raw_content.decode()
    material = _unresolved(raw_content)
    (Path(request.workspace) / "src" / "input.txt").write_text("mutable child sentinel\n")
    (tmp_path / "primary" / "src" / "input.txt").write_text("primary sentinel\n")
    request = replace(request, host_material=material, command=("/usr/bin/env", "-0"))
    handle = supervisor.launch(request)
    result = supervisor.finish(handle, timeout=10, token_usage=0)
    assert result["returncode"] == 0
    observed_environment = dict(line.decode().split("=", 1) for line in Path(
        result["streams"]["stdout"]["locator"]).read_bytes().split(b"\0") if line)
    assert set(observed_environment) == {"HOME", "PATH", "TMPDIR", "FFS_ARTIFACT_REVIEW_PROMPT"}
    prompt = json.loads(observed_environment["FFS_ARTIFACT_REVIEW_PROMPT"].split("\n", 1)[1])
    assert prompt["artifacts"] == [{
        "contents": content, "encoding": "utf-8", "name": "src/input.txt",
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
    }]
    durable = _durable_request(store, request)
    assert "host_material" in durable and content not in json.dumps(durable)
    with pytest.raises(OwnershipRefused, match="IDEMPOTENCY_CONFLICT"):
        supervisor.launch(replace(request, host_material=replace(material, config_sha256="e" * 64)))


def test_oversized_factory_material_refuses_before_debit_or_spawn(tmp_path, monkeypatch):
    supervisor, store, request = setup_owner(tmp_path)
    request, raw_content = _retained_selected_request(tmp_path, supervisor, store, request)
    content = raw_content.decode()
    long_absolute = "/" + "é" * 4095
    material = build_artifact_review_material(
        host="claude", model_request={"kind": "tier", "name": "judgment"},
        config_sha256="a" * 64, policy_sha256="b" * 64,
        environment={"HOME": long_absolute, "PATH": long_absolute, "TMPDIR": long_absolute},
        selected_artifacts={"src/input.txt": hashlib.sha256(content.encode()).hexdigest()},
        selected_contents={"src/input.txt": content}, provenance={"snapshot_sha256": "d" * 64},
    )
    before = _authority_snapshot(store)

    from run_state import supervisor as supervisor_module
    original_popen = supervisor_module.subprocess.Popen

    def forbid_child_spawn(argv, *args, **kwargs):
        if argv[:3] == [sys.executable, "-m", "run_state.supervisor"]:
            raise AssertionError("oversized material reached child Popen")
        return original_popen(argv, *args, **kwargs)

    monkeypatch.setattr("run_state.supervisor.subprocess.Popen", forbid_child_spawn)
    with pytest.raises(SupervisorRefused, match="HOST_MATERIAL_TOO_LARGE"):
        supervisor.launch(replace(request, host_material=material, command=("/usr/bin/true",)))
    assert _authority_snapshot(store) == before


@pytest.mark.parametrize("fault", ["tamper", "missing", "unselected", "invalid_utf8"])
def test_capture_authority_refusals_precede_debit_and_spawn(tmp_path, monkeypatch, fault):
    supervisor, store, request = setup_owner(tmp_path)
    source = b"\xff" if fault == "invalid_utf8" else b"retained authority\n"
    request, source = _retained_selected_request(tmp_path, supervisor, store, request, source)
    material = _unresolved(source, "outside.txt" if fault == "unselected" else "src/input.txt")
    capture = _snapshot_for(store, request).staging / "files" / "src" / "input.txt"
    if fault == "tamper":
        capture.write_bytes(b"tampered\n")
    elif fault == "missing":
        capture.unlink()
    before = _authority_snapshot(store)
    from run_state import supervisor as module
    original = module.subprocess.Popen
    def no_child(argv, *args, **kwargs):
        if argv[:3] == [sys.executable, "-m", "run_state.supervisor"]:
            raise AssertionError("spawn")
        return original(argv, *args, **kwargs)
    monkeypatch.setattr(module.subprocess, "Popen", no_child)
    with pytest.raises((SupervisorRefused, WorkspaceRefused)):
        supervisor.launch(replace(request, host_material=material, command=("/usr/bin/true",)))
    assert _authority_snapshot(store) == before


def _forbid_review_child(monkeypatch):
    from run_state import supervisor as module
    original = module.subprocess.Popen

    def no_child(argv, *args, **kwargs):
        if argv[:3] == [sys.executable, "-m", "run_state.supervisor"]:
            raise AssertionError("refused review reached child Popen")
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(module.subprocess, "Popen", no_child)


def test_capture_leaf_replaced_after_verified_read_refuses_before_reservation(tmp_path, monkeypatch):
    supervisor, store, request = setup_owner(tmp_path)
    request, source = _retained_selected_request(tmp_path, supervisor, store, request)
    snapshot = _snapshot_for(store, request)
    capture = snapshot.staging / "files" / "src" / "input.txt"
    from run_state import supervisor as module
    original = module._read_anchored_regular_metadata
    replaced = []

    def replace_after_read(root, relative, **kwargs):
        result = original(root, relative, **kwargs)
        if root == snapshot.staging / "files" and relative == "src/input.txt":
            replacement = capture.with_name("replacement.txt")
            replacement.write_bytes(b"unselected replacement sentinel\n")
            replacement.replace(capture)
            replaced.append(True)
        return result

    monkeypatch.setattr(module, "_read_anchored_regular_metadata", replace_after_read)
    _forbid_review_child(monkeypatch)
    before = _authority_snapshot(store)
    with pytest.raises(SupervisorRefused, match="HOST_MATERIAL_INVALID"):
        supervisor.launch(replace(request, host_material=_unresolved(source)))
    assert replaced == [True]
    assert _authority_snapshot(store) == before
    assert not (Path(request.workspace) / "ran").exists()


@pytest.mark.parametrize("mixed", [False, True])
def test_requested_deletion_cannot_be_reviewed_as_empty_content(tmp_path, monkeypatch, mixed):
    supervisor, store, request = setup_owner(tmp_path)
    base = b"base input\n"
    selected = b"copied review content\n"
    entries = [_delete("src/input.txt", base)]
    artifacts = {"src/input.txt": hashlib.sha256(base).hexdigest()}
    if mixed:
        (tmp_path / "primary/src/review.txt").write_bytes(selected)
        entries.append(_copy("src/review.txt", selected))
        artifacts["src/review.txt"] = hashlib.sha256(selected).hexdigest()
    request, _ = _retained_selected_request(
        tmp_path, supervisor, store, request, base, entries=entries,
    )
    material = build_artifact_review_material(
        host="claude", model_request={"kind": "tier", "name": "judgment"},
        config_sha256="a" * 64, policy_sha256="b" * 64,
        environment={"HOME": "/private/runtime", "PATH": "/usr/bin:/bin", "TMPDIR": "/private/scratch"},
        selected_artifacts=artifacts, provenance={"caller": "untrusted"},
    )
    assert not (Path(request.workspace) / "src/input.txt").exists()
    _forbid_review_child(monkeypatch)
    before = _authority_snapshot(store)
    with pytest.raises(SupervisorRefused, match="HOST_MATERIAL_INVALID"):
        supervisor.launch(replace(request, host_material=material))
    assert _authority_snapshot(store) == before


@pytest.mark.parametrize("context_kind", ["unavailable", "delete", "omitted_copy"])
def test_required_context_must_be_available_and_requested(tmp_path, monkeypatch, context_kind):
    supervisor, store, request = setup_owner(tmp_path)
    base = b"base input\n"
    selected = b"review content\n"
    (tmp_path / "primary/src/review.txt").write_bytes(selected)
    entries = [_copy("src/review.txt", selected)]
    if context_kind == "delete":
        entries.append(_delete("src/input.txt", base))
    elif context_kind == "omitted_copy":
        entries.append(_copy("src/input.txt", base))
    request, _ = _retained_selected_request(
        tmp_path, supervisor, store, request, base, entries=entries,
        required_context=[{"path": "src/input.txt", "reason": "necessary retained context"}],
    )
    _forbid_review_child(monkeypatch)
    before = _authority_snapshot(store)
    with pytest.raises(SupervisorRefused, match="HOST_MATERIAL_INVALID"):
        supervisor.launch(replace(request, host_material=_unresolved(selected, "src/review.txt")))
    assert _authority_snapshot(store) == before


def test_populated_caller_content_must_match_selected_capture(tmp_path, monkeypatch):
    supervisor, store, request = setup_owner(tmp_path)
    request, source = _retained_selected_request(tmp_path, supervisor, store, request)
    material = _unresolved(source)
    material = replace(material, selected_contents=(("src/input.txt", "different caller text"),))
    _forbid_review_child(monkeypatch)
    before = _authority_snapshot(store)
    with pytest.raises(SupervisorRefused, match="HOST_MATERIAL_INVALID"):
        supervisor.launch(replace(request, host_material=material))
    assert _authority_snapshot(store) == before


def test_metadata_v1_durable_request_is_not_silently_upgraded_or_redebited(tmp_path, monkeypatch):
    supervisor, store, request = setup_owner(tmp_path)
    request, source = _retained_selected_request(tmp_path, supervisor, store, request)
    material = _unresolved(source)
    from run_state import supervisor as module
    legacy_binding = material.replay_binding()
    legacy_binding["schema"] = "ffs.artifact-review-material/v1"
    legacy_payload = {
        "command_sha256": hashlib.sha256(module._canonical(request.command)).hexdigest(),
        "workspace": request.workspace, "expected_head": request.expected_head,
        "runtime_identity": request.runtime_identity, "contract_hash": request.contract_hash,
        "host_material": legacy_binding,
    }
    old_intent = store.reserve_launch(
        request.activity_id, supervisor.token, token_reservation=17,
        request_key=request.request_key, request_payload=legacy_payload,
    )
    request = replace(request, host_material=material, token_reservation=17)
    before = _authority_snapshot(store)
    _forbid_review_child(monkeypatch)
    with pytest.raises(OwnershipRefused, match="IDEMPOTENCY_CONFLICT"):
        supervisor.launch(request)
    assert _authority_snapshot(store) == before
    assert _durable_request(store, request)["host_material"]["schema"] == "ffs.artifact-review-material/v1"
    with store.read_transaction() as tx:
        intents = tx.execute("SELECT id FROM authority_launch_intents").fetchall()
        assert [row["id"] for row in intents] == [old_intent.id]
        assert tx.execute("SELECT dispatch_used FROM authority_run_limits").fetchone()[0] == 1
