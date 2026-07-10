"""Tests for scripts/harness-audit.py — 0-100 harness health scorer.

Invoked as a subprocess (not imported) so the `--json` CLI contract is
exercised end-to-end (plan.md Task 1 action note). Real tmp dirs only, no
filesystem mocking (testing-policy mock-minimization ladder rung 1).

Covers AC-008 / PATH-005 / EDGE-007 (specs/003-orchestration-hardening/spec.md):
  - dangling skill symlink -> `dangling` finding, score < 100
  - vendored-copy version drift -> `drift` finding
  - clean fixture -> score 100, findings [], exit 0
  - --json schema: {score:int, findings:[{kind,path,detail}]}
  - no ~/.claude/skills at all -> score 100 + `skipped: no harness dir` note
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "harness-audit.py"


def _skill_md(version: str) -> str:
    return f'---\nname: sample-skill\nversion: "{version}"\n---\n\n# Sample Skill\n'


def _run(cwd: Path, home: Path) -> subprocess.CompletedProcess:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin"}
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--json"],
        cwd=str(cwd), env=env, capture_output=True, text=True,
    )


def test_dangling_symlink_scores_below_100(tmp_path) -> None:
    home = tmp_path / "home"
    skill_dir = home / ".claude" / "skills" / "sample-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").symlink_to(home / "nowhere.md")  # missing target

    repo = tmp_path / "repo"
    repo.mkdir()

    result = _run(repo, home)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["score"] < 100
    assert any(f["kind"] == "dangling" for f in payload["findings"])


def test_version_drift_reported(tmp_path) -> None:
    home = tmp_path / "home"
    installed_dir = home / ".claude" / "skills" / "sample-skill"
    installed_dir.mkdir(parents=True)
    (installed_dir / "SKILL.md").write_text(_skill_md("2.3.0"))

    repo = tmp_path / "repo"
    packaged_dir = repo / "skills" / "sample-skill"
    packaged_dir.mkdir(parents=True)
    (packaged_dir / "SKILL.md").write_text(_skill_md("2.2.0"))

    result = _run(repo, home)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert any(f["kind"] == "drift" for f in payload["findings"])


def test_clean_fixture_scores_100(tmp_path) -> None:
    home = tmp_path / "home"
    installed_dir = home / ".claude" / "skills" / "sample-skill"
    installed_dir.mkdir(parents=True)
    (installed_dir / "SKILL.md").write_text(_skill_md("1.0.0"))

    repo = tmp_path / "repo"
    packaged_dir = repo / "skills" / "sample-skill"
    packaged_dir.mkdir(parents=True)
    (packaged_dir / "SKILL.md").write_text(_skill_md("1.0.0"))

    result = _run(repo, home)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["score"] == 100
    assert payload["findings"] == []


def test_json_schema_stable(tmp_path) -> None:
    home = tmp_path / "home"
    installed_dir = home / ".claude" / "skills" / "sample-skill"
    installed_dir.mkdir(parents=True)
    (installed_dir / "SKILL.md").write_text(_skill_md("1.0.0"))

    repo = tmp_path / "repo"
    packaged_dir = repo / "skills" / "sample-skill"
    packaged_dir.mkdir(parents=True)
    (packaged_dir / "SKILL.md").write_text(_skill_md("1.0.0"))

    result = _run(repo, home)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload["score"], int)
    assert isinstance(payload["findings"], list)
    for finding in payload["findings"]:
        assert set(finding.keys()) >= {"kind", "path", "detail"}


def test_no_harness_dir_is_not_drift(tmp_path) -> None:
    home = tmp_path / "empty-home"
    home.mkdir()  # exists but no .claude at all

    repo = tmp_path / "repo"
    repo.mkdir()

    result = _run(repo, home)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["score"] == 100
    assert payload.get("note") == "skipped: no harness dir"
