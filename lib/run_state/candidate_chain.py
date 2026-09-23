"""Read-only proof joining original execution input to journaled output bytes."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .ownership import OwnershipRefused


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _event(tx, activity, key):
    row = tx.execute('SELECT k.payload_hash,k.event_id,e.payload FROM authority_event_keys k '
        'JOIN control_events e ON e.id=k.event_id WHERE k.activity_id=? AND k.idempotency_key=?',
        (activity, key)).fetchone()
    if row is None:
        raise OwnershipRefused('FRONTEND_INTEGRATION_CHAIN_INCOMPLETE')
    value = json.loads(row['payload'])['data']
    if hashlib.sha256(_canonical(value)).hexdigest() != row['payload_hash']:
        raise OwnershipRefused('FRONTEND_INTEGRATION_CHAIN_INVALID')
    return value, {'event_id':row['event_id'], 'payload_hash':row['payload_hash']}


def _read(reference):
    from .supervisor import _read_evidence
    raw = _read_evidence(Path(reference['locator']))
    if hashlib.sha256(raw).hexdigest() != reference['sha256']:
        raise OwnershipRefused('FRONTEND_INTEGRATION_EVIDENCE_CHANGED')
    return json.loads(raw)


def verify_candidate_chain(store, token, *, receipt_hash, candidate_hash,
                           integration_evidence, no_commit_evidence):
    """Verify all completed waves and the adapter receipts for one execution.

    No review receipt, arbitrary file hash, or caller-selected output digest
    substitutes for the original successful execution and published chain.
    """
    from .integration_journal import validate_completed_publication_tx
    from .run_policy import validate_role_receipt
    from .supervisor import _read_evidence
    from .wave_execution import _head, _inventory, _material_entries
    try:
        with store.read_transaction() as tx:
            row = tx.execute('SELECT receipt_json FROM authority_acceptance_receipts '
                'WHERE repository_id=? AND run_id=? AND receipt_hash=?',
                (token.repository_id, token.run_id, receipt_hash)).fetchone()
            if row is None:
                raise OwnershipRefused('FRONTEND_INTEGRATION_RECEIPT_REQUIRED')
            typed = validate_role_receipt(json.loads(row['receipt_json']))
            if typed.role not in {'execution', 'recovery'} or typed.completion_status != 'succeeded':
                raise OwnershipRefused('FRONTEND_INTEGRATION_RECEIPT_REQUIRED')
            store._acceptance_receipt_execution_tx(tx, token, typed)
            journals = tx.execute('SELECT wave_key FROM authority_workspace_integrations '
                'WHERE repository_id=? AND run_id=? AND issuing_intent_id=? ORDER BY event_id',
                (token.repository_id, token.run_id, typed.intent_id)).fetchall()
            if not journals:
                raise OwnershipRefused('FRONTEND_INTEGRATION_CHAIN_INCOMPLETE')
            records, bindings = [], []
            for journal in journals:
                key = journal['wave_key']
                record = validate_completed_publication_tx(store, tx, token, wave_key=key, intent_id=typed.intent_id)
                output, output_binding = _event(tx, typed.activity_id, key + ':candidate-output')
                request_row = tx.execute('SELECT payload FROM control_events WHERE id=?',
                                        (record['journal']['event_id'],)).fetchone()
                request = json.loads(request_row['payload'])['data']
                bindings.append(output_binding)
                if key.startswith('recovery-trial:'):
                    records.append((record, output, None, request))
                    continue
                reply, reply_binding = _event(tx, typed.activity_id, key + ':reply')
                records.append((record, output, reply, request))
                bindings.append(reply_binding)
        for item in typed.evidence:
            store._verified_evidence(item)
        previous = typed.candidate_hash
        last_receipt_reference = None
        last_material = None
        for record, output, reply, request in records:
            material = _read(output['evidence'])
            if (material['schema'] != 'ffs.wave-candidate-output/v1'
                    or material['journal_sha256'] != record['journal']['contract_sha256']
                    or output['journal_sha256'] != material['journal_sha256']
                    or output['input_digest'] != previous or material['input_digest'] != previous
                    or material['output_digest'] != output['output_digest']
                    or material['workspace'] != record['contract']['authority']['workspace']
                    or material['base_commit'] != record['contract']['authority']['base_commit']):
                raise OwnershipRefused('FRONTEND_INTEGRATION_CHAIN_INVALID')
            if _read(record['evidence']) != record['material']:
                raise OwnershipRefused('FRONTEND_INTEGRATION_CHAIN_INVALID')
            if reply is None:
                # Recovery winner: the journaled bytes are the trial's retained
                # patch and its record passed every frozen check; no GSD wave
                # manifest/result/no-commit receipt exists for it.
                if (request.get('schema') != 'ffs.recovery-trial-checks/v1'
                        or record['material']['patch_sha256'] != request['patch']['sha256']
                        or record['contract']['authority']['patch_sha256'] != request['patch']['sha256']
                        or any(item['status'] != 'passed' for item in request['results'])):
                    raise OwnershipRefused('FRONTEND_INTEGRATION_CHAIN_INVALID')
                store._verified_evidence({'locator':request['patch']['locator'], 'sha256':request['patch']['sha256']})
                previous, last_material, last_receipt_reference = output['output_digest'], material, None
                continue
            body = request['body']
            if body['manifest_locator'] != '.planning/.ffs-wave-requests/' + body['manifest_sha256'] + '.json':
                raise OwnershipRefused('FRONTEND_INTEGRATION_CHAIN_INVALID')
            manifest = _read({'locator':str(Path(material['workspace']) / body['manifest_locator']),
                              'sha256':body['manifest_sha256']})
            root = Path(material['workspace']) / '.planning/.ffs-supervised/waves' / typed.activity_id
            prefix = root / ('wave-' + str(manifest['wave']))
            adapter_manifest = json.loads(_read_evidence(Path(str(prefix) + '.manifest.json')))
            result_raw = _read_evidence(Path(str(prefix) + '.result.json'))
            receipt_path = Path(str(prefix) + '.result.json.receipt.json')
            receipt_raw = _read_evidence(receipt_path)
            receipt = json.loads(receipt_raw)
            retained_reply = _read(reply['evidence'])
            if (adapter_manifest != manifest or manifest['commit_mode'] != 'patches'
                    or retained_reply != reply['reply']
                    or json.loads(result_raw) != retained_reply
                    or receipt != {'schema':'ffs.gsd-no-commit-completion/v1',
                        'manifest_sha256':hashlib.sha256(_canonical(manifest)).hexdigest(),
                        'result_sha256':hashlib.sha256(result_raw).hexdigest(),
                        'initial_head':material['base_commit'], 'commit_mode':'patches'}):
                raise OwnershipRefused('FRONTEND_NO_COMMIT_RECEIPT_INVALID')
            last_receipt_reference = {'locator':str(receipt_path), 'sha256':hashlib.sha256(receipt_raw).hexdigest()}
            previous, last_material = output['output_digest'], material
        if (previous != candidate_hash or records[-1][0]['evidence'] != integration_evidence
                or last_receipt_reference != no_commit_evidence):
            raise OwnershipRefused('FRONTEND_INTEGRATION_CANDIDATE_INVALID')
        workspace = Path(last_material['workspace'])
        base = last_material['base_commit']
        entries = tuple(sorted(last_material['manifest']['entries'], key=lambda item:item['path']))
        if _head(workspace) != base or _material_entries(workspace, base, _inventory(workspace, base)) != entries:
            raise OwnershipRefused('FRONTEND_INTEGRATION_CANDIDATE_STALE')
        return {'input_candidate_hash':typed.candidate_hash, 'candidate_hash':candidate_hash,
                'intent_id':typed.intent_id, 'activity_id':typed.activity_id,
                'event_bindings':bindings, 'journal_hashes':[record[0]['journal']['contract_sha256'] for record in records]}
    except OwnershipRefused:
        raise
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise OwnershipRefused('FRONTEND_INTEGRATION_CHAIN_INVALID') from error


@dataclass(frozen=True)
class CurrentFrontendCandidate:
    acceptance_hash: str
    candidate_hash: str
    receipt_hash: str
    workspace: str
    workspace_preparation_id: str
    parent_activity_id: str
    runtime_identity: str
    base_commit: str


def resolve_current_frontend_candidate(store, token) -> CurrentFrontendCandidate | None:
    """Locate journaled current bytes and their live parent without new authority.

    None denotes only the untouched sealed input. Integration proof retains its
    existing format; location comes from the final evidence it already checks.
    """
    from .ownership import assert_owner
    from .workspace import _assert_preparation_binding, _from_row

    def invalid():
        raise OwnershipRefused('FRONTEND_CURRENT_CANDIDATE_INVALID')

    def projection(tx):
        state = tx.execute('SELECT * FROM authority_frontend_policy_states WHERE repository_id=? AND run_id=?',
                           (token.repository_id, token.run_id)).fetchone()
        if state is None:
            invalid()
        candidate = tx.execute('SELECT * FROM authority_frontend_policy_candidates WHERE repository_id=? '
            'AND run_id=? AND acceptance_hash=? AND candidate_hash=?',
            (token.repository_id, token.run_id, state['acceptance_hash'], state['candidate_hash'])).fetchone()
        return dict(state), None if candidate is None else dict(candidate)

    try:
        sealed = store.get_sealed_acceptance(repository_id=token.repository_id, run_id=token.run_id)
        with store.read_transaction() as tx:
            assert_owner(tx, token)
            state, candidate = projection(tx)
            if (sealed is None or state['acceptance_hash'] != sealed.acceptance_hash
                    or state['generation'] != sealed.legacy_generation):
                invalid()
            if candidate is None:
                prior = tx.execute('SELECT 1 FROM authority_frontend_policy_candidates WHERE repository_id=? AND run_id=?',
                                   (token.repository_id, token.run_id)).fetchone()
                if prior is not None or state['candidate_hash'] != sealed.material['candidate_hash']:
                    invalid()
                return None
            if candidate['generation'] != state['generation']:
                invalid()
            key = 'frontend-integration:' + candidate['receipt_hash']
            rows = tx.execute('SELECT k.activity_id,e.payload FROM authority_event_keys k '
                'JOIN control_events e ON e.id=k.event_id JOIN authority_activities a ON a.id=k.activity_id '
                'WHERE k.idempotency_key=? AND a.repository_id=? AND a.run_id=?',
                (key, token.repository_id, token.run_id)).fetchall()
            if len(rows) != 1:
                invalid()
            activity_id = rows[0]['activity_id']
            wrapped = json.loads(rows[0]['payload'])
            if wrapped['run_id'] != token.run_id or wrapped['activity_id'] != activity_id:
                invalid()
            integration, event_binding = _event(tx, activity_id, key)
            if (integration['receipt_hash'] != candidate['receipt_hash']
                    or integration['candidate_hash'] != state['candidate_hash']):
                invalid()
        proof = verify_candidate_chain(store, token, receipt_hash=candidate['receipt_hash'],
            candidate_hash=state['candidate_hash'], integration_evidence=integration['integration_evidence'],
            no_commit_evidence=integration['no_commit_evidence'])
        if proof != integration['candidate_chain'] or proof['activity_id'] != activity_id:
            invalid()
        location = _read(integration['integration_evidence'])
        if location['schema'] != 'ffs.wave-integration/v1' or not Path(location['workspace']).is_absolute():
            invalid()
        with store.read_transaction() as tx:
            assert_owner(tx, token)
            if projection(tx) != (state, candidate) or _event(tx, activity_id, key) != (integration, event_binding):
                invalid()
            root = tx.execute('SELECT activity_id FROM context_runs WHERE repository_id=? AND run_id=?',
                              (token.repository_id, token.run_id)).fetchone()
            current, visited, selected = activity_id, set(), None
            while current is not None:
                if current in visited or len(visited) >= 1024:
                    invalid()
                visited.add(current)
                activity = store._assert_activity_binding(tx, token, current)
                child = tx.execute('SELECT * FROM authority_child_bindings WHERE activity_id=?', (current,)).fetchone()
                if child is None:
                    if root is None or current != root['activity_id']:
                        invalid()
                    break
                if selected is None and activity['state'] == 'active' and child['workspace_binding'] == location['workspace']:
                    ready = tx.execute('SELECT * FROM context_workspaces WHERE preparation_id=?',
                                       (child['workspace_preparation_id'],)).fetchone()
                    _assert_preparation_binding(ready, token, require_generation=True, tx=tx)
                    if (activity['generation'] != token.generation or ready['state'] != 'ready' or not ready['created_by_ffs']
                            or ready['path'] != location['workspace'] or ready['base_commit'] != location['initial_head']
                            or ready['parent_activity_id'] != child['parent_activity_id'] or ready['child_role'] != child['role']
                            or not store._valid_digest(activity['runtime_tuple_hash'])
                            or activity['runtime_tuple_hash'] != child['runtime_identity']
                            or activity['input_digest'] != child['candidate_hash']
                            or child['candidate_hash'] != _from_row(ready).input_digest):
                        invalid()
                    selected = CurrentFrontendCandidate(state['acceptance_hash'], state['candidate_hash'],
                        candidate['receipt_hash'], ready['path'], ready['preparation_id'], current,
                        activity['runtime_tuple_hash'], ready['base_commit'])
                current = child['parent_activity_id']
            if selected is None:
                invalid()
            store._assert_activity_ancestry(tx, selected.parent_activity_id,
                                           repository_id=token.repository_id, run_id=token.run_id)
            return selected
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise OwnershipRefused('FRONTEND_CURRENT_CANDIDATE_INVALID') from error
