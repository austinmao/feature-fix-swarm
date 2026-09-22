"""Independent acceptance for installer findings M-01–M-03 and L-01–L-04.

The tests use only private fixtures. Platform launch tests produce real child
effects; fault injection is limited to probe scaffolding and process cleanup.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from test_installer_opus_acceptance import (
    SCHEMA_PATH,
    fixture_paths,
    installation_authority,
    installation_for,
    manifest_for,
    sha,
    subject,
    write_json,
)
from test_verification_modes import ROOT, SETUP, invoke


def _private_installation(tmp_path: Path, stub_text: str = "raise SystemExit(0)\n"):
    module = subject()
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    candidate = source / "setup.sh"
    candidate.write_text("#!/bin/bash\nexit 0\n")
    fixture = tmp_path / "private-fixture"
    fixture.mkdir(mode=0o700)
    stub = fixture / "stub.py"
    stub.write_text(stub_text)
    manifest = manifest_for(candidate)
    installation = installation_for(candidate, fixture, stub, ["/bin/bash", str(candidate)])
    manifest["installation"] = installation
    return module, candidate, fixture, stub, manifest, installation


def _bypass_to_probe(module, candidate: Path, fixture: Path, monkeypatch) -> None:
    monkeypatch.setattr(module, "_authority_installation", lambda *_args: {})

    def stage(_candidate, selected: Path):
        staged = selected / "selected-source"
        staged.mkdir(mode=0o700)
        staged_setup = staged / "setup.sh"
        staged_setup.write_bytes(candidate.read_bytes())
        return staged, staged_setup

    monkeypatch.setattr(module, "_stage_install_source", stage)
    monkeypatch.setattr(
        module,
        "_sandbox_command",
        lambda _fixture, argv, *_args, **_kwargs: list(argv),
    )


def test_m01_linux_launch_uses_verifier_owned_absolute_bash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = subject()
    fixture = tmp_path / "private-fixture"
    fixture.mkdir(mode=0o700)
    staged = fixture / "selected-source"
    staged.mkdir()
    candidate = staged / "setup.sh"
    candidate.write_text("#!/bin/bash\nexit 0\n")
    marker = fixture / "fixture-bash-ran"
    fixture_bin = fixture / "bin"
    fixture_bin.mkdir()
    fake_bash = fixture_bin / "bash"
    fake_bash.write_text(f"#!/bin/sh\nprintf fixture > {marker!s}\n")
    fake_bash.chmod(0o700)

    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/true" if name == "bwrap" else None)
    command = module._sandbox_command(fixture, ["bash", str(candidate)], staged=staged)
    launch = command[-2:]
    observed = subprocess.run(
        launch,
        env={"PATH": f"{fixture_bin}:/usr/bin:/bin"},
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )

    assert launch[0] == "/bin/bash"
    assert observed.returncode == 0
    assert not marker.exists(), "fixture-controlled bin/bash became the interpreter"


def test_m02_undeclared_nested_popen_is_refused_before_child_effect(tmp_path: Path) -> None:
    fixture = tmp_path / "private-fixture"
    fixture.mkdir(mode=0o700)
    marker = fixture / "undeclared-popen-observed.json"
    first_party = ROOT / "tests/fixtures/gsd-installer-stub.py"
    stub = fixture / "popen-stub.py"
    stub.write_text(
        "import json,pathlib,subprocess,sys\n"
        f"child=subprocess.Popen([sys.executable,{str(first_party)!r},*sys.argv[1:]])\n"
        "code=child.wait(timeout=10)\n"
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps({{'exit_status':code}}))\n"
        "raise SystemExit(0)\n"
    )
    manifest = manifest_for(SETUP)
    installation = installation_for(SETUP, fixture, stub, ["/bin/bash", str(SETUP), "--scope", "user"])
    installation["timeout_seconds"] = 30
    # An approved staged copy must not permit reading the original path. This
    # reaches the OS boundary instead of passing through preflight rejection.
    installation["nested_stub_artifact"] = {"locator": str(first_party), "sha256": sha(first_party)}
    manifest["installation"] = installation
    authority = installation_authority(tmp_path, manifest, installation, fixture)
    source = write_json(tmp_path / "m02-manifest.json", manifest)

    run, payload = invoke(
        tmp_path,
        "installation",
        "--mode",
        "private",
        "--manifest",
        str(source),
        env={"FFS_VERIFICATION_AUTHORITY": str(authority)},
        authorize_fixture=False,
        output_name="m02-result.json",
    )

    assert marker.exists(), "the authorized wrapper must actually reach the child attempt"
    assert json.loads(marker.read_text())["exit_status"] != 0, "the original nested stub remained readable"
    assert run.returncode != 0
    assert payload["status"] in {"FAIL", "UNMET"}
    assert not (Path(installation["fixture"]["home"]) / ".claude/gsd-file-manifest.json").exists()
    assert not (Path(installation["fixture"]["codex_home"]) / "gsd-file-manifest.json").exists()


def test_m03_timed_out_probe_group_is_reaped_before_unmet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, candidate, fixture, _stub, manifest, installation = _private_installation(tmp_path)
    _bypass_to_probe(module, candidate, fixture, monkeypatch)
    identity = fixture / "probe-processes.json"
    program = (
        "import json,os,pathlib,subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({'child':child.pid,'group':os.getpgrp()}))\n"
        "time.sleep(30)\n"
    )
    # A real timed-out process leaves a child behind when only
    # its leader is killed. Both the old and replacement runners see this same
    # external child behavior; no runner implementation is monkeypatched.
    monkeypatch.setattr(module, "_sandbox_command",
                        lambda *_args, **_kwargs: [sys.executable, "-c", program, str(identity)])
    with pytest.raises(module.E) as rejected:
        module._run_private(manifest, module.read_checked(candidate), installation)

    assert identity.is_file(), "the probe must run before its timeout is observed"
    ids = json.loads(identity.read_text())
    alive = False
    for _ in range(40):
        observed = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(ids["child"])],
                                  capture_output=True, text=True, timeout=2)
        alive = bool(observed.stdout.strip()) and not observed.stdout.strip().startswith("Z")
        if not alive:
            break
        time.sleep(0.05)
    if alive and os.getpgid(ids["child"]) == ids["group"]:
        os.killpg(ids["group"], signal.SIGKILL)
    assert rejected.value.code == "CONFINEMENT_UNAVAILABLE"
    assert rejected.value.status == "UNMET"
    assert not alive, "the timed-out task-owned probe group survived the refusal"


def test_l01_canary_creation_oserror_is_typed_unmet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, candidate, fixture, _stub, manifest, installation = _private_installation(tmp_path)
    _bypass_to_probe(module, candidate, fixture, monkeypatch)
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **_kwargs: (_ for _ in ()).throw(OSError("read only")))

    with pytest.raises(module.E) as rejected:
        module._run_private(manifest, module.read_checked(candidate), installation)

    assert rejected.value.code == "CONFINEMENT_UNAVAILABLE"
    assert rejected.value.status == "UNMET"


@pytest.mark.parametrize(
    ("case", "expected_status"),
    [("missing_installations", "UNMET"), ("no_match", "UNMET"), ("malformed_selected", "FAIL")],
)
def test_l02_absent_and_malformed_selected_grants_have_stable_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, expected_status: str
) -> None:
    module, _candidate, fixture, _stub, manifest, installation = _private_installation(tmp_path)
    authority = installation_authority(tmp_path, manifest, installation, fixture)
    data = json.loads(authority.read_text())
    if case == "missing_installations":
        del data["installations"]
    elif case == "no_match":
        data["installations"][0]["inode"] += 1
    else:
        data["installations"][0]["initial_entries"] = [{"path": "stub.py", "type": "file"}]
    write_json(authority, data)
    monkeypatch.setenv("FFS_VERIFICATION_AUTHORITY", str(authority))

    with pytest.raises(module.E) as rejected:
        module._authority_installation(manifest, installation, fixture)

    assert rejected.value.status == expected_status


def test_l03_malformed_unrelated_grant_does_not_block_valid_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, _candidate, fixture, _stub, manifest, installation = _private_installation(tmp_path)
    authority = installation_authority(tmp_path, manifest, installation, fixture)
    data = json.loads(authority.read_text())
    data["installations"].insert(0, {"fixture_root": str(tmp_path / "other")})
    write_json(authority, data)
    monkeypatch.setenv("FFS_VERIFICATION_AUTHORITY", str(authority))

    observed = module._authority_installation(manifest, installation, fixture)

    assert observed["."] == ("directory", None)
    assert "stub.py" in observed


def test_l04_schema_matches_runtime_entry_and_absolute_argv_rules(tmp_path: Path) -> None:
    schema = json.loads(SCHEMA_PATH.read_text())
    validator = Draft202012Validator(schema)
    entry = schema["$defs"]["installation_initial_entry"]
    grant = schema["$defs"]["installation_authority_grant"]
    digest = "a" * 64
    base = {
        "fixture_root": str(tmp_path),
        "fixture": {"root": str(tmp_path), **{name: str(tmp_path / name) for name in ("home", "codex_home", "cache", "state", "project")}},
        "device": 1,
        "inode": 1,
        "stub_artifact": {"locator": str(tmp_path / "stub.py"), "sha256": digest},
        "initial_entries": [{"path": "stub.py", "type": "file", "sha256": digest}],
    }

    assert list(validator.evolve(schema=entry).iter_errors({"path": "stub.py", "type": "file"}))
    assert list(validator.evolve(schema=entry).iter_errors({"path": "dir", "type": "directory", "sha256": digest}))
    assert list(validator.evolve(schema=grant).iter_errors({**base, "setup_argv": ["/bin/bash"]}))
    assert list(validator.evolve(schema=grant).iter_errors({**base, "setup_argv": ["bash", str(tmp_path / "setup.sh")]}))
    assert not list(validator.evolve(schema=grant).iter_errors({**base, "setup_argv": ["/bin/bash", str(tmp_path / "setup.sh")]}))
