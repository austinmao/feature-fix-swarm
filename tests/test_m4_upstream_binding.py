"""Independent native contract for the pinned GSD planning resolver.

The external controller supplies the runtime descriptor. This file must only
run in its registered Node-capable fixture; missing admission is not a skip.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest


def _api():
    from run_state.upstream import (
        UpstreamRefused, UpstreamRuntime, resolve_upstream_binding,
    )
    return UpstreamRefused, UpstreamRuntime, resolve_upstream_binding


def _runtime():
    descriptor = os.environ.get("FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR")
    expected = os.environ.get("FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR_SHA256")
    assert descriptor and expected, "UNMET: registered Node runtime descriptor required"
    data = Path(descriptor).read_bytes()
    assert hashlib.sha256(data).hexdigest() == expected
    value = json.loads(data)
    return _api()[1].from_manifest(value), value


def _workspace(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / ".planning").mkdir(parents=True)
    return workspace


def _resolve(workspace, runtime, *, project=None, workstream=None,
             session_key="run-session", stored_workstream=None):
    return _api()[2](
        workspace, runtime=runtime, project=project, workstream=workstream,
        session_key=session_key, stored_workstream=stored_workstream,
    ).as_payload()


@pytest.mark.parametrize("project,workstream,suffix", [
    (None, None, ".planning"),
    ("project-key", None, ".planning/project-key"),
    (None, "backend", ".planning/workstreams/backend"),
    ("project-key", "backend", ".planning/project-key/workstreams/backend"),
], ids=["main", "project", "workstream", "project-workstream"])
def test_actual_upstream_computes_workspace_planning_root(tmp_path, project, workstream, suffix):
    runtime, _ = _runtime()
    workspace = _workspace(tmp_path)
    result = _resolve(workspace, runtime, project=project, workstream=workstream)
    assert result["planning_root"] == str(workspace / suffix)
    assert result["project"] == project
    assert result["workstream"] == workstream
    assert result["session_key"] == "run-session"
    assert result["effective_session_key"] == "gsd-session-key-run-session"
    assert result["resolver_version"] == "1.14.0"
    assert len(result["runtime_digest"]) == 64


def test_explicit_scope_wins_over_selected_stored_scope_without_mutating_pointer(tmp_path):
    runtime, _ = _runtime()
    workspace = _workspace(tmp_path)
    pointer = workspace / ".planning" / "active-workstream"
    pointer.write_text("unselected-ambient\n")
    before = pointer.read_bytes()
    explicit = _resolve(workspace, runtime, workstream="explicit", stored_workstream="stored")
    inherited = _resolve(workspace, runtime, stored_workstream="stored")
    assert explicit["workstream"] == "explicit"
    assert inherited["workstream"] == "stored"
    assert pointer.read_bytes() == before


def test_resolver_ignores_ambient_project_session_and_node_options(tmp_path, monkeypatch):
    runtime, _ = _runtime()
    workspace = _workspace(tmp_path)
    canary = tmp_path / "ambient-hook-fired"
    hook = tmp_path / "ambient-hook.cjs"
    hook.write_text("require('node:fs').writeFileSync(" + json.dumps(str(canary)) + ", 'fired')")
    monkeypatch.setenv("GSD_PROJECT", "ambient-project")
    monkeypatch.setenv("GSD_WORKSTREAM", "ambient-workstream")
    monkeypatch.setenv("GSD_SESSION_KEY", "ambient-session")
    monkeypatch.setenv("NODE_OPTIONS", "--require=" + str(hook))
    result = _resolve(workspace, runtime, session_key="bound-session")
    assert result["project"] is None and result["workstream"] is None
    assert result["session_key"] == "bound-session"
    assert result["effective_session_key"] == "gsd-session-key-bound-session"
    assert result["planning_root"] == str(workspace / ".planning")
    assert not canary.exists()


@pytest.mark.parametrize("field,value", [
    ("project", "../foreign"), ("project", "project/name"),
    ("workstream", "stream/name"), ("workstream", "a..b"),
    ("stored_workstream", "bad/name"),
], ids=["project-traversal", "project-slash", "workstream-slash", "workstream-dotdot", "stored-slash"])
def test_invalid_upstream_segments_refuse_without_workspace_mutation(tmp_path, field, value):
    runtime, _ = _runtime()
    workspace = _workspace(tmp_path)
    before = sorted(str(p.relative_to(workspace)) for p in workspace.rglob("*"))
    with pytest.raises(_api()[0]) as refused:
        _resolve(workspace, runtime, **{field: value})
    assert refused.value.code == "UPSTREAM_INVALID"
    assert sorted(str(p.relative_to(workspace)) for p in workspace.rglob("*")) == before


def test_planning_symlink_escape_is_refused_and_preserved(tmp_path):
    runtime, _ = _runtime()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "sentinel").write_bytes(b"unchanged")
    (workspace / ".planning").symlink_to(foreign, target_is_directory=True)
    with pytest.raises(_api()[0]) as refused:
        _resolve(workspace, runtime)
    assert refused.value.code == "UPSTREAM_ESCAPE"
    assert (foreign / "sentinel").read_bytes() == b"unchanged"
    assert (workspace / ".planning").is_symlink()


def test_runtime_module_tampering_refuses_before_resolver_execution(tmp_path):
    _, descriptor = _runtime()
    private = tmp_path / "runtime-copy"
    private.mkdir()
    copied = copy.deepcopy(descriptor)
    original = Path(descriptor["module_root"])
    for relative in descriptor["modules"]:
        target = private / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((original / relative).read_bytes())
    copied["module_root"] = str(private)
    runtime = _api()[1].from_manifest(copied)
    canary = tmp_path / "tampered-module-fired"
    target = private / "planning-workspace.cjs"
    target.write_text("require('node:fs').writeFileSync(" + json.dumps(str(canary)) + ", 'fired');\n" + target.read_text())
    with pytest.raises(_api()[0]) as refused:
        _resolve(_workspace(tmp_path), runtime)
    assert refused.value.code == "UPSTREAM_RUNTIME_DRIFT"
    assert not canary.exists()
