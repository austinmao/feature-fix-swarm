"""Independent native-runtime regression for managed Python upgrades."""
import subprocess
import sys
from pathlib import Path
import pytest
from test_installer_opus_acceptance import subject

@pytest.mark.skipif(sys.platform != "darwin", reason="macOS framework launcher boundary")
def test_current_native_python_runs_when_posix_spawn_is_denied(tmp_path):
    module = subject()
    profile = tmp_path / "native.sb"
    profile.write_text('(version 1)\n(allow default)\n(deny syscall-unix (syscall-number SYS_posix_spawn))\n')
    native = module._native_python()
    result = subprocess.run(
        ["/usr/bin/sandbox-exec", "-f", str(profile), native, "-c",
         "import json,sys; print(json.dumps(list(sys.version_info[:3])))"],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    import json
    assert json.loads(result.stdout) == list(sys.version_info[:3]), "selected stale runtime"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS framework launcher boundary")
def test_framework_prefix_alias_returns_canonical_runtime_and_accepts_owned_link(tmp_path, monkeypatch):
    module = subject()
    real_prefix = Path(sys.base_prefix).resolve()
    alias = tmp_path / "homebrew-opt-prefix"
    alias.symlink_to(real_prefix, target_is_directory=True)
    monkeypatch.setattr(module.sys, "base_prefix", str(alias))

    native = Path(module._native_python())
    assert native == native.resolve(), "runtime identity must canonicalize a trusted prefix alias"
    fixture = tmp_path / "private-fixture"
    (fixture / "bin").mkdir(parents=True)
    (fixture / "bin" / "python3").symlink_to(native)
    assert module._tree(fixture)["bin/python3"][0] == "runtime-link"
