"""Fenced wave-application journal in the existing ControlStore authority.

Material capture and Git application are caller-owned external effects. This
module records their immutable before/after contract and publication fence in
short transactions; it never opens a second database or applies a patch.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
from typing import Callable

from .ownership import OwnershipRefused, assert_owner


_SCHEMA = """
CREATE TABLE IF NOT EXISTS authority_workspace_integrations (
 repository_id TEXT NOT NULL, run_id TEXT NOT NULL,
 preparation_id TEXT NOT NULL, wave_key TEXT NOT NULL,
 activity_id TEXT NOT NULL, event_id INTEGER NOT NULL,
 issuing_intent_id TEXT NOT NULL, issuing_generation INTEGER NOT NULL,
 acknowledgement_id TEXT NOT NULL, permit_id TEXT NOT NULL,
 contract_json TEXT NOT NULL, contract_sha256 TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','published','quarantined')),
 evidence_json TEXT, publication_generation INTEGER,
 PRIMARY KEY(repository_id,run_id,wave_key)
)
"""


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def ensure_schema(store):
    store.ensure_authority_schema()
    with store.transaction() as tx:
        tx.execute(_SCHEMA)
        tx.execute("CREATE INDEX IF NOT EXISTS workspace_integrations_pending "
                   "ON authority_workspace_integrations(preparation_id,state)")


def _material(value):
    if not isinstance(value, dict):
        raise OwnershipRefused("WAVE_INTEGRATION_INVALID")
    for path, item in value.items():
        if (not isinstance(path, str) or not path or "\0" in path
                or PurePosixPath(path).is_absolute() or str(PurePosixPath(path)) != path
                or any(part in {"..", ".git"} for part in PurePosixPath(path).parts)):
            raise OwnershipRefused("WAVE_INTEGRATION_INVALID")
        if item is None:
            continue
        if (not isinstance(item, dict) or set(item) != {"sha256", "git_mode"}
                or item["git_mode"] not in {"100644", "100755"}
                or not isinstance(item["sha256"], str) or len(item["sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in item["sha256"])):
            raise OwnershipRefused("WAVE_INTEGRATION_INVALID")
    return json.loads(_canonical(value))


def assert_settled_tx(tx, preparation_id, *, except_wave_key=None):
    if tx.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name="
                  "'authority_workspace_integrations'").fetchone() is None:
        return
    rows = tx.execute("SELECT wave_key FROM authority_workspace_integrations "
                      "WHERE preparation_id=? AND state!='published'", (preparation_id,)).fetchall()
    if any(row["wave_key"] != except_wave_key for row in rows):
        raise OwnershipRefused("WORKSPACE_INTEGRATION_PENDING")


@dataclass(frozen=True)
class RecordFamily:
    """A journal whose authority is a retained child record, not a GSD wave request, named by its key prefix."""

    prefix: str
    schema: str
    record_key: Callable       # (action_id) -> the idempotency key of the child's retained record
    id_field: str              # the record field naming the policy action
    action: str                # the policy action the child ran under
    role: str                  # the child binding role
    receipt_role: str          # the role of the receipt the candidate is bound through
    results: bool              # whether the record carries frozen-check results that must all pass


def record_family(wave_key) -> RecordFamily | None:
    """The family a journal key belongs to (``recovery-trial:`` or ``repair:``); ``None`` for a GSD wave."""
    from .recovery_trial_checks import TRIAL_CHECKS_SCHEMA, trial_checks_key
    from .repair_integration import REPAIR_SCHEMA, repair_record_key
    families = (
        RecordFamily("recovery-trial:", TRIAL_CHECKS_SCHEMA, trial_checks_key, "trial_action_id", "recovery_trial",
                     "recovery", "recovery", True),
        RecordFamily("repair:", REPAIR_SCHEMA, repair_record_key, "action_id", "repair", "worker", "execution", False),
    )
    return next((family for family in families if isinstance(wave_key, str) and wave_key.startswith(family.prefix)),
                None)


def _recovery_input_is_live_tx(tx, token, workspace, trial_activity_id, input_digest, *, role="recovery") -> bool:
    """The recovery trial (or, as ``role="execution"``, the repair) ran on the candidate live on THIS preparation.

    A GSD wave (or an earlier recovery winner) advances the candidate in place on the shared
    preparation, whose own recorded input stays the sealed one; comparing the trial's input with
    that first input would refuse every winner after a real wave.  Instead, until the winner's own
    output is bound, the trial input must equal the frontend state's current candidate, and that
    candidate's journals must have landed on this preparation.  Once this winner's output is the
    current candidate, the candidate row must name the trial's input as its parent and the
    trial's own recovery receipt.  With no frontend lifecycle at all, or an untouched sealed
    input, the preparation's first input is the candidate, as before.
    """
    from .workspace import _from_row
    first = _from_row(workspace).input_digest
    if tx.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='authority_frontend_policy_states'"
                  ).fetchone() is None:
        return first == input_digest
    scope = (token.repository_id, token.run_id)
    state = tx.execute("SELECT acceptance_hash,candidate_hash FROM authority_frontend_policy_states "
                       "WHERE repository_id=? AND run_id=?", scope).fetchone()
    if state is None:
        return first == input_digest
    current = tx.execute("SELECT * FROM authority_frontend_policy_candidates WHERE repository_id=? AND run_id=? "
                         "AND acceptance_hash=? AND candidate_hash=?",
                         (*scope, state["acceptance_hash"], state["candidate_hash"])).fetchone()
    if current is None:
        return state["candidate_hash"] == input_digest == first
    receipt = tx.execute("SELECT receipt_json FROM authority_acceptance_receipts WHERE repository_id=? "
                         "AND run_id=? AND receipt_hash=?", (*scope, current["receipt_hash"])).fetchone()
    value = json.loads(receipt["receipt_json"])
    if state["candidate_hash"] == input_digest:
        landed = tx.execute("SELECT preparation_id FROM authority_workspace_integrations WHERE repository_id=? "
                            "AND run_id=? AND issuing_intent_id=? ORDER BY event_id DESC",
                            (*scope, value["intent_id"])).fetchone()
        return landed is not None and landed["preparation_id"] == workspace["preparation_id"]
    return (current["parent_candidate_hash"] == input_digest and value["role"] == role
            and value["activity_id"] == trial_activity_id)


def _record_binding_tx(store, tx, token, wave_key, preparation_id, family):
    """Bind a recovery winner's (or a repair's) journal to the child's retained record.

    The authority is the child's retained event (``recovery-trial-checks`` or
    ``repair-record``), its settled issuing intent/ACK/permit and the shared
    candidate preparation the retained patch lands in (never the child workspace).
    """
    assert_owner(tx, token)
    try:
        action_id = wave_key[len(family.prefix):]
        action = tx.execute("SELECT * FROM authority_policy_actions WHERE id=? AND repository_id=? AND run_id=?",
                            (action_id, token.repository_id, token.run_id)).fetchone()
        intent = tx.execute("SELECT i.* FROM authority_policy_action_attempts p JOIN authority_launch_intents i "
                            "ON i.id=p.intent_id WHERE p.action_id=? ORDER BY p.created_at DESC", (action_id,)).fetchone()
        activity = store._assert_activity_binding(tx, token, intent["activity_id"])
        row = tx.execute("SELECT k.event_id,k.payload_hash,e.payload FROM authority_event_keys k JOIN control_events e "
                         "ON e.id=k.event_id WHERE k.activity_id=? AND k.idempotency_key=?",
                         (activity["id"], family.record_key(action_id))).fetchone()
        wrapped = json.loads(row["payload"])
        data = wrapped["data"]
        workspace = tx.execute("SELECT * FROM context_workspaces WHERE preparation_id=? AND repository_id=? AND run_id=?",
                               (preparation_id, token.repository_id, token.run_id)).fetchone()
        child = tx.execute("SELECT * FROM authority_child_bindings WHERE activity_id=?", (activity["id"],)).fetchone()
        prepared = tx.execute("SELECT payload_hash FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?",
                              (activity["id"], wave_key + ":prepared")).fetchone()
        if (wrapped != {"run_id": token.run_id, "activity_id": activity["id"], "data": data}
                or row["payload_hash"] != hashlib.sha256(_canonical(data).encode()).hexdigest()
                or data["schema"] != family.schema or data[family.id_field] != action_id
                or data["issuing_intent_id"] != intent["id"] or data["acceptance_hash"] != child["contract_hash"]
                or action["action"] != family.action or action["state"] not in {"dispatched", "completed_valid"}
                or activity["state"] not in {"active", "succeeded"}
                or intent["state"] != "completed_succeeded" or intent["completion_status"] != "succeeded"
                or not intent["completion_evidence_json"] or not intent["acknowledgement_id"] or not intent["permit_id"]
                or workspace["state"] != "ready" or workspace["base_commit"] != data["base_commit"]
                or not _recovery_input_is_live_tx(tx, token, workspace, activity["id"], data["input_digest"],
                                                  role=family.receipt_role)
                or child["role"] != family.role or child["workspace_preparation_id"] != data["workspace_preparation_id"]
                or child["workspace_preparation_id"] == preparation_id or prepared is None):
            raise ValueError
        authority = {"activity_id": activity["id"], "event_id": row["event_id"],
                     "intent_id": intent["id"], "issuing_generation": intent["generation"],
                     "acknowledgement_id": intent["acknowledgement_id"], "permit_id": intent["permit_id"],
                     "request_sha256": row["payload_hash"], "prepared_sha256": prepared["payload_hash"],
                     "workspace": workspace["path"], "base_commit": workspace["base_commit"],
                     "input_digest": data["input_digest"], "runtime_identity": child["runtime_identity"],
                     "contract_hash": child["contract_hash"], "patch_sha256": data["patch"]["sha256"]}
        if family.results:
            authority["trial_candidate_hash"] = data["trial_candidate_hash"]
        return authority
    except (AttributeError, TypeError, ValueError, KeyError, IndexError) as error:
        raise OwnershipRefused("WAVE_INTEGRATION_BINDING_INVALID") from error


def _binding_tx(store, tx, token, wave_key, preparation_id, *, completed=False):
    from .workspace import _from_row
    family = record_family(wave_key)
    if family is not None:
        return _record_binding_tx(store, tx, token, wave_key, preparation_id, family)
    assert_owner(tx, token)
    try:
        parts = wave_key.split(":")
        if len(parts) != 2 or parts[0] != "gsd-wave" or str(int(parts[1])) != parts[1]:
            raise ValueError
        event_id = int(parts[1])
        row = tx.execute(
            "SELECT k.activity_id,k.payload_hash,k.idempotency_key,e.event_type,e.payload "
            "FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id WHERE e.id=?",
            (event_id,),
        ).fetchone()
        wrapped = json.loads(row["payload"])
        data = wrapped["data"]
        activity = store._assert_activity_binding(tx, token, row["activity_id"])
        intent = tx.execute("SELECT * FROM authority_launch_intents WHERE id=?",
                            (data["intent_id"],)).fetchone()
        workspace = tx.execute("SELECT * FROM context_workspaces WHERE preparation_id=? "
                               "AND repository_id=? AND run_id=?",
                               (preparation_id, token.repository_id, token.run_id)).fetchone()
        child = tx.execute("SELECT * FROM authority_child_bindings WHERE activity_id=?",
                           (row["activity_id"],)).fetchone()
        registration = tx.execute(
            "SELECT k.payload_hash,e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
            "WHERE k.activity_id=? AND k.idempotency_key=?",
            (row["activity_id"], "worker-registration:" + intent["id"]),
        ).fetchone()
        registered = json.loads(registration["payload"])["data"]
        if (wrapped != {"run_id": token.run_id, "activity_id": row["activity_id"], "data": data}
                or data["operation"] != "gsd-wave-request" or row["event_type"] != row["idempotency_key"]
                or row["payload_hash"] != hashlib.sha256(_canonical(data).encode()).hexdigest()
                or not row["event_type"].startswith("worker-request:" + intent["id"] + ":")
                or activity["state"] not in ({"active", "succeeded"} if completed else {"active"})
                or intent["activity_id"] != activity["id"]
                or intent["state"] not in ({"completed_succeeded"} if completed else
                                             {"released_to_execute", "reconcile_required"})
                or completed and (intent["completion_status"] != "succeeded"
                                  or not intent["completion_evidence_json"])
                or not intent["acknowledgement_id"] or not intent["permit_id"]
                or workspace["state"] != "ready" or child["workspace_preparation_id"] != preparation_id
                or workspace["path"] != data["workspace"] or registered["workspace"] != workspace["path"]
                or child["runtime_identity"] != data["runtime_identity"]
                or registration["payload_hash"] != hashlib.sha256(_canonical(registered).encode()).hexdigest()
                or registered["intent_id"] != intent["id"] or registered["generation"] != intent["generation"]):
            raise ValueError
        claim = tx.execute("SELECT payload_hash FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?",
                           (activity["id"], wave_key + ":claimed")).fetchone()
        prepared = tx.execute("SELECT payload_hash FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?",
                              (activity["id"], wave_key + ":prepared")).fetchone()
        if claim is None or prepared is None:
            raise ValueError
        return {"activity_id": activity["id"], "event_id": event_id,
                "intent_id": intent["id"], "issuing_generation": intent["generation"],
                "acknowledgement_id": intent["acknowledgement_id"], "permit_id": intent["permit_id"],
                "request_sha256": row["payload_hash"], "registration_sha256": registration["payload_hash"],
                "claim_sha256": claim["payload_hash"], "prepared_sha256": prepared["payload_hash"],
                "workspace": workspace["path"], "base_commit": workspace["base_commit"],
                "input_digest": _from_row(workspace).input_digest, "runtime_identity": child["runtime_identity"],
                "contract_hash": child["contract_hash"]}
    except (AttributeError, TypeError, ValueError, KeyError, IndexError) as error:
        raise OwnershipRefused("WAVE_INTEGRATION_BINDING_INVALID") from error


def validate_completed_publication_tx(store, tx, token, *, wave_key, intent_id):
    """Read a published journal through its successful original launch.

    This does not authorize another effect or reconstruct a revoked permit.
    Successful completion retains the original permit, ACK and generation;
    every issuing binding must still match the immutable published contract.
    Physical evidence verification remains outside the SQL transaction.
    """
    assert_owner(tx, token)
    row = tx.execute("SELECT * FROM authority_workspace_integrations "
                     "WHERE repository_id=? AND run_id=? AND wave_key=?",
                     (token.repository_id, token.run_id, wave_key)).fetchone()
    try:
        if row is None or row["state"] != "published" or row["issuing_intent_id"] != intent_id:
            raise ValueError
        contract = json.loads(row["contract_json"])
        if hashlib.sha256(_canonical(contract).encode()).hexdigest() != row["contract_sha256"]:
            raise ValueError
        authority = _binding_tx(store, tx, token, wave_key, row["preparation_id"], completed=True)
        if contract["authority"] != authority:
            raise ValueError
        if any(row[field] != authority[key] for field, key in (
            ("activity_id", "activity_id"), ("event_id", "event_id"),
            ("issuing_intent_id", "intent_id"), ("issuing_generation", "issuing_generation"),
            ("acknowledgement_id", "acknowledgement_id"), ("permit_id", "permit_id"),
        )):
            raise ValueError
        retained = {}
        for suffix in (":integrated", ":journal-published"):
            event = tx.execute("SELECT k.payload_hash,e.payload FROM authority_event_keys k "
                "JOIN control_events e ON e.id=k.event_id WHERE k.activity_id=? AND k.idempotency_key=?",
                (row["activity_id"], wave_key + suffix)).fetchone()
            value = json.loads(event["payload"])["data"]
            if hashlib.sha256(_canonical(value).encode()).hexdigest() != event["payload_hash"]:
                raise ValueError
            retained[suffix] = value
        integrated, published = retained[":integrated"], retained[":journal-published"]
        evidence = json.loads(row["evidence_json"])
        material = integrated["material"]
        if (integrated["evidence"] != evidence or published["evidence"] != evidence
                or published["contract_sha256"] != row["contract_sha256"]
                or published["publication_generation"] != row["publication_generation"]
                or material["before"] != contract["before"]
                or material["after"] != contract["expected_after"]
                or material["workspace"] != authority["workspace"]
                or material["initial_head"] != authority["base_commit"]
                or hashlib.sha256((_canonical(material) + "\n").encode()).hexdigest() != evidence["sha256"]):
            raise ValueError
        return {"journal": dict(row), "contract": contract, "material": material, "evidence": evidence}
    except (AttributeError, TypeError, ValueError, KeyError, IndexError) as error:
        raise OwnershipRefused("FRONTEND_INTEGRATION_PUBLICATION_INVALID") from error


def register_intent(store, token, *, wave_key, preparation_id, before_material,
                    expected_after_material, bindings):
    ensure_schema(store)
    with store.transaction() as tx:
        return register_intent_tx(store, tx, token, wave_key=wave_key, preparation_id=preparation_id,
                                  before_material=before_material,
                                  expected_after_material=expected_after_material, bindings=bindings)


def register_intent_tx(store, tx, token, *, wave_key, preparation_id, before_material,
                       expected_after_material, bindings):
    before, after = _material(before_material), _material(expected_after_material)
    if set(before) != set(after) or not isinstance(bindings, dict):
        raise OwnershipRefused("WAVE_INTEGRATION_INVALID")
    tx.execute(_SCHEMA)
    authority = _binding_tx(store, tx, token, wave_key, preparation_id)
    assert_settled_tx(tx, preparation_id, except_wave_key=wave_key)
    contract = {"schema": "ffs.workspace-integration-intent/v1", "authority": authority,
                "before": before, "expected_after": after, "bindings": bindings}
    encoded = _canonical(contract)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    prior = tx.execute("SELECT * FROM authority_workspace_integrations "
                       "WHERE repository_id=? AND run_id=? AND wave_key=?",
                       (token.repository_id, token.run_id, wave_key)).fetchone()
    if prior is not None:
        if prior["contract_json"] != encoded or prior["preparation_id"] != preparation_id:
            raise OwnershipRefused("WAVE_INTEGRATION_CONFLICT")
        return dict(prior)
    tx.execute(
        "INSERT INTO authority_workspace_integrations(repository_id,run_id,preparation_id,wave_key,"
        "activity_id,event_id,issuing_intent_id,issuing_generation,acknowledgement_id,permit_id,"
        "contract_json,contract_sha256,state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending')",
        (token.repository_id, token.run_id, preparation_id, wave_key, authority["activity_id"],
         authority["event_id"], authority["intent_id"], authority["issuing_generation"],
         authority["acknowledgement_id"], authority["permit_id"], encoded, digest),
    )
    return dict(tx.execute("SELECT * FROM authority_workspace_integrations WHERE repository_id=? "
                           "AND run_id=? AND wave_key=?", (token.repository_id, token.run_id, wave_key)).fetchone())


def read_intent(store, token, *, wave_key):
    ensure_schema(store)
    with store.transaction() as tx:
        assert_owner(tx, token)
        row = tx.execute("SELECT * FROM authority_workspace_integrations "
                         "WHERE repository_id=? AND run_id=? AND wave_key=?",
                         (token.repository_id, token.run_id, wave_key)).fetchone()
        return dict(row) if row is not None else None


def validate_journal_tx(store, tx, token, journal, actual_after=None):
    if not isinstance(journal, dict):
        raise OwnershipRefused("WAVE_INTEGRATION_BINDING_INVALID")
    row = tx.execute("SELECT * FROM authority_workspace_integrations WHERE repository_id=? AND run_id=? "
                     "AND wave_key=?", (token.repository_id, token.run_id, journal.get("wave_key"))).fetchone()
    if row is None or row["contract_sha256"] != journal.get("contract_sha256") or row["state"] == "quarantined":
        raise OwnershipRefused("WAVE_INTEGRATION_RECONCILIATION_REQUIRED")
    contract = json.loads(row["contract_json"])
    if _binding_tx(store, tx, token, row["wave_key"], row["preparation_id"]) != contract["authority"]:
        raise OwnershipRefused("WAVE_INTEGRATION_BINDING_INVALID")
    if actual_after is not None and _material(actual_after) != contract["expected_after"]:
        raise OwnershipRefused("WAVE_INTEGRATION_EVIDENCE_INVALID")
    return row, contract


def publish_tx(store, tx, token, journal, actual_after):
    row, contract = validate_journal_tx(store, tx, token, journal, actual_after)
    retained = tx.execute("SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
                          "WHERE k.activity_id=? AND k.idempotency_key=?",
                          (row["activity_id"], row["wave_key"] + ":integrated")).fetchone()
    try:
        integrated = json.loads(retained["payload"])["data"]
        material, evidence = integrated["material"], integrated["evidence"]
        if (material["after"] != contract["expected_after"] or material["before"] != contract["before"]
                or material["workspace"] != contract["authority"]["workspace"]
                or material["initial_head"] != contract["authority"]["base_commit"]
                or hashlib.sha256((_canonical(material) + "\n").encode()).hexdigest() != evidence["sha256"]):
            raise ValueError
    except (TypeError, KeyError, ValueError) as error:
        raise OwnershipRefused("WAVE_INTEGRATION_EVIDENCE_INVALID") from error
    encoded = _canonical(evidence)
    if row["state"] == "published" and row["evidence_json"] != encoded:
        raise OwnershipRefused("WAVE_INTEGRATION_CONFLICT")
    tx.execute("UPDATE authority_workspace_integrations SET state='published',evidence_json=?,"
               "publication_generation=? WHERE repository_id=? AND run_id=? AND wave_key=?",
               (encoded, token.generation, token.repository_id, token.run_id, row["wave_key"]))
    store._record_event_once_tx(tx, token, row["activity_id"], row["wave_key"] + ":journal-published",
                               {"contract_sha256": row["contract_sha256"], "evidence": evidence,
                                "publication_generation": token.generation})


def quarantine(store, token, *, wave_key, reason):
    ensure_schema(store)
    with store.transaction() as tx:
        assert_owner(tx, token)
        row = tx.execute("SELECT * FROM authority_workspace_integrations WHERE repository_id=? AND run_id=? "
                         "AND wave_key=?", (token.repository_id, token.run_id, wave_key)).fetchone()
        if row is None or row["state"] == "published":
            raise OwnershipRefused("WAVE_INTEGRATION_RECONCILIATION_REQUIRED")
        tx.execute("UPDATE authority_workspace_integrations SET state='quarantined' WHERE repository_id=? "
                   "AND run_id=? AND wave_key=?", (token.repository_id, token.run_id, wave_key))
        store._record_event_once_tx(tx, token, row["activity_id"], wave_key + ":integration-quarantined",
                                   {"contract_sha256": row["contract_sha256"], "reason": str(reason)})


def publish(store, token, *, wave_key, observed_after_material, evidence):
    """Publish previously captured/verified evidence under the original fence.

    Content validation happens before the short publication transaction. The
    immutable intent and current owner/issuing permit are rechecked inside it.
    """
    from pathlib import Path
    from .supervisor import _read_evidence
    from .wave_execution import _current_material, _material_record, _head
    after = _material(observed_after_material)
    if not isinstance(evidence, dict) or set(evidence) != {"locator", "sha256"}:
        raise OwnershipRefused("WAVE_INTEGRATION_EVIDENCE_INVALID")
    try:
        raw = _read_evidence(Path(evidence["locator"]))
        material = json.loads(raw)
        if (hashlib.sha256(raw).hexdigest() != evidence["sha256"]
                or material.get("schema") != "ffs.wave-integration/v1" or material.get("after") != after):
            raise ValueError
        workspace = Path(material["workspace"])
        if (_head(workspace) != material["initial_head"]
                or {path: _material_record(_current_material(workspace, path)) for path in after} != after):
            raise ValueError
    except (OSError, TypeError, ValueError, KeyError) as error:
        raise OwnershipRefused("WAVE_INTEGRATION_EVIDENCE_INVALID") from error
    ensure_schema(store)
    with store.transaction() as tx:
        row = tx.execute("SELECT * FROM authority_workspace_integrations WHERE repository_id=? AND run_id=? "
                         "AND wave_key=?", (token.repository_id, token.run_id, wave_key)).fetchone()
        if row is None or row["state"] == "quarantined":
            raise OwnershipRefused("WAVE_INTEGRATION_RECONCILIATION_REQUIRED")
        contract = json.loads(row["contract_json"])
        authority = _binding_tx(store, tx, token, wave_key, row["preparation_id"])
        if (authority != contract["authority"] or after != contract["expected_after"]
                or material.get("before") != contract["before"]
                or material.get("workspace") != authority["workspace"]
                or material.get("initial_head") != authority["base_commit"]
                or material.get("changed_files") != sorted(after)):
            raise OwnershipRefused("WAVE_INTEGRATION_EVIDENCE_INVALID")
        encoded = _canonical(evidence)
        if row["state"] == "published":
            if row["evidence_json"] != encoded:
                raise OwnershipRefused("WAVE_INTEGRATION_CONFLICT")
            return dict(row)
        tx.execute("UPDATE authority_workspace_integrations SET state='published',evidence_json=?,"
                   "publication_generation=? WHERE repository_id=? AND run_id=? AND wave_key=?",
                   (encoded, token.generation, token.repository_id, token.run_id, wave_key))
        store._record_event_once_tx(tx, token, row["activity_id"], wave_key + ":journal-published",
                                   {"contract_sha256": row["contract_sha256"], "evidence": evidence,
                                    "publication_generation": token.generation})
        return dict(tx.execute("SELECT * FROM authority_workspace_integrations WHERE repository_id=? AND run_id=? "
                               "AND wave_key=?", (token.repository_id, token.run_id, wave_key)).fetchone())
