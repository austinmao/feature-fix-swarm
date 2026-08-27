"""Wave-0 contract for the Phase-2 collector (REQ-201..203, EDGE-005).

The fixture deliberately uses a bare file-protocol origin and real worktrees.
It loads the collector only at test time so its absence is a typed RED
failure instead of a pytest collection error.

Plan 02-01 completed the Wave-0 placeholder harness into the GREEN-side
assertions below.  Every test still fails RED while the collector is absent
(``load_collector`` keeps its typed RED-EXPECTED path); once
``skills/land-queue/scripts/collect-queue.py`` ships, each test asserts the
real behavior named by its requirement scenario.
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "skills" / "land-queue" / "scripts" / "collect-queue.py"
ESTATE = ROOT / "skills" / "git-branch-consolidate" / "scripts" / "collect-estate.py"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


@pytest.fixture
def real_git_estate(tmp_path: Path) -> dict[str, Path | str]:
    """A real main/feature topology, local bare origin, and linked worktree."""
    origin, work, linked = tmp_path / "origin.git", tmp_path / "work", tmp_path / "linked"
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GIT_ASKPASS"):
        env.pop(key, None)
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, env=env, stdout=subprocess.PIPE)
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, env=env, stdout=subprocess.PIPE)
    git(work, "config", "user.email", "wave0@example.invalid")
    git(work, "config", "user.name", "Wave 0")
    (work / "README.md").write_text("base\n")
    git(work, "add", "README.md"); git(work, "commit", "-m", "base")
    git(work, "remote", "add", "origin", str(origin)); git(work, "push", "origin", "main")
    git(work, "checkout", "-b", "spec/201-docs")
    (work / "docs.md").write_text("docs only\n")
    git(work, "add", "docs.md"); git(work, "commit", "-m", "docs")
    head = git(work, "rev-parse", "HEAD")
    git(work, "push", "origin", "spec/201-docs")
    git(work, "checkout", "main")
    git(work, "worktree", "add", str(linked), "spec/201-docs")
    return {"origin": origin, "work": work, "linked": linked, "head": head}


def _writer_record(branch: str, head: str, run_id: str, spec_id: str) -> dict:
    """Byte-copy of scripts/gsd/takeover-record.py's writer output shape
    (schema_version 1): branch/head under git_state, both ids under ids —
    NO flat top-level identity fields (H5, ship round 5).  The old
    hand-shaped fixtures encoded a schema production never writes."""
    return {
        "schema_version": 1,
        "created_at": 0,
        "ids": {"spec_id": spec_id, "run_id": run_id},
        "gates_store": "/dev/null/evidence.json",
        "gates_store_anchor": "0" * 64,
        "git_state": {"branch": branch, "head": head, "upstream": "",
                      "dirty": []},
        "preflight": {}, "grants": [], "pendings": [], "promotions": [],
        "runner": {"status": "unknown", "pid": None, "live": False,
                   "process_state": "", "pid_start_time": "",
                   "boot_session_id": ""},
        "unresolved_findings": [], "phases": [], "evidence": [], "forbid": [],
        "resume": {"command": f"/spec-status {spec_id}", "preconditions": []},
    }


def load_collector():
    if not COLLECTOR.is_file():
        pytest.fail("RED-EXPECTED: REQ-201 collect-queue.py is not shipped")
    spec = importlib.util.spec_from_file_location("collect_queue", COLLECTOR)
    if spec is None or spec.loader is None:
        pytest.fail("RED-EXPECTED: REQ-201 collector has no loadable module spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules["collect_queue"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # typed failure keeps malformed targets out of collection phase
        pytest.fail(f"RED-EXPECTED: REQ-201 collector load failed: {type(exc).__name__}")
    return module


def _sanity(real_git_estate) -> Path:
    """The original Wave-0 real-Git fixture facts, preserved verbatim."""
    work = real_git_estate["work"]
    assert (work / ".git").exists()
    assert git(work, "remote", "get-url", "origin").startswith("/")
    assert git(work, "diff", "--name-only", "main...spec/201-docs") == "docs.md"
    assert ESTATE.is_file(), "collect-estate remains real first-party authority"
    return work


# ── REQ-201 ───────────────────────────────────────────────────────────────


def test_req201_fetch_first_and_three_source_union(real_git_estate, tmp_path):
    """REQ-201: fetch-first and three-source union."""
    work = _sanity(real_git_estate)
    head = real_git_estate["head"]
    module = load_collector()

    # A second explicit branch with production files.
    git(work, "checkout", "-b", "spec/201-code", "main")
    (work / "tool.py").write_text("x = 1\n")
    (work / "tests").mkdir()
    (work / "tests" / "test_tool.py").write_text("ok\n")
    git(work, "add", "tool.py", "tests/test_tool.py")
    git(work, "commit", "-m", "code")
    git(work, "push", "origin", "spec/201-code")
    git(work, "checkout", "main")

    # Prove fetch-first: drop remote truth locally; collect must restore it.
    git(work, "update-ref", "-d", "refs/remotes/origin/main")

    takeover_dir = tmp_path / "takeover"
    takeover_dir.mkdir()
    (takeover_dir / "spec-201.json").write_text(json.dumps(
        _writer_record("spec/201-docs", head, "spec-201", "201")))
    estate = [{"branch": "spec/201-docs", "disposition": "docs-only",
               "landed": False, "residual_files": 1, "spec_id": "201"}]

    doc = module.collect(repo=str(work), base="main",
                         explicit=["spec/201-docs", "spec/201-code"],
                         takeover_glob=str(takeover_dir / "*.json"),
                         estate=estate)
    assert doc["fetched"] is True
    assert git(work, "rev-parse", "refs/remotes/origin/main")  # fetch restored remote truth
    assert doc["conflicts"] == []
    assert [i["branch"] for i in doc["items"]] == ["spec/201-docs", "spec/201-code"]

    docs_item, code_item = doc["items"]
    assert docs_item["head"] == head
    assert docs_item["sources"] == ["estate", "explicit", "takeover"]
    assert docs_item["run_id"] == "spec-201"
    assert docs_item["spec_id"] == "201"
    assert docs_item["changed_files"] == ["docs.md"]
    assert docs_item["production_files"] == []           # root-level Markdown
    assert docs_item["production_touch"] is False

    assert code_item["sources"] == ["explicit"]
    assert code_item["changed_files"] == ["tests/test_tool.py", "tool.py"]
    assert code_item["production_files"] == ["tool.py"]  # tests/ is non-production
    assert code_item["production_touch"] is True
    assert code_item["head"] == git(work, "rev-parse", "spec/201-code")


def test_req201_docs_only_disposition_is_landable(real_git_estate):
    """REQ-201: docs-only disposition is landable; eligibility ignores landed."""
    work = _sanity(real_git_estate)
    module = load_collector()
    assert module.LANDABLE_DISPOSITIONS == {"merge-ready", "review-then-land", "docs-only"}
    estate = [
        # landed=True must NOT exclude a landable disposition (REQ-201).
        {"branch": "spec/201-docs", "disposition": "docs-only", "landed": True},
        {"branch": "spec/attention", "disposition": "pr-needs-attention", "landed": False},
        {"branch": "spec/stale", "disposition": "stale-abandoned", "landed": False},
        {"branch": "spec/residue", "disposition": "delete-safe", "landed": True},
    ]
    doc = module.collect(repo=str(work), base="main", estate=estate, fetch=False)
    assert [i["branch"] for i in doc["items"]] == ["spec/201-docs"]
    assert doc["items"][0]["sources"] == ["estate"]


def test_req201_identity_conflicts_block(real_git_estate, tmp_path):
    """REQ-201: head/run/spec identity conflicts block without a merged record."""
    work = _sanity(real_git_estate)
    module = load_collector()

    takeover_dir = tmp_path / "takeover"
    takeover_dir.mkdir()
    wrong_head = "0" * 40
    (takeover_dir / "a.json").write_text(json.dumps(
        _writer_record("spec/201-docs", wrong_head, "spec-201", "201")))
    doc = module.collect(repo=str(work), base="main",
                         explicit=["spec/201-docs"],
                         takeover_glob=str(takeover_dir / "*.json"), fetch=False)
    assert doc["items"] == []
    [conflict] = doc["conflicts"]
    assert conflict["status"] == "BLOCKED:identity-conflict"
    assert conflict["branch"] == "spec/201-docs"
    assert conflict["unblock"]
    # No richest-record merge: the conflict carries no merged authority fields.
    assert "head" not in conflict and "run_id" not in conflict and "spec_id" not in conflict
    # Source records are preserved, each still carrying its own claim.
    heads = {c.get("head") for c in conflict["candidates"] if c.get("head")}
    assert wrong_head in heads

    # run-id disagreement between two takeover records also blocks.
    head = real_git_estate["head"]
    (takeover_dir / "a.json").write_text(json.dumps(
        _writer_record("spec/201-docs", head, "spec-201", "201")))
    (takeover_dir / "b.json").write_text(json.dumps(
        _writer_record("spec/201-docs", head, "spec-999", "201")))
    doc = module.collect(repo=str(work), base="main",
                         takeover_glob=str(takeover_dir / "*.json"), fetch=False)
    assert doc["items"] == []
    assert doc["conflicts"][0]["status"] == "BLOCKED:identity-conflict"


def test_req201_empty_intake_is_deterministic(real_git_estate, capsys):
    """REQ-201: empty intake is deterministic (byte-identical serialization)."""
    work = _sanity(real_git_estate)
    module = load_collector()
    rc1 = module.main(["collect", "--repo", str(work), "--base", "main", "--no-fetch"])
    out1 = capsys.readouterr().out
    rc2 = module.main(["collect", "--repo", str(work), "--base", "main", "--no-fetch"])
    out2 = capsys.readouterr().out
    assert rc1 == 0 and rc2 == 0
    assert out1 == out2
    doc = json.loads(out1)
    assert doc["items"] == [] and doc["count"] == 0 and doc["conflicts"] == []


# ── REQ-202 ───────────────────────────────────────────────────────────────


def _item(branch: str, files: list[str], epoch: int) -> dict:
    return {"branch": branch, "head": "f" * 40, "changed_files": list(files),
            "production_files": [], "production_touch": False,
            "residual_count": len(files), "committed_epoch": epoch,
            "sources": ["explicit"], "run_id": None, "spec_id": None}


def test_req202_transitive_overlap_components_serialize_oldest_first(real_git_estate):
    """REQ-202: A~B and B~C form one component even when A does not touch C."""
    _sanity(real_git_estate)
    module = load_collector()
    a = _item("spec/a", ["f1", "f2"], 300)
    b = _item("spec/b", ["f2", "f3"], 100)
    c = _item("spec/c", ["f3", "f4"], 200)   # no overlap with a — transitive via b
    d = _item("spec/d", ["z1"], 50)          # disjoint singleton
    for perm in itertools.permutations([a, b, c, d]):
        ordered = [i["branch"] for i in module.order_queue(list(perm))]
        assert ordered == ["spec/b", "spec/c", "spec/a", "spec/d"], perm


def test_req202_disjoint_residual_file_ordering_is_stable(real_git_estate):
    """REQ-202: disjoint singletons sort by residual-file count, then branch."""
    _sanity(real_git_estate)
    module = load_collector()
    three = _item("spec/three", ["a", "b", "c"], 10)
    one = _item("spec/one", ["x"], 999)
    two = _item("spec/two", ["y", "z"], 5)
    tie = _item("spec/aaa-tie", ["q"], 500)   # count ties with spec/one
    for perm in itertools.permutations([three, one, two, tie]):
        ordered = [i["branch"] for i in module.order_queue(list(perm))]
        assert ordered == ["spec/aaa-tie", "spec/one", "spec/two", "spec/three"], perm


# ── REQ-203 / EDGE-005 ────────────────────────────────────────────────────


def test_req203_already_landed_is_skipped_at_item_start(real_git_estate):
    """REQ-203: already-landed is skipped at item start."""
    work = _sanity(real_git_estate)
    head = real_git_estate["head"]
    module = load_collector()
    git(work, "merge", "--no-ff", "-m", "land", "spec/201-docs")
    git(work, "push", "origin", "main")
    res = module.precheck(repo=str(work), base="main",
                          branch="spec/201-docs", head=head)
    assert res["status"] == "SKIPPED:already-landed"


def test_req203_gone_branch_with_reachable_head_reconciles_landed(real_git_estate):
    """REQ-203: gone branch with recorded head reachable from base reconciles LANDED."""
    work = _sanity(real_git_estate)
    linked = real_git_estate["linked"]
    head = real_git_estate["head"]
    module = load_collector()
    git(work, "merge", "--no-ff", "-m", "land", "spec/201-docs")
    git(work, "push", "origin", "main")
    git(work, "worktree", "remove", "--force", str(linked))
    git(work, "branch", "-d", "spec/201-docs")
    git(work, "push", "origin", "--delete", "spec/201-docs")
    res = module.precheck(repo=str(work), base="main",
                          branch="spec/201-docs", head=head)
    assert res["status"] == "LANDED"
    assert res["merge_sha"] == head


def test_req203_gone_branch_without_proof_blocks_source_missing(real_git_estate):
    """REQ-203: gone branch without proof blocks source-missing.

    Also proves the 221c8690 binding: squash-merge landed-ness reconciles via
    gh PR state MERGED + a mergeCommit reachable from base, never via
    ancestor-of-source-head alone.
    """
    work = _sanity(real_git_estate)
    linked = real_git_estate["linked"]
    head = real_git_estate["head"]
    module = load_collector()
    git(work, "worktree", "remove", "--force", str(linked))
    git(work, "branch", "-D", "spec/201-docs")
    git(work, "push", "origin", "--delete", "spec/201-docs")

    res = module.precheck(repo=str(work), base="main",
                          branch="spec/201-docs", head=head)
    assert res["status"] == "BLOCKED:source-missing"
    assert res["unblock"]

    # A MERGED PR whose merge commit is NOT reachable from base is no proof.
    res = module.precheck(repo=str(work), base="main",
                          branch="spec/201-docs", head=head,
                          pr_state={"state": "MERGED", "merge_commit": "0" * 40})
    assert res["status"] == "BLOCKED:source-missing"

    # Simulate a squash landing: equivalent content, new SHA, pushed to base.
    (work / "docs.md").write_text("docs only\n")
    git(work, "add", "docs.md")
    git(work, "commit", "-m", "squash: docs")
    squash_sha = git(work, "rev-parse", "HEAD")
    git(work, "push", "origin", "main")
    res = module.precheck(repo=str(work), base="main",
                          branch="spec/201-docs", head=head,
                          pr_state={"state": "MERGED", "merge_commit": squash_sha})
    assert res["status"] == "LANDED"
    assert res["merge_sha"] == squash_sha


def test_req203_one_rebase_merge_tree_conflict_blocks(real_git_estate):
    """REQ-203: after the one trial rebase, a merge-tree conflict blocks."""
    work = _sanity(real_git_estate)
    head = real_git_estate["head"]
    module = load_collector()
    (work / "docs.md").write_text("conflicting base content\n")
    git(work, "add", "docs.md")
    git(work, "commit", "-m", "conflict")
    git(work, "push", "origin", "main")
    res = module.precheck(repo=str(work), base="main",
                          branch="spec/201-docs", head=head)
    assert res["status"] == "BLOCKED:conflict"
    assert "rebase" in res["unblock"]


def test_edge005_external_merge_is_rechecked_before_dispatch(real_git_estate, tmp_path):
    """EDGE-005: an external merge is discovered at item start, model-free."""
    work = _sanity(real_git_estate)
    origin = real_git_estate["origin"]
    module = load_collector()

    # An external actor lands the branch on origin/main behind our back.
    ext = tmp_path / "ext"
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run(["git", "clone", "-q", str(origin), str(ext)], check=True, env=env)
    git(ext, "config", "user.email", "ext@example.invalid")
    git(ext, "config", "user.name", "External")
    git(ext, "merge", "--no-ff", "-m", "external land", "origin/spec/201-docs")
    git(ext, "push", "origin", "main")

    # Our checkout has NOT observed that merge; the precheck must fetch and
    # reconcile from authority alone (no model/reviewer seam even exists).
    res = module.precheck(repo=str(work), base="main",
                          branch="spec/201-docs", head=real_git_estate["head"])
    assert res["status"] == "SKIPPED:already-landed"
    assert set(res) <= {"status", "reason", "unblock", "branch", "head", "base", "merge_sha"}


# ── coverage / public API contract ────────────────────────────────────────


def test_coverage_contract_targets_only_stable_dynamic_module_name(real_git_estate, capsys, tmp_path):
    module = load_collector()
    assert module.__name__ == "collect_queue"
    for name in ("collect", "precheck", "order_queue", "production_files",
                 "main", "get_scalar", "emit_array0"):
        assert callable(getattr(module, name)), name

    # Closed non-production allowlist; everything else is production-touching.
    assert module.production_files([
        "tests/a.py", "lib/tests/b.py", "docs/c.md", ".planning/d.md",
        "specs/e.md", "README.md", "src/x.py", "nested/readme.md",
    ]) == ["src/x.py", "nested/readme.md"]

    # Hostile values reach argv/stdout byte-exact through the accessors.
    hostile = ["has space.txt", "tab\tname.txt", "glob*?.txt", "-leading-dash",
               "semi;colon.txt", "$(touch marker).txt", "embedded\nnewline.txt"]
    doc = {"schema": 1, "count": 1, "base": "main", "fetched": False,
           "conflicts": [],
           "items": [{"branch": "spec/x", "head": "a" * 40,
                      "changed_files": hostile, "production_files": [],
                      "production_touch": False, "residual_count": len(hostile),
                      "committed_epoch": 1, "sources": ["explicit"],
                      "run_id": None, "spec_id": None,
                      "reason": "bad\nnewline"}]}
    path = tmp_path / "doc.json"
    path.write_text(json.dumps(doc))

    rc = module.main(["emit-array0", "--doc", str(path), "--item", "0",
                      "--field", "changed_files"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "".join(f"{v}\0" for v in hostile)
    assert not (tmp_path / "marker").exists()

    rc = module.main(["get-scalar", "--doc", str(path), "--item", "0",
                      "--field", "branch"])
    out = capsys.readouterr().out
    assert rc == 0 and out == "spec/x\n"

    # Scalars fail closed: embedded newline and non-allowlisted fields refuse.
    rc = module.main(["get-scalar", "--doc", str(path), "--item", "0",
                      "--field", "reason"])
    captured = capsys.readouterr()
    assert rc != 0 and captured.out == ""
    rc = module.main(["get-scalar", "--doc", str(path), "--item", "0",
                      "--field", "__class__"])
    captured = capsys.readouterr()
    assert rc != 0 and captured.out == ""


# ── spec-006 ship round 5 (H5): canonical takeover-record intake ──────────


def test_h5_real_writer_record_is_loaded_by_intake(real_git_estate, tmp_path):
    """H5 (ship round 5): the intake loader parses the CANONICAL versioned
    schema the real takeover-record.py writer produces (ids.* + git_state.*).
    RED probe: with the flat-field loader a real writer record intakes as
    nothing at all."""
    work = _sanity(real_git_estate)
    linked = real_git_estate["linked"]  # checked out on spec/201-docs
    head = real_git_estate["head"]
    module = load_collector()
    gates_dir = tmp_path / "gates"
    gates_dir.mkdir()
    env = {**os.environ, "GATES_STORE": str(gates_dir / "evidence.json"),
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
           "TAKEOVER_TEST_IDENTITY": "pytest-boot-1"}
    writer = ROOT / "scripts" / "gsd" / "takeover-record.py"
    proc = subprocess.run(
        [sys.executable, str(writer), "--gates",
         str(ROOT / "lib" / "gates.py"), "--spec-id", "201",
         "--run-id", "spec-201"],
        cwd=linked, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    records = module.load_takeover_records(
        str(gates_dir / "takeover" / "*.json"))
    assert records == [{"source": "takeover", "branch": "spec/201-docs",
                        "head": head, "run_id": "spec-201",
                        "spec_id": "201"}]
    doc = module.collect(repo=str(work), base="main",
                         takeover_glob=str(gates_dir / "takeover" / "*.json"),
                         fetch=False)
    assert [i["branch"] for i in doc["items"]] == ["spec/201-docs"]
    assert doc["items"][0]["run_id"] == "spec-201"
    assert doc["items"][0]["spec_id"] == "201"


def test_h5_legacy_flat_record_still_loads(real_git_estate, tmp_path):
    """Legacy flat top-level fields stay accepted as a trivially cheap
    fallback; the canonical nested shape is authoritative when both exist."""
    _sanity(real_git_estate)
    head = real_git_estate["head"]
    module = load_collector()
    tdir = tmp_path / "takeover"
    tdir.mkdir()
    (tdir / "legacy.json").write_text(json.dumps({
        "branch": "spec/201-docs", "head": head,
        "run_id": "spec-201", "spec_id": "201"}))
    [rec] = module.load_takeover_records(str(tdir / "*.json"))
    assert rec == {"source": "takeover", "branch": "spec/201-docs",
                   "head": head, "run_id": "spec-201", "spec_id": "201"}
