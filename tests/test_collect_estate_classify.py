"""collect-estate.py classify(): session state must retain landed worktrees."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "git-branch-consolidate" / "scripts" / "collect-estate.py"


@pytest.fixture(scope="module")
def estate():
    spec = importlib.util.spec_from_file_location("collect_estate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def landed_worktree(planning):
    return {
        "branch": "spec/x",
        "landed": True,
        "worktree": "/tmp/wt",
        "worktree_dirty": 0,
        "worktree_ignored": 0,
        "planning": planning,
    }


def test_clean_landed_worktree_without_session_is_delete_safe(estate):
    assert estate.classify(landed_worktree(None)) == "delete-safe"


def test_finished_session_status_is_delete_safe(estate):
    assert estate.classify(landed_worktree({"status": "complete"})) == "delete-safe"


@pytest.mark.parametrize("status", ["executing", "planning", "paused"])
def test_unfinished_session_status_is_retained(estate, status):
    assert estate.classify(landed_worktree({"status": status})) == "retain-worktree-payload"


def test_stale_runner_pid_file_is_retained(estate):
    # pid file present but the process is gone: a stopped resumable run
    assert estate.classify(landed_worktree({"runner_pid_alive": None})) == "retain-worktree-payload"


def test_live_runner_is_retained(estate):
    assert estate.classify(landed_worktree({"runner_pid_alive": 4242})) == "retain-worktree-payload"


def test_ignored_payload_is_retained(estate):
    rec = landed_worktree(None)
    rec["worktree_ignored"] = 3
    assert estate.classify(rec) == "retain-worktree-payload"
