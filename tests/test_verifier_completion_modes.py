"""Completion-mode contracts for the Spec 014 read-only verifier."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest
from jsonschema import Draft202012Validator

from test_m1_upgrade_evidence import _changed_comparison_fixture


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
        "review-completion:upgraded": {
            "reviewer": {"host": "claude", "model": "opus", "session": "review-1"},
            "producer": {"host": "codex", "model": "terra", "session": "producer-1"},
            "findings": [], "evidence_origin": "imported", "identity_assurance": "asserted",
            "blocked_paths": [], "review_execution": {"argv": ["review"], "exit_status": 0},
        },
        "experiment": {"issue": "4588", "issue_status": "open", "outcome": "sequential-fallback",
                       "observed_utc": "2026-10-11T00:00:00Z"},
        "docs": {"topic_count": len(verifier.DOC_TOPICS), "example_count": 1},
        "authorization": {"history_count": 1},
    }
    purpose = verifier.AGGREGATE_RESULT_PURPOSE[gate]
    result = verifier._pass(source, gate, purpose, **details[gate])
    if gate == "hosts":
        result.update({"authenticated": True, "host": "mixed", "model": "exact-recorded"})
    return result


def review_rows(transcript: Path) -> list[dict[str, object]]:
    """One row per cross-family review direction: the outer host, the opposite reviewer and its transcript."""
    return [{"direction": f"{outer}-{reviewer}", "outer_host": outer, "reviewer_host": reviewer,
             "artifact": artifact(transcript)} for outer, reviewer in (("claude", "codex"), ("codex", "claude"))]


def hosts_value(receipt: Path, reviews: object) -> dict[str, object]:
    """A complete hosts section: six productive soaks, eight exact tiers and the given review rows."""
    tiers = (("codex", "astra"), ("codex", "sol"), ("codex", "terra"), ("codex", "luna"),
             ("claude", "fable"), ("claude", "opus"), ("claude", "sonnet"), ("claude", "haiku"))
    return {
        "review_directions": reviews,
        "rows": [
            {"pairing": pairing, "platform": platform, "overlap_seconds": 600,
             "productive": True, "authenticated": True, "status": "PASS", "artifact": artifact(receipt)}
            for pairing in ("claude-claude", "claude-codex", "codex-codex") for platform in ("darwin", "linux")
        ],
        "tier_rows": [
            {"host": host, "tier": tier, "requested_model": host + "-" + tier, "actual_model": host + "-" + tier,
             "status": "PASS", "artifact": artifact(receipt)}
            for host, tier in tiers
        ],
    }


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
        "review_directions": review_rows(receipt),
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


def _hosts(verifier, value: dict[str, object]) -> dict[str, object]:
    return verifier.hosts_mode(value, True, sorted(verifier.PAIRINGS), sorted(verifier.REVIEW_DIRECTIONS), 600)


def _swap_hosts(row: dict[str, object]) -> None:
    row["outer_host"], row["reviewer_host"] = row["reviewer_host"], row["outer_host"]


# spec-014 E8 prerequisite 4b (PATH-014, FR-046): a review direction is a row binding who ran the outer, who
# reviewed it and the review transcript, not a bare string that any evidence could carry.
_REVIEW_ROW_REFUSALS = {
    "bare-strings": (lambda rows: ["claude-codex", "codex-claude"], "HOST_REVIEW_ROW"),
    "hosts-disagree-with-direction": (lambda rows: [dict(rows[0], outer_host="codex", reviewer_host="claude"),
                                                    rows[1]], "HOST_REVIEW_ROW"),
    "same-host-review": (lambda rows: [dict(rows[0], direction="claude-claude", reviewer_host="claude"), rows[1]],
                         "HOST_REVIEW_ROW"),
    "same-host-under-a-cross-direction": (lambda rows: [dict(rows[0], reviewer_host="claude"), rows[1]],
                                          "HOST_REVIEW_ROW"),
    "duplicate-direction": (lambda rows: [rows[0], rows[0]], "HOST_REVIEW_ROW"),
    "missing-direction": (lambda rows: [rows[0]], "HOST_MATRIX"),
    "missing-transcript": (lambda rows: [{key: value for key, value in rows[0].items() if key != "artifact"},
                                         rows[1]], "HOST_REVIEW_ROW"),
    "extra-field": (lambda rows: [dict(rows[0], status="PASS"), rows[1]], "HOST_REVIEW_ROW"),
}


def test_hosts_bind_each_review_direction_to_opposite_hosts_and_a_transcript(tmp_path: Path) -> None:
    verifier = module()
    receipt, transcript = tmp_path / "host.json", tmp_path / "review-transcript.jsonl"
    receipt.write_text("{}\n")
    transcript.write_text('{"type":"result"}\n')
    result = _hosts(verifier, manifest(tmp_path, "hosts", hosts_value(receipt, review_rows(transcript))))
    assert result["status"] == "PASS"
    assert result["review_directions"] == review_rows(transcript)


@pytest.mark.parametrize("case", sorted(_REVIEW_ROW_REFUSALS))
def test_hosts_refuse_review_directions_that_do_not_bind_opposite_hosts(tmp_path: Path, case: str) -> None:
    verifier = module()
    receipt, transcript = tmp_path / "host.json", tmp_path / "review-transcript.jsonl"
    receipt.write_text("{}\n")
    transcript.write_text('{"type":"result"}\n')
    mutate, code = _REVIEW_ROW_REFUSALS[case]
    value = manifest(tmp_path, "hosts", hosts_value(receipt, mutate(review_rows(transcript))))
    with pytest.raises(verifier.E) as refused:
        _hosts(verifier, value)
    assert refused.value.code == code


def test_hosts_review_transcripts_are_byte_bound(tmp_path: Path) -> None:
    verifier = module()
    receipt, transcript = tmp_path / "host.json", tmp_path / "review-transcript.jsonl"
    receipt.write_text("{}\n")
    transcript.write_text('{"type":"result"}\n')
    value = manifest(tmp_path, "hosts", hosts_value(receipt, review_rows(transcript)))
    transcript.write_text('{"type":"result","forged":true}\n')
    with pytest.raises(verifier.E) as refused:
        _hosts(verifier, value)
    assert refused.value.code == "DIGEST_MISMATCH"


def test_aggregate_revalidates_the_hosts_review_rows(tmp_path: Path) -> None:
    verifier = module()
    receipt, transcript = tmp_path / "host.json", tmp_path / "review-transcript.jsonl"
    receipt.write_text("{}\n")
    transcript.write_text('{"type":"result"}\n')
    value = manifest(tmp_path, "hosts", hosts_value(receipt, review_rows(transcript)))
    manifest_path = tmp_path / "hosts-manifest.json"
    manifest_path.write_text(json.dumps(value) + "\n")
    result = _hosts(verifier, value)
    result["verification_proof"] = {"manifest": artifact(manifest_path), "inputs": {}}
    assert verifier._aggregate_result(result, verifier.Budget())["gate"] == "hosts"
    bare = dict(result, review_directions=sorted(verifier.REVIEW_DIRECTIONS))
    with pytest.raises(verifier.E) as refused:
        verifier._aggregate_result(bare, verifier.Budget())
    assert refused.value.code == "HOST_REVIEW_ROW"
    swapped = json.loads(json.dumps(result))
    _swap_hosts(swapped["review_directions"][0])
    with pytest.raises(verifier.E) as refused:
        verifier._aggregate_result(swapped, verifier.Budget())
    assert refused.value.code == "HOST_REVIEW_ROW"
    # A row naming another transcript than the revalidated manifest's does not match the gate evidence.
    other = tmp_path / "other-transcript.jsonl"
    other.write_text('{"type":"result","other":true}\n')
    rebound = json.loads(json.dumps(result))
    rebound["review_directions"][0]["artifact"] = artifact(other)
    with pytest.raises(verifier.E, match="does not match revalidated"):
        verifier._aggregate_result(rebound, verifier.Budget())


def test_published_schema_binds_review_rows_to_opposite_hosts(tmp_path: Path) -> None:
    verifier = module()
    schema = json.loads((ROOT / "schemas/parallel-host-verification.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    hosts = Draft202012Validator({"$ref": "#/$defs/hosts_manifest", "$defs": schema["$defs"]})
    results = Draft202012Validator({"$ref": "#/$defs/result_envelope", "$defs": schema["$defs"]})
    receipt, transcript = tmp_path / "host.json", tmp_path / "review-transcript.jsonl"
    receipt.write_text("{}\n")
    transcript.write_text('{"type":"result"}\n')
    value = manifest(tmp_path, "hosts", hosts_value(receipt, review_rows(transcript)))
    assert list(hosts.iter_errors(value)) == []
    for case in ("bare-strings", "hosts-disagree-with-direction", "same-host-review",
                 "same-host-under-a-cross-direction", "missing-transcript", "extra-field", "missing-direction",
                 "duplicate-direction"):
        mutate, _code = _REVIEW_ROW_REFUSALS[case]
        invalid = manifest(tmp_path, "hosts", hosts_value(receipt, mutate(review_rows(transcript))))
        assert list(hosts.iter_errors(invalid)), case
    manifest_path = tmp_path / "hosts-manifest.json"
    manifest_path.write_text(json.dumps(value) + "\n")
    result = _hosts(verifier, value)
    result["verification_proof"] = {"manifest": artifact(manifest_path), "inputs": {}}
    assert list(results.iter_errors(result)) == []
    assert list(results.iter_errors(dict(result, review_directions=sorted(verifier.REVIEW_DIRECTIONS))))


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
    domains = sorted(verifier.AUDIT_REQUIRED_DOMAINS | {"security"})
    audit = manifest(tmp_path, "audit", {
        "required_domains": domains,
        "domains": [{"id": domain, "status": "PASS", "artifact": evidence} for domain in domains],
        "findings": [],
        "producer": {"host": "codex", "model": "terra", "session": "producer"},
        "reviewer": {"host": "claude", "model": "opus", "session": "reviewer"},
        "review_pin": evidence,
    })
    assert verifier.audit_mode(audit)["status"] == "PASS"
    # The manifest cannot shrink the FR-036 to FR-038 domain floor by declaring fewer domains.
    narrowed = json.loads(json.dumps(audit))
    narrowed["audit"]["required_domains"] = ["security"]
    narrowed["audit"]["domains"] = [{"id": "security", "status": "PASS", "artifact": evidence}]
    with pytest.raises(verifier.E, match="FR-036"):
        verifier.audit_mode(narrowed)

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


# Each indexed gate claims only ids it owns (verifier.GATE_OWNED_IDS, derived from the spec's
# "E2E Test Paths" and "Required evidence traceability" tables).
HONEST_IDS = {
    "experiment": (["AC-047"], ["PATH-020"]),
    "docs": (["AC-056", "AC-057", "AC-058"], ["PATH-024"]),
    "authorization": (["AC-059", "AC-060"], ["PATH-024"]),
    "review-completion:upgraded": (["AC-010", "AC-031"], ["PATH-003"]),
}
BASELINE_IDS = (["AC-001", "AC-002", "AC-003", "AC-004"], ["PATH-001"])
# FU-upgrade-ledger-obligations: no gate proves these until the upgrade ledger checks them.
UNOWNED_IDS = {"AC-005", "AC-006", "AC-007", "AC-008"}


def missing_ids(error) -> set[str]:
    return set(error.problem.rsplit("missing ", 1)[1].split())


DOC_TOPIC_IDS = ("automatic-isolation", "commands", "dependencies", "evidence", "examples", "host-support",
                 "migration", "resume-takeover", "rollout-rollback", "setup", "tiers", "troubleshooting")


def owned(verifier, gate: str) -> tuple[list[str], list[str]]:
    ids = sorted(verifier.GATE_OWNED_IDS[gate])
    return [x for x in ids if x.startswith("AC-")], [x for x in ids if x.startswith("PATH-")]


def evidence_records(tmp_path: Path) -> dict[str, dict[str, str]]:
    """The structured records each obligation gate parses, written once per test."""
    same = "a" * 64
    stdout = tmp_path / "record-stdout.txt"
    stdout.write_text("usage: gsd-run [--help]\n")
    readme = tmp_path / "README.md"
    readme.write_text("documented workflow\n")
    claude_md, agents_md = tmp_path / "CLAUDE.md", tmp_path / "AGENTS.md"
    claude_md.write_text("repo Claude guidance\n")
    agents_md.write_text("repo Codex guidance\n")
    records = {
        "report": {"schema": "ffs.upstream-experiment/v1", "issue": "4588", "outcome": "sequential-fallback",
                   "issue_status": "open, upstream fix unreleased",
                   "observed_utc": "2026-10-11T00:00:00Z", "upstream_version": "gsd-core 1.13.2",
                   "runs": [{"workstreams": 2, "overlap_seconds": 0}]},
        "supported": {"schema": "ffs.supported-paths/v1", "execution_paths": ["/opt/gsd-core/worktrees/experiment-1"],
                      "path_implications": [{"path_id": "PATH-020",
                                             "implication": "sequential fallback; FFS supplies isolation"}]},
        "observed": {"schema": "ffs.command-observation/v1", "argv": ["gsd-run", "--help"],
                     "exit_status": 0, "stdout_sha256": artifact(stdout)["sha256"], "stdout": artifact(stdout)},
        "shared": {"schema": "ffs.shared-dependencies/v1", "dependencies": ["git object store", "gsd-core runtime"],
                   "self_contained": False,
                   "isolation_limit": "worktrees share one Git object store; isolation is cooperative"},
        "guidance": {"schema": "ffs.guidance-comparison/v1",
                     "repo_guidance": [{"path": "CLAUDE.md", "artifact": artifact(claude_md)},
                                       {"path": "AGENTS.md", "artifact": artifact(agents_md)}],
                     "user_global": {"before": same, "after": same},
                     "consumer_skills": {"before": same, "after": same}},
        "authorization": {"schema": "ffs.authorization-record/v1", "canonical_repository": "/repo/feature-fix-swarm",
                          "authorizations": [{"id": "auth-1", "action": "push", "scope": "origin/fix/branch"},
                                             {"id": "auth-2", "action": "commit", "scope": "/repo/feature-fix-swarm"}]},
        "history": {"schema": "ffs.repository-history/v1", "repository": "/repo/feature-fix-swarm",
                    "head": "b" * 40, "commits": [{"commit": "c" * 40, "authorization": "auth-2"}],
                    "pushes": [{"ref": "origin/fix/branch", "authorization": "auth-1"}]},
        "fixture-history": {"schema": "ffs.repository-history/v1", "repository": "/fixtures/run-1/repo",
                            "fixture_root": "/fixtures", "head": "e" * 40,
                            "commits": [{"commit": "f" * 40, "fixture": True}], "pushes": []},
        **{f"index-{category}": {"schema": "ffs.evidence-index/v1", "category": category, "entries": [category]}
           for category in ("results", "unmet", "hypotheses")},
        **{f"topic-{topic}": {"schema": "ffs.doc-topic/v1", "topic": topic, "documents": [artifact(readme)],
                              "statement": f"{topic} matches observed behavior"} for topic in DOC_TOPIC_IDS},
    }
    return {name: _write(tmp_path / f"record-{name}.json", value) for name, value in records.items()}


def obligation_values(evidence: dict[str, str], verifier, records: dict[str, dict[str, str]]) -> dict[str, dict[str, object]]:
    return {
        "experiment": {"issue": "4588", "issue_status": "open, upstream fix unreleased",
                       "observed_utc": "2026-10-11T00:00:00Z", "outcome": "sequential-fallback",
                       "report": records["report"], "supported_paths": records["supported"]},
        "docs": {"topics": [{"id": topic, "artifact": records[f"topic-{topic}"]} for topic in sorted(verifier.DOC_TOPICS)],
                 "examples": [{"argv": ["gsd-run", "--help"], "expected_exit_status": 0,
                               "observed": records["observed"]}],
                 "shared_dependencies": records["shared"], "guidance": records["guidance"],
                 "user_global_unchanged": True, "consumer_skills_unchanged": True},
        "authorization": {"record": records["authorization"],
                          "histories": [{"repository": "/repo/feature-fix-swarm", "artifact": records["history"]},
                                        {"repository": "/fixtures/run-1/repo", "artifact": records["fixture-history"]}],
                          "unauthorized_actions": [], "discarded_changes": [],
                          "evidence_index": {category: records[f"index-{category}"]
                                             for category in ("results", "unmet", "hypotheses")}},
    }


def test_aggregate_requires_review_completion_and_the_obligation_gates() -> None:
    verifier = module()
    assert {"review-completion:upgraded", "experiment", "docs", "authorization"} <= verifier.REQUIRED_AGGREGATE_GATES
    assert set(verifier.REQUIRED_AGGREGATE_GATES) == set(verifier.AGGREGATE_RESULT_FIELDS)


def test_obligation_gates_pass_only_on_complete_evidence(tmp_path: Path) -> None:
    verifier = module()
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}\n")
    evidence = artifact(receipt)
    modes = {"experiment": verifier.experiment_mode, "docs": verifier.docs_mode,
             "authorization": verifier.authorization_mode}
    records = evidence_records(tmp_path)
    for gate, value in obligation_values(evidence, verifier, records).items():
        result = modes[gate](manifest(tmp_path, gate, value))
        assert result["status"] == "PASS" and result["gate"] == gate, gate
        assert result["purpose"] == "verification"
        assert not any(result["gate_vector"].values())
    refusals = {
        "experiment": [("outcome", "assumed-parallel"), ("issue", "1234"), ("observed_utc", "yesterday")],
        "docs": [("user_global_unchanged", False), ("consumer_skills_unchanged", False), ("examples", [])],
        "authorization": [("unauthorized_actions", [{"kind": "push", "ref": "origin/main"}]),
                          ("discarded_changes", ["lib/x.py"]), ("histories", [])],
    }
    for gate, cases in refusals.items():
        for key, bad in cases:
            value = obligation_values(evidence, verifier, records)[gate]
            value[key] = bad
            with pytest.raises(verifier.E):
                modes[gate](manifest(tmp_path, gate, value))
    # A hash-bound but unrelated artifact is not evidence: each record must parse as its schema
    # and agree with the manifest it supports.
    unrelated = {"experiment": ("report",), "docs": ("guidance",), "authorization": ("histories",)}
    for gate, (key,) in unrelated.items():
        value = obligation_values(evidence, verifier, records)[gate]
        if key == "histories":
            value[key] = [{"repository": "/repo/feature-fix-swarm", "artifact": evidence}]
        else:
            value[key] = evidence
        with pytest.raises(verifier.E) as refused:
            modes[gate](manifest(tmp_path, gate, value))
        assert refused.value.code == "EVIDENCE_RECORD", gate
    contradictions = [
        ("report", "experiment", lambda r: r.update(outcome="parallel")),
        ("report", "experiment", lambda r: r.update(runs=[None])),
        ("report", "experiment", lambda r: r.update(runs=[{}])),
        ("report", "experiment", lambda r: r.update(runs=[{"workstreams": 1, "overlap_seconds": 0}])),
        ("report", "experiment", lambda r: r.update(runs=[{"workstreams": 2, "overlap_seconds": 30}])),
        ("observed", "docs", lambda r: r.update(argv=["gsd-run", "--version"])),
        ("guidance", "docs", lambda r: r["user_global"].update(after="c" * 64)),
        ("history", "authorization", lambda r: r.update(repository="/repo/other")),
    ]
    for name, gate, mutate in contradictions:
        changed = evidence_records(tmp_path)
        record = json.loads(Path(changed[name]["locator"]).read_text())
        mutate(record)
        changed[name] = _write(Path(changed[name]["locator"]), record)
        with pytest.raises(verifier.E):
            modes[gate](manifest(tmp_path, gate, obligation_values(evidence, verifier, changed)[gate]))
        records = evidence_records(tmp_path)
    docs = obligation_values(evidence, verifier, records)["docs"]
    docs["topics"].pop()
    with pytest.raises(verifier.E, match="topic"):
        verifier.docs_mode(manifest(tmp_path, "docs", docs))
    Path(records["topic-setup"]["locator"]).write_text("changed\n")
    with pytest.raises(verifier.E) as refused:
        verifier.docs_mode(manifest(tmp_path, "docs", obligation_values(evidence, verifier, records)["docs"]))
    assert refused.value.code == "DIGEST_MISMATCH"


def test_obligation_manifest_schemas_agree_with_runtime_refusals(tmp_path: Path) -> None:
    verifier = module()
    schema = json.loads((ROOT / "schemas/parallel-host-verification.schema.json").read_text())
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}\n")
    values = obligation_values(artifact(receipt), verifier, evidence_records(tmp_path))
    for gate, value in values.items():
        validator = Draft202012Validator({"$ref": f"#/$defs/{gate}_manifest", "$defs": schema["$defs"]})
        assert list(validator.iter_errors(manifest(tmp_path, gate, value))) == [], gate
    duplicated = json.loads(json.dumps(values["docs"]))
    duplicated["topics"] = [dict(duplicated["topics"][0]) for _ in duplicated["topics"]]
    relative = json.loads(json.dumps(values["authorization"]))
    relative["histories"][0]["repository"] = "feature-fix-swarm"
    undated = json.loads(json.dumps(values["experiment"]))
    undated["observed_utc"] = "yesterday"
    for gate, value in (("docs", duplicated), ("authorization", relative), ("experiment", undated)):
        validator = Draft202012Validator({"$ref": f"#/$defs/{gate}_manifest", "$defs": schema["$defs"]})
        assert list(validator.iter_errors(manifest(tmp_path, gate, value))), gate
        with pytest.raises(verifier.E):
            {"docs": verifier.docs_mode, "authorization": verifier.authorization_mode,
             "experiment": verifier.experiment_mode}[gate](manifest(tmp_path, gate, value))


def test_aggregate_revalidates_an_imported_review_completion(tmp_path: Path) -> None:
    verifier = module()
    value = manifest(tmp_path, "review", None)
    del value["review"]
    candidate = Path(value["candidate"]["locator"])
    review = tmp_path / "review.json"
    review.write_text(json.dumps({"verdict": "PASS", "findings": [],
                                  "reviewed_sha256": value["candidate"]["sha256"]}) + "\n")
    value.update({
        "platform": sys.platform, "command": ["review-adapter", str(candidate)],
        "started_utc": "2026-10-11T00:00:00Z", "completed_utc": "2026-10-11T00:01:00Z", "exit_status": 0,
        "ac_ids": HONEST_IDS["review-completion:upgraded"][0], "path_ids": HONEST_IDS["review-completion:upgraded"][1],
        "reviewer": {"host": "claude", "model": "opus", "session": "review-1"},
        "producer": {"host": "codex", "model": "terra", "session": "producer-1"},
        "artifact_sha256": value["candidate"]["sha256"],
        "artifacts": [value["candidate"], artifact(review)], "result": {"locator": str(review)},
        "severe_path_disposition": [],
    })
    manifest_path = tmp_path / "review-manifest.json"
    manifest_path.write_text(json.dumps(value) + "\n")
    result = verifier.do_review(value, "review-completion", False)
    result["verification_proof"] = {"manifest": artifact(manifest_path), "inputs": {}}
    assert verifier._aggregate_result(result, verifier.Budget())["gate"] == "review-completion:upgraded"
    blocked = dict(result, blocked_paths=["lib/run_state/"])
    with pytest.raises(verifier.E):
        verifier._aggregate_result(blocked, verifier.Budget())
    fresh = dict(result, review_adapter={"argv": ["review-adapter"]})
    with pytest.raises(verifier.E):
        verifier._aggregate_result(fresh, verifier.Budget())


def _write(path: Path, value: object) -> dict[str, str]:
    path.write_text(json.dumps(value) + "\n")
    return artifact(path)


CLOSURE = (json.dumps({"schema": "ffs.source-closure/v1", "files": [
    {"path": "lib/example.py", "type": "file", "mode": 0o644, "sha256": "a" * 64}]}) + "\n").encode()


def m0_comparison(tmp_path: Path, baseline_ids: tuple[list[str], list[str]] = BASELINE_IDS,
                  extra_sources: dict[str, bytes] | None = None):
    """A real attributed comparison over a complete M0 capture (test_m1_upgrade_evidence fixture)."""
    root = tmp_path / "m0"
    root.mkdir()
    return _changed_comparison_fixture(root, m0_candidate=CLOSURE, baseline_ids=baseline_ids,
                                       extra_sources=extra_sources)


def m0_capture(tmp_path: Path, baseline_ids: tuple[list[str], list[str]]) -> Path:
    return m0_comparison(tmp_path, baseline_ids)[2]


def test_aggregate_pass_is_reachable_with_each_gate_claiming_only_its_own_ids(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = module()
    candidate_sha = "d" * 64
    baseline = json.loads(m0_capture(tmp_path, BASELINE_IDS).read_text())
    results = []
    for gate in sorted(verifier.REQUIRED_AGGREGATE_GATES):
        result = aggregate_result(verifier, gate, candidate_sha)
        result["ac_ids"], result["path_ids"] = owned(verifier, gate)
        if gate == "upgrade-comparison":
            result["comparison_binding_sha256"] = "e" * 64
            result["verification_proof"] = {"manifest": {}, "inputs": {
                "baseline": _write(tmp_path / "baseline.json", baseline)}}
        results.append(_write(tmp_path / f"honest-{gate}.json", result))
    index = tmp_path / "index.json"
    _write(index, {"schema": "ffs.verification-index/v1", "candidate_sha256": candidate_sha, "results": results})
    # Per-gate revalidation is covered by the gate tests; this test covers the aggregate's id accounting.
    monkeypatch.setattr(verifier, "_aggregate_result", lambda value, budget: value)
    with pytest.raises(verifier.E) as unmet:
        verifier.aggregate_mode(index, True, "final")
    assert unmet.value.code == "AGGREGATE_COVERAGE" and missing_ids(unmet.value) == UNOWNED_IDS
    assert not set(BASELINE_IDS[0]) & missing_ids(unmet.value)

    # One gate cannot stand in for another: docs claiming every id does not cover the matrix ids.
    order = sorted(verifier.REQUIRED_AGGREGATE_GATES)
    for gate, ids in (("docs", ([f"AC-{n:03d}" for n in range(1, 61)], [f"PATH-{n:03d}" for n in range(1, 25)])),
                      ("matrix", ([], []))):
        path = tmp_path / f"honest-{gate}.json"
        changed = json.loads(path.read_text())
        changed["ac_ids"], changed["path_ids"] = ids
        results[order.index(gate)] = _write(path, changed)
    _write(index, {"schema": "ffs.verification-index/v1", "candidate_sha256": candidate_sha, "results": results})
    with pytest.raises(verifier.E) as refused:
        verifier.aggregate_mode(index, True, "final")
    assert refused.value.code == "AGGREGATE_COVERAGE"
    others = set().union(*(ids for gate, ids in verifier.GATE_OWNED_IDS.items() if gate != "matrix"))
    assert verifier.GATE_OWNED_IDS["matrix"] - others <= missing_ids(refused.value)
    for gate in ("docs", "matrix"):
        path = tmp_path / f"honest-{gate}.json"
        restored = json.loads(path.read_text())
        restored["ac_ids"], restored["path_ids"] = owned(verifier, gate)
        results[order.index(gate)] = _write(path, restored)

    # A diagnostic or source-unverified baseline proves nothing about AC-001 to AC-003.
    for key, bad in (("full_baseline_complete", False), ("canonical_source_verified", False),
                     ("full_baseline_unmet", ["coverage"]), ("baseline_profile", None),
                     ("full_inventory_evidence", {})):
        upgrade_path = tmp_path / "honest-upgrade-comparison.json"
        upgrade = json.loads(upgrade_path.read_text())
        upgrade["verification_proof"]["inputs"]["baseline"] = _write(
            tmp_path / f"baseline-{key}.json", dict(baseline, **{key: bad}))
        results[sorted(verifier.REQUIRED_AGGREGATE_GATES).index("upgrade-comparison")] = _write(upgrade_path, upgrade)
        _write(index, {"schema": "ffs.verification-index/v1", "candidate_sha256": candidate_sha, "results": results})
        with pytest.raises(verifier.E) as refused:
            verifier.aggregate_mode(index, True, "final")
        assert refused.value.code == "AGGREGATE_COVERAGE", key
        assert set(BASELINE_IDS[0]) <= missing_ids(refused.value), key


def test_retained_baseline_credit_needs_an_attributed_comparison_and_lends_only_baseline_ids(tmp_path: Path) -> None:
    verifier = module()
    every = ([f"AC-{n:03d}" for n in range(1, 61)], [f"PATH-{n:03d}" for n in range(1, 25)])
    proof = {"manifest": {}, "inputs": {"baseline": artifact(m0_capture(tmp_path, every))}}
    attributed = {"comparison_binding_sha256": "e" * 64, "verification_proof": proof}
    acs, paths, _ = verifier._credited_baseline(attributed, verifier.Budget())
    assert acs == ["AC-001", "AC-002", "AC-003", "AC-004"] and paths == ["PATH-001"]
    # The same-tuple compatibility path never re-checks the retained inventory, so it lends nothing.
    assert verifier._credited_baseline(dict(attributed, comparison_binding_sha256=None), verifier.Budget()) is None


def test_parallel_outcome_needs_measured_overlap(tmp_path: Path) -> None:
    verifier = module()
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}\n")
    records = evidence_records(tmp_path)
    report = json.loads(Path(records["report"]["locator"]).read_text())
    report.update(outcome="parallel", runs=[{"workstreams": 2, "overlap_seconds": 0},
                                             {"workstreams": 3, "overlap_seconds": 42.5}])
    records["report"] = _write(Path(records["report"]["locator"]), report)
    value = obligation_values(artifact(receipt), verifier, records)["experiment"]
    value["outcome"] = "parallel"
    assert verifier.experiment_mode(manifest(tmp_path, "experiment", value))["outcome"] == "parallel"


def test_retained_baseline_credit_reads_a_full_size_baseline(tmp_path: Path) -> None:
    verifier = module()
    baseline = json.loads(m0_capture(tmp_path, (["AC-001"], ["PATH-001"])).read_text())
    large = tmp_path / "large-baseline.json"
    # A real full baseline is about 2.9 MB, above the 2 MiB evidence file limit.
    large.write_text(json.dumps(baseline)[:-1] + " " * (3 * 1024 * 1024) + "}\n")
    attributed = {"comparison_binding_sha256": "e" * 64,
                  "verification_proof": {"manifest": {}, "inputs": {"baseline": artifact(large)}}}
    acs, paths, _ = verifier._credited_baseline(attributed, verifier.Budget())
    assert (acs, paths) == (["AC-001"], ["PATH-001"])


def test_gate_ownership_covers_every_ac_and_path_and_keeps_baseline_ids_apart() -> None:
    verifier = module()
    assert set(verifier.GATE_OWNED_IDS) == set(verifier.REQUIRED_AGGREGATE_GATES)
    gate_ids = set().union(*verifier.GATE_OWNED_IDS.values())
    every = {f"AC-{n:03d}" for n in range(1, 61)} | {f"PATH-{n:03d}" for n in range(1, 25)}
    assert every - (gate_ids | verifier.BASELINE_OWNED_IDS) == UNOWNED_IDS
    assert not gate_ids & verifier.BASELINE_OWNED_IDS
    # The real coverage manifests (e8-evidence make-coverage-manifest.py) claim AC-039 and PATH-018.
    assert {"AC-039", "PATH-018"} <= verifier.GATE_OWNED_IDS["coverage"]


def test_baseline_credit_revalidates_the_retained_m0_capture(tmp_path: Path) -> None:
    _, _, baseline_path, _ = m0_comparison(tmp_path)
    verifier = module()

    def credit(path: Path):
        proof = {"manifest": {}, "inputs": {"baseline": artifact(path)}}
        return verifier._credited_baseline({"comparison_binding_sha256": "e" * 64, "verification_proof": proof},
                                           verifier.Budget())

    acs, paths, _ = credit(baseline_path)
    assert (acs, paths) == (BASELINE_IDS[0], BASELINE_IDS[1])
    original = json.loads(baseline_path.read_text())
    forged = json.loads(json.dumps(original))
    del forged["full_inventory_evidence"]["backups_recovery"]
    del forged["full_inventory_evidence"]["source_runtime"][0]["validated"]["canonical_source"]
    proofless = dict(original)
    del proofless["verification_proof"]
    for name, value in (("forged", forged), ("proofless", proofless)):
        path = tmp_path / f"baseline-{name}.json"
        _write(path, value)
        with pytest.raises(verifier.E) as refused:
            credit(path)
        assert refused.value.code == "VERIFICATION_PROOF", name


def test_baseline_credit_survives_an_upgrade_commit_but_not_a_tampered_retained_inventory(tmp_path: Path) -> None:
    _, _, baseline_path, _ = m0_comparison(tmp_path)
    verifier = module()
    retained = json.loads(baseline_path.read_text())
    repository = Path(retained["repository"]["root"])
    # The fixture committed an upgrade after the capture: live HEAD is no longer the M0 generation.
    assert verifier._git(repository, "rev-parse", "HEAD") != retained["repository"]["head"]

    def credit(path: Path):
        proof = {"manifest": {}, "inputs": {"baseline": artifact(path)}}
        return verifier._credited_baseline({"comparison_binding_sha256": "e" * 64, "verification_proof": proof},
                                           verifier.Budget())

    assert credit(baseline_path)[:2] == (BASELINE_IDS[0], BASELINE_IDS[1])
    moved = json.loads(json.dumps(retained))
    moved["repository"]["head"] = verifier._git(repository, "rev-parse", "HEAD")
    _write(tmp_path / "baseline-moved.json", moved)
    with pytest.raises(verifier.E):
        credit(tmp_path / "baseline-moved.json")
    surface = sorted(verifier.M0_BACKUP_SURFACES)[0]
    (tmp_path / "m0" / "preserved" / surface).write_bytes(b"tampered after capture\n")
    with pytest.raises(verifier.E):
        credit(baseline_path)


def test_docs_bind_topic_records_exit_status_stdout_and_guidance_files(tmp_path: Path) -> None:
    verifier = module()
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}\n")
    empty = artifact(receipt)

    def docs(records: dict[str, dict[str, str]]) -> dict[str, object]:
        return obligation_values(empty, verifier, records)["docs"]

    def refused(value: dict[str, object]) -> str:
        with pytest.raises(verifier.E) as error:
            verifier.docs_mode(manifest(tmp_path, "docs", value))
        return error.value.code

    def rewrite(records: dict[str, dict[str, str]], name: str, mutate) -> dict[str, dict[str, str]]:
        record = json.loads(Path(records[name]["locator"]).read_text())
        mutate(record)
        return dict(records, **{name: _write(Path(records[name]["locator"]), record)})

    records = evidence_records(tmp_path)
    value = docs(records)
    value["topics"][0]["artifact"] = empty
    assert refused(value) == "EVIDENCE_RECORD"
    value = docs(records)
    value["shared_dependencies"] = empty
    assert refused(value) == "EVIDENCE_RECORD"
    value = docs(rewrite(evidence_records(tmp_path), "topic-setup", lambda r: r.update(topic="tiers")))
    assert refused(value) == "DOCS_TOPIC"
    value = docs(rewrite(evidence_records(tmp_path), "topic-setup", lambda r: r.update(documents=[])))
    assert refused(value) == "DOCS_TOPIC"
    value = docs(evidence_records(tmp_path))
    value["examples"][0]["expected_exit_status"] = 1
    assert refused(value) == "DOCS_EXAMPLE"
    value = docs(rewrite(evidence_records(tmp_path), "observed", lambda r: r.update(stdout_sha256="0" * 64)))
    assert refused(value) == "DOCS_EXAMPLE"
    value = docs(rewrite(evidence_records(tmp_path), "guidance", lambda r: r.pop("repo_guidance")))
    assert refused(value) == "DOCS_GUIDANCE"
    value = docs(rewrite(evidence_records(tmp_path), "guidance",
                         lambda r: r.update(repo_guidance=r["repo_guidance"][:1])))
    assert refused(value) == "DOCS_GUIDANCE"
    value = docs(rewrite(evidence_records(tmp_path), "shared", lambda r: r.update(isolation_limit="")))
    assert refused(value) == "DOCS_DEPENDENCIES"
    # A schema-valid argv may repeat an option.
    argv = ["gsd-run", "--arg", "x", "--arg", "y"]
    value = docs(rewrite(evidence_records(tmp_path), "observed", lambda r: r.update(argv=argv)))
    value["examples"][0]["argv"] = argv
    assert verifier.docs_mode(manifest(tmp_path, "docs", value))["status"] == "PASS"


def test_authorization_refuses_unauthorized_history_rows_and_shared_index_records(tmp_path: Path) -> None:
    verifier = module()
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}\n")

    def authorization(records: dict[str, dict[str, str]]) -> dict[str, object]:
        return obligation_values(artifact(receipt), verifier, records)["authorization"]

    def refused(value: dict[str, object]) -> str:
        with pytest.raises(verifier.E) as error:
            verifier.authorization_mode(manifest(tmp_path, "authorization", value))
        return error.value.code

    def history(mutate) -> dict[str, object]:
        records = evidence_records(tmp_path)
        record = json.loads(Path(records["history"]["locator"]).read_text())
        mutate(record)
        records["history"] = _write(Path(records["history"]["locator"]), record)
        return authorization(records)

    for mutate in (lambda r: r["pushes"].append({"ref": "origin/main", "authorization": "auth-1"}),
                   lambda r: r["commits"].append({"commit": "d" * 40, "authorization": "auth-1"}),
                   lambda r: r["commits"].append({"commit": "d" * 40, "fixture": True}),
                   lambda r: r["commits"].append({"commit": "d" * 40}),
                   lambda r: r["pushes"].append({"ref": "origin/main", "authorization": "auth-unknown"}),
                   lambda r: r["pushes"].append({"ref": "origin/main", "fixture": False}),
                   lambda r: r["commits"].append({"commit": "d" * 40, "fixture": True, "authorization": "auth-1"})):
        assert refused(history(mutate)) == "AUTHORIZATION_UNAUTHORIZED"
    records = evidence_records(tmp_path)
    value = authorization(records)
    value["record"] = artifact(receipt)
    assert refused(value) == "EVIDENCE_RECORD"
    value = authorization(records)
    value["evidence_index"] = {category: records["index-results"] for category in ("results", "unmet", "hypotheses")}
    assert refused(value) == "AUTHORIZATION_INDEX"
    value = authorization(records)
    value["evidence_index"]["unmet"], value["evidence_index"]["hypotheses"] = (
        records["index-hypotheses"], records["index-unmet"])
    assert refused(value) == "AUTHORIZATION_INDEX"


def test_experiment_binds_issue_status_and_supported_path_implications(tmp_path: Path) -> None:
    verifier = module()
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}\n")

    def refused(value: dict[str, object]) -> str:
        with pytest.raises(verifier.E) as error:
            verifier.experiment_mode(manifest(tmp_path, "experiment", value))
        return error.value.code

    records = evidence_records(tmp_path)
    value = obligation_values(artifact(receipt), verifier, records)["experiment"]
    value["issue_status"] = "closed, fixed upstream"
    assert refused(value) == "EXPERIMENT"
    value = obligation_values(artifact(receipt), verifier, records)["experiment"]
    value["supported_paths"] = artifact(receipt)
    assert refused(value) == "EVIDENCE_RECORD"
    for mutate in (lambda r: r.update(execution_paths=[]), lambda r: r.update(path_implications=[]),
                   lambda r: r.update(path_implications=[{"path_id": "PATH-99", "implication": "x"}])):
        changed = evidence_records(tmp_path)
        record = json.loads(Path(changed["supported"]["locator"]).read_text())
        mutate(record)
        changed["supported"] = _write(Path(changed["supported"]["locator"]), record)
        assert refused(obligation_values(artifact(receipt), verifier, changed)["experiment"]) == "EXPERIMENT"


def test_obligation_record_schemas_agree_with_the_runtime_records(tmp_path: Path) -> None:
    schema = json.loads((ROOT / "schemas/parallel-host-verification.schema.json").read_text())
    names = {
        "ffs.upstream-experiment/v1": "upstream_experiment_record", "ffs.supported-paths/v1": "supported_paths_record",
        "ffs.command-observation/v1": "command_observation_record", "ffs.doc-topic/v1": "doc_topic_record",
        "ffs.shared-dependencies/v1": "shared_dependencies_record",
        "ffs.guidance-comparison/v1": "guidance_comparison_record",
        "ffs.authorization-record/v1": "authorization_record", "ffs.repository-history/v1": "repository_history_record",
        "ffs.evidence-index/v1": "evidence_index_record",
    }
    for descriptor in evidence_records(tmp_path).values():
        record = json.loads(Path(descriptor["locator"]).read_text())
        validator = Draft202012Validator({"$ref": f"#/$defs/{names[record['schema']]}", "$defs": schema["$defs"]})
        assert list(validator.iter_errors(record)) == [], record["schema"]
        assert list(validator.iter_errors({"schema": record["schema"]})), record["schema"]


def _review_completion_manifest(tmp_path: Path, candidate: dict[str, str]) -> dict[str, object]:
    value = manifest(tmp_path, "review", None)
    del value["review"]
    review = tmp_path / "review.json"
    review.write_text(json.dumps({"verdict": "PASS", "findings": [], "reviewed_sha256": candidate["sha256"]}) + "\n")
    value.update({
        "candidate": candidate, "platform": sys.platform, "command": ["review-adapter", candidate["locator"]],
        "started_utc": "2026-10-11T00:00:00Z", "completed_utc": "2026-10-11T00:01:00Z", "exit_status": 0,
        "reviewer": {"host": "claude", "model": "opus", "session": "review-1"},
        "producer": {"host": "codex", "model": "terra", "session": "producer-1"},
        "artifact_sha256": candidate["sha256"],
        "artifacts": [candidate, artifact(review)], "result": {"locator": str(review)},
        "severe_path_disposition": [],
    })
    return value


def real_gate_index(tmp_path: Path) -> tuple[object, Path]:
    """Every required gate result produced by its real evaluator over one candidate and provenance."""
    verifier, upgrade, baseline_path, _ = m0_comparison(tmp_path)
    candidate, provenance = upgrade["candidate"], upgrade["provenance"]
    receipt, transcript = tmp_path / "receipt.json", tmp_path / "review-transcript.jsonl"
    receipt.write_text("{}\n")
    transcript.write_text('{"type":"result"}\n')
    evidence = artifact(receipt)
    report = tmp_path / "coverage.xml"
    report.write_text('<coverage lines-covered="1" lines-valid="1" branches-covered="0" branches-valid="0">'
                      '<packages><package><classes><class filename="lib/example.py"><lines>'
                      '<line number="1" hits="1"/></lines></class></classes></package></packages></coverage>')
    execution = _write(tmp_path / "coverage-execution.json", {
        "schema": "ffs.coverage-execution/v1", "source_sha256": candidate["sha256"],
        "xml_sha256": artifact(report)["sha256"], "argv": ["pytest", "--cov"], "exit_status": 0,
        "started_utc": "2026-10-11T00:00:00Z", "completed_utc": "2026-10-11T00:01:00Z"})
    source, staged = tmp_path / "source-surface", tmp_path / "staged-surface"
    source.write_bytes(b"canonical\n")
    staged.write_bytes(source.read_bytes())
    domains = sorted(verifier.AUDIT_REQUIRED_DOMAINS)
    obligations = obligation_values(evidence, verifier, evidence_records(tmp_path))
    sections = {
        "audit": ("audit", {"required_domains": domains, "findings": [], "review_pin": evidence,
                            "domains": [{"id": d, "status": "PASS", "artifact": evidence} for d in domains],
                            "producer": {"host": "codex", "model": "terra", "session": "producer"},
                            "reviewer": {"host": "claude", "model": "opus", "session": "reviewer"}}),
        "coverage": ("coverage", {"xml": artifact(report), "execution": execution}),
        "hosts": ("hosts", hosts_value(receipt, review_rows(transcript))),
        "installation-lifecycle": ("installation", {"protected_unchanged": True, "lifecycle_rows": [
            {"operation": op, "scope": scope, "platform": platform, "status": "PASS", "artifact": evidence}
            for op in ("fresh-install", "upgrade", "collision", "interruption", "rollback", "uninstall")
            for scope in ("project", "user") for platform in ("darwin", "linux")]}),
        "matrix": ("matrix", {"required_case_ids": ["reservation-race"], "rows": [
            {"case_id": "reservation-race", "platform": platform, "repetitions": 25, "status": "PASS",
             "artifact": evidence} for platform in ("darwin", "linux")]}),
        "migration": ("migration", {
            "sources": [evidence], "journal": evidence,
            "record_counts": {"source": 1, "imported": 1, "quarantined": 0},
            "id_bindings": [{"source_id": "legacy-db", "source_run_id": "old-run",
                             "canonical_run_id": "new-run", "evidence": evidence}],
            "epochs": [{"run_id": "new-run", "epoch": 1, "writer": "new", "proof": evidence}],
            "owner_fence": evidence, "interlock": evidence,
            "restart": {"status": "PASS", "artifact": evidence}, "rollback": {"status": "PASS", "artifact": evidence},
            "legacy_unchanged": True}),
        "rollout": ("rollout", {
            "consumer": str(tmp_path), "forks": [], "protected_unchanged": True, "rollback": evidence,
            "surfaces": [{"path": "skills/fix/SKILL.md", "owner": "ffs", "source": artifact(source),
                          "staged": artifact(staged), "status": "PASS"}],
            "canaries": [{"platform": platform, "round": round_, "status": "PASS",
                          "productive_overlap_seconds": 600, "artifact": evidence}
                         for platform in ("darwin", "linux") for round_ in (1, 2)]}),
        **{gate: (gate, obligations[gate]) for gate in ("experiment", "docs", "authorization")},
    }
    evaluators = {
        "audit": verifier.audit_mode, "coverage": lambda m: verifier.coverage_mode(m, 80),
        "hosts": lambda m: _hosts(verifier, m), "installation-lifecycle": lambda m: verifier.installation_mode(m, "lifecycle"),
        "matrix": lambda m: verifier.matrix_mode(m, "hermetic", 25),
        "migration": lambda m: verifier.migration_mode(m, "verify-legacy"),
        "rollout": lambda m: verifier.rollout_mode(m, tmp_path), "experiment": verifier.experiment_mode,
        "docs": verifier.docs_mode, "authorization": verifier.authorization_mode,
    }
    manifests: dict[str, dict[str, object]] = {}
    for gate, (key, value) in sections.items():
        manifests[gate] = dict(manifest(tmp_path, key, value), candidate=candidate, provenance=provenance)
    manifests["review-completion:upgraded"] = dict(_review_completion_manifest(tmp_path, candidate),
                                                   provenance=provenance)
    manifests["upgrade-comparison"] = upgrade
    results = []
    for gate in sorted(verifier.REQUIRED_AGGREGATE_GATES):
        value = manifests[gate]
        value["ac_ids"], value["path_ids"] = owned(verifier, gate)
        name = gate.replace(":", "-")
        proof = tmp_path / f"real-manifest-{name}.json"
        proof.write_text(json.dumps(value) + "\n")
        inputs: dict[str, dict[str, str]] = {}
        if gate == "upgrade-comparison":
            output = verifier.upgrade_mode(value, baseline_path)
            inputs["baseline"] = artifact(baseline_path)
        elif gate == "review-completion:upgraded":
            output = verifier.do_review(value, "review-completion", False)
        else:
            output = evaluators[gate](value)
        output["verification_proof"] = {"manifest": artifact(proof), "inputs": inputs}
        results.append(_write(tmp_path / f"real-result-{name}.json", output))
    index = tmp_path / "real-index.json"
    _write(index, {"schema": "ffs.verification-index/v1", "candidate_sha256": candidate["sha256"], "results": results})
    return verifier, index


def test_aggregate_over_real_gate_results_reports_exactly_the_unowned_upgrade_obligations(tmp_path: Path) -> None:
    verifier, index = real_gate_index(tmp_path)
    with pytest.raises(verifier.E) as unmet:
        verifier.aggregate_mode(index, True, "final")
    # Every other AC and every PATH is credited by its owning gate or the revalidated M0 baseline.
    assert unmet.value.code == "AGGREGATE_COVERAGE"
    assert missing_ids(unmet.value) == UNOWNED_IDS


def test_fixture_history_rows_need_a_declared_fixture_root_outside_the_canonical_repository(tmp_path: Path) -> None:
    verifier = module()
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}\n")
    for mutate in (lambda r: r.pop("fixture_root"), lambda r: r.update(fixture_root="/elsewhere"),
                   lambda r: r.update(fixture_root="/fixtures/run-1/repo")):
        records = evidence_records(tmp_path)
        record = json.loads(Path(records["fixture-history"]["locator"]).read_text())
        mutate(record)
        records["fixture-history"] = _write(Path(records["fixture-history"]["locator"]), record)
        value = obligation_values(artifact(receipt), verifier, records)["authorization"]
        with pytest.raises(verifier.E) as refused:
            verifier.authorization_mode(manifest(tmp_path, "authorization", value))
        assert refused.value.code == "AUTHORIZATION_UNAUTHORIZED"
    records = evidence_records(tmp_path)
    record = json.loads(Path(records["authorization"]["locator"]).read_text())
    record["canonical_repository"] = "/fixtures/run-1"
    records["authorization"] = _write(Path(records["authorization"]["locator"]), record)
    value = obligation_values(artifact(receipt), verifier, records)["authorization"]
    with pytest.raises(verifier.E) as refused:
        verifier.authorization_mode(manifest(tmp_path, "authorization", value))
    assert refused.value.code == "AUTHORIZATION_UNAUTHORIZED"


def test_experiment_paths_are_absolute_and_implications_name_spec_paths(tmp_path: Path) -> None:
    verifier = module()
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}\n")
    for mutate in (lambda r: r.update(execution_paths=["false"]),
                   lambda r: r.update(path_implications=[{"path_id": "PATH-999", "implication": "x"}]),
                   lambda r: r.update(path_implications=[{"path_id": "PATH-000", "implication": "x"}])):
        records = evidence_records(tmp_path)
        record = json.loads(Path(records["supported"]["locator"]).read_text())
        mutate(record)
        records["supported"] = _write(Path(records["supported"]["locator"]), record)
        with pytest.raises(verifier.E) as refused:
            verifier.experiment_mode(manifest(tmp_path, "experiment",
                                              obligation_values(artifact(receipt), verifier, records)["experiment"]))
        assert refused.value.code == "EXPERIMENT"


def test_docs_documents_and_guidance_files_are_byte_bound_artifacts(tmp_path: Path) -> None:
    verifier = module()
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}\n")

    def refused(records: dict[str, dict[str, str]]) -> str:
        value = obligation_values(artifact(receipt), verifier, records)["docs"]
        with pytest.raises(verifier.E) as error:
            verifier.docs_mode(manifest(tmp_path, "docs", value))
        return error.value.code

    records = evidence_records(tmp_path)
    record = json.loads(Path(records["topic-setup"]["locator"]).read_text())
    record["documents"] = ["README.md"]
    records["topic-setup"] = _write(Path(records["topic-setup"]["locator"]), record)
    assert refused(records) == "DOCS_TOPIC"
    records = evidence_records(tmp_path)
    (tmp_path / "README.md").write_text("edited after the record\n")
    assert refused(records) == "DIGEST_MISMATCH"
    records = evidence_records(tmp_path)
    record = json.loads(Path(records["guidance"]["locator"]).read_text())
    record["repo_guidance"][0]["artifact"] = artifact(receipt)
    records["guidance"] = _write(Path(records["guidance"]["locator"]), record)
    assert refused(records) == "DOCS_GUIDANCE"


def test_retained_repository_record_is_checked_against_the_canonical_object_store(tmp_path: Path) -> None:
    _, _, baseline_path, _ = m0_comparison(tmp_path)
    verifier = module()
    retained = json.loads(baseline_path.read_text())

    def credit(name: str, mutate) -> None:
        forged = json.loads(json.dumps(retained))
        mutate(forged["repository"])
        path = tmp_path / f"baseline-{name}.json"
        _write(path, forged)
        proof = {"manifest": {}, "inputs": {"baseline": artifact(path)}}
        return verifier._credited_baseline({"comparison_binding_sha256": "e" * 64, "verification_proof": proof},
                                           verifier.Budget())

    # The honest capture keeps credit after the post-capture upgrade commit.
    assert credit("honest", lambda r: None)[:2] == (BASELINE_IDS[0], BASELINE_IDS[1])
    forgeries = {
        "origin": lambda r: r.update(origin_main="0123456789abcdef0123456789abcdef01234567"),
        "inventory": lambda r: r.update(source_inventory=r["source_inventory"][:1]),
        "common-dir": lambda r: r.update(common_dir=str(tmp_path / "no-such-common-dir")),
    }
    assert len(retained["repository"]["source_inventory"]) > 1
    for name, mutate in forgeries.items():
        with pytest.raises(verifier.E):
            credit(name, mutate)


def test_fixture_containment_uses_normalized_physical_paths(tmp_path: Path) -> None:
    verifier = module()
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}\n")

    def refused(canonical: str, repository: str, root: str) -> None:
        records = evidence_records(tmp_path)
        grants = json.loads(Path(records["authorization"]["locator"]).read_text())
        grants["canonical_repository"] = canonical
        records["authorization"] = _write(Path(records["authorization"]["locator"]), grants)
        history = json.loads(Path(records["fixture-history"]["locator"]).read_text())
        history.update(repository=repository, fixture_root=root)
        records["fixture-history"] = _write(Path(records["fixture-history"]["locator"]), history)
        value = obligation_values(artifact(receipt), verifier, records)["authorization"]
        value["histories"][1]["repository"] = repository
        with pytest.raises(verifier.E) as error:
            verifier.authorization_mode(manifest(tmp_path, "authorization", value))
        assert error.value.code in {"AUTHORIZATION_UNAUTHORIZED", "AUTHORIZATION_HISTORY", "AUTHORIZATION_RECORD"}

    refused("/repo/feature-fix-swarm", "/fixtures/../repo/feature-fix-swarm", "/fixtures")
    refused("/repo/feature-fix-swarm", "/fixtures/./run-1/repo", "/fixtures")
    refused("/repo/./feature-fix-swarm", "/fixtures/run-1/repo", "/fixtures")
    canonical, fixtures = tmp_path / "canonical", tmp_path / "fixtures"
    canonical.mkdir()
    fixtures.mkdir()
    (fixtures / "repo").symlink_to(canonical)
    refused(str(canonical), str(fixtures / "repo"), str(fixtures))


@pytest.mark.parametrize("corpus", ["many-small-files", "one-large-file"])
def test_retained_source_inventory_uses_the_capture_limits(tmp_path: Path, corpus: str) -> None:
    verifier = module()
    if corpus == "many-small-files":
        extra = {f"skills/bulk/leaf-{index:03d}.txt": f"leaf {index}\n".encode() for index in range(289)}
    else:
        extra = {"skills/large/blob.bin": b"x" * (verifier.MAX_INPUT_BYTES + 1)}
    _, _, baseline_path, _ = m0_comparison(tmp_path, extra_sources=extra)
    retained = json.loads(baseline_path.read_text())
    assert set(extra) <= {row["path"] for row in retained["repository"]["source_inventory"]}
    proof = {"manifest": {}, "inputs": {"baseline": artifact(baseline_path)}}
    credited = verifier._credited_baseline({"comparison_binding_sha256": "e" * 64, "verification_proof": proof},
                                           verifier.Budget())
    assert credited[:2] == (BASELINE_IDS[0], BASELINE_IDS[1])
