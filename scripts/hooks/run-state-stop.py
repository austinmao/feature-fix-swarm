#!/usr/bin/env python3
"""Stop hook: check marker file; if active run exists, block stop and inject continuation.

Output contract:
  empty stdout                          -> allow stop
  JSON {decision: "block", reason: ...} -> block stop, inject reason

Performance: O(1) stat() when no run is active.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

LIB = Path.home() / ".claude" / "lib"
sys.path.insert(0, str(LIB))

from run_state.marker import MarkerFile  # noqa: E402
from run_state.state import RunStore, DEFAULT_DB  # noqa: E402


def _marker() -> MarkerFile:
    p = os.environ.get("RUN_STATE_MARKER")
    return MarkerFile(Path(p)) if p else MarkerFile()


def _store() -> RunStore:
    p = os.environ.get("RUN_STATE_DB", str(DEFAULT_DB))
    return RunStore(Path(p))


def main() -> int:
    marker = _marker()
    if not marker.exists():
        return 0
    run_id = marker.read()
    if not run_id:
        marker.clear()
        return 0
    store = _store()
    run = store.get_run(run_id)
    if run is None:
        marker.clear()
        return 0
    if run.state == "active":
        if not store.inc_continuation(run_id):
            marker.clear()
            return 0
        reason = (
            f"Run {run_id} ({run.skill}, phase={run.current_phase or 'unknown'}) is still active.\n"
            f"Objective: {run.objective}\n"
            f"Continue executing the {run.skill} pipeline. "
            f"Resume from current_phase. Update phase via `run-state update {run_id} --phase <name>` as you progress. "
            f"When done, run `run-state complete {run_id}` (after audit passes)."
        )
        print(json.dumps({"decision": "block", "reason": reason}))
        return 0
    if run.state in ("complete", "aborted", "failed", "paused"):
        marker.clear()
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
