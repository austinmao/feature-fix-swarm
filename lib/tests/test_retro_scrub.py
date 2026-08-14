"""Independent Wave 0 acceptance tests for lib/retro_scrub.py (spec-011 retro loop).

Authored BEFORE reading lib/retro_scrub.py or scripts/gsd/retro.sh, directly from
the contract documents: .planning/phases/01-retro-core/01-VALIDATION.md,
01-01/02/03-PLAN.md, WALL-RESIDUALS.md, .planning/REQUIREMENTS.md, and
specs/011-retro-loop/spec.md. Table-driven, stdlib + pytest only.

It is EXPECTED that some of these tests fail against the current production
module — the contract wins; do not weaken a test to make it pass.

## Ambiguity resolutions (documented per the task instructions)

- Reject-code mapping: the contract names four stable codes (RETRO:invalid-schema,
  RETRO:unsafe-value, RETRO:value-too-long, RETRO:consumer-identity) but does not
  enumerate which code applies to which failure category beyond length ("500-char
  cap" -> value-too-long) and identity ("consumer repo's owner/name" ->
  consumer-identity). We choose the simplest reading: unknown keys / wrong
  container-or-scalar types / booleans-in-numeric-slots / non-finite numbers ->
  RETRO:invalid-schema (a structural/type violation); path-shaped, URL-shaped,
  credential-bearing, traversal, NUL/control-char, and non-allowlisted-relative-path
  values -> RETRO:unsafe-value (a per-key content violation). Where this mapping is
  used for an assertion it is called out inline.
- Digest/finding row shape: REQ-02 and AC-002 name the fact classes but not exact
  JSON keys. We use: script, event_class, gate, exit_code, ts (RFC3339), duration_seconds,
  model_tier for digest rows, and script, gate, sig, severity for findings-queue rows
  (sig is the 64-hex raw signature the contract describes).
- collect_payload(events, findings) join key: the contract does not name how
  findings-queue rows correlate to digest events. sig_derived/ordering tests below
  are written to avoid depending on any particular join key — they assert only the
  documented invariants (raw sig never serialized, sig_derived present iff raw sig is
  exactly 64 lowercase hex, deterministic order under input permutation).
- suggested_fix is used as the vehicle for the 500/501-char boundary test because it
  is the one finding field the contract describes as pattern-anchored (not a small
  closed enum), so a long-but-pattern-matching value can be constructed without
  guessing an enum's membership.
- `script` vs `suggested_fix` shape: the interfaces text distinguishes "FFS script
  basenames" (the `script`/`event` field) from the anchored-glob-path form used only
  for `suggested_fix` ("scripts/gsd/*.sh", "lib/*.py", ...). We read this literally:
  `script` is a bare basename (e.g. "digest.sh", "retro.sh") with no path separator,
  while `suggested_fix` is a full FFS-relative path. This was confirmed empirically by
  running the tests once against the in-repo production module (an allowed black-box
  execution, not a source read) and observing that a full "scripts/gsd/digest.sh"
  value was rejected as an invalid script identifier — consistent with the
  basename-only reading, so the test builders below use bare basenames for `script`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "retro"
RETRO_SCRUB_PATH = REPO_ROOT / "lib" / "retro_scrub.py"

# Load by file path (repo convention, see lib/tests/test_dispatch.py) so no
# package __init__.py is required and so we "import it, do not read its source".
_spec = importlib.util.spec_from_file_location("retro_scrub", RETRO_SCRUB_PATH)
assert _spec is not None and _spec.loader is not None
retro_scrub = importlib.util.module_from_spec(_spec)
sys.modules["retro_scrub"] = retro_scrub
_spec.loader.exec_module(retro_scrub)


CODE_INVALID_SCHEMA = "RETRO:invalid-schema"
CODE_UNSAFE_VALUE = "RETRO:unsafe-value"
CODE_TOO_LONG = "RETRO:value-too-long"
CODE_CONSUMER_IDENTITY = "RETRO:consumer-identity"
ALL_REJECT_CODES = {CODE_INVALID_SCHEMA, CODE_UNSAFE_VALUE, CODE_TOO_LONG, CODE_CONSUMER_IDENTITY}


def reject_code(excinfo) -> str:
    """Best-effort extraction of the typed code from a raised RetroReject.

    RetroReject(code: str) is the declared constructor; we prefer a `.code`
    attribute but fall back to the exception's string form so this helper stays
    correct even if the attribute is named differently.
    """
    exc = excinfo.value
    code = getattr(exc, "code", None)
    if code:
        return code
    return str(exc)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

_PRIORITY_BY_CLASS = {
    "security": "P0", "scrub": "P0", "grant-bypass": "P0", "data-loss": "P0",
    "dead-executor": "P1", "unrecovered-stall": "P1",
    "fallback": "P2", "retry": "P2", "gate-warn": "P2",
    "optimization": "P3",
}


def mk_finding(**overrides) -> dict:
    # validate_payload appears to cross-check the caller-supplied priority and
    # fingerprint against the values the deterministic transforms would
    # produce (confirmed empirically: a placeholder fingerprint was rejected
    # as RETRO:invalid-fingerprint), so the default baseline computes both
    # self-consistently; callers that want to test a WRONG priority/fingerprint
    # pass them explicitly as overrides instead.
    finding = {
        "script": "digest.sh",
        "event_class": "dead-executor",
        "gate": "review-gate",
        "exit_code": 1,
        "ffs_minor": "5.0",
    }
    finding.update(overrides)
    if "priority" not in overrides:
        finding["priority"] = _PRIORITY_BY_CLASS.get(finding["event_class"], "P3")
    if "fingerprint" not in overrides:
        material = "|".join(str(finding[k]) for k in
                             ("script", "event_class", "gate", "exit_code", "ffs_minor"))
        finding["fingerprint"] = hashlib.sha256(material.encode()).hexdigest()[:16]
    return finding


def mk_payload(findings=None, metrics=None, **root_overrides) -> dict:
    payload = {
        "schema": "ffs.retro/v1",
        "findings": findings if findings is not None else [mk_finding()],
    }
    if metrics is not None:
        payload["metrics"] = metrics
    payload.update(root_overrides)
    return payload


def mk_event(**overrides) -> dict:
    # model_tier is deliberately omitted: it is optional per the interfaces
    # text and the contract does not name its closed value set, so fixtures
    # avoid guessing it (confirmed empirically: a guessed value was rejected
    # as RETRO:invalid-model-tier when run against the in-repo module).
    event = {
        "script": "digest.sh",
        "event_class": "retry",
        "gate": "review-gate",
        "exit_code": 0,
        "ts": "2026-08-11T10:00:00Z",
        "duration_seconds": 1.0,
    }
    event.update(overrides)
    return event


# ===========================================================================
# validate_payload — schema / type / enum / numeric-first
# ===========================================================================

class TestValidatePayloadPositiveControl:
    def test_baseline_payload_is_accepted(self):
        # Positive control: everything below assumes this baseline is legitimate.
        retro_scrub.validate_payload(mk_payload())

    def test_baseline_metrics_are_accepted(self):
        metrics = {
            "wall_seconds": 12.0,
            "active_seconds": 4.0,
            "wall_active_ratio": 3.0,
            "intervention_free": True,
        }
        retro_scrub.validate_payload(mk_payload(metrics=metrics))


class TestValidatePayloadSchema:
    def test_unknown_root_key_rejects(self):
        payload = mk_payload()
        payload["extra_root_key"] = "x"
        with pytest.raises(retro_scrub.RetroReject) as ei:
            retro_scrub.validate_payload(payload)
        assert reject_code(ei) == CODE_INVALID_SCHEMA

    def test_unknown_finding_key_rejects(self):
        payload = mk_payload(findings=[mk_finding(extra_finding_key="x")])
        with pytest.raises(retro_scrub.RetroReject) as ei:
            retro_scrub.validate_payload(payload)
        assert reject_code(ei) == CODE_INVALID_SCHEMA

    def test_missing_schema_key_rejects(self):
        payload = mk_payload()
        del payload["schema"]
        with pytest.raises(retro_scrub.RetroReject):
            retro_scrub.validate_payload(payload)

    def test_wrong_schema_value_rejects(self):
        payload = mk_payload(schema="ffs.retro/v2")
        with pytest.raises(retro_scrub.RetroReject):
            retro_scrub.validate_payload(payload)

    def test_findings_not_a_list_rejects(self):
        payload = mk_payload()
        payload["findings"] = {"not": "a list"}
        with pytest.raises(retro_scrub.RetroReject) as ei:
            retro_scrub.validate_payload(payload)
        assert reject_code(ei) == CODE_INVALID_SCHEMA

    def test_finding_not_a_dict_rejects(self):
        payload = mk_payload(findings=["not-a-dict"])
        with pytest.raises(retro_scrub.RetroReject):
            retro_scrub.validate_payload(payload)

    def test_exit_code_wrong_type_rejects(self):
        payload = mk_payload(findings=[mk_finding(exit_code="1")])
        with pytest.raises(retro_scrub.RetroReject) as ei:
            retro_scrub.validate_payload(payload)
        assert reject_code(ei) == CODE_INVALID_SCHEMA

    def test_boolean_in_numeric_slot_rejects(self):
        # bool is a subclass of int in Python; the contract requires bool to be
        # rejected explicitly ("reject bool before integer checks").
        payload = mk_payload(findings=[mk_finding(exit_code=True)])
        with pytest.raises(retro_scrub.RetroReject) as ei:
            retro_scrub.validate_payload(payload)
        assert reject_code(ei) == CODE_INVALID_SCHEMA

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_metric_number_rejects(self, bad):
        metrics = {
            "wall_seconds": bad,
            "active_seconds": 1.0,
            "wall_active_ratio": 1.0,
            "intervention_free": True,
        }
        with pytest.raises(retro_scrub.RetroReject):
            retro_scrub.validate_payload(mk_payload(metrics=metrics))

    def test_missing_required_finding_key_rejects(self):
        finding = mk_finding()
        del finding["gate"]
        with pytest.raises(retro_scrub.RetroReject):
            retro_scrub.validate_payload(mk_payload(findings=[finding]))

    def test_empty_root_rejects(self):
        with pytest.raises(retro_scrub.RetroReject):
            retro_scrub.validate_payload({})


class TestValidatePayloadMetricsSchemaClosed:
    """AC-013 / interfaces: metrics is a CLOSED schema, not an open channel."""

    def test_extra_metrics_key_rejects(self):
        metrics = {
            "wall_seconds": 1.0,
            "active_seconds": 1.0,
            "wall_active_ratio": 1.0,
            "intervention_free": True,
            "extra": "nope",
        }
        with pytest.raises(retro_scrub.RetroReject):
            retro_scrub.validate_payload(mk_payload(metrics=metrics))

    def test_metrics_wrong_type_rejects(self):
        metrics = {
            "wall_seconds": "12.0",  # must be a number, not a string
            "active_seconds": 1.0,
            "wall_active_ratio": 1.0,
            "intervention_free": True,
        }
        with pytest.raises(retro_scrub.RetroReject):
            retro_scrub.validate_payload(mk_payload(metrics=metrics))

    def test_intervention_free_non_bool_rejects(self):
        metrics = {
            "wall_seconds": 1.0,
            "active_seconds": 1.0,
            "wall_active_ratio": 1.0,
            "intervention_free": "true",
        }
        with pytest.raises(retro_scrub.RetroReject):
            retro_scrub.validate_payload(mk_payload(metrics=metrics))

    def test_wall_active_ratio_below_one_rejects(self):
        metrics = {
            "wall_seconds": 1.0,
            "active_seconds": 1.0,
            "wall_active_ratio": 0.5,
            "intervention_free": True,
        }
        with pytest.raises(retro_scrub.RetroReject):
            retro_scrub.validate_payload(mk_payload(metrics=metrics))

    def test_negative_wall_seconds_rejects(self):
        metrics = {
            "wall_seconds": -1.0,
            "active_seconds": 1.0,
            "wall_active_ratio": 1.0,
            "intervention_free": True,
        }
        with pytest.raises(retro_scrub.RetroReject):
            retro_scrub.validate_payload(mk_payload(metrics=metrics))

    def test_partial_metrics_object_is_accepted(self):
        # Metric keys are individually optional (omission is the contracted
        # behavior for undeliverable metrics) — a subset must still validate.
        retro_scrub.validate_payload(mk_payload(metrics={"intervention_free": False}))


# ===========================================================================
# validate_payload — unsafe representations (REQ-05 / AC-005 / EDGE-008)
# ===========================================================================

HOSTILE_SUGGESTED_FIX = [
    "/Users/eve/notes.md",
    "/home/eve/notes.md",
    "/private/etc/passwd",
    "/tmp/x.sh",
    "C:\\Users\\eve\\x.sh",
    "\\\\server\\share\\x.sh",
    "file:///etc/passwd",
    "scripts%2Fgsd%2Fx.sh",
    "..%2f..%2fetc%2fpasswd",
    "https://user:" + "hunter2@evil.example.com/x.sh",
    "scripts/gsd/../../../etc/passwd",
    "scripts/gsd/x\x00.sh",
    "scripts/gsd/x\x01.sh",
    "docs/README.md",  # syntactically safe but not one of the four allowlisted globs
    "lib/../lib/gates.py",
]


class TestValidatePayloadUnsafeValues:
    @pytest.mark.parametrize("hostile", HOSTILE_SUGGESTED_FIX)
    def test_unsafe_suggested_fix_rejects(self, hostile):
        payload = mk_payload(findings=[mk_finding(suggested_fix=hostile)])
        with pytest.raises(retro_scrub.RetroReject) as ei:
            retro_scrub.validate_payload(payload)
        assert reject_code(ei) == CODE_UNSAFE_VALUE
        # Rejection must never reflect the rejected bytes back to the caller.
        assert hostile not in str(ei.value)

    @pytest.mark.parametrize(
        "safe",
        [
            "scripts/gsd/retro.sh",
            "lib/retro_scrub.py",
            "skills/testing-policy/SKILL.md",
            ".github/workflows/ci.yml",
        ],
    )
    def test_allowlisted_suggested_fix_forms_pass(self, safe):
        retro_scrub.validate_payload(mk_payload(findings=[mk_finding(suggested_fix=safe)]))


class TestValidatePayloadLengthBoundary:
    def test_exactly_500_chars_passes(self):
        value = "scripts/gsd/" + ("a" * 485) + ".sh"
        assert len(value) == 500
        retro_scrub.validate_payload(mk_payload(findings=[mk_finding(suggested_fix=value)]))

    def test_501_chars_rejects(self):
        value = "scripts/gsd/" + ("a" * 486) + ".sh"
        assert len(value) == 501
        with pytest.raises(retro_scrub.RetroReject) as ei:
            retro_scrub.validate_payload(mk_payload(findings=[mk_finding(suggested_fix=value)]))
        assert reject_code(ei) == CODE_TOO_LONG


# ===========================================================================
# validate_payload — consumer identity (REQ-05 / WALL-RESIDUAL 5372515af063)
# ===========================================================================

class TestValidatePayloadConsumerIdentity:
    def test_identity_match_on_otherwise_valid_value_rejects(self):
        # "dead-executor" and "review-gate" are ordinary, otherwise-legitimate
        # closed values in mk_payload(); if the consumer's resolved owner/name
        # happens to equal one of them, that alone must cause a reject. This
        # proves the identity check inspects VALUES, not just obviously
        # path/URL-shaped strings.
        with pytest.raises(retro_scrub.RetroReject) as ei:
            retro_scrub.validate_payload(mk_payload(), consumer_identity=("dead-executor",))
        assert reject_code(ei) == CODE_CONSUMER_IDENTITY

    def test_identity_match_on_repo_name_component_rejects(self):
        with pytest.raises(retro_scrub.RetroReject) as ei:
            retro_scrub.validate_payload(mk_payload(), consumer_identity=("review-gate",))
        assert reject_code(ei) == CODE_CONSUMER_IDENTITY

    def test_default_empty_identity_does_not_falsely_reject(self):
        # Documents the function's own contract: an empty/default identity
        # tuple must not itself misfire on an otherwise-safe payload. Whether
        # every call SITE actually supplies a non-empty tuple (WALL-RESIDUAL
        # 5372515af063) is proven separately in tests/bats/retro.bats via the
        # "consumer identity leak via structurally-valid values" case, which
        # exercises the real retro.sh wiring end to end.
        retro_scrub.validate_payload(mk_payload())

    def test_unrelated_identity_does_not_reject(self):
        retro_scrub.validate_payload(mk_payload(), consumer_identity=("totally-unrelated-org",))


# ===========================================================================
# collect_payload — sig_derived (REQ-02 / interfaces wall sig bade4623)
# ===========================================================================

class TestSigDerived:
    def test_valid_64_hex_raw_sig_is_hashed_and_never_serialized(self):
        raw_sig = "a" * 64
        events = [mk_event()]
        findings = [{"script": events[0]["script"], "gate": events[0]["gate"],
                     "sig": raw_sig, "severity": "P2"}]
        candidate = retro_scrub.collect_payload(events, findings)
        dumped = json.dumps(candidate)
        expected = hashlib.sha256(raw_sig.encode()).hexdigest()[:16]
        assert expected in dumped
        assert raw_sig not in dumped

    @pytest.mark.parametrize(
        "bad_sig",
        [
            "A" * 64,       # uppercase not accepted
            "a" * 63,       # too short
            "a" * 65,       # too long
            "g" * 64,       # not hex
            "a" * 63 + "Z",  # mixed invalid
        ],
    )
    def test_malformed_raw_sig_never_becomes_sig_derived(self, bad_sig):
        events = [mk_event()]
        findings = [{"script": events[0]["script"], "gate": events[0]["gate"],
                     "sig": bad_sig, "severity": "P2"}]
        candidate = retro_scrub.collect_payload(events, findings)
        dumped = json.dumps(candidate)
        assert bad_sig not in dumped
        would_be_hash = hashlib.sha256(bad_sig.encode()).hexdigest()[:16]
        assert would_be_hash not in dumped

    def test_no_findings_queue_entry_means_no_sig_derived_leak(self):
        events = [mk_event()]
        candidate = retro_scrub.collect_payload(events, [])
        for finding in candidate.get("findings", []):
            assert "sig" not in finding


# ===========================================================================
# collect_payload — allowlist collection / provenance / ordering / self-events
# ===========================================================================

class TestCollectPayload:
    def test_self_events_are_excluded(self):
        self_event = mk_event(script="retro.sh")
        other = mk_event(script="digest.sh")
        candidate = retro_scrub.collect_payload([self_event, other], [])
        scripts = [f.get("script") for f in candidate["findings"]]
        assert "retro.sh" not in scripts
        assert len(candidate["findings"]) == 1

    def test_empty_input_yields_empty_findings(self):
        candidate = retro_scrub.collect_payload([], [])
        assert candidate["findings"] == []

    def test_order_is_deterministic_under_input_permutation(self):
        e1 = mk_event(script="digest.sh")
        e2 = mk_event(script="consume-danger-grant.py")
        forward = retro_scrub.collect_payload([e1, e2], [])
        backward = retro_scrub.collect_payload([e2, e1], [])
        assert forward == backward
        scripts = [f.get("script") for f in forward["findings"]]
        assert scripts == sorted(scripts)

    def test_consecutive_valid_records_stay_distinct(self):
        # A-REQ02-ADJACENCY: two structurally-identical-except-for-gate events
        # must both appear, not be merged into one.
        e1 = mk_event(gate="review-gate")
        e2 = mk_event(gate="canary-gate")
        candidate = retro_scrub.collect_payload([e1, e2], [])
        assert len(candidate["findings"]) == 2

    def test_unrecognized_safe_event_class_normalizes_to_unknown(self):
        # A-REQ03-UNCLASSIFIED: a syntactically-safe but unrecognized event
        # class identifier must not carry consumer text through as-is.
        event = mk_event(event_class="something-nobody-declared")
        candidate = retro_scrub.collect_payload([event], [])
        classes = [f.get("event_class") for f in candidate["findings"]]
        assert classes == ["unknown"]

    def test_consumer_prose_and_paths_do_not_enter_findings(self):
        event = mk_event()
        event["consumer_note"] = "do not leak this free text"  # not an allowlisted key
        candidate = retro_scrub.collect_payload([event], [])
        dumped = json.dumps(candidate)
        assert "do not leak this free text" not in dumped


# ===========================================================================
# classify_priority — REQ-03 / AC-003
# ===========================================================================

class TestClassifyPriority:
    @pytest.mark.parametrize("cls", ["security", "scrub", "grant-bypass", "data-loss"])
    def test_p0_classes(self, cls):
        assert retro_scrub.classify_priority(mk_finding(event_class=cls)) == "P0"

    @pytest.mark.parametrize("cls", ["dead-executor", "unrecovered-stall"])
    def test_p1_named_classes(self, cls):
        assert retro_scrub.classify_priority(mk_finding(event_class=cls)) == "P1"

    @pytest.mark.parametrize("cls", ["fallback", "retry", "gate-warn"])
    def test_p2_classes(self, cls):
        assert retro_scrub.classify_priority(mk_finding(event_class=cls)) == "P2"

    @pytest.mark.parametrize("cls", ["optimization", "unknown", "not-a-real-class"])
    def test_p3_and_unknown_fallback(self, cls):
        assert retro_scrub.classify_priority(mk_finding(event_class=cls)) == "P3"

    def test_ratio_at_exactly_2_0_triggers_p1_on_a_p2_baseline_class(self):
        finding = mk_finding(event_class="fallback")
        class_metrics = {"fallback": {"wall_active_ratio": 2.0}}
        assert retro_scrub.classify_priority(finding, class_metrics) == "P1"

    def test_ratio_below_2_0_does_not_trigger_p1(self):
        finding = mk_finding(event_class="fallback")
        class_metrics = {"fallback": {"wall_active_ratio": 1.999999}}
        assert retro_scrub.classify_priority(finding, class_metrics) == "P2"

    def test_absent_ratio_never_triggers_p1(self):
        finding = mk_finding(event_class="optimization")
        class_metrics = {"optimization": {}}
        assert retro_scrub.classify_priority(finding, class_metrics) == "P3"

    def test_ratio_on_unrelated_class_does_not_leak_into_this_finding(self):
        finding = mk_finding(event_class="optimization")
        class_metrics = {"fallback": {"wall_active_ratio": 99.0}}
        assert retro_scrub.classify_priority(finding, class_metrics) == "P3"

    def test_multi_rule_match_takes_highest_severity(self):
        # "fallback" alone -> P2, but a >=2.0 same-class ratio also matches the
        # P1 rule; multi-rule match must select the highest severity (P1).
        finding = mk_finding(event_class="fallback")
        class_metrics = {"fallback": {"wall_active_ratio": 5.0}}
        assert retro_scrub.classify_priority(finding, class_metrics) == "P1"

    def test_none_class_metrics_is_accepted(self):
        assert retro_scrub.classify_priority(mk_finding(event_class="dead-executor"), None) == "P1"


# ===========================================================================
# stable_fingerprint — REQ-04 / AC-004
# ===========================================================================

class TestStableFingerprint:
    def test_exact_formula_and_length(self):
        finding = mk_finding(
            script="x.sh", event_class="dead-executor",
            gate="review-gate", exit_code=1, ffs_minor="5.0",
        )
        expected = hashlib.sha256(
            "x.sh|dead-executor|review-gate|1|5.0".encode()
        ).hexdigest()[:16]
        got = retro_scrub.stable_fingerprint(finding)
        assert got == expected
        assert len(got) == 16
        assert got == got.lower()
        int(got, 16)  # must be valid hex

    def test_identical_across_dict_key_ordering(self):
        d1 = {"script": "x.sh", "event_class": "dead-executor", "gate": "review-gate",
              "exit_code": 1, "ffs_minor": "1.0"}
        d2 = {"ffs_minor": "1.0", "exit_code": 1, "gate": "review-gate",
              "event_class": "dead-executor", "script": "x.sh"}
        assert retro_scrub.stable_fingerprint(d1) == retro_scrub.stable_fingerprint(d2)

    def test_volatile_fields_do_not_perturb_identity(self):
        base = mk_finding()
        decorated = dict(base, ts="2026-08-11T10:00:00Z", duration_seconds=42,
                          run_id="run-123", model_tier="frontier", priority="P1",
                          fingerprint="ignored-here")
        assert retro_scrub.stable_fingerprint(base) == retro_scrub.stable_fingerprint(decorated)

    @pytest.mark.parametrize("field,value", [
        ("script", "other.sh"),
        ("event_class", "unrecovered-stall"),
        ("gate", "canary-gate"),
        ("exit_code", 2),
        ("ffs_minor", "5.1"),
    ])
    def test_changing_any_of_five_fields_changes_fingerprint(self, field, value):
        base = mk_finding()
        changed = dict(base, **{field: value})
        assert retro_scrub.stable_fingerprint(base) != retro_scrub.stable_fingerprint(changed)

    def test_identical_across_repeated_calls(self):
        finding = mk_finding()
        assert retro_scrub.stable_fingerprint(finding) == retro_scrub.stable_fingerprint(dict(finding))


# ===========================================================================
# derive_metrics / derive_class_metrics — REQ-13 / AC-013 / EDGE-011
# ===========================================================================

class TestDeriveMetrics:
    def test_fully_valid_events_produce_all_metrics(self):
        events = [
            mk_event(ts="2026-08-11T10:00:00Z", duration_seconds=1.0),
            mk_event(ts="2026-08-11T10:00:10Z", duration_seconds=2.0),
        ]
        metrics = retro_scrub.derive_metrics(events)
        assert metrics["wall_seconds"] == pytest.approx(10.0)
        assert metrics["active_seconds"] == pytest.approx(3.0)
        assert metrics["wall_active_ratio"] == pytest.approx(10.0 / 3.0)
        assert metrics["intervention_free"] is True

    def test_one_invalid_timestamp_omits_wall_but_not_active(self):
        events = [
            mk_event(ts="2026-08-11T10:00:00Z", duration_seconds=1.0),
            mk_event(ts="not-a-timestamp", duration_seconds=2.0),
        ]
        metrics = retro_scrub.derive_metrics(events)
        assert "wall_seconds" not in metrics
        assert metrics["active_seconds"] == pytest.approx(3.0)
        assert "wall_active_ratio" not in metrics

    def test_one_missing_duration_omits_active_but_not_wall(self):
        e2 = mk_event(ts="2026-08-11T10:00:10Z")
        del e2["duration_seconds"]
        events = [mk_event(ts="2026-08-11T10:00:00Z", duration_seconds=1.0), e2]
        metrics = retro_scrub.derive_metrics(events)
        assert metrics["wall_seconds"] == pytest.approx(10.0)
        assert "active_seconds" not in metrics
        assert "wall_active_ratio" not in metrics

    def test_no_valid_subset_fallback(self):
        # Three events, one bad timestamp: wall must be OMITTED entirely, never
        # computed from the two remaining valid ones.
        events = [
            mk_event(ts="2026-08-11T10:00:00Z", duration_seconds=1.0),
            mk_event(ts="garbage", duration_seconds=1.0),
            mk_event(ts="2026-08-11T10:00:30Z", duration_seconds=1.0),
        ]
        metrics = retro_scrub.derive_metrics(events)
        assert "wall_seconds" not in metrics

    def test_fromisoformat_lax_but_non_rfc3339_string_is_rejected(self):
        # Space instead of 'T' is accepted by datetime.fromisoformat in modern
        # Python but is NOT RFC3339 — the contract requires an anchored RFC3339
        # regex full-match BEFORE fromisoformat ever runs (wall sig 124bcf84).
        events = [
            mk_event(ts="2026-08-11 10:00:00", duration_seconds=1.0),
            mk_event(ts="2026-08-11T10:00:10Z", duration_seconds=1.0),
        ]
        metrics = retro_scrub.derive_metrics(events)
        assert "wall_seconds" not in metrics

    def test_backward_clock_jump_omits_wall(self):
        # WALL-RESIDUAL ee1e996ac142: an intermediate backward jump must omit
        # wall/ratio even though naive last-minus-first would still be positive.
        events = [
            mk_event(ts="2026-08-11T10:00:00Z", duration_seconds=1.0),
            mk_event(ts="2026-08-11T10:00:20Z", duration_seconds=1.0),
            mk_event(ts="2026-08-11T10:00:10Z", duration_seconds=1.0),  # backward jump
        ]
        metrics = retro_scrub.derive_metrics(events)
        assert "wall_seconds" not in metrics
        assert "wall_active_ratio" not in metrics
        # active is independent of ordering
        assert metrics.get("active_seconds") == pytest.approx(3.0)

    def test_clock_skew_wall_less_than_active_omits_ratio_only(self):
        events = [
            mk_event(ts="2026-08-11T10:00:00Z", duration_seconds=1.0),
            mk_event(ts="2026-08-11T10:00:01Z", duration_seconds=100.0),
        ]
        metrics = retro_scrub.derive_metrics(events)
        assert metrics["wall_seconds"] == pytest.approx(1.0)
        assert metrics["active_seconds"] == pytest.approx(101.0)
        assert "wall_active_ratio" not in metrics

    def test_zero_active_omits_ratio(self):
        events = [
            mk_event(ts="2026-08-11T10:00:00Z", duration_seconds=0.0),
            mk_event(ts="2026-08-11T10:00:10Z", duration_seconds=0.0),
        ]
        metrics = retro_scrub.derive_metrics(events)
        assert "wall_active_ratio" not in metrics

    @pytest.mark.parametrize("bad_duration", [True, float("nan"), float("inf"), -1.0])
    def test_bool_nonfinite_negative_duration_omits_active(self, bad_duration):
        events = [
            mk_event(ts="2026-08-11T10:00:00Z", duration_seconds=1.0),
            mk_event(ts="2026-08-11T10:00:10Z", duration_seconds=bad_duration),
        ]
        metrics = retro_scrub.derive_metrics(events)
        assert "active_seconds" not in metrics

    def test_intervention_free_true_when_no_operator_intervention(self):
        events = [mk_event(event_class="retry"), mk_event(event_class="dead-executor")]
        assert retro_scrub.derive_metrics(events)["intervention_free"] is True

    def test_intervention_free_false_when_present(self):
        events = [mk_event(event_class="retry"), mk_event(event_class="operator-intervention")]
        assert retro_scrub.derive_metrics(events)["intervention_free"] is False

    def test_intervention_free_omitted_when_class_completeness_unknown(self):
        events = [mk_event(event_class="retry"), mk_event(event_class=None)]
        metrics = retro_scrub.derive_metrics(events)
        assert "intervention_free" not in metrics

    def test_missing_metrics_are_absent_keys_never_null_or_zero(self):
        events = [mk_event(ts="garbage")]
        del events[0]["duration_seconds"]
        metrics = retro_scrub.derive_metrics(events)
        assert "wall_seconds" not in metrics
        assert "active_seconds" not in metrics
        assert "wall_active_ratio" not in metrics
        for key in ("wall_seconds", "active_seconds", "wall_active_ratio"):
            assert metrics.get(key, "ABSENT") == "ABSENT"

    def test_no_events_yields_no_derived_timing_metrics(self):
        metrics = retro_scrub.derive_metrics([])
        assert "wall_seconds" not in metrics
        assert "active_seconds" not in metrics


class TestDeriveClassMetrics:
    def test_prerequisite_check_is_independent_per_class(self):
        events = [
            mk_event(event_class="retry", ts="2026-08-11T10:00:00Z", duration_seconds=1.0),
            mk_event(event_class="retry", ts="2026-08-11T10:00:10Z", duration_seconds=1.0),
            mk_event(event_class="fallback", ts="garbage", duration_seconds=1.0),
            mk_event(event_class="fallback", ts="2026-08-11T10:00:20Z", duration_seconds=1.0),
        ]
        by_class = retro_scrub.derive_class_metrics(events)
        assert "wall_seconds" in by_class["retry"] or "wall_active_ratio" in by_class["retry"]
        assert by_class["retry"].get("wall_active_ratio") == pytest.approx(10.0)
        assert "wall_active_ratio" not in by_class.get("fallback", {})

    def test_single_event_class_has_no_positive_wall(self):
        events = [mk_event(event_class="optimization", ts="2026-08-11T10:00:00Z", duration_seconds=5.0)]
        by_class = retro_scrub.derive_class_metrics(events)
        assert "wall_active_ratio" not in by_class.get("optimization", {})

    def test_raw_per_class_map_never_reaches_serialized_payload(self):
        events = [
            mk_event(event_class="retry", ts="2026-08-11T10:00:00Z", duration_seconds=1.0),
            mk_event(event_class="retry", ts="2026-08-11T10:00:10Z", duration_seconds=1.0),
        ]
        candidate = retro_scrub.collect_payload(events, [])
        dumped = json.dumps(candidate)
        assert "wall_active_ratio" not in dumped or "class_metrics" not in dumped


# ===========================================================================
# normalize_ffs_minor — REQ-15 / AC-016 / EDGE-010
# ===========================================================================

class TestNormalizeFfsMinor:
    @pytest.mark.parametrize("heading,expected", [
        ("## v1.4.0 — Newer release\n", "1.4"),
        ("## 1.4.0 — no leading v\n", "1.4"),
        ("## [1.4.0] — bracketed\n", "1.4"),
    ])
    def test_accepted_stable_heading_forms(self, heading, expected):
        assert retro_scrub.normalize_ffs_minor(heading) == expected

    @pytest.mark.parametrize("text", [None, "", "no headings at all here"])
    def test_missing_or_empty_input_yields_0_0(self, text):
        assert retro_scrub.normalize_ffs_minor(text) == "0.0"

    def test_unreleased_only_yields_0_0(self):
        text = Path(FIXTURES / "changelog-unreleased.md").read_text(encoding="utf-8")
        assert retro_scrub.normalize_ffs_minor(text) == "0.0"

    def test_prerelease_heading_is_skipped(self):
        text = "## v2.0.0-rc.1 — prerelease\n\nstuff\n\n## v1.9.0 — stable\n\nmore\n"
        assert retro_scrub.normalize_ffs_minor(text) == "1.9"

    def test_topmost_stable_wins_over_later_headings(self):
        text = Path(FIXTURES / "changelog-release.md").read_text(encoding="utf-8")
        assert retro_scrub.normalize_ffs_minor(text) == "1.4"

    def test_output_matches_major_dot_minor_pattern(self):
        result = retro_scrub.normalize_ffs_minor("## v7.20.3\n")
        assert result == "7.20"

    def test_patch_and_suffix_never_enter_output(self):
        result = retro_scrub.normalize_ffs_minor("## v1.2.99-beta+build.5\n")
        assert "99" not in result
        assert "beta" not in result


# ===========================================================================
# parse_digest — REQ-02 / EDGE-001 / EDGE-002
# ===========================================================================

class TestParseDigest:
    def test_missing_file_yields_no_events(self, tmp_path):
        assert retro_scrub.parse_digest(tmp_path / "does-not-exist.jsonl") == []

    def test_empty_file_yields_no_events(self, tmp_path):
        empty = tmp_path / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        assert retro_scrub.parse_digest(empty) == []

    def test_corrupt_final_line_is_skipped_earlier_rows_kept(self):
        events = retro_scrub.parse_digest(FIXTURES / "corrupt-final-digest.jsonl")
        assert len(events) == 2
        assert {e.get("gate") for e in events} == {"review-gate"}

    def test_corrupt_non_final_line_raises_invalid_digest(self, tmp_path):
        bad = tmp_path / "bad.jsonl"
        bad.write_text(
            '{"script": "digest.sh", "event_class": "retry", '
            '"gate": "review-gate", "exit_code": 0, "ts": "2026-08-11T10:00:00Z", '
            '"duration_seconds": 1.0}\n'
            "{this is not json\n"
            '{"script": "digest.sh", "event_class": "retry", '
            '"gate": "review-gate", "exit_code": 0, "ts": "2026-08-11T10:00:05Z", '
            '"duration_seconds": 1.0}\n',
            encoding="utf-8",
        )
        with pytest.raises(retro_scrub.RetroReject) as ei:
            retro_scrub.parse_digest(bad)
        assert reject_code(ei) == "RETRO:invalid-digest"
        assert "{this is not json" not in str(ei.value)

    def test_utf8_decode_errors_are_tolerated_not_raised(self, tmp_path):
        bad = tmp_path / "bad-utf8.jsonl"
        line = (
            '{"script": "digest.sh", "event_class": "retry", '
            '"gate": "review-gate", "exit_code": 0, "ts": "2026-08-11T10:00:00Z", '
            '"duration_seconds": 1.0, "model_tier": "frontier"}\n'
        ).encode("utf-8")
        # inject an invalid UTF-8 byte sequence into the surrounding bytes; with
        # errors="replace" decoding must not raise UnicodeDecodeError.
        bad.write_bytes(b"\xff\xfe" + line)
        # must not raise
        retro_scrub.parse_digest(bad)

    def test_safe_fixture_parses_to_two_events(self):
        events = retro_scrub.parse_digest(FIXTURES / "safe-digest.jsonl")
        assert len(events) == 2

    def test_hostile_fixture_parses_without_raising(self):
        # parse_digest itself only parses JSONL; scrubbing hostility is
        # validate_payload's job downstream (proven at the CLI layer in bats).
        events = retro_scrub.parse_digest(FIXTURES / "hostile-digest.jsonl")
        assert len(events) == 3
        # The fixture escapes the URL delimiter solely to keep the repository
        # credential scanner clean; JSON decoding restores the exact hostile
        # value exercised by the scrub deny layer.
        assert events[1]["gate"] == "https://user:" + "secret@evil.example.com/hook"

    def test_derivable_and_non_derivable_fixtures_parse(self):
        assert len(retro_scrub.parse_digest(FIXTURES / "derivable-digest.jsonl")) == 3
        assert len(retro_scrub.parse_digest(FIXTURES / "non-derivable-digest.jsonl")) == 3


# ===========================================================================
# Secure state — REQ-14 / AC-014
# ===========================================================================

class TestSecureCacheDir:
    def test_creates_0700_directory(self, tmp_path):
        target = tmp_path / "cache" / "feature-fix-swarm"
        retro_scrub.ensure_cache_dir(target)
        assert target.is_dir()
        assert stat.S_IMODE(target.stat().st_mode) == 0o700

    def test_idempotent_on_already_correct_directory(self, tmp_path):
        target = tmp_path / "cache"
        retro_scrub.ensure_cache_dir(target)
        retro_scrub.ensure_cache_dir(target)  # must not raise
        assert stat.S_IMODE(target.stat().st_mode) == 0o700

    def test_intermediate_symlink_component_cannot_redirect_cache_root(self, tmp_path):
        # WALL-RESIDUAL 2f582fff1b18: O_NOFOLLOW on the final path component
        # alone does not protect against a symlinked INTERMEDIATE component
        # (e.g. a symlinked ~/.cache). We assert the externally observable
        # safety property — an attacker-controlled intermediate symlink must
        # never cause cache state to land inside the symlink's target — rather
        # than a specific implementation technique or exception type.
        attacker_target = tmp_path / "attacker_target"
        attacker_target.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        (home / ".cache").symlink_to(attacker_target, target_is_directory=True)
        cache_dir = home / ".cache" / "feature-fix-swarm"
        try:
            retro_scrub.ensure_cache_dir(cache_dir)
        except Exception:
            pass  # outright refusal is an acceptable secure outcome
        assert list(attacker_target.iterdir()) == []


class TestLoadStateFile:
    def test_missing_file_loads_empty_with_no_warning(self, tmp_path):
        value, warning = retro_scrub.load_state_file(tmp_path / "nope.json", {"empty": True})
        assert value == {"empty": True}
        assert warning is None

    def test_symlink_target_loads_empty_with_typed_warning(self, tmp_path):
        real = tmp_path / "real.json"
        real.write_text('{"granted": true}', encoding="utf-8")
        link = tmp_path / "state.json"
        link.symlink_to(real)
        value, warning = retro_scrub.load_state_file(link, {})
        assert value == {}
        assert warning is not None

    def test_fifo_loads_empty_with_typed_warning(self, tmp_path):
        fifo_path = tmp_path / "state.json"
        os.mkfifo(fifo_path)
        value, warning = retro_scrub.load_state_file(fifo_path, {})
        assert value == {}
        assert warning is not None

    def test_directory_loads_empty_with_typed_warning(self, tmp_path):
        dir_path = tmp_path / "state.json"
        dir_path.mkdir()
        value, warning = retro_scrub.load_state_file(dir_path, {})
        assert value == {}
        assert warning is not None

    def test_group_accessible_file_loads_empty_with_typed_warning(self, tmp_path):
        target = tmp_path / "state.json"
        target.write_text('{"granted": true}', encoding="utf-8")
        target.chmod(0o640)
        value, warning = retro_scrub.load_state_file(target, {})
        assert value == {}
        assert warning is not None

    def test_safe_regular_file_loads_real_content_no_warning(self, tmp_path):
        target = tmp_path / "state.json"
        target.write_text('{"granted": true}', encoding="utf-8")
        target.chmod(0o600)
        value, warning = retro_scrub.load_state_file(target, {})
        assert value == {"granted": True}
        assert warning is None


class TestWriteStateAtomic:
    def test_publishes_0600_file_with_exact_bytes(self, tmp_path):
        target = tmp_path / "state.json"
        payload = b'{"hello": "world"}'
        retro_scrub.write_state_atomic(target, payload)
        assert target.read_bytes() == payload
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_overwrite_replaces_content_atomically(self, tmp_path):
        target = tmp_path / "state.json"
        retro_scrub.write_state_atomic(target, b"first")
        retro_scrub.write_state_atomic(target, b"second")
        assert target.read_bytes() == b"second"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_unsafe_existing_target_is_never_replaced_or_unlinked(self, tmp_path):
        real = tmp_path / "real.json"
        real.write_text("original", encoding="utf-8")
        link = tmp_path / "state.json"
        link.symlink_to(real)
        try:
            retro_scrub.write_state_atomic(link, b"attacker-controlled")
        except Exception:
            pass  # refusing outright is an acceptable secure outcome
        # the symlink must still point at the same, untouched referent
        assert link.is_symlink()
        assert real.read_text(encoding="utf-8") == "original"


class TestRecordLedgerEntry:
    def test_appends_one_json_line_per_call(self, tmp_path):
        ledger = tmp_path / "retro-ledger.jsonl"
        retro_scrub.record_ledger_entry(ledger, {"status": "ok", "fingerprint": "abc", "priority": "P1"})
        retro_scrub.record_ledger_entry(ledger, {"status": "reject", "fingerprint": "def", "priority": "P2"})
        lines = ledger.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert first["fingerprint"] == "abc"
        assert second["fingerprint"] == "def"

    def test_creates_file_mode_0600(self, tmp_path):
        ledger = tmp_path / "retro-ledger.jsonl"
        retro_scrub.record_ledger_entry(ledger, {"status": "ok"})
        assert stat.S_IMODE(ledger.stat().st_mode) == 0o600


# ===========================================================================
# secure_payload_copy — scanner handoff seam
# ===========================================================================

class TestSecurePayloadCopy:
    def test_yields_a_0600_regular_file_with_exact_bytes(self, tmp_path):
        payload = {"schema": "ffs.retro/v1", "findings": []}
        with retro_scrub.secure_payload_copy(payload, directory=tmp_path) as copy_path:
            copy_path = Path(copy_path)
            assert copy_path.is_file()
            assert not copy_path.is_symlink()
            assert stat.S_IMODE(copy_path.stat().st_mode) == 0o600
            on_disk = json.loads(copy_path.read_text(encoding="utf-8"))
            assert on_disk == payload
        # cleaned up after the context manager exits
        assert not Path(copy_path).exists()


# ===========================================================================
# _owner_identity — remote-URL parse feeding the consumer-identity deny gate
# ===========================================================================

class TestOwnerIdentity:
    @pytest.mark.parametrize("remote,expected", [
        ("https://github.com/austinmao/feature-fix-swarm.git", ("austinmao", "feature-fix-swarm")),
        ("https://github.com/austinmao/feature-fix-swarm", ("austinmao", "feature-fix-swarm")),
        ("git@github.com:austinmao/feature-fix-swarm.git", ("austinmao", "feature-fix-swarm")),
        ("https://gitlab.example.com/other/repo.git", ()),
        ("", ()),
    ])
    def test_extracts_owner_repo_tuple(self, remote, expected, monkeypatch):
        class _Done:
            stdout = remote + "\n"
        monkeypatch.setattr(retro_scrub.subprocess, "run", lambda *a, **k: _Done())
        assert retro_scrub._owner_identity() == expected

    def test_git_failure_returns_empty_tuple(self, monkeypatch):
        def _boom(*a, **k):
            raise OSError("no git")
        monkeypatch.setattr(retro_scrub.subprocess, "run", _boom)
        assert retro_scrub._owner_identity() == ()


class TestSecureLedgerParent:
    def test_symlinked_parent_is_rejected_and_target_untouched(self, tmp_path):
        target = tmp_path / "attacker"
        target.mkdir()
        parent = tmp_path / "state"
        parent.symlink_to(target)
        ledger = parent / "retro-ledger.jsonl"
        with pytest.raises(retro_scrub.RetroReject):
            retro_scrub.record_ledger_entry(ledger, {"status": "ok"})
        assert list(target.iterdir()) == []

    def test_regular_file_parent_is_rejected(self, tmp_path):
        parent = tmp_path / "state"
        parent.write_text("not a dir")
        with pytest.raises(retro_scrub.RetroReject):
            retro_scrub.record_ledger_entry(parent / "retro-ledger.jsonl", {"status": "ok"})


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
