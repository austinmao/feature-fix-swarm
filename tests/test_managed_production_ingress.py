"""Actual candidate shell entry through pinned upstream preparation to refusal.

These are production-boundary checks. They do not qualify an executable host
or claim that a GSD executor wave ran.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from run_state.state import ControlStore
from run_state.managed import MANAGED_WRITER_VERSION
from test_m4_upstream_context_acceptance import (
    ROOT, _env, _manifest, _register, _registered_runtime, _repository,
    _tree, _write_manifest, _start,
)


def _setup(tmp_path):
    primary = _repository(tmp_path)
    authority = tmp_path / "authority"
    repository_id = _register(primary, authority)
    selection = _write_manifest(tmp_path, _manifest(
        primary, repository_id,
        upstream={"project": None, "workstream": None, "session_key": "ingress-session"},
    ))
    runtime, digest = _registered_runtime()
    env = _env(tmp_path)
    env.update(
        PATH=str(Path(sys.executable).parent) + os.pathsep + env["PATH"],
        FFS_SELECTION_MANIFEST=str(selection), FFS_STATE_ROOT=str(authority),
        FFS_UPSTREAM_RUNTIME_MANIFEST=str(runtime), FFS_UPSTREAM_RUNTIME_SHA256=digest,
        FFS_REQUEST_KEY="production-request", FFS_OBJECTIVE="production ingress",
        FFS_DISPATCH_LIMIT="3", GSD_TOKEN_BUDGET="1000", GSD_RUN_ID="production-ingress",
    )
    return primary, authority, repository_id, env


def _run(primary, env, *args):
    return subprocess.run(
        ["bash", str(ROOT / "scripts/gsd/gsd-run.sh"), *args],
        cwd=primary, env=env, capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize("entry", ["managed-start", "frontend-start"])
@pytest.mark.parametrize(("estimate", "tier"), [
    ({"files": 2, "loc": 50, "protected": False}, "small"),
    ({"files": 21, "loc": 50, "protected": False}, "large"),
    (None, "medium"),
])
def test_real_cli_persists_ceremony_estimate_and_refuses_tier_reset(tmp_path, entry, estimate, tier):
    setup = _setup
    if entry == "frontend-start":
        from test_frontend_production_ingress import _setup as setup
    primary, authority, repository_id, env = setup(tmp_path)
    argv = [sys.executable, "-m", "run_state.cli", entry,
            "--objective", env["FFS_OBJECTIVE"], "--state-root", str(authority),
            "--upstream-runtime-manifest", env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
            "--upstream-runtime-sha256", env["FFS_UPSTREAM_RUNTIME_SHA256"],
            "--request-key", env["FFS_REQUEST_KEY"], "--run-id", env["GSD_RUN_ID"],
            "--dispatch-limit", "3", "--token-limit", "1000"]
    if estimate is not None:
        argv += ["--ceremony-estimate", json.dumps(estimate)]
    if entry == "managed-start":
        tail = ["--selection-manifest", env["FFS_SELECTION_MANIFEST"], "--", "/gsd-plan-phase", "1"]
    else:
        tail = ["--frontend", "fix", "--select-file", "src/selected.sh"]
    result = subprocess.run(argv + tail, cwd=primary, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 78, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["code"] == "HOST_CAPABILITY_UNQUALIFIED"
    store = ControlStore(authority / "control.sqlite3")
    budget = store.get_run_policy_budget(repository_id=repository_id, run_id=env["GSD_RUN_ID"])
    assert budget.tier == tier and budget.launch_charged == 0
    changed = list(argv)
    replacement = json.dumps({"files": 10, "loc": 300, "protected": False})
    if estimate is None:
        changed += ["--ceremony-estimate", replacement]
    else:
        changed[changed.index("--ceremony-estimate") + 1] = replacement
    refused = subprocess.run(changed + ["--resume"] + tail, cwd=primary, env=env,
                             capture_output=True, text=True, timeout=60)
    assert refused.returncode != 0
    assert json.loads(refused.stdout)["code"] == "IDEMPOTENCY_CONFLICT"
    after = store.get_run_policy_budget(repository_id=repository_id, run_id=env["GSD_RUN_ID"])
    assert after.tier == tier and after.launch_charged == 0


def test_actual_shell_configures_limits_and_refuses_unqualified_host(tmp_path):
    primary, authority, repository_id, env = _setup(tmp_path)
    before = _tree(primary)
    result = _run(primary, env, "/gsd-execute-phase", "1")
    assert result.returncode == 78, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["code"] == "HOST_CAPABILITY_UNQUALIFIED"
    store = ControlStore(authority / "control.sqlite3")
    with store.read_transaction() as tx:
        context = tx.execute("SELECT * FROM context_runs WHERE repository_id=?", (repository_id,)).fetchone()
        activity = tx.execute("SELECT * FROM authority_activities WHERE id=?", (context["activity_id"],)).fetchone()
        limits = tx.execute("SELECT * FROM authority_run_limits").fetchone()
        assert activity["kind"] == "execute"
        assert context["writer_version"] == MANAGED_WRITER_VERSION
        assert activity["runtime_tuple_hash"] is None
        assert limits["dispatch_limit"] == 3
        assert limits["token_limit"] == 1000
        assert tx.execute("SELECT COUNT(*) FROM authority_launch_intents").fetchone()[0] == 0
    assert not (primary / ".planning" / "run-state").exists()
    # Git administration legitimately registers a worktree; primary source and
    # planning bytes are unchanged by the managed invocation.
    assert {k: v for k, v in _tree(primary).items() if not k.startswith(".git")} == {
        k: v for k, v in before.items() if not k.startswith(".git")
    }


@pytest.mark.parametrize("change", ["flags", "skill", "limit"])
def test_actual_shell_replay_rejects_changed_material_before_new_owner(tmp_path, change):
    primary, authority, _, env = _setup(tmp_path)
    assert _run(primary, env, "/gsd-plan-phase", "1").returncode == 78
    before = _tree(authority)
    command = ["/gsd-plan-phase", "1"]
    if change == "flags":
        command += ["--gaps"]
    elif change == "skill":
        command[0] = "/gsd-discuss-phase"
    else:
        env["FFS_DISPATCH_LIMIT"] = "4"
    result = _run(primary, env, *command)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["code"] == "IDEMPOTENCY_CONFLICT"
    assert _tree(authority) == before


def test_candidate_cannot_adopt_legacy_run(tmp_path):
    primary, authority, _, env = _setup(tmp_path)
    selection = Path(env["FFS_SELECTION_MANIFEST"])
    created = _start(authority, primary, selection, env["GSD_RUN_ID"], env=env)
    assert created.returncode == 0, (created.stdout, created.stderr)
    before = _tree(authority)
    env["GSD_RESUME"] = "1"
    refused = _run(primary, env, "/gsd-plan-phase", "1")
    assert json.loads(refused.stdout)["code"] == "WRITER_HANDOFF_REQUIRED"
    assert refused.returncode != 0
    assert _tree(authority) == before


def test_legacy_entry_cannot_write_managed_run(tmp_path):
    primary, authority, _, env = _setup(tmp_path)
    assert _run(primary, env, "/gsd-plan-phase", "1").returncode == 78
    before = _tree(authority)
    refused = _start(
        authority, primary, Path(env["FFS_SELECTION_MANIFEST"]),
        env["GSD_RUN_ID"], env=env, resume=True,
    )
    assert refused.returncode != 0
    assert json.loads(refused.stdout)["code"] == "WRITER_VERSION_MISMATCH"
    assert _tree(authority) == before


def test_resume_request_key_retains_its_own_command_binding(tmp_path):
    primary, authority, _, env = _setup(tmp_path)
    assert _run(primary, env, "/gsd-plan-phase", "1").returncode == 78
    env.update(GSD_RESUME="1", FFS_REQUEST_KEY="resume-request")
    assert _run(primary, env, "/gsd-plan-phase", "1").returncode == 78
    before = _tree(authority)
    refused = _run(primary, env, "/gsd-plan-phase", "1", "--gaps")
    assert refused.returncode == 2, (refused.stdout, refused.stderr)
    assert json.loads(refused.stdout)["code"] == "IDEMPOTENCY_CONFLICT"
    assert _tree(authority) == before


def test_legacy_complete_cannot_finalize_managed_activity(tmp_path):
    primary, authority, _, env = _setup(tmp_path)
    assert _run(primary, env, "/gsd-plan-phase", "1").returncode == 78
    before = _tree(authority)
    refused = subprocess.run(
        [sys.executable, "-m", "run_state.cli", "complete", env["GSD_RUN_ID"],
         "--state-root", str(authority), "--result-locator", "unverified-result",
         "--result-sha256", "f" * 64],
        cwd=primary, env=env, capture_output=True, text=True, timeout=60,
    )
    assert refused.returncode != 0, (refused.stdout, refused.stderr)
    assert json.loads(refused.stdout)["code"] == "WRITER_VERSION_MISMATCH"
    assert _tree(authority) == before


@pytest.mark.parametrize("setting", ["FFS_DISPATCH_LIMIT", "GSD_TOKEN_BUDGET"])
def test_oversized_limits_refuse_before_run_preparation(tmp_path, setting):
    primary, authority, _, env = _setup(tmp_path)
    env[setting] = str(2**63)
    before = _tree(authority)
    refused = _run(primary, env, "/gsd-plan-phase", "1")
    assert refused.returncode == 2, (refused.stdout, refused.stderr)
    assert json.loads(refused.stdout)["code"] == "INVALID_REQUEST"
    assert _tree(authority) == before
