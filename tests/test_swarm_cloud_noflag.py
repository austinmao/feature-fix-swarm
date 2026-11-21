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


def _git_ok(*args: str) -> bool:
    """True when the git command exits 0; any failure is simply False."""
    try:
        done = subprocess.run(
            ["git", *args], cwd=ROOT, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


@pytest.fixture(scope="module")
def base() -> str:
    # Stacked-branch aware: the base is the parent of the oldest commit in
    # `merge-base..HEAD` that adds lib/cloud_dispatch.py, so unrelated commits
    # beneath the cloud work (e.g. a model-tier refresh that edits skills) are
    # not counted as cloud differences.
    merge_base = _git("merge-base", "HEAD", "origin/main").strip()
    adders = _git(
        "log", "--diff-filter=A", "--format=%H", "--reverse",
        f"{merge_base}..HEAD", "--", "lib/cloud_dispatch.py",
    ).split()
    if not adders:
        # The cloud work is already on the base (after landing, on main or on a
        # branch cut from it): there is no pre-cloud text left to compare with.
        pytest.skip("cloud work is already on the base; no pre-cloud baseline")
    pre = _git("rev-parse", f"{adders[0]}^").strip()
    # A later merge of the base branch into the cloud branch moves the baseline.
    # Accept a merged-in parent only when it carries no cloud work and descends
    # from the pre-cloud commit; that excludes the cloud side of a synthetic PR
    # merge and a sibling cloud branch, and a base that does not descend from
    # the stacked pre-cloud commit. All merges in range are considered, newest
    # first (a synthetic PR merge puts the branch's own merges off the first
    # parent chain); otherwise fall through to the pre-cloud commit.
    merged = _git(
        "log", "--merges", "--format=%P", f"{adders[0]}..HEAD",
    ).split("\n")
    for line in merged:
        clean = [
            p for p in line.split()
            if not _git_ok("cat-file", "-e", f"{p}:lib/cloud_dispatch.py")
            and _git_ok("merge-base", "--is-ancestor", pre, p)
        ]
        if len(clean) == 1:
            return clean[0]
    return pre


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


def test_swarm_run_exists_is_reconciled_and_never_runs_cloud_tasks_locally():
    """RUN_EXISTS (a retried start after a lost acknowledgement) must read status, not fall back to local execution."""
    text = (ROOT / SWARM).read_text(encoding="utf-8")
    block = text[text.index(BEGIN):text.index(END)]
    step = block[block.index("6. **Refusals.**"):block.index("7. **Hard limits.**")]
    assert "RUN_EXISTS" in step and "status --run" in step and "reconcile.sh" in step
    assert "Never run those tasks locally" in step and "never call `start` again" in step
    assert step.index("RUN_EXISTS") > step.index("continue with every task locally")
    # CR-01: the match is made by `start` against the cloud-routed tasks; local tasks run, a new run id never starts
    for token in ("batch=same", "batch=other", "batch=unknown", "local groups", "`local-claimed=<ids>`",
                  "orchestrator=restarted", "orchestrator=restart-failed", "under a new run id"):
        assert token in step, token
    assert "without the operator's confirmation" in step and "`local=`" not in step
    assert "compare its task ids with this batch" not in step


def test_swarm_runs_local_tasks_only_from_the_start_output_never_from_the_plan():
    """Under `auto` start re-routes: a task the plan showed as local may be cloud by then (and the reverse)."""
    text = (ROOT / SWARM).read_text(encoding="utf-8")
    block = text[text.index(BEGIN):text.index(END)]
    step = block[block.index("5. **Start cloud groups.**"):block.index("6. **Refusals.**")]
    assert "`CLOUD:local tasks=`" in step and "never from the plan" in step
    assert "empty" in step and "run no local task" in step and "report" in step


def _cloud_block() -> str:
    text = (ROOT / SWARM).read_text(encoding="utf-8")
    return text[text.index(BEGIN):text.index(END)]


def test_swarm_secret_in_prompt_from_plan_is_terminal_and_never_falls_back_to_local():
    """A refused plan of an edited retry must not send tasks that may already run in the cloud to the local path."""
    block = _cloud_block()
    plan = block[block.index("2. **Plan.**"):block.index("3. **`--dry-run`.**")]
    assert "SECRET_IN_PROMPT" in plan and "terminal" in plan and "STOP" in plan
    assert "Dispatch nothing, cloud or local" in plan and "`--dry-run`" in plan
    assert all(policy in plan for policy in ("`auto`", "`prefer`", "`required`"))
    start = block[block.index("5. **Start cloud groups.**"):block.index("6. **Refusals.**")]
    assert "treat it as a refusal as in step 6" not in start
    refusals = block[block.index("6. **Refusals.**"):block.index("7. **Hard limits.**")]
    exception = refusals[refusals.index("SECRET_IN_PROMPT"):refusals.index("RUN_EXISTS")]
    assert "terminal" in exception and "dispatch nothing, cloud or local" in exception
    assert refusals.index("continue with every task locally") < refusals.index("SECRET_IN_PROMPT")
