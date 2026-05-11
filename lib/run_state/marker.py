"""Marker file at ~/.claude/state/.active-run.

Single source of truth for 'is anything running?'. Stop hook stats this first
(1 syscall) so the common case (no active run) costs almost nothing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

DEFAULT_PATH = Path.home() / ".claude" / "state" / ".active-run"


class MarkerFile:
    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self.path = path

    def exists(self) -> bool:
        return self.path.exists()

    def read(self) -> Optional[str]:
        if not self.path.exists():
            return None
        content = self.path.read_text(encoding="utf-8").strip()
        return content or None

    def set(self, run_id: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(run_id, encoding="utf-8")

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
