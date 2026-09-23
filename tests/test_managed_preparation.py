"""Real ownership coverage for the shared preparation-to-supervisor seam.

These tests exercise fixture preparation without claiming GSD runtime
qualification. Versioned runtime integration is tested with registered
upstream descriptors in the separate acceptance suite.
"""
from __future__ import annotations

import argparse
import pytest

from test_run_context_acceptance import INHERITED_CONTEXT_KEYS
from test_m4_upstream_context_acceptance import _repository
from run_state.cli import _cmd_fixture_start
from run_state.managed import _configure_managed_callback, prepare_managed_run
from run_state.ownership import (
    OwnershipRefused, ProcessIdentity, StartRequest, assert_owner, reserve_resources,
)


def _args(state_root, **overrides):
    return argparse.Namespace(**{
        "objective": "managed callback ownership", "state_root": str(state_root),
        "run_id": "adhoc-managed-callback", "activity": "plan", "scope": "",
        "resume": False, "revise": False, "request_key": "first",
        "selected_input": [], "selection_manifest": None,
        "upstream_runtime_manifest": None, "upstream_runtime_sha256": None,
        **overrides,
    })


def test_callback_owns_new_replayed_and_resumed_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(_repository(tmp_path))
    for key in INHERITED_CONTEXT_KEYS:
        monkeypatch.delenv(key, raising=False)
    retained = []

    def consumer(store, token, context):
        assert context.ready
        assert str(context.workspace) == token.workspace
        with store.transaction() as tx:
            assert_owner(tx, token)
        with pytest.raises(OwnershipRefused, match="OWNER_LIVE"):
            reserve_resources(store, StartRequest(
                token.run_id, token.workspace, token.objective_digest,
                ProcessIdentity.current(), repository_id=token.repository_id,
                planning_scope=token.planning_scope,
            ))
        retained.append((store, token))
        return 17

    args = _args(tmp_path / "authority")
    assert _cmd_fixture_start(args, on_ready=consumer) == 17
    assert _cmd_fixture_start(args, on_ready=consumer) == 17
    assert _cmd_fixture_start(
        _args(tmp_path / "authority", request_key="resume", resume=True),
        on_ready=consumer,
    ) == 17
    assert len({token.generation for _, token in retained}) == 3
    for store, token in retained:
        with store.transaction() as tx:
            with pytest.raises(OwnershipRefused, match="FENCE_REVOKED"):
                assert_owner(tx, token)


def test_callback_exception_releases_owner(tmp_path, monkeypatch):
    monkeypatch.chdir(_repository(tmp_path))
    for key in INHERITED_CONTEXT_KEYS:
        monkeypatch.delenv(key, raising=False)
    retained = []

    def failed_consumer(store, token, context):
        retained.append((store, token))
        raise RuntimeError("consumer failed")

    with pytest.raises(RuntimeError, match="consumer failed"):
        _cmd_fixture_start(_args(tmp_path / "authority"), on_ready=failed_consumer)
    store, token = retained[0]
    with store.transaction() as tx:
        with pytest.raises(OwnershipRefused, match="FENCE_REVOKED"):
            assert_owner(tx, token)


@pytest.mark.parametrize("missing", ["selection_manifest", "upstream_runtime_manifest", "upstream_runtime_sha256"])
def test_managed_ingress_requires_descriptors_before_state_writes(tmp_path, missing):
    kwargs = {
        "objective": "explicit material", "state_root": tmp_path / "authority",
        "selection_manifest": tmp_path / "selection.json",
        "upstream_runtime_manifest": tmp_path / "runtime.json",
        "upstream_runtime_sha256": "a" * 64, "request_key": "ingress",
        "dispatch_limit": 3, "token_limit": 100,
        "command": ("/gsd-plan-phase", "1"), "scope": "1",
        "on_ready": lambda *_: pytest.fail("consumer must not run"),
    }
    kwargs[missing] = None
    assert prepare_managed_run(**kwargs) == 2
    assert not (tmp_path / "authority").exists()


def test_managed_callback_configures_immutable_limits_before_consumer():
    calls = []

    class Store:
        def configure_run_limits(self, token, **limits):
            calls.append(("limits", token, limits))

        def get_activity(self, activity_id):
            assert activity_id == "activity"
            return argparse.Namespace(state="active")

    token = object()

    def consumer(received_store, received_token, context):
        calls.append(("consumer", received_store, received_token, context))
        return 17

    callback = _configure_managed_callback(
        consumer, dispatch_limit=3, token_limit=100, worker_capacity=2,
    )
    store = Store()
    context = argparse.Namespace(activity_id="activity")
    assert callback(store, token, context) == 17
    assert calls == [
        ("limits", token, {"dispatch_limit": 3, "token_limit": 100, "worker_capacity": 2}),
        ("consumer", store, token, context),
    ]


@pytest.mark.parametrize('ceiling,expected', [(None, 9), (2, 2)])
def test_default_worker_counter_does_not_impose_three_worker_ceiling(tmp_path, monkeypatch, ceiling, expected):
    from run_state import cli
    captured = []
    monkeypatch.setattr(cli, '_cmd_fixture_start', lambda args, **kwargs:
                        captured.append(args.managed_request_material) or 0)
    assert prepare_managed_run(
        objective='resource-derived concurrency', state_root=tmp_path / 'authority',
        selection_manifest=tmp_path / 'selected.json', upstream_runtime_manifest=tmp_path / 'runtime.json',
        upstream_runtime_sha256='a' * 64, request_key='resource-policy', dispatch_limit=9,
        token_limit=100, worker_capacity=ceiling, command=('/gsd-plan-phase', '1'), scope='1',
        on_ready=lambda *_: 0,
    ) == 0
    assert captured[0]['worker_capacity'] == expected
    assert captured[0]['dispatch_limit'] == 9 and captured[0]['token_limit'] == 100


def test_managed_callback_refuses_before_consumer_when_limits_are_immutable():
    called = False

    class Store:
        def configure_run_limits(self, token, **limits):
            raise OwnershipRefused("RUN_LIMITS_IMMUTABLE")

    def consumer(*_):
        nonlocal called
        called = True

    callback = _configure_managed_callback(
        consumer, dispatch_limit=3, token_limit=100, worker_capacity=2,
    )
    with pytest.raises(OwnershipRefused, match="RUN_LIMITS_IMMUTABLE"):
        callback(Store(), object(), object())
    assert not called
