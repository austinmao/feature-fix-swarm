"""AC-002 / AC-005 acceptance surface for the promotion protocol (spec-295).

The unit coverage lives in lib/tests/test_gates.py; this file is the acceptance
test in the location the spec-295 DONE-WHEN proofs target
(`cd packages/feature-fix-swarm && python3 -m pytest tests/ -k promote -q`), and
it exercises the REAL shipped gates.py enforcement — not a mock. `-k promote`
selects this whole module (its name carries "promote").

  AC-002: a prod-targeting action with a grant but NO promote evidence is REFUSED.
  AC-005: a non-prod action routes through the same code with ZERO side effects
          (the store is byte-identical before/after) — strictly additive.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_LIB = Path(__file__).resolve().parents[1] / "lib"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _LIB / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


gates = _load("gates")

# A valid artifact identity: name@sha256:<64 hex> (mutable tags are rejected).
_GOOD_ARTIFACT = "myapp@sha256:" + "f" * 64


def test_prod_deploy_without_promote_evidence_is_refused(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    gates.grant_actions(store, "run-1", ["deploy:prod-cp"])
    # Grant alone proves an operator approved the action — it does NOT prove the
    # artifact passed staging. No promote record => REFUSED (AC-002).
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp", _GOOD_ARTIFACT) is False
    pend = gates.list_pending(store, "run-1")
    assert any("NO-PROMOTE-EVIDENCE" in p["reason"] for p in pend)


def test_prod_deploy_with_fresh_matching_promote_is_allowed(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    assert gates.run_gate(
        store, "stg-web", ["python3", "-c", "raise SystemExit(0)"],
        artifact=_GOOD_ARTIFACT,
    ) == 0
    gates.grant_actions(store, "run-1", ["deploy:prod-cp"])
    gates.record_promotion(
        store, "run-1", from_env="staging", to_env="prod",
        surface="cp", artifact=_GOOD_ARTIFACT, evidence_ids=["stg-web"],
    )
    # Grant + fresh staging->prod promote for this exact artifact => allowed.
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp", _GOOD_ARTIFACT) is True


def test_prod_deploy_with_stale_dev_source_promote_is_refused(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    assert gates.run_gate(
        store, "stg-web", ["python3", "-c", "raise SystemExit(0)"],
        artifact=_GOOD_ARTIFACT,
    ) == 0
    gates.grant_actions(store, "run-1", ["deploy:prod-cp"])
    # A dev->prod promote never satisfies the staging precondition, even with a
    # matching artifact — the from_env guard rejects it.
    gates.record_promotion(
        store, "run-1", from_env="dev", to_env="prod",
        surface="cp", artifact=_GOOD_ARTIFACT, evidence_ids=["stg-web"],
    )
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp", _GOOD_ARTIFACT) is False


def test_non_prod_action_leaves_promote_store_byte_identical(tmp_path) -> None:
    # AC-005 backward-compat: a non-prod action carries NO _promotions key and
    # the precondition never fires — byte-identical store, strictly additive.
    store = tmp_path / "evidence.json"
    gates.grant_actions(store, "run-1", ["push:origin/main"])
    before = store.read_bytes()
    assert gates.check_grant_prod(store, "run-1", "push:origin/other", None) is False
    assert store.read_bytes() == before


# ── WR-03 / Rule 12a: the hotfix bypass is posture-dependent ──────────────
# The document promises the emergency bypass sits downstream of the
# stricter-only posture resolver; these tests make that property real.
# The check reads the resolver's exported AUTONOMY_POSTURE (never the raw
# FFS_AUTONOMY_POSTURE input, which only autonomy-posture.sh may read).

import json


def test_hotfix_bypass_permitted_under_zero_posture(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AUTONOMY_POSTURE", "zero")
    store = tmp_path / "evidence.json"
    gates.grant_actions(store, "run-1", ["hotfix:prod-cp"], reason="incident 42")
    assert gates.check_grant_prod(store, "run-1", "hotfix:prod-cp", None) is True
    data = json.loads(store.read_text())
    rec = data["_autonomy"]["run-1"]["hotfix_bypasses"][-1]
    assert rec.get("posture") == "zero", \
        f"bypass record does not durably carry the effective posture: {rec}"


def test_hotfix_bypass_refused_under_floor_posture(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AUTONOMY_POSTURE", "floor")
    store = tmp_path / "evidence.json"
    gates.grant_actions(store, "run-1", ["hotfix:prod-cp"], reason="incident 42")
    assert gates.check_grant_prod(store, "run-1", "hotfix:prod-cp", None) is False, \
        "floor posture must forbid the hotfix:prod-* emergency bypass"
    pend = gates.list_pending(store, "run-1")
    assert any("floor" in p["reason"].lower() for p in pend), pend
    data = json.loads(store.read_text())
    assert not data["_autonomy"]["run-1"].get("hotfix_bypasses"), \
        "a refused bypass must never be recorded as used"


def test_hotfix_bypass_fails_closed_on_unvalidated_posture(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AUTONOMY_POSTURE", "bananas")
    store = tmp_path / "evidence.json"
    gates.grant_actions(store, "run-1", ["hotfix:prod-cp"], reason="incident 42")
    assert gates.check_grant_prod(store, "run-1", "hotfix:prod-cp", None) is False, \
        "an unvalidated effective posture must never loosen the bypass"


def test_hotfix_bypass_unset_posture_keeps_the_zero_default(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AUTONOMY_POSTURE", raising=False)
    store = tmp_path / "evidence.json"
    gates.grant_actions(store, "run-1", ["hotfix:prod-cp"], reason="incident 42")
    # no resolver ran: the committed default posture is zero, so the
    # grant+reason contract still authorizes the escape.
    assert gates.check_grant_prod(store, "run-1", "hotfix:prod-cp", None) is True
