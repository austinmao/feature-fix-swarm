"""SQLite-backed run state for /feature and /fix lifecycle tracking."""
from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
import secrets
import sqlite3
import stat
import sys
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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


class ControlStoreRefused(RuntimeError):
    """Bounded, capability-free refusal from the fixture control authority."""
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class MigrationRawMutationRefused(RuntimeError):
    """A legacy façade attempted to mutate an enrolled migrated run."""

    def __init__(self, code: str = "MIGRATION_RAW_MUTATION_REFUSED") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class LegacyProjection:
    source_run_id: str | None
    canonical_run_id: str | None
    source_sha256: str
    disposition: str
    code: str | None = None


@dataclass(frozen=True)
class Activity:
    id: str
    repository_id: str
    run_id: str
    kind: str
    input_digest: str
    revision: int
    state: str
    retry_budget: int
    remaining_retry_budget: int
    runtime_tuple_hash: str | None = None
    result: dict | None = None
    reused_result: bool = False


@dataclass(frozen=True)
class RunControl:
    run_id: str
    state: str


@dataclass(frozen=True)
class LaunchIntent:
    id: str
    activity_id: str
    attempt_ordinal: int
    state: str
    child_identity: object | None = None
    code: str | None = None
    # A retry of a reserve request must be distinguishable from a new debit.
    # Callers must reconcile an unresolved intent instead of spawning again.
    reused: bool = False


@dataclass(frozen=True)
class ChildAcknowledgement:
    id: str
    intent_id: str
    child_identity: object


@dataclass(frozen=True)
class ChildPermit:
    id: str
    intent_id: str
    allowed: bool


@dataclass(frozen=True)
class LaunchCohortRequest:
    activity_id: str
    request_key: str
    request_payload: dict
    token_reservation: int
    runtime_receipt_sha256: str
    managed_input_sha256: str


@dataclass(frozen=True)
class LaunchCohort:
    id: str
    request_key: str
    state: str
    members: tuple[LaunchIntent, ...]
    reused: bool = False


@dataclass(frozen=True)
class BudgetDebit:
    id: str
    activity_id: str
    amount: int
    remaining: int


@dataclass(frozen=True)
class Grant:
    id: str
    action: str
    target: str
    consumed: bool


@dataclass(frozen=True)
class Decision:
    id: str
    repository_id: str
    run_id: str
    gate: str
    status: bool
    input_hashes: dict
    evidence: dict
    provenance: dict
    dependencies: list[str]
    expires_at: str


@dataclass(frozen=True)
class RuntimeReceipt:
    receipt_sha256: str
    producer_activity_id: str
    workspace_preparation_id: str
    runtime_tuple_hash: str
    host_id: str
    boot_id: str
    observed_at: str
    expires_at: str
    receipt_json: str
    reused: bool = False


@dataclass(frozen=True)
class LocalCheckReceipt:
    receipt_sha256: str
    producer_activity_id: str
    workspace_preparation_id: str
    acceptance_hash: str
    check_id: str
    material_sha256: str
    runtime_tuple_hash: str
    host_id: str
    boot_id: str
    generation: int
    receipt_json: str
    reused: bool = False


@dataclass(frozen=True)
class AcceptanceContract:
    """One immutable generation of the managed run acceptance boundary."""

    repository_id: str
    run_id: str
    generation: int
    contract_hash: str
    parent_contract_hash: str | None
    material: dict
    accepted_requirement_ids: tuple[str, ...]
    active_obligation_ids: tuple[str, ...]
    amendment_id: str | None = None


@dataclass(frozen=True)
class AcceptanceObligation:
    """A proposed obligation, which becomes a blocker only by amendment."""

    repository_id: str
    run_id: str
    obligation_id: str
    material: dict
    requirement_ids: tuple[str, ...]
    active: bool
    created_generation: int
    activated_generation: int | None


@dataclass(frozen=True)
class AcceptanceDraft:
    """An immutable policy draft bound to one legacy acceptance generation."""

    repository_id: str
    run_id: str
    draft_id: str
    revision: int
    acceptance_generation: int
    acceptance_contract_hash: str
    draft_hash: str
    material: dict
    reused: bool = False


@dataclass(frozen=True)
class SealedAcceptance:
    """A sealed, executable acceptance boundary; never a dispatch envelope."""

    repository_id: str
    run_id: str
    acceptance_generation: int
    draft_id: str
    draft_revision: int
    draft_hash: str
    legacy_generation: int
    legacy_contract_hash: str
    acceptance_hash: str
    material: dict
    reused: bool = False


@dataclass(frozen=True)
class AcceptanceReceipt:
    """A stored typed receipt binding evidence to one sealed acceptance."""

    repository_id: str
    run_id: str
    acceptance_hash: str
    receipt_hash: str
    receipt: object
    reused: bool = False


@dataclass(frozen=True)
class FrontendPolicyState:
    """Durable frontend projection of a sealed acceptance lifecycle.

    This is intentionally a projection inside ``ControlStore``.  The sealed
    contract, policy ledger, launch intent, and typed receipts remain the
    authorities; this row merely makes the next bounded frontend decision
    restart-safe.
    """

    repository_id: str
    run_id: str
    acceptance_hash: str
    stage: str
    candidate_hash: str
    generation: int
    decision_json: dict | None


@dataclass(frozen=True)
class RunPolicyBudget:
    """Durable cumulative authority exposed to managed-policy frontends."""

    repository_id: str
    run_id: str
    tier: str
    recovery_mode: str
    launch_limit: int
    launch_charged: int
    active_limit_ns: int
    active_ns: int
    capacity_wait_ns: int
    operator_wait_ns: int
    clock_boot_id: str
    clock_last_ns: int
    clock_active: bool
    clock_uncertain: bool


@dataclass(frozen=True)
class WorkerCapacityRevision:
    """One append-only operator revision of a run's worker ceiling."""

    repository_id: str
    run_id: str
    revision: int
    worker_capacity: int
    expected_revision: int
    owner_generation: int
    idempotency_key: str


@dataclass(frozen=True)
class PolicyActionReservation:
    id: str
    action: str
    logical_key: str
    input_hash: str
    recovery_cycle: int | None
    mutation_allowed: bool
    transport_attempts: int
    receipt_hash: str | None
    intent_id: str | None
    reused: bool = False


CONTROL_SCHEMA_VERSION = 1
CONTROL_SCHEMA = """
CREATE TABLE control_generation (singleton INTEGER PRIMARY KEY CHECK (singleton = 1), value INTEGER NOT NULL);
INSERT INTO control_generation (singleton, value) VALUES (1, 0);
CREATE TABLE control_reservations (
  id INTEGER PRIMARY KEY,
  owner_set TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  resource_key TEXT NOT NULL,
  generation INTEGER NOT NULL,
  nonce TEXT NOT NULL,
  host_id TEXT NOT NULL,
  boot_id TEXT NOT NULL,
  pid INTEGER NOT NULL,
  start_token TEXT NOT NULL,
  held INTEGER NOT NULL CHECK (held IN (0, 1)),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  released_at TEXT
);
CREATE UNIQUE INDEX control_held_resource ON control_reservations(resource_type, resource_key) WHERE held = 1;
CREATE TABLE control_events (id INTEGER PRIMARY KEY, event_type TEXT NOT NULL, payload TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
"""

# Installed only by an explicit 05-03 fixture-context operation.  Keeping
# these tables out of CONTROL_SCHEMA preserves the 05-02 constructor and its
# byte-level compatibility boundary for callers that only use reservations.
CONTEXT_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_repositories (
  repository_id TEXT PRIMARY KEY,
  marker_id TEXT NOT NULL UNIQUE,
  common_dir TEXT NOT NULL,
  filesystem_id TEXT NOT NULL,
  primary_root TEXT NOT NULL,
  workspace_root TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS context_runs (
  repository_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  objective_digest TEXT NOT NULL,
  objective_text TEXT NOT NULL,
  planning_scope TEXT NOT NULL,
  workspace TEXT NOT NULL,
  workspace_key TEXT NOT NULL UNIQUE,
  evidence_root TEXT NOT NULL,
  state TEXT NOT NULL,
  generation INTEGER NOT NULL,
  activity_id TEXT NOT NULL,
  activity_kind TEXT NOT NULL,
  input_digest TEXT NOT NULL,
  request_key TEXT,
  request_digest TEXT,
  writer_version TEXT,
  result_json TEXT,
  upstream_json TEXT NOT NULL,
  preparation_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (repository_id, run_id),
  FOREIGN KEY (repository_id) REFERENCES context_repositories(repository_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS context_active_objective
  ON context_runs(repository_id, planning_scope, objective_digest)
  WHERE state NOT IN ('complete', 'failed', 'aborted');
CREATE TABLE IF NOT EXISTS context_activities (
  activity_id TEXT PRIMARY KEY,
  repository_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  input_digest TEXT NOT NULL,
  revision INTEGER NOT NULL,
  state TEXT NOT NULL,
  result_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (repository_id, run_id, revision),
  FOREIGN KEY (repository_id, run_id) REFERENCES context_runs(repository_id, run_id)
);
CREATE TABLE IF NOT EXISTS context_workspaces (
  preparation_id TEXT PRIMARY KEY,
  repository_id TEXT,
  run_id TEXT NOT NULL,
  path TEXT NOT NULL,
  path_key TEXT NOT NULL UNIQUE,
  branch TEXT NOT NULL,
  branch_key TEXT NOT NULL,
  base_commit TEXT NOT NULL,
  repository_path TEXT NOT NULL,
  common_dir TEXT NOT NULL,
  selected_manifest_json TEXT NOT NULL,
  selected_manifest_hash TEXT NOT NULL,
  path_existed_before INTEGER NOT NULL,
  branch_existed_before INTEGER NOT NULL,
  registered_before INTEGER NOT NULL,
  created_by_ffs INTEGER NOT NULL DEFAULT 0,
  generation INTEGER NOT NULL,
  state TEXT NOT NULL,
  owned_manifest TEXT,
  parent_preparation_id TEXT,
  parent_activity_id TEXT,
  child_role TEXT,
  child_request_key TEXT,
  native_identity_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (repository_id, branch_key)
);
CREATE TABLE IF NOT EXISTS context_input_snapshots (
  preparation_id TEXT PRIMARY KEY,
  repository_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  base_commit TEXT NOT NULL,
  input_digest TEXT NOT NULL,
  full_manifest_hash TEXT NOT NULL,
  capture_locator TEXT NOT NULL,
  capture_hash TEXT NOT NULL,
  completion_locator TEXT,
  completion_hash TEXT,
  applied_json TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT,
  FOREIGN KEY (preparation_id) REFERENCES context_workspaces(preparation_id)
);
CREATE TABLE IF NOT EXISTS context_requests (
  repository_id TEXT NOT NULL,
  request_key TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  run_id TEXT NOT NULL,
  result_json TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (repository_id, request_key)
);
CREATE TABLE IF NOT EXISTS context_run_material (
  repository_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  selection_manifest_sha256 TEXT NOT NULL,
  input_digest TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  runtime_manifest_sha256 TEXT NOT NULL,
  runtime_digest TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (repository_id, run_id),
  FOREIGN KEY (repository_id, run_id) REFERENCES context_runs(repository_id, run_id)
);
"""

_CONTEXT_SCHEMA_STATEMENTS = tuple(
    statement.strip() for statement in CONTEXT_SCHEMA.split(";") if statement.strip()
)

AUTHORITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS authority_activities (
  id TEXT PRIMARY KEY, repository_id TEXT NOT NULL, run_id TEXT NOT NULL,
  kind TEXT NOT NULL, input_digest TEXT NOT NULL, revision INTEGER NOT NULL,
  state TEXT NOT NULL, retry_budget INTEGER NOT NULL, remaining_retry_budget INTEGER NOT NULL,
  runtime_tuple_hash TEXT, result_json TEXT, request_key TEXT NOT NULL,
  generation INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(repository_id, run_id, revision), UNIQUE(repository_id, run_id, request_key)
);
CREATE TABLE IF NOT EXISTS authority_launch_intents (
  id TEXT PRIMARY KEY, activity_id TEXT NOT NULL, attempt_ordinal INTEGER NOT NULL,
  state TEXT NOT NULL, generation INTEGER NOT NULL,
  capacity_exempt INTEGER NOT NULL DEFAULT 0 CHECK(capacity_exempt IN (0,1)),
  child_host_id TEXT, child_boot_id TEXT, child_pid INTEGER, child_start_token TEXT,
  acknowledgement_id TEXT, permit_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  completion_status TEXT, completion_evidence_json TEXT, token_usage INTEGER, completed_at TEXT,
  UNIQUE(activity_id, attempt_ordinal)
);
CREATE TABLE IF NOT EXISTS authority_run_limits (
  repository_id TEXT NOT NULL, run_id TEXT NOT NULL,
  dispatch_limit INTEGER NOT NULL, token_limit INTEGER NOT NULL, worker_capacity INTEGER NOT NULL,
  dispatch_used INTEGER NOT NULL DEFAULT 0, token_committed INTEGER NOT NULL DEFAULT 0,
  token_used INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(repository_id, run_id),
  CHECK(dispatch_limit >= 0), CHECK(token_limit >= 0), CHECK(worker_capacity > 0),
  CHECK(dispatch_used >= 0), CHECK(token_committed >= 0), CHECK(token_used >= 0)
);
CREATE TABLE IF NOT EXISTS authority_worker_capacity_revisions (
  repository_id TEXT NOT NULL, run_id TEXT NOT NULL, revision INTEGER NOT NULL,
  worker_capacity INTEGER NOT NULL, expected_revision INTEGER NOT NULL,
  idempotency_key TEXT NOT NULL, owner_generation INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(repository_id,run_id,revision),
  UNIQUE(repository_id,run_id,idempotency_key),
  CHECK(revision > 0), CHECK(expected_revision >= 0),
  CHECK(worker_capacity > 0), CHECK(owner_generation > 0)
);
CREATE TABLE IF NOT EXISTS authority_current_worker_capacity (
  repository_id TEXT NOT NULL, run_id TEXT NOT NULL, revision INTEGER NOT NULL,
  worker_capacity INTEGER NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(repository_id,run_id), CHECK(revision > 0), CHECK(worker_capacity > 0)
);
CREATE TABLE IF NOT EXISTS authority_launch_accounting (
  intent_id TEXT PRIMARY KEY, repository_id TEXT NOT NULL, run_id TEXT NOT NULL,
  token_reservation INTEGER NOT NULL, token_final INTEGER,
  created_at TEXT NOT NULL, completed_at TEXT,
  CHECK(token_reservation >= 0), CHECK(token_final IS NULL OR token_final >= 0)
);
CREATE TABLE IF NOT EXISTS authority_launch_cohorts (
  id TEXT PRIMARY KEY, repository_id TEXT NOT NULL, run_id TEXT NOT NULL,
  request_key TEXT NOT NULL, membership_sha256 TEXT NOT NULL,
  member_count INTEGER NOT NULL, state TEXT NOT NULL, generation INTEGER NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(repository_id, run_id, request_key), CHECK(member_count > 0)
);
CREATE TABLE IF NOT EXISTS authority_launch_cohort_members (
  cohort_id TEXT NOT NULL, member_ordinal INTEGER NOT NULL, intent_id TEXT NOT NULL UNIQUE,
  activity_id TEXT NOT NULL, request_key TEXT NOT NULL, request_payload_json TEXT NOT NULL,
  request_payload_sha256 TEXT NOT NULL, token_reservation INTEGER NOT NULL,
  runtime_receipt_sha256 TEXT NOT NULL, managed_input_sha256 TEXT NOT NULL,
  acknowledgement_id TEXT, child_host_id TEXT, child_boot_id TEXT,
  child_pid INTEGER, child_start_token TEXT, acknowledged_at TEXT,
  PRIMARY KEY(cohort_id, member_ordinal), UNIQUE(cohort_id, activity_id),
  UNIQUE(cohort_id, request_key), CHECK(member_ordinal >= 0), CHECK(token_reservation >= 0)
);
CREATE TABLE IF NOT EXISTS authority_qualification_launches (
  intent_id TEXT PRIMARY KEY, activity_id TEXT NOT NULL, request_key TEXT NOT NULL,
  probe_name TEXT NOT NULL, contract_sha256 TEXT NOT NULL, contract_json TEXT NOT NULL,
  qualification_envelope_sha256 TEXT NOT NULL,
  managed_input_sha256 TEXT NOT NULL, qualification_cohort_id TEXT NOT NULL,
  qualification_request_id TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(activity_id, request_key)
);
CREATE TABLE IF NOT EXISTS authority_qualification_promotions (
  activity_id TEXT PRIMARY KEY, qualification_request_key TEXT NOT NULL,
  expected_contract_hashes_json TEXT NOT NULL, runtime_identity TEXT NOT NULL,
  final_contract_hash TEXT NOT NULL, role TEXT NOT NULL,
  observation_evidence_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS authority_child_bindings (
  activity_id TEXT PRIMARY KEY, parent_activity_id TEXT NOT NULL, role TEXT NOT NULL,
  candidate_hash TEXT NOT NULL, contract_hash TEXT NOT NULL, runtime_identity TEXT NOT NULL,
  workspace_binding TEXT NOT NULL, workspace_preparation_id TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS authority_event_keys (
  activity_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, payload_hash TEXT NOT NULL,
  event_id INTEGER NOT NULL, PRIMARY KEY(activity_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS authority_budget_debits (
  id TEXT PRIMARY KEY, repository_id TEXT NOT NULL, run_id TEXT NOT NULL,
  activity_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, amount INTEGER NOT NULL,
  remaining INTEGER NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(activity_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS authority_grants (
  id TEXT PRIMARY KEY, repository_id TEXT NOT NULL, run_id TEXT NOT NULL,
  action TEXT NOT NULL, target TEXT NOT NULL, provenance_json TEXT NOT NULL,
  expires_at TEXT NOT NULL, idempotency_key TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0,
  consume_key TEXT, generation INTEGER NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(repository_id, run_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS authority_decisions (
  id TEXT PRIMARY KEY, repository_id TEXT NOT NULL, run_id TEXT NOT NULL,
  gate TEXT NOT NULL, status INTEGER NOT NULL, input_hashes_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL, provenance_json TEXT NOT NULL, dependencies_json TEXT NOT NULL,
  expires_at TEXT NOT NULL, generation INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS authority_runtime_receipts (
  receipt_sha256 TEXT PRIMARY KEY, producer_activity_id TEXT NOT NULL,
  workspace_preparation_id TEXT NOT NULL, runtime_tuple_hash TEXT NOT NULL,
  host_id TEXT NOT NULL, boot_id TEXT NOT NULL,
  observed_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  receipt_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS authority_local_check_receipts (
  receipt_sha256 TEXT PRIMARY KEY, producer_activity_id TEXT NOT NULL,
  workspace_preparation_id TEXT NOT NULL, acceptance_hash TEXT NOT NULL,
  check_id TEXT NOT NULL, material_sha256 TEXT NOT NULL, runtime_tuple_hash TEXT NOT NULL,
  host_id TEXT NOT NULL, boot_id TEXT NOT NULL, generation INTEGER NOT NULL,
  receipt_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""

# This is deliberately an extension rather than a control-schema version
# bump. Historical authorities remain readable and a writer installs these
# tables in one transaction before it records a managed acceptance contract.
# A contract is append-only by generation; proposals are append-only apart
# from the one proposed -> active transition performed by an amendment.
ACCEPTANCE_CONTRACT_SCHEMA = """
CREATE TABLE IF NOT EXISTS authority_acceptance_contracts (
  repository_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation > 0),
  contract_hash TEXT NOT NULL,
  parent_contract_hash TEXT,
  material_json TEXT NOT NULL,
  material_hash TEXT NOT NULL,
  accepted_requirement_ids_json TEXT NOT NULL,
  active_obligation_ids_json TEXT NOT NULL,
  amendment_id TEXT,
  amendment_json TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(repository_id, run_id, generation),
  UNIQUE(repository_id, run_id, contract_hash),
  UNIQUE(repository_id, run_id, amendment_id)
);
CREATE TABLE IF NOT EXISTS authority_acceptance_obligations (
  repository_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  obligation_id TEXT NOT NULL,
  material_json TEXT NOT NULL,
  material_hash TEXT NOT NULL,
  requirement_ids_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('proposed','active')),
  created_generation INTEGER NOT NULL CHECK(created_generation > 0),
  activated_generation INTEGER,
  created_at TEXT NOT NULL,
  activated_at TEXT,
  PRIMARY KEY(repository_id, run_id, obligation_id),
  CHECK((status='proposed' AND activated_generation IS NULL AND activated_at IS NULL)
     OR (status='active' AND activated_generation IS NOT NULL AND activated_at IS NOT NULL))
);
"""

# E3 is deliberately a second, additive extension *inside* the existing
# ControlStore.  The older acceptance tables remain the authoritative adapter
# for initial creation and operator amendments.  These rows add executable
# draft/seal material and must never reinterpret the old contract hash as a
# launch/dispatch envelope digest.
ACCEPTANCE_POLICY_SCHEMA = """
CREATE TABLE IF NOT EXISTS authority_acceptance_drafts (
  repository_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  draft_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision > 0),
  legacy_generation INTEGER NOT NULL CHECK(legacy_generation > 0),
  legacy_contract_hash TEXT NOT NULL,
  draft_hash TEXT NOT NULL,
  material_json TEXT NOT NULL,
  material_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(repository_id, run_id, draft_id, revision),
  UNIQUE(repository_id, run_id, draft_hash)
);
CREATE TABLE IF NOT EXISTS authority_sealed_acceptances (
  repository_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  acceptance_generation INTEGER NOT NULL CHECK(acceptance_generation > 0),
  draft_id TEXT NOT NULL,
  draft_revision INTEGER NOT NULL CHECK(draft_revision > 0),
  draft_hash TEXT NOT NULL,
  legacy_generation INTEGER NOT NULL CHECK(legacy_generation > 0),
  legacy_contract_hash TEXT NOT NULL,
  acceptance_hash TEXT NOT NULL,
  material_json TEXT NOT NULL,
  material_hash TEXT NOT NULL,
  sealed_at TEXT NOT NULL,
  PRIMARY KEY(repository_id, run_id, acceptance_generation),
  UNIQUE(repository_id, run_id, draft_hash),
  UNIQUE(repository_id, run_id, acceptance_hash)
);
CREATE TABLE IF NOT EXISTS authority_acceptance_receipts (
  repository_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  acceptance_hash TEXT NOT NULL,
  receipt_hash TEXT NOT NULL,
  receipt_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(repository_id, run_id, receipt_hash),
  FOREIGN KEY(repository_id, run_id, acceptance_hash)
    REFERENCES authority_sealed_acceptances(repository_id, run_id, acceptance_hash)
);
CREATE UNIQUE INDEX IF NOT EXISTS authority_one_seal_per_legacy_generation
  ON authority_sealed_acceptances(repository_id, run_id, legacy_generation);
"""

# E4 remains an extension of the single ControlStore.  The rows are keyed by
# the existing repository/run and (once dispatched) the existing intent ID;
# they are not a second status database or a best-effort event projection.
RUN_POLICY_BUDGET_SCHEMA = """
CREATE TABLE IF NOT EXISTS authority_run_policy_budgets (
  repository_id TEXT NOT NULL, run_id TEXT NOT NULL, tier TEXT NOT NULL,
  recovery_mode TEXT NOT NULL DEFAULT 'normal',
  launch_limit INTEGER NOT NULL, active_limit_ns INTEGER NOT NULL,
  launch_charged INTEGER NOT NULL DEFAULT 0, active_ns INTEGER NOT NULL DEFAULT 0,
  clock_boot_id TEXT NOT NULL, clock_last_ns INTEGER NOT NULL,
  clock_last_wall_ns INTEGER NOT NULL DEFAULT 0,
  clock_active INTEGER NOT NULL DEFAULT 0 CHECK(clock_active IN (0,1)),
  clock_uncertain INTEGER NOT NULL DEFAULT 0 CHECK(clock_uncertain IN (0,1)),
  capacity_wait_ns INTEGER NOT NULL DEFAULT 0, operator_wait_ns INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(repository_id, run_id),
  CHECK(launch_limit >= 0), CHECK(active_limit_ns > 0), CHECK(launch_charged >= 0),
  CHECK(active_ns >= 0), CHECK(clock_last_ns >= 0), CHECK(capacity_wait_ns >= 0),
  CHECK(operator_wait_ns >= 0)
);
CREATE TABLE IF NOT EXISTS authority_policy_actions (
  id TEXT PRIMARY KEY, repository_id TEXT NOT NULL, run_id TEXT NOT NULL,
  action TEXT NOT NULL, logical_key TEXT NOT NULL, input_hash TEXT NOT NULL,
  recovery_cycle INTEGER, mutation_allowed INTEGER NOT NULL CHECK(mutation_allowed IN (0,1)),
  state TEXT NOT NULL, transport_attempts INTEGER NOT NULL DEFAULT 0,
  receipt_hash TEXT, intent_id TEXT UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(repository_id, run_id, action, logical_key, input_hash),
  CHECK(transport_attempts >= 0)
);
CREATE INDEX IF NOT EXISTS authority_policy_actions_kind
  ON authority_policy_actions(repository_id, run_id, action, recovery_cycle);
CREATE TABLE IF NOT EXISTS authority_policy_action_attempts (
  action_id TEXT NOT NULL, intent_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
  PRIMARY KEY(action_id, intent_id)
);
CREATE TABLE IF NOT EXISTS authority_policy_clock_reconciliations (
  receipt_hash TEXT PRIMARY KEY, repository_id TEXT NOT NULL, run_id TEXT NOT NULL,
  old_boot_id TEXT NOT NULL, old_monotonic_ns INTEGER NOT NULL,
  new_boot_id TEXT NOT NULL, new_monotonic_ns INTEGER NOT NULL,
  prior_interval_ns INTEGER NOT NULL, evidence_json TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(repository_id, run_id, old_boot_id, old_monotonic_ns, new_boot_id, new_monotonic_ns)
);
CREATE TABLE IF NOT EXISTS authority_policy_work_intervals (
  id TEXT PRIMARY KEY, repository_id TEXT NOT NULL, run_id TEXT NOT NULL,
  generation INTEGER NOT NULL, kind TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('active','completed')),
  started_ns INTEGER NOT NULL, completed_ns INTEGER,
  boot_id TEXT NOT NULL
);
"""

# E6 uses the existing authority database for the frontend lifecycle.  A
# candidate can advance after a repair without changing the acceptance seal or
# resetting its action grants; every advancement points at an already verified
# receipt.  Check results are append-only per (candidate, check) so a resumed
# run cannot mistake a prior candidate's green result for the current one.
FRONTEND_POLICY_SCHEMA = """
CREATE TABLE IF NOT EXISTS authority_frontend_policy_states (
  repository_id TEXT NOT NULL, run_id TEXT NOT NULL, acceptance_hash TEXT NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('SPEC_DRAFT','SPEC_REVIEW','SEALED','EXECUTE','FINAL_REVIEW','RECOVER','DONE','NEEDS_DECISION','CAPABILITY_FAILURE','CANCELLED')),
  candidate_hash TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation > 0),
  decision_json TEXT, updated_at TEXT NOT NULL,
  PRIMARY KEY(repository_id, run_id),
  UNIQUE(repository_id, run_id, acceptance_hash)
);
CREATE TABLE IF NOT EXISTS authority_frontend_policy_candidates (
  repository_id TEXT NOT NULL, run_id TEXT NOT NULL, acceptance_hash TEXT NOT NULL,
  candidate_hash TEXT NOT NULL, receipt_hash TEXT NOT NULL, parent_candidate_hash TEXT,
  generation INTEGER NOT NULL CHECK(generation > 0), created_at TEXT NOT NULL,
  PRIMARY KEY(repository_id, run_id, acceptance_hash, candidate_hash),
  UNIQUE(repository_id, run_id, receipt_hash)
);
CREATE TABLE IF NOT EXISTS authority_frontend_policy_checks (
  repository_id TEXT NOT NULL, run_id TEXT NOT NULL, acceptance_hash TEXT NOT NULL,
  candidate_hash TEXT NOT NULL, check_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('passed','failed')),
  evidence_json TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(repository_id, run_id, acceptance_hash, candidate_hash, check_id)
);
CREATE TABLE IF NOT EXISTS authority_frontend_policy_findings (
  repository_id TEXT NOT NULL, run_id TEXT NOT NULL, acceptance_hash TEXT NOT NULL,
  candidate_hash TEXT NOT NULL, finding_hash TEXT NOT NULL, classification TEXT NOT NULL,
  criterion_ids_json TEXT NOT NULL, invariant_ids_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(repository_id, run_id, acceptance_hash, candidate_hash, finding_hash)
);
"""

_AUTHORITY_SCHEMA_STATEMENTS = tuple(
    statement.strip() for statement in AUTHORITY_SCHEMA.split(";") if statement.strip()
)

_ACCEPTANCE_CONTRACT_SCHEMA_STATEMENTS = tuple(
    statement.strip() for statement in ACCEPTANCE_CONTRACT_SCHEMA.split(";") if statement.strip()
)

_ACCEPTANCE_POLICY_SCHEMA_STATEMENTS = tuple(
    statement.strip() for statement in ACCEPTANCE_POLICY_SCHEMA.split(";") if statement.strip()
)

_RUN_POLICY_BUDGET_SCHEMA_STATEMENTS = tuple(
    statement.strip() for statement in RUN_POLICY_BUDGET_SCHEMA.split(";") if statement.strip()
)

_FRONTEND_POLICY_SCHEMA_STATEMENTS = tuple(
    statement.strip() for statement in FRONTEND_POLICY_SCHEMA.split(";") if statement.strip()
)

_CONTEXT_COLUMNS = {
    "context_repositories": {
        "repository_id", "marker_id", "common_dir", "filesystem_id", "primary_root",
        "workspace_root", "created_at",
    },
    "context_runs": {
        "repository_id", "run_id", "objective_digest", "objective_text", "planning_scope",
        "workspace", "workspace_key", "evidence_root", "state", "generation", "activity_id",
        "activity_kind", "input_digest", "request_key", "request_digest", "result_json",
        "writer_version", "upstream_json", "preparation_id", "created_at", "updated_at",
    },
    "context_activities": {
        "activity_id", "repository_id", "run_id", "kind", "input_digest", "revision",
        "state", "result_json", "created_at", "updated_at",
    },
    "context_workspaces": {
        "preparation_id", "repository_id", "run_id", "path", "path_key", "branch",
        "branch_key", "base_commit", "repository_path", "common_dir",
        "selected_manifest_json", "selected_manifest_hash", "path_existed_before",
        "branch_existed_before", "registered_before", "created_by_ffs", "generation",
        "state", "owned_manifest", "parent_preparation_id", "parent_activity_id",
        "child_role", "child_request_key", "native_identity_json", "created_at", "updated_at",
    },
    "context_input_snapshots": {
        "preparation_id", "repository_id", "run_id", "base_commit", "input_digest",
        "full_manifest_hash", "capture_locator", "capture_hash", "completion_locator",
        "completion_hash", "applied_json", "created_at", "completed_at",
    },
    "context_requests": {
        "repository_id", "request_key", "request_digest", "run_id", "result_json", "created_at",
    },
}

# Writable M4 stores install this private material extension.  It is optional
# during read-only validation so historical context stores remain inspectable;
# versioned resume paths require the row explicitly and never synthesize it.
_CONTEXT_RUN_MATERIAL_COLUMNS = {
    "repository_id", "run_id", "selection_manifest_sha256", "input_digest",
    "snapshot_json", "runtime_manifest_sha256", "runtime_digest", "created_at",
}

_CONTROL_SCHEMA_STATEMENTS = tuple(
    statement.strip() for statement in CONTROL_SCHEMA.split(";") if statement.strip()
)

# Opt-in fixture migration metadata. The legacy runs/events tables retain their
# existing schema in this same authority; these rows are provenance and writer
# admission, never a second run-state database. control_events is the durable
# audit/outbox and is appended in every journal/epoch transaction.
MIGRATION_SCHEMA = """
CREATE TABLE migration_fixture (
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  schema_version INTEGER NOT NULL CHECK(schema_version = 1),
  registration_id TEXT NOT NULL, capability_sha256 TEXT NOT NULL,
  source_identity_json TEXT NOT NULL, readers_ready INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE migration_snapshots (
  source_id TEXT NOT NULL, source_sha256 TEXT NOT NULL, source_bytes BLOB NOT NULL,
  source_identity_json TEXT NOT NULL, record_count INTEGER NOT NULL,
  PRIMARY KEY(source_id, source_sha256)
);
CREATE TABLE migration_journal (
  source_id TEXT NOT NULL, source_sha256 TEXT NOT NULL, record_key TEXT NOT NULL,
  record_sha256 TEXT NOT NULL, record_json TEXT NOT NULL, run_id TEXT,
  disposition TEXT NOT NULL CHECK(disposition IN ('imported', 'quarantined')),
  import_target TEXT, conflict_reason TEXT, checkpoint TEXT NOT NULL,
  epoch INTEGER NOT NULL, PRIMARY KEY(source_id, source_sha256, record_key),
  FOREIGN KEY(source_id, source_sha256)
    REFERENCES migration_snapshots(source_id, source_sha256)
);
CREATE TABLE migration_epochs (
  run_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL CHECK(epoch >= 0),
  writer TEXT NOT NULL CHECK(writer IN ('legacy', 'new', 'none')),
  checkpoint TEXT NOT NULL, owner_json TEXT, source_sha256 TEXT,
  proof_json TEXT, updated_at TEXT NOT NULL
);
"""

_MIGRATION_SCHEMA_STATEMENTS = tuple(
    statement.strip() for statement in MIGRATION_SCHEMA.split(";") if statement.strip()
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_SQLITE_HEADER = b"SQLite format 3\x00"
_ROLLBACK_JOURNAL_HEADER = b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7"
_SQLITE_BUSY_SECONDS = 2.0
_RECOVERY_COPY_LIMIT = 64 * 1024 * 1024

_GATE_REQUIRED_HASHES = {
    "review_complete": frozenset({"candidate", "runtime", "config"}),
    "repair_authorized": frozenset({"candidate", "runtime", "config"}),
    "path_admitted": frozenset({"candidate", "runtime", "config", "policy", "dependencies"}),
    "rollout_ready": frozenset({"candidate", "runtime", "config", "policy", "dependencies"}),
}
_TERMINAL_ACTIVITY_STATES = frozenset({"succeeded", "failed", "aborted"})
_MAX_GRANT_LIFETIME = timedelta(days=7)
_MAX_RUNTIME_RECEIPT_BYTES = 64 * 1024
_CLAUDE_RECEIPT_FRESHNESS_SECONDS = 15 * 60
_MANAGED_WRITER_VERSION = "ffs-supervisor/1"
_ACCEPTANCE_CONTRACT_SCHEMA = "ffs.acceptance-contract/v1"
_QUALIFICATION_CONTRACT_SCHEMA = "ffs.qualification-launch/v1"
_QUALIFICATION_ENVELOPE_SCHEMA = "ffs.qualification-envelope/v1"
_QUALIFICATION_PROBE_ORDER = (
    "ordinary", "native-positive", "native-negative", "native-multi-agent",
)
_QUALIFICATION_PROBES = frozenset(_QUALIFICATION_PROBE_ORDER)


def _parse_utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _canonical_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_canonical_hashes(value: object) -> None:
    """Require every explicitly named SHA-256 value to be canonical lowercase."""
    from .ownership import OwnershipRefused

    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise OwnershipRefused("RUNTIME_RECEIPT_INVALID")
            # Some qualification payloads retain a named map of component
            # digests (for example ``stream_sha256``).  Its scalar members
            # are validated by the enclosing schema, not as though the map
            # itself were one digest.
            if (key == "sha256" or key.endswith("_sha256")) and not isinstance(item, dict):
                if not ControlStore._valid_digest(item) or item != item.lower():
                    raise OwnershipRefused("RUNTIME_RECEIPT_INVALID")
            _validate_canonical_hashes(item)
    elif isinstance(value, list):
        for item in value:
            _validate_canonical_hashes(item)


def _qualified_runtime_material(payload: object) -> tuple[dict, str, str, str, datetime, datetime]:
    """Validate and canonically project one qualified runtime payload."""
    from .ownership import OwnershipRefused
    from host_capabilities import (
        OBSERVATION_FRESHNESS_SECONDS,
        QUALIFIED_RUNTIME_SCHEMA,
        TELEMETRY_SCHEMA,
    )

    required = {
        "schema", "status", "binary", "runtime", "workspace",
        "supervisor", "execution", "observation",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise OwnershipRefused("RUNTIME_RECEIPT_INVALID")
    workspace = payload.get("workspace")
    supervisor = payload.get("supervisor")
    observation = payload.get("observation")
    binary = payload.get("binary")
    runtime = payload.get("runtime")
    execution = payload.get("execution")
    if (
        payload.get("schema") != QUALIFIED_RUNTIME_SCHEMA
        or payload.get("status") != "admitted"
        or not isinstance(binary, dict) or "launcher_sha256" not in binary
        or not set(binary).issubset({"launcher_sha256", "node_sha256", "native_sha256"})
        or not isinstance(runtime, dict)
        or set(runtime) != {"path", "device", "inode", "config_sha256", "hooks_sha256",
                            "skills_sha256", "agents_sha256", "gsd_core_sha256",
                            "scripts_sha256", "gsd_manifest_sha256"}
        or not isinstance(runtime.get("path"), str) or not runtime["path"]
        or any(type(runtime.get(key)) is not int or runtime[key] < 0 for key in ("device", "inode"))
        or not isinstance(execution, dict)
        or set(execution) != {"model", "effort", "sandbox", "network_enabled", "roots",
                              "disabled_features"}
        or not isinstance(execution.get("model"), str)
        or execution.get("effort") is not None and not isinstance(execution.get("effort"), str)
        or not isinstance(execution.get("sandbox"), str)
        or type(execution.get("network_enabled")) is not bool
        or not isinstance(execution.get("roots"), list)
        or any(not isinstance(item, str) or not item for item in execution["roots"])
        or not isinstance(execution.get("disabled_features"), list)
        or any(not isinstance(item, str) or not item for item in execution["disabled_features"])
        or not isinstance(workspace, dict) or set(workspace) != {"path", "device", "inode"}
        or not isinstance(workspace.get("path"), str) or not workspace["path"]
        or any(type(workspace.get(key)) is not int or workspace[key] < 0 for key in ("device", "inode"))
        or not isinstance(supervisor, dict)
        or set(supervisor) != {"host_id", "boot_id", "pid", "start_token"}
        or not all(isinstance(supervisor.get(key), str) and supervisor[key] for key in ("host_id", "boot_id"))
        or type(supervisor.get("pid")) is not int or supervisor["pid"] <= 0
        or not isinstance(supervisor.get("start_token"), str) or not supervisor["start_token"]
        or not isinstance(observation, dict)
        or set(observation) != {"id", "created_at_unix", "environment_sha256", "telemetry_schema"}
        or not isinstance(observation.get("id"), str) or len(observation["id"]) != 32
        or any(character not in "0123456789abcdef" for character in observation["id"])
        or not isinstance(observation.get("environment_sha256"), str)
        or observation.get("telemetry_schema") != TELEMETRY_SCHEMA
        or isinstance(observation.get("created_at_unix"), bool)
        or not isinstance(observation.get("created_at_unix"), (int, float))
        or not math.isfinite(float(observation["created_at_unix"]))
    ):
        raise OwnershipRefused("RUNTIME_RECEIPT_INVALID")
    _validate_canonical_hashes(payload)
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        receipt_bytes = encoded.encode("utf-8")
        observed = datetime.fromtimestamp(float(observation["created_at_unix"]), timezone.utc)
    except (OverflowError, OSError, TypeError, UnicodeError, ValueError):
        raise OwnershipRefused("RUNTIME_RECEIPT_INVALID") from None
    if len(receipt_bytes) > _MAX_RUNTIME_RECEIPT_BYTES:
        raise OwnershipRefused("RUNTIME_RECEIPT_INVALID")
    stable = {
        "schema": payload["schema"],
        "binary": payload["binary"],
        "runtime": payload["runtime"],
        "workspace": workspace,
        "execution": payload["execution"],
        "supervisor": {"host_id": supervisor["host_id"], "boot_id": supervisor["boot_id"]},
        "observation": {
            "environment_sha256": observation["environment_sha256"],
            "telemetry_schema": observation["telemetry_schema"],
        },
    }
    stable_json = json.dumps(stable, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return (
        payload,
        encoded,
        hashlib.sha256(receipt_bytes).hexdigest(),
        hashlib.sha256(stable_json.encode("utf-8")).hexdigest(),
        observed,
        observed + timedelta(seconds=OBSERVATION_FRESHNESS_SECONDS),
    )


def _qualified_claude_runtime_material(payload: object) -> tuple[dict, str, str, str]:
    """Validate one exact Claude qualification payload before it reaches authority.

    Claude's qualification evidence does not carry an observer wall-clock.  The
    durable receipt records its observation window at commit time; this parser
    consequently accepts no caller-supplied freshness timestamp.
    """
    from .ownership import OwnershipRefused
    from .claude_host import QUALIFIED_CLAUDE_RUNTIME_SCHEMA

    required = {
        "schema", "status", "binary", "runtime", "workspace",
        "supervisor", "execution", "observation",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise OwnershipRefused("RUNTIME_RECEIPT_INVALID")
    binary = payload.get("binary")
    runtime = payload.get("runtime")
    workspace = payload.get("workspace")
    supervisor = payload.get("supervisor")
    execution = payload.get("execution")
    observation = payload.get("observation")
    probe_contracts = observation.get("probe_contracts") if isinstance(observation, dict) else None
    streams = observation.get("stream_sha256") if isinstance(observation, dict) else None
    if (
        payload.get("schema") != QUALIFIED_CLAUDE_RUNTIME_SCHEMA
        or payload.get("status") != "admitted"
        or not isinstance(binary, dict) or not binary or "launcher_sha256" not in binary
        or not set(binary).issubset({"launcher_sha256", "node_sha256", "native_sha256"})
        or not isinstance(runtime, dict)
        or set(runtime) != {"path", "device", "inode", "settings_sha256", "stage_sha256"}
        or not isinstance(runtime.get("path"), str) or not runtime["path"]
        or any(type(runtime.get(key)) is not int or runtime[key] < 0 for key in ("device", "inode"))
        or not isinstance(workspace, dict) or set(workspace) != {"path", "device", "inode"}
        or not isinstance(workspace.get("path"), str) or not workspace["path"]
        or any(type(workspace.get(key)) is not int or workspace[key] < 0 for key in ("device", "inode"))
        or not isinstance(supervisor, dict) or set(supervisor) != {"host_id", "boot_id", "pid", "start_token"}
        or not all(isinstance(supervisor.get(key), str) and supervisor[key] for key in ("host_id", "boot_id", "start_token"))
        or type(supervisor.get("pid")) is not int or supervisor["pid"] <= 0
        or not isinstance(execution, dict)
        or set(execution) != {"model", "effort", "sandbox", "network_enabled", "roots", "tools"}
        or not isinstance(execution.get("model"), str) or not execution["model"]
        or execution.get("effort") not in {None, "low", "medium", "high", "xhigh", "max"}
        or execution.get("sandbox") != "workspace-write" or execution.get("network_enabled") is not False
        or execution.get("roots") != [workspace["path"]]
        or execution.get("tools") != "Bash,Edit,Glob,Grep,Read,Skill,Write"
        or not isinstance(observation, dict)
        or set(observation) != {
            "schema", "version", "environment_sha256", "envelope_sha256", "probe_contracts",
            "stream_sha256", "auth_negative", "nested_auth_denied", "hook_events",
            "sandbox_write_boundary", "evidence_sha256",
        }
        or observation.get("schema") != "ffs.claude-runtime-qualification/v1"
        or not isinstance(observation.get("version"), str) or not observation["version"]
        or not isinstance(probe_contracts, dict) or set(probe_contracts) != {
            "auth-negative", "session-model", "sandbox-hooks", "nested-auth",
        }
        or not isinstance(streams, dict) or set(streams) != {
            "session-model", "sandbox-hooks", "nested-auth",
        }
        or observation.get("auth_negative") is not True or observation.get("nested_auth_denied") is not True
        or observation.get("sandbox_write_boundary") is not True
        or not isinstance(observation.get("hook_events"), list)
        or not all(isinstance(event, str) and event for event in observation["hook_events"])
        or not {"PreToolUse", "PostToolUse"}.issubset(
            {event.split(":", 1)[0] for event in observation["hook_events"]}
        )
    ):
        raise OwnershipRefused("RUNTIME_RECEIPT_INVALID")
    for values in (probe_contracts, streams):
        if any(not isinstance(key, str) or not ControlStore._valid_digest(value) or value != value.lower()
               for key, value in values.items()):
            raise OwnershipRefused("RUNTIME_RECEIPT_INVALID")
    _validate_canonical_hashes(payload)
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, UnicodeError):
        raise OwnershipRefused("RUNTIME_RECEIPT_INVALID") from None
    receipt_bytes = encoded.encode("utf-8")
    if len(receipt_bytes) > _MAX_RUNTIME_RECEIPT_BYTES:
        raise OwnershipRefused("RUNTIME_RECEIPT_INVALID")
    stable = {
        "schema": payload["schema"], "binary": binary, "runtime": runtime,
        "workspace": workspace, "execution": execution,
        "supervisor": {"host_id": supervisor["host_id"], "boot_id": supervisor["boot_id"]},
        "observation": observation,
    }
    return (
        payload, encoded, hashlib.sha256(receipt_bytes).hexdigest(),
        hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
    )


def qualified_runtime_tuple_hash(qualified: object) -> str:
    """Return the replay-stable tuple hash for one exact admitted host runtime."""
    from .ownership import OwnershipRefused
    from host_capabilities import QualifiedCodexRuntime
    from .claude_host import QualifiedClaudeRuntime

    if type(qualified) not in {QualifiedCodexRuntime, QualifiedClaudeRuntime}:
        raise OwnershipRefused("RUNTIME_RECEIPT_INVALID")
    try:
        payload = qualified.to_dict()
    except (AttributeError, TypeError, ValueError):
        raise OwnershipRefused("RUNTIME_RECEIPT_INVALID") from None
    if type(qualified) is QualifiedClaudeRuntime:
        return _qualified_claude_runtime_material(payload)[3]
    return _qualified_runtime_material(payload)[3]


class _ProcessAuthorityMutex:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.active_connections = 0


_PROCESS_MUTEX_GUARD = threading.Lock()
_PROCESS_MUTEX_BY_PATH: dict[str, _ProcessAuthorityMutex] = {}
_PROCESS_MUTEX_BY_INODE: dict[tuple[int, int], _ProcessAuthorityMutex] = {}


def _mutex_path_key(path: Path) -> str:
    value = os.fspath(path)
    if sys.platform == "darwin":
        value = unicodedata.normalize("NFC", value).casefold()
    return value


def _authority_mutex(path: Path) -> _ProcessAuthorityMutex:
    path_key = _mutex_path_key(path)
    inode = None
    try:
        info = os.stat(path, follow_symlinks=False)
        if stat.S_ISREG(info.st_mode):
            inode = (info.st_dev, info.st_ino)
    except OSError:
        pass
    with _PROCESS_MUTEX_GUARD:
        mutex = _PROCESS_MUTEX_BY_INODE.get(inode) if inode is not None else None
        if mutex is None:
            mutex = _PROCESS_MUTEX_BY_PATH.get(path_key)
        if mutex is None:
            mutex = _ProcessAuthorityMutex()
        _PROCESS_MUTEX_BY_PATH[path_key] = mutex
        if inode is not None:
            _PROCESS_MUTEX_BY_INODE[inode] = mutex
        return mutex


@dataclass(frozen=True)
class _AuthorityAnchor:
    chain: tuple[tuple[int, int], ...]
    root: tuple[int, int]
    database: tuple[int, int]
    filesystem: tuple[int, int, str]
    sidecar: tuple[tuple[int, int], tuple[int, int], tuple[int, int]] | None


class _DarwinFsid(ctypes.Structure):
    _fields_ = [("value", ctypes.c_int32 * 2)]


class _DarwinStatfs(ctypes.Structure):
    # __DARWIN_STRUCT_STATFS64 from sys/mount.h.
    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", _DarwinFsid),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_flags_ext", ctypes.c_uint32),
        ("f_reserved", ctypes.c_uint32 * 7),
    ]


_LINUX_LOCAL_FILESYSTEMS = {
    0xEF53: "ext",
    0x58465342: "xfs",
    0x9123683E: "btrfs",
    0x01021994: "tmpfs",
    0x794C7630: "overlay",
    0x2FC12FC1: "zfs",
    0x858458F6: "ramfs",
    0xF2F52010: "f2fs",
    0x3153464A: "jfs",
    0x52654973: "reiserfs",
    0x3434: "nilfs",
}


def _refuse(code: str, error: BaseException | None = None):
    refusal = ControlStoreRefused(code)
    if error is None:
        raise refusal
    raise refusal from error


def _sqlite_refusal(error: sqlite3.Error, *, corrupt_default: bool = False):
    message = str(error).lower()
    if "locked" in message or "busy" in message:
        _refuse("STORE_BUSY", error)
    corrupt_markers = ("malformed", "not a database", "file is encrypted", "schema is corrupt")
    if corrupt_default or any(marker in message for marker in corrupt_markers):
        _refuse("CORRUPT_STORE", error)
    _refuse("STORE_IO", error)


def _canonical_absolute(path: Path) -> Path:
    raw = os.fspath(path)
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        _refuse("UNSAFE_STATE_ROOT")
    normalized = os.path.normpath(raw)
    if normalized != raw or ".." in path.parts:
        _refuse("UNSAFE_STATE_ROOT")
    return path


def _filesystem_identity(directory_fd: int) -> tuple[int, int, str]:
    """Identify a supported local filesystem from a held directory descriptor."""
    try:
        fsid = int(os.fstatvfs(directory_fd).f_fsid)
    except (AttributeError, OSError) as error:
        _refuse("UNSAFE_STATE_ROOT", error)
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        value = _DarwinStatfs()
        if libc.fstatfs(directory_fd, ctypes.byref(value)) != 0:
            _refuse("STORE_IO", OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno())))
        filesystem = bytes(value.f_fstypename).split(b"\0", 1)[0].decode("ascii", "strict")
        if not value.f_flags & 0x00001000 or not filesystem:
            _refuse("UNSAFE_STATE_ROOT")
        return fsid, int(value.f_type), filesystem
    if sys.platform.startswith("linux"):
        buffer = ctypes.create_string_buffer(512)
        if libc.fstatfs(directory_fd, ctypes.byref(buffer)) != 0:
            _refuse("STORE_IO", OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno())))
        magic = ctypes.c_long.from_buffer(buffer).value & 0xFFFFFFFF
        filesystem = _LINUX_LOCAL_FILESYSTEMS.get(magic)
        if filesystem is None:
            _refuse("UNSAFE_STATE_ROOT")
        return fsid, magic, filesystem
    _refuse("UNSAFE_STATE_ROOT")


def _directory_policy(info: os.stat_result) -> None:
    mode = stat.S_IMODE(info.st_mode)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or mode & 0o077:
        _refuse("UNSAFE_STATE_ROOT")
    if mode & 0o700 != 0o700:
        _refuse("STORE_IO")


def _database_policy(info: os.stat_result, *, writable: bool) -> None:
    if not stat.S_ISREG(info.st_mode):
        _refuse("UNSAFE_STATE_ROOT")
    if info.st_nlink != 1:
        _refuse("UNSAFE_STATE_ROOT")
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid != os.getuid() or mode & 0o077:
        _refuse("UNSAFE_STATE_ROOT")
    required = 0o600 if writable else 0o400
    if mode & required != required:
        _refuse("STORE_IO")


def _open_parent(path: Path, *, create: bool) -> tuple[int, tuple[tuple[int, int], ...]]:
    if not _NOFOLLOW or not _DIRECTORY:
        _refuse("UNSAFE_STATE_ROOT")
    flags = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _NONBLOCK
    current_fd = -1
    chain: list[tuple[int, int]] = []
    try:
        current_fd = os.open("/", flags)
        chain.append((os.fstat(current_fd).st_dev, os.fstat(current_fd).st_ino))
        for component in path.parent.parts[1:]:
            created_here = False
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                    os.fsync(current_fd)
                    created_here = True
                except FileExistsError:
                    pass
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except OSError as error:
                if error.errno in (errno.ELOOP, errno.ENOTDIR):
                    _refuse("UNSAFE_STATE_ROOT", error)
                raise
            def _entry(name: str):
                try:
                    return os.stat(name, dir_fd=next_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return None

            git_entry = _entry(".git")
            head_entry = _entry("HEAD")
            objects_entry = _entry("objects")
            refs_entry = _entry("refs")
            git_directory = (
                git_entry is not None
                or (
                    head_entry is not None and stat.S_ISREG(head_entry.st_mode)
                    and objects_entry is not None and stat.S_ISDIR(objects_entry.st_mode)
                    and refs_entry is not None and stat.S_ISDIR(refs_entry.st_mode)
                )
            )
            if git_directory:
                os.close(next_fd)
                _refuse("UNSAFE_STATE_ROOT")
            os.close(current_fd)
            current_fd = next_fd
            info = os.fstat(current_fd)
            if created_here:
                _directory_policy(info)
            chain.append((info.st_dev, info.st_ino))
        return current_fd, tuple(chain)
    except ControlStoreRefused:
        if current_fd >= 0:
            os.close(current_fd)
        raise
    except FileNotFoundError:
        if current_fd >= 0:
            os.close(current_fd)
        raise
    except OSError as error:
        if current_fd >= 0:
            os.close(current_fd)
        _refuse("STORE_IO", error)


def _open_database_at(root_fd: int, name: str, *, writable: bool) -> tuple[int, os.stat_result]:
    flags = (os.O_RDWR if writable else os.O_RDONLY) | _NOFOLLOW | _NONBLOCK
    try:
        entry = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISREG(entry.st_mode):
            _refuse("UNSAFE_STATE_ROOT")
        fd = os.open(name, flags, dir_fd=root_fd)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            _refuse("UNSAFE_STATE_ROOT", error)
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _refuse("UNSAFE_STATE_ROOT")
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def _same_entry(root_fd: int, name: str, info: os.stat_result) -> bool:
    try:
        entry = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(entry.st_mode) and (entry.st_dev, entry.st_ino) == (info.st_dev, info.st_ino)


def _copy_regular_at(root_fd: int, source: str, destination: str, *, limit: int) -> None:
    source_fd = destination_fd = -1
    try:
        source_fd = os.open(source, os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=root_fd)
        info = os.fstat(source_fd)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077
        ):
            _refuse("CORRUPT_STORE")
        if info.st_size > limit:
            _refuse("STORE_RECOVERY_LIMIT")
        destination_fd = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o600, dir_fd=root_fd,
        )
        remaining = limit
        while True:
            chunk = os.read(source_fd, min(1024 * 1024, remaining + 1))
            if not chunk:
                break
            remaining -= len(chunk)
            if remaining < 0:
                _refuse("STORE_IO")
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    _refuse("STORE_IO")
                view = view[written:]
        os.fsync(destination_fd)
    except ControlStoreRefused:
        raise
    except OSError as error:
        _refuse("STORE_IO", error)
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        if source_fd >= 0:
            os.close(source_fd)


def _has_hot_journal(root_fd: int, database_name: str) -> bool:
    name = f"{database_name}-journal"
    fd = -1
    try:
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=root_fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            _refuse("CORRUPT_STORE")
        return info.st_size > 512 and os.read(fd, len(_ROLLBACK_JOURNAL_HEADER)) == _ROLLBACK_JOURNAL_HEADER
    except FileNotFoundError:
        return False
    except ControlStoreRefused:
        raise
    except OSError as error:
        _refuse("STORE_IO", error)
    finally:
        if fd >= 0:
            os.close(fd)


def _journal_snapshot(root_fd: int, database_name: str) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    values = []
    for name in (database_name, f"{database_name}-journal"):
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        values.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
    return values[0], values[1]


def _acquire_authority_flock(fd: int) -> None:
    deadline = time.monotonic() + _SQLITE_BUSY_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as error:
            if time.monotonic() >= deadline:
                _refuse("STORE_BUSY", error)
            time.sleep(0.02)


_AUTHORITY_PROTOCOL_DIRECTORY = ".control-locks"
_AUTHORITY_PROTOCOL_VERSION = 2


def _authority_protocol_names(info: os.stat_result) -> tuple[str, str]:
    identity = f"v{_AUTHORITY_PROTOCOL_VERSION}-{info.st_dev:x}-{info.st_ino:x}"
    return f"{identity}.lock", f"{identity}.protocol"


def _authority_protocol_payload(database_name: str, info: os.stat_result) -> bytes:
    return (
        json.dumps(
            {
                "database": database_name,
                "device": info.st_dev,
                "inode": info.st_ino,
                "version": _AUTHORITY_PROTOCOL_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )


def _open_authority_protocol_directory(parent_fd: int, *, create: bool) -> int:
    flags = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _NONBLOCK
    try:
        directory_fd = os.open(_AUTHORITY_PROTOCOL_DIRECTORY, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            _refuse("UNSUPPORTED_SCHEMA")
        try:
            os.mkdir(_AUTHORITY_PROTOCOL_DIRECTORY, 0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileExistsError:
            pass
        directory_fd = os.open(_AUTHORITY_PROTOCOL_DIRECTORY, flags, dir_fd=parent_fd)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            _refuse("UNSAFE_STATE_ROOT", error)
        raise
    try:
        directory_info = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.getuid()
            or stat.S_IMODE(directory_info.st_mode) & 0o077
        ):
            _refuse("UNSAFE_STATE_ROOT")
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _read_authority_protocol_marker(
    directory_fd: int, marker_name: str, expected: bytes,
) -> None:
    marker_fd = -1
    try:
        marker_fd = os.open(marker_name, os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=directory_fd)
        marker_info = os.fstat(marker_fd)
        if (
            not stat.S_ISREG(marker_info.st_mode)
            or marker_info.st_uid != os.getuid()
            or marker_info.st_nlink != 1
            or stat.S_IMODE(marker_info.st_mode) & 0o077
        ):
            _refuse("UNSAFE_STATE_ROOT")
        observed = os.read(marker_fd, len(expected) + 1)
        if observed != expected:
            _refuse("UNSUPPORTED_SCHEMA")
        entry = os.stat(marker_name, dir_fd=directory_fd, follow_symlinks=False)
        if (entry.st_dev, entry.st_ino) != (marker_info.st_dev, marker_info.st_ino):
            _refuse("STORE_REPLACED")
        return marker_info.st_dev, marker_info.st_ino
    except FileNotFoundError:
        _refuse("UNSUPPORTED_SCHEMA")
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            _refuse("UNSAFE_STATE_ROOT", error)
        raise
    finally:
        if marker_fd >= 0:
            os.close(marker_fd)



def _authority_sidecar_identity(
    parent_fd: int, database_name: str, info: os.stat_result, *, allow_absent: bool,
) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]] | None:
    directory_fd = lock_fd = -1
    try:
        directory_fd = _open_authority_protocol_directory(parent_fd, create=False)
        lock_name, marker_name = _authority_protocol_names(info)
        marker_identity = _read_authority_protocol_marker(
            directory_fd,
            marker_name,
            _authority_protocol_payload(database_name, info),
        )
        lock_fd = os.open(lock_name, os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=directory_fd)
        lock_info = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_info.st_mode)
            or lock_info.st_uid != os.getuid()
            or lock_info.st_nlink != 1
            or stat.S_IMODE(lock_info.st_mode) & 0o077
        ):
            _refuse("UNSAFE_STATE_ROOT")
        entry = os.stat(lock_name, dir_fd=directory_fd, follow_symlinks=False)
        if (entry.st_dev, entry.st_ino) != (lock_info.st_dev, lock_info.st_ino):
            _refuse("STORE_REPLACED")
        directory_info = os.fstat(directory_fd)
        return (
            (directory_info.st_dev, directory_info.st_ino),
            (lock_info.st_dev, lock_info.st_ino),
            marker_identity,
        )
    except ControlStoreRefused as error:
        if allow_absent and error.code == "UNSUPPORTED_SCHEMA":
            return None
        raise
    except OSError as error:
        if error.errno == errno.ENOENT:
            _refuse("UNSUPPORTED_SCHEMA", error)
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            _refuse("UNSAFE_STATE_ROOT", error)
        _refuse("STORE_IO", error)
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        if directory_fd >= 0:
            os.close(directory_fd)

def _provision_authority_lock_protocol(
    parent_fd: int, database_name: str, info: os.stat_result,
) -> None:
    """Install the lock sidecar only for a newly published control database."""
    directory_fd = lock_fd = marker_fd = -1
    try:
        directory_fd = _open_authority_protocol_directory(parent_fd, create=True)
        lock_name, marker_name = _authority_protocol_names(info)
        payload = _authority_protocol_payload(database_name, info)
        try:
            lock_fd = os.open(
                lock_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            os.fsync(lock_fd)
        except FileExistsError:
            _refuse("UNSUPPORTED_SCHEMA")
        try:
            marker_fd = os.open(
                marker_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            if os.write(marker_fd, payload) != len(payload):
                _refuse("STORE_IO")
            marker_info = os.fstat(marker_fd)
            if (
                not stat.S_ISREG(marker_info.st_mode)
                or marker_info.st_uid != os.getuid()
                or marker_info.st_nlink != 1
                or stat.S_IMODE(marker_info.st_mode) & 0o077
            ):
                _refuse("UNSAFE_STATE_ROOT")
            os.fsync(marker_fd)
        except FileExistsError:
            _refuse("UNSUPPORTED_SCHEMA")
        os.fsync(directory_fd)
    except ControlStoreRefused:
        raise
    except OSError as error:
        _refuse("STORE_IO", error)
    finally:
        if marker_fd >= 0:
            os.close(marker_fd)
        if lock_fd >= 0:
            os.close(lock_fd)
        if directory_fd >= 0:
            os.close(directory_fd)


def _open_authority_flock(
    parent_fd: int, database_name: str, info: os.stat_result, *, writable: bool,
) -> tuple[int, int]:
    """Open a verified protocol-v2 lock without creating sidecar state."""
    del writable
    directory_fd = fd = -1
    try:
        directory_fd = _open_authority_protocol_directory(parent_fd, create=False)
        lock_name, marker_name = _authority_protocol_names(info)
        _read_authority_protocol_marker(
            directory_fd,
            marker_name,
            _authority_protocol_payload(database_name, info),
        )
        fd = os.open(lock_name, os.O_RDWR | _NOFOLLOW | _NONBLOCK, dir_fd=directory_fd)
        lock_info = os.fstat(fd)
        if (
            not stat.S_ISREG(lock_info.st_mode)
            or lock_info.st_uid != os.getuid()
            or lock_info.st_nlink != 1
            or stat.S_IMODE(lock_info.st_mode) & 0o077
        ):
            _refuse("UNSAFE_STATE_ROOT")
        entry = os.stat(lock_name, dir_fd=directory_fd, follow_symlinks=False)
        if (entry.st_dev, entry.st_ino) != (lock_info.st_dev, lock_info.st_ino):
            _refuse("STORE_REPLACED")
        _acquire_authority_flock(fd)
        entry = os.stat(lock_name, dir_fd=directory_fd, follow_symlinks=False)
        if (entry.st_dev, entry.st_ino) != (lock_info.st_dev, lock_info.st_ino):
            _refuse("STORE_REPLACED")
        return fd, directory_fd
    except ControlStoreRefused:
        if fd >= 0:
            os.close(fd)
        if directory_fd >= 0:
            os.close(directory_fd)
        raise
    except OSError as error:
        if fd >= 0:
            os.close(fd)
        if directory_fd >= 0:
            os.close(directory_fd)
        if error.errno == errno.ENOENT:
            _refuse("UNSUPPORTED_SCHEMA", error)
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            _refuse("UNSAFE_STATE_ROOT", error)
        _refuse("STORE_IO", error)


def _recover_validated_hot_journal(path: Path, root_fd: int) -> None:
    """Recover only after a private staged copy proves the resulting schema."""
    stage = f".{path.name}.recovery-{secrets.token_hex(16)}"
    stage_path = path.parent / stage
    connection = None
    try:
        original = _journal_snapshot(root_fd, path.name)
        _copy_regular_at(root_fd, path.name, stage, limit=_RECOVERY_COPY_LIMIT)
        _copy_regular_at(
            root_fd, f"{path.name}-journal", f"{stage}-journal", limit=_RECOVERY_COPY_LIMIT,
        )
        connection = sqlite3.connect(
            _sqlite_uri(stage_path, "rw"), uri=True, timeout=_SQLITE_BUSY_SECONDS,
            isolation_level=None,
        )
        connection.execute("PRAGMA busy_timeout = 2000")
        _validate_schema(connection)
        connection.close()
        connection = None

        if _journal_snapshot(root_fd, path.name) != original:
            _refuse("STORE_BUSY")

        # The staged rollback produced a supported, internally consistent
        # store. Opening the anchored original read-write may now perform the
        # same rollback; a live writer still wins through SQLite's native lock.
        connection = sqlite3.connect(
            _sqlite_uri(path, "rw"), uri=True, timeout=_SQLITE_BUSY_SECONDS,
            isolation_level=None,
        )
        connection.execute("PRAGMA busy_timeout = 2000")
        _validate_schema(connection)
    except ControlStoreRefused:
        raise
    except sqlite3.Error as error:
        _sqlite_refusal(error, corrupt_default=True)
    finally:
        if connection is not None:
            connection.close()
        for owned_name in (stage, f"{stage}-journal", f"{stage}-wal", f"{stage}-shm"):
            try:
                os.unlink(owned_name, dir_fd=root_fd)
            except OSError:
                pass


def _sqlite_uri(path: Path, mode: str) -> str:
    return f"{path.as_uri()}?mode={mode}"


def _validate_schema(connection: sqlite3.Connection) -> None:
    """Validate schema and data invariants from one consistent read snapshot."""
    owns_snapshot = not connection.in_transaction
    if owns_snapshot:
        connection.execute("BEGIN")
    try:
        _validate_schema_snapshot(connection)
    finally:
        if owns_snapshot and connection.in_transaction:
            connection.rollback()


def _validate_schema_snapshot(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > CONTROL_SCHEMA_VERSION:
        _refuse("UNSUPPORTED_SCHEMA")
    if version != CONTROL_SCHEMA_VERSION:
        _refuse("CORRUPT_STORE")
    expected = {
        "control_generation": {
            "singleton": ("INTEGER", 0, 1), "value": ("INTEGER", 1, 0),
        },
        "control_reservations": {
            "id": ("INTEGER", 0, 1), "owner_set": ("TEXT", 1, 0),
            "resource_type": ("TEXT", 1, 0), "resource_key": ("TEXT", 1, 0),
            "generation": ("INTEGER", 1, 0), "nonce": ("TEXT", 1, 0),
            "host_id": ("TEXT", 1, 0), "boot_id": ("TEXT", 1, 0),
            "pid": ("INTEGER", 1, 0), "start_token": ("TEXT", 1, 0),
            "held": ("INTEGER", 1, 0), "created_at": ("TEXT", 1, 0),
            "released_at": ("TEXT", 0, 0),
        },
        "control_events": {
            "id": ("INTEGER", 0, 1), "event_type": ("TEXT", 1, 0),
            "payload": ("TEXT", 0, 0), "created_at": ("TEXT", 1, 0),
        },
    }
    for table, columns in expected.items():
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        observed = {row[1]: (str(row[2]).upper(), row[3], row[5]) for row in rows}
        if any(observed.get(name) != shape for name, shape in columns.items()):
            _refuse("CORRUPT_STORE")
    definitions = {
        row[0]: " ".join((row[1] or "").lower().replace("\n", " ").split())
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('control_generation', 'control_reservations', 'control_events')"
        )
    }
    if (
        "check (singleton = 1)" not in definitions.get("control_generation", "")
        or "check (held in (0, 1))" not in definitions.get("control_reservations", "")
    ):
        _refuse("CORRUPT_STORE")
    index = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'control_held_resource'"
    ).fetchone()
    normalized = " ".join((index[0] if index and index[0] else "").lower().split())
    if "create unique index" not in normalized or "where held = 1" not in normalized:
        _refuse("CORRUPT_STORE")
    index_columns = [
        row[2] for row in connection.execute("PRAGMA index_info(control_held_resource)")
    ]
    index_list = {
        row[1]: (row[2], row[4]) for row in connection.execute("PRAGMA index_list(control_reservations)")
    }
    if index_columns != ["resource_type", "resource_key"] or index_list.get("control_held_resource") != (1, 1):
        _refuse("CORRUPT_STORE")
    generation = connection.execute(
        "SELECT singleton, value FROM control_generation ORDER BY singleton"
    ).fetchall()
    if len(generation) != 1 or generation[0][0] != 1 or not isinstance(generation[0][1], int) or generation[0][1] < 0:
        _refuse("CORRUPT_STORE")
    malformed = connection.execute(
        "SELECT COUNT(*) FROM control_reservations WHERE generation <= 0 OR pid <= 0 "
        "OR owner_set = '' OR nonce = '' OR host_id = '' OR boot_id = '' OR start_token = '' "
        "OR resource_type NOT IN ('run', 'workspace', 'objective')"
    ).fetchone()[0]
    incomplete = connection.execute(
        "SELECT COUNT(*) FROM ("
        "SELECT owner_set FROM control_reservations GROUP BY owner_set "
        "HAVING COUNT(*) != 3 OR COUNT(DISTINCT resource_type) != 3 "
        "OR MIN(generation) != MAX(generation) OR MIN(nonce) != MAX(nonce) "
        "OR MIN(host_id) != MAX(host_id) OR MIN(boot_id) != MAX(boot_id) "
        "OR MIN(pid) != MAX(pid) OR MIN(start_token) != MAX(start_token) "
        "OR MIN(held) != MAX(held))"
    ).fetchone()[0]
    maximum = connection.execute(
        "SELECT COALESCE(MAX(generation), 0) FROM control_reservations"
    ).fetchone()[0]
    if malformed or incomplete or generation[0][1] < maximum:
        _refuse("CORRUPT_STORE")


def _validate_context_schema_snapshot(
    connection: sqlite3.Connection, *, allow_legacy_writer: bool = False,
    allow_legacy_children: bool = False,
) -> None:
    for table, expected in _CONTEXT_COLUMNS.items():
        observed = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        allowed = [expected]
        if table == "context_runs" and allow_legacy_writer:
            allowed.append(expected - {"writer_version"})
        if table == "context_workspaces" and allow_legacy_children:
            allowed.append(expected - {
                "parent_preparation_id", "parent_activity_id", "child_role", "child_request_key",
                "native_identity_json",
            })
        if observed not in allowed:
            _refuse("CORRUPT_STORE")
    material_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_run_material'"
    ).fetchone()
    if material_table is not None:
        material_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(context_run_material)")
        }
        if material_columns != _CONTEXT_RUN_MATERIAL_COLUMNS:
            _refuse("CORRUPT_STORE")
        malformed_material = connection.execute(
            "SELECT COUNT(*) FROM context_run_material WHERE repository_id='' OR run_id='' "
            "OR length(selection_manifest_sha256)!=64 OR length(input_digest)!=64 "
            "OR snapshot_json='' OR length(runtime_manifest_sha256)!=64 "
            "OR length(runtime_digest)!=64"
        ).fetchone()[0]
        if malformed_material:
            _refuse("CORRUPT_STORE")
    malformed = connection.execute(
        "SELECT COUNT(*) FROM context_runs WHERE repository_id = '' OR run_id = '' "
        "OR generation <= 0 OR workspace = '' OR workspace_key = '' "
        "OR state NOT IN ('preparing', 'ready', 'blocked', 'complete', 'failed', 'aborted')"
    ).fetchone()[0]
    malformed += connection.execute(
        "SELECT COUNT(*) FROM context_workspaces WHERE preparation_id = '' OR run_id = '' "
        "OR generation <= 0 OR state NOT IN ('preparing', 'ready', 'blocked', 'aborted')"
    ).fetchone()[0]
    if malformed:
        _refuse("CORRUPT_STORE")


def _project_gates_snapshot(
    connection: sqlite3.Connection, input_hashes: dict, *, run_id: str,
    repository_id: str, now: str,
) -> dict[str, bool]:
    """Evaluate gate rows and their decision dependencies in one read snapshot."""
    _require_decision_expiry_schema(connection)
    observed_now = _parse_utc_timestamp(now)
    if observed_now is None or not isinstance(input_hashes, dict):
        return {gate: False for gate in _GATE_REQUIRED_HASHES}
    rows = connection.execute(
        "SELECT rowid AS decision_rowid,* FROM authority_decisions "
        "WHERE repository_id = ? AND run_id = ? ORDER BY rowid",
        (repository_id, run_id),
    ).fetchall()
    by_id = {row["id"]: row for row in rows}
    latest: dict[str, object] = {}
    for row in rows:
        if row["gate"] in _GATE_REQUIRED_HASHES:
            latest[row["gate"]] = row
    memo: dict[str, bool] = {}

    def valid(row, visiting: frozenset[str]) -> bool:
        decision_id = row["id"]
        if decision_id in memo:
            return memo[decision_id]
        if decision_id in visiting or not bool(row["status"]):
            return False
        current = latest.get(row["gate"])
        if current is None or current["id"] != decision_id:
            memo[decision_id] = False
            return False
        expiry = _parse_utc_timestamp(row["expires_at"])
        if expiry is None or expiry <= observed_now:
            memo[decision_id] = False
            return False
        try:
            recorded = json.loads(row["input_hashes_json"])
            dependencies = json.loads(row["dependencies_json"])
        except (TypeError, json.JSONDecodeError):
            memo[decision_id] = False
            return False
        required = _GATE_REQUIRED_HASHES.get(row["gate"])
        if (
            required is None or not isinstance(recorded, dict)
            or not isinstance(dependencies, list)
            or any(
                not isinstance(recorded.get(key), str) or not recorded[key]
                or input_hashes.get(key) != recorded[key]
                for key in required
            )
        ):
            memo[decision_id] = False
            return False
        nested = visiting | {decision_id}
        outcome = all(
            isinstance(dependency_id, str)
            and dependency_id in by_id
            and valid(by_id[dependency_id], nested)
            for dependency_id in dependencies
        )
        memo[decision_id] = outcome
        return outcome

    return {
        gate: bool(latest.get(gate) is not None and valid(latest[gate], frozenset()))
        for gate in _GATE_REQUIRED_HASHES
    }


def _require_decision_expiry_schema(connection: sqlite3.Connection) -> None:
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(authority_decisions)")
    }
    if columns and "expires_at" not in columns:
        _refuse("UNSUPPORTED_SCHEMA")


def _validate_existing(
    path: Path, root_fd: int, *, writable: bool, authority_lock_held: bool = False,
) -> tuple[int, int]:
    fd = -1
    connection = None
    try:
        fd, info = _open_database_at(root_fd, path.name, writable=writable)
        header = os.read(fd, len(_SQLITE_HEADER))
        if header != _SQLITE_HEADER or not _same_entry(root_fd, path.name, info):
            _refuse("CORRUPT_STORE" if header != _SQLITE_HEADER else "STORE_REPLACED")
        _database_policy(info, writable=writable)
        if _has_hot_journal(root_fd, path.name):
            if not writable:
                _refuse("STORE_BUSY")
            if authority_lock_held:
                _recover_validated_hot_journal(path, root_fd)
            else:
                lock_fd, lock_directory_fd = _open_authority_flock(root_fd, path.name, info, writable=writable)
                try:
                    # A live cooperative writer may have committed while this
                    # constructor waited for the flock. Recheck after admission;
                    # only a still-hot journal needs staged rollback.
                    if _has_hot_journal(root_fd, path.name):
                        _recover_validated_hot_journal(path, root_fd)
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    os.close(lock_fd)
                    os.close(lock_directory_fd)
        connection = sqlite3.connect(
            _sqlite_uri(path, "ro"), uri=True, timeout=_SQLITE_BUSY_SECONDS, isolation_level=None
        )
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 2000")
        _validate_schema(connection)
        if not _same_entry(root_fd, path.name, info):
            _refuse("STORE_REPLACED")
        return info.st_dev, info.st_ino
    except ControlStoreRefused:
        raise
    except sqlite3.Error as error:
        _sqlite_refusal(error, corrupt_default=True)
    except FileNotFoundError:
        raise
    except OSError as error:
        _refuse("STORE_IO", error)
    finally:
        if connection is not None:
            connection.close()
        if fd >= 0:
            os.close(fd)


def _initialize_database(path: Path, root_fd: int) -> bool:
    stage = f".{path.name}.{secrets.token_hex(16)}.tmp"
    stage_path = path.parent / stage
    fd = -1
    connection = None
    try:
        fd = os.open(stage, os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=root_fd)
        os.close(fd)
        fd = -1
        connection = sqlite3.connect(
            _sqlite_uri(stage_path, "rw"), uri=True, timeout=_SQLITE_BUSY_SECONDS, isolation_level=None
        )
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0].lower() != "delete":
            _refuse("STORE_IO")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 2000")
        connection.execute("BEGIN IMMEDIATE")
        for statement in _CONTROL_SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {CONTROL_SCHEMA_VERSION}")
        connection.commit()
        connection.close()
        connection = None
        fd, info = _open_database_at(root_fd, stage, writable=True)
        _database_policy(info, writable=True)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        if not _same_entry(root_fd, stage, info):
            _refuse("STORE_REPLACED")
        published = False
        try:
            os.link(stage, path.name, src_dir_fd=root_fd, dst_dir_fd=root_fd, follow_symlinks=False)
            published = True
        except FileExistsError:
            pass
        os.unlink(stage, dir_fd=root_fd)
        os.fsync(root_fd)
        return published
    except ControlStoreRefused:
        if connection is not None:
            connection.rollback()
        raise
    except sqlite3.Error as error:
        if connection is not None:
            connection.rollback()
        _sqlite_refusal(error)
    except OSError as error:
        _refuse("STORE_IO", error)
    finally:
        if connection is not None:
            connection.close()
        if fd >= 0:
            os.close(fd)
        for owned_name in (stage, f"{stage}-journal", f"{stage}-wal", f"{stage}-shm"):
            try:
                os.unlink(owned_name, dir_fd=root_fd)
            except OSError:
                pass


class ControlStore:
    """A separate SQLite authority, available only at an explicit fixture path."""
    def __init__(self, path: Path, *, liveness_probe=None, fault_probe=None, policy_clock=None) -> None:
        self.db_path = _canonical_absolute(Path(path))
        self.liveness_probe = liveness_probe
        self.fault_probe = fault_probe
        self.policy_clock = policy_clock
        self._writable = True
        self._process_mutex = _authority_mutex(self.db_path)
        self._fenced_operation = threading.local()
        self._migration_epoch_fence = threading.local()
        with self._hold_process_mutex(raw_validation=True):
            self._initialize()
            self._bind_process_mutex()

    @contextmanager
    def _hold_process_mutex(self, *, raw_validation: bool):
        if not self._process_mutex.lock.acquire(timeout=_SQLITE_BUSY_SECONDS):
            _refuse("STORE_BUSY")
        try:
            if raw_validation and self._process_mutex.active_connections:
                _refuse("STORE_BUSY")
            yield
        finally:
            self._process_mutex.lock.release()

    def _bind_process_mutex(self) -> None:
        path_key = _mutex_path_key(self.db_path)
        inode = self._anchor.database
        with _PROCESS_MUTEX_GUARD:
            existing = _PROCESS_MUTEX_BY_INODE.get(inode)
            if existing is not None and existing is not self._process_mutex:
                _refuse("STORE_BUSY")
            _PROCESS_MUTEX_BY_PATH[path_key] = self._process_mutex
            _PROCESS_MUTEX_BY_INODE[inode] = self._process_mutex

    @contextmanager
    def _hold_authority_flock(self, *, writable: bool):
        """Serialize cooperative processes without adding authority entries."""
        root_fd = db_fd = lock_fd = lock_directory_fd = -1
        try:
            root_fd, _chain = _open_parent(self.db_path, create=False)
            db_fd, info = _open_database_at(root_fd, self.db_path.name, writable=writable)
            _database_policy(info, writable=writable)
            if (info.st_dev, info.st_ino) != self._anchor.database:
                _refuse("STORE_REPLACED")
            sidecar = _authority_sidecar_identity(
                root_fd, self.db_path.name, info, allow_absent=False,
            )
            if sidecar != self._anchor.sidecar:
                _refuse("STORE_REPLACED")
            lock_fd, lock_directory_fd = _open_authority_flock(
                root_fd, self.db_path.name, info, writable=writable,
            )
            if _authority_sidecar_identity(
                root_fd, self.db_path.name, info, allow_absent=False,
            ) != self._anchor.sidecar:
                _refuse("STORE_REPLACED")
            yield
        except ControlStoreRefused:
            raise
        except OSError as error:
            _refuse("STORE_IO", error)
        finally:
            if lock_fd >= 0:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(lock_fd)
            if lock_directory_fd >= 0:
                os.close(lock_directory_fd)
            if db_fd >= 0:
                os.close(db_fd)
            if root_fd >= 0:
                os.close(root_fd)

    def _capture_anchor(
        self, *, create: bool, writable: bool, authority_lock_held: bool = False,
    ) -> _AuthorityAnchor:
        root_fd = -1
        try:
            root_fd, chain = _open_parent(self.db_path, create=create)
            root_info = os.fstat(root_fd)
            _directory_policy(root_info)
            filesystem = _filesystem_identity(root_fd)
            database = _validate_existing(
                self.db_path, root_fd, writable=writable,
                authority_lock_held=authority_lock_held,
            )
            if database[0] != root_info.st_dev:
                _refuse("UNSAFE_STATE_ROOT")
            sidecar = _authority_sidecar_identity(
                root_fd, self.db_path.name,
                os.stat(self.db_path.name, dir_fd=root_fd, follow_symlinks=False),
                allow_absent=True,
            )
            return _AuthorityAnchor(
                chain, (root_info.st_dev, root_info.st_ino), database, filesystem, sidecar
            )
        except FileNotFoundError:
            raise
        except OSError as error:
            _refuse("STORE_IO", error)
        finally:
            if root_fd >= 0:
                os.close(root_fd)

    def _capture_path_anchor(self, *, writable: bool) -> _AuthorityAnchor:
        root_fd = -1
        db_fd = -1
        try:
            root_fd, chain = _open_parent(self.db_path, create=False)
            root_info = os.fstat(root_fd)
            _directory_policy(root_info)
            db_fd, db_info = _open_database_at(root_fd, self.db_path.name, writable=writable)
            _database_policy(db_info, writable=writable)
            if not _same_entry(root_fd, self.db_path.name, db_info):
                _refuse("STORE_REPLACED")
            return _AuthorityAnchor(
                chain,
                (root_info.st_dev, root_info.st_ino),
                (db_info.st_dev, db_info.st_ino),
                self._anchor.filesystem,
                _authority_sidecar_identity(
                    root_fd, self.db_path.name, db_info, allow_absent=True,
                ),
            )
        except ControlStoreRefused:
            raise
        except OSError as error:
            _refuse("STORE_IO", error)
        finally:
            if db_fd >= 0:
                os.close(db_fd)
            if root_fd >= 0:
                os.close(root_fd)

    def _capture_commit_anchor(self) -> _AuthorityAnchor:
        """Recheck paths without closing an fd for SQLite's locked inode."""
        root_fd = -1
        try:
            root_fd, chain = _open_parent(self.db_path, create=False)
            root_info = os.fstat(root_fd)
            _directory_policy(root_info)
            db_info = os.stat(self.db_path.name, dir_fd=root_fd, follow_symlinks=False)
            _database_policy(db_info, writable=True)
            return _AuthorityAnchor(
                chain,
                (root_info.st_dev, root_info.st_ino),
                (db_info.st_dev, db_info.st_ino),
                self._anchor.filesystem,
                _authority_sidecar_identity(
                    root_fd, self.db_path.name, db_info, allow_absent=True,
                ),
            )
        except ControlStoreRefused:
            raise
        except FileNotFoundError as error:
            _refuse("STORE_REPLACED", error)
        except OSError as error:
            _refuse("STORE_IO", error)
        finally:
            if root_fd >= 0:
                os.close(root_fd)

    def _check_anchored(
        self, *, lightweight: bool = False, writable: bool | None = None,
        authority_lock_held: bool = False,
    ) -> None:
        writable = self._writable if writable is None else writable
        try:
            observed = (
                self._capture_commit_anchor()
                if lightweight
                else self._capture_path_anchor(writable=writable)
            )
        except FileNotFoundError as error:
            _refuse("STORE_REPLACED", error)
        if observed != self._anchor:
            _refuse("STORE_REPLACED")
        if not lightweight:
            observed = self._capture_anchor(
                create=False, writable=writable, authority_lock_held=authority_lock_held,
            )
            if observed != self._anchor:
                _refuse("STORE_REPLACED")

    def _initialize(self) -> None:
        root_fd = -1
        try:
            root_fd, _chain = _open_parent(self.db_path, create=True)
            root_info = os.fstat(root_fd)
            _directory_policy(root_info)
            filesystem = _filesystem_identity(root_fd)
            # The database and its immutable lock protocol are one bootstrap
            # operation. A second initializer must not anchor the database in
            # the interval before its publisher has installed that protocol.
            # This is a short registry lock; it never spans a run or worker.
            _acquire_authority_flock(root_fd)
            published = False
            try:
                os.stat(self.db_path.name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                published = _initialize_database(self.db_path, root_fd)
            if published:
                database_fd = -1
                try:
                    database_fd, database_info = _open_database_at(
                        root_fd, self.db_path.name, writable=True,
                    )
                    _database_policy(database_info, writable=True)
                    if not _same_entry(root_fd, self.db_path.name, database_info):
                        _refuse("STORE_REPLACED")
                    _provision_authority_lock_protocol(
                        root_fd, self.db_path.name, database_info,
                    )
                finally:
                    if database_fd >= 0:
                        os.close(database_fd)
            info = os.stat(self.db_path.name, dir_fd=root_fd, follow_symlinks=False)
            bootstrap = _AuthorityAnchor(
                _chain, (root_info.st_dev, root_info.st_ino), (info.st_dev, info.st_ino),
                filesystem, _authority_sidecar_identity(
                    root_fd, self.db_path.name, info, allow_absent=True,
                ),
            )
        except OSError as error:
            _refuse("STORE_IO", error)
        finally:
            if root_fd >= 0:
                os.close(root_fd)  # releases the bootstrap directory flock
        # Schema validation can acquire the ordinary per-database lock. Do it
        # after releasing bootstrap to avoid inverting the two lock orders,
        # then compare every captured identity before binding this instance.
        # Legacy/missing protocol may remain inspectable (sidecar=None), but
        # every writer requires the protocol via _hold_authority_flock. Only
        # the original publisher provisions it; reopening never self-adopts.
        self._anchor = self._capture_anchor(create=False, writable=True)
        if self._anchor != bootstrap:
            _refuse("STORE_REPLACED")

    def _connect_locked(
        self, *, read_only: bool, authority_lock_held: bool = False,
    ) -> sqlite3.Connection:
        self._check_anchored(
            writable=not read_only, authority_lock_held=authority_lock_held,
        )
        try:
            connection = sqlite3.connect(
                _sqlite_uri(self.db_path, "ro" if read_only else "rw"),
                uri=True,
                timeout=_SQLITE_BUSY_SECONDS,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 2000")
            if read_only:
                connection.execute("PRAGMA query_only = ON")
            _validate_schema(connection)
            self._check_anchored(lightweight=True, writable=not read_only)
            return connection
        except ControlStoreRefused:
            if "connection" in locals():
                connection.close()
            raise
        except sqlite3.Error as error:
            if "connection" in locals():
                connection.close()
            _sqlite_refusal(error, corrupt_default=True)

    @contextmanager
    def fenced_operation(self, token):
        """Hold the canonical authority flock across one bounded external effect.

        The initial owner check commits before the effect.  All ordinary writer
        transactions, release, and reclaim use the same flock, so they order
        after this guard without keeping SQLite locks open during filesystem or
        Git work.
        """
        from .ownership import assert_owner
        depth = getattr(self._fenced_operation, "depth", 0)
        if depth:
            if getattr(self._fenced_operation, "token", None) != token:
                _refuse("STORE_BUSY")
            self._fenced_operation.depth = depth + 1
            try:
                yield
            finally:
                self._fenced_operation.depth -= 1
            return
        with self._hold_process_mutex(raw_validation=True):
            with self._hold_authority_flock(writable=True):
                self._fenced_operation.depth = 1
                self._fenced_operation.token = token
                try:
                    with self.transaction() as tx:
                        assert_owner(tx, token)
                    yield
                finally:
                    self._fenced_operation.depth = 0
                    self._fenced_operation.token = None

    @contextmanager
    def transaction(self):
        conn = None
        fenced = bool(getattr(self._fenced_operation, "depth", 0))
        outer = None
        try:
            if not fenced:
                outer = self._hold_process_mutex(raw_validation=True)
                outer.__enter__()
                flock = self._hold_authority_flock(writable=True)
                flock.__enter__()
            else:
                flock = None
            conn = self._connect_locked(read_only=False, authority_lock_held=True)
            self._process_mutex.active_connections += 1
            conn.execute("PRAGMA foreign_keys = ON")
            if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                _refuse("CORRUPT_STORE")
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            self._check_anchored(lightweight=True, writable=True)
            conn.commit()
        except sqlite3.Error as error:
            if conn is not None:
                conn.rollback()
            _sqlite_refusal(error)
        except BaseException:
            if conn is not None:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                conn.close()
                self._process_mutex.active_connections -= 1
            if not fenced and 'flock' in locals() and flock is not None:
                flock.__exit__(None, None, None)
            if outer is not None:
                outer.__exit__(None, None, None)

    @contextmanager
    def migration_interlock(self, registration_id: str, capability_sha256: str):
        """Fixture-only cooperative admission lock, with no open DB transaction.

        Source reads/probes must remain outside writer transactions. Holding
        this existing authority flock allows the fixture legacy writer and
        epoch selection to order together while those reads run. No installed
        legacy executable is claimed to understand this interlock.
        """
        if getattr(self._fenced_operation, "depth", 0):
            _refuse("STORE_BUSY")
        with self._hold_process_mutex(raw_validation=True):
            with self._hold_authority_flock(writable=True):
                self._fenced_operation.depth = 1
                self._fenced_operation.token = ("migration", registration_id)
                try:
                    with self.transaction() as tx:
                        row = tx.execute(
                            "SELECT registration_id, capability_sha256, schema_version "
                            "FROM migration_fixture WHERE singleton = 1"
                        ).fetchone()
                        if (row is None or row["schema_version"] != 1
                                or row["registration_id"] != registration_id
                                or not secrets.compare_digest(row["capability_sha256"], capability_sha256)):
                            _refuse("MIGRATION_FIXTURE_REQUIRED")
                    yield
                finally:
                    self._fenced_operation.depth = 0
                    self._fenced_operation.token = None

    def assert_migration_epoch_tx(self, tx, run_id: str, *, expected_epoch: int | None = None):
        """Assert a migrated run still selects the managed writer in this tx.

        Stores without migration metadata keep their established behavior.
        This is deliberately a transaction-level assertion: a preflight
        observation cannot authorize a workspace/launch mutation after a
        concurrent rollback has selected ``legacy`` or ``none``.
        """
        if not isinstance(run_id, str) or not run_id:
            _refuse("INVALID_MIGRATION_RUN_ID")
        fixture = tx.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_fixture'"
        ).fetchone()
        if fixture is None:
            return None
        row = tx.execute(
            "SELECT epoch,writer FROM migration_epochs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        if row["writer"] == "none":
            _refuse("MIGRATION_WRITER_PAUSED")
        if row["writer"] != "new":
            _refuse("WRITER_HANDOFF_REQUIRED")
        bound = getattr(self._migration_epoch_fence, "value", None)
        if expected_epoch is None and bound is not None and bound[0] == run_id:
            expected_epoch = bound[1]
        if expected_epoch is not None and row["epoch"] != expected_epoch:
            _refuse("MIGRATION_EPOCH_STALE")
        imported = tx.execute(
            "SELECT checkpoint FROM migration_journal WHERE run_id=? AND disposition='imported'",
            (run_id,),
        ).fetchone()
        if imported is None or imported["checkpoint"] != "materialized":
            _refuse("MIGRATION_MANAGED_CONTEXT_INCOMPLETE")
        return row["epoch"]

    def bind_migration_epoch(self, run_id: str, epoch: int) -> None:
        """Bind one managed ingress to the epoch it observed before effects."""
        if (not isinstance(run_id, str) or not run_id or isinstance(epoch, bool)
                or not isinstance(epoch, int) or epoch < 0):
            _refuse("INVALID_MIGRATION_EPOCH")
        self._migration_epoch_fence.value = (run_id, epoch)

    def clear_migration_epoch(self, run_id: str) -> None:
        bound = getattr(self._migration_epoch_fence, "value", None)
        if bound is not None and bound[0] == run_id:
            self._migration_epoch_fence.value = None

    def initialize_migration_fixture(
        self, *, registration_id: str, capability_sha256: str, source_identity_json: str,
    ) -> None:
        """Enroll only a fresh control authority created by the fixture factory."""
        with self.transaction() as tx:
            tables = {row[0] for row in tx.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )}
            if tables != {"control_generation", "control_reservations", "control_events"}:
                _refuse("MIGRATION_FIXTURE_REQUIRED")
            if (tx.execute("SELECT value FROM control_generation").fetchone()[0] != 0
                    or tx.execute("SELECT 1 FROM control_events LIMIT 1").fetchone()
                    or tx.execute("SELECT 1 FROM control_reservations LIMIT 1").fetchone()):
                _refuse("MIGRATION_FIXTURE_REQUIRED")
            for statement in _MIGRATION_SCHEMA_STATEMENTS:
                tx.execute(statement)
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    tx.execute(statement)
            tx.execute(
                "INSERT INTO migration_fixture "
                "(singleton, schema_version, registration_id, capability_sha256, source_identity_json) "
                "VALUES (1, 1, ?, ?, ?)",
                (registration_id, capability_sha256, source_identity_json),
            )
            tx.execute(
                "INSERT INTO control_events(event_type,payload) VALUES ('migration_fixture_registered',?)",
                (json.dumps({"registration_id": registration_id}),),
            )

    def held_reservations(self, keys, *, connection=None):
        """Read held reservation rows without initializing another authority."""
        placeholders = ",".join("(?, ?)" for _ in keys)
        parameters = [value for key in keys for value in key]
        query = ("SELECT owner_set, resource_type, resource_key, generation, nonce, host_id, boot_id, pid, start_token "
                 f"FROM control_reservations WHERE held = 1 AND (resource_type, resource_key) IN ({placeholders})")
        if connection is not None:
            try:
                return connection.execute(query, parameters).fetchall()
            except sqlite3.Error as error:
                _sqlite_refusal(error)
        with self._hold_process_mutex(raw_validation=True):
            with self._hold_authority_flock(writable=False):
                conn = self._connect_locked(read_only=True)
                self._process_mutex.active_connections += 1
                try:
                    return conn.execute(query, parameters).fetchall()
                except sqlite3.Error as error:
                    _sqlite_refusal(error)
                finally:
                    conn.close()
                    self._process_mutex.active_connections -= 1

    def ensure_context_schema(self) -> None:
        """Install the fixture-context tables inside one durable transaction."""
        with self.transaction() as tx:
            for statement in _CONTEXT_SCHEMA_STATEMENTS:
                tx.execute(statement)
            run_columns = {
                row[1] for row in tx.execute("PRAGMA table_info(context_runs)")
            }
            if "writer_version" not in run_columns:
                # Historical context rows are intentionally left NULL.  A
                # managed writer may not retrospectively adopt their state.
                tx.execute("ALTER TABLE context_runs ADD COLUMN writer_version TEXT")
            workspace_columns = {row[1] for row in tx.execute("PRAGMA table_info(context_workspaces)")}
            child_columns = {
                "parent_preparation_id", "parent_activity_id", "child_role", "child_request_key",
                "native_identity_json",
            }
            if workspace_columns not in (
                _CONTEXT_COLUMNS["context_workspaces"],
                _CONTEXT_COLUMNS["context_workspaces"] - child_columns,
            ):
                _refuse("CORRUPT_STORE")
            for name in sorted(child_columns):
                if name not in workspace_columns:
                    tx.execute(f"ALTER TABLE context_workspaces ADD COLUMN {name} TEXT")
            tx.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS context_child_request_unique "
                "ON context_workspaces(repository_id,run_id,child_request_key) "
                "WHERE child_request_key IS NOT NULL"
            )
            _validate_context_schema_snapshot(tx)

    @staticmethod
    def _valid_writer_version(value: object) -> bool:
        return (
            isinstance(value, str) and bool(value) and len(value.encode("utf-8")) <= 128
            and value.isprintable()
        )

    def insert_context_run_with_writer(self, tx, token, *, context_values: dict, writer_version: str) -> str:
        """Insert a newly managed context run and its immutable writer binding.

        This is deliberately an INSERT-only operation.  Historical NULL rows,
        including a crash boundary before request creation, cannot be adopted
        into managed ownership by an UPDATE.
        """
        from .ownership import OwnershipRefused, assert_owner

        if not self._valid_writer_version(writer_version):
            raise OwnershipRefused("INVALID_WRITER_VERSION")
        fields = (
            "repository_id", "run_id", "objective_digest", "objective_text", "planning_scope",
            "workspace", "workspace_key", "evidence_root", "state", "generation", "activity_id",
            "activity_kind", "input_digest", "request_key", "request_digest", "upstream_json",
            "created_at", "updated_at",
        )
        if not isinstance(context_values, dict) or set(context_values) != set(fields):
            raise OwnershipRefused("INVALID_WRITER_REGISTRATION")
        assert_owner(tx, token)
        self.assert_migration_epoch_tx(tx, token.run_id)
        if (
            context_values["repository_id"] != token.repository_id
            or context_values["run_id"] != token.run_id
            or context_values["generation"] != token.generation
            or context_values["state"] != "preparing"
        ):
            raise OwnershipRefused("FENCE_REVOKED")
        activity = tx.execute(
            "SELECT 1 FROM authority_activities WHERE id=? AND repository_id=? AND run_id=? "
            "AND generation=?",
            (context_values["activity_id"], token.repository_id, token.run_id, token.generation),
        ).fetchone()
        existing = tx.execute(
            "SELECT 1 FROM context_runs WHERE repository_id=? AND run_id=?",
            (token.repository_id, token.run_id),
        ).fetchone()
        if activity is None or existing is not None:
            raise OwnershipRefused("WRITER_HANDOFF_REQUIRED")
        columns = (*fields, "writer_version")
        tx.execute(
            f"INSERT INTO context_runs ({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            tuple(context_values[field] for field in fields) + (writer_version,),
        )
        tx.execute(
            "INSERT INTO control_events(event_type,payload) VALUES('writer_version_bound',?)",
            (json.dumps({
                "repository_id": token.repository_id, "run_id": token.run_id,
                "data": {"writer_version": writer_version},
            }, sort_keys=True, separators=(",", ":")),),
        )
        return writer_version

    def assert_writer_version_before_ownership(
        self, *, repository_id: str, run_id: str, writer_version: str,
    ) -> str:
        """Refuse a legacy or conflicting writer before any resume ownership claim."""
        from .ownership import OwnershipRefused

        if (
            not isinstance(repository_id, str) or not repository_id
            or not isinstance(run_id, str) or not run_id
            or not self._valid_writer_version(writer_version)
        ):
            raise OwnershipRefused("INVALID_WRITER_VERSION")
        with self.read_transaction() as tx:
            columns = {row[1] for row in tx.execute("PRAGMA table_info(context_runs)")}
            if "writer_version" not in columns:
                raise OwnershipRefused("WRITER_HANDOFF_REQUIRED")
            row = tx.execute(
                "SELECT writer_version FROM context_runs WHERE repository_id=? AND run_id=?",
                (repository_id, run_id),
            ).fetchone()
        if row is None:
            raise OwnershipRefused("RUN_NOT_FOUND")
        if row["writer_version"] is None:
            raise OwnershipRefused("WRITER_HANDOFF_REQUIRED")
        if row["writer_version"] != writer_version:
            raise OwnershipRefused("WRITER_VERSION_MISMATCH")
        return row["writer_version"]

    def ensure_authority_schema(self) -> None:
        """Install the opt-in activity/control extension in one transaction."""
        with self.transaction() as tx:
            for statement in _AUTHORITY_SCHEMA_STATEMENTS:
                tx.execute(statement)
            child_columns = {row[1] for row in tx.execute("PRAGMA table_info(authority_child_bindings)")}
            if "workspace_preparation_id" not in child_columns:
                tx.execute("ALTER TABLE authority_child_bindings ADD COLUMN workspace_preparation_id TEXT")
            # Releases before the durable supervisor extension created the
            # launch table without terminal evidence fields.  This is an
            # additive migration: no historical intent is inferred or
            # rewritten, and old rows consequently remain conservative.
            launch_columns = {
                row[1] for row in tx.execute("PRAGMA table_info(authority_launch_intents)")
            }
            for name, definition in (
                ("capacity_exempt", "INTEGER NOT NULL DEFAULT 0 CHECK(capacity_exempt IN (0,1))"),
                ("completion_status", "TEXT"),
                ("completion_evidence_json", "TEXT"),
                ("token_usage", "INTEGER"),
                ("completed_at", "TEXT"),
            ):
                if name not in launch_columns:
                    tx.execute(
                        f"ALTER TABLE authority_launch_intents ADD COLUMN {name} {definition}"
                    )
            columns = {
                row[1] for row in tx.execute("PRAGMA table_info(authority_decisions)")
            }
            if "expires_at" not in columns:
                tx.execute(
                    "ALTER TABLE authority_decisions ADD COLUMN expires_at TEXT NOT NULL "
                    "DEFAULT '1970-01-01T00:00:00Z'"
                )

    def ensure_acceptance_contract_schema(self) -> None:
        """Add the append-only acceptance-contract extension if it is absent.

        This is intentionally opt-in and additive.  In particular, opening a
        legacy authority for inspection never creates a contract table or
        invents an acceptance boundary for a historical run.
        """
        with self.transaction() as tx:
            for statement in _ACCEPTANCE_CONTRACT_SCHEMA_STATEMENTS:
                tx.execute(statement)
            self._validate_acceptance_contract_schema_tx(tx)

    def ensure_acceptance_policy_schema(self) -> None:
        """Install the E3 draft/seal tables without changing legacy meanings."""
        self.ensure_acceptance_contract_schema()
        with self.transaction() as tx:
            # A pre-fix E3 writer could create multiple seals for one legacy
            # generation. Refuse that ambiguous history rather than selecting
            # one and silently weakening a sealed acceptance boundary.
            duplicates = tx.execute(
                "SELECT 1 FROM authority_sealed_acceptances "
                "GROUP BY repository_id,run_id,legacy_generation HAVING COUNT(*) > 1 LIMIT 1"
            ).fetchone() if self._acceptance_policy_tables_present_tx(tx) else None
            if duplicates is not None:
                _refuse("ACCEPTANCE_SEAL_GENERATION_CONFLICT")
            for statement in _ACCEPTANCE_POLICY_SCHEMA_STATEMENTS:
                tx.execute(statement)
            self._validate_acceptance_policy_schema_tx(tx)

    @staticmethod
    def _acceptance_tables_present_tx(tx) -> bool:
        tables = {
            row[0] for row in tx.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('authority_acceptance_contracts','authority_acceptance_obligations')"
            )
        }
        if not tables:
            return False
        if tables != {"authority_acceptance_contracts", "authority_acceptance_obligations"}:
            _refuse("ACCEPTANCE_SCHEMA_CORRUPT")
        return True

    @classmethod
    def _validate_acceptance_contract_schema_tx(cls, tx) -> None:
        if not cls._acceptance_tables_present_tx(tx):
            return
        expected = {
            "authority_acceptance_contracts": {
                "repository_id", "run_id", "generation", "contract_hash", "parent_contract_hash",
                "material_json", "material_hash", "accepted_requirement_ids_json",
                "active_obligation_ids_json", "amendment_id", "amendment_json", "created_at",
            },
            "authority_acceptance_obligations": {
                "repository_id", "run_id", "obligation_id", "material_json", "material_hash",
                "requirement_ids_json", "status", "created_generation", "activated_generation",
                "created_at", "activated_at",
            },
        }
        for table, columns in expected.items():
            observed = {row[1] for row in tx.execute(f"PRAGMA table_info({table})")}
            if observed != columns:
                _refuse("ACCEPTANCE_SCHEMA_CORRUPT")

    @staticmethod
    def _acceptance_policy_tables_present_tx(tx) -> bool:
        expected = {
            "authority_acceptance_drafts", "authority_sealed_acceptances",
            "authority_acceptance_receipts",
        }
        tables = {
            row[0] for row in tx.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('authority_acceptance_drafts','authority_sealed_acceptances',"
                "'authority_acceptance_receipts')"
            )
        }
        if not tables:
            return False
        if tables != expected:
            _refuse("ACCEPTANCE_POLICY_SCHEMA_CORRUPT")
        return True

    @classmethod
    def _validate_acceptance_policy_schema_tx(cls, tx) -> None:
        if not cls._acceptance_policy_tables_present_tx(tx):
            return
        expected = {
            "authority_acceptance_drafts": {
                "repository_id", "run_id", "draft_id", "revision", "legacy_generation",
                "legacy_contract_hash", "draft_hash", "material_json", "material_hash", "created_at",
            },
            "authority_sealed_acceptances": {
                "repository_id", "run_id", "acceptance_generation", "draft_id", "draft_revision",
                "draft_hash", "legacy_generation", "legacy_contract_hash", "acceptance_hash",
                "material_json", "material_hash", "sealed_at",
            },
            "authority_acceptance_receipts": {
                "repository_id", "run_id", "acceptance_hash", "receipt_hash", "receipt_json", "created_at",
            },
        }
        for table, columns in expected.items():
            observed = {row[1] for row in tx.execute(f"PRAGMA table_info({table})")}
            if observed != columns:
                _refuse("ACCEPTANCE_POLICY_SCHEMA_CORRUPT")
        index = tx.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='authority_one_seal_per_legacy_generation'"
        ).fetchone()
        normalized = " ".join((index[0] if index and index[0] else "").lower().split())
        columns = [
            row[2] for row in tx.execute("PRAGMA index_info(authority_one_seal_per_legacy_generation)")
        ]
        index_list = {
            row[1]: row[2] for row in tx.execute("PRAGMA index_list(authority_sealed_acceptances)")
        }
        if (
            "create unique index" not in normalized
            or columns != ["repository_id", "run_id", "legacy_generation"]
            or index_list.get("authority_one_seal_per_legacy_generation") != 1
        ):
            _refuse("ACCEPTANCE_POLICY_SCHEMA_CORRUPT")

    @staticmethod
    def _run_policy_tables_present_tx(tx) -> bool:
        expected = {
            "authority_run_policy_budgets", "authority_policy_actions", "authority_policy_action_attempts",
            "authority_policy_clock_reconciliations",
            "authority_policy_work_intervals",
        }
        tables = {row[0] for row in tx.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('authority_run_policy_budgets','authority_policy_actions','authority_policy_action_attempts',"
            "'authority_policy_clock_reconciliations','authority_policy_work_intervals')"
        )}
        if not tables:
            return False
        if tables != expected:
            _refuse("RUN_POLICY_SCHEMA_CORRUPT")
        return True

    @classmethod
    def _validate_run_policy_schema_tx(cls, tx) -> None:
        if not cls._run_policy_tables_present_tx(tx):
            return
        expected = {
            "authority_run_policy_budgets": {
                "repository_id", "run_id", "tier", "recovery_mode", "launch_limit", "active_limit_ns",
                "launch_charged", "active_ns", "clock_boot_id", "clock_last_ns",
                "clock_last_wall_ns",
                "clock_active", "clock_uncertain", "capacity_wait_ns", "operator_wait_ns",
                "created_at", "updated_at",
            },
            "authority_policy_actions": {
                "id", "repository_id", "run_id", "action", "logical_key", "input_hash",
                "recovery_cycle", "mutation_allowed", "state", "transport_attempts",
                "receipt_hash", "intent_id", "created_at", "updated_at",
            },
            "authority_policy_action_attempts": {"action_id", "intent_id", "created_at"},
            "authority_policy_clock_reconciliations": {
                "receipt_hash", "repository_id", "run_id", "old_boot_id", "old_monotonic_ns",
                "new_boot_id", "new_monotonic_ns", "prior_interval_ns", "evidence_json", "created_at",
            },
            "authority_policy_work_intervals": {
                "id", "repository_id", "run_id", "generation", "kind", "state",
                "started_ns", "completed_ns", "boot_id",
            },
        }
        for table, columns in expected.items():
            if {row[1] for row in tx.execute(f"PRAGMA table_info({table})")} != columns:
                _refuse("RUN_POLICY_SCHEMA_CORRUPT")

    def ensure_run_policy_budget_schema(self) -> None:
        """Install E4's policy tables only within the existing authority DB."""
        self.ensure_acceptance_policy_schema()
        with self.transaction() as tx:
            # E4 correction migration: existing scratch policy rows have no
            # mode field. It is safe only because they had no persisted mode;
            # defaulting preserves the approved normal-mode ceiling.
            if tx.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='authority_run_policy_budgets'").fetchone() is not None:
                columns = {row[1] for row in tx.execute("PRAGMA table_info(authority_run_policy_budgets)")}
                if "recovery_mode" not in columns:
                    tx.execute("ALTER TABLE authority_run_policy_budgets ADD COLUMN recovery_mode TEXT NOT NULL DEFAULT 'normal'")
                if "clock_last_wall_ns" not in columns:
                    tx.execute("ALTER TABLE authority_run_policy_budgets ADD COLUMN clock_last_wall_ns INTEGER NOT NULL DEFAULT 0")
            for statement in _RUN_POLICY_BUDGET_SCHEMA_STATEMENTS:
                tx.execute(statement)
            self._validate_run_policy_schema_tx(tx)

    @staticmethod
    def _frontend_policy_tables_present_tx(tx) -> bool:
        expected = {
            "authority_frontend_policy_states", "authority_frontend_policy_candidates",
            "authority_frontend_policy_checks", "authority_frontend_policy_findings",
        }
        tables = {row[0] for row in tx.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('authority_frontend_policy_states','authority_frontend_policy_candidates',"
            "'authority_frontend_policy_checks','authority_frontend_policy_findings')"
        )}
        if not tables:
            return False
        if tables != expected:
            _refuse("FRONTEND_POLICY_SCHEMA_CORRUPT")
        return True

    def ensure_frontend_policy_schema(self) -> None:
        """Install E6 lifecycle rows in the existing ControlStore only."""
        self.ensure_run_policy_budget_schema()
        with self.transaction() as tx:
            for statement in _FRONTEND_POLICY_SCHEMA_STATEMENTS:
                tx.execute(statement)
            if not self._frontend_policy_tables_present_tx(tx):
                _refuse("FRONTEND_POLICY_SCHEMA_CORRUPT")

    @staticmethod
    def _policy_budget_from_row(row) -> RunPolicyBudget:
        return RunPolicyBudget(
            row["repository_id"], row["run_id"], row["tier"], row["recovery_mode"], row["launch_limit"],
            row["launch_charged"], row["active_limit_ns"], row["active_ns"],
            row["capacity_wait_ns"], row["operator_wait_ns"], row["clock_boot_id"],
            row["clock_last_ns"], bool(row["clock_active"]), bool(row["clock_uncertain"]),
        )

    @staticmethod
    def _policy_action_from_row(row, *, reused: bool = False) -> PolicyActionReservation:
        return PolicyActionReservation(
            row["id"], row["action"], row["logical_key"], row["input_hash"],
            row["recovery_cycle"], bool(row["mutation_allowed"]), row["transport_attempts"],
            row["receipt_hash"], row["intent_id"], reused,
        )

    @staticmethod
    def _policy_identifier(value: object, code: str) -> str:
        if not isinstance(value, str) or not value or value != value.strip() or len(value) > 256:
            _refuse(code)
        return value

    @staticmethod
    def _policy_monotonic(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _refuse("POLICY_CLOCK_INVALID")
        return value

    def configure_run_policy_budget(self, token, *, tier: object, clock_boot_id: object,
                                    clock_monotonic_ns: object, recovery_mode: object = "normal") -> RunPolicyBudget:
        """Persist the immutable tier before any policy-controlled dispatch."""
        from .ownership import OwnershipRefused, assert_owner
        from .run_policy import RunPolicyRefused, policy_tier, recovery_mode as validate_recovery_mode
        try:
            selected = policy_tier(tier)
            selected_mode = validate_recovery_mode(recovery_mode)
        except RunPolicyRefused as error:
            raise OwnershipRefused(error.code) from None
        boot = self._policy_identifier(clock_boot_id, "POLICY_CLOCK_INVALID")
        monotonic = self._policy_monotonic(clock_monotonic_ns)
        self.ensure_context_schema()
        self.ensure_run_policy_budget_schema()
        with self.transaction() as tx:
            assert_owner(tx, token)
            self.assert_migration_epoch_tx(tx, token.run_id)
            row = tx.execute("SELECT * FROM authority_run_policy_budgets WHERE repository_id=? AND run_id=?",
                             (token.repository_id, token.run_id)).fetchone()
            if row is not None:
                prior = self._policy_budget_from_row(row)
                if (prior.tier, prior.recovery_mode, prior.clock_boot_id, prior.clock_last_ns) != (selected.name, selected_mode, boot, monotonic):
                    raise OwnershipRefused("RUN_POLICY_IMMUTABLE")
                return prior
            used = tx.execute(
                "SELECT 1 FROM authority_launch_intents i JOIN authority_activities a ON a.id=i.activity_id "
                "WHERE a.repository_id=? AND a.run_id=? LIMIT 1",
                (token.repository_id, token.run_id),
            ).fetchone()
            limits = tx.execute(
                "SELECT dispatch_used FROM authority_run_limits WHERE repository_id=? AND run_id=?",
                (token.repository_id, token.run_id),
            ).fetchone()
            if used is not None or (limits is not None and limits["dispatch_used"] != 0):
                raise OwnershipRefused("POLICY_LATE_CONFIGURATION_REFUSED")
            now = self._now()
            tx.execute("INSERT INTO authority_run_policy_budgets "
                       "(repository_id,run_id,tier,recovery_mode,launch_limit,active_limit_ns,clock_boot_id,clock_last_ns,clock_last_wall_ns,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (token.repository_id, token.run_id, selected.name, selected_mode, selected.launch_limit,
                        selected.active_limit_ns, boot, monotonic, time.time_ns(), now, now))
            self._record_acceptance_event_tx(tx, "run_policy_configured", {
                "repository_id": token.repository_id, "run_id": token.run_id, "tier": selected.name,
                "launch_limit": selected.launch_limit, "active_limit_ns": selected.active_limit_ns,
            })
            return RunPolicyBudget(token.repository_id, token.run_id, selected.name, selected_mode,
                                   selected.launch_limit, 0, selected.active_limit_ns, 0, 0, 0,
                                   boot, monotonic, False, False)

    def get_run_policy_budget(self, *, repository_id: str, run_id: str) -> RunPolicyBudget | None:
        with self.read_transaction() as tx:
            if not self._run_policy_tables_present_tx(tx):
                return None
            self._validate_run_policy_schema_tx(tx)
            row = tx.execute("SELECT * FROM authority_run_policy_budgets WHERE repository_id=? AND run_id=?",
                             (repository_id, run_id)).fetchone()
            return None if row is None else self._policy_budget_from_row(row)

    def reserve_policy_action(self, token, *, action: object, logical_key: object, input_hash: object,
                              recovery_cycle: object = None, required_launch_overhead: object = 1) -> PolicyActionReservation:
        """Reserve a named non-resettable action before a transport launch.

        The reservation is idempotent for its exact frozen logical input.  A
        final-review grant is deliberately not transferable to another input.
        """
        from .ownership import OwnershipRefused, assert_owner
        from .run_policy import RunPolicyRefused, action_can_mutate, action_limit
        action = self._policy_identifier(action, "POLICY_ACTION_INVALID")
        logical_key = self._policy_identifier(logical_key, "POLICY_LOGICAL_KEY_INVALID")
        if not self._valid_digest(input_hash):
            raise OwnershipRefused("POLICY_INPUT_INVALID")
        if isinstance(required_launch_overhead, bool) or not isinstance(required_launch_overhead, int) or required_launch_overhead < 1:
            raise OwnershipRefused("POLICY_STAGE_OVERHEAD_INVALID")
        if recovery_cycle is not None and (isinstance(recovery_cycle, bool) or not isinstance(recovery_cycle, int) or recovery_cycle < 1):
            raise OwnershipRefused("POLICY_RECOVERY_CYCLE_INVALID")
        self.ensure_run_policy_budget_schema()
        self._preflight_policy_clock(token)
        with self.transaction() as tx:
            assert_owner(tx, token)
            budget = tx.execute("SELECT * FROM authority_run_policy_budgets WHERE repository_id=? AND run_id=?",
                                (token.repository_id, token.run_id)).fetchone()
            if budget is None:
                raise OwnershipRefused("RUN_POLICY_REQUIRED")
            if budget["clock_uncertain"]:
                raise OwnershipRefused("CLOCK_RECONCILIATION_REQUIRED")
            if budget["active_ns"] >= budget["active_limit_ns"]:
                raise OwnershipRefused("ACTIVE_TIME_EXHAUSTED")
            existing = tx.execute("SELECT * FROM authority_policy_actions WHERE repository_id=? AND run_id=? AND action=? AND logical_key=? AND input_hash=?",
                                  (token.repository_id, token.run_id, action, logical_key, input_hash)).fetchone()
            if existing is not None:
                if existing["state"] == "cancelled":
                    raise OwnershipRefused("POLICY_ACTION_CANCELLED")
                return self._policy_action_from_row(existing, reused=True)
            try:
                limit = action_limit(action, budget["tier"])
            except RunPolicyRefused as error:
                raise OwnershipRefused(error.code) from None
            if action in {"recovery_cycle_normal", "recovery_cycle_autonomous"}:
                expected = "recovery_cycle_" + budget["recovery_mode"]
                if action != expected:
                    raise OwnershipRefused("RECOVERY_MODE_CONFLICT")
            if action == "recovery_trial":
                if recovery_cycle is None:
                    raise OwnershipRefused("POLICY_RECOVERY_CYCLE_REQUIRED")
                cycle = tx.execute("SELECT 1 FROM authority_policy_actions WHERE repository_id=? AND run_id=? "
                                   "AND action IN ('recovery_cycle_normal','recovery_cycle_autonomous') AND recovery_cycle=?",
                                   (token.repository_id, token.run_id, recovery_cycle)).fetchone()
                used = tx.execute("SELECT COALESCE(SUM(MAX(1,transport_attempts)),0) FROM authority_policy_actions WHERE repository_id=? AND run_id=? "
                                  "AND action='recovery_trial' AND recovery_cycle=?",
                                  (token.repository_id, token.run_id, recovery_cycle)).fetchone()[0]
                if cycle is None or used >= limit:
                    raise OwnershipRefused("RECOVERY_TRIAL_LIMIT_EXHAUSTED")
            elif limit is not None:
                used = tx.execute("SELECT COALESCE(SUM(CASE WHEN action='repair' THEN MAX(1,transport_attempts) ELSE 1 END),0) FROM authority_policy_actions WHERE repository_id=? AND run_id=? AND action=? AND state<>'cancelled'",
                                  (token.repository_id, token.run_id, action)).fetchone()[0]
                if used >= limit:
                    raise OwnershipRefused("POLICY_ACTION_LIMIT_EXHAUSTED")
            if budget["launch_charged"] > budget["launch_limit"] - required_launch_overhead:
                raise OwnershipRefused("POLICY_STAGE_INFEASIBLE")
            identifier = str(uuid.uuid4())
            now = self._now()
            tx.execute("INSERT INTO authority_policy_actions "
                       "(id,repository_id,run_id,action,logical_key,input_hash,recovery_cycle,mutation_allowed,state,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?,'reserved',?,?)",
                       (identifier, token.repository_id, token.run_id, action, logical_key, input_hash,
                        recovery_cycle, int(action_can_mutate(action)), now, now))
            return PolicyActionReservation(identifier, action, logical_key, input_hash, recovery_cycle,
                                           action_can_mutate(action), 0, None, None)

    def cancel_unlaunched_policy_review(self, token, *, action_id: str) -> None:
        """Release an unconsumed review reservation, never an issued repair."""
        from .ownership import OwnershipRefused, assert_owner
        with self.transaction() as tx:
            assert_owner(tx, token)
            row = tx.execute("SELECT * FROM authority_policy_actions WHERE id=? AND repository_id=? AND run_id=?",
                             (action_id, token.repository_id, token.run_id)).fetchone()
            if (row is None or row["action"] not in {"spec_review", "final_review"}
                    or row["transport_attempts"] != 0 or row["intent_id"] is not None
                    or row["state"] not in {"reserved", "cancelled"}
                    or tx.execute("SELECT 1 FROM authority_policy_action_attempts WHERE action_id=?", (action_id,)).fetchone() is not None):
                raise OwnershipRefused("POLICY_ACTION_ALREADY_ISSUED")
            if row["state"] == "cancelled":
                return
            tx.execute("UPDATE authority_policy_actions SET state='cancelled',updated_at=? WHERE id=?",
                       (self._now(), action_id))
            self._record_acceptance_event_tx(tx, "unlaunched_review_cancelled", {
                "repository_id": token.repository_id, "run_id": token.run_id,
                "action_id": action_id, "input_hash": row["input_hash"],
            })

    def reconcile_run_policy_clock(self, token, *, receipt: object) -> RunPolicyBudget:
        """Clear a clock fence only with a durable, verified reconciliation receipt."""
        from .ownership import OwnershipRefused, assert_owner
        if not isinstance(receipt, dict) or set(receipt) != {
            "schema", "old_boot_id", "old_monotonic_ns", "new_boot_id", "new_monotonic_ns",
            "prior_interval_ns", "evidence",
        } or receipt["schema"] != "ffs.run-policy-clock-reconciliation/v1":
            raise OwnershipRefused("POLICY_CLOCK_RECEIPT_INVALID")
        old_boot = self._policy_identifier(receipt["old_boot_id"], "POLICY_CLOCK_RECEIPT_INVALID")
        new_boot = self._policy_identifier(receipt["new_boot_id"], "POLICY_CLOCK_RECEIPT_INVALID")
        old_monotonic = self._policy_monotonic(receipt["old_monotonic_ns"])
        new_monotonic = self._policy_monotonic(receipt["new_monotonic_ns"])
        prior_interval = self._policy_monotonic(receipt["prior_interval_ns"])
        evidence = self._verified_evidence(receipt["evidence"])
        try:
            proof_bytes = Path(evidence["locator"]).read_bytes()
            proof = json.loads(proof_bytes)
        except (OSError, ValueError, TypeError):
            raise OwnershipRefused("POLICY_CLOCK_RECEIPT_INVALID") from None
        expected_proof = {key: value for key, value in receipt.items() if key != "evidence"}
        expected_proof.update(repository_id=token.repository_id, run_id=token.run_id)
        if proof != expected_proof or hashlib.sha256(proof_bytes).hexdigest() != evidence["sha256"]:
            raise OwnershipRefused("POLICY_CLOCK_RECEIPT_BINDING_INVALID")
        encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False)
        receipt_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        self.ensure_run_policy_budget_schema()
        with self.transaction() as tx:
            assert_owner(tx, token)
            row = tx.execute("SELECT * FROM authority_run_policy_budgets WHERE repository_id=? AND run_id=?",
                             (token.repository_id, token.run_id)).fetchone()
            if row is None:
                raise OwnershipRefused("RUN_POLICY_REQUIRED")
            if (not row["clock_uncertain"] or row["clock_boot_id"] != old_boot
                    or row["clock_last_ns"] != old_monotonic):
                raise OwnershipRefused("POLICY_CLOCK_RECEIPT_BINDING_INVALID")
            # File bytes supplied by an owner cannot establish an unobserved
            # productive interval across a reboot. Retain the fence and hand
            # off a typed capability failure rather than invent elapsed time.
            if row["clock_active"]:
                raise OwnershipRefused("POLICY_CLOCK_INTERVAL_UNPROVEN")
            active = row["active_ns"] + (prior_interval if row["clock_active"] else 0)
            if active > row["active_limit_ns"]:
                raise OwnershipRefused("ACTIVE_TIME_EXHAUSTED")
            existing = tx.execute("SELECT * FROM authority_policy_clock_reconciliations WHERE receipt_hash=?",
                                  (receipt_hash,)).fetchone()
            if existing is not None:
                if (existing["repository_id"], existing["run_id"], existing["old_boot_id"],
                    existing["old_monotonic_ns"], existing["new_boot_id"], existing["new_monotonic_ns"],
                    existing["prior_interval_ns"], existing["evidence_json"]) != (
                    token.repository_id, token.run_id, old_boot, old_monotonic, new_boot, new_monotonic,
                    prior_interval, json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                ):
                    raise OwnershipRefused("POLICY_CLOCK_RECEIPT_CONFLICT")
            now = self._now()
            if existing is None:
                tx.execute("INSERT INTO authority_policy_clock_reconciliations "
                           "(receipt_hash,repository_id,run_id,old_boot_id,old_monotonic_ns,new_boot_id,new_monotonic_ns,prior_interval_ns,evidence_json,created_at) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (receipt_hash, token.repository_id, token.run_id, old_boot, old_monotonic,
                            new_boot, new_monotonic, prior_interval,
                            json.dumps(evidence, sort_keys=True, separators=(",", ":")), now))
            tx.execute("UPDATE authority_run_policy_budgets SET active_ns=?,clock_boot_id=?,clock_last_ns=?,clock_last_wall_ns=?,clock_uncertain=0,updated_at=? WHERE repository_id=? AND run_id=?",
                       (active, new_boot, new_monotonic, time.time_ns(), now, token.repository_id, token.run_id))
            row = tx.execute("SELECT * FROM authority_run_policy_budgets WHERE repository_id=? AND run_id=?",
                             (token.repository_id, token.run_id)).fetchone()
            return self._policy_budget_from_row(row)

    def record_policy_action_receipt(self, token, *, action_id: object, input_hash: object,
                                     receipt_hash: object, valid: object) -> PolicyActionReservation:
        """Settle a logical action once; valid review evidence deduplicates retries."""
        from .ownership import OwnershipRefused, assert_owner
        action_id = self._policy_identifier(action_id, "POLICY_ACTION_REQUIRED")
        if not self._valid_digest(input_hash) or not self._valid_digest(receipt_hash) or type(valid) is not bool:
            raise OwnershipRefused("POLICY_RECEIPT_INVALID")
        self.ensure_run_policy_budget_schema()
        with self.transaction() as tx:
            assert_owner(tx, token)
            row = tx.execute("SELECT * FROM authority_policy_actions WHERE id=? AND repository_id=? AND run_id=?",
                             (action_id, token.repository_id, token.run_id)).fetchone()
            if row is None or row["input_hash"] != input_hash:
                raise OwnershipRefused("POLICY_RECEIPT_BINDING_INVALID")
            # A malformed/rejected transport result is charged at launch, but
            # is not a review receipt and must leave the one retry available.
            if not valid:
                return self._policy_action_from_row(row, reused=True)
            if row["receipt_hash"] is not None:
                if row["receipt_hash"] != receipt_hash:
                    raise OwnershipRefused("POLICY_RECEIPT_CONFLICT")
                return self._policy_action_from_row(row, reused=True)
            tx.execute("UPDATE authority_policy_actions SET receipt_hash=?,state=?,updated_at=? WHERE id=?",
                       (receipt_hash, "completed_valid", self._now(), action_id))
            row = tx.execute("SELECT * FROM authority_policy_actions WHERE id=?", (action_id,)).fetchone()
            return self._policy_action_from_row(row)

    def record_policy_wait(self, token, *, kind: object, elapsed_ns: object,
                           clock_boot_id: object = None, clock_monotonic_ns: object = None) -> RunPolicyBudget:
        """Expose capacity/operator wait separately without advancing active time."""
        from .ownership import OwnershipRefused, assert_owner
        if kind not in {"capacity", "operator"} or isinstance(elapsed_ns, bool) or not isinstance(elapsed_ns, int) or elapsed_ns < 0:
            raise OwnershipRefused("POLICY_WAIT_INVALID")
        self.ensure_run_policy_budget_schema()
        clock_ok = True
        with self.transaction() as tx:
            assert_owner(tx, token)
            boot, monotonic = self._policy_sample(clock_boot_id, clock_monotonic_ns)
            row = tx.execute("SELECT * FROM authority_run_policy_budgets WHERE repository_id=? AND run_id=?",
                             (token.repository_id, token.run_id)).fetchone()
            if row is None:
                raise OwnershipRefused("RUN_POLICY_REQUIRED")
            # Waiting can coexist with productive siblings.  Therefore use
            # the same union-clock reconciliation, retaining active_after.
            clock_ok = self._policy_clock_tx(tx, token, boot_id=boot, monotonic_ns=monotonic,
                                             active_after=bool(row["clock_active"]),
                                             allow_idle_reanchor=clock_boot_id is None and clock_monotonic_ns is None)
            if not clock_ok:
                # Commit only the durable uncertainty marker. Waiting is not
                # credited and no launch/permit mutation is in this path.
                pass
            else:
                column = "capacity_wait_ns" if kind == "capacity" else "operator_wait_ns"
                tx.execute(f"UPDATE authority_run_policy_budgets SET {column}={column}+?,updated_at=? WHERE repository_id=? AND run_id=?",
                           (elapsed_ns, self._now(), token.repository_id, token.run_id))
            row = tx.execute("SELECT * FROM authority_run_policy_budgets WHERE repository_id=? AND run_id=?",
                             (token.repository_id, token.run_id)).fetchone()
        if not clock_ok:
            raise OwnershipRefused("CLOCK_RECONCILIATION_REQUIRED")
        return self._policy_budget_from_row(row)

    def _policy_bind_launch_tx(self, tx, token, *, action_id: str | None, intent_id: str,
                               request_key: str | None = None, input_hash: str | None = None,
                               expected_action: str | None = None) -> None:
        """Attach a charged existing launch intent to one durable action."""
        from .ownership import OwnershipRefused
        budget = tx.execute("SELECT * FROM authority_run_policy_budgets WHERE repository_id=? AND run_id=?",
                            (token.repository_id, token.run_id)).fetchone()
        if budget is None:
            return                         # legacy / unmanaged path is unchanged
        boot, sampled_ns = self._policy_sample()
        if budget["clock_boot_id"] != boot or sampled_ns < budget["clock_last_ns"]:
            raise OwnershipRefused("CLOCK_RECONCILIATION_REQUIRED")
        projected = budget["active_ns"] + (sampled_ns - budget["clock_last_ns"] if budget["clock_active"] else 0)
        if projected >= budget["active_limit_ns"]:
            raise OwnershipRefused("ACTIVE_TIME_EXHAUSTED")
        if budget["clock_uncertain"]:
            raise OwnershipRefused("CLOCK_RECONCILIATION_REQUIRED")
        if budget["active_ns"] >= budget["active_limit_ns"]:
            raise OwnershipRefused("ACTIVE_TIME_EXHAUSTED")
        if action_id is None:
            raise OwnershipRefused("POLICY_ACTION_REQUIRED")
        action = tx.execute("SELECT * FROM authority_policy_actions WHERE id=? AND repository_id=? AND run_id=?",
                            (action_id, token.repository_id, token.run_id)).fetchone()
        if action is None or action["state"] not in {"reserved", "dispatched"}:
            raise OwnershipRefused("POLICY_ACTION_REQUIRED")
        child = tx.execute(
            "SELECT b.role FROM authority_child_bindings b JOIN authority_launch_intents i ON i.activity_id=b.activity_id WHERE i.id=?",
            (intent_id,),
        ).fetchone()
        allowed_roles = {
            "worker": {"execute", "repair"},
            "reviewer": {"spec_review", "final_review", "check", "diagnosis"},
            "recovery": {"recovery_trial", "diagnosis"},
            "inventory": {"qualification", "check", "diagnosis"},
        }
        if child is not None and action["action"] not in allowed_roles.get(child["role"], set()):
            raise OwnershipRefused("POLICY_ACTION_BINDING_CONFLICT")
        if expected_action == "managed_outer":
            expected_action = "final_review" if child is not None and child["role"] == "reviewer" else "execute"
        if request_key is None:
            source = tx.execute("SELECT a.input_digest FROM authority_activities a JOIN authority_launch_intents i ON i.activity_id=a.id WHERE i.id=?",
                                (intent_id,)).fetchone()
            if source is None or action["input_hash"] != source["input_digest"]:
                raise OwnershipRefused("POLICY_ACTION_BINDING_CONFLICT")
        if (request_key is not None and (action["logical_key"] != request_key or action["input_hash"] != input_hash)
                or expected_action is not None and action["action"] != expected_action):
            raise OwnershipRefused("POLICY_ACTION_BINDING_CONFLICT")
        # Every logical request has at most one same-input transport retry.
        # A mutating repair/trial retry additionally consumes its named grant;
        # only reviews may retry under the original review grant.
        maximum = 2
        if action["transport_attempts"] >= maximum:
            raise OwnershipRefused("POLICY_TRANSPORT_LIMIT_EXHAUSTED")
        if action["transport_attempts"] and action["action"] in {"repair", "recovery_trial"}:
            from .run_policy import action_limit
            parameters = [token.repository_id, token.run_id, action["action"]]
            cycle_clause = ""
            if action["action"] == "recovery_trial":
                cycle_clause = " AND recovery_cycle=?"
                parameters.append(action["recovery_cycle"])
            used = tx.execute(
                "SELECT COALESCE(SUM(MAX(1,transport_attempts)),0) FROM authority_policy_actions "
                "WHERE repository_id=? AND run_id=? AND action=?" + cycle_clause, parameters,
            ).fetchone()[0]
            if used >= action_limit(action["action"], budget["tier"]):
                raise OwnershipRefused("POLICY_ACTION_LIMIT_EXHAUSTED")
        if budget["launch_charged"] >= budget["launch_limit"]:
            raise OwnershipRefused("POLICY_LAUNCH_LIMIT_EXHAUSTED")
        now = self._now()
        tx.execute("UPDATE authority_policy_actions SET intent_id=?,transport_attempts=transport_attempts+1,state='dispatched',updated_at=? WHERE id=? AND intent_id IS NULL",
                   (intent_id, now, action_id))
        # A retry needs a new intent but still belongs to the same logical
        # action. Preserve the first intent as the action's anchor; explicit
        # binding rows below retain every transport identity.
        if tx.execute("SELECT changes()").fetchone()[0] == 0:
            if action["intent_id"] is None:
                raise OwnershipRefused("POLICY_ACTION_BINDING_CONFLICT")
            tx.execute("UPDATE authority_policy_actions SET transport_attempts=transport_attempts+1,updated_at=? WHERE id=?",
                       (now, action_id))
            tx.execute("INSERT OR IGNORE INTO authority_policy_action_attempts(action_id,intent_id,created_at) VALUES(?,?,?)",
                       (action_id, intent_id, now))
        else:
            tx.execute("INSERT INTO authority_policy_action_attempts(action_id,intent_id,created_at) VALUES(?,?,?)",
                       (action_id, intent_id, now))
        tx.execute("UPDATE authority_run_policy_budgets SET launch_charged=launch_charged+1,updated_at=? WHERE repository_id=? AND run_id=?",
                   (now, token.repository_id, token.run_id))

    def _policy_sample(self, boot_id=None, monotonic_ns=None):
        """Sample production time while holding the authority transaction.

        Explicit samples and the injectable clock are deterministic test seams.
        Production callers omit both values, avoiding reordered caller samples.
        """
        from .ownership import OwnershipRefused
        if boot_id is None and monotonic_ns is None:
            if self.policy_clock is not None:
                boot_id, monotonic_ns = self.policy_clock()
            else:
                from process_identity import ProcessIdentity
                import time
                boot_id, monotonic_ns = ProcessIdentity.current().boot_id, time.monotonic_ns()
        elif boot_id is None or monotonic_ns is None:
            raise OwnershipRefused("POLICY_CLOCK_REQUIRED")
        return (self._policy_identifier(boot_id, "POLICY_CLOCK_INVALID"),
                self._policy_monotonic(monotonic_ns))

    def _preflight_policy_clock(self, token, *, clock_boot_id: str | None = None,
                                clock_monotonic_ns: int | None = None) -> None:
        """Persist a mismatch fence before an authorization can mutate state."""
        from .ownership import OwnershipRefused, assert_owner
        with self.read_transaction() as tx:
            if not self._run_policy_tables_present_tx(tx):
                return
            row = tx.execute("SELECT * FROM authority_run_policy_budgets WHERE repository_id=? AND run_id=?",
                             (token.repository_id, token.run_id)).fetchone()
        if row is None:
            return
        with self.transaction() as tx:
            assert_owner(tx, token)
            boot, monotonic = self._policy_sample(clock_boot_id, clock_monotonic_ns)
            current = tx.execute("SELECT * FROM authority_run_policy_budgets WHERE repository_id=? AND run_id=?",
                                 (token.repository_id, token.run_id)).fetchone()
            if current is None:
                return
            clock_ok = self._policy_clock_tx(
                tx, token, boot_id=boot, monotonic_ns=monotonic,
                active_after=bool(current["clock_active"]),
                allow_idle_reanchor=clock_boot_id is None and clock_monotonic_ns is None,
            )
            current = tx.execute("SELECT * FROM authority_run_policy_budgets WHERE repository_id=? AND run_id=?",
                                 (token.repository_id, token.run_id)).fetchone()
        if not clock_ok:
            raise OwnershipRefused("CLOCK_RECONCILIATION_REQUIRED")
        if current["active_ns"] >= current["active_limit_ns"]:
            raise OwnershipRefused("ACTIVE_TIME_EXHAUSTED")

    def _reconcile_dead_policy_work(self, token) -> None:
        """Conservatively close same-boot local work after proven owner death."""
        from process_identity import ProcessIdentity, probe_identity, DEAD
        from .ownership import OwnershipRefused, assert_owner, _token_keys
        with self.read_transaction() as tx:
            intervals = [dict(row) for row in tx.execute(
                "SELECT * FROM authority_policy_work_intervals WHERE repository_id=? AND run_id=? "
                "AND state='active' AND generation<>? ORDER BY id",
                (token.repository_id, token.run_id, token.generation),
            ).fetchall()]
            owners = []
            for generation in sorted({row["generation"] for row in intervals}):
                rows = tx.execute(
                    "SELECT host_id,boot_id,pid,start_token,held FROM control_reservations "
                    "WHERE resource_type='run' AND resource_key=? AND generation=?",
                    (_token_keys(token)[0][1], generation),
                ).fetchall()
                if len(rows) != 1 or rows[0]["held"]:
                    raise OwnershipRefused("POLICY_WORK_RECONCILIATION_REQUIRED")
                owners.append(ProcessIdentity(rows[0]["host_id"], rows[0]["boot_id"], rows[0]["pid"], rows[0]["start_token"]))
        if not intervals:
            return
        current_boot = ProcessIdentity.current().boot_id
        if (any(row["boot_id"] != current_boot for row in intervals)
                or any(owner.boot_id != current_boot or probe_identity(owner) != DEAD for owner in owners)):
            raise OwnershipRefused("POLICY_WORK_RECONCILIATION_REQUIRED")
        with self.transaction() as tx:
            assert_owner(tx, token)
            current = [dict(row) for row in tx.execute(
                "SELECT * FROM authority_policy_work_intervals WHERE repository_id=? AND run_id=? "
                "AND state='active' AND generation<>? ORDER BY id",
                (token.repository_id, token.run_id, token.generation),
            ).fetchall()]
            if current != intervals:
                raise OwnershipRefused("POLICY_WORK_RECONCILIATION_REQUIRED")
            boot, sample = self._policy_sample()
            clock_ok = self._policy_clock_tx(tx, token, boot_id=boot, monotonic_ns=sample, active_after=True)
            if clock_ok:
                for row in intervals:
                    tx.execute("UPDATE authority_policy_work_intervals SET state='completed',completed_ns=? WHERE id=?",
                               (sample, row["id"]))
                self._policy_clock_tx(tx, token, boot_id=boot, monotonic_ns=sample,
                                      active_after=self._policy_has_productive_work_tx(tx, token))
                self._record_acceptance_event_tx(tx, "policy_local_work_reconciled", {
                    "repository_id": token.repository_id, "run_id": token.run_id,
                    "settling_generation": token.generation, "interval_ids": [row["id"] for row in intervals],
                    "dead_owners": [dict(host_id=o.host_id, boot_id=o.boot_id, pid=o.pid, start_token=o.start_token) for o in owners],
                    "conservative_until_monotonic_ns": sample, "observed_wall_ns": time.time_ns(),
                })
        if not clock_ok:
            raise OwnershipRefused("CLOCK_RECONCILIATION_REQUIRED")

    def begin_policy_work(self, token, *, kind: str, clock_boot_id: str | None = None,
                          clock_monotonic_ns: int | None = None) -> str:
        """Charge supervisor-local work to the same durable union clock."""
        from .ownership import OwnershipRefused, assert_owner
        if kind not in {"preparation", "qualification", "harvest", "integration", "check", "recovery"}:
            raise OwnershipRefused("POLICY_WORK_KIND_INVALID")
        self._reconcile_dead_policy_work(token)
        self._preflight_policy_clock(
            token, clock_boot_id=clock_boot_id, clock_monotonic_ns=clock_monotonic_ns,
        )
        identifier = str(uuid.uuid4())
        with self.transaction() as tx:
            assert_owner(tx, token)
            clock_boot_id, clock_monotonic_ns = self._policy_sample(clock_boot_id, clock_monotonic_ns)
            self.assert_migration_epoch_tx(tx, token.run_id)
            budget = tx.execute("SELECT * FROM authority_run_policy_budgets WHERE repository_id=? AND run_id=?",
                                (token.repository_id, token.run_id)).fetchone()
            if budget is None:
                raise OwnershipRefused("RUN_POLICY_REQUIRED")
            prior = tx.execute(
                "SELECT 1 FROM authority_policy_work_intervals WHERE repository_id=? AND run_id=? "
                "AND state='active' AND generation<>? LIMIT 1",
                (token.repository_id, token.run_id, token.generation),
            ).fetchone()
            if prior is not None:
                raise OwnershipRefused("POLICY_WORK_RECONCILIATION_REQUIRED")
            clock_error = self._policy_authorize_clock_tx(tx, token, clock_boot_id, clock_monotonic_ns)
            clock_ok = clock_error is None
            if clock_ok:
                tx.execute("INSERT INTO authority_policy_work_intervals "
                           "(id,repository_id,run_id,generation,kind,state,started_ns,boot_id) "
                           "VALUES(?,?,?,?,?,'active',?,?)",
                           (identifier, token.repository_id, token.run_id, token.generation,
                            kind, clock_monotonic_ns, clock_boot_id))
        if not clock_ok:
            raise OwnershipRefused(clock_error)
        return identifier

    def end_policy_work(self, token, interval_id: str, *, clock_boot_id: str | None = None,
                        clock_monotonic_ns: int | None = None) -> None:
        """Close only this owner's interval; crash leftovers need reconciliation."""
        from .ownership import OwnershipRefused, assert_owner
        with self.transaction() as tx:
            assert_owner(tx, token)
            clock_boot_id, clock_monotonic_ns = self._policy_sample(clock_boot_id, clock_monotonic_ns)
            self.assert_migration_epoch_tx(tx, token.run_id)
            row = tx.execute("SELECT * FROM authority_policy_work_intervals WHERE id=?", (interval_id,)).fetchone()
            if row is None or (row["repository_id"], row["run_id"], row["generation"]) != (
                token.repository_id, token.run_id, token.generation,
            ):
                raise OwnershipRefused("POLICY_WORK_RECONCILIATION_REQUIRED")
            if row["state"] == "completed":
                return
            tx.execute("UPDATE authority_policy_work_intervals SET state='completed',completed_ns=? WHERE id=?",
                       (self._policy_monotonic(clock_monotonic_ns), interval_id))
            clock_ok = self._policy_clock_tx(
                tx, token, boot_id=self._policy_identifier(clock_boot_id, "POLICY_CLOCK_INVALID"),
                monotonic_ns=clock_monotonic_ns,
                active_after=self._policy_has_productive_work_tx(tx, token),
            )
        if not clock_ok:
            raise OwnershipRefused("CLOCK_RECONCILIATION_REQUIRED")

    def record_contained_process(self, token, *, intent_id: str, report: dict) -> None:
        """Stop productive time only for an evidenced, dead contained session.

        This does not settle token usage, release reservations, or authorize a
        retry. Those remain subject to ordinary launch settlement.
        """
        from process_identity import ProcessIdentity, probe_identity, DEAD
        from .ownership import OwnershipRefused
        if report.get("status") != "terminated" or report.get("unverified_members"):
            return
        try:
            identity = ProcessIdentity(**report["leader"])
            members = [ProcessIdentity(**value) for value in report["members"]]
        except (KeyError, TypeError, ValueError):
            raise OwnershipRefused("CONTAINMENT_EVIDENCE_INVALID") from None
        if identity not in members or any(probe_identity(member) != DEAD for member in members):
            return
        with self.transaction() as tx:
            row = tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (intent_id,)).fetchone()
            if row is None:
                raise OwnershipRefused("FENCE_REVOKED")
            self._assert_activity_binding(tx, token, row["activity_id"])
            if row["permit_id"] is not None or self._intent_from_row(row).child_identity != identity:
                raise OwnershipRefused("CONTAINMENT_EVIDENCE_INVALID")
            key = "policy-child-stopped:" + intent_id
            if tx.execute("SELECT 1 FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?",
                          (row["activity_id"], key)).fetchone() is None:
                self._record_event_once_tx(tx, token, row["activity_id"], key,
                                          {"intent_id": intent_id, "containment": report})
            if self._run_policy_tables_present_tx(tx):
                boot, sampled_ns = self._policy_sample()
                self._policy_clock_tx(tx, token, boot_id=boot, monotonic_ns=sampled_ns,
                                      active_after=self._policy_has_productive_work_tx(tx, token))

    @staticmethod
    def _policy_has_productive_work_tx(tx, token) -> bool:
        """Count authorized or unreconciled work; only reserved queues are idle."""
        local = tx.execute(
            "SELECT 1 FROM authority_policy_work_intervals WHERE repository_id=? AND run_id=? AND state='active' LIMIT 1",
            (token.repository_id, token.run_id),
        ).fetchone()
        return local is not None or tx.execute(
            "SELECT COUNT(*) FROM authority_launch_intents i JOIN authority_activities a ON a.id=i.activity_id "
            "WHERE a.repository_id=? AND a.run_id=? AND ("
            "i.state IN ('released_to_execute','uncertain','reconcile_required')) AND i.child_pid IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM authority_event_keys k WHERE k.activity_id=i.activity_id "
            "AND k.idempotency_key='policy-child-stopped:' || i.id)",
            (token.repository_id, token.run_id),
        ).fetchone()[0] > 0

    def _policy_authorize_clock_tx(self, tx, token, boot_id, monotonic_ns):
        boot, sample = self._policy_sample(boot_id, monotonic_ns)
        if not self._policy_clock_tx(tx, token, boot_id=boot, monotonic_ns=sample,
                                     active_after=self._policy_has_productive_work_tx(tx, token)):
            return "CLOCK_RECONCILIATION_REQUIRED"
        row = tx.execute("SELECT active_ns,active_limit_ns FROM authority_run_policy_budgets WHERE repository_id=? AND run_id=?",
                         (token.repository_id, token.run_id)).fetchone()
        if row["active_ns"] >= row["active_limit_ns"]:
            return "ACTIVE_TIME_EXHAUSTED"
        tx.execute("UPDATE authority_run_policy_budgets SET clock_active=1 WHERE repository_id=? AND run_id=?",
                   (token.repository_id, token.run_id))
        return None

    def _policy_clock_tx(self, tx, token, *, boot_id: str, monotonic_ns: int, active_after: bool,
                         allow_idle_reanchor: bool = False) -> bool:
        """Advance a union clock; time only advances while any child is active."""
        row = tx.execute("SELECT * FROM authority_run_policy_budgets WHERE repository_id=? AND run_id=?",
                         (token.repository_id, token.run_id)).fetchone()
        if row is None:
            return True
        if (allow_idle_reanchor and not row["clock_uncertain"] and row["clock_boot_id"] != boot_id
                and not row["clock_active"] and not self._policy_has_productive_work_tx(tx, token)
                and tx.execute(
                    "SELECT 1 FROM authority_launch_intents i JOIN authority_activities a ON a.id=i.activity_id "
                    "WHERE a.repository_id=? AND a.run_id=? AND i.state NOT IN ('completed_succeeded','completed_failed','closed_dead') LIMIT 1",
                    (token.repository_id, token.run_id),
                ).fetchone() is None):
            self._record_acceptance_event_tx(tx, "policy_idle_clock_reanchored", {
                "repository_id": token.repository_id, "run_id": token.run_id,
                "old_boot_id": row["clock_boot_id"], "old_monotonic_ns": row["clock_last_ns"],
                "old_wall_ns": row["clock_last_wall_ns"], "new_boot_id": boot_id,
                "new_monotonic_ns": monotonic_ns, "new_wall_ns": time.time_ns(),
            })
            tx.execute("UPDATE authority_run_policy_budgets SET clock_boot_id=?,clock_last_ns=?,clock_last_wall_ns=?,updated_at=? WHERE repository_id=? AND run_id=?",
                       (boot_id, monotonic_ns, time.time_ns(), self._now(), token.repository_id, token.run_id))
            return True
        if row["clock_uncertain"] or row["clock_boot_id"] != boot_id or monotonic_ns < row["clock_last_ns"]:
            tx.execute("UPDATE authority_run_policy_budgets SET clock_uncertain=1,updated_at=? WHERE repository_id=? AND run_id=?",
                       (self._now(), token.repository_id, token.run_id))
            return False
        elapsed = monotonic_ns - row["clock_last_ns"] if row["clock_active"] else 0
        active = row["active_ns"] + elapsed
        # Settlement retains elapsed time after exhaustion. Admission checks
        # the durable ceiling; rollback would hide work and block settlement.
        tx.execute("UPDATE authority_run_policy_budgets SET active_ns=?,clock_last_ns=?,clock_last_wall_ns=?,clock_active=?,updated_at=? WHERE repository_id=? AND run_id=?",
                   (active, monotonic_ns, time.time_ns(), int(active_after), self._now(), token.repository_id, token.run_id))
        return True

    @staticmethod
    def _acceptance_json(value: object, *, code: str) -> tuple[dict, str, str]:
        """Return an exact canonical JSON object and its digest, or refuse."""
        if not isinstance(value, dict) or not value:
            _refuse(code)
        try:
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            _refuse(code)
        return dict(value), encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _record_acceptance_event_tx(tx, event_type: str, payload: dict) -> None:
        tx.execute(
            "INSERT INTO control_events(event_type,payload) VALUES(?,?)",
            (event_type, json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)),
        )

    @staticmethod
    def _acceptance_identifier(value: object, *, code: str) -> str:
        if (
            not isinstance(value, str) or not value or value != value.strip()
            or len(value.encode("utf-8")) > 256 or not value.isprintable()
        ):
            _refuse(code)
        return value

    @classmethod
    def _acceptance_requirement_ids(
        cls, values: object, *, allow_empty: bool, code: str,
    ) -> tuple[str, ...]:
        if not isinstance(values, (list, tuple)):
            _refuse(code)
        identifiers = tuple(cls._acceptance_identifier(value, code=code) for value in values)
        if (not allow_empty and not identifiers) or len(set(identifiers)) != len(identifiers):
            _refuse(code)
        # Requirement order is not semantic.  Canonical ordering makes the
        # generation hash independent of caller serialization order.
        return tuple(sorted(identifiers))

    def _acceptance_binding_tx(self, tx, token) -> dict:
        """Read the frozen managed ingress material while the owner is live."""
        from .ownership import OwnershipRefused, assert_owner

        assert_owner(tx, token)
        self.assert_migration_epoch_tx(tx, token.run_id)
        self._validate_acceptance_contract_schema_tx(tx)
        row = tx.execute(
            "SELECT repository_id,run_id,objective_digest,input_digest,request_key,request_digest,"
            "writer_version FROM context_runs WHERE repository_id=? AND run_id=?",
            (token.repository_id, token.run_id),
        ).fetchone()
        if row is None or row["writer_version"] != _MANAGED_WRITER_VERSION:
            raise OwnershipRefused("MANAGED_ACCEPTANCE_REQUIRED")
        values = {
            "repository_id": row["repository_id"], "run_id": row["run_id"],
            "objective_digest": row["objective_digest"], "input_digest": row["input_digest"],
            "request_key": row["request_key"], "request_digest": row["request_digest"],
        }
        if (
            any(not isinstance(values[name], str) or not values[name] for name in values)
            or any(not self._valid_digest(values[name]) or values[name] != values[name].lower()
                   for name in ("objective_digest", "input_digest", "request_digest"))
        ):
            raise OwnershipRefused("ACCEPTANCE_MATERIAL_INVALID")
        request = tx.execute(
            "SELECT request_digest,run_id FROM context_requests WHERE repository_id=? AND request_key=?",
            (token.repository_id, values["request_key"]),
        ).fetchone()
        if request is None or request["run_id"] != token.run_id or request["request_digest"] != values["request_digest"]:
            raise OwnershipRefused("ACCEPTANCE_REQUEST_BINDING_REQUIRED")
        return values

    @classmethod
    def _validate_acceptance_binding_material(cls, material: object, *, code: str) -> dict:
        material, _encoded, _hash = cls._acceptance_json(material, code=code)
        fields = {
            "repository_id", "run_id", "objective_digest", "input_digest", "request_key", "request_digest",
        }
        if set(material) != fields:
            _refuse(code)
        cls._acceptance_identifier(material["repository_id"], code=code)
        cls._acceptance_identifier(material["run_id"], code=code)
        cls._acceptance_identifier(material["request_key"], code=code)
        if any(
            not cls._valid_digest(material[field]) or material[field] != material[field].lower()
            for field in ("objective_digest", "input_digest", "request_digest")
        ):
            _refuse(code)
        return material

    @classmethod
    def _acceptance_contract_hash(
        cls, *, generation: int, parent_contract_hash: str | None, material: dict,
        accepted_requirement_ids: tuple[str, ...], active_obligation_ids: tuple[str, ...],
        amendment_id: str | None, amendment: dict | None,
    ) -> str:
        body = {
            "schema": _ACCEPTANCE_CONTRACT_SCHEMA,
            "generation": generation,
            "parent_contract_hash": parent_contract_hash,
            "material": material,
            "accepted_requirement_ids": list(accepted_requirement_ids),
            "active_obligation_ids": list(active_obligation_ids),
            "amendment_id": amendment_id,
            "amendment": amendment,
        }
        _body, encoded, _digest = cls._acceptance_json(body, code="ACCEPTANCE_CONTRACT_CORRUPT")
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def _acceptance_contract_from_row(cls, row) -> AcceptanceContract:
        try:
            material = json.loads(row["material_json"])
            requirement_ids = cls._acceptance_requirement_ids(
                json.loads(row["accepted_requirement_ids_json"]), allow_empty=False,
                code="ACCEPTANCE_CONTRACT_CORRUPT",
            )
            active_ids = cls._acceptance_requirement_ids(
                json.loads(row["active_obligation_ids_json"]), allow_empty=True,
                code="ACCEPTANCE_CONTRACT_CORRUPT",
            )
            amendment = None if row["amendment_json"] is None else json.loads(row["amendment_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            _refuse("ACCEPTANCE_CONTRACT_CORRUPT")
        material, material_json, material_hash = cls._acceptance_json(
            material, code="ACCEPTANCE_CONTRACT_CORRUPT",
        )
        cls._validate_acceptance_binding_material(material, code="ACCEPTANCE_CONTRACT_CORRUPT")
        if (
            not isinstance(row["generation"], int) or row["generation"] < 1
            or not cls._valid_digest(row["contract_hash"])
            or not cls._valid_digest(row["material_hash"])
            or row["material_json"] != material_json or row["material_hash"] != material_hash
            or row["accepted_requirement_ids_json"] != json.dumps(list(requirement_ids), separators=(",", ":"))
            or row["active_obligation_ids_json"] != json.dumps(list(active_ids), separators=(",", ":"))
        ):
            _refuse("ACCEPTANCE_CONTRACT_CORRUPT")
        amendment_id = row["amendment_id"]
        if row["generation"] == 1:
            if row["parent_contract_hash"] is not None or amendment_id is not None or amendment is not None:
                _refuse("ACCEPTANCE_CONTRACT_CORRUPT")
        else:
            if (
                not cls._valid_digest(row["parent_contract_hash"])
                or not isinstance(amendment_id, str) or not amendment_id
                or amendment is None
            ):
                _refuse("ACCEPTANCE_CONTRACT_CORRUPT")
            cls._acceptance_identifier(amendment_id, code="ACCEPTANCE_CONTRACT_CORRUPT")
            cls._acceptance_json(amendment, code="ACCEPTANCE_CONTRACT_CORRUPT")
        expected = cls._acceptance_contract_hash(
            generation=row["generation"], parent_contract_hash=row["parent_contract_hash"],
            material=material, accepted_requirement_ids=requirement_ids,
            active_obligation_ids=active_ids, amendment_id=amendment_id, amendment=amendment,
        )
        if not secrets.compare_digest(expected, row["contract_hash"]):
            _refuse("ACCEPTANCE_CONTRACT_CORRUPT")
        return AcceptanceContract(
            row["repository_id"], row["run_id"], row["generation"], row["contract_hash"],
            row["parent_contract_hash"], material, requirement_ids, active_ids, amendment_id,
        )

    @classmethod
    def _acceptance_obligation_from_row(cls, row) -> AcceptanceObligation:
        try:
            material = json.loads(row["material_json"])
            requirement_ids = cls._acceptance_requirement_ids(
                json.loads(row["requirement_ids_json"]), allow_empty=True,
                code="ACCEPTANCE_OBLIGATION_CORRUPT",
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            _refuse("ACCEPTANCE_OBLIGATION_CORRUPT")
        material, material_json, material_hash = cls._acceptance_json(
            material, code="ACCEPTANCE_OBLIGATION_CORRUPT",
        )
        if (
            not isinstance(row["repository_id"], str) or not row["repository_id"]
            or not isinstance(row["run_id"], str) or not row["run_id"]
            or not isinstance(row["obligation_id"], str)
            or row["material_json"] != material_json or row["material_hash"] != material_hash
            or not cls._valid_digest(material_hash)
            or row["requirement_ids_json"] != json.dumps(list(requirement_ids), separators=(",", ":"))
            or row["status"] not in {"proposed", "active"}
            or not isinstance(row["created_generation"], int) or row["created_generation"] < 1
            or (row["status"] == "proposed" and (row["activated_generation"] is not None or row["activated_at"] is not None))
            or (row["status"] == "active" and (
                not isinstance(row["activated_generation"], int)
                or row["activated_generation"] <= row["created_generation"]
                or row["activated_at"] is None
            ))
        ):
            _refuse("ACCEPTANCE_OBLIGATION_CORRUPT")
        cls._acceptance_identifier(row["obligation_id"], code="ACCEPTANCE_OBLIGATION_CORRUPT")
        return AcceptanceObligation(
            row["repository_id"], row["run_id"], row["obligation_id"], material,
            requirement_ids, row["status"] == "active", row["created_generation"],
            row["activated_generation"],
        )

    def create_initial_acceptance_contract(
        self, token, *, accepted_requirement_ids: object, material: dict | None = None,
    ) -> AcceptanceContract:
        """Create the sole initial contract, bound to existing managed ingress.

        A caller may supply the expected material as a replay/conflict guard;
        it must exactly match the durable repository/run/objective/input/request
        binding. Omitting it still records that binding from the authority.
        """
        from .ownership import OwnershipRefused

        requirement_ids = self._acceptance_requirement_ids(
            accepted_requirement_ids, allow_empty=False, code="ACCEPTANCE_REQUIREMENTS_INVALID",
        )
        self.ensure_context_schema()
        self.ensure_acceptance_contract_schema()
        with self.transaction() as tx:
            bound = self._acceptance_binding_tx(tx, token)
            if material is not None:
                supplied, _encoded, _hash = self._acceptance_json(
                    material, code="ACCEPTANCE_MATERIAL_INVALID",
                )
                if supplied != bound:
                    raise OwnershipRefused("ACCEPTANCE_MATERIAL_CONFLICT")
            existing = tx.execute(
                "SELECT * FROM authority_acceptance_contracts WHERE repository_id=? AND run_id=? "
                "ORDER BY generation DESC LIMIT 1",
                (token.repository_id, token.run_id),
            ).fetchone()
            if existing is not None:
                # Validate before refusing so tampered/replayed rows cannot be
                # used to mask a corrupt acceptance history.
                self._acceptance_contract_from_row(existing)
                raise OwnershipRefused("ACCEPTANCE_INITIAL_REPLAY")
            material, material_json, material_hash = self._acceptance_json(
                bound, code="ACCEPTANCE_MATERIAL_INVALID",
            )
            contract_hash = self._acceptance_contract_hash(
                generation=1, parent_contract_hash=None, material=material,
                accepted_requirement_ids=requirement_ids, active_obligation_ids=(),
                amendment_id=None, amendment=None,
            )
            now = self._now()
            tx.execute(
                "INSERT INTO authority_acceptance_contracts "
                "(repository_id,run_id,generation,contract_hash,parent_contract_hash,material_json,material_hash,"
                "accepted_requirement_ids_json,active_obligation_ids_json,amendment_id,amendment_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (token.repository_id, token.run_id, 1, contract_hash, None, material_json, material_hash,
                 json.dumps(list(requirement_ids), separators=(",", ":")), "[]", None, None, now),
            )
            self._record_acceptance_event_tx(tx, "acceptance_contract_created", {
                "repository_id": token.repository_id, "run_id": token.run_id,
                "generation": 1, "contract_hash": contract_hash,
            })
            row = tx.execute(
                "SELECT * FROM authority_acceptance_contracts WHERE repository_id=? AND run_id=? AND generation=1",
                (token.repository_id, token.run_id),
            ).fetchone()
            return self._acceptance_contract_from_row(row)

    # The shorter verb is useful to managed ingress callers while retaining a
    # name that makes the one-time initialization property explicit.
    create_acceptance_contract = create_initial_acceptance_contract

    def append_proposed_acceptance_obligation(
        self, token, *, obligation_id: object, requirement_ids: object, material: object,
    ) -> AcceptanceObligation:
        """Append a non-blocking obligation; activation requires an amendment."""
        from .ownership import OwnershipRefused

        obligation_id = self._acceptance_identifier(
            obligation_id, code="ACCEPTANCE_OBLIGATION_ID_INVALID",
        )
        requirement_ids = self._acceptance_requirement_ids(
            requirement_ids, allow_empty=True, code="ACCEPTANCE_OBLIGATION_REQUIREMENTS_INVALID",
        )
        material, material_json, material_hash = self._acceptance_json(
            material, code="ACCEPTANCE_OBLIGATION_INVALID",
        )
        self.ensure_context_schema()
        self.ensure_acceptance_contract_schema()
        with self.transaction() as tx:
            self._acceptance_binding_tx(tx, token)
            current = tx.execute(
                "SELECT * FROM authority_acceptance_contracts WHERE repository_id=? AND run_id=? "
                "ORDER BY generation DESC LIMIT 1",
                (token.repository_id, token.run_id),
            ).fetchone()
            if current is None:
                raise OwnershipRefused("ACCEPTANCE_CONTRACT_REQUIRED")
            contract = self._acceptance_contract_from_row(current)
            existing = tx.execute(
                "SELECT * FROM authority_acceptance_obligations WHERE repository_id=? AND run_id=? AND obligation_id=?",
                (token.repository_id, token.run_id, obligation_id),
            ).fetchone()
            if existing is not None:
                prior = self._acceptance_obligation_from_row(existing)
                if prior.material != material or prior.requirement_ids != requirement_ids:
                    raise OwnershipRefused("ACCEPTANCE_OBLIGATION_CONFLICT")
                raise OwnershipRefused("ACCEPTANCE_OBLIGATION_REPLAY")
            now = self._now()
            tx.execute(
                "INSERT INTO authority_acceptance_obligations "
                "(repository_id,run_id,obligation_id,material_json,material_hash,requirement_ids_json,status,"
                "created_generation,activated_generation,created_at,activated_at) VALUES(?,?,?,?,?,?,? ,?,?,?,?)",
                (token.repository_id, token.run_id, obligation_id, material_json, material_hash,
                 json.dumps(list(requirement_ids), separators=(",", ":")), "proposed", contract.generation,
                 None, now, None),
            )
            self._record_acceptance_event_tx(tx, "acceptance_obligation_proposed", {
                "repository_id": token.repository_id, "run_id": token.run_id,
                "obligation_id": obligation_id, "contract_hash": contract.contract_hash,
            })
            row = tx.execute(
                "SELECT * FROM authority_acceptance_obligations WHERE repository_id=? AND run_id=? AND obligation_id=?",
                (token.repository_id, token.run_id, obligation_id),
            ).fetchone()
            return self._acceptance_obligation_from_row(row)

    append_proposed_obligation = append_proposed_acceptance_obligation

    def _consume_acceptance_amendment_grant_tx(
        self, tx, token, *, grant_id: object, contract_hash: str, amendment_id: str,
    ) -> None:
        """Consume the one explicit operator grant inside the amendment tx."""
        from .ownership import OwnershipRefused

        if not isinstance(grant_id, str) or not grant_id:
            raise OwnershipRefused("ACCEPTANCE_AMENDMENT_GRANT_REQUIRED")
        row = tx.execute(
            "SELECT * FROM authority_grants WHERE id=?", (grant_id,),
        ).fetchone()
        if row is None:
            raise OwnershipRefused("ACCEPTANCE_AMENDMENT_GRANT_REQUIRED")
        if row["repository_id"] != token.repository_id or row["run_id"] != token.run_id:
            raise OwnershipRefused("FENCE_REVOKED")
        if row["generation"] != token.generation:
            raise OwnershipRefused("GRANT_GENERATION_MISMATCH")
        now = _parse_utc_timestamp(self._now())
        expiry = _parse_utc_timestamp(row["expires_at"])
        if now is None or expiry is None or expiry <= now:
            raise OwnershipRefused("GRANT_EXPIRED")
        consume_key = f"acceptance-amend:{amendment_id}"
        if row["consumed"]:
            if row["consume_key"] == consume_key:
                raise OwnershipRefused("ACCEPTANCE_AMENDMENT_GRANT_REPLAY")
            raise OwnershipRefused("IDEMPOTENCY_CONFLICT")
        if row["action"] != "acceptance-amend" or row["target"] != contract_hash:
            raise OwnershipRefused("GRANT_MISMATCH")
        changed = tx.execute(
            "UPDATE authority_grants SET consumed=1,consume_key=? WHERE id=? AND consumed=0",
            (consume_key, grant_id),
        ).rowcount
        if changed != 1:
            raise OwnershipRefused("ACCEPTANCE_AMENDMENT_GRANT_REPLAY")

    def amend_acceptance_contract(
        self, token, *, amendment_id: object, amendment: object, grant_id: object,
        activate_obligation_ids: object = (),
    ) -> AcceptanceContract:
        """Record an operator amendment and atomically activate eligible proposals.

        The amendment never updates run limits or any accounting row. Its only
        state transition is a proposed obligation becoming active in the new
        contract generation after every referenced accepted requirement has
        been proven part of the frozen initial requirement set.
        """
        from .ownership import OwnershipRefused

        amendment_id = self._acceptance_identifier(amendment_id, code="ACCEPTANCE_AMENDMENT_ID_INVALID")
        amendment, amendment_json, _amendment_hash = self._acceptance_json(
            amendment, code="ACCEPTANCE_AMENDMENT_INVALID",
        )
        requested_ids = self._acceptance_requirement_ids(
            activate_obligation_ids, allow_empty=True, code="ACCEPTANCE_ACTIVATION_INVALID",
        )
        self.ensure_context_schema()
        self.ensure_acceptance_contract_schema()
        with self.transaction() as tx:
            bound = self._acceptance_binding_tx(tx, token)
            current_row = tx.execute(
                "SELECT * FROM authority_acceptance_contracts WHERE repository_id=? AND run_id=? "
                "ORDER BY generation DESC LIMIT 1",
                (token.repository_id, token.run_id),
            ).fetchone()
            if current_row is None:
                raise OwnershipRefused("ACCEPTANCE_CONTRACT_REQUIRED")
            current = self._acceptance_contract_from_row(current_row)
            if current.material != bound:
                raise OwnershipRefused("ACCEPTANCE_MATERIAL_CONFLICT")
            replay = tx.execute(
                "SELECT * FROM authority_acceptance_contracts WHERE repository_id=? AND run_id=? AND amendment_id=?",
                (token.repository_id, token.run_id, amendment_id),
            ).fetchone()
            if replay is not None:
                prior = self._acceptance_contract_from_row(replay)
                if prior.generation > 1 and prior.amendment_id == amendment_id:
                    raise OwnershipRefused("ACCEPTANCE_AMENDMENT_REPLAY")
                _refuse("ACCEPTANCE_CONTRACT_CORRUPT")
            if current.generation >= 9_223_372_036_854_775_807:
                raise OwnershipRefused("ACCEPTANCE_GENERATION_EXHAUSTED")
            # This is deliberately before every contract/proposal write. A
            # failed grant leaves the entire acceptance state untouched.
            self._consume_acceptance_amendment_grant_tx(
                tx, token, grant_id=grant_id, contract_hash=current.contract_hash,
                amendment_id=amendment_id,
            )
            active_ids = set(current.active_obligation_ids)
            if set(requested_ids) & active_ids:
                raise OwnershipRefused("ACCEPTANCE_ACTIVATION_REPLAY")
            proposals: list[AcceptanceObligation] = []
            for obligation_id in requested_ids:
                row = tx.execute(
                    "SELECT * FROM authority_acceptance_obligations WHERE repository_id=? AND run_id=? AND obligation_id=?",
                    (token.repository_id, token.run_id, obligation_id),
                ).fetchone()
                if row is None:
                    raise OwnershipRefused("ACCEPTANCE_OBLIGATION_UNKNOWN")
                proposal = self._acceptance_obligation_from_row(row)
                if proposal.active:
                    raise OwnershipRefused("ACCEPTANCE_ACTIVATION_REPLAY")
                if not proposal.requirement_ids or not set(proposal.requirement_ids).issubset(
                    set(current.accepted_requirement_ids),
                ):
                    raise OwnershipRefused("ACCEPTANCE_REQUIREMENT_REFERENCE_INVALID")
                proposals.append(proposal)
            generation = current.generation + 1
            all_active = tuple(sorted(active_ids | set(requested_ids)))
            material, material_json, material_hash = self._acceptance_json(
                current.material, code="ACCEPTANCE_CONTRACT_CORRUPT",
            )
            contract_hash = self._acceptance_contract_hash(
                generation=generation, parent_contract_hash=current.contract_hash, material=material,
                accepted_requirement_ids=current.accepted_requirement_ids,
                active_obligation_ids=all_active, amendment_id=amendment_id, amendment=amendment,
            )
            now = self._now()
            tx.execute(
                "INSERT INTO authority_acceptance_contracts "
                "(repository_id,run_id,generation,contract_hash,parent_contract_hash,material_json,material_hash,"
                "accepted_requirement_ids_json,active_obligation_ids_json,amendment_id,amendment_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (token.repository_id, token.run_id, generation, contract_hash, current.contract_hash,
                 material_json, material_hash, json.dumps(list(current.accepted_requirement_ids), separators=(",", ":")),
                 json.dumps(list(all_active), separators=(",", ":")), amendment_id, amendment_json, now),
            )
            for proposal in proposals:
                changed = tx.execute(
                    "UPDATE authority_acceptance_obligations SET status='active',activated_generation=?,activated_at=? "
                    "WHERE repository_id=? AND run_id=? AND obligation_id=? AND status='proposed'",
                    (generation, now, token.repository_id, token.run_id, proposal.obligation_id),
                ).rowcount
                if changed != 1:
                    raise OwnershipRefused("ACCEPTANCE_ACTIVATION_REPLAY")
            self._record_acceptance_event_tx(tx, "acceptance_contract_amended", {
                "repository_id": token.repository_id, "run_id": token.run_id,
                "generation": generation, "contract_hash": contract_hash,
                "parent_contract_hash": current.contract_hash,
                "activated_obligation_ids": list(requested_ids),
            })
            row = tx.execute(
                "SELECT * FROM authority_acceptance_contracts WHERE repository_id=? AND run_id=? AND generation=?",
                (token.repository_id, token.run_id, generation),
            ).fetchone()
            return self._acceptance_contract_from_row(row)

    def get_acceptance_contract(
        self, *, repository_id: str, run_id: str, generation: int | None = None,
    ) -> AcceptanceContract | None:
        """Read a verified acceptance generation without mutating legacy stores."""
        if not isinstance(repository_id, str) or not repository_id or not isinstance(run_id, str) or not run_id:
            _refuse("ACCEPTANCE_LOOKUP_INVALID")
        with self.read_transaction() as tx:
            if not self._acceptance_tables_present_tx(tx):
                return None
            self._validate_acceptance_contract_schema_tx(tx)
            if generation is not None and (isinstance(generation, bool) or not isinstance(generation, int) or generation < 1):
                _refuse("ACCEPTANCE_LOOKUP_INVALID")
            query = (
                "SELECT * FROM authority_acceptance_contracts WHERE repository_id=? AND run_id=? "
                + ("AND generation=?" if generation is not None else "ORDER BY generation DESC LIMIT 1")
            )
            parameters = (repository_id, run_id, generation) if generation is not None else (repository_id, run_id)
            row = tx.execute(query, parameters).fetchone()
            if row is None:
                return None
            contract = self._acceptance_contract_from_row(row)
            initial = tx.execute(
                "SELECT * FROM authority_acceptance_contracts WHERE repository_id=? AND run_id=? AND generation=1",
                (repository_id, run_id),
            ).fetchone()
            if initial is None:
                _refuse("ACCEPTANCE_CONTRACT_CORRUPT")
            frozen = self._acceptance_contract_from_row(initial)
            if contract.material != frozen.material or contract.accepted_requirement_ids != frozen.accepted_requirement_ids:
                _refuse("ACCEPTANCE_CONTRACT_CORRUPT")
            if contract.generation > 1:
                parent = tx.execute(
                    "SELECT contract_hash FROM authority_acceptance_contracts WHERE repository_id=? AND run_id=? AND generation=?",
                    (repository_id, run_id, contract.generation - 1),
                ).fetchone()
                if parent is None or parent["contract_hash"] != contract.parent_contract_hash:
                    _refuse("ACCEPTANCE_CONTRACT_CORRUPT")
            if generation is None:
                obligations = tuple(self._acceptance_obligation_from_row(row) for row in tx.execute(
                    "SELECT * FROM authority_acceptance_obligations WHERE repository_id=? AND run_id=?",
                    (repository_id, run_id),
                ))
                active_ids = tuple(sorted(item.obligation_id for item in obligations if item.active))
                if (
                    active_ids != contract.active_obligation_ids
                    or any(
                        not item.requirement_ids
                        or not set(item.requirement_ids).issubset(set(frozen.accepted_requirement_ids))
                        for item in obligations if item.active
                    )
                ):
                    _refuse("ACCEPTANCE_CONTRACT_CORRUPT")
            return contract

    def list_acceptance_obligations(
        self, *, repository_id: str, run_id: str,
    ) -> tuple[AcceptanceObligation, ...]:
        """Read verified proposals/active blockers for a run without activation side effects."""
        if not isinstance(repository_id, str) or not repository_id or not isinstance(run_id, str) or not run_id:
            _refuse("ACCEPTANCE_LOOKUP_INVALID")
        with self.read_transaction() as tx:
            if not self._acceptance_tables_present_tx(tx):
                return ()
            self._validate_acceptance_contract_schema_tx(tx)
            rows = tx.execute(
                "SELECT * FROM authority_acceptance_obligations WHERE repository_id=? AND run_id=? ORDER BY obligation_id",
                (repository_id, run_id),
            ).fetchall()
            obligations = tuple(self._acceptance_obligation_from_row(row) for row in rows)
            contract_row = tx.execute(
                "SELECT * FROM authority_acceptance_contracts WHERE repository_id=? AND run_id=? "
                "ORDER BY generation DESC LIMIT 1",
                (repository_id, run_id),
            ).fetchone()
            if contract_row is None:
                if obligations:
                    _refuse("ACCEPTANCE_CONTRACT_CORRUPT")
                return ()
            contract = self._acceptance_contract_from_row(contract_row)
            if tuple(sorted(item.obligation_id for item in obligations if item.active)) != contract.active_obligation_ids:
                _refuse("ACCEPTANCE_CONTRACT_CORRUPT")
            return obligations

    @staticmethod
    def _acceptance_policy_hash(*, kind: str, body: dict) -> str:
        encoded = json.dumps(
            {"schema": "ffs.acceptance-policy-storage/v1", "kind": kind, "body": body},
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def _acceptance_draft_from_row(cls, row, *, reused: bool = False) -> AcceptanceDraft:
        from .run_policy import RunPolicyRefused, validate_draft_material

        try:
            draft = validate_draft_material(json.loads(row["material_json"]))
        except (RunPolicyRefused, TypeError, ValueError, json.JSONDecodeError) as error:
            _refuse(getattr(error, "code", "ACCEPTANCE_POLICY_DRAFT_CORRUPT"))
        if (
            not isinstance(row["revision"], int) or row["revision"] < 1
            or not isinstance(row["legacy_generation"], int) or row["legacy_generation"] < 1
            or not cls._valid_digest(row["legacy_contract_hash"])
            or not cls._valid_digest(row["draft_hash"])
            or row["material_json"] != draft.material_json or row["material_hash"] != draft.material_hash
            or not cls._valid_digest(row["material_hash"])
        ):
            _refuse("ACCEPTANCE_POLICY_DRAFT_CORRUPT")
        cls._acceptance_identifier(row["draft_id"], code="ACCEPTANCE_POLICY_DRAFT_CORRUPT")
        expected = cls._acceptance_policy_hash(kind="draft", body={
            "repository_id": row["repository_id"], "run_id": row["run_id"],
            "draft_id": row["draft_id"], "revision": row["revision"],
            "legacy_generation": row["legacy_generation"],
            "legacy_contract_hash": row["legacy_contract_hash"],
            "material_hash": draft.material_hash,
        })
        if not secrets.compare_digest(expected, row["draft_hash"]):
            _refuse("ACCEPTANCE_POLICY_DRAFT_CORRUPT")
        return AcceptanceDraft(
            row["repository_id"], row["run_id"], row["draft_id"], row["revision"],
            row["legacy_generation"], row["legacy_contract_hash"], row["draft_hash"],
            draft.material, reused,
        )

    @classmethod
    def _sealed_acceptance_from_row(cls, row, *, reused: bool = False) -> SealedAcceptance:
        from .run_policy import RunPolicyRefused, validate_draft_material

        try:
            draft = validate_draft_material(json.loads(row["material_json"]))
        except (RunPolicyRefused, TypeError, ValueError, json.JSONDecodeError) as error:
            _refuse(getattr(error, "code", "ACCEPTANCE_SEAL_CORRUPT"))
        if (
            not isinstance(row["acceptance_generation"], int) or row["acceptance_generation"] < 1
            or not isinstance(row["draft_revision"], int) or row["draft_revision"] < 1
            or not isinstance(row["legacy_generation"], int) or row["legacy_generation"] < 1
            or any(not cls._valid_digest(row[name]) for name in (
                "draft_hash", "legacy_contract_hash", "acceptance_hash", "material_hash",
            ))
            or row["material_json"] != draft.material_json or row["material_hash"] != draft.material_hash
        ):
            _refuse("ACCEPTANCE_SEAL_CORRUPT")
        cls._acceptance_identifier(row["draft_id"], code="ACCEPTANCE_SEAL_CORRUPT")
        expected = cls._acceptance_policy_hash(kind="seal", body={
            "repository_id": row["repository_id"], "run_id": row["run_id"],
            "acceptance_generation": row["acceptance_generation"], "draft_id": row["draft_id"],
            "draft_revision": row["draft_revision"], "draft_hash": row["draft_hash"],
            "legacy_generation": row["legacy_generation"],
            "legacy_contract_hash": row["legacy_contract_hash"], "material_hash": draft.material_hash,
        })
        if not secrets.compare_digest(expected, row["acceptance_hash"]):
            _refuse("ACCEPTANCE_SEAL_CORRUPT")
        return SealedAcceptance(
            row["repository_id"], row["run_id"], row["acceptance_generation"], row["draft_id"],
            row["draft_revision"], row["draft_hash"], row["legacy_generation"],
            row["legacy_contract_hash"], row["acceptance_hash"], draft.material, reused,
        )

    def _current_acceptance_contract_tx(self, tx, token) -> AcceptanceContract:
        from .ownership import OwnershipRefused

        row = tx.execute(
            "SELECT * FROM authority_acceptance_contracts WHERE repository_id=? AND run_id=? "
            "ORDER BY generation DESC LIMIT 1", (token.repository_id, token.run_id),
        ).fetchone()
        if row is None:
            raise OwnershipRefused("ACCEPTANCE_CONTRACT_REQUIRED")
        return self._acceptance_contract_from_row(row)

    def _validate_sealed_draft_binding_tx(self, tx, sealed: SealedAcceptance) -> None:
        """Ensure a seal is a seal of its exact persisted draft, not a copy."""
        draft_row = tx.execute(
            "SELECT * FROM authority_acceptance_drafts WHERE repository_id=? AND run_id=? "
            "AND draft_id=? AND revision=?",
            (sealed.repository_id, sealed.run_id, sealed.draft_id, sealed.draft_revision),
        ).fetchone()
        if draft_row is None:
            _refuse("ACCEPTANCE_SEAL_CORRUPT")
        draft = self._acceptance_draft_from_row(draft_row)
        if (
            draft.draft_hash != sealed.draft_hash or draft.material != sealed.material
            or draft.acceptance_generation != sealed.legacy_generation
            or draft.acceptance_contract_hash != sealed.legacy_contract_hash
        ):
            _refuse("ACCEPTANCE_SEAL_CORRUPT")

    @staticmethod
    def _refuse_policy_error(error) -> None:
        _refuse(getattr(error, "code", "ACCEPTANCE_POLICY_INVALID"))

    def create_acceptance_draft(
        self, token, *, draft_id: object, revision: object,
        acceptance_contract_hash: object, material: object,
    ) -> AcceptanceDraft:
        """Persist one replay-safe executable draft for the current adapter generation.

        The legacy acceptance adapter remains the sole authority for which
        requirement IDs exist.  This method only supplies the immutable check,
        evidence, exclusion, invariant, runtime and candidate details needed
        to seal those IDs.
        """
        from .ownership import OwnershipRefused
        from .run_policy import RunPolicyRefused, validate_draft_material

        draft_id = self._acceptance_identifier(draft_id, code="ACCEPTANCE_DRAFT_ID_INVALID")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            _refuse("ACCEPTANCE_DRAFT_REVISION_INVALID")
        if not self._valid_digest(acceptance_contract_hash):
            _refuse("ACCEPTANCE_DRAFT_BINDING_INVALID")
        try:
            draft = validate_draft_material(material)
        except RunPolicyRefused as error:
            self._refuse_policy_error(error)
        self.ensure_context_schema()
        self.ensure_acceptance_policy_schema()
        with self.transaction() as tx:
            bound = self._acceptance_binding_tx(tx, token)
            current = self._current_acceptance_contract_tx(tx, token)
            if not secrets.compare_digest(current.contract_hash, acceptance_contract_hash):
                raise OwnershipRefused("ACCEPTANCE_DRAFT_BINDING_INVALID")
            if (
                draft.material["objective_digest"] != bound["objective_digest"]
                or draft.material["generation"] != current.generation
                or tuple(item["id"] for item in draft.material["criteria"])
                != current.accepted_requirement_ids
            ):
                raise OwnershipRefused("ACCEPTANCE_DRAFT_SCOPE_INVALID")
            expected_hash = self._acceptance_policy_hash(kind="draft", body={
                "repository_id": token.repository_id, "run_id": token.run_id,
                "draft_id": draft_id, "revision": revision,
                "legacy_generation": current.generation,
                "legacy_contract_hash": current.contract_hash, "material_hash": draft.material_hash,
            })
            existing = tx.execute(
                "SELECT * FROM authority_acceptance_drafts WHERE repository_id=? AND run_id=? "
                "AND draft_id=? AND revision=?",
                (token.repository_id, token.run_id, draft_id, revision),
            ).fetchone()
            if existing is not None:
                prior = self._acceptance_draft_from_row(existing, reused=True)
                if prior.draft_hash != expected_hash:
                    raise OwnershipRefused("ACCEPTANCE_DRAFT_CONFLICT")
                return prior
            now = self._now()
            tx.execute(
                "INSERT INTO authority_acceptance_drafts "
                "(repository_id,run_id,draft_id,revision,legacy_generation,legacy_contract_hash,draft_hash,"
                "material_json,material_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (token.repository_id, token.run_id, draft_id, revision, current.generation,
                 current.contract_hash, expected_hash, draft.material_json, draft.material_hash, now),
            )
            self._record_acceptance_event_tx(tx, "acceptance_draft_created", {
                "repository_id": token.repository_id, "run_id": token.run_id,
                "draft_id": draft_id, "revision": revision, "draft_hash": expected_hash,
                "legacy_contract_hash": current.contract_hash,
            })
            return AcceptanceDraft(
                token.repository_id, token.run_id, draft_id, revision, current.generation,
                current.contract_hash, expected_hash, draft.material,
            )

    def seal_acceptance_draft(
        self, token, *, draft_id: object, revision: object,
        acceptance_contract_hash: object,
    ) -> SealedAcceptance:
        """Seal a current draft exactly once, returning the same typed seal on replay."""
        from .ownership import OwnershipRefused

        draft_id = self._acceptance_identifier(draft_id, code="ACCEPTANCE_DRAFT_ID_INVALID")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            _refuse("ACCEPTANCE_DRAFT_REVISION_INVALID")
        if not self._valid_digest(acceptance_contract_hash):
            _refuse("ACCEPTANCE_SEAL_BINDING_INVALID")
        self.ensure_context_schema()
        self.ensure_acceptance_policy_schema()
        with self.transaction() as tx:
            self._acceptance_binding_tx(tx, token)
            draft_row = tx.execute(
                "SELECT * FROM authority_acceptance_drafts WHERE repository_id=? AND run_id=? "
                "AND draft_id=? AND revision=?",
                (token.repository_id, token.run_id, draft_id, revision),
            ).fetchone()
            if draft_row is None:
                raise OwnershipRefused("ACCEPTANCE_DRAFT_REQUIRED")
            draft = self._acceptance_draft_from_row(draft_row)
            # A replay is valid only for the exact legacy acceptance hash to
            # which its draft was bound. It remains readable after a later
            # operator amendment, but a caller cannot bypass that binding by
            # substituting a hash merely because this draft was already sealed.
            if not secrets.compare_digest(draft.acceptance_contract_hash, acceptance_contract_hash):
                raise OwnershipRefused("ACCEPTANCE_SEAL_BINDING_INVALID")
            replay = tx.execute(
                "SELECT * FROM authority_sealed_acceptances WHERE repository_id=? AND run_id=? AND draft_hash=?",
                (token.repository_id, token.run_id, draft.draft_hash),
            ).fetchone()
            if replay is not None:
                return self._sealed_acceptance_from_row(replay, reused=True)
            current = self._current_acceptance_contract_tx(tx, token)
            if (
                draft.acceptance_generation != current.generation
                or not secrets.compare_digest(draft.acceptance_contract_hash, current.contract_hash)
            ):
                raise OwnershipRefused("ACCEPTANCE_SEAL_STALE_DRAFT")
            sealed_generation = tx.execute(
                "SELECT 1 FROM authority_sealed_acceptances WHERE repository_id=? AND run_id=? "
                "AND legacy_generation=?",
                (token.repository_id, token.run_id, current.generation),
            ).fetchone()
            if sealed_generation is not None:
                raise OwnershipRefused("ACCEPTANCE_SEAL_GENERATION_CONFLICT")
            row = tx.execute(
                "SELECT COALESCE(MAX(acceptance_generation),0) AS generation "
                "FROM authority_sealed_acceptances WHERE repository_id=? AND run_id=?",
                (token.repository_id, token.run_id),
            ).fetchone()
            generation = row["generation"] + 1
            acceptance_hash = self._acceptance_policy_hash(kind="seal", body={
                "repository_id": token.repository_id, "run_id": token.run_id,
                "acceptance_generation": generation, "draft_id": draft.draft_id,
                "draft_revision": draft.revision, "draft_hash": draft.draft_hash,
                "legacy_generation": current.generation,
                "legacy_contract_hash": current.contract_hash,
                "material_hash": hashlib.sha256(json.dumps(
                    draft.material, sort_keys=True, separators=(",", ":"), allow_nan=False,
                ).encode("utf-8")).hexdigest(),
            })
            material_json = json.dumps(draft.material, sort_keys=True, separators=(",", ":"), allow_nan=False)
            material_hash = hashlib.sha256(material_json.encode("utf-8")).hexdigest()
            now = self._now()
            tx.execute(
                "INSERT INTO authority_sealed_acceptances "
                "(repository_id,run_id,acceptance_generation,draft_id,draft_revision,draft_hash,"
                "legacy_generation,legacy_contract_hash,acceptance_hash,material_json,material_hash,sealed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (token.repository_id, token.run_id, generation, draft.draft_id, draft.revision,
                 draft.draft_hash, current.generation, current.contract_hash, acceptance_hash,
                 material_json, material_hash, now),
            )
            self._record_acceptance_event_tx(tx, "acceptance_draft_sealed", {
                "repository_id": token.repository_id, "run_id": token.run_id,
                "acceptance_generation": generation, "acceptance_hash": acceptance_hash,
                "draft_hash": draft.draft_hash, "legacy_contract_hash": current.contract_hash,
            })
            return SealedAcceptance(
                token.repository_id, token.run_id, generation, draft.draft_id, draft.revision,
                draft.draft_hash, current.generation, current.contract_hash, acceptance_hash,
                draft.material,
            )

    def get_sealed_acceptance(
        self, *, repository_id: str, run_id: str, acceptance_generation: int | None = None,
    ) -> SealedAcceptance | None:
        """Read a validated E3 seal without creating schemas for legacy stores."""
        if not isinstance(repository_id, str) or not repository_id or not isinstance(run_id, str) or not run_id:
            _refuse("ACCEPTANCE_LOOKUP_INVALID")
        if acceptance_generation is not None and (
            isinstance(acceptance_generation, bool) or not isinstance(acceptance_generation, int)
            or acceptance_generation < 1
        ):
            _refuse("ACCEPTANCE_LOOKUP_INVALID")
        with self.read_transaction() as tx:
            if not self._acceptance_policy_tables_present_tx(tx):
                return None
            self._validate_acceptance_policy_schema_tx(tx)
            query = (
                "SELECT * FROM authority_sealed_acceptances WHERE repository_id=? AND run_id=? "
                + ("AND acceptance_generation=?" if acceptance_generation is not None
                   else "ORDER BY acceptance_generation DESC LIMIT 1")
            )
            args = (repository_id, run_id, acceptance_generation) if acceptance_generation is not None else (repository_id, run_id)
            row = tx.execute(query, args).fetchone()
            if row is None:
                return None
            sealed = self._sealed_acceptance_from_row(row)
            self._validate_sealed_draft_binding_tx(tx, sealed)
            return sealed

    def initialize_frontend_policy(self, token, *, acceptance_hash: object,
                                   stage: object = "SEALED") -> FrontendPolicyState:
        """Create the restart-safe E6 lifecycle projection for one seal.

        No caller supplies a candidate, generation, or budget here: all three
        are read from the already sealed contract and existing ledger.
        """
        from .ownership import OwnershipRefused, assert_owner
        # A seal is not evidence of execution.  Initialization has one safe
        # starting state; all other stages arise from guarded transitions.
        if not self._valid_digest(acceptance_hash) or stage != "SEALED":
            raise OwnershipRefused("FRONTEND_POLICY_BINDING_INVALID")
        self.ensure_frontend_policy_schema()
        with self.transaction() as tx:
            assert_owner(tx, token)
            sealed_row = tx.execute(
                "SELECT * FROM authority_sealed_acceptances WHERE repository_id=? AND run_id=? AND acceptance_hash=?",
                (token.repository_id, token.run_id, acceptance_hash),
            ).fetchone()
            if sealed_row is None:
                raise OwnershipRefused("ACCEPTANCE_SEAL_REQUIRED")
            sealed = self._sealed_acceptance_from_row(sealed_row)
            self._validate_sealed_draft_binding_tx(tx, sealed)
            existing = tx.execute(
                "SELECT * FROM authority_frontend_policy_states WHERE repository_id=? AND run_id=?",
                (token.repository_id, token.run_id),
            ).fetchone()
            if existing is not None:
                if (existing["acceptance_hash"], existing["generation"]) != (
                    acceptance_hash, sealed.legacy_generation,
                ):
                    raise OwnershipRefused("FRONTEND_POLICY_BINDING_CONFLICT")
                return FrontendPolicyState(token.repository_id, token.run_id, existing["acceptance_hash"],
                                           existing["stage"], existing["candidate_hash"], existing["generation"],
                                           json.loads(existing["decision_json"]) if existing["decision_json"] else None)
            tx.execute(
                "INSERT INTO authority_frontend_policy_states "
                "(repository_id,run_id,acceptance_hash,stage,candidate_hash,generation,decision_json,updated_at) "
                "VALUES(?,?,?,?,?,?,NULL,?)",
                (token.repository_id, token.run_id, acceptance_hash, stage,
                 sealed.material["candidate_hash"], sealed.legacy_generation, self._now()),
            )
            self._record_acceptance_event_tx(tx, "frontend_policy_initialized", {
                "repository_id": token.repository_id, "run_id": token.run_id,
                "acceptance_hash": acceptance_hash, "stage": stage,
            })
            return FrontendPolicyState(token.repository_id, token.run_id, acceptance_hash, stage,
                                       sealed.material["candidate_hash"], sealed.legacy_generation, None)

    def get_frontend_policy_state(self, *, repository_id: str, run_id: str) -> FrontendPolicyState | None:
        with self.read_transaction() as tx:
            if not self._frontend_policy_tables_present_tx(tx):
                return None
            row = tx.execute(
                "SELECT * FROM authority_frontend_policy_states WHERE repository_id=? AND run_id=?",
                (repository_id, run_id),
            ).fetchone()
            return None if row is None else FrontendPolicyState(
                repository_id, run_id, row["acceptance_hash"], row["stage"], row["candidate_hash"],
                row["generation"], json.loads(row["decision_json"]) if row["decision_json"] else None,
            )

    def bind_frontend_candidate(self, token, *, acceptance_hash: object, candidate_hash: object,
                                receipt_hash: object, parent_candidate_hash: object | None = None) -> FrontendPolicyState:
        """Advance a candidate only through a previously verified typed receipt."""
        from .ownership import OwnershipRefused, assert_owner
        if not all(self._valid_digest(value) for value in (acceptance_hash, candidate_hash, receipt_hash)):
            raise OwnershipRefused("FRONTEND_CANDIDATE_BINDING_INVALID")
        if parent_candidate_hash is not None and not self._valid_digest(parent_candidate_hash):
            raise OwnershipRefused("FRONTEND_CANDIDATE_BINDING_INVALID")
        self.ensure_frontend_policy_schema()
        with self.read_transaction() as tx:
            before_receipt = tx.execute('SELECT receipt_json FROM authority_acceptance_receipts '
                'WHERE repository_id=? AND run_id=? AND acceptance_hash=? AND receipt_hash=?',
                (token.repository_id, token.run_id, acceptance_hash, receipt_hash)).fetchone()
            before_integration = tx.execute('SELECT e.payload FROM authority_event_keys k '
                'JOIN control_events e ON e.id=k.event_id WHERE k.idempotency_key=?',
                ('frontend-integration:' + receipt_hash,)).fetchone()
        if before_receipt is None:
            raise OwnershipRefused('FRONTEND_CANDIDATE_BINDING_INVALID')
        before_value = json.loads(before_receipt['receipt_json'])
        if before_value.get('role') not in {'execution', 'recovery'} or before_value.get('completion_status') != 'succeeded':
            raise OwnershipRefused('FRONTEND_CANDIDATE_RECEIPT_STALE')
        if before_integration is None:
            raise OwnershipRefused('FRONTEND_INTEGRATION_REQUIRED')
        before_proof = json.loads(before_integration['payload'])['data']
        from .candidate_chain import verify_candidate_chain
        observed_proof = verify_candidate_chain(self, token, receipt_hash=receipt_hash, candidate_hash=candidate_hash,
            integration_evidence=before_proof['integration_evidence'], no_commit_evidence=before_proof['no_commit_evidence'])
        if observed_proof != before_proof.get('candidate_chain'):
            raise OwnershipRefused('FRONTEND_INTEGRATION_CHAIN_INVALID')
        with self.transaction() as tx:
            assert_owner(tx, token)
            state = tx.execute("SELECT * FROM authority_frontend_policy_states WHERE repository_id=? AND run_id=?",
                               (token.repository_id, token.run_id)).fetchone()
            receipt = tx.execute(
                "SELECT * FROM authority_acceptance_receipts WHERE repository_id=? AND run_id=? AND acceptance_hash=? AND receipt_hash=?",
                (token.repository_id, token.run_id, acceptance_hash, receipt_hash),
            ).fetchone()
            if state is None or receipt is None or state["acceptance_hash"] != acceptance_hash:
                raise OwnershipRefused("FRONTEND_CANDIDATE_BINDING_INVALID")
            try:
                receipt_value = json.loads(receipt["receipt_json"])
            except (TypeError, ValueError):
                raise OwnershipRefused("FRONTEND_CANDIDATE_BINDING_INVALID") from None
            if (receipt_value.get("completion_status") != "succeeded"
                    or receipt_value.get("role") not in {"execution", "recovery"}):
                raise OwnershipRefused("FRONTEND_CANDIDATE_RECEIPT_STALE")
            integration = tx.execute(
                "SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
                "WHERE k.idempotency_key=?",
                ("frontend-integration:" + receipt_hash,),
            ).fetchone()
            if integration is None:
                raise OwnershipRefused("FRONTEND_INTEGRATION_REQUIRED")
            try:
                integration_data = json.loads(integration["payload"])["data"]
            except (KeyError, TypeError, ValueError):
                raise OwnershipRefused("FRONTEND_INTEGRATION_REQUIRED") from None
            if integration_data.get("candidate_hash") != candidate_hash:
                raise OwnershipRefused("FRONTEND_INTEGRATION_REQUIRED")
            if integration_data != before_proof:
                raise OwnershipRefused('FRONTEND_INTEGRATION_CHAIN_INVALID')
            proof = integration_data.get('candidate_chain', {})
            parent = proof.get('input_candidate_hash') if parent_candidate_hash is None else parent_candidate_hash
            if (proof.get('input_candidate_hash') != receipt_value.get('candidate_hash')
                    or proof.get('input_candidate_hash') != parent or proof.get('candidate_hash') != candidate_hash):
                raise OwnershipRefused('FRONTEND_INTEGRATION_REQUIRED')
            if state['candidate_hash'] not in {parent, candidate_hash}:
                raise OwnershipRefused("FRONTEND_CANDIDATE_STALE")
            existing = tx.execute(
                "SELECT * FROM authority_frontend_policy_candidates WHERE repository_id=? AND run_id=? AND acceptance_hash=? AND candidate_hash=?",
                (token.repository_id, token.run_id, acceptance_hash, candidate_hash),
            ).fetchone()
            if existing is not None:
                if (existing["receipt_hash"], existing["parent_candidate_hash"], existing["generation"]) != (
                    receipt_hash, parent, state["generation"],
                ):
                    raise OwnershipRefused("FRONTEND_CANDIDATE_BINDING_CONFLICT")
            else:
                if parent != state['candidate_hash']:
                    raise OwnershipRefused('FRONTEND_CANDIDATE_STALE')
                tx.execute(
                    "INSERT INTO authority_frontend_policy_candidates "
                    "(repository_id,run_id,acceptance_hash,candidate_hash,receipt_hash,parent_candidate_hash,generation,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (token.repository_id, token.run_id, acceptance_hash, candidate_hash, receipt_hash, parent,
                     state["generation"], self._now()),
                )
            tx.execute("UPDATE authority_frontend_policy_states SET candidate_hash=?,updated_at=? WHERE repository_id=? AND run_id=?",
                       (candidate_hash, self._now(), token.repository_id, token.run_id))
            return FrontendPolicyState(token.repository_id, token.run_id, acceptance_hash, state["stage"],
                                       candidate_hash, state["generation"],
                                       json.loads(state["decision_json"]) if state["decision_json"] else None)

    def record_frontend_integration(self, token, *, receipt_hash: object, candidate_hash: object,
                                    integration_evidence: object, no_commit_evidence: object) -> None:
        """Additive E5 seam: bind a successful receipt to journaled integration.

        Astra/E5 supplies the journal's two independently content-addressed
        records.  This method verifies their bytes before its transaction and
        records no caller boolean as an authority fact.
        """
        from .ownership import OwnershipRefused, assert_owner
        if not self._valid_digest(receipt_hash) or not self._valid_digest(candidate_hash):
            raise OwnershipRefused("FRONTEND_INTEGRATION_INVALID")
        integration = self._verified_evidence(integration_evidence)
        # A recovery winner has no GSD no-commit receipt; the chain then requires None.
        no_commit = None if no_commit_evidence is None else self._verified_evidence(no_commit_evidence)
        self.ensure_frontend_policy_schema()
        from .candidate_chain import verify_candidate_chain
        proof = verify_candidate_chain(self, token, receipt_hash=receipt_hash, candidate_hash=candidate_hash,
            integration_evidence=integration, no_commit_evidence=no_commit)
        with self.transaction() as tx:
            assert_owner(tx, token)
            receipt = tx.execute(
                "SELECT receipt_json FROM authority_acceptance_receipts WHERE repository_id=? AND run_id=? AND receipt_hash=?",
                (token.repository_id, token.run_id, receipt_hash),
            ).fetchone()
            if receipt is None:
                raise OwnershipRefused("FRONTEND_INTEGRATION_RECEIPT_REQUIRED")
            value = json.loads(receipt["receipt_json"])
            if (value.get("candidate_hash") != proof['input_candidate_hash'] or value.get("completion_status") != "succeeded"
                    or value.get("role") not in {"execution", "recovery"}):
                raise OwnershipRefused("FRONTEND_INTEGRATION_RECEIPT_REQUIRED")
            for binding in proof['event_bindings']:
                retained = tx.execute('SELECT payload_hash FROM authority_event_keys WHERE event_id=?',
                                      (binding['event_id'],)).fetchone()
                if retained is None or retained['payload_hash'] != binding['payload_hash']:
                    raise OwnershipRefused('FRONTEND_INTEGRATION_CHAIN_INVALID')
            from .integration_journal import validate_completed_publication_tx
            journals = tx.execute('SELECT wave_key,contract_sha256 FROM authority_workspace_integrations '
                'WHERE repository_id=? AND run_id=? AND issuing_intent_id=? ORDER BY event_id',
                (token.repository_id, token.run_id, proof['intent_id'])).fetchall()
            if [row['contract_sha256'] for row in journals] != proof['journal_hashes']:
                raise OwnershipRefused('FRONTEND_INTEGRATION_CHAIN_INVALID')
            for journal in journals:
                validate_completed_publication_tx(self, tx, token, wave_key=journal['wave_key'], intent_id=proof['intent_id'])
            self._record_event_once_tx(tx, token, value["activity_id"], "frontend-integration:" + receipt_hash, {
                "receipt_hash": receipt_hash, "candidate_hash": candidate_hash,
                "integration_evidence": integration, "no_commit_evidence": no_commit,
                'candidate_chain':proof,
            })

    def _frontend_check_execution_tx(self, tx, token, *, acceptance_hash, candidate_hash,
                                     check_id, status, evidence):
        """Resolve a mapped check through its exact local receipt and terminal intent."""
        from .ownership import OwnershipRefused
        rows = tx.execute('SELECT i.*,b.candidate_hash,b.contract_hash,b.workspace_preparation_id '
            'FROM authority_launch_intents i JOIN authority_child_bindings b ON b.activity_id=i.activity_id '
            'JOIN authority_activities a ON a.id=i.activity_id WHERE a.repository_id=? AND a.run_id=? '
            'AND b.candidate_hash=? AND b.contract_hash=? AND b.role=?',
            (token.repository_id, token.run_id, candidate_hash, acceptance_hash, 'inventory')).fetchall()
        for row in rows:
            if (row['completion_status'] != ('succeeded' if status == 'passed' else 'failed')
                    or row['state'] != ('completed_succeeded' if status == 'passed' else 'completed_failed')
                    or not row['acknowledgement_id'] or not row['completion_evidence_json']):
                continue
            terminal = json.loads(row['completion_evidence_json'])
            if evidence != [terminal]:
                continue
            events = tx.execute('SELECT e.payload FROM authority_event_keys k '
                'JOIN control_events e ON e.id=k.event_id WHERE k.activity_id=? '
                "AND k.idempotency_key LIKE 'dispatch-request:%'", (row['activity_id'],)).fetchall()
            for event in events:
                data = json.loads(event['payload'])['data']
                if data.get('intent_id') != row['id']:
                    continue
                local = tx.execute('SELECT * FROM authority_local_check_receipts WHERE receipt_sha256=?',
                    (data.get('local_check_receipt_sha256'),)).fetchone()
                if (local is None or local['producer_activity_id'] != row['activity_id']
                        or local['workspace_preparation_id'] != row['workspace_preparation_id']
                        or local['acceptance_hash'] != acceptance_hash or local['check_id'] != check_id
                        or local['generation'] != row['generation']
                        or data.get('managed_input_sha256') != candidate_hash):
                    continue
                material = json.loads(local['receipt_json'])['material']
                if (material['candidate_hash'] == candidate_hash
                        and data['request'].get('local_check_material') == material):
                    return row['id']
        raise OwnershipRefused('FRONTEND_CHECK_EXECUTION_REQUIRED')

    def record_frontend_check_results(self, token, *, acceptance_hash: object, candidate_hash: object,
                                      results: object) -> None:
        """Persist exact frozen mapped-check outcomes after hashing their evidence.

        Evidence I/O deliberately happens before the writer transaction.
        """
        from .ownership import OwnershipRefused, assert_owner
        if not self._valid_digest(acceptance_hash) or not self._valid_digest(candidate_hash) or not isinstance(results, list):
            raise OwnershipRefused("FRONTEND_CHECK_INVALID")
        verified: list[tuple[str, str, list[dict]]] = []
        seen: set[str] = set()
        for result in results:
            if not isinstance(result, dict) or set(result) != {"check_id", "status", "evidence"}:
                raise OwnershipRefused("FRONTEND_CHECK_INVALID")
            check_id = self._acceptance_identifier(result["check_id"], code="FRONTEND_CHECK_INVALID")
            if check_id in seen or result["status"] not in {"passed", "failed"} or not isinstance(result["evidence"], list):
                raise OwnershipRefused("FRONTEND_CHECK_INVALID")
            seen.add(check_id)
            evidence = [self._verified_evidence(item) for item in result["evidence"]]
            if not evidence:
                raise OwnershipRefused("FRONTEND_CHECK_EVIDENCE_REQUIRED")
            verified.append((check_id, result["status"], evidence))
        self.ensure_frontend_policy_schema()
        with self.transaction() as tx:
            assert_owner(tx, token)
            state = tx.execute("SELECT * FROM authority_frontend_policy_states WHERE repository_id=? AND run_id=?",
                               (token.repository_id, token.run_id)).fetchone()
            sealed_row = tx.execute("SELECT * FROM authority_sealed_acceptances WHERE repository_id=? AND run_id=? AND acceptance_hash=?",
                                    (token.repository_id, token.run_id, acceptance_hash)).fetchone()
            if state is None or sealed_row is None or state["acceptance_hash"] != acceptance_hash or state["candidate_hash"] != candidate_hash:
                raise OwnershipRefused("FRONTEND_CHECK_CANDIDATE_STALE")
            sealed = self._sealed_acceptance_from_row(sealed_row)
            frozen = {check["id"] for criterion in sealed.material["criteria"] for check in criterion["checks"]}
            if seen != frozen:
                raise OwnershipRefused("FRONTEND_CHECKS_INCOMPLETE")
            for check_id, status, evidence in verified:
                self._frontend_check_execution_tx(tx, token, acceptance_hash=acceptance_hash,
                    candidate_hash=candidate_hash, check_id=check_id, status=status, evidence=evidence)
                encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)
                current = tx.execute(
                    "SELECT status,evidence_json FROM authority_frontend_policy_checks WHERE repository_id=? AND run_id=? AND acceptance_hash=? AND candidate_hash=? AND check_id=?",
                    (token.repository_id, token.run_id, acceptance_hash, candidate_hash, check_id),
                ).fetchone()
                if current is not None and (current["status"], current["evidence_json"]) != (status, encoded):
                    raise OwnershipRefused("FRONTEND_CHECK_CONFLICT")
                if current is None:
                    tx.execute(
                        "INSERT INTO authority_frontend_policy_checks "
                        "(repository_id,run_id,acceptance_hash,candidate_hash,check_id,status,evidence_json,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (token.repository_id, token.run_id, acceptance_hash, candidate_hash, check_id,
                         status, encoded, self._now()),
                    )

    def transition_frontend_policy(self, token, *, expected_stage: object, new_stage: object,
                                   decision: object | None = None) -> FrontendPolicyState:
        """Atomically retain an evidence-rich bounded handback or lifecycle stage."""
        from .ownership import OwnershipRefused, assert_owner
        allowed = {
            "SPEC_DRAFT": {"SPEC_REVIEW", "NEEDS_DECISION", "CAPABILITY_FAILURE", "CANCELLED"},
            "SPEC_REVIEW": {"SEALED", "NEEDS_DECISION", "CAPABILITY_FAILURE", "CANCELLED"},
            "SEALED": {"EXECUTE", "CAPABILITY_FAILURE", "CANCELLED"},
            "EXECUTE": {"FINAL_REVIEW", "RECOVER", "NEEDS_DECISION", "CAPABILITY_FAILURE", "CANCELLED"},
            "FINAL_REVIEW": {"EXECUTE", "RECOVER", "DONE", "NEEDS_DECISION", "CAPABILITY_FAILURE", "CANCELLED"},
            "RECOVER": {"EXECUTE", "FINAL_REVIEW", "NEEDS_DECISION", "CAPABILITY_FAILURE", "CANCELLED"},
        }
        if expected_stage not in allowed or new_stage not in allowed[expected_stage]:
            raise OwnershipRefused("FRONTEND_STAGE_TRANSITION_INVALID")
        if decision is not None:
            try:
                decision_json = json.dumps(decision, sort_keys=True, separators=(",", ":"), allow_nan=False)
            except (TypeError, ValueError):
                raise OwnershipRefused("FRONTEND_DECISION_INVALID") from None
        else:
            decision_json = None
        self.ensure_frontend_policy_schema()
        completion = None
        if new_stage == 'DONE':
            from .frontend_completion import verify_completion_evidence
            completion = verify_completion_evidence(self, token)
        with self.transaction() as tx:
            assert_owner(tx, token)
            row = tx.execute("SELECT * FROM authority_frontend_policy_states WHERE repository_id=? AND run_id=?",
                             (token.repository_id, token.run_id)).fetchone()
            if row is None or row["stage"] != expected_stage:
                raise OwnershipRefused("FRONTEND_STAGE_STALE")
            if new_stage == "DONE":
                if any(completion[key] != row[key] for key in ('acceptance_hash', 'candidate_hash', 'generation')):
                    raise OwnershipRefused('FRONTEND_STAGE_STALE')
                sealed_row = tx.execute(
                    "SELECT * FROM authority_sealed_acceptances WHERE repository_id=? AND run_id=? AND acceptance_hash=?",
                    (token.repository_id, token.run_id, row["acceptance_hash"]),
                ).fetchone()
                if sealed_row is None:
                    raise OwnershipRefused("ACCEPTANCE_SEAL_REQUIRED")
                sealed = self._sealed_acceptance_from_row(sealed_row)
                required_checks = {check["id"] for criterion in sealed.material["criteria"]
                                   for check in criterion["checks"]}
                passed = {item["check_id"] for item in tx.execute(
                    "SELECT check_id FROM authority_frontend_policy_checks WHERE repository_id=? AND run_id=? "
                    "AND acceptance_hash=? AND candidate_hash=? AND status='passed'",
                    (token.repository_id, token.run_id, row["acceptance_hash"], row["candidate_hash"]),
                ).fetchall()}
                if passed != required_checks:
                    raise OwnershipRefused("FRONTEND_COMPLETION_CHECKS_REQUIRED")
                review_rows = tx.execute(
                    "SELECT receipt_hash,receipt_json FROM authority_acceptance_receipts WHERE repository_id=? AND run_id=? "
                    "AND acceptance_hash=?",
                    (token.repository_id, token.run_id, row["acceptance_hash"]),
                ).fetchall()
                has_review = False
                for item in review_rows:
                    try:
                        receipt = json.loads(item["receipt_json"])
                    except (TypeError, ValueError):
                        continue
                    has_review = (item['receipt_hash'] in completion['review_receipt_hashes']
                                  and receipt.get("role") == "review"
                                  and receipt.get("candidate_hash") == row["candidate_hash"]
                                  and receipt.get("completion_status") == "succeeded")
                    if has_review:
                        action = tx.execute(
                            "SELECT a.action FROM authority_policy_actions a JOIN authority_policy_action_attempts p "
                            "ON p.action_id=a.id WHERE p.intent_id=?",
                            (receipt.get("intent_id"),),
                        ).fetchone()
                        has_review = action is not None and action["action"] == "final_review"
                    if has_review:
                        break
                if not has_review:
                    from .frontend_completion import post_repair_review_tx
                    post_repair = post_repair_review_tx(self, tx, token, row["acceptance_hash"], row["candidate_hash"])
                    if post_repair is None or post_repair[0] != completion['post_repair_review_receipt_hash']:
                        raise OwnershipRefused("FRONTEND_COMPLETION_REVIEW_REQUIRED")
                blockers = tx.execute(
                    "SELECT 1 FROM authority_frontend_policy_findings WHERE repository_id=? AND run_id=? "
                    "AND acceptance_hash=? AND candidate_hash=? AND classification IN ('CONTRACT_FAILURE','INVARIANT_VIOLATION') LIMIT 1",
                    (token.repository_id, token.run_id, row["acceptance_hash"], row["candidate_hash"]),
                ).fetchone()
                if blockers is not None:
                    raise OwnershipRefused("FRONTEND_COMPLETION_BLOCKERS_REMAIN")
            if new_stage in {'DONE', 'CANCELLED', 'CAPABILITY_FAILURE'}:
                root = tx.execute('SELECT a.* FROM context_runs r JOIN authority_activities a ON a.id=r.activity_id '
                    'WHERE r.repository_id=? AND r.run_id=?', (token.repository_id, token.run_id)).fetchone()
                if root is None:
                    raise OwnershipRefused('FRONTEND_ROOT_ACTIVITY_REQUIRED')
                if new_stage == 'DONE':
                    outstanding = tx.execute('WITH RECURSIVE descendants(id) AS (SELECT ? UNION '
                        'SELECT b.activity_id FROM authority_child_bindings b JOIN descendants d ON b.parent_activity_id=d.id) '
                        "SELECT 1 FROM authority_activities a JOIN descendants d ON d.id=a.id WHERE a.id<>? "
                        "AND a.state NOT IN ('succeeded','failed','aborted') LIMIT 1", (root['id'],root['id'])).fetchone()
                    unsettled = tx.execute('SELECT 1 FROM authority_launch_intents i JOIN authority_activities a ON a.id=i.activity_id '
                        "WHERE a.repository_id=? AND a.run_id=? AND i.state NOT IN ('completed_succeeded','completed_failed') LIMIT 1",
                        (token.repository_id,token.run_id)).fetchone()
                    if outstanding is not None or unsettled is not None:
                        raise OwnershipRefused('FRONTEND_COMPLETION_OBLIGATIONS_REMAIN')
                    if root['state'] in _TERMINAL_ACTIVITY_STATES:
                        raise OwnershipRefused('ACTIVITY_TERMINAL')
                if root['state'] not in _TERMINAL_ACTIVITY_STATES:
                    self._transition_activity_tx(tx, token, root['id'], expected=root['state'],
                        new={'DONE':'succeeded','CANCELLED':'aborted','CAPABILITY_FAILURE':'failed'}[new_stage],
                        result=receipt['evidence'][0] if new_stage == 'DONE' else None,
                        reason='frontend ' + new_stage)
            tx.execute("UPDATE authority_frontend_policy_states SET stage=?,decision_json=?,updated_at=? WHERE repository_id=? AND run_id=?",
                       (new_stage, decision_json, self._now(), token.repository_id, token.run_id))
            return FrontendPolicyState(token.repository_id, token.run_id, row["acceptance_hash"], new_stage,
                                       row["candidate_hash"], row["generation"], decision)

    def _acceptance_receipt_execution_tx(self, tx, token, typed):
        """Resolve receipt claims through existing launch and workspace authority."""
        from dataclasses import asdict
        from .ownership import OwnershipRefused
        from .workspace import _from_row
        code = "ACCEPTANCE_RECEIPT_BINDING_INVALID"
        intent = tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (typed.intent_id,)).fetchone()
        activity = tx.execute("SELECT * FROM authority_activities WHERE id=?", (typed.activity_id,)).fetchone()
        child = tx.execute("SELECT * FROM authority_child_bindings WHERE activity_id=?", (typed.activity_id,)).fetchone()
        if (intent is None or activity is None or child is None
                or intent["activity_id"] != typed.activity_id
                or (activity["repository_id"], activity["run_id"]) != (token.repository_id, token.run_id)
                or intent["generation"] != typed.fence_generation
                or activity["runtime_tuple_hash"] != typed.runtime_hash
                or child["candidate_hash"] != typed.candidate_hash
                or {"execution": "worker", "review": "reviewer", "recovery": "recovery",
                    "qualification": "inventory"}.get(typed.role) != child["role"]
                or intent["acknowledgement_id"] is None
                or intent["completion_evidence_json"] is None
                or typed.completion_status == "succeeded" and intent["completion_status"] != "succeeded"):
            raise OwnershipRefused(code)
        identity = {"host_id": intent["child_host_id"], "boot_id": intent["child_boot_id"],
                    "pid": intent["child_pid"], "start_token": intent["child_start_token"]}
        dispatch = tx.execute(
            "SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
            "WHERE k.activity_id=? AND k.idempotency_key=?",
            (typed.activity_id, "dispatch-request:" + typed.request_key),
        ).fetchone()
        preparation = tx.execute("SELECT * FROM context_workspaces WHERE preparation_id=?",
                                 (child["workspace_preparation_id"],)).fetchone()
        if (identity != typed.process_identity or dispatch is None or preparation is None
                or json.loads(dispatch["payload"])["data"].get("intent_id") != typed.intent_id):
            raise OwnershipRefused(code)
        preparation_hash = hashlib.sha256(json.dumps(asdict(_from_row(preparation)), default=str,
                                                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        terminal_evidence = json.loads(intent["completion_evidence_json"])
        if (preparation_hash != typed.workspace_preparation_hash
                or terminal_evidence not in [{"locator": item["locator"], "sha256": item["sha256"]}
                                             for item in typed.evidence]):
            raise OwnershipRefused(code)

    def record_acceptance_receipt(
        self, token, *, acceptance_hash: object, receipt: object,
    ) -> AcceptanceReceipt:
        """Store a typed receipt only when all sealed identities match exactly."""
        from .ownership import OwnershipRefused
        from .run_policy import RunPolicyRefused, validate_role_receipt

        if not self._valid_digest(acceptance_hash):
            _refuse("ACCEPTANCE_RECEIPT_BINDING_INVALID")
        try:
            typed = validate_role_receipt(receipt)
        except RunPolicyRefused as error:
            self._refuse_policy_error(error)
        self.ensure_context_schema()
        self.ensure_acceptance_policy_schema()
        with self.read_transaction() as tx:
            self._acceptance_receipt_execution_tx(tx, token, typed)
        # Verify physical bytes without holding the authority writer lock.
        for item in typed.evidence:
            self._verified_evidence({"locator": item["locator"], "sha256": item["sha256"]})
        with self.transaction() as tx:
            self._acceptance_binding_tx(tx, token)
            self._acceptance_receipt_execution_tx(tx, token, typed)
            row = tx.execute(
                "SELECT * FROM authority_sealed_acceptances WHERE repository_id=? AND run_id=? AND acceptance_hash=?",
                (token.repository_id, token.run_id, acceptance_hash),
            ).fetchone()
            if row is None:
                raise OwnershipRefused("ACCEPTANCE_SEAL_REQUIRED")
            sealed = self._sealed_acceptance_from_row(row)
            self._validate_sealed_draft_binding_tx(tx, sealed)
            child = tx.execute("SELECT contract_hash FROM authority_child_bindings WHERE activity_id=?",
                               (typed.activity_id,)).fetchone()
            sealed_child = child is not None and child["contract_hash"] == sealed.acceptance_hash
            review_action = None
            if typed.role == "review":
                dimensions = sealed.material.get("required_review_dimensions")
                if not sealed_child or not dimensions:
                    raise OwnershipRefused("ACCEPTANCE_REVIEW_CONTRACT_REQUIRED")
                try:
                    validate_role_receipt(receipt, required_review_dimensions=dimensions)
                except RunPolicyRefused as error:
                    self._refuse_policy_error(error)
                if not self._run_policy_tables_present_tx(tx):
                    raise OwnershipRefused("ACCEPTANCE_REVIEW_ACTION_REQUIRED")
                review_action = tx.execute(
                    "SELECT a.* FROM authority_policy_actions a JOIN authority_policy_action_attempts p ON p.action_id=a.id "
                    "WHERE p.intent_id=? AND a.repository_id=? AND a.run_id=? AND a.action IN ('spec_review','final_review')",
                    (typed.intent_id, token.repository_id, token.run_id),
                ).fetchone()
                if review_action is None:
                    raise OwnershipRefused("ACCEPTANCE_REVIEW_ACTION_REQUIRED")
            if (
                typed.acceptance_hash != sealed.acceptance_hash
                or not sealed_child and (typed.candidate_hash != sealed.material["candidate_hash"]
                                         or typed.runtime_hash != sealed.material["runtime"]["effective_hash"])
            ):
                raise OwnershipRefused("ACCEPTANCE_RECEIPT_BINDING_INVALID")
            if review_action is not None and typed.completion_status == "succeeded":
                if review_action["receipt_hash"] not in {None, typed.receipt_hash}:
                    raise OwnershipRefused("POLICY_RECEIPT_CONFLICT")
                tx.execute("UPDATE authority_policy_actions SET receipt_hash=?,state='completed_valid',updated_at=? WHERE id=?",
                           (typed.receipt_hash, self._now(), review_action["id"]))
            existing = tx.execute(
                "SELECT receipt_json FROM authority_acceptance_receipts WHERE repository_id=? AND run_id=? AND receipt_hash=?",
                (token.repository_id, token.run_id, typed.receipt_hash),
            ).fetchone()
            if existing is not None:
                if existing["receipt_json"] != typed.receipt_json:
                    raise OwnershipRefused("ACCEPTANCE_RECEIPT_CONFLICT")
                return AcceptanceReceipt(
                    token.repository_id, token.run_id, sealed.acceptance_hash,
                    typed.receipt_hash, typed, True,
                )
            tx.execute(
                "INSERT INTO authority_acceptance_receipts "
                "(repository_id,run_id,acceptance_hash,receipt_hash,receipt_json,created_at) VALUES(?,?,?,?,?,?)",
                (token.repository_id, token.run_id, sealed.acceptance_hash, typed.receipt_hash,
                 typed.receipt_json, self._now()),
            )
            self._record_acceptance_event_tx(tx, "acceptance_receipt_recorded", {
                "repository_id": token.repository_id, "run_id": token.run_id,
                "acceptance_hash": sealed.acceptance_hash, "receipt_hash": typed.receipt_hash,
            })
            return AcceptanceReceipt(
                token.repository_id, token.run_id, sealed.acceptance_hash,
                typed.receipt_hash, typed,
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _activity_from_row(row, *, reused_result: bool = False) -> Activity:
        result = json.loads(row["result_json"]) if row["result_json"] else None
        return Activity(
            row["id"], row["repository_id"], row["run_id"], row["kind"],
            row["input_digest"], row["revision"], row["state"], row["retry_budget"],
            row["remaining_retry_budget"], row["runtime_tuple_hash"], result, reused_result,
        )

    def _assert_activity_binding(self, tx, token, activity_id: str):
        from .ownership import OwnershipRefused, assert_owner
        assert_owner(tx, token)
        self.assert_migration_epoch_tx(tx, token.run_id)
        row = tx.execute(
            "SELECT * FROM authority_activities WHERE id = ?", (activity_id,),
        ).fetchone()
        if row is None or row["repository_id"] != token.repository_id or row["run_id"] != token.run_id:
            raise OwnershipRefused("FENCE_REVOKED")
        return row

    def _fault(self, operation: str, boundary: str) -> None:
        if self.fault_probe is not None:
            self.fault_probe(f"{operation}.{boundary}")

    @staticmethod
    def _assert_activity_ancestry(tx, activity_id: str, *, repository_id: str, run_id: str) -> None:
        """Reject a child whose parent (at any depth) can no longer dispatch."""
        from .ownership import OwnershipRefused
        current = activity_id
        visited: set[str] = set()
        while True:
            if current in visited:
                raise OwnershipRefused("PARENT_ACTIVITY_INVALID")
            visited.add(current)
            binding = tx.execute(
                "SELECT parent_activity_id FROM authority_child_bindings WHERE activity_id=?",
                (current,),
            ).fetchone()
            if binding is None:
                return
            parent = tx.execute(
                "SELECT repository_id,run_id,state FROM authority_activities WHERE id=?",
                (binding["parent_activity_id"],),
            ).fetchone()
            if (
                parent is None or parent["repository_id"] != repository_id
                or parent["run_id"] != run_id or parent["state"] != "active"
            ):
                raise OwnershipRefused("PARENT_ACTIVITY_INVALID")
            current = binding["parent_activity_id"]

    @staticmethod
    def _active_capacity_launch_count_tx(tx, token) -> int:
        """Return active launch intents that consume worker/reviewer capacity.

        The managed outer orchestrator and qualification probes record an
        explicit durable exemption. Every ordinary/direct child, including
        worker, reviewer, and recovery roles, remains capacity-bearing. All
        reservation paths use this one durable query so a cohort cannot
        receive a different allowance from an individual dispatch.
        """
        return tx.execute(
            "SELECT COUNT(*) FROM authority_launch_intents i "
            "JOIN authority_activities a ON a.id=i.activity_id "
            "WHERE a.repository_id=? AND a.run_id=? AND i.capacity_exempt=0 "
            "AND i.state IN ('reserved','acknowledged','released_to_execute',"
            "'reconcile_required','uncertain')",
            (token.repository_id, token.run_id),
        ).fetchone()[0]

    @staticmethod
    def _effective_worker_capacity_tx(tx, token, limits) -> int:
        """Return the current operator ceiling, or the immutable legacy one.

        The revision is deliberately a scheduling policy, not an accounting
        field.  It therefore never changes the configured tuple or attempts
        to reinterpret an already-issued intent.
        """
        from .ownership import OwnershipRefused

        current = tx.execute(
            "SELECT revision,worker_capacity FROM authority_current_worker_capacity "
            "WHERE repository_id=? AND run_id=?",
            (token.repository_id, token.run_id),
        ).fetchone()
        if current is None:
            return limits["worker_capacity"]
        if (
            not isinstance(current["revision"], int) or current["revision"] < 1
            or not isinstance(current["worker_capacity"], int) or current["worker_capacity"] < 1
        ):
            raise OwnershipRefused("WORKER_CAPACITY_REVISION_UNKNOWN")
        recorded = tx.execute(
            "SELECT worker_capacity FROM authority_worker_capacity_revisions "
            "WHERE repository_id=? AND run_id=? AND revision=?",
            (token.repository_id, token.run_id, current["revision"]),
        ).fetchone()
        if recorded is None or recorded["worker_capacity"] != current["worker_capacity"]:
            raise OwnershipRefused("WORKER_CAPACITY_REVISION_UNKNOWN")
        return current["worker_capacity"]

    @staticmethod
    def _verified_evidence(evidence: object) -> dict:
        from .ownership import OwnershipRefused
        if (
            not isinstance(evidence, dict)
            or not isinstance(evidence.get("locator"), str) or not evidence["locator"]
            or not ControlStore._valid_digest(evidence.get("sha256"))
        ):
            raise OwnershipRefused("EVIDENCE_REQUIRED")
        locator = Path(evidence["locator"])
        if not locator.is_absolute():
            raise OwnershipRefused("EVIDENCE_INVALID")
        try:
            observed, _identity = _hash_regular_file(locator)
        except (ControlStoreRefused, FileNotFoundError, OSError):
            raise OwnershipRefused("EVIDENCE_INVALID") from None
        if not secrets.compare_digest(observed, evidence["sha256"].lower()):
            raise OwnershipRefused("EVIDENCE_INVALID")
        return dict(evidence)

    def _terminal_child_status(self, row) -> str:
        """A final classification releases capacity only after a dead child."""
        from process_identity import DEAD, UNKNOWN, probe_identity
        identity = self._intent_from_row(row).child_identity
        if identity is None:
            return UNKNOWN
        native = probe_identity(identity)
        if native != DEAD:
            return native
        if self.liveness_probe is None:
            return DEAD
        try:
            injected = self.liveness_probe(identity)
        except Exception:
            injected = UNKNOWN
        return DEAD if injected == DEAD else UNKNOWN

    @staticmethod
    def _valid_nonnegative_integer(value: object) -> bool:
        return (
            not isinstance(value, bool) and isinstance(value, int)
            and 0 <= value <= 9_223_372_036_854_775_807
        )

    @staticmethod
    def _valid_digest(value: object) -> bool:
        return isinstance(value, str) and len(value) == 64 and all(
            character in "0123456789abcdef" for character in value.lower()
        )

    def configure_run_limits(
        self, token, *, dispatch_limit: int, token_limit: int, worker_capacity: int = 3,
    ) -> dict:
        """Create the immutable aggregate allowance for one managed run.

        Limits deliberately have no update/reset operation.  An exact retry is
        idempotent; every differing request is refused so a resumed or revised
        activity cannot replenish the run's allowance.
        """
        from .ownership import OwnershipRefused, assert_owner
        if (
            not self._valid_nonnegative_integer(dispatch_limit)
            or not self._valid_nonnegative_integer(token_limit)
            or isinstance(worker_capacity, bool) or not isinstance(worker_capacity, int)
            or worker_capacity < 1 or worker_capacity > 9_223_372_036_854_775_807
        ):
            raise OwnershipRefused("INVALID_RUN_LIMITS")
        self.ensure_context_schema()
        self.ensure_authority_schema()
        with self.transaction() as tx:
            assert_owner(tx, token)
            self.assert_migration_epoch_tx(tx, token.run_id)
            existing = tx.execute(
                "SELECT * FROM authority_run_limits WHERE repository_id=? AND run_id=?",
                (token.repository_id, token.run_id),
            ).fetchone()
            requested = (dispatch_limit, token_limit, worker_capacity)
            if existing is not None:
                recorded = (
                    existing["dispatch_limit"], existing["token_limit"], existing["worker_capacity"],
                )
                if recorded != requested:
                    raise OwnershipRefused("RUN_LIMITS_IMMUTABLE")
                return dict(existing)
            # Do not take over a run which already has launch history from an
            # older/non-managed writer: its past debits cannot be reconstructed
            # safely.  Such runs retain their original writer during migration.
            legacy_launch = tx.execute(
                "SELECT 1 FROM authority_launch_intents i JOIN authority_activities a "
                "ON a.id=i.activity_id WHERE a.repository_id=? AND a.run_id=? LIMIT 1",
                (token.repository_id, token.run_id),
            ).fetchone()
            if legacy_launch is not None:
                raise OwnershipRefused("LEGACY_ACCOUNTING_PRESENT")
            now = self._now()
            tx.execute(
                "INSERT INTO authority_run_limits "
                "(repository_id,run_id,dispatch_limit,token_limit,worker_capacity,generation,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (token.repository_id, token.run_id, dispatch_limit, token_limit,
                 worker_capacity, token.generation, now),
            )
            tx.execute(
                "INSERT INTO control_events(event_type,payload) VALUES('run_limits_configured',?)",
                (json.dumps({"repository_id": token.repository_id, "run_id": token.run_id,
                             "data": {"dispatch_limit": dispatch_limit, "token_limit": token_limit,
                                      "worker_capacity": worker_capacity}},
                            sort_keys=True, separators=(",", ":")),),
            )
            return {
                "repository_id": token.repository_id, "run_id": token.run_id,
                "dispatch_limit": dispatch_limit, "token_limit": token_limit,
                "worker_capacity": worker_capacity, "dispatch_used": 0,
                "token_committed": 0, "token_used": 0,
            }

    @staticmethod
    def _worker_capacity_revision_from_row(row) -> WorkerCapacityRevision:
        return WorkerCapacityRevision(
            row["repository_id"], row["run_id"], row["revision"],
            row["worker_capacity"], row["expected_revision"],
            row["owner_generation"], row["idempotency_key"],
        )

    def revise_capacity_policy(
        self, token, *, worker_capacity: int, expected_revision: int,
        request_key: str,
    ) -> WorkerCapacityRevision:
        """CAS a future-admission worker ceiling without replenishing a run.

        A revision is bound to the currently fenced owner and migration epoch.
        Exact idempotent retries return the original history row; a new key
        must name the exact current revision.  Existing launch intents remain
        valid even when a decrease leaves the active count above this ceiling.
        """
        from .ownership import OwnershipRefused, assert_owner

        if (
            isinstance(worker_capacity, bool) or not isinstance(worker_capacity, int)
            or worker_capacity < 1 or worker_capacity > 9_223_372_036_854_775_807
            or isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
            or expected_revision < 0 or expected_revision > 9_223_372_036_854_775_807
            or not isinstance(request_key, str) or not request_key
            or len(request_key.encode("utf-8")) > 256
        ):
            raise OwnershipRefused("INVALID_WORKER_CAPACITY_REVISION")
        self.ensure_context_schema()
        self.ensure_authority_schema()
        with self.transaction() as tx:
            assert_owner(tx, token)
            self.assert_migration_epoch_tx(tx, token.run_id)
            limits = tx.execute(
                "SELECT 1 FROM authority_run_limits WHERE repository_id=? AND run_id=?",
                (token.repository_id, token.run_id),
            ).fetchone()
            if limits is None:
                raise OwnershipRefused("RUN_LIMITS_REQUIRED")
            replay = tx.execute(
                "SELECT * FROM authority_worker_capacity_revisions "
                "WHERE repository_id=? AND run_id=? AND idempotency_key=?",
                (token.repository_id, token.run_id, request_key),
            ).fetchone()
            if replay is not None:
                if (
                    replay["worker_capacity"] != worker_capacity
                    or replay["expected_revision"] != expected_revision
                ):
                    raise OwnershipRefused("IDEMPOTENCY_CONFLICT")
                return self._worker_capacity_revision_from_row(replay)
            current = tx.execute(
                "SELECT revision FROM authority_current_worker_capacity "
                "WHERE repository_id=? AND run_id=?",
                (token.repository_id, token.run_id),
            ).fetchone()
            actual_revision = 0 if current is None else current["revision"]
            if actual_revision != expected_revision:
                raise OwnershipRefused("WORKER_CAPACITY_REVISION_CONFLICT")
            if actual_revision >= 9_223_372_036_854_775_807:
                raise OwnershipRefused("WORKER_CAPACITY_REVISION_EXHAUSTED")
            revision, now = actual_revision + 1, self._now()
            tx.execute(
                "INSERT INTO authority_worker_capacity_revisions "
                "(repository_id,run_id,revision,worker_capacity,expected_revision,"
                "idempotency_key,owner_generation,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (token.repository_id, token.run_id, revision, worker_capacity,
                 expected_revision, request_key, token.generation, now),
            )
            tx.execute(
                "INSERT INTO authority_current_worker_capacity "
                "(repository_id,run_id,revision,worker_capacity,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(repository_id,run_id) DO UPDATE SET "
                "revision=excluded.revision,worker_capacity=excluded.worker_capacity,"
                "updated_at=excluded.updated_at",
                (token.repository_id, token.run_id, revision, worker_capacity, now),
            )
            tx.execute(
                "INSERT INTO control_events(event_type,payload) VALUES('worker_capacity_revised',?)",
                (json.dumps({
                    "repository_id": token.repository_id, "run_id": token.run_id,
                    "data": {"revision": revision, "expected_revision": expected_revision,
                             "worker_capacity": worker_capacity,
                             "owner_generation": token.generation},
                }, sort_keys=True, separators=(",", ":")),),
            )
            return WorkerCapacityRevision(
                token.repository_id, token.run_id, revision, worker_capacity,
                expected_revision, token.generation, request_key,
            )

    def revise_worker_capacity(
        self, token, *, worker_capacity: int, expected_revision: int,
        idempotency_key: str,
    ) -> WorkerCapacityRevision:
        """Compatibility spelling for the capacity-policy revision authority."""
        return self.revise_capacity_policy(
            token, worker_capacity=worker_capacity, expected_revision=expected_revision,
            request_key=idempotency_key,
        )

    def get_worker_capacity_revision(
        self, *, repository_id: str, run_id: str,
    ) -> WorkerCapacityRevision | None:
        """Read the optional current revision; absence preserves legacy capacity."""
        with self.read_transaction() as tx:
            row = tx.execute(
                "SELECT r.* FROM authority_current_worker_capacity c "
                "JOIN authority_worker_capacity_revisions r ON "
                "r.repository_id=c.repository_id AND r.run_id=c.run_id AND r.revision=c.revision "
                "WHERE c.repository_id=? AND c.run_id=?",
                (repository_id, run_id),
            ).fetchone()
        return None if row is None else self._worker_capacity_revision_from_row(row)

    def get_capacity_policy(self, *, repository_id: str, run_id: str) -> dict:
        """Return the effective current ceiling with revision zero for legacy runs."""
        if (
            not isinstance(repository_id, str) or not repository_id
            or not isinstance(run_id, str) or not run_id
        ):
            from .ownership import OwnershipRefused
            raise OwnershipRefused("INVALID_CAPACITY_POLICY")
        with self.read_transaction() as tx:
            limits = tx.execute(
                "SELECT worker_capacity FROM authority_run_limits "
                "WHERE repository_id=? AND run_id=?",
                (repository_id, run_id),
            ).fetchone()
            if limits is None:
                from .ownership import OwnershipRefused
                raise OwnershipRefused("RUN_LIMITS_REQUIRED")
            current = tx.execute(
                "SELECT revision,worker_capacity FROM authority_current_worker_capacity "
                "WHERE repository_id=? AND run_id=?",
                (repository_id, run_id),
            ).fetchone()
            if current is not None:
                history = tx.execute(
                    "SELECT 1 FROM authority_worker_capacity_revisions "
                    "WHERE repository_id=? AND run_id=? AND revision=? AND worker_capacity=?",
                    (repository_id, run_id, current["revision"], current["worker_capacity"]),
                ).fetchone()
                if history is None:
                    from .ownership import OwnershipRefused
                    raise OwnershipRefused("WORKER_CAPACITY_REVISION_UNKNOWN")
        ceiling = limits["worker_capacity"] if current is None else current["worker_capacity"]
        return {
            "repository_id": repository_id, "run_id": run_id,
            "revision": 0 if current is None else current["revision"],
            "effective_worker_capacity": ceiling, "worker_capacity": ceiling,
        }

    def list_worker_capacity_revisions(
        self, *, repository_id: str, run_id: str,
    ) -> tuple[WorkerCapacityRevision, ...]:
        with self.read_transaction() as tx:
            rows = tx.execute(
                "SELECT * FROM authority_worker_capacity_revisions "
                "WHERE repository_id=? AND run_id=? ORDER BY revision",
                (repository_id, run_id),
            ).fetchall()
        return tuple(self._worker_capacity_revision_from_row(row) for row in rows)

    def create_child_activity(
        self, token, *, parent_activity_id: str, role: str, request_key: str,
        candidate_hash: str, contract_hash: str, runtime_identity: str,
        workspace_binding: str, workspace_preparation_id: str | None = None, retry_budget: int = 1,
        admission_guard=None, activity_id: str | None = None,
    ) -> Activity:
        """Bind a worker/reviewer/recovery child to an already owned activity."""
        from .ownership import OwnershipRefused, assert_owner
        if (
            not isinstance(parent_activity_id, str) or not parent_activity_id
            or role not in {"worker", "reviewer", "recovery", "inventory"}
            or not isinstance(request_key, str) or not request_key
            or not self._valid_digest(candidate_hash) or not self._valid_digest(contract_hash)
            or not isinstance(runtime_identity, str) or not runtime_identity
            or not isinstance(workspace_binding, str) or not workspace_binding
            or not isinstance(workspace_preparation_id, str) or not workspace_preparation_id
            or isinstance(retry_budget, bool) or not isinstance(retry_budget, int)
            or retry_budget < 0 or retry_budget > 9_223_372_036_854_775_807
        ):
            raise OwnershipRefused("INVALID_CHILD_ACTIVITY")
        if activity_id is not None:
            try:
                parsed_activity_id = uuid.UUID(activity_id)
            except (AttributeError, TypeError, ValueError):
                raise OwnershipRefused("INVALID_CHILD_ACTIVITY_ID") from None
            if str(parsed_activity_id) != activity_id:
                raise OwnershipRefused("INVALID_CHILD_ACTIVITY_ID")
        self.ensure_context_schema()
        self.ensure_authority_schema()
        with self.transaction() as tx:
            assert_owner(tx, token)
            if admission_guard is not None:
                admission_guard(tx)
            if tx.execute(
                "SELECT 1 FROM authority_run_limits WHERE repository_id=? AND run_id=?",
                (token.repository_id, token.run_id),
            ).fetchone() is None:
                raise OwnershipRefused("RUN_LIMITS_REQUIRED")
            parent = tx.execute(
                "SELECT * FROM authority_activities WHERE id=?", (parent_activity_id,),
            ).fetchone()
            if (
                parent is None or parent["repository_id"] != token.repository_id
                or parent["run_id"] != token.run_id
                or parent["state"] in _TERMINAL_ACTIVITY_STATES
            ):
                raise OwnershipRefused("PARENT_ACTIVITY_INVALID")
            preparation = tx.execute(
                "SELECT * FROM context_workspaces WHERE preparation_id=? "
                "AND repository_id=? AND run_id=? AND generation=? AND state='ready' AND created_by_ffs=1",
                (workspace_preparation_id, token.repository_id, token.run_id, token.generation),
            ).fetchone()
            if preparation is None:
                raise OwnershipRefused("WORKSPACE_BINDING_MISMATCH")
            from .workspace import _assert_preparation_binding
            _assert_preparation_binding(preparation, token, require_generation=True, tx=tx)
            if (preparation["parent_activity_id"] != parent_activity_id
                    or preparation["child_role"] != role or preparation["path"] != workspace_binding
                    or workspace_binding == token.workspace):
                raise OwnershipRefused("WORKSPACE_BINDING_MISMATCH")
            existing = tx.execute(
                "SELECT a.*,b.parent_activity_id,b.role,b.candidate_hash,b.contract_hash,"
                "b.runtime_identity,b.workspace_binding,b.workspace_preparation_id FROM authority_activities a "
                "LEFT JOIN authority_child_bindings b ON b.activity_id=a.id "
                "WHERE a.repository_id=? AND a.run_id=? AND a.request_key=?",
                (token.repository_id, token.run_id, request_key),
            ).fetchone()
            if existing is not None:
                expected = (parent_activity_id, role, candidate_hash, contract_hash,
                            runtime_identity, workspace_binding, workspace_preparation_id, retry_budget)
                actual = (existing["parent_activity_id"], existing["role"],
                          existing["candidate_hash"], existing["contract_hash"],
                          existing["runtime_identity"], existing["workspace_binding"],
                          existing["workspace_preparation_id"],
                          existing["retry_budget"])
                if actual != expected or (activity_id is not None and existing["id"] != activity_id):
                    raise OwnershipRefused("IDEMPOTENCY_CONFLICT")
                return self._activity_from_row(existing)
            if activity_id is not None and tx.execute(
                "SELECT 1 FROM authority_activities WHERE id=?", (activity_id,),
            ).fetchone() is not None:
                raise OwnershipRefused("ACTIVITY_ID_CONFLICT")
            if tx.execute(
                "SELECT 1 FROM authority_child_bindings WHERE workspace_preparation_id=?",
                (workspace_preparation_id,),
            ).fetchone() is not None:
                raise OwnershipRefused("WORKSPACE_ACTIVITY_BOUND")
            revision = tx.execute(
                "SELECT COALESCE(MAX(revision),0)+1 FROM authority_activities "
                "WHERE repository_id=? AND run_id=?", (token.repository_id, token.run_id),
            ).fetchone()[0]
            child_id, now = activity_id or str(uuid.uuid4()), self._now()
            tx.execute(
                "INSERT INTO authority_activities "
                "(id,repository_id,run_id,kind,input_digest,revision,state,retry_budget,"
                "remaining_retry_budget,runtime_tuple_hash,request_key,generation,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'pending',?,?,?,?,?,?,?)",
                (child_id, token.repository_id, token.run_id,
                 "review" if role == "reviewer" else "execute", candidate_hash, revision,
                 retry_budget, retry_budget, runtime_identity, request_key, token.generation, now, now),
            )
            tx.execute(
                "INSERT INTO authority_child_bindings "
                "(activity_id,parent_activity_id,role,candidate_hash,contract_hash,runtime_identity,"
                "workspace_binding,workspace_preparation_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (child_id, parent_activity_id, role, candidate_hash, contract_hash,
                 runtime_identity, workspace_binding, workspace_preparation_id, now),
            )
            row = tx.execute("SELECT * FROM authority_activities WHERE id=?", (child_id,)).fetchone()
            return self._activity_from_row(row)

    def create_activity(
        self, token, *, kind: str, input_digest: str, retry_budget: int,
        request_key: str,
    ) -> Activity:
        from .ownership import OwnershipRefused, assert_owner
        if (
            kind not in {"plan", "execute", "review"}
            or not isinstance(input_digest, str) or not input_digest
            or isinstance(retry_budget, bool) or not isinstance(retry_budget, int)
            or retry_budget < 0 or retry_budget > 9_223_372_036_854_775_807
            or not isinstance(request_key, str) or not request_key
        ):
            raise OwnershipRefused("INVALID_ACTIVITY")
        self.ensure_authority_schema()
        with self.transaction() as tx:
            assert_owner(tx, token)
            existing = tx.execute(
                "SELECT * FROM authority_activities WHERE repository_id = ? AND run_id = ? "
                "AND request_key = ?", (token.repository_id, token.run_id, request_key),
            ).fetchone()
            if existing is not None:
                if (
                    existing["kind"] != kind or existing["input_digest"] != input_digest
                    or existing["retry_budget"] != retry_budget
                ):
                    raise OwnershipRefused("IDEMPOTENCY_CONFLICT")
                return self._activity_from_row(existing)
            unfinished = tx.execute(
                "SELECT 1 FROM authority_activities WHERE repository_id = ? AND run_id = ? "
                "AND state NOT IN ('succeeded', 'failed', 'aborted') LIMIT 1",
                (token.repository_id, token.run_id),
            ).fetchone()
            if unfinished is not None:
                raise OwnershipRefused("RESUME_REQUIRED")
            revision = tx.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 FROM authority_activities "
                "WHERE repository_id = ? AND run_id = ?",
                (token.repository_id, token.run_id),
            ).fetchone()[0]
            activity_id = str(uuid.uuid4())
            now = self._now()
            tx.execute(
                "INSERT INTO authority_activities "
                "(id,repository_id,run_id,kind,input_digest,revision,state,retry_budget,"
                "remaining_retry_budget,request_key,generation,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,'pending',?,?,?,?,?,?)",
                (activity_id, token.repository_id, token.run_id, kind, input_digest, revision,
                 retry_budget, retry_budget, request_key, token.generation, now, now),
            )
            row = tx.execute("SELECT * FROM authority_activities WHERE id = ?", (activity_id,)).fetchone()
            return self._activity_from_row(row)

    def get_activity(self, activity_id: str) -> Activity:
        with self.read_transaction() as tx:
            row = tx.execute("SELECT * FROM authority_activities WHERE id = ?", (activity_id,)).fetchone()
        if row is None:
            raise ControlStoreRefused("ACTIVITY_NOT_FOUND")
        return self._activity_from_row(row)

    def get_run_control(
        self, run_id: str, *, repository_id: str | None = None,
    ) -> RunControl:
        with self.read_transaction() as tx:
            if repository_id is None:
                repositories = tx.execute(
                    "SELECT DISTINCT repository_id FROM authority_activities WHERE run_id = ?",
                    (run_id,),
                ).fetchall()
                if len(repositories) > 1:
                    raise ControlStoreRefused("AMBIGUOUS_RUN")
                if not repositories:
                    return RunControl(run_id, "idle")
                repository_id = repositories[0]["repository_id"]
            rows = tx.execute(
                "SELECT state FROM authority_activities WHERE repository_id = ? AND run_id = ? "
                "ORDER BY revision DESC",
                (repository_id, run_id),
            ).fetchall()
        if not rows:
            return RunControl(run_id, "idle")
        state = "complete" if all(row["state"] == "succeeded" for row in rows) else "active"
        return RunControl(run_id, state)

    def bind_runtime(self, token, activity_id: str, runtime_tuple_hash: str) -> Activity:
        from .ownership import OwnershipRefused
        if not isinstance(runtime_tuple_hash, str) or not runtime_tuple_hash:
            raise OwnershipRefused("RUNTIME_DRIFT")
        self.ensure_authority_schema()
        with self.transaction() as tx:
            row = self._assert_activity_binding(tx, token, activity_id)
            if row["runtime_tuple_hash"] not in (None, runtime_tuple_hash):
                raise OwnershipRefused("RUNTIME_DRIFT")
            tx.execute(
                "UPDATE authority_activities SET runtime_tuple_hash = ?, updated_at = ? WHERE id = ?",
                (runtime_tuple_hash, self._now(), activity_id),
            )
            row = tx.execute("SELECT * FROM authority_activities WHERE id = ?", (activity_id,)).fetchone()
            return self._activity_from_row(row)

    @staticmethod
    def runtime_tuple_hash(qualified: object) -> str:
        return qualified_runtime_tuple_hash(qualified)

    @staticmethod
    def _runtime_receipt_from_row(row, *, reused: bool = False) -> RuntimeReceipt:
        return RuntimeReceipt(
            row["receipt_sha256"], row["producer_activity_id"],
            row["workspace_preparation_id"], row["runtime_tuple_hash"],
            row["host_id"], row["boot_id"], row["observed_at"],
            row["expires_at"], row["receipt_json"], reused,
        )

    @staticmethod
    def _local_check_receipt_from_row(row, *, reused: bool = False) -> LocalCheckReceipt:
        return LocalCheckReceipt(
            row["receipt_sha256"], row["producer_activity_id"], row["workspace_preparation_id"],
            row["acceptance_hash"], row["check_id"], row["material_sha256"],
            row["runtime_tuple_hash"], row["host_id"], row["boot_id"], row["generation"],
            row["receipt_json"], reused,
        )

    @staticmethod
    def _activity_preparation_tx(tx, token, activity_id: str):
        """Resolve the one prepared workspace which an activity may execute in."""
        from .ownership import OwnershipRefused

        child = tx.execute(
            "SELECT workspace_preparation_id FROM authority_child_bindings WHERE activity_id=?",
            (activity_id,),
        ).fetchone()
        if child is not None:
            preparation_id = child["workspace_preparation_id"]
        else:
            run = tx.execute(
                "SELECT preparation_id FROM context_runs WHERE repository_id=? AND run_id=? "
                "AND activity_id=?",
                (token.repository_id, token.run_id, activity_id),
            ).fetchone()
            preparation_id = None if run is None else run["preparation_id"]
        if not isinstance(preparation_id, str) or not preparation_id:
            raise OwnershipRefused("WORKSPACE_BINDING_MISMATCH")
        preparation = tx.execute(
            "SELECT * FROM context_workspaces WHERE preparation_id=? AND repository_id=? "
            "AND run_id=? AND generation=? AND state='ready' AND created_by_ffs=1",
            (preparation_id, token.repository_id, token.run_id, token.generation),
        ).fetchone()
        if preparation is None:
            raise OwnershipRefused("WORKSPACE_BINDING_MISMATCH")
        from .workspace import _assert_preparation_binding
        _assert_preparation_binding(preparation, token, require_generation=True, tx=tx)
        return preparation

    @staticmethod
    def _assert_runtime_workspace(preparation, workspace: dict) -> None:
        from .ownership import OwnershipRefused

        raw_path = preparation["path"]
        path = Path(raw_path)
        if (
            not path.is_absolute() or os.path.normpath(raw_path) != raw_path
            or path.is_symlink() or workspace.get("path") != raw_path
        ):
            raise OwnershipRefused("WORKSPACE_BINDING_MISMATCH")
        try:
            info = os.stat(path, follow_symlinks=False)
            resolved = os.fspath(path.resolve(strict=True))
        except OSError:
            raise OwnershipRefused("WORKSPACE_BINDING_MISMATCH") from None
        if (
            not stat.S_ISDIR(info.st_mode) or resolved != raw_path
            or workspace.get("device") != info.st_dev
            or workspace.get("inode") != info.st_ino
        ):
            raise OwnershipRefused("WORKSPACE_BINDING_MISMATCH")

    def commit_runtime_receipt(self, token, activity_id: str, qualified: object) -> RuntimeReceipt:
        """Durably bind a fresh qualified runtime to its live activity/workspace."""
        from .ownership import OwnershipRefused
        from host_capabilities import QualifiedCodexRuntime
        from .claude_host import QualifiedClaudeRuntime
        from process_identity import ProcessIdentity

        if type(qualified) not in {QualifiedCodexRuntime, QualifiedClaudeRuntime}:
            raise OwnershipRefused("RUNTIME_RECEIPT_INVALID")
        try:
            payload = qualified.to_dict()
        except (AttributeError, TypeError, ValueError):
            raise OwnershipRefused("RUNTIME_RECEIPT_INVALID") from None
        is_claude = type(qualified) is QualifiedClaudeRuntime
        if is_claude:
            payload, receipt_json, receipt_sha256, tuple_hash = _qualified_claude_runtime_material(payload)
        else:
            payload, receipt_json, receipt_sha256, tuple_hash, observed, expires = (
                _qualified_runtime_material(payload)
            )
        now = _parse_utc_timestamp(self._now())
        if now is None:
            raise OwnershipRefused("RUNTIME_RECEIPT_INVALID")
        if is_claude:
            observed, expires = now, now + timedelta(seconds=_CLAUDE_RECEIPT_FRESHNESS_SECONDS)
        if observed > now + timedelta(seconds=5) or expires <= now:
            raise OwnershipRefused("RUNTIME_RECEIPT_STALE")
        try:
            principal = ProcessIdentity.current()
        except (OSError, ValueError, ProcessLookupError):
            raise OwnershipRefused("RUNTIME_HOST_MISMATCH") from None
        supervisor = payload["supervisor"]
        if supervisor["host_id"] != principal.host_id or supervisor["boot_id"] != principal.boot_id:
            raise OwnershipRefused("RUNTIME_HOST_MISMATCH")
        observed_at = _canonical_utc(observed)
        expires_at = _canonical_utc(expires)
        created_at = self._now()
        if _parse_utc_timestamp(created_at) is None:
            raise OwnershipRefused("RUNTIME_RECEIPT_INVALID")
        self.ensure_context_schema()
        self.ensure_authority_schema()
        with self.transaction() as tx:
            activity = self._assert_activity_binding(tx, token, activity_id)
            if activity["state"] != "active" or activity["generation"] != token.generation:
                raise OwnershipRefused("ACTIVITY_NOT_ACTIVE")
            preparation = self._activity_preparation_tx(tx, token, activity_id)
            self._assert_runtime_workspace(preparation, payload["workspace"])
            if activity["runtime_tuple_hash"] not in (None, tuple_hash):
                raise OwnershipRefused("RUNTIME_DRIFT")
            existing = tx.execute(
                "SELECT * FROM authority_runtime_receipts WHERE receipt_sha256=?",
                (receipt_sha256,),
            ).fetchone()
            expected = (
                activity_id, preparation["preparation_id"], tuple_hash,
                principal.host_id, principal.boot_id, observed_at, expires_at, receipt_json,
            )
            if existing is not None:
                recorded = tuple(existing[key] for key in (
                    "producer_activity_id", "workspace_preparation_id", "runtime_tuple_hash",
                    "host_id", "boot_id", "observed_at", "expires_at", "receipt_json",
                ))
                # A Claude qualification has no caller-provided clock. Its
                # first durable commit therefore establishes the receipt's
                # finite observation window; exact replays retain that window.
                if recorded != expected and not (
                    is_claude and recorded[:5] == expected[:5] and recorded[7] == expected[7]
                    and _parse_utc_timestamp(recorded[5]) is not None
                    and _parse_utc_timestamp(recorded[6]) is not None
                ):
                    raise OwnershipRefused("RUNTIME_RECEIPT_INVALID")
                return self._runtime_receipt_from_row(existing, reused=True)
            tx.execute(
                "INSERT INTO authority_runtime_receipts "
                "(receipt_sha256,producer_activity_id,workspace_preparation_id,runtime_tuple_hash,"
                "host_id,boot_id,observed_at,expires_at,receipt_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (receipt_sha256, *expected, created_at),
            )
            tx.execute(
                "UPDATE authority_activities SET runtime_tuple_hash=?,updated_at=? WHERE id=?",
                (tuple_hash, created_at, activity_id),
            )
            self._record_event_once_tx(
                tx, token, activity_id, "runtime-receipt:" + receipt_sha256,
                {"receipt_sha256": receipt_sha256,
                 "workspace_preparation_id": preparation["preparation_id"],
                 "runtime_tuple_hash": tuple_hash, "host_id": principal.host_id,
                 "boot_id": principal.boot_id, "observed_at": observed_at,
                 "expires_at": expires_at},
            )
            row = tx.execute(
                "SELECT * FROM authority_runtime_receipts WHERE receipt_sha256=?",
                (receipt_sha256,),
            ).fetchone()
            return self._runtime_receipt_from_row(row)

    def commit_local_check_receipt(self, token, activity_id: str, material: object) -> LocalCheckReceipt:
        """Record one owner-built sealed local-check contract, never a host receipt.

        This is intentionally separate from ``commit_runtime_receipt``: its
        accepted type remains the two qualified native runtime types.
        """
        from .local_check_runtime import LocalCheckRefused, LocalCheckMaterial, validate_local_check_material
        from .ownership import OwnershipRefused
        from process_identity import ProcessIdentity

        if type(material) is not LocalCheckMaterial:
            raise OwnershipRefused("LOCAL_CHECK_MATERIAL_INVALID")
        sealed = self.get_sealed_acceptance(repository_id=token.repository_id, run_id=token.run_id)
        if sealed is None:
            raise OwnershipRefused("LOCAL_CHECK_SEAL_REQUIRED")
        try:
            material = validate_local_check_material(material, sealed=sealed, require_current_bytes=True)
            principal = ProcessIdentity.current()
        except (LocalCheckRefused, OSError, ValueError) as error:
            raise OwnershipRefused(getattr(error, "code", "LOCAL_CHECK_MATERIAL_INVALID")) from error
        receipt_value = {"schema": "ffs.local-check-receipt/v1", "material": material.to_dict()}
        receipt_json = json.dumps(receipt_value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        receipt_sha256 = hashlib.sha256(receipt_json.encode()).hexdigest()
        created_at = self._now()
        self.ensure_authority_schema()
        with self.transaction() as tx:
            activity = self._assert_activity_binding(tx, token, activity_id)
            if activity["state"] not in {"pending", "active"} or activity["generation"] != token.generation:
                raise OwnershipRefused("ACTIVITY_NOT_ACTIVE")
            preparation = self._activity_preparation_tx(tx, token, activity_id)
            if (
                activity["input_digest"] != material.candidate_hash
                or activity["runtime_tuple_hash"] != material.runtime_identity
                or preparation["preparation_id"] != material.workspace_preparation_id
                or preparation["path"] != material.workspace
                or preparation["base_commit"] != material.expected_head
                or material.generation != token.generation
            ):
                raise OwnershipRefused("LOCAL_CHECK_BINDING_INVALID")
            # Re-read the sealed bytes under the authority writer fence.  A
            # stale check id/locator cannot survive a seal replacement.
            row = tx.execute(
                "SELECT material_json FROM authority_sealed_acceptances WHERE repository_id=? AND run_id=? "
                "AND acceptance_hash=?", (token.repository_id, token.run_id, material.acceptance_hash),
            ).fetchone()
            try:
                sealed_material = json.loads(row["material_json"])
                checks = [item for criterion in sealed_material["criteria"] for item in criterion["checks"]
                          if item["id"] == material.check_id]
            except (TypeError, KeyError, ValueError, json.JSONDecodeError):
                raise OwnershipRefused("LOCAL_CHECK_SEAL_INVALID") from None
            if len(checks) != 1 or checks[0].get("kind") != "command" or checks[0].get("locator") != material.locator:
                raise OwnershipRefused("LOCAL_CHECK_SEAL_INVALID")
            existing = tx.execute(
                "SELECT * FROM authority_local_check_receipts WHERE receipt_sha256=?", (receipt_sha256,),
            ).fetchone()
            expected = (activity_id, preparation["preparation_id"], material.acceptance_hash, material.check_id,
                        material.material_sha256, material.runtime_identity, principal.host_id, principal.boot_id,
                        token.generation, receipt_json)
            if existing is not None:
                recorded = tuple(existing[key] for key in (
                    "producer_activity_id", "workspace_preparation_id", "acceptance_hash", "check_id",
                    "material_sha256", "runtime_tuple_hash", "host_id", "boot_id", "generation", "receipt_json",
                ))
                if recorded != expected:
                    raise OwnershipRefused("LOCAL_CHECK_RECEIPT_INVALID")
                return self._local_check_receipt_from_row(existing, reused=True)
            tx.execute(
                "INSERT INTO authority_local_check_receipts "
                "(receipt_sha256,producer_activity_id,workspace_preparation_id,acceptance_hash,check_id,material_sha256,"
                "runtime_tuple_hash,host_id,boot_id,generation,receipt_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (receipt_sha256, *expected, created_at),
            )
            self._record_event_once_tx(tx, token, activity_id, "local-check-receipt:" + receipt_sha256, {
                "receipt_sha256": receipt_sha256, "acceptance_hash": material.acceptance_hash,
                "check_id": material.check_id, "material_sha256": material.material_sha256,
                "workspace_preparation_id": material.workspace_preparation_id,
            })
            row = tx.execute("SELECT * FROM authority_local_check_receipts WHERE receipt_sha256=?", (receipt_sha256,)).fetchone()
            return self._local_check_receipt_from_row(row)

    def _validate_local_check_receipt_tx(self, tx, token, activity, receipt_sha256: str,
                                         managed_input_sha256: str, principal) -> LocalCheckReceipt:
        from .local_check_runtime import LocalCheckMaterial, LocalCheckRefused, validate_local_check_material
        from .ownership import OwnershipRefused
        if activity["input_digest"] != managed_input_sha256:
            raise OwnershipRefused("MANAGED_INPUT_MISMATCH")
        row = tx.execute("SELECT * FROM authority_local_check_receipts WHERE receipt_sha256=?", (receipt_sha256,)).fetchone()
        if row is None or row["producer_activity_id"] != activity["id"]:
            raise OwnershipRefused("LOCAL_CHECK_RECEIPT_INVALID")
        try:
            payload = json.loads(row["receipt_json"])
            raw_material = dict(payload["material"])
            raw_material["argv"] = tuple(raw_material["argv"])
            raw_material["environment"] = tuple(tuple(item) for item in raw_material["environment"])
            raw_material["source_closure"] = tuple(tuple(item) for item in raw_material["source_closure"])
            material = LocalCheckMaterial(**raw_material)
            validate_local_check_material(material)
        except (TypeError, KeyError, ValueError, json.JSONDecodeError, LocalCheckRefused) as error:
            raise OwnershipRefused("LOCAL_CHECK_RECEIPT_INVALID") from error
        if (
            payload.get("schema") != "ffs.local-check-receipt/v1"
            or hashlib.sha256(row["receipt_json"].encode()).hexdigest() != receipt_sha256
            or row["material_sha256"] != material.material_sha256
            or row["workspace_preparation_id"] != material.workspace_preparation_id
            or row["runtime_tuple_hash"] != material.runtime_identity
            or activity["runtime_tuple_hash"] != material.runtime_identity
            or material.candidate_hash != managed_input_sha256
            or row["host_id"] != principal.host_id or row["boot_id"] != principal.boot_id
            or row["generation"] != token.generation or material.generation != token.generation
        ):
            raise OwnershipRefused("LOCAL_CHECK_RECEIPT_INVALID")
        preparation = self._activity_preparation_tx(tx, token, activity["id"])
        if preparation["preparation_id"] != material.workspace_preparation_id or preparation["path"] != material.workspace:
            raise OwnershipRefused("WORKSPACE_BINDING_MISMATCH")
        return self._local_check_receipt_from_row(row)

    def _validate_runtime_receipt_tx(
        self, tx, token, activity, receipt_sha256: str, managed_input_sha256: str,
        principal, now: datetime,
    ) -> RuntimeReceipt:
        from .ownership import OwnershipRefused

        if activity["input_digest"] != managed_input_sha256:
            raise OwnershipRefused("MANAGED_INPUT_MISMATCH")
        row = tx.execute(
            "SELECT * FROM authority_runtime_receipts WHERE receipt_sha256=?",
            (receipt_sha256,),
        ).fetchone()
        if row is None or row["producer_activity_id"] != activity["id"]:
            raise OwnershipRefused("RUNTIME_RECEIPT_INVALID")
        try:
            payload = json.loads(row["receipt_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            raise OwnershipRefused("RUNTIME_RECEIPT_INVALID") from None
        if payload.get("schema") == "ffs.qualified-claude-runtime/v1":
            _payload, encoded, observed_sha, tuple_hash = _qualified_claude_runtime_material(payload)
            observed = _parse_utc_timestamp(row["observed_at"])
            expires = _parse_utc_timestamp(row["expires_at"])
            if observed is None or expires is None or expires - observed != timedelta(seconds=_CLAUDE_RECEIPT_FRESHNESS_SECONDS):
                raise OwnershipRefused("RUNTIME_RECEIPT_INVALID")
            is_claude = True
        else:
            material = _qualified_runtime_material(payload)
            _payload, encoded, observed_sha, tuple_hash, observed, expires = material
            is_claude = False
        if (
            encoded != row["receipt_json"]
            or observed_sha != receipt_sha256
            or row["runtime_tuple_hash"] != tuple_hash
            or activity["runtime_tuple_hash"] != tuple_hash
            or row["host_id"] != principal.host_id
            or row["boot_id"] != principal.boot_id
            or payload["supervisor"]["host_id"] != principal.host_id
            or payload["supervisor"]["boot_id"] != principal.boot_id
            or (not is_claude and row["observed_at"] != _canonical_utc(observed))
            or (not is_claude and row["expires_at"] != _canonical_utc(expires))
            or _parse_utc_timestamp(row["observed_at"]) is None
            or _parse_utc_timestamp(row["expires_at"]) is None
        ):
            raise OwnershipRefused("RUNTIME_RECEIPT_INVALID")
        if observed > now + timedelta(seconds=5) or expires <= now:
            raise OwnershipRefused("RUNTIME_RECEIPT_STALE")
        preparation = self._activity_preparation_tx(tx, token, activity["id"])
        if preparation["preparation_id"] != row["workspace_preparation_id"]:
            raise OwnershipRefused("WORKSPACE_BINDING_MISMATCH")
        self._assert_runtime_workspace(preparation, payload["workspace"])
        return self._runtime_receipt_from_row(row)

    def transition_activity(
        self, token, activity_id: str, *, expected: str, new: str,
        result: dict | None = None, reason: str | None = None,
    ) -> Activity:
        from .ownership import OwnershipRefused
        valid = {"pending", "active", "paused", "succeeded", "failed", "aborted"}
        if expected not in valid or new not in valid:
            raise OwnershipRefused("INVALID_ACTIVITY_TRANSITION")
        if new == "succeeded" and (
            not isinstance(result, dict) or not result.get("locator") or not result.get("sha256")
        ):
            raise OwnershipRefused("EVIDENCE_REQUIRED")
        self.ensure_authority_schema()
        with self.transaction() as tx:
            return self._transition_activity_tx(tx, token, activity_id, expected=expected, new=new,
                                                result=result, reason=reason)

    def _transition_activity_tx(self, tx, token, activity_id, *, expected, new, result=None, reason=None):
        from .ownership import OwnershipRefused
        row = self._assert_activity_binding(tx, token, activity_id)
        if row["state"] in _TERMINAL_ACTIVITY_STATES:
            raise OwnershipRefused("ACTIVITY_TERMINAL")
        if row["state"] != expected:
            raise OwnershipRefused("ACTIVITY_STATE_CHANGED")
        revoked = []
        if new in _TERMINAL_ACTIVITY_STATES:
            # Revoke in the same transaction as the terminal boundary.
            # Keep identities and accounting for physical containment and
            # evidence settlement; terminality never refunds an attempt.
            revoked = [item["id"] for item in tx.execute(
                "WITH RECURSIVE descendants(id) AS (SELECT ? UNION "
                "SELECT b.activity_id FROM authority_child_bindings b "
                "JOIN descendants d ON b.parent_activity_id=d.id) "
                "SELECT i.id FROM authority_launch_intents i "
                "JOIN descendants d ON d.id=i.activity_id "
                "WHERE i.state IN ('reserved','acknowledged','released_to_execute','uncertain','reconcile_required')",
                (activity_id,),
            ).fetchall()]
            for intent_id in revoked:
                tx.execute(
                    "UPDATE authority_launch_intents SET state='reconcile_required',"
                    "permit_id=NULL,updated_at=? WHERE id=?", (self._now(), intent_id),
                )
        tx.execute(
            "UPDATE authority_activities SET state = ?, result_json = ?, updated_at = ? WHERE id = ?",
            (new, json.dumps(result, sort_keys=True) if result is not None else row["result_json"],
             self._now(), activity_id),
        )
        payload = {"run_id": token.run_id, "activity_id": activity_id,
                   "data": {"from": expected, "to": new, "reason": reason,
                            "revoked_intents": revoked}}
        tx.execute(
            "INSERT INTO control_events(event_type,payload) VALUES('activity_transition',?)",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")),),
        )
        row = tx.execute("SELECT * FROM authority_activities WHERE id = ?", (activity_id,)).fetchone()
        return self._activity_from_row(row)

    def select_activity(
        self, token, *, kind: str, input_digest: str, resume: bool, revise: bool,
    ) -> Activity:
        from .ownership import OwnershipRefused, assert_owner
        self.ensure_authority_schema()
        with self.transaction() as tx:
            assert_owner(tx, token)
            self.assert_migration_epoch_tx(tx, token.run_id)
            has_context = tx.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_runs'"
            ).fetchone()
            pointer = None if has_context is None else tx.execute(
                "SELECT r.activity_id FROM context_runs r JOIN authority_child_bindings b "
                "ON b.activity_id=r.activity_id WHERE r.repository_id=? AND r.run_id=?",
                (token.repository_id, token.run_id),
            ).fetchone()
            if pointer is not None:
                raise OwnershipRefused("PARENT_ACTIVITY_INVALID")
            latest = tx.execute(
                "SELECT * FROM authority_activities WHERE repository_id = ? AND run_id = ? "
                "AND NOT EXISTS (SELECT 1 FROM authority_child_bindings b "
                "WHERE b.activity_id=authority_activities.id) ORDER BY revision DESC LIMIT 1", (token.repository_id, token.run_id),
            ).fetchone()
            if latest is None:
                raise OwnershipRefused("ACTIVITY_NOT_FOUND")
            if latest["state"] not in {"succeeded", "failed", "aborted"}:
                if not resume:
                    raise OwnershipRefused("RESUME_REQUIRED")
                return self._activity_from_row(latest)
            if (
                not revise and latest["state"] == "succeeded" and latest["kind"] == kind
                and latest["input_digest"] == input_digest
            ):
                return self._activity_from_row(latest, reused_result=True)
            sequential = (
                latest["state"] == "succeeded"
                and latest["input_digest"] == input_digest
                and (latest["kind"], kind) in {("plan", "execute"), ("execute", "review")}
            )
            if not revise and not sequential:
                raise OwnershipRefused("REVISION_REQUIRED")
            activity_id = str(uuid.uuid4())
            revision = tx.execute(
                "SELECT COALESCE(MAX(revision),0)+1 FROM authority_activities "
                "WHERE repository_id=? AND run_id=?", (token.repository_id, token.run_id),
            ).fetchone()[0]
            now = self._now()
            request_key = f"revision:{revision}:{uuid.uuid4()}"
            tx.execute(
                "INSERT INTO authority_activities "
                "(id,repository_id,run_id,kind,input_digest,revision,state,retry_budget,"
                "remaining_retry_budget,runtime_tuple_hash,request_key,generation,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,'pending',?,?,?,?,?,?,?)",
                (activity_id, token.repository_id, token.run_id, kind, input_digest, revision,
                 latest["retry_budget"], latest["retry_budget"], None, request_key,
                 token.generation, now, now),
            )
            row = tx.execute("SELECT * FROM authority_activities WHERE id = ?", (activity_id,)).fetchone()
            return self._activity_from_row(row)

    @staticmethod
    def _record_event_once_tx(tx, token, activity_id: str, idempotency_key: str, payload: dict) -> dict:
        """Record one idempotent event inside an already fenced transaction."""
        from .ownership import OwnershipRefused
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(encoded.encode()).hexdigest()
        existing = tx.execute(
            "SELECT payload_hash,event_id FROM authority_event_keys "
            "WHERE activity_id = ? AND idempotency_key = ?",
            (activity_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                raise OwnershipRefused("IDEMPOTENCY_CONFLICT")
            return {"id": existing["event_id"], "event_type": idempotency_key, "payload": payload}
        wrapped = json.dumps(
            {"run_id": token.run_id, "activity_id": activity_id, "data": payload},
            sort_keys=True, separators=(",", ":"),
        )
        cursor = tx.execute(
            "INSERT INTO control_events(event_type,payload) VALUES(?,?)",
            (idempotency_key, wrapped),
        )
        tx.execute(
            "INSERT INTO authority_event_keys(activity_id,idempotency_key,payload_hash,event_id) "
            "VALUES(?,?,?,?)", (activity_id, idempotency_key, payload_hash, cursor.lastrowid),
        )
        return {"id": cursor.lastrowid, "event_type": idempotency_key, "payload": payload}

    def record_event_once(
        self, token, activity_id: str, idempotency_key: str, payload: dict,
    ) -> dict:
        from .ownership import OwnershipRefused
        if not isinstance(idempotency_key, str) or not idempotency_key or not isinstance(payload, dict):
            raise OwnershipRefused("IDEMPOTENCY_CONFLICT")
        self.ensure_authority_schema()
        with self.transaction() as tx:
            self._assert_activity_binding(tx, token, activity_id)
            return self._record_event_once_tx(tx, token, activity_id, idempotency_key, payload)

    def debit_budget(
        self, token, activity_id: str, amount: int, *, idempotency_key: str,
    ) -> BudgetDebit:
        from .ownership import OwnershipRefused
        if (
            isinstance(amount, bool) or not isinstance(amount, int) or amount < 0
            or amount > 9_223_372_036_854_775_807
        ):
            raise OwnershipRefused("INVALID_BUDGET_DEBIT")
        self.ensure_authority_schema()
        with self.transaction() as tx:
            activity = self._assert_activity_binding(tx, token, activity_id)
            if activity["state"] in _TERMINAL_ACTIVITY_STATES:
                raise OwnershipRefused("ACTIVITY_TERMINAL")
            self._assert_activity_ancestry(
                tx, activity_id, repository_id=token.repository_id, run_id=token.run_id,
            )
            existing = tx.execute(
                "SELECT * FROM authority_budget_debits WHERE activity_id = ? AND idempotency_key = ?",
                (activity_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["amount"] != amount or existing["run_id"] != token.run_id:
                    raise OwnershipRefused("IDEMPOTENCY_CONFLICT")
                return BudgetDebit(existing["id"], activity_id, amount, existing["remaining"])
            if amount > activity["remaining_retry_budget"]:
                raise OwnershipRefused("BUDGET_EXHAUSTED")
            remaining = activity["remaining_retry_budget"] - amount
            debit_id = str(uuid.uuid4())
            tx.execute(
                "UPDATE authority_activities SET remaining_retry_budget = ?, updated_at = ? WHERE id = ?",
                (remaining, self._now(), activity_id),
            )
            tx.execute(
                "INSERT INTO authority_budget_debits "
                "(id,repository_id,run_id,activity_id,idempotency_key,amount,remaining,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (debit_id, token.repository_id, token.run_id, activity_id, idempotency_key,
                 amount, remaining, self._now()),
            )
            return BudgetDebit(debit_id, activity_id, amount, remaining)

    def create_grant(
        self, token, *, action: str, target: str, provenance: dict,
        expires_at: str, idempotency_key: str,
    ) -> Grant:
        from .ownership import OwnerToken, OwnershipRefused, assert_owner
        if not isinstance(token, OwnerToken):
            raise OwnershipRefused("FENCE_REVOKED")
        now = _parse_utc_timestamp(self._now())
        expiry = _parse_utc_timestamp(expires_at)
        if (
            now is None or expiry is None or expiry <= now
            or expiry - now > _MAX_GRANT_LIFETIME
        ):
            raise OwnershipRefused("INVALID_GRANT")
        self.ensure_authority_schema()
        with self.transaction() as tx:
            assert_owner(tx, token)
            current_now = _parse_utc_timestamp(self._now())
            if current_now is None or expiry <= current_now:
                raise OwnershipRefused("INVALID_GRANT")
            existing = tx.execute(
                "SELECT * FROM authority_grants WHERE repository_id = ? AND run_id = ? "
                "AND idempotency_key = ?",
                (token.repository_id, token.run_id, idempotency_key),
            ).fetchone()
            encoded = json.dumps(provenance, sort_keys=True, separators=(",", ":"))
            if existing is not None:
                if any((existing["action"] != action, existing["target"] != target,
                        existing["provenance_json"] != encoded, existing["expires_at"] != expires_at)):
                    raise OwnershipRefused("IDEMPOTENCY_CONFLICT")
                return Grant(existing["id"], action, target, bool(existing["consumed"]))
            grant_id = str(uuid.uuid4())
            tx.execute(
                "INSERT INTO authority_grants "
                "(id,repository_id,run_id,action,target,provenance_json,expires_at,idempotency_key,"
                "generation,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (grant_id, token.repository_id, token.run_id, action, target, encoded, expires_at,
                 idempotency_key, token.generation, self._now()),
            )
            return Grant(grant_id, action, target, False)

    def consume_grant(
        self, token, grant_id: str, *, expected_action: str,
        expected_target: str, idempotency_key: str,
    ) -> Grant:
        from .ownership import OwnershipRefused, assert_owner
        self.ensure_authority_schema()
        with self.transaction() as tx:
            assert_owner(tx, token)
            row = tx.execute("SELECT * FROM authority_grants WHERE id = ?", (grant_id,)).fetchone()
            if row is None or row["repository_id"] != token.repository_id or row["run_id"] != token.run_id:
                raise OwnershipRefused("FENCE_REVOKED")
            if row["action"] != expected_action or row["target"] != expected_target:
                raise OwnershipRefused("GRANT_MISMATCH")
            now = _parse_utc_timestamp(self._now())
            expiry = _parse_utc_timestamp(row["expires_at"])
            if now is None or expiry is None or expiry <= now:
                raise OwnershipRefused("GRANT_EXPIRED")
            if row["consumed"]:
                if row["consume_key"] != idempotency_key:
                    raise OwnershipRefused("IDEMPOTENCY_CONFLICT")
            else:
                tx.execute(
                    "UPDATE authority_grants SET consumed = 1, consume_key = ? WHERE id = ?",
                    (idempotency_key, grant_id),
                )
            return Grant(grant_id, row["action"], row["target"], True)

    def record_decision(
        self, token, *, gate: str, status: bool, input_hashes: dict,
        evidence: dict, provenance: dict, dependencies: list[str] | None = None,
        expires_at: str,
    ) -> Decision:
        from .ownership import OwnershipRefused, assert_owner
        required = _GATE_REQUIRED_HASHES.get(gate)
        if required is None or type(status) is not bool or not isinstance(input_hashes, dict):
            raise OwnershipRefused("INVALID_DECISION")
        if any(not isinstance(input_hashes.get(key), str) or not input_hashes[key] for key in required):
            raise OwnershipRefused("INVALID_DECISION")
        if _parse_utc_timestamp(expires_at) is None:
            raise OwnershipRefused("INVALID_DECISION")
        if status and (not isinstance(evidence, dict) or not evidence.get("locator") or not evidence.get("sha256")):
            raise OwnershipRefused("EVIDENCE_REQUIRED")
        dependencies = list(dependencies or [])
        if any(not isinstance(item, str) or not item for item in dependencies):
            raise OwnershipRefused("INVALID_DECISION")
        self.ensure_authority_schema()
        with self.transaction() as tx:
            assert_owner(tx, token)
            if dependencies:
                placeholders = ",".join("?" for _ in dependencies)
                rows = tx.execute(
                    "SELECT id FROM authority_decisions WHERE repository_id = ? AND run_id = ? "
                    f"AND id IN ({placeholders})",
                    (token.repository_id, token.run_id, *dependencies),
                ).fetchall()
                if {row["id"] for row in rows} != set(dependencies):
                    raise OwnershipRefused("INVALID_DECISION")
            decision_id = str(uuid.uuid4())
            tx.execute(
                "INSERT INTO authority_decisions "
                "(id,repository_id,run_id,gate,status,input_hashes_json,evidence_json,"
                "provenance_json,dependencies_json,expires_at,generation,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (decision_id, token.repository_id, token.run_id, gate, int(status),
                 json.dumps(input_hashes, sort_keys=True, separators=(",", ":")),
                 json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                 json.dumps(provenance, sort_keys=True, separators=(",", ":")),
                 json.dumps(dependencies, separators=(",", ":")), expires_at,
                 token.generation, self._now()),
            )
            return Decision(decision_id, token.repository_id, token.run_id, gate, status,
                            dict(input_hashes), dict(evidence), dict(provenance), dependencies,
                            expires_at)

    def enumerate_decisions(self, *, gate: str | None = None):
        with self.read_transaction() as tx:
            _require_decision_expiry_schema(tx)
            if gate is None:
                rows = tx.execute("SELECT * FROM authority_decisions ORDER BY rowid").fetchall()
            else:
                rows = tx.execute(
                    "SELECT * FROM authority_decisions WHERE gate = ? ORDER BY rowid", (gate,),
                ).fetchall()
        for row in rows:
            yield Decision(row["id"], row["repository_id"], row["run_id"], row["gate"],
                           bool(row["status"]), json.loads(row["input_hashes_json"]),
                           json.loads(row["evidence_json"]), json.loads(row["provenance_json"]),
                           json.loads(row["dependencies_json"]), row["expires_at"])

    def project_gates(self, input_hashes: dict, *, run_id: str, repository_id: str) -> dict[str, bool]:
        with self.read_transaction() as tx:
            return _project_gates_snapshot(
                tx, input_hashes, run_id=run_id, repository_id=repository_id,
                now=self._now(),
            )

    @staticmethod
    def _intent_from_row(row, *, reused: bool = False) -> LaunchIntent:
        identity = None
        if row["child_pid"] is not None:
            from process_identity import ProcessIdentity
            identity = ProcessIdentity(
                row["child_host_id"], row["child_boot_id"],
                row["child_pid"], row["child_start_token"],
            )
        code = "OWNER_UNKNOWN" if row["state"] == "reconcile_required" else None
        return LaunchIntent(
            row["id"], row["activity_id"], row["attempt_ordinal"],
            row["state"], identity, code, reused,
        )

    def _request_replay_intent(
        self, tx, token, activity_id: str, request_key: str,
        request_payload: dict, token_reservation: int,
        runtime_receipt_sha256: str | None = None,
        managed_input_sha256: str | None = None,
        local_check_receipt_sha256: str | None = None,
        capacity_exempt: bool = False,
    ) -> LaunchIntent | None:
        """Resolve a managed dispatch replay from its transactionally bound event."""
        from .ownership import OwnershipRefused
        event_key = "dispatch-request:" + request_key
        binding = tx.execute(
            "SELECT payload_hash,event_id FROM authority_event_keys "
            "WHERE activity_id=? AND idempotency_key=?",
            (activity_id, event_key),
        ).fetchone()
        if binding is None:
            return None
        event = tx.execute(
            "SELECT event_type,payload FROM control_events WHERE id=?", (binding["event_id"],),
        ).fetchone()
        if event is None or event["event_type"] != event_key or not isinstance(event["payload"], str):
            raise OwnershipRefused("REQUEST_BINDING_UNKNOWN")
        try:
            wrapped = json.loads(event["payload"])
            data = wrapped["data"]
            encoded = json.dumps(data, sort_keys=True, separators=(",", ":"))
        except (KeyError, TypeError, ValueError):
            raise OwnershipRefused("REQUEST_BINDING_UNKNOWN") from None
        if (
            not isinstance(wrapped, dict)
            or wrapped.get("run_id") != token.run_id
            or wrapped.get("activity_id") != activity_id
            or hashlib.sha256(encoded.encode()).hexdigest() != binding["payload_hash"]
            or not isinstance(data, dict)
            or not isinstance(data.get("intent_id"), str)
            or not self._valid_nonnegative_integer(data.get("token_reservation"))
            or not isinstance(data.get("request"), dict)
            or type(data.get("capacity_exempt", False)) is not bool
        ):
            raise OwnershipRefused("REQUEST_BINDING_UNKNOWN")
        if (
            data["token_reservation"] != token_reservation
            or data["request"] != request_payload
            or data.get("runtime_receipt_sha256") != runtime_receipt_sha256
            or data.get("managed_input_sha256") != managed_input_sha256
            or data.get("local_check_receipt_sha256") != local_check_receipt_sha256
            or data.get("capacity_exempt", False) != capacity_exempt
        ):
            raise OwnershipRefused("IDEMPOTENCY_CONFLICT")
        intent = tx.execute(
            "SELECT * FROM authority_launch_intents WHERE id=? AND activity_id=?",
            (data["intent_id"], activity_id),
        ).fetchone()
        if intent is None or bool(intent["capacity_exempt"]) != capacity_exempt:
            raise OwnershipRefused("REQUEST_BINDING_UNKNOWN")
        cohort = tx.execute(
            "SELECT c.state FROM authority_launch_cohort_members m "
            "JOIN authority_launch_cohorts c ON c.id=m.cohort_id WHERE m.intent_id=?",
            (intent["id"],),
        ).fetchone()
        if cohort is not None and cohort["state"] != "released_to_execute":
            raise OwnershipRefused("COHORT_RECONCILIATION_REQUIRED")
        return self._intent_from_row(intent, reused=True)

    @staticmethod
    def _qualification_contract_material(
        qualification_contract: object, *, request_key: str,
        managed_input_sha256: str,
    ) -> tuple[dict, str, str, str, str]:
        """Validate the closed launch description for one fixed qualification probe."""
        from .ownership import OwnershipRefused

        if (
            not isinstance(qualification_contract, dict)
            or set(qualification_contract) != {
                "schema", "probe_contract", "qualification_envelope",
                "qualification_envelope_sha256",
            }
            or qualification_contract.get("schema") != _QUALIFICATION_CONTRACT_SCHEMA
        ):
            raise OwnershipRefused("INVALID_QUALIFICATION_CONTRACT")
        contract = dict(qualification_contract)
        probe = contract.get("probe_contract")
        envelope = contract.get("qualification_envelope")
        if (
            not isinstance(probe, dict)
            or set(probe) != {
                "probe_name", "command_sha256", "environment_sha256",
                "qualification_request_id",
            }
            or probe.get("probe_name") not in _QUALIFICATION_PROBES
            or probe.get("qualification_request_id") != request_key
            or not ControlStore._valid_digest(probe.get("command_sha256"))
            or probe["command_sha256"] != probe["command_sha256"].lower()
            or not ControlStore._valid_digest(probe.get("environment_sha256"))
            or probe["environment_sha256"] != probe["environment_sha256"].lower()
            or not isinstance(envelope, dict)
            or set(envelope) != {
                "schema", "qualification_cohort_id", "probes", "runtime_template_sha256",
                "workspace_binding", "candidate_input_sha256", "model", "effort",
                "sandbox", "roots", "policy_sha256",
            }
            or envelope.get("schema") != _QUALIFICATION_ENVELOPE_SCHEMA
        ):
            raise OwnershipRefused("INVALID_QUALIFICATION_CONTRACT")
        try:
            probe_json = json.dumps(probe, sort_keys=True, separators=(",", ":"), allow_nan=False)
            probe_sha256 = hashlib.sha256(probe_json.encode()).hexdigest()
        except (TypeError, ValueError):
            raise OwnershipRefused("INVALID_QUALIFICATION_CONTRACT") from None
        entries = envelope.get("probes")
        workspace = envelope.get("workspace_binding")
        roots = envelope.get("roots")
        if (
            not isinstance(entries, list) or len(entries) != len(_QUALIFICATION_PROBE_ORDER)
            or any(
                not isinstance(entry, dict)
                or set(entry) != {"probe_name", "probe_contract_sha256"}
                or entry.get("probe_name") != expected_name
                or not ControlStore._valid_digest(entry.get("probe_contract_sha256"))
                or entry["probe_contract_sha256"] != entry["probe_contract_sha256"].lower()
                for entry, expected_name in zip(entries, _QUALIFICATION_PROBE_ORDER)
            )
            or next(
                entry["probe_contract_sha256"] for entry in entries
                if entry["probe_name"] == probe["probe_name"]
            ) != probe_sha256
            or not isinstance(envelope.get("qualification_cohort_id"), str)
            or not envelope["qualification_cohort_id"]
            or any(
                not ControlStore._valid_digest(envelope.get(field))
                or envelope[field] != envelope[field].lower()
                for field in ("runtime_template_sha256", "candidate_input_sha256", "policy_sha256")
            )
            or envelope.get("candidate_input_sha256") != managed_input_sha256
            or not isinstance(workspace, str) or not workspace
            or not Path(workspace).is_absolute() or os.path.normpath(workspace) != workspace
            or not all(isinstance(envelope.get(field), str) and envelope[field]
                       for field in ("model", "effort", "sandbox"))
            or not isinstance(roots, list) or not roots
            or any(not isinstance(root, str) or not root or not Path(root).is_absolute()
                   or os.path.normpath(root) != root for root in roots)
            or len(set(roots)) != len(roots) or workspace not in roots
        ):
            raise OwnershipRefused("INVALID_QUALIFICATION_CONTRACT")
        try:
            envelope_json = json.dumps(
                envelope, sort_keys=True, separators=(",", ":"), allow_nan=False,
            )
            envelope_sha256 = hashlib.sha256(envelope_json.encode()).hexdigest()
            encoded = json.dumps(contract, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            raise OwnershipRefused("INVALID_QUALIFICATION_CONTRACT") from None
        if contract.get("qualification_envelope_sha256") != envelope_sha256:
            raise OwnershipRefused("INVALID_QUALIFICATION_CONTRACT")
        return contract, encoded, probe_sha256, envelope_sha256, probe["probe_name"]

    def reserve_qualification_launch(
        self, activity_id: str, token, *, request_key: str,
        qualification_contract: dict, token_reservation: int,
        managed_input_sha256: str, admission_guard=None, policy_action_id: str | None = None,
    ) -> LaunchIntent:
        """Reserve a managed inventory probe which will produce its runtime receipt."""
        from .ownership import OwnershipRefused

        if not isinstance(activity_id, str) or not activity_id:
            raise OwnershipRefused("INVALID_QUALIFICATION_CONTRACT")
        if not isinstance(request_key, str) or not request_key:
            raise OwnershipRefused("INVALID_REQUEST_KEY")
        if not self._valid_nonnegative_integer(token_reservation):
            raise OwnershipRefused("INVALID_TOKEN_RESERVATION")
        if policy_action_id is not None and (not isinstance(policy_action_id, str) or not policy_action_id):
            raise OwnershipRefused("POLICY_ACTION_REQUIRED")
        if (
            not self._valid_digest(managed_input_sha256)
            or managed_input_sha256 != managed_input_sha256.lower()
        ):
            raise OwnershipRefused("MANAGED_INPUT_MISMATCH")
        contract, contract_json, contract_sha256, envelope_sha256, probe_name = (
            self._qualification_contract_material(
            qualification_contract, request_key=request_key,
            managed_input_sha256=managed_input_sha256,
            )
        )
        self.ensure_context_schema()
        self.ensure_authority_schema()
        intent_id = None
        self._preflight_policy_clock(token)
        with self.transaction() as tx:
            activity = self._assert_activity_binding(tx, token, activity_id)
            run = tx.execute(
                "SELECT writer_version FROM context_runs WHERE repository_id=? AND run_id=?",
                (token.repository_id, token.run_id),
            ).fetchone()
            if run is None or run["writer_version"] != _MANAGED_WRITER_VERSION:
                raise OwnershipRefused("MANAGED_QUALIFICATION_REQUIRED")
            existing = tx.execute(
                "SELECT q.*,i.* FROM authority_qualification_launches q "
                "JOIN authority_launch_intents i ON i.id=q.intent_id "
                "WHERE q.activity_id=? AND q.request_key=?",
                (activity_id, request_key),
            ).fetchone()
            if existing is not None:
                accounting = tx.execute(
                    "SELECT token_reservation FROM authority_launch_accounting WHERE intent_id=?",
                    (existing["intent_id"],),
                ).fetchone()
                if (
                    existing["contract_sha256"] != contract_sha256
                    or existing["contract_json"] != contract_json
                    or existing["probe_name"] != probe_name
                    or existing["qualification_envelope_sha256"] != envelope_sha256
                    or existing["managed_input_sha256"] != managed_input_sha256
                    or existing["qualification_cohort_id"] != contract["qualification_envelope"]["qualification_cohort_id"]
                    or existing["qualification_request_id"] != request_key
                    or accounting is None or accounting["token_reservation"] != token_reservation
                ):
                    raise OwnershipRefused("IDEMPOTENCY_CONFLICT")
                replay = self._request_replay_intent(
                    tx, token, activity_id, request_key, contract, token_reservation,
                    None, managed_input_sha256, None, capacity_exempt=True,
                )
                if replay is None or replay.id != existing["intent_id"]:
                    raise OwnershipRefused("REQUEST_BINDING_UNKNOWN")
                return replay
            if admission_guard is not None:
                admission_guard(tx)
            if tx.execute(
                "SELECT 1 FROM authority_launch_cohorts WHERE repository_id=? AND run_id=? "
                "AND state!='released_to_execute' LIMIT 1",
                (token.repository_id, token.run_id),
            ).fetchone() is not None:
                raise OwnershipRefused("COHORT_RECONCILIATION_REQUIRED")
            binding = tx.execute(
                "SELECT * FROM authority_child_bindings WHERE activity_id=?", (activity_id,),
            ).fetchone()
            if (
                binding is None or binding["role"] != "inventory"
                or binding["candidate_hash"] != managed_input_sha256
                or binding["runtime_identity"] != envelope_sha256
                or binding["workspace_binding"] != contract["qualification_envelope"]["workspace_binding"]
                or activity["runtime_tuple_hash"] != envelope_sha256
                or activity["input_digest"] != managed_input_sha256
            ):
                raise OwnershipRefused("QUALIFICATION_BINDING_MISMATCH")
            preparation = tx.execute(
                "SELECT path,state,created_by_ffs,generation FROM context_workspaces "
                "WHERE preparation_id=? AND repository_id=? AND run_id=?",
                (binding["workspace_preparation_id"], token.repository_id, token.run_id),
            ).fetchone()
            if (
                preparation is None or preparation["path"] != contract["qualification_envelope"]["workspace_binding"]
                or preparation["state"] != "ready" or not preparation["created_by_ffs"]
                or preparation["generation"] != token.generation
            ):
                raise OwnershipRefused("WORKSPACE_BINDING_MISMATCH")
            if activity["state"] in _TERMINAL_ACTIVITY_STATES:
                raise OwnershipRefused("ACTIVITY_TERMINAL")
            self._assert_activity_ancestry(
                tx, activity_id, repository_id=token.repository_id, run_id=token.run_id,
            )
            active_intent = tx.execute(
                "SELECT 1 FROM authority_launch_intents WHERE activity_id=? AND state IN "
                "('reserved','acknowledged','released_to_execute','reconcile_required','uncertain') "
                "LIMIT 1", (activity_id,),
            ).fetchone()
            if active_intent is not None:
                raise OwnershipRefused("INTENT_RECONCILIATION_REQUIRED")
            if activity["remaining_retry_budget"] <= 0:
                raise OwnershipRefused("BUDGET_EXHAUSTED")
            limits = tx.execute(
                "SELECT * FROM authority_run_limits WHERE repository_id=? AND run_id=?",
                (token.repository_id, token.run_id),
            ).fetchone()
            if limits is None:
                raise OwnershipRefused("RUN_LIMITS_REQUIRED")
            uncertain = tx.execute(
                "SELECT 1 FROM authority_launch_intents i JOIN authority_activities a "
                "ON a.id=i.activity_id WHERE a.repository_id=? AND a.run_id=? AND ("
                "i.state IN ('reconcile_required','uncertain') OR "
                "(i.state IN ('reserved','acknowledged') AND i.child_pid IS NULL) OR "
                "(i.state IN ('acknowledged','released_to_execute') AND i.generation != ?)) LIMIT 1",
                (token.repository_id, token.run_id, token.generation),
            ).fetchone()
            if uncertain is not None:
                raise OwnershipRefused("INTENT_RECONCILIATION_REQUIRED")
            if limits["dispatch_used"] >= limits["dispatch_limit"]:
                raise OwnershipRefused("DISPATCH_LIMIT_EXHAUSTED")
            if token_reservation > limits["token_limit"] - limits["token_committed"]:
                raise OwnershipRefused("TOKEN_LIMIT_EXHAUSTED")
            active = self._active_capacity_launch_count_tx(tx, token)
            if active >= self._effective_worker_capacity_tx(tx, token, limits):
                raise OwnershipRefused("WORKER_CAPACITY_EXHAUSTED")
            ordinal = tx.execute(
                "SELECT COALESCE(MAX(attempt_ordinal),0)+1 FROM authority_launch_intents "
                "WHERE activity_id=?", (activity_id,),
            ).fetchone()[0]
            intent_id, now = str(uuid.uuid4()), self._now()
            tx.execute(
                "UPDATE authority_activities SET remaining_retry_budget=remaining_retry_budget-1,"
                "updated_at=? WHERE id=?", (now, activity_id),
            )
            tx.execute(
                "INSERT INTO authority_launch_intents "
                "(id,activity_id,attempt_ordinal,state,generation,capacity_exempt,created_at,updated_at) "
                "VALUES(?,?,?,'reserved',?,1,?,?)",
                (intent_id, activity_id, ordinal, token.generation, now, now),
            )
            tx.execute(
                "INSERT INTO authority_launch_accounting "
                "(intent_id,repository_id,run_id,token_reservation,created_at) VALUES(?,?,?,?,?)",
                (intent_id, token.repository_id, token.run_id, token_reservation, now),
            )
            tx.execute(
                "INSERT INTO authority_qualification_launches "
                "(intent_id,activity_id,request_key,probe_name,contract_sha256,contract_json,"
                "qualification_envelope_sha256,"
                "managed_input_sha256,qualification_cohort_id,qualification_request_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (intent_id, activity_id, request_key, probe_name, contract_sha256, contract_json,
                 envelope_sha256, managed_input_sha256,
                 contract["qualification_envelope"]["qualification_cohort_id"], request_key, now),
            )
            tx.execute(
                "UPDATE authority_run_limits SET dispatch_used=dispatch_used+1,"
                "token_committed=token_committed+?,generation=? WHERE repository_id=? AND run_id=?",
                (token_reservation, token.generation, token.repository_id, token.run_id),
            )
            if self._run_policy_tables_present_tx(tx):
                self._policy_bind_launch_tx(tx, token, action_id=policy_action_id, intent_id=intent_id,
                                           request_key=request_key, input_hash=contract_sha256,
                                           expected_action="qualification")
            dispatch = {
                "intent_id": intent_id, "token_reservation": token_reservation, "capacity_exempt": True,
                "request": contract, "managed_input_sha256": managed_input_sha256,
                "qualification_contract_sha256": contract_sha256,
                "qualification_envelope_sha256": envelope_sha256,
                "qualification_cohort_id": contract["qualification_envelope"]["qualification_cohort_id"],
                "qualification_request_id": request_key,
            }
            self._record_event_once_tx(
                tx, token, activity_id, "dispatch-request:" + request_key, dispatch,
            )
            tx.execute(
                "INSERT INTO control_events(event_type,payload) VALUES('qualification_launch_reserved',?)",
                (json.dumps({
                    "repository_id": token.repository_id, "run_id": token.run_id,
                    "activity_id": activity_id,
                    "data": {"intent_id": intent_id, "request_key": request_key,
                             "contract_sha256": contract_sha256,
                             "qualification_envelope_sha256": envelope_sha256,
                             "probe_name": probe_name,
                             "qualification_cohort_id": contract["qualification_envelope"]["qualification_cohort_id"],
                             "token_reservation": token_reservation},
                }, sort_keys=True, separators=(",", ":")),),
            )
            self._fault("reserve_qualification_launch", "after_write_before_commit")
        self._fault("reserve_qualification_launch", "after_commit_before_return")
        with self.read_transaction() as tx:
            row = tx.execute(
                "SELECT * FROM authority_launch_intents WHERE id=?", (intent_id,),
            ).fetchone()
        return self._intent_from_row(row)

    def promote_qualified_activity(
        self, token, activity_id: str, *, qualification_request_key: str,
        expected_contract_hashes: dict, runtime_identity: str,
        final_contract_hash: str, role: str, observation_evidence: dict,
    ) -> Activity:
        """Atomically promote one four-probe inventory binding to its final role."""
        from .ownership import OwnershipRefused

        if (
            not isinstance(activity_id, str) or not activity_id
            or not isinstance(qualification_request_key, str) or not qualification_request_key
            or not isinstance(expected_contract_hashes, dict)
            or set(expected_contract_hashes) != _QUALIFICATION_PROBES
            or any(
                not self._valid_digest(expected_contract_hashes.get(name))
                or expected_contract_hashes[name] != expected_contract_hashes[name].lower()
                for name in _QUALIFICATION_PROBE_ORDER
            )
            or not self._valid_digest(runtime_identity)
            or runtime_identity != runtime_identity.lower()
            or not self._valid_digest(final_contract_hash)
            or final_contract_hash != final_contract_hash.lower()
            or role not in {"worker", "reviewer"}
        ):
            raise OwnershipRefused("INVALID_QUALIFICATION_PROMOTION")
        evidence = self._verified_evidence(observation_evidence)
        hashes_json = json.dumps(
            expected_contract_hashes, sort_keys=True, separators=(",", ":"),
        )
        evidence_json = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        self.ensure_context_schema()
        self.ensure_authority_schema()
        with self.transaction() as tx:
            activity = self._assert_activity_binding(tx, token, activity_id)
            existing = tx.execute(
                "SELECT * FROM authority_qualification_promotions WHERE activity_id=?",
                (activity_id,),
            ).fetchone()
            if existing is not None:
                expected = (
                    qualification_request_key, hashes_json, runtime_identity,
                    final_contract_hash, role, evidence_json,
                )
                actual = tuple(existing[key] for key in (
                    "qualification_request_key", "expected_contract_hashes_json",
                    "runtime_identity", "final_contract_hash", "role",
                    "observation_evidence_json",
                ))
                binding = tx.execute(
                    "SELECT runtime_identity,contract_hash,role,workspace_preparation_id "
                    "FROM authority_child_bindings WHERE activity_id=?", (activity_id,),
                ).fetchone()
                workspace = None if binding is None else tx.execute(
                    "SELECT child_role FROM context_workspaces WHERE preparation_id=?",
                    (binding["workspace_preparation_id"],),
                ).fetchone()
                if (
                    actual != expected or activity["runtime_tuple_hash"] != runtime_identity
                    or binding is None or binding["runtime_identity"] != runtime_identity
                    or binding["contract_hash"] != final_contract_hash or binding["role"] != role
                    or workspace is None or workspace["child_role"] != role
                ):
                    raise OwnershipRefused("IDEMPOTENCY_CONFLICT")
                return self._activity_from_row(activity, reused_result=True)
            launches = tx.execute(
                "SELECT q.*,i.state AS intent_state,i.completion_status,i.token_usage,"
                "i.completion_evidence_json,a.token_reservation "
                "FROM authority_qualification_launches q "
                "JOIN authority_launch_intents i ON i.id=q.intent_id "
                "JOIN authority_launch_accounting a ON a.intent_id=i.id "
                "WHERE q.activity_id=? ORDER BY q.created_at,q.probe_name",
                (activity_id,),
            ).fetchall()
            if (
                len(launches) != len(_QUALIFICATION_PROBE_ORDER)
                or {row["probe_name"] for row in launches} != _QUALIFICATION_PROBES
                or any(
                    row["qualification_cohort_id"] != qualification_request_key
                    or row["intent_state"] != "completed_succeeded"
                    or row["completion_status"] != "succeeded"
                    or row["contract_sha256"] != expected_contract_hashes[row["probe_name"]]
                    for row in launches
                )
                or len({row["qualification_envelope_sha256"] for row in launches}) != 1
            ):
                raise OwnershipRefused("QUALIFICATION_INCOMPLETE")
            envelope_sha256 = launches[0]["qualification_envelope_sha256"]
            for row in launches:
                try:
                    contract = json.loads(row["contract_json"])
                    material = self._qualification_contract_material(
                        contract, request_key=row["request_key"],
                        managed_input_sha256=row["managed_input_sha256"],
                    )
                except (TypeError, ValueError, json.JSONDecodeError, OwnershipRefused):
                    raise OwnershipRefused("QUALIFICATION_BINDING_MISMATCH") from None
                if (
                    material[1] != row["contract_json"]
                    or material[2] != row["contract_sha256"]
                    or material[3] != envelope_sha256 or material[4] != row["probe_name"]
                    or contract["qualification_envelope"]["policy_sha256"] != final_contract_hash
                ):
                    raise OwnershipRefused("QUALIFICATION_BINDING_MISMATCH")
                replay = self._request_replay_intent(
                    tx, token, activity_id, row["request_key"], contract,
                    row["token_reservation"], None, row["managed_input_sha256"], None, capacity_exempt=True,
                )
                if replay is None or replay.id != row["intent_id"]:
                    raise OwnershipRefused("QUALIFICATION_BINDING_MISMATCH")
                completion_events = tx.execute(
                    "SELECT payload FROM control_events WHERE event_type='launch_completed'"
                ).fetchall()
                matches = []
                for event in completion_events:
                    try:
                        wrapped = json.loads(event["payload"])
                        data = wrapped["data"]
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if (
                        wrapped.get("run_id") == token.run_id
                        and wrapped.get("repository_id") == token.repository_id
                        and wrapped.get("activity_id") == activity_id
                        and data.get("intent_id") == row["intent_id"]
                    ):
                        matches.append(data)
                try:
                    completion_evidence = json.loads(row["completion_evidence_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    raise OwnershipRefused("QUALIFICATION_BINDING_MISMATCH") from None
                if matches != [{
                    "intent_id": row["intent_id"], "status": "succeeded",
                    "settlement_generation": token.generation,
                    "token_usage": row["token_usage"], "evidence": completion_evidence,
                }]:
                    raise OwnershipRefused("QUALIFICATION_BINDING_MISMATCH")
            if tx.execute(
                "SELECT 1 FROM authority_launch_intents WHERE activity_id=? AND state IN "
                "('reserved','acknowledged','released_to_execute','reconcile_required','uncertain') "
                "LIMIT 1", (activity_id,),
            ).fetchone() is not None:
                raise OwnershipRefused("INTENT_RECONCILIATION_REQUIRED")
            binding = tx.execute(
                "SELECT * FROM authority_child_bindings WHERE activity_id=?", (activity_id,),
            ).fetchone()
            workspace = None if binding is None else tx.execute(
                "SELECT * FROM context_workspaces WHERE preparation_id=? AND repository_id=? "
                "AND run_id=?", (binding["workspace_preparation_id"], token.repository_id,
                                token.run_id),
            ).fetchone()
            if (
                binding is None or binding["role"] != "inventory"
                or binding["runtime_identity"] != envelope_sha256
                or activity["runtime_tuple_hash"] != envelope_sha256
                or workspace is None or workspace["child_role"] != "inventory"
                or workspace["path"] != binding["workspace_binding"]
                or workspace["state"] != "ready" or not workspace["created_by_ffs"]
                or workspace["generation"] != token.generation
            ):
                raise OwnershipRefused("QUALIFICATION_BINDING_MISMATCH")
            now = self._now()
            tx.execute(
                "UPDATE authority_activities SET runtime_tuple_hash=?,updated_at=? WHERE id=?",
                (runtime_identity, now, activity_id),
            )
            tx.execute(
                "UPDATE authority_child_bindings SET runtime_identity=?,contract_hash=?,role=? "
                "WHERE activity_id=?",
                (runtime_identity, final_contract_hash, role, activity_id),
            )
            tx.execute(
                "UPDATE context_workspaces SET child_role=?,updated_at=? WHERE preparation_id=?",
                (role, now, binding["workspace_preparation_id"]),
            )
            tx.execute(
                "INSERT INTO authority_qualification_promotions "
                "(activity_id,qualification_request_key,expected_contract_hashes_json,"
                "runtime_identity,final_contract_hash,role,observation_evidence_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (activity_id, qualification_request_key, hashes_json, runtime_identity,
                 final_contract_hash, role, evidence_json, now),
            )
            self._record_event_once_tx(
                tx, token, activity_id,
                "qualification-promotion:" + qualification_request_key,
                {"qualification_request_key": qualification_request_key,
                 "expected_contract_hashes": expected_contract_hashes,
                 "runtime_identity": runtime_identity,
                 "final_contract_hash": final_contract_hash, "role": role,
                 "observation_evidence": evidence},
            )
            row = tx.execute(
                "SELECT * FROM authority_activities WHERE id=?", (activity_id,),
            ).fetchone()
            return self._activity_from_row(row)

    @staticmethod
    def _normalize_cohort_requests(members: Iterable[LaunchCohortRequest | dict]) -> tuple[tuple[dict, ...], str]:
        """Return an order-independent, exact canonical cohort description."""
        from .ownership import OwnershipRefused

        normalized = []
        try:
            supplied = tuple(members)
        except (TypeError, ValueError):
            raise OwnershipRefused("INVALID_COHORT_REQUEST") from None
        if not supplied:
            raise OwnershipRefused("EMPTY_LAUNCH_COHORT")
        required = {
            "activity_id", "request_key", "request_payload", "token_reservation",
            "runtime_receipt_sha256", "managed_input_sha256",
        }
        for member in supplied:
            if isinstance(member, LaunchCohortRequest):
                value = {
                    "activity_id": member.activity_id,
                    "request_key": member.request_key,
                    "request_payload": member.request_payload,
                    "token_reservation": member.token_reservation,
                    "runtime_receipt_sha256": member.runtime_receipt_sha256,
                    "managed_input_sha256": member.managed_input_sha256,
                }
            elif isinstance(member, dict) and set(member) == required:
                value = dict(member)
            else:
                raise OwnershipRefused("INVALID_COHORT_REQUEST")
            if (
                not isinstance(value["activity_id"], str) or not value["activity_id"]
                or not isinstance(value["request_key"], str) or not value["request_key"]
                or not isinstance(value["request_payload"], dict)
                or not ControlStore._valid_nonnegative_integer(value["token_reservation"])
                or not ControlStore._valid_digest(value["runtime_receipt_sha256"])
                or value["runtime_receipt_sha256"] != value["runtime_receipt_sha256"].lower()
                or not ControlStore._valid_digest(value["managed_input_sha256"])
                or value["managed_input_sha256"] != value["managed_input_sha256"].lower()
            ):
                raise OwnershipRefused("INVALID_COHORT_REQUEST")
            try:
                payload_json = json.dumps(
                    value["request_payload"], sort_keys=True, separators=(",", ":"),
                    allow_nan=False,
                )
            except (TypeError, ValueError):
                raise OwnershipRefused("INVALID_COHORT_REQUEST") from None
            value["request_payload_json"] = payload_json
            value["request_payload_sha256"] = hashlib.sha256(payload_json.encode()).hexdigest()
            normalized.append(value)
        if (
            len({item["activity_id"] for item in normalized}) != len(normalized)
            or len({item["request_key"] for item in normalized}) != len(normalized)
        ):
            raise OwnershipRefused("COHORT_MEMBERSHIP_CONFLICT")
        normalized.sort(key=lambda item: (item["request_key"], item["activity_id"]))
        material = [{key: item[key] for key in (
            "activity_id", "request_key", "request_payload", "token_reservation",
            "runtime_receipt_sha256", "managed_input_sha256",
        )} for item in normalized]
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return tuple(normalized), hashlib.sha256(encoded.encode()).hexdigest()

    def _launch_cohort_from_tx(self, tx, cohort_row, *, reused: bool = False) -> LaunchCohort:
        from .ownership import OwnershipRefused

        rows = tx.execute(
            "SELECT m.*,i.* FROM authority_launch_cohort_members m "
            "JOIN authority_launch_intents i ON i.id=m.intent_id "
            "WHERE m.cohort_id=? ORDER BY m.member_ordinal",
            (cohort_row["id"],),
        ).fetchall()
        if len(rows) != cohort_row["member_count"]:
            raise OwnershipRefused("COHORT_BINDING_UNKNOWN")
        return LaunchCohort(
            cohort_row["id"], cohort_row["request_key"], cohort_row["state"],
            tuple(self._intent_from_row(row, reused=reused) for row in rows), reused,
        )

    def reserve_launch_cohort(
        self, token, *, request_key: str,
        members: Iterable[LaunchCohortRequest | dict], admission_guard=None,
        policy_action_ids: dict[str, str] | None = None,
    ) -> LaunchCohort:
        """Atomically reserve and debit a complete managed launch cohort."""
        from .ownership import OwnershipRefused, assert_owner
        from process_identity import ProcessIdentity

        if not isinstance(request_key, str) or not request_key:
            raise OwnershipRefused("INVALID_REQUEST_KEY")
        normalized, membership_sha256 = self._normalize_cohort_requests(members)
        if policy_action_ids is not None and (
            not isinstance(policy_action_ids, dict)
            or set(policy_action_ids) != {item["request_key"] for item in normalized}
            or any(not isinstance(value, str) or not value for value in policy_action_ids.values())
        ):
            raise OwnershipRefused("POLICY_ACTION_REQUIRED")
        try:
            principal = ProcessIdentity.current()
        except (OSError, ValueError, ProcessLookupError):
            raise OwnershipRefused("RUNTIME_HOST_MISMATCH") from None
        observed_now = _parse_utc_timestamp(self._now())
        if observed_now is None:
            raise OwnershipRefused("RUNTIME_RECEIPT_INVALID")
        self.ensure_context_schema()
        self.ensure_authority_schema()
        cohort_id = None
        self._preflight_policy_clock(token)
        with self.transaction() as tx:
            assert_owner(tx, token)
            existing = tx.execute(
                "SELECT * FROM authority_launch_cohorts WHERE repository_id=? AND run_id=? "
                "AND request_key=?",
                (token.repository_id, token.run_id, request_key),
            ).fetchone()
            if existing is not None:
                retained = tx.execute(
                    "SELECT * FROM authority_launch_cohort_members WHERE cohort_id=? "
                    "ORDER BY member_ordinal", (existing["id"],),
                ).fetchall()
                expected = [(
                    item["activity_id"], item["request_key"], item["request_payload_json"],
                    item["request_payload_sha256"], item["token_reservation"],
                    item["runtime_receipt_sha256"], item["managed_input_sha256"],
                ) for item in normalized]
                actual = [(
                    row["activity_id"], row["request_key"], row["request_payload_json"],
                    row["request_payload_sha256"], row["token_reservation"],
                    row["runtime_receipt_sha256"], row["managed_input_sha256"],
                ) for row in retained]
                if existing["generation"] != token.generation:
                    raise OwnershipRefused("COHORT_RECONCILIATION_REQUIRED")
                if (
                    existing["membership_sha256"] != membership_sha256
                    or existing["member_count"] != len(normalized) or actual != expected
                ):
                    raise OwnershipRefused("COHORT_MEMBERSHIP_CONFLICT")
                return self._launch_cohort_from_tx(tx, existing, reused=True)
            unresolved = tx.execute(
                "SELECT 1 FROM authority_launch_cohorts WHERE repository_id=? AND run_id=? "
                "AND state!='released_to_execute' LIMIT 1",
                (token.repository_id, token.run_id),
            ).fetchone()
            if unresolved is not None:
                raise OwnershipRefused("COHORT_RECONCILIATION_REQUIRED")
            limits = tx.execute(
                "SELECT * FROM authority_run_limits WHERE repository_id=? AND run_id=?",
                (token.repository_id, token.run_id),
            ).fetchone()
            if limits is None:
                raise OwnershipRefused("RUN_LIMITS_REQUIRED")
            count = len(normalized)
            token_total = sum(item["token_reservation"] for item in normalized)
            if limits["dispatch_used"] > limits["dispatch_limit"] - count:
                raise OwnershipRefused("DISPATCH_LIMIT_EXHAUSTED")
            if token_total > limits["token_limit"] - limits["token_committed"]:
                raise OwnershipRefused("TOKEN_LIMIT_EXHAUSTED")
            active = self._active_capacity_launch_count_tx(tx, token)
            if active > self._effective_worker_capacity_tx(tx, token, limits) - count:
                raise OwnershipRefused("WORKER_CAPACITY_EXHAUSTED")
            if admission_guard is not None:
                admission_guard(tx)
            validated = []
            for item in normalized:
                activity = self._assert_activity_binding(tx, token, item["activity_id"])
                if activity["state"] in _TERMINAL_ACTIVITY_STATES:
                    raise OwnershipRefused("ACTIVITY_TERMINAL")
                self._assert_activity_ancestry(
                    tx, item["activity_id"], repository_id=token.repository_id,
                    run_id=token.run_id,
                )
                if activity["runtime_tuple_hash"] is None:
                    raise OwnershipRefused("RUNTIME_REQUIRED")
                if activity["remaining_retry_budget"] <= 0:
                    raise OwnershipRefused("BUDGET_EXHAUSTED")
                if tx.execute(
                    "SELECT 1 FROM authority_launch_intents WHERE activity_id=? AND state IN "
                    "('reserved','acknowledged','released_to_execute','reconcile_required','uncertain')",
                    (item["activity_id"],),
                ).fetchone() is not None:
                    raise OwnershipRefused("COHORT_RECONCILIATION_REQUIRED")
                if tx.execute(
                    "SELECT 1 FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?",
                    (item["activity_id"], "dispatch-request:" + item["request_key"]),
                ).fetchone() is not None:
                    raise OwnershipRefused("IDEMPOTENCY_CONFLICT")
                self._validate_runtime_receipt_tx(
                    tx, token, activity, item["runtime_receipt_sha256"],
                    item["managed_input_sha256"], principal, observed_now,
                )
                validated.append((item, activity))
            cohort_id, now = str(uuid.uuid4()), self._now()
            tx.execute(
                "INSERT INTO authority_launch_cohorts "
                "(id,repository_id,run_id,request_key,membership_sha256,member_count,state,"
                "generation,created_at,updated_at) VALUES(?,?,?,?,?,?,'reserved',?,?,?)",
                (cohort_id, token.repository_id, token.run_id, request_key,
                 membership_sha256, count, token.generation, now, now),
            )
            for ordinal, (item, _activity) in enumerate(validated):
                intent_id = str(uuid.uuid4())
                attempt = tx.execute(
                    "SELECT COALESCE(MAX(attempt_ordinal),0)+1 FROM authority_launch_intents "
                    "WHERE activity_id=?", (item["activity_id"],),
                ).fetchone()[0]
                tx.execute(
                    "UPDATE authority_activities SET remaining_retry_budget=remaining_retry_budget-1,"
                    "updated_at=? WHERE id=?", (now, item["activity_id"]),
                )
                tx.execute(
                    "INSERT INTO authority_launch_intents "
                    "(id,activity_id,attempt_ordinal,state,generation,created_at,updated_at) "
                    "VALUES(?,?,?,'reserved',?,?,?)",
                    (intent_id, item["activity_id"], attempt, token.generation, now, now),
                )
                tx.execute(
                    "INSERT INTO authority_launch_accounting "
                    "(intent_id,repository_id,run_id,token_reservation,created_at) VALUES(?,?,?,?,?)",
                    (intent_id, token.repository_id, token.run_id,
                     item["token_reservation"], now),
                )
                if self._run_policy_tables_present_tx(tx):
                    self._policy_bind_launch_tx(
                        tx, token,
                        action_id=None if policy_action_ids is None else policy_action_ids[item["request_key"]],
                        intent_id=intent_id,
                        request_key=item["request_key"], input_hash=item["request_payload_sha256"],
                        expected_action="execute",
                    )
                tx.execute(
                    "INSERT INTO authority_launch_cohort_members "
                    "(cohort_id,member_ordinal,intent_id,activity_id,request_key,request_payload_json,"
                    "request_payload_sha256,token_reservation,runtime_receipt_sha256,managed_input_sha256) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (cohort_id, ordinal, intent_id, item["activity_id"], item["request_key"],
                     item["request_payload_json"], item["request_payload_sha256"],
                     item["token_reservation"], item["runtime_receipt_sha256"],
                     item["managed_input_sha256"]),
                )
                dispatch = {
                    "intent_id": intent_id, "token_reservation": item["token_reservation"],
                    "capacity_exempt": False,
                    "request": item["request_payload"],
                    "runtime_receipt_sha256": item["runtime_receipt_sha256"],
                    "managed_input_sha256": item["managed_input_sha256"],
                    "cohort_id": cohort_id,
                }
                self._record_event_once_tx(
                    tx, token, item["activity_id"],
                    "dispatch-request:" + item["request_key"], dispatch,
                )
            tx.execute(
                "UPDATE authority_run_limits SET dispatch_used=dispatch_used+?,"
                "token_committed=token_committed+?,generation=? WHERE repository_id=? AND run_id=?",
                (count, token_total, token.generation, token.repository_id, token.run_id),
            )
            tx.execute(
                "INSERT INTO control_events(event_type,payload) VALUES('launch_cohort_reserved',?)",
                (json.dumps({
                    "repository_id": token.repository_id, "run_id": token.run_id,
                    "data": {"cohort_id": cohort_id, "request_key": request_key,
                             "membership_sha256": membership_sha256,
                             "member_count": count, "token_reservation": token_total},
                }, sort_keys=True, separators=(",", ":")),),
            )
            self._fault("reserve_launch_cohort", "after_write_before_commit")
        self._fault("reserve_launch_cohort", "after_commit_before_return")
        with self.read_transaction() as tx:
            row = tx.execute(
                "SELECT * FROM authority_launch_cohorts WHERE id=?", (cohort_id,),
            ).fetchone()
            return self._launch_cohort_from_tx(tx, row)

    def reserve_launch(
        self, activity_id: str, token, *, token_reservation: int = 0,
        request_key: str | None = None, request_payload: dict | None = None,
        admission_guard=None, runtime_receipt_sha256: str | None = None,
        managed_input_sha256: str | None = None,
        local_check_receipt_sha256: str | None = None,
        managed_outer_capacity_exempt: bool = False,
        policy_action_id: str | None = None,
    ) -> LaunchIntent:
        from .ownership import OwnershipRefused
        if not self._valid_nonnegative_integer(token_reservation):
            raise OwnershipRefused("INVALID_TOKEN_RESERVATION")
        if request_key is not None and (not isinstance(request_key, str) or not request_key):
            raise OwnershipRefused("INVALID_REQUEST_KEY")
        if request_key is None and request_payload is not None:
            raise OwnershipRefused("INVALID_REQUEST_KEY")
        if request_key is not None and request_payload is not None and not isinstance(request_payload, dict):
            raise OwnershipRefused("INVALID_REQUEST_KEY")
        for value, code in (
            (runtime_receipt_sha256, "RUNTIME_RECEIPT_INVALID"),
            (local_check_receipt_sha256, "LOCAL_CHECK_RECEIPT_INVALID"),
            (managed_input_sha256, "MANAGED_INPUT_MISMATCH"),
        ):
            if value is not None and (not self._valid_digest(value) or value != value.lower()):
                raise OwnershipRefused(code)
        if runtime_receipt_sha256 is not None and local_check_receipt_sha256 is not None:
            raise OwnershipRefused("RECEIPT_TRANSPORT_CONFLICT")
        if runtime_receipt_sha256 is None and local_check_receipt_sha256 is None and managed_input_sha256 is not None:
            raise OwnershipRefused("RUNTIME_RECEIPT_REQUIRED")
        if (runtime_receipt_sha256 is not None or local_check_receipt_sha256 is not None) and managed_input_sha256 is None:
            raise OwnershipRefused("MANAGED_INPUT_REQUIRED")
        if type(managed_outer_capacity_exempt) is not bool:
            raise OwnershipRefused("INVALID_CAPACITY_CLASSIFICATION")
        if policy_action_id is not None and (not isinstance(policy_action_id, str) or not policy_action_id):
            raise OwnershipRefused("POLICY_ACTION_REQUIRED")
        request_payload = {} if request_key is not None and request_payload is None else request_payload
        principal = None
        now = None
        if runtime_receipt_sha256 is not None or local_check_receipt_sha256 is not None:
            from process_identity import ProcessIdentity
            try:
                principal = ProcessIdentity.current()
            except (OSError, ValueError, ProcessLookupError):
                raise OwnershipRefused("RUNTIME_HOST_MISMATCH") from None
            now = _parse_utc_timestamp(self._now())
            if now is None:
                raise OwnershipRefused("RUNTIME_RECEIPT_INVALID")
        self.ensure_authority_schema()
        self._preflight_policy_clock(token)
        with self.transaction() as tx:
            activity = self._assert_activity_binding(tx, token, activity_id)
            has_context = tx.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_runs'"
            ).fetchone()
            context_columns = set() if has_context is None else {
                row[1] for row in tx.execute("PRAGMA table_info(context_runs)")
            }
            run = None if "writer_version" not in context_columns else tx.execute(
                "SELECT writer_version FROM context_runs WHERE repository_id=? AND run_id=?",
                (token.repository_id, token.run_id),
            ).fetchone()
            managed = run is not None and run["writer_version"] == _MANAGED_WRITER_VERSION
            if managed_outer_capacity_exempt:
                root = None if run is None else tx.execute(
                    "SELECT activity_id FROM context_runs WHERE repository_id=? AND run_id=? "
                    "AND writer_version=?",
                    (token.repository_id, token.run_id, _MANAGED_WRITER_VERSION),
                ).fetchone()
                binding = tx.execute(
                    "SELECT parent_activity_id FROM authority_child_bindings WHERE activity_id=?",
                    (activity_id,),
                ).fetchone()
                receipt = tx.execute(
                    "SELECT 1 FROM authority_runtime_receipts WHERE producer_activity_id=?",
                    (activity_id,),
                ).fetchone()
                if (
                    not managed or request_key is None or root is None or binding is None
                    or binding["parent_activity_id"] != root["activity_id"]
                    or not request_key.startswith("managed-host:")
                    or not request_key.endswith(":launch") or receipt is None
                ):
                    raise OwnershipRefused("CAPACITY_EXEMPTION_INVALID")
            if managed:
                if request_key is None:
                    raise OwnershipRefused("MANAGED_REQUEST_KEY_REQUIRED")
                if runtime_receipt_sha256 is None and local_check_receipt_sha256 is None:
                    raise OwnershipRefused("RUNTIME_RECEIPT_REQUIRED")
                if managed_input_sha256 is None:
                    raise OwnershipRefused("MANAGED_INPUT_REQUIRED")
            incomplete_cohort = tx.execute(
                "SELECT 1 FROM authority_launch_cohorts WHERE repository_id=? AND run_id=? "
                "AND state!='released_to_execute' LIMIT 1",
                (token.repository_id, token.run_id),
            ).fetchone()
            if incomplete_cohort is not None:
                raise OwnershipRefused("COHORT_RECONCILIATION_REQUIRED")
            if managed and request_key is not None:
                replay = self._request_replay_intent(
                    tx, token, activity_id, request_key, request_payload, token_reservation,
                    runtime_receipt_sha256, managed_input_sha256, local_check_receipt_sha256, managed_outer_capacity_exempt,
                )
                if replay is not None:
                    return replay
            if admission_guard is not None:
                admission_guard(tx)
            if not managed and request_key is not None:
                replay = self._request_replay_intent(
                    tx, token, activity_id, request_key, request_payload, token_reservation,
                    runtime_receipt_sha256, managed_input_sha256, local_check_receipt_sha256, managed_outer_capacity_exempt,
                )
                if replay is not None:
                    return replay
            if runtime_receipt_sha256 is not None:
                self._validate_runtime_receipt_tx(
                    tx, token, activity, runtime_receipt_sha256,
                    managed_input_sha256, principal, now,
                )
            elif local_check_receipt_sha256 is not None:
                self._validate_local_check_receipt_tx(
                    tx, token, activity, local_check_receipt_sha256,
                    managed_input_sha256, principal,
                )
            if activity["state"] in _TERMINAL_ACTIVITY_STATES:
                raise OwnershipRefused("ACTIVITY_TERMINAL")
            self._assert_activity_ancestry(
                tx, activity_id, repository_id=token.repository_id, run_id=token.run_id,
            )
            existing = tx.execute(
                "SELECT * FROM authority_launch_intents WHERE activity_id = ? "
                "AND state IN ('reserved','acknowledged','released_to_execute','reconcile_required','uncertain') "
                "ORDER BY attempt_ordinal DESC LIMIT 1", (activity_id,),
            ).fetchone()
            if existing is not None:
                accounting = tx.execute(
                    "SELECT token_reservation FROM authority_launch_accounting WHERE intent_id=?",
                    (existing["id"],),
                ).fetchone()
                # Legacy intents were not created under the aggregate
                # authority.  Treat them as uncertain rather than assigning a
                # post-hoc allowance or allowing a second spawn.
                if accounting is None:
                    limited = tx.execute(
                        "SELECT 1 FROM authority_run_limits WHERE repository_id=? AND run_id=?",
                        (token.repository_id, token.run_id),
                    ).fetchone()
                    if limited is None:
                        return self._intent_from_row(existing, reused=True)
                    tx.execute(
                        "UPDATE authority_launch_intents SET state='reconcile_required',"
                        "updated_at=? WHERE id=?",
                        (self._now(), existing["id"]),
                    )
                    existing = tx.execute(
                        "SELECT * FROM authority_launch_intents WHERE id=?", (existing["id"],),
                    ).fetchone()
                    return self._intent_from_row(existing, reused=True)
                if accounting["token_reservation"] != token_reservation:
                    raise OwnershipRefused("IDEMPOTENCY_CONFLICT")
                # Ordinary replay cannot transfer an intent or its permit to
                # a successor owner. Only explicit recovery may reconcile it.
                return self._intent_from_row(existing, reused=True)
            if activity["runtime_tuple_hash"] is None:
                raise OwnershipRefused("RUNTIME_REQUIRED")
            if activity["remaining_retry_budget"] <= 0:
                raise OwnershipRefused("BUDGET_EXHAUSTED")
            limits = tx.execute(
                "SELECT * FROM authority_run_limits WHERE repository_id=? AND run_id=?",
                (token.repository_id, token.run_id),
            ).fetchone()
            if limits is not None:
                uncertain = tx.execute(
                    "SELECT i.state,i.generation,i.child_pid FROM authority_launch_intents i "
                    "JOIN authority_activities a ON a.id=i.activity_id "
                    "WHERE a.repository_id=? AND a.run_id=? AND ("
                    "i.state IN ('reconcile_required','uncertain') OR "
                    "(i.state IN ('reserved','acknowledged') AND i.child_pid IS NULL) OR "
                    "(i.state IN ('acknowledged','released_to_execute') AND i.generation != ?)"
                    ") LIMIT 1",
                    (token.repository_id, token.run_id, token.generation),
                ).fetchone()
                if uncertain is not None:
                    # A new supervisor cannot infer whether an old spawn made
                    # it through its handshake.  recover_intent performs the
                    # identity probe and records the current fence first.
                    raise OwnershipRefused("INTENT_RECONCILIATION_REQUIRED")
                if limits["dispatch_used"] >= limits["dispatch_limit"]:
                    raise OwnershipRefused("DISPATCH_LIMIT_EXHAUSTED")
                if token_reservation > limits["token_limit"] - limits["token_committed"]:
                    raise OwnershipRefused("TOKEN_LIMIT_EXHAUSTED")
                active = self._active_capacity_launch_count_tx(tx, token)
                if active >= self._effective_worker_capacity_tx(tx, token, limits):
                    raise OwnershipRefused("WORKER_CAPACITY_EXHAUSTED")
            ordinal = tx.execute(
                "SELECT COALESCE(MAX(attempt_ordinal),0)+1 FROM authority_launch_intents "
                "WHERE activity_id = ?", (activity_id,),
            ).fetchone()[0]
            intent_id = str(uuid.uuid4())
            now = self._now()
            tx.execute(
                "UPDATE authority_activities SET remaining_retry_budget = remaining_retry_budget - 1, "
                "updated_at = ? WHERE id = ?", (now, activity_id),
            )
            tx.execute(
                "INSERT INTO authority_launch_intents "
                "(id,activity_id,attempt_ordinal,state,generation,capacity_exempt,created_at,updated_at) "
                "VALUES(?,?,?,'reserved',?,?,?,?)",
                (intent_id, activity_id, ordinal, token.generation,
                 int(managed_outer_capacity_exempt), now, now),
            )
            if limits is not None:
                tx.execute(
                    "UPDATE authority_run_limits SET dispatch_used=dispatch_used+1,"
                    "token_committed=token_committed+?,generation=? "
                    "WHERE repository_id=? AND run_id=?",
                    (token_reservation, token.generation, token.repository_id, token.run_id),
                )
                tx.execute(
                    "INSERT INTO authority_launch_accounting "
                    "(intent_id,repository_id,run_id,token_reservation,created_at) VALUES(?,?,?,?,?)",
                    (intent_id, token.repository_id, token.run_id, token_reservation, now),
                )
                if self._run_policy_tables_present_tx(tx):
                    self._policy_bind_launch_tx(
                        tx, token, action_id=policy_action_id, intent_id=intent_id,
                        request_key=request_key,
                        input_hash=None if request_payload is None else hashlib.sha256(
                            json.dumps(request_payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
                        ).hexdigest(),
                        expected_action="managed_outer" if managed_outer_capacity_exempt else None,
                    )
                launch_data = {"intent_id": intent_id,
                               "token_reservation": token_reservation,
                               "capacity_exempt": managed_outer_capacity_exempt}
                if runtime_receipt_sha256 is not None:
                    launch_data.update({"runtime_receipt_sha256": runtime_receipt_sha256,
                                        "managed_input_sha256": managed_input_sha256})
                if local_check_receipt_sha256 is not None:
                    launch_data.update({"local_check_receipt_sha256": local_check_receipt_sha256,
                                        "managed_input_sha256": managed_input_sha256})
                tx.execute(
                    "INSERT INTO control_events(event_type,payload) VALUES('launch_reserved',?)",
                    (json.dumps({"repository_id": token.repository_id, "run_id": token.run_id,
                                 "activity_id": activity_id, "data": launch_data},
                                sort_keys=True, separators=(",", ":")),),
                )
            if request_key is not None:
                dispatch = {"intent_id": intent_id, "token_reservation": token_reservation,
                            "request": request_payload,
                            "capacity_exempt": managed_outer_capacity_exempt}
                if runtime_receipt_sha256 is not None:
                    dispatch.update({"runtime_receipt_sha256": runtime_receipt_sha256,
                                     "managed_input_sha256": managed_input_sha256})
                if local_check_receipt_sha256 is not None:
                    dispatch.update({"local_check_receipt_sha256": local_check_receipt_sha256,
                                     "managed_input_sha256": managed_input_sha256})
                self._record_event_once_tx(
                    tx, token, activity_id, "dispatch-request:" + request_key,
                    dispatch,
                )
            self._fault("reserve_launch", "after_write_before_commit")
        self._fault("reserve_launch", "after_commit_before_return")
        with self.read_transaction() as tx:
            row = tx.execute("SELECT * FROM authority_launch_intents WHERE id = ?", (intent_id,)).fetchone()
        return self._intent_from_row(row)

    def acknowledge_child(
        self, intent_id: str, token, process_identity, *, admission_guard=None,
        _cohort_id: str | None = None,
    ) -> ChildAcknowledgement:
        from .ownership import OwnershipRefused
        from process_identity import LIVE, ProcessIdentity, probe_direct_parent, probe_identity
        if not isinstance(process_identity, ProcessIdentity):
            raise OwnershipRefused("CHILD_IDENTITY_MISMATCH")
        self.ensure_authority_schema()
        with self.read_transaction() as tx:
            observed = tx.execute(
                "SELECT i.*,a.repository_id,a.run_id,a.state AS activity_state "
                "FROM authority_launch_intents i "
                "JOIN authority_activities a ON a.id=i.activity_id WHERE i.id = ?", (intent_id,),
            ).fetchone()
        if observed is None:
            raise OwnershipRefused("FENCE_REVOKED")
        membership = None
        with self.read_transaction() as tx:
            membership = tx.execute(
                "SELECT m.*,c.state AS cohort_state,c.generation AS cohort_generation "
                "FROM authority_launch_cohort_members m JOIN authority_launch_cohorts c "
                "ON c.id=m.cohort_id WHERE m.intent_id=?", (intent_id,),
            ).fetchone()
        if _cohort_id is None:
            if membership is not None and membership["cohort_state"] != "released_to_execute":
                raise OwnershipRefused("COHORT_RECONCILIATION_REQUIRED")
        elif membership is None or membership["cohort_id"] != _cohort_id:
            raise OwnershipRefused("COHORT_MEMBER_MISMATCH")
        if (
            observed["activity_state"] in _TERMINAL_ACTIVITY_STATES
            and observed["state"] != "released_to_execute"
        ):
            raise OwnershipRefused("ACTIVITY_TERMINAL")
        existing_identity = self._intent_from_row(observed).child_identity
        if existing_identity is None:
            if probe_direct_parent(process_identity, ProcessIdentity.current()) != LIVE:
                raise OwnershipRefused("CHILD_IDENTITY_MISMATCH")
        elif existing_identity != process_identity:
            native = probe_identity(existing_identity)
            status = native
            if self.liveness_probe is not None:
                try:
                    injected = self.liveness_probe(existing_identity)
                except Exception:
                    injected = "UNKNOWN"
                if injected == "UNKNOWN" or native == "UNKNOWN":
                    status = "UNKNOWN"
            if status == "UNKNOWN" or observed["state"] == "reconcile_required":
                raise OwnershipRefused("OWNER_UNKNOWN")
            raise OwnershipRefused("CHILD_ALREADY_BOUND")
        acknowledgement_id = observed["acknowledgement_id"] or str(uuid.uuid4())
        with self.transaction() as tx:
            activity = self._assert_activity_binding(tx, token, observed["activity_id"])
            if admission_guard is not None:
                admission_guard(tx)
            row = tx.execute("SELECT * FROM authority_launch_intents WHERE id = ?", (intent_id,)).fetchone()
            if row is None:
                raise OwnershipRefused("FENCE_REVOKED")
            cohort_member = tx.execute(
                "SELECT m.*,c.state AS cohort_state,c.generation AS cohort_generation "
                "FROM authority_launch_cohort_members m JOIN authority_launch_cohorts c "
                "ON c.id=m.cohort_id WHERE m.intent_id=?", (intent_id,),
            ).fetchone()
            if _cohort_id is not None and (
                cohort_member is None or cohort_member["cohort_id"] != _cohort_id
                or cohort_member["cohort_generation"] != token.generation
                or cohort_member["cohort_state"] not in {"reserved", "acknowledged"}
            ):
                raise OwnershipRefused("COHORT_RECONCILIATION_REQUIRED")
            if row["generation"] != token.generation:
                raise OwnershipRefused("INTENT_RECONCILIATION_REQUIRED")
            if (
                activity["state"] in _TERMINAL_ACTIVITY_STATES
                and row["state"] != "released_to_execute"
            ):
                raise OwnershipRefused("ACTIVITY_TERMINAL")
            current_identity = self._intent_from_row(row).child_identity
            if current_identity is not None and current_identity != process_identity:
                raise OwnershipRefused("CHILD_ALREADY_BOUND")
            if row["state"] == "reconcile_required":
                raise OwnershipRefused("OWNER_UNKNOWN")
            if row["state"] not in {"reserved", "acknowledged", "released_to_execute"}:
                raise OwnershipRefused("CHILD_ALREADY_BOUND")
            tx.execute(
                "UPDATE authority_launch_intents SET state = CASE WHEN state='reserved' THEN 'acknowledged' ELSE state END, "
                "child_host_id=?,child_boot_id=?,child_pid=?,child_start_token=?,"
                "acknowledgement_id=?,updated_at=? WHERE id=?",
                (process_identity.host_id, process_identity.boot_id,
                 process_identity.pid, process_identity.start_token, acknowledgement_id,
                 self._now(), intent_id),
            )
            if _cohort_id is not None:
                now = self._now()
                if cohort_member["acknowledgement_id"] not in (None, acknowledgement_id):
                    raise OwnershipRefused("CHILD_IDENTITY_MISMATCH")
                retained_identity = (
                    cohort_member["child_host_id"], cohort_member["child_boot_id"],
                    cohort_member["child_pid"], cohort_member["child_start_token"],
                )
                supplied_identity = (
                    process_identity.host_id, process_identity.boot_id,
                    process_identity.pid, process_identity.start_token,
                )
                if cohort_member["acknowledgement_id"] is not None and retained_identity != supplied_identity:
                    raise OwnershipRefused("CHILD_IDENTITY_MISMATCH")
                tx.execute(
                    "UPDATE authority_launch_cohort_members SET acknowledgement_id=?,"
                    "child_host_id=?,child_boot_id=?,child_pid=?,child_start_token=?,"
                    "acknowledged_at=COALESCE(acknowledged_at,?) WHERE cohort_id=? AND intent_id=?",
                    (acknowledgement_id, *supplied_identity, now, _cohort_id, intent_id),
                )
                remaining = tx.execute(
                    "SELECT COUNT(*) FROM authority_launch_cohort_members "
                    "WHERE cohort_id=? AND acknowledgement_id IS NULL", (_cohort_id,),
                ).fetchone()[0]
                tx.execute(
                    "UPDATE authority_launch_cohorts SET state=?,updated_at=? WHERE id=?",
                    ("acknowledged" if remaining == 0 else "reserved", now, _cohort_id),
                )
            self._fault("acknowledge_child", "after_write_before_commit")
        self._fault("acknowledge_child", "after_commit_before_return")
        return ChildAcknowledgement(acknowledgement_id, intent_id, process_identity)

    def acknowledge_cohort_child(
        self, cohort_id: str, intent_id: str, token, process_identity, *, admission_guard=None,
    ) -> ChildAcknowledgement:
        """Record one exact child/session ACK as a member of its retained cohort."""
        from .ownership import OwnershipRefused
        if not isinstance(cohort_id, str) or not cohort_id:
            raise OwnershipRefused("COHORT_MEMBER_MISMATCH")
        return self.acknowledge_child(
            intent_id, token, process_identity, admission_guard=admission_guard,
            _cohort_id=cohort_id,
        )

    def acknowledge_monitored_child(
        self, intent_id: str, token, monitor_identity, child_identity, binding: dict,
        *, _cohort_id: str | None = None, admission_guard=None,
    ) -> ChildAcknowledgement:
        """Bind one supervisor->monitor->native chain in the normal ACK event.

        This is deliberately an internal transport primitive: it cannot create
        an intent, transfer a permit, or accept an arbitrary descendant.  The
        normal direct-child acknowledgement API remains the public path.
        """
        from .ownership import OwnershipRefused
        from process_identity import LIVE, ProcessIdentity, probe_direct_parent
        if (not isinstance(monitor_identity, ProcessIdentity)
                or not isinstance(child_identity, ProcessIdentity)
                or not isinstance(binding, dict)):
            raise OwnershipRefused("CHILD_IDENTITY_MISMATCH")
        owner_identity = ProcessIdentity.current()
        if (probe_direct_parent(monitor_identity, owner_identity) != LIVE
                or probe_direct_parent(child_identity, monitor_identity) != LIVE):
            raise OwnershipRefused("CHILD_IDENTITY_MISMATCH")
        required = {"transport", "monitor", "native", "initial_head", "workspace", "workspace_identity", "streams"}
        if set(binding) != required or binding["transport"] != "supervisor-monitor-v1":
            raise OwnershipRefused("CHILD_IDENTITY_MISMATCH")
        if binding["monitor"] != {
            "host_id": monitor_identity.host_id, "boot_id": monitor_identity.boot_id,
            "pid": monitor_identity.pid, "start_token": monitor_identity.start_token,
        } or binding["native"] != {
            "host_id": child_identity.host_id, "boot_id": child_identity.boot_id,
            "pid": child_identity.pid, "start_token": child_identity.start_token,
        }:
            raise OwnershipRefused("CHILD_IDENTITY_MISMATCH")
        acknowledgement_id = str(uuid.uuid4())
        self.ensure_authority_schema()
        with self.transaction() as tx:
            if admission_guard is not None:
                admission_guard(tx)
            row = tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (intent_id,)).fetchone()
            if row is None:
                raise OwnershipRefused("FENCE_REVOKED")
            cohort_member = tx.execute(
                "SELECT m.*,c.state AS cohort_state,c.generation AS cohort_generation "
                "FROM authority_launch_cohort_members m JOIN authority_launch_cohorts c "
                "ON c.id=m.cohort_id WHERE m.intent_id=?", (intent_id,),
            ).fetchone()
            if _cohort_id is None:
                if cohort_member is not None and cohort_member["cohort_state"] != "released_to_execute":
                    raise OwnershipRefused("COHORT_RECONCILIATION_REQUIRED")
            elif (
                cohort_member is None or cohort_member["cohort_id"] != _cohort_id
                or cohort_member["cohort_generation"] != token.generation
                or cohort_member["cohort_state"] not in {"reserved", "acknowledged"}
            ):
                raise OwnershipRefused("COHORT_RECONCILIATION_REQUIRED")
            activity = self._assert_activity_binding(tx, token, row["activity_id"])
            dispatches = tx.execute(
                "SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
                "WHERE k.activity_id=? AND k.idempotency_key LIKE 'dispatch-request:%'", (row["activity_id"],),
            ).fetchall()
            try:
                dispatch = next(json.loads(item["payload"])["data"] for item in dispatches
                                if json.loads(item["payload"])["data"].get("intent_id") == intent_id)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, StopIteration):
                raise OwnershipRefused("CHILD_IDENTITY_MISMATCH") from None
            if (not isinstance(dispatch.get("request"), dict)
                    or dispatch["request"].get("transport") != "supervisor-monitor-v1"
                    or row["generation"] != token.generation or row["state"] != "reserved"
                    or activity["state"] in _TERMINAL_ACTIVITY_STATES):
                raise OwnershipRefused("INTENT_RECONCILIATION_REQUIRED")
            tx.execute(
                "UPDATE authority_launch_intents SET state='acknowledged',child_host_id=?,child_boot_id=?,"
                "child_pid=?,child_start_token=?,acknowledgement_id=?,updated_at=? WHERE id=?",
                (child_identity.host_id, child_identity.boot_id, child_identity.pid,
                 child_identity.start_token, acknowledgement_id, self._now(), intent_id),
            )
            if _cohort_id is not None:
                now = self._now()
                supplied_identity = (
                    child_identity.host_id, child_identity.boot_id,
                    child_identity.pid, child_identity.start_token,
                )
                tx.execute(
                    "UPDATE authority_launch_cohort_members SET acknowledgement_id=?,"
                    "child_host_id=?,child_boot_id=?,child_pid=?,child_start_token=?,"
                    "acknowledged_at=COALESCE(acknowledged_at,?) WHERE cohort_id=? AND intent_id=?",
                    (acknowledgement_id, *supplied_identity, now, _cohort_id, intent_id),
                )
                remaining = tx.execute(
                    "SELECT COUNT(*) FROM authority_launch_cohort_members "
                    "WHERE cohort_id=? AND acknowledgement_id IS NULL", (_cohort_id,),
                ).fetchone()[0]
                tx.execute(
                    "UPDATE authority_launch_cohorts SET state=?,updated_at=? WHERE id=?",
                    ("acknowledged" if remaining == 0 else "reserved", now, _cohort_id),
                )
            payload = {**binding, "acknowledgement_id": acknowledgement_id,
                       "intent_id": intent_id, "issuing_generation": row["generation"]}
            self._record_event_once_tx(tx, token, row["activity_id"], "child-ack:" + intent_id, payload)
            self._fault("acknowledge_child", "after_write_before_commit")
        self._fault("acknowledge_child", "after_commit_before_return")
        return ChildAcknowledgement(acknowledgement_id, intent_id, child_identity)

    def acknowledge_monitored_cohort_child(
        self, cohort_id: str, intent_id: str, token, monitor_identity, child_identity,
        binding: dict, *, admission_guard=None,
    ) -> ChildAcknowledgement:
        """Record a monitored native child ACK in its exact launch cohort."""
        from .ownership import OwnershipRefused
        if not isinstance(cohort_id, str) or not cohort_id:
            raise OwnershipRefused("COHORT_MEMBER_MISMATCH")
        return self.acknowledge_monitored_child(
            intent_id, token, monitor_identity, child_identity, binding,
            _cohort_id=cohort_id, admission_guard=admission_guard,
        )

    def _record_launch_clock_tx(self, tx, token, activity_id: str, intent_id: str) -> None:
        key = "launch-release-clock:" + intent_id
        if tx.execute("SELECT 1 FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?",
                      (activity_id, key)).fetchone() is None:
            from process_identity import ProcessIdentity
            self._record_event_once_tx(tx, token, activity_id, key, {
                "intent_id": intent_id, "boot_id": ProcessIdentity.current().boot_id,
                "monotonic_ns": time.monotonic_ns(), "wall_time_ns": time.time_ns(),
            })

    def authorize_child(self, acknowledgement, token, *, admission_guard=None,
                        clock_boot_id: str | None = None, clock_monotonic_ns: int | None = None) -> ChildPermit:
        from .ownership import OwnershipRefused
        if not isinstance(acknowledgement, ChildAcknowledgement):
            raise OwnershipRefused("CHILD_IDENTITY_MISMATCH")
        self.ensure_authority_schema()
        self._preflight_policy_clock(
            token, clock_boot_id=clock_boot_id, clock_monotonic_ns=clock_monotonic_ns,
        )
        clock_ok = True
        with self.transaction() as tx:
            row = tx.execute(
                "SELECT i.*,a.repository_id,a.run_id FROM authority_launch_intents i "
                "JOIN authority_activities a ON a.id=i.activity_id WHERE i.id = ?",
                (acknowledgement.intent_id,),
            ).fetchone()
            if row is None:
                raise OwnershipRefused("FENCE_REVOKED")
            cohort = tx.execute(
                "SELECT c.state FROM authority_launch_cohort_members m "
                "JOIN authority_launch_cohorts c ON c.id=m.cohort_id WHERE m.intent_id=?",
                (row["id"],),
            ).fetchone()
            if cohort is not None and cohort["state"] != "released_to_execute":
                raise OwnershipRefused("COHORT_RELEASE_REQUIRED")
            activity = self._assert_activity_binding(tx, token, row["activity_id"])
            if admission_guard is not None:
                admission_guard(tx)
            if row["generation"] != token.generation:
                raise OwnershipRefused("INTENT_RECONCILIATION_REQUIRED")
            self._assert_activity_ancestry(
                tx, row["activity_id"], repository_id=token.repository_id, run_id=token.run_id,
            )
            if activity["state"] not in {"pending", "active"}:
                raise OwnershipRefused("ACTIVITY_TERMINAL")
            identity = self._intent_from_row(row).child_identity
            if row["acknowledgement_id"] != acknowledgement.id or identity != acknowledgement.child_identity:
                raise OwnershipRefused("CHILD_IDENTITY_MISMATCH")
            if row["state"] == "reconcile_required":
                raise OwnershipRefused("OWNER_UNKNOWN")
            if row["state"] not in {"acknowledged", "released_to_execute"}:
                raise OwnershipRefused("CHILD_NOT_ACKNOWLEDGED")
            if self._run_policy_tables_present_tx(tx):
                budget = tx.execute("SELECT 1 FROM authority_run_policy_budgets WHERE repository_id=? AND run_id=?",
                                    (token.repository_id, token.run_id)).fetchone()
                if budget is not None:
                    clock_error = self._policy_authorize_clock_tx(tx, token, clock_boot_id, clock_monotonic_ns)
                    clock_ok = clock_error is None
            if not clock_ok:
                # Deliberately leave this transaction normally so the durable
                # uncertainty update commits; refusal occurs below.
                pass
            else:
                permit_id = row["permit_id"] or str(uuid.uuid4())
                tx.execute(
                    "UPDATE authority_activities SET state='active',generation=?,updated_at=? WHERE id=?",
                    (token.generation, self._now(), row["activity_id"]),
                )
                tx.execute(
                    "UPDATE authority_launch_intents SET state='released_to_execute',"
                    "permit_id=?,updated_at=? WHERE id=?",
                    (permit_id, self._now(), row["id"]),
                )
                self._record_launch_clock_tx(tx, token, row["activity_id"], row["id"])
                self._fault("authorize_child", "after_write_before_commit")
        if not clock_ok:
            raise OwnershipRefused(clock_error)
        self._fault("authorize_child", "after_commit_before_return")
        return ChildPermit(permit_id, acknowledgement.intent_id, True)

    def authorize_launch_cohort(
        self, cohort_id: str, token, *, admission_guard=None,
        clock_boot_id: str | None = None, clock_monotonic_ns: int | None = None,
    ) -> tuple[ChildPermit, ...]:
        """Atomically release every member after every exact child ACK is live."""
        from .ownership import OwnershipRefused, assert_owner
        from process_identity import LIVE, ProcessIdentity, probe_identity

        if not isinstance(cohort_id, str) or not cohort_id:
            raise OwnershipRefused("COHORT_BINDING_UNKNOWN")
        self.ensure_authority_schema()
        self._preflight_policy_clock(
            token, clock_boot_id=clock_boot_id, clock_monotonic_ns=clock_monotonic_ns,
        )
        try:
            principal = ProcessIdentity.current()
        except (OSError, ValueError, ProcessLookupError):
            raise OwnershipRefused("CHILD_IDENTITY_MISMATCH") from None
        with self.transaction() as tx:
            assert_owner(tx, token)
        with self.read_transaction() as tx:
            cohort = tx.execute(
                "SELECT * FROM authority_launch_cohorts WHERE id=?", (cohort_id,),
            ).fetchone()
            observed = tx.execute(
                "SELECT m.*,i.state AS intent_state,i.generation AS intent_generation,"
                "i.acknowledgement_id AS intent_acknowledgement_id,i.permit_id,"
                "i.child_host_id AS intent_host_id,i.child_boot_id AS intent_boot_id,"
                "i.child_pid AS intent_pid,i.child_start_token AS intent_start_token,"
                "i.completion_status,a.state AS activity_state "
                "FROM authority_launch_cohort_members m "
                "JOIN authority_launch_intents i ON i.id=m.intent_id "
                "JOIN authority_activities a ON a.id=m.activity_id "
                "WHERE m.cohort_id=? ORDER BY m.member_ordinal", (cohort_id,),
            ).fetchall()
        if (
            cohort is None or cohort["repository_id"] != token.repository_id
            or cohort["run_id"] != token.run_id or len(observed) != cohort["member_count"]
        ):
            raise OwnershipRefused("COHORT_BINDING_UNKNOWN")
        if cohort["state"] == "released_to_execute":
            if any(row["intent_state"] != "released_to_execute" or not row["permit_id"] for row in observed):
                raise OwnershipRefused("COHORT_BINDING_UNKNOWN")
            return tuple(ChildPermit(row["permit_id"], row["intent_id"], True) for row in observed)
        if cohort["generation"] != token.generation or cohort["state"] == "reconcile_required":
            raise OwnershipRefused("COHORT_RECONCILIATION_REQUIRED")
        terminal = [row for row in observed if row["completion_status"] is not None
                    or row["intent_state"].startswith("completed_")
                    or row["intent_state"] == "closed_dead"]
        if terminal:
            raise OwnershipRefused("COHORT_MEMBER_FAILED")
        if any(
            row["intent_state"] != "acknowledged" or row["acknowledgement_id"] is None
            or row["intent_acknowledgement_id"] != row["acknowledgement_id"]
        for row in observed):
            raise OwnershipRefused("COHORT_ACK_INCOMPLETE")
        for row in observed:
            retained = (
                row["child_host_id"], row["child_boot_id"], row["child_pid"],
                row["child_start_token"],
            )
            intent_identity = (
                row["intent_host_id"], row["intent_boot_id"], row["intent_pid"],
                row["intent_start_token"],
            )
            if retained != intent_identity or retained[0] != principal.host_id or retained[1] != principal.boot_id:
                raise OwnershipRefused("CHILD_IDENTITY_MISMATCH")
            try:
                identity = ProcessIdentity(*retained)
                native = probe_identity(identity)
                injected = LIVE if self.liveness_probe is None else self.liveness_probe(identity)
            except Exception:
                raise OwnershipRefused("CHILD_IDENTITY_MISMATCH") from None
            if native != LIVE or injected != LIVE:
                raise OwnershipRefused("CHILD_IDENTITY_MISMATCH")
        permits = []
        clock_ok = True
        with self.transaction() as tx:
            current_cohort = tx.execute(
                "SELECT * FROM authority_launch_cohorts WHERE id=?", (cohort_id,),
            ).fetchone()
            if (
                current_cohort is None or current_cohort["generation"] != token.generation
                or current_cohort["state"] != "acknowledged"
            ):
                raise OwnershipRefused("COHORT_RECONCILIATION_REQUIRED")
            current = tx.execute(
                "SELECT m.*,i.state AS intent_state,i.generation AS intent_generation,"
                "i.acknowledgement_id AS intent_acknowledgement_id,i.permit_id,"
                "i.child_host_id AS intent_host_id,i.child_boot_id AS intent_boot_id,"
                "i.child_pid AS intent_pid,i.child_start_token AS intent_start_token,"
                "i.completion_status,a.state AS activity_state "
                "FROM authority_launch_cohort_members m "
                "JOIN authority_launch_intents i ON i.id=m.intent_id "
                "JOIN authority_activities a ON a.id=m.activity_id "
                "WHERE m.cohort_id=? ORDER BY m.member_ordinal", (cohort_id,),
            ).fetchall()
            def signature(row):
                return tuple(row[key] for key in (
                    "intent_id", "intent_state", "intent_generation", "acknowledgement_id",
                    "intent_acknowledgement_id", "child_host_id", "child_boot_id", "child_pid",
                    "child_start_token", "intent_host_id", "intent_boot_id", "intent_pid",
                    "intent_start_token", "completion_status", "activity_state",
                ))
            if len(current) != len(observed) or [signature(row) for row in current] != [signature(row) for row in observed]:
                raise OwnershipRefused("COHORT_RECONCILIATION_REQUIRED")
            if admission_guard is not None:
                admission_guard(tx)
            if self._run_policy_tables_present_tx(tx):
                budget = tx.execute("SELECT 1 FROM authority_run_policy_budgets WHERE repository_id=? AND run_id=?",
                                    (token.repository_id, token.run_id)).fetchone()
                if budget is not None:
                    clock_error = self._policy_authorize_clock_tx(tx, token, clock_boot_id, clock_monotonic_ns)
                    clock_ok = clock_error is None
            if not clock_ok:
                # A concurrent/reversed observation commits only its clock
                # fence. Prevent the following release loop from minting any
                # permits in that transaction.
                current = ()
            now = self._now()
            for row in current:
                activity = self._assert_activity_binding(tx, token, row["activity_id"])
                self._assert_activity_ancestry(
                    tx, row["activity_id"], repository_id=token.repository_id,
                    run_id=token.run_id,
                )
                if activity["state"] not in {"pending", "active"}:
                    raise OwnershipRefused("COHORT_MEMBER_FAILED")
                permit_id = str(uuid.uuid4())
                tx.execute(
                    "UPDATE authority_activities SET state='active',generation=?,updated_at=? WHERE id=?",
                    (token.generation, now, row["activity_id"]),
                )
                tx.execute(
                    "UPDATE authority_launch_intents SET state='released_to_execute',"
                    "permit_id=?,updated_at=? WHERE id=? AND state='acknowledged'",
                    (permit_id, now, row["intent_id"]),
                )
                self._record_launch_clock_tx(tx, token, row["activity_id"], row["intent_id"])
                permits.append(ChildPermit(permit_id, row["intent_id"], True))
            if clock_ok:
                tx.execute(
                    "UPDATE authority_launch_cohorts SET state='released_to_execute',updated_at=? WHERE id=?",
                    (now, cohort_id),
                )
                tx.execute(
                    "INSERT INTO control_events(event_type,payload) VALUES('launch_cohort_released',?)",
                    (json.dumps({
                        "repository_id": token.repository_id, "run_id": token.run_id,
                        "data": {"cohort_id": cohort_id,
                                 "intent_ids": [permit.intent_id for permit in permits],
                                 "permit_ids": [permit.id for permit in permits]},
                    }, sort_keys=True, separators=(",", ":")),),
                )
                self._fault("authorize_launch_cohort", "after_write_before_commit")
        if not clock_ok:
            raise OwnershipRefused(clock_error)
        self._fault("authorize_launch_cohort", "after_commit_before_return")
        return tuple(permits)

    def release_launch_cohort(
        self, cohort_id: str, token, *, admission_guard=None,
        clock_boot_id: str | None = None, clock_monotonic_ns: int | None = None,
    ) -> tuple[ChildPermit, ...]:
        """Supervisor-facing spelling for atomic cohort authorization/release."""
        return self.authorize_launch_cohort(
            cohort_id, token, admission_guard=admission_guard,
            clock_boot_id=clock_boot_id, clock_monotonic_ns=clock_monotonic_ns,
        )

    def reconcile_launch_cohort(self, cohort_id: str, token) -> LaunchCohort:
        """Retain every debit and make a pre-release crash explicitly uncertain."""
        from .ownership import OwnershipRefused, assert_owner
        if not isinstance(cohort_id, str) or not cohort_id:
            raise OwnershipRefused("COHORT_BINDING_UNKNOWN")
        self.ensure_authority_schema()
        with self.transaction() as tx:
            assert_owner(tx, token)
            cohort = tx.execute(
                "SELECT * FROM authority_launch_cohorts WHERE id=? AND repository_id=? AND run_id=?",
                (cohort_id, token.repository_id, token.run_id),
            ).fetchone()
            if cohort is None:
                raise OwnershipRefused("COHORT_BINDING_UNKNOWN")
            if cohort["state"] == "released_to_execute":
                return self._launch_cohort_from_tx(tx, cohort, reused=True)
            now = self._now()
            tx.execute(
                "UPDATE authority_launch_cohorts SET state='reconcile_required',updated_at=? WHERE id=?",
                (now, cohort_id),
            )
            tx.execute(
                "UPDATE authority_launch_intents SET state='reconcile_required',updated_at=? "
                "WHERE id IN (SELECT intent_id FROM authority_launch_cohort_members WHERE cohort_id=?) "
                "AND state IN ('reserved','acknowledged')",
                (now, cohort_id),
            )
            retained = tx.execute(
                "SELECT * FROM authority_launch_cohorts WHERE id=?", (cohort_id,),
            ).fetchone()
            return self._launch_cohort_from_tx(tx, retained)

    def recover_intent(self, intent_id: str, token) -> LaunchIntent:
        from .ownership import OwnershipRefused
        from process_identity import DEAD, LIVE, UNKNOWN, probe_identity
        self.ensure_authority_schema()
        with self.read_transaction() as tx:
            row = tx.execute(
                "SELECT i.*,a.repository_id,a.run_id FROM authority_launch_intents i "
                "JOIN authority_activities a ON a.id=i.activity_id WHERE i.id = ?", (intent_id,),
            ).fetchone()
        if row is None:
            raise OwnershipRefused("FENCE_REVOKED")
        identity = self._intent_from_row(row).child_identity
        status = None
        if identity is not None:
            native = probe_identity(identity)
            status = native
            if self.liveness_probe is not None:
                try:
                    injected = self.liveness_probe(identity)
                except Exception:
                    injected = UNKNOWN
                if injected not in (LIVE, DEAD, UNKNOWN) or injected == UNKNOWN or native == UNKNOWN:
                    status = UNKNOWN
                elif native == LIVE:
                    status = LIVE
                elif native == DEAD:
                    status = DEAD if injected == DEAD else UNKNOWN
        with self.transaction() as tx:
            self._assert_activity_binding(tx, token, row["activity_id"])
            current = tx.execute("SELECT * FROM authority_launch_intents WHERE id = ?", (intent_id,)).fetchone()
            if current is None:
                raise OwnershipRefused("FENCE_REVOKED")
            # Native liveness describes the snapshot probed outside the write
            # transaction. A concurrent ACK, permit, or completion invalidates
            # that observation; do not apply it to newer durable evidence.
            if any(current[column] != row[column] for column in current.keys()):
                raise OwnershipRefused("INTENT_RECONCILIATION_REQUIRED")
            new_state = current["state"]
            if current["completion_status"] is not None:
                # Process death does not settle missing telemetry or erase a
                # committed completion. Recovery preserves its classification
                # and accounting; only evidenced completion may settle it.
                pass
            elif identity is None and current["state"] in {"reserved", "acknowledged"}:
                # No child identity is an uncertain post-spawn boundary, never
                # evidence that no process exists.  Its reservation remains
                # debited and another sibling cannot launch until reconciled.
                new_state = "reconcile_required"
            elif current["state"] == "acknowledged":
                # A pre-authorisation child may be waiting on a supervisor
                # that no longer exists.  Even a live PID is not enough to
                # infer its intended operation after a restart.
                new_state = "closed_dead" if status == DEAD else "reconcile_required"
            elif current["state"] == "released_to_execute":
                # A permit is fenced to its issuing owner.  A later owner
                # generation cannot silently adopt even a live old child: it
                # lacks a new binding/authorisation receipt.  Dead/unknown
                # children likewise retain their debit until completion.
                if status != LIVE or current["generation"] != token.generation:
                    new_state = "reconcile_required"
            elif identity is not None:
                if status == DEAD:
                    new_state = "closed_dead"
                elif status == UNKNOWN:
                    new_state = "reconcile_required"
            # Observation by a successor never reissues an old permit.  Keep
            # the issuing generation on the intent; completion records the
            # settling owner's generation in its event instead.
            tx.execute(
                "UPDATE authority_launch_intents SET state=?,updated_at=? WHERE id=?",
                (new_state, self._now(), intent_id),
            )
            current = tx.execute("SELECT * FROM authority_launch_intents WHERE id = ?", (intent_id,)).fetchone()
            return self._intent_from_row(current)

    @staticmethod
    def _monitored_ack_binding_tx(tx, row) -> bool:
        """Recognize only the internal two-edge ACK recorded with this intent."""
        event = tx.execute(
            "SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
            "WHERE k.activity_id=? AND k.idempotency_key=?",
            (row["activity_id"], "child-ack:" + row["id"]),
        ).fetchone()
        try:
            data = json.loads(event["payload"])["data"]
            native = data["native"]
            return (
                isinstance(data, dict) and data.get("transport") == "supervisor-monitor-v1"
                and data.get("intent_id") == row["id"]
                and data.get("acknowledgement_id") == row["acknowledgement_id"]
                and data.get("issuing_generation") == row["generation"]
                and native == {"host_id": row["child_host_id"], "boot_id": row["child_boot_id"],
                               "pid": row["child_pid"], "start_token": row["child_start_token"]}
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def complete_launch(
        self, intent_id: str, token, *, status: str, evidence: dict,
        token_usage: int | None, clock_boot_id: str | None = None,
        clock_monotonic_ns: int | None = None,
    ) -> LaunchIntent:
        """Close a launch with verified evidence and conservative accounting.

        A missing or malformed meter is not interpreted as zero: its reserved
        allowance stays committed and the intent remains ``uncertain``.  This
        makes restart and telemetry failures consume capacity until an operator
        records an evidenced outcome.
        """
        from .ownership import OwnershipRefused
        if status not in {"succeeded", "failed", "uncertain"}:
            raise OwnershipRefused("INVALID_COMPLETION")
        evidence = self._verified_evidence(evidence)
        if status == "uncertain":
            if token_usage is not None:
                raise OwnershipRefused("MALFORMED_TELEMETRY")
        elif not self._valid_nonnegative_integer(token_usage):
            raise OwnershipRefused("MALFORMED_TELEMETRY")
        self.ensure_authority_schema()
        terminal_status = None
        # Process liveness and telemetry certainty are independent. A proven
        # stopped child no longer consumes productive time, while an unknown
        # meter must still retain its full token reservation.
        with self.read_transaction() as tx:
            observed = tx.execute(
                "SELECT * FROM authority_launch_intents WHERE id=?", (intent_id,),
            ).fetchone()
        if observed is None:
            raise OwnershipRefused("FENCE_REVOKED")
        if observed["completion_status"] in {None, "uncertain"}:
            terminal_status = self._terminal_child_status(observed)
            if status != "uncertain" and terminal_status != "DEAD":
                raise OwnershipRefused("CHILD_NOT_TERMINAL")
        with self.transaction() as tx:
            row = tx.execute(
                "SELECT i.*,a.repository_id,a.run_id FROM authority_launch_intents i "
                "JOIN authority_activities a ON a.id=i.activity_id WHERE i.id=?",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise OwnershipRefused("FENCE_REVOKED")
            if self._intent_from_row(row).child_identity != self._intent_from_row(observed).child_identity:
                raise OwnershipRefused("CHILD_LIVENESS_CHANGED")
            self._assert_activity_binding(tx, token, row["activity_id"])
            accounting = tx.execute(
                "SELECT * FROM authority_launch_accounting WHERE intent_id=?", (intent_id,),
            ).fetchone()
            if accounting is None:
                # A managed completion must never manufacture accounting for
                # an intent from an older writer.
                raise OwnershipRefused("ACCOUNTING_UNKNOWN")
            encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
            if row["completion_status"] is not None:
                # Unknown telemetry is an explicitly provisional result.  A
                # later fenced completion with a real meter may settle it; a
                # second uncertain report, or any attempt to rewrite a final
                # classification, remains idempotency-protected.
                if row["completion_status"] != "uncertain" or status == "uncertain":
                    if (
                        row["completion_status"] != status
                        or row["completion_evidence_json"] != encoded
                        or row["token_usage"] != token_usage
                    ):
                        raise OwnershipRefused("IDEMPOTENCY_CONFLICT")
                    return self._intent_from_row(row, reused=True)
            if row["state"] not in {
                "reserved", "acknowledged", "released_to_execute", "reconcile_required", "uncertain",
            }:
                raise OwnershipRefused("INTENT_NOT_ACTIVE")
            monitored_recovery = self._monitored_ack_binding_tx(tx, row)
            if (
                status == "succeeded" and (
                    not row["permit_id"] or (
                        row["state"] != "released_to_execute"
                        and row["completion_status"] != "uncertain"
                        and not (row["state"] == "reconcile_required" and monitored_recovery)
                    )
                )
            ):
                raise OwnershipRefused("CHILD_NOT_AUTHORIZED")
            if status != "uncertain" and token_usage > accounting["token_reservation"]:
                # Retain the full reservation.  An under-reserved child cannot
                # reduce a budget by claiming a larger number after the fact.
                raise OwnershipRefused("MALFORMED_TELEMETRY")
            now = self._now()
            if status == "uncertain":
                final_state, final_usage, release = "uncertain", None, 0
            else:
                final_state, final_usage = f"completed_{status}", token_usage
                release = accounting["token_reservation"] - token_usage
            tx.execute(
                "UPDATE authority_launch_intents SET state=?,completion_status=?,"
                "completion_evidence_json=?,token_usage=?,completed_at=?,updated_at=? WHERE id=?",
                (final_state, status, encoded, final_usage, now, now, intent_id),
            )
            tx.execute(
                "UPDATE authority_launch_accounting SET token_final=?,completed_at=? WHERE intent_id=?",
                (final_usage, now, intent_id),
            )
            if terminal_status == "DEAD" and tx.execute(
                "SELECT 1 FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?",
                (row["activity_id"], "policy-child-stopped:" + intent_id),
            ).fetchone() is None:
                self._record_event_once_tx(tx, token, row["activity_id"], "policy-child-stopped:" + intent_id, {
                    "intent_id": intent_id, "evidence": evidence,
                    "identity": {
                        "host_id": row["child_host_id"], "boot_id": row["child_boot_id"],
                        "pid": row["child_pid"], "start_token": row["child_start_token"],
                    },
                })
            if self._run_policy_tables_present_tx(tx):
                budget = tx.execute("SELECT 1 FROM authority_run_policy_budgets WHERE repository_id=? AND run_id=?",
                                    (token.repository_id, token.run_id)).fetchone()
                if budget is not None:
                    sampled_boot, sampled_ns = self._policy_sample(clock_boot_id, clock_monotonic_ns)
                    remaining_active = self._policy_has_productive_work_tx(tx, token)
                    self._policy_clock_tx(
                        tx, token, boot_id=sampled_boot,
                        monotonic_ns=sampled_ns, active_after=remaining_active,
                    )
            if status != "uncertain":
                changed = tx.execute(
                    "UPDATE authority_run_limits SET token_committed=token_committed-?,"
                    "token_used=token_used+?,generation=? WHERE repository_id=? AND run_id=? "
                    "AND token_committed >= ?",
                    (release, token_usage, token.generation, token.repository_id, token.run_id, release),
                ).rowcount
                if changed != 1:
                    raise OwnershipRefused("ACCOUNTING_UNKNOWN")
            tx.execute(
                "INSERT INTO control_events(event_type,payload) VALUES('launch_completed',?)",
                (json.dumps({"repository_id": token.repository_id, "run_id": token.run_id,
                             "activity_id": row["activity_id"],
                             "data": {"intent_id": intent_id, "status": status,
                                      "settlement_generation": token.generation,
                                      "token_usage": token_usage, "evidence": evidence}},
                            sort_keys=True, separators=(",", ":")),),
            )
            current = tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (intent_id,)).fetchone()
            return self._intent_from_row(current)

    def assert_integration_settled(self, token, workspace):
        from . import integration_journal
        from .ownership import assert_owner
        integration_journal.ensure_schema(self)
        with self.transaction() as tx:
            assert_owner(tx, token)
            rows = tx.execute("SELECT preparation_id FROM context_workspaces WHERE repository_id=? "
                              "AND path=?", (token.repository_id, str(workspace))).fetchall()
            for row in rows:
                integration_journal.assert_settled_tx(tx, row["preparation_id"])

    def register_integration_intent_tx(self, tx, token, wave_key, workspace, before_material,
                                       expected_after_material, bindings):
        from . import integration_journal
        from .ownership import OwnershipRefused
        rows = tx.execute("SELECT preparation_id FROM context_workspaces WHERE repository_id=? AND run_id=? "
                          "AND path=? AND state='ready'", (token.repository_id, token.run_id, str(workspace))).fetchall()
        if len(rows) != 1:
            raise OwnershipRefused("WAVE_INTEGRATION_BINDING_INVALID")
        return integration_journal.register_intent_tx(
            self, tx, token, wave_key=wave_key, preparation_id=rows[0]["preparation_id"],
            before_material=before_material, expected_after_material=expected_after_material, bindings=bindings,
        )

    def mark_workspace_integration_pending_tx(self, tx, token, journal, workspace):
        from . import integration_journal
        from .ownership import OwnershipRefused
        _, contract = integration_journal.validate_journal_tx(self, tx, token, journal)
        if contract["authority"]["workspace"] != str(workspace):
            raise OwnershipRefused("WAVE_INTEGRATION_BINDING_INVALID")

    def mark_integration_applied_tx(self, tx, token, journal, actual_after):
        from . import integration_journal
        integration_journal.validate_journal_tx(self, tx, token, journal, actual_after)

    def publish_integration_tx(self, tx, token, journal, actual_after):
        from . import integration_journal
        return integration_journal.publish_tx(self, tx, token, journal, actual_after)

    def validate_context_schema(self) -> None:
        with self.read_transaction() as tx:
            _validate_context_schema_snapshot(tx, allow_legacy_writer=True, allow_legacy_children=True)

    @contextmanager
    def read_transaction(self):
        """Yield one bounded, validated read snapshot without initialization."""
        if getattr(self._fenced_operation, "depth", 0):
            _refuse("STORE_BUSY")
        conn = None
        with self._hold_process_mutex(raw_validation=True):
            with self._hold_authority_flock(writable=False):
                try:
                    conn = self._connect_locked(read_only=True)
                    self._process_mutex.active_connections += 1
                    conn.execute("BEGIN")
                    yield conn
                except sqlite3.Error as error:
                    _sqlite_refusal(error)
                finally:
                    if conn is not None:
                        if conn.in_transaction:
                            conn.rollback()
                        conn.close()
                        self._process_mutex.active_connections -= 1

    def enumerate_events(
        self, *, run_id: str | None = None, repository_id: str | None = None,
    ):
        yield from self.open_read_only(self.db_path).enumerate_events(
            run_id=run_id, repository_id=repository_id,
        )

    @classmethod
    def open_read_only(cls, path: Path):
        path = _canonical_absolute(Path(path))
        candidate = cls.__new__(cls)
        candidate.db_path = path
        candidate.liveness_probe = None
        candidate.fault_probe = None
        candidate._writable = False
        candidate._process_mutex = _authority_mutex(path)
        candidate._fenced_operation = threading.local()
        with candidate._hold_process_mutex(raw_validation=True):
            candidate._anchor = candidate._capture_anchor(create=False, writable=False)
            candidate._bind_process_mutex()
        return _ControlStoreView(candidate)

    @staticmethod
    def read_legacy_run_store(path: Path):
        """Project legacy rows read-only; never migrate or initialize them."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(path)
        source_hash, identity = _hash_regular_file(path)
        _require_unchanged_regular(path, identity)
        connection = None
        try:
            connection = sqlite3.connect(_sqlite_uri(path, "ro"), uri=True)
            connection.execute("BEGIN")
            rows = connection.execute("SELECT id FROM runs ORDER BY created_at DESC").fetchall()
        except sqlite3.Error as error:
            _sqlite_refusal(error)
        finally:
            if connection is not None:
                connection.close()
        _require_unchanged_regular(path, identity)
        final_hash, final_identity = _hash_regular_file(path)
        if final_identity != identity:
            _refuse("STORE_REPLACED")
        if final_hash != source_hash:
            _refuse("LEGACY_SOURCE_CHANGED")
        for (run_id,) in rows:
            if isinstance(run_id, str) and run_id:
                yield LegacyProjection(run_id, run_id, source_hash, "mapped")
            else:
                yield LegacyProjection(None, None, source_hash, "quarantined", "INVALID_RUN_ID")

    @staticmethod
    def read_legacy_context(path: Path) -> LegacyProjection:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(path)
        source_hash, identity, data = _read_regular_file(path)
        payload = json.loads(data)
        _require_unchanged_regular(path, identity)
        aliases = [payload.get(key) for key in ("FFS_RUN_ID", "GSD_RUN_ID") if payload.get(key) is not None]
        if any(not isinstance(value, str) or not value for value in aliases):
            return LegacyProjection(None, None, source_hash, "quarantined", "INVALID_RUN_ID")
        values = set(aliases)
        if len(values) > 1:
            return LegacyProjection(None, None, source_hash, "quarantined", "ALIAS_CONFLICT")
        run_id = next(iter(values), None)
        if run_id is None:
            return LegacyProjection(None, None, source_hash, "quarantined", "MISSING_RUN_ID")
        return LegacyProjection(run_id, run_id, source_hash, "mapped")


class _ControlStoreView:
    def __init__(self, store: ControlStore) -> None:
        self._store = store

    @property
    def db_path(self) -> Path:
        return self._store.db_path

    def read_transaction(self):
        return self._store.read_transaction()

    def validate_context_schema(self) -> None:
        self._store.validate_context_schema()

    def validate_run_context(self, context) -> None:
        with self.read_transaction() as tx:
            has_context = tx.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_runs'"
            ).fetchone()
            if has_context is None:
                row = tx.execute(
                    "SELECT repository_id,run_id,generation FROM authority_activities WHERE id=?",
                    (context.activity_id,),
                ).fetchone()
            else:
                row = tx.execute(
                    "SELECT a.repository_id,a.run_id,COALESCE(r.generation,a.generation) AS generation,"
                    "r.activity_id AS current_activity_id "
                    "FROM authority_activities a LEFT JOIN context_runs r "
                    "ON r.repository_id=a.repository_id AND r.run_id=a.run_id WHERE a.id = ?",
                    (context.activity_id,),
                ).fetchone()
        if (
            row is None or row["repository_id"] != context.repository_id
            or row["run_id"] != context.run_id or row["generation"] != context.generation
            or (
                "current_activity_id" in row.keys()
                and row["current_activity_id"] is not None
                and row["current_activity_id"] != context.activity_id
            )
        ):
            _refuse("FENCE_REVOKED")

    def enumerate_events(
        self, *, run_id: str | None = None, repository_id: str | None = None,
    ):
        conn = None
        with self._store._hold_process_mutex(raw_validation=True):
            with self._store._hold_authority_flock(writable=False):
                try:
                    conn = self._store._connect_locked(read_only=True)
                    self._store._process_mutex.active_connections += 1
                    rows = conn.execute(
                        "SELECT id, event_type, payload, created_at FROM control_events ORDER BY id"
                    ).fetchall()
                except sqlite3.Error as error:
                    _sqlite_refusal(error)
                finally:
                    if conn is not None:
                        conn.close()
                        self._store._process_mutex.active_connections -= 1
        for row in rows:
            payload = row["payload"]
            if payload and payload[:1] in "[{":
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    pass
            event_run = payload.get("run_id") if isinstance(payload, dict) else payload
            event_repository = payload.get("repository_id") if isinstance(payload, dict) else None
            if run_id is not None and event_run != run_id:
                continue
            if repository_id is not None and event_repository != repository_id:
                continue
            if isinstance(payload, dict) and set(payload) >= {"run_id", "activity_id", "data"}:
                payload = payload["data"]
            yield {"id": row["id"], "event_type": row["event_type"], "payload": payload, "created_at": row["created_at"]}

    def enumerate_decisions(self, *, gate: str | None = None):
        with self.read_transaction() as tx:
            _require_decision_expiry_schema(tx)
            if gate is None:
                rows = tx.execute(
                    "SELECT * FROM authority_decisions ORDER BY rowid"
                ).fetchall()
            else:
                rows = tx.execute(
                    "SELECT * FROM authority_decisions WHERE gate=? ORDER BY rowid",
                    (gate,),
                ).fetchall()
        for row in rows:
            yield Decision(
                row["id"], row["repository_id"], row["run_id"], row["gate"],
                bool(row["status"]), json.loads(row["input_hashes_json"]),
                json.loads(row["evidence_json"]), json.loads(row["provenance_json"]),
                json.loads(row["dependencies_json"]), row["expires_at"],
            )

    def project_gates(self, input_hashes: dict, *, run_id: str, repository_id: str):
        with self.read_transaction() as tx:
            return _project_gates_snapshot(
                tx, input_hashes, run_id=run_id, repository_id=repository_id,
                now=self._store._now(),
            )


def _read_regular_file(path: Path) -> tuple[str, tuple[int, int], bytes]:
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
    except FileNotFoundError:
        raise
    except OSError as error:
        _refuse("STORE_IO", error)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _refuse("STORE_IO")
        digest = hashlib.sha256()
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            chunks.append(chunk)
        return digest.hexdigest(), (info.st_dev, info.st_ino), b"".join(chunks)
    finally:
        os.close(fd)


def _hash_regular_file(path: Path) -> tuple[str, tuple[int, int]]:
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
    except FileNotFoundError:
        raise
    except OSError as error:
        _refuse("STORE_IO", error)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _refuse("STORE_IO")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest(), (info.st_dev, info.st_ino)
    finally:
        os.close(fd)


def _require_unchanged_regular(path: Path, identity: tuple[int, int]) -> None:
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as error:
        _refuse("STORE_IO", error)
    if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != identity:
        _refuse("STORE_REPLACED")


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
        self.db_path = Path(db_path)
        # Do not let a legacy façade acquire a write transaction merely by
        # opening a migrated ControlStore. The mutator guard below remains the
        # decision point for a named run; this avoids an incidental schema
        # write before that refusal.
        if not self._is_migration_authority():
            init_db(db_path)

    def _is_migration_authority(self) -> bool:
        if not self.db_path.exists():
            return False
        connection = None
        try:
            path = self.db_path if self.db_path.is_absolute() else self.db_path.absolute()
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            return connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_fixture'"
            ).fetchone() is not None
        except (sqlite3.Error, ValueError):
            # Preserve standalone RunStore initialization/error behavior for
            # non-authority paths. A malformed authority will subsequently
            # fail its normal SQL operation instead of being silently adopted.
            return False
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def assert_raw_mutation_allowed(connection, run_id: str) -> None:
        """Block raw RunStore writes for any epoch enrolled in this database.

        An enrolled run is selected by the migration authority, irrespective
        of whether its current selection is ``legacy``, ``new``, or paused.
        Standalone legacy databases do not carry ``migration_fixture`` and
        retain their existing mutator behavior.
        """
        fixture = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_fixture'"
        ).fetchone()
        if fixture is None:
            return
        try:
            row = connection.execute(
                "SELECT 1 FROM migration_epochs WHERE run_id=?", (run_id,)
            ).fetchone()
        except sqlite3.Error as error:
            raise MigrationRawMutationRefused("MIGRATION_AUTHORITY_CORRUPT") from error
        if row is not None:
            raise MigrationRawMutationRefused()

    @staticmethod
    def assert_raw_creation_allowed(connection) -> None:
        """An enrolled authority accepts new runs only through managed ingress."""
        fixture = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_fixture'"
        ).fetchone()
        if fixture is not None:
            raise MigrationRawMutationRefused()

    def _begin_raw_mutation(self, connection, run_id: str) -> None:
        """Fence the epoch check and legacy write in one SQLite transaction."""
        connection.execute("BEGIN IMMEDIATE")
        self.assert_raw_mutation_allowed(connection, run_id)

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
            conn.execute("BEGIN IMMEDIATE")
            self.assert_raw_creation_allowed(conn)
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
            self._begin_raw_mutation(conn, run_id)
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
            self._begin_raw_mutation(conn, run_id)
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
            self._begin_raw_mutation(conn, run_id)
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
            self._begin_raw_mutation(conn, run_id)
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
