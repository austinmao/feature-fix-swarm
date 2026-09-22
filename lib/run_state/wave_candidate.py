"""Bind one finished supervised wave execution to the frontend candidate.

Production counterpart of the sequence that only tests orchestrated before:
execution receipt -> ``record_frontend_integration`` -> ``bind_frontend_candidate``.
Every input is read from retained authority events of the outer execution
intent; the caller selects no digest.  ``verify_candidate_chain`` (inside the
store calls) stays the authority; this module only assembles its inputs.  The
GSD adapter's no-commit receipt must already exist on disk; this never writes it.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from .candidate_chain import _event, _read
from .ownership import OwnershipRefused


def bind_wave_execution_candidate(store, token, *, sealed, handle, request_key, ready, process_evidence):
    """Return ``(recorded_receipt, frontend_state)``; replay reuses the retained rows."""
    with store.read_transaction() as tx:
        journals = tx.execute(
            "SELECT wave_key,event_id FROM authority_workspace_integrations WHERE repository_id=? AND run_id=? "
            "AND issuing_intent_id=? ORDER BY event_id", (token.repository_id, token.run_id, handle.intent_id)).fetchall()
        if not journals:
            raise OwnershipRefused("FRONTEND_INTEGRATION_CHAIN_INCOMPLETE")
        replies = [(row["wave_key"], _event(tx, handle.activity_id, row["wave_key"] + ":reply")[0]) for row in journals]
        last = journals[-1]
        output = _event(tx, handle.activity_id, last["wave_key"] + ":candidate-output")[0]
        integrated = _event(tx, handle.activity_id, last["wave_key"] + ":integrated")[0]
        request = json.loads(tx.execute("SELECT payload FROM control_events WHERE id=?",
                                        (last["event_id"],)).fetchone()["payload"])["data"]["body"]
        # A descendant's separately qualified workspace has its own runtime identity.
        runtime_hash = tx.execute("SELECT runtime_tuple_hash FROM authority_activities WHERE id=?",
                                  (handle.activity_id,)).fetchone()["runtime_tuple_hash"]
    workspace = Path(integrated["material"]["workspace"])
    manifest = _read({"locator": str(workspace / request["manifest_locator"]), "sha256": request["manifest_sha256"]})
    completion = (workspace / ".planning/.ffs-supervised/waves" / handle.activity_id
                  / ("wave-" + str(manifest["wave"]) + ".result.json.receipt.json"))
    try:
        no_commit = {"locator": str(completion), "sha256": hashlib.sha256(completion.read_bytes()).hexdigest()}
    except OSError as error:
        raise OwnershipRefused("FRONTEND_NO_COMMIT_RECEIPT_INVALID") from error
    receipt = {
        "schema": "ffs.run-policy-receipt/v1", "role": "execution", "request_key": request_key,
        "activity_id": handle.activity_id, "intent_id": handle.intent_id, "fence_generation": token.generation,
        "acceptance_hash": sealed.acceptance_hash, "candidate_hash": ready.input_digest,
        "runtime_hash": runtime_hash,
        "workspace_preparation_hash": hashlib.sha256(json.dumps(
            asdict(ready), default=str, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "evidence": [{"id": "wave-result:" + key, **reply["evidence"]} for key, reply in replies]
                    + [{"id": "process-result", **process_evidence}],
        "completion_status": "succeeded", "process_identity": asdict(handle.identity), "review_dimensions": [],
    }
    recorded = store.record_acceptance_receipt(token, acceptance_hash=sealed.acceptance_hash, receipt=receipt)
    store.record_frontend_integration(token, receipt_hash=recorded.receipt_hash,
                                      candidate_hash=output["output_digest"],
                                      integration_evidence=integrated["evidence"], no_commit_evidence=no_commit)
    state = store.bind_frontend_candidate(token, acceptance_hash=sealed.acceptance_hash,
                                          candidate_hash=output["output_digest"],
                                          receipt_hash=recorded.receipt_hash,
                                          parent_candidate_hash=ready.input_digest)
    return recorded, state
