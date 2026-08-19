"""Tests for lib/gates.py `findings-queue` subcommand family (REQ-06 / AC-006,
extended by spec-004 AC-015 — findings-queue v2: dispositions + reopen +
severity/source/plan metadata + filtered list).

Covers: function-level lifecycle, full subprocess CLI lifecycle (PATH-004),
signature stability + delimiter-injection distinctness (EDGE-004), dedup
atomicity, unknown-sig resolve error, usage errors, schema-conflict guard,
store-key isolation, synchronized-thread concurrency (adversary F1/F2/F4/F5/
F7/F8), and v2: disposition/reason requirement, reopen-on-re-add-of-resolved,
severity/source/plan filters, backward-compat store isolation.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

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
    sig1, deduped1, reopened1 = gates.findings_add(store, "a.py", "Missing null check")
    sig2, deduped2, reopened2 = gates.findings_add(store, "b.py", "No error handling")
    sig3, deduped3, reopened3 = gates.findings_add(store, "c.py", "Unbounded query")
    assert deduped1 is False and deduped2 is False and deduped3 is False
    assert reopened1 is False and reopened2 is False and reopened3 is False
    assert len({sig1, sig2, sig3}) == 3

    unresolved = gates.findings_list(store, unresolved=True)
    assert len(unresolved) == 3

    resolved = gates.findings_resolve(store, sig1, disposition="fix", reason="patched")
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
        assert set(payload.keys()) == {"sig", "deduped", "reopened"}
        assert len(payload["sig"]) == 64
        assert payload["deduped"] is False
        assert payload["reopened"] is False
        sigs.append(payload["sig"])

    proc = _run("list", "--unresolved", store=store)
    assert proc.returncode == 0
    listed = json.loads(proc.stdout.strip())
    assert isinstance(listed, list)
    assert len(listed) == 3

    proc = _run("resolve", sigs[0], "--disposition", "fix", "--reason", "patched",
                store=store)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    assert payload == {"sig": sigs[0], "resolved": True, "disposition": "fix"}

    proc = _run("list", "--unresolved", store=store)
    listed = json.loads(proc.stdout.strip())
    assert len(listed) == 2

    # re-add of the just-resolved signature — deduped:true, and since it was
    # resolved, this REOPENS it (v2 AC-015); count stays unchanged (same sig).
    proc = _run("add", "a.py", "issue one", store=store)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    assert payload["deduped"] is True
    assert payload["reopened"] is True
    assert payload["sig"] == sigs[0]

    proc = _run("list", store=store)
    listed = json.loads(proc.stdout.strip())
    assert len(listed) == 3  # unchanged — dedup didn't add a 4th entry


# ── signature stability + delimiter-injection distinctness (EDGE-004) ───────

def test_signature_stability_whitespace_and_case_normalized(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    sig1, _, _ = gates.findings_add(store, "f.py", "Missing check")
    sig2, deduped2, _ = gates.findings_add(store, "f.py", "  missing   CHECK ")
    assert sig1 == sig2
    assert len(sig1) == 64
    assert deduped2 is True


def test_signature_delimiter_injection_distinct(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    sig1, _, _ = gates.findings_add(store, "a:b", "c")
    sig2, _, _ = gates.findings_add(store, "a", "b:c")
    assert sig1 != sig2


def test_signature_same_file_reworded_issue_distinct(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    sig1, _, _ = gates.findings_add(store, "f.py", "Missing null check")
    sig2, _, _ = gates.findings_add(store, "f.py", "Null check missing entirely")
    assert sig1 != sig2


def test_signature_different_file_same_issue_distinct(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    sig1, _, _ = gates.findings_add(store, "a.py", "same issue")
    sig2, _, _ = gates.findings_add(store, "b.py", "same issue")
    assert sig1 != sig2


def test_signature_same_file_and_issue_different_plan_distinct(tmp_path) -> None:
    """spec-004 fix round finding 6: the SAME file+issue text reported for
    two different plans must be two distinct findings — otherwise plan B's
    identical finding silently vanishes into plan A's record and plan B's
    `list --plan B` (what the wall actually checks) sees nothing."""
    store = tmp_path / "evidence.json"
    sig1, deduped1, _ = gates.findings_add(
        store, "shared.py", "identical defect text", plan="phase-1/PLAN.md")
    sig2, deduped2, _ = gates.findings_add(
        store, "shared.py", "identical defect text", plan="phase-2/PLAN.md")
    assert sig1 != sig2
    assert deduped1 is False
    assert deduped2 is False

    plan1_view = gates.findings_list(store, unresolved=True, plan="phase-1/PLAN.md")
    plan2_view = gates.findings_list(store, unresolved=True, plan="phase-2/PLAN.md")
    assert {f["sig"] for f in plan1_view} == {sig1}
    assert {f["sig"] for f in plan2_view} == {sig2}


def test_cli_cross_plan_duplicate_surfaces_under_each_plans_own_wall_query(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _run("add", "shared.py", "identical defect text", "--severity", "HIGH",
         "--source", "wall", "--plan", "phase-1/PLAN.md", store=store)
    _run("add", "shared.py", "identical defect text", "--severity", "HIGH",
         "--source", "wall", "--plan", "phase-2/PLAN.md", store=store)

    proc = _run("list", "--unresolved", "--source", "wall", "--severity", "HIGH,CRITICAL",
                "--plan", "phase-2/PLAN.md", store=store)
    assert proc.returncode == 0
    listed = json.loads(proc.stdout.strip())
    assert len(listed) == 1
    assert listed[0]["plan"] == "phase-2/PLAN.md"


# ── dedup atomicity ───────────────────────────────────────────────────────────

def test_dedup_flag_from_locked_operation_not_a_preread(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    sig1, deduped1, _ = gates.findings_add(store, "x.py", "dup me")
    sig2, deduped2, _ = gates.findings_add(store, "x.py", "dup me")
    assert sig1 == sig2
    assert deduped1 is False
    assert deduped2 is True


def test_concurrent_add_same_sig_exactly_one_not_deduped(tmp_path) -> None:
    # finding 11: exercise the RACE, not sequential adds. Two threads add the
    # same NEW sig at once (Barrier-synchronized). If dedup is computed inside
    # the store lock (not a racy pre-read), exactly one add returns
    # deduped=False and the store holds exactly one record for that sig.
    store = tmp_path / "evidence.json"
    barrier = threading.Barrier(2)
    results: list[tuple[str, bool, bool]] = []
    append_lock = threading.Lock()

    def do_add():
        barrier.wait()
        r = gates.findings_add(store, "race.py", "same finding")
        with append_lock:
            results.append(r)

    t1 = threading.Thread(target=do_add)
    t2 = threading.Thread(target=do_add)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(results) == 2
    sigs = {sig for sig, _, _ in results}
    assert len(sigs) == 1  # identical sig
    assert sorted(deduped for _, deduped, _ in results) == [False, True]  # exactly one new

    data = json.loads(store.read_text())
    matching = [f for f in data["findings"] if f["sig"] == sigs.pop()]
    assert len(matching) == 1  # no double-insert under the race


# ── resolve unknown sig (adversary F4) ───────────────────────────────────────

def test_findings_resolve_unknown_sig_returns_false(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    gates.findings_add(store, "a.py", "known issue")
    assert gates.findings_resolve(store, "deadbeef" * 8, disposition="fix",
                                   reason="n/a") is False


def test_cli_resolve_unknown_sig_nonzero_exit_with_stderr(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _run("add", "a.py", "known issue", store=store)
    proc = _run("resolve", "deadbeef" * 8, "--disposition", "fix", "--reason", "n/a",
                store=store)
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

    proc = _run("resolve", "deadbeef" * 8, "--disposition", "fix", "--reason", "n/a",
                store=store)
    assert proc.returncode == 3
    assert store.read_text() == before


# ── store-key isolation ──────────────────────────────────────────────────────

def test_unrelated_store_keys_untouched(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    seeded = {"_autonomy": {"run-1": {"grants": {"push:origin/main": {}}}},
              "T001": {"gate": {"exit_code": 0}}}
    store.write_text(json.dumps(seeded, indent=2))

    sig, _, _ = gates.findings_add(store, "a.py", "issue")
    gates.findings_resolve(store, sig, disposition="fix", reason="patched")

    data = json.loads(store.read_text())
    assert data["_autonomy"] == seeded["_autonomy"]
    assert data["T001"] == seeded["T001"]
    assert "findings" in data


# ── concurrency (adversary F5) ───────────────────────────────────────────────

def test_concurrent_add_and_resolve_no_lost_update(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    pre_sig, _, _ = gates.findings_add(store, "pre.py", "pre-existing")

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def do_add():
        barrier.wait()
        results["new_sig"] = gates.findings_add(store, "new.py", "new finding")

    def do_resolve():
        barrier.wait()
        results["resolved"] = gates.findings_resolve(store, pre_sig, disposition="fix",
                                                       reason="patched")

    t1 = threading.Thread(target=do_add)
    t2 = threading.Thread(target=do_resolve)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results["resolved"] is True
    new_sig, deduped, reopened = results["new_sig"]
    assert deduped is False
    assert reopened is False

    data = json.loads(store.read_text())
    findings = data["findings"]
    by_sig = {f["sig"]: f for f in findings}
    assert new_sig in by_sig
    assert by_sig[pre_sig]["resolved"] is True


# ── v2 (AC-015): disposition/reason requirement ──────────────────────────────

def test_resolve_without_disposition_raises(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    sig, _, _ = gates.findings_add(store, "a.py", "issue")
    with pytest.raises(ValueError):
        gates.findings_resolve(store, sig, disposition="bogus", reason="x")


def test_resolve_without_reason_raises(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    sig, _, _ = gates.findings_add(store, "a.py", "issue")
    with pytest.raises(ValueError):
        gates.findings_resolve(store, sig, disposition="fix", reason="")
    with pytest.raises(ValueError):
        gates.findings_resolve(store, sig, disposition="fix", reason="   ")


def test_cli_resolve_missing_disposition_usage_error(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    proc = _run("add", "a.py", "issue", store=store)
    sig = json.loads(proc.stdout.strip())["sig"]
    proc = _run("resolve", sig, store=store)
    assert proc.returncode == 2
    assert proc.stderr.strip() != ""


def test_cli_resolve_bad_disposition_usage_error(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    proc = _run("add", "a.py", "issue", store=store)
    sig = json.loads(proc.stdout.strip())["sig"]
    proc = _run("resolve", sig, "--disposition", "yolo", "--reason", "x", store=store)
    assert proc.returncode == 2
    assert proc.stderr.strip() != ""


# ── v2 (AC-015): reopen semantics ────────────────────────────────────────────

def test_readd_of_resolved_signature_reopens_with_history(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    sig1, deduped1, reopened1 = gates.findings_add(store, "a.py", "leaky query")
    assert deduped1 is False and reopened1 is False
    assert gates.findings_resolve(store, sig1, disposition="refute", reason="false positive")

    unresolved = gates.findings_list(store, unresolved=True)
    assert sig1 not in {f["sig"] for f in unresolved}

    sig2, deduped2, reopened2 = gates.findings_add(store, "a.py", "leaky query")
    assert sig2 == sig1
    assert deduped2 is True
    assert reopened2 is True

    unresolved_after = gates.findings_list(store, unresolved=True)
    assert sig1 in {f["sig"] for f in unresolved_after}

    record = next(f for f in gates.findings_list(store) if f["sig"] == sig1)
    assert record["resolved"] is False
    assert "disposition" not in record
    assert len(record["history"]) == 1
    assert record["history"][0]["disposition"] == "refute"
    assert record["history"][0]["reason"] == "false positive"


def test_double_resolve_preserves_prior_disposition_in_history(tmp_path) -> None:
    """spec-004 fix round finding 11: re-resolving an ALREADY-resolved
    signature (e.g. an operator correcting refute -> fix) must not silently
    discard the earlier adjudication — it goes to `history`, same as the
    reopen path already does."""
    store = tmp_path / "evidence.json"
    sig, _, _ = gates.findings_add(store, "a.py", "leaky query")
    assert gates.findings_resolve(store, sig, disposition="refute", reason="false positive")
    assert gates.findings_resolve(store, sig, disposition="fix", reason="actually real, patched")

    record = next(f for f in gates.findings_list(store) if f["sig"] == sig)
    assert record["resolved"] is True
    assert record["disposition"] == "fix"
    assert record["reason"] == "actually real, patched"
    assert len(record["history"]) == 1
    assert record["history"][0]["disposition"] == "refute"
    assert record["history"][0]["reason"] == "false positive"

    # a THIRD resolve appends a second history entry — nothing overwritten
    assert gates.findings_resolve(store, sig, disposition="waive", reason="deprioritized")
    record2 = next(f for f in gates.findings_list(store) if f["sig"] == sig)
    assert len(record2["history"]) == 2
    assert record2["history"][1]["disposition"] == "fix"
    assert record2["history"][1]["reason"] == "actually real, patched"


def test_readd_of_unresolved_signature_does_not_reopen(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    sig1, _, _ = gates.findings_add(store, "a.py", "issue")
    sig2, deduped2, reopened2 = gates.findings_add(store, "a.py", "issue")
    assert sig1 == sig2
    assert deduped2 is True
    assert reopened2 is False


def test_cli_readd_of_resolved_reopens(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    proc = _run("add", "a.py", "leaky query", store=store)
    sig = json.loads(proc.stdout.strip())["sig"]
    _run("resolve", sig, "--disposition", "refute", "--reason", "nope", store=store)

    proc = _run("add", "a.py", "leaky query", store=store)
    payload = json.loads(proc.stdout.strip())
    assert payload["sig"] == sig
    assert payload["deduped"] is True
    assert payload["reopened"] is True

    proc = _run("list", "--unresolved", store=store)
    listed = json.loads(proc.stdout.strip())
    assert sig in {f["sig"] for f in listed}


# ── D-1a: similarity fold (reworded-but-identical findings collapse) ────────

def test_fold_exact_duplicate_unchanged(tmp_path) -> None:
    """Byte-identical issue text still hits the exact-sig path — exact match
    takes priority over fold-hunting, and no `aliases` key appears."""
    store = tmp_path / "evidence.json"
    sig1, _, _ = gates.findings_add(store, "a.py", "leaky query")
    sig2, deduped2, _ = gates.findings_add(store, "a.py", "leaky query")
    assert sig1 == sig2
    assert deduped2 is True
    record = next(f for f in gates.findings_list(store) if f["sig"] == sig1)
    assert "aliases" not in record


def test_fold_reworded_same_plan_file_folds_with_alias(tmp_path) -> None:
    import difflib
    a = "The auth handler does not validate the session token"
    b = "The auth handler does not validate session token"

    def norm(s: str) -> str:
        return " ".join(s.split()).lower()

    ratio = difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()
    assert ratio >= gates._FOLD_THRESHOLD

    store = tmp_path / "evidence.json"
    sig1, deduped1, _ = gates.findings_add(store, "auth.py", a, plan="phase-1/PLAN.md")
    sig2, deduped2, _ = gates.findings_add(store, "auth.py", b, plan="phase-1/PLAN.md")
    assert deduped1 is False
    assert sig1 == sig2
    assert deduped2 is True
    record = next(f for f in gates.findings_list(store) if f["sig"] == sig1)
    assert b in record.get("aliases", [])


def test_fold_same_file_different_plan_never_folds(tmp_path) -> None:
    a = "The auth handler does not validate the session token"
    b = "The auth handler does not validate session token"
    store = tmp_path / "evidence.json"
    sig1, _, _ = gates.findings_add(store, "auth.py", a, plan="phase-1/PLAN.md")
    sig2, _, _ = gates.findings_add(store, "auth.py", b, plan="phase-2/PLAN.md")
    assert sig1 != sig2


def test_fold_below_threshold_new_sig(tmp_path) -> None:
    import difflib
    a = "API endpoint has no rate limiting"
    b = "The endpoint lacks any throttling protections"

    def norm(s: str) -> str:
        return " ".join(s.split()).lower()

    ratio = difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()
    assert ratio < gates._FOLD_THRESHOLD

    store = tmp_path / "evidence.json"
    sig1, _, _ = gates.findings_add(store, "api.py", a, plan="phase-1/PLAN.md")
    sig2, deduped2, _ = gates.findings_add(store, "api.py", b, plan="phase-1/PLAN.md")
    assert sig1 != sig2
    assert deduped2 is False
    record = next(f for f in gates.findings_list(store) if f["sig"] == sig2)
    assert "aliases" not in record


def test_fold_onto_resolved_record_reopens(tmp_path) -> None:
    a = "The auth handler does not validate the session token"
    b = "The auth handler does not validate session token"
    store = tmp_path / "evidence.json"
    sig1, _, _ = gates.findings_add(store, "auth.py", a, plan="phase-1/PLAN.md")
    assert gates.findings_resolve(store, sig1, disposition="fix", reason="patched")

    sig2, deduped2, reopened2 = gates.findings_add(store, "auth.py", b, plan="phase-1/PLAN.md")
    assert sig2 == sig1
    assert deduped2 is True
    assert reopened2 is True

    record = next(f for f in gates.findings_list(store) if f["sig"] == sig1)
    assert record["resolved"] is False
    assert "disposition" not in record
    assert len(record["history"]) == 1
    assert record["history"][0]["disposition"] == "fix"
    assert record["history"][0]["reason"] == "patched"
    assert b in record.get("aliases", [])


def test_fold_leaves_unrelated_rows_byte_stable(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    unrelated_sig, _, _ = gates.findings_add(store, "unrelated.py", "totally unrelated finding")
    unrelated_record = next(f for f in gates.findings_list(store) if f["sig"] == unrelated_sig)
    unrelated_json = json.dumps(unrelated_record, sort_keys=True)

    a = "The auth handler does not validate the session token"
    b = "The auth handler does not validate session token"
    gates.findings_add(store, "auth.py", a, plan="phase-1/PLAN.md")
    gates.findings_add(store, "auth.py", b, plan="phase-1/PLAN.md")

    unrelated_record_after = next(
        f for f in gates.findings_list(store) if f["sig"] == unrelated_sig
    )
    assert json.dumps(unrelated_record_after, sort_keys=True) == unrelated_json


def test_fold_higher_incoming_severity_does_not_fold(tmp_path) -> None:
    """A reworded report that ranks MORE severe than the stored candidate
    must mint a brand-new record, never fold under it — folding a fresh
    CRITICAL under an unresolved LOW would hide the CRITICAL from the
    queue."""
    a = "The auth handler does not validate the session token"
    b = "The auth handler does not validate session token"
    store = tmp_path / "evidence.json"
    sig1, _, _ = gates.findings_add(store, "auth.py", a, plan="phase-1/PLAN.md", severity="LOW")
    sig2, deduped2, _ = gates.findings_add(store, "auth.py", b, plan="phase-1/PLAN.md", severity="CRITICAL")
    assert sig1 != sig2
    assert deduped2 is False
    record = next(f for f in gates.findings_list(store) if f["sig"] == sig2)
    assert "aliases" not in record


def test_fold_equal_severity_still_folds(tmp_path) -> None:
    """Equal-severity reworded reports fold exactly as before the guard."""
    a = "The auth handler does not validate the session token"
    b = "The auth handler does not validate session token"
    store = tmp_path / "evidence.json"
    sig1, _, _ = gates.findings_add(store, "auth.py", a, plan="phase-1/PLAN.md", severity="HIGH")
    sig2, deduped2, _ = gates.findings_add(store, "auth.py", b, plan="phase-1/PLAN.md", severity="HIGH")
    assert sig1 == sig2
    assert deduped2 is True
    record = next(f for f in gates.findings_list(store) if f["sig"] == sig1)
    assert b in record.get("aliases", [])


def test_fold_double_submit_same_alias_text_dedupes_to_one(tmp_path) -> None:
    """Re-submitting the SAME reworded text a second time must not append a
    second alias row — it's still a fold (deduped=True), just no duplicate."""
    a = "The auth handler does not validate the session token"
    b = "The auth handler does not validate session token"
    store = tmp_path / "evidence.json"
    sig1, _, _ = gates.findings_add(store, "auth.py", a, plan="phase-1/PLAN.md")
    sig2, deduped2, _ = gates.findings_add(store, "auth.py", b, plan="phase-1/PLAN.md")
    sig3, deduped3, _ = gates.findings_add(store, "auth.py", b, plan="phase-1/PLAN.md")
    assert sig1 == sig2 == sig3
    assert deduped2 is True
    assert deduped3 is True
    record = next(f for f in gates.findings_list(store) if f["sig"] == sig1)
    assert record.get("aliases", []) == [b]
    assert len(record.get("aliases", [])) == 1


# ── v2 (AC-015): metadata + filters ──────────────────────────────────────────

def test_add_with_metadata_flags(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    proc = _run("add", "plan.md", "missing test", "--severity", "HIGH",
                "--run-id", "run-1", "--source", "wall", "--plan",
                ".planning/phases/1/PLAN.md", store=store)
    assert proc.returncode == 0
    sig = json.loads(proc.stdout.strip())["sig"]
    record = next(f for f in gates.findings_list(store) if f["sig"] == sig)
    assert record["severity"] == "HIGH"
    assert record["run_id"] == "run-1"
    assert record["source"] == "wall"
    assert record["plan"] == ".planning/phases/1/PLAN.md"


def test_add_positional_value_matching_a_flag_name_is_not_hijacked(tmp_path) -> None:
    """spec-004 fix round finding 5a: <file>/<issue> are the two FIXED
    leading positionals, consumed strictly by position — an
    adversary-controlled value (e.g. an LLM-reported finding's `file` text)
    that happens to equal a known flag name like "--severity" must land in
    its intended positional slot, not get reinterpreted as that flag."""
    store = tmp_path / "evidence.json"
    proc = _run("add", "--severity", "attacker-controlled issue text", store=store)
    assert proc.returncode == 0
    sig = json.loads(proc.stdout.strip())["sig"]
    record = next(f for f in gates.findings_list(store) if f["sig"] == sig)
    assert record["file"] == "--severity"
    assert record["issue"] == "attacker-controlled issue text"
    # never silently promoted to an actual HIGH/etc severity via injection
    assert record["severity"] != "attacker-controlled issue text"


def test_add_trailing_flag_with_no_value_is_a_usage_error(tmp_path) -> None:
    """spec-004 fix round finding 5b: a recognized flag with nothing after it
    used to silently fall through and become an unintended 3rd positional
    (ignored, not an error) — now it is a usage error."""
    store = tmp_path / "evidence.json"
    proc = _run("add", "a.py", "issue", "--severity", store=store)
    assert proc.returncode == 2
    assert proc.stderr.strip() != ""


def test_resolve_trailing_flag_with_no_value_is_a_usage_error(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    sig, _, _ = gates.findings_add(store, "a.py", "issue")
    proc = _run("resolve", sig, "--disposition", "fix", "--reason", store=store)
    assert proc.returncode == 2
    assert proc.stderr.strip() != ""


def test_list_severity_filter_comma_separated(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    gates.findings_add(store, "a.py", "one", severity="HIGH")
    gates.findings_add(store, "b.py", "two", severity="LOW")
    gates.findings_add(store, "c.py", "three", severity="CRITICAL")
    result = gates.findings_list(store, severity="HIGH,CRITICAL")
    assert {f["file"] for f in result} == {"a.py", "c.py"}


def test_list_source_and_plan_filters(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    gates.findings_add(store, "a.py", "one", source="wall", plan="phase-1/PLAN.md")
    gates.findings_add(store, "b.py", "two", source="review-gate", plan="phase-1/PLAN.md")
    gates.findings_add(store, "c.py", "three", source="wall", plan="phase-2/PLAN.md")

    wall_only = gates.findings_list(store, source="wall")
    assert {f["file"] for f in wall_only} == {"a.py", "c.py"}

    phase1_only = gates.findings_list(store, plan="phase-1/PLAN.md")
    assert {f["file"] for f in phase1_only} == {"a.py", "b.py"}

    scoped = gates.findings_list(store, unresolved=True, source="wall",
                                  severity="HIGH,CRITICAL", plan="phase-1/PLAN.md")
    assert scoped == []  # none of the phase-1/wall findings carry HIGH/CRITICAL


def test_cli_list_plan_scoped_filter(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _run("add", "a.py", "issue-a", "--severity", "HIGH", "--source", "wall",
         "--plan", "phase-1/PLAN.md", store=store)
    _run("add", "b.py", "issue-b", "--severity", "HIGH", "--source", "wall",
         "--plan", "phase-2/PLAN.md", store=store)
    proc = _run("list", "--unresolved", "--source", "wall", "--severity", "HIGH,CRITICAL",
                "--plan", "phase-1/PLAN.md", store=store)
    assert proc.returncode == 0
    listed = json.loads(proc.stdout.strip())
    assert len(listed) == 1
    assert listed[0]["file"] == "a.py"


# ── backward-compat: prior (v1-shaped) entries untouched by v2 filters ──────

def test_v1_shaped_entries_survive_v2_filters(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    seeded = {"findings": [{"sig": "a" * 64, "file": "x.py", "issue": "old",
                            "resolved": False, "recorded_at": 0}]}
    store.write_text(json.dumps(seeded, indent=2))

    all_findings = gates.findings_list(store)
    assert len(all_findings) == 1
    assert all_findings[0]["file"] == "x.py"

    # a v1 entry has no severity/source/plan — filtering on any of those excludes it,
    # never raises.
    assert gates.findings_list(store, severity="HIGH") == []
    assert gates.findings_list(store, source="wall") == []
    assert gates.findings_list(store, plan="anything") == []

    # resolving a v1-shaped entry with v2 semantics still works
    assert gates.findings_resolve(store, "a" * 64, disposition="fix", reason="ok") is True
