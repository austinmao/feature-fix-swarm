#!/usr/bin/env bash
# Durable lifecycle records for resumable GSD runs.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  lifecycle.sh checkpoint <run> <state> <reason> <wake-type> <wake-params-json> <resume-argv-json> <budgets-json>
  lifecycle.sh transition <run> <state> <reason>
  lifecycle.sh decrement <run> <respawns|wakes|ci_reruns>
  lifecycle.sh show <run>
  lifecycle.sh validate <run>
USAGE
}

fail() {
  printf 'LIFECYCLE:%s\n' "$*" >&2
  exit 1
}

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
RUN_STATE_DIR="$REPO_ROOT/.planning/run-state"
VERB="${1:-}"
[ -n "$VERB" ] || { usage; fail 'usage'; }
shift
case "$VERB" in
  checkpoint|transition|decrement|show|validate) ;;
  *) usage; fail 'usage' ;;
esac

exec python3 - "$VERB" "$REPO_ROOT" "$RUN_STATE_DIR" "$@" <<'PYEOF'
import datetime as dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from filelock import FileLock

verb, root, run_state, *args = sys.argv[1:]
ROOT = Path(root).resolve()
STATE_DIR = Path(run_state)
STATES = {"running", "waiting", "runnable", "done", "failed", "quarantined"}
WAKE_TYPES = {"wall-decided", "time", "ci-completed", "manual"}
TRANSITIONS = {
    "running": {"waiting", "done", "failed", "quarantined"},
    "waiting": {"runnable", "failed", "quarantined"},
    "runnable": {"running", "failed", "quarantined"},
    "done": set(), "failed": set(), "quarantined": set(),
}
BUDGETS = ("respawns", "wakes", "ci_reruns")


def fail(message: str) -> None:
    print(f"LIFECYCLE:{message}", file=sys.stderr)
    raise SystemExit(1)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def safe_run_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value or ""):
        fail("invalid-run-id")
    return value


def path_for(run_id: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"lifecycle-{safe_run_id(run_id)}.json"


def load(path: Path) -> dict:
    try:
        with path.open() as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError):
        fail("invalid-json")
    if not isinstance(value, dict):
        fail("invalid-record")
    return value


def save(path: Path, value: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def validate(record: dict) -> None:
    required = {"run_id", "state", "reason", "wake_condition", "resume_argv", "budgets", "waiting_since", "wake_at", "updated_at"}
    if not required.issubset(record):
        fail("missing-schema-key")
    if record["state"] not in STATES:
        fail("invalid-state")
    wake = record["wake_condition"]
    if not isinstance(wake, dict) or wake.get("type") not in WAKE_TYPES or "params" not in wake:
        fail("invalid-wake-condition")
    if not isinstance(record["resume_argv"], list) or not record["resume_argv"]:
        fail("invalid-resume-argv")
    executable = Path(str(record["resume_argv"][0])).resolve()
    scripts = (ROOT / "scripts" / "gsd").resolve()
    if executable.name in {"bash", "sh"}:
        if len(record["resume_argv"]) < 2:
            fail("invalid-resume-argv")
        executable = Path(str(record["resume_argv"][1])).resolve()
    if not executable.is_relative_to(scripts):
        fail("invalid-resume-argv")
    budgets = record["budgets"]
    if not isinstance(budgets, dict) or any(not isinstance(budgets.get(key), int) or budgets[key] < 0 for key in BUDGETS):
        fail("invalid-budgets")


def transition(record: dict, target: str, reason: str) -> None:
    source = record.get("state")
    if target not in TRANSITIONS.get(source, set()):
        fail(f"illegal-transition {source}>{target}")
    record["state"] = target
    record["reason"] = reason
    record["updated_at"] = now()
    if target == "waiting":
        record["waiting_since"] = record["updated_at"]


if verb == "checkpoint":
    if len(args) != 7:
        fail("usage")
    run_id, state, reason, wake_type, wake_params_raw, resume_raw, budgets_raw = args
    if state not in STATES or wake_type not in WAKE_TYPES:
        fail("invalid-checkpoint")
    try:
        wake_params = json.loads(wake_params_raw)
        resume_argv = json.loads(resume_raw)
        requested = json.loads(budgets_raw)
    except json.JSONDecodeError:
        fail("invalid-json")
    if not isinstance(requested, dict):
        fail("invalid-budgets")
    path = path_for(run_id)
    with FileLock(f"{path}.lock"):
        if path.exists():
            record = load(path)
            transition(record, state, reason)
            for key in BUDGETS:
                if key in requested:
                    if not isinstance(requested[key], int) or requested[key] > record["budgets"].get(key, 0):
                        fail("budget-increase-refused")
                    record["budgets"][key] = requested[key]
        else:
            budgets = {key: requested.get(key, 0) for key in BUDGETS}
            record = {"run_id": safe_run_id(run_id), "state": state, "reason": reason,
                      "wake_condition": {"type": wake_type, "params": wake_params},
                      "resume_argv": resume_argv, "budgets": budgets,
                      "waiting_since": now() if state == "waiting" else None,
                      "wake_at": None, "updated_at": now()}
        save(path, record)
elif verb == "transition":
    if len(args) != 3:
        fail("usage")
    run_id, target, reason = args
    path = path_for(run_id)
    with FileLock(f"{path}.lock"):
        if not path.exists():
            fail("missing-record")
        record = load(path)
        source = record["state"]
        transition(record, target, reason)
        save(path, record)
    print(f"LIFECYCLE:{source}>{target} run={run_id} reason={reason}")
elif verb == "decrement":
    if len(args) != 2 or args[1] not in BUDGETS:
        fail("usage")
    run_id, key = args
    path = path_for(run_id)
    with FileLock(f"{path}.lock"):
        if not path.exists():
            fail("missing-record")
        record = load(path)
        if record["budgets"].get(key, 0) <= 0:
            fail("budget-exhausted")
        record["budgets"][key] -= 1
        record["updated_at"] = now()
        save(path, record)
elif verb == "show":
    if len(args) != 1:
        fail("usage")
    print(json.dumps(load(path_for(args[0])), indent=2, sort_keys=True))
elif verb == "validate":
    if len(args) != 1:
        fail("usage")
    validate(load(path_for(args[0])))
PYEOF
