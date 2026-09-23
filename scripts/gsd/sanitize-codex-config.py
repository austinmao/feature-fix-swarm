#!/usr/bin/env python3
"""Project an untrusted Codex config into an empty runtime policy.

The runner supplies every runtime setting itself.  In particular this helper
must never become a filtering copy: Codex config grows new execution surfaces
regularly and an allowlist of zero inherited settings fails safely.
"""

from __future__ import annotations

import sys


def sanitize(text: str) -> str:
    # Deliberately inspect no values.  The source home can contain provider,
    # MCP, web, hook, notification, instruction, and environment policy data.
    # Runtime-owned settings are rendered by gsd-run after this projection.
    del text
    return ""


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: sanitize-codex-config.py <config.toml>", file=sys.stderr)
        return 2
    # The argument remains for command compatibility.  Do not read it: this
    # operation is a from-scratch projection, not a source-config migration.
    sys.stdout.write(sanitize(""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
