"""SQLite-backed run state for /feature and /fix lifecycle tracking."""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

DEFAULT_DB = Path.home() / ".claude" / "state" / "runs.db"

VALID_STATES = (
    "active",
    "pending_audit",
    "complete",
    "failed",
    "aborted",
)
VALID_SKILLS = ("feature", "fix")


class UnknownRunError(LookupError):
    """Raised when a mutator targets a run_id that matches no row.

    Subclasses LookupError (the stdlib base for "lookup miss") rather than
    KeyError (message-repr mangling) or ValueError (already used by
    create_run/update_state for invalid enum values).
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"no run found with id {run_id!r}")


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  skill TEXT NOT NULL,
  objective TEXT NOT NULL,
  state TEXT NOT NULL,
  session_id TEXT,
  current_phase TEXT,
  tokens_used INTEGER NOT NULL DEFAULT 0,
  tokens_budget INTEGER,
  audit_attempts INTEGER NOT NULL DEFAULT 0,
  last_audit_verdict TEXT,
  worktree TEXT,
  metadata_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_state ON runs(state);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, created_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db(db_path: Path = DEFAULT_DB) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@dataclass
class Run:
    id: str
    skill: str
    objective: str
    state: str
    session_id: Optional[str]
    current_phase: Optional[str]
    tokens_used: int
    tokens_budget: Optional[int]
    audit_attempts: int
    last_audit_verdict: Optional[str]
    worktree: Optional[str]
    metadata: dict
    created_at: str
    updated_at: str
    completed_at: Optional[str]


class RunStore:
    def __init__(self, db_path: Path = DEFAULT_DB) -> None:
        self.db_path = db_path
        init_db(db_path)

    def create_run(
        self,
        *,
        skill: str,
        objective: str,
        session_id: Optional[str] = None,
        tokens_budget: Optional[int] = None,
        worktree: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> str:
        if skill not in VALID_SKILLS:
            raise ValueError(f"skill must be one of {VALID_SKILLS}, got {skill!r}")
        run_id = uuid.uuid4().hex[:12]
        now = _now()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO runs (id, skill, objective, state, session_id,
                  tokens_budget, worktree, metadata_json,
                  created_at, updated_at)
                VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, skill, objective, session_id,
                    tokens_budget, worktree,
                    json.dumps(metadata or {}),
                    now, now,
                ),
            )
            conn.execute(
                "INSERT INTO events (run_id, event_type, payload_json, created_at) VALUES (?, 'created', ?, ?)",
                (run_id, json.dumps({"skill": skill, "objective": objective}), now),
            )
            conn.commit()
        finally:
            conn.close()
        return run_id

    def get_run(self, run_id: str) -> Optional[Run]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return Run(
            id=row["id"],
            skill=row["skill"],
            objective=row["objective"],
            state=row["state"],
            session_id=row["session_id"],
            current_phase=row["current_phase"],
            tokens_used=row["tokens_used"],
            tokens_budget=row["tokens_budget"],
            audit_attempts=row["audit_attempts"],
            last_audit_verdict=row["last_audit_verdict"],
            worktree=row["worktree"],
            metadata=json.loads(row["metadata_json"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    def update_state(self, run_id: str, new_state: str) -> None:
        if new_state not in VALID_STATES:
            raise ValueError(f"state must be one of {VALID_STATES}, got {new_state!r}")
        now = _now()
        completed_at = now if new_state == "complete" else None
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                "UPDATE runs SET state = ?, updated_at = ?, completed_at = COALESCE(?, completed_at) WHERE id = ?",
                (new_state, now, completed_at, run_id),
            )
            if cursor.rowcount == 0:
                raise UnknownRunError(run_id)
            conn.execute(
                "INSERT INTO events (run_id, event_type, payload_json, created_at) VALUES (?, 'state_change', ?, ?)",
                (run_id, json.dumps({"new_state": new_state}), now),
            )
            conn.commit()
        finally:
            conn.close()

    def recover_state(self, run_id: str, from_state: str, to_state: str) -> bool:
        """CAS: transition run_id from from_state to to_state in one UPDATE.

        Returns True iff the transition happened. Returns False — silently,
        no exception, no event row — when run_id is unknown OR its current
        state isn't from_state (already moved on: a concurrent abort/
        complete, or a verdict that landed before this call). For crash/
        interrupt cleanup paths that must never raise over the original
        error and must never clobber a state someone else already set.
        # ponytail: no VALID_STATES check — the only caller passes a
        # hardcoded literal, so an invalid to_state is a programmer error,
        # not a runtime input to validate.
        """
        now = _now()
        # review-gate round 3 HIGH: mirror update_state's completed_at
        # semantics — COALESCE so a non-complete transition never clears a
        # completed_at set earlier, and landing on "complete" always sets it.
        completed_at = now if to_state == "complete" else None
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                "UPDATE runs SET state = ?, updated_at = ?, completed_at = COALESCE(?, completed_at) WHERE id = ? AND state = ?",
                (to_state, now, completed_at, run_id, from_state),
            )
            if cursor.rowcount == 0:
                return False
            conn.execute(
                "INSERT INTO events (run_id, event_type, payload_json, created_at) VALUES (?, 'state_change', ?, ?)",
                (run_id, json.dumps({"new_state": to_state, "recovered_from": from_state}), now),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def update_phase(self, run_id: str, phase: str) -> None:
        now = _now()
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                "UPDATE runs SET current_phase = ?, updated_at = ? WHERE id = ?",
                (phase, now, run_id),
            )
            if cursor.rowcount == 0:
                raise UnknownRunError(run_id)
            conn.execute(
                "INSERT INTO events (run_id, event_type, payload_json, created_at) VALUES (?, 'phase', ?, ?)",
                (run_id, json.dumps({"phase": phase}), now),
            )
            conn.commit()
        finally:
            conn.close()

    def inc_tokens(self, run_id: str, delta: int) -> tuple[int, int] | None:
        if delta < 0:
            raise ValueError("delta must be non-negative")
        now = _now()
        conn = sqlite3.connect(self.db_path)
        try:
            before_row = conn.execute("SELECT tokens_used, tokens_budget FROM runs WHERE id = ?", (run_id,)).fetchone()
            if before_row is None:
                raise UnknownRunError(run_id)
            conn.execute(
                "UPDATE runs SET tokens_used = tokens_used + ?, updated_at = ? WHERE id = ?",
                (delta, now, run_id),
            )
            row = conn.execute(
                "SELECT tokens_used, tokens_budget FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            crossed = bool(row and row[1] is not None and before_row[0] < row[1] <= row[0])
            if crossed:
                conn.execute(
                    "INSERT INTO events (run_id, event_type, payload_json, created_at) VALUES (?, 'budget_limit_hit', ?, ?)",
                    (run_id, json.dumps({"tokens_used": row[0], "tokens_budget": row[1]}), now),
                )
            conn.commit()
            return (row[1], row[0]) if crossed else None
        finally:
            conn.close()

    def list_runs(self, state: Optional[str] = None) -> Iterable[Run]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            if state:
                rows = conn.execute("SELECT id FROM runs WHERE state = ? ORDER BY created_at DESC", (state,)).fetchall()
            else:
                rows = conn.execute("SELECT id FROM runs ORDER BY created_at DESC").fetchall()
        finally:
            conn.close()
        for row in rows:
            run = self.get_run(row["id"])
            if run is not None:
                yield run
