#!/usr/bin/env python3
"""Narrow, serialized producer for the finalizer's typed skipped event."""

from __future__ import annotations

import argparse
import re
import sys

from gates import _StoreLock, _load_store, _now, _save_store, _store_path

MAX_RUN_ID = 128
RUN_ID_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")
POS_INT_RE = re.compile(r"^[1-9][0-9]*$")


def _run_id(value: str) -> str:
    if not RUN_ID_RE.fullmatch(value):
        raise ValueError("run id must be non-empty, <=128 bytes, and control-free")
    if len(value.encode()) > MAX_RUN_ID:
        raise ValueError("run id exceeds 128 bytes")
    return value


def _positive(value: str, name: str) -> int:
    if not POS_INT_RE.fullmatch(value):
        raise ValueError(f"{name} must be a strictly positive base-10 integer")
    return int(value)


def finisher_skipped(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="evidence_events.py finisher-skipped")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--lock-path")
    parser.add_argument("--holder-pid")
    ns = parser.parse_args(args)
    try:
        run_id = _run_id(ns.run_id)
        pr = _positive(ns.pr, "pr")
        trace = (ns.lock_path, ns.holder_pid)
        if run_id == "unattributed":
            if not all(trace):
                raise ValueError("unattributed events require --lock-path and --holder-pid")
            if not ns.lock_path.startswith("/") or not RUN_ID_RE.fullmatch(ns.lock_path):
                raise ValueError("lock path must be an absolute control-free path")
            holder_pid = _positive(ns.holder_pid, "holder pid")
        elif any(trace):
            raise ValueError("trace flags are permitted only for unattributed events")
        else:
            holder_pid = None
    except ValueError as exc:
        print(f"finisher-skipped rejected: {exc}", file=sys.stderr)
        return 2

    store = _store_path()
    try:
        with _StoreLock(store):
            data = _load_store(store)
            events = data.setdefault("events", [])
            if not isinstance(events, list):
                raise ValueError("evidence events namespace is not a list")
            event = {"kind": "finisher-skipped", "run_id": run_id, "pr": pr, "ts": _now()}
            if run_id == "unattributed":
                event.update({"lock_path": ns.lock_path, "holder_pid": holder_pid})
            events.append(event)
            _save_store(store, data)
    except (OSError, ValueError, TypeError) as exc:
        print(f"finisher-skipped write failed: {exc}", file=sys.stderr)
        return 78
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "finisher-skipped":
        print("usage: evidence_events.py finisher-skipped --run-id ID --pr NUMBER", file=sys.stderr)
        return 2
    return finisher_skipped(argv[1:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
