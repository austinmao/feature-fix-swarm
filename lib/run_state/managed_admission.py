"""Shared resource-derived admission authority; v1 is migrated in place."""

from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import pwd
import secrets
import sqlite3
import stat
import time
from typing import Callable, Mapping, Protocol

from process_identity import DEAD, LIVE, ProcessIdentity, probe_identity
from .resource_observation import (
    ResourceDemand,
    ResourceObservation,
)
from .resource_scheduler import ResourceScheduler, demand_from_record

DEFAULT_MANAGED_RUN_CAPACITY = 2  # compatibility symbol, never an authority bound
GLOBAL_ROOT_ENV = "FFS_MANAGED_ADMISSION_ROOT"
_V2 = 2


class ManagedAdmissionRefused(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AdmissionTicket:
    sequence: int
    ticket: str
    owner: ProcessIdentity


@dataclass(frozen=True)
class LeaseIdentity:
    repository_id: str
    run_id: str
    request_key: str
    launch_intent_id: str | None
    generation: int
    supervisor: ProcessIdentity
    consumer: ProcessIdentity | None = None

    def __post_init__(self):
        if (
            not all(
                isinstance(v, str) and v
                for v in (
                    self.repository_id,
                    self.run_id,
                    self.request_key,
                )
            )
            or type(self.generation) is not int
            or self.generation <= 0
            or (self.launch_intent_id is not None
                and (not isinstance(self.launch_intent_id, str) or not self.launch_intent_id))
        ):
            raise ManagedAdmissionRefused("LEASE_IDENTITY_INVALID")


@dataclass(frozen=True)
class FencedLeaseRecord:
    """Exact immutable record returned by the authoritative ControlStore reader."""

    repository_id: str
    run_id: str
    request_key: str
    launch_intent_id: str | None
    generation: int
    recorded_writer: ProcessIdentity
    intent_state: str
    request_state: str
    record_hash: str

    def proves_never_authorized(self, lease: LeaseIdentity) -> bool:
        return (
            (
                self.repository_id,
                self.run_id,
                self.request_key,
                self.launch_intent_id,
                self.generation,
            )
            == (
                lease.repository_id,
                lease.run_id,
                lease.request_key,
                lease.launch_intent_id,
                lease.generation,
            )
            and self.recorded_writer == lease.supervisor
            and self.intent_state == "never_authorized"
            and self.request_state == "generation_fenced"
            and len(self.record_hash) == 64
        )


class LeaseEvidenceReader(Protocol):
    """Read-only ControlStore seam; call it before, never inside, registry SQL."""

    def read_fenced_lease(
        self, identity: LeaseIdentity
    ) -> FencedLeaseRecord | None: ...


def global_admission_root() -> Path:
    try:
        root = (
            Path(os.environ[GLOBAL_ROOT_ENV])
            if os.environ.get(GLOBAL_ROOT_ENV)
            else Path(pwd.getpwuid(os.getuid()).pw_dir)
            / ".local/state/feature-fix-swarm/managed-admission"
        )
    except (KeyError, OSError) as error:
        raise ManagedAdmissionRefused("MANAGED_ADMISSION_ROOT_UNSAFE") from error
    if not root.is_absolute() or ".." in root.parts or root.is_symlink():
        raise ManagedAdmissionRefused("MANAGED_ADMISSION_ROOT_UNSAFE")
    return root.resolve()


def _identity(row, prefix=""):
    fields = tuple(prefix + x for x in ("host_id", "boot_id", "pid", "start_token"))
    return (
        None
        if any(row[x] is None for x in fields)
        else ProcessIdentity(*(row[x] for x in fields))
    )


def _encode(demand):
    return json.dumps(demand.record(), sort_keys=True, separators=(",", ":"))


class ManagedAdmissionQueue:
    """One private DELETE-journal database; active count is only a metric."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        liveness_probe=None,
        observation_provider: Callable[[], ResourceObservation] | None = None,
        ceilings: Mapping[str, int] | None = None,
        scheduler=None,
    ):
        root = global_admission_root() if root is None else Path(root)
        if not root.is_absolute() or ".." in root.parts or root.is_symlink():
            raise ManagedAdmissionRefused("MANAGED_ADMISSION_ROOT_UNSAFE")
        self.root, self.path = root.resolve(), root.resolve() / "admission.sqlite3"
        from .resource_watchdog import LocalObservationCollector
        self._liveness_probe, self._observe = (
            liveness_probe,
            observation_provider or LocalObservationCollector(),
        )
        self._ceilings, self._scheduler, self._nonce = (
            dict(ceilings or {}),
            scheduler or ResourceScheduler(),
            None,
        )
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.root.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise ManagedAdmissionRefused("MANAGED_ADMISSION_ROOT_UNSAFE")
            self._root_identity = (info.st_dev, info.st_ino)
            if not self.path.exists():
                self._initialize()
            self._migrate()
            info = self.path.lstat()
            self._database_identity = (info.st_dev, info.st_ino)
            with self._connection() as c:
                row = c.execute(
                    "SELECT version,writer_nonce FROM admission_policy WHERE singleton=1"
                ).fetchone()
                if row is None or row["version"] != _V2 or not row["writer_nonce"]:
                    raise ManagedAdmissionRefused("MANAGED_ADMISSION_SCHEMA_INVALID")
                self._nonce = row["writer_nonce"]
                c.execute(
                    "SELECT writer_version,demand_json FROM managed_admissions LIMIT 0"
                )
        except ManagedAdmissionRefused:
            raise
        except (OSError, sqlite3.Error) as error:
            raise ManagedAdmissionRefused(
                "MANAGED_ADMISSION_STORE_UNAVAILABLE"
            ) from error

    @staticmethod
    def _schema(c):
        nonce = secrets.token_hex(32)
        c.execute(
            "CREATE TABLE admission_policy (singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL CHECK(version=2),writer_nonce TEXT NOT NULL,scheduler_cursor TEXT,last_progress_ns INTEGER NOT NULL DEFAULT 0)"
        )
        c.execute(
            "CREATE TABLE managed_admissions (sequence INTEGER PRIMARY KEY AUTOINCREMENT,ticket TEXT NOT NULL UNIQUE,state_root TEXT NOT NULL,run_id TEXT NOT NULL,host_id TEXT NOT NULL,boot_id TEXT NOT NULL,pid INTEGER NOT NULL CHECK(pid>0),start_token TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('waiting','active','released','reclaimed')),repository_id TEXT NOT NULL DEFAULT '',request_key TEXT NOT NULL DEFAULT '',generation INTEGER NOT NULL DEFAULT 1,writer_version INTEGER NOT NULL DEFAULT 1,demand_json TEXT NOT NULL,group_id TEXT,group_width INTEGER NOT NULL DEFAULT 1,group_age_ns INTEGER NOT NULL DEFAULT 0,next_recheck_ns INTEGER NOT NULL DEFAULT 0,limiting_resource TEXT,observation_age_ns INTEGER,last_progress_ns INTEGER NOT NULL DEFAULT 0,launch_intent_id TEXT,child_host_id TEXT,child_boot_id TEXT,child_pid INTEGER,child_start_token TEXT)"
        )
        c.execute(
            "CREATE INDEX admission_order ON managed_admissions(status,run_id,sequence)"
        )
        c.execute(
            "CREATE INDEX admission_group ON managed_admissions(status,group_id,sequence)"
        )
        c.execute(
            "INSERT INTO admission_policy(singleton,version,writer_nonce) VALUES(1,2,?)",
            (nonce,),
        )
        ManagedAdmissionQueue._fences(c)

    @staticmethod
    def _fences(c):
        from .provider_feedback import ensure_schema
        ensure_schema(c)
        # v1 clients receive the v1 default writer_version on migration.  The
        # insert fence therefore also covers already-open/raw clients, while
        # release remains deliberately executable for the old writer.
        c.execute(
            "CREATE TRIGGER IF NOT EXISTS admission_fence_enqueue BEFORE INSERT ON managed_admissions WHEN NEW.writer_version!=2 BEGIN SELECT RAISE(ABORT,'LEGACY_ADMISSION_WRITE_REFUSED'); END"
        )
        c.execute(
            "CREATE TRIGGER IF NOT EXISTS admission_fence_activate BEFORE UPDATE OF status ON managed_admissions WHEN OLD.status='waiting' AND NEW.status='active' AND OLD.writer_version!=2 BEGIN SELECT RAISE(ABORT,'LEGACY_ADMISSION_WRITE_REFUSED'); END"
        )

    def _initialize(self):
        stage = self.root / (".admission-" + secrets.token_hex(16) + ".sqlite3")
        fd = os.open(stage, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        try:
            with sqlite3.connect(stage, isolation_level=None) as c:
                c.execute("PRAGMA journal_mode=DELETE")
                c.execute("PRAGMA synchronous=FULL")
                c.execute("BEGIN IMMEDIATE")
                self._schema(c)
                c.commit()
            try:
                os.link(stage, self.path, follow_symlinks=False)
            except FileExistsError:
                pass
        finally:
            stage.unlink(missing_ok=True)

    def _migrate(self):
        # Verify the exact private regular file before opening it for mutation,
        # and once again inside the short serialized migration window.
        info = self.path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ManagedAdmissionRefused("MANAGED_ADMISSION_STORE_UNSAFE")
        expected = (info.st_dev, info.st_ino)
        c = sqlite3.connect(
            self.path.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=2
        )
        try:
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA busy_timeout=2000")
            current = self.path.lstat()
            if (
                (current.st_dev, current.st_ino) != expected
                or not stat.S_ISREG(current.st_mode)
                or stat.S_IMODE(current.st_mode) != 0o600
            ):
                raise ManagedAdmissionRefused("MANAGED_ADMISSION_STORE_UNSAFE")
            if c.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise ManagedAdmissionRefused("MANAGED_ADMISSION_STORE_UNSAFE")
            version = c.execute(
                "SELECT version FROM admission_policy WHERE singleton=1"
            ).fetchone()[0]
            if version == _V2:
                c.execute("BEGIN IMMEDIATE")
                self._fences(c)
                c.commit()
                return
            if version != 1:
                raise ManagedAdmissionRefused("MANAGED_ADMISSION_SCHEMA_INVALID")
            c.execute("BEGIN IMMEDIATE")
            nonce = secrets.token_hex(32)
            # SQLite cannot remove v1 CHECK(version=1) with ALTER.  Rebuild
            # only the one-row policy table inside this same database file;
            # managed_admissions (and its inode, fields, rows and release SQL)
            # are never copied or replaced.
            c.execute(
                "CREATE TABLE admission_policy_v2 (singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL CHECK(version=2),writer_nonce TEXT NOT NULL,scheduler_cursor TEXT,last_progress_ns INTEGER NOT NULL DEFAULT 0)"
            )
            c.execute(
                "INSERT INTO admission_policy_v2(singleton,version,writer_nonce) VALUES(1,2,?)",
                (nonce,),
            )
            c.execute("DROP TABLE admission_policy")
            c.execute("ALTER TABLE admission_policy_v2 RENAME TO admission_policy")
            # Defaults mark old rows v1; old ticket columns are unchanged and legacy release SQL still works.
            for col in (
                "repository_id TEXT NOT NULL DEFAULT ''",
                "request_key TEXT NOT NULL DEFAULT ''",
                "generation INTEGER NOT NULL DEFAULT 1",
                "writer_version INTEGER NOT NULL DEFAULT 1",
                'demand_json TEXT NOT NULL DEFAULT \'{"cpu":1,"disk_bytes":0,"io_units":0,"memory_bytes":0,"processes":1,"provider":null,"provider_units":0}\'',
                "group_id TEXT",
                "group_width INTEGER NOT NULL DEFAULT 1",
                "group_age_ns INTEGER NOT NULL DEFAULT 0",
                "next_recheck_ns INTEGER NOT NULL DEFAULT 0",
                "limiting_resource TEXT",
                "observation_age_ns INTEGER",
                "last_progress_ns INTEGER NOT NULL DEFAULT 0",
                "launch_intent_id TEXT",
                "child_host_id TEXT",
                "child_boot_id TEXT",
                "child_pid INTEGER",
                "child_start_token TEXT",
            ):
                c.execute("ALTER TABLE managed_admissions ADD COLUMN " + col)
            c.execute(
                "CREATE INDEX IF NOT EXISTS admission_group ON managed_admissions(status,group_id,sequence)"
            )
            self._fences(c)
            c.commit()
        except BaseException:
            if c.in_transaction:
                c.rollback()
            raise
        finally:
            c.close()

    @contextmanager
    def _connection(self):
        c = None
        try:
            ri, di = self.root.lstat(), self.path.lstat()
            if (
                (ri.st_dev, ri.st_ino) != self._root_identity
                or not stat.S_ISDIR(ri.st_mode)
                or stat.S_IMODE(ri.st_mode) != 0o700
                or (di.st_dev, di.st_ino) != self._database_identity
                or not stat.S_ISREG(di.st_mode)
                or di.st_uid != os.getuid()
                or stat.S_IMODE(di.st_mode) != 0o600
            ):
                raise ManagedAdmissionRefused("MANAGED_ADMISSION_STORE_UNSAFE")
            c = sqlite3.connect(
                self.path.as_uri() + "?mode=rw",
                uri=True,
                timeout=2,
                isolation_level=None,
            )
            c.row_factory = sqlite3.Row
            c.create_function("ffs_admission_writer", 0, lambda: self._nonce)
            c.execute("PRAGMA busy_timeout=2000")
            c.execute("PRAGMA synchronous=FULL")
            if c.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise ManagedAdmissionRefused("MANAGED_ADMISSION_STORE_UNSAFE")
            yield c
        except ManagedAdmissionRefused:
            raise
        except (OSError, sqlite3.Error) as e:
            raise ManagedAdmissionRefused("MANAGED_ADMISSION_STORE_UNAVAILABLE") from e
        finally:
            if c is not None:
                c.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.commit()
            except BaseException:
                c.rollback()
                raise

    def _owner(self, expected=None):
        try:
            o = ProcessIdentity.current()
        except (OSError, ValueError) as e:
            raise ManagedAdmissionRefused("MANAGED_ADMISSION_IDENTITY_UNKNOWN") from e
        if (expected is not None and o != expected) or probe_identity(o) != LIVE:
            raise ManagedAdmissionRefused("MANAGED_ADMISSION_IDENTITY_UNKNOWN")
        return o

    def enqueue(
        self,
        *,
        state_root: Path,
        run_id: str,
        repository_id="",
        request_key="",
        generation=1,
        demand=None,
        group_id=None,
        group_width=1,
        launch_intent_id=None,
    ):
        if (
            not isinstance(run_id, str)
            or not run_id
            or not Path(state_root).is_absolute()
            or not isinstance(repository_id, str)
            or not isinstance(request_key, str)
            or type(generation) is not int
            or generation <= 0
            or type(group_width) is not int
            or group_width <= 0
        ):
            raise ManagedAdmissionRefused("INVALID_REQUEST")
        owner = self._owner()
        demand = demand or ResourceDemand()
        ticket = secrets.token_hex(32)
        now = time.monotonic_ns()
        resolved_state_root = str(Path(state_root).resolve())
        with self._transaction() as c:
            if repository_id and request_key:
                retained = c.execute(
                    "SELECT * FROM managed_admissions WHERE repository_id=? AND run_id=? "
                    "AND request_key=? AND generation=?",
                    (repository_id, run_id, request_key, generation),
                ).fetchall()
                if retained:
                    row = retained[0]
                    if (len(retained) != 1 or _identity(row) != owner
                            or row["state_root"] != resolved_state_root
                            or row["demand_json"] != _encode(demand)
                            or row["group_id"] != group_id or row["group_width"] != group_width
                            or (launch_intent_id is not None and row["launch_intent_id"] != launch_intent_id)
                            or row["status"] not in {"waiting", "active"}):
                        raise ManagedAdmissionRefused("LEASE_RECONCILIATION_REQUIRED")
                    return AdmissionTicket(row["sequence"], row["ticket"], owner)
            seq = c.execute(
                "INSERT INTO managed_admissions(ticket,state_root,run_id,host_id,boot_id,pid,start_token,status,repository_id,request_key,generation,writer_version,demand_json,group_id,group_width,group_age_ns,last_progress_ns,launch_intent_id) VALUES(?,?,?,?,?,?,?,'waiting',?,?,?,?,?,?,?,?,?,?)",
                (
                    ticket,
                    resolved_state_root,
                    run_id,
                    owner.host_id,
                    owner.boot_id,
                    owner.pid,
                    owner.start_token,
                    repository_id,
                    request_key,
                    generation,
                    2,
                    _encode(demand),
                    group_id,
                    group_width,
                    now,
                    now,
                    launch_intent_id,
                ),
            ).lastrowid
        return AdmissionTicket(seq, ticket, owner)

    def snapshot(self):
        with self._connection() as c:
            return [
                dict(x)
                for x in c.execute("SELECT * FROM managed_admissions ORDER BY sequence")
            ]

    def status(self, ticket):
        with self._connection() as c:
            row = c.execute(
                "SELECT * FROM managed_admissions WHERE sequence=? AND ticket=?",
                (ticket.sequence, ticket.ticket),
            ).fetchone()
            if row is None:
                raise ManagedAdmissionRefused("MANAGED_ADMISSION_TICKET_INVALID")
            out = dict(row)
            out["queue_age_ns"] = max(0, time.monotonic_ns() - out["group_age_ns"])
            out["active_count"] = c.execute(
                "SELECT COUNT(*) FROM managed_admissions WHERE status='active'"
            ).fetchone()[0]
            return out

    def _dead(self, identity):
        if identity is None:
            return False
        try:
            return (
                probe_identity(identity) == DEAD
                and (
                    DEAD
                    if self._liveness_probe is None
                    else self._liveness_probe(identity)
                )
                == DEAD
            )
        except Exception:
            return False

    def _reclaim_dead(self):
        with self._connection() as c:
            rows = c.execute(
                "SELECT * FROM managed_admissions WHERE status IN ('waiting','active')"
            ).fetchall()
        # Missing child identity is uncertainty, not evidence of no child.
        # This deliberately retains lease-before-intent and legacy demand.
        doomed = []
        for row in rows:
            supervisor, child = _identity(row), _identity(row, "child_")
            if (supervisor is not None and child is None and row["writer_version"] == 2
                    and row["repository_id"] and row["request_key"]
                    and row["launch_intent_id"] is None and self._dead(supervisor)):
                from .shared_resources import ControlStoreLeaseEvidenceReader
                lease = LeaseIdentity(row["repository_id"], row["run_id"], row["request_key"],
                                      None, row["generation"], supervisor)
                try:
                    self.reclaim_pre_spawn(
                        AdmissionTicket(row["sequence"], row["ticket"], supervisor), lease,
                        ControlStoreLeaseEvidenceReader(Path(row["state_root"]) / "control.sqlite3"),
                    )
                except ManagedAdmissionRefused:
                    # Busy/unknown authority cannot release the reservation.
                    pass
            if (
                supervisor is not None
                and child is not None
                and self._dead(supervisor)
                and self._dead(child)
            ):
                doomed.append((row["sequence"], row["ticket"], supervisor, child))
        if doomed:
            with self._transaction() as c:
                for seq, ticket, supervisor, child in doomed:
                    c.execute(
                        "UPDATE managed_admissions SET status='reclaimed',limiting_resource='lease-dead' WHERE sequence=? AND ticket=? AND host_id=? AND boot_id=? AND pid=? AND start_token=? AND child_host_id=? AND child_boot_id=? AND child_pid=? AND child_start_token=? AND status IN ('waiting','active')",
                        (
                            seq,
                            ticket,
                            supervisor.host_id,
                            supervisor.boot_id,
                            supervisor.pid,
                            supervisor.start_token,
                            child.host_id,
                            child.boot_id,
                            child.pid,
                            child.start_token,
                        ),
                    )

    def _candidates(self, c):
        first = {}
        for row in c.execute(
            "SELECT * FROM managed_admissions WHERE status='waiting' ORDER BY sequence"
        ):
            first.setdefault((row["repository_id"], row["run_id"]), row)
        keys = sorted(first)
        cursor = c.execute(
            "SELECT scheduler_cursor FROM admission_policy WHERE singleton=1"
        ).fetchone()[0]
        try:
            cursor = tuple(json.loads(cursor)) if cursor else None
        except (TypeError, ValueError):
            cursor = None
        if cursor in keys:
            i = (keys.index(cursor) + 1) % len(keys)
            keys = keys[i:] + keys[:i]
        return [first[x] for x in keys]

    def try_admit(self, ticket, *, observation=None):
        self._owner(ticket.owner)
        retained = self.status(ticket)
        if retained['status'] == 'active' and _identity(retained) == ticket.owner:
            return True
        self._reclaim_dead()
        # An abandoned waiting group cannot reserve future capacity merely by
        # aging. Probe outside SQLite; rebind this snapshot to exact identities
        # below. Unknown owners retain their leases but receive no new grant.
        with self._connection() as connection:
            waiting = connection.execute("SELECT * FROM managed_admissions WHERE status='waiting'").fetchall()
        live_waiters = {_identity(row) for row in waiting if _identity(row) is not None
                        and probe_identity(_identity(row)) == LIVE}
        try:
            observation = observation or self._observe()
        except Exception as e:
            raise ManagedAdmissionRefused("RESOURCE_OBSERVATION_UNAVAILABLE") from e
        with self._transaction() as c:
            own = c.execute(
                "SELECT * FROM managed_admissions WHERE sequence=? AND ticket=?",
                (ticket.sequence, ticket.ticket),
            ).fetchone()
            if (
                own is None
                or _identity(own) != ticket.owner
                or own["status"] not in ("waiting", "active")
            ):
                raise ManagedAdmissionRefused("MANAGED_ADMISSION_TICKET_INVALID")
            if own["writer_version"] != 2:
                raise ManagedAdmissionRefused(
                    "MANAGED_ADMISSION_LEGACY_RESUME_REQUIRED"
                )
            if own["status"] == "active":
                return True
            choices = [row for row in self._candidates(c) if _identity(row) in live_waiters]
            from .provider_feedback import effective_observation
            providers = [demand_from_record(row["demand_json"]).provider for row in choices]
            observation = effective_observation(
                c, observation, providers=providers, boot_id=ticket.owner.boot_id,
                now_ns=time.monotonic_ns(),
            )
            # Legacy demand remains opaque even after legacy release.  It
            # cannot be silently converted to zero CPU/memory/provider cost.
            opaque = c.execute(
                "SELECT 1 FROM managed_admissions WHERE writer_version=1 AND status!='reclaimed' LIMIT 1"
            ).fetchone()
            requested_demand = demand_from_record(own["demand_json"])
            if opaque is not None and any(
                (
                    requested_demand.cpu,
                    requested_demand.memory_bytes,
                    requested_demand.disk_bytes,
                    requested_demand.io_units,
                    requested_demand.processes,
                )
            ):
                c.execute(
                    "UPDATE managed_admissions SET limiting_resource='legacy-opaque',next_recheck_ns=? WHERE sequence=?",
                    (
                        time.monotonic_ns() + self._scheduler.cooldown_ns,
                        own["sequence"],
                    ),
                )
                return False
            active = [
                dict(x)
                for x in c.execute(
                    "SELECT * FROM managed_admissions WHERE status='active'"
                )
            ]
            group = None
            decisions = None
            candidate_groups = {
                candidate["sequence"]: ([candidate] if not candidate["group_id"] else c.execute(
                    "SELECT * FROM managed_admissions WHERE status='waiting' AND group_id=? ORDER BY sequence",
                    (candidate["group_id"],),
                ).fetchall()) for candidate in choices
            }
            priority, protected_resource = None, None
            now = time.monotonic_ns()
            for candidate in sorted(choices, key=lambda row: (row["group_age_ns"], row["sequence"])):
                peers = candidate_groups[candidate["sequence"]]
                if (not candidate["group_id"] or len(peers) != candidate["group_width"]
                        or now - candidate["group_age_ns"] < self._scheduler.cooldown_ns):
                    continue
                demands = [demand_from_record(row["demand_json"]) for row in peers]
                empty_decisions = self._scheduler.decide_group(demands, [], observation, ceilings=self._ceilings)
                if not all(decision.admitted for decision in empty_decisions):
                    continue  # Structurally infeasible/throttled groups cannot capture other pools.
                with_active = self._scheduler.decide_group(demands, active, observation, ceilings=self._ceilings)
                blocked = next((decision for decision in with_active if not decision.admitted), None)
                priority = candidate
                protected_resource = None if blocked is None else blocked.limiting_resource
                break
            if priority is not None:
                choices = [priority] + [row for row in choices if row["sequence"] != priority["sequence"]]
            for candidate in choices:
                candidate_group = candidate_groups[candidate["sequence"]]
                if len(candidate_group) < candidate["group_width"]:
                    continue
                if priority is not None and candidate["sequence"] != priority["sequence"]:
                    demands = [demand_from_record(row["demand_json"]) for row in candidate_group]
                    competes = protected_resource is None or any(
                        (demand.provider == protected_resource.removeprefix("provider:") and demand.provider_units > 0)
                        if protected_resource.startswith("provider:") else getattr(demand, protected_resource, 0) > 0
                        for demand in demands
                    )
                    if competes:
                        c.execute("UPDATE managed_admissions SET limiting_resource=?,next_recheck_ns=? WHERE sequence=?",
                                  ("aged-group:" + str(priority["group_id"]), now + self._scheduler.cooldown_ns,
                                   candidate["sequence"]))
                        continue
                candidate_decisions = self._scheduler.decide_group(
                    [demand_from_record(x["demand_json"]) for x in candidate_group],
                    active,
                    observation,
                    ceilings=self._ceilings,
                )
                blocked = next(
                    (
                        decision
                        for decision in candidate_decisions
                        if not decision.admitted
                    ),
                    None,
                )
                if blocked is not None:
                    c.execute(
                        "UPDATE managed_admissions SET limiting_resource=?,observation_age_ns=?,next_recheck_ns=? WHERE sequence=?",
                        (
                            blocked.limiting_resource or blocked.code,
                            blocked.observation_age_ns,
                            blocked.next_recheck_ns,
                            candidate["sequence"],
                        ),
                    )
                    # A throttled/unknown provider must not head-of-line block
                    # an independent feasible resource class.
                    continue
                if candidate["sequence"] != ticket.sequence:
                    return False
                group, decisions = candidate_group, candidate_decisions
                break
            if group is None:
                return False
            now = time.monotonic_ns()
            for row in group:
                c.execute(
                    "UPDATE managed_admissions SET status='active',limiting_resource=NULL,observation_age_ns=?,next_recheck_ns=?,last_progress_ns=? WHERE sequence=? AND status='waiting'",
                    (
                        decisions[0].observation_age_ns,
                        decisions[0].next_recheck_ns,
                        now,
                        row["sequence"],
                    ),
                )
            c.execute(
                "UPDATE admission_policy SET scheduler_cursor=?,last_progress_ns=? WHERE singleton=1",
                (json.dumps([own["repository_id"], own["run_id"]]), now),
            )
            return True

    def bind_consumer(self, ticket, lease):
        self._owner(ticket.owner)
        if (not isinstance(lease, LeaseIdentity) or lease.supervisor != ticket.owner
                or lease.launch_intent_id is None):
            raise ManagedAdmissionRefused("LEASE_IDENTITY_MISMATCH")
        child = lease.consumer
        with self._transaction() as c:
            row = c.execute("SELECT * FROM managed_admissions WHERE sequence=? AND ticket=?",
                            (ticket.sequence, ticket.ticket)).fetchone()
            if (row is None or _identity(row) != ticket.owner or row["status"] != "active"
                    or row["run_id"] != lease.run_id
                    or row["repository_id"] not in {"", lease.repository_id}
                    or row["request_key"] not in {"", lease.request_key}
                    or row["generation"] != lease.generation
                    or row["launch_intent_id"] not in {None, lease.launch_intent_id}
                    or (_identity(row, "child_") is not None and _identity(row, "child_") != child)):
                raise ManagedAdmissionRefused("LEASE_IDENTITY_MISMATCH")
            n = c.execute(
                "UPDATE managed_admissions SET repository_id=?,request_key=?,generation=?,launch_intent_id=?,child_host_id=?,child_boot_id=?,child_pid=?,child_start_token=?,last_progress_ns=? WHERE sequence=? AND ticket=? AND status='active'",
                (
                    lease.repository_id,
                    lease.request_key,
                    lease.generation,
                    lease.launch_intent_id,
                    None if child is None else child.host_id,
                    None if child is None else child.boot_id,
                    None if child is None else child.pid,
                    None if child is None else child.start_token,
                    time.monotonic_ns(),
                    ticket.sequence,
                    ticket.ticket,
                ),
            ).rowcount
            if n != 1:
                raise ManagedAdmissionRefused("MANAGED_ADMISSION_TICKET_INVALID")

    def record_provider_feedback(self, ticket, *, outcome, retry_after_ns=0):
        owner = self._owner(ticket.owner)
        from .provider_feedback import record_result
        with self._transaction() as c:
            try:
                record_result(c, ticket=ticket.ticket, outcome=outcome, retry_after_ns=retry_after_ns,
                              boot_id=owner.boot_id, now_ns=time.monotonic_ns())
            except ValueError as error:
                raise ManagedAdmissionRefused("PROVIDER_FEEDBACK_INVALID") from error

    def resume_legacy(self, ticket, recorded_writer):
        self._owner(ticket.owner)
        with self._transaction() as c:
            row = c.execute(
                "SELECT * FROM managed_admissions WHERE sequence=? AND ticket=?",
                (ticket.sequence, ticket.ticket),
            ).fetchone()
            if row is None or row["writer_version"] != 1:
                raise ManagedAdmissionRefused(
                    "MANAGED_ADMISSION_LEGACY_RESUME_REQUIRED"
                )
            if (
                not isinstance(recorded_writer, ProcessIdentity)
                or _identity(row) != recorded_writer
                or recorded_writer != ticket.owner
            ):
                raise ManagedAdmissionRefused("LEGACY_WRITER_PROOF_REQUIRED")
            c.execute(
                "UPDATE managed_admissions SET writer_version=2 WHERE sequence=?",
                (ticket.sequence,),
            )

    def reclaim_pre_spawn(self, ticket, lease, evidence_reader):
        if not isinstance(lease, LeaseIdentity) or not hasattr(
            evidence_reader, "read_fenced_lease"
        ):
            raise ManagedAdmissionRefused("PRESPAWN_RECLAIM_PROOF_REQUIRED")
        # ControlStore evidence is read before the registry transaction.  It
        # must be an exact immutable proof, never caller supplied booleans.
        evidence = evidence_reader.read_fenced_lease(lease)
        if not isinstance(
            evidence, FencedLeaseRecord
        ) or not evidence.proves_never_authorized(lease):
            raise ManagedAdmissionRefused("PRESPAWN_RECLAIM_PROOF_REQUIRED")
        with self._transaction() as c:
            row = c.execute(
                "SELECT * FROM managed_admissions WHERE sequence=? AND ticket=?",
                (ticket.sequence, ticket.ticket),
            ).fetchone()
            if row is None or row["status"] not in ("waiting", "active"):
                return False
            if (
                row["repository_id"],
                row["run_id"],
                row["request_key"],
                row["launch_intent_id"],
                row["generation"],
            ) != (
                lease.repository_id,
                lease.run_id,
                lease.request_key,
                lease.launch_intent_id,
                lease.generation,
            ) or _identity(row, "child_") is not None:
                raise ManagedAdmissionRefused("LEASE_IDENTITY_MISMATCH")
            return (
                c.execute(
                    "UPDATE managed_admissions SET status='reclaimed',limiting_resource='pre-spawn-proved' WHERE sequence=? AND ticket=? AND status IN ('waiting','active')",
                    (ticket.sequence, ticket.ticket),
                ).rowcount
                == 1
            )

    def release(self, ticket):
        self._owner(ticket.owner)
        # Probe outside SQLite.  A changed child identity is compared again by
        # the subsequent CAS rather than being freed based on a stale snapshot.
        with self._connection() as c:
            snapshot = c.execute(
                "SELECT * FROM managed_admissions WHERE sequence=? AND ticket=?",
                (ticket.sequence, ticket.ticket),
            ).fetchone()
        if snapshot is None or _identity(snapshot) != ticket.owner:
            raise ManagedAdmissionRefused("MANAGED_ADMISSION_TICKET_INVALID")
        child = _identity(snapshot, "child_")
        if snapshot["status"] == "active" and (child is None or not self._dead(child)):
            raise ManagedAdmissionRefused("MANAGED_ADMISSION_DESCENDANT_RETAINED")
        with self._transaction() as c:
            row = c.execute(
                "SELECT * FROM managed_admissions WHERE sequence=? AND ticket=?",
                (ticket.sequence, ticket.ticket),
            ).fetchone()
            if row is None or _identity(row) != ticket.owner:
                raise ManagedAdmissionRefused("MANAGED_ADMISSION_TICKET_INVALID")
            sql = "UPDATE managed_admissions SET status='released' WHERE sequence=? AND ticket=? AND host_id=? AND boot_id=? AND pid=? AND start_token=? AND status IN ('waiting','active','released')"
            values = (
                ticket.sequence,
                ticket.ticket,
                ticket.owner.host_id,
                ticket.owner.boot_id,
                ticket.owner.pid,
                ticket.owner.start_token,
            )
            if child is not None:
                sql = sql.replace(
                    " AND status",
                    " AND child_host_id=? AND child_boot_id=? AND child_pid=? AND child_start_token=? AND status",
                )
                values = values + (
                    child.host_id,
                    child.boot_id,
                    child.pid,
                    child.start_token,
                )
            if c.execute(sql, values).rowcount != 1:
                raise ManagedAdmissionRefused("MANAGED_ADMISSION_TICKET_INVALID")

    def acquire(
        self, *, state_root, run_id, timeout=None, poll_interval=0.2, **request
    ):
        if (
            not isinstance(poll_interval, (int, float))
            or isinstance(poll_interval, bool)
            or not math.isfinite(poll_interval)
            or poll_interval <= 0
            or (
                timeout is not None
                and (
                    not isinstance(timeout, (int, float))
                    or isinstance(timeout, bool)
                    or not math.isfinite(timeout)
                    or timeout < 0
                )
            )
        ):
            raise ManagedAdmissionRefused("INVALID_REQUEST")
        deadline = None if timeout is None else time.monotonic() + timeout
        ticket = self.enqueue(state_root=state_root, run_id=run_id, **request)
        ok = False
        try:
            while not self.try_admit(ticket):
                if deadline is not None and time.monotonic() >= deadline:
                    raise ManagedAdmissionRefused("MANAGED_ADMISSION_TIMEOUT")
                time.sleep(
                    poll_interval
                    if deadline is None
                    else min(poll_interval, max(0, deadline - time.monotonic()))
                )
            ok = True
            return ticket
        finally:
            if not ok:
                self.release(ticket)
