"""CLI coverage for typed finalizer skipped-event validation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
EVENTS = ROOT / "lib" / "evidence_events.py"


def _invoke(store: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["GATES_STORE"] = str(store)
    return subprocess.run(
        [sys.executable, str(EVENTS), "finisher-skipped", *args],
        cwd=store.parent, env=environment, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )


def _trace_args(path: str) -> tuple[str, ...]:
    return ("--run-id", "unattributed", "--pr", "7", "--lock-path", path, "--holder-pid", "321")


def test_finisher_skipped_cli_accepts_long_absolute_unicode_lock_path(tmp_path: Path) -> None:
    store = tmp_path / "evidence.json"
    lock_path = "/tmp/lock path apostrophe's unicode-雪/" + ("x" * 300)

    completed = _invoke(store, *_trace_args(lock_path))

    assert completed.returncode == 0, completed.stderr
    event = json.loads(store.read_text(encoding="utf-8"))["events"][-1]
    assert event["run_id"] == "unattributed"
    assert event["lock_path"] == lock_path
    assert event["holder_pid"] == 321
    assert len(lock_path.encode("utf-8")) > 128


def test_finisher_skipped_cli_keeps_run_id_128_byte_boundary(tmp_path: Path) -> None:
    store = tmp_path / "evidence.json"

    accepted = _invoke(store, "--run-id", "r" * 128, "--pr", "9")

    assert accepted.returncode == 0, accepted.stderr
    original = store.read_bytes()
    refused = _invoke(store, "--run-id", "r" * 129, "--pr", "9")
    assert refused.returncode == 2
    assert "run id" in refused.stderr
    assert store.read_bytes() == original


@pytest.mark.parametrize(
    ("lock_path", "message"),
    [
        ("relative/lock", "lock path must be absolute"),
        ("/tmp/control\nlock", "lock path must be control-free"),
        ("/tmp/" + ("x" * 4092), "lock path exceeds 4096 bytes"),
    ],
)
def test_finisher_skipped_cli_refuses_unsafe_lock_paths_without_mutating_store(
    tmp_path: Path, lock_path: str, message: str,
) -> None:
    store = tmp_path / "evidence.json"
    original = b'{"events":[{"kind":"existing"}]}'
    store.write_bytes(original)

    completed = _invoke(store, *_trace_args(lock_path))

    assert completed.returncode == 2
    assert message in completed.stderr
    assert store.read_bytes() == original
