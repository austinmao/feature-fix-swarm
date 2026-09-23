#!/usr/bin/env python3
"""Private cross-interpreter coverage collector for Bash-launched Python."""
from __future__ import annotations
import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "tests/coverage-parallel.ini"
# This command has a real successful CLI path on the oldest supported macOS
# interpreter (3.9) and the current interpreter.  It is deliberately a
# first-party production script, not a one-line test helper.
TARGET = ROOT / "lib/model_requests.py"

# The coverage shim is imported as ``sitecustomize`` because Python's startup
# machinery only provides that hook.  Some interpreter distributions also use
# an interpreter-owned sitecustomize module to add their package locations.
# Chaining it by import name would recurse into this shim or select a fixture's
# module from PYTHONPATH, so load only the standard-library candidate beneath
# the current interpreter's base prefix.
STARTUP_SHIM = """\\
import importlib.util
from pathlib import Path
import sys
import sysconfig

def _run_base_sitecustomize():
    base = Path(sys.base_prefix).resolve()
    try:
        candidate = (Path(sysconfig.get_path("stdlib")) / "sitecustomize.py").resolve()
        candidate.relative_to(base)
    except (OSError, TypeError, ValueError):
        return
    if not candidate.is_file():
        return
    spec = importlib.util.spec_from_file_location("_ffs_base_sitecustomize", candidate)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

_run_base_sitecustomize()
import coverage
coverage.process_startup()
"""


def write_private_startup_shim(output: Path) -> Path:
    """Write the owned startup shim without shadowing base interpreter setup."""
    shim = output / "startup"
    shim.mkdir(exist_ok=True)
    (shim / "sitecustomize.py").write_text(STARTUP_SHIM)
    return shim


def _collector_environment() -> dict[str, str]:
    """Do not inherit an outer pytest/coverage subprocess instrumentation plan.

    The private collector selects a package compatible with every interpreter.
    A parent coverage version's serialized config can override its data path or
    invoke an API absent from that private copy.
    """
    excluded = ("COVERAGE_", "COV_CORE_", "PYTEST_COV")
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(excluded) and key not in {"PYTHONPATH", "PYTHONHOME"}
    }


def _coverage_package(interpreter: str) -> Path | None:
    """Return an installed coverage package without assuming an OS path."""
    try:
        package = subprocess.check_output(
            [interpreter, "-c", "import coverage; print(coverage.__path__[0])"],
            text=True,
            env=_collector_environment(),
            cwd=ROOT,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    candidate = Path(package)
    return candidate if (candidate / "__init__.py").is_file() else None


def _can_import_private(interpreters: list[str], vendor: Path) -> bool:
    """Prove every requested interpreter imports the copied package."""
    environment = dict(_collector_environment(), PYTHONPATH=str(vendor))
    probe = (
        "import coverage, pathlib, sys; "
        "package = pathlib.Path(coverage.__file__).resolve(); "
        "expected = pathlib.Path(sys.argv[1]).resolve(); "
        "raise SystemExit(0 if expected in package.parents else 1)"
    )
    for interpreter in interpreters:
        try:
            checked = subprocess.run(
                [interpreter, "-c", probe, str(vendor)], env=environment,
                cwd=ROOT,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if checked.returncode:
            return False
    return True


def _add_slug_compatibility(vendor: Path) -> None:
    """Let a newer interpreter startup hook call an older private package.

    Homebrew's Coverage.py `.pth` hook runs before our `sitecustomize` shim.
    It can therefore import the copied 7.10 package from ``PYTHONPATH`` and
    call the newer ``process_startup(slug=...)`` form before the shim has a
    chance to instrument anything.  The slug is deliberately unused by both
    versions' startup behavior, so widening the owned private copy's
    signature preserves the 7.10 behavior while making that hook compatible.
    """
    control = vendor / "coverage" / "control.py"
    source = control.read_text()
    legacy = "def process_startup(*, force: bool = False) -> Coverage | None:"
    if legacy not in source:
        return
    control.write_text(source.replace(
        legacy,
        "def process_startup(*, force: bool = False, slug: str = \"default\") -> Coverage | None:",
        1,
    ))


def private_coverage(output: Path, interpreters: list[str]) -> Path:
    """Copy a coverage package which is validated against every child Python.

    The source interpreter is discovered explicitly: an optional
    ``COVERAGE_SOURCE_PYTHON`` takes precedence, then each requested child,
    the runner, and finally ``python3``.  This avoids a hard-coded macOS path
    and works when Ubuntu's system Python has no global coverage package.  The
    copy contains only Python source, so an incompatible extension cannot be
    inherited by a different interpreter.
    """
    candidates = [os.environ.get("COVERAGE_SOURCE_PYTHON", ""), *interpreters, sys.executable, "python3"]
    destination = output / "private-coverage" / "coverage"
    for interpreter in dict.fromkeys(candidate for candidate in candidates if candidate):
        package = _coverage_package(interpreter)
        if package is None:
            continue
        shutil.rmtree(destination.parent, ignore_errors=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(package, destination, ignore=shutil.ignore_patterns("__pycache__", "*.so", "*.pyd"))
        if _can_import_private(interpreters, destination.parent):
            _add_slug_compatibility(destination.parent)
            return destination.parent
    raise RuntimeError(
        "no installed coverage package can be copied and imported by every requested interpreter; "
        "set COVERAGE_SOURCE_PYTHON to a compatible interpreter"
    )


def _inside(path: str | Path | None, directory: Path) -> bool:
    if path is None:
        return False
    try:
        Path(path).resolve().relative_to(directory.resolve())
    except (OSError, ValueError):
        return False
    return True


def coverage_data_class(vendor: Path):
    """Load CoverageData from the validated private copy, never globally.

    The collectors are dedicated processes.  A parent test runner can already
    have imported another Coverage.py package, and Python would otherwise
    return that ambient module even after the private vendor is prepended to
    ``sys.path``.
    """
    package_root = vendor / "coverage"
    loaded = sys.modules.get("coverage")
    if loaded is None or not _inside(getattr(loaded, "__file__", None), package_root):
        for name in tuple(sys.modules):
            if name == "coverage" or name.startswith("coverage."):
                del sys.modules[name]
    if str(vendor) not in sys.path:
        sys.path.insert(0, str(vendor))
    coverage = importlib.import_module("coverage")
    if not _inside(getattr(coverage, "__file__", None), package_root):
        raise RuntimeError("validated private coverage package was not imported")
    return coverage.CoverageData


def collect(interpreters: list[str], output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    # This process also reads and renders the data, so validate it too. This
    # matters on Ubuntu where /usr/bin/python3 deliberately has no coverage.
    coverage_interpreters = list(dict.fromkeys([*interpreters, sys.executable]))
    vendor = private_coverage(output, coverage_interpreters)
    CoverageData = coverage_data_class(vendor)
    shim = write_private_startup_shim(output)
    evidence = {}
    for index, interpreter in enumerate(interpreters):
        raw = output / f"raw-{index}"
        raw.mkdir()
        # This deliberately remains the production invocation: Bash directly
        # execs the selected interpreter and first-party script.  The private
        # startup shim is the only instrumentation mechanism.
        env = dict(_collector_environment(), COVERAGE_FILE=str(raw / ".coverage"),
                   COVERAGE_PROCESS_START=str(CONFIG),
                   PYTHONPATH=os.pathsep.join((str(vendor), str(shim))))
        result = subprocess.run(["bash", "-c", 'exec "$1" "$2" resolve "$3" --host codex', "coverage-bash", interpreter, str(TARGET), '{"kind":"tier","name":"volume"}'], cwd=ROOT, env=env,
                                text=True, capture_output=True, timeout=10)
        files = list(raw.glob(".coverage.*"))
        data = CoverageData(basename=str(files[0])) if files else CoverageData()
        if files:
            data.read()
        target_recorded = any(Path(name).resolve() == TARGET.resolve() and data.lines(name) for name in data.measured_files())
        evidence[interpreter] = {"exit": result.returncode, "data_files": len(files),
                                 "target_recorded": target_recorded, "stderr": result.stderr[-1000:]}
    combined = output / "combined"
    # ``coverage combine`` looks for files matching its output basename.  Raw
    # files intentionally retain the standard .coverage.* basename, so merge
    # those explicit files once into an external destination instead.
    merged = CoverageData(basename=str(combined))
    for raw_file in output.glob("raw-*/.coverage.*"):
        raw_data = CoverageData(basename=str(raw_file))
        raw_data.read()
        merged.update(raw_data)
    merged.write()
    xml = output / "coverage-cross-python.xml"
    xml_result = subprocess.run(
        [sys.executable, "-m", "coverage", "xml", "--rcfile", str(CONFIG), "-o", str(xml)],
        cwd=ROOT,
        env=dict(_collector_environment(), COVERAGE_FILE=str(combined), PYTHONPATH=str(vendor)),
        text=True,
        capture_output=True,
        timeout=15,
    )
    if xml_result.returncode:
        raise RuntimeError(f"coverage xml failed: {xml_result.stdout} {xml_result.stderr}")
    return {"interpreters": evidence, "xml": str(xml), "target": str(TARGET)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", action="append", required=True)
    args = parser.parse_args()
    record = collect(args.python, args.output)
    print(json.dumps(record, sort_keys=True))
    return 0 if all(x["exit"] == 0 and x["data_files"] and x["target_recorded"] for x in record["interpreters"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
