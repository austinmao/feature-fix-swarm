"""The private GSD runtime descriptor is registrable, immutable, and drift-refusing."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from run_state import cli
from run_state.upstream import UpstreamRefused, UpstreamRuntime, _REQUIRED_MODULES, describe_runtime

ROOT = Path(__file__).resolve().parents[1]
MODULE_ROOT = ROOT / "node_modules" / "@opengsd" / "gsd-core" / "gsd-core" / "bin" / "lib"


def _private_closure(tmp_path: Path) -> tuple[Path, Path]:
    if not MODULE_ROOT.is_dir():
        pytest.skip("UNMET: private gsd-core closure is not installed under node_modules")
    root = tmp_path / "closure"
    for relative in _REQUIRED_MODULES:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(MODULE_ROOT / relative, target)
    node = Path(shutil.which("node") or "")
    if not node.is_file():
        pytest.skip("UNMET: node binary required to describe a runtime")
    return root, node


def test_descriptor_binds_every_required_module_and_verifies(tmp_path, capsys):
    root, node = _private_closure(tmp_path)
    output = tmp_path / "upstream-runtime.json"
    assert cli.main(["describe-upstream-runtime", "--module-root", str(root), "--node", str(node),
                     "--output", str(output)]) == 0
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    raw = output.read_bytes()
    assert printed["sha256"] == hashlib.sha256(raw).hexdigest() and printed["version"] == "1.14.0"
    assert os.stat(output).st_mode & 0o777 == 0o600
    descriptor = json.loads(raw)
    assert set(descriptor["modules"]) == set(_REQUIRED_MODULES)
    UpstreamRuntime.from_manifest(descriptor).verify()
    args = type("Args", (), {"upstream_runtime_manifest": str(output), "upstream_runtime_sha256": printed["sha256"]})()
    assert cli._load_upstream_runtime(args)[1] == printed["sha256"]
    # The registered descriptor is immutable: a second registration at the same path refuses.
    assert cli.main(["describe-upstream-runtime", "--module-root", str(root), "--node", str(node),
                     "--output", str(output)]) == 2


def test_module_drift_refuses_the_registered_descriptor(tmp_path):
    root, node = _private_closure(tmp_path)
    descriptor = describe_runtime(root, node)
    runtime = UpstreamRuntime.from_manifest(descriptor)
    victim = root / sorted(_REQUIRED_MODULES)[0]
    victim.write_bytes(victim.read_bytes() + b"\n// drift\n")
    with pytest.raises(UpstreamRefused, match="UPSTREAM_RUNTIME_DRIFT"):
        runtime.verify()
    with pytest.raises(UpstreamRefused):
        describe_runtime(root / "missing", node)
