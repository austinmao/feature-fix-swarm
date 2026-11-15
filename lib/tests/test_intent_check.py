"""F1 intent contract: `gates.py intent-check` pins the spec's intent block and
checks production line delta at finish. Every test points GATES_STORE at a
tmp_path file; git repos are temp repos driven through FFS_BASE."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

GATES = Path(__file__).resolve().parents[1] / "gates.py"
SPEC = "spec-123"
SPEC_REL = "specs/123-feat/spec.md"
PARENT_REL = "specs/100-parent/spec.md"
MARKER = "<!-- ffs:intent -->"


def block(delta="negative", budget=0, targets=None, inherit=None):
    lines = [MARKER, f"prod_delta: {delta}", f"prod_budget_lines: {budget}"]
    if targets:
        lines.append(f"delete_targets: {targets}")
    if inherit:
        lines.append(f"inherit: {inherit}")
    return "\n".join(lines)


def spec_text(intent_block):
    body = f"# Spec 123\n\n## Intent\n{intent_block}\n\n## Other\nsome prose\n"
    return body if intent_block else "# Spec 123\n\n## Other\nsome prose\n"


@pytest.fixture
def ctx(tmp_path):
    store = tmp_path / "gates.json"
    env = {k: v for k, v in os.environ.items()
           if k not in ("FFS_SPEC_ID", "GSD_RUN_ID", "GATES_STORE", "FFS_BASE")}
    env["GATES_STORE"] = str(store)
    repo = tmp_path / "repo"
    repo.mkdir()
    return {"store": store, "env": env, "repo": repo}


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@invalid", *args],
        cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def write(repo, rel, text):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def lines(n, tag="x"):
    return "".join(f"{tag}{i}\n" for i in range(n))


def init_repo(ctx, intent_block, parent=None):
    """Base commit: src/old.py (20 lines), src/keep.py (40 lines), spec(s)."""
    repo = ctx["repo"]
    git(repo, "init", "-q", "-b", "123-feat")
    write(repo, "src/old.py", lines(20, "old"))
    write(repo, "src/keep.py", lines(40, "keep"))
    write(repo, SPEC_REL, spec_text(intent_block))
    if parent is not None:
        write(repo, PARENT_REL, spec_text(parent))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    ctx["base"] = git(repo, "rev-parse", "HEAD")
    ctx["env"]["FFS_BASE"] = ctx["base"]
    return repo


def set_spec(ctx, intent_block):
    write(ctx["repo"], SPEC_REL, spec_text(intent_block))


def run(ctx, stage, *extra, spec=SPEC_REL):
    return subprocess.run(
        [sys.executable, str(GATES), "intent-check", "--spec", spec,
         "--stage", stage, *extra],
        env=ctx["env"], cwd=ctx["repo"], capture_output=True, text=True,
        timeout=60)


def out(res):
    return res.stdout + res.stderr


def store(ctx):
    return json.loads(ctx["store"].read_text())


def pinned_repo(ctx, intent_block):
    init_repo(ctx, intent_block)
    assert run(ctx, "plan").returncode == 0
    return ctx["repo"]


# 1
def test_plan_first_run_pins_sha_and_rev(ctx):
    init_repo(ctx, block())
    res = run(ctx, "plan")
    assert res.returncode == 0, out(res)
    entry = store(ctx)["_intent"][SPEC]
    assert entry["sha256"]
    assert entry["rev"] == 1


# 2
def test_plan_unchanged_block_passes(ctx):
    pinned_repo(ctx, block())
    res = run(ctx, "plan")
    assert res.returncode == 0, out(res)
    assert store(ctx)["_intent"][SPEC]["rev"] == 1


# 3
@pytest.mark.parametrize("stage", ["plan", "finish"])
def test_edited_block_after_pin_fails(ctx, stage):
    pinned_repo(ctx, block(budget=0))
    set_spec(ctx, block(budget=5))
    res = run(ctx, stage)
    assert res.returncode == 1
    assert "INTENT-FAIL" in out(res)
    assert "pinned" in out(res)


# 4
def test_removed_block_after_pin_fails(ctx):
    pinned_repo(ctx, block())
    set_spec(ctx, "")
    res = run(ctx, "plan")
    assert res.returncode == 1
    assert "INTENT-FAIL" in out(res)


# 5
def test_absent_block_never_pinned_is_ok(ctx):
    init_repo(ctx, "")
    res = run(ctx, "plan")
    assert res.returncode == 0, out(res)
    assert "INTENT:absent" in out(res)


# 6
@pytest.mark.parametrize("bad", [
    block(delta="smaller"),
    block(budget=-1),
    block(budget="abc"),
    block(budget="1.5"),
])
def test_invalid_block_fails(ctx, bad):
    init_repo(ctx, bad)
    res = run(ctx, "plan")
    assert res.returncode == 1
    assert "INTENT-FAIL" in out(res)


# 7
@pytest.mark.parametrize("child_delta,child_budget,expected", [
    ("positive", 0, 1),   # cannot loosen sign
    ("neutral", 0, 1),    # cannot loosen sign
    ("negative", 50, 1),  # cannot raise budget
    ("negative", 0, 0),
])
def test_inherit_cannot_loosen_parent(ctx, child_delta, child_budget, expected):
    init_repo(ctx, block(child_delta, child_budget, inherit=PARENT_REL),
              parent=block("negative", 0))
    res = run(ctx, "plan")
    assert res.returncode == expected, out(res)
    if expected:
        assert "INTENT-FAIL" in out(res)


def test_inherit_neutral_parent_allows_negative_child(ctx):
    init_repo(ctx, block("negative", 5, inherit=PARENT_REL),
              parent=block("neutral", 10))
    assert run(ctx, "plan").returncode == 0


def test_inherit_neutral_parent_rejects_positive_child(ctx):
    init_repo(ctx, block("positive", 5, inherit=PARENT_REL),
              parent=block("neutral", 10))
    assert run(ctx, "plan").returncode == 1


# 8
def test_repin_after_edit_bumps_rev_and_keeps_loops(ctx):
    pinned_repo(ctx, block(budget=5))
    loop = subprocess.run(
        [sys.executable, str(GATES), "loop-round", SPEC, "review:spec",
         "--max", "3"], env=ctx["env"], cwd=ctx["repo"],
        capture_output=True, text=True, timeout=60)
    assert loop.returncode == 0, out(loop)
    loops_before = store(ctx)["_loops"]
    set_spec(ctx, block(budget=0))
    assert run(ctx, "plan").returncode == 1
    res = run(ctx, "plan", "--repin")
    assert res.returncode == 0, out(res)
    assert store(ctx)["_intent"][SPEC]["rev"] == 2
    assert run(ctx, "plan").returncode == 0
    assert store(ctx)["_loops"] == loops_before


# 9
def test_finish_negative_net_decrease_passes(ctx):
    repo = pinned_repo(ctx, block("negative", 0, targets="src/old.py"))
    (repo / "src/old.py").unlink()
    write(repo, "src/new.py", lines(5, "new"))
    res = run(ctx, "finish")
    assert res.returncode == 0, out(res)


def test_finish_negative_but_net_positive_fails(ctx):
    repo = pinned_repo(ctx, block("negative", 0, targets="src/old.py"))
    (repo / "src/old.py").unlink()
    write(repo, "src/new.py", lines(30, "new"))
    res = run(ctx, "finish")
    assert res.returncode == 1
    text = out(res)
    assert "INTENT-FAIL" in text
    assert "prod_delta=negative" in text
    assert "net=+10" in text


def test_finish_delete_target_still_present_fails_despite_overall_negative(ctx):
    repo = pinned_repo(ctx, block("negative", 0, targets="src/old.py"))
    (repo / "src/keep.py").unlink()  # overall net -40, but old.py untouched
    res = run(ctx, "finish")
    assert res.returncode == 1
    assert "INTENT-FAIL" in out(res)


def test_finish_delete_target_directory_removed_passes(ctx):
    init_repo(ctx, block("negative", 0, targets="src/legacy/"))
    repo = ctx["repo"]
    write(repo, "src/legacy/a.py", lines(8, "a"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "legacy")
    ctx["env"]["FFS_BASE"] = git(repo, "rev-parse", "HEAD")
    assert run(ctx, "plan").returncode == 0
    (repo / "src/legacy/a.py").unlink()
    (repo / "src/legacy").rmdir()
    assert run(ctx, "finish").returncode == 0


# 10
def test_finish_counts_committed_staged_unstaged_untracked(ctx):
    repo = pinned_repo(ctx, block("positive", 5))
    write(repo, "src/c.py", lines(2, "c"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "committed")
    write(repo, "src/s.py", lines(1, "s"))
    git(repo, "add", "src/s.py")
    with (repo / "src/keep.py").open("a") as fh:
        fh.write("unstaged\n")
    write(repo, "src/u.py", lines(1, "u"))
    assert run(ctx, "finish").returncode == 0  # exactly +5
    write(repo, "src/u.py", lines(2, "u"))  # +6, only untracked grew
    res = run(ctx, "finish")
    assert res.returncode == 1
    assert "net=+6" in out(res)


def test_finish_ignores_test_and_doc_files(ctx):
    repo = pinned_repo(ctx, block("positive", 5))
    write(repo, "src/a.py", lines(5, "a"))
    write(repo, "tests/test_x.py", lines(100, "t"))
    write(repo, "src/a.test.ts", lines(100, "t"))
    write(repo, "docs/x.md", lines(100, "d"))
    res = run(ctx, "finish")
    assert res.returncode == 0, out(res)


def test_finish_counts_skill_markdown(ctx):
    repo = pinned_repo(ctx, block("positive", 5))
    write(repo, "skills/x/SKILL.md", lines(6, "s"))
    res = run(ctx, "finish")
    assert res.returncode == 1
    assert "net=+6" in out(res)


# 11
@pytest.mark.parametrize("delta,budget,add,remove,expected", [
    ("neutral", 10, 8, 0, 0),
    ("neutral", 10, 0, 8, 0),
    ("neutral", 10, 12, 0, 1),
    ("neutral", 10, 0, 12, 1),
    ("positive", 10, 10, 0, 0),
    ("positive", 10, 11, 0, 1),
])
def test_finish_budget_bounds(ctx, delta, budget, add, remove, expected):
    repo = pinned_repo(ctx, block(delta, budget))
    if add:
        write(repo, "src/new.py", lines(add, "n"))
    if remove:
        write(repo, "src/keep.py", lines(40 - remove, "keep"))
    res = run(ctx, "finish")
    assert res.returncode == expected, out(res)
    if expected:
        assert "INTENT-FAIL" in out(res)


# H3: --repin may only narrow, and only at the plan stage
def test_repin_refuses_loosening(ctx):
    pinned_repo(ctx, block("negative", 0))
    set_spec(ctx, block("positive", 9999))
    res = run(ctx, "plan", "--repin")
    assert res.returncode == 1
    assert "INTENT-FAIL" in out(res) and "narrow" in out(res)
    assert store(ctx)["_intent"][SPEC]["rev"] == 1


def test_repin_allows_narrowing(ctx):
    pinned_repo(ctx, block("neutral", 10))
    set_spec(ctx, block("negative", 5))
    res = run(ctx, "plan", "--repin")
    assert res.returncode == 0, out(res)
    assert store(ctx)["_intent"][SPEC]["rev"] == 2


def test_repin_at_finish_is_usage_error(ctx):
    pinned_repo(ctx, block("negative", 0))
    assert run(ctx, "finish", "--repin").returncode == 2


# H2: base resolution
def commit_all(repo, msg):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def test_master_repo_without_remote_or_ffs_base_does_not_crash(ctx):
    repo = ctx["repo"]
    git(repo, "init", "-q", "-b", "master")
    write(repo, "src/old.py", lines(5, "old"))
    write(repo, SPEC_REL, spec_text(block("positive", 100)))
    commit_all(repo, "base")
    ctx["env"].pop("FFS_BASE", None)
    git(repo, "checkout", "-q", "-b", "123-feat")
    assert run(ctx, "plan").returncode == 0
    res = run(ctx, "finish")
    assert "Traceback" not in out(res)
    assert res.returncode in (0, 1)
    assert "INTENT" in out(res)


def test_ffs_base_head_spoof_rejected(ctx):
    repo = ctx["repo"]
    git(repo, "init", "-q", "-b", "main")
    write(repo, "src/old.py", lines(5, "old"))
    write(repo, SPEC_REL, spec_text(block("neutral", 0)))
    commit_all(repo, "base")
    git(repo, "checkout", "-q", "-b", "123-feat")
    write(repo, "src/big.py", lines(500, "big"))
    commit_all(repo, "feature")
    assert run(ctx, "plan").returncode == 0
    ctx["env"]["FFS_BASE"] = "HEAD"
    res = run(ctx, "finish")
    assert res.returncode == 1
    assert "INTENT-FAIL" in out(res) and "FFS_BASE" in out(res)


def test_gsd_base_branch_cannot_move_the_finish_base(ctx):
    repo = ctx["repo"]
    git(repo, "init", "-q", "-b", "main")
    write(repo, "src/old.py", lines(5, "old"))
    write(repo, SPEC_REL, spec_text(block("negative", 0)))
    commit_all(repo, "base")
    git(repo, "checkout", "-q", "-b", "123-feat")
    write(repo, "src/big.py", lines(50, "big"))
    commit_all(repo, "feature")
    write(repo, "src/old.py", lines(4, "old"))
    ctx["env"].pop("FFS_BASE", None)
    ctx["env"]["GSD_BASE_BRANCH"] = "123-feat"
    res = run(ctx, "finish")
    assert res.returncode == 1
    assert "INTENT-FAIL" in out(res) and "net=+49" in out(res)


def test_candidate_hash_folds_in_base(ctx):
    sys.path.insert(0, str(GATES.parent))
    import gates as gates_mod
    repo = ctx["repo"]
    git(repo, "init", "-q", "-b", "123-feat")
    write(repo, "src/a.py", "a\n")
    first = commit_all(repo, "one")
    git(repo, "commit", "-q", "--allow-empty", "-m", "two")
    second = git(repo, "rev-parse", "HEAD")
    write(repo, "src/c.py", "c\n")
    old = os.environ.get("FFS_BASE")
    try:
        os.environ["FFS_BASE"] = first
        h1 = gates_mod.candidate_hash(repo)
        os.environ["FFS_BASE"] = second
        h2 = gates_mod.candidate_hash(repo)
    finally:
        os.environ.pop("FFS_BASE", None) if old is None else os.environ.update(FFS_BASE=old)
    assert h1 != h2
