from __future__ import annotations

import importlib.util
import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import re

import pytest

LIB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LIB))
SPEC = importlib.util.spec_from_file_location("clean_code_metrics", LIB / "clean_code_metrics.py")
ccm = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ccm)

FAKE_CRAPPER = """#!{py}
import pathlib, re, sys
path = pathlib.Path(sys.argv[-1])
root = pathlib.Path(sys.argv[sys.argv.index("--root") + 1])
text = path.read_text()
ns = ".".join(path.relative_to(root).with_suffix("").parts)  # real crapper: dotted path under --root
print("function  namespace  CC  Cov%  CRAP")
print("--------------------------------------")
for name, cc in re.findall(r"# cc:(\\w+)=(\\d+)", text):
    print(f"{{name}}  {{ns}}  {{cc}}  N/A  N/A")
"""
FAKE_DRYER = """#!{py}
import pathlib, sys
if (pathlib.Path(__file__).parent / "crash").exists():
    sys.exit(3)
print((pathlib.Path(__file__).parent / "dryer.out").read_text())
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    (root / "src/mod.py").write_text("# cc:old_fn=12\n# cc:small=2\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    monkeypatch.chdir(root)
    return root


def install_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dups: str = "") -> Path:
    tools = tmp_path / "tools"
    for name, body in (("crapper", FAKE_CRAPPER), ("dryer", FAKE_DRYER)):
        script = tools / name / name
        script.parent.mkdir(parents=True)
        script.write_text(body.format(py=sys.executable))
        script.chmod(0o755)
    (tools / "dryer/dryer.out").write_text(dups)
    monkeypatch.setenv("FFS_UB_TOOLS_DIR", str(tools))
    return tools


@pytest.fixture(autouse=True)
def hide_real_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    only_git = tmp_path / "bin"
    only_git.mkdir()
    (only_git / "git").symlink_to(shutil.which("git"))
    monkeypatch.setenv("PATH", str(only_git))
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    monkeypatch.delenv("FFS_UB_TOOLS_DIR", raising=False)


def run_main(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], diff: str) -> tuple[int, str]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(diff))
    rc = ccm.main([])
    return rc, capsys.readouterr().out


def diff_of(repo: Path) -> str:
    return git(repo, "diff", "-U0", "HEAD")


def test_no_tools_prints_nothing(repo, monkeypatch, capsys):
    (repo / "src/mod.py").write_text("# cc:new_fn=15\n")
    assert run_main(monkeypatch, capsys, diff_of(repo)) == (0, "")


def test_parse_diff_files_ranges_deletion_and_tests():
    diff = (
        "+++ b/src/a.py\n@@ -1,0 +2,3 @@\n+x\n@@ -9 +12 @@\n+y\n@@ -20,2 +22,0 @@\n"
        "+++ /dev/null\n@@ -1,2 +0,0 @@\n"
        "+++ b/tests/test_a.py\n@@ -1 +1 @@\n"
    )
    assert ccm.parse_diff(diff) == {
        "src/a.py": [(2, 4), (12, 12)],
        "tests/test_a.py": [(1, 1)],
    }
    assert ccm.production_files(ccm.parse_diff(diff), cap=40) == ["src/a.py"]


def test_new_function_over_threshold_reported(repo, tmp_path, monkeypatch, capsys):
    install_tools(tmp_path, monkeypatch)
    (repo / "src/mod.py").write_text("# cc:old_fn=12\n# cc:new_fn=11\n# cc:tiny=3\n")
    rc, out = run_main(monkeypatch, capsys, diff_of(repo))
    assert rc == 0
    assert "CLEAN-CODE METRICS (advisory; tools: crapper, dryer)" in out
    assert "COMPLEXITY: src/mod.py new_fn CC new->11" in out
    assert "old_fn" not in out and "tiny" not in out


def test_grown_cc_reported_unchanged_not(repo, tmp_path, monkeypatch, capsys):
    install_tools(tmp_path, monkeypatch)
    (repo / "src/mod.py").write_text("# cc:old_fn=14\n# cc:small=2\n")
    _, out = run_main(monkeypatch, capsys, diff_of(repo))
    assert "COMPLEXITY: src/mod.py old_fn CC 12->14" in out


@pytest.mark.parametrize("rel", ["src/mod.py", "lib/pkg/__init__.py", "src/util.py"])
def test_unchanged_complex_function_not_new_in_any_layout(repo, tmp_path, monkeypatch, capsys, rel):
    install_tools(tmp_path, monkeypatch)
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text("# cc:old_fn=12\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "layout")
    (repo / rel).write_text("# cc:old_fn=12\n# cc:added_fn=13\n")
    _, out = run_main(monkeypatch, capsys, diff_of(repo))
    assert f"COMPLEXITY: {rel} added_fn CC new->13" in out
    assert "old_fn" not in out


def test_duplication_overlap_kept_non_overlap_dropped(repo, tmp_path, monkeypatch, capsys):
    dups = (
        f"DUPLICATE score=0.91\n  {repo}/src/mod.py:1-5\n  src/other.py:3-9\n\n"
        "DUPLICATE score=0.85\n  src/mod.py:40-50\n  src/other.py:1-4\n"
    )
    install_tools(tmp_path, monkeypatch, dups)
    (repo / "src/mod.py").write_text("# cc:old_fn=12\n# cc:small=2\nx = 1\n")
    _, out = run_main(monkeypatch, capsys, diff_of(repo))
    assert "DUPLICATION: 0.91 src/mod.py:1-5 src/other.py:3-9" in out
    assert "0.85" not in out


def test_repo_tree_untouched(repo, tmp_path, monkeypatch, capsys):
    install_tools(tmp_path, monkeypatch, "DUPLICATE score=0.9\n  src/mod.py:1-2\n  src/o.py:1-2\n")
    (repo / "src/mod.py").write_text("# cc:new_fn=20\n")
    before = git(repo, "status", "--porcelain", "--untracked-files=all")
    run_main(monkeypatch, capsys, diff_of(repo))
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == before
    assert not (repo / ".metrics").exists()


def test_tool_crash_and_timeout_exit_zero(repo, tmp_path, monkeypatch, capsys):
    tools = install_tools(tmp_path, monkeypatch, "DUPLICATE score=0.9\n  src/mod.py:1-2\n  src/o.py:1-2\n")
    (repo / "src/mod.py").write_text("# cc:new_fn=20\n")
    (tools / "dryer/crash").write_text("")
    rc, out = run_main(monkeypatch, capsys, diff_of(repo))
    assert rc == 0 and "COMPLEXITY" in out and "DUPLICATION" not in out
    (tools / "crapper/crapper").write_text(f"#!{sys.executable}\nimport time\ntime.sleep(5)\n")
    monkeypatch.setattr(ccm, "BUDGET", 1)
    assert run_main(monkeypatch, capsys, diff_of(repo)) == (0, "")


def test_overall_budget_bounds_total_runtime(repo, tmp_path, monkeypatch, capsys):
    tools = install_tools(tmp_path, monkeypatch, "DUPLICATE score=0.9\n  src/mod.py:1-2\n  src/o.py:1-2\n")
    (repo / "src/mod.py").write_text("# cc:new_fn=20\n")
    (tools / "crapper/crapper").write_text(f"#!{sys.executable}\nimport time\ntime.sleep(3)\n")
    monkeypatch.setattr(ccm, "BUDGET", 1)
    start = time.monotonic()
    rc, out = run_main(monkeypatch, capsys, diff_of(repo))
    assert time.monotonic() - start < 2.5
    assert rc == 0 and out == ""


def test_output_capped_at_twenty_lines(repo, tmp_path, monkeypatch, capsys):
    install_tools(tmp_path, monkeypatch)
    (repo / "src/mod.py").write_text("".join(f"# cc:f{i}=11\n" for i in range(25)))
    _, out = run_main(monkeypatch, capsys, diff_of(repo))
    assert out.count("COMPLEXITY:") == 20 and "... 5 more" in out


def test_review_gate_pass3_splices_the_metrics_advisory() -> None:
    text = (LIB.parent / "skills/review-gate/SKILL.md").read_text()
    assert "clean_code_metrics.py" in text
    assert "CLEAN-CODE METRICS" in text
    assert re.search(r"CLEAN-CODE METRICS[^\n]*at most MEDIUM", text)


def test_metrics_tools_are_an_optional_documented_dependency() -> None:
    roster = (LIB.parent / "scripts/gsd/deps.sh").read_text()
    for tool in ("crapper", "dryer"):
        assert re.search(rf"^{tool}\|binary\|optional\|", roster, re.M), tool
    docs = (LIB.parent / "docs/dependencies.md").read_text()
    assert "crapper" in docs and "dryer" in docs and "FFS_UB_TOOLS_DIR" in docs


def test_find_tool_ignores_tools_inside_the_reviewed_repo(tmp_path, monkeypatch):
    """A relative PATH entry or FFS_UB_TOOLS_DIR resolves against the reviewed
    checkout, so a branch could supply the binary the gate runs."""
    repo = tmp_path / "repo"
    tool = repo / "bin" / "crapper"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    (repo / "crapper" / "crapper").parent.mkdir()
    shutil.copy2(tool, repo / "crapper" / "crapper")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", "bin" + os.pathsep + "/usr/bin" + os.pathsep + "/bin")
    monkeypatch.setenv("FFS_UB_TOOLS_DIR", ".")
    assert ccm.find_tool("crapper") is None
    monkeypatch.setenv("FFS_UB_TOOLS_DIR", str(repo))
    assert ccm.find_tool("crapper") is None
