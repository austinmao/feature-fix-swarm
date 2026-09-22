#!/usr/bin/env python3
"""Run an unchanged Python/Bats suite with private inherited coverage startup."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from cross_interpreter import (
    CONFIG,
    ROOT,
    coverage_data_class,
    private_coverage,
    write_private_startup_shim,
)

LINE_COVERAGE_FLOOR = 0.80
RUNTIME_DESCRIPTOR_ENV = "FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR"
RUNTIME_DESCRIPTOR_SHA256_ENV = "FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR_SHA256"


def _empty_coverage_metrics() -> dict[str, float | bool | None]:
    return {
        "line_rate": None,
        "branch_rate": None,
        "line_coverage_floor_passed": False,
    }


def _coverage_metrics(xml: Path) -> dict[str, float | bool | None]:
    """Read coverage rates and apply the line-only release threshold."""
    metrics = _empty_coverage_metrics()
    try:
        root = ET.parse(xml).getroot()
        line_rate = float(root.attrib["line-rate"])
        branch_rate = float(root.attrib["branch-rate"])
    except (ET.ParseError, KeyError, OSError, TypeError, ValueError):
        return metrics
    if not all(math.isfinite(rate) and 0.0 <= rate <= 1.0 for rate in (line_rate, branch_rate)):
        return metrics
    metrics["line_rate"] = line_rate
    metrics["branch_rate"] = branch_rate
    metrics["line_coverage_floor_passed"] = line_rate >= LINE_COVERAGE_FLOOR
    return metrics


def _failure(output: Path, reason: str) -> int:
    print(json.dumps({
        "both_attempted": False, "coverage_xml": None, "full_suite_coverage": False,
        "line_coverage_floor": LINE_COVERAGE_FLOOR, **_empty_coverage_metrics(),
        "output": str(output), "reason": reason,
    }, sort_keys=True))
    return 1


def _registered_runtime_input_error(environment: dict[str, str]) -> str | None:
    """Verify the externally registered runtime pair before a full launch.

    The upstream bridge acceptance tests deliberately require a controller
    supplied descriptor.  Do this at the full-suite boundary so a missing
    pair cannot consume a long Python/Bats run and surface only as unrelated
    setup errors.  This launcher validates bytes and never manufactures a
    descriptor from ambient Node state.
    """
    descriptor = environment.get(RUNTIME_DESCRIPTOR_ENV)
    digest = environment.get(RUNTIME_DESCRIPTOR_SHA256_ENV)
    if not descriptor or not digest:
        return f"{RUNTIME_DESCRIPTOR_ENV} and {RUNTIME_DESCRIPTOR_SHA256_ENV} are required"
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return f"{RUNTIME_DESCRIPTOR_SHA256_ENV} must be a lowercase SHA-256 digest"
    path = Path(descriptor)
    if not path.is_absolute():
        return f"{RUNTIME_DESCRIPTOR_ENV} must be an absolute path"
    try:
        info = path.lstat()
        data = path.read_bytes()
    except OSError as exc:
        return f"{RUNTIME_DESCRIPTOR_ENV} is unreadable: {exc}"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return f"{RUNTIME_DESCRIPTOR_ENV} must name a regular non-symlink file"
    if hashlib.sha256(data).hexdigest() != digest:
        return f"{RUNTIME_DESCRIPTOR_SHA256_ENV} does not match descriptor bytes"
    return None


def _fresh_owned_output(requested: Path) -> tuple[Path | None, str | None]:
    """Claim one empty, non-symlink directory without touching retained evidence."""
    output = Path(os.path.abspath(requested.expanduser()))
    try:
        metadata = os.lstat(output)
    except FileNotFoundError:
        try:
            output.mkdir(mode=0o700)
        except OSError as exc:
            return None, f"could not create fresh output directory: {exc}"
        metadata = os.lstat(output)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        return None, "output boundary must be a regular directory, not a symlink or file"
    if metadata.st_uid != os.getuid():
        return None, "output boundary must be owned by the current user"
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        return None, "output boundary must not be group- or world-writable"
    try:
        if any(output.iterdir()):
            return None, "output directory is not fresh; refusing to overwrite retained coverage evidence"
    except OSError as exc:
        return None, f"could not inspect output boundary: {exc}"
    return output, None


def _project_coverage_config(output: Path) -> Path:
    """Bind collection to this checkout even when a Bats fixture changes cwd.

    ``coverage-parallel.ini`` deliberately describes the source tree
    relatively so its checked-in contract remains portable.  Bats exercises
    production commands from temporary fixture repositories, though, and
    Coverage resolves that relative source entry from each child process's
    working directory.  Give every inherited child an output-local config
    with the already-resolved checkout root instead.
    """
    source = CONFIG.read_text()
    relative_source = "source =\n    ."
    if source.count(relative_source) != 1:
        raise RuntimeError("coverage config does not contain its expected relative source entry")
    configured = output / "coverage-parallel.ini"
    configured.write_text(source.replace(relative_source, f"source =\n    {ROOT}"))
    return configured


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable,
                        help="Python used for the complete lib/ and tests/ suite")
    parser.add_argument("--child-python", action="append", default=[],
                        help="Additional Python interpreter invoked by suite subprocesses; validate private coverage for it")
    args = parser.parse_args()
    runtime_error = _registered_runtime_input_error(dict(os.environ))
    if runtime_error:
        return _failure(args.output, runtime_error)
    output, boundary_error = _fresh_owned_output(args.output)
    if boundary_error:
        return _failure(args.output, boundary_error)
    assert output is not None
    raw = output / "raw"
    # The claimed output is empty, so all artifacts below it belong to this
    # attempt.  Do not delete or reuse an earlier attempt's evidence.
    raw.mkdir(mode=0o700)
    coverage_nonce = uuid.uuid4().hex
    coverage_prefix = raw / f".coverage.{coverage_nonce}"
    coverage_config = _project_coverage_config(output)
    vendor = private_coverage(output, list(dict.fromkeys([args.python, sys.executable, *args.child_python])))
    CoverageData = coverage_data_class(vendor)
    shim = write_private_startup_shim(output)
    env = dict(
        os.environ,
        COVERAGE_FILE=str(coverage_prefix),
        COVERAGE_PROCESS_START=str(coverage_config),
        # This runner verifies the packaged source repository, matching the
        # CI Bats job.  There is no consumer-installed copy to compare here.
        GSD_FFS_STANDALONE="1",
        PYTHONPATH=os.pathsep.join((str(vendor), str(shim))),
    )
    # Do not short-circuit: Bats is part of the suite even when Python fails.
    python_result = subprocess.run([args.python, "-m", "pytest", "lib/", "tests/"], cwd=ROOT, env=env)
    excluded = {".git", "node_modules", ".venv", ".claude", ".codex", ".specify", "vendor", ".staging"}
    suites = sorted(str(path.relative_to(ROOT)) for path in ROOT.rglob("*.bats")
                    if not any(part in excluded for part in path.relative_to(ROOT).parts))
    if not suites:
        raise RuntimeError("no first-party Bats suites discovered")
    bats_result = subprocess.run(["bats", "--print-output-on-failure", *suites], cwd=ROOT, env=env)

    merged = CoverageData(basename=str(output / "combined"))
    raw_files = list(raw.glob(f"{coverage_prefix.name}.*"))
    for raw_file in raw_files:
        data = CoverageData(basename=str(raw_file))
        data.read()
        merged.update(data)
    xml = output / "coverage.xml"
    xml_result = None
    metrics = _empty_coverage_metrics()
    if raw_files:
        merged.write()
        xml_result = subprocess.run(
            [args.python, "-m", "coverage", "xml", "--ignore-errors",
             "--rcfile", str(coverage_config), "-o", str(xml)],
            cwd=ROOT,
            env=dict(env, COVERAGE_FILE=str(output / "combined")),
        )
        if xml_result.returncode == 0 and xml.is_file():
            metrics = _coverage_metrics(xml)
    else:
        print("coverage collector: no raw interpreter data", file=sys.stderr)

    record = {
        "python_exit": python_result.returncode,
        "bats_exit": bats_result.returncode,
        "both_attempted": True,
        "bats_suites": suites,
        "interpreters": list(dict.fromkeys([args.python, sys.executable, *args.child_python])),
        "coverage_xml": str(xml) if xml.is_file() else None,
        "line_coverage_floor": LINE_COVERAGE_FLOOR,
        **metrics,
        # An XML from a failed suite is diagnostic data, never a full-suite claim.
        "full_suite_coverage": bool(xml_result and xml_result.returncode == 0
                                    and python_result.returncode == 0 and bats_result.returncode == 0
                                    and metrics["line_coverage_floor_passed"]),
    }
    print(__import__("json").dumps(record, sort_keys=True))
    return 0 if record["full_suite_coverage"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
