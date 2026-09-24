from __future__ import annotations

from collections import namedtuple
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import hashlib

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import host_capabilities as host
from process_identity import ProcessIdentity
from run_state.host_request import CodexHostRequest
import run_state.managed_qualification as managed
from run_state.runtime_staging import stage_private_codex_runtime
from run_state.workspace import WorkspacePreparation
from test_codex_runtime_observer import _runtime, observer
from test_qualification_launch_authority import (
    PROBES, _publish, _qualified, _qualification_store, _reserve,
)


def _gsd(tmp_path: Path) -> host.GsdSupervisorEnvironment:
    bridge = tmp_path / "gsd_wave_bridge.py"
    bridge.write_text("# identity only\n")
    return host.GsdSupervisorEnvironment(
        "ffs-supervised-process", "patches", str(tmp_path / "admission.json"),
        json.dumps([str(bridge)], separators=(",", ":")),
    )


def test_frozen_plan_previews_exact_verified_runtime_after_publication(tmp_path, monkeypatch):
    runtime, binary = _runtime(tmp_path)
    (runtime / "auth.json").write_text("{}")
    (runtime / "auth.json").chmod(0o600)
    registered = {name: [] for name in host.REQUIRED_HOOK_EVENTS}
    (runtime / "hooks.json").write_text(json.dumps({"hooks": registered}))
    worktree = tmp_path / "work"
    worktree.mkdir()
    additions = _gsd(tmp_path)
    identity = {"host_id": "h", "boot_id": "b", "pid": 7, "start_token": "s"}
    monkeypatch.setattr(host, "current_supervisor_identity", lambda: identity)
    seed = observer.prepare_qualification_seed(runtime)
    preview_plan = observer.prepare_qualification_plan(
        runtime, binary, worktree, runtime / "runtime-observation.json", 10,
        model="gpt-6-astra", effort="high", gsd_environment=additions,
        seed=seed, preview=True,
    )
    predicted = observer.preview_qualification_runtime(
        seed, runtime, binary, worktree, model="gpt-6-astra", effort="high",
        gsd_environment=additions,
    )
    assert type(managed._qualified(predicted)) is host.QualifiedCodexRuntime
    assert not Path(additions.admission_file).exists()
    Path(additions.admission_file).write_text("{}")
    Path(additions.admission_file).chmod(0o600)
    admission_inode = Path(additions.admission_file).stat().st_ino
    admission_content = Path(additions.admission_file).read_bytes()
    plan = observer.prepare_qualification_plan(
        runtime, binary, worktree, runtime / "runtime-observation.json", 10,
        model="gpt-6-astra", effort="high", gsd_environment=additions, seed=seed,
    )
    assert [(item.argv, item.environment) for item in plan.probes] == [
        (item.argv, item.environment) for item in preview_plan.probes
    ]
    assert observer.preview_qualified_runtime(plan).to_dict() == predicted.to_dict()
    stream = '\n'.join(json.dumps(value) for value in (
        {"type": "thread.started", "thread_id": "fixture"},
        {"type": "turn.completed"},
    ))
    results = tuple(observer.QualificationResult(probe.name, stream, "", 0)
                    for probe in plan.probes)
    record = observer.publish_qualification_results(plan, results)
    assert record["observation"]["created_at_unix"] == plan.observation_created_at_unix
    monkeypatch.setattr(host, "_parse_toml", lambda _text: host._runtime_policy(
        worktree, "workspace-write", False, [str(worktree)],
    ))
    monkeypatch.setattr(host, "_require_observation", lambda *args, **kwargs: record)
    verified = host.verify_runtime(
        runtime, worktree, roots=[str(worktree)], binary=str(binary),
        model="gpt-6-astra", effort="high",
    )
    assert verified.to_dict() == predicted.to_dict()
    assert Path(additions.admission_file).stat().st_ino == admission_inode
    assert Path(additions.admission_file).read_bytes() == admission_content


class _Store:
    def get_run_policy_budget(self, **_kwargs):
        return None

    def __init__(self):
        self.promotions = []
        self.receipts = []
        self.events = {}
        self.binding = None
        self.completions = {}

    @contextmanager
    def read_transaction(self):
        store = self
        class Tx:
            def execute(self, sql, values):
                if "authority_event_keys" in sql:
                    payload = store.events.get(values)
                    row = None if payload is None else {"payload": json.dumps({
                        "run_id": "run", "activity_id": values[0], "data": payload,
                    })}
                elif "authority_qualification_launches" in sql:
                    row = store.completions.get(values)
                elif "authority_child_bindings" in sql:
                    row = store.binding
                else:
                    raise AssertionError(sql)
                return SimpleNamespace(fetchone=lambda: row)
        yield Tx()

    def record_event_once(self, _token, activity_id, key, payload):
        self.events[(activity_id, key)] = dict(payload)
        return {"payload": payload}

    def runtime_tuple_hash(self, _qualified):
        return "7" * 64

    def create_child_activity(self, _token, **kwargs):
        self.created = kwargs
        self.binding = {**kwargs, "role": kwargs["role"], "request_key": kwargs["request_key"],
                        "runtime_tuple_hash": kwargs["runtime_identity"]}
        return SimpleNamespace(id=kwargs["activity_id"], state="pending")

    def get_activity(self, activity_id):
        return SimpleNamespace(id=activity_id, state="active")

    def promote_qualified_activity(self, _token, activity_id, **kwargs):
        self.promotions.append((activity_id, kwargs))
        self.binding.update({"role": kwargs["role"],
                             "contract_hash": kwargs["final_contract_hash"],
                             "runtime_identity": kwargs["runtime_identity"],
                             "runtime_tuple_hash": kwargs["runtime_identity"]})
        return SimpleNamespace(id=activity_id, state="pending")

    def transition_activity(self, _token, activity_id, *, expected, new, reason):
        assert expected == "pending" and new == "active" and reason
        return SimpleNamespace(id=activity_id, state="active")

    def commit_runtime_receipt(self, _token, activity_id, qualified):
        receipt = SimpleNamespace(receipt_sha256="8" * 64)
        self.receipts.append((activity_id, qualified))
        return receipt


def _fixture(tmp_path: Path, *, uncertain_at: int | None = None):
    workspace_path = (tmp_path / "workspace").resolve()
    workspace_path.mkdir()
    source = tmp_path / ".codex"
    skills = tmp_path / ".agents" / "skills" / "gsd-quick"
    for directory in (source / "agents", source / "gsd-core", source / "scripts", source / "hooks", skills):
        directory.mkdir(parents=True, exist_ok=True)
    files = {
        "agents/gsd-executor.toml": b"name = \"gsd-executor\"\n",
        "gsd-core/VERSION": b"1.14.0\n",
        "scripts/gsd-run.sh": b"#!/bin/sh\nexit 0\n",
        "hooks/gsd-hook.js": b"#!/usr/bin/env node\n",
        "skills/gsd-quick/SKILL.md": b"# quick\n",
    }
    for relative, data in files.items():
        root = source if not relative.startswith("skills/") else tmp_path / ".agents"
        path = root / relative
        path.write_bytes(data)
        if relative.endswith((".sh", ".js")):
            path.chmod(0o755)
    (source / "hooks.json").write_text("{}\n")
    (source / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
        "tokens": {"id_token": "fixture-id", "access_token": "fixture",
                   "refresh_token": "excluded-refresh", "account_id": "fixture-account"},
        "last_refresh": "fixture",
    }) + "\n")
    (source / "auth.json").chmod(0o600)
    owned = {relative: hashlib.sha256(data).hexdigest() for relative, data in files.items()}
    (source / "gsd-file-manifest.json").write_text(json.dumps({"version": "1.14.0", "files": owned}))
    runtime = tmp_path / "runtime"
    stage_private_codex_runtime(source, runtime, workspace_path)
    binary = (tmp_path / "codex").resolve()
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)
    evidence = (tmp_path / "evidence").resolve()
    evidence.mkdir()
    gsd = _gsd(tmp_path)
    token = SimpleNamespace(repository_id="repo", run_id="run", generation=3)
    workspace = WorkspacePreparation(
        id="prep", run_id="run", repository_id="repo", path=workspace_path,
        branch="ffs/child", base_commit="a" * 40, repository_path=workspace_path,
        state="ready", ready=True, generation=3, selected_manifest_hash="b" * 64,
        input_digest="c" * 64, selected_manifest_json="{}",
        path_existed_before=False, branch_existed_before=False,
        registered_before=False, created_by_ffs=True, parent_activity_id="parent",
        child_role="inventory", child_request_key="plan-key",
    )
    request = CodexHostRequest(
        str(runtime.resolve()), str(binary), "gpt-6-astra", "high",
        "workspace-write", False, 5, 30,
    )
    base = host.codex_closed_environment(
        runtime.resolve(), runtime / ".ffs-codex-policy-tmp", binary,
        {"launcher_sha256": host._digest(binary)},
    )
    base.update(gsd.as_dict())
    Probe = namedtuple("Probe", "name argv environment timeout_seconds")
    probes = tuple(Probe(name, (str(binary), name), tuple(sorted({
        **base, "TMPDIR": str(workspace_path / ".ffs-observer-tmp"),
        "FFS_HOOK_OBSERVATION": str(runtime / "observer-hooks.log"),
        "FFS_HOOK_NONCE": "1" * 32,
    }.items())), 10) for name in managed._PROBES)
    plan = SimpleNamespace(
        probes=probes, policy_environment=tuple(sorted(base.items())),
        runtime_identity='{"runtime":"fixture"}',
        binary_identity='{"binary":"fixture"}', workspace_identity='{"workspace":"fixture"}',
    )
    predicted = host.QualifiedCodexRuntime(
        binary=(("launcher_sha256", "1" * 64),), runtime=(("path", str(runtime)),),
        workspace=(("path", str(workspace_path)),),
        supervisor=(("host_id", "h"),), execution=(("model", "gpt-6-astra"),),
        observation=(("environment_sha256", host.preview_gsd_codex_environment_policy_hash(base)),
                     ("id", "1" * 32)),
    )
    Result = namedtuple("QualificationResult", "name stdout stderr exit_code")

    class Observer:
        QualificationResult = Result
        QualificationSeed = namedtuple("QualificationSeed", "nonce skill_token observation_created_at_unix")

        @staticmethod
        def prepare_qualification_seed(_runtime):
            return Observer.QualificationSeed("1" * 32, "token", 1000.0)

        @staticmethod
        def prepare_observer_skill(_runtime):
            return "token"

        @staticmethod
        def preview_qualification_runtime(*args, **kwargs):
            return predicted

        @staticmethod
        def prepare_qualification_plan(*args, **kwargs):
            return plan

        @staticmethod
        def preview_qualified_runtime(_plan):
            return predicted

        @staticmethod
        def publish_qualification_results(_plan, results):
            assert tuple(item.name for item in results) == managed._PROBES
            observation = {"schema": "fixture", "results": len(results)}
            (runtime / "runtime-observation.json").write_text(json.dumps(observation))
            return observation

    store = _Store()

    class Supervisor:
        def __init__(self):
            self.store, self.token, self.evidence_root = store, token, evidence
            self.launched = []

        def launch_qualification(self, dispatch, *, qualification_contract):
            self.launched.append((dispatch, qualification_contract))
            return SimpleNamespace(request=dispatch, ordinal=len(self.launched))

        def finish(self, handle, *, timeout):
            root = evidence / str(handle.ordinal)
            root.mkdir()
            streams = {}
            for name, value in (("stdout", b"{}\n"), ("stderr", b"")):
                path = root / (name + ".log")
                path.write_bytes(value)
                streams[name] = {"locator": str(path), "sha256": managed.hashlib.sha256(value).hexdigest(),
                                 "bytes": len(value)}
            material = handle.request.qualification_material
            receipt = {
                "schema": "ffs.codex-qualification-invocation/v1",
                "probe_name": material.probe_name,
                "contract_sha256": material.contract_sha256,
                "envelope_sha256": material.envelope_sha256,
                "runtime_template_sha256": material.runtime_template_sha256,
            }
            if uncertain_at == handle.ordinal:
                receipt["status"] = "uncertain"
            result = {"returncode": 0, "streams": streams, "host_receipt": receipt}
            result_path = root / "result.json"
            encoded = json.dumps(result, sort_keys=True).encode()
            result_path.write_bytes(encoded)
            store.completions[(handle.request.activity_id, handle.request.request_key)] = {
                "state": "uncertain" if uncertain_at == handle.ordinal else "completed_succeeded",
                "completion_evidence_json": json.dumps({
                    "locator": str(result_path),
                    "sha256": managed.hashlib.sha256(encoded).hexdigest(),
                }),
            }
            return result

    return store, token, workspace, runtime.resolve(), binary, gsd, request, evidence, Supervisor(), Observer, predicted


def test_helper_launches_only_four_supervised_probes_then_promotes_and_receipts(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path)
    store, token, workspace, runtime, binary, gsd, request, evidence, supervisor, module, predicted = fixture
    monkeypatch.setattr(managed, "verify_runtime", lambda *args, **kwargs: predicted)
    bundle = managed.qualify_managed_runtime(
        store, token, activity_id="11111111-1111-4111-8111-111111111111",
        activity_request_key="plan-key",
        parent_activity_id="parent", workspace=workspace,
        runtime_home=runtime, binary=binary, gsd_environment=gsd,
        host_request=request, role="worker", evidence_root=evidence,
        final_contract_hash="9" * 64, supervisor=supervisor, observer_module=module,
    )
    assert [item[0].qualification_material.probe_name for item in supervisor.launched] == list(managed._PROBES)
    assert store.created["role"] == "inventory"
    assert store.created["activity_id"] == "11111111-1111-4111-8111-111111111111"
    assert store.created["retry_budget"] == 5
    assert store.promotions[0][1]["role"] == "worker"
    assert store.receipts == [(bundle.activity.id, predicted)]
    assert json.loads(Path(gsd.admission_file).read_text()) == bundle.admission


def test_qualification_leaves_no_probe_scratch_in_the_worktree(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path)
    store, token, workspace, runtime, binary, gsd, request, evidence, supervisor, module, predicted = fixture
    monkeypatch.setattr(managed, "verify_runtime", lambda *args, **kwargs: predicted)
    scratch = workspace.path / ".ffs-observer-tmp"
    planned = module.prepare_qualification_plan

    def plan(*args, **kwargs):
        # The real observer creates the probes' TMPDIR inside the worktree on every plan.
        scratch.mkdir(exist_ok=True)
        (scratch / "probe-temp").write_text("x")
        return planned(*args, **kwargs)

    monkeypatch.setattr(module, "prepare_qualification_plan", plan)
    kwargs = dict(
        activity_id="55555555-5555-4555-8555-555555555555", activity_request_key="plan-key",
        parent_activity_id="parent", workspace=workspace, runtime_home=runtime, binary=binary,
        gsd_environment=gsd, host_request=request, role="worker", evidence_root=evidence,
        final_contract_hash="9" * 64, supervisor=supervisor, observer_module=module,
    )
    managed.qualify_managed_runtime(store, token, **kwargs)
    # A leftover untracked directory would change the later mapped-check input digest.
    assert not scratch.exists()
    managed.qualify_managed_runtime(
        store, token, **{**kwargs, "workspace": replace(workspace, child_role="worker")},
    )
    assert not scratch.exists()


def test_helper_stops_on_uncertain_probe_without_observation_or_promotion(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path, uncertain_at=2)
    store, token, workspace, runtime, binary, gsd, request, evidence, supervisor, module, predicted = fixture
    monkeypatch.setattr(managed, "verify_runtime", lambda *args, **kwargs: predicted)
    with pytest.raises(managed.ManagedQualificationRefused, match="QUALIFICATION_UNCERTAIN"):
        managed.qualify_managed_runtime(
            store, token, activity_id="22222222-2222-4222-8222-222222222222",
            activity_request_key="plan-key",
            parent_activity_id="parent", workspace=workspace,
            runtime_home=runtime, binary=binary, gsd_environment=gsd,
            host_request=request, role="reviewer", evidence_root=evidence,
            final_contract_hash="9" * 64, supervisor=supervisor, observer_module=module,
        )
    assert len(supervisor.launched) == 2
    assert store.promotions == []
    assert not (runtime / "runtime-observation.json").exists()


def test_replay_after_activity_before_publish_reuses_seed_activity_and_plan_key(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path)
    store, token, workspace, runtime, binary, gsd, request, evidence, supervisor, module, predicted = fixture
    monkeypatch.setattr(managed, "verify_runtime", lambda *args, **kwargs: predicted)
    publish = managed._publish_admission
    monkeypatch.setattr(managed, "_publish_admission", lambda *_args: (_ for _ in ()).throw(RuntimeError("crash")))
    kwargs = dict(
        activity_id="33333333-3333-4333-8333-333333333333", activity_request_key="plan-key",
        parent_activity_id="parent", workspace=workspace, runtime_home=runtime, binary=binary,
        gsd_environment=gsd, host_request=request, role="worker", evidence_root=evidence,
        final_contract_hash="9" * 64, supervisor=supervisor, observer_module=module,
    )
    with pytest.raises(RuntimeError, match="crash"):
        managed.qualify_managed_runtime(store, token, **kwargs)
    retained = dict(store.events[("parent", "qualification-preparation:" + kwargs["activity_id"])])
    assert store.created["request_key"] == "plan-key"
    assert not Path(gsd.admission_file).exists()
    monkeypatch.setattr(managed, "_publish_admission", publish)
    bundle = managed.qualify_managed_runtime(store, token, **kwargs)
    assert store.events[("parent", "qualification-preparation:" + kwargs["activity_id"])] == retained
    assert bundle.activity.id == kwargs["activity_id"]
    admission_inode = Path(gsd.admission_file).stat().st_ino
    launched = len(supervisor.launched)
    replay = managed.qualify_managed_runtime(
        store, token, **{**kwargs, "workspace": replace(workspace, child_role="worker")},
    )
    assert replay.runtime_receipt.receipt_sha256 == bundle.runtime_receipt.receipt_sha256
    assert len(supervisor.launched) == launched
    assert Path(gsd.admission_file).stat().st_ino == admission_inode
    (runtime / "runtime-observation.json").unlink()
    with pytest.raises(managed.ManagedQualificationRefused, match="QUALIFICATION_RESULT_INVALID"):
        managed.qualify_managed_runtime(
            store, token, **{**kwargs, "workspace": replace(workspace, child_role="worker")},
        )


def test_helper_refuses_hand_shaped_stage_manifest_before_authority_or_launch(tmp_path):
    fixture = _fixture(tmp_path)
    store, token, workspace, runtime, binary, gsd, request, evidence, supervisor, module, _ = fixture
    stage = runtime / managed.STAGE_MANIFEST_NAME
    stage.write_text(json.dumps({
        "target": {"home": {"path": str(runtime)},
                   "workspace": {"path": str(workspace.path)}},
    }))
    stage.chmod(0o600)
    with pytest.raises(managed.ManagedQualificationRefused, match="RUNTIME_STAGE_INVALID"):
        managed.qualify_managed_runtime(
            store, token, activity_id="44444444-4444-4444-8444-444444444444",
            activity_request_key="plan-key", parent_activity_id="parent",
            workspace=workspace, runtime_home=runtime, binary=binary,
            gsd_environment=gsd, host_request=request, role="worker",
            evidence_root=evidence, final_contract_hash="9" * 64,
            supervisor=supervisor, observer_module=module,
        )
    assert supervisor.launched == []
    assert store.promotions == []


def test_five_attempt_budget_retains_one_normal_launch_after_promotion(tmp_path):
    store, token, workspace, contracts, hashes, _envelope = _qualification_store(
        tmp_path, dispatch_limit=5,
    )
    with store.transaction() as tx:
        tx.execute(
            "UPDATE authority_activities SET retry_budget=5,remaining_retry_budget=5 "
            "WHERE id='inventory-activity'",
        )
    for index, name in enumerate(PROBES):
        intent = _reserve(store, token, contracts[name], tokens=0)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            ack = store.acknowledge_child(intent.id, token, ProcessIdentity.from_pid(child.pid))
            store.authorize_child(ack, token)
        finally:
            child.terminate()
            child.wait(timeout=10)
        store.complete_launch(
            intent.id, token, status="succeeded",
            evidence=_publish(tmp_path, f"budget-probe-{index}.json", {"probe": name}),
            token_usage=0,
        )
    qualified = _qualified(workspace)
    runtime_identity = store.runtime_tuple_hash(qualified)
    observation = _publish(tmp_path, "budget-observation.json", {"qualified": True})
    store.promote_qualified_activity(
        token, "inventory-activity", qualification_request_key="qualification-wave",
        expected_contract_hashes=hashes, runtime_identity=runtime_identity,
        final_contract_hash="9" * 64, role="worker", observation_evidence=observation,
    )
    receipt = store.commit_runtime_receipt(token, "inventory-activity", qualified)
    production = store.reserve_launch(
        "inventory-activity", token, token_reservation=0,
        request_key="caller-plan-production", request_payload={"managed": True},
        runtime_receipt_sha256=receipt.receipt_sha256,
        managed_input_sha256=contracts["ordinary"]["qualification_envelope"]["candidate_input_sha256"],
    )
    assert production.state == "reserved"
    with store.read_transaction() as tx:
        assert tx.execute(
            "SELECT remaining_retry_budget FROM authority_activities "
            "WHERE id='inventory-activity'",
        ).fetchone()[0] == 0
