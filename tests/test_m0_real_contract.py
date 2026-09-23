"""Frozen acceptance for contract shapes observed during the actual M0 source run."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator
import pytest


ROOT = Path(__file__).resolve().parents[1]
VERIFIER_PATH = ROOT / "scripts/verification/parallel_host_parity.py"
SCHEMA_PATH = ROOT / "schemas/parallel-host-verification.schema.json"
SHA1 = bytes.fromhex("71879b74 6b972bde a21ccc25 cf02cddd d512ba10").hex()
SHA256 = "7" * 64


def verifier_module():
    spec = importlib.util.spec_from_file_location("m0_real_contract_verifier", VERIFIER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def doctor_result() -> dict:
    return {
        "schema": "ffs.doctor/v1",
        "version": "5.0.5",
        "scope": "user",
        "status": "degraded",
        "exit_code": 0,
        "checks": [
            {"id": "managed-path", "status": "pass", "message": "managed bytes match"},
            {
                "id": "model-routing-catalog",
                "status": "warn",
                "message": "expected models are absent from the upstream catalog",
                "remediation": "expected until the upstream catalog adds these model ids",
            },
        ],
    }


def doctor_observation(result: dict | None = None) -> dict:
    return {
        "schema": "ffs.doctor-observation/v1",
        "argv": ["/canonical/setup.sh", "--scope", "user", "--doctor", "--json"],
        "exit_status": 0,
        "started_utc": "2026-09-12T19:01:24Z",
        "completed_utc": "2026-09-12T19:01:25Z",
        "stdout": json.dumps(result if result is not None else doctor_result()),
        "stderr": "",
    }


def test_degraded_doctor_with_expected_catalog_warning_is_checked_pass() -> None:
    module = verifier_module()
    assert module._doctor_passes(doctor_observation()) is True


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("remediation", 7),
        ("remediation", ""),
        ("unknown-check-field", "invented"),
        ("check-status", "fail"),
        ("result-status", "failed"),
        ("result-exit", 1),
        ("outer-exit", 1),
    ],
)
def test_malformed_doctor_observations_remain_rejected(mutation: str, value: object) -> None:
    module = verifier_module()
    result = doctor_result()
    observation = doctor_observation(result)
    if mutation == "remediation":
        result["checks"][1]["remediation"] = value
    elif mutation == "unknown-check-field":
        result["checks"][1][str(value)] = "unverified"
    elif mutation == "check-status":
        result["checks"][1]["status"] = value
    elif mutation == "result-status":
        result["status"] = value
    elif mutation == "result-exit":
        result["exit_code"] = value
    else:
        observation["exit_status"] = value
    observation["stdout"] = json.dumps(result)
    assert module._doctor_passes(observation) is False


def git_repository_validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text())
    return Draft202012Validator({"$ref": "#/$defs/m0_git_repository", "$defs": schema["$defs"]})


@pytest.mark.parametrize("object_id", [SHA1, SHA256])
def test_m0_git_repository_accepts_real_git_object_id_formats(object_id: str) -> None:
    repository = {
        "root": "/private/tmp/canonical",
        "common_dir": "/private/tmp/canonical.git",
        "head": object_id,
        "generation": object_id,
    }
    assert list(git_repository_validator().iter_errors(repository)) == []


@pytest.mark.parametrize("object_id", ["g" * 40, "A" * 40, "7" * 39, "7" * 41, "7" * 63])
def test_m0_git_repository_rejects_malformed_object_ids(object_id: str) -> None:
    repository = {
        "root": "/private/tmp/canonical",
        "common_dir": "/private/tmp/canonical.git",
        "head": object_id,
        "generation": object_id,
    }
    assert len(list(git_repository_validator().iter_errors(repository))) == 2
