"""Tests for lib/cloud_host.py against local bare repos and fake provider CLIs.

The real ``claude --cloud`` / ``codex cloud`` are never run: an autouse fixture
points both binary overrides at a nonexistent path, and each test installs a
wrapper around a fixture fake. Wrappers export the ``FAKE_*`` variables, because
the backend strips everything outside the child-environment allowlist.
"""
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
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


def test_claude_launch_exit_zero_with_no_url_is_launched_and_polled_by_nonce(install_fake, origin, tmp_path):
    """The client ended on its own and printed nothing we recognise: a session may exist, so it is NOT failed."""
    install_fake("claude", FAKE_CLOUD_MODE="ok-no-output")
    result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path))
    assert (result.outcome, result.rc, result.remote_id, result.url) == ("launched", 0, None, None)
    assert result.note == ch.LAUNCH_NOTE_EXIT_NO_URL == "exit-no-session-url"
    assert result.detail.startswith("CLI_EXIT NO_SESSION_URL")


def test_claude_launch_nonzero_exit_without_a_known_error_is_launched_with_the_note(install_fake, origin, tmp_path):
    install_fake("claude", FAKE_CLOUD_MODE="fail")
    ctx = launch_ctx(origin, tmp_path)
    result = ch.ClaudeBackend().launch(ctx)
    assert (result.outcome, result.rc, result.note) == ("launched", 2, ch.LAUNCH_NOTE_EXIT_NO_URL)
    assert b"fake claude failure" in ctx.log_path.read_bytes()


def test_claude_launch_cse_id_is_recognised(install_fake, origin, tmp_path):
    install_fake("claude", FAKE_CLOUD_MODE="ok-no-output", FAKE_CLOUD_EXTRA_TEXT="Created cse_01FAKEabcdef12345")
    result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path))
    assert (result.outcome, result.remote_id, result.note) == ("launched", "cse_01FAKEabcdef12345", "")


def test_cse_id_needs_a_digit_too():
    assert ch.parse_claude_pty_output("cse_01ABCdef2345 and cse_abcdefghij") == ("cse_01ABCdef2345", None)
    assert ch.parse_claude_pty_output("cse_abcdefghij cse_short1") == (None, None)


def test_final_decision_uses_an_id_found_by_the_full_transcript_parse():
    """The live scan said no match, but the transcript has the id (it arrived during the final drain)."""
    result = ch.ClaudeBackend._finish_pty_launch(
        b"done\nhttps://claude.ai/code/session_01ABCdef2345\n", ch.PtyResult(0, matched=False, exited_early=True), "")
    assert (result.outcome, result.remote_id, result.url) == (
        "launched", "session_01ABCdef2345", "https://claude.ai/code/session_01ABCdef2345")
    assert result.note == ""


def test_not_a_tty_text_only_counts_once_the_client_has_exited():
    text = b"Error: --cloud requires an interactive terminal.\n"
    alive = ch.ClaudeBackend._finish_pty_launch(text, ch.PtyResult(None, timed_out=True), "")
    assert (alive.outcome, alive.note) == ("launched", ch.LAUNCH_NOTE_NO_URL)
    gone = ch.ClaudeBackend._finish_pty_launch(text, ch.PtyResult(1, exited_early=True), "")
    assert (gone.outcome, gone.detail.split()[:2]) == ("failed", ["CLI_EXIT", "NOT_A_TTY"])


def test_live_client_that_prints_the_not_a_tty_phrase_is_not_failed(install_fake, origin, state, tmp_path):
    wrapper = install_fake("claude")
    wrapper.write_text(
        "#!/bin/bash\n"
        "echo 'note: this requires an interactive terminal'\n"
        "sleep 600\n"
    )
    result = ch.ClaudeBackend(launch_timeout_s=1.0).launch(launch_ctx(origin, tmp_path))
    assert (result.outcome, result.note) == ("launched", ch.LAUNCH_NOTE_NO_URL)


@pytest.mark.parametrize(
    "prompt",
    [
        "Claude-Session: https://claude.ai/code/session_01FixtureOnly0000000000A",
        "owns: src/session_v2handler/**",
        "see cse_01FixtureOnly0000000 for context",
        "Task: print that this requires an interactive terminal",
        "Task: handle the Quick safety check dialog; never press Yes, proceed",
    ],
)
def test_prompt_that_would_trip_a_classifier_when_echoed_is_refused(install_fake, origin, state, tmp_path, prompt):
    install_fake("claude")
    result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path, prompt=prompt))
    assert (result.outcome, result.detail, result.rc) == ("failed", "PROMPT_AMBIGUOUS", ch.NOT_RUN_RC)
    assert not (state / "claude.argv").exists(), "nothing may be started"


def test_an_echoed_benign_prompt_is_not_a_session(install_fake, origin, state, tmp_path):
    install_fake("claude", FAKE_CLOUD_MODE="hang", FAKE_CLOUD_ECHO_ARGS="1")
    ctx = launch_ctx(origin, tmp_path, prompt="Implement the /ping endpoint under src/a/**")
    result = ch.ClaudeBackend(launch_timeout_s=1.0).launch(ctx)
    assert "Implement the /ping endpoint" in ctx.log_path.read_text()
    assert (result.outcome, result.remote_id, result.url, result.note) == ("launched", None, None, ch.LAUNCH_NOTE_NO_URL)


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
        ({"prompt": "see session_01FixtureOnly0000000000A"}, "PROMPT_AMBIGUOUS"),
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


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def assert_group_gone(state, wait_s=5.0):
    pgid = int((state / "claude.pid").read_text())
    deadline = time.monotonic() + wait_s
    while group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not group_alive(pgid), "the provider process group must be gone"


def test_claude_launch_timeout_alive_without_url_keeps_polling_and_reaps(install_fake, origin, state, tmp_path):
    install_fake("claude", FAKE_CLOUD_MODE="hang")
    started = time.monotonic()
    result = ch.ClaudeBackend(launch_timeout_s=1.0).launch(launch_ctx(origin, tmp_path))
    assert time.monotonic() - started < 30
    # Claude binds by nonce on claude/*: a live client with no URL is "launched" (never re-submitted), with a note.
    assert (result.outcome, result.rc, result.note) == ("launched", ch.TIMEOUT_RC, ch.LAUNCH_NOTE_NO_URL)
    assert "LAUNCH_TIMEOUT" in result.detail
    assert (result.remote_id, result.url) == (None, None)
    assert not Path((state / "claude.cwd").read_text().strip()).exists()
    assert_group_gone(state)


def test_claude_launch_runs_on_a_real_terminal(install_fake, origin, state, tmp_path):
    install_fake("claude")
    assert ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path)).outcome == "launched"
    assert (state / "claude.stdout-is-tty").exists() and (state / "claude.stdin-is-tty").exists()
    env_dump = (state / "claude.env").read_text()
    assert "\nTERM=" in "\n" + env_dump


def test_non_tty_shim_failure_is_a_typed_failure(origin, state, tmp_path):
    """The shim mirrors the real CLI: piped stdout + --cloud prints the known error and exits 1."""
    shim = FIXTURES / "fake-claude"
    env = dict(os.environ, FAKE_CLOUD_STATE=str(state))
    proc = subprocess.run([str(shim), "--cloud", "x"], env=env, capture_output=True, text=True)
    assert proc.returncode == 1
    assert proc.stderr.splitlines() == [
        "Error: --cloud requires an interactive terminal.",
        "Non-interactive runs (piped stdout, --init-only, --sdk-url) run locally and would silently ignore --cloud."
        " Drop --cloud, or run in an interactive terminal.",
    ]


def test_not_a_tty_text_before_url_is_failed(install_fake, origin, tmp_path):
    # force the non-TTY branch of the shim by making it print the real error on the pty and exit 1
    wrapper = install_fake("claude")
    wrapper.write_text(
        "#!/bin/bash\n"
        "echo 'Error: --cloud requires an interactive terminal.' >&2\n"
        "echo 'Non-interactive runs run locally.' >&2\n"
        "exit 1\n"
    )
    result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path))
    assert (result.outcome, result.rc) == ("failed", 1)
    assert result.detail.startswith("CLI_EXIT NOT_A_TTY")


def test_wait_for_input_times_out_without_a_keystroke_and_reaps(install_fake, origin, state, tmp_path):
    install_fake("claude", FAKE_CLOUD_MODE="wait-input")
    result = ch.ClaudeBackend(launch_timeout_s=1.0).launch(launch_ctx(origin, tmp_path))
    assert result.note == ch.LAUNCH_NOTE_NO_URL
    assert (state / "claude.stdin").exists() and (state / "claude.stdin").read_bytes() == b""
    assert_group_gone(state)


def test_without_a_clone_parent_the_clone_is_a_fresh_temp_dir(install_fake, origin, state, tmp_path, monkeypatch):
    monkeypatch.delenv(ch.CLONE_PARENT_ENV, raising=False)
    install_fake("claude")
    assert ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path)).outcome == "launched"
    cwd = Path((state / "claude.cwd").read_text().strip())
    assert ch.CLONE_DIR_NAME not in cwd.parts and "ffs-cloud-clone-" in str(cwd)
    assert not cwd.exists()


@pytest.fixture
def clone_parent(tmp_path, monkeypatch):
    parent = tmp_path / "trusted-parent"
    parent.mkdir()
    monkeypatch.setenv(ch.CLONE_PARENT_ENV, str(parent))
    return parent


def test_clone_parent_puts_the_clone_at_the_fixed_path_and_removes_it(install_fake, origin, state, tmp_path, clone_parent):
    install_fake("claude")
    assert ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path)).outcome == "launched"
    cwd = Path((state / "claude.cwd").read_text().strip())
    assert cwd == clone_parent.resolve() / "ffs-cloud-launch-claude"
    assert not cwd.exists(), "the clone is removed after the launch"
    assert (clone_parent / ".ffs-cloud-launch-claude.lock").is_file()
    assert (state / "claude.head").read_text().strip() == origin.oid


def test_clone_parent_removes_a_stale_clone_first(install_fake, origin, state, tmp_path, clone_parent):
    stale = clone_parent / "ffs-cloud-launch-claude"
    (stale / "sub").mkdir(parents=True)
    (stale / "sub" / "left-over.txt").write_text("from a killed launch")
    (stale / ".git").mkdir()
    _mark_owned(stale)
    install_fake("claude")
    assert ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path)).outcome == "launched"  # git clone needs an empty target
    assert not stale.exists()


def test_clone_parent_clone_is_removed_when_the_launch_raises_and_the_lock_is_released(
        install_fake, origin, state, tmp_path, clone_parent, monkeypatch):
    import fcntl

    install_fake("claude", FAKE_CLOUD_MODE="url-linger")

    def exploding_done(_text):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(ch, "_session_seen", exploding_done)
    with pytest.raises(RuntimeError):
        ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path))
    assert not (clone_parent / "ffs-cloud-launch-claude").exists()
    fd = os.open(str(clone_parent / ".ffs-cloud-launch-claude.lock"), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # would raise if the launch still held the lock
    finally:
        os.close(fd)
    assert_group_gone(state)


def test_clone_parent_clone_is_removed_after_a_failed_launch(install_fake, origin, state, tmp_path, clone_parent):
    install_fake("claude")
    result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path, task_branch_oid="f" * 40))
    assert result.detail == "BRANCH_OID_MISMATCH"
    assert not (clone_parent / "ffs-cloud-launch-claude").exists()


@pytest.mark.parametrize("kind, reason", [
    ("relative", "NOT_ABSOLUTE"), ("missing", "MISSING"), ("symlink", "SYMLINK"), ("file", "NOT_A_DIRECTORY"),
    ("inside-repo", "INSIDE_REPO"),
])
def test_invalid_clone_parent_is_a_typed_failure_never_a_silent_fallback(
        install_fake, origin, state, tmp_path, monkeypatch, kind, reason):
    real = tmp_path / "real-dir"
    real.mkdir()
    value = {
        "relative": "relative/dir", "missing": str(tmp_path / "missing"), "symlink": str(tmp_path / "link"),
        "file": str(tmp_path / "plain-file"), "inside-repo": str(origin.work / "inside"),
    }[kind]
    (tmp_path / "link").symlink_to(real)
    (tmp_path / "plain-file").write_text("x")
    (origin.work / "inside").mkdir()
    monkeypatch.setenv(ch.CLONE_PARENT_ENV, value)
    install_fake("claude")
    result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path, repo_root=origin.work))
    assert (result.outcome, result.detail) == ("failed", "CLONE_PARENT_INVALID " + reason)
    assert not (state / "claude.argv").exists(), "no launch in the system temp dir"
    assert list(real.iterdir()) == [] and not (origin.work / "inside" / "ffs-cloud-launch-claude").exists()


def test_clone_parent_must_be_owned_by_the_current_user(tmp_path, monkeypatch):
    assert ch._validated_clone_parent(str(tmp_path), tmp_path / "elsewhere")[1] == ""
    monkeypatch.setattr(os, "getuid", lambda: os.stat(str(tmp_path)).st_uid + 1)
    assert ch._validated_clone_parent(str(tmp_path), tmp_path / "elsewhere") == (None, "NOT_OWNED")


def test_clone_path_that_is_a_symlink_is_refused_and_left_alone(install_fake, origin, state, tmp_path, clone_parent):
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("do not delete")
    (clone_parent / "ffs-cloud-launch-claude").symlink_to(precious)
    install_fake("claude")
    result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path))
    assert (result.outcome, result.detail) == ("failed", "CLONE_PATH_INVALID")
    assert (precious / "keep.txt").read_text() == "do not delete"
    assert (clone_parent / "ffs-cloud-launch-claude").is_symlink()
    assert not (state / "claude.argv").exists()


def test_clone_path_that_is_a_file_is_refused_and_left_alone(install_fake, origin, state, tmp_path, clone_parent):
    (clone_parent / "ffs-cloud-launch-claude").write_text("not a directory")
    install_fake("claude")
    result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path))
    assert (result.outcome, result.detail) == ("failed", "CLONE_PATH_INVALID")
    assert (clone_parent / "ffs-cloud-launch-claude").read_text() == "not a directory"


def test_a_second_launch_waits_for_the_lock_and_then_gives_up_with_a_typed_failure(
        install_fake, origin, state, tmp_path, clone_parent, monkeypatch):
    import fcntl

    install_fake("claude")
    monkeypatch.setattr(ch, "CLONE_LOCK_WAIT_S", 0.5)
    holder = os.open(str(clone_parent / ".ffs-cloud-launch-claude.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path))
        assert (result.outcome, result.detail) == ("failed", "CLONE_LOCK_BUSY")
        assert not (state / "claude.argv").exists()
    finally:
        os.close(holder)
    assert ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path)).outcome == "launched"


def test_blocking_first_run_dialog_is_failed_not_polled(install_fake, origin, state, tmp_path):
    install_fake("claude", FAKE_CLOUD_MODE="dialog")
    result = ch.ClaudeBackend(launch_timeout_s=1.0).launch(launch_ctx(origin, tmp_path))
    assert result.outcome == "failed" and result.detail.startswith("CLI_EXIT BLOCKED_DIALOG")
    assert (state / "claude.stdin").read_bytes() == b""
    assert_group_gone(state)


def test_url_then_client_lingers_is_launched_then_hung_up(install_fake, origin, state, tmp_path):
    install_fake("claude", FAKE_CLOUD_MODE="url-linger")
    started = time.monotonic()
    result = ch.ClaudeBackend(launch_timeout_s=30.0, pty_grace_s=0.5).launch(launch_ctx(origin, tmp_path))
    assert time.monotonic() - started < 25
    assert result.outcome == "launched" and result.note == ""
    assert result.remote_id == "session_01FAKEabcdef12345"
    assert result.url == "https://claude.ai/code/session_01FAKEabcdef12345"
    assert_group_gone(state)


def test_grace_period_keeps_capturing_output_after_the_url(install_fake, origin, tmp_path):
    install_fake("claude", FAKE_CLOUD_MODE="ok-with-url", FAKE_CLOUD_EXTRA_TEXT="trailing line after url")
    ctx = launch_ctx(origin, tmp_path)
    assert ch.ClaudeBackend(pty_grace_s=5.0).launch(ctx).outcome == "launched"
    assert b"trailing line after url" in ctx.log_path.read_bytes()


def test_transcript_has_no_ansi_and_no_secret_shaped_content(install_fake, origin, tmp_path):
    secret = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
    install_fake("claude", FAKE_CLOUD_ANSI="1", FAKE_CLOUD_EXTRA_TEXT="token " + secret)
    ctx = launch_ctx(origin, tmp_path, sanitize=lambda raw: raw.replace(secret.encode(), b"[REDACTED]"))
    result = ch.ClaudeBackend().launch(ctx)
    logged = ctx.log_path.read_bytes()
    assert result.outcome == "launched"
    assert b"\x1b" not in logged and b"\r" not in logged
    assert secret.encode() not in logged and b"[REDACTED]" in logged
    assert stat.S_IMODE(ctx.log_path.stat().st_mode) == 0o600


def test_transcript_is_written_even_when_the_launch_raises(install_fake, origin, state, tmp_path, monkeypatch):
    install_fake("claude", FAKE_CLOUD_MODE="url-linger")

    def exploding_done(_text):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(ch, "_session_seen", exploding_done)
    ctx = launch_ctx(origin, tmp_path)
    with pytest.raises(RuntimeError):
        ch.ClaudeBackend().launch(ctx)
    assert b"claude.ai/code" in ctx.log_path.read_bytes()
    assert_group_gone(state)


def test_killing_the_launching_process_hangs_the_client_up(install_fake, origin, state, tmp_path):
    """SIGKILL of the orchestrator closes the pty master; the kernel hangs the client up (no orphan)."""
    wrapper = install_fake("claude", FAKE_CLOUD_MODE="hang")
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "import cloud_host as ch\n"
        "ch.run_pty([%r, '--cloud', 'x'], cwd=None, env=ch.child_env(), timeout=300, sink=bytearray())\n"
        % (str(Path(ch.__file__).resolve().parent), str(wrapper))
    )
    # like an orchestrator started under nohup: SIGHUP is ignored and inherited by every child unless reset
    launcher = subprocess.Popen(
        [sys.executable, "-c", script], preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN))
    deadline = time.monotonic() + 10
    while not (state / "claude.pid").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    pgid = int((state / "claude.pid").read_text())
    assert group_alive(pgid)
    launcher.kill()
    launcher.wait()
    try:
        gone_by = time.monotonic() + 10
        while group_alive(pgid) and time.monotonic() < gone_by:
            time.sleep(0.05)
        members = subprocess.run(["ps", "-axo", "pid,ppid,pgid,stat,command"], capture_output=True, text=True).stdout
        rows = [r for r in members.splitlines() if str(pgid) in r.split()[:3]]
        assert not group_alive(pgid), "the hang-up must end the client without the launcher: %r" % rows
    finally:
        if group_alive(pgid):
            os.killpg(pgid, 9)


def test_scan_window_keeps_working_after_the_transcript_cap(tmp_path):
    script = tmp_path / "noisy"
    script.write_text("#!/bin/bash\nhead -c 6000 /dev/zero | tr '\\0' 'x'\necho\necho https://claude.ai/code/session_01LATEcap99\nsleep 30\n")
    script.chmod(0o755)
    sink = bytearray()
    result = ch.run_pty([str(script)], cwd=None, env=ch.child_env(), timeout=20, sink=sink, cap=100, done=ch._session_seen)
    assert result.matched and result.truncated and len(sink) == 100


def test_osc8_hyperlink_target_is_kept_for_the_scan():
    raw = b"\x1b]8;;https://claude.ai/code/session_01OSCtarget1\x07open it\x1b]8;;\x07"
    assert ch.parse_claude_pty_output(ch.strip_terminal(raw).decode()) == (
        "session_01OSCtarget1", "https://claude.ai/code/session_01OSCtarget1")


def test_pty_session_id_needs_a_digit():
    assert ch.parse_claude_pty_output("tokens: session_duration and session_started") == (None, None)


def test_a_real_handoff_packet_never_looks_like_a_session_or_a_dialog():
    import handoff_packet as hp

    task = {"id": "t1", "title": "Add endpoint", "prompt": "Implement the /ping endpoint.",
            "owns": ["src/a/**"], "est_minutes": 45, "needs_secrets": [], "verify_cmd": "npm test"}
    config = {"schema": 1, "verify": {"cmd": "make test"}, "deadline_hours": 12,
              "secrets": {"provider": "none", "wrapper": "", "exposure": {"claude": [], "codex": []}}}
    prompt = hp.build_packet(task, run="run1", generation=1, nonce=NONCE, base_sha="a" * 40, provider="claude",
                             config=config, result_branch="claude/ffs-run1-t1-abcd1234")
    assert not ch._session_seen(prompt)
    assert not ch._blocking_dialog(prompt)
    assert not ch._blocking_dialog("one stray phrase: yes, proceed")
    assert ch._blocking_dialog("Quick safety check: ... Yes, I trust this folder")


def test_strip_terminal_removes_escapes_and_controls():
    raw = b"\x1b]0;title\x07\x1b[1;32mgreen\x1b[0m\x1b(B\r\nline\x00\x08two\x1bPdcs\x1b\\"
    assert ch.strip_terminal(raw) == b"green\nlinetwo"


def test_strip_terminal_turns_column_moves_into_spaces():
    assert ch.strip_terminal(b"\x1b[2GQuick\x1b[8Gsafety\x1b[15Gcheck:\r\r\nnext") == b" Quick safety check:\nnext"


def test_strip_terminal_separates_redrawn_frames_so_an_id_is_not_glued_to_the_next_line():
    raw = b"https://claude.ai/code/session_01ABCdef2345\x1b[1A\x1b[2KNext frame"
    assert ch.parse_claude_pty_output(ch.strip_terminal(raw).decode()) == (
        "session_01ABCdef2345", "https://claude.ai/code/session_01ABCdef2345")
    for seq in (b"\x1b[1A", b"\x1b[2F", b"\x1b[2K", b"\x1b[K", b"\x1b[2J", b"\x1b[3;1H", b"\x1b[5d"):
        assert ch.strip_terminal(b"session_01ABCdef2345" + seq + b"Next") == b"session_01ABCdef2345\nNext", seq


def test_cap_head_tail_keeps_both_ends_within_the_cap():
    data = b"HEAD" + b"." * 500 + b"TAIL"
    out = ch.cap_head_tail(data, cap=200, head=20)
    assert len(out) == 200 and out.startswith(b"HEAD") and out.endswith(b"TAIL")
    assert b"bytes of terminal output omitted" in out
    assert ch.cap_head_tail(b"short", cap=200, head=20) == b"short"


def test_join_head_tail_marks_a_gap_and_does_not_duplicate_an_overlap():
    tail = ch.TailWindow(b"cdefgh")
    tail.total = 8
    assert ch.join_head_tail(b"abcd", tail) == b"abcdefgh"  # overlap: nothing lost
    tail.total = 30
    out = ch.join_head_tail(b"abcd", tail)
    assert out.startswith(b"abcd") and out.endswith(b"cdefgh") and b"000000000020 bytes" in out


def test_a_late_url_survives_the_64kb_log_cap(install_fake, origin, tmp_path):
    install_fake("claude", FAKE_CLOUD_MODE="ok-with-url", FAKE_CLOUD_PRE_BYTES=str(300 * 1024))
    ctx = launch_ctx(origin, tmp_path)
    result = ch.ClaudeBackend().launch(ctx)
    logged = ctx.log_path.read_bytes()
    assert result.outcome == "launched" and result.remote_id == "session_01FAKEabcdef12345"
    assert len(logged) == ch.LOG_CAP_BYTES
    assert b"session_01FAKEabcdef12345" in logged and b"bytes of terminal output omitted" in logged
    assert logged.startswith(b"xxxx")


def test_a_late_url_survives_the_in_memory_capture_cap(install_fake, origin, tmp_path, monkeypatch):
    monkeypatch.setattr(ch, "CAPTURE_CAP_BYTES", 2000)
    monkeypatch.setattr(ch, "PTY_SCAN_WINDOW_BYTES", 4000)
    install_fake("claude", FAKE_CLOUD_MODE="ok-with-url", FAKE_CLOUD_PRE_BYTES="40000")
    ctx = launch_ctx(origin, tmp_path)
    result = ch.ClaudeBackend().launch(ctx)
    assert (result.outcome, result.remote_id, result.url) == (
        "launched", "session_01FAKEabcdef12345", "https://claude.ai/code/session_01FAKEabcdef12345")
    assert b"bytes of terminal output omitted" in ctx.log_path.read_bytes()


def test_pty_url_parser_ignores_other_claude_links():
    assert ch.parse_claude_pty_output("see https://claude.ai/login for help") == (None, None)
    rid, url = ch.parse_claude_pty_output("open https://claude.ai/code/session_01ABCdef2345 now")
    assert (rid, url) == ("session_01ABCdef2345", "https://claude.ai/code/session_01ABCdef2345")


def test_probe_reports_a_dialog_without_sending_keys(install_fake, state, tmp_path):
    install_fake("claude", FAKE_CLOUD_MODE="dialog")
    report = ch.probe_claude_first_screen(tmp_path, timeout=1.5)
    assert report["started"] and report["dialog"]
    assert "do you trust" in report["markers"] and "yes, proceed" in report["markers"]
    assert b"--cloud" not in (state / "claude.argv").read_bytes()
    assert recorded_argv(state, "claude.argv") == [""]  # no arguments at all: no prompt, no --cloud
    assert (state / "claude.stdin").read_bytes() == b""
    assert Path((state / "claude.cwd").read_text().strip()) == tmp_path.resolve()
    assert_group_gone(state)


def test_probe_without_binary(tmp_path):
    assert ch.probe_claude_first_screen(tmp_path, timeout=1.0) == {"started": False, "error": "BIN_NOT_FOUND: claude"}


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


def test_setup_link_is_not_a_session_url():
    """The client prints its environment setup link when it could not create a session; that is not a launch."""
    text = "Could not create a cloud environment. Set one up at https://claude.ai/code/onboarding?magic=env-setup\n"
    assert ch.CLAUDE_CODE_URL_RE.search(text) is None
    assert ch.CLAUDE_CODE_URL_RE.search("https://claude.ai/code/session_01AbCdEfGh2345") is not None


def test_live_launch_transcript_parses():
    """Scrubbed transcript of a real `claude --cloud` launch on a pty (Claude Code 2.1.329)."""
    text = (Path(__file__).parent / "fixtures" / "cloud" / "live" / "claude-launch-transcript.txt").read_text()
    remote_id, url = ch.parse_claude_pty_output(text)
    assert remote_id == "session_01LiveFixture0000000000AA"
    assert url is not None and url.startswith("https://claude.ai/code/session_01LiveFixture0000000000AA")


# ── Fixed-clone safety: the child path, the disposable-clone marker, checkout hardening ──────────────────


def _make_checkout(origin, where):
    git("clone", "-q", origin.url, where)
    (where / "dirty.txt").write_text("uncommitted work")
    return where


def test_unmarked_existing_clone_is_refused_and_left_alone(install_fake, origin, state, tmp_path, clone_parent):
    existing = _make_checkout(origin, clone_parent / "ffs-cloud-launch-claude")
    install_fake("claude")
    result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path, repo_root=origin.work))
    assert (result.outcome, result.detail) == ("failed", "CLONE_PATH_INVALID")
    assert (existing / "dirty.txt").read_text() == "uncommitted work" and (existing / ".git").is_dir()
    assert not (state / "claude.argv").exists()


def test_clone_child_that_is_the_operators_checkout_is_never_deleted(install_fake, origin, state, tmp_path, clone_parent):
    checkout = _make_checkout(origin, clone_parent / "ffs-cloud-launch-claude")
    install_fake("claude")
    result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path, repo_root=checkout))
    assert (result.outcome, result.detail) == ("failed", "CLONE_PATH_INVALID")
    assert (checkout / "dirty.txt").exists() and (checkout / ".git").is_dir()


def test_clone_child_that_is_a_linked_worktree_is_never_deleted(install_fake, origin, state, tmp_path, clone_parent):
    child = clone_parent / "ffs-cloud-launch-claude"
    git("worktree", "add", "-q", "-b", "wt-branch", child, "main", cwd=origin.work)
    (child / "dirty.txt").write_text("uncommitted work")
    (child / ".git").unlink()  # the worst case: even the marker location is not a directory
    (child / ".git").write_text("gitdir: %s\n" % (origin.work / ".git" / "worktrees" / "ffs-cloud-launch-claude"))
    install_fake("claude")
    result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path, repo_root=origin.work))
    assert (result.outcome, result.detail) == ("failed", "CLONE_PATH_INVALID")
    assert (child / "dirty.txt").exists()


def test_worktree_inside_the_clone_child_is_refused(install_fake, origin, state, tmp_path, clone_parent):
    child = clone_parent / "ffs-cloud-launch-claude"
    child.mkdir()
    (child / ".git").mkdir()
    _mark_owned(child)  # even a marked child may not hold a worktree
    inner = child / "nested-checkout"
    git("worktree", "add", "-q", "-b", "wt-inner", inner, "main", cwd=origin.work)
    (inner / "dirty.txt").write_text("uncommitted work")
    install_fake("claude")
    result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path, repo_root=origin.work))
    assert (result.outcome, result.detail) == ("failed", "CLONE_PATH_INVALID")
    assert (inner / "dirty.txt").exists()


def test_path_overlap_is_case_insensitive_in_both_directions():
    folded = [ch._fold_path("/Work/Repo")]
    assert ch._overlaps_any(ch._fold_path("/work/repo/ffs-cloud-launch-claude"), folded)  # child inside a worktree
    assert ch._overlaps_any(ch._fold_path("/WORK"), folded)  # a worktree inside the child
    assert ch._overlaps_any(ch._fold_path("/WORK/REPO"), folded)
    assert not ch._overlaps_any(ch._fold_path("/work/repository"), folded)


def test_parent_inside_the_repo_is_refused_case_insensitively(tmp_path):
    parent = tmp_path / "Inner"
    parent.mkdir()
    assert ch._validated_clone_parent(str(parent), Path(str(tmp_path).upper()))[1] == "INSIDE_REPO"


def test_launch_claims_the_clone_dir_with_an_ownership_marker_beside_it(tmp_path):
    clone = tmp_path / "c"
    assert ch._claim_clone(clone) == "" and clone.is_dir()
    marker = ch._clone_marker_path(clone)
    assert marker.parent == clone.parent and marker.is_file() and not marker.is_symlink()
    assert ch._claim_clone(clone) == "CLONE_PATH_INVALID", "an existing directory is never claimed"
    assert ch._clear_stale_clone(clone) == "" and not clone.exists() and not marker.exists()


def test_a_marker_that_names_another_directory_does_not_prove_ownership(tmp_path):
    clone = tmp_path / "c"
    clone.mkdir()
    (clone / "dirty.txt").write_text("operator work")
    ch._clone_marker_path(clone).write_text("1:1\n")  # a stale marker from a directory that is gone
    assert ch._clear_stale_clone(clone) == "CLONE_PATH_INVALID"
    assert (clone / "dirty.txt").exists()


def _mark_owned(clone):
    info = os.lstat(str(clone))
    ch._clone_marker_path(clone).write_text("%d:%d\n" % (info.st_dev, info.st_ino))


def _hostile_home(tmp_path, monkeypatch):
    """A global git config with a checkout hook and a smudge filter: neither may ever run in the launch clone."""
    home = tmp_path / "home"
    hooks = tmp_path / "hooks"
    hooks.mkdir(parents=True)
    home.mkdir()
    ran = tmp_path / "ran.txt"
    hook = hooks / "post-checkout"
    hook.write_text("#!/bin/sh\necho hook >> %s\n" % shlex.quote(str(ran)))
    hook.chmod(0o755)
    (home / ".gitconfig").write_text(
        "[core]\n\thooksPath = %s\n[filter \"tracker\"]\n\tsmudge = sh -c 'echo filter >> %s; cat'\n"
        % (hooks, shlex.quote(str(ran)).strip("'")))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return ran


def test_launch_clone_never_runs_hooks_or_filters_and_checks_the_oid_before_any_checkout(
        install_fake, origin, state, tmp_path, monkeypatch):
    git("checkout", "-q", "-B", origin.branch, origin.oid, cwd=origin.work)
    (origin.work / ".gitattributes").write_text("*.txt filter=tracker\n")
    git("add", ".gitattributes", cwd=origin.work)
    git("commit", "-m", "attrs", cwd=origin.work)
    moved = git("rev-parse", "HEAD", cwd=origin.work)
    git("push", "origin", "HEAD:refs/heads/" + origin.branch, cwd=origin.work)
    ran = _hostile_home(tmp_path, monkeypatch)
    install_fake("claude")
    # the pinned OID is the old tip: the branch moved after preflight, so nothing may be checked out
    result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path, task_branch_oid=origin.oid))
    assert result.detail == "BRANCH_OID_MISMATCH" and not ran.exists()
    # the pinned OID is the new tip: it is checked out, still without running the hook or the filter
    result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path, task_branch_oid=moved))
    assert result.outcome == "launched" and not ran.exists()
    assert (state / "claude.head").read_text().strip() == moved


# ── Remote ref names that match the secret scanner ──────────────────────────


def _secret_ref(origin):
    import cloud_scan
    name = "claude/" + cloud_scan.canary("github-token").decode("ascii")
    return name, origin.push_ref(name)


def test_secret_shaped_ref_names_are_dropped_from_the_parse(origin):
    name, oid = _secret_ref(origin)
    listing = subprocess.run(["git", "ls-remote", origin.url, "refs/heads/claude/*"], capture_output=True, text=True,
                             check=True).stdout
    assert name in listing
    clean, flagged = ch.split_ls_remote(listing)
    assert "refs/heads/" + name not in clean and "refs/heads/claude/old-1" in clean
    assert list(flagged) == [ch.redacted_ref_key("refs/heads/" + name)]
    assert name not in json.dumps(list(flagged)) and ch.parse_ls_remote(listing) == clean


def test_launch_snapshot_stores_a_secret_shaped_ref_by_hash_only(install_fake, origin, state, tmp_path):
    name, oid = _secret_ref(origin)
    install_fake("claude")
    ctx = launch_ctx(origin, tmp_path)
    assert ch.ClaudeBackend().launch(ctx).outcome == "launched"
    text = Path(ctx.refs_snapshot_path).read_text()
    assert name not in text and ch.redacted_ref_key("refs/heads/" + name) in text


def test_poll_reports_a_new_secret_shaped_ref_with_a_fixed_note_and_no_name(install_fake, origin, state, tmp_path):
    install_fake("claude")
    existing, _ = _secret_ref(origin)  # present at launch: not new, not reported
    assert ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path)).outcome == "launched"
    quiet = ch.ClaudeBackend().poll(poll_ctx(origin, tmp_path))
    assert ch.REF_NAME_SECRET_NOTE not in quiet.detail
    import cloud_scan
    fresh = "claude/x-" + cloud_scan.canary("github-token").decode("ascii")
    origin.push_ref(fresh)
    result = ch.ClaudeBackend().poll(poll_ctx(origin, tmp_path))
    assert ch.REF_NAME_SECRET_NOTE in result.detail.split()
    assert result.candidates == [] and fresh not in repr(result) and existing not in repr(result)


def test_a_dir_that_appears_before_the_clone_is_not_deleted_without_the_marker(
        install_fake, origin, state, tmp_path, clone_parent, monkeypatch):
    """WR-01: the exit-path delete checks the disposable marker too; an unmarked directory is never removed."""
    install_fake("claude")
    real = ch._clear_stale_clone

    def racing(clone):
        problem = real(clone)  # absent here ...
        clone.mkdir()  # ... and then a directory appears before `git clone` creates it
        (clone / "dirty.txt").write_text("uncommitted work")
        return problem

    monkeypatch.setattr(ch, "_clear_stale_clone", racing)
    result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path))
    assert result.outcome == "failed"
    assert (clone_parent / "ffs-cloud-launch-claude" / "dirty.txt").read_text() == "uncommitted work"


def _scanner_down(monkeypatch):
    import cloud_scan

    def refuse(*_a, **_k):
        raise cloud_scan.ScanRefused("SCAN_INTERNAL")

    monkeypatch.setattr(cloud_scan, "scan_bytes", refuse)


def test_a_scanner_failure_fails_the_launch_snapshot_instead_of_hashing_every_ref(
        install_fake, origin, state, tmp_path, monkeypatch):
    """WR-02: a refused scan is not 'every ref name is secret': nothing is launched and no snapshot is written."""
    install_fake("claude")
    _scanner_down(monkeypatch)
    ctx = launch_ctx(origin, tmp_path)
    result = ch.ClaudeBackend().launch(ctx)
    assert (result.outcome, result.detail) == ("failed", "SNAPSHOT_FAILED")
    assert not Path(ctx.refs_snapshot_path).exists() and not (state / "claude.argv").exists()


def test_a_scanner_failure_in_poll_is_unknown_not_a_secret_ref_name(install_fake, origin, state, tmp_path, monkeypatch):
    install_fake("claude")
    assert ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path)).outcome == "launched"
    with monkeypatch.context() as patch:
        _scanner_down(patch)
        result = ch.ClaudeBackend().poll(poll_ctx(origin, tmp_path))
    assert (result.state, result.candidates) == ("unknown", []) and "REF_SCAN_REFUSED" in result.detail
    assert ch.REF_NAME_SECRET_NOTE not in result.detail
    assert ch.ClaudeBackend().poll(poll_ctx(origin, tmp_path)).state == "running"  # the next tick recovers


def test_a_clone_killed_at_the_timeout_does_not_wedge_the_next_fixed_clone_launch(
        install_fake, origin, state, tmp_path, clone_parent, monkeypatch):
    """WR-A: SIGKILL leaves a partial clone; the dispatcher made the directory, so it can remove it."""
    install_fake("claude")
    real = ch.run_git
    calls = []

    def killed_clone(args, **kwargs):
        if "clone" in args:
            calls.append(1)
            (Path(args[-1]) / ".git" / "objects").mkdir(parents=True)  # what a killed git leaves behind
            return real(["not-a-git-command"], timeout=5)
        return real(args, **kwargs)

    monkeypatch.setattr(ch, "run_git", killed_clone)
    assert ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path)).detail == "CLONE_FAILED"
    monkeypatch.setattr(ch, "run_git", real)
    assert ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path)).outcome == "launched" and calls


def test_a_clone_dir_left_by_a_killed_dispatcher_is_removed_by_the_next_launch(
        install_fake, origin, state, tmp_path, clone_parent):
    clone = clone_parent / "ffs-cloud-launch-claude"
    assert ch._claim_clone(clone) == ""
    (clone / ".git" / "objects").mkdir(parents=True)  # the dispatcher died mid-clone: no exit path ran
    install_fake("claude")
    assert ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path)).outcome == "launched"
    assert not clone.exists() and not ch._clone_marker_path(clone).exists()


def test_an_unmarked_clone_dir_is_refused_and_never_deleted(install_fake, origin, state, tmp_path, clone_parent):
    clone = clone_parent / "ffs-cloud-launch-claude"
    (clone / ".git").mkdir(parents=True)
    (clone / ".git" / "ffs-disposable-clone").write_text("the old in-tree marker no longer proves anything\n")
    (clone / "dirty.txt").write_text("operator work")
    install_fake("claude")
    result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path))
    assert (result.outcome, result.detail) == ("failed", "CLONE_PATH_INVALID")
    assert (clone / "dirty.txt").exists()


# ── Marker file: never truncated, never unlinked unless it is the dispatcher's own ──


def _launch_refused(origin, tmp_path):
    result = ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path))
    assert result.outcome == "failed" and result.detail.startswith("CLONE_PATH_INVALID ")
    assert "MARKER" in result.detail  # the refusal carries a reason
    return result


def test_an_unrelated_file_at_the_marker_name_is_refused_and_kept(install_fake, origin, state, tmp_path, clone_parent):
    install_fake("claude")
    marker = ch._clone_marker_path(clone_parent / "ffs-cloud-launch-claude")
    marker.write_text("operator notes\n")
    _launch_refused(origin, tmp_path)
    assert marker.read_text() == "operator notes\n"
    assert not (clone_parent / "ffs-cloud-launch-claude").exists()


def test_a_hard_link_at_the_marker_name_is_refused_and_its_target_is_untouched(
        install_fake, origin, state, tmp_path, clone_parent):
    install_fake("claude")
    keep = tmp_path / "operator-file"
    keep.write_text("precious\n")
    marker = ch._clone_marker_path(clone_parent / "ffs-cloud-launch-claude")
    os.link(str(keep), str(marker))
    _launch_refused(origin, tmp_path)
    assert keep.read_text() == "precious\n" and marker.exists() and os.stat(str(keep)).st_nlink == 2


def test_a_symlink_or_fifo_at_the_marker_name_is_refused_and_kept(install_fake, origin, state, tmp_path, clone_parent):
    install_fake("claude")
    marker = ch._clone_marker_path(clone_parent / "ffs-cloud-launch-claude")
    target = tmp_path / "elsewhere"
    target.write_text("keep\n")
    marker.symlink_to(target)
    _launch_refused(origin, tmp_path)
    assert marker.is_symlink() and target.read_text() == "keep\n"
    marker.unlink()
    os.mkfifo(str(marker))  # must not hang the launch while it holds the flock
    _launch_refused(origin, tmp_path)
    assert stat.S_ISFIFO(os.lstat(str(marker)).st_mode)


def test_claiming_never_truncates_a_file_that_appears_at_the_marker_name(tmp_path):
    clone = tmp_path / "c"
    keep = tmp_path / "operator-file"
    keep.write_text("precious\n")
    os.link(str(keep), str(ch._clone_marker_path(clone)))
    assert ch._claim_clone(clone) != ""
    assert keep.read_text() == "precious\n" and not clone.exists()


def test_a_valid_orphan_marker_with_no_clone_is_dropped(install_fake, origin, state, tmp_path, clone_parent):
    install_fake("claude")
    marker = ch._clone_marker_path(clone_parent / "ffs-cloud-launch-claude")
    marker.write_text("1:1\n")  # a dispatcher marker whose directory is gone
    assert ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path)).outcome == "launched"
    assert not marker.exists()


# ── Race-free creation and retirement of the fixed clone ────────────────────


def _plant_foreign_dir(path, text="operator work"):
    """What a rename between the dispatcher's check and its use looks like: a foreign directory takes the name."""
    if os.path.lexists(str(path)):
        os.rename(str(path), str(path) + "-moved-aside")
    os.mkdir(str(path))
    (path / "dirty.txt").write_text(text)


def _staging_dirs(parent):
    return [p.name for p in parent.iterdir() if p.name.startswith(".ffs-cloud-") and not p.name.endswith(".lock")]


def test_a_directory_swapped_in_after_the_ownership_check_is_not_deleted(tmp_path, monkeypatch):
    clone = tmp_path / "c"
    assert ch._claim_clone(clone) == ""
    real = ch._owns_clone

    def swapped(path):
        verdict = real(path)
        if verdict:
            _plant_foreign_dir(path)  # between the check and the delete
        return verdict

    monkeypatch.setattr(ch, "_owns_clone", swapped)
    ch._release_clone(clone)
    assert (clone / "dirty.txt").read_text() == "operator work", "the foreign directory is put back untouched"
    assert _staging_dirs(tmp_path) == []
    assert (tmp_path / "c-moved-aside").is_dir(), "the directory the dispatcher made is not the one that was removed"


def test_a_mismatched_retired_directory_is_left_in_staging_when_its_name_is_taken(tmp_path, monkeypatch):
    clone = tmp_path / "c"
    assert ch._claim_clone(clone) == ""
    real_owns, real_lstat = ch._owns_clone, os.lstat

    def swapped(path):
        verdict = real_owns(path)
        if verdict:
            _plant_foreign_dir(path, "first")
        return verdict

    def lstat(path, *args, **kwargs):
        if os.sep + "retired" in str(path) and not os.path.lexists(str(clone)):
            os.mkdir(str(clone))  # the name is taken again before the retired directory can be put back
            (clone / "second.txt").write_text("second")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(ch, "_owns_clone", swapped)
    monkeypatch.setattr(os, "lstat", lstat)
    reason = ch._release_clone(clone)
    monkeypatch.undo()
    assert reason.startswith("CLONE_PATH_INVALID ")
    assert (clone / "second.txt").read_text() == "second"
    kept = [p for p in tmp_path.glob(".ffs-cloud-*/retired/dirty.txt")]
    assert len(kept) == 1 and kept[0].read_text() == "first", "never deleted, left in private staging"


def test_a_directory_that_takes_the_name_before_publication_is_not_marked_or_replaced(tmp_path, monkeypatch):
    clone = tmp_path / "c"
    real_lstat, planted = os.lstat, []

    def lstat(path, *args, **kwargs):
        if not planted:
            planted.append(1)
            if os.path.lexists(str(clone)):
                os.rename(str(clone), str(clone) + "-moved-aside")
            os.mkdir(str(clone))  # an EMPTY foreign directory: a plain rename would replace it
            (tmp_path / "unrelated").write_text("x")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)
    reason = ch._claim_clone(clone)
    monkeypatch.undo()
    assert reason.startswith("CLONE_PATH_INVALID") and clone.is_dir()
    assert not ch._owns_clone(clone) and not ch._clone_marker_path(clone).exists()
    assert _staging_dirs(tmp_path) == []


def test_rename_noreplace_never_replaces_an_existing_target_even_an_empty_directory(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    with pytest.raises(FileExistsError):
        ch._rename_noreplace(str(src), str(dst))
    assert src.is_dir() and dst.is_dir()
    ch._rename_noreplace(str(src), str(tmp_path / "free"))
    assert not src.exists() and (tmp_path / "free").is_dir()


def test_a_launch_leaves_no_staging_directory_behind(install_fake, origin, state, tmp_path, clone_parent):
    install_fake("claude")
    assert ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path)).outcome == "launched"
    assert _staging_dirs(clone_parent) == []


# ── Leftovers of a killed launch are recovered only when provably the dispatcher's own ──


def _leftover(parent, prefix):
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))
    os.chmod(str(path), 0o700)
    return path


def test_a_killed_claim_before_publication_is_recovered_by_the_next_launch(
        install_fake, origin, state, tmp_path, clone_parent):
    staging = _leftover(clone_parent, ch.STAGE_NEW_PREFIX)
    (staging / "new").mkdir()
    info = os.lstat(str(staging / "new"))
    ch._create_marker(ch._clone_marker_path(clone_parent / "ffs-cloud-launch-claude"), info.st_dev, info.st_ino)
    install_fake("claude")
    assert ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path)).outcome == "launched"
    assert _staging_dirs(clone_parent) == [] and not (clone_parent / "ffs-cloud-launch-claude.ffs-owned").exists()


def test_a_staging_directory_that_holds_anything_is_left_alone_and_the_launch_continues(
        install_fake, origin, state, tmp_path, clone_parent):
    staging = _leftover(clone_parent, ch.STAGE_NEW_PREFIX)  # right prefix, mode and owner, but not empty
    (staging / "new").mkdir()
    (staging / "new" / "keep").write_text("a")
    (clone_parent / (staging.name + "x")).mkdir(mode=0o700)  # an empty one beside it is still removed
    other = _leftover(clone_parent, ch.STAGE_NEW_PREFIX)
    (other / "stray").write_text("b")
    info = os.lstat(str(staging / "new"))
    ch._create_marker(ch._clone_marker_path(clone_parent / "ffs-cloud-launch-claude"), info.st_dev, info.st_ino)
    install_fake("claude")
    assert ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path)).outcome == "launched"
    assert (staging / "new" / "keep").read_text() == "a" and (other / "stray").read_text() == "b"
    assert not (clone_parent / (staging.name + "x")).exists()


def test_a_kill_after_retiring_the_clone_is_recovered_when_the_marker_names_it(
        install_fake, origin, state, tmp_path, clone_parent):
    clone = clone_parent / "ffs-cloud-launch-claude"
    assert ch._claim_clone(clone) == ""
    (clone / "partial").write_text("x")
    staging = _leftover(clone_parent, ch.STAGE_OLD_PREFIX)
    os.rename(str(clone), str(staging / "retired"))  # killed mid-delete: the marker is still there
    install_fake("claude")
    assert ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path)).outcome == "launched"
    assert _staging_dirs(clone_parent) == [] and not ch._clone_marker_path(clone).exists()


def test_a_kill_after_retiring_the_marker_is_recovered(install_fake, origin, state, tmp_path, clone_parent):
    clone = clone_parent / "ffs-cloud-launch-claude"
    assert ch._claim_clone(clone) == ""
    shutil.rmtree(str(clone))
    staging = _leftover(clone_parent, ch.STAGE_OLD_PREFIX)
    os.rename(str(ch._clone_marker_path(clone)), str(staging / "marker"))
    install_fake("claude")
    assert ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path)).outcome == "launched"
    assert _staging_dirs(clone_parent) == []


def test_staging_leftovers_that_are_not_provably_the_dispatchers_are_never_deleted(
        install_fake, origin, state, tmp_path, clone_parent):
    loose = _leftover(clone_parent, ch.STAGE_NEW_PREFIX)
    (loose / "new").mkdir()
    (loose / "new" / "keep").write_text("a")
    os.chmod(str(loose), 0o500)  # right prefix, wrong mode (anything but 0o700 is refused)
    foreign = _leftover(clone_parent, ch.STAGE_OLD_PREFIX)  # a retired directory no marker names
    (foreign / "retired").mkdir()
    (foreign / "retired" / "keep").write_text("b")
    elsewhere = tmp_path / "operator-dir"
    elsewhere.mkdir()
    (elsewhere / "keep").write_text("c")
    (clone_parent / (ch.STAGE_NEW_PREFIX + "link")).symlink_to(elsewhere)
    install_fake("claude")
    assert ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path)).outcome == "launched"
    assert (loose / "new" / "keep").read_text() == "a" and (foreign / "retired" / "keep").read_text() == "b"
    assert (elsewhere / "keep").read_text() == "c"


def test_staging_leftovers_that_group_or_others_can_reach_are_never_deleted(
        install_fake, origin, state, tmp_path, clone_parent):
    """Only an exact 0o700 directory can be the dispatcher's.  The mode is built from stat bits (owner rwx plus group and
    other read/execute) so the fixture states what it means without a permissive-looking literal.  One leftover is
    empty inside (a removed mode check would delete it) and one holds a file (a mode check masked down to the owner bits,
    combined with a recursive delete, would delete it): both must survive, and the 0o500 case above cannot show either."""
    reachable = stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
    assert reachable & (stat.S_IRWXG | stat.S_IRWXO) and stat.S_IMODE(reachable) != stat.S_IRWXU
    empty_inside = _leftover(clone_parent, ch.STAGE_NEW_PREFIX)
    (empty_inside / "new").mkdir()
    with_file = _leftover(clone_parent, ch.STAGE_NEW_PREFIX)
    (with_file / "new").mkdir()
    (with_file / "new" / "keep").write_text("a")
    for path in (empty_inside, with_file):
        os.chmod(str(path), reachable)
    install_fake("claude")
    assert ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path)).outcome == "launched"
    assert (empty_inside / "new").is_dir()
    assert (with_file / "new" / "keep").read_text() == "a"
    assert stat.S_IMODE(os.lstat(str(empty_inside)).st_mode) == reachable


# ── The ownership marker is written atomically; an empty leftover never wedges the fixed path ──


def test_a_failed_marker_write_leaves_neither_a_marker_nor_a_temp_file(tmp_path, monkeypatch):
    clone = tmp_path / "c"

    def full_disk(fd, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "write", full_disk)
    with pytest.raises(OSError):
        ch._create_marker(ch._clone_marker_path(clone), 1, 2)
    monkeypatch.undo()
    assert not ch._clone_marker_path(clone).exists() and _staging_dirs(tmp_path) == []


def test_an_empty_marker_left_by_a_killed_write_is_recovered_by_the_next_launch(
        install_fake, origin, state, tmp_path, clone_parent):
    marker = ch._clone_marker_path(clone_parent / "ffs-cloud-launch-claude")
    marker.write_bytes(b"")
    marker.chmod(0o600)
    fd, name = tempfile.mkstemp(prefix=ch.STAGE_NEW_PREFIX, dir=str(clone_parent))  # killed before the rename
    os.close(fd)
    leftover = Path(name)
    install_fake("claude")
    assert ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path)).outcome == "launched"
    assert not marker.exists() and not leftover.exists()


def test_a_non_empty_unrecognized_marker_is_refused_and_left_untouched(
        install_fake, origin, state, tmp_path, clone_parent):
    marker = ch._clone_marker_path(clone_parent / "ffs-cloud-launch-claude")
    marker.write_text("operator notes\n")
    install_fake("claude")
    assert ch.ClaudeBackend().launch(launch_ctx(origin, tmp_path)).outcome != "launched"
    assert marker.read_text() == "operator notes\n"


def test_a_libc_without_renameat2_falls_back_to_a_check_and_a_rename(tmp_path, monkeypatch):
    import ctypes

    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace())  # no renameat2 (glibc < 2.28)
    monkeypatch.setattr(os, "uname", lambda: SimpleNamespace(sysname="Linux"))
    src, taken = tmp_path / "src", tmp_path / "taken"
    src.mkdir()
    taken.mkdir()
    with pytest.raises(FileExistsError):
        ch._rename_noreplace(str(src), str(taken))
    ch._rename_noreplace(str(src), str(tmp_path / "free"))
    assert not src.exists() and (tmp_path / "free").is_dir()
