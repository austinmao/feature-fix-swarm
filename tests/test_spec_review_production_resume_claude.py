"""spec-014 E8 prerequisite 3c, review R1-2 (Claude host): what the PRODUCTION Claude path does when the owner dies.

The Codex twin (``test_spec_review_production_resume``) shows a crashed spec review resumes to
``RETAINED_RUNTIME_NOT_REUSABLE`` while the unlaunched outer's home is staged again.  Claude differs: its session
preparation stages nothing (``prepare_managed_claude_session``), the strict Codex reuse check has no Claude analogue
(a retained stage is reused while its recorded files are exact, ``claude_runtime_staging._retained_stage``) and
``qualify_managed_claude_runtime`` has no ``workspace.generation`` check.  The lifecycle driver is therefore reached,
but its first step is ``session.prepare_outer()``, which replays the outer's qualification, and the replay refuses a
new owner fence at the admission placeholder, which carries the qualifying owner's generation.

Real ``qualify_managed_claude_runtime``, real private staging and the real plan run on synthetic credentials; only the
control store and the supervisor are scripted, as in ``test_claude_qualification_crash_replay`` (whose world this
reuses).  The replay is made under a NEW fence (generation 2); that suite only ever replays under generation 1.
Fixture-level proof only: no Claude CLI runs.
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import run_state.frontend_producers as frontend_producers
from run_state import managed_claude_qualification as managed
from run_state.supervisor import SupervisorRefused
from test_claude_qualification_crash_replay import _World, _session


def _fenced(world, generation):
    world.new_process()
    world.token = SimpleNamespace(repository_id="repo", run_id="run", generation=generation)


def _facts(world):
    """Hash-bound bytes, probes run, distinct receipts (the scripted store appends one per call) and promotions."""
    return (world.snapshot(), world.launched_probes(), set(world.store.receipts), len(world.store.promotions))


def test_a_qualified_unlaunched_claude_outer_is_not_replayed_by_a_new_owner(tmp_path, monkeypatch):
    world = _World(tmp_path, monkeypatch)
    world.qualify()                                  # the first owner (generation 1): promoted, receipt committed
    qualified = _facts(world)

    # Control: another process under the SAME fence replays it as a pure replay (prerequisite 2).
    _fenced(world, 1)
    world.qualify()
    assert _facts(world) == qualified

    # A new owner (generation 2): the retained stage is still exact and the plan rehydrates, but the admission
    # descriptor on disk names the qualifying owner's generation.  Refused before any probe, promotion or receipt.
    _fenced(world, 2)
    calls = len(world.store.receipts)
    with pytest.raises(managed.ManagedClaudeQualificationRefused, match=r"^ADMISSION_CONFLICT$"):
        world.qualify()
    assert _facts(world) == qualified and len(world.store.receipts) == calls
    assert world.supervisor.outer_launches == 0


def test_the_claude_session_maps_that_refusal_to_an_unqualified_host(tmp_path, monkeypatch):
    prepare, ready, _counts = _session(tmp_path, monkeypatch, qualify=managed.ManagedClaudeQualificationRefused(
        "ADMISSION_CONFLICT"))
    session = prepare()
    with pytest.raises(SupervisorRefused) as refused:
        session.seam.qualify("33333333-3333-4333-8333-333333333333", ready, "managed-host:request", "parent",
                             "d" * 64, "worker")
    assert refused.value.code == "HOST_CAPABILITY_UNQUALIFIED"
    session.close(None, None, None)


@pytest.mark.parametrize("native", [True, False], ids=["opted-in", "key-less"])
def test_the_lifecycle_driver_refuses_at_prepare_outer_before_a_re_fence_a_seal_replay_or_an_unsealed_launch(
        monkeypatch, native):
    """The driver's first step is ``prepare_outer``: the re-fence, the seal replay and the single launch follow it."""
    reached = []

    class Transaction:
        def execute(self, *_args):
            return SimpleNamespace(fetchone=lambda: None, fetchall=lambda: [])

    class Store:
        def get_sealed_acceptance(self, **_kwargs):
            return SimpleNamespace(draft_id="assembly", draft_revision=1, material={})   # a seal an earlier owner left

        def get_frontend_policy_state(self, **_kwargs):
            return None                                                                      # ... before the lifecycle state

        @contextmanager
        def read_transaction(self):
            yield Transaction()

    def refused():
        raise SupervisorRefused("HOST_CAPABILITY_UNQUALIFIED")

    session = SimpleNamespace(
        outer_activity_id="outer", prepare_outer=refused, ready=SimpleNamespace(id="ready", input_digest="a" * 64),
        execute=lambda *_args, **_kwargs: reached.append("single launch"),
        close=lambda *_args: reached.append("closed"))
    monkeypatch.setattr(frontend_producers, "refence_unlaunched_outer",
                        lambda *_args, **_kwargs: reached.append("re-fence"))
    monkeypatch.setattr(frontend_producers, "seal_from_draft", lambda *_args, **_kwargs: reached.append("seal"))
    draft = {"criteria": [], "exclusions": [], "global_invariants": [], **({"spec_review": "native"} if native else {})}
    context = SimpleNamespace(activity_id="parent")
    with pytest.raises(SupervisorRefused) as refusal:
        frontend_producers.drive_managed_session(
            Store(), SimpleNamespace(repository_id="repo", run_id="run", generation=2), context, session,
            acceptance_draft=draft)
    assert refusal.value.code == "HOST_CAPABILITY_UNQUALIFIED" and reached == ["closed"]
