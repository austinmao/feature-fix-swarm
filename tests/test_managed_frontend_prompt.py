"""A managed frontend's outer prompt must name a real staged GSD command.

F32 (spec-014 Release C): the outer prompt used to be ``$<frontend>
<invocation_text>`` for a bare frontend word (feature-spec, feature-implement,
fix, code-uplift, task-swarm). No such skill is staged into a private host
runtime -- only ``gsd-*`` skills are (see runtime_staging.py's manifest
check) -- so the outer executor fell back to an unstaged skill and never did
plan work.

Review-gate follow-up defects fixed here:
1. planning_scope must be a plain GSD phase token (digits, optionally
   dot-segmented) -- never whitespace, a flag, or free text -- since it is
   staged as the command's only argument.
2. The operator's invocation_text must never enter the outer prompt at all
   (not even on a separate line): gsd-execute-phase treats everything after
   its invocation as GSD_ARGS, so any operator text in the prompt could
   activate a flag or forge a labelled line. It is still parsed once, only
   to catch a malformed/conflicting operation payload.
3. feature-spec/fix/code-uplift's staged mapping is undecided; they always
   refuse MANAGED_FRONTEND_COMMAND_UNSTAGED.
4. The dispatch doc/script relative paths are shared module constants, and
   are checked against the real vendored @opengsd/gsd-core layout this repo
   ships (not just compared to themselves).
"""
from __future__ import annotations

import json

import pytest

from run_state.supervisor import (
    _DISPATCH_DOC_RELATIVE, _DISPATCH_SCRIPT_RELATIVE, SupervisorRefused, _managed_prompt,
)
from test_m4_upstream_context_acceptance import ROOT


def _root(kind: str = "review") -> dict:
    return {"kind": kind}


def _operation(invocation_text: str) -> str:
    return json.dumps({"data": {"invocation_text": invocation_text}})


def test_task_swarm_prompt_names_staged_command_and_never_leaks_invocation_text(tmp_path):
    staged_runtime_home = tmp_path / "runtimes" / "outer-activity"
    staged_runtime_home.mkdir(parents=True)

    invocation, prompt, role = _managed_prompt(
        _root(), _operation("add --version flag; --interactive"), ("task-swarm",),
        staged_runtime_home=staged_runtime_home,
        planning_root=str(tmp_path / "workspace" / ".planning"),
        project="demo-project", planning_scope="03",
    )

    assert invocation == ("task-swarm",)
    assert "$task-swarm" not in prompt
    # The staged command's only argument is the real planning scope.
    assert prompt.split("\n", 1)[0] == "$gsd-execute-phase 03"
    # gsd-execute-phase treats all trailing text as GSD_ARGS: the operator's
    # invocation text must never enter the prompt anywhere, not even on a
    # separate labelled line -- it could otherwise forge a flag or a line
    # below (e.g. a fake "Planning root:"/"GSD project:").
    assert "add --version flag" not in prompt
    assert "--interactive" not in prompt
    assert "Operator request" not in prompt
    assert "invocation" not in prompt.lower()
    # Project comes from the real persisted upstream data.
    assert "GSD project: demo-project" in prompt
    assert str(staged_runtime_home / _DISPATCH_DOC_RELATIVE) in prompt
    assert str(staged_runtime_home / _DISPATCH_SCRIPT_RELATIVE) in prompt
    assert str(tmp_path / "workspace" / ".planning") in prompt


def test_feature_implement_prompt_with_no_operation_payload(tmp_path):
    invocation, prompt, role = _managed_prompt(
        _root("execute"), None, ("feature-implement",),
        staged_runtime_home=tmp_path, planning_root=str(tmp_path),
        project=None, planning_scope="1.2",
    )

    assert prompt.split("\n", 1)[0] == "$gsd-execute-phase 1.2"
    assert "GSD project: (default)" in prompt


@pytest.mark.parametrize("scope", [
    "", " ", "\t", "1 --interactive", "-1", "--gaps-only", "03\n--gaps-only", "1.a", "1.",
])
def test_scope_that_is_not_a_plain_phase_token_refuses_before_naming_a_command(tmp_path, scope):
    with pytest.raises(SupervisorRefused) as excinfo:
        _managed_prompt(
            _root(), _operation("add --version flag"), ("task-swarm",),
            staged_runtime_home=tmp_path, planning_root=str(tmp_path),
            project=None, planning_scope=scope,
        )
    assert excinfo.value.code == "PRELAUNCH_PHASE_SCOPE_REQUIRED"


@pytest.mark.parametrize("scope", ["1", "03", "3.2.1"])
def test_plain_phase_token_scopes_are_accepted(tmp_path, scope):
    invocation, prompt, role = _managed_prompt(
        _root(), _operation(""), ("task-swarm",),
        staged_runtime_home=tmp_path, planning_root=str(tmp_path),
        project=None, planning_scope=scope,
    )
    assert prompt.split("\n", 1)[0] == "$gsd-execute-phase " + scope


@pytest.mark.parametrize("frontend", ["feature-spec", "fix", "code-uplift"])
def test_deferred_frontends_always_refuse_as_unstaged(frontend, tmp_path):
    with pytest.raises(SupervisorRefused) as excinfo:
        _managed_prompt(
            _root(), _operation(""), (frontend,),
            staged_runtime_home=tmp_path, planning_root=str(tmp_path),
            project=None, planning_scope="1",
        )
    assert excinfo.value.code == "MANAGED_FRONTEND_COMMAND_UNSTAGED"


def test_unmapped_frontend_refuses_instead_of_emitting_unstaged_prompt(tmp_path, monkeypatch):
    from run_state import supervisor

    monkeypatch.setattr(supervisor, "_MANAGED_FRONTEND_STAGED_COMMAND", {}, raising=True)

    with pytest.raises(SupervisorRefused) as excinfo:
        _managed_prompt(
            _root(), _operation("03"), ("task-swarm",),
            staged_runtime_home=tmp_path, planning_root=str(tmp_path),
            project=None, planning_scope="1",
        )
    assert excinfo.value.code == "MANAGED_FRONTEND_COMMAND_UNSTAGED"


def test_malformed_operation_payload_refuses_as_context_conflict(tmp_path):
    with pytest.raises(SupervisorRefused) as excinfo:
        _managed_prompt(
            _root(), "not json", ("task-swarm",),
            staged_runtime_home=tmp_path, planning_root=str(tmp_path),
            project=None, planning_scope="1",
        )
    assert excinfo.value.code == "MANAGED_COMMAND_CONTEXT_CONFLICT"


def test_dispatch_doc_and_script_paths_match_the_real_vendored_gsd_core_layout():
    """Cheapest real-layout check: the shared constants _managed_prompt uses
    actually resolve inside the vendored @opengsd/gsd-core package this repo
    ships, not just against each other."""
    package_root = ROOT / "node_modules" / "@opengsd" / "gsd-core"
    assert (package_root / _DISPATCH_DOC_RELATIVE).is_file()
    patch_text = (ROOT / "patches" / "gsd-1.14-ffs-supervised-dispatch.patch").read_text()
    assert f"+++ b/{_DISPATCH_SCRIPT_RELATIVE.as_posix()}" in patch_text
