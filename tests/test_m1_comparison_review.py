"""Independent regression oracles for the final Opus comparison review."""
import json
from pathlib import Path

import pytest

from test_m1_upgrade_evidence import _canonical_json, _changed_comparison_fixture, _descriptor
from test_installer_opus_acceptance import write_json


def changed_inventory(tmp_path: Path, component: str, explained: bool):
    module, manifest, baseline, _ = _changed_comparison_fixture(tmp_path)
    binding = manifest["comparison_binding"]
    identity_path = Path(binding["after"]["identity_artifact"]["locator"])
    identity = json.loads(identity_path.read_text())
    before_identity_path = Path(binding["before"]["identity_artifact"]["locator"])
    before_identity = json.loads(before_identity_path.read_text())
    changed_components = {component}
    if component == "dependencies":
        environment_value = _canonical_json({"fixture_lock": "2", "python": "changed"})
        inventory = write_json(tmp_path / "changed-dependencies.json", {
            "schema": "ffs.environment-observation/v1", "name": "dependencies",
            "value": environment_value,
        })
        identity["artifacts"]["dependencies"] = _descriptor(inventory)
        changed_components.add("environment")
        environment_name = "dependencies"
    else:
        environment_value = "fixture-os-changed"
        environment_name = "platform"
    current_environment_path = Path(identity["artifacts"]["environment"]["locator"])
    current_environment = json.loads(current_environment_path.read_text())
    entries = []
    for item in current_environment["observations"]["entries"]:
        observation = json.loads(Path(item["artifact"]["locator"]).read_text())
        if observation["name"] == environment_name:
            observation["value"] = environment_value
        path = write_json(tmp_path / f"changed-environment-{observation['name']}.json", observation)
        entries.append({"name": observation["name"], "artifact": _descriptor(path)})
    current_environment["observations"]["entries"] = entries
    environment_inventory = write_json(tmp_path / "changed-environment.json", current_environment)
    identity["artifacts"]["environment"] = _descriptor(environment_inventory)
    write_json(identity_path, identity)
    binding["after"]["identity_artifact"] = _descriptor(identity_path)
    transition_path = Path(binding["transition_artifact"]["locator"])
    transition = json.loads(transition_path.read_text())
    transition["after_identity_sha256"] = _descriptor(identity_path)["sha256"]
    transition["changes"] = [
        row for row in transition["changes"] if row["component"] not in changed_components
    ]
    if explained:
        for changed in sorted(changed_components):
            fields = {
                "component": changed,
                "before_sha256": before_identity["artifacts"][changed]["sha256"],
                "after_sha256": identity["artifacts"][changed]["sha256"],
            }
            proof = write_json(tmp_path / f"{changed}-transition-proof.json", {
                "schema": "ffs.upgrade-change-evidence/v1", "run": binding["run"],
                "repository": binding["repository"], **fields,
            })
            transition["changes"].append({**fields, "evidence": [_descriptor(proof)]})
    write_json(transition_path, transition)
    binding["transition_artifact"] = _descriptor(transition_path)
    current_descriptor = manifest["current"]["suite_artifacts"][0]
    current_path = Path(current_descriptor["locator"])
    current = json.loads(current_path.read_text())
    if component == "dependencies":
        current[component] = environment_value
    else:
        metadata = json.loads(current["environment"])
        metadata["platform"] = environment_value
        current[component] = _canonical_json(metadata)
    write_json(current_path, current)
    current_descriptor.update(_descriptor(current_path))
    return module, manifest, baseline


@pytest.mark.parametrize("component", ["dependencies", "environment"])
def test_changed_inventory_requires_a_transition_record(tmp_path: Path, component: str):
    module, manifest, baseline = changed_inventory(tmp_path, component, False)
    with pytest.raises(module.E) as rejected:
        module.upgrade_mode(manifest, baseline)
    assert rejected.value.code == "UPGRADE_TRANSITION"


@pytest.mark.parametrize("component", ["dependencies", "environment"])
def test_attributable_inventory_transition_can_be_compared(tmp_path: Path, component: str):
    module, manifest, baseline = changed_inventory(tmp_path, component, True)
    before = baseline.read_bytes()
    result = module.upgrade_mode(manifest, baseline)
    assert result["comparison_passed"] is True
    assert baseline.read_bytes() == before
    assert all(value is False for value in result["gate_vector"].values())


@pytest.mark.parametrize("metadata", [None, {}, ""])
def test_empty_historical_suite_metadata_cannot_satisfy_comparison(tmp_path: Path, metadata):
    module, manifest, baseline, _ = _changed_comparison_fixture(tmp_path)
    record = json.loads(baseline.read_text())
    for observation in record["suite_observations"]:
        observation["environment"] = metadata
        observation["dependencies"] = metadata
        observation["config_sha256"] = metadata
    write_json(baseline, record)
    binding = manifest["comparison_binding"]
    transition_path = Path(binding["transition_artifact"]["locator"])
    transition = json.loads(transition_path.read_text())
    transition["before_baseline_sha256"] = _descriptor(baseline)["sha256"]
    write_json(transition_path, transition)
    binding["transition_artifact"] = _descriptor(transition_path)
    with pytest.raises(module.E) as rejected:
        module.upgrade_mode(manifest, baseline)
    assert rejected.value.code == "COMPARISON_EVIDENCE"


@pytest.mark.parametrize("component", ["dependencies", "environment"])
def test_before_identity_cannot_replace_retained_inventory_bytes(tmp_path: Path, component: str):
    module, manifest, baseline, _ = _changed_comparison_fixture(tmp_path)
    binding = manifest["comparison_binding"]
    replacement = write_json(tmp_path / "invented-history.json", {
        "schema": f"ffs.{component}/v1", "complete": True, "value": "invented-history",
    })
    # Consistent re-pinning of both identities cannot change what baseline observed.
    transition_path = Path(binding["transition_artifact"]["locator"])
    transition = json.loads(transition_path.read_text())
    for side in ["before", "after"]:
        identity_path = Path(binding[side]["identity_artifact"]["locator"])
        identity = json.loads(identity_path.read_text())
        identity["artifacts"][component] = _descriptor(replacement)
        write_json(identity_path, identity)
        binding[side]["identity_artifact"] = _descriptor(identity_path)
        transition[f"{side}_identity_sha256"] = _descriptor(identity_path)["sha256"]
    write_json(transition_path, transition)
    binding["transition_artifact"] = _descriptor(transition_path)
    with pytest.raises(module.E) as rejected:
        module.upgrade_mode(manifest, baseline)
    assert rejected.value.code == "COMPARISON_EVIDENCE"


def test_consistently_foreign_repository_cannot_replace_baseline_repository(tmp_path: Path):
    module, manifest, baseline, _ = _changed_comparison_fixture(tmp_path)
    binding = manifest["comparison_binding"]
    binding["repository"] = str(tmp_path / "foreign/.git")
    transition_path = Path(binding["transition_artifact"]["locator"])
    transition = json.loads(transition_path.read_text())
    transition["repository"] = binding["repository"]
    for side in ["before", "after"]:
        identity_path = Path(binding[side]["identity_artifact"]["locator"])
        identity = json.loads(identity_path.read_text())
        identity["repository"] = binding["repository"]
        write_json(identity_path, identity)
        binding[side]["identity_artifact"] = _descriptor(identity_path)
        transition[f"{side}_identity_sha256"] = _descriptor(identity_path)["sha256"]
    write_json(transition_path, transition)
    binding["transition_artifact"] = _descriptor(transition_path)
    with pytest.raises(module.E) as rejected:
        module.upgrade_mode(manifest, baseline)
    assert rejected.value.code == "COMPARISON_BINDING"
