"""Typed, immutable inputs for one production host launch.

The shell and CLI may select a qualified host profile, but they never supply
an executable argv or an agent prompt.  Those are derived later by the
supervisor from the managed GSD command and this bounded request.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path

from model_requests import ModelRequestError, resolve_request
from .claude_host import ClaudeHostRequest, ClaudeHostRefused, parse_claude_host_request as _parse_claude


class HostRequestRefused(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CodexHostRequest:
    runtime_home: str
    binary: str
    model: str
    effort: str
    sandbox: str
    network_enabled: bool
    token_reservation: int
    timeout_seconds: int

    def material(self) -> dict[str, object]:
        return {"host": "codex", **asdict(self)}


def parse_codex_host_request(
    *, runtime_home: str, binary: str, model_request_json: str,
    sandbox: str, network_enabled: bool, token_reservation: int,
    timeout_seconds: int,
) -> CodexHostRequest:
    """Validate host selection before repository or authority writes."""
    try:
        request = json.loads(model_request_json)
        resolved = resolve_request(request, host="codex")
    except (json.JSONDecodeError, ModelRequestError, TypeError) as error:
        raise HostRequestRefused("HOST_MODEL_REQUEST_INVALID") from error
    try:
        home = Path(runtime_home)
        executable = Path(binary)
    except (TypeError, ValueError) as error:
        raise HostRequestRefused("HOST_REQUEST_INVALID") from error
    if (
        not home.is_absolute() or home.resolve() != home
        or not executable.is_absolute() or executable.resolve() != executable
        or sandbox not in {"read-only", "workspace-write", "danger-full-access"}
        or type(network_enabled) is not bool
        or type(token_reservation) is not int or not 0 <= token_reservation <= 2**63 - 1
        or type(timeout_seconds) is not int or not 0 < timeout_seconds <= 3600
        or not isinstance(resolved.get("model"), str) or not resolved["model"]
        or not isinstance(resolved.get("effort"), str) or not resolved["effort"]
    ):
        raise HostRequestRefused("HOST_REQUEST_INVALID")
    if sandbox == "workspace-write" and network_enabled:
        # The qualified subscription runtime currently proves a denied network
        # surface.  A future enabled-network profile needs its own observation
        # contract rather than inheriting this one.
        raise HostRequestRefused("HOST_NETWORK_UNQUALIFIED")
    return CodexHostRequest(
        str(home), str(executable), resolved["model"], resolved["effort"],
        sandbox, network_enabled, token_reservation, timeout_seconds,
    )


def parse_claude_host_request(
    *, runtime_home: str, credential_source: str, binary: str,
    model_request_json: str, sandbox: str, network_enabled: bool,
    token_reservation: int, timeout_seconds: int,
) -> ClaudeHostRequest:
    """Normalize Claude's closed request into this ingress's refusal contract."""
    try:
        return _parse_claude(
            runtime_home=runtime_home, credential_source=credential_source,
            binary=binary, model_request_json=model_request_json, sandbox=sandbox,
            network_enabled=network_enabled, token_reservation=token_reservation,
            timeout_seconds=timeout_seconds,
        )
    except ClaudeHostRefused as error:
        raise HostRequestRefused(str(error)) from error
