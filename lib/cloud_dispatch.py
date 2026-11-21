#!/usr/bin/env python3
"""Cloud delegation dispatcher: CLI verbs and the detached orchestrator.

Contract: docs/cloud-delegation-contracts.md sections 3, 4, 6, 7, 10 and the
amendments of section 14.  This module is the integration layer over
``cloud_ledger`` (state), ``cloud_host`` (provider backends), ``cloud_collect``
(collect, review, verify, integrate), ``cloud_scan``, ``delegation_router`` and
``handoff_packet``.  Python 3.9, stdlib only, no ``shell=True``.

Every side effect goes through ``Services`` (clock, sleep, subprocess runner,
backends, spawn, gh, review, verify, fault hook) so the orchestrator can be driven
deterministically in tests.  The orchestrator never writes anything the ledger
module does not own: ledger updates go through ``cloud_ledger.locked_update``.

Output: human lines start with ``CLOUD:``; refusals are
``CLOUD-REFUSED: <CODE> <detail>``.  ``plan --json`` style machine output
(``plan``, ``status --json``, ``doctor --json``) is one JSON document.
Exit codes: 0 ok, 2 usage, 3 refused, 4 ok but operator items exist, 75 the orchestrator lock was
still held when ``orchestrate --wait-lock N`` gave up (the only path that produces it: a plain
``orchestrate`` that finds the lock held is not an error, it points the lifecycle record at the holder and
exits 0, contract section 4), 78 store or config error.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import errno
import fcntl
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

LIB_DIR = Path(__file__).resolve().parent
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import cloud_collect  # noqa: E402
import cloud_host  # noqa: E402
import cloud_ledger as ledger  # noqa: E402
import cloud_scan  # noqa: E402
import delegation_router  # noqa: E402
import handoff_packet  # noqa: E402
import operator_guard_hook  # noqa: E402

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_OPERATOR = 4
EXIT_LOCK = 75
EXIT_STORE = 78

PREFIX = "CLOUD:"
REFUSED_PREFIX = "CLOUD-REFUSED:"

TICK_ACTIVE_S = 60
TICK_IDLE_MAX_S = 600
TICK_JITTER = 0.1
WAIT_LOCK_S = 300  # ``orchestrate --resume --wait-lock`` spawned by an operator verb while the lock is held
LOCK_WAIT_POLL_S = 0.5
GIT_TIMEOUT_S = 120
GIT_PUSH_TIMEOUT_S = 300
SCRIPT_TIMEOUT_S = 60
GATES_TIMEOUT_S = 30
GH_TIMEOUT_S = 30
NOTIFY_TIMEOUT_S = 30
SECONDS_PER_HOUR = 3600
DEFAULT_DEADLINE_HOURS = 12
DEFAULT_VERIFY_TIMEOUT_S = 900
FETCH_FAILURE_LIMIT = 3
CONSECUTIVE_ERROR_LIMIT = 5  # global (non-task) orchestrator steps
TASK_ERROR_LIMIT = 3  # consecutive internal errors of one task step before it goes to needs-operator
DECISION_TEXT_MAX = 3900
LOG_LINE_MAX = 2000
MAX_LISTED = 20
ORIGIN_NAME = "origin"
LIFECYCLE_BUDGETS = '{"respawns":0,"env_deaths":8}'
WRAPPER_RELATIVE = "scripts/gsd/cloud-dispatch.sh"
PLACEHOLDER_NONCE = "0" * 32
#: collect blockers that mean "this candidate is not my task's result" (not a content verdict)
WRONG_CANDIDATE_CONSTANTS = ("B_MANIFEST_MISSING", "B_MANIFEST_INVALID", "B_MANIFEST_MISMATCH")
WRONG_CANDIDATE_NAMES = ("manifest-missing", "manifest-invalid", "manifest-mismatch")

ENV_ALLOW_NON_GITHUB = "FFS_CLOUD_ALLOW_NON_GITHUB_ORIGIN"  # test seam: downgrades the GitHub check to WARN
ENV_TOKEN_BUDGET = "GSD_TOKEN_BUDGET"  # same variable gsd-run.sh reads for a run token budget
ENV_MANAGED_INGRESS = "FFS_MANAGED_INGRESS"
ENV_NOTIFY = "DIGEST_NOTIFY_CMD"
DEFAULT_CODEX_ENV_VAR = "FFS_CODEX_CLOUD_ENV"

CLOUD_FILES = ("config.json", "setup.sh", "env-check.sh", "session-start.sh")
CLOUD_DIR_REL = ".ffs/cloud"
EXECUTABLE_FILES = ("setup.sh", "env-check.sh", "session-start.sh")
SESSION_HOOK_COMMAND = operator_guard_hook.SESSION_START_COMMAND
SESSION_HOOK_TIMEOUT_S = 300
SETTINGS_REL = ".claude/settings.json"

POLICIES = ("auto", "prefer", "required")
PROVIDERS = ("claude", "codex")

# gates.py LEDGER evidence ids are broader than the contract's run id regex.
EVIDENCE_RUN_ID_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")
GITHUB_URL_RE = re.compile(
    r"^(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
# ``ssh://<user>@host`` and ``git@host:path`` carry a user name only, never a secret.
SCP_USER_RE = re.compile(r"^git@[A-Za-z0-9_.-]+:")
URL_AUTHORITY_USERINFO_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/?#]*@")
SSH_PLAIN_USER_RE = re.compile(r"^ssh://[A-Za-z0-9._-]+@[^/?#@:]*(?:/|$)")
OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
DIGEST_RE = re.compile(r"^[0-9a-f]{12,128}$")
PACKAGE_RE = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9@._/+-]{0,127}$")

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class DispatchError(Exception):
    """Base class: ``code`` is a stable UPPER_SNAKE token, ``detail`` free text."""

    exit_code = EXIT_STORE

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__("%s %s" % (code, detail) if detail else code)
        self.code = code
        self.detail = detail


class Refused(DispatchError):
    exit_code = EXIT_REFUSED


class StoreError(DispatchError):
    exit_code = EXIT_STORE


class UsageError(DispatchError):
    exit_code = EXIT_USAGE


class IoRefused(Refused):
    """A filesystem failure reported as ``CLOUD-REFUSED:`` with the store exit code and no stack trace."""

    exit_code = EXIT_STORE


class SimulatedCrash(BaseException):
    """Raised by test fault hooks; deliberately not an ``Exception``."""


# Ledger error codes mapped onto exit classes.
_REFUSED_LEDGER = frozenset(
    [ledger.E_ILLEGAL, ledger.E_BLOCKER_MISMATCH, ledger.E_UNKNOWN_TASK, ledger.E_RUN_DONE, ledger.E_STALE,
     ledger.E_EXISTS, ledger.E_BAD_TASKS, ledger.E_INTEGRATING]
)
_USAGE_LEDGER = frozenset([ledger.E_BAD_ID, ledger.E_BAD_ARGUMENT, ledger.E_BAD_FIELD])


def classify_ledger_error(exc: ledger.LedgerError) -> DispatchError:
    if exc.code in _REFUSED_LEDGER:
        return Refused(exc.code, exc.detail)
    if exc.code in _USAGE_LEDGER:
        return UsageError(exc.code, exc.detail)
    return StoreError(exc.code, exc.detail)


# --------------------------------------------------------------------------
# Blocker mapping (contract 14.8 plus the extra names cloud_collect returns)
# --------------------------------------------------------------------------

EV_REJECT = ledger.EV_REJECT
EV_NEEDS = ledger.EV_NEEDS_OPERATOR
EV_REPLAN = ledger.EV_REPLAN

#: ``cloud_collect`` blocker name -> (ledger event, ledger blocker).  Structural
#: refusals become ``quarantine-refused`` and keep the original name in the
#: decision text.  ``tip-moved`` is not a failure: the caller re-plans.
BLOCKER_MAP: Dict[str, Tuple[str, Optional[str]]] = {
    cloud_collect.B_SECRET_LEAK: (EV_REJECT, ledger.B_SECRET_LEAK),
    cloud_collect.B_SCAN_REFUSED: (EV_REJECT, ledger.B_QUARANTINE_REFUSED),
    cloud_collect.B_NOT_DESCENDED: (EV_REJECT, ledger.B_QUARANTINE_REFUSED),
    cloud_collect.B_UNSAFE_MODE: (EV_REJECT, ledger.B_QUARANTINE_REFUSED),
    cloud_collect.B_BLOB_LIMIT: (EV_REJECT, ledger.B_QUARANTINE_REFUSED),
    cloud_collect.B_TOO_MANY_COMMITS: (EV_REJECT, ledger.B_QUARANTINE_REFUSED),
    cloud_collect.B_MANIFEST_INVALID: (EV_REJECT, ledger.B_QUARANTINE_REFUSED),
    cloud_collect.B_MANIFEST_MISSING: (EV_REJECT, ledger.B_QUARANTINE_REFUSED),
    cloud_collect.B_MANIFEST_BLOCKED: (EV_NEEDS, ledger.B_MANIFEST_BLOCKED),
    cloud_collect.B_OUTSIDE_OWNS: (EV_NEEDS, ledger.B_PATH_OUTSIDE_OWNS),
    cloud_collect.B_DENYLIST: (EV_NEEDS, ledger.B_DENYLIST),
    cloud_collect.B_DEPENDENCY: (EV_NEEDS, ledger.B_DEPENDENCY_CHANGE),
    cloud_collect.B_GIT: (EV_REJECT, ledger.B_QUARANTINE_REFUSED),
    cloud_collect.B_CANDIDATE_CONFLICT: (EV_REJECT, ledger.B_QUARANTINE_REFUSED),
    cloud_collect.B_REVIEW_UNAVAILABLE: (EV_NEEDS, ledger.B_REVIEW_UNAVAILABLE),
    cloud_collect.B_REVIEW_CAP: (EV_NEEDS, ledger.B_REVIEW_CAP),
    cloud_collect.B_REVIEW_MODEL_DOWNGRADED: (EV_NEEDS, ledger.B_REVIEW_MODEL_DOWNGRADED),
    cloud_collect.B_REVIEW_MODEL_UNKNOWN: (EV_NEEDS, ledger.B_REVIEW_MODEL_UNKNOWN),
    cloud_collect.B_REVIEW_REVISE: (EV_REJECT, ledger.B_REVIEW_REVISE),
    cloud_collect.B_TESTS_FAILED: (EV_REJECT, ledger.B_TESTS_FAILED),
    cloud_collect.B_SANDBOX: (EV_NEEDS, ledger.B_SANDBOX_UNAVAILABLE),
    cloud_collect.B_VERIFY_CMD_BLANK: (EV_NEEDS, ledger.B_VERIFY_CMD_MISSING),
    cloud_collect.B_MERGE_CONFLICT: (EV_NEEDS, ledger.B_MERGE_CONFLICT),
    cloud_collect.B_PATCH_ID: (EV_NEEDS, ledger.B_PATCH_ID_MISMATCH),
    cloud_collect.B_TIP_MOVED: (EV_REPLAN, None),
    # blockers the collector adds with the binary/merge review binding; keyed by value so this module
    # imports whichever constants cloud_collect defines
    "binary-change": (EV_NEEDS, ledger.B_BINARY_CHANGE),
}

#: A collect blocker this table does not know is not a reason to raise: it goes to the operator.
UNKNOWN_BLOCKER: Tuple[str, str] = (EV_NEEDS, ledger.B_INTERNAL_ERROR)

STAGE_COLLECT = "collect"
STAGE_REVIEW = "review"
STAGE_VERIFY = "verify"
STAGE_RETEST = "retest"
STAGE_INTEGRATE = "integrate"

#: The ledger state a stage runs from (used to check the mapping against TRANSITIONS).
STAGE_SOURCE_STATE = {
    STAGE_COLLECT: ledger.RESULT_READY,
    STAGE_REVIEW: ledger.COLLECTED,
    STAGE_VERIFY: ledger.REVIEWED,
    STAGE_RETEST: ledger.VERIFIED,
    STAGE_INTEGRATE: ledger.VERIFIED,
}

#: Where the default mapping has no legal move from the stage's source state.
STAGE_OVERRIDES: Dict[Tuple[str, str], Tuple[str, str]] = {
    (STAGE_REVIEW, cloud_collect.B_GIT): (EV_NEEDS, ledger.B_REVIEW_UNAVAILABLE),
    (STAGE_VERIFY, cloud_collect.B_GIT): (EV_NEEDS, ledger.B_SANDBOX_UNAVAILABLE),
    (STAGE_RETEST, cloud_collect.B_GIT): (EV_NEEDS, ledger.B_SANDBOX_UNAVAILABLE),
    (STAGE_INTEGRATE, cloud_collect.B_GIT): (EV_NEEDS, ledger.B_MERGE_CONFLICT),
}

def map_blocker(stage: str, name: Optional[str]) -> Tuple[str, Optional[str]]:
    """(event, ledger blocker) for a ``cloud_collect`` blocker raised in ``stage``.

    A mapping that is not a legal move from the stage's source state, and a blocker name this table does not
    know, both become ``needs-operator`` / ``internal-error`` (legal from every stage) instead of raising."""
    key = (stage, name or "")
    if key in STAGE_OVERRIDES:
        return STAGE_OVERRIDES[key]
    mapped = BLOCKER_MAP.get(name or "")
    if mapped is not None and _legal(stage, mapped):
        return mapped
    return UNKNOWN_BLOCKER


def _legal(stage: str, mapped: Tuple[str, Optional[str]]) -> bool:
    event, blocker = mapped
    if event == EV_REPLAN:
        return True
    row = ledger.TRANSITIONS.get((STAGE_SOURCE_STATE[stage], event))
    return row is not None and blocker in row[1]


# --------------------------------------------------------------------------
# Services (every side effect is injectable)
# --------------------------------------------------------------------------


class CmdResult:
    def __init__(self, rc: int, out: bytes = b"", err: bytes = b"") -> None:
        self.rc = rc
        self.out = out
        self.err = err

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def text(self) -> str:
        return self.out.decode("utf-8", "replace")


def default_spawn(argv: Sequence[str], log_path: Path, cwd: Path) -> int:
    """Start ``argv`` in its own session with stdio off the caller's terminal; return its pid."""
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        proc = subprocess.Popen(
            list(argv), stdin=subprocess.DEVNULL, stdout=fd, stderr=subprocess.STDOUT, cwd=str(cwd),
            start_new_session=True, close_fds=True,
        )
    finally:
        os.close(fd)
    return proc.pid


class Services:
    """Clock, sleep, subprocess, backends and test seams in one injectable object."""

    def __init__(self) -> None:
        self.now: Callable[[], float] = time.time
        self.sleep: Callable[[float], None] = time.sleep
        self.rand: Callable[[], float] = random.random
        self.stdout: Any = sys.stdout
        self.fault: Callable[[str], None] = lambda point: None
        self.spawn: Callable[[Sequence[str], Path, Path], int] = default_spawn
        self.backend_factory: Callable[[str], Any] = cloud_host.get_backend
        self.gh_runner: Optional[Callable[[Sequence[str]], Tuple[int, str]]] = None
        self.review_script: Optional[str] = None
        self.verify_fn: Optional[Callable[..., int]] = None
        self.sandbox_available: Optional[Callable[[], bool]] = None
        self.merge_hook_entry: Optional[Callable[..., Dict[str, Any]]] = None
        self.wait_sleep: Callable[[float], None] = time.sleep  # lock waiting only; never the tick sleep
        self._backends: Dict[str, Any] = {}

    # -- environment -------------------------------------------------------

    def environ(self) -> Dict[str, str]:
        env = cloud_scan.scrub_git_env(dict(os.environ))  # repository-selecting git variables never reach a child
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def run(self, argv: Sequence[str], *, cwd: Optional[Path] = None, timeout: float = GIT_TIMEOUT_S,
            env: Optional[Dict[str, str]] = None, input_bytes: Optional[bytes] = None) -> CmdResult:
        kwargs: Dict[str, Any] = {}
        if input_bytes is not None:
            kwargs["input"] = input_bytes
        else:
            kwargs["stdin"] = subprocess.DEVNULL
        try:
            proc = subprocess.run(
                list(argv), cwd=str(cwd) if cwd else None, env=env if env is not None else self.environ(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False, **kwargs)
        except subprocess.TimeoutExpired:
            return CmdResult(124, b"", b"timeout")
        except OSError as exc:
            return CmdResult(127, b"", str(exc).encode("utf-8", "replace"))
        return CmdResult(proc.returncode, proc.stdout, proc.stderr)

    # -- providers ---------------------------------------------------------

    def backend(self, name: str) -> Any:
        if name not in self._backends:
            self._backends[name] = self.backend_factory(name)
        return self._backends[name]

    def emit(self, line: str) -> None:
        self.stdout.write(line + "\n")
        try:
            self.stdout.flush()
        except (OSError, ValueError):
            pass


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def redact_text(text: str, limit: int = LOG_LINE_MAX) -> str:
    """Bounded, control-free text with secret shapes replaced; never raises."""
    try:
        raw = cloud_scan.redact(text.encode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - scanner refusal means withhold
        return "[withheld]"
    clean = raw.decode("utf-8", "replace")
    clean = "".join(ch if ch.isprintable() or ch in "\n\t" else "?" for ch in clean)
    return clean[:limit]


def write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_private(path: Path, line: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (line.rstrip("\n") + "\n").encode("utf-8", "replace"))
    finally:
        os.close(fd)


def clip_list(items: Iterable[str], limit: int = ledger.MANIFEST_LIST_MAX) -> List[str]:
    out: List[str] = []
    for item in items:
        out.append(redact_text(str(item), DECISION_TEXT_MAX))
        if len(out) >= limit:
            break
    return out


def validate_run_id(run: Any) -> str:
    """Contract regex AND what gates.py accepts, so one id works everywhere.

    gates.py ``check-grant``/``grant``/``loop-round`` accept any printable id up to 128
    characters (``EVIDENCE_RUN_ID_RE``), which the contract regex always satisfies; the
    degradation evidence ledger is narrower (spec-NNN | adhoc-* | run-N) and
    ``cloud-review.sh`` already maps cloud ids into ``adhoc-cloud-*`` for it.
    """
    try:
        ledger.validate_run_id(run)
    except ledger.LedgerError:
        raise UsageError("BAD_RUN_ID", "run id must match ^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
    if not delegation_router.RUN_ID_RE.match(run) or not EVIDENCE_RUN_ID_RE.match(run):
        raise UsageError("BAD_RUN_ID", "run id is not accepted by the grants store")
    return str(run)


def script_candidates(root: Path, name: str) -> List[Path]:
    """Only the dispatcher's own installation: the managed layout (``<lib>/scripts/gsd``), then its source
    checkout.  Never ``root`` (the target repository): a merged result must not control later reviews."""
    return [LIB_DIR / "scripts" / "gsd" / name, LIB_DIR.parent / "scripts" / "gsd" / name]


def find_script(root: Path, name: str) -> Optional[Path]:
    for candidate in script_candidates(root, name):
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------


def git(svc: Services, root: Path, args: Sequence[str], *, timeout: float = GIT_TIMEOUT_S,
        input_bytes: Optional[bytes] = None) -> CmdResult:
    # --no-optional-locks: read-only helpers (plan, doctor, status) must not rewrite the operator's index
    return svc.run(["git", "--no-optional-locks", "-C", str(root)] + list(args), timeout=timeout, input_bytes=input_bytes)


def git_text(svc: Services, root: Path, args: Sequence[str]) -> Optional[str]:
    result = git(svc, root, args)
    return result.text().strip() if result.ok else None


def rev_parse(svc: Services, root: Path, spec: str) -> Optional[str]:
    text = git_text(svc, root, ["rev-parse", "--verify", "--quiet", spec])
    return text if text and OID_RE.match(text) else None


def is_ancestor(svc: Services, root: Path, ancestor: str, descendant: str) -> bool:
    return git(svc, root, ["merge-base", "--is-ancestor", ancestor, descendant]).ok


def remote_env() -> Dict[str, str]:
    return cloud_host.child_env()


def pushed_new_ref(porcelain: str, ref: str) -> bool:
    """True when ``git push --porcelain`` output reports ``ref`` as a newly created destination (flag ``*``).
    An up-to-date (``=``) or otherwise reported ref leaves creation unconfirmed."""
    for line in porcelain.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0] == "*" and parts[1].partition(":")[2] == ref and parts[2].startswith("[new "):
            return True
    return False


def ls_remote(svc: Services, url: str, pattern: str) -> Tuple[bool, Dict[str, str]]:
    """``(ok, {ref: oid})`` for refs matching ``pattern`` on the pinned URL."""
    result = svc.run(["git", "ls-remote", url, pattern], timeout=GIT_TIMEOUT_S, env=remote_env())
    if not result.ok:
        return False, {}
    refs: Dict[str, str] = {}
    for line in result.text().splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and OID_RE.match(parts[0]):
            refs[parts[1]] = parts[0]
    return True, refs


def resolve_root(svc: Services, root_arg: Optional[str]) -> Path:
    """The main worktree: ``--root`` (absolute) or the one containing the cwd."""
    if root_arg:
        candidate = Path(root_arg)
        if not candidate.is_absolute():
            raise UsageError("BAD_ROOT", "--root must be an absolute path")
    else:
        candidate = Path.cwd()
    top = git_text(svc, candidate, ["rev-parse", "--show-toplevel"]) if candidate.is_dir() else None
    if not top:
        raise Refused("NOT_A_REPO", "no git worktree at %s" % candidate)
    top_path = Path(os.path.realpath(top))
    git_dir = git_text(svc, top_path, ["rev-parse", "--absolute-git-dir"])
    common = git_text(svc, top_path, ["rev-parse", "--git-common-dir"])
    if not git_dir or not common:
        raise Refused("NOT_A_REPO", "cannot resolve the git directory")
    common_abs = os.path.realpath(common if os.path.isabs(common) else os.path.join(str(top_path), common))
    if os.path.realpath(git_dir) != common_abs:
        raise Refused("NOT_MAIN_WORKTREE", "%s is a linked worktree; use the main worktree" % top_path)
    if root_arg and os.path.realpath(root_arg) != str(top_path):
        raise Refused("NOT_MAIN_WORKTREE", "--root must be the worktree top level (%s)" % top_path)
    return top_path


def origin_has_credentials(url: str) -> bool:
    """True when the URL embeds a secret: any ``https://user[:pass]@`` userinfo, or ``user:pass@`` anywhere.

    ``git@github.com:o/r`` and ``ssh://git@github.com/o/r`` (a user name, no secret) are fine.  Uses
    ``cloud_scan.url_has_userinfo`` when the scanner provides it, ORed with the local test so a scanner
    that is more lenient never lets a credential through."""
    if not url or GITHUB_URL_RE.match(url) or SCP_USER_RE.match(url) or SSH_PLAIN_USER_RE.match(url):
        return False
    if URL_AUTHORITY_USERINFO_RE.match(url):
        return True
    scanner = getattr(cloud_scan, "url_has_userinfo", None)
    if callable(scanner):
        try:
            return bool(scanner(url))
        except Exception:  # noqa: BLE001 - fail closed
            return True
    return False


def origin_url(svc: Services, root: Path) -> str:
    """The pinned origin URL.  Never logged or stored when it embeds credentials (refused before any use)."""
    url = git_text(svc, root, ["remote", "get-url", ORIGIN_NAME])
    if not url:
        raise Refused("NO_ORIGIN", "this repository has no '%s' remote" % ORIGIN_NAME)
    if origin_has_credentials(url):
        raise Refused("ORIGIN_HAS_CREDENTIALS",
                      "the origin URL embeds credentials; use a credential-free URL and a credential helper")
    if url.startswith("-") or re.search(r"[\s\x00-\x1f]", url):
        raise Refused("ORIGIN_URL_INVALID", "origin URL is not usable")
    return url


# --------------------------------------------------------------------------
# Config, grants, providers
# --------------------------------------------------------------------------


def load_config(svc: Services, root: Path, base_sha: Optional[str] = None) -> Dict[str, Any]:
    """``.ffs/cloud/config.json`` from the base commit (or the working tree); ``{}`` when absent."""
    rel = CLOUD_DIR_REL + "/config.json"
    if base_sha:
        shown = git(svc, root, ["show", "%s:%s" % (base_sha, rel)])
        raw = shown.out if shown.ok else None
    else:
        path = root / rel
        raw = path.read_bytes() if path.is_file() else None
    if raw is None:
        return {}
    try:
        config = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise StoreError("CONFIG_INVALID", "%s is not valid JSON" % rel)
    if not isinstance(config, dict):
        raise StoreError("CONFIG_INVALID", "%s must be an object" % rel)
    return config


def config_section(config: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = config.get(key)
    return value if isinstance(value, dict) else {}


def effective_verify_cmd(task_cmd: Any, config: Dict[str, Any]) -> str:
    """The task's own verify command, else ``config.verify.cmd``; both stripped, ``""`` when neither is non-blank.

    A whitespace-only task command is blank, so it falls back to the config command instead of
    shadowing it (``bash -c '   '`` exits 0 and would pass with no test run)."""
    own = task_cmd.strip() if isinstance(task_cmd, str) else ""
    if own:
        return own
    fallback = config_section(config, "verify").get("cmd")
    return fallback.strip() if isinstance(fallback, str) else ""


def limits_from(config: Dict[str, Any]) -> Dict[str, int]:
    limits = dict(delegation_router.DEFAULT_LIMITS)
    for key, value in config_section(config, "limits").items():
        if key in limits and isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            limits[key] = value
    return limits


def deadline_seconds(config: Dict[str, Any]) -> int:
    hours = config.get("deadline_hours", DEFAULT_DEADLINE_HOURS)
    if isinstance(hours, bool) or not isinstance(hours, int) or hours < 1:
        hours = DEFAULT_DEADLINE_HOURS
    return hours * SECONDS_PER_HOUR


def exposure_names(config: Dict[str, Any], provider: str) -> List[str]:
    raw = config_section(config_section(config, "secrets"), "exposure").get(provider, [])
    return sorted(set(str(n) for n in raw)) if isinstance(raw, list) else []


def codex_env_id(config: Dict[str, Any]) -> Optional[str]:
    var = str(config_section(config, "codex").get("env_id_var") or DEFAULT_CODEX_ENV_VAR)
    value = os.environ.get(var, "").strip()
    return value or None


def gates_check(svc: Services, run: str, action: str) -> bool:
    """``gates.py check-grant``: exit 0 granted, 1 not, anything else is a store error (refuse)."""
    result = svc.run([sys.executable or "python3", str(LIB_DIR / "gates.py"), "check-grant", run, "--action", action],
                     timeout=GATES_TIMEOUT_S)
    if result.rc == 0:
        return True
    if result.rc == 1:
        return False
    raise Refused("GRANT_STORE_ERROR", "gates.py check-grant exited %d" % result.rc)


def granted_set(svc: Services, run: str, keys: Iterable[str]) -> List[str]:
    return sorted(key for key in set(keys) if gates_check(svc, run, key))


def push_ref_valid(run: str, ref: str) -> bool:
    """Section 10: only ``refs/heads/ffs/cloud/<R>/(integration|<T>-g<G>)``; no force or ``..``."""
    if not isinstance(ref, str) or ref.startswith("+") or ".." in ref or re.search(r"[\s:\\^~?*\[]", ref):
        return False
    prefix = "refs/heads/ffs/cloud/%s/" % run
    if not ref.startswith(prefix):
        return False
    name = ref[len(prefix):]
    return name == "integration" or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}-g[1-9][0-9]{0,5}", name) is not None


def provider_state(svc: Services, config: Dict[str, Any]) -> Dict[str, Tuple[bool, str]]:
    """``{provider: (usable, reason)}``; codex also needs its environment id."""
    state: Dict[str, Tuple[bool, str]] = {}
    for name in PROVIDERS:
        try:
            ok, reason = svc.backend(name).available()
        except cloud_host.UnsupportedBackend as exc:
            ok, reason = False, exc.code
        if ok and name == "codex" and codex_env_id(config) is None:
            ok, reason = False, "CODEX_ENV_MISSING"
        if ok and limits_from(config)[name] <= 0:
            ok, reason = False, "LIMIT_ZERO"
        state[name] = (ok, reason)
    return state


def slots_free(svc: Services, root: Path, config: Dict[str, Any]) -> Dict[str, int]:
    limits = limits_from(config)
    free = {name: limits[name] for name in PROVIDERS}
    try:
        slots = ledger.read(ledger.slots_path(root)).get("slots", [])
    except ledger.LedgerError:
        return free
    for slot in slots:
        provider = slot.get("provider")
        if provider in free:
            free[provider] = max(0, free[provider] - 1)
    total_free = max(0, limits["total"] - len(slots))
    return {name: min(value, total_free) for name, value in free.items()}


def host_pressure_high(svc: Services, root: Path) -> bool:
    script = find_script(root, "host-pressure.sh")
    if script is None:
        return False
    return svc.run(["bash", str(script), "check"], cwd=root, timeout=SCRIPT_TIMEOUT_S).rc == 75


# --------------------------------------------------------------------------
# Doctor
# --------------------------------------------------------------------------

OK, WARN, FAIL = "OK", "WARN", "FAIL"


class Check:
    def __init__(self, code: str, level: str, detail: str = "", data: Optional[Dict[str, Any]] = None) -> None:
        self.code = code
        self.level = level
        self.detail = detail
        self.data = data  # structured, value-free extras for ``doctor --json``

    def as_dict(self) -> Dict[str, Any]:
        record: Dict[str, Any] = {"code": self.code, "level": self.level, "detail": self.detail}
        if self.data is not None:
            record["data"] = self.data
        return record


def parse_github(url: str) -> Optional[Tuple[str, str]]:
    match = GITHUB_URL_RE.match(url or "")
    return (match.group(1), match.group(2)) if match else None


def run_gh(svc: Services, argv: Sequence[str]) -> Tuple[int, str]:
    if svc.gh_runner is not None:
        return svc.gh_runner(argv)
    result = svc.run(["gh"] + list(argv), timeout=GH_TIMEOUT_S)
    return result.rc, result.text()


def branch_rules_verified(svc: Services, url: str, branch: str) -> Tuple[bool, str]:
    """Section 14.6: active ``update`` and ``deletion`` rules for this exact branch via ``gh api``."""
    repo = parse_github(url)
    if repo is None:
        return False, "origin is not a GitHub remote"
    try:
        rc, text = run_gh(svc, ["api", "repos/%s/%s/rules/branches/%s" % (repo[0], repo[1], branch)])
        rules = json.loads(text) if rc == 0 else None
    except (ValueError, OSError):
        rules = None
    if not isinstance(rules, list) or not rules:
        return False, "no active rules returned for %s" % branch
    kinds = set(str(rule.get("type")) for rule in rules if isinstance(rule, dict))
    missing = sorted(set(["update", "deletion"]) - kinds)
    return (not missing), ("rules ok" if not missing else "missing active rules: %s" % ", ".join(missing))


ADMIN_ROLE_ACTOR_ID = 5  # GitHub's repository admin role


def ruleset_checks(svc: Services, url: str, exposed: bool) -> List[Check]:
    """Ruleset on ``ffs/cloud/**`` and its bypass actors (14.6)."""
    repo = parse_github(url)
    level_missing = FAIL if exposed else WARN
    if repo is None:
        return [Check("RULESET", level_missing, "origin is not a GitHub remote")]
    base = "repos/%s/%s/rulesets" % (repo[0], repo[1])
    try:
        rc, text = run_gh(svc, ["api", base])
        listing = json.loads(text) if rc == 0 else None
    except (ValueError, OSError):
        listing = None
    if not isinstance(listing, list):
        return [Check("RULESET", level_missing, "rulesets could not be read (gh api failed)")]
    matching = []
    for item in listing:
        detail = _ruleset_detail(svc, base, item)
        if detail is not None and _covers_cloud_branches(detail):
            matching.append(detail)
    if not matching:
        return [Check("RULESET", level_missing, "no ruleset covers ffs/cloud/**")]
    checks = [Check("RULESET", OK, "%d ruleset(s) cover ffs/cloud/**" % len(matching))]
    bypass = sorted(set(_foreign_bypass(matching)))
    if bypass:
        checks.append(Check("RULESET_BYPASS", FAIL, "bypass actors other than the repository admin role: %s" % ", ".join(bypass)))
    return checks


def _ruleset_detail(svc: Services, base: str, item: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict) or not isinstance(item.get("id"), int):
        return None
    try:
        rc, text = run_gh(svc, ["api", "%s/%d" % (base, item["id"])])
        detail = json.loads(text) if rc == 0 else None
    except (ValueError, OSError):
        return None
    return detail if isinstance(detail, dict) else {"_unreadable": True}


def _covers_cloud_branches(detail: Dict[str, Any]) -> bool:
    if detail.get("_unreadable") or detail.get("enforcement") != "active":
        return False
    names = config_section(config_section(detail, "conditions"), "ref_name").get("include", [])
    return any(str(n) in ("~ALL", "refs/heads/ffs/cloud/**", "refs/heads/ffs/**") for n in names or [])


def _foreign_bypass(rulesets: Sequence[Dict[str, Any]]) -> List[str]:
    found: List[str] = []
    for detail in rulesets:
        actors = detail.get("bypass_actors")
        if not isinstance(actors, list):
            found.append("unreadable")
            continue
        for actor in actors:
            is_admin = (isinstance(actor, dict) and actor.get("actor_type") == "RepositoryRole"
                        and actor.get("actor_id") == ADMIN_ROLE_ACTOR_ID)
            if not is_admin:
                found.append("%s:%s" % (actor.get("actor_type"), actor.get("actor_id")) if isinstance(actor, dict) else "?")
    return found


def doctor_checks(svc: Services, root: Path, *, tasks: Optional[Sequence[Dict[str, Any]]] = None,
                  write_cache: bool = True, base_sha: Optional[str] = None) -> List[Check]:
    """Section 10 checks; green means no ``FAIL``. ``start`` passes the sha it pinned so the commit doctor
    validates is the commit admission pushes; standalone doctor reads HEAD."""
    if base_sha is None:
        base_sha = rev_parse(svc, root, "HEAD")
    config = load_config(svc, root, base_sha) if base_sha else {}
    url = git_text(svc, root, ["remote", "get-url", ORIGIN_NAME]) or ""
    if origin_has_credentials(url):
        # refuse before any subprocess receives the URL (ls-remote would put it in argv)
        return [Check("ORIGIN_HAS_CREDENTIALS", FAIL,
                      "the origin URL embeds credentials; use a credential-free URL and a credential helper")]
    checks: List[Check] = []
    checks.extend(_provider_checks(svc, config))
    checks.append(_origin_check(url))
    checks.extend(_switch_checks())
    checks.append(_sandbox_check(svc))
    checks.append(_review_script_check(svc, root))
    checks.extend(_base_checks(svc, root, base_sha, url))
    checks.extend(_consumer_checks(svc, root, base_sha, config))
    checks.append(_reconcile_checks(svc, root))
    checks.append(_reconcile_agent_check(svc, root))
    if tasks is not None:
        checks.append(_secret_declared_check(config, tasks))
    exposed = any(exposure_names(config, p) for p in PROVIDERS)
    checks.extend(ruleset_checks(svc, url, exposed))
    if base_sha and provider_state(svc, config)["claude"][0]:
        checks.append(_history_check(svc, root, base_sha, config, write_cache))
    return checks


def _provider_checks(svc: Services, config: Dict[str, Any]) -> List[Check]:
    checks: List[Check] = []
    state = provider_state(svc, config)
    for name in PROVIDERS:
        ok, reason = state[name]
        if ok:
            checks.append(Check("PROVIDER_" + name.upper(), OK, "binary found, no API-key variable set"))
            checks.append(_login_check(svc, name))
        elif reason.startswith("API_KEY_BACKEND"):
            checks.append(Check("API_KEY_BACKEND", FAIL, reason))
        else:
            checks.append(Check("PROVIDER_" + name.upper(), WARN, "%s unavailable: %s" % (name, reason)))
    if not any(ok for ok, _ in state.values()) and not any(c.level == FAIL for c in checks):
        checks.append(Check("NO_PROVIDER", FAIL, "no cloud provider is usable"))
    return checks


LOGIN_PROBES = {"claude": ("auth", "status"), "codex": ("login", "status")}
LOGIN_PROBE_TIMEOUT_S = 15


def _login_check(svc: Services, name: str) -> Check:
    """Best-effort login probe: ``<cli> auth status`` / ``<cli> login status`` when ``--help`` lists it.

    Unknown is never a FAIL: no such subcommand, a failing probe or a timeout is ``WARN LOGIN_UNVERIFIED``."""
    binary = cloud_host.resolve_binary(name)
    if not binary:
        return Check("LOGIN_UNVERIFIED", WARN, "%s: no login probe, binary path unknown" % name)
    group, verb = LOGIN_PROBES[name]
    env = remote_env()
    helped = svc.run([str(binary), group, "--help"], timeout=LOGIN_PROBE_TIMEOUT_S, env=env)
    if not helped.ok or verb not in (helped.text() + helped.err.decode("utf-8", "replace")):
        return Check("LOGIN_UNVERIFIED", WARN, "%s: this CLI has no `%s %s`; run it yourself to confirm the login" % (
            name, group, verb))
    probe = svc.run([str(binary), group, verb], timeout=LOGIN_PROBE_TIMEOUT_S, env=env)
    if probe.ok:
        return Check("LOGIN_" + name.upper(), OK, "`%s %s` succeeded" % (group, verb))
    return Check("LOGIN_UNVERIFIED", WARN, "%s: `%s %s` exited %d, so a login is not confirmed" % (
        name, group, verb, probe.rc))


def _review_script_check(svc: Services, root: Path) -> Check:
    """The cross-vendor review needs ``adversary-host.sh`` (sourced by cloud-review.sh); without it every
    review would come back unavailable at run time, so cloud stays off instead."""
    host = find_script(root, "adversary-host.sh")
    reviewer = svc.review_script or (str(find_script(root, "cloud-review.sh")) if find_script(root, "cloud-review.sh") else "")
    if host is None or not reviewer:
        missing = [n for n, found in (("adversary-host.sh", host), ("cloud-review.sh", reviewer)) if not found]
        return Check("REVIEW_UNAVAILABLE", FAIL, "not found next to the library: %s" % ", ".join(missing))
    return Check("REVIEW_SCRIPTS", OK, "cloud-review.sh and adversary-host.sh found")


def _origin_check(url: str) -> Check:
    if not url:
        return Check("ORIGIN_GITHUB", FAIL, "no origin remote")
    if parse_github(url) is not None:
        return Check("ORIGIN_GITHUB", OK, "origin is a GitHub remote")
    if os.environ.get(ENV_ALLOW_NON_GITHUB) == "1":
        return Check("ORIGIN_GITHUB", WARN, "origin is not GitHub (%s=1)" % ENV_ALLOW_NON_GITHUB)
    return Check("ORIGIN_GITHUB", FAIL, "origin is not a GitHub remote; cloud providers can only clone GitHub")


def _switch_checks() -> List[Check]:
    checks = []
    if os.environ.get("FFS_LIFECYCLE", "on") == "off":
        checks.append(Check("LIFECYCLE_OFF", FAIL, "FFS_LIFECYCLE=off: the orchestrator cannot be registered"))
    else:
        checks.append(Check("LIFECYCLE", OK, "lifecycle records enabled"))
    if os.environ.get("FFS_RECONCILE", "on") == "off":
        checks.append(Check("RECONCILE_OFF", WARN, "FFS_RECONCILE=off: a dead orchestrator needs a manual --resume"))
    return checks


def _sandbox_check(svc: Services) -> Check:
    probe = svc.sandbox_available
    if probe is None:
        import cloud_sandbox
        probe = cloud_sandbox.sandbox_available
    if probe():
        return Check("SANDBOX", OK, "sandbox-exec usable")
    return Check("SANDBOX", FAIL, "sandbox-exec unavailable: result tests cannot run")


def _base_checks(svc: Services, root: Path, base_sha: Optional[str], url: str) -> List[Check]:
    if not base_sha:
        return [Check("BASE", FAIL, "no commit at HEAD")]
    dirty = git_text(svc, root, ["status", "--porcelain", "--untracked-files=no"])
    checks = [Check("BASE_CLEAN", FAIL if dirty else OK, "tracked files modified" if dirty else "clean")]
    ok, refs = ls_remote(svc, url, "refs/heads/*") if url else (False, {})
    pushed = ok and any(oid == base_sha or is_ancestor(svc, root, base_sha, oid) for oid in refs.values())
    checks.append(Check("BASE_PUSHED", OK if pushed else FAIL,
                        "base is on origin" if pushed else "base commit %s is not on origin" % base_sha[:12]))
    return checks


def _consumer_checks(svc: Services, root: Path, base_sha: Optional[str], config: Dict[str, Any]) -> List[Check]:
    if not base_sha:
        return []
    listing = git_text(svc, root, ["ls-tree", "-r", "--name-only", base_sha, "--", CLOUD_DIR_REL]) or ""
    present = set(listing.splitlines())
    missing = [f for f in CLOUD_FILES if "%s/%s" % (CLOUD_DIR_REL, f) not in present]
    checks = [Check("CLOUD_FILES", FAIL if missing else OK,
                    "missing in base: %s (run `env init`, review, commit)" % ", ".join(missing) if missing else "present in base")]
    settings = git(svc, root, ["show", "%s:%s" % (base_sha, SETTINGS_REL)])
    hooked = settings.ok and ".ffs/cloud/session-start.sh" in settings.text()
    checks.append(Check("SESSION_HOOK", OK if hooked else WARN,
                        "SessionStart entry committed" if hooked else "no SessionStart entry in the committed settings"))
    verify_cmd = str(config_section(config, "verify").get("cmd") or "").strip()
    checks.append(Check("VERIFY_CMD", OK if verify_cmd else WARN,
                        "config.verify.cmd set" if verify_cmd else "config.verify.cmd is empty (tasks need their own verify)"))
    return checks


def _reconcile_checks(svc: Services, root: Path) -> Check:
    script = find_script(root, "reconcile.sh")
    if script is None:
        return Check("RECONCILE_SCRIPT", WARN, "reconcile.sh not installed: restarts need a manual --resume")
    try:
        text = script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    if "mapfile" in text:
        return Check("RECONCILE_MAPFILE", WARN, "installed reconcile.sh uses mapfile (absent in bash 3.2): restarts need a manual --resume")
    return Check("RECONCILE_SCRIPT", OK, "reconcile.sh has no mapfile")


def _reconcile_agent_check(svc: Services, root: Path) -> Check:
    script = find_script(root, "reconcile-agent.sh")
    if script is None:
        return Check("RECONCILE_AGENT", WARN, "reconcile-agent.sh not installed")
    result = svc.run(["bash", str(script), "status", "--repo", str(root)], cwd=root, timeout=SCRIPT_TIMEOUT_S)
    return Check("RECONCILE_AGENT", OK if result.ok else WARN,
                 "scheduler installed" if result.ok else "reconcile scheduler not installed")


def _secret_declared_check(config: Dict[str, Any], tasks: Sequence[Dict[str, Any]]) -> Check:
    """Every needs-secret name must be declared in an exposure list.

    A needs-secret entry is free text from the tasks file, and the doctor runs before the start-time scan, so a
    pasted value could sit where a name belongs. The line therefore reports WHERE each undeclared entry is (task
    and entry position) and never the entry itself."""
    declared = set()
    for provider in PROVIDERS:
        declared.update(exposure_names(config, provider))
    undeclared: List[str] = []
    for task_pos, task in enumerate(tasks, 1):
        for entry_pos, name in enumerate(task.get("needs_secrets") or [], 1):
            if name not in declared:
                undeclared.append("task %d entry %d" % (task_pos, entry_pos))
    if not undeclared:
        return Check("SECRETS_DECLARED", OK, "all needed names declared")
    shown = "; ".join(undeclared[:10]) + ("; ..." if len(undeclared) > 10 else "")
    return Check("SECRETS_DECLARED", FAIL,
                 "%d needs-secret entr%s not declared in config.secrets.exposure (names withheld; %s)"
                 % (len(undeclared), "y" if len(undeclared) == 1 else "ies", shown))


def _history_check(svc: Services, root: Path, base_sha: str, config: Dict[str, Any], write_cache: bool) -> Check:
    allow = config_section(config, "scan").get("allow", [])
    allow = [a for a in allow if isinstance(a, dict)] if isinstance(allow, list) else []
    cache = ledger.scan_cache_path(root) if write_cache else Path(tempfile.mkdtemp(prefix="ffs-scan-")) / "scan-cache.json"
    try:
        findings = cloud_scan.scan_git_ancestry(str(root), base_sha, str(cache), allow)
    except cloud_scan.ScanRefused as exc:
        return Check("HISTORY_SECRET", FAIL, "history scan refused: %s" % exc.reason)
    finally:
        if not write_cache:
            shutil.rmtree(str(cache.parent), ignore_errors=True)
    if findings:
        return _history_findings_check(svc, root, base_sha, allow, findings)
    return Check("HISTORY_SECRET", OK, "base history scan clean")


def _history_findings_check(svc: Services, root: Path, base_sha: str, allow: Sequence[Dict[str, Any]],
                            findings: Sequence[Any]) -> Check:
    """FAIL for findings in the base history; the JSON form carries every finding, the line a prefix."""
    shown = findings[:MAX_LISTED]
    count = "%d finding%s" % (len(findings), "" if len(findings) == 1 else "s")
    if len(shown) < len(findings):
        count += ", showing first %d; use doctor --json for all" % len(shown)
    listing = "; ".join("%s@%s" % (f.cls, f.location) for f in shown)
    hint = ("scan.allow is read from the committed .ffs/cloud/config.json at %s, not the working tree: "
            "commit the allowlist, then rerun doctor" % base_sha[:12])
    try:
        working = config_section(load_config(svc, root), "scan").get("allow", [])
    except (StoreError, OSError):  # an unreadable working-tree config must not hide the finding
        working = []
    working = [a for a in working if isinstance(a, dict)] if isinstance(working, list) else []
    if working and working != list(allow):
        hint += " (the working-tree scan.allow differs and is ignored)"
    detail = "secret-shaped content in the base history (%s); %s: %s" % (count, hint, listing)
    data = {
        "base_sha": base_sha,
        "allow_source": "committed .ffs/cloud/config.json at base_sha",
        "allow_entries": len(allow),
        "findings_total": len(findings),
        "findings": [cloud_scan.describe_finding(f) for f in findings],
    }
    return Check("HISTORY_SECRET", FAIL, detail, data)


def doctor_green(checks: Sequence[Check]) -> bool:
    return not any(c.level == FAIL for c in checks)


def failing(checks: Sequence[Check]) -> str:
    return "; ".join("%s: %s" % (c.code, c.detail) for c in checks if c.level == FAIL)


# --------------------------------------------------------------------------
# Planning (plan, start)
# --------------------------------------------------------------------------

REFUSAL_CODES = {
    "RUN_LIFECYCLE_DISABLED": "LIFECYCLE_DISABLED",
    "RUN_MANAGED_INGRESS": "MANAGED_INGRESS",
    "RUN_TOKEN_BUDGET": "TOKEN_BUDGET",
    "RUN_DIRTY_BASE": "DIRTY_BASE",
    "RUN_DOCTOR_NOT_GREEN": "DOCTOR_NOT_GREEN",
}
ROUTE_REFUSED = frozenset(
    [delegation_router.DEP_UNKNOWN, delegation_router.DEP_CYCLE, delegation_router.CLOUD_REQUIRED_NOT_ELIGIBLE,
     delegation_router.TASK_ID_DUPLICATE]
)
ROUTE_STORE = frozenset([delegation_router.CONFIG_INVALID])


def route_error(exc: delegation_router.RouteError) -> DispatchError:
    text = str(exc)
    found = scan_text(text, "route-error")
    if found:  # the router quotes caller-supplied task ids and dependency names, and this prints before any id scan: name the class, never the value
        text = "an id or name in the task list matched the secret scanner: %s" % ", ".join(found)
    if exc.code in ROUTE_REFUSED:
        return Refused(exc.code, text)
    if exc.code in ROUTE_STORE:
        return StoreError(exc.code, text)
    return UsageError(exc.code, text)


def read_tasks_input(path: str) -> List[Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise UsageError("BAD_TASKS_FILE", "cannot read %s as JSON" % path)
    if isinstance(raw, dict) and isinstance(raw.get("tasks"), list):
        raw = raw["tasks"]
    if not isinstance(raw, list) or not raw:
        raise UsageError("BAD_TASKS_FILE", "the tasks file must be a non-empty JSON list")
    return raw


def run_refusals(svc: Services, root: Path) -> List[str]:
    """Run-level refusal codes that keep every task local (contract 2.2 rule 2)."""
    codes: List[str] = []
    if os.environ.get("FFS_LIFECYCLE", "on") == "off":
        codes.append("RUN_LIFECYCLE_DISABLED")
    ingress = os.environ.get(ENV_MANAGED_INGRESS, "")
    if ingress and ingress != "0":
        codes.append("RUN_MANAGED_INGRESS")
    if os.environ.get(ENV_TOKEN_BUDGET, "").strip():
        codes.append("RUN_TOKEN_BUDGET")
    if git_text(svc, root, ["status", "--porcelain", "--untracked-files=no"]):
        codes.append("RUN_DIRTY_BASE")
    return codes


def route(svc: Services, root: Path, tasks: List[Dict[str, Any]], *, policy: str, run: Optional[str],
          config: Dict[str, Any], doctor: str, refusals: List[str], grants: Optional[List[str]]) -> Dict[str, Any]:
    state = provider_state(svc, config)
    try:
        return delegation_router.plan_routes(
            tasks, policy=policy, config=config, providers_available=[p for p, (ok, _) in state.items() if ok],
            slots_free=slots_free(svc, root, config), host_pressure_high=host_pressure_high(svc, root),
            grants=grants, run_refusals=refusals, run=run, doctor=doctor)
    except delegation_router.RouteError as exc:
        raise route_error(exc)


def normalize(raw: List[Any], sequential: bool) -> List[Dict[str, Any]]:
    try:
        return delegation_router.normalize_tasks(raw, sequential=sequential)
    except delegation_router.RouteError as exc:
        raise route_error(exc)


def cmd_plan(svc: Services, args: argparse.Namespace) -> int:
    """Pure: reads the tasks file, config, grants store and host state; writes nothing."""
    root = resolve_root(svc, args.root)
    if args.run is not None:
        validate_run_id(args.run)
    tasks = normalize(read_tasks_input(args.tasks), args.sequential)
    _scan_task_ids(tasks)  # the plan JSON prints every id: refuse a scanner-positive one before start would
    config = load_config(svc, root)
    refusals = run_refusals(svc, root)
    doctor = "unknown"
    if args.with_doctor:
        green = doctor_green(doctor_checks(svc, root, tasks=tasks, write_cache=False))
        doctor = "green" if green else "not-green"
        if not green:
            refusals.append("RUN_DOCTOR_NOT_GREEN")
    plan = route(svc, root, tasks, policy=args.policy, run=args.run, config=config, doctor=doctor,
                 refusals=refusals, grants=None)
    if args.run is not None:
        required = plan["preconditions"]["grants_missing"]
        try:
            granted: List[str] = granted_set(svc, args.run, required)
        except Refused:  # an unreadable grants store counts as "nothing granted" for a read-only plan
            granted = []
        plan = route(svc, root, tasks, policy=args.policy, run=args.run, config=config, doctor=doctor,
                     refusals=refusals, grants=granted)
    svc.emit(json.dumps(plan, sort_keys=True))
    return EXIT_OK


def cloud_records(plan: Dict[str, Any], tasks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The cloud-routed tasks for ``tasks.json``: normalized task plus the routing outcome."""
    records = []
    for task in tasks:
        entry = plan["tasks"][str(task["id"])]
        if entry["route"] != "cloud":
            continue
        record = copy.deepcopy(dict(task))
        record.update({"provider": entry["provider"], "model": entry["model"], "effort": entry["effort"],
                       "provenance": entry["provenance"], "depends_on": list(entry["depends_on"])})
        records.append(record)
    return records


def packet_for(record: Dict[str, Any], *, run: str, generation: int, nonce: str, base_sha: str,
               config: Dict[str, Any]) -> str:
    provider = record["provider"]
    result_branch = ledger.claude_result_branch(run, str(record["id"]), nonce) if provider == "claude" else None
    try:
        return handoff_packet.build_packet(record, run=run, generation=generation, nonce=nonce, base_sha=base_sha,
                                           provider=provider, config=config, result_branch=result_branch)
    except handoff_packet.PacketError as exc:
        raise Refused("PACKET_INVALID", str(exc))


def normalize_dependency_report(data: Any) -> Optional[Dict[str, Any]]:
    """The collect dependency report with ``packages`` (list), ``unattributed`` (bool) and ``report_digest`` (hex).

    The collector supplies all three; anything missing is treated as unattributed (a lockfile-only or
    ``postinstall``-only change names no package), and a missing digest is computed over the report itself so
    the operator still has something exact to grant."""
    if not isinstance(data, dict):
        return None
    deps = data.get("dependencies") if isinstance(data.get("dependencies"), dict) else (
        data if ("packages" in data or "report_digest" in data) else None)
    if deps is None:
        return None
    report = copy.deepcopy(dict(deps))
    packages = report.get("packages")
    complete = isinstance(packages, list) and "unattributed" in report and isinstance(report.get("report_digest"), str)
    report["packages"] = [str(n) for n in packages] if isinstance(packages, list) else []
    report["unattributed"] = bool(report.get("unattributed")) if complete else True
    digest = report.get("report_digest")
    if not isinstance(digest, str) or not DIGEST_RE.match(digest):
        body = json.dumps({k: v for k, v in report.items() if k != "report_digest"}, sort_keys=True, default=str)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    report["report_digest"] = digest
    return report


def scan_labels(findings: Iterable[Any]) -> List[str]:
    return ["%s@%s" % (f.cls, f.location) for f in findings][:MAX_LISTED]


def scan_text(text: str, label: str) -> List[str]:
    """Finding labels for ``text``; a scanner failure is itself a finding (refuse)."""
    try:
        return scan_labels(cloud_scan.scan_bytes(text.encode("utf-8", "replace"), label=label))
    except cloud_scan.ScanRefused as exc:
        return ["scan-refused:%s" % exc.reason]


def check_start_grants(svc: Services, run: str, plan: Dict[str, Any]) -> List[str]:
    missing = list(plan["preconditions"]["grants_missing"])
    if not missing:
        return []
    return [key for key in missing if key not in set(granted_set(svc, run, missing))]


def refuse_start_grants(missing: List[str]) -> None:
    exposure = [k for k in missing if k.startswith("secret-use:")]
    if exposure:
        raise Refused("EXPOSURE_NOT_GRANTED", "missing grants: %s" % ", ".join(missing))
    raise Refused("GRANT_MISSING", "missing grants: %s" % ", ".join(missing))


_ROUTING_KEYS = ("provider", "model", "effort", "provenance")


def _batch_record(tasks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Every id of the batch (cloud and local) and a digest of its ordered content.

    The digest covers the normalized input in input order, ``depends_on`` included (so ``--sequential`` and a new
    local-to-cloud dependency change it); only the routing outcome keys are left out."""
    core = [{k: v for k, v in t.items() if k not in _ROUTING_KEYS} for t in tasks]
    body = json.dumps(core, sort_keys=True, default=str, separators=(",", ":"))
    return {"ids": sorted(str(t["id"]) for t in tasks), "digest": hashlib.sha256(body.encode("utf-8")).hexdigest()}


def _run_exists_batch(svc: Services, root: Path, run: str, raw: List[Any], sequential: bool) -> str:
    """``batch=same [local-claimed=ids]`` only when the retried batch is exactly the one run ``run`` started, else
    ``batch=other|unknown``.

    The ledger holds the cloud-routed tasks; the first attempt's full id set and content digest sit beside them,
    so a superset or an edited batch is ``other``.  ``local-claimed`` lists the local tasks the first attempt
    claimed before it printed them: a retry never offers them as ``local=`` again, because that caller may already
    have started them.  A ledger without those records, or anything unreadable, is ``unknown``: the caller must
    not start the tasks again."""
    try:
        tasks = normalize(raw, sequential)
        doc = ledger.read(ledger.run_ledger_path(root, run))
        ledger.load_tasks_file(ledger.tasks_path(root, run), doc["tasks_sha256"])
        same = doc["batch"] == _batch_record(tasks)
        claimed = [str(i) for i in doc["local_claimed"]]
    except (DispatchError, ledger.LedgerError, OSError, ValueError, KeyError, TypeError):
        return "batch=unknown"
    if not same:
        return "batch=other"
    return "batch=same" + (" local-claimed=%s" % ",".join(claimed) if claimed else "")


def cmd_start(svc: Services, args: argparse.Namespace) -> int:
    run = validate_run_id(args.run)
    root = resolve_root(svc, args.root)
    if args.policy not in POLICIES:
        raise UsageError("BAD_POLICY", "policy must be one of %s" % ", ".join(POLICIES))
    raw = read_tasks_input(args.tasks)
    if ledger.run_ledger_path(root, run).exists():
        raise _run_exists(svc, root, run, raw, args.sequential)
    url = origin_url(svc, root)  # credentials in the URL are refused before any subprocess receives it
    refusals = run_refusals(svc, root)
    if refusals:
        raise Refused(REFUSAL_CODES.get(refusals[0], refusals[0]), "run-level refusal: %s" % ", ".join(refusals))
    base_sha = rev_parse(svc, root, "HEAD")
    if base_sha is None:
        raise Refused("NO_BASE", "the repository has no commit at HEAD")
    config = load_config(svc, root, base_sha)
    tasks = normalize(raw, args.sequential)
    _scan_task_ids(tasks)  # every id (local ones too) is stored in the ledger and printed
    checks = doctor_checks(svc, root, tasks=tasks, base_sha=base_sha)
    if not doctor_green(checks):
        raise Refused("DOCTOR_NOT_GREEN", failing(checks))
    plan = route(svc, root, tasks, policy=args.policy, run=run, config=config, doctor="green",
                 refusals=[], grants=None)
    missing = check_start_grants(svc, run, plan)
    if missing:
        refuse_start_grants(missing)
    records = cloud_records(plan, tasks)
    if not records:
        raise Refused("NO_CLOUD_TASKS", "nothing routes to the cloud: %s" % _local_reasons(plan))
    _scan_start_inputs(records, run=run, base_sha=base_sha, config=config)
    local = [str(t["id"]) for t in tasks if plan["tasks"][str(t["id"])]["route"] != "cloud"]
    sha = ledger.tasks_sha256(records)
    specs = [{"id": r["id"], "provider": r["provider"], "depends_on": r["depends_on"], "owns": r.get("owns") or [],
              "verify_cmd": effective_verify_cmd(r.get("verify_cmd"), config)}
             for r in records]
    try:
        with _orch_lock_held(root, run):  # no resume can act on the run until its initialization record is committed
            reserve_run(root, run, specs, records, url=url, policy=args.policy, base_sha=base_sha, sha=sha,
                        now=int(svc.now()), batch=_batch_record(tasks), local_claimed=local)
            _initialize_run(svc, root, run, base_sha, fresh=True)
    except Refused as exc:
        if exc.code != "RUN_EXISTS":
            raise
        raise _run_exists(svc, root, run, raw, args.sequential)  # lost the race: another start holds or made the run
    svc.emit("%sstarted run=%s tasks=%d" % (PREFIX, run, len(records)))
    svc.emit("%slocal tasks=%s" % (PREFIX, ",".join(local)))  # always printed: an absent line is "unparseable", not "none"
    if args.foreground:
        return Orchestrator(svc, root, run).run()
    pid = svc.spawn(orchestrator_argv(root, run, resume=False), ledger.run_dir(root, run) / "orchestrate.log", root)
    svc.emit("%sorchestrator-started pid=%d" % (PREFIX, pid))
    return EXIT_OK


@contextlib.contextmanager
def _orch_lock_held(root: Path, run: str) -> Any:
    """Hold the run's orchestrator lock (non-blocking) for the body.  Busy means another start is initializing the
    run or an orchestrator owns it: ``RUN_EXISTS``.  The caller releases it (by leaving the body) before an
    orchestrator is spawned, or that orchestrator would find the lock held and exit."""
    path = ledger.orch_lock_path(root, run)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise IoRefused("STORE_IO", "cannot open the orchestrator lock: %s" % exc.strerror)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refused("RUN_EXISTS", "run %s is being started or is running" % run)
        yield
    finally:
        os.close(fd)


def _mark_initialized(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc["initialized"] = True
    doc["integration_ref_created"] = True  # written with the record, only after this start's update-ref succeeded
    return doc


def _integration_ref_removal_advice(svc: Services, root: Path, ref: str, oid: str) -> str:
    """How an operator may remove a leftover integration ref without touching anything else.  A symbolic ref is
    followed by ``rev-parse`` and by a plain ``update-ref -d``, so for one the advice is to inspect the target and
    remove only the symbolic ref; a plain ref is deleted with ``--no-deref`` at the inspected OID."""
    target = git(svc, root, ["symbolic-ref", "-q", ref])
    if target.ok:
        return ("it is a symbolic ref to %s: inspect that target, and remove only the symbolic ref itself "
                "(`git symbolic-ref --delete %s`)" % (_listable(target.text().strip()) or "another ref", ref))
    return "inspect it and delete it by hand (`git update-ref --no-deref -d %s %s`)" % (ref, oid)


def _initialize_run(svc: Services, root: Path, run: str, tip: str, *, fresh: bool) -> None:
    """Create the integration ref and commit the initialization record (``initialized``), under the orchestrator lock.

    The ref must not exist yet.  A ref found there is never adopted, not even one at ``tip``: the ledger proves a
    creation only through ``integration_ref_created``, written with the initialization record after the update-ref
    succeeded, so a ref left by a crash (or by somebody else) cannot be told from one this start made.  A ref that
    cannot be created rolls the reservation back (nothing else has acted on an uninitialized run, and the lock is
    ours), freeing the run id.  ``fresh`` only picks the message."""
    ref = ledger.integration_ref(run)
    created = git(svc, root, ["update-ref", "--no-deref", ref, tip, ""]).ok
    if not created:
        _release_reservation(root, run, tasks_written=True)
        found = rev_parse(svc, root, ref)
        if not fresh and found is not None:
            raise StoreError("INTEGRATION_REF_UNCONFIRMED", "%s already exists and nothing proves this run created it "
                             "(a crashed start, or another party); %s, then start again"
                             % (ref, _integration_ref_removal_advice(svc, root, ref, found)))
        raise StoreError("INTEGRATION_REF", "could not create %s" % ref)
    try:
        ledger.locked_update(ledger.run_ledger_path(root, run), _mark_initialized)
    except ledger.LedgerError as exc:
        raise classify_ledger_error(exc)


def _finish_start(svc: Services, root: Path, run: str) -> None:
    """A start that died after reserving the run but before its initialization record: finish it under the lock."""
    with _orch_lock_held(root, run):
        doc = ledger.read(ledger.run_ledger_path(root, run))
        if not doc.get("initialized"):
            ledger.load_tasks_file(ledger.tasks_path(root, run), doc["tasks_sha256"])
            _initialize_run(svc, root, run, doc["integration"]["tip"], fresh=False)


def _merge_created(doc: Dict[str, Any], seq: int, flags: Dict[str, bool]) -> Dict[str, Any]:
    """Add the ``True`` creation proofs in ``flags`` to the journal entry ``seq``; a proof is never taken back."""
    for entry in doc.get("journal", []):
        if entry.get("seq") == seq:
            merged = dict(entry.get("created") or {})
            merged.update({key: True for key, value in flags.items() if value is True})
            entry["created"] = merged
    return doc


class _RestartFailed(Exception):
    """The spawn of a restarted orchestrator raised ``OSError``; ``cause`` is its errno name, or the fixed token
    ``OSERROR`` when it has none (never the message, which can carry a path or a command line)."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


def _errno_name(exc: OSError) -> str:
    name = errno.errorcode.get(exc.errno) if isinstance(exc.errno, int) else None
    return name if name and re.fullmatch(r"E[A-Z0-9]+", name) else "OSERROR"


def _restart_unowned_run(svc: Services, root: Path, run: str) -> bool:
    """An initialized, active run that no process owns (its start died before the spawn, or its orchestrator died
    before registering a lifecycle record reconcile could see) gets an ``orchestrate --resume``, which takes the
    orchestrator lock itself.  A parked run stays parked, and a held lock means a live owner."""
    doc = ledger.read(ledger.run_ledger_path(root, run))
    if not doc.get("initialized") or doc["state"] != ledger.RUN_ACTIVE or not probe_lock_free(root, run):
        return False
    try:
        svc.spawn(orchestrator_argv(root, run, resume=True), ledger.run_dir(root, run) / "orchestrate.log", root)
    except OSError as exc:  # no process was started and nothing was recorded: say so, never look like an ordinary retry
        raise _RestartFailed(_errno_name(exc)) from exc
    return True


def _run_exists(svc: Services, root: Path, run: str, raw: List[Any], sequential: bool) -> Refused:
    """The ``RUN_EXISTS`` refusal with its ``batch=`` classification; an identical retry of a start that died before
    its initialization record finishes that start first."""
    tokens = _run_exists_batch(svc, root, run, raw, sequential)
    if tokens.startswith("batch=same"):
        try:
            _finish_start(svc, root, run)
            if _restart_unowned_run(svc, root, run):
                tokens += " orchestrator=restarted"
        except _RestartFailed as exc:  # the claims and the ledger are untouched: the same retry tries again
            tokens += " orchestrator=restart-failed cause=%s" % exc.cause
        except (Refused, ledger.LedgerError, OSError):
            pass  # busy (an in-flight start or a live orchestrator) or unreadable: the classification stands
    return Refused("RUN_EXISTS", "run %s already has a ledger: read it with `status --run %s`; never run its tasks "
                                 "locally; %s" % (run, run, tokens))


def _release_reservation(root: Path, run: str, *, tasks_written: bool) -> None:
    """Undo this invocation's reservation under the run lock: its ``tasks.json`` (only if it wrote one) first and the
    ledger last, because the ledger is what holds the run id.  The lock file stays: a concurrent start may hold or
    wait on it, and it is that start's files that a different order or an unlinked lock would hit."""
    path = ledger.run_ledger_path(root, run)
    try:
        fd = os.open(str(ledger.lock_path_for(path)), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        return  # fails closed: the run id stays reserved rather than risk another start's files
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        for leftover in ([ledger.tasks_path(root, run)] if tasks_written else []) + [path]:
            try:
                os.unlink(str(leftover))
            except OSError:
                pass
    finally:
        os.close(fd)


def reserve_run(root: Path, run: str, specs: List[Dict[str, Any]], records: List[Dict[str, Any]], *, url: str,
                policy: str, base_sha: str, sha: str, now: int, batch: Dict[str, Any],
                local_claimed: Sequence[str] = ()) -> None:
    """Reserve the run id by creating its ledger exclusively, and only then write ``tasks.json``.

    Two concurrent ``start`` calls for one run id used to overwrite each other's ``tasks.json`` before the
    ledger picked a winner.  Now the loser's exclusive create fails (``RUN_EXISTS``, exit 3) before it writes
    anything; the winner's ledger already carries the hash of the file it writes next.  A failure writing
    the file releases the reservation again so the id is not left reserved by a run that cannot start."""
    path = ledger.run_ledger_path(root, run)
    try:
        doc = ledger.new_run(run, base_sha=base_sha, origin=ORIGIN_NAME, origin_url=url, policy=policy,
                             tasks=specs, tasks_sha256=sha, created_at=now)
        doc["batch"] = batch  # the first attempt's whole batch: a retried start is compared with it
        doc["local_claimed"] = list(local_claimed)  # claimed before any caller is told to run them locally
        ledger.create_run_ledger(path, doc)
    except ledger.LedgerError as exc:
        if exc.code == ledger.E_EXISTS:
            raise Refused("RUN_EXISTS", "run %s already has a ledger" % run)
        raise classify_ledger_error(exc)
    except OSError as exc:
        raise IoRefused("STORE_IO", "cannot create the run ledger: %s" % exc.strerror)
    try:
        ledger.write_tasks_file(ledger.tasks_path(root, run), records)
    except (OSError, ledger.LedgerError) as exc:
        _release_reservation(root, run, tasks_written=False)
        if isinstance(exc, ledger.LedgerError):
            raise classify_ledger_error(exc)
        raise IoRefused("STORE_IO", "cannot write tasks.json: %s" % exc.strerror)


def _local_reasons(plan: Dict[str, Any]) -> str:
    return "; ".join("%s: %s" % (tid, ",".join(entry["reasons"])) for tid, entry in plan["tasks"].items())


def _scan_task_ids(tasks: Sequence[Dict[str, Any]]) -> None:
    for task in tasks:
        found = scan_text(str(task["id"]), "task-id")
        if found:  # name the class, never the id
            raise Refused("SECRET_IN_PROMPT", "a task id matched the secret scanner: %s" % ", ".join(found))


def _scan_start_inputs(records: Sequence[Dict[str, Any]], *, run: str, base_sha: str, config: Dict[str, Any]) -> None:
    try:
        findings = scan_labels(cloud_scan.scan_text_fields(list(records), label="task"))
    except cloud_scan.ScanRefused as exc:
        findings = ["scan-refused:%s" % exc.reason]
    if findings:
        raise Refused("SECRET_IN_PROMPT", "task text matched the secret scanner: %s" % ", ".join(findings))
    for record in records:
        packet = packet_for(record, run=run, generation=1, nonce=PLACEHOLDER_NONCE, base_sha=base_sha, config=config)
        found = scan_text(packet, "packet:%s" % record["id"])
        if found:
            raise Refused("SECRET_IN_PACKET", "the worker packet matched the secret scanner: %s" % ", ".join(found))


def orchestrator_argv(root: Path, run: str, *, resume: bool, wait_lock: int = 0) -> List[str]:
    argv = [sys.executable or "python3", str(Path(__file__).resolve()), "orchestrate", "--run", run, "--root", str(root)]
    argv += ["--resume"] if resume else []
    return argv + (["--wait-lock", str(wait_lock)] if wait_lock > 0 else [])


# --------------------------------------------------------------------------
# doctor, env init, env-spec
# --------------------------------------------------------------------------


def cmd_doctor(svc: Services, args: argparse.Namespace) -> int:
    root = resolve_root(svc, args.root)
    checks = doctor_checks(svc, root)
    green = doctor_green(checks)
    if args.json:
        svc.emit(json.dumps({"green": green, "checks": [c.as_dict() for c in checks]}, sort_keys=True))
    else:
        for check in checks:
            svc.emit("%sdoctor %s %s %s" % (PREFIX, check.level, check.code, check.detail))
        svc.emit("%sdoctor %s" % (PREFIX, "green" if green else "not-green"))
    return EXIT_OK if green else EXIT_REFUSED


def _probe_seconds(value: Any) -> float:
    """Clamp to 1..60 s; ``nan`` and ``inf`` must never remove the deadline."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return cloud_host.PROBE_TIMEOUT_S
    if math.isnan(seconds):
        return cloud_host.PROBE_TIMEOUT_S
    return min(max(seconds, 1.0), 60.0)  # +-inf clamp to the bounds


def cmd_probe_claude(svc: Services, args: argparse.Namespace) -> int:
    """Operator probe: start the plain ``claude`` binary (no ``--cloud``, no prompt) on a pty and report the first
    screen. Creates no cloud session and sends no keystroke."""
    target = Path(args.dir).resolve()
    if not target.is_dir():
        raise Refused("PROBE_DIR_MISSING", "--dir is not a directory")
    report = cloud_host.probe_claude_first_screen(target, timeout=_probe_seconds(args.seconds), sanitize=cloud_scan.redact)
    svc.emit(json.dumps(report, sort_keys=True))
    return EXIT_OK if report.get("started") else EXIT_REFUSED


def template_dir() -> Path:
    for candidate in (LIB_DIR / "cloud-templates", LIB_DIR.parent / "templates" / "cloud"):
        if candidate.is_dir():
            return candidate
    raise StoreError("TEMPLATES_MISSING", "no cloud-templates directory next to the library")


def session_hook_entry() -> Dict[str, Any]:
    return {"hooks": [{"type": "command", "command": SESSION_HOOK_COMMAND, "timeout": SESSION_HOOK_TIMEOUT_S}]}


def _merge_hook(svc: Services) -> Callable[..., Dict[str, Any]]:
    if svc.merge_hook_entry is not None:
        return svc.merge_hook_entry
    try:
        import operator_guard_hook
        return getattr(operator_guard_hook, "merge_hook_entry")
    except (ImportError, AttributeError):
        raise StoreError("HOOK_API_MISSING", "lib/operator_guard_hook.py has no merge_hook_entry")


def register_session_hook(svc: Services, root: Path) -> Path:
    path = root / SETTINGS_REL
    try:
        document = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        raise StoreError("SETTINGS_INVALID", "%s is not valid JSON" % SETTINGS_REL)
    if not isinstance(document, dict):
        raise StoreError("SETTINGS_INVALID", "%s must be an object" % SETTINGS_REL)
    merged = _merge_hook(svc)(document, "SessionStart", session_hook_entry(), None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    return path


def cmd_env_init(svc: Services, args: argparse.Namespace) -> int:
    try:
        return _env_init(svc, args)
    except OSError as exc:
        raise IoRefused("STORE_IO", "env init failed: %s (%s)" % (exc.strerror or type(exc).__name__, exc.filename or ""))


def _env_init(svc: Services, args: argparse.Namespace) -> int:
    root = resolve_root(svc, args.root)
    source = template_dir()
    target = root / CLOUD_DIR_REL
    target.mkdir(parents=True, exist_ok=True)
    written: List[str] = []
    for name in CLOUD_FILES:
        destination = target / name
        if destination.exists() and not args.force:
            svc.emit("%senv-init kept existing %s/%s (use --force to overwrite)" % (PREFIX, CLOUD_DIR_REL, name))
            continue
        shutil.copyfile(str(source / name), str(destination))
        os.chmod(str(destination), 0o700 if name in EXECUTABLE_FILES else 0o600)
        written.append("%s/%s" % (CLOUD_DIR_REL, name))
        svc.emit("%senv-init wrote %s/%s" % (PREFIX, CLOUD_DIR_REL, name))
    if args.register_hook:
        register_session_hook(svc, root)
        written.append(SETTINGS_REL)
        svc.emit("%senv-init registered the SessionStart hook in %s" % (PREFIX, SETTINGS_REL))
    if written:
        svc.emit("%senv-init review and commit (never committed automatically): git add %s" % (PREFIX, " ".join(written)))
    return EXIT_OK


ENV_SPEC_COMMON = (
    "FFS cannot select or verify the cloud environment from the CLI; these steps are done once in the provider UI.",
    "Use a private environment. List EVERY secret name reachable from it (environment variables, network-secret hosts as "
    "host:<name>, and every name the token can read) in .ffs/cloud/config.json secrets.exposure.<provider>; each name needs a "
    "secret-use:<NAME> grant before any task is submitted.",
    "Commit a GitHub ruleset on ffs/cloud/** that blocks updates and deletions for non-admins (required when any secret is exposed).",
)
ENV_SPEC = {
    "claude": (
        "Open the Claude Code web environment settings and create (or pick) a private environment for this repository.",
        "Setup script: leave empty; the committed SessionStart hook runs .ffs/cloud/setup.sh on ffs/cloud/* branches.",
        "Network access: allow the package hosts your lockfile needs; for Doppler also allow api.doppler.com and the CLI download host.",
        "Doppler: create a branch config <env>_agent, mint a token with `doppler configs tokens create ffs-cloud -p <project> "
        "-c <env>_agent --max-age 720h --plain`, paste it as DOPPLER_TOKEN into the environment, and list names with "
        "`doppler secrets --only-names`. Workers run commands as `doppler run --no-fallback -- <cmd>`.",
        "Other providers: put values in the environment's variables, or network secrets for HTTP keys.",
    ),
    "codex": (
        "Open the Codex cloud environment settings and create (or pick) a private environment for this repository.",
        "Export its id on the host that runs the dispatcher: FFS_CODEX_CLOUD_ENV=<environment id> "
        "(the variable name is config.codex.env_id_var).",
        "Secrets: the Codex Secrets store is setup-only, so workers read secrets from environment variables set on the environment.",
        "Doppler: mint a token as for Claude and set DOPPLER_TOKEN as an environment variable; allow api.doppler.com.",
    ),
}


def cmd_env_spec(svc: Services, args: argparse.Namespace) -> int:
    if args.provider not in ENV_SPEC:
        raise UsageError("BAD_PROVIDER", "provider must be claude or codex")
    steps = list(ENV_SPEC[args.provider]) + list(ENV_SPEC_COMMON)
    for index, step in enumerate(steps, start=1):
        svc.emit("%senv-spec %s %d. %s" % (PREFIX, args.provider, index, step))
    return EXIT_OK


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

OPERATOR_STATES = (ledger.NEEDS_OPERATOR, ledger.UNCERTAIN)


def read_orchestrator_pid(root: Path, run: str) -> Optional[int]:
    try:
        text = ledger.orch_lock_path(root, run).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def status_document(svc: Services, root: Path, run: str) -> Dict[str, Any]:
    try:
        doc = ledger.read(ledger.run_ledger_path(root, run))
    except ledger.LedgerError as exc:
        raise classify_ledger_error(exc)
    pid = read_orchestrator_pid(root, run)
    beat = doc.get("heartbeat_at")
    tasks: Dict[str, Any] = {}
    for name, entry in doc["tasks"].items():
        item = {
            "state": entry["state"], "provider": entry["provider"], "generation": entry["generation"],
            "url": entry.get("url"), "remote_id": entry.get("remote_id"), "branch": entry["task_branch"],
            "depends_on": entry["depends_on"], "review": entry.get("review"), "verify": entry.get("verify"),
            "blocker": entry.get("blocker"), "decisions_needed": entry.get("decisions_needed") or [],
            "candidate_oid": entry.get("candidate_oid"), "bound_oid": entry.get("bound_oid"),
            "poll_errors": entry.get("poll_errors", 0),
        }
        if entry.get("blocker") == ledger.B_SECRET_LEAK:
            item["runbook"] = cloud_collect.SECRET_LEAK_RUNBOOK
        tasks[name] = item
    return {
        "run": run, "state": doc["state"], "epoch": doc["epoch"], "policy": doc["policy"], "base_sha": doc["base_sha"],
        "origin": doc["origin"], "wake_seq": doc.get("wake_seq", 0),
        "orchestrator": {"pid": pid, "alive": pid_alive(pid), "heartbeat_at": beat,
                         "heartbeat_age_s": None if beat is None else max(0, int(svc.now()) - int(beat))},
        "integration": {"tip": doc["integration"]["tip"], "ref": ledger.integration_ref(run),
                        "history": [{"task": h["task"], "generation": h["generation"], "publish_oid": h["publish_oid"]}
                                    for h in doc["integration"]["history"]]},
        "tasks": tasks,
        "operator_items": sum(1 for t in tasks.values() if t["state"] in OPERATOR_STATES),
    }


def cmd_status(svc: Services, args: argparse.Namespace) -> int:
    run = validate_run_id(args.run)
    root = resolve_root(svc, args.root)
    document = status_document(svc, root, run)
    if args.json:
        svc.emit(json.dumps(document, sort_keys=True))
    else:
        orch = document["orchestrator"]
        svc.emit("%sstatus run=%s state=%s epoch=%d orchestrator-pid=%s alive=%s heartbeat-age-s=%s tip=%s" % (
            PREFIX, run, document["state"], document["epoch"], orch["pid"], str(orch["alive"]).lower(),
            orch["heartbeat_age_s"], document["integration"]["tip"]))
        for name, item in document["tasks"].items():
            svc.emit("%stask %s state=%s provider=%s generation=%d branch=%s url=%s blocker=%s" % (
                PREFIX, name, item["state"], item["provider"], item["generation"], item["branch"],
                item["url"], item["blocker"]))
            for decision in item["decisions_needed"]:
                svc.emit("%sdecision %s %s" % (PREFIX, name, decision.replace("\n", " ")))
            if item.get("runbook"):
                svc.emit("%srunbook %s %s" % (PREFIX, name, item["runbook"]))
        svc.emit("%sintegration ref=%s" % (PREFIX, document["integration"]["ref"]))
    return EXIT_OPERATOR if document["operator_items"] else EXIT_OK


# --------------------------------------------------------------------------
# Operator verbs: adopt, abandon, resolve, and waking the orchestrator
# --------------------------------------------------------------------------


def probe_lock_free(root: Path, run: str) -> bool:
    """True when nobody holds the orchestrator lock (acquired and released at once)."""
    path = ledger.orch_lock_path(root, run)
    if not path.exists():
        return True
    fd = os.open(str(path), os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def kick_orchestrator(svc: Services, root: Path, run: str) -> None:
    """Section 14.4: make sure an orchestrator sees the operator's mutation.

    Lock free: start ``orchestrate --resume``.  Lock held: the holder may be mid-park and about to exit, and
    parking (notify plus the lifecycle call) can take longer than any fixed wait, so spawn a detached
    ``orchestrate --resume --wait-lock 300`` that blocks on the lock and then runs.  The holder itself
    re-checks ``wake_seq`` right before exiting and un-parks if it moved (``Orchestrator.finish``)."""
    state = ledger.read(ledger.run_ledger_path(root, run))["state"]
    if state == ledger.RUN_DONE:
        svc.emit("%sorchestrator-not-needed run=%s state=done" % (PREFIX, run))
        return
    log = ledger.run_dir(root, run) / "orchestrate.log"
    if probe_lock_free(root, run):
        pid = svc.spawn(orchestrator_argv(root, run, resume=True), log, root)
        svc.emit("%sorchestrator-started pid=%d (lock was free)" % (PREFIX, pid))
        return
    pid = svc.spawn(orchestrator_argv(root, run, resume=True, wait_lock=WAIT_LOCK_S), log, root)
    svc.emit("%sorchestrator-waiting pid=%d (the lock is held; it takes over within %ds if the holder exits)" % (
        PREFIX, pid, WAIT_LOCK_S))


def mutate(svc: Services, root: Path, run: str, fn: Callable[[Dict[str, Any]], Any]) -> None:
    try:
        ledger.locked_update(ledger.run_ledger_path(root, run), fn)
    except ledger.LedgerError as exc:
        raise classify_ledger_error(exc)


def _task_entry(root: Path, run: str, task: str) -> Dict[str, Any]:
    try:
        doc = ledger.read(ledger.run_ledger_path(root, run))
        return doc["tasks"][ledger.validate_task_id(task)]
    except ledger.LedgerError as exc:
        raise classify_ledger_error(exc)
    except KeyError:
        raise Refused(ledger.E_UNKNOWN_TASK, task)


def cmd_adopt(svc: Services, args: argparse.Namespace) -> int:
    run, root = validate_run_id(args.run), resolve_root(svc, args.root)
    if bool(args.oid) == bool(args.remote_id):
        raise UsageError("BAD_ADOPT", "pass exactly one of --oid (Claude result) or --remote-id (Codex session id)")
    entry = _task_entry(root, run, args.task)
    if args.remote_id:
        return _adopt_remote(svc, root, run, args, entry)
    if not OID_RE.match(args.oid or ""):
        raise UsageError("BAD_OID", "--oid must be a full lowercase object id")
    if entry["provider"] == "codex":
        raise Refused("ADOPT_UNSUPPORTED_PROVIDER", "a Codex result has no object id until the dispatcher applies its "
                      "diff; adopt it with --remote-id <codex task id> instead of --oid")
    mutate(svc, root, run, lambda d: ledger.apply_adopt(d, args.task, args.oid))
    svc.emit("%sadopted run=%s task=%s oid=%s" % (PREFIX, run, args.task, args.oid))
    kick_orchestrator(svc, root, run)
    return EXIT_OK


def _adopt_remote(svc: Services, root: Path, run: str, args: argparse.Namespace, entry: Dict[str, Any]) -> int:
    """Codex ``adopt --remote-id``: bind the session id, go back to ``launched``; the normal fetch follows."""
    if entry["provider"] != "codex":
        raise Refused("ADOPT_UNSUPPORTED_PROVIDER", "--remote-id binds a Codex session; a Claude result binds by "
                      "nonce, adopt it with --oid")
    remote_id = ledger.safe_remote_id(args.remote_id)
    if not remote_id or remote_id.startswith("-") or scan_text(remote_id, "remote-id"):
        raise UsageError("BAD_REMOTE_ID", "--remote-id is not a usable Codex task id")
    now = int(svc.now())
    deadline = now + deadline_seconds(load_config(svc, root, ledger.read(ledger.run_ledger_path(root, run))["base_sha"]))
    mutate(svc, root, run, lambda d: ledger.apply_adopt_remote(d, args.task, remote_id, launched_at=now, deadline_at=deadline))
    svc.emit("%sadopted run=%s task=%s remote-id=%s" % (PREFIX, run, args.task, remote_id))
    kick_orchestrator(svc, root, run)
    return EXIT_OK


def cmd_abandon(svc: Services, args: argparse.Namespace) -> int:
    run, root = validate_run_id(args.run), resolve_root(svc, args.root)
    _task_entry(root, run, args.task)
    try:
        mutate(svc, root, run, lambda d: ledger.apply_abandon(d, args.task, args.retry))
    except Refused as exc:
        if exc.code == ledger.E_INTEGRATING:
            # the holder (or a restarted orchestrator) settles the integration; make sure one is running
            kick_orchestrator(svc, root, run)
        raise
    svc.emit("%sabandoned run=%s task=%s retry=%s" % (PREFIX, run, args.task, str(bool(args.retry)).lower()))
    kick_orchestrator(svc, root, run)
    return EXIT_OK


def dependency_report(root: Path, run: str, task: str, generation: int) -> Dict[str, Any]:
    """The stored report for the task generation: ``packages``, ``unattributed``, ``report_digest``.

    Missing fields are treated as unattributed (an empty package list proves nothing: a lockfile-only or
    ``postinstall``-only change has no package name)."""
    path = ledger.run_dir(root, run) / ("dependency-%s-g%d.json" % (task, generation))
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Refused("DEPENDENCY_REPORT_MISSING", "no dependency report for %s; abandon --retry instead" % task)
    if not isinstance(report, dict):
        raise Refused("DEPENDENCY_REPORT_INVALID", "the dependency report for %s is not an object" % task)
    packages = report.get("packages")
    names = [str(n) for n in packages] if isinstance(packages, list) else []
    digest = report.get("report_digest")
    unattributed = bool(report.get("unattributed")) if isinstance(packages, list) and "unattributed" in report else True
    if not all(PACKAGE_RE.match(n) for n in names):
        raise Refused("DEPENDENCY_REPORT_INVALID", "a package name cannot be expressed as an install grant")
    if not isinstance(digest, str) or not DIGEST_RE.match(digest):
        raise Refused("DEPENDENCY_REPORT_INVALID", "the dependency report carries no usable digest")
    return {"packages": names, "unattributed": unattributed, "digest": digest}


def required_install_grants(report: Dict[str, Any]) -> List[str]:
    grants = ["install:%s" % name for name in report["packages"]]
    if report["unattributed"]:
        grants.append("install:unattributed:%s" % report["digest"][:12])
    return grants


def cmd_resolve(svc: Services, args: argparse.Namespace) -> int:
    run, root = validate_run_id(args.run), resolve_root(svc, args.root)
    entry = _task_entry(root, run, args.task)
    try:  # dry run on a copy: the ledger's refusal table decides before any grant is consulted
        ledger.apply_resolve(copy.deepcopy(ledger.read(ledger.run_ledger_path(root, run))), args.task, args.action)
    except ledger.LedgerError as exc:
        raise classify_ledger_error(exc)
    snapshot: Optional[Tuple[int, Optional[str], Optional[str]]] = None
    if args.action == ledger.ACTION_ACCEPT_DEPS:
        report = dependency_report(root, run, args.task, entry["generation"])
        required = required_install_grants(report)
        if not required:
            raise Refused("DEPENDENCY_REPORT_INVALID", "the report names no package and is not marked unattributed")
        if report["digest"] != entry.get("deps_digest"):
            raise Refused("STALE_APPROVAL", "the stored dependency report is not the one recorded for this result")
        missing = [key for key in required if not gates_check(svc, run, key)]
        if missing:
            raise Refused("GRANT_MISSING", "missing grants: %s" % ", ".join(missing))
        snapshot = (entry["generation"], entry.get("bound_oid"), report["digest"])

    def apply(doc: Dict[str, Any]) -> Any:
        if snapshot is not None:  # what the grants covered must still be what is being accepted
            current = doc["tasks"][args.task]
            if (current["generation"], current.get("bound_oid"), current.get("deps_digest")) != snapshot:
                raise Refused("STALE_APPROVAL", "generation, bound result or dependency report changed since the grants "
                              "were checked; re-run `resolve` for the current result")
        return ledger.apply_resolve(doc, args.task, args.action)

    mutate(svc, root, run, apply)
    svc.emit("%sresolved run=%s task=%s action=%s" % (PREFIX, run, args.task, args.action))
    kick_orchestrator(svc, root, run)
    return EXIT_OK


# --------------------------------------------------------------------------
# cleanup
# --------------------------------------------------------------------------


def _list_refs(svc: Services, root: Path, prefix: str) -> Optional[List[str]]:
    """The refs under ``prefix``; ``None`` when git could not list them (never an empty list that means "none")."""
    text = git_text(svc, root, ["for-each-ref", "--format=%(refname)", prefix])
    return None if text is None else [line for line in text.splitlines() if line]


def _server_confirms(svc: Services, url: str, ref: str, oid: str) -> bool:
    """A remote-tracking ref is a local cache; it is a safe copy only when the server still has that branch at that OID."""
    prefix = "refs/remotes/%s/" % ORIGIN_NAME
    if not ref.startswith(prefix) or not url:
        return False
    remote_ref = "refs/heads/" + ref[len(prefix):]
    ok, refs = ls_remote(svc, url, remote_ref)
    return ok and refs.get(remote_ref) == oid


def _integration_unmerged(svc: Services, root: Path, run: str, base_sha: str, url: str = "") -> bool:
    read = git(svc, root, ["rev-parse", "--verify", "--quiet", ledger.integration_ref(run)])
    if not read.ok and read.rc == 1 and not read.text().strip():
        return False  # the ref is absent: nothing to lose
    tip = read.text().strip() if read.ok else ""
    if not OID_RE.match(tip):  # git failed (or said something unreadable): fail closed, never "nothing unmerged"
        raise Refused("INTEGRATION_UNMERGED", "could not read refs/heads/ffs/cloud/%s/integration (git failed), so it "
                      "cannot be shown to be merged; fix the repository and rerun, or pass --discard-unmerged" % run)
    if tip == base_sha:
        return False
    own = ("refs/heads/ffs/cloud/%s/" % run, "refs/ffs/cloud/%s/" % run,
           "refs/remotes/%s/ffs/cloud/%s/" % (ORIGIN_NAME, run))
    containing = git_text(svc, root, ["for-each-ref", "--contains", tip, "--format=%(refname) %(objectname)"]) or ""
    for line in containing.splitlines():
        ref, _, oid = line.partition(" ")
        if not ref or ref.startswith(own):
            continue
        if ref.startswith("refs/remotes/"):
            if _server_confirms(svc, url, ref, oid):
                return False
            continue
        return False
    return True


def _owned_refs(doc: Dict[str, Any], run: str, *, remote: bool = False) -> Dict[str, Optional[frozenset]]:
    """The refs the dispatcher creates for this run, derived from the ledger, each with the OIDs it may
    legitimately hold (``None``: the ledger recorded none, so the ref is kept, never deleted).  Any other ref under
    the run prefix is somebody else's.  An earlier generation's OIDs come from its journaled admit (the task branch
    is created at that ``base_oid``) and integrate (candidate and publish OIDs); the per-task fields are reset on retry.
    A task branch (local, or with ``remote`` the pushed one) and the integration ref count only with proof that this run
    created them: the admit intent records ``base_oid`` before anything is scanned or pushed, so it proves nothing."""
    published: Dict[Tuple[str, int], set] = {}  # (task, generation) -> every publish OID the ledger knows of
    branches: Dict[Tuple[str, int], set] = {}  # ... every task-branch OID
    candidates: Dict[Tuple[str, int], set] = {}  # ... every candidate OID
    for h in doc["integration"]["history"]:
        published.setdefault((h["task"], h["generation"]), set()).add(h["publish_oid"])
        if OID_RE.match(str(h.get("candidate_oid") or "")):
            candidates.setdefault((h["task"], h["generation"]), set()).add(h["candidate_oid"])
    for j in doc.get("journal", []):
        data, key = j.get("data") or {}, (j.get("task"), j.get("generation"))
        if j.get("op") == ledger.OP_INTEGRATE and data.get("publish_oid"):
            published.setdefault(key, set()).add(data["publish_oid"])
            if OID_RE.match(str(data.get("candidate_oid") or "")):
                candidates.setdefault(key, set()).add(data["candidate_oid"])
        elif (j.get("op") == ledger.OP_ADMIT and OID_RE.match(str(data.get("base_oid") or ""))
              and (j.get("created") or {}).get("remote" if remote else "local") is True):
            branches.setdefault(key, set()).add(data["base_oid"])
    owned: Dict[str, Optional[frozenset]] = {
        ledger.integration_ref(run): frozenset([doc["integration"]["tip"]]) if doc.get("integration_ref_created") else None}
    for name, entry in doc["tasks"].items():
        for gen in range(1, entry["generation"] + 1):
            def recorded(oid: Any, journaled: Dict[Tuple[str, int], set]) -> Optional[frozenset]:
                known = set(journaled.get((name, gen), ()))
                if gen == entry["generation"] and oid:
                    known.add(oid)
                return frozenset(known) if known else None
            owned[ledger.task_branch_ref(run, name, gen)] = recorded(None, branches)
            owned[ledger.candidate_ref(run, name, gen)] = recorded(entry.get("candidate_oid"), candidates)
            owned[ledger.publication_ref(run, name, gen)] = frozenset(published[(name, gen)]) if (name, gen) in published else None
    return owned


def _ref_oid(svc: Services, root: Path, ref: str) -> Optional[str]:
    """The OID a plain (non-symbolic) ref holds; ``None`` for a missing ref, a symbolic one or a git failure."""
    text = git_text(svc, root, ["for-each-ref", "--format=%(objectname) %(symref)", ref])
    parts = (text or "").split()
    return parts[0] if len(parts) == 1 and OID_RE.match(parts[0]) else None


def _listable(ref: str) -> str:
    """A ref name that is safe to print.  Names the dispatcher did not create can carry anything, so one that the
    secret scanner flags (or that has control characters) is replaced by a digest the operator can recompute."""
    if scan_text(ref, "ref") or redact_text(ref, max(len(ref), 1)) != ref:
        return "[withheld sha256:%s]" % hashlib.sha256(ref.encode("utf-8", "replace")).hexdigest()[:12]
    return ref


def _delete_remote_branches(svc: Services, root: Path, run: str, url: str,
                            owned: Optional[Dict[str, Optional[frozenset]]] = None) -> Tuple[int, List[str]]:
    """``(deleted, refs left)``.  Only task branches the ledger accounts for, and only at an OID it recorded: the
    delete is leased to that OID, so a branch that moved is kept and reported.  A branch with no recorded OID is
    kept too.  The integration ref is published locally, never to the remote, so a remote one is somebody else's."""
    owned = owned or {}
    if not gates_check(svc, run, "push:origin/ffs/cloud/%s" % run):
        raise Refused("GRANT_MISSING", "missing grants: push:origin/ffs/cloud/%s" % run)
    ok, refs = ls_remote(svc, url, "refs/heads/ffs/cloud/%s/*" % run)
    if not ok:
        raise Refused("REMOTE_UNREACHABLE", "ls-remote failed")
    deleted = 0
    failed: List[str] = []
    for ref in sorted(refs):
        if not push_ref_valid(run, ref) or ref not in owned:  # not a name the dispatcher creates: report, never delete
            failed.append("remote:" + _listable(ref))
            continue
        if owned[ref] is None:
            svc.emit("%scleanup-kept ref=remote:%s (creation not confirmed)" % (PREFIX, ref))
            continue
        if refs[ref] not in owned[ref]:
            svc.emit("%scleanup-kept ref=remote:%s (moved off the recorded OID)" % (PREFIX, ref))
            failed.append("remote:" + ref)
            continue
        result = svc.run(["git", "-C", str(root), "push", "--no-verify", "--no-follow-tags", "--recurse-submodules=no",
                          "--force-with-lease=%s:%s" % (ref, refs[ref]), url, "--delete", ref],
                         timeout=GIT_PUSH_TIMEOUT_S, env=remote_env())
        if result.ok:
            deleted += 1
        else:  # includes a lease rejection: the branch moved between the listing and the delete
            svc.emit("%scleanup-kept ref=remote:%s (delete refused: moved or unreachable)" % (PREFIX, ref))
            failed.append("remote:" + ref)
    return deleted, failed


def cmd_cleanup(svc: Services, args: argparse.Namespace) -> int:
    run, root = validate_run_id(args.run), resolve_root(svc, args.root)

    def clean(doc: Dict[str, Any]) -> Any:
        # runs under the ledger lock that `abandon --retry` also takes, so the run cannot reopen mid-cleanup
        # stranded (start died between the two writes) = no inputs file AND nothing ever started: refuse otherwise
        began = doc.get("journal") or any(t.get("state") != ledger.WAITING or t.get("generation") != 1 or t.get(
            "remote_id") or t.get("task_branch_oid") for t in doc["tasks"].values())
        if doc["state"] != ledger.RUN_DONE and (ledger.tasks_path(root, run).exists() or began):
            raise Refused("RUN_NOT_DONE", "run %s is %s; cleanup only removes done runs" % (run, doc["state"]))
        if not args.discard_unmerged and _integration_unmerged(svc, root, run, doc["base_sha"], doc["origin_url"]):
            raise Refused("INTEGRATION_UNMERGED", "refs/heads/ffs/cloud/%s/integration holds work that no other "
                          "branch contains; merge it or pass --discard-unmerged" % run)
        def incomplete(left: List[str], removed: int) -> Refused:
            # keep the ledger (origin url, grants, run state) so a rerun can finish the job
            svc.emit("%scleanup-partial run=%s refs-removed=%d" % (PREFIX, run, removed))
            return Refused("CLEANUP_INCOMPLETE", "refs left behind, ledger kept; fix the cause (a remote ref the dispatcher "
                           "did not create must be deleted by hand) and rerun cleanup: %s" % ", ".join(left))

        owned = _owned_refs(doc, run)
        doomed: List[Tuple[str, str]] = []  # (ref, observed oid): everything is classified before anything is deleted
        left: List[str] = []
        for prefix in ("refs/heads/ffs/cloud/%s/" % run, "refs/ffs/cloud/%s/" % run):
            listed = _list_refs(svc, root, prefix)
            if listed is None:
                left.append("unlisted:" + prefix)
                continue
            for ref in listed:
                oid = _ref_oid(svc, root, ref)
                if ref not in owned:
                    svc.emit("%scleanup-kept ref=%s (not created by the dispatcher)" % (PREFIX, _listable(ref)))
                elif owned[ref] is None:  # no proof the run created it or no OID recorded: it may hold anything now
                    svc.emit("%scleanup-kept ref=%s (creation not confirmed)" % (PREFIX, ref))
                elif oid is None or (owned[ref] is not None and oid not in owned[ref]):
                    left.append(ref)  # moved off the recorded OID (or symbolic): somebody else's work may be on it
                else:
                    doomed.append((ref, oid))
        if left:
            raise incomplete(left, 0)
        if args.remote:
            deleted, failed = _delete_remote_branches(svc, root, run, doc["origin_url"], {
                r: o for r, o in _owned_refs(doc, run, remote=True).items()
                if r.startswith("refs/heads/") and not r.endswith("/integration")})
            svc.emit("%scleanup remote-branches-deleted=%d" % (PREFIX, deleted))
            if failed:  # before any local deletion, so the rerun's unmerged guard still has its integration ref
                raise incomplete(failed, 0)
        removed = 0
        for ref, oid in doomed:
            if git(svc, root, ["update-ref", "--no-deref", "-d", ref, oid]).ok:  # compare-and-delete
                removed += 1
            else:
                left.append(ref)
        if left:
            raise incomplete(left, removed)
        ledger.reap_slots(ledger.slots_path(root), int(svc.now()), lambda candidate: None if candidate == run else ledger.RUN_ACTIVE)
        shutil.rmtree(str(ledger.run_dir(root, run)), ignore_errors=True)
        for path in (ledger.run_ledger_path(root, run), ledger.lock_path_for(ledger.run_ledger_path(root, run)),
                     ledger.orch_lock_path(root, run)):
            try:
                os.unlink(str(path))
            except OSError:
                pass
        svc.emit("%scleanup run=%s refs-removed=%d" % (PREFIX, run, removed))
        return ledger.UNCHANGED

    mutate(svc, root, run, clean)
    return EXIT_OK


# --------------------------------------------------------------------------
# Orchestrator (contract section 4, 6, 7, 14.3, 14.4)
# --------------------------------------------------------------------------

ST = ledger  # state names live in the ledger module
ACTIVE_FOR_SLEEP = frozenset(
    [ST.LAUNCHED, ST.RUNNING, ST.RESULT_READY, ST.COLLECTED, ST.REVIEWED, ST.VERIFIED, ST.INTEGRATING, ST.UNCERTAIN,
     ST.RESERVED, ST.SUBMITTING]
)
# E_ILLEGAL is deliberately absent: an illegal transition is a mapping bug and must surface as an
# ``internal-error`` for the task, not be logged as "superseded" and retried every tick.
STALE_CODES = frozenset([ledger.E_STALE, ledger.E_BLOCKER_MISMATCH])
OUTCOME_CONTINUE, OUTCOME_DONE, OUTCOME_PARKED = "continue", "done", "parked"


class Preflight:
    """Result of the submit-time checks (14.5, 14.6): a blocker with reasons, a retry, or the prompt."""

    def __init__(self, blocker: Optional[str] = None, reasons: Optional[List[str]] = None, retry: bool = False,
                 prompt: str = "") -> None:
        self.blocker = blocker
        self.reasons = reasons or []
        self.retry = retry
        self.prompt = prompt


class Orchestrator:
    def __init__(self, svc: Services, root: Path, run: str, *, once: bool = False, wait_lock: float = 0) -> None:
        self.svc = svc
        self.root = root
        self.run_id = run
        self.once = once
        self.wait_lock = wait_lock
        self.lock_exit = EXIT_OK
        self.path = ledger.run_ledger_path(root, run)
        self.slots_file = ledger.slots_path(root)
        self.rundir = ledger.run_dir(root, run)
        self.log_path = self.rundir / "orchestrate.log"
        self.state_dir = ledger.state_dir(root)
        self.config: Dict[str, Any] = {}
        self.limits: Dict[str, int] = dict(delegation_router.DEFAULT_LIMITS)
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.lock_fd: Optional[int] = None
        self.lifecycle_id = ""
        self.idle_sleep = TICK_ACTIVE_S
        self._fetch_failures: Dict[Tuple[str, int], int] = {}
        self._errors: Dict[Tuple[str, ...], int] = {}

    # -- plumbing ------------------------------------------------------------

    def log(self, message: str) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.svc.now()))
        try:
            append_private(self.log_path, "%s %s" % (stamp, redact_text(message)))
        except OSError:
            pass

    def say(self, line: str) -> None:
        self.svc.emit(line)

    def doc(self) -> Dict[str, Any]:
        return ledger.read(self.path)

    def update(self, fn: Callable[[Dict[str, Any]], Any]) -> Dict[str, Any]:
        return ledger.locked_update(self.path, fn)

    def apply(self, name: str, generation: int, fn: Callable[[Dict[str, Any]], Any], what: str) -> bool:
        """Locked update that drops (and logs) a result whose generation or state is no longer current."""
        try:
            self.update(fn)
            return True
        except ledger.LedgerError as exc:
            if exc.code not in STALE_CODES:
                raise
            self.log("dropped %s for %s g%d: %s" % (what, name, generation, exc.code))
            return False

    def safely(self, key: Tuple[str, ...], fn: Callable[[], Any], task: Optional[str] = None) -> None:
        """Run one per-task step.  A stale result is logged and dropped.  Any other failure is logged; after
        ``TASK_ERROR_LIMIT`` consecutive failures of the same step the task goes to ``needs-operator`` with
        blocker ``internal-error`` (detail sanitized) and the other tasks carry on.  Steps without a task
        (``task=None``) escalate to a ``STEP_FAILING`` store error after ``CONSECUTIVE_ERROR_LIMIT``."""
        try:
            fn()
            self._errors.pop(key, None)
        except ledger.LedgerError as exc:
            if exc.code in STALE_CODES:
                self.log("superseded %s: %s" % (":".join(key), exc.code))
                return
            self.step_failed(key, exc, task)
        except Exception as exc:  # noqa: BLE001 - one task's failure must not stop the others
            self.step_failed(key, exc, task)

    def step_failed(self, key: Tuple[str, ...], exc: BaseException, task: Optional[str]) -> None:
        count = self._errors.get(key, 0) + 1
        self._errors[key] = count
        self.log("step %s failed (%d): %s: %s" % (":".join(key), count, type(exc).__name__, exc))
        if task is not None and count >= TASK_ERROR_LIMIT:
            self._errors.pop(key, None)
            self.internal_error(task, exc)
        elif task is None and count >= CONSECUTIVE_ERROR_LIMIT:
            raise StoreError("STEP_FAILING", "%s failed %d times in a row" % (":".join(key), count))

    def internal_error(self, name: str, exc: BaseException) -> None:
        entry = self.doc()["tasks"].get(name)
        if entry is None or entry["state"] in ledger.TERMINAL or entry["state"] in (ST.NEEDS_OPERATOR, ST.SUBMITTING):
            return
        detail = "internal error in the orchestrator step (%s): %s" % (type(exc).__name__, redact_text(str(exc), 200))
        try:
            self.to_operator(name, entry["generation"], ledger.B_INTERNAL_ERROR, [detail])
        except ledger.LedgerError as err:  # even the escalation failed: it is logged, the loop goes on
            self.log("could not park %s after repeated internal errors: %s" % (name, err.code))

    def step(self, label: str, fn: Callable[[], Any]) -> None:
        """A step that is not about one task (heartbeat, slots, finish): a failure is logged and retried next
        tick, and only a long streak ends the orchestrator (the lifecycle record then lets reconcile restart it)."""
        self.safely((label,), fn)

    def quarantine(self, name: str, generation: int) -> Path:
        return self.rundir / "quarantine" / ("%s-g%d" % (name, generation))

    def run_file(self, kind: str, name: str, generation: int, suffix: str = "log") -> Path:
        return self.rundir / ("%s-%s-g%d.%s" % (kind, name, generation, suffix))

    def lifecycle_script(self) -> Path:
        script = find_script(self.root, "lifecycle.sh")
        if script is None:
            raise StoreError("LIFECYCLE_MISSING", "scripts/gsd/lifecycle.sh not found")
        return script

    def lifecycle(self, *args: str) -> CmdResult:
        return self.svc.run(["bash", str(self.lifecycle_script())] + list(args), cwd=self.root, timeout=SCRIPT_TIMEOUT_S)

    # -- entry ---------------------------------------------------------------

    def run(self) -> int:
        if not self.path.parent.is_dir() or not self.path.exists():
            raise StoreError(ledger.E_MISSING, "no ledger for run %s" % self.run_id)
        if not self.acquire_lock():
            return self.lock_exit
        try:
            if not self.startup():
                return EXIT_OK
            return self.loop()
        finally:
            self.release_lock()

    def acquire_lock(self) -> bool:
        lock_path = ledger.orch_lock_path(self.root, self.run_id)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
        give_up = self.svc.now() + self.wait_lock
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if self.wait_lock > 0 and self.svc.now() < give_up:  # blocking flock with a timeout
                    self.svc.wait_sleep(LOCK_WAIT_POLL_S)
                    continue
                holder = os.pread(fd, 32, 0).decode("ascii", "replace").strip()
                os.close(fd)
                if self.wait_lock > 0:
                    self.say("%sorchestrator-lock-timeout pid=%s waited=%ds" % (PREFIX, holder or "unknown", int(self.wait_lock)))
                    self.lock_exit = EXIT_LOCK
                    return False
                self.register_holder(holder)
                self.say("%sorchestrator-running pid=%s" % (PREFIX, holder or "unknown"))
                return False
        os.ftruncate(fd, 0)
        os.pwrite(fd, ("%d\n" % os.getpid()).encode("ascii"), 0)
        self.lock_fd = fd
        return True

    def release_lock(self) -> None:
        if self.lock_fd is not None:
            try:
                os.close(self.lock_fd)
            finally:
                self.lock_fd = None

    def register_holder(self, holder: str) -> None:
        """Lock contention: point the lifecycle record at the live holder (errors ignored)."""
        if not holder.isdigit():
            return
        try:
            epoch = self.doc()["epoch"]
            self.lifecycle("set-pid", ledger.lifecycle_id(self.run_id, epoch), holder)
        except (ledger.LedgerError, DispatchError):
            return

    def close_lifecycle(self, doc: Dict[str, Any], reason: str) -> None:
        """Idempotently close this epoch's lifecycle record (a crash between the ledger write and the
        ``transition ... done`` call leaves it ``running``; reconcile would then relaunch a finished run).
        A missing or already-terminal record is fine."""
        try:
            result = self.lifecycle("transition", ledger.lifecycle_id(self.run_id, doc["epoch"]), "done", reason)
        except DispatchError as exc:  # no lifecycle.sh: nothing to close
            self.log("lifecycle close (%s) skipped: %s" % (reason, exc.code))
            return
        text = result.text() + result.err.decode("utf-8", "replace")
        if not result.ok and "illegal-transition" not in text and "missing-record" not in text:
            self.log("lifecycle close (%s) failed (rc=%d)" % (reason, result.rc))

    def startup(self) -> bool:
        doc = self.doc()
        if not doc.get("initialized"):  # its start has not committed: only an identical retry of `start` finishes it
            raise Refused("RUN_NOT_INITIALIZED", "run %s has no committed start record; retry the identical `start`" % self.run_id)
        if doc["state"] == ledger.RUN_DONE:
            self.close_lifecycle(doc, "complete")
            self.say("%srun-done run=%s" % (PREFIX, self.run_id))
            return False
        if doc["state"] == ledger.RUN_PARKED:
            self.close_lifecycle(doc, "parked")  # the old epoch; registration below opens a new one
        self.check_origin(doc)
        self.load_inputs(doc)
        self.ensure_integration_ref(doc)
        if doc["state"] == ledger.RUN_PARKED:
            self.replay_park_entries()
        self.register_lifecycle()
        self.update(self._startup_update)
        return True

    def _startup_update(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        if doc["state"] == ledger.RUN_PARKED:
            ledger.resume_run(doc)
        return self._heartbeat(doc)

    def _heartbeat(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        # a task is ``submitting`` only inside phase_launch: one found at the start of a tick (or of the process)
        # lost its launch result to a failed write, so it becomes ``uncertain`` and is polled, never relaunched
        for name in ledger.recover_submitting(doc):
            self.log("task %s was left submitting: now uncertain (never relaunched)" % name)
        ledger.touch_heartbeat(doc, int(self.svc.now()))
        return doc

    def check_origin(self, doc: Dict[str, Any]) -> None:
        current = git_text(self.svc, self.root, ["remote", "get-url", ORIGIN_NAME])
        if current and origin_has_credentials(current):
            raise Refused("ORIGIN_HAS_CREDENTIALS", "the origin URL now embeds credentials; refusing to use it")
        if current != doc["origin_url"]:
            raise Refused("ORIGIN_CHANGED", "origin no longer matches the URL pinned at start")

    def load_inputs(self, doc: Dict[str, Any]) -> None:
        records = ledger.load_tasks_file(ledger.tasks_path(self.root, self.run_id), doc["tasks_sha256"])
        self.tasks = {str(r["id"]): r for r in records}
        self.config = load_config(self.svc, self.root, doc["base_sha"])
        self.limits = limits_from(self.config)

    def ensure_integration_ref(self, doc: Dict[str, Any]) -> None:
        ref = ledger.integration_ref(self.run_id)
        if rev_parse(self.svc, self.root, ref) is None:
            if not git(self.svc, self.root, ["update-ref", "--no-deref", ref, doc["integration"]["tip"], ""]).ok:
                raise StoreError("INTEGRATION_REF", "could not create %s" % ref)

    def register_lifecycle(self) -> None:
        for attempt in (0, 1):
            epoch = self.doc()["epoch"]
            lid = ledger.lifecycle_id(self.run_id, epoch)
            argv = json.dumps([WRAPPER_RELATIVE, "orchestrate", "--run", self.run_id, "--root", str(self.root), "--resume"])
            result = self.lifecycle("launch", lid, argv, LIFECYCLE_BUDGETS, str(os.getpid()))
            text = result.text()
            if result.rc == 0 and "LIFECYCLE:disabled" in text:
                raise Refused("LIFECYCLE_DISABLED", "FFS_LIFECYCLE=off: the orchestrator cannot be registered")
            if result.rc == 0 and "LIFECYCLE:launched" in text:
                self.lifecycle_id = lid
                return
            if result.rc == 3 and "LIFECYCLE:terminal" in text and attempt == 0:
                self.update(lambda d: (ledger.bump_epoch(d), d)[1])
                continue
            raise StoreError("LIFECYCLE_ERROR", "launch failed (rc=%d): %s" % (result.rc, redact_text(text + result.err.decode("utf-8", "replace"), 300)))

    # -- loop ----------------------------------------------------------------

    def loop(self) -> int:
        while True:
            outcome = self.tick()
            if outcome != OUTCOME_CONTINUE or self.once:
                return EXIT_OK
            self.svc.sleep(self.next_sleep())

    def next_sleep(self) -> float:
        doc = self.doc()
        if any(t["state"] in ACTIVE_FOR_SLEEP for t in doc["tasks"].values()):
            self.idle_sleep = TICK_ACTIVE_S
            base = float(TICK_ACTIVE_S)
        else:
            self.idle_sleep = min(TICK_IDLE_MAX_S, self.idle_sleep * 2)
            base = float(self.idle_sleep)
        return base * (1.0 + (self.svc.rand() * 2.0 - 1.0) * TICK_JITTER)

    def tick(self) -> str:
        seen_wake = self.doc().get("wake_seq", 0)
        self.step("heartbeat", lambda: self.update(self._heartbeat))
        self.replay_journal()
        self.step("expire-slots", self.expire_slots)
        for phase in (self.phase_poll, self.phase_collect, self.phase_integrate, self.phase_admit, self.phase_launch):
            phase()
        return self.finish(seen_wake)

    # -- journal replay (section 6) -------------------------------------------

    def replay_slots(self) -> None:
        for seq, op, action in ledger.replay_slot_journal(self.path, self.slots_file, self.limits):
            self.log("journal %d %s: %s" % (seq, op, action))

    def replay_journal(self) -> None:
        self.step("replay-slots", self.replay_slots)
        for entry in ledger.pending_journal(self.doc()):
            handler = {ledger.OP_ADMIT: self.replay_admit, ledger.OP_INTEGRATE: self.replay_integrate,
                       ledger.OP_PARK: self.replay_park_tick}.get(entry["op"])
            if handler is not None:
                self.safely(("replay", entry["op"], str(entry["seq"])), lambda h=handler, e=entry: h(e), task=entry.get("task"))

    def replay_park_entries(self) -> None:
        """Startup, run still parked: finish a park that crashed before its notification."""
        for entry in ledger.pending_journal(self.doc()):
            if entry["op"] == ledger.OP_PARK:
                self.notify_parked()
                self.update(lambda d, s=entry["seq"]: ledger.journal_done(d, s))

    def replay_park_tick(self, entry: Dict[str, Any]) -> None:
        self.update(lambda d: ledger.journal_done(d, entry["seq"]))

    def replay_admit(self, entry: Dict[str, Any]) -> None:
        doc = self.doc()
        name, gen = entry["task"], entry["generation"]
        task = doc["tasks"][name]
        if ledger.journal_is_stale(doc, entry) or task["state"] != ST.WAITING:
            self.update(lambda d: ledger.journal_done(d, entry["seq"]))
            return
        ref = ledger.task_branch_ref(self.run_id, name, gen)
        ok, refs = ls_remote(self.svc, doc["origin_url"], ref)
        if not ok:
            self.log("admit replay for %s: ls-remote failed, retrying next tick" % name)
            return
        action = ledger.replay_admit(entry, refs.get(ref))
        base_oid = entry["data"]["base_oid"]
        if action == "push-again":
            self.publish_admit(name, gen, base_oid, entry["seq"])
        elif action == "admitted":
            self.finish_admit(name, gen, base_oid, entry["seq"])
        else:
            self.to_operator(name, gen, ledger.B_BRANCH_CONFLICT,
                             ["task branch exists on origin at another OID than the journaled base"], entry["seq"])

    def replay_integrate(self, entry: Dict[str, Any]) -> None:
        """Settle a pending ``integrate`` from the observed integration ref.

        The ledger tip and the history record the JOURNALED ``publish_oid``.  A ref that holds more than the
        journaled commit is accepted only when its tip is itself a publish commit this ledger recorded;
        a descendant somebody else added is ``integration-ref-foreign`` (needs-operator) and is never
        adopted, so no successor can launch from it."""
        doc = self.doc()
        name, gen, data = entry["task"], entry["generation"], entry["data"]
        if ledger.journal_is_stale(doc, entry) or doc["tasks"][name]["state"] != ST.INTEGRATING:
            self.update(lambda d: ledger.journal_done(d, entry["seq"]))
            return
        ref = ledger.integration_ref(self.run_id)
        ref_oid = rev_parse(self.svc, self.root, ref)
        publish = data["publish_oid"]
        ancestor = ref_oid is not None and ref_oid != publish and is_ancestor(self.svc, self.root, publish, ref_oid)
        action = ledger.replay_integrate(entry, ref_oid, publish_is_ancestor=ancestor,
                                         known=ledger.known_publish_oids(doc))
        tip = publish
        if action in ("update-ref", "complete", "complete-known") and not ledger.review_has_model_evidence(
                doc["tasks"][name].get("review")):
            # never apply the move on an approval without reviewer-model evidence, and never record a move an older
            # process already made on one: that would release the dependents
            action = "replan" if action == "update-ref" else "unreviewed"
        if action == "update-ref":
            result = cloud_collect.apply_integration(repo=str(self.root), ref=ref, publish_oid=publish,
                                                     expected_tip=data["expected_tip"])
            action = "complete" if result.ok else "replan"
        elif action == "complete-known":
            action, tip = "complete", str(ref_oid)
        if action == "complete":
            verify = doc["tasks"][name].get("verify") or {}
            # the journaled evidence is for the published tree (the merged-tree retest); an older entry has none
            self.complete_integration(name, gen, entry["seq"], data, int(data.get("test_rc", verify.get("test_rc", 0))),
                                      str(data.get("test_log_sha", verify.get("test_log_sha", ""))), tip)
        elif action == "unreviewed":
            self.to_operator(name, gen, ledger.B_MERGE_CONFLICT,
                             ["the integration ref already holds the published result but its approval has no "
                              "reviewer-model evidence; reset the ref to the ledger tip (%s) so the task is reviewed "
                              "and verified again, then `resolve --action retest`" % doc["integration"]["tip"][:12]],
                             entry["seq"])
        elif action == "foreign":
            self.log("integration ref %s holds commits FFS did not publish (tip %s)" % (ref, str(ref_oid)[:12]))
            self.to_operator(name, gen, ledger.B_INTEGRATION_FOREIGN,
                             ["the integration ref has commits on top of the published result that FFS did not publish; "
                              "review them, then `resolve --action reject` or `abandon`"], entry["seq"])
        else:
            self.apply(name, gen, lambda d: ledger.transition(d, name, ledger.EV_REPLAN, generation=gen,
                                                              journal_seq=entry["seq"]), "replan")

    # -- slots (L3) ------------------------------------------------------------------

    def expire_slots(self) -> None:
        """Release the slot of every ``needs-operator``/``uncertain`` task whose deadline has passed, exactly
        once (journaled release, then the slots ledger).  Runs every tick and again right before parking."""
        now = self.svc.now()

        def fn(doc: Dict[str, Any]) -> Any:
            released = ledger.release_expired_slots(
                doc, now, lambda name, entry: self.effective_deadline(doc, name, entry))
            return doc if released else ledger.UNCHANGED

        self.update(fn)
        self.replay_slots()

    # -- operator hand-off ---------------------------------------------------

    def to_operator(self, name: str, gen: int, blocker: str, reasons: Sequence[str], seq: Optional[int] = None) -> bool:
        decisions = clip_list(reasons)
        extra: Dict[str, Any] = {"journal_seq": seq} if seq is not None else {}

        def fn(doc: Dict[str, Any]) -> Dict[str, Any]:
            return ledger.transition(doc, name, ledger.EV_NEEDS_OPERATOR, generation=gen, blocker=blocker,
                                     decisions_needed=decisions, **extra)

        self.log("%s g%d -> needs-operator (%s): %s" % (name, gen, blocker, "; ".join(decisions)))
        return self.apply(name, gen, fn, "needs-operator")

    # -- poll and bind --------------------------------------------------------

    def effective_deadline(self, doc: Dict[str, Any], name: str, entry: Dict[str, Any]) -> float:
        if entry.get("deadline_at"):
            return float(entry["deadline_at"])
        try:
            for slot in ledger.read(self.slots_file).get("slots", []):
                if slot["key"] == ledger.slot_key(self.run_id, name, entry["generation"]):
                    return float(slot["deadline_at"])
        except ledger.LedgerError:
            pass
        return float(doc["created_at"]) + deadline_seconds(self.config)

    def poll_ctx(self, doc: Dict[str, Any], name: str, entry: Dict[str, Any]) -> cloud_host.PollCtx:
        gen = entry["generation"]
        return cloud_host.PollCtx(
            run=self.run_id, task=name, generation=gen, nonce=entry["nonce"], base_oid=entry.get("base_oid") or doc["base_sha"],
            task_branch=ledger.task_branch_name(self.run_id, name, gen), remote_id=entry.get("remote_id"),
            launched_at=float(entry.get("launched_at") or 0), refs_snapshot_path=self.run_file("claude_refs", name, gen, "json"),
            rejected=entry.get("rejected_candidates") or [], clone_url=doc["origin_url"], env_id=codex_env_id(self.config))

    def phase_poll(self) -> None:
        doc = self.doc()
        for name, entry in doc["tasks"].items():
            if entry["state"] in (ST.LAUNCHED, ST.RUNNING):
                self.safely(("poll", name), lambda n=name: self.poll_task(n), task=name)
            elif entry["state"] == ST.UNCERTAIN:
                self.safely(("uncertain", name), lambda n=name: self.poll_uncertain(n), task=name)

    def backend_poll(self, doc: Dict[str, Any], name: str, entry: Dict[str, Any]) -> Optional[cloud_host.PollResult]:
        try:
            return self.svc.backend(entry["provider"]).poll(self.poll_ctx(doc, name, entry))
        except Exception as exc:  # noqa: BLE001 - a failing poll is a poll error, not a crash
            self.log("poll %s raised %s" % (name, type(exc).__name__))
            return None

    def poll_task(self, name: str) -> None:
        doc = self.doc()
        entry = doc["tasks"][name]
        gen = entry["generation"]
        if entry["state"] not in (ST.LAUNCHED, ST.RUNNING):
            return
        if self.svc.now() > self.effective_deadline(doc, name, entry):
            self.apply(name, gen, lambda d: ledger.transition(d, name, ledger.EV_DEADLINE, generation=gen), "deadline")
            return
        result = self.backend_poll(doc, name, entry)
        if result is None or result.state in ("unknown", "failed"):
            self.record_poll_error(name, gen)
        elif entry["provider"] == "claude":
            self.bind_claude(name, gen, entry, result)
        else:
            self.bind_codex(doc, name, gen, entry, result)

    def record_poll_error(self, name: str, gen: int) -> None:
        def fn(doc: Dict[str, Any]) -> Dict[str, Any]:
            ledger.note_poll(doc, name, generation=gen, ok=False)
            if ledger.poll_errors_exceeded(doc["tasks"][name]):
                ledger.transition(doc, name, ledger.EV_POLL_ERRORS, generation=gen)
            return doc

        self.apply(name, gen, fn, "poll error")

    def choose_candidates(self, name: str, entry: Dict[str, Any],
                          candidates: Sequence[Tuple[str, str]]) -> List[Tuple[str, str]]:
        """Exact requested branch, else nonce-named refs, else any changed ref; distinct pairs.

        Defense in depth next to ``cloud_host.select_candidates``: a ref named for ANOTHER task of this run
        (``claude/ffs-<R>-<other>-…``) is never this task's result, and a rejected pair is never reconsidered."""
        prefix = entry["nonce"][:8]
        exact_ref = "refs/heads/" + ledger.claude_result_branch(self.run_id, name, entry["nonce"])
        siblings = tuple("refs/heads/claude/ffs-%s-%s-" % (self.run_id, other) for other in self.tasks if other != name)
        rejected = set((str(r), str(o)) for r, o in entry.get("rejected_candidates") or [])
        usable = [c for c in candidates if c not in rejected and not (c[0] != exact_ref and prefix not in c[0]
                                                                     and c[0].startswith(siblings))]
        pool = [c for c in usable if c[0] == exact_ref] or [c for c in usable if prefix in c[0]] or list(usable)
        return sorted(set((str(r), str(o)) for r, o in pool))

    def has_own_result(self, name: str, entry: Dict[str, Any], pool: Sequence[Tuple[str, str]]) -> bool:
        """The clean pool already holds this generation's exact or nonce-named result ref."""
        exact = "refs/heads/" + ledger.claude_result_branch(self.run_id, name, entry["nonce"])
        return any(ref == exact or entry["nonce"][:8] in ref for ref, _ in pool)

    def bind_claude(self, name: str, gen: int, entry: Dict[str, Any], result: cloud_host.PollResult) -> None:
        pool = self.choose_candidates(name, entry, result.candidates)
        if cloud_host.REF_NAME_SECRET_NOTE in (result.detail or "").split() and not self.has_own_result(name, entry, pool):
            # a new result ref whose NAME matched the scanner: it is never snapshotted, bound or recorded by name
            self.to_operator(name, gen, ledger.B_AMBIGUOUS, ["a remote claude/* ref name matched the secret scanner; name withheld"])
            return
        if len(pool) > 1:
            self.to_operator(name, gen, ledger.B_AMBIGUOUS, ["several candidate refs: " + ", ".join(r for r, _ in pool[:MAX_LISTED])])
            return
        ref, oid = pool[0] if pool else ("", "")

        def fn(doc: Dict[str, Any]) -> Dict[str, Any]:
            ledger.note_poll(doc, name, generation=gen, ok=True)
            ledger.note_seen(doc, name, generation=gen, oid=oid or None)
            if oid and ledger.bind_condition_met(doc["tasks"][name]):
                ledger.transition(doc, name, ledger.EV_BIND, generation=gen, bound_oid=oid, bound_ref=ref)
            else:
                ledger.transition(doc, name, ledger.EV_POLL_RUNNING, generation=gen)
            return doc

        if self.apply(name, gen, fn, "poll"):
            self.svc.fault("after-bind")

    def bind_codex(self, doc: Dict[str, Any], name: str, gen: int, entry: Dict[str, Any],
                   result: cloud_host.PollResult) -> None:
        if result.state != "result-ready":
            def running(d: Dict[str, Any]) -> Dict[str, Any]:
                ledger.note_poll(d, name, generation=gen, ok=True)
                return ledger.transition(d, name, ledger.EV_POLL_RUNNING, generation=gen)

            self.apply(name, gen, running, "poll")
            return
        oid = self.fetch_codex(doc, name, gen, entry)
        if oid is None:
            self.record_poll_error(name, gen)
            return

        def fn(d: Dict[str, Any]) -> Dict[str, Any]:
            ledger.note_poll(d, name, generation=gen, ok=True)
            return ledger.transition(d, name, ledger.EV_BIND, generation=gen, bound_oid=oid)

        if self.apply(name, gen, fn, "bind"):
            self.svc.fault("after-bind")

    def fetch_codex(self, doc: Dict[str, Any], name: str, gen: int, entry: Dict[str, Any]) -> Optional[str]:
        """Apply the Codex diff in a persistent quarantine at the task base; the commit OID is what gets bound."""
        quarantine = self.quarantine(name, gen)
        base_oid = entry.get("base_oid") or doc["base_sha"]
        if not self.prepare_quarantine_at_base(quarantine, ledger.task_branch_ref(self.run_id, name, gen), base_oid):
            self.log("codex quarantine for %s could not be prepared" % name)
            return None
        try:
            oid = self.svc.backend("codex").fetch(self.poll_ctx(doc, name, entry), ("", ""), quarantine)
        except Exception as exc:  # noqa: BLE001 - FetchError or anything else: retry as a poll error
            self.log("codex fetch for %s failed: %s" % (name, getattr(exc, "code", type(exc).__name__)))
            return None
        return oid if OID_RE.match(oid or "") else None

    def prepare_quarantine_at_base(self, quarantine: Path, branch_ref: str, base_oid: str) -> bool:
        shutil.rmtree(str(quarantine), ignore_errors=True)
        quarantine.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        steps = (
            ["git", "init", "-q", str(quarantine)],
            ["git", "-C", str(quarantine), "-c", "protocol.file.allow=always", "fetch", "-q", "--no-tags", str(self.root),
             "%s:refs/heads/base" % branch_ref],
            ["git", "-C", str(quarantine), "checkout", "-q", "--detach", "refs/heads/base"],
        )
        for argv in steps:
            if not self.svc.run(argv, timeout=GIT_TIMEOUT_S).ok:
                return False
        return rev_parse(self.svc, quarantine, "HEAD") == base_oid

    def poll_uncertain(self, name: str) -> None:
        doc = self.doc()
        entry = doc["tasks"][name]
        gen = entry["generation"]
        found = self.find_launch(doc, name, entry)
        if found is not None:
            remote_id, url = found
            fields = {k: v for k, v in (("remote_id", remote_id), ("url", url)) if v}
            self.apply(name, gen, lambda d: ledger.transition(d, name, ledger.EV_LAUNCH_FOUND, generation=gen, **fields), "launch-found")
            return
        # the "seen once" mark lives in the ledger (a restart must not reset it): recovery polls are counted
        # there, and only a spent budget turns the launch into an operator item (contract 14.3)
        if ledger.uncertain_poll_budget_spent(entry):
            self.apply(name, gen, lambda d: ledger.transition(d, name, ledger.EV_UNCERTAIN_TIMEOUT, generation=gen), "uncertain-timeout")
        else:
            self.apply(name, gen, lambda d: (ledger.note_uncertain_tick(d, name, generation=gen), d)[1], "uncertain-poll")

    def find_launch(self, doc: Dict[str, Any], name: str, entry: Dict[str, Any]) -> Optional[Tuple[Optional[str], Optional[str]]]:
        """One re-parse of the launch log (Codex id) or a nonce candidate (Claude) proves the launch happened."""
        gen = entry["generation"]
        if entry["provider"] == "codex":
            try:
                text = self.run_file("launch", name, gen).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
            remote_id, url = cloud_host.parse_codex_launch(text)
            return (ledger.safe_remote_id(remote_id), ledger.safe_url(url)) if remote_id else None
        result = self.backend_poll(doc, name, entry)
        if result is not None and self.choose_candidates(name, entry, result.candidates):
            return None, None
        return None

    # -- collect, review, verify ------------------------------------------------

    def phase_collect(self) -> None:
        budget = max(1, self.limits.get("max_local_verifiers", 1))
        handled = 0
        for name, entry in self.doc()["tasks"].items():
            if entry["state"] in (ST.RESULT_READY, ST.COLLECTED, ST.REVIEWED):
                if handled >= budget:
                    break
                handled += 1
                self.safely(("result", name), lambda n=name: self.advance_result(n), task=name)

    def advance_result(self, name: str) -> None:
        if self.doc()["tasks"][name]["state"] == ST.RESULT_READY and not self.stage_collect(name):
            return
        if self.doc()["tasks"][name]["state"] == ST.COLLECTED and not self.stage_review(name):
            return
        if self.doc()["tasks"][name]["state"] == ST.REVIEWED and self.review_evidence_current(name):
            self.stage_verify(name)

    def review_evidence_current(self, name: str) -> bool:
        """True when the task's approval names the reviewer model that answered (see
        ``ledger.review_has_model_evidence``). An approval recorded before that check carries no such evidence:
        it is not trusted, and the task goes back through review and verify."""
        entry = self.doc()["tasks"][name]
        if ledger.review_has_model_evidence(entry.get("review")):
            return True
        gen = entry["generation"]
        self.log("%s g%d: approval has no reviewer-model evidence; back to review" % (name, gen))
        self.apply(name, gen, lambda d: ledger.transition(d, name, ledger.EV_REVIEW_STALE, generation=gen), "review-stale")
        return False

    def bound_ref(self, doc: Dict[str, Any], name: str, entry: Dict[str, Any]) -> str:
        """The ref the poll bound (stored in the ledger); an operator-adopted result has none: use the exact name."""
        return str(entry.get("bound_ref") or "refs/heads/" + ledger.claude_result_branch(self.run_id, name, entry["nonce"]))

    def stage_collect(self, name: str) -> bool:
        doc = self.doc()
        entry = doc["tasks"][name]
        gen, oid = entry["generation"], entry["bound_oid"]
        quarantine = self.quarantine(name, gen)
        problem = self.materialize_result(doc, name, entry, quarantine)
        if problem is not None:
            return self.fetch_problem(name, gen, problem)
        ctx = cloud_collect.CollectCtx(
            run=self.run_id, task=name, generation=gen, nonce=entry["nonce"], base_sha=entry.get("base_oid") or doc["base_sha"],
            oid=oid, quarantine=str(quarantine), repo=str(self.root), owns=list(entry["owns"]), provider=entry["provider"],
            deps_accepted=bool(entry.get("deps_accepted")),
            accepted_digest=str(entry.get("deps_digest") or "") if entry.get("deps_accepted") else "")
        result = cloud_collect.collect(ctx)
        self.write_collect_log(name, gen, result)
        if not result.ok:
            if self.is_own_invalid_manifest(entry, name, result):
                self.park_invalid_manifest(name, gen, entry, result)
            elif self.is_wrong_candidate(entry, result):
                self.unbind(name, gen, self.bound_ref(doc, name, entry), oid, result)
            else:
                self.apply_blocker(name, gen, STAGE_COLLECT, result, ref_oid=(self.bound_ref(doc, name, entry), oid))
            return False
        manifest = result.data.get("manifest") or {}
        write_private(self.run_file("manifest", name, gen, "json"), json.dumps(manifest, sort_keys=True).encode("utf-8"))
        fields: Dict[str, Any] = {"candidate_oid": result.data["candidate_oid"],
                                  "decisions_needed": clip_list(manifest.get("decisions_needed") or [])}
        if isinstance(manifest.get("env_report"), dict):  # the manifest was validated by collect; the ledger re-validates
            fields["env_report"] = manifest["env_report"]
        done = self.apply(name, gen, lambda d: ledger.transition(d, name, ledger.EV_COLLECT_OK, generation=gen, **fields), "collect")
        if done:
            self.svc.fault("after-collect")
        return done

    @staticmethod
    def is_wrong_candidate(entry: Dict[str, Any], result: cloud_collect.StepResult) -> bool:
        """A Claude candidate whose manifest is missing or invalid is another task's, nonce's or generation's
        result, not a verdict on this task: record it, clear the bind and keep polling.  An operator-adopted
        result (no poll ref) is the operator's explicit choice and stays terminal."""
        names = set(getattr(cloud_collect, n, None) for n in WRONG_CANDIDATE_CONSTANTS) | set(WRONG_CANDIDATE_NAMES)
        return entry["provider"] == "claude" and bool(entry.get("bound_ref")) and result.blocker in names

    def is_own_invalid_manifest(self, entry: Dict[str, Any], name: str, result: cloud_collect.StepResult) -> bool:
        """The bound candidate is provably THIS task generation's result but its manifest fails validation.

        True for a poll-bound Claude candidate whose ref is exactly this generation's expected result branch
        (``claude/ffs-<run>-<task>-<nonce8>``), or whose manifest carries this generation's nonce, with a
        manifest that is present and rejected by the validator.  A foreign ``claude/*`` ref that merely is new
        matches neither, so it stays "not this task's result" and polling continues; a missing manifest is
        not covered here either (the worker may still push it)."""
        if entry["provider"] != "claude" or not entry.get("bound_ref") or result.blocker != cloud_collect.B_MANIFEST_INVALID:
            return False
        expected = "refs/heads/" + ledger.claude_result_branch(self.run_id, name, entry["nonce"])
        return entry["bound_ref"] == expected or bool(result.data.get("nonce_matches"))

    def park_invalid_manifest(self, name: str, gen: int, entry: Dict[str, Any], result: cloud_collect.StepResult) -> None:
        """``needs-operator`` / ``manifest-invalid``; the detail names the validator's error code only."""
        code = str(result.data.get("validator_code") or "MANIFEST_UNPARSEABLE")
        self.log("%s g%d: this task's own result has an invalid manifest (%s): needs-operator" % (name, gen, code))
        parked = cloud_collect.StepResult(
            False, "needs-operator", cloud_collect.B_MANIFEST_INVALID,
            "the task's own result branch carries a manifest the validator rejected: %s" % code)
        self.apply_blocker(name, gen, STAGE_COLLECT, parked,
                           override=(EV_NEEDS, ledger.B_MANIFEST_INVALID))

    def unbind(self, name: str, gen: int, ref: str, oid: str, result: cloud_collect.StepResult) -> None:
        self.log("%s g%d: candidate %s is not this task's result (%s): back to polling" % (
            name, gen, ref, result.blocker))
        deadline = int(self.effective_deadline(self.doc(), name, self.doc()["tasks"][name]))
        self.apply(name, gen, lambda d: ledger.unbind_candidate(d, name, generation=gen, ref=ref, oid=oid, deadline_at=deadline),
                   "unbind")

    def materialize_result(self, doc: Dict[str, Any], name: str, entry: Dict[str, Any], quarantine: Path) -> Optional[str]:
        """Put the bound result in a quarantine repo; returns a failure code or ``None``."""
        oid = entry["bound_oid"]
        if entry["provider"] == "codex":
            exists = quarantine.is_dir() and git(self.svc, quarantine, ["cat-file", "-e", oid + "^{commit}"]).ok
            return None if exists else "RESULT_GONE"
        shutil.rmtree(str(quarantine), ignore_errors=True)
        quarantine.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.svc.run(["git", "init", "-q", str(quarantine)]).ok:
            return "QUARANTINE_INIT"
        ref = self.bound_ref(doc, name, entry)
        try:
            self.svc.backend("claude").fetch(self.poll_ctx(doc, name, entry), (ref, oid), quarantine)
        except Exception as exc:  # noqa: BLE001
            return str(getattr(exc, "code", type(exc).__name__))
        return None

    def fetch_problem(self, name: str, gen: int, code: str) -> bool:
        count = self._fetch_failures.get((name, gen), 0) + 1
        self._fetch_failures[(name, gen)] = count
        self.log("result for %s g%d could not be materialized: %s (%d)" % (name, gen, code, count))
        if count >= FETCH_FAILURE_LIMIT or code == "RESULT_GONE":
            result = cloud_collect.StepResult(False, "rejected", cloud_collect.B_NOT_DESCENDED, "result unavailable: %s" % code)
            self.apply_blocker(name, gen, STAGE_COLLECT, result)
        return False

    def write_collect_log(self, name: str, gen: int, result: cloud_collect.StepResult) -> None:
        line = "ok=%s state=%s blocker=%s\n%s\n" % (result.ok, result.state, result.blocker, result.detail)
        write_private(self.run_file("collect", name, gen), redact_text(line, 20000).encode("utf-8"))

    def apply_blocker(self, name: str, gen: int, stage: str, result: cloud_collect.StepResult,
                      ref_oid: Optional[Tuple[str, str]] = None,
                      override: Optional[Tuple[str, Optional[str]]] = None) -> bool:
        event, blocker = override or map_blocker(stage, result.blocker)
        reasons = ["%s: %s" % (result.blocker, result.detail)]
        manifest = result.data.get("manifest") if isinstance(result.data, dict) else None
        if isinstance(manifest, dict) and result.blocker == cloud_collect.B_MANIFEST_BLOCKED:
            reasons.extend("worker blocker: %s" % b for b in (manifest.get("blockers") or []))
            reasons.extend("decision needed: %s" % d for d in (manifest.get("decisions_needed") or []))
        report = normalize_dependency_report(result.data) if blocker == ledger.B_DEPENDENCY_CHANGE else None
        extra: Dict[str, Any] = {}
        if report is not None:
            write_private(self.run_file("dependency", name, gen, "json"), json.dumps(report, sort_keys=True).encode("utf-8"))
            extra["deps_digest"] = report["report_digest"]  # accept-deps later compares against this, under the lock
            reasons.append("dependency report digest %s (%s)" % (
                report["report_digest"][:12], "unattributed changes: grant install:unattributed:%s" % report["report_digest"][:12]
                if report["unattributed"] else "packages only"))
        decisions = clip_list(reasons)
        self.log("%s g%d %s -> %s/%s: %s" % (name, gen, stage, event, blocker, decisions[0] if decisions else ""))

        def fn(doc: Dict[str, Any]) -> Dict[str, Any]:
            if event == ledger.EV_REJECT and ref_oid and ref_oid[0].startswith("refs/heads/claude/"):
                ledger.reject_candidate(doc, name, generation=gen, ref=ref_oid[0], oid=ref_oid[1])
            return ledger.transition(doc, name, event, generation=gen, blocker=blocker, decisions_needed=decisions, **extra)

        return self.apply(name, gen, fn, "blocker " + str(blocker))

    def review_script(self) -> Optional[str]:
        if self.svc.review_script:
            return self.svc.review_script
        found = find_script(self.root, "cloud-review.sh")
        return str(found) if found else None

    def stage_review(self, name: str) -> bool:
        entry = self.doc()["tasks"][name]
        gen = entry["generation"]
        try:
            manifest = json.loads(self.run_file("manifest", name, gen, "json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
        kwargs: Dict[str, Any] = dict(
            repo=str(self.root), run=self.run_id, task=name, generation=gen, producer=entry["provider"],
            base_oid=entry.get("base_oid") or self.doc()["base_sha"], candidate_oid=entry["candidate_oid"],
            manifest=manifest if isinstance(manifest, dict) else {}, script=self.review_script(),
            attempt=ledger.review_round_attempt(entry))  # 7th cloud-review.sh argument
        result = cloud_collect.review(**kwargs)
        if not result.ok:
            self.apply_blocker(name, gen, STAGE_REVIEW, result)
            return False
        review = {key: result.data[key] for key in ("family", "verdict", "diff_sha", "patch_id", "tree")}
        review.update({key: result.data[key] for key in ("model", "requested_model") if result.data.get(key)})
        return self.apply(name, gen, lambda d: ledger.transition(d, name, ledger.EV_REVIEW_APPROVE, generation=gen, review=review), "review")

    def verify_settings(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        verify = config_section(self.config, "verify")
        timeout = verify.get("timeout_s")
        return {
            "cmd": effective_verify_cmd(entry.get("verify_cmd"), self.config),
            "deps": [str(d) for d in verify.get("deps", [])] if isinstance(verify.get("deps"), list) else [],
            "read_roots": [str(d) for d in verify.get("read_roots", [])] if isinstance(verify.get("read_roots"), list) else [],
            "timeout_s": float(timeout) if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0
            else float(DEFAULT_VERIFY_TIMEOUT_S),
        }

    def other_worktrees(self) -> List[str]:
        text = git_text(self.svc, self.root, ["worktree", "list", "--porcelain"]) or ""
        paths = [line[len("worktree "):] for line in text.splitlines() if line.startswith("worktree ")]
        return [p for p in paths if os.path.realpath(p) != str(self.root)]

    def run_verification(self, name: str, entry: Dict[str, Any], tree_oid: str,
                         label: str) -> Optional[cloud_collect.StepResult]:
        """Tests on ``tree_oid`` under the global verifier limit; ``None`` when every slot is busy."""
        settings = self.verify_settings(entry)
        slot = cloud_collect.verify_slot(str(self.state_dir), max(1, self.limits.get("max_local_verifiers", 1)))
        if slot is None:
            self.log("%s: no verifier slot free, retrying next tick" % name)
            return None
        try:
            return cloud_collect.verify(
                tree_oid=tree_oid, cmd=settings["cmd"], repo=str(self.root), main_worktree=str(self.root),
                deps=settings["deps"], read_roots=settings["read_roots"], timeout_s=settings["timeout_s"],
                log_path=str(self.run_file(label, name, entry["generation"])), other_worktrees=self.other_worktrees(),
                run_verify_fn=self.svc.verify_fn)
        finally:
            slot.close()

    def stage_verify(self, name: str) -> bool:
        entry = self.doc()["tasks"][name]
        gen = entry["generation"]
        result = self.run_verification(name, entry, entry["review"]["tree"], "verify")
        if result is None:
            return False
        if not result.ok:
            self.apply_blocker(name, gen, STAGE_VERIFY, result)
            return False
        evidence = {"test_rc": int(result.data["test_rc"]), "test_log_sha": str(result.data["test_log_sha"])}
        return self.apply(name, gen, lambda d: ledger.transition(d, name, ledger.EV_TESTS_PASS, generation=gen, verify=evidence), "verify")

    # -- integrate ---------------------------------------------------------------

    def phase_integrate(self) -> None:
        for name, entry in self.doc()["tasks"].items():
            if entry["state"] == ST.VERIFIED:
                self.safely(("integrate", name), lambda n=name: self.integrate_task(n), task=name)

    def integrate_task(self, name: str) -> None:
        doc = self.doc()
        entry = doc["tasks"][name]
        gen, tip = entry["generation"], doc["integration"]["tip"]
        if not self.review_evidence_current(name):
            return
        ref = ledger.integration_ref(self.run_id)
        if rev_parse(self.svc, self.root, ref) != tip:
            self.to_operator(name, gen, ledger.B_MERGE_CONFLICT, ["the integration ref moved outside FFS; reconcile it, then `resolve --action retest`"])
            return
        plan = cloud_collect.plan_integration(
            repo=str(self.root), expected_tip=tip, candidate_oid=entry["candidate_oid"],
            reviewed_patch_id=entry["review"]["patch_id"], pub_ref=ledger.publication_ref(self.run_id, name, gen), task=name,
            reviewed_tree=str(entry["review"].get("tree") or ""))
        if not plan.ok:
            self.apply_blocker(name, gen, STAGE_INTEGRATE, plan)
            return
        data = plan.data
        evidence = dict(entry.get("verify") or {})
        if data.get("needs_retest"):
            retest = self.run_verification(name, entry, data["tree"], "retest")
            if retest is None:
                return
            if not retest.ok:
                self.apply_blocker(name, gen, STAGE_RETEST, retest)
                return
            evidence = {"test_rc": int(retest.data["test_rc"]), "test_log_sha": str(retest.data["test_log_sha"])}
        self.commit_integration(name, gen, entry, data, evidence)

    def commit_integration(self, name: str, gen: int, entry: Dict[str, Any], data: Dict[str, Any],
                           evidence: Dict[str, Any]) -> None:
        seq_box: Dict[str, int] = {}

        def begin(doc: Dict[str, Any]) -> Dict[str, Any]:
            seq_box["seq"] = ledger.begin_integration(
                doc, name, generation=gen, expected_tip=data["expected_tip"], candidate_oid=entry["candidate_oid"],
                publish_oid=data["publish_oid"], tree=data["tree"], test_rc=int(evidence.get("test_rc", 0)),
                test_log_sha=str(evidence.get("test_log_sha", "")))
            return doc

        if not self.apply(name, gen, begin, "begin-integration"):
            return
        seq = seq_box["seq"]
        self.svc.fault("after-integrate-journal")
        result = cloud_collect.apply_integration(repo=str(self.root), ref=ledger.integration_ref(self.run_id),
                                                 publish_oid=data["publish_oid"], expected_tip=data["expected_tip"])
        self.svc.fault("after-update-ref")
        if result.ok:
            journal = {"publish_oid": data["publish_oid"], "candidate_oid": entry["candidate_oid"], "tree": data["tree"]}
            self.complete_integration(name, gen, seq, journal, int(evidence.get("test_rc", 0)),
                                      str(evidence.get("test_log_sha", "")), data["publish_oid"])
        elif result.blocker == cloud_collect.B_TIP_MOVED:
            self.log("%s: integration tip moved, re-planning" % name)
            self.apply(name, gen, lambda d: ledger.transition(d, name, ledger.EV_REPLAN, generation=gen, journal_seq=seq), "replan")
        else:
            self.to_operator(name, gen, ledger.B_MERGE_CONFLICT, ["integrate: %s: %s" % (result.blocker, result.detail)], seq)

    def complete_integration(self, name: str, gen: int, seq: int, data: Dict[str, Any], test_rc: int, test_log_sha: str,
                             tip: str) -> None:
        self.apply(name, gen, lambda d: ledger.complete_integration(
            d, name, generation=gen, seq=seq, publish_oid=data["publish_oid"], candidate_oid=data["candidate_oid"],
            tree=data["tree"], test_rc=test_rc, test_log_sha=test_log_sha, tip=tip), "complete-integration")

    # -- admit ----------------------------------------------------------------------

    def phase_admit(self) -> None:
        self.step("propagate", lambda: self.update(lambda d: d if ledger.propagate_dependency_failures(d) else ledger.UNCHANGED))
        doc = self.doc()
        pending = set((e["task"], e["generation"]) for e in ledger.pending_journal(doc) if e["op"] == ledger.OP_ADMIT)
        for name in ledger.next_admissible(doc):
            entry = doc["tasks"][name]
            if (name, entry["generation"]) in pending:
                continue
            base = doc["integration"]["tip"] if entry["depends_on"] else doc["base_sha"]
            self.safely(("admit", name), lambda n=name, g=entry["generation"], b=base: self.start_admit(n, g, b), task=name)

    def start_admit(self, name: str, gen: int, base_oid: str) -> None:
        seq_box: Dict[str, int] = {}

        def begin(doc: Dict[str, Any]) -> Dict[str, Any]:
            seq_box["seq"] = ledger.journal_begin(doc, ledger.OP_ADMIT, name, gen, {
                "base_oid": base_oid, "task_branch": ledger.task_branch_ref(self.run_id, name, gen)})
            return doc

        if self.apply(name, gen, begin, "admit-journal"):
            self.svc.fault("after-admit-journal")
            self.publish_admit(name, gen, base_oid, seq_box["seq"])

    def outbound_findings(self, doc: Dict[str, Any], provider: str, tip_oid: str) -> List[str]:
        """Section 8.1 / 14.6: new commits always; the full ancestry (cached by OID) for Claude."""
        allow = config_section(self.config, "scan").get("allow", [])
        allow = [a for a in allow if isinstance(a, dict)] if isinstance(allow, list) else []
        try:
            findings = []
            if tip_oid != doc["base_sha"]:
                findings.extend(cloud_scan.scan_git_objects(str(self.root), doc["base_sha"], tip_oid, outbound=True))
            if provider == "claude":
                findings.extend(cloud_scan.scan_git_ancestry(str(self.root), tip_oid, str(ledger.scan_cache_path(self.root)), allow))
            return scan_labels(findings)
        except cloud_scan.ScanRefused as exc:
            return ["scan-refused:%s" % exc.reason]

    def publish_admit(self, name: str, gen: int, base_oid: str, seq: int) -> None:
        doc = self.doc()
        entry = doc["tasks"][name]
        ref = ledger.task_branch_ref(self.run_id, name, gen)
        existing = rev_parse(self.svc, self.root, ref)
        if existing not in (None, base_oid):
            self.to_operator(name, gen, ledger.B_BRANCH_CONFLICT, ["local task branch exists at another OID"], seq)
            return
        if existing is None and not git(self.svc, self.root, ["update-ref", "--no-deref", ref, base_oid, ""]).ok:
            raise StoreError("TASK_BRANCH", "cannot create %s" % ref)
        if existing is None:
            # Journaled as ours before anything else can fail.  The write carries no generation check (locked_update
            # raises no stale code and _merge_created only adds a proof), and none is needed: this run made the ref.
            self.record_created(name, gen, seq, {"local": True})
        prior = next((j.get("created") or {} for j in doc.get("journal", []) if j.get("seq") == seq), {})
        # a retry after a failed push finds the branch it made itself: the proof journaled by the attempt that made it stands
        created = {"local": existing is None or prior.get("local") is True,
                   "remote": False}  # the remote proof is recorded once the push is confirmed
        findings = self.outbound_findings(doc, entry["provider"], base_oid)
        if findings:
            self.to_operator(name, gen, ledger.B_SCAN_REFUSED, ["outbound scan refused the push: " + ", ".join(findings)], seq)
            return
        key = "push:origin/ffs/cloud/%s" % self.run_id
        if not push_ref_valid(self.run_id, ref) or not self.grant_ok(key):
            self.to_operator(name, gen, ledger.B_GRANT_MISSING, ["missing grant %s" % key], seq)
            return
        seen, seen_refs = ls_remote(self.svc, doc["origin_url"], ref)
        absent_before = seen and ref not in seen_refs
        pushed = self.svc.run(["git", "-C", str(self.root), "push", "--porcelain", "--no-force", "--no-verify",
                               "--no-follow-tags", "--recurse-submodules=no", doc["origin_url"],
                               "%s:%s" % (base_oid, ref)], timeout=GIT_PUSH_TIMEOUT_S, env=remote_env())
        if not pushed.ok:
            self.push_failed(name, gen, ref, pushed.rc, seq)
            return
        # Ownership needs the push itself to report a NEW ref for this destination: another clone with the same origin,
        # run, task and base may have created it between the pre-check and this push, which then says "up to date" (=).
        created["remote"] = absent_before and pushed_new_ref(pushed.text(), ref)
        self.svc.fault("after-admit-push")
        ok, refs = ls_remote(self.svc, doc["origin_url"], ref)
        if not ok or refs.get(ref) != base_oid:
            self.push_failed(name, gen, ref, 0, seq, "ls-remote did not confirm the push")
            return
        self.finish_admit(name, gen, base_oid, seq, created)

    def record_created(self, name: str, gen: int, seq: int, flags: Dict[str, bool]) -> bool:
        """Journal, at once, a creation proof for the admit entry ``seq``.  The merge raises no stale-task code, so this
        is ``True`` unless the ledger itself fails (which propagates)."""
        return self.apply(name, gen, lambda d: _merge_created(d, seq, flags), "creation-proof")

    def push_failed(self, name: str, gen: int, ref: str, rc: int, seq: int, why: str = "") -> None:
        """Count a failed admit push in the ledger; after ``PUSH_FAILURE_LIMIT`` attempts the task needs an
        operator (``push-failed``, visible in status) instead of retrying silently forever."""
        box: Dict[str, int] = {}

        def fn(doc: Dict[str, Any]) -> Dict[str, Any]:
            box["n"] = ledger.note_push_failure(doc, name, generation=gen)
            return doc

        if not self.apply(name, gen, fn, "push-failure"):
            return
        self.log("push of %s failed (%s rc=%d), attempt %d of %d" % (ref, why or "git push", rc, box["n"], ledger.PUSH_FAILURE_LIMIT))
        if box["n"] >= ledger.PUSH_FAILURE_LIMIT:
            self.to_operator(name, gen, ledger.B_PUSH_FAILED, [
                "the push of %s failed %d times (for example a protected-branch rejection); fix the cause, then "
                "`resolve --action retest`" % (ref, box["n"])], seq)

    def finish_admit(self, name: str, gen: int, base_oid: str, seq: int, created: Optional[Dict[str, bool]] = None) -> None:
        """``created``: which task-branch refs this process saw absent and then made.  It is journaled in the write that
        settles the admit, i.e. after the push was confirmed; a replay passes none, so its refs are never cleanup-owned."""
        def settle(d: Dict[str, Any]) -> Dict[str, Any]:
            if created and any(created.values()):
                _merge_created(d, seq, created)
            return ledger.transition(d, name, ledger.EV_DEPS_MET, generation=gen, journal_seq=seq, base_oid=base_oid,
                                     task_branch_oid=base_oid)

        self.apply(name, gen, settle, "admit")

    def grant_ok(self, key: str) -> bool:
        try:
            return gates_check(self.svc, self.run_id, key)
        except Refused as exc:
            self.log("grant check %s: %s" % (key, exc.code))
            return False

    # -- reserve and launch ----------------------------------------------------------

    def phase_launch(self) -> None:
        self.step("replay-slots", self.replay_slots)
        self.step("reap-slots", self.reap_slots)
        self.step("slot-check", self.reclaim_lost_slots)
        self.step("reserve", self.reserve_admitted)
        for name, entry in self.doc()["tasks"].items():
            if entry["state"] == ST.RESERVED:
                self.safely(("launch", name), lambda n=name: self.submit(n), task=name)

    def reap_slots(self) -> None:
        """Before reserving: drop slots whose run ledger is missing or done, whose deadline + 1 h passed, or
        whose (parked) run is past its deadline, so a dead run cannot hold capacity forever."""
        reaped = ledger.reap_slots(self.slots_file, int(self.svc.now()), ledger.make_run_state_fn(self.root))
        for key in reaped:
            self.log("reaped slot %s" % key)

    def reclaim_lost_slots(self) -> None:
        """A ``reserved`` task whose slot is gone (released at its deadline or reaped while its run was parked)
        goes back to ``admitted`` and re-reserves before any submit: no launch without a slot."""
        doc = self.doc()
        try:
            held = set(ledger.slot_keys(self.slots_file))
        except ledger.LedgerError:
            held = set()
        pending = set((e["task"], e["generation"]) for e in ledger.pending_journal(doc) if e["op"] == ledger.OP_RESERVE)
        for name, entry in doc["tasks"].items():
            gen = entry["generation"]
            if entry["state"] == ST.RESERVED and ledger.slot_key(self.run_id, name, gen) not in held and (name, gen) not in pending:
                self.log("%s g%d: its slot is gone; re-reserving before any submit" % (name, gen))
                self.apply(name, gen, lambda d, n=name, g=gen: ledger.transition(d, n, ledger.EV_SLOT_LOST, generation=g), "slot-lost")

    def reserve_admitted(self) -> None:
        doc = self.doc()
        pending = set((e["task"], e["generation"]) for e in ledger.pending_journal(doc) if e["op"] == ledger.OP_RESERVE)
        free = slots_free(self.svc, self.root, self.config)
        deadline = int(self.svc.now()) + deadline_seconds(self.config)
        reserved = False
        for name, entry in doc["tasks"].items():
            provider, gen = entry["provider"], entry["generation"]
            if entry["state"] != ST.ADMITTED or (name, gen) in pending or free.get(provider, 0) <= 0:
                continue
            free[provider] -= 1
            data = {"key": ledger.slot_key(self.run_id, name, gen), "provider": provider, "deadline_at": deadline}
            reserved = self.apply(name, gen, lambda d, n=name, g=gen, x=data: (
                ledger.journal_begin(d, ledger.OP_RESERVE, n, g, x), d)[1], "reserve-journal") or reserved
        if reserved:
            self.svc.fault("after-reserve-journal")
            self.replay_slots()

    def preflight(self, name: str, entry: Dict[str, Any], doc: Dict[str, Any]) -> Preflight:
        provider, gen = entry["provider"], entry["generation"]
        record = self.tasks[name]
        exposure = exposure_names(self.config, provider)
        required = ["cloud-dispatch:" + provider, "push:origin/ffs/cloud/" + self.run_id] + ["secret-use:" + n for n in exposure]
        required += ["secret-use:" + n for n in record.get("needs_secrets") or []]
        missing = [k for k in sorted(set(required)) if not self.grant_ok(k)]
        if missing:
            blocker = ledger.B_EXPOSURE_NOT_GRANTED if any(k.startswith("secret-use:") for k in missing) else ledger.B_GRANT_MISSING
            return Preflight(blocker, ["missing grants: " + ", ".join(missing)])
        branch = ledger.task_branch_name(self.run_id, name, gen)
        if exposure:
            verified, detail = branch_rules_verified(self.svc, doc["origin_url"], branch)
            if not verified:
                return Preflight(ledger.B_RULESET_UNVERIFIED, ["ruleset evidence: " + detail])
        ref = ledger.task_branch_ref(self.run_id, name, gen)
        ok, refs = ls_remote(self.svc, doc["origin_url"], ref)
        if not ok:
            return Preflight(retry=True)
        if refs.get(ref) != entry.get("task_branch_oid"):
            return Preflight(ledger.B_BRANCH_CONFLICT, ["task branch on origin is not at the admitted OID (moved or deleted)"])
        prompt = packet_for(dict(record, provider=provider), run=self.run_id, generation=gen, nonce=entry["nonce"],
                            base_sha=entry.get("base_oid") or doc["base_sha"], config=self.config)
        found = scan_text(prompt, "packet:%s" % name)
        if found:
            return Preflight(ledger.B_SCAN_REFUSED, ["packet scan refused the submit: " + ", ".join(found)])
        return Preflight(prompt=prompt)

    def submit(self, name: str) -> None:
        doc = self.doc()
        entry = doc["tasks"][name]
        gen, provider = entry["generation"], entry["provider"]
        check = self.preflight(name, entry, doc)
        if check.retry:
            return
        if check.blocker:
            self.to_operator(name, gen, check.blocker, check.reasons)
            return
        if not self.apply(name, gen, lambda d: ledger.transition(d, name, ledger.EV_SUBMIT, generation=gen), "submit"):
            return
        self.svc.fault("after-submitting")
        record = self.tasks[name]
        ctx = cloud_host.LaunchCtx(
            run=self.run_id, task=name, generation=gen, prompt=check.prompt, task_branch=ledger.task_branch_name(self.run_id, name, gen),
            clone_url=doc["origin_url"], model=record.get("model") if provider == "claude" else None,
            effort=record.get("effort") if provider == "claude" else None, log_path=self.run_file("launch", name, gen),
            env_id=codex_env_id(self.config), task_branch_oid=str(entry.get("task_branch_oid") or ""),
            refs_snapshot_path=self.run_file("claude_refs", name, gen, "json"), sanitize=cloud_scan.redact, repo_root=self.root)
        try:
            result = self.svc.backend(provider).launch(ctx)
        except Exception as exc:  # noqa: BLE001 - a launch that raised may still have started a session
            self.log("launch of %s raised %s; treating as uncertain" % (name, type(exc).__name__))
            result = cloud_host.LaunchResult(-1, None, None, "uncertain", "LAUNCH_RAISED")
        self.svc.fault("after-launch-exec")
        self.record_launch(name, gen, result)

    def record_launch(self, name: str, gen: int, result: cloud_host.LaunchResult) -> None:
        now = int(self.svc.now())
        self.log("launch %s g%d: %s %s" % (name, gen, result.outcome, redact_text(result.detail or "", 300)))
        if result.outcome == "launched":
            fields = {"launched_at": now, "deadline_at": now + deadline_seconds(self.config),
                      "remote_id": self.clean_field(result.remote_id, ledger.safe_remote_id),
                      "url": self.clean_field(result.url, ledger.safe_url)}
            if result.note:
                fields["launch_note"] = result.note
            self.apply(name, gen, lambda d: ledger.transition(d, name, ledger.EV_LAUNCH_OK, generation=gen, **fields), "launch-ok")
        elif result.outcome == "uncertain":
            self.apply(name, gen, lambda d: ledger.transition(d, name, ledger.EV_CRASH_FOUND, generation=gen), "launch-uncertain")
        else:
            self.apply(name, gen, lambda d: ledger.transition(d, name, ledger.EV_LAUNCH_FAILED, generation=gen), "launch-failed")

    @staticmethod
    def clean_field(value: Optional[str], validator: Callable[[Any], Optional[str]]) -> Optional[str]:
        clean = validator(value)
        if clean is not None and scan_text(clean, "launch-field"):
            return None
        return clean

    # -- finish and park (section 4, 14.4) ---------------------------------------------

    def finish(self, seen_wake: int) -> str:
        self.step("expire-slots", self.expire_slots)  # a parked run must hold no slot past its deadline
        outcome = {"done": False}

        def fn(doc: Dict[str, Any]) -> Any:
            outcome["done"] = ledger.finish_run(doc)
            return doc if outcome["done"] else ledger.UNCHANGED

        self.update(fn)
        if outcome["done"]:
            self.lifecycle_done("complete")
            self.say("%srun-done run=%s" % (PREFIX, self.run_id))
            return OUTCOME_DONE
        self.svc.fault("before-park")
        if not ledger.park_run(self.path, seen_wake):
            return OUTCOME_CONTINUE
        self.svc.fault("after-park-state")
        self.notify_parked()
        self.update(self._finish_park_entries)
        self.lifecycle_done("parked")
        self.svc.fault("before-exit-recheck")
        if self.wake_after_park(seen_wake):
            return OUTCOME_CONTINUE
        self.say("%sparked run=%s" % (PREFIX, self.run_id))
        return OUTCOME_PARKED

    def wake_after_park(self, seen_wake: int) -> bool:
        """Last look before exiting (14.4): an operator verb that mutated the ledger after the park decision
        found the lock held and may be waiting for us to leave.  Under the ledger lock, if ``wake_seq`` moved,
        clear ``parked`` back to ``active`` (new epoch, the old lifecycle record is already closed) and keep
        ticking instead of exiting.  Later mutations see ``parked`` and spawn a lock-waiting orchestrator."""
        box = {"woken": False}

        def fn(doc: Dict[str, Any]) -> Any:
            if doc.get("state") == ledger.RUN_PARKED and doc.get("wake_seq") != seen_wake:
                ledger.resume_run(doc)
                ledger.bump_epoch(doc)
                box["woken"] = True
                return doc
            return ledger.UNCHANGED

        self.update(fn)
        if box["woken"]:
            self.log("wake_seq moved while parking: un-parked, continuing")
            self.register_lifecycle()
        return box["woken"]

    @staticmethod
    def _finish_park_entries(doc: Dict[str, Any]) -> Dict[str, Any]:
        for entry in ledger.pending_journal(doc):
            if entry["op"] == ledger.OP_PARK:
                ledger.journal_done(doc, entry["seq"])
        return doc

    def lifecycle_done(self, reason: str) -> None:
        result = self.lifecycle("transition", self.lifecycle_id, "done", reason)
        if not result.ok:
            self.log("lifecycle transition done %s failed (rc=%d)" % (reason, result.rc))

    def notify_parked(self) -> None:
        doc = self.doc()
        items = sum(1 for t in doc["tasks"].values() if t["state"] in OPERATOR_STATES)
        line = "CLOUD-PARKED run=%s items=%d see: cloud-dispatch.sh status --run %s" % (self.run_id, items, self.run_id)
        self.log(line)
        self.say(line)
        command = os.environ.get(ENV_NOTIFY, "")
        if command:
            result = self.svc.run(["sh", "-c", command], timeout=NOTIFY_TIMEOUT_S, input_bytes=(line + "\n").encode("utf-8"))
            if not result.ok:
                self.log("%s exited %d" % (ENV_NOTIFY, result.rc))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def cmd_orchestrate(svc: Services, args: argparse.Namespace) -> int:
    run = validate_run_id(args.run)
    root = resolve_root(svc, args.root)
    wait_lock = int(args.wait_lock or 0)
    if args.detach:
        argv = orchestrator_argv(root, run, resume=args.resume, wait_lock=wait_lock) + (["--once"] if args.once else [])
        pid = svc.spawn(argv, ledger.run_dir(root, run) / "orchestrate.log", root)
        svc.emit("%sdetached pid=%d" % (PREFIX, pid))
        return EXIT_OK
    return Orchestrator(svc, root, run, once=args.once, wait_lock=wait_lock).run()


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # argparse exits 2 itself; keep stderr tagged
        self.exit(EXIT_USAGE, "%s: error: %s\n" % (self.prog, message))


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="cloud-dispatch.sh")
    verbs = parser.add_subparsers(dest="verb", parser_class=_Parser)
    verbs.required = True

    def add(name: str, root: bool = True) -> argparse.ArgumentParser:
        sub = verbs.add_parser(name)
        if root:
            sub.add_argument("--root", default=None, help="absolute path of the main worktree (default: from cwd)")
        return sub

    plan = add("plan")
    plan.add_argument("--tasks", required=True)
    plan.add_argument("--policy", required=True, choices=POLICIES)
    plan.add_argument("--run", default=None)
    plan.add_argument("--with-doctor", action="store_true")
    plan.add_argument("--sequential", action="store_true")
    start = add("start")
    start.add_argument("--run", required=True)
    start.add_argument("--tasks", required=True)
    start.add_argument("--policy", required=True, choices=POLICIES)
    start.add_argument("--foreground", action="store_true")
    start.add_argument("--sequential", action="store_true")
    orchestrate = add("orchestrate")
    orchestrate.add_argument("--run", required=True)
    orchestrate.add_argument("--resume", action="store_true")
    orchestrate.add_argument("--detach", action="store_true")
    orchestrate.add_argument("--once", action="store_true")
    orchestrate.add_argument("--wait-lock", type=int, default=0, metavar="SECONDS",
                             help="block on the orchestrator lock for up to SECONDS (exit 75 if it stays held)")
    status = add("status")
    status.add_argument("--run", required=True)
    status.add_argument("--json", action="store_true")
    doctor = add("doctor")
    doctor.add_argument("--json", action="store_true")
    env = add("env", root=False)
    env_verbs = env.add_subparsers(dest="env_verb", parser_class=_Parser)
    env_verbs.required = True
    init = env_verbs.add_parser("init")
    init.add_argument("--root", default=None)
    init.add_argument("--register-hook", action="store_true")
    init.add_argument("--force", action="store_true")
    spec = add("env-spec")
    spec.add_argument("--provider", required=True, choices=PROVIDERS)
    cleanup = add("cleanup")
    cleanup.add_argument("--run", required=True)
    cleanup.add_argument("--remote", action="store_true")
    cleanup.add_argument("--discard-unmerged", action="store_true")
    probe = add("probe-claude", root=False)
    probe.add_argument("--dir", required=True, help="directory to start the plain claude binary in")
    probe.add_argument("--seconds", type=float, default=cloud_host.PROBE_TIMEOUT_S)
    adopt = add("adopt")
    adopt.add_argument("--run", required=True)
    adopt.add_argument("--task", required=True)
    adopt.add_argument("--oid", default=None, help="Claude: the result commit to bind")
    adopt.add_argument("--remote-id", default=None, help="Codex: the cloud task id to bind (then the normal fetch)")
    abandon = add("abandon")
    abandon.add_argument("--run", required=True)
    abandon.add_argument("--task", required=True)
    abandon.add_argument("--retry", action="store_true")
    resolve = add("resolve")
    resolve.add_argument("--run", required=True)
    resolve.add_argument("--task", required=True)
    resolve.add_argument("--action", required=True, choices=(ledger.ACTION_RETEST, ledger.ACTION_ACCEPT_DEPS, ledger.ACTION_REJECT))
    return parser


def dispatch(svc: Services, args: argparse.Namespace) -> int:
    if args.verb == "env":
        return cmd_env_init(svc, args)
    handlers = {
        "plan": cmd_plan, "start": cmd_start, "orchestrate": cmd_orchestrate, "status": cmd_status,
        "doctor": cmd_doctor, "env-spec": cmd_env_spec, "cleanup": cmd_cleanup, "adopt": cmd_adopt,
        "abandon": cmd_abandon, "resolve": cmd_resolve, "probe-claude": cmd_probe_claude,
    }
    return handlers[args.verb](svc, args)


def main(argv: Optional[Sequence[str]] = None, svc: Optional[Services] = None) -> int:
    services = svc or Services()
    try:
        args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    try:
        return dispatch(services, args)
    except ledger.LedgerError as exc:
        error: DispatchError = classify_ledger_error(exc)
    except DispatchError as exc:
        error = exc
    except OSError as exc:  # a filesystem failure is a refusal with the store exit code, not a stack trace
        error = IoRefused("STORE_IO", "%s: %s" % (type(exc).__name__, exc.strerror or exc))
    if isinstance(error, Refused):
        services.emit("%s %s %s" % (REFUSED_PREFIX, error.code, error.detail))
    else:
        sys.stderr.write("CLOUD-ERROR: %s %s\n" % (error.code, error.detail))
    return error.exit_code


if __name__ == "__main__":
    sys.exit(main())
