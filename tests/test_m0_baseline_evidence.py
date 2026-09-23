"""Independent M0 original-baseline and joined-evidence acceptance."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts/verification/parallel_host_parity.py"
FULL_KEYS = {"ci", "python", "bats", "backups_recovery", "source_runtime",
             "tools_customizations", "coverage", "environment"}

HISTORICAL_METADATA = {
    "macos-original-python": {
        "argv": None, "exit_status": 0, "started_utc": None, "completed_utc": None,
    },
    "macos-original-bats-incomplete": {
        "argv": None, "exit_status": None, "started_utc": None, "completed_utc": None,
    },
    "ubuntu-original-python": {
        "argv": None, "exit_status": 1, "started_utc": None, "completed_utc": None,
    },
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


def artifact(path: Path) -> dict[str, str]:
    return {"locator": str(path), "sha256": sha(path)}


def suite(tmp_path: Path, *, reverse_time: bool = False) -> tuple[dict[str, object], Path]:
    script = tmp_path / "observed-suite.py"
    script.write_text(
        "import json\n"
        "tests={'retained::one':'FAIL','retained::two':'FAIL','healthy::one':'PASS'}\n"
        "print(json.dumps({'tests': tests}))\nraise SystemExit(1)\n"
    )
    started = utc()
    run = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                         check=False, env=clean_env(), timeout=10)
    completed = utc()
    if reverse_time:
        started, completed = "2026-09-12T00:00:01Z", "2026-09-12T00:00:00Z"
    observed = json.loads(run.stdout)["tests"]
    record = write_json(tmp_path / "suite-result.json", {
        "argv": [sys.executable, str(script)], "exit_status": run.returncode,
        "stdout": run.stdout, "stderr": run.stderr, "started_utc": started,
        "completed_utc": completed, "tests": observed,
    })
    return ({"id": "original-python", **artifact(record), "argv": [sys.executable, str(script)],
             "exit_status": run.returncode, "started_utc": started, "completed_utc": completed}, record)


def manifest(tmp_path: Path, mutation: str = "valid") -> tuple[Path, Path]:
    candidate = tmp_path / "original-source"; candidate.write_bytes(b"original source bytes\n")
    observation, record = suite(tmp_path, reverse_time=mutation == "chronology")
    if mutation == "forged-digest":
        observation["sha256"] = "0" * 64
    digest = sha(candidate)
    value = {
        "schema": "ffs.parallel-host-verification/v1",
        "binding": {"run": "m0-baseline-fixture", "activity": "baseline", "attempt": "1"},
        "label": "hermetic", "candidate": artifact(candidate),
        "provenance": {key: digest for key in ("source_sha256", "binary_sha256", "bundle_sha256", "config_sha256")},
        "ac_ids": ["AC-001"], "path_ids": ["PATH-001"], "int_ids": ["INT-001"],
        "baseline": {"suite_artifacts": [observation]},
    }
    return write_json(tmp_path / f"manifest-{mutation}.json", value), record


def invoke(tmp_path: Path, source: Path) -> tuple[subprocess.CompletedProcess[str], dict]:
    output = tmp_path / "output" / "baseline.json"
    run = subprocess.run([sys.executable, str(VERIFIER), "baseline", "--manifest", str(source),
                          "--output", str(output)], capture_output=True, text=True, check=False,
                         env=clean_env(), timeout=20)
    return run, json.loads(run.stdout)


def test_fixture_actual_failed_suite_is_captured_without_green_authority(tmp_path: Path) -> None:
    source, record = manifest(tmp_path)
    before = sha(record)
    run, payload = invoke(tmp_path, source)
    assert run.returncode == 0 and payload["status"] == "PASS"
    assert payload["suite_passed"] is False
    assert {name for name, status in payload["tests"].items() if status == "FAIL"} == {
        "retained::one", "retained::two"}
    assert payload["full_baseline_complete"] is False
    assert set(payload["full_baseline_unmet"]) == FULL_KEYS
    assert set(payload["gate_vector"].values()) == {False}
    assert sha(record) == before


def test_fixture_forged_raw_suite_digest_is_rejected(tmp_path: Path) -> None:
    source, record = manifest(tmp_path, "forged-digest")
    before = sha(record)
    run, payload = invoke(tmp_path, source)
    assert run.returncode != 0 and payload["status"] != "PASS"
    assert sha(record) == before


def test_fixture_reversed_suite_chronology_is_rejected(tmp_path: Path) -> None:
    source, record = manifest(tmp_path, "chronology")
    before = sha(record)
    run, payload = invoke(tmp_path, source)
    assert run.returncode != 0 and payload["status"] != "PASS"
    assert sha(record) == before


def env_report(name: str) -> tuple[Path, dict]:
    raw = os.environ.get(name)
    # The evidence gate opts in; ordinary CI has no operator reports and skips.
    if not raw and os.environ.get("FFS_REQUIRE_ACTUAL_EVIDENCE") == "1":
        pytest.fail(f"{name} is required when FFS_REQUIRE_ACTUAL_EVIDENCE=1")
    if not raw:
        pytest.skip(f"actual evidence not selected: set {name}")
    path = Path(raw)
    assert path.is_absolute() and path.is_file(), f"missing actual evidence: {path}"
    return path, json.loads(path.read_text())


def assert_historical_metadata_is_preserved(
    rows: dict[str, dict], limitations: list[str],
) -> None:
    """Require retained values and explicit gaps, including contradictory values."""
    report_gaps = " ".join(limitations).lower()
    for name, expected in HISTORICAL_METADATA.items():
        row = rows[name]
        for field, value in expected.items():
            assert row[field] == value, f"{name}.{field} must preserve the retained value"
        gaps = " ".join(row["metadata_gaps"]).lower()
        assert "argv" in gaps or "argv" in report_gaps
        assert "utc" in gaps or "utc" in report_gaps
    macos_python_gaps = " ".join(rows["macos-original-python"]["metadata_gaps"]).lower()
    assert "exit" in macos_python_gaps and "conflict" in macos_python_gaps
    assert "5" in macos_python_gaps and "0" in macos_python_gaps
    macos_bats_gaps = " ".join(rows["macos-original-bats-incomplete"]["metadata_gaps"]).lower()
    assert "exit" in macos_bats_gaps and "incomplete" in macos_bats_gaps


@pytest.mark.xfail(
    reason="terminal RETAIN_UNMET historical input disposition (2026-09-17)",
    raises=AssertionError,
    strict=True,
)
def test_actual_measurements_preserve_original_failures_and_completed_bats_arithmetic() -> None:
    _, report = env_report("FFS_M0_MEASUREMENTS")
    assert report["schema"] == "ffs.m0-measurements/v1"
    rows = {row["id"]: row for row in report["observations"]}
    expected = {
        "macos-original-python": (1335, 1330, 5),
        "macos-original-bats-incomplete": (991, 990, 1),
        "ubuntu-original-python": (1335, 1334, 1),
    }
    for name, counts in expected.items():
        row = rows[name]
        assert row["kind"] in {"historical", "diagnostic"}
        assert tuple(row["counts"][key] for key in ("executed", "passed", "failed")) == counts
        assert len(row["failures"]) == counts[2]
        raw = Path(row["artifact"]["locator"])
        assert raw.is_file() and not raw.is_symlink() and sha(raw) == row["artifact"]["sha256"]
    assert_historical_metadata_is_preserved(rows, report["limitations"])
    aggregate = report["bats_aggregate"]
    assert aggregate["component_ids"] == ["ubuntu-prepared-test-tree", "ubuntu-browser-proof", "ubuntu-qa-swarm-proof"]
    components = [rows[name]["counts"] for name in aggregate["component_ids"]]
    assert [(x["executed"], x["passed"], x["failed"]) for x in components] == [
        (1598, 1597, 1), (17, 17, 0), (13, 13, 0)]
    assert (aggregate["executed"], aggregate["passed"], aggregate["failed"]) == (1628, 1627, 1)
    assert aggregate["complete"] is True
    assert any("incomplete" in item.lower() for item in report["limitations"])
    assert report["complete"] is False
    assert report["unmet"]
    assert any("original" in item.lower() and "unavailable" in item.lower()
               for item in report["unmet"])


def test_actual_measurements_reject_fabricated_historical_metadata() -> None:
    _, report = env_report("FFS_M0_MEASUREMENTS")
    rows = {row["id"]: row for row in report["observations"]}
    fabrications = (
        ("macos-original-python", "argv", ["python3", "-m", "pytest"]),
        ("macos-original-python", "exit_status", 1),
        ("macos-original-python", "started_utc", "2026-09-12T00:00:00Z"),
        ("macos-original-bats-incomplete", "completed_utc", "2026-09-12T00:00:01Z"),
        ("ubuntu-original-python", "argv", ["python3", "-m", "pytest", "lib/", "tests/"]),
    )
    for row_id, field, fabricated in fabrications:
        mutated = deepcopy(rows)
        mutated[row_id][field] = fabricated
        with pytest.raises(AssertionError):
            assert_historical_metadata_is_preserved(mutated, report["limitations"])


@pytest.mark.xfail(
    reason="terminal RETAIN_UNMET historical input disposition (2026-09-17)",
    raises=AssertionError,
    strict=True,
)
def test_actual_measurements_coverage_uses_complete_corpus_and_separate_counters() -> None:
    _, report = env_report("FFS_M0_MEASUREMENTS")
    coverage = report["coverage"]
    xml_path = Path(coverage["xml_artifact"]["locator"])
    assert xml_path.is_file() and not xml_path.is_symlink() and sha(xml_path) == coverage["xml_artifact"]["sha256"]
    root = ET.fromstring(xml_path.read_bytes())
    assert int(root.attrib["lines-covered"]) == coverage["line_hits"]
    assert int(root.attrib["lines-valid"]) == coverage["line_total"] > 0
    assert int(root.attrib["branches-covered"]) == coverage["branch_covered"]
    assert int(root.attrib["branches-valid"]) == coverage["branch_total"]
    assert coverage["production_inventory"] and len(coverage["production_inventory"]) == len(set(coverage["production_inventory"]))
    assert coverage["line_percent"] == 100 * coverage["line_hits"] / coverage["line_total"]
    assert coverage["gap_to_80"] == max(0, 80 - coverage["line_percent"])
    if coverage["full_suite_coverage"] is False:
        assert coverage["classification"] == "diagnostic"


@pytest.mark.xfail(
    reason="terminal RETAIN_UNMET historical input disposition (2026-09-17)",
    raises=AssertionError,
    strict=True,
)
def test_actual_evidence_joined_baseline_is_complete_without_hiding_failures() -> None:
    baseline_path, baseline = env_report("FFS_M0_BASELINE")
    manifest_path, source = env_report("FFS_M0_MANIFEST")
    assert baseline["schema"] == "ffs.parallel-host-verification/v1"
    assert baseline["gate"] == "baseline-capture" and baseline["status"] == "PASS"
    assert baseline["baseline_profile"] == "m0/v1"
    assert baseline["canonical_source_verified"] is True
    # AD-016 seals eight historical gaps.  A joined capture can be valid and
    # retain all observed failures while historical completeness remains false.
    assert baseline["full_baseline_complete"] is False
    assert baseline["full_baseline_unmet"]
    assert baseline["suite_passed"] is False
    assert {key for key, value in baseline["tests"].items() if value == "FAIL"}
    assert set(baseline["full_inventory_evidence"]) == FULL_KEYS
    assert set(baseline["gate_vector"].values()) == {False}
    assert source["baseline_profile"] == "m0/v1"
    assert source["binding"] == baseline["binding"] and source["provenance"] == baseline["provenance"]
    assert source["candidate"] == baseline["candidate"]
    reference = baseline.get("observed_upgraded_reference")
    assert isinstance(reference, dict) and reference.get("label") == "observed-upgraded-reference"
    reference_path = Path(reference["locator"])
    assert reference_path.is_absolute() and reference_path.is_file()
    assert sha(reference_path) == reference["sha256"]
    ci = baseline["full_inventory_evidence"]["ci"][0]["validated"]
    assert ci["head"] == ci["origin_main"] == baseline["repository"]["origin_main"]
    assert baseline_path.is_file() and manifest_path.is_file()
