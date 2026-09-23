"""Independent final acceptance for M1 before/after comparison semantics."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from test_installer_opus_acceptance import write_json
from test_m1_upgrade_evidence import _canonical_json, _changed_comparison_fixture, _descriptor


def _record(descriptor: dict) -> tuple[Path, dict]:
    path = Path(descriptor["locator"])
    return path, json.loads(path.read_text())


def _rewrite_current_metadata(manifest: dict, *, environment: str, dependencies: str,
                              config_sha256: str) -> None:
    descriptor = manifest["current"]["suite_artifacts"][0]
    path, record = _record(descriptor)
    record.update({
        "environment": environment,
        "dependencies": dependencies,
        "config_sha256": config_sha256,
    })
    write_json(path, record)
    descriptor.update(_descriptor(path))


def _transition_proof(tmp_path: Path, binding: dict, component: str,
                      before_sha256: str, after_sha256: str,
                      before_metadata: dict, after_metadata: dict) -> dict:
    proof = write_json(tmp_path / f"{component}-typed-transition.json", {
        "schema": "ffs.upgrade-component-transition/v1",
        "run": binding["run"],
        "repository": binding["repository"],
        "component": component,
        "before_sha256": before_sha256,
        "after_sha256": after_sha256,
        "before_suite_metadata": before_metadata,
        "after_suite_metadata": after_metadata,
    })
    return {
        "component": component,
        "before_sha256": before_sha256,
        "after_sha256": after_sha256,
        "evidence": [_descriptor(proof)],
    }


def _repin_after_observations(tmp_path: Path, manifest: dict, baseline: Path,
                              *, environment: str, dependencies: str) -> None:
    binding = manifest["comparison_binding"]
    before_identity_path, before_identity = _record(binding["before"]["identity_artifact"])
    after_identity_path, after_identity = _record(binding["after"]["identity_artifact"])
    before_environment_path, before_environment = _record(before_identity["artifacts"]["environment"])
    before_suite = json.loads(baseline.read_text())["suite_observations"][0]
    after_config = manifest["provenance"]["config_sha256"]
    after_metadata = {
        "environment": environment,
        "dependencies": dependencies,
        "config_sha256": after_config,
    }
    environment_record = json.loads(environment)

    after_entries = []
    for entry in before_environment["observations"]["entries"]:
        _old_path, observation = _record(entry["artifact"])
        name = observation["name"]
        if name == "platform":
            observation["value"] = environment_record["platform"]
        elif name == "python":
            observation["value"] = _canonical_json(environment_record["python"])
        elif name == "dependencies":
            observation["value"] = dependencies
        elif name == "config":
            observation["value"] = after_config
        after_observation = write_json(tmp_path / f"after-environment-{name}.json", observation)
        after_entries.append({"name": name, "artifact": _descriptor(after_observation)})

    after_environment = deepcopy(before_environment)
    after_environment["provenance"] = manifest["provenance"]
    after_environment["observations"]["entries"] = after_entries
    after_environment_path = write_json(tmp_path / "after-inventory-environment.json", after_environment)
    after_dependency = next(
        entry["artifact"] for entry in after_entries if entry["name"] == "dependencies"
    )
    after_identity["artifacts"]["dependencies"] = after_dependency
    after_identity["artifacts"]["environment"] = _descriptor(after_environment_path)
    write_json(after_identity_path, after_identity)
    binding["after"]["identity_artifact"] = _descriptor(after_identity_path)

    transition_path, transition = _record(binding["transition_artifact"])
    transition["before_baseline_sha256"] = _descriptor(baseline)["sha256"]
    transition["after_identity_sha256"] = _descriptor(after_identity_path)["sha256"]
    transition["changes"] = [
        change for change in transition["changes"]
        if change["component"] not in {"dependencies", "environment"}
    ]
    for component in ("dependencies", "environment"):
        before_sha = before_identity["artifacts"][component]["sha256"]
        after_sha = after_identity["artifacts"][component]["sha256"]
        if before_sha != after_sha:
            transition["changes"].append(_transition_proof(
                tmp_path, binding, component, before_sha, after_sha,
                {key: before_suite[key] for key in after_metadata}, after_metadata,
            ))
    write_json(transition_path, transition)
    binding["transition_artifact"] = _descriptor(transition_path)
    _rewrite_current_metadata(manifest, **after_metadata)


def coherent_changed_fixture(tmp_path: Path, *, suite_executable: Path | None = None):
    module, manifest, baseline, _stale_bytes = _changed_comparison_fixture(
        tmp_path, suite_executable=suite_executable
    )
    retained = json.loads(baseline.read_text())
    before_metadata = retained["suite_observations"][0]
    before_metadata["config_sha256"] = retained["provenance"]["config_sha256"]
    write_json(baseline, retained)
    _repin_after_observations(
        tmp_path, manifest, baseline,
        environment=before_metadata["environment"],
        dependencies=before_metadata["dependencies"],
    )
    return module, manifest, baseline, baseline.read_bytes()


def same_tuple_legacy_fixture(tmp_path: Path, *, suite_executable: Path | None = None):
    module, manifest, baseline, baseline_bytes = coherent_changed_fixture(
        tmp_path, suite_executable=suite_executable
    )
    retained = json.loads(baseline.read_text())
    metadata = retained["suite_observations"][0]
    manifest["provenance"] = retained["provenance"]
    manifest.pop("comparison_binding")
    _rewrite_current_metadata(
        manifest,
        environment=metadata["environment"],
        dependencies=metadata["dependencies"],
        config_sha256=metadata["config_sha256"],
    )
    return module, manifest, baseline, baseline_bytes


def test_coherent_changed_fixture_binds_after_environment_to_suite_and_config(tmp_path: Path) -> None:
    module, manifest, baseline, baseline_bytes = coherent_changed_fixture(tmp_path)
    result = module.upgrade_mode(manifest, baseline)
    assert result["status"] == "PASS" and result["comparison_passed"] is True
    assert baseline.read_bytes() == baseline_bytes
    assert all(value is False for value in result["gate_vector"].values())


def test_legacy_same_tuple_remains_readable_without_comparison_binding(tmp_path: Path) -> None:
    module, manifest, baseline, baseline_bytes = same_tuple_legacy_fixture(tmp_path)
    result = module.upgrade_mode(manifest, baseline)
    assert result["status"] == "PASS" and result["comparison_binding_sha256"] is None
    assert baseline.read_bytes() == baseline_bytes
    assert all(value is False for value in result["gate_vector"].values())


@pytest.mark.parametrize("component", ["dependencies", "environment"])
def test_no_binding_legacy_refuses_observed_suite_metadata_drift(
    tmp_path: Path, component: str,
) -> None:
    module, manifest, baseline, baseline_bytes = same_tuple_legacy_fixture(tmp_path)
    descriptor = manifest["current"]["suite_artifacts"][0]
    path, current = _record(descriptor)
    current[component] += "-changed"
    write_json(path, current)
    descriptor.update(_descriptor(path))
    with pytest.raises(module.E) as rejected:
        module.upgrade_mode(manifest, baseline)
    assert rejected.value.code == "COMPARISON_BINDING"
    assert baseline.read_bytes() == baseline_bytes


def test_after_identity_cannot_reuse_before_inventory_when_suite_tuple_changed(tmp_path: Path) -> None:
    module, manifest, baseline, baseline_bytes = coherent_changed_fixture(tmp_path)
    observed_environment = json.loads(json.loads(baseline.read_text())["suite_observations"][0]["environment"])
    observed_environment["platform"] = "fixture-os-after"
    _repin_after_observations(
        tmp_path, manifest, baseline,
        environment=json.dumps(observed_environment, sort_keys=True, separators=(",", ":")),
        dependencies=json.dumps({"fixture_lock": "after"}, sort_keys=True, separators=(",", ":")),
    )
    assert module.upgrade_mode(manifest, baseline)["comparison_passed"] is True

    binding = manifest["comparison_binding"]
    _before_path, before_identity = _record(binding["before"]["identity_artifact"])
    after_path, after_identity = _record(binding["after"]["identity_artifact"])
    for component in ("dependencies", "environment"):
        after_identity["artifacts"][component] = before_identity["artifacts"][component]
    write_json(after_path, after_identity)
    binding["after"]["identity_artifact"] = _descriptor(after_path)
    transition_path, transition = _record(binding["transition_artifact"])
    transition["after_identity_sha256"] = _descriptor(after_path)["sha256"]
    transition["changes"] = [
        change for change in transition["changes"]
        if change["component"] not in {"dependencies", "environment"}
    ]
    write_json(transition_path, transition)
    binding["transition_artifact"] = _descriptor(transition_path)

    with pytest.raises(module.E) as rejected:
        module.upgrade_mode(manifest, baseline)
    assert rejected.value.code == "COMPARISON_EVIDENCE"
    assert baseline.read_bytes() == baseline_bytes
