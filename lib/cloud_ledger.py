"""Durable state machine for ``/swarm --cloud`` (contracts: docs/cloud-delegation-contracts.md).

This module is the only writer of the run ledger, the slots ledger and the
per-run ``tasks.json``.  It does no git and no subprocess work: it only keeps
JSON documents consistent.  Python 3.9, stdlib only.

Layout (contract section 14.1), all under ``<main worktree>/.feature-fix-swarm/cloud/``::

    runs/<R>.json  runs/<R>.lock  runs/<R>.orch.lock  runs/<R>/tasks.json
    providers.json providers.lock verify.<i>.lock scan-cache.json

Write discipline: ``locked_update`` takes ``flock`` on the sibling lock file,
reads, runs the caller's pure function, re-checks ``revision`` on disk, writes a
temp file in the same directory, fsyncs it, ``os.replace``s it, fsyncs the
directory and only then reports ``revision + 1``.  Two ledger locks are never
held at the same time.

The transition table (``TRANSITIONS``) is data.  ``transition`` validates
everything before it mutates anything, so a rejected move leaves the document
byte-identical.  Slot release is journaled automatically in the same write as
any transition that stops a task from holding a slot.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

SCHEMA = 1
LEDGER_MAX_BYTES = 16 * 1024 * 1024
MAX_EPOCH = 999999
MAX_GENERATION = 999999
MANIFEST_MAX_BYTES = 64 * 1024
MANIFEST_STRING_MAX = 4000
MANIFEST_SUMMARY_MAX = 2000
MANIFEST_LIST_MAX = 50
POLL_ERROR_LIMIT = 6
# Recovery polls an ``uncertain`` launch gets before it parks (contract 14.3): Codex has one re-parse
# of the launch log; Claude keeps polling for a nonce-named candidate a little longer.
UNCERTAIN_POLLS = {"claude": 3, "codex": 1}
PUSH_FAILURE_LIMIT = 5
REVIEW_RETEST_LIMIT = 2
DEFAULT_SLOT_DEADLINE_S = 12 * 3600
SLOT_REAP_GRACE_S = 3600
BIND_CONSECUTIVE_POLLS = 2

# run states
RUN_ACTIVE = "active"
RUN_PARKED = "parked"
RUN_DONE = "done"
RUN_STATES = (RUN_ACTIVE, RUN_PARKED, RUN_DONE)

# task states
WAITING = "waiting"
ADMITTED = "admitted"
RESERVED = "reserved"
SUBMITTING = "submitting"
LAUNCHED = "launched"
RUNNING = "running"
RESULT_READY = "result-ready"
COLLECTED = "collected"
REVIEWED = "reviewed"
VERIFIED = "verified"
INTEGRATING = "integrating"
INTEGRATED = "integrated"
UNCERTAIN = "uncertain"
NEEDS_OPERATOR = "needs-operator"
SUPERSEDED = "superseded"
REJECTED = "rejected"
FAILED = "failed"
ABANDONED = "abandoned"

ALL_STATES = frozenset(
    [WAITING, ADMITTED, RESERVED, SUBMITTING, LAUNCHED, RUNNING, RESULT_READY, COLLECTED, REVIEWED,
     VERIFIED, INTEGRATING, INTEGRATED, UNCERTAIN, NEEDS_OPERATOR, SUPERSEDED, REJECTED, FAILED, ABANDONED]
)
TERMINAL = frozenset([INTEGRATED, REJECTED, FAILED, ABANDONED, SUPERSEDED])
NON_TERMINAL = ALL_STATES - TERMINAL
# Exactly the states that hold a provider slot (contract 14.3).
ACTIVE_SET = frozenset([RESERVED, SUBMITTING, LAUNCHED, RUNNING, UNCERTAIN])
# States from which a slot may still exist.  ``needs-operator`` keeps the slot of
# poll-errors / uncertain-launch / submit-time blockers until the deadline, and
# ``admitted`` may hold one mid-reserve.  Leaving this set journals a release.
SLOT_MAYBE_HELD = ACTIVE_SET | frozenset([ADMITTED, NEEDS_OPERATOR])

# blockers (contract 14.8; the three marked "ext" are named here, see report)
B_UNCERTAIN_LAUNCH = "uncertain-launch"
B_POLL_ERRORS = "poll-errors"
B_AMBIGUOUS = "ambiguous-candidates"
B_CANDIDATE_MOVED = "candidate-moved"
B_MANIFEST_BLOCKED = "manifest-blocked"
B_PATH_OUTSIDE_OWNS = "path-outside-owns"
B_DENYLIST = "denylist"
B_DEPENDENCY_CHANGE = "dependency-change"
B_REVIEW_UNAVAILABLE = "review-unavailable"
B_REVIEW_CAP = "review-cap"
B_REVIEW_MODEL_DOWNGRADED = "review-model-downgraded"  # ext: the reviewer that answered was not the requested judgment-tier model
B_REVIEW_MODEL_UNKNOWN = "review-model-unknown"  # ext: the answering reviewer model could not be determined
B_MANIFEST_INVALID = "manifest-invalid"  # ext: the task's own result carries a manifest the validator rejected
B_VERIFY_CMD_MISSING = "verify-cmd-missing"  # ext: the effective verify command is empty or whitespace-only
B_SANDBOX_UNAVAILABLE = "sandbox-unavailable"
B_MERGE_CONFLICT = "merge-conflict"
B_PATCH_ID_MISMATCH = "patch-id-mismatch"
B_EXPOSURE_NOT_GRANTED = "exposure-not-granted"
B_RULESET_UNVERIFIED = "ruleset-unverified"
B_GRANT_MISSING = "grant-missing"
B_SECRET_LEAK = "secret-leak"
B_TESTS_FAILED = "tests-failed"
B_REVIEW_REVISE = "review-revise"  # ext: names the "review REVISE" row
B_QUARANTINE_REFUSED = "quarantine-refused"  # ext: symlink/submodule/ancestry refusals of section 7 step 1
B_BRANCH_CONFLICT = "branch-conflict"  # ext: admit found the task branch at another OID (section 6)
B_SCAN_REFUSED = "scan-refused"  # ext: an outbound/packet scan refused a push or a submit
B_PUSH_FAILED = "push-failed"  # ext: the admit push failed PUSH_FAILURE_LIMIT times
B_INTERNAL_ERROR = "internal-error"  # ext: a per-task step failed repeatedly (or a blocker we do not know)
B_INTEGRATION_FOREIGN = "integration-ref-foreign"  # ext: the integration ref holds commits FFS did not publish
B_BINARY_CHANGE = "binary-change"  # ext: the result changes binary content the reviewer cannot read
B_DEADLINE = "deadline"
DEPENDENCY_BLOCKERS = frozenset("dependency-" + s for s in (REJECTED, FAILED, ABANDONED, SUPERSEDED))

ALL_BLOCKERS = frozenset(
    [B_UNCERTAIN_LAUNCH, B_POLL_ERRORS, B_AMBIGUOUS, B_CANDIDATE_MOVED, B_MANIFEST_BLOCKED, B_PATH_OUTSIDE_OWNS,
     B_DENYLIST, B_DEPENDENCY_CHANGE, B_REVIEW_UNAVAILABLE, B_REVIEW_CAP, B_REVIEW_MODEL_DOWNGRADED,
     B_REVIEW_MODEL_UNKNOWN, B_VERIFY_CMD_MISSING, B_MANIFEST_INVALID, B_SANDBOX_UNAVAILABLE, B_MERGE_CONFLICT,
     B_PATCH_ID_MISMATCH, B_EXPOSURE_NOT_GRANTED, B_RULESET_UNVERIFIED, B_GRANT_MISSING, B_SECRET_LEAK,
     B_TESTS_FAILED, B_REVIEW_REVISE, B_QUARANTINE_REFUSED, B_BRANCH_CONFLICT, B_DEADLINE, B_SCAN_REFUSED,
     B_PUSH_FAILED, B_INTERNAL_ERROR, B_INTEGRATION_FOREIGN, B_BINARY_CHANGE]
) | DEPENDENCY_BLOCKERS

# Operator verbs (contract 14.8).  ``resolve --action X`` is refused unless the
# task's current blocker is in RESOLVE_ACTIONS[X]; ``adopt`` likewise (unless the
# task is ``uncertain``).  ``abandon [--retry]`` is allowed from any non-terminal
# state (14.3); ``--retry`` is also allowed from rejected/failed.
ACTION_RETEST = "retest"
ACTION_ACCEPT_DEPS = "accept-deps"
ACTION_REJECT = "reject"
ADOPT_BLOCKERS = frozenset([B_UNCERTAIN_LAUNCH, B_POLL_ERRORS, B_AMBIGUOUS, B_CANDIDATE_MOVED])
RESOLVE_ACTIONS: Dict[str, FrozenSet[str]] = {
    ACTION_RETEST: frozenset(
        [B_REVIEW_UNAVAILABLE, B_REVIEW_CAP, B_REVIEW_MODEL_DOWNGRADED, B_REVIEW_MODEL_UNKNOWN, B_SANDBOX_UNAVAILABLE,
         B_MERGE_CONFLICT, B_PATCH_ID_MISMATCH, B_EXPOSURE_NOT_GRANTED, B_RULESET_UNVERIFIED, B_GRANT_MISSING, B_BRANCH_CONFLICT, B_SCAN_REFUSED,
         B_PUSH_FAILED, B_INTERNAL_ERROR]
    ),
    ACTION_ACCEPT_DEPS: frozenset([B_DEPENDENCY_CHANGE]),
    ACTION_REJECT: frozenset(
        [B_PATH_OUTSIDE_OWNS, B_DENYLIST, B_DEPENDENCY_CHANGE, B_REVIEW_UNAVAILABLE, B_REVIEW_CAP,
         B_REVIEW_MODEL_DOWNGRADED, B_REVIEW_MODEL_UNKNOWN, B_VERIFY_CMD_MISSING, B_SANDBOX_UNAVAILABLE, B_MERGE_CONFLICT, B_PATCH_ID_MISMATCH, B_BINARY_CHANGE, B_INTEGRATION_FOREIGN]
    ),
}
# Blockers that need an operator but cannot be cleared by any resolve action
# (they clear with adopt or abandon only).
ABANDON_ONLY_BLOCKERS = frozenset([B_MANIFEST_BLOCKED, B_MANIFEST_INVALID])
REVIEW_BLOCKERS = frozenset([B_REVIEW_UNAVAILABLE, B_REVIEW_CAP, B_REVIEW_MODEL_DOWNGRADED, B_REVIEW_MODEL_UNKNOWN])

# events
EV_DEPS_MET = "deps-met"
EV_DEP_TERMINAL = "dependency-terminal"
EV_DEP_REOPEN = "dependency-reopen"
EV_CAPACITY = "capacity"
EV_SLOT_LOST = "slot-lost"
EV_SUBMIT = "submit"
EV_LAUNCH_OK = "launch-ok"
EV_LAUNCH_FAILED = "launch-failed"
EV_CRASH_FOUND = "crash-found"
EV_POLL_RUNNING = "poll-running"
EV_BIND = "bind"
EV_UNBIND = "unbind"
EV_DEADLINE = "deadline"
EV_POLL_ERRORS = "poll-errors-exceeded"
EV_LAUNCH_FOUND = "launch-found"
EV_UNCERTAIN_TIMEOUT = "uncertain-timeout"
EV_COLLECT_OK = "collect-ok"
EV_REVIEW_APPROVE = "review-approve"
EV_REVIEW_STALE = "review-stale"  # an approval without reviewer-model evidence goes back through review
EV_TESTS_PASS = "tests-pass"
EV_MERGE_PLANNED = "merge-planned"
EV_INTEGRATED = "update-ref-done"
EV_REPLAN = "replan"
EV_NEEDS_OPERATOR = "needs-operator"
EV_REJECT = "reject"
EV_ADOPT = "adopt"
EV_ADOPT_REMOTE = "adopt-remote"
EV_RETEST = "retest"
EV_ACCEPT_DEPS = "accept-deps"
EV_OPERATOR_REJECT = "operator-reject"
EV_ABANDON = "abandon"
EV_ABANDON_RETRY = "abandon-retry"

# journal ops (``release`` is added by contract 14.3; section 2.3 omits it)
OP_INTEGRATE = "integrate"
OP_RESERVE = "reserve"
OP_ADMIT = "admit"
OP_PARK = "park"
OP_RELEASE = "release"
JOURNAL_OPS = (OP_INTEGRATE, OP_RESERVE, OP_ADMIT, OP_PARK, OP_RELEASE)

# error codes
E_MISSING = "LEDGER_MISSING"
E_CORRUPT = "LEDGER_CORRUPT"
E_SCHEMA = "LEDGER_SCHEMA_MISMATCH"
E_REVISION = "REVISION_CONFLICT"
E_ILLEGAL = "ILLEGAL_TRANSITION"
E_STALE = "STALE_GENERATION"
E_TASK_INPUTS = "TASK_INPUTS_CHANGED"
E_BAD_ID = "BAD_ID"
E_BAD_FIELD = "BAD_FIELD"
E_BAD_BLOCKER = "BAD_BLOCKER"
E_BLOCKER_MISMATCH = "BLOCKER_MISMATCH"
E_UNKNOWN_TASK = "UNKNOWN_TASK"
E_BAD_ARGUMENT = "BAD_ARGUMENT"
E_BAD_TASKS = "BAD_TASKS"
E_RUN_DONE = "RUN_DONE"
E_EXISTS = "ALREADY_EXISTS"
E_JOURNAL = "JOURNAL_ERROR"
E_BAD_UPDATE = "BAD_UPDATE"
E_EPOCH = "EPOCH_EXHAUSTED"
E_INTEGRATING = "INTEGRATION_IN_PROGRESS"
E_IO = "LEDGER_IO"


class LedgerError(Exception):
    """Typed ledger failure; ``code`` is one of the ``E_*`` constants."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__("%s: %s" % (code, detail) if detail else code)
        self.code = code
        self.detail = detail


class ManifestError(Exception):
    """Typed manifest rejection; ``code`` starts with ``MANIFEST_``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__("%s: %s" % (code, detail) if detail else code)
        self.code = code
        self.detail = detail


# --------------------------------------------------------------------------
# Identifiers, ref names, paths
# --------------------------------------------------------------------------

_RUN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")
_TASK_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,23}")
_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_NONCE_RE = re.compile(r"[0-9a-f]{32}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_REMOTE_RE = re.compile(r"[A-Za-z0-9_.:/-]{1,200}")
_SECRET_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+~:-]{0,63}")
_SLOT_KEY_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]{0,39})/([a-z0-9][a-z0-9-]{0,23})/g([1-9][0-9]{0,5})")
# Suffixes that would make a run id collide with a sibling file in runs/.
_RUN_FORBIDDEN_SUFFIXES = (".lock", ".json", ".orch", ".")
PROVIDERS = ("claude", "codex")
POLICIES = ("auto", "prefer", "required")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_run_id(run: Any) -> str:
    if not isinstance(run, str) or _RUN_RE.fullmatch(run) is None:
        raise LedgerError(E_BAD_ID, "run id")
    if ".." in run or run.endswith(_RUN_FORBIDDEN_SUFFIXES):
        raise LedgerError(E_BAD_ID, "run id is not a valid ref component or collides with a sibling file")
    return run


def validate_task_id(task: Any) -> str:
    if not isinstance(task, str) or _TASK_RE.fullmatch(task) is None:
        raise LedgerError(E_BAD_ID, "task id")
    return task


def validate_generation(generation: Any) -> int:
    if not _is_int(generation) or generation < 1 or generation > MAX_GENERATION:
        raise LedgerError(E_BAD_ID, "generation")
    return generation


def validate_epoch(epoch: Any) -> int:
    if not _is_int(epoch) or epoch < 1 or epoch > MAX_EPOCH:
        raise LedgerError(E_BAD_ID, "epoch")
    return epoch


def validate_oid(value: Any, label: str = "oid") -> str:
    if not isinstance(value, str) or _OID_RE.fullmatch(value) is None:
        raise LedgerError(E_BAD_FIELD, label)
    return value


def validate_nonce(value: Any) -> str:
    if not isinstance(value, str) or _NONCE_RE.fullmatch(value) is None:
        raise LedgerError(E_BAD_FIELD, "nonce")
    return value


def task_branch_name(run: str, task: str, generation: int) -> str:
    return "ffs/cloud/%s/%s-g%d" % (validate_run_id(run), validate_task_id(task), validate_generation(generation))


def task_branch_ref(run: str, task: str, generation: int) -> str:
    return "refs/heads/" + task_branch_name(run, task, generation)


def integration_ref(run: str) -> str:
    return "refs/heads/ffs/cloud/%s/integration" % validate_run_id(run)


def candidate_ref(run: str, task: str, generation: int) -> str:
    return "refs/ffs/cloud/%s/cand/%s-g%d" % (
        validate_run_id(run), validate_task_id(task), validate_generation(generation))


def publication_ref(run: str, task: str, generation: int) -> str:
    return "refs/ffs/cloud/%s/pub/%s-g%d" % (
        validate_run_id(run), validate_task_id(task), validate_generation(generation))


def claude_result_branch(run: str, task: str, nonce: str) -> str:
    return "claude/ffs-%s-%s-%s" % (validate_run_id(run), validate_task_id(task), validate_nonce(nonce)[:8])


def lifecycle_id(run: str, epoch: int) -> str:
    return "cloud-%s-e%d" % (validate_run_id(run), validate_epoch(epoch))


def slot_key(run: str, task: str, generation: int) -> str:
    return "%s/%s/g%d" % (validate_run_id(run), validate_task_id(task), validate_generation(generation))


def validate_slot_key(key: Any) -> Tuple[str, str, int]:
    match = _SLOT_KEY_RE.fullmatch(key) if isinstance(key, str) else None
    if match is None:
        raise LedgerError(E_BAD_ID, "slot key")
    run, task, generation = match.group(1), match.group(2), int(match.group(3))
    validate_run_id(run)
    return run, task, generation


def state_dir(main_worktree: Any) -> Path:
    return Path(main_worktree) / ".feature-fix-swarm" / "cloud"


def run_ledger_path(main_worktree: Any, run: str) -> Path:
    return state_dir(main_worktree) / "runs" / (validate_run_id(run) + ".json")


def run_dir(main_worktree: Any, run: str) -> Path:
    return state_dir(main_worktree) / "runs" / validate_run_id(run)


def slots_path(main_worktree: Any) -> Path:
    return state_dir(main_worktree) / "providers.json"


def orch_lock_path(main_worktree: Any, run: str) -> Path:
    return state_dir(main_worktree) / "runs" / (validate_run_id(run) + ".orch.lock")


def tasks_path(main_worktree: Any, run: str) -> Path:
    return run_dir(main_worktree, run) / "tasks.json"


def scan_cache_path(main_worktree: Any) -> Path:
    return state_dir(main_worktree) / "scan-cache.json"


def lock_path_for(path: Any) -> Path:
    """Sibling lock file: ``x.json`` -> ``x.lock``."""
    target = Path(path)
    name = target.name[:-5] if target.name.endswith(".json") else target.name
    return target.with_name(name + ".lock")


def sanitize_remote_id(value: Any) -> Optional[str]:
    """Validate a provider session id (contract 2.3); ``None`` passes through."""
    if value is None:
        return None
    if not isinstance(value, str) or _REMOTE_RE.fullmatch(value) is None:
        raise LedgerError(E_BAD_FIELD, "remote_id")
    return value


LAUNCH_NOTES = frozenset(["no-session-url", "exit-no-session-url"])


def _check_launch_note(value: Any) -> Optional[str]:
    if value is None:
        return None
    if value not in LAUNCH_NOTES:
        raise LedgerError(E_BAD_FIELD, "launch_note")
    return str(value)


def sanitize_url(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or _REMOTE_RE.fullmatch(value) is None:
        raise LedgerError(E_BAD_FIELD, "url")
    return value


def safe_remote_id(value: Any) -> Optional[str]:
    """Like ``sanitize_remote_id`` but returns ``None`` for anything invalid."""
    try:
        return sanitize_remote_id(value)
    except LedgerError:
        return None


def safe_url(value: Any) -> Optional[str]:
    try:
        return sanitize_url(value)
    except LedgerError:
        return None


# --------------------------------------------------------------------------
# Atomic locked JSON store
# --------------------------------------------------------------------------

UNCHANGED = object()  # a ``locked_update`` function may return this to skip the write

_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _reject_constant(name: str) -> Any:
    raise ValueError("non-finite number: " + name)


def _load(path: Path) -> Dict[str, Any]:
    try:
        fd = os.open(str(path), os.O_RDONLY | _O_CLOEXEC | _O_NOFOLLOW)
    except FileNotFoundError:
        raise LedgerError(E_MISSING, str(path))
    except OSError as exc:
        raise LedgerError(E_IO, "%s: %s" % (path, exc))
    try:
        if os.fstat(fd).st_size > LEDGER_MAX_BYTES:
            raise LedgerError(E_CORRUPT, "oversize")
        chunks: List[bytes] = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    try:
        doc = json.loads(b"".join(chunks).decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise LedgerError(E_CORRUPT, str(path))
    if not isinstance(doc, dict):
        raise LedgerError(E_CORRUPT, "not an object")
    if doc.get("schema") != SCHEMA or not _is_int(doc.get("schema")):
        raise LedgerError(E_SCHEMA, "schema=%r" % (doc.get("schema"),))
    if not _is_int(doc.get("revision")) or doc["revision"] < 0:
        raise LedgerError(E_CORRUPT, "revision")
    return doc


def read(path: Any) -> Dict[str, Any]:
    """Lock-free read.  Safe because writers publish with ``os.replace``."""
    return _load(Path(path))


def _fsync_dir(directory: Path) -> None:
    fd = os.open(str(directory), os.O_RDONLY | _O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    data = (json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
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
    _fsync_dir(path.parent)


def _open_lock(lock: Path) -> int:
    try:
        return os.open(str(lock), os.O_RDWR | os.O_CREAT | _O_CLOEXEC | _O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise LedgerError(E_MISSING if isinstance(exc, FileNotFoundError) else E_IO, "%s: %s" % (lock, exc))


def locked_update(path: Any, fn: Callable[[Dict[str, Any]], Any], *, create: Any = None) -> Dict[str, Any]:
    """Locked read-modify-write; returns the document as written.

    ``fn(doc)`` returns the new document (or mutates in place and returns
    ``None``/the same dict), or ``UNCHANGED`` to skip the write.  ``fn`` must not
    touch ``revision`` and must not call ``locked_update`` on the same path (flock
    is per open file description, so that would deadlock).  ``create`` (a dict or
    a zero-argument callable returning one) makes a missing file legal.
    """
    target = Path(path)
    if create is not None:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    elif not target.parent.is_dir():
        raise LedgerError(E_MISSING, str(target))
    lock_fd = _open_lock(lock_path_for(target))
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        fresh = False
        try:
            doc = _load(target)
        except LedgerError as exc:
            if exc.code != E_MISSING or create is None:
                raise
            doc = create() if callable(create) else copy.deepcopy(create)
            if not isinstance(doc, dict):
                raise LedgerError(E_BAD_UPDATE, "create must produce an object")
            doc.setdefault("schema", SCHEMA)
            doc["revision"] = 0
            fresh = True
        base_revision = doc["revision"]
        result = fn(doc)
        if result is UNCHANGED:
            return doc
        if result is None:
            result = doc
        if not isinstance(result, dict):
            raise LedgerError(E_BAD_UPDATE, "fn must return an object")
        if result.get("revision") != base_revision:
            raise LedgerError(E_REVISION, "fn altered revision")
        try:
            on_disk: Optional[int] = _load(target)["revision"]
        except LedgerError as exc:
            if exc.code != E_MISSING:
                raise
            on_disk = None
        if on_disk != (None if fresh else base_revision):
            raise LedgerError(E_REVISION, "revision changed under lock: %r != %r" % (on_disk, base_revision))
        payload = dict(result)
        payload["revision"] = base_revision + 1
        _atomic_write(target, payload)
        result["revision"] = base_revision + 1
        return result
    finally:
        os.close(lock_fd)


# --------------------------------------------------------------------------
# tasks.json (contract 14.2)
# --------------------------------------------------------------------------


def _canonical_tasks_bytes(tasks: Any) -> bytes:
    if not isinstance(tasks, list):
        raise LedgerError(E_BAD_TASKS, "tasks must be a list")
    return (json.dumps(tasks, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def tasks_sha256(tasks: Any) -> str:
    """Hash of the canonical ``tasks.json`` bytes, computable before the file exists (reserve the run id first)."""
    return hashlib.sha256(_canonical_tasks_bytes(tasks)).hexdigest()


def write_tasks_file(path: Any, tasks: Any) -> str:
    """Write the normalized task list (mode 0600, atomic) and return its sha256."""
    target = Path(path)
    data = _canonical_tasks_bytes(tasks)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix="." + target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(target))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(target.parent)
    return hashlib.sha256(data).hexdigest()


def load_tasks_file(path: Any, expected_sha256: str) -> List[Dict[str, Any]]:
    """Load ``tasks.json``; any hash mismatch (or absence) is ``TASK_INPUTS_CHANGED``."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        raise LedgerError(E_TASK_INPUTS, "tasks file unreadable")
    if not isinstance(expected_sha256, str) or not hmac.compare_digest(
        hashlib.sha256(data).hexdigest(), expected_sha256
    ):
        raise LedgerError(E_TASK_INPUTS, "sha256 differs from the ledger")
    try:
        tasks = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        raise LedgerError(E_TASK_INPUTS, "not JSON")
    if not isinstance(tasks, list):
        raise LedgerError(E_TASK_INPUTS, "not a list")
    return tasks


# --------------------------------------------------------------------------
# Run ledger construction
# --------------------------------------------------------------------------

_RUNTIME_RESET: Dict[str, Callable[[], Any]] = {
    "task_branch_oid": lambda: None,
    "base_oid": lambda: None,
    "remote_id": lambda: None,
    "url": lambda: None,
    "launch_note": lambda: None,
    "launched_at": lambda: None,
    "deadline_at": lambda: None,
    "poll_errors": lambda: 0,
    "bound_oid": lambda: None,
    "bound_ref": lambda: None,
    "seen_oid": lambda: None,
    "seen_count": lambda: 0,
    "candidate_oid": lambda: None,
    "review": lambda: None,
    "verify": lambda: None,
    "decisions_needed": lambda: [],
    "env_report": lambda: None,
    "env_report_source": lambda: None,
    "deps_accepted": lambda: False,
    "deps_digest": lambda: None,
    "resume_state": lambda: None,
    "uncertain_ticks": lambda: 0,
    "slot_released": lambda: False,
    "push_failures": lambda: 0,
    "review_attempts": lambda: 0,
}


def _new_nonce() -> str:
    return secrets.token_hex(16)


def _now() -> int:
    return int(time.time())


def _blank_task(run: str, task: str, spec: Dict[str, Any], nonce: str) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "state": WAITING,
        "generation": 1,
        "provider": spec["provider"],
        "depends_on": list(spec.get("depends_on") or []),
        "owns": list(spec.get("owns") or []),
        "verify_cmd": spec.get("verify_cmd") or "",
        "nonce": nonce,
        "task_branch": task_branch_ref(run, task, 1),
        "blocker": None,
        "rejected_candidates": [],
    }
    for name, factory in _RUNTIME_RESET.items():
        entry[name] = factory()
    return entry


def _check_acyclic(graph: Dict[str, List[str]]) -> None:
    indegree = {node: len(deps) for node, deps in graph.items()}
    ready = [node for node, count in indegree.items() if count == 0]
    seen = 0
    while ready:
        node = ready.pop()
        seen += 1
        for other, deps in graph.items():
            if node in deps:
                indegree[other] -= 1
                if indegree[other] == 0:
                    ready.append(other)
    if seen != len(graph):
        raise LedgerError(E_BAD_TASKS, "dependency cycle")


def _validate_specs(specs: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(specs, list) or not specs:
        raise LedgerError(E_BAD_TASKS, "at least one cloud task is required")
    by_id: Dict[str, Dict[str, Any]] = {}
    for spec in specs:
        if not isinstance(spec, dict):
            raise LedgerError(E_BAD_TASKS, "task spec must be an object")
        task = validate_task_id(spec.get("id"))
        if task in by_id:
            raise LedgerError(E_BAD_TASKS, "duplicate task id " + task)
        if spec.get("provider") not in PROVIDERS:
            raise LedgerError(E_BAD_TASKS, "provider for " + task)
        for key in ("depends_on", "owns"):
            value = spec.get(key) or []
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise LedgerError(E_BAD_TASKS, "%s for %s" % (key, task))
        if not isinstance(spec.get("verify_cmd") or "", str):
            raise LedgerError(E_BAD_TASKS, "verify_cmd for " + task)
        by_id[task] = spec
    graph: Dict[str, List[str]] = {}
    for task, spec in by_id.items():
        deps = list(spec.get("depends_on") or [])
        if task in deps or any(dep not in by_id for dep in deps):
            raise LedgerError(E_BAD_TASKS, "bad depends_on for " + task)
        graph[task] = deps
    _check_acyclic(graph)
    return by_id


def new_run(
    run: str,
    *,
    base_sha: str,
    origin: str,
    origin_url: str,
    policy: str,
    tasks: List[Dict[str, Any]],
    tasks_sha256: str,
    created_at: Optional[int] = None,
) -> Dict[str, Any]:
    """Build the section 2.3 document (revision 0).  Prompts never enter it.

    ``tasks`` are the *cloud* tasks only: ``{"id", "provider", "depends_on"?,
    "owns"?, "verify_cmd"?}`` in file order.  Every task starts ``waiting``.
    """
    validate_run_id(run)
    validate_oid(base_sha, "base_sha")
    if policy not in POLICIES:
        raise LedgerError(E_BAD_ARGUMENT, "policy")
    for label, value in (("origin", origin), ("origin_url", origin_url)):
        if not isinstance(value, str) or not value or len(value) > 2000 or re.search(r"[\s\x00-\x1f]", value):
            raise LedgerError(E_BAD_ARGUMENT, label)
    if not isinstance(tasks_sha256, str) or _SHA256_RE.fullmatch(tasks_sha256) is None:
        raise LedgerError(E_BAD_ARGUMENT, "tasks_sha256")
    by_id = _validate_specs(tasks)
    return {
        "schema": SCHEMA,
        "revision": 0,
        "run": run,
        "state": RUN_ACTIVE,
        "epoch": 1,
        "policy": policy,
        "base_sha": base_sha,
        "origin": origin,
        "origin_url": origin_url,
        "created_at": _now() if created_at is None else created_at,
        "heartbeat_at": None,
        "wake_seq": 0,
        "tasks_sha256": tasks_sha256,
        "integration": {"tip": base_sha, "history": []},
        "journal": [],
        "journal_seq": 0,
        "tasks": {task: _blank_task(run, task, spec, _new_nonce()) for task, spec in by_id.items()},
    }


def create_run_ledger(path: Any, doc: Dict[str, Any]) -> Dict[str, Any]:
    """Write a fresh ledger atomically; refuse to overwrite an existing one."""

    def fn(current: Dict[str, Any]) -> Dict[str, Any]:
        if current["revision"] != 0:
            raise LedgerError(E_EXISTS, "run ledger already exists")
        return current

    return locked_update(path, fn, create=lambda: copy.deepcopy(doc))


# --------------------------------------------------------------------------
# Document helpers
# --------------------------------------------------------------------------


def _tasks(doc: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    tasks = doc.get("tasks")
    if not isinstance(tasks, dict):
        raise LedgerError(E_CORRUPT, "tasks")
    return tasks


def _task(doc: Dict[str, Any], task: Any) -> Dict[str, Any]:
    entry = _tasks(doc).get(task) if isinstance(task, str) else None
    if entry is None:
        raise LedgerError(E_UNKNOWN_TASK, str(task))
    return entry


def _fence(entry: Dict[str, Any], generation: Any) -> None:
    if not _is_int(generation) or generation != entry["generation"]:
        raise LedgerError(E_STALE, "generation %r is not current (%r)" % (generation, entry["generation"]))


def touch_heartbeat(doc: Dict[str, Any], now: Optional[int] = None) -> Dict[str, Any]:
    doc["heartbeat_at"] = _now() if now is None else now
    return doc


def bump_epoch(doc: Dict[str, Any]) -> int:
    """Increment the epoch (call only while holding the orchestrator lock)."""
    if doc["epoch"] >= MAX_EPOCH:
        raise LedgerError(E_EPOCH, "epoch limit %d" % MAX_EPOCH)
    doc["epoch"] += 1
    return doc["epoch"]


def _bump_wake(doc: Dict[str, Any]) -> None:
    doc["wake_seq"] = int(doc.get("wake_seq", 0)) + 1


# --------------------------------------------------------------------------
# Journal (contract 6)
# --------------------------------------------------------------------------


def journal_begin(doc: Dict[str, Any], op: str, task: Optional[str], generation: Optional[int], data: Dict[str, Any]) -> int:
    """Append an unfinished entry and return its ``seq``.  Write it before acting."""
    if op not in JOURNAL_OPS:
        raise LedgerError(E_JOURNAL, "unknown op %r" % (op,))
    if not isinstance(data, dict):
        raise LedgerError(E_JOURNAL, "data must be an object")
    if op == OP_PARK:
        task, generation = None, None
    else:
        entry = _task(doc, task)
        validate_generation(generation)
        if op == OP_RELEASE:
            if generation > entry["generation"]:
                raise LedgerError(E_STALE, "release for a future generation")
        else:
            _fence(entry, generation)
    _check_journal_data(op, task, generation, data, doc["run"])
    seq = int(doc.get("journal_seq", 0)) + 1
    doc["journal_seq"] = seq
    doc.setdefault("journal", []).append(
        {"seq": seq, "op": op, "task": task, "generation": generation, "data": copy.deepcopy(data), "done": False}
    )
    return seq


_OID_DATA_KEYS = {
    OP_ADMIT: ("base_oid",),
    OP_INTEGRATE: ("expected_tip", "candidate_oid", "publish_oid", "tree"),
}


def _check_journal_data(op: str, task: Optional[str], generation: Optional[int], data: Dict[str, Any], run: str) -> None:
    """Per-op data contract (section 6); malformed data never reaches a replay."""
    try:
        if op in (OP_RESERVE, OP_RELEASE):
            if data.get("key") != "%s/%s/g%d" % (run, task, generation):
                raise LedgerError(E_JOURNAL, "key must be <run>/<task>/g<generation>")
            if op == OP_RESERVE and data.get("provider") not in PROVIDERS:
                raise LedgerError(E_JOURNAL, "provider")
            if "deadline_at" in data:
                _check_pos(data["deadline_at"])
        for name in _OID_DATA_KEYS.get(op, ()):
            validate_oid(data.get(name), name)
        if op == OP_INTEGRATE and ("test_rc" in data or "test_log_sha" in data):
            if not _is_int(data.get("test_rc")) or not isinstance(data.get("test_log_sha"), str) \
                    or len(data["test_log_sha"]) > 128:
                raise LedgerError(E_JOURNAL, "integrate data: test evidence")
    except LedgerError as exc:
        if exc.code == E_JOURNAL:
            raise
        raise LedgerError(E_JOURNAL, "%s data: %s" % (op, exc.detail))


def _journal_entry(doc: Dict[str, Any], seq: Any) -> Dict[str, Any]:
    for entry in doc.get("journal", []):
        if entry.get("seq") == seq:
            return entry
    raise LedgerError(E_JOURNAL, "no journal entry %r" % (seq,))


def journal_done(doc: Dict[str, Any], seq: int) -> Dict[str, Any]:
    """Mark an entry done (idempotent)."""
    entry = _journal_entry(doc, seq)
    entry["done"] = True
    return doc


def pending_journal(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    return sorted((e for e in doc.get("journal", []) if not e.get("done")), key=lambda e: e["seq"])


def journal_is_stale(doc: Dict[str, Any], entry: Dict[str, Any]) -> bool:
    """Generation fence for replay: everything but ``release`` dies with its generation."""
    if entry["op"] in (OP_PARK, OP_RELEASE) or entry.get("task") is None:
        return False
    task = _tasks(doc).get(entry["task"])
    return task is None or entry.get("generation") != task["generation"]


# Replay decisions as pure functions (the caller supplies the observed facts).


def replay_reserve(entry: Dict[str, Any], slot_present: bool) -> str:
    return "mark-done" if slot_present else "add-slot"


def replay_release(entry: Dict[str, Any], slot_present: bool) -> str:
    return "remove-slot" if slot_present else "mark-done"


def replay_admit(entry: Dict[str, Any], origin_oid: Optional[str]) -> str:
    """``origin_oid`` is the task branch tip on origin (``None`` when absent)."""
    expected = entry["data"].get("base_oid")
    if origin_oid is None:
        return "push-again"
    return "admitted" if origin_oid == expected else "needs-operator"


def known_publish_oids(doc: Dict[str, Any]) -> FrozenSet[str]:
    """Every ``publish_oid`` this ledger recorded as published (``integration.history``).  An integrate intent is
    NOT proof of publication: one whose compare-and-swap failed was closed for replanning."""
    return frozenset(h["publish_oid"] for h in doc.get("integration", {}).get("history", []) if h.get("publish_oid"))


def replay_integrate(entry: Dict[str, Any], ref_oid: Optional[str], *, publish_is_ancestor: bool = False,
                     known: FrozenSet[str] = frozenset()) -> str:
    """Decide what an unfinished ``integrate`` entry means given the observed integration ref.

    * ``complete``        the ref equals ``publish_oid`` exactly.
    * ``update-ref``      the ref still equals ``expected_tip``: apply the compare-and-swap again.
    * ``complete-known``  ``publish_oid`` is an ancestor of the ref AND the ref tip is itself a ``publish_oid``
                          this ledger recorded (history or a journaled integrate): FFS published a later commit.
    * ``foreign``         ``publish_oid`` is an ancestor but the tip is not one of ours: somebody else committed on
                          top.  The tip must never be adopted (a successor would launch from an ungated commit).
    * ``replan``          anything else: re-plan the merge from the ledger tip.

    Every FFS publish commit sits on top of its candidate's own commits, so "every commit in
    ``publish_oid..tip`` is a publish_oid" cannot hold for a real multi-commit candidate; the tip being a
    recorded publish_oid (whose own gates already ran) is the check that can.
    """
    data = entry["data"]
    if ref_oid is not None and ref_oid == data.get("publish_oid"):
        return "complete"
    if ref_oid == data.get("expected_tip"):
        return "update-ref"
    if publish_is_ancestor:
        return "complete-known" if ref_oid in known else "foreign"
    return "replan"


def recover_submitting(doc: Dict[str, Any]) -> List[str]:
    """Startup rule (14.3): any task found in ``submitting`` becomes ``uncertain``; never relaunched."""
    moved = []
    for name, entry in _tasks(doc).items():
        if entry["state"] == SUBMITTING:
            transition(doc, name, EV_CRASH_FOUND, generation=entry["generation"])
            moved.append(name)
    return moved


# --------------------------------------------------------------------------
# Transition table (contract 14.3 + 14.8), as data
# --------------------------------------------------------------------------

RESUME = "<resume-state>"  # destination is the state recorded when the task went to needs-operator
TransitionRow = Tuple[str, FrozenSet[str]]
TRANSITIONS: Dict[Tuple[str, str], TransitionRow] = {}


def _row(sources: Any, event: str, dest: str, blockers: Any = ()) -> None:
    """Add a row; a second row for the same ``(state, event)`` and destination widens its blocker set."""
    for source in ([sources] if isinstance(sources, str) else sources):
        previous = TRANSITIONS.get((source, event))
        merged = frozenset(blockers) | (previous[1] if previous and previous[0] == dest else frozenset())
        TRANSITIONS[(source, event)] = (dest, merged)


_row(WAITING, EV_DEPS_MET, ADMITTED)
_row(WAITING, EV_DEP_TERMINAL, FAILED, DEPENDENCY_BLOCKERS)
_row(WAITING, EV_NEEDS_OPERATOR, NEEDS_OPERATOR, [B_GRANT_MISSING, B_BRANCH_CONFLICT, B_SCAN_REFUSED, B_PUSH_FAILED])
_row(ADMITTED, EV_CAPACITY, RESERVED)
_row(ADMITTED, EV_NEEDS_OPERATOR, NEEDS_OPERATOR,
     [B_GRANT_MISSING, B_BRANCH_CONFLICT, B_SCAN_REFUSED, B_PUSH_FAILED])  # admit run while already admitted
_row(RESERVED, EV_SLOT_LOST, ADMITTED)  # the slot was reaped or released: re-reserve before any (re)submission
_row(RESERVED, EV_SUBMIT, SUBMITTING)
_row(RESERVED, EV_NEEDS_OPERATOR, NEEDS_OPERATOR,
     [B_EXPOSURE_NOT_GRANTED, B_RULESET_UNVERIFIED, B_GRANT_MISSING, B_SCAN_REFUSED, B_BRANCH_CONFLICT])
_row(SUBMITTING, EV_LAUNCH_OK, LAUNCHED)
_row(SUBMITTING, EV_LAUNCH_FAILED, FAILED)
_row(SUBMITTING, EV_CRASH_FOUND, UNCERTAIN)
_row([LAUNCHED, RUNNING], EV_POLL_RUNNING, RUNNING)
_row([LAUNCHED, RUNNING], EV_BIND, RESULT_READY)
_row([LAUNCHED, RUNNING], EV_DEADLINE, FAILED, [B_DEADLINE])
_row([LAUNCHED, RUNNING], EV_POLL_ERRORS, NEEDS_OPERATOR, [B_POLL_ERRORS])
_row([LAUNCHED, RUNNING], EV_NEEDS_OPERATOR, NEEDS_OPERATOR, [B_AMBIGUOUS, B_CANDIDATE_MOVED])
_row(UNCERTAIN, EV_LAUNCH_FOUND, LAUNCHED)
_row(UNCERTAIN, EV_UNCERTAIN_TIMEOUT, NEEDS_OPERATOR, [B_UNCERTAIN_LAUNCH])
_row(RESULT_READY, EV_UNBIND, RUNNING)  # the bound candidate was somebody else's: keep polling
_row(RESULT_READY, EV_COLLECT_OK, COLLECTED)
_row(RESULT_READY, EV_REJECT, REJECTED, [B_SECRET_LEAK, B_QUARANTINE_REFUSED])
_row(RESULT_READY, EV_NEEDS_OPERATOR, NEEDS_OPERATOR,
     [B_MANIFEST_BLOCKED, B_MANIFEST_INVALID, B_PATH_OUTSIDE_OWNS, B_DENYLIST, B_DEPENDENCY_CHANGE])
_row(COLLECTED, EV_REVIEW_APPROVE, REVIEWED)
_row(COLLECTED, EV_REJECT, REJECTED, [B_REVIEW_REVISE, B_SECRET_LEAK])  # the review scan found a credential
_row(COLLECTED, EV_NEEDS_OPERATOR, NEEDS_OPERATOR,
     [B_REVIEW_UNAVAILABLE, B_REVIEW_CAP, B_REVIEW_MODEL_DOWNGRADED, B_REVIEW_MODEL_UNKNOWN])
_row([REVIEWED, VERIFIED], EV_REVIEW_STALE, COLLECTED)  # legacy approval: review and verify again
_row(REVIEWED, EV_TESTS_PASS, VERIFIED)
_row(REVIEWED, EV_REJECT, REJECTED, [B_TESTS_FAILED])
_row(REVIEWED, EV_NEEDS_OPERATOR, NEEDS_OPERATOR, [B_SANDBOX_UNAVAILABLE, B_VERIFY_CMD_MISSING])
_row(VERIFIED, EV_MERGE_PLANNED, INTEGRATING)
_row(VERIFIED, EV_REJECT, REJECTED, [B_TESTS_FAILED])  # merged-tree test run failed
_row(VERIFIED, EV_NEEDS_OPERATOR, NEEDS_OPERATOR,
     [B_MERGE_CONFLICT, B_PATCH_ID_MISMATCH, B_SANDBOX_UNAVAILABLE, B_VERIFY_CMD_MISSING])
# collect/review/merge may refuse for content the reviewer cannot read (binary)
_row([RESULT_READY, COLLECTED, REVIEWED, VERIFIED], EV_NEEDS_OPERATOR, NEEDS_OPERATOR, [B_BINARY_CHANGE])
_row(INTEGRATING, EV_INTEGRATED, INTEGRATED)
_row(INTEGRATING, EV_REPLAN, VERIFIED)
_row(INTEGRATING, EV_NEEDS_OPERATOR, NEEDS_OPERATOR, [B_MERGE_CONFLICT, B_PATCH_ID_MISMATCH, B_INTEGRATION_FOREIGN])
# a per-task step that keeps failing, or a blocker we do not know, needs a human rather than killing the run
_row([WAITING, ADMITTED, RESERVED, LAUNCHED, RUNNING, UNCERTAIN, RESULT_READY, COLLECTED, REVIEWED, VERIFIED,
      INTEGRATING], EV_NEEDS_OPERATOR, NEEDS_OPERATOR, [B_INTERNAL_ERROR])
# operator verbs
_row([NEEDS_OPERATOR, UNCERTAIN], EV_ADOPT, RESULT_READY)
_row([NEEDS_OPERATOR, UNCERTAIN], EV_ADOPT_REMOTE, LAUNCHED)  # Codex: bind the session id, then the normal fetch
_row(NEEDS_OPERATOR, EV_RETEST, RESUME)
_row(NEEDS_OPERATOR, EV_ACCEPT_DEPS, RESUME)
_row(NEEDS_OPERATOR, EV_OPERATOR_REJECT, REJECTED)
_row(NON_TERMINAL, EV_ABANDON, ABANDONED)
_row(NON_TERMINAL | frozenset([REJECTED, FAILED]), EV_ABANDON_RETRY, WAITING)
_row(FAILED, EV_DEP_REOPEN, WAITING)  # only for ``dependency-*`` failures (checked in _plan)

EVENT_FIELDS: Dict[str, Tuple[str, ...]] = {
    EV_DEPS_MET: ("base_oid", "task_branch_oid"),
    EV_LAUNCH_OK: ("launched_at", "deadline_at", "remote_id", "url", "launch_note"),
    EV_POLL_RUNNING: ("remote_id", "url"),
    EV_LAUNCH_FOUND: ("remote_id", "url"),
    EV_BIND: ("bound_oid", "bound_ref"),
    EV_ADOPT: ("bound_oid",),
    EV_ADOPT_REMOTE: ("remote_id", "url", "launched_at", "deadline_at"),
    EV_COLLECT_OK: ("candidate_oid", "decisions_needed", "env_report"),
    EV_REVIEW_APPROVE: ("review",),
    EV_TESTS_PASS: ("verify",),
    EV_NEEDS_OPERATOR: ("decisions_needed", "env_report", "review", "verify", "deps_digest"),
    EV_REJECT: ("decisions_needed", "env_report", "review", "verify"),
    EV_ABANDON: ("nonce",),
    EV_ABANDON_RETRY: ("nonce",),
}
REQUIRED_FIELDS: Dict[str, Tuple[str, ...]] = {
    EV_LAUNCH_OK: ("launched_at", "deadline_at"),
    EV_ADOPT_REMOTE: ("remote_id", "launched_at", "deadline_at"),
    EV_BIND: ("bound_oid",),
    EV_ADOPT: ("bound_oid",),
    EV_COLLECT_OK: ("candidate_oid",),
    EV_REVIEW_APPROVE: ("review",),
    EV_TESTS_PASS: ("verify",),
}
BUMP_GENERATION_EVENTS = frozenset([EV_ABANDON, EV_ABANDON_RETRY])
KEEP_BLOCKER_EVENTS = frozenset([EV_OPERATOR_REJECT])
# Events whose current-blocker precondition is the operator table (14.8).
OPERATOR_BLOCKER_RULES: Dict[str, FrozenSet[str]] = {
    EV_ADOPT: ADOPT_BLOCKERS,
    EV_ADOPT_REMOTE: ADOPT_BLOCKERS,
    EV_RETEST: RESOLVE_ACTIONS[ACTION_RETEST],
    EV_ACCEPT_DEPS: RESOLVE_ACTIONS[ACTION_ACCEPT_DEPS],
    EV_OPERATOR_REJECT: RESOLVE_ACTIONS[ACTION_REJECT],
}
def review_has_model_evidence(review: Any) -> bool:
    """True when an approval names the requested and the answering reviewer model and they are the same one.

    ``review_model_problem`` only lets an approval through in that case, so every approval recorded since the
    reviewer-model check carries both fields. An older record has neither and must be reviewed again."""
    if not isinstance(review, dict):
        return False
    requested, answered = review.get("requested_model"), review.get("model")
    return bool(requested) and isinstance(requested, str) and requested == answered


_REVIEW_KEYS = frozenset(["family", "verdict", "diff_sha", "patch_id", "tree"])
#: Recorded since the reviewer-model check; absent from older records, which stay valid.
_REVIEW_OPTIONAL_KEYS = frozenset(["model", "requested_model"])
_VERIFY_KEYS = frozenset(["test_rc", "test_log_sha"])


def _check_nonneg(value: Any) -> int:
    if not _is_int(value) or value < 0:
        raise LedgerError(E_BAD_FIELD, "expected a non-negative integer")
    return value


def _check_pos(value: Any) -> int:
    if not _is_int(value) or value <= 0:
        raise LedgerError(E_BAD_FIELD, "expected a positive integer")
    return value


def _check_ref(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or _REMOTE_RE.fullmatch(value) is None:
        raise LedgerError(E_BAD_FIELD, "bound_ref")
    return value


def _check_digest(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{12,128}", value) is None:
        raise LedgerError(E_BAD_FIELD, "deps_digest")
    return value


def _check_str_dict(value: Any, keys: FrozenSet[str], label: str,
                    optional: FrozenSet[str] = frozenset()) -> Dict[str, Any]:
    if not isinstance(value, dict) or not keys <= set(value) <= (keys | optional):
        raise LedgerError(E_BAD_FIELD, label)
    for item in value.values():
        if not (isinstance(item, str) and len(item) <= 200) and not _is_int(item):
            raise LedgerError(E_BAD_FIELD, label)
    return dict(value)


def _check_decisions(value: Any) -> List[str]:
    return _manifest_str_list(value, "decisions_needed", LedgerError, E_BAD_FIELD)


def _check_env_report(value: Any) -> Dict[str, Any]:
    try:
        return _validate_env_report(value)
    except ManifestError as exc:
        raise LedgerError(E_BAD_FIELD, "env_report: " + exc.code)


_FIELD_CHECKS: Dict[str, Callable[[Any], Any]] = {
    "base_oid": lambda v: validate_oid(v, "base_oid"),
    "task_branch_oid": lambda v: validate_oid(v, "task_branch_oid"),
    "bound_oid": lambda v: validate_oid(v, "bound_oid"),
    "bound_ref": lambda v: _check_ref(v),
    "deps_digest": lambda v: _check_digest(v),
    "candidate_oid": lambda v: validate_oid(v, "candidate_oid"),
    "launched_at": _check_nonneg,
    "deadline_at": _check_pos,
    "remote_id": sanitize_remote_id,
    "url": sanitize_url,
    "launch_note": lambda v: _check_launch_note(v),
    "nonce": validate_nonce,
    "review": lambda v: _check_str_dict(v, _REVIEW_KEYS, "review", _REVIEW_OPTIONAL_KEYS),
    "verify": lambda v: _check_str_dict(v, _VERIFY_KEYS, "verify"),
    "decisions_needed": _check_decisions,
    "env_report": _check_env_report,
}


def failed_dependency(doc: Dict[str, Any], entry: Dict[str, Any]) -> Optional[str]:
    """State of the first dependency that is terminal and not integrated, else ``None``."""
    for dep in entry["depends_on"]:
        state = _task(doc, dep)["state"]
        if state in TERMINAL and state != INTEGRATED:
            return state
    return None


def dependencies_integrated(doc: Dict[str, Any], entry: Dict[str, Any]) -> bool:
    return all(_task(doc, dep)["state"] == INTEGRATED for dep in entry["depends_on"])


def _resolve_blocker(event: str, allowed: FrozenSet[str], fields: Dict[str, Any]) -> Optional[str]:
    given = fields.get("blocker")
    if not allowed:
        if given is not None:
            raise LedgerError(E_BAD_BLOCKER, "event %s takes no blocker" % event)
        return None
    if given is None:
        if len(allowed) == 1:
            return next(iter(allowed))
        raise LedgerError(E_BAD_BLOCKER, "event %s requires a blocker" % event)
    if given not in allowed:
        raise LedgerError(E_BAD_BLOCKER, "%r not allowed for event %s" % (given, event))
    return str(given)


def _check_fields(event: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    allowed = set(EVENT_FIELDS.get(event, ())) | {"blocker"}
    extra = set(fields) - allowed
    if extra:
        raise LedgerError(E_BAD_FIELD, "unexpected fields for %s: %s" % (event, sorted(extra)))
    missing = [name for name in REQUIRED_FIELDS.get(event, ()) if name not in fields]
    if missing:
        raise LedgerError(E_BAD_FIELD, "missing fields for %s: %s" % (event, missing))
    return {name: _FIELD_CHECKS[name](value) for name, value in fields.items() if name != "blocker"}


def _check_guards(doc: Dict[str, Any], entry: Dict[str, Any], event: str, blocker: Optional[str]) -> None:
    if event == EV_DEPS_MET and not dependencies_integrated(doc, entry):
        raise LedgerError(E_ILLEGAL, "dependencies are not all integrated")
    if event == EV_DEP_TERMINAL:
        state = failed_dependency(doc, entry)
        if state is None or blocker != "dependency-" + state:
            raise LedgerError(E_ILLEGAL, "no matching terminal dependency")
    if event == EV_DEP_REOPEN and not str(entry.get("blocker") or "").startswith("dependency-"):
        raise LedgerError(E_ILLEGAL, "only dependency failures can be reopened")
    if event == EV_BIND and entry.get("bound_oid") not in (None,):
        raise LedgerError(E_ILLEGAL, "first bind wins; bound_oid already set")
    rule = OPERATOR_BLOCKER_RULES.get(event)
    if rule is not None and not (event in (EV_ADOPT, EV_ADOPT_REMOTE) and entry["state"] == UNCERTAIN):
        if entry.get("blocker") not in rule:
            raise LedgerError(E_BLOCKER_MISMATCH, "blocker %r does not allow %s" % (entry.get("blocker"), event))


def _plan(doc: Dict[str, Any], entry: Dict[str, Any], event: str, fields: Dict[str, Any],
          journal_seq: Optional[int]) -> Dict[str, Any]:
    row = TRANSITIONS.get((entry["state"], event))
    if row is None:
        raise LedgerError(E_ILLEGAL, "%s --%s--> ?" % (entry["state"], event))
    dest, allowed = row
    checked = _check_fields(event, fields)
    blocker = _resolve_blocker(event, allowed, fields)
    _check_guards(doc, entry, event, blocker)
    if dest == RESUME:
        dest = entry.get("resume_state") or ""
        if dest not in ALL_STATES or (event == EV_ACCEPT_DEPS and dest != RESULT_READY):
            raise LedgerError(E_ILLEGAL, "no valid resume state recorded")
    if event in BUMP_GENERATION_EVENTS and entry["generation"] >= MAX_GENERATION:
        raise LedgerError(E_BAD_ID, "generation limit")
    if journal_seq is not None:
        pending = _journal_entry(doc, journal_seq)
        if pending.get("task") is not None and (
            pending["task"] != _task_name(doc, entry) or pending["generation"] != entry["generation"]
        ):
            raise LedgerError(E_JOURNAL, "journal entry does not belong to this task generation")
    return {"dest": dest, "blocker": blocker, "fields": checked, "journal_seq": journal_seq}


def _task_name(doc: Dict[str, Any], entry: Dict[str, Any]) -> str:
    for name, candidate in _tasks(doc).items():
        if candidate is entry:
            return name
    raise LedgerError(E_UNKNOWN_TASK, "entry not in document")


def _commit(doc: Dict[str, Any], name: str, entry: Dict[str, Any], event: str, plan: Dict[str, Any]) -> None:
    source, dest = entry["state"], plan["dest"]
    held_before_generation = entry["generation"]
    release_needed = _may_hold_slot(entry) and dest not in SLOT_MAYBE_HELD
    entry.update(plan["fields"])
    if "env_report" in plan["fields"]:
        entry["env_report_source"] = "worker"
    if event in BUMP_GENERATION_EVENTS:
        _reset_for_new_generation(doc["run"], name, entry, plan["fields"].get("nonce") or _new_nonce())
    if event == EV_ACCEPT_DEPS:
        entry["deps_accepted"] = True
    if event == EV_ADOPT:
        entry["seen_oid"], entry["seen_count"], entry["bound_ref"] = None, 0, None  # operator-chosen: no poll ref
    if event == EV_ADOPT_REMOTE:
        entry["poll_errors"], entry["uncertain_ticks"] = 0, 0
    if event == EV_UNBIND:
        entry["bound_oid"] = entry["bound_ref"] = entry["seen_oid"] = None
        entry["seen_count"] = 0
    if event == EV_REVIEW_STALE:  # the old approval and its test evidence no longer count; a fresh round meter
        entry["review"] = entry["verify"] = None
        entry["review_attempts"] = int(entry.get("review_attempts") or 0) + 1
    if event == EV_RETEST:
        entry["push_failures"] = 0
        if entry.get("blocker") in REVIEW_BLOCKERS:
            entry["review_attempts"] = int(entry.get("review_attempts") or 0) + 1
    if event not in KEEP_BLOCKER_EVENTS:
        entry["blocker"] = plan["blocker"] if dest in (NEEDS_OPERATOR, REJECTED, FAILED) else None
    if dest == NEEDS_OPERATOR:
        entry["resume_state"] = VERIFIED if source == INTEGRATING else source
    else:
        entry["resume_state"] = None
    entry["state"] = dest
    if dest != NEEDS_OPERATOR and dest != source:
        entry["slot_released"] = False  # a later stay in an operator state starts a fresh slot accounting
    if release_needed:
        journal_begin(doc, OP_RELEASE, name, held_before_generation,
                      {"key": "%s/%s/g%d" % (doc["run"], name, held_before_generation), "provider": entry["provider"]})
    if plan["journal_seq"] is not None:
        journal_done(doc, plan["journal_seq"])


def _may_hold_slot(entry: Dict[str, Any]) -> bool:
    """A slot may exist for this task right now.

    ``needs-operator`` keeps the slot only when it was entered from a state that
    held one (``resume_state``); a task parked from ``waiting`` never reserved
    anything, so leaving it must not journal a release (a stale release could
    delete the live slot of a later reservation under the same key).
    """
    if entry.get("slot_released"):
        return False  # the deadline release already journaled it exactly once
    state = entry["state"]
    if state == NEEDS_OPERATOR:
        state = entry.get("resume_state")
    return state in ACTIVE_SET or state == ADMITTED


def _reset_for_new_generation(run: str, name: str, entry: Dict[str, Any], nonce: str) -> None:
    entry["generation"] += 1
    entry["nonce"] = nonce
    entry["task_branch"] = task_branch_ref(run, name, entry["generation"])
    for field, factory in _RUNTIME_RESET.items():
        entry[field] = factory()


def transition(doc: Dict[str, Any], task: str, event: str, *, generation: int,
               journal_seq: Optional[int] = None, **fields: Any) -> Dict[str, Any]:
    """Apply one legal move or raise without touching ``doc``.

    ``generation`` is mandatory: it must equal the task's current generation or
    ``STALE_GENERATION`` is raised (the caller records the result as superseded
    in its log).  ``journal_seq`` marks that journal entry done in the same
    mutation.  A transition that stops a task from possibly holding a slot
    appends a ``release`` journal entry in the same mutation.  ``blocker=`` is
    optional when the event allows exactly one blocker.
    """
    entry = _task(doc, task)
    _fence(entry, generation)
    plan = _plan(doc, entry, event, fields, journal_seq)
    _commit(doc, task, entry, event, plan)
    return doc


# Non-state bookkeeping that the orchestrator performs every poll.


def note_poll(doc: Dict[str, Any], task: str, *, generation: int, ok: bool) -> int:
    """Record a poll outcome; returns ``poll_errors``.  Over ``POLL_ERROR_LIMIT`` the caller fires ``EV_POLL_ERRORS``."""
    entry = _task(doc, task)
    _fence(entry, generation)
    if entry["state"] not in (LAUNCHED, RUNNING):
        raise LedgerError(E_ILLEGAL, "poll bookkeeping outside launched/running")
    entry["poll_errors"] = 0 if ok else entry["poll_errors"] + 1
    return int(entry["poll_errors"])


def note_seen(doc: Dict[str, Any], task: str, *, generation: int, oid: Optional[str]) -> int:
    """Count consecutive polls that saw the same candidate OID (``None`` resets)."""
    entry = _task(doc, task)
    _fence(entry, generation)
    if oid is None:
        entry["seen_oid"], entry["seen_count"] = None, 0
    else:
        validate_oid(oid, "seen_oid")
        same = entry["seen_oid"] == oid
        entry["seen_oid"], entry["seen_count"] = oid, (entry["seen_count"] + 1 if same else 1)
    return int(entry["seen_count"])


def bind_condition_met(entry: Dict[str, Any]) -> bool:
    return entry.get("seen_oid") is not None and entry.get("seen_count", 0) >= BIND_CONSECUTIVE_POLLS


def poll_errors_exceeded(entry: Dict[str, Any]) -> bool:
    return entry.get("poll_errors", 0) > POLL_ERROR_LIMIT


def note_uncertain_tick(doc: Dict[str, Any], task: str, *, generation: int) -> int:
    """Record that one recovery poll of an ``uncertain`` launch ran (stored in the ledger, never in memory)."""
    entry = _task(doc, task)
    _fence(entry, generation)
    if entry["state"] != UNCERTAIN:
        raise LedgerError(E_ILLEGAL, "uncertain bookkeeping outside uncertain")
    entry["uncertain_ticks"] = int(entry.get("uncertain_ticks") or 0) + 1
    return int(entry["uncertain_ticks"])


def uncertain_poll_budget_spent(entry: Dict[str, Any]) -> bool:
    return int(entry.get("uncertain_ticks") or 0) >= UNCERTAIN_POLLS.get(entry.get("provider"), 1)


def note_push_failure(doc: Dict[str, Any], task: str, *, generation: int) -> int:
    """Count one failed admit push; the caller escalates to ``push-failed`` at ``PUSH_FAILURE_LIMIT``."""
    entry = _task(doc, task)
    _fence(entry, generation)
    entry["push_failures"] = int(entry.get("push_failures") or 0) + 1
    return int(entry["push_failures"])


def reject_candidate(doc: Dict[str, Any], task: str, *, generation: int, ref: str, oid: str) -> None:
    entry = _task(doc, task)
    _fence(entry, generation)
    validate_oid(oid, "candidate")
    if not isinstance(ref, str) or not _REMOTE_RE.fullmatch(ref):
        raise LedgerError(E_BAD_FIELD, "candidate ref")
    pair = [ref, oid]
    if pair not in entry["rejected_candidates"]:
        entry["rejected_candidates"].append(pair)


# --------------------------------------------------------------------------
# Multi-step helpers that must land in one write
# --------------------------------------------------------------------------


def begin_integration(doc: Dict[str, Any], task: str, *, generation: int, expected_tip: str, candidate_oid: str,
                      publish_oid: str, tree: str, test_rc: Optional[int] = None,
                      test_log_sha: Optional[str] = None) -> int:
    """``verified -> integrating`` plus the ``integrate`` journal entry, in one mutation.

    ``test_rc``/``test_log_sha`` are the evidence for the tree being published (the merged-tree retest when the
    plan merged); they are journaled so a replay after a crash records them, not the candidate's own log."""
    for label, value in (("expected_tip", expected_tip), ("candidate_oid", candidate_oid),
                         ("publish_oid", publish_oid), ("tree", tree)):
        validate_oid(value, label)
    evidence: Dict[str, Any] = {}
    if test_rc is not None or test_log_sha is not None:
        if not _is_int(test_rc) or not isinstance(test_log_sha, str) or len(test_log_sha) > 128:
            raise LedgerError(E_BAD_FIELD, "test evidence")
        evidence = {"test_rc": test_rc, "test_log_sha": test_log_sha}
    entry = _task(doc, task)
    _fence(entry, generation)
    _plan(doc, entry, EV_MERGE_PLANNED, {}, None)  # legality check before any mutation
    seq = journal_begin(doc, OP_INTEGRATE, task, generation, dict({
        "expected_tip": expected_tip, "candidate_oid": candidate_oid, "publish_oid": publish_oid, "tree": tree},
        **evidence))
    transition(doc, task, EV_MERGE_PLANNED, generation=generation)
    return seq


def complete_integration(doc: Dict[str, Any], task: str, *, generation: int, seq: int, publish_oid: str,
                         candidate_oid: str, tree: str, test_rc: int, test_log_sha: str,
                         tip: Optional[str] = None) -> Dict[str, Any]:
    """The single atomic update of 14.3: state ``integrated``, ``integration.tip``,
    a history entry and the journal entry done.  Call it inside one ``locked_update``."""
    for label, value in (("publish_oid", publish_oid), ("candidate_oid", candidate_oid), ("tree", tree)):
        validate_oid(value, label)
    new_tip = validate_oid(tip if tip is not None else publish_oid, "tip")
    if not _is_int(test_rc) or not isinstance(test_log_sha, str) or len(test_log_sha) > 128:
        raise LedgerError(E_BAD_FIELD, "test evidence")
    entry = _task(doc, task)
    _fence(entry, generation)
    pending = _journal_entry(doc, seq)
    if pending["op"] != OP_INTEGRATE or pending.get("task") != task or pending.get("done"):
        raise LedgerError(E_JOURNAL, "not a pending integrate entry for this task")
    plan = _plan(doc, entry, EV_INTEGRATED, {}, seq)  # validates state and journal ownership
    _commit(doc, task, entry, EV_INTEGRATED, plan)
    doc["integration"]["tip"] = new_tip
    doc["integration"]["history"].append({
        "task": task, "generation": generation, "publish_oid": publish_oid, "candidate_oid": candidate_oid,
        "tree": tree, "test_rc": test_rc, "test_log_sha": test_log_sha})
    return doc


# --------------------------------------------------------------------------
# Graph queries, run state, park/wake (14.4)
# --------------------------------------------------------------------------


def next_admissible(doc: Dict[str, Any]) -> List[str]:
    """``waiting`` tasks whose dependencies are all integrated, in file order."""
    return [name for name, entry in _tasks(doc).items() if entry["state"] == WAITING and dependencies_integrated(doc, entry)]


def propagate_dependency_failures(doc: Dict[str, Any]) -> List[str]:
    """Fail every ``waiting`` task with a terminal, non-integrated dependency (14.3)."""
    failed: List[str] = []
    changed = True
    while changed:
        changed = False
        for name, entry in _tasks(doc).items():
            state = failed_dependency(doc, entry) if entry["state"] == WAITING else None
            if state is not None:
                transition(doc, name, EV_DEP_TERMINAL, generation=entry["generation"], blocker="dependency-" + state)
                failed.append(name)
                changed = True
    return failed


def run_is_done(doc: Dict[str, Any]) -> bool:
    tasks = _tasks(doc)
    return bool(tasks) and all(entry["state"] in TERMINAL for entry in tasks.values())


def finish_run(doc: Dict[str, Any]) -> bool:
    """Set ``state: done`` when every task is terminal."""
    if run_is_done(doc):
        doc["state"] = RUN_DONE
        return True
    return False


def resume_run(doc: Dict[str, Any]) -> Dict[str, Any]:
    """``parked -> active`` (a ``done`` run never resumes)."""
    if doc["state"] == RUN_DONE:
        raise LedgerError(E_RUN_DONE, doc["run"])
    doc["state"] = RUN_ACTIVE
    return doc


def _can_progress(doc: Dict[str, Any], entry: Dict[str, Any]) -> bool:
    state = entry["state"]
    if state == UNCERTAIN:
        # An uncertain launch gets its recovery polls first (14.3); the run only parks on it when the
        # ledger-stored tick counter says those polls already happened and the timeout did not fire.
        return int(entry.get("uncertain_ticks") or 0) <= UNCERTAIN_POLLS.get(entry.get("provider"), 1)
    if state in TERMINAL or state == NEEDS_OPERATOR:
        return False
    if state == WAITING:
        return dependencies_integrated(doc, entry) or failed_dependency(doc, entry) is not None
    return True


def park_condition(doc: Dict[str, Any]) -> bool:
    """No task can progress, none is mid-journal, and at least one needs an operator (section 4)."""
    tasks = _tasks(doc).values()
    if doc.get("state") == RUN_DONE or not any(t["state"] in (NEEDS_OPERATOR, UNCERTAIN) for t in tasks):
        return False
    if any(e["op"] != OP_PARK for e in pending_journal(doc)):
        return False
    return not any(_can_progress(doc, entry) for entry in tasks)


def try_park(doc: Dict[str, Any], seen_wake_seq: int) -> bool:
    """Park handshake (14.4); call inside ``locked_update``.

    Returns ``False`` without touching ``doc`` when ``wake_seq`` moved since the
    tick began or the park condition no longer holds.  Otherwise sets
    ``state: parked`` and appends the unfinished ``park`` journal entry in the
    same mutation (the caller notifies, then ``journal_done``).
    """
    if doc.get("state") != RUN_ACTIVE or doc.get("wake_seq") != seen_wake_seq or not park_condition(doc):
        return False
    doc["state"] = RUN_PARKED
    journal_begin(doc, OP_PARK, None, None, {})
    return True


def park_run(path: Any, seen_wake_seq: int) -> bool:
    outcome = {"parked": False}

    def fn(doc: Dict[str, Any]) -> Any:
        outcome["parked"] = try_park(doc, seen_wake_seq)
        return doc if outcome["parked"] else UNCHANGED

    locked_update(path, fn)
    return outcome["parked"]


# --------------------------------------------------------------------------
# Operator mutations (14.8); each bumps wake_seq
# --------------------------------------------------------------------------


def _require_mutable(doc: Dict[str, Any]) -> None:
    if doc.get("state") == RUN_DONE:
        raise LedgerError(E_RUN_DONE, "run is done")


def apply_adopt(doc: Dict[str, Any], task: str, oid: str) -> Dict[str, Any]:
    """Bind ``oid`` to an ``uncertain`` task or a ``needs-operator`` task whose blocker is adoptable."""
    _require_mutable(doc)
    entry = _task(doc, task)
    transition(doc, task, EV_ADOPT, generation=entry["generation"], bound_oid=oid)
    _bump_wake(doc)
    return doc


def apply_adopt_remote(doc: Dict[str, Any], task: str, remote_id: str, *, launched_at: int, deadline_at: int,
                       url: Optional[str] = None) -> Dict[str, Any]:
    """Codex: bind the operator-supplied session id and go back to ``launched`` (the normal fetch follows).

    The task may have released its slot at the deadline, so a ``reserve`` journal entry is added in the same
    write; replay makes it a no-op when the slot still exists and re-adds it when capacity allows.
    """
    _require_mutable(doc)
    entry = _task(doc, task)
    if entry["provider"] != "codex":
        raise LedgerError(E_BAD_ARGUMENT, "--remote-id applies to Codex tasks; Claude results bind by nonce (use --oid)")
    generation = entry["generation"]
    fields: Dict[str, Any] = {"remote_id": remote_id, "launched_at": launched_at, "deadline_at": deadline_at}
    if url is not None:
        fields["url"] = url
    transition(doc, task, EV_ADOPT_REMOTE, generation=generation, **fields)
    journal_begin(doc, OP_RESERVE, task, generation, {
        "key": "%s/%s/g%d" % (doc["run"], task, generation), "provider": entry["provider"], "deadline_at": deadline_at})
    _bump_wake(doc)
    return doc


def unbind_candidate(doc: Dict[str, Any], task: str, *, generation: int, ref: str, oid: str, deadline_at: int) -> None:
    """``result-ready -> running``: the bound candidate was another task's (or another nonce's).

    One write records the pair in ``rejected_candidates``, clears the bind and journals a slot ``reserve``
    (the bind released the slot; the cloud session is still running).  Nothing is pending afterwards, so this
    needs no replay; the journal entry is the durable ``unbind`` record of the slot side.
    """
    entry = _task(doc, task)
    _fence(entry, generation)
    if entry["state"] != RESULT_READY or entry.get("bound_oid") != oid:
        raise LedgerError(E_ILLEGAL, "unbind: task is not bound to that candidate")
    reject_candidate(doc, task, generation=generation, ref=ref, oid=oid)
    transition(doc, task, EV_UNBIND, generation=generation)
    journal_begin(doc, OP_RESERVE, task, generation, {
        "key": "%s/%s/g%d" % (doc["run"], task, generation), "provider": entry["provider"], "deadline_at": deadline_at})


def release_expired_slots(doc: Dict[str, Any], now: float, deadline_of: Callable[[str, Dict[str, Any]], float]) -> List[str]:
    """Contract 14.3: an ``uncertain``/``needs-operator`` task that may still hold a slot releases it once
    ``now > deadline``.  Journals one ``release`` per task generation (``slot_released`` makes it exactly once)."""
    released: List[str] = []
    for name, entry in _tasks(doc).items():
        if entry["state"] in (NEEDS_OPERATOR, UNCERTAIN) and _may_hold_slot(entry) and now > deadline_of(name, entry):
            journal_begin(doc, OP_RELEASE, name, entry["generation"], {
                "key": "%s/%s/g%d" % (doc["run"], name, entry["generation"]), "provider": entry["provider"]})
            entry["slot_released"] = True
            released.append(name)
    return released


def _pending_integrate(doc: Dict[str, Any], task: str) -> bool:
    return any(e["op"] == OP_INTEGRATE and e.get("task") == task for e in pending_journal(doc))


def _reopen_dependents(doc: Dict[str, Any], root: str) -> List[str]:
    """After ``abandon --retry`` of ``root``, dependents that failed only because of it go back to ``waiting``."""
    reopened: List[str] = []
    frontier = [root]
    while frontier:
        current = frontier.pop()
        for name, entry in _tasks(doc).items():
            if current in entry["depends_on"] and name not in reopened:
                if entry["state"] == FAILED and str(entry.get("blocker") or "").startswith("dependency-"):
                    transition(doc, name, EV_DEP_REOPEN, generation=entry["generation"])
                    reopened.append(name)
                    frontier.append(name)
    return reopened


def apply_abandon(doc: Dict[str, Any], task: str, retry: bool = False, *, nonce: Optional[str] = None) -> Dict[str, Any]:
    """Bump the generation (new nonce); the task becomes ``abandoned`` or, with ``retry``, ``waiting``.

    ``--retry`` is also accepted from ``rejected``/``failed`` (14.8) and re-opens
    dependents that failed with ``dependency-*``.  A release is journaled when a slot may exist.
    Refused while the task is ``integrating`` (or has a pending ``integrate`` entry): the journal says
    ``update-ref`` may already have moved the ref, and a generation bump would drop the entry and leave the
    ref and the ledger disagreeing.  ``abandon --retry`` of a rejected/failed task reopens a ``done`` run.
    """
    entry = _task(doc, task)
    if entry["state"] == INTEGRATING or _pending_integrate(doc, task):
        raise LedgerError(E_INTEGRATING, "task %s is integrating; retry after the orchestrator's next tick" % task)
    reopen = retry and doc.get("state") == RUN_DONE and entry["state"] in (REJECTED, FAILED)
    if not reopen:
        _require_mutable(doc)
    extra = {"nonce": nonce} if nonce is not None else {}
    transition(doc, task, EV_ABANDON_RETRY if retry else EV_ABANDON, generation=entry["generation"], **extra)
    if retry:
        _reopen_dependents(doc, task)
    if reopen:
        doc["state"] = RUN_ACTIVE  # the next orchestrate opens a new epoch (its lifecycle record is terminal)
    _bump_wake(doc)
    return doc


def apply_resolve(doc: Dict[str, Any], task: str, action: str) -> Dict[str, Any]:
    """``resolve --action retest|accept-deps|reject``; refused unless the blocker is in the row for the action."""
    _require_mutable(doc)
    event = {ACTION_RETEST: EV_RETEST, ACTION_ACCEPT_DEPS: EV_ACCEPT_DEPS, ACTION_REJECT: EV_OPERATOR_REJECT}.get(action)
    if event is None:
        raise LedgerError(E_BAD_ARGUMENT, "action %r" % (action,))
    entry = _task(doc, task)
    if entry["state"] != NEEDS_OPERATOR:
        raise LedgerError(E_BLOCKER_MISMATCH, "task is not needs-operator")
    if (event == EV_RETEST and entry.get("blocker") in REVIEW_BLOCKERS
            and int(entry.get("review_attempts") or 0) >= REVIEW_RETEST_LIMIT):
        raise LedgerError(E_BLOCKER_MISMATCH, "review retest limit (%d) reached; use --action reject or abandon" % REVIEW_RETEST_LIMIT)
    transition(doc, task, event, generation=entry["generation"])
    _bump_wake(doc)
    return doc


# --------------------------------------------------------------------------
# Slots ledger (contract 2.4)
# --------------------------------------------------------------------------


def _new_slots_doc(limits: Dict[str, int]) -> Dict[str, Any]:
    return {"schema": SCHEMA, "revision": 0, "limits": dict(limits), "slots": []}


def _check_limits(limits: Any) -> Dict[str, int]:
    if not isinstance(limits, dict) or not all(_is_int(limits.get(k)) and limits[k] >= 0 for k in PROVIDERS + ("total",)):
        raise LedgerError(E_BAD_ARGUMENT, "limits need claude, codex and total")
    return {k: limits[k] for k in PROVIDERS + ("total",)}


def reserve_slot(slots_file: Any, key: str, provider: str, limits: Dict[str, int], deadline_at: int, *,
                 now: Optional[int] = None, force: bool = False) -> bool:
    """Add slot ``key`` if per-provider and total limits allow (``force``: account a live job over the limits).
    Idempotent on ``key``."""
    run, _task_id, _generation = validate_slot_key(key)
    if provider not in PROVIDERS:
        raise LedgerError(E_BAD_ARGUMENT, "provider")
    bounded = _check_limits(limits)
    _check_pos(deadline_at)
    outcome = {"ok": False}

    def fn(doc: Dict[str, Any]) -> Any:
        slots = doc["slots"]
        if any(slot["key"] == key for slot in slots):
            outcome["ok"] = True
            return UNCHANGED
        used = sum(1 for slot in slots if slot["provider"] == provider)
        if not force and (used >= bounded[provider] or len(slots) >= bounded["total"]):
            return UNCHANGED
        slots.append({"key": key, "run": run, "provider": provider,
                      "reserved_at": _now() if now is None else now, "deadline_at": deadline_at})
        doc["limits"] = bounded
        outcome["ok"] = True
        return doc

    locked_update(slots_file, fn, create=lambda: _new_slots_doc(bounded))
    return outcome["ok"]


def release_slot(slots_file: Any, key: str) -> bool:
    """Remove slot ``key`` (idempotent).  Returns whether a slot was removed."""
    validate_slot_key(key)
    outcome = {"removed": False}

    def fn(doc: Dict[str, Any]) -> Any:
        kept = [slot for slot in doc["slots"] if slot["key"] != key]
        if len(kept) == len(doc["slots"]):
            return UNCHANGED
        doc["slots"] = kept
        outcome["removed"] = True
        return doc

    try:
        locked_update(slots_file, fn)
    except LedgerError as exc:
        if exc.code != E_MISSING:
            raise
    return outcome["removed"]


def reap_slots(slots_file: Any, now: int, run_state_fn: Callable[[str], Optional[str]]) -> List[str]:
    """Drop slots that can no longer be used (contract 14.3, L3).

    A slot goes when its run ledger is missing or ``done``, when ``now > deadline_at + 3600``, or when its
    run is ``parked`` and ``now > deadline_at`` (a parked run holds no slot past its deadline).
    ``run_state_fn(run)`` returns the run's state, or ``None`` when its ledger is missing (see
    ``make_run_state_fn``).  It is called under the slots lock, so it must not take that lock.
    """
    reaped: List[str] = []

    def fn(doc: Dict[str, Any]) -> Any:
        kept = []
        for slot in doc["slots"]:
            state = run_state_fn(slot["run"])
            past = now > slot["deadline_at"]
            if state in (None, RUN_DONE) or now > slot["deadline_at"] + SLOT_REAP_GRACE_S or (state == RUN_PARKED and past):
                reaped.append(slot["key"])
            else:
                kept.append(slot)
        if not reaped:
            return UNCHANGED
        doc["slots"] = kept
        return doc

    try:
        locked_update(slots_file, fn)
    except LedgerError as exc:
        if exc.code != E_MISSING:
            raise
    return reaped


def make_run_state_fn(main_worktree: Any) -> Callable[[str], Optional[str]]:
    """``run_state_fn`` for ``reap_slots`` from the on-disk run ledgers (lock-free reads).

    A missing ledger is ``None``; an unreadable one counts as ``active`` (never reaped on a guess)."""

    def state(run: str) -> Optional[str]:
        try:
            return str(read(run_ledger_path(main_worktree, run))["state"])
        except LedgerError as exc:
            return None if exc.code in (E_MISSING, E_BAD_ID) else RUN_ACTIVE

    return state


def slot_keys(slots_file: Any) -> List[str]:
    try:
        return [slot["key"] for slot in read(slots_file)["slots"]]
    except LedgerError as exc:
        if exc.code == E_MISSING:
            return []
        raise


def replay_slot_journal(run_file: Any, slots_file: Any, limits: Dict[str, int], *, now: Optional[int] = None,
                        default_deadline_s: int = DEFAULT_SLOT_DEADLINE_S) -> List[Tuple[int, str, str]]:
    """Replay unfinished ``reserve``/``release`` entries (contract 6, 14.3).

    The slots ledger and the run ledger are touched one after the other, never
    nested.  Returns ``(seq, op, action)`` triples.  A reserve whose generation is
    stale is dropped (its abandon journaled the release); a reserve that no longer
    fits is dropped and the task stays ``admitted``.
    """
    done: List[Tuple[int, str, str]] = []
    doc = read(run_file)
    present = set(slot_keys(slots_file))
    for entry in pending_journal(doc):
        if entry["op"] not in (OP_RESERVE, OP_RELEASE):
            continue
        seq, data = entry["seq"], entry.get("data") or {}
        key = data.get("key")
        validate_slot_key(key)  # malformed entries raise LedgerError, never KeyError
        if entry["op"] == OP_RELEASE:
            action = replay_release(entry, key in present)
            if action == "remove-slot":
                release_slot(slots_file, key)
            _finish_entry(run_file, seq, None)
        elif journal_is_stale(doc, entry):
            action = "drop-stale"
            _finish_entry(run_file, seq, None)
        else:
            action = replay_reserve(entry, key in present)
            deadline = data.get("deadline_at") or (_now() if now is None else now) + default_deadline_s
            live = _tasks(doc)[entry["task"]]["state"] in (LAUNCHED, RUNNING)  # a recovered job that already runs
            if action == "add-slot" and not reserve_slot(slots_file, key, data.get("provider"), limits, deadline, force=live):
                action = "no-capacity"
                _finish_entry(run_file, seq, None)
            else:
                _finish_entry(run_file, seq, (entry["task"], entry["generation"]))
                action = "reserved" if action == "add-slot" else "reserved-existing"
        done.append((seq, entry["op"], action))
    return done


def _finish_entry(run_file: Any, seq: int, reserve_for: Optional[Tuple[str, int]]) -> None:
    def fn(doc: Dict[str, Any]) -> Any:
        if reserve_for is not None:
            name, generation = reserve_for
            entry = _tasks(doc).get(name)
            if entry is not None and entry["state"] == ADMITTED and entry["generation"] == generation:
                transition(doc, name, EV_CAPACITY, generation=generation, journal_seq=seq)
                return doc
        return journal_done(doc, seq)

    locked_update(run_file, fn)


# --------------------------------------------------------------------------
# Manifest v1 validator (contract 2.5)
# --------------------------------------------------------------------------

_MANIFEST_KEYS = frozenset(
    ["schema", "run", "task", "generation", "nonce", "base_sha", "status", "summary", "decisions_needed",
     "blockers", "env_report", "tests_run"]
)
_ENV_KEYS = frozenset(["tools", "present", "missing"])
_TEST_KEYS = frozenset(["cmd", "rc"])
_MANIFEST_STATUSES = ("complete", "blocked")
_RC_LIMIT = 65535


def _no_dupes(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError("MANIFEST_DUPLICATE_KEY", key)
        result[key] = value
    return result


def _manifest_str(value: Any, label: str, limit: int = MANIFEST_STRING_MAX, exc: Any = ManifestError,
                  code: str = "MANIFEST_TYPE") -> str:
    if not isinstance(value, str):
        raise exc(code, label + " must be a string")
    if len(value) > limit:
        raise exc("MANIFEST_TOO_LONG" if exc is ManifestError else code, label)
    if "\x00" in value:
        raise exc(code, label + " contains NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise exc(code, label + " is not valid text")
    return value


def _manifest_str_list(value: Any, label: str, exc: Any = ManifestError, code: str = "MANIFEST_TYPE") -> List[str]:
    if not isinstance(value, list):
        raise exc(code, label + " must be a list")
    if len(value) > MANIFEST_LIST_MAX:
        raise exc("MANIFEST_TOO_MANY" if exc is ManifestError else code, label)
    return [_manifest_str(item, label, exc=exc, code=code) for item in value]


def _name_list(value: Any, label: str) -> List[str]:
    items = _manifest_str_list(value, label)
    if not all(_SECRET_NAME_RE.fullmatch(item) for item in items):
        raise ManifestError("MANIFEST_ENV_NAME", label)
    return items


def _validate_env_report(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _ENV_KEYS:
        raise ManifestError("MANIFEST_ENV_REPORT", "env_report keys")
    tools = value["tools"]
    if not isinstance(tools, dict):
        raise ManifestError("MANIFEST_ENV_REPORT", "tools must be an object")
    if len(tools) > MANIFEST_LIST_MAX:
        raise ManifestError("MANIFEST_TOO_MANY", "tools")
    for name, version in tools.items():
        if not isinstance(name, str) or _TOOL_NAME_RE.fullmatch(name) is None:
            raise ManifestError("MANIFEST_ENV_NAME", "tool name")
        if not isinstance(version, str) or _VERSION_RE.fullmatch(version) is None:
            raise ManifestError("MANIFEST_ENV_VERSION", "tool version")
    return {"tools": dict(tools), "present": _name_list(value["present"], "present"),
            "missing": _name_list(value["missing"], "missing")}


def _validate_tests_run(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        raise ManifestError("MANIFEST_TYPE", "tests_run must be a list")
    if len(value) > MANIFEST_LIST_MAX:
        raise ManifestError("MANIFEST_TOO_MANY", "tests_run")
    checked = []
    for item in value:
        if not isinstance(item, dict) or set(item) != _TEST_KEYS:
            raise ManifestError("MANIFEST_TESTS_RUN", "tests_run item keys")
        if not _is_int(item["rc"]) or abs(item["rc"]) > _RC_LIMIT:
            raise ManifestError("MANIFEST_TESTS_RUN", "rc")
        checked.append({"cmd": _manifest_str(item["cmd"], "cmd"), "rc": item["rc"]})
    return checked


def _decode_manifest(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, (bytes, bytearray)):
        raise ManifestError("MANIFEST_TYPE", "raw manifest must be bytes")
    if len(raw) > MANIFEST_MAX_BYTES:
        raise ManifestError("MANIFEST_TOO_LARGE", "%d bytes" % len(raw))
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        raise ManifestError("MANIFEST_NOT_UTF8")
    try:
        doc = json.loads(text, object_pairs_hook=_no_dupes, parse_constant=_reject_constant)
    except ManifestError:
        raise
    except (ValueError, RecursionError):
        raise ManifestError("MANIFEST_BAD_JSON")
    if not isinstance(doc, dict):
        raise ManifestError("MANIFEST_NOT_OBJECT")
    return doc


def validate_manifest(raw_bytes: bytes, *, run: str, task: str, generation: int, nonce: str, base_sha: str,
                      require_nonce: bool = True) -> Dict[str, Any]:
    """Validate manifest v1 bytes against the ledger's expectations; raises ``ManifestError``.

    Does no secret scanning (the collector scans the raw bytes first).  With
    ``require_nonce=False`` the ``nonce`` key must still be a 32-hex string but is
    not compared.
    """
    doc = _decode_manifest(raw_bytes)
    if set(doc) != _MANIFEST_KEYS:
        raise ManifestError("MANIFEST_KEYS", "missing=%s extra=%s" % (
            sorted(_MANIFEST_KEYS - set(doc)), sorted(set(doc) - _MANIFEST_KEYS)))
    if not _is_int(doc["schema"]) or doc["schema"] != SCHEMA:
        raise ManifestError("MANIFEST_SCHEMA")
    if not _is_int(doc["generation"]) or doc["generation"] < 1:
        raise ManifestError("MANIFEST_TYPE", "generation")
    for key in ("run", "task", "nonce", "base_sha", "status"):
        _manifest_str(doc[key], key)
    if _NONCE_RE.fullmatch(doc["nonce"]) is None:
        raise ManifestError("MANIFEST_NONCE", "format")
    if _OID_RE.fullmatch(doc["base_sha"]) is None:
        raise ManifestError("MANIFEST_TYPE", "base_sha format")
    for key, expected in (("run", run), ("task", task), ("generation", generation), ("base_sha", base_sha)):
        if doc[key] != expected or not isinstance(doc[key], type(expected)):
            raise ManifestError("MANIFEST_MISMATCH", key)
    if require_nonce and not hmac.compare_digest(doc["nonce"].encode("ascii"), str(nonce).encode("utf-8")):
        raise ManifestError("MANIFEST_NONCE", "does not match the ledger")
    if doc["status"] not in _MANIFEST_STATUSES:
        raise ManifestError("MANIFEST_STATUS")
    _manifest_str(doc["summary"], "summary", MANIFEST_SUMMARY_MAX)
    _manifest_str_list(doc["decisions_needed"], "decisions_needed")
    _manifest_str_list(doc["blockers"], "blockers")
    doc["env_report"] = _validate_env_report(doc["env_report"])
    doc["tests_run"] = _validate_tests_run(doc["tests_run"])
    return doc
