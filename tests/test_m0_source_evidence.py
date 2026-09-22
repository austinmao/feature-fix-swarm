"""Independent M0 canonical-source acceptance.

The fixture cases drive the public baseline CLI.  They intentionally require
the versioned M0 profile described by Phase 02; a generic four-role
``source_runtime`` observation is not canonical-install proof.
"""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts/verification/parallel_host_parity.py"
SCHEMA = "ffs.parallel-host-verification/v1"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n")
    return path


def clean_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith(("GIT_", "GSD_")) or name in {
            "FFS_VERIFICATION_MANIFEST", "FFS_VERIFICATION_AUTHORITY",
            "FFS_M0_SOURCE_REPORT", "FFS_M0_BACKUP_REPORT", "FFS_M0_BASELINE",
            "FFS_M0_MANIFEST", "FFS_M0_MEASUREMENTS",
            "FFS_M0_SOURCE_HISTORICAL_REPORT", "FFS_M0_SOURCE_REFERENCE_REPORT",
        }:
            env.pop(name, None)
    return env


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, env=clean_env(), timeout=10
    ).strip()


def init_repo(repo: Path) -> None:
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=clean_env(), timeout=10)
    (repo / "skills/demo").mkdir(parents=True)
    (repo / "skills/demo/SKILL.md").write_text("canonical skill bytes\n")
    (repo / "setup.sh").write_text("#!/bin/sh\nexit 0\n")
    subprocess.run(["git", "-C", str(repo), "add", "setup.sh", "skills/demo/SKILL.md"],
                   check=True, env=clean_env(), timeout=10)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=M0 Acceptance",
                    "-c", "user.email=m0@example.invalid", "commit", "-qm", "fixture"],
                   check=True, env=clean_env(), timeout=10)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", git(repo, "rev-parse", "HEAD")],
                   check=True, env=clean_env(), timeout=10)


def artifact(path: Path) -> dict[str, str]:
    return {"locator": str(path), "sha256": sha(path)}


def utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def suite(tmp_path: Path) -> dict[str, object]:
    program = tmp_path / "suite.py"
    program.write_text("import json\nprint(json.dumps({'tests': {'source-fixture': 'PASS'}}))\n")
    started = utc()
    run = subprocess.run([sys.executable, str(program)], text=True, capture_output=True,
                         check=False, env=clean_env(), timeout=10)
    completed = utc()
    record = write_json(tmp_path / "suite.json", {
        "argv": [sys.executable, str(program)], "exit_status": run.returncode,
        "stdout": run.stdout, "stderr": run.stderr, "started_utc": started,
        "completed_utc": completed, "tests": {"source-fixture": "PASS"},
    })
    return {"id": "source-fixture", **artifact(record), "argv": [sys.executable, str(program)],
            "exit_status": run.returncode, "started_utc": started, "completed_utc": completed}


def doctor(tmp_path: Path) -> Path:
    program = tmp_path / "doctor.py"
    program.write_text("print('doctor: PASS')\n")
    started = utc()
    run = subprocess.run([sys.executable, str(program)], text=True, capture_output=True,
                         check=False, env=clean_env(), timeout=10)
    return write_json(tmp_path / "doctor.json", {
        "schema": "ffs.doctor-observation/v1", "argv": [sys.executable, str(program)],
        "exit_status": run.returncode, "started_utc": started, "completed_utc": utc(),
        "stdout": run.stdout, "stderr": run.stderr,
    })


def source_fixture(tmp_path: Path, mutation: str = "valid") -> tuple[Path, dict[str, str]]:
    canonical = tmp_path / "canonical"
    init_repo(canonical)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    shutil.copytree(canonical / "skills", consumer / "skills")
    staged = canonical / ".agents/skills/demo/SKILL.md"
    staged.parent.mkdir(parents=True)
    shutil.copy2(canonical / "skills/demo/SKILL.md", staged)
    source_root = consumer if mutation == "consumer-source" else canonical
    if mutation == "changed-stage":
        staged.write_text("changed after doctor PASS\n")
    elif mutation == "uncommitted-generation":
        (canonical / "skills/demo/SKILL.md").write_text("matching dirty bytes outside declared generation\n")
        staged.write_bytes((canonical / "skills/demo/SKILL.md").read_bytes())

    rendered_source = str(consumer) if mutation == "consumer-source" else "."
    install = write_json(canonical / ".feature-fix-swarm/install-manifest.json", {
        "schema": "ffs.install/v1", "version": "fixture", "scope": "project",
        "installed_at": utc(), "source": rendered_source,
        "paths": {".agents/skills/demo/SKILL.md": {
            "fingerprint": "file:" + sha(canonical / "skills/demo/SKILL.md")}},
        "gsd": {"owner": "upstream-installer", "version": "fixture",
                "profiles": {"claude": "full", "codex": "full"}},
    })
    doctor_record = doctor(tmp_path)
    binary = tmp_path / "python.bin"; binary.write_bytes(b"runtime\n")
    bundle = tmp_path / "bundle"; bundle.write_bytes(b"bundle\n")
    config = tmp_path / "config"; config.write_bytes(b"config\n")
    provenance = {
        "source_sha256": sha(canonical / "setup.sh"), "binary_sha256": sha(binary),
        "bundle_sha256": sha(bundle), "config_sha256": sha(config),
    }
    evidence = write_json(tmp_path / "source-runtime.json", {
        "schema": "ffs.full-inventory-evidence/v2", "category": "source_runtime",
        "status": "PASS", "complete": True, "started_utc": utc(), "completed_utc": utc(),
        "provenance": provenance,
        "observations": {
            "entries": [
                {"role": "source", "artifact": artifact(canonical / "setup.sh")},
                {"role": "binary", "artifact": artifact(binary)},
                {"role": "bundle", "artifact": artifact(bundle)},
                {"role": "config", "artifact": artifact(config)},
            ],
            "canonical_source": {
                "manifests": [{
                    "scope": "project", "install_manifest": artifact(install),
                    "project_root": str(canonical),
                    "resolved_source_root": str(source_root.resolve()),
                    "repository": {"root": str(canonical.resolve()),
                        "common_dir": str((canonical / git(canonical, "rev-parse", "--git-common-dir")).resolve()),
                        "head": git(canonical, "rev-parse", "HEAD"),
                        "generation": git(canonical, "rev-parse", "HEAD")},
                    "doctor_artifact": artifact(doctor_record),
                    "mappings": [{"managed_path": ".agents/skills/demo/SKILL.md", "type": "file",
                        "source_artifact": artifact(source_root / "skills/demo/SKILL.md"),
                        "staged_artifact": artifact(staged)}],
                }],
                "upstream_mappings": [], "current_dirty_inventory": [],
            },
        },
    })
    candidate = canonical / "setup.sh"
    manifest = {
        "schema": SCHEMA, "binding": {"run": "m0-source-fixture", "activity": "baseline", "attempt": "1"},
        "label": "hermetic", "candidate": artifact(candidate), "provenance": provenance,
        "ac_ids": ["AC-003"], "path_ids": ["PATH-001"], "int_ids": ["INT-001"],
        "repository": str(canonical), "baseline_profile": "m0/v1",
        "baseline": {"suite_artifacts": [suite(tmp_path)]},
        "full_inventory": {"source_runtime": {"status": "PASS", "artifacts": [artifact(evidence)]}},
    }
    return write_json(tmp_path / f"manifest-{mutation}.json", manifest), {
        "install": sha(install), "source": sha(source_root / "skills/demo/SKILL.md"),
        "staged": sha(staged), "doctor": sha(doctor_record),
    }


def invoke(tmp_path: Path, manifest: Path) -> tuple[subprocess.CompletedProcess[str], dict]:
    output = tmp_path / "output" / "baseline.json"
    run = subprocess.run([sys.executable, str(VERIFIER), "baseline", "--manifest", str(manifest),
                          "--output", str(output)], text=True, capture_output=True, check=False,
                         env=clean_env(), timeout=20)
    payload = json.loads(run.stdout)
    return run, payload


def error_code(payload: dict) -> str:
    assert len(payload["errors"]) == 1
    return payload["errors"][0]["code"]


def test_fixture_canonical_install_manifest_and_staged_bytes_are_verified(tmp_path: Path) -> None:
    manifest, before = source_fixture(tmp_path)
    run, payload = invoke(tmp_path, manifest)
    assert run.returncode == 0, payload
    assert payload["baseline_profile"] == "m0/v1"
    assert payload["canonical_source_verified"] is True
    assert before == source_fixture_hashes(manifest)


def source_fixture_hashes(manifest_path: Path) -> dict[str, str]:
    manifest = json.loads(manifest_path.read_text())
    wrapper_path = Path(manifest["full_inventory"]["source_runtime"]["artifacts"][0]["locator"])
    canonical = json.loads(wrapper_path.read_text())["observations"]["canonical_source"]["manifests"][0]
    return {"install": sha(Path(canonical["install_manifest"]["locator"])),
            "source": sha(Path(canonical["mappings"][0]["source_artifact"]["locator"])),
            "staged": sha(Path(canonical["mappings"][0]["staged_artifact"]["locator"])),
            "doctor": sha(Path(canonical["doctor_artifact"]["locator"]))}


def test_fixture_consumer_checkout_is_not_canonical_even_with_identical_bytes_and_doctor_pass(tmp_path: Path) -> None:
    manifest, before = source_fixture(tmp_path, "consumer-source")
    run, payload = invoke(tmp_path, manifest)
    assert run.returncode != 0
    assert payload["status"] == "UNMET" and error_code(payload) == "SOURCE_CANONICAL"
    assert before == source_fixture_hashes(manifest)


def test_fixture_altered_staged_byte_is_rejected_despite_doctor_pass(tmp_path: Path) -> None:
    manifest, before = source_fixture(tmp_path, "changed-stage")
    run, payload = invoke(tmp_path, manifest)
    assert run.returncode != 0
    assert payload["status"] == "UNMET" and error_code(payload) == "SOURCE_CANONICAL"
    assert before == source_fixture_hashes(manifest)


def test_fixture_matching_dirty_bytes_do_not_belong_to_the_declared_git_generation(tmp_path: Path) -> None:
    manifest, before = source_fixture(tmp_path, "uncommitted-generation")
    run, payload = invoke(tmp_path, manifest)
    assert run.returncode != 0
    assert payload["status"] == "UNMET" and error_code(payload) == "SOURCE_CANONICAL"
    assert before == source_fixture_hashes(manifest)


def required_report(name: str, *, fallback: str | None = None) -> dict:
    raw = os.environ.get(name) or (os.environ.get(fallback) if fallback else None)
    assert raw, f"{name} is required for actual_evidence"
    path = Path(raw)
    assert path.is_absolute() and path.is_file(), f"missing actual evidence: {path}"
    return json.loads(path.read_text())


def safe_relative_path(raw: str) -> Path:
    path = Path(raw)
    assert raw and not path.is_absolute()
    assert not ({"", ".", ".."} & set(path.parts))
    return path


def filesystem_tree(root: Path) -> dict[str, str]:
    assert root.is_dir() and not root.is_symlink()
    result = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        kind = "symlink" if path.is_symlink() else "directory" if path.is_dir() else "file"
        result[relative] = kind
    return result


def directory_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            value = b"L\0" + relative + b"\0" + os.readlink(path).encode()
        elif path.is_file():
            value = b"F\0" + relative + b"\0" + sha(path).encode()
        elif path.is_dir():
            value = b"D\0" + relative
        else:
            value = b"O\0" + relative
        digest.update(value + b"\n")
    return "dir:" + digest.hexdigest()


def generation_fingerprint(entries: list[dict]) -> str:
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda item: item["relative_path"]):
        relative = entry["relative_path"].encode()
        if entry["type"] == "symlink":
            value = b"L\0" + relative + b"\0" + entry["source"]["link_target"].encode()
        elif entry["type"] == "file":
            value = b"F\0" + relative + b"\0" + entry["source"]["sha256"].encode()
        else:
            value = b"D\0" + relative
        digest.update(value + b"\n")
    return "dir:" + digest.hexdigest()


def assert_leaf_mapping(entry: dict) -> None:
    safe_relative_path(entry["relative_path"])
    kind = entry["type"]
    assert kind in {"file", "symlink"}
    source, staged = entry["source"], entry["staged"]
    source_path, staged_path = Path(source["locator"]), Path(staged["locator"])
    assert source["type"] == staged["type"] == kind
    if kind == "file":
        assert source_path.is_file() and not source_path.is_symlink()
        assert staged_path.is_file() and not staged_path.is_symlink()
        assert sha(source_path) == source["sha256"] == staged["sha256"] == sha(staged_path)
        assert entry["git_mode"] in {"100644", "100755"}
        assert source["mode"] == staged["mode"] == int(entry["git_mode"], 8) & 0o777
    else:
        assert source_path.is_symlink() and staged_path.is_symlink()
        assert os.readlink(source_path) == source["link_target"]
        assert os.readlink(staged_path) == staged["link_target"] == source["link_target"]
        assert entry["git_mode"] == "120000"


def outside_paths(row: dict, field: str) -> list[str]:
    result = [item if isinstance(item, str) else item["relative_path"] for item in row[field]]
    assert len(result) == len(set(result))
    for relative in result:
        safe_relative_path(relative)
    return result


def assert_directory_mapping(row: dict) -> tuple[int, list[str], int]:
    source, staged = row["source"], row["staged"]
    assert source["type"] == staged["type"] == "directory"
    source_root, staged_root = Path(source["locator"]), Path(staged["locator"])
    assert source_root.is_dir() and not source_root.is_symlink()
    assert staged_root.is_dir() and not staged_root.is_symlink()
    entries = row["entries"]
    assert source["fingerprint"] == directory_fingerprint(source_root)
    assert staged["fingerprint"] == directory_fingerprint(staged_root)
    assert row["expected_fingerprint"] == source["fingerprint"] == staged["fingerprint"]
    relative_paths = [entry["relative_path"] for entry in entries]
    assert entries and len(relative_paths) == len(set(relative_paths))
    declared_types = {entry["relative_path"]: entry["type"] for entry in entries}
    for entry in entries:
        safe_relative_path(entry["relative_path"])
        assert entry["type"] in {"directory", "file", "symlink"}
        if entry["type"] == "directory":
            assert (source_root / entry["relative_path"]).is_dir()
            assert (staged_root / entry["relative_path"]).is_dir()
        else:
            assert_leaf_mapping(entry)
    source_outside = outside_paths(row, "source_entries_outside_generation")
    staged_outside = outside_paths(row, "stage_entries_outside_generation")
    assert not set(source_outside) & set(relative_paths)
    assert not set(staged_outside) & set(relative_paths)
    source_types = filesystem_tree(source_root)
    staged_types = filesystem_tree(staged_root)
    assert set(source_types) == set(relative_paths) | set(source_outside)
    assert set(staged_types) == set(relative_paths) | set(staged_outside)
    assert all(source_types[path] == kind for path, kind in declared_types.items())
    assert all(staged_types[path] == kind for path, kind in declared_types.items())
    immutable_fingerprint = generation_fingerprint(entries)
    if source_outside or staged_outside:
        assert immutable_fingerprint != row["expected_fingerprint"]
    else:
        assert immutable_fingerprint == row["expected_fingerprint"]
    return (
        sum(entry["type"] != "directory" for entry in entries),
        source_outside + staged_outside,
        sum(staged_types[path] != "directory" for path in staged_outside),
    )


def test_actual_evidence_directory_oracle_rejects_unreported_generation_extras(
    tmp_path: Path,
) -> None:
    source_root, staged_root = tmp_path / "source", tmp_path / "staged"
    source_root.mkdir()
    staged_root.mkdir()
    for root in (source_root, staged_root):
        (root / "SKILL.md").write_text("committed bytes\n")
        (root / "SKILL.md").chmod(0o644)
    file_sha = sha(source_root / "SKILL.md")
    entries = [{
        "relative_path": "SKILL.md", "type": "file", "git_mode": "100644",
        "source": {"type": "file", "locator": str(source_root / "SKILL.md"),
                   "sha256": file_sha, "mode": 0o644},
        "staged": {"type": "file", "locator": str(staged_root / "SKILL.md"),
                   "sha256": file_sha, "mode": 0o644},
    }]
    fingerprint = directory_fingerprint(source_root)
    row = {
        "expected_fingerprint": fingerprint,
        "source": {"type": "directory", "locator": str(source_root),
                   "fingerprint": fingerprint},
        "staged": {"type": "directory", "locator": str(staged_root),
                   "fingerprint": fingerprint},
        "entries": entries,
        "source_entries_outside_generation": [],
        "stage_entries_outside_generation": [],
    }
    assert assert_directory_mapping(row) == (1, [], 0)
    for root in (source_root, staged_root):
        (root / "untracked.pyc").write_bytes(b"generated\n")
    changed_fingerprint = directory_fingerprint(source_root)
    row["expected_fingerprint"] = changed_fingerprint
    row["source"]["fingerprint"] = changed_fingerprint
    row["staged"]["fingerprint"] = changed_fingerprint
    with pytest.raises(AssertionError):
        assert_directory_mapping(row)


@pytest.mark.xfail(
    reason="terminal RETAIN_UNMET historical input disposition (2026-09-17)",
    raises=AssertionError,
    strict=True,
)
def test_actual_evidence_incomplete_report_integrity_preserves_directory_manifests_and_unmet() -> None:
    # This is the retained historical capture.  It must remain incomplete under
    # AD-016; a current reference is a separately selected artifact below.
    report = required_report("FFS_M0_SOURCE_HISTORICAL_REPORT", fallback="FFS_M0_SOURCE_REPORT")
    assert report["schema"] == "ffs.m0-source-evidence/v1"
    assert report["baseline_profile"] == "m0/v1"
    assert report["manifests"]
    directory_count = 0
    file_count = 0
    leaf_count = 0
    staged_extra_leaf_count = 0
    all_outside = []
    for manifest in report["manifests"]:
        assert manifest["scope"] in {"project", "user"}
        assert (manifest["project_root"] is None) == (manifest["scope"] == "user")
        mappings = manifest["mappings"]
        assert mappings and len({row["managed_path"] for row in mappings}) == len(mappings)
        assert set(manifest["install_paths"]) == {row["managed_path"] for row in mappings}
        for row in mappings:
            assert ".." not in Path(row["managed_path"]).parts
            source, staged = Path(row["source"]["locator"]), Path(row["staged"]["locator"])
            mapping_type = row.get("type")
            assert mapping_type in {"file", "directory", "symlink"}, (
                "historical source mapping lacks a typed file/directory/symlink contract"
            )
            if mapping_type == "file":
                file_count += 1
                assert row["source"]["type"] == row["staged"]["type"] == "file"
                assert source.is_file() and not source.is_symlink() and sha(source) == row["source"]["sha256"]
                assert staged.is_file() and not staged.is_symlink() and sha(staged) == row["staged"]["sha256"]
                assert row["source"]["sha256"] == row["staged"]["sha256"]
                assert row["expected_fingerprint"] == "file:" + row["source"]["sha256"]
            elif mapping_type == "directory":
                directory_count += 1
                leaves, outside, staged_extra_leaves = assert_directory_mapping(row)
                leaf_count += leaves
                staged_extra_leaf_count += staged_extra_leaves
                all_outside.extend(outside)
            else:
                assert mapping_type == "symlink"
                assert row["source"]["type"] == row["staged"]["type"] == "symlink"
                assert source.is_symlink() and staged.is_symlink()
                assert os.readlink(source) == row["source"]["link_target"]
                assert os.readlink(staged) == row["staged"]["link_target"] == row["source"]["link_target"]
                assert row["expected_fingerprint"] == "symlink:" + row["source"]["link_target"]
    assert sum(len(manifest["mappings"]) for manifest in report["manifests"]) == 54
    assert directory_count == 50
    assert file_count == 4
    assert leaf_count == 66
    assert staged_extra_leaf_count == 4
    assert leaf_count + file_count + staged_extra_leaf_count == 74
    assert all_outside, "current staged bytes outside the pinned generation must remain visible"
    assert report["complete"] is False and report["unmet"]
    assert any("outside" in item.lower() or "generation" in item.lower() for item in report["unmet"])
    negatives = {row["id"]: row for row in report["negative_results"]}
    assert set(negatives) == {"consumer-source-doctor-pass", "changed-stage-doctor-pass"}
    assert all(row["doctor_status"] == "PASS" and row["verification_status"] == "UNMET"
               and row["error"] == "SOURCE_CANONICAL" for row in negatives.values())
    assert all(row["before_sha256"] == row["after_sha256"] for row in report["unchanged"])


def checked_json_artifact(value: dict) -> dict:
    path = Path(value["locator"])
    assert path.is_absolute() and path.is_file() and not path.is_symlink()
    assert sha(path) == value["sha256"]
    return json.loads(path.read_text())


@pytest.mark.xfail(
    reason="terminal RETAIN_UNMET historical input disposition (2026-09-17)",
    raises=AssertionError,
    strict=True,
)
def test_complete_source_evidence_requires_actual_cli_proof_and_attributable_negatives() -> None:
    # Do not point this assertion at the historical report: one report cannot
    # honestly be both the permanently incomplete historical capture and the
    # separately labelled current observed-upgraded-reference.
    report = required_report("FFS_M0_SOURCE_REFERENCE_REPORT")
    assert report.get("evidence_role") == "observed-upgraded-reference"
    assert report["schema"] == "ffs.m0-source-evidence/v1"
    assert report["complete"] is True and report["unmet"] == []
    assert report["schema_validation"] == {"valid": True, "errors": []}
    positive = report["positive_result"]
    assert positive["doctor_status"] == "PASS"
    assert positive["verification_status"] == "PASS"
    assert positive.get("error") is None and positive.get("problem") is None
    cli_result = checked_json_artifact(positive["verifier_output"])
    assert cli_result["status"] == "PASS"
    assert cli_result["canonical_source_verified"] is True
    assert cli_result["full_baseline_complete"] is False
    assert set(cli_result["gate_vector"].values()) == {False}

    negatives = {row["id"]: row for row in report["negative_results"]}
    assert set(negatives) == {"consumer-source-doctor-pass", "changed-stage-doctor-pass"}
    consumer = negatives["consumer-source-doctor-pass"]
    changed = negatives["changed-stage-doctor-pass"]
    for row in negatives.values():
        assert row["doctor_status"] == "PASS" and row["doctor_exit_code"] == 0
        assert row["verification_status"] == "UNMET" and row["error"] == "SOURCE_CANONICAL"
        negative_result = checked_json_artifact(row["verifier_output"])
        assert negative_result["status"] == "UNMET"
        assert negative_result["errors"][0]["code"] == "SOURCE_CANONICAL"
    assert consumer["before_sha256"] == consumer["after_sha256"]
    assert any(term in consumer["problem"].lower() for term in ("repository", "generation"))
    assert changed["before_sha256"] != changed["after_sha256"]
    assert any(term in changed["problem"].lower() for term in ("byte", "fingerprint", "stage"))
