#!/usr/bin/env python3
"""macOS ``sandbox-exec`` verification runner (contract 8.2 as amended by 14.7).

Materializes a git tree (no archive, no filters), runs the verify command in a
sandbox with no network, no Mach services beyond one directory lookup, no
Launch Services, no signals to processes outside the sandbox and no reads of
home, other worktrees, the state directory or the operator temp tree, and
returns its exit code. Stdlib only, Python 3.9 compatible.

Profile changes against the contract text, found by running real sandbox-exec:
see ``build_profile``. ``(allow default)`` is kept as the base so ordinary
test runners start, but every IPC class that reaches outside the sandbox is
denied: ``mach-lookup`` (only ``com.apple.system.opendirectoryd.libinfo`` is
re-allowed, needed for ``getpwuid`` in python, node and git), ``lsopen``,
``signal`` and ``process-info*`` (same-sandbox only), ``mach-register``,
``iokit-open``, ``system-socket``, ``appleevent-send`` and ``job-creation``.
A rule this macOS rejects makes the profile fail validation, which raises
``SandboxUnavailable`` (fail closed) instead of silently dropping the rule.

Known limit: the cleanup sweep kills only processes proven to belong to the run
(the launched process group or an observed descendant, same pid and start
time). A double-fork escapee that no process-table poll saw while its parent
chain was alive is not swept; argv or working directory never selects a victim.
"""

from __future__ import annotations

import fnmatch
import os
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set

import cloud_scan

__all__ = [
    "SandboxUnavailable",
    "SandboxError",
    "build_profile",
    "resolve_toolchain_roots",
    "materialize_tree",
    "run_verify",
    "sandbox_available",
]

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
BASH = "/bin/bash"
PS = "/bin/ps"
KILL_GRACE_S = 2.0
PREFLIGHT_TIMEOUT_S = 20
GIT_TIMEOUT_S = 120
POLL_INTERVAL_S = 0.25
MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_BLOB_BYTES = 512 * 1024 * 1024
RC_TIMEOUT = 124
RC_SIGNAL_BASE = 128
MODE_FILE = "100644"
MODE_EXEC = "100755"
MODE_SYMLINK = "120000"
MODE_GITLINK = "160000"
# Default child limits (see ``run_verify``). RLIMIT_NPROC is per uid on macOS,
# so the limit is the operator's current process count plus this headroom.
DEFAULT_NPROC_HEADROOM = 512
DEFAULT_FSIZE_BYTES = 512 * 1024 * 1024
DEFAULT_NOFILE = 4096
# Directly under HOME: nothing inside, and nothing containing one of these, is
# ever re-opened for reading.
SECRET_HOME_PATHS = (
    ".ssh",
    ".aws",
    ".config",
    ".codex",
    ".claude",
    ".doppler",
    ".gnupg",
    ".netrc",
    ".docker",
    ".kube",
    ".npmrc",
    ".pypirc",
    ".orbstack",
    ".git-credentials",
    ".gitconfig",
    ".pgpass",
    ".azure",
    ".vercel",
    ".railway",
    ".wrangler",
    ".cargo/credentials",
    ".cargo/credentials.toml",
    "Library",
)
# Never granted as a whole (a root may sit below them, never contain them).
HOME_CONTAINER_PATHS = (".local/share", ".cache")
TMP_AREAS = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")
MACH_LOOKUP_ALLOW = ("com.apple.system.opendirectoryd.libinfo",)
# Known toolchain layouts below HOME: ``*`` matches one path component and the
# granted root is exactly the matched prefix.
TOOLCHAIN_LAYOUTS = (
    (".nvm", "versions", "node", "*"),
    (".nodenv", "versions", "*"),
    (".pyenv", "versions", "*"),
    (".rbenv", "versions", "*"),
    (".rustup", "toolchains", "*"),
    (".asdf", "installs", "*", "*"),
    (".volta", "tools", "image", "*", "*"),
    (".fnm", "node-versions", "*", "installation"),
    (".sdkman", "candidates", "*", "*"),
    (".local", "share", "uv", "tools", "*"),
    (".local", "share", "uv", "python", "*"),
    (".local", "share", "pipx", "venvs", "*"),
    (".local", "share", "mise", "installs", "*", "*"),
)
MAX_FARM_ENTRIES = 4096
STATE_SUBDIR = (".feature-fix-swarm", "cloud")
SECRET_DEP_NAMES = (".git", ".feature-fix-swarm", ".ssh", ".aws", ".gnupg", ".netrc", ".npmrc", ".pypirc")
LOG_WITHHELD = b"[log withheld: scanner refused]\n"
LOG_TRUNCATED = b"\n[log truncated]\n"


class SandboxUnavailable(Exception):
    """sandbox-exec cannot be used here (not macOS, missing, or nested)."""


class SandboxError(Exception):
    """The tree could not be materialized safely."""


# --------------------------------------------------------------------------
# Paths and profile
# --------------------------------------------------------------------------


def _real(path: str) -> str:
    return os.path.realpath(path)


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _quote(path: str) -> str:
    if "\n" in path or "\0" in path or not path.startswith("/"):
        raise SandboxError("unusable path in sandbox profile")
    return '"%s"' % path.replace("\\", "\\\\").replace('"', '\\"')


def _subpaths(paths: Iterable[str]) -> str:
    return " ".join("(subpath %s)" % _quote(p) for p in paths)


def _ancestors(paths: Iterable[str]) -> List[str]:
    seen: List[str] = []
    for path in paths:
        current = os.path.dirname(path)
        while True:
            if current not in seen:
                seen.append(current)
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    return seen


def _dedupe(items: Iterable[str]) -> List[str]:
    out: List[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def _hardening_rules() -> List[str]:
    """The IPC-class rules shared by every profile and the preflight."""
    allow = " ".join('(global-name "%s")' % name for name in MACH_LOOKUP_ALLOW)
    return [
        "(deny mach-lookup)",
        "(allow mach-lookup %s)" % allow,
        "(deny mach-register)",
        "(deny lsopen)",
        "(deny appleevent-send)",
        "(deny iokit-open)",
        "(deny system-socket)",
        "(deny job-creation)",
        "(deny signal)",
        "(allow signal (target same-sandbox))",
        "(deny process-info*)",
        "(allow process-info* (target same-sandbox))",
    ]


class _Zones:
    """Areas a granted root must never contain or sit inside."""

    def __init__(self, home: str, main: str, others: Sequence[str], state: str, tmp_areas: Sequence[str]):
        self.home = home
        self.main = main
        self.others = list(others)
        self.state = state
        self.secrets = [os.path.join(home, rel) for rel in SECRET_HOME_PATHS]
        self.containers = [os.path.join(home, rel) for rel in HOME_CONTAINER_PATHS]
        self.tmp_areas = list(tmp_areas)

    def hard(self) -> List[str]:
        """Denied for reading no matter which allow rule precedes the deny."""
        return _dedupe(self.secrets + self.others + [self.state])

    def violation(self, root: str, *, inside_main_ok: bool) -> Optional[str]:
        """Why ``root`` may not be granted, or ``None``."""
        must_not_contain = [self.home, self.main, self.state, "/Volumes"]
        must_not_contain += self.others + self.secrets + self.containers + self.tmp_areas
        for zone in must_not_contain:
            if _under(zone, root):
                return "root contains a denied area"
        inside = self.secrets + self.others + [self.state]
        if not inside_main_ok:
            inside.append(self.main)
        for zone in inside:
            if _under(root, zone):
                return "root is inside a denied area"
        return None


def _zones_for(
    home: str, main_worktree: str, state_dir: str, other_worktrees: Sequence[str], tmp_dir: Optional[str]
) -> _Zones:
    tmp_areas = [_real(p) for p in TMP_AREAS]
    if tmp_dir is not None:
        base = os.path.dirname(_real(tmp_dir))
        if base not in ("/", os.path.dirname(base)):
            tmp_areas.append(base)
    return _Zones(
        _real(home), _real(main_worktree), [_real(p) for p in other_worktrees], _real(state_dir),
        _dedupe(tmp_areas),
    )


def build_profile(
    *,
    work_dir: str,
    tmp_dir: str,
    home: str,
    main_worktree: str,
    state_dir: str,
    other_worktrees: Sequence[str] = (),
    dep_roots: Sequence[str] = (),
    toolchain_roots: Sequence[str] = (),
    read_roots: Sequence[str] = (),
) -> str:
    """Render the SBPL profile of contract 14.7 (hardened).

    Later rules win. Order: IPC hardening, network and write denies, the read
    deny list (home, ``/Volumes``, every worktree, the state dir, the operator
    temp tree), the read allows for the work dir, this run's temp dir and the
    validated roots, and finally the deny list is emitted AGAIN for every
    credential path, other worktree and the state dir so no allow can re-open
    them. Every path is passed through ``realpath`` because sandbox-exec
    matches resolved paths. A dependency, toolchain or read root that equals
    or contains the main worktree, home or any denied area raises
    ``SandboxError``.
    """
    work, tmp = _real(work_dir), _real(tmp_dir)
    zones = _zones_for(home, main_worktree, state_dir, other_worktrees, tmp)
    deps = [_real(p) for p in dep_roots]
    tools = [_real(p) for p in toolchain_roots]
    extra = [_real(p) for p in read_roots]
    for root in deps + extra:
        if zones.violation(root, inside_main_ok=True) is not None:
            raise SandboxError("read root overlaps a denied area")
    for root in tools:
        if zones.violation(root, inside_main_ok=False) is not None:
            raise SandboxError("toolchain root overlaps a denied area")
    denied = _dedupe(
        [zones.home, "/Volumes", zones.main] + zones.others + [zones.state] + zones.secrets + zones.tmp_areas
    )
    allowed = _dedupe([work, tmp] + deps + tools + extra)
    ancestors = _dedupe(_ancestors(allowed + denied))
    lines = ["(version 1)", "(allow default)", "(deny network*)"]
    lines += _hardening_rules()
    lines += [
        '(deny file-write* (subpath "/"))',
        "(allow file-write* (subpath %s) (subpath %s) (literal \"/dev/null\") "
        '(literal "/dev/dtracehelper") (subpath "/dev/fd"))' % (_quote(work), _quote(tmp)),
        "(deny file-read* %s)" % _subpaths(denied),
        "(allow file-read* %s)" % _subpaths(allowed),
        "(allow file-read-metadata %s)"
        % " ".join("(literal %s)" % _quote(a) for a in ancestors),
        "(deny file-read* %s)" % _subpaths(zones.hard()),
        '(deny file-read* (regex #"^/dev/tty"))',  # operator terminals: no keystroke theft
        "(deny file-read* %s)" % _subpaths([zones.state]),
    ]
    return "\n".join(lines) + "\n"


def _layout_root(path: str, home_real: str) -> Optional[str]:
    """The toolchain-layout root containing ``path``, if its layout is known."""
    if not _under(path, home_real) or path == home_real:
        return None
    parts = os.path.relpath(path, home_real).split(os.sep)
    for layout in TOOLCHAIN_LAYOUTS:
        if len(parts) >= len(layout) and all(fnmatch.fnmatchcase(p, l) for p, l in zip(parts, layout)):
            return os.path.join(home_real, *parts[: len(layout)])
    return None


def _farm_targets(directory: str) -> List[str]:
    """Real targets of symlinks directly inside ``directory`` (bounded)."""
    targets: List[str] = []
    try:
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_FARM_ENTRIES:
                    break
                if entry.is_symlink():
                    target = _real(entry.path)
                    if target not in targets:
                        targets.append(target)
    except OSError:
        pass
    return targets


def _rustup_toolchains(home_real: str) -> List[str]:
    base = os.path.join(home_real, ".rustup", "toolchains")
    try:
        return [os.path.join(base, name) for name in sorted(os.listdir(base))]
    except OSError:
        return []


def resolve_toolchain_roots(
    path_env: str,
    home: str,
    *,
    main_worktree: Optional[str] = None,
    other_worktrees: Sequence[str] = (),
    state_dir: Optional[str] = None,
) -> List[str]:
    """Readable roots for ``PATH`` entries that live under ``home``.

    A bin directory is granted on its own (never its parent). When it sits in
    a known toolchain layout (nvm ``versions/node/<v>``, pyenv, rustup,
    uv/pipx tool venvs, asdf, mise, volta ...) the layout root is granted
    instead, and when the directory is a symlink farm the targets' layout
    roots are granted too. ``~/.cargo/bin`` is granted alone, plus the
    rustup toolchain directories. A root that equals or contains home, a
    worktree, the state dir or a credential path (and any root inside a
    credential path) is dropped.
    """
    home_real = _real(home)
    zones = _Zones(
        home_real,
        _real(main_worktree) if main_worktree else os.path.join(home_real, ".ffs-no-main"),
        [_real(p) for p in other_worktrees],
        _real(state_dir) if state_dir else os.path.join(home_real, ".ffs-no-state"),
        [_real(p) for p in TMP_AREAS],
    )
    roots: List[str] = []

    def add(candidate: Optional[str]) -> None:
        if candidate and candidate not in roots and zones.violation(candidate, inside_main_ok=False) is None:
            roots.append(candidate)

    for entry in path_env.split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        resolved = _real(entry)
        if resolved == home_real or not _under(resolved, home_real):
            continue
        layout = _layout_root(resolved, home_real)
        add(layout or resolved)
        farms = [resolved] + ([os.path.join(layout, "bin")] if layout else [])
        for farm in farms:
            for target in _farm_targets(farm):
                found = _layout_root(target, home_real)
                add(found)
                if found:  # one more hop: a tool venv's python lives in its own layout
                    for inner in _farm_targets(os.path.join(found, "bin")):
                        add(_layout_root(inner, home_real))
        if resolved == os.path.join(home_real, ".cargo", "bin"):
            for toolchain in _rustup_toolchains(home_real):
                add(toolchain)
    return roots


def _filtered_path(
    path_env: str, home: str, toolchain_roots: Sequence[str], denied: Sequence[str] = ()
) -> str:
    """Keep PATH entries outside ``home`` and those under a granted root."""
    home_real = _real(home)
    areas = [_real(p) for p in TMP_AREAS] + [_real(p) for p in denied]
    kept: List[str] = []
    for entry in path_env.split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        resolved = _real(entry)
        if _under(resolved, "/Volumes"):
            continue
        granted = any(_under(resolved, r) for r in toolchain_roots)
        if not granted and (_under(resolved, home_real) or any(_under(resolved, area) for area in areas)):
            continue
        kept.append(entry)
    return os.pathsep.join(kept)


# --------------------------------------------------------------------------
# Materialization
# --------------------------------------------------------------------------


def _git(repo: str, args: Sequence[str]) -> bytes:
    try:
        proc = subprocess.run(
            ["git", "-C", repo] + list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=cloud_scan.git_env(),
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise SandboxError("git failed") from None
    if proc.returncode != 0:
        raise SandboxError("git failed")
    return proc.stdout


def _safe_relpath(raw: bytes) -> str:
    text = raw.decode("utf-8", "surrogateescape")
    parts = text.split("/")
    if not text or text.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise SandboxError("unsafe path in tree")
    if any(p.lower() == ".git" for p in parts) or "\0" in text:
        raise SandboxError("unsafe path in tree")
    return text


def _parse_tree(listing: bytes) -> List[tuple]:
    entries = []
    for record in listing.split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        fields = meta.split(b" ")
        if len(fields) != 3 or not path:
            raise SandboxError("unparsable tree entry")
        mode, kind, oid = (f.decode("ascii", "replace") for f in fields)
        if mode in (MODE_SYMLINK, MODE_GITLINK) or kind == "commit":
            raise SandboxError("symlink or gitlink in tree")
        if mode not in (MODE_FILE, MODE_EXEC) or kind != "blob":
            raise SandboxError("unsupported mode in tree")
        entries.append((mode, oid, _safe_relpath(path)))
    return entries


def materialize_tree(repo: str, tree_oid: str, dest: str) -> int:
    """Write ``tree_oid`` into ``dest``; returns the number of files.

    Uses ``git ls-tree -r -z`` and blob reads (``cat-file --batch`` returns
    raw blob bytes: no filters, no attributes, no smudge). The executable bit
    comes from the mode. Symlinks and gitlinks are refused, not skipped.
    """
    listing = _git(repo, ["ls-tree", "-r", "-z", "--full-tree", tree_oid])
    entries = _parse_tree(listing)
    os.makedirs(dest, exist_ok=True)
    dest_real = _real(dest)
    try:
        with cloud_scan.CatFile(repo) as cat:
            for mode, oid, rel in entries:
                got = cat.read(oid, MAX_BLOB_BYTES)
                if got is None or got[0] != "blob" or len(got[2]) != got[1]:
                    raise SandboxError("blob unavailable")
                _write_file(dest_real, rel, got[2], mode == MODE_EXEC)
    except cloud_scan.ScanRefused:
        raise SandboxError("git failed") from None
    return len(entries)


def _write_file(dest_real: str, rel: str, data: bytes, executable: bool) -> None:
    target = os.path.join(dest_real, rel)
    mode = 0o755 if executable else 0o644
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        fd = os.open(target, flags, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(target, mode)
    except OSError:
        raise SandboxError("cannot write tree entry") from None


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------

_PREFLIGHT: Dict[str, bool] = {}


def _run_under(rules: str, argv: Sequence[str]) -> Optional[int]:
    try:
        proc = subprocess.run(
            [SANDBOX_EXEC, "-p", rules] + list(argv),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PREFLIGHT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode


def _hardening_enforced() -> bool:
    """Run the IPC rules for real: they must be accepted AND must bite.

    A rule form this macOS accepts but ignores (``(target others)`` on signal
    is one) would otherwise be dropped silently.
    """
    rules = "(version 1)\n(allow default)\n" + "\n".join(_hardening_rules()) + "\n"
    if _run_under(rules, ["/usr/bin/true"]) != 0:
        return False
    if _run_under(rules, [BASH, "-c", "kill -0 %d" % os.getpid()]) in (0, None):
        return False
    if os.access("/usr/bin/pbpaste", os.X_OK):
        outside = _run_under("(version 1)(allow default)", ["/usr/bin/pbpaste"])
        if outside == 0 and _run_under(rules, ["/usr/bin/pbpaste"]) in (0, None):
            return False
    return True


def sandbox_available() -> bool:
    """True when sandbox-exec exists, applies a profile, and enforces the rules."""
    if sys.platform != "darwin" or not os.access(SANDBOX_EXEC, os.X_OK):
        return False
    if "ok" not in _PREFLIGHT:
        _PREFLIGHT["ok"] = _hardening_enforced()
    return _PREFLIGHT["ok"]


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------


def _secret_component(name: str) -> bool:
    lowered = name.lower()
    return lowered in SECRET_DEP_NAMES or lowered.startswith(".env")


def _link_deps(
    work: str,
    main_worktree: str,
    deps: Sequence[str],
    zones: Optional[_Zones] = None,
) -> List[str]:
    """Symlink existing dep dirs into ``work``; returns their resolved roots.

    A dep whose real path equals or contains the main worktree, home or a
    denied area, or that runs through a credential name, raises
    ``SandboxError``. Syntactically unsafe or missing deps are skipped.
    """
    roots: List[str] = []
    main_real = _real(main_worktree)
    for dep in deps:
        parts = dep.split("/")
        if not dep or dep.startswith("/") or any(p in ("", ".", "..") for p in parts):
            continue
        if any(_secret_component(p) for p in parts):
            raise SandboxError("dependency path names a secret location")
        source = _real(os.path.join(main_real, dep))
        if not os.path.exists(source):
            continue
        if zones is not None and zones.violation(source, inside_main_ok=True) is not None:
            raise SandboxError("dependency root overlaps a denied area")
        if source == main_real or _under(main_real, source):
            raise SandboxError("dependency root contains the main worktree")
        if not _under(source, main_real):
            continue
        if any(_secret_component(p) for p in os.path.relpath(source, main_real).split(os.sep)):
            raise SandboxError("dependency path names a secret location")
        link = os.path.join(work, dep)
        if os.path.lexists(link):
            continue
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(source, link)
        roots.append(source)
    return roots


def _child_env(tmp: str, path_value: str) -> Dict[str, str]:
    return {
        "PATH": path_value,
        "HOME": os.path.join(tmp, "home"),
        "TMPDIR": tmp,
        "LANG": os.environ.get("LANG", "C"),
        "LC_ALL": os.environ.get("LC_ALL", "C"),
        "CI": "1",
    }


# --------------------------------------------------------------------------
# Process tracking and cleanup
# --------------------------------------------------------------------------


class _Proc:
    __slots__ = ("pid", "ppid", "pgid", "sess", "start")

    def __init__(self, pid: int, ppid: int, pgid: int, sess: int, start: float):
        self.pid, self.ppid, self.pgid, self.sess, self.start = pid, ppid, pgid, sess, start


def _snapshot() -> List[_Proc]:
    """Every process of this host as ``_Proc`` (empty when ``ps`` fails)."""
    try:
        out = subprocess.run(
            [PS, "-axww", "-o", "pid=,ppid=,pgid=,sess=,lstart="],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15, check=False,
        ).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return []
    procs: List[_Proc] = []
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < 9:
            continue
        try:
            stamp = time.mktime(time.strptime(" ".join(fields[4:9]), "%a %b %d %H:%M:%S %Y"))
            procs.append(_Proc(int(fields[0]), int(fields[1]), int(fields[2]), int(fields[3]), stamp))
        except ValueError:
            continue
    return procs


def _victims(procs: Sequence[_Proc], known: Dict[int, float], root_pid: int, started: float) -> Set[int]:
    """Processes proven to belong to the run: the launched root or an observed
    descendant (same pid and start time; the root's start time is recorded when
    the tracker is built, so a recycled root pid is never owned), members of the
    group or session of one of those that is still alive, and their descendants.
    Argv and working directory never select a victim."""
    me = os.getpid()
    owned = {p.pid for p in procs if known.get(p.pid) == p.start}
    victims = {
        p.pid for p in procs
        if p.pid not in (me, 0, 1) and p.start >= started - 2
        and (p.pid in owned or p.pgid in owned or p.sess in owned)
    }
    changed = True
    while changed:
        changed = False
        for proc in procs:
            if proc.pid not in victims and proc.ppid in victims and proc.pid not in (me, 0, 1):
                victims.add(proc.pid)
                changed = True
    return victims


class _Tracker(threading.Thread):
    """Polls the process table and remembers every descendant of the run."""

    def __init__(self, root_pid: int, started: float):
        super().__init__(daemon=True)
        self.root_pid, self.started = root_pid, started
        self.known: Dict[int, float] = {p.pid: p.start for p in _snapshot() if p.pid == root_pid}
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(POLL_INTERVAL_S):
            self.observe()

    def observe(self) -> None:
        self.remember(_snapshot())

    def remember(self, procs: Sequence[_Proc]) -> Set[int]:
        victims = _victims(procs, self.known, self.root_pid, self.started)
        for proc in procs:
            if proc.pid in victims:
                self.known[proc.pid] = proc.start
        return victims

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5)


def _sweep(tracker: _Tracker) -> None:
    """Kill every process proven to belong to the run; each pid is re-checked
    (same start time) in a fresh snapshot right before the signal."""
    for _ in range(6):
        victims = tracker.remember(_snapshot())
        if not victims:
            return
        fresh = {p.pid: p.start for p in _snapshot()}
        for pid in victims:
            if fresh.get(pid) == tracker.known.get(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        time.sleep(0.1)


def _kill_group(pgid: int) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        if sig == signal.SIGTERM:
            time.sleep(KILL_GRACE_S)


def _supervise(proc: "subprocess.Popen[bytes]", timeout_s: float, tracker: _Tracker) -> int:
    try:
        rc = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        tracker.observe()
        _kill_group(proc.pid)
        proc.wait()
        return RC_TIMEOUT
    tracker.observe()
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # stragglers left by the command
    except (ProcessLookupError, PermissionError):
        pass
    return rc if rc >= 0 else RC_SIGNAL_BASE - rc


# --------------------------------------------------------------------------
# Bounded, redacting log
# --------------------------------------------------------------------------


class _BoundedLog:
    """Collects child output in memory up to a cap; redacts before any write.

    Raw output never touches disk: the log file is written once, after
    redaction, so a crash leaves an empty 0600 file rather than raw output.
    """

    def __init__(self, cap: int, redact: Callable[[bytes], bytes]):
        self._cap = cap
        self._redact = redact
        self._buf = bytearray()
        self._truncated = False

    def feed(self, chunk: bytes) -> None:
        room = self._cap - len(self._buf)
        if room > 0:
            self._buf += chunk[:room]
        if len(chunk) > max(room, 0):
            self._truncated = True

    def render(self) -> bytes:
        data = bytes(self._buf)
        if self._truncated:
            cut = data.rfind(b"\n")
            data = data[: cut + 1] if cut >= len(data) - 65536 and cut >= 0 else data[:-4096]
        try:
            clean = self._redact(data)
        except Exception:  # noqa: BLE001 - any redaction failure withholds the log
            clean = LOG_WITHHELD
        return clean + LOG_TRUNCATED if self._truncated else clean

    def write_to(self, log_path: str) -> None:
        payload = self.render()
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.chmod(log_path, stat.S_IRUSR | stat.S_IWUSR)


def _pump(fd: int, sink: _BoundedLog) -> None:
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            return
        if not chunk:
            return
        sink.feed(chunk)


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------


def _process_count() -> Optional[int]:
    """Processes owned by this uid (what ``RLIMIT_NPROC`` is compared with)."""
    try:
        out = subprocess.run(
            [PS, "-U", str(os.getuid()), "-o", "pid="],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return len(out.split()) or None


def _limiter(nproc: Optional[int], fsize: int, nofile: int) -> Callable[[], None]:
    """Child-side rlimits (runs between fork and exec; keep it trivial)."""
    wanted = [(resource.RLIMIT_CORE, 0), (resource.RLIMIT_FSIZE, fsize), (resource.RLIMIT_NOFILE, nofile)]
    if nproc is not None:
        wanted.append((resource.RLIMIT_NPROC, nproc))

    def apply() -> None:
        for which, value in wanted:
            try:
                _, hard = resource.getrlimit(which)
                if hard != resource.RLIM_INFINITY:
                    value = min(value, hard)
                resource.setrlimit(which, (value, value))
            except (ValueError, OSError):
                pass  # a limit this host refuses is skipped, never fatal

    return apply


def run_verify(
    tree_oid: str,
    cmd: str,
    *,
    repo: str,
    main_worktree: str,
    deps: Sequence[str] = (),
    read_roots: Sequence[str] = (),
    timeout_s: float,
    log_path: str,
    other_worktrees: Sequence[str] = (),
    home: Optional[str] = None,
    path_env: Optional[str] = None,
    redact: Optional[Callable[[bytes], bytes]] = None,
    max_log_bytes: int = MAX_LOG_BYTES,
    nproc_headroom: Optional[int] = DEFAULT_NPROC_HEADROOM,
    fsize_bytes: int = DEFAULT_FSIZE_BYTES,
    nofile: int = DEFAULT_NOFILE,
) -> int:
    """Run ``cmd`` against the materialized tree under the sandbox; return rc.

    rc 124 means the wall timeout killed the process group. Output is read
    through a bounded (``max_log_bytes``, default 8 MB) redacting writer, so
    the 0600 log is only ever written redacted. After the run every process
    proven to belong to it is killed (see the module docstring for the limit). The child gets ``RLIMIT_CORE`` 0, ``RLIMIT_FSIZE`` and
    ``RLIMIT_NOFILE``, and ``RLIMIT_NPROC`` set to the host's current process
    count plus ``nproc_headroom`` (the limit is per uid on macOS; ``None`` skips it). ``redact``
    defaults to ``cloud_scan.redact``. Raises ``SandboxUnavailable`` when
    sandbox-exec cannot be used (or rejects the profile) and ``SandboxError``
    when the tree cannot be materialized or a dependency root is unsafe.
    """
    if not sandbox_available():
        raise SandboxUnavailable("sandbox-exec unavailable on this host")
    redactor = redact if redact is not None else cloud_scan.redact
    home_dir = home if home is not None else os.path.expanduser("~")
    path_source = path_env if path_env is not None else os.environ.get("PATH", "")
    tmp = _real(tempfile.mkdtemp(prefix="ffs-verify-"))
    try:
        sink = _BoundedLog(max_log_bytes, redactor)
        limiter = _limiter(_nproc_limit(nproc_headroom), fsize_bytes, nofile)
        return _run_in_tmp(
            tmp, tree_oid, cmd, repo, main_worktree, deps, read_roots,
            timeout_s, log_path, other_worktrees, home_dir, path_source, sink, limiter,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _nproc_limit(headroom: Optional[int]) -> Optional[int]:
    """Absolute RLIMIT_NPROC (uid process count + headroom); ``None`` skips it."""
    count = _process_count()
    return None if count is None or headroom is None else max(1, count + headroom)


def _run_in_tmp(
    tmp: str,
    tree_oid: str,
    cmd: str,
    repo: str,
    main_worktree: str,
    deps: Sequence[str],
    read_roots: Sequence[str],
    timeout_s: float,
    log_path: str,
    other_worktrees: Sequence[str],
    home_dir: str,
    path_source: str,
    sink: _BoundedLog,
    limiter: Callable[[], None],
) -> int:
    work = os.path.join(tmp, "w")
    os.makedirs(os.path.join(tmp, "home"))
    materialize_tree(repo, tree_oid, work)
    state_dir = os.path.join(_real(main_worktree), *STATE_SUBDIR)
    zones = _zones_for(home_dir, main_worktree, state_dir, other_worktrees, tmp)
    dep_roots = _link_deps(work, main_worktree, deps, zones)
    toolchain = resolve_toolchain_roots(
        path_source, home_dir, main_worktree=main_worktree,
        other_worktrees=other_worktrees, state_dir=state_dir,
    )
    profile = build_profile(
        work_dir=work, tmp_dir=tmp, home=home_dir, main_worktree=main_worktree,
        state_dir=state_dir, other_worktrees=other_worktrees, dep_roots=dep_roots,
        toolchain_roots=toolchain, read_roots=read_roots,
    )
    profile_path = os.path.join(tmp, "profile.sb")
    with open(profile_path, "w", encoding="utf-8") as handle:
        handle.write(profile)
    if _run_profile_check(profile_path) != 0:
        raise SandboxUnavailable("sandbox-exec rejected the verify profile")
    env = _child_env(tmp, _filtered_path(path_source, home_dir, toolchain, zones.others + [zones.main]))
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    os.chmod(log_path, stat.S_IRUSR | stat.S_IWUSR)
    started = time.time()
    proc = subprocess.Popen(
        [SANDBOX_EXEC, "-f", profile_path, BASH, "-c", cmd],
        cwd=work, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
        preexec_fn=limiter,
    )
    assert proc.stdout is not None
    reader = threading.Thread(target=_pump, args=(proc.stdout.fileno(), sink), daemon=True)
    tracker = _Tracker(proc.pid, started)
    reader.start()
    tracker.start()
    try:
        rc = _supervise(proc, timeout_s, tracker)
    finally:
        tracker.stop()
        _sweep(tracker)
        reader.join(timeout=10)
        proc.stdout.close()
    sink.write_to(log_path)
    return rc


def _run_profile_check(profile_path: str) -> Optional[int]:
    try:
        return subprocess.run(
            [SANDBOX_EXEC, "-f", profile_path, "/usr/bin/true"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=PREFLIGHT_TIMEOUT_S, check=False,
        ).returncode
    except (OSError, subprocess.SubprocessError):
        return None
