"""Group A: R4 K/M/B/T token suffix parser unit tests.

Tests `run_state.cli._parse_tokens` directly (no subprocess) — argparse type
hook that accepts raw ints (`250000`), single-letter suffixes (`250K`, `1.5M`,
`1B`, `2T`), case-insensitively, with surrounding whitespace tolerated.
Invalid suffixes / non-numeric values must raise `argparse.ArgumentTypeError`
so argparse surfaces a clean CLI error rather than a stack trace.
"""
from __future__ import annotations

import argparse

import pytest

from run_state.cli import _parse_tokens


def test_raw_integer_passes_through() -> None:
    assert _parse_tokens("250000") == 250000


def test_K_suffix() -> None:
    assert _parse_tokens("250K") == 250_000


def test_M_suffix_decimal() -> None:
    assert _parse_tokens("1.5M") == 1_500_000


def test_B_suffix() -> None:
    assert _parse_tokens("1B") == 1_000_000_000


def test_T_suffix() -> None:
    assert _parse_tokens("2T") == 2_000_000_000_000


def test_lowercase_suffix_accepted() -> None:
    assert _parse_tokens("250k") == 250_000


def test_whitespace_tolerated() -> None:
    assert _parse_tokens(" 250 K ") == 250_000


def test_none_passes_through() -> None:
    assert _parse_tokens(None) is None


def test_unknown_suffix_rejected() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_tokens("1G")


def test_garbage_rejected() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_tokens("bogus")
