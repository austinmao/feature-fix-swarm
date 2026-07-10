"""Tests for lib/gates.py `findings-queue` subcommand family (REQ-06 / AC-006).

Covers: function-level lifecycle, full subprocess CLI lifecycle (PATH-004),
signature stability + delimiter-injection distinctness (EDGE-004), dedup
atomicity, unknown-sig resolve error, usage errors, schema-conflict guard,
store-key isolation, and synchronized-thread concurrency (adversary F1/F2/
F4/F5/F7/F8).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

DISPATCH_DIR = Path(__file__).resolve().parents[1]
GATES_PY = DISPATCH_DIR / "gates.py"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, DISPATCH_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


gates = _load("gates")


def _run(*args: str, store: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "GATES_STORE": str(store)}
    return subprocess.run(
        [sys.executable, str(GATES_PY), "findings-queue", *args],
        capture_output=True, text=True, env=env,
    )


# ── function-level happy path (PATH-004) ─────────────────────────────────────

def test_add_list_resolve_function_lifecycle(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    sig1, deduped1 = gates.findings_add(store, "a.py", "Missing null check")
    sig2, deduped2 = gates.findings_add(store, "b.py", "No error handling")
    sig3, deduped3 = gates.findings_add(store, "c.py", "Unbounded query")
    assert deduped1 is False and deduped2 is False and deduped3 is False
    assert len({sig1, sig2, sig3}) == 3

    unresolved = gates.findings_list(store, unresolved=True)
    assert len(unresolved) == 3

    resolved = gates.findings_resolve(store, sig1)
    assert resolved is True

    unresolved_after = gates.findings_list(store, unresolved=True)
    assert len(unresolved_after) == 2
    assert sig1 not in {f["sig"] for f in unresolved_after}

    all_findings = gates.findings_list(store, unresolved=False)
    assert len(all_findings) == 3


# ── full subprocess CLI lifecycle (all three dispatch branches) ─────────────

def test_cli_full_lifecycle_add_list_resolve_dedup(tmp_path) -> None:
    store = tmp_path / "evidence.json"

    sigs = []
    for f, issue in [("a.py", "issue one"), ("b.py", "issue two"), ("c.py", "issue three")]:
        proc = _run("add", f, issue, store=store)
        assert proc.returncode == 0
        line = proc.stdout.strip()
        payload = json.loads(line)
        assert set(payload.keys()) == {"sig", "deduped"}
        assert len(payload["sig"]) == 64
        assert payload["deduped"] is False
        sigs.append(payload["sig"])

    proc = _run("list", "--unresolved", store=store)
    assert proc.returncode == 0
    listed = json.loads(proc.stdout.strip())
    assert isinstance(listed, list)
    assert len(listed) == 3

    proc = _run("resolve", sigs[0], store=store)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    assert payload == {"sig": sigs[0], "resolved": True}

    proc = _run("list", "--unresolved", store=store)
    listed = json.loads(proc.stdout.strip())
    assert len(listed) == 2

    # re-add a duplicate — deduped:true, count unchanged
    proc = _run("add", "a.py", "issue one", store=store)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    assert payload["deduped"] is True
    assert payload["sig"] == sigs[0]

    proc = _run("list", store=store)
    listed = json.loads(proc.stdout.strip())
    assert len(listed) == 3  # unchanged — dedup didn't add a 4th entry


# ── signature stability + delimiter-injection distinctness (EDGE-004) ───────

def test_signature_stability_whitespace_and_case_normalized(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    sig1, _ = gates.findings_add(store, "f.py", "Missing check")
    sig2, deduped2 = gates.findings_add(store, "f.py", "  missing   CHECK ")
    assert sig1 == sig2
    assert len(sig1) == 64
    assert deduped2 is True


def test_signature_delimiter_injection_distinct(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    sig1, _ = gates.findings_add(store, "a:b", "c")
    sig2, _ = gates.findings_add(store, "a", "b:c")
    assert sig1 != sig2


def test_signature_same_file_reworded_issue_distinct(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    sig1, _ = gates.findings_add(store, "f.py", "Missing null check")
    sig2, _ = gates.findings_add(store, "f.py", "Null check missing entirely")
    assert sig1 != sig2


def test_signature_different_file_same_issue_distinct(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    sig1, _ = gates.findings_add(store, "a.py", "same issue")
    sig2, _ = gates.findings_add(store, "b.py", "same issue")
    assert sig1 != sig2


# ── dedup atomicity ───────────────────────────────────────────────────────────

def test_dedup_flag_from_locked_operation_not_a_preread(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    sig1, deduped1 = gates.findings_add(store, "x.py", "dup me")
    sig2, deduped2 = gates.findings_add(store, "x.py", "dup me")
    assert sig1 == sig2
    assert deduped1 is False
    assert deduped2 is True


# ── resolve unknown sig (adversary F4) ───────────────────────────────────────

def test_findings_resolve_unknown_sig_returns_false(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    gates.findings_add(store, "a.py", "known issue")
    assert gates.findings_resolve(store, "deadbeef" * 8) is False


def test_cli_resolve_unknown_sig_nonzero_exit_with_stderr(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _run("add", "a.py", "known issue", store=store)
    proc = _run("resolve", "deadbeef" * 8, store=store)
    assert proc.returncode == 1
    assert proc.stderr.strip() != ""


# ── usage errors (adversary F8) ──────────────────────────────────────────────

def test_cli_usage_error_missing_operand(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    proc = _run("add", "onlyonearg", store=store)
    assert proc.returncode == 2
    assert proc.stderr.strip() != ""


def test_cli_usage_error_unknown_subverb(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    proc = _run("bogus", store=store)
    assert proc.returncode == 2
    assert proc.stderr.strip() != ""


# ── schema-conflict guard (adversary F7) ─────────────────────────────────────

def test_schema_conflict_guard_never_clobbers(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    seeded = {"findings": {"gate": {"exit_code": 0}}}
    store.write_text(json.dumps(seeded, indent=2))
    before = store.read_text()

    proc = _run("add", "a.py", "issue", store=store)
    assert proc.returncode == 3
    assert store.read_text() == before

    proc = _run("list", store=store)
    assert proc.returncode == 3
    assert store.read_text() == before

    proc = _run("resolve", "deadbeef" * 8, store=store)
    assert proc.returncode == 3
    assert store.read_text() == before


# ── store-key isolation ──────────────────────────────────────────────────────

def test_unrelated_store_keys_untouched(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    seeded = {"_autonomy": {"run-1": {"grants": {"push:origin/main": {}}}},
              "T001": {"gate": {"exit_code": 0}}}
    store.write_text(json.dumps(seeded, indent=2))

    sig, _ = gates.findings_add(store, "a.py", "issue")
    gates.findings_resolve(store, sig)

    data = json.loads(store.read_text())
    assert data["_autonomy"] == seeded["_autonomy"]
    assert data["T001"] == seeded["T001"]
    assert "findings" in data


# ── concurrency (adversary F5) ───────────────────────────────────────────────

def test_concurrent_add_and_resolve_no_lost_update(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    pre_sig, _ = gates.findings_add(store, "pre.py", "pre-existing")

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def do_add():
        barrier.wait()
        results["new_sig"] = gates.findings_add(store, "new.py", "new finding")

    def do_resolve():
        barrier.wait()
        results["resolved"] = gates.findings_resolve(store, pre_sig)

    t1 = threading.Thread(target=do_add)
    t2 = threading.Thread(target=do_resolve)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results["resolved"] is True
    new_sig, deduped = results["new_sig"]
    assert deduped is False

    data = json.loads(store.read_text())
    findings = data["findings"]
    by_sig = {f["sig"]: f for f in findings}
    assert new_sig in by_sig
    assert by_sig[pre_sig]["resolved"] is True
