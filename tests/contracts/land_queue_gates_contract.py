"""Direct-only Wave-0 contract for degradation binding and prod promotion.

This filename intentionally avoids pytest's default test_*.py / *_test.py rules.
Its evidence is always the explicitly named direct path, never a vacuous broad
collection run.
"""
from __future__ import annotations

import importlib.util
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
def reviewed_git(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"; repo.mkdir()
    def run(*args): return subprocess.run(["git", *args], cwd=repo, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
    run("init", "-b", "main"); run("config", "user.email", "wave0@example.invalid"); run("config", "user.name", "Wave 0")
    (repo / "README.md").write_text("base\n"); run("add", "README.md"); run("commit", "-m", "base"); base = run("rev-parse", "HEAD")
    run("checkout", "-b", "reviewed"); (repo / "lib.py").write_text("production\n"); run("add", "lib.py"); run("commit", "-m", "production"); return repo, base, run("rev-parse", "HEAD")


@pytest.mark.parametrize("case", [
    "computed three-dot files override caller omissions", "below/equal/above 50 percent ratio",
    "degraded production touch always refuses", "non-production event uses aggregate only",
    "malformed binding fails closed", "legacy event conservatively touches production",
    "different-run events are irrelevant",
])
def test_degradation_binding_and_check_grant_prod_authority(reviewed_git, tmp_path, case):
    repo, baseline, head = reviewed_git
    assert subprocess.run(["git", "diff", "--name-only", f"{baseline}...{head}"], cwd=repo, check=True, text=True, stdout=subprocess.PIPE).stdout.strip() == "lib.py"
    gates = gates_module()
    assert callable(gates.note_degraded) and callable(gates.check_grant_prod)
    pytest.fail(f"RED-EXPECTED: REQ-209 {case} requires queue-bound production authority")
