"""No-flag guarantee for /swarm --cloud (contract sections 11 and 14.10).

Outside the cloud block the only permitted differences from the base file are
the version line, `permissions.network`, and the one invocation line naming flag. feature-implement and task-swarm differ from the base only by the
version line and the marked `ffs:cloud-deferred` refusal block.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, NoReturn

import pytest

ROOT = Path(__file__).resolve().parents[1]
SWARM = "skills/swarm/SKILL.md"
BEGIN, END = "<!-- ffs:cloud:begin -->", "<!-- ffs:cloud:end -->"
RECONCILE = "At entry, make one opportunistic, fail-soft `bash scripts/gsd/reconcile.sh` pass"


def _in_ci() -> bool:
    return bool(os.environ.get("CI"))


def _unavailable(reason: str, ci: bool) -> NoReturn:
    """Missing history is a skip on a developer machine but a failure in CI, where a skip would hide the comparison."""
    if ci:
        pytest.fail(reason)
    pytest.skip(reason)


def _git_in(root: Path, ci: bool, *args: str) -> str:
    try:
        done = subprocess.run(
            ["git", *args], cwd=root, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _unavailable(f"git unavailable: {exc}", ci)
    if done.returncode != 0:
        _unavailable(f"git {' '.join(args)} unavailable: {done.stderr.strip()[:120]}", ci)
    return done.stdout


def _git(*args: str) -> str:
    return _git_in(ROOT, _in_ci(), *args)


def _git_ok_in(root: Path, *args: str) -> bool:
    """True when the git command exits 0; any failure is simply False."""
    try:
        done = subprocess.run(
            ["git", *args], cwd=root, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


MAIN_REF = "origin/main"


def _base_carries_the_cloud_block(git, git_ok, commit: str) -> bool:
    """True when the commit's own tree proves the cloud work has landed: the dispatcher and the swarm skill's block."""
    if not git_ok("cat-file", "-e", f"{commit}:lib/cloud_dispatch.py"):
        return False
    skill = git("show", f"{commit}:{SWARM}")
    return skill.count(BEGIN) == 1 and skill.count(END) == 1


def _resolve_base(root: Path, ci: bool) -> str:
    """The commit whose skill text the cloud branch must equal outside the cloud block.

    The pre-cloud commit is the parent of the oldest commit in `merge-base..HEAD` that adds lib/cloud_dispatch.py, so
    unrelated commits beneath the cloud work (e.g. a model-tier refresh that edits skills) are not counted as cloud
    differences. A later merge of main into the cloud branch moves it: only a merge parent that carries no cloud work
    and is an ancestor of origin/main is accepted, so an unrelated cloud-free topic merged in does not qualify (its text
    was never on main). All merges in range are considered, newest first (a synthetic PR merge puts the branch's own
    merges off the first-parent chain); otherwise the pre-cloud commit stands. Nothing here reads any other ref.

    The comparison retires, before anything else, when the base tree itself carries the cloud block (the dispatcher and
    the skill's block markers): then there is no pre-cloud text left to compare with. With no cloud-adding commit in
    range and without that proof it is missing history, a skip on a developer machine and a failure in CI."""
    def git(*args: str) -> str:
        return _git_in(root, ci, *args)

    def git_ok(*args: str) -> bool:
        return _git_ok_in(root, *args)

    merge_base = git("merge-base", "HEAD", MAIN_REF).strip()
    # Checked before `adders`: a branch stacked on unsquashed history keeps its own adder commit in range after main has
    # landed the work, and main's later edits to the skill would then be blamed on it.
    # POST-LANDING: when this skip fires the four comparison items that use this resolver compare against text that no
    # longer exists on the base. They are to be deleted after landing (the other tests of this file keep running).
    if _base_carries_the_cloud_block(git, git_ok, merge_base):
        pytest.skip("cloud work is already on the base; no pre-cloud baseline")
    adders = git(
        "log", "--diff-filter=A", "--format=%H", "--reverse",
        f"{merge_base}..HEAD", "--", "lib/cloud_dispatch.py",
    ).split()
    if not adders:
        _unavailable(f"no commit adds lib/cloud_dispatch.py after {merge_base[:12]} and the base does not carry "
                     "the cloud block either", ci)
    pre = git("rev-parse", f"{adders[0]}^").strip()
    merged = git("log", "--merges", "--format=%P", f"{adders[0]}..HEAD").split("\n")
    for line in merged:
        clean = [
            p for p in line.split()
            if not git_ok("cat-file", "-e", f"{p}:lib/cloud_dispatch.py")
            and git_ok("merge-base", "--is-ancestor", p, MAIN_REF)
        ]
        if len(clean) == 1:
            return clean[0]
    return pre


@pytest.fixture(scope="module")
def base() -> str:
    return _resolve_base(ROOT, _in_ci())


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


def test_swarm_plan_refusal_runs_locally_only_when_the_ledger_is_proven_missing(tmp_path):
    """A refused plan of a retry must not send tasks to the local path while a run with the same id may be active."""
    block = _cloud_block()
    refusals = block[block.index("6. **Refusals.**"):block.index("7. **Hard limits.**")]
    rule = " ".join(refusals[:refusals.index("**Exception: `CLOUD-REFUSED: SECRET_IN_PROMPT`**")].split())
    assert "falls back to local only when the run does not exist" in rule
    assert "status --run <run>" in rule and "CLOUD-ERROR: LEDGER_MISSING" in rule
    assert "Any other result" in rule and "run nothing, cloud or local" in rule
    assert "the run id chosen in step 1" in rule
    # the status verb really answers a run with no ledger with exactly that stderr line, and exit 78
    repo = tmp_path / "repo"
    repo.mkdir()
    _g(repo, "init", "-q", "-b", "main")
    _g(repo, "commit", "-q", "--allow-empty", "-m", "init")
    done = subprocess.run([sys.executable, str(ROOT / "lib" / "cloud_dispatch.py"), "status", "--run", "never-started",
                           "--root", str(repo)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 78 and done.stderr.startswith("CLOUD-ERROR: LEDGER_MISSING "), (done.returncode, done.stderr)
    assert "CLOUD-ERROR: LEDGER_MISSING" in rule


ROOT_DERIVATION = 'ROOT="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"'


def test_swarm_cloud_verbs_take_the_main_worktree_root_so_a_linked_worktree_still_works(tmp_path):
    """From a linked worktree `--show-toplevel` is refused (NOT_MAIN_WORKTREE); the skill's derivation names the main one."""
    block = " ".join(_cloud_block().split())
    assert ROOT_DERIVATION in block
    assert '--policy P --root "$ROOT" --with-doctor' in block and 'start --run <run> --tasks FILE --policy P --root "$ROOT"' in block
    assert 'status --run <run> --root "$ROOT"' in block and "git rev-parse --show-toplevel)\"`" not in block
    main, linked = tmp_path / "main", tmp_path / "linked"
    main.mkdir()
    _g(main, "init", "-q", "-b", "main")
    _g(main, "commit", "-q", "--allow-empty", "-m", "init")
    _g(main, "worktree", "add", "-q", str(linked))

    def cli(*args, cwd):
        return subprocess.run([sys.executable, str(ROOT / "lib" / "cloud_dispatch.py"), *args], cwd=str(cwd),
                              capture_output=True, text=True, timeout=60)

    derived = subprocess.run(["bash", "-c", ROOT_DERIVATION + '; printf %s "$ROOT"'], cwd=str(linked), capture_output=True,
                             text=True, timeout=60).stdout
    assert os.path.realpath(derived) == os.path.realpath(str(main))
    wrong = cli("status", "--run", "never-started", "--root", os.path.realpath(str(linked)), cwd=linked)
    assert "NOT_MAIN_WORKTREE" in wrong.stdout + wrong.stderr  # what the old text hit
    right = cli("status", "--run", "never-started", "--root", derived, cwd=linked)
    assert right.returncode == 78 and right.stderr.startswith("CLOUD-ERROR: LEDGER_MISSING "), (right.stdout, right.stderr)


def test_swarm_names_the_run_id_source_and_stops_on_other_plan_failures():
    block = " ".join(_cloud_block().split())
    step1 = block[block.index("1. **Normalize tasks.**"):block.index("2. **Plan.**")]
    assert "Choose the run id `<run>` now" in step1 and "print it" in step1
    assert "reuse that invocation's run id" in step1 and "ask the operator rather than choosing a new one" in step1
    step2 = block[block.index("2. **Plan.**"):block.index("3. **`--dry-run`.**")]
    assert "Any other non-zero exit of `plan`" in step2 and "exit 2" in step2 and "exit 78" in step2
    assert "run no task, cloud or local, and stop" in step2
    assert "the run id chosen in step 1" in block


def test_swarm_not_a_repo_refusal_falls_back_local_but_never_under_required():
    rule = " ".join(_cloud_block().split())
    sentence = rule[rule.index("CLOUD-REFUSED: NOT_A_REPO"):]
    sentence = sentence[:sentence.index("**Exception")]
    assert "falls back to local under `auto` and `prefer`, and stops under `required`" in sentence


def test_swarm_exit_78_from_start_is_relayed_and_stops_everything():
    """INTEGRATION_REF_UNCONFIRMED exits 78 with CLOUD-ERROR on stderr: neither success nor an exit-3 refusal."""
    block = _cloud_block()
    start = block[block.index("5. **Start cloud groups.**"):block.index("6. **Refusals.**")]
    branch = start[start.index("If `start` exits 78"):]
    assert "`CLOUD-ERROR:" in branch and "**stderr**" in branch and "INTEGRATION_REF_UNCONFIRMED" in branch
    assert "run no task" in branch and "cloud or local" in branch
    assert "stop for the operator to inspect" in branch and "Do not call `start` again" in branch
    assert "Step 6 does not apply" in branch


def test_swarm_restart_failed_ends_after_reporting_with_no_reconcile_and_keeps_the_same_batch_retry():
    block = _cloud_block()
    step = block[block.index("6. **Refusals.**"):block.index("7. **Hard limits.**")]
    step = " ".join(step.split())  # the skill wraps its lines
    failed = step[step.index("`orchestrator=restart-failed` means"):step.index("Otherwise run")]
    assert "`cause=`" in failed and "`orchestrator=restart-failed cause=<ERRNO>`" in step
    assert "ends after you report it" in failed and "run no `reconcile.sh`" in failed
    assert "same `start` (same run id, same tasks file) may be repeated" in failed
    assert "reconcile.sh` cannot fix" not in step
    rest = step[step.index("Otherwise run"):]
    assert "carried no `orchestrator=` token" in rest and "run `bash scripts/gsd/reconcile.sh` once" in rest


# -- the baseline resolver, exercised on synthetic histories ---------------------------

def _g(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
         *args],
        cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=60)
    return done.stdout.strip()


def _commit(repo: Path, files: Dict[str, str], message: str) -> str:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    _g(repo, "add", *files)
    _g(repo, "commit", "-q", "-m", message)
    return _g(repo, "rev-parse", "HEAD")


CLOUD_SKILL = f"line one\n{BEGIN}\ncloud block\n{END}\nline two\nline three\n"


def _outcome(repo: Path, ci: bool):
    """``("fail"|"skip"|"ok", message)``.  `pytest.raises(pytest.fail.Exception)` would let a skip escape and report the
    test as skipped, so an arm that regressed from failing to skipping would stay green; this names the outcome."""
    try:
        base = _resolve_base(repo, ci=ci)
    except pytest.fail.Exception as exc:
        return "fail", str(exc)
    except pytest.skip.Exception as exc:
        return "skip", str(exc)
    return "ok", base


def _history(tmp_path: Path, *, merge_main: bool, merge_unrelated: bool, cloud_on_main: bool = False,
             publish_main: bool = True, block_on_main: bool = True) -> Dict[str, str]:
    """main (moves on after `pre`) with branches topic and cloud cut from `pre`; main and topic edit different lines."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _g(repo, "init", "-q", "-b", "main")
    skill = "line one\nline two\nline three\n"
    first = {"skills/swarm/SKILL.md": CLOUD_SKILL if cloud_on_main and block_on_main else skill}
    if cloud_on_main:
        first["lib/cloud_dispatch.py"] = "# cloud\n"
    ids = {"pre": _commit(repo, first, "base")}
    ids["main"] = _commit(repo, {"skills/swarm/SKILL.md": first["skills/swarm/SKILL.md"].replace("one", "tiered")},
                          "main moves on")
    if publish_main:
        _g(repo, "update-ref", "refs/remotes/origin/main", ids["main"])
    _g(repo, "checkout", "-q", "-b", "topic", ids["pre"])
    ids["topic"] = _commit(repo, {"skills/swarm/SKILL.md": skill.replace("three", "changed behavior")}, "unrelated topic")
    _g(repo, "checkout", "-q", "-b", "cloud", ids["pre"])
    if not cloud_on_main:
        _commit(repo, {"lib/cloud_dispatch.py": "# cloud\n"}, "add the dispatcher")
        _commit(repo, {"docs/cloud.md": "cloud\n"}, "docs")
    if merge_main:
        _g(repo, "merge", "-q", "--no-ff", "-m", "merge main", "main")
    if merge_unrelated:
        _g(repo, "merge", "-q", "--no-ff", "-m", "merge an unrelated topic", "topic")
    ids["repo"] = str(repo)
    return ids


def test_an_unrelated_cloud_free_merge_is_not_the_baseline(tmp_path: Path) -> None:
    ids = _history(tmp_path, merge_main=True, merge_unrelated=True)
    assert _resolve_base(Path(ids["repo"]), ci=False) == ids["main"]  # not the newest merge, the topic


def test_an_unrelated_merge_alone_leaves_the_pre_cloud_commit_as_the_baseline(tmp_path: Path) -> None:
    ids = _history(tmp_path, merge_main=False, merge_unrelated=True)
    assert _resolve_base(Path(ids["repo"]), ci=False) == ids["pre"]


def test_a_merge_of_main_moves_the_baseline(tmp_path: Path) -> None:
    ids = _history(tmp_path, merge_main=True, merge_unrelated=False)
    assert _resolve_base(Path(ids["repo"]), ci=False) == ids["main"]


def test_missing_history_skips_locally_and_fails_in_ci(tmp_path: Path) -> None:
    ids = _history(tmp_path, merge_main=True, merge_unrelated=False, publish_main=False)
    repo = Path(ids["repo"])
    with pytest.raises(pytest.skip.Exception):
        _resolve_base(repo, ci=False)
    kind, message = _outcome(repo, ci=True)
    assert kind == "fail" and "origin/main" in message, (kind, message)


def test_cloud_work_already_on_the_base_is_a_skip_even_in_ci(tmp_path: Path) -> None:
    ids = _history(tmp_path, merge_main=False, merge_unrelated=False, cloud_on_main=True)
    repo = Path(ids["repo"])
    for ci in (False, True):
        with pytest.raises(pytest.skip.Exception) as skipped:
            _resolve_base(repo, ci=ci)
        assert "already on the base" in str(skipped.value)


def test_a_branch_stacked_on_unsquashed_history_retires_once_main_carries_the_block(tmp_path: Path) -> None:
    """Main lands the work as a squash commit and edits the skill outside the block; the branch (whose own adder commit is
    still in range) merges main.  Its baseline must not be an older commit that would blame main's edit on the branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _g(repo, "init", "-q", "-b", "main")
    plain = "line one\nline two\nline three\nline four\n"
    with_block = f"line one\n{BEGIN}\ncloud block\n{END}\nline two\nline three\nline four\n"
    pre = _commit(repo, {"skills/swarm/SKILL.md": plain}, "base")
    _g(repo, "update-ref", "refs/remotes/origin/main", pre)
    _g(repo, "checkout", "-q", "-b", "cloud")
    _commit(repo, {"lib/cloud_dispatch.py": "# cloud\n", "skills/swarm/SKILL.md": with_block}, "add the dispatcher")
    _g(repo, "checkout", "-q", "main")
    landed = _commit(repo, {"lib/cloud_dispatch.py": "# cloud\n",
                            "skills/swarm/SKILL.md": with_block.replace("four", "edited on main")}, "squash: cloud")
    _g(repo, "update-ref", "refs/remotes/origin/main", landed)
    _g(repo, "checkout", "-q", "cloud")
    _g(repo, "merge", "-q", "--no-ff", "-m", "merge main", "main")
    for ci in (False, True):
        with pytest.raises(pytest.skip.Exception) as skipped:
            _resolve_base(repo, ci=ci)
        assert "already on the base" in str(skipped.value)


def test_a_base_without_the_cloud_block_is_never_a_landed_base(tmp_path: Path) -> None:
    """The dispatcher alone is not the proof: the retirement needs the skill's block in the base tree too, and a CI
    run without it fails instead of skipping the comparison."""
    ids = _history(tmp_path, merge_main=False, merge_unrelated=False, cloud_on_main=True, block_on_main=False)
    repo = Path(ids["repo"])
    with pytest.raises(pytest.skip.Exception) as skipped:
        _resolve_base(repo, ci=False)
    assert "does not carry the cloud block" in str(skipped.value)
    kind, message = _outcome(repo, ci=True)
    assert kind == "fail" and "does not carry the cloud block" in message, (kind, message)


def test_a_history_with_no_cloud_work_anywhere_fails_in_ci(tmp_path: Path) -> None:
    ids = _history(tmp_path, merge_main=False, merge_unrelated=False)
    repo = Path(ids["repo"])
    _g(repo, "checkout", "-q", "main")  # HEAD is the base itself and the dispatcher exists nowhere
    with pytest.raises(pytest.skip.Exception):
        _resolve_base(repo, ci=False)
    assert _outcome(repo, ci=True)[0] == "fail"


def test_a_synthetic_pr_merge_puts_the_main_tip_on_the_first_parent_and_still_resolves_to_it(tmp_path: Path) -> None:
    ids = _history(tmp_path, merge_main=False, merge_unrelated=False)
    repo = Path(ids["repo"])
    _g(repo, "checkout", "-q", "-b", "synthetic", ids["main"])
    _g(repo, "merge", "-q", "--no-ff", "-m", "synthetic PR merge", "cloud")
    assert _resolve_base(repo, ci=False) == ids["main"]
    assert _resolve_base(repo, ci=True) == ids["main"]


def test_a_stacked_base_squash_merged_into_main_resolves_to_the_squash_commit_whether_or_not_its_ref_was_pruned(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _g(repo, "init", "-q", "-b", "main")
    skill = "line one\nline two\nline three\n"
    root = _commit(repo, {"skills/swarm/SKILL.md": skill}, "base")
    _g(repo, "update-ref", "refs/remotes/origin/main", root)
    # the stacked base edits line one; the cloud work is cut from it, so the pre-cloud commit is on that line
    _g(repo, "checkout", "-q", "-b", "stacked")
    tiered = skill.replace("one", "tiered")
    stacked = _commit(repo, {"skills/swarm/SKILL.md": tiered}, "stacked base edit")
    _g(repo, "update-ref", "refs/remotes/origin/chore/model-tiers-6-2", stacked)
    _g(repo, "checkout", "-q", "-b", "cloud")
    _commit(repo, {"lib/cloud_dispatch.py": "# cloud\n"}, "add the dispatcher")
    # main moves on, then the stacked base lands as ONE squash commit that is not a descendant of `stacked`
    _g(repo, "checkout", "-q", "main")
    _commit(repo, {"skills/swarm/SKILL.md": skill.replace("three", "later main edit")}, "later main edit")
    landed = _commit(repo, {"skills/swarm/SKILL.md": tiered.replace("three", "later main edit")}, "squash: stacked base")
    _g(repo, "update-ref", "refs/remotes/origin/main", landed)
    _g(repo, "update-ref", "-d", "refs/remotes/origin/chore/model-tiers-6-2")  # branch deleted on merge
    # the operator merges main into the cloud branch
    _g(repo, "checkout", "-q", "cloud")
    _g(repo, "merge", "-q", "--no-ff", "-m", "merge main", "main")
    assert not _git_ok_in(repo, "merge-base", "--is-ancestor", stacked, landed)  # the squash does not contain it
    assert _resolve_base(repo, ci=False) == landed
    assert _resolve_base(repo, ci=True) == landed
    # a developer who never pruned the obsolete remote-tracking ref gets the same baseline
    _g(repo, "update-ref", "refs/remotes/origin/chore/model-tiers-6-2", stacked)
    assert _resolve_base(repo, ci=False) == landed
    assert _resolve_base(repo, ci=True) == landed
