#!/usr/bin/env bash
# Durable lifecycle records for resumable GSD runs.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  lifecycle.sh checkpoint <run> <state> <reason> <wake-type> <wake-params-json> <resume-argv-json> <budgets-json>
  lifecycle.sh transition <run> <state> <reason>
  lifecycle.sh decrement <run> <respawns|wakes|ci_reruns>
  lifecycle.sh wake-checkpoint <run> <wake-at> <resume-argv-json> <max-attempts>
  lifecycle.sh ci-reserve <run> <attempt> <max-reruns>
  lifecycle.sh ci-complete-rerun <run>
  lifecycle.sh ci-refund-rerun <run>
  lifecycle.sh ci-gh-error <run> <max-errors>
  lifecycle.sh ci-probe-success <run>
  lifecycle.sh ci-ready <run>
  lifecycle.sh ci-failed <run> <reason>
  lifecycle.sh show <run>
  lifecycle.sh validate <run>
  lifecycle.sh relaunch <run> <reason> [--no-charge]
  lifecycle.sh set-pid <run> <pid>
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
  checkpoint|transition|decrement|wake-checkpoint|ci-reserve|ci-complete-rerun|ci-refund-rerun|ci-gh-error|ci-probe-success|ci-ready|ci-failed|show|validate|relaunch|set-pid) ;;
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


def save_validated(path: Path, record: dict) -> None:
    """Keep write verbs from persisting records the reader would reject."""
    validate(record)
    save(path, record)


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
        save_validated(path, record)
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
        save_validated(path, record)
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
        save_validated(path, record)
elif verb == "wake-checkpoint":
    if len(args) != 4:
        fail("usage")
    run_id, wake_at_raw, resume_raw, maximum_raw = args
    try:
        wake_at, maximum = int(wake_at_raw), int(maximum_raw)
        resume_argv = json.loads(resume_raw)
    except (ValueError, json.JSONDecodeError):
        fail("invalid-wake")
    if wake_at < 0 or maximum < 0:
        fail("invalid-wake")
    path = path_for(run_id)
    with FileLock(f"{path}.lock"):
        if path.exists():
            record = load(path)
            if record.get("state") != "waiting":
                fail("illegal-transition")
        else:
            record = {"run_id": safe_run_id(run_id), "state": "waiting", "reason": "session-limit",
                      "wake_condition": {"type": "time", "params": {}}, "resume_argv": resume_argv,
                      "budgets": {"respawns": 0, "wakes": maximum, "ci_reruns": 0},
                      "waiting_since": now(), "wake_at": None, "updated_at": now()}
        if record["budgets"].get("wakes", 0) <= 0:
            record["state"] = "failed"
            record["reason"] = "wake-budget-exhausted"
            record["updated_at"] = now()
            save_validated(path, record)
            print("LIFECYCLE:wake-exhausted")
            raise SystemExit(2)
        record["budgets"]["wakes"] -= 1
        record["state"] = "waiting"
        record["reason"] = "session-limit"
        record["wake_condition"] = {"type": "time", "params": {"wake_at": wake_at}}
        record["wake_at"] = wake_at
        record["waiting_since"] = now()
        record["updated_at"] = now()
        save_validated(path, record)
    print(f"LIFECYCLE:wake-reserved run={run_id}")
elif verb == "ci-reserve":
    if len(args) != 3:
        fail("usage")
    run_id, attempt_raw, maximum_raw = args
    try:
        attempt, maximum = int(attempt_raw), int(maximum_raw)
    except ValueError:
        fail("invalid-attempt")
    path = path_for(run_id)
    with FileLock(f"{path}.lock"):
        if not path.exists():
            fail("missing-record")
        record = load(path)
        ci = record.setdefault("ci", {})
        if ci.get("rerun_pending"):
            print("LIFECYCLE:ci-pending")
        elif attempt <= int(ci.get("last_classified_attempt", 0)):
            print("LIFECYCLE:ci-cas-lost")
        elif record["budgets"].get("ci_reruns", 0) <= 0:
            record["state"] = "failed"; record["reason"] = "ci-rerun-exhausted"; record["updated_at"] = now()
            save_validated(path, record)
            print("LIFECYCLE:ci-exhausted")
        else:
            record["budgets"]["ci_reruns"] -= 1
            ci["last_classified_attempt"] = attempt
            ci["rerun_pending"] = True
            record["updated_at"] = now()
            save_validated(path, record)
            print("LIFECYCLE:ci-reserved")
elif verb == "ci-complete-rerun":
    if len(args) != 1:
        fail("usage")
    path = path_for(args[0])
    with FileLock(f"{path}.lock"):
        record = load(path); record.setdefault("ci", {})["rerun_pending"] = False; record["updated_at"] = now(); save_validated(path, record)
elif verb == "ci-refund-rerun":
    if len(args) != 1:
        fail("usage")
    path = path_for(args[0])
    with FileLock(f"{path}.lock"):
        record = load(path); validate(record)
        ci = record.setdefault("ci", {})
        if ci.get("rerun_pending"):
            record["budgets"]["ci_reruns"] += 1
            ci["rerun_pending"] = False
        record["updated_at"] = now(); save_validated(path, record)
elif verb == "ci-gh-error":
    if len(args) != 2:
        fail("usage")
    path = path_for(args[0])
    with FileLock(f"{path}.lock"):
        record = load(path); ci = record.setdefault("ci", {}); ci["gh_errors"] = int(ci.get("gh_errors", 0)) + 1
        if ci["gh_errors"] >= int(args[1]): record["state"] = "failed"; record["reason"] = "ci-gh-error-exhausted"
        record["updated_at"] = now(); save_validated(path, record); print(f"LIFECYCLE:ci-gh-errors={ci['gh_errors']}")
elif verb == "ci-probe-success":
    if len(args) != 1:
        fail("usage")
    path = path_for(args[0])
    with FileLock(f"{path}.lock"):
        record = load(path); record.setdefault("ci", {})["gh_errors"] = 0; record["updated_at"] = now(); save_validated(path, record)
elif verb == "ci-ready":
    if len(args) != 1:
        fail("usage")
    path = path_for(args[0])
    with FileLock(f"{path}.lock"):
        record = load(path); transition(record, "runnable", "ci-pass"); record.setdefault("ci", {})["gh_errors"] = 0; save_validated(path, record)
elif verb == "ci-failed":
    if len(args) != 2:
        fail("usage")
    path = path_for(args[0])
    with FileLock(f"{path}.lock"):
        record = load(path); transition(record, "failed", args[1]); save_validated(path, record)
elif verb == "show":
    if len(args) != 1:
        fail("usage")
    print(json.dumps(load(path_for(args[0])), indent=2, sort_keys=True))
elif verb == "validate":
    if len(args) != 1:
        fail("usage")
    validate(load(path_for(args[0])))
elif verb == "relaunch":
    if len(args) not in {2, 3} or (len(args) == 3 and args[2] != "--no-charge"):
        fail("usage")
    run_id, reason = args[:2]
    charge = len(args) == 2
    path = path_for(run_id)
    with FileLock(f"{path}.lock"):
        record = load(path)
        validate(record)
        if record["state"] not in {"waiting", "runnable"}:
            fail("not-relaunchable")
        if charge and record["budgets"]["respawns"] <= 0:
            record["state"] = "failed"; record["reason"] = "respawn-budget-exhausted"; record["updated_at"] = now()
            save_validated(path, record)
            fail("budget-exhausted")
        if charge:
            record["budgets"]["respawns"] -= 1
        record["state"] = "running"; record["reason"] = reason; record["launched_at"] = int(dt.datetime.now().timestamp()); record["launcher_pid"] = os.getpid(); record["updated_at"] = now()
        save_validated(path, record)
        print(json.dumps(record["resume_argv"]))
elif verb == "set-pid":
    if len(args) != 2 or not args[1].isdigit():
        fail("usage")
    path = path_for(args[0])
    with FileLock(f"{path}.lock"):
        record = load(path); validate(record)
        if record["state"] != "running":
            fail("not-running")
        record["child_pid"] = int(args[1]); record["updated_at"] = now(); save_validated(path, record)
PYEOF
