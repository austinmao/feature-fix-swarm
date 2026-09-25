"""A managed frontend's outer prompt must name a real staged GSD command.

F32 (spec-014 Release C): the outer prompt used to be ``$<frontend>
<invocation_text>`` for a bare frontend word (feature-spec, feature-implement,
fix, code-uplift, task-swarm). No such skill is staged into the private Codex
runtime -- only ``gsd-*`` skills are (see runtime_staging.py's manifest
check) -- so the outer executor fell back to an unstaged skill and never did
plan work. The managed prompt must instead name the staged ``gsd-*`` command
the frontend's managed lifecycle actually drives, plus the absolute staged
adapter paths and the absolute planning root the outer executor needs to find
its scope.
"""
from __future__ import annotations

import json

import pytest

from run_state.supervisor import SupervisorRefused, _managed_prompt


def _root(kind: str = "review") -> dict:
    return {"kind": kind}


def _operation(invocation_text: str) -> str:
    return json.dumps({"data": {"invocation_text": invocation_text}})


def test_task_swarm_prompt_names_staged_command_and_absolute_paths(tmp_path):
    staged_codex_home = tmp_path / "runtimes" / "outer-activity"
    staged_codex_home.mkdir(parents=True)

    invocation, prompt, role = _managed_prompt(
        _root(), _operation("03"), ("task-swarm",),
        staged_codex_home=staged_codex_home,
        planning_root=str(tmp_path / "workspace" / ".planning"),
        project=None,
    )

    assert invocation == ("task-swarm",)
    assert "$task-swarm" not in prompt
    assert "$gsd-execute-phase 03" in prompt
    assert str(staged_codex_home / "gsd-core" / "workflows" / "execute-phase"
               / "steps" / "executor-isolation-dispatch.md") in prompt
    assert str(staged_codex_home / "gsd-core" / "bin" / "ffs-supervised-dispatch.cjs") in prompt
    assert str(tmp_path / "workspace" / ".planning") in prompt


def test_unmapped_frontend_refuses_instead_of_emitting_unstaged_prompt(tmp_path, monkeypatch):
    from run_state import supervisor

    monkeypatch.setattr(supervisor, "_MANAGED_FRONTEND_STAGED_COMMAND", {}, raising=True)

    with pytest.raises(SupervisorRefused) as excinfo:
        _managed_prompt(
            _root(), _operation("03"), ("task-swarm",),
            staged_codex_home=tmp_path, planning_root=str(tmp_path), project=None,
        )
    assert excinfo.value.code == "MANAGED_FRONTEND_COMMAND_UNSTAGED"
