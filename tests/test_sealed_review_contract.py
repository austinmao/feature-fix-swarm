"""Fixture-only prompt contracts; no model, runtime qualification or authority."""
from dataclasses import replace
import hashlib
import json

import pytest

from host_capabilities import (
    CapabilityError, build_artifact_review_material, validate_artifact_review_material,
)
from run_state.managed import build_frontend_acceptance_draft
from run_state.state import SealedAcceptance
from test_native_review_transport import _inputs, _stage


def _material(content="fixture candidate", **extra):
    return build_artifact_review_material(
        host="claude", model_request={"kind": "tier", "name": "judgment"},
        config_sha256="a" * 64, policy_sha256="b" * 64,
        environment={"HOME": "/private/runtime", "PATH": "/usr/bin:/bin", "TMPDIR": "/private/tmp"},
        selected_artifacts={"candidate.txt": hashlib.sha256(content.encode()).hexdigest()},
        selected_contents={"candidate.txt": content}, provenance={"fixture": "yes"},
        **extra,
    )


def _sealed():
    material = build_frontend_acceptance_draft(
        objective_digest="a" * 64,
        criteria=[{"id": "AC-1", "objective_clause": "fixture result is correct",
                   "checks": [{"id": "check-1", "kind": "command", "locator": "/usr/bin/true"}],
                   "evidence_rules": [{"id": "proof-1", "kind": "log", "required": True},
                                      {"id": "optional", "kind": "log", "required": False}]}],
        exclusions=[], global_invariants=[{"id": "invariant-1", "reason": "preserve fixture input"}],
        requested_runtime_hash="b" * 64, effective_runtime_hash="b" * 64,
        candidate_hash="c" * 64, generation=1, command_mode="feature-implement",
    )
    return SealedAcceptance("fixture-repo", "fixture-run", 1, "draft", 1,
                            "d" * 64, 1, "e" * 64, "f" * 64, material)


def test_default_prompt_and_replay_preserve_existing_contract_bytes():
    material = _material()
    assert material.output_contract_json is None
    expected = {
        "operation": "artifact-review",
        "artifacts": [{"name": "candidate.txt", "sha256": hashlib.sha256(b"fixture candidate").hexdigest(),
                       "encoding": "utf-8", "contents": "fixture candidate"}],
        "provenance": {"fixture": "yes"},
        "instructions": [
            "Review only the selected artifacts and their supplied provenance.",
            "Do not invoke tools, discover agents, plugins, sessions, or remote services.",
            "Return one JSON object with a verdict, findings, and evidence references.",
        ],
    }
    assert material.prompt == "Artifact-only review request:\n" + json.dumps(
        expected, sort_keys=True, separators=(",", ":"))
    assert set(material.replay_binding()) == {
        "schema", "operation", "host", "requested_model", "effective_model", "effective_effort",
        "config_sha256", "policy_sha256", "environment_sha256", "artifacts_sha256",
        "provenance_sha256", "prompt_sha256",
    }
    assert _material(output_contract=None) == material


def test_output_contract_is_canonical_immutable_and_bound():
    contract = {"fixed": {"candidate": "a" * 64}, "allowed": ["passed", "failed"]}
    material = _material(output_contract=contract)
    assert _material(output_contract=dict(reversed(list(contract.items())))) == material
    contract["fixed"]["candidate"] = "b" * 64
    assert json.loads(material.output_contract_json)["fixed"]["candidate"] == "a" * 64
    payload = json.loads(material.prompt.split("\n", 1)[1])
    assert payload["output_contract"] == json.loads(material.output_contract_json)
    assert "verdict" not in payload["instructions"][-1]
    assert material.replay_binding()["output_contract_sha256"] == hashlib.sha256(
        material.output_contract_json.encode()).hexdigest()
    assert validate_artifact_review_material(material) == material
    with pytest.raises(CapabilityError):
        validate_artifact_review_material(replace(material, output_contract_json='{"forged":true}'))
    with pytest.raises(CapabilityError):
        validate_artifact_review_material(replace(material, output_contract_json='{"x":1,"x":2}'))
    with pytest.raises(CapabilityError):
        validate_artifact_review_material(replace(material, output_contract_json=' {"x": 1} '))


def test_forged_excessive_json_nesting_has_typed_refusal():
    forged = replace(_material(), output_contract_json='{"x":' * 2000 + '0' + '}' * 2000)
    with pytest.raises(CapabilityError):
        validate_artifact_review_material(forged)


@pytest.mark.parametrize("contract", [
    {}, [], "text", {"bad": {1: "nonstring key"}}, {"bad": {1, 2}},
    {"bad": float("nan")}, {"bad": float("inf")}, {"large": "x" * 40000},
])
def test_invalid_output_contract_refuses(contract):
    with pytest.raises(CapabilityError):
        _material(output_contract=contract)


def test_recursive_and_overdeep_contract_refuse_without_recursion_error():
    recursive = {}
    recursive["self"] = recursive
    deep = {"leaf": True}
    for _ in range(40):
        deep = {"child": deep}
    for contract in (recursive, deep):
        with pytest.raises(CapabilityError):
            _material(output_contract=contract)


def test_contract_and_selected_content_share_existing_prompt_limit():
    from run_state.sealed_review import final_review_output_contract
    content = "x" * 31000
    _material(content=content)
    contract = final_review_output_contract(_sealed(), candidate_hash="1" * 64)
    with pytest.raises(CapabilityError, match="bounded input limit"):
        _material(content=content, output_contract=contract)


def test_supervisor_capture_rebuild_preserves_contract(tmp_path):
    from test_host_review_dispatch_material import _retained_selected_request, _unresolved
    from test_supervised_process import setup_owner
    supervisor, store, request = setup_owner(tmp_path)
    request, raw = _retained_selected_request(tmp_path, supervisor, store, request)
    old = _unresolved(raw)
    contract = {"fixed": {"candidate_hash": "a" * 64}}
    context = {'criteria': [{'id': 'AC-1', 'objective_clause': 'fixture semantic input'}]}
    material = build_artifact_review_material(
        host=old.host, model_request=dict(old.requested_model), config_sha256=old.config_sha256,
        policy_sha256=old.policy_sha256, environment=dict(old.environment),
        selected_artifacts=dict(old.selected_artifacts), provenance=dict(old.provenance),
        output_contract=contract,
        review_context=context,
    )
    request = replace(request, host_material=material, command=("/usr/bin/true",))
    preparation = supervisor._validate(request)
    resolved = supervisor._resolve_review_material(request, preparation).host_material
    assert resolved.output_contract_json == material.output_contract_json
    assert resolved.review_context_json == material.review_context_json
    assert resolved.provenance != material.provenance
    assert dict(resolved.selected_contents) == {"src/input.txt": raw.decode()}
    assert resolved.replay_binding()["output_contract_sha256"] == material.replay_binding()["output_contract_sha256"]
    assert resolved.replay_binding()["prompt_sha256"] != material.replay_binding()["prompt_sha256"]


def test_sealed_contract_derives_current_candidate_and_exact_review_requirements():
    from run_state.sealed_review import final_review_output_contract
    sealed = _sealed()
    contract = final_review_output_contract(sealed, candidate_hash="1" * 64)
    assert contract["fixed_fields"] == {
        "schema": "ffs.sealed-final-review/v1", "acceptance_hash": sealed.acceptance_hash,
        "candidate_hash": "1" * 64,
        "review_dimensions": ["correctness", "security", "regression"],
    }
    assert set(contract["required_fields"]) == {
        "schema", "acceptance_hash", "candidate_hash", "review_dimensions", "criteria", "findings",
    }
    assert contract["criteria"]["AC-1"]["required_evidence_ids_for_pass"] == ["proof-1"]
    assert contract["criterion_result"]["status_values"] == ["passed", "failed"]
    assert contract["findings"]["fixed_fields"]["runtime_hash"] == "b" * 64
    assert contract["findings"]["allowed_check_ids"] == ["check-1"]
    assert contract["findings"]["allowed_invariant_ids"] == ["invariant-1"]
    assert contract["evidence"]["required_fields"] == ["id", "locator", "sha256"]
    material = _material(output_contract=contract)
    assert json.loads(material.output_contract_json) == contract
    assert sealed.material["candidate_hash"] == "c" * 64


@pytest.mark.parametrize("candidate", ["", "bogus", "A" * 64, None])
def test_sealed_contract_refuses_malformed_candidate(candidate):
    from run_state.sealed_review import final_review_output_contract
    with pytest.raises(ValueError):
        final_review_output_contract(_sealed(), candidate_hash=candidate)


def test_sixty_criterion_contract_leaves_room_for_selected_review_artifacts():
    from run_state.sealed_review import final_review_output_contract
    sealed = _sealed()
    criteria = []
    for index in range(60):
        criteria.append({
            "id": f"AC-{index:03}", "objective_clause": "fixture requirement " + "x" * 200,
            "checks": [{"id": f"check-{index:03}", "kind": "command", "locator": "/" + "x" * 200}],
            "evidence_rules": [{"id": f"proof-{index:03}", "kind": "log", "required": True}],
        })
    sealed = replace(sealed, material={**sealed.material, "criteria": criteria})
    contract = final_review_output_contract(sealed, candidate_hash="1" * 64)
    material = _material(output_contract=contract)
    assert len(material.prompt.encode()) < 20 * 1024
    assert set(contract["criteria"]) == {f"AC-{index:03}" for index in range(60)}


@pytest.mark.parametrize("host", ["codex", "claude"])
def test_native_launch_keeps_sealed_contract_in_exact_prompt(tmp_path, host):
    from run_state.sealed_review import final_review_output_contract
    contract = final_review_output_contract(_sealed(), candidate_hash="1" * 64)
    launch = _stage(_inputs(tmp_path, host, output_contract=contract))
    assert launch.native.argv[-1] == launch.artifact.prompt
    assert json.loads(launch.artifact.output_contract_json) == contract
    assert launch.native.prompt_sha256 == launch.artifact.replay_binding()["prompt_sha256"]


def test_review_context_is_immutable_canonical_and_bound_without_changing_defaults():
    context = {"criteria": [{"id": "AC-1", "objective_clause": "the fixture result is correct"}]}
    material = _material(review_context=context)
    context["criteria"][0]["objective_clause"] = "forged"
    assert json.loads(material.review_context_json)["criteria"][0]["objective_clause"] != "forged"
    assert material.replay_binding()["review_context_sha256"] == hashlib.sha256(
        material.review_context_json.encode()).hexdigest()
    assert validate_artifact_review_material(material) == material
    for forged in (' {"x":1}', '{"x":1,"x":2}', '{"x":true}'):
        with pytest.raises(CapabilityError):
            validate_artifact_review_material(replace(material, review_context_json=forged))
    assert _material(review_context=None) == _material()
    assert 'review_context_sha256' not in _material().replay_binding()


@pytest.mark.parametrize('context', [{}, [], {'bad': float('nan')}, {'bad': 'x' * 65536}])
def test_invalid_review_context_refuses(context):
    with pytest.raises(CapabilityError):
        _material(review_context=context)


@pytest.mark.parametrize('character,width', [('a', 1), ('é', 6)])
def test_combined_review_context_prompt_limit_includes_json_escaping(character, width):
    # JSON escaping, envelope and selected text all count toward the byte cap.
    remaining = 65536 - len(_material(review_context={'text': ''}).prompt.encode())
    value = character * (remaining // width) + 'a' * (remaining % width)
    exact = _material(review_context={'text': value})
    assert len(exact.prompt.encode()) == 65536
    with pytest.raises(CapabilityError, match='bounded input limit'):
        _material(review_context={'text': value + 'a'})
    with pytest.raises(CapabilityError):
        _material(output_contract={'text': 'a' * 32768}, review_context={'x': 1})


def _scoped_context():
    return {
        'criteria': [{'id': 'AC-' + suffix, 'checks': [{'id': 'check-' + suffix}],
                      'evidence_rules': [{'id': 'proof-' + suffix}]} for suffix in ('1', '2')],
        'global_invariants': [{'id': 'invariant-1'}],
        'checks': {'check-' + suffix: {'evidence': [{'locator': '/fixture/check-' + suffix,
                                                   'sha256': suffix * 64}]} for suffix in ('1', '2')},
    }


def _scope_output():
    return {'criteria': {'AC-1': {'evidence': [{'id': 'proof-1', 'locator': '/fixture/check-1',
                                              'sha256': '1' * 64}]}}, 'findings': []}


@pytest.mark.parametrize('mutation', ['cross-criterion', 'unknown-rule', 'unrelated', 'duplicate-id',
                                     'empty-finding', 'foreign-finding', 'mixed-criterion-finding',
                                     'mixed-check-finding'])
def test_review_evidence_cannot_borrow_other_criterion_or_finding_references(mutation):
    from run_state.final_review_context import validate_native_review_evidence
    from run_state.supervisor import SupervisorRefused
    context, output = _scoped_context(), _scope_output()
    evidence = output['criteria']['AC-1']['evidence'][0]
    if mutation == 'cross-criterion':
        evidence.update(locator='/fixture/check-2', sha256='2' * 64)
    elif mutation == 'unknown-rule':
        evidence['id'] = 'proof-2'
    elif mutation == 'unrelated':
        evidence['locator'] = '/fixture/unrelated'
    elif mutation == 'duplicate-id':
        output['criteria']['AC-1']['evidence'].append(dict(evidence))
    else:
        finding = {'criterion_ids': [], 'check_ids': [], 'invariant_ids': [], 'evidence': [dict(evidence)]}
        if mutation == 'foreign-finding':
            finding['criterion_ids'] = ['AC-2']
        elif mutation == 'mixed-criterion-finding':
            finding['criterion_ids'] = ['AC-1', 'AC-2']
            finding['evidence'][0].update(locator='/fixture/check-2', sha256='2' * 64)
        elif mutation == 'mixed-check-finding':
            finding['check_ids'] = ['check-1', 'check-2']
            finding['evidence'][0].update(id='check-1', locator='/fixture/check-2', sha256='2' * 64)
        output['findings'] = [finding]
    with pytest.raises(SupervisorRefused, match='FINAL_REVIEW_EVIDENCE_SCOPE_INVALID'):
        validate_native_review_evidence(output, context)


def test_review_evidence_accepts_scoped_check_and_invariant_references():
    from run_state.final_review_context import validate_native_review_evidence
    context, output = _scoped_context(), _scope_output()
    for identifier, scopes in [('check-1', ([], ['check-1'], [])),
                               ('proof-1', (['AC-1'], [], [])),
                               ('invariant-1', ([], [], ['invariant-1']))]:
        output['findings'] = [{'criterion_ids': scopes[0], 'check_ids': scopes[1],
                              'invariant_ids': scopes[2], 'evidence': [
                                  {'id': identifier, 'locator': '/fixture/check-1', 'sha256': '1' * 64}]}]
        validate_native_review_evidence(output, context)


@pytest.mark.parametrize('scope', ['criterion', 'invariant', 'check', 'pass'])
def test_selected_source_evidence_supports_findings_but_not_check_proof(scope):
    from run_state.final_review_context import validate_native_review_evidence
    from run_state.supervisor import SupervisorRefused
    context, output = _scoped_context(), _scope_output()
    reference = {'locator': '/fixture/capture/src/input.txt', 'sha256': 'a' * 64}
    context['selected_sources'] = {'src/input.txt': reference}
    evidence = {'id': {'criterion': 'proof-1', 'invariant': 'invariant-1',
                       'check': 'check-1', 'pass': 'proof-1'}[scope], **reference}
    if scope == 'pass':
        output['criteria']['AC-1']['evidence'] = [evidence]
    else:
        output['findings'] = [{'criterion_ids': ['AC-1'] if scope == 'criterion' else [],
                              'check_ids': ['check-1'] if scope in {'criterion', 'check'} else [],
                              'invariant_ids': ['invariant-1'] if scope == 'invariant' else [],
                              'evidence': [evidence]}]
    if scope in {'check', 'pass'}:
        with pytest.raises(SupervisorRefused, match='FINAL_REVIEW_EVIDENCE_SCOPE_INVALID'):
            validate_native_review_evidence(output, context)
    else:
        validate_native_review_evidence(output, context)


@pytest.mark.parametrize('mutation', ['locator', 'sha256', 'colliding-check-id', 'artifact-name-id'])
def test_selected_source_findings_refuse_unbound_refs_and_check_id_collisions(mutation):
    from run_state.final_review_context import validate_native_review_evidence
    from run_state.supervisor import SupervisorRefused
    context, output = _scoped_context(), _scope_output()
    reference = {'locator': '/fixture/capture/src/input.txt', 'sha256': 'a' * 64}
    context['selected_sources'] = {'src/input.txt': dict(reference)}
    if mutation == 'colliding-check-id':
        context['criteria'][0]['evidence_rules'][0]['id'] = 'check-1'
        output['criteria']['AC-1']['evidence'][0]['id'] = 'check-1'
    elif mutation in {'locator', 'sha256'}:
        reference[mutation] = '/fixture/other' if mutation == 'locator' else 'b' * 64
    output['findings'] = [{'criterion_ids': ['AC-1'], 'check_ids': ['check-1'], 'invariant_ids': [],
                          'evidence': [{'id': 'check-1' if mutation == 'colliding-check-id' else 'proof-1',
                                        **reference}]}]
    if mutation == 'artifact-name-id':
        output['findings'][0]['evidence'][0]['id'] = 'src/input.txt'
    with pytest.raises(SupervisorRefused, match='FINAL_REVIEW_EVIDENCE_SCOPE_INVALID'):
        validate_native_review_evidence(output, context)


def test_sixty_criteria_keep_full_semantics_and_outputs_within_combined_limit():
    from run_state.final_review_context import SOURCE_EVIDENCE_POLICY
    from run_state.sealed_review import final_review_output_contract
    sealed = _sealed()
    criteria, checks = [], {}
    for index in range(60):
        check_id = f'check-{index:03}'
        criteria.append({'id': f'AC-{index:03}', 'objective_clause': 'fixture requirement ' + 'x' * 80,
                         'checks': [{'id': check_id, 'kind': 'command', 'locator': '/' + 'x' * 60}],
                         'evidence_rules': [{'id': f'proof-{index:03}', 'kind': 'log', 'required': True}]})
        reference = {'locator': '/private/fixture/current-run/evidence/' + 'x' * 100 + f'/{index:03}/result.json',
                     'sha256': 'f' * 64}
        checks[check_id] = {'status': 'passed', 'evidence': [reference],
                            'result': {'returncode': 0,
                                       'stdout': {'sha256': 'f' * 64, 'contents': 'fixture check passed'},
                                       'stderr': {'sha256': 'e' * 64, 'contents': ''}}}
    sealed = replace(sealed, material={**sealed.material, 'criteria': criteria})
    context = {'schema': 'ffs.sealed-final-review-context/v1', 'acceptance_hash': sealed.acceptance_hash,
               'candidate_hash': '1' * 64, 'runtime_hash': 'b' * 64, 'objective_digest': 'a' * 64,
               'criteria': criteria, 'checks': checks, 'global_invariants': sealed.material['global_invariants'],
               'exclusions': sealed.material['exclusions'],
               'selected_sources': {'input.txt': {'locator': '/private/fixture/capture/files/input.txt',
                                                  'sha256': 'a' * 64}},
               'source_scope': 'exact-selected-review-inputs', 'source_evidence_policy': SOURCE_EVIDENCE_POLICY}
    contract = final_review_output_contract(sealed, candidate_hash='1' * 64)
    material = _material(output_contract=contract, review_context=context)
    assert len(material.output_contract_json.encode()) <= 32768
    assert len(material.prompt.encode()) <= 65536
    payload = json.loads(material.prompt.split('\n', 1)[1])
    assert payload['review_context'] == context  # full text, no omitted or truncated clauses
    context['checks']['check-000']['result']['stdout']['contents'] = 'x' * 65536
    with pytest.raises(CapabilityError):
        _material(output_contract=contract, review_context=context)
