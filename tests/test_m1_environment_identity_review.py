"""Independent regressions for complete M1 environment and native-runtime identity."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import sys

import pytest

from test_installer_opus_acceptance import subject, write_json
from test_m1_comparison_final_review import coherent_changed_fixture, same_tuple_legacy_fixture
from test_m1_upgrade_evidence import _descriptor


def _rewrite_suite(descriptor: dict, **metadata: str) -> None:
    path = Path(descriptor["locator"])
    record = json.loads(path.read_text())
    record.update(metadata)
    write_json(path, record)
    descriptor.update(_descriptor(path))


def test_no_binding_platform_only_legacy_tuple_is_typed_unmet(tmp_path: Path) -> None:
    module, manifest, baseline, _baseline_bytes = same_tuple_legacy_fixture(tmp_path)
    retained = json.loads(baseline.read_text())
    retained_metadata = retained["suite_observations"][0]
    retained_metadata.update({"environment": sys.platform, "dependencies": "fixture-lock"})
    write_json(baseline, retained)
    _rewrite_suite(
        manifest["current"]["suite_artifacts"][0],
        environment=sys.platform,
        dependencies="fixture-lock",
        config_sha256=retained_metadata["config_sha256"],
    )
    sealed = baseline.read_bytes()

    with pytest.raises(module.E) as rejected:
        module.upgrade_mode(manifest, baseline)

    assert rejected.value.code == "COMPARISON_EVIDENCE"
    assert rejected.value.status == "UNMET"
    assert baseline.read_bytes() == sealed


def test_no_binding_rehashes_python_executable_in_complete_suite_environment(tmp_path: Path) -> None:
    launcher = tmp_path / "python-runtime"
    launcher.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n")
    launcher.chmod(0o755)
    module, manifest, baseline, baseline_bytes = same_tuple_legacy_fixture(
        tmp_path, suite_executable=launcher
    )

    # The locator, platform, dependency and config strings remain unchanged,
    # while the executable bytes at that locator no longer match the captured
    # Python identity.
    launcher.write_text(launcher.read_text() + "# replaced in place\n")
    launcher.chmod(0o755)

    with pytest.raises(module.E) as rejected:
        module.upgrade_mode(manifest, baseline)

    assert rejected.value.code == "COMPARISON_EVIDENCE"
    assert rejected.value.status == "UNMET"
    assert baseline.read_bytes() == baseline_bytes


def test_binding_rejects_before_suite_metadata_not_anchored_to_before_inventory(tmp_path: Path) -> None:
    module, manifest, baseline, _baseline_bytes = coherent_changed_fixture(tmp_path)
    binding = manifest["comparison_binding"]

    # This is the formerly accepted legacy exploit shape: both suite sides use
    # the same platform claim, the after inventory is repinned to that claim,
    # but the retained before inventory still records the actual platform.
    foreign_platform = "foreign-platform"
    retained = json.loads(baseline.read_text())
    retained["suite_observations"][0]["environment"] = foreign_platform
    write_json(baseline, retained)
    _rewrite_suite(manifest["current"]["suite_artifacts"][0], environment=foreign_platform)

    after_identity_path = Path(binding["after"]["identity_artifact"]["locator"])
    after_identity = json.loads(after_identity_path.read_text())
    environment_path = Path(after_identity["artifacts"]["environment"]["locator"])
    environment = json.loads(environment_path.read_text())
    entries = []
    for entry in environment["observations"]["entries"]:
        observation = json.loads(Path(entry["artifact"]["locator"]).read_text())
        if observation["name"] == "platform":
            observation["value"] = foreign_platform
        path = write_json(tmp_path / f"foreign-before-{observation['name']}.json", observation)
        entries.append({"name": observation["name"], "artifact": _descriptor(path)})
    environment["observations"]["entries"] = entries
    write_json(environment_path, environment)
    after_identity["artifacts"]["environment"] = _descriptor(environment_path)
    write_json(after_identity_path, after_identity)
    binding["after"]["identity_artifact"] = _descriptor(after_identity_path)

    transition_path = Path(binding["transition_artifact"]["locator"])
    transition = json.loads(transition_path.read_text())
    transition["before_baseline_sha256"] = _descriptor(baseline)["sha256"]
    transition["after_identity_sha256"] = _descriptor(after_identity_path)["sha256"]
    environment_change = next(row for row in transition["changes"] if row["component"] == "environment")
    environment_change["after_sha256"] = _descriptor(environment_path)["sha256"]
    write_json(transition_path, transition)
    binding["transition_artifact"] = _descriptor(transition_path)
    sealed = baseline.read_bytes()

    with pytest.raises(module.E) as rejected:
        module.upgrade_mode(manifest, baseline)

    assert rejected.value.code == "COMPARISON_EVIDENCE"
    assert rejected.value.status == "UNMET"
    assert baseline.read_bytes() == sealed


def test_native_python_is_anchored_to_current_base_prefix_not_nearest_framework(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = subject()
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    executable = tmp_path / "home" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_text("decoy parent executable\n")
    executable.chmod(0o755)

    decoy = (tmp_path / "home" / "Frameworks" / "Python.framework" / "Versions" / version
             / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python")
    decoy.parent.mkdir(parents=True)
    decoy.write_text("unrelated framework\n")
    decoy.chmod(0o755)

    base_prefix = tmp_path / "current" / "Python.framework" / "Versions" / version
    expected = base_prefix / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    expected.parent.mkdir(parents=True)
    expected.write_text("current framework\n")
    expected.chmod(0o755)

    monkeypatch.setattr(module.sys, "executable", str(executable))
    monkeypatch.setattr(module.sys, "base_prefix", str(base_prefix))

    assert Path(module._native_python()) == expected
    assert os.access(expected, os.X_OK)
