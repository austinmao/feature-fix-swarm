"""Independent executed-diff regressions for review admission trust."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts/verification/parallel_host_parity.py"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("defect", ["unrelated-executable", "unlisted-script", "same-model", "candidate-mutation", "missing-provenance"])
def test_admission_requires_the_actual_unchanged_execution_chain(tmp_path: Path, defect: str) -> None:
    candidate = tmp_path / "candidate.txt"
    candidate.write_text("review these bytes\n")
    adapter = tmp_path / "adapter.py"
    marker = tmp_path / "adapter-ran"
    adapter.write_text(
        "import hashlib,json,pathlib,sys\n"
        "candidate=pathlib.Path(sys.argv[1]); before=hashlib.sha256(candidate.read_bytes()).hexdigest()\n"
        f"pathlib.Path({str(marker)!r}).write_text('ran')\n" +
        ("candidate.write_text('changed after review began')\n" if defect == "candidate-mutation" else "") +
        "print(json.dumps({'verdict':'PASS','findings':[],'reviewed_sha256':before,'host':'review-vendor','model':'review-model','session':'review-attempt'}))\n"
    )
    producer = {"host": "producer-vendor", "model": "producer-model", "session": "producer-attempt"}
    if defect == "same-model":
        producer.update(host="review-vendor", model="review-model")
    argv = [sys.executable, str(adapter), str(candidate)]
    manifest = {
        "schema": "ffs.parallel-host-verification/v1",
        "binding": {"run": "review-regression", "activity": "review", "attempt": "1"},
        "ac_ids": ["AC-010"], "path_ids": ["PATH-002"], "int_ids": ["INT-001"],
        "label": "hermetic", "candidate": {"locator": str(candidate), "sha256": digest(candidate)},
        "provenance": {key: digest(candidate) for key in
                       ("source_sha256", "binary_sha256", "bundle_sha256", "config_sha256")},
        "producer": producer, "review_adapter": argv,
    }
    if defect == "missing-provenance":
        manifest["provenance"].pop("config_sha256")
    supervisor = tmp_path / "supervisor"
    supervisor.mkdir(mode=0o700)
    executable = Path(sys.executable).resolve()
    if defect == "unrelated-executable":
        executable = tmp_path / "unrelated-program"
        executable.write_text("not the executable invoked\n")
    authority = {
        "schema": "ffs.verification-repair-authority/v1", "run": "review-regression",
        "candidate_sha256": digest(candidate), "producer": producer, "assignments": [],
        "adapters": [{"argv": argv, "label": "hermetic", "host": "review-vendor", "model": "review-model",
                      "executable": {"locator": str(executable), "sha256": digest(executable)},
                      "artifacts": [] if defect == "unlisted-script" else [{"locator": str(adapter), "sha256": digest(adapter)}]}],
    }
    authority_path = supervisor / "authority.json"
    authority_path.write_text(json.dumps(authority)); authority_path.chmod(0o600)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    result = subprocess.run(
        [sys.executable, str(VERIFIER), "review", "--stage", "upgraded"],
        env={**os.environ, "FFS_VERIFICATION_MANIFEST": str(manifest_path),
             "FFS_VERIFICATION_AUTHORITY": str(authority_path)},
        text=True, capture_output=True, timeout=10,
    )
    payload = json.loads(result.stdout)
    assert result.returncode != 0 and payload["path_admitted"] is False, payload
    if defect in {"unrelated-executable", "unlisted-script", "missing-provenance"}:
        assert not marker.exists(), "An invalid execution pin or envelope must refuse before invoking the adapter"


def test_fifo_manifest_is_refused_without_waiting_for_a_writer(tmp_path: Path) -> None:
    fifo = tmp_path / "manifest.fifo"
    os.mkfifo(fifo, mode=0o600)
    try:
        result = subprocess.run([sys.executable, str(VERIFIER), "review", "--manifest", str(fifo)],
                                text=True, capture_output=True, timeout=2)
    except subprocess.TimeoutExpired:
        pytest.fail("verifier blocked opening a FIFO before validating the input type")
    assert result.returncode != 0
    assert json.loads(result.stdout)["path_admitted"] is False
