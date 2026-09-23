"""Coverage configuration contracts.

These tests deliberately run only a tiny parent/child process pair.  They do
not represent the project's coverage result; the CI coverage command does.
"""

from __future__ import annotations

import configparser
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import types

from coverage import CoverageData


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "tests" / "coverage-parallel.ini"


def _config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read(CONFIG)
    return parser


def _full_suite_collector():
    coverage_dir = ROOT / "tests" / "coverage"
    spec = importlib.util.spec_from_file_location("coverage_full_suite_contract", coverage_dir / "run-full-suite.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    previous = list(sys.path)
    try:
        sys.path.insert(0, str(coverage_dir))
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = previous
    return module


def _entries(parser: configparser.ConfigParser, section: str, option: str) -> set[str]:
    return {
        line.strip()
        for line in parser.get(section, option).splitlines()
        if line.strip()
    }


def _production_python_files() -> set[Path]:
    excluded = {"tests", "vendor", ".staging", "X", "node_modules", "__pycache__"}
    return {
        path.relative_to(ROOT)
        for path in ROOT.rglob("*.py")
        if not excluded.intersection(path.relative_to(ROOT).parts)
    }


def test_coverage_configuration_measures_the_full_first_party_tree() -> None:
    config = _config()

    assert _entries(config, "run", "source") == {"."}
    assert config.getboolean("run", "branch")
    assert config.getboolean("run", "parallel")
    assert config.get("run", "concurrency") == "multiprocessing"
    assert config.get("run", "patch") == "subprocess"
    assert config.getboolean("report", "include_namespace_packages")
    # With branch=True, Coverage.py's fail_under applies to the combined
    # line-and-branch percentage.  The release gate therefore checks XML's
    # line-rate explicitly after the full suite has produced it.
    assert not config.has_option("report", "fail_under")

    omitted = _entries(config, "report", "omit")
    for path in (
        "tests/*", "*/tests/*", "vendor/*", "*/vendor/*",
        ".staging/*", "*/.staging/*", "X/*", "*/X/*",
    ):
        assert path in omitted
    assert not any("lib" in path or "scripts" in path or "skills" in path for path in omitted)


def test_source_scope_contains_every_current_production_python_file() -> None:
    inventory = _production_python_files()
    assert Path("lib/host_capabilities.py") in inventory
    assert all(path.parts[0] in {"lib", "scripts", "skills"} for path in inventory)


def test_full_suite_binds_inherited_child_coverage_to_checkout_root(tmp_path: Path) -> None:
    """Fixture-repository children must not resolve ``source = .`` to themselves."""
    collector = _full_suite_collector()
    generated = collector._project_coverage_config(tmp_path)
    parser = configparser.ConfigParser()
    parser.read(generated)
    assert _entries(parser, "run", "source") == {str(ROOT)}
    # The portable checked-in config remains the source of truth; only the
    # attempt-owned copy is made absolute for inherited Bats children.
    assert _entries(_config(), "run", "source") == {"."}


def test_full_suite_gates_line_rate_and_reports_branch_rate_separately(tmp_path: Path) -> None:
    collector = _full_suite_collector()
    low_line = tmp_path / "low-line.xml"
    low_line.write_text('<coverage line-rate="0.799" branch-rate="0.999"/>')
    low_metrics = collector._coverage_metrics(low_line)
    assert low_metrics == {
        "line_rate": 0.799,
        "branch_rate": 0.999,
        "line_coverage_floor_passed": False,
    }

    low_branch = tmp_path / "low-branch.xml"
    low_branch.write_text('<coverage line-rate="0.800" branch-rate="0.001"/>')
    high_line_metrics = collector._coverage_metrics(low_branch)
    assert high_line_metrics == {
        "line_rate": 0.8,
        "branch_rate": 0.001,
        "line_coverage_floor_passed": True,
    }


def test_full_suite_requires_a_sealed_registered_runtime_descriptor(tmp_path: Path) -> None:
    collector = _full_suite_collector()
    descriptor = tmp_path / "upstream-runtime.json"
    descriptor.write_bytes(b'{"schema":"fixture"}\n')
    digest = hashlib.sha256(descriptor.read_bytes()).hexdigest()
    valid = {
        collector.RUNTIME_DESCRIPTOR_ENV: str(descriptor),
        collector.RUNTIME_DESCRIPTOR_SHA256_ENV: digest,
    }
    assert collector._registered_runtime_input_error({}) == (
        "FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR and "
        "FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR_SHA256 are required"
    )
    assert collector._registered_runtime_input_error({
        collector.RUNTIME_DESCRIPTOR_ENV: str(descriptor),
        collector.RUNTIME_DESCRIPTOR_SHA256_ENV: "0" * 64,
    }) == "FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR_SHA256 does not match descriptor bytes"
    assert collector._registered_runtime_input_error(valid) is None


def test_full_suite_refuses_missing_runtime_before_claiming_output(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    collector = _full_suite_collector()
    output = tmp_path / "coverage-output"
    monkeypatch.delenv(collector.RUNTIME_DESCRIPTOR_ENV, raising=False)
    monkeypatch.delenv(collector.RUNTIME_DESCRIPTOR_SHA256_ENV, raising=False)
    monkeypatch.setattr(sys, "argv", ["run-full-suite.py", "--output", str(output)])

    assert collector.main() == 1

    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == (
        "FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR and "
        "FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR_SHA256 are required"
    )
    assert not output.exists()


def test_private_startup_shim_chains_only_base_prefix_sitecustomize(
    tmp_path: Path, monkeypatch
) -> None:
    """The coverage hook must not suppress interpreter setup or load fixture code."""
    collector = _full_suite_collector()
    shim = collector.write_private_startup_shim(tmp_path)
    base = tmp_path / "base"
    stdlib = base / "lib" / "python" / "stdlib"
    stdlib.mkdir(parents=True)
    base_marker = tmp_path / "base-ran"
    (stdlib / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(base_marker)!r}).write_text('base')\n"
    )
    hostile = tmp_path / "fixture"
    hostile.mkdir()
    hostile_marker = tmp_path / "hostile-ran"
    (hostile / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(hostile_marker)!r}).write_text('hostile')\n"
    )
    startup_calls: list[str] = []
    coverage = types.ModuleType("coverage")
    coverage.process_startup = lambda: startup_calls.append("coverage")  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "base_prefix", str(base))
    monkeypatch.setattr(sysconfig, "get_path", lambda name: str(stdlib) if name == "stdlib" else None)
    monkeypatch.setitem(sys.modules, "coverage", coverage)
    monkeypatch.chdir(hostile)

    namespace = {"__name__": "sitecustomize", "__file__": str(shim / "sitecustomize.py")}
    exec((shim / "sitecustomize.py").read_text(), namespace)

    assert base_marker.read_text() == "base"
    assert not hostile_marker.exists()
    assert startup_calls == ["coverage"]


def test_subprocess_patch_creates_distinct_parent_and_child_data_files(tmp_path: Path) -> None:
    """A patched subprocess must contribute its own parallel data file."""
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys\n"
        "subprocess.run([sys.executable, '-c', "
        "'from lib.model_requests import resolve_request; "
        "resolve_request({\\\"kind\\\": \\\"tier\\\", \\\"name\\\": \\\"volume\\\"})'"
        "], check=True)\n"
    )
    data_file = tmp_path / "coverage-data"
    environment = {"COVERAGE_FILE": str(data_file), "PYTHONPATH": str(ROOT)}
    result = subprocess.run(
        [sys.executable, "-m", "coverage", "run", "--rcfile", str(CONFIG), str(parent)],
        cwd=ROOT,
        env={**os.environ, **environment},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    data_files = list(tmp_path.glob("coverage-data.*"))
    assert len(data_files) >= 2, "parent and patched child must retain separate data files"

    combined = subprocess.run(
        [sys.executable, "-m", "coverage", "combine", "--keep", "--rcfile", str(CONFIG), str(tmp_path)],
        cwd=ROOT,
        env={**os.environ, **environment},
        capture_output=True,
        text=True,
    )
    assert combined.returncode == 0, combined.stderr
    recorded = CoverageData(basename=str(data_file))
    recorded.read()
    assert recorded.lines(str(ROOT / "lib/model_requests.py")), "child source lines were not recorded"
