"""Existing real wave transport exercises the additive integration journal."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from run_state import integration_journal as journal
from run_state.ownership import OwnershipRefused
from test_wave_consumer import wave_fixture


def test_completed_publication_retains_issuing_authority_without_reopening_launch(tmp_path, monkeypatch):
    with wave_fixture(tmp_path, monkeypatch, plans=1,
                      outer_command=(sys.executable, '-c', 'import time; time.sleep(12)')) as f:
        assert f.consumer(f.event)['results'][0]['status'] == 'complete'
        key = f'gsd-wave:{f.event}'
        initial = journal.read_intent(f.store, f.supervisor.token, wave_key=key)
        result = f.supervisor.finish(f.outer, timeout=20)
        assert result['returncode'] == 0
        with f.store.read_transaction() as tx:
            terminal = json.loads(tx.execute('SELECT completion_evidence_json FROM authority_launch_intents '
                                            'WHERE id=?', (f.outer.intent_id,)).fetchone()[0])
        f.store.transition_activity(f.supervisor.token, f.outer.activity_id,
                                    expected='active', new='succeeded', result=terminal, reason='fixture completed')
        with f.store.read_transaction() as tx:
            retained = journal.validate_completed_publication_tx(
                f.store, tx, f.supervisor.token, wave_key=key, intent_id=f.outer.intent_id)
            assert retained['journal'] == initial
            candidate = json.loads(tx.execute('SELECT e.payload FROM authority_event_keys k '
                'JOIN control_events e ON e.id=k.event_id WHERE k.activity_id=? AND k.idempotency_key=?',
                (f.outer.activity_id, key + ':candidate-output')).fetchone()[0])['data']
            assert candidate['input_digest'] != candidate['output_digest']
            assert candidate['journal_sha256'] == initial['contract_sha256']
            # Live effect authorization remains closed after completion.
            with pytest.raises(OwnershipRefused):
                journal.validate_journal_tx(f.store, tx, f.supervisor.token, initial)
        with f.store.transaction() as tx:
            tx.execute('UPDATE authority_launch_intents SET permit_id=NULL WHERE id=?', (f.outer.intent_id,))
        with f.store.read_transaction() as tx, pytest.raises(OwnershipRefused):
            journal.validate_completed_publication_tx(
                f.store, tx, f.supervisor.token, wave_key=key, intent_id=f.outer.intent_id)


def test_full_candidate_capture_refuses_unrelated_changes_after_wave_apply(tmp_path, monkeypatch):
    import run_state.wave_consumer as module
    original = module.integrate_wave_patches
    with wave_fixture(tmp_path, monkeypatch, plans=1) as f:
        def inject(*args, **kwargs):
            value = original(*args, **kwargs)
            (f.parent / 'unrelated.txt').write_text('not produced by any admitted patch')
            return value
        monkeypatch.setattr(module, 'integrate_wave_patches', inject)
        with pytest.raises(Exception, match='WAVE_CANDIDATE_UNRELATED_CHANGE'):
            f.consumer(f.event)
        retained = journal.read_intent(f.store, f.supervisor.token, wave_key=f'gsd-wave:{f.event}')
        assert retained['state'] == 'pending'


@pytest.mark.parametrize("revoke_after_apply", [False, True])
def test_pending_application_publishes_once_or_stays_fenced(tmp_path, monkeypatch, revoke_after_apply):
    import run_state.wave_consumer as consumer_module
    original = consumer_module.integrate_wave_patches
    with wave_fixture(tmp_path, monkeypatch, plans=1) as f:
        observed = []

        def integrate(*args, **kwargs):
            with f.store.transaction() as tx:
                preparation_id = tx.execute("SELECT workspace_preparation_id FROM authority_child_bindings "
                                            "WHERE activity_id=?", (f.outer.activity_id,)).fetchone()[0]
            after = {"result-0.txt": {"sha256": hashlib.sha256(b"done").hexdigest(), "git_mode": "100644"}}
            key = f"gsd-wave:{f.event}"
            pending = journal.read_intent(f.store, f.supervisor.token, wave_key=key)
            assert pending["state"] == "pending"
            with f.store.transaction() as tx, pytest.raises(OwnershipRefused, match="WORKSPACE_INTEGRATION_PENDING"):
                journal.assert_settled_tx(tx, preparation_id)
            applied = original(*args, **kwargs)
            evidence = {key: applied[key] for key in ("locator", "sha256")}
            if revoke_after_apply:
                f.store.transition_activity(f.supervisor.token, f.outer.activity_id,
                                             expected="active", new="failed", reason="revoked after apply")
                with pytest.raises(OwnershipRefused):
                    journal.publish(f.store, f.supervisor.token, wave_key=key,
                                    observed_after_material=after, evidence=evidence)
                assert journal.read_intent(f.store, f.supervisor.token, wave_key=key)["state"] == "pending"
                journal.quarantine(f.store, f.supervisor.token, wave_key=key, reason="publication revoked")
                observed.append("quarantined")
            else:
                published = journal.publish(f.store, f.supervisor.token, wave_key=key,
                                             observed_after_material=after, evidence=evidence)
                assert published["state"] == "published"
                assert journal.publish(f.store, f.supervisor.token, wave_key=key,
                                        observed_after_material=after, evidence=evidence) == published
                with f.store.transaction() as tx:
                    journal.assert_settled_tx(tx, preparation_id)
                observed.append("published")
            return applied

        monkeypatch.setattr(consumer_module, "integrate_wave_patches", integrate)
        if revoke_after_apply:
            with pytest.raises(Exception):
                f.consumer(f.event)
        else:
            assert f.consumer(f.event)["results"][0]["status"] == "complete"
        assert observed == ["quarantined" if revoke_after_apply else "published"]
        assert (f.parent / "result-0.txt").read_bytes() == b"done"


@pytest.mark.parametrize("mixed", [False, True])
def test_interrupted_apply_reconciles_exact_after_without_reapplying(tmp_path, monkeypatch, mixed):
    import run_state.wave_consumer as consumer_module
    from run_state.supervisor import SupervisorRefused

    class Interrupted(BaseException):
        pass

    original = consumer_module.integrate_wave_patches
    with wave_fixture(tmp_path, monkeypatch, plans=1) as f:
        calls = []

        def interrupted(*args, **kwargs):
            calls.append(1)
            original(*args, **kwargs)
            raise Interrupted()

        monkeypatch.setattr(consumer_module, "integrate_wave_patches", interrupted)
        with pytest.raises(Interrupted):
            f.consumer(f.event)
        key = f"gsd-wave:{f.event}"
        pending = journal.read_intent(f.store, f.supervisor.token, wave_key=key)
        assert pending["state"] == "pending"
        with pytest.raises(OwnershipRefused, match="WORKSPACE_INTEGRATION_PENDING"):
            f.store.assert_integration_settled(f.supervisor.token, str(f.parent))
        if mixed:
            (f.parent / "result-0.txt").write_bytes(b"unrecognized change")
        f.consumer.prepare_child = lambda *_: pytest.fail("recovery prepared another child")
        if mixed:
            with pytest.raises(SupervisorRefused, match="WAVE_INTEGRATION_RECONCILIATION_REQUIRED"):
                f.consumer(f.event)
            assert journal.read_intent(f.store, f.supervisor.token, wave_key=key)["state"] == "quarantined"
        else:
            assert f.consumer(f.event)["results"][0]["status"] == "complete"
            published = journal.read_intent(f.store, f.supervisor.token, wave_key=key)
            assert published["state"] == "published"
            for field in ("contract_sha256", "issuing_intent_id", "issuing_generation",
                          "acknowledgement_id", "permit_id"):
                assert published[field] == pending[field]
        assert calls == [1]


def test_workspace_effect_lock_is_cross_process_and_scoped(tmp_path):
    from run_context import ContextRefused, workspace_effect_lock
    common = tmp_path.resolve()
    script = """
import sys
from pathlib import Path
from run_context import workspace_effect_lock
with workspace_effect_lock(Path(sys.argv[1]), repository_id='repo', run_id='run', preparation_id='one'):
    print('locked', flush=True)
    sys.stdin.read(1)
"""
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "lib")}
    child = subprocess.Popen([sys.executable, "-c", script, str(common)], env=environment,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(ContextRefused, match="GIT_ADMIN_BUSY"):
            with workspace_effect_lock(common, repository_id="repo", run_id="run",
                                       preparation_id="one", timeout=.05):
                pytest.fail("concurrent same-workspace effect")
        with workspace_effect_lock(common, repository_id="repo", run_id="run",
                                   preparation_id="two", timeout=.05):
            pass
        files = {p.name: p.stat().st_ino for p in (common / "ffs").iterdir()}
        child.kill()
        child.wait(timeout=5)
        with workspace_effect_lock(common, repository_id="repo", run_id="run",
                                   preparation_id="one", timeout=.05):
            assert {p.name: p.stat().st_ino for p in (common / "ffs").iterdir()} == files
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
