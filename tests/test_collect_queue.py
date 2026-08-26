"""Wave-0 contract for the Phase-2 collector (REQ-201..203, EDGE-005).

The fixture deliberately uses a bare file-protocol origin and real worktrees.
It loads the future collector only at test time so its absence is a typed RED
failure instead of a pytest collection error.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "skills" / "land-queue" / "scripts" / "collect-queue.py"
ESTATE = ROOT / "skills" / "git-branch-consolidate" / "scripts" / "collect-estate.py"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


@pytest.fixture
def real_git_estate(tmp_path: Path) -> dict[str, Path | str]:
    """A real main/feature topology, local bare origin, and linked worktree."""
    origin, work, linked = tmp_path / "origin.git", tmp_path / "work", tmp_path / "linked"
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GIT_ASKPASS"):
        env.pop(key, None)
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, env=env, stdout=subprocess.PIPE)
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, env=env, stdout=subprocess.PIPE)
    git(work, "config", "user.email", "wave0@example.invalid")
    git(work, "config", "user.name", "Wave 0")
    (work / "README.md").write_text("base\n")
    git(work, "add", "README.md"); git(work, "commit", "-m", "base")
    git(work, "remote", "add", "origin", str(origin)); git(work, "push", "origin", "main")
    git(work, "checkout", "-b", "spec/201-docs")
    (work / "docs.md").write_text("docs only\n")
    git(work, "add", "docs.md"); git(work, "commit", "-m", "docs")
    head = git(work, "rev-parse", "HEAD")
    git(work, "push", "origin", "spec/201-docs")
    git(work, "checkout", "main")
    git(work, "worktree", "add", str(linked), "spec/201-docs")
    return {"origin": origin, "work": work, "linked": linked, "head": head}


def load_collector():
    if not COLLECTOR.is_file():
        pytest.fail("RED-EXPECTED: REQ-201 collect-queue.py is not shipped")
    spec = importlib.util.spec_from_file_location("collect_queue", COLLECTOR)
    if spec is None or spec.loader is None:
        pytest.fail("RED-EXPECTED: REQ-201 collector has no loadable module spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules["collect_queue"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # typed failure keeps malformed targets out of collection phase
        pytest.fail(f"RED-EXPECTED: REQ-201 collector load failed: {type(exc).__name__}")
    return module


@pytest.mark.parametrize("requirement, scenario", [
    ("REQ-201", "fetch-first and three-source union"),
    ("REQ-201", "docs-only disposition is landable"),
    ("REQ-201", "head/run/spec identity conflicts block"),
    ("REQ-201", "empty intake is deterministic"),
    ("REQ-202", "transitive overlap components serialize oldest-first"),
    ("REQ-202", "disjoint residual-file ordering is stable"),
    ("REQ-203", "already-landed is skipped at item start"),
    ("REQ-203", "gone branch with reachable head reconciles landed"),
    ("REQ-203", "gone branch without proof blocks source-missing"),
    ("REQ-203", "one-rebase merge-tree conflict blocks"),
    ("EDGE-005", "external merge is rechecked before dispatch"),
])
def test_collector_contracts_use_real_git_facts(real_git_estate, requirement, scenario):
    """Named requirement matrix; future assertions execute public collector helpers."""
    work = real_git_estate["work"]
    assert (work / ".git").exists()
    assert git(work, "remote", "get-url", "origin").startswith("/")
    assert git(work, "diff", "--name-only", "main...spec/201-docs") == "docs.md"
    assert ESTATE.is_file(), "collect-estate remains real first-party authority"
    module = load_collector()
    pytest.fail(f"RED-EXPECTED: {requirement} missing collector behavior: {scenario}; loaded={module.__name__}")


def test_coverage_contract_targets_only_stable_dynamic_module_name(real_git_estate):
    # This is intentionally a normal failing test until Plan 01 supplies the API.
    module = load_collector()
    assert module.__name__ == "collect_queue"
    pytest.fail("RED-EXPECTED: REQ-201 stable collect_queue coverage contract needs public collection API")
