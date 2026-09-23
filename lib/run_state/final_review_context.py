"""Canonical sealed-review inputs derived from current supervised checks."""
import hashlib
import json
from pathlib import Path

from .native_review_runtime import NativeReviewRuntimeRefused, _read_checked
from .ownership import OwnershipRefused, assert_owner
from .supervisor import SupervisorRefused, _canonical, artifact_review_inputs
from .workspace import WorkspaceRefused, _from_row


CONTEXT_LIMIT = 64 * 1024
SOURCE_EVIDENCE_POLICY = {
    'names': 'selected_sources keys are artifact names, never evidence IDs.',
    'criterion_finding': 'Name the criterion and at least one mapped check; cite the selected source descriptor using that criterion evidence-rule ID.',
    'invariant_finding': 'Name the invariant; cite the selected source descriptor using that invariant ID.',
    'check_and_verdict': 'Check-ID findings and criterion verdicts may cite only their mapped check terminal receipts, never selected sources.',
}


def final_review_input_context(store, token, *, acceptance_hash, candidate_hash,
                               reviewer_activity_id=None, selected_artifacts=None):
    """Read semantics and verified check output without creating authority.

    Result metadata is projected to the exit status and exact UTF-8 streams;
    the full terminal receipt stays available through its original hash-bound
    descriptor. Nothing is truncated to make a review prompt fit.
    """
    def refused():
        raise SupervisorRefused('FINAL_REVIEW_CONTEXT_INVALID')

    read_references = {}

    def snapshot(tx):
        assert_owner(tx, token)
        state = tx.execute('SELECT * FROM authority_frontend_policy_states '
            'WHERE repository_id=? AND run_id=?', (token.repository_id, token.run_id)).fetchone()
        seal = tx.execute('SELECT * FROM authority_sealed_acceptances '
            'WHERE repository_id=? AND run_id=? AND acceptance_hash=?',
            (token.repository_id, token.run_id, acceptance_hash)).fetchone()
        if (state is None or seal is None or state['acceptance_hash'] != acceptance_hash
                or state['candidate_hash'] != candidate_hash or state['stage'] != 'FINAL_REVIEW'):
            refused()
        rows = tx.execute('SELECT check_id,status,evidence_json FROM authority_frontend_policy_checks '
            'WHERE repository_id=? AND run_id=? AND acceptance_hash=? AND candidate_hash=? ORDER BY check_id',
            (token.repository_id, token.run_id, acceptance_hash, candidate_hash)).fetchall()
        reviewer = None
        if reviewer_activity_id is not None:
            activity = store._assert_activity_binding(tx, token, reviewer_activity_id)
            child = tx.execute('SELECT * FROM authority_child_bindings WHERE activity_id=?',
                               (reviewer_activity_id,)).fetchone()
            preparation = (tx.execute('SELECT * FROM context_workspaces WHERE preparation_id=?',
                (child['workspace_preparation_id'],)).fetchone() if child is not None else None)
            if (child is None or preparation is None or activity['generation'] != token.generation
                    or child['role'] != 'reviewer' or child['contract_hash'] != acceptance_hash
                    or child['candidate_hash'] != candidate_hash
                    or preparation['repository_id'] != token.repository_id
                    or preparation['run_id'] != token.run_id
                    or preparation['generation'] != token.generation
                    or preparation['state'] != 'ready' or preparation['child_role'] != 'reviewer'
                    or child['workspace_binding'] != preparation['path']
                    or _from_row(preparation).input_digest != candidate_hash):
                refused()
            reviewer = dict(activity), dict(child), dict(preparation)
        return dict(state), dict(seal), [dict(row) for row in rows], reviewer

    def read(reference):
        raw, _identity = _read_checked(Path(reference['locator']), 'sealed check evidence')
        if hashlib.sha256(raw).hexdigest() != reference['sha256']:
            refused()
        store._verified_evidence(reference)
        read_references[(reference['locator'], reference['sha256'])] = reference
        return raw

    try:
        if (reviewer_activity_id is None) != (selected_artifacts is None):
            refused()
        with store.read_transaction() as tx:
            before = snapshot(tx)
            sealed = store._sealed_acceptance_from_row(before[1])
        from .run_policy import validate_draft_material
        material = validate_draft_material(sealed.material).material
        if before[0]['generation'] != sealed.legacy_generation:
            refused()
        required = {check['id'] for criterion in material['criteria'] for check in criterion['checks']}
        if {row['check_id'] for row in before[2]} != required:
            refused()
        context = {
            'schema': 'ffs.sealed-final-review-context/v1', 'acceptance_hash': acceptance_hash,
            'candidate_hash': candidate_hash, 'runtime_hash': material['runtime']['effective_hash'],
            'objective_digest': material['objective_digest'], 'criteria': material['criteria'],
            'global_invariants': material['global_invariants'], 'exclusions': material['exclusions'],
            'checks': {},
        }
        if reviewer_activity_id is not None:
            selected = artifact_review_inputs(store, _from_row(before[3][2]), selected_artifacts)
            context['selected_sources'] = selected['evidence']
            context['source_scope'] = 'exact-selected-review-inputs'
            context['source_evidence_policy'] = dict(SOURCE_EVIDENCE_POLICY)
            for reference in selected['evidence'].values():
                read(reference)
        if len(_canonical(context)) > CONTEXT_LIMIT:
            refused()
        for row in before[2]:
            evidence = json.loads(row['evidence_json'])
            if (row['status'] not in {'passed', 'failed'} or not isinstance(evidence, list)
                    or len(evidence) != 1 or set(evidence[0]) != {'locator', 'sha256'}):
                refused()
            result = json.loads(read(evidence[0]))
            if (type(result['returncode']) is not int
                    or (result['returncode'] == 0) != (row['status'] == 'passed')):
                refused()
            observed = {'returncode': result['returncode']}
            for name in ('stdout', 'stderr'):
                stream = result['streams'][name]
                reference = {key: stream[key] for key in ('locator', 'sha256')}
                raw = read(reference)
                if type(stream['bytes']) is not int or stream['bytes'] != len(raw):
                    refused()
                # The terminal descriptor already binds each stream locator;
                # expose its hash and complete text without repeating paths.
                observed[name] = {'sha256': reference['sha256'], 'contents': raw.decode('utf-8')}
            context['checks'][row['check_id']] = {
                'status': row['status'], 'evidence': evidence, 'result': observed,
            }
            if len(_canonical(context)) > CONTEXT_LIMIT:
                refused()
        # Detect replaced or rewritten files across the multi-file capture,
        # before rejoining the unchanged authority rows.
        for reference in tuple(read_references.values()):
            read(reference)
        with store.read_transaction() as tx:
            if snapshot(tx) != before:
                refused()
            for identifier, checked in context['checks'].items():
                store._frontend_check_execution_tx(tx, token, acceptance_hash=acceptance_hash,
                    candidate_hash=candidate_hash, check_id=identifier,
                    status=checked['status'], evidence=checked['evidence'])
        return context
    except (OwnershipRefused, WorkspaceRefused, NativeReviewRuntimeRefused,
            KeyError, TypeError, ValueError, OSError) as error:
        raise SupervisorRefused('FINAL_REVIEW_CONTEXT_INVALID') from error


def validate_native_review_evidence(output, context):
    """Restrict reported references to their supplied criterion/check scope.

    Rules may name a supervised terminal receipt for their own criterion.
    Findings may also cite exact selected source captures through criterion
    rule or invariant IDs. Check IDs and criterion verdicts stay check-only.
    """
    def refused():
        raise SupervisorRefused('FINAL_REVIEW_EVIDENCE_SCOPE_INVALID')

    def pool(check_ids):
        return {(item['locator'], item['sha256']) for identifier in check_ids
                for item in context['checks'][identifier]['evidence']}

    def references(items, pools):
        if not isinstance(items, list):
            refused()
        seen = set()
        for item in items:
            if (not isinstance(item, dict) or set(item) != {'id', 'locator', 'sha256'}
                    or item['id'] not in pools or item['id'] in seen
                    or (item['locator'], item['sha256']) not in pools[item['id']]):
                refused()
            seen.add(item['id'])

    try:
        sources = {(item['locator'], item['sha256'])
                   for item in context.get('selected_sources', {}).values()}
        criteria = {criterion['id']: criterion for criterion in context['criteria']}
        for identifier, checked in output['criteria'].items():
            criterion = criteria[identifier]
            permitted = pool(check['id'] for check in criterion['checks'])
            references(checked['evidence'], {rule['id']: permitted for rule in criterion['evidence_rules']})
        for finding in output['findings']:
            criterion_ids, check_ids, invariant_ids = (
                finding[key] for key in ('criterion_ids', 'check_ids', 'invariant_ids'))
            if any(not isinstance(value, list) or any(not isinstance(item, str) for item in value)
                   or len(value) != len(set(value)) for value in (criterion_ids, check_ids, invariant_ids)):
                refused()
            if (not (criterion_ids or check_ids or invariant_ids)
                    or not set(criterion_ids).issubset(criteria)
                    or not set(check_ids).issubset(context['checks'])
                    or not set(invariant_ids).issubset(item['id'] for item in context['global_invariants'])):
                refused()
            scope = set(check_ids)
            pools = {identifier: pool([identifier]) for identifier in check_ids}
            for identifier in criterion_ids:
                criterion_checks = {check['id'] for check in criteria[identifier]['checks']}
                scope.update(criterion_checks)
                for rule in criteria[identifier]['evidence_rules']:
                    permitted = pool(criterion_checks) | sources
                    # If namespaces share an ID, satisfy both meanings rather
                    # than granting the union of unrelated evidence pools.
                    pools[rule['id']] = pools.get(rule['id'], permitted) & permitted
            if not scope and invariant_ids:
                scope = set(context['checks'])
            for identifier in invariant_ids:
                permitted = pool(scope) | sources
                pools[identifier] = pools.get(identifier, permitted) & permitted
            if not finding['evidence']:
                refused()
            references(finding['evidence'], pools)
    except (KeyError, TypeError, ValueError) as error:
        raise SupervisorRefused('FINAL_REVIEW_EVIDENCE_SCOPE_INVALID') from error
