"""Independent M1 ledger, staging, installation, and comparison acceptance."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_verifier_baseline_regressions import (
    artifact,
    candidate_manifest,
    complete_full_inventory,
    suite_observation,
)
from test_installer_opus_acceptance import SCHEMA_PATH, subject, write_json


TARGETS = {
    "gsd-claude", "gsd-codex", "ffs-gsd-pin", "claude", "codex", "gh", "bats",
    "playwright-python", "playwright-chromium", "tmux", "libevent", "coreutils",
    "python", "pytest", "pytest-cov", "filelock", "bandit", "npm-transitives",
    "node-npm", "gbrain", "gstack",
}
TOP_LEVEL = {
    "schema", "binding", "candidate", "targets", "profile_history", "reader_preflight",
    "sessions", "preservation", "review_findings", "dependency_diff", "skill_pins", "unmet",
}
TARGET_FIELDS = {
    "identity", "old_version", "installed_version", "intended_version", "tested_version",
    "source", "manager", "release_check", "command", "backup", "old_runtime",
    "current_runtime", "recovery", "customization", "dependency_changes",
    "host_observations", "canary", "status", "incompatibility", "rollback", "raw_artifacts",
}


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _descriptor(path: Path) -> dict[str, str]:
    return {"locator": str(path), "sha256": _digest(path)}


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _suite_environment(executable: Path) -> tuple[str, str]:
    resolved = executable.resolve()
    python = {
        "executable": str(resolved),
        "executable_sha256": _digest(resolved),
        "implementation": sys.implementation.name,
        "version": sys.version,
    }
    return (
        _canonical_json({"platform": sys.platform, "python": python}),
        _canonical_json({"fixture_lock": "1", "python": sys.version.split()[0]}),
    )


def _suite_with_executable(tmp_path: Path, name: str, tests: dict[str, str],
                           executable: Path) -> dict:
    descriptor = suite_observation(tmp_path, name, tests, upgrade_ready=True)
    path = Path(descriptor["locator"])
    record = json.loads(path.read_text())
    argv = [str(executable), record["argv"][1]]
    started = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    run = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         check=False, timeout=10)
    completed = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    record.update({"argv": argv, "exit_status": run.returncode, "stdout": run.stdout,
                   "stderr": run.stderr, "started_utc": started, "completed_utc": completed})
    write_json(path, record)
    descriptor.update(_descriptor(path))
    descriptor.update({"argv": argv, "exit_status": run.returncode,
                       "started_utc": started, "completed_utc": completed})
    return descriptor


def _assert_descriptor(value: object, *, actual: bool) -> None:
    assert isinstance(value, dict) and set(value) == {"locator", "sha256"}
    path = Path(value["locator"])
    assert path.is_absolute() and path.is_file()
    assert value["sha256"] == _digest(path)
    if actual:
        assert "pytest-" not in str(path) and "fixture" not in str(path).lower()


def _assert_rich_ledger(value: object, *, actual: bool = False) -> list[tuple[str, str, str, str]]:
    assert isinstance(value, dict) and set(value) == TOP_LEVEL
    assert value["schema"] == "ffs.m1-upgrade-evidence/v1"
    assert isinstance(value["binding"], dict) and value["binding"]
    _assert_descriptor(value["candidate"], actual=actual)
    targets = value["targets"]
    assert isinstance(targets, list) and targets
    by_slug: dict[str, dict] = {}
    identities: list[tuple[str, str, str, str]] = []
    for target in targets:
        assert isinstance(target, dict) and set(target) == TARGET_FIELDS
        identity = target["identity"]
        assert isinstance(identity, dict) and set(identity) == {"tool", "manager", "profile", "platform"}
        assert all(isinstance(identity[key], str) and identity[key] for key in identity)
        slug = identity["tool"]
        assert slug not in by_slug
        by_slug[slug] = target
        identities.append((slug, identity["manager"], identity["profile"], identity["platform"]))
        for key in (
            "old_version", "installed_version", "intended_version", "tested_version", "source",
            "manager", "release_check", "command", "backup", "old_runtime", "current_runtime",
            "recovery", "customization", "dependency_changes", "host_observations", "canary",
            "incompatibility", "rollback",
        ):
            assert target[key] not in (None, "", [], {}), f"{slug}.{key} is required"
        assert target["status"] in {"PASS", "FAIL", "UNMET", "HELD"}
        assert isinstance(target["raw_artifacts"], list) and target["raw_artifacts"]
        for item in target["raw_artifacts"]:
            _assert_descriptor(item, actual=actual)
    assert set(by_slug) >= TARGETS
    assert len(identities) == len(set(identities)), "canonical target identities conflict"
    assert isinstance(value["reader_preflight"], dict) and value["reader_preflight"].get("status") == "PASS"
    assert isinstance(value["preservation"], dict) and value["preservation"].get("snapshot_before")
    assert isinstance(value["sessions"], list) and value["sessions"]
    assert isinstance(value["profile_history"], list) and value["profile_history"]
    assert isinstance(value["skill_pins"], list) and value["skill_pins"]
    for pin in value["skill_pins"]:
        assert set(pin) == {"name", "current_commit", "intended_commit", "relation", "evidence"}
        assert pin["relation"] in {"equal", "intended-descends-from-current"}
        _assert_descriptor(pin["evidence"], actual=actual)
    assert isinstance(value["unmet"], list)
    return sorted(identities)


def _rich_report(tmp_path: Path) -> dict:
    raw = tmp_path / "raw-observation.json"
    write_json(raw, {"schema": "fixture.raw/v1", "observed": True})
    candidate = tmp_path / "candidate.bin"
    candidate.write_bytes(b"candidate fixture bytes\n")
    rows = []
    for slug in sorted(TARGETS):
        rows.append({
            "identity": {"tool": slug, "manager": "fixture-manager", "profile": "shared", "platform": "fixture-os"},
            "old_version": "1.0", "installed_version": "2.0", "intended_version": "2.0",
            "tested_version": "2.0", "source": {"kind": "fixture", "release_utc": "2026-09-12T00:00:00Z"},
            "manager": {"name": "fixture-manager", "preserved": True},
            "release_check": {"checked_utc": "2026-09-12T00:00:00Z", "evidence": _descriptor(raw)},
            "command": {"argv": ["fixture-manager", "upgrade", slug], "exit_status": 0},
            "backup": {"status": "PASS", "evidence": _descriptor(raw)},
            "old_runtime": {"identity": f"old-{slug}"}, "current_runtime": {"identity": f"new-{slug}"},
            "recovery": {"status": "PASS", "evidence": _descriptor(raw)},
            "customization": {"status": "preserved", "evidence": _descriptor(raw)},
            "dependency_changes": {"declared": [], "observed": ["fixture-lock"]},
            "host_observations": [{"platform": "fixture-os", "status": "PASS", "evidence": _descriptor(raw)}],
            "canary": {"status": "PASS", "evidence": _descriptor(raw)},
            "status": "PASS", "incompatibility": {"allowed": False, "reason": "none"},
            "rollback": {"status": "ready", "evidence": _descriptor(raw)},
            "raw_artifacts": [_descriptor(raw)],
        })
    return {
        "schema": "ffs.m1-upgrade-evidence/v1", "binding": {"run": "fixture-run", "attempt": "1"},
        "candidate": _descriptor(candidate), "targets": rows,
        "profile_history": [{"kind": "profile", "status": "PASS"}, {"kind": "canary", "status": "PASS"}, {"kind": "profile", "status": "PASS"}],
        "reader_preflight": {"status": "PASS", "writers_enabled": False, "evidence": _descriptor(raw)},
        "sessions": [{"pid": 424242, "identity": "synthetic-live-old-session", "synthetic": True, "preserved": True}],
        "preservation": {"snapshot_before": _descriptor(raw), "recovery_before": _descriptor(raw)},
        "review_findings": [{"id": "M-01", "status": "assigned"}],
        "dependency_diff": {"direct_ranges_preserved": True, "evidence": _descriptor(raw)},
        "skill_pins": [{"name": "skill-π", "current_commit": "a" * 40, "intended_commit": "b" * 40,
                        "relation": "intended-descends-from-current", "evidence": _descriptor(raw)}],
        "unmet": [],
    }


def test_fixture_ledger_accepts_complete_utf8_targets_in_canonical_identity_order(tmp_path: Path) -> None:
    report = _rich_report(tmp_path)
    report["targets"] = list(reversed(report["targets"]))
    report["targets"][0]["identity"]["profile"] = "profil-ž"

    identities = _assert_rich_ledger(report)

    assert identities == sorted(identities)
    assert any("ž" in row[2] for row in identities)


@pytest.mark.parametrize("mutation", ["empty", "null", "missing", "duplicate", "conflict", "older-pin", "missing-recovery"])
def test_fixture_ledger_rejects_incomplete_or_conflicting_collections(tmp_path: Path, mutation: str) -> None:
    report = _rich_report(tmp_path)
    if mutation == "empty": report["targets"] = []
    elif mutation == "null": report["targets"] = None
    elif mutation == "missing": report["targets"] = report["targets"][1:]
    elif mutation == "duplicate": report["targets"].append(deepcopy(report["targets"][0]))
    elif mutation == "conflict":
        other = deepcopy(report["targets"][0]); other["identity"]["manager"] = "other-manager"; report["targets"].append(other)
    elif mutation == "older-pin": report["skill_pins"][0]["relation"] = "current-descends-from-intended"
    else: report["targets"][0]["recovery"] = {}

    with pytest.raises(AssertionError):
        _assert_rich_ledger(report)


def test_fixture_ledger_failed_first_canary_cannot_precede_second_profile(tmp_path: Path) -> None:
    module = subject()
    candidate = tmp_path / "candidate"; candidate.write_bytes(b"candidate\n")
    proof = tmp_path / "target.json"
    entry = {"target": "fixture", "old_version": "1", "new_version": "2", "source": "fixture",
             "manager": "fixture", "command": ["fixture", "upgrade"], "rollback": {"ready": True},
             "backup": {"ready": True}, "runtime": {"ready": True}, "recovery": {"ready": True},
             "incompatible": False}
    write_json(proof, entry)
    progression = []
    for index, (kind, status) in enumerate((("profile", "PASS"), ("canary", "FAIL"), ("profile", "PASS"))):
        observed = tmp_path / f"progress-{index}.json"
        write_json(observed, {"kind": kind, "status": status, "candidate_sha256": _digest(candidate)})
        progression.append({"kind": kind, "status": status, "evidence": _descriptor(observed)})
    ledger = {"schema": "ffs.upgrade-ledger/v1", "complete": True,
              "entries": [{**entry, "evidence": [_descriptor(proof)]}], "profile_progression": progression}

    with pytest.raises(module.E) as rejected:
        module._ledger(ledger, module.Budget(), _digest(candidate))

    assert rejected.value.code == "LEDGER" and rejected.value.status == "UNMET"


def _changed_comparison_fixture(tmp_path: Path, *, suite_executable: Path | None = None) -> tuple[object, dict, Path, bytes]:
    module = subject()
    candidate = tmp_path / "candidate.py"; candidate.write_text("print('candidate')\n")
    executable = suite_executable or Path(sys.executable)
    before_suite = _suite_with_executable(tmp_path, "stable-suite", {"stable-π": "PASS"}, executable)
    baseline_manifest = candidate_manifest(candidate, "baseline")
    baseline_manifest["baseline"] = {"suite_artifacts": [before_suite]}
    baseline_manifest["full_inventory"] = complete_full_inventory(tmp_path, baseline_manifest)

    environment_metadata, dependency_metadata = _suite_environment(executable)
    environment_path = tmp_path / "inventory-environment.json"
    environment = json.loads(environment_path.read_text())
    for item in environment["observations"]["entries"]:
        observation_path = Path(item["artifact"]["locator"])
        observation = json.loads(observation_path.read_text())
        if observation["name"] == "python":
            observation["value"] = _canonical_json(json.loads(environment_metadata)["python"])
        elif observation["name"] == "dependencies":
            observation["value"] = dependency_metadata
        write_json(observation_path, observation)
        item["artifact"] = _descriptor(observation_path)
    write_json(environment_path, environment)
    baseline_manifest["full_inventory"]["environment"]["artifacts"] = [_descriptor(environment_path)]
    before_suite_path = Path(before_suite["locator"])
    before_suite_record = json.loads(before_suite_path.read_text())
    before_suite_record.update({"environment": environment_metadata,
                                "dependencies": dependency_metadata,
                                "config_sha256": baseline_manifest["provenance"]["config_sha256"]})
    write_json(before_suite_path, before_suite_record)
    before_suite.update(_descriptor(before_suite_path))
    baseline = module.baseline_mode(baseline_manifest)
    baseline_path = write_json(tmp_path / "immutable-baseline.json", baseline)

    current = _suite_with_executable(tmp_path, "current-suite", {"stable-π": "PASS"}, executable)
    current["id"] = before_suite["id"]
    entry = {"target": "fixture", "old_version": "1", "new_version": "2", "source": "fixture",
             "manager": "fixture", "command": ["fixture", "upgrade"], "rollback": {"ready": True},
             "backup": {"ready": True}, "runtime": {"ready": True}, "recovery": {"ready": True},
             "incompatible": False}
    entry_proof = write_json(tmp_path / "ledger-entry.json", entry)
    ledger = write_json(tmp_path / "ledger.json", {"schema": "ffs.upgrade-ledger/v1", "complete": True,
                        "entries": [{**entry, "evidence": [_descriptor(entry_proof)]}]})
    upgrade = candidate_manifest(candidate, "upgrade")
    before_provenance = deepcopy(baseline["provenance"])
    after_provenance = deepcopy(before_provenance)
    after_config = tmp_path / "after-config"; after_config.write_bytes(b"new config bytes\n")
    after_provenance["config_sha256"] = _digest(after_config)
    current_path = Path(current["locator"])
    current_record = json.loads(current_path.read_text())
    current_record.update({"environment": environment_metadata,
                           "dependencies": dependency_metadata,
                           "config_sha256": after_provenance["config_sha256"]})
    write_json(current_path, current_record)
    current.update(_descriptor(current_path))
    upgrade["provenance"] = after_provenance
    upgrade["current"] = {"suite_artifacts": [current]}
    upgrade["ledger_artifact"] = _descriptor(ledger)

    def locate_digest(digest: str) -> Path:
        matches = [path for path in tmp_path.rglob("*") if path.is_file() and _digest(path) == digest]
        assert matches, f"fixture did not retain bytes for {digest}"
        return matches[0]

    logical_run = baseline["binding"]["run"]
    repository_identity = baseline["repository"]["common_dir"]

    before_environment_path = tmp_path / "inventory-environment.json"
    after_environment = deepcopy(json.loads(before_environment_path.read_text()))
    after_environment["provenance"] = after_provenance
    after_entries = []
    for item in after_environment["observations"]["entries"]:
        observed_path = Path(item["artifact"]["locator"])
        observed = json.loads(observed_path.read_text())
        if observed["name"] == "config":
            observed["value"] = after_provenance["config_sha256"]
        after_observed = write_json(tmp_path / f"after-environment-{observed['name']}.json", observed)
        after_entries.append({"name": observed["name"], "artifact": _descriptor(after_observed)})
    after_environment["observations"]["entries"] = after_entries
    after_environment_path = write_json(tmp_path / "after-inventory-environment.json", after_environment)

    def identity(name: str, binding: dict, provenance: dict) -> dict:
        pieces = {}
        for component, key in (("source", "source_sha256"), ("binary", "binary_sha256"),
                               ("bundle", "bundle_sha256"), ("config", "config_sha256")):
            piece = after_config if component == "config" and name == "after" else locate_digest(provenance[key])
            pieces[component] = _descriptor(piece)
        for component in ("dependencies", "environment"):
            # Before identity must reuse bytes actually checked by baseline.
            # This config-only fixture retains those inventory observations.
            if component == "dependencies":
                piece = tmp_path / "environment-dependencies.json"
            else:
                piece = after_environment_path if name == "after" else before_environment_path
            pieces[component] = _descriptor(piece)
        record = write_json(tmp_path / f"{name}-identity.json", {
            "schema": "ffs.upgrade-environment/v1", "run": logical_run, "repository": repository_identity,
            "binding": binding, "provenance": provenance, "artifacts": pieces,
        })
        return {"binding": binding, "provenance": provenance, "identity_artifact": _descriptor(record)}

    before_binding = baseline["binding"]
    after_binding = upgrade["binding"]
    before_identity = identity("before", before_binding, before_provenance)
    after_identity = identity("after", after_binding, after_provenance)
    change_proof = tmp_path / "config-transition.diff"; change_proof.write_bytes(b"fixture config transition\n")
    environment_proof = tmp_path / "environment-transition.diff"
    environment_proof.write_bytes(b"fixture environment wrapper follows config transition\n")
    transition = write_json(tmp_path / "transition.json", {
        "schema": "ffs.upgrade-transition/v1", "run": logical_run, "repository": repository_identity,
        "before_baseline_sha256": _digest(baseline_path),
        "before_identity_sha256": before_identity["identity_artifact"]["sha256"],
        "after_identity_sha256": after_identity["identity_artifact"]["sha256"],
        "ledger_sha256": _digest(ledger),
        "changes": [{"component": "config", "before_sha256": before_provenance["config_sha256"],
                     "after_sha256": after_provenance["config_sha256"], "evidence": [_descriptor(change_proof)]},
                    {"component": "environment", "before_sha256": _digest(before_environment_path),
                     "after_sha256": _digest(after_environment_path), "evidence": [_descriptor(environment_proof)]}],
    })
    upgrade["comparison_binding"] = {"id": "fixture-change", "run": logical_run, "repository": repository_identity,
                                     "before": before_identity, "after": after_identity,
                                     "transition_artifact": _descriptor(transition)}
    baseline_bytes = baseline_path.read_bytes()
    return module, upgrade, baseline_path, baseline_bytes


def test_fixture_comparison_accepts_attributable_changed_tuple_without_relabeling_history(tmp_path: Path) -> None:
    module, manifest, baseline, before_bytes = _changed_comparison_fixture(tmp_path)

    result = module.upgrade_mode(manifest, baseline)

    assert result["status"] == "PASS" and result["comparison_passed"] is True
    assert result["provenance_drift"] is True
    assert result["before_provenance"] != result["after_provenance"]
    binding_bytes = json.dumps(manifest["comparison_binding"], sort_keys=True, separators=(",", ":")).encode()
    assert result["comparison_binding_sha256"] == sha256(binding_bytes).hexdigest()
    assert baseline.read_bytes() == before_bytes
    assert all(value is False for value in result["gate_vector"].values())


@pytest.mark.parametrize("mutation", ["wrong-run", "wrong-repository", "unexplained-change"])
def test_fixture_comparison_rejects_foreign_or_unbound_transition(tmp_path: Path, mutation: str) -> None:
    module, manifest, baseline, _before_bytes = _changed_comparison_fixture(tmp_path)
    if mutation == "wrong-run": manifest["comparison_binding"]["run"] = "foreign-run"
    elif mutation == "wrong-repository": manifest["comparison_binding"]["repository"] = "foreign-repository"
    else: manifest["comparison_binding"]["transition_artifact"] = manifest["comparison_binding"]["before"]["identity_artifact"]

    with pytest.raises(module.E) as rejected:
        module.upgrade_mode(manifest, baseline)

    assert rejected.value.status == "FAIL"


def test_fixture_contract_publishes_closed_comparison_binding_schema() -> None:
    schema = json.loads(SCHEMA_PATH.read_text())
    required = {"upgrade_comparison_binding", "upgrade_environment", "upgrade_transition", "upgrade_transition_change"}
    assert required <= set(schema["$defs"])
    upgrade = schema["$defs"]["upgrade_manifest"]
    assert "comparison_binding" in upgrade["properties"]
    result = schema["$defs"]["upgrade_result"]
    assert {"before_provenance", "after_provenance", "comparison_binding_sha256"} <= set(result["properties"])


def _actual_json(name: str) -> object:
    selected = os.environ.get(name)
    assert selected, f"{name} is required; missing actual evidence fails instead of skipping"
    path = Path(selected)
    assert path.is_absolute() and path.is_file()
    return json.loads(path.read_text())


def test_actual_ledger_requires_checked_production_report() -> None:
    _assert_rich_ledger(_actual_json("FFS_M1_LEDGER_REPORT"), actual=True)


def test_actual_staging_requires_terminal_or_accountable_owned_outcomes() -> None:
    value = _actual_json("FFS_M1_STAGING_REPORT")
    assert isinstance(value, dict) and value.get("schema") == "ffs.m1-staging-evidence/v1"
    assert value.get("targets") and all(row.get("owner") and row.get("result") in {"PASS", "HELD", "UNMET"} for row in value["targets"])


@pytest.mark.parametrize("name", ["FFS_M1_INSTALLATION_INDEX", "FFS_M1_MEASUREMENTS"])
def test_actual_installation_and_actual_measurements_require_explicit_inputs(name: str) -> None:
    value = _actual_json(name)
    assert isinstance(value, dict) and value


@pytest.mark.xfail(
    reason="terminal RETAIN_UNMET historical input disposition (2026-09-17)",
    raises=AssertionError,
    strict=True,
)
def test_actual_comparison_requires_all_historical_and_current_inputs() -> None:
    for name in ("FFS_M1_BASELINE", "FFS_M1_UPGRADE_MANIFEST", "FFS_M1_UPGRADE_RESULT"):
        value = _actual_json(name)
        assert isinstance(value, dict) and value.get("label") != "hermetic"
    reference = _actual_json("FFS_M1_REFERENCE_MANIFEST")
    assert isinstance(reference, dict)
    assert reference.get("label") == "observed-upgraded-reference"
    # The current reference describes present observations; it cannot rewrite
    # the historical baseline completeness decision sealed by AD-016.
    historical = _actual_json("FFS_M1_BASELINE")
    if "full_baseline_complete" in historical:
        assert historical["full_baseline_complete"] is False
