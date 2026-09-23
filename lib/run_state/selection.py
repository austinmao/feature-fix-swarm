"""Pure, closed-schema selected-input manifest validation."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any


_HEX = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SESSION_KEY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema",
        "base_oid",
        "repository_id",
        "entries",
        "required_context",
        "upstream",
    }
)
_ENTRY_FIELDS = frozenset({"operation", "path", "sha256", "git_mode"})
_REQUIRED_CONTEXT_FIELDS = frozenset({"path", "reason"})
_UPSTREAM_FIELDS = frozenset({"project", "workstream", "session_key"})
_HARD_RESERVED_PREFIXES = (
    (".feature-fix-swarm",),
    (".ffs-children",),
    (".planning", "run-state"),
    (".claude", ".credentials.json"),
    (".codex", "auth.json"),
)


class SelectionRefused(ValueError):
    """A selected-input manifest failed closed-schema validation."""

    def __init__(self, code: str, field: str) -> None:
        self.code = code
        self.field = field
        super().__init__(f"{code}:{field}")


@dataclass(frozen=True)
class InputEntry:
    operation: str
    path: str
    sha256: str
    git_mode: str


@dataclass(frozen=True)
class RequiredContext:
    path: str
    reason: str


@dataclass(frozen=True)
class UpstreamScope:
    project: str | None
    workstream: str | None
    session_key: str | None


@dataclass(frozen=True)
class InputSelection:
    schema: str
    base_oid: str
    repository_id: str
    entries: tuple[InputEntry, ...]
    required_context: tuple[RequiredContext, ...]
    upstream: UpstreamScope
    _canonical_manifest_json: str = field(repr=False)
    _digest_payload_json: str = field(repr=False)
    manifest_sha256: str
    input_digest: str

    @property
    def canonical_manifest(self) -> dict[str, Any]:
        """Return a fresh JSON-safe canonical manifest projection."""
        return json.loads(self._canonical_manifest_json)

    @property
    def digest_payload(self) -> dict[str, Any]:
        """Return a fresh JSON-safe digest-payload projection."""
        return json.loads(self._digest_payload_json)


def _refuse(code: str, field: str) -> None:
    raise SelectionRefused(code, field)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _closed_fields(
    value: object,
    expected: frozenset[str],
    field: str,
    *,
    refusal_code: str = "INVALID_SELECTION",
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _refuse(refusal_code, field or "selection")
    actual = set(value)
    if actual != expected:
        differing = sorted(actual ^ expected)
        detail = differing[0] if not field else f"{field}.{differing[0]}"
        _refuse(refusal_code, detail)
    return value


def _portable_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _utf8_length(value: str) -> int | None:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def _safe_path(
    path: object,
    field: str,
    *,
    refusal_code: str = "UNSAFE_SELECTION_PATH",
) -> tuple[str, tuple[str, ...]]:
    if not isinstance(path, str) or not path or _utf8_length(path) is None:
        _refuse(refusal_code, field)
    if unicodedata.normalize("NFC", path) != path:
        _refuse(refusal_code, field)
    if "\x00" in path or "\\" in path or path.startswith("/"):
        _refuse(refusal_code, field)

    components = path.split("/")
    if any(component in {"", ".", ".."} for component in components):
        _refuse(refusal_code, field)
    return path, tuple(component.casefold() for component in components)


def _validate_reserved_prefixes(
    reserved_prefixes: tuple[str, ...],
) -> tuple[tuple[str, ...], ...]:
    if not isinstance(reserved_prefixes, tuple):
        _refuse("INVALID_RESERVED_PREFIXES", "reserved_prefixes")

    prefixes: list[tuple[str, ...]] = []
    for index, prefix in enumerate(reserved_prefixes):
        _, components = _safe_path(
            prefix,
            f"reserved_prefixes[{index}]",
            refusal_code="INVALID_RESERVED_PREFIXES",
        )
        prefixes.append(components)
    return tuple(prefixes)


def _validate_path(
    path: object,
    field: str,
    reserved_prefixes: tuple[tuple[str, ...], ...],
) -> tuple[str, str]:
    path_value, components = _safe_path(path, field)
    if ".git" in components:
        _refuse("RESERVED_SELECTION_PATH", field)
    for prefix in _HARD_RESERVED_PREFIXES + reserved_prefixes:
        if components[: len(prefix)] == prefix:
            _refuse("RESERVED_SELECTION_PATH", field)
    return path_value, "/".join(components)


def _refuse_portable_duplicates(paths: object, collection_field: str) -> None:
    """Refuse aliases before path validation so order cannot alter the code."""
    if not isinstance(paths, list):
        return
    seen: set[str] = set()
    for item in paths:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str):
            continue
        key = _portable_key(path)
        if key in seen:
            _refuse("SELECTION_CONFLICT", collection_field)
        seen.add(key)


def _entry_projection(entry: InputEntry) -> dict[str, str]:
    return {
        "operation": entry.operation,
        "path": entry.path,
        "sha256": entry.sha256,
        "git_mode": entry.git_mode,
    }


def _required_projection(context: RequiredContext) -> dict[str, str]:
    return {"path": context.path, "reason": context.reason}


def _validate_upstream(value: object) -> UpstreamScope:
    upstream = _closed_fields(
        value,
        _UPSTREAM_FIELDS,
        "upstream",
        refusal_code="INVALID_UPSTREAM_SCOPE",
    )
    for name in ("project", "workstream"):
        segment = upstream[name]
        if segment is None:
            continue
        if (
            not isinstance(segment, str)
            or _utf8_length(segment) is None
            or _utf8_length(segment) > 160
            or not _SEGMENT.fullmatch(segment)
            or ".." in segment
        ):
            _refuse("INVALID_UPSTREAM_SCOPE", f"upstream.{name}")

    session_key = upstream["session_key"]
    if session_key is not None and (
        not isinstance(session_key, str)
        or _utf8_length(session_key) is None
        or _utf8_length(session_key) > 160
        or not _SESSION_KEY.fullmatch(session_key)
    ):
        _refuse("INVALID_UPSTREAM_SCOPE", "upstream.session_key")
    return UpstreamScope(
        project=upstream["project"],
        workstream=upstream["workstream"],
        session_key=session_key,
    )


def parse_input_selection(
    value: object,
    *,
    reserved_prefixes: tuple[str, ...] = (),
) -> InputSelection:
    """Validate and canonically project a pure selected-input manifest."""
    top_level = _closed_fields(value, _TOP_LEVEL_FIELDS, "")
    if top_level["schema"] != "ffs.input-selection/v1":
        _refuse("UNSUPPORTED_SELECTION_SCHEMA", "schema")

    base_oid = top_level["base_oid"]
    if not isinstance(base_oid, str) or not _HEX.fullmatch(base_oid):
        _refuse("INVALID_SELECTION", "base_oid")

    repository_id = top_level["repository_id"]
    try:
        normalized_repository_id = str(uuid.UUID(repository_id))
    except (AttributeError, TypeError, ValueError):
        _refuse("INVALID_SELECTION", "repository_id")
    if normalized_repository_id != repository_id:
        _refuse("INVALID_SELECTION", "repository_id")

    reserved = _validate_reserved_prefixes(reserved_prefixes)
    entries_value = top_level["entries"]
    required_value = top_level["required_context"]
    if not isinstance(entries_value, list):
        _refuse("INVALID_SELECTION", "entries")
    if not isinstance(required_value, list):
        _refuse("INVALID_SELECTION", "required_context")
    if len(entries_value) > 4096:
        _refuse("SELECTION_LIMIT_EXCEEDED", "entries")
    if len(required_value) > 4096:
        _refuse("SELECTION_LIMIT_EXCEEDED", "required_context")

    _refuse_portable_duplicates(entries_value, "entries")
    _refuse_portable_duplicates(required_value, "required_context")

    entries: list[tuple[str, InputEntry]] = []
    entry_paths: dict[str, str] = {}
    for index, raw_entry in enumerate(entries_value):
        prefix = f"entries[{index}]"
        entry = _closed_fields(raw_entry, _ENTRY_FIELDS, prefix)
        if not isinstance(entry["operation"], str) or entry["operation"] not in {"copy", "delete"}:
            _refuse("INVALID_SELECTION", f"{prefix}.operation")
        path, path_key = _validate_path(entry["path"], f"{prefix}.path", reserved)
        sha256 = entry["sha256"]
        if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
            _refuse("INVALID_SELECTION", f"{prefix}.sha256")
        git_mode = entry["git_mode"]
        if not isinstance(git_mode, str) or git_mode not in {"100644", "100755"}:
            _refuse("INVALID_SELECTION", f"{prefix}.git_mode")
        entry_paths[path_key] = path
        entries.append((path_key, InputEntry(entry["operation"], path, sha256, git_mode)))

    required_context: list[tuple[str, RequiredContext]] = []
    for index, raw_context in enumerate(required_value):
        prefix = f"required_context[{index}]"
        context = _closed_fields(raw_context, _REQUIRED_CONTEXT_FIELDS, prefix)
        path, path_key = _validate_path(context["path"], f"{prefix}.path", reserved)
        reason = context["reason"]
        if (
            not isinstance(reason, str)
            or not reason
            or _utf8_length(reason) is None
        ):
            _refuse("INVALID_SELECTION", f"{prefix}.reason")
        if _utf8_length(reason) > 1024:
            _refuse("SELECTION_LIMIT_EXCEEDED", f"{prefix}.reason")
        selected_path = entry_paths.get(path_key)
        if selected_path is not None and selected_path != path:
            _refuse("SELECTION_CONFLICT", f"{prefix}.path")
        required_context.append((path_key, RequiredContext(path, reason)))

    upstream = _validate_upstream(top_level["upstream"])
    entries.sort(key=lambda item: item[0])
    required_context.sort(key=lambda item: item[0])
    sorted_entries = tuple(entry for _, entry in entries)
    sorted_context = tuple(context for _, context in required_context)
    upstream_projection = {
        "project": upstream.project,
        "workstream": upstream.workstream,
        "session_key": upstream.session_key,
    }
    manifest = {
        "schema": "ffs.input-selection/v1",
        "base_oid": base_oid,
        "repository_id": normalized_repository_id,
        "entries": [_entry_projection(entry) for entry in sorted_entries],
        "required_context": [
            _required_projection(context) for context in sorted_context
        ],
        "upstream": upstream_projection,
    }
    digest_payload = {
        "schema": "ffs.input-digest/v1",
        "base_oid": base_oid,
        "repository_id": normalized_repository_id,
        "entries": [_entry_projection(entry) for entry in sorted_entries],
        "upstream": upstream_projection,
    }
    manifest_json = _canonical_json(manifest)
    digest_json = _canonical_json(digest_payload)
    return InputSelection(
        schema="ffs.input-selection/v1",
        base_oid=base_oid,
        repository_id=normalized_repository_id,
        entries=sorted_entries,
        required_context=sorted_context,
        upstream=upstream,
        _canonical_manifest_json=manifest_json.decode("utf-8"),
        _digest_payload_json=digest_json.decode("utf-8"),
        manifest_sha256=hashlib.sha256(manifest_json).hexdigest(),
        input_digest=hashlib.sha256(digest_json).hexdigest(),
    )
