"""GH-152: clamp + anti-drift coverage for the takeover snapshot/read ceiling.

The takeover snapshot/read/record path hard-caps a consumer shared evidence
store at a literal in eleven places across four files. This pins the shape
every one of the four standalone mirrors must agree on: default 32 MiB,
overridable by FFS_TAKEOVER_SNAPSHOT_MAX_BYTES, clamped to [1 MiB, 256 MiB],
garbage/empty/absent falls back to the default. The ceiling is never a
sentinel-driven off switch.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MAX = 33554432
FLOOR = 1048576
CEIL = 268435456

CLAMP_CASES = [
    (None, DEFAULT_MAX),
    ("", DEFAULT_MAX),
    ("abc", DEFAULT_MAX),
    ("1.5", DEFAULT_MAX),
    ("-1", FLOOR),
    ("0", FLOOR),
    ("512", FLOOR),
    ("1048576", FLOOR),
    ("2097152", 2097152),
    ("999999999", CEIL),
    (" 2097152 ", 2097152),
]

AGREEMENT_CASES = [None, "2097152", "garbage", "999999999"]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _gates():
    return _load("takeover_max_bytes_gates", ROOT / "lib" / "gates.py")


def _takeover_io():
    return _load("takeover_max_bytes_io", ROOT / "scripts" / "gsd" / "takeover-io.py")


def _takeover_transaction():
    return _load("takeover_max_bytes_transaction", ROOT / "scripts" / "gsd" / "takeover-transaction.py")


def _set_env(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("FFS_TAKEOVER_SNAPSHOT_MAX_BYTES", raising=False)
    else:
        monkeypatch.setenv("FFS_TAKEOVER_SNAPSHOT_MAX_BYTES", value)


_SHELL_HELPER_START = "# GH-152-CEILING-HELPER-START"
_SHELL_HELPER_END = "# GH-152-CEILING-HELPER-END"


def _shell_helper_value(env_value):
    """Extract the shell mirror's ceiling helper and invoke it standalone.

    The helper lives inline inside scripts/gsd/takeover-check.sh's existing
    validation heredoc, bracketed by marker comments, so it can be pulled out
    and executed in isolation under a controlled environment. A drifted
    fourth mirror (wrong default, wrong floor/cap, or a disable sentinel)
    fails this exact extraction+execution round trip.
    """
    script = ROOT / "scripts" / "gsd" / "takeover-check.sh"
    text = script.read_text()
    start = text.index(_SHELL_HELPER_START) + len(_SHELL_HELPER_START)
    end = text.index(_SHELL_HELPER_END)
    snippet = text[start:end]
    env = os.environ.copy()
    if env_value is None:
        env.pop("FFS_TAKEOVER_SNAPSHOT_MAX_BYTES", None)
    else:
        env["FFS_TAKEOVER_SNAPSHOT_MAX_BYTES"] = env_value
    result = subprocess.run(
        [sys.executable, "-c", snippet + "\nprint(_takeover_snapshot_max_bytes())"],
        env=env, capture_output=True, text=True, check=True,
    )
    return int(result.stdout.strip())


@pytest.mark.parametrize("value,expected", CLAMP_CASES)
def test_gates_py_clamp(value, expected, monkeypatch):
    _set_env(monkeypatch, value)
    gates = _gates()
    assert gates._takeover_snapshot_max_bytes() == expected


@pytest.mark.parametrize("value,expected", CLAMP_CASES)
def test_takeover_io_clamp(value, expected, monkeypatch):
    _set_env(monkeypatch, value)
    tio = _takeover_io()
    assert tio.MAX_BYTES == expected


@pytest.mark.parametrize("value,expected", CLAMP_CASES)
def test_takeover_transaction_clamp(value, expected, monkeypatch):
    _set_env(monkeypatch, value)
    ttx = _takeover_transaction()
    assert ttx._bounded_max_bytes() == expected


@pytest.mark.parametrize("value,expected", CLAMP_CASES)
def test_takeover_check_sh_clamp(value, expected):
    assert _shell_helper_value(value) == expected


@pytest.mark.parametrize("value", AGREEMENT_CASES)
def test_all_four_mirrors_agree(value, monkeypatch):
    _set_env(monkeypatch, value)
    gates = _gates()
    tio = _takeover_io()
    ttx = _takeover_transaction()
    shell_value = _shell_helper_value(value)
    values = {
        gates._takeover_snapshot_max_bytes(),
        tio.MAX_BYTES,
        ttx._bounded_max_bytes(),
        shell_value,
    }
    assert len(values) == 1, values
