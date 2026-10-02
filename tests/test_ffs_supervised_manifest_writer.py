"""Behavior and regression tests for the FFS-supervised adapter's
--write-manifest mode (F36).

A real executor writes FFS_WAVE_MANIFEST_JSON with keys in ordinary
insertion order; the adapter must canonicalize it before the strict
--manifest check rejects anything that is not already bounded canonical
JSON. Most tests here are regression tests for behavior delivered across
two fix commits (canonicalization, dual raw/canonical size bounds, schema
validation, strict UTF-8 decoding, and an exclusive/symlink-safe publish
via a private temp file plus a hard link). Two tests pin down bugs found
in review of the first fix commit and are the ones that actually failed
against that commit:

- test_write_manifest_accepts_file_stdin: the first --write-manifest only
  resolved its stdin-reading promise on the stream's 'close' event, which
  a file redirected onto stdin does not reliably emit; the process used to
  report success and write nothing.
- test_write_manifest_accepts_pretty_printed_input_over_16kib_raw_but_canonical_within_bound:
  the first --write-manifest bounded raw stdin to 16 KiB, so a pretty-
  printed manifest whose canonical form fits comfortably under 16 KiB was
  wrongly rejected for being "too big" before it was ever parsed.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "lib"))

PATCH = ROOT / "patches" / "gsd-1.14-ffs-supervised-dispatch.patch"
INSTALLED = ROOT / "node_modules" / "@opengsd" / "gsd-core"
ADAPTER_RELATIVE = "gsd-core/bin/ffs-supervised-dispatch.cjs"
DOC_RELATIVE = "gsd-core/workflows/execute-phase/steps/executor-isolation-dispatch.md"


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@pytest.fixture(scope="module")
def patched_package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Apply the pinned patch to a clean copy of the installed package."""
    target = tmp_path_factory.mktemp("f36-manifest-writer") / "gsd-core"
    shutil.copytree(INSTALLED, target, symlinks=True)
    subprocess.run(["git", "apply", "--check", str(PATCH)], cwd=target, check=True)
    subprocess.run(["git", "apply", str(PATCH)], cwd=target, check=True)
    return target


def _valid_manifest(workspace: Path) -> dict:
    """A schema-valid manifest with the real-world non-canonical key order
    observed in the rejected-wave-1 evidence (top-level keys are NOT
    sorted; nested `admission` and `plans[0]` also insert out of order).
    """
    child = workspace / "child"
    child.mkdir(parents=True, exist_ok=True)
    prompt = "fresh executor prompt"
    admission = {
        "activity_id": "activity-1",
        "available": True,
        "generation": 1,
        "repository_id": "repo",
        "run_id": "run",
        "runtime_identity": "runtime",
        "schema": "ffs.supervisor-admission/v1",
        "workspace": str(workspace),
    }
    plan = {
        "id": "01-01",
        "initial_head": "a" * 40,
        "prompt": prompt,
        "prompt_fresh": True,
        "prompt_nonce": "nonce-1",
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "files_modified": ["src/shared.txt"],
        "files_deleted": [],
    }
    return {
        "schema": "ffs.gsd-supervised-dispatch/v1",
        "mode": "ffs-supervised-process",
        "admission": admission,
        "orchestrator_root": str(workspace),
        "initial_head": "a" * 40,
        "wave": 1,
        "phase": "1",
        "commit_mode": "patches",
        "apply_between_waves": True,
        "plans": [plan],
    }


def _fake_supervisor(tmp_path: Path) -> Path:
    script = tmp_path / "supervisor.py"
    script.write_text(
        "import json, sys\n"
        "manifest = json.load(sys.stdin)\n"
        "results = []\n"
        "for p in manifest['plans']:\n"
        "    results.append(dict(plan_id=p['id'], status='complete', summary='ok', changed_files=[], patch=''))\n"
        "print(json.dumps({\n"
        "  'schema': manifest['schema'], 'mode': manifest['mode'], 'wave': manifest['wave'],\n"
        "  'initial_head': manifest['initial_head'], 'apply_between_waves': True, 'results': results,\n"
        "}))\n",
    )
    return script


def _run_write_manifest(
    adapter: Path, target: Path, *, input_bytes: bytes | None = None, stdin_path: Path | None = None,
) -> subprocess.CompletedProcess:
    command = ["node", str(adapter), "--write-manifest", str(target)]
    if stdin_path is not None:
        with stdin_path.open("rb") as handle:
            return subprocess.run(command, stdin=handle, capture_output=True, check=False)
    return subprocess.run(command, input=input_bytes, capture_output=True, check=False)


def _leftover_entries(directory: Path, *expected: Path) -> list[Path]:
    expected_names = {path.name for path in expected}
    return [entry for entry in directory.iterdir() if entry.name not in expected_names]


def test_write_manifest_canonicalizes_and_manifest_path_then_accepts_it(
    patched_package: Path, tmp_path: Path,
) -> None:
    """A valid, non-canonically-ordered manifest is canonicalized by
    --write-manifest, and the resulting file then passes the existing
    strict --manifest/--output canonical-JSON check end to end."""
    write_dir = tmp_path / "write"
    write_dir.mkdir()
    workspace = tmp_path / "workspace"
    document = _valid_manifest(workspace)
    encoded = json.dumps(document).encode()  # insertion order, deliberately NOT sorted
    assert encoded.decode() != _canonical_json(document), "fixture must actually be non-canonical"

    manifest_path = write_dir / "wave.manifest.json"
    write = _run_write_manifest(patched_package / ADAPTER_RELATIVE, manifest_path, input_bytes=encoded)
    assert write.returncode == 0, write.stderr
    assert manifest_path.read_text() == _canonical_json(document)
    assert oct(manifest_path.stat().st_mode)[-3:] == "600"
    assert _leftover_entries(write_dir, manifest_path) == []

    output_path = tmp_path / "wave.result.json"
    admission_path = tmp_path / "admission.json"
    admission_path.write_text(_canonical_json(document["admission"]))
    env = os.environ | {
        "FFS_SUPERVISED_DISPATCH_COMMAND_JSON": json.dumps([sys.executable, str(_fake_supervisor(tmp_path))]),
        "FFS_SUPERVISED_ADMISSION_FILE": str(admission_path),
    }
    accepted = subprocess.run(
        ["node", str(patched_package / ADAPTER_RELATIVE), "--manifest", str(manifest_path), "--output", str(output_path)],
        env=env, text=True, capture_output=True, check=False,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert "canonical JSON" not in accepted.stderr
    assert json.loads(output_path.read_text())["results"][0]["status"] == "complete"


def test_write_manifest_accepts_file_stdin(patched_package: Path, tmp_path: Path) -> None:
    """Regression for the file-redirected-stdin bug found in review: the
    first implementation resolved only on the stream's 'close' event, which
    a real file does not reliably emit, so the process exited 0 without
    writing anything."""
    write_dir = tmp_path / "write"
    write_dir.mkdir()
    workspace = tmp_path / "workspace"
    document = _valid_manifest(workspace)
    input_file = tmp_path / "manifest-input.json"
    input_file.write_text(json.dumps(document))

    manifest_path = write_dir / "wave.manifest.json"
    result = _run_write_manifest(patched_package / ADAPTER_RELATIVE, manifest_path, stdin_path=input_file)
    assert result.returncode == 0, result.stderr
    assert manifest_path.read_text() == _canonical_json(document)
    assert _leftover_entries(write_dir, manifest_path) == []


def test_write_manifest_accepts_pretty_printed_input_over_16kib_raw_but_canonical_within_bound(
    patched_package: Path, tmp_path: Path,
) -> None:
    """Regression for the single-bound bug found in review: the first
    implementation bounded raw stdin to 16 KiB, rejecting a pretty-printed
    manifest whose canonical form fit well inside 16 KiB."""
    write_dir = tmp_path / "write"
    write_dir.mkdir()
    workspace = tmp_path / "workspace"
    document = _valid_manifest(workspace)
    pretty = json.dumps(document, indent=400).encode()
    assert 16 * 1024 < len(pretty) <= 64 * 1024, "fixture must sit strictly between the raw and canonical bounds"
    assert len(_canonical_json(document).encode()) <= 16 * 1024

    manifest_path = write_dir / "wave.manifest.json"
    result = _run_write_manifest(patched_package / ADAPTER_RELATIVE, manifest_path, input_bytes=pretty)
    assert result.returncode == 0, result.stderr
    assert manifest_path.read_text() == _canonical_json(document)


def test_write_manifest_accepts_long_target_name(patched_package: Path, tmp_path: Path) -> None:
    """Regression for round-2 review: the temporary name embedded the whole
    target basename plus a suffix, so a legal 240-byte target name overflowed
    the 255-byte name limit (ENAMETOOLONG)."""
    write_dir = tmp_path / "write"
    write_dir.mkdir()
    document = _valid_manifest(tmp_path / "workspace")
    manifest_path = write_dir / ("m" * 235 + ".json")
    result = _run_write_manifest(patched_package / ADAPTER_RELATIVE, manifest_path, input_bytes=json.dumps(document).encode())
    assert result.returncode == 0, result.stderr
    assert manifest_path.read_text() == _canonical_json(document)
    assert _leftover_entries(write_dir, manifest_path) == []


def test_write_manifest_refuses_invalid_json(patched_package: Path, tmp_path: Path) -> None:
    write_dir = tmp_path / "write"
    write_dir.mkdir()
    manifest_path = write_dir / "wave.manifest.json"
    result = _run_write_manifest(patched_package / ADAPTER_RELATIVE, manifest_path, input_bytes=b"{not json")
    assert result.returncode != 0
    assert b"manifest JSON on stdin is invalid" in result.stderr
    assert not manifest_path.exists()
    assert _leftover_entries(write_dir) == []


def test_write_manifest_refuses_invalid_utf8(patched_package: Path, tmp_path: Path) -> None:
    write_dir = tmp_path / "write"
    write_dir.mkdir()
    manifest_path = write_dir / "wave.manifest.json"
    result = _run_write_manifest(patched_package / ADAPTER_RELATIVE, manifest_path, input_bytes=b"\xff\xfe\x00\x01")
    assert result.returncode != 0
    assert b"not valid UTF-8" in result.stderr
    assert not manifest_path.exists()
    assert _leftover_entries(write_dir) == []


def test_write_manifest_refuses_oversize_raw_input(patched_package: Path, tmp_path: Path) -> None:
    write_dir = tmp_path / "write"
    write_dir.mkdir()
    manifest_path = write_dir / "wave.manifest.json"
    oversize = json.dumps({"padding": "x" * (65 * 1024)}).encode()
    assert len(oversize) > 64 * 1024
    result = _run_write_manifest(patched_package / ADAPTER_RELATIVE, manifest_path, input_bytes=oversize)
    assert result.returncode != 0
    assert b"65536 byte bound" in result.stderr
    assert not manifest_path.exists()
    assert _leftover_entries(write_dir) == []


def test_write_manifest_refuses_canonical_oversize_input(patched_package: Path, tmp_path: Path) -> None:
    """Raw input stays well under the 64 KiB raw bound, but re-serializing
    each compact exponential-notation number expands it past the 16 KiB
    canonical bound (e.g. "9e15" becomes "9000000000000000")."""
    write_dir = tmp_path / "write"
    write_dir.mkdir()
    manifest_path = write_dir / "wave.manifest.json"
    raw = ("[" + ",".join(["9e15"] * 1500) + "]").encode()
    assert len(raw) <= 64 * 1024
    result = _run_write_manifest(patched_package / ADAPTER_RELATIVE, manifest_path, input_bytes=raw)
    assert result.returncode != 0
    assert b"16384 byte canonical bound" in result.stderr
    assert not manifest_path.exists()
    assert _leftover_entries(write_dir) == []


def test_write_manifest_refuses_schema_invalid_manifest(patched_package: Path, tmp_path: Path) -> None:
    write_dir = tmp_path / "write"
    write_dir.mkdir()
    manifest_path = write_dir / "wave.manifest.json"
    invalid = json.dumps({"schema": "wrong", "mode": "ffs-supervised-process"}).encode()
    result = _run_write_manifest(patched_package / ADAPTER_RELATIVE, manifest_path, input_bytes=invalid)
    assert result.returncode != 0
    assert b"manifest must declare schema" in result.stderr
    assert not manifest_path.exists()
    assert _leftover_entries(write_dir) == []


def test_write_manifest_refuses_existing_path(patched_package: Path, tmp_path: Path) -> None:
    write_dir = tmp_path / "write"
    write_dir.mkdir()
    workspace = tmp_path / "workspace"
    document = _valid_manifest(workspace)
    manifest_path = write_dir / "wave.manifest.json"
    manifest_path.write_text("preexisting")
    result = _run_write_manifest(
        patched_package / ADAPTER_RELATIVE, manifest_path, input_bytes=json.dumps(document).encode(),
    )
    assert result.returncode != 0
    assert b"already exists or is a symlink (EEXIST)" in result.stderr
    assert manifest_path.read_text() == "preexisting"
    assert _leftover_entries(write_dir, manifest_path) == []


def test_write_manifest_refuses_symlink_target(patched_package: Path, tmp_path: Path) -> None:
    write_dir = tmp_path / "write"
    write_dir.mkdir()
    workspace = tmp_path / "workspace"
    document = _valid_manifest(workspace)
    real_target = tmp_path / "real.json"
    real_target.write_text("untouched")
    manifest_path = write_dir / "wave.manifest.json"
    manifest_path.symlink_to(real_target)
    result = _run_write_manifest(
        patched_package / ADAPTER_RELATIVE, manifest_path, input_bytes=json.dumps(document).encode(),
    )
    assert result.returncode != 0
    assert b"already exists or is a symlink (EEXIST)" in result.stderr
    assert manifest_path.is_symlink()
    assert real_target.read_text() == "untouched"
    assert _leftover_entries(write_dir, manifest_path) == []


def test_doc_writer_step_uses_write_manifest_and_drops_raw_buffer_writer(patched_package: Path) -> None:
    """The patched doc's exclusive-writer step pipes the manifest into the
    adapter's writer (since 1c through --write-wave-manifest, which calls the
    same --write-manifest path) and no longer contains the raw
    Buffer.from(process.argv[2]) one-liner."""
    doc = (patched_package / DOC_RELATIVE).read_text()
    assert "Buffer.from(process.argv[2])" not in doc
    section = doc.split("## FFS-supervised-process compatibility mode\n", 1)[1]
    assert '--write-wave-manifest "${FFS_WAVE_NUMBER:-}"' in section
    assert "`retained` is `none`" in section
    assert 'printf \'%s\' "${FFS_WAVE_MANIFEST_JSON:-}" | node "${CODEX_HOME:-${CLAUDE_CONFIG_DIR:-}}/gsd-core/bin/ffs-supervised-dispatch.cjs" --write-wave-manifest "${FFS_WAVE_NUMBER:-}" || {' in section
    assert "keys may be" in section and "any order" in section
