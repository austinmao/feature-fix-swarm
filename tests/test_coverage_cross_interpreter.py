from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_private_startup_shim_records_bash_launched_system_and_current_python(tmp_path: Path):
    result = subprocess.run([sys.executable, "tests/coverage/cross_interpreter.py", "--output", str(tmp_path / "coverage"), "--python", sys.executable, "--python", "/usr/bin/python3"], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    xml = Path(evidence["xml"])
    assert xml.is_file()
    assert "lib/model_requests.py" in xml.read_text()
    assert all(x["target_recorded"] and x["data_files"] for x in evidence["interpreters"].values())
    assert all("process_startup() got an unexpected keyword argument 'slug'" not in x["stderr"]
               for x in evidence["interpreters"].values())


def test_private_coverage_source_is_not_hard_coded_to_a_system_python():
    source = (ROOT / "tests/coverage/cross_interpreter.py").read_text()
    assert '"/usr/bin/python3"' not in source
    assert "COVERAGE_SOURCE_PYTHON" in source


def test_private_coverage_ignores_ambient_path_and_loaded_module(tmp_path: Path):
    ambient = tmp_path / "ambient"
    package = ambient / "coverage"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("class CoverageData: pass\n")
    output = tmp_path / "output"
    probe = """
import json
import sys
from pathlib import Path
import cross_interpreter

ambient = Path(sys.argv[1]).resolve()
output = Path(sys.argv[2]).resolve()
import coverage
assert Path(coverage.__file__).resolve().is_relative_to(ambient)
vendor = cross_interpreter.private_coverage(output, [sys.executable])
data_class = cross_interpreter.coverage_data_class(vendor)
loaded = Path(sys.modules["coverage"].__file__).resolve()
print(json.dumps({"loaded": str(loaded), "vendor": str(vendor.resolve()),
                  "data_module": data_class.__module__}))
"""
    environment = dict(os.environ)
    environment.pop("COVERAGE_SOURCE_PYTHON", None)
    environment["PYTHONPATH"] = os.pathsep.join((str(ambient), str(ROOT / "tests" / "coverage")))
    result = subprocess.run(
        [sys.executable, "-c", probe, str(ambient), str(output)],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert Path(evidence["loaded"]).is_relative_to(Path(evidence["vendor"]))
    assert evidence["data_module"].startswith("coverage.")
