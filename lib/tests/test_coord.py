"""Tests for scripts/coord/coord.py — the cross-session claims core.

`scripts/` is not a Python package (no __init__.py), so the module under
test is loaded by path rather than imported normally — mirrors the pattern
this file's sibling test_gates.py already uses for other non-package
modules. Do not add __init__.py, do not mutate sys.path, no conftest.py.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess as sp
import sys
import time
import uuid as uuid_mod
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
COORD_PATH = REPO_ROOT / "scripts" / "coord" / "coord.py"

_spec = importlib.util.spec_from_file_location("coord_under_test", COORD_PATH)
coord = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = coord  # dataclasses' postponed-annotation resolution needs this registered
_spec.loader.exec_module(coord)


# ── fixtures ─────────────────────────────────────────────────────────────
def _init_repo(path: Path) -> None:
    sp.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-q", "--allow-empty", "-m", "init"], cwd=path, check=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    _init_repo(r)
    monkeypatch.chdir(r)
    for var in ("FFS_COORD_STORE", "FFS_RUN_ID", "FFS_COORD_SESSION",
                "FFS_COORD_ANCHOR_PID", "FFS_COORD_MODE"):
        monkeypatch.delenv(var, raising=False)
    coord._IDENTITY_MOD_CACHE = "unset"
    return r


def _clear_pointer_write_wrap(monkeypatch):
    """Wrap os.open to assert no store access escapes sessions/."""
    calls = []
    real_open = os.open

    def spy(path, *a, **kw):
        calls.append((path, kw.get("dir_fd")))
        return real_open(path, *a, **kw)

    monkeypatch.setattr(os, "open", spy)
    return calls


# ── REQ-02: store resolution ─────────────────────────────────────────────
def test_store_root_resolves_worktree_to_main_checkout(tmp_path, monkeypatch):
    main = tmp_path / "main"
    main.mkdir()
    _init_repo(main)
    wt = tmp_path / "wt"
    sp.run(["git", "worktree", "add", "-q", str(wt), "-b", "side"], cwd=main, check=True)
    for var in ("FFS_COORD_STORE",):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(wt)
    resolved = coord._coord_store_root()
    assert resolved == main / ".feature-fix-swarm" / "coord"


def test_store_root_env_override_always_wins(repo, tmp_path, monkeypatch):
    custom = tmp_path / "custom-store"
    monkeypatch.setenv("FFS_COORD_STORE", str(custom))
    assert coord._coord_store_root() == custom


def test_outside_git_repo_exits_78(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FFS_COORD_STORE", raising=False)
    with pytest.raises(coord.CoordExit) as exc:
        coord._open_store()
    assert exc.value.code == 78


@pytest.mark.parametrize("leaf", ["registry.json", "registry.lock", "mode"])
def test_open_store_refuses_symlinked_leaf_files(repo, leaf):
    store = coord._open_store()
    coord._close_store(store)
    store.store_root.mkdir(parents=True, exist_ok=True)
    target = store.store_root / "elsewhere.txt"
    target.write_text("x")
    (store.store_root / leaf).symlink_to(target)
    with pytest.raises(coord.CoordExit) as exc:
        coord._open_store()
    assert exc.value.code == 78


@pytest.mark.parametrize("component", [".feature-fix-swarm", "coord", "sessions", "by-run"])
def test_open_store_refuses_symlinked_ancestor(repo, component):
    """T-01-04: a symlinked ANCESTOR, not just the leaf store root, is caught
    by the per-component openat chain — a single multi-component O_NOFOLLOW
    open would pass this and must not."""
    real_elsewhere = repo / "elsewhere"
    real_elsewhere.mkdir()
    if component == ".feature-fix-swarm":
        (repo / ".feature-fix-swarm").symlink_to(real_elsewhere)
    elif component == "coord":
        (repo / ".feature-fix-swarm").mkdir()
        (repo / ".feature-fix-swarm" / "coord").symlink_to(real_elsewhere)
    elif component == "sessions":
        (repo / ".feature-fix-swarm" / "coord").mkdir(parents=True)
        (repo / ".feature-fix-swarm" / "coord" / "sessions").symlink_to(real_elsewhere)
    else:  # by-run
        (repo / ".feature-fix-swarm" / "coord" / "sessions").mkdir(parents=True)
        (repo / ".feature-fix-swarm" / "coord" / "sessions" / "by-run").symlink_to(real_elsewhere)
    with pytest.raises(coord.CoordExit) as exc:
        coord._open_store()
    assert exc.value.code == 78


def test_lock_path_vs_dir_fd_divergence_refuses(repo):
    """T-01-17: after an ancestor rename/swap mid-invocation, the held
    coord_fd and the FileLock's path-resolved directory diverge — refuse
    rather than silently mutating two different registries."""
    store = coord._open_store()
    try:
        coord_dir = store.store_root
        moved = coord_dir.parent / "coord-moved-away"
        os.rename(coord_dir, moved)
        impostor = coord_dir
        impostor.mkdir(parents=True)
        with pytest.raises(coord.CoordExit) as exc:
            with coord.registry_transaction(store):
                pass
        assert exc.value.code == 78
        assert not (impostor / "registry.json").exists()
    finally:
        coord._close_store(store)


# ── validation (T-01-01, T-01-05, T-01-09, spec EDGE-005) ──────────────────
@pytest.mark.parametrize("bad", ["../etc", "a/b", "", "x" * 65, "a\x00b"])
def test_validate_claim_id_rejects(bad):
    with pytest.raises(coord.CoordExit) as exc:
        coord._validate_claim_id(bad)
    assert exc.value.code == 2


@pytest.mark.parametrize("good", ["spec-009", "009", "spec_009.1"])
def test_validate_claim_id_accepts(good):
    assert coord._validate_claim_id(good) == good


@pytest.mark.parametrize("bad", ["../x", "a/b", "/abs", "", "x" * 129, "a\x00b"])
def test_env_token_validation_rejects(bad):
    with pytest.raises(coord.CoordExit) as exc:
        coord._validate_env_token("FFS_RUN_ID", bad)
    assert exc.value.code == 64
    with pytest.raises(coord.CoordExit) as exc2:
        coord._validate_env_token("FFS_COORD_SESSION", bad)
    assert exc2.value.code == 64


@pytest.mark.parametrize("good", ["r1", "run-009", "run_009.2"])
def test_env_token_validation_accepts(good):
    coord._validate_env_token("FFS_RUN_ID", good)
    coord._validate_env_token("FFS_COORD_SESSION", good)


@pytest.mark.parametrize("bad", ["../../evil", "a/b", "/etc/passwd", "", "evil",
                                  "11111111-1111-1111-1111-111111111111"])
def test_by_run_pointer_content_validation_rejects(repo, bad, monkeypatch):
    """T-01-14: pointer CONTENT is untrusted input from the filesystem, not
    covered by env validation — must be checked against the strict uuid4
    form before any filesystem use.

    W5 (P-09): wired to `_clear_pointer_write_wrap` — across every rejected
    pointer content, no `os.open` is ever issued with the untrusted content
    itself as a basename (no repair, no "write it back through" attempt),
    making threat T-01-14's mechanical claim real instead of dead code.
    """
    store = coord._open_store()
    # write the pointer directly through the held descriptor, bypassing the
    # publish helper (which would itself validate on the writer's side).
    fd = os.open("r1", os.O_CREAT | os.O_WRONLY, 0o644, dir_fd=store.by_run_fd)
    with os.fdopen(fd, "w") as f:
        f.write(bad)
    calls = _clear_pointer_write_wrap(monkeypatch)
    with pytest.raises(coord.CoordExit) as exc:
        coord._read_by_run_pointer(store, "r1")
    assert exc.value.code == 69
    for path, _dir_fd in calls:
        assert os.path.basename(str(path)) != bad
    after = coord._read_text_fd(store.by_run_fd, "r1")
    assert after == bad  # left byte-identical — never repaired or reminted
    coord._close_store(store)


def test_by_run_pointer_torn_read_is_transient_not_corrupt(repo):
    """CI-observed race: the O_EXCL publish loser can read the winner's
    pointer EMPTY in the open()-to-write() window. The reader must retry
    through that transient state instead of exiting 69 (which refused a
    perfectly healthy claim on iteration 7 of the same-run-id race in CI)."""
    import threading

    store = coord._open_store()
    fd = os.open("torn", os.O_CREAT | os.O_WRONLY, 0o644, dir_fd=store.by_run_fd)
    os.close(fd)  # pointer exists, EMPTY — mid-publish state
    real_uuid = str(uuid_mod.uuid4())

    def finish_publish():
        time.sleep(0.02)
        wfd = os.open("torn", os.O_WRONLY, dir_fd=store.by_run_fd)
        try:
            os.write(wfd, (real_uuid + "\n").encode("ascii"))
        finally:
            os.close(wfd)

    t = threading.Thread(target=finish_publish)
    t.start()
    try:
        got = coord._read_by_run_pointer(store, "torn")
    finally:
        t.join()
        coord._close_store(store)
    assert got == real_uuid


def test_ttl_recording_and_validation(repo):
    assert coord._validate_ttl(None) == coord.DEFAULT_TTL_SECS
    assert coord._validate_ttl("9999") == 9999.0
    for bad in ("0", "-5", "abc", "nan", "inf", "999999999"):
        with pytest.raises(coord.CoordExit) as exc:
            coord._validate_ttl(bad)
        assert exc.value.code == 2


def test_edge_005_ttl_heartbeat_floor(repo):
    with pytest.raises(coord.CoordExit) as exc:
        coord._validate_ttl("300", "100")
    assert exc.value.code == 2
    assert coord._validate_ttl("300", "60") == 300.0


# ── identity + anchor (REQ-03, REQ-04, threat T-01-10) ──────────────────────
def test_identity_persists_across_processes_via_run_id(repo, monkeypatch):
    monkeypatch.setenv("FFS_RUN_ID", "r1")
    store = coord._open_store()
    try:
        id1 = coord.resolve_identity(store)
    finally:
        coord._close_store(store)
    store2 = coord._open_store()
    try:
        id2 = coord.resolve_identity(store2)
    finally:
        coord._close_store(store2)
    assert id1["session_uuid"] == id2["session_uuid"]

    monkeypatch.setenv("FFS_RUN_ID", "r2")
    store3 = coord._open_store()
    try:
        id3 = coord.resolve_identity(store3)
    finally:
        coord._close_store(store3)
    assert id3["session_uuid"] != id1["session_uuid"]


def test_identity_mints_fresh_each_time_with_no_run_id(repo, monkeypatch):
    monkeypatch.delenv("FFS_RUN_ID", raising=False)
    store = coord._open_store()
    try:
        id1 = coord.resolve_identity(store)
        id2 = coord.resolve_identity(store)
    finally:
        coord._close_store(store)
    assert id1["session_uuid"] != id2["session_uuid"]
    by_run_dir = store.store_root / "sessions" / "by-run"
    assert not by_run_dir.exists() or not list(by_run_dir.iterdir())


def test_anchor_pid_defaults_to_parent_and_env_wins(repo, monkeypatch):
    monkeypatch.delenv("FFS_COORD_ANCHOR_PID", raising=False)
    store = coord._open_store()
    try:
        identity = coord.resolve_identity(store)
        assert identity["anchor_pid"] == os.getppid()
        assert identity["cli_pid"] == os.getpid()
        assert identity["anchor_pid"] != identity["cli_pid"]
    finally:
        coord._close_store(store)

    store2 = coord._open_store()
    try:
        monkeypatch.setenv("FFS_COORD_ANCHOR_PID", "999999")
        monkeypatch.delenv("FFS_RUN_ID", raising=False)
        monkeypatch.delenv("FFS_COORD_SESSION", raising=False)
        identity2 = coord.resolve_identity(store2)
        assert identity2["anchor_pid"] == 999999
    finally:
        coord._close_store(store2)


def test_claim_survives_claiming_cli_process_exit(repo, monkeypatch):
    """REQ-04 regression guard: liveness is judged on the ANCHOR, never the
    coord.py CLI pid — a claim survives its own claiming command exiting.
    In-process (no FFS_COORD_ANCHOR_PID override) the anchor defaults to
    os.getppid(), which necessarily differs from this process's own pid."""
    monkeypatch.delenv("FFS_COORD_ANCHOR_PID", raising=False)
    store = coord._open_store()
    try:
        args = argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None)
        coord.cmd_claim(store, args)
        with coord.registry_transaction(store) as registry:
            entry = registry["claims"]["claim:spec-009"]
        assert entry["holder_anchor_pid"] == os.getppid()
        assert entry["cli_pid"] == os.getpid()
        assert entry["cli_pid"] != entry["holder_anchor_pid"]
    finally:
        coord._close_store(store)


def argparse_namespace(**kw):
    import argparse
    return argparse.Namespace(**kw)


def _spawn_anchor():
    """A real, killable process to stand in for a session's anchor — a
    genuinely-dead PID never captures a start token at mint, which the
    rebind rule (correctly) classifies UNPROBEABLE rather than STALE."""
    return sp.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


# ── anchor rebind (threat T-01-13, T-01-16) ─────────────────────────────
def test_anchor_rebind_on_dead_anchor_adoption(repo, monkeypatch):
    anchor = _spawn_anchor()
    monkeypatch.setenv("FFS_COORD_ANCHOR_PID", str(anchor.pid))
    monkeypatch.setenv("FFS_RUN_ID", "r1")
    store = coord._open_store()
    try:
        original = coord.resolve_identity(store)
        assert original["anchor_pid"] == anchor.pid
        assert original["anchor_start_token"] is not None
        coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
    finally:
        coord._close_store(store)

    anchor.kill()
    anchor.wait(timeout=5)

    live_pid = os.getpid()
    monkeypatch.setenv("FFS_COORD_ANCHOR_PID", str(live_pid))
    store2 = coord._open_store()
    try:
        rebound = coord.resolve_identity(store2)
        assert rebound["session_uuid"] == original["session_uuid"]
        assert rebound["anchor_pid"] == live_pid
        with coord.registry_transaction(store2) as registry:
            entry = registry["claims"]["claim:spec-009"]
        assert entry["holder_anchor_pid"] == live_pid
    finally:
        coord._close_store(store2)


def test_anchor_rebind_does_not_happen_while_anchor_alive(repo, monkeypatch):
    live_pid = os.getpid()
    monkeypatch.setenv("FFS_COORD_ANCHOR_PID", str(live_pid))
    monkeypatch.setenv("FFS_RUN_ID", "r1")
    store = coord._open_store()
    try:
        original = coord.resolve_identity(store)
    finally:
        coord._close_store(store)

    store2 = coord._open_store()
    try:
        adopted = coord.resolve_identity(store2)
    finally:
        coord._close_store(store2)
    assert adopted["anchor_pid"] == live_pid
    assert adopted["session_uuid"] == original["session_uuid"]


def test_rebind_ordering_registry_written_before_session_record(repo, monkeypatch):
    anchor = _spawn_anchor()
    monkeypatch.setenv("FFS_COORD_ANCHOR_PID", str(anchor.pid))
    monkeypatch.setenv("FFS_RUN_ID", "r1")
    store = coord._open_store()
    try:
        original = coord.resolve_identity(store)
        coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
    finally:
        coord._close_store(store)

    anchor.kill()
    anchor.wait(timeout=5)

    live_pid = os.getpid()
    monkeypatch.setenv("FFS_COORD_ANCHOR_PID", str(live_pid))

    real_atomic_write = coord._atomic_write_json
    call_count = {"n": 0}

    def boom(dir_fd, name, data):
        # let the registry write (call #1) through; fail only the session
        # record rewrite (call #2) — the crash window this ordering exists
        # to make harmless.
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise RuntimeError("simulated crash before session-record rewrite")
        return real_atomic_write(dir_fd, name, data)

    coord._atomic_write_json = boom
    store2 = coord._open_store()
    try:
        with pytest.raises(RuntimeError):
            coord.resolve_identity(store2)
    finally:
        coord._atomic_write_json = real_atomic_write
        coord._close_store(store2)

    store3 = coord._open_store()
    try:
        with coord.registry_transaction(store3) as registry:
            entry = registry["claims"]["claim:spec-009"]
        assert entry["holder_anchor_pid"] == live_pid
    finally:
        coord._close_store(store3)


# ── registry persistence + atomic save (P-04, layer 4) ─────────────────────
def test_fresh_registry_has_four_top_level_keys_and_first_gen(repo):
    store = coord._open_store()
    try:
        coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
        with coord.registry_transaction(store) as registry:
            assert set(registry.keys()) == {"version", "claims", "leases", "generations"}
            assert registry["generations"]["claim:spec-009"]["gen"] == 1
    finally:
        coord._close_store(store)


def test_atomic_save_temp_file_is_o_excl(repo):
    store = coord._open_store()
    try:
        coord._save_registry(store, {"version": 1, "claims": {}, "leases": {}, "generations": {}})
        collision_calls = []
        real_secrets_token_hex = coord.secrets.token_hex

        def fixed_token(_n):
            collision_calls.append(1)
            return "deadbeefdeadbeef"

        coord.secrets.token_hex = fixed_token
        try:
            colliding = f".registry.json-{'deadbeefdeadbeef'}.tmp"
            fd = os.open(colliding, os.O_CREAT | os.O_WRONLY, 0o644, dir_fd=store.coord_fd)
            os.write(fd, b"pre-existing")
            os.close(fd)
            with pytest.raises(FileExistsError):
                coord._save_registry(store, {"version": 1, "claims": {}, "leases": {}, "generations": {}})
            content = coord._read_text_fd(store.coord_fd, colliding)
            assert content == "pre-existing"
        finally:
            coord.secrets.token_hex = real_secrets_token_hex
            os.unlink(colliding, dir_fd=store.coord_fd)
    finally:
        coord._close_store(store)


# ── Task 2: idempotency + no second artifact (P-04) ─────────────────────────
def test_idempotent_reclaim_same_uuid_leaves_generation_unchanged(repo, monkeypatch):
    monkeypatch.setenv("FFS_RUN_ID", "r1")
    store = coord._open_store()
    try:
        coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
        coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
        with coord.registry_transaction(store) as registry:
            assert registry["claims"]["claim:spec-009"]["generation"] == 1
            assert registry["generations"]["claim:spec-009"]["gen"] == 1
    finally:
        coord._close_store(store)


def test_foreign_fresh_claim_is_refused_not_overwritten(repo, monkeypatch):
    monkeypatch.setenv("FFS_RUN_ID", "holder")
    store = coord._open_store()
    try:
        coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
    finally:
        coord._close_store(store)

    monkeypatch.setenv("FFS_RUN_ID", "other")
    store2 = coord._open_store()
    try:
        rc = coord.cmd_claim(store2, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
        assert rc == 3
        with coord.registry_transaction(store2) as registry:
            assert registry["claims"]["claim:spec-009"]["generation"] == 1
    finally:
        coord._close_store(store2)


def test_no_second_artifact_only_registry_and_sessions_on_disk(repo, monkeypatch):
    """P-04: the registry claim entry is the SOLE claim record — no
    claims/ directory, no per-resource lease marker file."""
    monkeypatch.setenv("FFS_RUN_ID", "r1")
    store = coord._open_store()
    try:
        coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
    finally:
        coord._close_store(store)
    entries = {p.name for p in store.store_root.iterdir()}
    assert entries <= {"registry.json", "registry.lock", "sessions", "mode"}
    assert "claims" not in entries


# ── must_haves.truths — one named test per truth this task can prove ───────
def test_must_have_req02_single_store_per_repo(tmp_path, monkeypatch):
    """REQ-02: every coord path resolves through git-common-dir to ONE store
    per repo; a symlinked store root is refused with exit 78 before any
    byte is written."""
    main = tmp_path / "main"
    main.mkdir()
    _init_repo(main)
    wt = tmp_path / "wt"
    sp.run(["git", "worktree", "add", "-q", str(wt), "-b", "side"], cwd=main, check=True)
    monkeypatch.delenv("FFS_COORD_STORE", raising=False)
    monkeypatch.chdir(main)
    root_from_main = coord._coord_store_root()
    monkeypatch.chdir(wt)
    root_from_wt = coord._coord_store_root()
    assert root_from_main == root_from_wt


# ── Task 3: staleness, fencing, release authorization, doctor/status ───────
def _spawn_dead_pid_source():
    """A real subprocess whose pid we can capture a start token for, then
    kill — the only way to produce an entry with a non-null recorded token
    whose pid later proves dead (STALE), as opposed to a pid that was never
    alive (UNPROBEABLE, null token from birth)."""
    return sp.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def test_anchor_is_stale_verdicts(repo, monkeypatch):
    live_pid = os.getpid()
    live_token = coord._capture_start_token(live_pid)
    assert coord._anchor_is_stale(live_pid, coord._host_name(), live_token) == "live"

    proc = _spawn_dead_pid_source()
    token = coord._capture_start_token(proc.pid)
    assert token is not None
    proc.kill()
    proc.wait(timeout=5)
    assert coord._anchor_is_stale(proc.pid, coord._host_name(), token) == "stale"

    # unprobeable: null fields, foreign host, degraded module
    assert coord._anchor_is_stale(None, coord._host_name(), 1) == "unprobeable"
    assert coord._anchor_is_stale(live_pid, "some-other-host", live_token) == "unprobeable"
    assert coord._anchor_is_stale(live_pid, coord._host_name(), None) == "unprobeable"

    saved = coord._IDENTITY_MOD_CACHE
    coord._IDENTITY_MOD_CACHE = None
    try:
        assert coord._anchor_is_stale(live_pid, coord._host_name(), live_token) == "unprobeable"
    finally:
        coord._IDENTITY_MOD_CACHE = saved


def test_anchor_is_stale_unreadable_token_is_unprobeable_not_live_or_stale(repo, monkeypatch):
    """The branch REQ-04 exists to get right: an unreadable token for a
    LIVE pid is neither proof of life nor proof of death."""
    live_pid = os.getpid()
    real_mod = coord._identity_module()

    class _FakeMod:
        host_name = staticmethod(real_mod.host_name)
        process_alive = staticmethod(real_mod.process_alive)

        @staticmethod
        def process_start_token(pid):
            return None  # unreadable for every pid, simulating a probe failure

    coord._IDENTITY_MOD_CACHE = _FakeMod
    try:
        verdict = coord._anchor_is_stale(live_pid, coord._host_name(), 12345)
        assert verdict == "unprobeable"
    finally:
        coord._IDENTITY_MOD_CACHE = real_mod


def _hand_stamp_claim(store, claim_id, **overrides):
    key = coord._claim_key(claim_id)
    with coord.registry_transaction(store) as registry:
        gen_entry = registry["generations"].setdefault(
            key, {"gen": 0, "last_holder_uuid": None, "released_at": None}
        )
        now = time.time()
        gen_entry["gen"] = max(gen_entry["gen"], 1)
        entry = {
            "holder_uuid": "foreign-uuid",
            "holder_anchor_pid": None,
            "holder_anchor_start_token": None,
            "holder_host": coord._host_name(),
            "holder_worktree": str(store.store_root.parent.parent),
            "generation": gen_entry["gen"],
            "acquired_at": now,
            "last_renewed_at": now,
            "ttl_secs": coord.DEFAULT_TTL_SECS,
            "expires_at": now + coord.DEFAULT_TTL_SECS,
            "cli_pid": 0,
        }
        entry.update(overrides)
        registry["claims"][key] = entry
        coord._save_registry(store, registry)
    return entry


def test_reclaim_decision_worktree_gone_is_immediately_reclaimable(repo, monkeypatch):
    monkeypatch.setenv("FFS_RUN_ID", "peer")
    store = coord._open_store()
    try:
        _hand_stamp_claim(
            store, "spec-009",
            holder_worktree=str(store.store_root / "nonexistent-worktree"),
            holder_anchor_pid=os.getpid(),
            holder_anchor_start_token=coord._capture_start_token(os.getpid()),
        )
        rc = coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
        assert rc == 0
        with coord.registry_transaction(store) as registry:
            assert registry["claims"]["claim:spec-009"]["generation"] == 2
    finally:
        coord._close_store(store)


def test_reclaim_decision_stale_anchor_is_reclaimable(repo, monkeypatch):
    proc = _spawn_dead_pid_source()
    token = coord._capture_start_token(proc.pid)
    monkeypatch.setenv("FFS_RUN_ID", "peer")
    store = coord._open_store()
    try:
        _hand_stamp_claim(
            store, "spec-009",
            holder_anchor_pid=proc.pid,
            holder_anchor_start_token=token,
            holder_worktree=str(store.store_root.parent.parent),
        )
        proc.kill()
        proc.wait(timeout=5)
        rc = coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
        assert rc == 0
        with coord.registry_transaction(store) as registry:
            assert registry["claims"]["claim:spec-009"]["generation"] == 2
    finally:
        coord._close_store(store)


@pytest.mark.parametrize("multiplier", [1, 2, 10])
def test_reclaim_decision_live_matching_anchor_never_reclaimable_at_any_age(repo, monkeypatch, multiplier):
    live_pid = os.getpid()
    token = coord._capture_start_token(live_pid)
    monkeypatch.setenv("FFS_RUN_ID", "peer")
    store = coord._open_store()
    try:
        ttl = 60.0
        _hand_stamp_claim(
            store, "spec-009",
            holder_anchor_pid=live_pid,
            holder_anchor_start_token=token,
            holder_worktree=str(store.store_root.parent.parent),
            ttl_secs=ttl,
            last_renewed_at=time.time() - (multiplier * ttl),
        )
        rc = coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
        assert rc == 3
        with coord.registry_transaction(store) as registry:
            assert registry["claims"]["claim:spec-009"]["generation"] == 1
    finally:
        coord._close_store(store)


def test_reclaim_decision_unprobeable_bounded_by_own_ttl_secs(repo, monkeypatch):
    """Per-claim ttl_secs governs reclaim, never DEFAULT_TTL_SECS directly:
    a --ttl 9999 claim survives past DEFAULT_TTL_SECS; a --ttl 30 claim
    does not survive to DEFAULT_TTL_SECS."""
    monkeypatch.setenv("FFS_RUN_ID", "peer")
    store = coord._open_store()
    try:
        # long TTL: aged past DEFAULT_TTL_SECS but well under its own ttl_secs
        _hand_stamp_claim(
            store, "spec-009",
            holder_anchor_pid=None,
            holder_anchor_start_token=None,
            holder_worktree=str(store.store_root.parent.parent),
            ttl_secs=9999.0,
            last_renewed_at=time.time() - (coord.DEFAULT_TTL_SECS + 5),
        )
        rc = coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
        assert rc == 3  # not reclaimable — comparing against DEFAULT_TTL_SECS would wrongly reclaim here
    finally:
        coord._close_store(store)

    store2 = coord._open_store()
    try:
        # short TTL: past its OWN ttl_secs, well under DEFAULT_TTL_SECS
        _hand_stamp_claim(
            store2, "spec-030",
            holder_anchor_pid=None,
            holder_anchor_start_token=None,
            holder_worktree=str(store2.store_root.parent.parent),
            ttl_secs=30.0,
            last_renewed_at=time.time() - 60,
        )
        rc = coord.cmd_claim(store2, argparse_namespace(spec_id="spec-030", ttl=None, heartbeat=None))
        assert rc == 0  # reclaimed on its own short schedule, not DEFAULT_TTL_SECS
    finally:
        coord._close_store(store2)


def test_crash_recovery_torn_entry_reclaimable_once_ttl_since_acquired(repo, monkeypatch):
    monkeypatch.setenv("FFS_RUN_ID", "peer")
    store = coord._open_store()
    try:
        key = coord._claim_key("spec-009")
        with coord.registry_transaction(store) as registry:
            registry["generations"][key] = {"gen": 1, "last_holder_uuid": None, "released_at": None}
            registry["claims"][key] = {
                "holder_uuid": "torn",
                "holder_anchor_pid": None,
                "holder_worktree": str(store.store_root.parent.parent),
                "acquired_at": time.time() - (coord.DEFAULT_TTL_SECS + 5),
                # last_renewed_at and ttl_secs both absent — torn record
            }
            coord._save_registry(store, registry)
        rc = coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
        assert rc == 0
    finally:
        coord._close_store(store)


def test_generation_persists_across_release_1_2_3(repo, monkeypatch):
    monkeypatch.setenv("FFS_RUN_ID", "r1")
    store = coord._open_store()
    try:
        for expected_gen in (1, 2, 3):
            rc = coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
            assert rc == 0
            with coord.registry_transaction(store) as registry:
                assert registry["claims"]["claim:spec-009"]["generation"] == expected_gen
            rc = coord.cmd_release(
                store, argparse_namespace(spec_id="spec-009", generation=expected_gen)
            )
            assert rc == 0
            with coord.registry_transaction(store) as registry:
                assert registry["generations"]["claim:spec-009"]["gen"] == expected_gen
    finally:
        coord._close_store(store)


def test_claim_check_ok_and_superseded(repo, monkeypatch):
    monkeypatch.setenv("FFS_RUN_ID", "r1")
    store = coord._open_store()
    try:
        coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
        rc_ok = coord.cmd_claim_check(store, argparse_namespace(spec_id="spec-009", generation="1"))
        assert rc_ok == 0
        rc_bad = coord.cmd_claim_check(store, argparse_namespace(spec_id="spec-009", generation="2"))
        assert rc_bad == 4
        with pytest.raises(coord.CoordExit) as exc:
            coord.cmd_claim_check(store, argparse_namespace(spec_id="spec-009", generation="abc"))
        assert exc.value.code == 2
    finally:
        coord._close_store(store)


def test_claim_renew_fencing_stale_process_same_uuid(repo, monkeypatch):
    """REQ-04 renewal fencing: holder UUID alone is not proof of generation
    — a stale process sharing the holder UUID at an old generation must be
    refused with exit 4, and must not bump the clock."""
    monkeypatch.setenv("FFS_RUN_ID", "r1")
    store = coord._open_store()
    try:
        coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
        key = coord._claim_key("spec-009")
        with coord.registry_transaction(store) as registry:
            registry["claims"][key]["generation"] = 2
            registry["generations"][key]["gen"] = 2
            frozen_renewed_at = registry["claims"][key]["last_renewed_at"] = time.time() - 100
            coord._save_registry(store, registry)

        rc = coord.cmd_claim_renew(
            store, argparse_namespace(spec_id="spec-009", generation="1", ttl=None, heartbeat=None)
        )
        assert rc == 4
        with coord.registry_transaction(store) as registry:
            assert registry["claims"][key]["last_renewed_at"] == frozen_renewed_at
            assert registry["claims"][key]["generation"] == 2

        rc_ok = coord.cmd_claim_renew(
            store, argparse_namespace(spec_id="spec-009", generation="2", ttl=None, heartbeat=None)
        )
        assert rc_ok == 0
        with coord.registry_transaction(store) as registry:
            assert registry["claims"][key]["last_renewed_at"] != frozen_renewed_at
    finally:
        coord._close_store(store)


def test_claim_renew_foreign_holder_refused(repo, monkeypatch):
    monkeypatch.setenv("FFS_RUN_ID", "holder")
    store = coord._open_store()
    try:
        coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
    finally:
        coord._close_store(store)
    monkeypatch.setenv("FFS_RUN_ID", "other")
    store2 = coord._open_store()
    try:
        rc = coord.cmd_claim_renew(
            store2, argparse_namespace(spec_id="spec-009", generation="1", ttl=None, heartbeat=None)
        )
        assert rc == 3
    finally:
        coord._close_store(store2)


def test_release_present_entry_foreign_and_stale_generation_refused(repo, monkeypatch):
    monkeypatch.setenv("FFS_RUN_ID", "holder")
    store = coord._open_store()
    try:
        coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
    finally:
        coord._close_store(store)

    monkeypatch.setenv("FFS_RUN_ID", "other")
    store2 = coord._open_store()
    try:
        rc = coord.cmd_release(store2, argparse_namespace(spec_id="spec-009", generation=1))
        assert rc == 3
    finally:
        coord._close_store(store2)

    monkeypatch.setenv("FFS_RUN_ID", "holder")
    store3 = coord._open_store()
    try:
        rc = coord.cmd_release(store3, argparse_namespace(spec_id="spec-009", generation=99))
        assert rc == 3
        rc_ok = coord.cmd_release(store3, argparse_namespace(spec_id="spec-009", generation=1))
        assert rc_ok == 0
    finally:
        coord._close_store(store3)


def test_release_absent_entry_idempotent_for_last_holder_refused_for_others(repo, monkeypatch):
    monkeypatch.setenv("FFS_RUN_ID", "holder")
    store = coord._open_store()
    try:
        coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
        rc = coord.cmd_release(store, argparse_namespace(spec_id="spec-009", generation=1))
        assert rc == 0
        # idempotent second release by the same (last) holder
        rc2 = coord.cmd_release(store, argparse_namespace(spec_id="spec-009", generation=1))
        assert rc2 == 0
    finally:
        coord._close_store(store)

    monkeypatch.setenv("FFS_RUN_ID", "somebody-else")
    store2 = coord._open_store()
    try:
        rc3 = coord.cmd_release(store2, argparse_namespace(spec_id="spec-009", generation=1))
        assert rc3 == 3
    finally:
        coord._close_store(store2)


def test_borrowed_identity_refused_via_session_env_exempt_via_run_id(repo, monkeypatch):
    """EDGE-006/T-01-02: a live foreign anchor is refused only through
    resolution step (i), $FFS_COORD_SESSION — step (ii), the by-run
    pointer, is exempt by design (same run id == same logical session)."""
    anchor = _spawn_anchor()
    monkeypatch.setenv("FFS_COORD_ANCHOR_PID", str(anchor.pid))
    monkeypatch.setenv("FFS_RUN_ID", "peer-run")
    store = coord._open_store()
    try:
        peer_identity = coord.resolve_identity(store)
    finally:
        coord._close_store(store)

    try:
        monkeypatch.setenv("FFS_COORD_ANCHOR_PID", str(os.getpid()))
        monkeypatch.delenv("FFS_RUN_ID", raising=False)
        monkeypatch.setenv("FFS_COORD_SESSION", peer_identity["session_uuid"])
        store2 = coord._open_store()
        try:
            with pytest.raises(coord.CoordExit) as exc:
                coord.resolve_identity(store2)
            assert exc.value.code == 78
        finally:
            coord._close_store(store2)

        # step (ii), same run id: exempt, resolves normally (adopts unchanged
        # since the anchor is still alive).
        monkeypatch.delenv("FFS_COORD_SESSION", raising=False)
        monkeypatch.setenv("FFS_RUN_ID", "peer-run")
        store3 = coord._open_store()
        try:
            adopted = coord.resolve_identity(store3)
            assert adopted["session_uuid"] == peer_identity["session_uuid"]
        finally:
            coord._close_store(store3)
    finally:
        anchor.kill()
        anchor.wait(timeout=5)


def test_borrowed_identity_not_refused_when_anchor_is_dead_or_own(repo, monkeypatch):
    """Re-exporting a session= value emitted by a finished invocation must
    keep working — only a LIVE, FOREIGN anchor is refused."""
    monkeypatch.delenv("FFS_RUN_ID", raising=False)
    store = coord._open_store()
    try:
        finished = coord.resolve_identity(store)
    finally:
        coord._close_store(store)

    monkeypatch.setenv("FFS_COORD_SESSION", finished["session_uuid"])
    store2 = coord._open_store()
    try:
        # the anchor default here (os.getppid()) is THIS invocation's own
        # anchor most of the time in a pytest run, but even so this must
        # not raise — re-exporting is always safe when the anchor is dead
        # or already ours.
        adopted = coord.resolve_identity(store2)
        assert adopted["session_uuid"] == finished["session_uuid"]
    finally:
        coord._close_store(store2)


def test_mode_resolution_precedence_and_invalid_value(repo, monkeypatch):
    store = coord._open_store()
    try:
        assert coord._resolve_mode(store) == "enforce"
        coord._atomic_write_json  # sanity: helper exists
        fd = os.open("mode", os.O_CREAT | os.O_WRONLY, 0o644, dir_fd=store.coord_fd)
        with os.fdopen(fd, "w") as f:
            f.write("audit\n")
        assert coord._resolve_mode(store) == "audit"

        monkeypatch.setenv("FFS_COORD_MODE", "off")
        assert coord._resolve_mode(store) == "off"

        monkeypatch.setenv("FFS_COORD_MODE", "bogus")
        with pytest.raises(coord.CoordExit) as exc:
            coord._resolve_mode(store)
        assert exc.value.code == 2
    finally:
        coord._close_store(store)


def test_edge_003_unparseable_registry_enforce_vs_audit(repo, monkeypatch):
    store = coord._open_store()
    try:
        fd = os.open(
            ".registry-corrupt.tmp", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644, dir_fd=store.coord_fd
        )
        with os.fdopen(fd, "w") as f:
            f.write("{not json")
        os.replace(".registry-corrupt.tmp", "registry.json", src_dir_fd=store.coord_fd, dst_dir_fd=store.coord_fd)

        monkeypatch.delenv("FFS_COORD_MODE", raising=False)
        with pytest.raises(coord.CoordExit) as exc:
            coord._load_registry(store)
        assert exc.value.code == 69

        monkeypatch.setenv("FFS_COORD_MODE", "audit")
        registry = coord._load_registry(store)
        assert registry["claims"] == {}
    finally:
        coord._close_store(store)


def test_status_flags_missing_worktree_foreign_host_incomplete_entry(repo, monkeypatch, capsys):
    store = coord._open_store()
    try:
        _hand_stamp_claim(
            store, "spec-missing-wt",
            holder_worktree=str(store.store_root / "gone"),
        )
        with coord.registry_transaction(store) as registry:
            registry["claims"][coord._claim_key("spec-foreign-host")] = {
                "holder_uuid": "x", "holder_host": "some-other-host",
                "holder_worktree": str(store.store_root.parent.parent),
                "generation": 1, "last_renewed_at": time.time(), "ttl_secs": 300.0,
            }
            registry["claims"][coord._claim_key("spec-incomplete")] = {
                "holder_uuid": "y", "holder_worktree": str(store.store_root.parent.parent),
                "generation": 1,
            }
            coord._save_registry(store, registry)
        coord.cmd_status(store, argparse_namespace())
    finally:
        coord._close_store(store)
    out = capsys.readouterr().out
    assert "MISSING-WORKTREE" in out
    assert "FOREIGN-HOST" in out
    assert "INCOMPLETE-ENTRY" in out


def test_doctor_reports_version_store_mode_live_claims(repo, monkeypatch, capsys):
    store = coord._open_store()
    try:
        coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
        rc = coord.cmd_doctor(store, argparse_namespace())
    finally:
        coord._close_store(store)
    assert rc == 0
    out = capsys.readouterr().out
    assert "filelock_version=" in out
    assert "store_path=" in out
    assert "mode=" in out
    assert "live_claims=1" in out


# ── W1 gap-closure: exit 75 COORD-CONTENTION (plan T-01-12) ──────────────
def test_registry_contention_exits_75(repo, monkeypatch):
    """A registry lock held by ANOTHER process forces exit 75, not a hang."""
    monkeypatch.setattr(coord, "LEASE_ACQUIRE_TIMEOUT_SECS", 0.3)
    store = coord._open_store()
    try:
        holder = sp.Popen(
            [sys.executable, "-c",
             "import sys, time\n"
             "from filelock import FileLock\n"
             "l = FileLock(sys.argv[1])\n"
             "l.acquire()\n"
             "print('HELD', flush=True)\n"
             "time.sleep(30)\n",
             str(store.lock_path)],
            stdout=sp.PIPE, text=True)
        try:
            assert holder.stdout.readline().strip() == "HELD"
            before = (store.store_root / "registry.json").read_bytes() \
                if (store.store_root / "registry.json").exists() else None
            with pytest.raises(coord.CoordExit) as exc:
                with coord.registry_transaction(store):
                    pass  # pragma: no cover — must never be reached
            assert exc.value.code == 75
            assert "COORD-CONTENTION" in (exc.value.message or "")
            after = (store.store_root / "registry.json").read_bytes() \
                if (store.store_root / "registry.json").exists() else None
            assert before == after  # registry untouched on contention
        finally:
            holder.kill()
            holder.wait()
    finally:
        coord._close_store(store)


# ═════════════════════════════════════════════════════════════════════════
# Phase 2 — Task 1: lease resource validator + namespace hygiene + tracer
# ═════════════════════════════════════════════════════════════════════════

def _lease_args(**kw):
    base = dict(resource=None, mode=None, ttl=None, heartbeat=None)
    base.update(kw)
    return argparse_namespace(**base)


def _renew_args(**kw):
    base = dict(resource=None, generation=None, ttl=None, heartbeat=None)
    base.update(kw)
    return argparse_namespace(**base)


def _release_args(**kw):
    base = dict(resource=None, generation=None)
    base.update(kw)
    return argparse_namespace(**base)


def _hand_stamp_lease(store, key, mode, holder_uuid="foreign-uuid", **overrides):
    """Test-side helper modelled on `_hand_stamp_claim` — construct a
    foreign-holder lease state directly inside a registry_transaction."""
    with coord.registry_transaction(store) as registry:
        gen_entry = coord._lease_gen_entry(registry, key)
        now = time.time()
        gen_entry["gen"] = max(gen_entry["gen"], 1)
        holder = {
            "holder_uuid": holder_uuid,
            "holder_anchor_pid": None,
            "holder_anchor_start_token": None,
            "holder_host": coord._host_name(),
            "holder_worktree": str(store.store_root.parent.parent),
            "generation": gen_entry["gen"],
            "acquired_at": now,
            "last_renewed_at": now,
            "ttl_secs": coord.DEFAULT_TTL_SECS,
            "expires_at": now + coord.DEFAULT_TTL_SECS,
            "cli_pid": 0,
        }
        holder.update(overrides)
        entry = registry["leases"].setdefault(key, {"mode": mode, "holders": {}})
        entry["mode"] = mode
        entry["holders"][holder_uuid] = holder
        coord._save_registry(store, registry)
    return holder


# ── REQ-06: resource form acceptance / rejection ────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("path:docs/a.md", "path:docs/a.md"),
    ("path:a", "path:a"),
    ("path:docs/**", "path:docs/**"),
    ("path:a/b/c.py", "path:a/b/c.py"),
])
def test_lease_key_from_arg_accepts_stable_forms(raw, expected):
    assert coord._lease_key_from_arg(raw) == expected
    # idempotent: re-feeding the canonical key returns it unchanged
    assert coord._lease_key_from_arg(expected) == expected


def test_lease_parse_resource_segments_and_prefix_flag():
    assert coord._parse_lease_resource("path:docs/**") == (("docs",), True)
    assert coord._parse_lease_resource("path:docs") == (("docs",), False)


@pytest.mark.parametrize("bad", [
    "docs/a.md",            # no path: prefix
    "path:",                # empty
    "path:/**",             # empty literal
    "path:**",              # bare glob, not the suffix form
    "path:docs/*.md",
    "path:docs/?.md",
    "path:docs/[ab].md",
    "path:/etc/passwd",
    "path:../outside",
    "path:docs/../../outside",
    "path:docs//a.md",      # empty segment
    "path:docs/./a.md",
    "path:docs/",           # trailing slash
])
def test_lease_key_from_arg_rejects_forms(bad):
    with pytest.raises(coord.CoordExit) as exc:
        coord._lease_key_from_arg(bad)
    assert exc.value.code == 2


@pytest.mark.parametrize("bad", ["path:a\x00b", "path:a\nb", "path:a\tb"])
def test_lease_key_from_arg_rejects_control_chars_exit_2_not_traceback(bad):
    with pytest.raises(coord.CoordExit) as exc:
        coord._lease_key_from_arg(bad)
    assert exc.value.code == 2


@pytest.mark.parametrize("bad", [None, "", "--mode Read", "Shared"])
def test_lease_mode_argparse_rejects_bad_values(bad):
    parser = coord.build_parser()
    argv = ["lease-acquire", "--resource", "path:a"]
    if bad is not None:
        argv += ["--mode", bad]
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("mode", ["shared", "exclusive"])
def test_lease_mode_argparse_accepts_good_values(mode):
    parser = coord.build_parser()
    args = parser.parse_args(["lease-acquire", "--resource", "path:a", "--mode", mode])
    assert args.mode == mode


def test_lease_and_claim_validators_have_disjoint_accept_corpora():
    """Pitfall 4 / T-02-07: the accepted key spaces must be disjoint by
    construction — a shared `generations` map is only safe if neither
    validator accepts what the other does."""
    lease_accepted = ["path:a", "path:docs/**", "path:a/b/c.py"]
    claim_accepted = ["spec-009", "009", "spec_009.1"]
    for good in lease_accepted:
        with pytest.raises(coord.CoordExit):
            coord._validate_claim_id(good)
    for good in claim_accepted:
        with pytest.raises(coord.CoordExit):
            coord._lease_key_from_arg(good)


def test_lease_containment_symlink_escape_exits_78(repo):
    real_elsewhere = repo.parent / "elsewhere"
    real_elsewhere.mkdir()
    (repo / "sub").symlink_to(real_elsewhere)
    root = coord._worktree_root()
    with pytest.raises(coord.CoordExit) as exc:
        coord._validate_lease_resource("path:sub/a.md", root)
    assert exc.value.code == 78


def test_lease_containment_in_repo_symlink_accepted(repo):
    real_elsewhere = repo / "real-sub"
    real_elsewhere.mkdir()
    (repo / "linked-sub").symlink_to(real_elsewhere)
    root = coord._worktree_root()
    key = coord._validate_lease_resource("path:linked-sub/a.md", root)
    assert key == "path:linked-sub/a.md"


def test_lease_containment_nonexistent_path_accepted(repo):
    root = coord._worktree_root()
    key = coord._validate_lease_resource("path:brand/new/file.md", root)
    assert key == "path:brand/new/file.md"


# ── end-to-end tracer: exclusive lease blocks a foreign session ────────────
def test_lease_acquire_exclusive_end_to_end(repo, monkeypatch):
    monkeypatch.setenv("FFS_COORD_ANCHOR_PID", str(os.getpid()))
    monkeypatch.setenv("FFS_RUN_ID", "holder")
    store = coord._open_store()
    try:
        rc = coord.cmd_lease_acquire(
            store, _lease_args(resource="path:docs/a.md", mode="exclusive")
        )
        assert rc == 0
        with coord.registry_transaction(store) as registry:
            entry = registry["leases"]["path:docs/a.md"]
            assert entry["mode"] == "exclusive"
            assert len(entry["holders"]) == 1
    finally:
        coord._close_store(store)

    monkeypatch.setenv("FFS_RUN_ID", "other")
    store2 = coord._open_store()
    try:
        rc_shared = coord.cmd_lease_acquire(
            store2, _lease_args(resource="path:docs/a.md", mode="shared")
        )
        assert rc_shared == 3
        rc_excl = coord.cmd_lease_acquire(
            store2, _lease_args(resource="path:docs/a.md", mode="exclusive")
        )
        assert rc_excl == 3
        # non-overlapping resource: scoped refusal, not global
        rc_ok = coord.cmd_lease_acquire(
            store2, _lease_args(resource="path:src/b.md", mode="exclusive")
        )
        assert rc_ok == 0
    finally:
        coord._close_store(store2)


def test_lease_holder_field_set_matches_claim_entry(repo, monkeypatch):
    monkeypatch.setenv("FFS_RUN_ID", "r1")
    store = coord._open_store()
    try:
        coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
        coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="exclusive"))
        with coord.registry_transaction(store) as registry:
            claim_entry = registry["claims"]["claim:spec-009"]
            lease_holder = next(iter(registry["leases"]["path:a"]["holders"].values()))
        assert set(lease_holder.keys()) == set(claim_entry.keys())
    finally:
        coord._close_store(store)


# ── W3: --ttl write-side, both lease and claim ──────────────────────────────
def test_lease_and_claim_ttl_write_side_w3(repo, monkeypatch):
    monkeypatch.setenv("FFS_RUN_ID", "r1")
    store = coord._open_store()
    try:
        coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
        coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="exclusive"))
        with coord.registry_transaction(store) as registry:
            claim_entry = registry["claims"]["claim:spec-009"]
            lease_holder = next(iter(registry["leases"]["path:a"]["holders"].values()))
        assert claim_entry["ttl_secs"] == coord.DEFAULT_TTL_SECS
        assert claim_entry["expires_at"] == claim_entry["last_renewed_at"] + claim_entry["ttl_secs"]
        assert lease_holder["ttl_secs"] == coord.DEFAULT_TTL_SECS
        assert lease_holder["expires_at"] == lease_holder["last_renewed_at"] + lease_holder["ttl_secs"]
    finally:
        coord._close_store(store)

    monkeypatch.setenv("FFS_RUN_ID", "r2")
    store2 = coord._open_store()
    try:
        coord.cmd_claim(store2, argparse_namespace(spec_id="spec-030", ttl="9999", heartbeat=None))
        coord.cmd_lease_acquire(store2, _lease_args(resource="path:b", mode="exclusive", ttl="9999"))
        with coord.registry_transaction(store2) as registry:
            claim_entry = registry["claims"]["claim:spec-030"]
            lease_holder = next(iter(registry["leases"]["path:b"]["holders"].values()))
        assert claim_entry["ttl_secs"] == 9999.0
        assert claim_entry["expires_at"] == claim_entry["last_renewed_at"] + 9999.0
        assert lease_holder["ttl_secs"] == 9999.0
        assert lease_holder["expires_at"] == lease_holder["last_renewed_at"] + 9999.0
    finally:
        coord._close_store(store2)


# ── W4: the 4x-heartbeat floor made mechanical ──────────────────────────────
def test_lease_ttl_floor_w4_mechanical(repo, monkeypatch):
    monkeypatch.setenv("FFS_RUN_ID", "r1")
    store = coord._open_store()
    try:
        floor = 4 * coord.DEFAULT_HEARTBEAT_SECS
        assert floor == 240.0
        rc = coord.cmd_lease_acquire(
            store, _lease_args(resource="path:a", mode="exclusive", ttl=str(floor))
        )
        assert rc == 0
        for bad in ("239", "0", "-5", "abc", "nan", "inf", "999999999"):
            with pytest.raises(coord.CoordExit) as exc:
                coord.cmd_lease_acquire(
                    store, _lease_args(resource="path:c", mode="exclusive", ttl=bad)
                )
            assert exc.value.code == 2
        with coord.registry_transaction(store) as registry:
            assert "path:c" not in registry["leases"]
    finally:
        coord._close_store(store)


# ── overlap scan + mutation in ONE transaction ──────────────────────────────
def test_lease_acquire_enters_registry_transaction_exactly_once(repo, monkeypatch):
    """P2-W1: prove the registry lock is HELD at _save_registry time, not
    merely that the lock file EXISTS (a lock file survives release, so
    existence proves nothing). A non-blocking (timeout=0) re-acquisition of
    the SAME lock from inside the spy must raise filelock.Timeout -- that
    can only happen while this process itself still holds it."""
    import filelock

    monkeypatch.setenv("FFS_RUN_ID", "r1")
    store = coord._open_store()
    calls = []
    real_save = coord._save_registry

    def spy(store_, data):
        probe = filelock.FileLock(str(store_.lock_path), timeout=0)
        with pytest.raises(filelock.Timeout):
            probe.acquire()
        calls.append(1)
        return real_save(store_, data)

    monkeypatch.setattr(coord, "_save_registry", spy)
    try:
        rc = coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="exclusive"))
        assert rc == 0
        assert calls == [1]
    finally:
        coord._close_store(store)


# ── namespace init: pre-Phase-2 / hand-edited stores ────────────────────────
@pytest.mark.parametrize("missing", ["leases", "generations", "claims"])
def test_load_registry_namespace_setdefault_preserves_existing(repo, monkeypatch, missing):
    """A registry loaded without a `leases` (or `claims`, or `generations`)
    key is handled by `_load_registry`'s setdefaults. When the ABSENT
    namespace is NOT `claims` itself, the pre-existing claim entry is
    byte-identical afterward — deleting `claims` itself trivially removes
    its own contents (nothing to preserve), but must not crash the loader
    or the sibling namespaces."""
    monkeypatch.setenv("FFS_RUN_ID", "r1")
    store = coord._open_store()
    try:
        coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
        with coord.registry_transaction(store) as registry:
            before_claims = json.dumps(registry["claims"], sort_keys=True)
            del registry[missing]
            coord._save_registry(store, registry)

        rc = coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="exclusive"))
        assert rc == 0
        with coord.registry_transaction(store) as registry:
            assert isinstance(registry["leases"], dict)
            assert isinstance(registry["generations"], dict)
            assert isinstance(registry["claims"], dict)
            if missing != "claims":
                assert json.dumps(registry["claims"], sort_keys=True) == before_claims
            assert "path:a" in registry["leases"]
    finally:
        coord._close_store(store)


@pytest.mark.parametrize("bad_ns", [
    {"claims": []}, {"leases": "x"}, {"generations": 3},
])
def test_load_registry_namespace_wrong_type_exits_69(repo, bad_ns):
    store = coord._open_store()
    try:
        with coord.registry_transaction(store) as registry:
            registry.update(bad_ns)
            coord._save_registry(store, registry)
        with pytest.raises(coord.CoordExit) as exc:
            coord._load_registry(store)
        assert exc.value.code == 69
    finally:
        coord._close_store(store)


# ── structural corruption of persisted lease entries -> exit 69 ────────────
def _write_raw_leases(store, leases_value):
    with coord.registry_transaction(store) as registry:
        registry["leases"] = leases_value
        coord._save_registry(store, registry)


@pytest.mark.parametrize("bad_leases", [
    [],
    {"path:a": []},
    {"path:a": {"mode": "readonly", "holders": {}}},
    {"path:a": {"mode": "shared", "holders": []}},
    {"path:a": {"mode": "shared", "holders": {"u": {"generation": "3"}}}},
])
def test_lease_structural_corruption_exits_69_everywhere_no_repair(repo, monkeypatch, bad_leases):
    monkeypatch.setenv("FFS_RUN_ID", "r1")
    store = coord._open_store()
    try:
        _write_raw_leases(store, bad_leases)
        before = (store.store_root / "registry.json").read_bytes()

        with pytest.raises(coord.CoordExit) as exc:
            coord.cmd_lease_acquire(store, _lease_args(resource="path:b", mode="exclusive"))
        assert exc.value.code == 69

        with pytest.raises(coord.CoordExit) as exc:
            coord.cmd_lease_renew(store, _renew_args(resource="path:a", generation=1))
        assert exc.value.code == 69

        with pytest.raises(coord.CoordExit) as exc:
            coord.cmd_lease_release(store, _release_args(resource="path:a", generation=1))
        assert exc.value.code == 69

        with pytest.raises(coord.CoordExit) as exc:
            coord.cmd_status(store, argparse_namespace())
        assert exc.value.code == 69

        after = (store.store_root / "registry.json").read_bytes()
        assert before == after
    finally:
        coord._close_store(store)


@pytest.mark.parametrize("bad_gen", [None, "3", 3.0, True, 0, -1])
def test_lease_holder_generation_required_and_valid_exits_69(repo, monkeypatch, bad_gen):
    monkeypatch.setenv("FFS_RUN_ID", "r1")
    store = coord._open_store()
    try:
        holder = _hand_stamp_lease(store, "path:a", "exclusive", holder_uuid="foreign")
        with coord.registry_transaction(store) as registry:
            h = registry["leases"]["path:a"]["holders"]["foreign"]
            if bad_gen is None:
                del h["generation"]
            else:
                h["generation"] = bad_gen
            coord._save_registry(store, registry)

        for fn, args in (
            (coord.cmd_lease_renew, _renew_args(resource="path:a", generation=1)),
            (coord.cmd_lease_release, _release_args(resource="path:a", generation=1)),
            (coord.cmd_lease_acquire, _lease_args(resource="path:a", mode="exclusive")),
        ):
            with pytest.raises(coord.CoordExit) as exc:
                fn(store, args)
            assert exc.value.code == 69
    finally:
        coord._close_store(store)


@pytest.mark.parametrize("field,bad_value", [
    ("holder_anchor_pid", "1234"),
    ("last_renewed_at", "yesterday"),
    ("ttl_secs", "300"),
    ("acquired_at", []),
    ("holder_worktree", 7),
])
def test_lease_holder_wrong_type_staleness_field_is_corrupt(repo, field, bad_value):
    store = coord._open_store()
    try:
        _hand_stamp_lease(store, "path:a", "exclusive", holder_uuid="foreign", **{field: bad_value})
        with pytest.raises(coord.CoordExit) as exc:
            coord.cmd_status(store, argparse_namespace())
        assert exc.value.code == 69
    finally:
        coord._close_store(store)


@pytest.mark.parametrize("field", [
    "holder_anchor_pid", "holder_anchor_start_token", "last_renewed_at", "ttl_secs",
])
def test_lease_holder_torn_field_is_not_corrupt(repo, field, capsys):
    store = coord._open_store()
    try:
        _hand_stamp_lease(store, "path:a", "exclusive", holder_uuid="foreign", **{field: None})
        rc = coord.cmd_status(store, argparse_namespace())
        assert rc == 0
        out = capsys.readouterr().out
        assert "INCOMPLETE-ENTRY" in out
    finally:
        coord._close_store(store)


def test_doctor_never_raises_on_corrupt_lease_store(repo, capsys):
    """P2-W2: capsys.readouterr() DRAINS the buffer -- a second call returns
    empty, so the original two-call form silently asserted nothing about
    `out`. One drain, and the `lease_store=corrupt` line (with its
    offending key) is asserted for real (02-01-PLAN.md:173)."""
    store = coord._open_store()
    try:
        _write_raw_leases(store, {"path:a": []})
        rc = coord.cmd_doctor(store, argparse_namespace())
        assert rc == 69
        cap = capsys.readouterr()
        combined = cap.out + cap.err
        assert "lease_store=corrupt" in combined, combined
        assert "path:a" in combined, combined
    finally:
        coord._close_store(store)


def test_doctor_escaped_leases_diagnostic_t_02_11(repo, capsys):
    store = coord._open_store()
    try:
        (repo / "sub").mkdir()
        rc = coord.cmd_lease_acquire(
            store, _lease_args(resource="path:sub/a.md", mode="exclusive")
        )
        assert rc == 0
    finally:
        coord._close_store(store)
    capsys.readouterr()

    real_elsewhere = repo.parent / "elsewhere-escape"
    real_elsewhere.mkdir()
    import shutil
    shutil.rmtree(repo / "sub")
    (repo / "sub").symlink_to(real_elsewhere)

    store2 = coord._open_store()
    try:
        rc = coord.cmd_doctor(store2, argparse_namespace())
    finally:
        coord._close_store(store2)
    assert rc == 0
    out = capsys.readouterr().out
    assert "escaped_leases=path:sub/a.md" in out


# ── persisted KEY validation (both halves) ──────────────────────────────────
@pytest.mark.parametrize("bad_key", ["spec-009", "path:docs/*.md", "path:../x"])
def test_lease_persisted_key_non_conforming_exits_69(repo, bad_key):
    store = coord._open_store()
    try:
        _write_raw_leases(store, {bad_key: {"mode": "shared", "holders": {}}})
        with pytest.raises(coord.CoordExit) as exc:
            coord.cmd_status(store, argparse_namespace())
        assert exc.value.code == 69
    finally:
        coord._close_store(store)


def test_lease_persisted_key_non_canonical_exits_69(repo):
    store = coord._open_store()
    try:
        # unfolded casing — fails to round-trip through _lease_key_from_arg
        _write_raw_leases(store, {"path:Docs/**": {"mode": "shared", "holders": {}}})
        with pytest.raises(coord.CoordExit) as exc:
            coord.cmd_status(store, argparse_namespace())
        assert exc.value.code == 69
    finally:
        coord._close_store(store)

    store2 = coord._open_store()
    try:
        nfd_key = "path:" + __import__("unicodedata").normalize("NFD", "café")
        _write_raw_leases(store2, {nfd_key: {"mode": "shared", "holders": {}}})
        with pytest.raises(coord.CoordExit) as exc:
            coord.cmd_status(store2, argparse_namespace())
        assert exc.value.code == 69
    finally:
        coord._close_store(store2)


# ── generations record shape (P-05, read by lease code) ────────────────────
@pytest.mark.parametrize("bad_record", [7, "7", {"gen": "7"}, {"released_holders": []},
                                         {"released_holders": {"u": {"generation": "5"}}}])
def test_generations_record_wrong_shape_exits_69(repo, bad_record):
    store = coord._open_store()
    try:
        with coord.registry_transaction(store) as registry:
            registry["generations"]["path:a"] = bad_record
            coord._save_registry(store, registry)
        with pytest.raises(coord.CoordExit) as exc:
            coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="exclusive"))
        assert exc.value.code == 69
    finally:
        coord._close_store(store)


def test_generations_record_missing_fields_not_corrupt(repo):
    store = coord._open_store()
    try:
        with coord.registry_transaction(store) as registry:
            registry["generations"]["path:a"] = {}  # missing gen and released_holders
            coord._save_registry(store, registry)
        rc = coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="exclusive"))
        assert rc == 0
    finally:
        coord._close_store(store)


# ═════════════════════════════════════════════════════════════════════════
# Phase 2 — Task 2: full conflict matrix, casefold/NFC, staleness reclaim
# ═════════════════════════════════════════════════════════════════════════

def _spawn_lease_anchor():
    return sp.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def test_lease_matrix_shared_shared_two_different_sessions(repo, monkeypatch):
    monkeypatch.setenv("FFS_COORD_ANCHOR_PID", str(os.getpid()))
    monkeypatch.setenv("FFS_RUN_ID", "a")
    store = coord._open_store()
    try:
        coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="shared"))
    finally:
        coord._close_store(store)
    monkeypatch.setenv("FFS_RUN_ID", "b")
    store2 = coord._open_store()
    try:
        rc = coord.cmd_lease_acquire(store2, _lease_args(resource="path:a", mode="shared"))
        assert rc == 0
        with coord.registry_transaction(store2) as registry:
            assert len(registry["leases"]["path:a"]["holders"]) == 2
    finally:
        coord._close_store(store2)


@pytest.mark.parametrize("first_mode,second_mode", [
    ("shared", "exclusive"), ("exclusive", "shared"), ("exclusive", "exclusive"),
])
def test_lease_matrix_conflicts_when_exclusive_involved(repo, monkeypatch, first_mode, second_mode):
    monkeypatch.setenv("FFS_COORD_ANCHOR_PID", str(os.getpid()))
    monkeypatch.setenv("FFS_RUN_ID", "a")
    store = coord._open_store()
    try:
        coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode=first_mode))
    finally:
        coord._close_store(store)
    monkeypatch.setenv("FFS_RUN_ID", "b")
    store2 = coord._open_store()
    try:
        rc = coord.cmd_lease_acquire(store2, _lease_args(resource="path:a", mode=second_mode))
        assert rc == 3
    finally:
        coord._close_store(store2)


@pytest.mark.parametrize("mode1,mode2", [
    ("shared", "exclusive"), ("exclusive", "shared"),
    ("exclusive", "exclusive"), ("shared", "shared"),
])
def test_lease_p06_self_non_conflict_all_combinations(repo, monkeypatch, mode1, mode2):
    monkeypatch.setenv("FFS_COORD_ANCHOR_PID", str(os.getpid()))
    monkeypatch.setenv("FFS_RUN_ID", "self")
    store = coord._open_store()
    try:
        rc1 = coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode=mode1))
        assert rc1 == 0
        rc2 = coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode=mode2))
        assert rc2 == 0
        with coord.registry_transaction(store) as registry:
            entry = registry["leases"]["path:a"]
            assert entry["mode"] == mode2
            assert len(entry["holders"]) == 1
            if mode1 == "exclusive" and mode2 == "exclusive":
                assert list(entry["holders"].values())[0]["generation"] == 1
    finally:
        coord._close_store(store)


def test_lease_p06_cross_key_self_overlap(repo, monkeypatch):
    monkeypatch.setenv("FFS_COORD_ANCHOR_PID", str(os.getpid()))
    monkeypatch.setenv("FFS_RUN_ID", "self")
    store = coord._open_store()
    try:
        rc1 = coord.cmd_lease_acquire(store, _lease_args(resource="path:skills/**", mode="exclusive"))
        assert rc1 == 0
        rc2 = coord.cmd_lease_acquire(store, _lease_args(resource="path:skills/a/b.md", mode="exclusive"))
        assert rc2 == 0
        with coord.registry_transaction(store) as registry:
            assert "path:skills/**" in registry["leases"]
            assert "path:skills/a/b.md" in registry["leases"]
    finally:
        coord._close_store(store)

    monkeypatch.setenv("FFS_RUN_ID", "foreign")
    store2 = coord._open_store()
    try:
        rc3 = coord.cmd_lease_acquire(store2, _lease_args(resource="path:skills/a/b.md", mode="exclusive"))
        assert rc3 == 3
    finally:
        coord._close_store(store2)


def test_lease_cross_key_overlap_both_orderings(repo, monkeypatch):
    monkeypatch.setenv("FFS_COORD_ANCHOR_PID", str(os.getpid()))
    monkeypatch.setenv("FFS_RUN_ID", "a")
    store = coord._open_store()
    try:
        coord.cmd_lease_acquire(store, _lease_args(resource="path:skills/**", mode="exclusive"))
    finally:
        coord._close_store(store)
    monkeypatch.setenv("FFS_RUN_ID", "b")
    store2 = coord._open_store()
    try:
        rc = coord.cmd_lease_acquire(
            store2, _lease_args(resource="path:skills/feature-implement/SKILL.md", mode="shared")
        )
        assert rc == 3
    finally:
        coord._close_store(store2)

    monkeypatch.setenv("FFS_RUN_ID", "c")
    store3 = coord._open_store()
    try:
        coord.cmd_lease_acquire(store3, _lease_args(resource="path:other/x.md", mode="exclusive"))
    finally:
        coord._close_store(store3)
    monkeypatch.setenv("FFS_RUN_ID", "d")
    store4 = coord._open_store()
    try:
        rc2 = coord.cmd_lease_acquire(store4, _lease_args(resource="path:other/**", mode="shared"))
        assert rc2 == 3
    finally:
        coord._close_store(store4)


def test_lease_strictly_under_boundary(repo, monkeypatch):
    monkeypatch.setenv("FFS_COORD_ANCHOR_PID", str(os.getpid()))
    monkeypatch.setenv("FFS_RUN_ID", "a")
    store = coord._open_store()
    try:
        coord.cmd_lease_acquire(store, _lease_args(resource="path:skills/**", mode="exclusive"))
    finally:
        coord._close_store(store)
    monkeypatch.setenv("FFS_RUN_ID", "b")
    store2 = coord._open_store()
    try:
        rc = coord.cmd_lease_acquire(store2, _lease_args(resource="path:skills", mode="exclusive"))
        assert rc == 0
        with coord.registry_transaction(store2) as registry:
            assert "path:skills/**" in registry["leases"]
            assert "path:skills" in registry["leases"]
    finally:
        coord._close_store(store2)


def test_lease_prefix_vs_prefix_overlap():
    assert coord._lease_keys_overlap("path:a/**", "path:a/b/**") is True
    assert coord._lease_keys_overlap("path:a/b/**", "path:a/**") is True
    assert coord._lease_keys_overlap("path:a/**", "path:a/**") is True
    assert coord._lease_keys_overlap("path:a/**", "path:b/**") is False
    assert coord._lease_keys_overlap("path:ab/**", "path:a/**") is False  # segment-wise, not char-prefix


def test_lease_casefold_nonascii(repo, monkeypatch):
    monkeypatch.setenv("FFS_COORD_ANCHOR_PID", str(os.getpid()))
    monkeypatch.setenv("FFS_RUN_ID", "a")
    store = coord._open_store()
    try:
        coord.cmd_lease_acquire(store, _lease_args(resource="path:Docs/**", mode="exclusive"))
    finally:
        coord._close_store(store)
    monkeypatch.setenv("FFS_RUN_ID", "b")
    store2 = coord._open_store()
    try:
        rc = coord.cmd_lease_acquire(store2, _lease_args(resource="path:docs/a.md", mode="exclusive"))
        assert rc == 3
    finally:
        coord._close_store(store2)

    key1 = coord._lease_key_from_arg("path:STRASSE/**")
    key2 = coord._lease_key_from_arg("path:strasse/**")
    assert key1 == key2

    key3 = coord._lease_key_from_arg("path:straße/**")  # 'ß'
    assert key3 == coord._lease_key_from_arg("path:strasse/**")  # ß.casefold() == 'ss'


def test_lease_nfc_normalization(repo, monkeypatch):
    import unicodedata
    nfd = unicodedata.normalize("NFD", "café")
    nfc = unicodedata.normalize("NFC", "café")
    assert nfd != nfc  # sanity: the two byte-sequences genuinely differ
    key_nfd = coord._lease_key_from_arg(f"path:{nfd}/**")
    key_nfc = coord._lease_key_from_arg(f"path:{nfc}/**")
    assert key_nfd == key_nfc

    monkeypatch.setenv("FFS_COORD_ANCHOR_PID", str(os.getpid()))
    monkeypatch.setenv("FFS_RUN_ID", "a")
    store = coord._open_store()
    try:
        coord.cmd_lease_acquire(store, _lease_args(resource=f"path:{nfd}/x.md", mode="exclusive"))
    finally:
        coord._close_store(store)
    monkeypatch.setenv("FFS_RUN_ID", "b")
    store2 = coord._open_store()
    try:
        rc = coord.cmd_lease_acquire(store2, _lease_args(resource=f"path:{nfc}/x.md", mode="exclusive"))
        assert rc == 3
    finally:
        coord._close_store(store2)


def test_lease_reclaim_dead_anchor_prunes_and_regrants(repo, monkeypatch):
    proc = _spawn_lease_anchor()
    token = coord._capture_start_token(proc.pid)
    monkeypatch.setenv("FFS_RUN_ID", "peer")
    store = coord._open_store()
    try:
        _hand_stamp_lease(
            store, "path:a", "exclusive", holder_uuid="dying",
            holder_anchor_pid=proc.pid, holder_anchor_start_token=token,
        )
        proc.kill()
        proc.wait(timeout=5)
        rc = coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="exclusive"))
        assert rc == 0
        with coord.registry_transaction(store) as registry:
            holders = registry["leases"]["path:a"]["holders"]
            assert "dying" not in holders
            assert len(holders) == 1
    finally:
        coord._close_store(store)


def test_lease_reclaim_recycled_pid_stale_w2(repo, monkeypatch):
    live_pid = os.getpid()
    real_mod = coord._identity_module()

    class _FakeMod:
        host_name = staticmethod(real_mod.host_name)
        process_alive = staticmethod(real_mod.process_alive)

        @staticmethod
        def process_start_token(pid):
            return "different-token"

    coord._IDENTITY_MOD_CACHE = _FakeMod
    try:
        assert coord._anchor_is_stale(live_pid, coord._host_name(), "recorded-token") == "stale"
    finally:
        coord._IDENTITY_MOD_CACHE = real_mod

    monkeypatch.setenv("FFS_RUN_ID", "peer")
    store = coord._open_store()
    try:
        _hand_stamp_lease(
            store, "path:a", "exclusive", holder_uuid="recycled",
            holder_anchor_pid=live_pid, holder_anchor_start_token="recorded-token",
        )
        coord._IDENTITY_MOD_CACHE = _FakeMod
        try:
            rc = coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="exclusive"))
        finally:
            coord._IDENTITY_MOD_CACHE = real_mod
        assert rc == 0
        with coord.registry_transaction(store) as registry:
            assert "recycled" not in registry["leases"]["path:a"]["holders"]
    finally:
        coord._close_store(store)


@pytest.mark.parametrize("multiplier", [1, 2, 10])
def test_lease_reclaim_live_holder_never_reclaimable(repo, monkeypatch, multiplier):
    live_pid = os.getpid()
    token = coord._capture_start_token(live_pid)
    monkeypatch.setenv("FFS_RUN_ID", "peer")
    store = coord._open_store()
    try:
        ttl = 60.0
        _hand_stamp_lease(
            store, "path:a", "exclusive", holder_uuid="live",
            holder_anchor_pid=live_pid, holder_anchor_start_token=token,
            ttl_secs=ttl, last_renewed_at=time.time() - (multiplier * ttl),
        )
        rc = coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="exclusive"))
        assert rc == 3
        with coord.registry_transaction(store) as registry:
            assert "live" in registry["leases"]["path:a"]["holders"]
    finally:
        coord._close_store(store)


def test_lease_prune_does_not_eat_live_shared_holder(repo, monkeypatch):
    """The `_is_reclaimable` tuple-truthiness regression: a bare truthiness
    test on the (reclaimable, verdict) tuple is always true and would prune
    every LIVE foreign holder — this is the only case that catches it."""
    live_pid = os.getpid()
    token = coord._capture_start_token(live_pid)
    monkeypatch.setenv("FFS_RUN_ID", "peer")
    store = coord._open_store()
    try:
        _hand_stamp_lease(
            store, "path:a", "shared", holder_uuid="live",
            holder_anchor_pid=live_pid, holder_anchor_start_token=token,
        )
        before = (store.store_root / "registry.json").read_bytes()
        rc = coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="exclusive"))
        assert rc == 3
        after = (store.store_root / "registry.json").read_bytes()
        assert before == after
        with coord.registry_transaction(store) as registry:
            assert "live" in registry["leases"]["path:a"]["holders"]
    finally:
        coord._close_store(store)


def test_lease_reclaim_unprobeable_bounded_by_own_ttl(repo, monkeypatch):
    monkeypatch.setenv("FFS_RUN_ID", "peer")
    store = coord._open_store()
    try:
        _hand_stamp_lease(
            store, "path:a", "exclusive", holder_uuid="unprobeable",
            holder_anchor_pid=None, holder_anchor_start_token=None,
            ttl_secs=240.0, last_renewed_at=time.time() - 100,
        )
        rc = coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="exclusive"))
        assert rc == 3  # not yet past its own ttl_secs
    finally:
        coord._close_store(store)

    store2 = coord._open_store()
    try:
        _hand_stamp_lease(
            store2, "path:b", "exclusive", holder_uuid="unprobeable2",
            holder_anchor_pid=None, holder_anchor_start_token=None,
            ttl_secs=240.0, last_renewed_at=time.time() - 300,
        )
        rc = coord.cmd_lease_acquire(store2, _lease_args(resource="path:b", mode="exclusive"))
        assert rc == 0  # past its own ttl_secs
    finally:
        coord._close_store(store2)


def test_lease_shared_join_does_not_evict_existing_holders(repo, monkeypatch):
    monkeypatch.setenv("FFS_COORD_ANCHOR_PID", str(os.getpid()))
    expected_count = 0
    acquired_ats = {}
    last_store = None
    for run_id in ("a", "b", "c"):
        monkeypatch.setenv("FFS_RUN_ID", run_id)
        store = coord._open_store()
        last_store = store
        expected_count += 1
        rc = coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="shared"))
        assert rc == 0
        with coord.registry_transaction(store) as registry:
            holders = registry["leases"]["path:a"]["holders"]
            assert len(holders) == expected_count
            for uuid_, h in holders.items():
                if uuid_ in acquired_ats:
                    assert h["acquired_at"] == acquired_ats[uuid_]
                else:
                    acquired_ats[uuid_] = h["acquired_at"]
        coord._close_store(store)
    store_final = coord._open_store()
    try:
        with coord.registry_transaction(store_final) as registry:
            assert len(registry["leases"]["path:a"]["holders"]) == 3
    finally:
        coord._close_store(store_final)


# ═════════════════════════════════════════════════════════════════════════
# Phase 2 — Task 3: release authorization + renewal — tombstones + fencing
# ═════════════════════════════════════════════════════════════════════════

def test_lease_release_happy_path_exclusive(repo, monkeypatch):
    monkeypatch.setenv("FFS_RUN_ID", "r1")
    store = coord._open_store()
    try:
        coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="exclusive"))
        rc = coord.cmd_lease_release(store, _release_args(resource="path:a", generation=1))
        assert rc == 0
        with coord.registry_transaction(store) as registry:
            assert "path:a" not in registry["leases"]
            uuid_ = coord.resolve_identity(store)["session_uuid"]
            tomb = registry["generations"]["path:a"]["released_holders"][uuid_]
            assert tomb["generation"] == 1
            assert "released_at" in tomb
    finally:
        coord._close_store(store)


def test_lease_release_shared_partial(repo, monkeypatch):
    monkeypatch.setenv("FFS_COORD_ANCHOR_PID", str(os.getpid()))
    uuids = {}
    for run_id in ("a", "b", "c"):
        monkeypatch.setenv("FFS_RUN_ID", run_id)
        store = coord._open_store()
        coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="shared"))
        uuids[run_id] = coord.resolve_identity(store)["session_uuid"]
        coord._close_store(store)

    monkeypatch.setenv("FFS_RUN_ID", "a")
    store = coord._open_store()
    try:
        rc = coord.cmd_lease_release(store, _release_args(resource="path:a", generation=1))
        assert rc == 0
        with coord.registry_transaction(store) as registry:
            entry = registry["leases"]["path:a"]
            assert len(entry["holders"]) == 2
            assert uuids["a"] not in entry["holders"]
            assert uuids["b"] in entry["holders"] and uuids["c"] in entry["holders"]
    finally:
        coord._close_store(store)

    # last of the three deletes the entry entirely
    for run_id, gen in (("b", 2), ("c", 3)):
        monkeypatch.setenv("FFS_RUN_ID", run_id)
        store = coord._open_store()
        rc = coord.cmd_lease_release(store, _release_args(resource="path:a", generation=gen))
        assert rc == 0
        coord._close_store(store)
    store = coord._open_store()
    try:
        with coord.registry_transaction(store) as registry:
            assert "path:a" not in registry["leases"]
    finally:
        coord._close_store(store)


def test_lease_p05_tombstone_before_refusal(repo, monkeypatch, capsys):
    monkeypatch.setenv("FFS_COORD_ANCHOR_PID", str(os.getpid()))
    holder_sids = {}
    for run_id in ("a", "b", "c"):
        monkeypatch.setenv("FFS_RUN_ID", run_id)
        store = coord._open_store()
        coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="shared"))
        holder_sids[run_id] = capsys.readouterr().out.splitlines()[0][len("session="):]
        coord._close_store(store)

    monkeypatch.setenv("FFS_RUN_ID", "a")
    store = coord._open_store()
    rc = coord.cmd_lease_release(store, _release_args(resource="path:a", generation=1))
    assert rc == 0
    coord._close_store(store)

    # A releases AGAIN with its original generation — idempotent via tombstone
    store = coord._open_store()
    rc2 = coord.cmd_lease_release(store, _release_args(resource="path:a", generation=1))
    assert rc2 == 0
    coord._close_store(store)

    # a never-holder D releasing that key is refused, held_by names B and C
    monkeypatch.setenv("FFS_RUN_ID", "d")
    store = coord._open_store()
    try:
        capsys.readouterr()
        rc3 = coord.cmd_lease_release(store, _release_args(resource="path:a", generation=1))
        assert rc3 == 3
        cap = capsys.readouterr()
        combined = cap.out + cap.err
        assert "not a recorded holder" in combined, combined
        expected_held_by = ",".join(sorted([holder_sids["b"], holder_sids["c"]]))
        assert f"held_by={expected_held_by}" in combined, combined
        assert "foreign holder" not in combined, combined
    finally:
        coord._close_store(store)


def test_lease_p05_out_of_order_idempotency(repo, monkeypatch):
    monkeypatch.setenv("FFS_COORD_ANCHOR_PID", str(os.getpid()))
    # acquire order a, b, c -> per-holder generations 1, 2, 3 (shared joins bump)
    gens = {"a": 1, "b": 2, "c": 3}
    for run_id in ("a", "b", "c"):
        monkeypatch.setenv("FFS_RUN_ID", run_id)
        store = coord._open_store()
        coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="shared"))
        coord._close_store(store)

    for run_id in ("c", "a", "b"):
        monkeypatch.setenv("FFS_RUN_ID", run_id)
        store = coord._open_store()
        rc = coord.cmd_lease_release(
            store, _release_args(resource="path:a", generation=gens[run_id])
        )
        assert rc == 0
        coord._close_store(store)

    for run_id in ("a", "b", "c"):
        monkeypatch.setenv("FFS_RUN_ID", run_id)
        store = coord._open_store()
        rc = coord.cmd_lease_release(
            store, _release_args(resource="path:a", generation=gens[run_id])
        )
        assert rc == 0
        coord._close_store(store)

    monkeypatch.setenv("FFS_RUN_ID", "never")
    store = coord._open_store()
    try:
        rc = coord.cmd_lease_release(store, _release_args(resource="path:a", generation=1))
        assert rc == 3
    finally:
        coord._close_store(store)


def test_lease_release_ladder_holdership_before_stale_tombstone(repo, monkeypatch):
    monkeypatch.setenv("FFS_RUN_ID", "a")
    store = coord._open_store()
    coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="exclusive"))
    coord.cmd_lease_release(store, _release_args(resource="path:a", generation=1))
    coord._close_store(store)

    store = coord._open_store()
    coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="exclusive"))
    with coord.registry_transaction(store) as registry:
        new_gen = list(registry["leases"]["path:a"]["holders"].values())[0]["generation"]
    assert new_gen == 2
    coord._close_store(store)

    store = coord._open_store()
    try:
        rc = coord.cmd_lease_release(store, _release_args(resource="path:a", generation=new_gen))
        assert rc == 0
    finally:
        coord._close_store(store)


def test_lease_release_refusals_stale_generation_and_stranger(repo, monkeypatch, capsys):
    monkeypatch.setenv("FFS_RUN_ID", "holder")
    store = coord._open_store()
    coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="exclusive"))
    holder_sid = capsys.readouterr().out.splitlines()[0][len("session="):]
    coord._close_store(store)

    store = coord._open_store()
    try:
        before = (store.store_root / "registry.json").read_bytes()
        capsys.readouterr()
        rc = coord.cmd_lease_release(store, _release_args(resource="path:a", generation=99))
        assert rc == 3
        cap = capsys.readouterr()
        combined = cap.out + cap.err
        assert "stale generation" in combined, combined
        assert "foreign holder" not in combined, combined
        after = (store.store_root / "registry.json").read_bytes()
        assert before == after
    finally:
        coord._close_store(store)

    monkeypatch.setenv("FFS_RUN_ID", "stranger")
    store2 = coord._open_store()
    try:
        before = (store2.store_root / "registry.json").read_bytes()
        capsys.readouterr()
        rc2 = coord.cmd_lease_release(store2, _release_args(resource="path:a", generation=1))
        assert rc2 == 3
        cap2 = capsys.readouterr()
        combined2 = cap2.out + cap2.err
        assert "not a recorded holder" in combined2, combined2
        assert f"held_by={holder_sid}" in combined2, combined2
        assert "foreign holder" not in combined2, combined2
        after = (store2.store_root / "registry.json").read_bytes()
        assert before == after
    finally:
        coord._close_store(store2)


def test_lease_release_reclaimed_holder_refused_not_released(repo, monkeypatch, capsys):
    """P2-W3 (deviation from the plan's literal token, recorded honestly):
    the reclaiming session (peer) is the SAME identity used for both the
    reclaim and this release call, so the ladder's step-1 "caller IS a
    current holder" branch fires here -- peer genuinely holds the lease
    after reclaiming it -- and the refusal is on GENERATION, not on
    holdership. The dead "dying" holder was hand-stamped directly into the
    registry with no session record, so there is no way to authenticate AS
    "dying" through the normal identity flow to exercise the
    never-a-holder path from this fixture; that path is already covered by
    the stranger sub-case in test_lease_release_refusals_stale_generation_and_stranger.
    Verified against production directly (io-capture probe) before writing
    this assertion -- production is correct, the plan text's stale-generation
    assertion in its OWN paragraph applies to exactly this shape."""
    proc = _spawn_lease_anchor()
    token = coord._capture_start_token(proc.pid)
    monkeypatch.setenv("FFS_RUN_ID", "peer")
    store = coord._open_store()
    holder = _hand_stamp_lease(
        store, "path:a", "exclusive", holder_uuid="dying",
        holder_anchor_pid=proc.pid, holder_anchor_start_token=token,
    )
    proc.kill()
    proc.wait(timeout=5)
    coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="exclusive"))  # B reclaims
    coord._close_store(store)

    store2 = coord._open_store()
    try:
        capsys.readouterr()
        rc = coord.cmd_lease_release(
            store2, _release_args(resource="path:a", generation=holder["generation"])
        )
        assert rc == 3
        cap = capsys.readouterr()
        combined = cap.out + cap.err
        assert "stale generation" in combined, combined
        assert "foreign holder" not in combined, combined
        with coord.registry_transaction(store2) as registry:
            entry = registry["leases"]["path:a"]
            assert "dying" not in entry["holders"]
            assert len(entry["holders"]) == 1
    finally:
        coord._close_store(store2)


def test_lease_release_idempotent_generation_must_match(repo, monkeypatch):
    monkeypatch.setenv("FFS_RUN_ID", "r1")
    store = coord._open_store()
    coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="exclusive"))
    coord.cmd_lease_release(store, _release_args(resource="path:a", generation=1))
    coord._close_store(store)

    store2 = coord._open_store()
    try:
        rc = coord.cmd_lease_release(store2, _release_args(resource="path:a", generation=2))
        assert rc == 3
    finally:
        coord._close_store(store2)


def test_lease_claim_tombstones_untouched(repo, monkeypatch):
    monkeypatch.setenv("FFS_RUN_ID", "r1")
    store = coord._open_store()
    try:
        coord.cmd_claim(store, argparse_namespace(spec_id="spec-009", ttl=None, heartbeat=None))
        coord.cmd_release(store, argparse_namespace(spec_id="spec-009", generation=1))
        with coord.registry_transaction(store) as registry:
            gen_entry = registry["generations"]["claim:spec-009"]
            assert gen_entry["last_holder_uuid"] is not None
            assert "released_holders" not in gen_entry
    finally:
        coord._close_store(store)


def test_lease_renew_happy_path_and_ttl_carry_forward(repo, monkeypatch):
    monkeypatch.setenv("FFS_RUN_ID", "r1")
    store = coord._open_store()
    try:
        coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="exclusive"))
        with coord.registry_transaction(store) as registry:
            holder_before = dict(next(iter(registry["leases"]["path:a"]["holders"].values())))

        rc = coord.cmd_lease_renew(store, _renew_args(resource="path:a", generation=1))
        assert rc == 0
        with coord.registry_transaction(store) as registry:
            holder = next(iter(registry["leases"]["path:a"]["holders"].values()))
        assert holder["generation"] == 1
        assert holder["last_renewed_at"] >= holder_before["last_renewed_at"]
        assert holder["ttl_secs"] == coord.DEFAULT_TTL_SECS  # carried forward, not reset

        rc2 = coord.cmd_lease_renew(store, _renew_args(resource="path:a", generation=1, ttl="9999"))
        assert rc2 == 0
        with coord.registry_transaction(store) as registry:
            holder = next(iter(registry["leases"]["path:a"]["holders"].values()))
        assert holder["ttl_secs"] == 9999.0
    finally:
        coord._close_store(store)


def test_lease_renew_fencing_writes_nothing_on_mismatch(repo, monkeypatch):
    monkeypatch.setenv("FFS_RUN_ID", "r1")
    store = coord._open_store()
    try:
        coord.cmd_lease_acquire(store, _lease_args(resource="path:a", mode="exclusive"))
        before = (store.store_root / "registry.json").read_bytes()
        rc = coord.cmd_lease_renew(store, _renew_args(resource="path:a", generation=99))
        assert rc == 4
        after = (store.store_root / "registry.json").read_bytes()
        assert before == after
    finally:
        coord._close_store(store)


def test_lease_renew_authority_gone_exits_4(repo, monkeypatch):
    store = coord._open_store()
    try:
        rc = coord.cmd_lease_renew(store, _renew_args(resource="path:never-acquired", generation=1))
        assert rc == 4
    finally:
        coord._close_store(store)

    monkeypatch.setenv("FFS_RUN_ID", "holder")
    store2 = coord._open_store()
    coord.cmd_lease_acquire(store2, _lease_args(resource="path:a", mode="exclusive"))
    coord._close_store(store2)

    monkeypatch.setenv("FFS_RUN_ID", "stranger")
    store3 = coord._open_store()
    try:
        rc = coord.cmd_lease_renew(store3, _renew_args(resource="path:a", generation=1))
        assert rc == 4
    finally:
        coord._close_store(store3)


def test_lease_renew_per_holder_generation_not_resource_wide(repo, monkeypatch):
    monkeypatch.setenv("FFS_COORD_ANCHOR_PID", str(os.getpid()))
    monkeypatch.setenv("FFS_RUN_ID", "a")
    store_a = coord._open_store()
    coord.cmd_lease_acquire(store_a, _lease_args(resource="path:a", mode="shared"))
    coord._close_store(store_a)

    monkeypatch.setenv("FFS_RUN_ID", "b")
    store_b = coord._open_store()
    coord.cmd_lease_acquire(store_b, _lease_args(resource="path:a", mode="shared"))
    coord._close_store(store_b)

    monkeypatch.setenv("FFS_RUN_ID", "a")
    store_a2 = coord._open_store()
    try:
        rc = coord.cmd_lease_renew(store_a2, _renew_args(resource="path:a", generation=1))
        assert rc == 0
        rc_wrong = coord.cmd_lease_renew(store_a2, _renew_args(resource="path:a", generation=2))
        assert rc_wrong == 4
    finally:
        coord._close_store(store_a2)


@pytest.mark.parametrize("bad", ["abc", "-1", "1.5", "", "9" * 200])
def test_lease_generation_arg_parsing_rejects_before_comparison(repo, bad):
    store = coord._open_store()
    try:
        with pytest.raises(coord.CoordExit) as exc:
            coord.cmd_lease_renew(store, _renew_args(resource="path:a", generation=bad))
        assert exc.value.code == 2
        with pytest.raises(coord.CoordExit) as exc2:
            coord.cmd_lease_release(store, _release_args(resource="path:a", generation=bad))
        assert exc2.value.code == 2
    finally:
        coord._close_store(store)


def test_lease_renew_release_grammar_still_validated(repo):
    store = coord._open_store()
    try:
        for bad in ("path:../outside", "path:docs/*.md", "spec-009", "path:a\x00b"):
            with pytest.raises(coord.CoordExit) as exc:
                coord.cmd_lease_renew(store, _renew_args(resource=bad, generation=1))
            assert exc.value.code == 2
            with pytest.raises(coord.CoordExit) as exc2:
                coord.cmd_lease_release(store, _release_args(resource=bad, generation=1))
            assert exc2.value.code == 2
    finally:
        coord._close_store(store)


def test_lease_escaped_lease_stays_renewable_and_releasable(repo, monkeypatch):
    """T-02-11: containment is enforced at ACQUISITION only. A path
    component that becomes an escaping symlink afterward must not strand
    the lease as unrenewable/unreleasable until TTL expiry."""
    monkeypatch.setenv("FFS_RUN_ID", "r1")
    (repo / "sub").mkdir()
    store = coord._open_store()
    try:
        rc = coord.cmd_lease_acquire(store, _lease_args(resource="path:sub/a.md", mode="exclusive"))
        assert rc == 0
    finally:
        coord._close_store(store)

    real_elsewhere = repo.parent / "elsewhere-escape2"
    real_elsewhere.mkdir()
    import shutil
    shutil.rmtree(repo / "sub")
    (repo / "sub").symlink_to(real_elsewhere)

    store2 = coord._open_store()
    try:
        rc_renew = coord.cmd_lease_renew(store2, _renew_args(resource="path:sub/a.md", generation=1))
        assert rc_renew == 0
        rc_release = coord.cmd_lease_release(store2, _release_args(resource="path:sub/a.md", generation=1))
        assert rc_release == 0
    finally:
        coord._close_store(store2)
