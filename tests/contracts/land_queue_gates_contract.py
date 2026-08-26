"""Direct-only Wave-0 contract for degradation binding and prod promotion.

This filename intentionally avoids pytest's default test_*.py / *_test.py rules.
Its evidence is always the explicitly named direct path, never a vacuous broad
collection run.

REQ-209 / waiver 5d794fab: gates.py computes the changed-file set ITSELF from
`git diff --name-only BASE...HEAD` (BASE = merge-base with the recorded
baseline commit); caller-supplied file lists are advisory widen-only.  The
production authority (check_grant_prod) refuses when degraded reviews exceed
50% of this run OR any degraded event for this run is production-touching;
legacy degraded events without binding fields are conservatively
production-touching.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATES_PATH = ROOT / "lib" / "gates.py"


def gates_module():
    if not GATES_PATH.is_file():
        pytest.fail("RED-EXPECTED: REQ-209 lib/gates.py target is absent")
    spec = importlib.util.spec_from_file_location("land_queue_real_gates", GATES_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        pytest.fail(f"RED-EXPECTED: REQ-209 gates load failed: {type(exc).__name__}")
    return module


@pytest.fixture
def reviewed_git(tmp_path: Path) -> tuple[Path, str, str, str]:
    """Real local repo: baseline, a production head, and a tests-only head."""
    repo = tmp_path / "repo"; repo.mkdir()
    def run(*args): return subprocess.run(["git", *args], cwd=repo, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
    run("init", "-b", "main"); run("config", "user.email", "wave0@example.invalid"); run("config", "user.name", "Wave 0")
    (repo / "README.md").write_text("base\n"); run("add", "README.md"); run("commit", "-m", "base"); base = run("rev-parse", "HEAD")
    run("checkout", "-b", "reviewed"); (repo / "lib.py").write_text("production\n"); run("add", "lib.py"); run("commit", "-m", "production")
    head_prod = run("rev-parse", "HEAD")
    run("checkout", "-b", "reviewed-tests", base)
    (repo / "tests").mkdir(); (repo / "tests" / "unit_test.py").write_text("assert True\n")
    run("add", "tests/unit_test.py"); run("commit", "-m", "tests-only")
    head_tests = run("rev-parse", "HEAD")
    return repo, base, head_prod, head_tests


def _note(gates, store, run_id, inv, degraded, repo=None, baseline=None, head=None,
          branch="reviewed", **extra):
    kwargs = dict(run_id=run_id, seam="review", degraded=degraded, invocation_id=inv)
    if repo is not None:
        kwargs.update(repo=str(repo), baseline=baseline, head=head, branch=branch)
    kwargs.update(extra)
    return gates.note_degraded(store, "invocation", **kwargs)


def _events(store, run_id):
    data = json.loads(Path(store).read_text())
    return [e for e in data["_degradation"]["invocations"] if e.get("run_id") == run_id]


def _prod_gate(gates, store, run_id):
    """(passed_degradation_gates, sink). check_grant_prod with no artifact
    refuses with a recorded NO-PROMOTE-EVIDENCE reason ONLY after the
    degradation gates pass; a degradation refusal returns False silently."""
    sink: list[str] = []
    ok = gates.check_grant_prod(store, run_id, "deploy:prod-web", None,
                                reason_sink=sink)
    assert ok is False
    return bool(sink), sink


def test_computed_three_dot_files_override_caller_omissions(reviewed_git, tmp_path):
    repo, base, head_prod, _ = reviewed_git
    gates = gates_module()
    store = tmp_path / "evidence.json"
    _note(gates, store, "run-1", "inv-1", True, repo=repo, baseline=base,
          head=head_prod, changed_files=["docs/extra.md"])
    (event,) = _events(store, "run-1")
    # Git authority computed lib.py even though the caller omitted it; the
    # caller's advisory file widened (never shrank) the recorded set.
    assert "lib.py" in event["changed_files"], event
    assert "docs/extra.md" in event["changed_files"], event
    assert event["production_files"] == ["lib.py"], event
    assert event["production_touch"] is True, event
    assert event["branch"] == "reviewed" and event["head"] == head_prod
    assert event["baseline"] == base


def test_below_equal_above_50_percent_ratio(reviewed_git, tmp_path):
    repo, base, _, head_tests = reviewed_git
    gates = gates_module()
    # Non-production-touching degraded events isolate the pure ratio rule.
    store = tmp_path / "e1.json"
    _note(gates, store, "run-2", "d1", True, repo=repo, baseline=base,
          head=head_tests, branch="reviewed-tests")
    _note(gates, store, "run-2", "o1", False, repo=repo, baseline=base,
          head=head_tests, branch="reviewed-tests")
    _note(gates, store, "run-2", "o2", False, repo=repo, baseline=base,
          head=head_tests, branch="reviewed-tests")
    passed, _ = _prod_gate(gates, store, "run-2")  # 1/3 below
    assert passed
    _note(gates, store, "run-2", "d2", True, repo=repo, baseline=base,
          head=head_tests, branch="reviewed-tests")
    passed, _ = _prod_gate(gates, store, "run-2")  # 2/4 equal
    assert passed
    _note(gates, store, "run-2", "d3", True, repo=repo, baseline=base,
          head=head_tests, branch="reviewed-tests")
    passed, sink = _prod_gate(gates, store, "run-2")  # 3/5 above
    assert not passed and sink == []


def test_degraded_production_touch_always_refuses(reviewed_git, tmp_path):
    repo, base, head_prod, head_tests = reviewed_git
    gates = gates_module()
    store = tmp_path / "e2.json"
    _note(gates, store, "run-3", "d1", True, repo=repo, baseline=base,
          head=head_prod)
    for i in range(3):
        _note(gates, store, "run-3", f"o{i}", False, repo=repo, baseline=base,
              head=head_tests, branch="reviewed-tests")
    # Ratio is 25% — fine — but ONE degraded production touch refuses.
    passed, sink = _prod_gate(gates, store, "run-3")
    assert not passed and sink == []


def test_non_production_event_uses_aggregate_only(reviewed_git, tmp_path):
    repo, base, head_prod, _ = reviewed_git
    gates = gates_module()
    store = tmp_path / "e3.json"
    _note(gates, store, "run-4", "d1", True, repo=repo, baseline=base,
          head=head_prod)
    # A non-prod action is untouched by the production-touch rule: only the
    # ordinary exact grant decides it.
    assert gates.grant_actions(store, "run-4", ["merge:pr-5"], reason="t")
    assert gates.check_grant_prod(store, "run-4", "merge:pr-5", None) is True


def test_malformed_binding_fails_closed(reviewed_git, tmp_path):
    repo, base, head_prod, _ = reviewed_git
    gates = gates_module()
    store = tmp_path / "e4.json"
    with pytest.raises(ValueError):  # inconsistent head shape
        _note(gates, store, "run-5", "b1", True, repo=repo, baseline=base,
              head=head_prod[:12])
    with pytest.raises(ValueError):  # binding without its repo authority
        gates.note_degraded(store, "invocation", run_id="run-5", seam="review",
                            degraded=True, invocation_id="b2",
                            branch="reviewed", head=head_prod, baseline=base)
    with pytest.raises(ValueError):  # advisory files without any binding
        gates.note_degraded(store, "invocation", run_id="run-5", seam="review",
                            degraded=True, invocation_id="b3",
                            changed_files=["lib.py"])
    with pytest.raises(ValueError):  # unknown baseline commit
        _note(gates, store, "run-5", "b4", True, repo=repo,
              baseline="f" * 40, head=head_prod)
    assert _events(store, "run-5") == []


def test_legacy_event_conservatively_touches_production(reviewed_git, tmp_path):
    repo, base, _, head_tests = reviewed_git
    gates = gates_module()
    store = tmp_path / "e5.json"
    # Legacy shape: no binding fields at all (pre-02-03 writer).
    gates.note_degraded(store, "invocation", run_id="run-6", seam="review",
                        degraded=True, invocation_id="legacy-1")
    for i in range(2):
        gates.note_degraded(store, "invocation", run_id="run-6", seam="review",
                            degraded=False, invocation_id=f"legacy-ok-{i}")
    # Ratio passes (1/3) yet the legacy degraded event counts as
    # production-touching, so production promotion refuses.
    passed, sink = _prod_gate(gates, store, "run-6")
    assert not passed and sink == []
    # The legacy event stays readable by the existing aggregate reader.
    assert gates.degraded_ratio(store, "run-6") == (1, 3)


def test_different_run_events_are_irrelevant(reviewed_git, tmp_path):
    repo, base, head_prod, _ = reviewed_git
    gates = gates_module()
    store = tmp_path / "e6.json"
    _note(gates, store, "run-7", "d1", True, repo=repo, baseline=base,
          head=head_prod)
    # run-8 has no degradation events: its production gate is untouched by
    # run-7's production-touching degradation.
    passed, sink = _prod_gate(gates, store, "run-8")
    assert passed and sink
