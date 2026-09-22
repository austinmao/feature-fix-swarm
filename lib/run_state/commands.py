"""Pure command typing for managed GSD drives, before preparation effects.

This is an admission vocabulary, not a host launcher. Unsupported workflows
need an explicit activity contract before they may enter managed preparation.
Arguments remain separate strings; neither a shell nor a model prompt parses
them at this boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from collections.abc import Sequence


class ManagedCommandRefused(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ManagedCommand:
    skill: str
    arguments: tuple[str, ...]
    activity: str
    scope: str


_PHASE_COMMANDS = {
    "gsd-plan-phase": "plan",
    "gsd-discuss-phase": "plan",
    "gsd-execute-phase": "execute",
    "gsd-code-review": "review",
    "gsd-verify-work": "review",
}
_TASK_COMMANDS = {"gsd-quick": "execute"}
_PHASE_FLAGS = {
    "gsd-plan-phase": {"--gaps", "--research", "--skip-research", "--reviews", "--text", "--tdd"},
    "gsd-discuss-phase": {"--batch", "--analyze", "--text", "--power", "--assumptions"},
    "gsd-execute-phase": {"--gaps-only", "--tdd"},
    "gsd-code-review": {"--depth=quick", "--depth=standard", "--depth=deep"},
    "gsd-verify-work": set(),
}


def parse_managed_command(command: Sequence[str]) -> ManagedCommand:
    if (
        isinstance(command, (str, bytes)) or not command
        or any(not isinstance(arg, str) or "\0" in arg for arg in command)
    ):
        raise ManagedCommandRefused("MANAGED_COMMAND_INVALID")
    name = command[0]
    if not name.startswith(("/gsd-", "$gsd-")):
        raise ManagedCommandRefused("MANAGED_COMMAND_UNSUPPORTED")
    skill = name[1:]
    args = tuple(command[1:])
    if skill in _PHASE_COMMANDS:
        if not args or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", args[0]):
            raise ManagedCommandRefused("MANAGED_COMMAND_PHASE_REQUIRED")
        tail = iter(args[1:])
        for flag in tail:
            if flag == "--wave" and skill == "gsd-execute-phase":
                wave = next(tail, "")
                if not re.fullmatch(r"[1-9][0-9]*", wave):
                    raise ManagedCommandRefused("MANAGED_COMMAND_ARGUMENT_UNSUPPORTED")
            elif flag not in _PHASE_FLAGS[skill]:
                raise ManagedCommandRefused("MANAGED_COMMAND_ARGUMENT_UNSUPPORTED")
        return ManagedCommand(skill, args, _PHASE_COMMANDS[skill], args[0])
    if skill in _TASK_COMMANDS:
        if not args or not args[0].strip() or args[0].startswith("-"):
            raise ManagedCommandRefused("MANAGED_COMMAND_TASK_REQUIRED")
        if args[0].split()[0] in {"list", "status", "resume"} or any(
            arg not in {"--full", "--validate", "--discuss", "--research"}
            for arg in args[1:]
        ):
            raise ManagedCommandRefused("MANAGED_COMMAND_ARGUMENT_UNSUPPORTED")
        return ManagedCommand(skill, args, _TASK_COMMANDS[skill], "")
    raise ManagedCommandRefused("MANAGED_COMMAND_UNSUPPORTED")
