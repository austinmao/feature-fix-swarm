"""Independent production admission refusals for forged review material."""
from dataclasses import replace
import hashlib
from pathlib import Path
import sys

import pytest

LIB = Path(__file__).resolve().parents[1] / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from host_capabilities import build_artifact_review_material  # noqa: E402
from run_state.supervisor import SupervisorRefused  # noqa: E402
from test_supervised_process import setup_owner  # noqa: E402


def _authority_snapshot(store):
    with store.read_transaction() as tx:
        return {
            table: [tuple(row) for row in tx.execute("SELECT * FROM " + table)]
            for table in ("control_events", "authority_launch_intents",
                          "authority_launch_accounting", "authority_run_limits")
        }


@pytest.mark.parametrize("forged_field", ["environment", "model", "prompt", "config", "contents"])
def test_forged_material_refuses_before_any_launch_or_debit(tmp_path, monkeypatch, forged_field):
    supervisor, store, request = setup_owner(tmp_path)
    material = build_artifact_review_material(
        host="claude", model_request={"kind": "tier", "name": "judgment"},
        config_sha256="a" * 64, policy_sha256="b" * 64,
        environment={"HOME": "/private/runtime", "PATH": "/usr/bin:/bin", "TMPDIR": "/private/scratch"},
        selected_artifacts={"input.json": hashlib.sha256(b"input\\n").hexdigest()},
        selected_contents={"input.json": "input\\n"},
        provenance={"snapshot_sha256": "d" * 64},
    )
    if forged_field == "environment":
        material = replace(material, environment=material.environment + (("PYTHONPATH", "/untrusted"),))
    elif forged_field == "model":
        material = replace(material, effective_model="unrequested-model")
    elif forged_field == "prompt":
        material = replace(material, prompt="different unbound instructions")
    elif forged_field == "contents":
        material = replace(material, selected_contents=(("input.json", "forged content"),))
    else:
        material = replace(material, config_sha256="not-a-digest")
    request = replace(request, host_material=material, command=("/usr/bin/true",))
    before = _authority_snapshot(store)

    def forbidden_spawn(*args, **kwargs):
        raise AssertionError("forged host material reached Popen")

    monkeypatch.setattr("run_state.supervisor.subprocess.Popen", forbidden_spawn)
    with pytest.raises(SupervisorRefused) as caught:
        supervisor.launch(request)
    assert caught.value.code == "HOST_MATERIAL_INVALID"
    assert _authority_snapshot(store) == before
