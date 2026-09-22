"""Acceptance regressions from the 2026-09-12 prerequisite review.

These cases are deliberately independent of the older focused tests: each
reproduces the review finding at the public boundary that a fix must protect.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


HOST = _module("prerequisite_host_capabilities", ROOT / "lib/host_capabilities.py")
OBSERVER = _module("prerequisite_runtime_observer", ROOT / "scripts/gsd/codex-runtime-observer.py")


def test_prh002_quoted_newline_worktree_cannot_add_toml_surfaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The runtime verifier must parse policy, rather than echo a raw template."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    # A legal pathname can close the raw TOML project key and add an MCP table.
    worktree = tmp_path / 'work"]\n[mcp_servers.injected]\ncommand = "touch"\n[projects."reopened'
    worktree.mkdir()
    roots = [str(worktree.resolve())]
    (runtime / "config.toml").write_text(
        'approval_policy = "never"\n'
        'sandbox_mode = "workspace-write"\nweb_search = "disabled"\nproject_doc_max_bytes = 0\n\n'
        '[sandbox_workspace_write]\nnetwork_access = false\nexclude_slash_tmp = true\n'
        f'exclude_tmpdir_env_var = true\nwritable_roots = {json.dumps(roots)}\n\n'
        f'[projects."{worktree.resolve()}"]\ntrust_level = "untrusted"\n'
    )
    auth = runtime / "auth.json"
    auth.write_text("{}\n")
    auth.chmod(0o600)
    (runtime / "hooks.json").write_text(json.dumps({"hooks": {event: [] for event in HOST.REQUIRED_HOOK_EVENTS}}))
    (runtime / "skills").mkdir()
    (runtime / "agents").mkdir()
    monkeypatch.setattr(HOST, "_require_observation", lambda *args, **kwargs: None)

    with pytest.raises(HOST.CapabilityError, match="allowlisted|TOML|policy"):
        HOST.verify_runtime(runtime, worktree, roots=roots)


def test_prh003_agent_marker_is_not_machine_native_web_denial_proof(tmp_path: Path) -> None:
    positive = tmp_path / "positive.jsonl"
    positive.write_text(json.dumps({"type": "item.completed", "item": {"type": "web_search", "query": "example.com"}}) + "\n" + json.dumps({"type": "turn.completed"}) + "\n")
    negative = tmp_path / "negative.jsonl"
    # This is only the text the prompt tells an agent to emit.  It is not a
    # tool inventory or a CLI rejected-tool event.
    negative.write_text(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "FFS_NATIVE_WEB_UNAVAILABLE"}}) + "\n" + json.dumps({"type": "turn.completed"}) + "\n")
    proved, _ = OBSERVER._native_proof(positive, negative)
    assert proved is False


def test_prh002_observer_prompt_escapes_quoted_newline_workspace_before_shell_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Execute the observer's exact generated Python program with a harmless Path shim."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "config.toml").write_text("x")
    (runtime / "hooks.json").write_text("{}")
    (runtime / "skills").mkdir()
    (runtime / "agents").mkdir()
    (runtime / "gsd-core").mkdir()
    (runtime / "scripts").mkdir()
    (runtime / "gsd-file-manifest.json").write_text("{}")
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)
    # Newlines and quotes are permitted in POSIX directory names.  In the
    # unsafe template this ends the intended literal, invokes an extra Path
    # write, then comments out the intended allowed-path suffix.
    injected_marker = "FFS_INJECTED_SIDE_EFFECT"
    worktree = tmp_path / f'quoted"); Path("{injected_marker}").write_text("bad"); #'
    worktree.mkdir()
    output = tmp_path / "observation.json"
    calls: list[list[str]] = []
    real_run = subprocess.run

    class Result:
        returncode = 0
        stderr = ""
        def __init__(self, stdout: str): self.stdout = stdout

    def intercepted(command, **kwargs):
        calls.append(command)
        prompt = command[-1]
        if "Use native web search" in prompt:
            item = {"type": "web_search", "query": "example.com"} if 'web_search="live"' in command else {"type": "agent_message", "text": "FFS_NATIVE_WEB_UNAVAILABLE"}
            return Result(json.dumps({"type": "thread.started"}) + "\n" + json.dumps({"type": "item.completed", "item": item}) + "\n" + json.dumps({"type": "turn.completed"}) + "\n")
        return Result(json.dumps({"type": "thread.started"}) + "\n" + json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "token"}}) + "\n" + json.dumps({"type": "turn.completed"}) + "\n")

    monkeypatch.setattr(OBSERVER.subprocess, "run", intercepted)
    assert OBSERVER.run_canary(runtime, binary, worktree, output, 1) == 0
    prompt = calls[0][-1]
    assert prompt.endswith("output:result.output}))"), "The exact tool program must end without prose punctuation"
    encoded_command = prompt.split("const command=", 1)[1]
    command_text, _ = json.JSONDecoder().raw_decode(encoded_command)
    shell_argv = shlex.split(command_text)
    assert shell_argv == ["python3", "$CODEX_HOME/.ffs-observer-shell-probe.py"]
    probe = runtime / OBSERVER.SHELL_PROBE_NAME
    assert probe.read_text() == OBSERVER.SHELL_PROBE_SOURCE
    admission = tmp_path / "admission.json"
    admission.write_text(json.dumps({"workspace": str(worktree)}))
    nonce = "quoted-workspace"
    executed = real_run(
        [sys.executable, str(probe)], text=True, capture_output=True, cwd=tmp_path,
        env={**os.environ, "FFS_HOOK_NONCE": nonce,
             "FFS_SUPERVISED_ADMISSION_FILE": str(admission)},
    )
    assert executed.returncode == 0, executed.stderr
    assert (worktree / f"ffs-observer-allowed-{nonce}.txt").read_text() == "allowed"
    assert (tmp_path / f"admission.json.blocked-{nonce}").read_text() == "blocked"
    assert not (tmp_path / injected_marker).exists()


def test_prh004_stale_coverage_files_do_not_certify_a_new_full_suite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The collector is a standalone executable whose sibling import normally
    # resolves because Python starts in tests/coverage.
    monkeypatch.syspath_prepend(str(ROOT / "tests/coverage"))
    collector = _module("prerequisite_coverage_collector", ROOT / "tests/coverage/run-full-suite.py")
    output = tmp_path / "output"
    raw = output / "raw"
    raw.mkdir(parents=True)
    (raw / ".coverage.stale").write_text("old data")

    class Data:
        def __init__(self, basename: str): self.basename = basename
        def read(self): pass
        def update(self, other): pass
        def write(self): pass

    class Result:
        returncode = 0

    def fake_run(command, **kwargs):
        if "xml" in command:
            Path(command[command.index("-o") + 1]).write_text("<coverage/>")
        return Result()

    monkeypatch.setattr(collector, "coverage_data_class", lambda vendor: Data)
    monkeypatch.setattr(collector, "private_coverage", lambda output, pythons: tmp_path / "vendor")
    monkeypatch.setattr(collector.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["run-full-suite.py", "--output", str(output)])
    assert collector.main() == 1


def test_prh005_help_only_cli_is_labeled_static_not_a_behavioral_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installer = _module("prerequisite_installer", ROOT / "lib/ffs_installer.py")
    fake = tmp_path / "codex"
    fake.write_text("#!/bin/sh\n"
                    "case \"$1 $2\" in\n"
                    "'--version ') echo 'codex 0.154.0' ;;\n"
                    "'exec --help') echo '--strict-config --ignore-user-config --ignore-rules --sandbox --add-dir --dangerously-bypass-hook-trust' ;;\n"
                    "*) exit 73 ;; esac\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(installer.shutil, "which", lambda name: str(fake) if name == "codex" else None)
    checks: list[dict[str, str]] = []
    installer.add_codex_version_check(checks)
    codex = next(check for check in checks if check["id"] == "codex-cli-version")
    assert "behavioral capability contract" not in codex["message"].lower()
    assert "static" in codex["message"].lower() or codex["status"] != "pass"


def test_prh006_capability_help_has_a_bounded_timeout(tmp_path: Path) -> None:
    fake = tmp_path / "codex"
    fake.write_text("#!/bin/sh\n"
                    "if [ \"$1\" = --version ]; then echo 'codex 0.154.0'; exit 0; fi\n"
                    "sleep 6\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    started = time.monotonic()
    with pytest.raises(HOST.CapabilityError):
        HOST.admit_cli(str(fake))
    assert time.monotonic() - started < 5.5, "capability inspection must not wait for an unbounded help command"
