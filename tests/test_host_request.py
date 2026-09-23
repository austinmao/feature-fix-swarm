import json

import pytest

from run_state.host_request import (
    HostRequestRefused, parse_claude_host_request, parse_codex_host_request,
)


def test_codex_host_request_resolves_tier_without_accepting_argv(tmp_path):
    home = tmp_path / "runtime"
    binary = tmp_path / "codex"
    request = parse_codex_host_request(
        runtime_home=str(home), binary=str(binary),
        model_request_json=json.dumps({"kind": "tier", "name": "execution"}),
        sandbox="workspace-write", network_enabled=False,
        token_reservation=1000, timeout_seconds=600,
    )
    assert request.model == "gpt-5.6-terra"
    assert request.effort == "medium"
    assert set(request.material()) == {
        "host", "runtime_home", "binary", "model", "effort", "sandbox",
        "network_enabled", "token_reservation", "timeout_seconds",
    }


@pytest.mark.parametrize("defect", ["relative-home", "relative-binary", "model", "network", "tokens", "timeout"])
def test_codex_host_request_refuses_unbounded_or_unqualified_inputs(tmp_path, defect):
    values = dict(
        runtime_home=str(tmp_path / "runtime"), binary=str(tmp_path / "codex"),
        model_request_json=json.dumps({"kind": "tier", "name": "execution"}),
        sandbox="workspace-write", network_enabled=False,
        token_reservation=1000, timeout_seconds=600,
    )
    if defect == "relative-home": values["runtime_home"] = "runtime"
    elif defect == "relative-binary": values["binary"] = "codex"
    elif defect == "model": values["model_request_json"] = json.dumps({"kind": "tier", "name": "unknown"})
    elif defect == "network": values["network_enabled"] = True
    elif defect == "tokens": values["token_reservation"] = -1
    else: values["timeout_seconds"] = 0
    with pytest.raises(HostRequestRefused):
        parse_codex_host_request(**values)


def test_claude_host_request_requires_closed_subscription_inputs(tmp_path):
    request = parse_claude_host_request(
        runtime_home=str(tmp_path / "runtime"), credential_source=str(tmp_path / "credential"),
        binary=str(tmp_path / "claude"),
        model_request_json=json.dumps({"kind": "exact", "id": "claude-opus-5"}),
        sandbox="workspace-write", network_enabled=False, token_reservation=1000,
        timeout_seconds=600,
    )
    assert request.material()["host"] == "claude"
    with pytest.raises(HostRequestRefused, match="HOST_REQUEST_INVALID"):
        parse_claude_host_request(
            runtime_home=str(tmp_path / "runtime"), credential_source=str(tmp_path / "credential"),
            binary=str(tmp_path / "claude"),
            model_request_json=json.dumps({"kind": "exact", "id": "claude-opus-5"}),
            sandbox="read-only", network_enabled=False, token_reservation=1000,
            timeout_seconds=600,
        )
