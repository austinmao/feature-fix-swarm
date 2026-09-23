"""Independent M0 backup and recovery-read acceptance."""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts/verification/parallel_host_parity.py"
SCHEMA = "ffs.parallel-host-verification/v1"
REQUIRED_SURFACES = {
    "claude-profile", "codex-profile", "activation-snapshot", "shared-skill-incident",
    "node-custom-tap", "node-old-keg", "node-global-npm", "gstack-original",
    "gstack-customizations", "homebrew-snapshot",
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n")
    return path


def utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith(("GIT_", "GSD_")) or name.startswith("FFS_M0_") or name in {
            "FFS_VERIFICATION_MANIFEST", "FFS_VERIFICATION_AUTHORITY",
        }:
            env.pop(name, None)
    return env


def artifact(path: Path, locator: str | None = None) -> dict[str, str]:
    return {"locator": locator or str(path), "sha256": sha(path)}


def suite(tmp_path: Path) -> dict[str, object]:
    script = tmp_path / "suite.py"
    script.write_text("import json\nprint(json.dumps({'tests': {'backup-fixture': 'PASS'}}))\n")
    started = utc()
    run = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                         check=False, env=clean_env(), timeout=10)
    completed = utc()
    result = write_json(tmp_path / "suite.json", {
        "argv": [sys.executable, str(script)], "exit_status": run.returncode,
        "stdout": run.stdout, "stderr": run.stderr, "started_utc": started,
        "completed_utc": completed, "tests": {"backup-fixture": "PASS"},
    })
    return {"id": "backup-fixture", **artifact(result), "argv": [sys.executable, str(script)],
            "exit_status": run.returncode, "started_utc": started, "completed_utc": completed}


def backup_manifest(tmp_path: Path, mutation: str = "valid") -> tuple[Path, dict[str, str]]:
    candidate = tmp_path / "candidate"; candidate.write_bytes(b"candidate\n")
    binary = tmp_path / "binary"; binary.write_bytes(b"binary\n")
    bundle = tmp_path / "bundle"; bundle.write_bytes(b"bundle\n")
    config = tmp_path / "config"; config.write_bytes(b"config\n")
    provenance = {"source_sha256": sha(candidate), "binary_sha256": sha(binary),
                  "bundle_sha256": sha(bundle), "config_sha256": sha(config)}
    entries = []
    sentinels: dict[str, str] = {}
    for name in sorted(REQUIRED_SURFACES):
        original = tmp_path / "preserved" / name
        restored = tmp_path / "recovery" / name
        original.parent.mkdir(parents=True, exist_ok=True)
        restored.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes((name + " preserved bytes\n").encode())
        restored.write_bytes(original.read_bytes())
        verification = write_json(tmp_path / "verification" / f"{name}.json", {
            "name": name, "status": "PASS", "expected_sha256": sha(original),
            "backup_sha256": sha(original), "restored_sha256": sha(restored),
        })
        entries.append({"name": name, "expected_sha256": sha(original),
                        "backup_artifact": artifact(original), "restored_artifact": artifact(restored),
                        "verification_artifact": artifact(verification)})
        sentinels[str(original)] = sha(original)
    if mutation == "missing-surface":
        entries.pop()
    elif mutation == "duplicate-surface":
        entries[-1]["name"] = entries[0]["name"]
    elif mutation == "corrupt-restore":
        restored = Path(entries[0]["restored_artifact"]["locator"])
        restored.write_bytes(b"corrupt\n")
        entries[0]["restored_artifact"]["sha256"] = sha(restored)
    elif mutation == "traversal":
        original = Path(entries[0]["backup_artifact"]["locator"])
        entries[0]["backup_artifact"]["locator"] = str(original.parent / "x" / ".." / original.name)
    elif mutation == "symlink":
        restored = Path(entries[0]["restored_artifact"]["locator"])
        target = restored.with_name(restored.name + "-target")
        restored.rename(target)
        restored.symlink_to(target.name)
        entries[0]["restored_artifact"]["sha256"] = sha(restored)

    wrapper = write_json(tmp_path / f"backup-evidence-{mutation}.json", {
        "schema": "ffs.full-inventory-evidence/v2", "category": "backups_recovery",
        "status": "PASS", "complete": True, "started_utc": utc(), "completed_utc": utc(),
        "provenance": provenance, "observations": {
            "required_surface_ids": sorted(REQUIRED_SURFACES), "entries": entries},
    })
    manifest = write_json(tmp_path / f"manifest-{mutation}.json", {
        "schema": SCHEMA, "binding": {"run": "m0-backup-fixture", "activity": "baseline", "attempt": "1"},
        "label": "hermetic", "candidate": artifact(candidate), "provenance": provenance,
        "ac_ids": ["AC-002"], "path_ids": ["PATH-001"], "int_ids": ["INT-001"],
        "baseline_profile": "m0/v1", "baseline": {"suite_artifacts": [suite(tmp_path)]},
        "full_inventory": {"backups_recovery": {"status": "PASS", "artifacts": [artifact(wrapper)]}},
    })
    return manifest, sentinels


def invoke(tmp_path: Path, manifest: Path) -> tuple[subprocess.CompletedProcess[str], dict]:
    output = tmp_path / "output" / "result.json"
    run = subprocess.run([sys.executable, str(VERIFIER), "baseline", "--manifest", str(manifest),
                          "--output", str(output)], text=True, capture_output=True, check=False,
                         env=clean_env(), timeout=20)
    return run, json.loads(run.stdout)


def assert_preserved(sentinels: dict[str, str]) -> None:
    assert {path: sha(Path(path)) for path in sentinels} == sentinels


def error_code(payload: dict) -> str:
    assert len(payload["errors"]) == 1
    return payload["errors"][0]["code"]


def test_fixture_all_pinned_backup_surfaces_are_byte_recoverable(tmp_path: Path) -> None:
    manifest, sentinels = backup_manifest(tmp_path)
    run, payload = invoke(tmp_path, manifest)
    assert run.returncode == 0, payload
    validated = payload["full_inventory_evidence"]["backups_recovery"][0]["validated"]
    assert set(validated["entries"]) == REQUIRED_SURFACES
    assert payload["full_baseline_complete"] is False
    assert_preserved(sentinels)


def test_fixture_missing_required_backup_surface_is_unmet(tmp_path: Path) -> None:
    manifest, sentinels = backup_manifest(tmp_path, "missing-surface")
    run, payload = invoke(tmp_path, manifest)
    assert run.returncode != 0 and error_code(payload) == "BACKUP_EVIDENCE"
    assert_preserved(sentinels)


def test_fixture_duplicate_required_backup_surface_is_unmet(tmp_path: Path) -> None:
    manifest, sentinels = backup_manifest(tmp_path, "duplicate-surface")
    run, payload = invoke(tmp_path, manifest)
    assert run.returncode != 0 and error_code(payload) == "BACKUP_EVIDENCE"
    assert_preserved(sentinels)


def test_fixture_corrupt_recovery_bytes_are_unmet(tmp_path: Path) -> None:
    manifest, sentinels = backup_manifest(tmp_path, "corrupt-restore")
    run, payload = invoke(tmp_path, manifest)
    assert run.returncode != 0 and error_code(payload) == "BACKUP_EVIDENCE"
    assert_preserved(sentinels)


def test_fixture_lexical_traversal_artifact_locator_is_refused(tmp_path: Path) -> None:
    manifest, sentinels = backup_manifest(tmp_path, "traversal")
    run, payload = invoke(tmp_path, manifest)
    assert run.returncode != 0 and error_code(payload) == "UNSAFE_PATH"
    assert_preserved(sentinels)


def test_fixture_symlink_recovery_artifact_is_refused(tmp_path: Path) -> None:
    manifest, sentinels = backup_manifest(tmp_path, "symlink")
    run, payload = invoke(tmp_path, manifest)
    assert run.returncode != 0 and error_code(payload) == "UNSAFE_INPUT"
    assert_preserved(sentinels)


def report_from_env(name: str) -> tuple[Path, dict]:
    raw = os.environ.get(name)
    # The evidence gate opts in; ordinary CI has no operator reports and skips.
    if not raw and os.environ.get("FFS_REQUIRE_ACTUAL_EVIDENCE") == "1":
        pytest.fail(f"{name} is required when FFS_REQUIRE_ACTUAL_EVIDENCE=1")
    if not raw:
        pytest.skip(f"actual evidence not selected: set {name}")
    path = Path(raw)
    assert path.is_absolute() and path.is_file(), f"missing actual evidence: {path}"
    return path, json.loads(path.read_text())


def link_digest(path: Path) -> str:
    return hashlib.sha256(os.readlink(path).encode()).hexdigest()


def test_actual_evidence_every_pinned_surface_is_readable_and_recoverable() -> None:
    _, report = report_from_env("FFS_M0_BACKUP_REPORT")
    assert report["schema"] == "ffs.m0-backup-evidence/v1"
    assert report["complete"] is True and report["unmet"] == []
    assert set(report["required_surface_ids"]) == REQUIRED_SURFACES
    surfaces = report["surfaces"]
    assert len(surfaces) == len(REQUIRED_SURFACES)
    assert {row["id"] for row in surfaces} == REQUIRED_SURFACES
    for surface in surfaces:
        assert surface["readable"] is True and surface["unmet"] == []
        assert surface["artifacts"] and surface["recovery"]
        for item in surface["artifacts"]:
            path = Path(item["locator"])
            assert path.is_absolute() and ".." not in path.parts
            if item["type"] == "file":
                assert path.is_file() and not path.is_symlink() and sha(path) == item["sha256"]
            else:
                assert item["type"] == "symlink" and path.is_symlink()
                assert os.readlink(path) == item["link_target"] and link_digest(path) == item["sha256"]
        for recovery in surface["recovery"]:
            source, restored = Path(recovery["source"]["locator"]), Path(recovery["restored"]["locator"])
            assert source.is_file() and not source.is_symlink()
            assert restored.is_file() and not restored.is_symlink()
            assert sha(source) == recovery["source"]["sha256"]
            assert sha(restored) == recovery["restored"]["sha256"] == recovery["source"]["sha256"]
            assert recovery["argv"] and recovery["exit_status"] == 0
            assert recovery["started_utc"] <= recovery["completed_utc"]
    manifest = report["manifest"]
    manifest_path = Path(manifest["locator"])
    assert manifest_path.is_file() and not manifest_path.is_symlink() and sha(manifest_path) == manifest["sha256"]
    assert manifest["entry_count"] == manifest["regular_file_count"] + manifest["symlink_count"]
    assert all(row["before_sha256"] == row["after_sha256"] for row in report["unchanged"])
