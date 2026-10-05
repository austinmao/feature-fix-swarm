"""The native spec review of an unsealed acceptance draft: its contract, its context, its record and the seal gate.

A review of the draft happens BEFORE it is sealed, so it cannot be an acceptance receipt (a role-``review`` receipt
would make the lifecycle skip the final review) and it cannot ride the sealed lifecycle (its stages need a seal).  It
is one keyed authority event under the reviewer child, ``spec-review:<draft_hash>``, written in the same fenced
transaction that ends the child.  ``require_spec_review_accepted`` is the only thing that lets ``seal_from_draft``
seal a draft that asked for the review: the record must exist, bind this exact draft, its material, its candidate,
its non-cancelled ``spec_review`` action and its succeeded intent (an attempt of that action), sit under this
draft's succeeded ``reviewer`` child, carry a verdict its own criteria support, and be an ``accept``.

``draft_hash`` is a function of the draft id, revision, legacy generation, legacy contract hash and material hash, so
a revised draft is a new hash with no record, and the same id and revision with other bytes refuses at creation.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json

from .ownership import OwnershipRefused, assert_owner
from .sealed_review import _host_final_text, _unique_keys
from .supervisor import SupervisorRefused, _canonical, _read_evidence
from .workspace import WorkspaceRefused, _from_row

SPEC_REVIEW_SCHEMA = "ffs.spec-review/v1"
CONTEXT_SCHEMA = "ffs.spec-review-context/v1"
RECORD_SCHEMA = "ffs.frontend-spec-review/v1"
CONTEXT_LIMIT = 64 * 1024
TEXT_LIMIT = 1024           # bytes of one criterion reason or note
NOTES_LIMIT = 8
STATUSES = ("acceptable", "revise")
VERDICTS = ("accept", "revise")
REVIEW_QUESTIONS = (
    "Does each criterion's objective_clause state an outcome that can be verified, and does it serve the objective?",
    "Could each criterion's checks pass while its objective_clause is not met, or fail while it is?",
    "Do the evidence rules ask for evidence a reviewer could verify later?",
    "Are the exclusions and global invariants consistent with the objective and with each other?",
)


def spec_review_record_key(draft_hash: str) -> str:
    return "spec-review:" + draft_hash


def spec_review_launch_key(draft_hash: str) -> str:
    """The logical key of the draft's one review grant and launch (``produce_spec_review``)."""
    return f"spec-review:{draft_hash[:16]}:launch"


def material_hash_of(draft) -> str:
    from .run_policy import validate_draft_material
    return validate_draft_material(draft.material).material_hash


def spec_review_output_contract(draft) -> dict:
    """Describe the response grammar of the spec review; this is not acceptance evidence."""
    return {
        "required_fields": ["schema", "draft_hash", "candidate_hash", "verdict", "criteria", "notes"],
        "additional_fields": False,
        "fixed_fields": {"schema": SPEC_REVIEW_SCHEMA, "draft_hash": draft.draft_hash,
                         "candidate_hash": draft.material["candidate_hash"]},
        "verdict_values": list(VERDICTS),
        "verdict_rule": "accept exactly when every criterion is acceptable; revise when any criterion is not.",
        "criterion_result": {
            "required_fields": ["status", "reason"], "additional_fields": False, "status_values": list(STATUSES),
            "reason": "plain text, at most %d bytes" % TEXT_LIMIT,
        },
        "criteria": {item["id"]: {} for item in sorted(draft.material["criteria"], key=lambda item: item["id"])},
        "criteria_membership": "Exactly the listed criterion IDs; each value follows criterion_result; no omissions or extra IDs.",
        "notes": {"type": "array", "max_items": NOTES_LIMIT, "item": "plain text, at most %d bytes" % TEXT_LIMIT},
        "instructions": [
            "Return exactly the required top-level fields and exact fixed values.",
            "Judge the draft in review_context.draft against the objective; you are not asked to run anything.",
            "Mark a criterion revise when its clause, checks or evidence rules cannot establish the objective.",
            "This response is subject to independent process and authority validation.",
        ],
    }


def spec_review_attempted(store, token, *, draft_id: str, revision: int) -> bool:
    """Whether the persisted draft row of this id and revision has a spec-review record or a live spec_review grant.

    The opt-in (``"spec_review": "native"``) is not part of the draft's material or hash, so it cannot be read back
    from the row.  This is the part of it that outlives the key: once a review of the row was granted or recorded, a
    seal of that same row that does not ask for the review is refused (``seal_from_draft``).  A released grant was
    never a review, and a row nobody reviewed is the operator's to seal either way.
    """
    with store.read_transaction() as tx:
        tables = {row[0] for row in tx.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('authority_acceptance_drafts','authority_policy_actions')")}
        row = None if "authority_acceptance_drafts" not in tables else tx.execute(
            "SELECT draft_hash FROM authority_acceptance_drafts WHERE repository_id=? AND run_id=? AND draft_id=? "
            "AND revision=?", (token.repository_id, token.run_id, draft_id, revision)).fetchone()
        if row is None:
            return False
        recorded = tx.execute(
            "SELECT 1 FROM authority_event_keys k JOIN authority_activities a ON a.id=k.activity_id "
            "WHERE k.idempotency_key=? AND a.repository_id=? AND a.run_id=?",
            (spec_review_record_key(row["draft_hash"]), token.repository_id, token.run_id)).fetchone()
        granted = "authority_policy_actions" in tables and tx.execute(
            "SELECT 1 FROM authority_policy_actions WHERE repository_id=? AND run_id=? AND action='spec_review' "
            "AND logical_key=? AND state<>'cancelled'",
            (token.repository_id, token.run_id, spec_review_launch_key(row["draft_hash"]))).fetchone()
    return recorded is not None or bool(granted)


def load_draft(store, token, draft_hash: str):
    """The persisted draft with this hash and whether it is already sealed: ``(draft or None, sealed)``."""
    with store.read_transaction() as tx:
        row = tx.execute("SELECT * FROM authority_acceptance_drafts WHERE repository_id=? AND run_id=? AND draft_hash=?",
                         (token.repository_id, token.run_id, draft_hash)).fetchone()
        sealed = tx.execute("SELECT 1 FROM authority_sealed_acceptances WHERE repository_id=? AND run_id=? "
                            "AND draft_hash=?", (token.repository_id, token.run_id, draft_hash)).fetchone()
    return (None if row is None else store._acceptance_draft_from_row(row)), sealed is not None


def spec_review_input_context(store, token, *, draft, reviewer_activity_id, selected_artifacts) -> dict:
    """Read semantics only: the draft, the objective and the reviewer's own binding; no authority is created.

    Mirrors ``final_review_input_context`` for an unsealed draft: the reviewer must be this owner's, bound to this
    draft hash and its candidate, in a ready ``reviewer`` workspace holding exactly that candidate.
    """
    def refused():
        raise SupervisorRefused("SPEC_REVIEW_CONTEXT_INVALID")

    material = draft.material
    try:
        with store.read_transaction() as tx:
            assert_owner(tx, token)
            stored = tx.execute("SELECT 1 FROM authority_acceptance_drafts WHERE repository_id=? AND run_id=? "
                                "AND draft_hash=?", (token.repository_id, token.run_id, draft.draft_hash)).fetchone()
            run = tx.execute("SELECT objective_text FROM context_runs WHERE repository_id=? AND run_id=?",
                             (token.repository_id, token.run_id)).fetchone()
            activity = store._assert_activity_binding(tx, token, reviewer_activity_id)
            child = tx.execute("SELECT * FROM authority_child_bindings WHERE activity_id=?",
                               (reviewer_activity_id,)).fetchone()
            preparation = (None if child is None else tx.execute(
                "SELECT * FROM context_workspaces WHERE preparation_id=?", (child["workspace_preparation_id"],)).fetchone())
            if (stored is None or run is None or child is None or preparation is None
                    or activity["generation"] != token.generation
                    or child["role"] != "reviewer" or child["contract_hash"] != draft.draft_hash
                    or child["candidate_hash"] != material["candidate_hash"]
                    or preparation["repository_id"] != token.repository_id or preparation["run_id"] != token.run_id
                    or preparation["generation"] != token.generation
                    or preparation["state"] != "ready" or preparation["child_role"] != "reviewer"
                    or child["workspace_binding"] != preparation["path"]
                    or _from_row(preparation).input_digest != material["candidate_hash"]):
                refused()
        context = {
            "schema": CONTEXT_SCHEMA, "objective_text": run["objective_text"],
            "objective_digest": material["objective_digest"],
            "accepted_requirement_ids": sorted(item["id"] for item in material["criteria"]),
            "legacy_contract_hash": draft.acceptance_contract_hash,
            "draft": {
                "draft_id": draft.draft_id, "revision": draft.revision, "draft_hash": draft.draft_hash,
                "material_hash": material_hash_of(draft), "criteria": material["criteria"],
                "exclusions": material["exclusions"], "global_invariants": material["global_invariants"],
                "candidate_hash": material["candidate_hash"], "command_mode": material["command_mode"],
            },
            "selected_artifacts": dict(sorted(dict(selected_artifacts).items())),
            "review_questions": list(REVIEW_QUESTIONS),
        }
        if len(_canonical(context)) > CONTEXT_LIMIT:
            refused()
        return context
    except (OwnershipRefused, WorkspaceRefused, KeyError, TypeError, ValueError, OSError) as error:
        raise SupervisorRefused("SPEC_REVIEW_CONTEXT_INVALID") from error


def _text(value) -> bool:
    return isinstance(value, str) and "\0" not in value and len(value.encode("utf-8")) <= TEXT_LIMIT


def _output(result: dict, raw: bytes) -> dict:
    try:
        if result.get("host_receipt") is None:
            value = json.loads(raw, object_pairs_hook=_unique_keys)
        else:
            value = json.loads(_host_final_text(result, raw), object_pairs_hook=_unique_keys)
        if not isinstance(value, dict):
            raise ValueError("invalid review output")
        return value
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        raise SupervisorRefused("SPEC_REVIEW_OUTPUT_INVALID") from error


def _checked(output: dict, draft) -> None:
    """The reply must match the published grammar exactly, and its verdict must follow from its criteria."""
    try:
        fixed = spec_review_output_contract(draft)["fixed_fields"]
        criteria, notes = output["criteria"], output["notes"]
        if (set(output) != {"schema", "draft_hash", "candidate_hash", "verdict", "criteria", "notes"}
                or any(output[key] != value for key, value in fixed.items())
                or output["verdict"] not in VERDICTS or not isinstance(criteria, dict)
                or set(criteria) != {item["id"] for item in draft.material["criteria"]}
                or not isinstance(notes, list) or len(notes) > NOTES_LIMIT or not all(_text(item) for item in notes)
                or any(not isinstance(item, dict) or set(item) != {"status", "reason"}
                       or item["status"] not in STATUSES or not _text(item["reason"]) for item in criteria.values())
                or (output["verdict"] == "accept") != all(item["status"] == "acceptable" for item in criteria.values())):
            raise ValueError("invalid spec review output")
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        raise SupervisorRefused("SPEC_REVIEW_OUTPUT_INVALID") from error


def record_spec_review(supervisor, handle, *, draft) -> dict:
    """Record only a complete, grammatical review of exactly this draft; end the reviewer child with it.

    The record and the child's ``succeeded`` state commit in one fenced transaction, so a crash leaves both or
    neither.  Returns the record (``verdict`` is ``accept`` or ``revise``; the caller decides what a revise costs).
    """
    if (supervisor._handles.get(handle.intent_id) is not handle or not handle.recorded
            or not isinstance(handle.result, dict) or handle.result.get("returncode") != 0):
        raise SupervisorRefused("SPEC_REVIEW_RESULT_REQUIRED")
    store, token = supervisor.store, supervisor.token
    candidate_hash = draft.material["candidate_hash"]
    with store.read_transaction() as tx:
        child = tx.execute("SELECT * FROM authority_child_bindings WHERE activity_id=?", (handle.activity_id,)).fetchone()
        intent = tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (handle.intent_id,)).fetchone()
        action = tx.execute(
            "SELECT a.* FROM authority_policy_actions a JOIN authority_policy_action_attempts p ON p.action_id=a.id "
            "WHERE p.intent_id=?", (handle.intent_id,)).fetchone()
    if (child is None or child["role"] != "reviewer" or child["contract_hash"] != draft.draft_hash
            or child["candidate_hash"] != candidate_hash or intent is None or intent["completion_status"] != "succeeded"
            or action is None or action["action"] != "spec_review"):
        raise SupervisorRefused("SPEC_REVIEW_BINDING_INVALID")
    raw = _read_evidence(handle.stdout_path, handle.stream_identities["stdout"])
    stdout = handle.result["streams"]["stdout"]["sha256"]
    if hashlib.sha256(raw).hexdigest() != stdout:
        raise SupervisorRefused("EVIDENCE_CHANGED")
    if "native_review_material" in (handle.replay_material or {}):
        from .native_review_supervision import completion
        proof, _usage = completion(supervisor, handle, handle.result, raw)
        if (proof.get("status") != "complete"
                or json.dumps(proof, sort_keys=True) != json.dumps(handle.result.get("host_receipt"), sort_keys=True)):
            raise SupervisorRefused("SPEC_REVIEW_NATIVE_PROOF_INVALID")
    output = _output(handle.result, raw)
    _checked(output, draft)
    payload = {
        "schema": RECORD_SCHEMA, "repository_id": token.repository_id, "run_id": token.run_id,
        "draft_hash": draft.draft_hash, "material_hash": material_hash_of(draft), "candidate_hash": candidate_hash,
        "action_id": action["id"], "intent_id": handle.intent_id, "fence_generation": intent["generation"],
        "verdict": output["verdict"], "criteria": output["criteria"], "notes": output["notes"],
        "output": {"locator": str(handle.stdout_path), "sha256": stdout},
        "process_identity": asdict(handle.identity),
    }
    with store.transaction() as tx:
        assert_owner(tx, token)
        store._record_event_once_tx(tx, token, handle.activity_id, spec_review_record_key(draft.draft_hash), payload)
        state = tx.execute("SELECT state FROM authority_activities WHERE id=?", (handle.activity_id,)).fetchone()["state"]
        if state == "active":
            store._transition_activity_tx(tx, token, handle.activity_id, expected="active", new="succeeded",
                                          result=handle.result["evidence"], reason="spec review recorded")
    return payload


def retained_spec_review(store, token, draft) -> dict | None:
    """The verified record of this exact draft, ``None`` when none exists; any record that fails a binding refuses."""
    def invalid():
        raise SupervisorRefused("SPEC_REVIEW_RECORD_INVALID")

    with store.read_transaction() as tx:
        rows = tx.execute(
            "SELECT k.activity_id,k.payload_hash,e.payload FROM authority_event_keys k JOIN control_events e "
            "ON e.id=k.event_id JOIN authority_activities a ON a.id=k.activity_id WHERE k.idempotency_key=? "
            "AND a.repository_id=? AND a.run_id=?",
            (spec_review_record_key(draft.draft_hash), token.repository_id, token.run_id)).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            invalid()
        try:
            payload = json.loads(rows[0]["payload"])["data"]
            action_id, intent_id = payload.get("action_id"), payload.get("intent_id")
        except (KeyError, TypeError, ValueError, AttributeError):
            invalid()
        action = tx.execute("SELECT * FROM authority_policy_actions WHERE id=? AND repository_id=? AND run_id=?",
                            (action_id, token.repository_id, token.run_id)).fetchone()
        intent = tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (intent_id,)).fetchone()
        # The event is only as good as the activity it sits under: that must be this draft's finished reviewer.
        child = tx.execute("SELECT * FROM authority_child_bindings WHERE activity_id=?",
                           (rows[0]["activity_id"],)).fetchone()
        reviewer = tx.execute("SELECT state FROM authority_activities WHERE id=?", (rows[0]["activity_id"],)).fetchone()
        attempt = tx.execute("SELECT 1 FROM authority_policy_action_attempts WHERE action_id=? AND intent_id=?",
                             (action_id, intent_id)).fetchone()
    if (rows[0]["payload_hash"] != hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            or payload.get("schema") != RECORD_SCHEMA
            or payload.get("repository_id") != token.repository_id or payload.get("run_id") != token.run_id
            or payload.get("draft_hash") != draft.draft_hash or payload.get("material_hash") != material_hash_of(draft)
            or payload.get("candidate_hash") != draft.material["candidate_hash"]
            or payload.get("verdict") not in VERDICTS
            or action is None or action["action"] != "spec_review" or action["state"] == "cancelled"
            or intent is None or action["intent_id"] != intent["id"] or intent["activity_id"] != rows[0]["activity_id"]
            or intent["completion_status"] != "succeeded"
            or attempt is None or child is None or child["role"] != "reviewer"
            or child["contract_hash"] != draft.draft_hash or child["candidate_hash"] != draft.material["candidate_hash"]
            or not child["workspace_binding"] or reviewer is None or reviewer["state"] != "succeeded"):
        invalid()
    try:
        # The stored verdict must follow from the stored criteria, under the same grammar as the reply itself.
        _checked({**spec_review_output_contract(draft)["fixed_fields"], "verdict": payload["verdict"],
                  "criteria": payload["criteria"], "notes": payload["notes"]}, draft)
        store._verified_evidence(payload.get("output"))
    except (SupervisorRefused, OwnershipRefused, KeyError):
        invalid()
    return payload


def require_spec_review_accepted(store, token, draft) -> dict:
    """The seal gate: only a verified ``accept`` record of this exact draft lets it be sealed."""
    record = retained_spec_review(store, token, draft)
    if record is None or record["verdict"] != "accept":
        raise SupervisorRefused("SPEC_REVIEW_REQUIRED")
    return record
