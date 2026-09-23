"""Production CLI must type a drive before it can prepare or mutate a run."""
from __future__ import annotations

import pytest

from run_state import cli, managed


def _argv(*command, activity=None, scope=None):
    args = [
        "managed-start", "--objective", "typed drive", "--state-root", "/absent/authority",
        "--selection-manifest", "/absent/selection.json",
        "--upstream-runtime-manifest", "/absent/runtime.json",
        "--upstream-runtime-sha256", "a" * 64, "--request-key", "typed-request",
        "--dispatch-limit", "8", "--token-limit", "250K",
    ]
    if activity is not None:
        args += ["--activity", activity]
    if scope is not None:
        args += ["--scope", scope]
    return [*args, "--", *command]


@pytest.mark.parametrize("command,activity,scope", [
    (("/gsd-execute-phase", "06", "--gaps-only"), "execute", "06"),
    (("$gsd-plan-phase", "6.1", "--gaps"), "plan", "6.1"),
    (("/gsd-code-review", "6"), "review", "6"),
    (("$gsd-quick", "repair the parser with spaces"), "execute", ""),
])
def test_cli_derives_activity_before_preparation(monkeypatch, command, activity, scope):
    calls = []

    def prepare(**kwargs):
        calls.append(kwargs)
        return 17

    monkeypatch.setattr(managed, "prepare_managed_run", prepare)
    assert cli.main(_argv(*command)) == 17
    assert calls[0]["activity"] == activity
    assert calls[0]["scope"] == scope
    assert calls[0]["dispatch_limit"] == 8
    assert calls[0]["token_limit"] == 250000


@pytest.mark.parametrize("command,activity,scope", [
    (("/gsd-execute-phase", "6"), "plan", None),
    (("/gsd-plan-phase", "6"), None, "7"),
    (("/gsd-execute-phase",), None, None),
    (("/gsd-execute-phase", "../6"), None, None),
    (("/gsd-execute-phase", "--help"), None, None),
    (("/gsd-not-a-real-command",), None, None),
    (("/bin/sh", "-c", "touch forbidden"), None, None),
    (("$gsd-quick",), None, None),
    (("/gsd-code-review", "6", "--fix"), None, None),
    (("/gsd-execute-phase", "6", "--ws", "other"), None, None),
    (("/gsd-execute-phase", "6", "--interactive"), None, None),
    (("/gsd-plan-review-convergence", "6"), None, None),
    (("$gsd-quick", "resume old-task"), None, None),
    (("$gsd-quick", "list"), None, None),
])
def test_invalid_or_conflicting_command_never_enters_preparation(
    monkeypatch, capsys, command, activity, scope,
):
    monkeypatch.setattr(managed, "prepare_managed_run", lambda **_: pytest.fail("prepared"))
    assert cli.main(_argv(*command, activity=activity, scope=scope)) == 2
    assert "MANAGED_COMMAND" in capsys.readouterr().out
