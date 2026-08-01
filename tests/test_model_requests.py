from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.model_requests import ModelRequestError, resolve_request, validate_document


@pytest.mark.parametrize(
    ("name", "model", "effort"),
    [
        ("judgment", "gpt-5.6-sol", "high"),
        ("execution", "gpt-5.6-terra", "medium"),
        ("volume", "gpt-5.6-luna", "low"),
    ],
)
def test_tier_requests_resolve_to_role_defaults(name: str, model: str, effort: str) -> None:
    resolved = resolve_request({"kind": "tier", "name": name})
    assert resolved == {
        "kind": "tier",
        "name": name,
        "model": model,
        "effort": effort,
        "exact_provenance": False,
    }


def test_exact_request_preserves_vendor_id_and_never_falls_back() -> None:
    resolved = resolve_request({"kind": "exact", "id": "claude-fable-5"})
    assert resolved["model"] == "claude-fable-5"
    assert resolved["exact_provenance"] is True
    assert resolved["fallback_allowed"] is False


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("judgment", "claude-opus-5"),
        ("execution", "claude-sonnet-5"),
        ("volume", "claude-haiku-4-5-20251001"),
    ],
)
def test_tier_requests_resolve_for_claude_host(name: str, model: str) -> None:
    resolved = resolve_request({"kind": "tier", "name": name}, host="claude")
    assert resolved["model"] == model
    assert resolved["effort"] is None


@pytest.mark.parametrize(
    "model_request",
    [
        "gpt-5.6-sol",
        {"kind": "tier", "name": "gpt-5.6-sol"},
        {"kind": "tier", "name": "premium"},
        {"kind": "exact"},
        {"kind": "exact", "id": ""},
        {"kind": "raw", "id": "gpt-5.6-sol"},
    ],
)
def test_invalid_or_raw_model_requests_fail_closed(model_request: object) -> None:
    with pytest.raises(ModelRequestError):
        resolve_request(model_request)


def test_document_lint_rejects_raw_vendor_ids_outside_exact(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"planner": {"model": "gpt-5.6-sol"}}))
    with pytest.raises(ModelRequestError, match="raw vendor model id"):
        validate_document(path)


def test_document_lint_accepts_only_typed_requests(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps(
            {
                "planner": {"kind": "tier", "name": "judgment"},
                "adversary": {"kind": "exact", "id": "claude-fable-5"},
            }
        )
    )
    validate_document(path)


def test_shipped_role_defaults_use_typed_requests() -> None:
    path = Path(__file__).resolve().parents[1] / "templates/model-requests.json"
    validate_document(path)
    data = json.loads(path.read_text())
    assert data["orchestrator"] == {"kind": "tier", "name": "execution"}
    assert data["gsd-planner"] == {"kind": "tier", "name": "judgment"}
    assert data["gsd-executor"] == {"kind": "tier", "name": "execution"}
    assert data["gsd-codebase-mapper"] == {"kind": "tier", "name": "volume"}


def test_adversary_runtime_surfaces_expose_only_typed_model_overrides() -> None:
    scripts = Path(__file__).resolve().parents[1] / "scripts/gsd"
    expectations = {
        "plan-adversary.sh": "PLAN_ADVERSARY_MODEL_REQUEST",
        "qa-coverage-adversary.sh": "QA_COVERAGE_MODEL_REQUEST",
        "review-gate-command.sh": "GSD_REVIEW_MODEL_REQUEST",
        "scope-drift-gate.sh": "GSD_DRIFT_MODEL_REQUEST",
    }
    for name, typed_variable in expectations.items():
        source = (scripts / name).read_text()
        assert typed_variable in source
        assert "adversary_invoke_typed_request" in source
