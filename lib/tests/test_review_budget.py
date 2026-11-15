"""Durable review meter (F4'): token dedup, exhaustion contract, spec identity,
operator-only dispositions. Every test points GATES_STORE at a tmp_path file."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

GATES = Path(__file__).resolve().parents[1] / "gates.py"
SPEC = "spec-123"
LOOP = "review:spec"
BUDGET_PREFIX = (f"BUDGET-EXHAUSTED {LOOP} {SPEC}: stopped. Operator picks one: "
                 "merge-with-issues | descope | stop. Recommended:")
BANNED = ["grant", "renew", "reset", "--max", "another round"]


@pytest.fixture
def ctx(tmp_path):
    store = tmp_path / "gates.json"
    env = {k: v for k, v in os.environ.items()
           if k not in ("FFS_SPEC_ID", "GSD_RUN_ID", "GATES_STORE")}
    env["GATES_STORE"] = str(store)
    return {"store": store, "env": env, "cwd": tmp_path}


def gates(ctx, *args, env=None, cwd=None):
    return subprocess.run([sys.executable, str(GATES), *map(str, args)],
                          env=env or ctx["env"], cwd=cwd or ctx["cwd"],
                          capture_output=True, text=True, timeout=60)


def round_(ctx, token=None, site=None, maximum=2, loop=LOOP):
    args = ["loop-round", SPEC, loop, "--max", maximum, "--durable"]
    if token:
        args += ["--token", token]
    if site:
        args += ["--site", site]
    return gates(ctx, *args)


def used(ctx, loop=LOOP):
    data = json.loads(ctx["store"].read_text())
    return data["_loops"][SPEC][loop + "#cap"]["used"]


def test_token_skill_then_seam_spends_one_round(ctx):
    assert round_(ctx, "T1", "skill").returncode == 0
    assert round_(ctx, "T1", "seam").returncode == 0
    assert used(ctx) == 1


def test_tokens_stored_outside_cap_meta(ctx):
    assert round_(ctx, "T1", "skill").returncode == 0
    data = json.loads(ctx["store"].read_text())
    assert SPEC in data["_review_tokens"]
    assert set(data["_loops"][SPEC][LOOP + "#cap"]) == {"max", "used"}


def test_token_same_site_twice_spends_two(ctx):
    assert round_(ctx, "T1", "skill").returncode == 0
    assert round_(ctx, "T1", "skill").returncode == 0
    assert used(ctx) == 2


def test_token_continuation_only_once(ctx):
    assert round_(ctx, "T1", "skill", maximum=5).returncode == 0
    assert round_(ctx, "T1", "seam", maximum=5).returncode == 0
    assert round_(ctx, "T1", "seam", maximum=5).returncode == 0
    assert used(ctx) == 2


def test_sealed_max_unchanged_with_token(ctx):
    assert round_(ctx, "T1", "skill", maximum=2).returncode == 0
    later = round_(ctx, "T2", "skill", maximum=3)
    assert later.returncode != 0
    assert "sealed" in (later.stdout + later.stderr).lower()
    assert used(ctx) == 1


def _exhaust(ctx):
    assert round_(ctx, "A", "skill").returncode == 0
    assert round_(ctx, "B", "skill").returncode == 0
    return round_(ctx, "C", "skill")


def test_cap_message_has_no_renewal_words(ctx):
    res = _exhaust(ctx)
    assert res.returncode != 0
    out = res.stdout + res.stderr
    lines = out.splitlines()
    assert any(ln.startswith("LOOP-CAP:") for ln in lines)
    assert any(ln.startswith(BUDGET_PREFIX) for ln in lines)
    assert "/prompt-master" in out and "$prompt-master" in out
    lowered = out.lower()
    for word in BANNED:
        assert word not in lowered, word


def test_exhaustion_records_pending(ctx):
    _exhaust(ctx)
    entries = json.loads(ctx["store"].read_text())["_exhausted"][SPEC]
    assert len(entries) == 1
    assert entries[0]["loop"] == LOOP
    assert "BUDGET-EXHAUSTED" in json.dumps(entries[0])


def test_generic_loop_cap_text_drops_raise_reset(ctx):
    loop = "plangate:plan"
    assert gates(ctx, "loop-round", SPEC, loop, "--max", 1).returncode == 0
    res = gates(ctx, "loop-round", SPEC, loop, "--max", 1)
    assert res.returncode != 0
    out = res.stdout + res.stderr
    assert "LOOP-CAP:" in out
    assert "--max" not in out and "--reset" not in out


def _repo(tmp_path, branch="123-x", detach=False):
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*a):
        subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)

    run("init", "-q", "-b", branch)
    run("-c", "user.name=t", "-c", "user.email=t@invalid", "commit", "-q",
        "--allow-empty", "-m", "init")
    if detach:
        run("checkout", "-q", "--detach")
    return repo


def test_spec_id_branch(ctx, tmp_path):
    repo = _repo(tmp_path)
    res = gates(ctx, "spec-id", cwd=repo)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == SPEC


def test_spec_id_detached_uses_env(ctx, tmp_path):
    repo = _repo(tmp_path, detach=True)
    env = {**ctx["env"], "FFS_SPEC_ID": SPEC, "GSD_RUN_ID": "drive-new-p4"}
    res = gates(ctx, "spec-id", env=env, cwd=repo)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == SPEC


def test_spec_id_mismatch_fails(ctx, tmp_path):
    repo = _repo(tmp_path)
    env = {**ctx["env"], "FFS_SPEC_ID": "spec-456"}
    res = gates(ctx, "spec-id", env=env, cwd=repo)
    assert res.returncode != 0
    assert "SPEC-ID:mismatch" in res.stderr


def test_spec_id_unbound_falls_back(ctx, tmp_path):
    repo = _repo(tmp_path, detach=True)
    env = {**ctx["env"], "GSD_RUN_ID": "run-9"}
    res = gates(ctx, "spec-id", env=env, cwd=repo)
    assert res.stdout.strip() == "run-9"
    assert "SPEC-ID:unbound" in res.stderr


def add(ctx, file, issue, *flags):
    res = gates(ctx, "findings-queue", "add", file, issue, *flags)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)["sig"]


def resolve(ctx, sig, disposition, reason="r"):
    return gates(ctx, "findings-queue", "resolve", sig,
                 "--disposition", disposition, "--reason", reason)


def stored(ctx):
    return json.loads(ctx["store"].read_text())["findings"]


@pytest.mark.parametrize("disposition", ["issue", "descope"])
def test_dispositions_issue_descope_valid(ctx, disposition):
    sig = add(ctx, "a.py", "minor thing", "--severity", "MEDIUM")
    res = resolve(ctx, sig, disposition)
    assert res.returncode == 0, res.stderr
    assert stored(ctx)[0]["disposition"] == disposition


def test_issue_descope_refused_for_security_high(ctx):
    sig = add(ctx, "a.py", "auth bypass", "--severity", "HIGH", "--security")
    before = ctx["store"].read_text()
    for disposition in ("issue", "descope"):
        assert resolve(ctx, sig, disposition).returncode != 0
    assert ctx["store"].read_text() == before
    assert resolve(ctx, sig, "fix").returncode == 0


def test_findings_add_records_spec(ctx):
    sig = add(ctx, "a.py", "bad input", "--severity", "HIGH", "--spec", SPEC)
    assert stored(ctx)[0]["spec"] == SPEC
    again = add(ctx, "a.py", "bad input", "--severity", "HIGH", "--spec", SPEC,
                "--security")
    assert again == sig
    assert stored(ctx)[0]["security"] is True
    add(ctx, "a.py", "bad input", "--severity", "HIGH", "--spec", SPEC)
    assert stored(ctx)[0]["security"] is True


def _git(repo, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@invalid", *a],
                   cwd=repo, check=True, capture_output=True)


def _write(repo, rel, text):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _auto_repo(ctx, tmp_path):
    repo = _repo(tmp_path)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()
    env = {**ctx["env"], "FFS_BASE": base}
    _write(repo, "src/app.py", "print('one')\n")
    return repo, env


def auto_round(ctx, repo, env, site):
    return gates(ctx, "loop-round", SPEC, LOOP, "--max", 5, "--durable",
                 "--site", site, env=env, cwd=repo)


def test_auto_token_ignores_docs_commit_between_sites(ctx, tmp_path):
    repo, env = _auto_repo(ctx, tmp_path)
    assert auto_round(ctx, repo, env, "skill").returncode == 0
    _write(repo, "docs/x.md", "notes\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "docs")
    assert auto_round(ctx, repo, env, "seam").returncode == 0
    assert used(ctx) == 1


def test_auto_token_changes_with_prod_file(ctx, tmp_path):
    repo, env = _auto_repo(ctx, tmp_path)
    assert auto_round(ctx, repo, env, "skill").returncode == 0
    _write(repo, "src/app.py", "print('two')\n")
    assert auto_round(ctx, repo, env, "seam").returncode == 0
    assert used(ctx) == 2


def test_auto_token_skill_md_counts_as_production(ctx, tmp_path):
    repo, env = _auto_repo(ctx, tmp_path)
    assert auto_round(ctx, repo, env, "skill").returncode == 0
    _write(repo, "skills/x/SKILL.md", "# skill\n")
    assert auto_round(ctx, repo, env, "seam").returncode == 0
    assert used(ctx) == 2


def test_free_continuation_refused_after_exhaustion(ctx):
    assert round_(ctx, "A", "skill").returncode == 0
    assert round_(ctx, "B", "skill").returncode == 0
    assert round_(ctx, "C", "skill").returncode != 0  # exhausted
    late = round_(ctx, "B", "seam")  # B's other site: no free pass once exhausted
    assert late.returncode == 1
    assert "BUDGET-EXHAUSTED" in late.stdout + late.stderr
