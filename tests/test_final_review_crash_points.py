"""F51c: every durable step between FINAL_REVIEW and DONE resumes to DONE once.

Live M5 (``e2e-m5e-phase02``) died while the final reviewer's workspace was
``preparing`` (generation N-1, no reviewer activity); the same-key resume tried
to finish that dead owner's preparation and refused ``FENCE_REVOKED``.

Each point below crashes the managed Codex lifecycle (fixture host, real
ControlStore, real reviewer workspace preparation with a real Git worktree,
real CLI entry) with an uncaught ``BaseException``; the owner's fence is then
left to a dead process.  The same-key resume must reach DONE with one outer
launch, one native review and one grant each, and never finish or reuse an
earlier owner's unfinished reviewer workspace.  The review-launch points that
refuse typed are covered in ``test_final_review_resume.py`` and the real
qualification probe points in ``test_final_review_resume_sigkill.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import run_state.frontend_producers as frontend_producers
import run_state.managed_qualification as managed_qualification
import run_state.workspace as workspace
from run_state.state import ControlStore
from test_final_review_resume import _Killed, _assert_done_once, _crash, _held_resources, _start
from test_managed_lifecycle_assembly import requires_local_confinement

_REVIEWER = "final-review:reviewer"


def _arm(monkeypatch, point: str) -> list:
    """Raise ``_Killed`` once at ``point``; return the list that records the crash."""
    fired = []

    def once():
        if not fired:
            fired.append(point)
            raise _Killed()

    if point in {"final-review-entered", "settled-before-done"}:
        transition, stage = ControlStore.transition_frontend_policy, (
            "FINAL_REVIEW" if point == "final-review-entered" else "DONE")

        def transition_frontend_policy(self, token, *, expected_stage, new_stage, decision=None):
            if point == "settled-before-done" and new_stage == stage:
                once()
            result = transition(self, token, expected_stage=expected_stage, new_stage=new_stage, decision=decision)
            if point == "final-review-entered" and new_stage == stage:
                once()
            return result
        monkeypatch.setattr(ControlStore, "transition_frontend_policy", transition_frontend_policy)
    elif point == "reviewer-workspace-begun":
        prepare = frontend_producers.prepare_workspace

        def prepare_workspace(store, token, preparation, **kwargs):
            if (preparation.child_request_key or "").startswith(_REVIEWER):
                once()
            return prepare(store, token, preparation, **kwargs)
        monkeypatch.setattr(frontend_producers, "prepare_workspace", prepare_workspace)
    elif point == "reviewer-worktree-created":
        create, open_chain, armed = workspace._create_registered_worktree, workspace._open_directory_chain_raw, []

        def create_registered_worktree(store, token, preparation, admin_fd, **kwargs):
            if (preparation.child_request_key or "").startswith(_REVIEWER):
                armed.append(preparation.path)
            return create(store, token, preparation, admin_fd, **kwargs)

        def open_directory_chain_raw(root, parts, *, create):
            # After ``git worktree add``, before the native identity is recorded.
            if armed and Path(root, *parts) == armed[-1]:
                once()
            return open_chain(root, parts, create=create)
        monkeypatch.setattr(workspace, "_create_registered_worktree", create_registered_worktree)
        monkeypatch.setattr(workspace, "_open_directory_chain_raw", open_directory_chain_raw)
    elif point in {"reviewer-overlay-interrupted", "reviewer-workspace-unpublished", "reviewer-workspace-locked"}:
        name = {"reviewer-overlay-interrupted": "_apply_input_snapshot_locked",
                "reviewer-workspace-unpublished": "publish_workspace_ready",
                "reviewer-workspace-locked": "_unlock_workspace_locked"}[point]
        original, prepare, reviewers = getattr(workspace, name), frontend_producers.prepare_workspace, set()

        def prepare_workspace(store, token, preparation, **kwargs):
            if (preparation.child_request_key or "").startswith(_REVIEWER):
                reviewers.add(preparation.id)
            return prepare(store, token, preparation, **kwargs)

        def crash_for_reviewer(store, token, preparation, *args, **kwargs):
            # No store read here: the overlay runs inside the owner's fenced operation.
            if (preparation if isinstance(preparation, str) else preparation.id) in reviewers:
                once()
            return original(store, token, preparation, *args, **kwargs)
        monkeypatch.setattr(frontend_producers, "prepare_workspace", prepare_workspace)
        monkeypatch.setattr(workspace, name, crash_for_reviewer)
    elif point == "reviewer-workspace-ready":
        qualify = managed_qualification.qualify_managed_runtime

        def qualify_managed_runtime(store, token, **kwargs):
            if kwargs["role"] == "reviewer":
                once()
            return qualify(store, token, **kwargs)
        monkeypatch.setattr(managed_qualification, "qualify_managed_runtime", qualify_managed_runtime)
    elif point == "reviewer-settled-outer-active":
        settle = frontend_producers._settle_reviewers

        def settle_reviewers(store, token, *, parent_activity_id):
            settle(store, token, parent_activity_id=parent_activity_id)
            once()
        monkeypatch.setattr(frontend_producers, "_settle_reviewers", settle_reviewers)
    else:
        raise AssertionError(point)
    return fired


def _reviewer_workspaces(store) -> list[tuple]:
    with store.read_transaction() as tx:
        return [tuple(row) for row in tx.execute(
            "SELECT child_request_key,state,generation FROM context_workspaces "
            "WHERE child_request_key LIKE 'final-review:reviewer%' ORDER BY created_at,child_request_key")]


@requires_local_confinement
@pytest.mark.parametrize("point", [
    "final-review-entered",
    "reviewer-workspace-begun",        # M5e: preparing, generation N-1, no reviewer activity
    "reviewer-worktree-created",
    "reviewer-overlay-interrupted",
    "reviewer-workspace-unpublished",
    "reviewer-workspace-locked",
    "reviewer-workspace-ready",
    "reviewer-settled-outer-active",
    "settled-before-done",
])
def test_a_crash_at_each_final_review_step_resumes_to_done_once(tmp_path, monkeypatch, capsys, point):
    authority, repository_id, run = _start(tmp_path, monkeypatch)
    fired = _arm(monkeypatch, point)
    _crash(run, authority)
    assert fired == [point]
    store = ControlStore(authority / "control.sqlite3")
    crashed = _reviewer_workspaces(store)

    capsys.readouterr()
    assert run() == 0, capsys.readouterr().out[-1500:]
    facts = _assert_done_once(authority, repository_id)
    assert facts.reviews == 1 and _held_resources(tmp_path) == {}
    resumed = _reviewer_workspaces(store)
    unfinished = [row for row in crashed if row[1] != "ready"]
    # A dead owner's unfinished reviewer workspace is retained exactly as left and never finished;
    # the review ran in a fresh workspace under the next attempt key.
    assert all(row in resumed for row in unfinished)
    if unfinished:
        assert [row[0] for row in resumed] == [_REVIEWER, _REVIEWER + ":2"]
        assert resumed[-1][1] == "ready"
