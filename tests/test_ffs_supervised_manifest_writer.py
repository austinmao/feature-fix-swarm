"""TDD for F36: the FFS-supervised adapter must accept an out-of-order wave
manifest and canonicalize it, so a real executor's insertion-ordered JSON
does not get rejected by the strict ``--manifest`` check.

RED (this file, before the fix): ``--write-manifest`` does not exist yet, so
every test below fails against the unmodified patch.
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


def _noncanonical_manifest(workspace: Path) -> dict:
    """Build a valid manifest with the exact real-world non-canonical key order
    observed in the rejected-wave-1 evidence (top-level keys are NOT sorted;
    nested `admission` and `plans[0]` also insert out of alphabetical order).
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


def test_write_manifest_canonicalizes_and_manifest_path_then_accepts_it(
    patched_package: Path, tmp_path: Path,
) -> None:
    """(a) A valid, non-canonically-ordered manifest is canonicalized by
    --write-manifest, and the resulting file then passes the existing
    strict --manifest/--output canonical-JSON check end to end."""
    workspace = tmp_path / "workspace"
    document = _noncanonical_manifest(workspace)
    encoded = json.dumps(document)  # insertion order, deliberately NOT sorted
    assert encoded != _canonical_json(document), "fixture must actually be non-canonical"

    manifest_path = tmp_path / "wave.manifest.json"
    write = subprocess.run(
        ["node", str(patched_package / ADAPTER_RELATIVE), "--write-manifest", str(manifest_path)],
        input=encoded, text=True, capture_output=True, check=False,
    )
    assert write.returncode == 0, write.stderr
    assert manifest_path.read_text() == _canonical_json(document)
    assert oct(manifest_path.stat().st_mode)[-3:] == "600"

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


def test_write_manifest_refuses_invalid_json(patched_package: Path, tmp_path: Path) -> None:
    manifest_path = tmp_path / "wave.manifest.json"
    result = subprocess.run(
        ["node", str(patched_package / ADAPTER_RELATIVE), "--write-manifest", str(manifest_path)],
        input="{not json", text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert not manifest_path.exists()


def test_write_manifest_refuses_oversize_input(patched_package: Path, tmp_path: Path) -> None:
    manifest_path = tmp_path / "wave.manifest.json"
    oversize = json.dumps({"padding": "x" * (17 * 1024)})
    result = subprocess.run(
        ["node", str(patched_package / ADAPTER_RELATIVE), "--write-manifest", str(manifest_path)],
        input=oversize, text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert not manifest_path.exists()


def test_write_manifest_refuses_existing_path(patched_package: Path, tmp_path: Path) -> None:
    manifest_path = tmp_path / "wave.manifest.json"
    manifest_path.write_text("preexisting")
    result = subprocess.run(
        ["node", str(patched_package / ADAPTER_RELATIVE), "--write-manifest", str(manifest_path)],
        input=json.dumps({"a": 1}), text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert manifest_path.read_text() == "preexisting"


def test_write_manifest_refuses_symlink_target(patched_package: Path, tmp_path: Path) -> None:
    real_target = tmp_path / "real.json"
    real_target.write_text("untouched")
    manifest_path = tmp_path / "wave.manifest.json"
    manifest_path.symlink_to(real_target)
    result = subprocess.run(
        ["node", str(patched_package / ADAPTER_RELATIVE), "--write-manifest", str(manifest_path)],
        input=json.dumps({"a": 1}), text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert manifest_path.is_symlink()
    assert real_target.read_text() == "untouched"


def test_doc_writer_step_uses_write_manifest_and_drops_raw_buffer_writer(patched_package: Path) -> None:
    """(c) The patched doc's exclusive-writer step invokes --write-manifest and
    no longer contains the raw Buffer.from(process.argv[2]) one-liner."""
    doc = (patched_package / DOC_RELATIVE).read_text()
    assert "Buffer.from(process.argv[2])" not in doc
    section = doc.split("## FFS-supervised-process compatibility mode\n", 1)[1]
    assert '--write-manifest "$FFS_WAVE_MANIFEST"' in section
    assert '[ "$FFS_WAVE_RETAINED" = none ] || exit 78' in section
    assert 'printf \'%s\' "$FFS_WAVE_MANIFEST_JSON" | node "${GSD_TOOLS%/*}/ffs-supervised-dispatch.cjs" --write-manifest "$FFS_WAVE_MANIFEST" || exit 78' in section
