"""Independent CLI acceptance for the prospective verifier's real modes.

These cases deliberately exercise a filesystem, Git, and subprocess boundary.
Only the upstream GSD package and an external reviewer are replaced by local
hermetic executables.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import UTC, datetime

import pytest


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts/verification/parallel_host_parity.py"
SETUP = ROOT / "setup.sh"
SCHEMA = "ffs.parallel-host-verification/v1"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, sort_keys=True) + "\n")
    return path


def common(candidate: Path, activity: str) -> dict[str, object]:
    digest = sha(candidate)
    return {
        "schema": SCHEMA,
        "binding": {"run": "acceptance-fixture", "activity": activity, "attempt": "1"},
        "ac_ids": ["AC-010"], "path_ids": ["PATH-002"], "int_ids": ["INT-001"],
        "label": "hermetic",
        "candidate": {"locator": str(candidate), "sha256": digest},
        "provenance": {key: digest for key in (
            "source_sha256", "binary_sha256", "bundle_sha256", "config_sha256")},
    }


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def invoke(
    tmp_path: Path, *args: str, env: dict[str, str] | None = None, output_name: str = "result.json",
    authorize_fixture: bool = True,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    output = tmp_path / "external-evidence" / output_name
    child_env = os.environ.copy()
    child_env.update(env or {})
    if args and args[0] == "installation" and "--manifest" in args and authorize_fixture:
        manifest = json.loads(Path(args[args.index("--manifest") + 1]).read_text())
        installation = manifest.get("installation", {})
        fixture = Path(installation["fixture"]["root"])
        nested = Path(manifest["candidate"]["locator"]).parent / "tests/fixtures/gsd-installer-stub.py"
        if nested.is_file() and "nested_stub_artifact" not in installation:
            installation["nested_stub_artifact"] = {"locator": str(nested), "sha256": sha(nested)}
            write_json(Path(args[args.index("--manifest") + 1]), manifest)
        entries = []
        for path in sorted(fixture.rglob("*")):
            assert not path.is_symlink(), "the acceptance supervisor grants only regular fixture inputs"
            entries.append({"path": path.relative_to(fixture).as_posix(),
                            "type": "directory" if path.is_dir() else "file",
                            **({"sha256": sha(path)} if path.is_file() else {})})
        supervisor = tmp_path / "installation-supervisor"
        supervisor.mkdir(mode=0o700, exist_ok=True)
        authority = write_json(supervisor / "authority.json", {
            "schema": "ffs.verification-repair-authority/v1",
            "run": manifest["binding"]["run"], "candidate_sha256": manifest["candidate"]["sha256"],
            "assignments": [], "installations": [{
                "fixture_root": str(fixture.resolve()), "device": fixture.stat().st_dev,
                "inode": fixture.stat().st_ino, "setup_argv": installation["setup_argv"],
                "stub_artifact": installation["stub_artifact"], "initial_entries": entries,
                "fixture": installation["fixture"],
                **({"nested_stub_artifact": installation["nested_stub_artifact"]}
                   if "nested_stub_artifact" in installation else {}),
            }],
        })
        authority.chmod(0o600)
        child_env["FFS_VERIFICATION_AUTHORITY"] = str(authority)
    result = subprocess.run(
        [sys.executable, str(VERIFIER), *args, "--output", str(output)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_env,
        check=False, timeout=45,
    )
    assert output.is_file(), result.stderr
    payload = json.loads(output.read_text())
    assert json.loads(result.stdout) == payload
    return result, payload


def actual_suite_artifact(tmp_path: Path, name: str, tests: dict[str, str]) -> tuple[Path, dict[str, object]]:
    """Execute the tiny suite, then preserve its observed command record."""
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import json\n"
        f"tests = {tests!r}\n"
        "print(json.dumps({'tests': tests}))\n"
        "raise SystemExit(1 if 'FAIL' in tests.values() else 0)\n"
    )
    started = utc_now()
    completed = subprocess.run([sys.executable, str(script)], text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, check=False, timeout=10)
    finished = utc_now()
    observed = json.loads(completed.stdout)["tests"]
    artifact = write_json(tmp_path / f"{name}.json", {
        "argv": [sys.executable, str(script)], "exit_status": completed.returncode,
        "stdout": completed.stdout, "stderr": completed.stderr,
        "started_utc": started, "completed_utc": finished, "tests": observed,
        # The same-runtime upgrade comparator requires observed metadata even
        # when a fixture changes only test outcomes. This tiny suite uses stdlib.
        "environment": json.dumps({"platform": sys.platform, "python": {
            "executable": str(Path(sys.executable).resolve()),
            "executable_sha256": sha(Path(sys.executable).resolve()),
            "implementation": sys.implementation.name, "version": sys.version,
        }}, sort_keys=True, separators=(",", ":")),
        "dependencies": json.dumps({"stdlib_python": sys.version.split()[0]},
                                   sort_keys=True, separators=(",", ":")),
        "config_sha256": sha(tmp_path / "disposable-repository" / "candidate.txt"),
    })
    return artifact, {"id": "fixture-suite", "locator": str(artifact), "sha256": sha(artifact),
                      "argv": [sys.executable, str(script)], "exit_status": completed.returncode,
                      "started_utc": started, "completed_utc": finished}


def actual_baseline(tmp_path: Path, tests: dict[str, str], output_name: str) -> tuple[Path, Path]:
    repo = disposable_repo(tmp_path)
    artifact, observation = actual_suite_artifact(tmp_path, "before-suite", tests)
    manifest = common(repo / "candidate.txt", "baseline")
    manifest["repository"] = str(repo)
    manifest["baseline"] = {"suite_artifacts": [observation]}
    source = write_json(tmp_path / "baseline-manifest.json", manifest)
    run, payload = invoke(tmp_path, "baseline", "--manifest", str(source), output_name=output_name)
    assert run.returncode == 0, run.stderr
    assert payload["status"] == "PASS"
    assert payload["artifacts"][0]["sha256"] == sha(artifact)
    return repo / "candidate.txt", tmp_path / "external-evidence" / output_name


def disposable_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "disposable-repository"
    repo.mkdir(mode=0o700)
    clean_env = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_CEILING_DIRECTORIES",
                 "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX",
                 "GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
                 "GIT_DISCOVERY_ACROSS_FILESYSTEM"):
        clean_env.pop(name, None)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=clean_env)
    def common_dir(path: Path) -> Path:
        raw = Path(subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "--git-common-dir"], text=True, env=clean_env
        ).strip())
        return raw.resolve() if raw.is_absolute() else (path / raw).resolve()

    fixture_common = common_dir(repo)
    for real_repo in (ROOT, Path("/Users/luminamao/Documents/Github/openclaw")):
        if real_repo.exists():
            assert fixture_common != common_dir(real_repo), f"fixture must not share {real_repo.name} Git common-dir"
    candidate = repo / "candidate.txt"
    candidate.write_text("candidate bytes\n")
    subprocess.run(["git", "-C", str(repo), "add", "candidate.txt"], check=True, env=clean_env)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Acceptance", "-c",
                    "user.email=acceptance@example.invalid", "commit", "-qm", "fixture"],
                   check=True, env=clean_env)
    return repo


@pytest.mark.parametrize("fsmonitor", [False, True])
def test_baseline_cli_aggregates_actual_command_evidence(tmp_path: Path, fsmonitor: bool) -> None:
    repo = disposable_repo(tmp_path)
    canary = tmp_path / "fsmonitor-executed"
    if fsmonitor:
        hook = tmp_path / "fsmonitor.py"
        hook.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(canary)!r}).write_text('executed')\n")
        hook.chmod(0o700)
        subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", str(hook)], check=True)
    status_program = tmp_path / "emit-suite-status.py"
    status_program.write_text(
        "import json\nprint(json.dumps({'tests': {'known-regression': 'FAIL', 'healthy': 'PASS'}}))\nraise SystemExit(1)\n"
    )
    started = utc_now()
    executed = subprocess.run([sys.executable, str(status_program)], text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, check=False, timeout=10)
    completed = utc_now()
    observed_tests = json.loads(executed.stdout)["tests"]
    result_artifact = write_json(tmp_path / "suite-result.json", {
        "argv": [sys.executable, str(status_program)], "exit_status": executed.returncode,
        "stdout": executed.stdout, "stderr": executed.stderr, "started_utc": started, "completed_utc": completed,
        "tests": observed_tests,
    })
    manifest = common(repo / "candidate.txt", "baseline")
    manifest["repository"] = str(repo)
    manifest["baseline"] = {"suite_artifacts": [{
        "id": "fixture-suite", "locator": str(result_artifact), "sha256": sha(result_artifact),
        "argv": [sys.executable, str(status_program)], "exit_status": executed.returncode,
        "started_utc": started, "completed_utc": completed,
    }]}
    source = write_json(tmp_path / "baseline-manifest.json", manifest)

    run, payload = invoke(tmp_path, "baseline", "--manifest", str(source))

    assert run.returncode == 0, run.stderr
    assert payload["status"] == "PASS"  # evidence capture is complete, not suite success
    assert payload["suite_passed"] is False
    assert payload["tests"]["known-regression"] == "FAIL"
    assert payload["repository"]["head"] == subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    assert payload["artifacts"][0]["sha256"] == sha(result_artifact)
    assert executed.returncode == 1
    assert not canary.exists(), "Read-only Git inventory must disable executable fsmonitor hooks"


def test_upgrade_cli_consumes_named_baseline(tmp_path: Path) -> None:
    candidate, before = actual_baseline(
        tmp_path, {"retained": "FAIL", "changed": "PASS"}, "named-baseline.json"
    )
    current, observation = actual_suite_artifact(
        tmp_path, "upgraded-suite", {"retained": "FAIL", "changed": "FAIL"}
    )
    ledger = write_json(tmp_path / "ledger.json", {"entries": []})
    manifest = common(candidate, "upgrade")
    manifest["current"] = {"suite_artifacts": [observation]}
    manifest["ledger_artifact"] = {"locator": str(ledger), "sha256": sha(ledger)}
    source = write_json(tmp_path / "upgrade-manifest.json", manifest)

    run, payload = invoke(tmp_path, "upgrade", "--baseline", str(before), "--manifest", str(source))

    assert run.returncode != 0
    assert payload["status"] == "FAIL"
    assert payload["baseline_sha256"] == sha(before)
    assert payload["new_failures"] == ["changed"]
    assert payload["remaining_failures"] == ["changed", "retained"]
    assert payload["suite_passed"] is False


def test_fresh_review_executes_configured_adapter_and_completion_cannot_admit(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("print('candidate')\n")
    adapter_log = tmp_path / "adapter-log.json"
    adapter = tmp_path / "fake-review-adapter.py"
    adapter.write_text(
        "import hashlib, json, pathlib, sys\n"
        f"log = pathlib.Path({str(adapter_log)!r})\n"
        "candidate = pathlib.Path(sys.argv[1])\n"
        "log.write_text(json.dumps(sys.argv[1:]))\n"
        "print(json.dumps({'verdict':'PASS','findings':[], 'reviewed_sha256': hashlib.sha256(candidate.read_bytes()).hexdigest(), 'host':'fake-vendor', 'model':'fixture-v1', 'session':'fresh-1'}))\n"
    )
    adapter.chmod(0o700)
    manifest = common(candidate, "review-upgraded")
    manifest.update({"review_adapter": [sys.executable, str(adapter), str(candidate)],
                     "producer": {"host": "producer", "model": "p1", "session": "producer-1"}})
    source = write_json(tmp_path / "fresh-review-manifest.json", manifest)

    run, payload = invoke(tmp_path, "review", "--stage", "upgraded", "--purpose", "review-completion",
                          env={"FFS_VERIFICATION_MANIFEST": str(source)})

    assert run.returncode == 0, run.stderr
    assert adapter_log.is_file(), "fresh mode must invoke the configured external boundary"
    assert payload["evidence_origin"] == "executed"
    assert payload["reviewer"]["model"] == "fixture-v1"
    assert payload["review_adapter"]["argv"] == [sys.executable, str(adapter), str(candidate)]
    assert payload["path_admitted"] is False
    assert payload["rollout_ready"] is False
    assert payload["repair_authorized"] is False


@pytest.mark.parametrize("trust", ["valid", "missing", "changed-adapter", "wrong-model", "relabeled-producer"])
def test_fresh_review_admission_accepts_only_a_byte_bound_executed_identity(tmp_path: Path, trust: str) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("print('admissible candidate')\n")
    adapter = tmp_path / "identity-review-adapter.py"
    adapter.write_text(
        "import hashlib, json, pathlib, sys\n"
        "candidate = pathlib.Path(sys.argv[1])\n"
        "print(json.dumps({'verdict':'PASS','findings':[], 'reviewed_sha256': hashlib.sha256(candidate.read_bytes()).hexdigest(), 'host':'fixture-reviewer', 'model':'fixture-v1', 'session':'review-1'}))\n"
    )
    adapter.chmod(0o700)
    manifest = common(candidate, "review-upgraded")
    manifest.update({"review_adapter": [sys.executable, str(adapter), str(candidate)],
                     "producer": {"host": "producer", "model": "p1", "session": "producer-1"}})
    source = write_json(tmp_path / "admission-manifest.json", manifest)
    supervisor = tmp_path / "supervisor"
    supervisor.mkdir(mode=0o700)
    authority = write_json(supervisor / "authority.json", {
        "schema": "ffs.verification-repair-authority/v1", "run": "acceptance-fixture",
        "candidate_sha256": sha(candidate), "assignments": [],
        "producer": manifest["producer"],
        "adapters": [{"argv": manifest["review_adapter"], "label": "hermetic",
                      "host": "fixture-reviewer", "model": "fixture-v1",
                      "executable": {"locator": str(Path(sys.executable).resolve()),
                                     "sha256": sha(Path(sys.executable).resolve())},
                      "artifacts": [{"locator": str(adapter), "sha256": sha(adapter)}]}],
    })
    authority.chmod(0o600)
    if trust == "changed-adapter":
        adapter.write_text(adapter.read_text() + "# changed after supervisor approval\n")
    elif trust == "wrong-model":
        data = json.loads(authority.read_text())
        data["adapters"][0]["model"] = "different-model"
        write_json(authority, data)
    elif trust == "relabeled-producer":
        data = json.loads(authority.read_text())
        data["producer"] = {"host": "fixture-reviewer", "model": "fixture-v1", "session": "review-1"}
        write_json(authority, data)
    trusted_env = {"FFS_VERIFICATION_MANIFEST": str(source),
                   "FFS_VERIFICATION_AUTHORITY": "" if trust == "missing" else str(authority)}

    run, payload = invoke(tmp_path, "review", "--stage", "upgraded",
                          env=trusted_env)

    if trust != "valid":
        assert run.returncode != 0
        assert payload["status"] != "PASS"
        assert payload["path_admitted"] is False
        assert payload["rollout_ready"] is False
        return

    assert run.returncode == 0, run.stderr
    assert payload["schema"] == SCHEMA
    assert payload["status"] == "PASS"
    assert payload["evidence_origin"] == "executed"
    assert payload["purpose"] == "admission"
    assert payload["path_admitted"] is True
    assert payload["rollout_ready"] is False


def test_fresh_review_missing_reviewer_identity_is_unmet_and_cannot_admit(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("print('candidate')\n")
    adapter = tmp_path / "anonymous-review-adapter.py"
    adapter.write_text(
        "import hashlib, json, pathlib, sys\n"
        "print(json.dumps({'verdict':'PASS','findings':[], 'reviewed_sha256': hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest()}))\n"
    )
    adapter.chmod(0o700)
    manifest = common(candidate, "review-upgraded")
    manifest.update({"review_adapter": [sys.executable, str(adapter), str(candidate)],
                     "producer": {"host": "producer", "model": "p1", "session": "producer-1"}})
    source = write_json(tmp_path / "anonymous-manifest.json", manifest)

    run, payload = invoke(tmp_path, "review", "--stage", "upgraded", "--purpose", "review-completion",
                          env={"FFS_VERIFICATION_MANIFEST": str(source)})

    assert run.returncode != 0
    assert payload["status"] != "PASS"
    assert payload.get("path_admitted") is not True
    assert "identity" in json.dumps(payload).lower()


def test_installation_cli_runs_real_private_installer(tmp_path: Path) -> None:
    fixture = tmp_path / "private-fixture"
    fixture.mkdir(mode=0o700)
    home, codex, cache, state, project = (fixture / name for name in
                                          ("home", "codex", "cache", "state", "project"))
    gsd_log = fixture / "gsd-stub.log"
    manifest = common(SETUP, "installation-private")
    manifest["installation"] = {
        "setup_argv": ["/bin/bash", str(SETUP), "--scope", "user"],
        "fixture": {"root": str(fixture), "home": str(home), "codex_home": str(codex),
                    "cache": str(cache), "state": str(state), "project": str(project)},
        "env": {"FFS_SKIP_PROMPT_MASTER": "1", "FFS_SKIP_SOCRATIC": "1",
                "FFS_GSD_INSTALLER": str(ROOT / "tests/fixtures/gsd-installer-stub.py"),
                "FFS_GSD_STUB_LOG": str(gsd_log)},
        "stub_artifact": {"locator": str(ROOT / "tests/fixtures/gsd-installer-stub.py"),
                          "sha256": sha(ROOT / "tests/fixtures/gsd-installer-stub.py")},
        "timeout_seconds": 30,
    }
    source = write_json(tmp_path / "installation-manifest.json", manifest)
    parent_home = os.environ.get("HOME")

    run, payload = invoke(tmp_path, "installation", "--mode", "private", "--manifest", str(source))

    assert run.returncode == 0, run.stderr
    assert payload["status"] == "PASS"
    assert payload["invocation"]["exit_status"] == 0
    assert gsd_log.is_file(), "the contained real setup.sh must reach its upstream stub boundary"
    assert (home / ".claude/gsd-file-manifest.json").is_file()
    assert os.environ.get("HOME") == parent_home
    assert not (tmp_path / "home").exists(), "verifier must use declared child HOME, never a default fixture"


def test_private_installation_os_confines_untrusted_stub_before_candidate_code(tmp_path: Path) -> None:
    """The child must receive an OS boundary; fixture env alone is insufficient."""
    fixture = tmp_path / "private-fixture"
    fixture.mkdir(mode=0o700)
    external_sentinel = tmp_path / "outside-active-profile"
    external_sentinel.write_text("preserve\n")
    denial_marker = fixture / "outside-write-denial.json"
    stub = fixture / "confinement-probe-stub.py"
    stub.write_text(
        "import errno, json, pathlib, subprocess, sys\n"
        f"outside = pathlib.Path({str(external_sentinel)!r})\n"
        f"marker = pathlib.Path({str(denial_marker)!r})\n"
        "try:\n    outside.write_text('escaped')\n"
        "except OSError as error:\n"
        "    if error.errno not in (errno.EACCES, errno.EPERM, errno.EROFS): raise\n"
        "    marker.write_text(json.dumps({'result': 'permission-denied'}))\n"
        "else:\n    marker.write_text(json.dumps({'result': 'write-escaped'})); raise SystemExit(97)\n"
        f"raise SystemExit(subprocess.run([sys.executable, {str(ROOT / 'tests/fixtures/gsd-installer-stub.py')!r}, *sys.argv[1:]], check=False).returncode)\n"
    )
    stub.chmod(0o700)
    manifest = common(SETUP, "installation-private")
    manifest["installation"] = {
        "setup_argv": ["/bin/bash", str(SETUP), "--scope", "user"],
        "fixture": {"root": str(fixture), **{key: str(fixture / key) for key in
                    ("home", "codex_home", "cache", "state", "project")}},
        "env": {"FFS_SKIP_PROMPT_MASTER": "1", "FFS_SKIP_SOCRATIC": "1",
                "FFS_GSD_INSTALLER": str(stub)},
        "stub_artifact": {"locator": str(stub), "sha256": sha(stub)},
        "timeout_seconds": 30,
    }
    source = write_json(tmp_path / "confinement-manifest.json", manifest)

    run, payload = invoke(tmp_path, "installation", "--mode", "private", "--manifest", str(source))

    assert run.returncode == 0, run.stderr
    assert payload["status"] == "PASS"
    assert json.loads(denial_marker.read_text()) == {"result": "permission-denied"}
    assert external_sentinel.read_text() == "preserve\n"
    assert (fixture / "home/.claude/gsd-file-manifest.json").is_file()


def test_baseline_cli_rejects_missing_execution_evidence(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.txt"
    candidate.write_text("candidate\n")
    source = write_json(tmp_path / "claimed-baseline.json", {
        **common(candidate, "baseline"), "completed": True,
        "tests": {"claimed-pass": "PASS"},
    })

    run, payload = invoke(tmp_path, "baseline", "--manifest", str(source))

    assert run.returncode != 0
    assert payload["status"] != "PASS"
    assert payload.get("suite_passed") is not True


def test_upgrade_cli_rejects_missing_and_new_failures(tmp_path: Path) -> None:
    candidate, before = actual_baseline(
        tmp_path, {"missing": "PASS", "new": "PASS"}, "before.json"
    )
    current, observation = actual_suite_artifact(tmp_path, "upgraded-suite", {"new": "FAIL"})
    ledger = write_json(tmp_path / "ledger.json", {"entries": []})
    manifest = common(candidate, "upgrade")
    manifest["current"] = {"suite_artifacts": [observation]}
    manifest["ledger_artifact"] = {"locator": str(ledger), "sha256": sha(ledger)}
    source = write_json(tmp_path / "upgrade.json", manifest)

    run, payload = invoke(tmp_path, "upgrade", "--baseline", str(before), "--manifest", str(source))

    assert run.returncode != 0
    assert payload["status"] == "FAIL"
    assert payload["new_failures"] == ["new"]
    assert payload["missing_tests"] == ["missing"]


def test_installation_cli_rejects_escaped_runtime(tmp_path: Path) -> None:
    fixture = tmp_path / "private-fixture"
    outside = tmp_path / "outside-runtime.sh"
    outside.write_text("#!/usr/bin/env bash\nexit 0\n")
    fixture.mkdir(mode=0o700)
    escaped_stub = fixture / "escaped-upstream-stub.py"
    escaped_stub.write_text(
        "import os\nfrom pathlib import Path\n"
        f"outside = Path({str(outside)!r})\n"
        "root = Path.home() / '.claude'; root.mkdir(parents=True, exist_ok=True)\n"
        "(root / 'gsd-core').symlink_to(outside)\n"
        "(root / 'gsd-file-manifest.json').write_text('{\\\"files\\\": {}}')\n"
    )
    escaped_stub.chmod(0o700)
    sentinel = fixture / "preserved-sentinel"
    sentinel.write_text("preserve\n")
    manifest = common(SETUP, "installation-private")
    manifest["installation"] = {
        "setup_argv": ["/bin/bash", str(SETUP), "--scope", "user"],
        "fixture": {"root": str(fixture), **{key: str(fixture / key) for key in
                    ("home", "codex_home", "cache", "state", "project")}},
        "env": {"FFS_SKIP_PROMPT_MASTER": "1", "FFS_SKIP_SOCRATIC": "1",
                "FFS_GSD_INSTALLER": str(escaped_stub)},
        "stub_artifact": {"locator": str(escaped_stub), "sha256": sha(escaped_stub)},
        "timeout_seconds": 30,
    }
    source = write_json(tmp_path / "escaped-installation.json", manifest)

    run, payload = invoke(tmp_path, "installation", "--mode", "private", "--manifest", str(source))

    assert run.returncode != 0
    assert payload["status"] != "PASS"
    assert "escape" in json.dumps(payload).lower() and "runtime" in json.dumps(payload).lower()
    assert sentinel.read_text() == "preserve\n"


def test_private_installation_timeout_stops_descendant_writes(tmp_path: Path) -> None:
    import time
    fixture = tmp_path / "private-fixture"
    fixture.mkdir(mode=0o700)
    invoked, late = fixture / "child-started", fixture / "late-write"
    stub = fixture / "delayed-stub.py"
    child = f"import time,pathlib; time.sleep(6); pathlib.Path({str(late)!r}).write_text('late')"
    stub.write_text("import pathlib,subprocess,sys,time\n" +
                    f"subprocess.Popen([sys.executable,'-c',{child!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n" +
                    f"pathlib.Path({str(invoked)!r}).write_text('started')\n" + "time.sleep(20)\n")
    stub.chmod(0o700)
    manifest = common(SETUP, "installation-private")
    manifest["installation"] = {
        "setup_argv": ["/bin/bash", str(SETUP), "--scope", "user"],
        "fixture": {"root": str(fixture), **{key: str(fixture / key) for key in
                    ("home", "codex_home", "cache", "state", "project")}},
        "env": {"FFS_SKIP_PROMPT_MASTER": "1", "FFS_SKIP_SOCRATIC": "1",
                "FFS_GSD_INSTALLER": str(stub)},
        "stub_artifact": {"locator": str(stub), "sha256": sha(stub)}, "timeout_seconds": 3,
    }
    source = write_json(tmp_path / "timeout-installation.json", manifest)
    run, payload = invoke(tmp_path, "installation", "--mode", "private", "--manifest", str(source))
    assert invoked.is_file(), "The real installer must reach the delayed upstream boundary"
    assert run.returncode != 0 and payload["status"] != "PASS"
    time.sleep(6.5)
    assert not late.exists(), "Timed-out installer descendants must not continue writing"


@pytest.mark.parametrize("escape", ["active-home", "child-root"])
def test_private_installation_refuses_protected_fixture_roots_before_launch(tmp_path: Path, escape: str) -> None:
    active = tmp_path / "simulated-active-home"
    active.mkdir(mode=0o700)
    sentinel = active / "preserved.txt"
    sentinel.write_text("preserve active state\n")
    fixture = active if escape == "active-home" else tmp_path / "private-fixture"
    fixture.mkdir(mode=0o700, exist_ok=True)
    invoked = tmp_path / "installer-invoked"
    stub = fixture / "upstream-stub.py"
    stub.write_text(f"from pathlib import Path\nPath({str(invoked)!r}).write_text('invoked')\n")
    stub.chmod(0o700)
    roots = {key: str(fixture / key) for key in ("home", "codex_home", "cache", "state", "project")}
    if escape == "child-root":
        roots["codex_home"] = str(active / ".codex")
    manifest = common(SETUP, "installation-private")
    manifest["installation"] = {
        "setup_argv": ["/bin/bash", str(SETUP), "--scope", "user"],
        "fixture": {"root": str(fixture), **roots},
        "env": {"FFS_SKIP_PROMPT_MASTER": "1", "FFS_SKIP_SOCRATIC": "1", "FFS_GSD_INSTALLER": str(stub)},
        "stub_artifact": {"locator": str(stub), "sha256": sha(stub)}, "timeout_seconds": 3,
    }
    source = write_json(tmp_path / "protected-root.json", manifest)
    run, payload = invoke(tmp_path, "installation", "--mode", "private", "--manifest", str(source),
                          env={"HOME": str(active), "CODEX_HOME": str(active / ".codex")})
    assert run.returncode != 0 and payload["status"] != "PASS"
    assert not invoked.exists(), "Invalid fixture roots must be rejected before installer execution"
    assert sentinel.read_text() == "preserve active state\n"
    assert "fixture" in json.dumps(payload).lower()


def test_private_installation_cannot_claim_an_unregistered_same_owner_temp_directory(tmp_path: Path) -> None:
    fixture = tmp_path / "another-attempt"
    fixture.mkdir(mode=0o700)
    sentinel = fixture / "retained-evidence"
    sentinel.write_text("preserve other attempt\n")
    marker = fixture / "unauthorized-installer"
    stub = fixture / "stub.py"
    stub.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('launched')\n")
    manifest = common(SETUP, "installation-private")
    manifest["installation"] = {
        "setup_argv": ["/bin/bash", str(SETUP), "--scope", "user"],
        "fixture": {"root": str(fixture), **{key: str(fixture / key) for key in
                    ("home", "codex_home", "cache", "state", "project")}},
        "env": {"FFS_SKIP_PROMPT_MASTER": "1", "FFS_SKIP_SOCRATIC": "1", "FFS_GSD_INSTALLER": str(stub)},
        "stub_artifact": {"locator": str(stub), "sha256": sha(stub)}, "timeout_seconds": 3,
    }
    source = write_json(tmp_path / "unregistered-installation.json", manifest)
    run, payload = invoke(tmp_path, "installation", "--mode", "private", "--manifest", str(source),
                          env={"FFS_VERIFICATION_AUTHORITY": ""}, authorize_fixture=False)
    assert run.returncode != 0 and payload["status"] != "PASS"
    assert not marker.exists()
    assert sentinel.read_text() == "preserve other attempt\n"
    errors = json.dumps(payload.get("errors", [])).lower()
    assert "authority" in errors or "ownership" in errors


@pytest.mark.parametrize("detach", ["setsid", "spawn-group"])
def test_private_installation_cannot_detach_children_from_timeout_cleanup(tmp_path: Path, detach: str) -> None:
    import time
    fixture = tmp_path / "private-fixture"
    fixture.mkdir(mode=0o700)
    invoked, late = fixture / "detach-attempted", fixture / "detached-late-write"
    stub = fixture / "detach-stub.py"
    late_program = f"import time,pathlib;time.sleep(6);pathlib.Path({str(late)!r}).write_text('escaped cleanup')"
    detach_program = ("os.setsid()\n" + late_program + "\n" if detach == "setsid" else
                      f"pid=os.posix_spawn(sys.executable,[sys.executable,'-c',{late_program!r}],dict(os.environ),setpgroup=0)\nos.waitpid(pid,0)\n")
    stub.write_text("import os,pathlib,sys,time\n" +
                    f"pathlib.Path({str(invoked)!r}).write_text('attempted')\n" + detach_program)
    manifest = common(SETUP, "installation-private")
    manifest["installation"] = {
        "setup_argv": ["/bin/bash", str(SETUP), "--scope", "user"],
        "fixture": {"root": str(fixture), **{key: str(fixture / key) for key in
                    ("home", "codex_home", "cache", "state", "project")}},
        "env": {"FFS_SKIP_PROMPT_MASTER": "1", "FFS_SKIP_SOCRATIC": "1", "FFS_GSD_INSTALLER": str(stub)},
        "stub_artifact": {"locator": str(stub), "sha256": sha(stub)}, "timeout_seconds": 3,
    }
    source = write_json(tmp_path / "detached-installation.json", manifest)
    run, payload = invoke(tmp_path, "installation", "--mode", "private", "--manifest", str(source))
    assert invoked.is_file(), "The contained installer must reach the detach attempt"
    assert run.returncode != 0 and payload["status"] != "PASS"
    time.sleep(6.5)
    assert not late.exists(), "A changed session must not outlive the owned installer sandbox"


def test_private_installation_refuses_hardlinked_input_even_with_fixture_grant(tmp_path: Path) -> None:
    fixture = tmp_path / "private-fixture"
    fixture.mkdir(mode=0o700)
    outside = tmp_path / "outside-retained-evidence"
    outside.write_text("preserve outside inode\n")
    linked = fixture / "linked-input"
    os.link(outside, linked)
    marker = fixture / "installer-started"
    stub = fixture / "hardlink-stub.py"
    stub.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\nPath({str(linked)!r}).write_text('changed outside inode')\n")
    manifest = common(SETUP, "installation-private")
    manifest["installation"] = {
        "setup_argv": ["/bin/bash", str(SETUP), "--scope", "user"],
        "fixture": {"root": str(fixture), **{key: str(fixture / key) for key in
                    ("home", "codex_home", "cache", "state", "project")}},
        "env": {"FFS_SKIP_PROMPT_MASTER": "1", "FFS_SKIP_SOCRATIC": "1", "FFS_GSD_INSTALLER": str(stub)},
        "stub_artifact": {"locator": str(stub), "sha256": sha(stub)}, "timeout_seconds": 3,
    }
    source = write_json(tmp_path / "hardlinked-installation.json", manifest)
    run, payload = invoke(tmp_path, "installation", "--mode", "private", "--manifest", str(source))
    assert run.returncode != 0 and payload["status"] != "PASS"
    assert not marker.exists(), "Hardlinked writable inputs must be refused before running the installer"
    assert outside.read_text() == "preserve outside inode\n"
    errors = json.dumps(payload.get("errors", [])).lower()
    assert "hardlink" in errors or "hard link" in errors


@pytest.mark.parametrize("args", [
    ("baseline",), ("upgrade",), ("installation", "--mode", "public"),
    ("review", "--stage", "nonsense"),
])
def test_missing_or_malformed_mode_input_refuses_with_json_and_no_active_writes(tmp_path: Path, args: tuple[str, ...]) -> None:
    sentinel = tmp_path / "active-profile-sentinel"
    sentinel.write_text("preserve\n")

    run, payload = invoke(tmp_path, *args)

    assert run.returncode != 0
    assert payload["status"] != "PASS"
    assert sentinel.read_text() == "preserve\n"


@pytest.mark.parametrize("behavior", ["empty", "timeout"])
def test_review_adapter_empty_or_timeout_is_unmet(tmp_path: Path, behavior: str) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("print('candidate')\n")
    observed = tmp_path / "adapter-invoked"
    adapter = tmp_path / "incomplete-adapter.py"
    adapter.write_text("import pathlib,time\n" + f"pathlib.Path({str(observed)!r}).write_text('invoked')\n" +
                       ("time.sleep(30)\n" if behavior == "timeout" else ""))
    manifest = common(candidate, "review-upgraded")
    manifest.update({"review_adapter": [sys.executable, str(adapter), str(candidate)],
                     "review_timeout_seconds": 1,
                     "producer": {"host": "producer", "model": "p1", "session": "producer-1"}})
    source = write_json(tmp_path / "incomplete-manifest.json", manifest)
    run, payload = invoke(tmp_path, "review", "--stage", "upgraded", "--purpose", "review-completion",
                          env={"FFS_VERIFICATION_MANIFEST": str(source)})
    assert observed.is_file(), "The requested external review was actually attempted"
    assert run.returncode == 2
    assert payload["status"] == "UNMET"
    assert payload.get("path_admitted") is not True
    assert behavior in json.dumps(payload).lower()
