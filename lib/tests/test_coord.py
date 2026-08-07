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
def test_by_run_pointer_content_validation_rejects(repo, bad):
    """T-01-14: pointer CONTENT is untrusted input from the filesystem, not
    covered by env validation — must be checked against the strict uuid4
    form before any filesystem use."""
    store = coord._open_store()
    # write the pointer directly through the held descriptor, bypassing the
    # publish helper (which would itself validate on the writer's side).
    fd = os.open("r1", os.O_CREAT | os.O_WRONLY, 0o644, dir_fd=store.by_run_fd)
    with os.fdopen(fd, "w") as f:
        f.write(bad)
    with pytest.raises(coord.CoordExit) as exc:
        coord._read_by_run_pointer(store, "r1")
    assert exc.value.code == 69
    after = coord._read_text_fd(store.by_run_fd, "r1")
    assert after == bad  # left byte-identical — never repaired or reminted
    coord._close_store(store)


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
