"""Local check confinement effects and unsupported-platform refusal."""
from dataclasses import replace
from pathlib import Path
import shlex
import sys
import socket
from types import SimpleNamespace

import pytest

from run_state.managed import build_frontend_acceptance_draft
from run_state.supervisor import SupervisorRefused
from test_supervised_process import setup_owner


def _sealed_command_check(store, token, request, locator=None):
    with store.transaction() as tx:
        tx.execute("UPDATE context_runs SET writer_version='ffs-supervisor/1', objective_digest=?, "
                   "input_digest=?, request_key=?, request_digest=? WHERE repository_id=? AND run_id=?",
                   ("a" * 64, "b" * 64, "managed-request", "c" * 64, token.repository_id, token.run_id))
        tx.execute("INSERT OR REPLACE INTO context_requests(repository_id,request_key,request_digest,run_id,created_at) "
                   "VALUES(?,?,?,?,?)", (token.repository_id, "managed-request", "c" * 64, token.run_id, "fixture"))
    legacy = store.create_initial_acceptance_contract(token, accepted_requirement_ids=["REQ-local"])
    with store.read_transaction() as tx:
        child = tx.execute("SELECT candidate_hash FROM authority_child_bindings WHERE activity_id=?", (request.activity_id,)).fetchone()
    material = build_frontend_acceptance_draft(
        objective_digest=legacy.material["objective_digest"],
        criteria=[{"id": "REQ-local", "objective_clause": "local command",
                   "checks": [{"id": "real-local", "kind": "command",
                               "locator": locator or shlex.join((sys.executable, "-c", "print('must not run')"))}],
                   "evidence_rules": [{"id": "local-output", "kind": "log", "required": True}]}],
        exclusions=[{"id": "no-extra", "reason": "fixture"}],
        global_invariants=[{"id": "no-commit", "reason": "fixture"}],
        requested_runtime_hash=request.runtime_identity, effective_runtime_hash=request.runtime_identity,
        candidate_hash=child["candidate_hash"], generation=legacy.generation, command_mode="feature-implement",
    )
    store.create_acceptance_draft(token, draft_id="local", revision=1,
                                  acceptance_contract_hash=legacy.contract_hash, material=material)
    sealed = store.seal_acceptance_draft(token, draft_id="local", revision=1,
                                         acceptance_contract_hash=legacy.contract_hash)
    with store.transaction() as tx:
        tx.execute("UPDATE authority_child_bindings SET contract_hash=? WHERE activity_id=?",
                   (sealed.acceptance_hash, request.activity_id))
    return sealed


def test_unsupported_platform_refuses_before_receipt_intent_or_pid(tmp_path, monkeypatch):
    supervisor, store, request = setup_owner(tmp_path)
    sealed = _sealed_command_check(store, supervisor.token, request)
    request = replace(request, contract_hash=sealed.acceptance_hash)
    monkeypatch.setattr('run_state.local_check_runtime.sys', SimpleNamespace(platform='linux'))
    with pytest.raises(SupervisorRefused, match="LOCAL_CHECK_CONFINEMENT_UNAVAILABLE"):
        supervisor.launch_sealed_check(request, acceptance_hash=sealed.acceptance_hash, check_id="real-local")
    with store.read_transaction() as tx:
        assert tx.execute("SELECT count(*) FROM authority_local_check_receipts").fetchone()[0] == 0
        assert tx.execute("SELECT count(*) FROM authority_launch_intents").fetchone()[0] == 0


@pytest.mark.skipif(sys.platform != 'darwin', reason='actual Darwin sandbox effects')
def test_registered_local_policy_denies_writes_escape_network_and_fork(tmp_path):
    from run_state.local_check_runtime import sealed_check_material, build_confined_local_argv
    from test_artifact_review_containment import _build_probe, _run
    supervisor, store, request = setup_owner(tmp_path)
    runtime = tmp_path / 'native-probe'
    runtime.mkdir(mode=0o700)
    executable = _build_probe(runtime)
    sealed = _sealed_command_check(store, supervisor.token, request, shlex.join((executable, 'thread')))
    with store.read_transaction() as tx:
        child = tx.execute('SELECT * FROM authority_child_bindings WHERE activity_id=?',
                           (request.activity_id,)).fetchone()
    material = sealed_check_material(sealed=sealed, acceptance_hash=sealed.acceptance_hash,
        check_id='real-local', candidate_hash=child['candidate_hash'], workspace=request.workspace,
        workspace_preparation_id=child['workspace_preparation_id'], expected_head=request.expected_head,
        runtime_identity=request.runtime_identity, generation=supervisor.token.generation)
    bound, _argv, policy = build_confined_local_argv(store, supervisor.token, request.activity_id, material)
    scratch = Path(bound.confinement_scratch)
    artifact = Path(request.workspace) / 'artifact.txt'
    artifact.write_text('selected public bytes')
    secret = tmp_path / 'secret.txt'
    secret.write_text('fixture sentinel')
    def run(*args):
        return _run(policy, (executable, *args), scratch).returncode
    assert run('read', str(artifact)) == 0
    assert run('copy', str(artifact), str(scratch / 'copy.txt')) == 0
    assert run('write', str(artifact)) != 0
    assert artifact.read_text() == 'selected public bytes'
    assert run('read', str(secret)) != 0
    assert run('symlink-read', str(scratch / 'escape'), str(tmp_path)) != 0
    assert run('fork') != 0
    assert run('exec', '/usr/bin/true') != 0
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        assert run('network', str(listener.getsockname()[1])) != 0
