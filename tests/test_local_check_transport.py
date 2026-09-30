"""Local check confinement effects and unsupported-platform refusal."""
from dataclasses import replace
import functools
import os
from pathlib import Path
import selectors
import shlex
import signal
import subprocess
import sys
import socket
import tempfile
import threading
import time
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


# F46: the argv `build_confined_local_argv` returns is what runs. Both tests
# execute exactly that argv, sealed from the command they name.

@requires_local_confinement
def test_sealed_check_argv_opens_dev_null_and_no_other_device(tmp_path):
    from test_artifact_review_containment import _build_probe
    runtime = tmp_path / 'native-probe'
    runtime.mkdir(mode=0o700)
    probe = _build_probe(runtime)

    def launch(name, mode, device):
        # The probe names the device itself: a sealed command may not name a
        # file operand outside the workspace, and /dev/null is one.
        return _confined_check(tmp_path / name, shlex.join((probe, 'device', mode, device)), None)[1]
    for mode in ('read', 'write'):
        opened = launch(f'null-{mode}', mode, 'null')
        assert opened.returncode == 0, opened.stderr
        # Negative control: the grant is the one literal, not the device class.
        assert launch(f'zero-{mode}', mode, 'zero').returncode != 0
        assert launch(f'random-{mode}', mode, 'random').returncode != 0


@requires_local_confinement
def test_sealed_interpreter_opens_dev_null_as_pytest_capture_and_logging_do(tmp_path):
    from run_state.local_check_runtime import _overlap
    prefix, interpreter = _host_interpreter()
    # open(os.devnull) is pytest's capture; FileHandler(os.devnull) its logging plugin.
    # (The locator is capped at 256 bytes, hence the terse spelling.)
    probe = ('import os,logging;n=os.devnull;open(n).read();open(n,"w").write("x");'
             'logging.FileHandler(n).close()')
    locator = shlex.join((str(interpreter), "-c", probe))
    if _overlap(prefix, Path.home()):
        with pytest.raises(Exception, match="LOCAL_CHECK_CONFINEMENT_INVALID"):
            _confined_check(tmp_path, locator, [str(prefix)])
        return
    _bound, completed = _confined_check(tmp_path, locator, [str(prefix)])
    assert completed.returncode == 0, completed.stderr


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


# F42 review round 2: an unusable system temp dir is a typed refusal, and
# the protected identities are read once per batch, not once per root.

def _no_temp_dir():
    raise FileNotFoundError(2, "No usable temporary directory found")


def test_unusable_temp_dir_is_a_typed_refusal_at_seal(tmp_path, monkeypatch):
    supervisor, store, request = setup_owner(tmp_path)
    declared = tmp_path / "declared"
    declared.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", _no_temp_dir)
    with pytest.raises(Exception, match="LOCAL_CHECK_CONFINEMENT_UNAVAILABLE") as refused:
        _sealed_command_check(store, supervisor.token, request, read_roots=[str(declared)])
    assert not isinstance(refused.value, OSError)


@requires_local_confinement
def test_unusable_temp_dir_is_a_typed_refusal_at_launch(tmp_path, monkeypatch):
    from run_state.local_check_runtime import LocalCheckRefused, build_confined_local_argv
    (tmp_path / "system-temp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "system-temp"))
    supervisor, store, request = _owner(tmp_path / "owner")
    declared = tmp_path / "declared"
    declared.mkdir()
    sealed = _sealed_command_check(store, supervisor.token, request, read_roots=[str(declared)])
    material = _launch_material(store, supervisor, request, sealed)
    monkeypatch.setattr(tempfile, "gettempdir", _no_temp_dir)
    with pytest.raises(LocalCheckRefused, match="LOCAL_CHECK_CONFINEMENT_UNAVAILABLE"):
        build_confined_local_argv(store, supervisor.token, request.activity_id, material)


def test_read_root_overlap_reads_protected_identities_once_per_batch(tmp_path, monkeypatch):
    from run_state.local_check_runtime import validate_runtime_read_roots
    root = tmp_path.resolve()
    roots = [root / "roots" / f"r{index}" for index in range(10)]
    blocked = [root / "blocked" / f"b{index}" for index in range(200)]
    for directory in (*roots, *blocked):
        directory.mkdir(parents=True)
    calls = []
    for name in ("stat", "lstat"):
        monkeypatch.setattr(os, name, lambda *args, _real=getattr(os, name), **kwargs:
                            calls.append(1) or _real(*args, **kwargs))

    def stats(count):
        calls.clear()
        validate_runtime_read_roots(tuple(map(str, roots[:count])), blocked=tuple(blocked))
        return len(calls)
    one, many = stats(1), stats(len(roots))
    assert one > len(blocked)
    # Each extra root costs fewer stat calls than there are protected paths.
    assert many - one < (len(roots) - 1) * len(blocked)


def _data_volume_home():
    spelled = Path("/System/Volumes/Data" + str(Path.home()))
    return spelled.is_dir() and os.path.samefile(spelled, Path.home())


requires_data_volume_home = pytest.mark.skipif(
    not _data_volume_home(), reason="no macOS data volume spelling of HOME")


@requires_data_volume_home
def test_the_data_volume_root_still_holds_home():
    # Regression guard: the firmlink spelling refused in round 1 stays refused.
    from run_state.local_check_runtime import LocalCheckRefused, validate_runtime_read_roots
    with pytest.raises(LocalCheckRefused, match="LOCAL_CHECK_CONFINEMENT_INVALID"):
        validate_runtime_read_roots(("/System/Volumes/Data",), blocked=(Path.home(),))


# F47: a sealed check's stdout and stderr are pipes the supervisor drains into
# the evidence logs. The profile grants no file-read-metadata on the evidence
# root, so a regular-file stdio fd fails fstat with EPERM, and pytest's fd
# capture (which fstats fds 1 and 2) then closes them and exits 120. Each test
# below runs through Supervisor.launch_sealed_check, not through a bare argv.

def _interpreter_outside_home():
    from run_state.local_check_runtime import _overlap
    return not _overlap(_host_interpreter()[0], Path.home())


requires_interpreter_outside_home = pytest.mark.skipif(
    not _interpreter_outside_home(),
    reason="the host interpreter is installed under HOME, which a sealed check cannot declare as a read root")


def _launch_sealed_check(tmp_path, code, *, fault=None):
    """Return (supervisor, launch) for ``code`` as a sealed check on the real launch path."""
    prefix, interpreter = _host_interpreter()
    tmp_path.mkdir(exist_ok=True)
    supervisor, store, request = setup_owner(tmp_path, fault=fault)
    locator = shlex.join((str(interpreter), "-c", code))
    sealed = _sealed_command_check(store, supervisor.token, request, locator, [str(prefix)])
    request = replace(request, contract_hash=sealed.acceptance_hash)
    return supervisor, functools.partial(
        supervisor.launch_sealed_check, request, acceptance_hash=sealed.acceptance_hash, check_id="real-local")


def _finish_sealed_check(tmp_path, code):
    supervisor, launch = _launch_sealed_check(tmp_path, code)
    return supervisor.finish(launch(), timeout=60)


def _logs(result):
    return {key: Path(result["streams"][key]["locator"]).read_bytes() for key in ("stdout", "stderr")}


@requires_local_confinement
@requires_interpreter_outside_home
def test_sealed_check_can_fstat_its_own_stdout_and_stderr(tmp_path):
    code = 'import os,sys; os.fstat(1); os.fstat(2); print("out"); print("err", file=sys.stderr)'
    result = _finish_sealed_check(tmp_path, code)
    assert result["returncode"] == 0, _logs(result)
    assert _logs(result) == {"stdout": b"out\n", "stderr": b"err\n"}


@requires_local_confinement
@requires_interpreter_outside_home
def test_sealed_check_survives_the_fd_dance_of_pytest_capture(tmp_path):
    code = ('import os,tempfile;t=tempfile.TemporaryFile();fd=os.dup(1);os.fstat(1);'
            'os.dup2(t.fileno(),1);os.dup2(fd,1);print("ok")')
    result = _finish_sealed_check(tmp_path, code)
    assert result["returncode"] == 0, _logs(result)
    assert _logs(result)["stdout"] == b"ok\n"


@requires_local_confinement
@requires_interpreter_outside_home
def test_sealed_check_stdout_and_stderr_are_pipes(tmp_path):
    code = 'import os,stat as t;print(t.S_ISFIFO(os.fstat(1).st_mode),t.S_ISFIFO(os.fstat(2).st_mode))'
    result = _finish_sealed_check(tmp_path, code)
    assert result["returncode"] == 0, _logs(result)
    assert _logs(result)["stdout"] == b"True True\n"


@requires_local_confinement
@requires_interpreter_outside_home
def test_sealed_check_large_interleaved_output_is_copied_without_deadlock(tmp_path):
    import hashlib
    # 2 MiB on each stream, alternating 4 KiB writes: a supervisor that drains
    # one pipe at a time, or only after exit, stalls the child on the other.
    code = ('import os;os.fstat(1);os.fstat(2);b=bytes(range(256))*16;'
            '[(os.write(1,b),os.write(2,b[::-1]))for _ in range(512)]')
    result = _finish_sealed_check(tmp_path, code)
    chunk = bytes(range(256)) * 16
    expected = {"stdout": chunk * 512, "stderr": chunk[::-1] * 512}
    assert result["returncode"] == 0
    logs = _logs(result)
    assert logs == expected
    for key, raw in expected.items():
        stream = result["streams"][key]
        assert stream["bytes"] == len(raw) == 2 * 1024 * 1024
        assert stream["sha256"] == hashlib.sha256(raw).hexdigest()
        assert stream["locator"].endswith(f"/{key}.log")


def test_other_launches_keep_file_backed_stdio(tmp_path):
    supervisor, _store, request = setup_owner(tmp_path)
    code = "import os,stat;print(stat.S_ISREG(os.fstat(1).st_mode),stat.S_ISREG(os.fstat(2).st_mode))"
    handle = supervisor.launch(replace(request, command=(sys.executable, "-c", code)))
    result = supervisor.finish(handle, timeout=30, token_usage=0)
    assert result["returncode"] == 0
    assert handle.process.stdout is None and handle.process.stderr is None
    assert Path(result["streams"]["stdout"]["locator"]).read_bytes() == b"True True\n"


@requires_local_confinement
@requires_interpreter_outside_home
def test_sealed_check_stopped_mid_output_leaves_complete_closed_logs(tmp_path):
    code = 'import sys,time;print("partial",flush=True);print("oops",file=sys.stderr,flush=True);time.sleep(300)'
    supervisor, launch = _launch_sealed_check(tmp_path, code)
    handle = launch()
    try:
        with pytest.raises(SupervisorRefused, match="CHILD_DEADLINE_EXCEEDED"):
            supervisor.finish(handle, timeout=5)
    finally:
        try:
            os.killpg(handle.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        handle.process.wait(timeout=10)
    # The copy ends with the child: strict settle refuses a thread still alive.
    handle.drain.settle(exited=True, strict=True)
    assert handle.stdout_path.read_bytes() == b"partial\n"
    assert handle.stderr_path.read_bytes() == b"oops\n"


@requires_local_confinement
@requires_interpreter_outside_home
def test_sealed_check_launch_that_fails_after_spawn_leaves_no_copy_thread(tmp_path):
    def crash(point):
        if point == "after_spawn_before_ack":
            raise RuntimeError("injected crash")
    _supervisor, launch = _launch_sealed_check(tmp_path, "print(1)", fault=crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        launch()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(t.name == "ffs-pipe-drain" for t in threading.enumerate()):
        time.sleep(.05)
    assert not any(t.name == "ffs-pipe-drain" for t in threading.enumerate())


# F47 review round 1: the copy must PROVE it finished. Strict settle refuses
# logs that a copy which never started, died, or was cut off before EOF left.

@requires_local_confinement
@requires_interpreter_outside_home
def test_sealed_check_is_refused_when_the_copy_cannot_start(tmp_path, monkeypatch):
    import run_state.supervisor as supervisor_module

    def no_selector():
        raise OSError(24, "injected: too many open files")
    monkeypatch.setattr(supervisor_module, "selectors", SimpleNamespace(
        DefaultSelector=no_selector, EVENT_READ=selectors.EVENT_READ))
    supervisor, launch = _launch_sealed_check(tmp_path, 'print("out")')
    handle = launch()
    with pytest.raises(SupervisorRefused, match="EVIDENCE_CHANGED"):
        supervisor.finish(handle, timeout=60)
    assert not (handle.stdout_path.parent / "result.json").exists()


def test_drain_cut_off_before_eof_is_refused_when_strict(tmp_path):
    from run_state.supervisor import _PipeDrain
    out_read, out_write = os.pipe()
    err_read, err_write = os.pipe()
    process = SimpleNamespace(stdout=os.fdopen(out_read, "rb"), stderr=os.fdopen(err_read, "rb"))
    drain = _PipeDrain(process, open(tmp_path / "out.log", "wb"), open(tmp_path / "err.log", "wb"))
    try:
        os.write(out_write, b"partial")
        os.close(err_write)  # stderr reaches EOF; stdout's writer outlives the child
        time.sleep(.3)  # well past one 100 ms poll
        started = time.monotonic()
        drain.settle(exited=False)  # a child still running is never waited for
        assert time.monotonic() - started < 1
        with pytest.raises(SupervisorRefused, match="EVIDENCE_CHANGED"):
            drain.settle(exited=True, strict=True)
        assert (tmp_path / "out.log").read_bytes() == b"partial"
    finally:
        os.close(out_write)


@requires_local_confinement
@requires_interpreter_outside_home
def test_launch_closes_the_pipe_readers_when_the_copy_thread_cannot_start(tmp_path, monkeypatch):
    real_start, real_popen, spawned = threading.Thread.start, subprocess.Popen, []

    def start(self):
        if self.name == "ffs-pipe-drain":
            raise RuntimeError("can't start new thread")
        return real_start(self)

    def spy(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process
    _supervisor, launch = _launch_sealed_check(tmp_path, "print(1)")
    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(subprocess, "Popen", spy)
    with pytest.raises(RuntimeError, match="can't start new thread"):
        launch()
    (child,) = [process for process in spawned if "_child" in process.args]
    assert child.stdout.closed and child.stderr.closed
