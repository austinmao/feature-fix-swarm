from pathlib import Path

from run_state.marker import MarkerFile


def test_set_and_read(tmp_path: Path) -> None:
    marker = MarkerFile(tmp_path / ".active-run")
    marker.set("run-abc123")
    assert marker.read() == "run-abc123"
    assert marker.exists() is True


def test_clear_removes_file(tmp_path: Path) -> None:
    marker = MarkerFile(tmp_path / ".active-run")
    marker.set("run-abc")
    marker.clear()
    assert marker.exists() is False
    assert marker.read() is None


def test_read_missing_returns_none(tmp_path: Path) -> None:
    marker = MarkerFile(tmp_path / ".active-run")
    assert marker.read() is None
    assert marker.exists() is False


def test_clear_missing_is_idempotent(tmp_path: Path) -> None:
    marker = MarkerFile(tmp_path / ".active-run")
    marker.clear()
    marker.clear()


def test_set_overwrites(tmp_path: Path) -> None:
    marker = MarkerFile(tmp_path / ".active-run")
    marker.set("run-1")
    marker.set("run-2")
    assert marker.read() == "run-2"
