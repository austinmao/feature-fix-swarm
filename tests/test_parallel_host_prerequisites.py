"""Independent regression contracts for upgraded-baseline findings B2/B4.

Auth and real tool denial are separate rollout gates; these tests exercise the
actual config projection and public model resolver without mocking either.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("tier", "model", "effort"),
    [
        ("frontier", "gpt-6-astra", "xhigh"),
        ("judgment", "gpt-6-sol", "xhigh"),
        ("execution", "gpt-5.6-terra", "medium"),
        ("volume", "gpt-5.6-luna", "low"),
    ],
)
def test_native_codex_tier_public_resolution(tier, model, effort):
    result = subprocess.run(
        [sys.executable, str(ROOT / "lib/model_requests.py"), "resolve",
         json.dumps({"kind": "tier", "name": tier}), "--host", "codex"],
        capture_output=True, text=True, check=True,
    )
    resolved = json.loads(result.stdout)
    assert (resolved["model"], resolved["effort"]) == (model, effort)


def test_exact_frontier_request_preserves_identity_and_forbids_fallback():
    result = subprocess.run(
        [sys.executable, str(ROOT / "lib/model_requests.py"), "resolve",
         json.dumps({"kind": "exact", "id": "gpt-6-astra"}), "--host", "codex"],
        capture_output=True, text=True, check=True,
    )
    resolved = json.loads(result.stdout)
    assert resolved["model"] == "gpt-6-astra"
    assert resolved["fallback_allowed"] is False


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_ambient_execution_surfaces_do_not_survive_config_projection(newline):
    module_spec = importlib.util.spec_from_file_location(
        "ffs_config_projection", ROOT / "scripts/gsd/sanitize-codex-config.py"
    )
    assert module_spec and module_spec.loader
    projection = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(projection)
    source = '''model = "ambient-model"
approval_policy = "never"
sandbox_mode = "danger-full-access"
web_search = "live"
notify = ["sh", "-c", "ambient-notify-canary"]
developer_instructions = "ambient instruction canary"
model_provider = "ambient-provider"
[sandbox_workspace_write]
network_access = true
writable_roots = ["/ambient-sibling"]
[mcp_servers.ambient]
command = "ambient-mcp-canary"
[agents.ambient]
config_file = "/ambient-agent-canary.toml"
[hooks]
ambient = "ambient-hook-canary"
[profiles.ambient]
sandbox_mode = "danger-full-access"
[shell_environment_policy]
inherit = "all"
[features]
apps = true
'''.replace("\n", newline)
    rendered = projection.sanitize(source)
    config = tomllib.loads(rendered)
    for key in (
        "approval_policy", "sandbox_mode", "web_search", "notify",
        "developer_instructions", "model_provider", "sandbox_workspace_write",
        "mcp_servers", "agents", "hooks", "profiles", "shell_environment_policy",
        "features",
    ):
        assert key not in config, f"ambient execution surface survived: {key}"
    assert "ambient-" not in rendered
