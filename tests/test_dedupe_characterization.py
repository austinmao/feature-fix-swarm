"""Characterization pins for duplicated-helper consolidation (behavior must not change)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time

import pytest

LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))

FAMILIES = [
    ("_acceptance_policy_tables_present_tx", "ACCEPTANCE_POLICY_SCHEMA_CORRUPT",
     ["authority_acceptance_drafts", "authority_sealed_acceptances", "authority_acceptance_receipts"]),
    ("_run_policy_tables_present_tx", "RUN_POLICY_SCHEMA_CORRUPT",
     ["authority_run_policy_budgets", "authority_policy_actions", "authority_policy_action_attempts",
      "authority_policy_clock_reconciliations", "authority_policy_work_intervals"]),
    ("_frontend_policy_tables_present_tx", "FRONTEND_POLICY_SCHEMA_CORRUPT",
     ["authority_frontend_policy_states", "authority_frontend_policy_candidates",
      "authority_frontend_policy_checks", "authority_frontend_policy_findings"]),
]


def _db(tables):
    conn = sqlite3.connect(":memory:")
    for name in tables:
        conn.execute(f"CREATE TABLE {name} (x)")
    return conn


@pytest.mark.parametrize("method,code,tables", FAMILIES)
def test_table_presence_empty_complete_partial(method, code, tables):
    from run_state.state import ControlStore, ControlStoreRefused

    check = getattr(ControlStore, method)
    assert check(_db([])) is False
    assert check(_db(["unrelated"])) is False
    assert check(_db(tables)) is True
    assert check(_db(tables + ["unrelated"])) is True
    for drop in range(len(tables)):
        with pytest.raises(ControlStoreRefused) as caught:
            check(_db(tables[:drop] + tables[drop + 1:]))
        assert caught.value.code == code
    with pytest.raises(ControlStoreRefused) as caught:
        check(_db(tables[:1]))
    assert caught.value.code == code


_IDENTITY = '{"host_id":"h","boot_id":"b","pid":1,"start_token":"t"}'
_SCOPE = ('{"repository_id":"r","run_id":"run-1",%s"activity_id":"a","intent_id":"i",'
          '"generation":1,"supervisor_identity":' + _IDENTITY + '}')
_CHANNEL = '{"root":"/x",%s"capability":"' + "c" * 43 + '"}'


@pytest.mark.parametrize("kind", ["scope", "file"])
def test_bridge_duplicate_key_refusal(monkeypatch, kind):
    """A repeated key inside an otherwise valid document is refused; the same
    document without the repeat is accepted, so only the duplicate check decides."""
    from run_state import gsd_wave_bridge as bridge

    if kind == "scope":
        monkeypatch.setenv("FFS_WORKER_ENDPOINT", "endpoint")
        env, valid, repeated = "FFS_WORKER_SCOPE", _SCOPE % "", _SCOPE % '"run_id":"run-2",'
        call = bridge._scope_from_environment
    else:
        env, valid, repeated = "FFS_WORKER_FILE_CHANNEL", _CHANNEL % "", _CHANNEL % '"capability":"x",'
        call = bridge._file_channel_from_environment
    monkeypatch.setenv(env, valid)
    assert call()
    monkeypatch.setenv(env, repeated)
    with pytest.raises(bridge.GsdWaveBridgeRefused) as caught:
        call()
    assert str(caught.value) == "WAVE_CHANNEL_UNAVAILABLE"


def test_enumerate_decisions_gate_filter_store_and_view(tmp_path):
    from run_state.ownership import ControlStore, ProcessIdentity, StartRequest, reserve_resources

    path = tmp_path / "authority" / "control.sqlite3"
    store = ControlStore(path)
    ownership = reserve_resources(store, StartRequest(
        "dedupe-run", str(tmp_path / "ws"), "objective", ProcessIdentity.current(),
        repository_id="fixture-repository", planning_scope="fixture-scope",
    ))
    hashes = {"candidate": "a" * 64, "runtime": "b" * 64, "config": "c" * 64}
    for gate, status in (("review_complete", True), ("repair_authorized", False), ("review_complete", False)):
        store.record_decision(
            ownership.token, gate=gate, status=status, input_hashes=dict(hashes),
            evidence={"locator": "fixture://e", "sha256": "f" * 64}, provenance={"author": "fx"},
            expires_at="2099-01-01T00:00:00Z",
        )
    view = ControlStore.open_read_only(path)
    for source in (store, view):
        everything = list(source.enumerate_decisions())
        assert [d.gate for d in everything] == ["review_complete", "repair_authorized", "review_complete"]
        assert [d.status for d in everything] == [True, False, False]
        filtered = list(source.enumerate_decisions(gate="review_complete"))
        assert [d.id for d in filtered] == [everything[0].id, everything[2].id]
        assert list(source.enumerate_decisions(gate="nope")) == []
        assert everything[0].input_hashes == hashes and everything[0].expires_at == "2099-01-01T00:00:00Z"
    assert [d.id for d in view.enumerate_decisions()] == [d.id for d in store.enumerate_decisions()]


@pytest.mark.parametrize("func,env", [
    ("_lock_seam_hold", "GATES_TEST_HOLD_LOCK"),
    ("_takeover_pause_locked", "TAKEOVER_TEST_PAUSE_LOCKED"),
])
def test_seam_hold_publishes_identity_and_waits_for_release(tmp_path, monkeypatch, func, env):
    import gates

    fn = getattr(gates, func)
    lock = tmp_path / "lock"
    lock.write_text("")
    seam = tmp_path / "seam"
    seam.mkdir()
    fd = os.open(lock, os.O_RDONLY)
    try:
        monkeypatch.delenv(env, raising=False)
        fn(fd)  # no seam configured: immediate no-op
        assert not (seam / "held").exists()
        monkeypatch.setenv(env, str(seam))
        started = time.monotonic()
        releaser = threading.Timer(0.3, lambda: (seam / "release").write_text(""))
        releaser.start()
        fn(fd)
        releaser.join()
        assert time.monotonic() - started >= 0.25
        st = os.fstat(fd)
        assert (seam / "held").read_text() == "%d:%d" % (st.st_dev, st.st_ino)
    finally:
        os.close(fd)


def test_review_output_parse_and_codes():
    from run_state import sealed_review, spec_review
    from run_state.supervisor import SupervisorRefused

    for func, code in ((sealed_review._review_output, "FINAL_REVIEW_OUTPUT_INVALID"),
                       (spec_review._output, "SPEC_REVIEW_OUTPUT_INVALID")):
        assert func({"host_receipt": None}, b'{"a": 1}') == {"a": 1}
        for bad in (b'{"a":1,"a":2}', b"[1]", b"\xff", b"nope"):
            with pytest.raises(SupervisorRefused) as caught:
                func({"host_receipt": None}, bad)
            assert caught.value.code == code
        with pytest.raises(SupervisorRefused) as caught:
            func({"host_receipt": {"bogus": 1}}, b"{}")
        assert caught.value.code == code


def test_retained_lookup_both_modules():
    from run_state import recovery_integration, repair_integration

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE authority_event_keys (activity_id, idempotency_key, event_id, payload_hash)")
    conn.execute("CREATE TABLE control_events (id INTEGER PRIMARY KEY, payload)")
    conn.execute("INSERT INTO control_events VALUES (1, ?)", (json.dumps({"data": {"k": "v"}}),))
    conn.execute("INSERT INTO authority_event_keys VALUES ('act', 'key', 1, 'hash')")
    for func in (recovery_integration._retained, repair_integration._retained):
        assert func(conn, "act", "key") == ({"k": "v"}, "hash")
        assert func(conn, "act", "missing") is None
        assert func(conn, "other", "key") is None
