"""Roster↔docs drift gate: every REQUIRED row in scripts/gsd/deps.sh must be
mentioned in docs/dependencies.md, so the executable roster and the dependency
page cannot diverge silently (the drift class that produced a stale pin hash
and a phantom-scanner claim in the past)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# doc alias: roster names whose documented spelling differs
DOC_ALIASES = {
    "node": ["Node.js"],
    "claude,codex": ["Claude Code", "Codex CLI"],
    "shasum,sha256sum": ["shasum"],
    "timeout,gtimeout": ["timeout"],
    "@opengsd/gsd-core": ["@opengsd/gsd-core"],
}


def roster_rows() -> list[tuple[str, str, str]]:
    text = (ROOT / "scripts" / "gsd" / "deps.sh").read_text()
    match = re.search(r"ROSTER='([^']+)'", text)
    assert match, "ROSTER block not found in deps.sh"
    rows = []
    for line in match.group(1).strip().splitlines():
        name, kind, required, _remedy = line.split("|", 3)
        rows.append((name, kind, required))
    return rows


def test_roster_parses_and_has_both_tiers() -> None:
    rows = roster_rows()
    assert len(rows) >= 20
    assert any(required == "required" for _, _, required in rows)
    assert any(required == "optional" for _, _, required in rows)
    for _, kind, _ in rows:
        assert kind in {"binary", "npm", "pip", "pin"}


def test_every_required_roster_entry_is_documented() -> None:
    docs = (ROOT / "docs" / "dependencies.md").read_text()
    missing = []
    for name, _kind, required in roster_rows():
        if required != "required":
            continue
        needles = DOC_ALIASES.get(name, [name])
        if not all(needle in docs for needle in needles):
            missing.append(name)
    assert missing == [], f"required deps absent from docs/dependencies.md: {missing}"


def test_readme_points_at_the_executable_roster() -> None:
    readme = (ROOT / "README.md").read_text()
    assert "scripts/gsd/deps.sh check" in readme
    assert "docs/initialization.md" in readme
