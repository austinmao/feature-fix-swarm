"""Physical completion evidence checks outside the ControlStore writer."""
import json
from pathlib import Path

from .ownership import OwnershipRefused


def post_repair_review_tx(store, tx, token, acceptance_hash, candidate_hash):
    """Design row 213: a refused final review plus a journaled repair/recovery descendant.

    Returns the failed ``final_review`` receipt hash recorded on a strict
    ancestor of ``candidate_hash``, else None.  No second broad review exists;
    the caller still requires every mapped check to pass on the current
    candidate and no blocking finding for it.
    """
    from .run_policy import validate_role_receipt
    ancestors, current = [], candidate_hash
    while current is not None:
        row = tx.execute('SELECT parent_candidate_hash FROM authority_frontend_policy_candidates '
            'WHERE repository_id=? AND run_id=? AND acceptance_hash=? AND candidate_hash=?',
            (token.repository_id, token.run_id, acceptance_hash, current)).fetchone()
        current = None if row is None else row['parent_candidate_hash']
        if current is None or current == candidate_hash or current in ancestors:
            break
        ancestors.append(current)
    for row in tx.execute('SELECT receipt_hash,receipt_json FROM authority_acceptance_receipts '
            'WHERE repository_id=? AND run_id=? AND acceptance_hash=?',
            (token.repository_id, token.run_id, acceptance_hash)).fetchall():
        receipt = validate_role_receipt(json.loads(row['receipt_json']))
        if receipt.role != 'review' or receipt.completion_status != 'failed' or receipt.candidate_hash not in ancestors:
            continue
        action = tx.execute('SELECT a.action FROM authority_policy_actions a '
            'JOIN authority_policy_action_attempts p ON p.action_id=a.id WHERE p.intent_id=?',
            (receipt.intent_id,)).fetchone()
        if action is not None and action['action'] == 'final_review':
            store._acceptance_receipt_execution_tx(tx, token, receipt)
            return row['receipt_hash'], receipt
    return None


def verify_completion_evidence(store, token):
    from .wave_execution import _head, _inventory, _material_entries
    from .run_policy import validate_role_receipt
    with store.read_transaction() as tx:
        state = tx.execute('SELECT * FROM authority_frontend_policy_states WHERE repository_id=? AND run_id=?',
                           (token.repository_id, token.run_id)).fetchone()
        if state is None:
            raise OwnershipRefused('FRONTEND_STAGE_STALE')
        candidate = tx.execute('SELECT receipt_hash FROM authority_frontend_policy_candidates '
            'WHERE repository_id=? AND run_id=? AND acceptance_hash=? AND candidate_hash=?',
            (token.repository_id, token.run_id, state['acceptance_hash'], state['candidate_hash'])).fetchone()
        integration = None
        if candidate is not None:
            event = tx.execute('SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id '
                'WHERE k.idempotency_key=?', ('frontend-integration:' + candidate['receipt_hash'],)).fetchone()
            if event is None:
                raise OwnershipRefused('FRONTEND_INTEGRATION_REQUIRED')
            integration = json.loads(event['payload'])['data']
        checks = tx.execute('SELECT * FROM authority_frontend_policy_checks WHERE repository_id=? AND run_id=? '
            'AND acceptance_hash=? AND candidate_hash=?',
            (token.repository_id, token.run_id, state['acceptance_hash'], state['candidate_hash'])).fetchall()
        parents, references = [], []
        for check in checks:
            evidence = json.loads(check['evidence_json'])
            intent_id = store._frontend_check_execution_tx(tx, token, acceptance_hash=state['acceptance_hash'],
                candidate_hash=state['candidate_hash'], check_id=check['check_id'], status=check['status'], evidence=evidence)
            row = tx.execute('SELECT p.workspace_binding,w.base_commit,w.selected_manifest_json '
                'FROM authority_launch_intents i JOIN authority_child_bindings b ON b.activity_id=i.activity_id '
                'JOIN authority_child_bindings p ON p.activity_id=b.parent_activity_id '
                'JOIN context_workspaces w ON w.preparation_id=b.workspace_preparation_id WHERE i.id=?',
                (intent_id,)).fetchone()
            if row is None:
                raise OwnershipRefused('FRONTEND_COMPLETION_WORKSPACE_REQUIRED')
            parents.append(dict(row))
            references.extend(evidence)
        reviews = tx.execute('SELECT r.receipt_hash,r.receipt_json FROM authority_acceptance_receipts r '
            'WHERE r.repository_id=? AND r.run_id=? AND r.acceptance_hash=?',
            (token.repository_id, token.run_id, state['acceptance_hash'])).fetchall()
        accepted = []
        for row in reviews:
            receipt = validate_role_receipt(json.loads(row['receipt_json']))
            if receipt.role != 'review' or receipt.completion_status != 'succeeded' or receipt.candidate_hash != state['candidate_hash']:
                continue
            action = tx.execute('SELECT a.action FROM authority_policy_actions a '
                'JOIN authority_policy_action_attempts p ON p.action_id=a.id WHERE p.intent_id=?',
                (receipt.intent_id,)).fetchone()
            if action is None or action['action'] != 'final_review':
                continue
            store._acceptance_receipt_execution_tx(tx, token, receipt)
            accepted.append(row['receipt_hash'])
            references.extend(receipt.evidence)
        post_repair = None
        if not accepted:
            post_repair = post_repair_review_tx(store, tx, token, state['acceptance_hash'], state['candidate_hash'])
            if post_repair is not None:
                references.extend(post_repair[1].evidence)
    if integration is not None:
        from .candidate_chain import verify_candidate_chain
        proof = verify_candidate_chain(store, token, receipt_hash=candidate['receipt_hash'],
            candidate_hash=state['candidate_hash'], integration_evidence=integration['integration_evidence'],
            no_commit_evidence=integration['no_commit_evidence'])
        if proof != integration.get('candidate_chain'):
            raise OwnershipRefused('FRONTEND_INTEGRATION_CHAIN_INVALID')
    for reference in references:
        store._verified_evidence(reference)
    for parent in parents:
        workspace, base = Path(parent['workspace_binding']), parent['base_commit']
        expected = tuple(sorted(json.loads(parent['selected_manifest_json'])['entries'], key=lambda item:item['path']))
        if _head(workspace) != base or _material_entries(workspace, base, _inventory(workspace, base)) != expected:
            raise OwnershipRefused('FRONTEND_COMPLETION_CANDIDATE_STALE')
    return {'acceptance_hash':state['acceptance_hash'], 'candidate_hash':state['candidate_hash'],
            'generation':state['generation'], 'review_receipt_hashes':accepted,
            'post_repair_review_receipt_hash':None if post_repair is None else post_repair[0]}
