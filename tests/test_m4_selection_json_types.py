"""Independent public JSON-type refusal checks for the pure selection parser."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest


def _manifest() -> dict:
    return {
        "schema": "ffs.input-selection/v1",
        "base_oid": "1" * 40,
        "repository_id": "f14f9463-83a2-4c49-8c79-60b0045e684d",
        "entries": [{
            "operation": "copy",
            "path": "src/example.py",
            "sha256": "2" * 64,
            "git_mode": "100644",
        }],
        "required_context": [{"path": "docs/context.md", "reason": "shared context"}],
        "upstream": {"project": "project", "workstream": "stream", "session_key": "session"},
    }


@pytest.mark.parametrize("field", ["operation", "git_mode"])
@pytest.mark.parametrize("value", [[], {}], ids=["array", "object"])
def test_json_containers_in_scalar_entry_fields_return_typed_refusal(
    field: str, value: object,
) -> None:
    from run_state.selection import SelectionRefused, parse_input_selection

    manifest = _manifest()
    manifest["entries"][0][field] = copy.deepcopy(value)
    before = copy.deepcopy(manifest)
    with pytest.raises(SelectionRefused) as refused:
        parse_input_selection(manifest)
    assert refused.value.code == "INVALID_SELECTION"
    assert refused.value.field == f"entries[0].{field}"
    assert manifest == before


@pytest.mark.parametrize(
    ("section", "field", "code", "diagnostic"),
    [
        ("entries", "path", "UNSAFE_SELECTION_PATH", "entries[0].path"),
        ("required_context", "path", "UNSAFE_SELECTION_PATH", "required_context[0].path"),
        ("required_context", "reason", "INVALID_SELECTION", "required_context[0].reason"),
        ("upstream", "project", "INVALID_UPSTREAM_SCOPE", "upstream.project"),
        ("upstream", "workstream", "INVALID_UPSTREAM_SCOPE", "upstream.workstream"),
        ("upstream", "session_key", "INVALID_UPSTREAM_SCOPE", "upstream.session_key"),
    ],
)
def test_escaped_unpaired_surrogates_return_typed_refusal(
    section: str, field: str, code: str, diagnostic: str,
) -> None:
    from run_state.selection import SelectionRefused, parse_input_selection

    # A JSON parser accepts this escape, but UTF-8 manifest bytes cannot encode it.
    value = json.loads('"prefix\\ud800suffix"')
    manifest = _manifest()
    target = manifest[section] if section == "upstream" else manifest[section][0]
    target[field] = value
    before = copy.deepcopy(manifest)
    with pytest.raises(SelectionRefused) as refused:
        parse_input_selection(manifest)
    assert refused.value.code == code
    assert refused.value.field == diagnostic
    assert manifest == before


def test_valid_non_ascii_paths_and_reasons_keep_their_utf8_digest() -> None:
    from run_state.selection import parse_input_selection

    manifest = _manifest()
    manifest["entries"][0]["path"] = "src/café.py"
    manifest["required_context"][0] = {"path": "docs/日本語.md", "reason": "共有文脈"}
    selected = parse_input_selection(manifest)
    canonical = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    assert selected.canonical_manifest == manifest
    assert selected.manifest_sha256 == hashlib.sha256(canonical).hexdigest()
