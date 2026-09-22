"""Durable, single-writer import and handoff for legacy FFS run state.

This module is deliberately separate from installation.  A migration authority
is a *new*, explicitly registered ``ControlStore``.  Legacy databases and
context files are only read through the compatibility readers in ``state``;
this module never opens them for writing and never invents an owner identity.

The existing migration tables are used as an append-only journal.  Their
registration capability and source manifest make an accidental invocation on
an ordinary control store fail closed.  The resulting epoch is the one writer
selection for a migrated run: ``legacy``, ``new``, or ``none`` (paused pending
an operator-supported rollback).  There is no dual-writer state.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Literal, Sequence

from run_context import ContextRefused, validate_run_id
from process_identity import DEAD, LIVE, UNKNOWN, ProcessIdentity, probe_identity

from .state import ControlStore, ControlStoreRefused, LegacyProjection


_SOURCE_SCHEMA = "ffs.migration-source-identity/v1"
_RECORD_SCHEMA = "ffs.migration-record/v1"
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_KINDS = frozenset({"run-store", "context"})
_OWNER_STATES = frozenset({"released", "dead", "live", "unknown"})
_INVALID_SOURCE_CODE = "MIGRATION_SOURCE_INVALID"


class MigrationRefused(RuntimeError):
    """A bounded migration refusal with an inspectable stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class LegacySource:
    """One immutable legacy input admitted by the registered source manifest."""

    path: Path
    kind: Literal["run-store", "context"]
    repository_id: str | None = None

    @property
    def canonical_path(self) -> Path:
        # The registration manifest records the resolved target.  Rejecting a
        # symlink before resolving prevents a late path swap from silently
        # changing the reader's source identity.
        return self.path.expanduser().resolve(strict=True)

    @property
    def source_id(self) -> str:
        material = f"{self.kind}\0{self.canonical_path}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def manifest_entry(self) -> dict:
        return {
            "kind": self.kind,
            "path": str(self.canonical_path),
            "source_id": self.source_id,
            # Repository identity is optional for the compatibility API, but
            # required by the public enroll command.  Retaining null rather
            # than guessing makes older fixture sources inspectable without
            # treating them as production handoff candidates.
            "repository_id": self.repository_id,
        }


@dataclass(frozen=True)
class LegacyOwner:
    """Observed legacy-writer liveness supplied by a compatibility probe.

    ``identity`` is evidence only.  It is retained verbatim after JSON
    validation and is never used to synthesize a PID or alter the legacy
    source.  LIVE and UNKNOWN are intentionally non-handoff outcomes.
    """

    state: Literal["released", "dead", "live", "unknown"]
    identity: dict | None
    proof: dict


@dataclass(frozen=True)
class MigrationReport:
    source_id: str
    source_sha256: str
    imported: tuple[str, ...]
    quarantined: tuple[tuple[str, str], ...]
    replayed: tuple[str, ...]


@dataclass(frozen=True)
class WriterEpoch:
    run_id: str
    epoch: int
    writer: Literal["legacy", "new", "none"]
    checkpoint: str
    proof: dict | None


def source_manifest(sources: Sequence[LegacySource]) -> str:
    """Build the exact source-identity JSON required at authority enrollment."""
    if any(
        not isinstance(source, LegacySource) or source.kind not in _KINDS
        for source in sources
    ):
        raise MigrationRefused("INVALID_MIGRATION_SOURCES")
    entries = [source.manifest_entry() for source in sources]
    if not entries or len({entry["source_id"] for entry in entries}) != len(entries):
        raise MigrationRefused("INVALID_MIGRATION_SOURCES")
    payload = {
        "schema": _SOURCE_SCHEMA,
        "sources": sorted(entries, key=lambda item: item["source_id"]),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def enroll_authority(
    path: Path,
    *,
    registration_id: str,
    capability_sha256: str,
    sources: Sequence[LegacySource],
) -> MigrationCoordinator:
    """Create the only migration authority at a fresh execution-store path.

    This deliberately creates the target ``ControlStore`` itself; migration
    metadata is never written to a sibling JSON/SQLite sidecar.  Public
    callers must bind every source to a repository identity before there is
    any authority state to mutate.
    """
    sources = tuple(sources)
    if (
        not sources
        or any(
            not isinstance(source.repository_id, str)
            or not source.repository_id
            or "\x00" in source.repository_id
            for source in sources
        )
    ):
        raise MigrationRefused("MIGRATION_REPOSITORY_ID_REQUIRED")
    store = ControlStore(path)
    store.initialize_migration_fixture(
        registration_id=registration_id,
        capability_sha256=capability_sha256,
        source_identity_json=source_manifest(sources),
    )
    return MigrationCoordinator(
        store, registration_id=registration_id, capability_sha256=capability_sha256
    )


class MigrationCoordinator:
    """Compatibility reader, idempotent importer, and writer-epoch selector."""

    def __init__(
        self, store: ControlStore, *, registration_id: str, capability_sha256: str
    ) -> None:
        if (
            not isinstance(store, ControlStore)
            or not isinstance(registration_id, str)
            or not registration_id
            or not _valid_digest(capability_sha256)
        ):
            raise MigrationRefused("INVALID_MIGRATION_REGISTRATION")
        self.store = store
        self.registration_id = registration_id
        self.capability_sha256 = capability_sha256

    def import_sources(
        self, sources: Sequence[LegacySource]
    ) -> tuple[MigrationReport, ...]:
        """Validate, dual-read, snapshot, and import each legacy source once.

        Source reads happen before the short authority transaction.  The
        reader's hash must equal two direct regular-file snapshots, so a source
        changed during dual-read is refused rather than journaled ambiguously.
        """
        sources = tuple(sources)
        expected = self._registered_sources()
        observed = {source.source_id: source.manifest_entry() for source in sources}
        if observed != expected or len(observed) != len(sources):
            raise MigrationRefused("MIGRATION_SOURCE_MANIFEST_MISMATCH")
        loaded = [self._load_source(source) for source in sources]
        reports: list[MigrationReport] = []
        with self.store.migration_interlock(
            self.registration_id, self.capability_sha256
        ):
            for source, source_sha, source_bytes, source_identity, projections, source_error in loaded:
                reports.append(
                    self._journal_source(
                        source,
                        source_sha,
                        source_bytes,
                        source_identity,
                        projections,
                        source_error,
                    )
                )
            with self.store.transaction() as tx:
                tx.execute(
                    "UPDATE migration_fixture SET readers_ready=1 WHERE singleton=1"
                )
        return tuple(reports)

    def handoff(self, run_id: str, owner: LegacyOwner) -> WriterEpoch:
        """Activate the new writer only after released/dead ownership proof."""
        self._validate_owner(owner)
        with self.store.migration_interlock(
            self.registration_id, self.capability_sha256
        ):
            with self.store.transaction() as tx:
                current = self._epoch_tx(tx, run_id)
                if current is None:
                    raise MigrationRefused("MIGRATION_RUN_NOT_IMPORTED")
                if current.writer == "new":
                    return current
                if current.writer == "none":
                    raise MigrationRefused("MIGRATION_WRITER_PAUSED")
                if owner.state in {"live", "unknown"}:
                    # Preserve the legacy writer selection and identity.  The
                    # event records why a dependent new writer remains blocked.
                    self._event_tx(
                        tx,
                        "migration_handoff_blocked",
                        {
                            "run_id": run_id,
                            "legacy_owner": _owner_material(owner),
                        },
                    )
                    return current
                proof = _owner_material(owner)
                return self._set_epoch_tx(
                    tx,
                    run_id,
                    current.epoch + 1,
                    "new",
                    "new_writer_activated",
                    proof,
                )

    def handoff_trusted(self, run_id: str) -> WriterEpoch:
        """Public handoff using only the installed legacy compatibility adapter.

        There is intentionally no JSON owner argument here.  A caller cannot
        claim a dead PID or released lease: the adapter observes the legacy
        database's durable start fence and native process incarnation while
        the migration authority interlock is held.
        """
        with self.store.migration_interlock(
            self.registration_id, self.capability_sha256
        ):
            with self.store.transaction() as tx:
                current = self._epoch_tx(tx, run_id)
                if current is None:
                    raise MigrationRefused("MIGRATION_RUN_NOT_IMPORTED")
                if current.writer == "new":
                    return current
                if current.writer == "none":
                    raise MigrationRefused("MIGRATION_WRITER_PAUSED")
                if not self._managed_context_ready_tx(tx, run_id):
                    raise MigrationRefused("MIGRATION_MANAGED_CONTEXT_INCOMPLETE")
            owner = self._trusted_legacy_owner(run_id)
            self._validate_owner(owner)
            with self.store.transaction() as tx:
                current = self._epoch_tx(tx, run_id)
                if current is None:
                    raise MigrationRefused("MIGRATION_RUN_NOT_IMPORTED")
                if current.writer == "new":
                    return current
                if current.writer == "none":
                    raise MigrationRefused("MIGRATION_WRITER_PAUSED")
                if not self._managed_context_ready_tx(tx, run_id):
                    raise MigrationRefused("MIGRATION_MANAGED_CONTEXT_INCOMPLETE")
                if owner.state in {"live", "unknown"}:
                    self._event_tx(
                        tx,
                        "migration_handoff_blocked",
                        {"run_id": run_id, "legacy_owner": _owner_material(owner)},
                    )
                    return current
                return self._set_epoch_tx(
                    tx,
                    run_id,
                    current.epoch + 1,
                    "new",
                    "new_writer_activated",
                    _owner_material(owner),
                )

    def _trusted_legacy_owner(self, run_id: str) -> LegacyOwner:
        """Read one fenced legacy lease, including its SQLite sidecar identity."""
        if not isinstance(run_id, str) or not run_id:
            raise MigrationRefused("INVALID_MIGRATION_RUN_ID")
        # No legacy probe occurs in an authority writer transaction.  The
        # outer interlock prevents a competing local migration transition;
        # the legacy protocol fence is separately required below.
        with self.store.transaction() as tx:
            rows = tx.execute(
                "SELECT j.source_id,j.source_sha256,j.record_json,s.source_identity_json "
                "FROM migration_journal j JOIN migration_snapshots s "
                "ON s.source_id=j.source_id AND s.source_sha256=j.source_sha256 "
                "WHERE j.run_id=? AND j.disposition='imported'",
                (run_id,),
            ).fetchall()
            manifest_row = tx.execute(
                "SELECT source_identity_json FROM migration_fixture WHERE singleton=1"
            ).fetchone()
        if len(rows) != 1:
            raise MigrationRefused("MIGRATION_RUN_AMBIGUOUS")
        row = rows[0]
        try:
            record = json.loads(row["record_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise MigrationRefused("MIGRATION_JOURNAL_CORRUPT") from error
        if (
            not isinstance(record, dict)
            or record.get("source_run_id") != run_id
            or record.get("canonical_run_id") != run_id
        ):
            raise MigrationRefused("MIGRATION_JOURNAL_CORRUPT")
        if manifest_row is None:
            raise MigrationRefused("MIGRATION_FIXTURE_REQUIRED")
        source_entry = _parse_registered_sources(
            manifest_row["source_identity_json"]
        ).get(row["source_id"])
        if source_entry is None or source_entry["kind"] != "run-store":
            raise MigrationRefused("LEGACY_OWNER_ADAPTER_UNAVAILABLE")
        source = LegacySource(
            Path(source_entry["path"]), source_entry["kind"], source_entry["repository_id"]
        )
        try:
            snapshot_identity = json.loads(row["source_identity_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise MigrationRefused("MIGRATION_SNAPSHOT_CORRUPT") from error
        if not _valid_source_snapshot_identity(snapshot_identity, kind=source.kind):
            raise MigrationRefused("MIGRATION_SNAPSHOT_CORRUPT")
        return _read_fenced_legacy_owner(
            source, run_id=run_id, expected_source_sha256=row["source_sha256"],
            expected_snapshot_identity=snapshot_identity,
        )

    def writer_for(self, run_id: str) -> WriterEpoch:
        """Return the sole selected writer or fail before any caller writes."""
        with self.store.migration_interlock(
            self.registration_id, self.capability_sha256
        ):
            with self.store.transaction() as tx:
                epoch = self._epoch_tx(tx, run_id)
        if epoch is None:
            raise MigrationRefused("MIGRATION_RUN_NOT_IMPORTED")
        if epoch.writer == "none":
            raise MigrationRefused("MIGRATION_WRITER_PAUSED")
        return epoch

    def assert_new_writer(self, run_id: str) -> WriterEpoch:
        epoch = self.writer_for(run_id)
        if epoch.writer != "new":
            raise MigrationRefused("WRITER_HANDOFF_REQUIRED")
        return epoch

    def rollback(
        self, run_id: str, *, legacy_writer_compatible: bool, proof: dict
    ) -> WriterEpoch:
        """Freeze a new epoch, preserve its evidence, then select one rollback writer.

        A supported legacy writer receives a newly recorded legacy epoch.  If
        reverse compatibility is unavailable, the run is paused (``none``),
        which remains a single writer selection and cannot be mistaken for a
        completed rollback.
        """
        if not isinstance(legacy_writer_compatible, bool) or not _json_object(proof):
            raise MigrationRefused("INVALID_ROLLBACK_PROOF")
        with self.store.migration_interlock(
            self.registration_id, self.capability_sha256
        ):
            with self.store.transaction() as tx:
                current = self._epoch_tx(tx, run_id)
                if current is None:
                    raise MigrationRefused("MIGRATION_RUN_NOT_IMPORTED")
                if current.writer != "new":
                    if current.writer == "none":
                        return current
                    raise MigrationRefused("ROLLBACK_NEW_WRITER_REQUIRED")
                # Record the pause/fence decision before selecting a reverse
                # writer. This is in the same authority transaction as the
                # epoch update, so a crash yields either the old new-writer
                # epoch or durable fence evidence plus exactly one successor.
                self._event_tx(
                    tx,
                    "migration_rollback_fenced",
                    {"run_id": run_id, "from_epoch": current.epoch},
                )
                selected = "legacy" if legacy_writer_compatible else "none"
                checkpoint = (
                    "legacy_writer_reinstated"
                    if legacy_writer_compatible
                    else "paused_incompatible"
                )
                material = {"rollback_proof": proof, "from_epoch": current.epoch}
                return self._set_epoch_tx(
                    tx,
                    run_id,
                    current.epoch + 1,
                    selected,
                    checkpoint,
                    material,
                )

    def journal(self, run_id: str | None = None) -> tuple[dict, ...]:
        """Read the retained import/quarantine evidence without changing it."""
        with self.store.migration_interlock(
            self.registration_id, self.capability_sha256
        ):
            with self.store.transaction() as tx:
                query = (
                    "SELECT source_id,source_sha256,record_key,record_sha256,record_json,run_id,"
                    "disposition,import_target,conflict_reason,checkpoint,epoch "
                    "FROM migration_journal"
                )
                values: tuple[object, ...] = ()
                if run_id is not None:
                    query += " WHERE run_id=?"
                    values = (run_id,)
                query += " ORDER BY source_id,source_sha256,record_key"
                rows = tx.execute(query, values).fetchall()
        return tuple({key: row[key] for key in row.keys()} for row in rows)

    def _registered_sources(self) -> dict[str, dict]:
        with self.store.migration_interlock(
            self.registration_id, self.capability_sha256
        ):
            with self.store.transaction() as tx:
                row = tx.execute(
                    "SELECT source_identity_json FROM migration_fixture WHERE singleton=1"
                ).fetchone()
        if row is None:
            raise MigrationRefused("MIGRATION_FIXTURE_REQUIRED")
        return _parse_registered_sources(row["source_identity_json"])

    def _load_source(
        self,
        source: LegacySource,
    ) -> tuple[LegacySource, str, bytes, dict, tuple[LegacyProjection, ...], dict | None]:
        if not isinstance(source, LegacySource) or source.kind not in _KINDS:
            raise MigrationRefused("INVALID_MIGRATION_SOURCE")
        path = source.path.expanduser()
        try:
            before_info, before = _read_regular_source(path)
        except FileNotFoundError as error:
            raise MigrationRefused("MIGRATION_SOURCE_MISSING") from error
        except MigrationRefused:
            raise
        except OSError as error:
            raise MigrationRefused("MIGRATION_SOURCE_READ_FAILED") from error
        if len(before) > _MAX_SOURCE_BYTES:
            raise MigrationRefused("MIGRATION_SOURCE_TOO_LARGE")
        source_sha = hashlib.sha256(before).hexdigest()
        before_identity = _source_snapshot_identity(source, before_info, source_sha)
        source_error = None
        try:
            projections = (
                tuple(ControlStore.read_legacy_run_store(path))
                if source.kind == "run-store"
                else (ControlStore.read_legacy_context(path),)
            )
        except (
            ControlStoreRefused,
            json.JSONDecodeError,
            UnicodeDecodeError,
            AttributeError,
        ) as error:
            if isinstance(error, ControlStoreRefused) and error.code in {
                "LEGACY_SOURCE_CHANGED",
                "STORE_REPLACED",
            }:
                raise MigrationRefused("MIGRATION_SOURCE_CHANGED") from error
            if isinstance(error, AttributeError) and source.kind != "context":
                raise
            # A registered source whose stable bytes cannot be projected is a
            # source-level quarantine.  It must not suppress unrelated valid
            # imports.  Identity/snapshot failures are checked below and still
            # refuse the whole operation.
            source_error = _source_error_evidence(source, source_sha, error)
            projections = (
                LegacyProjection(
                    None, None, source_sha, "quarantined", _INVALID_SOURCE_CODE
                ),
            )
        try:
            after_info, after = _read_regular_source(path)
        except MigrationRefused:
            raise MigrationRefused("MIGRATION_SOURCE_CHANGED") from None
        except OSError as error:
            raise MigrationRefused("MIGRATION_SOURCE_CHANGED") from error
        after_identity = _source_snapshot_identity(
            source, after_info, hashlib.sha256(after).hexdigest()
        )
        if (
            (before_info.st_dev, before_info.st_ino)
            != (after_info.st_dev, after_info.st_ino)
            or before != after
            or hashlib.sha256(after).hexdigest() != source_sha
            or before_identity != after_identity
            or any(projection.source_sha256 != source_sha for projection in projections)
        ):
            raise MigrationRefused("MIGRATION_SOURCE_CHANGED")
        return source, source_sha, before, before_identity, projections, source_error

    def _journal_source(
        self,
        source: LegacySource,
        source_sha: str,
        source_bytes: bytes,
        source_identity: dict,
        projections: tuple[LegacyProjection, ...],
        source_error: dict | None = None,
    ) -> MigrationReport:
        imported: list[str] = []
        quarantined: list[tuple[str, str]] = []
        replayed: list[str] = []
        seen_record_keys: set[str] = set()
        identity_json = json.dumps(
            source_identity, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        with self.store.transaction() as tx:
            tx.execute(
                "INSERT OR IGNORE INTO migration_snapshots(source_id,source_sha256,source_bytes,source_identity_json,record_count) "
                "VALUES(?,?,?,?,?)",
                (source.source_id, source_sha, source_bytes, identity_json, len(projections)),
            )
            snapshot = tx.execute(
                "SELECT source_bytes,source_identity_json,record_count FROM migration_snapshots WHERE source_id=? AND source_sha256=?",
                (source.source_id, source_sha),
            ).fetchone()
            if (
                snapshot is None
                or snapshot["source_bytes"] != source_bytes
                or snapshot["source_identity_json"] != identity_json
                or snapshot["record_count"] != len(projections)
            ):
                raise MigrationRefused("MIGRATION_SNAPSHOT_CONFLICT")
            for index, projection in enumerate(projections):
                base_record_key = projection.source_run_id or f"quarantine-{index:08d}"
                duplicate_source_key = base_record_key in seen_record_keys
                seen_record_keys.add(base_record_key)
                # Preserve both records in an ambiguous legacy source rather
                # than allowing the second to masquerade as an idempotent
                # replay of the first.
                record_key = (
                    f"{base_record_key}#duplicate-{index:08d}"
                    if duplicate_source_key
                    else base_record_key
                )
                record = {
                    "schema": _RECORD_SCHEMA,
                    "source_id": source.source_id,
                    "source_kind": source.kind,
                    "repository_id": source.repository_id,
                    "source_run_id": projection.source_run_id,
                    "canonical_run_id": projection.canonical_run_id,
                    "mapping_evidence": {
                        "schema": "ffs.migration-run-mapping/v1",
                        "repository_id": source.repository_id,
                        "source_id": source.source_id,
                        "source_run_id": projection.source_run_id,
                        "canonical_run_id": projection.canonical_run_id,
                        "source_sha256": source_sha,
                    },
                }
                if source_error is not None:
                    record["source_error"] = source_error
                record_json = json.dumps(
                    record, sort_keys=True, separators=(",", ":"), allow_nan=False
                )
                record_sha = hashlib.sha256(record_json.encode("utf-8")).hexdigest()
                present = tx.execute(
                    "SELECT record_sha256,disposition,conflict_reason FROM migration_journal "
                    "WHERE source_id=? AND source_sha256=? AND record_key=?",
                    (source.source_id, source_sha, record_key),
                ).fetchone()
                if present is not None:
                    if present["record_sha256"] != record_sha:
                        raise MigrationRefused("MIGRATION_JOURNAL_CONFLICT")
                    replayed.append(record_key)
                    continue
                reason = "DUPLICATE_SOURCE_RUN_ID" if duplicate_source_key else projection.code
                run_id = projection.canonical_run_id
                if reason is None and run_id is not None:
                    try:
                        validate_run_id(run_id)
                    except ContextRefused:
                        reason = "INVALID_RUN_ID"
                if reason is None and run_id is not None:
                    conflict = tx.execute(
                        "SELECT 1 FROM migration_journal WHERE run_id=? AND disposition='imported' LIMIT 1",
                        (run_id,),
                    ).fetchone()
                    if conflict is not None:
                        reason = "CANONICAL_RUN_CONFLICT"
                if reason is None and run_id is not None:
                    materialized, material_reason, accounting = (
                        self._materialize_run_tx(
                            tx, source, source_bytes, projection, run_id
                        )
                    )
                    disposition, target, checkpoint, epoch = (
                        "imported",
                        f"run/{run_id}",
                        "materialized" if materialized else "imported_incomplete",
                        0,
                    )
                    tx.execute(
                        "INSERT OR IGNORE INTO migration_epochs "
                        "(run_id,epoch,writer,checkpoint,owner_json,source_sha256,proof_json,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                        (
                            run_id,
                            0,
                            "legacy",
                            "imported",
                            None,
                            source_sha,
                            json.dumps({"source_id": source.source_id}),
                        ),
                    )
                    imported.append(run_id)
                else:
                    disposition, target, checkpoint, epoch = (
                        "quarantined",
                        None,
                        "quarantined",
                        0,
                    )
                    reason = reason or "MISSING_CANONICAL_RUN_ID"
                    quarantined.append((record_key, reason))
                tx.execute(
                    "INSERT INTO migration_journal "
                    "(source_id,source_sha256,record_key,record_sha256,record_json,run_id,disposition,"
                    "import_target,conflict_reason,checkpoint,epoch) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        source.source_id,
                        source_sha,
                        record_key,
                        record_sha,
                        record_json,
                        run_id,
                        disposition,
                        target,
                        reason,
                        checkpoint,
                        epoch,
                    ),
                )
                event = {
                    "source_id": source.source_id,
                    "source_sha256": source_sha,
                    "record_key": record_key,
                    "run_id": run_id,
                    "disposition": disposition,
                    "reason": reason,
                }
                if source_error is not None:
                    event["source_error"] = source_error
                if reason is None and run_id is not None:
                    event["materialized"] = checkpoint == "materialized"
                    event["materialization_reason"] = material_reason
                self._event_tx(tx, "migration_record_journaled", event)
        return MigrationReport(
            source.source_id,
            source_sha,
            tuple(imported),
            tuple(quarantined),
            tuple(replayed),
        )

    @staticmethod
    def _materialize_run_tx(
        tx,
        source: LegacySource,
        source_bytes: bytes,
        projection: LegacyProjection,
        canonical_run_id: str,
    ) -> tuple[bool, str | None, dict | None]:
        """Copy observed legacy accounting into this enrolled execution store.

        A migration never creates a replacement budget.  The copy is limited
        to the legacy ``runs``/``events`` compatibility schema and uses the
        exact main-database bytes already retained in ``migration_snapshots``.
        Context-only sources and WAL-dependent rows remain journaled but are
        intentionally incomplete handoff candidates.
        """
        if source.kind != "run-store" or projection.source_run_id is None:
            return False, "MIGRATION_MANAGED_CONTEXT_INCOMPLETE", None
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            try:
                connection.deserialize(source_bytes)
                row = connection.execute(
                    "SELECT * FROM runs WHERE id=?", (projection.source_run_id,)
                ).fetchone()
            except (sqlite3.Error, ValueError, AttributeError):
                return False, "MIGRATION_MANAGED_CONTEXT_INCOMPLETE", None
            if row is None or projection.source_run_id != canonical_run_id:
                return False, "MIGRATION_MANAGED_CONTEXT_INCOMPLETE", None
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                return False, "MIGRATION_MANAGED_CONTEXT_INCOMPLETE", None
            if not isinstance(metadata, dict) or any(
                not isinstance(row[name], int) or row[name] < 0
                for name in ("tokens_used", "audit_attempts")
            ) or (
                row["tokens_budget"] is not None
                and (
                    not isinstance(row["tokens_budget"], int)
                    or row["tokens_budget"] < 0
                )
            ):
                return False, "MIGRATION_MANAGED_CONTEXT_INCOMPLETE", None
            values = {
                "id": canonical_run_id,
                "skill": row["skill"],
                "objective": row["objective"],
                "state": row["state"],
                "session_id": row["session_id"],
                "current_phase": row["current_phase"],
                "tokens_used": row["tokens_used"],
                "tokens_budget": row["tokens_budget"],
                "audit_attempts": row["audit_attempts"],
                "last_audit_verdict": row["last_audit_verdict"],
                "worktree": row["worktree"],
                "metadata_json": json.dumps(
                    metadata, sort_keys=True, separators=(",", ":"), allow_nan=False
                ),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "completed_at": row["completed_at"],
            }
            existing = tx.execute("SELECT * FROM runs WHERE id=?", (canonical_run_id,)).fetchone()
            if existing is not None:
                if any(existing[name] != value for name, value in values.items()):
                    return False, "MIGRATION_TARGET_RUN_CONFLICT", None
            else:
                tx.execute(
                    f"INSERT INTO runs ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                    tuple(values.values()),
                )
                event_rows = connection.execute(
                    "SELECT event_type,payload_json,created_at FROM events WHERE run_id=? ORDER BY id",
                    (projection.source_run_id,),
                ).fetchall()
                for event in event_rows:
                    tx.execute(
                        "INSERT INTO events(run_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                        (canonical_run_id, event["event_type"], event["payload_json"], event["created_at"]),
                    )
            return True, None, {
                "tokens_used": row["tokens_used"],
                "tokens_budget": row["tokens_budget"],
                "source": "legacy-runs",
            }
        finally:
            connection.close()

    def _managed_context_ready_tx(self, tx, run_id: str) -> bool:
        """Require observed accounting; missing legacy budgets are never zeroed."""
        journal = tx.execute(
            "SELECT checkpoint FROM migration_journal WHERE run_id=? AND disposition='imported'",
            (run_id,),
        ).fetchone()
        if journal is None or journal["checkpoint"] != "materialized":
            return False
        row = tx.execute(
            "SELECT tokens_used,tokens_budget FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        return (
            row is not None
            and isinstance(row["tokens_used"], int)
            and row["tokens_used"] >= 0
            and isinstance(row["tokens_budget"], int)
            and row["tokens_budget"] >= 0
        )

    def _epoch_tx(self, tx, run_id: str) -> WriterEpoch | None:
        if not isinstance(run_id, str) or not run_id:
            raise MigrationRefused("INVALID_MIGRATION_RUN_ID")
        row = tx.execute(
            "SELECT run_id,epoch,writer,checkpoint,proof_json FROM migration_epochs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            proof = None if row["proof_json"] is None else json.loads(row["proof_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise MigrationRefused("MIGRATION_EPOCH_CORRUPT") from error
        return WriterEpoch(
            row["run_id"], row["epoch"], row["writer"], row["checkpoint"], proof
        )

    def _set_epoch_tx(
        self, tx, run_id: str, epoch: int, writer: str, checkpoint: str, proof: dict
    ) -> WriterEpoch:
        proof_json = json.dumps(
            proof, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        tx.execute(
            "UPDATE migration_epochs SET epoch=?,writer=?,checkpoint=?,proof_json=?,updated_at="
            "strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE run_id=?",
            (epoch, writer, checkpoint, proof_json, run_id),
        )
        self._event_tx(
            tx,
            "migration_writer_epoch",
            {
                "run_id": run_id,
                "epoch": epoch,
                "writer": writer,
                "checkpoint": checkpoint,
                "proof": proof,
            },
        )
        return WriterEpoch(run_id, epoch, writer, checkpoint, proof)

    @staticmethod
    def _event_tx(tx, event_type: str, payload: dict) -> None:
        tx.execute(
            "INSERT INTO control_events(event_type,payload) VALUES(?,?)",
            (
                event_type,
                json.dumps(
                    payload, sort_keys=True, separators=(",", ":"), allow_nan=False
                ),
            ),
        )

    @staticmethod
    def _validate_owner(owner: LegacyOwner) -> None:
        if (
            not isinstance(owner, LegacyOwner)
            or owner.state not in _OWNER_STATES
            or not _json_object(owner.proof)
            or owner.identity is not None
            and not _json_object(owner.identity)
        ):
            raise MigrationRefused("INVALID_LEGACY_OWNER_PROOF")
        if owner.proof.get("observed_state") != owner.state:
            raise MigrationRefused("INVALID_LEGACY_OWNER_PROOF")
        if owner.state in {"released", "dead"} and owner.identity is None:
            raise MigrationRefused("LEGACY_OWNER_IDENTITY_REQUIRED")
        if owner.identity is not None and not _valid_process_identity(owner.identity):
            raise MigrationRefused("INVALID_LEGACY_OWNER_IDENTITY")


def _owner_material(owner: LegacyOwner) -> dict:
    return {"state": owner.state, "identity": owner.identity, "proof": owner.proof}


def _source_error_evidence(
    source: LegacySource,
    source_sha256: str,
    error: BaseException,
) -> dict:
    if isinstance(error, ControlStoreRefused):
        message = error.code
    elif isinstance(error, json.JSONDecodeError):
        message = f"{error.msg}; line={error.lineno}; column={error.colno}"
    elif isinstance(error, UnicodeDecodeError):
        message = (
            f"invalid {error.encoding} input; byte_start={error.start}; "
            f"byte_end={error.end}"
        )
    else:
        # read_legacy_context currently raises AttributeError when valid JSON
        # has a non-object top level.  Do not retain Python's object repr.
        message = "context document must be a JSON object"
    return {
        "code": _INVALID_SOURCE_CODE,
        "source_kind": source.kind,
        "source_path": str(source.canonical_path),
        "source_sha256": source_sha256,
        "exception_class": type(error).__name__,
        "exception_message": message,
    }


def _valid_digest(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value == value.lower()


def _json_object(value: object) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    try:
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


def _valid_process_identity(value: dict) -> bool:
    if set(value) != {"host_id", "boot_id", "pid", "start_token"}:
        return False
    return (
        isinstance(value["pid"], int)
        and not isinstance(value["pid"], bool)
        and value["pid"] > 0
        and all(
            isinstance(value[key], str) and value[key]
            for key in ("host_id", "boot_id", "start_token")
        )
    )


def _parse_registered_sources(raw: object) -> dict[str, dict]:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise MigrationRefused("INVALID_MIGRATION_SOURCE_MANIFEST") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "sources"}
        or payload["schema"] != _SOURCE_SCHEMA
    ):
        raise MigrationRefused("INVALID_MIGRATION_SOURCE_MANIFEST")
    entries = payload["sources"]
    if not isinstance(entries, list) or not entries:
        raise MigrationRefused("INVALID_MIGRATION_SOURCE_MANIFEST")
    result: dict[str, dict] = {}
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"kind", "path", "source_id", "repository_id"}
            or entry["kind"] not in _KINDS
            or not isinstance(entry["path"], str)
            or not Path(entry["path"]).is_absolute()
            or not _valid_digest(entry["source_id"])
            or entry["source_id"] in result
            or entry["repository_id"] is not None
            and (
                not isinstance(entry["repository_id"], str)
                or not entry["repository_id"]
                or "\x00" in entry["repository_id"]
            )
        ):
            raise MigrationRefused("INVALID_MIGRATION_SOURCE_MANIFEST")
        result[entry["source_id"]] = entry
    return result


def _sqlite_identity(path: Path) -> dict[str, dict | None]:
    """Hash the database and every journal/WAL sidecar without following links."""
    identity: dict[str, dict | None] = {}
    for suffix, name in (("database", path), ("journal", Path(f"{path}-journal")),
                         ("wal", Path(f"{path}-wal")), ("shm", Path(f"{path}-shm"))):
        try:
            info, content = _read_regular_source(name)
        except FileNotFoundError:
            identity[suffix] = None
            continue
        identity[suffix] = {
            "device": info.st_dev,
            "inode": info.st_ino,
            "size": info.st_size,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    return identity


def _source_snapshot_identity(
    source: LegacySource, info: os.stat_result, source_sha256: str
) -> dict:
    if source.kind == "run-store":
        return _sqlite_identity(source.canonical_path)
    return {
        "database": {
            "device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
            "sha256": source_sha256,
        },
        "journal": None,
        "wal": None,
        "shm": None,
    }


def _valid_source_snapshot_identity(value: object, *, kind: str) -> bool:
    if not isinstance(value, dict) or set(value) != {"database", "journal", "wal", "shm"}:
        return False
    for name, item in value.items():
        if item is None:
            if name == "database":
                return False
            continue
        if (
            not isinstance(item, dict)
            or set(item) != {"device", "inode", "size", "sha256"}
            or any(not isinstance(item[field], int) or item[field] < 0
                   for field in ("device", "inode", "size"))
            or not _valid_digest(item["sha256"])
        ):
            return False
    # Context has no SQLite sidecar semantics. A run-store's complete
    # identity is intentionally retained even when all sidecars are absent.
    return kind in _KINDS


def _read_fenced_legacy_owner(
    source: LegacySource, *, run_id: str, expected_source_sha256: str,
    expected_snapshot_identity: dict,
) -> LegacyOwner:
    """Use the installed legacy owner-fence table; never caller-provided JSON.

    The compatibility adapter is intentionally narrow.  A legacy store which
    does not expose this durable fence is safe to import but cannot hand off.
    """
    before = _sqlite_identity(source.canonical_path)
    database = before["database"]
    if (
        database is None
        or database["sha256"] != expected_source_sha256
        or before != expected_snapshot_identity
    ):
        raise MigrationRefused("MIGRATION_SOURCE_CHANGED")
    connection = None
    try:
        connection = sqlite3.connect(
            source.canonical_path.as_uri() + "?mode=ro", uri=True, timeout=2
        )
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT state,host_id,boot_id,pid,start_token,legacy_start_fence "
            "FROM migration_legacy_owner_fence WHERE run_id=?",
            (run_id,),
        ).fetchone()
    except sqlite3.Error as error:
        raise MigrationRefused("LEGACY_OWNER_ADAPTER_UNAVAILABLE") from error
    finally:
        if connection is not None:
            connection.close()
    after = _sqlite_identity(source.canonical_path)
    if before != after:
        raise MigrationRefused("MIGRATION_SOURCE_CHANGED")
    if row is None or row["state"] not in _OWNER_STATES:
        raise MigrationRefused("LEGACY_OWNER_ADAPTER_UNAVAILABLE")
    identity = {
        "host_id": row["host_id"], "boot_id": row["boot_id"],
        "pid": row["pid"], "start_token": row["start_token"],
    }
    if not _valid_process_identity(identity) or not isinstance(row["legacy_start_fence"], str) or not row["legacy_start_fence"]:
        raise MigrationRefused("LEGACY_OWNER_ADAPTER_UNAVAILABLE")
    native = probe_identity(ProcessIdentity(**identity))
    state = row["state"]
    # A legacy process claiming death must be natively dead.  A release also
    # needs a native observation and a durable writer-start fence; otherwise
    # stale SQL alone cannot authorize a takeover.
    if state == "dead" and native != DEAD:
        state = "unknown"
    elif state == "released" and native == UNKNOWN:
        state = "unknown"
    elif state == "live" and native != LIVE:
        state = "unknown"
    return LegacyOwner(
        state, identity,
        {
            "schema": "ffs.legacy-owner-adapter/v1",
            "observed_state": state,
            "source_id": source.source_id,
            "legacy_start_fence": row["legacy_start_fence"],
            "native_liveness": native.lower(),
            "sqlite_identity": after,
        },
    )


def assert_managed_epoch(
    store: ControlStore, run_id: str, *, expected_epoch: int | None = None
) -> int | None:
    """Refuse managed admission if a migrated run no longer selects ``new``.

    Non-migration authorities deliberately return ``None`` so their existing
    admission semantics are unchanged.  The state-layer transaction hook uses
    the same table checks for subsequent authority mutations.
    """
    try:
        with store.read_transaction() as tx:
            return store.assert_migration_epoch_tx(
                tx, run_id, expected_epoch=expected_epoch
            )
    except ControlStoreRefused as error:
        raise MigrationRefused(error.code) from error


def _read_regular_source(path: Path) -> tuple[os.stat_result, bytes]:
    """Read one regular source through a no-follow descriptor and bounded size."""
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise MigrationRefused("UNSAFE_MIGRATION_SOURCE")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, _MAX_SOURCE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_SOURCE_BYTES:
                raise MigrationRefused("MIGRATION_SOURCE_TOO_LARGE")
        final = os.fstat(descriptor)
        if (info.st_dev, info.st_ino, info.st_size) != (
            final.st_dev,
            final.st_ino,
            final.st_size,
        ):
            raise MigrationRefused("MIGRATION_SOURCE_CHANGED")
        return info, b"".join(chunks)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
