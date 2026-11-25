"""scripts/ci/fail-on-sandbox-skips.py: the macOS leg must not stay green when any cloud test skips."""
from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "ci" / "fail-on-sandbox-skips.py"

# the two reason strings the cloud test files use today, plus a skip that has nothing to do with the sandbox
MODULE = textwrap.dedent('''
    import pytest

    @pytest.mark.skipif(True, reason="sandbox-exec is unavailable (non-darwin host or nested sandbox)")
    def test_gated_by_marker():
        pass

    def test_gated_inline():
        pytest.skip("sandbox-exec is unavailable on this host; the sandboxed test run cannot be exercised")

    def test_other_skip():
        pytest.skip("no pasteboard in this session")

    def test_passes():
        pass
''')


def _report(tmp_path: Path, body: str) -> Path:
    module = tmp_path / "test_probe.py"
    module.write_text(body)
    report = tmp_path / "report.xml"
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml", str(report), str(module)],
                   cwd=str(tmp_path), capture_output=True, check=False)
    return report


def _guard(report: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(GUARD), str(report)], capture_output=True, text=True)


def test_a_skipped_sandbox_gated_test_fails_the_step(tmp_path: Path) -> None:
    done = _guard(_report(tmp_path, MODULE))
    assert done.returncode == 1
    assert "2 test(s) were skipped" in done.stderr
    assert "test_gated_by_marker" in done.stderr and "test_gated_inline" in done.stderr
    assert "test_other_skip" not in done.stderr


def test_a_report_without_sandbox_skips_passes_even_with_other_skips(tmp_path: Path) -> None:
    body = MODULE.split("@pytest.mark.skipif")[0] + MODULE[MODULE.index("def test_other_skip"):]
    done = _guard(_report(tmp_path, body))
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("content", [None, "not xml"])
def test_an_unreadable_report_fails_closed(tmp_path: Path, content) -> None:
    report = tmp_path / "missing.xml"
    if content is not None:
        report.write_text(content)
    assert _guard(report).returncode == 2


def test_the_macos_cloud_step_runs_the_guard_on_the_junit_report_it_writes() -> None:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    step = text[text.index("- name: Sandbox-gated cloud tests"):text.index("- name: Bash and zsh parser checks")]
    assert "--junitxml=\"$RUNNER_TEMP/sandbox-gated.xml\"" in step
    assert step.index("python -m pytest") < step.index("scripts/ci/fail-on-sandbox-skips.py \"$RUNNER_TEMP/sandbox-gated.xml\"")
    assert "if: runner.os == 'macOS'" in step and "|| true" not in step and "continue-on-error" not in step


def _skip_reasons(path: Path) -> set:
    """Every literal reason of a `pytest.skip(...)` call and of a `skipif(..., reason=...)` marker in the file."""
    reasons = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr == "skip" and node.args and isinstance(node.args[0], ast.Constant):
            reasons.add(node.args[0].value)
        if node.func.attr == "skipif":
            reasons.update(k.value.value for k in node.keywords if k.arg == "reason" and isinstance(k.value, ast.Constant))
    return reasons


def test_the_allowlist_is_exactly_the_non_sandbox_skips_the_cloud_test_files_contain() -> None:
    spec = importlib.util.spec_from_file_location("fail_on_sandbox_skips", GUARD)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    found = set()
    for name in ("test_cloud_sandbox.py", "test_cloud_collect.py", "test_cloud_dispatch.py"):
        found |= _skip_reasons(ROOT / "tests" / name)
    sandbox_gated = {r for r in found if "sandbox" in r.lower()}
    assert len(sandbox_gated) == 3, sandbox_gated  # the marker and the two inline reasons; a new one is a conscious change
    assert found - sandbox_gated == set(guard.ALLOWED_REASONS)
    assert not any("sandbox" in r.lower() for r in guard.ALLOWED_REASONS)


def test_a_skip_that_is_not_on_the_allowlist_fails_the_step(tmp_path: Path) -> None:
    body = "import pytest\n\ndef test_new_skip():\n    pytest.skip(\"some new reason\")\n"
    done = _guard(_report(tmp_path, body))
    assert done.returncode == 1 and "some new reason" in done.stderr and "test_new_skip" in done.stderr


@pytest.mark.parametrize("xml", [
    '<testsuites><testsuite tests="0"/></testsuites>',
    "<html/>",
    '<testsuites><testsuite><testcase classname="c" name="n"><skipped message="no pasteboard in this session"/>'
    "</testcase></testsuite></testsuites>",
])
def test_a_report_in_which_no_test_ran_fails_even_when_every_skip_is_allowlisted(tmp_path: Path, xml: str) -> None:
    report = tmp_path / "report.xml"
    report.write_text(xml)
    done = _guard(report)
    assert done.returncode == 1 and "no test ran" in done.stderr, (done.returncode, done.stderr)

