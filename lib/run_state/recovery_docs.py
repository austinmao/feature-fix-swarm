"""Version-bound documentation cache for diagnostic recovery.

This is intentionally a small filesystem boundary.  Recovery orchestration
supplies an already-resolved dependency identity and a source that it has
approved; this module neither resolves dependencies nor starts agents.  A
receipt always proves that the exact bytes are present locally.
"""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit, urlunsplit


_SCHEMA = "ffs.recovery-document-cache/v1"
_MAX_CONTENT_BYTES = 16 * 1024 * 1024
_MAX_MUTABLE_AGE_SECONDS = 7 * 24 * 60 * 60
_MAX_METADATA_BYTES = 128 * 1024
_MAX_WAIT_SECONDS = 60.0
_LOCK_POLL_SECONDS = 0.01
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IN_PROCESS_GUARD = threading.Lock()
_IN_PROCESS_LOCKS: dict[tuple[str, str], threading.Lock] = {}


class DocumentationCacheError(RuntimeError):
    """Base class for typed cache failures."""


class DocumentationCacheMiss(DocumentationCacheError):
    """The requested exact document is unavailable locally."""


class DocumentationCacheCorruption(DocumentationCacheError):
    """A cache entry exists but cannot prove its identity and bytes."""


class DocumentationCacheStale(DocumentationCacheError):
    """A mutable source needs revalidation before use."""


class DocumentationFetchError(DocumentationCacheError):
    """A bounded approved-source fetch did not produce safe bytes."""


class DocumentationCacheBusy(DocumentationCacheError):
    """A live identical fetch retained the per-identity cache lock too long."""


@dataclasses.dataclass(frozen=True)
class DocumentRequest:
    """An exact dependency-document identity resolved by the caller.

    ``identity`` is the caller's immutable dependency identity, such as a
    package integrity string or source commit.  It is deliberately not
    inferred from a URL or document body.
    """

    dependency: str
    version: str
    identity: str
    source_url: str
    provenance: Mapping[str, Any]
    mutable: bool = False
    freshness_seconds: int | None = None


@dataclasses.dataclass(frozen=True)
class FetchResult:
    content: bytes
    final_url: str


@dataclasses.dataclass(frozen=True)
class CachedDocument:
    content: bytes
    metadata: Mapping[str, Any]
    receipt_path: Path
    from_cache: bool


Fetcher = Callable[[DocumentRequest], FetchResult]


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise DocumentationCacheError("DOCUMENTATION_CACHE_INVALID_METADATA") from error


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_https_url(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise DocumentationCacheError("DOCUMENTATION_SOURCE_INVALID")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as error:
        raise DocumentationCacheError("DOCUMENTATION_SOURCE_INVALID") from error
    if (parts.scheme != "https" or not parts.netloc or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.fragment or port is not None or urlunsplit(parts) != value):
        raise DocumentationCacheError("DOCUMENTATION_SOURCE_INVALID")


class _RejectRedirect(urlrequest.HTTPRedirectHandler):
    """No redirect can preserve an exact approved source identity."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise DocumentationFetchError("DOCUMENTATION_REDIRECT_REFUSED")


class BoundedHTTPSFetcher:
    """Small real-fetch adapter; tests normally inject a deterministic seam.

    Redirects are bounded to zero because a redirected URL is not the exact
    source the recovery caller approved.  The transport accepts HTTPS only,
    has an enforceable total deadline, and streams at most the cache's byte
    allowance.  Each read receives only the remaining total-deadline timeout,
    so a slow trickle cannot extend collection indefinitely.
    """

    def __init__(self, *, timeout_seconds: float = 15.0, max_bytes: int = _MAX_CONTENT_BYTES,
                 monotonic: Callable[[], float] = time.monotonic,
                 opener_factory: Callable[..., Any] = urlrequest.build_opener):
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (float, int))
                or not 0 < timeout_seconds <= _MAX_WAIT_SECONDS):
            raise ValueError("timeout_seconds must be between 0 and 60")
        if not isinstance(max_bytes, int) or not 0 < max_bytes <= _MAX_CONTENT_BYTES:
            raise ValueError("max_bytes is outside the bounded cache limit")
        self.timeout_seconds = float(timeout_seconds)
        self.max_bytes = max_bytes
        self.monotonic = monotonic
        self.opener_factory = opener_factory

    def __call__(self, document: DocumentRequest) -> FetchResult:
        _validate_https_url(document.source_url)
        if self.opener_factory is not urlrequest.build_opener:
            # Explicit deterministic transport injection. Production networking
            # is always isolated so DNS, TLS and HTTP headers share the same
            # enforceable deadline as response collection.
            return self._fetch_document(document)
        started = self.monotonic()
        environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
                       "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        process = subprocess.Popen(
            [sys.executable, "-c", "from run_state.recovery_docs import _fetch_transport_main; _fetch_transport_main()",
             document.source_url, str(self.timeout_seconds), str(self.max_bytes)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=environment, start_new_session=True,
        )
        try:
            remaining = self.timeout_seconds - (self.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, self.timeout_seconds)
            content, error = process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            process.kill()
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired as unsettled:
                raise DocumentationFetchError("DOCUMENTATION_FETCH_PROCESS_UNSETTLED") from unsettled
            raise DocumentationFetchError("DOCUMENTATION_FETCH_DEADLINE_EXCEEDED") from error
        if process.returncode:
            code = error.decode("ascii", errors="replace").strip()
            if not code.startswith("DOCUMENTATION_") or not code.replace("_", "").isalnum():
                code = "DOCUMENTATION_FETCH_FAILED"
            raise DocumentationFetchError(code)
        if len(content) > self.max_bytes:
            raise DocumentationFetchError("DOCUMENTATION_CONTENT_TOO_LARGE")
        return FetchResult(content, document.source_url)

    def _fetch_document(self, document: DocumentRequest) -> FetchResult:
        _validate_https_url(document.source_url)
        started = self.monotonic()
        deadline = started + self.timeout_seconds
        opener = self.opener_factory(_RejectRedirect())
        request = urlrequest.Request(document.source_url, headers={"User-Agent": "ffs-recovery-doc-cache/1"})
        try:
            with opener.open(request, timeout=self._remaining(deadline)) as response:
                self._remaining(deadline)
                if response.geturl() != document.source_url:
                    raise DocumentationFetchError("DOCUMENTATION_SOURCE_REDIRECTED")
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdigit() or int(length) > self.max_bytes):
                    raise DocumentationFetchError("DOCUMENTATION_CONTENT_TOO_LARGE")
                parts: list[bytes] = []
                total = 0
                while True:
                    self._set_response_timeout(response, self._remaining(deadline))
                    reader = getattr(response, "read1", response.read)
                    block = reader(min(64 * 1024, self.max_bytes - total + 1))
                    self._remaining(deadline)
                    if not block:
                        break
                    total += len(block)
                    if total > self.max_bytes:
                        raise DocumentationFetchError("DOCUMENTATION_CONTENT_TOO_LARGE")
                    parts.append(block)
                return FetchResult(content=b"".join(parts), final_url=response.geturl())
        except DocumentationCacheError:
            raise
        except TimeoutError as error:
            raise DocumentationFetchError("DOCUMENTATION_FETCH_DEADLINE_EXCEEDED") from error
        except (OSError, urlerror.URLError, urlerror.HTTPError) as error:
            raise DocumentationFetchError("DOCUMENTATION_FETCH_FAILED") from error

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise DocumentationFetchError("DOCUMENTATION_FETCH_DEADLINE_EXCEEDED")
        return remaining

    @staticmethod
    def _set_response_timeout(response: Any, timeout: float) -> None:
        """Set the read socket timeout or fail closed when the transport hides it."""
        candidates = (
            getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
            getattr(getattr(response, "fp", None), "_sock", None),
        )
        for socket in candidates:
            setter = getattr(socket, "settimeout", None)
            if callable(setter):
                setter(timeout)
                return
        raise DocumentationFetchError("DOCUMENTATION_FETCH_DEADLINE_UNENFORCEABLE")


def _fetch_transport_main():
    """Fixed internal subprocess of an admitted documentation-fetch job."""
    try:
        source, timeout, max_bytes = sys.argv[1:]
        fetcher = BoundedHTTPSFetcher(timeout_seconds=float(timeout), max_bytes=int(max_bytes))
        document = DocumentRequest("transport", "exact-source", "exact-source", source, {})
        result = fetcher._fetch_document(document)
        sys.stdout.buffer.write(result.content)
    except DocumentationCacheError as error:
        sys.stderr.write(str(error))
        raise SystemExit(78) from None


class DocumentationCache:
    """A content-addressed cache rooted at the supplied ``docs/cached-docs``.

    The caller must explicitly configure permitted immutable source URLs.
    Without a configured approval set, reads are still safe but fetching is
    refused.  The cache never holds a ControlStore or workspace lock.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        approved_sources: Sequence[str] = (),
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_bytes: int = 2 * 1024 * 1024,
        max_mutable_age_seconds: int = 24 * 60 * 60,
        lock_timeout_seconds: float = 15.0,
    ):
        if not isinstance(max_bytes, int) or not 0 < max_bytes <= _MAX_CONTENT_BYTES:
            raise ValueError("max_bytes is outside the bounded cache limit")
        if (not isinstance(max_mutable_age_seconds, int) or not 0 < max_mutable_age_seconds
                <= _MAX_MUTABLE_AGE_SECONDS):
            raise ValueError("max_mutable_age_seconds is outside the bounded policy")
        raw_root = Path(root)
        if not raw_root.is_absolute():
            raise ValueError("documentation cache root must be absolute")
        self.root = raw_root
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self.max_bytes = max_bytes
        self.max_mutable_age_seconds = max_mutable_age_seconds
        if (isinstance(lock_timeout_seconds, bool) or not isinstance(lock_timeout_seconds, (float, int))
                or not 0 < lock_timeout_seconds <= _MAX_WAIT_SECONDS):
            raise ValueError("lock_timeout_seconds must be between 0 and 60")
        self.lock_timeout_seconds = float(lock_timeout_seconds)
        approved = frozenset(approved_sources)
        for source in approved:
            _validate_https_url(source)
        self.approved_sources = approved

    def read(self, document: DocumentRequest) -> CachedDocument:
        """Return verified local bytes only; never fetches."""
        self._validate_document(document)
        self._ensure_layout()
        key = self._key(document)
        return self._read_verified(document, key, from_cache=True)

    def get_or_fetch(self, document: DocumentRequest, *, fetcher: Fetcher | None = None,
                     offline: bool = False) -> CachedDocument:
        """Use verified bytes or one coalesced fetch under a per-identity lock."""
        self._validate_document(document)
        self._ensure_layout()
        key = self._key(document)
        try:
            return self._read_verified(document, key, from_cache=True)
        except (DocumentationCacheMiss, DocumentationCacheStale):
            if offline:
                raise
        # Corruption is evidence failure, never an excuse to trust a refetch.
        except DocumentationCacheCorruption:
            raise
        if fetcher is None:
            fetcher = BoundedHTTPSFetcher(max_bytes=self.max_bytes)
        self._assert_approved(document)
        with self._identity_lock(key):
            try:
                return self._read_verified(document, key, from_cache=True)
            except (DocumentationCacheMiss, DocumentationCacheStale):
                if offline:
                    raise
            result = fetcher(document)
            if not isinstance(result, FetchResult) or result.final_url != document.source_url:
                raise DocumentationFetchError("DOCUMENTATION_FETCH_IDENTITY_INVALID")
            if not isinstance(result.content, bytes) or len(result.content) > self.max_bytes:
                raise DocumentationFetchError("DOCUMENTATION_CONTENT_TOO_LARGE")
            return self._publish(document, key, result.content)

    def _validate_document(self, document: DocumentRequest) -> None:
        if not isinstance(document, DocumentRequest):
            raise DocumentationCacheError("DOCUMENTATION_REQUEST_INVALID")
        if any(not isinstance(value, str) or not value or "\0" in value
               for value in (document.dependency, document.version, document.identity)):
            raise DocumentationCacheError("DOCUMENTATION_REQUEST_INVALID")
        _validate_https_url(document.source_url)
        _canonical(dict(document.provenance))
        if not isinstance(document.mutable, bool):
            raise DocumentationCacheError("DOCUMENTATION_REQUEST_INVALID")
        if document.mutable:
            if (not isinstance(document.freshness_seconds, int) or not 0 < document.freshness_seconds
                    <= self.max_mutable_age_seconds):
                raise DocumentationCacheError("DOCUMENTATION_FRESHNESS_INVALID")
        elif document.freshness_seconds is not None:
            raise DocumentationCacheError("DOCUMENTATION_FRESHNESS_INVALID")

    def _descriptor(self, document: DocumentRequest) -> dict[str, Any]:
        return {
            "dependency": document.dependency,
            "identity": document.identity,
            "mutable": document.mutable,
            "provenance": json.loads(_canonical(dict(document.provenance))),
            "source_url": document.source_url,
            "version": document.version,
        }

    def _key(self, document: DocumentRequest) -> str:
        return _sha256(_canonical(self._descriptor(document)).encode("utf-8"))

    def _assert_approved(self, document: DocumentRequest) -> None:
        if document.source_url not in self.approved_sources:
            raise DocumentationFetchError("DOCUMENTATION_SOURCE_NOT_APPROVED")

    def _ensure_layout(self) -> None:
        with self._root_directory() as root_fd:
            for name in ("objects", "receipts", "locks", "tmp"):
                descriptor = self._open_cache_directory(root_fd, name, create=True)
                os.close(descriptor)

    @contextlib.contextmanager
    def _root_directory(self):
        """Open the absolute cache root through no-follow directory FDs.

        Every public operation opens this chain and then uses dir_fd-relative
        calls.  A parent rename after the open therefore cannot redirect an
        object, receipt, temporary, or lock write outside this cache root.
        """
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        current = -1
        try:
            current = os.open(self.root.anchor, flags)
            for index, part in enumerate(self.root.parts[1:], start=1):
                try:
                    os.mkdir(part, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                next_fd = os.open(part, flags, dir_fd=current)
                info = os.fstat(next_fd)
                # Ancestors belong to root or this user and are not group/other
                # writable, except a root-owned sticky directory such as /tmp,
                # where other users cannot rename or remove this user's
                # entries.  The cache root itself is private to this user.
                mode = stat.S_IMODE(info.st_mode)
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid())
                        or (mode & 0o022 and not (info.st_uid == 0 and mode & stat.S_ISVTX))
                        or (index == len(self.root.parts) - 1
                            and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077))):
                    os.close(next_fd)
                    raise DocumentationCacheCorruption("DOCUMENTATION_CACHE_PATH_UNSAFE")
                os.close(current)
                current = next_fd
            yield current
        except DocumentationCacheError:
            raise
        except OSError as error:
            raise DocumentationCacheCorruption("DOCUMENTATION_CACHE_PATH_UNSAFE") from error
        finally:
            if current >= 0:
                os.close(current)

    @staticmethod
    def _open_cache_directory(root_fd: int, name: str, *, create: bool = False) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            if create:
                try:
                    os.mkdir(name, 0o700, dir_fd=root_fd)
                except FileExistsError:
                    pass
            descriptor = os.open(name, flags, dir_fd=root_fd)
            info = os.fstat(descriptor)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077):
                os.close(descriptor)
                raise DocumentationCacheCorruption("DOCUMENTATION_CACHE_PATH_UNSAFE")
            return descriptor
        except DocumentationCacheError:
            raise
        except OSError as error:
            raise DocumentationCacheCorruption("DOCUMENTATION_CACHE_PATH_UNSAFE") from error

    @staticmethod
    def _read_regular_at(directory_fd: int, name: str, *, limit: int) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=directory_fd)
        except FileNotFoundError as error:
            raise DocumentationCacheMiss("DOCUMENTATION_CACHE_MISS") from error
        except OSError as error:
            raise DocumentationCacheCorruption("DOCUMENTATION_CACHE_PATH_UNSAFE") from error
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size < 0 or info.st_size > limit):
                raise DocumentationCacheCorruption("DOCUMENTATION_CACHE_PATH_UNSAFE")
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = os.read(descriptor, min(64 * 1024, limit - size + 1))
                if not chunk:
                    return b"".join(chunks)
                size += len(chunk)
                if size > limit:
                    raise DocumentationCacheCorruption("DOCUMENTATION_CACHE_CONTENT_TOO_LARGE")
                chunks.append(chunk)
        finally:
            os.close(descriptor)

    @contextlib.contextmanager
    def _identity_lock(self, key: str):
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        with _IN_PROCESS_GUARD:
            thread_lock = _IN_PROCESS_LOCKS.setdefault((str(self.root), key), threading.Lock())
        if not thread_lock.acquire(timeout=self.lock_timeout_seconds):
            raise DocumentationCacheBusy("DOCUMENTATION_CACHE_LOCK_TIMEOUT")
        root_fd = -1
        locks_fd = -1
        root_context = None
        try:
            root_context = self._root_directory()
            root_fd = root_context.__enter__()
            locks_fd = self._open_cache_directory(root_fd, "locks")
            descriptor = os.open(f"{key}.lock", flags, 0o600, dir_fd=locks_fd)
            current = os.fstat(descriptor)
            named = os.stat(f"{key}.lock", dir_fd=locks_fd, follow_symlinks=False)
            if (not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid()
                    or current.st_nlink != 1 or stat.S_IMODE(current.st_mode) & 0o077
                    or (current.st_dev, current.st_ino) != (named.st_dev, named.st_ino)):
                raise DocumentationCacheCorruption("DOCUMENTATION_CACHE_PATH_UNSAFE")
            deadline = self.monotonic() + self.lock_timeout_seconds
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - self.monotonic()
                    if remaining <= 0:
                        raise DocumentationCacheBusy("DOCUMENTATION_CACHE_LOCK_TIMEOUT")
                    self.sleep(min(_LOCK_POLL_SECONDS, remaining))
            yield
        except DocumentationCacheError:
            raise
        except OSError as error:
            raise DocumentationCacheCorruption("DOCUMENTATION_CACHE_LOCK_FAILED") from error
        finally:
            if "descriptor" in locals():
                with contextlib.suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            if locks_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(locks_fd)
            if root_context is not None:
                with contextlib.suppress(OSError):
                    root_context.__exit__(None, None, None)
            thread_lock.release()

    def _read_verified(self, document: DocumentRequest, key: str, *, from_cache: bool) -> CachedDocument:
        receipt_path = self.root / "receipts" / f"{key}.json"
        with self._root_directory() as root_fd:
            receipts_fd = self._open_cache_directory(root_fd, "receipts")
            try:
                raw = self._read_regular_at(receipts_fd, f"{key}.json", limit=_MAX_METADATA_BYTES)
            finally:
                os.close(receipts_fd)
            try:
                metadata = json.loads(raw)
            except (TypeError, ValueError) as error:
                raise DocumentationCacheCorruption("DOCUMENTATION_RECEIPT_INVALID") from error
            expected = self._descriptor(document)
            if (not isinstance(metadata, dict) or set(metadata) != {"schema", "cache_key", "request", "content", "retrieved_at"}
                    or metadata["schema"] != _SCHEMA or metadata["cache_key"] != key
                    or metadata["request"] != expected or not isinstance(metadata["content"], dict)
                    or set(metadata["content"]) != {"bytes", "sha256"}
                    or isinstance(metadata["content"]["bytes"], bool)
                    or not isinstance(metadata["content"]["bytes"], int)
                    or not 0 <= metadata["content"]["bytes"] <= self.max_bytes
                    or not isinstance(metadata["content"]["sha256"], str)
                    or _SHA256.fullmatch(metadata["content"]["sha256"]) is None
                    or isinstance(metadata["retrieved_at"], bool)
                    or not isinstance(metadata["retrieved_at"], (int, float))
                    or not math.isfinite(metadata["retrieved_at"]) or not 0 <= metadata["retrieved_at"] <= self.clock() + _MAX_WAIT_SECONDS):
                raise DocumentationCacheCorruption("DOCUMENTATION_RECEIPT_INVALID")
            objects_fd = self._open_cache_directory(root_fd, "objects")
            try:
                content = self._read_regular_at(objects_fd, metadata["content"]["sha256"], limit=self.max_bytes)
            finally:
                os.close(objects_fd)
        if len(content) != metadata["content"]["bytes"] or _sha256(content) != metadata["content"]["sha256"]:
            raise DocumentationCacheCorruption("DOCUMENTATION_CONTENT_INVALID")
        if document.mutable:
            age = self.clock() - metadata["retrieved_at"]
            if age < 0:
                raise DocumentationCacheCorruption("DOCUMENTATION_TIMESTAMP_INVALID")
            if age > document.freshness_seconds:
                raise DocumentationCacheStale("DOCUMENTATION_CACHE_STALE")
        return CachedDocument(content=content, metadata=metadata, receipt_path=receipt_path, from_cache=from_cache)

    def _write_new(self, directory: str, name: str, payload: bytes) -> bool:
        stage = f".{name}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        root_fd = -1
        destination_fd = -1
        temporary_fd = -1
        try:
            with self._root_directory() as root_fd:
                destination_fd = self._open_cache_directory(root_fd, directory)
                temporary_fd = self._open_cache_directory(root_fd, "tmp")
                descriptor = os.open(stage, flags, 0o600, dir_fd=temporary_fd)
                try:
                    self._write_all(descriptor, payload)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                try:
                    os.link(stage, name, src_dir_fd=temporary_fd, dst_dir_fd=destination_fd)
                    os.fsync(destination_fd)
                    return True
                except FileExistsError:
                    return False
        except OSError as error:
            raise DocumentationCacheCorruption("DOCUMENTATION_CACHE_WRITE_FAILED") from error
        finally:
            if temporary_fd >= 0:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(stage, dir_fd=temporary_fd)
            if destination_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(destination_fd)
            if temporary_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(temporary_fd)

    def _replace_verified(self, directory: str, name: str, payload: bytes) -> None:
        """Atomically refresh a verified receipt under an anchored root FD."""
        stage = f".{name}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        destination_fd = -1
        temporary_fd = -1
        try:
            with self._root_directory() as root_fd:
                destination_fd = self._open_cache_directory(root_fd, directory)
                temporary_fd = self._open_cache_directory(root_fd, "tmp")
                self._read_regular_at(destination_fd, name, limit=_MAX_METADATA_BYTES)
                descriptor = os.open(stage, flags, 0o600, dir_fd=temporary_fd)
                try:
                    self._write_all(descriptor, payload)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.replace(stage, name, src_dir_fd=temporary_fd, dst_dir_fd=destination_fd)
                os.fsync(destination_fd)
        except OSError as error:
            raise DocumentationCacheCorruption("DOCUMENTATION_CACHE_WRITE_FAILED") from error
        finally:
            if temporary_fd >= 0:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(stage, dir_fd=temporary_fd)
            if destination_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(destination_fd)
            if temporary_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(temporary_fd)

    @staticmethod
    def _write_all(descriptor: int, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short documentation cache write")
            view = view[written:]

    def _publish(self, document: DocumentRequest, key: str, content: bytes) -> CachedDocument:
        digest = _sha256(content)
        if not self._write_new("objects", digest, content):
            with self._root_directory() as root_fd:
                objects_fd = self._open_cache_directory(root_fd, "objects")
                try:
                    existing = self._read_regular_at(objects_fd, digest, limit=self.max_bytes)
                finally:
                    os.close(objects_fd)
            if existing != content:
                raise DocumentationCacheCorruption("DOCUMENTATION_OBJECT_CONFLICT")
        metadata = {
            "schema": _SCHEMA,
            "cache_key": key,
            "request": self._descriptor(document),
            "content": {"bytes": len(content), "sha256": digest},
            "retrieved_at": self.clock(),
        }
        encoded = _canonical(metadata).encode("utf-8")
        receipt_path = self.root / "receipts" / f"{key}.json"
        if not self._write_new("receipts", f"{key}.json", encoded):
            if document.mutable:
                self._replace_verified("receipts", f"{key}.json", encoded)
                return CachedDocument(content=content, metadata=metadata, receipt_path=receipt_path, from_cache=False)
            return self._read_verified(document, key, from_cache=True)
        return CachedDocument(content=content, metadata=metadata, receipt_path=receipt_path, from_cache=False)
