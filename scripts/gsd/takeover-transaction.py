#!/usr/bin/env python3
"""Descriptor bootstrap for the takeover wall; this contains no wall policy."""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location("takeover_io", Path(__file__).with_name("takeover-io.py"))
assert _SPEC and _SPEC.loader
_IO = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _IO
_SPEC.loader.exec_module(_IO)


def inheritable(fd: int) -> str:
    os.set_inheritable(fd, True)
    return str(fd)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True)
    ap.add_argument("--store", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("args", nargs=argparse.REMAINDER)
    ns = ap.parse_args()
    if not ns.run_id.startswith("spec-") or "/" in ns.run_id:
        return 1
    store_fd = evidence_fd = takeover_fd = record_fd = None
    owner_lock = None
    try:
        if not os.path.isdir(os.path.dirname(ns.store)):
            env = dict(os.environ)
            env["TAKEOVER_TRANSACTION"] = "1"
            wall_args = ns.args[1:] if ns.args[:1] == ["--"] else ns.args
            os.execve("/bin/bash", ["bash", ns.script, *wall_args], env)
        store_fd = _IO.open_store_directory(ns.store)
        try:
            evidence_fd = _IO.open_regular(store_fd, "evidence.json")
        except FileNotFoundError:
            evidence_fd = None
        takeover_fd = _IO.open_takeover(store_fd)
        record_name = f"{ns.run_id}.json"
        if takeover_fd is not None:
            try:
                record_fd = _IO.open_regular(takeover_fd, record_name)
            except FileNotFoundError:
                record_fd = None
        env = dict(os.environ)
        env["TAKEOVER_TRANSACTION"] = "1"
        if evidence_fd is not None:
            env.update({"TAKEOVER_STORE_DIR_FD": inheritable(store_fd),
                        "TAKEOVER_EVIDENCE_FD": inheritable(evidence_fd)})
        if takeover_fd is not None:
            env["TAKEOVER_DIR_FD"] = inheritable(takeover_fd)
        if record_fd is not None:
            owner_lock = _IO.OwnerLock(store_fd, ns.run_id)
            owner_lock.acquire()
            env["TAKEOVER_RECORD_FD"] = inheritable(record_fd)
            env["TAKEOVER_RECORD_NAME"] = record_name
            env["TAKEOVER_LOCK_HELD"] = "1"
            env["TAKEOVER_LOCK_DEV"] = str(owner_lock.identity[0])
            env["TAKEOVER_LOCK_INO"] = str(owner_lock.identity[1])
        wall_args = ns.args[1:] if ns.args[:1] == ["--"] else ns.args
        os.execve("/bin/bash", ["bash", ns.script, *wall_args], env)
    except _IO.LockBusy:
        print("TAKEOVER-REFUSED:runner-live\nUnblock (operator): bash scripts/gsd/takeover-check.sh --run-id " + ns.run_id)
        return 1
    except (_IO.UnsafeTakeoverPath, OSError):
        # The shell cannot regain control after an exec bootstrap failure.
        # Preserve the wall's fail-closed operator grammar here.
        print("TAKEOVER-REFUSED:record-mismatch\nUnblock (operator): /spec-status")
        return 1
    finally:
        # exec never returns; close only on rejection.
        for fd in (record_fd, takeover_fd, evidence_fd, store_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
