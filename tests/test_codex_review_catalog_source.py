"""spec-014 E8 prerequisite 4: the Codex native review catalog comes from the qualified binary.

The production source is the reviewer binary's own bundled catalog (``codex debug models --bundled``),
produced by FFS from the launcher the runtime was qualified with, never a caller-supplied file.  The tier
names resolve through ``lib/model_requests.py``, which must agree with ``scripts/gsd/model-equivalents.sh``;
an exact or tier model absent from the bundled catalog refuses typed, with no alternate slug.

Fixture-level proof only (a scripted binary): not native host qualification and not E8.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from model_requests import CLAUDE_TIERS, CODEX_TIERS, resolve_request
from run_state import cli
from run_state.frontend_producers import HostRuntimeSeam, _native_request
from run_state.host_request import HostRequestRefused
from run_state.native_review_runtime import (
    CLAUDE_CLI_VERSION,
    CODEX_CLI_VERSION,
    NativeReviewRequest,
    NativeReviewRuntimeRefused,
    prepare_native_review_runtime,
    validate_native_review_material,
)
from run_state.supervisor import SupervisorRefused

ROOT = Path(__file__).resolve().parents[1]
TIERS = ("frontier", "judgment", "execution", "volume")
SESSION = "643b3a28-33d2-4000-b983-a18c5da41bbd"


def _bundled(slugs) -> bytes:
    return json.dumps({"models": [{"slug": slug, "display_name": slug, "apply_patch_tool_type": "freeform",
                                   "experimental_supported_tools": ["clock"], "tool_mode": "code_mode_only"}
                                  for slug in slugs]}, sort_keys=True).encode() + b"\n"


def _fake(tmp_path: Path, stdout: bytes = b"", *, status: int = 0, delay: int = 0) -> tuple[Path, Path]:
    """A scripted Codex that prints ``stdout`` for ``debug models --bundled`` and logs every call."""
    catalog = tmp_path / "bundled.json"
    catalog.write_bytes(stdout)
    log = tmp_path / "calls.log"
    binary = tmp_path / "codex"
    binary.write_text(
        "#!/bin/sh\n"
        f"{{ printf 'argv=%s\\n' \"$*\"; printf 'cwd=%s\\n' \"$(pwd)\"; env; ls -A \"$HOME\" | sed 's/^/home=/'; "
        f"stat -f 'mode=%Lp' \"$HOME\" 2>/dev/null || stat -c 'mode=%a' \"$HOME\"; }} >> '{log}'\n"
        f"[ \"$*\" = 'debug models --bundled' ] || exit 64\n"
        f"sleep {delay}\ncat '{catalog}'\nexit {status}\n")
    binary.chmod(0o755)
    return binary, log


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    workspace = tmp_path / "review-workspace"
    workspace.mkdir(mode=0o755)
    return parent, workspace


def _codex_request(binary: Path, model: str, effort: str, **fields) -> NativeReviewRequest:
    return NativeReviewRequest(
        host="codex", requested_model=model, cli_version=CODEX_CLI_VERSION, binary=str(binary),
        binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(), runtime_identity="codex-catalog-test",
        prompt="Review only the supplied artifacts; do not modify files.", effort=effort, **fields)


@pytest.mark.parametrize("tier", TIERS)
def test_tier_tables_match_model_equivalents_sh(tier):
    claude_model = CLAUDE_TIERS[tier][0]
    source = f". '{ROOT / 'scripts/gsd/model-equivalents.sh'}'"
    model = subprocess.run(["bash", "-c", f"{source}; codex_equiv_model '{claude_model}'"],
                           capture_output=True, text=True, check=True).stdout.strip()
    effort = subprocess.run(["bash", "-c", f"{source}; codex_equiv_effort '{claude_model}'"],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert (model, effort) == CODEX_TIERS[tier]


@pytest.mark.parametrize("tier", TIERS)
def test_codex_tier_review_narrows_the_binary_bundled_catalog(tmp_path, tier):
    resolved = resolve_request({"kind": "tier", "name": tier}, host="codex")
    stdout = _bundled([*(model for model, _effort in CODEX_TIERS.values()), "gpt-decoy"])
    binary, log = _fake(tmp_path, stdout)
    parent, workspace = _dirs(tmp_path)
    material = prepare_native_review_runtime(_codex_request(binary, resolved["model"], resolved["effort"]),
                                             runtime_root=parent / tier, workspace=workspace)
    assert material.source_catalog_sha256 == hashlib.sha256(stdout).hexdigest()
    private = json.loads(Path(material.catalog_path).read_text())
    assert [item["slug"] for item in private["models"]] == [resolved["model"]]
    assert private["default_model"] == resolved["model"]
    assert material.argv[material.argv.index("--model") + 1] == resolved["model"]
    assert f'model_reasoning_effort="{resolved["effort"]}"' in material.argv
    assert log.read_text().count("argv=debug models --bundled") == 1
    assert validate_native_review_material(material) is material
    assert log.read_text().count("argv=") == 1


@pytest.mark.parametrize("tier", TIERS)
def test_claude_tier_review_needs_no_catalog_and_runs_no_binary(tmp_path, tier):
    resolved = resolve_request({"kind": "tier", "name": tier}, host="claude")
    binary, log = _fake(tmp_path, _bundled(["unused"]))
    parent, workspace = _dirs(tmp_path)
    request = NativeReviewRequest(
        host="claude", requested_model=resolved["model"], cli_version=CLAUDE_CLI_VERSION, binary=str(binary),
        binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(), runtime_identity="claude-catalog-test",
        prompt="Review bound artifacts only.", effort=None, session_id=SESSION)
    material = prepare_native_review_runtime(request, runtime_root=parent / tier, workspace=workspace)
    assert (material.catalog_path, material.catalog_sha256, material.source_catalog_sha256) == (None, None, None)
    assert material.argv[material.argv.index("--model") + 1] == resolved["model"]
    assert "--effort" not in material.argv
    assert not log.exists()


@pytest.mark.parametrize("request_kind", ["exact", "tier"])
def test_model_absent_from_the_bundled_catalog_refuses_typed(tmp_path, request_kind):
    request = ({"kind": "exact", "id": "gpt-6-sol"} if request_kind == "exact"
               else {"kind": "tier", "name": "judgment"})
    resolved = resolve_request(request, host="codex")
    binary, log = _fake(tmp_path, _bundled(["gpt-6.1-sol", "gpt-5.6-sol"]))
    parent, workspace = _dirs(tmp_path)
    with pytest.raises(NativeReviewRuntimeRefused) as caught:
        prepare_native_review_runtime(_codex_request(binary, resolved["model"], resolved["effort"]),
                                      runtime_root=parent / "absent", workspace=workspace)
    assert caught.value.code == "NATIVE_REVIEW_MODEL_UNAVAILABLE"
    assert not (parent / "absent").exists()
    assert log.read_text().count("argv=") == 1


def test_bundled_catalog_runs_in_a_closed_empty_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FFS_AMBIENT_SENTINEL", "leak")
    binary, log = _fake(tmp_path, _bundled(["gpt-5.6-terra"]))
    parent, workspace = _dirs(tmp_path)
    prepare_native_review_runtime(_codex_request(binary, "gpt-5.6-terra", "medium"),
                                  runtime_root=parent / "closed", workspace=workspace)
    lines = log.read_text().splitlines()
    env = dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith(("argv=", "home=")))
    assert env["HOME"] == env["CODEX_HOME"]
    assert Path(env["HOME"]).parent == parent
    assert env["cwd"] == "/"
    assert env["mode"] == "700"
    assert not [line for line in lines if line.startswith("home=")]
    assert "FFS_AMBIENT_SENTINEL" not in env
    assert sorted(path.name for path in parent.iterdir()) == ["closed"]


@pytest.mark.parametrize("case", ["nonzero", "empty", "not-json", "timeout"])
def test_bundled_catalog_failure_refuses_typed(tmp_path, monkeypatch, case):
    import run_state.native_review_runtime as runtime
    monkeypatch.setattr(runtime, "_BUNDLED_CATALOG_TIMEOUT", 1)
    stdout = {"nonzero": _bundled(["gpt-5.6-terra"]), "empty": b"", "not-json": b"models\n",
              "timeout": _bundled(["gpt-5.6-terra"])}[case]
    binary, _log = _fake(tmp_path, stdout, status=3 if case == "nonzero" else 0,
                         delay=5 if case == "timeout" else 0)
    parent, workspace = _dirs(tmp_path)
    with pytest.raises(NativeReviewRuntimeRefused) as caught:
        prepare_native_review_runtime(_codex_request(binary, "gpt-5.6-terra", "medium"),
                                      runtime_root=parent / case, workspace=workspace)
    assert caught.value.code == "NATIVE_REVIEW_CATALOG_UNAVAILABLE"
    assert not (parent / case).exists()
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize("stdout", [b'{"models": []}\n', b"{}\n", b"[]\n", b"x" * (2 * 1024 * 1024 + 1)],
                         ids=["no-models", "no-models-key", "not-an-object", "oversize"])
def test_an_unusable_bundled_catalog_refuses_catalog_unavailable(tmp_path, stdout):
    binary, _log = _fake(tmp_path, stdout)
    parent, workspace = _dirs(tmp_path)
    with pytest.raises(NativeReviewRuntimeRefused) as caught:
        prepare_native_review_runtime(_codex_request(binary, "gpt-5.6-terra", "medium"),
                                      runtime_root=parent / "unusable", workspace=workspace)
    assert caught.value.code == "NATIVE_REVIEW_CATALOG_UNAVAILABLE"
    assert list(parent.iterdir()) == []


def test_a_descendant_holding_stdout_refuses_within_the_deadline(tmp_path, monkeypatch):
    import time
    import run_state.native_review_runtime as runtime
    monkeypatch.setattr(runtime, "_BUNDLED_CATALOG_TIMEOUT", 1)
    binary, _log = _fake(tmp_path, _bundled(["gpt-5.6-terra"]))
    binary.write_text(binary.read_text().replace("sleep 0\n", "(sleep 20 &)\n"))
    parent, workspace = _dirs(tmp_path)
    started = time.monotonic()
    with pytest.raises(NativeReviewRuntimeRefused) as caught:
        prepare_native_review_runtime(_codex_request(binary, "gpt-5.6-terra", "medium"),
                                      runtime_root=parent / "held", workspace=workspace)
    assert caught.value.code == "NATIVE_REVIEW_CATALOG_UNAVAILABLE"
    assert time.monotonic() - started < 10
    assert list(parent.iterdir()) == []


def test_a_descendant_appending_after_the_leader_exits_is_part_of_the_output(tmp_path):
    binary, _log = _fake(tmp_path, _bundled(["gpt-5.6-terra"]))
    binary.write_text(binary.read_text().replace("exit 0\n", "(sleep 1; echo trailing) &\nexit 0\n"))
    parent, workspace = _dirs(tmp_path)
    with pytest.raises(NativeReviewRuntimeRefused) as caught:
        prepare_native_review_runtime(_codex_request(binary, "gpt-5.6-terra", "medium"),
                                      runtime_root=parent / "appended", workspace=workspace)
    assert caught.value.code == "NATIVE_REVIEW_CATALOG_UNAVAILABLE"


def test_a_launcher_changed_while_its_catalog_runs_refuses(tmp_path):
    binary, _log = _fake(tmp_path, _bundled(["gpt-5.6-terra"]))
    binary.write_text(binary.read_text().replace("exit 0\n", f"printf '# swapped\\n' >> '{binary}'\nexit 0\n"))
    parent, workspace = _dirs(tmp_path)
    with pytest.raises(NativeReviewRuntimeRefused, match="changed while its catalog ran"):
        prepare_native_review_runtime(_codex_request(binary, "gpt-5.6-terra", "medium"),
                                      runtime_root=parent / "swapped", workspace=workspace)
    assert not (parent / "swapped").exists()


def test_a_partial_caller_catalog_refuses_without_running_the_binary(tmp_path):
    binary, log = _fake(tmp_path, _bundled(["gpt-5.6-terra"]))
    parent, workspace = _dirs(tmp_path)
    with pytest.raises(NativeReviewRuntimeRefused):
        prepare_native_review_runtime(_codex_request(binary, "gpt-5.6-terra", "medium",
                                                     catalog_path=str(tmp_path / "bundled.json")),
                                      runtime_root=parent / "partial", workspace=workspace)
    assert not log.exists()


def _seam(binary: Path) -> HostRuntimeSeam:
    return HostRuntimeSeam(host="codex", qualify=None, bind=None, binary=str(binary), cli_version=CODEX_CLI_VERSION,
                           model="gpt-5.6-terra", effort="medium", model_request={"kind": "tier", "name": "execution"})


def test_production_review_request_carries_no_caller_catalog_and_pins_the_qualified_launcher(tmp_path):
    binary, _log = _fake(tmp_path, _bundled(["gpt-5.6-terra"]))
    pin = hashlib.sha256(binary.read_bytes()).hexdigest()
    request = _native_request(_seam(binary), runtime_identity="tuple", prompt="p",
                              qualified_binary=(("launcher_sha256", pin),))
    assert (request.catalog_path, request.catalog_sha256) == (None, None)
    assert request.binary_sha256 == pin
    with pytest.raises(SupervisorRefused) as caught:
        _native_request(_seam(binary), runtime_identity="tuple", prompt="p", qualified_binary=())
    assert caught.value.code == "HOST_CAPABILITY_UNQUALIFIED"


def test_a_launcher_swapped_after_qualification_refuses_before_its_catalog_runs(tmp_path):
    binary, log = _fake(tmp_path, _bundled(["gpt-5.6-terra"]))
    pin = hashlib.sha256(binary.read_bytes()).hexdigest()
    binary.write_text(binary.read_text() + "# swapped\n")
    request = _native_request(_seam(binary), runtime_identity="tuple", prompt="p",
                              qualified_binary=(("launcher_sha256", pin),))
    parent, workspace = _dirs(tmp_path)
    with pytest.raises(NativeReviewRuntimeRefused):
        prepare_native_review_runtime(request, runtime_root=parent / "swapped", workspace=workspace)
    assert not log.exists()


def test_the_retired_review_model_catalog_flag_refuses_typed(tmp_path):
    catalog = tmp_path / "models.json"
    catalog.write_bytes(_bundled(["gpt-5.6-terra"]))
    assert cli._review_catalog_from_args(argparse.Namespace(review_model_catalog=None)) is None
    with pytest.raises(HostRequestRefused) as caught:
        cli._review_catalog_from_args(argparse.Namespace(review_model_catalog=str(catalog)))
    assert caught.value.code == "REVIEW_MODEL_CATALOG_RETIRED"
