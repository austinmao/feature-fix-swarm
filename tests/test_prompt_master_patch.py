from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PIN = "d15eabbe5d2122eedc060bae8a771381e9873d1b"


def test_prompt_master_pin_is_exact() -> None:
    metadata = json.loads((ROOT / "vendor/prompt-master/pin.json").read_text())
    assert metadata["repository"] == "https://github.com/nidhinjs/prompt-master.git"
    assert metadata["commit"] == PIN


def test_compatibility_patch_applies_to_pinned_upstream(tmp_path: Path) -> None:
    checkout = tmp_path / "prompt-master"
    subprocess.run(
        ["git", "clone", "--quiet", "https://github.com/nidhinjs/prompt-master.git", str(checkout)],
        check=True,
    )
    subprocess.run(["git", "-C", str(checkout), "checkout", "--quiet", PIN], check=True)
    subprocess.run(
        ["git", "-C", str(checkout), "apply", "--check", str(ROOT / "vendor/prompt-master/codex-gpt56.patch")],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(checkout), "apply", str(ROOT / "vendor/prompt-master/codex-gpt56.patch")],
        check=True,
    )
    text = (checkout / "SKILL.md").read_text()
    for phrase in (
        "Goal, Context, Constraints, and Done",
        "Use Plan Mode",
        "bounded subtasks",
        "one coherent outcome",
        "reasoning effort intentionally",
        "Never request hidden chain-of-thought",
    ):
        assert phrase in text

