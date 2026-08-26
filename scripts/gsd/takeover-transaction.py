#!/usr/bin/env python3
"""Descriptor bootstrap for the takeover wall; this contains no wall policy."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import signal
import stat
import sys
import tempfile
import time
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location("takeover_io", Path(__file__).with_name("takeover-io.py"))
assert _SPEC and _SPEC.loader
_IO = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _IO
_SPEC.loader.exec_module(_IO)

_GATES_SPEC = importlib.util.spec_from_file_location(
    "takeover_gates", Path(__file__).resolve().parents[2] / "lib" / "gates.py")
assert _GATES_SPEC and _GATES_SPEC.loader
_GATES = importlib.util.module_from_spec(_GATES_SPEC)
sys.modules[_GATES_SPEC.name] = _GATES
_GATES_SPEC.loader.exec_module(_GATES)


def inheritable(fd: int) -> str:
    os.set_inheritable(fd, True)
    return str(fd)


def _read_log(kind: str) -> None:
    log = os.environ.get("TAKEOVER_READ_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(kind + "\n")


def recover_takeover_transaction(store: str, store_dir_fd: int, takeover_dir_fd: int,
                                 run_id: str) -> dict:
    """Pre-absence durable-intent discovery and bounded recovery dispatch.

    Runs after canonical store/takeover binding and BEFORE record discovery
    can choose the no-record fast path, so an unreconciled transaction can
    never be misread as absence (TAKEOVER-NONE) or missing-record.
    """
    return _GATES.recover_takeover_transaction(store, store_dir_fd, takeover_dir_fd, run_id)


def _record_binds(record_fd: int, store: str, run_id: str) -> bool:
    """Validate hostile record claims before any evidence bytes are read."""
    st = os.fstat(record_fd)
    if not stat.S_ISREG(st.st_mode) or st.st_size > 1024 * 1024:
        return False
    os.lseek(record_fd, 0, os.SEEK_SET)
    # Full-read loop to the exact fstat size: a short kernel read must never
    # be judged as a binding failure or truncated claim data.
    chunks: list[bytes] = []
    total = 0
    while total < st.st_size:
        chunk = os.read(record_fd, min(65536, st.st_size - total))
        if not chunk:
            return False
        chunks.append(chunk)
        total += len(chunk)
    if os.read(record_fd, 1):
        return False
    raw = b"".join(chunks)
    try:
        row = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (isinstance(row, dict) and row.get("ids", {}).get("run_id") == run_id
            and row.get("gates_store") == store
            and row.get("gates_store_anchor") == hashlib.sha256(store.encode()).hexdigest())


def _snapshot_bytes(raw: bytes) -> tuple[int, str]:
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("non-object evidence")
    snap_fd, snap_name = tempfile.mkstemp(prefix=".takeover-snapshot-", dir="/tmp")
    try:
        os.fchmod(snap_fd, 0o400)
        # Same full-write discipline as replace_bytes/OwnerLock: the immutable
        # snapshot holds the complete evidence bytes and zero progress is a
        # hard failure, never a truncated authority snapshot.
        view = memoryview(raw)
        while view:
            written = os.write(snap_fd, view)
            if written <= 0:
                raise OSError("short write made no progress")
            view = view[written:]
        os.fsync(snap_fd)
        readonly = os.open(snap_name, os.O_RDONLY)
    finally:
        os.close(snap_fd)
        os.unlink(snap_name)
    return readonly, hashlib.sha256(raw).hexdigest()


def _capture_snapshot(evidence_fd: int) -> tuple[int, str]:
    """Capture immutable evidence bytes, rejecting every torn read."""
    for _ in range(3):
        before = os.fstat(evidence_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 1024 * 1024:
            raise ValueError("invalid evidence")
        os.lseek(evidence_fd, 0, os.SEEK_SET)
        raw = os.read(evidence_fd, before.st_size)
        after = os.fstat(evidence_fd)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                and len(raw) == before.st_size):
            return _snapshot_bytes(raw)
    raise ValueError("torn evidence")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True)
    ap.add_argument("--store", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("args", nargs=argparse.REMAINDER)
    ns = ap.parse_args()
    # List mode carries no run id; a present run id must still be exact.
    if ns.run_id and (not ns.run_id.startswith("spec-") or "/" in ns.run_id):
        return 1
    store_fd = evidence_fd = takeover_fd = record_fd = snapshot_fd = None
    owner_lock = None
    try:
        if not os.path.isdir(os.path.dirname(ns.store)):
            # A missing canonical store is the explicit no-record case.  Make
            # only its parent so the descriptor bootstrap can pass an empty,
            # immutable authority snapshot to the wall.
            os.makedirs(os.path.dirname(ns.store), mode=0o700, exist_ok=True)
        store_fd = _IO.open_store_directory(ns.store)
        try:
            evidence_fd = _IO.open_regular(store_fd, "evidence.json")
        except FileNotFoundError:
            evidence_fd = None
        takeover_fd = _IO.open_takeover(store_fd)
        record_name = f"{ns.run_id}.json"
        if takeover_fd is not None and ns.run_id:
            recovery = recover_takeover_transaction(ns.store, store_fd, takeover_fd, ns.run_id)
            outcome = recovery.get("outcome")
            if outcome == "recovered-success":
                # Recovery emitted TAKEOVER-OK for the completed transaction.
                return 0
            if outcome == "superseded":
                # Recovery emitted the typed TAKEOVER-SUPERSEDED result.
                return 1
            if outcome in ("locked-out", "unexplained"):
                print("TAKEOVER-REFUSED:record-mismatch\nUnblock (operator): /spec-status")
                return 1
        if takeover_fd is not None and ns.run_id:
            try:
                record_fd = _IO.open_regular(takeover_fd, record_name)
            except FileNotFoundError:
                record_fd = None
        if record_fd is not None and not _record_binds(record_fd, ns.store, ns.run_id):
            # A present record that fails binding is the decoy case; keep the
            # wall's exact operator grammar for it (never record-mismatch).
            print("TAKEOVER-REFUSED:decoy-store\nUnblock (operator): /spec-status")
            return 1
        if evidence_fd is not None:
            snapshot_fd, digest = _capture_snapshot(evidence_fd)
            _read_log("pre-decision")
        elif record_fd is not None:
            raise ValueError("record without authority")
        else:
            snapshot_fd, digest = _snapshot_bytes(b"{}")
        env = dict(os.environ)
        env["TAKEOVER_TRANSACTION"] = "1"
        if evidence_fd is not None:
            env.update({"TAKEOVER_STORE_DIR_FD": inheritable(store_fd),
                        "TAKEOVER_EVIDENCE_FD": inheritable(evidence_fd)})
        env["TAKEOVER_SNAPSHOT_FD"] = inheritable(snapshot_fd)
        env["TAKEOVER_SNAPSHOT_SHA256"] = digest
        if takeover_fd is not None:
            env["TAKEOVER_DIR_FD"] = inheritable(takeover_fd)
        if record_fd is not None:
            owner_lock = _IO.OwnerLock(store_fd, ns.run_id)
            owner_lock.acquire()

            def _signal_cleanup(signum, frame):
                owner_lock.cleanup()
                # Conventional signal exit: cleanup then terminate, never
                # continued execution (01-VERIFICATION gap).
                sys.exit(128 + signum)

            for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, _signal_cleanup)
            pause = os.environ.get("TAKEOVER_TEST_PAUSE_AFTER_LOCK")
            if pause:
                open(os.path.join(pause, "ready"), "w").close()
                deadline = time.monotonic() + 30
                while (not os.path.exists(os.path.join(pause, "release"))
                       and time.monotonic() < deadline):
                    time.sleep(0.05)
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
    except (_IO.UnsafeTakeoverPath, OSError, ValueError, json.JSONDecodeError):
        # The shell cannot regain control after an exec bootstrap failure.
        # Preserve the wall's fail-closed operator grammar here.
        print("TAKEOVER-REFUSED:record-mismatch\nUnblock (operator): /spec-status")
        return 1
    finally:
        # exec never returns; close only on rejection.
        for fd in (snapshot_fd, record_fd, takeover_fd, evidence_fd, store_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
