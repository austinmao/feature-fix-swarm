"""Local check confinement effects and unsupported-platform refusal."""
from dataclasses import replace
import os
from pathlib import Path
import shlex
import sys
import socket
import tempfile
from types import SimpleNamespace

import pytest

from run_state.managed import build_frontend_acceptance_draft
from run_state.supervisor import SupervisorRefused
from test_supervised_process import setup_owner
from test_frontend_supervised_checks import requires_local_confinement


def _sealed_command_check(store, token, request, locator=None, read_roots=None):
    with store.transaction() as tx:
        tx.execute("UPDATE context_runs SET writer_version='ffs-supervisor/1', objective_digest=?, "
                   "input_digest=?, request_key=?, request_digest=? WHERE repository_id=? AND run_id=?",
                   ("a" * 64, "b" * 64, "managed-request", "c" * 64, token.repository_id, token.run_id))
        tx.execute("INSERT OR REPLACE INTO context_requests(repository_id,request_key,request_digest,run_id,created_at) "
                   "VALUES(?,?,?,?,?)", (token.repository_id, "managed-request", "c" * 64, token.run_id, "fixture"))
    legacy = store.create_initial_acceptance_contract(token, accepted_requirement_ids=["REQ-local"])
    with store.read_transaction() as tx:
        child = tx.execute("SELECT candidate_hash FROM authority_child_bindings WHERE activity_id=?", (request.activity_id,)).fetchone()
    check = {"id": "real-local", "kind": "command",
             "locator": locator or shlex.join((sys.executable, "-c", "print('must not run')"))}
    if read_roots is not None:
        check["runtime_read_roots"] = read_roots
    material = build_frontend_acceptance_draft(
        objective_digest=legacy.material["objective_digest"],
        criteria=[{"id": "REQ-local", "objective_clause": "local command",
                   "checks": [check],
                   "evidence_rules": [{"id": "local-output", "kind": "log", "required": True}]}],
        exclusions=[{"id": "no-extra", "reason": "fixture"}],
        global_invariants=[{"id": "no-commit", "reason": "fixture"}],
        requested_runtime_hash=request.runtime_identity, effective_runtime_hash=request.runtime_identity,
        candidate_hash=child["candidate_hash"], generation=legacy.generation, command_mode="feature-implement",
    )
    store.create_acceptance_draft(token, draft_id="local", revision=1,
                                  acceptance_contract_hash=legacy.contract_hash, material=material)
    sealed = store.seal_acceptance_draft(token, draft_id="local", revision=1,
                                         acceptance_contract_hash=legacy.contract_hash)
    with store.transaction() as tx:
        tx.execute("UPDATE authority_child_bindings SET contract_hash=? WHERE activity_id=?",
                   (sealed.acceptance_hash, request.activity_id))
    return sealed


def test_unsupported_platform_refuses_before_receipt_intent_or_pid(tmp_path, monkeypatch):
    supervisor, store, request = setup_owner(tmp_path)
    sealed = _sealed_command_check(store, supervisor.token, request)
    request = replace(request, contract_hash=sealed.acceptance_hash)
    monkeypatch.setattr('run_state.local_check_runtime.sys', SimpleNamespace(platform='linux'))
    with pytest.raises(SupervisorRefused, match="LOCAL_CHECK_CONFINEMENT_UNAVAILABLE"):
        supervisor.launch_sealed_check(request, acceptance_hash=sealed.acceptance_hash, check_id="real-local")
    with store.read_transaction() as tx:
        assert tx.execute("SELECT count(*) FROM authority_local_check_receipts").fetchone()[0] == 0
        assert tx.execute("SELECT count(*) FROM authority_launch_intents").fetchone()[0] == 0


@pytest.mark.skipif(sys.platform != 'darwin', reason='actual Darwin sandbox effects')
def test_registered_local_policy_denies_writes_escape_network_and_fork(tmp_path):
    from run_state.local_check_runtime import sealed_check_material, build_confined_local_argv
    from test_artifact_review_containment import _build_probe, _run
    supervisor, store, request = setup_owner(tmp_path)
    runtime = tmp_path / 'native-probe'
    runtime.mkdir(mode=0o700)
    executable = _build_probe(runtime)
    sealed = _sealed_command_check(store, supervisor.token, request, shlex.join((executable, 'thread')))
    with store.read_transaction() as tx:
        child = tx.execute('SELECT * FROM authority_child_bindings WHERE activity_id=?',
                           (request.activity_id,)).fetchone()
    material = sealed_check_material(sealed=sealed, acceptance_hash=sealed.acceptance_hash,
        check_id='real-local', candidate_hash=child['candidate_hash'], workspace=request.workspace,
        workspace_preparation_id=child['workspace_preparation_id'], expected_head=request.expected_head,
        runtime_identity=request.runtime_identity, generation=supervisor.token.generation)
    bound, _argv, policy = build_confined_local_argv(store, supervisor.token, request.activity_id, material)
    scratch = Path(bound.confinement_scratch)
    artifact = Path(request.workspace) / 'artifact.txt'
    artifact.write_text('selected public bytes')
    secret = tmp_path / 'secret.txt'
    secret.write_text('fixture sentinel')
    def run(*args):
        return _run(policy, (executable, *args), scratch).returncode
    assert run('read', str(artifact)) == 0
    assert run('copy', str(artifact), str(scratch / 'copy.txt')) == 0
    assert run('write', str(artifact)) != 0
    assert artifact.read_text() == 'selected public bytes'
    assert run('read', str(secret)) != 0
    assert run('symlink-read', str(scratch / 'escape'), str(tmp_path)) != 0
    assert run('fork') != 0
    assert run('exec', '/usr/bin/true') != 0
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        assert run('network', str(listener.getsockname()[1])) != 0



# F42: declared read roots for a sealed check's runtime.

def _draft_with_roots(roots, kind="command"):
    from run_state.run_policy import build_draft_material
    return build_draft_material(
        objective_digest="a" * 64,
        criteria=[{"id": "REQ-local", "objective_clause": "local command",
                   "checks": [{"id": "real-local", "kind": kind, "locator": "/usr/bin/true",
                               "runtime_read_roots": roots}],
                   "evidence_rules": [{"id": "local-output", "kind": "log", "required": True}]}],
        exclusions=[], global_invariants=[], requested_runtime_hash="b" * 64,
        effective_runtime_hash="b" * 64, candidate_hash="c" * 64, generation=1,
        command_mode="feature-implement")


def test_draft_accepts_declared_read_roots_only_as_unique_absolute_paths(tmp_path):
    from run_state.run_policy import RunPolicyRefused, validate_draft_material
    root = str(tmp_path.resolve())
    draft = validate_draft_material(_draft_with_roots([root]))
    assert draft.material["criteria"][0]["checks"][0]["runtime_read_roots"] == [root]
    for roots, kind in (([], "command"), (["relative/dir"], "command"), ([root, root], "command"),
                        ([root + "/"], "command"), ([7], "command"), ([root], "log")):
        with pytest.raises(RunPolicyRefused, match="POLICY_DRAFT_INVALID"):
            validate_draft_material(_draft_with_roots(roots, kind))


def test_declared_read_roots_must_be_real_canonical_directories(tmp_path):
    from run_state.local_check_runtime import LocalCheckRefused, validate_runtime_read_roots
    root = tmp_path.resolve()
    real = root / "runtime"
    real.mkdir()
    (root / "file").write_text("x")
    (root / "link").symlink_to(real)
    assert validate_runtime_read_roots((str(real),), blocked=()) == (real,)
    for roots in (("relative",), (str(root / "missing"),), (str(root / "file"),),
                  (str(root / "link"),), (str(real), str(real))):
        with pytest.raises(LocalCheckRefused, match="LOCAL_CHECK_READ_ROOT_INVALID"):
            validate_runtime_read_roots(roots, blocked=())


def test_declared_read_roots_never_overlap_home_state_or_primary(tmp_path):
    from run_state.local_check_runtime import LocalCheckRefused, validate_runtime_read_roots
    root = tmp_path.resolve()
    state, primary = root / "state", root / "primary"
    for directory in (state / "inner", primary):
        directory.mkdir(parents=True)
    blocked = (Path.home(), state, primary)
    for declared in (Path.home(), state / "inner", root):
        with pytest.raises(LocalCheckRefused, match="LOCAL_CHECK_CONFINEMENT_INVALID"):
            validate_runtime_read_roots((str(declared),), blocked=blocked)


def test_sealing_refuses_a_declared_root_that_overlaps_home(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)
    with pytest.raises(Exception, match="LOCAL_CHECK_CONFINEMENT_INVALID"):
        _sealed_command_check(store, supervisor.token, request, read_roots=[str(Path.home())])


def test_declared_read_roots_change_the_check_material_hash(tmp_path):
    from run_state.local_check_runtime import sealed_check_material
    workspace = tmp_path.resolve()
    locator = shlex.join((sys.executable, "-c", "print(1)"))

    def material(roots):
        check = {"id": "real-local", "kind": "command", "locator": locator}
        if roots is not None:
            check["runtime_read_roots"] = roots
        sealed = SimpleNamespace(acceptance_hash="a" * 64, material={"criteria": [{"checks": [check]}]})
        return sealed_check_material(
            sealed=sealed, acceptance_hash="a" * 64, check_id="real-local", candidate_hash="b" * 64,
            workspace=str(workspace), workspace_preparation_id="prep", expected_head="c" * 40,
            runtime_identity="d" * 64, generation=1)

    absent = material(None)
    declared = material(["/usr/lib"])
    assert "runtime_read_roots" not in absent.to_dict()
    assert declared.runtime_read_roots == ("/usr/lib",)
    assert declared.material_sha256 != absent.material_sha256
    assert material(["/usr/lib"]).material_sha256 == declared.material_sha256


def _host_interpreter():
    """The host's real interpreter binary and its install prefix.

    A framework build's bin/python3.x only re-execs Python.app, which an
    exact-executable sandbox forbids, so the check names that binary.
    """
    prefix = Path(sys.base_prefix).resolve()
    app = prefix / "Resources/Python.app/Contents/MacOS/Python"
    return prefix, app if app.is_file() else Path(sys.executable).resolve()


def _confined_check(tmp_path, locator, read_roots):
    from run_state.local_check_runtime import sealed_check_material, build_confined_local_argv
    tmp_path.mkdir(exist_ok=True)
    supervisor, store, request = setup_owner(tmp_path)
    sealed = _sealed_command_check(store, supervisor.token, request, locator, read_roots)
    with store.read_transaction() as tx:
        child = tx.execute('SELECT * FROM authority_child_bindings WHERE activity_id=?',
                           (request.activity_id,)).fetchone()
    material = sealed_check_material(sealed=sealed, acceptance_hash=sealed.acceptance_hash,
        check_id='real-local', candidate_hash=child['candidate_hash'], workspace=request.workspace,
        workspace_preparation_id=child['workspace_preparation_id'], expected_head=request.expected_head,
        runtime_identity=request.runtime_identity, generation=supervisor.token.generation)
    bound, argv, _policy = build_confined_local_argv(store, supervisor.token, request.activity_id, material)
    import subprocess
    completed = subprocess.run(argv, env=bound.execution_environment(), cwd=request.workspace,
                               capture_output=True, text=True, timeout=120, check=False)
    return bound, completed


@requires_local_confinement
def test_interpreter_check_runs_only_with_its_declared_read_roots(tmp_path):
    # F42 (live M3 attempt 16): an interpreter needs its install prefix
    # (framework dylib, stdlib) beside bin/, which the default runtime roots
    # omit. TMPDIR is the check's scratch, so tempfile users (pytest's
    # tmp_path included) never fall back to the read-only workspace.
    from run_state.local_check_runtime import _overlap
    prefix, interpreter = _host_interpreter()
    probe = "import sys,tempfile;tempfile.TemporaryFile().write(b'ok');print(sys.version)"
    locator = shlex.join((str(interpreter), "-c", probe))
    if _overlap(prefix, Path.home()):
        # An interpreter installed under HOME cannot be declared at all.
        with pytest.raises(Exception, match="LOCAL_CHECK_CONFINEMENT_INVALID"):
            _confined_check(tmp_path / "declared", locator, [str(prefix)])
        return
    _bound, denied = _confined_check(tmp_path / "default", locator, None)
    assert denied.returncode != 0
    bound, allowed = _confined_check(tmp_path / "declared", locator, [str(prefix)])
    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout.strip() == sys.version
    assert bound.execution_environment()["TMPDIR"] == bound.confinement_scratch


# F42 review round 1: declared roots overlap by filesystem identity, never
# name another registered workspace or the system temp dir, and every path
# error is a refusal.

def _case_alias(path):
    """``path`` spelled in the other case, or None on a case-sensitive volume."""
    alias = Path(str(path).swapcase())
    if str(alias) == str(path) or not alias.exists() or not os.path.samefile(alias, path):
        return None
    return alias


requires_case_insensitive_home = pytest.mark.skipif(
    _case_alias(Path.home()) is None, reason="HOME is on a case-sensitive filesystem")


def _owner(directory):
    directory.mkdir(parents=True)
    return setup_owner(directory)


def _register_workspace(store, token, path, *, run_id):
    path.mkdir(parents=True, exist_ok=True)
    with store.transaction() as tx:
        row = dict(tx.execute("SELECT * FROM context_workspaces WHERE repository_id=? LIMIT 1",
                              (token.repository_id,)).fetchone())
        key = f"{run_id}-{path.name}"
        row.update(preparation_id=key, run_id=run_id, path=str(path), path_key=str(path).lower(),
                   branch_key=key, child_request_key=None)
        tx.execute(f"INSERT INTO context_workspaces({','.join(row)}) VALUES({','.join('?' * len(row))})",
                   tuple(row.values()))
    return path


def _launch_material(store, supervisor, request, sealed):
    from run_state.local_check_runtime import sealed_check_material
    with store.read_transaction() as tx:
        child = tx.execute('SELECT * FROM authority_child_bindings WHERE activity_id=?',
                           (request.activity_id,)).fetchone()
    return sealed_check_material(sealed=sealed, acceptance_hash=sealed.acceptance_hash,
        check_id='real-local', candidate_hash=child['candidate_hash'], workspace=request.workspace,
        workspace_preparation_id=child['workspace_preparation_id'], expected_head=request.expected_head,
        runtime_identity=request.runtime_identity, generation=supervisor.token.generation)


def test_declared_root_overlap_is_by_filesystem_identity(tmp_path):
    from run_state.local_check_runtime import LocalCheckRefused, validate_runtime_read_roots
    # A protected root spelled through a symlink names the same directory as
    # the canonical root, equal to, inside or holding it.
    root = tmp_path.resolve()
    state = root / "state"
    (state / "inner").mkdir(parents=True)
    spelled = root / "spelled"
    spelled.symlink_to(state)
    for declared, blocked in ((state, spelled), (state / "inner", spelled), (state, spelled / "inner")):
        with pytest.raises(LocalCheckRefused, match="LOCAL_CHECK_CONFINEMENT_INVALID"):
            validate_runtime_read_roots((str(declared),), blocked=(blocked,))


@requires_case_insensitive_home
def test_sealing_refuses_an_alternate_case_home_root(tmp_path):
    # On case-insensitive APFS Path.resolve keeps the given case, so the
    # alias passes the canonical check and only identity overlap refuses it.
    alias = _case_alias(Path.home())
    supervisor, store, request = setup_owner(tmp_path)
    with pytest.raises(Exception, match="LOCAL_CHECK_CONFINEMENT_INVALID"):
        _sealed_command_check(store, supervisor.token, request, read_roots=[str(alias)])


@requires_local_confinement
def test_declared_root_never_names_another_registered_workspace(tmp_path, monkeypatch):
    # Outside HOME, another run's workspace at seal and this run's workspace
    # registered after sealing at launch. The system temp dir is moved aside
    # so only the workspace rule can refuse.
    from run_state.local_check_runtime import LocalCheckRefused, build_confined_local_argv
    (tmp_path / "system-temp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "system-temp"))
    supervisor, store, request = _owner(tmp_path / "seal")
    other = _register_workspace(store, supervisor.token, tmp_path / "other-run", run_id="other-run")
    with pytest.raises(Exception, match="LOCAL_CHECK_CONFINEMENT_INVALID"):
        _sealed_command_check(store, supervisor.token, request, read_roots=[str(other)])

    supervisor, store, request = _owner(tmp_path / "launch")
    later = tmp_path / "registered-later"
    later.mkdir()
    sealed = _sealed_command_check(store, supervisor.token, request, read_roots=[str(later)])
    material = _launch_material(store, supervisor, request, sealed)
    _register_workspace(store, supervisor.token, later, run_id=supervisor.token.run_id)
    with pytest.raises(LocalCheckRefused, match="LOCAL_CHECK_CONFINEMENT_INVALID"):
        build_confined_local_argv(store, supervisor.token, request.activity_id, material)


def test_sealing_refuses_a_root_equal_to_or_holding_the_system_temp_dir(tmp_path, monkeypatch):
    system_temp = tmp_path / "system" / "temp"
    system_temp.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(system_temp))
    for index, declared in enumerate((system_temp, system_temp.parent)):
        supervisor, store, request = _owner(tmp_path / f"owner-{index}")
        with pytest.raises(Exception, match="LOCAL_CHECK_CONFINEMENT_INVALID"):
            _sealed_command_check(store, supervisor.token, request, read_roots=[str(declared)])


@requires_local_confinement
def test_launch_refuses_a_root_holding_the_system_temp_dir(tmp_path, monkeypatch):
    # The check's scratch is made under the system temp dir at launch.
    from run_state.local_check_runtime import LocalCheckRefused, build_confined_local_argv
    declared = tmp_path / "declared"
    (declared / "temp").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "elsewhere"))
    supervisor, store, request = _owner(tmp_path / "owner")
    sealed = _sealed_command_check(store, supervisor.token, request, read_roots=[str(declared)])
    material = _launch_material(store, supervisor, request, sealed)
    monkeypatch.setattr(tempfile, "tempdir", str(declared / "temp"))
    with pytest.raises(LocalCheckRefused, match="LOCAL_CHECK_CONFINEMENT_INVALID"):
        build_confined_local_argv(store, supervisor.token, request.activity_id, material)


def test_read_root_path_errors_are_refusals(tmp_path, monkeypatch):
    from run_state.local_check_runtime import LocalCheckRefused, validate_runtime_read_roots
    real = tmp_path.resolve() / "runtime"
    real.mkdir()
    for error in (RuntimeError("symlink loop"), ValueError("embedded null")):
        def resolve(self, strict=False, _error=error):
            raise _error
        monkeypatch.setattr(Path, "resolve", resolve)
        with pytest.raises(LocalCheckRefused, match="LOCAL_CHECK_READ_ROOT_INVALID"):
            validate_runtime_read_roots((str(real),), blocked=())
