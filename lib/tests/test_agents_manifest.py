"""Tests for lib/agents_manifest.py — agent roster discovery + manifest + tag check.

TDD RED-first (v3.19.0 Stream A). The manifest is the bridge between a consuming
repo's real sub-agent roster and FFS swarm stages (spec-decompose specialists,
feature-implement [agent:] tags). Stdlib-only, like gates.py.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agents_manifest as am  # noqa: E402

MODULE = Path(__file__).resolve().parents[1] / "agents_manifest.py"


def write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


# ---------------------------------------------------------------- discovery

def test_scan_bare_repo_has_generic_floor_and_builtins(tmp_path):
    m = am.build_manifest(tmp_path)
    # generic floor always present so a bare OSS repo still swarms
    for role in ("planner", "coder", "reviewer", "tester", "researcher"):
        assert role in m["all_agents"]
    # ruflo built-in roles present as fallback
    assert "architect" in m["all_agents"]
    assert "security-architect" in m["all_agents"]
    assert m["version"] == 1
    assert m["domains"]  # non-empty buckets


def test_scan_discovers_local_claude_agents_nested(tmp_path):
    write(tmp_path / ".claude/agents/frontend-engineer.md",
          "---\nname: frontend-engineer\ndescription: Next.js 15 React UI builder\n---\nbody")
    write(tmp_path / ".claude/agents/sub/dir/db-guy.md",
          "---\nname: database-reviewer\ndescription: PostgreSQL schema and migration review\n---\nbody")
    m = am.build_manifest(tmp_path)
    assert "frontend-engineer" in m["all_agents"]
    assert "database-reviewer" in m["all_agents"]
    assert "frontend-engineer" in m["domains"]["frontend"]
    assert "database-reviewer" in m["domains"]["database"]


def test_scan_discovers_codex_toml_agents(tmp_path):
    write(tmp_path / ".codex/agents/brand-designer.toml",
          'name = "brand-designer"\ndescription = "Brand colors, typography, visual design"\n')
    m = am.build_manifest(tmp_path)
    assert "brand-designer" in m["all_agents"]


def test_seed_file_merges_extra_agents_and_overrides(tmp_path):
    write(tmp_path / ".feature-fix-swarm/agents.local.json", json.dumps({
        "extra_agents": [
            {"name": "ecc:typescript-reviewer", "description": "TS review plugin agent"},
        ],
        "domain_overrides": {"review": ["ecc:typescript-reviewer"]},
    }))
    m = am.build_manifest(tmp_path)
    assert "ecc:typescript-reviewer" in m["all_agents"]
    assert "ecc:typescript-reviewer" in m["domains"]["review"]


def test_classification_keywords():
    assert am.classify("security-reviewer", "OWASP vulnerability detection") == "security"
    assert am.classify("planner", "implementation planning specialist") == "planning"
    assert am.classify("email-engineer", "HTML email deliverability") == "other"


def test_classification_auth_not_greedy():
    """'authoring' must not trip the security bucket (real openclaw regression:
    'SOUL.md authoring' classified openclaw-expert as security)."""
    assert am.classify("openclaw-expert", "config auditing and SOUL.md authoring") != "security"
    assert am.classify("auth-reviewer", "OAuth and authentication flows") == "security"


def test_classification_qa_beats_frontend():
    assert am.classify("qa-engineer", "Playwright rendering checks, WCAG component QA") == "testing"


def test_dedup_titlecase_codex_vs_kebab_claude(tmp_path):
    """Codex TOML display names ('Brand Designer') and claude kebab names
    ('brand-designer') are the SAME agent — one canonical kebab entry."""
    write(tmp_path / ".claude/agents/bd.md",
          "---\nname: brand-designer\ndescription: brand visual design\n---\n")
    write(tmp_path / ".codex/agents/bd.toml",
          'name = "Brand Designer"\ndescription = "brand visual design"\n')
    m = am.build_manifest(tmp_path)
    assert "brand-designer" in m["all_agents"]
    assert "Brand Designer" not in m["all_agents"]


def test_codex_only_titlecase_normalized_to_kebab(tmp_path):
    write(tmp_path / ".codex/agents/x.toml",
          'name = "Website Planner"\ndescription = "site audits"\n')
    m = am.build_manifest(tmp_path)
    assert "website-planner" in m["all_agents"]
    assert "Website Planner" not in m["all_agents"]


def test_manifest_deterministic_sorted(tmp_path):
    write(tmp_path / ".claude/agents/zz.md", "---\nname: zz-agent\ndescription: z\n---\n")
    write(tmp_path / ".claude/agents/aa.md", "---\nname: aa-agent\ndescription: a\n---\n")
    m1 = am.build_manifest(tmp_path)
    m2 = am.build_manifest(tmp_path)
    assert m1["all_agents"] == sorted(m1["all_agents"])
    assert m1["all_agents"] == m2["all_agents"]


# ---------------------------------------------------------------- check

def test_check_valid_tags_pass(tmp_path):
    manifest = am.build_manifest(tmp_path)
    mf = tmp_path / "agents.json"
    mf.write_text(json.dumps(manifest))
    tasks = tmp_path / "tasks.md"
    tasks.write_text("- [ ] T001 [model:sonnet] [agent:planner] do a thing\n")
    ok, unknown = am.check_tasks(mf, tasks)
    assert ok and unknown == []


def test_check_unknown_tag_fails(tmp_path):
    manifest = am.build_manifest(tmp_path)
    mf = tmp_path / "agents.json"
    mf.write_text(json.dumps(manifest))
    tasks = tmp_path / "tasks.md"
    tasks.write_text("- [ ] T001 [agent:nonexistent-xyz-agent] do a thing\n")
    ok, unknown = am.check_tasks(mf, tasks)
    assert not ok
    assert "nonexistent-xyz-agent" in unknown


def test_check_accepts_dept_slash_role_form(tmp_path):
    """FFS task grammar uses [agent:dept/role]; manifest stores bare role names."""
    write(tmp_path / ".claude/agents/fe.md",
          "---\nname: frontend-engineer\ndescription: react ui\n---\n")
    manifest = am.build_manifest(tmp_path)
    mf = tmp_path / "agents.json"
    mf.write_text(json.dumps(manifest))
    tasks = tmp_path / "tasks.md"
    tasks.write_text("- [ ] T001 [agent:engineering/frontend-engineer] build page\n")
    ok, unknown = am.check_tasks(mf, tasks)
    assert ok and unknown == []


# ---------------------------------------------------------------- CLI

def run_cli(*args, cwd=None):
    return subprocess.run([sys.executable, str(MODULE), *args],
                          capture_output=True, text=True, cwd=cwd)


def test_cli_scan_writes_manifest_and_prints_summary(tmp_path):
    write(tmp_path / ".claude/agents/fe.md",
          "---\nname: frontend-engineer\ndescription: react\n---\n")
    out = tmp_path / ".feature-fix-swarm/agents.json"
    r = run_cli("scan", "--repo", str(tmp_path), "--out", str(out))
    assert r.returncode == 0, r.stderr
    assert "AGENTS-MANIFEST:" in r.stdout
    data = json.loads(out.read_text())
    assert "frontend-engineer" in data["all_agents"]


def test_cli_check_exit_codes(tmp_path):
    out = tmp_path / ".feature-fix-swarm/agents.json"
    assert run_cli("scan", "--repo", str(tmp_path), "--out", str(out)).returncode == 0
    good = tmp_path / "good.md"
    good.write_text("- [ ] T001 [agent:coder] x\n")
    bad = tmp_path / "bad.md"
    bad.write_text("- [ ] T001 [agent:zzz-not-real] x\n")
    assert run_cli("check", str(good), "--manifest", str(out)).returncode == 0
    r = run_cli("check", str(bad), "--manifest", str(out))
    assert r.returncode == 1
    assert "zzz-not-real" in r.stdout


def test_cli_check_missing_manifest_fails_closed(tmp_path):
    tasks = tmp_path / "t.md"
    tasks.write_text("- [ ] T001 [agent:coder] x\n")
    r = run_cli("check", str(tasks), "--manifest", str(tmp_path / "nope.json"))
    assert r.returncode == 1
