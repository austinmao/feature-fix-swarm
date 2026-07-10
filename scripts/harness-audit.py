#!/usr/bin/env python3
"""harness-audit.py — grades the agent HARNESS itself, 0-100, advisory only.

FFS grades the environment a run touches (gates.py, preflight) but never the
harness (~/.claude) a run is LAUNCHED from — a dangling skill symlink or a
stale vendored copy fails silently, only surfacing mid-run (US5,
specs/003-orchestration-hardening/spec.md AC-008). This scorer catches that
BEFORE the run starts.

Four dimensions, each finding deducts from a 100 start:
  1. dangling   — an installed skill's SKILL.md is a symlink to a missing target
  2. drift      — installed skill `version:` frontmatter != the packaged copy's
  3. dead-pin   — .planning/config.json model_overrides value isn't a known model alias
  4. unregistered-hook — a hook script on disk isn't registered in settings.json

Absence of ~/.claude/skills entirely is NOT drift (EDGE-007) — a machine that
never installed the harness scores 100 with a `skipped` note, not a failure.

Always exits 0 — advisory only, never blocks preflight (AC-008).

Usage:
    python3 scripts/harness-audit.py [--json]

--json schema: {"score": int, "findings": [{"kind", "path", "detail"}], "note"?: str}
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Reused from lib/agents_manifest.py::_parse_frontmatter (stdlib regex reader,
# no PyYAML — RESEARCH Anti-Patterns). An unclosed `---` block yields {},
# guarding against fence-less body text injecting a fake `version:`.
FRONTMATTER_FIELD = re.compile(r"^(name|version):\s*(.*)$")

KNOWN_MODEL_ALIASES = {"opus", "sonnet", "haiku", "fable"}
# Full model ids like claude-opus-4-8 / claude-sonnet-4-6 are also valid pins.
KNOWN_MODEL_ID = re.compile(r"^claude-[a-z0-9-]+$")


def _is_known_model(model: object) -> bool:
    return model in KNOWN_MODEL_ALIASES or bool(KNOWN_MODEL_ID.match(str(model)))


def _parse_frontmatter(path: Path) -> dict:
    fields: dict[str, str] = {}
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return fields
    if not lines or lines[0].strip() != "---":
        return fields
    closed = False
    for line in lines[1:200]:
        if line.strip() == "---":
            closed = True
            break
        m = FRONTMATTER_FIELD.match(line)
        if m:
            fields[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return fields if closed else {}


def check_dangling_links(harness_dir: Path) -> list[dict]:
    findings = []
    skills_dir = harness_dir / "skills"
    if not skills_dir.is_dir():
        return findings
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        if skill_md.is_symlink() and not skill_md.exists():
            findings.append({"kind": "dangling", "path": str(skill_md),
                              "detail": "symlink target missing"})
    return findings


def check_version_drift(harness_dir: Path, repo_root: Path) -> list[dict]:
    findings = []
    installed_skills = harness_dir / "skills"
    packaged_skills = repo_root / "skills"
    if not installed_skills.is_dir() or not packaged_skills.is_dir():
        return findings
    for skill_md in sorted(installed_skills.glob("*/SKILL.md")):
        packaged_md = packaged_skills / skill_md.parent.name / "SKILL.md"
        if not packaged_md.is_file():
            continue
        installed_version = _parse_frontmatter(skill_md).get("version")
        packaged_version = _parse_frontmatter(packaged_md).get("version")
        if installed_version and packaged_version and installed_version != packaged_version:
            findings.append({
                "kind": "drift", "path": str(skill_md),
                "detail": f"installed {installed_version} != packaged {packaged_version}",
            })
    return findings


def check_dead_model_pins(config_path: Path) -> list[dict]:
    findings = []
    try:
        config = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError):
        return findings
    for agent, model in (config.get("model_overrides") or {}).items():
        if not _is_known_model(model):
            findings.append({
                "kind": "dead-pin", "path": str(config_path),
                "detail": f"{agent} -> {model!r} not a recognized model alias",
            })
    return findings


def check_hook_drift(harness_dir: Path) -> list[dict]:
    findings = []
    hooks_dir = harness_dir / "hooks"
    settings_path = harness_dir / "settings.json"
    if not hooks_dir.is_dir() or not settings_path.is_file():
        return findings
    try:
        settings = json.loads(settings_path.read_text())
    except (OSError, json.JSONDecodeError):
        return findings
    # Compare against parsed per-command basenames, NOT a substring of one
    # joined string — otherwise `foo.sh` looks registered whenever it's a
    # substring of some OTHER registered command (e.g. `.../xfoo.sh`).
    registered_basenames = {
        Path(token).name
        for entries in (settings.get("hooks") or {}).values()
        for entry in entries
        for hook in entry.get("hooks", [])
        for token in hook.get("command", "").split()
    }
    for hook_script in sorted(hooks_dir.glob("*.sh")) + sorted(hooks_dir.glob("*.py")):
        if hook_script.name not in registered_basenames:
            findings.append({
                "kind": "unregistered-hook", "path": str(hook_script),
                "detail": "on disk but not registered in settings.json hooks",
            })
    return findings


def run_audit(harness_dir: Path, repo_root: Path, config_path: Path) -> dict:
    if not harness_dir.is_dir():
        return {"score": 100, "findings": [], "note": "skipped: no harness dir"}

    findings: list[dict] = []
    findings += check_dangling_links(harness_dir)
    findings += check_version_drift(harness_dir, repo_root)
    findings += check_dead_model_pins(config_path)
    findings += check_hook_drift(harness_dir)

    return {"score": max(0, 100 - 10 * len(findings)), "findings": findings}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit {score, findings} as JSON")
    args = parser.parse_args(argv)

    harness_dir = Path.home() / ".claude"
    repo_root = Path(".")
    config_path = Path(".planning/config.json")

    result = run_audit(harness_dir, repo_root, config_path)

    if args.json:
        print(json.dumps(result))
    else:
        note = f" ({result['note']})" if "note" in result else ""
        print(f"HARNESS-AUDIT: score {result['score']}/100{note}")
        for finding in result["findings"]:
            print(f"  [{finding['kind']}] {finding['path']}: {finding['detail']}")

    return 0  # advisory only — never blocks (AC-008)


if __name__ == "__main__":
    sys.exit(main())
