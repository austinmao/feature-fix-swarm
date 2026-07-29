"""Tests for lib/runtime_proof.py — browser-proof bundle verification.

TDD RED-first (v3.20.0 Stream A). Completion authority for browser QA lives in
evidence, not agent self-report: a phase's browser gate passes only when a
proof.json bundle survives these checks. Each check defeats a named
anti-pattern (curl-200-as-proof, screenshot-of-wrong-page, soft-404,
post-hydration console death, stale/absent artifacts, self-report driver).
Stdlib-only, like gates.py.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runtime_proof as rp

MODULE = Path(__file__).resolve().parents[1] / "runtime_proof.py"


# ---------------------------------------------------------------- fixtures

def make_screenshot(tmp_path: Path, name: str = "shot.png", age_min: int = 0) -> Path:
    p = tmp_path / name
    p.write_bytes(b"\x89PNG fake image bytes")
    if age_min:
        old = time.time() - age_min * 60
        os.utime(p, (old, old))
    return p


def good_scenario(tmp_path: Path, **over) -> dict:
    shot = make_screenshot(tmp_path, over.pop("shot_name", "shot.png"))
    sc = {
        "id": "US1-S1",
        "kind": "functional",
        "status": "pass",
        "url": "/dashboard",
        "url_final": "http://localhost:3000/dashboard",
        "expect_url": "/dashboard",
        "http_status": 200,
        "content_assert": "h1: Dashboard",
        "dom_excerpt": "<h1>Dashboard</h1><p>Welcome back</p>",
        "console_errors": [],
        "interactions": 2,
        "screenshot": str(shot),
    }
    sc.update(over)
    return sc


def good_proof(tmp_path: Path, scenarios=None, **over) -> Path:
    # v3.20 hardening: driver=playwright requires a fresh run artifact
    art = tmp_path / "pw-results.json"
    if not art.exists():
        art.write_text('{"suites": []}')
    proof = {
        "version": 1,
        "driver": "playwright",
        "base_url": "http://localhost:3000",
        "playwright_artifact": str(art),
        "scenarios": scenarios if scenarios is not None
        else [good_scenario(tmp_path)],
    }
    proof.update(over)
    if proof.get("playwright_artifact") is None:
        del proof["playwright_artifact"]  # explicit None = omit (for tests)
    p = tmp_path / "proof.json"
    p.write_text(json.dumps(proof))
    return p


def verify(path: Path, **kw):
    return rp.verify_proof(str(path), **kw)


# ---------------------------------------------------------------- happy path

def test_good_bundle_passes(tmp_path):
    ok, findings = verify(good_proof(tmp_path))
    assert ok, findings
    assert findings == []


def test_visual_scenario_no_interactions_required(tmp_path):
    sc = good_scenario(tmp_path, kind="visual", interactions=0)
    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]))
    assert ok, findings


def test_static_functional_scenario_exempt_from_interactions(tmp_path):
    sc = good_scenario(tmp_path, interactions=0, static=True)
    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]))
    assert ok, findings


# ---------------------------------------------------------------- schema

def test_missing_file_fails(tmp_path):
    ok, findings = verify(tmp_path / "nope.json")
    assert not ok
    assert any("not found" in f.lower() or "missing" in f.lower() for f in findings)


def test_invalid_json_fails(tmp_path):
    p = tmp_path / "proof.json"
    p.write_text("{not json")
    ok, _findings = verify(p)
    assert not ok


def test_empty_scenarios_fails(tmp_path):
    ok, findings = verify(good_proof(tmp_path, scenarios=[]))
    assert not ok
    assert any("empty" in f.lower() or "no scenarios" in f.lower() for f in findings)


def test_unknown_driver_fails(tmp_path):
    ok, _findings = verify(good_proof(tmp_path, driver="curl"))
    assert not ok


def test_unfilled_skeleton_rejected(tmp_path):
    sc = good_scenario(tmp_path, status="UNFILLED")
    ok, _findings = verify(good_proof(tmp_path, scenarios=[sc]))
    assert not ok


@pytest.mark.parametrize("scenario", [None, "not-an-object", 7, True, []])
def test_scenario_must_be_an_object_without_crashing(tmp_path, scenario):
    ok, findings = verify(good_proof(tmp_path, scenarios=[scenario]))

    assert not ok
    assert any("scenario[0]" in finding and "object" in finding
               for finding in findings)


@pytest.mark.parametrize("scenarios", [{"id": "US1-S1"}, "not-a-list", 7, True])
def test_scenarios_must_be_a_list_without_crashing(tmp_path, scenarios):
    ok, findings = verify(good_proof(tmp_path, scenarios=scenarios))

    assert not ok
    assert any("scenarios" in finding and "list" in finding for finding in findings)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", ""),
        ("id", 7),
        ("content_assert", 7),
        ("content_assert", ""),
        ("dom_excerpt", 7),
        ("dom_excerpt", ""),
        ("url_final", 7),
        ("url_final", ""),
        ("screenshot", 7),
        ("screenshot", ""),
    ],
)
def test_required_scenario_strings_are_nonempty_strings(tmp_path, field, value):
    sc = good_scenario(tmp_path, **{field: value})

    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]))

    assert not ok
    assert any(field in finding and "non-empty string" in finding
               for finding in findings)


@pytest.mark.parametrize("console_errors", ["", "TypeError", {}, 7, True])
def test_console_errors_must_be_a_list_without_crashing(tmp_path, console_errors):
    sc = good_scenario(tmp_path, console_errors=console_errors)

    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]))

    assert not ok
    assert any("console_errors" in finding and "list" in finding
               for finding in findings)


def test_console_error_entries_must_be_strings(tmp_path):
    sc = good_scenario(tmp_path, console_errors=[{"message": "boom"}])

    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]))

    assert not ok
    assert any("console_errors" in finding and "strings" in finding
               for finding in findings)


@pytest.mark.parametrize("interactions", [True, False, 1.0, "1", 0, -1])
def test_functional_interactions_must_be_positive_real_integer(tmp_path, interactions):
    sc = good_scenario(tmp_path, interactions=interactions)

    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]))

    assert not ok
    assert any("interactions" in finding for finding in findings)


@pytest.mark.parametrize("static", ["true", 1, 0, None, [], {}])
def test_static_must_be_boolean_when_present(tmp_path, static):
    sc = good_scenario(tmp_path, static=static)

    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]))

    assert not ok
    assert any("static" in finding and "boolean" in finding for finding in findings)


@pytest.mark.parametrize("http_status", [True, False, 200.0, "200", None, [], {}])
def test_http_status_must_be_real_integer_when_present(tmp_path, http_status):
    sc = good_scenario(tmp_path, http_status=http_status)

    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]))

    assert not ok
    assert any("http_status" in finding and "integer" in finding
               for finding in findings)


def test_malformed_optional_expect_url_yields_finding_instead_of_crashing(tmp_path):
    sc = good_scenario(tmp_path, expect_url={"path": "/dashboard"})

    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]))

    assert not ok
    assert any("expect_url" in finding and "string" in finding
               for finding in findings)


# ---------------------------------------------------------------- verdicts

def test_failed_scenario_fails_bundle(tmp_path):
    scs = [good_scenario(tmp_path), good_scenario(tmp_path, id="US1-S2", status="fail",
                                                  shot_name="s2.png")]
    ok, findings = verify(good_proof(tmp_path, scenarios=scs))
    assert not ok
    assert any("US1-S2" in f for f in findings)


# ------------------------------------------------- anti-pattern: curl-200

def test_missing_content_assert_fails(tmp_path):
    sc = good_scenario(tmp_path)
    del sc["content_assert"]
    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]))
    assert not ok
    assert any("content_assert" in f for f in findings)


def test_empty_content_assert_fails(tmp_path):
    sc = good_scenario(tmp_path, content_assert="")
    ok, _ = verify(good_proof(tmp_path, scenarios=[sc]))
    assert not ok


def test_missing_dom_excerpt_fails(tmp_path):
    sc = good_scenario(tmp_path)
    del sc["dom_excerpt"]
    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]))
    assert not ok
    assert any("dom_excerpt" in f for f in findings)


# ------------------------------------------------- anti-pattern: soft-404

@pytest.mark.parametrize("marker", [
    "This page could not be found",
    "404 | Page Not Found",
    "Application error: a client-side exception has occurred",
    "Internal Server Error",
    "Something went wrong",
])
def test_soft_404_marker_in_dom_fails_even_with_200(tmp_path, marker):
    sc = good_scenario(tmp_path, dom_excerpt=f"<div>{marker}</div>",
                       http_status=200, status="pass")
    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]))
    assert not ok
    assert any("soft-404" in f.lower() or "marker" in f.lower() for f in findings)


def test_benign_404_like_content_passes(tmp_path):
    # "404" as a bare number in normal copy must not false-positive
    sc = good_scenario(tmp_path, dom_excerpt="<p>Room 404 is booked. Price $404</p>")
    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]))
    assert ok, findings


def test_http_status_4xx_fails(tmp_path):
    sc = good_scenario(tmp_path, http_status=404)
    ok, _ = verify(good_proof(tmp_path, scenarios=[sc]))
    assert not ok


def test_http_status_5xx_fails(tmp_path):
    sc = good_scenario(tmp_path, http_status=500)
    ok, _ = verify(good_proof(tmp_path, scenarios=[sc]))
    assert not ok


# --------------------------------------- anti-pattern: wrong-page screenshot

def test_url_final_mismatch_fails(tmp_path):
    sc = good_scenario(tmp_path, url_final="http://localhost:3000/login")
    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]))
    assert not ok
    assert any("url" in f.lower() for f in findings)


def test_missing_url_final_fails(tmp_path):
    sc = good_scenario(tmp_path)
    del sc["url_final"]
    ok, _findings = verify(good_proof(tmp_path, scenarios=[sc]))
    assert not ok


def test_no_expect_url_skips_match_check(tmp_path):
    sc = good_scenario(tmp_path, url_final="http://localhost:3000/anywhere")
    del sc["expect_url"]
    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]))
    assert ok, findings


# ---------------------------------------- anti-pattern: hydration console death

def test_console_errors_fail(tmp_path):
    sc = good_scenario(tmp_path, console_errors=["TypeError: x is undefined"])
    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]))
    assert not ok
    assert any("console" in f.lower() for f in findings)


def test_allow_console_regex_filters(tmp_path):
    sc = good_scenario(
        tmp_path,
        console_errors=["Warning: third-party analytics blocked by client"])
    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]),
                          allow_console=r"third-party analytics")
    assert ok, findings


def test_allow_console_regex_does_not_swallow_real_errors(tmp_path):
    sc = good_scenario(tmp_path, console_errors=[
        "Warning: third-party analytics blocked by client",
        "TypeError: cannot read properties of null",
    ])
    ok, _ = verify(good_proof(tmp_path, scenarios=[sc]),
                   allow_console=r"third-party analytics")
    assert not ok


def test_missing_console_errors_field_fails(tmp_path):
    # absence of the field means "nobody looked" — not the same as zero errors
    sc = good_scenario(tmp_path)
    del sc["console_errors"]
    ok, _findings = verify(good_proof(tmp_path, scenarios=[sc]))
    assert not ok


# ---------------------------------------------- interaction-proves-alive

def test_zero_interactions_functional_fails(tmp_path):
    sc = good_scenario(tmp_path, interactions=0)
    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]))
    assert not ok
    assert any("interaction" in f.lower() for f in findings)


# ---------------------------------------------------------------- artifacts

def test_missing_screenshot_fails(tmp_path):
    sc = good_scenario(tmp_path, screenshot=str(tmp_path / "ghost.png"))
    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]))
    assert not ok
    assert any("screenshot" in f.lower() for f in findings)


def test_empty_screenshot_fails(tmp_path):
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    sc = good_scenario(tmp_path, screenshot=str(empty))
    ok, _ = verify(good_proof(tmp_path, scenarios=[sc]))
    assert not ok


def test_stale_screenshot_fails(tmp_path):
    shot = make_screenshot(tmp_path, "old.png", age_min=600)
    sc = good_scenario(tmp_path, screenshot=str(shot))
    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]), max_age_min=240)
    assert not ok
    assert any("stale" in f.lower() or "age" in f.lower() for f in findings)


# ---------------------------------------------------------------- driver tiers

def test_agent_driver_ok_by_default(tmp_path):
    ok, findings = verify(good_proof(tmp_path, driver="agent"))
    assert ok, findings


def test_strict_rejects_agent_driver(tmp_path):
    ok, findings = verify(good_proof(tmp_path, driver="agent"), strict=True)
    assert not ok
    assert any("strict" in f.lower() or "agent" in f.lower() for f in findings)


def test_strict_accepts_canonical_canary_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    session_id = "runtime-proof-session"
    session = tmp_path / ".canary" / "sessions" / session_id
    session.mkdir(parents=True)
    (session / "results.json").write_text(json.dumps(
        {"steps": [{"name": "US1-S1", "status": "pass"}]}))
    make_screenshot(session, "scenario.png")
    scenario = good_scenario(tmp_path, screenshot="scenario.png")
    p = good_proof(
        tmp_path,
        driver="canary",
        canary_session=session_id,
        scenarios=[scenario],
    )
    ok, findings = verify(p, strict=True)
    assert ok, findings


def test_strict_canary_rejects_arbitrary_absolute_session_path(tmp_path):
    session = tmp_path / "attacker-controlled-session"
    session.mkdir()
    (session / "results.json").write_text(json.dumps(
        {"steps": [{"name": "US1-S1", "status": "pass"}]}))
    p = good_proof(tmp_path, driver="canary", canary_session=str(session))
    ok, findings = verify(p, strict=True)
    assert not ok
    assert any("session id" in finding.lower() for finding in findings)


def test_canary_rejects_absolute_screenshot_even_inside_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    session_id = "absolute-screenshot-session"
    session = tmp_path / ".canary" / "sessions" / session_id
    session.mkdir(parents=True)
    (session / "results.json").write_text(json.dumps(
        {"steps": [{"name": "US1-S1", "status": "pass"}]}))
    shot = make_screenshot(session, "scenario.png")
    scenario = good_scenario(tmp_path, screenshot=str(shot))
    p = good_proof(
        tmp_path,
        driver="canary",
        canary_session=session_id,
        scenarios=[scenario],
    )
    ok, findings = verify(p, strict=True)
    assert not ok
    assert any("relative" in finding.lower() for finding in findings)


def test_canary_session_id_resolves_relative_artifacts_without_absolute_proof_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    session_id = "spec299-fixture-session"
    session = tmp_path / ".canary" / "sessions" / session_id
    session.mkdir(parents=True)
    (session / "results.json").write_text(json.dumps(
        {"steps": [{"name": "US1-S1", "status": "pass"}]}))
    shot = make_screenshot(session, "generated.png")
    sc = good_scenario(tmp_path, screenshot=shot.name)
    p = good_proof(tmp_path, driver="canary", canary_session=session_id, scenarios=[sc])
    ok, findings = verify(p, strict=True)
    assert ok, findings


def test_canary_relative_screenshot_rejects_symlink_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    session_id = "spec299-symlink-session"
    session = tmp_path / ".canary" / "sessions" / session_id
    session.mkdir(parents=True)
    (session / "results.json").write_text(json.dumps(
        {"steps": [{"name": "US1-S1", "status": "pass"}]}))
    outside = make_screenshot(tmp_path, "outside.png")
    (session / "escaped.png").symlink_to(outside)
    sc = good_scenario(tmp_path, screenshot="escaped.png")
    p = good_proof(tmp_path, driver="canary", canary_session=session_id, scenarios=[sc])
    ok, findings = verify(p, strict=True)
    assert not ok
    assert any("symlink" in finding.lower() or "escapes" in finding.lower() for finding in findings)


def test_canary_session_id_rejects_symlinked_session_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    session_id = "spec299-session-root-link"
    sessions = tmp_path / ".canary" / "sessions"
    sessions.mkdir(parents=True)
    outside = tmp_path / "outside-session"
    outside.mkdir()
    (outside / "results.json").write_text(json.dumps(
        {"steps": [{"name": "US1-S1", "status": "pass"}]}))
    make_screenshot(outside, "shot.png")
    (sessions / session_id).symlink_to(outside, target_is_directory=True)
    sc = good_scenario(tmp_path, screenshot="shot.png")
    p = good_proof(tmp_path, driver="canary", canary_session=session_id, scenarios=[sc])
    ok, findings = verify(p, strict=True)
    assert not ok
    assert any("session" in finding.lower() and "symlink" in finding.lower() for finding in findings)


def test_strict_canary_rejects_symlinked_results_json(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    session_id = "spec299-results-link"
    session = tmp_path / ".canary" / "sessions" / session_id
    session.mkdir(parents=True)
    outside = tmp_path / "outside-results.json"
    outside.write_text(json.dumps(
        {"steps": [{"name": "US1-S1", "status": "pass"}]}))
    (session / "results.json").symlink_to(outside)
    make_screenshot(session, "shot.png")
    scenario = good_scenario(tmp_path, screenshot="shot.png")
    p = good_proof(
        tmp_path,
        driver="canary",
        canary_session=session_id,
        scenarios=[scenario],
    )

    ok, findings = verify(p, strict=True)

    assert not ok
    assert any("results.json" in finding and "symlink" in finding.lower()
               for finding in findings)


@pytest.mark.parametrize("symlinked_boundary", [".canary", "sessions"])
def test_canary_session_id_rejects_symlinked_canonical_root_boundary(
    tmp_path, monkeypatch, symlinked_boundary,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    outside = tmp_path / "outside-root"
    outside.mkdir()
    if symlinked_boundary == ".canary":
        (tmp_path / ".canary").symlink_to(outside, target_is_directory=True)
    else:
        canary_root = tmp_path / ".canary"
        canary_root.mkdir()
        (canary_root / "sessions").symlink_to(outside, target_is_directory=True)
    session_id = "spec299-boundary-link"
    session = outside / session_id
    session.mkdir()
    (session / "results.json").write_text(json.dumps(
        {"steps": [{"name": "US1-S1", "status": "pass"}]}))
    make_screenshot(session, "shot.png")
    scenario = good_scenario(tmp_path, screenshot="shot.png")
    p = good_proof(
        tmp_path,
        driver="canary",
        canary_session=session_id,
        scenarios=[scenario],
    )

    ok, findings = verify(p, strict=True)

    assert not ok
    assert any("boundary" in finding.lower() and "symlink" in finding.lower() for finding in findings)


# ---------------------------------------------------------------- canary cross-check

def test_canary_driver_requires_session_results(tmp_path):
    p = good_proof(tmp_path, driver="canary")  # no canary_session
    ok, findings = verify(p)
    assert not ok
    assert any("canary" in f.lower() for f in findings)


def test_canary_failed_step_fails_bundle(tmp_path):
    session = tmp_path / "session"
    session.mkdir()
    (session / "results.json").write_text(json.dumps(
        {"steps": [{"name": "US1-S1", "status": "pass"},
                   {"name": "US1-S2", "status": "fail"}]}))
    p = good_proof(tmp_path, driver="canary", canary_session=str(session))
    ok, findings = verify(p)
    assert not ok
    assert any("US1-S2" in f for f in findings)


def test_canary_results_must_cover_each_proof_scenario_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    session_id = "scenario-binding-session"
    session = tmp_path / ".canary" / "sessions" / session_id
    session.mkdir(parents=True)
    (session / "results.json").write_text(json.dumps(
        {"steps": [{"name": "UNRELATED-S1", "status": "pass"}]}))
    make_screenshot(session, "scenario.png")
    scenario = good_scenario(tmp_path, screenshot="scenario.png")
    p = good_proof(
        tmp_path,
        driver="canary",
        canary_session=session_id,
        scenarios=[scenario],
    )
    ok, findings = verify(p, strict=True)
    assert not ok
    assert any("US1-S1" in finding and "step" in finding.lower() for finding in findings)


def test_canary_results_reject_unbound_extra_step(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    session_id = "extra-step-session"
    session = tmp_path / ".canary" / "sessions" / session_id
    session.mkdir(parents=True)
    (session / "results.json").write_text(json.dumps({"steps": [
        {"name": "US1-S1-recorded", "status": "pass"},
        {"name": "US9-S9-unbound", "status": "pass"},
    ]}))
    make_screenshot(session, "scenario.png")
    scenario = good_scenario(tmp_path, screenshot="scenario.png")
    p = good_proof(
        tmp_path,
        driver="canary",
        canary_session=session_id,
        scenarios=[scenario],
    )
    ok, findings = verify(p, strict=True)
    assert not ok
    assert any("US9-S9-unbound" in finding and "unbound" in finding.lower() for finding in findings)


def test_canary_results_reject_duplicate_proof_scenario_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    session_id = "duplicate-scenario-session"
    session = tmp_path / ".canary" / "sessions" / session_id
    session.mkdir(parents=True)
    (session / "results.json").write_text(json.dumps(
        {"steps": [{"name": "US1-S1-recorded", "status": "pass"}]}))
    make_screenshot(session, "first.png")
    make_screenshot(session, "second.png")
    scenarios = [
        good_scenario(tmp_path, screenshot="first.png"),
        good_scenario(tmp_path, screenshot="second.png"),
    ]
    p = good_proof(
        tmp_path,
        driver="canary",
        canary_session=session_id,
        scenarios=scenarios,
    )

    ok, findings = verify(p, strict=True)

    assert not ok
    assert any("duplicate proof scenario ID" in finding for finding in findings)


def test_canary_results_reject_duplicate_step_scenario_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    session_id = "duplicate-step-id-session"
    session = tmp_path / ".canary" / "sessions" / session_id
    session.mkdir(parents=True)
    (session / "results.json").write_text(json.dumps({"steps": [
        {"name": "first capture", "scenarioId": "US1-S1", "status": "pass"},
        {"name": "second capture", "scenarioId": "US1-S1", "status": "pass"},
    ]}))
    make_screenshot(session, "scenario.png")
    scenario = good_scenario(tmp_path, screenshot="scenario.png")
    p = good_proof(
        tmp_path,
        driver="canary",
        canary_session=session_id,
        scenarios=[scenario],
    )

    ok, findings = verify(p, strict=True)

    assert not ok
    assert any("multiple Canary result steps" in finding for finding in findings)


def test_canary_results_reject_proof_scenario_without_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    session_id = "missing-scenario-id-session"
    session = tmp_path / ".canary" / "sessions" / session_id
    session.mkdir(parents=True)
    (session / "results.json").write_text(json.dumps(
        {"steps": [{"name": "US1-S1", "status": "pass"}]}))
    make_screenshot(session, "scenario.png")
    scenario = good_scenario(tmp_path, screenshot="scenario.png")
    del scenario["id"]
    p = good_proof(
        tmp_path,
        driver="canary",
        canary_session=session_id,
        scenarios=[scenario],
    )

    ok, findings = verify(p, strict=True)

    assert not ok
    assert any("id" in finding and "non-empty string" in finding
               for finding in findings)


@pytest.mark.parametrize("results_payload", [[], "not-an-object", 7, True])
def test_canary_malformed_results_payload_yields_finding(
    tmp_path, monkeypatch, results_payload,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    session_id = "malformed-results-session"
    session = tmp_path / ".canary" / "sessions" / session_id
    session.mkdir(parents=True)
    (session / "results.json").write_text(json.dumps(results_payload))
    make_screenshot(session, "scenario.png")
    scenario = good_scenario(tmp_path, screenshot="scenario.png")
    p = good_proof(
        tmp_path,
        driver="canary",
        canary_session=session_id,
        scenarios=[scenario],
    )

    ok, findings = verify(p, strict=True)

    assert not ok
    assert any("results.json" in finding and "object" in finding
               for finding in findings)


def test_canary_results_status_must_match_bound_proof_scenario(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    session_id = "status-binding-session"
    session = tmp_path / ".canary" / "sessions" / session_id
    session.mkdir(parents=True)
    (session / "results.json").write_text(json.dumps(
        {"steps": [{"name": "US1-S1-recorded", "status": "fail"}]}))
    make_screenshot(session, "scenario.png")
    scenario = good_scenario(tmp_path, screenshot="scenario.png")
    p = good_proof(
        tmp_path,
        driver="canary",
        canary_session=session_id,
        scenarios=[scenario],
    )

    ok, findings = verify(p, strict=True)

    assert not ok
    assert any("proof status" in finding.lower() and "step status" in finding.lower() for finding in findings)


def test_canary_missing_results_json_fails(tmp_path):
    session = tmp_path / "session"
    session.mkdir()
    p = good_proof(tmp_path, driver="canary", canary_session=str(session))
    ok, _ = verify(p)
    assert not ok


# ---------------------------------------------------------------- CLI

def run_cli(*args):
    return subprocess.run([sys.executable, str(MODULE), *args],
                          capture_output=True, text=True)


def test_cli_verify_pass_exit_0(tmp_path):
    r = run_cli("verify", str(good_proof(tmp_path)))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RUNTIME-PROOF: PASS" in r.stdout


def test_cli_verify_fail_exit_1(tmp_path):
    sc = good_scenario(tmp_path, console_errors=["boom"])
    r = run_cli("verify", str(good_proof(tmp_path, scenarios=[sc])))
    assert r.returncode == 1
    assert "RUNTIME-PROOF: FAIL" in r.stdout


def test_cli_strict_flag(tmp_path):
    p = good_proof(tmp_path, driver="agent")
    assert run_cli("verify", str(p)).returncode == 0
    assert run_cli("verify", str(p), "--strict").returncode == 1


def test_cli_env_strict(tmp_path):
    p = good_proof(tmp_path, driver="agent")
    env = dict(os.environ, RUNTIME_PROOF_STRICT="1")
    r = subprocess.run([sys.executable, str(MODULE), "verify", str(p)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 1


def test_cli_skeleton_emits_unfilled_template(tmp_path):
    scen_md = tmp_path / "scenarios.md"
    scen_md.write_text(
        "## US1-S1: login happy path\n"
        "- Given a registered user on /login\n"
        "- When they submit valid credentials\n"
        "- Then they land on /dashboard and see 'Welcome'\n"
        "\n"
        "## US2-S1 [visual]: visual dashboard\n"
        "- Given the dashboard page\n"
        "- Then it matches design intent\n")
    out = tmp_path / "proof.json"
    r = run_cli("skeleton", str(scen_md), "--out", str(out),
                "--base-url", "http://localhost:3000")
    assert r.returncode == 0, r.stdout + r.stderr
    data = json.loads(out.read_text())
    ids = [s["id"] for s in data["scenarios"]]
    assert ids == ["US1-S1", "US2-S1"]
    assert all(s["status"] == "UNFILLED" for s in data["scenarios"])
    # skeleton must NOT verify — evidence has to be filled by a real run
    assert run_cli("verify", str(out)).returncode == 1


# ---------------------------------------------------------- forgery hardening
# (v3.20.0 adversarial round: F2 forgeable bundle, F9 coverage completeness)

def make_pw_artifact(tmp_path: Path, name: str = "results.json",
                     age_min: int = 0) -> Path:
    p = tmp_path / name
    p.write_text('{"suites": []}')
    if age_min:
        old = time.time() - age_min * 60
        os.utime(p, (old, old))
    return p


def test_screenshot_must_be_an_image(tmp_path):
    shot = tmp_path / "fake.png"
    shot.write_text("x")  # printf x > shot.png forgery
    sc = good_scenario(tmp_path, screenshot=str(shot))
    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]))
    assert not ok
    assert any("not an image" in f for f in findings)


def test_playwright_driver_requires_artifact(tmp_path):
    p = good_proof(tmp_path, playwright_artifact=None)
    ok, findings = verify(p)
    assert not ok
    assert any("playwright_artifact" in f for f in findings)


def test_playwright_artifact_must_exist(tmp_path):
    p = good_proof(tmp_path, playwright_artifact=str(tmp_path / "nope.json"))
    ok, findings = verify(p)
    assert not ok
    assert any("playwright" in f and "not found" in f for f in findings)


def test_playwright_artifact_must_be_fresh(tmp_path):
    art = make_pw_artifact(tmp_path, "old-results.json", age_min=600)
    p = good_proof(tmp_path, playwright_artifact=str(art))
    ok, findings = verify(p, max_age_min=240)
    assert not ok
    assert any("stale" in f for f in findings)


def test_driver_cross_check_mismatch_fails(tmp_path):
    bp = tmp_path / "bp.txt"
    bp.write_text("WEB-TOUCH:yes\nDRIVER:canary\nBASE-URL:http://localhost:3000\n")
    ok, findings = verify(good_proof(tmp_path), browser_proof=str(bp))
    assert not ok
    assert any("mismatch" in f for f in findings)


def test_driver_cross_check_match_passes(tmp_path):
    bp = tmp_path / "bp.txt"
    bp.write_text("DRIVER:playwright\n")
    ok, findings = verify(good_proof(tmp_path), browser_proof=str(bp))
    assert ok, findings


def test_driver_cross_check_auto_detects_sibling(tmp_path):
    # browser-proof.txt next to proof.json is picked up without a flag
    (tmp_path / "browser-proof.txt").write_text("DRIVER:canary\n")
    ok, findings = verify(good_proof(tmp_path))
    assert not ok
    assert any("mismatch" in f for f in findings)


# coverage completeness vs scenarios.md (F9)

SCEN_MD = """## US1-S1: login happy path
- Given a registered user

## US1-S2: bad password error state
- Given a registered user

## US2-S1 [visual]: visual dashboard design
- Given the dashboard page
"""


def test_coverage_missing_functional_scenario_fails(tmp_path):
    md = tmp_path / "scenarios.md"
    md.write_text(SCEN_MD)
    sc = good_scenario(tmp_path, id="US1-S1")
    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]),
                          scenarios_md=str(md))
    assert not ok
    assert any("US1-S2" in f for f in findings)


def test_coverage_visual_not_required_in_functional_bundle(tmp_path):
    # codex round: the caller pins the requirement via kind= — bundle
    # self-declared kinds must never shrink it (default kind="all" would
    # require the visual ID too)
    md = tmp_path / "scenarios.md"
    md.write_text(SCEN_MD)
    scs = [good_scenario(tmp_path, id="US1-S1", shot_name="a.png"),
           good_scenario(tmp_path, id="US1-S2", shot_name="b.png")]
    ok, findings = verify(good_proof(tmp_path, scenarios=scs),
                          scenarios_md=str(md), kind="functional")
    assert ok, findings  # US2-S1 is visual — lives in design-proof.json


def test_coverage_visual_bundle_requires_visual_ids(tmp_path):
    md = tmp_path / "scenarios.md"
    md.write_text(SCEN_MD +
                  "\n## US2-S2 [visual]: visual mobile breakpoint\n"
                  "- Given 375px\n")
    sc = good_scenario(tmp_path, id="US2-S1", kind="visual",
                       interactions=0, static=True)
    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]),
                          scenarios_md=str(md))
    assert not ok
    assert any("US2-S2" in f for f in findings)


def test_scenarios_source_field_auto_loads(tmp_path):
    md = tmp_path / "scenarios.md"
    md.write_text(SCEN_MD)
    sc = good_scenario(tmp_path, id="US1-S1")
    p = good_proof(tmp_path, scenarios=[sc], scenarios_source=str(md))
    ok, findings = verify(p)
    assert not ok
    assert any("US1-S2" in f for f in findings)


def test_scenarios_source_missing_file_fails(tmp_path):
    p = good_proof(tmp_path, scenarios_source=str(tmp_path / "gone.md"))
    ok, findings = verify(p)
    assert not ok
    assert any("scenarios" in f and ("not found" in f or "missing" in f)
               for f in findings)


def test_skeleton_embeds_scenarios_source(tmp_path):
    md = tmp_path / "scenarios.md"
    md.write_text(SCEN_MD)
    out = tmp_path / "proof.json"
    rp.emit_skeleton(str(md), str(out), "http://localhost:3000")
    data = json.loads(out.read_text())
    assert data["scenarios_source"] == str(md)


def test_cli_scenarios_flag(tmp_path):
    md = tmp_path / "scenarios.md"
    md.write_text(SCEN_MD)
    sc = good_scenario(tmp_path, id="US1-S1")
    p = good_proof(tmp_path, scenarios=[sc])
    r = run_cli("verify", str(p), "--scenarios", str(md))
    assert r.returncode == 1
    assert "US1-S2" in r.stdout


# ------------------------------------------------- codex round: C1 kind gaming

def test_forged_kind_mismatch_vs_source_fails(tmp_path):
    # source says US1-S1 is functional; bundle marks it visual+static to
    # dodge the interactions check — must be caught
    md = tmp_path / "scenarios.md"
    md.write_text(SCEN_MD)
    sc = good_scenario(tmp_path, id="US1-S1", kind="visual",
                       interactions=0, static=True)
    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]),
                          scenarios_md=str(md))
    assert not ok
    assert any("kind mismatch" in f for f in findings)


def test_kind_functional_requires_ids_regardless_of_bundle_kinds(tmp_path):
    # all-visual forged bundle must NOT shrink the functional requirement
    md = tmp_path / "scenarios.md"
    md.write_text(SCEN_MD)
    sc = good_scenario(tmp_path, id="US2-S1", kind="visual",
                       interactions=0, static=True)
    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]),
                          scenarios_md=str(md), kind="functional")
    assert not ok
    assert any("US1-S1" in f and "US1-S2" in f for f in findings)


def test_kind_visual_requires_only_visual_ids(tmp_path):
    md = tmp_path / "scenarios.md"
    md.write_text(SCEN_MD)
    sc = good_scenario(tmp_path, id="US2-S1", kind="visual",
                       interactions=0, static=True)
    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]),
                          scenarios_md=str(md), kind="visual")
    assert ok, findings


def test_kind_all_requires_everything(tmp_path):
    md = tmp_path / "scenarios.md"
    md.write_text(SCEN_MD)
    sc = good_scenario(tmp_path, id="US1-S1")
    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]),
                          scenarios_md=str(md), kind="all")
    assert not ok
    assert any("US1-S2" in f for f in findings)
    assert any("US2-S1" in f for f in findings)


def test_cli_kind_flag(tmp_path):
    md = tmp_path / "scenarios.md"
    md.write_text(SCEN_MD)
    scs = [good_scenario(tmp_path, id="US1-S1", shot_name="a.png"),
           good_scenario(tmp_path, id="US1-S2", shot_name="b.png")]
    p = good_proof(tmp_path, scenarios=scs)
    r = run_cli("verify", str(p), "--scenarios", str(md), "--kind", "functional")
    assert r.returncode == 0, r.stdout


# codex round 3 H2: scenario kind must come from an explicit heading tag
# ("## <ID> [visual]: <title>"), never from title-keyword inference — a
# functional flow titled "visual polish ..." must NOT be reclassified out
# of the --kind functional required set. Untagged defaults to functional.

def test_kind_explicit_visual_tag_parsed():
    parsed = rp._parse_scenarios_md(
        "## US1-S1 [visual]: login happy path\n- Given x\n")
    assert parsed == [("US1-S1", "login happy path", "visual")]


def test_kind_explicit_functional_tag_wins_over_title():
    parsed = rp._parse_scenarios_md(
        "## US1-S1 [functional]: visual design of dashboard\n- x\n")
    assert parsed[0][2] == "functional"


def test_kind_title_keyword_does_not_reclassify(tmp_path):
    md = tmp_path / "scenarios.md"
    md.write_text("## US1-S1: visual polish of the login flow\n- Given x\n")
    sc = good_scenario(tmp_path, id="OTHER-1")
    ok, findings = verify(good_proof(tmp_path, scenarios=[sc]),
                          scenarios_md=str(md), kind="functional")
    assert not ok
    assert any("US1-S1" in f for f in findings)
