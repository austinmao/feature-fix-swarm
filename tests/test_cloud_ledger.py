"""Tests for lib/cloud_ledger.py (contract: docs/cloud-delegation-contracts.md sections 1, 2.3-2.5, 6, 14)."""

import copy
import json
import os
import stat
import threading
from pathlib import Path

import pytest

import cloud_ledger as cl

RUN = "R1"
BASE = "%040x" % 0xBA5E
SHA = "ab" * 32
LIMITS = {"claude": 2, "codex": 1, "total": 3}
FAR = 4_000_000_000


def oid(n):
    return "%040x" % n


def specs():
    return [
        {"id": "a", "provider": "claude", "owns": ["src/a/**"], "verify_cmd": "t"},
        {"id": "b", "provider": "claude", "depends_on": ["a"]},
        {"id": "c", "provider": "codex"},
    ]


def fresh_doc(task_specs=None):
    return cl.new_run(RUN, base_sha=BASE, origin="origin", origin_url="git@github.com:o/r.git", policy="auto",
                      tasks=specs() if task_specs is None else task_specs, tasks_sha256=SHA, created_at=100)


def put(doc, name, state, **fields):
    doc["tasks"][name]["state"] = state
    doc["tasks"][name].update(fields)
    return doc


def gen(doc, name="a"):
    return doc["tasks"][name]["generation"]


def reserve_data(name="a", generation=1, provider="claude", **extra):
    return dict({"key": "R1/%s/g%d" % (name, generation), "provider": provider}, **extra)


def pending_ops(doc):
    return [(e["op"], e["task"]) for e in cl.pending_journal(doc)]


# --------------------------------------------------------------------------
# locked_update / read
# --------------------------------------------------------------------------


def test_create_and_revision_bump(tmp_path):
    path = tmp_path / "runs" / "R1.json"
    written = cl.create_run_ledger(path, fresh_doc())
    assert written["revision"] == 1
    assert cl.read(path)["revision"] == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (tmp_path / "runs" / "R1.lock").exists()
    after = cl.locked_update(path, lambda d: d)
    assert after["revision"] == 2 and cl.read(path)["revision"] == 2


def test_create_refuses_existing(tmp_path):
    path = tmp_path / "R1.json"
    cl.create_run_ledger(path, fresh_doc())
    with pytest.raises(cl.LedgerError) as err:
        cl.create_run_ledger(path, fresh_doc())
    assert err.value.code == cl.E_EXISTS


def test_lock_path_for_keeps_dotted_ids():
    assert cl.lock_path_for("/x/runs/r1.2.json").name == "r1.2.lock"
    assert cl.lock_path_for("/x/providers.json").name == "providers.lock"


def test_read_errors(tmp_path):
    with pytest.raises(cl.LedgerError) as err:
        cl.read(tmp_path / "none.json")
    assert err.value.code == cl.E_MISSING
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(cl.LedgerError) as err:
        cl.read(bad)
    assert err.value.code == cl.E_CORRUPT
    nan = tmp_path / "nan.json"
    nan.write_text('{"schema":1,"revision":NaN}')
    with pytest.raises(cl.LedgerError) as err:
        cl.read(nan)
    assert err.value.code == cl.E_CORRUPT
    other = tmp_path / "other.json"
    other.write_text('{"schema":2,"revision":0}')
    with pytest.raises(cl.LedgerError) as err:
        cl.read(other)
    assert err.value.code == cl.E_SCHEMA
    norev = tmp_path / "norev.json"
    norev.write_text('{"schema":1}')
    with pytest.raises(cl.LedgerError) as err:
        cl.read(norev)
    assert err.value.code == cl.E_CORRUPT


def test_update_missing_without_create(tmp_path):
    with pytest.raises(cl.LedgerError) as err:
        cl.locked_update(tmp_path / "x.json", lambda d: d)
    assert err.value.code == cl.E_MISSING
    with pytest.raises(cl.LedgerError) as err:
        cl.locked_update(tmp_path / "nodir" / "x.json", lambda d: d)
    assert err.value.code == cl.E_MISSING
    assert not (tmp_path / "x.json").exists()


def test_revision_conflict_from_bypassing_writer(tmp_path):
    path = tmp_path / "R1.json"
    cl.create_run_ledger(path, fresh_doc())

    def rogue(doc):
        sneaky = dict(cl.read(path))
        sneaky["revision"] += 1
        sneaky["epoch"] = 99
        path.write_text(json.dumps(sneaky))
        doc["epoch"] = 5
        return doc

    with pytest.raises(cl.LedgerError) as err:
        cl.locked_update(path, rogue)
    assert err.value.code == cl.E_REVISION
    assert cl.read(path)["epoch"] == 99  # the rogue writer's file was not clobbered


def test_fn_may_not_alter_revision_or_return_junk(tmp_path):
    path = tmp_path / "R1.json"
    cl.create_run_ledger(path, fresh_doc())

    def tamper(doc):
        doc["revision"] += 5
        return doc

    with pytest.raises(cl.LedgerError) as err:
        cl.locked_update(path, tamper)
    assert err.value.code == cl.E_REVISION
    with pytest.raises(cl.LedgerError) as err:
        cl.locked_update(path, lambda d: [d])
    assert err.value.code == cl.E_BAD_UPDATE
    assert cl.read(path)["revision"] == 1


def test_fn_exception_and_unchanged_write_nothing(tmp_path):
    path = tmp_path / "R1.json"
    cl.create_run_ledger(path, fresh_doc())

    def boom(doc):
        doc["epoch"] = 7
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        cl.locked_update(path, boom)
    cl.locked_update(path, lambda d: cl.UNCHANGED)
    snap = cl.read(path)
    assert snap["epoch"] == 1 and snap["revision"] == 1


@pytest.mark.parametrize("target", ["replace", "fsync"])
def test_atomic_write_failures_leave_original_intact(tmp_path, monkeypatch, target):
    path = tmp_path / "R1.json"
    cl.create_run_ledger(path, fresh_doc())
    before = path.read_bytes()

    def explode(*args, **kwargs):
        raise OSError("injected")

    monkeypatch.setattr(os, target, explode)
    with pytest.raises(OSError):
        cl.locked_update(path, lambda d: d.update({"epoch": 9}) or d)
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert cl.read(path)["revision"] == 1
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_threads_serialize_through_the_lock(tmp_path):
    path = tmp_path / "counter.json"
    cl.locked_update(path, lambda d: d.update({"n": 0}) or d, create={"schema": 1, "revision": 0})
    errors = []

    def work():
        try:
            for _ in range(5):
                cl.locked_update(path, lambda d: d.update({"n": d["n"] + 1}) or d)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert cl.read(path)["n"] == 30 and cl.read(path)["revision"] == 31


# --------------------------------------------------------------------------
# identifiers, refs, paths
# --------------------------------------------------------------------------


@pytest.mark.parametrize("run", ["R1", "a", "run-1_x.y", "A" * 40, "1abc"])
def test_valid_run_ids(run):
    assert cl.validate_run_id(run) == run


@pytest.mark.parametrize("run", ["", "A" * 41, "-x", ".x", "a b", "a/b", "a..b", "a.", "x.lock", "x.json", "x.orch",
                                 "R1\n", "über", None, 5])
def test_invalid_run_ids(run):
    with pytest.raises(cl.LedgerError) as err:
        cl.validate_run_id(run)
    assert err.value.code == cl.E_BAD_ID


@pytest.mark.parametrize("task", ["t1", "a", "x" * 24, "0-a-b"])
def test_valid_task_ids(task):
    assert cl.validate_task_id(task) == task


@pytest.mark.parametrize("task", ["", "T1", "x" * 25, "-a", "a_b", "a.b", "a/b", "a\n", None])
def test_invalid_task_ids(task):
    with pytest.raises(cl.LedgerError):
        cl.validate_task_id(task)


@pytest.mark.parametrize("value", [0, -1, 1000000, True, "1", 1.0])
def test_generation_and_epoch_bounds(value):
    with pytest.raises(cl.LedgerError):
        cl.validate_generation(value)
    with pytest.raises(cl.LedgerError):
        cl.validate_epoch(value)


def test_ref_builders():
    assert cl.task_branch_ref("R1", "t1", 2) == "refs/heads/ffs/cloud/R1/t1-g2"
    assert cl.task_branch_name("R1", "t1", 2) == "ffs/cloud/R1/t1-g2"
    assert cl.integration_ref("R1") == "refs/heads/ffs/cloud/R1/integration"
    assert cl.candidate_ref("R1", "t1", 2) == "refs/ffs/cloud/R1/cand/t1-g2"
    assert cl.publication_ref("R1", "t1", 2) == "refs/ffs/cloud/R1/pub/t1-g2"
    assert cl.lifecycle_id("R1", 3) == "cloud-R1-e3"
    assert cl.claude_result_branch("R1", "t1", "0123456789abcdef" * 2) == "claude/ffs-R1-t1-01234567"
    assert cl.slot_key("R1", "t1", 2) == "R1/t1/g2"
    with pytest.raises(cl.LedgerError):
        cl.claude_result_branch("R1", "t1", "short")


def test_lifecycle_id_leaves_room_for_the_reconcile_prefix():
    # reconcile.sh names its record "reconcile-<lifecycle id>" (<= 64 bytes)
    assert len(cl.lifecycle_id("A" * 40, 999999)) == 54
    assert len(("reconcile-" + cl.lifecycle_id("A" * 40, 999999)).encode()) == 64
    with pytest.raises(cl.LedgerError):
        cl.lifecycle_id("A" * 40, 1000000)


def test_task_branch_names_cannot_collide_across_generations():
    # "<T>-g<G>" parses uniquely from the right, and no name equals ``integration``.
    assert cl.task_branch_name("R", "t1-g2", 1) != cl.task_branch_name("R", "t1", 2)
    assert not cl.task_branch_name("R", "integration", 1).endswith("/integration")


def test_paths(tmp_path):
    base = tmp_path / ".feature-fix-swarm" / "cloud"
    assert cl.state_dir(tmp_path) == base
    assert cl.run_ledger_path(tmp_path, "R1") == base / "runs" / "R1.json"
    assert cl.run_dir(tmp_path, "R1") == base / "runs" / "R1"
    assert cl.slots_path(tmp_path) == base / "providers.json"
    assert cl.orch_lock_path(tmp_path, "R1") == base / "runs" / "R1.orch.lock"
    assert cl.tasks_path(tmp_path, "R1") == base / "runs" / "R1" / "tasks.json"
    assert cl.scan_cache_path(tmp_path) == base / "scan-cache.json"
    with pytest.raises(cl.LedgerError):
        cl.run_ledger_path(tmp_path, "../x")
    # a run id can never collide with the slots ledger or with another run's lock
    assert cl.lock_path_for(cl.run_ledger_path(tmp_path, "providers")) != cl.lock_path_for(cl.slots_path(tmp_path))
    assert cl.orch_lock_path(tmp_path, "x") != cl.lock_path_for(cl.run_ledger_path(tmp_path, "x"))


def test_remote_id_and_url_sanitizers():
    assert cl.sanitize_remote_id(None) is None
    assert cl.sanitize_remote_id("session_01:abc/x-y.z") == "session_01:abc/x-y.z"
    assert cl.sanitize_url("https://claude.ai/code/session_1") == "https://claude.ai/code/session_1"
    for bad in ["", "a b", "a?b=c", "x" * 201, "a\n", "token=abc", 5]:
        with pytest.raises(cl.LedgerError):
            cl.sanitize_remote_id(bad)
        with pytest.raises(cl.LedgerError):
            cl.sanitize_url(bad)
        assert cl.safe_remote_id(bad) is None and cl.safe_url(bad) is None
    assert cl.safe_remote_id("ok-1") == "ok-1"


# --------------------------------------------------------------------------
# new_run and tasks.json
# --------------------------------------------------------------------------


def test_new_run_shape():
    doc = fresh_doc()
    assert doc["schema"] == 1 and doc["revision"] == 0 and doc["state"] == "active"
    assert doc["epoch"] == 1 and doc["wake_seq"] == 0 and doc["tasks_sha256"] == SHA
    assert doc["origin"] == "origin" and doc["origin_url"] == "git@github.com:o/r.git"
    assert doc["integration"] == {"tip": BASE, "history": []} and doc["journal"] == []
    assert list(doc["tasks"]) == ["a", "b", "c"]
    for name, entry in doc["tasks"].items():
        assert entry["state"] == cl.WAITING and entry["generation"] == 1 and entry["blocker"] is None
        assert len(entry["nonce"]) == 32 and entry["task_branch"] == "refs/heads/ffs/cloud/R1/%s-g1" % name
        assert entry["rejected_candidates"] == [] and entry["decisions_needed"] == []
    assert doc["tasks"]["b"]["depends_on"] == ["a"]
    assert len({e["nonce"] for e in doc["tasks"].values()}) == 3
    json.dumps(doc)


def test_new_run_never_stores_prompts():
    task_specs = specs()
    task_specs[0]["prompt"] = "do the secret thing"
    assert "secret thing" not in json.dumps(fresh_doc(task_specs))


@pytest.mark.parametrize("mutate", [
    lambda s: s.clear(),
    lambda s: s.append(dict(s[0])),
    lambda s: s[0].update(provider="gemini"),
    lambda s: s[1].update(depends_on=["zzz"]),
    lambda s: s[0].update(depends_on=["a"]),
    lambda s: s[0].update(depends_on=["b"]),
    lambda s: s[0].update(id="BAD"),
    lambda s: s[0].update(owns="src"),
])
def test_new_run_rejects_bad_tasks(mutate):
    task_specs = specs()
    mutate(task_specs)
    with pytest.raises(cl.LedgerError) as err:
        fresh_doc(task_specs)
    assert err.value.code in (cl.E_BAD_TASKS, cl.E_BAD_ID)


@pytest.mark.parametrize("kwargs", [
    {"base_sha": "xyz"}, {"policy": "yolo"}, {"origin_url": "has space"}, {"origin": ""}, {"tasks_sha256": "abc"},
])
def test_new_run_rejects_bad_arguments(kwargs):
    args = dict(base_sha=BASE, origin="origin", origin_url="u", policy="auto", tasks=specs(), tasks_sha256=SHA)
    args.update(kwargs)
    with pytest.raises(cl.LedgerError):
        cl.new_run(RUN, **args)


def test_tasks_file_roundtrip_and_hash_mismatch(tmp_path):
    path = cl.tasks_path(tmp_path, RUN)
    tasks = [{"id": "a", "prompt": "p", "owns": []}]
    digest = cl.write_tasks_file(path, tasks)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and len(digest) == 64
    assert cl.load_tasks_file(path, digest) == tasks
    path.write_text(path.read_text().replace('"p"', '"q"'))
    with pytest.raises(cl.LedgerError) as err:
        cl.load_tasks_file(path, digest)
    assert err.value.code == cl.E_TASK_INPUTS
    path.unlink()
    with pytest.raises(cl.LedgerError) as err:
        cl.load_tasks_file(path, digest)
    assert err.value.code == cl.E_TASK_INPUTS
    with pytest.raises(cl.LedgerError):
        cl.write_tasks_file(path, {"not": "a list"})


# --------------------------------------------------------------------------
# transition table
# --------------------------------------------------------------------------

EXTRA = {
    cl.EV_LAUNCH_OK: {"launched_at": 5, "deadline_at": 50},
    cl.EV_BIND: {"bound_oid": oid(1)},
    cl.EV_ADOPT: {"bound_oid": oid(1)},
    cl.EV_ADOPT_REMOTE: {"remote_id": "task_1", "launched_at": 5, "deadline_at": 50},
    cl.EV_COLLECT_OK: {"candidate_oid": oid(2)},
    cl.EV_REVIEW_APPROVE: {"review": {"family": "codex", "verdict": "APPROVE", "diff_sha": "d", "patch_id": "p", "tree": "t"}},
    cl.EV_TESTS_PASS: {"verify": {"test_rc": 0, "test_log_sha": "ab"}},
}
OPERATOR_PRECONDITIONS = {
    cl.EV_ADOPT: ("uncertain-launch", "launched"),
    cl.EV_ADOPT_REMOTE: ("uncertain-launch", "launched"),
    cl.EV_RETEST: ("review-cap", "collected"),
    cl.EV_ACCEPT_DEPS: ("dependency-change", "result-ready"),
    cl.EV_OPERATOR_REJECT: ("path-outside-owns", "collected"),
}


def build(state, event):
    doc = fresh_doc()
    subject, kwargs = "a", dict(EXTRA.get(event, {}))
    if event in (cl.EV_DEPS_MET, cl.EV_DEP_TERMINAL, cl.EV_DEP_REOPEN):
        subject = "b"
    if event == cl.EV_DEPS_MET:
        put(doc, "a", cl.INTEGRATED)
    if event == cl.EV_DEP_TERMINAL:
        put(doc, "a", cl.REJECTED)
        kwargs["blocker"] = "dependency-rejected"
    if event == cl.EV_DEP_REOPEN:
        put(doc, "a", cl.REJECTED)
    put(doc, subject, state)
    entry = doc["tasks"][subject]
    if event == cl.EV_DEP_REOPEN:
        entry["blocker"] = "dependency-rejected"
    if state == cl.NEEDS_OPERATOR:
        blocker, resume = OPERATOR_PRECONDITIONS.get(event, ("poll-errors", "launched"))
        entry["blocker"], entry["resume_state"] = blocker, resume
    allowed = cl.TRANSITIONS[(state, event)][1]
    if len(allowed) > 1 and event != cl.EV_DEP_TERMINAL:
        kwargs["blocker"] = sorted(allowed)[0]
    return doc, subject, kwargs


def test_table_is_complete_and_well_formed():
    assert len(cl.TRANSITIONS) >= 60
    for (source, event), (dest, blockers) in cl.TRANSITIONS.items():
        assert source in cl.ALL_STATES and (dest in cl.ALL_STATES or dest == cl.RESUME)
        assert source not in cl.TERMINAL or event in (cl.EV_ABANDON_RETRY, cl.EV_DEP_REOPEN)
        assert blockers <= cl.ALL_BLOCKERS
    assert cl.ACTIVE_SET == {"reserved", "submitting", "launched", "running", "uncertain"}
    assert cl.TERMINAL == {"integrated", "rejected", "failed", "abandoned", "superseded"}
    assert cl.NEEDS_OPERATOR not in cl.TERMINAL and cl.UNCERTAIN not in cl.TERMINAL


@pytest.mark.parametrize("key", sorted(cl.TRANSITIONS))
def test_every_legal_transition(key):
    state, event = key
    doc, subject, kwargs = build(state, event)
    old = copy.deepcopy(doc["tasks"][subject])
    dest, allowed = cl.TRANSITIONS[key]
    cl.transition(doc, subject, event, generation=old["generation"], **kwargs)
    entry = doc["tasks"][subject]
    if dest == cl.RESUME:
        dest = old["resume_state"]
    if event in (cl.EV_ABANDON, cl.EV_ABANDON_RETRY):
        assert entry["generation"] == old["generation"] + 1 and entry["nonce"] != old["nonce"]
    assert entry["state"] == dest
    if event == cl.EV_OPERATOR_REJECT:
        assert entry["blocker"] == old["blocker"]
    elif dest in (cl.NEEDS_OPERATOR, cl.REJECTED, cl.FAILED):
        assert entry["blocker"] == (kwargs.get("blocker") or (next(iter(allowed)) if allowed else None))
    else:
        assert entry["blocker"] is None
    assert entry["resume_state"] == ((cl.VERIFIED if state == cl.INTEGRATING else state) if dest == cl.NEEDS_OPERATOR else None)


@pytest.mark.parametrize("state", sorted(cl.ALL_STATES))
def test_every_illegal_move_is_rejected_without_change(state):
    illegal = [e for e in sorted(cl.EVENT_FIELDS.keys() | {ev for (_s, ev) in cl.TRANSITIONS}) if (state, e) not in cl.TRANSITIONS]
    assert illegal
    for event in illegal:
        doc = put(fresh_doc(), "a", state)
        before = copy.deepcopy(doc)
        with pytest.raises(cl.LedgerError) as err:
            cl.transition(doc, "a", event, generation=1)
        assert err.value.code == cl.E_ILLEGAL
        assert doc == before


def test_unknown_event_and_task():
    doc = fresh_doc()
    with pytest.raises(cl.LedgerError) as err:
        cl.transition(doc, "a", "teleport", generation=1)
    assert err.value.code == cl.E_ILLEGAL
    with pytest.raises(cl.LedgerError) as err:
        cl.transition(doc, "zzz", cl.EV_DEPS_MET, generation=1)
    assert err.value.code == cl.E_UNKNOWN_TASK


def test_terminal_states_only_reopen_by_retry():
    for state in cl.TERMINAL:
        doc = put(fresh_doc(), "a", state)
        before = copy.deepcopy(doc)
        with pytest.raises(cl.LedgerError):
            cl.apply_abandon(doc, "a", retry=False)
        assert doc == before
    for state in (cl.INTEGRATED, cl.ABANDONED, cl.SUPERSEDED):
        with pytest.raises(cl.LedgerError):
            cl.apply_abandon(put(fresh_doc(), "a", state), "a", retry=True)


def test_generation_fence_leaves_state_untouched():
    doc = put(fresh_doc(), "a", cl.LAUNCHED, launched_at=1, deadline_at=2)
    before = copy.deepcopy(doc)
    for stale in (0, 2, True, "1", None):
        with pytest.raises(cl.LedgerError) as err:
            cl.transition(doc, "a", cl.EV_BIND, generation=stale, bound_oid=oid(1))
        assert err.value.code == cl.E_STALE
        assert doc == before
    with pytest.raises(cl.LedgerError) as err:
        cl.note_poll(doc, "a", generation=2, ok=True)
    assert err.value.code == cl.E_STALE
    cl.apply_abandon(doc, "a", retry=True)  # generation is now 2
    with pytest.raises(cl.LedgerError) as err:
        cl.transition(doc, "a", cl.EV_DEPS_MET, generation=1)
    assert err.value.code == cl.E_STALE
    with pytest.raises(cl.LedgerError) as err:
        cl.journal_begin(doc, cl.OP_ADMIT, "a", 1, {"base_oid": oid(1)})
    assert err.value.code == cl.E_STALE


def test_generation_is_required_keyword():
    with pytest.raises(TypeError):
        cl.transition(fresh_doc(), "a", cl.EV_DEPS_MET)  # type: ignore[call-arg]


def test_field_and_blocker_validation():
    doc = put(fresh_doc(), "a", cl.LAUNCHED)
    before = copy.deepcopy(doc)
    cases = [
        (cl.EV_BIND, {}, cl.E_BAD_FIELD),
        (cl.EV_BIND, {"bound_oid": "nothex"}, cl.E_BAD_FIELD),
        (cl.EV_BIND, {"bound_oid": oid(1), "state": "x"}, cl.E_BAD_FIELD),
        (cl.EV_BIND, {"bound_oid": oid(1), "blocker": "deadline"}, cl.E_BAD_BLOCKER),
        (cl.EV_POLL_RUNNING, {"remote_id": "a b"}, cl.E_BAD_FIELD),
        (cl.EV_NEEDS_OPERATOR, {}, cl.E_BAD_BLOCKER),
        (cl.EV_NEEDS_OPERATOR, {"blocker": "tests-failed"}, cl.E_BAD_BLOCKER),
        (cl.EV_DEADLINE, {"blocker": "poll-errors"}, cl.E_BAD_BLOCKER),
    ]
    for event, fields, code in cases:
        with pytest.raises(cl.LedgerError) as err:
            cl.transition(doc, "a", event, generation=1, **fields)
        assert err.value.code == code, (event, fields)
        assert doc == before
    cl.transition(doc, "a", cl.EV_DEADLINE, generation=1)  # single allowed blocker is implied
    assert doc["tasks"]["a"]["blocker"] == "deadline"


def test_dependency_guards():
    doc = fresh_doc()
    with pytest.raises(cl.LedgerError):
        cl.transition(doc, "b", cl.EV_DEPS_MET, generation=1)  # a not integrated
    put(doc, "a", cl.REJECTED)
    with pytest.raises(cl.LedgerError):
        cl.transition(doc, "b", cl.EV_DEP_TERMINAL, generation=1, blocker="dependency-failed")  # wrong state named
    with pytest.raises(cl.LedgerError):
        cl.transition(doc, "b", cl.EV_DEPS_MET, generation=1)
    assert cl.propagate_dependency_failures(doc) == ["b"]
    assert doc["tasks"]["b"]["state"] == cl.FAILED and doc["tasks"]["b"]["blocker"] == "dependency-rejected"
    with pytest.raises(cl.LedgerError):
        cl.transition(put(fresh_doc(), "a", cl.FAILED), "a", cl.EV_DEP_REOPEN, generation=1)  # not a dependency failure


def test_propagation_is_transitive():
    doc = fresh_doc([{"id": "a", "provider": "claude"}, {"id": "b", "provider": "claude", "depends_on": ["a"]},
                     {"id": "c", "provider": "claude", "depends_on": ["b"]}])
    put(doc, "a", cl.ABANDONED)
    assert cl.propagate_dependency_failures(doc) == ["b", "c"]
    assert doc["tasks"]["c"]["blocker"] == "dependency-failed"


def test_launch_and_bind_fields_are_recorded():
    doc = put(fresh_doc(), "a", cl.SUBMITTING)
    cl.transition(doc, "a", cl.EV_LAUNCH_OK, generation=1, launched_at=10, deadline_at=99, remote_id="s_1",
                  url="https://claude.ai/code/s_1")
    entry = doc["tasks"]["a"]
    assert (entry["state"], entry["launched_at"], entry["deadline_at"], entry["remote_id"]) == ("launched", 10, 99, "s_1")
    cl.transition(doc, "a", cl.EV_POLL_RUNNING, generation=1)
    cl.transition(doc, "a", cl.EV_BIND, generation=1, bound_oid=oid(7))
    assert entry["state"] == cl.RESULT_READY and entry["bound_oid"] == oid(7)


def test_first_bind_wins():
    doc = put(fresh_doc(), "a", cl.RUNNING, bound_oid=oid(1))
    with pytest.raises(cl.LedgerError):
        cl.transition(doc, "a", cl.EV_BIND, generation=1, bound_oid=oid(2))


def test_manifest_fields_land_with_worker_source():
    doc = put(fresh_doc(), "a", cl.RESULT_READY)
    report = {"tools": {"node": "v22"}, "present": ["API_KEY"], "missing": []}
    cl.transition(doc, "a", cl.EV_NEEDS_OPERATOR, generation=1, blocker="manifest-blocked",
                  decisions_needed=["which db?"], env_report=report)
    entry = doc["tasks"]["a"]
    assert entry["env_report"] == report and entry["env_report_source"] == "worker"
    assert entry["decisions_needed"] == ["which db?"]
    with pytest.raises(cl.LedgerError):
        cl.transition(put(fresh_doc(), "a", cl.RESULT_READY), "a", cl.EV_COLLECT_OK, generation=1,
                      candidate_oid=oid(1), env_report={"tools": {}, "present": ["lower"], "missing": []})


def test_poll_bookkeeping():
    doc = put(fresh_doc(), "a", cl.RUNNING)
    entry = doc["tasks"]["a"]
    for expected in range(1, cl.POLL_ERROR_LIMIT + 2):
        assert cl.note_poll(doc, "a", generation=1, ok=False) == expected
    assert cl.poll_errors_exceeded(entry)
    assert cl.note_poll(doc, "a", generation=1, ok=True) == 0 and not cl.poll_errors_exceeded(entry)
    assert cl.note_seen(doc, "a", generation=1, oid=oid(1)) == 1 and not cl.bind_condition_met(entry)
    assert cl.note_seen(doc, "a", generation=1, oid=oid(1)) == 2 and cl.bind_condition_met(entry)
    assert cl.note_seen(doc, "a", generation=1, oid=oid(2)) == 1
    assert cl.note_seen(doc, "a", generation=1, oid=None) == 0 and not cl.bind_condition_met(entry)
    cl.reject_candidate(doc, "a", generation=1, ref="refs/heads/claude/x", oid=oid(3))
    cl.reject_candidate(doc, "a", generation=1, ref="refs/heads/claude/x", oid=oid(3))
    assert entry["rejected_candidates"] == [["refs/heads/claude/x", oid(3)]]
    with pytest.raises(cl.LedgerError):
        cl.note_poll(put(fresh_doc(), "a", cl.WAITING), "a", generation=1, ok=True)


def test_recover_submitting_goes_uncertain():
    doc = put(fresh_doc(), "a", cl.SUBMITTING)
    put(doc, "c", cl.LAUNCHED)
    assert cl.recover_submitting(doc) == ["a"]
    assert doc["tasks"]["a"]["state"] == cl.UNCERTAIN and doc["tasks"]["c"]["state"] == cl.LAUNCHED


# --------------------------------------------------------------------------
# automatic slot release and the operator verbs
# --------------------------------------------------------------------------


def test_release_is_journaled_exactly_when_a_slot_may_be_dropped():
    holding_to_nonholding = [
        (cl.SUBMITTING, cl.EV_LAUNCH_FAILED, {}), (cl.LAUNCHED, cl.EV_BIND, {"bound_oid": oid(1)}),
        (cl.RUNNING, cl.EV_DEADLINE, {}),
    ]
    for state, event, fields in holding_to_nonholding:
        doc = put(fresh_doc(), "a", state)
        cl.transition(doc, "a", event, generation=1, **fields)
        assert pending_ops(doc) == [(cl.OP_RELEASE, "a")], (state, event)
        assert cl.pending_journal(doc)[0]["data"]["key"] == "R1/a/g1"
    keeps = [(cl.LAUNCHED, cl.EV_POLL_RUNNING), (cl.RUNNING, cl.EV_POLL_ERRORS), (cl.UNCERTAIN, cl.EV_UNCERTAIN_TIMEOUT),
             (cl.UNCERTAIN, cl.EV_LAUNCH_FOUND), (cl.RESERVED, cl.EV_SUBMIT), (cl.ADMITTED, cl.EV_CAPACITY)]
    for state, event in keeps:
        doc = put(fresh_doc(), "a", state)
        cl.transition(doc, "a", event, generation=1)
        assert pending_ops(doc) == [], (state, event)


def test_abandon_releases_the_old_generation_key_and_bumps_nonce():
    for state in (cl.ADMITTED, cl.RESERVED, cl.LAUNCHED, cl.NEEDS_OPERATOR, cl.UNCERTAIN):
        doc = put(fresh_doc(), "a", state, resume_state="launched")
        old_nonce = doc["tasks"]["a"]["nonce"]
        cl.apply_abandon(doc, "a", nonce="f" * 32)
        entry = doc["tasks"]["a"]
        assert (entry["state"], entry["generation"], entry["nonce"]) == (cl.ABANDONED, 2, "f" * 32) and old_nonce != "f" * 32
        assert entry["task_branch"] == "refs/heads/ffs/cloud/R1/a-g2"
        assert [(e["op"], e["generation"], e["data"]["key"]) for e in cl.pending_journal(doc)] == [(cl.OP_RELEASE, 1, "R1/a/g1")]
        assert doc["wake_seq"] == 1
    doc = put(fresh_doc(), "a", cl.WAITING)
    cl.apply_abandon(doc, "a", retry=True)
    assert doc["tasks"]["a"]["state"] == cl.WAITING and doc["tasks"]["a"]["generation"] == 2 and pending_ops(doc) == []


def test_abandon_retry_from_rejected_failed_and_reopens_dependents():
    doc = put(fresh_doc(), "a", cl.REJECTED, blocker="tests-failed", candidate_oid=oid(9), rejected_candidates=[["r", oid(1)]])
    cl.propagate_dependency_failures(doc)
    assert doc["tasks"]["b"]["state"] == cl.FAILED
    cl.apply_abandon(doc, "a", retry=True)
    entry = doc["tasks"]["a"]
    assert entry["state"] == cl.WAITING and entry["blocker"] is None and entry["candidate_oid"] is None
    assert entry["rejected_candidates"] == [["r", oid(1)]]  # kept across generations
    assert doc["tasks"]["b"]["state"] == cl.WAITING and doc["tasks"]["b"]["blocker"] is None
    assert doc["tasks"]["b"]["generation"] == 1
    failed = put(fresh_doc(), "c", cl.FAILED, blocker="deadline")
    cl.apply_abandon(failed, "c", retry=True)
    assert failed["tasks"]["c"]["state"] == cl.WAITING


def test_plain_abandon_does_not_reopen_dependents():
    doc = put(fresh_doc(), "a", cl.LAUNCHED)
    cl.apply_abandon(doc, "a")
    cl.propagate_dependency_failures(doc)
    assert doc["tasks"]["b"]["blocker"] == "dependency-abandoned"


def test_operator_mutations_refuse_done_runs():
    doc = put(fresh_doc(), "a", cl.NEEDS_OPERATOR, blocker="poll-errors")
    doc["state"] = cl.RUN_DONE
    for call in (lambda: cl.apply_adopt(doc, "a", oid(1)), lambda: cl.apply_abandon(doc, "a"),
                 lambda: cl.apply_resolve(doc, "a", "reject")):
        with pytest.raises(cl.LedgerError) as err:
            call()
        assert err.value.code == cl.E_RUN_DONE
    assert doc["wake_seq"] == 0


RESUME_FOR = {"review-unavailable": "collected", "review-cap": "collected", "review-model-downgraded": "collected",
              "review-model-unknown": "collected", "sandbox-unavailable": "reviewed",
              "merge-conflict": "verified", "patch-id-mismatch": "verified", "exposure-not-granted": "reserved",
              "ruleset-unverified": "reserved", "grant-missing": "reserved", "branch-conflict": "waiting",
              "dependency-change": "result-ready", "scan-refused": "reserved", "push-failed": "waiting",
              "integration-ref-foreign": "verified", "binary-change": "collected", "internal-error": "collected"}


@pytest.mark.parametrize("blocker", sorted(cl.ALL_BLOCKERS))
@pytest.mark.parametrize("action", sorted(cl.RESOLVE_ACTIONS))
def test_resolve_table_exhaustively(blocker, action):
    doc = put(fresh_doc(), "a", cl.NEEDS_OPERATOR, blocker=blocker, resume_state=RESUME_FOR.get(blocker, "collected"))
    before = copy.deepcopy(doc)
    if blocker not in cl.RESOLVE_ACTIONS[action]:
        with pytest.raises(cl.LedgerError) as err:
            cl.apply_resolve(doc, "a", action)
        assert err.value.code == cl.E_BLOCKER_MISMATCH and doc == before
        return
    cl.apply_resolve(doc, "a", action)
    entry = doc["tasks"]["a"]
    assert doc["wake_seq"] == 1
    if action == "retest":
        assert entry["state"] == RESUME_FOR[blocker] and entry["blocker"] is None
    elif action == "accept-deps":
        assert entry["state"] == cl.RESULT_READY and entry["deps_accepted"] is True and entry["blocker"] is None
    else:
        assert entry["state"] == cl.REJECTED and entry["blocker"] == blocker


def test_resolve_documented_rows():
    assert cl.RESOLVE_ACTIONS["accept-deps"] == {"dependency-change"}
    assert {"secret-leak", "tests-failed", "review-revise", "manifest-blocked", "uncertain-launch", "poll-errors",
            "ambiguous-candidates", "candidate-moved"}.isdisjoint(set().union(*cl.RESOLVE_ACTIONS.values()))
    assert {"exposure-not-granted", "ruleset-unverified", "grant-missing"} <= cl.RESOLVE_ACTIONS["retest"]
    assert {"exposure-not-granted", "ruleset-unverified", "grant-missing"}.isdisjoint(cl.RESOLVE_ACTIONS["reject"])


def test_resolve_refuses_unknown_action_and_wrong_state():
    doc = put(fresh_doc(), "a", cl.NEEDS_OPERATOR, blocker="review-cap", resume_state="collected")
    with pytest.raises(cl.LedgerError) as err:
        cl.apply_resolve(doc, "a", "merge")
    assert err.value.code == cl.E_BAD_ARGUMENT
    with pytest.raises(cl.LedgerError) as err:
        cl.apply_resolve(fresh_doc(), "a", "retest")
    assert err.value.code == cl.E_BLOCKER_MISMATCH


def test_resolve_retest_returns_to_the_failing_check_from_a_real_flow():
    doc = put(fresh_doc(), "a", cl.INTEGRATING)
    cl.transition(doc, "a", cl.EV_NEEDS_OPERATOR, generation=1, blocker="patch-id-mismatch")
    assert doc["tasks"]["a"]["resume_state"] == cl.VERIFIED
    cl.apply_resolve(doc, "a", "retest")
    assert doc["tasks"]["a"]["state"] == cl.VERIFIED
    doc = put(fresh_doc(), "a", cl.RESERVED)
    cl.transition(doc, "a", cl.EV_NEEDS_OPERATOR, generation=1, blocker="exposure-not-granted")
    assert pending_ops(doc) == []  # the slot is kept while the operator fixes grants
    cl.apply_resolve(doc, "a", "retest")
    assert doc["tasks"]["a"]["state"] == cl.RESERVED and pending_ops(doc) == []
    doc = put(fresh_doc(), "a", cl.WAITING)
    cl.transition(doc, "a", cl.EV_NEEDS_OPERATOR, generation=1, blocker="grant-missing")
    cl.apply_resolve(doc, "a", "retest")
    assert doc["tasks"]["a"]["state"] == cl.WAITING


@pytest.mark.parametrize("blocker", sorted(cl.ALL_BLOCKERS))
def test_adopt_table_exhaustively(blocker):
    doc = put(fresh_doc(), "a", cl.NEEDS_OPERATOR, blocker=blocker, resume_state="launched", seen_oid=oid(5), seen_count=2)
    before = copy.deepcopy(doc)
    if blocker not in cl.ADOPT_BLOCKERS:
        with pytest.raises(cl.LedgerError) as err:
            cl.apply_adopt(doc, "a", oid(1))
        assert err.value.code == cl.E_BLOCKER_MISMATCH and doc == before
        return
    cl.apply_adopt(doc, "a", oid(1))
    entry = doc["tasks"]["a"]
    assert (entry["state"], entry["bound_oid"], entry["blocker"], entry["seen_count"]) == (cl.RESULT_READY, oid(1), None, 0)
    assert doc["wake_seq"] == 1 and pending_ops(doc) == [(cl.OP_RELEASE, "a")]


def test_adopt_from_uncertain_with_null_blocker_and_bad_oid():
    doc = put(fresh_doc(), "a", cl.UNCERTAIN)
    with pytest.raises(cl.LedgerError):
        cl.apply_adopt(doc, "a", "nothex")
    assert doc["wake_seq"] == 0 and doc["tasks"]["a"]["state"] == cl.UNCERTAIN
    cl.apply_adopt(doc, "a", oid(4))
    assert doc["tasks"]["a"]["state"] == cl.RESULT_READY
    with pytest.raises(cl.LedgerError):
        cl.apply_adopt(fresh_doc(), "a", oid(4))  # waiting task


def test_bump_epoch_and_heartbeat():
    doc = fresh_doc()
    assert cl.bump_epoch(doc) == 2 and doc["epoch"] == 2
    doc["epoch"] = cl.MAX_EPOCH
    with pytest.raises(cl.LedgerError) as err:
        cl.bump_epoch(doc)
    assert err.value.code == cl.E_EPOCH and doc["epoch"] == cl.MAX_EPOCH
    assert cl.touch_heartbeat(doc, 55)["heartbeat_at"] == 55


# --------------------------------------------------------------------------
# journal
# --------------------------------------------------------------------------


def test_journal_begin_done_pending():
    doc = fresh_doc()
    first = cl.journal_begin(doc, cl.OP_ADMIT, "a", 1, {"base_oid": BASE})
    second = cl.journal_begin(doc, cl.OP_PARK, None, None, {})
    assert (first, second) == (1, 2)
    assert [e["seq"] for e in cl.pending_journal(doc)] == [1, 2]
    cl.journal_done(doc, first)
    cl.journal_done(doc, first)  # idempotent
    assert [e["seq"] for e in cl.pending_journal(doc)] == [2]
    assert cl.journal_begin(doc, cl.OP_RESERVE, "a", 1, reserve_data()) == 3  # seq never reused
    with pytest.raises(cl.LedgerError):
        cl.journal_done(doc, 99)
    with pytest.raises(cl.LedgerError):
        cl.journal_begin(doc, "mystery", "a", 1, {})
    with pytest.raises(cl.LedgerError):
        cl.journal_begin(doc, cl.OP_ADMIT, "nope", 1, {})
    with pytest.raises(cl.LedgerError):
        cl.journal_begin(doc, cl.OP_ADMIT, "a", 1, [])  # type: ignore[arg-type]


def test_transition_marks_journal_done_in_the_same_mutation():
    doc = put(fresh_doc(), "a", cl.ADMITTED)
    seq = cl.journal_begin(doc, cl.OP_RESERVE, "a", 1, reserve_data())
    other = cl.journal_begin(doc, cl.OP_RESERVE, "c", 1, reserve_data("c", provider="codex"))
    with pytest.raises(cl.LedgerError):
        cl.transition(doc, "a", cl.EV_CAPACITY, generation=1, journal_seq=other)  # someone else's entry
    assert doc["tasks"]["a"]["state"] == cl.ADMITTED
    cl.transition(doc, "a", cl.EV_CAPACITY, generation=1, journal_seq=seq)
    assert doc["tasks"]["a"]["state"] == cl.RESERVED and [e["seq"] for e in cl.pending_journal(doc)] == [other]


def test_journal_staleness():
    doc = put(fresh_doc(), "a", cl.RESERVED)
    seq = cl.journal_begin(doc, cl.OP_RESERVE, "a", 1, reserve_data())
    entry = cl.pending_journal(doc)[0]
    assert not cl.journal_is_stale(doc, entry)
    cl.apply_abandon(doc, "a", retry=True)
    assert cl.journal_is_stale(doc, cl.pending_journal(doc)[0])
    release = [e for e in cl.pending_journal(doc) if e["op"] == cl.OP_RELEASE][0]
    assert not cl.journal_is_stale(doc, release) and seq == 1


def test_replay_decisions_are_pure():
    reserve = {"op": "reserve", "data": {"key": "R1/a/g1"}}
    assert cl.replay_reserve(reserve, True) == "mark-done" and cl.replay_reserve(reserve, False) == "add-slot"
    assert cl.replay_release(reserve, True) == "remove-slot" and cl.replay_release(reserve, False) == "mark-done"
    admit = {"data": {"base_oid": oid(1)}}
    assert cl.replay_admit(admit, None) == "push-again"
    assert cl.replay_admit(admit, oid(1)) == "admitted"
    assert cl.replay_admit(admit, oid(2)) == "needs-operator"
    integrate = {"data": {"publish_oid": oid(2), "expected_tip": oid(1)}}
    assert cl.replay_integrate(integrate, oid(2)) == "complete"
    assert cl.replay_integrate(integrate, oid(1)) == "update-ref"
    assert cl.replay_integrate(integrate, oid(9)) == "replan"
    assert cl.replay_integrate(integrate, None) == "replan"


def test_replay_integrate_accepts_only_exact_or_known_descendants():
    integrate = {"data": {"publish_oid": oid(2), "expected_tip": oid(1)}}
    known = frozenset([oid(2), oid(3)])
    # the ref is exactly the journaled publish commit
    assert cl.replay_integrate(integrate, oid(2), publish_is_ancestor=False, known=known) == "complete"
    # a descendant that is itself a publish_oid this ledger recorded
    assert cl.replay_integrate(integrate, oid(3), publish_is_ancestor=True, known=known) == "complete-known"
    # a descendant somebody else added: never accepted, never recorded as the tip
    assert cl.replay_integrate(integrate, oid(7), publish_is_ancestor=True, known=known) == "foreign"
    assert cl.replay_integrate(integrate, oid(7), publish_is_ancestor=True) == "foreign"
    # not a descendant at all: re-plan
    assert cl.replay_integrate(integrate, oid(7), publish_is_ancestor=False, known=known) == "replan"


def test_known_publish_oids_reads_only_recorded_publications():
    doc = put(fresh_doc(), "a", cl.VERIFIED)
    doc["integration"]["history"].append({"task": "c", "generation": 1, "publish_oid": oid(5)})
    seq = cl.begin_integration(doc, "a", generation=1, expected_tip=BASE, candidate_oid=oid(6), publish_oid=oid(7), tree=oid(8))
    # an open (unpublished) intent is not a recorded publication
    assert cl.known_publish_oids(doc) == {oid(5)}
    # neither is an intent whose compare-and-swap failed and was closed for replanning
    cl.transition(doc, "a", cl.EV_REPLAN, generation=1, journal_seq=seq)
    assert cl.known_publish_oids(doc) == {oid(5)}


def test_begin_and_complete_integration_in_one_write(tmp_path):
    path = tmp_path / "R1.json"
    doc = put(fresh_doc(), "a", cl.VERIFIED)
    cl.create_run_ledger(path, doc)
    publish, cand, tree = oid(10), oid(11), oid(12)
    seq_box = []

    def begin(d):
        seq_box.append(cl.begin_integration(d, "a", generation=1, expected_tip=BASE, candidate_oid=cand,
                                            publish_oid=publish, tree=tree))
        return d

    cl.locked_update(path, begin)
    mid = cl.read(path)
    assert mid["tasks"]["a"]["state"] == cl.INTEGRATING and pending_ops(mid) == [(cl.OP_INTEGRATE, "a")]
    revision = mid["revision"]
    cl.locked_update(path, lambda d: cl.complete_integration(
        d, "a", generation=1, seq=seq_box[0], publish_oid=publish, candidate_oid=cand, tree=tree, test_rc=0,
        test_log_sha="ff"))
    final = cl.read(path)
    assert final["revision"] == revision + 1  # exactly one write
    assert final["tasks"]["a"]["state"] == cl.INTEGRATED
    assert final["integration"]["tip"] == publish
    assert final["integration"]["history"] == [{"task": "a", "generation": 1, "publish_oid": publish,
                                                 "candidate_oid": cand, "tree": tree, "test_rc": 0, "test_log_sha": "ff"}]
    assert pending_ops(final) == []


def test_complete_integration_validates_before_mutating():
    doc = put(fresh_doc(), "a", cl.VERIFIED)
    seq = cl.begin_integration(doc, "a", generation=1, expected_tip=BASE, candidate_oid=oid(1), publish_oid=oid(2), tree=oid(3))
    ok = dict(generation=1, seq=seq, publish_oid=oid(2), candidate_oid=oid(1), tree=oid(3), test_rc=0, test_log_sha="x")
    before = copy.deepcopy(doc)
    for patch, code in (({"generation": 2}, cl.E_STALE), ({"seq": 99}, cl.E_JOURNAL), ({"publish_oid": "zz"}, cl.E_BAD_FIELD),
                        ({"test_rc": "0"}, cl.E_BAD_FIELD), ({"tip": "zz"}, cl.E_BAD_FIELD)):
        with pytest.raises(cl.LedgerError) as err:
            cl.complete_integration(doc, "a", **dict(ok, **patch))
        assert err.value.code == code and doc == before
    other = put(fresh_doc(), "a", cl.VERIFIED)
    with pytest.raises(cl.LedgerError):  # not integrating
        cl.complete_integration(other, "a", **dict(ok, seq=cl.journal_begin(other, cl.OP_RESERVE, "a", 1, reserve_data())))
    cl.complete_integration(doc, "a", tip=oid(20), **ok)
    assert doc["integration"]["tip"] == oid(20)
    with pytest.raises(cl.LedgerError):  # a second completion is illegal
        cl.complete_integration(doc, "a", **ok)


def test_begin_integration_requires_verified():
    doc = put(fresh_doc(), "a", cl.REVIEWED)
    before = copy.deepcopy(doc)
    with pytest.raises(cl.LedgerError):
        cl.begin_integration(doc, "a", generation=1, expected_tip=BASE, candidate_oid=oid(1), publish_oid=oid(2), tree=oid(3))
    assert doc == before  # no orphan journal entry


def test_integrating_replan_goes_back_to_verified():
    doc = put(fresh_doc(), "a", cl.VERIFIED)
    cl.begin_integration(doc, "a", generation=1, expected_tip=BASE, candidate_oid=oid(1), publish_oid=oid(2), tree=oid(3))
    cl.transition(doc, "a", cl.EV_REPLAN, generation=1)
    assert doc["tasks"]["a"]["state"] == cl.VERIFIED


# --------------------------------------------------------------------------
# graph queries, done, park/wake
# --------------------------------------------------------------------------


def test_graph_queries():
    doc = fresh_doc()
    assert cl.dependencies_integrated(doc, doc["tasks"]["a"]) and not cl.dependencies_integrated(doc, doc["tasks"]["b"])
    assert cl.next_admissible(doc) == ["a", "c"]
    put(doc, "a", cl.INTEGRATED)
    assert cl.dependencies_integrated(doc, doc["tasks"]["b"]) and cl.next_admissible(doc) == ["b", "c"]
    put(doc, "b", cl.ADMITTED)
    assert cl.next_admissible(doc) == ["c"]
    assert not cl.run_is_done(doc)
    for name in doc["tasks"]:
        put(doc, name, cl.INTEGRATED)
    assert cl.run_is_done(doc) and cl.finish_run(doc) and doc["state"] == cl.RUN_DONE
    with pytest.raises(cl.LedgerError):
        cl.resume_run(doc)
    assert not cl.run_is_done({"tasks": {}})
    parked = put(fresh_doc(), "a", cl.NEEDS_OPERATOR)
    parked["state"] = cl.RUN_PARKED
    assert cl.resume_run(parked)["state"] == cl.RUN_ACTIVE


def stuck_doc():
    doc = put(fresh_doc(), "a", cl.NEEDS_OPERATOR, blocker="poll-errors", resume_state="launched")
    put(doc, "b", cl.WAITING)
    put(doc, "c", cl.REJECTED)
    return doc


def test_park_condition():
    doc = stuck_doc()
    assert cl.park_condition(doc)
    assert not cl.park_condition(put(stuck_doc(), "c", cl.RUNNING))  # something can progress
    assert not cl.park_condition(put(stuck_doc(), "c", cl.ADMITTED))
    finished = stuck_doc()
    finished["state"] = cl.RUN_DONE
    assert not cl.park_condition(finished)
    all_done = fresh_doc()
    for name in all_done["tasks"]:
        put(all_done, name, cl.REJECTED)
    assert not cl.park_condition(all_done)  # nothing needs an operator
    pending = stuck_doc()
    cl.journal_begin(pending, cl.OP_RELEASE, "a", 1, {"key": "R1/a/g1"})
    assert not cl.park_condition(pending)
    # an uncertain launch is progressing until its ledger-stored recovery polls are spent (14.3)
    uncertain = put(stuck_doc(), "a", cl.UNCERTAIN, uncertain_ticks=0)
    assert not cl.park_condition(uncertain)
    assert not cl.park_condition(put(stuck_doc(), "a", cl.UNCERTAIN, uncertain_ticks=cl.UNCERTAIN_POLLS["claude"]))
    stale = put(stuck_doc(), "a", cl.UNCERTAIN, uncertain_ticks=cl.UNCERTAIN_POLLS["claude"] + 1)
    assert cl.park_condition(stale)
    dependent_failing = put(stuck_doc(), "b", cl.WAITING, depends_on=["c"])
    assert not cl.park_condition(dependent_failing)  # b will fail next tick, that is progress


def test_try_park_handshake():
    doc = stuck_doc()
    seen = doc["wake_seq"]
    cl.apply_abandon(doc, "a", retry=True)  # operator acts between tick start and park
    assert doc["wake_seq"] == seen + 1
    before = copy.deepcopy(doc)
    assert cl.try_park(doc, seen) is False and doc == before
    stuck = stuck_doc()
    snapshot = copy.deepcopy(stuck)
    assert cl.try_park(stuck, stuck["wake_seq"]) is True
    assert stuck["state"] == cl.RUN_PARKED and pending_ops(stuck) == [(cl.OP_PARK, None)]
    assert snapshot["state"] == cl.RUN_ACTIVE
    assert cl.try_park(stuck, stuck["wake_seq"]) is False  # already parked
    healthy = put(stuck_doc(), "c", cl.RUNNING)
    assert cl.try_park(healthy, healthy["wake_seq"]) is False and healthy["state"] == cl.RUN_ACTIVE


def test_park_wake_race_through_the_store(tmp_path):
    path = tmp_path / "R1.json"
    cl.create_run_ledger(path, stuck_doc())
    seen = cl.read(path)["wake_seq"]
    cl.locked_update(path, lambda d: cl.apply_resolve(put(d, "a", cl.NEEDS_OPERATOR, blocker="review-cap",
                                                          resume_state="collected"), "a", "retest"))
    assert cl.park_run(path, seen) is False
    assert cl.read(path)["state"] == cl.RUN_ACTIVE
    cl.locked_update(path, lambda d: put(d, "a", cl.NEEDS_OPERATOR, blocker="poll-errors", resume_state="launched"))
    revision = cl.read(path)["revision"]
    assert cl.park_run(path, cl.read(path)["wake_seq"]) is True
    assert cl.read(path)["state"] == cl.RUN_PARKED and cl.read(path)["revision"] == revision + 1
    assert cl.park_run(path, 0) is False and cl.read(path)["revision"] == revision + 1  # no write when refused


# --------------------------------------------------------------------------
# slots ledger
# --------------------------------------------------------------------------


def test_reserve_limits_and_idempotency(tmp_path):
    slots = tmp_path / "providers.json"
    assert cl.reserve_slot(slots, "R1/a/g1", "claude", LIMITS, FAR, now=10)
    assert cl.reserve_slot(slots, "R1/a/g1", "claude", LIMITS, FAR, now=10)  # idempotent
    revision = cl.read(slots)["revision"]
    assert cl.reserve_slot(slots, "R1/a/g1", "claude", LIMITS, FAR)
    assert cl.read(slots)["revision"] == revision  # an idempotent hit writes nothing
    assert cl.reserve_slot(slots, "R1/b/g1", "claude", LIMITS, FAR)
    assert not cl.reserve_slot(slots, "R1/c/g1", "claude", LIMITS, FAR)  # per-provider cap 2
    assert cl.reserve_slot(slots, "R1/c/g1", "codex", LIMITS, FAR)
    assert not cl.reserve_slot(slots, "R2/d/g1", "codex", LIMITS, FAR)  # total cap 3 (and codex cap 1)
    assert not cl.reserve_slot(slots, "R2/d/g1", "claude", {"claude": 9, "codex": 9, "total": 3}, FAR)
    assert cl.slot_keys(slots) == ["R1/a/g1", "R1/b/g1", "R1/c/g1"]
    first = cl.read(slots)["slots"][0]
    assert first == {"key": "R1/a/g1", "run": "R1", "provider": "claude", "reserved_at": 10, "deadline_at": FAR}
    assert stat.S_IMODE(slots.stat().st_mode) == 0o600


def test_reserve_validation(tmp_path):
    slots = tmp_path / "providers.json"
    for args in (("bad", "claude", LIMITS, FAR), ("R1/a/g1", "gemini", LIMITS, FAR), ("R1/a/g1", "claude", {"claude": 1}, FAR),
                 ("R1/a/g1", "claude", LIMITS, 0), ("R1/a/g0", "claude", LIMITS, FAR), ("R1/A/g1", "claude", LIMITS, FAR)):
        with pytest.raises(cl.LedgerError):
            cl.reserve_slot(slots, *args)
    assert not slots.exists()
    assert not cl.reserve_slot(slots, "R1/a/g1", "codex", {"claude": 1, "codex": 0, "total": 1}, FAR)


def test_release_is_idempotent(tmp_path):
    slots = tmp_path / "providers.json"
    assert cl.release_slot(slots, "R1/a/g1") is False  # no file yet
    cl.reserve_slot(slots, "R1/a/g1", "claude", LIMITS, FAR)
    assert cl.release_slot(slots, "R1/a/g1") is True
    revision = cl.read(slots)["revision"]
    assert cl.release_slot(slots, "R1/a/g1") is False and cl.read(slots)["revision"] == revision
    assert cl.slot_keys(slots) == []
    with pytest.raises(cl.LedgerError):
        cl.release_slot(slots, "nonsense")


def test_reap_by_deadline_and_by_run_state(tmp_path):
    slots = tmp_path / "providers.json"
    assert cl.reap_slots(slots, 0, lambda run: cl.RUN_ACTIVE) == []  # no file
    cl.reserve_slot(slots, "R1/a/g1", "claude", LIMITS, 1000)
    cl.reserve_slot(slots, "R2/b/g1", "claude", LIMITS, FAR)
    cl.reserve_slot(slots, "R3/c/g1", "codex", LIMITS, FAR)
    states = {"R1": cl.RUN_ACTIVE, "R2": None, "R3": cl.RUN_ACTIVE}  # R2 has no ledger
    assert cl.reap_slots(slots, 1000 + cl.SLOT_REAP_GRACE_S, states.get) == ["R2/b/g1"]  # R1 not past grace yet
    assert cl.slot_keys(slots) == ["R1/a/g1", "R3/c/g1"]
    assert cl.reap_slots(slots, 1000 + cl.SLOT_REAP_GRACE_S + 1, states.get) == ["R1/a/g1"]
    assert cl.slot_keys(slots) == ["R3/c/g1"]
    revision = cl.read(slots)["revision"]
    assert cl.reap_slots(slots, 1, states.get) == [] and cl.read(slots)["revision"] == revision


def test_a_parked_run_holds_no_slot_past_its_deadline_but_an_active_one_does(tmp_path):
    slots = tmp_path / "providers.json"
    cl.reserve_slot(slots, "R1/a/g1", "claude", LIMITS, 1000)
    cl.reserve_slot(slots, "R2/b/g1", "claude", LIMITS, 1000)
    states = {"R1": cl.RUN_PARKED, "R2": cl.RUN_ACTIVE}
    assert cl.reap_slots(slots, 999, states.get) == []
    assert cl.reap_slots(slots, 1001, states.get) == ["R1/a/g1"]  # parked and past its deadline
    assert cl.slot_keys(slots) == ["R2/b/g1"]
    assert cl.reap_slots(slots, 1001, {"R2": cl.RUN_DONE}.get) == ["R2/b/g1"]  # done runs hold nothing


def test_run_state_fn_reads_real_ledgers(tmp_path):
    state = cl.make_run_state_fn(tmp_path)
    assert state("R1") is None  # missing ledger
    path = cl.run_ledger_path(tmp_path, "R1")
    cl.create_run_ledger(path, fresh_doc())
    assert state("R1") == cl.RUN_ACTIVE
    cl.locked_update(path, lambda d: d.update({"state": cl.RUN_DONE}) or d)
    assert state("R1") == cl.RUN_DONE
    path.write_text("garbage")
    assert state("R1") == cl.RUN_ACTIVE  # unreadable ledgers are never reaped on a guess
    slots = cl.slots_path(tmp_path)
    cl.reserve_slot(slots, "R1/a/g1", "claude", LIMITS, FAR)
    cl.reserve_slot(slots, "ghost/a/g1", "claude", LIMITS, FAR)
    assert cl.reap_slots(slots, 1, state) == ["ghost/a/g1"]


# --------------------------------------------------------------------------
# crash injection across the multi-store operations
# --------------------------------------------------------------------------


class Crash(Exception):
    pass


class World:
    def __init__(self, tmp_path):
        self.run = tmp_path / "R1.json"
        self.slots = tmp_path / "providers.json"


def assert_recoverable(world):
    """The ledger reads, no journal entry is left, and every slot belongs to a live task generation."""
    doc = cl.read(world.run)
    cl.read(world.slots) if world.slots.exists() else None
    leftovers = [e for e in cl.pending_journal(doc) if e["op"] in (cl.OP_RESERVE, cl.OP_RELEASE)]
    assert leftovers == []
    for key in cl.slot_keys(world.slots):
        _run, name, generation = cl.validate_slot_key(key)
        entry = doc["tasks"][name]
        assert entry["generation"] == generation and entry["state"] in cl.SLOT_MAYBE_HELD, key
    return doc


def replay(world):
    first = cl.replay_slot_journal(world.run, world.slots, LIMITS)
    assert cl.replay_slot_journal(world.run, world.slots, LIMITS) == []  # idempotent
    return first


def run_with_crash(monkeypatch, steps, crash_at, when):
    """Run ``steps``; raise ``Crash`` before/after the ``crash_at``-th atomic write (0-based)."""
    real = cl._atomic_write
    counter = {"n": 0}

    def wrapper(path, payload):
        index = counter["n"]
        counter["n"] += 1
        if index == crash_at and when == "before":
            raise Crash()
        real(path, payload)
        if index == crash_at and when == "after":
            raise Crash()

    monkeypatch.setattr(cl, "_atomic_write", wrapper)
    try:
        for step in steps:
            step()
    except Crash:
        pass
    finally:
        monkeypatch.undo()
    return counter["n"]


def reserve_steps(world):
    box = {}

    def begin():
        def fn(d):
            box["seq"] = cl.journal_begin(d, cl.OP_RESERVE, "a", 1, {"key": "R1/a/g1", "provider": "claude", "deadline_at": FAR})
            return d
        cl.locked_update(world.run, fn)

    def add():
        assert cl.reserve_slot(world.slots, "R1/a/g1", "claude", LIMITS, FAR)

    def finish():
        cl.locked_update(world.run, lambda d: cl.transition(d, "a", cl.EV_CAPACITY, generation=1, journal_seq=box["seq"]))

    return [begin, add, finish]


MODES = [(n, w) for n in range(4) for w in ("before", "after")]


@pytest.mark.parametrize("crash_at,when", MODES)
def test_crash_during_reserve(tmp_path, monkeypatch, crash_at, when):
    world = World(tmp_path)
    cl.create_run_ledger(world.run, put(fresh_doc(), "a", cl.ADMITTED))
    run_with_crash(monkeypatch, reserve_steps(world), crash_at, when)
    replay(world)
    doc = assert_recoverable(world)
    state, slots = doc["tasks"]["a"]["state"], cl.slot_keys(world.slots)
    # either the reservation never started (no slot, task still admitted) or it completed (one slot)
    assert (state, slots) in ((cl.ADMITTED, []), (cl.RESERVED, ["R1/a/g1"]))
    if crash_at >= 1:
        assert state == cl.RESERVED


def bind_steps(world):
    box = {}

    def bind():
        def fn(d):
            cl.transition(d, "a", cl.EV_BIND, generation=1, bound_oid=oid(1))
            box["seq"] = cl.pending_journal(d)[0]["seq"]
            return d
        cl.locked_update(world.run, fn)

    def drop():
        cl.release_slot(world.slots, "R1/a/g1")

    def finish():
        cl.locked_update(world.run, lambda d: cl.journal_done(d, box["seq"]))

    return [bind, drop, finish]


@pytest.mark.parametrize("crash_at,when", MODES)
def test_crash_during_bind_and_release(tmp_path, monkeypatch, crash_at, when):
    world = World(tmp_path)
    cl.create_run_ledger(world.run, put(fresh_doc(), "a", cl.RUNNING))
    cl.reserve_slot(world.slots, "R1/a/g1", "claude", LIMITS, FAR)
    run_with_crash(monkeypatch, bind_steps(world), crash_at, when)
    doc = cl.read(world.run)
    if doc["tasks"]["a"]["state"] == cl.RUNNING:  # crashed before the bind write: the poller simply binds again
        assert cl.slot_keys(world.slots) == ["R1/a/g1"]
        return
    replay(world)
    doc = assert_recoverable(world)
    assert doc["tasks"]["a"]["state"] == cl.RESULT_READY and cl.slot_keys(world.slots) == []


def abandon_during_reserve_steps(world):
    steps = reserve_steps(world)
    steps.append(lambda: cl.locked_update(world.run, lambda d: cl.apply_abandon(d, "a", retry=True)))
    return steps


@pytest.mark.parametrize("crash_at,when", [(n, w) for n in range(5) for w in ("before", "after")])
def test_crash_when_operator_abandons_mid_reserve(tmp_path, monkeypatch, crash_at, when):
    world = World(tmp_path)
    cl.create_run_ledger(world.run, put(fresh_doc(), "a", cl.ADMITTED))
    steps = reserve_steps(world)[:2]  # journal + slot add, but never the final transition (orchestrator died)
    steps.append(lambda: cl.locked_update(world.run, lambda d: cl.apply_abandon(d, "a", retry=True)))
    run_with_crash(monkeypatch, steps, crash_at, when)
    replay(world)
    doc = assert_recoverable(world)
    entry = doc["tasks"]["a"]
    if entry["generation"] == 2:
        assert entry["state"] == cl.WAITING and cl.slot_keys(world.slots) == []
    else:
        expected = [] if entry["state"] == cl.ADMITTED else ["R1/a/g1"]
        assert entry["state"] in (cl.ADMITTED, cl.RESERVED) and cl.slot_keys(world.slots) == expected


@pytest.mark.parametrize("crash_at,when", [(n, w) for n in range(3) for w in ("before", "after")])
def test_crash_when_operator_adopts_a_task_holding_a_slot(tmp_path, monkeypatch, crash_at, when):
    world = World(tmp_path)
    cl.create_run_ledger(world.run, put(fresh_doc(), "a", cl.NEEDS_OPERATOR, blocker="poll-errors", resume_state="launched"))
    cl.reserve_slot(world.slots, "R1/a/g1", "claude", LIMITS, FAR)

    def adopt():
        cl.locked_update(world.run, lambda d: cl.apply_adopt(d, "a", oid(5)))

    run_with_crash(monkeypatch, [adopt, lambda: cl.release_slot(world.slots, "R1/a/g1")], crash_at, when)
    replay(world)
    doc = cl.read(world.run)
    if doc["tasks"]["a"]["state"] == cl.NEEDS_OPERATOR:  # adopt never landed: the slot is still legitimately held
        assert cl.slot_keys(world.slots) == ["R1/a/g1"]
    else:
        assert_recoverable(world) and cl.slot_keys(world.slots) == []


@pytest.mark.parametrize("crash_at,when", [(n, w) for n in range(2) for w in ("before", "after")])
def test_crash_between_begin_and_complete_integration(tmp_path, monkeypatch, crash_at, when):
    world = World(tmp_path)
    cl.create_run_ledger(world.run, put(fresh_doc(), "a", cl.VERIFIED))
    box = {}

    def begin():
        def fn(d):
            box["seq"] = cl.begin_integration(d, "a", generation=1, expected_tip=BASE, candidate_oid=oid(1),
                                              publish_oid=oid(2), tree=oid(3))
            return d
        cl.locked_update(world.run, fn)

    def complete():
        cl.locked_update(world.run, lambda d: cl.complete_integration(
            d, "a", generation=1, seq=box["seq"], publish_oid=oid(2), candidate_oid=oid(1), tree=oid(3), test_rc=0,
            test_log_sha="x"))

    run_with_crash(monkeypatch, [begin, complete], crash_at, when)
    doc = cl.read(world.run)
    state = doc["tasks"]["a"]["state"]
    if state == cl.VERIFIED:
        assert pending_ops(doc) == [] and doc["integration"]["tip"] == BASE
    elif state == cl.INTEGRATING:  # recoverable: the replay decision picks the action from the observed ref
        entry = cl.pending_journal(doc)[0]
        assert cl.replay_integrate(entry, BASE) == "update-ref"
        assert cl.replay_integrate(entry, oid(2)) == "complete"
        cl.locked_update(world.run, lambda d: cl.complete_integration(
            d, "a", generation=1, seq=entry["seq"], publish_oid=oid(2), candidate_oid=oid(1), tree=oid(3),
            test_rc=0, test_log_sha="x"))
        state = cl.read(world.run)["tasks"]["a"]["state"]
    final = cl.read(world.run)
    assert final["tasks"]["a"]["state"] in (cl.VERIFIED, cl.INTEGRATED)
    if final["tasks"]["a"]["state"] == cl.INTEGRATED:  # the four effects are all-or-nothing
        assert final["integration"]["tip"] == oid(2) and len(final["integration"]["history"]) == 1
        assert pending_ops(final) == []
    else:
        assert final["integration"]["tip"] == BASE and final["integration"]["history"] == []


def test_replay_reserve_without_capacity_keeps_task_admitted(tmp_path):
    world = World(tmp_path)
    cl.create_run_ledger(world.run, put(fresh_doc(), "a", cl.ADMITTED))
    cl.locked_update(world.run, lambda d: cl.journal_begin(d, cl.OP_RESERVE, "a", 1,
                                                           {"key": "R1/a/g1", "provider": "claude", "deadline_at": FAR}) and d)
    tight = {"claude": 0, "codex": 0, "total": 0}
    assert cl.replay_slot_journal(world.run, world.slots, tight) == [(1, "reserve", "no-capacity")]
    doc = cl.read(world.run)
    assert doc["tasks"]["a"]["state"] == cl.ADMITTED and pending_ops(doc) == [] and cl.slot_keys(world.slots) == []


@pytest.mark.parametrize("state", [cl.LAUNCHED, cl.RUNNING])
def test_replay_reserve_for_a_live_recovered_task_is_accounted_even_without_capacity(tmp_path, state):
    world = World(tmp_path)
    cl.create_run_ledger(world.run, put(fresh_doc(), "a", state))
    cl.locked_update(world.run, lambda d: cl.journal_begin(d, cl.OP_RESERVE, "a", 1,
                                                           {"key": "R1/a/g1", "provider": "claude", "deadline_at": FAR}) and d)
    tight = {"claude": 0, "codex": 0, "total": 0}
    assert cl.replay_slot_journal(world.run, world.slots, tight) == [(1, "reserve", "reserved")]
    doc = cl.read(world.run)
    assert doc["tasks"]["a"]["state"] == state and pending_ops(doc) == [] and cl.slot_keys(world.slots) == ["R1/a/g1"]


def test_leaving_needs_operator_releases_only_if_a_slot_was_held():
    # waiting -> needs-operator (admit blocker) -> retest -> waiting: nothing was ever reserved
    doc = put(fresh_doc(), "a", cl.WAITING)
    cl.transition(doc, "a", cl.EV_NEEDS_OPERATOR, generation=1, blocker="grant-missing")
    cl.apply_resolve(doc, "a", "retest")
    assert pending_ops(doc) == []
    # the same generation is admitted and reserved under the same key; a replay must keep that live slot
    put(doc, "a", cl.ADMITTED)
    cl.transition(doc, "a", cl.EV_CAPACITY, generation=1)
    # admitted -> needs-operator -> abandon: a reservation may be half done, so the release is journaled
    admitted = put(fresh_doc(), "a", cl.ADMITTED)
    cl.transition(admitted, "a", cl.EV_NEEDS_OPERATOR, generation=1, blocker="branch-conflict")
    assert admitted["tasks"]["a"]["resume_state"] == cl.ADMITTED and pending_ops(admitted) == []
    cl.apply_abandon(admitted, "a")
    assert [e["data"]["key"] for e in cl.pending_journal(admitted)] == ["R1/a/g1"]
    # needs-operator entered from a slot-holding state keeps its slot until the operator acts
    held = put(fresh_doc(), "a", cl.RUNNING)
    cl.transition(held, "a", cl.EV_POLL_ERRORS, generation=1)
    cl.apply_adopt(held, "a", oid(1))
    assert pending_ops(held) == [(cl.OP_RELEASE, "a")]


def test_live_slot_survives_replay_after_a_waiting_blocker_roundtrip(tmp_path):
    world = World(tmp_path)
    doc = put(fresh_doc(), "a", cl.WAITING)
    cl.transition(doc, "a", cl.EV_NEEDS_OPERATOR, generation=1, blocker="grant-missing")
    cl.apply_resolve(doc, "a", "retest")
    put(doc, "a", cl.RESERVED)
    cl.create_run_ledger(world.run, doc)
    assert cl.reserve_slot(world.slots, "R1/a/g1", "claude", LIMITS, FAR)
    assert cl.replay_slot_journal(world.run, world.slots, LIMITS) == []
    assert cl.slot_keys(world.slots) == ["R1/a/g1"]


def test_journal_data_is_validated_per_op():
    doc = put(fresh_doc(), "a", cl.ADMITTED)
    before = copy.deepcopy(doc)
    bad_data = [
        (cl.OP_RESERVE, {}), (cl.OP_RESERVE, {"key": "R1/a/g1"}), (cl.OP_RESERVE, reserve_data(provider="gemini")),
        (cl.OP_RESERVE, reserve_data("c")), (cl.OP_RESERVE, reserve_data(deadline_at=0)),
        (cl.OP_RELEASE, {"key": "R1/a/g2"}), (cl.OP_ADMIT, {}), (cl.OP_ADMIT, {"base_oid": "zz"}),
        ("bind", {"oid": "zz"}), (cl.OP_INTEGRATE, {"expected_tip": BASE}),
    ]
    for op, data in bad_data:
        with pytest.raises(cl.LedgerError) as err:
            cl.journal_begin(doc, op, "a", 1, data)
        assert err.value.code == cl.E_JOURNAL, (op, data)
        assert doc == before
    cl.journal_begin(doc, cl.OP_RESERVE, "a", 1, reserve_data(deadline_at=FAR))  # deadline_at is optional and allowed
    cl.journal_begin(doc, cl.OP_ADMIT, "a", 1, {"base_oid": BASE, "task_branch": "refs/heads/x"})


def test_replay_reserve_with_contract_shaped_data_uses_the_default_deadline(tmp_path):
    world = World(tmp_path)
    cl.create_run_ledger(world.run, put(fresh_doc(), "a", cl.ADMITTED))
    cl.locked_update(world.run, lambda d: cl.journal_begin(d, cl.OP_RESERVE, "a", 1, reserve_data()) and d)
    assert cl.replay_slot_journal(world.run, world.slots, LIMITS, now=1000) == [(1, "reserve", "reserved")]
    assert cl.read(world.slots)["slots"][0]["deadline_at"] == 1000 + cl.DEFAULT_SLOT_DEADLINE_S
    assert cl.read(world.run)["tasks"]["a"]["state"] == cl.RESERVED


def test_replay_raises_ledger_error_on_malformed_entries(tmp_path):
    world = World(tmp_path)
    doc = put(fresh_doc(), "a", cl.ADMITTED)
    doc["journal"] = [{"seq": 1, "op": "reserve", "task": "a", "generation": 1, "data": {}, "done": False}]
    cl.create_run_ledger(world.run, doc)
    with pytest.raises(cl.LedgerError):
        cl.replay_slot_journal(world.run, world.slots, LIMITS)


def test_replay_ignores_other_ops(tmp_path):
    world = World(tmp_path)
    cl.create_run_ledger(world.run, fresh_doc())
    cl.locked_update(world.run, lambda d: cl.journal_begin(d, cl.OP_ADMIT, "a", 1, {"base_oid": BASE}) and d)
    assert cl.replay_slot_journal(world.run, world.slots, LIMITS) == []
    assert pending_ops(cl.read(world.run)) == [(cl.OP_ADMIT, "a")]


# --------------------------------------------------------------------------
# manifest v1
# --------------------------------------------------------------------------

NONCE = "0123456789abcdef" * 2
MBASE = "c" * 40
EXPECT = dict(run="R1", task="t1", generation=1, nonce=NONCE, base_sha=MBASE)


def manifest_dict(**over):
    doc = {"schema": 1, "run": "R1", "task": "t1", "generation": 1, "nonce": NONCE, "base_sha": MBASE,
           "status": "complete", "summary": "done", "decisions_needed": [], "blockers": [],
           "env_report": {"tools": {"node": "v22", "python": "3.12.1"}, "present": ["API_KEY"], "missing": ["DB_URL"]},
           "tests_run": [{"cmd": "npm test", "rc": 0}]}
    doc.update(over)
    return doc


def manifest_bytes(**over):
    return json.dumps(manifest_dict(**over)).encode()


def check(raw, **over):
    return cl.validate_manifest(raw, **dict(EXPECT, **over))


def test_manifest_positive():
    parsed = check(manifest_bytes())
    assert parsed["status"] == "complete" and parsed["tests_run"] == [{"cmd": "npm test", "rc": 0}]
    blocked = check(manifest_bytes(status="blocked", blockers=["need db"], decisions_needed=["which?"], summary="x" * 2000))
    assert blocked["status"] == "blocked"
    assert check(manifest_bytes(env_report={"tools": {}, "present": [], "missing": []}, tests_run=[]))["tests_run"] == []
    assert check(manifest_bytes(), base_sha=MBASE)["base_sha"] == MBASE
    sha256_base = "d" * 64
    assert check(manifest_bytes(base_sha=sha256_base), base_sha=sha256_base)


def bad(raw, code, **over):
    with pytest.raises(cl.ManifestError) as err:
        check(raw, **over)
    assert err.value.code == code, err.value


def test_manifest_transport_negatives():
    bad(b"x" * (cl.MANIFEST_MAX_BYTES + 1), "MANIFEST_TOO_LARGE")
    bad(b"\xff\xfe{}", "MANIFEST_NOT_UTF8")
    bad(b"{nope", "MANIFEST_BAD_JSON")
    bad(b'{"schema": NaN}', "MANIFEST_BAD_JSON")
    bad(b"[" * 60000, "MANIFEST_BAD_JSON")
    bad(b"[]", "MANIFEST_NOT_OBJECT")
    bad(b'{"schema":1,"schema":1}', "MANIFEST_DUPLICATE_KEY")
    bad(b"\xef\xbb\xbf" + manifest_bytes(), "MANIFEST_BAD_JSON")
    bad("text".encode(), "MANIFEST_BAD_JSON")
    with pytest.raises(cl.ManifestError) as err:
        cl.validate_manifest("not bytes", **EXPECT)  # type: ignore[arg-type]
    assert err.value.code == "MANIFEST_TYPE"


def test_manifest_key_set_negatives():
    bad(json.dumps(dict(manifest_dict(), extra=1)).encode(), "MANIFEST_KEYS")
    missing = manifest_dict()
    del missing["summary"]
    bad(json.dumps(missing).encode(), "MANIFEST_KEYS")
    bad(manifest_bytes(schema=2), "MANIFEST_SCHEMA")
    bad(manifest_bytes(schema=True), "MANIFEST_SCHEMA")


@pytest.mark.parametrize("field,value,code", [
    ("run", "R2", "MANIFEST_MISMATCH"), ("task", "t2", "MANIFEST_MISMATCH"), ("generation", 2, "MANIFEST_MISMATCH"),
    ("base_sha", "e" * 40, "MANIFEST_MISMATCH"), ("nonce", "f" * 32, "MANIFEST_NONCE"),
    ("nonce", "short", "MANIFEST_NONCE"), ("nonce", NONCE.upper(), "MANIFEST_NONCE"),
    ("generation", True, "MANIFEST_TYPE"), ("generation", "1", "MANIFEST_TYPE"), ("generation", 0, "MANIFEST_TYPE"),
    ("run", 5, "MANIFEST_TYPE"), ("base_sha", "xyz", "MANIFEST_TYPE"),
    ("status", "done", "MANIFEST_STATUS"), ("status", ["complete"], "MANIFEST_TYPE"),
    ("summary", "x" * 2001, "MANIFEST_TOO_LONG"), ("summary", 3, "MANIFEST_TYPE"), ("summary", "a\x00b", "MANIFEST_TYPE"),
    ("summary", "\ud800", "MANIFEST_TYPE"),
    ("blockers", ["x" * 4001], "MANIFEST_TOO_LONG"), ("blockers", ["x"] * 51, "MANIFEST_TOO_MANY"),
    ("blockers", "none", "MANIFEST_TYPE"), ("blockers", [1], "MANIFEST_TYPE"),
    ("decisions_needed", ["x"] * 51, "MANIFEST_TOO_MANY"), ("decisions_needed", None, "MANIFEST_TYPE"),
    ("env_report", None, "MANIFEST_ENV_REPORT"),
    ("env_report", {"tools": {}, "present": [], "missing": [], "extra": 1}, "MANIFEST_ENV_REPORT"),
    ("env_report", {"tools": {}, "present": []}, "MANIFEST_ENV_REPORT"),
    ("env_report", {"tools": [], "present": [], "missing": []}, "MANIFEST_ENV_REPORT"),
    ("env_report", {"tools": {"node": "v 22; rm -rf"}, "present": [], "missing": []}, "MANIFEST_ENV_VERSION"),
    ("env_report", {"tools": {"node": 22}, "present": [], "missing": []}, "MANIFEST_ENV_VERSION"),
    ("env_report", {"tools": {"bad name": "1"}, "present": [], "missing": []}, "MANIFEST_ENV_NAME"),
    ("env_report", {"tools": {}, "present": ["lower"], "missing": []}, "MANIFEST_ENV_NAME"),
    ("env_report", {"tools": {}, "present": [], "missing": ["sk-" + "ant-abcdefghijklmnop"]}, "MANIFEST_ENV_NAME"),
    ("env_report", {"tools": {}, "present": ["A" * 65], "missing": []}, "MANIFEST_ENV_NAME"),
    ("env_report", {"tools": {}, "present": ["X"] * 51, "missing": []}, "MANIFEST_TOO_MANY"),
    ("env_report", {"tools": {"t%d" % i: "1" for i in range(51)}, "present": [], "missing": []}, "MANIFEST_TOO_MANY"),
    ("tests_run", "npm test", "MANIFEST_TYPE"), ("tests_run", [{"cmd": "x"}], "MANIFEST_TESTS_RUN"),
    ("tests_run", [{"cmd": "x", "rc": 0, "extra": 1}], "MANIFEST_TESTS_RUN"),
    ("tests_run", [{"cmd": "x", "rc": True}], "MANIFEST_TESTS_RUN"), ("tests_run", [{"cmd": "x", "rc": "0"}], "MANIFEST_TESTS_RUN"),
    ("tests_run", [{"cmd": "x", "rc": 10 ** 9}], "MANIFEST_TESTS_RUN"), ("tests_run", [{"cmd": 1, "rc": 0}], "MANIFEST_TYPE"),
    ("tests_run", [{"cmd": "x" * 4001, "rc": 0}], "MANIFEST_TOO_LONG"), ("tests_run", [{"cmd": "x", "rc": 0}] * 51, "MANIFEST_TOO_MANY"),
])
def test_manifest_field_negatives(field, value, code):
    bad(json.dumps({**manifest_dict(), field: value}).encode(), code)


def test_manifest_require_nonce_flag():
    other = manifest_bytes(nonce="f" * 32)
    bad(other, "MANIFEST_NONCE")
    assert cl.validate_manifest(other, **dict(EXPECT, require_nonce=False))["nonce"] == "f" * 32
    with pytest.raises(cl.ManifestError):
        cl.validate_manifest(manifest_bytes(nonce="zz"), **dict(EXPECT, require_nonce=False))


def test_manifest_does_not_scan_for_secrets():
    # scanning is B4's job; a secret-shaped summary is merely text here
    assert check(manifest_bytes(summary="sk-ant-" + "a" * 30))["summary"].startswith("sk-ant-")


# --------------------------------------------------------------------------
# review fixes: abandon vs integration, unbind, slot deadlines, uncertain, adopt --remote-id, blockers
# --------------------------------------------------------------------------


def test_abandon_is_refused_while_a_task_is_integrating():
    doc = put(fresh_doc(), "a", cl.VERIFIED)
    cl.begin_integration(doc, "a", generation=1, expected_tip=BASE, candidate_oid=oid(1), publish_oid=oid(2), tree=oid(3))
    before = copy.deepcopy(doc)
    for retry in (False, True):
        with pytest.raises(cl.LedgerError) as err:
            cl.apply_abandon(doc, "a", retry=retry)
        assert err.value.code == cl.E_INTEGRATING == "INTEGRATION_IN_PROGRESS"
        assert doc == before  # generation, journal and wake_seq untouched
    seq = cl.pending_journal(doc)[0]["seq"]
    cl.complete_integration(doc, "a", generation=1, seq=seq, publish_oid=oid(2), candidate_oid=oid(1), tree=oid(3),
                            test_rc=0, test_log_sha="x")
    assert doc["integration"]["tip"] == oid(2)
    with pytest.raises(cl.LedgerError) as err:  # integrated is terminal: still no plain abandon
        cl.apply_abandon(doc, "a")
    assert err.value.code != cl.E_INTEGRATING


def test_a_pending_integrate_entry_alone_also_blocks_abandon():
    doc = put(fresh_doc(), "a", cl.NEEDS_OPERATOR, blocker="poll-errors", resume_state="launched")
    doc["journal"].append({"seq": 1, "op": cl.OP_INTEGRATE, "task": "a", "generation": 1, "done": False, "data": {}})
    with pytest.raises(cl.LedgerError) as err:
        cl.apply_abandon(doc, "a", retry=True)
    assert err.value.code == cl.E_INTEGRATING


def test_abandon_retry_reopens_a_done_run_for_a_rejected_or_failed_task_only():
    for state in (cl.REJECTED, cl.FAILED):
        doc = fresh_doc([{"id": "a", "provider": "claude"}])
        put(doc, "a", state, blocker="tests-failed" if state == cl.REJECTED else "deadline")
        assert cl.finish_run(doc) and doc["state"] == cl.RUN_DONE
        cl.apply_abandon(doc, "a", retry=True)
        assert doc["state"] == cl.RUN_ACTIVE and doc["tasks"]["a"]["state"] == cl.WAITING
        assert doc["tasks"]["a"]["generation"] == 2 and doc["wake_seq"] == 1
    done = fresh_doc([{"id": "a", "provider": "claude"}])
    put(done, "a", cl.REJECTED, blocker="tests-failed")
    cl.finish_run(done)
    with pytest.raises(cl.LedgerError) as err:  # a plain abandon of a finished run stays refused
        cl.apply_abandon(done, "a")
    assert err.value.code == cl.E_RUN_DONE
    integrated = fresh_doc([{"id": "a", "provider": "claude"}])
    put(integrated, "a", cl.INTEGRATED)
    cl.finish_run(integrated)
    with pytest.raises(cl.LedgerError):
        cl.apply_abandon(integrated, "a", retry=True)


def test_unbind_records_the_pair_clears_the_bind_and_journals_the_slot():
    doc = put(fresh_doc(), "a", cl.RESULT_READY, bound_oid=oid(4), bound_ref="refs/heads/claude/other", seen_oid=oid(4),
              seen_count=2, deadline_at=FAR)
    cl.unbind_candidate(doc, "a", generation=1, ref="refs/heads/claude/other", oid=oid(4), deadline_at=FAR)
    entry = doc["tasks"]["a"]
    assert entry["state"] == cl.RUNNING and entry["bound_oid"] is None and entry["bound_ref"] is None
    assert entry["seen_oid"] is None and entry["seen_count"] == 0 and entry["blocker"] is None
    assert entry["rejected_candidates"] == [["refs/heads/claude/other", oid(4)]]
    pending = cl.pending_journal(doc)
    assert [(e["op"], e["data"]["key"]) for e in pending] == [(cl.OP_RESERVE, "R1/a/g1")]
    with pytest.raises(cl.LedgerError):  # not bound to that oid any more
        cl.unbind_candidate(doc, "a", generation=1, ref="refs/heads/claude/other", oid=oid(4), deadline_at=FAR)
    with pytest.raises(cl.LedgerError) as err:
        cl.unbind_candidate(put(fresh_doc(), "a", cl.RESULT_READY, bound_oid=oid(4)), "a", generation=2, ref="r", oid=oid(4),
                            deadline_at=FAR)
    assert err.value.code == cl.E_STALE


def test_bind_stores_the_poll_ref_and_adopt_clears_it():
    doc = put(fresh_doc(), "a", cl.RUNNING, launched_at=1, deadline_at=9)
    cl.transition(doc, "a", cl.EV_BIND, generation=1, bound_oid=oid(1), bound_ref="refs/heads/claude/ffs-R1-a-abcd1234")
    assert doc["tasks"]["a"]["bound_ref"] == "refs/heads/claude/ffs-R1-a-abcd1234"
    cl.transition(doc, "a", cl.EV_NEEDS_OPERATOR, generation=1, blocker="manifest-blocked")
    doc["tasks"]["a"]["blocker"] = "candidate-moved"
    cl.apply_adopt(doc, "a", oid(2))
    assert doc["tasks"]["a"]["bound_oid"] == oid(2) and doc["tasks"]["a"]["bound_ref"] is None


def test_release_expired_slots_journals_once_per_generation():
    doc = put(fresh_doc(), "a", cl.NEEDS_OPERATOR, blocker="poll-errors", resume_state="launched", deadline_at=1000)
    put(doc, "c", cl.UNCERTAIN)
    deadlines = {"a": 1000, "c": 2000}
    assert cl.release_expired_slots(doc, 999, lambda n, e: deadlines[n]) == []
    assert cl.release_expired_slots(doc, 1001, lambda n, e: deadlines[n]) == ["a"]  # c's deadline has not passed
    assert cl.release_expired_slots(doc, 1001, lambda n, e: deadlines[n]) == []  # exactly once
    assert cl.release_expired_slots(doc, 2001, lambda n, e: deadlines[n]) == ["c"]
    assert sorted(e["data"]["key"] for e in cl.pending_journal(doc)) == ["R1/a/g1", "R1/c/g1"]
    assert doc["tasks"]["a"]["slot_released"] is True
    cl.apply_abandon(doc, "a")  # leaving the operator state must not journal a second release
    assert sorted(e["data"]["key"] for e in cl.pending_journal(doc)) == ["R1/a/g1", "R1/c/g1"]
    working = put(fresh_doc(), "a", cl.RUNNING)
    assert cl.release_expired_slots(working, 10 ** 9, lambda n, e: 0) == []  # only operator states release at the deadline


def test_leaving_an_operator_state_after_a_deadline_release_forgets_the_release():
    doc = put(fresh_doc(), "a", cl.NEEDS_OPERATOR, blocker="grant-missing", resume_state="reserved")
    cl.release_expired_slots(doc, 10, lambda n, e: 1)
    assert doc["tasks"]["a"]["slot_released"] is True
    cl.apply_resolve(doc, "a", "retest")
    assert doc["tasks"]["a"]["state"] == cl.RESERVED and doc["tasks"]["a"]["slot_released"] is False
    cl.transition(doc, "a", cl.EV_SLOT_LOST, generation=1)  # the slot is gone: back to admitted, re-reserve first
    assert doc["tasks"]["a"]["state"] == cl.ADMITTED
    assert [e["op"] for e in cl.pending_journal(doc)].count(cl.OP_RELEASE) == 1  # the deadline release, nothing more


def test_uncertain_counts_its_recovery_polls_in_the_ledger():
    doc = put(fresh_doc(), "c", cl.UNCERTAIN)  # c is codex: a single re-parse
    assert not cl.uncertain_poll_budget_spent(doc["tasks"]["c"])
    assert cl.note_uncertain_tick(doc, "c", generation=1) == 1
    assert cl.uncertain_poll_budget_spent(doc["tasks"]["c"])
    claude = put(fresh_doc(), "a", cl.UNCERTAIN)
    for expected in range(1, cl.UNCERTAIN_POLLS["claude"] + 1):
        assert not cl.uncertain_poll_budget_spent(claude["tasks"]["a"])
        assert cl.note_uncertain_tick(claude, "a", generation=1) == expected
    assert cl.uncertain_poll_budget_spent(claude["tasks"]["a"])
    with pytest.raises(cl.LedgerError):
        cl.note_uncertain_tick(put(fresh_doc(), "a", cl.RUNNING), "a", generation=1)
    # the run does not park in the tick the launch became uncertain, nor while its polls are still due
    for ticks in range(cl.UNCERTAIN_POLLS["codex"] + 1):
        fresh = put(fresh_doc(), "c", cl.UNCERTAIN, uncertain_ticks=ticks)
        for name in ("a", "b"):
            put(fresh, name, cl.REJECTED)
        assert not cl.park_condition(fresh)


def test_adopt_remote_binds_a_codex_session_and_relaunches_polling():
    doc = put(fresh_doc(), "c", cl.UNCERTAIN, uncertain_ticks=1)
    cl.apply_adopt_remote(doc, "c", "task_e_123", launched_at=50, deadline_at=500)
    entry = doc["tasks"]["c"]
    assert entry["state"] == cl.LAUNCHED and entry["remote_id"] == "task_e_123"
    assert entry["launched_at"] == 50 and entry["deadline_at"] == 500 and entry["uncertain_ticks"] == 0
    assert doc["wake_seq"] == 1
    assert [(e["op"], e["data"]["key"]) for e in cl.pending_journal(doc)] == [(cl.OP_RESERVE, "R1/c/g1")]
    parked = put(fresh_doc(), "c", cl.NEEDS_OPERATOR, blocker="uncertain-launch", resume_state="uncertain")
    cl.apply_adopt_remote(parked, "c", "task_e_9", launched_at=1, deadline_at=9)
    assert parked["tasks"]["c"]["state"] == cl.LAUNCHED
    wrong = put(fresh_doc(), "c", cl.NEEDS_OPERATOR, blocker="review-cap", resume_state="collected")
    with pytest.raises(cl.LedgerError) as err:
        cl.apply_adopt_remote(wrong, "c", "task_e_9", launched_at=1, deadline_at=9)
    assert err.value.code == cl.E_BLOCKER_MISMATCH
    with pytest.raises(cl.LedgerError) as err:  # Claude binds by nonce
        cl.apply_adopt_remote(put(fresh_doc(), "a", cl.UNCERTAIN), "a", "sess", launched_at=1, deadline_at=9)
    assert err.value.code == cl.E_BAD_ARGUMENT
    with pytest.raises(cl.LedgerError):
        cl.apply_adopt_remote(put(fresh_doc(), "c", cl.UNCERTAIN), "c", "has space", launched_at=1, deadline_at=9)


def test_push_failures_are_counted_and_reset_by_a_retest():
    doc = put(fresh_doc(), "a", cl.WAITING)
    for expected in range(1, cl.PUSH_FAILURE_LIMIT + 1):
        assert cl.note_push_failure(doc, "a", generation=1) == expected
    cl.transition(doc, "a", cl.EV_NEEDS_OPERATOR, generation=1, blocker="push-failed")
    cl.apply_resolve(doc, "a", "retest")
    assert doc["tasks"]["a"]["state"] == cl.WAITING and doc["tasks"]["a"]["push_failures"] == 0


def test_review_retests_are_counted_and_capped_at_two():
    doc = put(fresh_doc(), "a", cl.NEEDS_OPERATOR, blocker="review-cap", resume_state="collected")
    for attempt in range(1, cl.REVIEW_RETEST_LIMIT + 1):
        cl.apply_resolve(doc, "a", "retest")
        assert doc["tasks"]["a"]["review_attempts"] == attempt
        cl.transition(doc, "a", cl.EV_NEEDS_OPERATOR, generation=1, blocker="review-cap")
    with pytest.raises(cl.LedgerError) as err:
        cl.apply_resolve(doc, "a", "retest")
    assert err.value.code == cl.E_BLOCKER_MISMATCH and "limit" in err.value.detail
    cl.apply_resolve(doc, "a", "reject")  # reject is still open
    assert doc["tasks"]["a"]["state"] == cl.REJECTED
    other = put(fresh_doc(), "a", cl.NEEDS_OPERATOR, blocker="sandbox-unavailable", resume_state="reviewed")
    cl.apply_resolve(other, "a", "retest")
    assert other["tasks"]["a"]["review_attempts"] == 0  # only review blockers count


def test_new_blockers_are_in_the_tables_with_their_clearing_actions():
    for blocker in ("internal-error", "push-failed", "scan-refused"):
        assert blocker in cl.ALL_BLOCKERS and blocker in cl.RESOLVE_ACTIONS["retest"]
    assert "integration-ref-foreign" in cl.RESOLVE_ACTIONS["reject"]
    assert "integration-ref-foreign" not in cl.RESOLVE_ACTIONS["retest"]
    assert "binary-change" in cl.RESOLVE_ACTIONS["reject"] and "binary-change" not in cl.RESOLVE_ACTIONS["retest"]
    for source in (cl.RESULT_READY, cl.COLLECTED, cl.REVIEWED, cl.VERIFIED):
        assert "binary-change" in cl.TRANSITIONS[(source, cl.EV_NEEDS_OPERATOR)][1]
        assert cl.TRANSITIONS[(source, cl.EV_REJECT)][0] == cl.REJECTED
    for source in (cl.WAITING, cl.ADMITTED, cl.RESERVED, cl.LAUNCHED, cl.RUNNING, cl.UNCERTAIN, cl.RESULT_READY,
                   cl.COLLECTED, cl.REVIEWED, cl.VERIFIED, cl.INTEGRATING):
        assert "internal-error" in cl.TRANSITIONS[(source, cl.EV_NEEDS_OPERATOR)][1], source
    assert "integration-ref-foreign" in cl.TRANSITIONS[(cl.INTEGRATING, cl.EV_NEEDS_OPERATOR)][1]
    assert {"scan-refused", "push-failed"} <= cl.TRANSITIONS[(cl.WAITING, cl.EV_NEEDS_OPERATOR)][1]
    assert {"scan-refused", "branch-conflict"} <= cl.TRANSITIONS[(cl.RESERVED, cl.EV_NEEDS_OPERATOR)][1]


def test_internal_error_parks_a_running_task_and_retest_resumes_it():
    doc = put(fresh_doc(), "a", cl.LAUNCHED, launched_at=1, deadline_at=9)
    cl.transition(doc, "a", cl.EV_NEEDS_OPERATOR, generation=1, blocker="internal-error", decisions_needed=["boom"])
    assert doc["tasks"]["a"]["state"] == cl.NEEDS_OPERATOR and doc["tasks"]["a"]["resume_state"] == cl.LAUNCHED
    cl.apply_resolve(doc, "a", "retest")
    assert doc["tasks"]["a"]["state"] == cl.LAUNCHED


def test_deps_digest_is_stored_with_the_dependency_blocker_and_reset_per_generation():
    doc = put(fresh_doc(), "a", cl.RESULT_READY)
    digest = "ab" * 32
    cl.transition(doc, "a", cl.EV_NEEDS_OPERATOR, generation=1, blocker="dependency-change", deps_digest=digest)
    assert doc["tasks"]["a"]["deps_digest"] == digest
    with pytest.raises(cl.LedgerError):
        cl.transition(put(fresh_doc(), "a", cl.RESULT_READY), "a", cl.EV_NEEDS_OPERATOR, generation=1,
                      blocker="dependency-change", deps_digest="NOT-HEX")
    cl.apply_abandon(doc, "a", retry=True)
    assert doc["tasks"]["a"]["deps_digest"] is None and doc["tasks"]["a"]["bound_ref"] is None


def test_tasks_sha256_matches_the_file_written_later(tmp_path):
    tasks = [{"id": "a", "prompt": "p"}]
    path = tmp_path / "runs" / "R1" / "tasks.json"
    assert cl.tasks_sha256(tasks) == cl.write_tasks_file(path, tasks)
    assert cl.load_tasks_file(path, cl.tasks_sha256(tasks)) == tasks


def test_live_worker_manifest_with_string_tests_run_is_rejected():
    """Manifest a real cloud worker produced before the packet stated the `tests_run` item shape."""
    import cloud_ledger as _ledger
    from pathlib import Path as _Path
    nonce, base = "0a" * 16, "1b" * 20  # the fixture holds placeholders, not the real values
    text = (_Path(__file__).parent / "fixtures" / "cloud" / "live" / "claude-manifest-strings-tests-run.json").read_text()
    raw = text.replace("@NONCE@", nonce).replace("@BASE_SHA@", base).encode("utf-8")
    with pytest.raises(_ledger.ManifestError) as err:
        _ledger.validate_manifest(raw, run="smoke-1120c", task="t1", generation=1, nonce=nonce, base_sha=base)
    assert "MANIFEST_TESTS_RUN" in str(err.value)


# ------------------------------------------------ reviewer model (record and blockers)

_OLD_REVIEW = {"family": "codex", "verdict": "APPROVE", "diff_sha": "d", "patch_id": "p", "tree": "t"}


@pytest.mark.parametrize("extra", [
    {},  # a record written before the model fields existed stays valid
    {"model": "gpt-6.2-sol"},
    {"model": "gpt-6.2-sol", "requested_model": "gpt-6.2-sol"},
])
def test_review_record_accepts_old_and_new_shapes(extra):
    doc = put(fresh_doc(), "a", cl.COLLECTED)
    review = dict(_OLD_REVIEW, **extra)
    cl.transition(doc, "a", cl.EV_REVIEW_APPROVE, generation=1, review=review)
    assert doc["tasks"]["a"]["state"] == cl.REVIEWED and doc["tasks"]["a"]["review"] == review


@pytest.mark.parametrize("bad", [
    dict(_OLD_REVIEW, surprise="x"),
    dict(_OLD_REVIEW, model=["gpt"]),
    {k: v for k, v in _OLD_REVIEW.items() if k != "tree"},
])
def test_review_record_still_refuses_unknown_missing_or_mistyped_keys(bad):
    doc = put(fresh_doc(), "a", cl.COLLECTED)
    with pytest.raises(cl.LedgerError) as err:
        cl.transition(doc, "a", cl.EV_REVIEW_APPROVE, generation=1, review=bad)
    assert err.value.code == cl.E_BAD_FIELD


@pytest.mark.parametrize("blocker", ["review-model-downgraded", "review-model-unknown"])
def test_review_model_blockers_park_a_collected_task_and_retest_re_reviews(blocker):
    doc = put(fresh_doc(), "a", cl.COLLECTED)
    cl.transition(doc, "a", cl.EV_NEEDS_OPERATOR, generation=1, blocker=blocker)
    entry = doc["tasks"]["a"]
    assert entry["state"] == cl.NEEDS_OPERATOR and entry["blocker"] == blocker and entry["resume_state"] == cl.COLLECTED
    assert blocker in cl.REVIEW_BLOCKERS
    cl.apply_resolve(doc, "a", "retest")
    assert entry["state"] == cl.COLLECTED and entry["review_attempts"] == 1
    # reject is also open to the operator
    doc2 = put(fresh_doc(), "a", cl.NEEDS_OPERATOR, blocker=blocker, resume_state="collected")
    cl.apply_resolve(doc2, "a", "reject")
    assert doc2["tasks"]["a"]["state"] == cl.REJECTED


def test_manifest_invalid_parks_a_result_ready_task_and_only_abandon_clears_it():
    doc = put(fresh_doc(), "a", cl.RESULT_READY, bound_oid=oid(1), bound_ref="refs/heads/claude/ffs-r1-a-aaaaaaaa")
    cl.transition(doc, "a", cl.EV_NEEDS_OPERATOR, generation=1, blocker="manifest-invalid",
                  decisions_needed=["manifest-invalid: MANIFEST_TESTS_RUN"])
    entry = doc["tasks"]["a"]
    assert (entry["state"], entry["blocker"], entry["resume_state"]) == (cl.NEEDS_OPERATOR, "manifest-invalid", cl.RESULT_READY)
    assert "manifest-invalid" in cl.ALL_BLOCKERS and "manifest-invalid" in cl.ABANDON_ONLY_BLOCKERS
    for action in cl.RESOLVE_ACTIONS:
        with pytest.raises(cl.LedgerError) as err:
            cl.apply_resolve(doc, "a", action)
        assert err.value.code == cl.E_BLOCKER_MISMATCH
    assert not any(e["op"] == cl.OP_RELEASE for e in cl.pending_journal(doc))  # result-ready held no slot
