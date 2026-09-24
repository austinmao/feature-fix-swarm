"""Actual frontend shell/CLI admission; no native host qualification claim."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from run_state.state import ControlStore
from test_m4_workspace_acceptance import _repository, _git
from test_m4_workspace_hardening import _track_planning_context
from test_m4_upstream_context_acceptance import ROOT, _env, _register, _registered_runtime, _tree


def _setup(tmp_path):
    primary = _repository(tmp_path)
    _track_planning_context(primary)
    authority = tmp_path / "authority"
    repository_id = _register(primary, authority)
    runtime, digest = _registered_runtime()
    env = _env(tmp_path)
    env.update(
        PATH=str(Path(sys.executable).parent) + os.pathsep + env["PATH"],
        FFS_STATE_ROOT=str(authority), FFS_UPSTREAM_RUNTIME_MANIFEST=str(runtime),
        FFS_UPSTREAM_RUNTIME_SHA256=digest, FFS_REQUEST_KEY="frontend-first",
        FFS_OBJECTIVE="frontend admission", FFS_DISPATCH_LIMIT="3",
        GSD_TOKEN_BUDGET="1000", GSD_RUN_ID="frontend-run",
    )
    return primary, authority, repository_id, env


def _run(primary, env, frontend="fix", *options):
    return subprocess.run(
        ["bash", str(ROOT / "scripts/gsd/ffs-frontend.sh"), frontend, *options],
        cwd=primary, env=env, capture_output=True, text=True, timeout=60,
    )


def _source_tree(primary):
    return {k: v for k, v in _tree(primary).items() if not k.startswith(".git")}


@pytest.mark.parametrize("frontend", ["feature-spec", "fix", "code-uplift", "feature-implement"])
def test_frontend_retains_registered_runtime_for_parent_resource_admission(tmp_path, monkeypatch, frontend):
    from run_state import cli, supervisor
    from run_state.upstream import UpstreamRuntime

    primary, authority, _, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    for key in ("GSD_RUN_ID", "FFS_RUN_ID", "GSD_RESUME"):
        monkeypatch.delenv(key, raising=False)
    reached = []

    def execute(store, token, context, **kwargs):
        runtime = kwargs.get("upstream_runtime")
        assert isinstance(runtime, UpstreamRuntime), "parent resource admission requires the registered runtime"
        runtime.verify()
        assert Path(context.workspace) != primary
        assert kwargs["command"] == (frontend,)
        reached.append(runtime)
        return 0

    monkeypatch.setattr(supervisor, "run_managed_command", execute)
    assert cli.main([
        "frontend-start", "--frontend", frontend, "--objective", "runtime binding",
        "--state-root", str(authority), "--request-key", "runtime-binding",
        "--run-id", "frontend-runtime-binding", "--dispatch-limit", "3", "--token-limit", "1000",
        "--upstream-runtime-manifest", env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
        "--upstream-runtime-sha256", env["FFS_UPSTREAM_RUNTIME_SHA256"],
    ]) == 0
    assert len(reached) == 1


def _frontend_refusal(tmp_path, monkeypatch, capsys, error):
    """Drive frontend-start to a managed-run refusal and return (rc, envelope)."""
    from run_state import cli, supervisor

    primary, authority, _, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    for key in ("GSD_RUN_ID", "FFS_RUN_ID", "GSD_RESUME"):
        monkeypatch.delenv(key, raising=False)

    def refuse(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(supervisor, "run_managed_command", refuse)
    returncode = cli.main([
        "frontend-start", "--frontend", "fix", "--objective", "typed refusal",
        "--state-root", str(authority), "--request-key", "typed-refusal",
        "--run-id", "frontend-typed-refusal", "--dispatch-limit", "3", "--token-limit", "1000",
        "--upstream-runtime-manifest", env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
        "--upstream-runtime-sha256", env["FFS_UPSTREAM_RUNTIME_SHA256"],
    ])
    return returncode, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_frontend_policy_refusal_is_a_typed_envelope_not_a_traceback(tmp_path, monkeypatch, capsys):
    from run_state.frontend_policy import FrontendPolicyRefused

    returncode, body = _frontend_refusal(
        tmp_path, monkeypatch, capsys, FrontendPolicyRefused("FRONTEND_CHECK_CANDIDATE_STALE"),
    )
    assert returncode == 78
    assert body["ok"] is False and body["code"] == "FRONTEND_CHECK_CANDIDATE_STALE"
    assert body["run_id"] == "frontend-typed-refusal"
    assert body["recovery_action"]["action"] != "qualify_host_adapter"


@pytest.mark.parametrize(("code", "cause", "detail", "action"), [
    ("HOST_CAPABILITY_UNQUALIFIED", ("RuntimeStagingError", "retained stage contains an unowned or missing file"),
     "RuntimeStagingError: retained stage contains an unowned or missing file", "qualify_host_adapter"),
    # A message naming a path (or any value) is reduced to the error type.
    ("HOST_CAPABILITY_UNQUALIFIED", ("CapabilityError", "runtime tree contains unsafe member: /home/u/.codex/x"),
     "CapabilityError", "qualify_host_adapter"),
    ("RETAINED_RUNTIME_NOT_REUSABLE", ("RetainedRuntimeNotReusable", "staged auth has been revoked"),
     "RetainedRuntimeNotReusable: staged auth has been revoked", "resume_with_new_request_key"),
])
def test_supervisor_refusal_envelope_carries_a_path_free_detail(tmp_path, monkeypatch, capsys,
                                                                code, cause, detail, action):
    import host_capabilities
    from run_state import runtime_staging
    from run_state.supervisor import SupervisorRefused

    kind, message = cause
    error = SupervisorRefused(code)
    error.__cause__ = getattr(runtime_staging, kind, getattr(host_capabilities, kind, None))(message)
    returncode, body = _frontend_refusal(tmp_path, monkeypatch, capsys, error)
    assert returncode == 78 and body["code"] == code
    assert body["detail"] == detail
    assert body["recovery_action"] == {"action": action}


@pytest.mark.parametrize(("frontend", "kind"), [
    ("feature-spec", "plan"), ("fix", "plan"), ("code-uplift", "review"),
    ("feature-implement", "execute"),
])
def test_frontend_shell_prepares_explicit_overlay_and_refuses_host(tmp_path, frontend, kind):
    primary, authority, repository_id, env = _setup(tmp_path)
    (primary / "src/selected.sh").write_bytes(b"selected edit\n")
    (primary / "src/selected.sh").chmod(0o755)
    (primary / "src/delete.txt").unlink()
    (primary / "new.txt").write_bytes(b"selected new\n")
    (primary / "src/unrelated.txt").write_bytes(b"unselected edit\n")
    before = _source_tree(primary)
    options = ("--select-file", "src/selected.sh", "--select-file", "new.txt",
               "--delete-file", "src/delete.txt")
    result = _run(primary, env, frontend, *options)
    assert result.returncode == 78, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["code"] == "HOST_CAPABILITY_UNQUALIFIED"
    store = ControlStore(authority / "control.sqlite3")
    with store.read_transaction() as tx:
        context = tx.execute("SELECT * FROM context_runs WHERE repository_id=?", (repository_id,)).fetchone()
        preparation = tx.execute("SELECT * FROM context_workspaces WHERE preparation_id=?", (context["preparation_id"],)).fetchone()
        activity = tx.execute("SELECT * FROM authority_activities WHERE id=?", (context["activity_id"],)).fetchone()
        limits = dict(tx.execute("SELECT * FROM authority_run_limits").fetchone())
        assert context["writer_version"] == "ffs-supervisor/1"
        assert activity["kind"] == kind
        assert activity["runtime_tuple_hash"] is None
        assert limits["dispatch_limit"] == 3 and limits["token_limit"] == 1000
        # Capacity includes the supervised outer controller, so 3 permits the
        # controller plus the operating default of two worker/reviewer children.
        assert limits["worker_capacity"] == 3 and limits["dispatch_used"] == 0
        assert tx.execute("SELECT COUNT(*) FROM authority_launch_intents").fetchone()[0] == 0
        binding = tx.execute("SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id WHERE k.activity_id=? AND k.idempotency_key='frontend-operation'", (context["activity_id"],)).fetchone()
        assert json.loads(binding["payload"])["data"]["frontend"] == frontend
    contract = store.get_acceptance_contract(
        repository_id=repository_id, run_id=context["run_id"],
    )
    assert contract is not None
    assert contract.generation == 1
    assert contract.active_obligation_ids == ()
    assert contract.accepted_requirement_ids == (
        "objective:" + hashlib.sha256(b"frontend admission").hexdigest(),
    )
    workspace = Path(preparation["path"])
    assert (workspace / "src/selected.sh").read_bytes() == b"selected edit\n"
    assert (workspace / "src/selected.sh").stat().st_mode & 0o111
    assert (workspace / "new.txt").read_bytes() == b"selected new\n"
    assert not (workspace / "src/delete.txt").exists()
    assert (workspace / "src/unrelated.txt").read_bytes() == b"base-unrelated\n"
    assert _source_tree(primary) == before
    refs = _git("worktree", "list", "--porcelain", cwd=primary).stdout
    replay = _run(primary, env, frontend, *options)
    assert replay.returncode == 78, (replay.stdout, replay.stderr)
    assert _git("worktree", "list", "--porcelain", cwd=primary).stdout == refs
    with store.read_transaction() as tx:
        after = dict(tx.execute("SELECT * FROM authority_run_limits").fetchone())
        assert after == limits
        assert tx.execute("SELECT COUNT(*) FROM authority_event_keys WHERE idempotency_key='frontend-operation'").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["source", "frontend", "limit"])
def test_frontend_same_key_changed_material_refuses_without_effects(tmp_path, change):
    primary, authority, _, env = _setup(tmp_path)
    assert _run(primary, env, "fix", "--select-file", "src/selected.sh").returncode == 78
    frontend = "fix"
    if change == "source":
        (primary / "src/selected.sh").write_bytes(b"changed selection\n")
    elif change == "frontend":
        frontend = "feature-spec"
    else:
        env["FFS_DISPATCH_LIMIT"] = "4"
    before = _tree(authority)
    result = _run(primary, env, frontend, "--select-file", "src/selected.sh")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["code"] == "IDEMPOTENCY_CONFLICT"
    assert _tree(authority) == before


@pytest.mark.parametrize("frontend", ["feature-spec", "code-uplift"])
def test_frontend_new_key_cannot_change_unfinished_operation(tmp_path, frontend):
    primary, authority, _, env = _setup(tmp_path)
    assert _run(primary, env).returncode == 78
    env.update(FFS_REQUEST_KEY="frontend-resume", GSD_RESUME="1")
    result = _run(primary, env, frontend)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["code"] in {"IDEMPOTENCY_CONFLICT", "MANAGED_COMMAND_CONTEXT_CONFLICT"}
    with ControlStore(authority / "control.sqlite3").read_transaction() as tx:
        assert tx.execute("SELECT COUNT(*) FROM authority_launch_intents").fetchone()[0] == 0
        assert tx.execute("SELECT dispatch_used FROM authority_run_limits").fetchone()[0] == 0


@pytest.mark.parametrize("option", ["--frontend", "--state-root", "--dispatch-limit"])
def test_frontend_shell_cannot_override_controller_identity(tmp_path, option):
    primary, authority, _, env = _setup(tmp_path)
    before = _tree(authority)
    result = _run(primary, env, "fix", option, "override")
    assert result.returncode == 2
    assert "unsupported selection option" in result.stderr
    assert _tree(authority) == before


# DEFERRED (spec-014 Release B ledger, operator ruling 2): frontend skills keep
# main's text; the managed admission section lands with the deferred skill wiring.
deferred_skill_wiring = pytest.mark.xfail(
    reason="DEFERRED: frontend skill admission wiring (spec-014 Release B ledger)",
    raises=ValueError, strict=True,
)


@deferred_skill_wiring
@pytest.mark.parametrize("frontend", ["feature-spec", "feature-implement", "fix", "code-uplift"])
def test_frontend_skill_routes_before_init_and_requires_ambient_stop(frontend):
    text = (ROOT / "skills" / frontend / "SKILL.md").read_text()
    admission = text.index("## Managed frontend admission")
    assert admission < text.index("## Host dispatch contract")
    assert f"ffs-frontend.sh {frontend}" in text
    assert "Relay the command's result and end this invocation." in text
    assert "this candidate does not execute it after bootstrap returns" in text


@pytest.mark.parametrize("change", ["removed", "changed", "head-ahead"])
def test_frontend_resume_uses_retained_selection_after_source_changes(tmp_path, change):
    primary, authority, _, env = _setup(tmp_path)
    (primary / "src/selected.sh").write_bytes(b"retained edit\n")
    assert _run(primary, env, "fix", "--select-file", "src/selected.sh").returncode == 78
    with ControlStore(authority / "control.sqlite3").read_transaction() as tx:
        before = dict(tx.execute("SELECT * FROM context_run_material").fetchone())
        workspace = Path(tx.execute("SELECT path FROM context_workspaces WHERE parent_preparation_id IS NULL").fetchone()[0])
    if change == "removed":
        (primary / "src/selected.sh").unlink()
    else:
        (primary / "src/selected.sh").write_bytes(b"later source edit\n")
        if change == "head-ahead":
            _git("add", "src/selected.sh", cwd=primary)
            _git("commit", "-qm", "fixture later HEAD", cwd=primary)
    env.update(FFS_REQUEST_KEY="frontend-resume", GSD_RESUME="1")
    result = _run(primary, env, "fix", "--select-file", "src/selected.sh")
    assert result.returncode == 78, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["code"] == "HOST_CAPABILITY_UNQUALIFIED"
    assert (workspace / "src/selected.sh").read_bytes() == b"retained edit\n"
    with ControlStore(authority / "control.sqlite3").read_transaction() as tx:
        assert dict(tx.execute("SELECT * FROM context_run_material").fetchone()) == before
        assert tx.execute("SELECT COUNT(*) FROM authority_launch_intents").fetchone()[0] == 0


@pytest.mark.parametrize("cached", [True, False])
@pytest.mark.parametrize("change", ["selection", "runtime"])
def test_frontend_resume_conflict_precedence_is_bound_to_request_key(tmp_path, cached, change):
    primary, authority, _, env = _setup(tmp_path)
    assert _run(primary, env, "fix", "--select-file", "src/selected.sh").returncode == 78
    env["GSD_RESUME"] = "1"
    if not cached:
        env["FFS_REQUEST_KEY"] = "new-resume-key"
    selected = "src/selected.sh"
    if change == "selection":
        selected = "src/unrelated.txt"
    else:
        descriptor = tmp_path / "equivalent-runtime.json"
        descriptor.write_bytes(Path(env["FFS_UPSTREAM_RUNTIME_MANIFEST"]).read_bytes() + b"\n")
        env["FFS_UPSTREAM_RUNTIME_MANIFEST"] = str(descriptor)
        env["FFS_UPSTREAM_RUNTIME_SHA256"] = hashlib.sha256(descriptor.read_bytes()).hexdigest()
    before = _tree(authority)
    result = _run(primary, env, "fix", "--select-file", selected)
    expected = "IDEMPOTENCY_CONFLICT" if cached else (
        "UPSTREAM_CHANGED" if change == "selection" else "UPSTREAM_RUNTIME_CHANGED"
    )
    assert result.returncode == (2 if cached else 3), (result.stdout, result.stderr)
    assert json.loads(result.stdout)["code"] == expected
    assert _tree(authority) == before


def test_frontend_resume_without_selected_identity_does_not_mint_or_write(tmp_path):
    primary, authority, _, env = _setup(tmp_path)
    env.pop("GSD_RUN_ID")
    env["GSD_RESUME"] = "1"
    before = _tree(authority)
    result = _run(primary, env)
    assert result.returncode == 3, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["code"] == "RUN_NOT_FOUND"
    assert _tree(authority) == before


@pytest.mark.parametrize("new_key", [False, True])
def test_frontend_invocation_text_cannot_change_unfinished_work(tmp_path, new_key):
    primary, authority, _, env = _setup(tmp_path)
    env["FFS_INVOCATION_TEXT"] = "014 --autonomous"
    assert _run(primary, env, "feature-implement").returncode == 78
    env["FFS_INVOCATION_TEXT"] = "--adhoc 'different work' --autonomous"
    if new_key:
        env.update(FFS_REQUEST_KEY="new-invocation", GSD_RESUME="1")
    result = _run(primary, env, "feature-implement")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["code"] == "IDEMPOTENCY_CONFLICT"
    with ControlStore(authority / "control.sqlite3").read_transaction() as tx:
        assert tx.execute("SELECT COUNT(*) FROM authority_launch_intents").fetchone()[0] == 0
        row = tx.execute("SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id WHERE k.idempotency_key='frontend-operation'").fetchone()
        assert json.loads(row["payload"])["data"]["invocation_text"] == "014 --autonomous"
