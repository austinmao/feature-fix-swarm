"""E8 prerequisite 2: a Claude qualification that crashed resumes by replaying its persisted plan.

Real ``qualify_managed_claude_runtime``, real private staging and the real
qualification plan/publication run against synthetic credentials under
``tmp_path``.  Only the control store and the supervisor are scripted, in the
style of ``test_managed_qualification``: the supervisor never starts a process,
it synthesizes the probe streams, consumes the probe credential copy and records
the completion the way ``Supervisor.finish`` does.  A crash is a ``BaseException``
raised at a chosen seam, so nothing in the code under test can swallow it; the
resume then repeats the identical request from a "new process" (new supervisor
identity) against the files and rows the crash left behind.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from run_state import claude_qualification as qualification
from run_state import claude_runtime_staging as staging
from run_state import managed_claude_qualification as managed
from run_state.claude_host import ClaudeHostRequest
from run_state.ownership import OwnershipRefused
from run_state.state import ControlStore
from run_state.supervisor import SupervisorRefused
from test_claude_qualification_transport import _stream

ACTIVITY = "11111111-1111-4111-8111-111111111111"
PREPARATION_KEY = "qualification-preparation:" + ACTIVITY
HASH_BOUND = ("runtime-stage-manifest.json", "settings.json", ".credentials.json",
              "gsd-file-manifest.json", "qualification.json")
_PROMPT = "Use Bash exactly once with this exact command, then stop: "
CRASH_POINTS = ("after-stage", "before-seed", "after-create", "after-probe-1", "after-probe-2",
                "after-publish", "after-promote", "after-receipt")
UNPERSISTED = ("after-stage", "before-seed")   # no seed row yet: the resume builds the plan afresh


class _Crash(BaseException):
    """An abrupt death: not an Exception, so no handler in the code under test can absorb it."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class _Store:
    """Control-store rows the Claude qualification touches; mutations mirror the real store's idempotency."""

    def __init__(self):
        self.events, self.event_inserts = {}, {}
        self.binding, self.state = None, None
        self.completions, self.reserved = {}, set()
        self.created, self.promotions, self.receipts = [], [], []
        self.crash = set()

    def get_run_policy_budget(self, **_kwargs):
        return None

    @contextmanager
    def read_transaction(self):
        store = self

        class Tx:
            def execute(self, sql, values):
                if "authority_event_keys" in sql:
                    payload = store.events.get(values)
                    row = None if payload is None else {"payload": json.dumps({
                        "run_id": "run", "activity_id": values[0], "data": payload})}
                elif "authority_qualification_launches" in sql:
                    row = store.completions.get(values)
                elif "authority_child_bindings" in sql:
                    row = store.binding
                else:
                    raise AssertionError(sql)
                return SimpleNamespace(fetchone=lambda: row)
        yield Tx()

    def _fire(self, point):
        if point in self.crash:
            self.crash.discard(point)
            raise _Crash(point)

    def record_event_once(self, _token, activity_id, key, payload):
        self._fire("before-seed")
        slot = (activity_id, key)
        if slot in self.events:
            if self.events[slot] != json.loads(json.dumps(payload)):
                raise OwnershipRefused("IDEMPOTENCY_CONFLICT")
            return {"payload": payload}
        self.events[slot] = json.loads(json.dumps(payload))
        self.event_inserts[slot] = self.event_inserts.get(slot, 0) + 1
        return {"payload": payload}

    def create_child_activity(self, _token, **kwargs):
        self.created.append(kwargs)
        fields = ("parent_activity_id", "role", "candidate_hash", "contract_hash", "runtime_identity",
                  "workspace_binding", "workspace_preparation_id", "retry_budget", "request_key")
        if self.binding is not None:
            if any(self.binding.get(name) != kwargs.get(name) for name in fields):
                raise OwnershipRefused("IDEMPOTENCY_CONFLICT")
        else:
            self.binding, self.state = {**kwargs, "runtime_tuple_hash": kwargs["runtime_identity"]}, "pending"
        self._fire("after-create")
        return SimpleNamespace(id=kwargs["activity_id"], state=self.state)

    def get_activity(self, activity_id):
        return SimpleNamespace(id=activity_id, state=self.state)

    def transition_activity(self, _token, activity_id, *, expected, new, reason):
        assert self.state == expected and reason
        self.state = new
        return SimpleNamespace(id=activity_id, state=new)

    @staticmethod
    def runtime_tuple_hash(qualified):
        return ControlStore.runtime_tuple_hash(qualified)

    def promote_qualified_activity(self, _token, activity_id, **kwargs):
        self._fire("after-publish")
        record = {name: kwargs[name] for name in (
            "qualification_request_key", "expected_contract_hashes", "runtime_identity",
            "final_contract_hash", "role", "observation_evidence")}
        if self.promotions:
            if self.promotions[0] != record:
                raise OwnershipRefused("IDEMPOTENCY_CONFLICT")
        else:
            if len(self.completions) != 4:
                raise OwnershipRefused("QUALIFICATION_INCOMPLETE")
            self.promotions.append(record)
            self.binding.update(role=kwargs["role"], contract_hash=kwargs["final_contract_hash"],
                                runtime_identity=kwargs["runtime_identity"],
                                runtime_tuple_hash=kwargs["runtime_identity"])
        self._fire("after-promote")
        return SimpleNamespace(id=activity_id, state=self.state)

    def commit_runtime_receipt(self, _token, activity_id, qualified):
        if self.state != "active":
            raise OwnershipRefused("ACTIVITY_NOT_ACTIVE")
        stable = qualified.to_dict()
        stable["supervisor"] = {key: stable["supervisor"][key] for key in ("host_id", "boot_id")}
        receipt = SimpleNamespace(receipt_sha256=hashlib.sha256(_canonical(stable)).hexdigest())
        self.receipts.append((activity_id, receipt.receipt_sha256))
        self._fire("after-receipt")
        return receipt


class _Supervisor:
    def __init__(self, store, evidence, workspace):
        self.store, self.evidence, self.workspace = store, evidence, workspace
        self.launched, self.finished, self.consumed = [], [], []
        self.outer_launches = 0
        self.crash_after = None

    def launch_managed_outer(self, _request):
        self.outer_launches += 1
        raise AssertionError("qualification must never launch the outer run")

    def launch_qualification(self, request, *, qualification_contract):
        slot = (request.activity_id, request.request_key)
        if slot in self.store.completions:
            raise SupervisorRefused("REQUEST_ALREADY_COMPLETED")
        if slot in self.store.reserved:
            raise SupervisorRefused("INTENT_RECONCILIATION_REQUIRED")
        self.store.reserved.add(slot)
        self.launched.append(request)
        return SimpleNamespace(request=request)

    def _stdout(self, material):
        if material.probe_name == "auth-negative":
            return '{"loggedIn":false}', 1
        command = next((part[len(_PROMPT):] for part in material.argv if part.startswith(_PROMPT)), "")
        return _stream(
            SimpleNamespace(model=material.model, sandbox_command=command),
            SimpleNamespace(session_id=material.session_id),
            hook=material.probe_name == "sandbox-hooks", nested=material.probe_name == "nested-auth",
        ), 0

    def finish(self, handle, *, timeout):
        request = handle.request
        material = request.claude_qualification_material
        self.finished.append(material.probe_name)
        stdout, returncode = self._stdout(material)
        root = self.evidence / "probes" / f"{len(self.finished)}-{material.probe_name}"
        root.mkdir(parents=True)
        streams = {}
        for name, text in (("stdout", stdout), ("stderr", "")):
            path = root / (name + ".log")
            path.write_bytes(text.encode())
            streams[name] = {"locator": str(path), "sha256": hashlib.sha256(text.encode()).hexdigest(),
                             "bytes": len(text.encode())}
        if material.credential_path is not None:
            info = Path(material.credential_path).lstat()
            self.consumed.append((material.credential_path, info.st_dev, info.st_ino))
            Path(material.credential_path).unlink()
        if material.probe_name == "sandbox-hooks":
            (self.workspace / ".ffs-claude-inside").write_text("FFS_INSIDE")
        result = {
            "returncode": returncode, "streams": streams,
            "host_receipt": {
                "schema": "ffs.claude-qualification-invocation/v1", "probe_name": material.probe_name,
                "contract_sha256": material.contract_sha256, "envelope_sha256": material.envelope_sha256,
                "runtime_template_sha256": material.runtime_template_sha256, "exit_code": returncode,
                "token_usage": {}, "passed": True, "telemetry_sha256": streams["stdout"]["sha256"],
            },
        }
        encoded = json.dumps(result, sort_keys=True).encode()
        (root / "result.json").write_bytes(encoded)
        evidence = {"locator": str(root / "result.json"), "sha256": hashlib.sha256(encoded).hexdigest()}
        self.store.completions[(request.activity_id, request.request_key)] = {
            "state": "completed_succeeded", "completion_evidence_json": json.dumps(evidence)}
        if self.crash_after == len(self.finished):
            self.crash_after = None
            raise _Crash(f"after-probe-{len(self.finished)}")
        return {**result, "evidence": evidence}


class _World:
    def __init__(self, tmp_path, monkeypatch):
        self.mp = monkeypatch
        candidate, self.workspace = tmp_path / "candidate", tmp_path / "work"
        self.candidate = candidate
        self.evidence = tmp_path / "evidence"
        for directory in (candidate, self.workspace, self.evidence):
            directory.mkdir()
        owned = candidate / "gsd-core" / "bin" / "gsd-tools.cjs"
        owned.parent.mkdir(parents=True)
        owned.write_text("fixture")
        (candidate / "settings.json").write_text("{}")
        (candidate / "gsd-file-manifest.json").write_text(json.dumps({
            "version": "1.15.0", "runtime": "claude",
            "files": {"gsd-core/bin/gsd-tools.cjs": qualification._digest(owned)}}))
        self.credential = tmp_path / "credential.json"
        self.credential.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "synthetic-access-token", "refreshToken": "synthetic-refresh-token",
            "expiresAt": 9999999999999, "refreshTokenExpiresAt": 9999999999999,
            "scopes": ["user:inference"], "subscriptionType": "fixture", "rateLimitTier": "fixture"}}))
        self.credential.chmod(0o600)
        binary = tmp_path / "claude"
        binary.write_text("#!/bin/sh\n:\n")
        binary.chmod(0o700)
        bridge = tmp_path / "gsd_wave_bridge.py"
        bridge.write_text("# fixture\n")
        self.bridge_command = json.dumps([str(bridge)], separators=(",", ":"))
        self.host_request = ClaudeHostRequest(str(candidate), str(self.credential), str(binary),
                                              "claude-opus-5", None, "workspace-write", False, 0, 60)
        self.token = SimpleNamespace(repository_id="repo", run_id="run", generation=1)
        self.prepared = SimpleNamespace(id="workspace", parent_activity_id="parent",
                                        child_request_key="request", ready=True, path=self.workspace,
                                        input_digest="a" * 64, base_commit="b" * 40)
        self.store = _Store()
        self.supervisor = _Supervisor(self.store, self.evidence, self.workspace)
        self.runtime = managed.claude_runtime_home(self.evidence, ACTIVITY)
        self.plans, self.stage_armed, self.pid = [], False, 100
        real_prepare = managed.prepare_claude_qualification_plan

        def spy(*args, **kwargs):
            plan = real_prepare(*args, **kwargs)
            self.plans.append(plan)
            return plan

        monkeypatch.setattr(managed, "prepare_claude_qualification_plan", spy)
        real_stage = staging.stage_private_claude_runtime

        def stage(*args, **kwargs):
            result = real_stage(*args, **kwargs)
            if self.stage_armed:
                self.stage_armed = False
                raise _Crash("after-stage")
            return result

        monkeypatch.setattr(staging, "stage_private_claude_runtime", stage)
        monkeypatch.setattr(managed, "stage_private_claude_runtime", stage, raising=False)
        self.new_process()

    def new_process(self):
        """A resumed supervisor process: same host and boot, a different pid."""
        self.pid += 1
        identity = {"host_id": "fixture-host", "boot_id": "fixture-boot", "pid": self.pid, "start_token": str(self.pid)}
        self.mp.setattr(qualification, "current_supervisor_identity", lambda: dict(identity))

    def arm(self, point):
        if point == "after-stage":
            self.stage_armed = True
        elif point.startswith("after-probe-"):
            self.supervisor.crash_after = int(point.rsplit("-", 1)[1])
        else:
            self.store.crash.add(point)

    def crash_at(self, point):
        """Run the qualification until it dies at ``point``."""
        self.arm(point)
        try:
            self.qualify()
        except _Crash:
            return
        pytest.fail(f"the seam {point!r} was never reached: no qualification-preparation record is written")

    def qualify(self, **overrides):
        kwargs = dict(
            activity_id=ACTIVITY, activity_request_key="request", parent_activity_id="parent",
            workspace=self.prepared, host_request=self.host_request, role="worker",
            evidence_root=self.evidence, final_contract_hash="c" * 64, supervisor=self.supervisor,
            bridge_command=self.bridge_command,
        )
        kwargs.update(overrides)
        return managed.qualify_managed_claude_runtime(self.store, self.token, **kwargs)

    def seed_event(self):
        return self.store.events.get(("parent", PREPARATION_KEY))

    def snapshot(self):
        found = {}
        for name in HASH_BOUND:
            path = self.runtime / name
            if os.path.lexists(path):
                info = path.lstat()
                found[name] = (hashlib.sha256(path.read_bytes()).hexdigest(), info.st_ino, info.st_mtime_ns)
        return found

    def launched_probes(self):
        return [item.claude_qualification_material.probe_name for item in self.supervisor.launched]


@pytest.fixture
def world(tmp_path, monkeypatch):
    return _World(tmp_path, monkeypatch)


def _not_reusable():
    error = getattr(staging, "RetainedClaudeRuntimeNotReusable", None)
    assert error is not None, "claude_runtime_staging has no typed RetainedClaudeRuntimeNotReusable"
    return error


@pytest.mark.parametrize("point", CRASH_POINTS)
def test_resume_replays_the_persisted_plan_with_no_second_launch(world, point):
    world.crash_at(point)
    crashed = world.snapshot()
    seed_before = json.loads(json.dumps(world.seed_event()))
    world.new_process()

    result = world.qualify()

    # Every probe ran exactly once across the crash and the resume; the outer run never launched.
    assert world.launched_probes() == ["auth-negative", "session-model", "sandbox-hooks", "nested-auth"]
    assert len(set(world.supervisor.consumed)) == len(world.supervisor.consumed) == 3
    assert world.supervisor.outer_launches == 0
    # The persisted seed is reused, never rewritten, carries no credential material, and the
    # replayed plan is the original plan.
    seed = world.seed_event()
    assert seed is not None, "the qualification plan was never persisted"
    assert world.store.event_inserts[("parent", PREPARATION_KEY)] == 1
    assert point in UNPERSISTED or seed == seed_before
    blob = json.dumps(seed)
    assert "synthetic-access-token" not in blob and "synthetic-refresh-token" not in blob
    assert str(world.credential) not in blob
    if point not in UNPERSISTED:
        first, last = world.plans[0], world.plans[-1]
        assert [(p.session_id, p.argv, p.environment, p.contract_sha256) for p in first.probes] == [
            (p.session_id, p.argv, p.environment, p.contract_sha256) for p in last.probes]
        assert (first.envelope_sha256, str(first.outside_sentinel)) == (
            last.envelope_sha256, str(last.outside_sentinel))
    # Hash-bound bytes the crash left behind are byte-identical and were never rewritten.
    after = world.snapshot()
    assert {name: after[name] for name in crashed} == crashed
    assert len({json.dumps(call, sort_keys=True, default=str) for call in world.store.created}) == 1
    assert len(world.store.promotions) == 1
    promoted, qualified, receipt, runtime, additions = result
    assert (promoted.id, runtime) == (ACTIVITY, world.runtime)
    identity = world.store.runtime_tuple_hash(qualified)
    admission = json.loads(Path(additions.admission_file).read_text())
    assert admission["runtime_identity"] == identity == world.store.promotions[0]["runtime_identity"]
    assert receipt.receipt_sha256 == world.store.receipts[-1][1]
    # A repeat after completion is a pure replay: nothing is launched, written or rewritten.
    done = world.snapshot()
    world.new_process()
    replay = world.qualify()
    assert world.launched_probes() == ["auth-negative", "session-model", "sandbox-hooks", "nested-auth"]
    assert world.snapshot() == done
    assert replay[2].receipt_sha256 == receipt.receipt_sha256


@pytest.mark.parametrize("drift", ["final-contract", "tampered-session", "tampered-credential-identity"])
def test_drift_from_the_persisted_plan_refuses_preparation_conflict(world, drift):
    world.crash_at("after-create")
    crashed = world.snapshot()
    overrides = {}
    if drift == "final-contract":
        overrides["final_contract_hash"] = "d" * 64
    else:
        seed = world.seed_event()
        assert seed is not None, "the qualification plan was not persisted before the crash window"
        if drift == "tampered-session":
            seed["plan_seed"]["sessions"][0] = "22222222-2222-4222-8222-222222222222"
        else:
            credentials = seed["plan_seed"]["credentials"]
            credentials[sorted(credentials)[0]]["inode"] += 1
    world.new_process()
    with pytest.raises(managed.ManagedClaudeQualificationRefused, match="QUALIFICATION_PREPARATION_CONFLICT"):
        world.qualify(**overrides)
    assert world.supervisor.launched == [] and world.store.promotions == []
    assert len(world.store.created) == 1
    assert world.snapshot() == crashed


@pytest.mark.parametrize("defect", ["settings-drifted", "credential-consumed", "manifest-record-drifted"])
def test_tampered_retained_stage_is_a_typed_non_reusable_refusal_and_is_not_repaired(world, defect):
    not_reusable = _not_reusable()
    world.crash_at("after-create")
    if defect == "settings-drifted":
        (world.runtime / "settings.json").write_text('{"drifted":true}\n')
    elif defect == "credential-consumed":
        (world.runtime / ".credentials.json").unlink()
    else:
        manifest = world.runtime / staging.STAGE_MANIFEST_NAME
        value = json.loads(manifest.read_text())
        value["target"]["files"]["settings.json"] = "0" * 64
        manifest.write_text(json.dumps(value))
    tampered = world.snapshot()
    world.new_process()
    with pytest.raises(not_reusable):
        world.qualify()
    assert world.supervisor.launched == [] and world.store.promotions == []
    assert world.snapshot() == tampered


@pytest.mark.parametrize(("defect", "code"), [
    ("receipt-contract", "QUALIFICATION_UNCERTAIN"),
    ("receipt-probe", "QUALIFICATION_UNCERTAIN"),
    ("stream-bytes", "QUALIFICATION_RESULT_INVALID"),
])
def test_a_replayed_probe_must_bind_this_plan_and_its_own_streams(world, defect, code):
    world.crash_at("after-probe-1")
    row = world.store.completions[(ACTIVITY, "request:qualification:" + ACTIVITY + ":ordinary")]
    evidence = json.loads(row["completion_evidence_json"])
    result = json.loads(Path(evidence["locator"]).read_text())
    if defect == "stream-bytes":
        Path(result["streams"]["stdout"]["locator"]).write_text('{"loggedIn":true}')
    else:
        result["host_receipt"]["contract_sha256" if defect == "receipt-contract" else "probe_name"] = "session-model"
        encoded = json.dumps(result, sort_keys=True).encode()
        Path(evidence["locator"]).write_bytes(encoded)
        row["completion_evidence_json"] = json.dumps({**evidence, "sha256": hashlib.sha256(encoded).hexdigest()})
    world.new_process()
    with pytest.raises(managed.ManagedClaudeQualificationRefused, match=code):
        world.qualify()
    assert world.launched_probes() == ["auth-negative"] and world.store.promotions == []


# --- r1 review: source binding, scratch strictness, admission identity, stage permissions, receipt fields ---


def _admission(world):
    return world.runtime / "supervisor-admission.json"


@pytest.mark.parametrize("change", ["credential", "settings", "installer-file", "candidate-path"])
def test_an_unseeded_stage_is_not_reused_for_a_different_source(world, tmp_path, change):
    not_reusable = _not_reusable()
    world.crash_at("before-seed")
    if change == "credential":
        world.credential.write_text(json.dumps({"claudeAiOauth": {"accessToken": "another-synthetic-token"}}))
    elif change == "settings":
        (world.candidate / "settings.json").write_text('{"hooks":{"PreToolUse":[]}}')
    elif change == "installer-file":
        (world.candidate / "gsd-core" / "bin" / "gsd-tools.cjs").write_text("drifted")
    else:
        copy = tmp_path / "candidate-copy"
        shutil.copytree(world.candidate, copy)
        world.host_request = replace(world.host_request, runtime_home=str(copy))
    left = world.snapshot()
    world.new_process()
    with pytest.raises(not_reusable):
        world.qualify()
    assert world.store.events == {} and world.store.binding is None and world.supervisor.launched == []
    assert world.snapshot() == left


@pytest.mark.parametrize(("crash", "defect"), [
    ("after-create", "mode"), ("after-create", "missing"), ("after-create", "hardlink"),
    ("after-create", "noauth-missing"), ("after-create", "scratch-directory-missing"),
    ("before-seed", "mode"), ("before-seed", "hardlink"),
])
def test_replay_requires_retained_scratch_to_be_present_private_and_unlinked(world, tmp_path, crash, defect):
    world.crash_at(crash)
    probe = world.runtime / ".ffs-claude-probe-session-model" / "settings.json"
    target = {"noauth-missing": world.runtime / ".ffs-claude-noauth" / "settings.json",
              "scratch-directory-missing": world.runtime / ".ffs-claude-qualification-tmp"}.get(defect, probe)
    if defect == "mode":
        target.chmod(0o666)
    elif defect == "hardlink":
        os.link(target, tmp_path / "alias")
    elif defect == "scratch-directory-missing":
        target.rmdir()
    else:
        target.unlink()
    world.new_process()
    with pytest.raises(managed.ManagedClaudeQualificationRefused, match="QUALIFICATION_PREPARATION_INVALID"):
        world.qualify()
    assert world.supervisor.launched == []
    assert defect not in ("missing", "noauth-missing", "scratch-directory-missing") or not os.path.lexists(target)


def test_an_unpromoted_admission_must_be_the_exact_placeholder(world):
    world.crash_at("after-create")
    placeholder = json.loads(_admission(world).read_text())
    _admission(world).write_bytes(managed._canonical({**placeholder, "runtime_identity": "e" * 64}) + b"\n")
    world.new_process()
    with pytest.raises(managed.ManagedClaudeQualificationRefused) as refused:
        world.qualify()
    assert world.supervisor.launched == [], "a probe ran under an admission that is not the placeholder"
    assert str(refused.value) == "ADMISSION_CONFLICT"


def test_a_promoted_admission_must_carry_the_promoted_identity(world):
    world.crash_at("after-promote")
    placeholder = json.loads(_admission(world).read_text())
    _admission(world).write_bytes(managed._canonical({**placeholder, "runtime_identity": "e" * 64}) + b"\n")
    receipts = list(world.store.receipts)
    world.new_process()
    with pytest.raises(managed.ManagedClaudeQualificationRefused) as refused:
        world.qualify()
    assert world.store.receipts == receipts, "a receipt was committed under an unrelated admitted identity"
    assert str(refused.value) == "ADMISSION_CONFLICT"


@pytest.mark.parametrize("defect", ["settings-mode", "closure-file-mode", "hardlinked-file",
                                    "directory-mode", "inventory-record-removed"])
def test_a_retained_stage_must_keep_its_modes_links_and_inventory(world, tmp_path, defect):
    not_reusable = _not_reusable()
    world.crash_at("before-seed" if defect == "inventory-record-removed" else "after-create")
    closure = world.runtime / "gsd-core" / "bin" / "gsd-tools.cjs"
    if defect == "settings-mode":
        (world.runtime / "settings.json").chmod(0o666)
    elif defect == "closure-file-mode":
        closure.chmod(0o644)
    elif defect == "hardlinked-file":
        os.link(world.runtime / "settings.json", tmp_path / "alias")
    elif defect == "directory-mode":
        (world.runtime / "gsd-core").chmod(0o755)
    else:
        manifest = world.runtime / staging.STAGE_MANIFEST_NAME
        value = json.loads(manifest.read_text())
        del value["target"]["files"]["gsd-core/bin/gsd-tools.cjs"]
        manifest.write_text(json.dumps(value))
    tampered = world.snapshot()
    world.new_process()
    with pytest.raises(not_reusable):
        world.qualify()
    assert world.supervisor.launched == [] and world.store.promotions == []
    assert world.snapshot() == tampered


@pytest.mark.parametrize(("field", "value"), [
    ("schema", "ffs.codex-qualification-invocation/v1"),
    ("runtime_template_sha256", "0" * 64),
    ("exit_code", 0),
    ("telemetry_sha256", "0" * 64),
])
def test_a_replayed_probe_receipt_must_match_the_plan_and_its_completion(world, field, value):
    world.crash_at("after-probe-1")
    row = world.store.completions[(ACTIVITY, "request:qualification:" + ACTIVITY + ":ordinary")]
    evidence = json.loads(row["completion_evidence_json"])
    result = json.loads(Path(evidence["locator"]).read_text())
    result["host_receipt"][field] = value
    encoded = json.dumps(result, sort_keys=True).encode()
    Path(evidence["locator"]).write_bytes(encoded)
    row["completion_evidence_json"] = json.dumps({**evidence, "sha256": hashlib.sha256(encoded).hexdigest()})
    world.new_process()
    with pytest.raises(managed.ManagedClaudeQualificationRefused, match="QUALIFICATION_UNCERTAIN"):
        world.qualify()
    assert world.launched_probes() == ["auth-negative"] and world.store.promotions == []


# --- r2 review: expected bytes come from the present source, never from the retained manifest ---


@pytest.mark.parametrize("forgery", ["credential-keeps-refresh-token", "closure-file-altered",
                                     "installer-manifest-altered"])
def test_a_retained_stage_is_checked_against_its_source_not_its_own_manifest(world, forgery):
    not_reusable = _not_reusable()
    world.crash_at("after-stage")
    target = {"credential-keeps-refresh-token": ".credentials.json",
              "closure-file-altered": "gsd-core/bin/gsd-tools.cjs",
              "installer-manifest-altered": "gsd-file-manifest.json"}[forgery]
    path = world.runtime / target
    if forgery == "credential-keeps-refresh-token":
        forged = world.credential.read_bytes()      # the source document, refresh bearer included
    else:
        forged = path.read_bytes() + b" "
    path.write_bytes(forged)
    manifest = world.runtime / staging.STAGE_MANIFEST_NAME
    value = json.loads(manifest.read_text())
    value["target"]["files"][target] = hashlib.sha256(forged).hexdigest()   # the forger updates the record too
    manifest.write_text(json.dumps(value))
    left = world.snapshot()
    world.new_process()
    with pytest.raises(not_reusable):
        world.qualify()
    assert world.store.events == {} and world.store.binding is None and world.supervisor.launched == []
    assert world.snapshot() == left


def test_a_record_written_before_replay_support_stays_fail_closed(world):
    """No ``qualification-preparation`` event but a retained activity: today's refusal, not a replay."""
    world.crash_at("after-create")
    world.store.events.pop(("parent", PREPARATION_KEY), None)
    crashed = world.snapshot()
    world.new_process()
    with pytest.raises(managed.ManagedClaudeQualificationRefused, match="QUALIFICATION_PREPARATION_INVALID"):
        world.qualify()
    assert world.supervisor.launched == [] and world.store.promotions == []
    assert len(world.store.created) == 1
    assert world.snapshot() == crashed


# --- the session seam: outer-launch replay guard and the retained-runtime refusal codes ------------


def _session(tmp_path, monkeypatch, *, launch=None, qualify=None):
    """``prepare_managed_claude_session`` over scripted rows; ``launch`` is the retained real outer intent."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ready = SimpleNamespace(id="outer-workspace", parent_activity_id="parent",
                            child_request_key="managed-host:request", ready=True, path=workspace,
                            input_digest="a" * 64, base_commit="b" * 40)
    counts = SimpleNamespace(qualify=0, outer=0, probes=0)

    class Transaction:
        def execute(self, sql, *_args):
            if "SELECT state,completion_status FROM authority_launch_intents" in sql:
                return SimpleNamespace(fetchone=lambda: launch)
            if any(marker in sql for marker in ("child_request_key", "a.request_key", "capacity_exempt",
                                                  "runtime_identity FROM authority_child_bindings",
                                                  "idempotency_key='frontend-operation'")):
                return SimpleNamespace(fetchone=lambda: None)
            return SimpleNamespace(fetchone=lambda: {"state": "ready", "kind": "execute"})

    class Store:
        def get_run_policy_budget(self, **_kwargs):
            return None

        def get_sealed_acceptance(self, **_kwargs):
            return None

        def get_frontend_policy_state(self, **_kwargs):
            return None

        @contextmanager
        def read_transaction(self):
            yield Transaction()

    class Channel:
        def __init__(self, *_args):
            pass

        def start(self):
            return None

        def attach_wave_consumer(self, _consumer):
            return None

        def close(self):
            return None

    class Supervisor:
        def __init__(self, *_args, **_kwargs):
            pass

        def contain_revoked(self):
            return ()

        def launch_managed_outer(self, _request):
            counts.outer += 1

        def launch_qualification(self, *_args, **_kwargs):
            counts.probes += 1

    class WaveConsumer:
        def __init__(self, *_args, **_kwargs):
            pass

    def stub(_store, _token, **_kwargs):
        counts.qualify += 1
        if qualify is not None:
            raise qualify
        raise AssertionError("qualification must not run in this scenario")

    monkeypatch.setattr(managed, "_from_row", lambda _row: SimpleNamespace(base_commit="b" * 40, repository_path=workspace))
    monkeypatch.setattr(managed, "load_input_snapshot", lambda *_args: SimpleNamespace(manifest={}))
    monkeypatch.setattr(managed, "_verify_snapshot_complete", lambda *_args: None)
    monkeypatch.setattr(managed, "begin_child_workspace_preparation", lambda *_args, **_kwargs: ready)
    monkeypatch.setattr(managed, "prepare_workspace", lambda *_args, **_kwargs: ready)
    monkeypatch.setattr(managed, "WorkerChannelServer", Channel)
    monkeypatch.setattr(managed, "Supervisor", Supervisor)
    monkeypatch.setattr(managed, "WaveConsumer", WaveConsumer)
    monkeypatch.setattr(managed, "qualify_managed_claude_runtime", stub)
    request = ClaudeHostRequest(str(tmp_path / "candidate"), str(tmp_path / "credential"), str(tmp_path / "claude"),
                                "claude-opus-5", None, "workspace-write", False, 23, 60)
    token = SimpleNamespace(repository_id="repo", run_id="run", generation=1, planning_scope="1")
    context = SimpleNamespace(
        activity_id="parent", evidence_root=tmp_path / "evidence", workspace=str(tmp_path / "root-workspace"),
        upstream={"project": None, "workstream": None, "session_key": None,
                  "planning_root": str(tmp_path / "root-workspace" / ".planning")})
    return (lambda: managed.prepare_managed_claude_session(
        Store(), token, context, ("/gsd-execute-phase", "1"), "request", request)), ready, counts


def test_session_without_a_retained_outer_launch_is_prepared(tmp_path, monkeypatch):
    prepare, _ready, counts = _session(tmp_path, monkeypatch)
    prepare().close(None, None, None)
    assert (counts.qualify, counts.outer, counts.probes) == (0, 0, 0)


@pytest.mark.parametrize(("state", "code"), [
    ("reserved", "INTENT_RECONCILIATION_REQUIRED"),
    ("acknowledged", "INTENT_RECONCILIATION_REQUIRED"),
    ("released_to_execute", "INTENT_RECONCILIATION_REQUIRED"),
    ("completed_succeeded", "REQUEST_ALREADY_COMPLETED"),
    ("completed_failed", "REQUEST_ALREADY_COMPLETED"),
    ("closed_dead", "REQUEST_ALREADY_COMPLETED"),
])
def test_retained_outer_launch_refuses_and_never_launches_again(tmp_path, monkeypatch, state, code):
    prepare, _ready, counts = _session(tmp_path, monkeypatch, launch={"state": state, "completion_status": None})
    with pytest.raises(SupervisorRefused) as refused:
        prepare()
    assert refused.value.code == code
    assert (counts.qualify, counts.outer, counts.probes) == (0, 0, 0)


@pytest.mark.parametrize(("request_key", "code"), [
    ("managed-host:request", "RETAINED_RUNTIME_NOT_REUSABLE"),
    ("wave-request", "CHILD_RUNTIME_NOT_REUSABLE"),
])
def test_non_reusable_retained_stage_maps_like_the_codex_host(tmp_path, monkeypatch, request_key, code):
    prepare, ready, counts = _session(tmp_path, monkeypatch, qualify=_not_reusable()("drifted"))
    session = prepare()
    with pytest.raises(SupervisorRefused) as refused:
        session.seam.qualify("33333333-3333-4333-8333-333333333333", ready, request_key, "parent", "d" * 64, "worker")
    assert refused.value.code == code and counts.qualify == 1
    session.close(None, None, None)
