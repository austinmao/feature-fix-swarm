"""Compatibility contract for valid upstream segments that need no remapping."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest


def _runtime():
    from run_state.upstream import UpstreamRuntime

    descriptor = os.environ.get("FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR")
    expected = os.environ.get("FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR_SHA256")
    assert descriptor and expected, "UNMET: registered Node runtime descriptor required"
    data = Path(descriptor).read_bytes()
    assert hashlib.sha256(data).hexdigest() == expected
    return UpstreamRuntime.from_manifest(json.loads(data))


@pytest.mark.parametrize(
    "field,value",
    (
        ("project", "feature_one"),
        ("project", "release."),
        ("project", "release-"),
        ("workstream", "feature_one"),
        ("workstream", "release."),
        ("workstream", "release-"),
    ),
    ids=(
        "project-internal-underscore",
        "project-trailing-dot",
        "project-trailing-hyphen",
        "workstream-internal-underscore",
        "workstream-trailing-dot",
        "workstream-trailing-hyphen",
    ),
)
def test_resolver_safe_native_segments_are_accepted_without_remapping(
    tmp_path: Path, field: str, value: str,
) -> None:
    from run_state.upstream import resolve_upstream_binding

    workspace = tmp_path / "workspace"
    (workspace / ".planning").mkdir(parents=True)
    kwargs = {"project": None, "workstream": None}
    kwargs[field] = value
    result = resolve_upstream_binding(
        workspace,
        runtime=_runtime(),
        project=kwargs["project"],
        workstream=kwargs["workstream"],
        session_key="segment-compatibility",
        stored_workstream=None,
    ).as_payload()
    assert result[field] == value
    expected = (
        workspace / ".planning" / value
        if field == "project"
        else workspace / ".planning" / "workstreams" / value
    )
    assert result["planning_root"] == str(expected)
    assert result["session_key"] == "segment-compatibility"
    assert result["effective_session_key"] == "gsd-session-key-segment-compatibility"


@pytest.mark.parametrize("value", [
    "demo-project", "release.", "feature_one", "9" * 160,
    "../x", "a/b", "a..b", "-f", ".h", "x y", "é", "9" * 161, "", "demo\n",
])
def test_env_scope_validator_matches_resolver_and_bridge(value: str) -> None:
    """F34 5.9: a name table checked against `_validate_segment` plus the
    `..` ban (upstream_bridge.cjs's isSegment) must agree with
    host_capabilities' own copy of that rule. Rules out two regexes
    drifting apart -- host_capabilities cannot import run_state
    (layering), so it copies the resolver's segment rule."""
    import host_capabilities
    from run_state.upstream import UpstreamRefused, _validate_segment

    try:
        _validate_segment(value, allow_none=False)
        resolver_accepts = ".." not in value
    except UpstreamRefused:
        resolver_accepts = False

    try:
        host_capabilities._validate_gsd_scope_segment(value, "project")
        host_accepts = True
    except host_capabilities.CapabilityError:
        host_accepts = False

    assert host_accepts == resolver_accepts
