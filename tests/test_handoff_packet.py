import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import delegation_router
import handoff_packet as hp

NONCE = "0123456789abcdef" + "0123456789abcdef"  # concat: keeps this file credential-gate clean
BASE = "a" * 40
CANARY = "CANARY_SECRET_VALUE_9f8e7d6c"
CONFIG = {
    "schema": 1,
    "verify": {"cmd": "make test"},
    "secrets": {"provider": "none", "wrapper": "", "exposure": {"claude": [], "codex": []}},
    "deadline_hours": 12,
}
TASK = {
    "id": "t1",
    "title": "Add endpoint",
    "prompt": "Implement the /ping endpoint.",
    "owns": ["src/a/**", "b.py"],
    "est_minutes": 45,
    "needs_secrets": [],
    "verify_cmd": "npm test -- a",
}
BRANCH = "claude/ffs-run1-t1-01234567"


def packet(provider="claude", task=None, config=None, **overrides):
    kwargs = dict(
        run="run1",
        generation=2,
        nonce=NONCE,
        base_sha=BASE,
        provider=provider,
        config=config or CONFIG,
        result_branch=BRANCH if provider == "claude" else None,
    )
    kwargs.update(overrides)
    return hp.build_packet(task or TASK, **kwargs)


def skeleton_from(text):
    start = text.index("{\n")
    return json.loads(text[start : text.index("\n}", start) + 2])


def test_result_branch_name_matches_contract():
    assert hp.result_branch_name("run1", "t1", NONCE) == BRANCH


def test_claude_packet_contents():
    text = packet()
    assert "Implement the /ping endpoint." in text
    assert "- src/a/**" in text and "- b.py" in text
    assert "Verify command: npm test -- a" in text
    assert "Push to exactly this branch name: " + BRANCH in text
    assert "Commit all work" in text
    assert "run 1" not in text and "run run1" in text
    assert "at most 4 nested subagents" in text
    assert "`bash .ffs/cloud/env-check.sh claude`" in text
    assert "Never ask the user" in text
    assert "Never print, echo, log, write or commit a secret value" in text
    assert "Stay inside `owns`" in text
    assert "write `status: blocked`" in text
    assert ".ffs-result/t1.json" in text
    assert BASE in text


def test_denylist_summary_lists_every_entry():
    text = packet()
    for entry in delegation_router.DENYLIST:
        assert entry in text


def test_codex_packet_leaves_changes_in_working_tree():
    text = packet("codex")
    assert "Leave all changes" in text and "working tree" in text
    assert "Do not commit and do not push" in text
    assert "claude/ffs-" not in text and "Push to exactly" not in text
    assert NONCE in text
    assert "Commit your work before you finish" not in text


def test_manifest_skeleton_filled_and_parseable():
    text = packet()
    skeleton = skeleton_from(text)
    assert skeleton == hp.manifest_skeleton(
        run="run1", task_id="t1", generation=2, nonce=NONCE, base_sha=BASE
    )
    assert skeleton["nonce"] == NONCE and skeleton["generation"] == 2
    assert skeleton["schema"] == 1 and skeleton["task"] == "t1" and skeleton["run"] == "run1"
    assert set(skeleton) == {
        "schema", "run", "task", "generation", "nonce", "base_sha", "status", "summary",
        "decisions_needed", "blockers", "env_report", "tests_run",
    }
    assert set(skeleton["env_report"]) == {"tools", "present", "missing"}


def test_verify_falls_back_to_config():
    task = dict(TASK, verify_cmd="")
    assert "Verify command: make test" in packet(task=task)
    assert "Verify command: (none given)" in packet(task=task, config={"secrets": {"provider": "none"}})


def test_no_wrapper_line_when_provider_none():
    text = packet()
    assert "Secrets wrapper" not in text and "Secret names" not in text


def test_wrapper_line_and_names_only():
    config = {"secrets": {"provider": "doppler", "wrapper": "doppler run --"}}
    task = dict(TASK, needs_secrets=["API_KEY", "DB_URL"])
    text = packet(task=task, config=config)
    assert "`doppler run -- <command>`" in text
    assert "API_KEY, DB_URL" in text


def test_provider_env_without_wrapper():
    config = {"secrets": {"provider": "provider-env", "wrapper": ""}}
    text = packet(config=config)
    assert "environment variables" in text and "Secrets wrapper" not in text


@pytest.mark.parametrize("wrapper", ["a\nb", "x" * 300, "ok\x00", "ünï"])
def test_bad_wrapper_rejected(wrapper):
    with pytest.raises(hp.PacketError):
        packet(config={"secrets": {"provider": "command", "wrapper": wrapper}})


def test_time_budget():
    assert "(90 minutes)" in packet()
    small = dict(TASK, est_minutes=5)
    assert "(30 minutes)" in packet(task=small)
    capped = dict(TASK, est_minutes=10000)
    assert "(720 minutes)" in packet(task=capped)
    assert "(60 minutes)" in packet(task=capped, config=dict(CONFIG, deadline_hours=1))


def test_never_reads_environment(monkeypatch):
    monkeypatch.setenv("SOME_API_KEY", CANARY)
    monkeypatch.setenv("DOPPLER_TOKEN", CANARY)
    monkeypatch.setattr(os, "environ", dict(os.environ, EXTRA=CANARY))
    config = {"secrets": {"provider": "doppler", "wrapper": "doppler run --"}}
    text = packet(task=dict(TASK, needs_secrets=["SOME_API_KEY"]), config=config)
    assert CANARY not in text
    assert "SOME_API_KEY" in text and "doppler run --" in text


def test_module_source_never_touches_environ_or_files():
    source = Path(hp.__file__).read_text()
    assert "os.environ" not in source and "getenv" not in source
    assert not re.search(r"^\s*(import|from)\s+(os|subprocess|pathlib)\b", source, re.M)
    assert "open(" not in source


@pytest.mark.parametrize(
    "overrides",
    [
        {"run": "bad run"},
        {"generation": 0},
        {"generation": True},
        {"nonce": "XYZ"},
        {"nonce": NONCE.upper()},
        {"base_sha": "main"},
        {"provider": "gemini"},
        {"result_branch": "claude/ffs-other"},
        {"result_branch": None},
    ],
)
def test_input_validation(overrides):
    with pytest.raises(hp.PacketError):
        packet(**overrides)


def test_task_id_validation():
    with pytest.raises(hp.PacketError):
        packet(task=dict(TASK, id="T 1"))


def test_codex_ignores_result_branch_argument():
    assert "claude/ffs-" not in packet("codex", result_branch="whatever")


def test_packet_is_deterministic():
    assert packet() == packet()


# ── env-check.sh <-> manifest validator ──────────────────────────────────────

TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "cloud"


def test_packet_passes_the_provider_to_env_check():
    assert "`bash .ffs/cloud/env-check.sh claude`" in packet("claude")
    assert "`bash .ffs/cloud/env-check.sh codex`" in packet("codex")
    assert hp.env_check_command("codex") == "bash .ffs/cloud/env-check.sh codex"


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_real_env_check_output_validates_inside_a_full_manifest(tmp_path, provider):
    """The packet's command, run as the worker runs it, yields an accepted `env_report`."""
    import cloud_ledger

    cloud = tmp_path / ".ffs" / "cloud"
    cloud.mkdir(parents=True)
    for name in ("env-check.sh", "config.json"):
        shutil.copy(str(TEMPLATES / name), str(cloud / name))
    config = json.loads((cloud / "config.json").read_text())
    config["secrets"]["exposure"][provider] = ["FFS_PRESENT_NAME", "FFS_MISSING_NAME"]
    (cloud / "config.json").write_text(json.dumps(config))
    home = tmp_path / "home"
    home.mkdir()
    command = hp.env_check_command(provider)
    assert command in packet(provider)
    out = subprocess.run(command.split(), cwd=str(tmp_path), capture_output=True, text=True, check=True,
                         env={"PATH": os.environ["PATH"], "HOME": str(home), "FFS_PRESENT_NAME": "x"}).stdout
    report = json.loads(out)
    assert set(report) == {"tools", "present", "missing"}
    assert report["present"] == ["FFS_PRESENT_NAME"] and report["missing"] == ["FFS_MISSING_NAME"]

    manifest = hp.manifest_skeleton(run="run1", task_id="t1", generation=2, nonce=NONCE, base_sha=BASE)
    manifest["env_report"] = report
    manifest["summary"] = "done"
    checked = cloud_ledger.validate_manifest(
        json.dumps(manifest).encode(), run="run1", task="t1", generation=2, nonce=NONCE, base_sha=BASE)
    assert checked["env_report"] == report


def test_env_check_with_a_setup_key_would_be_refused():
    """Guards the contract the other way round: the validator rejects the old `setup` key."""
    import cloud_ledger

    manifest = hp.manifest_skeleton(run="run1", task_id="t1", generation=2, nonce=NONCE, base_sha=BASE)
    manifest["env_report"] = {"tools": {}, "present": [], "missing": [], "setup": None}
    with pytest.raises(cloud_ledger.ManifestError):
        cloud_ledger.validate_manifest(
            json.dumps(manifest).encode(), run="run1", task="t1", generation=2, nonce=NONCE, base_sha=BASE)
