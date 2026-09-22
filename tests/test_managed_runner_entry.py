"""Exercise the production shell ingress before its first stateful preflight."""
import os
from pathlib import Path
import subprocess
import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("selected", [True, False])
def test_selected_managed_runner_cannot_fall_through_to_planning_wall(tmp_path, selected):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith(("FFS_", "GSD_", "GIT_")):
            del env[key]
    env.update(FFS_STATE_ROOT=str(tmp_path / "authority"), FFS_REQUEST_KEY="entry-request")
    if selected:
        env["FFS_SELECTION_MANIFEST"] = str(tmp_path / "missing-selection.json")
    before = sorted(str(p.relative_to(repo)) for p in repo.rglob("*"))
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/gsd/gsd-run.sh"), "/gsd-execute-phase", "1"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode != 0
    assert '"ok": false' in result.stdout
    assert "requirement-ownership" not in result.stderr
    assert "plan-wall:" not in result.stderr
    assert not (repo / ".planning").exists()
    assert not (tmp_path / "authority").exists()
    assert sorted(str(p.relative_to(repo)) for p in repo.rglob("*")) == before
