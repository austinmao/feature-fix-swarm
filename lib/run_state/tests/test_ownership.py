"""Public atomic ownership and fencing acceptance contract for M3."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import sqlite3
import stat
import subprocess
import sys
import textwrap
import threading
import time

import pytest

LIB = Path(__file__).resolve().parents[2]
_OWNED_PROCESSES: set[subprocess.Popen[str]] = set()


@pytest.fixture(autouse=True)
def _cleanup_fixture_processes():
    """Every child created by this module remains owned and bounded."""
    yield
    for process in tuple(_OWNED_PROCESSES):
        _stop_owned(process)
        _OWNED_PROCESSES.discard(process)


def _env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    for key in (
        "FFS_RUN_ID", "GSD_RUN_ID", "GSD_RESUME", "GSD_WORKSTREAM",
        "GSD_PROJECT", "GSD_SESSION_KEY", "CLAUDE_CONFIG_DIR", "GSD_HOME",
    ):
        env.pop(key, None)
    env.update(
        HOME=str(home),
        TMPDIR=str(home / "tmp"),
        PYTHONPATH=f"{LIB}:{env.get('PYTHONPATH', '')}",
    )
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    return env


_OWNER_PROGRAM = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    from run_state.ownership import (
        ControlStore, OwnershipRefused, ProcessIdentity, StartRequest,
        assert_owner, release_owner, reserve_resources,
    )

    db, run_id, workspace, objective, repository, scope, mode = sys.argv[1:]
    if mode.startswith("barrier:"):
        gate = Path(mode.removeprefix("barrier:"))
        while not gate.exists():
            import time
            time.sleep(0.005)
    store = ControlStore(Path(db))
    request = StartRequest(
        run_id, workspace, objective, ProcessIdentity.current(),
        repository_id=repository, planning_scope=scope,
    )
    try:
        owned = reserve_resources(store, request)
    except OwnershipRefused as error:
        print(json.dumps({"status": "refused", "code": error.code}), flush=True)
        raise SystemExit(3)
    print(json.dumps({"status": "ready", "run_id": owned.run_id,
                      "generation": owned.generation}), flush=True)
    if mode == "exit":
        raise SystemExit(0)
    command = sys.stdin.readline().strip()
    if command == "assert-release":
        with store.transaction() as tx:
            assert_owner(tx, owned.token)
            release_owner(tx, owned.token)
        print(json.dumps({"status": "released"}), flush=True)
    """
)


def _spawn_owner(
    tmp_path: Path,
    db: Path,
    *,
    run_id: str,
    workspace: Path,
    objective: str,
    repository: str = "repo-fixture",
    scope: str = "scope-fixture",
    mode: str = "hold",
) -> subprocess.Popen[str]:
    home = tmp_path / f"home-{run_id}-{len(list(tmp_path.glob('home-*')))}"
    process = subprocess.Popen(
        [sys.executable, "-c", _OWNER_PROGRAM, str(db), run_id, str(workspace),
         objective, repository, scope, mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_env(home),
    )
    _OWNED_PROCESSES.add(process)
    return process


def _line(process: subprocess.Popen[str], timeout: float = 10) -> dict:
    assert process.stdout is not None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=timeout), (
            f"owner result deadline exceeded; pid={process.pid} returncode={process.poll()}"
        )
    line = process.stdout.readline()
    if not line:
        diagnostic = "owner emitted no result"
        if process.poll() is not None and process.stderr is not None:
            diagnostic = process.stderr.read()
        raise AssertionError(diagnostic)
    return json.loads(line)


def _release(process: subprocess.Popen[str]) -> dict:
    assert process.stdin is not None
    process.stdin.write("assert-release\n")
    process.stdin.flush()
    result = _line(process)
    assert process.wait(timeout=10) == 0
    return result


def _stop_owned(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if process.stdin is not None:
        try:
            process.stdin.write("assert-release\n")
            process.stdin.flush()
        except BrokenPipeError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.terminate()
        process.wait(timeout=5)
    _OWNED_PROCESSES.discard(process)


def test_public_reservation_tracer(tmp_path: Path) -> None:
    """Two self-bound real owners expose atomic conflict and concurrency."""
    db = tmp_path / "authority" / "control.sqlite3"
    first = _spawn_owner(
        tmp_path, db, run_id="run-a", workspace=tmp_path / "future-a",
        objective="shared-objective",
    )
    unrelated = None
    rolled_back = None
    try:
        first_ready = _line(first)
        assert first_ready["status"] == "ready"

        contender = _spawn_owner(
            tmp_path, db, run_id="run-b", workspace=tmp_path / "future-b",
            objective="shared-objective",
        )
        refused = _line(contender)
        assert contender.wait(timeout=10) == 3
        assert refused == {"status": "refused", "code": "OWNER_LIVE"}

        rolled_back = _spawn_owner(
            tmp_path, db, run_id="run-b", workspace=tmp_path / "future-b",
            objective="objective-after-rollback", scope="rollback-scope",
        )
        assert _line(rolled_back)["status"] == "ready"
        assert _release(rolled_back) == {"status": "released"}
        rolled_back = None

        unrelated = _spawn_owner(
            tmp_path, db, run_id="run-c", workspace=tmp_path / "future-c",
            objective="unrelated-objective", scope="unrelated-scope",
        )
        assert _line(unrelated)["status"] == "ready"
        assert _release(unrelated) == {"status": "released"}
        unrelated = None
        assert _release(first) == {"status": "released"}
    finally:
        _stop_owned(first)
        if unrelated is not None:
            _stop_owned(unrelated)
        if rolled_back is not None:
            _stop_owned(rolled_back)


@pytest.mark.parametrize(
    ("first_run", "first_workspace", "first_objective", "second_run", "second_workspace", "second_objective"),
    [
        ("same-run", "future-a", "objective-a", "same-run", "future-b", "objective-b"),
        ("run-a", "same-workspace", "objective-a", "run-b", "same-workspace", "objective-b"),
        ("run-a", "future-a", "same-objective", "run-b", "future-b", "same-objective"),
    ],
    ids=("run", "workspace", "objective"),
)
def test_each_resource_conflict_has_exactly_one_live_owner(
    tmp_path: Path,
    first_run: str,
    first_workspace: str,
    first_objective: str,
    second_run: str,
    second_workspace: str,
    second_objective: str,
) -> None:
    db = tmp_path / "authority" / "control.sqlite3"
    gate = tmp_path / "start-race"
    first = _spawn_owner(tmp_path, db, run_id=first_run,
                         workspace=tmp_path / first_workspace, objective=first_objective,
                         mode=f"barrier:{gate}")
    second = _spawn_owner(tmp_path, db, run_id=second_run,
                          workspace=tmp_path / second_workspace, objective=second_objective,
                          mode=f"barrier:{gate}")
    try:
        gate.touch()
        outcomes = [_line(first), _line(second)]
        assert sum(row["status"] == "ready" for row in outcomes) == 1
        assert sum(row == {"status": "refused", "code": "OWNER_LIVE"}
                   for row in outcomes) == 1
    finally:
        _stop_owned(first)
        _stop_owned(second)


def test_third_party_identity_is_rejected_without_reserving_resources(tmp_path: Path) -> None:
    from run_state.ownership import (
        ControlStore, OwnershipRefused, ProcessIdentity, StartRequest, reserve_resources,
    )

    db = tmp_path / "authority" / "control.sqlite3"
    sleeper = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read(1)"],
                               stdin=subprocess.PIPE)
    try:
        with pytest.raises(OwnershipRefused) as rejected:
            reserve_resources(
                ControlStore(db),
                StartRequest("lent-run", str(tmp_path / "lent-workspace"), "lent-objective",
                             ProcessIdentity.from_pid(sleeper.pid)),
            )
        assert rejected.value.code == "OWNER_IDENTITY_MISMATCH"

        legitimate = reserve_resources(
            ControlStore(db),
            StartRequest("lent-run", str(tmp_path / "lent-workspace"), "lent-objective",
                         ProcessIdentity.current()),
        )
        assert legitimate.run_id == "lent-run"
    finally:
        assert sleeper.stdin is not None
        sleeper.stdin.write(b"x")
        sleeper.stdin.close()
        sleeper.wait(timeout=10)


def test_dead_self_owned_reservation_reclaims_with_higher_generation(tmp_path: Path) -> None:
    db = tmp_path / "authority" / "control.sqlite3"
    first = _spawn_owner(tmp_path, db, run_id="dead-run", workspace=tmp_path / "future",
                         objective="dead-objective", mode="exit")
    first_ready = _line(first)
    assert first.wait(timeout=10) == 0
    second = _spawn_owner(tmp_path, db, run_id="dead-run", workspace=tmp_path / "future",
                          objective="dead-objective")
    try:
        second_ready = _line(second)
        assert second_ready["generation"] > first_ready["generation"]
    finally:
        _stop_owned(second)


def test_release_retains_generation_and_fences_stale_or_wrong_nonce(tmp_path: Path) -> None:
    from run_state.ownership import (
        ControlStore, OwnershipRefused, ProcessIdentity, StartRequest,
        assert_owner, release_owner, reserve_resources,
    )

    store = ControlStore(tmp_path / "authority" / "control.sqlite3")
    request = StartRequest("fenced-run", str(tmp_path / "future"), "fenced-objective",
                           ProcessIdentity.current())
    first = reserve_resources(store, request)
    with store.transaction() as tx:
        release_owner(tx, first.token)
    second = reserve_resources(store, request)
    assert second.generation > first.generation
    for token in (first.token, dataclasses.replace(second.token, nonce="wrong")):
        with pytest.raises(OwnershipRefused) as rejected:
            with store.transaction() as tx:
                assert_owner(tx, token)
        assert rejected.value.code == "FENCE_REVOKED"


def test_owner_token_binds_role_and_complete_resource_set(tmp_path: Path) -> None:
    from run_state.ownership import (
        ControlStore, OwnershipRefused, ProcessIdentity, StartRequest,
        assert_owner, reserve_resources,
    )

    store = ControlStore(tmp_path / "authority" / "control.sqlite3")
    owned = reserve_resources(store, StartRequest(
        "bound-run", str(tmp_path / "bound-workspace"), "bound-objective",
        ProcessIdentity.current(), repository_id="bound-repository", planning_scope="bound-scope",
    ))
    assert owned.token.role == "supervisor"
    mutations = (
        dataclasses.replace(owned.token, role="worker"),
        dataclasses.replace(owned.token, run_id="other-run"),
        dataclasses.replace(owned.token, workspace=str(tmp_path / "other-workspace")),
        dataclasses.replace(owned.token, objective_digest="other-objective"),
        dataclasses.replace(owned.token, repository_id="other-repository"),
        dataclasses.replace(owned.token, planning_scope="other-scope"),
    )
    for token in mutations:
        with pytest.raises(OwnershipRefused) as rejected:
            with store.transaction() as tx:
                assert_owner(tx, token)
        assert rejected.value.code == "FENCE_REVOKED"


def test_release_and_reacquire_history_is_public_and_redacted(tmp_path: Path) -> None:
    from run_state.ownership import (
        ControlStore, ProcessIdentity, StartRequest, release_owner, reserve_resources,
    )

    store = ControlStore(tmp_path / "authority" / "control.sqlite3")
    released = reserve_resources(store, StartRequest(
        "released-run", str(tmp_path / "released-workspace"), "released-objective",
        ProcessIdentity.current(),
    ))
    with store.transaction() as tx:
        release_owner(tx, released.token)
    reacquired = reserve_resources(store, StartRequest(
        "released-run", str(tmp_path / "released-workspace"), "released-objective",
        ProcessIdentity.current(),
    ))
    assert reacquired.generation > released.generation
    events = list(store.enumerate_events(run_id="released-run"))
    assert [event["event_type"] for event in events] == [
        "resources_reserved", "resources_released", "resources_reserved",
    ]
    serialized = json.dumps(events, sort_keys=True)
    assert released.token.nonce not in serialized
    assert reacquired.token.nonce not in serialized
    assert "nonce" not in serialized.lower()


def test_unknown_probe_blocks_takeover_after_real_owner_dies(tmp_path: Path) -> None:
    from run_state.ownership import (
        ControlStore, OwnershipRefused, ProcessIdentity, StartRequest, reserve_resources,
    )

    db = tmp_path / "authority" / "control.sqlite3"
    first = _spawn_owner(tmp_path, db, run_id="unknown-run", workspace=tmp_path / "future",
                         objective="unknown-objective", mode="exit")
    assert _line(first)["status"] == "ready"
    assert first.wait(timeout=10) == 0
    uncertain = ControlStore(db, liveness_probe=lambda _identity: "UNKNOWN")
    with pytest.raises(OwnershipRefused) as rejected:
        reserve_resources(
            uncertain,
            StartRequest("unknown-run", str(tmp_path / "future"), "unknown-objective",
                         ProcessIdentity.current()),
        )
    assert rejected.value.code == "OWNER_UNKNOWN"


def test_injected_dead_cannot_override_natively_live_owner(tmp_path: Path) -> None:
    from run_state.ownership import (
        ControlStore, OwnershipRefused, ProcessIdentity, StartRequest, reserve_resources,
    )

    db = tmp_path / "authority" / "control.sqlite3"
    owner = _spawn_owner(tmp_path, db, run_id="native-live", workspace=tmp_path / "future",
                         objective="native-live-objective")
    try:
        assert _line(owner)["status"] == "ready"
        injected = ControlStore(db, liveness_probe=lambda _identity: "DEAD")
        with pytest.raises(OwnershipRefused) as rejected:
            reserve_resources(
                injected,
                StartRequest("native-live", str(tmp_path / "future"),
                             "native-live-objective", ProcessIdentity.current()),
            )
        assert rejected.value.code == "OWNER_LIVE"
    finally:
        _stop_owned(owner)


@pytest.mark.parametrize("alias_kind", ["case", "unicode-normalization"])
def test_workspace_aliases_follow_observed_filesystem_identity(
    tmp_path: Path, alias_kind: str,
) -> None:
    db = tmp_path / "authority" / "control.sqlite3"
    parent = tmp_path / "workspace-parent"; parent.mkdir()
    if alias_kind == "case":
        first_path = parent / "CaseWorkspace"
        alias_path = parent / "caseworkspace"
    else:
        first_path = parent / "caf\N{LATIN SMALL LETTER E WITH ACUTE}"
        alias_path = parent / "cafe\N{COMBINING ACUTE ACCENT}"
    first_path.mkdir()
    if alias_path.exists():
        equivalent = os.path.samefile(first_path, alias_path)
    else:
        alias_path.mkdir()
        equivalent = os.path.samefile(first_path, alias_path)

    owner = _spawn_owner(tmp_path, db, run_id=f"alias-a-{alias_kind}",
                         workspace=first_path, objective=f"objective-a-{alias_kind}")
    contender = None
    try:
        assert _line(owner)["status"] == "ready"
        contender = _spawn_owner(
            tmp_path, db, run_id=f"alias-b-{alias_kind}", workspace=alias_path,
            objective=f"objective-b-{alias_kind}", mode="exit",
        )
        observed = _line(contender)
        if equivalent:
            assert observed == {"status": "refused", "code": "OWNER_LIVE"}
            assert contender.wait(timeout=10) == 3
        else:
            assert observed["status"] == "ready"
            assert contender.wait(timeout=10) == 0
    finally:
        _stop_owned(owner)
        if contender is not None:
            _stop_owned(contender)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin pathconf seam")
def test_unknown_darwin_case_sensitivity_refuses_without_reserving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import run_state.ownership as ownership
    from run_state.ownership import (
        ControlStore, OwnershipRefused, ProcessIdentity, StartRequest, reserve_resources,
    )

    store = ControlStore(tmp_path / "authority" / "control.sqlite3")
    workspace = tmp_path / "Workspace"; workspace.mkdir()
    request = StartRequest(
        "unknown-case-policy", str(workspace), "case-policy-objective",
        ProcessIdentity.current(),
    )
    native_pathconf = ownership.os.pathconf

    def unavailable_case_policy(path: object, name: str | int) -> int:
        if name in ("PC_CASE_SENSITIVE", 11):
            raise OSError("case-sensitivity query unavailable")
        return native_pathconf(path, name)

    monkeypatch.setattr(ownership.os, "pathconf", unavailable_case_policy)
    with pytest.raises(OwnershipRefused) as rejected:
        reserve_resources(store, request)
    assert rejected.value.code == "WORKSPACE_IDENTITY_UNKNOWN"

    # A refusal must leave no held run/workspace/objective resource behind.
    monkeypatch.setattr(ownership.os, "pathconf", native_pathconf)
    admitted = reserve_resources(store, request)
    assert admitted.run_id == request.run_id


@pytest.mark.skipif(not hasattr(signal, "SIGSTOP"), reason="platform has no stop signal")
def test_stopped_then_resumed_fixture_owner_remains_live(tmp_path: Path) -> None:
    db = tmp_path / "authority" / "control.sqlite3"
    owner = _spawn_owner(tmp_path, db, run_id="paused-run", workspace=tmp_path / "missing",
                         objective="paused-objective")
    stopped = False
    try:
        assert _line(owner)["status"] == "ready"
        os.kill(owner.pid, signal.SIGSTOP)
        stopped = True
        contender = _spawn_owner(tmp_path, db, run_id="paused-run",
                                 workspace=tmp_path / "missing", objective="paused-objective")
        assert _line(contender) == {"status": "refused", "code": "OWNER_LIVE"}
        assert contender.wait(timeout=10) == 3
    finally:
        if stopped:
            os.kill(owner.pid, signal.SIGCONT)
        _stop_owned(owner)


@pytest.mark.parametrize(
    ("first_repository", "first_scope", "second_repository", "second_scope",
     "first_run", "second_run", "first_workspace", "second_workspace"),
    [
        ("repo", "scope-a", "repo", "scope-b", "same-run", "same-run", "future-a", "future-b"),
        ("repo-a", "scope-a", "repo-b", "scope-b", "run-a", "run-b", "same-workspace", "same-workspace"),
    ],
    ids=("run-cannot-evade-by-scope", "workspace-is-physical-not-namespaced"),
)
def test_namespace_changes_do_not_evade_run_or_workspace_owner(
    tmp_path: Path,
    first_repository: str,
    first_scope: str,
    second_repository: str,
    second_scope: str,
    first_run: str,
    second_run: str,
    first_workspace: str,
    second_workspace: str,
) -> None:
    db = tmp_path / "authority" / "control.sqlite3"
    first = _spawn_owner(
        tmp_path, db, run_id=first_run, workspace=tmp_path / first_workspace,
        objective="objective-a", repository=first_repository, scope=first_scope,
    )
    try:
        assert _line(first)["status"] == "ready"
        contender = _spawn_owner(
            tmp_path, db, run_id=second_run, workspace=tmp_path / second_workspace,
            objective="objective-b", repository=second_repository, scope=second_scope,
            mode="exit",
        )
        assert _line(contender) == {"status": "refused", "code": "OWNER_LIVE"}
        assert contender.wait(timeout=10) == 3
    finally:
        _stop_owned(first)


def test_distinct_resources_in_distinct_namespaces_can_overlap(tmp_path: Path) -> None:
    db = tmp_path / "authority" / "control.sqlite3"
    first = _spawn_owner(tmp_path, db, run_id="same-spelling",
                         workspace=tmp_path / "workspace-a", objective="objective-a",
                         repository="repo-a", scope="scope-a")
    second = None
    try:
        assert _line(first)["status"] == "ready"
        second = _spawn_owner(tmp_path, db, run_id="same-spelling",
                              workspace=tmp_path / "workspace-b", objective="objective-b",
                              repository="repo-b", scope="scope-b")
        assert _line(second)["status"] == "ready"
    finally:
        _stop_owned(first)
        if second is not None:
            _stop_owned(second)


def test_stalled_liveness_probe_does_not_hold_writer_and_rechecks_changed_owner(
    tmp_path: Path,
) -> None:
    """A native probe runs outside the write transaction and its snapshot is fenced."""
    from run_state.ownership import (
        ControlStore, OwnershipRefused, ProcessIdentity, StartRequest,
        release_owner, reserve_resources,
    )
    from process_identity import probe_identity

    db = tmp_path / "authority" / "control.sqlite3"
    dead = _spawn_owner(tmp_path, db, run_id="target", workspace=tmp_path / "target",
                        objective="target-objective", mode="exit")
    assert _line(dead)["status"] == "ready"
    assert dead.wait(timeout=10) == 0

    entered = threading.Event()
    release_probe = threading.Event()
    probe_calls = 0

    def blocking_probe(identity):
        nonlocal probe_calls
        probe_calls += 1
        if probe_calls == 1:
            entered.set()
            assert release_probe.wait(timeout=10)
            return "DEAD"
        return probe_identity(identity)

    result: dict[str, object] = {}

    def attempt_stale_reclaim() -> None:
        try:
            result["ownership"] = reserve_resources(
                ControlStore(db, liveness_probe=blocking_probe),
                StartRequest("target", str(tmp_path / "target"), "target-objective",
                             ProcessIdentity.current()),
            )
        except OwnershipRefused as error:
            result["code"] = error.code

    thread = threading.Thread(target=attempt_stale_reclaim, daemon=True)
    thread.start()
    assert entered.wait(timeout=10)

    # This must commit while the target probe is blocked.
    store = ControlStore(db)
    unrelated = reserve_resources(
        store,
        StartRequest("unrelated", str(tmp_path / "unrelated"), "other-objective",
                     ProcessIdentity.current(), repository_id="other-repo"),
    )
    with store.transaction() as tx:
        release_owner(tx, unrelated.token)

    replacement = _spawn_owner(tmp_path, db, run_id="target", workspace=tmp_path / "target",
                               objective="target-objective")
    try:
        assert _line(replacement)["status"] == "ready"
        release_probe.set()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert result == {"code": "OWNER_LIVE"}
    finally:
        release_probe.set()
        _stop_owned(replacement)


@pytest.mark.parametrize("case", ["inside-repository", "symlink", "public-mode", "foreign-root"])
def test_control_store_rejects_unsafe_roots_without_repairing_them(
    tmp_path: Path, case: str,
) -> None:
    from run_state.ownership import ControlStore, ControlStoreRefused

    if case == "inside-repository":
        root = tmp_path / "repo"
        root.mkdir(); (root / ".git").mkdir()
        authority = root / "authority"
        db = authority / "control.sqlite3"
        before = None
    elif case == "symlink":
        target = tmp_path / "target"; target.mkdir(mode=0o700)
        authority = tmp_path / "link"; authority.symlink_to(target, target_is_directory=True)
        db = authority / "control.sqlite3"
        before = target.stat().st_mode
    elif case == "public-mode":
        authority = tmp_path / "authority"; authority.mkdir(mode=0o755)
        authority.chmod(0o755)
        db = authority / "control.sqlite3"
        before = authority.stat().st_mode
    else:
        authority = Path("/")
        db = authority / ".ffs-m3-foreign-root-probe-control.sqlite3"
        before = authority.stat().st_mode

    with pytest.raises(ControlStoreRefused) as rejected:
        ControlStore(db)
    assert rejected.value.code == "UNSAFE_STATE_ROOT"
    assert not db.exists()
    if before is not None:
        observed = (target if case == "symlink" else authority).stat().st_mode
        assert observed == before


def test_control_store_rejects_corrupt_or_newer_schema(tmp_path: Path) -> None:
    from run_state.ownership import ControlStore, ControlStoreRefused

    corrupt_root = tmp_path / "corrupt"; corrupt_root.mkdir(mode=0o700)
    corrupt = corrupt_root / "control.sqlite3"; corrupt.write_bytes(b"not sqlite")
    corrupt_before = {entry.name: hashlib.sha256(entry.read_bytes()).hexdigest()
                      for entry in corrupt_root.iterdir()}
    with pytest.raises(ControlStoreRefused) as bad:
        ControlStore(corrupt)
    assert bad.value.code == "CORRUPT_STORE"
    assert corrupt.read_bytes() == b"not sqlite"
    assert {entry.name: hashlib.sha256(entry.read_bytes()).hexdigest()
            for entry in corrupt_root.iterdir()} == corrupt_before

    newer_root = tmp_path / "newer"; newer_root.mkdir(mode=0o700)
    newer = newer_root / "control.sqlite3"
    with sqlite3.connect(newer) as connection:
        connection.execute("PRAGMA user_version = 2147483647")
    newer.chmod(0o600)
    before = newer.read_bytes()
    newer_entries = {entry.name for entry in newer_root.iterdir()}
    with pytest.raises(ControlStoreRefused) as unsupported:
        ControlStore(newer)
    assert unsupported.value.code == "UNSUPPORTED_SCHEMA"
    assert newer.read_bytes() == before
    assert {entry.name for entry in newer_root.iterdir()} == newer_entries


def test_relative_control_store_path_is_rejected_without_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from run_state.ownership import ControlStore, ControlStoreRefused

    monkeypatch.chdir(tmp_path)
    relative = Path("relative-authority") / "control.sqlite3"
    with pytest.raises(ControlStoreRefused) as rejected:
        ControlStore(relative)
    assert rejected.value.code == "UNSAFE_STATE_ROOT"
    assert not (tmp_path / "relative-authority").exists()


def test_bare_git_repository_cannot_contain_control_authority(tmp_path: Path) -> None:
    from run_state.ownership import ControlStore, ControlStoreRefused

    bare = tmp_path / "fixture.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True,
                   capture_output=True, text=True)
    before = {entry.name for entry in bare.iterdir()}
    db = bare / "authority" / "control.sqlite3"
    with pytest.raises(ControlStoreRefused) as rejected:
        ControlStore(db)
    assert rejected.value.code == "UNSAFE_STATE_ROOT"
    assert not db.parent.exists()
    assert {entry.name for entry in bare.iterdir()} == before


def test_new_nested_authority_components_are_private_despite_ambient_umask(tmp_path: Path) -> None:
    from run_state.ownership import ControlStore

    base = tmp_path / "new-authority"
    db = base / "nested" / "control.sqlite3"
    prior = os.umask(0o022)
    try:
        ControlStore(db)
    finally:
        os.umask(prior)
    assert stat.S_IMODE(base.stat().st_mode) == 0o700
    assert stat.S_IMODE((base / "nested").stat().st_mode) == 0o700
    assert stat.S_IMODE(db.stat().st_mode) == 0o600


def test_existing_database_symlink_is_rejected_without_touching_target(tmp_path: Path) -> None:
    from run_state.ownership import ControlStore, ControlStoreRefused

    target = tmp_path / "target" / "control.sqlite3"
    ControlStore(target)
    target_before = (target.read_bytes(), target.stat().st_mode)
    link_root = tmp_path / "link-root"; link_root.mkdir(mode=0o700)
    link = link_root / "control.sqlite3"; link.symlink_to(target)
    with pytest.raises(ControlStoreRefused) as rejected:
        ControlStore(link)
    assert rejected.value.code == "UNSAFE_STATE_ROOT"
    assert link.is_symlink()
    assert (target.read_bytes(), target.stat().st_mode) == target_before


def test_existing_public_database_mode_is_rejected_without_chmod(tmp_path: Path) -> None:
    from run_state.ownership import ControlStore, ControlStoreRefused

    db = tmp_path / "authority" / "control.sqlite3"
    ControlStore(db)
    db.chmod(0o644)
    before = (db.read_bytes(), db.stat().st_mode)
    with pytest.raises(ControlStoreRefused) as rejected:
        ControlStore(db)
    assert rejected.value.code == "UNSAFE_STATE_ROOT"
    assert (db.read_bytes(), db.stat().st_mode) == before


def test_uri_special_characters_in_valid_database_filename_are_escaped(tmp_path: Path) -> None:
    from run_state.ownership import (
        ControlStore, ProcessIdentity, StartRequest, reserve_resources,
    )

    db = tmp_path / "authority" / "control?#.sqlite3"
    store = ControlStore(db)
    owned = reserve_resources(store, StartRequest(
        "uri-run", str(tmp_path / "uri-workspace"), "uri-objective",
        ProcessIdentity.current(),
    ))
    events = list(ControlStore.open_read_only(db).enumerate_events())
    assert db.is_file()
    assert any(event["event_type"] == "resources_reserved" for event in events)
    assert owned.token.nonce not in json.dumps(
        events, sort_keys=True,
    )


@pytest.mark.parametrize("node_type", ["directory", "fifo"])
def test_nonregular_database_node_is_rejected_without_blocking(
    tmp_path: Path, node_type: str,
) -> None:
    from run_state.ownership import ControlStoreRefused

    root = tmp_path / "authority"; root.mkdir(mode=0o700)
    db = root / "control.sqlite3"
    if node_type == "directory":
        db.mkdir()
    else:
        os.mkfifo(db, mode=0o600)
    program = textwrap.dedent("""
        import json, sys
        from pathlib import Path
        from run_state.ownership import ControlStore, ControlStoreRefused
        try:
            ControlStore(Path(sys.argv[1]))
        except ControlStoreRefused as error:
            print(json.dumps({"code": error.code}), flush=True)
            raise SystemExit(3)
        raise SystemExit("unsafe node accepted")
    """)
    child = subprocess.Popen(
        [sys.executable, "-c", program, str(db)], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=_env(tmp_path / f"node-{node_type}"),
    )
    _OWNED_PROCESSES.add(child)
    try:
        assert _line(child, timeout=2) == {"code": "UNSAFE_STATE_ROOT"}
        assert child.wait(timeout=2) == 3
    finally:
        _stop_owned(child)
    assert db.is_dir() if node_type == "directory" else stat.S_ISFIFO(db.lstat().st_mode)


def test_newer_schema_in_wal_mode_is_unchanged_on_refusal(tmp_path: Path) -> None:
    from run_state.ownership import ControlStore, ControlStoreRefused

    root = tmp_path / "authority"; root.mkdir(mode=0o700)
    db = root / "control.sqlite3"
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower() == "wal"
        connection.execute("PRAGMA user_version = 2147483647")
        connection.execute("CREATE TABLE retained(value TEXT)")
        connection.execute("INSERT INTO retained VALUES ('unchanged')")
    db.chmod(0o600)
    before = (db.read_bytes(), db.stat().st_mode, db.stat().st_mtime_ns,
              {entry.name for entry in root.iterdir()})
    with pytest.raises(ControlStoreRefused) as rejected:
        ControlStore(db)
    assert rejected.value.code == "UNSUPPORTED_SCHEMA"
    after = (db.read_bytes(), db.stat().st_mode, db.stat().st_mtime_ns,
             {entry.name for entry in root.iterdir()})
    assert after == before
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("SELECT value FROM retained").fetchone()[0] == "unchanged"


@pytest.mark.parametrize("component", ["database", "root"])
def test_open_store_rejects_path_replacement_instead_of_following_it(
    tmp_path: Path, component: str,
) -> None:
    from run_state.ownership import ControlStore, ControlStoreRefused

    root = tmp_path / "authority"
    db = root / "control.sqlite3"
    store = ControlStore(db)
    original = db.read_bytes()
    if component == "database":
        retained = root / "retained.sqlite3"
        db.rename(retained)
        db.write_bytes(original); db.chmod(0o600)
    else:
        retained = tmp_path / "retained-authority"
        root.rename(retained)
        root.mkdir(mode=0o700)
        db.write_bytes(original); db.chmod(0o600)
    replacement_before = db.read_bytes()
    with pytest.raises(ControlStoreRefused) as rejected:
        with store.transaction():
            pass
    assert rejected.value.code == "STORE_REPLACED"
    assert db.read_bytes() == replacement_before


@pytest.mark.parametrize("change", ["root-permissions", "ancestor-symlink"])
def test_open_store_revalidates_root_chain_before_each_transaction(
    tmp_path: Path, change: str,
) -> None:
    from run_state.ownership import ControlStore, ControlStoreRefused

    ancestor = tmp_path / "stable-parent"
    root = ancestor / "authority"
    db = root / "control.sqlite3"
    store = ControlStore(db)
    retained_bytes = db.read_bytes()
    if change == "root-permissions":
        root.chmod(0o755)
    else:
        retained = tmp_path / "retained-parent"
        ancestor.rename(retained)
        ancestor.symlink_to(retained, target_is_directory=True)
    try:
        with pytest.raises(ControlStoreRefused) as rejected:
            with store.transaction():
                pass
        assert rejected.value.code in {"UNSAFE_STATE_ROOT", "STORE_REPLACED"}
        assert db.read_bytes() == retained_bytes
    finally:
        if change == "root-permissions":
            root.chmod(0o700)


def test_same_process_reader_cannot_release_an_outer_writer_lock(
    tmp_path: Path,
) -> None:
    from run_state.ownership import (
        ControlStore, ProcessIdentity, StartRequest, release_owner, reserve_resources,
    )

    db = tmp_path / "authority" / "control.sqlite3"
    store = ControlStore(db)
    original = reserve_resources(store, StartRequest(
        "outer-writer", str(tmp_path / "outer-workspace"), "outer-objective",
        ProcessIdentity.current(),
    ))
    contender_gate = tmp_path / "contender-start"
    contender = _spawn_owner(
        tmp_path, db, run_id="external-contender",
        workspace=tmp_path / "external-workspace",
        objective="external-objective", scope="external-scope",
        mode=f"barrier:{contender_gate}",
    )
    reader_entered = threading.Event()
    reader_done = threading.Event()
    reader_errors: list[BaseException] = []

    def same_process_reader() -> None:
        reader_entered.set()
        try:
            list(ControlStore(db).enumerate_events())
            list(ControlStore.open_read_only(db).enumerate_events())
        except BaseException as error:
            reader_errors.append(error)
        finally:
            reader_done.set()

    reader = threading.Thread(target=same_process_reader, daemon=True)
    try:
        with store.transaction() as tx:
            release_owner(tx, original.token)
            reader.start()
            assert reader_entered.wait(timeout=2)
            contender_gate.touch()

            assert contender.stdout is not None
            with selectors.DefaultSelector() as selector:
                selector.register(contender.stdout, selectors.EVENT_READ)
                assert not selector.select(timeout=0.25), (
                    "external contender committed before the outer transaction"
                )
            assert not reader_done.wait(timeout=0.1), (
                "same-process reader bypassed the active writer transaction"
            )

        reader.join(timeout=10)
        assert not reader.is_alive(), "same-process reader did not resume after commit"
        assert reader_errors == []
        contender_ready = _line(contender)
        assert contender_ready["status"] == "ready"
        assert contender_ready["run_id"] == "external-contender"
        assert contender_ready["generation"] > 0
        assert _release(contender) == {"status": "released"}
        events = list(store.enumerate_events(run_id="outer-writer"))
        assert [event["event_type"] for event in events] == [
            "resources_reserved", "resources_released",
        ]
    finally:
        if reader.is_alive():
            reader.join(timeout=10)
        _stop_owned(contender)


def test_hardlink_alias_of_control_database_is_rejected_without_modification(
    tmp_path: Path,
) -> None:
    from run_state.ownership import ControlStore, ControlStoreRefused

    root = tmp_path / "authority"
    db = root / "control.sqlite3"
    ControlStore(db)
    alias = root / "control-alias.sqlite3"
    os.link(db, alias)
    before = (db.read_bytes(), alias.read_bytes(), db.stat().st_ino, db.stat().st_nlink)
    assert before[0] == before[1]
    assert before[3] == 2

    with pytest.raises(ControlStoreRefused) as rejected:
        ControlStore(alias)
    assert rejected.value.code == "UNSAFE_STATE_ROOT"
    after = (db.read_bytes(), alias.read_bytes(), db.stat().st_ino, db.stat().st_nlink)
    assert after == before
    assert os.path.samefile(db, alias)

    alias.unlink()
    assert list(ControlStore(db).enumerate_events()) == []


def test_control_store_busy_refusal_is_bounded_and_preserves_owner(tmp_path: Path) -> None:
    from run_state.ownership import (
        ControlStore, ControlStoreRefused, ProcessIdentity, StartRequest,
        assert_owner, reserve_resources,
    )

    store = ControlStore(tmp_path / "authority" / "control.sqlite3")
    owned = reserve_resources(store, StartRequest(
        "busy-run", str(tmp_path / "busy-workspace"), "busy-objective",
        ProcessIdentity.current(),
    ))
    started = time.monotonic()
    with store.transaction() as tx:
        with pytest.raises(ControlStoreRefused) as busy:
            reserve_resources(
                ControlStore(store.db_path),
                StartRequest("other-run", str(tmp_path / "other-workspace"),
                             "other-objective", ProcessIdentity.current()),
            )
        assert busy.value.code == "STORE_BUSY"
        assert time.monotonic() - started < 10
        assert_owner(tx, owned.token)


def test_read_only_events_do_not_initialize_and_redact_capabilities(tmp_path: Path) -> None:
    from run_state.ownership import (
        ControlStore, ProcessIdentity, StartRequest, reserve_resources,
    )

    missing = tmp_path / "missing" / "control.sqlite3"
    with pytest.raises(FileNotFoundError):
        ControlStore.open_read_only(missing)
    assert not missing.parent.exists()

    db = tmp_path / "authority" / "control.sqlite3"
    store = ControlStore(db)
    owned = reserve_resources(store, StartRequest(
        "events-run", str(tmp_path / "events-workspace"), "events-objective",
        ProcessIdentity.current(),
    ))
    before = (db.stat().st_size, db.stat().st_mtime_ns)
    events = list(ControlStore.open_read_only(db).enumerate_events())
    after = (db.stat().st_size, db.stat().st_mtime_ns)
    assert before == after
    assert events
    serialized = json.dumps(events, sort_keys=True)
    assert owned.token.nonce not in serialized
    assert "nonce" not in serialized.lower()


def test_concurrent_public_readers_never_misclassify_healthy_store_as_corrupt(
    tmp_path: Path,
) -> None:
    from run_state.ownership import ControlStore

    db = tmp_path / "authority" / "control.sqlite3"
    ControlStore(db)
    gate = tmp_path / "race-start"
    writer_ready = tmp_path / "writer-ready"
    reader_ready = tmp_path / "reader-ready"
    done = tmp_path / "writer-done"
    writer_program = textwrap.dedent(
        """
        import json, sys
        from pathlib import Path
        from run_state.ownership import (
            ControlStore, ProcessIdentity, StartRequest, release_owner,
            reserve_resources,
        )
        db, gate, ready, done, workspace_root = map(Path, sys.argv[1:])
        ready.touch()
        while not gate.exists():
            import time
            time.sleep(0.002)
        completed = 0
        try:
            store = ControlStore(db)
            for index in range(64):
                owned = reserve_resources(store, StartRequest(
                    f"writer-{index}", str(workspace_root / f"workspace-{index}"),
                    f"objective-{index}", ProcessIdentity.current(),
                    repository_id="writer-repository", planning_scope="writer-scope",
                ))
                with store.transaction() as tx:
                    release_owner(tx, owned.token)
                completed += 1
            print(json.dumps({"status": "ok", "writes": completed}), flush=True)
        finally:
            done.touch()
        """
    )
    reader_program = textwrap.dedent(
        """
        import json, sys
        from pathlib import Path
        from run_state.ownership import ControlStore
        db, gate, ready, done = map(Path, sys.argv[1:])
        ready.touch()
        while not gate.exists():
            import time
            time.sleep(0.002)
        scans = 0
        try:
            while not done.exists() or scans < 96:
                list(ControlStore.open_read_only(db).enumerate_events())
                list(ControlStore(db).enumerate_events())
                scans += 1
            print(json.dumps({"status": "ok", "scans": scans}), flush=True)
        except Exception as error:
            print(json.dumps({
                "status": "refused",
                "code": getattr(error, "code", type(error).__name__),
                "detail": str(error),
                "scans": scans,
            }), flush=True)
            raise SystemExit(3)
        """
    )
    writer = subprocess.Popen(
        [sys.executable, "-c", writer_program, str(db), str(gate),
         str(writer_ready), str(done), str(tmp_path / "writer-workspaces")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=_env(tmp_path / "writer-home"),
    )
    reader = subprocess.Popen(
        [sys.executable, "-c", reader_program, str(db), str(gate),
         str(reader_ready), str(done)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=_env(tmp_path / "reader-home"),
    )
    _OWNED_PROCESSES.update((writer, reader))
    try:
        deadline = time.monotonic() + 10
        while not (writer_ready.exists() and reader_ready.exists()):
            assert time.monotonic() < deadline, "reader/writer start barrier timed out"
            time.sleep(0.005)
        gate.touch()
        writer_stdout, writer_stderr = writer.communicate(timeout=30)
        reader_stdout, reader_stderr = reader.communicate(timeout=30)
        assert writer.returncode == 0, writer_stderr
        assert reader.returncode == 0, reader_stderr
        assert json.loads(writer_stdout) == {"status": "ok", "writes": 64}
        observed = json.loads(reader_stdout)
        assert observed["status"] == "ok"
        assert observed["scans"] >= 96
    finally:
        _stop_owned(writer)
        _stop_owned(reader)


def test_hot_journal_from_crashed_writer_recovers_before_new_reservation(
    tmp_path: Path,
) -> None:
    from run_state.ownership import ControlStore

    db = tmp_path / "authority" / "control.sqlite3"
    store = ControlStore(db)
    baseline = list(store.enumerate_events())
    crash_program = textwrap.dedent(
        """
        import os, signal, sys
        from pathlib import Path
        from run_state.ownership import ControlStore

        store = ControlStore(Path(sys.argv[1]))
        with store.transaction() as tx:
            tx.execute("PRAGMA cache_size = 5")
            for index in range(128):
                tx.execute(
                    "INSERT INTO control_events (event_type, payload) VALUES (?, ?)",
                    (f"uncommitted-{index}", "x" * 4096),
                )
            print('{"status":"after-spill-before-commit"}', flush=True)
            os.kill(os.getpid(), signal.SIGKILL)
        """
    )
    crashed = subprocess.Popen(
        [sys.executable, "-c", crash_program, str(db)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=_env(tmp_path / "crash-home"),
    )
    _OWNED_PROCESSES.add(crashed)
    try:
        assert _line(crashed) == {"status": "after-spill-before-commit"}
        assert crashed.wait(timeout=10) == -signal.SIGKILL
        journal = Path(f"{db}-journal")
        assert journal.is_file()
        assert journal.stat().st_size > 0

        recovery_program = textwrap.dedent(
            """
            import json, sys
            from pathlib import Path
            from run_state.ownership import (
                ControlStore, ProcessIdentity, StartRequest, reserve_resources,
            )

            db, workspace = map(Path, sys.argv[1:])
            recovered = ControlStore(db)
            before = list(recovered.enumerate_events())
            owned = reserve_resources(recovered, StartRequest(
                "post-crash-run", str(workspace), "post-crash-objective",
                ProcessIdentity.current(), repository_id="post-crash-repository",
                planning_scope="post-crash-scope",
            ))
            print(json.dumps({
                "before": before,
                "generation": owned.generation,
                "reserved_events": len(list(recovered.enumerate_events())),
            }, sort_keys=True), flush=True)
            """
        )
        recovery = subprocess.Popen(
            [sys.executable, "-c", recovery_program, str(db),
             str(tmp_path / "post-crash-workspace")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=_env(tmp_path / "recovery-home"),
        )
        _OWNED_PROCESSES.add(recovery)
        observed = _line(recovery)
        assert recovery.wait(timeout=10) == 0
        assert observed["before"] == baseline
        assert observed["generation"] > 0
        assert observed["reserved_events"] == len(baseline) + 1
    finally:
        _stop_owned(crashed)
        if "recovery" in locals():
            _stop_owned(recovery)


def test_existing_control_store_recovers_hot_journal_on_its_next_transaction(
    tmp_path: Path,
) -> None:
    from run_state.ownership import (
        ControlStore, ProcessIdentity, StartRequest, reserve_resources,
    )

    db = tmp_path / "authority" / "control.sqlite3"
    existing = ControlStore(db)
    baseline = list(existing.enumerate_events())
    crash_program = textwrap.dedent(
        """
        import os, signal, sys
        from pathlib import Path
        from run_state.ownership import ControlStore

        store = ControlStore(Path(sys.argv[1]))
        with store.transaction() as tx:
            tx.execute("PRAGMA cache_size = 5")
            for index in range(128):
                tx.execute(
                    "INSERT INTO control_events (event_type, payload) VALUES (?, ?)",
                    (f"existing-instance-uncommitted-{index}", "x" * 4096),
                )
            print('{"status":"after-spill-before-commit"}', flush=True)
            os.kill(os.getpid(), signal.SIGKILL)
        """
    )
    crashed = subprocess.Popen(
        [sys.executable, "-c", crash_program, str(db)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=_env(tmp_path / "existing-instance-crash-home"),
    )
    _OWNED_PROCESSES.add(crashed)
    try:
        assert _line(crashed) == {"status": "after-spill-before-commit"}
        assert crashed.wait(timeout=10) == -signal.SIGKILL
        journal = Path(f"{db}-journal")
        assert journal.is_file() and journal.stat().st_size > 0

        with existing.transaction() as tx:
            assert tx.execute("SELECT COUNT(*) FROM control_events").fetchone()[0] == len(baseline)
        assert list(existing.enumerate_events()) == baseline
        owned = reserve_resources(existing, StartRequest(
            "existing-instance-recovered", str(tmp_path / "existing-workspace"),
            "existing-objective", ProcessIdentity.current(),
            repository_id="existing-repository", planning_scope="existing-scope",
        ))
        assert owned.generation > 0
        assert len(list(existing.enumerate_events())) == len(baseline) + 1
    finally:
        _stop_owned(crashed)


def test_permission_loss_returns_typed_io_refusal_without_reinitializing(tmp_path: Path) -> None:
    from run_state.ownership import ControlStore, ControlStoreRefused

    root = tmp_path / "authority"
    db = root / "control.sqlite3"
    ControlStore(db)
    before = db.read_bytes()
    db.chmod(0)
    root.chmod(0o500)
    try:
        with pytest.raises(ControlStoreRefused) as denied:
            ControlStore.open_read_only(db)
        assert denied.value.code == "STORE_IO"
    finally:
        root.chmod(0o700)
        db.chmod(0o600)
    assert db.read_bytes() == before
