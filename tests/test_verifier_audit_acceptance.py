"""Acceptance boundaries for the executed verifier audit (VES findings)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts/verification/parallel_host_parity.py"
SCHEMA = "ffs.parallel-host-verification/v1"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, sort_keys=True) + "\n")
    return path


def load_verifier():
    spec = importlib.util.spec_from_file_location("verifier_audit_subject", VERIFIER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def complete_manifest(candidate: Path) -> dict[str, object]:
    candidate_digest = digest(candidate)
    return {
        "schema": SCHEMA,
        "binding": {"run": "audit-acceptance", "activity": "review-upgraded", "attempt": "1"},
        "label": "hermetic",
        "candidate": {"locator": str(candidate), "sha256": candidate_digest},
        "provenance": {key: candidate_digest for key in
                       ("source_sha256", "binary_sha256", "bundle_sha256", "config_sha256")},
        "ac_ids": ["AC-010"], "path_ids": ["PATH-002"], "int_ids": ["INT-001"],
        "producer": {"host": "producer", "model": "producer-v1", "session": "producer-1"},
    }


def private_authority(tmp_path: Path, manifest: dict[str, object], assignments: list[dict] | None = None) -> Path:
    private = tmp_path / "supervisor-evidence"
    private.mkdir(mode=0o700)
    record = {
        "schema": "ffs.verification-repair-authority/v1",
        "run": manifest["binding"]["run"],
        "candidate_sha256": manifest["candidate"]["sha256"],
        "assignments": assignments or [],
    }
    argv = manifest.get("review_adapter")
    if isinstance(argv, list) and len(argv) > 1:
        executable, adapter = Path(argv[0]).resolve(), Path(argv[1]).resolve()
        record.update({"producer": manifest["producer"], "adapters": [{
            "argv": argv, "label": manifest["label"], "host": "reviewer", "model": "reviewer-v1",
            "executable": {"locator": str(executable), "sha256": digest(executable)},
            "artifacts": [{"locator": str(adapter), "sha256": digest(adapter)}],
        }]})
    authority = write_json(private / "authority.json", record)
    authority.chmod(0o600)
    return authority


def invoke(tmp_path: Path, manifest: dict[str, object], *, fresh: bool, purpose: str = "admission",
           authority: Path | None = None, output: Path | None = None) -> tuple[subprocess.CompletedProcess[str], dict | None]:
    source = write_json(tmp_path / "manifest.json", manifest)
    output = output or tmp_path / "external-evidence" / "result.json"
    env = os.environ.copy()
    if fresh:
        env["FFS_VERIFICATION_MANIFEST"] = str(source)
        arguments = ["review", "--stage", "upgraded", "--purpose", purpose]
    else:
        arguments = ["review", "--manifest", str(source), "--stage", "upgraded", "--purpose", purpose]
    if authority:
        env["FFS_VERIFICATION_AUTHORITY"] = str(authority)
    result = subprocess.run([sys.executable, str(VERIFIER), *arguments, "--output", str(output)],
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                            check=False, timeout=15)
    payload = json.loads(result.stdout) if result.stdout.strip() else None
    if output.exists():
        assert json.loads(output.read_text()) == payload
    return result, payload


def fresh_fixture(tmp_path: Path, body: str | None = None) -> tuple[dict[str, object], Path, Path]:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("print('candidate')\n")
    adapter = tmp_path / "reviewer.py"
    adapter.write_text(body or (
        "import hashlib,json,pathlib,sys\n"
        "print(json.dumps({'verdict':'PASS','findings':[],"
        "'reviewed_sha256':hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest(),"
        "'host':'reviewer','model':'reviewer-v1','session':'review-1'}))\n"))
    adapter.chmod(0o700)
    manifest = complete_manifest(candidate)
    manifest["review_adapter"] = [sys.executable, str(adapter), str(candidate)]
    manifest["review_timeout_seconds"] = 1
    return manifest, candidate, adapter


def test_ves001_duplicate_or_incomplete_severe_assignments_cannot_authorize_repair(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("candidate\n")
    manifest = complete_manifest(candidate)
    review = {"verdict": "FAIL", "reviewed_sha256": digest(candidate),
              "findings": [{"id": "F1", "severity": "critical", "status": "open"},
                           {"id": "F2", "severity": "high", "status": "open"}]}
    result_file = write_json(tmp_path / "review.json", review)
    incomplete = {"finding_id": "F1", "action": "assign-repair"}
    manifest.update({"reviewer": {"host": "reviewer", "model": "reviewer-v1", "session": "r1"},
                     "platform": sys.platform, "command": ["audit"], "started_utc": "2026-09-12T00:00:00Z",
                     "completed_utc": "2026-09-12T00:00:01Z", "exit_status": 1,
                     "artifact_sha256": digest(candidate),
                     "artifacts": [{"locator": str(result_file), "sha256": digest(result_file)}],
                     "result": {"locator": str(result_file)},
                     "severe_path_disposition": [incomplete, dict(incomplete)]})
    authority = private_authority(tmp_path, manifest, [incomplete, dict(incomplete)])
    run, payload = invoke(tmp_path, manifest, fresh=False, purpose="review-completion", authority=authority)
    assert run.returncode != 0
    assert payload and payload["repair_authorized"] is False and payload["review_complete"] is False


def test_ves004_fresh_admission_requires_complete_binding_and_nonempty_ids(tmp_path: Path) -> None:
    manifest, _, _ = fresh_fixture(tmp_path)
    authority = private_authority(tmp_path, manifest)
    passing, payload = invoke(tmp_path, manifest, fresh=True, authority=authority)
    assert passing.returncode == 0, passing.stderr
    assert payload and payload["path_admitted"] is True and payload["path_ids"] == ["PATH-002"]
    malformed = dict(manifest)
    malformed["binding"] = {"run": "audit-acceptance"}
    malformed.pop("path_ids")
    rejected, refusal = invoke(tmp_path, malformed, fresh=True, authority=authority,
                               output=tmp_path / "external-evidence" / "malformed.json")
    assert rejected.returncode != 0
    assert refusal and refusal["path_admitted"] is False


@pytest.mark.parametrize("field,value", [("binding", {"run": "r", "activity": [], "attempt": {}}),
                                          ("command", "not-an-argv"), ("exit_status", "zero"),
                                          ("ac_ids", "AC-010")])
def test_ves005_nested_manifest_values_are_typed_and_closed(tmp_path: Path, field: str, value: object) -> None:
    candidate = tmp_path / "candidate"
    candidate.write_text("candidate\n")
    manifest = complete_manifest(candidate)
    review_path = write_json(tmp_path / "review.json", {"verdict": "PASS", "findings": [], "reviewed_sha256": digest(candidate)})
    manifest.update({"reviewer": {"host": "reviewer", "model": "reviewer-v1", "session": "r1"},
                     "platform": sys.platform, "command": ["audit"], "started_utc": "2026-09-12T00:00:00Z",
                     "completed_utc": "2026-09-12T00:00:01Z", "exit_status": 0,
                     "artifact_sha256": digest(candidate), "artifacts": [{"locator": str(review_path), "sha256": digest(review_path)}],
                     "result": {"locator": str(review_path)}, "severe_path_disposition": []})
    manifest[field] = value
    run, payload = invoke(tmp_path, manifest, fresh=False, purpose="review-completion")
    assert run.returncode != 0
    assert payload and payload["status"] != "PASS"


def test_ves006_ancestor_swap_cannot_redirect_checked_read_or_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    subject = load_verifier()
    safe, redirected = tmp_path / "safe", tmp_path / "redirected"
    safe.mkdir(); redirected.mkdir()
    (safe / "input.json").write_text("safe")
    (redirected / "input.json").write_text("redirected")
    original_open = subject.os.open
    swapped = False
    def swap_then_open(path, *args, **kwargs):
        nonlocal swapped
        if not swapped and (Path(path) == safe / "input.json" or path == "input.json"):
            swapped = True
            parked = tmp_path / "parked"
            safe.rename(parked)
            safe.symlink_to(redirected, target_is_directory=True)
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(subject.os, "open", swap_then_open)
    try:
        observed = subject.read(safe / "input.json")
    except subject.E:
        observed = None
    assert swapped, "the read boundary must be exercised after ancestor validation"
    assert observed in (None, b"safe"), "a checked read may refuse or retain its original descriptor, never redirect"
    assert (redirected / "input.json").read_text() == "redirected"


def test_ves009_adapter_output_and_timeout_descendants_are_bounded_and_reaped(tmp_path: Path) -> None:
    marker = tmp_path / "adapter-reached-four-megabytes"
    late = tmp_path / "late-private-marker"
    child = "import time,pathlib;time.sleep(2);pathlib.Path(" + repr(str(late)) + ").write_text('late')"
    body = (
        "import pathlib,subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable,'-c',{child!r}])\n"
        "for _ in range(64): sys.stdout.write('x'*65536);sys.stdout.flush()\n"
        f"pathlib.Path({str(marker)!r}).write_text('unbounded')\n"
        "time.sleep(20)\n")
    manifest, _, _ = fresh_fixture(tmp_path, body)
    authority = private_authority(tmp_path, manifest)
    run, payload = invoke(tmp_path, manifest, fresh=True, authority=authority)
    assert run.returncode != 0 and payload and payload["status"] == "UNMET"
    time.sleep(2.5)
    assert not marker.exists(), "adapter output must be capped before multi-megabyte buffering"
    assert not late.exists(), "a timed-out adapter child must be reaped with its parent"


def test_ves010_short_write_publishes_exact_complete_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    subject = load_verifier()
    target = tmp_path / "evidence" / "result.json"
    expected = b'{"complete":true}\n'
    original_write = subject.os.write
    first = True
    def short_write(fd, data):
        nonlocal first
        if first:
            first = False
            portion = bytes(data[: max(1, len(data) // 2)])
            return original_write(fd, portion)
        return original_write(fd, data)
    monkeypatch.setattr(subject.os, "write", short_write)
    subject.save(target, expected)
    assert target.read_bytes() == expected
    assert json.loads(target.read_text()) == {"complete": True}


def test_ves017_output_filesystem_errors_still_emit_one_json_refusal(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.write_text("candidate\n")
    manifest = complete_manifest(candidate)
    missing_output = tmp_path / "missing" / "nested" / "result.json"
    run, payload = invoke(tmp_path, manifest, fresh=False, output=missing_output)
    assert run.returncode != 0
    assert payload and payload["status"] != "PASS"
    assert "Traceback" not in run.stderr


def test_ves007_and_ves008_refuse_authority_and_output_inside_a_real_repository(tmp_path: Path) -> None:
    repo = tmp_path / "disposable-repository"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=10)
    manifest, _, _ = fresh_fixture(tmp_path)
    repo_private = repo / "private"
    repo_private.mkdir(mode=0o700)
    external_authority = private_authority(tmp_path, manifest)
    authority = write_json(repo_private / "authority.json", json.loads(external_authority.read_text()))
    authority.chmod(0o600)
    run, payload = invoke(tmp_path, manifest, fresh=True, authority=authority, output=repo_private / "result.json")
    assert run.returncode != 0
    assert payload and payload["status"] != "PASS"
    assert not (repo_private / "result.json").exists()
