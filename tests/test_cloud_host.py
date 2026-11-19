"""Tests for lib/cloud_host.py against local bare repos and fake provider CLIs.

The real ``claude --cloud`` / ``codex cloud`` are never run: an autouse fixture
points both binary overrides at a nonexistent path, and each test installs a
wrapper around a fixture fake. Wrappers export the ``FAKE_*`` variables, because
the backend strips everything outside the child-environment allowlist.
"""
import json
import os
import shlex
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import cloud_host as ch

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cloud"
NONCE = "abcd1234" + "0" * 24
CLAUDE_RESULT_BRANCH = "claude/ffs-r1-t1-abcd1234"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "commit.gpgsign",
    "GIT_CONFIG_VALUE_0": "false",
}


def git(*args, cwd=None):
    result = subprocess.run(
        ["git"] + [str(a) for a in args],
        cwd=str(cwd) if cwd else None,
        env=dict(os.environ, **GIT_ENV),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _never_real_providers(monkeypatch, tmp_path):
    """No test may reach a real provider CLI or inherit API-key variables."""
    monkeypatch.setenv("FFS_CLOUD_BIN_CLAUDE", str(tmp_path / "no-such-claude"))
    monkeypatch.setenv("FFS_CLOUD_BIN_CODEX", str(tmp_path / "no-such-codex"))
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", ch.LAUNCH_TIMEOUT_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "state"
    path.mkdir()
    return path


@pytest.fixture
def install_fake(monkeypatch, tmp_path, state):
    def install(which, **env):
        values = {"FAKE_CLOUD_STATE": state}
        values.update(env)
        lines = ["#!/bin/bash"]
        lines += ["export %s=%s" % (k, shlex.quote(str(v))) for k, v in values.items()]
        lines.append("exec %s \"$@\"" % shlex.quote(str(FIXTURES / ("fake-" + which))))
        wrapper = tmp_path / ("wrap-" + which)
        wrapper.write_text("\n".join(lines) + "\n")
        wrapper.chmod(0o755)
        monkeypatch.setenv(ch.BIN_ENV[which], str(wrapper))
        return wrapper

    return install


@pytest.fixture
def origin(tmp_path):
    bare = tmp_path / "origin.git"
    work = tmp_path / "work"
    git("init", "--bare", "-b", "main", bare)
    git("init", "-b", "main", work)
    (work / "f.txt").write_text("base\n")
    git("add", "f.txt", cwd=work)
    git("commit", "-m", "base", cwd=work)
    base = git("rev-parse", "HEAD", cwd=work)
    git("remote", "add", "origin", bare, cwd=work)
    git("push", "origin", "main", cwd=work)
    git("checkout", "-b", "other", cwd=work)
    git("commit", "--allow-empty", "-m", "other", cwd=work)
    git("push", "origin", "other", cwd=work)
    git("tag", "v1", cwd=work)
    git("push", "origin", "v1", cwd=work)
    git("checkout", "-b", "ffs/cloud/r1/t1-g1", "main", cwd=work)
    (work / "task.txt").write_text("task\n")
    git("add", "task.txt", cwd=work)
    git("commit", "-m", "task", cwd=work)
    task_oid = git("rev-parse", "HEAD", cwd=work)
    git("push", "origin", "ffs/cloud/r1/t1-g1", cwd=work)
    ns = SimpleNamespace(
        path=bare, url="file://" + str(bare), work=work, branch="ffs/cloud/r1/t1-g1", oid=task_oid, base=base
    )
    ns.push_ref = lambda name, rev="HEAD": _push_ref(ns, name, rev)
    ns.new_commit = lambda: _new_commit(ns)
    ns.push_ref("claude/old-1")
    return ns


def _new_commit(origin):
    git("checkout", "-q", "--detach", origin.oid, cwd=origin.work)
    git("commit", "--allow-empty", "-m", "n-%s" % time.time_ns(), cwd=origin.work)
    return git("rev-parse", "HEAD", cwd=origin.work)


def _push_ref(origin, name, rev="HEAD"):
    oid = _new_commit(origin) if rev == "HEAD" else rev
    git("push", "--force", origin.path, "%s:refs/heads/%s" % (oid, name), cwd=origin.work)
    return oid


def launch_ctx(origin, tmp_path, **overrides):
    values = dict(
        run="r1",
        task="t1",
        generation=1,
        prompt="Do the thing",
        task_branch=origin.branch,
        clone_url=origin.url,
        model="claude-opus-test",
        effort="high",
        log_path=tmp_path / "logs" / "launch.log",
        env_id=None,
        task_branch_oid=origin.oid,
        refs_snapshot_path=tmp_path / "logs" / "claude_refs_at_launch.json",
    )
    values.update(overrides)
    return ch.LaunchCtx(**values)


def poll_ctx(origin, tmp_path, **overrides):
    values = dict(
        run="r1",
        task="t1",
        generation=1,
        nonce=NONCE,
        base_oid=origin.base,
        task_branch=origin.branch,
        remote_id=None,
        launched_at=0.0,
        refs_snapshot_path=tmp_path / "logs" / "claude_refs_at_launch.json",
        rejected=[],
        clone_url=origin.url,
        env_id=None,
    )
    values.update(overrides)
    return ch.PollCtx(**values)


def recorded_argv(state, name):
    raw = (state / name).read_bytes()
    return raw.decode().split("\0")[:-1]


def quarantine(tmp_path, name="q"):
    repo = tmp_path / name
    git("init", "-b", "main", repo)
    return repo


# ── Pure parsers ────────────────────────────────────────────────────────────


def test_parse_claude_launch_url_and_session():
    text = "Created session_01ABCDEFGH\nhttps://claude.ai/code/session_01ABCDEFGH?x=1\n"
    assert ch.parse_claude_launch(text) == (
        "session_01ABCDEFGH",
        "https://claude.ai/code/session_01ABCDEFGH",
    )


def test_parse_claude_launch_nothing():
    assert ch.parse_claude_launch("") == (None, None)
    assert ch.parse_claude_launch("launched ok\n") == (None, None)


@pytest.mark.parametrize(
    "text",
    [
        "https://claude.ai.evil.com/code/session_01ABCDEFGH",
        "https://evilclaude.ai/x",
        "http://claude.ai/x",
        "https://user@claude.ai/x",
    ],
)
def test_parse_claude_launch_rejects_lookalike_urls(text):
    assert ch.parse_claude_launch(text)[1] is None


def test_parse_codex_launch_text_json_and_none():
    assert ch.parse_codex_launch("Task submitted: task_e_abc123\nhttps://chatgpt.com/codex/tasks/task_e_abc123")[0] == (
        "task_e_abc123"
    )
    assert ch.parse_codex_launch('{"id":"task_e_9","url":"https://x/y"}')[0] == "task_e_9"
    assert ch.parse_codex_launch("Task submitted.") == (None, None)
    assert ch.parse_codex_launch('{"id":"-rf bad"}') == (None, None)


@pytest.mark.parametrize(
    "text, expected",
    [
        ('{"id":"t","status":"running"}', "running"),
        ('{"id":"t","status":"completed"}', "result-ready"),
        ('{"status":{"state":"failed"}}', "failed"),
        ("[READY] some title\n", "result-ready"),
        ("[PENDING] some title\n", "running"),
        ("[ERROR] some title\n", "failed"),
        ("Status: in progress\n", "running"),
        ("status = Cancelled", "failed"),
        ("", "unknown"),
        ("something else entirely", "unknown"),
        ("Status: frobnicating", "unknown"),
    ],
)
def test_parse_codex_status_mapping(text, expected):
    assert ch.parse_codex_status(text)[0] == expected


def test_parse_codex_status_id_mismatch_and_list():
    assert ch.parse_codex_status('{"id":"other","status":"ready"}', "mine") == ("unknown", "ID_MISMATCH")
    listing = '[{"id":"a","status":"failed"},{"id":"mine","status":"ready"}]'
    assert ch.parse_codex_status(listing, "mine")[0] == "result-ready"


def test_parse_ls_remote_drops_malformed_lines():
    good = "a" * 40
    text = "\n".join(
        [
            "%s\trefs/heads/claude/ok" % good,
            "zzz\trefs/heads/claude/bad-oid",
            "%s\trefs/heads/main" % good,
            "%s\trefs/heads/claude/a..b" % good,
            "%s\trefs/heads/claude/has space" % good,
        ]
    )
    assert ch.parse_ls_remote(text) == {"refs/heads/claude/ok": good}


def test_select_candidates_order_cap_and_rejected():
    oid = lambda n: "%040x" % n  # noqa: E731
    refs = {"refs/heads/claude/old": oid(1), "refs/heads/claude/zz-new": oid(2), "refs/heads/claude/aa-new": oid(3)}
    refs["refs/heads/claude/ffs-abcd1234-x"] = oid(4)
    snapshot = {"refs/heads/claude/old": oid(1)}
    picked, capped = ch.select_candidates(refs, snapshot, NONCE, [])
    assert [r for r, _ in picked] == [
        "refs/heads/claude/ffs-abcd1234-x",
        "refs/heads/claude/aa-new",
        "refs/heads/claude/zz-new",
    ]
    assert capped is False
    rejected = [["refs/heads/claude/aa-new", oid(3)]]  # JSON round-trip gives lists
    picked, _ = ch.select_candidates(refs, snapshot, NONCE, rejected)
    assert "refs/heads/claude/aa-new" not in [r for r, _ in picked]
    # A rejected ref that moved to a new oid is a new pair and is offered again.
    refs["refs/heads/claude/aa-new"] = oid(99)
    picked, _ = ch.select_candidates(refs, snapshot, NONCE, rejected)
    assert ("refs/heads/claude/aa-new", oid(99)) in picked


def test_select_candidates_cap_applies_after_rejected_filter():
    refs = {"refs/heads/claude/n%02d" % i: "%040x" % (i + 1) for i in range(25)}
    rejected = [[r, o] for r, o in list(refs.items())[:3]]
    picked, capped = ch.select_candidates(refs, {}, NONCE, rejected)
    assert len(picked) == ch.MAX_CANDIDATES
    assert capped is True
    assert not set(map(tuple, rejected)) & set(picked)


def test_select_candidates_without_snapshot_only_nonce_matches():
    refs = {"refs/heads/claude/ffs-abcd1234": "a" * 40, "refs/heads/claude/other": "b" * 40}
    picked, _ = ch.select_candidates(refs, None, NONCE, [])
    assert picked == [("refs/heads/claude/ffs-abcd1234", "a" * 40)]


def test_select_candidates_never_offers_a_sibling_tasks_result():
    t1, t2 = "refs/heads/claude/ffs-r1-t1-abcd1234", "refs/heads/claude/ffs-r1-t2-ef012345"
    refs = {t1: "a" * 40, t2: "b" * 40, "refs/heads/claude/free-form": "c" * 40}
    nonce2 = "ef012345" + "0" * 24
    # both appeared after the snapshot; each task sees only its own named ref plus un-named refs
    picked, _ = ch.select_candidates(refs, {}, nonce2, [], run="r1", task="t2")
    assert [r for r, _ in picked] == [t2, "refs/heads/claude/free-form"]
    picked, _ = ch.select_candidates(refs, {}, NONCE, [], run="r1", task="t1")
    assert [r for r, _ in picked] == [t1, "refs/heads/claude/free-form"]
    # without run the old behaviour (new refs qualify) is unchanged
    assert t1 in [r for r, _ in ch.select_candidates(refs, {}, nonce2, [])[0]]


def test_select_candidates_sibling_rule_is_scoped_to_the_run_and_the_exact_shape():
    refs = {
        "refs/heads/claude/ffs-r2-t1-abcd1234": "a" * 40,  # another run: not "a sibling of this run"
        "refs/heads/claude/ffs-r1-t9-zzzzzzzz": "b" * 40,  # not a nonce-shaped tail: ordinary new ref
        "refs/heads/claude/ffs-r1-t1-abcd1234": "c" * 40,  # ours
    }
    picked, _ = ch.select_candidates(refs, {}, NONCE, [], run="r1", task="t1")
    assert [r for r, _ in picked] == [  # nonce-named refs first, then the new un-named one
        "refs/heads/claude/ffs-r1-t1-abcd1234", "refs/heads/claude/ffs-r2-t1-abcd1234",
        "refs/heads/claude/ffs-r1-t9-zzzzzzzz"]


def test_select_candidates_short_nonce_never_matches_everything():
    refs = {"refs/heads/claude/x": "a" * 40}
    assert ch.select_candidates(refs, None, "", [])[0] == []


# ── Factory and availability ────────────────────────────────────────────────


def test_get_backend_known_names():
    assert isinstance(ch.get_backend("claude"), ch.ClaudeBackend)
    assert isinstance(ch.get_backend("codex"), ch.CodexBackend)


@pytest.mark.parametrize("name", ["anthropic-api", "openai-api", "api", " API ", "OpenAI-API"])
def test_get_backend_api_key_names(name):
    with pytest.raises(ch.UnsupportedBackend) as err:
        ch.get_backend(name)
    assert err.value.code == "API_KEY_BACKEND"


@pytest.mark.parametrize("name", ["", "gemini", "local", "anthropic", "openai", "claude-api", "codex-api-key",
                                  "OPENAI_API", "tokenizer", "keyboard", "claude-cloud"])
def test_get_backend_unknown(name):
    with pytest.raises(ch.UnsupportedBackend) as err:
        ch.get_backend(name)
    assert err.value.code == "UNKNOWN_BACKEND"


def test_available_ok(install_fake):
    install_fake("claude")
    install_fake("codex")
    assert ch.ClaudeBackend().available() == (True, "ok")
    assert ch.CodexBackend().available() == (True, "ok")


def test_available_binary_missing():
    ok, reason = ch.ClaudeBackend().available()
    assert not ok and reason.startswith("BIN_NOT_FOUND")
    ok, reason = ch.CodexBackend().available()
    assert not ok and reason.startswith("BIN_NOT_FOUND")


def test_available_refuses_api_keys(install_fake, monkeypatch):
    install_fake("claude")
    install_fake("codex")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-a-real-key")
    ok, reason = ch.ClaudeBackend().available()
    assert not ok and reason.startswith("API_KEY_BACKEND") and "ANTHROPIC_API_KEY" in reason
    ok, reason = ch.CodexBackend().available()
    assert not ok and reason.startswith("API_KEY_BACKEND") and "OPENAI_API_KEY" in reason
    assert "sk-" not in reason


def test_launch_timeout_env_is_clamped(monkeypatch):
    monkeypatch.setenv(ch.LAUNCH_TIMEOUT_ENV, "0.5")
    assert ch.ClaudeBackend().launch_timeout_s == 0.5
    monkeypatch.setenv(ch.LAUNCH_TIMEOUT_ENV, "99999")
    assert ch.ClaudeBackend().launch_timeout_s == ch.LAUNCH_TIMEOUT_S
    monkeypatch.setenv(ch.LAUNCH_TIMEOUT_ENV, "junk")
    assert ch.CodexBackend().launch_timeout_s == ch.LAUNCH_TIMEOUT_S


# ── Claude launch ───────────────────────────────────────────────────────────


def test_claude_launch_argv_clone_and_snapshot(install_fake, origin, state, tmp_path):
    install_fake("claude", FAKE_CLOUD_MODE="ok-with-url")
    ctx = launch_ctx(origin, tmp_path)
    result = ch.ClaudeBackend().launch(ctx)

    assert result.outcome == "launched" and result.rc == 0
    assert result.remote_id == "session_01FAKEabcdef12345"
    assert result.url == "https://claude.ai/code/session_01FAKEabcdef12345"
    assert recorded_argv(state, "claude.argv") == [
        "--cloud", "Do the thing", "--model", "claude-opus-test", "--effort", "high",
    ]
    clone_cwd = Path((state / "claude.cwd").read_text().strip())
    assert clone_cwd != origin.work.resolve()
    assert not clone_cwd.exists(), "clone must be removed after launch"
    assert (state / "claude.head").read_text().strip() == origin.oid
    refs = set((state / "claude.refs").read_text().split())
    assert refs <= {"refs/heads/" + origin.branch, "refs/remotes/origin/" + origin.branch}
    assert "refs/heads/" + origin.branch in refs
    assert not any("claude/" in r or "other" in r or "tags" in r for r in refs)
    snapshot = json.loads(ctx.refs_snapshot_path.read_text())
    assert list(snapshot) == ["refs/heads/claude/old-1"]
    assert stat.S_IMODE(ctx.refs_snapshot_path.stat().st_mode) == 0o600


def test_claude_launch_accepts_refs_heads_prefix(install_fake, origin, state, tmp_path):
    install_fake("claude")
    ctx = launch_ctx(origin, tmp_path, task_branch="refs/heads/" + origin.branch)
    assert ch.ClaudeBackend().launch(ctx).outcome == "launched"


def test_claude_launch_without_id_is_still_launched(install_fake, origin, tmp_path):
    install_fake("claude", FAKE_CLOUD_MODE="ok-no-output")
    result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path))
    assert (result.outcome, result.rc, result.remote_id, result.url) == ("launched", 0, None, None)


def test_claude_launch_nonzero_rc_is_failed(install_fake, origin, tmp_path):
    install_fake("claude", FAKE_CLOUD_MODE="fail")
    ctx = launch_ctx(origin, tmp_path)
    result = ch.ClaudeBackend().launch(ctx)
    assert (result.outcome, result.rc) == ("failed", 2)
    assert b"fake claude failure" in ctx.log_path.read_bytes()


def test_claude_launch_omits_empty_model_and_effort(install_fake, origin, state, tmp_path):
    install_fake("claude")
    ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path, model=None, effort=""))
    assert recorded_argv(state, "claude.argv") == ["--cloud", "Do the thing"]


def test_claude_launch_prompt_cannot_look_like_an_option(install_fake, origin, state, tmp_path):
    install_fake("claude")
    ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path, prompt="--dangerous"))
    assert recorded_argv(state, "claude.argv")[1] == " --dangerous"


def test_claude_launch_branch_oid_mismatch(install_fake, origin, state, tmp_path):
    install_fake("claude")
    ctx = launch_ctx(origin, tmp_path, task_branch_oid="f" * 40)
    result = ch.ClaudeBackend().launch(ctx)
    assert (result.outcome, result.detail) == ("failed", "BRANCH_OID_MISMATCH")
    assert not (state / "claude.argv").exists(), "provider must not run"
    assert not ctx.refs_snapshot_path.exists()


def test_claude_launch_clone_failure(install_fake, origin, state, tmp_path):
    install_fake("claude")
    ctx = launch_ctx(origin, tmp_path, clone_url="file://" + str(tmp_path / "missing.git"))
    result = ch.ClaudeBackend().launch(ctx)
    assert (result.outcome, result.detail) == ("failed", "CLONE_FAILED")
    assert not (state / "claude.argv").exists()


def test_claude_launch_missing_branch_fails(install_fake, origin, state, tmp_path):
    install_fake("claude")
    result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path, task_branch="ffs/cloud/r1/nope-g1"))
    assert (result.outcome, result.detail) == ("failed", "CLONE_FAILED")


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"task_branch": "-x"}, "TASK_BRANCH_INVALID"),
        ({"task_branch": "a/../b"}, "TASK_BRANCH_INVALID"),
        ({"clone_url": "--upload-pack=x"}, "CLONE_URL_INVALID"),
        ({"task_branch_oid": ""}, "BRANCH_OID_MISSING"),
        ({"refs_snapshot_path": None}, "SNAPSHOT_PATH_MISSING"),
        ({"model": "a b"}, "MODEL_INVALID"),
        ({"effort": "-high"}, "EFFORT_INVALID"),
        ({"prompt": ""}, "PROMPT_INVALID"),
        ({"prompt": "a\x00b"}, "PROMPT_INVALID"),
    ],
)
def test_claude_launch_rejects_bad_inputs(install_fake, origin, state, tmp_path, overrides, code):
    install_fake("claude")
    result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path, **overrides))
    assert (result.outcome, result.detail, result.rc) == ("failed", code, ch.NOT_RUN_RC)
    assert not (state / "claude.argv").exists()


def test_claude_launch_refuses_api_key(install_fake, origin, state, tmp_path, monkeypatch):
    install_fake("claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path))
    assert (result.outcome, result.detail) == ("failed", "API_KEY_BACKEND")
    assert not (state / "claude.argv").exists()


def test_claude_launch_binary_missing(origin, tmp_path):
    result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path))
    assert (result.outcome, result.detail) == ("failed", "BIN_NOT_FOUND")


def test_claude_launch_timeout_is_uncertain_and_cleans_up(install_fake, origin, state, tmp_path):
    install_fake("claude", FAKE_CLOUD_MODE="hang")
    started = time.monotonic()
    result = ch.ClaudeBackend(launch_timeout_s=1.0).launch(launch_ctx(origin, tmp_path))
    assert time.monotonic() - started < 30
    assert (result.outcome, result.rc) == ("uncertain", ch.TIMEOUT_RC)
    assert "LAUNCH_TIMEOUT" in result.detail
    assert not Path((state / "claude.cwd").read_text().strip()).exists()
    pid = int((state / "claude.pid").read_text())
    deadline = time.monotonic() + 5
    alive = True
    while alive and time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.05)
        except ProcessLookupError:
            alive = False
    assert not alive, "hung provider must be killed"


def test_child_env_is_an_allowlist(install_fake, origin, state, tmp_path, monkeypatch):
    install_fake("claude")
    monkeypatch.setenv("FFS_CANARY_TOKEN", "canary-value-xyz")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "a" * 36)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")
    ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path))
    env_dump = (state / "claude.env").read_text()
    seen = dict(line.split("=", 1) for line in env_dump.splitlines() if "=" in line)
    assert "canary-value-xyz" not in env_dump
    assert "GITHUB_TOKEN" not in seen and "FFS_CANARY_TOKEN" not in seen
    assert not any(k.startswith("FFS_CLOUD_BIN") for k in seen)
    assert seen["SSH_AUTH_SOCK"] == "/tmp/fake-agent.sock"
    assert seen["GIT_TERMINAL_PROMPT"] == "0"
    wrapper_vars = {"FAKE_CLOUD_STATE", "FAKE_CLOUD_MODE"}
    allowed = set(ch.ENV_ALLOWLIST) | set(ch.GIT_HARDENING_ENV) | wrapper_vars | {"PWD", "SHLVL", "_", "OLDPWD"}
    assert set(seen) <= allowed, set(seen) - allowed


def test_child_env_function_drops_unlisted():
    env = ch.child_env({"PATH": "/x", "SECRET": "s", "HOME": "/h", "OPENAI_API_KEY": "k"})
    assert env["PATH"] == "/x" and env["HOME"] == "/h"
    assert "SECRET" not in env and "OPENAI_API_KEY" not in env
    assert ch.child_env({})["PATH"] == ch.DEFAULT_PATH


# ── Log cap and sanitize hook ───────────────────────────────────────────────


def test_log_is_capped_at_64kb_and_private(install_fake, origin, tmp_path):
    install_fake("claude", FAKE_CLOUD_EXTRA_BYTES=str(300 * 1024))
    ctx = launch_ctx(origin, tmp_path)
    ctx.log_path.parent.mkdir(parents=True)
    ctx.log_path.write_bytes(b"old")
    ctx.log_path.chmod(0o644)
    result = ch.ClaudeBackend().launch(ctx)
    assert result.outcome == "launched"
    assert ctx.log_path.stat().st_size == ch.LOG_CAP_BYTES
    assert stat.S_IMODE(ctx.log_path.stat().st_mode) == 0o600


def test_sanitize_runs_before_truncation_and_nothing_raw_is_written(install_fake, origin, tmp_path):
    secret = "sk-ant-SECRETVALUE123"
    install_fake("claude", FAKE_CLOUD_EXTRA_TEXT=secret, FAKE_CLOUD_EXTRA_BYTES=str(200 * 1024))
    seen = {}

    def sanitize(data):
        seen["raw"] = data
        return data.replace(secret.encode(), b"[REDACTED:api-key]")

    ctx = launch_ctx(origin, tmp_path, sanitize=sanitize)
    ch.ClaudeBackend().launch(ctx)
    assert secret.encode() in seen["raw"], "hook receives the raw bytes"
    written = ctx.log_path.read_bytes()
    assert secret.encode() not in written
    assert b"[REDACTED:api-key]" in written
    assert len(written) <= ch.LOG_CAP_BYTES


def test_sanitizer_failure_withholds_output(install_fake, origin, tmp_path):
    install_fake("claude", FAKE_CLOUD_EXTRA_TEXT="sk-ant-SECRETVALUE123")

    def boom(_data):
        raise RuntimeError("scanner crashed")

    ctx = launch_ctx(origin, tmp_path, sanitize=boom)
    result = ch.ClaudeBackend().launch(ctx)
    assert result.outcome == "launched"
    assert "SANITIZER_FAILED" in result.detail
    assert ctx.log_path.read_bytes() == ch.SANITIZER_FAILED_NOTICE


def test_sanitizer_returning_non_bytes_withholds_output(install_fake, origin, tmp_path):
    install_fake("claude")
    ctx = launch_ctx(origin, tmp_path, sanitize=lambda d: "text")
    ch.ClaudeBackend().launch(ctx)
    assert ctx.log_path.read_bytes() == ch.SANITIZER_FAILED_NOTICE


def test_session_id_is_parsed_from_sanitized_output_only(install_fake, origin, tmp_path):
    install_fake("claude")
    ctx = launch_ctx(origin, tmp_path, sanitize=lambda d: b"nothing useful\n")
    result = ch.ClaudeBackend().launch(ctx)
    assert (result.remote_id, result.url) == (None, None)
    assert result.outcome == "launched"


def test_log_write_failure_does_not_change_outcome(install_fake, origin, tmp_path):
    install_fake("claude")
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    ctx = launch_ctx(origin, tmp_path, log_path=blocker / "launch.log")
    result = ch.ClaudeBackend().launch(ctx)
    assert result.outcome == "launched"
    assert "LOG_WRITE_FAILED" in result.detail


# ── Claude poll ─────────────────────────────────────────────────────────────


def write_snapshot_for(origin, tmp_path):
    listing = subprocess.run(
        ["git", "ls-remote", origin.url, "refs/heads/claude/*"], capture_output=True, text=True, check=True
    ).stdout
    refs = ch.parse_ls_remote(listing)
    path = tmp_path / "logs" / "claude_refs_at_launch.json"
    ch.write_snapshot(path, refs)
    return refs


def test_poll_no_changes_is_running_without_candidates(origin, tmp_path):
    write_snapshot_for(origin, tmp_path)
    result = ch.ClaudeBackend().poll(poll_ctx(origin, tmp_path))
    assert result.state == "running" and result.candidates == []


def test_poll_nonce_match_first_then_new_then_changed(origin, tmp_path):
    write_snapshot_for(origin, tmp_path)
    new_oid = origin.push_ref("claude/aaa-new")
    named_oid = origin.push_ref(CLAUDE_RESULT_BRANCH)
    changed_oid = origin.push_ref("claude/old-1")
    result = ch.ClaudeBackend().poll(poll_ctx(origin, tmp_path))
    assert result.state == "running"
    assert result.candidates == [
        ("refs/heads/" + CLAUDE_RESULT_BRANCH, named_oid),
        ("refs/heads/claude/aaa-new", new_oid),
        ("refs/heads/claude/old-1", changed_oid),
    ]


def test_poll_drops_rejected_pairs_given_as_json_lists(origin, tmp_path):
    write_snapshot_for(origin, tmp_path)
    oid = origin.push_ref(CLAUDE_RESULT_BRANCH)
    ref = "refs/heads/" + CLAUDE_RESULT_BRANCH
    ctx = poll_ctx(origin, tmp_path, rejected=json.loads(json.dumps([[ref, oid]])))
    assert ch.ClaudeBackend().poll(ctx).candidates == []
    moved = origin.push_ref(CLAUDE_RESULT_BRANCH)
    assert ch.ClaudeBackend().poll(ctx).candidates == [(ref, moved)]


def test_poll_caps_candidates(origin, tmp_path):
    write_snapshot_for(origin, tmp_path)
    for index in range(25):
        origin.push_ref("claude/n%02d" % index)
    result = ch.ClaudeBackend().poll(poll_ctx(origin, tmp_path))
    assert len(result.candidates) == ch.MAX_CANDIDATES
    assert "CAPPED_AT_20" in result.detail


def test_poll_without_snapshot_uses_nonce_matches_only(origin, tmp_path):
    origin.push_ref("claude/unrelated-new")
    oid = origin.push_ref(CLAUDE_RESULT_BRANCH)
    result = ch.ClaudeBackend().poll(poll_ctx(origin, tmp_path))
    assert result.candidates == [("refs/heads/" + CLAUDE_RESULT_BRANCH, oid)]
    assert "NO_SNAPSHOT" in result.detail


def test_poll_ls_remote_failure_is_unknown(origin, tmp_path):
    ctx = poll_ctx(origin, tmp_path, clone_url="file://" + str(tmp_path / "gone.git"))
    result = ch.ClaudeBackend().poll(ctx)
    assert (result.state, result.candidates, result.detail) == ("unknown", [], "LS_REMOTE_FAILED")


def test_poll_rejects_option_like_url(origin, tmp_path):
    result = ch.ClaudeBackend().poll(poll_ctx(origin, tmp_path, clone_url="--exec=evil"))
    assert result.state == "unknown"


def test_launch_simulated_result_then_poll_and_fetch(install_fake, origin, state, tmp_path):
    """End to end: launch snapshots, the 'cloud worker' pushes, poll finds it, fetch quarantines it."""
    install_fake(
        "claude",
        FAKE_CLOUD_ORIGIN=origin.path,
        FAKE_CLOUD_PUSH_BRANCH=CLAUDE_RESULT_BRANCH,
        FAKE_CLOUD_MANIFEST='{"schema":1,"task":"t1"}',
    )
    backend = ch.ClaudeBackend()
    assert backend.launch(launch_ctx(origin, tmp_path)).outcome == "launched"
    polled = backend.poll(poll_ctx(origin, tmp_path))
    assert len(polled.candidates) == 1
    ref, oid = polled.candidates[0]
    assert ref == "refs/heads/" + CLAUDE_RESULT_BRANCH
    repo = quarantine(tmp_path)
    assert backend.fetch(poll_ctx(origin, tmp_path), (ref, oid), repo) == oid
    tree = git("ls-tree", "-r", "--name-only", oid, cwd=repo).splitlines()
    assert ".ffs-result/fake.json" in tree and "task.txt" in tree
    assert git("remote", cwd=repo) == ""
    assert git("rev-parse", "%s^" % oid, cwd=repo) == origin.oid


def test_two_tasks_of_one_run_never_bind_each_others_result(install_fake, origin, state, tmp_path):
    """Both results land AFTER both launches: each task's poll must see only its own branch."""
    nonce1, nonce2 = NONCE, "ef012345" + "0" * 24
    branch1, branch2 = CLAUDE_RESULT_BRANCH, "claude/ffs-r1-t2-ef012345"
    snap = lambda task: tmp_path / "logs" / ("%s_refs.json" % task)  # noqa: E731
    backend = ch.ClaudeBackend()
    for task, branch in (("t1", branch1), ("t2", branch2)):
        install_fake("claude", FAKE_CLOUD_ORIGIN=origin.path, FAKE_CLOUD_PUSH_BRANCH=branch,
                     FAKE_CLOUD_PUSH_MODE="deferred", FAKE_CLOUD_MANIFEST='{"schema":1,"task":"%s"}' % task)
        ctx = launch_ctx(origin, tmp_path, task=task, refs_snapshot_path=snap(task),
                         log_path=tmp_path / "logs" / ("launch-%s.log" % task))
        assert backend.launch(ctx).outcome == "launched"
    # nothing is visible yet: the workers have not pushed
    assert backend.poll(poll_ctx(origin, tmp_path, refs_snapshot_path=snap("t1"))).candidates == []
    subprocess.run([str(tmp_path / "wrap-claude"), "--flush"], check=True)  # both results appear now
    first = backend.poll(poll_ctx(origin, tmp_path, task="t1", nonce=nonce1, refs_snapshot_path=snap("t1")))
    second = backend.poll(poll_ctx(origin, tmp_path, task="t2", nonce=nonce2, refs_snapshot_path=snap("t2")))
    assert [r for r, _ in first.candidates] == ["refs/heads/" + branch1]
    assert [r for r, _ in second.candidates] == ["refs/heads/" + branch2]


# ── Claude fetch ────────────────────────────────────────────────────────────



def test_fetch_into_remoteless_quarantine(origin, tmp_path):
    oid = origin.push_ref(CLAUDE_RESULT_BRANCH)
    repo = quarantine(tmp_path)
    result = ch.ClaudeBackend().fetch(poll_ctx(origin, tmp_path), ("refs/heads/" + CLAUDE_RESULT_BRANCH, oid), repo)
    assert result == oid
    git("cat-file", "-e", oid + "^{commit}", cwd=repo)
    assert git("remote", cwd=repo) == ""


def test_fetch_leaves_no_promisor_remote_or_url_in_quarantine(origin, tmp_path):
    """A --filter fetch registers remote.<url>.promisor; it must be scrubbed so later reads stay offline."""
    oid = origin.push_ref(CLAUDE_RESULT_BRANCH)
    repo = quarantine(tmp_path)
    ch.ClaudeBackend().fetch(poll_ctx(origin, tmp_path), ("refs/heads/" + CLAUDE_RESULT_BRANCH, oid), repo)
    config = (repo / ".git" / "config").read_text()
    assert "promisor" not in config and "partialclone" not in config.lower()
    assert str(origin.path) not in config
    assert git("remote", cwd=repo) == ""


def test_fetch_refuses_quarantine_with_remote(origin, tmp_path):
    oid = origin.push_ref(CLAUDE_RESULT_BRANCH)
    repo = quarantine(tmp_path)
    git("remote", "add", "origin", origin.url, cwd=repo)
    with pytest.raises(ch.FetchError) as err:
        ch.ClaudeBackend().fetch(poll_ctx(origin, tmp_path), ("refs/heads/" + CLAUDE_RESULT_BRANCH, oid), repo)
    assert err.value.code == "QUARANTINE_HAS_REMOTES"


def test_a_git_warning_on_stderr_is_not_a_remote(tmp_path, monkeypatch):
    """B5: the remote check reads stdout only; a warning about an absolute-path origin is not a remote."""
    repo = quarantine(tmp_path)
    shim = tmp_path / "bin"
    shim.mkdir()
    real = subprocess.run(["which", "git"], capture_output=True, text=True, check=True).stdout.strip()
    git_shim = shim / "git"
    git_shim.write_text("#!/bin/bash\nfor a in \"$@\"; do if [ \"$a\" = remote ]; then\n"
                        "echo 'warning: /abs/origin looks like a path, not a remote' >&2; fi; done\n"
                        "exec %s \"$@\"\n" % shlex.quote(real))
    git_shim.chmod(0o755)
    monkeypatch.setattr(ch, "DEFAULT_PATH", str(shim) + ":" + os.environ["PATH"])
    monkeypatch.setenv("PATH", str(shim) + ":" + os.environ["PATH"])
    ch._quarantine_check(repo, 10)  # must not raise


def test_a_real_remote_is_still_refused_with_the_stdout_only_check(origin, tmp_path):
    repo = quarantine(tmp_path)
    git("remote", "add", "origin", origin.url, cwd=repo)
    with pytest.raises(ch.FetchError) as err:
        ch._quarantine_check(repo, 10)
    assert err.value.code == "QUARANTINE_HAS_REMOTES"


def test_fetch_refuses_non_repo(origin, tmp_path):
    oid = origin.push_ref(CLAUDE_RESULT_BRANCH)
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ch.FetchError) as err:
        ch.ClaudeBackend().fetch(poll_ctx(origin, tmp_path), ("refs/heads/" + CLAUDE_RESULT_BRANCH, oid), plain)
    assert err.value.code in {"QUARANTINE_NOT_A_REPO", "QUARANTINE_HAS_REMOTES"}


@pytest.mark.parametrize(
    "candidate",
    [("refs/heads/main", "a" * 40), ("refs/heads/claude/x", "not-an-oid"), ("", ""), ("refs/heads/claude/x", "-x")],
)
def test_fetch_rejects_invalid_candidates(origin, tmp_path, candidate):
    with pytest.raises(ch.FetchError) as err:
        ch.ClaudeBackend().fetch(poll_ctx(origin, tmp_path), candidate, quarantine(tmp_path))
    assert err.value.code == "CANDIDATE_INVALID"


def test_fetch_unknown_oid_fails_typed(origin, tmp_path):
    with pytest.raises(ch.FetchError) as err:
        ch.ClaudeBackend().fetch(
            poll_ctx(origin, tmp_path), ("refs/heads/claude/old-1", "1" * 40), quarantine(tmp_path)
        )
    assert err.value.code == "FETCH_FAILED"


# ── Codex launch ────────────────────────────────────────────────────────────


def codex_ctx(origin, tmp_path, **overrides):
    values = dict(env_id="env_test_1", model=None, effort=None, refs_snapshot_path=None, task_branch_oid="")
    values.update(overrides)
    return launch_ctx(origin, tmp_path, **values)


def test_codex_launch_argv_exactness(install_fake, origin, state, tmp_path):
    install_fake("codex", FAKE_CLOUD_MODE="ok")
    ctx = codex_ctx(origin, tmp_path, task_branch="refs/heads/" + origin.branch)
    result = ch.CodexBackend().launch(ctx)
    assert recorded_argv(state, "codex.exec.argv") == [
        "cloud", "exec", "--env", "env_test_1", "--branch", origin.branch, "Do the thing",
    ]
    assert (result.outcome, result.rc, result.remote_id) == ("launched", 0, "task_e_fake0001")
    assert result.url == "https://chatgpt.com/codex/tasks/task_e_fake0001"
    cwd = Path((state / "codex.exec.cwd").read_text().strip())
    assert cwd != origin.work.resolve() and not cwd.exists()
    assert ctx.log_path.exists() and stat.S_IMODE(ctx.log_path.stat().st_mode) == 0o600


def test_codex_launch_without_id_is_uncertain(install_fake, origin, tmp_path):
    install_fake("codex", FAKE_CLOUD_MODE="no-id")
    result = ch.CodexBackend().launch(codex_ctx(origin, tmp_path))
    assert (result.outcome, result.rc, result.remote_id) == ("uncertain", 0, None)
    assert "NO_REMOTE_ID" in result.detail


def test_codex_launch_requires_env_id(install_fake, origin, state, tmp_path):
    install_fake("codex")
    result = ch.CodexBackend().launch(codex_ctx(origin, tmp_path, env_id=None))
    assert (result.outcome, result.detail) == ("failed", "ENV_ID_MISSING")
    assert not (state / "codex.exec.argv").exists()


def test_codex_launch_refuses_api_key(install_fake, origin, state, tmp_path, monkeypatch):
    install_fake("codex")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-a-real-key")
    result = ch.CodexBackend().launch(codex_ctx(origin, tmp_path))
    assert (result.outcome, result.detail) == ("failed", "API_KEY_BACKEND")
    assert not (state / "codex.exec.argv").exists()


def test_codex_launch_binary_missing(origin, tmp_path):
    result = ch.CodexBackend().launch(codex_ctx(origin, tmp_path))
    assert (result.outcome, result.detail) == ("failed", "BIN_NOT_FOUND")


def test_codex_launch_timeout_is_uncertain(install_fake, origin, state, tmp_path):
    install_fake("codex", FAKE_CLOUD_MODE="hang")
    result = ch.CodexBackend(launch_timeout_s=1.0).launch(codex_ctx(origin, tmp_path))
    assert (result.outcome, result.rc) == ("uncertain", ch.TIMEOUT_RC)


def test_codex_child_env_is_an_allowlist(install_fake, origin, state, tmp_path, monkeypatch):
    install_fake("codex")
    monkeypatch.setenv("FFS_CANARY_TOKEN", "canary-value-xyz")
    ch.CodexBackend().launch(codex_ctx(origin, tmp_path))
    assert "canary-value-xyz" not in (state / "codex.exec.env").read_text()


def test_codex_launch_sanitizes_log(install_fake, origin, tmp_path):
    install_fake("codex")
    ctx = codex_ctx(origin, tmp_path, sanitize=lambda d: d.replace(b"task_e_fake0001", b"[REDACTED]"))
    result = ch.CodexBackend().launch(ctx)
    assert b"task_e_fake0001" not in ctx.log_path.read_bytes()
    assert result.outcome == "uncertain", "ids are parsed from sanitized output only"


# ── Codex poll ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("fmt", ["text", "json"])
@pytest.mark.parametrize(
    "mode, expected",
    [("running", "running"), ("ok", "running"), ("complete", "result-ready"), ("failed", "failed")],
)
def test_codex_poll_states(install_fake, origin, tmp_path, state, mode, expected, fmt):
    install_fake("codex", FAKE_CLOUD_MODE=mode, FAKE_CLOUD_FORMAT=fmt)
    result = ch.CodexBackend().poll(poll_ctx(origin, tmp_path, remote_id="task_e_fake0001"))
    assert result.state == expected and result.candidates == []
    assert recorded_argv(state, "codex.status.argv") == ["cloud", "status", "task_e_fake0001"]


def test_codex_poll_without_remote_id_is_unknown(install_fake, origin, state, tmp_path):
    install_fake("codex")
    result = ch.CodexBackend().poll(poll_ctx(origin, tmp_path, remote_id=None))
    assert (result.state, result.detail) == ("unknown", "NO_REMOTE_ID")
    assert not (state / "codex.status.argv").exists()


def test_codex_poll_option_like_id_is_refused(install_fake, origin, state, tmp_path):
    install_fake("codex")
    result = ch.CodexBackend().poll(poll_ctx(origin, tmp_path, remote_id="--help"))
    assert result.state == "unknown" and not (state / "codex.status.argv").exists()


def test_codex_poll_command_timeout_is_unknown(install_fake, origin, tmp_path, monkeypatch):
    install_fake("codex", FAKE_CLOUD_MODE="hang")
    monkeypatch.setattr(ch, "STATUS_TIMEOUT_S", 0.5)
    result = ch.CodexBackend().poll(poll_ctx(origin, tmp_path, remote_id="task_e_fake0001"))
    assert (result.state, result.detail) == ("unknown", "STATUS_CMD_FAILED")


# ── Codex fetch ─────────────────────────────────────────────────────────────


def prepared_quarantine(origin, tmp_path):
    repo = tmp_path / "cq"
    git("clone", "-q", "--no-checkout", origin.path, repo)
    git("checkout", "-q", "--detach", origin.base, cwd=repo)
    git("remote", "remove", "origin", cwd=repo)
    return repo


def test_codex_fetch_applies_diff_and_commits(install_fake, origin, tmp_path):
    install_fake("codex", FAKE_CLOUD_MODE="complete")
    repo = prepared_quarantine(origin, tmp_path)
    ctx = poll_ctx(origin, tmp_path, remote_id="task_e_fake0001", generation=3)
    oid = ch.CodexBackend().fetch(ctx, ("", ""), repo)
    assert oid == git("rev-parse", "HEAD", cwd=repo)
    assert git("rev-parse", oid + "^", cwd=repo) == origin.base
    assert git("log", "-1", "--format=%s", cwd=repo) == "ffs cloud result r1/t1 g3"
    assert git("log", "-1", "--format=%an <%ae>|%cn <%ce>", cwd=repo) == (
        "%s <%s>|%s <%s>" % (ch.COMMIT_NAME, ch.COMMIT_EMAIL, ch.COMMIT_NAME, ch.COMMIT_EMAIL)
    )
    assert git("show", oid + ":codex-out.txt", cwd=repo) == "hello from codex"
    assert git("status", "--porcelain", cwd=repo) == ""


def test_codex_fetch_refuses_a_patch_that_grows_the_quarantine_too_much(install_fake, origin, tmp_path, monkeypatch):
    install_fake("codex", FAKE_CLOUD_MODE="complete")
    repo = prepared_quarantine(origin, tmp_path)
    monkeypatch.setattr(ch, "APPLIED_GROWTH_CAP_BYTES", 5)
    with pytest.raises(ch.FetchError) as err:
        ch.CodexBackend().fetch(poll_ctx(origin, tmp_path, remote_id="task_e_fake0001"), ("", ""), repo)
    assert err.value.code == "DIFF_TOO_LARGE"


def test_codex_fetch_caps_a_single_inflated_file(install_fake, origin, tmp_path, monkeypatch):
    """RLIMIT_FSIZE kills `git apply` before a zlib bomb can fill the disk."""
    install_fake("codex", FAKE_CLOUD_MODE="complete")
    repo = prepared_quarantine(origin, tmp_path)
    monkeypatch.setattr(ch, "APPLIED_FILE_CAP_BYTES", 4)
    with pytest.raises(ch.FetchError) as err:
        ch.CodexBackend().fetch(poll_ctx(origin, tmp_path, remote_id="task_e_fake0001"), ("", ""), repo)
    assert err.value.code == "DIFF_TOO_LARGE"


def test_codex_fetch_within_the_caps_still_works(install_fake, origin, tmp_path):
    install_fake("codex", FAKE_CLOUD_MODE="complete")
    repo = prepared_quarantine(origin, tmp_path)
    assert ch.CodexBackend().fetch(poll_ctx(origin, tmp_path, remote_id="task_e_fake0001"), ("", ""), repo)


def test_codex_fetch_commit_ignores_hooks_and_signing(install_fake, origin, tmp_path):
    install_fake("codex", FAKE_CLOUD_MODE="complete")
    repo = prepared_quarantine(origin, tmp_path)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho hook-ran > %s\nexit 1\n" % (tmp_path / "hook-marker"))
    hook.chmod(0o755)
    git("config", "commit.gpgsign", "true", cwd=repo)
    ch.CodexBackend().fetch(poll_ctx(origin, tmp_path, remote_id="task_e_fake0001"), ("", ""), repo)
    assert not (tmp_path / "hook-marker").exists()


def test_codex_fetch_bad_diff(install_fake, origin, tmp_path):
    install_fake("codex", FAKE_CLOUD_MODE="bad-diff")
    repo = prepared_quarantine(origin, tmp_path)
    with pytest.raises(ch.FetchError) as err:
        ch.CodexBackend().fetch(poll_ctx(origin, tmp_path, remote_id="task_e_fake0001"), ("", ""), repo)
    assert err.value.code == "DIFF_APPLY_FAILED"
    assert git("rev-parse", "HEAD", cwd=repo) == origin.base
    assert git("status", "--porcelain", cwd=repo) == ""


def test_codex_fetch_empty_diff(install_fake, origin, tmp_path):
    empty = tmp_path / "empty.diff"
    empty.write_text("\n")
    install_fake("codex", FAKE_CLOUD_MODE="complete", FAKE_CODEX_DIFF=empty)
    repo = prepared_quarantine(origin, tmp_path)
    with pytest.raises(ch.FetchError) as err:
        ch.CodexBackend().fetch(poll_ctx(origin, tmp_path, remote_id="task_e_fake0001"), ("", ""), repo)
    assert err.value.code == "DIFF_EMPTY"


def test_codex_fetch_refuses_quarantine_not_at_base(install_fake, origin, tmp_path):
    install_fake("codex", FAKE_CLOUD_MODE="complete")
    repo = prepared_quarantine(origin, tmp_path)
    ctx = poll_ctx(origin, tmp_path, remote_id="task_e_fake0001", base_oid=origin.oid)
    with pytest.raises(ch.FetchError) as err:
        ch.CodexBackend().fetch(ctx, ("", ""), repo)
    assert err.value.code == "QUARANTINE_NOT_AT_BASE"


def test_codex_fetch_refuses_dirty_quarantine(install_fake, origin, tmp_path):
    install_fake("codex", FAKE_CLOUD_MODE="complete")
    repo = prepared_quarantine(origin, tmp_path)
    (repo / "stray.txt").write_text("x")
    with pytest.raises(ch.FetchError) as err:
        ch.CodexBackend().fetch(poll_ctx(origin, tmp_path, remote_id="task_e_fake0001"), ("", ""), repo)
    assert err.value.code == "QUARANTINE_DIRTY"


def test_codex_fetch_refuses_quarantine_with_remote(install_fake, origin, tmp_path):
    install_fake("codex", FAKE_CLOUD_MODE="complete")
    repo = prepared_quarantine(origin, tmp_path)
    git("remote", "add", "origin", origin.url, cwd=repo)
    with pytest.raises(ch.FetchError) as err:
        ch.CodexBackend().fetch(poll_ctx(origin, tmp_path, remote_id="task_e_fake0001"), ("", ""), repo)
    assert err.value.code == "QUARANTINE_HAS_REMOTES"


def test_codex_fetch_requires_remote_id(install_fake, origin, tmp_path):
    install_fake("codex", FAKE_CLOUD_MODE="complete")
    repo = prepared_quarantine(origin, tmp_path)
    with pytest.raises(ch.FetchError) as err:
        ch.CodexBackend().fetch(poll_ctx(origin, tmp_path, remote_id=None), ("", ""), repo)
    assert err.value.code == "FETCH_INPUT_INVALID"


def test_codex_fetch_diff_that_escapes_the_repo_fails(install_fake, origin, tmp_path):
    evil = tmp_path / "evil.diff"
    evil.write_text(
        "diff --git a/../escape.txt b/../escape.txt\nnew file mode 100644\n--- /dev/null\n+++ b/../escape.txt\n"
        "@@ -0,0 +1 @@\n+pwned\n"
    )
    install_fake("codex", FAKE_CLOUD_MODE="complete", FAKE_CODEX_DIFF=evil)
    repo = prepared_quarantine(origin, tmp_path)
    with pytest.raises(ch.FetchError) as err:
        ch.CodexBackend().fetch(poll_ctx(origin, tmp_path, remote_id="task_e_fake0001"), ("", ""), repo)
    assert err.value.code == "DIFF_APPLY_FAILED"
    assert not (tmp_path / "escape.txt").exists()


# ── Process helper ──────────────────────────────────────────────────────────


def test_run_capture_caps_memory_and_reports_truncation():
    result = ch.run_capture(
        ["/bin/bash", "-c", "head -c 5000 /dev/zero"], cwd=None, env=ch.child_env(), timeout=10, cap=100
    )
    assert len(result.output) == 100 and result.truncated and result.rc == 0


def test_run_capture_spawn_failure():
    result = ch.run_capture(["/no/such/binary"], cwd=None, env=ch.child_env(), timeout=5)
    assert result.spawn_failed and result.rc == ch.SPAWN_FAILED_RC and not result.ok


def test_run_capture_closes_stdin():
    result = ch.run_capture(["/bin/bash", "-c", "cat; echo done"], cwd=None, env=ch.child_env(), timeout=5)
    assert result.ok and result.output.strip() == b"done"


def test_write_private_file_replaces_symlink_target_safely(tmp_path):
    target = tmp_path / "target"
    target.write_text("keep")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        ch.write_private_file(link, b"x")
    assert target.read_text() == "keep"
