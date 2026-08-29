import sqlite3
from pathlib import Path

import pytest

from run_state.state import init_db, RunStore

# NOTE: UnknownRunError does not exist until Task 2 (GREEN). Importing it at
# module scope would break collection of every test in this file, including
# the pre-existing ones the RED gate must keep green — so each new test below
# imports it locally, letting only those tests fail during RED.


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


def test_inc_tokens_does_not_change_state(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    run_id = store.create_run(skill="fix", objective="x", tokens_budget=500)
    store.inc_tokens(run_id, 600)
    assert store.get_run(run_id).state == "active"


def test_inc_tokens_reports_only_the_budget_crossing(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    run_id = store.create_run(skill="fix", objective="x", tokens_budget=500)
    assert store.inc_tokens(run_id, 499) is None
    assert store.inc_tokens(run_id, 1) == (500, 500)
    assert store.inc_tokens(run_id, 1) is None


def test_list_active_runs(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    a = store.create_run(skill="fix", objective="bug A")
    b = store.create_run(skill="feature", objective="spec B")
    store.update_state(b, "complete")
    active = list(store.list_runs(state="active"))
    assert len(active) == 1
    assert active[0].id == a


def test_legacy_v2_db_with_dropped_states_readable(tmp_path: Path) -> None:
    """v3.0 codex-gate Pass 3 #2: v2.x DBs have rows in `paused` and `budget_limited`
    states + extra columns (continuation_count, max_continuations). v3.0 must read
    them gracefully — no crash, no data loss."""
    import sqlite3
    db_path = tmp_path / "runs.db"

    # Simulate a v2.x DB: full schema + rows in dropped states + extra cols.
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE runs (
          id TEXT PRIMARY KEY,
          skill TEXT NOT NULL,
          objective TEXT NOT NULL,
          state TEXT NOT NULL,
          session_id TEXT,
          current_phase TEXT,
          tokens_used INTEGER NOT NULL DEFAULT 0,
          tokens_budget INTEGER,
          continuation_count INTEGER NOT NULL DEFAULT 0,
          max_continuations INTEGER NOT NULL DEFAULT 500,
          audit_attempts INTEGER NOT NULL DEFAULT 0,
          last_audit_verdict TEXT,
          worktree TEXT,
          metadata_json TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          completed_at TEXT
        );
        CREATE TABLE events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          payload_json TEXT,
          created_at TEXT NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO runs (id, skill, objective, state, continuation_count, max_continuations, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy-paused-1", "fix", "old paused run", "paused", 42, 500, "2026-04-01T00:00:00Z", "2026-04-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO runs (id, skill, objective, state, continuation_count, max_continuations, tokens_used, tokens_budget, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy-budget-1", "feature", "old budget run", "budget_limited", 7, 500, 5000, 1000, "2026-04-01T00:00:00Z", "2026-04-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    # v3.0 RunStore must NOT crash on init (init_db is CREATE TABLE IF NOT EXISTS — won't migrate, that's OK).
    store = RunStore(db_path)

    # Reading legacy rows directly via list_runs (no state filter) must work.
    all_runs = list(store.list_runs())
    assert len(all_runs) == 2
    ids = {r.id for r in all_runs}
    assert ids == {"legacy-paused-1", "legacy-budget-1"}

    # Reading by id works for both legacy states.
    paused = store.get_run("legacy-paused-1")
    assert paused is not None
    assert paused.state == "paused"  # legacy state preserved on read
    assert paused.skill == "fix"

    budget = store.get_run("legacy-budget-1")
    assert budget is not None
    assert budget.state == "budget_limited"
    assert budget.tokens_used == 5000
    assert budget.tokens_budget == 1000


def test_legacy_v2_run_can_be_aborted(tmp_path: Path) -> None:
    """v3.0: operator should be able to abort stale v2.x rows via standard CLI path."""
    import sqlite3
    db_path = tmp_path / "runs.db"

    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE runs (
          id TEXT PRIMARY KEY, skill TEXT NOT NULL, objective TEXT NOT NULL,
          state TEXT NOT NULL, session_id TEXT, current_phase TEXT,
          tokens_used INTEGER NOT NULL DEFAULT 0, tokens_budget INTEGER,
          continuation_count INTEGER NOT NULL DEFAULT 0,
          max_continuations INTEGER NOT NULL DEFAULT 500,
          audit_attempts INTEGER NOT NULL DEFAULT 0, last_audit_verdict TEXT,
          worktree TEXT, metadata_json TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT
        );
        CREATE TABLE events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
          event_type TEXT NOT NULL, payload_json TEXT, created_at TEXT NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO runs (id, skill, objective, state, created_at, updated_at) "
        "VALUES ('legacy-1', 'fix', 'x', 'paused', '2026-04-01T00:00:00Z', '2026-04-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    store = RunStore(db_path)
    # `aborted` is in v3.0 VALID_STATES, so the transition itself is allowed.
    store.update_state("legacy-1", "aborted")
    assert store.get_run("legacy-1").state == "aborted"


# --- GH-3: unknown run_id must raise, never fabricate history ---------------


def test_update_state_unknown_run_id_raises(tmp_path: Path) -> None:
    from run_state.state import UnknownRunError
    store = RunStore(tmp_path / "runs.db")
    with pytest.raises(UnknownRunError):
        store.update_state("nope-000000", "complete")


def test_update_phase_unknown_run_id_raises(tmp_path: Path) -> None:
    from run_state.state import UnknownRunError
    store = RunStore(tmp_path / "runs.db")
    with pytest.raises(UnknownRunError):
        store.update_phase("nope-000000", "wedge-1")


def test_inc_tokens_unknown_run_id_raises(tmp_path: Path) -> None:
    from run_state.state import UnknownRunError
    store = RunStore(tmp_path / "runs.db")
    with pytest.raises(UnknownRunError):
        store.inc_tokens("nope-000000", 100)


def test_unknown_run_id_writes_no_event_row(tmp_path: Path) -> None:
    from run_state.state import UnknownRunError
    db_path = tmp_path / "runs.db"
    store = RunStore(db_path)
    for call in (
        lambda: store.update_state("nope-000000", "complete"),
        lambda: store.update_phase("nope-000000", "wedge-1"),
        lambda: store.inc_tokens("nope-000000", 100),
    ):
        with pytest.raises(UnknownRunError):
            call()
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


# --- review-gate HIGH: recover_state must be an atomic CAS, never clobber a
# terminal/concurrently-moved state -------------------------------------------


def test_recover_state_transitions_matching_state(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    run_id = store.create_run(skill="fix", objective="x")
    store.update_state(run_id, "pending_audit")
    assert store.recover_state(run_id, "pending_audit", "active") is True
    assert store.get_run(run_id).state == "active"


def test_recover_state_noop_when_current_state_does_not_match(tmp_path: Path) -> None:
    # Simulates a concurrent abort/complete landing during the audit
    # subprocess: recover_state must not overwrite it back to "active".
    store = RunStore(tmp_path / "runs.db")
    run_id = store.create_run(skill="fix", objective="x")
    store.update_state(run_id, "aborted")
    assert store.recover_state(run_id, "pending_audit", "active") is False
    assert store.get_run(run_id).state == "aborted"


def test_recover_state_noop_when_already_settled_to_complete(tmp_path: Path) -> None:
    # Simulates a signal landing after the verdict path already wrote
    # "complete" but before the caller's settled flag flips.
    store = RunStore(tmp_path / "runs.db")
    run_id = store.create_run(skill="fix", objective="x")
    store.update_state(run_id, "pending_audit")
    store.update_state(run_id, "complete")
    assert store.recover_state(run_id, "pending_audit", "active") is False
    assert store.get_run(run_id).state == "complete"


def test_recover_state_noop_on_unknown_run_id(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    assert store.recover_state("nope-000000", "pending_audit", "active") is False


def test_recover_state_writes_no_event_row_on_noop(tmp_path: Path) -> None:
    db_path = tmp_path / "runs.db"
    store = RunStore(db_path)
    run_id = store.create_run(skill="fix", objective="x")  # state=active
    conn = sqlite3.connect(db_path)
    try:
        before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()

    assert store.recover_state(run_id, "pending_audit", "active") is False

    conn = sqlite3.connect(db_path)
    try:
        after = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()
    assert after == before
