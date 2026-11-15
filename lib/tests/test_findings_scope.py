"""F2: a finding whose smallest fix needs a new requirement cannot be closed as
`fix` without an operator scope decision. Security findings keep `fix`."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1]
GATES = LIB / "gates.py"
sys.path.insert(0, str(LIB))
import gates as gates_mod  # noqa: E402

NEEDS = "--needs-requirement"


@pytest.fixture
def ctx(tmp_path):
    store = tmp_path / "gates.json"
    env = {k: v for k, v in os.environ.items()
           if k not in ("FFS_SPEC_ID", "GSD_RUN_ID", "GATES_STORE")}
    env["GATES_STORE"] = str(store)
    return {"store": store, "env": env, "cwd": tmp_path}


def cli(ctx, *args):
    return subprocess.run([sys.executable, str(GATES), "findings-queue", *map(str, args)],
                          env=ctx["env"], cwd=ctx["cwd"], capture_output=True,
                          text=True, timeout=60)


def add(ctx, *flags, issue="needs a new limit"):
    res = cli(ctx, "add", "a.py", issue, *flags)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)["sig"]


def resolve(ctx, sig, disposition="fix"):
    return cli(ctx, "resolve", sig, "--disposition", disposition, "--reason", "r")


def stored(ctx):
    return json.loads(ctx["store"].read_text())["findings"]


def test_add_flag_stores_true(ctx):
    add(ctx, NEEDS)
    assert stored(ctx)[0]["needs_requirement"] is True


def test_add_without_flag_absent_or_false(ctx):
    add(ctx)
    assert not stored(ctx)[0].get("needs_requirement")


def test_flag_position_independent(ctx):
    add(ctx, "--severity", "HIGH", NEEDS, "--spec", "s1")
    rec = stored(ctx)[0]
    assert rec["needs_requirement"] is True
    assert rec["severity"] == "HIGH" and rec["spec"] == "s1"


def test_sticky_on_dedup_readd_without_flag(ctx):
    sig = add(ctx, NEEDS)
    assert add(ctx) == sig
    assert stored(ctx)[0]["needs_requirement"] is True


def test_flag_set_on_later_readd(ctx):
    sig = add(ctx)
    assert add(ctx, NEEDS) == sig
    assert stored(ctx)[0]["needs_requirement"] is True


def test_cli_fix_refused_for_needs_requirement(ctx):
    sig = add(ctx, NEEDS)
    before = ctx["store"].read_text()
    res = resolve(ctx, sig, "fix")
    assert res.returncode == 2
    assert "needs-requirement" in res.stderr
    assert ctx["store"].read_text() == before
    assert not stored(ctx)[0].get("resolved")


def test_api_fix_raises_value_error(ctx):
    sig, _, _ = gates_mod.findings_add(ctx["store"], "b.py", "api case",
                                       needs_requirement=True)
    with pytest.raises(ValueError, match="needs-requirement"):
        gates_mod.findings_resolve(ctx["store"], sig, disposition="fix", reason="r")
    assert not stored(ctx)[0].get("resolved")


def test_security_keeps_fix(ctx):
    sig = add(ctx, "--severity", "HIGH", "--security", NEEDS)
    res = resolve(ctx, sig, "fix")
    assert res.returncode == 0, res.stderr
    assert stored(ctx)[0]["disposition"] == "fix"


@pytest.mark.parametrize("disposition", ["issue", "descope", "refute", "waive"])
def test_other_dispositions_allowed(ctx, disposition):
    sig = add(ctx, NEEDS)
    res = resolve(ctx, sig, disposition)
    assert res.returncode == 0, res.stderr
    assert stored(ctx)[0]["disposition"] == disposition


def test_retag_in_spec_clears_flag_then_fix_allowed(ctx):
    sig = add(ctx, NEEDS)
    res = cli(ctx, "retag", sig, "--in-spec")
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout)["sig"] == sig
    assert not stored(ctx)[0].get("needs_requirement")
    assert resolve(ctx, sig, "fix").returncode == 0


def test_retag_unknown_sig_exit_1(ctx):
    add(ctx, NEEDS)
    assert cli(ctx, "retag", "deadbeef", "--in-spec").returncode == 1


def test_retag_missing_in_spec_usage_exit_2(ctx):
    sig = add(ctx, NEEDS)
    res = cli(ctx, "retag", sig)
    assert res.returncode == 2
    assert "usage: findings-queue retag" in res.stderr
    assert stored(ctx)[0]["needs_requirement"] is True


def test_rereport_of_dispositioned_finding_does_not_reopen(ctx):
    sig = add(ctx, NEEDS)
    assert resolve(ctx, sig, "issue").returncode == 0
    assert add(ctx, NEEDS) == sig
    rec = stored(ctx)[0]
    assert rec["resolved"] is True and rec["disposition"] == "issue"


def test_resolved_descope_reopens_on_security_high_rereport(ctx):
    sig = add(ctx, "--severity", "MEDIUM")
    assert resolve(ctx, sig, "descope").returncode == 0
    plain = cli(ctx, "add", "a.py", "needs a new limit", "--severity", "MEDIUM")
    assert json.loads(plain.stdout).get("reopened") is not True
    assert stored(ctx)[0]["resolved"] is True
    res = cli(ctx, "add", "a.py", "needs a new limit", "--security", "--severity", "CRITICAL")
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout).get("reopened") is True
    assert stored(ctx)[0]["resolved"] is False
