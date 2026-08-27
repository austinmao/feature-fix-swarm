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
import subprocess
import sys
import time
from pathlib import Path

SCHEMA = 1
# CR-02 (spec-006 phase 3, round 2): the ABSOLUTE queue deadline is recorded
# at init as created_at + QUEUE_WALL_SECONDS and is the ONLY source of grant
# lifetime — matching queue-guard.sh QUEUE_WALL_SECONDS, never a caller
# value.  Additive schema note: SCHEMA stays 1; `deadline` is a new optional
# field, so journals written before this change remain readable (readers
# treat a missing deadline as fail-closed at the grant-mint boundary).
QUEUE_WALL_SECONDS = 28800
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
    deadline = doc.get("deadline")
    if deadline is not None and (isinstance(deadline, bool)
                                 or not isinstance(deadline, (int, float))):
        raise JournalError("QUEUE-ERROR:store invalid journal deadline")
    for key in ("repo_root", "base"):
        value = doc.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise JournalError(f"QUEUE-ERROR:store invalid journal {key}")
    manifest = doc.get("manifest")
    if manifest is not None:
        if (not isinstance(manifest, list)
                or any(not isinstance(m, str) or not m for m in manifest)
                or len(set(manifest)) != len(manifest)):
            raise JournalError("QUEUE-ERROR:store invalid journal manifest")
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


def effective_terminal_items(events) -> set:
    """CR-03 (round 3): sequence-ordered lifecycle projection.

    An item is terminal only when its LATEST lifecycle-relevant event is a
    terminal — a later intent starts a new attempt and REOPENS the item, so
    a historical quarantine terminal (zero posture writes BLOCKED:conflict,
    then retries after the base advances) can never hide a crashed retry
    from resume, grant derivation, or reporting.  Result events resolve
    intents and never open attempts, so they are ignored here.

    Additive schema note: SCHEMA stays 1 and no field changes — this is a
    purely behavioral projection rule, so journals written before this
    change remain readable and simply project through the same rule.
    """
    last = {}
    for event in events:
        item = event.get("item")
        if not item:
            continue
        kind = event.get("kind")
        if kind == "terminal":
            last[item] = True
        elif kind == "intent":
            last.pop(item, None)
    return set(last)


def _last_terminal_index(events) -> dict:
    """Index of each item's LAST terminal event (LOW-3, round 4).

    Events at or before that index belong to a CLOSED attempt: its results
    must never satisfy a reopened attempt's identically-named step, and its
    intents/keys are already accounted by the terminal — otherwise a
    crashed attempt-2 finalize looks resolved and resume never reconciles
    it against merge authority.
    """
    last = {}
    for idx, event in enumerate(events):
        if event.get("kind") == "terminal" and event.get("item"):
            last[event["item"]] = idx
    return last


def canonical_repo_root(repo: str) -> str:
    """Physical git toplevel of `repo`, realpath-canonicalized (CR-04)."""
    proc = subprocess.run(["git", "-C", repo, "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise JournalError(f"QUEUE-ERROR:store --repo is not a git repository: {repo}")
    return os.path.realpath(proc.stdout.strip())


def _valid_base(base: str) -> str:
    base = _field(base, "base")
    if not base or any(ch.isspace() for ch in base):
        raise JournalError("QUEUE-ERROR:store invalid base branch name")
    return base


def cmd_init(ns) -> int:
    path = _doc_path(ns.store, ns.queue_id)
    if path.exists():
        raise JournalError(f"QUEUE-ERROR:store journal already exists: {path.name}")
    created = int(time.time())
    doc = {"schema": SCHEMA, "queue_id": ns.queue_id,
           "run_id": _field(ns.run_id, "run_id"),
           "created_at": created,
           # CR-02: the immutable absolute deadline — grant lifetime
           # derives ONLY from this field, never from a caller timeout.
           "deadline": created + QUEUE_WALL_SECONDS,
           "events": []}
    # CR-04: canonicalize and persist the creating repository root + base.
    # Both-or-neither: a bound journal is the only journal grant-consolidate
    # will mint from (unbound journals stay readable but fail closed at the
    # mint boundary).
    if (ns.repo is None) != (ns.base is None):
        raise JournalError(
            "QUEUE-ERROR:store init requires --repo and --base together")
    if ns.repo is not None:
        doc["repo_root"] = canonical_repo_root(ns.repo)
        doc["base"] = _valid_base(ns.base)
    _save(path, doc)
    return 0


def cmd_record_manifest(ns) -> int:
    """Persist the complete validated intake manifest (CR-03) — atomic,
    immutable, recorded after collection and BEFORE the first item effect.
    Identical replays are idempotent; a differing list refuses: the intake
    truth is written once.  Additive schema note: SCHEMA stays 1 and
    `manifest` is optional at read, so pre-manifest journals stay readable
    (they fail closed at the grant-mint boundary instead)."""
    items = list(ns.item or [])
    seen = set()
    for item in items:
        value = _field(item, "manifest item")
        if not value or any(ch.isspace() for ch in value):
            raise JournalError("QUEUE-ERROR:store invalid manifest item")
        if value in seen:
            raise JournalError("QUEUE-ERROR:store duplicate manifest item")
        seen.add(value)
    path = _doc_path(ns.store, ns.queue_id)
    doc = _load(path)
    existing = doc.get("manifest")
    if existing is not None:
        if existing == items:
            return 0  # idempotent replay
        raise JournalError(
            "QUEUE-ERROR:store manifest already recorded and differs; the "
            "intake truth is immutable")
    doc["manifest"] = items
    _save(path, doc)
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
    # REQ-214: non-LANDED terminals carry a separate reason and a
    # one-command unblock; both validate through the same closed field rule.
    if ns.reason is not None:
        event["reason"] = _field(ns.reason, "reason")
    if ns.unblock is not None:
        event["unblock"] = _field(ns.unblock, "unblock")
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
    # LOW-3 (round 4): results and intents are ATTEMPT-scoped — only
    # events after the item's last terminal participate.
    last_term = _last_terminal_index(events)
    resolved = {(e.get("item"), e.get("step"))
                for idx, e in enumerate(events)
                if e.get("kind") == "result"
                and idx > last_term.get(e.get("item"), -1)}
    # CR-03 (round 3): sequence-ordered — a later intent reopens the item.
    terminal_items = effective_terminal_items(events)
    for idx, event in enumerate(events):
        if event.get("kind") != "intent" or not event.get("item"):
            continue
        if idx <= last_term.get(event["item"], -1):
            continue
        if (event.get("item"), event.get("step")) in resolved:
            continue
        if event.get("item") in terminal_items:
            continue
        for value in (event.get("item", ""), event.get("step", ""),
                      event.get("pr", ""), event.get("head", "")):
            sys.stdout.write(str(value) + "\0")
    return 0


def cmd_read_nonterminal(ns) -> int:
    """NUL-emit item, dangling-step, pr, head for EVERY item lacking a
    terminal — the complete per-item resume projection (REQ-208).

    dangling-step is the item's latest intent lacking a result (empty when
    the item has no dangling intent, e.g. a crash between steps); pr/head
    come from that intent's idempotency key, else from the item's latest
    keyed intent, so resume can reconcile against merge authority before
    deciding whether an effect must be retried.
    """
    doc = _load(_doc_path(ns.store, ns.queue_id))
    events = doc["events"]
    # LOW-3 (round 4): results, dangling intents, and idempotency keys are
    # ATTEMPT-scoped — only events after the item's last terminal
    # participate.  A closed attempt's result must never satisfy a
    # reopened attempt's identically-named step, and its keys must never
    # let resume conclude the NEW attempt's effect already happened.
    last_term = _last_terminal_index(events)
    resolved = {(e.get("item"), e.get("step"))
                for idx, e in enumerate(events)
                if e.get("kind") == "result"
                and idx > last_term.get(e.get("item"), -1)}
    # CR-03 (round 3): sequence-ordered — a later intent reopens the item,
    # so a crashed second-round attempt after a quarantine terminal resumes.
    terminal_items = effective_terminal_items(events)
    order, dangling, keyed = [], {}, {}
    # CR-03: the declared manifest — not the event stream — is the item
    # universe: an item that crashed before its FIRST event still resumes.
    for item in doc.get("manifest") or []:
        if item not in terminal_items:
            order.append(item)
            dangling[item] = None
    for idx, event in enumerate(events):
        item = event.get("item")
        if not item or item in terminal_items:
            continue
        if item not in dangling:
            order.append(item)
            dangling[item] = None
        if idx <= last_term.get(item, -1):
            continue
        if event.get("kind") != "intent":
            continue
        if event.get("key"):
            keyed[item] = event
        if (item, event.get("step")) not in resolved:
            dangling[item] = event
    for item in order:
        intent = dangling[item] or keyed.get(item) or {}
        step = dangling[item].get("step", "") if dangling[item] else ""
        for value in (item, step, str(intent.get("pr", "") or ""),
                      str(intent.get("head", "") or "")):
            sys.stdout.write(str(value) + "\0")
    return 0


def cmd_count_terminals(ns) -> int:
    """Durable per-item terminal counter (EDGE-010 quarantine park)."""
    doc = _load(_doc_path(ns.store, ns.queue_id))
    count = sum(1 for event in doc["events"]
                if event.get("kind") == "terminal"
                and event.get("item") == ns.item
                and event.get("status") == ns.status)
    print(count)
    return 0


def cmd_read_terminals(ns) -> int:
    doc = _load(_doc_path(ns.store, ns.queue_id))
    for event in doc["events"]:
        if event.get("kind") != "terminal":
            continue
        for field in ("item", "status", "detail"):
            sys.stdout.write(str(event.get(field, "")) + "\0")
    return 0


def landed_tuples(doc: dict) -> list:
    """The closed canonical target tuple projection
    (branch ref, expected tip OID, PR#, observed merge commit) for every
    item whose LAST terminal is LANDED (spec-006 Phase 3, REQ-301).

    This is the ONLY projection consolidate:estate may derive from — shared
    by cmd_read_landed_tuples and gates.py grant-consolidate (CR-03): the
    tip OID and PR come from the item's latest keyed intent, the observed
    merge commit from the LANDED terminal's detail, and every field is
    re-validated against the closed regexes on the way out.  A LANDED item
    with NO keyed intent at all performed no queue effect (WR-03: an
    externally-observed landing) and is omitted; a LANDED item WITH a keyed
    intent but missing/malformed evidence refuses the WHOLE read — a
    corrupted effect target must never mint a grant.
    """
    last_terminal, keyed = {}, {}
    for event in doc["events"]:
        item = event.get("item")
        if not item:
            continue
        if event.get("kind") == "terminal":
            last_terminal[item] = event
        elif event.get("kind") == "intent":
            # CR-03 (round 3): a later intent starts a new attempt — the
            # stale terminal is no longer the item's lifecycle truth, so a
            # reopened item can never stay a proven-final deletion target.
            last_terminal.pop(item, None)
            if event.get("key"):
                keyed[item] = event
    tuples = []
    for item in sorted(last_terminal):
        term = last_terminal[item]
        if term.get("status") != "LANDED":
            continue
        intent = keyed.get(item)
        merge_sha = term.get("detail")
        if intent is None:
            # WR-03 (round 2): no keyed merge/finalize intent was ever
            # journaled for this item — the queue performed no effect on it
            # (a merge can only run AFTER its keyed intent is durable), so
            # its landing was observed externally.  A proven no-effect
            # target is OMITTED from the deletion projection instead of
            # poisoning every valid target in the queue.
            continue
        if (not PR_RE.match(str(intent.get("pr", "")))
                or not OID_RE.match(str(intent.get("head", "")))
                or not isinstance(merge_sha, str)
                or not OID_RE.match(merge_sha)):
            raise JournalError(
                "QUEUE-ERROR:store LANDED item lacks a validated canonical "
                "tuple (keyed intent + 40-hex merge detail): " + item)
        tuples.append((item, intent["head"], str(intent["pr"]), merge_sha))
    return tuples


def nonterminal_items(doc: dict) -> list:
    """Items carrying events but no terminal — a nonterminal queue must
    never mint a consolidate grant (CR-03)."""
    seen = []
    # CR-03: enumerate the declared manifest so an item with zero events is
    # still nonterminal — a partial manifest must never mint a grant.
    for item in doc.get("manifest") or []:
        if item not in seen:
            seen.append(item)
    for event in doc["events"]:
        item = event.get("item")
        if not item:
            continue
        if item not in seen:
            seen.append(item)
    # CR-03 (round 3): sequence-ordered — a later intent reopens the item.
    terminal = effective_terminal_items(doc["events"])
    return [item for item in seen if item not in terminal]


def cmd_read_landed_tuples(ns) -> int:
    doc = _load(_doc_path(ns.store, ns.queue_id))
    for branch, head, pr, merge_sha in landed_tuples(doc):
        for value in (branch, head, pr, merge_sha):
            sys.stdout.write(str(value) + "\0")
    return 0


def cmd_read_meta(ns) -> int:
    """NUL-emit created_at, deadline, repo_root, base (CR-05/CR-02).

    The journal's immutable clock and repository binding, loaded by resume
    BEFORE any reconciliation effect so the guard clock and the repo check
    are anchored to journal evidence, never the resumer's environment.
    Optional fields absent from pre-binding journals emit empty (additive:
    SCHEMA stays 1)."""
    doc = _load(_doc_path(ns.store, ns.queue_id))
    for key in ("created_at", "deadline", "repo_root", "base"):
        value = doc.get(key)
        sys.stdout.write(("" if value is None else str(value)) + "\0")
    return 0


def cmd_read_run_id(ns) -> int:
    """Print the journal's recorded owning run id (WR-02): resumed queues
    bind the consolidate grant to the ORIGINAL run, never the resumer."""
    doc = _load(_doc_path(ns.store, ns.queue_id))
    print(doc.get("run_id", ""))
    return 0


def cmd_read_report(ns) -> int:
    """NUL-emit item,status,detail,reason,unblock for report rendering.

    Item terminals are deduplicated to the LAST terminal per item (a
    requeued quarantine reports its parked outcome once); queue-level
    terminals pass through untouched.
    """
    doc = _load(_doc_path(ns.store, ns.queue_id))
    terminals = [e for e in doc["events"] if e.get("kind") == "terminal"]
    # CR-03 (round 3): a stale terminal superseded by a later intent is not
    # a FINAL outcome — reporting it as final hides a crashed retry.
    effective = effective_terminal_items(doc["events"])
    last = {}
    for idx, event in enumerate(terminals):
        item = event.get("item", "")
        if item:
            last[item] = idx
    for idx, event in enumerate(terminals):
        item = event.get("item", "")
        if item and (last[item] != idx or item not in effective):
            continue
        for field in ("item", "status", "detail", "reason", "unblock"):
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

    for name in ("init", "append", "events", "read-terminals", "read-dangling",
                 "read-nonterminal", "count-terminals", "read-report",
                 "read-landed-tuples", "read-run-id", "read-meta",
                 "record-manifest"):
        p = sub.add_parser(name)
        p.add_argument("--store", required=True)
        p.add_argument("--queue-id", required=True)
        if name == "init":
            p.add_argument("--run-id", required=True)
            p.add_argument("--repo")
            p.add_argument("--base")
        if name == "append":
            p.add_argument("--kind", required=True)
            p.add_argument("--step", required=True)
            p.add_argument("--item")
            p.add_argument("--status")
            p.add_argument("--detail")
            p.add_argument("--reason")
            p.add_argument("--unblock")
            p.add_argument("--pr")
            p.add_argument("--head")
        if name == "count-terminals":
            p.add_argument("--item", required=True)
            p.add_argument("--status", required=True)
        if name == "record-manifest":
            p.add_argument("--item", action="append")
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
                "read-nonterminal": cmd_read_nonterminal,
                "count-terminals": cmd_count_terminals,
                "read-report": cmd_read_report,
                "read-landed-tuples": cmd_read_landed_tuples,
                "read-run-id": cmd_read_run_id,
                "read-meta": cmd_read_meta,
                "record-manifest": cmd_record_manifest,
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
