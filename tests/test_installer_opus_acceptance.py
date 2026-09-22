"""Independent acceptance regressions for the Opus installer review.

The cases in this module keep every write inside ``tmp_path``.  They exercise
the verifier as a module where launching a real platform sandbox would make a
validation-order regression host-dependent.
"""
from __future__ import annotations

from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = Path(os.environ.get(
    "FFS_INSTALLER_VERIFIER_UNDER_TEST",
    ROOT / "scripts/verification/parallel_host_parity.py",
))
SCHEMA_PATH = Path(os.environ.get(
    "FFS_INSTALLER_SCHEMA_UNDER_TEST",
    ROOT / "schemas/parallel-host-verification.schema.json",
))
SCHEMA = "ffs.parallel-host-verification/v1"


def sha(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, sort_keys=True) + "\n")
    return path


def subject():
    name = "installer_opus_subject_" + sha256(str(VERIFIER).encode()).hexdigest()[:12]
    spec = importlib.util.spec_from_file_location(name, VERIFIER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def manifest_for(candidate: Path) -> dict:
    digest = sha(candidate)
    return {
        "schema": SCHEMA,
        "binding": {"run": "installer-opus", "activity": "installation-private", "attempt": "1"},
        "label": "hermetic",
        "candidate": {"locator": str(candidate), "sha256": digest},
        "provenance": {key: digest for key in (
            "source_sha256", "binary_sha256", "bundle_sha256", "config_sha256"
        )},
        "ac_ids": ["AC-010"],
        "path_ids": ["PATH-002"],
        "int_ids": ["INT-001"],
    }


def fixture_paths(fixture: Path) -> dict[str, str]:
    return {"root": str(fixture), **{
        key: str(fixture / key) for key in ("home", "codex_home", "cache", "state", "project")
    }}


def installation_for(candidate: Path, fixture: Path, stub: Path, setup_argv: list[str]) -> dict:
    return {
        "setup_argv": setup_argv,
        "fixture": fixture_paths(fixture),
        "env": {
            "FFS_SKIP_PROMPT_MASTER": "1",
            "FFS_SKIP_SOCRATIC": "1",
            "FFS_GSD_INSTALLER": str(stub),
        },
        "stub_artifact": {"locator": str(stub), "sha256": sha(stub)},
        "timeout_seconds": 3,
    }


def installation_authority(
    tmp_path: Path,
    manifest: dict,
    installation: dict,
    fixture: Path,
    *,
    parent_name: str = "installation-supervisor",
) -> Path:
    entries = []
    for path in sorted(fixture.rglob("*")):
        assert not path.is_symlink()
        entry = {
            "path": path.relative_to(fixture).as_posix(),
            "type": "directory" if path.is_dir() else "file",
        }
        if path.is_file():
            entry["sha256"] = sha(path)
        entries.append(entry)
    supervisor = tmp_path / parent_name
    supervisor.mkdir(mode=0o700)
    supervisor.chmod(0o700)
    authority = write_json(supervisor / "authority.json", {
        "schema": "ffs.verification-repair-authority/v1",
        "run": manifest["binding"]["run"],
        "candidate_sha256": manifest["candidate"]["sha256"],
        "assignments": [],
        "installations": [{
            "fixture_root": str(fixture.resolve()),
            "device": fixture.stat().st_dev,
            "inode": fixture.stat().st_ino,
            "setup_argv": installation["setup_argv"],
            "stub_artifact": installation["stub_artifact"],
            "initial_entries": entries,
            "fixture": installation["fixture"],
            **({"nested_stub_artifact": installation["nested_stub_artifact"]}
               if "nested_stub_artifact" in installation else {}),
        }],
    })
    authority.chmod(0o600)
    return authority


@pytest.mark.parametrize("root_name", ["home", "codex_home", "cache", "state", "project"])
def test_f2_each_private_root_rejects_parent_traversal_before_creation(
    tmp_path: Path, root_name: str
) -> None:
    module = subject()
    fixture = tmp_path / "private-fixture"
    fixture.mkdir(mode=0o700)
    escaped = tmp_path / f"escaped-{root_name}"
    lexical_escape = fixture / ".." / escaped.name

    with pytest.raises(module.E) as rejected:
        path = module._private_path(str(lexical_escape), fixture, root_name)
        path.mkdir(mode=0o700, parents=True)

    assert rejected.value.code == "FIXTURE_PATH"
    assert not escaped.exists(), "validation must not create a directory outside the fixture"


def test_f3_setup_argv_must_select_the_candidate_before_any_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = subject()
    source = tmp_path / "selected-source"
    source.mkdir(mode=0o700)
    candidate = source / "setup.sh"
    candidate.write_text("#!/bin/bash\nexit 0\n")
    fixture = tmp_path / "private-fixture"
    fixture.mkdir(mode=0o700)
    stub = fixture / "approved-stub.py"
    stub.write_text("raise SystemExit(0)\n")
    bypass = fixture / "grant-approved-bypass.py"
    bypass.write_text("raise SystemExit(0)\n")
    manifest = manifest_for(candidate)
    installation = installation_for(candidate, fixture, stub, [sys.executable, str(bypass)])
    manifest["installation"] = installation
    authority = installation_authority(tmp_path, manifest, installation, fixture)
    monkeypatch.setenv("FFS_VERIFICATION_AUTHORITY", str(authority))

    def fake_stage(_candidate, selected_fixture: Path):
        staged = selected_fixture / "selected-source"
        staged.mkdir(mode=0o700)
        staged_setup = staged / "setup.sh"
        staged_setup.write_bytes(candidate.read_bytes())
        return staged, staged_setup

    launched: list[list[str]] = []

    def fake_run(command, _cwd, _timeout, _env):
        launched.append(command)
        home = Path(installation["fixture"]["home"]) / ".claude"
        codex = Path(installation["fixture"]["codex_home"])
        for runtime in (home, codex):
            runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_json(runtime / "gsd-file-manifest.json", {"files": {}})
        return 0, b"", b""

    monkeypatch.setattr(module, "_stage_install_source", fake_stage)
    monkeypatch.setattr(module, "_sandbox_command", lambda _fixture, argv, *_args, **_kwargs: list(argv))
    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
    )

    candidate_item = module.read_checked(candidate)
    with pytest.raises(module.E) as rejected:
        module._run_private(manifest, candidate_item, installation)

    assert rejected.value.code == "INSTALL_ARGV"
    assert not launched, "an argv that omits the selected candidate must be refused before launch"


def _minimal_authorized(tmp_path: Path):
    candidate = tmp_path / 'setup.sh'
    candidate.write_text('#!/bin/bash\nexit 0\n')
    fixture = tmp_path / 'private-fixture'
    fixture.mkdir(mode=0o700)
    stub = fixture / 'stub.py'
    stub.write_text('raise SystemExit(0)\n')
    manifest = manifest_for(candidate)
    installation = installation_for(candidate, fixture, stub, ['/bin/bash', str(candidate)])
    manifest['installation'] = installation
    authority = installation_authority(tmp_path, manifest, installation, fixture)
    return manifest, installation, fixture, authority


@pytest.mark.parametrize('case', ['public_file', 'public_parent', 'registered_workspace'])
def test_f4_installation_authority_requires_private_supervisor_store(tmp_path, monkeypatch, case):
    module = subject()
    manifest, installation, fixture, authority = _minimal_authorized(tmp_path)
    monkeypatch.setenv('FFS_VERIFICATION_AUTHORITY', str(authority))
    if case == 'public_file':
        authority.chmod(0o644)
    elif case == 'public_parent':
        authority.parent.chmod(0o755)
    else:
        monkeypatch.setenv('FFS_REGISTERED_WORKSPACES', str(authority.parent))
    with pytest.raises(module.E):
        module._authority_installation(manifest, installation, fixture)


def test_f2_authority_binds_each_private_root(tmp_path, monkeypatch):
    module = subject()
    manifest, installation, fixture, authority = _minimal_authorized(tmp_path)
    monkeypatch.setenv('FFS_VERIFICATION_AUTHORITY', str(authority))
    installation['fixture']['home'] = str(fixture / 'different-home')
    with pytest.raises(module.E):
        module._authority_installation(manifest, installation, fixture)


def test_f7_output_dangling_symlink_cannot_select_another_destination(tmp_path):
    module = subject()
    parent = tmp_path / 'evidence'
    parent.mkdir(mode=0o700)
    destination = parent / 'unselected.json'
    output = parent / 'selected.json'
    output.symlink_to(destination)
    with pytest.raises(module.E):
        module.save(output, b'{}\n')
    assert output.is_symlink()
    assert not destination.exists()


def test_f5_real_installation_pass_conforms_to_published_schema(tmp_path):
    from jsonschema import Draft202012Validator
    from test_verification_modes import test_installation_cli_runs_real_private_installer
    test_installation_cli_runs_real_private_installer(tmp_path)
    result = json.loads((tmp_path / 'external-evidence/result.json').read_text())
    assert result['status'] == 'PASS'
    errors = list(Draft202012Validator(json.loads(SCHEMA_PATH.read_text())).iter_errors(result))
    assert not errors, [(list(error.path), error.validator) for error in errors]


def test_f8_nested_dependency_digest_is_checked_before_staging(tmp_path, monkeypatch):
    module = subject()
    manifest, installation, fixture, authority = _minimal_authorized(tmp_path)
    nested = tmp_path / 'tests/fixtures/gsd-installer-stub.py'
    nested.parent.mkdir(parents=True)
    nested.write_text('raise SystemExit(0)\n')
    descriptor = {'locator': str(nested), 'sha256': sha(nested)}
    installation['nested_stub_artifact'] = descriptor
    grant = json.loads(authority.read_text())
    grant['installations'][0]['nested_stub_artifact'] = descriptor
    write_json(authority, grant)
    nested.write_text('raise SystemExit(1)\n')
    monkeypatch.setenv('FFS_VERIFICATION_AUTHORITY', str(authority))
    staged = []
    def premature_stage(*args):
        staged.append(args)
        raise AssertionError('source staging preceded dependency digest verification')
    monkeypatch.setattr(module, '_stage_install_source', premature_stage)
    with pytest.raises(module.E):
        module._run_private(manifest, module.read_checked(manifest['candidate']['locator']), installation)
    assert not staged


@pytest.mark.parametrize('case', ['traversal', 'candidate_omitted'])
def test_f2_f3_cli_refuses_malformed_installation_before_creating_roots(tmp_path, case):
    from test_verification_modes import invoke
    manifest, installation, fixture, authority = _minimal_authorized(tmp_path)
    escaped = tmp_path / 'escaped-home'
    if case == 'traversal':
        installation['fixture']['home'] = str(fixture / '..' / escaped.name)
    else:
        installation['setup_argv'] = ['bash', str(fixture / 'stub.py')]
    source = write_json(tmp_path / 'manifest.json', manifest)
    run, payload = invoke(tmp_path, 'installation', '--mode', 'private', '--manifest', str(source))
    assert run.returncode != 0
    assert any(e['code'] in {'FIXTURE_PATH', 'INSTALL_ARGV'} for e in payload['errors']), payload
    assert not escaped.exists()
    assert not (fixture / 'home').exists()
