"""Independent pure-parser review regressions for M4 input selection."""
from __future__ import annotations

import copy
import hashlib

import pytest


REPOSITORY_ID = "f14f9463-83a2-4c49-8c79-60b0045e684d"
BASE_OID = bytes.fromhex("12345678 90abcdef 12345678 90abcdef 12345678").hex()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _copy(path: str, value: bytes = b"selected\n") -> dict:
    return {
        "operation": "copy",
        "path": path,
        "sha256": _sha(value),
        "git_mode": "100644",
    }


def _manifest(*, entries=None, required_context=None, upstream=None) -> dict:
    return {
        "schema": "ffs.input-selection/v1",
        "base_oid": BASE_OID,
        "repository_id": REPOSITORY_ID,
        "entries": list(entries or []),
        "required_context": list(required_context or []),
        "upstream": upstream or {
            "project": "project",
            "workstream": "workstream",
            "session_key": "session",
        },
    }


def test_selection_manifest_and_digest_projections_do_not_mutate_internal_value() -> None:
    from run_state.selection import parse_input_selection

    selected = parse_input_selection(_manifest(entries=[_copy("src/selected.py")]))
    original_manifest = copy.deepcopy(selected.canonical_manifest)
    original_digest_payload = copy.deepcopy(selected.digest_payload)
    original_manifest_sha256 = selected.manifest_sha256
    original_input_digest = selected.input_digest

    selected.canonical_manifest["entries"][0]["path"] = "src/mutated.py"
    selected.digest_payload["entries"].clear()
    selected.canonical_manifest["upstream"]["project"] = "mutated-project"

    assert selected.canonical_manifest == original_manifest
    assert selected.digest_payload == original_digest_payload
    assert selected.entries[0].path == "src/selected.py"
    assert selected.upstream.project == "project"
    assert selected.manifest_sha256 == original_manifest_sha256
    assert selected.input_digest == original_input_digest


def test_required_context_refuses_a_non_nfc_path_without_an_alias() -> None:
    from run_state.selection import SelectionRefused, parse_input_selection

    manifest = _manifest(required_context=[{
        "path": "docs/Cafe\u0301.md",
        "reason": "required planning context",
    }])
    with pytest.raises(SelectionRefused) as refused:
        parse_input_selection(manifest)
    assert refused.value.code == "UNSAFE_SELECTION_PATH"
    assert refused.value.field == "required_context[0].path"


@pytest.mark.parametrize(
    ("reason", "accepted"),
    [("x" * 1024, True), ("x" * 1025, False), ("é" * 512, True), ("é" * 513, False)],
)
def test_required_context_reason_is_bounded_to_1024_utf8_bytes(
    reason: str, accepted: bool,
) -> None:
    from run_state.selection import SelectionRefused, parse_input_selection

    manifest = _manifest(required_context=[{"path": "docs/context.md", "reason": reason}])
    if accepted:
        assert parse_input_selection(manifest).required_context[0].reason == reason
    else:
        with pytest.raises(SelectionRefused) as refused:
            parse_input_selection(manifest)
        assert refused.value.code == "SELECTION_LIMIT_EXCEEDED"
        assert refused.value.field == "required_context[0].reason"


@pytest.mark.parametrize("field", ["project", "workstream"])
def test_upstream_project_and_workstream_are_bounded_to_160_bytes(field: str) -> None:
    from run_state.selection import SelectionRefused, parse_input_selection

    accepted = {"project": "p", "workstream": "w", "session_key": "ss"}
    accepted[field] = "x" * 160
    assert getattr(parse_input_selection(_manifest(upstream=accepted)).upstream, field) == "x" * 160
    refused_value = dict(accepted)
    refused_value[field] += "x"
    with pytest.raises(SelectionRefused) as refused:
        parse_input_selection(_manifest(upstream=refused_value))
    assert refused.value.code == "INVALID_UPSTREAM_SCOPE"
    assert refused.value.field == f"upstream.{field}"


@pytest.mark.parametrize(
    ("reserved_prefixes", "field"),
    [
        ("private", "reserved_prefixes"),
        (("",), "reserved_prefixes[0]"),
        (("../private",), "reserved_prefixes[0]"),
        (("private//runtime",), "reserved_prefixes[0]"),
        (("Private/Cafe\u0301",), "reserved_prefixes[0]"),
    ],
)
def test_reserved_prefixes_are_an_immutable_tuple_of_canonical_safe_paths(
    reserved_prefixes: object, field: str,
) -> None:
    from run_state.selection import SelectionRefused, parse_input_selection

    with pytest.raises(SelectionRefused) as refused:
        parse_input_selection(_manifest(), reserved_prefixes=reserved_prefixes)
    assert refused.value.code == "INVALID_RESERVED_PREFIXES"
    assert refused.value.field == field


def test_one_character_stable_ascii_session_key_is_valid() -> None:
    from run_state.selection import parse_input_selection

    upstream = {"project": None, "workstream": None, "session_key": "s"}
    assert parse_input_selection(_manifest(upstream=upstream)).upstream.session_key == "s"


@pytest.mark.parametrize(
    "paths",
    [
        ("Cafe\u0301.txt", "CAFÉ.TXT"),
        ("CAFÉ.TXT", "Cafe\u0301.txt"),
    ],
)
def test_nfc_portable_alias_conflict_is_independent_of_request_order(paths: tuple[str, str]) -> None:
    from run_state.selection import SelectionRefused, parse_input_selection

    manifest = _manifest(entries=[_copy(paths[0], b"first"), _copy(paths[1], b"second")])
    with pytest.raises(SelectionRefused) as refused:
        parse_input_selection(manifest)
    assert refused.value.code == "SELECTION_CONFLICT"
    assert refused.value.field == "entries"
