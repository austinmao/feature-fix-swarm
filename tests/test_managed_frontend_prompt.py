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

Review round 3 follow-up defects fixed here:
5. A mode flag (--dry-run, --adhoc) in invocation_text changes what actually
   runs in a way gsd-execute-phase cannot honor, so it refuses
   MANAGED_FRONTEND_MODE_UNSUPPORTED instead of silently running for real
   (or running gsd-execute-phase instead of gsd-quick).
6. F34: a non-default project or workstream now reaches the qualified host
   process env as GSD_PROJECT/GSD_WORKSTREAM (the closed GSD env addition
   set carries them when set), so it is accepted instead of refusing
   MANAGED_PROJECT_SCOPE_UNSUPPORTED. Each value is still validated against
   the resolver's segment rule, raising MANAGED_PROMPT_VALUE_UNSAFE for an
   unsafe one.
7. A raw prompt value (planning_root/project) carrying a control character
   refuses MANAGED_PROMPT_VALUE_UNSAFE.
8. is_valid_phase_scope is ASCII-only (covered in test_prelaunch_inventory.py).
"""
from __future__ import annotations

import json

import pytest

from run_state.supervisor import (
    _DISPATCH_DOC_RELATIVE, _DISPATCH_SCRIPT_RELATIVE, SupervisorRefused,
    _managed_gsd_prompt, _managed_prompt,
)
from test_m4_upstream_context_acceptance import ROOT


def _root(kind: str = "review") -> dict:
    return {"kind": kind}


def _operation(invocation_text) -> str:
    return json.dumps({"data": {"invocation_text": invocation_text}})


def test_task_swarm_prompt_names_staged_command_and_never_leaks_invocation_text(tmp_path):
    staged_runtime_home = tmp_path / "runtimes" / "outer-activity"
    staged_runtime_home.mkdir(parents=True)

    invocation, prompt, role = _managed_prompt(
        _root(), _operation("add --version flag; --interactive"), ("task-swarm",),
        staged_runtime_home=staged_runtime_home,
        planning_root=str(tmp_path / "workspace" / ".planning"),
        project=None, workstream=None, planning_scope="03",
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
    assert "GSD project: (default)" in prompt
    assert str(staged_runtime_home / _DISPATCH_DOC_RELATIVE) in prompt
    assert str(staged_runtime_home / _DISPATCH_SCRIPT_RELATIVE) in prompt
    assert str(tmp_path / "workspace" / ".planning") in prompt


def test_feature_implement_prompt_with_no_operation_payload(tmp_path):
    invocation, prompt, role = _managed_prompt(
        _root("execute"), None, ("feature-implement",),
        staged_runtime_home=tmp_path, planning_root=str(tmp_path),
        project=None, workstream=None, planning_scope="1.2",
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
            project=None, workstream=None, planning_scope=scope,
        )
    assert excinfo.value.code == "PRELAUNCH_PHASE_SCOPE_REQUIRED"


@pytest.mark.parametrize("scope", ["1", "03", "3.2.1"])
def test_plain_phase_token_scopes_are_accepted(tmp_path, scope):
    invocation, prompt, role = _managed_prompt(
        _root(), _operation(""), ("task-swarm",),
        staged_runtime_home=tmp_path, planning_root=str(tmp_path),
        project=None, workstream=None, planning_scope=scope,
    )
    assert prompt.split("\n", 1)[0] == "$gsd-execute-phase " + scope


@pytest.mark.parametrize("frontend", ["feature-spec", "fix", "code-uplift"])
def test_deferred_frontends_always_refuse_as_unstaged(frontend, tmp_path):
    with pytest.raises(SupervisorRefused) as excinfo:
        _managed_prompt(
            _root(), _operation(""), (frontend,),
            staged_runtime_home=tmp_path, planning_root=str(tmp_path),
            project=None, workstream=None, planning_scope="1",
        )
    assert excinfo.value.code == "MANAGED_FRONTEND_COMMAND_UNSTAGED"


def test_unmapped_frontend_refuses_instead_of_emitting_unstaged_prompt(tmp_path, monkeypatch):
    from run_state import supervisor

    monkeypatch.setattr(supervisor, "_MANAGED_FRONTEND_STAGED_COMMAND", {}, raising=True)

    with pytest.raises(SupervisorRefused) as excinfo:
        _managed_prompt(
            _root(), _operation("03"), ("task-swarm",),
            staged_runtime_home=tmp_path, planning_root=str(tmp_path),
            project=None, workstream=None, planning_scope="1",
        )
    assert excinfo.value.code == "MANAGED_FRONTEND_COMMAND_UNSTAGED"


def test_malformed_operation_payload_refuses_as_context_conflict(tmp_path):
    with pytest.raises(SupervisorRefused) as excinfo:
        _managed_prompt(
            _root(), "not json", ("task-swarm",),
            staged_runtime_home=tmp_path, planning_root=str(tmp_path),
            project=None, workstream=None, planning_scope="1",
        )
    assert excinfo.value.code == "MANAGED_COMMAND_CONTEXT_CONFLICT"


@pytest.mark.parametrize("invocation_text", [None, ["--gaps-only"], 4])
def test_invocation_text_that_is_not_a_string_refuses_as_context_conflict(tmp_path, invocation_text):
    with pytest.raises(SupervisorRefused) as excinfo:
        _managed_prompt(
            _root(), _operation(invocation_text), ("task-swarm",),
            staged_runtime_home=tmp_path, planning_root=str(tmp_path),
            project=None, workstream=None, planning_scope="1",
        )
    assert excinfo.value.code == "MANAGED_COMMAND_CONTEXT_CONFLICT"


@pytest.mark.parametrize("invocation_text", ["--dry-run", "1.2 --dry-run", "--adhoc", '--adhoc "add x"'])
def test_invocation_text_naming_an_unsupported_mode_refuses(tmp_path, invocation_text):
    with pytest.raises(SupervisorRefused) as excinfo:
        _managed_prompt(
            _root(), _operation(invocation_text), ("task-swarm",),
            staged_runtime_home=tmp_path, planning_root=str(tmp_path),
            project=None, workstream=None, planning_scope="1",
        )
    assert excinfo.value.code == "MANAGED_FRONTEND_MODE_UNSUPPORTED"


def test_invocation_text_mentioning_dry_run_as_free_text_still_passes(tmp_path):
    # Whole-token match only: "dry-run" inside prose is not the flag "--dry-run".
    invocation, prompt, role = _managed_prompt(
        _root(), _operation("please do a dry-run of this"), ("task-swarm",),
        staged_runtime_home=tmp_path, planning_root=str(tmp_path),
        project=None, workstream=None, planning_scope="1",
    )
    assert prompt.split("\n", 1)[0] == "$gsd-execute-phase 1"


def test_a_quoted_task_naming_a_mode_flag_stays_one_token_and_passes(tmp_path):
    # shlex.split keeps a quoted phrase as one token, so --dry-run inside it
    # never matches the mode-flag set.
    invocation, prompt, role = _managed_prompt(
        _root(), _operation('"add --dry-run support"'), ("task-swarm",),
        staged_runtime_home=tmp_path, planning_root=str(tmp_path),
        project=None, workstream=None, planning_scope="1",
    )
    assert prompt.split("\n", 1)[0] == "$gsd-execute-phase 1"


def test_unbalanced_quotes_fall_back_to_whitespace_split_and_still_refuse(tmp_path):
    # shlex.split raises ValueError on unbalanced quotes; the fallback plain
    # split still finds the bare --dry-run token and fails closed.
    with pytest.raises(SupervisorRefused) as excinfo:
        _managed_prompt(
            _root(), _operation('"add --dry-run'), ("task-swarm",),
            staged_runtime_home=tmp_path, planning_root=str(tmp_path),
            project=None, workstream=None, planning_scope="1",
        )
    assert excinfo.value.code == "MANAGED_FRONTEND_MODE_UNSUPPORTED"


@pytest.mark.parametrize(("project", "workstream"), [
    ("demo-project", None), (None, "demo-workstream"), ("demo-project", "demo-workstream"),
])
def test_non_default_project_or_workstream_is_accepted_for_a_staged_frontend(tmp_path, project, workstream):
    # F34: the closed GSD env addition set now carries GSD_PROJECT/
    # GSD_WORKSTREAM, so a non-default scope reaches the host and is no
    # longer refused as MANAGED_PROJECT_SCOPE_UNSUPPORTED.
    invocation, prompt, role = _managed_prompt(
        _root(), _operation(""), ("task-swarm",),
        staged_runtime_home=tmp_path, planning_root=str(tmp_path),
        project=project, workstream=workstream, planning_scope="1",
    )
    assert prompt.split("\n", 1)[0] == "$gsd-execute-phase 1"
    # Review round 1 item 17: the scope must reach wherever _managed_prompt
    # itself carries it -- the "GSD project: X" prompt line. workstream is
    # not embedded in prompt text at all (only in the env additions a higher
    # layer builds); that path is proven end-to-end by
    # test_managed_codex_dispatch.py's parametrized 5.10 and
    # test_managed_claude_wave.py's scoped-outer-qualify test.
    expected_project_line = "GSD project: " + (project if project is not None else "(default)")
    assert expected_project_line in prompt
    assert invocation == ("task-swarm",)


def test_non_default_project_is_accepted_for_a_raw_gsd_command_too(tmp_path):
    # Applied at the shared function for every managed prompt: a non-default
    # scope is accepted for ANY managed prompt, not only a staged frontend.
    invocation, prompt, role = _managed_prompt(
        _root(), None, ("/gsd-plan-phase", "1"),
        staged_runtime_home=tmp_path, planning_root=str(tmp_path),
        project="demo-project", workstream=None, planning_scope="1",
    )
    assert invocation == ("/gsd-plan-phase", "1")
    assert "GSD project: demo-project" in prompt


@pytest.mark.parametrize("field", ["project", "workstream"])
@pytest.mark.parametrize("bad_value", [
    "../x", "a/b", "a..b", "-f", ".h", "x y", "é", "9" * 161, "",
])
def test_unsafe_project_or_workstream_segment_refuses_value_unsafe(tmp_path, field, bad_value):
    kwargs = {"project": None, "workstream": None, field: bad_value}
    with pytest.raises(SupervisorRefused) as excinfo:
        _managed_prompt(
            _root(), _operation(""), ("task-swarm",),
            staged_runtime_home=tmp_path, planning_root=str(tmp_path),
            planning_scope="1", **kwargs,
        )
    assert excinfo.value.code == "MANAGED_PROMPT_VALUE_UNSAFE"


@pytest.mark.parametrize("bad_planning_root", [
    "/tmp/x\x07", "/tmp/x\x7f", "/tmp/x\x00y", "/tmp/x\x1f",
    "/tmp/x\u0085y",  # NEL (C1, Cc)
    "/tmp/x y",  # LINE SEPARATOR (Zl)
    "/tmp/x y",  # PARAGRAPH SEPARATOR (Zp)
])
def test_planning_root_with_a_control_character_refuses(tmp_path, bad_planning_root):
    with pytest.raises(SupervisorRefused) as excinfo:
        _managed_prompt(
            _root(), None, ("/gsd-plan-phase", "1"),
            staged_runtime_home=tmp_path, planning_root=bad_planning_root,
            project=None, workstream=None, planning_scope="1",
        )
    assert excinfo.value.code == "MANAGED_PROMPT_VALUE_UNSAFE"


def test_managed_gsd_prompt_itself_refuses_a_control_character_in_project(tmp_path):
    with pytest.raises(SupervisorRefused) as excinfo:
        _managed_gsd_prompt(
            "$gsd-plan-phase 1", dispatch_doc=tmp_path / "doc.md", dispatch_script=tmp_path / "script.cjs",
            planning_root=str(tmp_path), project="demo\x07project",
        )
    assert excinfo.value.code == "MANAGED_PROMPT_VALUE_UNSAFE"


def test_dispatch_doc_and_script_paths_match_the_real_vendored_gsd_core_layout():
    """Cheapest real-layout check: the shared constants _managed_prompt uses
    actually resolve inside the vendored @opengsd/gsd-core package this repo
    ships, not just against each other."""
    package_root = ROOT / "node_modules" / "@opengsd" / "gsd-core"
    assert (package_root / _DISPATCH_DOC_RELATIVE).is_file()
    patch_text = (ROOT / "patches" / "gsd-1.14-ffs-supervised-dispatch.patch").read_text()
    assert f"+++ b/{_DISPATCH_SCRIPT_RELATIVE.as_posix()}" in patch_text


def test_each_staged_frontend_command_is_a_real_gsd_skill_that_passes_the_staging_filter():
    from run_state.runtime_staging import _GSD_SKILL
    from run_state.supervisor import _MANAGED_FRONTEND_STAGED_COMMAND

    skills_root = ROOT / "node_modules" / "@opengsd" / "gsd-core" / "skills"
    for value in _MANAGED_FRONTEND_STAGED_COMMAND.values():
        assert _GSD_SKILL.fullmatch(value) is not None
        assert (skills_root / value).is_dir()
        assert (skills_root / value / "SKILL.md").is_file()
