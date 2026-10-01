"""F51 round 2: a SIGKILLed frontend-start resumes through the real qualification path.

The first run executes in a child process that SIGKILLs itself during the final
reviewer's qualification, so its owner fence and any released probe child are
really dead (M5 ``e2e-m5a-phase02`` died with ``qualification:dispatched`` in
flight).  The resume then runs the real ``prepare_managed_codex_session``
``qualify_runtime`` closure, real private runtime staging and its strict reuse
validation, and the real ``qualify_managed_runtime`` and promotion.  Only the
host observation is scripted: the Codex observer module and ``verify_runtime``
(no Codex CLI ever runs), plus the lifecycle assembly's fixture host binary,
shared-admission observation and wave proof.
"""
from __future__ import annotations

from collections import namedtuple
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

import pytest

import host_capabilities
from host_capabilities import QualifiedCodexRuntime, TELEMETRY_SCHEMA, _binary_chain
from process_identity import DEAD, ProcessIdentity, probe_identity
from run_state import cli
import run_state.managed_qualification as managed_qualification
from run_state.state import ControlStore
from test_final_review_resume import _held_resources, _outer_intents
from test_managed_lifecycle_assembly import _REVIEW, _draft, _setup, requires_local_confinement

ROOT = Path(__file__).resolve().parents[1]
_PROBES = ("ordinary", "native-positive", "native-negative", "native-multi-agent")
_USAGE = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
          "reasoning_output_tokens")
_STREAM = "\n".join(json.dumps(row) for row in (
    {"type": "thread.started", "thread_id": "scripted-probe"}, {"type": "turn.started"},
    {"type": "turn.completed", "usage": {name: 0 for name in _USAGE}}))
_FIELDS = ("binary", "runtime", "workspace", "supervisor", "execution", "observation")
_Probe = namedtuple("Probe", "name argv environment timeout_seconds")


def _template(root: Path) -> Path:
    """A real installer Codex home: the source closure that private staging copies and validates."""
    source, skills = root / ".codex", root / ".agents" / "skills" / "gsd-quick"
    files = {"agents/gsd-executor.toml": b'name = "gsd-executor"\n', "gsd-core/VERSION": b"1.14.0\n",
             "scripts/gsd-run.sh": b"#!/bin/sh\nexit 0\n", "hooks/gsd-hook.js": b"#!/usr/bin/env node\n"}
    for relative, data in {**files, "skills/gsd-quick/SKILL.md": b"# quick\n"}.items():
        path = (root / ".agents" if relative.startswith("skills/") else source) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if relative.endswith((".sh", ".js")):
            path.chmod(0o755)
    skills.mkdir(parents=True, exist_ok=True)
    (source / "hooks.json").write_text("{}\n")
    (source / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt", "OPENAI_API_KEY": None, "last_refresh": "fixture",
        "tokens": {"id_token": "fixture-id", "access_token": "fixture", "refresh_token": "excluded",
                   "account_id": "fixture-account"}}) + "\n")
    (source / "auth.json").chmod(0o600)
    owned = {relative: hashlib.sha256(data).hexdigest() for relative, data in files.items()}
    owned["skills/gsd-quick/SKILL.md"] = hashlib.sha256(b"# quick\n").hexdigest()
    (source / "gsd-file-manifest.json").write_text(json.dumps({"version": "1.14.0", "files": owned}))
    return source


def _policy(home, binary, gsd) -> dict:
    environment = host_capabilities.codex_closed_environment(
        Path(home), Path("/tmp").resolve() / "ffs-codex-policy-tmp", Path(binary), _binary_chain(Path(binary)))
    environment.update(gsd.as_dict())
    return environment


def _tuple(home, worktree, binary, gsd, seed, *, model, effort, sandbox, network_enabled):
    """The tuple a real observation would report, bound to this home, worktree and supervisor."""
    home, worktree, principal = Path(home), Path(worktree), ProcessIdentity.current()
    rinfo, winfo = home.stat(), worktree.stat()

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    runtime = {"path": str(home), "device": rinfo.st_dev, "inode": rinfo.st_ino,
               "config_sha256": digest(home / "config.toml"), "hooks_sha256": digest(home / "hooks.json"),
               **{key: hashlib.sha256(key.encode()).hexdigest() for key in (
                   "skills_sha256", "agents_sha256", "gsd_core_sha256", "scripts_sha256", "gsd_manifest_sha256")}}
    return QualifiedCodexRuntime(
        binary=tuple(sorted(_binary_chain(Path(binary)).items())), runtime=tuple(sorted(runtime.items())),
        workspace=tuple(sorted({"path": str(worktree), "device": winfo.st_dev, "inode": winfo.st_ino}.items())),
        supervisor=tuple(sorted({"host_id": principal.host_id, "boot_id": principal.boot_id,
                                 "pid": principal.pid, "start_token": principal.start_token}.items())),
        execution=tuple(sorted({"model": model, "effort": effort, "sandbox": sandbox,
                                "network_enabled": network_enabled, "roots": [str(worktree)],
                                "disabled_features": list(host_capabilities.DISABLED_NATIVE_FEATURES)}.items())),
        observation=tuple(sorted({
            "id": seed.nonce, "created_at_unix": seed.observation_created_at_unix,
            "environment_sha256": host_capabilities.preview_gsd_codex_environment_policy_hash(
                _policy(home, binary, gsd)),
            "telemetry_schema": TELEMETRY_SCHEMA}.items())))


def _probe_code(group_dir) -> str:
    """One probe's program; with ``group_dir`` armed it leaves a sleeper in its own process group."""
    code = "print(" + repr(_STREAM) + ")"
    if group_dir is None:
        return code
    flag, release, pidfile = (str(Path(group_dir) / name) for name in ("spawn", "release", "member.pid"))
    member = f"import os, time\nwhile not os.path.exists({release!r}): time.sleep(0.05)\n"
    return ("import os, subprocess, sys\n"
            f"if os.path.exists({flag!r}):\n"
            f"    os.unlink({flag!r})\n"
            f"    member = subprocess.Popen([sys.executable, '-c', {member!r}], stdin=subprocess.DEVNULL, "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"    open({pidfile!r}, 'w').write(str(member.pid))\n" + code)


class _Observer:
    """Scripted stand-in for the Codex runtime observer: four real supervised probe processes."""

    group_dir = None  # a test arms one probe to leave a live member in its process group
    QualificationResult = namedtuple("QualificationResult", "name stdout stderr exit_code")
    QualificationSeed = namedtuple("QualificationSeed", "nonce skill_token observation_created_at_unix")

    @staticmethod
    def prepare_qualification_seed(_runtime):
        return _Observer.QualificationSeed(uuid.uuid4().hex, "scripted-skill-token", time.time())

    @staticmethod
    def prepare_observer_skill(_runtime):
        return "scripted-skill-token"

    @staticmethod
    def prepare_qualification_plan(runtime, executable, worktree, observation_path, timeout, *, model, effort,
                                   sandbox, network_enabled, roots, gsd_environment, seed, preview=False,
                                   allow_existing_evidence=False):
        del roots, allow_existing_evidence
        runtime, worktree = Path(runtime), Path(worktree)
        scratch, identity = worktree / ".ffs-observer-tmp", None
        if not preview:
            scratch.mkdir(exist_ok=True)
            identity = (scratch.stat().st_dev, scratch.stat().st_ino)
        environment = {"HOME": str(runtime), "CODEX_HOME": str(runtime), "TMPDIR": str(scratch),
                       "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "NO_COLOR": "1",
                       "FFS_HOOK_OBSERVATION": str(runtime / "observer-hooks.log"), "FFS_HOOK_NONCE": seed.nonce,
                       **gsd_environment.as_dict()}
        command = (sys.executable, "-c", _probe_code(_Observer.group_dir))
        return SimpleNamespace(
            probes=tuple(_Probe(name, command, tuple(sorted(environment.items())), timeout) for name in _PROBES),
            policy_environment=tuple(sorted(_policy(runtime, executable, gsd_environment).items())),
            runtime_identity=json.dumps({"runtime": str(runtime)}),
            binary_identity=json.dumps({"binary": str(executable)}),
            workspace_identity=json.dumps({"workspace": str(worktree)}), scratch_identity=identity,
            observation_path=Path(observation_path),
            qualified=lambda: _tuple(runtime, worktree, executable, gsd_environment, seed, model=model,
                                     effort=effort, sandbox=sandbox, network_enabled=network_enabled))

    @staticmethod
    def preview_qualification_runtime(seed, runtime, executable, worktree, *, model, effort, sandbox,
                                      network_enabled, roots, gsd_environment):
        del roots
        return _tuple(runtime, worktree, executable, gsd_environment, seed, model=model, effort=effort,
                      sandbox=sandbox, network_enabled=network_enabled)

    @staticmethod
    def preview_qualified_runtime(plan):
        return plan.qualified()

    @staticmethod
    def publish_qualification_results(plan, results):
        assert tuple(item.name for item in results) == _PROBES
        record = plan.qualified().to_dict()
        plan.observation_path.write_text(json.dumps(record))
        plan.observation_path.chmod(0o600)
        return record


def _verify_runtime(home, worktree, **_kwargs):
    record = json.loads((Path(home) / "runtime-observation.json").read_text())
    return QualifiedCodexRuntime(**{field: tuple(sorted(record[field].items())) for field in _FIELDS})


def _real_host(tmp_path: Path, monkeypatch, *, group_dir=None) -> tuple[Path, Path, Path]:
    """Real staging and qualification; scripted observer, fixture host binary and admission observation."""
    monkeypatch.setattr(_Observer, "group_dir", group_dir)
    template = _template(tmp_path / "installer")
    fake = tmp_path / "qualified-codex"
    if not fake.exists():
        fake.write_text(f"#!{sys.executable}\n" + _REVIEW)
        fake.chmod(0o700)
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps({"models": [{"slug": "gpt-5.6-terra"}]}))
    monkeypatch.setattr(managed_qualification, "_observer_module", lambda: _Observer)
    monkeypatch.setattr(managed_qualification, "verify_runtime", _verify_runtime)
    monkeypatch.setattr(host_capabilities, "admit_cli", lambda _binary: {"version": "0.154.0"})
    import run_state.shared_resources as shared_resources
    import run_state.supervisor as supervisor_module
    from run_state.managed_admission import ManagedAdmissionQueue
    from run_state.resource_observation import ResourceObservation
    monkeypatch.setattr(shared_resources, "ManagedAdmissionQueue", lambda *args, **kwargs: ManagedAdmissionQueue(
        tmp_path / "managed-admission", observation_provider=lambda:
        ResourceObservation(time.monotonic_ns(), 4, 4 << 30, 4 << 30, 100, 100, {"codex": 4}, "fixture")))
    # The fixture host cannot open a real GSD wave (test_managed_lifecycle_assembly).
    monkeypatch.setattr(supervisor_module, "_gsd_wave_completion_code", lambda *_args, require_wave=True: None)
    return template, fake, catalog


def _argv(env, authority, template, fake, catalog, draft) -> list[str]:
    return [
        "frontend-start", "--frontend", "task-swarm", "--objective", "assembly", "--state-root", str(authority),
        "--upstream-runtime-manifest", env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
        "--upstream-runtime-sha256", env["FFS_UPSTREAM_RUNTIME_SHA256"],
        "--request-key", "assembly", "--run-id", "rk", "--dispatch-limit", "32", "--token-limit", "100000",
        "--select-file", "src/input.txt", "--host", "codex", "--host-runtime-home", str(template),
        "--host-binary", str(fake), "--host-model-request", '{"kind":"tier","name":"execution"}',
        "--host-sandbox", "workspace-write", "--host-network", "disabled", "--host-token-reservation", "100",
        "--host-timeout", "30", "--review-model-catalog", str(catalog), "--acceptance-draft", str(draft),
        "--scope", "1",
    ]


_DRIVER = '''import json, os, signal, sys, time
from pathlib import Path
import pytest
import test_final_review_resume_sigkill as harness
from run_state import cli
from run_state.supervisor import Supervisor

config = json.loads(Path(sys.argv[1]).read_text())
patch = pytest.MonkeyPatch()
group = config["group_dir"] and Path(config["group_dir"])
harness._real_host(Path(config["tmp_path"]), patch, group_dir=group)
qualification, review = Supervisor.launch_qualification, Supervisor.launch_native_review


def launch_qualification(self, request, *, qualification_contract):
    reviewer = request.request_key.startswith("final-review:reviewer:")
    if group and reviewer:
        (group / "spawn").touch()  # this probe leaves a sleeper in its process group
    handle = qualification(self, request, qualification_contract=qualification_contract)
    if config["point"] == "probe-released" and reviewer:
        while group and not (group / "member.pid").exists():
            time.sleep(0.02)
        os.kill(os.getpid(), signal.SIGKILL)  # the reviewer's first probe is released and running
    return handle


def launch_native_review(self, request):
    if config["point"] == "reviewer-qualified":
        os.kill(os.getpid(), signal.SIGKILL)  # the reviewer is promoted; no review action was reserved
    return review(self, request)


patch.setattr(Supervisor, "launch_qualification", launch_qualification)
patch.setattr(Supervisor, "launch_native_review", launch_native_review)
sys.exit(cli.main(config["argv"]))
'''


def _sigkilled_run(tmp_path, primary, argv, point, group_dir=None) -> None:
    """Run frontend-start in a child process that SIGKILLs itself at ``point``."""
    result = _driven_run(tmp_path, primary, argv, point, group_dir)
    assert result.returncode == -signal.SIGKILL, (result.stdout[-2000:], result.stderr[-4000:])


def _driven_run(tmp_path, primary, argv, point, group_dir=None) -> subprocess.CompletedProcess:
    """Run frontend-start in a child process; it exits (or is SIGKILLed at ``point``) and its fence is left."""
    driver, config = tmp_path / "driver.py", tmp_path / "driver.json"
    driver.write_text(_DRIVER)
    config.write_text(json.dumps({"tmp_path": str(tmp_path), "argv": argv, "point": point,
                                  "group_dir": None if group_dir is None else str(group_dir)}))
    env = {key: value for key, value in os.environ.items()
           if key not in {"GSD_RUN_ID", "FFS_RUN_ID", "GSD_RESUME", "PYTHONPATH"}}
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT / "lib"), str(ROOT / "tests")))
    return subprocess.run([sys.executable, str(driver), str(config)], cwd=primary, env=env,
                          capture_output=True, text=True, timeout=900)


def _reviewer_probes(store) -> list[dict]:
    with store.read_transaction() as tx:
        return [dict(row) for row in tx.execute(
            "SELECT i.*,a.state AS activity_state,a.request_key AS activity_key FROM authority_launch_intents i "
            "JOIN authority_qualification_launches q ON q.intent_id=i.id "
            "JOIN authority_activities a ON a.id=i.activity_id "
            "WHERE a.request_key LIKE 'final-review:reviewer%' ORDER BY a.created_at,q.created_at")]


def _wait_dead(identity: ProcessIdentity) -> None:
    deadline = time.monotonic() + 30
    while probe_identity(identity) != DEAD:
        assert time.monotonic() < deadline, "the released probe child never exited"
        time.sleep(0.05)


def _review_launches(store) -> int:
    with store.read_transaction() as tx:
        return tx.execute("SELECT count(*) FROM authority_policy_action_attempts p JOIN authority_policy_actions a "
                          "ON a.id=p.action_id WHERE a.action='final_review'").fetchone()[0]


def _facts(store, repository_id):
    state = store.get_frontend_policy_state(repository_id=repository_id, run_id="rk")
    with store.read_transaction() as tx:
        reviews = tx.execute("SELECT count(*) FROM authority_acceptance_receipts "
                             "WHERE json_extract(receipt_json,'$.role')='review'").fetchone()[0]
        grants = dict(tx.execute("SELECT action,count(*) FROM authority_policy_actions WHERE state<>'cancelled' "
                                 "AND action IN ('execute','final_review') GROUP BY action").fetchall())
        open_children = tx.execute(
            "SELECT a.request_key,a.state FROM authority_activities a JOIN authority_child_bindings b "
            "ON b.activity_id=a.id WHERE a.state NOT IN ('succeeded','failed','aborted')").fetchall()
    return SimpleNamespace(stage=state.stage, reviews=reviews, grants=grants,
                           open_children=[tuple(row) for row in open_children], review_launches=_review_launches(store))


@requires_local_confinement
@pytest.mark.parametrize("point", ["probe-released", "reviewer-qualified"])
def test_sigkilled_reviewer_qualification_resumes_through_real_qualification_to_done(tmp_path, monkeypatch, point):
    primary, authority, repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    for key in ("GSD_RUN_ID", "FFS_RUN_ID", "GSD_RESUME"):
        monkeypatch.delenv(key, raising=False)
    template, fake, catalog = _real_host(tmp_path, monkeypatch)
    argv = _argv(env, authority, template, fake, catalog, _draft(tmp_path))
    _sigkilled_run(tmp_path, primary, argv, point)

    store = ControlStore(authority / "control.sqlite3")
    crashed = _facts(store, repository_id)
    assert (crashed.stage, crashed.reviews, crashed.review_launches) == ("FINAL_REVIEW", 0, 0)
    assert _outer_intents(SimpleNamespace(store=store)) == [("completed_succeeded", "succeeded")]
    probes = _reviewer_probes(store)
    if point == "probe-released":
        # M5: the reviewer's probe was released when its owner died, and its child is really gone.
        assert [row["state"] for row in probes] == ["released_to_execute"]
        _wait_dead(ProcessIdentity(probes[0]["child_host_id"], probes[0]["child_boot_id"],
                                   probes[0]["child_pid"], probes[0]["child_start_token"]))
    else:
        # The reviewer was promoted: its home holds qualification evidence beyond the staged closure.
        assert [row["state"] for row in probes] == ["completed_succeeded"] * 4

    # The same-build resume: the identical request, real qualify_runtime closure and staging.
    assert cli.main(argv) == 0
    done = _facts(store, repository_id)
    assert (done.stage, done.reviews, done.review_launches) == ("DONE", 1, 1)
    assert done.grants == {"execute": 1, "final_review": 1} and done.open_children == []
    assert _outer_intents(SimpleNamespace(store=store)) == [("completed_succeeded", "succeeded")]
    probes = _reviewer_probes(store)
    abandoned = [row for row in probes if row["activity_key"] == "final-review:reviewer"]
    fresh = [row for row in probes if row["activity_key"] != "final-review:reviewer"]
    # The dead owner's reviewer is abandoned, never re-qualified; a fresh reviewer qualified once.
    assert {row["activity_state"] for row in abandoned} == {"aborted"}
    assert [row["state"] for row in abandoned] == (
        ["closed_dead"] if point == "probe-released" else ["completed_succeeded"] * 4)
    assert [row["state"] for row in fresh] == ["completed_succeeded"] * 4
    assert _held_resources(tmp_path) == {}
    # A terminal run replays without preparing or launching anything.
    assert cli.main(argv) == 0
    assert _review_launches(store) == 1 and len(_reviewer_probes(store)) == len(probes)


def _group_gone(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    return False


@requires_local_confinement
def test_a_live_member_of_a_dead_probes_group_blocks_the_close_until_it_exits(tmp_path, monkeypatch):
    primary, authority, repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    for key in ("GSD_RUN_ID", "FFS_RUN_ID", "GSD_RESUME"):
        monkeypatch.delenv(key, raising=False)
    group = tmp_path / "group"
    group.mkdir()
    template, fake, catalog = _real_host(tmp_path, monkeypatch, group_dir=group)
    argv = _argv(env, authority, template, fake, catalog, _draft(tmp_path))
    member = None
    try:
        _sigkilled_run(tmp_path, primary, argv, "probe-released", group_dir=group)
        member = int((group / "member.pid").read_text())
        store = ControlStore(authority / "control.sqlite3")
        [probe] = _reviewer_probes(store)
        _wait_dead(ProcessIdentity(probe["child_host_id"], probe["child_boot_id"], probe["child_pid"],
                                   probe["child_start_token"]))
        # The owner and the recorded probe child are dead; a member of the child's group still runs.
        assert probe["state"] == "released_to_execute" and not _group_gone(probe["child_pid"])

        # A refusing owner keeps its fence while a launch is unsettled; this resume runs, refuses and exits.
        resumed = _driven_run(tmp_path, primary, argv, "none", group_dir=group)
        assert resumed.returncode == 78, (resumed.stdout[-2000:], resumed.stderr[-4000:])
        assert json.loads(resumed.stdout.strip().splitlines()[-1])["code"] == "INTENT_RECONCILIATION_REQUIRED"
        refused = _facts(store, repository_id)
        assert (refused.stage, refused.reviews, refused.review_launches) == ("FINAL_REVIEW", 0, 0)
        # Nothing was closed and no fresh reviewer qualified.
        assert [(row["activity_key"], row["state"]) for row in _reviewer_probes(store)] == [
            ("final-review:reviewer", "released_to_execute")]
        with store.read_transaction() as tx:
            reviewers = tx.execute("SELECT request_key,state FROM authority_activities "
                                   "WHERE request_key LIKE 'final-review:reviewer%'").fetchall()
        assert [tuple(row) for row in reviewers] == [("final-review:reviewer", "active")]

        (group / "release").touch()
        deadline = time.monotonic() + 30
        while not _group_gone(probe["child_pid"]):
            assert time.monotonic() < deadline, "the probe group member never exited"
            time.sleep(0.05)
        assert cli.main(argv) == 0
        done = _facts(store, repository_id)
        assert (done.stage, done.reviews, done.review_launches) == ("DONE", 1, 1)
        assert [row["state"] for row in _reviewer_probes(store)] == ["closed_dead"] + ["completed_succeeded"] * 4
        assert _held_resources(tmp_path) == {}
    finally:
        (group / "release").touch()
        if member is not None:
            try:
                os.kill(member, signal.SIGKILL)
            except ProcessLookupError:
                pass


@requires_local_confinement
def test_successor_closes_only_a_dead_owners_dead_qualification_probe(tmp_path, monkeypatch):
    from run_state.ownership import OwnershipRefused, StartRequest, reserve_resources, release_owner
    primary, authority, repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    for key in ("GSD_RUN_ID", "FFS_RUN_ID", "GSD_RESUME"):
        monkeypatch.delenv(key, raising=False)
    template, fake, catalog = _real_host(tmp_path, monkeypatch)
    _sigkilled_run(tmp_path, primary, _argv(env, authority, template, fake, catalog, _draft(tmp_path)),
                   "probe-released")
    store = ControlStore(authority / "control.sqlite3")
    [probe] = _reviewer_probes(store)
    child = ProcessIdentity(probe["child_host_id"], probe["child_boot_id"], probe["child_pid"],
                            probe["child_start_token"])
    _wait_dead(child)
    with store.read_transaction() as tx:
        run = tx.execute("SELECT * FROM context_runs").fetchone()
        outer = tx.execute("SELECT id FROM authority_launch_intents WHERE capacity_exempt=1 AND NOT EXISTS "
                           "(SELECT 1 FROM authority_qualification_launches q WHERE q.intent_id="
                           "authority_launch_intents.id)").fetchone()["id"]
    token = reserve_resources(store, StartRequest(
        run["run_id"], run["workspace"], run["objective_digest"], ProcessIdentity.current(),
        repository_id=run["repository_id"], planning_scope=run["planning_scope"])).token
    live = ProcessIdentity.current()
    try:
        # A completed outer launch is not a qualification probe.
        with pytest.raises(OwnershipRefused, match="INTENT_RECONCILIATION_REQUIRED"):
            store.close_dead_qualification_intent(outer, token)
        # A probe whose child still lives is never closed.
        with store.transaction() as tx:
            tx.execute("UPDATE authority_launch_intents SET child_pid=?,child_start_token=? WHERE id=?",
                       (live.pid, live.start_token, probe["id"]))
        with pytest.raises(OwnershipRefused, match="INTENT_RECONCILIATION_REQUIRED"):
            store.close_dead_qualification_intent(probe["id"], token)
        with store.transaction() as tx:
            tx.execute("UPDATE authority_launch_intents SET child_pid=?,child_start_token=? WHERE id=?",
                       (child.pid, child.start_token, probe["id"]))
        with store.read_transaction() as tx:
            debit = dict(tx.execute("SELECT * FROM authority_launch_accounting WHERE intent_id=?",
                                    (probe["id"],)).fetchone())
        closed = store.close_dead_qualification_intent(probe["id"], token)
        assert closed.state == "closed_dead"
        with store.read_transaction() as tx:
            # Its debit stays charged; nothing about its result is adopted.
            assert dict(tx.execute("SELECT * FROM authority_launch_accounting WHERE intent_id=?",
                                   (probe["id"],)).fetchone()) == debit
            row = tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (probe["id"],)).fetchone()
        assert (row["completion_status"], row["completion_evidence_json"], row["generation"]) == (
            None, None, probe["generation"])
        # Settled once: a second close is refused, never re-applied.
        with pytest.raises(OwnershipRefused, match="INTENT_RECONCILIATION_REQUIRED"):
            store.close_dead_qualification_intent(probe["id"], token)
    finally:
        with store.transaction() as tx:
            release_owner(tx, token)
