"""Production entrypoints drive the sealed frontend lifecycle end to end.

The host executable is a Python telemetry fixture standing in for a qualified
Codex CLI; the four-probe qualification and private staging are the same
fixture seams ``test_managed_codex_dispatch`` uses.  Every launch still crosses
the real ControlStore/Supervisor authority through the real CLI entry
(``managed-start`` / ``frontend-start``).  This is fixture proof of the
production assembly, not native host qualification.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

requires_local_confinement = pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.isfile("/usr/bin/sandbox-exec"),
    reason="needs Darwin sandbox-exec local-check confinement (non-Darwin fails closed; covered by test_local_check_transport)",
)

import host_capabilities
from host_capabilities import QualifiedCodexRuntime, TELEMETRY_SCHEMA, _binary_chain
from process_identity import ProcessIdentity
from run_state import cli
from run_state.state import ControlStore
import run_state.managed_qualification as managed_qualification
import run_state.runtime_staging as runtime_staging
from test_m4_upstream_context_acceptance import (
    _env, _git, _manifest, _register, _registered_runtime, _repository, _write_manifest,
)

_REVIEW = '''import json, os, pathlib, sys, time
prompt = sys.argv[-1]
credential = pathlib.Path(os.environ["CODEX_HOME"]) / "auth.json"
if prompt.startswith("Artifact-only review request:"):
    data = json.loads(prompt.split("\\n", 1)[1])
    contract, context = data["output_contract"], data["review_context"]
    check = next(iter(context["checks"].values()))["evidence"][0]
    criteria = {cid: {"status": "passed", "evidence": [{"id": rid, **check}
                for rid in spec["required_evidence_ids_for_pass"]]} for cid, spec in contract["criteria"].items()}
    text = json.dumps({**contract["fixed_fields"], "criteria": criteria, "findings": []}, sort_keys=True, separators=(",", ":"))
    assert credential.is_file()
    sessions = credential.parent / "sessions"
    sessions.mkdir()
    records = [{"type":"session_meta","payload":{"id":"fixture-thread","cwd":os.getcwd()}},
               {"type":"turn_context","payload":{"cwd":os.getcwd(),"model":"gpt-5.6-terra","effort":"medium","turn_id":"fixture-turn"}},
               {"type":"response_item","payload":{"type":"message","role":"assistant"}}]
    (sessions / "rollout-fixture-thread.jsonl").write_text("".join(json.dumps(r)+"\\n" for r in records))
    print(json.dumps({"type":"thread.started","thread_id":"fixture-thread"}), flush=True)
    for _ in range(100):
        if not credential.exists(): break
        time.sleep(.01)
    else: raise SystemExit(21)
    print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":text}}), flush=True)
    print(json.dumps({"type":"turn.completed","usage":{"input_tokens":3,"cached_input_tokens":0,"output_tokens":4,"cache_write_input_tokens":0,"reasoning_output_tokens":1}}), flush=True)
    raise SystemExit(0)
print(json.dumps({"type":"thread.started","thread_id":"managed-thread"}), flush=True)
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":prompt}}), flush=True)
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":7,"cached_input_tokens":2,"cache_write_input_tokens":1,"output_tokens":3,"reasoning_output_tokens":2}}), flush=True)
'''


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()






def _setup(tmp_path):
    primary = _repository(tmp_path)
    phase = primary / ".planning" / "phases" / "01-fixture"
    phase.mkdir(parents=True, exist_ok=True)
    (phase / "01-01-PLAN.md").write_text("---\nphase: 01\nplan: 01\n---\nPlan\n")
    (primary / "src" / "input.txt").write_bytes(b"base-input\n")
    _git("add", ".planning/phases", "src/input.txt", cwd=primary)
    _git("commit", "-qm", "fixture frozen plan", cwd=primary)
    # The reviewable candidate is the selected dirty overlay, never the untouched base tree.
    selected = b"base-input\nselected edit\n"
    (primary / "src" / "input.txt").write_bytes(selected)
    authority = tmp_path / "authority"
    repository_id = _register(primary, authority)
    selection = _write_manifest(tmp_path, _manifest(
        primary, repository_id, selected=selected,
        upstream={"project": None, "workstream": None, "session_key": "ingress-session"},
    ))
    runtime, digest = _registered_runtime()
    env = _env(tmp_path)
    env.update(
        PATH=str(Path(sys.executable).parent) + os.pathsep + env["PATH"],
        FFS_SELECTION_MANIFEST=str(selection), FFS_STATE_ROOT=str(authority),
        FFS_UPSTREAM_RUNTIME_MANIFEST=str(runtime), FFS_UPSTREAM_RUNTIME_SHA256=digest,
    )
    return primary, authority, repository_id, env


def _fixture_host(tmp_path, monkeypatch):
    """Qualified-runtime fixture seams shared with the Codex dispatch regression."""
    runtime = tmp_path / "private-runtime"
    runtime.mkdir(mode=0o700)
    for directory in ("skills", "agents"):
        (runtime / directory).mkdir(mode=0o700)
        (runtime / directory / "fixture").write_text(directory)
    (runtime / "config.toml").write_text("qualified fixture\n")
    (runtime / "hooks.json").write_text("{}\n")
    (runtime / "auth.json").write_text('{"fixture":"dummy"}\n')
    (runtime / "auth.json").chmod(0o600)
    fake = tmp_path / "qualified-codex"
    fake.write_text(f"#!{sys.executable}\n" + _REVIEW)
    fake.chmod(0o700)
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps({"models": [{"slug": "gpt-5.6-terra"}]}))

    def stage(_template, home, worktree):
        home = Path(home)
        if not home.exists():
            home.mkdir(mode=0o700)
            (home / "config.toml").write_text("qualified fixture\n")
            (home / "hooks.json").write_text("{}\n")
            (home / "auth.json").write_text('{"fixture":"dummy"}\n')
            (home / "auth.json").chmod(0o600)
            (home / runtime_staging.STAGE_MANIFEST_NAME).write_text(json.dumps({
                "target": {"home": {"path": str(home)}, "workspace": {"path": str(worktree)}},
            }) + "\n")
        return json.loads((home / runtime_staging.STAGE_MANIFEST_NAME).read_text())

    retained = {}

    def qualify(store, token, *, activity_id, activity_request_key, parent_activity_id,
                workspace, runtime_home, binary, gsd_environment, host_request, role,
                evidence_root, final_contract_hash, supervisor, observer_module=None):
        del evidence_root, supervisor, observer_module
        home, worktree = Path(runtime_home), workspace.path
        admission = {
            "schema": "ffs.supervisor-admission/v1", "available": True,
            "repository_id": token.repository_id, "run_id": token.run_id,
            "activity_id": activity_id, "generation": token.generation,
            "workspace": str(worktree), "runtime_identity": "pending",
        }
        Path(gsd_environment.admission_file).write_text(json.dumps(admission) + "\n")
        Path(gsd_environment.admission_file).chmod(0o600)
        if activity_id in retained:
            # Replay: the retained observation yields the same qualified tuple and receipt.
            qualified, receipt = retained[activity_id]
            return SimpleNamespace(activity=store.get_activity(activity_id), qualified_runtime=qualified,
                                   runtime_receipt=receipt)
        rinfo, winfo = home.stat(), worktree.stat()
        principal = ProcessIdentity.current()
        runtime_value = {
            "path": str(home), "device": rinfo.st_dev, "inode": rinfo.st_ino,
            "config_sha256": _digest(home / "config.toml"),
            "hooks_sha256": _digest(home / "hooks.json"),
            "skills_sha256": hashlib.sha256(b"skills").hexdigest(),
            "agents_sha256": hashlib.sha256(b"agents").hexdigest(),
            "gsd_core_sha256": hashlib.sha256(b"core").hexdigest(),
            "scripts_sha256": hashlib.sha256(b"scripts").hexdigest(),
            "gsd_manifest_sha256": hashlib.sha256(b"manifest").hexdigest(),
        }
        policy = host_capabilities.codex_closed_environment(
            home, home / "ffs-codex-policy-tmp", fake, _binary_chain(fake), gsd_environment,
        )
        qualified = QualifiedCodexRuntime(
            binary=tuple(sorted(_binary_chain(fake).items())),
            runtime=tuple(sorted(runtime_value.items())),
            workspace=tuple(sorted({"path": str(worktree), "device": winfo.st_dev, "inode": winfo.st_ino}.items())),
            supervisor=tuple(sorted({"host_id": principal.host_id, "boot_id": principal.boot_id,
                                     "pid": principal.pid, "start_token": principal.start_token}.items())),
            execution=tuple(sorted({"model": host_request.model, "effort": host_request.effort,
                                    "sandbox": host_request.sandbox,
                                    "network_enabled": host_request.network_enabled,
                                    "roots": [str(worktree)],
                                    "disabled_features": list(host_capabilities.DISABLED_NATIVE_FEATURES)}.items())),
            observation=tuple(sorted({"id": "a" * 32, "created_at_unix": time.time(),
                                      "environment_sha256": host_capabilities.codex_environment_policy_hash(policy),
                                      "telemetry_schema": TELEMETRY_SCHEMA}.items())),
        )
        runtime_identity = store.runtime_tuple_hash(qualified)
        admission["runtime_identity"] = runtime_identity
        Path(gsd_environment.admission_file).write_text(json.dumps(admission) + "\n")
        Path(gsd_environment.admission_file).chmod(0o600)
        with store.transaction() as tx:
            tx.execute("UPDATE context_workspaces SET child_role=? WHERE preparation_id=?", (role, workspace.id))
        activity = store.create_child_activity(
            token, parent_activity_id=parent_activity_id, role=role,
            request_key=activity_request_key, candidate_hash=workspace.input_digest,
            contract_hash=final_contract_hash, runtime_identity=runtime_identity,
            workspace_binding=str(worktree), workspace_preparation_id=workspace.id,
            retry_budget=2, activity_id=activity_id,
        )
        activity = store.transition_activity(token, activity.id, expected="pending", new="active",
                                             reason="fixture qualified")
        receipt = store.commit_runtime_receipt(token, activity.id, qualified)
        retained[activity_id] = (qualified, receipt)
        return SimpleNamespace(activity=activity, qualified_runtime=qualified, runtime_receipt=receipt)

    monkeypatch.setattr(runtime_staging, "stage_or_reuse_private_codex_runtime", stage)
    monkeypatch.setattr(managed_qualification, "qualify_managed_runtime", qualify)
    monkeypatch.setattr(host_capabilities, "admit_cli", lambda _binary: {"version": "0.154.0"})
    import run_state.shared_resources as shared_resources
    from run_state.managed_admission import ManagedAdmissionQueue
    from run_state.resource_observation import ResourceObservation
    monkeypatch.setattr(shared_resources, "ManagedAdmissionQueue", lambda *args, **kwargs: ManagedAdmissionQueue(
        tmp_path / "managed-admission", observation_provider=lambda:
        ResourceObservation(time.monotonic_ns(), 4, 4 << 30, 4 << 30, 100, 100, {"codex": 4}, "fixture")))
    return runtime, fake, catalog


def _draft(tmp_path, *, check: str = "/usr/bin/grep -q base-input src/input.txt", mode=None) -> Path:
    # Sealed criteria must name the run's accepted requirement ids: the ingress
    # binds exactly ``objective:<sha256(objective)>`` for its objective text.
    criterion = "objective:" + hashlib.sha256(b"assembly").hexdigest()
    draft = {
        "draft_id": "assembly", "revision": 1,
        "criteria": [{"id": criterion, "objective_clause": "the input is retained",
                      "checks": [{"id": "input-check", "kind": "command", "locator": check}],
                      "evidence_rules": [{"id": "input-evidence", "kind": "log", "required": True}]}],
        "exclusions": [{"id": "none", "reason": "fixture"}],
        "global_invariants": [{"id": "no-commit", "reason": "fixture"}],
    }
    if mode is not None:
        draft["command_mode"] = mode
    path = tmp_path / "acceptance-draft.json"
    path.write_text(json.dumps(draft))
    return path


def _host_args(runtime, fake, catalog, draft):
    args = ["--host", "codex", "--host-runtime-home", str(runtime), "--host-binary", str(fake),
            "--host-model-request", '{"kind":"tier","name":"execution"}', "--host-sandbox", "workspace-write",
            "--host-network", "disabled", "--host-token-reservation", "100", "--host-timeout", "30",
            "--review-model-catalog", str(catalog)]
    if draft is not None:
        args += ["--acceptance-draft", str(draft)]
    return args


def _managed_start(env, authority, run_id, runtime, fake, catalog, draft, *extra):
    return cli.main([
        "managed-start", "--objective", "assembly", "--state-root", str(authority),
        "--selection-manifest", env["FFS_SELECTION_MANIFEST"],
        "--upstream-runtime-manifest", env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
        "--upstream-runtime-sha256", env["FFS_UPSTREAM_RUNTIME_SHA256"],
        "--request-key", "assembly", "--run-id", run_id, "--dispatch-limit", "8", "--token-limit", "1000",
        *_host_args(runtime, fake, catalog, draft), *extra, "--", "/gsd-plan-phase", "1",
    ])


def _frontend_start(env, authority, run_id, runtime, fake, catalog, draft, frontend, *extra):
    return cli.main([
        "frontend-start", "--frontend", frontend, "--objective", "assembly", "--state-root", str(authority),
        "--upstream-runtime-manifest", env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
        "--upstream-runtime-sha256", env["FFS_UPSTREAM_RUNTIME_SHA256"],
        "--request-key", "assembly", "--run-id", run_id, "--dispatch-limit", "8", "--token-limit", "1000",
        "--select-file", "src/input.txt", *_host_args(runtime, fake, catalog, draft), *extra,
    ])


def _facts(authority, repository_id, run_id):
    store = ControlStore(authority / "control.sqlite3")
    state = store.get_frontend_policy_state(repository_id=repository_id, run_id=run_id)
    with store.read_transaction() as tx:
        outer = tx.execute("SELECT count(*) FROM authority_launch_intents WHERE capacity_exempt=1").fetchone()[0]
        native = tx.execute("SELECT count(*) FROM authority_launch_intents i JOIN authority_child_bindings b "
                            "ON b.activity_id=i.activity_id WHERE b.role='reviewer'").fetchone()[0]
        reviews = tx.execute("SELECT count(*) FROM authority_acceptance_receipts "
                             "WHERE json_extract(receipt_json,'$.role')='review'").fetchone()[0]
        actions = tx.execute("SELECT action,count(*) FROM authority_policy_actions WHERE state<>'cancelled' "
                             "GROUP BY action").fetchall()
    # Sealed-check grants (`check`) are per-candidate bookkeeping; the counts
    # that matter here are the single execution and the single broad review.
    grants = {action: count for action, count in actions if action != "check"}
    return SimpleNamespace(stage=None if state is None else state.stage, outer=outer, native=native,
                           reviews=reviews, actions=grants, store=store)


def _last_code(capsys) -> str:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])["code"]


@requires_local_confinement
@pytest.mark.parametrize("entry", ["managed-start", "frontend-start"])
def test_entrypoint_seals_draft_executes_reviews_natively_and_replays_to_done(tmp_path, monkeypatch, entry):
    primary, authority, repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    for key in ("GSD_RUN_ID", "FFS_RUN_ID", "GSD_RESUME"):
        monkeypatch.delenv(key, raising=False)
    runtime, fake, catalog = _fixture_host(tmp_path, monkeypatch)
    draft = _draft(tmp_path, mode="task-swarm" if entry == "managed-start" else None)
    # The fixture host cannot open a real GSD wave; record that task-swarm demands
    # one and accept it. The unproven case is test_task_swarm_without_a_supervised_wave.
    import run_state.supervisor as supervisor
    wave_checks = []
    monkeypatch.setattr(supervisor, "_gsd_wave_completion_code",
                        lambda *_args, require_wave=True: wave_checks.append(require_wave))
    # Evidence locators are 256-byte identifiers; keep the fixture run id short under pytest's deep tmp path.
    run_id = "ms" if entry == "managed-start" else "fs"
    if entry == "managed-start":
        result = _managed_start(env, authority, run_id, runtime, fake, catalog, draft)
    else:
        result = _frontend_start(env, authority, run_id, runtime, fake, catalog, draft, "task-swarm", "--scope", "1")
    assert result == 0
    assert wave_checks == ([] if entry == "managed-start" else [True])
    facts = _facts(authority, repository_id, run_id)
    assert facts.stage == "DONE"
    assert facts.outer == 1 and facts.native == 1 and facts.reviews == 1
    assert facts.actions == {"execute": 1, "final_review": 1}
    with facts.store.read_transaction() as tx:
        review = json.loads(tx.execute("SELECT receipt_json FROM authority_acceptance_receipts "
                                       "WHERE json_extract(receipt_json,'$.role')='review'").fetchone()[0])
        sealed = tx.execute("SELECT * FROM authority_sealed_acceptances").fetchall()
    assert review["completion_status"] == "succeeded" and len(sealed) == 1
    # Replay the identical request through the same entrypoint (the request digest
    # is part of the idempotency key): terminal stage, no new launch, no new grant.
    if entry == "managed-start":
        replay = _managed_start(env, authority, run_id, runtime, fake, catalog, draft)
    else:
        replay = _frontend_start(env, authority, run_id, runtime, fake, catalog, draft, "task-swarm", "--scope", "1")
    assert replay == 0
    again = _facts(authority, repository_id, run_id)
    assert (again.stage, again.outer, again.native, again.reviews, again.actions) == (
        "DONE", 1, 1, 1, {"execute": 1, "final_review": 1})


def test_task_swarm_without_a_supervised_wave_refuses_before_checks_or_review(tmp_path, monkeypatch, capsys):
    primary, authority, repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    runtime, fake, catalog = _fixture_host(tmp_path, monkeypatch)
    # The fixture host exits 0 after echoing its prompt: no delegated wave.
    result = _frontend_start(env, authority, "nw", runtime, fake, catalog, _draft(tmp_path),
                             "task-swarm", "--scope", "1")
    assert result == 78 and _last_code(capsys) == "WAVE_EXECUTION_UNPROVEN"
    facts = _facts(authority, repository_id, "nw")
    assert facts.stage != "DONE" and facts.outer == 1 and facts.native == 0 and facts.reviews == 0


@pytest.mark.parametrize(("resume", "code"), [("0", "ACCEPTANCE_DRAFT_REQUIRED"), ("1", "RUN_NOT_FOUND")])
def test_gsd_resume_zero_is_a_fresh_start_like_the_legacy_runner(tmp_path, monkeypatch, capsys, resume, code):
    primary, authority, repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    for key in ("GSD_RUN_ID", "FFS_RUN_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GSD_RESUME", resume)
    runtime, fake, catalog = _fixture_host(tmp_path, monkeypatch)
    # No --run-id: only a resume request needs the retained run identity.
    result = cli.main([
        "frontend-start", "--frontend", "task-swarm", "--objective", "assembly", "--state-root", str(authority),
        "--upstream-runtime-manifest", env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
        "--upstream-runtime-sha256", env["FFS_UPSTREAM_RUNTIME_SHA256"],
        "--request-key", "assembly", "--dispatch-limit", "8", "--token-limit", "1000",
        "--select-file", "src/input.txt", "--scope", "1", *_host_args(runtime, fake, catalog, None),
    ])
    assert result != 0 and _last_code(capsys) == code


def test_frontend_start_without_a_draft_refuses_before_any_outer_launch(tmp_path, monkeypatch, capsys):
    primary, authority, repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    runtime, fake, catalog = _fixture_host(tmp_path, monkeypatch)
    result = _frontend_start(env, authority, "nd", runtime, fake, catalog, None, "task-swarm", "--scope", "1")
    assert result == 78 and _last_code(capsys) == "ACCEPTANCE_DRAFT_REQUIRED"
    facts = _facts(authority, repository_id, "nd")
    assert facts.outer == 0 and facts.native == 0 and facts.actions == {}


@requires_local_confinement
def test_failed_sealed_check_without_a_repair_producer_hands_back_and_refuses_truthfully(tmp_path, monkeypatch, capsys):
    primary, authority, repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    runtime, fake, catalog = _fixture_host(tmp_path, monkeypatch)
    draft = _draft(tmp_path, check="/usr/bin/grep -q absent-marker src/input.txt", mode="task-swarm")
    result = _managed_start(env, authority, "fl", runtime, fake, catalog, draft)
    assert result == 78 and _last_code(capsys) == "RECOVERY_PRODUCER_UNAVAILABLE"
    facts = _facts(authority, repository_id, "fl")
    assert facts.stage == "RECOVER" and facts.outer == 1 and facts.native == 0 and facts.reviews == 0
    assert facts.actions == {"execute": 1}
    replay = _managed_start(env, authority, "fl", runtime, fake, catalog, draft)
    assert replay == 78 and _last_code(capsys) == "RECOVERY_PRODUCER_UNAVAILABLE"
    again = _facts(authority, repository_id, "fl")
    assert (again.stage, again.outer, again.native, again.actions) == ("RECOVER", 1, 0, {"execute": 1})


def test_planning_frontend_without_a_phase_scope_refuses_before_any_outer_launch(tmp_path, monkeypatch, capsys):
    primary, authority, repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    runtime, fake, catalog = _fixture_host(tmp_path, monkeypatch)
    draft = _draft(tmp_path)
    result = _frontend_start(env, authority, "fx", runtime, fake, catalog, draft, "fix")
    assert result == 78 and _last_code(capsys) == "PRELAUNCH_PHASE_SCOPE_REQUIRED"
    facts = _facts(authority, repository_id, "fx")
    assert facts.outer == 0 and facts.native == 0 and facts.actions == {}


def test_legacy_managed_start_without_a_seal_executes_once_as_before(tmp_path, monkeypatch):
    primary, authority, repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    runtime, fake, catalog = _fixture_host(tmp_path, monkeypatch)
    result = _managed_start(env, authority, "lg", runtime, fake, catalog, None)
    assert result == 0
    facts = _facts(authority, repository_id, "lg")
    assert facts.stage is None and facts.outer == 1 and facts.native == 0 and facts.actions == {"execute": 1}
