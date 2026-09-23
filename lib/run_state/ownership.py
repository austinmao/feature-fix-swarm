"""Atomic, fenced ownership reservations for the isolated ControlStore."""
from __future__ import annotations

import secrets
import json
import os
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from process_identity import DEAD, LIVE, UNKNOWN, ProcessIdentity, probe_identity
from .state import ControlStore, ControlStoreRefused

__all__ = [
    "ControlStore",
    "ControlStoreRefused",
    "OwnerToken",
    "Ownership",
    "OwnershipRefused",
    "ProcessIdentity",
    "StartRequest",
    "assert_owner",
    "release_owner",
    "reserve_launch",
    "acknowledge_child",
    "authorize_child",
    "recover_intent",
    "reserve_resources",
]


class OwnershipRefused(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class StartRequest:
    run_id: str
    workspace: str
    objective_digest: str
    owner: ProcessIdentity
    repository_id: str | None = None
    planning_scope: str = ""


@dataclass(frozen=True)
class OwnerToken:
    run_id: str
    workspace: str
    objective_digest: str
    repository_id: str
    planning_scope: str
    generation: int
    nonce: str
    role: str = "supervisor"

    def __repr__(self) -> str:  # capabilities must not enter diagnostics
        return ("OwnerToken(run_id={!r}, workspace={!r}, objective_digest={!r}, "
                "repository_id={!r}, planning_scope={!r}, generation={!r}, "
                "nonce=<redacted>, role={!r})").format(
                    self.run_id, self.workspace, self.objective_digest,
                    self.repository_id, self.planning_scope, self.generation, self.role)


@dataclass(frozen=True)
class Ownership:
    run_id: str
    generation: int
    token: OwnerToken


def _encoded(value: dict[str, str]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _canonical_workspace(raw: str) -> str:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = Path(os.path.normpath(os.fspath(candidate)))
    missing: list[str] = []
    existing = candidate
    while not existing.exists():
        if existing == existing.parent:
            break
        missing.append(existing.name)
        existing = existing.parent
    try:
        resolved = existing.resolve(strict=True)
    except OSError:
        resolved = existing.absolute()
    return os.fspath(resolved.joinpath(*reversed(missing)))


def _workspace_key(workspace: str) -> str:
    canonical = _canonical_workspace(workspace)
    if sys.platform == "darwin":
        canonical = unicodedata.normalize("NFC", canonical)
        probe = Path(canonical)
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            sensitivity = os.pathconf(probe, 11)  # Darwin _PC_CASE_SENSITIVE
        except (OSError, ValueError):
            raise OwnershipRefused("WORKSPACE_IDENTITY_UNKNOWN") from None
        if sensitivity not in (0, 1):
            raise OwnershipRefused("WORKSPACE_IDENTITY_UNKNOWN")
        case_sensitive = bool(sensitivity)
        if not case_sensitive:
            canonical = canonical.casefold()
    return canonical


def durable_workspace_key(workspace: str) -> str:
    """Portable key for durable registrations and Git-derived names."""
    return unicodedata.normalize("NFC", _canonical_workspace(workspace)).casefold()


def _registered_binding_conflict(tx, request: StartRequest, workspace: str) -> str | None:
    """Check durable 05-03 bindings in the reservation writer snapshot."""
    if request.repository_id is None:
        return None
    try:
        row = tx.execute(
            "SELECT repository_id, run_id FROM context_runs "
            "WHERE workspace_key = ? AND state NOT IN ('complete', 'failed', 'aborted')",
            (durable_workspace_key(workspace),),
        ).fetchone()
        if row is not None and (
            row["repository_id"] != request.repository_id or row["run_id"] != request.run_id
        ):
            return "WORKSPACE_REGISTERED"
        row = tx.execute(
            "SELECT run_id FROM context_runs WHERE repository_id = ? "
            "AND planning_scope = ? AND objective_digest = ? "
            "AND state NOT IN ('complete', 'failed', 'aborted')",
            (request.repository_id, request.planning_scope, request.objective_digest),
        ).fetchone()
        if row is not None and row["run_id"] != request.run_id:
            return "OBJECTIVE_RESERVED"
    except Exception as error:
        # Context tables are opt-in. Only their complete absence means this is
        # a 05-02-only store; malformed/present tables must fail closed.
        if "no such table: context_runs" in str(error).lower():
            return None
        raise
    return None


def _keys(request: StartRequest) -> tuple[str, str, str, str, str]:
    repository = request.repository_id or "__isolated_low_level_namespace__"
    workspace = _canonical_workspace(request.workspace)
    return repository, request.planning_scope, request.run_id, workspace, request.objective_digest


def _token_keys(token: OwnerToken) -> tuple[tuple[str, str], ...]:
    return (
        ("run", _encoded({"repository_id": token.repository_id, "run_id": token.run_id})),
        ("workspace", _encoded({"workspace": _workspace_key(token.workspace)})),
        ("objective", _encoded({"repository_id": token.repository_id,
                                 "planning_scope": token.planning_scope,
                                 "objective_digest": token.objective_digest})),
    )


def reserve_resources(store: ControlStore, request: StartRequest) -> Ownership:
    """Reserve run, workspace and objective in one immediate SQLite transaction."""
    principal = ProcessIdentity.current()
    if request.owner != principal:
        raise OwnershipRefused("OWNER_IDENTITY_MISMATCH")
    repository, scope, run_id, workspace, objective = _keys(request)
    token_keys = (
        ("run", _encoded({"repository_id": repository, "run_id": run_id})),
        ("workspace", _encoded({"workspace": _workspace_key(workspace)})),
        ("objective", _encoded({"repository_id": repository, "planning_scope": scope,
                                 "objective_digest": objective})),
    )
    for _attempt in range(3):
        # Native liveness probing can call sysctl and must never occur while a
        # SQLite writer transaction is held.  The exact snapshot is checked
        # again under BEGIN IMMEDIATE before any mutation.
        snapshot = store.held_reservations(token_keys)
        statuses: dict[str, str] = {}
        for row in snapshot:
            owner_set = row["owner_set"]
            if owner_set not in statuses:
                recorded = ProcessIdentity(row["host_id"], row["boot_id"], row["pid"], row["start_token"])
                native_status = probe_identity(recorded)
                if store.liveness_probe is None:
                    status = native_status
                else:
                    try:
                        injected_status = store.liveness_probe(recorded)
                    except Exception:
                        injected_status = UNKNOWN
                    if injected_status not in (LIVE, DEAD, UNKNOWN):
                        injected_status = UNKNOWN
                    # The seam may force uncertainty. It cannot turn native
                    # LIVE into DEAD or authorize reclaim without native DEAD.
                    if native_status == LIVE:
                        status = UNKNOWN if injected_status == UNKNOWN else LIVE
                    elif native_status == DEAD:
                        status = DEAD if injected_status == DEAD else UNKNOWN
                    else:
                        status = UNKNOWN
                statuses[owner_set] = status
        if LIVE in statuses.values():
            raise OwnershipRefused("OWNER_LIVE")
        if any(status != DEAD for status in statuses.values()):
            raise OwnershipRefused("OWNER_UNKNOWN")
        with store.transaction() as tx:
            rows = store.held_reservations(token_keys, connection=tx)

            def signature(values):
                return {
                    (
                        row["owner_set"], row["resource_type"], row["resource_key"],
                        row["generation"], row["nonce"], row["host_id"],
                        row["boot_id"], row["pid"], row["start_token"],
                    )
                    for row in values
                }

            if signature(rows) != signature(snapshot):
                continue
            conflict = _registered_binding_conflict(tx, request, workspace)
            if conflict is not None:
                raise OwnershipRefused(conflict)
            for owner_set in statuses:
                tx.execute("UPDATE control_reservations SET held = 0, released_at = CURRENT_TIMESTAMP WHERE owner_set = ? AND held = 1", (owner_set,))
            prior = tx.execute("SELECT value FROM control_generation WHERE singleton = 1").fetchone()[0]
            if prior >= 9_223_372_036_854_775_807:
                raise OwnershipRefused("GENERATION_EXHAUSTED")
            tx.execute("UPDATE control_generation SET value = value + 1 WHERE singleton = 1")
            generation = tx.execute("SELECT value FROM control_generation WHERE singleton = 1").fetchone()[0]
            nonce = secrets.token_urlsafe(32)
            owner_set = secrets.token_hex(16)
            for resource_type, resource_key in token_keys:
                tx.execute(
                    "INSERT INTO control_reservations (owner_set, resource_type, resource_key, generation, nonce, host_id, boot_id, pid, start_token, held) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (owner_set, resource_type, resource_key, generation, nonce, principal.host_id,
                     principal.boot_id, principal.pid, principal.start_token),
                )
            tx.execute(
                "INSERT INTO control_events (event_type, payload) VALUES ('resources_reserved', ?)",
                (_encoded({"repository_id": repository, "run_id": run_id}),),
            )
            break
    else:
        raise OwnershipRefused("OWNER_CHANGED")
    token = OwnerToken(run_id, workspace, objective, repository, scope, generation, nonce)
    return Ownership(run_id, generation, token)


def assert_owner(tx, token: OwnerToken) -> None:
    if (
        not isinstance(token, OwnerToken)
        or token.role != "supervisor"
        or isinstance(token.generation, bool)
        or not isinstance(token.generation, int)
        or token.generation <= 0
        or not isinstance(token.nonce, str)
        or not token.nonce
    ):
        raise OwnershipRefused("FENCE_REVOKED")
    keys = _token_keys(token)
    placeholders = ",".join("(?, ?)" for _ in keys)
    parameters = [value for key in keys for value in key]
    rows = tx.execute(
        "SELECT owner_set, resource_type, resource_key, generation, nonce "
        "FROM control_reservations WHERE held = 1 "
        f"AND (resource_type, resource_key) IN ({placeholders})",
        parameters,
    ).fetchall()
    if (
        len(rows) != len(keys)
        or {(row["resource_type"], row["resource_key"]) for row in rows} != set(keys)
        or len({row["owner_set"] for row in rows}) != 1
        or any(row["generation"] != token.generation or row["nonce"] != token.nonce for row in rows)
    ):
        raise OwnershipRefused("FENCE_REVOKED")


def release_owner(tx, token: OwnerToken) -> None:
    assert_owner(tx, token)
    keys = _token_keys(token)
    first_type, first_key = keys[0]
    owner_set = tx.execute(
        "SELECT owner_set FROM control_reservations WHERE resource_type = ? AND resource_key = ? "
        "AND held = 1 AND generation = ? AND nonce = ?",
        (first_type, first_key, token.generation, token.nonce),
    ).fetchone()["owner_set"]
    changed = tx.execute(
        "UPDATE control_reservations SET held = 0, released_at = CURRENT_TIMESTAMP "
        "WHERE owner_set = ? AND held = 1 AND generation = ? AND nonce = ?",
        (owner_set, token.generation, token.nonce),
    ).rowcount
    if changed != len(keys):
        raise OwnershipRefused("FENCE_REVOKED")
    tx.execute(
        "INSERT INTO control_events (event_type, payload) VALUES ('resources_released', ?)",
        (_encoded({"repository_id": token.repository_id, "run_id": token.run_id}),),
    )


def reserve_launch(store: ControlStore, activity_id: str, token: OwnerToken):
    return store.reserve_launch(activity_id, token)


def acknowledge_child(
    store: ControlStore, intent_id: str, token: OwnerToken, process_identity: ProcessIdentity,
):
    return store.acknowledge_child(intent_id, token, process_identity)


def authorize_child(store: ControlStore, acknowledgement, token: OwnerToken):
    return store.authorize_child(acknowledgement, token)


def recover_intent(store: ControlStore, intent_id: str, token: OwnerToken):
    return store.recover_intent(intent_id, token)
