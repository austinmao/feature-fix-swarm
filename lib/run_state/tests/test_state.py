import sqlite3
from pathlib import Path

import pytest

from run_state.state import init_db, RunStore


def test_init_db_creates_runs_table(tmp_path: Path) -> None:
    db_path = tmp_path / "runs.db"
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "runs" in tables
    assert "events" in tables


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "runs.db"
    init_db(db_path)
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    assert count >= 2


def test_create_run_returns_run_id(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    run_id = store.create_run(skill="fix", objective="auth redirect broken", tokens_budget=250_000)
    assert run_id
    assert len(run_id) >= 8


def test_create_run_persists(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    run_id = store.create_run(skill="feature", objective="ship spec 130")
    run = store.get_run(run_id)
    assert run is not None
    assert run.skill == "feature"
    assert run.objective == "ship spec 130"
    assert run.state == "active"
    assert run.tokens_used == 0
    assert run.continuation_count == 0


def test_create_run_rejects_invalid_skill(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    with pytest.raises(ValueError, match="skill"):
        store.create_run(skill="not-a-skill", objective="x")


def test_update_state_transitions(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    run_id = store.create_run(skill="fix", objective="x")
    store.update_state(run_id, "pending_audit")
    assert store.get_run(run_id).state == "pending_audit"
    store.update_state(run_id, "complete")
    assert store.get_run(run_id).state == "complete"
    assert store.get_run(run_id).completed_at is not None


def test_update_state_rejects_invalid_state(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    run_id = store.create_run(skill="fix", objective="x")
    with pytest.raises(ValueError, match="state"):
        store.update_state(run_id, "bogus")


def test_inc_tokens_accumulates(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    run_id = store.create_run(skill="fix", objective="x", tokens_budget=1000)
    store.inc_tokens(run_id, 300)
    store.inc_tokens(run_id, 200)
    assert store.get_run(run_id).tokens_used == 500


def test_inc_tokens_flips_to_budget_limited(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    run_id = store.create_run(skill="fix", objective="x", tokens_budget=500)
    store.inc_tokens(run_id, 600)
    assert store.get_run(run_id).state == "budget_limited"


def test_inc_continuation_caps_at_max(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    run_id = store.create_run(skill="fix", objective="x", max_continuations=3)
    for _ in range(3):
        assert store.inc_continuation(run_id) is True
    assert store.inc_continuation(run_id) is False
    assert store.get_run(run_id).state == "paused"


def test_list_active_runs(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    a = store.create_run(skill="fix", objective="bug A")
    b = store.create_run(skill="feature", objective="spec B")
    store.update_state(b, "complete")
    active = list(store.list_runs(state="active"))
    assert len(active) == 1
    assert active[0].id == a
