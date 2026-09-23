from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from run_state.recovery_docs import (
    CachedDocument,
    DocumentRequest,
    DocumentationCache,
    DocumentationCacheBusy,
    DocumentationCacheCorruption,
    DocumentationCacheMiss,
    DocumentationCacheStale,
    DocumentationFetchError,
    BoundedHTTPSFetcher,
    FetchResult,
)


SOURCE = "https://docs.example.test/releases/1.2.3/reference.html"


def test_real_transport_process_deadline_kills_blocked_open(monkeypatch):
    from run_state import recovery_docs
    original = subprocess.Popen
    children = []

    def blocked_transport(argv, **kwargs):
        assert "_fetch_transport_main" in argv[2]
        child = original([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(recovery_docs.subprocess, "Popen", blocked_transport)
    started = time.monotonic()
    with pytest.raises(DocumentationFetchError, match="DOCUMENTATION_FETCH_DEADLINE_EXCEEDED"):
        BoundedHTTPSFetcher(timeout_seconds=.05)(DocumentRequest("dep", "1", "sha", SOURCE, {}))
    assert time.monotonic() - started < 2
    assert len(children) == 1 and children[0].poll() is not None


def _cross_process_fetch(root: str, counter: str, start, outcome) -> None:
    subject = DocumentationCache(Path(root), approved_sources=[SOURCE])
    start.wait(5)

    def fetcher(_: DocumentRequest) -> FetchResult:
        descriptor = os.open(counter, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(descriptor, b"1")
        finally:
            os.close(descriptor)
        time.sleep(0.08)
        return FetchResult(b"cross-process", SOURCE)

    try:
        outcome.put(("ok", subject.get_or_fetch(request(), fetcher=fetcher).content))
    except BaseException as error:  # test worker must report failures to its parent
        outcome.put(("error", repr(error)))


def _hold_identity_lock(root: str, ready, release) -> None:
    subject = DocumentationCache(Path(root), approved_sources=[SOURCE], lock_timeout_seconds=1)
    subject._ensure_layout()
    with subject._identity_lock(subject._key(request())):
        ready.set()
        release.wait(5)


class _FakeSocket:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)


class _TricklingResponse:
    headers = {}

    def __init__(self, clock: list[float], socket: _FakeSocket) -> None:
        self.clock = clock
        self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=socket))
        self.blocks = [b"a", b"b", b""]

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def geturl(self) -> str:
        return SOURCE

    def read(self, _size: int) -> bytes:
        self.clock[0] += 0.4
        return self.blocks.pop(0)


class _SlowOpen:
    def __init__(self, clock: list[float], response: _TricklingResponse) -> None:
        self.clock = clock
        self.response = response

    def open(self, _request, *, timeout: float):
        assert timeout <= 1.0
        self.clock[0] += 0.2
        return self.response


def request(*, version: str = "1.2.3", identity: str = "sha256:package-123", mutable: bool = False,
            freshness_seconds: int | None = None) -> DocumentRequest:
    return DocumentRequest(
        dependency="example-lib", version=version, identity=identity, source_url=SOURCE,
        provenance={"resolver": "package-lock", "resolved_commit": "abc123"}, mutable=mutable,
        freshness_seconds=freshness_seconds,
    )


def cache(tmp_path: Path, *, clock=lambda: 1000.0) -> DocumentationCache:
    return DocumentationCache(tmp_path.resolve() / "docs" / "cached-docs", approved_sources=[SOURCE], clock=clock)


def test_absent_then_present_cache_returns_actual_offline_bytes(tmp_path: Path) -> None:
    subject = cache(tmp_path)
    document = request()
    with pytest.raises(DocumentationCacheMiss):
        subject.read(document)
    calls = []

    def fetcher(value: DocumentRequest) -> FetchResult:
        calls.append(value)
        return FetchResult(b"versioned docs", SOURCE)

    fetched = subject.get_or_fetch(document, fetcher=fetcher)
    offline = subject.get_or_fetch(document, offline=True)
    assert fetched.content == offline.content == b"versioned docs"
    assert not fetched.from_cache and offline.from_cache and calls == [document]
    assert fetched.metadata["request"]["version"] == "1.2.3"
    assert (subject.root / "objects" / fetched.metadata["content"]["sha256"]).read_bytes() == b"versioned docs"


def test_corrupt_content_and_wrong_version_are_rejected_offline(tmp_path: Path) -> None:
    subject = cache(tmp_path)
    saved = subject.get_or_fetch(request(), fetcher=lambda _: FetchResult(b"good", SOURCE))
    object_path = subject.root / "objects" / saved.metadata["content"]["sha256"]
    object_path.write_bytes(b"evil")
    with pytest.raises(DocumentationCacheCorruption):
        subject.get_or_fetch(request(), offline=True)
    with pytest.raises(DocumentationCacheMiss):
        subject.get_or_fetch(request(version="1.2.4", identity="sha256:package-124"), offline=True)


def test_same_identity_fetch_is_coalesced_across_concurrent_callers(tmp_path: Path) -> None:
    subject = cache(tmp_path)
    count = 0
    count_lock = threading.Lock()
    barrier = threading.Barrier(4)
    received: list[CachedDocument] = []

    def fetcher(_: DocumentRequest) -> FetchResult:
        nonlocal count
        with count_lock:
            count += 1
        time.sleep(0.04)
        return FetchResult(b"one object", SOURCE)

    def worker() -> None:
        barrier.wait()
        received.append(subject.get_or_fetch(request(), fetcher=fetcher))

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert count == 1
    assert [item.content for item in received] == [b"one object"] * 3
    assert sum(not item.from_cache for item in received) == 1


def test_identical_fetches_are_coalesced_across_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    outcome = context.Queue()
    root = tmp_path.resolve() / "docs" / "cached-docs"
    counter = tmp_path / "fetch-count"
    workers = [context.Process(target=_cross_process_fetch, args=(str(root), str(counter), start, outcome))
               for _ in range(2)]
    for worker in workers:
        worker.start()
    start.set()
    results = [outcome.get(timeout=10) for _ in workers]
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0
    assert results == [("ok", b"cross-process"), ("ok", b"cross-process")]
    assert counter.read_bytes() == b"1"


def test_live_identical_fetch_lock_has_a_bounded_typed_wait(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    root = tmp_path.resolve() / "docs" / "cached-docs"
    holder = context.Process(target=_hold_identity_lock, args=(str(root), ready, release))
    holder.start()
    assert ready.wait(5)
    calls = []
    subject = DocumentationCache(root, approved_sources=[SOURCE], lock_timeout_seconds=0.03)
    with pytest.raises(DocumentationCacheBusy):
        subject.get_or_fetch(request(), fetcher=lambda _: calls.append(1) or FetchResult(b"unexpected", SOURCE))
    assert calls == []
    release.set()
    holder.join(timeout=5)
    assert holder.exitcode == 0


def test_real_fetcher_enforces_one_total_deadline_against_a_slow_trickle() -> None:
    clock = [0.0]
    socket = _FakeSocket()
    response = _TricklingResponse(clock, socket)
    fetcher = BoundedHTTPSFetcher(
        timeout_seconds=1.0, monotonic=lambda: clock[0],
        opener_factory=lambda _redirect: _SlowOpen(clock, response),
    )
    with pytest.raises(DocumentationFetchError, match="DOCUMENTATION_FETCH_DEADLINE_EXCEEDED"):
        fetcher(request())
    # The second read gets only the total deadline's remaining 0.4 seconds;
    # it cannot reset the complete 1-second budget for every trickle block.
    assert socket.timeouts == pytest.approx([0.8, 0.4])


def test_immutable_version_reuses_verified_object_without_freshness(tmp_path: Path) -> None:
    now = [100.0]
    subject = cache(tmp_path, clock=lambda: now[0])
    subject.get_or_fetch(request(), fetcher=lambda _: FetchResult(b"immutable", SOURCE))
    now[0] += 365 * 24 * 60 * 60
    reused = subject.get_or_fetch(request(), offline=True)
    assert reused.content == b"immutable"
    assert reused.from_cache


def test_mutable_sources_require_freshness_revalidation(tmp_path: Path) -> None:
    now = [100.0]
    subject = cache(tmp_path, clock=lambda: now[0])
    mutable = request(mutable=True, freshness_seconds=10)
    subject.get_or_fetch(mutable, fetcher=lambda _: FetchResult(b"mutable", SOURCE))
    now[0] = 111.0
    with pytest.raises(DocumentationCacheStale):
        subject.get_or_fetch(mutable, offline=True)
    refreshed = subject.get_or_fetch(mutable, fetcher=lambda _: FetchResult(b"new mutable", SOURCE))
    assert refreshed.content == b"new mutable"


def test_receipt_version_tampering_and_symlink_paths_are_refused(tmp_path: Path) -> None:
    subject = cache(tmp_path)
    saved = subject.get_or_fetch(request(), fetcher=lambda _: FetchResult(b"safe", SOURCE))
    receipt = saved.receipt_path
    data = json.loads(receipt.read_text())
    data["request"]["version"] = "attacker-version"
    receipt.write_text(json.dumps(data))
    with pytest.raises(DocumentationCacheCorruption):
        subject.read(request())

    # Object paths are read with O_NOFOLLOW as well; a receipt that points at
    # a symlink never supplies offline evidence.
    clean = cache(tmp_path / "object-symlink")
    object_saved = clean.get_or_fetch(request(), fetcher=lambda _: FetchResult(b"safe", SOURCE))
    object_path = clean.root / "objects" / object_saved.metadata["content"]["sha256"]
    object_path.unlink()
    os.symlink(tmp_path / "outside", object_path)
    with pytest.raises(DocumentationCacheCorruption):
        clean.read(request())

    unsafe = tmp_path.resolve() / "unsafe-cached-docs"
    target = tmp_path.resolve() / "target"
    target.mkdir()
    os.symlink(target, unsafe)
    hostile = DocumentationCache(unsafe, approved_sources=[SOURCE])
    with pytest.raises(DocumentationCacheCorruption):
        hostile.read(request())


def test_receipt_paths_hardlinks_and_insecure_root_are_refused_before_use(tmp_path: Path) -> None:
    subject = cache(tmp_path)
    saved = subject.get_or_fetch(request(), fetcher=lambda _: FetchResult(b"safe", SOURCE))
    receipt = saved.receipt_path
    data = json.loads(receipt.read_text())
    data["content"]["sha256"] = "../outside"
    receipt.write_text(json.dumps(data))
    with pytest.raises(DocumentationCacheCorruption):
        subject.read(request())

    hardlinked = cache(tmp_path / "hardlink")
    item = hardlinked.get_or_fetch(request(), fetcher=lambda _: FetchResult(b"hardlink", SOURCE))
    object_path = hardlinked.root / "objects" / item.metadata["content"]["sha256"]
    os.link(object_path, tmp_path.resolve() / "second-link")
    with pytest.raises(DocumentationCacheCorruption):
        hardlinked.read(request())

    insecure_root = tmp_path.resolve() / "insecure" / "cached-docs"
    insecure_root.mkdir(parents=True, mode=0o700)
    insecure_root.chmod(0o777)
    with pytest.raises(DocumentationCacheCorruption):
        DocumentationCache(insecure_root, approved_sources=[SOURCE]).read(request())

    # Only a root-owned sticky ancestor (/tmp) may be world-writable.
    for index, mode in enumerate((0o777, 0o1777)):
        shared_parent = tmp_path.resolve() / f"shared-{index}"
        shared_parent.mkdir()
        shared_parent.chmod(mode)
        with pytest.raises(DocumentationCacheCorruption):
            DocumentationCache(shared_parent / "cached-docs", approved_sources=[SOURCE]).read(request())

    linked_parent_target = tmp_path.resolve() / "private-target"
    linked_parent_target.mkdir()
    linked_parent = tmp_path.resolve() / "linked-parent"
    os.symlink(linked_parent_target, linked_parent)
    with pytest.raises(DocumentationCacheCorruption):
        DocumentationCache(linked_parent / "docs" / "cached-docs", approved_sources=[SOURCE]).read(request())


def test_fetch_requires_exact_preapproved_https_source_and_exact_result_url(tmp_path: Path) -> None:
    unapproved = DocumentationCache(tmp_path.resolve() / "docs" / "cached-docs")
    with pytest.raises(DocumentationFetchError):
        unapproved.get_or_fetch(request(), fetcher=lambda _: FetchResult(b"no", SOURCE))
    subject = cache(tmp_path)
    with pytest.raises(DocumentationFetchError):
        subject.get_or_fetch(request(), fetcher=lambda _: FetchResult(b"no", SOURCE + "?redirected"))
