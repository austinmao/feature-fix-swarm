#!/usr/bin/env python3
"""Write the versioned takeover record from typed, local facts only."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
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
process_identity = _IO.process_identity
process_liveness = _IO.process_liveness
takeover_directory = _IO.takeover_directory
validate_final = _IO.validate_final

MAX_EVIDENCE_ROWS = 256


def refuse(message: str) -> None:
    raise RuntimeError(f"TAKEOVER-WRITER-REFUSED: {message}")


def checked_git(*args: str, text: bool = True) -> str | bytes:
    done = subprocess.run(["git", *args], text=text, capture_output=True, check=False)
    if done.returncode:
        refuse(f"mandatory git probe failed: {' '.join(args)}")
    return done.stdout.strip() if text else done.stdout


def dirty_entries() -> list[str]:
    raw = checked_git("status", "--porcelain=v2", "-z", "--untracked-files=all", text=False)
    assert isinstance(raw, bytes)
    return sorted(row.decode("utf-8", "surrogateescape") for row in raw.split(b"\0") if row)


def runner_snapshot(root: Path) -> dict:
    state = root / ".planning" / "run-state"
    status = (state / "gsd-run.status").read_text(errors="replace").strip() if (state / "gsd-run.status").is_file() else "unknown"
    pid_text = (state / "gsd-run.pid").read_text(errors="replace").strip() if (state / "gsd-run.pid").is_file() else ""
    try:
        pid = int(pid_text) if pid_text else None
    except ValueError:
        pid = 0
    identity = process_identity(pid)
    return {"status": status, "pid": identity.pid, "live": process_liveness(identity),
            "process_state": identity.process_state,
            "pid_start_time": identity.pid_start_time,
            "boot_session_id": identity.boot_session_id}


def phase_snapshot(root: Path) -> list[dict]:
    phases = root / ".planning" / "phases"
    rows: list[dict] = []
    if phases.is_dir():
        for path in sorted(phases.rglob("*-PLAN.md")):
            rel = path.relative_to(root).as_posix()
            rows.append({"plan": rel, "summary": path.with_name(path.name.replace("-PLAN.md", "-SUMMARY.md")).relative_to(root).as_posix(), "complete": path.with_name(path.name.replace("-PLAN.md", "-SUMMARY.md")).is_file()})
    return rows


def evidence_snapshot(root: Path, spec_id: str) -> list[str]:
    """Return bounded regular evidence rows from the uniquely selected spec.

    Evidence is display and handoff metadata, never an instruction source.  Do
    not follow symlinks while walking the repository-owned directory.
    """
    matches = [path for path in (root / "specs").glob(f"{spec_id}-*")
               if path.is_dir() and not path.is_symlink()]
    if len(matches) != 1:
        return []
    evidence = matches[0] / "evidence"
    if not evidence.is_dir() or evidence.is_symlink():
        return []
    rows: list[str] = []
    for base, dirs, files in os.walk(evidence, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not (Path(base) / name).is_symlink())
        for name in sorted(files):
            path = Path(base) / name
            if path.is_symlink() or not path.is_file():
                continue
            rows.append(path.relative_to(root).as_posix())
            if len(rows) >= MAX_EVIDENCE_ROWS:
                return sorted(rows)
    return sorted(rows)


def resume_preconditions(run_id: str, store_path: str, head: str) -> list[dict[str, str]]:
    canonical_store = str(Path(store_path).resolve(strict=False))
    return [
        {"kind": "run_id", "value": run_id},
        {"kind": "gates_store", "value": canonical_store},
        {"kind": "git_head", "value": head},
    ]


def git_state() -> tuple[Path, str, str, str, list[str], bool]:
    root = Path(str(checked_git("rev-parse", "--show-toplevel")))
    branch = str(checked_git("branch", "--show-current"))
    head = str(checked_git("rev-parse", "HEAD^{commit}"))
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", head):
        refuse("invalid HEAD identity")
    # A detached HEAD is legitimate only after its exact object identity was
    # checked above.  Upstream absence is a typed absence, not a probe error.
    probe = subprocess.run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], text=True, capture_output=True)
    if probe.returncode:
        configured = subprocess.run(["git", "config", "--get", f"branch.{branch}.remote"], text=True, capture_output=True)
        if configured.returncode == 1:
            upstream = ""
        else:
            refuse("mandatory git upstream probe failed")
    else:
        upstream = probe.stdout.strip()
    rebase_merge = str(checked_git("rev-parse", "--git-path", "rebase-merge"))
    rebase_apply = str(checked_git("rev-parse", "--git-path", "rebase-apply"))
    return root, branch, head, upstream, dirty_entries(), Path(rebase_merge).is_dir() or Path(rebase_apply).is_dir()


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
    root, branch, head, upstream, dirty, mid_rebase = git_state()
    created = int(time.time())
    record = {
        "schema_version": 1,
        "created_at": created,
        "ids": {"spec_id": args.spec_id, "run_id": args.run_id},
        "gates_store": str(Path(store_path).resolve(strict=False)),
        "gates_store_anchor": hashlib.sha256(str(Path(store_path).resolve(strict=False)).encode()).hexdigest(),
        "git_state": {"branch": branch, "head": head, "upstream": upstream, "dirty": dirty},
        "preflight": state.get("preflight", {}), "grants": state.get("grants", []),
        "pendings": state.get("pendings", []), "promotions": state.get("promotions", []),
        "runner": runner_snapshot(root), "unresolved_findings": state.get("unresolved_findings", []),
        "phases": phase_snapshot(root), "evidence": evidence_snapshot(root, args.spec_id),
        "forbid": ([{"action": "mid-rebase", "probe": "mid-rebase", "reason": "git rebase is in progress"}]
                   if mid_rebase else []),
        "resume": {"command": f"/spec-status {args.spec_id}",
                   "preconditions": resume_preconditions(args.run_id, store_path, head)},
    }
    # The expectation is written immediately before the sole authoritative
    # replace: a crash here fails closed as expected-without-record.
    raw = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    markdown = "# Takeover record\n\ngeneration: %s\n\n```json\n%s```\n" % (created, json.dumps(record, indent=2, sort_keys=True))
    digest = hashlib.sha256("\0".join(dirty).encode("utf-8", "surrogateescape")).hexdigest()
    with takeover_directory(store_path) as directory_fd:
        # Validate both siblings before the expectation mutation. A crash after
        # the authoritative JSON replace is then fail-closed and the old
        # Markdown remains detectably stale by its generation stamp.
        validate_final(directory_fd, f"{args.run_id}.json")
        validate_final(directory_fd, f"{args.run_id}.md")
        subprocess.check_call([sys.executable, args.gates, "takeover-expect", args.run_id,
                               "--created-at", str(created), "--dirty-digest", digest], env=env)
        replace_bytes(directory_fd, f"{args.run_id}.json", raw)
        if os.environ.get("TAKEOVER_FAULT_AFTER_JSON") == "1":
            return 75
        replace_bytes(directory_fd, f"{args.run_id}.md", markdown.encode())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, UnsafeTakeoverPath, subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)
