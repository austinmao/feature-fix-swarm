#!/usr/bin/env python3
"""queue-journal.py — versioned land-queue event journal and queue lock.

The journal is the queue's durable recovery evidence: intent is appended
BEFORE every external effect and the observed result afterwards, so a crashed
queue can be reconciled against merge authority instead of guessed at.

Durability follows the phase-01 consume discipline (binding 03d8a75d): the
replacement file is fsynced, renamed over the journal, and the parent
directory descriptor is fsynced — an append is durable only once the
directory fsync returns.

The queue lock reuses the phase-01 OwnerLock primitive (binding 71193d25)
published at ``<store>/land-queue/queue.lock``.  The payload pid is the
RUNNER's pid, so liveness judgment covers the whole shell run: a dead runner
is reclaimable, a live one refuses contenders with QUEUE-REFUSED:queue-live.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import stat
import sys
import time
from pathlib import Path

SCHEMA = 1
MAX_FIELD = 4096
MAX_JOURNAL_BYTES = 4 * 1024 * 1024
KINDS = frozenset({"intent", "result", "terminal"})
# Closed action vocabulary (REQ-213): every journaled step names one of the
# queue's enumerated lifecycle actions — nothing free-form is ever persisted.
STEPS = frozenset({"collect", "precheck", "rebase", "implement", "push",
                   "review", "ci", "precheck-merge", "grant", "merge",
                   "finalize", "terminal"})
PR_RE = re.compile(r"^[0-9]{1,9}$")
OID_RE = re.compile(r"^[0-9a-f]{40}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_IO = None


class JournalError(RuntimeError):
    pass


def _io():
    """Path-load the phase-01 takeover-io primitives (OwnerLock and friends)."""
    global _IO
    if _IO is None:
        path = Path(__file__).resolve().parents[3] / "scripts" / "gsd" / "takeover-io.py"
        spec = importlib.util.spec_from_file_location("takeover_io", path)
        if spec is None or spec.loader is None:
            raise JournalError("QUEUE-ERROR:store takeover-io primitive unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("takeover_io", module)
        spec.loader.exec_module(module)
        _IO = module
    return _IO


def _store_dir(store: str) -> Path:
    path = Path(store)
    try:
        st = os.lstat(path)
    except OSError:
        raise JournalError(f"QUEUE-ERROR:store unsafe store directory {store}")
    # retro_state-patterned trust checks: a real directory, owned by us (or
    # root), never a symlink, never group/world-writable.
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise JournalError(f"QUEUE-ERROR:store unsafe store directory {store}")
    if st.st_uid not in (os.getuid(), 0) or st.st_mode & 0o022:
        raise JournalError(f"QUEUE-ERROR:store untrusted store directory {store}")
    return path


def _doc_path(store: str, queue_id: str) -> Path:
    if not NAME_RE.match(queue_id or ""):
        raise JournalError("QUEUE-ERROR:store invalid queue id")
    return _store_dir(store) / f"{queue_id}.json"


def _load(path: Path) -> dict:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise JournalError(f"QUEUE-ERROR:store journal missing: {path.name}")
    except OSError as exc:
        raise JournalError(f"QUEUE-ERROR:store unreadable journal: {exc}")
    if stat.S_ISLNK(st.st_mode):
        raise JournalError(f"QUEUE-ERROR:store symlinked journal: {path.name}")
    if not stat.S_ISREG(st.st_mode):
        raise JournalError(f"QUEUE-ERROR:store non-regular journal: {path.name}")
    if st.st_uid != os.getuid():
        raise JournalError(f"QUEUE-ERROR:store foreign-owned journal: {path.name}")
    if st.st_size > MAX_JOURNAL_BYTES:
        raise JournalError("QUEUE-ERROR:store oversized journal")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise JournalError(f"QUEUE-ERROR:store unreadable journal: {exc}")
    try:
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise JournalError(
                    f"QUEUE-ERROR:store non-regular journal: {path.name}")
            raw = handle.read(MAX_JOURNAL_BYTES + 1)
    except OSError as exc:
        raise JournalError(f"QUEUE-ERROR:store unreadable journal: {exc}")
    if len(raw) > MAX_JOURNAL_BYTES:
        raise JournalError("QUEUE-ERROR:store oversized journal")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise JournalError("QUEUE-ERROR:store corrupt journal")
    if (not isinstance(doc, dict) or doc.get("schema") != SCHEMA
            or not isinstance(doc.get("events"), list)):
        raise JournalError("QUEUE-ERROR:store invalid journal shape")
    return doc


def _save(path: Path, doc: dict) -> None:
    directory = path.parent
    tmp = directory / f".{path.name}.{os.getpid()}.{time.time_ns()}"
    data = json.dumps(doc, sort_keys=True).encode()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # 03d8a75d: fsync file, rename, then fsync the parent directory fd.
    dfd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _field(value, name: str) -> str:
    text = "" if value is None else str(value)
    if len(text) > MAX_FIELD or any(ch in text for ch in ("\0", "\r", "\n")):
        raise JournalError(f"QUEUE-ERROR:store hostile or oversized {name} field")
    return text


def _queue_lock(io, directory_fd: int, run_id: str, pid: int):
    class QueueLock(io.OwnerLock):
        name = "queue.lock"

        def _payload(self) -> bytes:
            try:
                identity = io.process_identity(pid)
                start = identity.pid_start_time or "unknown"
                boot = identity.boot_session_id or io.boot_session_id()
            except io.UnsafeTakeoverPath:
                start = "unknown"
                boot = io.boot_session_id()
            return json.dumps({"pid": pid, "pid_start_time": start,
                               "boot_session_id": boot,
                               "claimed_at": int(time.time()),
                               "run_id": self.run_id},
                              separators=(",", ":")).encode()

    return QueueLock(directory_fd, run_id)


def cmd_init(ns) -> int:
    path = _doc_path(ns.store, ns.queue_id)
    if path.exists():
        raise JournalError(f"QUEUE-ERROR:store journal already exists: {path.name}")
    _save(path, {"schema": SCHEMA, "queue_id": ns.queue_id,
                 "run_id": _field(ns.run_id, "run_id"),
                 "created_at": int(time.time()), "events": []})
    return 0


def cmd_append(ns) -> int:
    if ns.kind not in KINDS:
        raise JournalError("QUEUE-ERROR:store invalid event kind")
    if ns.step not in STEPS:
        raise JournalError("QUEUE-ERROR:store unknown step action")
    path = _doc_path(ns.store, ns.queue_id)
    doc = _load(path)
    event = {"seq": len(doc["events"]) + 1, "ts": int(time.time()),
             "kind": ns.kind, "step": _field(ns.step, "step")}
    if ns.item is not None:
        event["item"] = _field(ns.item, "item")
    if ns.status is not None:
        event["status"] = _field(ns.status, "status")
    if ns.detail is not None:
        event["detail"] = _field(ns.detail, "detail")
    # REQ-213 / 8c88ebfa: the effect's idempotency key (PR number + head OID)
    # lives in the intent itself, validated closed, before the effect runs.
    if ns.pr is not None or ns.head is not None:
        if ns.pr is None or ns.head is None:
            raise JournalError(
                "QUEUE-ERROR:store idempotency key requires both --pr and --head")
        if not PR_RE.match(ns.pr):
            raise JournalError("QUEUE-ERROR:store malformed pr number")
        if not OID_RE.match(ns.head):
            raise JournalError("QUEUE-ERROR:store malformed head oid")
        event["pr"] = int(ns.pr)
        event["head"] = ns.head
        event["key"] = f"pr-{ns.pr}@{ns.head}"
    if ns.kind == "intent" and ns.step == "merge" and "key" not in event:
        raise JournalError(
            "QUEUE-ERROR:store merge intent requires an idempotency key")
    doc["events"].append(event)
    _save(path, doc)
    return 0


def cmd_events(ns) -> int:
    doc = _load(_doc_path(ns.store, ns.queue_id))
    print(json.dumps(doc, sort_keys=True))
    return 0


def cmd_read_dangling(ns) -> int:
    """NUL-emit item, step, pr, head for each intent lacking its result.

    Startup recovery (01-08 two-phase intent): the runner resolves each
    record against merge authority by its idempotency key and never
    re-executes an effect whose key is already satisfied.
    """
    doc = _load(_doc_path(ns.store, ns.queue_id))
    events = doc["events"]
    resolved = {(e.get("item"), e.get("step"))
                for e in events if e.get("kind") == "result"}
    terminal_items = {e.get("item")
                      for e in events if e.get("kind") == "terminal"}
    for event in events:
        if event.get("kind") != "intent" or not event.get("item"):
            continue
        if (event.get("item"), event.get("step")) in resolved:
            continue
        if event.get("item") in terminal_items:
            continue
        for value in (event.get("item", ""), event.get("step", ""),
                      event.get("pr", ""), event.get("head", "")):
            sys.stdout.write(str(value) + "\0")
    return 0


def cmd_read_terminals(ns) -> int:
    doc = _load(_doc_path(ns.store, ns.queue_id))
    for event in doc["events"]:
        if event.get("kind") != "terminal":
            continue
        for field in ("item", "status", "detail"):
            sys.stdout.write(str(event.get(field, "")) + "\0")
    return 0


def cmd_lock_acquire(ns) -> int:
    io = _io()
    store = _store_dir(ns.store)
    directory_fd = os.open(store, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                           | getattr(os, "O_NOFOLLOW", 0))
    try:
        lock = _queue_lock(io, directory_fd, ns.run_id, ns.pid)
        try:
            lock.acquire()
        except io.LockBusy:
            print("QUEUE-REFUSED:queue-live")
            return 75
    finally:
        os.close(directory_fd)
    return 0


def cmd_lock_release(ns) -> int:
    lock_path = _store_dir(ns.store) / "queue.lock"
    try:
        payload = json.loads(lock_path.read_text())
    except FileNotFoundError:
        return 0  # already released
    except (OSError, ValueError):
        print("LOCK-RELEASE-REFUSED:unreadable", file=sys.stderr)
        return 77
    if not isinstance(payload, dict) or payload.get("pid") != ns.pid:
        print("LOCK-RELEASE-REFUSED:not-owner", file=sys.stderr)
        return 77
    lock_path.unlink()
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--contract-probe"]:
        return 0
    parser = argparse.ArgumentParser(prog="queue-journal.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("init", "append", "events", "read-terminals", "read-dangling"):
        p = sub.add_parser(name)
        p.add_argument("--store", required=True)
        p.add_argument("--queue-id", required=True)
        if name == "init":
            p.add_argument("--run-id", required=True)
        if name == "append":
            p.add_argument("--kind", required=True)
            p.add_argument("--step", required=True)
            p.add_argument("--item")
            p.add_argument("--status")
            p.add_argument("--detail")
            p.add_argument("--pr")
            p.add_argument("--head")
    for name in ("lock-acquire", "lock-release"):
        p = sub.add_parser(name)
        p.add_argument("--store", required=True)
        p.add_argument("--pid", required=True, type=int)
        if name == "lock-acquire":
            p.add_argument("--run-id", required=True)

    ns = parser.parse_args(argv)
    handlers = {"init": cmd_init, "append": cmd_append, "events": cmd_events,
                "read-terminals": cmd_read_terminals,
                "read-dangling": cmd_read_dangling,
                "lock-acquire": cmd_lock_acquire, "lock-release": cmd_lock_release}
    try:
        return handlers[ns.cmd](ns)
    except JournalError as exc:
        print(str(exc), file=sys.stderr)
        return 70
    except OSError as exc:
        print(f"QUEUE-ERROR:store {exc}", file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
