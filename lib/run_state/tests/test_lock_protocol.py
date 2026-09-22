"""Independent acceptance for the per-database authority lock protocol.

These tests were authored after the first protocol-v2 implementation draft.
They use disposable private stores and fixture-owned subprocesses only.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import stat
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
LIB = ROOT / "lib"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _private_store_path(tmp_path: Path) -> Path:
    parent = tmp_path / "authority"
    parent.mkdir(mode=0o700)
    return parent / "control.sqlite3"


def _protocol_paths(database: Path) -> tuple[Path, Path, Path]:
    info = database.stat()
    directory = database.parent / ".control-locks"
    stem = f"v2-{info.st_dev:x}-{info.st_ino:x}"
    return directory, directory / f"{stem}.lock", directory / f"{stem}.protocol"


def _tree(root: Path) -> dict[str, tuple[int, int, str | None]]:
    result = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        digest = _sha256(path) if stat.S_ISREG(info.st_mode) else None
        result[path.relative_to(root).as_posix()] = (
            stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), digest,
        )
    return result


def _line(process: subprocess.Popen[str], timeout: float = 5.0) -> str:
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout), "fixture child did not emit its barrier"
        line = process.stdout.readline()
    finally:
        selector.close()
    status = process.poll()
    diagnostic = ""
    if not line and status is not None and process.stderr is not None:
        selector = selectors.DefaultSelector()
        try:
            selector.register(process.stderr, selectors.EVENT_READ)
            if selector.select(0.25):
                diagnostic = os.read(process.stderr.fileno(), 65_536).decode(
                    errors="replace",
                )
        finally:
            selector.close()
    assert line, f"fixture child exited early: {status}; stderr={diagnostic!r}"
    return line.rstrip("\n")


def _stop_owned(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def test_fresh_store_provisions_one_canonical_private_protocol_per_database(
    tmp_path: Path,
) -> None:
    from run_state.state import ControlStore

    database = _private_store_path(tmp_path)
    ControlStore(database)
    directory, lock, marker = _protocol_paths(database)
    info = database.stat()
    expected = (
        json.dumps(
            {
                "database": database.name,
                "device": info.st_dev,
                "inode": info.st_ino,
                "version": 2,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert directory.stat().st_uid == os.getuid()
    assert sorted(path.name for path in directory.iterdir()) == [lock.name, marker.name]
    for path in (lock, marker):
        file_info = path.lstat()
        assert stat.S_ISREG(file_info.st_mode)
        assert file_info.st_uid == os.getuid()
        assert file_info.st_nlink == 1
        assert stat.S_IMODE(file_info.st_mode) == 0o600
    assert marker.read_bytes() == expected
    before = _tree(database.parent)

    ControlStore(database)

    assert _tree(database.parent) == before


def test_read_only_open_of_preprotocol_store_creates_nothing_and_reads_fail_closed(
    tmp_path: Path,
) -> None:
    from run_state.state import ControlStore, ControlStoreRefused

    database = _private_store_path(tmp_path)
    ControlStore(database)
    directory, _lock, _marker = _protocol_paths(database)
    shutil.rmtree(directory)
    before = _tree(database.parent)
    database_before = _sha256(database)

    view = ControlStore.open_read_only(database)

    assert _tree(database.parent) == before
    with pytest.raises(ControlStoreRefused) as refused:
        with view.read_transaction():
            pytest.fail("a protocol-less read transaction was admitted")
    assert refused.value.code == "UNSUPPORTED_SCHEMA"
    assert _tree(database.parent) == before
    assert _sha256(database) == database_before


def test_two_processes_using_the_same_database_serialize_on_one_protocol_lock(
    tmp_path: Path,
) -> None:
    from run_state.state import ControlStore

    database = _private_store_path(tmp_path)
    ControlStore(database)
    environment = dict(os.environ)
    environment.update(
        PYTHONPATH=str(LIB),
        PYTHONDONTWRITEBYTECODE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    holder_program = (
        "import pathlib,sys\n"
        "from run_state.state import ControlStore\n"
        "store=ControlStore(pathlib.Path(sys.argv[1]))\n"
        "print('holder-ready',flush=True)\n"
        "sys.stdin.readline()\n"
        "with store.transaction() as tx:\n"
        " print('holder-entered',flush=True)\n"
        " sys.stdin.readline()\n"
        " tx.execute('SELECT 1').fetchone()\n"
        "print('holder-returned',flush=True)\n"
    )
    contender_program = (
        "import pathlib,sys\n"
        "from run_state.state import ControlStore\n"
        "store=ControlStore(pathlib.Path(sys.argv[1]))\n"
        "print('contender-ready',flush=True)\n"
        "sys.stdin.readline()\n"
        "print('contender-attempted',flush=True)\n"
        "with store.transaction() as tx:\n"
        " tx.execute('SELECT 1').fetchone()\n"
        " print('contender-entered',flush=True)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_program, str(database)],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    contender = None
    try:
        contender = subprocess.Popen(
            [sys.executable, "-c", contender_program, str(database)],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert _line(holder) == "holder-ready"
        assert _line(contender) == "contender-ready"
        assert holder.stdin is not None
        holder.stdin.write("start\n")
        holder.stdin.flush()
        assert _line(holder) == "holder-entered"
        assert contender.stdin is not None
        contender.stdin.write("start\n")
        contender.stdin.flush()
        assert _line(contender) == "contender-attempted"
        assert contender.stdout is not None
        selector = selectors.DefaultSelector()
        try:
            selector.register(contender.stdout, selectors.EVENT_READ)
            assert selector.select(0.25) == [], (
                "the contender entered while the first process held the protocol lock"
            )
        finally:
            selector.close()
        holder.stdin.write("release\n")
        holder.stdin.flush()
        assert _line(holder) == "holder-returned"
        assert _line(contender) == "contender-entered"
        assert holder.wait(timeout=5) == 0
        assert contender.wait(timeout=5) == 0
    finally:
        _stop_owned(holder)
        if contender is not None:
            _stop_owned(contender)


def test_database_parent_symlink_is_never_an_authority_root(tmp_path: Path) -> None:
    from run_state.state import ControlStore, ControlStoreRefused

    real = tmp_path / "real-authority"
    real.mkdir(mode=0o700)
    alias = tmp_path / "authority-alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(ControlStoreRefused) as refused:
        ControlStore(alias / "control.sqlite3")
    assert refused.value.code == "UNSAFE_STATE_ROOT"
    assert not (real / "control.sqlite3").exists()
    assert not (real / ".control-locks").exists()


def test_database_hardlink_is_refused_without_sidecar_mutation(tmp_path: Path) -> None:
    from run_state.state import ControlStore, ControlStoreRefused

    database = _private_store_path(tmp_path)
    ControlStore(database)
    alias = database.parent / "alias.sqlite3"
    os.link(database, alias)
    before = _tree(database.parent)

    with pytest.raises(ControlStoreRefused) as refused:
        ControlStore(alias)
    assert refused.value.code == "UNSAFE_STATE_ROOT"
    assert _tree(database.parent) == before


def test_database_replacement_invalidates_existing_store_before_transaction(
    tmp_path: Path,
) -> None:
    from run_state.state import ControlStore, ControlStoreRefused

    database = _private_store_path(tmp_path)
    store = ControlStore(database)
    original = database.parent / "original.sqlite3"
    database.rename(original)
    shutil.copyfile(original, database)
    database.chmod(0o600)
    replacement_before = _sha256(database)

    with pytest.raises(ControlStoreRefused) as refused:
        with store.transaction():
            pytest.fail("a replaced database entered a writer transaction")
    assert refused.value.code == "STORE_REPLACED"
    assert _sha256(database) == replacement_before


@pytest.mark.parametrize("entry", ["lock", "protocol"])
def test_replaced_sidecar_entry_is_refused_before_transaction(
    tmp_path: Path, entry: str,
) -> None:
    from run_state.state import ControlStore, ControlStoreRefused

    database = _private_store_path(tmp_path)
    store = ControlStore(database)
    _directory, lock, marker = _protocol_paths(database)
    target = lock if entry == "lock" else marker
    retained = target.with_name(f"retained-{target.name}")
    target.rename(retained)
    shutil.copyfile(retained, target)
    target.chmod(0o600)
    database_before = _sha256(database)

    with pytest.raises(ControlStoreRefused) as refused:
        with store.transaction():
            pytest.fail("a replacement sidecar entered a writer transaction")
    assert refused.value.code == "STORE_REPLACED"
    assert _sha256(database) == database_before


@pytest.mark.parametrize("mutation", ["symlink-lock", "hardlink-lock", "open-directory"])
def test_unsafe_sidecar_shapes_are_refused_without_database_write(
    tmp_path: Path, mutation: str,
) -> None:
    from run_state.state import ControlStore, ControlStoreRefused

    database = _private_store_path(tmp_path)
    store = ControlStore(database)
    directory, lock, _marker = _protocol_paths(database)
    if mutation == "open-directory":
        directory.chmod(0o755)
    else:
        retained = lock.with_name("retained-lock")
        lock.rename(retained)
        if mutation == "symlink-lock":
            lock.symlink_to(retained.name)
        else:
            os.link(retained, lock)
    database_before = _sha256(database)

    with pytest.raises(ControlStoreRefused) as refused:
        with store.transaction():
            pytest.fail("an unsafe sidecar entered a writer transaction")
    assert refused.value.code == "UNSAFE_STATE_ROOT"
    assert _sha256(database) == database_before


@pytest.mark.parametrize("legacy_shape", ["absent", "mismatched-marker"])
def test_old_or_mismatched_writer_protocol_fails_closed_without_fallback_lock(
    tmp_path: Path, legacy_shape: str,
) -> None:
    from run_state.state import ControlStore, ControlStoreRefused

    database = _private_store_path(tmp_path)
    ControlStore(database)
    directory, _lock, marker = _protocol_paths(database)
    if legacy_shape == "absent":
        shutil.rmtree(directory)
    else:
        marker.write_bytes(b'{"version":1}\n')
    before = _tree(database.parent)
    database_before = _sha256(database)
    writer = ControlStore(database)

    with pytest.raises(ControlStoreRefused) as refused:
        with writer.transaction():
            pytest.fail("an unsupported writer protocol entered a transaction")
    assert refused.value.code == "UNSUPPORTED_SCHEMA"
    assert _tree(database.parent) == before
    assert _sha256(database) == database_before
