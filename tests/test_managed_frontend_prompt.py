"""A managed frontend's outer prompt must name a real staged GSD command.

F32 (spec-014 Release C): the outer prompt used to be ``$<frontend>
<invocation_text>`` for a bare frontend word (feature-spec, feature-implement,
fix, code-uplift, task-swarm). No such skill is staged into the private Codex
runtime -- only ``gsd-*`` skills are (see runtime_staging.py's manifest
check) -- so the outer executor fell back to an unstaged skill and never did
plan work.

Three follow-up defects in the first fix (#F32):
1. The staged command's argument must be the already-selected planning scope
   (``frontend-start --scope``), never the operator's free-text invocation
   text -- the latter is context, not a command argument.
2. The project/planning-root source must be the real persisted upstream data
   (``context.upstream``), read directly -- never a ``getattr`` fallback that
   would silently mask a missing field.
3. Only feature-implement/task-swarm actually drive ``gsd-execute-phase``.
   feature-spec/fix/code-uplift's staged mapping is undecided; they must
   refuse as unstaged rather than guess.
"""
from __future__ import annotations

import json

import pytest

from run_state.supervisor import SupervisorRefused, _managed_prompt


def _root(kind: str = "review") -> dict:
    return {"kind": kind}


def _operation(invocation_text: str) -> str:
    return json.dumps({"data": {"invocation_text": invocation_text}})


def test_task_swarm_prompt_names_staged_command_with_real_scope_and_labelled_request(tmp_path):
    staged_codex_home = tmp_path / "runtimes" / "outer-activity"
    staged_codex_home.mkdir(parents=True)

    invocation, prompt, role = _managed_prompt(
        _root(), _operation("add --version flag"), ("task-swarm",),
        staged_codex_home=staged_codex_home,
        planning_root=str(tmp_path / "workspace" / ".planning"),
        project="demo-project", planning_scope="03",
    )

    assert invocation == ("task-swarm",)
    assert "$task-swarm" not in prompt
    command_line = prompt.split("\n", 1)[0]
    # The staged command's argument is the real planning scope, not the
    # operator's free-text invocation.
    assert command_line == "$gsd-execute-phase 03"
    assert "add --version flag" not in command_line
    # The operator's request is a clearly labelled context line instead.
    assert "Operator request: add --version flag" in prompt
    # Project comes from the real persisted upstream data.
    assert "GSD project: demo-project" in prompt
    assert str(staged_codex_home / "gsd-core" / "workflows" / "execute-phase"
               / "steps" / "executor-isolation-dispatch.md") in prompt
    assert str(staged_codex_home / "gsd-core" / "bin" / "ffs-supervised-dispatch.cjs") in prompt
    assert str(tmp_path / "workspace" / ".planning") in prompt


def test_feature_implement_prompt_without_invocation_text_omits_operator_request_line(tmp_path):
    invocation, prompt, role = _managed_prompt(
        _root("execute"), _operation(""), ("feature-implement",),
        staged_codex_home=tmp_path, planning_root=str(tmp_path),
        project=None, planning_scope="1.2",
    )

    assert prompt.split("\n", 1)[0] == "$gsd-execute-phase 1.2"
    assert "Operator request:" not in prompt
    assert "GSD project: (default)" in prompt


def test_execute_family_frontend_without_a_scope_refuses_before_naming_a_command(tmp_path):
    with pytest.raises(SupervisorRefused) as excinfo:
        _managed_prompt(
            _root(), _operation("add --version flag"), ("task-swarm",),
            staged_codex_home=tmp_path, planning_root=str(tmp_path),
            project=None, planning_scope="",
        )
    assert excinfo.value.code == "PRELAUNCH_PHASE_SCOPE_REQUIRED"


@pytest.mark.parametrize("frontend", ["feature-spec", "fix", "code-uplift"])
def test_deferred_frontends_always_refuse_as_unstaged(frontend, tmp_path):
    with pytest.raises(SupervisorRefused) as excinfo:
        _managed_prompt(
            _root(), _operation(""), (frontend,),
            staged_codex_home=tmp_path, planning_root=str(tmp_path),
            project=None, planning_scope="1",
        )
    assert excinfo.value.code == "MANAGED_FRONTEND_COMMAND_UNSTAGED"


def test_unmapped_frontend_refuses_instead_of_emitting_unstaged_prompt(tmp_path, monkeypatch):
    from run_state import supervisor

    monkeypatch.setattr(supervisor, "_MANAGED_FRONTEND_STAGED_COMMAND", {}, raising=True)

    with pytest.raises(SupervisorRefused) as excinfo:
        _managed_prompt(
            _root(), _operation("03"), ("task-swarm",),
            staged_codex_home=tmp_path, planning_root=str(tmp_path),
            project=None, planning_scope="1",
        )
    assert excinfo.value.code == "MANAGED_FRONTEND_COMMAND_UNSTAGED"
