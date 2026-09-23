#!/usr/bin/env python3
"""Bounded, candidate-bound evidence verification for parallel host gates."""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree

try:
    import fcntl
except ImportError:  # Windows cannot establish this descriptor-path contract.
    fcntl = None  # type: ignore[assignment]

SCHEMA = "ffs.parallel-host-verification/v1"
AUTHORITY_SCHEMA = "ffs.verification-repair-authority/v1"
MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_EXECUTABLE_BYTES = 64 * 1024 * 1024
MAX_EXECUTABLE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_INVENTORY_FILE_BYTES = 64 * 1024 * 1024
MAX_INVENTORY_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ARTIFACTS = 128
MAX_CANONICAL_ARTIFACTS = MAX_ARTIFACTS * 2 + 32
MAX_JSON_DEPTH = 32
MAX_ADAPTER_OUTPUT = 1024 * 1024
MAX_ADAPTER_TIMEOUT = 60
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
UTC_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|\+00:00)$")

COMMON = {"schema", "binding", "label", "candidate", "provenance", "ac_ids", "path_ids", "int_ids"}
FIELDS = {
    "baseline": COMMON | {"repository", "baseline", "full_inventory", "baseline_profile"},
    "installation": COMMON | {"installation"},
    "upgrade": COMMON | {"current", "ledger_artifact", "comparison_binding"},
    "review": COMMON | {"platform", "command", "started_utc", "completed_utc", "exit_status",
        "ac_ids", "path_ids", "int_ids", "reviewer", "producer", "artifact_sha256",
        "artifacts", "result", "severe_path_disposition", "review_adapter", "review_timeout_seconds"},
    "matrix": COMMON | {"matrix"},
    "hosts": COMMON | {"hosts"},
    "audit": COMMON | {"audit"},
    "coverage": COMMON | {"coverage"},
    "migration": COMMON | {"migration"},
    "rollout": COMMON | {"rollout"},
}
REQUIRED = {
    "baseline": COMMON | {"baseline"},
    "installation": COMMON | {"installation"},
    "upgrade": COMMON | {"current", "ledger_artifact"},
    "review": COMMON | {"platform", "command", "started_utc", "completed_utc", "exit_status",
        "ac_ids", "path_ids", "int_ids", "reviewer", "producer", "artifact_sha256",
        "artifacts", "result", "severe_path_disposition"},
    "matrix": COMMON | {"matrix"},
    "hosts": COMMON | {"hosts"},
    "audit": COMMON | {"audit"},
    "coverage": COMMON | {"coverage"},
    "migration": COMMON | {"migration"},
    "rollout": COMMON | {"rollout"},
}
PROVENANCE = {"source_sha256", "binary_sha256", "bundle_sha256", "config_sha256"}
ASSIGNMENT = {"finding_id", "action", "owner", "path_ids", "owning_phase", "regression_contract", "authority_id"}
PLATFORMS = frozenset({"darwin", "linux"})
PAIRINGS = frozenset({"claude-claude", "claude-codex", "codex-codex"})
REVIEW_DIRECTIONS = frozenset({"claude-codex", "codex-claude"})
REQUIRED_AGGREGATE_GATES = frozenset({
    "audit", "coverage", "hosts", "installation-lifecycle", "matrix",
    "migration", "rollout", "upgrade-comparison",
})
RESULT_COMMON_FIELDS = frozenset({
    "schema", "gate", "purpose", "binding", "ac_ids", "path_ids", "int_ids", "status",
    "platform", "host", "model", "label", "authenticated", "command", "started_utc",
    "completed_utc", "exit_status", "provenance", "artifacts", "errors", "unmet_reasons",
    "gate_vector", "review_complete", "repair_authorized", "path_admitted", "rollout_ready",
    "verification_proof",
})
AGGREGATE_RESULT_FIELDS = {
    "audit": {"domains", "finding_count", "reviewer"},
    "coverage": {"line_min", "coverage"},
    "hosts": {"pairings", "review_directions", "soak_seconds", "row_count"},
    "installation-lifecycle": {"row_count", "platforms"},
    "matrix": {"repetitions", "platforms", "case_count", "row_count"},
    "migration": {"source_count", "record_counts", "run_count"},
    "rollout": {"consumer", "surface_count", "fork_count", "canary_count"},
    "upgrade-comparison": {
        "new_failures", "missing_tests", "remaining_failures", "suite_passed",
        "comparison_passed", "baseline_sha256", "ledger_sha256", "ledger_entries",
        "suite_ids", "missing_suites", "new_suites", "suite_observations",
        "provenance_drift", "before_provenance", "after_provenance",
        "comparison_binding_sha256",
    },
}
AGGREGATE_RESULT_PURPOSE = {
    "audit": "verification", "coverage": "verification", "hosts": "authenticated-verification",
    "installation-lifecycle": "verification", "matrix": "verification",
    "migration": "verify-legacy", "rollout": "verification", "upgrade-comparison": "comparison",
}


class E(Exception):
    def __init__(self, code: str, problem: str, status: str = "FAIL") -> None:
        super().__init__(problem)
        self.code, self.problem, self.status = code, problem, status


@dataclass(frozen=True)
class Checked:
    locator: str
    data: bytes
    device: int
    inode: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


class Budget:
    def __init__(self, *, max_artifacts: int = MAX_ARTIFACTS,
                 max_evidence_bytes: int = MAX_TOTAL_BYTES) -> None:
        self.total = 0
        self.evidence_total = 0
        self.executable_total = 0
        self.identities: dict[tuple[int, int], str] = {}
        self.classes: dict[tuple[int, int], set[str]] = {}
        self.by_locator: dict[str, Checked] = {}
        self.max_artifacts = max_artifacts
        self.max_evidence_bytes = max_evidence_bytes

    def add(self, item: Checked, artifact_class: str = "evidence") -> None:
        if artifact_class not in {"evidence", "executable"}:
            raise E("EVIDENCE_LIMIT", "unknown artifact budget class")
        identity = (item.device, item.inode)
        if identity in self.identities and self.identities[identity] != item.locator:
            raise E("ARTIFACT_ALIAS", "two locators name the same physical artifact")
        if identity not in self.identities:
            self.identities[identity] = item.locator
            self.total += len(item.data)
        self.by_locator[item.locator] = item
        classes = self.classes.setdefault(identity, set())
        if artifact_class not in classes:
            classes.add(artifact_class)
            if artifact_class == "executable":
                self.executable_total += len(item.data)
            else:
                self.evidence_total += len(item.data)
        if (len(self.identities) > self.max_artifacts or self.evidence_total > self.max_evidence_bytes
                or self.executable_total > MAX_EXECUTABLE_TOTAL_BYTES
                or self.total > self.max_evidence_bytes + MAX_EXECUTABLE_TOTAL_BYTES):
            raise E("EVIDENCE_LIMIT", "artifact count or cumulative bytes exceed the verifier limit")


def manifest_fields(gate: str) -> dict[str, list[str]]:
    return {"allowed": sorted(FIELDS[gate]), "required": sorted(REQUIRED[gate])}


def h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _local_host() -> str:
    return os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", "unknown")


def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise E("DUPLICATE_KEY", f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _depth(value: Any, level: int = 0) -> None:
    if level > MAX_JSON_DEPTH:
        raise E("JSON_DEPTH", "JSON nesting limit exceeded")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise E("INVALID_JSON", "JSON keys must be strings")
            _depth(child, level + 1)
    elif isinstance(value, list):
        for child in value:
            _depth(child, level + 1)


def obj(data: bytes) -> Any:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs)
    except E:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E("INVALID_JSON", "invalid UTF-8 JSON") from exc
    _depth(value)
    return value


def _parts(path: Path) -> tuple[str, ...]:
    if not path.is_absolute() or path.parts[0] != os.sep or any(x in {"", ".", ".."} for x in path.parts[1:]):
        raise E("UNSAFE_PATH", "a normalized absolute path is required")
    return path.parts[1:]


def _parent_fd(path: Path, create_immediate: bool = False) -> tuple[int, str]:
    parts = _parts(path)
    if not parts:
        raise E("UNSAFE_PATH", "a file path is required")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(os.sep, flags)
    try:
        for index, component in enumerate(parts[:-1]):
            try:
                child = os.open(component, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create_immediate or index != len(parts) - 2:
                    raise E("UNSAFE_PATH", "output parent does not exist")
                os.mkdir(component, 0o700, dir_fd=fd)
                child = os.open(component, flags, dir_fd=fd)
            if not stat.S_ISDIR(os.fstat(child).st_mode):
                os.close(child)
                raise E("UNSAFE_PATH", "path ancestor is not a directory")
            os.close(fd)
            fd = child
        return fd, parts[-1]
    except Exception:
        os.close(fd)
        raise


def read_checked(path: str | Path, budget: Budget | None = None, *, max_bytes: int = MAX_INPUT_BYTES,
                 allow_empty: bool = False, artifact_class: str = "evidence") -> Checked:
    locator, parent, fd = str(path), -1, -1
    try:
        parent, name = _parent_fd(Path(locator))
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=parent)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise E("UNSAFE_INPUT", "a single-link regular input is required")
        if before.st_size > max_bytes or (not allow_empty and before.st_size == 0):
            raise E("UNSAFE_INPUT", "input is empty or exceeds the byte limit")
        chunks, remaining = [], before.st_size
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                raise E("INPUT_CHANGED", "input shortened while it was read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise E("INPUT_CHANGED", "input grew while it was read")
        after = os.fstat(fd)
        left = (before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns)
        right = (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns)
        if left != right:
            raise E("INPUT_CHANGED", "input changed while it was read")
        item = Checked(locator, b"".join(chunks), before.st_dev, before.st_ino)
        if budget:
            budget.add(item, artifact_class)
        return item
    except E:
        raise
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise E("UNSAFE_INPUT", "symlinked input is refused") from exc
        raise E("READ_FAILED", "unable to open checked input") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if parent >= 0:
            os.close(parent)


def read(path: str | Path) -> bytes:
    return read_checked(path).data


def _artifact(value: Any, budget: Budget, *, max_bytes: int = MAX_INPUT_BYTES,
              artifact_class: str = "evidence") -> Checked:
    if not isinstance(value, dict) or set(value) != {"locator", "sha256"}:
        raise E("ARTIFACT_FIELDS", "artifact locator and sha256 are required")
    if not isinstance(value["locator"], str) or not isinstance(value["sha256"], str) or not HASH_RE.fullmatch(value["sha256"]):
        raise E("ARTIFACT_FIELDS", "artifact locator and sha256 must be typed")
    item = budget.by_locator.get(value["locator"])
    if item is None:
        item = read_checked(value["locator"], budget, max_bytes=max_bytes, artifact_class=artifact_class)
    else:
        if not item.data or len(item.data) > max_bytes:
            raise E("UNSAFE_INPUT", "input is empty or exceeds the byte limit")
        budget.add(item, artifact_class)
    if item.sha256 != value["sha256"]:
        raise E("DIGEST_MISMATCH", "artifact digest mismatch")
    return item


def art(value: Any) -> tuple[str, bytes]:
    item = _artifact(value, Budget())
    return item.locator, item.data


def _closed(value: Any, required: set[str], allowed: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not required <= set(value) or set(value) - allowed:
        raise E(code, f"closed {code.lower()} fields are invalid")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise E("MANIFEST_SCHEMA", f"{name} must be a nonempty string")
    return value


def _strings(value: Any, name: str, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value) or any(not isinstance(x, str) or not x for x in value):
        raise E("MANIFEST_SCHEMA", f"{name} must be a string array")
    if len(value) != len(set(value)):
        raise E("MANIFEST_SCHEMA", f"{name} must contain unique values")
    return value


def _identity(value: Any, name: str) -> dict[str, str]:
    value = _closed(value, {"host", "model", "session"}, {"host", "model", "session"}, "IDENTITY")
    for key in ("host", "model", "session"):
        if not isinstance(value[key], str) or not value[key] or "\0" in value[key]:
            raise E("IDENTITY", f"{name} identity requires nonempty host, model and session")
    return value  # type: ignore[return-value]


def ident(value: Any) -> bool:
    try:
        _identity(value, "identity")
        return True
    except E:
        return False


def same(a: Any, b: Any) -> bool:
    return ident(a) and ident(b) and a == b


def _timestamps(started: Any, completed: Any, fresh: bool) -> None:
    def parse(value: Any, name: str) -> datetime:
        if not isinstance(value, str) or not UTC_RE.fullmatch(value):
            raise E("TIMESTAMP", f"{name} must be UTC")
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError as exc:
            raise E("TIMESTAMP", f"{name} is invalid") from exc
    start, finish, current = parse(started, "started_utc"), parse(completed, "completed_utc"), datetime.now(timezone.utc)
    if finish < start or finish > current + timedelta(minutes=5):
        raise E("TIMESTAMP", "timestamps are unordered or future-dated")
    if fresh and start < current - timedelta(days=7):
        raise E("STALE_EVIDENCE", "review evidence is stale", "UNMET")


def _common(manifest: Any, gate: str, fresh_review: bool = False) -> tuple[dict[str, Any], Checked, Budget]:
    manifest = _closed(manifest, COMMON if fresh_review else REQUIRED[gate], FIELDS[gate], "MANIFEST_SCHEMA")
    if manifest["schema"] != SCHEMA:
        raise E("MANIFEST_SCHEMA", "unsupported manifest schema")
    binding = _closed(manifest["binding"], {"run", "activity", "attempt"}, {"run", "activity", "attempt"}, "BINDING")
    for key in ("run", "activity", "attempt"):
        _string(binding[key], f"binding.{key}")
    for key in ("ac_ids", "path_ids", "int_ids"):
        _strings(manifest[key], key, fresh_review and gate == "review")
    if manifest["label"] not in {"hermetic", "authenticated", "degraded", "local"}:
        raise E("MANIFEST_SCHEMA", "unknown evidence label")
    budget = Budget()
    candidate = _artifact(manifest["candidate"], budget)
    provenance = _closed(manifest["provenance"], PROVENANCE, PROVENANCE, "PROVENANCE")
    if any(not isinstance(value, str) or not HASH_RE.fullmatch(value) for value in provenance.values()):
        raise E("PROVENANCE", "all provenance fields must be SHA-256 digests")
    if provenance["source_sha256"] != candidate.sha256:
        raise E("CANDIDATE", "source provenance does not bind candidate bytes")
    return manifest, candidate, budget


def check(manifest: Any, gate: str) -> bytes:
    return _common(manifest, gate)[1].data


def vec(**values: bool) -> dict[str, bool]:
    return {key: bool(values.get(key)) for key in ("review_complete", "repair_authorized", "path_admitted", "rollout_ready")}


def _safe_argv(values: Iterable[Any]) -> list[str]:
    result, secret_next = [], False
    for raw in values:
        value = str(raw)
        low = value.lower()
        if secret_next:
            result.append("<redacted>")
            secret_next = False
        elif any(x in low for x in ("token=", "secret=", "password=", "authorization=")):
            result.append(value.split("=", 1)[0] + "=<redacted>")
        else:
            result.append(value)
            secret_next = low in {"--token", "--secret", "--password", "--authorization"}
    return result


def envelope(gate: str, purpose: str, status: str, manifest: Any, vector: dict[str, bool], errors: list[dict[str, str]]) -> dict[str, Any]:
    source = manifest if isinstance(manifest, dict) else {}
    binding = source.get("binding")
    if not isinstance(binding, dict) or set(binding) != {"run", "activity", "attempt"} or any(
            not isinstance(value, str) or not value for value in binding.values()):
        binding = {"run": "unknown", "activity": "unknown", "attempt": "unknown"}
    instant = now()
    output: dict[str, Any] = {
        "schema": SCHEMA, "gate": gate, "purpose": purpose,
        "binding": binding,
        "ac_ids": source.get("ac_ids", []), "path_ids": source.get("path_ids", []), "int_ids": source.get("int_ids", []),
        "status": status, "platform": sys.platform, "host": "unknown", "model": "unknown",
        "label": source.get("label", "unknown"), "authenticated": False,
        "command": _safe_argv([sys.executable, *sys.argv]), "started_utc": instant, "completed_utc": instant,
        "exit_status": {"PASS": 0, "FAIL": 1, "UNMET": 2}[status], "provenance": source.get("provenance", {}),
        "artifacts": source.get("artifacts", []), "errors": errors, "unmet_reasons": [], "gate_vector": vector,
    }
    output.update(vector)
    return output


def fail(gate: str, purpose: str, error: E, manifest: Any) -> dict[str, Any]:
    detail = {"code": error.code, "problem": error.problem, "cause": error.code,
        "fix": "supply validated byte-bound evidence", "docs": "specs/014-parallel-host-parity/contracts/verification.md",
        "recovery_action": "correct the input and select a new output", "unmet_code": error.code if error.status == "UNMET" else ""}
    output = envelope(gate, purpose, error.status, manifest, vec(), [detail])
    output["unmet_reasons"] = [error.code] if error.status == "UNMET" else []
    return output


def _git_repository_ancestor(path: Path) -> bool:
    current = path.resolve(strict=False)
    if not current.exists():
        current = current.parent
    while current != current.parent:
        marker = current / ".git"
        if marker.exists() or marker.is_symlink():
            return True
        current = current.parent
    return False


def _protected(path: Path) -> bool:
    resolved = path.resolve(strict=False)
    roots = {Path(__file__).resolve().parents[2]}
    roots.update(Path(x).resolve() for x in os.environ.get("FFS_REGISTERED_WORKSPACES", "").split(os.pathsep) if x)
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            pass
    return _git_repository_ancestor(path)


def _descriptor_path(fd: int) -> Path:
    proc = Path(f"/proc/self/fd/{fd}")
    if proc.exists():
        return Path(os.readlink(proc)).resolve()
    try:
        if fcntl is None:
            raise OSError("F_GETPATH unavailable")
        raw = fcntl.fcntl(fd, 50, b"\0" * 1024)  # F_GETPATH on macOS.
        return Path(raw.split(b"\0", 1)[0].decode()).resolve()
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise E("PATH_IDENTITY", "physical descriptor path is unavailable", "UNMET") from exc


def _read_authority_file(selected: str, budget: Budget, code: str = "AUTHORITY") -> Any:
    path = Path(selected)
    if not path.is_absolute():
        raise E(code, "authority locator must be absolute", "UNMET")
    if _protected(path):
        raise E(code, "authority cannot reside inside a repository or workspace", "UNMET")
    item = read_checked(path, budget)
    parent_fd = -1
    try:
        parent_fd, name = _parent_fd(path)
        parent, info = os.fstat(parent_fd), os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _protected(_descriptor_path(parent_fd)) or (info.st_dev, info.st_ino) != (item.device, item.inode):
            raise E(code, "authority path identity changed or enters a protected workspace", "UNMET")
        if info.st_uid != os.geteuid() or parent.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or stat.S_IMODE(parent.st_mode) != 0o700:
            raise E(code, "authority requires owner identity, 0600 file and 0700 parent", "UNMET")
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
    return obj(item.data)


def _authority(manifest: dict[str, Any], candidate: Checked, budget: Budget) -> dict[str, Any] | None:
    selected = os.environ.get("FFS_VERIFICATION_AUTHORITY")
    if not selected:
        return None
    value = _closed(_read_authority_file(selected, budget), {"schema", "run", "candidate_sha256", "assignments"},
                    {"schema", "run", "candidate_sha256", "producer", "assignments", "adapters", "installations"}, "AUTHORITY")
    if value["schema"] != AUTHORITY_SCHEMA or value["run"] != manifest["binding"]["run"] or value["candidate_sha256"] != candidate.sha256:
        raise E("AUTHORITY", "authority binding mismatch", "UNMET")
    if not isinstance(value["assignments"], list):
        raise E("AUTHORITY", "authority assignments must be an array", "UNMET")
    return value


def authority(manifest: dict[str, Any], candidate_bytes: bytes) -> dict[str, Any] | None:
    item = Checked(manifest["candidate"]["locator"], candidate_bytes, -1, -1)
    return _authority(manifest, item, Budget())


def _finding(value: Any) -> dict[str, Any]:
    finding = _closed(value, {"id", "severity", "status"},
        {"id", "severity", "status", "summary", "affected_paths", "evidence", "adjudication"}, "FINDING")
    _string(finding["id"], "finding.id")
    if not isinstance(finding["severity"], str) or finding["severity"].lower() not in {"low", "medium", "high", "critical"}:
        raise E("FINDING", "unknown finding severity")
    finding["severity"] = finding["severity"].lower()
    if finding["status"] not in {"open", "resolved", "refuted"}:
        raise E("FINDING", "unknown finding status")
    if "summary" in finding:
        _string(finding["summary"], "finding.summary")
    if "affected_paths" in finding:
        _strings(finding["affected_paths"], "finding.affected_paths", True)
    if "evidence" in finding:
        _strings(finding["evidence"], "finding.evidence", True)
    return finding


def _review(value: Any, manifest: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    value = _closed(value, {"verdict", "findings", "reviewed_sha256"},
                    {"verdict", "findings", "reviewed_sha256", "host", "model", "session"}, "REVIEW")
    if value["verdict"] not in {"PASS", "FAIL"} or not isinstance(value["reviewed_sha256"], str) or not HASH_RE.fullmatch(value["reviewed_sha256"]):
        raise E("REVIEW", "review verdict or digest is malformed")
    if not isinstance(value["findings"], list):
        raise E("REVIEW", "findings must be an array")
    findings = [_finding(x) for x in value["findings"]]
    ids = [x["id"] for x in findings]
    if len(ids) != len(set(ids)):
        raise E("FINDING", "finding IDs must be unique")
    if value["reviewed_sha256"] != manifest["candidate"]["sha256"] or manifest.get("artifact_sha256", value["reviewed_sha256"]) != value["reviewed_sha256"]:
        raise E("CANDIDATE", "review is not bound to candidate bytes")
    return value, findings


def review(value: Any, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    findings = _review(value, manifest)[1]
    return [x for x in findings if x["status"] == "open" and x["severity"] in {"high", "critical"}]


def _assignment(value: Any) -> dict[str, Any]:
    value = _closed(value, ASSIGNMENT, ASSIGNMENT, "ASSIGNMENT")
    for key in ("finding_id", "owner", "owning_phase", "regression_contract", "authority_id"):
        _string(value[key], f"assignment.{key}")
    if value["action"] != "assign-repair":
        raise E("ASSIGNMENT", "severe finding action must be assign-repair")
    _strings(value["path_ids"], "assignment.path_ids", True)
    return value


def _disposition(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and value.get("action") == "assign-repair":
        return _assignment(value)
    value = _closed(value, {"finding_id", "action", "owner", "rationale", "evidence"},
                    {"finding_id", "action", "owner", "rationale", "evidence", "authority_id"}, "DISPOSITION")
    for key in ("finding_id", "owner", "rationale"):
        _string(value[key], f"disposition.{key}")
    if value["action"] not in {"accept-risk", "track", "resolve", "refute"}:
        raise E("DISPOSITION", "unknown disposition action")
    _strings(value["evidence"], "disposition.evidence", True)
    return value


def _adjudicate(manifest: dict[str, Any], findings: list[dict[str, Any]], control: dict[str, Any] | None,
                known: set[str]) -> tuple[bool, list[str]]:
    raw = manifest.get("severe_path_disposition", [])
    if not isinstance(raw, list):
        raise E("DISPOSITION", "dispositions must be an array")
    dispositions = [_disposition(x) for x in raw]
    ids = [x["finding_id"] for x in dispositions]
    if len(ids) != len(set(ids)):
        raise E("DISPOSITION", "dispositions must map one-to-one to findings")
    by_id = {x["finding_id"]: x for x in dispositions}
    finding_ids = {x["id"] for x in findings}
    if set(by_id) - finding_ids:
        raise E("DISPOSITION", "disposition names an unknown finding")
    authorized: dict[str, dict[str, Any]] = {}
    if control:
        parsed = [_assignment(x) for x in control["assignments"]]
        auth_ids = [x["finding_id"] for x in parsed]
        if len(auth_ids) != len(set(auth_ids)):
            raise E("ASSIGNMENT", "authority assignments must be unique")
        authorized = {x["finding_id"]: x for x in parsed}
    open_severe = [x for x in findings if x["status"] == "open" and x["severity"] in {"high", "critical"}]
    blocked: set[str] = set()
    for finding in open_severe:
        disposition, grant = by_id.get(finding["id"]), authorized.get(finding["id"])
        if disposition is None or grant is None or disposition != grant:
            raise E("REPAIR_AUTHORITY", "every open severe finding requires an exact supervisor assignment")
        affected = set(finding.get("affected_paths", disposition["path_ids"]))
        if affected != set(disposition["path_ids"]) or not affected:
            raise E("REPAIR_AUTHORITY", "assignment must cover the exact affected paths")
        blocked.update(affected)
    for finding in findings:
        if finding in open_severe:
            continue
        disposition = by_id.get(finding["id"])
        if finding["status"] == "open":
            if not disposition or disposition.get("action") == "assign-repair":
                raise E("DISPOSITION", "open lower-severity finding requires owner and disposition")
            if not set(disposition["evidence"]) <= known:
                raise E("DISPOSITION", "disposition evidence is not verified")
        else:
            adjudication = finding.get("adjudication")
            evidence = adjudication.get("evidence") if isinstance(adjudication, dict) else (
                disposition.get("evidence") if disposition and disposition.get("action") in {"resolve", "refute"} else None)
            owner = adjudication.get("owner") if isinstance(adjudication, dict) else (disposition.get("owner") if disposition else None)
            if not isinstance(owner, str) or not owner or not isinstance(evidence, list) or not evidence or not set(evidence) <= known:
                raise E("ADJUDICATION", "resolved/refuted finding requires verified adjudication evidence")
    return bool(open_severe), sorted(blocked)


def assigned(manifest: dict[str, Any], control: dict[str, Any] | None, severe: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    try:
        return _adjudicate(manifest, severe, control, set())
    except E:
        return False, []


def _executable(argv0: str) -> Path:
    found = argv0 if os.path.isabs(argv0) else shutil.which(argv0)
    if not found or not Path(found).is_file():
        raise E("ADAPTER_UNAVAILABLE", "review adapter executable is unavailable", "UNMET")
    return Path(found).resolve()


def _pin(control: dict[str, Any] | None, manifest: dict[str, Any], argv: list[str], reviewer: dict[str, str] | None,
         budget: Budget) -> tuple[dict[str, Any], list[Checked]]:
    if not control:
        raise E("ADAPTER_PIN", "trusted adapter supervisor is required", "UNMET")
    producer = _identity(manifest.get("producer"), "producer")
    if "producer" not in control or _identity(control["producer"], "authority.producer") != producer:
        raise E("PRODUCER_PIN", "trusted producer identity mismatch", "UNMET")
    adapters = control.get("adapters")
    matches = [x for x in adapters if isinstance(x, dict) and x.get("argv") == argv] if isinstance(adapters, list) else []
    if len(matches) != 1:
        raise E("ADAPTER_PIN", "exactly one trusted adapter pin is required", "UNMET")
    pin = _closed(matches[0], {"argv", "label", "host", "model", "executable", "artifacts"},
                  {"argv", "label", "host", "model", "executable", "artifacts"}, "ADAPTER_PIN")
    if pin["label"] != manifest["label"] or pin["label"] not in {"hermetic", "authenticated", "degraded", "local"}:
        raise E("ADAPTER_PIN", "trusted adapter label mismatch", "UNMET")
    if reviewer and (pin["host"], pin["model"]) != (reviewer["host"], reviewer["model"]):
        raise E("ADAPTER_PIN", "trusted reviewer identity mismatch", "UNMET")
    executable = _artifact(pin["executable"], budget, max_bytes=MAX_EXECUTABLE_BYTES,
                           artifact_class="executable")
    if Path(executable.locator).resolve() != _executable(argv[0]):
        raise E("ADAPTER_PIN", "pinned executable is not invoked", "UNMET")
    if executable.data.startswith(b"#!"):
        raise E("ADAPTER_PIN", "invoke a pinned native interpreter with the script as a pinned argument", "UNMET")
    if any(os.path.isabs(item) and Path(item).is_dir() for item in argv[1:]):
        raise E("ADAPTER_PIN", "adapter directory arguments have no immutable execution pin", "UNMET")
    if not isinstance(pin["artifacts"], list):
        raise E("ADAPTER_PIN", "adapter artifact pins must be an array", "UNMET")
    checked = [executable] + [_artifact(x, budget) for x in pin["artifacts"]]
    pinned_paths = {str(Path(x.locator).resolve()) for x in checked[1:]}
    candidate = str(Path(manifest["candidate"]["locator"]).resolve())
    invoked = {str(Path(x).resolve()) for x in argv[1:] if os.path.isabs(x) and Path(x).is_file() and str(Path(x).resolve()) != candidate}
    if not invoked <= pinned_paths:
        raise E("ADAPTER_PIN", "each invoked script must be pinned", "UNMET")
    if pin["label"] == "authenticated":
        artifact_hashes = {item.sha256 for item in checked[1:]}
        provenance = manifest["provenance"]
        if provenance["binary_sha256"] != executable.sha256 or provenance["bundle_sha256"] not in artifact_hashes or provenance["config_sha256"] not in artifact_hashes:
            raise E("PROVENANCE", "authenticated adapter provenance does not bind executable, bundle and config bytes", "UNMET")
    return pin, checked


def pin(control: dict[str, Any] | None, argv: list[str], got: dict[str, str] | None, producer: dict[str, str]) -> list[tuple[Any, ...]]:
    # Compatibility wrapper; real admission uses _pin with the complete manifest.
    if not control or control.get("producer") != producer:
        raise E("PRODUCER_PIN", "trusted producer identity mismatch", "UNMET")
    return [(x.get("executable"), *x.get("artifacts", [])) for x in control.get("adapters", []) if x.get("argv") == argv]


def _stage(argv: list[str], checked: list[Checked], directory: Path) -> list[str]:
    staged: dict[str, str] = {}
    for index, item in enumerate(checked):
        target = directory / f"adapter-{index}{Path(item.locator).suffix}"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700 if index == 0 else 0o600)
        try:
            _write_all(fd, item.data)
            os.fsync(fd)
        finally:
            os.close(fd)
        staged[str(Path(item.locator).resolve())] = str(target)
    result = list(argv)
    result[0] = staged[str(_executable(argv[0]))]
    for index, value in enumerate(result[1:], 1):
        if os.path.isabs(value) and str(Path(value).resolve()) in staged:
            result[index] = staged[str(Path(value).resolve())]
    return result


def _stop(process: subprocess.Popen[bytes]) -> None:
    group = process.pid

    def group_exists() -> bool:
        try:
            os.killpg(group, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def signal_group(sig: int) -> None:
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            if process.poll() is None:
                try:
                    process.send_signal(sig)
                except ProcessLookupError:
                    pass

    signal_group(signal.SIGTERM)
    grace = time.monotonic() + 1
    while group_exists() and time.monotonic() < grace:
        if process.poll() is None:
            try:
                process.wait(timeout=min(0.05, max(0.001, grace - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
        else:
            time.sleep(0.01)
    if group_exists():
        signal_group(signal.SIGKILL)
    if process.poll() is None:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def _run(argv: list[str], cwd: Path, timeout: int, trusted_env: dict[str, str] | None = None) -> tuple[int, bytes, bytes]:
    env = (dict(trusted_env) if trusted_env is not None else
           {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT") if key in os.environ})
    if trusted_env is None:
        env.update({"HOME": str(cwd), "TMPDIR": str(cwd), "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        raise E("ADAPTER_UNAVAILABLE", "review adapter could not launch", "UNMET") from exc
    assert process.stdout and process.stderr
    selector, buffers = selectors.DefaultSelector(), {"stdout": bytearray(), "stderr": bytearray()}
    for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    deadline = time.monotonic() + timeout

    def group_exists() -> bool:
        try:
            os.killpg(process.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def drain(key: selectors.SelectorKey) -> None:
        try:
            chunk = os.read(key.fileobj.fileno(), 65536)
        except (BlockingIOError, InterruptedError):
            return
        if not chunk:
            selector.unregister(key.fileobj)
            return
        buffers[key.data].extend(chunk)
        if len(buffers["stdout"]) + len(buffers["stderr"]) > MAX_ADAPTER_OUTPUT:
            raise E("ADAPTER_OUTPUT_LIMIT", "review adapter output exceeded its byte limit", "UNMET")

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise E("ADAPTER_TIMEOUT", "review adapter exceeded its deadline", "UNMET")
            events = selector.select(min(remaining, 0.1)) if selector.get_map() else []
            for key, _ in events:
                drain(key)
            returncode = process.poll()
            if returncode is not None and not events:
                for key in list(selector.get_map().values()):
                    drain(key)
                if group_exists():
                    raise E("ADAPTER_LINGERING", "review adapter left a process running", "UNMET")
                if not selector.get_map():
                    return returncode, bytes(buffers["stdout"]), bytes(buffers["stderr"])
            if not selector.get_map() and returncode is None:
                time.sleep(min(remaining, 0.01))
    except E:
        _stop(process)
        raise
    finally:
        selector.close()
        _stop(process)


def _known(manifest: dict[str, Any], budget: Budget) -> set[str]:
    known: set[str] = set()
    candidate_locator = manifest.get("candidate", {}).get("locator") if isinstance(manifest.get("candidate"), dict) else None
    result_locator = manifest.get("result", {}).get("locator") if isinstance(manifest.get("result"), dict) else None
    artifacts = manifest.get("artifacts", [])
    if artifacts is not None and not isinstance(artifacts, list):
        raise E("ARTIFACT_FIELDS", "supporting artifacts must be an array")
    for descriptor in artifacts or []:
        item = _artifact(descriptor, budget)
        if item.locator not in {candidate_locator, result_locator}:
            known.update({item.locator, item.sha256})
    return known


def do_review(value: Any, purpose: str, fresh: bool) -> dict[str, Any]:
    manifest, candidate, budget = _common(value, "review", fresh)
    gate = "review-admission:upgraded" if purpose == "admission" else "review-completion:upgraded"
    control = _authority(manifest, candidate, budget)
    producer = _identity(manifest.get("producer"), "producer")
    supporting_evidence: set[str] = set()
    if fresh:
        _strings(manifest.get("ac_ids"), "ac_ids", True)
        _strings(manifest.get("path_ids"), "path_ids", True)
        _strings(manifest.get("int_ids"), "int_ids", True)
        argv, timeout = manifest.get("review_adapter"), manifest.get("review_timeout_seconds", 10)
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x for x in argv):
            raise E("ADAPTER", "configured adapter argv is required", "UNMET")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 0 < timeout <= MAX_ADAPTER_TIMEOUT:
            raise E("ADAPTER", "bounded integer adapter timeout is required", "UNMET")
        pin_record, checked = (None, [])
        if purpose == "admission":
            pin_record, checked = _pin(control, manifest, argv, None, budget)
        supporting_evidence = _known(manifest, budget)
        started = now()
        with tempfile.TemporaryDirectory(prefix="ffs-review-") as tmp:
            run_argv = _stage(argv, checked, Path(tmp)) if checked else argv
            exit_code, stdout, _ = _run(run_argv, Path(tmp), timeout)
        completed = now()
        if exit_code or not stdout:
            raise E("ADAPTER_EMPTY", "review adapter failed or emitted no result", "UNMET")
        result, findings = _review(obj(stdout), manifest)
        reviewer = _identity({key: result.get(key) for key in ("host", "model", "session")}, "reviewer")
        if purpose == "admission":
            post_pin, post = _pin(control, manifest, argv, reviewer, Budget())
            if post_pin != pin_record or len(post) != len(checked) or any(
                (a.sha256, a.device, a.inode) != (b.sha256, b.device, b.inode) for a, b in zip(checked, post)):
                raise E("ADAPTER_CHANGED", "adapter bytes changed during execution", "UNMET")
        after = read_checked(candidate.locator)
        if (after.sha256, after.device, after.inode) != (candidate.sha256, candidate.device, candidate.inode):
            raise E("CANDIDATE_CHANGED", "candidate changed during review")
        post_support = _known(manifest, Budget())
        if post_support != supporting_evidence:
            raise E("EVIDENCE_CHANGED", "adjudication evidence changed during review")
        origin, assurance = "executed", "observed" if pin_record else "asserted"
        label = pin_record["label"] if pin_record else manifest["label"]
        execution = {"argv": argv, "started_utc": started, "completed_utc": completed, "exit_status": exit_code, "stdout_bytes": len(stdout)}
    else:
        _strings(manifest["ac_ids"], "ac_ids")
        _strings(manifest["path_ids"], "path_ids")
        _strings(manifest["int_ids"], "int_ids")
        _strings(manifest["command"], "command", True)
        if isinstance(manifest["exit_status"], bool) or not isinstance(manifest["exit_status"], int) or manifest["exit_status"] < 0:
            raise E("MANIFEST_SCHEMA", "exit_status must be a nonnegative integer")
        if manifest["platform"] != sys.platform:
            raise E("PLATFORM", "review evidence platform does not match this invocation", "UNMET")
        _timestamps(manifest["started_utc"], manifest["completed_utc"], True)
        reviewer = _identity(manifest.get("reviewer"), "reviewer")
        if not isinstance(manifest["artifacts"], list) or not manifest["artifacts"]:
            raise E("RESULT", "verified artifact inventory is required")
        artifacts: dict[str, Checked] = {}
        for descriptor in manifest["artifacts"]:
            item = _artifact(descriptor, budget)
            if item.locator in artifacts:
                raise E("ARTIFACT_ALIAS", "duplicate artifact locator")
            artifacts[item.locator] = item
        result_ref = manifest.get("result")
        if not isinstance(result_ref, dict) or set(result_ref) != {"locator"} or not isinstance(result_ref["locator"], str):
            raise E("RESULT", "result locator is required")
        selected = artifacts.get(result_ref["locator"])
        if not selected:
            raise E("RESULT", "result locator is not a verified artifact")
        result, findings = _review(obj(selected.data), manifest)
        if purpose == "admission":
            raise E("IMPORTED_ADMISSION", "imported review cannot admit a path", "UNMET")
        origin, assurance, label = "imported", "asserted", manifest["label"]
        supporting_evidence = _known(manifest, budget)
        execution = {"argv": manifest["command"], "started_utc": manifest["started_utc"],
                     "completed_utc": manifest["completed_utc"], "exit_status": manifest["exit_status"]}
    if reviewer == producer or (reviewer["host"], reviewer["model"]) == (producer["host"], producer["model"]) or reviewer["session"] == producer["session"]:
        raise E("REVIEWER_IDENTITY", "reviewer is not independent from producer", "UNMET")
    open_severe, blocked = _adjudicate(manifest, findings, control, supporting_evidence)
    if purpose == "admission":
        if result["verdict"] != "PASS" or open_severe:
            raise E("REVIEW_NEGATIVE", "negative or open severe review cannot admit")
        vector = vec(review_complete=True, path_admitted=True)
    else:
        if result["verdict"] == "FAIL" and not open_severe:
            raise E("REVIEW_NEGATIVE", "negative review lacks an authorized repair finding")
        vector = vec(review_complete=True, repair_authorized=open_severe)
    output = envelope(gate, purpose, "PASS", manifest, vector, [])
    output.update({"reviewer": reviewer, "producer": producer, "findings": findings, "evidence_origin": origin,
        "identity_assurance": assurance, "blocked_paths": blocked, "label": label,
        "authenticated": label == "authenticated" and origin == "executed" and assurance == "observed", "review_execution": execution,
        "host": reviewer["host"], "model": reviewer["model"]})
    if fresh:
        output["review_adapter"] = {"argv": argv}
    return output


def compare_baselines(before: Any, current: Any) -> dict[str, Any]:
    for value in (before, current):
        if not isinstance(value, dict) or value.get("completed") is not True or not isinstance(value.get("tests"), dict) or not value["tests"]:
            return {"status": "UNMET", "errors": ["completed nonempty baseline results are required"]}
        if any(not isinstance(k, str) or not k or not isinstance(v, str) or v not in {"PASS", "FAIL"} for k, v in value["tests"].items()):
            return {"status": "UNMET", "errors": ["typed PASS/FAIL results are required"]}
    old, new = before["tests"], current["tests"]
    new_failures = sorted(k for k, v in new.items() if v == "FAIL" and (k not in old or old[k] == "PASS"))
    missing = sorted(k for k in old if k not in new)
    remaining = sorted(k for k, v in new.items() if v == "FAIL")
    return {"status": "FAIL" if new_failures or missing or remaining else "PASS", "new_failures": new_failures,
        "missing_tests": missing, "remaining_failures": remaining, "suite_passed": not missing and not remaining}


def validate_evidence(value: Any, paths: list[str]) -> dict[str, Any]:
    try:
        if not isinstance(value, dict) or value.get("status") != "PASS" or not isinstance(value.get("paths"), list) or any(x not in value["paths"] for x in paths):
            raise E("EVIDENCE", "evidence is not attributable", "UNMET")
        if not isinstance(value.get("artifacts"), list) or not value["artifacts"]:
            raise E("EVIDENCE", "evidence artifacts are absent", "UNMET")
        budget = Budget()
        for item in value["artifacts"]:
            _artifact(item, budget)
        return {"status": "PASS", "errors": []}
    except (E, AttributeError) as exc:
        return {"status": "UNMET", "errors": [getattr(exc, "problem", "invalid evidence")]}


def _coverage_name(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path.suffix != ".py" or not path.as_posix().startswith(("lib/", "scripts/", "skills/")):
        raise ValueError("coverage filename is outside first-party production scope")
    if {"tests", "vendor", ".staging", "node_modules", "__pycache__"}.intersection(path.parts):
        raise ValueError("coverage filename is explicitly excluded")
    return path.as_posix()


def _coverage_bytes(raw: bytes, production_inventory: Iterable[str | Path] | str | Path | None = None) -> dict[str, Any]:
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("coverage DTD/entity input refused")
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ValueError("invalid coverage XML") from exc
    if sum(1 for _ in root.iter()) > 200000:
        raise ValueError("coverage element limit exceeded")
    def count(name: str) -> int:
        value = root.attrib.get(name)
        if value is None or not value.isdigit():
            raise ValueError(f"{name} must be a nonnegative integer")
        return int(value)
    declared = {"lines_covered": count("lines-covered"), "lines_valid": count("lines-valid"),
        "branches_covered": count("branches-covered"), "branches_valid": count("branches-valid")}
    if not declared["lines_valid"] or declared["lines_covered"] > declared["lines_valid"] or declared["branches_covered"] > declared["branches_valid"]:
        raise ValueError("invalid coverage numerator or denominator")
    files: dict[str, dict[str, int]] = {}
    totals = [0, 0, 0, 0]
    for node in root.findall(".//class"):
        name = _coverage_name(node.attrib.get("filename", ""))
        if name in files:
            raise ValueError("duplicate coverage filename")
        lines, seen, values = node.findall("./lines/line"), set(), [0, 0, 0, 0]
        if not lines:
            raise ValueError("coverage class has no line inventory")
        for line in lines:
            number, hits = line.attrib.get("number", ""), line.attrib.get("hits", "")
            if not number.isdigit() or int(number) <= 0 or int(number) in seen or not hits.isdigit():
                raise ValueError("invalid coverage line identity/count")
            seen.add(int(number))
            values[1] += 1
            values[0] += int(hits) > 0
            if line.attrib.get("branch", "false").lower() == "true":
                match = re.fullmatch(r"\s*\d+(?:\.\d+)?%\s*\((\d+)\s*/\s*(\d+)\)\s*", line.attrib.get("condition-coverage", ""))
                if not match or int(match.group(1)) > int(match.group(2)):
                    raise ValueError("invalid branch condition coverage")
                values[2] += int(match.group(1))
                values[3] += int(match.group(2))
        files[name] = dict(zip(("lines_covered", "lines_valid", "branches_covered", "branches_valid"), values))
        totals = [a + b for a, b in zip(totals, values)]
    if not files or totals != [declared["lines_covered"], declared["lines_valid"], declared["branches_covered"], declared["branches_valid"]]:
        raise ValueError("coverage totals do not reconcile with per-file observations")
    if production_inventory is not None:
        if isinstance(production_inventory, (str, Path)):
            base = Path(production_inventory)
            inventory = {x.relative_to(base).as_posix() for top in ("lib", "scripts", "skills") for x in (base / top).rglob("*.py")
                         if x.is_file() and not {"tests", "vendor", ".staging", "node_modules", "__pycache__"}.intersection(x.relative_to(base).parts)}
        else:
            inventory = {_coverage_name(str(x)) for x in production_inventory}
        if set(files) != inventory:
            raise ValueError(f"coverage production inventory mismatch: missing={sorted(inventory-set(files))}, extra={sorted(set(files)-inventory)}")
    return {**declared, "line_percent": round(declared["lines_covered"] * 100 / declared["lines_valid"], 2),
        "branch_percent": round(declared["branches_covered"] * 100 / declared["branches_valid"], 2) if declared["branches_valid"] else None,
        "branch_opportunities": bool(declared["branches_valid"]), "files": files}


def coverage_totals(path: str | Path, production_inventory: Iterable[str | Path] | str | Path | None = None) -> dict[str, Any]:
    try:
        raw = read_checked(path).data
    except E as exc:
        raise ValueError(exc.problem) from exc
    return _coverage_bytes(raw, production_inventory)


def _suite_rows(items: Any, budget: Budget) -> tuple[dict[str, str], list[dict[str, str]], list[str], list[dict[str, Any]]]:
    if not isinstance(items, list) or not items:
        raise E("SUITES", "selected suite artifacts are required", "UNMET")
    rows, artifacts, ids, observations = {}, [], [], []
    descriptor_fields = {"id", "locator", "sha256", "argv", "exit_status", "started_utc", "completed_utc"}
    result_fields = descriptor_fields - {"id", "locator", "sha256"} | {"tests", "stdout", "stderr", "environment", "dependencies", "config_sha256"}
    for descriptor in items:
        descriptor = _closed(descriptor, descriptor_fields, descriptor_fields, "SUITE")
        suite_id = _string(descriptor["id"], "suite.id")
        if suite_id in ids:
            raise E("SUITES", "suite IDs must be unique")
        ids.append(suite_id)
        item = _artifact({"locator": descriptor["locator"], "sha256": descriptor["sha256"]}, budget)
        result = _closed(obj(item.data), {"argv", "exit_status", "started_utc", "completed_utc", "tests"}, result_fields, "SUITE_RESULT")
        if _strings(result["argv"], "suite.argv", True) != _strings(descriptor["argv"], "descriptor.argv", True):
            raise E("SUITES", "suite argv mismatch")
        exit_code = result["exit_status"]
        if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code < 0 or descriptor["exit_status"] != exit_code:
            raise E("SUITES", "suite exit status mismatch")
        _timestamps(result["started_utc"], result["completed_utc"], False)
        if (descriptor["started_utc"], descriptor["completed_utc"]) != (result["started_utc"], result["completed_utc"]):
            raise E("SUITES", "suite timestamp mismatch")
        tests = result["tests"]
        if not isinstance(tests, dict) or not tests or any(not isinstance(k, str) or not k or not isinstance(v, str) or v not in {"PASS", "FAIL"} for k, v in tests.items()):
            raise E("SUITES", "suite test inventory is malformed")
        if any(name in rows for name in tests):
            raise E("SUITES", "duplicate cross-suite test identity")
        rows.update(tests)
        expected = 0 if all(x == "PASS" for x in tests.values()) else 1
        if exit_code != expected:
            raise E("SUITES", "suite exit conflicts with test results")
        if "stdout" in result:
            try:
                emitted = obj(result["stdout"].encode()) if isinstance(result["stdout"], str) else None
            except E as exc:
                raise E("SUITES", "suite stdout is not its observation") from exc
            if not isinstance(emitted, dict) or emitted.get("tests") != tests:
                raise E("SUITES", "suite stdout/test mismatch")
        artifacts.append({"locator": item.locator, "sha256": item.sha256})
        observations.append({"id": suite_id, "environment": result.get("environment", "unavailable"),
            "dependencies": result.get("dependencies", "unavailable"), "config_sha256": result.get("config_sha256", "unavailable")})
    return rows, artifacts, ids, observations


def suite_rows(items: Any) -> tuple[dict[str, str], list[dict[str, str]]]:
    rows, artifacts, _, _ = _suite_rows(items, Budget())
    return rows, artifacts


def _git_env() -> dict[str, str]:
    remove = {"GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CEILING_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX", "GIT_CONFIG",
        "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_EXTERNAL_DIFF", "GIT_DIFF_OPTS", "GIT_TRACE", "GIT_TRACE2", "GIT_TRACE2_EVENT"}
    env = {k: v for k, v in os.environ.items() if k not in remove and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat"})
    return env


def _git(repo: Path, *args: str, missing: bool = False) -> str:
    base = ["git", "-C", str(repo), "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
            "-c", "core.untrackedCache=false", "-c", "diff.external=", "-c", "color.ui=false"]
    try:
        result = subprocess.run([*base, *args], capture_output=True, text=True, timeout=5, env=_git_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise E("GIT", "read-only Git command unavailable", "UNMET") from exc
    if result.returncode:
        if missing:
            return "unavailable"
        raise E("GIT", "read-only Git inventory failed", "UNMET")
    return result.stdout.rstrip("\n")


_GIT_BLOB_CACHE: dict[tuple[str, str, str], bytes] = {}
_GIT_TREE_CACHE: dict[tuple[str, str, str], dict[str, tuple[str, str]]] = {}


def _git_bytes(repo: Path, args: list[str], max_bytes: int) -> bytes:
    command = ["git", "-C", str(repo), "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
               "-c", "core.untrackedCache=false", *args]
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_git_env())
        assert process.stdout is not None
        stdout = process.stdout.read(max_bytes + 1)
        if len(stdout) > max_bytes:
            process.kill()
            process.communicate()
            raise E("SOURCE_CANONICAL", "Git object output exceeds the canonical-source limit", "UNMET")
        _remaining, stderr = process.communicate(timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if "process" in locals():
            process.kill()
            process.communicate()
        raise E("SOURCE_CANONICAL", "bounded Git object inspection unavailable", "UNMET") from exc
    if process.returncode:
        raise E("SOURCE_CANONICAL", "managed source is absent from the declared Git generation", "UNMET")
    if stderr:
        raise E("SOURCE_CANONICAL", "Git object inspection produced unexpected diagnostics", "UNMET")
    return stdout


def _git_blob(repo: Path, generation: str, relative_path: str) -> bytes:
    """Read and cache a bounded Git blob without text normalization."""
    key = (str(repo.resolve()), generation, relative_path)
    cached = _GIT_BLOB_CACHE.get(key)
    if cached is not None:
        return cached
    size_raw = _git(repo, "cat-file", "-s", f"{generation}:{relative_path}")
    try:
        size = int(size_raw)
    except ValueError as exc:
        raise E("SOURCE_CANONICAL", "Git blob size is malformed", "UNMET") from exc
    if size < 0 or size > MAX_INPUT_BYTES:
        raise E("SOURCE_CANONICAL", "Git blob exceeds the canonical per-leaf limit", "UNMET")
    value = _git_bytes(repo, ["show", f"{generation}:{relative_path}"], MAX_INPUT_BYTES)
    if len(value) != size:
        raise E("SOURCE_CANONICAL", "Git blob size changed during inspection", "UNMET")
    if len(_GIT_BLOB_CACHE) >= MAX_CANONICAL_ARTIFACTS:
        raise E("SOURCE_CANONICAL", "Git blob cache exceeds the canonical-source limit", "UNMET")
    _GIT_BLOB_CACHE[key] = value
    return value


def _git_tree(repo: Path, generation: str, relative_root: str) -> dict[str, tuple[str, str]]:
    """Return committed leaves below relative_root as relative path -> (mode, oid)."""
    key = (str(repo.resolve()), generation, relative_root)
    cached = _GIT_TREE_CACHE.get(key)
    if cached is not None:
        return cached
    raw = _git_bytes(repo, ["ls-tree", "-r", "-z", "--full-tree", generation, "--", relative_root],
                     MAX_ADAPTER_OUTPUT)
    prefix = relative_root.rstrip("/") + "/" if relative_root else ""
    result: dict[str, tuple[str, str]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path_raw = record.split(b"\t", 1)
            mode_raw, object_type, oid_raw = metadata.split(b" ", 2)
            full_path = path_raw.decode("utf-8")
            mode, object_id = mode_raw.decode("ascii"), oid_raw.decode("ascii")
        except (ValueError, UnicodeDecodeError) as exc:
            raise E("SOURCE_CANONICAL", "Git tree inventory is malformed", "UNMET") from exc
        if object_type != b"blob" or not full_path.startswith(prefix):
            raise E("SOURCE_CANONICAL", "Git tree contains an unsupported canonical entry", "UNMET")
        relative = full_path[len(prefix):]
        if not relative or relative in result or mode not in {"100644", "100755", "120000"}:
            raise E("SOURCE_CANONICAL", "Git tree path, type, or mode is unsupported", "UNMET")
        result[relative] = (mode, object_id)
        if len(result) > MAX_ARTIFACTS:
            raise E("SOURCE_CANONICAL", "Git tree exceeds the canonical leaf-count limit", "UNMET")
    if not result:
        raise E("SOURCE_CANONICAL", "canonical directory has no committed leaves", "UNMET")
    _GIT_TREE_CACHE[key] = result
    return result


def git_inventory(repository: Any) -> dict[str, Any]:
    if not isinstance(repository, str) or not Path(repository).is_absolute():
        return {"head": "unavailable", "origin_main": "unavailable", "source_inventory": []}
    requested = Path(repository)
    try:
        repo = Path(_git(requested, "rev-parse", "--show-toplevel")).resolve()
    except AssertionError:
        # Compatibility for isolated inventory readers that replace _git with a
        # fixed transcript. Production _git always observes the true toplevel.
        repo = requested.resolve()
    inventory_bytes = 0

    def inventory_read(path: Path) -> Checked:
        nonlocal inventory_bytes
        item = read_checked(path, max_bytes=MAX_INVENTORY_FILE_BYTES, allow_empty=True)
        inventory_bytes += len(item.data)
        if inventory_bytes > MAX_INVENTORY_TOTAL_BYTES:
            raise E("EVIDENCE_LIMIT", "repository inventory exceeds the cumulative byte limit")
        return item

    configs = []
    for scope in ("--local", "--worktree"):
        raw = _git(repo, "config", scope, "--no-includes", "--null", "--list", missing=scope == "--worktree")
        if raw != "unavailable":
            configs.extend(x for x in raw.split("\0") if x)
    for record in configs:
        key = record.split("\n", 1)[0].split("=", 1)[0].lower()
        if key.startswith(("include.", "includeif.", "filter.", "diff.")):
            raise E("GIT_CONFIG", "execution-widening Git config is refused", "UNMET")
    index_raw = _git(repo, "rev-parse", "--git-path", "index", missing=True)
    index = Path(index_raw) if Path(index_raw).is_absolute() else repo / index_raw
    before = inventory_read(index).sha256 if index.is_file() else "unavailable"
    head, branch = _git(repo, "rev-parse", "HEAD"), _git(repo, "symbolic-ref", "--short", "-q", "HEAD", missing=True)
    dirty = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    worktrees, origin = _git(repo, "worktree", "list", "--porcelain"), _git(repo, "rev-parse", "origin/main", missing=True)
    tracked = _git(repo, "ls-files", "-z").split("\0")
    untracked = _git(repo, "ls-files", "-z", "--others", "--exclude-standard").split("\0")
    source = []
    first_party_roots = {"lib", "scripts", "skills"}
    root_sources = {"setup.sh"}
    for rel in sorted({x for x in tracked + untracked if x and (Path(x).parts[0] in first_party_roots or x in root_sources)}):
        path = repo / rel
        if path.is_file() and not path.is_symlink() and not {".git", "node_modules", "vendor", "__pycache__"}.intersection(Path(rel).parts):
            source.append({"path": rel, "sha256": inventory_read(path).sha256})
    after = inventory_read(index).sha256 if index.is_file() else "unavailable"
    if before != after:
        raise E("GIT_MUTATION", "Git index changed during read-only inventory")
    common_raw = _git(repo, "rev-parse", "--git-common-dir")
    common = Path(common_raw) if Path(common_raw).is_absolute() else repo / common_raw
    return {"root": str(repo), "common_dir": str(common.resolve()), "head": head, "branch": branch,
        "dirty_paths": dirty.splitlines(), "worktrees": worktrees.splitlines(), "origin_main": origin,
        "source_inventory": source, "index_sha256": before}


FULL_KEYS = {"ci", "python", "bats", "backups_recovery", "source_runtime", "tools_customizations", "coverage", "environment"}


M0_PROFILE = "m0/v1"
M0_BACKUP_SURFACES = {"activation-snapshot", "claude-profile", "codex-profile", "gstack-customizations",
                      "gstack-original", "homebrew-snapshot", "node-custom-tap", "node-global-npm",
                      "node-old-keg", "shared-skill-incident"}


def _inside(path: Path, root: Path) -> bool:
    """Return true only when path is a strict descendant of root."""
    try:
        path.relative_to(root)
        return path != root
    except ValueError:
        return False


def _without_symlink_ancestors(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    for part in relative.parts:
        current /= part
        try:
            if current.is_symlink():
                return False
        except OSError:
            return False
    return True


def _directory_fingerprint(root: Path, budget: Budget | None = None) -> tuple[str, dict[str, str]]:
    digest, entries = hashlib.sha256(), {}
    children = []
    for child in root.rglob("*"):
        if len(children) >= MAX_ARTIFACTS:
            raise E("SOURCE_CANONICAL", "directory exceeds the canonical entry-count limit", "UNMET")
        children.append(child)
    for child in sorted(children, key=lambda item: item.as_posix()):
        relative = child.relative_to(root).as_posix()
        if child.is_symlink():
            encoded = b"L\0" + relative.encode() + b"\0" + os.readlink(child).encode()
            kind = "symlink"
        elif child.is_file():
            encoded = b"F\0" + relative.encode() + b"\0" + h(read_checked(child, budget).data).encode()
            kind = "file"
        elif child.is_dir():
            encoded, kind = b"D\0" + relative.encode(), "directory"
        else:
            raise E("SOURCE_CANONICAL", "directory inventory contains an unsupported entry", "UNMET")
        digest.update(encoded + b"\n")
        entries[relative] = kind
    return digest.hexdigest(), entries


def _directory_mapping(mapping: dict[str, Any], managed: str, install: dict[str, Any], source_root: Path,
                       project_root: Path | None, generation: str, budget: Budget) -> None:
    fields = {"managed_path", "type", "source_root", "staged_root", "fingerprint", "entries"}
    mapping = _closed(mapping, fields, fields, "SOURCE_CANONICAL")
    if not isinstance(mapping["entries"], list) or not mapping["entries"] or len(mapping["entries"]) > MAX_ARTIFACTS:
        raise E("SOURCE_CANONICAL", "directory mapping entries exceed the canonical limit", "UNMET")
    if not isinstance(mapping["source_root"], str) or not isinstance(mapping["staged_root"], str):
        raise E("SOURCE_CANONICAL", "directory mapping roots must be absolute strings", "UNMET")
    source, staged = Path(mapping["source_root"]), Path(mapping["staged_root"])
    expected_stage = Path(managed) if project_root is None else project_root / managed
    if (not source.is_absolute() or not staged.is_absolute() or source.is_symlink() or staged.is_symlink()
            or not source.is_dir() or not staged.is_dir() or not _inside(source.resolve(), source_root)
            or staged != expected_stage or staged.resolve() != expected_stage.resolve()
            or not _without_symlink_ancestors(staged, project_root or staged.parent)):
        raise E("SOURCE_CANONICAL", "directory mapping roots escape canonical ownership", "UNMET")
    try:
        source_digest, source_entries = _directory_fingerprint(source, budget)
        staged_digest, staged_entries = _directory_fingerprint(staged, budget)
    except E as exc:
        if exc.code == "SOURCE_CANONICAL":
            raise
        raise E("SOURCE_CANONICAL", "canonical directory exceeds its artifact policy", "UNMET") from exc
    if mapping["fingerprint"] != "dir:" + source_digest or staged_digest != source_digest or install["paths"].get(managed, {}).get("fingerprint") != mapping["fingerprint"]:
        raise E("SOURCE_CANONICAL", "directory fingerprint differs from source, stage, or install manifest", "UNMET")
    declared: dict[str, dict[str, Any]] = {}
    for raw in mapping["entries"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("relative_path"), str) or raw["relative_path"] in declared:
            raise E("SOURCE_CANONICAL", "directory entries must have unique paths", "UNMET")
        relative = raw["relative_path"]
        if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise E("SOURCE_CANONICAL", "directory entry path is unsafe", "UNMET")
        declared[relative] = raw
    source_relative = source.resolve().relative_to(source_root).as_posix()
    committed = _git_tree(source_root, generation, source_relative)
    committed_entries: dict[str, str] = {
        relative: "symlink" if mode == "120000" else "file"
        for relative, (mode, _oid) in committed.items()
    }
    for relative in tuple(committed_entries):
        parent = Path(relative).parent
        while parent != Path("."):
            committed_entries[parent.as_posix()] = "directory"
            parent = parent.parent
    if (set(declared) != set(source_entries) or source_entries != staged_entries
            or source_entries != committed_entries):
        raise E("SOURCE_CANONICAL", "directory mapping does not cover exact source and staged inventories", "UNMET")
    for relative, kind in source_entries.items():
        raw = declared[relative]
        if raw.get("type") != kind:
            raise E("SOURCE_CANONICAL", "directory entry type differs from observed inventory", "UNMET")
        source_path, staged_path = source / relative, staged / relative
        if kind == "directory":
            if set(raw) != {"relative_path", "type"}:
                raise E("SOURCE_CANONICAL", "directory entries cannot carry unverified fields", "UNMET")
        elif kind == "file":
            if set(raw) != {"relative_path", "type", "source_artifact", "staged_artifact"}:
                raise E("SOURCE_CANONICAL", "file entries need checked source and staged artifacts", "UNMET")
            source_item = _canonical_artifact(raw["source_artifact"], budget)
            staged_item = _canonical_artifact(raw["staged_artifact"], budget)
            if Path(source_item.locator) != source_path or Path(staged_item.locator) != staged_path or source_item.sha256 != staged_item.sha256:
                raise E("SOURCE_CANONICAL", "directory file bytes differ from mapping", "UNMET")
            rel_to_repo = source_path.resolve().relative_to(source_root).as_posix()
            git_mode, _object_id = committed[relative]
            source_mode = "100755" if source_path.stat().st_mode & stat.S_IXUSR else "100644"
            staged_mode = "100755" if staged_path.stat().st_mode & stat.S_IXUSR else "100644"
            if git_mode != source_mode or git_mode != staged_mode or h(_git_blob(source_root, generation, rel_to_repo)) != source_item.sha256:
                raise E("SOURCE_CANONICAL", "directory file is absent or changed from Git generation", "UNMET")
        else:
            if set(raw) != {"relative_path", "type", "source_link_target", "staged_link_target"}:
                raise E("SOURCE_CANONICAL", "symlink entries need explicit intended targets", "UNMET")
            target = raw["source_link_target"]
            if not isinstance(target, str) or target != raw["staged_link_target"] or target != os.readlink(source_path) or target != os.readlink(staged_path):
                raise E("SOURCE_CANONICAL", "symlink targets differ from mapping", "UNMET")
            if not _inside((source_path.parent / target).resolve(), source) or not _inside((staged_path.parent / target).resolve(), staged):
                raise E("SOURCE_CANONICAL", "symlink target escapes managed directory", "UNMET")
            rel_to_repo = source_path.relative_to(source_root).as_posix()
            if committed[relative][0] != "120000" or _git_blob(source_root, generation, rel_to_repo) != target.encode():
                raise E("SOURCE_CANONICAL", "symlink target is absent or changed from Git generation", "UNMET")


def _doctor_passes(value: Any) -> bool:
    if (not isinstance(value, dict) or value.get("schema") != "ffs.doctor-observation/v1"
            or value.get("exit_status") != 0 or not isinstance(value.get("stdout"), str)):
        return False
    stdout = value["stdout"].strip()
    if "doctor: PASS" in stdout:
        return True
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        return False
    if (not isinstance(result, dict) or result.get("schema") != "ffs.doctor/v1"
            or result.get("exit_code") != 0 or result.get("status") not in {"ok", "degraded"}
            or not isinstance(result.get("checks"), list) or not result["checks"]):
        return False
    for check in result["checks"]:
        if (not isinstance(check, dict)
                or set(check) not in ({"id", "status", "message"},
                                      {"id", "status", "message", "remediation"})
                or not isinstance(check["id"], str) or not check["id"]
                or check["status"] not in {"pass", "warn"}
                or not isinstance(check["message"], str)
                or ("remediation" in check
                    and (not isinstance(check["remediation"], str) or not check["remediation"]))):
            return False
    return True


def _canonical_artifact(value: Any, budget: Budget) -> Checked:
    try:
        return _artifact(value, budget)
    except E as exc:
        if exc.code == "SOURCE_CANONICAL":
            raise
        raise E("SOURCE_CANONICAL", "canonical artifact is malformed or exceeds policy", "UNMET") from exc


def _canonical_source(value: Any, budget: Budget, repository: dict[str, Any], candidate: Checked) -> dict[str, Any]:
    value = _closed(value, {"manifests", "upstream_mappings", "current_dirty_inventory"},
                    {"manifests", "upstream_mappings", "current_dirty_inventory"}, "SOURCE_CANONICAL")
    if (not isinstance(value["manifests"], list) or not value["manifests"]
            or len(value["manifests"]) > MAX_ARTIFACTS):
        raise E("SOURCE_CANONICAL", "canonical source manifests are required", "UNMET")
    # These are intentionally separate observations: neither can establish canonical ownership.
    if value["upstream_mappings"] != [] or value["current_dirty_inventory"] != []:
        raise E("SOURCE_CANONICAL", "canonical source auxiliary inventories are not yet defined", "UNMET")
    if not repository.get("root") or not repository.get("common_dir") or not repository.get("head"):
        raise E("SOURCE_CANONICAL", "M0 canonical proof requires an observed repository", "UNMET")
    observed_common = Path(repository["common_dir"]) if repository.get("common_dir") else None
    canonical_budget = Budget(max_artifacts=MAX_CANONICAL_ARTIFACTS,
                              max_evidence_bytes=MAX_INVENTORY_TOTAL_BYTES)
    candidate_path = Path(candidate.locator)
    try:
        candidate_root = Path(_git(candidate_path.parent, "rev-parse", "--show-toplevel")).resolve()
        candidate_common_raw = _git(candidate_root, "rev-parse", "--git-common-dir")
        candidate_head = _git(candidate_root, "rev-parse", "HEAD")
    except E as exc:
        raise E("SOURCE_CANONICAL", "candidate is not in a canonical Git worktree", "UNMET") from exc
    candidate_common_path = Path(candidate_common_raw)
    candidate_common = (candidate_common_path if candidate_common_path.is_absolute()
                        else candidate_root / candidate_common_path).resolve()
    if not _inside(candidate_path.resolve(), candidate_root):
        raise E("SOURCE_CANONICAL", "candidate is outside its canonical Git worktree", "UNMET")
    verified, manifest_keys = [], set()
    inventory_cache: dict[str, dict[str, Any]] = {}
    for raw in value["manifests"]:
        required_fields = {"scope", "install_manifest", "project_root", "resolved_source_root", "repository", "doctor_artifact", "mappings"}
        row = _closed(raw, required_fields, required_fields | {"profile_root"}, "SOURCE_CANONICAL")
        if row["scope"] not in {"project", "user"} or not isinstance(row["resolved_source_root"], str):
            raise E("SOURCE_CANONICAL", "canonical manifest scope and roots are invalid", "UNMET")
        project_root = Path(row["project_root"]) if row["scope"] == "project" and isinstance(row["project_root"], str) else None
        source_root = Path(row["resolved_source_root"])
        if (row["scope"] == "user" and row["project_root"] is not None) or (row["scope"] == "project" and (project_root is None or not project_root.is_absolute())) or not source_root.is_absolute():
            raise E("SOURCE_CANONICAL", "canonical manifest project scope is invalid", "UNMET")
        repo = _closed(row["repository"], {"root", "common_dir", "head", "generation"}, {"root", "common_dir", "head", "generation"}, "SOURCE_CANONICAL")
        if any(not isinstance(repo.get(key), str) for key in ("root", "common_dir", "head", "generation")):
            raise E("SOURCE_CANONICAL", "canonical repository identity must contain strings", "UNMET")
        canonical_inventory = inventory_cache.get(repo["root"])
        if canonical_inventory is None:
            canonical_inventory = git_inventory(repo["root"])
            inventory_cache[repo["root"]] = canonical_inventory
        if (not canonical_inventory.get("root") or source_root != Path(canonical_inventory["root"]) or repo["root"] != canonical_inventory["root"]
                or repo["common_dir"] != canonical_inventory.get("common_dir") or repo["head"] != canonical_inventory.get("head")
                or (observed_common is not None and repo["common_dir"] != str(observed_common))
                or not isinstance(repo["generation"], str) or not re.fullmatch(r"[0-9a-f]{40,64}", repo["generation"]) or repo["generation"] != repo["head"]):
            raise E("SOURCE_CANONICAL", "canonical repository identity or generation differs from observation", "UNMET")
        declared_worktrees = {line.removeprefix("worktree ") for line in canonical_inventory["worktrees"]
                              if line.startswith("worktree ")}
        if (str(candidate_root) not in declared_worktrees or candidate_common != Path(repo["common_dir"])
                or candidate_head != repo["generation"]):
            raise E("SOURCE_CANONICAL", "candidate worktree is not part of the canonical generation", "UNMET")
        candidate_relative = candidate_path.resolve().relative_to(candidate_root).as_posix()
        if h(_git_blob(candidate_root, repo["generation"], candidate_relative)) != candidate.sha256:
            raise E("SOURCE_CANONICAL", "candidate bytes are not part of the canonical generation", "UNMET")
        install_item = _canonical_artifact(row["install_manifest"], canonical_budget)
        install_path = Path(install_item.locator)
        manifest_key = (row["scope"], str(install_path.resolve()))
        if manifest_key in manifest_keys:
            raise E("SOURCE_CANONICAL", "canonical install manifests must be unique", "UNMET")
        manifest_keys.add(manifest_key)
        if row["scope"] == "project":
            assert project_root is not None
            profile_root = project_root
            expected_manifest = project_root / ".feature-fix-swarm" / "install-manifest.json"
        else:
            explicit_profile = row.get("profile_root")
            if explicit_profile is not None and (not isinstance(explicit_profile, str) or not Path(explicit_profile).is_absolute()):
                raise E("SOURCE_CANONICAL", "user profile root must be an absolute path", "UNMET")
            if explicit_profile is not None:
                profile_root = Path(explicit_profile)
                expected_manifest = profile_root / ".cache" / "feature-fix-swarm" / "install-manifest.json"
            elif install_path.parent.name == "feature-fix-swarm" and install_path.parent.parent.name == ".cache":
                profile_root = install_path.parent.parent.parent
                expected_manifest = profile_root / ".cache" / "feature-fix-swarm" / "install-manifest.json"
            else:
                profile_root = install_path.parent.parent
                expected_manifest = profile_root / ".feature-fix-swarm" / "install-manifest.json"
        if install_path != expected_manifest or install_path.resolve() != expected_manifest.resolve():
            raise E("SOURCE_CANONICAL", "install manifest is outside its scope-defined location", "UNMET")
        install = obj(install_item.data)
        install_fields = {"schema", "version", "scope", "installed_at", "source", "paths", "gsd"}
        if not isinstance(install, dict) or set(install) != install_fields or install.get("schema") != "ffs.install/v1" or install.get("scope") != row["scope"] or not isinstance(install.get("source"), str) or not isinstance(install.get("paths"), dict):
            raise E("SOURCE_CANONICAL", "checked install manifest is malformed", "UNMET")
        install_source = Path(install["source"])
        resolved_install_source = install_source.resolve() if install_source.is_absolute() else (profile_root / install_source).resolve()
        if resolved_install_source != source_root or not install["paths"]:
            raise E("SOURCE_CANONICAL", "install manifest does not resolve to its canonical source", "UNMET")
        for key, metadata in install["paths"].items():
            if (not isinstance(key, str) or not isinstance(metadata, dict)
                    or set(metadata) != {"fingerprint"} or not isinstance(metadata["fingerprint"], str)):
                raise E("SOURCE_CANONICAL", "install manifest paths are malformed", "UNMET")
        doctor = obj(_canonical_artifact(row["doctor_artifact"], canonical_budget).data)
        if not _doctor_passes(doctor):
            raise E("SOURCE_CANONICAL", "doctor observation is not a checked PASS", "UNMET")
        if (not isinstance(row["mappings"], list) or not row["mappings"]
                or len(row["mappings"]) > MAX_ARTIFACTS or len(install["paths"]) > MAX_ARTIFACTS):
            raise E("SOURCE_CANONICAL", "managed-path mappings are required", "UNMET")
        mapped, install_paths = set(), set(install["paths"])
        for mapping in row["mappings"]:
            if not isinstance(mapping, dict):
                raise E("SOURCE_CANONICAL", "managed mapping must be an object", "UNMET")
            managed = mapping.get("managed_path")
            if (not isinstance(managed, str) or not managed or ".." in Path(managed).parts
                    or str(Path(managed)) != managed or managed in mapped):
                raise E("SOURCE_CANONICAL", "managed paths must be unique normalized relative paths", "UNMET")
            mapped.add(managed)
            user_scope = row["scope"] == "user"
            if (user_scope and (not Path(managed).is_absolute() or ".." in Path(managed).parts)) or (not user_scope and (Path(managed).is_absolute() or ".." in Path(managed).parts)):
                raise E("SOURCE_CANONICAL", "managed path does not match manifest scope", "UNMET")
            if user_scope and not _inside(Path(managed).resolve(), profile_root.resolve()):
                raise E("SOURCE_CANONICAL", "user managed path escapes the declared profile root", "UNMET")
            if mapping.get("type") == "directory":
                _directory_mapping(mapping, managed, install, source_root, project_root, repo["generation"], canonical_budget)
                continue
            base = {"managed_path", "type", "source_artifact", "staged_artifact"}
            mapping = _closed(mapping, base, base, "SOURCE_CANONICAL")
            if mapping["type"] != "file":
                raise E("SOURCE_CANONICAL", "M0 mappings must be regular files or directory inventories", "UNMET")
            source = _canonical_artifact(mapping["source_artifact"], canonical_budget)
            staged = _canonical_artifact(mapping["staged_artifact"], canonical_budget)
            source_path, staged_path = Path(source.locator), Path(staged.locator)
            expected_stage = Path(managed) if user_scope else project_root / managed  # type: ignore[operator]
            if not _inside(source_path.resolve(), source_root) or staged_path != expected_stage or source.sha256 != staged.sha256:
                raise E("SOURCE_CANONICAL", "source and staged managed bytes do not match canonical ownership", "UNMET")
            rel = source_path.resolve().relative_to(source_root).as_posix()
            committed = _git_blob(source_root, repo["generation"], rel)
            if h(committed) != source.sha256:
                raise E("SOURCE_CANONICAL", "managed source bytes are not immutable generation bytes", "UNMET")
            fingerprint = install["paths"].get(managed, {}).get("fingerprint")
            if fingerprint != "file:" + staged.sha256:
                raise E("SOURCE_CANONICAL", "install manifest fingerprint differs from staged bytes", "UNMET")
        if mapped != install_paths:
            raise E("SOURCE_CANONICAL", "every installed path needs one canonical mapping", "UNMET")
        verified.append({"scope": row["scope"], "project_root": str(project_root) if project_root else None,
                         "profile_root": str(profile_root), "source_root": str(source_root),
                         "install_manifest_sha256": install_item.sha256,
                         "generation": repo["generation"], "generation_source": "git-head",
                         "managed_paths": sorted(mapped)})
    return {"manifests": verified}


def _inventory_observations(name: str, value: Any, budget: Budget, provenance: dict[str, str],
                            repository: dict[str, Any], m0: bool, candidate: Checked) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise E("FULL_INVENTORY", f"{name} observations must be a typed object", "UNMET")
    if name in {"ci", "python", "bats"}:
        allowed = {"suite_artifacts", "head", "origin_main"} if name == "ci" else {"suite_artifacts"}
        value = _closed(value, allowed, allowed, "FULL_INVENTORY")
        rows, artifacts, ids, metadata = _suite_rows(value["suite_artifacts"], budget)
        if name == "ci":
            if not isinstance(value["head"], str) or not re.fullmatch(r"[0-9a-f]{40,64}", value["head"]) or value["head"] != value["origin_main"]:
                raise E("FULL_INVENTORY", "CI evidence must run at the exact origin/main identity", "UNMET")
            if repository.get("origin_main") != value["head"]:
                raise E("FULL_INVENTORY", "CI evidence does not match observed repository origin/main", "UNMET")
        return {"tests": rows, "artifacts": artifacts, "suite_ids": ids, "metadata": metadata,
                **({"head": value["head"], "origin_main": value["origin_main"]} if name == "ci" else {})}
    if name == "backups_recovery":
        required = {"required_surface_ids"} if m0 else set()
        allowed = {"entries", "surfaces", "required_surface_ids"} if m0 else {"entries"}
        value = _closed(value, required, allowed, "FULL_INVENTORY")
        if ("entries" in value) == ("surfaces" in value):
            raise E("FULL_INVENTORY", "backup/recovery entries are required", "UNMET")
        if m0 and (not isinstance(value["required_surface_ids"], list)
                   or any(not isinstance(item, str) for item in value["required_surface_ids"])):
            raise E("BACKUP_EVIDENCE", "required backup surface IDs must be strings", "UNMET")
        records = value.get("entries")
        if "surfaces" in value:
            surfaces = value["surfaces"]
            if not isinstance(surfaces, list) or not surfaces:
                raise E("BACKUP_EVIDENCE", "backup surfaces must be a nonempty array", "UNMET")
            records = []
            for raw_surface in surfaces:
                surface = _closed(raw_surface, {"name", "recoveries"}, {"name", "recoveries"}, "BACKUP_EVIDENCE")
                if not isinstance(surface["recoveries"], list) or not surface["recoveries"]:
                    raise E("BACKUP_EVIDENCE", "every backup surface needs recovery artifacts", "UNMET")
                records.extend({"name": surface["name"], **recovery} for recovery in surface["recoveries"])
        if not isinstance(records, list) or not records:
            raise E("FULL_INVENTORY", "backup/recovery entries are required", "UNMET")
        result: list[dict[str, str]] = []
        physical_identities: set[tuple[int, int]] = set()
        fields = {"name", "expected_sha256", "backup_artifact", "restored_artifact", "verification_artifact"}
        for raw in records:
            entry = _closed(raw, fields, fields, "BACKUP_EVIDENCE")
            if not isinstance(entry["name"], str) or not entry["name"]:
                raise E("BACKUP_EVIDENCE", "backup surface name must be a nonempty string", "UNMET")
            if not isinstance(entry["expected_sha256"], str) or not HASH_RE.fullmatch(entry["expected_sha256"]):
                raise E("BACKUP_EVIDENCE", "backup expected SHA-256 is invalid", "UNMET")
            backup = _artifact(entry["backup_artifact"], budget)
            restored = _artifact(entry["restored_artifact"], budget)
            backup_identity, restored_identity = (backup.device, backup.inode), (restored.device, restored.inode)
            backup_path, restored_path = Path(backup.locator), Path(restored.locator)
            if (backup_identity == restored_identity or backup_identity in physical_identities
                    or restored_identity in physical_identities
                    or (m0 and (backup_path.parent == restored_path.parent
                        or not any(entry["name"] in part for part in backup_path.parts)
                        or not any(entry["name"] in part for part in restored_path.parts)))):
                raise E("BACKUP_EVIDENCE", "backup and restore need distinct surface-bound artifacts", "UNMET")
            physical_identities.update({backup_identity, restored_identity})
            verification = obj(_artifact(entry["verification_artifact"], budget).data)
            if backup.sha256 != entry["expected_sha256"] or restored.sha256 != entry["expected_sha256"] or not isinstance(verification, dict) or verification != {
                "name": entry["name"], "status": "PASS", "expected_sha256": entry["expected_sha256"],
                "backup_sha256": backup.sha256, "restored_sha256": restored.sha256}:
                raise E("BACKUP_EVIDENCE", "backup and recovery bytes do not reconcile", "UNMET")
            result.append({"name": entry["name"], "sha256": backup.sha256})
        names = [row["name"] for row in result]
        if m0 and (set(value["required_surface_ids"]) != M0_BACKUP_SURFACES or len(value["required_surface_ids"]) != len(M0_BACKUP_SURFACES) or set(names) != M0_BACKUP_SURFACES):
            raise E("BACKUP_EVIDENCE", "M0 requires every pinned backup surface exactly once", "UNMET")
        grouped = {surface: [row["sha256"] for row in result if row["name"] == surface]
                   for surface in sorted(set(names))}
        return {"entries": {surface: hashes[0] if len(hashes) == 1 else hashes
                            for surface, hashes in grouped.items()}}
    if name == "source_runtime":
        value = _closed(value, {"entries", "canonical_source"} if m0 else {"entries"}, {"entries", "canonical_source"} if m0 else {"entries"}, "FULL_INVENTORY")
        if not isinstance(value["entries"], list):
            raise E("FULL_INVENTORY", "source/runtime entries are required", "UNMET")
        expected = {"source": "source_sha256", "binary": "binary_sha256", "bundle": "bundle_sha256", "config": "config_sha256"}
        observed: dict[str, str] = {}
        for raw in value["entries"]:
            entry = _closed(raw, {"role", "artifact"}, {"role", "artifact"}, "SOURCE_RUNTIME_EVIDENCE")
            if entry["role"] not in expected or entry["role"] in observed:
                raise E("SOURCE_RUNTIME_EVIDENCE", "source/runtime roles must be exact and unique", "UNMET")
            item = _artifact(entry["artifact"], budget)
            if item.sha256 != provenance[expected[entry["role"]]]:
                raise E("SOURCE_RUNTIME_EVIDENCE", "source/runtime bytes do not match provenance", "UNMET")
            observed[entry["role"]] = item.sha256
        if set(observed) != set(expected):
            raise E("SOURCE_RUNTIME_EVIDENCE", "all source/runtime/config roles are required", "UNMET")
        result = {"entries": observed}
        if m0:
            result["canonical_source"] = _canonical_source(value["canonical_source"], budget, repository, candidate)
        return result
    if name == "tools_customizations":
        value = _closed(value, {"entries"}, {"entries"}, "FULL_INVENTORY")
        if not isinstance(value["entries"], list) or not value["entries"]:
            raise E("FULL_INVENTORY", "tool/customization inventory is required", "UNMET")
        names, result = set(), []
        for raw in value["entries"]:
            entry = _closed(raw, {"name", "artifact"}, {"name", "artifact"}, "TOOL_EVIDENCE")
            tool = _string(entry["name"], "tool.name")
            if tool in names:
                raise E("TOOL_EVIDENCE", "tool names must be unique", "UNMET")
            names.add(tool)
            item = _artifact(entry["artifact"], budget)
            result.append({"name": tool, "sha256": item.sha256})
        return {"entries": result}
    if name == "coverage":
        value = _closed(value, {"xml_artifact", "production_inventory", "suite_artifacts"},
                        {"xml_artifact", "production_inventory", "suite_artifacts"}, "FULL_INVENTORY")
        inventory = _strings(value["production_inventory"], "coverage.production_inventory", True)
        if not repository.get("root") or not isinstance(repository.get("source_inventory"), list):
            raise E("COVERAGE", "coverage completeness requires an observed repository inventory", "UNMET")
        exclusions = {"tests", "vendor", ".staging", "node_modules", "__pycache__", ".git"}
        production = {row["path"] for row in repository["source_inventory"]
                      if row["path"].endswith(".py") and not exclusions.intersection(Path(row["path"]).parts)}
        if not production or set(inventory) != production:
            raise E("COVERAGE", "coverage inventory differs from the observed first-party Python corpus", "UNMET")
        report = _artifact(value["xml_artifact"], budget)
        try:
            totals = _coverage_bytes(report.data, inventory)
        except ValueError as exc:
            raise E("COVERAGE", str(exc), "UNMET") from exc
        rows, _, ids, _ = _suite_rows(value["suite_artifacts"], budget)
        return {"report_sha256": report.sha256, "totals": totals, "tests": rows, "suite_ids": ids}
    if name == "environment":
        value = _closed(value, {"entries"}, {"entries"}, "FULL_INVENTORY")
        if not isinstance(value["entries"], list):
            raise E("FULL_INVENTORY", "environment tuple entries are required", "UNMET")
        required, observed = {"platform", "python", "dependencies", "config"}, {}
        for raw in value["entries"]:
            entry = _closed(raw, {"name", "artifact"}, {"name", "artifact"}, "ENVIRONMENT_EVIDENCE")
            name_value = entry["name"]
            if name_value not in required or name_value in observed:
                raise E("ENVIRONMENT_EVIDENCE", "environment tuple names must be exact and unique", "UNMET")
            item = _artifact(entry["artifact"], budget)
            record = obj(item.data)
            if not isinstance(record, dict) or set(record) != {"schema", "name", "value"} or record["schema"] != "ffs.environment-observation/v1" or record["name"] != name_value or not isinstance(record["value"], str) or not record["value"]:
                raise E("ENVIRONMENT_EVIDENCE", "environment observation bytes are malformed", "UNMET")
            observed[name_value] = {"value": record["value"], "sha256": item.sha256}
        if set(observed) != required:
            raise E("ENVIRONMENT_EVIDENCE", "complete platform/runtime/dependency/config tuple is required", "UNMET")
        return {"entries": observed}
    raise E("FULL_INVENTORY", "unknown inventory category", "UNMET")


def _inventory(value: Any, budget: Budget, provenance: dict[str, str], repository: dict[str, Any],
               m0: bool = False, candidate: Checked | None = None) -> tuple[bool, list[str], dict[str, Any]]:
    if value is None:
        return False, sorted(FULL_KEYS), {}
    if not isinstance(value, dict) or set(value) - FULL_KEYS:
        raise E("FULL_INVENTORY", "full inventory fields are malformed", "UNMET")
    verified, unmet = {}, []
    for name in sorted(FULL_KEYS):
        record = value.get(name)
        if not isinstance(record, dict) or record.get("verified") is True or record.get("status") != "PASS" or not isinstance(record.get("artifacts"), list) or not record["artifacts"]:
            unmet.append(name)
            continue
        record = _closed(record, {"status", "artifacts"}, {"status", "artifacts"}, "FULL_INVENTORY")
        checked = [_artifact(x, budget) for x in record["artifacts"]]
        observations = []
        for item in checked:
            evidence = _closed(obj(item.data),
                {"schema", "category", "status", "complete", "started_utc", "completed_utc", "provenance", "observations"},
                {"schema", "category", "status", "complete", "started_utc", "completed_utc", "provenance", "observations"},
                "FULL_INVENTORY_EVIDENCE")
            expected_schema = "ffs.full-inventory-evidence/v2" if m0 and name in {"backups_recovery", "source_runtime"} else "ffs.full-inventory-evidence/v1"
            if evidence["schema"] != expected_schema or evidence["category"] != name or evidence["status"] != "PASS" or evidence["complete"] is not True:
                raise E("FULL_INVENTORY", f"{name} inventory evidence is not a complete PASS", "UNMET")
            _timestamps(evidence["started_utc"], evidence["completed_utc"], False)
            if evidence["provenance"] != provenance:
                raise E("FULL_INVENTORY", f"{name} inventory evidence provenance mismatch", "UNMET")
            if candidate is None:
                raise E("FULL_INVENTORY", "candidate binding is unavailable", "UNMET")
            validated = _inventory_observations(name, evidence["observations"], budget, provenance, repository, m0, candidate)
            observations.append({"locator": item.locator, "sha256": item.sha256, "validated": validated})
        verified[name] = observations
    return not unmet, unmet, verified


def baseline_mode(value: Any) -> dict[str, Any]:
    manifest, candidate, budget = _common(value, "baseline")
    baseline = manifest["baseline"]
    if not isinstance(baseline, dict) or set(baseline) != {"suite_artifacts"}:
        raise E("BASELINE", "baseline suite artifact selection is required", "UNMET")
    rows, artifacts, ids, observations = _suite_rows(baseline["suite_artifacts"], budget)
    profile = manifest.get("baseline_profile")
    if "baseline_profile" in manifest and profile != M0_PROFILE:
        raise E("BASELINE_PROFILE", "unsupported baseline profile", "UNMET")
    m0 = profile == M0_PROFILE
    repository = git_inventory(manifest.get("repository"))
    complete, unmet, inventory = _inventory(manifest.get("full_inventory"), budget, manifest["provenance"], repository,
                                            m0, candidate)
    output = envelope("baseline-capture", "capture", "PASS", manifest, vec(), [])
    output.update({"tests": rows, "suite_ids": ids, "suite_observations": observations, "artifacts": artifacts,
        "suite_passed": all(x == "PASS" for x in rows.values()), "repository": repository,
        "baseline_profile": profile, "canonical_source_verified": bool(m0 and "source_runtime" in inventory),
        "full_baseline_complete": complete, "full_baseline_unmet": unmet, "full_inventory_evidence": inventory,
        "coverage_scope": "full-suite" if complete else "diagnostic", "host": _local_host(),
        "model": "parallel-host-verifier"})
    return output


def _baseline(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA or value.get("gate") != "baseline-capture" or value.get("purpose") != "capture" or value.get("status") != "PASS" or value.get("exit_status") != 0:
        raise E("BASELINE", "named baseline envelope is invalid", "UNMET")
    if compare_baselines({"completed": True, "tests": value.get("tests")}, {"completed": True, "tests": value.get("tests")}).get("status") == "UNMET":
        raise E("BASELINE", "baseline tests are malformed", "UNMET")
    _closed(value.get("binding"), {"run", "activity", "attempt"}, {"run", "activity", "attempt"}, "BASELINE")
    if not isinstance(value.get("provenance"), dict) or set(value["provenance"]) != PROVENANCE:
        raise E("BASELINE", "baseline provenance is incomplete", "UNMET")
    return value


UPGRADE_COMPONENTS = {"source", "binary", "bundle", "config", "dependencies", "environment"}
_PROVENANCE_COMPONENTS = {
    "source": "source_sha256", "binary": "binary_sha256", "bundle": "bundle_sha256", "config": "config_sha256",
}


def _upgrade_environment(value: Any, budget: Budget, expected_binding: dict[str, Any],
                         expected_provenance: dict[str, str], run: str, repository: str) -> tuple[Checked, dict[str, Checked]]:
    value = _closed(value, {"binding", "provenance", "identity_artifact"},
                    {"binding", "provenance", "identity_artifact"}, "COMPARISON_BINDING")
    if value["binding"] != expected_binding or value["provenance"] != expected_provenance:
        raise E("COMPARISON_BINDING", "before/after binding or provenance does not match its envelope")
    identity = _artifact(value["identity_artifact"], budget)
    record = _closed(obj(identity.data), {"schema", "run", "repository", "binding", "provenance", "artifacts"},
                     {"schema", "run", "repository", "binding", "provenance", "artifacts"}, "UPGRADE_ENVIRONMENT")
    if (record["schema"] != "ffs.upgrade-environment/v1" or record["run"] != run
            or record["repository"] != repository or record["binding"] != expected_binding
            or record["provenance"] != expected_provenance):
        raise E("UPGRADE_ENVIRONMENT", "environment identity does not bind run, repository, or tuple")
    artifacts = _closed(record["artifacts"], UPGRADE_COMPONENTS, UPGRADE_COMPONENTS, "UPGRADE_ENVIRONMENT")
    components: dict[str, Checked] = {}
    for component in UPGRADE_COMPONENTS:
        item = _artifact(artifacts[component], budget, max_bytes=MAX_INVENTORY_FILE_BYTES)
        components[component] = item
        key = _PROVENANCE_COMPONENTS.get(component)
        if key and item.sha256 != expected_provenance[key]:
            raise E("UPGRADE_ENVIRONMENT", "environment component differs from named provenance")
        if component == "dependencies":
            observation = obj(item.data)
            retained_dependency = (isinstance(observation, dict)
                                   and set(observation) == {"schema", "name", "value"}
                                   and observation["schema"] == "ffs.environment-observation/v1"
                                   and observation["name"] == "dependencies"
                                   and isinstance(observation["value"], str) and bool(observation["value"]))
            if not retained_dependency and (not isinstance(observation, dict) or observation.get("complete") is not True):
                raise E("UPGRADE_ENVIRONMENT", "dependency observation is incomplete", "UNMET")
        elif component == "environment":
            observation = obj(item.data)
            if not isinstance(observation, dict) or observation.get("complete") is not True:
                raise E("UPGRADE_ENVIRONMENT", "environment observation is incomplete", "UNMET")
    return identity, components


def _baseline_component_anchors(old: dict[str, Any]) -> dict[str, str]:
    inventory = old.get("full_inventory_evidence")
    if not isinstance(inventory, dict):
        raise E("COMPARISON_EVIDENCE", "baseline inventory observations are unavailable", "UNMET")
    source_rows, environment_rows = inventory.get("source_runtime"), inventory.get("environment")
    if not isinstance(source_rows, list) or not source_rows or not isinstance(environment_rows, list) or not environment_rows:
        raise E("COMPARISON_EVIDENCE", "baseline source/runtime/environment observations are unavailable", "UNMET")
    anchors: dict[str, set[str]] = {component: set() for component in UPGRADE_COMPONENTS}
    for row in source_rows:
        validated = row.get("validated") if isinstance(row, dict) else None
        entries = validated.get("entries") if isinstance(validated, dict) else None
        if not isinstance(entries, dict):
            raise E("COMPARISON_EVIDENCE", "baseline source/runtime observations are malformed", "UNMET")
        for component in _PROVENANCE_COMPONENTS:
            digest = entries.get(component)
            if not isinstance(digest, str) or not HASH_RE.fullmatch(digest):
                raise E("COMPARISON_EVIDENCE", "baseline source/runtime component is unanchored", "UNMET")
            anchors[component].add(digest)
    for row in environment_rows:
        if not isinstance(row, dict) or not isinstance(row.get("sha256"), str) or not HASH_RE.fullmatch(row["sha256"]):
            raise E("COMPARISON_EVIDENCE", "baseline environment wrapper is malformed", "UNMET")
        validated = row.get("validated")
        entries = validated.get("entries") if isinstance(validated, dict) else None
        dependency = entries.get("dependencies") if isinstance(entries, dict) else None
        digest = dependency.get("sha256") if isinstance(dependency, dict) else None
        if not isinstance(digest, str) or not HASH_RE.fullmatch(digest):
            raise E("COMPARISON_EVIDENCE", "baseline dependency observation is unanchored", "UNMET")
        anchors["dependencies"].add(digest)
        anchors["environment"].add(row["sha256"])
    if any(len(digests) != 1 for digests in anchors.values()):
        raise E("COMPARISON_EVIDENCE", "baseline inventory has ambiguous component observations", "UNMET")
    return {component: next(iter(digests)) for component, digests in anchors.items()}


def _suite_metadata(value: Any, suite_ids: Any) -> dict[str, tuple[str, str, str]]:
    if (not isinstance(value, list) or not isinstance(suite_ids, list)
            or len(value) != len(suite_ids) or any(not isinstance(name, str) or not name for name in suite_ids)
            or len(set(suite_ids)) != len(suite_ids)):
        raise E("COMPARISON_EVIDENCE", "suite metadata identity is malformed", "UNMET")
    result: dict[str, tuple[str, str, str]] = {}
    for suite_id, item in zip(suite_ids, value):
        if not isinstance(item, dict) or item.get("id") != suite_id:
            raise E("COMPARISON_EVIDENCE", "suite metadata does not match suite identity", "UNMET")
        fields = tuple(item.get(key) for key in ("environment", "dependencies", "config_sha256"))
        if any(not isinstance(field, str) or not field or field == "unavailable" for field in fields):
            raise E("COMPARISON_EVIDENCE", "suite metadata is incomplete", "UNMET")
        result[suite_id] = fields  # type: ignore[assignment]
    return result


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _complete_tuple(environment: Any, dependencies: Any, config_sha256: Any) -> tuple[str, str, str]:
    if not isinstance(environment, str) or not isinstance(dependencies, str) or not isinstance(config_sha256, str):
        raise E("COMPARISON_EVIDENCE", "suite environment tuple must be canonical strings", "UNMET")
    try:
        environment_value, dependencies_value = obj(environment.encode("utf-8")), obj(dependencies.encode("utf-8"))
    except E as exc:
        raise E("COMPARISON_EVIDENCE", "suite environment tuple is not canonical JSON", "UNMET") from exc
    if (not isinstance(environment_value, dict) or set(environment_value) != {"platform", "python"}
            or not isinstance(environment_value.get("platform"), str) or not environment_value["platform"]
            or not isinstance(environment_value.get("python"), dict)
            or set(environment_value["python"]) != {"executable", "executable_sha256", "implementation", "version"}
            or any(not isinstance(environment_value["python"].get(key), str) or not environment_value["python"][key]
                   for key in ("executable", "executable_sha256", "implementation", "version"))
            or not HASH_RE.fullmatch(environment_value["python"]["executable_sha256"])
            or _canonical_json(environment_value) != environment
            or not isinstance(dependencies_value, dict) or not dependencies_value
            or _canonical_json(dependencies_value) != dependencies
            or not HASH_RE.fullmatch(config_sha256)):
        raise E("COMPARISON_EVIDENCE", "suite environment tuple is incomplete", "UNMET")
    return environment, dependencies, config_sha256


def _environment_wrapper_tuple(item: Checked, budget: Budget, provenance: dict[str, str]) -> tuple[tuple[str, str, str], str]:
    wrapper = _closed(obj(item.data),
                      {"schema", "category", "status", "complete", "started_utc", "completed_utc", "provenance", "observations"},
                      {"schema", "category", "status", "complete", "started_utc", "completed_utc", "provenance", "observations"},
                      "COMPARISON_EVIDENCE")
    if (wrapper["schema"] != "ffs.full-inventory-evidence/v1" or wrapper["category"] != "environment"
            or wrapper["status"] != "PASS" or wrapper["complete"] is not True or wrapper["provenance"] != provenance):
        raise E("COMPARISON_EVIDENCE", "environment wrapper is not a complete tuple observation", "UNMET")
    entries = wrapper["observations"].get("entries") if isinstance(wrapper["observations"], dict) else None
    if not isinstance(entries, list):
        raise E("COMPARISON_EVIDENCE", "environment wrapper observations are malformed", "UNMET")
    observed: dict[str, str] = {}
    dependency_sha = ""
    for raw in entries:
        entry = _closed(raw, {"name", "artifact"}, {"name", "artifact"}, "COMPARISON_EVIDENCE")
        name = entry["name"]
        record_item = _artifact(entry["artifact"], budget)
        record = obj(record_item.data)
        if (name not in {"platform", "python", "dependencies", "config"} or name in observed
                or not isinstance(record, dict) or set(record) != {"schema", "name", "value"}
                or record.get("schema") != "ffs.environment-observation/v1" or record.get("name") != name
                or not isinstance(record.get("value"), str) or not record["value"]):
            raise E("COMPARISON_EVIDENCE", "environment observation is malformed", "UNMET")
        observed[name] = record["value"]
        if name == "dependencies":
            dependency_sha = record_item.sha256
    if set(observed) != {"platform", "python", "dependencies", "config"}:
        raise E("COMPARISON_EVIDENCE", "environment wrapper is incomplete", "UNMET")
    try:
        python = obj(observed["python"].encode("utf-8"))
    except E as exc:
        raise E("COMPARISON_EVIDENCE", "environment python identity is invalid", "UNMET") from exc
    return _complete_tuple(_canonical_json({"platform": observed["platform"], "python": python}),
                           observed["dependencies"], observed["config"]), dependency_sha


def _current_python_identity(environment: str, argv: list[str], budget: Budget) -> None:
    parsed = obj(environment.encode("utf-8"))
    assert isinstance(parsed, dict) and isinstance(parsed.get("python"), dict)
    python = parsed["python"]
    executable = Path(python["executable"])
    if (not executable.is_absolute() or str(executable.resolve()) != python["executable"]
            or not argv or not isinstance(argv[0], str) or str(Path(argv[0]).resolve()) != python["executable"]
            or python["implementation"] != sys.implementation.name or python["version"] != sys.version):
        raise E("COMPARISON_EVIDENCE", "current suite Python identity is inconsistent", "UNMET")
    current = read_checked(executable, budget, max_bytes=MAX_EXECUTABLE_BYTES, artifact_class="executable")
    if current.sha256 != python["executable_sha256"]:
        raise E("COMPARISON_EVIDENCE", "current suite Python executable bytes changed", "UNMET")


def _after_identity_matches_suites(components: dict[str, Checked], provenance: dict[str, str],
                                   suite_tuples: dict[str, tuple[str, str, str]], suite_argv: dict[str, list[str]], budget: Budget) -> None:
    values = set(suite_tuples.values())
    if len(values) != 1:
        raise E("COMPARISON_EVIDENCE", "after identity requires one exact current suite tuple", "UNMET")
    environment, dependencies, config_sha256 = _complete_tuple(*next(iter(values)))
    if any(suite_argv.get(suite_id) is None for suite_id in suite_tuples):
        raise E("COMPARISON_EVIDENCE", "current suite argv is unavailable", "UNMET")
    for argv in suite_argv.values():
        _current_python_identity(environment, argv, budget)
    if components["config"].sha256 != config_sha256 or config_sha256 != provenance["config_sha256"]:
        raise E("COMPARISON_EVIDENCE", "after config identity differs from current suite metadata")
    dependency = obj(components["dependencies"].data)
    if (not isinstance(dependency, dict) or set(dependency) != {"schema", "name", "value"}
            or dependency.get("schema") != "ffs.environment-observation/v1"
            or dependency.get("name") != "dependencies" or dependency.get("value") != dependencies):
        raise E("COMPARISON_EVIDENCE", "after dependency identity differs from current suite metadata")
    wrapper_tuple, dependency_sha = _environment_wrapper_tuple(components["environment"], budget, provenance)
    if (wrapper_tuple != (environment, dependencies, config_sha256)
            or components["dependencies"].sha256 != dependency_sha):
        raise E("COMPARISON_EVIDENCE", "after environment wrapper differs from current suite metadata")


def _suite_argv(manifest: dict[str, Any], suite_ids: dict[str, tuple[str, str, str]]) -> dict[str, list[str]]:
    current = manifest.get("current")
    artifacts = current.get("suite_artifacts") if isinstance(current, dict) else None
    if not isinstance(artifacts, list):
        raise E("COMPARISON_EVIDENCE", "current suite argv is unavailable", "UNMET")
    result: dict[str, list[str]] = {}
    for raw in artifacts:
        if not isinstance(raw, dict) or raw.get("id") not in suite_ids:
            raise E("COMPARISON_EVIDENCE", "current suite argv identity is malformed", "UNMET")
        suite_id, argv = raw["id"], raw.get("argv")
        if suite_id in result or not isinstance(argv, list) or not argv or any(not isinstance(value, str) or not value for value in argv):
            raise E("COMPARISON_EVIDENCE", "current suite argv is malformed", "UNMET")
        result[suite_id] = argv
    if set(result) != set(suite_ids):
        raise E("COMPARISON_EVIDENCE", "current suite argv is incomplete", "UNMET")
    return result


def _comparison_binding(value: Any, budget: Budget, old: dict[str, Any], manifest: dict[str, Any],
                        baseline_sha256: str, ledger_sha256: str,
                        before_tuples: dict[str, tuple[str, str, str]],
                        current_tuples: dict[str, tuple[str, str, str]]) -> str:
    binding = _closed(value, {"id", "run", "repository", "before", "after", "transition_artifact"},
                      {"id", "run", "repository", "before", "after", "transition_artifact"}, "COMPARISON_BINDING")
    for key in ("id", "run", "repository"):
        _string(binding[key], f"comparison_binding.{key}")
    if binding["run"] != old["binding"]["run"] or binding["run"] != manifest["binding"]["run"]:
        raise E("COMPARISON_BINDING", "comparison run is not the logical before/after run")
    baseline_repository = old.get("repository")
    if not isinstance(baseline_repository, dict) or binding["repository"] != baseline_repository.get("common_dir"):
        raise E("COMPARISON_BINDING", "comparison repository does not match the retained baseline repository")
    before, before_components = _upgrade_environment(binding["before"], budget, old["binding"], old["provenance"], binding["run"], binding["repository"])
    after, after_components = _upgrade_environment(binding["after"], budget, manifest["binding"], manifest["provenance"], binding["run"], binding["repository"])
    anchors = _baseline_component_anchors(old)
    if any(before_components[component].sha256 != anchors[component] for component in UPGRADE_COMPONENTS):
        raise E("COMPARISON_EVIDENCE", "before identity does not match retained baseline inventory", "UNMET")
    before_wrapper, before_dependency_sha = _environment_wrapper_tuple(before_components["environment"], budget, old["provenance"])
    if (set(before_tuples.values()) != {before_wrapper}
            or before_components["dependencies"].sha256 != before_dependency_sha):
        raise E("COMPARISON_EVIDENCE", "before suite metadata differs from retained inventory", "UNMET")
    _after_identity_matches_suites(after_components, manifest["provenance"], current_tuples,
                                  _suite_argv(manifest, current_tuples), budget)
    transition = _artifact(binding["transition_artifact"], budget)
    record = _closed(obj(transition.data),
                     {"schema", "run", "repository", "before_baseline_sha256", "before_identity_sha256", "after_identity_sha256", "ledger_sha256", "changes"},
                     {"schema", "run", "repository", "before_baseline_sha256", "before_identity_sha256", "after_identity_sha256", "ledger_sha256", "changes"}, "UPGRADE_TRANSITION")
    if (record["schema"] != "ffs.upgrade-transition/v1" or record["run"] != binding["run"]
            or record["repository"] != binding["repository"] or record["before_baseline_sha256"] != baseline_sha256
            or record["before_identity_sha256"] != before.sha256 or record["after_identity_sha256"] != after.sha256
            or record["ledger_sha256"] != ledger_sha256 or not isinstance(record["changes"], list)):
        raise E("UPGRADE_TRANSITION", "transition does not bind the exact comparison artifacts")
    expected = {component for component in UPGRADE_COMPONENTS
                if before_components[component].sha256 != after_components[component].sha256}
    seen: set[str] = set()
    for raw in record["changes"]:
        change = _closed(raw, {"component", "before_sha256", "after_sha256", "evidence"},
                         {"component", "before_sha256", "after_sha256", "evidence"}, "UPGRADE_TRANSITION")
        component = change["component"]
        if (not isinstance(component, str) or component not in UPGRADE_COMPONENTS or component in seen
                or not isinstance(change["evidence"], list) or not change["evidence"]):
            raise E("UPGRADE_TRANSITION", "transition changes must be unique and evidenced")
        seen.add(component)
        if (change["before_sha256"] != before_components[component].sha256
                or change["after_sha256"] != after_components[component].sha256):
            raise E("UPGRADE_TRANSITION", "transition component digest is incorrect")
        for descriptor in change["evidence"]:
            _artifact(descriptor, budget)
    if seen != expected:
        raise E("UPGRADE_TRANSITION", "transition does not explain exactly every tuple change")
    return h(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _complete_suite_metadata(value: Any, suite_ids: list[str]) -> bool:
    if not isinstance(value, list) or len(value) != len(suite_ids):
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        for key in ("environment", "dependencies", "config_sha256"):
            observed = item.get(key)
            if not isinstance(observed, str) or not observed or observed == "unavailable":
                return False
    return True


LEDGER_FIELDS = {"target", "old_version", "new_version", "source", "manager", "command", "rollback", "backup", "runtime", "recovery", "incompatible", "evidence"}


def _ledger(value: Any, budget: Budget, candidate_sha256: str) -> tuple[list[dict[str, Any]], bool, dict[str, dict[str, Any]]]:
    value = _closed(value, {"entries"}, {"schema", "complete", "entries", "profile_progression", "baseline_exceptions"}, "LEDGER")
    if not isinstance(value["entries"], list):
        raise E("LEDGER", "ledger entries must be an array", "UNMET")
    entries = []
    for raw in value["entries"]:
        entry = _closed(raw, LEDGER_FIELDS, LEDGER_FIELDS, "LEDGER_ENTRY")
        for key in ("target", "old_version", "new_version", "source", "manager"):
            _string(entry[key], f"ledger.{key}")
        _strings(entry["command"], "ledger.command", True)
        if not isinstance(entry["incompatible"], bool) or any(not isinstance(entry[x], dict) or not entry[x] for x in ("rollback", "backup", "runtime", "recovery")):
            raise E("LEDGER", "ledger rollback/runtime/recovery evidence is incomplete", "UNMET")
        if not isinstance(entry["evidence"], list) or not entry["evidence"]:
            raise E("LEDGER", "ledger referenced evidence is required", "UNMET")
        for descriptor in entry["evidence"]:
            observed = obj(_artifact(descriptor, budget).data)
            compared = LEDGER_FIELDS - {"evidence"}
            if not isinstance(observed, dict) or any(observed.get(field) != entry[field] for field in compared):
                raise E("LEDGER", "ledger evidence does not bind its target", "UNMET")
        entries.append(entry)
    progression = value.get("profile_progression")
    if progression is not None:
        if not isinstance(progression, list) or len(progression) != 3 or [x.get("kind") if isinstance(x, dict) else None for x in progression] != ["profile", "canary", "profile"] or progression[1].get("status") != "PASS":
            raise E("LEDGER", "profile progression requires a passing first canary", "UNMET")
        for step in progression:
            step = _closed(step, {"kind", "status", "evidence"}, {"kind", "status", "evidence"}, "PROFILE_PROGRESSION")
            if step["status"] not in {"PASS", "FAIL"}:
                raise E("PROFILE_PROGRESSION", "profile/canary status is invalid", "UNMET")
            record = obj(_artifact(step["evidence"], budget).data)
            if not isinstance(record, dict) or record != {"kind": step["kind"], "status": step["status"], "candidate_sha256": candidate_sha256}:
                raise E("PROFILE_PROGRESSION", "profile/canary evidence is not byte-bound", "UNMET")
    exceptions_raw = value.get("baseline_exceptions", [])
    if not isinstance(exceptions_raw, list):
        raise E("LEDGER", "baseline exceptions must be an array", "UNMET")
    exceptions: dict[str, dict[str, Any]] = {}
    exception_fields = {"test", "candidate_sha256", "owner", "evidence"}
    for raw in exceptions_raw:
        exception = _closed(raw, exception_fields, exception_fields, "BASELINE_EXCEPTION")
        name = _string(exception["test"], "baseline_exception.test")
        _string(exception["owner"], "baseline_exception.owner")
        if name in exceptions or exception["candidate_sha256"] != candidate_sha256:
            raise E("BASELINE_EXCEPTION", "baseline exception identity or candidate binding is invalid", "UNMET")
        proof = _artifact(exception["evidence"], budget)
        observed = obj(proof.data)
        if not isinstance(observed, dict) or observed.get("test") != name or observed.get("candidate_sha256") != candidate_sha256 or observed.get("owner") != exception["owner"]:
            raise E("BASELINE_EXCEPTION", "baseline exception evidence is not attributable", "UNMET")
        exceptions[name] = exception
    return entries, value.get("complete") is True and bool(entries), exceptions


def upgrade_mode(value: Any, baseline_path: str | Path) -> dict[str, Any]:
    manifest, _, budget = _common(value, "upgrade")
    baseline_item = read_checked(baseline_path, budget)
    old = _baseline(obj(baseline_item.data))
    if old["binding"].get("run") != manifest["binding"].get("run"):
        raise E("COMPARISON_BINDING", "baseline and current evidence have different logical runs")
    ledger_item = _artifact(manifest["ledger_artifact"], budget)
    entries, ledger_complete, exceptions = _ledger(obj(ledger_item.data), budget, manifest["candidate"]["sha256"])
    current = manifest["current"]
    if not isinstance(current, dict) or set(current) != {"suite_artifacts"}:
        raise E("CURRENT", "current suite artifacts are required", "UNMET")
    rows, artifacts, ids, observations = _suite_rows(current["suite_artifacts"], budget)
    comparison = compare_baselines({"completed": True, "tests": old["tests"]}, {"completed": True, "tests": rows})
    if comparison["status"] == "UNMET":
        raise E("COMPARISON", "observations cannot be compared", "UNMET")
    old_suite_ids = old.get("suite_ids")
    old_tuples = _suite_metadata(old.get("suite_observations"), old_suite_ids)
    current_tuples = _suite_metadata(observations, ids)
    drift = old.get("provenance") != manifest["provenance"]
    comparison_binding = manifest.get("comparison_binding")
    if comparison_binding is None and (drift or old_tuples != current_tuples):
        raise E("COMPARISON_BINDING", "a changed provenance or suite tuple requires an explicit comparison binding")
    for tuple_value in [*old_tuples.values(), *current_tuples.values()]:
        _complete_tuple(*tuple_value)
    current_argv = _suite_argv(manifest, current_tuples)
    for suite_id, tuple_value in current_tuples.items():
        _current_python_identity(tuple_value[0], current_argv[suite_id], budget)
    missing_suites = sorted(set(old_tuples) - set(current_tuples))
    new_suites = sorted(set(current_tuples) - set(old_tuples))
    regressions = bool(comparison["new_failures"] or comparison["missing_tests"] or missing_suites or new_suites)
    # Historical same-tuple envelopes remain readable for compatibility. Any
    # newly attributed comparison must prove the complete retained baseline.
    if comparison_binding is not None and (old.get("full_baseline_complete") is not True
            or old.get("full_baseline_unmet") != [] or not isinstance(old.get("full_inventory_evidence"), dict)
            or not {"source_runtime", "environment"} <= set(old["full_inventory_evidence"])):
        raise E("COMPARISON_EVIDENCE", "an attributed comparison requires complete historical source/runtime/environment observations", "UNMET")
    binding_sha256 = None
    if comparison_binding is not None:
        binding_sha256 = _comparison_binding(comparison_binding, budget, old, manifest,
                                             baseline_item.sha256, ledger_item.sha256, old_tuples, current_tuples)
    retained = {name for name in comparison["remaining_failures"] if old["tests"].get(name) == "FAIL"}
    if regressions:
        status = "FAIL"
    elif not ledger_complete or set(exceptions) != retained:
        raise E("COMPARISON_EVIDENCE", "comparison provenance or ledger is incomplete", "UNMET")
    else:
        status = "PASS"
    output = envelope("upgrade-comparison", "comparison", status, manifest, vec(), [])
    output.update(comparison)
    output.update({"status": status, "exit_status": 0 if status == "PASS" else 1, "comparison_passed": status == "PASS",
        "baseline_sha256": baseline_item.sha256, "ledger_sha256": ledger_item.sha256, "ledger_entries": entries,
        "suite_ids": ids, "missing_suites": missing_suites, "new_suites": new_suites,
        "suite_observations": observations, "artifacts": artifacts, "provenance_drift": drift,
        "before_provenance": old["provenance"], "after_provenance": manifest["provenance"],
        "comparison_binding_sha256": binding_sha256,
        "host": _local_host(), "model": "parallel-host-verifier"})
    return output


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("zero-length write")
        view = view[written:]


def save(path_value: str | Path, data: bytes) -> None:
    # macOS exposes the temporary root through both /var (a system alias) and
    # /private/var. Canonicalize only the parent alias: the final component
    # stays literal so an existing or dangling link is refused as the selected
    # create-only destination rather than followed to a different path.
    requested = Path(path_value)
    path = Path(os.path.realpath(requested.parent)) / requested.name
    if _protected(path):
        raise E("OUTPUT_PATH", "output cannot reside inside a repository or workspace")
    parent, temp_fd = -1, -1
    linked = False
    published_name = ""
    temp_name = f".ffs-{os.getpid()}-{hashlib.sha256(os.urandom(16)).hexdigest()[:12]}"
    try:
        parent, name = _parent_fd(path, True)
        info = os.fstat(parent)
        if _protected(_descriptor_path(parent)):
            raise E("OUTPUT_PATH", "physical output parent is inside a repository or workspace")
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise E("OUTPUT_PATH", "output parent must be owner-controlled mode 0700")
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise E("OUTPUT_PATH", "output is create-only and already exists")
        temp_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent)
        _write_all(temp_fd, data)
        os.fsync(temp_fd)
        if os.fstat(temp_fd).st_size != len(data):
            raise E("OUTPUT_WRITE", "persisted size mismatch")
        os.close(temp_fd)
        temp_fd = -1
        os.link(temp_name, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        linked = True
        published_name = name
        published = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
        try:
            chunks = []
            while sum(map(len, chunks)) < len(data):
                chunk = os.read(published, len(data) - sum(map(len, chunks)))
                if not chunk:
                    break
                chunks.append(chunk)
            if b"".join(chunks) != data:
                raise E("OUTPUT_WRITE", "persisted digest mismatch")
        finally:
            os.close(published)
        os.fsync(parent)
        linked = False
    except E:
        raise
    except OSError as exc:
        raise E("OUTPUT_WRITE", "atomic output publication failed") from exc
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        if parent >= 0:
            if linked and published_name:
                try:
                    os.unlink(published_name, dir_fd=parent)
                except OSError:
                    pass
            try:
                os.unlink(temp_name, dir_fd=parent)
            except OSError:
                pass
            os.close(parent)


def _regular(path: Path, code: str = "UNSAFE_INPUT") -> os.stat_result:
    try:
        value = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise E(code, f"required path is unavailable: {path}") from exc
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise E(code, f"regular single-link file is required: {path}")
    return value


def _tree(root: Path) -> dict[str, tuple[str, str | None]]:
    """Hash a no-follow tree.  Links and hard links are never fixture data."""
    result: dict[str, tuple[str, str | None]] = {".": ("directory", None)}
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in list(dirs) + list(files):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            value = path.stat(follow_symlinks=False)
            if stat.S_ISLNK(value.st_mode):
                if relative == "bin/python3" and sys.platform == "darwin" and path.resolve() == Path(_native_python()):
                    result[relative] = ("runtime-link", hashlib.sha256(str(path.resolve()).encode()).hexdigest())
                    continue
                raise E("FIXTURE_LINK", f"fixture contains a symlink: {relative}")
            if stat.S_ISDIR(value.st_mode):
                result[relative] = ("directory", None)
            elif stat.S_ISREG(value.st_mode):
                if value.st_nlink != 1:
                    raise E("FIXTURE_HARDLINK", f"fixture contains a hard link: {relative}")
                if value.st_size > MAX_INPUT_BYTES:
                    raise E("FIXTURE_SIZE", f"fixture file exceeds scanner limit: {relative}")
                with path.open("rb") as handle:
                    result[relative] = ("file", hashlib.sha256(handle.read()).hexdigest())
            else:
                raise E("FIXTURE_SPECIAL", f"fixture contains an unsafe entry: {relative}")
    return result


def _private_path(value: Any, fixture: Path, name: str) -> Path:
    if not isinstance(value, str) or not os.path.isabs(value):
        raise E("FIXTURE_PATH", f"fixture.{name} must be absolute")
    if any(component in {"", ".", ".."} for component in value.split(os.sep)[1:]) or os.path.normpath(value) != value:
        raise E("FIXTURE_PATH", f"fixture.{name} must be a normalized absolute path")
    fixture = Path(os.path.realpath(fixture))
    path = Path(value)
    resolved = Path(os.path.realpath(path))
    if resolved != path:
        raise E("FIXTURE_LINK", f"fixture.{name} must not traverse a symlink")
    if not _inside(resolved, fixture):
        raise E("FIXTURE_PATH", f"fixture.{name} must be strictly beneath fixture.root")
    current = fixture
    for component in resolved.relative_to(fixture).parts:
        current /= component
        try:
            info = current.stat(follow_symlinks=False)
        except FileNotFoundError:
            break
        except OSError as exc:
            raise E("FIXTURE_PATH", f"fixture.{name} cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode):
            raise E("FIXTURE_LINK", f"fixture.{name} must not traverse a symlink")
        if current != resolved and not stat.S_ISDIR(info.st_mode):
            raise E("FIXTURE_PATH", f"fixture.{name} ancestor must be a directory")
    return resolved


def _authority_installation(manifest: dict[str, Any], installation: dict[str, Any], fixture: Path) -> dict[str, Any]:
    locator = os.environ.get("FFS_VERIFICATION_AUTHORITY")
    if not locator:
        raise E("INSTALLATION_AUTHORITY", "private fixture requires supervisor authority", "UNMET")
    authority = _closed(_read_authority_file(locator, Budget(), "INSTALLATION_AUTHORITY"),
                        {"schema", "run", "candidate_sha256", "assignments"},
                        {"schema", "run", "candidate_sha256", "producer", "assignments", "adapters", "installations"},
                        "INSTALLATION_AUTHORITY")
    if authority.get("schema") != AUTHORITY_SCHEMA:
        raise E("INSTALLATION_AUTHORITY", "supervisor authority schema is invalid")
    if authority.get("run") != manifest["binding"]["run"] or authority.get("candidate_sha256") != manifest["candidate"]["sha256"]:
        raise E("INSTALLATION_AUTHORITY", "supervisor authority is not bound to this run/candidate")
    if not isinstance(authority.get("assignments"), list):
        raise E("INSTALLATION_AUTHORITY", "supervisor authority assignments must be an array")
    entries = authority.get("installations")
    if not isinstance(entries, list):
        raise E("INSTALLATION_AUTHORITY", "supervisor authority has no installation fixture", "UNMET")
    identity = fixture.stat(follow_symlinks=False)
    selected = []
    for grant in entries:
        if isinstance(grant, dict) and (grant.get("fixture_root") == str(fixture)
                                        and grant.get("device") == identity.st_dev
                                        and grant.get("inode") == identity.st_ino):
            selected.append(grant)
    if len(selected) > 1:
        raise E("INSTALLATION_AUTHORITY", "multiple supervisor grants match this fixture")
    if selected:
        grant = _closed(selected[0],
                        {"fixture_root", "fixture", "device", "inode", "setup_argv", "stub_artifact", "initial_entries"},
                        {"fixture_root", "fixture", "device", "inode", "setup_argv", "stub_artifact", "nested_stub_artifact", "initial_entries"},
                        "INSTALLATION_AUTHORITY")
        if (grant.get("fixture") != installation.get("fixture") or grant.get("setup_argv") != installation.get("setup_argv")
                or grant.get("stub_artifact") != installation.get("stub_artifact")
                or grant.get("nested_stub_artifact") != installation.get("nested_stub_artifact")):
            raise E("INSTALLATION_AUTHORITY", "supervisor grant does not match installation inputs")
        initial = grant.get("initial_entries")
        if not isinstance(initial, list):
            raise E("INSTALLATION_AUTHORITY", "fixture grant lacks initial entries")
        expected: dict[str, tuple[str, str | None]] = {".": ("directory", None)}
        for entry in initial:
            if not isinstance(entry, dict) or set(entry) - {"path", "type", "sha256"}:
                raise E("INSTALLATION_AUTHORITY", "fixture grant entries are malformed")
            relative, kind = entry.get("path"), entry.get("type")
            if not isinstance(relative, str) or relative in {"", "."} or Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise E("INSTALLATION_AUTHORITY", "fixture grant entry path is unsafe")
            if kind not in {"file", "directory"}:
                raise E("INSTALLATION_AUTHORITY", "fixture grant entry type is invalid")
            digest = entry.get("sha256") if kind == "file" else None
            if kind == "file" and (not isinstance(digest, str) or not HASH_RE.fullmatch(digest)):
                raise E("INSTALLATION_AUTHORITY", "fixture grant file hash is invalid")
            if kind == "directory" and "sha256" in entry:
                raise E("INSTALLATION_AUTHORITY", "fixture grant directories cannot have a hash")
            expected[relative] = (kind, digest)
        if _tree(fixture) != expected:
            raise E("FIXTURE_OWNERSHIP", "fixture inputs differ from supervisor grant")
        return expected
    raise E("INSTALLATION_AUTHORITY", "no supervisor grant matches this fixture", "UNMET")


def _stage_install_source(candidate: Checked, fixture: Path) -> tuple[Path, Path]:
    source_root = Path(candidate.locator).parent
    staged = fixture / "selected-source"
    if staged.exists():
        raise E("FIXTURE_OWNERSHIP", "staging destination already exists")
    staged.mkdir(mode=0o700)
    # This deliberately small source closure covers setup's local imports and
    # its optional shell helpers without copying a checkout or node runtime.
    for relative in ("setup.sh", "lib", "scripts/gsd", "skills", "patches", "data/installer", "package.json",
                     "package-lock.json", "node_modules/@opengsd/gsd-core/package.json"):
        origin, destination = source_root / relative, staged / relative
        if not origin.exists():
            if relative in {"skills", "scripts/gsd"}:
                continue
            raise E("SOURCE_STAGE", f"selected source input is missing: {relative}")
        if origin.is_symlink():
            raise E("SOURCE_STAGE", f"selected source input is a symlink: {relative}")
        if origin.is_dir():
            shutil.copytree(origin, destination, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, destination, follow_symlinks=False)
    if hashlib.sha256(read_checked(staged / "setup.sh").data).hexdigest() != candidate.sha256:
        raise E("SOURCE_STAGE", "staged setup bytes do not match selected candidate")
    # The upstream metadata is observed even though hermetic verification does
    # not execute node/npm.
    try:
        package = obj(read_checked(staged / "package.json").data)
        installed = obj(read_checked(staged / "node_modules/@opengsd/gsd-core/package.json").data)
    except E:
        raise E("SOURCE_STAGE", "selected GSD package metadata is unavailable")
    if package.get("devDependencies", {}).get("@opengsd/gsd-core") != "1.14.0" or installed.get("version") != "1.14.0":
        raise E("SOURCE_STAGE", "selected GSD package metadata is not pinned to 1.14.0")
    return staged, staged / "setup.sh"


def _sandbox_profile(fixture: Path, staged: Path, denied_read: Path | None = None) -> str:
    escaped = str(fixture).replace('\\', '\\\\').replace('"', '\\"')
    # system.sb supplies the normal executable/read surface.  A scoped write
    # allow follows the default deny. The per-invocation probe covers the
    # outside-write boundary; broader capability probes are tracked separately.
    return """(version 1)
(deny default)
(import \"system.sb\")
(allow file-read*)
(allow file-write* (subpath \"%s\"))
(deny file-write* (subpath \"%s\"))
(allow process-exec)
(allow process-exec (literal "/bin/bash"))
(allow process-fork)
(allow sysctl-read)
(allow mach-lookup)
(deny mach-lookup)
(deny network*)
(deny signal)
(deny syscall-unix (syscall-number SYS_setsid SYS_setpgid SYS_posix_spawn))
""" % (escaped, str(staged).replace('\\', '\\\\').replace('"', '\\"')) + (
        '(deny file-read* (literal "%s"))\n' % str(denied_read).replace('\\', '\\\\').replace('"', '\\"')
        if denied_read is not None else "")


def _native_python() -> str:
    executable = Path(sys.executable).resolve()
    candidates = [Path(sys.base_prefix) / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"]
    candidates.append(executable)
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    raise E("CONFINEMENT_UNAVAILABLE", "native Python executable is unavailable", "UNMET")


def _sandbox_command(fixture: Path, argv: list[str], profile: Path | None = None, staged: Path | None = None,
                     readonly_probe_parent: Path | None = None) -> list[str]:
    # The manifest is validated by _run_private, but keep the confinement
    # launcher verifier-owned when this helper is used directly as well.
    launch_argv = ["/bin/bash", *argv[1:]] if argv and argv[0] in {"bash", "/bin/bash"} else argv
    if sys.platform == "darwin":
        if not shutil.which("sandbox-exec") or profile is None:
            raise E("CONFINEMENT_UNAVAILABLE", "sandbox-exec is unavailable", "UNMET")
        return ["/usr/bin/sandbox-exec", "-f", str(profile), *launch_argv]
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise E("CONFINEMENT_UNAVAILABLE", "bubblewrap is unavailable", "UNMET")
    command = [bwrap, "--die-with-parent", "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-net", "--new-session", "--cap-drop", "ALL", "--proc", "/proc", "--dev", "/dev", "--bind", str(fixture), str(fixture), "--chdir", str(fixture)]
    for root in ("/usr", "/bin", "/lib", "/lib64", "/etc"):
        if Path(root).exists():
            command += ["--ro-bind", root, root]
    if staged:
        command += ["--ro-bind", str(staged), str(staged)]
    if readonly_probe_parent:
        command += ["--ro-bind", str(readonly_probe_parent), str(readonly_probe_parent)]
    # bwrap creates mount ancestors unless its root is remounted read-only.
    # Rebind only the supervisor-authorized fixture after that remount; staged
    # source is overmounted read-only below it.
    command += ["--remount-ro", "/", "--bind", str(fixture), str(fixture)]
    if staged:
        command += ["--ro-bind", str(staged), str(staged)]
    return command + launch_argv


def _owned_descendants(root_pid: int) -> set[int]:
    """Return the still-parented descendants of the task-owned launcher."""
    try:
        rows = subprocess.check_output(["/bin/ps", "-axo", "pid=,ppid="], text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return set()
    parents: dict[int, set[int]] = {}
    for row in rows.splitlines():
        try:
            pid, parent = (int(item) for item in row.split())
        except ValueError:
            continue
        parents.setdefault(parent, set()).add(pid)
    found, pending = set(), [root_pid]
    while pending:
        parent = pending.pop()
        for child in parents.get(parent, set()):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def _run_private(manifest: dict[str, Any], candidate: Checked, installation: dict[str, Any]) -> dict[str, Any]:
    installation = _closed(installation,
                           {"setup_argv", "fixture", "env", "stub_artifact", "timeout_seconds"},
                           {"setup_argv", "fixture", "env", "stub_artifact", "nested_stub_artifact", "timeout_seconds"},
                           "INSTALLATION")
    fixture_data = _closed(installation.get("fixture"), {"root", "home", "codex_home", "cache", "state", "project"}, {"root", "home", "codex_home", "cache", "state", "project"}, "FIXTURE")
    root_value = fixture_data["root"]
    if not isinstance(root_value, str) or not os.path.isabs(root_value):
        raise E("FIXTURE_PATH", "fixture.root must be absolute")
    if any(component in {"", ".", ".."} for component in root_value.split(os.sep)[1:]) or os.path.normpath(root_value) != root_value:
        raise E("FIXTURE_PATH", "fixture.root must be a normalized absolute path")
    fixture = Path(os.path.realpath(root_value))
    if fixture != Path(root_value):
        raise E("FIXTURE_LINK", "fixture.root must not traverse a symlink")
    root_stat = fixture.stat(follow_symlinks=False) if fixture.exists() else None
    if root_stat is None or not stat.S_ISDIR(root_stat.st_mode) or fixture.is_symlink() or fixture.parent == fixture:
        raise E("FIXTURE_PATH", "fixture.root must be an existing real directory")
    setup_argv = installation.get("setup_argv")
    if (not isinstance(setup_argv, list) or len(setup_argv) < 2
            or any(not isinstance(item, str) or not item for item in setup_argv)):
        raise E("INSTALL_ARGV", "setup_argv must be a nonempty string argv selecting setup.sh")
    if setup_argv[0] != "/bin/bash" or setup_argv[1] != candidate.locator or setup_argv.count(candidate.locator) != 1:
        raise E("INSTALL_ARGV", "setup_argv must select the candidate exactly once at index 1 behind bash")
    # Parent-authorized roots may be under /private/tmp on macOS or a task
    # supervisor directory; never permit active roots or their descendants.
    active = [Path(os.path.realpath(os.environ[key])) for key in ("HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME") if os.environ.get(key)]
    private_paths = {key: _private_path(fixture_data[key], fixture, key)
                     for key in ("home", "codex_home", "cache", "state", "project")}
    for path in [fixture, *private_paths.values()]:
        if any(path == item or _inside(path, item) or _inside(item, path) for item in active):
            raise E("FIXTURE_PROTECTED", "fixture overlaps an active profile root")
    _authority_installation(manifest, installation, fixture)
    env_data = _closed(installation.get("env"), {"FFS_SKIP_PROMPT_MASTER", "FFS_SKIP_SOCRATIC", "FFS_GSD_INSTALLER"}, {"FFS_SKIP_PROMPT_MASTER", "FFS_SKIP_SOCRATIC", "FFS_GSD_INSTALLER", "FFS_GSD_STUB_LOG", "FFS_GSD_STUB_FAIL_RUNTIME", "FFS_GSD_STUB_CORRUPT_ON_FAILURE"}, "INSTALL_ENV")
    if env_data.get("FFS_SKIP_PROMPT_MASTER") != "1" or env_data.get("FFS_SKIP_SOCRATIC") != "1":
        raise E("INSTALL_ENV", "private hermetic installation requires prompt/socratic skips")
    stub_budget = Budget()
    stub = _artifact(installation.get("stub_artifact"), stub_budget)
    stub_path = Path(stub.locator)
    first_party_stub = Path(candidate.locator).parent / "tests/fixtures/gsd-installer-stub.py"
    if not _inside(stub_path, fixture) and stub_path != first_party_stub:
        raise E("INSTALL_STUB", "hermetic stub must be fixture-contained or the verified first-party stub")
    if env_data.get("FFS_GSD_INSTALLER") != str(stub_path):
        raise E("INSTALL_STUB", "FFS_GSD_INSTALLER must match verified stub artifact")
    nested_descriptor = installation.get("nested_stub_artifact")
    nested_stub = None
    if nested_descriptor is not None:
        nested_stub = _artifact(nested_descriptor, stub_budget)
        if Path(nested_stub.locator) != first_party_stub:
            raise E("INSTALL_STUB", "nested stub artifact must bind the selected source's first-party stub")
    timeout = installation.get("timeout_seconds")
    if not isinstance(timeout, int) or not 1 <= timeout <= 60:
        raise E("INSTALL_TIMEOUT", "timeout_seconds must be between 1 and 60")
    staged, staged_setup = _stage_install_source(candidate, fixture)
    staged_stub = fixture / "verified-upstream-stub.py"
    if not _inside(stub_path, fixture):
        shutil.copy2(stub_path, staged_stub, follow_symlinks=False)
        if hashlib.sha256(read_checked(staged_stub).data).hexdigest() != stub.sha256:
            raise E("INSTALL_STUB", "staged first-party stub bytes changed")
    else:
        staged_stub = stub_path
    staged_first_party = None
    if nested_stub is not None:
        staged_first_party = fixture / "verified-first-party-stub.py"
        if staged_first_party.exists():
            raise E("FIXTURE_OWNERSHIP", "nested stub staging destination already exists")
        shutil.copy2(first_party_stub, staged_first_party, follow_symlinks=False)
        if hashlib.sha256(read_checked(staged_first_party).data).hexdigest() != nested_stub.sha256:
            raise E("INSTALL_STUB", "staged nested stub bytes changed")
    # Every fixture stub is projected through a verifier-owned interpreter
    # wrapper.  This permits Python fixture snippets without a shebang and
    # rewrites only their declared first-party upstream boundary to the staged,
    # byte-verified copy; the nested upstream installer is still a real child.
    if True:
        runner = fixture / "stub-native-runner.py"
        shebang = _native_python() if sys.platform == "darwin" else "/usr/bin/env python3"
        runner.write_text("#!" + shebang + "\nimport runpy,subprocess,sys\n" + ("sys.executable=" + repr(_native_python()) + "\n" if sys.platform == "darwin" else "") + "_target=" + repr(str(staged_stub)) + "\n_staged_first_party=" + repr(str(staged_first_party) if staged_first_party else None) + "\n_original_first_party=" + repr(str(first_party_stub) if first_party_stub.is_file() else None) + "\n_oldrun=subprocess.run\ndef _run(argv,*a,**k):\n value=list(argv)\n if _original_first_party and len(value)>1 and value[1]==_original_first_party:\n  if not _staged_first_party: raise RuntimeError('nested first-party stub is not byte-bound')\n  value[1]=_staged_first_party\n return _oldrun(value,*a,**k)\nsubprocess.run=_run\nsys.argv=[_target,*sys.argv[1:]]\nrunpy.run_path(sys.argv[0], run_name='__main__')\n")
        runner.chmod(0o700)
        staged_stub = runner
    rewritten = ["/bin/bash", str(staged_setup), *setup_argv[2:]]
    for path in private_paths.values():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    bin_dir = fixture / "bin"
    bin_dir.mkdir(mode=0o700, exist_ok=True)
    python_link = bin_dir / "python3"
    if sys.platform == "darwin":
        python_link.symlink_to(_native_python())
    child_env = {"PATH": str(bin_dir) + ":/usr/bin:/bin", "HOME": fixture_data["home"], "CODEX_HOME": fixture_data["codex_home"], "CLAUDE_CONFIG_DIR": str(Path(fixture_data["home"]) / ".claude"), "XDG_CONFIG_HOME": fixture_data["cache"], "XDG_CACHE_HOME": fixture_data["cache"], "XDG_STATE_HOME": fixture_data["state"], "TMPDIR": str(fixture / "tmp"), **{key: value for key, value in env_data.items() if isinstance(value, str)}}
    child_env["FFS_GSD_INSTALLER"] = str(staged_stub)
    (fixture / "tmp").mkdir(mode=0o700, exist_ok=True)
    if "FFS_GSD_STUB_LOG" in child_env and not _inside(Path(child_env["FFS_GSD_STUB_LOG"]), fixture):
        raise E("INSTALL_ENV", "stub log must be contained in fixture")
    profile = fixture / "seatbelt.sb"
    if sys.platform == "darwin":
        profile.write_text(_sandbox_profile(fixture, staged, first_party_stub.resolve()))
    # The probe path is exclusively verifier-created.  A predictable sibling
    # could be retained evidence from another attempt and must never be
    # overwritten merely to prove confinement.
    canary: Path | None = None
    target: Path | None = None
    probe = "import errno,pathlib,sys\np=pathlib.Path(sys.argv[1])\ntry: p.write_text('escape')\nexcept OSError as e: raise SystemExit(0 if e.errno in (errno.EACCES,errno.EPERM,errno.EROFS) else 98)\nraise SystemExit(97)\n"
    probe_python = _native_python() if sys.platform == "darwin" else "/usr/bin/python3"
    try:
        canary = Path(tempfile.mkdtemp(prefix=f".ffs-{os.getpid()}-canary-", dir=fixture.parent))
        target = canary / "sentinel"
        target.write_text("preserve\n")
        try:
            status, _, _ = _run(_sandbox_command(fixture, [probe_python, "-c", probe, str(target)], profile, staged,
                                                  canary if sys.platform != "darwin" else None), fixture, 10, child_env)
        except E as exc:
            raise E("CONFINEMENT_UNAVAILABLE", "sandbox outside-write denial probe did not complete", "UNMET") from exc
        if status != 0 or target.read_text() != "preserve\n":
            raise E("CONFINEMENT_UNAVAILABLE", "sandbox outside-write denial probe failed", "UNMET")
    except OSError as exc:
        raise E("CONFINEMENT_UNAVAILABLE", "sandbox outside-write denial probe is unavailable", "UNMET") from exc
    finally:
        if target is not None:
            try:
                target.unlink()
            except (FileNotFoundError, OSError):
                pass
        if canary is not None:
            try:
                canary.rmdir()
            except OSError:
                # A failed cleanup is retained for attribution; it is never an
                # excuse to touch another fixture-owned path.
                pass
    # Start Seatbelt beneath the framework executable.  Shell/Python launcher
    # wrappers use posix_spawn, which is deliberately denied for descendants;
    # execve preserves the boundary while allowing setup.sh's real exec path.
    sandbox_argv = ([_native_python(), "-c", "import os,sys; os.execve('/bin/bash', ['/bin/bash', *sys.argv[1:]], os.environ)", str(staged_setup), *rewritten[2:]]
                   if sys.platform == "darwin" else rewritten)
    if str(staged_setup) not in sandbox_argv:
        raise E("INSTALL_ARGV", "sandbox command does not execute the staged candidate")
    command = _sandbox_command(fixture, sandbox_argv, profile, staged)
    started = now()
    try:
        exit_status, stdout, stderr = _run(command, staged, timeout, child_env)
    except E as exc:
        # _run has already stopped/reaped the dedicated process group.  Scan
        # retained partial fixture bytes before reporting its bounded failure.
        _tree(fixture)
        code = "INSTALL_TIMEOUT" if exc.code == "ADAPTER_TIMEOUT" else "INSTALL_OUTPUT_LIMIT" if exc.code == "ADAPTER_OUTPUT_LIMIT" else "INSTALL_EXECUTION"
        raise E(code, exc.problem, exc.status) from exc
    finished = now()
    # The sandbox's private PID namespace/Seatbelt escape denial is the actual
    # cleanup boundary; process-group reaping handles its direct launcher.
    # Attribute a managed-runtime alias as a runtime escape before the generic
    # no-follow inventory reports it as a fixture link.
    for runtime, root in (("claude", Path(fixture_data["home"]) / ".claude"),
                          ("codex", Path(fixture_data["codex_home"]))):
        if (root / "gsd-core").is_symlink():
            raise E("RUNTIME_ESCAPE", f"{runtime} emitted runtime escapes the private fixture")
    scanned = _tree(fixture)
    if exit_status != 0:
        full_detail = stderr.decode("utf-8", "replace").strip()
        detail = full_detail[:512]
        if "symlink" in full_detail.lower():
            raise E("RUNTIME_ESCAPE", "installer attempted an escaped runtime symlink")
        raise E("INSTALL_EXIT", f"installer exited {exit_status}" + (f": {detail}" if detail else ""))
    runtime_roots = (("claude", Path(fixture_data["home"]) / ".claude"),
                     ("codex", Path(fixture_data["codex_home"])))
    for runtime, root in runtime_roots:
        manifest_path = root / "gsd-file-manifest.json"
        if not manifest_path.is_file() or (root / "gsd-core").is_symlink():
            raise E("RUNTIME_ESCAPE", f"{runtime} emitted runtime is missing or escaped")
        obj(read_checked(manifest_path).data)
    output = envelope(_gate("installation", "admission"), "admission", "PASS", manifest, vec(), [])
    output["invocation"] = {"argv": _safe_argv(command), "exit_status": exit_status, "started_utc": started, "completed_utc": finished, "stdout_sha256": hashlib.sha256(stdout).hexdigest(), "stderr_sha256": hashlib.sha256(stderr).hexdigest()}
    output["fixture"] = {"root": str(fixture), "inventory": [{"path": key, "type": value[0], "sha256": value[1]} for key, value in sorted(scanned.items())]}
    return output


def installation_mode(manifest: Any, mode: str) -> dict[str, Any]:
    checked, candidate, _ = _common(manifest, "installation")
    installation = checked["installation"]
    if not isinstance(installation, dict):
        raise E("INSTALLATION", "installation must be an object")
    if mode == "private":
        return _run_private(checked, candidate, installation)
    if mode != "lifecycle":
        raise E("INSTALL_MODE", "installation mode must be private or lifecycle")
    lifecycle = _closed(installation, {"lifecycle_rows", "protected_unchanged"},
                        {"lifecycle_rows", "protected_unchanged"}, "INSTALLATION_LIFECYCLE")
    rows = lifecycle["lifecycle_rows"]
    if not isinstance(rows, list):
        raise E("INSTALLATION_LIFECYCLE", "lifecycle rows must be an array")
    operations = {"fresh-install", "upgrade", "collision", "interruption", "rollback", "uninstall"}
    scopes = {"project", "user"}
    expected = {(operation, scope, platform) for operation in operations
                for scope in scopes for platform in PLATFORMS}
    seen: set[tuple[str, str, str]] = set()
    budget = Budget()
    for raw in rows:
        row = _closed(raw, {"operation", "scope", "platform", "status", "artifact"},
                      {"operation", "scope", "platform", "status", "artifact"},
                      "INSTALLATION_LIFECYCLE_ROW")
        key = (str(row["operation"]), str(row["scope"]), str(row["platform"]))
        if key not in expected or key in seen or row["status"] != "PASS":
            raise E("INSTALLATION_LIFECYCLE", "all 24 unique lifecycle rows must pass", "UNMET")
        _gate_artifact(row["artifact"], budget, "installation lifecycle")
        seen.add(key)
    if seen != expected or lifecycle["protected_unchanged"] is not True:
        raise E("INSTALLATION_LIFECYCLE", "complete protected 24-cell lifecycle evidence is required", "UNMET")
    return _pass(checked, "installation-lifecycle", "verification", row_count=len(rows),
                 platforms=sorted(PLATFORMS))


def _gate_artifact(value: Any, budget: Budget, name: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise E("EVIDENCE", f"{name} artifact descriptor is required", "UNMET")
    item = _artifact(value, budget)
    return {"locator": item.locator, "sha256": item.sha256}


def _pass(manifest: dict[str, Any], gate: str, purpose: str, **details: Any) -> dict[str, Any]:
    output = envelope(gate, purpose, "PASS", manifest, vec(), [])
    output.update(details)
    return output


def matrix_mode(value: Any, mode: str, repetitions: int) -> dict[str, Any]:
    if mode != "hermetic":
        raise E("MATRIX_MODE", "only matrix --mode hermetic is supported")
    if repetitions != 25:
        raise E("MATRIX_REPETITIONS", "matrix requires exactly 25 repetitions")
    manifest, _, budget = _common(value, "matrix")
    matrix = _closed(manifest["matrix"], {"required_case_ids", "rows"},
                     {"required_case_ids", "rows"}, "MATRIX_SCHEMA")
    required = set(_strings(matrix["required_case_ids"], "matrix.required_case_ids", True))
    rows = matrix["rows"]
    if not isinstance(rows, list) or not rows:
        raise E("MATRIX_ROWS", "matrix rows are required", "UNMET")
    seen: set[tuple[str, str]] = set()
    for raw in rows:
        row = _closed(raw, {"case_id", "platform", "repetitions", "status", "artifact"},
                      {"case_id", "platform", "repetitions", "status", "artifact"}, "MATRIX_ROW")
        case_id = _string(row["case_id"], "matrix.case_id")
        platform = _string(row["platform"], "matrix.platform")
        key = (case_id, platform)
        if platform not in PLATFORMS or key in seen:
            raise E("MATRIX_ROW", "matrix rows require unique darwin/linux case identities")
        if row["repetitions"] != repetitions or row["status"] != "PASS":
            raise E("MATRIX_INCOMPLETE", "every matrix row must pass all 25 repetitions", "UNMET")
        _gate_artifact(row["artifact"], budget, "matrix row")
        seen.add(key)
    expected = {(case_id, platform) for case_id in required for platform in PLATFORMS}
    if seen != expected:
        raise E("MATRIX_INCOMPLETE", "matrix is missing a required case/platform row", "UNMET")
    return _pass(manifest, "matrix", "verification", repetitions=repetitions,
                 platforms=sorted(PLATFORMS), case_count=len(required), row_count=len(rows))


def hosts_mode(value: Any, authenticated: bool, pairings: list[str],
               review_directions: list[str], soak_seconds: int) -> dict[str, Any]:
    if not authenticated:
        raise E("HOST_AUTHENTICATION", "authenticated host verification is required", "UNMET")
    if set(pairings) != PAIRINGS or set(review_directions) != REVIEW_DIRECTIONS:
        raise E("HOST_MATRIX", "all host pairings and both review directions are required")
    if soak_seconds != 600:
        raise E("HOST_SOAK", "host verification requires exactly 600 seconds of overlap")
    manifest, _, budget = _common(value, "hosts")
    hosts = _closed(manifest["hosts"], {"rows", "review_directions", "tier_rows"},
                    {"rows", "review_directions", "tier_rows"}, "HOSTS_SCHEMA")
    if set(_strings(hosts["review_directions"], "hosts.review_directions", True)) != REVIEW_DIRECTIONS:
        raise E("HOST_MATRIX", "host evidence is missing a review direction", "UNMET")
    rows = hosts["rows"]
    if not isinstance(rows, list):
        raise E("HOST_ROWS", "host rows must be an array")
    seen: set[tuple[str, str]] = set()
    for raw in rows:
        row = _closed(raw, {"pairing", "platform", "overlap_seconds", "productive", "authenticated", "status", "artifact"},
                      {"pairing", "platform", "overlap_seconds", "productive", "authenticated", "status", "artifact"}, "HOST_ROW")
        pairing = _string(row["pairing"], "hosts.pairing")
        platform = _string(row["platform"], "hosts.platform")
        key = (pairing, platform)
        if pairing not in PAIRINGS or platform not in PLATFORMS or key in seen:
            raise E("HOST_ROW", "host rows require unique pairing/platform identities")
        overlap = row["overlap_seconds"]
        if (isinstance(overlap, bool) or not isinstance(overlap, (int, float))
                or overlap < soak_seconds or row["productive"] is not True
                or row["authenticated"] is not True or row["status"] != "PASS"):
            raise E("HOST_SOAK", "each authenticated host row needs productive 600-second overlap", "UNMET")
        _gate_artifact(row["artifact"], budget, "host row")
        seen.add(key)
    expected = {(pairing, platform) for pairing in PAIRINGS for platform in PLATFORMS}
    if seen != expected:
        raise E("HOST_MATRIX", "six pairing/platform host rows are required", "UNMET")
    tier_rows = hosts["tier_rows"]
    if not isinstance(tier_rows, list):
        raise E("HOST_TIERS", "host tier rows must be an array")
    required_tiers = {
        ("codex", tier) for tier in ("astra", "sol", "terra", "luna")
    } | {("claude", tier) for tier in ("fable", "opus", "sonnet", "haiku")}
    observed_tiers: set[tuple[str, str]] = set()
    for raw in tier_rows:
        row = _closed(raw, {"host", "tier", "requested_model", "actual_model", "status", "artifact"},
                      {"host", "tier", "requested_model", "actual_model", "status", "artifact"},
                      "HOST_TIER_ROW")
        key = (_string(row["host"], "tier.host"), _string(row["tier"], "tier.tier"))
        requested_model = _string(row["requested_model"], "tier.requested_model")
        actual_model = _string(row["actual_model"], "tier.actual_model")
        if (key in observed_tiers or row["status"] != "PASS"
                or actual_model != requested_model):
            raise E("HOST_TIERS", "every exact host tier must pass once", "UNMET")
        _gate_artifact(row["artifact"], budget, "host tier")
        observed_tiers.add(key)
    if observed_tiers != required_tiers:
        raise E("HOST_TIERS", "all eight native tiers are required", "UNMET")
    output = _pass(manifest, "hosts", "authenticated-verification", authenticated=True,
                   pairings=sorted(PAIRINGS), review_directions=sorted(REVIEW_DIRECTIONS),
                   soak_seconds=soak_seconds, row_count=len(rows))
    output["host"] = "mixed"
    output["model"] = "exact-recorded"
    output["authenticated"] = True
    return output


def audit_mode(value: Any) -> dict[str, Any]:
    manifest, _, budget = _common(value, "audit")
    audit = _closed(manifest["audit"],
                    {"required_domains", "domains", "findings", "producer", "reviewer", "review_pin"},
                    {"required_domains", "domains", "findings", "producer", "reviewer", "review_pin"},
                    "AUDIT_SCHEMA")
    producer = _identity(audit["producer"], "audit.producer")
    reviewer = _identity(audit["reviewer"], "audit.reviewer")
    if producer == reviewer:
        raise E("AUDIT_INDEPENDENCE", "final audit reviewer must differ from the producer")
    required = set(_strings(audit["required_domains"], "audit.required_domains", True))
    domains = audit["domains"]
    if not isinstance(domains, list):
        raise E("AUDIT_DOMAINS", "audit domains must be an array")
    observed: set[str] = set()
    for raw in domains:
        row = _closed(raw, {"id", "status", "artifact"}, {"id", "status", "artifact"}, "AUDIT_DOMAIN")
        domain = _string(row["id"], "audit.domain")
        if domain in observed or row["status"] != "PASS":
            raise E("AUDIT_INCOMPLETE", "each required audit domain must pass once", "UNMET")
        _gate_artifact(row["artifact"], budget, "audit domain")
        observed.add(domain)
    if observed != required:
        raise E("AUDIT_INCOMPLETE", "audit domain coverage is incomplete", "UNMET")
    findings = audit["findings"]
    if not isinstance(findings, list):
        raise E("AUDIT_FINDINGS", "audit findings must be an array")
    finding_ids: set[str] = set()
    for raw in findings:
        finding = _closed(raw, {"id", "severity", "disposition", "owner", "evidence"},
                          {"id", "severity", "disposition", "owner", "evidence"}, "AUDIT_FINDING")
        finding_id = _string(finding["id"], "finding.id")
        severity = _string(finding["severity"], "finding.severity").lower()
        disposition = _string(finding["disposition"], "finding.disposition")
        if finding_id in finding_ids or severity not in {"critical", "high", "medium", "low"}:
            raise E("AUDIT_FINDINGS", "finding IDs and severities must be valid and unique")
        if severity in {"critical", "high"} and disposition not in {"resolved", "refuted"}:
            raise E("AUDIT_SEVERE_OPEN", "confirmed critical/high findings must be closed", "UNMET")
        _string(finding["owner"], "finding.owner")
        _gate_artifact(finding["evidence"], budget, "finding disposition")
        finding_ids.add(finding_id)
    _gate_artifact(audit["review_pin"], budget, "review pin")
    return _pass(manifest, "audit", "verification", domains=sorted(required),
                 finding_count=len(findings), reviewer=reviewer)


def _candidate_production_inventory(candidate: Checked) -> list[str]:
    closure = _closed(obj(candidate.data), {"schema", "files"}, {"schema", "files"},
                      "SOURCE_CLOSURE")
    if closure["schema"] != "ffs.source-closure/v1" or not isinstance(closure["files"], list):
        raise E("SOURCE_CLOSURE", "coverage candidate must be a sealed source closure", "UNMET")
    seen: set[str] = set()
    production: list[str] = []
    for raw in closure["files"]:
        row = _closed(raw, {"path", "type", "mode", "sha256"},
                      {"path", "type", "mode", "sha256", "link_target"},
                      "SOURCE_CLOSURE_FILE")
        path = _string(row["path"], "source_closure.path")
        if (path in seen or Path(path).is_absolute() or ".." in Path(path).parts
                or isinstance(row["mode"], bool) or not isinstance(row["mode"], int)
                or row["mode"] < 0 or row["mode"] > 0o7777
                or not isinstance(row["sha256"], str) or not HASH_RE.fullmatch(row["sha256"])):
            raise E("SOURCE_CLOSURE", "source closure file inventory is malformed", "UNMET")
        if (row["type"] == "file" and "link_target" in row) or (
                row["type"] == "symlink"
                and (not isinstance(row.get("link_target"), str) or not row["link_target"])
        ) or row["type"] not in {"file", "symlink"}:
            raise E("SOURCE_CLOSURE", "source closure file type is malformed", "UNMET")
        seen.add(path)
        if (row["type"] == "file" and path.startswith(("lib/", "scripts/", "skills/")) and path.endswith(".py")
                and not {"tests", "vendor", ".staging", "node_modules", "__pycache__"}.intersection(Path(path).parts)):
            production.append(path)
    if not production:
        raise E("SOURCE_CLOSURE", "source closure has no first-party Python inventory", "UNMET")
    return sorted(production)


def coverage_mode(value: Any, line_min: float) -> dict[str, Any]:
    if isinstance(line_min, bool) or not 0 <= line_min <= 100:
        raise E("COVERAGE_THRESHOLD", "line minimum must be between 0 and 100")
    manifest, candidate, budget = _common(value, "coverage")
    coverage = _closed(manifest["coverage"], {"xml", "execution"}, {"xml", "execution"},
                       "COVERAGE_SCHEMA")
    report = _artifact(coverage["xml"], budget)
    execution_item = _artifact(coverage["execution"], budget)
    inventory = _candidate_production_inventory(candidate)
    try:
        totals = _coverage_bytes(report.data, inventory)
    except ValueError as error:
        raise E("COVERAGE_INVALID", str(error)) from error
    execution = _closed(obj(execution_item.data),
                        {"schema", "source_sha256", "xml_sha256", "argv", "exit_status",
                         "started_utc", "completed_utc"},
                        {"schema", "source_sha256", "xml_sha256", "argv", "exit_status",
                         "started_utc", "completed_utc"}, "COVERAGE_EXECUTION")
    if (execution["schema"] != "ffs.coverage-execution/v1"
            or execution["source_sha256"] != candidate.sha256
            or execution["xml_sha256"] != report.sha256 or execution["exit_status"] != 0):
        raise E("COVERAGE_EXECUTION", "coverage execution does not bind candidate and XML bytes", "UNMET")
    _strings(execution["argv"], "coverage.execution.argv", True)
    _timestamps(execution["started_utc"], execution["completed_utc"], False)
    manifest = dict(manifest)
    manifest["artifacts"] = [
        {"locator": report.locator, "sha256": report.sha256},
        {"locator": execution_item.locator, "sha256": execution_item.sha256},
    ]
    if totals["line_percent"] < line_min:
        raise E("COVERAGE_BELOW_MINIMUM",
                f"line coverage {totals['line_percent']:.2f}% is below {line_min:.2f}%")
    return _pass(manifest, "coverage", "verification", line_min=line_min, coverage=totals)


def migration_mode(value: Any, mode: str) -> dict[str, Any]:
    if mode != "verify-legacy":
        raise E("MIGRATION_MODE", "only migration --mode verify-legacy is supported")
    manifest, _, budget = _common(value, "migration")
    migration = _closed(manifest["migration"],
        {"sources", "journal", "record_counts", "id_bindings", "epochs", "owner_fence",
         "interlock", "restart", "rollback", "legacy_unchanged"},
        {"sources", "journal", "record_counts", "id_bindings", "epochs", "owner_fence",
         "interlock", "restart", "rollback", "legacy_unchanged"},
        "MIGRATION_SCHEMA")
    sources = migration["sources"]
    if not isinstance(sources, list) or not sources:
        raise E("MIGRATION_SOURCES", "migration sources are required", "UNMET")
    for source in sources:
        _gate_artifact(source, budget, "legacy source")
    _gate_artifact(migration["journal"], budget, "migration journal")
    counts = _closed(migration["record_counts"], {"source", "imported", "quarantined"},
                     {"source", "imported", "quarantined"}, "MIGRATION_COUNTS")
    if any(isinstance(counts[key], bool) or not isinstance(counts[key], int) or counts[key] < 0
           for key in counts) or counts["source"] != counts["imported"] + counts["quarantined"]:
        raise E("MIGRATION_CONSERVATION", "every legacy record must be imported or quarantined")
    bindings = migration["id_bindings"]
    if not isinstance(bindings, list) or not bindings:
        raise E("MIGRATION_BINDINGS", "explicit legacy-to-canonical ID bindings are required", "UNMET")
    source_ids: set[str] = set()
    canonical_ids: set[str] = set()
    for raw in bindings:
        binding = _closed(raw, {"source_id", "source_run_id", "canonical_run_id", "evidence"},
                          {"source_id", "source_run_id", "canonical_run_id", "evidence"},
                          "MIGRATION_BINDING")
        source_id = _string(binding["source_id"], "binding.source_id")
        source_run_id = _string(binding["source_run_id"], "binding.source_run_id")
        canonical_run_id = _string(binding["canonical_run_id"], "binding.canonical_run_id")
        source_key = f"{source_id}\0{source_run_id}"
        if source_key in source_ids or canonical_run_id in canonical_ids:
            raise E("MIGRATION_BINDINGS", "migration ID bindings must be one-to-one")
        _gate_artifact(binding["evidence"], budget, "migration ID binding")
        source_ids.add(source_key)
        canonical_ids.add(canonical_run_id)
    epochs = migration["epochs"]
    if not isinstance(epochs, list) or not epochs:
        raise E("MIGRATION_EPOCH", "writer epoch evidence is required", "UNMET")
    run_ids: set[str] = set()
    for raw in epochs:
        epoch = _closed(raw, {"run_id", "epoch", "writer", "proof"},
                        {"run_id", "epoch", "writer", "proof"}, "MIGRATION_EPOCH")
        run_id = _string(epoch["run_id"], "epoch.run_id")
        if (run_id in run_ids or isinstance(epoch["epoch"], bool) or not isinstance(epoch["epoch"], int)
                or epoch["epoch"] < 1 or epoch["writer"] not in {"legacy", "new", "none"}):
            raise E("MIGRATION_EPOCH", "each run requires one valid writer epoch")
        _gate_artifact(epoch["proof"], budget, "writer epoch")
        run_ids.add(run_id)
    _gate_artifact(migration["owner_fence"], budget, "migration owner fence")
    _gate_artifact(migration["interlock"], budget, "migration interlock")
    for name in ("restart", "rollback"):
        row = _closed(migration[name], {"status", "artifact"}, {"status", "artifact"},
                      "MIGRATION_" + name.upper())
        if row["status"] != "PASS":
            raise E("MIGRATION_" + name.upper(), f"migration {name} proof is incomplete", "UNMET")
        _gate_artifact(row["artifact"], budget, f"migration {name}")
    if migration["legacy_unchanged"] is not True:
        raise E("MIGRATION_SOURCE_CHANGED", "legacy sources must remain unchanged")
    return _pass(manifest, "migration", "verify-legacy", source_count=len(sources),
                 record_counts=counts, run_count=len(run_ids))


def rollout_mode(value: Any, consumer: Path | None) -> dict[str, Any]:
    manifest, _, budget = _common(value, "rollout")
    rollout = _closed(manifest["rollout"],
        {"consumer", "surfaces", "forks", "canaries", "protected_unchanged", "rollback"},
        {"consumer", "surfaces", "forks", "canaries", "protected_unchanged", "rollback"},
        "ROLLOUT_SCHEMA")
    expected_consumer = Path(_string(rollout["consumer"], "rollout.consumer"))
    if not expected_consumer.is_absolute() or (consumer is not None and consumer.resolve() != expected_consumer.resolve()):
        raise E("ROLLOUT_CONSUMER", "rollout consumer identity does not match")
    surfaces = rollout["surfaces"]
    if not isinstance(surfaces, list) or not surfaces:
        raise E("ROLLOUT_SURFACES", "owned rollout surfaces are required", "UNMET")
    paths: set[str] = set()
    for raw in surfaces:
        row = _closed(raw, {"path", "owner", "source", "staged", "status"},
                      {"path", "owner", "source", "staged", "status"}, "ROLLOUT_SURFACE")
        path = _string(row["path"], "surface.path")
        source = _artifact(row["source"], budget)
        staged = _artifact(row["staged"], budget)
        if (path in paths or row["owner"] != "ffs" or row["status"] != "PASS"
                or source.sha256 != staged.sha256 or source.data != staged.data):
            raise E("ROLLOUT_SURFACE", "every FFS-owned surface must match canonical bytes")
        paths.add(path)
    forks = rollout["forks"]
    if not isinstance(forks, list):
        raise E("ROLLOUT_FORKS", "fork adjudications must be an array")
    fork_ids: set[str] = set()
    for raw in forks:
        row = _closed(raw, {"id", "disposition", "artifact"},
                      {"id", "disposition", "artifact"}, "ROLLOUT_FORK")
        fork_id = _string(row["id"], "fork.id")
        if fork_id in fork_ids or row["disposition"] not in {"ported", "retained-consumer", "not-present"}:
            raise E("ROLLOUT_FORKS", "every discovered fork needs one disposition")
        _gate_artifact(row["artifact"], budget, "fork adjudication")
        fork_ids.add(fork_id)
    canaries = rollout["canaries"]
    if not isinstance(canaries, list):
        raise E("ROLLOUT_CANARIES", "consumer canaries must be an array")
    seen: set[tuple[str, int]] = set()
    for raw in canaries:
        row = _closed(raw, {"platform", "round", "status", "productive_overlap_seconds", "artifact"},
                      {"platform", "round", "status", "productive_overlap_seconds", "artifact"}, "ROLLOUT_CANARY")
        key = (_string(row["platform"], "canary.platform"), row["round"])
        if (key[0] not in PLATFORMS or key[1] not in {1, 2} or key in seen
                or row["status"] != "PASS" or row["productive_overlap_seconds"] < 600):
            raise E("ROLLOUT_CANARIES", "two productive consumer canaries per platform are required", "UNMET")
        _gate_artifact(row["artifact"], budget, "consumer canary")
        seen.add(key)
    if seen != {(platform, round_) for platform in PLATFORMS for round_ in (1, 2)}:
        raise E("ROLLOUT_CANARIES", "four consumer canary rows are required", "UNMET")
    if rollout["protected_unchanged"] is not True:
        raise E("ROLLOUT_PROTECTED", "consumer-owned surfaces changed")
    _gate_artifact(rollout["rollback"], budget, "rollout rollback")
    return _pass(manifest, "rollout", "verification", consumer=str(expected_consumer),
                 surface_count=len(surfaces), fork_count=len(forks), canary_count=len(canaries))


def _aggregate_result(value: Any, budget: Budget) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise E("AGGREGATE_RESULT", "indexed result is not an object", "UNMET")
    gate = value.get("gate")
    if gate not in AGGREGATE_RESULT_FIELDS:
        raise E("AGGREGATE_RESULT", "indexed result has an unsupported gate", "UNMET")
    expected_fields = RESULT_COMMON_FIELDS | AGGREGATE_RESULT_FIELDS[gate]
    result = _closed(value, set(expected_fields), set(expected_fields), "AGGREGATE_RESULT")
    if (result["schema"] != SCHEMA or result["status"] != "PASS" or result["exit_status"] != 0
            or result["purpose"] != AGGREGATE_RESULT_PURPOSE[gate]
            or result["errors"] != [] or result["unmet_reasons"] != []):
        raise E("AGGREGATE_RESULT", "every indexed result must be a passing verifier envelope", "UNMET")
    binding = _closed(result["binding"], {"run", "activity", "attempt"},
                      {"run", "activity", "attempt"}, "AGGREGATE_RESULT")
    for name, item in binding.items():
        _string(item, f"result.binding.{name}")
    for name in ("ac_ids", "path_ids", "int_ids"):
        _strings(result[name], f"result.{name}")
    if (not isinstance(result["authenticated"], bool)
            or not isinstance(result["command"], list) or not result["command"]
            or any(not isinstance(item, str) or not item for item in result["command"])):
        raise E("AGGREGATE_RESULT", "indexed result execution identity is malformed", "UNMET")
    for name in ("platform", "host", "model", "label"):
        _string(result[name], f"result.{name}")
    _timestamps(result["started_utc"], result["completed_utc"], False)
    vector = _closed(result["gate_vector"],
                     {"review_complete", "repair_authorized", "path_admitted", "rollout_ready"},
                     {"review_complete", "repair_authorized", "path_admitted", "rollout_ready"},
                     "AGGREGATE_RESULT")
    if any(not isinstance(vector[name], bool) or result[name] is not vector[name] for name in vector):
        raise E("AGGREGATE_RESULT", "indexed result gate vector is inconsistent", "UNMET")
    provenance = _closed(result["provenance"], PROVENANCE, PROVENANCE, "AGGREGATE_RESULT")
    if any(not isinstance(item, str) or not HASH_RE.fullmatch(item) for item in provenance.values()):
        raise E("AGGREGATE_RESULT", "indexed result provenance is malformed", "UNMET")
    if not isinstance(result["artifacts"], list):
        raise E("AGGREGATE_RESULT", "indexed result artifact inventory is malformed", "UNMET")
    for descriptor in result["artifacts"]:
        _artifact(descriptor, budget)
    for name in AGGREGATE_RESULT_FIELDS[gate]:
        if result[name] is None and not (gate == "upgrade-comparison" and name == "comparison_binding_sha256"):
            raise E("AGGREGATE_RESULT", "indexed result detail is incomplete", "UNMET")
    if gate == "audit":
        _strings(result["domains"], "result.domains", True)
        _identity(result["reviewer"], "result.reviewer")
        if isinstance(result["finding_count"], bool) or not isinstance(result["finding_count"], int) or result["finding_count"] < 0:
            raise E("AGGREGATE_RESULT", "audit result summary is malformed", "UNMET")
    elif gate == "coverage":
        coverage = _closed(result["coverage"],
                           {"lines_covered", "lines_valid", "branches_covered", "branches_valid",
                            "line_percent", "branch_percent", "branch_opportunities", "files"},
                           {"lines_covered", "lines_valid", "branches_covered", "branches_valid",
                            "line_percent", "branch_percent", "branch_opportunities", "files"},
                           "AGGREGATE_RESULT")
        minimum = result["line_min"]
        if (isinstance(minimum, bool) or not isinstance(minimum, (int, float))
                or not 0 <= minimum <= 100 or not isinstance(coverage["line_percent"], (int, float))
                or coverage["line_percent"] < minimum or not isinstance(coverage["files"], dict)
                or not coverage["files"]):
            raise E("AGGREGATE_RESULT", "coverage result summary is malformed", "UNMET")
    elif gate == "hosts":
        if (set(_strings(result["pairings"], "result.pairings", True)) != PAIRINGS
                or set(_strings(result["review_directions"], "result.review_directions", True)) != REVIEW_DIRECTIONS
                or result["soak_seconds"] != 600 or result["row_count"] != 6
                or result["authenticated"] is not True or result["host"] != "mixed"
                or result["model"] != "exact-recorded"):
            raise E("AGGREGATE_RESULT", "host result summary is incomplete", "UNMET")
    elif gate == "installation-lifecycle":
        if result["row_count"] != 24 or set(_strings(result["platforms"], "result.platforms", True)) != PLATFORMS:
            raise E("AGGREGATE_RESULT", "installation result summary is incomplete", "UNMET")
    elif gate == "matrix":
        case_count, row_count = result["case_count"], result["row_count"]
        if (result["repetitions"] != 25 or set(_strings(result["platforms"], "result.platforms", True)) != PLATFORMS
                or isinstance(case_count, bool) or not isinstance(case_count, int) or case_count < 1
                or row_count != case_count * len(PLATFORMS)):
            raise E("AGGREGATE_RESULT", "matrix result summary is incomplete", "UNMET")
    elif gate == "migration":
        counts = _closed(result["record_counts"], {"source", "imported", "quarantined"},
                         {"source", "imported", "quarantined"}, "AGGREGATE_RESULT")
        if (any(isinstance(counts[name], bool) or not isinstance(counts[name], int) or counts[name] < 0
                for name in counts) or counts["source"] != counts["imported"] + counts["quarantined"]
                or not isinstance(result["source_count"], int) or result["source_count"] < 1
                or not isinstance(result["run_count"], int) or result["run_count"] < 1):
            raise E("AGGREGATE_RESULT", "migration result summary is incomplete", "UNMET")
    elif gate == "rollout":
        if (not Path(result["consumer"]).is_absolute() or result["surface_count"] < 1
                or result["fork_count"] < 0 or result["canary_count"] != 4):
            raise E("AGGREGATE_RESULT", "rollout result summary is incomplete", "UNMET")
    elif gate == "upgrade-comparison":
        for name in ("new_failures", "missing_tests", "remaining_failures", "missing_suites", "new_suites"):
            if result[name] != []:
                raise E("AGGREGATE_RESULT", "upgrade comparison retains failures or inventory drift", "UNMET")
        if (result["suite_passed"] is not True or result["comparison_passed"] is not True
                or not HASH_RE.fullmatch(str(result["baseline_sha256"]))
                or not HASH_RE.fullmatch(str(result["ledger_sha256"]))
                or not isinstance(result["ledger_entries"], list) or not result["ledger_entries"]
                or not isinstance(result["suite_observations"], list) or not result["suite_observations"]
                or _closed(result["after_provenance"], PROVENANCE, PROVENANCE, "AGGREGATE_RESULT") != provenance):
            raise E("AGGREGATE_RESULT", "upgrade comparison result summary is incomplete", "UNMET")
    proof = _closed(result["verification_proof"], {"manifest", "inputs"},
                    {"manifest", "inputs"}, "VERIFICATION_PROOF")
    manifest_item = _artifact(proof["manifest"], budget)
    if not isinstance(proof["inputs"], dict):
        raise E("VERIFICATION_PROOF", "verification proof inputs must be a closed object", "UNMET")
    expected_inputs = {"baseline"} if gate == "upgrade-comparison" else set()
    if set(proof["inputs"]) != expected_inputs:
        raise E("VERIFICATION_PROOF", "verification proof inputs do not match the gate", "UNMET")
    checked_inputs = {name: _artifact(descriptor, budget)
                      for name, descriptor in proof["inputs"].items()}
    manifest = obj(manifest_item.data)
    if gate == "audit":
        regenerated = audit_mode(manifest)
    elif gate == "coverage":
        regenerated = coverage_mode(manifest, result["line_min"])
    elif gate == "hosts":
        regenerated = hosts_mode(manifest, True, sorted(PAIRINGS),
                                 sorted(REVIEW_DIRECTIONS), 600)
    elif gate == "installation-lifecycle":
        regenerated = installation_mode(manifest, "lifecycle")
    elif gate == "matrix":
        regenerated = matrix_mode(manifest, "hermetic", 25)
    elif gate == "migration":
        regenerated = migration_mode(manifest, "verify-legacy")
    elif gate == "rollout":
        regenerated = rollout_mode(manifest, Path(result["consumer"]))
    else:
        regenerated = upgrade_mode(manifest, checked_inputs["baseline"].locator)
    stable_fields = {
        "gate", "purpose", "binding", "ac_ids", "path_ids", "int_ids", "status", "exit_status",
        "provenance", "gate_vector", "review_complete", "repair_authorized", "path_admitted",
        "rollout_ready", *AGGREGATE_RESULT_FIELDS[gate],
    }
    if any(result[name] != regenerated[name] for name in stable_fields):
        raise E("VERIFICATION_PROOF", "indexed result does not match revalidated gate evidence", "UNMET")
    return result


def aggregate_mode(evidence: Path, require_all_paths: bool, stage: str) -> dict[str, Any]:
    selected = evidence / "index.json" if evidence.is_dir() else evidence
    index = obj(read_checked(selected, Budget(max_artifacts=512, max_evidence_bytes=64 * 1024 * 1024)).data)
    index = _closed(index, {"schema", "candidate_sha256", "results"},
                    {"schema", "candidate_sha256", "results"}, "AGGREGATE_SCHEMA")
    if index["schema"] != "ffs.verification-index/v1" or not HASH_RE.fullmatch(str(index["candidate_sha256"])):
        raise E("AGGREGATE_SCHEMA", "unsupported evidence index")
    results = index["results"]
    if not isinstance(results, list) or not results:
        raise E("AGGREGATE_EMPTY", "evidence index contains no results", "UNMET")
    budget = Budget(max_artifacts=512, max_evidence_bytes=64 * 1024 * 1024)
    gates: set[str] = set()
    ac_ids: set[str] = set()
    path_ids: set[str] = set()
    artifacts: list[dict[str, str]] = []
    selected_provenance: dict[str, str] | None = None
    for descriptor in results:
        checked = _artifact(descriptor, budget)
        result = _aggregate_result(obj(checked.data), budget)
        provenance = result.get("provenance")
        if (not isinstance(provenance, dict) or set(provenance) != PROVENANCE
                or any(not isinstance(value, str) or not HASH_RE.fullmatch(value)
                       for value in provenance.values())
                or provenance["source_sha256"] != index["candidate_sha256"]):
            raise E("AGGREGATE_STALE", "result source does not bind the selected candidate")
        if selected_provenance is None:
            selected_provenance = dict(provenance)
        elif provenance != selected_provenance:
            raise E("AGGREGATE_STALE", "indexed results do not share one runtime provenance closure")
        gate = _string(result.get("gate"), "result.gate")
        if gate in gates:
            raise E("AGGREGATE_RESULT", "evidence index contains a duplicate gate result", "UNMET")
        gates.add(gate)
        ac_ids.update(_strings(result.get("ac_ids", []), "result.ac_ids"))
        path_ids.update(_strings(result.get("path_ids", []), "result.path_ids"))
        artifacts.append({"locator": checked.locator, "sha256": checked.sha256})
    if not REQUIRED_AGGREGATE_GATES.issubset(gates):
        raise E("AGGREGATE_GATES", "required verification gates are missing", "UNMET")
    required_acs = {f"AC-{number:03d}" for number in range(1, 61)}
    required_paths = {f"PATH-{number:03d}" for number in range(1, 25)}
    if not required_acs.issubset(ac_ids) or (require_all_paths and not required_paths.issubset(path_ids)):
        raise E("AGGREGATE_COVERAGE", "all 60 ACs and 24 PATHs are required", "UNMET")
    if stage not in {"m7", "final"}:
        raise E("AGGREGATE_STAGE", "aggregate stage must be m7 or final")
    manifest = {
        "binding": {"run": "spec-014", "activity": "aggregate", "attempt": index["candidate_sha256"][:16]},
        "ac_ids": sorted(ac_ids), "path_ids": sorted(path_ids), "int_ids": [], "label": "authenticated",
        "provenance": selected_provenance, "artifacts": artifacts,
    }
    output = _pass(manifest, "aggregate", stage, result_count=len(results), gates=sorted(gates),
                   candidate_sha256=index["candidate_sha256"], m7_complete=True)
    output["rollout_ready"] = stage == "final"
    output["gate_vector"]["rollout_ready"] = stage == "final"
    return output


def _gate(command: str, purpose: str) -> str:
    if command == "review":
        return "review-admission:upgraded" if purpose == "admission" else "review-completion:upgraded"
    return {"baseline": "baseline-capture", "upgrade": "upgrade-comparison", "installation": "installation:private"}.get(command, command)


def main() -> int:
    parser = argparse.ArgumentParser(description="validate candidate-bound parallel-host evidence")
    sub = parser.add_subparsers(dest="gate", required=True)
    for name in FIELDS:
        command = sub.add_parser(name)
        command.add_argument("--manifest", type=Path, help="absolute manifest; otherwise FFS_VERIFICATION_MANIFEST")
        command.add_argument("--output", type=Path, help="new absolute owner-private result")
        if name == "review":
            command.add_argument("--stage", default="upgraded")
            command.add_argument("--purpose", choices=("admission", "review-completion"), default="admission")
        if name == "installation":
            command.add_argument("--mode", default="private")
        if name == "upgrade":
            command.add_argument("--baseline", type=Path, help="exact named baseline artifact")
        if name == "matrix":
            command.add_argument("--mode", default="hermetic")
            command.add_argument("--repetitions", type=int, default=25)
        if name == "hosts":
            command.add_argument("--authenticated", action="store_true")
            command.add_argument("--pairings", default="claude-claude,claude-codex,codex-codex")
            command.add_argument("--review-directions", default="claude-codex,codex-claude")
            command.add_argument("--soak-seconds", type=int, default=600)
        if name == "migration":
            command.add_argument("--mode", default="verify-legacy")
        if name == "rollout":
            command.add_argument("--consumer", type=Path)
        if name == "coverage":
            command.add_argument("--line-min", type=float, default=80.0)
    command = sub.add_parser("aggregate")
    command.add_argument("--evidence", type=Path, required=True)
    command.add_argument("--require-all-paths", action="store_true")
    command.add_argument("--stage", choices=("m7", "final"), default="final")
    command.add_argument("--output", type=Path)
    args = parser.parse_args()
    purpose = getattr(args, "purpose", "capture" if args.gate == "baseline" else "comparison" if args.gate == "upgrade" else "verification")
    gate = ("installation-lifecycle" if args.gate == "installation"
            and getattr(args, "mode", None) == "lifecycle" else _gate(args.gate, purpose))
    manifest = None
    manifest_item = None
    try:
        if args.gate == "aggregate":
            output = aggregate_mode(args.evidence, args.require_all_paths, args.stage)
        else:
            selected = args.manifest or (Path(os.environ["FFS_VERIFICATION_MANIFEST"]) if os.environ.get("FFS_VERIFICATION_MANIFEST") else None)
            if not selected:
                raise E("MISSING_MANIFEST", "actual evidence manifest is missing", "UNMET")
            manifest_item = read_checked(selected)
            manifest = obj(manifest_item.data)
        if args.gate == "coverage":
            output = coverage_mode(manifest, args.line_min)
        elif args.gate == "review":
            if args.stage != "upgraded":
                raise E("STAGE", "only upgraded review is supported")
            output = do_review(manifest, purpose, args.manifest is None)
        elif args.gate == "baseline":
            output = baseline_mode(manifest)
        elif args.gate == "upgrade":
            if not args.baseline:
                raise E("BASELINE", "--baseline is required", "UNMET")
            output = upgrade_mode(manifest, args.baseline)
        elif args.gate == "installation":
            output = installation_mode(manifest, args.mode)
        elif args.gate == "matrix":
            output = matrix_mode(manifest, args.mode, args.repetitions)
        elif args.gate == "hosts":
            output = hosts_mode(manifest, args.authenticated,
                                [item for item in args.pairings.split(",") if item],
                                [item for item in args.review_directions.split(",") if item],
                                args.soak_seconds)
        elif args.gate == "audit":
            output = audit_mode(manifest)
        elif args.gate == "migration":
            output = migration_mode(manifest, args.mode)
        elif args.gate == "rollout":
            output = rollout_mode(manifest, args.consumer)
        if manifest_item is not None:
            inputs: dict[str, dict[str, str]] = {}
            if args.gate == "upgrade":
                baseline_proof = read_checked(args.baseline)
                if baseline_proof.sha256 != output.get("baseline_sha256"):
                    raise E("VERIFICATION_PROOF", "upgrade baseline changed during verification")
                inputs["baseline"] = {
                    "locator": baseline_proof.locator, "sha256": baseline_proof.sha256,
                }
            output["verification_proof"] = {
                "manifest": {"locator": manifest_item.locator, "sha256": manifest_item.sha256},
                "inputs": inputs,
            }
    except E as exc:
        output = fail(gate, purpose, exc, manifest)
    except Exception as exc:
        output = fail(gate, purpose, E("RUNNER_ERROR", f"unexpected verifier invariant failure: {type(exc).__name__}"), manifest)
        output["exit_status"] = 3
    raw = (json.dumps(output, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    if args.output:
        try:
            save(args.output, raw)
        except E as exc:
            output = fail(gate, purpose, exc, manifest)
            raw = (json.dumps(output, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    sys.stdout.buffer.write(raw)
    return int(output["exit_status"])


if __name__ == "__main__":
    raise SystemExit(main())
