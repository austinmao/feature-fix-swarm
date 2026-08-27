"""Regression: safe_path walk must accept POSIX shared tmp (root, 1777).

CI-linux RED (2026-08-27 ship round): every fixture store under /tmp was
refused with "untrusted store path component" because /tmp carries the world
write bit. The adjudicated carve-out trusts a ROOT-owned sticky
world-writable parent; everything else keeps the full cacd9f1a refusal.
"""
import importlib.util
import os
import stat
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "takeover_io", Path(__file__).resolve().parent.parent / "scripts/gsd/takeover-io.py")
takeover_io = importlib.util.module_from_spec(_SPEC)
sys.modules["takeover_io"] = takeover_io
_SPEC.loader.exec_module(takeover_io)


def _st(mode: int, uid: int) -> os.stat_result:
    return os.stat_result((mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))


def test_root_sticky_world_writable_tmp_is_trusted():
    assert takeover_io._trusted(_st(stat.S_IFDIR | 0o1777, 0))


def test_world_writable_without_sticky_is_refused():
    assert not takeover_io._trusted(_st(stat.S_IFDIR | 0o777, 0))


def test_sticky_but_non_root_owner_is_refused():
    assert not takeover_io._trusted(_st(stat.S_IFDIR | 0o1777, os.getuid() + 1))


def test_private_own_directory_still_trusted():
    assert takeover_io._trusted(_st(stat.S_IFDIR | 0o700, os.getuid()))
