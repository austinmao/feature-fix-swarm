#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


runtime = "claude" if "--claude" in sys.argv else "codex" if "--codex" in sys.argv else None
if runtime is None:
    raise SystemExit(2)
root = Path.home() / ".claude" if runtime == "claude" else Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
if os.environ.get("FFS_GSD_STUB_FAIL_RUNTIME") == runtime:
    if os.environ.get("FFS_GSD_STUB_CORRUPT_ON_FAILURE") == "1":
        root.mkdir(parents=True, exist_ok=True)
        (root / "gsd-file-manifest.json").write_text("{truncated")
    raise SystemExit(9)
root.mkdir(parents=True, exist_ok=True)
(root / "gsd-core").mkdir(exist_ok=True)
version = os.environ.get("FFS_GSD_STUB_VERSION", "1.10.0")
(root / "gsd-core/VERSION").write_text(version + "\n")
(root / "gsd-file-manifest.json").write_text(
    json.dumps(
        {
            "version": version,
            "timestamp": "2026-08-01T00:00:00Z",
            "mode": "full",
            "files": {"gsd-core/VERSION": "stub-owned"},
        }
    )
    + "\n"
)
log = os.environ.get("FFS_GSD_STUB_LOG")
if log:
    with Path(log).open("a") as handle:
        handle.write(" ".join(sys.argv[1:]) + "\n")
