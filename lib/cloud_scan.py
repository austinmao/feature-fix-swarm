#!/usr/bin/env python3
"""Secret and path scanner for cloud delegation (contract 8.1, 14.5, 14.6).

Stdlib only, Python 3.9 compatible. A ``Finding`` carries the pattern class
and a location, never the matched value. Every public scan runs ``self_test``
first (one canary per pattern class); a failed canary, a git failure or any
other internal exception raises ``ScanRefused``, which callers treat as
"refuse". ``redact`` replaces matches with ``[REDACTED:<class>]``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "Finding",
    "ScanRefused",
    "CatFile",
    "scan_bytes",
    "scan_text_fields",
    "scan_json_decoded",
    "scan_prompt",
    "url_has_userinfo",
    "scan_git_objects",
    "scan_git_ancestry",
    "describe_finding",
    "redact",
    "self_test",
    "canary",
    "pattern_classes",
    "git_env",
    "scrub_git_env",
    "is_binary",
]

# 3: ``pem-block`` flags private-key armor only (signature and public blocks are clean).
SCANNER_VERSION = 3
GIT_TIMEOUT_S = 120
CAT_FILE_LIFETIME_S = 600
MAX_SCAN_BLOB_BYTES = 64 * 1024 * 1024
BINARY_SNIFF_BYTES = 8000
MAX_TEXT_DEPTH = 20
MAX_LOCATION_CHARS = 200
MIN_DISTINCT_CHARS = 5
# Matching budget: one ``_raw_matches`` call may run SCAN_BUDGET_S, a whole
# public scan TOTAL_BUDGET_S; exhaustion raises ``ScanRefused('budget')``.
SCAN_BUDGET_S = 30.0
TOTAL_BUDGET_S = 900.0
# Text is matched in line-aligned windows of about WINDOW bytes; a single line
# longer than that is scanned in windows overlapping by WINDOW_OVERLAP bytes
# (a secret-shaped token longer than the overlap is still found, but its span
# may be cut at a window edge).
WINDOW = 256 * 1024
WINDOW_OVERLAP = 8 * 1024
MAX_NESTED_JSON = 1 << 20
CLS_BINARY = "binary-blob"
CLS_FILENAME = "secret-filename"
_OID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_OID_LINE_RE = re.compile(rb"^([0-9a-f]{40}(?:[0-9a-f]{24})?)(?: (.*))?$", re.S)
_REDACT_FMT = "[REDACTED:{}]"
_UNSAFE_LOCATION = "<unsafe-path>"


class ScanRefused(Exception):
    """The scan could not be completed; the caller must refuse.

    ``reason`` is a short code (never content); ``findings`` carries any
    findings found before the refusal.
    """

    def __init__(self, reason: str, findings: Optional[List["Finding"]] = None):
        super().__init__(reason)
        self.reason = reason
        self.findings: List[Finding] = list(findings or [])


@dataclass(frozen=True)
class Finding:
    cls: str
    location: str


# --------------------------------------------------------------------------
# Pattern classes
# --------------------------------------------------------------------------

_SECRET_KEY_WORDS = (
    r"(?:secret|token|passw(?:or)?d|api[_-]?key|private[_-]?key"
    r"|credential|auth[_-]?key)"
)
_ASSIGNMENT = (
    r"(?i)" + _SECRET_KEY_WORDS + r"[A-Za-z0-9_.-]{0,40}[\"']?[ \t]*[:=][ \t]*"
    r"(?:\"(?P<dq>[^\"\s]{12,})\"|'(?P<sq>[^'\s]{12,})'"
    r"|(?P<u>[A-Za-z0-9+/_=-]{16,}))"
)


def _assignment_ok(match: "re.Match[bytes]") -> bool:
    """Keep literal-looking values; drop references, URLs and masked values."""
    quoted = match.group("dq") or match.group("sq")
    value = quoted if quoted is not None else match.group("u")
    if value is None:
        return False
    if value[:1] in (b"$", b"<", b"{", b"%"):
        return False
    if re.match(rb"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        return False
    if len(set(value)) < MIN_DISTINCT_CHARS:
        return False
    if quoted is None:
        return bool(re.search(rb"[0-9]", value) and re.search(rb"[A-Za-z]", value))
    return True


_Validator = Optional[Callable[["re.Match[bytes]"], bool]]

# Order matters: specific classes come first so they claim an overlapping span.
_PATTERN_SPECS: Tuple[Tuple[str, bytes, _Validator], ...] = (
    (
        # Private-key armor only: the label must contain ``PRIVATE KEY`` or ``SECRET KEY`` (RSA/EC/DSA/
        # OPENSSH/ENCRYPTED/PGP ... PRIVATE KEY [BLOCK], PGP SECRET KEY BLOCK). Signature, public-key and certificate
        # blocks are not secrets: signed commits and tags carry a PGP/SSH SIGNATURE block
        # in a ``gpgsig`` header. The lookahead stays inside one label run, so a scan is
        # linear; an unterminated private block still runs to the end of the input.
        "pem-block",
        rb"-----BEGIN (?=[A-Z0-9 ]*(?:PRIVATE|SECRET) KEY)[A-Z0-9][A-Z0-9 ]*-----"
        rb"(?s:.*?)(?:-----END [A-Z0-9][A-Z0-9 ]*-----|\Z)",
        None,
    ),
    ("sk-ant", rb"\bsk-ant-[A-Za-z0-9_-]{8,}", None),
    ("sk-proj", rb"\bsk-proj-[A-Za-z0-9_-]{8,}", None),
    ("sk-key", rb"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{15,}", None),
    ("aws-access-key", rb"\bAKIA[0-9A-Z]{16}\b", None),
    ("github-pat", rb"\bgithub_pat_[A-Za-z0-9_]{16,}", None),
    ("github-token", rb"\bgh[pousr]_[A-Za-z0-9]{16,}", None),
    ("slack-token", rb"\bxox[abpr]-[A-Za-z0-9-]{8,}", None),
    ("doppler-token", rb"\bdp\.(?:st|ct|sa|pt)\.[A-Za-z0-9._-]{16,}", None),
    (
        "jwt",
        rb"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*",
        None,
    ),
    (
        "credential-url",
        # user:pass@ on any scheme, or a bare token@ on http(s) (ssh://git@ is not a secret)
        rb"\b[A-Za-z][A-Za-z0-9+.-]{0,19}://[^/\s:@]+:[^/\s@]+@"
        rb"|(?i:\b(?:[a-z][a-z0-9.-]{0,9}\+)?https?://[^/\s:@]+@)",
        None,
    ),
    ("secret-assignment", _ASSIGNMENT.encode("ascii"), _assignment_ok),
)
_PATTERNS: Tuple[Tuple[str, "re.Pattern[bytes]", _Validator], ...] = tuple(
    (cls, re.compile(rx), check) for cls, rx, check in _PATTERN_SPECS
)

_FILENAME_RES = (
    re.compile(r"^\.env"),
    re.compile(r"^id_rsa"),
    re.compile(r"\.pem$"),
)
# Template names carry no secrets by convention; their content is still scanned.
_ALLOWED_FILENAMES = frozenset((".env.example", ".env.sample", ".env.template", ".env.dist"))


def pattern_classes() -> List[str]:
    """Every class the scanner can emit from byte content or path names."""
    return [cls for cls, _, _ in _PATTERN_SPECS] + [CLS_FILENAME]


def canary(cls: str) -> bytes:
    """A synthetic sample of ``cls``, assembled at runtime (never a literal)."""
    table: Dict[str, str] = {
        "pem-block": "-----BEGIN " + "RSA PRIVATE" + " KEY-----",
        "sk-ant": "sk-" + "ant-" + "A1b2C3d4E5f6",
        "sk-proj": "sk-" + "proj-" + "A1b2C3d4E5f6",
        "sk-key": "sk-" + "A1b2C3d4E5f6G7h8I9j0K1",
        "aws-access-key": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
        "github-token": "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0",
        "github-pat": "github" + "_pat_" + "A1b2C3d4E5f6G7h8I9j0",
        "slack-token": "xo" + "xb-" + "1234567890ab",
        "doppler-token": "dp." + "st." + "dev." + "A1b2C3d4E5f6G7h8I9j0",
        "jwt": "ey" + "Jabcd" + "." + "ey" + "Jefgh" + "." + "sig12",
        "credential-url": "https" + "://user" + ":pw1" + "@host.invalid/",
        "secret-assignment": "api_" + "key" + ' = "' + "A1b2C3d4E5f6G7" + '"',
        CLS_FILENAME: ".en" + "v",
    }
    return table[cls].encode("ascii")


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:BINARY_SNIFF_BYTES]


class _Budget:
    """A wall-clock limit shared by the loops of one matching call."""

    def __init__(self, seconds: float):
        deadline = time.monotonic() + seconds
        outer = getattr(_LOCAL, "deadline", None)
        self.deadline = deadline if outer is None else min(deadline, outer)

    def check(self) -> None:
        if time.monotonic() > self.deadline:
            raise ScanRefused("budget")


_LOCAL = threading.local()


def _windows(data: bytes) -> Iterable[Tuple[int, int, int]]:
    """``(lo, hi, accept)`` windows covering ``data``.

    Windows end on a newline when they can; a line longer than ``WINDOW`` is
    covered by windows overlapping by ``WINDOW_OVERLAP``. A match belongs to
    the window in which it starts below ``accept``.
    """
    size, pos = len(data), 0
    while pos < size:
        end = min(size, pos + WINDOW)
        if end == size:
            yield pos, size, size
            return
        newline = data.rfind(b"\n", pos, end)
        if newline >= pos:
            yield pos, newline + 1, newline + 1
            pos = newline + 1
        else:
            yield pos, end, end - WINDOW_OVERLAP
            pos = end - WINDOW_OVERLAP


def _class_spans(
    rx: "re.Pattern[bytes]", check: _Validator, data: bytes, budget: _Budget, whole: bool
) -> List[Tuple[int, int]]:
    """Ordered, non-overlapping spans of one class (windowed unless ``whole``)."""
    spans: List[Tuple[int, int]] = []
    last_end = -1
    windows = [(0, len(data), len(data))] if whole else _windows(data)
    for lo, hi, accept in windows:
        budget.check()
        for index, match in enumerate(rx.finditer(data, lo, hi)):
            if index % 512 == 0:
                budget.check()
            start, end = match.span()
            if start >= accept and accept < hi:
                break
            if start < last_end or (check is not None and not check(match)):
                continue
            spans.append((start, end))
            last_end = end
    return spans


def _merge_class(
    claimed: List[Tuple[str, int, int]], cls: str, spans: List[Tuple[int, int]]
) -> List[Tuple[str, int, int]]:
    """Add ``spans`` of ``cls`` to ``claimed`` (both sorted); earlier classes win."""
    kept: List[Tuple[str, int, int]] = []
    pointer = 0
    for start, end in spans:
        while pointer < len(claimed) and claimed[pointer][2] <= start:
            pointer += 1
        if pointer < len(claimed) and claimed[pointer][1] < end:
            continue
        kept.append((cls, start, end))
    return sorted(claimed + kept, key=lambda item: (item[1], item[2]))


def _raw_matches(data: bytes) -> List[Tuple[str, int, int]]:
    """Non-overlapping (class, start, end) spans, earliest class wins.

    Linear in ``len(data)`` per class: line-aligned windows, one ordered pass
    per class and a two-pointer merge (no pairwise overlap checks), under a
    wall-clock budget (``ScanRefused('budget')``).
    """
    budget = _Budget(SCAN_BUDGET_S)
    claimed: List[Tuple[str, int, int]] = []
    for cls, rx, check in _PATTERNS:
        spans = _class_spans(rx, check, data, budget, whole=(cls == "pem-block"))
        if spans:
            claimed = _merge_class(claimed, cls, spans)
    return claimed


def _filename_hit(name: str) -> bool:
    if name.lower() in _ALLOWED_FILENAMES:
        return False
    return any(rx.search(name) for rx in _FILENAME_RES)


def url_has_userinfo(url: str) -> bool:
    """True when a scheme-style URL carries userinfo (``user:pw@`` or ``tok@``).

    The authority ends at the first ``/``, ``?`` or ``#``; a backslash does
    not end it, so ``https://good\\@evil`` counts as userinfo. Scp-style
    remotes (``git@host:path``) have no ``://`` and return ``False``.
    """
    if not isinstance(url, str) or "://" not in url:
        return False
    rest = url.split("://", 1)[1]
    return "@" in re.split(r"[/?#]", rest, maxsplit=1)[0]


def self_test() -> None:
    """Run one canary per class; raise ``ScanRefused`` on any miss."""
    try:
        for cls, _, _ in _PATTERN_SPECS:
            hits = {c for c, _, _ in _raw_matches(b"x " + canary(cls) + b" y")}
            if cls not in hits:
                raise ScanRefused("self-test")
        if not _filename_hit(canary(CLS_FILENAME).decode("ascii")):
            raise ScanRefused("self-test")
        if _filename_hit(".env" + ".example"):
            raise ScanRefused("self-test")
        bare = "https" + "://tok" + "en123@host.invalid/"
        if "credential-url" not in {c for c, _, _ in _raw_matches(bare.encode("ascii"))}:
            raise ScanRefused("self-test")
        if not url_has_userinfo(bare) or url_has_userinfo("https://host.invalid/a@b"):
            raise ScanRefused("self-test")
        if _raw_matches(b"hello world, nothing secret in this line\n"):
            raise ScanRefused("self-test")
    except ScanRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - any failure refuses
        raise ScanRefused("self-test") from exc


def _guard(func: Callable[..., Any]) -> Callable[..., Any]:
    """Public-scan wrapper: self-test first, any stray exception refuses."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        outermost = getattr(_LOCAL, "deadline", None) is None
        if outermost:
            _LOCAL.deadline = time.monotonic() + TOTAL_BUDGET_S
        try:
            self_test()
            return func(*args, **kwargs)
        except ScanRefused:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ScanRefused("internal-" + type(exc).__name__) from None
        finally:
            if outermost:
                _LOCAL.deadline = None

    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    return wrapper


# --------------------------------------------------------------------------
# Byte, text and path scanning
# --------------------------------------------------------------------------


def _safe_text(raw: bytes) -> str:
    """A printable rendering of ``raw`` that is itself clean, else a marker."""
    if _raw_matches(raw):
        return _UNSAFE_LOCATION
    text = raw.decode("utf-8", "replace")
    if not text.isprintable():
        return _UNSAFE_LOCATION
    return text[:MAX_LOCATION_CHARS]


def _scan_raw(data: bytes, label: str) -> List[Finding]:
    findings: List[Finding] = []
    line, cursor = 1, 0
    for cls, start, _ in _raw_matches(data):  # sorted: count newlines incrementally
        line += data.count(b"\n", cursor, start)
        cursor = start
        findings.append(Finding(cls, "%s:L%d" % (label, line)))
    return findings


def _scan_path(path: bytes, oid: str) -> List[Finding]:
    """Findings for a pathname; the location never echoes a dirty path."""
    findings = [Finding(cls, "path:%s" % oid) for cls, _, _ in _raw_matches(path)]
    base = path.rsplit(b"/", 1)[-1].decode("utf-8", "replace")
    if _filename_hit(base):
        findings.append(Finding(CLS_FILENAME, "path:%s:%s" % (oid, _safe_text(path))))
    return findings


@_guard
def scan_bytes(data: bytes, *, label: str = "") -> List[Finding]:
    """Scan ``data``; locations are ``<label>:L<line>``."""
    return _scan_raw(bytes(data), _safe_text(label.encode("utf-8")) if label else "data")


class _Pairs(list):
    """A JSON object as ``(key, value)`` pairs, so duplicate keys all survive."""


_NOT_JSON = object()


def _parse_json(raw: Any) -> Any:
    """Decoded JSON (objects as ``_Pairs``) or ``_NOT_JSON``; never raises."""
    try:
        return json.loads(raw, object_pairs_hook=_Pairs)
    except (ValueError, RecursionError):
        return _NOT_JSON


def _walk_text(obj: Any, path: str, depth: int, out: List[Finding]) -> None:
    if depth > MAX_TEXT_DEPTH:
        raise ScanRefused("depth")
    if isinstance(obj, str):
        out.extend(_scan_raw(obj.encode("utf-8", "replace"), path))
        if obj[:1] in ("{", "[", '"') and len(obj) <= MAX_NESTED_JSON:
            nested = _parse_json(obj)  # a JSON document carried in a string
            if nested is not _NOT_JSON:
                _walk_text(nested, path + "<json>", depth + 1, out)
    elif isinstance(obj, (dict, _Pairs)):
        for index, (key, value) in enumerate(obj.items() if isinstance(obj, dict) else obj):
            key_text = str(key).encode("utf-8", "replace")
            out.extend(_scan_raw(key_text, "%s.<key%d>" % (path, index)))
            _walk_text(value, "%s.%s" % (path, _safe_text(key_text)), depth + 1, out)
    elif isinstance(obj, (list, tuple)):
        for index, item in enumerate(obj):
            _walk_text(item, "%s[%d]" % (path, index), depth + 1, out)


@_guard
def scan_text_fields(obj: Any, *, label: str = "field") -> List[Finding]:
    """Scan every decoded string key and value of a JSON-like structure.

    ``obj`` may also be JSON ``bytes``/``str`` text. Values are scanned after
    JSON decoding (so ``\\u0073k-...`` escapes are seen as ``sk-...``), and a
    string value that is itself a JSON document is decoded and walked too.
    """
    out: List[Finding] = []
    name = _safe_text(label.encode("utf-8"))
    if isinstance(obj, (bytes, bytearray)):
        obj = bytes(obj).decode("utf-8", "replace")
    _walk_text(obj, name, 0, out)
    return _dedupe(out)


@_guard
def scan_json_decoded(raw_bytes: bytes, *, label: str = "json") -> List[Finding]:
    """Scan JSON bytes raw AND decoded (keys, string values, nested JSON).

    Text that is not valid JSON raises ``ScanRefused('bad-json')`` carrying
    the raw-byte findings.
    """
    raw = bytes(raw_bytes)
    name = _safe_text(label.encode("utf-8"))
    findings = _scan_raw(raw, name)
    decoded = _parse_json(raw.decode("utf-8", "replace"))
    if decoded is _NOT_JSON:
        raise ScanRefused("bad-json", _dedupe(findings))
    _walk_text(decoded, name + ".decoded", 0, findings)
    return _dedupe(findings)


_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})")


def _unescape(text: str) -> str:
    return _ESCAPE_RE.sub(lambda m: chr(int(m.group(1) or m.group(2), 16)), text)


@_guard
def scan_prompt(text: Any, *, label: str = "prompt") -> List[Finding]:
    """Scan an assembled outbound prompt (``str`` or ``bytes``).

    The text is scanned as sent and again with ``\\uXXXX``/``\\xXX`` escapes
    decoded, so a credential a model could un-escape is still found.
    """
    raw = text.encode("utf-8", "replace") if isinstance(text, str) else bytes(text)
    name = _safe_text(label.encode("utf-8"))
    findings = _scan_raw(raw, name)
    plain = raw.decode("utf-8", "replace")
    if "\\u" in plain or "\\x" in plain:
        findings.extend(_scan_raw(_unescape(plain).encode("utf-8", "replace"), name))
    return _dedupe(findings)


@_guard
def redact(data: bytes) -> bytes:
    """Replace each match with ``[REDACTED:<class>]``."""
    raw = bytes(data)
    pieces: List[bytes] = []
    cursor = 0
    for cls, start, end in _raw_matches(raw):
        pieces.append(raw[cursor:start])
        pieces.append(_REDACT_FMT.format(cls).encode("ascii"))
        cursor = end
    pieces.append(raw[cursor:])
    return b"".join(pieces)


# --------------------------------------------------------------------------
# git plumbing
# --------------------------------------------------------------------------


# Variables that select a repository or change git's behaviour; no cloud git subprocess may inherit them from the
# operator's shell (an absolute GIT_DIR would redirect a quarantine ``git init``/``fetch`` into the main checkout).
GIT_STRIP_ENV = frozenset((
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_CONFIG",
    "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_EXEC_PATH", "GIT_SSH_COMMAND", "GIT_SSH", "GIT_EXTERNAL_DIFF",
    "GIT_PREFIX", "GIT_QUARANTINE_PATH", "GIT_REPLACE_REF_BASE", "GIT_SHALLOW_FILE", "GIT_GRAFT_FILE",
    "GIT_ATTR_SOURCE", "GIT_PROXY_COMMAND", "GIT_TEMPLATE_DIR", "GIT_DEFAULT_HASH", "GIT_DEFAULT_REF_FORMAT",
    "GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS",
))
GIT_STRIP_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


def scrub_git_env(env: Dict[str, str]) -> Dict[str, str]:
    """``env`` without the repository-selecting and behaviour-changing git variables (the one shared helper).

    ``GIT_NO_REPLACE_OBJECTS=1`` is forced: a ``refs/replace`` ref must never change which bytes the scanner or
    the collector read (``cat-file`` honours replace refs, ``pack-objects`` does not). Config-file overrides
    (``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM``) are kept: ``HOME`` is inherited anyway."""
    out = {k: v for k, v in env.items() if k not in GIT_STRIP_ENV and not k.startswith(GIT_STRIP_PREFIXES)}
    out["GIT_NO_REPLACE_OBJECTS"] = "1"
    return out


def git_env() -> Dict[str, str]:
    env = scrub_git_env(dict(os.environ))
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
    )
    return env


def _git(repo: str, args: Sequence[str], stdin: Optional[bytes] = None) -> bytes:
    argv = ["git", "-C", repo] + list(args)
    try:
        proc = subprocess.run(
            argv,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_env(),
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise ScanRefused("git-error") from None
    if proc.returncode != 0:
        raise ScanRefused("git-error")
    return proc.stdout


class CatFile:
    """``git cat-file --batch`` as a request/response object."""

    def __init__(self, repo: str, lifetime_s: int = CAT_FILE_LIFETIME_S):
        self._proc = subprocess.Popen(
            ["git", "-C", repo, "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=git_env(),
        )
        self._timer = threading.Timer(lifetime_s, self._proc.kill)
        self._timer.daemon = True
        self._timer.start()

    def read(
        self, spec: str, max_bytes: int = MAX_SCAN_BLOB_BYTES
    ) -> Optional[Tuple[str, int, bytes]]:
        """Return ``(type, size, data)``; ``None`` when missing.

        ``data`` is empty when ``size`` exceeds ``max_bytes`` (content is
        drained, not returned).
        """
        stdin, stdout = self._proc.stdin, self._proc.stdout
        if stdin is None or stdout is None:
            raise ScanRefused("git-error")
        try:
            stdin.write(spec.encode("utf-8") + b"\n")
            stdin.flush()
            header = stdout.readline().split()
            if len(header) == 2 and header[1] == b"missing":
                return None
            if len(header) != 3:
                raise ScanRefused("git-error")
            size = int(header[2])
            data = self._take(stdout, size, size <= max_bytes)
            stdout.read(1)
        except (OSError, ValueError):
            raise ScanRefused("git-error") from None
        return header[1].decode("ascii"), size, data

    @staticmethod
    def _take(stream: Any, size: int, keep: bool) -> bytes:
        chunks: List[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = stream.read(min(remaining, 1 << 20))
            if not chunk:
                raise ScanRefused("git-error")
            remaining -= len(chunk)
            if keep:
                chunks.append(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        self._timer.cancel()
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
            self._proc.wait(timeout=GIT_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError):
            self._proc.kill()
        if self._proc.stdout is not None:
            self._proc.stdout.close()

    def __enter__(self) -> "CatFile":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _check_oid(value: str) -> str:
    if not isinstance(value, str) or not _OID_RE.match(value):
        raise ScanRefused("bad-oid")
    return value


def _tree_names(raw: bytes, oid_len: int) -> List[bytes]:
    """Entry names of a raw tree object (``<mode> <name>\\0<binary oid>``)."""
    names: List[bytes] = []
    pos = 0
    while pos < len(raw):
        space = raw.index(b" ", pos)
        nul = raw.index(b"\0", space)
        names.append(raw[space + 1 : nul])
        pos = nul + 1 + oid_len
    return names


def _list_objects(
    repo: str, tip: str, exclude: Iterable[str]
) -> Tuple[List[Tuple[str, bytes]], List[str], List[bytes]]:
    """``rev-list --objects`` as ([(oid, path)], missing oids, odd lines)."""
    lines = [tip] + ["^" + oid for oid in exclude]
    out = _git(
        repo,
        ["rev-list", "--objects", "--missing=print", "--stdin"],
        ("\n".join(lines) + "\n").encode("ascii"),
    )
    objects: List[Tuple[str, bytes]] = []
    missing: List[str] = []
    odd: List[bytes] = []
    for line in out.split(b"\n"):
        if not line:
            continue
        if line.startswith(b"?"):
            missing.append(line[1:].decode("ascii", "replace"))
            continue
        match = _OID_LINE_RE.match(line)
        if match is None:
            odd.append(line)
            continue
        objects.append((match.group(1).decode("ascii"), match.group(2) or b""))
    return objects, missing, odd


def _scan_object(
    cat: CatFile, oid: str, path: bytes, outbound: bool
) -> Tuple[List[Finding], bool]:
    """Scan one object; returns (findings, missing)."""
    got = cat.read(oid)
    if got is None:
        return [], True
    kind, size, data = got
    if size > MAX_SCAN_BLOB_BYTES:
        raise ScanRefused("oversize")
    findings: List[Finding] = []
    if path:
        findings.extend(_scan_path(path, oid))
    if kind == "tree":
        prefix = path + b"/" if path else b""
        for name in _tree_names(data, len(oid) // 2):
            findings.extend(_scan_path(prefix + name, oid))
        return findings, False
    label = "%s:%s" % (kind, oid)
    if kind == "blob":
        label = "blob:%s:%s" % (oid, _safe_text(path)) if path else label
        if outbound and is_binary(data):
            findings.append(Finding(CLS_BINARY, label))
    findings.extend(_scan_raw(data, label))
    return findings, False


def _scan_objects(
    repo: str, tip: str, exclude: Iterable[str], outbound: bool
) -> List[Finding]:
    objects, missing, odd = _list_objects(repo, tip, exclude)
    findings: List[Finding] = []
    for line in odd:
        findings.extend(_scan_raw(line, "path:unparsed"))
    unreadable = list(missing)
    with CatFile(repo) as cat:
        for oid, path in objects:
            found, gone = _scan_object(cat, oid, path, outbound)
            findings.extend(found)
            if gone:
                unreadable.append(oid)
    if unreadable:
        raise ScanRefused("missing-object", _dedupe(findings))
    return _dedupe(findings)


def _dedupe(findings: Iterable[Finding]) -> List[Finding]:
    return sorted(set(findings), key=lambda f: (f.cls, f.location))


@_guard
def scan_git_objects(
    repo: str, base: Optional[str], tip: str, *, outbound: bool = False
) -> List[Finding]:
    """Scan every object new in ``base..tip``: blobs, commit messages, paths.

    Uses ``git rev-list --objects`` so merge-only content and intermediate
    commits are covered. ``outbound=True`` also flags binary blobs. Missing
    objects (partial fetch) raise ``ScanRefused('missing-object')`` carrying
    any findings made so far.
    """
    _check_oid(tip)
    exclude = [_check_oid(base)] if base else []
    return _scan_objects(repo, tip, exclude, outbound)


# --------------------------------------------------------------------------
# Ancestry scan with an OID-keyed cache (14.6)
# --------------------------------------------------------------------------


def _allow_signature(allow: Sequence[Dict[str, str]]) -> str:
    triples = sorted(
        (str(a.get("blob", "")), str(a.get("path", "")), str(a.get("class", ""))) for a in allow
    )
    return hashlib.sha256(json.dumps(triples).encode("utf-8")).hexdigest()[:16]


def _load_cache(path: str) -> Dict[str, Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(path: str, cache: Dict[str, Dict[str, Any]]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".scan-cache.", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        return


def _entry_clean(entry: Any, allow_sig: str) -> bool:
    if not isinstance(entry, dict) or entry.get("clean") is not True:
        return False
    if entry.get("v") != SCANNER_VERSION:
        return False
    return entry.get("allow") == allow_sig  # a missing or different signature is never trusted


def _blob_oid(location: str) -> str:
    parts = location.split(":")
    return parts[1] if len(parts) > 1 and parts[0] == "blob" else ""


_BLOB_LOCATION_RE = re.compile(r"^blob:[0-9a-f]+:(?P<path>.*):L\d+$", re.S)


def _location_path(location: str) -> str:
    """The repository path a finding location names (``""`` when it has none)."""
    if location.startswith("path:"):
        parts = location.split(":", 2)
        return parts[2] if len(parts) == 3 else ""
    match = _BLOB_LOCATION_RE.match(location)
    return match.group("path") if match else ""


def _apply_allow(findings: List[Finding], allow: Sequence[Dict[str, str]]) -> List[Finding]:
    """Drop findings named by ``{"blob", "class"}`` or ``{"path", "class"}`` entries."""
    by_blob = {(str(a["blob"]), str(a.get("class", ""))) for a in allow if a.get("blob")}
    by_path = {(str(a["path"]), str(a.get("class", ""))) for a in allow if a.get("path")}
    return [
        f for f in findings
        if (_blob_oid(f.location), f.cls) not in by_blob
        and ((_location_path(f.location) or None), f.cls) not in by_path
    ]


_LOCATION_RE = re.compile(
    r"^(?P<kind>blob|commit|tag|tree|path):(?P<oid>[0-9a-f]{40,64}|unparsed)(?::(?P<rest>.*))?$", re.S
)
_LINE_SUFFIX_RE = re.compile(r"^(?P<head>.*?)(?::?L(?P<line>\d+))?$", re.S)


def describe_finding(finding: Finding) -> Dict[str, Any]:
    """A structured, value-free record of one finding.

    ``kind`` is the object kind (``blob``, ``commit``, ``tag``, ``tree``) or
    ``path`` for a finding about a pathname; ``oid`` is that object's id,
    ``path`` the repository path (or ``None``) and ``line`` the 1-based line
    (or ``None``). ``allow_entry`` is the ``scan.allow`` entry that would
    allow the finding, or ``None`` when no entry can (commit and tag objects).
    The matched value is never part of a finding, so it is never here.
    """
    record: Dict[str, Any] = {
        "class": finding.cls, "location": finding.location,
        "kind": None, "oid": None, "path": None, "line": None, "allow_entry": None,
    }
    match = _LOCATION_RE.match(finding.location)
    if match is None:
        return record
    rest = match.group("rest") or ""
    kind = match.group("kind")
    head = rest
    if kind in ("blob", "commit", "tag"):  # only these carry ``:L<n>``; a path finding's name is the whole rest
        suffix = _LINE_SUFFIX_RE.match(rest)
        head = suffix.group("head") if suffix else rest
        if suffix and suffix.group("line") is not None:
            record["line"] = int(suffix.group("line"))
    record["kind"] = kind
    record["oid"] = match.group("oid") if match.group("oid") != "unparsed" else None
    record["path"] = head or None
    if kind == "blob":
        record["allow_entry"] = {"blob": record["oid"], "class": finding.cls}
    elif record["path"] and record["path"] != _UNSAFE_LOCATION and len(record["path"]) < MAX_LOCATION_CHARS:
        # never a catch-all (``<unsafe-path>``) or a truncated path: neither names the real file
        record["allow_entry"] = {"path": record["path"], "class": finding.cls}
    return record


@_guard
def scan_git_ancestry(
    repo: str,
    tip: str,
    cache_path: str,
    allow: Sequence[Dict[str, str]] = (),
) -> List[Finding]:
    """Scan the full ancestry of ``tip`` (outbound rules), caching by commit.

    ``allow`` entries are ``{"blob": <oid>, "class": <pattern class>}`` or
    ``{"path": <repo path>, "class": <pattern class>}`` (a filename finding, or
    any finding inside a file at that path). A
    commit cached as clean under this exact ``allow`` signature means everything
    reachable from it was clean, so a later scan excludes it and each base is
    scanned once; an entry with another or no signature is ignored.
    """
    _check_oid(tip)
    commits = _git(repo, ["rev-list", tip]).decode("ascii").split()
    cache = _load_cache(cache_path)
    allow_sig = _allow_signature(allow)
    clean = [c for c in commits if _entry_clean(cache.get(c), allow_sig)]
    clean_set = set(clean)
    fresh = [c for c in commits if c not in clean_set]
    if not fresh:
        return []
    raw = _scan_objects(repo, tip, clean, True)
    kept = _apply_allow(raw, allow)
    for oid in fresh:
        cache[oid] = {"clean": not kept, "findings": len(kept), "v": SCANNER_VERSION, "allow": allow_sig}
    _save_cache(cache_path, cache)
    return kept
