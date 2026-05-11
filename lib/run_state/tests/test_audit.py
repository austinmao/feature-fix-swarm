import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from run_state.audit import run_audit


def _mock_completed(stdout: str, returncode: int = 0):
    m = MagicMock()
    m.stdout = stdout
    m.stderr = ""
    m.returncode = returncode
    return m


def test_pass_verdict_parses_json() -> None:
    payload = json.dumps({"verdict": "pass", "reasoning": "no repro found"})
    with patch("run_state.audit.subprocess.run", return_value=_mock_completed(payload)):
        result = run_audit(prompt="x", cwd=Path("/tmp"))
    assert result.verdict == "pass"
    assert "no repro" in result.reasoning


def test_fail_verdict_parses_json() -> None:
    payload = json.dumps({"verdict": "fail", "reasoning": "repro works", "missing": ["edge case"]})
    with patch("run_state.audit.subprocess.run", return_value=_mock_completed(payload)):
        result = run_audit(prompt="x", cwd=Path("/tmp"))
    assert result.verdict == "fail"
    assert result.missing == ["edge case"]


def test_malformed_output_becomes_error() -> None:
    with patch("run_state.audit.subprocess.run", return_value=_mock_completed("not json at all")):
        result = run_audit(prompt="x", cwd=Path("/tmp"))
    assert result.verdict == "error"


def test_subprocess_failure_becomes_error() -> None:
    with patch("run_state.audit.subprocess.run", return_value=_mock_completed("", returncode=2)):
        result = run_audit(prompt="x", cwd=Path("/tmp"))
    assert result.verdict == "error"


def test_invalid_verdict_string_becomes_error() -> None:
    payload = json.dumps({"verdict": "maybe", "reasoning": "x"})
    with patch("run_state.audit.subprocess.run", return_value=_mock_completed(payload)):
        result = run_audit(prompt="x", cwd=Path("/tmp"))
    assert result.verdict == "error"


def test_extracts_json_after_chatter() -> None:
    raw = "Some preamble text...\n{\"verdict\":\"pass\",\"reasoning\":\"ok\"}\ntrailing"
    with patch("run_state.audit.subprocess.run", return_value=_mock_completed(raw)):
        result = run_audit(prompt="x", cwd=Path("/tmp"))
    assert result.verdict == "pass"
