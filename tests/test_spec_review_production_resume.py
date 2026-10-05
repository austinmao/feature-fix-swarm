"""spec-014 E8 prerequisite 3c, review R1-2: what the PRODUCTION path does when a spec review's owner dies.

Unlike ``test_spec_review_crash_points`` (whose host seam replays a retained qualification without staging), this
drives the real ``prepare_managed_codex_session`` closure, real private runtime staging with its strict reuse
validation and the real ``qualify_managed_runtime``; only the Codex observer, the host binary and the admission
observation are scripted (``test_final_review_resume_sigkill``).  A child process is SIGKILLed once the spec reviewer
is qualified, and the identical request is resumed under a new owner.

The outer orchestrator was qualified before the review began and never launched, so its private runtime home already
holds qualification evidence: the resume is refused ``RETAINED_RUNTIME_NOT_REUSABLE`` (new request key) while the
home is staged again, before ``drive_managed_session`` runs.  The re-fence of an unlaunched outer is never reached.
Fixture-level proof only: no Codex CLI runs.
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import run_state.frontend_producers as frontend_producers
from run_state import cli
from run_state.state import ControlStore
from recovery_fixture import host_script
from test_final_review_resume_sigkill import _argv, _real_host, _sigkilled_run
from test_managed_lifecycle_assembly import _draft, _last_envelope, _setup, requires_local_confinement

pytestmark = requires_local_confinement


def test_a_spec_review_crash_resumes_through_real_staging_to_a_typed_new_request_key_refusal(
        tmp_path, monkeypatch, capsys):
    primary, authority, repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    for key in ("GSD_RUN_ID", "FFS_RUN_ID", "GSD_RESUME"):
        monkeypatch.delenv(key, raising=False)
    mode = tmp_path / "mode.json"
    mode.write_text(json.dumps({"review": "passed", "spec_review": "accept"}))
    fake = tmp_path / "qualified-codex"
    fake.write_text(f"#!{sys.executable}\n" + host_script(mode))
    fake.chmod(0o700)
    template, fake, catalog = _real_host(tmp_path, monkeypatch)
    argv = _argv(env, authority, template, fake, catalog, _draft(tmp_path, spec_review="native"))
    # The reviewer is promoted; the spec review has reserved nothing and launched nothing.
    _sigkilled_run(tmp_path, primary, argv, "reviewer-qualified")

    store = ControlStore(authority / "control.sqlite3")
    with store.read_transaction() as tx:
        outer = tx.execute("SELECT a.id,a.generation FROM authority_activities a JOIN authority_child_bindings b "
                           "ON b.activity_id=a.id WHERE a.request_key LIKE 'managed-host:%'").fetchone()
        reviewers = [tuple(row) for row in tx.execute(
            "SELECT request_key,state FROM authority_activities WHERE request_key LIKE 'spec-review:%'")]
        launched = tx.execute("SELECT count(*) FROM authority_launch_intents WHERE capacity_exempt=1 AND NOT EXISTS "
                              "(SELECT 1 FROM authority_qualification_launches q WHERE q.intent_id="
                              "authority_launch_intents.id)").fetchone()[0]
    assert len(reviewers) == 1 and reviewers[0][0].startswith("spec-review:") and launched == 0
    assert store.get_sealed_acceptance(repository_id=repository_id, run_id="rk") is None

    reached = []
    real = frontend_producers.refence_unlaunched_outer
    monkeypatch.setattr(frontend_producers, "refence_unlaunched_outer",
                        lambda *args, **kwargs: reached.append(args) or real(*args, **kwargs))
    capsys.readouterr()
    assert cli.main(argv) == 78
    envelope = _last_envelope(capsys)
    assert (envelope["code"], envelope["recovery_action"]["action"]) == (
        "RETAINED_RUNTIME_NOT_REUSABLE", "resume_with_new_request_key")
    # Refused while the outer's home is staged again, before the lifecycle driver: nothing past it ran.
    assert reached == []
    with store.read_transaction() as tx:
        assert tx.execute("SELECT generation FROM authority_activities WHERE id=?",
                          (outer["id"],)).fetchone()[0] == outer["generation"]
        assert tx.execute("SELECT count(*) FROM authority_policy_actions WHERE action='spec_review'").fetchone()[0] == 0
    assert store.get_sealed_acceptance(repository_id=repository_id, run_id="rk") is None


# --- the base case: an owner that died after the seal and before the lifecycle state existed -------------------

_SEAL_DRIVER = '''import json, os, signal, sys
from pathlib import Path
import pytest
import test_final_review_resume_sigkill as harness
from run_state import cli
from run_state.state import ControlStore

config = json.loads(Path(sys.argv[1]).read_text())
patch = pytest.MonkeyPatch()
harness._real_host(Path(config["tmp_path"]), patch)
seal = ControlStore.seal_acceptance_draft


def seal_then_die(self, *args, **kwargs):
    result = seal(self, *args, **kwargs)
    os.kill(os.getpid(), signal.SIGKILL)  # sealed; the lifecycle state does not exist yet
    return result


patch.setattr(ControlStore, "seal_acceptance_draft", seal_then_die)
sys.exit(cli.main(config["argv"]))
'''


@pytest.mark.parametrize("native", [False, True], ids=["key-less", "opted-in"])
def test_a_crash_between_the_seal_and_the_lifecycle_state_never_falls_through_to_the_unsealed_launch(
        tmp_path, monkeypatch, capsys, native):
    """The ``state is None`` fall-through of ``drive_managed_session`` is behind ``prepare_outer``; production refuses first."""
    import os
    import signal
    import subprocess
    from pathlib import Path
    from test_final_review_resume_sigkill import ROOT
    primary, authority, repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    for key in ("GSD_RUN_ID", "FFS_RUN_ID", "GSD_RESUME"):
        monkeypatch.delenv(key, raising=False)
    mode = tmp_path / "mode.json"
    mode.write_text(json.dumps({"review": "passed", "spec_review": "accept"}))
    fake = tmp_path / "qualified-codex"
    fake.write_text(f"#!{sys.executable}\n" + host_script(mode))
    fake.chmod(0o700)
    template, fake, catalog = _real_host(tmp_path, monkeypatch)
    argv = _argv(env, authority, template, fake, catalog, _draft(tmp_path, spec_review="native" if native else None))
    driver, config = tmp_path / "seal-driver.py", tmp_path / "seal-driver.json"
    driver.write_text(_SEAL_DRIVER)
    config.write_text(json.dumps({"tmp_path": str(tmp_path), "argv": argv}))
    child_env = {key: value for key, value in os.environ.items()
                 if key not in {"GSD_RUN_ID", "FFS_RUN_ID", "GSD_RESUME", "PYTHONPATH"}}
    child_env["PYTHONPATH"] = os.pathsep.join((str(ROOT / "lib"), str(ROOT / "tests")))
    died = subprocess.run([sys.executable, str(driver), str(config)], cwd=primary, env=child_env,
                          capture_output=True, text=True, timeout=900)
    assert died.returncode == -signal.SIGKILL, (died.stdout[-2000:], died.stderr[-4000:])

    store = ControlStore(authority / "control.sqlite3")
    assert store.get_sealed_acceptance(repository_id=repository_id, run_id="rk") is not None
    assert store.get_frontend_policy_state(repository_id=repository_id, run_id="rk") is None
    capsys.readouterr()
    assert cli.main(argv) == 78
    envelope = _last_envelope(capsys)
    assert (envelope["code"], envelope["recovery_action"]["action"]) == (
        "RETAINED_RUNTIME_NOT_REUSABLE", "resume_with_new_request_key")
    # The single unsealed launch never ran, and the lifecycle state still does not exist.
    with store.read_transaction() as tx:
        assert tx.execute("SELECT count(*) FROM authority_launch_intents WHERE capacity_exempt=1 AND NOT EXISTS "
                          "(SELECT 1 FROM authority_qualification_launches q WHERE q.intent_id="
                          "authority_launch_intents.id)").fetchone()[0] == 0
    assert store.get_frontend_policy_state(repository_id=repository_id, run_id="rk") is None
