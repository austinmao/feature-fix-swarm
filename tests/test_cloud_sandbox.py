"""Tests for lib/cloud_sandbox.py. Real sandbox-exec on macOS, skipped elsewhere."""

from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import cloud_sandbox as sb  # noqa: E402
import cloud_scan  # noqa: E402

SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
NEEDS_SANDBOX = pytest.mark.skipif(
    not sb.sandbox_available(),
    reason="sandbox-exec is unavailable (non-darwin host or nested sandbox)",
)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(repo: Path, *args: str, stdin: str = "") -> str:
    env = dict(os.environ)
    env.update(GIT_ENV)
    proc = subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        input=stdin, capture_output=True, text=True, env=env, check=True,
    )
    return proc.stdout.strip()


@pytest.fixture()
def world(tmp_path: Path):
    """A fake HOME, main worktree (with deps and a .env), second worktree."""
    tmp = tmp_path.resolve()
    home = tmp / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id").write_text("ssh-secret")
    (home / "notes.txt").write_text("home-secret")
    (home / ".tool" / "v1" / "bin").mkdir(parents=True)
    (home / ".tool" / "v1" / "marker.txt").write_text("toolchain-ok")
    main = tmp / "main"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    (main / ".env").write_text("main-secret")
    (main / "node_modules" / "pkg").mkdir(parents=True)
    (main / "node_modules" / "pkg" / "dep.txt").write_text("dep-ok")
    (main / "app.py").write_text("print('hi')\n")
    (main / "run.sh").write_text("#!/bin/sh\necho ran-exec\n")
    os.chmod(main / "run.sh", 0o755)
    (main / "sub").mkdir()
    (main / "sub" / "inner.txt").write_text("inner\n")
    git(main, "add", "app.py", "run.sh", "sub/inner.txt")
    git(main, "commit", "-q", "-m", "init")
    other = tmp / "wt2"
    other.mkdir()
    (other / "file.txt").write_text("other-secret")
    return {
        "tmp": tmp, "home": home, "main": main, "other": other,
        "tree": git(main, "rev-parse", "HEAD^{tree}"),
    }


def verify(world, cmd, *, timeout_s=60, deps=("node_modules",), path_env=SYSTEM_PATH, read_roots=(), **extra):
    log = world["tmp"] / "verify.log"
    rc = sb.run_verify(
        world["tree"], cmd, repo=str(world["main"]), main_worktree=str(world["main"]),
        deps=list(deps), read_roots=list(read_roots), timeout_s=timeout_s,
        log_path=str(log), other_worktrees=[str(world["other"])],
        home=str(world["home"]), path_env=path_env, **extra,
    )
    return rc, log.read_text(errors="replace")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_dead(pid: int, seconds: float = 5.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if not alive(pid):
            return True
        time.sleep(0.1)
    return not alive(pid)


class Listener:
    """A localhost TCP listener that records what connects to it."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.requests = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            conn.settimeout(2)
            try:
                self.requests.append(conn.recv(256))
            except OSError:
                self.requests.append(b"")
            try:
                conn.sendall(b"HTTP/1.0 204 No Content\r\n\r\n")
            except OSError:
                pass
            conn.close()

    def close(self):
        self.sock.close()


@pytest.fixture()
def listener():
    srv = Listener()
    yield srv
    srv.close()


# ------------------------------------------------------------------ profile


def profile_for(tmp_path, **extra):
    args = dict(
        work_dir=str(tmp_path / "w"), tmp_dir=str(tmp_path), home=str(tmp_path / "h"),
        main_worktree=str(tmp_path / "m"), state_dir=str(tmp_path / "m" / ".feature-fix-swarm" / "cloud"),
        other_worktrees=[str(tmp_path / "o")], dep_roots=[str(tmp_path / "m" / "node_modules")],
        toolchain_roots=[str(tmp_path / "t")], read_roots=[str(tmp_path / "r")],
    )
    args.update(extra)
    return sb.build_profile(**args)


def test_profile_shape(tmp_path):
    text = profile_for(tmp_path)
    lines = text.splitlines()
    assert lines[:3] == ["(version 1)", "(allow default)", "(deny network*)"]
    for rule in ("(deny mach-lookup)", "(deny mach-register)", "(deny lsopen)", "(deny appleevent-send)",
                 "(deny iokit-open)", "(deny system-socket)", "(deny job-creation)", "(deny signal)",
                 "(deny process-info*)", "(allow signal (target same-sandbox))",
                 "(allow process-info* (target same-sandbox))"):
        assert rule in lines, rule
    assert lines.index("(deny mach-lookup)") < lines.index(
        '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))')
    assert '(deny file-write* (subpath "/"))' in lines
    first_deny = next(ln for ln in lines if ln.startswith("(deny file-read*"))
    for area in ('"/Volumes"', '"/private/tmp"', '"/private/var/folders"'):
        assert area in first_deny
    allow = next(ln for ln in lines if ln.startswith("(allow file-read* "))
    for name in ("w", "t", "r"):
        assert os.path.realpath(str(tmp_path / name)) in allow
    assert os.path.realpath(str(tmp_path / "m" / "node_modules")) in allow
    # the deny rules are emitted again AFTER every allow (last match wins)
    allow_at = max(i for i, ln in enumerate(lines) if ln.startswith("(allow file-read"))
    redeny = [ln for ln in lines[allow_at + 1:] if ln.startswith("(deny file-read*")]
    assert redeny and "o" in redeny[0] and ".ssh" in redeny[0] and ".cargo/credentials" in redeny[0]
    assert lines[-1].startswith("(deny file-read*") and "cloud" in lines[-1]


def test_profile_quotes_and_rejects_bad_paths(tmp_path):
    with pytest.raises(sb.SandboxError):
        sb.build_profile(
            work_dir=str(tmp_path / "a\nb"), tmp_dir=str(tmp_path), home=str(tmp_path),
            main_worktree=str(tmp_path), state_dir=str(tmp_path),
        )
    assert sb._quote('/a"b\\c') == '"/a\\"b\\\\c"'


@pytest.mark.parametrize("root", ["h", "m", "o", "m/.feature-fix-swarm", "h/.ssh", "h/.config/gh", "h/.cargo/credentials.toml",
                                  "h/.local/share", "h/Library/Python", ".", "h/.."])
def test_profile_refuses_roots_that_reopen_denied_areas(tmp_path, root):
    base = tmp_path.resolve()
    target = str((base / root).resolve())
    for kind in ("toolchain_roots", "read_roots", "dep_roots"):
        # a dependency root may live inside the main worktree, never contain it or a denied area
        if kind == "dep_roots" and root == "m/.feature-fix-swarm":
            pass
        with pytest.raises(sb.SandboxError):
            profile_for(base, **{kind: [target]})


def test_profile_allows_dependency_root_inside_main_but_not_toolchain(tmp_path):
    base = tmp_path.resolve()
    inside = str(base / "m" / "node_modules")
    assert inside in profile_for(base, dep_roots=[inside], toolchain_roots=[])
    with pytest.raises(sb.SandboxError):
        profile_for(base, toolchain_roots=[inside], dep_roots=[])


def test_profile_rejects_nothing_for_normal_roots(tmp_path):
    base = tmp_path.resolve()
    text = profile_for(base, toolchain_roots=[str(base / "h" / ".nvm" / "versions" / "node" / "v22")])
    assert "v22" in text


def test_rejected_rule_fails_closed(monkeypatch):
    sb._PREFLIGHT.clear()
    monkeypatch.setattr(sb, "_hardening_rules", lambda: ["(deny no-such-operation-xyz)"])
    try:
        assert sb._hardening_enforced() is False
        assert sb.sandbox_available() is False
    finally:
        sb._PREFLIGHT.clear()


@NEEDS_SANDBOX
def test_rule_that_does_not_bite_fails_closed(monkeypatch):
    """``(target others)`` is accepted by sandbox-exec but does not block kill."""
    sb._PREFLIGHT.clear()
    rules = [r for r in sb._hardening_rules() if "signal" not in r]
    monkeypatch.setattr(sb, "_hardening_rules", lambda: rules + ["(deny signal (target others))"])
    try:
        assert sb._hardening_enforced() is False
    finally:
        sb._PREFLIGHT.clear()


@NEEDS_SANDBOX
def test_profile_rejected_by_sandbox_exec_raises_unavailable(world, monkeypatch):
    monkeypatch.setattr(sb, "_run_profile_check", lambda path: 65)
    with pytest.raises(sb.SandboxUnavailable):
        verify(world, "true")


def make_home(tmp_path, rels):
    home = tmp_path.resolve() / "h"
    for rel in rels:
        (home / rel).mkdir(parents=True, exist_ok=True)
    return home


def roots_for(home, entries, **extra):
    path_env = os.pathsep.join([str(home / e) if not e.startswith("/") else e for e in entries])
    return sb.resolve_toolchain_roots(path_env, str(home), **extra)


def test_resolve_toolchain_roots_grants_only_the_bin_directory(tmp_path):
    home = make_home(tmp_path, [".cargo/bin", ".local/bin", "bin", ".local/share/pnpm", "Documents/bin"])
    roots = roots_for(home, [".cargo/bin", ".local/bin", "bin", ".local/share/pnpm", "Documents/bin"])
    assert roots == [str(home / p) for p in (".cargo/bin", ".local/bin", "bin", ".local/share/pnpm", "Documents/bin")]
    for forbidden in (home, home / ".local" / "share", home / "Documents", home / ".cargo"):
        assert str(forbidden) not in roots


def test_resolve_toolchain_roots_known_layouts(tmp_path):
    home = make_home(tmp_path, [".nvm/versions/node/v22/bin", ".pyenv/versions/3.12.1/bin", ".rustup/toolchains/stable-x/bin",
                                ".cargo/bin"])
    roots = roots_for(home, [".nvm/versions/node/v22/bin", ".pyenv/versions/3.12.1/bin", ".cargo/bin"])
    assert str(home / ".nvm/versions/node/v22") in roots
    assert str(home / ".pyenv/versions/3.12.1") in roots
    assert str(home / ".cargo/bin") in roots and str(home / ".rustup/toolchains/stable-x") in roots
    assert str(home / ".cargo") not in roots and str(home / ".nvm") not in roots


def test_resolve_toolchain_roots_follows_symlink_farm_into_known_layouts_only(tmp_path):
    home = make_home(tmp_path, [".local/bin", ".local/share/uv/tools/ruff/bin", ".local/share/uv/python/cpython-3.12/bin",
                                ".local/share/opencode", "elsewhere/bin"])
    (home / ".local/share/uv/tools/ruff/bin/ruff").write_text("x")
    (home / ".local/bin/ruff").symlink_to(home / ".local/share/uv/tools/ruff/bin/ruff")
    (home / ".local/share/uv/python/cpython-3.12/bin/python3").write_text("x")
    (home / ".local/share/uv/tools/ruff/bin/python").symlink_to(home / ".local/share/uv/python/cpython-3.12/bin/python3")
    (home / ".local/bin/auth").symlink_to(home / ".local/share/opencode")  # not a toolchain layout
    (home / "elsewhere/bin/tool").write_text("x")
    (home / ".local/bin/other").symlink_to(home / "elsewhere/bin/tool")
    roots = roots_for(home, [".local/bin"])
    assert str(home / ".local/bin") in roots
    assert str(home / ".local/share/uv/tools/ruff") in roots
    assert str(home / ".local/share/uv/python/cpython-3.12") in roots
    for bad in (home / ".local/share", home / ".local/share/opencode", home / "elsewhere", home):
        assert str(bad) not in roots


@pytest.mark.parametrize("entry", [".config/gh/bin", ".orbstack/bin", ".ssh/x/bin", ".aws/bin", ".codex/bin", ".claude/bin",
                                   ".doppler/bin", ".gnupg/bin", ".docker/bin", ".kube/bin", "Library/Python/3.9/bin"])
def test_resolve_toolchain_roots_never_grants_credential_areas(tmp_path, entry):
    home = make_home(tmp_path, [entry])
    assert roots_for(home, [entry]) == []


def test_resolve_toolchain_roots_never_grants_home_worktrees_or_state(tmp_path):
    home = make_home(tmp_path, ["Documents/main/node_modules/.bin", "Documents/wt2/bin", "Documents/main/.feature-fix-swarm/cloud/bin",
                                "Documents/bin"])
    main, other = home / "Documents/main", home / "Documents/wt2"
    state = main / ".feature-fix-swarm" / "cloud"
    entries = ["Documents/main/node_modules/.bin", "Documents/wt2/bin", "Documents/main/.feature-fix-swarm/cloud/bin",
               "Documents/bin", "Documents/main", str(home), str(home / "Documents")]
    roots = roots_for(home, entries, main_worktree=str(main), other_worktrees=[str(other)], state_dir=str(state))
    assert roots == [str(home / "Documents/bin")]


def test_resolve_toolchain_roots_symlinked_entry_pointing_home_is_refused(tmp_path):
    home = make_home(tmp_path, ["real"])
    (home / "bin").symlink_to(home)
    assert roots_for(home, ["bin"]) == []


def test_toolchain_root_never_home_even_for_direct_child(tmp_path):
    home = tmp_path.resolve() / "h"
    (home / "bin").mkdir(parents=True)
    assert sb.resolve_toolchain_roots(str(home / "bin"), str(home)) == [str(home / "bin")]
    assert sb.resolve_toolchain_roots(str(home), str(home)) == []
    assert sb.resolve_toolchain_roots("rel/bin" + os.pathsep + "", str(home)) == []


def test_filtered_path_drops_unreadable_home_entries(tmp_path):
    home = tmp_path.resolve() / "h"
    (home / ".tool/v1/bin").mkdir(parents=True)
    (home / "other/bin").mkdir(parents=True)
    path_env = os.pathsep.join(["/usr/bin", "/private/tmp", str(home / ".tool/v1/bin"), str(home / "other/bin")])
    roots = [str(home / ".tool/v1/bin")]
    assert sb._filtered_path(path_env, str(home), roots) == "/usr/bin" + os.pathsep + str(home / ".tool/v1/bin")


# ------------------------------------------------------------ materialization


def test_materialize_writes_files_and_exec_bit(world, tmp_path):
    dest = tmp_path / "out"
    count = sb.materialize_tree(str(world["main"]), world["tree"], str(dest))
    assert count == 3
    assert (dest / "app.py").read_text() == "print('hi')\n"
    assert (dest / "sub" / "inner.txt").read_text() == "inner\n"
    assert stat.S_IMODE((dest / "run.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((dest / "app.py").stat().st_mode) == 0o644


def test_materialize_ignores_gitattributes_filters(world, tmp_path):
    main = world["main"]
    (main / ".gitattributes").write_text("*.txt filter=evil text eol=crlf\n")
    git(main, "config", "filter.evil.smudge", "touch /tmp/ffs-should-not-exist; cat")
    git(main, "add", ".gitattributes")
    git(main, "commit", "-q", "-m", "attrs")
    tree = git(main, "rev-parse", "HEAD^{tree}")
    dest = tmp_path / "out"
    sb.materialize_tree(str(main), tree, str(dest))
    assert (dest / "sub" / "inner.txt").read_bytes() == b"inner\n"
    assert not os.path.exists("/tmp/ffs-should-not-exist")


def test_materialize_refuses_symlink(world, tmp_path):
    main = world["main"]
    blob = git(main, "hash-object", "-w", "--stdin", stdin="target")
    git(main, "update-index", "--add", "--cacheinfo", "120000,%s,link" % blob)
    tree = git(main, "write-tree")
    with pytest.raises(sb.SandboxError):
        sb.materialize_tree(str(main), tree, str(tmp_path / "out"))


def test_materialize_refuses_gitlink(world, tmp_path):
    main = world["main"]
    git(main, "update-index", "--add", "--cacheinfo", "160000,%s,sm" % ("1" * 40))
    tree = git(main, "write-tree")
    with pytest.raises(sb.SandboxError):
        sb.materialize_tree(str(main), tree, str(tmp_path / "out"))


@pytest.mark.parametrize("raw", [b"sub/.GIT/hook", b".git/config", b"a/../b", b"/abs", b"a//b", b"./a", b""])
def test_unsafe_tree_paths_refused(raw):
    with pytest.raises(sb.SandboxError):
        sb._safe_relpath(raw)


def test_materialize_bad_tree_fails(world, tmp_path):
    with pytest.raises(sb.SandboxError):
        sb.materialize_tree(str(world["main"]), "0" * 40, str(tmp_path / "out"))


# ------------------------------------------------------------- real sandbox


@NEEDS_SANDBOX
def test_runs_tree_and_exec_bit(world):
    rc, log = verify(world, "cat app.py && ./run.sh")
    assert rc == 0, log
    assert "print('hi')" in log and "ran-exec" in log


@NEEDS_SANDBOX
def test_fork_and_exec_work(world):
    rc, log = verify(world, "echo a | cat")
    assert rc == 0 and log.strip() == "a"
    rc, log = verify(
        world, "/usr/bin/python3 -c \"import subprocess; print(subprocess.run(['/bin/echo','fork-ok'], capture_output=True).stdout.decode().strip())\"",
    )
    assert rc == 0 and "fork-ok" in log, log


@NEEDS_SANDBOX
def test_network_denied_while_the_same_connect_works_outside(world, listener):
    # outside the sandbox the connection succeeds ...
    before = len(listener.requests)
    with socket.create_connection(("127.0.0.1", listener.port), 3) as conn:
        conn.sendall(b"GET /outside HTTP/1.0\r\n\r\n")
        conn.recv(64)
    deadline = time.time() + 3
    while len(listener.requests) == before and time.time() < deadline:
        time.sleep(0.05)
    assert len(listener.requests) == before + 1
    # ... and the identical connect inside the sandbox is blocked by the sandbox itself
    connect = ("import socket,sys\n"
               "try:\n"
               "    socket.create_connection(('127.0.0.1', %d), 3).sendall(b'GET /inside HTTP/1.0\\r\\n\\r\\n')\n"
               "except OSError as exc:\n"
               "    print('blocked errno', exc.errno); sys.exit(7)\n"
               "print('connected'); sys.exit(0)\n") % listener.port
    rc, log = verify(world, "/usr/bin/python3 - <<'EOF'\n%sEOF\n" % connect)
    assert rc == 7 and "blocked errno 1" in log, log  # EPERM from the sandbox, not ECONNREFUSED
    rc, log = verify(world, "/usr/bin/curl -sS -m 5 http://127.0.0.1:%d/curl" % listener.port)
    assert rc != 0
    time.sleep(0.3)
    assert len(listener.requests) == before + 1, listener.requests


@NEEDS_SANDBOX
def test_write_outside_work_denied_and_inside_allowed(world):
    outside = world["tmp"] / "outside.txt"
    rc, log = verify(world, "echo x > %s" % outside)
    assert rc != 0 and not outside.exists(), log
    rc, log = verify(world, "echo y > new.txt && cat new.txt && echo z > $TMPDIR/scratch && echo ok")
    assert rc == 0, log
    rc, log = verify(world, "echo y > %s/escape.txt" % world["main"])
    assert rc != 0 and not (world["main"] / "escape.txt").exists()


@NEEDS_SANDBOX
def test_reads_of_home_and_main_worktree_denied(world):
    for target in (world["home"] / "notes.txt", world["home"] / ".ssh" / "id",
                   world["main"] / ".env", world["other"] / "file.txt"):
        rc, log = verify(world, "cat %s" % target)
        assert rc != 0 and "secret" not in log, (target, log)


@NEEDS_SANDBOX
def test_dependency_root_and_work_readable(world):
    rc, log = verify(world, "cat node_modules/pkg/dep.txt && cat app.py")
    assert rc == 0 and "dep-ok" in log, log
    rc, log = verify(world, "echo hack > node_modules/pkg/dep.txt")
    assert rc != 0
    assert (world["main"] / "node_modules/pkg/dep.txt").read_text() == "dep-ok"
    rc, log = verify(world, "cat %s/.env" % (world["main"]))
    assert rc != 0 and "main-secret" not in log


@NEEDS_SANDBOX
def test_toolchain_and_read_roots_readable_but_not_home(world):
    (world["home"] / ".tool" / "v1" / "bin" / "marker.txt").write_text("bin-ok")
    path_env = SYSTEM_PATH + os.pathsep + str(world["home"] / ".tool/v1/bin")
    cmd = "cat %s" % (world["home"] / ".tool/v1/bin/marker.txt")
    rc, log = verify(world, cmd, path_env=path_env)
    assert rc == 0 and "bin-ok" in log, log
    # only the bin directory is granted, not its parent
    rc, log = verify(world, "cat %s" % (world["home"] / ".tool/v1/marker.txt"), path_env=path_env)
    assert rc != 0 and "toolchain-ok" not in log
    rc, log = verify(world, "cat %s" % (world["home"] / "notes.txt"), path_env=path_env)
    assert rc != 0 and "home-secret" not in log
    cmd = "cat %s" % (world["home"] / ".tool/v1/marker.txt")
    rc, log = verify(world, cmd, read_roots=[str(world["home"] / ".tool")])
    assert rc == 0 and "toolchain-ok" in log, log


@NEEDS_SANDBOX
def test_cargo_credentials_stay_denied_with_cargo_bin_on_path(world):
    home = world["home"]
    (home / ".cargo" / "bin").mkdir(parents=True)
    (home / ".cargo" / "bin" / "tool").write_text("tool-ok")
    (home / ".cargo" / "credentials.toml").write_text("cargo-secret")
    path_env = SYSTEM_PATH + os.pathsep + str(home / ".cargo/bin")
    rc, log = verify(world, "cat %s/.cargo/bin/tool" % home, path_env=path_env)
    assert rc == 0 and "tool-ok" in log
    for name in ("credentials.toml", "credentials"):
        rc, log = verify(world, "cat %s/.cargo/%s" % (home, name), path_env=path_env)
        assert rc != 0 and "cargo-secret" not in log


@NEEDS_SANDBOX
def test_share_and_documents_bin_entries_do_not_reopen_their_parents(world):
    home, main = world["home"], world["main"]
    (home / ".local/share/pnpm").mkdir(parents=True)
    (home / ".local/share/opencode").mkdir(parents=True)
    (home / ".local/share/opencode/auth.json").write_text("opencode-secret")
    (home / ".local/share/pnpm/p").write_text("pnpm-ok")
    path_env = SYSTEM_PATH + os.pathsep + str(home / ".local/share/pnpm")
    rc, log = verify(world, "cat %s/.local/share/pnpm/p" % home, path_env=path_env)
    assert rc == 0 and "pnpm-ok" in log
    rc, log = verify(world, "cat %s/.local/share/opencode/auth.json" % home, path_env=path_env)
    assert rc != 0 and "opencode-secret" not in log
    # a worktree that lives under ~/Documents stays closed when ~/Documents/bin is on PATH
    docs_main = home / "Documents" / "proj"
    docs_main.mkdir(parents=True)
    (home / "Documents" / "bin").mkdir()
    (docs_main / ".env").write_text("docs-secret")
    path_env = SYSTEM_PATH + os.pathsep + str(home / "Documents/bin")
    rc, log = verify(world, "cat %s/.env" % docs_main, path_env=path_env)
    assert rc != 0 and "docs-secret" not in log


@NEEDS_SANDBOX
def test_state_dir_and_credential_dirs_deny_even_when_a_root_names_them(world):
    state = world["main"] / ".feature-fix-swarm" / "cloud"
    state.mkdir(parents=True)
    (state / "ledger.json").write_text("state-secret")
    rc, log = verify(world, "cat %s/ledger.json" % state)
    assert rc != 0 and "state-secret" not in log
    with pytest.raises(sb.SandboxError):
        verify(world, "true", read_roots=[str(world["home"])])
    with pytest.raises(sb.SandboxError):
        verify(world, "true", read_roots=[str(world["home"] / ".ssh")])


def test_child_env_allowlist_is_exact(tmp_path):
    env = sb._child_env(str(tmp_path), "/usr/bin")
    assert set(env) == {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "CI"}
    assert env["CI"] == "1" and env["HOME"] == str(tmp_path / "home") and env["TMPDIR"] == str(tmp_path)


@NEEDS_SANDBOX
def test_environment_is_exactly_the_allowlist(world, monkeypatch):
    for name in ("FFS_TEST_LEAK", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "DOPPLER_TOKEN", "SSH_AUTH_SOCK"):
        monkeypatch.setenv(name, "leaked-value")
    rc, log = verify(world, "exec /usr/bin/env")
    assert rc == 0, log
    pairs = [line.split("=", 1) for line in log.splitlines() if "=" in line]
    env = dict(pairs)
    assert len(pairs) == len(env)
    # the allowlist plus the two variables bash itself always exports (PWD, SHLVL)
    assert set(env) == {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "CI", "PWD", "SHLVL"}, sorted(env)
    assert "leaked-value" not in log
    assert env["CI"] == "1" and env["PATH"] == SYSTEM_PATH
    assert env["HOME"].endswith("/home") and str(world["home"]) not in env["HOME"]
    assert env["TMPDIR"] == os.path.dirname(env["HOME"]) and env["PWD"] == env["TMPDIR"] + "/w"


@NEEDS_SANDBOX
def test_exit_code_passthrough_and_signal(world):
    assert verify(world, "exit 7")[0] == 7
    rc, _ = verify(world, "kill -9 $$")
    assert rc == 137


@NEEDS_SANDBOX
def test_timeout_kills_process_group(world):
    marker = "301.%d" % (os.getpid() % 1000 + 1)
    started = time.time()
    rc, _ = verify(world, "sleep %s & sleep %s; echo never" % (marker, marker), timeout_s=1)
    assert rc == sb.RC_TIMEOUT
    assert time.time() - started < 30
    time.sleep(0.5)
    left = subprocess.run(["pgrep", "-f", "sleep %s" % marker], capture_output=True, text=True)
    assert left.stdout.strip() == ""


@NEEDS_SANDBOX
def test_log_is_private_and_redacted(world):
    canary = cloud_scan.canary("github-token").decode()
    rc, log = verify(world, "echo %s" % canary)
    assert rc == 0
    assert canary not in log and "[REDACTED:github-token]" in log
    mode = stat.S_IMODE((world["tmp"] / "verify.log").stat().st_mode)
    assert mode == 0o600


@NEEDS_SANDBOX
def test_missing_dep_is_skipped_and_work_dir_removed(world):
    rc, log = verify(world, "ls; pwd", deps=("does-not-exist", "../escape", "/abs"))
    assert rc == 0 and "does-not-exist" not in log.split("\n")[0:3]
    cwd = log.strip().splitlines()[-1]
    assert not os.path.exists(cwd)


def test_unavailable_raises(monkeypatch, world):
    monkeypatch.setattr(sb, "sandbox_available", lambda: False)
    with pytest.raises(sb.SandboxUnavailable):
        verify(world, "true")


def test_not_darwin_is_unavailable(monkeypatch):
    sb._PREFLIGHT.clear()
    monkeypatch.setattr(sb.sys, "platform", "linux")
    assert sb.sandbox_available() is False
    sb._PREFLIGHT.clear()


# ------------------------------------------------- S1: Mach, Launch Services, IPC


def pgrep_exact(name: str):
    out = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True).stdout
    return {int(x) for x in out.split()}


@NEEDS_SANDBOX
def test_open_url_cannot_reach_a_listener(world, listener):
    rc, log = verify(world, "open -g 'http://127.0.0.1:%d/leak?d=x'; echo open-rc=$?; sleep 3" % listener.port)
    time.sleep(2)
    assert listener.requests == [], listener.requests
    assert "open-rc=0" not in log, log


@NEEDS_SANDBOX
def test_open_application_does_not_start_it(world):
    before = pgrep_exact("Calculator")
    try:
        rc, log = verify(world, "open -g -j -a Calculator; echo open-rc=$?; sleep 2")
        time.sleep(1)
        started = pgrep_exact("Calculator") - before
        assert not started, "Calculator was launched from inside the sandbox"
        assert "open-rc=0" not in log, log
    finally:
        for pid in pgrep_exact("Calculator") - before:
            try:
                os.kill(pid, 9)
            except OSError:
                pass


@NEEDS_SANDBOX
def test_defaults_write_fails_and_creates_no_plist(world):
    domain = "com.ffs.sandboxtest%d" % os.getpid()
    plist = Path(os.path.expanduser("~/Library/Preferences/%s.plist" % domain))
    assert not plist.exists()
    try:
        rc, log = verify(world, "defaults write %s k v; echo defaults-rc=$?" % domain)
        time.sleep(0.5)
        assert "defaults-rc=0" not in log, log
        assert not plist.exists(), "defaults write created a preferences plist outside the sandbox"
    finally:
        if plist.exists():
            subprocess.run(["defaults", "delete", domain], capture_output=True)
            plist.unlink()


@NEEDS_SANDBOX
def test_clipboard_unreachable_and_untouched(world):
    probe = subprocess.run(["pbpaste"], capture_output=True)
    if probe.returncode != 0:
        pytest.skip("no pasteboard in this session")
    saved = probe.stdout
    marker = ("ffs-sandbox-test-%d-%f" % (os.getpid(), time.time())).encode()
    try:
        subprocess.run(["pbcopy"], input=marker, check=True)
        rc, log = verify(world, "pbpaste; echo paste-rc=$?; echo poisoned | pbcopy; echo copy-rc=$?")
        assert marker.decode() not in log, "the sandbox read the clipboard"
        assert "paste-rc=0" not in log or "paste-rc=0" in log and marker.decode() not in log
        assert "copy-rc=0" not in log, log
        assert subprocess.run(["pbpaste"], capture_output=True).stdout == marker, "the sandbox changed the clipboard"
    finally:
        subprocess.run(["pbcopy"], input=saved, check=False)


@NEEDS_SANDBOX
def test_signals_to_processes_outside_the_sandbox_are_denied(world):
    bystander = subprocess.Popen(["sleep", "60"])
    try:
        cmd = "kill -TERM %d; echo term-rc=$?; kill -0 %d; echo probe-rc=$?; kill -KILL %d; echo kill-rc=$?" % (
            bystander.pid, os.getpid(), bystander.pid)
        rc, log = verify(world, cmd)
        assert "term-rc=0" not in log and "probe-rc=0" not in log and "kill-rc=0" not in log, log
        time.sleep(0.3)
        assert bystander.poll() is None, "the sandbox killed a process outside it"
    finally:
        bystander.kill()
        bystander.wait()


@NEEDS_SANDBOX
def test_signals_inside_the_sandbox_still_work(world):
    rc, log = verify(world, "sleep 30 & pid=$!; kill $pid; wait $pid; echo waited=$?; (sleep 30 & kill $!; wait; echo sub=$?)")
    assert rc == 0 and "waited=143" in log, log


@NEEDS_SANDBOX
def test_process_listing_of_others_is_denied(world):
    rc, log = verify(world, "ps -p %d -o pid=; echo ps-rc=$?" % os.getpid())
    assert str(os.getpid()) not in log.replace("ps-rc", "") and "ps-rc=0" not in log, log


@NEEDS_SANDBOX
def test_ordinary_test_runners_still_work(world):
    cases = [
        ("echo a | tr a-z A-Z | cat; test -d . && echo cwd-ok", "A"),
        ("git --version", "git version"),
        ("python3 -c 'import subprocess,json,sqlite3;print(\"py-ok\", sqlite3.connect(\":memory:\").execute(\"select 1\").fetchone())'", "py-ok (1,)"),
        ("python3 -c 'import getpass,pwd,os;print(\"user-ok\", bool(pwd.getpwuid(os.getuid()).pw_name))'", "user-ok True"),
        ("python3 -c 'import asyncio,multiprocessing as m;asyncio.run(asyncio.sleep(0));print(\"mp\", m.Pool(2).map(abs,[-3]))'", "mp [3]"),
        ("git init -q r && cd r && echo x > f && git add f && git -c user.name=a -c user.email=a@b commit -qm m && git log --oneline | wc -l", "1"),
    ]
    for cmd, expect in cases:
        rc, log = verify(world, cmd)
        assert rc == 0 and expect in log, (cmd, rc, log)


@NEEDS_SANDBOX
def test_node_still_works(world):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    path_env = SYSTEM_PATH + os.pathsep + os.path.dirname(node)
    rc, log = verify(world, "node -e 'console.log(\"node-ok\", require(\"os\").userInfo().username.length > 0)'", path_env=path_env)
    assert rc == 0 and "node-ok true" in log, log


@NEEDS_SANDBOX
def test_operator_temp_trees_are_unreadable_but_own_tmp_works(world):
    marker_dirs = []
    try:
        for base in ("/tmp", "/private/tmp", os.path.realpath(__import__("tempfile").gettempdir())):
            directory = Path(base) / ("ffs-sbx-test-%d-%d" % (os.getpid(), len(marker_dirs)))
            directory.mkdir()
            marker_dirs.append(directory)
            (directory / "secret").write_text("temp-secret")
        for directory in marker_dirs:
            rc, log = verify(world, "cat %s/secret; ls %s" % (directory, directory))
            assert rc != 0 and "temp-secret" not in log, (directory, log)
        for target in ("/private/tmp", "/private/var/folders", "/tmp/"):
            rc, log = verify(world, "ls %s" % target)
            assert rc != 0 and "Operation not permitted" in log, (target, log)
        rc, log = verify(world, "echo mine > $TMPDIR/own && cat $TMPDIR/own && mktemp $TMPDIR/x.XXXX >/dev/null && echo ok")
        assert rc == 0 and "mine" in log and "ok" in log, log
    finally:
        for directory in marker_dirs:
            shutil.rmtree(directory, ignore_errors=True)


# ------------------------------------------------ S3: dependency roots


def make_dep_link(world, name, target):
    link = world["main"] / name
    link.symlink_to(target)
    return name


@NEEDS_SANDBOX
@pytest.mark.parametrize("label", ["main-self", "main-parent", "home", "home-ssh", "other-worktree", "state-dir", "tmp-root"])
def test_dependency_realpath_overlapping_a_denied_area_is_rejected(world, label):
    state = world["main"] / ".feature-fix-swarm" / "cloud"
    state.mkdir(parents=True)
    targets = {
        "main-self": world["main"], "main-parent": world["tmp"], "home": world["home"], "home-ssh": world["home"] / ".ssh",
        "other-worktree": world["other"], "state-dir": state, "tmp-root": Path("/private/tmp"),
    }
    dep = make_dep_link(world, "dep-" + label, targets[label])
    with pytest.raises(sb.SandboxError):
        verify(world, "true", deps=(dep,))


@NEEDS_SANDBOX
@pytest.mark.parametrize("dep", [".env", ".env.local", ".git", "sub/.git/hooks", ".feature-fix-swarm"])
def test_dependency_naming_a_secret_location_is_rejected(world, dep):
    (world["main"] / dep).mkdir(parents=True, exist_ok=True) if not dep.startswith(".env") else (world["main"] / dep).write_text("s")
    with pytest.raises(sb.SandboxError):
        verify(world, "true", deps=(dep,))


@NEEDS_SANDBOX
def test_dependency_symlink_into_env_file_is_rejected(world):
    dep = make_dep_link(world, "innocent", world["main"] / ".env")
    with pytest.raises(sb.SandboxError):
        verify(world, "true", deps=(dep,))


@NEEDS_SANDBOX
def test_good_dependency_still_links(world):
    rc, log = verify(world, "cat node_modules/pkg/dep.txt")
    assert rc == 0 and "dep-ok" in log


# ------------------------------------------------ S4: stray processes, limits, log


SETSID_CHILD = (
    "import os, sys, time\n"
    "if os.fork() == 0:\n"
    "    os.setsid()\n"
    "    if os.fork() == 0:\n"
    "        %s\n"
    "        print('grandchild', os.getpid()); sys.stdout.flush()\n"
    "        time.sleep(300)\n"
    "    time.sleep(1)\n"  # lets the tracker see the ancestry; a faster escapee is the documented limit
    "    os._exit(0)\n"
    "time.sleep(%s)\n"
)


def child_pids(log: str, word: str = "grandchild"):
    return [int(line.split()[1]) for line in log.splitlines() if line.startswith(word + " ")]


@NEEDS_SANDBOX
def test_setsid_double_fork_child_is_dead_after_run_verify(world):
    code = SETSID_CHILD % ("pass", "1.5")
    rc, log = verify(world, "/usr/bin/python3 - <<'EOF'\n%sEOF\necho parent-done" % code)
    pids = child_pids(log)
    assert rc == 0 and pids, log
    try:
        assert all(wait_dead(pid) for pid in pids), "a setsid child outlived run_verify"
    finally:
        for pid in pids:
            try:
                os.kill(pid, 9)
            except OSError:
                pass


@NEEDS_SANDBOX
def test_setsid_child_that_changes_directory_is_still_killed(world):
    code = SETSID_CHILD % ("os.chdir('/')", "1.5")
    rc, log = verify(world, "/usr/bin/python3 - <<'EOF'\n%sEOF\necho parent-done" % code)
    pids = child_pids(log)
    assert pids, log
    try:
        assert all(wait_dead(pid) for pid in pids)
    finally:
        for pid in pids:
            try:
                os.kill(pid, 9)
            except OSError:
                pass


@NEEDS_SANDBOX
def test_nohup_background_child_is_dead_after_run_verify(world):
    rc, log = verify(world, "nohup sleep 301 >/dev/null 2>&1 & disown; echo nohup-done")
    assert rc == 0
    time.sleep(0.5)
    left = subprocess.run(["pgrep", "-f", "sleep 301"], capture_output=True, text=True).stdout.split()
    assert left == [], left


@NEEDS_SANDBOX
def test_timeout_also_kills_detached_children(world):
    code = SETSID_CHILD % ("pass", "60")
    started = time.time()
    rc, log = verify(world, "/usr/bin/python3 - <<'EOF'\n%sEOF\n" % code, timeout_s=3)
    assert rc == sb.RC_TIMEOUT and time.time() - started < 40
    pids = child_pids(log)
    assert pids, log
    assert all(wait_dead(pid) for pid in pids)


@NEEDS_SANDBOX
def test_rlimits_are_applied_with_defaults_and_overrides(world):
    rc, log = verify(world, "echo core=$(ulimit -c) nofile=$(ulimit -n) fsize=$(ulimit -f) nproc=$(ulimit -u)")
    values = dict(part.split("=") for part in log.split())
    assert values["core"] == "0" and values["nofile"] == str(sb.DEFAULT_NOFILE)
    assert values["fsize"] == str(sb.DEFAULT_FSIZE_BYTES // 1024)  # bash counts 1024-byte blocks
    assert 0 < int(values["nproc"]) <= 2 * 2666
    rc, log = verify(world, "echo nofile=$(ulimit -n) fsize=$(ulimit -f)", nofile=2048, fsize_bytes=1024 * 1024)
    assert "nofile=2048" in log and "fsize=1024" in log, log


@NEEDS_SANDBOX
def test_process_limit_stops_fork_storms(world):
    rc, log = verify(world, "echo a | cat; echo after", nproc_headroom=-300)
    assert "Resource temporarily unavailable" in log and "a\n" not in log.replace("after", ""), log
    rc, log = verify(world, "echo a | cat")
    assert rc == 0 and log.strip() == "a"


@NEEDS_SANDBOX
def test_file_size_limit_stops_disk_fill(world):
    rc, log = verify(world, "head -c 5000000 /dev/zero > big; echo rc=$?; ls -l big | tr -s ' ' | cut -d' ' -f5", fsize_bytes=1024 * 1024)
    assert "Filesize limit exceeded" in log and "rc=153" in log, (rc, log)
    assert int(log.strip().splitlines()[-1]) <= 1024 * 1024


@NEEDS_SANDBOX
def test_log_is_capped_and_marked(world):
    rc, log = verify(world, "head -c 400000 /dev/zero | tr '\\0' 'a' | fold -w 80", max_log_bytes=20000)
    assert rc == 0
    assert len(log) <= 20000 + len(sb.LOG_TRUNCATED) and log.endswith(sb.LOG_TRUNCATED.decode())


@NEEDS_SANDBOX
def test_raw_output_never_touches_the_log_file(world):
    seen = {}
    log_path = world["tmp"] / "verify.log"

    def spy(data: bytes) -> bytes:
        seen["size_during_redaction"] = os.path.getsize(log_path)
        seen["raw"] = data
        return data.replace(b"RAWSECRET", b"[gone]")

    rc, log = verify(world, "echo RAWSECRET-1", redact=spy)
    assert rc == 0 and seen["size_during_redaction"] == 0 and b"RAWSECRET" in seen["raw"]
    assert "RAWSECRET" not in log and "[gone]-1" in log


@NEEDS_SANDBOX
def test_failing_redactor_withholds_the_log(world):
    def boom(_data: bytes) -> bytes:
        raise RuntimeError("redactor down")

    rc, log = verify(world, "echo " + cloud_scan.canary("github-token").decode(), redact=boom)
    assert log == sb.LOG_WITHHELD.decode()


@NEEDS_SANDBOX
def test_budget_exhaustion_withholds_the_log(world, monkeypatch):
    monkeypatch.setattr(cloud_scan, "SCAN_BUDGET_S", -1.0)
    rc, log = verify(world, "echo hello")
    assert log == sb.LOG_WITHHELD.decode()


@NEEDS_SANDBOX
def test_pem_block_is_redacted_whole_in_the_log(world):
    begin = "-----BEGIN " + "RSA PRIVATE" + " KEY-----"
    end = "-----END " + "RSA PRIVATE" + " KEY-----"
    rc, log = verify(world, "printf '%%s\\n' '%s' 'MIIEBODYLINE1' 'MIIEBODYLINE2' '%s' after" % (begin, end))
    assert rc == 0 and "MIIEBODY" not in log and "BEGIN" not in log and "[REDACTED:pem-block]" in log and "after" in log, log


def test_bounded_log_unit(tmp_path):
    sink = sb._BoundedLog(10, lambda b: b)
    sink.feed(b"abcdefgh")
    sink.feed(b"ijklmnop")
    out = sink.render()
    assert out.endswith(sb.LOG_TRUNCATED) and len(out) < 10 + len(sb.LOG_TRUNCATED)
    target = tmp_path / "l"
    sink.write_to(str(target))
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@NEEDS_SANDBOX
@pytest.mark.parametrize("kind", ["argv", "cwd"])
def test_sweep_spares_unrelated_host_process_naming_the_temp_dir(world, monkeypatch, kind):
    """An editor opened on the verify temp dir is not the verifier's process."""
    created = []
    real_mkdtemp = sb.tempfile.mkdtemp

    def spy(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    monkeypatch.setattr(sb.tempfile, "mkdtemp", spy)
    decoys = []

    def launch_decoy():
        deadline = time.time() + 20
        while not created and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(1.0)
        tmp = os.path.realpath(created[0])
        os.makedirs(os.path.join(tmp, "w"), exist_ok=True)
        if kind == "argv":
            decoys.append(subprocess.Popen(["/bin/sh", "-c", "sleep 60; true", "decoy", tmp], start_new_session=True))
        else:
            decoys.append(subprocess.Popen(["/bin/sh", "-c", "sleep 60; true"], cwd=os.path.join(tmp, "w"), start_new_session=True))

    thread = threading.Thread(target=launch_decoy)
    thread.start()
    try:
        rc, log = verify(world, "sleep 3; echo done")
        thread.join()
        assert rc == 0 and decoys, log
        assert decoys[0].poll() is None, "the sweep killed an unrelated host process"
    finally:
        thread.join()
        for decoy in decoys:
            decoy.kill()
            decoy.wait()


@NEEDS_SANDBOX
def test_operator_terminals_cannot_be_opened_for_reading(world):
    ttys = sorted(Path("/dev").glob("ttys[0-9]*"))
    if not ttys:
        pytest.skip("no /dev/ttys* device on this host")
    cmd = "python3 -c 'import os,sys\ntry:\n os.close(os.open(sys.argv[1], os.O_RDONLY | os.O_NONBLOCK)); print(\"tty-open-ok\")\nexcept OSError as e:\n print(\"tty-open-denied\", e.errno)' %s" % ttys[0]
    rc, log = verify(world, cmd)
    assert "tty-open-denied" in log and "tty-open-ok" not in log, log


def test_sweep_selects_nothing_when_the_root_pid_was_recycled() -> None:
    root, started = 500, 1000.0
    live = [sb._Proc(500, 1, 500, 500, 1000.0), sb._Proc(501, 500, 500, 500, 1001.0)]
    tracker = sb._Tracker.__new__(sb._Tracker)
    tracker.root_pid, tracker.started, tracker.known = root, started, {500: 1000.0}
    assert tracker.remember(live) == {500, 501}

    tracker = sb._Tracker.__new__(sb._Tracker)
    tracker.root_pid, tracker.started, tracker.known = root, started, {500: 1000.0}
    recycled = [
        sb._Proc(500, 1, 500, 500, 2000.0),  # pid 500 now belongs to an unrelated process
        sb._Proc(501, 500, 500, 500, 2001.0),  # its child
        sb._Proc(777, 1, 500, 500, 2002.0),  # unrelated member of its group
    ]
    assert tracker.remember(recycled) == set()
    assert tracker.known == {500: 1000.0}


def test_tracker_records_the_root_start_time_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb, "_snapshot", lambda: [sb._Proc(500, 1, 500, 500, 1000.0), sb._Proc(9, 1, 9, 9, 5.0)])
    assert sb._Tracker(500, 1000.0).known == {500: 1000.0}
