#!/usr/bin/env python3
"""Collect, review, verify and integrate pipeline for cloud results.

Implements contract section 7 with the amendments of 14.6 (step order) and
14.10 (integrate checks). No ledger writes happen here: every function
returns a ``StepResult`` and the caller (the dispatcher) persists it.

Collect order (``collect``):
  1. scan every new object (blobs, commit messages, paths, manifest bytes)
  2. ancestry, mode and size checks
  3. manifest (exempt path ``.ffs-result/<task>.json``)
  4. changed paths against ``owns`` and the denylist
  5. dependency gate
  6. candidate commit imported into the main repo: a fresh single-parent commit
     (parent = the trusted task base, fixed identity) over the validated tree
     minus the manifest, so no worker history is carried (K1)

The result commit must already be in a quarantine repository (the provider
backend's ``fetch`` put it there). Stdlib only, Python 3.9 compatible.
"""

from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import signal
import tempfile
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import cloud_scan

__all__ = [
    "StepResult", "CollectCtx", "SECRET_LEAK_RUNBOOK", "ZERO_OID",
    "owns_match", "changed_paths", "history_paths",
    "step_scan", "step_structure", "step_manifest", "step_paths",
    "step_dependencies", "step_candidate", "collect", "review", "verify",
    "plan_integration", "apply_integration", "verify_slot", "patch_id",
    "default_runner", "dependency_report",
]

# Blocker names (contract 14.8). Names not in that table are marked "new".
B_SECRET_LEAK = "secret-leak"
B_SCAN_REFUSED = "scan-refused"  # new: the scanner itself failed
B_NOT_DESCENDED = "not-descended"  # new
B_UNSAFE_MODE = "unsafe-mode"  # new: symlink or submodule
B_BLOB_LIMIT = "blob-over-limit"  # new
B_TOO_MANY_COMMITS = "too-many-commits"  # new
B_MANIFEST_INVALID = "manifest-invalid"  # new
B_MANIFEST_MISSING = "manifest-missing"  # new
B_MANIFEST_BLOCKED = "manifest-blocked"
B_OUTSIDE_OWNS = "path-outside-owns"
B_DENYLIST = "denylist"
B_DEPENDENCY = "dependency-change"
B_GIT = "git-error"  # new
B_CANDIDATE_CONFLICT = "candidate-ref-conflict"  # new
B_REVIEW_UNAVAILABLE = "review-unavailable"
B_REVIEW_CAP = "review-cap"
B_REVIEW_MODEL_DOWNGRADED = "review-model-downgraded"  # new: approved, but not by the requested judgment-tier model
B_REVIEW_MODEL_UNKNOWN = "review-model-unknown"  # new: approved, but the answering model is not reported
B_REVIEW_REVISE = "review-revise"  # new (state rejected)
B_TESTS_FAILED = "tests-failed"
B_SANDBOX = "sandbox-unavailable"
B_VERIFY_CMD_BLANK = "verify-cmd-missing"  # new: an empty command must never count as a passing test run
B_MERGE_CONFLICT = "merge-conflict"
B_PATCH_ID = "patch-id-mismatch"
B_TIP_MOVED = "tip-moved"  # new: compare-and-swap lost, re-plan
B_BINARY_CHANGE = "binary-change"  # new: a binary change cannot be reviewed as a diff

ST_OK_INTERMEDIATE = "result-ready"
ST_REJECTED = "rejected"
ST_NEEDS_OPERATOR = "needs-operator"

ZERO_OID = "0" * 40
GIT_TIMEOUT_S = 120
DEFAULT_MAX_BLOB_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_COMMITS = 1000
MAX_REVIEW_DIFF_BYTES = 2_000_000
REVIEW_TIMEOUT_S = 700
MAX_REVIEW_TEXT = 4000
MAX_DETAIL = 2000
MAX_LISTED = 20
MANIFEST_DIR = ".ffs-result"
SECRET_REDACT_LIMIT = 500
FIXED_DATE = "@0 +0000"
BOT_NAME = "ffs-cloud"
BOT_EMAIL = "ffs-cloud@localhost.invalid"
REF_RE = re.compile(r"^refs/heads/ffs/cloud/[A-Za-z0-9][A-Za-z0-9_.-]{0,39}/integration$")
OID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
PRODUCERS = ("claude", "codex")

SECRET_LEAK_RUNBOOK = (
    "A cloud result contained a secret-shaped value. "
    "1. Revoke or rotate the credential now (Doppler: `doppler configs tokens revoke`, "
    "then rotate the leaked secret itself). "
    "2. Delete the remote branch that carries it: `git push <origin> --delete <branch>`. "
    "3. Check the provider session transcript and delete or unshare the session. "
    "4. Re-mint the agent token and update the provider environment. "
    "FFS has not integrated this result."
)


@dataclass
class StepResult:
    ok: bool
    state: str
    blocker: Optional[str] = None
    detail: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CollectCtx:
    run: str
    task: str
    generation: int
    nonce: str
    base_sha: str
    oid: str
    quarantine: str
    repo: str
    owns: Sequence[str]
    provider: str = "claude"
    candidate_ref: str = ""
    max_blob_bytes: int = DEFAULT_MAX_BLOB_BYTES
    max_commits: int = DEFAULT_MAX_COMMITS
    deps_accepted: bool = False
    accepted_digest: str = ""  # report_digest the operator accepted; "" = not bound
    validate_manifest: Optional[Callable[..., Any]] = None
    denylist_match: Optional[Callable[[str], bool]] = None
    cache: Dict[str, Any] = field(default_factory=dict)

    @property
    def manifest_path(self) -> str:
        return "%s/%s.json" % (MANIFEST_DIR, self.task)

    @property
    def cand_ref(self) -> str:
        if self.candidate_ref:
            return self.candidate_ref
        from cloud_ledger import candidate_ref  # lazy: the ledger owns the ref layout

        return candidate_ref(self.run, self.task, self.generation)


# --------------------------------------------------------------------------
# git plumbing
# --------------------------------------------------------------------------


class GitError(Exception):
    pass


def _git(
    repo: str,
    args: Sequence[str],
    *,
    stdin: Optional[bytes] = None,
    env: Optional[Dict[str, str]] = None,
    ok_codes: Sequence[int] = (0,),
) -> bytes:
    full_env = cloud_scan.git_env()
    full_env.update(env or {})
    try:
        proc = subprocess.run(
            ["git", "-C", repo] + list(args), input=stdin, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=full_env, timeout=GIT_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise GitError("git invocation failed") from None
    if proc.returncode not in ok_codes:
        raise GitError("git %s failed (rc=%d)" % (args[0], proc.returncode))
    return proc.stdout


def _git_ok(repo: str, args: Sequence[str]) -> bool:
    try:
        _git(repo, args)
        return True
    except GitError:
        return False


def _rev(repo: str, spec: str) -> str:
    return _git(repo, ["rev-parse", "--verify", "--quiet", spec]).decode("ascii").strip()


def _safe_text(text: str, limit: int = SECRET_REDACT_LIMIT) -> str:
    """Redacted, control-free, bounded text safe for a log or ledger detail."""
    raw = text.encode("utf-8", "replace")
    try:
        raw = cloud_scan.redact(raw)
    except cloud_scan.ScanRefused:
        return "[withheld]"
    clean = raw.decode("utf-8", "replace")
    clean = "".join(ch if ch.isprintable() or ch == "\n" else "?" for ch in clean)
    return clean[:limit]


def _fail(state: str, blocker: str, detail: str, **data: Any) -> StepResult:
    return StepResult(False, state, blocker, detail[:MAX_DETAIL], dict(data))


def _guarded(func: Callable[[CollectCtx], StepResult]) -> Callable[[CollectCtx], StepResult]:
    def wrapper(ctx: CollectCtx) -> StepResult:
        try:
            return func(ctx)
        except GitError as exc:
            return _fail(ST_NEEDS_OPERATOR, B_GIT, str(exc))
        except cloud_scan.ScanRefused as exc:
            return _fail(ST_NEEDS_OPERATOR, B_SCAN_REFUSED, "scanner refused: %s" % exc.reason)

    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    return wrapper


# --------------------------------------------------------------------------
# Owns globs
# --------------------------------------------------------------------------


def _glob_segments(glob: str) -> Optional[Tuple[str, ...]]:
    text = glob.strip().replace("\\", "/")
    if not text or text.startswith("/"):
        return None
    segments = [s for s in text.split("/") if s not in ("", ".")]
    if ".." in segments:
        return None
    if not segments or segments[-1] != "**":
        segments.append("**")
    return tuple(segments)


def _match_segments(pattern: Tuple[str, ...], parts: Tuple[str, ...]) -> bool:
    memo: Dict[Tuple[int, int], bool] = {}

    def walk(i: int, j: int) -> bool:
        key = (i, j)
        if key not in memo:
            if i == len(pattern):
                memo[key] = j == len(parts)
            elif pattern[i] == "**":
                memo[key] = walk(i + 1, j) or (j < len(parts) and walk(i, j + 1))
            elif j < len(parts):
                memo[key] = fnmatch.fnmatchcase(parts[j], pattern[i]) and walk(i + 1, j + 1)
            else:
                memo[key] = False
        return memo[key]

    return walk(0, 0)


def owns_match(path: str, owns: Sequence[str]) -> bool:
    """fnmatch-style match with ``**`` (zero or more segments); case sensitive.

    A glob also matches everything below it, so ``src/a`` covers ``src/a/x``.
    """
    parts = tuple(p for p in path.split("/"))
    if not path or path.startswith("/") or any(p in ("", ".", "..") for p in parts):
        return False
    for glob in owns:
        segments = _glob_segments(glob)
        if segments is not None and _match_segments(segments, parts):
            return True
    return False


def _default_denylist() -> Callable[[str], bool]:
    from delegation_router import matches_denylist  # lazy: sibling builder's module

    return matches_denylist


# --------------------------------------------------------------------------
# Diff parsing
# --------------------------------------------------------------------------


def _decode_path(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def _parse_raw_z(out: bytes) -> List[Tuple[str, str, str, str, List[str]]]:
    """``(srcmode, dstmode, dstoid, status, paths)`` entries of ``--raw -z``."""
    records = out.split(b"\0")
    entries: List[Tuple[str, str, str, str, List[str]]] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record.startswith(b":"):
            continue  # header line (commit id) or trailing empty record
        meta = record[1:].decode("ascii", "replace").split(" ")
        if len(meta) != 5:
            raise GitError("unparsable diff record")
        status = meta[4]
        count = 2 if status[:1] in ("R", "C") else 1
        paths = [_decode_path(p) for p in records[index : index + count]]
        index += count
        entries.append((meta[0], meta[1], meta[3], status, paths))
    return entries


def changed_paths(ctx: CollectCtx) -> List[Tuple[str, List[str]]]:
    """``(status, paths)`` for ``base..oid``; renames list both sides."""
    if "changed" not in ctx.cache:
        out = _git(ctx.quarantine, [
            "diff", "--raw", "-z", "-M", "--no-abbrev", "--no-ext-diff", "--no-textconv",
            ctx.base_sha, ctx.oid,
        ])
        ctx.cache["changed"] = [(s, p) for _, _, _, s, p in _parse_raw_z(out)]
    return list(ctx.cache["changed"])


# --------------------------------------------------------------------------
# Step 1: scan every new object
# --------------------------------------------------------------------------


def _base_for_scan(ctx: CollectCtx) -> Optional[str]:
    present = _git_ok(ctx.quarantine, ["cat-file", "-e", ctx.base_sha + "^{commit}"])
    return ctx.base_sha if present else None


def _manifest_bytes(ctx: CollectCtx) -> Optional[bytes]:
    spec = "%s:%s" % (ctx.oid, ctx.manifest_path)
    if not _git_ok(ctx.quarantine, ["cat-file", "-e", spec]):
        return None
    return _git(ctx.quarantine, ["cat-file", "blob", spec])


def _leak_result(findings: Sequence[cloud_scan.Finding]) -> StepResult:
    listing = "; ".join("%s@%s" % (f.cls, f.location) for f in findings[:MAX_LISTED])
    detail = "secret-shaped content in result (%d findings): %s\n%s" % (
        len(findings), listing, SECRET_LEAK_RUNBOOK)
    return StepResult(False, ST_REJECTED, B_SECRET_LEAK, detail,
                      {"findings": [{"class": f.cls, "location": f.location} for f in findings],
                       "runbook": SECRET_LEAK_RUNBOOK})


def _scan_decoded_json(raw: bytes, label: str) -> List[cloud_scan.Finding]:
    """Findings in the DECODED strings of a JSON document (escapes resolved, K3)."""
    entry = getattr(cloud_scan, "scan_json_decoded", None)
    if entry is not None:
        try:
            return list(entry(raw) or [])
        except cloud_scan.ScanRefused as exc:
            if exc.reason == "bad-json":
                return []  # the manifest step rejects invalid JSON with its own blocker
            raise
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return []  # the manifest step rejects invalid JSON
    return list(cloud_scan.scan_text_fields(parsed, label=label))


def _scan_prompt_text(text: str, label: str) -> List[cloud_scan.Finding]:
    """Findings in an assembled outbound prompt (K3)."""
    entry = getattr(cloud_scan, "scan_prompt", None)
    if entry is not None:
        return list(entry(text) or [])
    return list(cloud_scan.scan_bytes(text.encode("utf-8", "replace"), label=label))


@_guarded
def step_scan(ctx: CollectCtx) -> StepResult:
    """Scan all new objects plus the raw manifest bytes (14.6)."""
    findings: List[cloud_scan.Finding] = []
    refused: Optional[cloud_scan.ScanRefused] = None
    try:
        findings.extend(cloud_scan.scan_git_objects(ctx.quarantine, _base_for_scan(ctx), ctx.oid))
    except cloud_scan.ScanRefused as exc:
        findings.extend(exc.findings)
        if exc.reason != "missing-object":
            refused = exc
    manifest = _manifest_bytes(ctx)
    if manifest is not None:
        findings.extend(cloud_scan.scan_bytes(manifest, label="manifest"))
        findings.extend(_scan_decoded_json(manifest, "manifest"))
    if findings:
        return _leak_result(sorted(set(findings), key=lambda f: (f.cls, f.location)))
    if refused is not None:
        raise refused
    return StepResult(True, ST_OK_INTERMEDIATE, None, "scan clean")


# --------------------------------------------------------------------------
# Step 2: ancestry, modes, size
# --------------------------------------------------------------------------


def _range_args(ctx: CollectCtx) -> List[str]:
    return ["%s..%s" % (ctx.base_sha, ctx.oid)]


def _commits_in_range(ctx: CollectCtx) -> List[str]:
    out = _git(ctx.quarantine, ["rev-list"] + _range_args(ctx))
    return out.decode("ascii").split()


def _range_entries(ctx: CollectCtx, commits: Sequence[str]) -> List[Tuple[str, str, str, str, List[str]]]:
    if not commits:
        return []
    if "range_entries" not in ctx.cache:
        out = _git(ctx.quarantine, [
            "diff-tree", "--stdin", "-r", "-z", "-m", "--no-renames", "--root", "--no-abbrev",
        ], stdin=("\n".join(commits) + "\n").encode("ascii"))
        ctx.cache["range_entries"] = _parse_raw_z(out)
    return list(ctx.cache["range_entries"])


def history_paths(ctx: CollectCtx) -> List[str]:
    """Every path touched by ANY commit of ``base..oid`` (adds, edits, deletions), sorted.

    A change that a later commit restores still shows up here, so the path,
    denylist, owns and dependency checks see worker history, not only the net diff.
    """
    paths = {p for _, _, _, _, ps in _range_entries(ctx, _commits_in_range(ctx)) for p in ps}
    return sorted(paths)


def _blob_sizes(ctx: CollectCtx, oids: Sequence[str]) -> Dict[str, Optional[int]]:
    if not oids:
        return {}
    out = _git(ctx.quarantine, ["cat-file", "--batch-check"],
               stdin=("\n".join(oids) + "\n").encode("ascii"))
    sizes: Dict[str, Optional[int]] = {}
    for line in out.decode("ascii", "replace").splitlines():
        fields = line.split()
        sizes[fields[0]] = None if len(fields) < 3 else int(fields[2])
    return sizes


@_guarded
def step_structure(ctx: CollectCtx) -> StepResult:
    """Descends from base; no symlink or submodule; every blob present and capped."""
    if not _git_ok(ctx.quarantine, ["merge-base", "--is-ancestor", ctx.base_sha, ctx.oid]):
        return _fail(ST_REJECTED, B_NOT_DESCENDED, "result does not descend from the task base")
    commits = _commits_in_range(ctx)
    if len(commits) > ctx.max_commits:
        return _fail(ST_REJECTED, B_TOO_MANY_COMMITS, "%d commits exceed %d" % (len(commits), ctx.max_commits))
    entries = _range_entries(ctx, commits)
    bad = sorted({p[0] for _, dst, _, _, p in entries if dst in ("120000", "160000")})
    if bad:
        return _fail(ST_REJECTED, B_UNSAFE_MODE, "symlink or submodule entries: %s" % _paths_text(bad))
    blobs = sorted({oid for _, dst, oid, _, _ in entries if dst != "000000"})
    sizes = _blob_sizes(ctx, blobs)
    over = [oid for oid in blobs if sizes.get(oid) is None or (sizes[oid] or 0) > ctx.max_blob_bytes]
    if over:
        return _fail(ST_REJECTED, B_BLOB_LIMIT, "%d blobs missing or over %d bytes" % (len(over), ctx.max_blob_bytes))
    return StepResult(True, ST_OK_INTERMEDIATE, None, "%d commits" % len(commits))


def _paths_text(paths: Sequence[str]) -> str:
    shown = ", ".join(repr(p) for p in list(paths)[:MAX_LISTED])
    extra = "" if len(paths) <= MAX_LISTED else " (+%d more)" % (len(paths) - MAX_LISTED)
    return _safe_text(shown + extra, MAX_DETAIL)


# --------------------------------------------------------------------------
# Step 3: manifest
# --------------------------------------------------------------------------


def _load_validator(ctx: CollectCtx) -> Callable[..., Any]:
    if ctx.validate_manifest is not None:
        return ctx.validate_manifest
    from cloud_ledger import validate_manifest  # lazy: sibling builder's module

    return validate_manifest


def _manifest_dict(result: Any, raw: bytes) -> Dict[str, Any]:
    if isinstance(result, dict):
        return result
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("manifest is not an object")
    return parsed


_VALIDATOR_CODE_RE = re.compile(r"^MANIFEST_[A-Z0-9_]{1,40}$")


def _validator_code(exc: BaseException) -> str:
    """The validator's typed error code, never its free-text detail (which can echo worker-chosen text)."""
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and _VALIDATOR_CODE_RE.match(code) else "MANIFEST_UNPARSEABLE"


def _raw_nonce_matches(raw: bytes, nonce: str) -> bool:
    """Whether the manifest bytes carry exactly this task generation's nonce (best effort, never raises).

    The nonce is a 128-bit secret only this task's worker was given, so a match proves whose result
    this is even when another field of the manifest is wrong."""
    try:
        doc = json.loads(raw.decode("utf-8"))
        found = doc.get("nonce") if isinstance(doc, dict) else None
        return isinstance(found, str) and bool(nonce) and hmac.compare_digest(found.encode("utf-8"), str(nonce).encode("utf-8"))
    except (ValueError, TypeError, AttributeError):
        return False


@_guarded
def step_manifest(ctx: CollectCtx) -> StepResult:
    """Validate the worker manifest; ``blocked`` parks the task."""
    raw = _manifest_bytes(ctx)
    if raw is None:
        if ctx.provider == "codex":
            manifest = {"status": "complete", "manifest_source": "synthesized"}
            ctx.cache["manifest"] = manifest
            return StepResult(True, ST_OK_INTERMEDIATE, None, "manifest synthesized", {"manifest": manifest})
        return _fail(ST_REJECTED, B_MANIFEST_MISSING, "no manifest at %s" % ctx.manifest_path)
    try:
        result = _load_validator(ctx)(
            raw, run=ctx.run, task=ctx.task, generation=ctx.generation,
            nonce=ctx.nonce, base_sha=ctx.base_sha, require_nonce=True)
        manifest = _manifest_dict(result, raw)
    except Exception as exc:  # noqa: BLE001 - any validator failure rejects
        return _fail(ST_REJECTED, B_MANIFEST_INVALID, "manifest invalid: %s" % _safe_text(str(exc)),
                     validator_code=_validator_code(exc), nonce_matches=_raw_nonce_matches(raw, ctx.nonce))
    manifest = dict(manifest)
    manifest.setdefault("manifest_source", "worker")
    ctx.cache["manifest"] = manifest
    if manifest.get("status") == "blocked":
        reasons = "; ".join(_safe_text(str(b), 200) for b in (manifest.get("blockers") or [])[:MAX_LISTED])
        return _fail(ST_NEEDS_OPERATOR, B_MANIFEST_BLOCKED, "worker reported blocked: %s" % reasons,
                     manifest=manifest)
    return StepResult(True, ST_OK_INTERMEDIATE, None, "manifest ok", {"manifest": manifest})


# --------------------------------------------------------------------------
# Step 4: paths against owns and denylist
# --------------------------------------------------------------------------


def _tree_paths(repo: str, tree_ish: str) -> List[str]:
    out = _git(repo, ["ls-tree", "-r", "-z", "--name-only", tree_ish])
    return [_decode_path(p) for p in out.split(b"\0") if p]


def _case_collision_check(ctx: CollectCtx) -> List[str]:
    """Result paths that collide case-insensitively with each other or with base paths."""
    from delegation_router import case_collisions  # lazy: sibling module

    net = changed_paths(ctx)
    deleted = {ps[0] for status, ps in net if status[:1] in ("D", "R")}
    kept = {p for status, ps in net if status[:1] != "D" for p in ps[-1:]}
    kept.discard(ctx.manifest_path)
    if not kept:
        return []
    base_paths = [p for p in _tree_paths(ctx.quarantine, ctx.base_sha) if p not in deleted]
    return case_collisions(sorted(kept), base_paths)


@_guarded
def step_paths(ctx: CollectCtx) -> StepResult:
    """Adds, modifications, deletions and both rename sides must be owned.

    Checked over the net diff AND over every commit of ``base..oid`` (K1): a
    forbidden path touched by any commit blocks even when a later commit
    restores it.
    """
    deny = ctx.denylist_match or _default_denylist()
    denied: List[str] = []
    outside: List[str] = []
    seen = set()
    candidates = [p for _, paths in changed_paths(ctx) for p in paths] + history_paths(ctx)
    for path in candidates:
        if path == ctx.manifest_path or path in seen:
            continue
        seen.add(path)
        if deny(path):
            denied.append(path)
        elif not owns_match(path, ctx.owns):
            outside.append(path)
    if denied:
        return _fail(ST_NEEDS_OPERATOR, B_DENYLIST, "denylisted paths: %s" % _paths_text(denied), paths=denied)
    if outside:
        return _fail(ST_NEEDS_OPERATOR, B_OUTSIDE_OWNS, "paths outside owns: %s" % _paths_text(outside),
                     paths=outside)
    collided = _case_collision_check(ctx)
    if collided:  # reported as a denylist hit: the path would alias another file on a case-folding checkout
        return _fail(ST_NEEDS_OPERATOR, B_DENYLIST, "case-colliding paths: %s" % _paths_text(collided),
                     paths=collided, reason="case-collision")
    return StepResult(True, ST_OK_INTERMEDIATE, None, "%d paths" % len(seen), {"paths": sorted(seen)})


# --------------------------------------------------------------------------
# Step 5: dependency gate
# --------------------------------------------------------------------------

_LOCK_NAMES = (
    "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "bun.lock",
    "uv.lock", "poetry.lock", "Pipfile.lock", "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock",
)
#: Manifests of ecosystems whose packages are NOT parsed: any change is unattributed.
_OPAQUE_NAMES = (
    "pyproject.toml", "Cargo.toml", "go.mod", "go.work", "Pipfile", "composer.json",
    "pnpm-workspace.yaml", "setup.cfg", "setup.py", "pom.xml", "build.gradle", "build.gradle.kts", "Package.swift",
    "deno.json", "deno.jsonc", "bunfig.toml", "environment.yml", "NuGet.config", "Podfile", "mix.exs", "pubspec.yaml",
)
_NPM_SECTIONS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
#: package.json keys that never run or fetch anything at install time.
_NPM_HARMLESS = frozenset([
    "name", "version", "description", "keywords", "license", "author", "contributors", "homepage",
    "repository", "bugs", "main", "module", "types", "typings", "files", "engines", "private", "type",
    "exports", "sideEffects", "funding",
])
_NAME_SPLIT = re.compile(r"[<>=!~;\[\s@(]")
_NPM_REGISTRY_SPEC = re.compile(r"^[A-Za-z0-9.^~<>=*|\s-]*$")  # semver range or dist-tag; no ':' or '/'
# Linear-time by construction: every character is matched by exactly one alternative, so a failing match never
# backtracks over competing splits (adjacent \s* runs, an optional "=" that "[^;@\s,]*" also matches, and "[^#]*"
# overlapping the --hash tokens are what made the one-line form polynomial/exponential on adversarial input).
# Reads as: name[extras], optional comma-separated version specs, then either ";marker" (a "#" may appear only
# inside a trailing --hash token) or only --hash tokens.
_REQ_SPEC = r"[<>=!~][^;@\s,]*"
_REQ_REGISTRY_LINE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._-]*\])?"
    r"(?:\s*" + _REQ_SPEC + r"(?:\s*,\s*" + _REQ_SPEC + r")*)?"
    r"(?:\s*;(?:[^#]*$|[^#]*\s--hash=[^#\s]*#\S*(?:\s+--hash=\S+)*\s*$)"
    r"|(?:\s+--hash=\S+)*\s*$)")


_LOCK_FOLDED = frozenset(n.casefold() for n in _LOCK_NAMES)
_OPAQUE_FOLDED = frozenset(n.casefold() for n in _OPAQUE_NAMES)


def _dep_kind(path: str) -> Optional[str]:
    """The dependency-file kind of ``path``. Names and patterns are case-folded: on a case-insensitive checkout
    ``Package.json`` is opened as ``package.json``, install scripts included."""
    base = path.rsplit("/", 1)[-1].casefold()
    if base in _LOCK_FOLDED or fnmatch.fnmatchcase(base, "pipfile*") and base != "pipfile":
        return "lock"
    if base == "package.json":
        return "npm"
    if fnmatch.fnmatchcase(base, "requirements*.txt"):
        return "requirements"
    if base in _OPAQUE_FOLDED or fnmatch.fnmatchcase(base, "gemfile*"):
        return "opaque"
    return None


Analysis = Tuple[Dict[str, str], Dict[str, str]]


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _analyze_npm(text: str) -> Analysis:
    doc = json.loads(text)
    if not isinstance(doc, dict):
        raise ValueError("package.json is not an object")
    deps: Dict[str, str] = {}
    other: Dict[str, str] = {}
    for key, value in doc.items():
        if key in _NPM_SECTIONS and isinstance(value, dict):
            for name, spec in value.items():
                if name in deps:
                    other["repeated:" + name] = other.get("repeated:" + name, deps[name]) + "\n%s:%s" % (key, _canon(spec))
                deps[name] = "%s:%s" % (key, _canon(spec))
        else:
            other[key] = _canon(value)
    return deps, other


def _analyze_requirements(text: str) -> Analysis:
    deps: Dict[str, str] = {}
    other: Dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.split(" #")[0].strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("-"):
            other["option:" + stripped] = stripped
        else:
            name = _NAME_SPLIT.split(stripped, 1)[0].lower()
            if name in deps:  # a repeated declaration is not attributable to one line: report every line
                other["repeated:" + name] = other.get("repeated:" + name, deps[name]) + "\n" + stripped
            deps[name] = stripped
    return deps, other


def _analyze(kind: str, text: Optional[str]) -> Analysis:
    if text is None:
        return {}, {}
    if kind == "npm":
        return _analyze_npm(text)
    if kind == "requirements":
        return _analyze_requirements(text)
    return {}, {"content": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()}


def _blob_text(repo: str, oid: str, path: str) -> Optional[str]:
    spec = "%s:%s" % (oid, path)
    if not _git_ok(repo, ["cat-file", "-e", spec]):
        return None
    return _git(repo, ["cat-file", "blob", spec]).decode("utf-8", "replace")


def _safe_analyze(kind: str, text: Optional[str]) -> Tuple[Analysis, bool]:
    try:
        return _analyze(kind, text), True
    except (ValueError, KeyError, TypeError, AttributeError):
        digest = hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()
        return ({}, {"unparsable": digest}), False


def _npm_source_reasons(path: str, name: str, packed: str) -> List[str]:
    section, _, canon = packed.partition(":")
    try:
        spec = json.loads(canon)
    except ValueError:
        spec = None
    if not isinstance(spec, str) or not _NPM_REGISTRY_SPEC.match(spec):
        return ["%s: %s (%s) does not come from the registry by version range" % (path, name, section)]
    return []


def _attribution_reasons(path: str, kind: str, parsed: bool, added_changed: Dict[str, str],
                         other_keys: Sequence[str]) -> List[str]:
    """Why this file's change cannot be attributed to named registry packages."""
    if kind == "lock":
        return ["%s: lockfile change (resolved sources and transitive packages are not attributable)" % path]
    if kind == "opaque":
        return ["%s: manifest of a package manager whose packages are not parsed" % path]
    if not parsed:
        return ["%s: unparsable dependency file" % path]
    reasons: List[str] = []
    if kind == "npm":
        for name, packed in sorted(added_changed.items()):
            reasons.extend(_npm_source_reasons(path, name, packed))
        for key in other_keys:
            if key == "scripts" or key not in _NPM_HARMLESS:
                reasons.append("%s: %s changed (install-time behavior)" % (path, key))
    elif kind == "requirements":
        for name, line in sorted(added_changed.items()):
            if not _REQ_REGISTRY_LINE.match(line):
                reasons.append("%s: %s is not a plain registry requirement" % (path, name))
        if other_keys:
            reasons.append("%s: option or repeated-package line changed" % path)
    return reasons


def dependency_report(
    repo: str, base: str, oid: str, paths: Sequence[str]
) -> Dict[str, Any]:
    """Added, changed and removed packages plus attribution, per dependency file.

    ``unattributed`` is True when ANY change cannot be pinned to named registry
    packages (lockfiles, scripts, non-registry sources, unparsed ecosystems,
    removal-only, repeated declarations, history-only). ``report_digest`` is
    the sha256 of the normalized report bound to ``oid``; a grant for an
    unattributed change must name it.
    """
    report: Dict[str, Any] = {"files": [], "added": [], "changed": [], "removed": [], "other": [],
                              "packages": [], "unattributed": False, "unattributed_reasons": []}
    names = set()
    reasons: List[str] = []
    for path in sorted(set(paths)):
        kind = _dep_kind(path)
        if kind is None:
            continue
        report["files"].append(path)
        old_text, new_text = _blob_text(repo, base, path), _blob_text(repo, oid, path)
        if old_text == new_text:
            report["other"].append("%s: touched in history, net unchanged" % path)
            reasons.append("%s: touched in worker history" % path)
            continue
        (old_deps, old_other), _ = _safe_analyze(kind, old_text)
        (new_deps, new_other), parsed = _safe_analyze(kind, new_text)
        added = sorted(set(new_deps) - set(old_deps))
        changed = sorted(n for n in set(new_deps) & set(old_deps) if new_deps[n] != old_deps[n])
        for name in added:
            report["added"].append("%s: %s" % (path, name))
        for name in changed:
            report["changed"].append("%s: %s" % (path, name))
        names.update(added + changed)
        report["removed"].extend("%s: %s" % (path, n) for n in sorted(set(old_deps) - set(new_deps)))
        keys = sorted(k for k in set(old_other) | set(new_other) if old_other.get(k) != new_other.get(k))
        label = "(unparsable file)" if not parsed else None
        report["other"].extend("%s: %s" % (path, label or ("option change" if k.startswith("option:") else k)) for k in keys)
        reasons.extend(_attribution_reasons(path, kind, parsed, {n: new_deps[n] for n in added + changed}, keys))
    if report["removed"] and not names:  # removal alone names no package, so no install grant could ever cover it
        reasons.append("dependencies removed with no package added or changed")
    report["packages"] = sorted(names)
    report["unattributed_reasons"] = sorted(set(reasons))
    report["unattributed"] = bool(reasons)
    body = json.dumps(report, sort_keys=True)
    report["report_digest"] = hashlib.sha256((body + "\n" + oid).encode("utf-8")).hexdigest()
    return report


@_guarded
def step_dependencies(ctx: CollectCtx) -> StepResult:
    """Dependency files need the operator (``resolve --action accept-deps``).

    ``data`` carries ``dependencies`` (the full report) plus its ``packages``,
    ``unattributed`` and ``report_digest`` so the dispatcher can demand an
    ``install:unattributed:<digest12>`` grant and compare the digest under lock.
    """
    flat = [p for _, paths in changed_paths(ctx) for p in paths] + history_paths(ctx)
    if not any(_dep_kind(p) for p in flat):
        return StepResult(True, ST_OK_INTERMEDIATE, None, "no dependency files")
    report = dependency_report(ctx.quarantine, ctx.base_sha, ctx.oid, flat)
    if not report["packages"] and not report["unattributed"]:  # no dependency declaration changed (e.g. version bump)
        return StepResult(True, ST_OK_INTERMEDIATE, None, "no dependency declaration changed")
    detail = _safe_text(
        "dependency files %s; added %s; changed %s; removed %s; other changes %s; unattributed %s %s" % (
            report["files"], report["added"], report["changed"], report["removed"], report["other"],
            report["unattributed"], report["unattributed_reasons"]),
        MAX_DETAIL)
    data = {"dependencies": report, "packages": report["packages"], "unattributed": report["unattributed"],
            "report_digest": report["report_digest"]}
    if ctx.deps_accepted and ctx.accepted_digest and ctx.accepted_digest != report["report_digest"]:
        return StepResult(False, ST_NEEDS_OPERATOR, B_DEPENDENCY,
                          "dependency report changed since it was accepted; " + detail, data)
    if ctx.deps_accepted:
        return StepResult(True, ST_OK_INTERMEDIATE, None, "dependencies accepted: " + detail, data)
    return StepResult(False, ST_NEEDS_OPERATOR, B_DEPENDENCY, detail, data)


# --------------------------------------------------------------------------
# Step 6: candidate commit
# --------------------------------------------------------------------------

_BOT_ENV = {
    "GIT_AUTHOR_NAME": BOT_NAME, "GIT_AUTHOR_EMAIL": BOT_EMAIL, "GIT_AUTHOR_DATE": FIXED_DATE,
    "GIT_COMMITTER_NAME": BOT_NAME, "GIT_COMMITTER_EMAIL": BOT_EMAIL, "GIT_COMMITTER_DATE": FIXED_DATE,
}


def _build_candidate(ctx: CollectCtx) -> str:
    """Fresh commit of the validated tree (minus the manifest) on the trusted base.

    Worker history is dropped (K1): the candidate has exactly one parent, the
    task base, a fixed identity and a fixed message. A successor that pushes
    the candidate therefore exports only content the gates validated.
    """
    tmp = tempfile.mkdtemp(prefix="ffs-cand-")
    try:
        env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        _git(ctx.quarantine, ["read-tree", ctx.oid], env=env)
        if _manifest_bytes(ctx) is not None:
            _git(ctx.quarantine, ["update-index", "--force-remove", "--", ctx.manifest_path], env=env)
        tree = _git(ctx.quarantine, ["write-tree"], env=env).decode("ascii").strip()
        env.update(_BOT_ENV)
        message = ("ffs: cloud result for %s (generation %d)\n" % (ctx.task, ctx.generation)).encode()
        out = _git(ctx.quarantine, ["commit-tree", tree, "-p", ctx.base_sha], stdin=message, env=env)
        return out.decode("ascii").strip()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _import_candidate(ctx: CollectCtx, candidate: str) -> None:
    """Install ``candidate`` at the candidate ref without ever writing through another ref.

    The fetch names no destination ref: it only brings the objects in (and is not
    trusted for anything else). The ref itself is then installed with
    ``update-ref --no-deref`` as a compare-and-swap, so a symref or a moved ref
    raced in after the checks below can neither redirect the write into another
    branch nor be overwritten.
    """
    ref = ctx.cand_ref
    if _git_ok(ctx.repo, ["symbolic-ref", "-q", ref]):  # refuse a (possibly dangling) symref outright
        raise ValueError(B_CANDIDATE_CONFLICT)
    existing = _rev(ctx.repo, ref + "^{commit}") if _git_ok(ctx.repo, ["rev-parse", "--verify", "--quiet", ref]) else ""
    if existing and existing != candidate:
        raise ValueError(B_CANDIDATE_CONFLICT)
    _git(ctx.quarantine, ["update-ref", "refs/ffs/candidate", candidate])
    _git(ctx.repo, [
        "-c", "protocol.file.allow=always", "-c", "transfer.fsckObjects=true",
        "fetch", "--no-tags", "--no-write-fetch-head", ctx.quarantine, candidate,
    ])
    if _rev(ctx.repo, candidate + "^{commit}") != candidate:  # pinned OID, not FETCH_HEAD
        raise GitError("candidate import did not land")
    if _git_ok(ctx.repo, ["symbolic-ref", "-q", ref]):  # raced in since the first check
        raise ValueError(B_CANDIDATE_CONFLICT)
    expected_old = existing or "0" * len(candidate)  # all-zero OID: the ref must not exist yet
    try:
        _git(ctx.repo, ["update-ref", "--no-deref", ref, candidate, expected_old])
    except GitError:
        # The compare-and-swap lost a race (or git failed). Only a ref that now holds the
        # candidate is success; anything else is the typed conflict, never a write-through.
        if _git_ok(ctx.repo, ["symbolic-ref", "-q", ref]):
            raise ValueError(B_CANDIDATE_CONFLICT) from None
        try:
            now = _rev(ctx.repo, ref + "^{commit}")
        except GitError:
            raise GitError("candidate import did not land") from None
        if now != candidate:
            raise ValueError(B_CANDIDATE_CONFLICT) from None
    if _git_ok(ctx.repo, ["symbolic-ref", "-q", ref]) or _rev(ctx.repo, ref + "^{commit}") != candidate:
        raise GitError("candidate import did not land")


@_guarded
def step_candidate(ctx: CollectCtx) -> StepResult:
    """Build the candidate (manifest removed) and import it; freezes oid and tree."""
    candidate = _build_candidate(ctx)
    try:
        _import_candidate(ctx, candidate)
    except ValueError:
        return _fail(ST_NEEDS_OPERATOR, B_CANDIDATE_CONFLICT, "candidate ref already points elsewhere")
    tree = _rev(ctx.repo, candidate + "^{tree}")
    data = {"candidate_oid": candidate, "tree": tree, "candidate_ref": ctx.cand_ref, "result_oid": ctx.oid}
    return StepResult(True, "collected", None, "candidate %s" % candidate, data)


# --------------------------------------------------------------------------
# Orchestration of collect
# --------------------------------------------------------------------------

_COLLECT_STEPS = (step_scan, step_structure, step_manifest, step_paths, step_dependencies, step_candidate)


def collect(ctx: CollectCtx) -> StepResult:
    """Run steps 1-6 in order; the first failure is returned as is.

    On success ``data`` holds ``candidate_oid``, ``tree``, ``candidate_ref``,
    ``manifest`` and ``paths`` (and ``dependencies`` when accepted).
    """
    merged: Dict[str, Any] = {}
    for step in _COLLECT_STEPS:
        result = step(ctx)
        if not result.ok:
            return result
        merged.update(result.data)
    return StepResult(True, "collected", None, "collected", merged)


# --------------------------------------------------------------------------
# Review
# --------------------------------------------------------------------------

Runner = Callable[[Sequence[str], Dict[str, str], int], Tuple[int, str]]
_VERDICT_RE = re.compile(r"^CLOUD-REVIEW: (APPROVE|REVISE|UNAVAILABLE|cap)\s*$")


def _kill_group(proc: "subprocess.Popen[bytes]") -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def default_runner(argv: Sequence[str], env: Dict[str, str], timeout_s: int,
                   cwd: Optional[str] = None) -> Tuple[int, str]:
    """Run the reviewer in its own process group; a timeout kills the whole group (K6).
    ``cwd`` is the repository the review belongs to: the script's grant-store lookups resolve there."""
    try:
        proc = subprocess.Popen(
            list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, start_new_session=True, cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        return 124, ""
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            proc.communicate(timeout=5)
        except (subprocess.SubprocessError, OSError):
            pass
        return 124, ""
    except (OSError, subprocess.SubprocessError):
        _kill_group(proc)
        return 124, ""
    return proc.returncode, out.decode("utf-8", "replace")


def patch_id(repo: str, old: str, new: str) -> str:
    """Exact ``git patch-id --verbatim`` of ``git diff old new`` ('' for an empty diff).

    ``--verbatim`` hashes whitespace as written (``--stable`` would ignore it);
    an older git without the flag falls back to ``--stable`` (the merge path
    then relies on its exact-tree expectation instead).
    """
    diff = _git(repo, ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "-M", old, new])
    try:
        out = _git(repo, ["patch-id", "--verbatim"], stdin=diff)
    except GitError:
        out = _git(repo, ["patch-id", "--stable"], stdin=diff)
    return out.decode("ascii").split()[0] if out.strip() else ""


def _script_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "..", "scripts", "gsd", "cloud-review.sh")


def _summary_text(manifest: Dict[str, Any]) -> str:
    lines = ["status: %s" % manifest.get("status", "")]
    lines.append("summary: %s" % manifest.get("summary", ""))
    for key in ("decisions_needed", "blockers"):
        for item in manifest.get(key) or []:
            lines.append("%s: %s" % (key, item))
    for test in manifest.get("tests_run") or []:
        lines.append("test: %s" % json.dumps(test, sort_keys=True))
    return "\n".join(lines) + "\n"


def _write_private(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


#: ``cloud-review.sh`` prints exactly one of these immediately before the verdict line when the
#: reviewer ran. It is taken from adversary-host.sh's out-of-band answer provenance, and only the line
#: in that position is read, so reviewer text (which precedes it) cannot author it.
_MODEL_RE = re.compile(
    r"^CLOUD-REVIEW-MODEL: requested=([A-Za-z0-9._:-]{1,64}) answered=([A-Za-z0-9._:-]{1,64}) descent=([01])\s*$")


def _split_model_line(lines: List[str]) -> Tuple[List[str], Optional["re.Match[str]"]]:
    """``(lines without the model line, its match)``; the model line is the one just before the verdict."""
    if len(lines) >= 2:
        found = _MODEL_RE.match(lines[-2])
        if found:
            return lines[:-2] + lines[-1:], found
    return lines, None


def _classify_review(rc: int, out: str) -> Tuple[str, str]:
    """(verdict, reviewer text) from rc and the final ``CLOUD-REVIEW:`` line."""
    return _classify_review_full(rc, out)[:2]


def _classify_review_full(rc: int, out: str) -> Tuple[str, str, Dict[str, str]]:
    """(verdict, reviewer text, model facts); model facts are ``{}`` when the model line is absent.

    ``{"requested", "answered", "descent"}`` where ``-`` means the script could not name the model."""
    lines = out.rstrip("\n").split("\n")
    found = _VERDICT_RE.match(lines[-1]) if lines else None
    model: Dict[str, str] = {}
    if found:
        lines, model_match = _split_model_line(lines)
        if model_match:
            model = {"requested": model_match.group(1), "answered": model_match.group(2), "descent": model_match.group(3)}
    text = "\n".join(lines[:-1]) if found else out
    word = found.group(1) if found else "UNAVAILABLE"
    agreed = {(0, "APPROVE"): "APPROVE", (1, "REVISE"): "REVISE", (4, "cap"): "cap"}
    return agreed.get((rc, word), "UNAVAILABLE"), text, model


#: Environment the reviewer child receives (K3): no API keys, no cloud tokens.
#: ``PATH``/``HOME``/locale/terminal and the CLI config dirs, plus exactly the
#: FFS_/GSD_/ADVERSARY_/GATES_ variables read by cloud-review.sh, adversary-host.sh
#: and fence-data.sh.
REVIEW_ENV_ALLOW = (
    "PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "TERM", "CODEX_HOME",
    "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME",
    "FFS_HOST", "FFS_HOST_PROCESS_DETECT", "FFS_CROSS_VENDOR_FALLBACK",
    "FFS_ADVERSARY_MODEL_READY", "FFS_ADVERSARY_MODEL_PROBE", "FFS_ADVERSARY_MODEL_PROBE_TIMEOUT",
    "FFS_ADVERSARY_ATTEMPT_TIMEOUT", "FFS_ADVERSARY_REVIEW_ATTEMPT_TIMEOUT",
    "FFS_ADVERSARY_PREFERRED_ATTEMPT_TIMEOUT", "FFS_ADVERSARY_PREFERRED_REVIEW_TIMEOUT",
    "FFS_ADVERSARY_FALLBACK_ATTEMPT_TIMEOUT", "FFS_ADVERSARY_FALLBACK_REVIEW_TIMEOUT",
    "GSD_RUN_ID", "ADVERSARY_SEAM", "ADVERSARY_BIN_CODEX", "ADVERSARY_BIN_CLAUDE",
    "ADVERSARY_LAST_TIER_DESCENT", "ADVERSARY_RUNG_FILE", "ADVERSARY_INVOCATION_ID", "GATES_STORE",
)


def _review_env(run: str) -> Dict[str, str]:
    env = {name: os.environ[name] for name in REVIEW_ENV_ALLOW if name in os.environ}
    env.update({"GSD_RUN_ID": run, "ADVERSARY_SEAM": "cloud-review", "FFS_CROSS_VENDOR_FALLBACK": "off"})
    return env


def _binary_changes(repo: str, old: str, new: str) -> List[str]:
    """Paths git treats as binary in ``old..new`` (``-`` in ``--numstat``)."""
    out = _git(repo, ["diff", "--numstat", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", old, new])
    paths: List[str] = []
    for record in out.split(b"\0"):
        fields = record.split(b"\t", 2)
        if len(fields) == 3 and fields[0] == b"-" and fields[1] == b"-":
            paths.append(_decode_path(fields[2]))
    return paths


def _redact_base_lines(diff: bytes) -> Tuple[bytes, bytes]:
    """``(redacted diff, added lines)``.

    Deleted and context lines come from the operator's own base and may hold
    credentials the worker never wrote; the outbound diff is redacted rather
    than rejected. Added lines were already scanned inbound; they are returned
    separately so a leak there still rejects.
    """
    added = b"".join(
        line[1:] for line in diff.splitlines(keepends=True)
        if line.startswith(b"+") and not line.startswith(b"+++"))
    return cloud_scan.redact(diff), added


def review(
    *,
    repo: str,
    run: str,
    task: str,
    generation: int,
    producer: str,
    base_oid: str,
    candidate_oid: str,
    manifest: Dict[str, Any],
    runner: Optional[Runner] = None,
    script: Optional[str] = None,
    attempt: int = 0,
) -> StepResult:
    """Cross-vendor review of ``base_oid..candidate_oid`` via cloud-review.sh.

    Any binary change makes the task ``needs-operator`` with blocker
    ``binary-change``: the reviewer cannot see the published bytes.
    ``data`` carries ``tree`` (the candidate tree the approval is bound to),
    ``base_oid`` and ``candidate_oid``. ``attempt`` (an operator retest counter,
    default 0) selects the script's round meter, so a retest gets a fresh, still
    bounded, round budget.
    """
    if producer not in PRODUCERS:
        return _fail(ST_NEEDS_OPERATOR, B_REVIEW_UNAVAILABLE, "unknown producer")
    try:
        diff = _git(repo, ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "-M", base_oid, candidate_oid])
        pid = patch_id(repo, base_oid, candidate_oid)
        tree = _rev(repo, candidate_oid + "^{tree}")
        binaries = _binary_changes(repo, base_oid, candidate_oid)
    except GitError as exc:
        return _fail(ST_NEEDS_OPERATOR, B_GIT, str(exc))
    if binaries:
        return _fail(ST_NEEDS_OPERATOR, B_BINARY_CHANGE,
                     "binary changes cannot be reviewed as a diff: %s" % _paths_text(binaries), paths=binaries)
    if len(diff) > MAX_REVIEW_DIFF_BYTES:
        return _fail(ST_NEEDS_OPERATOR, B_REVIEW_UNAVAILABLE, "diff exceeds %d bytes" % MAX_REVIEW_DIFF_BYTES)
    try:
        diff, added = _redact_base_lines(diff)
    except cloud_scan.ScanRefused as exc:
        return _fail(ST_NEEDS_OPERATOR, B_REVIEW_UNAVAILABLE, "outbound scan refused: %s" % exc.reason)
    summary = _summary_text(manifest)
    try:
        findings = list(cloud_scan.scan_bytes(added, label="added-lines"))
        findings.extend(_scan_prompt_text(summary + "\n" + diff.decode("utf-8", "replace"), "review-prompt"))
    except cloud_scan.ScanRefused as exc:
        return _fail(ST_NEEDS_OPERATOR, B_REVIEW_UNAVAILABLE, "outbound scan refused: %s" % exc.reason)
    if findings:
        return _leak_result(sorted(set(findings), key=lambda f: (f.cls, f.location)))
    meta = {"family": "codex" if producer == "claude" else "claude", "diff_sha": hashlib.sha256(diff).hexdigest(),
            "patch_id": pid, "tree": tree, "base_oid": base_oid, "candidate_oid": candidate_oid}
    tmp = tempfile.mkdtemp(prefix="ffs-review-")
    try:
        diff_file, summary_file = os.path.join(tmp, "change.diff"), os.path.join(tmp, "summary.txt")
        _write_private(diff_file, diff)
        _write_private(summary_file, summary.encode("utf-8", "replace"))
        argv = ["bash", script or _script_path(), run, task, producer, diff_file, summary_file, str(generation)]
        if attempt > 0:
            argv.append(str(attempt))
        if runner is not None:
            rc, out = runner(argv, _review_env(run), REVIEW_TIMEOUT_S)
        else:  # the review's evidence store is the target repository's, never the caller's working directory
            rc, out = default_runner(argv, _review_env(run), REVIEW_TIMEOUT_S, cwd=repo)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return _review_result(rc, out, meta)


def review_model_problem(model: Dict[str, str]) -> Optional[Tuple[str, str]]:
    """``(blocker, detail)`` when the reviewer model is not provably the requested one, else ``None``.

    An approval only counts when the script named both models, they are the same model, and
    adversary-host.sh reported no tier descent. Anything else (no line, ``-``) is "unknown"."""
    requested, answered = model.get("requested", "-"), model.get("answered", "-")
    if not model or "-" in (requested, answered) or model.get("descent") not in ("0", "1"):
        return B_REVIEW_MODEL_UNKNOWN, "the review did not report which model answered (requested=%s)" % requested
    if answered != requested or model.get("descent") == "1":
        return B_REVIEW_MODEL_DOWNGRADED, "review answered by %s, not the requested judgment-tier model %s" % (answered, requested)
    return None


def _review_result(rc: int, out: str, meta: Dict[str, str]) -> StepResult:
    verdict, text, model = _classify_review_full(rc, out)
    meta = dict(meta, verdict=verdict)
    if model.get("answered", "-") != "-":
        meta["model"] = model["answered"]
    if model.get("requested", "-") != "-":
        meta["requested_model"] = model["requested"]
    excerpt = _safe_text(text, MAX_REVIEW_TEXT)
    if verdict == "APPROVE":
        problem = review_model_problem(model)
        if problem is not None:
            return StepResult(False, ST_NEEDS_OPERATOR, problem[0], problem[1], meta)
        return StepResult(True, "reviewed", None, "approved", dict(meta, review_text=excerpt))
    if verdict == "REVISE":
        return StepResult(False, ST_REJECTED, B_REVIEW_REVISE, excerpt[:MAX_DETAIL], meta)
    blocker = B_REVIEW_CAP if verdict == "cap" else B_REVIEW_UNAVAILABLE
    detail = "review %s (rc=%d)" % (verdict, rc)
    if verdict == "UNAVAILABLE":
        reason = _review_reason(text)
        if reason:
            detail += ": " + reason
    return StepResult(False, ST_NEEDS_OPERATOR, blocker, detail, meta)


_REASON_RE = re.compile(r"^CLOUD-REVIEW-REASON: ([A-Za-z0-9 ._:/=()-]{1,200})$")


def _review_reason(text: str) -> str:
    """The script's one-line reason when the reviewer never ran (round accounting failed).  Only a text that is
    exactly that line counts, so reviewer prose is never shown, and it is redacted like any other excerpt."""
    found = _REASON_RE.match(text.strip())
    return _safe_text(found.group(1), MAX_DETAIL) if found else ""


# --------------------------------------------------------------------------
# Verify
# --------------------------------------------------------------------------


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _log_tail(path: str, limit: int = 1200) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - limit))
            return _safe_text(handle.read().decode("utf-8", "replace"), limit)
    except OSError:
        return ""


def verify(
    *,
    tree_oid: str,
    cmd: str,
    repo: str,
    main_worktree: str,
    deps: Sequence[str],
    read_roots: Sequence[str] = (),
    timeout_s: float,
    log_path: str,
    other_worktrees: Sequence[str] = (),
    run_verify_fn: Optional[Callable[..., int]] = None,
) -> StepResult:
    """Run the verify command in the sandbox against ``tree_oid`` (8.2).

    A blank command (empty or whitespace-only, ``bash -c ''`` exits 0) is refused before any sandbox
    work: it would otherwise mark the tree ``verified`` without running a test."""
    if not isinstance(cmd, str) or not cmd.strip():
        return _fail(ST_NEEDS_OPERATOR, B_VERIFY_CMD_BLANK, "the effective verify command is empty; no tests were run")
    import cloud_sandbox

    runner = run_verify_fn or cloud_sandbox.run_verify
    try:
        rc = runner(tree_oid, cmd, repo=repo, main_worktree=main_worktree, deps=list(deps),
                    read_roots=list(read_roots), timeout_s=timeout_s, log_path=log_path,
                    other_worktrees=list(other_worktrees))
    except cloud_sandbox.SandboxUnavailable as exc:
        return _fail(ST_NEEDS_OPERATOR, B_SANDBOX, _safe_text(str(exc)))
    except cloud_sandbox.SandboxError as exc:
        return _fail(ST_NEEDS_OPERATOR, B_SANDBOX, "sandbox could not prepare the tree: %s" % _safe_text(str(exc)))
    data = {"test_rc": rc, "test_log_sha": _sha256_file(log_path), "tree": tree_oid}
    if rc == 0:
        return StepResult(True, "verified", None, "tests passed", data)
    return StepResult(False, ST_REJECTED, B_TESTS_FAILED, "tests failed (rc=%d): %s" % (rc, _log_tail(log_path)), data)


def verify_slot(state_dir: str, max_verifiers: int) -> Optional[Any]:
    """Non-blocking global verifier slot (14.10).

    Returns an open file holding ``flock`` on ``verify.<i>.lock`` (close it to
    release) or ``None`` when every slot is taken.
    """
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    for index in range(max(1, max_verifiers)):
        handle = open(os.path.join(state_dir, "verify.%d.lock" % index), "a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            handle.close()
    return None


# --------------------------------------------------------------------------
# Integration
# --------------------------------------------------------------------------


def _is_ancestor(repo: str, ancestor: str, descendant: str) -> bool:
    return _git_ok(repo, ["merge-base", "--is-ancestor", ancestor, descendant])


def _tree_changes(repo: str, old: str, new: str) -> Dict[str, Tuple[str, str]]:
    """``path -> (dst mode, dst oid)`` of ``old..new`` without rename detection."""
    out = _git(repo, ["diff-tree", "-r", "-z", "--raw", "--no-renames", "--no-abbrev", old, new])
    return {p[0]: (dst, oid) for _, dst, oid, _, p in _parse_raw_z(out)}


def _path_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _overlapping(left: Sequence[str], right: Sequence[str]) -> List[str]:
    """Paths of ``left`` that equal, or sit above or below, a path of ``right`` (case-folded)."""
    keys = [_path_key(p) for p in right]
    hits = []
    for path in left:
        key = _path_key(path)
        if any(key == k or k.startswith(key + "/") or key.startswith(k + "/") for k in keys):
            hits.append(path)
    return sorted(hits)


def _merge_by_index(repo: str, tip: str, candidate: str, message: str) -> Tuple[Optional[str], str]:
    """``(merge commit, "")`` or ``(None, reason)``; no worktree, no hooks, no content merge (K5).

    The reviewed change and the integration tip must touch disjoint paths. The
    merge tree is built with a temporary index (``read-tree -m --aggressive``)
    and must equal the deterministic expectation: ``tip`` with exactly the
    candidate's changed paths taken from the candidate. Anything else is
    refused, so the merge result is byte-for-byte what the reviewer saw.
    """
    base = _git(repo, ["merge-base", tip, candidate], ok_codes=(0, 1)).decode("ascii").strip()
    if not base:
        return None, "no common ancestor"
    mine = _tree_changes(repo, base, candidate)
    theirs = _tree_changes(repo, base, tip)
    overlap = _overlapping(list(mine), list(theirs))
    if overlap:
        return None, "both sides changed %s" % _paths_text(overlap)
    tmp = tempfile.mkdtemp(prefix="ffs-merge-")
    try:
        env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        try:
            _git(repo, ["read-tree", "-m", "--aggressive", base, tip, candidate], env=env)
            tree = _git(repo, ["write-tree"], env=env).decode("ascii").strip()
        except GitError:
            return None, "tree merge left unresolved entries"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if _tree_changes(repo, tip, tree) != mine:
        return None, "merged tree differs from the reviewed change"
    from delegation_router import case_collisions  # lazy: sibling module
    kept = sorted(p for p, (mode, _) in mine.items() if mode != "000000")  # both candidates passed alone
    if kept and (hit := case_collisions(kept, _tree_paths(repo, tree))):
        return None, "case-colliding paths: %s" % _paths_text(hit)
    env = dict(_BOT_ENV)
    out = _git(repo, ["commit-tree", tree, "-p", tip, "-p", candidate], stdin=(message + "\n").encode(), env=env)
    return out.decode("ascii").strip(), ""


def plan_integration(
    *,
    repo: str,
    expected_tip: str,
    candidate_oid: str,
    reviewed_patch_id: str,
    pub_ref: str,
    task: str = "task",
    reviewed_tree: str = "",
) -> StepResult:
    """Choose fast-forward or merge, check the patch-id, pin ``publish_oid``.

    Returns ``data = {mode, publish_oid, tree, expected_tip, needs_retest,
    patch_id}``. ``mode`` is ``fast-forward``, ``merge`` or ``noop``. Merge
    results need the tests re-run on ``tree``. The publication ref
    ``pub_ref`` is only written once every check has passed.

    ``reviewed_tree`` (optional), when given, must still be the candidate's tree.
    The merge path (``_merge_by_index``) yields exactly the reviewed change, so
    only the tests are re-run.
    """
    try:
        if not (OID_RE.match(expected_tip) and OID_RE.match(candidate_oid)):
            return _fail(ST_NEEDS_OPERATOR, B_GIT, "bad oid")
        if reviewed_tree and _rev(repo, candidate_oid + "^{tree}") != reviewed_tree:
            return _fail(ST_NEEDS_OPERATOR, B_PATCH_ID, "candidate tree differs from the reviewed tree")
        return _plan(repo, expected_tip, candidate_oid, reviewed_patch_id, pub_ref, task)
    except GitError as exc:
        return _fail(ST_NEEDS_OPERATOR, B_GIT, str(exc))


def _plan(repo: str, tip: str, candidate: str, reviewed: str, pub_ref: str, task: str) -> StepResult:
    if _is_ancestor(repo, candidate, tip):
        mode, publish = "noop", tip
    elif _is_ancestor(repo, tip, candidate):
        mode, publish = "fast-forward", candidate
    else:
        merged, why = _merge_by_index(repo, tip, candidate, "ffs: integrate %s" % task)
        if merged is None:
            return _fail(ST_NEEDS_OPERATOR, B_MERGE_CONFLICT,
                         "merge of %s into the integration tip refused: %s" % (task, why))
        mode, publish = "merge", merged
    actual = patch_id(repo, tip, publish)
    if mode != "noop" and actual != reviewed:
        return _fail(ST_NEEDS_OPERATOR, B_PATCH_ID, "merged change differs from the reviewed change",
                     patch_id=actual, reviewed_patch_id=reviewed, publish_oid=publish)
    _git(repo, ["update-ref", "--no-deref", pub_ref, publish])
    data = {"mode": mode, "publish_oid": publish, "tree": _rev(repo, publish + "^{tree}"),
            "expected_tip": tip, "needs_retest": mode == "merge", "patch_id": actual, "pub_ref": pub_ref}
    return StepResult(True, "integrating", None, "%s -> %s" % (mode, publish), data)


def apply_integration(*, repo: str, ref: str, publish_oid: str, expected_tip: str) -> StepResult:
    """``git update-ref <ref> <publish_oid> <expected_tip>`` (compare-and-swap)."""
    if not REF_RE.match(ref):
        return _fail(ST_NEEDS_OPERATOR, B_GIT, "refusing to move a ref outside ffs/cloud/<run>/integration")
    if not (OID_RE.match(publish_oid) and OID_RE.match(expected_tip)):
        return _fail(ST_NEEDS_OPERATOR, B_GIT, "bad oid")
    try:
        if _git(repo, ["symbolic-ref", "--quiet", ref], ok_codes=(0, 1)).strip():
            return _fail(ST_NEEDS_OPERATOR, B_GIT, "the integration ref is symbolic; refusing to move it")
        if not _is_ancestor(repo, expected_tip, publish_oid):
            return _fail(ST_NEEDS_OPERATOR, B_TIP_MOVED, "publish commit does not descend from the expected tip")
        _git(repo, ["update-ref", "--no-deref", "-m", "ffs cloud integrate", ref, publish_oid, expected_tip])
    except GitError:
        return _fail("verified", B_TIP_MOVED, "integration ref moved or missing; re-plan", ref=ref)
    return StepResult(True, "integrated", None, "ref %s at %s" % (ref, publish_oid), {"tip": publish_oid})


if __name__ == "__main__":  # pragma: no cover - library module
    sys.exit("cloud_collect is a library module")
