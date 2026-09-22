"""Independent acceptance of verification evidence; never accept invented PASS."""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest


def verifier():
    path = Path(__file__).resolve().parents[1] / "scripts/verification/parallel_host_parity.py"
    assert path.is_file(), "The evidence verifier must exist before it can certify a phase"
    spec = importlib.util.spec_from_file_location("parallel_host_parity", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_unchanged_baseline_failures_are_preserved_not_called_green():
    result = verifier().compare_baselines(
        {"completed": True, "tests": {"one": "PASS", "known": "FAIL"}},
        {"completed": True, "tests": {"one": "PASS", "known": "FAIL"}},
    )
    assert result["new_failures"] == []
    assert result["remaining_failures"] == ["known"]
    assert result["suite_passed"] is False


def test_new_failure_and_missing_test_are_upgrade_regressions():
    result = verifier().compare_baselines(
        {"completed": True, "tests": {"one": "PASS", "two": "PASS"}},
        {"completed": True, "tests": {"one": "FAIL"}},
    )
    assert result["status"] == "FAIL"
    assert result["new_failures"] == ["one"]
    assert result["missing_tests"] == ["two"]


@pytest.mark.parametrize("result", [{}, {"completed": False, "tests": {"one": "PASS"}},
                                       {"completed": True, "tests": {}}])
def test_incomplete_or_empty_results_cannot_establish_baseline_comparison(result):
    comparison = verifier().compare_baselines({"completed": True, "tests": {"one": "PASS"}}, result)
    assert comparison["status"] == "UNMET"


@pytest.mark.parametrize("record", [{}, {"status": "PASS"},
    {"status": "PASS", "paths": ["PATH-001"], "artifacts": []}])
def test_unattributed_self_report_cannot_satisfy_path(record):
    verdict = verifier().validate_evidence(record, ["PATH-001"])
    assert verdict["status"] != "PASS"
    assert verdict["errors"]


def test_coverage_counts_expose_line_and_branch_separately(tmp_path):
    report = tmp_path / "coverage.xml"
    lines = '<line number="1" hits="1" branch="true" condition-coverage="25% (1/4)"/>'
    lines += ''.join(f'<line number="{n}" hits="{1 if n <= 8 else 0}"/>' for n in range(2, 11))
    report.write_text('<coverage lines-covered="8" lines-valid="10" branches-covered="1" branches-valid="4">'
                      '<packages><package name="lib"><classes><class filename="lib/example.py"><lines>' + lines + '</lines></class></classes>'
                      '</package></packages></coverage>')
    result = verifier().coverage_totals(report)
    assert result["line_percent"] == 80.0
    assert result["branch_percent"] == 25.0
    assert result["lines_valid"] == 10


def test_zero_denominator_coverage_is_not_a_pass(tmp_path):
    report = tmp_path / "coverage.xml"
    report.write_text('<coverage lines-covered="0" lines-valid="0" branches-covered="0" branches-valid="0"/>')
    with pytest.raises(ValueError):
        verifier().coverage_totals(report)


def test_forged_artifact_digest_is_rejected(tmp_path):
    artifact = tmp_path / "proof.json"
    artifact.write_text('{"actual": "FAIL"}')
    record = {"status": "PASS", "paths": ["PATH-001"], "artifacts": [
        {"locator": str(artifact), "sha256": "0" * 64}]}
    result = verifier().validate_evidence(record, ["PATH-001"])
    assert result["status"] != "PASS", "Hash strings must be checked against actual bytes"


def test_missing_artifact_is_not_proof(tmp_path):
    record = {"status": "PASS", "paths": ["PATH-001"], "artifacts": [
        {"locator": str(tmp_path / "does-not-exist"), "sha256": "a" * 64}]}
    assert verifier().validate_evidence(record, ["PATH-001"])["status"] != "PASS"


@pytest.mark.parametrize("manifest", [[], 12, "self report", {"reviewer":"same", "model":"same",
    "artifact_sha256":"fake", "result":"PASS", "severe_path_disposition":"waived"}])
def test_review_cli_rejects_unstructured_or_self_report_only_evidence(tmp_path, manifest):
    source = tmp_path / "input.json"
    target = tmp_path / "output.json"
    source.write_text(json.dumps(manifest))
    script = Path(__file__).resolve().parents[1] / "scripts/verification/parallel_host_parity.py"
    result = subprocess.run([sys.executable, str(script), "review", "--manifest", str(source),
                             "--output", str(target)], capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    payload = json.loads(target.read_text())
    assert payload["status"] != "PASS"


def review_fixture(tmp_path):
    candidate = tmp_path / "candidate.py"
    candidate.write_text("print('actual candidate bytes')\n")
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    artifact = tmp_path / "review.json"
    review = {"verdict": "PASS", "findings": [], "reviewed_sha256": digest}
    artifact.write_text(json.dumps(review))
    manifest = {
        "schema": "ffs.parallel-host-verification/v1",
        "binding": {"run": "spec-014", "activity": "review-upgraded", "attempt": "attempt-1"},
        "platform": sys.platform, "label": "hermetic",
        "command": ["review-adapter", "--candidate", str(candidate)],
        "started_utc": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), "completed_utc": datetime.now(timezone.utc).isoformat(),
        "exit_status": 0,
        "provenance": {key: digest for key in ("source_sha256", "binary_sha256", "bundle_sha256", "config_sha256")},
        "ac_ids": ["AC-010"], "path_ids": ["PATH-002"], "int_ids": ["INT-001"],
        "reviewer": {"host": "claude", "model": "claude-opus-5", "session": "review-1"},
        "producer": {"host": "codex", "model": "gpt-5.6-terra", "session": "producer-1"},
        "candidate": {"locator": str(candidate), "sha256": digest},
        "artifact_sha256": digest,
        "artifacts": [{"locator": str(candidate), "sha256": digest},
                      {"locator": str(artifact), "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}],
        "result": {"locator": str(artifact)}, "severe_path_disposition": [],
    }
    return manifest, review


def run_review(tmp_path, manifest, review, *flags):
    artifact = tmp_path / "review.json"
    artifact.write_text(json.dumps(review))
    for item in manifest.get("artifacts", []):
        if item.get("locator") == str(artifact):
            item["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    source, target = tmp_path / "input.json", tmp_path / "output.json"
    source.write_text(json.dumps(manifest))
    script = Path(__file__).resolve().parents[1] / "scripts/verification/parallel_host_parity.py"
    if "--purpose" not in flags:
        flags = (*flags, "--purpose", "review-completion")
    result = subprocess.run([sys.executable, str(script), "review", "--manifest", str(source),
                             "--output", str(target), *flags], capture_output=True, text=True, timeout=10)
    assert "Traceback" not in result.stderr
    payload = json.loads(target.read_text()) if target.exists() and not target.is_symlink() else None
    return result, payload, target


def test_review_cli_accepts_hash_bound_independent_artifact_with_no_open_severe(tmp_path):
    manifest, review = review_fixture(tmp_path)
    result, payload, target = run_review(tmp_path, manifest, review)
    assert result.returncode == 0, result.stdout + result.stderr
    assert payload["status"] == "PASS"
    assert payload["review_complete"] is True
    assert payload["path_admitted"] is False
    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("findings", [None, {}, "clean", ["clean"], [12], [{"severity": "mystery"}]])
def test_malformed_findings_never_admit_review(tmp_path, findings):
    manifest, review = review_fixture(tmp_path)
    review["findings"] = findings
    result, payload, _ = run_review(tmp_path, manifest, review)
    assert result.returncode != 0
    assert payload and payload["status"] != "PASS" and not payload.get("path_admitted")


def test_fail_verdict_with_empty_findings_does_not_admit(tmp_path):
    manifest, review = review_fixture(tmp_path)
    review["verdict"] = "FAIL"
    result, payload, _ = run_review(tmp_path, manifest, review)
    assert result.returncode != 0
    assert payload and not payload.get("path_admitted")


@pytest.mark.parametrize("severity", ["high", "HIGH", "Critical", "critical"])
@pytest.mark.parametrize("disposition", [["waived"], [{"finding_id": "F1", "action": "defer", "owner": "agent"}]])
def test_disposition_never_waives_severe_path_admission(tmp_path, severity, disposition):
    manifest, review = review_fixture(tmp_path)
    review["findings"] = [{"id": "F1", "severity": severity, "status": "open", "summary": "unsafe admission"}]
    manifest["severe_path_disposition"] = disposition
    result, payload, _ = run_review(tmp_path, manifest, review)
    assert result.returncode != 0
    assert payload and payload["status"] != "PASS" and not payload.get("path_admitted")
    assert not payload.get("rollout_ready")


@pytest.mark.parametrize("authority", ["valid", "absent", "wrong-owner", "wrong-candidate"])
def test_review_completion_preserves_blocked_paths(tmp_path, monkeypatch, authority):
    """Only explicitly selected supervisor authority can assign a severe repair."""
    manifest, review = review_fixture(tmp_path)
    review["verdict"] = "FAIL"
    review["findings"] = [{"id": "F1", "severity": "high", "status": "open",
                           "summary": "unsafe admission"}]
    assignment = {"finding_id": "F1", "action": "assign-repair", "owner": "executor-m3",
                  "path_ids": ["PATH-002"], "owning_phase": "05",
                  "regression_contract": "tests/test_run_context_acceptance.py",
                  "authority_id": "repair-F1"}
    manifest["severe_path_disposition"] = [assignment]
    private = tmp_path / "supervisor"
    private.mkdir(mode=0o700)
    authority_file = private / "authority.json"
    authority_record = {"schema": "ffs.verification-repair-authority/v1",
                        "run": "spec-014", "candidate_sha256": manifest["candidate"]["sha256"],
                        "assignments": [dict(assignment)]}
    if authority == "wrong-owner":
        authority_record["assignments"][0]["owner"] = "another-executor"
    elif authority == "wrong-candidate":
        authority_record["candidate_sha256"] = "0" * 64
    authority_file.write_text(json.dumps(authority_record))
    authority_file.chmod(0o600)
    if authority == "absent":
        monkeypatch.delenv("FFS_VERIFICATION_AUTHORITY", raising=False)
    else:
        monkeypatch.setenv("FFS_VERIFICATION_AUTHORITY", str(authority_file))
    result, payload, _ = run_review(tmp_path, manifest, review)
    assert payload, result.stdout + result.stderr
    assert payload["path_admitted"] is False
    assert payload["rollout_ready"] is False
    if authority == "valid":
        assert result.returncode == 0, result.stdout + result.stderr
        assert payload["status"] == "PASS"
        assert payload["review_complete"] is True
        assert payload["repair_authorized"] is True
        assert payload["blocked_paths"] == ["PATH-002"]
    else:
        assert result.returncode != 0
        assert payload["repair_authorized"] is False


@pytest.mark.parametrize("mutation", ["both-absent", "forged-equal", "candidate-absent", "candidate-changed"])
def test_candidate_identity_is_bound_to_actual_bytes(tmp_path, mutation):
    manifest, review = review_fixture(tmp_path)
    if mutation == "both-absent":
        manifest.pop("artifact_sha256"); review.pop("reviewed_sha256")
    elif mutation == "forged-equal":
        manifest["artifact_sha256"] = review["reviewed_sha256"] = "a" * 64
    elif mutation == "candidate-absent":
        manifest.pop("candidate")
        manifest["artifacts"] = manifest["artifacts"][1:]
    else:
        Path(manifest["candidate"]["locator"]).write_text("different candidate")
    result, payload, _ = run_review(tmp_path, manifest, review)
    assert result.returncode != 0
    assert payload and payload["status"] != "PASS"


@pytest.mark.parametrize("field", ["binding", "platform", "label", "command", "started_utc", "completed_utc", "provenance"])
def test_incomplete_provenance_cannot_admit(tmp_path, field):
    manifest, review = review_fixture(tmp_path)
    manifest.pop(field)
    result, payload, _ = run_review(tmp_path, manifest, review)
    assert result.returncode != 0
    assert payload and payload["status"] != "PASS"


def test_documented_upgraded_stage_flag_and_complete_envelope(tmp_path):
    manifest, review = review_fixture(tmp_path)
    result, payload, _ = run_review(tmp_path, manifest, review, "--stage", "upgraded")
    assert result.returncode == 0, result.stdout + result.stderr
    for key in ("schema", "gate", "binding", "platform", "label", "command", "started_utc", "completed_utc",
                "exit_status", "provenance", "ac_ids", "path_ids", "int_ids", "artifacts", "reviewer",
                "review_complete", "repair_authorized", "path_admitted", "rollout_ready", "errors"):
        assert key in payload, key
    assert payload["binding"] == manifest["binding"]
    assert payload["provenance"]["source_sha256"] == manifest["candidate"]["sha256"]


@pytest.mark.parametrize("status", ["SKIPPED", None, [], {}, 1])
def test_invalid_baseline_status_is_unmet_without_exception(status):
    result = verifier().compare_baselines(
        {"completed": True, "tests": {"one": "PASS"}},
        {"completed": True, "tests": {"one": status}},
    )
    assert result["status"] == "UNMET"


def test_symlink_artifact_ancestor_cannot_supply_evidence(tmp_path):
    real = tmp_path / "real"; real.mkdir()
    proof = real / "proof.json"; proof.write_text('{"status":"PASS"}')
    alias = tmp_path / "alias"; alias.symlink_to(real, target_is_directory=True)
    result = verifier().validate_evidence({"status": "PASS", "paths": ["PATH-001"], "artifacts": [
        {"locator": str(alias / "proof.json"), "sha256": hashlib.sha256(proof.read_bytes()).hexdigest()}]}, ["PATH-001"])
    assert result["status"] != "PASS"


def test_review_output_cannot_replace_symlink_target(tmp_path):
    manifest, review = review_fixture(tmp_path)
    protected = tmp_path / "protected.json"
    protected.write_text("preserve unrelated content")
    target = tmp_path / "output.json"; target.symlink_to(protected)
    result, _, _ = run_review(tmp_path, manifest, review)
    assert result.returncode != 0
    assert target.is_symlink()
    assert protected.read_text() == "preserve unrelated content"


def test_review_output_preserves_existing_private_file_and_emits_refusal(tmp_path):
    manifest, _ = review_fixture(tmp_path)
    source = tmp_path / "input.json"
    source.write_text(json.dumps(manifest))
    target = tmp_path / "private-note.txt"
    target.write_text("unrelated private note\n")
    target.chmod(0o600)
    script = Path(__file__).resolve().parents[1] / "scripts/verification/parallel_host_parity.py"
    result = subprocess.run([sys.executable, str(script), "review", "--manifest", str(source),
                             "--purpose", "review-completion", "--output", str(target)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert target.read_text() == "unrelated private note\n"
    assert result.stdout.strip().startswith("{"), "Persistence refusal must still emit JSON"
    payload = json.loads(result.stdout)
    assert payload["status"] == "FAIL"
    assert payload["path_admitted"] is False
    assert payload["rollout_ready"] is False


def test_review_output_cannot_traverse_symlink_directory(tmp_path):
    manifest, review = review_fixture(tmp_path)
    artifact = tmp_path / "review.json"
    artifact.write_text(json.dumps(review))
    source = tmp_path / "input.json"; source.write_text(json.dumps(manifest))
    protected = tmp_path / "protected"; protected.mkdir()
    alias = tmp_path / "alias"; alias.symlink_to(protected, target_is_directory=True)
    script = Path(__file__).resolve().parents[1] / "scripts/verification/parallel_host_parity.py"
    result = subprocess.run([sys.executable, str(script), "review", "--manifest", str(source),
                             "--output", str(alias / "result.json")], capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert not (protected / "result.json").exists()
    assert "Traceback" not in result.stderr


def test_unverified_result_locator_is_not_opened(tmp_path):
    manifest, review = review_fixture(tmp_path)
    fifo = tmp_path / "unverified-fifo"
    import os
    os.mkfifo(fifo)
    manifest["result"] = {"locator": str(fifo)}
    # Opening this unverified locator blocks forever; refusal must finish by deadline.
    result, payload, _ = run_review(tmp_path, manifest, review)
    assert result.returncode != 0
    assert payload and not payload.get("path_admitted")


def test_review_parses_the_bytes_it_hashed_not_a_second_path_read(tmp_path, monkeypatch):
    manifest, review = review_fixture(tmp_path)
    artifact = tmp_path / "review.json"
    review["verdict"] = "FAIL"
    artifact.write_text(json.dumps(review))
    manifest["artifacts"][1]["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    source, target = tmp_path / "input.json", tmp_path / "output.json"
    source.write_text(json.dumps(manifest))
    real_read_text = Path.read_text
    forged = dict(review, verdict="PASS")

    def switched_read(path, *args, **kwargs):
        # Model a filesystem replacement precisely at the old hash/read seam.
        if path == artifact:
            return json.dumps(forged)
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", switched_read)
    monkeypatch.setattr(sys, "argv", ["verifier", "review", "--manifest", str(source), "--output", str(target), "--purpose", "review-completion"])
    assert verifier().main() != 0
    assert not json.loads(real_read_text(target)).get("path_admitted")


def test_imported_review_never_admits_on_claimed_reviewer_identity(tmp_path):
    manifest, review = review_fixture(tmp_path)
    result, payload, _ = run_review(tmp_path, manifest, review, "--purpose", "admission")
    assert result.returncode != 0
    assert payload and not payload.get("path_admitted")


def test_unknown_review_manifest_fields_are_rejected(tmp_path):
    manifest, review = review_fixture(tmp_path)
    manifest["unrecognized_execution_override"] = "unsafe"
    result, payload, _ = run_review(tmp_path, manifest, review)
    assert result.returncode != 0
    assert payload and payload["status"] != "PASS"


def test_verifier_schema_matches_runtime_validation():
    schema_path = Path(__file__).resolve().parents[1] / "schemas/parallel-host-verification.schema.json"
    assert schema_path.is_file(), "Published verifier schema must exist"
    schema = json.loads(schema_path.read_text())
    module = verifier()
    assert hasattr(module, "manifest_fields"), "Public per-mode field contract is needed for schema conformance"
    for gate in ("baseline", "installation", "upgrade", "review"):
        contract = module.manifest_fields(gate)
        published = schema["$defs"][gate + "_manifest"]
        assert published["additionalProperties"] is False
        assert set(published["properties"]) == set(contract["allowed"])
        if gate == "review":
            imported = next(variant for variant in published["oneOf"] if "result" in variant["required"])
            assert set(published["required"]) | set(imported["required"]) == set(contract["required"])
            fresh = next(variant for variant in published["oneOf"] if "review_adapter" in variant["required"])
            assert set(published["required"]) | set(fresh["required"]) == set(module.COMMON) | {"producer", "review_adapter"}
        else:
            assert set(published["required"]) == set(contract["required"])
