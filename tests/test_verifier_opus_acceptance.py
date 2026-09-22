"""Independent acceptance regressions for the final Opus verifier review."""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import jsonschema
import pytest


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = Path(os.environ.get("FFS_VERIFIER_UNDER_TEST", ROOT / "scripts/verification/parallel_host_parity.py"))
SCHEMA_PATH = Path(os.environ.get("FFS_VERIFIER_SCHEMA_UNDER_TEST", ROOT / "schemas/parallel-host-verification.schema.json"))
SCHEMA = "ffs.parallel-host-verification/v1"
EVIDENCE_LIMIT = 2 * 1024 * 1024


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, sort_keys=True) + "\n")
    return path


def artifact(path: Path) -> dict[str, str]:
    return {"locator": str(path), "sha256": sha(path)}


def subject():
    name = "opus_acceptance_subject_" + hashlib.sha256(str(VERIFIER).encode()).hexdigest()[:12]
    spec = importlib.util.spec_from_file_location(name, VERIFIER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def common_manifest(candidate: Path, activity: str, *, label: str = "hermetic") -> dict:
    digest = sha(candidate)
    return {
        "schema": SCHEMA,
        "binding": {"run": "opus-acceptance", "activity": activity, "attempt": "1"},
        "label": label,
        "candidate": artifact(candidate),
        "provenance": {key: digest for key in ("source_sha256", "binary_sha256", "bundle_sha256", "config_sha256")},
        "ac_ids": ["AC-010"],
        "path_ids": ["PATH-002"],
        "int_ids": ["INT-001"],
    }


def invoke(tmp_path: Path, *arguments: str, env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess[str], dict]:
    output = tmp_path / "published" / "result.json"
    run = subprocess.run(
        [sys.executable, str(VERIFIER), *arguments, "--output", str(output)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=20,
        env=env,
    )
    assert run.stdout.strip(), run.stderr
    payload = json.loads(run.stdout)
    if output.exists():
        assert json.loads(output.read_text()) == payload
    return run, payload


def suite_observation(tmp_path: Path, name: str) -> dict:
    program = tmp_path / f"{name}.py"
    program.write_text("import json\nprint(json.dumps({'tests': {'inventory': 'PASS'}}))\n")
    stamp = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    run = subprocess.run([sys.executable, str(program)], text=True, capture_output=True, check=False, timeout=10)
    record = write_json(tmp_path / f"{name}.json", {
        "argv": [sys.executable, str(program)], "exit_status": run.returncode,
        "stdout": run.stdout, "stderr": run.stderr, "started_utc": stamp, "completed_utc": stamp,
        "tests": {"inventory": "PASS"},
    })
    return {
        "id": name, **artifact(record), "argv": [sys.executable, str(program)],
        "exit_status": run.returncode, "started_utc": stamp, "completed_utc": stamp,
    }


def full_inventory(tmp_path: Path, manifest: dict, production_inventory: list[str], ci_hash: str) -> dict:
    """Create all eight byte-backed categories; callers vary only the claims under test."""
    stamp = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    suite = suite_observation(tmp_path, "inventory-suite")
    candidate = Path(manifest["candidate"]["locator"])
    binary, bundle, config = (tmp_path / name for name in ("runtime.bin", "bundle.txt", "config.txt"))
    binary.write_bytes(b"binary fixture\n")
    bundle.write_text("bundle fixture\n")
    config.write_text("config fixture\n")
    manifest["provenance"].update({
        "source_sha256": sha(candidate), "binary_sha256": sha(binary),
        "bundle_sha256": sha(bundle), "config_sha256": sha(config),
    })
    backup, restored = tmp_path / "backup.bin", tmp_path / "restored.bin"
    backup.write_bytes(b"recoverable bytes\n")
    restored.write_bytes(backup.read_bytes())
    recovery = write_json(tmp_path / "recovery.json", {
        "name": "fixture-backup", "status": "PASS", "expected_sha256": sha(backup),
        "backup_sha256": sha(backup), "restored_sha256": sha(restored),
    })
    tool = tmp_path / "tool.txt"
    tool.write_text("tool customization\n")
    classes = "".join(
        f'<class filename="{name}"><lines><line number="1" hits="1"/></lines></class>'
        for name in production_inventory
    )
    coverage = tmp_path / "coverage.xml"
    coverage.write_text(
        f'<coverage lines-covered="{len(production_inventory)}" lines-valid="{len(production_inventory)}" '
        f'branches-covered="0" branches-valid="0"><packages><package><classes>{classes}'
        "</classes></package></packages></coverage>"
    )
    environment = []
    for name, value in {
        "platform": sys.platform, "python": sys.version.split()[0],
        "dependencies": "fixture-lock", "config": sha(config),
    }.items():
        observation = write_json(tmp_path / f"environment-{name}.json", {
            "schema": "ffs.environment-observation/v1", "name": name, "value": value,
        })
        environment.append({"name": name, "artifact": artifact(observation)})
    observations = {
        "ci": {"suite_artifacts": [suite], "head": ci_hash, "origin_main": ci_hash},
        "python": {"suite_artifacts": [suite]},
        "bats": {"suite_artifacts": [suite]},
        "backups_recovery": {"entries": [{
            "name": "fixture-backup", "expected_sha256": sha(backup),
            "backup_artifact": artifact(backup), "restored_artifact": artifact(restored),
            "verification_artifact": artifact(recovery),
        }]},
        "source_runtime": {"entries": [
            {"role": "source", "artifact": artifact(candidate)},
            {"role": "binary", "artifact": artifact(binary)},
            {"role": "bundle", "artifact": artifact(bundle)},
            {"role": "config", "artifact": artifact(config)},
        ]},
        "tools_customizations": {"entries": [{"name": "fixture-tool", "artifact": artifact(tool)}]},
        "coverage": {
            "xml_artifact": artifact(coverage), "production_inventory": production_inventory,
            "suite_artifacts": [suite],
        },
        "environment": {"entries": environment},
    }
    result = {}
    for category, value in observations.items():
        wrapper = write_json(tmp_path / f"inventory-{category}.json", {
            "schema": "ffs.full-inventory-evidence/v1", "category": category,
            "status": "PASS", "complete": True, "started_utc": stamp, "completed_utc": stamp,
            "provenance": manifest["provenance"], "observations": value,
        })
        result[category] = {"status": "PASS", "artifacts": [artifact(wrapper)]}
    return result


def repository(tmp_path: Path, files: dict[str, bytes]) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=10)
    for relative, data in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, timeout=10)
    subprocess.run([
        "git", "-C", str(repo), "-c", "user.name=fixture", "-c", "user.email=fixture@invalid",
        "commit", "-qm", "fixture",
    ], check=True, timeout=10)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, text=True,
        stdout=subprocess.PIPE, timeout=10,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", head], check=True, timeout=10)
    return repo, head


@pytest.mark.parametrize("kind", ["empty-source", "large-source", "large-index"])
def test_f1_repository_inventory_has_a_distinct_bounded_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    module = subject()
    repo = tmp_path / "repository"
    (repo / ".git").mkdir(parents=True)
    index = repo / ".git" / "index"
    index.write_bytes(b"index" if kind != "large-index" else b"i" * (EVIDENCE_LIMIT + 1))
    relative = {"empty-source": "lib/empty.py", "large-source": "scripts/large.py"}.get(kind)
    if relative:
        source = repo / relative
        source.parent.mkdir(parents=True)
        source.write_bytes(b"" if kind == "empty-source" else b"x" * (EVIDENCE_LIMIT + 1))

    def fake_git(_repo: Path, *args: str, missing: bool = False) -> str:
        if args[:2] == ("config", "--local") or args[:2] == ("config", "--worktree"):
            return ""
        if args[:2] == ("rev-parse", "--git-path"):
            return str(index)
        if args == ("rev-parse", "HEAD") or args == ("rev-parse", "origin/main"):
            return "1" * 40
        if args[:2] == ("symbolic-ref", "--short"):
            return "main"
        if args[:2] == ("status", "--porcelain=v1"):
            return ""
        if args[:2] == ("worktree", "list"):
            return f"worktree {repo}"
        if args == ("ls-files", "-z"):
            return relative or ""
        if args[:3] == ("ls-files", "-z", "--others"):
            return ""
        if args == ("rev-parse", "--git-common-dir"):
            return str(repo / ".git")
        raise AssertionError(args)

    monkeypatch.setattr(module, "_git", fake_git)
    observed = module.git_inventory(str(repo))
    assert observed["index_sha256"] == sha(index)
    if relative:
        assert observed["source_inventory"] == [{"path": relative, "sha256": sha(repo / relative)}]


@pytest.mark.parametrize("repository_mode", ["omitted-module", "absent"])
def test_f2_coverage_denominator_comes_from_repository_inventory(tmp_path: Path, repository_mode: str) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("print('candidate')\n")
    manifest = common_manifest(candidate, "baseline")
    manifest["baseline"] = {"suite_artifacts": [suite_observation(tmp_path, "baseline-suite")]}
    claimed = ["lib/inventory.py"]
    if repository_mode == "omitted-module":
        repo, head = repository(tmp_path, {
            "lib/inventory.py": b"covered = True\n", "lib/omitted.py": b"omitted = True\n",
        })
        manifest["repository"] = str(repo)
    else:
        head = "2" * 40
    manifest["full_inventory"] = full_inventory(tmp_path, manifest, claimed, head)
    source = write_json(tmp_path / "baseline.json", manifest)
    run, payload = invoke(tmp_path, "baseline", "--manifest", str(source))
    assert run.returncode != 0 or payload.get("full_baseline_complete") is False
    assert payload["status"] == "UNMET" or "coverage" in payload.get("full_baseline_unmet", [])


def _owned_pid(path: Path) -> int | None:
    try:
        value = int(path.read_text())
    except (FileNotFoundError, ValueError):
        return None
    return value if value > 1 else None


def _kill_owned(path: Path) -> None:
    pid = _owned_pid(path)
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup contract is POSIX")
@pytest.mark.parametrize("case", ["child-holds-pipe", "leader-closes-pipes", "successful-orphan"])
def test_f3_all_lingering_adapter_shapes_are_bounded_and_reaped(tmp_path: Path, case: str) -> None:
    module = subject()
    pid_file, late_marker = tmp_path / "owned.pid", tmp_path / "late-marker"
    delayed = (
        "import os,pathlib,time;"
        + ("os.close(1);os.close(2);" if case == "successful-orphan" else "")
        + "time.sleep(1.5);pathlib.Path(" + repr(str(late_marker)) + ").write_text('survived');time.sleep(30)"
    )
    if case in {"child-holds-pipe", "successful-orphan"}:
        body = (
            "import pathlib,subprocess,sys;"
            f"child=subprocess.Popen([sys.executable,'-c',{delayed!r}]);"
            f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid));"
            + ("print('{}')" if case == "successful-orphan" else "")
        )
    else:
        body = (
            "import os,pathlib,time;"
            f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()));"
            "os.close(1);os.close(2);"
            f"time.sleep(1.5);pathlib.Path({str(late_marker)!r}).write_text('survived');time.sleep(30)"
        )
    try:
        with pytest.raises(module.E) as raised:
            module._run([sys.executable, "-c", body], tmp_path, 1)
        assert raised.value.status == "UNMET"
        assert raised.value.code in {"ADAPTER_TIMEOUT", "ADAPTER_LINGERING"}
        time.sleep(1.7)
        assert not late_marker.exists(), "the exact adapter process group must be gone before refusal returns"
    finally:
        _kill_owned(pid_file)


def fresh_review_manifest(candidate: Path, adapter: list[str], *, label: str = "hermetic") -> dict:
    manifest = common_manifest(candidate, "review", label=label)
    manifest.update({
        "producer": {"host": "producer", "model": "producer-v1", "session": "producer-1"},
        "review_adapter": adapter, "review_timeout_seconds": 3,
    })
    return manifest


def test_f4_unpinned_fresh_completion_cannot_claim_observed_authenticated_identity(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("print('candidate')\n")
    review = {
        "verdict": "PASS", "findings": [], "reviewed_sha256": sha(candidate),
        "host": "self-reported-host", "model": "self-reported-model", "session": "self-reported-session",
    }
    adapter = tmp_path / "adapter.py"
    adapter.write_text("import json\nprint(json.dumps(" + repr(review) + "))\n")
    manifest = fresh_review_manifest(candidate, [sys.executable, str(adapter)], label="authenticated")
    source = write_json(tmp_path / "fresh-review.json", manifest)
    env = os.environ.copy()
    env.pop("FFS_VERIFICATION_AUTHORITY", None)
    env["FFS_VERIFICATION_MANIFEST"] = str(source)
    run, payload = invoke(tmp_path, "review", "--stage", "upgraded", "--purpose", "review-completion", env=env)
    if run.returncode == 0:
        assert payload["identity_assurance"] == "asserted"
        assert payload["authenticated"] is False
    else:
        assert payload["status"] == "UNMET"


def test_f5_ci_identity_is_compared_with_observed_origin_main(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("print('candidate')\n")
    repo, observed_head = repository(tmp_path, {"lib/inventory.py": b"covered = True\n"})
    forged = "a" * 40 if observed_head != "a" * 40 else "b" * 40
    manifest = common_manifest(candidate, "baseline")
    manifest.update({
        "repository": str(repo),
        "baseline": {"suite_artifacts": [suite_observation(tmp_path, "baseline-suite")]},
    })
    manifest["full_inventory"] = full_inventory(tmp_path, manifest, ["lib/inventory.py"], forged)
    source = write_json(tmp_path / "baseline.json", manifest)
    run, payload = invoke(tmp_path, "baseline", "--manifest", str(source))
    assert run.returncode != 0 or payload.get("full_baseline_complete") is False
    assert payload["status"] == "UNMET" or "ci" in payload.get("full_baseline_unmet", [])


def authority_for(tmp_path: Path, manifest: dict, argv: list[str], executable: Path, artifacts: list[Path] | None = None) -> Path:
    private = tmp_path / "supervisor"
    private.mkdir(mode=0o700)
    value = {
        "schema": "ffs.verification-repair-authority/v1",
        "run": manifest["binding"]["run"], "candidate_sha256": manifest["candidate"]["sha256"],
        "producer": manifest["producer"], "assignments": [],
        "adapters": [{
            "argv": argv, "label": manifest["label"], "host": "reviewer", "model": "reviewer-v1",
            "executable": artifact(executable), "artifacts": [artifact(path) for path in artifacts or []],
        }],
    }
    path = write_json(private / "authority.json", value)
    path.chmod(0o600)
    return path


def invoke_fresh_admission(tmp_path: Path, manifest: dict, authority: Path, *, path: str | None = None):
    source = write_json(tmp_path / "fresh-review.json", manifest)
    env = os.environ.copy()
    env["FFS_VERIFICATION_MANIFEST"] = str(source)
    env["FFS_VERIFICATION_AUTHORITY"] = str(authority)
    if path is not None:
        env["PATH"] = path
    return invoke(tmp_path, "review", "--stage", "upgraded", "--purpose", "admission", env=env)


def test_f6_absolute_python_package_directory_requires_a_pin(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("print('candidate')\n")
    marker = tmp_path / "directory-executed"
    package = tmp_path / "review_package"
    package.mkdir()
    package.joinpath("__main__.py").write_text(
        "import json,pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text('executed')\n"
        f"print(json.dumps({{'verdict':'PASS','findings':[],'reviewed_sha256':{sha(candidate)!r},"
        "'host':'reviewer','model':'reviewer-v1','session':'reviewer-1'}))\n"
    )
    argv = [sys.executable, str(package)]
    manifest = fresh_review_manifest(candidate, argv)
    authority = authority_for(tmp_path, manifest, argv, Path(sys.executable).resolve())
    run, payload = invoke_fresh_admission(tmp_path, manifest, authority)
    assert run.returncode != 0 and payload["status"] == "UNMET"
    assert not marker.exists(), "an unpinned package directory must be refused before launch"


def test_f6_shebang_interpreter_requires_an_execution_pin(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("print('candidate')\n")
    marker = tmp_path / "interpreter-executed"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    interpreter = bin_dir / "ffs-opus-interpreter"
    review = json.dumps({
        "verdict": "PASS", "findings": [], "reviewed_sha256": sha(candidate),
        "host": "reviewer", "model": "reviewer-v1", "session": "reviewer-1",
    })
    interpreter.write_text(f"#!/bin/sh\nprintf executed > {marker}\nprintf '%s\\n' '{review}'\n")
    interpreter.chmod(0o700)
    adapter = tmp_path / "review-adapter"
    adapter.write_text("#!/usr/bin/env ffs-opus-interpreter\n")
    adapter.chmod(0o700)
    argv = [str(adapter)]
    manifest = fresh_review_manifest(candidate, argv)
    authority = authority_for(tmp_path, manifest, argv, adapter)
    run, payload = invoke_fresh_admission(tmp_path, manifest, authority, path=str(bin_dir) + os.pathsep + os.environ["PATH"])
    assert run.returncode != 0 and payload["status"] == "UNMET"
    assert not marker.exists(), "an unpinned shebang interpreter must be refused before launch"


def _native_reviewer(tmp_path: Path, candidate: Path) -> tuple[Path, list[str]]:
    executable = Path(sys.executable).resolve()
    copied = tmp_path / "native-reviewer"
    shutil.copy2(executable, copied)
    # Native Mach-O and ELF executables permit trailing non-load bytes. Padding a
    # real relocatable interpreter crosses the evidence cap without fabricating
    # an executable format or making the test depend on one host's binary sizes.
    with copied.open("ab") as stream:
        stream.write(b"\0" * max(0, EVIDENCE_LIMIT + 1 - copied.stat().st_size))
    probe = subprocess.run([str(copied), "-c", "print('native-copy-ok')"], text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=10)
    assert probe.returncode == 0 and probe.stdout.strip() == "native-copy-ok", probe.stderr
    script = (
        "import hashlib,json,pathlib,sys;"
        "d=hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest();"
        "print(json.dumps({'verdict':'PASS','findings':[],'reviewed_sha256':d,"
        "'host':'reviewer','model':'reviewer-v1','session':'reviewer-1'}))"
    )
    return copied, [str(copied), "-c", script, str(candidate)]


def test_f1_large_native_executable_uses_the_executable_pin_policy(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("print('candidate')\n")
    executable, argv = _native_reviewer(tmp_path, candidate)
    assert executable.stat().st_size > EVIDENCE_LIMIT
    manifest = fresh_review_manifest(candidate, argv)
    authority = authority_for(tmp_path, manifest, argv, executable)
    run, payload = invoke_fresh_admission(tmp_path, manifest, authority)
    assert run.returncode == 0, payload
    assert payload["path_admitted"] is True


def imported_review_manifest(tmp_path: Path, candidate: Path) -> dict:
    result = write_json(tmp_path / "review-result.json", {
        "verdict": "PASS", "findings": [], "reviewed_sha256": sha(candidate),
    })
    stamp = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    manifest = common_manifest(candidate, "review")
    manifest.update({
        "platform": sys.platform, "command": ["reviewer"], "started_utc": stamp, "completed_utc": stamp,
        "exit_status": 0, "reviewer": {"host": "reviewer", "model": "reviewer-v1", "session": "reviewer-1"},
        "producer": {"host": "producer", "model": "producer-v1", "session": "producer-1"},
        "artifact_sha256": sha(candidate), "artifacts": [artifact(result)],
        "result": {"locator": str(result)}, "severe_path_disposition": [],
    })
    return manifest


def schema_validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))


def test_f7_schema_accepts_both_runtime_review_manifest_variants(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("print('candidate')\n")
    adapter = tmp_path / "adapter.py"
    adapter.write_text("print('review')\n")
    validator = schema_validator()
    validator.validate(fresh_review_manifest(candidate, [sys.executable, str(adapter)]))
    validator.validate(imported_review_manifest(tmp_path, candidate))


@pytest.mark.parametrize("severity", ["High", "cRiTiCaL"])
def test_f7_review_severity_schema_matches_runtime_case_handling(severity: str) -> None:
    module = subject()
    finding = {"id": "F1", "severity": severity, "status": "open"}
    assert module._finding(dict(finding))["severity"] == severity.lower()
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.Draft202012Validator({"$ref": "#/$defs/review_finding", "$defs": schema["$defs"]}).validate(finding)


def test_f7_malformed_binding_failure_still_conforms_to_result_schema(tmp_path: Path) -> None:
    malformed = write_json(tmp_path / "malformed.json", {"schema": SCHEMA, "binding": "not-an-object"})
    run, payload = invoke(tmp_path, "review", "--manifest", str(malformed), "--purpose", "review-completion")
    assert run.returncode != 0 and payload["status"] != "PASS"
    schema_validator().validate(payload)
