"""Characterize the selected installer's emitted user-layout manifests."""
from __future__ import annotations

import json
from pathlib import Path

from test_installer import ROOT, run_setup


def test_selected_setup_emits_resolved_claude_and_codex_layout(tmp_path: Path) -> None:
    unrelated = tmp_path / "home/.codex/skills/operator-owned/SKILL.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"operator bytes\n")

    completed = run_setup(tmp_path, "--scope", "user")

    assert completed.returncode == 0, completed.stderr
    home = tmp_path / "home"
    install = json.loads(
        (home / ".cache/feature-fix-swarm/install-manifest.json").read_text()
    )
    claude = json.loads((home / ".claude/gsd-file-manifest.json").read_text())
    codex = json.loads((home / ".codex/gsd-file-manifest.json").read_text())

    assert install["gsd"]["version"] == "1.14.0"
    assert install["gsd"]["commit"] == bytes.fromhex("f8542fef 67c1f978 ffa70912 cb6f2aaa b76464c6").hex()
    assert install["gsd"]["profiles"] == {"claude": "full", "codex": "full"}
    assert install["gsd"]["owner"] == "upstream-installer"
    assert claude["version"] == codex["version"] == "1.14.0"
    assert (home / ".claude/gsd-core/VERSION").read_text() == "1.14.0\n"
    assert (home / ".codex/gsd-core/VERSION").read_text() == "1.14.0\n"

    # Shared managed skills are emitted to the upstream shared root, outside
    # CODEX_HOME. A config-directory-only implementation would miss them.
    shared = home / ".agents/skills/feature-spec/SKILL.md"
    claude_copy = home / ".claude/skills/feature-spec/SKILL.md"
    assert shared.read_bytes() == (ROOT / "skills/feature-spec/SKILL.md").read_bytes()
    assert claude_copy.read_bytes() == shared.read_bytes()
    assert not (home / ".codex/skills/feature-spec").exists()
    assert unrelated.read_bytes() == b"operator bytes\n"

    managed = {Path(key) for key in install["paths"]}
    assert shared.parent in managed
    assert claude_copy.parent in managed
    assert unrelated not in managed


def test_selected_setup_preserves_external_codex_skill_root_bytes(tmp_path: Path) -> None:
    external_skill = tmp_path / "home/.agents/skills/private-local/SKILL.md"
    external_skill.parent.mkdir(parents=True)
    external_skill.write_bytes("private π bytes\n".encode())

    first = run_setup(tmp_path, "--scope", "user")
    manifest_path = tmp_path / "home/.cache/feature-fix-swarm/install-manifest.json"
    before = manifest_path.read_bytes()
    second = run_setup(tmp_path, "--scope", "user")

    assert first.returncode == second.returncode == 0
    assert manifest_path.read_bytes() == before
    assert external_skill.read_bytes() == "private π bytes\n".encode()
