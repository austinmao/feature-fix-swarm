#!/usr/bin/env python3
"""Write the versioned takeover record from typed, local facts only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def refuse(message: str) -> None:
    raise RuntimeError(f"TAKEOVER-WRITER-REFUSED: {message}")


def regular_or_absent(path: Path) -> None:
    try:
        row = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(row.st_mode) or not stat.S_ISREG(row.st_mode):
        refuse(f"unsafe output path: {path.name}")


def atomic_write(directory: Path, name: str, payload: bytes) -> None:
    regular_or_absent(directory / name)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=directory)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        regular_or_absent(directory / name)  # reject swap before replacement
        os.replace(tmp, directory / name)
        os.chmod(directory / name, 0o600)
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if tmp.exists():
            tmp.unlink()


def git(*args: str) -> str:
    done = subprocess.run(["git", *args], text=True, capture_output=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gates", required=True)
    ap.add_argument("--spec-id", required=True)
    ap.add_argument("--run-id", required=True)
    args = ap.parse_args()
    env = {key: value for key, value in os.environ.items() if key != "GATES_STORE"}
    store_dir = subprocess.check_output([sys.executable, args.gates, "store-dir"], text=True, env=env).strip()
    store_path = subprocess.check_output([sys.executable, args.gates, "store-path"], text=True, env=env).strip()
    state = json.loads(subprocess.check_output([sys.executable, args.gates, "takeover-state", args.run_id], text=True, env=env))
    directory = Path(store_dir) / "takeover"
    if directory.exists() and directory.is_symlink():
        refuse("takeover directory is a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not directory.is_dir() or directory.is_symlink():
        refuse("takeover directory is unsafe")
    created = int(time.time())
    record = {
        "schema_version": 1,
        "created_at": created,
        "ids": {"spec_id": args.spec_id, "run_id": args.run_id},
        "gates_store": str(Path(store_path).resolve(strict=False)),
        "gates_store_anchor": hashlib.sha256(str(Path(store_path).resolve(strict=False)).encode()).hexdigest(),
        "git_state": {"branch": git("branch", "--show-current"), "head": git("rev-parse", "HEAD"), "upstream": git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"), "dirty": bool(git("status", "--porcelain"))},
        "preflight": state.get("preflight", {}), "grants": state.get("grants", []),
        "pendings": state.get("pendings", []), "promotions": state.get("promotions", []),
        "runner": {}, "unresolved_findings": state.get("unresolved_findings", []),
        "phases": [], "evidence": [], "forbid": [],
        "resume": {"command": "", "preconditions": []},
    }
    # The expectation is written immediately before the sole authoritative
    # replace: a crash here fails closed as expected-without-record.
    subprocess.check_call([sys.executable, args.gates, "takeover-expect", args.run_id], env=env)
    raw = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    atomic_write(directory, f"{args.run_id}.json", raw)
    markdown = "# Takeover record\n\ngeneration: %s\n\n```json\n%s```\n" % (created, json.dumps(record, indent=2, sort_keys=True))
    if os.environ.get("TAKEOVER_FAULT_AFTER_JSON") == "1":
        return 75
    atomic_write(directory, f"{args.run_id}.md", markdown.encode())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)
