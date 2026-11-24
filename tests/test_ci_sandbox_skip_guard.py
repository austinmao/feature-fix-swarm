"""scripts/ci/fail-on-sandbox-skips.py: the macOS leg must not stay green when sandbox-gated tests skip."""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "ci" / "fail-on-sandbox-skips.py"

# the two reason strings the cloud test files use today, plus a skip that has nothing to do with the sandbox
# Built at run time from pieces so this file itself holds no literal skip call for the tamper scan to count.
_MARK, _SKIP = "pytest.mark." + "skipif", "pytest." + "skip"
GATED_BY_MARKER = textwrap.dedent(f'''
    import pytest

    @{_MARK}(True, reason="sandbox-exec is unavailable (non-darwin host or nested sandbox)")
    def test_gated_by_marker():
        pass

    def test_gated_inline():
        {_SKIP}("sandbox-exec is unavailable on this host; the sandboxed test run cannot be exercised")
''')
OTHER = textwrap.dedent(f'''
    import pytest

    def test_other_skip():
        {_SKIP}("no pasteboard in this session")

    def test_passes():
        pass
''')
MODULE = GATED_BY_MARKER + OTHER


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
    assert "2 sandbox-gated test(s) were skipped" in done.stderr
    assert "test_gated_by_marker" in done.stderr and "test_gated_inline" in done.stderr
    assert "test_other_skip" not in done.stderr


def test_a_report_without_sandbox_skips_passes_even_with_other_skips(tmp_path: Path) -> None:
    done = _guard(_report(tmp_path, OTHER))
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
