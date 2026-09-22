"""Independent M4 workspace selection and recovery acceptance contracts.

Prospective M4 imports stay inside test bodies so the accepted M3 suite still
collects before the implementation exists. Every Git operation is confined to
a disposable repository created by the test.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import textwrap
import unicodedata
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "lib"
INHERITED = (
    "FFS_RUN_ID", "GSD_RUN_ID", "GSD_RESUME", "GSD_WORKSTREAM",
    "GSD_PROJECT", "GSD_SESSION_KEY", "CLAUDE_CONFIG_DIR", "GSD_HOME",
)

REPOSITORY_ID = "f14f9463-83a2-4c49-8c79-60b0045e684d"
BASE_SHA1 = bytes.fromhex("12345678 90abcdef 12345678 90abcdef 12345678").hex()
BASE_SHA256 = "sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef".removeprefix("sha256:")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _copy(path: str, data: bytes, mode: str = "100644") -> dict:
    return {"operation": "copy", "path": path, "sha256": _sha(data), "git_mode": mode}


def _delete(path: str, base_data: bytes, mode: str = "100644") -> dict:
    return {"operation": "delete", "path": path, "sha256": _sha(base_data), "git_mode": mode}


def _pure_manifest(*, entries=None, required_context=None, upstream=None, **changes) -> dict:
    manifest = {
        "schema": "ffs.input-selection/v1",
        "base_oid": BASE_SHA1,
        "repository_id": REPOSITORY_ID,
        "entries": list(entries or []),
        "required_context": list(required_context or []),
        "upstream": upstream or {
            "project": "fixture-project",
            "workstream": "fixture-workstream",
            "session_key": "fixture-session",
        },
    }
    manifest.update(changes)
    return manifest


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


@pytest.mark.parametrize("base_oid", [BASE_SHA1, BASE_SHA256])
def test_input_selection_accepts_complete_sha1_or_sha256_identity_and_is_pure(
    monkeypatch: pytest.MonkeyPatch, base_oid: str,
) -> None:
    from run_state.selection import parse_input_selection

    def filesystem_forbidden(*_args, **_kwargs):
        raise AssertionError("the pure selection parser attempted filesystem I/O")

    monkeypatch.setattr("builtins.open", filesystem_forbidden)
    monkeypatch.setattr(Path, "open", filesystem_forbidden)
    monkeypatch.setattr(Path, "stat", filesystem_forbidden)
    entries = [
        _delete("src/removed.txt", b"old\n"),
        _copy("src/selected.sh", b"selected\n", "100755"),
    ]
    required = [{"path": ".claude/rules/CLAUDE.md", "reason": "shared rules"}]
    manifest = _pure_manifest(
        base_oid=base_oid, entries=entries, required_context=required,
    )
    before = _canonical_json(manifest)

    selection = parse_input_selection(manifest)

    assert selection.schema == "ffs.input-selection/v1"
    assert selection.base_oid == base_oid
    assert selection.repository_id == str(uuid.UUID(REPOSITORY_ID))
    assert [(item.operation, item.path, item.sha256, item.git_mode)
            for item in selection.entries] == [
        (item["operation"], item["path"], item["sha256"], item["git_mode"])
        for item in entries
    ]
    assert [(item.path, item.reason) for item in selection.required_context] == [
        (".claude/rules/CLAUDE.md", "shared rules"),
    ]
    assert (
        selection.upstream.project,
        selection.upstream.workstream,
        selection.upstream.session_key,
    ) == ("fixture-project", "fixture-workstream", "fixture-session")
    assert selection.canonical_manifest == json.loads(before)
    assert selection.manifest_sha256 == _sha(before)
    assert _canonical_json(manifest) == before


def test_input_selection_digest_is_versioned_material_and_order_stable() -> None:
    from run_state.selection import parse_input_selection

    first = _pure_manifest(entries=[
        _delete("src/removed.txt", b"old\n"),
        _copy("src/selected.sh", b"selected\n", "100755"),
    ])
    reordered = _pure_manifest(entries=list(reversed(first["entries"])))
    reason_only = json.loads(json.dumps(first))
    reason_only["required_context"] = [{"path": "docs/context.md", "reason": "operator note"}]
    changed_mode = json.loads(json.dumps(first))
    changed_mode["entries"][1]["git_mode"] = "100644"
    changed_upstream = json.loads(json.dumps(first))
    changed_upstream["upstream"]["session_key"] = "other-session"

    selected = parse_input_selection(first)
    assert [entry.path for entry in parse_input_selection(reordered).entries] == [
        "src/removed.txt", "src/selected.sh",
    ]
    assert selected.digest_payload == {
        "schema": "ffs.input-digest/v1",
        "base_oid": BASE_SHA1,
        "repository_id": REPOSITORY_ID,
        "entries": first["entries"],
        "upstream": first["upstream"],
    }
    assert selected.input_digest == _sha(_canonical_json(selected.digest_payload))
    assert parse_input_selection(reordered).input_digest == selected.input_digest
    assert parse_input_selection(reason_only).input_digest == selected.input_digest
    assert parse_input_selection(reason_only).manifest_sha256 != selected.manifest_sha256
    assert parse_input_selection(changed_mode).input_digest != selected.input_digest
    assert parse_input_selection(changed_upstream).input_digest != selected.input_digest


def test_input_selection_versioned_empty_overlay_has_a_material_digest() -> None:
    from run_state.selection import parse_input_selection

    selected = parse_input_selection(_pure_manifest())
    assert selected.entries == ()
    assert selected.required_context == ()
    assert selected.digest_payload["schema"] == "ffs.input-digest/v1"
    assert selected.input_digest == _sha(_canonical_json(selected.digest_payload))


@pytest.mark.parametrize(
    ("change", "code", "field"),
    [
        ({"schema": "ffs.input-selection/v2"}, "UNSUPPORTED_SELECTION_SCHEMA", "schema"),
        ({"base_oid": "1234"}, "INVALID_SELECTION", "base_oid"),
        ({"base_oid": "A" * 40}, "INVALID_SELECTION", "base_oid"),
        ({"repository_id": "repository"}, "INVALID_SELECTION", "repository_id"),
        ({"entries": "src/file.py"}, "INVALID_SELECTION", "entries"),
        ({"required_context": {}}, "INVALID_SELECTION", "required_context"),
        ({"unexpected": True}, "INVALID_SELECTION", "unexpected"),
    ],
)
def test_input_selection_rejects_open_or_partial_top_level_shapes(
    change: dict, code: str, field: str,
) -> None:
    from run_state.selection import SelectionRefused, parse_input_selection

    manifest = _pure_manifest()
    manifest.update(change)
    with pytest.raises(SelectionRefused) as refused:
        parse_input_selection(manifest)
    assert refused.value.code == code
    assert refused.value.field == field


@pytest.mark.parametrize(
    "field",
    ["schema", "base_oid", "repository_id", "entries", "required_context", "upstream"],
)
def test_input_selection_requires_every_top_level_field(field: str) -> None:
    from run_state.selection import SelectionRefused, parse_input_selection

    manifest = _pure_manifest()
    del manifest[field]
    with pytest.raises(SelectionRefused) as refused:
        parse_input_selection(manifest)
    assert refused.value.code == "INVALID_SELECTION"
    assert refused.value.field == field


@pytest.mark.parametrize(
    ("entry", "field"),
    [
        ({"operation": "move", "path": "src/x", "sha256": "1" * 64,
          "git_mode": "100644"}, "entries[0].operation"),
        ({"operation": "copy", "path": "src/x", "sha256": "1" * 63,
          "git_mode": "100644"}, "entries[0].sha256"),
        ({"operation": "copy", "path": "src/x", "sha256": "A" * 64,
          "git_mode": "100644"}, "entries[0].sha256"),
        ({"operation": "copy", "path": "src/x", "sha256": "1" * 64,
          "git_mode": "100600"}, "entries[0].git_mode"),
        ({"operation": "copy", "path": "src/x", "sha256": "1" * 64,
          "git_mode": "100644", "size": 1}, "entries[0].size"),
    ],
)
def test_input_selection_rejects_incomplete_or_open_entry_shapes(entry: dict, field: str) -> None:
    from run_state.selection import SelectionRefused, parse_input_selection

    with pytest.raises(SelectionRefused) as refused:
        parse_input_selection(_pure_manifest(entries=[entry]))
    assert refused.value.code == "INVALID_SELECTION"
    assert refused.value.field == field


@pytest.mark.parametrize(
    ("required", "field"),
    [
        ({"path": "docs/context.md"}, "required_context[0].reason"),
        ({"reason": "context"}, "required_context[0].path"),
        ({"path": "docs/context.md", "reason": ""}, "required_context[0].reason"),
        ({"path": "docs/context.md", "reason": "context", "optional": True},
         "required_context[0].optional"),
    ],
)
def test_input_selection_rejects_partial_or_open_required_context_shapes(
    required: dict, field: str,
) -> None:
    from run_state.selection import SelectionRefused, parse_input_selection

    with pytest.raises(SelectionRefused) as refused:
        parse_input_selection(_pure_manifest(required_context=[required]))
    assert refused.value.code == "INVALID_SELECTION"
    assert refused.value.field == field


@pytest.mark.parametrize(
    "path",
    [
        "/absolute.txt", "../escape.txt", "src/../../escape.txt", "src//file.txt",
        "src\\file.txt", "src/./file.txt", "src/\x00file.txt", "Cafe\u0301.txt",
    ],
)
def test_input_selection_rejects_noncanonical_or_traversing_paths(path: str) -> None:
    from run_state.selection import SelectionRefused, parse_input_selection

    with pytest.raises(SelectionRefused) as refused:
        parse_input_selection(_pure_manifest(entries=[_copy(path, b"x")]))
    assert refused.value.code == "UNSAFE_SELECTION_PATH"
    assert refused.value.field == "entries[0].path"


@pytest.mark.parametrize(
    "path",
    [
        ".git", "src/.GiT/config", ".feature-fix-swarm/control.sqlite3",
        ".planning/run-state/control.sqlite3", ".claude/.credentials.json",
        ".codex/auth.json",
    ],
)
def test_input_selection_hard_denies_control_and_private_runtime_paths(path: str) -> None:
    from run_state.selection import SelectionRefused, parse_input_selection

    with pytest.raises(SelectionRefused) as refused:
        parse_input_selection(_pure_manifest(entries=[_copy(path, b"x")]))
    assert refused.value.code == "RESERVED_SELECTION_PATH"
    assert refused.value.field == "entries[0].path"


def test_input_selection_applies_path_safety_to_required_context() -> None:
    from run_state.selection import SelectionRefused, parse_input_selection

    with pytest.raises(SelectionRefused) as refused:
        parse_input_selection(_pure_manifest(required_context=[{
            "path": ".codex/auth.json", "reason": "must never enter a snapshot",
        }]))
    assert refused.value.code == "RESERVED_SELECTION_PATH"
    assert refused.value.field == "required_context[0].path"


def test_input_selection_allows_required_context_to_name_the_exact_selected_path() -> None:
    from run_state.selection import parse_input_selection

    manifest = _pure_manifest(
        entries=[_copy(".claude/rules/CLAUDE.md", b"selected rules\n")],
        required_context=[{
            "path": ".claude/rules/CLAUDE.md",
            "reason": "required shared context was explicitly selected",
        }],
    )
    selected = parse_input_selection(manifest)
    assert selected.entries[0].path == selected.required_context[0].path


@pytest.mark.parametrize(
    ("required_context", "field"),
    [
        ([
            {"path": "docs/Context.md", "reason": "first"},
            {"path": "DOCS/context.MD", "reason": "second"},
        ], "required_context"),
        ([{
            "path": ".CLAUDE/rules/claude.md",
            "reason": "portable alias differs from the selected spelling",
        }], "required_context[0].path"),
    ],
)
def test_input_selection_rejects_required_context_portable_aliases(
    required_context: list[dict], field: str,
) -> None:
    from run_state.selection import SelectionRefused, parse_input_selection

    manifest = _pure_manifest(
        entries=[_copy(".claude/rules/CLAUDE.md", b"selected rules\n")],
        required_context=required_context,
    )
    with pytest.raises(SelectionRefused) as refused:
        parse_input_selection(manifest)
    assert refused.value.code == "SELECTION_CONFLICT"
    assert refused.value.field == field


def test_input_selection_configurable_reserved_prefixes_do_not_ban_legitimate_source_names() -> None:
    from run_state.selection import SelectionRefused, parse_input_selection

    manifest = _pure_manifest(entries=[
        _copy("auth/validator.py", b"safe source\n"),
        _copy(".claude/rules/CLAUDE.md", b"shared rules\n"),
    ])
    accepted = parse_input_selection(manifest, reserved_prefixes=("private/runtime",))
    assert [entry.path for entry in accepted.entries] == [
        ".claude/rules/CLAUDE.md", "auth/validator.py",
    ]
    with pytest.raises(SelectionRefused) as refused:
        parse_input_selection(manifest, reserved_prefixes=("auth",))
    assert refused.value.code == "RESERVED_SELECTION_PATH"
    assert refused.value.field == "entries[0].path"


@pytest.mark.parametrize(
    "entries",
    [
        [_copy("src/Name.txt", b"a"), _delete("SRC/name.TXT", b"b")],
        [_copy("café.txt", b"a"), _copy("CAFE\u0301.TXT", b"b")],
        [_copy("src/item.txt", b"a"), _copy("src/item.txt", b"a")],
    ],
)
def test_input_selection_rejects_portable_copy_delete_aliases(entries: list[dict]) -> None:
    from run_state.selection import SelectionRefused, parse_input_selection

    with pytest.raises(SelectionRefused) as refused:
        parse_input_selection(_pure_manifest(entries=entries))
    assert refused.value.code == "SELECTION_CONFLICT"
    assert refused.value.field == "entries"


def test_input_selection_enforces_bounded_entries_and_required_context() -> None:
    from run_state.selection import SelectionRefused, parse_input_selection

    entries = [_copy(f"src/{index:04d}.txt", str(index).encode()) for index in range(4097)]
    with pytest.raises(SelectionRefused) as refused:
        parse_input_selection(_pure_manifest(entries=entries))
    assert refused.value.code == "SELECTION_LIMIT_EXCEEDED"
    assert refused.value.field == "entries"

    required = [
        {"path": f"docs/{index:04d}.md", "reason": "required context"}
        for index in range(4097)
    ]
    with pytest.raises(SelectionRefused) as refused:
        parse_input_selection(_pure_manifest(required_context=required))
    assert refused.value.code == "SELECTION_LIMIT_EXCEEDED"
    assert refused.value.field == "required_context"


@pytest.mark.parametrize(
    ("upstream", "field"),
    [
        ({"project": "../project", "workstream": "ws", "session_key": "session"},
         "upstream.project"),
        ({"project": "project", "workstream": "nested/ws", "session_key": "session"},
         "upstream.workstream"),
        ({"project": "project", "workstream": "ws", "session_key": "x" * 161},
         "upstream.session_key"),
        ({"project": "project", "workstream": "ws", "session_key": "session", "root": "/tmp"},
         "upstream.root"),
    ],
)
def test_input_selection_refuses_upstream_sanitization_or_truncation(
    upstream: dict, field: str,
) -> None:
    from run_state.selection import SelectionRefused, parse_input_selection

    with pytest.raises(SelectionRefused) as refused:
        parse_input_selection(_pure_manifest(upstream=upstream))
    assert refused.value.code == "INVALID_UPSTREAM_SCOPE"
    assert refused.value.field == field


@pytest.mark.parametrize("project", [None, "fixture-project"])
@pytest.mark.parametrize("workstream", [None, "fixture-workstream"])
def test_input_selection_accepts_explicit_flat_upstream_scope(
    project: str | None, workstream: str | None,
) -> None:
    from run_state.selection import parse_input_selection

    upstream = {
        "project": project,
        "workstream": workstream,
        "session_key": "fixture-session",
    }
    selected = parse_input_selection(_pure_manifest(upstream=upstream))
    assert selected.upstream.project == project
    assert selected.upstream.workstream == workstream
    assert selected.upstream.session_key == "fixture-session"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project", "needs sanitize"),
        ("project", "project..escape"),
        ("workstream", "wörkstream"),
        ("workstream", "work..stream"),
        ("session_key", "session key"),
        ("session_key", "séssion"),
        ("session_key", "_session"),
        ("session_key", "session_"),
    ],
)
def test_input_selection_refuses_upstream_values_needing_mapping(
    field: str, value: object,
) -> None:
    from run_state.selection import SelectionRefused, parse_input_selection

    upstream = {
        "project": "fixture-project",
        "workstream": "fixture-workstream",
        "session_key": "fixture-session",
    }
    upstream[field] = value
    with pytest.raises(SelectionRefused) as refused:
        parse_input_selection(_pure_manifest(upstream=upstream))
    assert refused.value.code == "INVALID_UPSTREAM_SCOPE"
    assert refused.value.field == f"upstream.{field}"


def test_input_selection_preserves_unrequested_session_key() -> None:
    from run_state.selection import parse_input_selection

    upstream = {
        "project": "fixture-project", "workstream": "fixture-workstream",
        "session_key": None,
    }
    selection = parse_input_selection(_pure_manifest(upstream=upstream))
    assert selection.upstream.session_key is None
    assert selection.canonical_manifest["upstream"] == upstream
    # Derivation is performed by the versioned resolver from the durable run ID,
    # not by inventing a session or filesystem path in the input descriptor.


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["rtk", "proxy", "git", *args], cwd=cwd, check=check,
        capture_output=True, text=True,
    )


def _git_text(*args: str, cwd: Path) -> str:
    return _git(*args, cwd=cwd).stdout.strip()


def _env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    for key in INHERITED:
        env.pop(key, None)
    home = tmp_path / "home"
    temp = home / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    env.update(
        HOME=str(home), TMPDIR=str(temp),
        PYTHONPATH=f"{LIB}:{env.get('PYTHONPATH', '')}",
        PYTHONDONTWRITEBYTECODE="1",
    )
    return env


def _repository(tmp_path: Path) -> Path:
    primary = tmp_path / "primary"
    primary.mkdir()
    _git("init", "-q", cwd=primary)
    _git("config", "user.email", "m4-acceptance@example.test", cwd=primary)
    _git("config", "user.name", "M4 Acceptance", cwd=primary)
    (primary / "src").mkdir()
    (primary / "src" / "selected.sh").write_bytes(b"base-selected\n")
    (primary / "src" / "delete.txt").write_bytes(b"base-delete\n")
    (primary / "src" / "unrelated.txt").write_bytes(b"base-unrelated\n")
    (primary / ".gitignore").write_text(".planning/local-required.json\n")
    _git("add", ".gitignore", "src", cwd=primary)
    _git("commit", "-qm", "fixture base", cwd=primary)
    return primary


def _owner(tmp_path: Path, primary: Path, run_id: str):
    import run_state.workspace as workspace_module
    from run_state.cli import main
    from run_state.ownership import Ownership

    class FixturePreparationBoundary(BaseException):
        pass

    captured = {}

    def stop_before_git(store, token, **kwargs):
        captured.update(store=store, token=token, workspace=Path(kwargs["workspace"]))
        raise FixturePreparationBoundary

    original = workspace_module.begin_workspace_preparation
    prior_cwd = Path.cwd()
    prior_env = dict(os.environ)
    fixture_env = _env(tmp_path)
    workspace_module.begin_workspace_preparation = stop_before_git
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        os.environ.clear()
        os.environ.update(fixture_env)
        os.chdir(primary)
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                main([
                    "start", "--skill", "fix", "--objective", f"objective-{run_id}",
                    "--activity", "plan", "--run-id", run_id, "--scope", "m4-fixture",
                    "--json", "--state-root", str(tmp_path / "authority"),
                ])
        except FixturePreparationBoundary:
            pass
        except BaseException as error:
            pytest.fail(
                "fixture CLI failed before the preparation boundary: "
                f"{type(error).__name__}: {error}; "
                f"stdout={stdout.getvalue()!r}; stderr={stderr.getvalue()!r}; "
                f"captured_fields={sorted(captured)}"
            )
        else:
            pytest.fail(
                "fixture CLI returned before the preparation boundary; "
                f"stdout={stdout.getvalue()!r}; stderr={stderr.getvalue()!r}; "
                f"captured_fields={sorted(captured)}"
            )
    finally:
        workspace_module.begin_workspace_preparation = original
        os.chdir(prior_cwd)
        os.environ.clear()
        os.environ.update(prior_env)
    token = captured["token"]
    owner = Ownership(run_id=token.run_id, generation=token.generation, token=token)
    return captured["store"], owner, captured["workspace"], token.repository_id


def _selection(primary: Path, repository_id: str, *, upstream=None, entries=None,
               required_context=None) -> dict:
    return {
        "schema": "ffs.input-selection/v1",
        "base_oid": _git_text("rev-parse", "HEAD", cwd=primary),
        "repository_id": repository_id,
        "entries": list(entries or []),
        "required_context": list(required_context or []),
        "upstream": upstream or {
            "project": "fixture-project",
            "workstream": "fixture-workstream",
            "session_key": "fixture-session",
        },
    }


def test_selected_snapshot_applies_only_declared_bytes_delete_and_mode(tmp_path: Path) -> None:
    from run_state.workspace import (
        begin_workspace_preparation, parse_input_selection, prepare_workspace,
        snapshot_inputs,
    )

    primary = _repository(tmp_path)
    store, owner, workspace, repository_id = _owner(tmp_path, primary, "selected-snapshot")
    selected = b"selected-dirty\n"
    unrelated = b"unrelated-dirty\n"
    (primary / "src" / "selected.sh").write_bytes(selected)
    (primary / "src" / "selected.sh").chmod(0o755)
    (primary / "src" / "delete.txt").unlink()
    (primary / "src" / "unrelated.txt").write_bytes(unrelated)
    (primary / "untracked.txt").write_bytes(b"unrelated-untracked\n")
    primary_before = _git("status", "--porcelain=v1", cwd=primary).stdout
    manifest = _selection(primary, repository_id, entries=[
        _copy("src/selected.sh", selected, "100755"),
        _delete("src/delete.txt", b"base-delete\n"),
    ])
    selection = parse_input_selection(manifest)
    staging = tmp_path / "private-selection-staging"
    snapshot = snapshot_inputs(primary, selection, staging)
    preparation = begin_workspace_preparation(
        store, owner.token, run_id=owner.run_id, workspace=workspace,
        branch=f"ffs/runs/{owner.run_id}", base_commit=manifest["base_oid"],
        selected_input_manifest=snapshot.manifest, repository_path=primary,
    )
    ready = prepare_workspace(store, owner.token, preparation, input_snapshot=snapshot)

    assert ready.ready is True
    assert (workspace / "src" / "selected.sh").read_bytes() == selected
    assert stat.S_IMODE((workspace / "src" / "selected.sh").stat().st_mode) == 0o755
    assert not (workspace / "src" / "delete.txt").exists()
    assert (workspace / "src" / "unrelated.txt").read_bytes() == b"base-unrelated\n"
    assert not (workspace / "untracked.txt").exists()
    assert _git("status", "--porcelain=v1", cwd=primary).stdout == primary_before
    assert (primary / "src" / "unrelated.txt").read_bytes() == unrelated
    assert snapshot.manifest["schema"] == "ffs.input-snapshot/v1"
    assert len(snapshot.input_digest) == len(snapshot.selection_manifest_hash) == 64
    assert ready.input_digest == snapshot.input_digest
    assert ready.selected_manifest_hash == snapshot.selection_manifest_hash


@pytest.mark.parametrize("operation", ["copy", "delete"])
def test_snapshot_application_refuses_committed_base_symlink_destination_escape(
    tmp_path: Path, operation: str,
) -> None:
    from run_state.workspace import (
        WorkspaceRefused, apply_input_snapshot, begin_workspace_preparation,
        parse_input_selection, snapshot_inputs,
    )

    primary = _repository(tmp_path)
    safe = primary / "safe"
    safe.mkdir()
    (safe / "item.txt").write_bytes(b"base-item\n")
    _git("add", "safe/item.txt", cwd=primary)
    _git("commit", "-qm", "safe item", cwd=primary)
    selected = b"selected-item\n"
    (safe / "item.txt").write_bytes(selected)
    store, owner, workspace, repository_id = _owner(tmp_path, primary, f"destination-{operation}")
    entry = (
        _copy("safe/item.txt", selected)
        if operation == "copy" else _delete("safe/item.txt", b"base-item\n")
    )
    manifest = _selection(primary, repository_id, entries=[entry])
    snapshot = snapshot_inputs(
        primary, parse_input_selection(manifest), tmp_path / "snapshot-staging",
    )
    preparation = begin_workspace_preparation(
        store, owner.token, run_id=owner.run_id, workspace=workspace,
        branch=f"ffs/runs/{owner.run_id}", base_commit=manifest["base_oid"],
        selected_input_manifest=snapshot.manifest, repository_path=primary,
    )
    _git(
        "worktree", "add", "--lock", "--reason", f"ffs-preparation:{preparation.id}",
        "-q", "-b", preparation.branch, str(workspace), preparation.base_commit,
        cwd=primary,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "item.txt"
    sentinel.write_bytes(b"outside-sentinel\n")
    for child in (workspace / "safe").iterdir():
        child.unlink()
    (workspace / "safe").rmdir()
    (workspace / "safe").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceRefused) as refused:
        apply_input_snapshot(store, owner.token, preparation.id, snapshot)
    assert refused.value.code == "UNSAFE_SELECTION_PATH"
    assert sentinel.read_bytes() == b"outside-sentinel\n"
    assert not (outside / "escape-write.txt").exists()


def test_gitignored_required_context_is_never_silently_omitted(tmp_path: Path) -> None:
    from run_context import register_repository, resolve_repository
    from run_state.ownership import ControlStore
    from run_state.workspace import WorkspaceRefused, parse_input_selection, snapshot_inputs

    primary = _repository(tmp_path)
    required = primary / ".planning" / "local-required.json"
    required.parent.mkdir()
    required.write_bytes(b'{"required":true}\n')
    state_root = tmp_path / "registered-authority"
    store = ControlStore(state_root / "control.sqlite3")
    repository_id = register_repository(store, resolve_repository(primary), state_root)
    manifest = _selection(
        primary, repository_id, required_context=[{
            "path": ".planning/local-required.json",
            "reason": "feature planning context",
        }],
    )
    with pytest.raises(WorkspaceRefused) as refused:
        snapshot_inputs(
            primary, parse_input_selection(manifest), tmp_path / "selection-staging",
        )
    assert refused.value.code == "INPUT_SELECTION_REQUIRED"
    assert refused.value.candidates == [".planning/local-required.json"]
    assert required.read_bytes() == b'{"required":true}\n'


@pytest.mark.parametrize(
    "entries",
    [
        [_copy(".GiT/config", b"x")],
        [_copy(".feature-fix-swarm/control.sqlite3", b"x")],
        [_copy("Cafe\u0301.txt", b"a"), _delete("CAFÉ.TXT", b"b")],
        [_copy("src/Name.txt", b"a"), _copy("src/name.txt", b"b")],
    ],
)
def test_selection_uses_portable_nfc_casefold_reserved_and_collision_rules(
    tmp_path: Path, entries: list[dict],
) -> None:
    from run_state.workspace import WorkspaceRefused, parse_input_selection

    primary = _repository(tmp_path)
    manifest = _selection(primary, REPOSITORY_ID, entries=entries)
    before = json.dumps(manifest, sort_keys=True)
    with pytest.raises(WorkspaceRefused) as refused:
        parse_input_selection(manifest)
    expected = "UNSAFE_SELECTION_PATH" if len(entries) == 1 else "SELECTION_CONFLICT"
    assert refused.value.code == expected
    assert json.dumps(manifest, sort_keys=True) == before
    if len(entries) > 1:
        assert unicodedata.normalize("NFC", entries[0]["path"]).casefold() == unicodedata.normalize(
            "NFC", entries[1]["path"]
        ).casefold()


@pytest.mark.parametrize("entrypoint", ["publish", "recover", "adopt"])
def test_incomplete_snapshot_never_reaches_ready_through_any_public_path(
    tmp_path: Path, entrypoint: str,
) -> None:
    from run_state.workspace import (
        WorkspaceRefused, adopt_workspace_preparation_fence,
        begin_workspace_preparation, inspect_workspace, parse_input_selection,
        publish_workspace_ready, recover_workspace_preparation, snapshot_inputs,
    )

    primary = _repository(tmp_path)
    selected = b"complete-selected\n"
    (primary / "src" / "selected.sh").write_bytes(selected)
    store, owner, workspace, repository_id = _owner(tmp_path, primary, f"partial-{entrypoint}")
    manifest = _selection(primary, repository_id, entries=[
        _copy("src/selected.sh", selected),
        _delete("src/delete.txt", b"base-delete\n"),
    ])
    snapshot = snapshot_inputs(
        primary, parse_input_selection(manifest), tmp_path / "selection-staging",
    )
    preparation = begin_workspace_preparation(
        store, owner.token, run_id=owner.run_id, workspace=workspace,
        branch=f"ffs/runs/{owner.run_id}", base_commit=manifest["base_oid"],
        selected_input_manifest=snapshot.manifest, repository_path=primary,
    )
    _git(
        "worktree", "add", "--lock", "--reason", f"ffs-preparation:{preparation.id}",
        "-q", "-b", preparation.branch, str(workspace), preparation.base_commit,
        cwd=primary,
    )
    # Model a real interrupted overlay: one selected copy landed, the selected
    # deletion and durable completion marker did not.
    (workspace / "src" / "selected.sh").write_bytes(selected)
    action = {
        "publish": lambda: publish_workspace_ready(store, owner.token, preparation.id),
        "recover": lambda: recover_workspace_preparation(store, owner.token, preparation.id),
        "adopt": lambda: adopt_workspace_preparation_fence(
            store, owner.token, preparation.id,
        ),
    }[entrypoint]
    with pytest.raises(WorkspaceRefused) as refused:
        action()
    assert refused.value.code == "SNAPSHOT_INCOMPLETE"
    assert inspect_workspace(store, preparation.id).ready is False
    assert (workspace / "src" / "delete.txt").read_bytes() == b"base-delete\n"


def test_legacy_empty_selection_keeps_m3_digest_and_ready_behavior(tmp_path: Path) -> None:
    from run_state.workspace import begin_workspace_preparation, prepare_workspace

    primary = _repository(tmp_path)
    store, owner, workspace, _repository_id = _owner(tmp_path, primary, "legacy-empty")
    base = _git_text("rev-parse", "HEAD", cwd=primary)
    legacy = {"entries": []}
    expected = _sha(json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode())
    preparation = begin_workspace_preparation(
        store, owner.token, run_id=owner.run_id, workspace=workspace,
        branch=f"ffs/runs/{owner.run_id}", base_commit=base,
        selected_input_manifest=legacy, repository_path=primary,
    )
    ready = prepare_workspace(store, owner.token, preparation)
    assert ready.ready is True
    assert ready.selected_manifest_hash == expected
    assert ready.input_digest == expected
    assert _git_text("rev-parse", "HEAD", cwd=workspace) == base


def _cli(state_root: Path, cwd: Path, *args: str, env: dict[str, str]):
    return subprocess.run(
        [sys.executable, "-m", "run_state.cli", *args, "--state-root", str(state_root)],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=30,
    )


def test_post_selection_crash_nonresume_has_no_reclaim_ready_or_lock_effect(
    tmp_path: Path,
) -> None:
    from run_context import repository_identity, resolve_repository
    from run_state.ownership import ControlStore

    primary = _repository(tmp_path)
    state_root = tmp_path / "authority"
    env = _env(tmp_path)
    run_id = "latest-revision-crash"
    objective = "latest revision crash"
    started = _cli(
        state_root, primary, "start", "--skill", "fix", "--objective", objective,
        "--activity", "plan", "--run-id", run_id, "--json", env=env,
    )
    assert started.returncode == 0, (
        f"stdout={started.stdout!r}; stderr={started.stderr!r}"
    )
    completed = _cli(
        state_root, primary, "complete", run_id, "--json",
        "--result-locator", "fixture://latest-revision/plan",
        "--result-sha256", "6" * 64, env=env,
    )
    assert completed.returncode == 0, (
        f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
    )
    crash_program = textwrap.dedent(
        """
        import json, os, signal, sys
        from run_state.state import ControlStore
        original = ControlStore.transition_activity
        def crash_after_transition(self, token, activity_id, **kwargs):
            result = original(self, token, activity_id, **kwargs)
            if kwargs.get('new') == 'active':
                print(json.dumps({'boundary':'activity-active-before-context-pointer',
                                  'activity_id':activity_id,'pid':os.getpid()}), flush=True)
                os.kill(os.getpid(), signal.SIGKILL)
            return result
        ControlStore.transition_activity = crash_after_transition
        from run_state.cli import main
        raise SystemExit(main(sys.argv[1:]))
        """
    )
    crashed = subprocess.run(
        [sys.executable, "-c", crash_program, "start", "--skill", "fix",
         "--objective", objective, "--activity", "execute", "--run-id", run_id,
         "--json", "--state-root", str(state_root)],
        cwd=primary, env=env, capture_output=True, text=True, timeout=30,
    )
    assert crashed.returncode == -signal.SIGKILL, (
        f"stdout={crashed.stdout!r}; stderr={crashed.stderr!r}"
    )
    assert json.loads(crashed.stdout)["boundary"] == "activity-active-before-context-pointer"

    repository_id = repository_identity(resolve_repository(primary))
    store = ControlStore(state_root / "control.sqlite3")
    context_before = json.loads(_cli(
        state_root, primary, "context", "--run-id", run_id, "--json", env=env,
    ).stdout)
    events_before = list(store.enumerate_events(run_id=run_id, repository_id=repository_id))
    refs_before = _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=primary).stdout
    worktrees_before = _git("worktree", "list", "--porcelain", cwd=primary).stdout

    refused = _cli(
        state_root, primary, "start", "--skill", "fix", "--objective", objective,
        "--activity", "execute", "--run-id", run_id, "--json", env=env,
    )
    assert refused.returncode == 3, (
        f"stdout={refused.stdout!r}; stderr={refused.stderr!r}"
    )
    assert json.loads(refused.stdout)["code"] == "RESUME_REQUIRED"
    context_after = json.loads(_cli(
        state_root, primary, "context", "--run-id", run_id, "--json", env=env,
    ).stdout)
    assert context_after["generation"] == context_before["generation"]
    assert list(store.enumerate_events(run_id=run_id, repository_id=repository_id)) == events_before
    assert _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=primary).stdout == refs_before
    assert _git("worktree", "list", "--porcelain", cwd=primary).stdout == worktrees_before
