#!/usr/bin/env python3
"""Write the versioned takeover record from typed, local facts only."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_IO_SPEC = importlib.util.spec_from_file_location(
    "takeover_io", Path(__file__).with_name("takeover-io.py")
)
assert _IO_SPEC is not None and _IO_SPEC.loader is not None
_IO = importlib.util.module_from_spec(_IO_SPEC)
sys.modules[_IO_SPEC.name] = _IO
_IO_SPEC.loader.exec_module(_IO)
UnsafeTakeoverPath = _IO.UnsafeTakeoverPath
replace_bytes = _IO.replace_bytes
takeover_directory = _IO.takeover_directory
validate_final = _IO.validate_final


def refuse(message: str) -> None:
    raise RuntimeError(f"TAKEOVER-WRITER-REFUSED: {message}")


def dirty_entries() -> list[str]:
    done = subprocess.run(["git", "status", "--porcelain=v2", "-z", "--untracked-files=all"],
                          capture_output=True, check=False)
    if done.returncode:
        return []
    return sorted(row.decode("utf-8", "surrogateescape") for row in done.stdout.split(b"\0") if row)


def runner_snapshot(root: Path) -> dict:
    state = root / ".planning" / "run-state"
    status = (state / "gsd-run.status").read_text(errors="replace").strip() if (state / "gsd-run.status").is_file() else "unknown"
    pid_text = (state / "gsd-run.pid").read_text(errors="replace").strip() if (state / "gsd-run.pid").is_file() else ""
    try:
        pid = int(pid_text)
    except ValueError:
        pid = None
    return {"status": status, "pid": pid, "live": bool(pid and pid > 0 and os.path.exists(f"/proc/{pid}"))}


def phase_snapshot(root: Path) -> list[dict]:
    phases = root / ".planning" / "phases"
    rows: list[dict] = []
    if phases.is_dir():
        for path in sorted(phases.rglob("*-PLAN.md")):
            rel = path.relative_to(root).as_posix()
            rows.append({"plan": rel, "summary": path.with_name(path.name.replace("-PLAN.md", "-SUMMARY.md")).relative_to(root).as_posix(), "complete": path.with_name(path.name.replace("-PLAN.md", "-SUMMARY.md")).is_file()})
    return rows


def git(*args: str) -> str:
    done = subprocess.run(["git", *args], text=True, capture_output=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gates", required=True)
    ap.add_argument("--spec-id", required=True)
    ap.add_argument("--run-id", required=True)
    args = ap.parse_args()
    env = dict(os.environ)
    store_dir = subprocess.check_output([sys.executable, args.gates, "store-dir"], text=True, env=env).strip()
    store_path = subprocess.check_output([sys.executable, args.gates, "store-path"], text=True, env=env).strip()
    state = json.loads(subprocess.check_output([sys.executable, args.gates, "takeover-state", args.run_id], text=True, env=env))
    root = Path(git("rev-parse", "--show-toplevel") or os.getcwd())
    created = int(time.time())
    dirty = dirty_entries()
    record = {
        "schema_version": 1,
        "created_at": created,
        "ids": {"spec_id": args.spec_id, "run_id": args.run_id},
        "gates_store": str(Path(store_path).resolve(strict=False)),
        "gates_store_anchor": hashlib.sha256(str(Path(store_path).resolve(strict=False)).encode()).hexdigest(),
        "git_state": {"branch": git("branch", "--show-current"), "head": git("rev-parse", "HEAD"), "upstream": git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"), "dirty": dirty},
        "preflight": state.get("preflight", {}), "grants": state.get("grants", []),
        "pendings": state.get("pendings", []), "promotions": state.get("promotions", []),
        "runner": runner_snapshot(root), "unresolved_findings": state.get("unresolved_findings", []),
        "phases": phase_snapshot(root), "evidence": [],
        "forbid": ([{"action": "mid-rebase", "probe": "mid-rebase", "reason": "git rebase is in progress"}]
                   if (root / ".git" / "rebase-merge").is_dir() or (root / ".git" / "rebase-apply").is_dir() else []),
        "resume": {"command": f"/spec-status {args.spec_id}", "preconditions": []},
    }
    # The expectation is written immediately before the sole authoritative
    # replace: a crash here fails closed as expected-without-record.
    raw = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    markdown = "# Takeover record\n\ngeneration: %s\n\n```json\n%s```\n" % (created, json.dumps(record, indent=2, sort_keys=True))
    digest = hashlib.sha256("\0".join(dirty).encode("utf-8", "surrogateescape")).hexdigest()
    with takeover_directory(store_path) as directory_fd:
        # Validate and commit the authoritative record before expectation. A
        # crash after JSON therefore remains observable as stale Markdown.
        validate_final(directory_fd, f"{args.run_id}.json")
        validate_final(directory_fd, f"{args.run_id}.md")
        replace_bytes(directory_fd, f"{args.run_id}.json", raw)
        if os.environ.get("TAKEOVER_FAULT_AFTER_JSON") == "1":
            return 75
        replace_bytes(directory_fd, f"{args.run_id}.md", markdown.encode())
    subprocess.check_call([sys.executable, args.gates, "takeover-expect", args.run_id,
                           "--created-at", str(created), "--dirty-digest", digest], env=env)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, UnsafeTakeoverPath, subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)
