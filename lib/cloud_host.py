"""Provider backends for cloud delegation (``/swarm --cloud``).

Contract: docs/cloud-delegation-contracts.md section 5, with amendments 14.1,
14.5, 14.6 and 14.10. Stdlib only, Python 3.9 compatible, no ``shell=True``.

Design points:

* Every child process (provider CLI and git) gets an allowlisted environment.
  API keys, tokens and anything not on the list never reach a child.
* Raw provider output is never written anywhere. ``LaunchCtx.sanitize`` runs on
  the captured bytes first; at most ``LOG_CAP_BYTES`` of the sanitized result is
  written to ``ctx.log_path`` (mode 0600, fsync'd). If the hook raises, a fixed
  notice is written instead and nothing raw survives.
* ``detail`` strings are fixed codes. Git stderr, URLs and prompts are never
  copied into them, because a clone URL may carry credentials.
* Provider-output parsing lives in small pure functions
  (``parse_claude_launch``, ``parse_codex_launch``, ``parse_codex_status``) so
  recorded fixtures can replace the guessed shapes after the live smoke.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

import cloud_scan

# ── Constants ────────────────────────────────────────────────────────────────

LAUNCH_TIMEOUT_S = 180.0
GIT_QUICK_TIMEOUT_S = 60.0
GIT_TRANSFER_TIMEOUT_S = 300.0
STATUS_TIMEOUT_S = 60.0
LAUNCH_TIMEOUT_ENV = "FFS_CLOUD_LAUNCH_TIMEOUT_S"
CLONE_PARENT_ENV = "FFS_CLOUD_CLONE_PARENT"

LOG_CAP_BYTES = 64 * 1024
CAPTURE_CAP_BYTES = 1024 * 1024
DIFF_CAP_BYTES = 16 * 1024 * 1024
#: A binary literal in a patch is zlib-compressed, so a small diff can expand on disk.
#: Applying may grow the quarantine by at most 4x the diff cap, and no single file may exceed it.
APPLIED_GROWTH_CAP_BYTES = 4 * DIFF_CAP_BYTES
APPLIED_FILE_CAP_BYTES = 4 * DIFF_CAP_BYTES
READ_CHUNK_BYTES = 64 * 1024
READER_JOIN_S = 5.0
KILL_WAIT_S = 10.0

MAX_CANDIDATES = 20
NONCE_PREFIX_LEN = 8
BLOB_LIMIT_FILTER = "--filter=blob:limit=5m"

TIMEOUT_RC = 124
SPAWN_FAILED_RC = 127
NOT_RUN_RC = -1

CLAUDE_REF_GLOB = "refs/heads/claude/*"
HEADS_PREFIX = "refs/heads/"

ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TERM",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "XDG_CONFIG_HOME",
    "SSH_AUTH_SOCK",
)
DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
GIT_HARDENING_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
    "GIT_ALLOW_PROTOCOL": "file:git:http:https:ssh",
    "GIT_NO_REPLACE_OBJECTS": "1",
}

BIN_ENV = {"claude": "FFS_CLOUD_BIN_CLAUDE", "codex": "FFS_CLOUD_BIN_CODEX"}
API_KEY_ENV = {"claude": "ANTHROPIC_API_KEY", "codex": "OPENAI_API_KEY"}

COMMIT_NAME = "FFS Cloud Dispatcher"
COMMIT_EMAIL = "ffs-cloud@invalid.example"
SANITIZER_FAILED_NOTICE = b"[output withheld: sanitizer failed]\n"

OID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
CLAUDE_REF_RE = re.compile(r"^refs/heads/claude/[A-Za-z0-9._/-]{1,200}$")
REMOTE_ID_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,200}$")
SAFE_ARG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$")
SAFE_URL_RE = re.compile(r"^[^\s\x00-\x1f\-][^\s\x00-\x1f]{0,1999}$")
IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

CLAUDE_SESSION_RE = re.compile(r"\bsession_[A-Za-z0-9]{8,64}\b")
CLAUDE_URL_RE = re.compile(r"https://claude\.ai/[A-Za-z0-9_./-]{1,160}")
#: Only a session link counts: the client also prints other ``claude.ai/code/...`` links (for example its
#: environment setup page) when it could NOT create a session.
CLAUDE_CODE_URL_RE = re.compile(r"https://claude\.ai/code/(?:session|cse)_(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{8,64}\b")
#: Real ids carry digits (``session_01...``); requiring one keeps words like ``session_duration`` from matching.
#: The client also prints the ``cse_`` form of the same id, so both prefixes count.
CLAUDE_PTY_SESSION_RE = re.compile(r"\b(?:session|cse)_(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{8,64}\b")
CODEX_TASK_RE = re.compile(r"\btask_[A-Za-z0-9_]{4,100}\b")
CODEX_URL_RE = re.compile(r"https://chatgpt\.com/codex/[A-Za-z0-9_./-]{1,160}")
STATUS_LINE_RE = re.compile(r"(?im)^\s*(?:task\s+)?status\s*[:=]\s*([A-Za-z_ -]{1,40}?)\s*$")
STATUS_BRACKET_RE = re.compile(r"(?m)^\s*\[([A-Za-z_ -]{1,20})\]")

STATUS_CLASSES: Dict[str, Tuple[str, ...]] = {
    "running": (
        "running", "pending", "queued", "in_progress", "working",
        "started", "starting", "processing", "submitted", "created",
    ),
    "result-ready": ("ready", "complete", "completed", "succeeded", "success", "done", "finished", "applied"),
    "failed": ("failed", "error", "errored", "cancelled", "canceled", "rejected", "timed_out", "timeout", "expired"),
}

# ── Types ────────────────────────────────────────────────────────────────────


def _identity(data: bytes) -> bytes:
    return data


@dataclass
class LaunchCtx:
    run: str
    task: str
    generation: int
    prompt: str
    task_branch: str
    clone_url: str
    model: Optional[str]
    effort: Optional[str]
    log_path: Path
    env_id: Optional[str] = None
    # Amendments beyond the section 5 field list (appended so positional use stays valid).
    task_branch_oid: str = ""
    refs_snapshot_path: Optional[Path] = None
    sanitize: Callable[[bytes], bytes] = _identity
    repo_root: Optional[Path] = None  # the operator's checkout: the fixed launch clone must not live inside it


@dataclass
class LaunchResult:
    rc: int
    remote_id: Optional[str]
    url: Optional[str]
    outcome: str  # "launched" | "uncertain" | "failed"
    detail: str = ""
    note: str = ""  # fixed code recorded on the ledger entry (``launch_note``); "" for none


@dataclass
class PollCtx:
    run: str
    task: str
    generation: int
    nonce: str
    base_oid: str
    task_branch: str
    remote_id: Optional[str]
    launched_at: float
    refs_snapshot_path: Optional[Path]
    rejected: Sequence[Sequence[str]]
    clone_url: str
    env_id: Optional[str] = None


@dataclass
class PollResult:
    state: str  # "running" | "result-ready" | "failed" | "unknown"
    candidates: List[Tuple[str, str]] = field(default_factory=list)
    detail: str = ""


class UnsupportedBackend(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FetchError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Backend(Protocol):
    name: str

    def available(self) -> Tuple[bool, str]: ...

    def launch(self, ctx: LaunchCtx) -> LaunchResult: ...

    def poll(self, ctx: PollCtx) -> PollResult: ...

    def fetch(self, ctx: PollCtx, candidate: Tuple[str, str], quarantine_repo: Path) -> str: ...


# ── Process helpers ──────────────────────────────────────────────────────────


@dataclass
class ExecResult:
    rc: int
    output: bytes
    timed_out: bool = False
    truncated: bool = False
    spawn_failed: bool = False

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out and not self.spawn_failed

    def text(self) -> str:
        return self.output.decode("utf-8", errors="replace")


def child_env(source: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Allowlisted environment for every child; nothing else is inherited."""
    src = os.environ if source is None else source
    env = {name: src[name] for name in ENV_ALLOWLIST if name in src}
    env.setdefault("PATH", DEFAULT_PATH)
    env.update(GIT_HARDENING_ENV)
    return env


def _drain(stream, sink: List[bytes], state: Dict[str, int], cap: int) -> None:
    """Read until EOF, keeping at most ``cap`` bytes and discarding the rest."""
    while True:
        try:
            data = stream.read1(READ_CHUNK_BYTES)
        except (OSError, ValueError):
            return
        if not data:
            return
        room = cap - state["kept"]
        if room > 0:
            sink.append(data[:room])
            state["kept"] += min(room, len(data))
        if len(data) > room:
            state["truncated"] = 1


def _feed(stream, data: bytes) -> None:
    try:
        stream.write(data)
        stream.close()
    except (OSError, ValueError):
        pass


def _kill_group(proc: "subprocess.Popen[bytes]") -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def run_capture(
    argv: Sequence[str],
    *,
    cwd: Optional[Path],
    env: Dict[str, str],
    timeout: float,
    stdin_bytes: Optional[bytes] = None,
    cap: int = CAPTURE_CAP_BYTES,
    stdout_only: bool = False,
    file_size_limit: Optional[int] = None,
) -> ExecResult:
    """Run argv (no shell) in its own session; kill the whole group on timeout.

    stdout and stderr are merged unless ``stdout_only`` (stderr is then dropped,
    so a warning can never be mistaken for output). stdin is closed unless
    ``stdin_bytes`` is given. ``file_size_limit`` (bytes) caps any single file
    the child writes (``RLIMIT_FSIZE``; the child dies with SIGXFSZ past it).
    """
    preexec = None
    if file_size_limit is not None:
        import resource

        def preexec() -> None:  # runs in the child between fork and exec
            resource.setrlimit(resource.RLIMIT_FSIZE, (file_size_limit, file_size_limit))

    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            stdin=subprocess.DEVNULL if stdin_bytes is None else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL if stdout_only else subprocess.STDOUT,
            start_new_session=True,
            shell=False,
            preexec_fn=preexec,
        )
    except (OSError, ValueError):
        return ExecResult(SPAWN_FAILED_RC, b"", spawn_failed=True)
    chunks: List[bytes] = []
    state = {"kept": 0, "truncated": 0}
    reader = threading.Thread(target=_drain, args=(proc.stdout, chunks, state, cap), daemon=True)
    reader.start()
    if stdin_bytes is not None:
        threading.Thread(target=_feed, args=(proc.stdin, stdin_bytes), daemon=True).start()
    timed_out = False
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        rc = proc.wait(timeout=KILL_WAIT_S)
    reader.join(READER_JOIN_S)
    return ExecResult(
        TIMEOUT_RC if timed_out else rc,
        b"".join(chunks),
        timed_out=timed_out,
        truncated=bool(state["truncated"]),
    )


def resolve_binary(name: str) -> Optional[str]:
    """``FFS_CLOUD_BIN_<NAME>`` (tests) wins; otherwise PATH. None when unusable."""
    override = os.environ.get(BIN_ENV[name], "").strip()
    if override:
        return override if os.access(override, os.X_OK) and os.path.isfile(override) else None
    return shutil.which(name)


# ── Pseudo-terminal launcher (Claude backend only) ───────────────────────────
#
# UNDOCUMENTED PROVIDER BEHAVIOUR: ``claude --cloud`` refuses to run unless
# stdout is a terminal (Claude Code 2.1.329: "--cloud requires an interactive
# terminal"), and there is no documented headless creation path. The launcher
# therefore drives the interactive client through a pseudo-terminal. A CLI
# update can break this. The parent NEVER writes to the pty (no keystrokes, no
# auto-answered dialogs); it only reads, then hangs the terminal up.

PTY_ROWS = 40
PTY_COLS = 120
PTY_TERM = "xterm-256color"
PTY_GRACE_S = 20.0
PTY_GRACE_MAX_S = 120.0
PTY_GRACE_ENV = "FFS_CLOUD_PTY_GRACE_S"
PTY_HANGUP_WAIT_S = 10.0
PTY_TERM_WAIT_S = 5.0
PTY_POLL_S = 0.1
PTY_SCAN_WINDOW_BYTES = 128 * 1024
PROBE_TIMEOUT_S = 8.0
PROBE_SCREEN_CAP_CHARS = 4000
PTY_DRAIN_S = 1.0
#: fixed width, so a capped log is exactly the cap
TRUNCATION_MARKER = b"\n[... %012d bytes of terminal output omitted ...]\n"

NOT_A_TTY_TEXT = "requires an interactive terminal"
LAUNCH_NOTE_NO_URL = "no-session-url"
#: The client exited on its own with no session id/URL and no known error: a session may exist, so it is polled by nonce.
LAUNCH_NOTE_EXIT_NO_URL = "exit-no-session-url"
LOG_HEAD_BYTES = 16 * 1024  # of the 64 KB launch log: the start of the transcript; the rest is its end
CLONE_LOCK_WAIT_S = 600.0
#: Poll note: a new ``claude/*`` ref name matched the secret scanner. The name is never stored, logged or shown.
REF_NAME_SECRET_NOTE = "REF_NAME_SECRET"
CLONE_DIR_NAME = "ffs-cloud-launch-claude"
CLONE_LOCK_NAME = ".ffs-cloud-launch-claude.lock"
#: Sibling of the clone directory, created exclusively before the directory is published (so it survives a killed
#: ``git clone``); it names the directory's device and inode. Only a directory it names is ever deleted.
CLONE_MARKER_SUFFIX = ".ffs-owned"
#: Prefixes of the private staging directories (mkdtemp, 0700) the dispatcher makes in the clone parent: "new" ones hold
#: a clone not yet published, "old" ones a directory or marker retired for an identity check before deletion.
STAGE_NEW_PREFIX = ".ffs-cloud-new-"
STAGE_OLD_PREFIX = ".ffs-cloud-old-"
#: Checkout of the launch clone: no hooks, and no user or system git config (so no filter driver is defined).
NEUTRAL_CHECKOUT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
#: A first-run dialog that waits for a keystroke we never send: the client can never create a session behind it.
BLOCKING_DIALOG_MIN_MARKERS = 2
BLOCKING_DIALOG_MARKERS = ("do you trust", "quick safety check", "trust this folder", "yes, proceed")
DIALOG_MARKERS = (
    "do you trust",
    "quick safety check",
    "is this a project you created",
    "i trust this folder",
    "trust this folder",
    "trust the files",
    "yes, proceed",
    "welcome",
    "log in",
    "login",
    "sign in",
    "choose the text style",
)

_OSC8_RE = re.compile(rb"\x1b\]8;[^;\x07\x1b]*;([^\x07\x1b]*)(?:\x07|\x1b\\)")  # hyperlink target
_OSC_RE = re.compile(rb"\x1b\](?:[^\x07\x1b]*)(?:\x07|\x1b\\|$)")
_STRING_RE = re.compile(rb"\x1b[PX^_](?:[^\x1b]*)(?:\x1b\\|$)")
_CSI_RE = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_CURSOR_RIGHT_RE = re.compile(rb"\x1b\[(\d*)C")  # Ink-style UIs draw spaces as cursor moves
_CURSOR_COLUMN_RE = re.compile(rb"\x1b\[\d*G")  # absolute column: word placement, so it becomes a space
_CURSOR_LINE_RE = re.compile(rb"\x1b\[[0-9;]*[ABEFHdf]")  # moves to another row: keep the words apart
_ERASE_RE = re.compile(rb"\x1b\[[0-9]*[JK]")  # erase in line / display: a redrawn frame starts here
_ESC_RE = re.compile(rb"\x1b[ -/]*[0-~]?")
_CONTROL_RE = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_terminal(data: bytes) -> bytes:
    """Remove ANSI/OSC/DCS escape sequences and control bytes; CR becomes LF."""
    data = _OSC8_RE.sub(lambda m: b" " + m.group(1) + b" " if m.group(1) else b"", data)  # keep the target text
    for pattern in (_OSC_RE, _STRING_RE):
        data = pattern.sub(b"", data)
    data = _CURSOR_RIGHT_RE.sub(lambda m: b" " * min(int(m.group(1) or 1), PTY_COLS), data)
    data = _CURSOR_COLUMN_RE.sub(b" ", data)
    data = _CURSOR_LINE_RE.sub(b"\n", data)
    data = _ERASE_RE.sub(b"\n", data)
    for pattern in (_CSI_RE, _ESC_RE):
        data = pattern.sub(b"", data)
    data = re.sub(rb"\r+\n", b"\n", data).replace(b"\r", b"\n")
    return _CONTROL_RE.sub(b"", data)


@dataclass
class PtyResult:
    rc: Optional[int]
    matched: bool = False
    exited_early: bool = False  # the child ended on its own before we hung the terminal up
    timed_out: bool = False
    spawn_failed: bool = False
    truncated: bool = False


class TailWindow(bytearray):
    """The last ``PTY_SCAN_WINDOW_BYTES`` bytes read, plus a count of every byte read (``total``)."""

    total = 0


def _set_winsize(fd: int) -> None:
    import fcntl
    import struct
    import termios

    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0))
    except OSError:
        pass


def _become_controlling_tty() -> None:  # runs in the child after setsid
    import fcntl
    import termios

    # An orchestrator started under nohup passes SIGHUP=ignored down to its children; the terminal hang-up is the
    # gentle way to end the client (and what ends it if we are killed), so restore the default disposition.
    signal.signal(signal.SIGHUP, signal.SIG_DFL)
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError:
        pass


def _signal_group(proc: "subprocess.Popen[bytes]", sig: int) -> bool:
    """Signal the child's process group; False when the group no longer exists."""
    try:
        os.killpg(proc.pid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _end_pty_child(proc: "subprocess.Popen[bytes]", master: int) -> None:
    """Hang up, then SIGTERM, then SIGKILL the group; never leaves a process behind."""
    try:
        os.close(master)
    except OSError:
        pass
    try:
        proc.wait(timeout=PTY_HANGUP_WAIT_S)
    except subprocess.TimeoutExpired:
        _signal_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=PTY_TERM_WAIT_S)
        except subprocess.TimeoutExpired:
            pass
    _signal_group(proc, signal.SIGKILL)  # stragglers in the group; no-op when it is already empty
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=KILL_WAIT_S)


def _read_master(master: int, sink: bytearray, tail: TailWindow, cap: int) -> Tuple[int, bool, bool]:
    """One non-blocking read. Returns ``(bytes_read, eof, truncated)``.

    ``sink`` is the capped head of the transcript; ``tail`` is the last ``PTY_SCAN_WINDOW_BYTES`` bytes whatever the
    cap, so a redrawing screen can still be scanned, and its end kept, after the head is full."""
    try:
        data = os.read(master, READ_CHUNK_BYTES)
    except BlockingIOError:
        return 0, False, False
    except OSError:  # EIO on Linux once every slave fd is closed
        return 0, True, False
    if not data:
        return 0, True, False
    room = cap - len(sink)
    if room > 0:
        sink.extend(data[:room])
    tail.extend(data)
    tail.total += len(data)
    del tail[:-PTY_SCAN_WINDOW_BYTES]
    return len(data), False, len(data) > room


def join_head_tail(head: bytes, tail: TailWindow) -> bytes:
    """The transcript with both ends: the capped head, then the tail, with a marker where bytes were dropped."""
    total = tail.total
    if total <= len(head):
        return bytes(head)
    tail_start = total - len(tail)
    if tail_start <= len(head):  # the two overlap or touch: nothing was lost
        return bytes(head) + bytes(tail)[len(head) - tail_start:]
    return bytes(head) + TRUNCATION_MARKER % (tail_start - len(head)) + bytes(tail)


def _scan(done: Callable[[str], bool], tail: TailWindow) -> bool:
    return bool(done(strip_terminal(bytes(tail)).decode("utf-8", errors="replace")))


def run_pty(
    argv: Sequence[str],
    *,
    cwd: Optional[Path],
    env: Dict[str, str],
    timeout: float,
    sink: bytearray,
    grace_s: float = 0.0,
    done: Optional[Callable[[str], bool]] = None,
    cap: int = CAPTURE_CAP_BYTES,
    tail: Optional[TailWindow] = None,
) -> PtyResult:
    """Run argv on a pseudo-terminal, reading only, until ``done``/exit/timeout.

    ``sink`` receives the head of the raw transcript incrementally (capped at ``cap``) and ``tail`` its end, so the
    caller can persist both even when this raises (see ``join_head_tail``).
    After ``done(stripped_text)`` first returns True the read continues for ``grace_s`` seconds. The child is
    then ended gently (hang-up, SIGTERM, SIGKILL of its process group). Nothing is ever written to the pty.
    """
    import pty
    import select
    import time

    master, slave = pty.openpty()
    _set_winsize(slave)
    child_environment = dict(env)
    child_environment.setdefault("TERM", PTY_TERM)
    try:
        proc = subprocess.Popen(
            list(argv), cwd=str(cwd) if cwd is not None else None, env=child_environment,
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True, close_fds=True, shell=False,
            preexec_fn=_become_controlling_tty,
        )
    except (OSError, ValueError):
        os.close(master)
        os.close(slave)
        return PtyResult(None, spawn_failed=True)
    os.close(slave)
    os.set_blocking(master, False)
    result = PtyResult(None)
    tail = TailWindow() if tail is None else tail
    deadline = time.monotonic() + timeout
    grace_end: Optional[float] = None
    try:
        while True:
            now = time.monotonic()
            if grace_end is not None and now >= grace_end:
                break
            if grace_end is None and now >= deadline:
                result.timed_out = True
                break
            ready, _w, _x = select.select([master], [], [], PTY_POLL_S)
            got, eof, cut = _read_master(master, sink, tail, cap) if ready else (0, False, False)
            result.truncated = result.truncated or cut
            if got and not result.matched and done is not None and _scan(done, tail):
                result.matched = True
                grace_end = time.monotonic() + max(0.0, grace_s)
            if eof or proc.poll() is not None:
                # whatever the child wrote just before it ended is still in the pty: read it out (bounded), then scan
                drain_end = time.monotonic() + PTY_DRAIN_S
                while time.monotonic() < drain_end:
                    more, _eof, cut = _read_master(master, sink, tail, cap)
                    result.truncated = result.truncated or cut
                    if not more:
                        break
                if not result.matched and done is not None and _scan(done, tail):
                    result.matched = True
                result.exited_early = True
                break
    finally:
        _end_pty_child(proc, master)
    result.rc = proc.returncode
    return result


def run_git(
    args: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    timeout: float = GIT_QUICK_TIMEOUT_S,
    stdin_bytes: Optional[bytes] = None,
    extra_env: Optional[Dict[str, str]] = None,
    cap: int = CAPTURE_CAP_BYTES,
    stdout_only: bool = False,
    file_size_limit: Optional[int] = None,
) -> ExecResult:
    """git with allowlisted env, no prompts, a timeout, and stdin closed."""
    env = child_env()
    if extra_env:
        env.update(extra_env)
    return run_capture(["git"] + list(args), cwd=cwd, env=env, timeout=timeout, stdin_bytes=stdin_bytes, cap=cap,
                       stdout_only=stdout_only, file_size_limit=file_size_limit)


# ── Pure parsers ─────────────────────────────────────────────────────────────


def _valid_id(value: object) -> Optional[str]:
    if isinstance(value, str) and REMOTE_ID_RE.match(value) and not value.startswith("-"):
        return value
    return None


def _json_documents(text: str) -> List[object]:
    """Whole-text JSON, else every line that parses as JSON."""
    stripped = text.strip()
    if not stripped:
        return []
    try:
        return [json.loads(stripped)]
    except ValueError:
        pass
    docs: List[object] = []
    for line in stripped.splitlines():
        try:
            docs.append(json.loads(line))
        except ValueError:
            continue
    return docs


def parse_claude_launch(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(remote_id, url)`` found in ``claude --cloud`` output, else None parts.

    Strict by design: the url must start with ``https://claude.ai/`` and the id
    must look like ``session_<alnum>``. Query strings and fragments are dropped.
    """
    url_match = CLAUDE_URL_RE.search(text)
    url = _valid_id(url_match.group(0).rstrip(".")) if url_match else None
    id_match = CLAUDE_SESSION_RE.search(text)
    return (_valid_id(id_match.group(0)) if id_match else None), url


def parse_claude_pty_output(text: str) -> Tuple[Optional[str], Optional[str]]:
    """``(remote_id, url)`` from the pty transcript: only a ``claude.ai/code/`` URL or a ``session_`` id counts.

    Stricter than ``parse_claude_launch`` because an interactive first screen also prints other claude.ai links
    (login, docs) that must never be mistaken for a created session.
    """
    url_match = CLAUDE_CODE_URL_RE.search(text)
    url = _valid_id(url_match.group(0)) if url_match else None
    id_match = CLAUDE_PTY_SESSION_RE.search(text)
    return (_valid_id(id_match.group(0)) if id_match else None), url


def _session_seen(text: str) -> bool:
    remote_id, url = parse_claude_pty_output(text)
    return bool(remote_id or url)


def parse_codex_launch(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(remote_id, url)`` from ``codex cloud exec`` output (JSON or text)."""
    for doc in _json_documents(text):
        if isinstance(doc, dict):
            found = _valid_id(doc.get("id")) or _valid_id(doc.get("task_id"))
            if found:
                return found, _valid_id(doc.get("url"))
    id_match = CODEX_TASK_RE.search(text)
    url_match = CODEX_URL_RE.search(text)
    url = _valid_id(url_match.group(0).rstrip(".")) if url_match else None
    return (_valid_id(id_match.group(0)) if id_match else None), url


def _classify_status(token: str) -> Optional[str]:
    word = re.sub(r"[\s-]+", "_", token.strip().lower())
    for state, words in STATUS_CLASSES.items():
        if word in words:
            return state
    return None


def _status_from_json(docs: Iterable[object], expected_id: Optional[str]) -> Optional[Tuple[str, str]]:
    for doc in docs:
        items = doc if isinstance(doc, list) else [doc]
        for item in items:
            if not isinstance(item, dict):
                continue
            seen_id = item.get("id") or item.get("task_id")
            if expected_id and seen_id and seen_id != expected_id:
                if isinstance(doc, list):
                    continue
                return "unknown", "ID_MISMATCH"
            raw = item.get("status")
            if isinstance(raw, dict):
                raw = raw.get("state") or raw.get("status")
            if isinstance(raw, str):
                return _state_result(raw)
    return None


def _state_result(token: str) -> Tuple[str, str]:
    state = _classify_status(token)
    clean = re.sub(r"[^A-Za-z0-9_ -]", "", token)[:40]
    if state is None:
        return "unknown", "UNRECOGNIZED_STATUS:" + clean
    return state, clean.lower()


def parse_codex_status(text: str, expected_id: Optional[str] = None) -> Tuple[str, str]:
    """Map ``codex cloud status`` output to ``(state, detail)``.

    state is running / result-ready / failed / unknown. Accepts JSON (``status``,
    optional ``id``; a list of such objects is allowed) or text (``Status: x`` or
    a leading ``[READY]`` style tag).
    """
    from_json = _status_from_json(_json_documents(text), expected_id)
    if from_json is not None:
        return from_json
    tokens = [m.group(1) for m in STATUS_LINE_RE.finditer(text)]
    tokens += [m.group(1) for m in STATUS_BRACKET_RE.finditer(text)]
    for token in tokens:
        if _classify_status(token) is not None:
            return _state_result(token)
    if tokens:
        return _state_result(tokens[0])
    return "unknown", "NO_STATUS"


def _all_claude_refs(text: str) -> Dict[str, str]:
    refs: Dict[str, str] = {}
    for line in text.splitlines():
        oid, _, ref = line.partition("\t")
        if OID_RE.match(oid) and CLAUDE_REF_RE.match(ref) and ".." not in ref:
            refs[ref] = oid
    return refs


def _secret_ref_names(refs: Iterable[str]) -> "set[str]":
    """Ref names the secret scanner flags. A scanner failure raises ``ScanRefused``: it is not a verdict on any name."""
    names = sorted(refs)
    if not names:
        return set()
    if not cloud_scan.scan_bytes("\n".join(names).encode("utf-8", "replace"), label="ref"):
        return set()
    return {n for n in names if cloud_scan.scan_bytes(n.encode("utf-8", "replace"), label="ref")}


def redacted_ref_key(ref: str) -> str:
    """A stable stand-in for a ref whose name matched the scanner: the name itself is never stored or shown."""
    return "redacted:" + hashlib.sha256(ref.encode("utf-8", "replace")).hexdigest()[:16]


def split_ls_remote(text: str) -> Tuple[Dict[str, str], Dict[str, Tuple[str, str]]]:
    """``(clean refs, flagged)``: ``flagged`` maps ``redacted_ref_key`` to ``(ref, oid)`` and is for in-memory use only.

    Raises ``cloud_scan.ScanRefused`` when the scanner fails: callers treat that as "unknown", never as a flag."""
    refs = _all_claude_refs(text)
    bad = _secret_ref_names(refs)
    clean = {r: o for r, o in refs.items() if r not in bad}
    return clean, {redacted_ref_key(r): (r, refs[r]) for r in bad}


def parse_ls_remote(text: str) -> Dict[str, str]:
    """``git ls-remote`` output to ``{ref: oid}``, keeping only well-formed claude/* refs whose name is clean."""
    return split_ls_remote(text)[0]


def normalize_rejected(rejected: Sequence[Sequence[str]]) -> "set[Tuple[str, str]]":
    """JSON round-trips pairs as lists; compare as tuples."""
    pairs = set()
    for entry in rejected or ():
        if isinstance(entry, (list, tuple)) and len(entry) == 2 and all(isinstance(x, str) for x in entry):
            pairs.add((entry[0], entry[1]))
    return pairs


_SIBLING_TAIL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}-[0-9a-f]{8}$")


def _is_sibling_ref(ref: str, run: str, task: str, prefix: str) -> bool:
    """True for ``claude/ffs-<run>-<other task>-<8 hex>``: another task's result of this run."""
    head = "refs/heads/claude/ffs-%s-" % run
    if not run or not ref.startswith(head):
        return False
    tail = ref[len(head):]
    return bool(_SIBLING_TAIL_RE.match(tail)) and tail != "%s-%s" % (task, prefix)


def select_candidates(
    refs: Dict[str, str],
    snapshot: Optional[Dict[str, str]],
    nonce: str,
    rejected: Sequence[Sequence[str]],
    run: str = "",
    task: str = "",
) -> Tuple[List[Tuple[str, str]], bool]:
    """Nonce-named refs first, then refs new or changed since the snapshot.

    Rejected pairs are dropped before the cap. Without a snapshot only nonce
    matches qualify (we cannot tell what is new). With ``run`` given, a ref
    named ``claude/ffs-<run>-<other task>-<8 hex>`` belongs to a sibling task
    of the same run and is never a candidate, however new it is (two tasks of
    one run would otherwise each bind the other's result). Un-named refs keep
    the "new or changed" rule. Returns ``(candidates, capped)``.
    """
    skip = normalize_rejected(rejected)
    prefix = nonce[:NONCE_PREFIX_LEN] if len(nonce) >= NONCE_PREFIX_LEN else ""
    usable = {r: o for r, o in refs.items() if not _is_sibling_ref(r, run, task, prefix)}
    named = [r for r in sorted(usable) if prefix and prefix in r]
    changed: List[str] = []
    if snapshot is not None:
        changed = [r for r in sorted(usable) if r not in named and snapshot.get(r) != usable[r]]
    ordered = [(ref, usable[ref]) for ref in named + changed if (ref, usable[ref]) not in skip]
    return ordered[:MAX_CANDIDATES], len(ordered) > MAX_CANDIDATES


# ── Log and snapshot writers ─────────────────────────────────────────────────


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_private_file(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` as a 0600 file, fsync'd (file and directory)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(path.parent)


def write_snapshot(path: Path, refs: Dict[str, str]) -> None:
    """Atomic replace so a crash never leaves a half-written snapshot."""
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    write_private_file(tmp, json.dumps(refs, sort_keys=True).encode("utf-8"))
    os.replace(str(tmp), str(path))
    _fsync_dir(path.parent)


def load_refs_snapshot(path: Optional[Path]) -> Optional[Dict[str, str]]:
    """Read the launch snapshot; None when missing or unreadable."""
    if path is None:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _sanitize_bytes(ctx: LaunchCtx, raw: bytes) -> Tuple[bytes, str]:
    """Run the sanitize hook once; a failing hook yields the fixed notice and ``SANITIZER_FAILED``."""
    try:
        clean = ctx.sanitize(raw)
        if not isinstance(clean, bytes):
            raise TypeError("sanitize must return bytes")
    except Exception:  # noqa: BLE001 - any hook failure must withhold the output
        return SANITIZER_FAILED_NOTICE, "SANITIZER_FAILED"
    return clean, ""


def cap_head_tail(data: bytes, cap: int = LOG_CAP_BYTES, head: int = LOG_HEAD_BYTES) -> bytes:
    """At most ``cap`` bytes holding the start and the END of ``data`` with a marker between, so a late line (the
    session URL, an error) survives next to the startup frames. Shorter data is returned unchanged."""
    if len(data) <= cap:
        return data
    keep_tail = cap - head - len(TRUNCATION_MARKER % 0)
    if keep_tail <= 0:
        return data[:cap]
    return data[:head] + TRUNCATION_MARKER % (len(data) - head - keep_tail) + data[-keep_tail:]


def _write_launch_log(ctx: LaunchCtx, clean: bytes, detail: str) -> Tuple[bytes, str]:
    capped = cap_head_tail(clean)
    try:
        write_private_file(Path(ctx.log_path), capped)
    except OSError:
        detail = (detail + " LOG_WRITE_FAILED").strip()
    return capped, detail


def sanitize_and_log(ctx: LaunchCtx, raw: bytes) -> Tuple[bytes, str]:
    """Sanitize, cap to 64 KB (start and end are both kept), write 0600 to ``ctx.log_path``.

    Returns ``(sanitized_capped_bytes, detail)``. ``detail`` is "" on success.
    Sanitization happens before truncation, and nothing unsanitized is written.
    """
    clean, detail = _sanitize_bytes(ctx, raw)
    return _write_launch_log(ctx, clean, detail)


# ── Shared validation ────────────────────────────────────────────────────────


def normalize_branch(name: str) -> str:
    return name[len(HEADS_PREFIX):] if name.startswith(HEADS_PREFIX) else name


def _branch_ok(name: str) -> bool:
    bad = ("..", "//", "@{")
    return bool(BRANCH_RE.match(name)) and not any(t in name for t in bad) and not name.endswith(("/", ".", ".lock"))


def _safe_prompt(prompt: str) -> Optional[str]:
    """Positional prompt must not parse as an option; NUL and empty are refused."""
    if not prompt or "\x00" in prompt:
        return None
    return " " + prompt if prompt.startswith("-") else prompt


def _api_key_reason(name: str) -> Optional[str]:
    var = API_KEY_ENV[name]
    if os.environ.get(var, "").strip():
        return "API_KEY_BACKEND: %s is set (OAuth only)" % var
    return None


def _failed(detail: str, rc: int = NOT_RUN_RC) -> LaunchResult:
    return LaunchResult(rc=rc, remote_id=None, url=None, outcome="failed", detail=detail)


def _timeout_from_env(default: float) -> float:
    raw = os.environ.get(LAUNCH_TIMEOUT_ENV, "")
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if 0 < value <= default else default


def _blocking_dialog(text: str) -> bool:
    """At least two distinct first-run dialog phrases: one echoed word (for example in the prompt) is not enough."""
    squashed = " ".join(text.lower().split())
    return sum(1 for marker in BLOCKING_DIALOG_MARKERS if marker in squashed) >= BLOCKING_DIALOG_MIN_MARKERS


def _prompt_ambiguous(prompt: str) -> bool:
    """True when the prompt, echoed back by the interactive client, would itself look like a session id/URL, the
    not-a-terminal error, or a first-run dialog. The echo cannot be told from the client's own output, so such a
    prompt is refused before anything is started."""
    remote_id, url = parse_claude_pty_output(prompt)
    squashed = " ".join(prompt.lower().split())
    return bool(remote_id or url) or NOT_A_TTY_TEXT in squashed or _blocking_dialog(prompt)


def _grace_from_env() -> float:
    raw = os.environ.get(PTY_GRACE_ENV, "")
    try:
        value = float(raw)
    except ValueError:
        return PTY_GRACE_S
    return value if 0 <= value <= PTY_GRACE_MAX_S else PTY_GRACE_S


def _quarantine_check(repo: Path, timeout: float) -> None:
    """Quarantine must be a git repo with no remotes (URLs go on the command line)."""
    probe = run_git(["-C", str(repo), "rev-parse", "--git-dir"], timeout=timeout)
    if not probe.ok:
        raise FetchError("QUARANTINE_NOT_A_REPO")
    # stdout only: a git warning on stderr is not a remote name.
    remotes = run_git(["-C", str(repo), "remote"], timeout=timeout, stdout_only=True)
    if not remotes.ok:
        raise FetchError("QUARANTINE_REMOTE_CHECK_FAILED")
    if remotes.output.strip():
        raise FetchError("QUARANTINE_HAS_REMOTES")


def _scrub_promisor_remote(repo: Path, timeout: float) -> None:
    """Undo the remote that a ``--filter`` fetch registers.

    ``git fetch --filter=... <url> <oid>`` writes ``remote.<url>.promisor`` and
    ``extensions.partialclone`` into the repo, so the URL (possibly carrying
    credentials) is stored and later reads (``git log -p``, scans) would lazily
    fetch any blob the filter skipped. Quarantine must stay offline and
    remoteless, so the section is removed again and a skipped blob stays a
    missing object (``git rev-list --objects --missing=print`` lists it as
    ``?<oid>``). A leftover remote is raised by the caller's final check.
    """
    names = run_git(["-C", str(repo), "remote"], timeout=timeout, stdout_only=True)
    for name in names.text().splitlines():
        if name.strip():
            run_git(["-C", str(repo), "config", "--local", "--remove-section", "remote." + name.strip()], timeout=timeout)
    run_git(["-C", str(repo), "config", "--local", "--unset-all", "extensions.partialclone"], timeout=timeout)
    left = run_git(["-C", str(repo), "remote"], timeout=timeout, stdout_only=True)
    if not left.ok or left.output.strip():
        raise FetchError("QUARANTINE_HAS_REMOTES")


def _tree_bytes(repo: Path) -> int:
    """Total size in bytes of every file under ``repo`` (working tree and ``.git``)."""
    total = 0
    for root, _dirs, files in os.walk(str(repo)):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def _launch_outcome_for(exec_result: ExecResult) -> Optional[LaunchResult]:
    """Common non-success outcomes of a provider exec, else None."""
    if exec_result.spawn_failed:
        return _failed("SPAWN_FAILED", SPAWN_FAILED_RC)
    if exec_result.timed_out:
        return LaunchResult(TIMEOUT_RC, None, None, "uncertain", "LAUNCH_TIMEOUT")
    return None


def probe_claude_first_screen(
    directory: Path,
    *,
    timeout: float = PROBE_TIMEOUT_S,
    sanitize: Callable[[bytes], bytes] = _identity,
) -> Dict[str, object]:
    """Start the plain ``claude`` binary (no ``--cloud``, no prompt) on a pty in ``directory`` and report the
    first screen. No keystroke is sent and no session is created. ``dialog`` is a text heuristic only."""
    binary = resolve_binary("claude")
    if binary is None:
        return {"started": False, "error": "BIN_NOT_FOUND: claude"}
    sink = bytearray()
    outcome = run_pty([binary], cwd=Path(directory), env=child_env(), timeout=timeout, sink=sink)
    if outcome.spawn_failed:
        return {"started": False, "error": "SPAWN_FAILED"}
    text = strip_terminal(bytes(sink)).decode("utf-8", errors="replace")
    lowered = " ".join(text.lower().split())
    try:
        shown = sanitize(text.encode("utf-8")).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - a failing hook withholds the screen
        shown = SANITIZER_FAILED_NOTICE.decode("ascii").strip()
    markers = [m for m in DIALOG_MARKERS if m in lowered]
    return {
        "started": True, "exited_early": outcome.exited_early, "rc": outcome.rc, "dialog": bool(markers),
        "markers": markers, "bytes": len(sink), "screen": shown[:PROBE_SCREEN_CAP_CHARS],
    }


def _repo_root_of_cwd() -> Optional[str]:
    probe = run_git(["rev-parse", "--show-toplevel"], cwd=Path.cwd(), timeout=GIT_QUICK_TIMEOUT_S, stdout_only=True)
    return probe.text().strip() if probe.ok and probe.text().strip() else None


def _fold_path(path: str) -> str:
    """Real path, case-folded: a case-insensitive volume must not let ``Repo`` and ``repo`` look different."""
    return os.path.normcase(os.path.realpath(path)).casefold().rstrip(os.sep) or os.sep


def _overlaps_any(candidate: str, folded: Iterable[str]) -> bool:
    """True when ``candidate`` equals, lies inside, or contains any folded path (both directions)."""
    for other in folded:
        if candidate == other or candidate.startswith(other.rstrip(os.sep) + os.sep) \
                or other.startswith(candidate.rstrip(os.sep) + os.sep):
            return True
    return False


def _validated_clone_parent(raw: str, repo_root: Optional[Path]) -> Tuple[Optional[Path], str]:
    """``(real parent path, "")`` or ``(None, reason)``: absolute, an existing directory, not a symlink, owned by the
    current user, and not inside the operator's checkout."""
    if not os.path.isabs(raw):
        return None, "NOT_ABSOLUTE"
    try:
        info = os.lstat(raw)
    except OSError:
        return None, "MISSING"
    if stat.S_ISLNK(info.st_mode):
        return None, "SYMLINK"
    if not stat.S_ISDIR(info.st_mode):
        return None, "NOT_A_DIRECTORY"
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        return None, "NOT_OWNED"
    real = os.path.realpath(raw)
    root = os.path.realpath(str(repo_root)) if repo_root is not None else _repo_root_of_cwd()
    if root is not None:
        folded_root, folded = _fold_path(root), _fold_path(real)
        if folded == folded_root or folded.startswith(folded_root + os.sep):
            return None, "INSIDE_REPO"
    return Path(real), ""


def _clone_child_conflict(child: Path, repo_root: Optional[Path]) -> str:
    """``""`` or ``CLONE_PATH_INVALID``: the clone child must neither lie inside nor contain the operator's checkout or
    any of its linked worktrees. An unknown checkout or an unreadable worktree list fails closed."""
    root = str(repo_root) if repo_root is not None else _repo_root_of_cwd()
    if not root:
        return "CLONE_PATH_INVALID"
    listing = run_git(["worktree", "list", "--porcelain"], cwd=Path(root), timeout=GIT_QUICK_TIMEOUT_S, stdout_only=True)
    if not listing.ok:
        return "CLONE_PATH_INVALID"
    roots = [root]
    for line in listing.text().splitlines():
        if line.startswith("worktree "):
            roots.append(line[len("worktree "):])
    if _overlaps_any(_fold_path(str(child)), [_fold_path(r) for r in roots]):
        return "CLONE_PATH_INVALID"
    return ""


def _lock_clone_parent(parent: Path) -> Tuple[Optional[int], str]:
    """Take the exclusive flock that serializes use of the fixed clone path; ``(fd, "")`` or ``(None, code)``."""
    import errno
    import fcntl
    import time

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(str(parent / CLONE_LOCK_NAME), flags, 0o600)
    except OSError:
        return None, "CLONE_LOCK_FAILED"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise OSError(errno.EPERM, "unexpected lock file")
    except OSError:
        os.close(fd)
        return None, "CLONE_LOCK_FAILED"
    deadline = time.monotonic() + CLONE_LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd, ""
        except OSError as exc:
            if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                os.close(fd)
                return None, "CLONE_LOCK_FAILED"
        if time.monotonic() >= deadline:
            os.close(fd)
            return None, "CLONE_LOCK_BUSY"
        time.sleep(0.2)


def _clone_marker_path(clone: Path) -> Path:
    """The ownership marker sits beside the clone (in the validated parent), so it exists before ``git clone`` does."""
    return clone.with_name(clone.name + CLONE_MARKER_SUFFIX)


CLONE_MARKER_RE = re.compile(rb"([0-9]{1,20}):([0-9]{1,20})\n")


def _read_own_file(path: Path) -> Optional[bytes]:
    """The content (at most 64 bytes) of a regular, single-link file owned by this user, else ``None``. Never follows a
    symlink or blocks on a FIFO."""
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 64:
            return None
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            return None
        return os.read(fd, 65)
    except OSError:
        return None
    finally:
        os.close(fd)


def _read_marker(path: Path) -> Optional[Tuple[int, int]]:
    """The ``(device, inode)`` a dispatcher marker names, or ``None`` for anything else at that name: it must be a
    regular file with one link, owned by this user, holding exactly ``dev:ino``."""
    content = _read_own_file(path)
    found = CLONE_MARKER_RE.fullmatch(content) if content is not None else None
    return (int(found.group(1)), int(found.group(2))) if found else None


def _is_own_empty_file(path: Path) -> bool:
    """An empty, single-link, user-owned regular file: what a killed or failed marker write can leave behind."""
    return _read_own_file(path) == b""


def _create_marker(path: Path, device: int, inode: int) -> None:
    """Write the marker atomically: an exclusive 0600 temp file (the staging prefix, same parent) is filled and synced,
    then renamed to the marker name without replacing anything, so the name never holds a partial marker."""
    fd, temp = tempfile.mkstemp(prefix=STAGE_NEW_PREFIX, dir=str(path.parent))
    try:
        try:
            data = memoryview(("%d:%d\n" % (device, inode)).encode("ascii"))
            while data:
                data = data[os.write(fd, data):]
            os.fsync(fd)
        finally:
            os.close(fd)
        _rename_noreplace(temp, str(path))
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _rename_noreplace(src: str, dst: str) -> None:
    """``rename(2)`` that fails with ``FileExistsError`` when ``dst`` exists (a plain rename silently replaces an empty
    directory): the kernel's no-replace rename where there is one, else a check and a rename."""
    import ctypes
    import errno

    call = None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if os.uname().sysname == "Darwin":
            call = lambda a, b: libc.renamex_np(a, b, 0x4)  # RENAME_EXCL
        else:
            call = lambda a, b: libc.renameat2(-100, a, -100, b, 0x1)  # AT_FDCWD, RENAME_NOREPLACE
    except (OSError, AttributeError):
        call = None
    if call is not None:
        ctypes.set_errno(0)
        if call(os.fsencode(src), os.fsencode(dst)) == 0:
            return
        code = ctypes.get_errno()
        if code not in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
            raise OSError(code, os.strerror(code), dst)
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "target exists", dst)
    os.rename(src, dst)


def _move_aside(path: Path, name: str, prefix: str) -> Optional[Tuple[str, str]]:
    """Rename ``path`` into a fresh private (mkdtemp, 0700) staging directory beside it; ``(staging, moved)``, or
    ``None`` when the name is absent or cannot be moved. Whatever is then inspected or deleted is the thing that was
    moved, never a name another process can re-point."""
    try:
        staging = tempfile.mkdtemp(prefix=prefix, dir=str(path.parent))
    except OSError:
        return None
    moved = os.path.join(staging, name)
    try:
        os.rename(str(path), moved)
    except OSError:
        os.rmdir(staging)
        return None
    return staging, moved


def _put_back(moved: str, path: Path, staging: str) -> None:
    """Restore a moved-aside name if it is still free; otherwise leave it in staging (nothing is ever deleted)."""
    try:
        _rename_noreplace(moved, str(path))
        os.rmdir(staging)
    except OSError:
        pass


def _drop_marker(clone: Path) -> str:
    """Remove the marker of a directory that is gone; ``""`` or ``CLONE_PATH_INVALID MARKER_UNRECOGNIZED``. Only a file
    that verifies as a dispatcher marker is removed, and it is verified after being moved into staging; anything else
    at that name is put back (or left in staging) and refused."""
    marker = _clone_marker_path(clone)
    if not os.path.lexists(str(marker)):
        return ""
    if _read_marker(marker) is None and not _is_own_empty_file(marker):
        return "CLONE_PATH_INVALID MARKER_UNRECOGNIZED"
    aside = _move_aside(marker, "marker", STAGE_OLD_PREFIX)
    if aside is None:
        return ""
    staging, moved = aside
    if _read_marker(Path(moved)) is None and not _is_own_empty_file(Path(moved)):
        _put_back(moved, marker, staging)
        return "CLONE_PATH_INVALID MARKER_UNRECOGNIZED"
    shutil.rmtree(staging, ignore_errors=True)
    return ""


def _owns_clone(clone: Path) -> bool:
    """True only for a plain directory the dispatcher created: the sibling marker records its device and inode."""
    try:
        info = os.lstat(str(clone))
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and _read_marker(_clone_marker_path(clone)) == (info.st_dev, info.st_ino)


def _claim_clone(clone: Path) -> str:
    """Create the clone directory in private staging, pin its identity in the marker, then publish it at the fixed path
    with a rename that fails if the name exists; ``""`` or an error code. A name that already exists is never claimed,
    marked or replaced."""
    if os.path.lexists(str(clone)):
        return "CLONE_PATH_INVALID"  # the rename below is the authoritative check; this one moves nothing
    try:
        staging = tempfile.mkdtemp(prefix=STAGE_NEW_PREFIX, dir=str(clone.parent))
    except OSError:
        return "CLONE_DIR_FAILED"
    marked, reason = False, "CLONE_DIR_FAILED"
    try:
        fresh = os.path.join(staging, "new")
        os.mkdir(fresh, 0o700)
        info = os.lstat(fresh)
        reason = "CLONE_PATH_INVALID MARKER_EXISTS"
        _create_marker(_clone_marker_path(clone), info.st_dev, info.st_ino)  # before publishing: a kill never wedges
        marked, reason = True, "CLONE_PATH_INVALID"
        _rename_noreplace(fresh, str(clone))
    except OSError as exc:
        if not isinstance(exc, FileExistsError) and reason == "CLONE_PATH_INVALID MARKER_EXISTS":
            reason = "CLONE_DIR_FAILED"
        if marked:
            _drop_marker(clone)  # the marker this call just created
        shutil.rmtree(staging, ignore_errors=True)  # holds only the directory this call just made
        return reason
    os.rmdir(staging)
    return ""


def _release_clone(clone: Path) -> str:
    """Delete the clone only with ownership proof; ``""`` or a ``CLONE_PATH_INVALID`` reason. The directory is renamed
    into private staging and its device and inode compared with the marker there; only a match is deleted. A mismatch
    is put back if its name is free, else left in staging, never deleted. The marker goes last."""
    marker = _clone_marker_path(clone)
    named = _read_marker(marker)
    if os.path.lexists(str(clone)):
        if named is None or not _owns_clone(clone):  # cheap pre-check: moves nothing that already fails
            return "CLONE_PATH_INVALID"
        aside = _move_aside(clone, "retired", STAGE_OLD_PREFIX)
        if aside is None:
            return "CLONE_PATH_INVALID MOVE_FAILED"
        staging, moved = aside
        try:
            info = os.lstat(moved)
            match = stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == named
        except OSError:
            match = False
        if not match:
            _put_back(moved, clone, staging)
            return "CLONE_PATH_INVALID MOVED_AWAY"
        shutil.rmtree(staging, ignore_errors=True)
        if os.path.lexists(staging):
            return "CLONE_PATH_STALE"  # the marker stays: it still names the remains in staging
    return "" if os.path.lexists(str(clone)) else _drop_marker(clone)


def _recover_staging(parent: Path) -> None:
    """Rule: only what is provably the dispatcher's own is removed, without operator action: a mkdtemp staging directory
    with its prefix (0700, owned by this user) only when empty (or holding only an empty ``new``), and the retired
    directory in an old-staging directory only when the marker names it. Anything else is left where it is."""
    named = _read_marker(_clone_marker_path(parent / CLONE_DIR_NAME))
    try:
        entries = sorted(os.listdir(str(parent)))
    except OSError:
        return
    for entry in entries:
        path = parent / entry
        fresh = entry.startswith(STAGE_NEW_PREFIX)
        if not (fresh or entry.startswith(STAGE_OLD_PREFIX)):
            continue
        try:
            info = os.lstat(str(path))
            if fresh and stat.S_ISREG(info.st_mode):
                if _read_marker(path) is not None or _is_own_empty_file(path):
                    os.unlink(str(path))  # a marker temp file a killed write left (never published, so never named)
                continue
            if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
                continue
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                continue
            if fresh:
                # The dispatcher only ever leaves an empty ``new`` here (the clone is made after publication), so
                # only empty directories are removed; rmdir fails on anything else, which is then left in place.
                if os.path.lexists(os.path.join(str(path), "new")):
                    os.rmdir(os.path.join(str(path), "new"))
                os.rmdir(str(path))
                continue
            for child in os.listdir(str(path)):
                inner = os.path.join(str(path), child)
                if child == "marker" and _read_marker(Path(inner)) is not None:
                    os.unlink(inner)
                elif child == "retired":
                    found = os.lstat(inner)
                    if stat.S_ISDIR(found.st_mode) and named == (found.st_dev, found.st_ino):
                        shutil.rmtree(inner, ignore_errors=True)
            os.rmdir(str(path))  # only succeeds when empty
        except OSError:
            continue


def _clear_stale_clone(clone: Path) -> str:
    """Remove a clone left at the fixed path by a killed launch. Refuses anything that is not a plain directory the
    dispatcher created (its sibling marker names this directory): an operator's checkout is never deleted."""
    _recover_staging(clone.parent)
    try:
        info = os.lstat(str(clone))
    except FileNotFoundError:
        return _drop_marker(clone)  # a marker whose directory is gone; an unrecognized file there is refused
    except OSError:
        return "CLONE_PATH_INVALID"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or not _owns_clone(clone):
        return "CLONE_PATH_INVALID"
    return _release_clone(clone) or ("CLONE_PATH_STALE" if os.path.lexists(str(clone)) else "")


# ── Claude ───────────────────────────────────────────────────────────────────


class ClaudeBackend:
    name = "claude"

    def __init__(
        self, launch_timeout_s: Optional[float] = None, git_timeout_s: Optional[float] = None,
        pty_grace_s: Optional[float] = None,
    ) -> None:
        self.launch_timeout_s = _timeout_from_env(LAUNCH_TIMEOUT_S) if launch_timeout_s is None else launch_timeout_s
        self.pty_grace_s = _grace_from_env() if pty_grace_s is None else pty_grace_s
        self._git_quick = GIT_QUICK_TIMEOUT_S if git_timeout_s is None else git_timeout_s
        self._git_transfer = GIT_TRANSFER_TIMEOUT_S if git_timeout_s is None else git_timeout_s

    def available(self) -> Tuple[bool, str]:
        reason = _api_key_reason(self.name)
        if reason:
            return False, reason
        if resolve_binary(self.name) is None:
            return False, "BIN_NOT_FOUND: claude"
        return True, "ok"

    # launch -------------------------------------------------------------

    def _precheck(self, ctx: LaunchCtx) -> str:
        checks = (
            (not _branch_ok(normalize_branch(ctx.task_branch)), "TASK_BRANCH_INVALID"),
            (not SAFE_URL_RE.match(ctx.clone_url or ""), "CLONE_URL_INVALID"),
            (not OID_RE.match((ctx.task_branch_oid or "").lower()), "BRANCH_OID_MISSING"),
            (ctx.refs_snapshot_path is None, "SNAPSHOT_PATH_MISSING"),
            (bool(ctx.model) and not SAFE_ARG_RE.match(ctx.model or ""), "MODEL_INVALID"),
            (bool(ctx.effort) and not SAFE_ARG_RE.match(ctx.effort or ""), "EFFORT_INVALID"),
            (_safe_prompt(ctx.prompt) is None, "PROMPT_INVALID"),
            (_prompt_ambiguous(ctx.prompt or ""), "PROMPT_AMBIGUOUS"),
        )
        for failed, code in checks:
            if failed:
                return code
        if _api_key_reason(self.name):
            return "API_KEY_BACKEND"
        return "" if resolve_binary(self.name) else "BIN_NOT_FOUND"

    def launch(self, ctx: LaunchCtx) -> LaunchResult:
        problem = self._precheck(ctx)
        if problem:
            return _failed(problem)
        raw_parent = os.environ.get(CLONE_PARENT_ENV, "").strip()
        if raw_parent:
            return self._launch_in_fixed_clone(ctx, raw_parent)
        try:
            workdir = tempfile.mkdtemp(prefix="ffs-cloud-clone-")
        except OSError:
            return _failed("CLONE_DIR_FAILED")  # nothing was started
        try:
            return self._launch_from_clone(ctx, Path(workdir) / "clone")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _launch_in_fixed_clone(self, ctx: LaunchCtx, raw_parent: str) -> LaunchResult:
        """The client trusts a folder per git root, so a fresh temp clone always shows the trust dialog. With
        ``FFS_CLOUD_CLONE_PARENT`` the clone goes to ``<parent>/ffs-cloud-launch-claude``, a path the operator can
        trust once by running the client there. Never falls back to the system temp dir."""
        parent, problem = _validated_clone_parent(raw_parent, ctx.repo_root)
        if parent is None:
            return _failed("CLONE_PARENT_INVALID " + problem)
        lock_fd, problem = _lock_clone_parent(parent)
        if lock_fd is None:
            return _failed(problem)
        clone = parent / CLONE_DIR_NAME
        owned = False
        try:
            problem = _clone_child_conflict(clone, ctx.repo_root) or _clear_stale_clone(clone)
            if problem:
                return _failed(problem)
            problem = _claim_clone(clone)  # private mkdir, marker, then a no-replace rename, before git runs
            if problem:
                return _failed(problem)
            owned = True
            return self._launch_from_clone(ctx, clone)
        finally:
            if owned:
                _release_clone(clone)  # every exit path, including an exception; never a directory it did not create
            os.close(lock_fd)  # releases the lock

    def _prepare_clone(self, ctx: LaunchCtx, clone: Path) -> str:
        branch = normalize_branch(ctx.task_branch)
        no_hooks = ["-c", "core.hooksPath=" + os.devnull]
        cloned = run_git(
            no_hooks + ["clone", "--no-checkout", "--single-branch", "--branch", branch, "--no-tags", ctx.clone_url,
                        str(clone)],
            timeout=self._git_transfer,
        )
        if not cloned.ok:
            return "CLONE_FAILED"
        head = run_git(["-C", str(clone), "rev-parse", "HEAD"], timeout=self._git_quick)
        if not head.ok or head.text().strip().lower() != ctx.task_branch_oid.lower():
            return "BRANCH_OID_MISMATCH"  # nothing was checked out: the replacement tree was never materialised
        oid = ctx.task_branch_oid.lower()
        checkout = run_git(
            no_hooks + ["-C", str(clone), "checkout", "--quiet", "--force", "-B", branch, oid],
            timeout=self._git_quick, extra_env=NEUTRAL_CHECKOUT_ENV,
        )
        return "" if checkout.ok else "CLONE_FAILED"

    def _write_launch_snapshot(self, ctx: LaunchCtx) -> str:
        listing = run_git(["ls-remote", ctx.clone_url, CLAUDE_REF_GLOB], timeout=self._git_quick)
        if not listing.ok:
            return "SNAPSHOT_FAILED"
        try:
            clean, flagged = split_ls_remote(listing.text())
            clean.update({key: oid for key, (_, oid) in flagged.items()})  # a flagged name is stored as a hash only
            write_snapshot(Path(ctx.refs_snapshot_path), clean)  # type: ignore[arg-type]
        except cloud_scan.ScanRefused:
            return "SNAPSHOT_FAILED"  # nothing was started; a hash-only snapshot would make old refs look new
        except OSError:
            return "SNAPSHOT_WRITE_FAILED"
        return ""

    def _launch_from_clone(self, ctx: LaunchCtx, clone: Path) -> LaunchResult:
        for step in (lambda: self._prepare_clone(ctx, clone), lambda: self._write_launch_snapshot(ctx)):
            problem = step()
            if problem:
                return _failed(problem)
        argv = [resolve_binary(self.name) or "", "--cloud", _safe_prompt(ctx.prompt) or ""]
        if ctx.model:
            argv += ["--model", ctx.model]
        if ctx.effort:
            argv += ["--effort", ctx.effort]
        return self._launch_on_pty(ctx, argv, clone)

    def _launch_on_pty(self, ctx: LaunchCtx, argv: List[str], clone: Path) -> LaunchResult:
        sink, tail = bytearray(), TailWindow()
        clean, log_detail = SANITIZER_FAILED_NOTICE, "SANITIZER_FAILED"
        try:
            pty_result = run_pty(
                argv, cwd=clone, env=child_env(), timeout=self.launch_timeout_s, sink=sink,
                grace_s=self.pty_grace_s, done=_session_seen, cap=CAPTURE_CAP_BYTES, tail=tail)
        finally:  # the transcript (its start AND its end) is persisted on every path, including an exception or a timeout
            clean, log_detail = _sanitize_bytes(ctx, strip_terminal(join_head_tail(bytes(sink), tail)))
            _, log_detail = _write_launch_log(ctx, clean, log_detail)
        if pty_result.spawn_failed:
            return _failed("SPAWN_FAILED", SPAWN_FAILED_RC)
        return self._finish_pty_launch(clean, pty_result, log_detail)

    @staticmethod
    def _finish_pty_launch(clean: bytes, pty_result: PtyResult, log_detail: str) -> LaunchResult:
        # ids and urls are parsed from the sanitized transcript only, never from raw output
        text = clean.decode("utf-8", errors="replace")
        remote_id, url = parse_claude_pty_output(text)
        exited = pty_result.exited_early
        if pty_result.matched or remote_id or url:  # an id/url in the full transcript counts even if the live scan missed it
            return LaunchResult(0, remote_id, url, "launched", log_detail)
        if exited and NOT_A_TTY_TEXT in " ".join(text.lower().split()):  # only an exited client has said its last word
            return LaunchResult(pty_result.rc or 1, None, None, "failed", ("CLI_EXIT NOT_A_TTY " + log_detail).strip())
        if _blocking_dialog(text):
            rc = (pty_result.rc or 1) if exited else TIMEOUT_RC
            return LaunchResult(rc, None, None, "failed", ("CLI_EXIT BLOCKED_DIALOG " + log_detail).strip())
        if exited:
            # The client ended on its own, printed no id/url we recognise and no known pre-creation error. It may still
            # have created the session, and Claude binds by nonce on claude/*: keep polling, never fail or resubmit.
            return LaunchResult(
                pty_result.rc or 0, None, None, "launched", ("CLI_EXIT NO_SESSION_URL " + log_detail).strip(),
                note=LAUNCH_NOTE_EXIT_NO_URL)
        # Still running at the launch timeout with no URL: same reasoning. The note records why the URL is missing.
        return LaunchResult(TIMEOUT_RC, None, None, "launched", ("LAUNCH_TIMEOUT " + log_detail).strip(), note=LAUNCH_NOTE_NO_URL)

    # poll ---------------------------------------------------------------

    def poll(self, ctx: PollCtx) -> PollResult:
        if not SAFE_URL_RE.match(ctx.clone_url or ""):
            return PollResult("unknown", [], "CLONE_URL_INVALID")
        listing = run_git(["ls-remote", ctx.clone_url, CLAUDE_REF_GLOB], timeout=self._git_quick)
        if not listing.ok:
            return PollResult("unknown", [], "LS_REMOTE_FAILED")
        snapshot = load_refs_snapshot(ctx.refs_snapshot_path)
        try:
            clean, flagged = split_ls_remote(listing.text())
        except cloud_scan.ScanRefused:
            return PollResult("unknown", [], "REF_SCAN_REFUSED")  # a scanner fault: retry next tick, park nothing
        candidates, capped = select_candidates(clean, snapshot, ctx.nonce, ctx.rejected, run=ctx.run, task=ctx.task)
        notes = ["candidates=%d" % len(candidates)]
        prefix = ctx.nonce[:NONCE_PREFIX_LEN] if len(ctx.nonce) >= NONCE_PREFIX_LEN else ""
        if any((prefix and prefix in ref) or (snapshot is not None and snapshot.get(key) != oid)
               for key, (ref, oid) in flagged.items()):
            notes.append(REF_NAME_SECRET_NOTE)  # a new ref whose name matched the scanner: never a candidate
        if snapshot is None:
            notes.append("NO_SNAPSHOT")
        if capped:
            notes.append("CAPPED_AT_%d" % MAX_CANDIDATES)
        return PollResult("running", candidates, " ".join(notes))

    # fetch --------------------------------------------------------------

    def fetch(self, ctx: PollCtx, candidate: Tuple[str, str], quarantine_repo: Path) -> str:
        ref, oid = candidate
        if not CLAUDE_REF_RE.match(ref or "") or not OID_RE.match(oid or ""):
            raise FetchError("CANDIDATE_INVALID")
        if not SAFE_URL_RE.match(ctx.clone_url or ""):
            raise FetchError("CLONE_URL_INVALID")
        _quarantine_check(quarantine_repo, self._git_quick)
        try:
            fetched = run_git(
                ["-C", str(quarantine_repo), "fetch", "--no-tags", BLOB_LIMIT_FILTER, ctx.clone_url, oid],
                timeout=self._git_transfer,
            )
        finally:
            _scrub_promisor_remote(quarantine_repo, self._git_quick)
        if not fetched.ok:
            raise FetchError("FETCH_FAILED")
        present = run_git(["-C", str(quarantine_repo), "cat-file", "-e", oid + "^{commit}"], timeout=self._git_quick)
        if not present.ok:
            raise FetchError("FETCH_OID_MISSING")
        return oid


# ── Codex ────────────────────────────────────────────────────────────────────


class CodexBackend:
    name = "codex"

    def __init__(self, launch_timeout_s: Optional[float] = None, git_timeout_s: Optional[float] = None) -> None:
        self.launch_timeout_s = _timeout_from_env(LAUNCH_TIMEOUT_S) if launch_timeout_s is None else launch_timeout_s
        self._git_quick = GIT_QUICK_TIMEOUT_S if git_timeout_s is None else git_timeout_s

    def available(self) -> Tuple[bool, str]:
        reason = _api_key_reason(self.name)
        if reason:
            return False, reason
        if resolve_binary(self.name) is None:
            return False, "BIN_NOT_FOUND: codex"
        return True, "ok"

    def launch(self, ctx: LaunchCtx) -> LaunchResult:
        branch = normalize_branch(ctx.task_branch)
        prompt = _safe_prompt(ctx.prompt)
        binary = resolve_binary(self.name)
        problems = (
            (not _branch_ok(branch), "TASK_BRANCH_INVALID"),
            (not ctx.env_id or not SAFE_ARG_RE.match(ctx.env_id), "ENV_ID_MISSING"),
            (prompt is None, "PROMPT_INVALID"),
            (_api_key_reason(self.name) is not None, "API_KEY_BACKEND"),
            (binary is None, "BIN_NOT_FOUND"),
        )
        for failed, code in problems:
            if failed:
                return _failed(code)
        argv = [binary or "", "cloud", "exec", "--env", ctx.env_id or "", "--branch", branch, prompt or ""]
        workdir = tempfile.mkdtemp(prefix="ffs-cloud-codex-")
        try:
            result = run_capture(argv, cwd=Path(workdir), env=child_env(), timeout=self.launch_timeout_s)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return self._finish_launch(ctx, result)

    def _finish_launch(self, ctx: LaunchCtx, result: ExecResult) -> LaunchResult:
        text_bytes, log_detail = sanitize_and_log(ctx, result.output)
        remote_id, url = parse_codex_launch(text_bytes.decode("utf-8", errors="replace"))
        early = _launch_outcome_for(result)
        if early is not None:
            early.remote_id, early.url = remote_id, url
            early.detail = (early.detail + " " + log_detail).strip()
            return early
        if result.rc != 0:
            return LaunchResult(result.rc, None, None, "failed", ("CLI_EXIT " + log_detail).strip())
        if remote_id is None:
            return LaunchResult(0, None, url, "uncertain", ("NO_REMOTE_ID " + log_detail).strip())
        return LaunchResult(0, remote_id, url, "launched", log_detail)

    def poll(self, ctx: PollCtx) -> PollResult:
        remote_id = _valid_id(ctx.remote_id)
        binary = resolve_binary(self.name)
        if remote_id is None:
            return PollResult("unknown", [], "NO_REMOTE_ID")
        if binary is None:
            return PollResult("unknown", [], "BIN_NOT_FOUND")
        result = run_capture(
            [binary, "cloud", "status", remote_id], cwd=None, env=child_env(), timeout=STATUS_TIMEOUT_S
        )
        if not result.ok:
            return PollResult("unknown", [], "STATUS_CMD_FAILED")
        state, detail = parse_codex_status(result.text(), expected_id=remote_id)
        return PollResult(state, [], detail)

    def fetch(self, ctx: PollCtx, candidate: Tuple[str, str], quarantine_repo: Path) -> str:
        """Apply the task diff on top of ``ctx.base_oid`` in quarantine; return the new commit OID.

        ``candidate`` is ignored: Codex results have no ref or OID before the
        diff is applied, so ``poll`` reports no candidates and the result is
        addressed by ``ctx.remote_id``.
        """
        remote_id = _valid_id(ctx.remote_id)
        binary = resolve_binary(self.name)
        if remote_id is None or binary is None or not OID_RE.match((ctx.base_oid or "").lower()):
            raise FetchError("FETCH_INPUT_INVALID")
        if not (IDENT_RE.match(ctx.run) and IDENT_RE.match(ctx.task)):
            raise FetchError("FETCH_INPUT_INVALID")
        _quarantine_check(quarantine_repo, self._git_quick)
        self._require_clean_base(quarantine_repo, ctx.base_oid)
        diff = self._download_diff(binary, remote_id)
        return self._apply_and_commit(ctx, quarantine_repo, diff)

    def _require_clean_base(self, repo: Path, base_oid: str) -> None:
        head = run_git(["-C", str(repo), "rev-parse", "HEAD"], timeout=self._git_quick)
        if not head.ok or head.text().strip().lower() != base_oid.lower():
            raise FetchError("QUARANTINE_NOT_AT_BASE")
        status = run_git(["-C", str(repo), "status", "--porcelain"], timeout=self._git_quick)
        if not status.ok or status.output.strip():
            raise FetchError("QUARANTINE_DIRTY")

    def _download_diff(self, binary: str, remote_id: str) -> bytes:
        result = run_capture(
            [binary, "cloud", "diff", remote_id],
            cwd=None,
            env=child_env(),
            timeout=STATUS_TIMEOUT_S,
            cap=DIFF_CAP_BYTES,
        )
        if not result.ok:
            raise FetchError("DIFF_DOWNLOAD_FAILED")
        if result.truncated:
            raise FetchError("DIFF_TOO_LARGE")
        if not result.output.strip():
            raise FetchError("DIFF_EMPTY")
        return result.output

    def _apply_and_commit(self, ctx: PollCtx, repo: Path, diff: bytes) -> str:
        before = _tree_bytes(repo)
        applied = run_git(["-C", str(repo), "apply", "--index", "-"], stdin_bytes=diff, timeout=self._git_quick,
                          file_size_limit=APPLIED_FILE_CAP_BYTES)
        if applied.rc == -signal.SIGXFSZ:
            raise FetchError("DIFF_TOO_LARGE")  # a binary literal inflated past the per-file cap
        if _tree_bytes(repo) - before > APPLIED_GROWTH_CAP_BYTES:
            raise FetchError("DIFF_TOO_LARGE")  # the applied patch grew the quarantine past its cap
        if not applied.ok:
            raise FetchError("DIFF_APPLY_FAILED")
        identity = {
            "GIT_AUTHOR_NAME": COMMIT_NAME,
            "GIT_AUTHOR_EMAIL": COMMIT_EMAIL,
            "GIT_COMMITTER_NAME": COMMIT_NAME,
            "GIT_COMMITTER_EMAIL": COMMIT_EMAIL,
        }
        message = "ffs cloud result %s/%s g%d" % (ctx.run, ctx.task, ctx.generation)
        committed = run_git(
            [
                "-C", str(repo), "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                "commit", "--no-verify", "--no-gpg-sign", "-q", "-m", message,
            ],
            extra_env=identity,
            timeout=self._git_quick,
        )
        if not committed.ok:
            raise FetchError("COMMIT_FAILED")
        head = run_git(["-C", str(repo), "rev-parse", "HEAD"], timeout=self._git_quick)
        oid = head.text().strip()
        if not head.ok or not OID_RE.match(oid):
            raise FetchError("COMMIT_FAILED")
        return oid


# ── Factory ──────────────────────────────────────────────────────────────────

#: Backend names that are known to mean "use an API key"; everything else that is not
#: ``claude`` or ``codex`` is simply unknown.
_API_KEY_BACKEND_NAMES = frozenset(["anthropic-api", "openai-api", "api"])


def get_backend(name: str) -> Backend:
    """``"claude"`` or ``"codex"``. ``anthropic-api``/``openai-api``/``api`` raise
    ``API_KEY_BACKEND``; any other name raises ``UNKNOWN_BACKEND``."""
    normalized = (name or "").strip().lower()
    if normalized == "claude":
        return ClaudeBackend()
    if normalized == "codex":
        return CodexBackend()
    if normalized in _API_KEY_BACKEND_NAMES:
        raise UnsupportedBackend("API_KEY_BACKEND")
    raise UnsupportedBackend("UNKNOWN_BACKEND")
