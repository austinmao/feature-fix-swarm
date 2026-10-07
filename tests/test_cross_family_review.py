"""spec-014 E8 prerequisite 4b: an opted-in native reviewer runs on the opposite host family.

The production native review ran on the outer run's own host: every producer took ``session.seam``.  PATH-014 and
FR-046 need both cross-family directions: ``claude-codex`` (a Claude outer reviewed by Codex) and ``codex-claude``
(a Codex outer reviewed by Claude).  Operator decision D31: an explicit ``--review-host*`` request, all-or-none like
``--host*``, gives the spec review and the final review a seam built with the other host's own staging and
qualification; recovery and repair stay on the outer host.  Absent, the request material and every seam are as before.

This file covers the ingress, the session seam and the lifecycle routing over scripted collaborators;
``test_cross_family_review_lifecycle`` runs the Codex-outer direction end to end on the real authority.
Fixture-level proof only: no Codex or Claude CLI runs.  Not native host qualification, not E8.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import host_capabilities
import run_state.codex_host as codex_host
import run_state.frontend_lifecycle as frontend_lifecycle
import run_state.frontend_producers as frontend_producers
import run_state.managed_qualification as managed_qualification
import run_state.recovery_producer as recovery_producer
import run_state.runtime_staging as runtime_staging
import run_state.supervisor as supervisor_module
from model_requests import resolve_request
from run_state import cli
from run_state import managed_claude_qualification as managed
from run_state.claude_host import ClaudeHostRequest
from run_state.frontend_producers import HostRuntimeSeam, ManagedHostSession
from run_state.host_request import CodexHostRequest
from run_state.supervisor import SupervisorRefused
from test_managed_lifecycle_assembly import _last_envelope, _setup

JUDGMENT = '{"kind":"tier","name":"judgment"}'
EXECUTION = '{"kind":"tier","name":"execution"}'
ENTRIES = ("managed-start", "frontend-start")
DIRECTIONS = pytest.mark.parametrize(("outer", "reviewer"), [("claude", "codex"), ("codex", "claude")],
                                     ids=["claude-codex", "codex-claude"])


# --- ingress: the CLI request, its material and its refusals ----------------------------------------------------


def _flags(host: str, prefix: str, root: Path, *, reservation: str = "100", model: str = EXECUTION) -> list[str]:
    """One complete ``--host*`` or ``--review-host*`` request; its paths need only be canonical."""
    flag = "--" + prefix
    values = [flag, host, flag + "-runtime-home", str(root / f"{prefix}-{host}-home"),
              flag + "-binary", str(root / f"{prefix}-{host}"), flag + "-model-request", model,
              flag + "-sandbox", "workspace-write", flag + "-network", "disabled",
              flag + "-token-reservation", reservation, flag + "-timeout", "30"]
    if host == "claude":
        values += [flag + "-credential-source", str(root / f"{prefix}-credential")]
    return values


def _without(values: list[str], flag: str) -> list[str]:
    index = values.index(flag)
    return values[:index] + values[index + 2:]


def _ingress(tmp_path, monkeypatch):
    _primary, authority, _repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(_primary)
    for key in ("GSD_RUN_ID", "FFS_RUN_ID", "GSD_RESUME"):
        monkeypatch.delenv(key, raising=False)
    return env, authority, tmp_path.resolve()


def _start(env, authority, entry: str, *request: str) -> int:
    common = ["--objective", "cross family", "--state-root", str(authority),
              "--upstream-runtime-manifest", env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
              "--upstream-runtime-sha256", env["FFS_UPSTREAM_RUNTIME_SHA256"],
              "--request-key", "cross-family", "--run-id", "xf", "--dispatch-limit", "8", "--token-limit", "1000"]
    if entry == "managed-start":
        return cli.main(["managed-start", *common, "--selection-manifest", env["FFS_SELECTION_MANIFEST"],
                         *request, "--", "/gsd-plan-phase", "1"])
    return cli.main(["frontend-start", "--frontend", "task-swarm", *common, "--select-file", "src/input.txt",
                     "--scope", "1", *request])


def _captured_material(monkeypatch) -> list[dict]:
    """Every managed request material the ingress hands the writer; nothing is admitted or written."""
    seen = []

    def fixture_start(args, *, on_ready):
        del on_ready
        seen.append(json.loads(json.dumps(args.managed_request_material)))
        return 0

    monkeypatch.setattr(cli, "_cmd_fixture_start", fixture_start)
    return seen


def _tree(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


# Today's request material: an unchanged request keeps its digest, and so its request key and run.
_MATERIAL_KEYS = {
    "managed-start": {"writer_version", "skill", "arguments", "accepted_requirement_ids", "dispatch_limit",
                      "token_limit", "worker_capacity", "policy_tier", "ceremony_estimate",
                      "upstream_runtime_sha256", "host_request"},
    "frontend-start": {"writer_version", "frontend", "invocation_text", "operation", "dispatch_limit",
                       "token_limit", "worker_capacity", "policy_tier", "ceremony_estimate",
                       "accepted_requirement_ids", "upstream_runtime_sha256", "host_request"},
}


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("outer", ["claude", "codex"])
def test_without_review_flags_the_request_material_is_unchanged(tmp_path, monkeypatch, entry, outer):
    env, authority, root = _ingress(tmp_path, monkeypatch)
    seen = _captured_material(monkeypatch)
    assert _start(env, authority, entry, *_flags(outer, "host", root)) == 0
    (material,) = seen
    assert set(material) == _MATERIAL_KEYS[entry] and material["host_request"]["host"] == outer


@pytest.mark.parametrize("entry", ENTRIES)
@DIRECTIONS
def test_an_opposite_review_host_joins_the_material_and_nothing_else_changes(tmp_path, monkeypatch, entry, outer,
                                                                             reviewer):
    env, authority, root = _ingress(tmp_path, monkeypatch)
    seen = _captured_material(monkeypatch)
    plain_request = _flags(outer, "host", root)
    assert _start(env, authority, entry, *plain_request) == 0
    assert _start(env, authority, entry, *plain_request, *_flags(reviewer, "review-host", root, model=JUDGMENT)) == 0
    plain, opted = seen
    review = opted.pop("review_host_request")
    assert opted == plain
    resolved = resolve_request(json.loads(JUDGMENT), host=reviewer)
    assert (review["host"], review["model"], review["effort"]) == (reviewer, resolved["model"], resolved["effort"])
    assert review["binary"] == str(root / f"review-host-{reviewer}")


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("host", ["claude", "codex"])
def test_a_reviewer_on_the_outer_host_refuses_before_any_effect(tmp_path, monkeypatch, capsys, entry, host):
    env, authority, root = _ingress(tmp_path, monkeypatch)
    seen = _captured_material(monkeypatch)
    before = _tree(authority)
    capsys.readouterr()
    assert _start(env, authority, entry, *_flags(host, "host", root), *_flags(host, "review-host", root)) == 2
    assert _last_envelope(capsys)["code"] == "REVIEW_HOST_NOT_OPPOSITE"
    assert seen == [] and _tree(authority) == before


# (outer request, review request): every shape the all-or-none rule refuses.
_PARTIAL = {
    "kind-only": lambda root: (_flags("claude", "host", root), ["--review-host", "codex"]),
    "no-timeout": lambda root: (_flags("claude", "host", root),
                                _without(_flags("codex", "review-host", root), "--review-host-timeout")),
    "fields-without-kind": lambda root: (_flags("claude", "host", root),
                                         _without(_flags("codex", "review-host", root), "--review-host")),
    "claude-without-credential": lambda root: (
        _flags("codex", "host", root),
        _without(_flags("claude", "review-host", root), "--review-host-credential-source")),
    "codex-with-credential": lambda root: (
        _flags("claude", "host", root),
        [*_flags("codex", "review-host", root), "--review-host-credential-source", str(root / "credential")]),
    "no-outer-host": lambda root: ([], _flags("codex", "review-host", root)),
}


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("case", sorted(_PARTIAL))
def test_a_partial_review_host_request_refuses_incomplete(tmp_path, monkeypatch, capsys, entry, case):
    env, authority, root = _ingress(tmp_path, monkeypatch)
    seen = _captured_material(monkeypatch)
    outer, review = _PARTIAL[case](root)
    capsys.readouterr()
    assert _start(env, authority, entry, *outer, *review) == 2
    assert _last_envelope(capsys)["code"] == "HOST_REQUEST_INCOMPLETE" and seen == []


@pytest.mark.parametrize("entry", ENTRIES)
def test_a_changed_reviewer_request_on_the_same_key_is_an_idempotency_conflict(tmp_path, monkeypatch, capsys, entry):
    env, authority, root = _ingress(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(supervisor_module, "run_managed_command",
                        lambda store, token, context, **kwargs: calls.append(kwargs) or 0)
    outer = _flags("claude", "host", root)
    review = _flags("codex", "review-host", root, model=JUDGMENT)
    assert _start(env, authority, entry, *outer, *review) == 0
    (first,) = calls
    # The request reaches the managed command whole: the reviewer's own request and model request, beside the outer's.
    assert type(first["host_request"]) is ClaudeHostRequest and first["model_request"] == json.loads(EXECUTION)
    assert type(first["review_host_request"]) is CodexHostRequest
    assert first["review_host_request"].token_reservation == 100
    assert first["review_model_request"] == json.loads(JUDGMENT)
    # The identical request replays under its key ...
    assert _start(env, authority, entry, *outer, *review) == 0 and len(calls) == 2
    # ... a changed or dropped reviewer request does not.
    for changed in (_flags("codex", "review-host", root, model=JUDGMENT, reservation="200"), []):
        capsys.readouterr()
        assert _start(env, authority, entry, *outer, *changed) == 2
        assert _last_envelope(capsys)["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(calls) == 2


def _request(host: str, root: Path, *, model: str = EXECUTION):
    if host == "codex":
        resolved = resolve_request(json.loads(model), host="codex")
        return CodexHostRequest(str(root / "codex-home"), str(root / "codex"), resolved["model"], resolved["effort"],
                                "workspace-write", False, 41, 60)
    resolved = resolve_request(json.loads(model), host="claude")
    return ClaudeHostRequest(str(root / "claude-home"), str(root / "claude-credential"), str(root / "claude"),
                             resolved["model"], resolved["effort"], "workspace-write", False, 23, 60)


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_the_managed_command_refuses_a_same_host_reviewer_before_any_session(tmp_path, host):
    root = tmp_path.resolve()
    with pytest.raises(SupervisorRefused) as refused:
        supervisor_module.run_managed_command(
            None, None, None, ("/gsd-plan-phase", "1"), "key", 1, 1, _request(host, root),
            review_host_request=_request(host, root, model=JUDGMENT))
    assert refused.value.code == "REVIEW_HOST_NOT_OPPOSITE"


# --- the session seam: a Claude outer builds its Codex reviewer with Codex's own pieces ---------------------------


def _claude_session(tmp_path, monkeypatch):
    """``prepare_managed_claude_session`` over scripted rows, as ``test_managed_claude_wave`` drives it."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ready = SimpleNamespace(id="outer-workspace", parent_activity_id="parent", child_request_key="managed-host:request",
                            ready=True, path=workspace, input_digest="a" * 64, base_commit="b" * 40)
    reviewer_path = tmp_path / "reviewer-workspace"
    reviewer_path.mkdir()
    reviewer_ready = SimpleNamespace(id="reviewer-workspace", path=reviewer_path, input_digest="c" * 64,
                                     base_commit="b" * 40)
    claude_qualified = []

    class Transaction:
        def execute(self, sql, *_args):
            if any(marker in sql for marker in ("child_request_key", "a.request_key", "capacity_exempt",
                                                  "authority_launch_intents",
                                                  "runtime_identity FROM authority_child_bindings",
                                                  "idempotency_key='frontend-operation'")):
                return SimpleNamespace(fetchone=lambda: None, fetchall=lambda: [])
            return SimpleNamespace(fetchone=lambda: {"state": "ready", "kind": "execute"})

    class Store:
        db_path = str(tmp_path / "authority" / "control.sqlite3")

        def get_run_policy_budget(self, **_kwargs):
            return None

        def get_sealed_acceptance(self, **_kwargs):
            return None

        @contextmanager
        def read_transaction(self):
            yield Transaction()

        def runtime_tuple_hash(self, runtime):
            return "runtime:" + runtime.marker

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
        def __init__(self, *_args, **kwargs):
            self.worker_channel = kwargs.get("worker_channel")

        def contain_revoked(self):
            return ()

    class WaveConsumer:
        def __init__(self, *_args, **_kwargs):
            pass

    def claude_qualify(_store, _token, **kwargs):
        claude_qualified.append(kwargs)
        raise AssertionError("the Codex reviewer must never qualify on Claude")

    monkeypatch.setattr(managed, "_from_row", lambda _row: SimpleNamespace(base_commit="b" * 40,
                                                                          repository_path=workspace))
    monkeypatch.setattr(managed, "load_input_snapshot", lambda *_args: SimpleNamespace(manifest={}))
    monkeypatch.setattr(managed, "_verify_snapshot_complete", lambda *_args: None)
    monkeypatch.setattr(managed, "begin_child_workspace_preparation", lambda *_args, **_kwargs: ready)
    monkeypatch.setattr(managed, "prepare_workspace", lambda *_args, **_kwargs: ready)
    monkeypatch.setattr(managed, "WorkerChannelServer", Channel)
    monkeypatch.setattr(managed, "Supervisor", Supervisor)
    monkeypatch.setattr(managed, "WaveConsumer", WaveConsumer)
    monkeypatch.setattr(managed, "qualify_managed_claude_runtime", claude_qualify)
    token = SimpleNamespace(repository_id="repo", run_id="run", generation=1, planning_scope="1")
    evidence = tmp_path / "evidence"
    context = SimpleNamespace(
        activity_id="parent", evidence_root=evidence, workspace=str(tmp_path / "root-workspace"),
        upstream={"project": None, "workstream": None, "session_key": None,
                  "planning_root": str(tmp_path / "root-workspace" / ".planning")})
    outer = _request("claude", tmp_path.resolve())

    def prepare(**review):
        return managed.prepare_managed_claude_session(Store(), token, context, ("/gsd-execute-phase", "1"),
                                                      "request", outer, **review)

    return SimpleNamespace(prepare=prepare, reviewer_ready=reviewer_ready, host_evidence=evidence / "host",
                           claude_qualified=claude_qualified, outer=outer)


def _codex_pieces(tmp_path, monkeypatch, *, admitted=None):
    """Codex admission, staging, qualification and adapter fixtures, recording every call."""
    chain = {"launcher_sha256": "c" * 64}
    calls = SimpleNamespace(admitted=[], staged=[], qualified=[], built=[], released=[])

    def admit_cli(binary):
        calls.admitted.append(binary)
        return {"version": "0.154.0", "binary": dict(admitted or chain)}

    def stage(template, home, worktree):
        calls.staged.append((Path(template), Path(home), Path(worktree)))

    def qualify(_store, _token, **kwargs):
        calls.qualified.append(kwargs)
        activity_id = kwargs["activity_id"]
        return SimpleNamespace(activity=SimpleNamespace(id=activity_id),
                               qualified_runtime=SimpleNamespace(binary=tuple(chain.items()), marker=activity_id),
                               runtime_receipt=SimpleNamespace(receipt_sha256="receipt:" + activity_id))

    class Adapter:
        def __init__(self, _qualified, _binary, version, *, state_root, tmp_records, activity_id):
            del state_root, tmp_records
            self.version, self.activity_id = version, activity_id

        def build_launch_material(self, prompt, *, attempt, gsd_environment):
            assert attempt == 1 and gsd_environment is not None
            directory = tmp_path / ("private-tmp-" + self.activity_id)
            directory.mkdir()
            material = SimpleNamespace(argv=("codex", prompt), temporary_dir=str(directory))
            calls.built.append(material)
            return material

        @staticmethod
        def release_launch_material(material):
            calls.released.append(material)

    monkeypatch.setattr(host_capabilities, "admit_cli", admit_cli)
    monkeypatch.setattr(runtime_staging, "stage_or_reuse_private_codex_runtime", stage)
    monkeypatch.setattr(managed_qualification, "qualify_managed_runtime", qualify)
    monkeypatch.setattr(codex_host, "CodexHostAdapter", Adapter)
    monkeypatch.setattr(supervisor_module, "_launches_provably_dead", lambda _store, _activity_id: True)
    return calls


def test_a_claude_outer_session_without_a_reviewer_request_builds_no_codex_seam(tmp_path, monkeypatch):
    """Control: today's Claude session, with no Codex admission, staging or qualification."""
    world = _claude_session(tmp_path, monkeypatch)
    codex = _codex_pieces(tmp_path, monkeypatch)
    session = world.prepare()
    assert session.seam.host == "claude" and getattr(session, "review_seam", None) is None
    session.close(None, None, None)
    assert (codex.admitted, codex.staged, codex.qualified, codex.released) == ([], [], [], [])


def test_a_claude_outer_session_builds_its_reviewer_with_codex_staging_and_qualification(tmp_path, monkeypatch):
    world = _claude_session(tmp_path, monkeypatch)
    codex = _codex_pieces(tmp_path, monkeypatch)
    review_request = _request("codex", tmp_path.resolve(), model=JUDGMENT)
    session = world.prepare(review_host_request=review_request, review_model_request=json.loads(JUDGMENT))
    review = session.review_seam
    assert (session.seam.host, review.host) == ("claude", "codex")
    # The reviewer's identity is its own request's, resolved for Codex, never the Claude outer's.
    resolved = resolve_request(json.loads(JUDGMENT), host="codex")
    assert (review.binary, review.cli_version, review.model, review.effort, review.model_request) == (
        review_request.binary, "0.154.0", resolved["model"], resolved["effort"], json.loads(JUDGMENT))
    assert codex.admitted == [review_request.binary]

    reviewer_supervisor = SimpleNamespace(worker_channel=None)
    qualified = review.qualify("reviewer-activity", world.reviewer_ready, "final-review:reviewer", "outer-activity",
                               "d" * 64, "reviewer", supervisor=reviewer_supervisor)
    home = world.host_evidence / "runtimes" / "reviewer-activity"
    assert codex.staged == [(Path(review_request.runtime_home), home, world.reviewer_ready.path)]
    (call,) = codex.qualified
    assert (call["role"], call["activity_request_key"], call["parent_activity_id"]) == (
        "reviewer", "final-review:reviewer", "outer-activity")
    assert call["supervisor"] is reviewer_supervisor and call["workspace"] is world.reviewer_ready
    assert call["host_request"] == replace(review_request, runtime_home=str(home))
    assert (call["runtime_home"], call["binary"]) == (home, Path(review_request.binary))
    assert world.claude_qualified == []

    request, _adapter = review.bind(qualified, "review prompt", world.reviewer_ready, "d" * 64, "final-review:launch")
    assert request.claude_material is None and request.codex_material is codex.built[-1]
    assert request.codex_material.argv == ("codex", "review prompt")
    assert (request.token_reservation, request.runtime_receipt_sha256, request.runtime_identity) == (
        review_request.token_reservation, "receipt:reviewer-activity", "runtime:reviewer-activity")
    # Session close releases the reviewer's bound material too, through the same liveness proof.
    session.close(None, None, None)
    assert codex.released == [request.codex_material]


def test_a_codex_reviewer_keeps_its_admitted_version_bound_to_the_qualified_launcher(tmp_path, monkeypatch):
    world = _claude_session(tmp_path, monkeypatch)
    _codex_pieces(tmp_path, monkeypatch, admitted={"launcher_sha256": "e" * 64})
    review_request = _request("codex", tmp_path.resolve(), model=JUDGMENT)
    session = world.prepare(review_host_request=review_request, review_model_request=None)
    # No explicit model request: the reviewer's exact resolved model, as the outer seam falls back.
    assert session.review_seam.model_request == {"kind": "exact", "id": review_request.model}
    with pytest.raises(SupervisorRefused) as refused:
        session.review_seam.qualify("reviewer-activity", world.reviewer_ready, "final-review:reviewer",
                                    "outer-activity", "d" * 64, "reviewer", supervisor=SimpleNamespace())
    assert refused.value.code == "HOST_CLI_VERSION_UNBOUND"
    session.close(None, None, None)


# --- the lifecycle routing: only the spec and final reviews cross --------------------------------------------------


def _seam(host: str) -> HostRuntimeSeam:
    return HostRuntimeSeam(host=host, qualify=None, bind=None, binary="/fixture/" + host, cli_version="fixture",
                           model=host + "-model", effort=None, model_request={"kind": "exact", "id": host + "-model"})


def _route(monkeypatch, *, outer: str, reviewer: str | None) -> dict[str, str]:
    """Drive one sealed lifecycle over scripted producers; return the host seam each producer was handed."""
    routed, sealed = {}, {}

    class Transaction:
        def execute(self, *_args):
            return SimpleNamespace(fetchone=lambda: None, fetchall=lambda: [])

    class Store:
        def get_sealed_acceptance(self, **_kwargs):
            return sealed.get("acceptance")

        def get_frontend_policy_state(self, **_kwargs):
            return SimpleNamespace(stage="EXECUTE") if sealed else None

        @contextmanager
        def read_transaction(self):
            yield Transaction()

    def seal(_store, _token, *, command_mode, draft, runtime_hash, candidate_hash, review):
        del command_mode, draft, runtime_hash, candidate_hash
        review(SimpleNamespace(draft_hash="f" * 64))
        sealed["acceptance"] = SimpleNamespace(material={"command_mode": "task-swarm"})

    def producer(name):
        def produce(*_args, seam, **_kwargs):
            routed[name] = seam.host
        return produce

    def drive(_store, _token, *, supervisor, controller, workspace, parent_activity_id, producers):
        del supervisor, controller, workspace, parent_activity_id
        producers.repair(None, ["criterion"])
        producers.recover({"packet": "fixture"})
        producers.repair_unfinished()
        producers.final_review(None)
        return "DONE"

    monkeypatch.setattr(frontend_producers, "seal_from_draft", seal)
    monkeypatch.setattr(frontend_producers, "produce_spec_review", producer("spec_review"))
    monkeypatch.setattr(frontend_producers, "produce_final_review", producer("final_review"))
    for name in ("produce_recovery", "produce_repair", "repair_unfinished"):
        monkeypatch.setattr(recovery_producer, name, producer(name))
    monkeypatch.setattr(frontend_lifecycle, "drive_frontend_lifecycle", drive)
    monkeypatch.setattr(supervisor_module, "Supervisor", lambda *_args, **_kwargs: SimpleNamespace(worker_channel=None))
    request = SimpleNamespace(activity_id="outer", request_key="managed-host:key:launch", runtime_identity="r" * 64,
                              codex_material=None, claude_material=None)
    closed = []
    fields = dict(host=outer, supervisor=None, evidence_root=Path("/fixture/evidence"),
                  ready=SimpleNamespace(id="ready", input_digest="a" * 64, path=Path("/fixture/ready")),
                  child_key="managed-host:key", outer_activity_id="outer", invocation=("task-swarm",),
                  timeout_seconds=30, seam=_seam(outer), prepare_outer=lambda: (request, None),
                  execute=lambda *_args, **_kwargs: None, close=lambda *args: closed.append(args))
    if reviewer is not None:
        fields["review_seam"] = _seam(reviewer)
    draft = {"spec_review": "native", "criteria": [], "exclusions": [], "global_invariants": []}
    assert frontend_producers.drive_managed_session(
        Store(), SimpleNamespace(repository_id="repo", run_id="run", generation=1), SimpleNamespace(activity_id="parent"),
        ManagedHostSession(**fields), acceptance_draft=draft) == 0
    assert len(closed) == 1
    return routed


@pytest.mark.parametrize("outer", ["claude", "codex"])
def test_without_a_reviewer_seam_every_producer_stays_on_the_outer_host(monkeypatch, outer):
    """Control: today's routing."""
    assert _route(monkeypatch, outer=outer, reviewer=None) == {
        name: outer for name in ("spec_review", "final_review", "produce_recovery", "produce_repair",
                                 "repair_unfinished")}


@DIRECTIONS
def test_only_the_spec_and_final_reviews_cross_to_the_reviewer_seam(monkeypatch, outer, reviewer):
    assert _route(monkeypatch, outer=outer, reviewer=reviewer) == {
        "spec_review": reviewer, "final_review": reviewer,
        "produce_recovery": outer, "produce_repair": outer, "repair_unfinished": outer}
