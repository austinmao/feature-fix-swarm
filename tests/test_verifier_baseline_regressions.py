"""Independent baseline, upgrade, Git, and coverage acceptance boundaries."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import UTC, datetime

import pytest


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts/verification/parallel_host_parity.py"
SCHEMA = "ffs.parallel-host-verification/v1"


def subject():
    spec = importlib.util.spec_from_file_location("baseline_regression_subject", VERIFIER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, sort_keys=True) + "\n")
    return path


def candidate_manifest(candidate: Path, activity: str) -> dict:
    digest = sha(candidate)
    return {"schema": SCHEMA, "binding": {"run": "baseline-audit", "activity": activity, "attempt": "1"},
            "label": "hermetic", "candidate": {"locator": str(candidate), "sha256": digest},
            "provenance": {key: digest for key in ("source_sha256", "binary_sha256", "bundle_sha256", "config_sha256")},
            "ac_ids": ["AC-010"], "path_ids": ["PATH-002"], "int_ids": ["INT-001"]}


def suite_observation(tmp_path: Path, name: str, tests: dict[str, str], *, upgrade_ready: bool = False) -> dict:
    program = tmp_path / f"{name}.py"
    metadata_program = ""
    if upgrade_ready:
        config = write_json(tmp_path / "suite-config.json", {"cases": sorted(tests)})
        dependencies = write_json(tmp_path / "suite-dependencies.json", {"modules": ["json", "hashlib", "pathlib", "sys"]})
        metadata_program = (
            "import hashlib, sys\nfrom pathlib import Path\n"
            "executable = Path(sys.executable).resolve()\n"
            "environment = {'platform': sys.platform, 'python': {"
            "'executable': str(executable), 'executable_sha256': hashlib.sha256(executable.read_bytes()).hexdigest(), "
            "'implementation': sys.implementation.name, 'version': sys.version}}\n"
            "payload['environment'] = json.dumps(environment, sort_keys=True, separators=(',', ':'))\n"
            f"payload['dependencies'] = json.dumps(json.loads(Path({str(dependencies)!r}).read_text()), sort_keys=True, separators=(',', ':'))\n"
            f"payload['config_sha256'] = hashlib.sha256(Path({str(config)!r}).read_bytes()).hexdigest()\n"
        )
    program.write_text("import json\n" + "tests = " + repr(tests) + "\npayload = {'tests': tests}\n" +
                       metadata_program + "print(json.dumps(payload))\n" +
                       "raise SystemExit(1 if 'FAIL' in tests.values() else 0)\n")
    started = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    run = subprocess.run([sys.executable, str(program)], text=True, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, check=False, timeout=10)
    completed = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    observation = json.loads(run.stdout)
    observed = observation["tests"]
    record_value = {"argv": [sys.executable, str(program)],
                        "exit_status": run.returncode, "stdout": run.stdout, "stderr": run.stderr,
                        "started_utc": started, "completed_utc": completed, "tests": observed}
    if upgrade_ready:
        record_value.update({key: observation[key] for key in ("environment", "dependencies", "config_sha256")})
    record = write_json(tmp_path / f"{name}.json", record_value)
    return {"id": name, "locator": str(record), "sha256": sha(record), "argv": [sys.executable, str(program)],
            "exit_status": run.returncode, "started_utc": started, "completed_utc": completed}


def invoke(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    output = tmp_path / "evidence" / ("-".join(args[:2]).replace("/", "_") + ".json")
    run = subprocess.run([sys.executable, str(VERIFIER), *args, "--output", str(output)], text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=15)
    assert run.stdout.strip(), run.stderr
    payload = json.loads(run.stdout)
    if output.exists():
        assert json.loads(output.read_text()) == payload
    return run, payload


def disposable_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=10)
    (repo / "candidate.txt").write_text("candidate\n")
    subprocess.run(["git", "-C", str(repo), "add", "candidate.txt"], check=True, timeout=10)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=fixture", "-c", "user.email=fixture@invalid",
                    "commit", "-qm", "fixture"], check=True, timeout=10)
    return repo


def baseline_file(tmp_path: Path, candidate: Path, tests: dict[str, str] | None = None) -> Path:
    observation = suite_observation(tmp_path, "before", tests or {"existing": "PASS"})
    manifest = candidate_manifest(candidate, "baseline")
    manifest["baseline"] = {"suite_artifacts": [observation]}
    source = write_json(tmp_path / "baseline-input.json", manifest)
    run, payload = invoke(tmp_path, "baseline", "--manifest", str(source))
    assert run.returncode == 0 and payload["status"] == "PASS"
    return tmp_path / "evidence" / "baseline---manifest.json"


def artifact(path: Path) -> dict[str, str]:
    return {"locator": str(path), "sha256": sha(path)}


def complete_full_inventory(tmp_path: Path, manifest: dict) -> dict:
    """Build all eight category wrappers around independently checked underlying bytes."""
    repository = disposable_repo(tmp_path)
    (repository / "lib").mkdir()
    (repository / "lib/inventory.py").write_text("value = 1\n")
    observed_head = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(repository), "update-ref", "refs/remotes/origin/main", observed_head],
                   check=True, timeout=10)
    manifest["repository"] = str(repository)
    stamp = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    suite = suite_observation(tmp_path, "inventory-suite", {"inventory": "PASS"})
    candidate = Path(manifest["candidate"]["locator"])
    binary, bundle, config = (tmp_path / name for name in ("runtime.bin", "bundle.txt", "config.txt"))
    binary.write_bytes(b"binary fixture\n"); bundle.write_text("bundle fixture\n"); config.write_text("config fixture\n")
    manifest["provenance"].update({"source_sha256": sha(candidate), "binary_sha256": sha(binary),
                                   "bundle_sha256": sha(bundle), "config_sha256": sha(config)})
    backup, restored = tmp_path / "backup.bin", tmp_path / "restored.bin"
    backup.write_bytes(b"recoverable bytes\n"); restored.write_bytes(backup.read_bytes())
    verification = write_json(tmp_path / "recovery-verification.json", {"name": "fixture-backup", "status": "PASS",
                              "expected_sha256": sha(backup), "backup_sha256": sha(backup), "restored_sha256": sha(restored)})
    tool = tmp_path / "tool-customization.txt"; tool.write_text("actual tool customization\n")
    coverage = tmp_path / "coverage.xml"
    coverage.write_text('<coverage lines-covered="1" lines-valid="1" branches-covered="0" branches-valid="0"><packages><package><classes><class filename="lib/inventory.py"><lines><line number="1" hits="1"/></lines></class></classes></package></packages></coverage>')
    environment = {}
    for name, value in {"platform": sys.platform, "python": sys.version.split()[0], "dependencies": "fixture-lock", "config": sha(config)}.items():
        record = write_json(tmp_path / f"environment-{name}.json", {"schema": "ffs.environment-observation/v1", "name": name, "value": value})
        environment[name] = {"name": name, "artifact": artifact(record)}
    observations = {
        "ci": {"suite_artifacts": [suite], "head": observed_head, "origin_main": observed_head},
        "python": {"suite_artifacts": [suite]}, "bats": {"suite_artifacts": [suite]},
        "backups_recovery": {"entries": [{"name": "fixture-backup", "expected_sha256": sha(backup),
            "backup_artifact": artifact(backup), "restored_artifact": artifact(restored), "verification_artifact": artifact(verification)}]},
        "source_runtime": {"entries": [{"role": "source", "artifact": artifact(candidate)}, {"role": "binary", "artifact": artifact(binary)},
            {"role": "bundle", "artifact": artifact(bundle)}, {"role": "config", "artifact": artifact(config)}]},
        "tools_customizations": {"entries": [{"name": "fixture-tool", "artifact": artifact(tool)}]},
        "coverage": {"xml_artifact": artifact(coverage), "production_inventory": ["lib/inventory.py"], "suite_artifacts": [suite]},
        "environment": {"entries": list(environment.values())},
    }
    inventory = {}
    for category, observed in observations.items():
        wrapper = write_json(tmp_path / f"inventory-{category}.json", {"schema": "ffs.full-inventory-evidence/v1", "category": category,
                             "status": "PASS", "complete": True, "started_utc": stamp, "completed_utc": stamp,
                             "provenance": manifest["provenance"], "observations": observed})
        inventory[category] = {"status": "PASS", "artifacts": [artifact(wrapper)]}
    return inventory


def test_introduced_failure_is_reported_as_new_not_retained() -> None:
    result = subject().compare_baselines({"completed": True, "tests": {"stable": "PASS", "known": "FAIL"}},
                                         {"completed": True, "tests": {"stable": "PASS", "known": "FAIL", "introduced": "FAIL"}})
    assert result["status"] == "FAIL"
    assert result["new_failures"] == ["introduced"]
    assert result["remaining_failures"] == ["introduced", "known"]


@pytest.mark.parametrize("ledger_kind", ["missing", "unproven"])
def test_upgrade_never_cleans_pass_with_missing_or_forged_ledger(tmp_path: Path, ledger_kind: str) -> None:
    candidate = tmp_path / "candidate"
    candidate.write_text("candidate\n")
    baseline = baseline_file(tmp_path, candidate)
    current = suite_observation(tmp_path, "current", {"existing": "PASS"})
    ledger = tmp_path / "ledger.json"
    if ledger_kind == "unproven":
        missing_proof = tmp_path / "missing-target-proof.json"
        write_json(ledger, {"entries": [{"old_version": "1", "new_version": "2", "source": "fixture",
            "manager": "fixture", "command": ["fixture", "upgrade"], "rollback": "fixture rollback",
            "backup": "fixture backup", "runtime": "fixture runtime",
            "artifacts": [{"locator": str(missing_proof), "sha256": "0" * 64}]}]})
        ledger_hash = sha(ledger)
    else:
        ledger_hash = "a" * 64
    manifest = candidate_manifest(candidate, "upgrade")
    manifest["current"] = {"suite_artifacts": [current]}
    manifest["ledger_artifact"] = {"locator": str(ledger), "sha256": ledger_hash}
    source = write_json(tmp_path / f"upgrade-{ledger_kind}.json", manifest)
    run, payload = invoke(tmp_path, "upgrade", "--baseline", str(baseline), "--manifest", str(source))
    assert run.returncode != 0 and payload["status"] != "PASS"


@pytest.mark.parametrize("mutation", ["schema", "purpose", "provenance"])
def test_upgrade_requires_a_complete_emitted_baseline_envelope(tmp_path: Path, mutation: str) -> None:
    candidate = tmp_path / "candidate"
    candidate.write_text("candidate\n")
    baseline = baseline_file(tmp_path, candidate)
    record = json.loads(baseline.read_text())
    if mutation == "schema": record["schema"] = "forged/v1"
    elif mutation == "purpose": record["purpose"] = "review-completion"
    else: record["provenance"] = {"source_sha256": "0" * 64}
    baseline.write_text(json.dumps(record))
    current = suite_observation(tmp_path, "current", {"existing": "PASS"})
    ledger = write_json(tmp_path / "ledger.json", {"entries": []})
    manifest = candidate_manifest(candidate, "upgrade")
    manifest.update({"current": {"suite_artifacts": [current]}, "ledger_artifact": {"locator": str(ledger), "sha256": sha(ledger)}})
    source = write_json(tmp_path / f"upgrade-{mutation}.json", manifest)
    run, payload = invoke(tmp_path, "upgrade", "--baseline", str(baseline), "--manifest", str(source))
    assert run.returncode != 0 and payload["status"] != "PASS"


def test_boolean_full_inventory_claim_cannot_complete_baseline(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.write_text("candidate\n")
    manifest = candidate_manifest(candidate, "baseline")
    manifest["baseline"] = {"suite_artifacts": [suite_observation(tmp_path, "suite", {"one": "PASS"})]}
    manifest["full_inventory"] = {key: {"verified": True} for key in
                                  ("ci", "python", "bats", "backups_recovery", "source_runtime",
                                   "tools_customizations", "coverage", "environment")}
    source = write_json(tmp_path / "boolean-inventory.json", manifest)
    run, payload = invoke(tmp_path, "baseline", "--manifest", str(source))
    assert run.returncode != 0 or payload["full_baseline_complete"] is False


def test_hashing_inventory_claim_wrappers_does_not_prove_underlying_inventory(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.write_text("candidate\n")
    manifest = candidate_manifest(candidate, "baseline")
    suite = suite_observation(tmp_path, "selected", {"one": "PASS"})
    manifest["baseline"] = {"suite_artifacts": [suite]}
    inventory = {}
    for category in ("ci", "python", "bats", "backups_recovery", "source_runtime",
                     "tools_customizations", "coverage", "environment"):
        claimed = write_json(tmp_path / f"{category}.json", {
            "schema": "ffs.full-inventory-evidence/v1", "category": category,
            "status": "PASS", "complete": True,
            "started_utc": suite["started_utc"], "completed_utc": suite["completed_utc"],
            "provenance": manifest["provenance"],
            "observations": [{"status": "PASS", "complete": True, "count": 1}],
        })
        inventory[category] = {"status": "PASS", "artifacts": [
            {"locator": str(claimed), "sha256": sha(claimed)}]}
    manifest["full_inventory"] = inventory
    source = write_json(tmp_path / "claimed-inventory-wrappers.json", manifest)
    run, payload = invoke(tmp_path, "baseline", "--manifest", str(source))
    assert payload.get("full_baseline_complete") is not True, (
        "Hash-valid self-reports lack CI files, raw test inventories, backup bytes, runtime identities, "
        "environment observations, and production coverage XML; they cannot prove the full baseline"
    )


def test_complete_full_inventory_uses_checked_underlying_bytes(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"; candidate.write_text("print('candidate')\n")
    manifest = candidate_manifest(candidate, "baseline")
    manifest["baseline"] = {"suite_artifacts": [suite_observation(tmp_path, "selected-suite", {"selected": "PASS"})]}
    manifest["full_inventory"] = complete_full_inventory(tmp_path, manifest)
    source = write_json(tmp_path / "complete-inventory.json", manifest)
    run, payload = invoke(tmp_path, "baseline", "--manifest", str(source))
    assert run.returncode == 0, run.stderr
    assert payload["status"] == "PASS" and payload["full_baseline_complete"] is True
    assert payload["full_baseline_unmet"] == []
    assert set(payload["full_inventory_evidence"]) == {"ci", "python", "bats", "backups_recovery", "source_runtime", "tools_customizations", "coverage", "environment"}


def test_clean_upgrade_requires_a_byte_bound_nonempty_ledger(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"; candidate.write_text("print('candidate')\n")
    before = suite_observation(tmp_path, "before-clean", {"stable": "PASS"}, upgrade_ready=True)
    baseline_manifest = candidate_manifest(candidate, "baseline")
    baseline_manifest["baseline"] = {"suite_artifacts": [before]}
    baseline_source = write_json(tmp_path / "baseline-clean.json", baseline_manifest)
    baseline_run, baseline_payload = invoke(tmp_path, "baseline", "--manifest", str(baseline_source))
    assert baseline_run.returncode == 0
    baseline = tmp_path / "evidence" / "baseline---manifest.json"
    current = suite_observation(tmp_path, "current-clean", {"stable": "PASS"}, upgrade_ready=True)
    current["id"] = before["id"]
    entry = {"target": "fixture-package", "old_version": "1.0", "new_version": "2.0", "source": "fixture-index",
             "manager": "fixture-manager", "command": ["fixture-manager", "upgrade", "fixture-package"],
             "rollback": {"command": ["fixture-manager", "rollback"]}, "backup": {"path": "fixture-backup"},
             "runtime": {"python": sys.version.split()[0]}, "recovery": {"status": "PASS"}, "incompatible": False}
    proof = write_json(tmp_path / "ledger-entry-evidence.json", entry)
    ledger = write_json(tmp_path / "complete-ledger.json", {"schema": "ffs.upgrade-ledger/v1", "complete": True,
                        "entries": [{**entry, "evidence": [artifact(proof)]}]})
    manifest = candidate_manifest(candidate, "upgrade")
    manifest["current"] = {"suite_artifacts": [current]}
    manifest["ledger_artifact"] = artifact(ledger)
    source = write_json(tmp_path / "clean-upgrade.json", manifest)
    run, payload = invoke(tmp_path, "upgrade", "--baseline", str(baseline), "--manifest", str(source))
    assert run.returncode == 0, run.stderr
    assert payload["status"] == "PASS" and payload["comparison_passed"] is True
    assert payload["ledger_entries"][0]["target"] == "fixture-package"


@pytest.mark.parametrize("mutation", ["metadata", "empty", "duplicate-id"])
def test_suite_identity_and_metadata_must_be_complete(tmp_path: Path, mutation: str) -> None:
    candidate = tmp_path / "candidate"
    candidate.write_text("candidate\n")
    first = suite_observation(tmp_path, "suite-a", {"one": "PASS"})
    suites = [first]
    if mutation == "metadata": first["argv"] = ["forged"]
    elif mutation == "empty":
        artifact = Path(first["locator"]); write_json(artifact, {**json.loads(artifact.read_text()), "tests": {}}); first["sha256"] = sha(artifact)
    else:
        second = suite_observation(tmp_path, "suite-b", {"two": "PASS"}); second["id"] = first["id"]; suites.append(second)
    manifest = candidate_manifest(candidate, "baseline"); manifest["baseline"] = {"suite_artifacts": suites}
    source = write_json(tmp_path / f"suite-{mutation}.json", manifest)
    run, payload = invoke(tmp_path, "baseline", "--manifest", str(source))
    assert run.returncode != 0 and payload["status"] != "PASS"


def test_local_widening_git_config_refuses_before_inventory_without_index_write(tmp_path: Path) -> None:
    repo = disposable_repo(tmp_path)
    canary = tmp_path / "local-git-canary"
    hook = tmp_path / "hook.py"; hook.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(canary)!r}).write_text('ran')\n"); hook.chmod(0o700)
    subprocess.run(["git", "-C", str(repo), "config", "filter.ffsfixture.clean", str(hook)], check=True, timeout=10)
    index = repo / ".git" / "index"; before = sha(index)
    manifest = candidate_manifest(repo / "candidate.txt", "baseline"); manifest["repository"] = str(repo)
    manifest["baseline"] = {"suite_artifacts": [suite_observation(tmp_path, "suite", {"one": "PASS"})]}
    source = write_json(tmp_path / "git-config-baseline.json", manifest)
    run, payload = invoke(tmp_path, "baseline", "--manifest", str(source))
    assert run.returncode != 0 and payload["status"] != "PASS"
    assert not canary.exists()
    assert sha(index) == before


def test_global_git_config_isolated_and_inventory_does_not_write_index(tmp_path: Path) -> None:
    repo = disposable_repo(tmp_path)
    global_config = tmp_path / "global.gitconfig"; canary = tmp_path / "global-git-canary"
    hook = tmp_path / "global-hook.py"; hook.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(canary)!r}).write_text('ran')\n"); hook.chmod(0o700)
    global_config.write_text("[core]\n\tfsmonitor = " + str(hook) + "\n")
    index = repo / ".git" / "index"; before = sha(index)
    manifest = candidate_manifest(repo / "candidate.txt", "baseline"); manifest["repository"] = str(repo)
    manifest["baseline"] = {"suite_artifacts": [suite_observation(tmp_path, "suite", {"one": "PASS"})]}
    source = write_json(tmp_path / "global-config-baseline.json", manifest)
    env = os.environ.copy(); env["GIT_CONFIG_GLOBAL"] = str(global_config)
    output = tmp_path / "global-evidence.json"
    run = subprocess.run([sys.executable, str(VERIFIER), "baseline", "--manifest", str(source), "--output", str(output)],
                         text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False, timeout=15)
    assert run.returncode == 0 and json.loads(run.stdout)["status"] == "PASS"
    assert not canary.exists() and sha(index) == before


@pytest.mark.parametrize("xml", [
    '<!DOCTYPE coverage [<!ENTITY xxe "no">]><coverage lines-covered="1" lines-valid="1" branches-covered="0" branches-valid="0"/>',
    '<coverage lines-covered="2" lines-valid="2" branches-covered="0" branches-valid="0"><packages><package><classes><class filename="../escape.py"><lines><line number="1" hits="1"/></lines></class><class filename="../escape.py"><lines><line number="2" hits="1"/></lines></class></classes></package></packages></coverage>',
    '<coverage lines-covered="2" lines-valid="2" branches-covered="0" branches-valid="0"><packages><package><classes><class filename="lib/a.py"><lines><line number="1" hits="1"/></lines></class></classes></package></packages></coverage>',
])
def test_coverage_rejects_dtd_and_per_file_headline_mismatches(tmp_path: Path, xml: str) -> None:
    report = tmp_path / "coverage.xml"; report.write_text(xml)
    with pytest.raises(Exception):
        subject().coverage_totals(report)


def test_coverage_input_is_bounded(tmp_path: Path) -> None:
    report = tmp_path / "oversized.xml"; report.write_bytes(b"<coverage " + b"x" * (2 * 1024 * 1024) + b"/>")
    with pytest.raises(Exception):
        subject().coverage_totals(report)
