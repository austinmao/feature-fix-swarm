"""Worker handoff packet for ``/swarm --cloud`` (contracts §2.5, §9, §13, §14.10).

``build_packet`` renders the full prompt a cloud worker receives.  It is pure:
it never reads the process environment and never touches the filesystem, so a
packet can only contain what is passed in.  Secret values are never inputs;
only secret *names* (``task.needs_secrets``) and the operator's wrapper string
from ``config.secrets.wrapper`` appear.

Reviewer prompts are fenced by ``scripts/gsd/fence-data.sh`` (the cloud review
script owns that); this module only builds the worker prompt.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional

import delegation_router

RUN_ID_RE = delegation_router.RUN_ID_RE
TASK_ID_RE = delegation_router.TASK_ID_RE
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
NONCE_PREFIX_LEN = 8
PROVIDERS = ("claude", "codex")

MAX_NESTED_SUBAGENTS = 4
DEFAULT_DEADLINE_HOURS = 12
MIN_TIME_BUDGET_MINUTES = 30
TIME_BUDGET_FACTOR = 2
MINUTES_PER_HOUR = 60
MAX_WRAPPER_CHARS = 200
MANIFEST_SCHEMA = 1

MANIFEST_DIR = ".ffs-result"
ENV_CHECK_CMD = "bash .ffs/cloud/env-check.sh"  # the provider is appended: see env_check_command
_WRAPPER_RE = re.compile(r"^[\x20-\x7e]+$")


class PacketError(ValueError):
    """Invalid packet input."""


def result_branch_name(run: str, task_id: str, nonce: str) -> str:
    """Requested Claude result branch (§1): ``claude/ffs-<R>-<T>-<nonce8>``."""
    return "claude/ffs-%s-%s-%s" % (run, task_id, nonce[:NONCE_PREFIX_LEN])


def env_check_command(provider: str) -> str:
    """``bash .ffs/cloud/env-check.sh <provider>``: the script exits 2 without a provider."""
    return "%s %s" % (ENV_CHECK_CMD, provider)


def manifest_path(task_id: str) -> str:
    return "%s/%s.json" % (MANIFEST_DIR, task_id)


def manifest_skeleton(
    *, run: str, task_id: str, generation: int, nonce: str, base_sha: str
) -> Dict[str, Any]:
    """The §2.5 manifest v1 with identity fields filled and the rest empty."""
    return {
        "schema": MANIFEST_SCHEMA,
        "run": run,
        "task": task_id,
        "generation": generation,
        "nonce": nonce,
        "base_sha": base_sha,
        "status": "complete",
        "summary": "",
        "decisions_needed": [],
        "blockers": [],
        "env_report": {"tools": {}, "present": [], "missing": []},
        "tests_run": [],
    }


def _check_inputs(
    task: Mapping[str, Any],
    *,
    run: str,
    generation: int,
    nonce: str,
    base_sha: str,
    provider: str,
    result_branch: Optional[str],
) -> None:
    if not isinstance(run, str) or not RUN_ID_RE.match(run):
        raise PacketError("invalid run id %r" % (run,))
    task_id = task.get("id")
    if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
        raise PacketError("invalid task id %r" % (task_id,))
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise PacketError("generation must be an integer >= 1")
    if not isinstance(nonce, str) or not NONCE_RE.match(nonce):
        raise PacketError("nonce must be 32 lowercase hex characters")
    if not isinstance(base_sha, str) or not OID_RE.match(base_sha):
        raise PacketError("base_sha must be a full lowercase hex object id")
    if provider not in PROVIDERS:
        raise PacketError("provider must be one of %s" % ", ".join(PROVIDERS))
    if provider == "claude" and result_branch != result_branch_name(run, task_id, nonce):
        raise PacketError("result_branch must be %s" % result_branch_name(run, task_id, nonce))


def _secrets_config(config: Mapping[str, Any]) -> Mapping[str, Any]:
    section = config.get("secrets") if isinstance(config, dict) else None
    return section if isinstance(section, dict) else {}


def _wrapper_lines(config: Mapping[str, Any], needs: List[str]) -> List[str]:
    secrets = _secrets_config(config)
    provider = secrets.get("provider", "none")
    if provider == "none":
        return []
    wrapper = secrets.get("wrapper", "")
    if not isinstance(wrapper, str):
        raise PacketError("config.secrets.wrapper must be a string")
    wrapper = wrapper.strip()
    if wrapper and (len(wrapper) > MAX_WRAPPER_CHARS or not _WRAPPER_RE.match(wrapper)):
        raise PacketError("config.secrets.wrapper must be one short printable line")
    if wrapper:
        lines = ["Secrets wrapper: run every test and runtime command as `%s <command>`." % wrapper]
    else:
        lines = ["Secrets are provided as environment variables of this session; no wrapper is needed."]
    if needs:
        lines.append("Secret names this task may use (names only): %s." % ", ".join(needs))
    return lines


def _time_budget_minutes(task: Mapping[str, Any], config: Mapping[str, Any]) -> int:
    hours = config.get("deadline_hours", DEFAULT_DEADLINE_HOURS) if isinstance(config, dict) else None
    if isinstance(hours, bool) or not isinstance(hours, int) or hours < 1:
        hours = DEFAULT_DEADLINE_HOURS
    estimate = task.get("est_minutes")
    estimate = estimate if isinstance(estimate, int) and not isinstance(estimate, bool) else 0
    wanted = max(estimate * TIME_BUDGET_FACTOR, MIN_TIME_BUDGET_MINUTES)
    return min(wanted, hours * MINUTES_PER_HOUR)


def _verify_command(task: Mapping[str, Any], config: Mapping[str, Any]) -> str:
    verify = config.get("verify") if isinstance(config, dict) else None
    fallback = verify.get("cmd", "") if isinstance(verify, dict) else ""
    return str(task.get("verify_cmd") or fallback or "")


def _delivery_lines(provider: str, result_branch: Optional[str], task_id: str) -> List[str]:
    path = manifest_path(task_id)
    if provider == "claude":
        return [
            "Commit all work, including `%s`." % path,
            "Push to exactly this branch name: %s" % result_branch,
            "Do not push to any other branch and do not force-push.",
        ]
    return [
        "Leave all changes, including `%s`, in the working tree." % path,
        "Do not commit and do not push: the dispatcher collects the diff.",
    ]


def _contract_lines(provider: str, task_id: str, nonce: str, budget: int) -> List[str]:
    lines = [
        "Never ask the user anything. If you are stuck, record it and stop.",
        "Run `%s` first and put its JSON output, unchanged, in the manifest `env_report`." % env_check_command(provider),
        "Never print, echo, log, write or commit a secret value. Names only.",
        "Stay inside `owns`. Do not touch denylisted paths.",
        "Use at most %d nested subagents." % MAX_NESTED_SUBAGENTS,
        "Stop at the time budget (%d minutes) and write `status: blocked` with reasons." % budget,
        "Write the manifest to `%s` with the nonce below, exactly as in the skeleton." % manifest_path(task_id),
    ]
    if provider == "claude":
        lines.insert(4, "Commit your work before you finish.")
    return lines


def _numbered(lines: List[str]) -> List[str]:
    return ["%d. %s" % (index, line) for index, line in enumerate(lines, start=1)]


def build_packet(
    task: Mapping[str, Any],
    *,
    run: str,
    generation: int,
    nonce: str,
    base_sha: str,
    provider: str,
    config: Mapping[str, Any],
    result_branch: Optional[str],
) -> str:
    """Render the full worker prompt for one task.

    Names only: the packet carries the task prompt, ``owns``, the denylist,
    the verify command, the worker contract, the manifest skeleton (nonce
    filled), the delivery instruction (Claude: requested result branch; Codex:
    leave changes in the working tree), the secrets wrapper line when
    ``config.secrets.provider`` is not ``none``, the time budget and the
    nested-subagent cap.  Raises ``PacketError`` on malformed identifiers.
    """
    _check_inputs(
        task,
        run=run,
        generation=generation,
        nonce=nonce,
        base_sha=base_sha,
        provider=provider,
        result_branch=result_branch,
    )
    task_id = str(task["id"])
    budget = _time_budget_minutes(task, config)
    verify = _verify_command(task, config)
    owns = [str(glob) for glob in task.get("owns") or []]
    needs = [str(name) for name in task.get("needs_secrets") or []]
    skeleton = manifest_skeleton(
        run=run, task_id=task_id, generation=generation, nonce=nonce, base_sha=base_sha
    )
    lines: List[str] = [
        "You are an autonomous cloud worker for FeatureFix Swarm run %s, task %s (generation %d)."
        % (run, task_id, generation),
        "Base commit: %s" % base_sha,
        "",
        "## Task",
        str(task.get("prompt") or task.get("title") or ""),
        "",
        "## Scope",
        "owns (you may change only paths matching these globs):",
    ]
    lines.extend("- %s" % glob for glob in owns)
    lines.append("Denylisted paths (never touch): %s" % ", ".join(delegation_router.DENYLIST))
    lines.extend(["", "## Verification", "Verify command: %s" % (verify or "(none given)")])
    lines.extend(_wrapper_lines(config, needs))
    lines.extend(["", "## Worker contract"])
    lines.extend(_numbered(_contract_lines(provider, task_id, nonce, budget)))
    lines.extend(["", "## Delivery"])
    lines.extend(_delivery_lines(provider, result_branch, task_id))
    lines.extend(
        [
            "",
            "## Manifest skeleton (write to `%s`)" % manifest_path(task_id),
            "Fill `status` (complete or blocked), `summary` (at most 2000 characters), "
            "`decisions_needed`, `blockers`, `env_report` and `tests_run`. "
            "Keep `schema`, `run`, `task`, `generation`, `nonce` and `base_sha` unchanged. "
            "`env_report` holds names and version strings only.",
            json.dumps(skeleton, indent=2),
            "",
        ]
    )
    return "\n".join(lines)
