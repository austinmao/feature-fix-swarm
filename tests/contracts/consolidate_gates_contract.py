"""Direct Wave-0 contract for the queue-derived consolidate:estate grant.

Filename intentionally avoids pytest's default test_*.py rules (convention:
land_queue_gates_contract.py) — evidence is always the explicitly named
direct path.

Phase 3 contract (REQ/T-03-02): a consolidation may run only under an exact
queue-derived grant scope `consolidate:estate:<sha256(target tuples)>` with
TTL <= 8h, refused on scope substitution and on expiry at the effect
boundary.  The generic exact-match/expiry machinery already landed in
Phase 2 (GREEN below); the consolidate-specific scope-format requirement and
8h TTL cap have NOT (RED below, typed marker
EXPECTED-RED:GRANT:missing-consolidate-grant).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATES_PATH = ROOT / "lib" / "gates.py"
MARKER = "EXPECTED-RED:GRANT:missing-consolidate-grant"
CAP_HOURS = 8.0


def gates_module():
    if not GATES_PATH.is_file():
        pytest.fail("lib/gates.py target is absent")
    spec = importlib.util.spec_from_file_location("consolidate_real_gates", GATES_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def queue_scope(tuples: list[tuple[str, str]]) -> str:
    """The queue-derived exact scope: sha256 over the canonical serialized
    (branch_ref, expected_tip_oid) tuples — never a hand-typed constant."""
    canon = json.dumps(sorted([list(t) for t in tuples]),
                       separators=(",", ":")).encode()
    return "consolidate:estate:" + hashlib.sha256(canon).hexdigest()


QJ_PATH = ROOT / "skills" / "land-queue" / "scripts" / "queue-journal.py"


def make_journal(tmp_path, queue_id="q-cr03", run_id="run-q",
                 items=(("spec/merged", "a" * 40, "201", "b" * 40),),
                 nonterminal_item=None):
    """Real durable queue journal built through the production accessor —
    the ONLY manifest source grant-consolidate may trust (CR-03)."""
    store = tmp_path / "lq"
    store.mkdir()
    store.chmod(0o700)

    def qj(*args):
        proc = subprocess.run([sys.executable, str(QJ_PATH), *args,
                               "--store", str(store), "--queue-id", queue_id],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
    qj("init", "--run-id", run_id)
    for branch, head, pr, merge in items:
        qj("append", "--kind", "intent", "--step", "merge",
           "--item", branch, "--pr", pr, "--head", head)
        qj("append", "--kind", "terminal", "--step", "terminal",
           "--item", branch, "--status", "LANDED", "--detail", merge)
    if nonterminal_item:
        qj("append", "--kind", "intent", "--step", "merge",
           "--item", nonterminal_item, "--pr", "999", "--head", "f" * 40)
    return store


TUPLES = [("spec/merged", "a" * 40)]
OTHER_TUPLES = [("spec/other", "b" * 40)]


def _red(cond: bool, why: str) -> None:
    """Behavioral RED: emit the exact typed marker line, then fail."""
    if not cond:
        print(MARKER)
        pytest.fail(why)


# ── GREEN: generic exact-match machinery already landed in Phase 2 ────────

def test_exact_scope_matches_and_substitution_is_refused(tmp_path):
    gates = gates_module()
    store = tmp_path / "evidence.json"
    scope = queue_scope(TUPLES)
    assert gates.grant_actions(store, "run-1", [scope], ttl_hours=CAP_HOURS,
                               _allow_consolidate=True)
    assert gates.check_grant(store, "run-1", scope) is True
    # scope substitution: a DIFFERENT queue-derived manifest never matches
    assert gates.check_grant(store, "run-1", queue_scope(OTHER_TUPLES)) is False
    # run substitution never matches either
    assert gates.check_grant(store, "run-2", scope) is False


def test_expiry_is_refused_at_the_effect_boundary(tmp_path):
    gates = gates_module()
    store = tmp_path / "evidence.json"
    scope = queue_scope(TUPLES)
    assert gates.grant_actions(store, "run-1", [scope], ttl_hours=CAP_HOURS,
                               _allow_consolidate=True)
    granted = json.loads(store.read_text())["_autonomy"]["run-1"]["grants"][scope]
    at = granted["granted_at"]
    assert gates.check_grant(store, "run-1", scope, now=at + CAP_HOURS * 3600 - 1)
    assert gates.check_grant(store, "run-1", scope,
                             now=at + CAP_HOURS * 3600 + 1) is False


def test_free_prose_action_is_rejected(tmp_path):
    gates = gates_module()
    store = tmp_path / "evidence.json"
    assert gates.grant_actions(store, "run-1", ["delete all the branches"]) is False


# ── RED: consolidate-specific enforcement Phase 3 must add ────────────────

def test_consolidate_ttl_is_capped_at_8h(tmp_path):
    gates = gates_module()
    store = tmp_path / "evidence.json"
    scope = queue_scope(TUPLES)
    accepted = gates.grant_actions(store, "run-1", [scope], ttl_hours=9.0,
                                   _allow_consolidate=True)
    _red(accepted is False,
         "a consolidate:estate grant with TTL 9h was accepted; the "
         "consolidate cap is 8h (T-03-02)")


def test_bare_consolidate_estate_without_queue_scope_is_rejected(tmp_path):
    gates = gates_module()
    store = tmp_path / "evidence.json"
    accepted = gates.grant_actions(store, "run-1", ["consolidate:estate"],
                                   ttl_hours=CAP_HOURS,
                                   _allow_consolidate=True)
    _red(accepted is False,
         "a bare consolidate:estate grant (no queue-derived sha256 scope) "
         "was accepted; the scope must pin the exact target manifest")


def test_default_ttl_cannot_outlive_the_8h_cap(tmp_path):
    gates = gates_module()
    store = tmp_path / "evidence.json"
    scope = queue_scope(TUPLES)
    accepted = gates.grant_actions(store, "run-1", [scope],
                                   _allow_consolidate=True)  # default TTL
    if accepted:
        live_past_cap = gates.check_grant(store, "run-1", scope,
                                          now=json.loads(store.read_text())
                                          ["_autonomy"]["run-1"]["grants"][scope]
                                          ["granted_at"] + CAP_HOURS * 3600 + 1)
        _red(live_past_cap is False,
             "a consolidate:estate grant under the default TTL is still "
             "valid past the 8h cap")


def test_generic_grant_actions_refuses_consolidate_scopes(tmp_path):
    """CR-03: the public grant machinery must never mint a consolidate:*
    action — only the queue-derived grant_consolidate_estate path may."""
    gates = gates_module()
    store = tmp_path / "evidence.json"
    scope = gates.consolidate_scope([("spec/a", "a" * 40, 17, "b" * 40)],
                                    repo_root="/r", base="main")
    assert gates.grant_actions(store, "run-1", [scope], ttl_hours=1.0) is False
    assert gates.check_grant(store, "run-1", scope) is False


def test_generic_grant_cli_refuses_consolidate_with_typed_reason(tmp_path):
    """CR-03 probe fix: `gates.py grant ... --action consolidate:estate:<sha>`
    returned rc 0 GRANTED.  It must refuse with a typed reason and mint
    nothing."""
    scope = "consolidate:estate:" + "0" * 64
    env = dict(os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    proc = subprocess.run(
        [sys.executable, str(GATES_PATH), "grant", "probe-run",
         "--action", scope, "--ttl-hours", "1", "--reason", "probe"],
        capture_output=True, text=True, env=env, cwd=tmp_path)
    assert proc.returncode != 0, "generic grant minted a consolidate:* action"
    combined = proc.stdout + proc.stderr
    assert "GRANT-REJECTED" in combined and "grant-consolidate" in combined, \
        f"refusal is not typed toward the queue-derived path: {combined!r}"
    check = subprocess.run(
        [sys.executable, str(GATES_PATH), "check-grant", "probe-run",
         "--action", scope], capture_output=True, text=True, env=env,
        cwd=tmp_path)
    assert check.returncode != 0, "a refused grant is still checkable"


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    return os.path.realpath(repo)


def _grant_consolidate(tmp_path, run_id, queue_id, journal_store, stdin=b""):
    env = dict(os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    return subprocess.run(
        [sys.executable, str(GATES_PATH), "grant-consolidate", run_id,
         "--queue-id", queue_id, "--journal-store", str(journal_store),
         "--repo", str(tmp_path / "repo"), "--base", "main"],
        input=stdin, capture_output=True, env=env, cwd=tmp_path)


def test_grant_consolidate_derives_tuples_from_the_named_journal(tmp_path):
    """CR-03: grant-consolidate loads the named queue journal itself and
    derives the canonical tuples from it — hostile stdin is ignored."""
    gates = gates_module()
    repo_root = make_repo(tmp_path)
    journal = make_journal(tmp_path)
    proc = _grant_consolidate(tmp_path, "run-q", "q-cr03", journal,
                              stdin=b"evil\x00" + b"9" * 40 + b"\x001\x00"
                              + b"8" * 40 + b"\x00")
    assert proc.returncode == 0, proc.stderr
    expected = gates.consolidate_scope(
        [("spec/merged", "a" * 40, 201, "b" * 40)],
        repo_root=repo_root, base="main")
    assert expected in proc.stdout.decode(), \
        "granted scope is not derived from the journal projection"
    env = dict(os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    check = subprocess.run(
        [sys.executable, str(GATES_PATH), "check-grant", "run-q",
         "--action", expected], capture_output=True, env=env, cwd=tmp_path)
    assert check.returncode == 0


def test_grant_consolidate_refuses_run_binding_mismatch(tmp_path):
    """CR-03: the caller-supplied run id must equal the journal's recorded
    run id — a substituted run id never grafts queue authority."""
    gates = gates_module()
    repo_root = make_repo(tmp_path)
    journal = make_journal(tmp_path)
    valid_stdin = b"spec/merged\x00" + b"a" * 40 + b"\x00201\x00" \
        + b"b" * 40 + b"\x00"
    proc = _grant_consolidate(tmp_path, "run-SUBSTITUTED", "q-cr03", journal,
                              stdin=valid_stdin)
    assert proc.returncode != 0, \
        "grant-consolidate minted under a run id the journal never recorded"
    scope = gates.consolidate_scope([("spec/merged", "a" * 40, 201, "b" * 40)],
                                    repo_root=repo_root, base="main")
    for run in ("run-SUBSTITUTED", "run-q"):
        env = dict(os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
        check = subprocess.run(
            [sys.executable, str(GATES_PATH), "check-grant", run,
             "--action", scope], capture_output=True, env=env, cwd=tmp_path)
        assert check.returncode != 0, f"grant leaked for {run}"


def test_grant_consolidate_refuses_a_nonterminal_queue(tmp_path):
    """CR-03: minting requires the whole queue to be items-terminal — a
    dangling item means the queue is still live."""
    make_repo(tmp_path)
    journal = make_journal(tmp_path, nonterminal_item="spec/live")
    valid_stdin = b"spec/merged\x00" + b"a" * 40 + b"\x00201\x00" \
        + b"b" * 40 + b"\x00"
    proc = _grant_consolidate(tmp_path, "run-q", "q-cr03", journal,
                              stdin=valid_stdin)
    assert proc.returncode != 0, \
        "grant-consolidate minted while an item is still nonterminal"


def test_scope_differs_when_only_pr_differs():
    """CR-04: the canonical scope must cover ALL FOUR tuple fields.  Two
    manifests identical in (branch, head) but differing only in PR number
    must produce different scopes -- otherwise a grant minted for one merged
    PR authorizes finalizing a different PR with the same head."""
    gates = gates_module()
    a = gates.consolidate_scope([("spec/a", "a" * 40, 17, "b" * 40)],
                                repo_root="/r", base="main")
    b = gates.consolidate_scope([("spec/a", "a" * 40, 18, "b" * 40)],
                                repo_root="/r", base="main")
    assert a != b, ("scope collision: PR number is not part of the "
                    "canonical consolidate scope")


def test_scope_differs_when_only_merge_commit_differs():
    """CR-04: same collision requirement for the observed merge commit."""
    gates = gates_module()
    a = gates.consolidate_scope([("spec/a", "a" * 40, 17, "b" * 40)],
                                repo_root="/r", base="main")
    b = gates.consolidate_scope([("spec/a", "a" * 40, 17, "c" * 40)],
                                repo_root="/r", base="main")
    assert a != b, ("scope collision: the observed merge commit is not part "
                    "of the canonical consolidate scope")


def test_minted_grant_never_matches_a_pr_substituted_scope(tmp_path):
    """CR-04 refusal path: a grant minted for one canonical tuple must fail
    check_grant for the scope of the SAME (branch, head) under a different
    PR/merge pair."""
    gates = gates_module()
    store = tmp_path / "evidence.json"
    minted = gates.grant_consolidate_estate(
        store, "run-1", [("spec/a", "a" * 40, 17, "b" * 40)], queue_id="q1",
        repo_root="/r", base="main")
    assert minted, "queue-derived mint refused a valid canonical tuple"
    substituted = gates.consolidate_scope([("spec/a", "a" * 40, 18, "c" * 40)],
                                          repo_root="/r", base="main")
    assert substituted != minted
    assert gates.check_grant(store, "run-1", substituted) is False


def test_scope_binds_repository_identity_and_base():
    """CR-07: the canonical scope must include the physical repository root
    and the base branch — a grant proven in repository A must never check
    out in repository B (or against another base)."""
    gates = gates_module()
    t = [("spec/a", "a" * 40, 17, "b" * 40)]
    one = gates.consolidate_scope(t, repo_root="/repo/one", base="main")
    two = gates.consolidate_scope(t, repo_root="/repo/two", base="main")
    other_base = gates.consolidate_scope(t, repo_root="/repo/one",
                                         base="develop")
    assert one != two, "scope ignores repository identity"
    assert one != other_base, "scope ignores the base branch"


def test_cli_refuses_over_cap_consolidate_grant(tmp_path):
    scope = queue_scope(TUPLES)
    env = dict(os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    proc = subprocess.run(
        [sys.executable, str(GATES_PATH), "grant", "run-cli",
         "--action", scope, "--ttl-hours", "24", "--reason", "contract"],
        capture_output=True, text=True, env=env, cwd=tmp_path)
    _red(proc.returncode != 0,
         "the gates.py CLI accepted a consolidate:estate grant with a 24h "
         "TTL; consolidate grants are capped at 8h")


# ── CR-02 (round 2): the queue deadline is journal-immutable ──────────────
# The caller must never be able to extend an expired queue deadline: `init`
# records the absolute deadline, mint derives lifetime ONLY from it, and the
# caller-controlled --queue-timeout-seconds flag is refused outright.

QUEUE_WALL_SECONDS = 28800


def test_journal_init_records_absolute_queue_deadline(tmp_path):
    """CR-02: `init` persists created_at + the fixed queue wall as the
    absolute deadline; grant lifetime may derive ONLY from this field."""
    make_repo(tmp_path)
    journal = make_journal(tmp_path)
    doc = json.loads((journal / "q-cr03.json").read_text())
    deadline = doc.get("deadline")
    assert isinstance(deadline, int) and not isinstance(deadline, bool), \
        "journal init records no absolute queue deadline"
    assert deadline == doc["created_at"] + QUEUE_WALL_SECONDS, \
        "deadline is not created_at + the fixed queue wall"


def test_mint_refuses_caller_supplied_queue_timeout_flag(tmp_path):
    """CR-02: --queue-timeout-seconds is caller-controlled deadline authority
    and is removed from the production mint path — its presence refuses
    outright with no grant write."""
    make_repo(tmp_path)
    journal = make_journal(tmp_path)
    env = dict(os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    proc = subprocess.run(
        [sys.executable, str(GATES_PATH), "grant-consolidate", "run-q",
         "--queue-id", "q-cr03", "--journal-store", str(journal),
         "--repo", str(tmp_path / "repo"), "--base", "main",
         "--queue-timeout-seconds", "999999"],
        capture_output=True, text=True, env=env, cwd=tmp_path)
    assert proc.returncode != 0, \
        "grant-consolidate accepted a caller-supplied queue timeout"
    store = tmp_path / "evidence.json"
    assert not store.exists() or "consolidate:estate:" not in store.read_text(), \
        "a grant was written despite the caller-timeout refusal"


def test_late_mint_refuses_after_journal_deadline_despite_oversized_env(tmp_path):
    """CR-02: once the journal's recorded absolute deadline has passed, no
    derivation re-opens authority — an oversized caller timeout after the
    original deadline must refuse with no grant write."""
    make_repo(tmp_path)
    journal = make_journal(tmp_path)
    path = journal / "q-cr03.json"
    doc = json.loads(path.read_text())
    doc["created_at"] = int(time.time()) - (QUEUE_WALL_SECONDS + 7200)
    if "deadline" in doc:
        doc["deadline"] = doc["created_at"] + QUEUE_WALL_SECONDS
    path.write_text(json.dumps(doc, sort_keys=True))
    env = dict(os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    # the pre-fix hole: an oversized caller timeout re-opened the window
    proc = subprocess.run(
        [sys.executable, str(GATES_PATH), "grant-consolidate", "run-q",
         "--queue-id", "q-cr03", "--journal-store", str(journal),
         "--repo", str(tmp_path / "repo"), "--base", "main",
         "--queue-timeout-seconds", "999999"],
        capture_output=True, text=True, env=env, cwd=tmp_path)
    assert proc.returncode != 0, \
        "an oversized caller timeout re-opened an expired queue deadline"
    # and the flagless direct path must refuse on the journal deadline alone
    proc2 = _grant_consolidate(tmp_path, "run-q", "q-cr03", journal)
    assert proc2.returncode != 0, \
        "a late derivation minted after the journal deadline passed"
    store = tmp_path / "evidence.json"
    assert not store.exists() or "consolidate:estate:" not in store.read_text(), \
        "a grant was written after the original queue deadline"
