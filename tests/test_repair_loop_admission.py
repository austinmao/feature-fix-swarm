"""Public repair-budget entrypoints with disposable Git and inert reviewer CLIs.

The reviewer fixtures prove invocation admission only, never native host behavior.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1]
RUN = "spec-777"


def _run(c, argv, *, text="", timeout=30):
    return subprocess.run([*map(str, argv)], cwd=c.repo,
                          env=c.env, input=text, text=True, capture_output=True,
                          timeout=timeout)


def _gate(c, *args):
    return _run(c, [sys.executable, c.repo / "lib/gates.py", *args])


def _diff(c, text):
    return _run(c, ["bash", c.repo / "scripts/gsd/review-gate-command.sh"], text=text)


def _wall(c, phase):
    return _run(c, ["bash", c.repo / "scripts/gsd/plan-wall.sh", phase])


def _markers(c):
    return c.marker.read_text().splitlines() if c.marker.exists() else []


def _delta(value):
    return "diff --git a/widget.py b/widget.py\n--- a/widget.py\n+++ b/widget.py\n@@ -1 +1 @@\n-old\n+" + value + "\n"


@pytest.fixture
def case(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ["scripts", "schemas"]:
        shutil.copytree(SOURCE / name, repo / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (repo / "lib").mkdir()
    for name in ["gates.py", "model_requests.py"]:
        shutil.copyfile(SOURCE / "lib" / name, repo / "lib" / name)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
           "TZ": "UTC", "PYTHONDONTWRITEBYTECODE": "1", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "FFS_HOST": "claude", "GSD_RUN_ID": RUN,
           "FFS_ADVERSARY_MODEL_PROBE": "off", "PLAN_WALL_TIMEOUT": "10",
           "GSD_REVIEW_TIMEOUT": "10", "PLAN_WALL_MAX_ROUNDS": "2"}
    for key in ["HOME", "TMPDIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"]:
        path = private / key.lower()
        path.mkdir(mode=0o700)
        env[key] = str(path)
    env["GATES_STORE"] = str(private / "gates.json")
    bindir = private / "bin"
    bindir.mkdir()
    marker = private / "reviewer-invocations.jsonl"
    mode = private / "reviewer-mode"
    mode.write_text("pass")
    code = "#!" + sys.executable + "\n" + """import json,os,sys
from pathlib import Path
prompt=sys.stdin.read()
with Path(os.environ['FIXTURE_MARKER']).open('a') as stream:
    stream.write(json.dumps({'argv':sys.argv[1:],'prompt':prompt})+'\\n')
mode=Path(os.environ['FIXTURE_MODE']).read_text()
if '--output-schema' in sys.argv or Path(sys.argv[0]).name=='fake-claude':
    findings=[] if mode!='critical' else [{'severity':'CRITICAL','file':'widget.py','line':1,'claim':'actual fixture critical finding'}]
    print(json.dumps({'findings':findings}))
else:
    print('VERDICT: BLOCK' if mode=='block' else 'VERDICT: PASS')
"""
    for name in ["fake-codex", "fake-claude"]:
        cli = bindir / name
        cli.write_text(code)
        cli.chmod(0o755)
    env.update(ADVERSARY_BIN_CODEX=str(bindir / "fake-codex"),
               ADVERSARY_BIN_CLAUDE=str(bindir / "fake-claude"),
               FIXTURE_MARKER=str(marker), FIXTURE_MODE=str(mode))
    c = SimpleNamespace(repo=repo, env=env, marker=marker, mode=mode,
                        store=Path(env["GATES_STORE"]))
    assert _run(c, ["git", "init", "-q", "-b", "777-fixture"]).returncode == 0
    assert _run(c, ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@invalid", "commit", "-q", "--allow-empty", "-m", "fixture initialization"]).returncode == 0
    granted = _gate(c, "grant", RUN, "--action", "ship:gsd", "--reason", "disposable review-admission fixture")
    assert granted.returncode == 0, granted.stderr
    yield c


def test_changed_diff_after_two_passes_is_refused_before_reviewer(case):
    for value in ["first", "second"]:
        result = _diff(case, _delta(value))
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["verdict"] == "APPROVED"
    assert len(_markers(case)) == 2
    rejected = _diff(case, _delta("third-new-review-target"))
    assert rejected.returncode != 0
    assert json.loads(rejected.stdout)["verdict"] == "REVISE"
    assert len(_markers(case)) == 2, "third changed diff must not invoke any reviewer"


def test_failed_review_is_charged_across_fresh_processes(case):
    case.mode.write_text("block")
    for value in ["failure-A", "failure-B"]:
        result = _diff(case, _delta(value))
        assert result.returncode != 0
        assert json.loads(result.stdout)["verdict"] == "REVISE"
    assert len(_markers(case)) == 2
    case.mode.write_text("pass")
    rejected = _diff(case, _delta("fresh-target-after-failure"))
    assert rejected.returncode != 0
    assert len(_markers(case)) == 2


def test_empty_diff_does_not_consume_review_allowance(case):
    for _ in range(3):
        result = _diff(case, "")
        assert result.returncode == 0 and json.loads(result.stdout)["verdict"] == "APPROVED"
    assert _markers(case) == []
    assert _diff(case, _delta("one")).returncode == 0
    assert _diff(case, _delta("two")).returncode == 0
    assert len(_markers(case)) == 2
    assert _diff(case, _delta("three")).returncode != 0
    assert len(_markers(case)) == 2


@pytest.mark.parametrize("corruption", ["invalid-json", "loops-array", "cap-used-boolean"])
def test_malformed_budget_store_refuses_before_reviewer(case, corruption):
    if corruption == "cap-used-boolean":
        seeded = _gate(case, "loop-round", RUN, "review:diff", "--max", "2", "--durable")
        assert seeded.returncode == 0, seeded.stderr
        data = json.loads(case.store.read_text())
        data["_loops"][RUN]["review:diff#cap"]["used"] = True
        case.store.write_text(json.dumps(data))
    elif corruption == "loops-array":
        data = json.loads(case.store.read_text())
        data["_loops"] = []
        case.store.write_text(json.dumps(data))
    else:
        case.store.write_text("{")
    before = case.store.read_bytes()
    result = _diff(case, _delta("should-never-be-reviewed"))
    assert result.returncode != 0
    assert json.loads(result.stdout)["verdict"] == "REVISE"
    assert _markers(case) == []
    assert case.store.read_bytes() == before


def test_durable_cli_ceiling_cannot_be_raised_after_first_use(case):
    assert _gate(case, "loop-round", RUN, "review:diff", "--max", "2", "--durable").returncode == 0
    raised = _gate(case, "loop-round", RUN, "review:diff", "--max", "3", "--durable")
    assert raised.returncode != 0
    assert _gate(case, "loop-round", RUN, "review:diff", "--max", "2", "--durable").returncode == 0
    assert _gate(case, "loop-round", RUN, "review:diff", "--max", "2", "--durable").returncode != 0


@pytest.mark.parametrize("reset", ["named", "all"])
def test_generic_cli_reset_cannot_replenish_exhausted_durable_allowance(case, reset):
    for _ in range(2):
        assert _gate(case, "loop-round", RUN, "review:diff", "--max", "2", "--durable").returncode == 0
    args = ["loop-round", RUN, "review:diff", "--reset"] if reset == "named" else ["loop-round", RUN, "--reset-all"]
    _gate(case, *args)
    assert _gate(case, "loop-round", RUN, "review:diff", "--max", "2", "--durable").returncode != 0
    assert _markers(case) == []


def test_concurrent_fresh_cli_processes_admit_at_most_two(case):
    barrier = threading.Barrier(3)
    def request():
        barrier.wait(timeout=10)
        return _gate(case, "loop-round", RUN, "review:diff", "--max", "2", "--durable")
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: request(), range(3)))
    assert sum(result.returncode == 0 for result in results) == 2
    assert _gate(case, "loop-round", RUN, "review:diff", "--max", "2", "--durable").returncode != 0


def test_unrelated_legacy_convergence_reset_remains_compatible(case):
    assert _gate(case, "loop-round", RUN, "legacy-convergence", "--max", "1").returncode == 0
    assert _gate(case, "loop-round", RUN, "legacy-convergence", "--reset").returncode == 0
    assert _gate(case, "loop-round", RUN, "legacy-convergence", "--max", "1").returncode == 0


@pytest.mark.parametrize("run_child", [False, True])
def test_cached_plan_is_free_but_pass_edit_and_descriptive_rename_do_not_reset(case, run_child):
    planning = case.repo / ".planning"
    phase = planning / "phases/01-alpha"
    phase.mkdir(parents=True)
    (planning / "config.json").write_text(json.dumps({"model_overrides":{"gsd-planner":"fable"}, "dynamic_routing":{"escalate_on_failure":True}}))
    plan = phase / "PLAN.md"
    plan.write_text("Phase 1: build widget version A\n")
    if run_child:
        case.env["PLAN_WALL_RUN_CHILD"] = "1"
    first = _wall(case, phase)
    assert first.returncode == 0, first.stdout + first.stderr
    first_markers = len(_markers(case))
    assert first_markers > 0, "actual first wall dispatch must reach inert reviewer"
    cached = _wall(case, phase)
    assert cached.returncode == 0
    assert len(_markers(case)) == first_markers
    renamed = phase.with_name("01-renamed-description")
    phase.rename(renamed)
    plan = renamed / "PLAN.md"
    plan.write_text("Phase 1: build widget version B\n")
    second = _wall(case, renamed)
    assert second.returncode == 0, second.stdout + second.stderr
    second_markers = len(_markers(case))
    assert second_markers > first_markers
    second_prompts = [json.loads(row)["prompt"] for row in _markers(case)[first_markers:second_markers]]
    assert second_prompts
    assert all("REPAIR CONFIRMATION: Use the original accepted requirements." in prompt
               and "Do not add requirements or evidence obligations based on reviewer preference." in prompt
               for prompt in second_prompts)
    plan.write_text("Phase 1: build widget version C, a fresh review target\n")
    third = _wall(case, renamed)
    assert third.returncode != 0
    assert "WALL-ROUND-CAP" in third.stdout + third.stderr
    assert len(_markers(case)) == second_markers


def test_genuine_critical_plan_finding_remains_blocking_at_exhaustion(case):
    phase = case.repo / ".planning/phases/01-critical"
    phase.mkdir(parents=True)
    (case.repo / ".planning/config.json").write_text(json.dumps({"model_overrides":{"gsd-planner":"fable"}}))
    plan = phase / "PLAN.md"
    plan.write_text("Phase 1: critical fixture version A\n")
    case.mode.write_text("critical")
    assert _wall(case, phase).returncode != 0
    markers = len(_markers(case))
    assert markers > 0
    plan.write_text("Phase 1: critical fixture version B\n")
    assert _wall(case, phase).returncode != 0
    plan.write_text("Phase 1: critical fixture version C\n")
    final = _wall(case, phase)
    assert final.returncode != 0
    assert "WALL-ROUND-CAP" in final.stdout + final.stderr
    queued = _gate(case, "findings-queue", "list", "--unresolved", "--source", "wall", "--severity", "CRITICAL")
    assert queued.returncode == 0
    assert any(row["issue"] == "actual fixture critical finding (line 1)"
               and row["severity"] == "CRITICAL" and row["resolved"] is False
               for row in json.loads(queued.stdout))


@pytest.mark.parametrize("counter", [None, True, -1, "one"], ids=["none", "boolean", "negative", "string"])
def test_malformed_ordinary_wall_counter_refuses_before_dispatch_without_mutation(case, counter):
    phase = case.repo / ".planning/phases/01-malformed"
    phase.mkdir(parents=True)
    (case.repo / ".planning/config.json").write_text(json.dumps({"model_overrides":{"gsd-planner":"fable"}}))
    (phase / "PLAN.md").write_text("Phase 1: malformed accounting fixture\n")
    data = json.loads(case.store.read_text())
    data.setdefault("_loops", {}).setdefault(RUN, {})["wall:01-malformed"] = counter
    case.store.write_text(json.dumps(data))
    before = case.store.read_bytes()
    result = _wall(case, phase)
    assert result.returncode == 78, result.stdout + result.stderr
    assert _markers(case) == []
    assert case.store.read_bytes() == before
