"""Independent check that private installation executes its selected shell entrypoint."""
from pathlib import Path
import shlex
import shutil
import time

from test_verification_modes import ROOT, common, invoke, sha, write_json
from test_verification_modes import test_installation_cli_runs_real_private_installer as _run_real_install_fixture


def _selected_source(tmp_path: Path) -> Path:
    source_root = tmp_path / "selected-source"
    source_root.mkdir()
    for relative in ("setup.sh", "lib", "scripts/gsd", "skills", "patches", "data/installer", "package.json", "package-lock.json",
                     "node_modules/@opengsd/gsd-core/package.json"):
        origin, destination = ROOT / relative, source_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if origin.is_dir():
            shutil.copytree(origin, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(origin, destination)
    return source_root


def test_private_installation_executes_selected_setup_bytes(tmp_path: Path) -> None:
    source_root = _selected_source(tmp_path)
    fixture = tmp_path / "private-fixture"
    fixture.mkdir(mode=0o700)
    marker = fixture / "selected-entrypoint-ran"
    setup = source_root / "setup.sh"
    shebang, body = setup.read_text().split("\n", 1)
    setup.write_text(shebang + "\nprintf 'selected-entrypoint\\n' > " + shlex.quote(str(marker)) + "\n" + body)
    stub = fixture / "upstream-stub.py"
    shutil.copy2(ROOT / "tests/fixtures/gsd-installer-stub.py", stub)
    manifest = common(setup, "installation-private")
    manifest["installation"] = {
        "setup_argv": ["/bin/bash", str(setup), "--scope", "user"],
        "fixture": {"root": str(fixture), **{key: str(fixture / key) for key in
                    ("home", "codex_home", "cache", "state", "project")}},
        "env": {"FFS_SKIP_PROMPT_MASTER": "1", "FFS_SKIP_SOCRATIC": "1", "FFS_GSD_INSTALLER": str(stub)},
        "stub_artifact": {"locator": str(stub), "sha256": sha(stub)}, "timeout_seconds": 30,
    }
    selected_hash = sha(setup)
    manifest_file = write_json(tmp_path / "selected-installation.json", manifest)
    run, payload = invoke(tmp_path, "installation", "--mode", "private", "--manifest", str(manifest_file))
    assert run.returncode == 0, payload
    assert marker.is_file(), "PASS must come from the selected setup.sh, not a direct library invocation"
    assert marker.read_text() == "selected-entrypoint\n"
    assert sha(setup) == selected_hash


def _stub_manifest(tmp_path: Path, program: str, source_root: Path = ROOT) -> tuple[Path, Path]:
    fixture = tmp_path / "private-fixture"
    fixture.mkdir(mode=0o700)
    stub = fixture / "boundary-stub.py"
    stub.write_text(program)
    manifest = common(source_root / "setup.sh", "installation-private")
    manifest["installation"] = {
        "setup_argv": ["/bin/bash", str(source_root / "setup.sh"), "--scope", "user"],
        "fixture": {"root": str(fixture), **{key: str(fixture / key) for key in
                    ("home", "codex_home", "cache", "state", "project")}},
        "env": {"FFS_SKIP_PROMPT_MASTER": "1", "FFS_SKIP_SOCRATIC": "1", "FFS_GSD_INSTALLER": str(stub)},
        "stub_artifact": {"locator": str(stub), "sha256": sha(stub)}, "timeout_seconds": 3,
    }
    return write_json(tmp_path / "boundary-installation.json", manifest), fixture


def test_private_installation_bounds_entrypoint_output_before_timeout(tmp_path: Path) -> None:
    source_root = _selected_source(tmp_path)
    setup = source_root / "setup.sh"
    shebang, body = setup.read_text().split("\n", 1)
    program = "import sys,time;sys.stdout.write('x' * (2 * 1024 * 1024));sys.stdout.flush();time.sleep(20)"
    setup.write_text(shebang + "\npython3 -c " + shlex.quote(program) + "\n" + body)
    manifest, _ = _stub_manifest(tmp_path, "raise SystemExit(0)\n", source_root)
    run, payload = invoke(tmp_path, "installation", "--mode", "private", "--manifest", str(manifest))
    assert run.returncode != 0
    assert any(error["code"].endswith("OUTPUT_LIMIT") for error in payload["errors"]), payload


def test_private_installation_reaps_children_after_successful_parent_exit(tmp_path: Path) -> None:
    fixture = tmp_path / "private-fixture"
    marker, late = fixture / "child-started", fixture / "orphan-late-write"
    child = f"import pathlib,time;time.sleep(5);pathlib.Path({str(late)!r}).write_text('survived')"
    program = ("import pathlib,subprocess,sys\n"
        f"result=subprocess.run([sys.executable,{str(ROOT / 'tests/fixtures/gsd-installer-stub.py')!r},*sys.argv[1:]])\n"
        "if result.returncode: raise SystemExit(result.returncode)\n"
        f"subprocess.Popen([sys.executable,'-c',{child!r}],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        f"pathlib.Path({str(marker)!r}).write_text('spawned')\n")
    manifest, _ = _stub_manifest(tmp_path, program)
    invoke(tmp_path, "installation", "--mode", "private", "--manifest", str(manifest))
    assert marker.is_file(), "the fixture must actually spawn the ordinary child"
    time.sleep(5.5)
    assert not late.exists(), "successful installer exit cannot leave a writer running in its fixture"


def test_private_installation_preserves_unowned_probe_paths(tmp_path: Path) -> None:
    unowned = tmp_path / "private-fixture.outside-write-canary"
    unowned.mkdir(mode=0o700)
    sentinel = unowned / "sentinel"
    sentinel.write_text("existing unrelated bytes\n")
    digest = sha(sentinel)
    _run_real_install_fixture(tmp_path)
    assert sha(sentinel) == digest, "preflight cannot overwrite a sibling not granted in fixture authority"
