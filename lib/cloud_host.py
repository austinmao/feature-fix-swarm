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

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

# ── Constants ────────────────────────────────────────────────────────────────

LAUNCH_TIMEOUT_S = 180.0
GIT_QUICK_TIMEOUT_S = 60.0
GIT_TRANSFER_TIMEOUT_S = 300.0
STATUS_TIMEOUT_S = 60.0
LAUNCH_TIMEOUT_ENV = "FFS_CLOUD_LAUNCH_TIMEOUT_S"

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


@dataclass
class LaunchResult:
    rc: int
    remote_id: Optional[str]
    url: Optional[str]
    outcome: str  # "launched" | "uncertain" | "failed"
    detail: str = ""


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


def parse_ls_remote(text: str) -> Dict[str, str]:
    """``git ls-remote`` output to ``{ref: oid}``, keeping only well-formed claude/* refs."""
    refs: Dict[str, str] = {}
    for line in text.splitlines():
        oid, _, ref = line.partition("\t")
        if OID_RE.match(oid) and CLAUDE_REF_RE.match(ref) and ".." not in ref:
            refs[ref] = oid
    return refs


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


def sanitize_and_log(ctx: LaunchCtx, raw: bytes) -> Tuple[bytes, str]:
    """Sanitize, cap to 64 KB, write 0600 to ``ctx.log_path``.

    Returns ``(sanitized_capped_bytes, detail)``. ``detail`` is "" on success.
    Sanitization happens before truncation, and nothing unsanitized is written.
    """
    detail = ""
    try:
        clean = ctx.sanitize(raw)
        if not isinstance(clean, bytes):
            raise TypeError("sanitize must return bytes")
    except Exception:  # noqa: BLE001 - any hook failure must withhold the output
        clean = SANITIZER_FAILED_NOTICE
        detail = "SANITIZER_FAILED"
    capped = clean[:LOG_CAP_BYTES]
    try:
        write_private_file(Path(ctx.log_path), capped)
    except OSError:
        detail = (detail + " LOG_WRITE_FAILED").strip()
    return capped, detail


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


# ── Claude ───────────────────────────────────────────────────────────────────


class ClaudeBackend:
    name = "claude"

    def __init__(self, launch_timeout_s: Optional[float] = None, git_timeout_s: Optional[float] = None) -> None:
        self.launch_timeout_s = _timeout_from_env(LAUNCH_TIMEOUT_S) if launch_timeout_s is None else launch_timeout_s
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
        workdir = tempfile.mkdtemp(prefix="ffs-cloud-clone-")
        try:
            return self._launch_from_clone(ctx, Path(workdir) / "clone")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _prepare_clone(self, ctx: LaunchCtx, clone: Path) -> str:
        branch = normalize_branch(ctx.task_branch)
        cloned = run_git(
            ["clone", "--single-branch", "--branch", branch, "--no-tags", ctx.clone_url, str(clone)],
            timeout=self._git_transfer,
        )
        if not cloned.ok:
            return "CLONE_FAILED"
        head = run_git(["-C", str(clone), "rev-parse", "HEAD"], timeout=self._git_quick)
        if not head.ok or head.text().strip().lower() != ctx.task_branch_oid.lower():
            return "BRANCH_OID_MISMATCH"
        return ""

    def _write_launch_snapshot(self, ctx: LaunchCtx) -> str:
        listing = run_git(["ls-remote", ctx.clone_url, CLAUDE_REF_GLOB], timeout=self._git_quick)
        if not listing.ok:
            return "SNAPSHOT_FAILED"
        try:
            write_snapshot(Path(ctx.refs_snapshot_path), parse_ls_remote(listing.text()))  # type: ignore[arg-type]
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
        result = run_capture(argv, cwd=clone, env=child_env(), timeout=self.launch_timeout_s)
        return self._finish_launch(ctx, result)

    def _finish_launch(self, ctx: LaunchCtx, result: ExecResult) -> LaunchResult:
        text_bytes, log_detail = sanitize_and_log(ctx, result.output)
        remote_id, url = parse_claude_launch(text_bytes.decode("utf-8", errors="replace"))
        early = _launch_outcome_for(result)
        if early is not None:
            early.remote_id, early.url = remote_id, url
            early.detail = (early.detail + " " + log_detail).strip()
            return early
        if result.rc != 0:
            return LaunchResult(result.rc, None, None, "failed", ("CLI_EXIT " + log_detail).strip())
        # Claude binds by nonce, so a missing id/url is still "launched".
        return LaunchResult(0, remote_id, url, "launched", log_detail)

    # poll ---------------------------------------------------------------

    def poll(self, ctx: PollCtx) -> PollResult:
        if not SAFE_URL_RE.match(ctx.clone_url or ""):
            return PollResult("unknown", [], "CLONE_URL_INVALID")
        listing = run_git(["ls-remote", ctx.clone_url, CLAUDE_REF_GLOB], timeout=self._git_quick)
        if not listing.ok:
            return PollResult("unknown", [], "LS_REMOTE_FAILED")
        snapshot = load_refs_snapshot(ctx.refs_snapshot_path)
        candidates, capped = select_candidates(
            parse_ls_remote(listing.text()), snapshot, ctx.nonce, ctx.rejected, run=ctx.run, task=ctx.task)
        notes = ["candidates=%d" % len(candidates)]
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
