"""Independent regressions for the M0 opposite-vendor contract review.

These tests exercise only disposable repositories and files.  They describe
the verifier boundary; they do not repair production code.
"""
from __future__ import annotations

import ast
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess

from jsonschema import Draft202012Validator
import pytest

import scripts.verification.parallel_host_parity as verifier
from test_m0_backup_evidence import (
    artifact as backup_artifact,
    backup_manifest,
    invoke as invoke_backup,
    sha as backup_sha,
    write_json as write_backup_json,
)
from test_m0_baseline_evidence import manifest as baseline_manifest
from test_m0_directory_acceptance import directory_digest, directory_fixture
from test_m0_source_evidence import (
    VERIFIER,
    artifact,
    clean_env,
    git,
    invoke,
    sha,
    source_fixture,
    write_json,
)


def source_payload(manifest_path: Path) -> tuple[dict, Path, dict, dict, dict]:
    manifest = json.loads(manifest_path.read_text())
    evidence_path = Path(manifest["full_inventory"]["source_runtime"]["artifacts"][0]["locator"])
    evidence = json.loads(evidence_path.read_text())
    row = evidence["observations"]["canonical_source"]["manifests"][0]
    mapping = row["mappings"][0]
    return manifest, evidence_path, evidence, row, mapping


def seal_source(manifest_path: Path, manifest: dict, evidence_path: Path, evidence: dict) -> None:
    write_json(evidence_path, evidence)
    manifest["full_inventory"]["source_runtime"]["artifacts"] = [artifact(evidence_path)]
    write_json(manifest_path, manifest)


def reseal_install(row: dict) -> dict:
    install_path = Path(row["install_manifest"]["locator"])
    install = json.loads(install_path.read_text())
    return install


def write_install(row: dict, path: Path, install: dict) -> None:
    write_json(path, install)
    row["install_manifest"] = artifact(path)


def assert_source_refused(tmp_path: Path, manifest_path: Path) -> None:
    run, payload = invoke(tmp_path, manifest_path)
    assert run.returncode != 0, payload
    assert payload["status"] == "UNMET", payload
    assert payload["errors"][0]["code"] == "SOURCE_CANONICAL", payload


def directory_entries(source: Path, staged: Path) -> list[dict]:
    entries: list[dict] = []
    for path in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(source).as_posix()
        target = staged / relative
        entry: dict = {"relative_path": relative}
        if path.is_symlink():
            entry.update(type="symlink", source_link_target=os.readlink(path),
                         staged_link_target=os.readlink(target))
        elif path.is_file():
            entry.update(type="file", source_artifact=artifact(path),
                         staged_artifact=artifact(target))
        else:
            entry.update(type="directory")
        entries.append(entry)
    return entries


def test_m0_01_project_directory_must_read_the_manifest_owned_stage(tmp_path: Path) -> None:
    manifest_path = directory_fixture(tmp_path, "valid")
    manifest, evidence_path, evidence, row, mapping = source_payload(manifest_path)
    real_stage = Path(mapping["staged_root"])
    (real_stage / "SKILL.md").write_text("unverified installed bytes\n")

    mapping["staged_root"] = mapping["source_root"]
    for entry in mapping["entries"]:
        if entry["type"] == "file":
            entry["staged_artifact"] = deepcopy(entry["source_artifact"])
    seal_source(manifest_path, manifest, evidence_path, evidence)

    assert_source_refused(tmp_path, manifest_path)


def test_m0_02_directory_must_cover_every_leaf_in_the_git_generation(tmp_path: Path) -> None:
    manifest_path = directory_fixture(tmp_path, "valid")
    manifest, evidence_path, evidence, row, mapping = source_payload(manifest_path)
    source, staged = Path(mapping["source_root"]), Path(mapping["staged_root"])
    (source / "SKILL.md").unlink()
    (staged / "SKILL.md").unlink()
    mapping["entries"] = directory_entries(source, staged)
    mapping["fingerprint"] = "dir:" + directory_digest(source)
    install_path = Path(row["install_manifest"]["locator"])
    install = reseal_install(row)
    install["paths"] = {mapping["managed_path"]: {"fingerprint": mapping["fingerprint"]}}
    write_install(row, install_path, install)
    seal_source(manifest_path, manifest, evidence_path, evidence)

    assert_source_refused(tmp_path, manifest_path)


def test_m0_03_canonical_proof_must_bind_the_selected_candidate(tmp_path: Path) -> None:
    manifest_path, _ = source_fixture(tmp_path)
    manifest, evidence_path, evidence, _row, _mapping = source_payload(manifest_path)
    outside = tmp_path / "outside" / "setup.sh"
    outside.parent.mkdir()
    outside.write_text("#!/bin/sh\necho unrelated candidate\n")
    manifest["candidate"] = artifact(outside)
    manifest["provenance"]["source_sha256"] = sha(outside)
    evidence["provenance"]["source_sha256"] = sha(outside)
    evidence["observations"]["entries"][0]["artifact"] = artifact(outside)
    seal_source(manifest_path, manifest, evidence_path, evidence)

    assert_source_refused(tmp_path, manifest_path)


def test_m0_04_claimed_subdirectory_cannot_launder_a_dirty_file(tmp_path: Path) -> None:
    manifest_path, _ = source_fixture(tmp_path)
    manifest, evidence_path, evidence, row, mapping = source_payload(manifest_path)
    repo = Path(manifest["repository"])
    duplicate = repo / "demo/SKILL.md"
    duplicate.parent.mkdir()
    duplicate.write_text("different committed path\n")
    subprocess.run(["git", "-C", str(repo), "add", "demo/SKILL.md"], check=True, env=clean_env())
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=M0 Acceptance", "-c",
                    "user.email=m0@example.invalid", "commit", "-qm", "duplicate path"],
                   check=True, env=clean_env())
    generation = git(repo, "rev-parse", "HEAD")
    source = repo / "skills/demo/SKILL.md"
    staged = repo / ".agents/skills/demo/SKILL.md"
    source.write_bytes(duplicate.read_bytes())
    staged.write_bytes(duplicate.read_bytes())
    claimed_root = repo / "skills"
    row["resolved_source_root"] = row["repository"]["root"] = str(claimed_root)
    row["repository"]["head"] = row["repository"]["generation"] = generation
    mapping["source_artifact"] = artifact(source)
    mapping["staged_artifact"] = artifact(staged)
    install_path = Path(row["install_manifest"]["locator"])
    install = reseal_install(row)
    install["paths"][mapping["managed_path"]]["fingerprint"] = "file:" + sha(source)
    write_install(row, install_path, install)
    seal_source(manifest_path, manifest, evidence_path, evidence)

    assert_source_refused(tmp_path, manifest_path)


def test_m0_05_install_manifest_must_be_at_the_scope_defined_location(tmp_path: Path) -> None:
    manifest_path, _ = source_fixture(tmp_path)
    manifest, evidence_path, evidence, row, _mapping = source_payload(manifest_path)
    original = Path(row["install_manifest"]["locator"])
    decoy = tmp_path / "producer-selected" / "manifest.json"
    decoy.parent.mkdir()
    shutil.copy2(original, decoy)
    row["install_manifest"] = artifact(decoy)
    seal_source(manifest_path, manifest, evidence_path, evidence)

    assert_source_refused(tmp_path, manifest_path)


def test_m0_06_backup_and_restore_need_distinct_physical_evidence(tmp_path: Path) -> None:
    manifest_path, _ = backup_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    wrapper_path = Path(manifest["full_inventory"]["backups_recovery"]["artifacts"][0]["locator"])
    wrapper = json.loads(wrapper_path.read_text())
    shared = tmp_path / "one-file-for-every-surface"
    shared.write_text("one object is not ten recoveries\n")
    for entry in wrapper["observations"]["entries"]:
        entry["expected_sha256"] = backup_sha(shared)
        entry["backup_artifact"] = backup_artifact(shared)
        entry["restored_artifact"] = backup_artifact(shared)
        verification_path = Path(entry["verification_artifact"]["locator"])
        write_backup_json(verification_path, {
            "name": entry["name"], "status": "PASS", "expected_sha256": backup_sha(shared),
            "backup_sha256": backup_sha(shared), "restored_sha256": backup_sha(shared),
        })
        entry["verification_artifact"] = backup_artifact(verification_path)
    write_backup_json(wrapper_path, wrapper)
    manifest["full_inventory"]["backups_recovery"]["artifacts"] = [backup_artifact(wrapper_path)]
    write_backup_json(manifest_path, manifest)

    run, payload = invoke_backup(tmp_path, manifest_path)
    assert run.returncode != 0, payload
    assert payload["status"] == "UNMET", payload
    assert payload["errors"][0]["code"] == "BACKUP_EVIDENCE", payload


def test_m0_07_git_object_type_must_match_directory_leaf_type(tmp_path: Path) -> None:
    manifest_path = directory_fixture(tmp_path, "valid")
    manifest, evidence_path, evidence, row, mapping = source_payload(manifest_path)
    source, staged = Path(mapping["source_root"]), Path(mapping["staged_root"])
    for root in (source, staged):
        link = root / "latest.md"
        target = os.readlink(link)
        link.unlink()
        link.write_text(target)
    mapping["entries"] = directory_entries(source, staged)
    mapping["fingerprint"] = "dir:" + directory_digest(source)
    install_path = Path(row["install_manifest"]["locator"])
    install = reseal_install(row)
    install["paths"] = {mapping["managed_path"]: {"fingerprint": mapping["fingerprint"]}}
    write_install(row, install_path, install)
    seal_source(manifest_path, manifest, evidence_path, evidence)

    assert_source_refused(tmp_path, manifest_path)


def test_m0_08_oversized_declared_inventory_is_refused_before_tree_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    source = project / "skills/demo"
    staged = project / ".agents/skills/demo"
    source.mkdir(parents=True)
    staged.mkdir(parents=True)
    managed = ".agents/skills/demo"
    mapping = {
        "managed_path": managed,
        "type": "directory",
        "source_root": str(source),
        "staged_root": str(staged),
        "fingerprint": "dir:" + "0" * 64,
        "entries": [
            {"relative_path": f"declared-{index}", "type": "directory"}
            for index in range(verifier.MAX_ARTIFACTS + 1)
        ],
    }
    install = {"paths": {managed: {"fingerprint": mapping["fingerprint"]}}}

    def forbidden_walk(_root: Path):
        pytest.fail("tree walk began before the over-limit declared inventory was refused")

    monkeypatch.setattr(verifier, "_directory_fingerprint", forbidden_walk)
    with pytest.raises(verifier.E) as rejected:
        verifier._directory_mapping(mapping, managed, install, project, project, "0" * 40, verifier.Budget())
    assert rejected.value.code in {"EVIDENCE_LIMIT", "SOURCE_CANONICAL"}


def test_m0_09_realistic_directory_leaf_count_fits_the_m0_budget(tmp_path: Path) -> None:
    manifest_path = directory_fixture(tmp_path, "valid")
    manifest, evidence_path, evidence, row, mapping = source_payload(manifest_path)
    source, staged = Path(mapping["source_root"]), Path(mapping["staged_root"])
    for index in range(61):
        relative = Path("bulk") / f"leaf-{index:02d}.txt"
        (source / relative).parent.mkdir(exist_ok=True)
        (staged / relative).parent.mkdir(exist_ok=True)
        (source / relative).write_text(f"committed leaf {index}\n")
        (staged / relative).write_bytes((source / relative).read_bytes())
    repo = Path(manifest["repository"])
    subprocess.run(["git", "-C", str(repo), "add", "skills/demo"], check=True, env=clean_env())
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=M0 Acceptance", "-c",
                    "user.email=m0@example.invalid", "commit", "-qm", "realistic leaf count"],
                   check=True, env=clean_env())
    row["repository"]["head"] = row["repository"]["generation"] = git(repo, "rev-parse", "HEAD")
    mapping["entries"] = directory_entries(source, staged)
    mapping["fingerprint"] = "dir:" + directory_digest(source)
    install_path = Path(row["install_manifest"]["locator"])
    install = reseal_install(row)
    install["paths"] = {mapping["managed_path"]: {"fingerprint": mapping["fingerprint"]}}
    write_install(row, install_path, install)
    seal_source(manifest_path, manifest, evidence_path, evidence)

    run, payload = invoke(tmp_path, manifest_path)
    assert run.returncode == 0, payload
    assert payload["canonical_source_verified"] is True


def test_m0_10_backup_surface_supports_multiple_recovery_artifacts(tmp_path: Path) -> None:
    manifest_path, _ = backup_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    wrapper_path = Path(manifest["full_inventory"]["backups_recovery"]["artifacts"][0]["locator"])
    wrapper = json.loads(wrapper_path.read_text())
    plural = []
    for entry in wrapper["observations"].pop("entries"):
        second_backup = tmp_path / "preserved" / f"{entry['name']}-second"
        second_restore = tmp_path / "recovery" / f"{entry['name']}-second"
        second_backup.write_text(entry["name"] + " second preserved byte set\n")
        second_restore.write_bytes(second_backup.read_bytes())
        second_verification = write_backup_json(tmp_path / "verification" / f"{entry['name']}-second.json", {
            "name": entry["name"], "status": "PASS", "expected_sha256": backup_sha(second_backup),
            "backup_sha256": backup_sha(second_backup), "restored_sha256": backup_sha(second_restore),
        })
        plural.append({
            "name": entry["name"],
            "recoveries": [
                {key: entry[key] for key in (
                    "expected_sha256", "backup_artifact", "restored_artifact", "verification_artifact")},
                {
                    "expected_sha256": backup_sha(second_backup),
                    "backup_artifact": backup_artifact(second_backup),
                    "restored_artifact": backup_artifact(second_restore),
                    "verification_artifact": backup_artifact(second_verification),
                },
            ],
        })
    wrapper["observations"]["surfaces"] = plural
    write_backup_json(wrapper_path, wrapper)
    manifest["full_inventory"]["backups_recovery"]["artifacts"] = [backup_artifact(wrapper_path)]
    write_backup_json(manifest_path, manifest)

    run, payload = invoke_backup(tmp_path, manifest_path)
    assert run.returncode == 0, payload


def test_m0_11_null_profile_is_rejected_by_runtime_like_the_schema(tmp_path: Path) -> None:
    manifest_path, _ = baseline_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    manifest["baseline_profile"] = None
    write_json(manifest_path, manifest)

    run, payload = invoke(tmp_path, manifest_path)
    assert run.returncode != 0, payload


def test_m0_11_schema_closes_the_m0_v2_observation_shape(tmp_path: Path) -> None:
    manifest_path, _ = source_fixture(tmp_path)
    _manifest, evidence_path, evidence, _row, _mapping = source_payload(manifest_path)
    evidence["observations"]["canonical_source"] = {}
    write_json(evidence_path, evidence)
    schema = json.loads((VERIFIER.parents[2] / "schemas/parallel-host-verification.schema.json").read_text())

    errors = list(Draft202012Validator(schema).iter_errors(evidence))
    assert errors, "published schema accepted a canonical-source object that runtime refuses"


@pytest.mark.parametrize("case", ["paths-value", "doctor-stdout"])
def test_m0_12_malformed_source_values_have_typed_unmet_errors(tmp_path: Path, case: str) -> None:
    manifest_path, _ = source_fixture(tmp_path)
    manifest, evidence_path, evidence, row, mapping = source_payload(manifest_path)
    if case == "paths-value":
        install_path = Path(row["install_manifest"]["locator"])
        install = reseal_install(row)
        install["paths"][mapping["managed_path"]] = "file:" + sha(Path(mapping["source_artifact"]["locator"]))
        write_install(row, install_path, install)
    else:
        doctor_path = Path(row["doctor_artifact"]["locator"])
        doctor = json.loads(doctor_path.read_text())
        doctor["stdout"] = 1
        write_json(doctor_path, doctor)
        row["doctor_artifact"] = artifact(doctor_path)
    seal_source(manifest_path, manifest, evidence_path, evidence)

    assert_source_refused(tmp_path, manifest_path)


def test_m0_12_malformed_backup_surface_ids_have_typed_unmet_error(tmp_path: Path) -> None:
    manifest_path, _ = backup_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    wrapper_path = Path(manifest["full_inventory"]["backups_recovery"]["artifacts"][0]["locator"])
    wrapper = json.loads(wrapper_path.read_text())
    wrapper["observations"]["required_surface_ids"][0] = {}
    write_backup_json(wrapper_path, wrapper)
    manifest["full_inventory"]["backups_recovery"]["artifacts"] = [backup_artifact(wrapper_path)]
    write_backup_json(manifest_path, manifest)

    run, payload = invoke_backup(tmp_path, manifest_path)
    assert run.returncode != 0, payload
    assert payload["status"] == "UNMET", payload
    assert payload["errors"][0]["code"] == "BACKUP_EVIDENCE", payload


def test_m0_13_normalized_managed_path_alias_is_rejected(tmp_path: Path) -> None:
    manifest_path, _ = source_fixture(tmp_path)
    manifest, evidence_path, evidence, row, mapping = source_payload(manifest_path)
    alias = "./" + mapping["managed_path"]
    duplicate = deepcopy(mapping)
    duplicate["managed_path"] = alias
    row["mappings"].append(duplicate)
    install_path = Path(row["install_manifest"]["locator"])
    install = reseal_install(row)
    install["paths"][alias] = deepcopy(install["paths"][mapping["managed_path"]])
    write_install(row, install_path, install)
    seal_source(manifest_path, manifest, evidence_path, evidence)

    assert_source_refused(tmp_path, manifest_path)


@pytest.mark.parametrize("field,value", [("upstream_mappings", [42]), ("current_dirty_inventory", [42])])
def test_m0_13_auxiliary_source_observations_are_typed(
    tmp_path: Path, field: str, value: list[int]
) -> None:
    manifest_path, _ = source_fixture(tmp_path)
    manifest, evidence_path, evidence, _row, _mapping = source_payload(manifest_path)
    evidence["observations"]["canonical_source"][field] = value
    seal_source(manifest_path, manifest, evidence_path, evidence)

    assert_source_refused(tmp_path, manifest_path)


def test_m0_14_containment_helpers_are_not_silently_shadowed() -> None:
    tree = ast.parse(VERIFIER.read_text())
    definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_inside"]
    assert len(definitions) == 1


def test_actual_structured_doctor_warning_is_not_replaced_with_a_pass_marker(tmp_path: Path) -> None:
    manifest_path, _ = source_fixture(tmp_path)
    manifest, evidence_path, evidence, row, _mapping = source_payload(manifest_path)
    doctor_path = Path(row["doctor_artifact"]["locator"])
    doctor = json.loads(doctor_path.read_text())
    doctor["stdout"] = json.dumps({
        "schema": "ffs.doctor/v1",
        "status": "degraded",
        "exit_code": 0,
        "checks": [
            {"id": "managed-path", "status": "pass", "message": "all managed paths match"},
            {"id": "model-routing-catalog", "status": "warn", "message": "tracked upstream catalog gap"},
        ],
    }) + "\n"
    write_json(doctor_path, doctor)
    row["doctor_artifact"] = artifact(doctor_path)
    seal_source(manifest_path, manifest, evidence_path, evidence)

    run, payload = invoke(tmp_path, manifest_path)
    assert run.returncode == 0, payload
    assert payload["canonical_source_verified"] is True


@pytest.mark.parametrize("case", ["wrong-schema", "error-check", "nonzero"])
def test_actual_structured_doctor_refuses_invalid_or_failed_results(tmp_path: Path, case: str) -> None:
    manifest_path, _ = source_fixture(tmp_path)
    manifest, evidence_path, evidence, row, _mapping = source_payload(manifest_path)
    doctor_path = Path(row["doctor_artifact"]["locator"])
    doctor = json.loads(doctor_path.read_text())
    result = {
        "schema": "ffs.doctor/v1",
        "status": "ok",
        "exit_code": 0,
        "checks": [{"id": "managed-path", "status": "pass", "message": "all managed paths match"}],
    }
    if case == "wrong-schema":
        result["schema"] = "claim-only-doctor/v0"
    elif case == "error-check":
        result["status"] = "error"
        result["checks"][0]["status"] = "fail"
    else:
        doctor["exit_status"] = 1
        result["exit_code"] = 1
    doctor["stdout"] = json.dumps(result) + "\n"
    write_json(doctor_path, doctor)
    row["doctor_artifact"] = artifact(doctor_path)
    seal_source(manifest_path, manifest, evidence_path, evidence)

    assert_source_refused(tmp_path, manifest_path)
