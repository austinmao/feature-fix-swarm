#!/usr/bin/env python3
"""Remove runner-owned Codex keys and tables from a source config."""

from __future__ import annotations

from pathlib import Path
import re
import sys


TABLE = re.compile(r"^\s*\[([^]]+)]\s*(?:#.*)?$")
RUNNER_KEY = re.compile(r"^\s*(approval_policy|sandbox_mode)\s*=")


def sanitize(text: str) -> str:
    output: list[str] = []
    section: str | None = None
    for line in text.splitlines(keepends=True):
        match = TABLE.match(line)
        if match:
            section = match.group(1).strip()
            if section == "sandbox_workspace_write":
                continue
        if section == "sandbox_workspace_write":
            continue
        if section is None and RUNNER_KEY.match(line):
            continue
        output.append(line)
    return "".join(output)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: sanitize-codex-config.py <config.toml>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if path.exists():
        sys.stdout.write(sanitize(path.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
