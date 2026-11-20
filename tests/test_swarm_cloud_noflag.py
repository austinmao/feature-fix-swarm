"""No-flag guarantee for /swarm --cloud (contract sections 11 and 14.10).

Outside the cloud block the only permitted differences from the base file are
the version line, `permissions.network`, and the one invocation line naming flag. feature-implement and task-swarm differ from the base only by the
version line and the marked `ffs:cloud-deferred` refusal block.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SWARM = "skills/swarm/SKILL.md"
BEGIN, END = "<!-- ffs:cloud:begin -->", "<!-- ffs:cloud:end -->"
RECONCILE = "At entry, make one opportunistic, fail-soft `bash scripts/gsd/reconcile.sh` pass"


def _git(*args: str) -> str:
    try:
        done = subprocess.run(
            ["git", *args], cwd=ROOT, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"git unavailable: {exc}")
    if done.returncode != 0:
        pytest.skip(f"git {' '.join(args)} unavailable: {done.stderr.strip()[:120]}")
    return done.stdout


@pytest.fixture(scope="module")
def base() -> str:
    # Stacked-branch aware: the base is the parent of the oldest commit in
    # `merge-base..HEAD` that adds lib/cloud_dispatch.py, so unrelated commits
    # beneath the cloud work (e.g. a model-tier refresh that edits skills) are
    # not counted as cloud differences. When no such commit is in range (after
    # the merge, on main) fall back to the plain merge-base.
    merge_base = _git("merge-base", "HEAD", "origin/main").strip()
    adders = _git(
        "log", "--diff-filter=A", "--format=%H", "--reverse",
        f"{merge_base}..HEAD", "--", "lib/cloud_dispatch.py",
    ).split()
    if not adders:
        return merge_base
    return _git("rev-parse", f"{adders[0]}^").strip()


def _base_text(base: str, path: str) -> str:
    return _git("show", f"{base}:{path}")


def _strip_block(text: str) -> str:
    start, end = text.index(BEGIN), text.index(END) + len(END)
    rest = text[end:]
    return text[:start] + (rest[1:] if rest.startswith("\n") else rest).lstrip("\n")


def _drop_permitted_lines(text: str) -> str:
    kept = []
    for line in text.splitlines(keepends=True):
        if re.match(r'^version:\s', line) or re.match(r"^\s+network:\s", line) or "--cloud" in line:
            continue
        kept.append(line)
    return "".join(kept)


def test_swarm_outside_the_cloud_block_matches_the_base(base: str) -> None:
    current = (ROOT / SWARM).read_text(encoding="utf-8")
    original = _base_text(base, SWARM)
    assert BEGIN not in original
    assert _drop_permitted_lines(_strip_block(current)) == _drop_permitted_lines(original)


def test_only_the_three_permitted_lines_differ_outside_the_block(base: str) -> None:
    current = _strip_block((ROOT / SWARM).read_text(encoding="utf-8")).splitlines()
    original = _base_text(base, SWARM).splitlines()
    added = [line for line in current if line not in original]
    removed = [line for line in original if line not in current]
    assert len(added) == 3 and len(removed) == 2, (added, removed)
    assert sum(line.startswith("version:") for line in added) == 1
    assert "  network: true" in added
    assert sum("--cloud" in line for line in added) == 1
    assert all(line.startswith("version:") or "network:" in line for line in removed)


def test_cloud_block_markers_appear_once_and_in_order() -> None:
    text = (ROOT / SWARM).read_text(encoding="utf-8")
    assert text.count(BEGIN) == 1 and text.count(END) == 1
    assert text.index(BEGIN) < text.index(END)
    assert text.count("--cloud") >= 2
    assert "cloud-dispatch.sh" not in text[: text.index(BEGIN)] + text[text.index(END):]


def test_cloud_block_sits_after_the_init_guard_and_before_the_reconcile_line() -> None:
    text = (ROOT / SWARM).read_text(encoding="utf-8")
    assert text.count(RECONCILE) == 1
    assert text.index("init-guard.sh") < text.index(BEGIN) < text.index(END) < text.index(RECONCILE)
    # the block starts after the init-guard paragraph ends
    assert text.index(BEGIN) > text.index("Advisory only")


DEFERRED = ["feature-implement", "task-swarm"]
D_BEGIN, D_END = "<!-- ffs:cloud-deferred:begin -->", "<!-- ffs:cloud-deferred:end -->"


def _strip_deferred_block(text: str) -> str:
    start, end = text.index(D_BEGIN), text.index(D_END) + len(D_END)
    rest = text[end:]
    return text[:start] + (rest[1:] if rest.startswith("\n") else rest).lstrip("\n")


def _drop_version(text: str) -> str:
    return "".join(line for line in text.splitlines(keepends=True) if not re.match(r"^version:\s", line))


@pytest.mark.parametrize("name", DEFERRED)
def test_deferred_skills_are_identical_to_the_base_outside_the_guard_block(base: str, name: str) -> None:
    path = f"skills/{name}/SKILL.md"
    current = (ROOT / path).read_text(encoding="utf-8")
    original = _base_text(base, path)
    assert D_BEGIN not in original
    assert _drop_version(_strip_deferred_block(current)) == _drop_version(original)


@pytest.mark.parametrize("name", DEFERRED)
def test_deferred_skills_carry_the_cloud_refusal_block(name: str) -> None:
    text = (ROOT / f"skills/{name}/SKILL.md").read_text(encoding="utf-8")
    assert text.count(D_BEGIN) == 1 and text.count(D_END) == 1
    assert text.index(D_BEGIN) < text.index(D_END)
    block = text[text.index(D_BEGIN): text.index(D_END)]
    message = f"Cloud delegation for /{name} arrives in a later release; /swarm --cloud is available now."
    assert message in block
    for token in ("--cloud", "--cloud=auto", "--cloud=prefer", "--cloud=required", "FFS_CLOUD"):
        assert token in block, token
    assert "stop" in block
    # the guard runs before the init gate and every other step
    assert text.index(D_END) < text.index("## Init gate")
    assert "Check this before every other step" in block
