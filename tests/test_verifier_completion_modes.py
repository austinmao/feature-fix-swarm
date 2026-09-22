"""Completion-mode contracts for the Spec 014 read-only verifier."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest
from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts/verification/parallel_host_parity.py"


def module():
    spec = importlib.util.spec_from_file_location("spec014_completion_verifier", VERIFIER)
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


def artifact(path: Path) -> dict[str, str]:
    return {"locator": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def manifest(tmp_path: Path, gate: str, value: object) -> dict[str, object]:
    candidate = tmp_path / "candidate.tar"
    candidate.write_bytes(b"sealed candidate\n")
    digest = artifact(candidate)["sha256"]
    return {
        "schema": "ffs.parallel-host-verification/v1",
        "binding": {"run": "spec-014", "activity": gate, "attempt": "1"},
        "label": "authenticated" if gate in {"hosts", "rollout"} else "hermetic",
        "candidate": artifact(candidate),
        "provenance": {key: digest for key in
                       ("source_sha256", "binary_sha256", "bundle_sha256", "config_sha256")},
        "ac_ids": ["AC-039"], "path_ids": ["PATH-001"], "int_ids": ["INT-001"],
        gate: value,
    }


def aggregate_result(verifier, gate: str, candidate_sha: str) -> dict[str, object]:
    source = {
        "binding": {"run": "spec-014", "activity": gate, "attempt": "1"},
        "ac_ids": [f"AC-{number:03d}" for number in range(1, 61)],
        "path_ids": [f"PATH-{number:03d}" for number in range(1, 25)],
        "int_ids": [], "label": "authenticated",
        "provenance": {key: candidate_sha for key in verifier.PROVENANCE},
    }
    details = {
        "audit": {"domains": ["security"], "finding_count": 0,
                  "reviewer": {"host": "claude", "model": "opus", "session": "review-1"}},
        "coverage": {"line_min": 80, "coverage": {
            "lines_covered": 1, "lines_valid": 1, "branches_covered": 0,
            "branches_valid": 0, "line_percent": 100, "branch_percent": None,
            "branch_opportunities": False, "files": {"lib/example.py": {"lines_valid": 1}},
        }},
        "hosts": {"pairings": sorted(verifier.PAIRINGS),
                  "review_directions": sorted(verifier.REVIEW_DIRECTIONS),
                  "soak_seconds": 600, "row_count": 6},
        "installation-lifecycle": {"row_count": 24, "platforms": sorted(verifier.PLATFORMS)},
        "matrix": {"repetitions": 25, "platforms": sorted(verifier.PLATFORMS),
                   "case_count": 1, "row_count": 2},
        "migration": {"source_count": 1,
                      "record_counts": {"source": 1, "imported": 1, "quarantined": 0},
                      "run_count": 1},
        "rollout": {"consumer": "/tmp/consumer", "surface_count": 1,
                    "fork_count": 0, "canary_count": 4},
        "upgrade-comparison": {
            "new_failures": [], "missing_tests": [], "remaining_failures": [],
            "suite_passed": True, "comparison_passed": True,
            "baseline_sha256": candidate_sha, "ledger_sha256": candidate_sha,
            "ledger_entries": [{}], "suite_ids": ["suite"], "missing_suites": [],
            "new_suites": [], "suite_observations": [{}], "provenance_drift": False,
            "before_provenance": source["provenance"], "after_provenance": source["provenance"],
            "comparison_binding_sha256": None,
        },
    }
    purpose = verifier.AGGREGATE_RESULT_PURPOSE[gate]
    result = verifier._pass(source, gate, purpose, **details[gate])
    if gate == "hosts":
        result.update({"authenticated": True, "host": "mixed", "model": "exact-recorded"})
    return result


def test_matrix_requires_25_complete_rows_on_both_platforms(tmp_path: Path) -> None:
    verifier = module()
    receipt = tmp_path / "matrix.json"
    receipt.write_text("{}\n")
    value = manifest(tmp_path, "matrix", {
        "required_case_ids": ["reservation-race"],
        "rows": [
            {"case_id": "reservation-race", "platform": platform,
             "repetitions": 25, "status": "PASS", "artifact": artifact(receipt)}
            for platform in ("darwin", "linux")
        ],
    })
    assert verifier.matrix_mode(value, "hermetic", 25)["status"] == "PASS"
    value["matrix"]["rows"][0]["repetitions"] = 24
    with pytest.raises(verifier.E, match="25 repetitions"):
        verifier.matrix_mode(value, "hermetic", 25)


def test_hosts_require_six_productive_soaks_and_all_exact_tiers(tmp_path: Path) -> None:
    verifier = module()
    receipt = tmp_path / "host.json"
    receipt.write_text("{}\n")
    pairings = ("claude-claude", "claude-codex", "codex-codex")
    tiers = (("codex", "astra"), ("codex", "sol"), ("codex", "terra"), ("codex", "luna"),
             ("claude", "fable"), ("claude", "opus"), ("claude", "sonnet"), ("claude", "haiku"))
    value = manifest(tmp_path, "hosts", {
        "review_directions": ["claude-codex", "codex-claude"],
        "rows": [
            {"pairing": pairing, "platform": platform, "overlap_seconds": 600,
             "productive": True, "authenticated": True, "status": "PASS", "artifact": artifact(receipt)}
            for pairing in pairings for platform in ("darwin", "linux")
        ],
        "tier_rows": [
            {"host": host, "tier": tier, "requested_model": host + "-" + tier,
             "actual_model": host + "-" + tier,
             "status": "PASS", "artifact": artifact(receipt)}
            for host, tier in tiers
        ],
    })
    result = verifier.hosts_mode(value, True, list(pairings),
                                 ["claude-codex", "codex-claude"], 600)
    assert result["status"] == "PASS" and result["authenticated"] is True
    value["hosts"]["rows"][0]["overlap_seconds"] = 599
    with pytest.raises(verifier.E, match="600-second"):
        verifier.hosts_mode(value, True, list(pairings),
                            ["claude-codex", "codex-claude"], 600)
    value["hosts"]["rows"][0]["overlap_seconds"] = 600
    value["hosts"]["tier_rows"][0]["actual_model"] = "fallback-model"
    with pytest.raises(verifier.E, match="exact host tier"):
        verifier.hosts_mode(value, True, list(pairings),
                            ["claude-codex", "codex-claude"], 600)


def test_coverage_mode_uses_the_repository_production_inventory(tmp_path: Path) -> None:
    verifier = module()
    files = sorted(
        path.relative_to(ROOT).as_posix()
        for top in ("lib", "scripts", "skills")
        for path in (ROOT / top).rglob("*.py")
        if path.is_file() and not {"tests", "vendor", ".staging", "node_modules", "__pycache__"}.intersection(
            path.relative_to(ROOT).parts)
    )
    classes = "".join(
        f'<class filename="{name}"><lines><line number="1" hits="1"/></lines></class>'
        for name in files
    )
    report = tmp_path / "coverage.xml"
    report.write_text(
        f'<coverage lines-covered="{len(files)}" lines-valid="{len(files)}" '
        f'branches-covered="0" branches-valid="0"><packages><package><classes>{classes}'
        "</classes></package></packages></coverage>"
    )
    value = manifest(tmp_path, "coverage", {})
    candidate_path = Path(value["candidate"]["locator"])
    candidate_path.write_text(json.dumps({
        "schema": "ffs.source-closure/v1",
        "files": [{"path": name, "type": "file", "mode": 0o644, "sha256": "a" * 64}
                  for name in files],
    }) + "\n")
    candidate_digest = artifact(candidate_path)["sha256"]
    value["candidate"] = artifact(candidate_path)
    value["provenance"] = {key: candidate_digest for key in verifier.PROVENANCE}
    execution = tmp_path / "coverage-execution.json"
    execution.write_text(json.dumps({
        "schema": "ffs.coverage-execution/v1", "source_sha256": candidate_digest,
        "xml_sha256": artifact(report)["sha256"], "argv": ["pytest", "--cov"],
        "exit_status": 0, "started_utc": verifier.now(), "completed_utc": verifier.now(),
    }) + "\n")
    value["coverage"] = {"xml": artifact(report), "execution": artifact(execution)}
    result = verifier.coverage_mode(value, 80)
    assert result["status"] == "PASS"
    assert result["coverage"]["line_percent"] == 100
    report.write_text(report.read_text().replace(files[0], "lib/not-in-repository.py"))
    value["coverage"]["xml"] = artifact(report)
    execution_value = json.loads(execution.read_text())
    execution_value["xml_sha256"] = value["coverage"]["xml"]["sha256"]
    execution.write_text(json.dumps(execution_value) + "\n")
    value["coverage"]["execution"] = artifact(execution)
    with pytest.raises(verifier.E, match="inventory mismatch"):
        verifier.coverage_mode(value, 80)


def test_aggregate_rejects_forged_minimal_pass_envelopes(tmp_path: Path) -> None:
    verifier = module()
    candidate_sha = "a" * 64
    results = []
    for gate in sorted(verifier.REQUIRED_AGGREGATE_GATES):
        result = tmp_path / f"{gate}.json"
        result.write_text(json.dumps({
            "schema": verifier.SCHEMA, "gate": gate, "status": "PASS",
            "provenance": {key: candidate_sha for key in verifier.PROVENANCE},
            "ac_ids": [f"AC-{number:03d}" for number in range(1, 61)],
            "path_ids": [f"PATH-{number:03d}" for number in range(1, 25)],
        }) + "\n")
        results.append(artifact(result))
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"schema": "ffs.verification-index/v1",
                                 "candidate_sha256": candidate_sha, "results": results}) + "\n")
    with pytest.raises(verifier.E, match="aggregate_result"):
        verifier.aggregate_mode(index, True, "final")


def test_installation_lifecycle_requires_all_24_cells(tmp_path: Path) -> None:
    verifier = module()
    receipt = tmp_path / "installation.json"
    receipt.write_text("{}\n")
    value = manifest(tmp_path, "installation", {
        "lifecycle_rows": [
            {"operation": operation, "scope": scope, "platform": platform,
             "status": "PASS", "artifact": artifact(receipt)}
            for operation in ("fresh-install", "upgrade", "collision", "interruption", "rollback", "uninstall")
            for scope in ("project", "user") for platform in ("darwin", "linux")
        ],
        "protected_unchanged": True,
    })
    result = verifier.installation_mode(value, "lifecycle")
    assert result["status"] == "PASS" and result["row_count"] == 24
    value["installation"]["lifecycle_rows"].pop()
    with pytest.raises(verifier.E, match="24"):
        verifier.installation_mode(value, "lifecycle")


def test_audit_migration_and_rollout_require_byte_bound_completion_evidence(tmp_path: Path) -> None:
    verifier = module()
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}\n")
    evidence = artifact(receipt)
    audit = manifest(tmp_path, "audit", {
        "required_domains": ["security"],
        "domains": [{"id": "security", "status": "PASS", "artifact": evidence}],
        "findings": [],
        "producer": {"host": "codex", "model": "terra", "session": "producer"},
        "reviewer": {"host": "claude", "model": "opus", "session": "reviewer"},
        "review_pin": evidence,
    })
    assert verifier.audit_mode(audit)["status"] == "PASS"

    migration = manifest(tmp_path, "migration", {
        "sources": [evidence], "journal": evidence,
        "record_counts": {"source": 1, "imported": 1, "quarantined": 0},
        "id_bindings": [{"source_id": "legacy-db", "source_run_id": "old-run",
                         "canonical_run_id": "new-run", "evidence": evidence}],
        "epochs": [{"run_id": "new-run", "epoch": 1, "writer": "new", "proof": evidence}],
        "owner_fence": evidence, "interlock": evidence,
        "restart": {"status": "PASS", "artifact": evidence},
        "rollback": {"status": "PASS", "artifact": evidence},
        "legacy_unchanged": True,
    })
    assert verifier.migration_mode(migration, "verify-legacy")["status"] == "PASS"
    migration["migration"]["id_bindings"].append(dict(migration["migration"]["id_bindings"][0]))
    with pytest.raises(verifier.E, match="one-to-one"):
        verifier.migration_mode(migration, "verify-legacy")

    source = tmp_path / "source-surface"
    staged = tmp_path / "staged-surface"
    source.write_bytes(b"canonical\n")
    staged.write_bytes(source.read_bytes())
    rollout = manifest(tmp_path, "rollout", {
        "consumer": str(tmp_path),
        "surfaces": [{"path": "skills/fix/SKILL.md", "owner": "ffs",
                      "source": artifact(source), "staged": artifact(staged), "status": "PASS"}],
        "forks": [],
        "canaries": [
            {"platform": platform, "round": round_, "status": "PASS",
             "productive_overlap_seconds": 600, "artifact": evidence}
            for platform in ("darwin", "linux") for round_ in (1, 2)
        ],
        "protected_unchanged": True, "rollback": evidence,
    })
    assert verifier.rollout_mode(rollout, tmp_path)["status"] == "PASS"
    staged.write_bytes(b"different\n")
    rollout["rollout"]["surfaces"][0]["staged"] = artifact(staged)
    with pytest.raises(verifier.E, match="canonical bytes"):
        verifier.rollout_mode(rollout, tmp_path)


def test_aggregate_rejects_full_shaped_receipts_without_gate_proof(tmp_path: Path) -> None:
    verifier = module()
    candidate_sha = "c" * 64
    result_paths = []
    for gate in sorted(verifier.REQUIRED_AGGREGATE_GATES):
        result = tmp_path / f"complete-{gate}.json"
        result.write_text(json.dumps(aggregate_result(verifier, gate, candidate_sha)) + "\n")
        result_paths.append(result)
    index = tmp_path / "complete-index.json"

    def write_index() -> None:
        index.write_text(json.dumps({
            "schema": "ffs.verification-index/v1", "candidate_sha256": candidate_sha,
            "results": [artifact(path) for path in result_paths],
        }) + "\n")

    write_index()
    with pytest.raises(verifier.E, match="aggregate_result"):
        verifier.aggregate_mode(index, True, "final")


def test_aggregate_revalidates_the_indexed_gate_manifest(tmp_path: Path) -> None:
    verifier = module()
    receipt = tmp_path / "matrix-receipt.json"
    receipt.write_text("{}\n")
    value = manifest(tmp_path, "matrix", {
        "required_case_ids": ["reservation-race"],
        "rows": [
            {"case_id": "reservation-race", "platform": platform,
             "repetitions": 25, "status": "PASS", "artifact": artifact(receipt)}
            for platform in ("darwin", "linux")
        ],
    })
    manifest_path = tmp_path / "matrix-manifest.json"
    manifest_path.write_text(json.dumps(value) + "\n")
    result = verifier.matrix_mode(value, "hermetic", 25)
    result["verification_proof"] = {"manifest": artifact(manifest_path), "inputs": {}}
    checked = verifier._aggregate_result(result, verifier.Budget())
    assert checked["gate"] == "matrix"
    result["gate_vector"] = {key: True for key in result["gate_vector"]}
    for key in result["gate_vector"]:
        result[key] = True
    with pytest.raises(verifier.E, match="does not match revalidated"):
        verifier._aggregate_result(result, verifier.Budget())
    result["gate_vector"] = {key: False for key in result["gate_vector"]}
    for key in result["gate_vector"]:
        result[key] = False
    value["matrix"]["rows"][0]["repetitions"] = 24
    manifest_path.write_text(json.dumps(value) + "\n")
    result["verification_proof"]["manifest"] = artifact(manifest_path)
    with pytest.raises(verifier.E, match="25 repetitions"):
        verifier._aggregate_result(result, verifier.Budget())


def test_published_schema_rejects_proofless_completion_results(tmp_path: Path) -> None:
    verifier = module()
    schema = json.loads((ROOT / "schemas/parallel-host-verification.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator({
        "$ref": "#/$defs/result_envelope", "$defs": schema["$defs"],
    })
    result = aggregate_result(verifier, "matrix", "e" * 64)
    errors = list(validator.iter_errors(result))
    assert errors and any("verification_proof" in error.message for error in errors)
    descriptor = {"locator": str(tmp_path / "proof.json"), "sha256": "f" * 64}
    result["verification_proof"] = {"manifest": descriptor, "inputs": {"baseline": descriptor}}
    assert list(validator.iter_errors(result)), "matrix proof inputs must be empty"
    upgrade = aggregate_result(verifier, "upgrade-comparison", "e" * 64)
    upgrade["verification_proof"] = {"manifest": descriptor, "inputs": {}}
    assert list(validator.iter_errors(upgrade)), "upgrade proof must name its baseline"
