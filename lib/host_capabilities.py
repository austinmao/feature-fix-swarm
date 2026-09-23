#!/usr/bin/env python3
"""Fail-closed admission contract for the isolated Codex GSD host.

Versions are evidence, not a compatibility range.  Admission is based on the
CLI surfaces and the concrete private runtime home that the runner will use.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import shutil
import time
try:  # Python 3.11+
    import tomllib
except ImportError:  # Python 3.9/3.10 installations may provide the compatible backport.
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

# The runner also executes this file directly; resolve the one canonical model
# policy without maintaining a second table here.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from model_requests import CODEX_TIERS, ModelRequestError, resolve_request  # noqa: E402
try:
    from process_identity import ProcessIdentity  # noqa: E402
except ImportError:  # installed bundles must carry this dependency too
    ProcessIdentity = None  # type: ignore[assignment,misc]

REQUIRED_EXEC_FLAGS = ("--strict-config", "--ignore-user-config", "--ignore-rules", "--sandbox", "--add-dir", "--disable")
REQUIRED_HOOK_FLAG = "--dangerously-bypass-hook-trust"
# GSD 1.14 deliberately removed Codex's context-monitor registrations because
# Codex hook payloads do not provide the metrics that implementation needs.
# SessionStart is the one production hook the pinned installer owns.
REQUIRED_HOOK_EVENTS = {"SessionStart"}
DISABLED_NATIVE_FEATURES = (
    "multi_agent", "multi_agent_v2", "plugins", "remote_plugin",
    "recommended_plugins", "plugin_sharing", "apps",
)
OBSERVATION_SCHEMA = "ffs.codex-runtime-observation/v2"
SHELL_PROBE_NAME = ".ffs-observer-shell-probe.py"
QUALIFIED_RUNTIME_SCHEMA = "ffs.qualified-codex-runtime/v1"
TELEMETRY_SCHEMA = "ffs.codex-runtime-telemetry/v1"
OBSERVATION_FRESHNESS_SECONDS = 15 * 60
CLI_INSPECTION_TIMEOUT_SECONDS = 3
ARTIFACT_REVIEW_SCHEMA = "ffs.artifact-review-material/v2"
ARTIFACT_REVIEW_PROMPT_LIMIT = 32 * 1024
ARTIFACT_REVIEW_CONTEXT_LIMIT = 64 * 1024
_CLOSED_REVIEW_ENVIRONMENT = {
    "HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "CODEX_HOME",
}
_CODEX_ENVIRONMENT = {
    "HOME", "CODEX_HOME", "TMPDIR", "PATH", "LANG", "LC_ALL", "NO_COLOR",
}
_GSD_ENVIRONMENT = {
    "GSD_DISPATCH_MODE", "FFS_SUPERVISED_COMMIT_MODE",
    "FFS_SUPERVISED_ADMISSION_FILE", "FFS_SUPERVISED_DISPATCH_COMMAND_JSON",
}
_GSD_DISPATCH_MODE = "ffs-supervised-process"
_GSD_COMMIT_MODE = "patches"


class CapabilityError(ValueError):
    pass


def _path_identity(path: Path, label: str) -> dict[str, object]:
    try:
        info = path.stat()
    except OSError as exc:
        raise CapabilityError(f"{label} is unavailable") from exc
    if not path.is_dir() or path.is_symlink():
        raise CapabilityError(f"{label} must be a real directory")
    return {"path": str(path.resolve()), "device": info.st_dev, "inode": info.st_ino}


def current_supervisor_identity() -> dict[str, object]:
    """Return the local host/boot incarnation without ever serializing secrets."""
    if ProcessIdentity is None:
        raise CapabilityError("runtime process identity implementation is unavailable")
    try:
        identity = ProcessIdentity.current()
    except (OSError, ValueError, ProcessLookupError) as exc:
        raise CapabilityError("current supervisor process identity is unavailable") from exc
    return {"host_id": identity.host_id, "boot_id": identity.boot_id,
            "pid": identity.pid, "start_token": identity.start_token}


def closed_environment_hash(environment: dict[str, str]) -> str:
    """Bind a deliberately closed observer envelope without persisting values."""
    if not isinstance(environment, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                                for key, value in environment.items()):
        raise CapabilityError("runtime closed environment is malformed")
    return hashlib.sha256(json.dumps(environment, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class GsdSupervisorEnvironment:
    """Typed, closed additions required by the installed GSD wave bridge."""

    dispatch_mode: str
    commit_mode: str
    admission_file: str
    dispatch_command_json: str

    def as_dict(self) -> dict[str, str]:
        return {
            "GSD_DISPATCH_MODE": self.dispatch_mode,
            "FFS_SUPERVISED_COMMIT_MODE": self.commit_mode,
            "FFS_SUPERVISED_ADMISSION_FILE": self.admission_file,
            "FFS_SUPERVISED_DISPATCH_COMMAND_JSON": self.dispatch_command_json,
        }


def _validate_gsd_command_json(value: object) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > 16 * 1024 or "\0" in value:
        raise CapabilityError("GSD supervisor command JSON is malformed")
    try:
        command = json.loads(value)
    except (json.JSONDecodeError, RecursionError) as error:
        raise CapabilityError("GSD supervisor command JSON is malformed") from error
    if (not isinstance(command, list) or not command
            or any(not isinstance(part, str) or not part or "\0" in part for part in command)):
        raise CapabilityError("GSD supervisor command argv is malformed")
    canonical = json.dumps(command, ensure_ascii=True, separators=(",", ":"))
    if value != canonical:
        raise CapabilityError("GSD supervisor command JSON is not canonical")
    bridge_paths = [Path(part) for part in command if Path(part).name == "gsd_wave_bridge.py"]
    if len(bridge_paths) != 1 or bridge_paths[0] != Path(command[-1]):
        raise CapabilityError("GSD supervisor command does not name gsd_wave_bridge")
    bridge = bridge_paths[0]
    if not bridge.is_absolute() or bridge.is_symlink() or not bridge.is_file():
        raise CapabilityError("GSD supervisor bridge is unavailable or unsafe")
    try:
        if bridge.stat().st_uid != os.getuid():
            raise CapabilityError("GSD supervisor bridge is not current-user owned")
    except OSError as error:
        raise CapabilityError("GSD supervisor bridge is unavailable") from error
    return value


def validate_gsd_supervisor_environment(value: object) -> GsdSupervisorEnvironment:
    """Validate the exact four-variable GSD supervisor addition set."""
    if isinstance(value, GsdSupervisorEnvironment):
        values = value.as_dict()
    elif isinstance(value, dict):
        values = value
    else:
        raise CapabilityError("GSD supervisor environment additions are malformed")
    if set(values) != _GSD_ENVIRONMENT or any(not isinstance(key, str) for key in values):
        raise CapabilityError("GSD supervisor environment additions are not closed")
    if values["GSD_DISPATCH_MODE"] != _GSD_DISPATCH_MODE:
        raise CapabilityError("GSD supervisor dispatch mode is unsupported")
    if values["FFS_SUPERVISED_COMMIT_MODE"] != _GSD_COMMIT_MODE:
        raise CapabilityError("GSD supervisor commit mode is unsupported")
    admission = values["FFS_SUPERVISED_ADMISSION_FILE"]
    if (not isinstance(admission, str) or not admission or "\0" in admission
            or not Path(admission).is_absolute()):
        raise CapabilityError("GSD supervisor admission file must be absolute")
    _private_regular(Path(admission), "GSD supervisor admission file")
    command_json = _validate_gsd_command_json(values["FFS_SUPERVISED_DISPATCH_COMMAND_JSON"])
    return GsdSupervisorEnvironment(_GSD_DISPATCH_MODE, _GSD_COMMIT_MODE, admission, command_json)


def gsd_supervisor_environment_from_process() -> GsdSupervisorEnvironment | None:
    """Read the optional closed addition set from the invoking GSD process."""
    present = _GSD_ENVIRONMENT.intersection(os.environ)
    if not present:
        return None
    return validate_gsd_supervisor_environment({key: os.environ.get(key) for key in _GSD_ENVIRONMENT})


def codex_closed_environment(home: Path, tmpdir: Path, binary: Path,
                             chain: dict[str, str] | tuple[tuple[str, str], ...],
                             gsd_environment: object = None) -> dict[str, str]:
    """Build the one closed environment used by production Codex launches.

    ``tmpdir`` is a concrete invocation leaf.  The policy hash deliberately
    canonicalizes it to its runtime-home parent, while the returned mapping
    retains the leaf for the child process.
    """
    home = home.resolve()
    binary = binary.resolve()
    chain = dict(chain)
    path_entries = [str(binary.parent)]
    if binary.suffix == ".js" or "node_sha256" in chain:
        node = Path(os.environ.get("CODEX_NODE_BINARY") or shutil.which("node") or "").resolve()
        if not node.is_absolute() or not node.is_file() or node.is_symlink():
            raise CapabilityError("Codex JS launcher has no resolved regular Node binary")
        if "node_sha256" in chain and chain["node_sha256"] != _digest(node):
            raise CapabilityError("Codex Node binary differs from qualified chain")
        path_entries.append(str(node.parent))
    path_entries.extend(("/usr/bin", "/bin"))
    environment = {
        "HOME": str(home), "CODEX_HOME": str(home), "TMPDIR": str(tmpdir.resolve()),
        "PATH": os.pathsep.join(dict.fromkeys(path_entries)),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "NO_COLOR": "1",
    }
    if gsd_environment is not None:
        environment.update(validate_gsd_supervisor_environment(gsd_environment).as_dict())
    return environment


def codex_environment_policy(environment: dict[str, str]) -> dict[str, str]:
    """Return the replay-stable policy envelope for a closed Codex environment."""
    if (not isinstance(environment, dict)
            or set(environment) not in (_CODEX_ENVIRONMENT, _CODEX_ENVIRONMENT | _GSD_ENVIRONMENT)
            or any(not isinstance(key, str) or not isinstance(value, str)
                   for key, value in environment.items())):
        raise CapabilityError("Codex closed environment is malformed")
    for key in ("HOME", "CODEX_HOME", "TMPDIR"):
        if not os.path.isabs(environment[key]):
            raise CapabilityError("Codex closed environment paths must be absolute")
    if (environment["HOME"] != environment["CODEX_HOME"]
            or any(not part or not os.path.isabs(part)
                   for part in environment["PATH"].split(os.pathsep))):
        raise CapabilityError("Codex closed environment policy is malformed")
    policy = dict(environment)
    policy["TMPDIR"] = str(Path(environment["TMPDIR"]).resolve().parent)
    if _GSD_ENVIRONMENT.issubset(policy):
        additions = validate_gsd_supervisor_environment({key: policy[key] for key in _GSD_ENVIRONMENT})
        policy["FFS_SUPERVISED_ADMISSION_FILE"] = str(
            Path(additions.admission_file).resolve().parent / "<admission>"
        )
    return policy


def codex_environment_policy_hash(environment: dict[str, str]) -> str:
    """Hash only stable Codex launch policy, excluding its random temp leaf."""
    return closed_environment_hash(codex_environment_policy(environment))


def preview_gsd_codex_environment_policy_hash(environment: dict[str, str]) -> str:
    """Precompute policy before exclusively publishing its admission file.

    Only admission-file existence is deferred.  The bridge command and every
    other closed value are validated now; callers must compare this preview to
    ``codex_environment_policy_hash`` immediately after immutable publication.
    """
    if not isinstance(environment, dict) or set(environment) != _CODEX_ENVIRONMENT | _GSD_ENVIRONMENT:
        raise CapabilityError("Codex closed environment is malformed")
    base = {key: environment[key] for key in _CODEX_ENVIRONMENT}
    policy = codex_environment_policy(base)
    additions = {key: environment[key] for key in _GSD_ENVIRONMENT}
    if additions.get("GSD_DISPATCH_MODE") != _GSD_DISPATCH_MODE:
        raise CapabilityError("GSD supervisor dispatch mode is unsupported")
    if additions.get("FFS_SUPERVISED_COMMIT_MODE") != _GSD_COMMIT_MODE:
        raise CapabilityError("GSD supervisor commit mode is unsupported")
    admission = additions.get("FFS_SUPERVISED_ADMISSION_FILE")
    if not isinstance(admission, str) or not admission or "\0" in admission or not Path(admission).is_absolute():
        raise CapabilityError("GSD supervisor admission file must be absolute")
    additions["FFS_SUPERVISED_DISPATCH_COMMAND_JSON"] = _validate_gsd_command_json(
        additions.get("FFS_SUPERVISED_DISPATCH_COMMAND_JSON")
    )
    policy.update(additions)
    policy["FFS_SUPERVISED_ADMISSION_FILE"] = str(Path(admission).resolve().parent / "<admission>")
    return closed_environment_hash(policy)


@dataclass(frozen=True)
class QualifiedCodexRuntime:
    """Canonical supervisor admission, built only after replaying canary evidence."""

    binary: tuple[tuple[str, str], ...]
    runtime: tuple[tuple[str, object], ...]
    workspace: tuple[tuple[str, object], ...]
    supervisor: tuple[tuple[str, object], ...]
    execution: tuple[tuple[str, object], ...]
    observation: tuple[tuple[str, object], ...]

    @property
    def status(self) -> str:
        return "admitted"

    def to_dict(self) -> dict[str, object]:
        payload = {
            "schema": QUALIFIED_RUNTIME_SCHEMA,
            "status": self.status,
            "binary": dict(self.binary), "runtime": dict(self.runtime),
            "workspace": dict(self.workspace), "supervisor": dict(self.supervisor),
            "execution": dict(self.execution), "observation": dict(self.observation),
        }
        # JSON is the external boundary.  Round-trip so callers cannot mutate
        # a nested list in the immutable admission object after verification.
        return json.loads(json.dumps(payload, sort_keys=True))

    def __getitem__(self, key: str) -> object:
        return self.to_dict()[key]


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise CapabilityError(f"artifact review {label} must be a SHA-256 digest")
    return value


def _bounded_string(value: object, label: str, *, limit: int = 4096) -> str:
    if not isinstance(value, str) or not value or "\0" in value or len(value) > limit:
        raise CapabilityError(f"artifact review {label} is malformed")
    return value


def _canonical_pairs(value: object, label: str, *, hashes: bool = False) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict) or not value:
        raise CapabilityError(f"artifact review {label} must be a nonempty object")
    pairs = []
    for key, item in value.items():
        key = _bounded_string(key, f"{label} key", limit=256)
        item = _sha256(item, label) if hashes else _bounded_string(item, label)
        pairs.append((key, item))
    return tuple(sorted(pairs))


def _artifact_contents(value: object, artifacts: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...] | None:
    """Validate exact UTF-8 review text against its selected-byte digest."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {name for name, _digest in artifacts}:
        raise CapabilityError("artifact review contents do not match selected artifacts")
    contents: list[tuple[str, str]] = []
    for name, digest in artifacts:
        content = value[name]
        if not isinstance(content, str):
            raise CapabilityError("artifact review contents must be text")
        try:
            encoded = content.encode("utf-8")
        except UnicodeError as exc:
            raise CapabilityError("artifact review contents are not UTF-8") from exc
        if hashlib.sha256(encoded).hexdigest() != digest:
            raise CapabilityError(f"artifact review content digest differs for {name}")
        contents.append((name, content))
    return tuple(contents)


def _review_output_contract(value: object, *, limit: int = ARTIFACT_REVIEW_PROMPT_LIMIT) -> str | None:
    """Retain only immutable, bounded canonical JSON; never arbitrary objects."""
    if value is None:
        return None
    if type(value) is not dict or not value:
        raise CapabilityError("artifact review output contract must be a nonempty object")
    nodes = 0

    def check(item, depth):
        nonlocal nodes
        nodes += 1
        if depth > 32 or nodes > 8192:
            raise CapabilityError("artifact review output contract exceeds its structural limit")
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise CapabilityError("artifact review output contract keys must be strings")
                check(child, depth + 1)
        elif type(item) is list:
            for child in item:
                check(child, depth + 1)
        elif type(item) not in (str, int, float, bool, type(None)):
            raise CapabilityError("artifact review output contract is not JSON")
    check(value, 0)
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > limit:
            raise ValueError("oversized contract")
    except (ValueError, TypeError, UnicodeError) as error:
        raise CapabilityError("artifact review output contract is malformed or oversized") from error
    return encoded


@dataclass(frozen=True)
class ArtifactReviewMaterial:
    """Immutable, non-executing input for a future qualified host adapter.

    This binds the requested/effective model and a closed launch environment to
    a fresh artifact-only prompt.  It does not claim that either host is
    qualified and cannot launch a process by itself.
    """

    host: str
    requested_model: tuple[tuple[str, str], ...]
    effective_model: str
    effective_effort: str | None
    config_sha256: str
    policy_sha256: str
    environment: tuple[tuple[str, str], ...]
    selected_artifacts: tuple[tuple[str, str], ...]
    selected_contents: tuple[tuple[str, str], ...] | None
    provenance: tuple[tuple[str, str], ...]
    prompt: str
    output_contract_json: str | None = None
    review_context_json: str | None = None

    def replay_binding(self) -> dict[str, object]:
        """Return durable nonsecret material; never persist prompt/env values."""
        binding = {
            "schema": ARTIFACT_REVIEW_SCHEMA,
            "operation": "artifact-review",
            "host": self.host,
            "requested_model": dict(self.requested_model),
            "effective_model": self.effective_model,
            "effective_effort": self.effective_effort,
            "config_sha256": self.config_sha256,
            "policy_sha256": self.policy_sha256,
            "environment_sha256": hashlib.sha256(
                json.dumps(self.environment, separators=(",", ":")).encode()
            ).hexdigest(),
            "artifacts_sha256": hashlib.sha256(
                json.dumps(self.selected_artifacts, separators=(",", ":")).encode()
            ).hexdigest(),
            "provenance_sha256": hashlib.sha256(
                json.dumps(self.provenance, separators=(",", ":")).encode()
            ).hexdigest(),
            "prompt_sha256": hashlib.sha256(self.prompt.encode()).hexdigest(),
        }
        if self.output_contract_json is not None:
            binding["output_contract_sha256"] = hashlib.sha256(self.output_contract_json.encode()).hexdigest()
        if self.review_context_json is not None:
            binding["review_context_sha256"] = hashlib.sha256(self.review_context_json.encode()).hexdigest()
        return binding

    def execution_environment(self) -> dict[str, str]:
        """Return a closed transport envelope; this is not a native CLI API."""
        return {**dict(self.environment), "FFS_ARTIFACT_REVIEW_PROMPT": self.prompt}


def build_artifact_review_material(
    *, host: str, model_request: object, config_sha256: str, policy_sha256: str,
    environment: object, selected_artifacts: object, provenance: object, selected_contents: object = None,
    output_contract: object = None, review_context: object = None,
) -> ArtifactReviewMaterial:
    """Construct the only bindable material for an artifact-only review.

    The caller supplies only a closed, explicit environment and artifact
    metadata.  Credential-bearing values and ambient environment inheritance
    are deliberately absent.  A containment adapter must still prove that it
    enforces this material before native execution may be admitted.
    """
    if host not in {"codex", "claude"}:
        raise CapabilityError("artifact review host is unsupported")
    if not isinstance(model_request, dict):
        raise CapabilityError("artifact review model request must be typed")
    try:
        resolved = resolve_request(model_request, host=host)
    except ModelRequestError as exc:
        raise CapabilityError("artifact review model request is invalid") from exc
    requested_model = _canonical_pairs(model_request, "model request")
    environment_pairs = _canonical_pairs(environment, "environment")
    environment_dict = dict(environment_pairs)
    if (set(environment_dict) - _CLOSED_REVIEW_ENVIRONMENT
            or not {"HOME", "PATH", "TMPDIR"}.issubset(environment_dict)):
        raise CapabilityError("artifact review environment is not closed")
    if any(not os.path.isabs(value) for key, value in environment_dict.items()
           if key not in {"LANG", "LC_ALL"}):
        raise CapabilityError("artifact review environment paths must be absolute")
    if any(not part or not os.path.isabs(part) for part in environment_dict["PATH"].split(os.pathsep)):
        raise CapabilityError("artifact review PATH must contain only absolute entries")
    artifacts = _canonical_pairs(selected_artifacts, "artifacts", hashes=True)
    contents = _artifact_contents(selected_contents, artifacts)
    source_provenance = _canonical_pairs(provenance, "provenance")
    contract_json = _review_output_contract(output_contract)
    context_json = _review_output_contract(review_context, limit=ARTIFACT_REVIEW_CONTEXT_LIMIT)
    prompt_data = {
        "operation": "artifact-review",
        "artifacts": [
            {"name": name, "sha256": digest, "encoding": "utf-8", "contents": content}
            for (name, digest), (_content_name, content) in zip(artifacts, contents)
        ] if contents is not None else dict(artifacts),
        "provenance": dict(source_provenance),
        "instructions": [
            "Review only the selected artifacts and their supplied provenance.",
            "Do not invoke tools, discover agents, plugins, sessions, or remote services.",
            "Return one JSON object with a verdict, findings, and evidence references.",
        ],
    }
    if contract_json is not None:
        prompt_data["output_contract"] = json.loads(contract_json)
        prompt_data["instructions"][-1] = (
            "Return exactly one JSON object matching output_contract; no prose or Markdown fences.")
    if context_json is not None:
        prompt_data["review_context"] = json.loads(context_json)
        prompt_data["instructions"].insert(0,
            "Evaluate the full sealed criteria and supplied verified check outputs in review_context; "
            "use only the evidence references supplied for each criterion or finding scope.")
    prompt = "Artifact-only review request:\n" + json.dumps(
        prompt_data, sort_keys=True, separators=(",", ":"),
    )
    limit = ARTIFACT_REVIEW_PROMPT_LIMIT if context_json is None else ARTIFACT_REVIEW_CONTEXT_LIMIT
    if len(prompt.encode()) > limit:
        raise CapabilityError("artifact review prompt exceeds its bounded input limit")
    return ArtifactReviewMaterial(
        host=host, requested_model=requested_model,
        effective_model=resolved["model"], effective_effort=resolved["effort"],
        config_sha256=_sha256(config_sha256, "config"),
        policy_sha256=_sha256(policy_sha256, "policy"), environment=environment_pairs,
        selected_artifacts=artifacts, selected_contents=contents,
        provenance=source_provenance, prompt=prompt, output_contract_json=contract_json,
        review_context_json=context_json,
    )


def validate_artifact_review_material(value: object) -> ArtifactReviewMaterial:
    """Reject forged dataclass values by reconstructing the canonical material."""
    if type(value) is not ArtifactReviewMaterial:
        raise CapabilityError("artifact review material has an invalid type")
    try:
        rebuilt = build_artifact_review_material(
            host=value.host, model_request=dict(value.requested_model),
            config_sha256=value.config_sha256, policy_sha256=value.policy_sha256,
            environment=dict(value.environment), selected_artifacts=dict(value.selected_artifacts),
            selected_contents=None if value.selected_contents is None else dict(value.selected_contents),
            provenance=dict(value.provenance),
            output_contract=None if value.output_contract_json is None else json.loads(value.output_contract_json),
            review_context=None if value.review_context_json is None else json.loads(value.review_context_json),
        )
    except (CapabilityError, TypeError, ValueError, RecursionError) as exc:
        raise CapabilityError("artifact review material is malformed") from exc
    if rebuilt != value:
        raise CapabilityError("artifact review material is not canonical")
    return rebuilt


def _parse_toml(text: str) -> dict[str, object]:
    """Parse policy with an available standard/backport parser, never a traceback."""
    if tomllib is None:
        raise CapabilityError("runtime TOML validation requires Python 3.11+ or the tomli package")
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CapabilityError("runtime config is invalid TOML") from exc


def version_from(output: str) -> str:
    match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", output)
    if not match:
        raise CapabilityError("Codex CLI did not report a semver version")
    return match.group(1)


def command_output(binary: str, *args: str) -> str:
    try:
        result = subprocess.run(
            [binary, *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            check=False, timeout=CLI_INSPECTION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise CapabilityError(f"Codex CLI {' '.join(args)} timed out after {CLI_INSPECTION_TIMEOUT_SECONDS}s") from exc
    if result.returncode:
        raise CapabilityError(f"Codex CLI {' '.join(args)} failed")
    return result.stdout


def admit_cli(binary: str) -> dict[str, object]:
    version = version_from(command_output(binary, "--version"))
    help_text = command_output(binary, "exec", "--help")
    absent = [flag for flag in (*REQUIRED_EXEC_FLAGS, REQUIRED_HOOK_FLAG) if flag not in help_text]
    if absent:
        raise CapabilityError("Codex CLI lacks required isolated-runtime capabilities: " + ", ".join(absent))
    # Help output is only a static surface check.  Runtime admission remains
    # UNMET until verify_runtime validates a canary from this exact executable.
    return {"schema": "ffs.codex-capabilities/v1", "version": version, "tiers": CODEX_TIERS,
            "runtime_readiness": "UNMET", "static_surface": True}


def _toml_string(value: str) -> str:
    """Encode a TOML basic string without treating a pathname as TOML source."""
    escaped: list[str] = []
    for character in value:
        codepoint = ord(character)
        if character == "\\":
            escaped.append("\\\\")
        elif character == '"':
            escaped.append('\\"')
        elif character == "\n":
            escaped.append("\\n")
        elif character == "\r":
            escaped.append("\\r")
        elif character == "\t":
            escaped.append("\\t")
        elif codepoint < 0x20 or codepoint == 0x7F:
            escaped.append(f"\\u{codepoint:04x}")
        else:
            escaped.append(character)
    return '"' + "".join(escaped) + '"'


def _canonical_git_worktree(worktree: Path) -> Path | None:
    """Return the primary worktree Codex persists for a linked worktree.

    Codex currently canonicalizes linked worktrees through their common Git
    directory and writes that primary path to ``config.toml`` on first use.
    Derive and bind the same path before launch so a successful probe cannot
    mutate the qualified runtime identity.
    """
    marker = worktree / ".git"
    if marker.is_symlink() or not marker.exists():
        return None
    if marker.is_dir():
        return worktree
    if not marker.is_file():
        raise CapabilityError("worktree Git marker is unsafe")
    try:
        line = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise CapabilityError("worktree Git marker is unreadable") from exc
    if not line.startswith("gitdir: ") or "\n" in line:
        raise CapabilityError("worktree Git marker is malformed")
    gitdir = Path(line[8:])
    if not gitdir.is_absolute():
        gitdir = marker.parent / gitdir
    if gitdir.is_symlink() or not gitdir.is_dir():
        raise CapabilityError("worktree Git directory is unsafe")
    gitdir = gitdir.resolve()
    common_marker = gitdir / "commondir"
    if common_marker.is_symlink() or not common_marker.is_file():
        raise CapabilityError("linked worktree common directory is unavailable")
    try:
        common_text = common_marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise CapabilityError("linked worktree common directory is unreadable") from exc
    if not common_text or "\n" in common_text:
        raise CapabilityError("linked worktree common directory is malformed")
    common = Path(common_text)
    if not common.is_absolute():
        common = gitdir / common
    if common.is_symlink() or not common.is_dir():
        raise CapabilityError("linked worktree primary directory is unsafe")
    common = common.resolve()
    primary = common.parent
    if common.name != ".git" or primary.is_symlink() or not primary.is_dir():
        raise CapabilityError("linked worktree primary directory is unsafe")
    return primary.resolve()


def _runtime_policy(worktree: Path, sandbox_mode: str, network_enabled: bool, roots: list[str]) -> dict[str, object]:
    projects = {str(worktree): {"trust_level": "untrusted"}}
    primary = _canonical_git_worktree(worktree)
    if primary is not None and primary != worktree:
        # This is the exact value Codex 0.154 materializes when hook trust is
        # explicitly bypassed.  It is policy-bound here and rules remain
        # disabled by the production argv.
        projects[str(primary)] = {"trust_level": "trusted"}
    return {
        "approval_policy": "never", "sandbox_mode": sandbox_mode, "web_search": "disabled",
        "project_doc_max_bytes": 0,
        "sandbox_workspace_write": {"network_access": network_enabled, "exclude_slash_tmp": True,
                                    "exclude_tmpdir_env_var": True, "writable_roots": roots},
        "projects": projects,
    }


def render_runtime_config(path: Path, worktree: Path, sandbox_mode: str, network_enabled: bool, roots: list[str]) -> None:
    """Render the small allowlisted policy using typed values only."""
    worktree = worktree.resolve()
    roots = [str(Path(root).resolve()) for root in roots]
    policy = _runtime_policy(worktree, sandbox_mode, network_enabled, roots)
    if sandbox_mode not in {"read-only", "workspace-write", "danger-full-access"}:
        raise CapabilityError("runtime requested an unknown sandbox mode")
    if not roots or roots[0] != str(worktree) or len(set(roots)) != len(roots):
        raise CapabilityError("runtime writable-root contract is malformed")
    lines = [
        'approval_policy = "never"', f'sandbox_mode = {_toml_string(sandbox_mode)}',
        'web_search = "disabled"', 'project_doc_max_bytes = 0', '', '[sandbox_workspace_write]',
        f'network_access = {str(network_enabled).lower()}', 'exclude_slash_tmp = true',
        'exclude_tmpdir_env_var = true',
        'writable_roots = [' + ', '.join(_toml_string(root) for root in roots) + ']', '',
        f'[{"projects"}.{_toml_string(str(worktree))}]', 'trust_level = "untrusted"', '',
    ]
    primary = _canonical_git_worktree(worktree)
    if primary is not None and primary != worktree:
        lines.extend((f'[{"projects"}.{_toml_string(str(primary))}]', 'trust_level = "trusted"', ''))
    rendered = "\n".join(lines)
    if _parse_toml(rendered) != policy:
        raise CapabilityError("rendered runtime config differs from typed allowlist")
    path.write_text(rendered, encoding="utf-8")


def _private_regular(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise CapabilityError(f"{label} must be a regular non-symlink file")
    meta = os.lstat(path)
    if meta.st_uid != os.getuid() or stat.S_IMODE(meta.st_mode) != 0o600:
        raise CapabilityError(f"{label} must be current-user owned mode 0600")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_digest(root: Path, excluded_top: frozenset[str] = frozenset()) -> str:
    if not root.is_dir() or root.is_symlink():
        raise CapabilityError(f"runtime tree is missing or unsafe: {root.name}")
    for name in excluded_top:
        excluded = root / name
        if os.path.lexists(excluded):
            _tree_digest(excluded)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative_path = path.relative_to(root)
        if relative_path.parts and relative_path.parts[0] in excluded_top:
            continue
        if path.is_symlink():
            raise CapabilityError(f"runtime tree contains unsafe member: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise CapabilityError(f"runtime tree contains unsafe member: {path}")
        relative, content = relative_path.as_posix().encode(), path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _binary_chain(binary: Path) -> dict[str, str]:
    launcher = binary.resolve()
    if launcher.is_symlink() or not launcher.is_file():
        raise CapabilityError("Codex launcher is unsafe")
    chain = {"launcher_sha256": _digest(launcher)}
    is_js = launcher.suffix == ".js"
    if is_js:
        node = Path(os.environ.get("CODEX_NODE_BINARY") or shutil.which("node") or "").resolve()
        if not node.is_file() or node.is_symlink():
            raise CapabilityError("Codex JS launcher has no resolved regular Node binary")
        chain["node_sha256"] = _digest(node.resolve())
    override = os.environ.get("CODEX_NATIVE_BINARY")
    candidates = [Path(override)] if override else []
    if len(launcher.parents) >= 3:
        candidates.extend(launcher.parents[2].glob("codex-*/vendor/*/bin/codex"))
    if len(launcher.parents) >= 2:
        candidates.extend(launcher.parents[1].glob("node_modules/@openai/codex-*/vendor/*/bin/codex"))
    for native in candidates:
        if native.is_file() and not native.is_symlink():
            chain["native_sha256"] = _digest(native.resolve())
            break
    if is_js and "native_sha256" not in chain:
        raise CapabilityError("Codex JS launcher has no resolved native CLI")
    return chain


def _revalidate_native_artifacts(home: Path, artifacts: dict[str, object], worktree: Path | None,
                                 model: str, effort: str, observation_id: str) -> None:
    """Recompute native proof from retained bytes, never from claimed IDs alone."""
    required = [f"native_{mode}_{field}" for mode in ("positive", "negative", "multi_agent")
                for field in ("sha256", "session_sha256", "thread_id", "turn_id", "call_id")]
    if any(not isinstance(artifacts.get(key), str) or not artifacts[key] for key in required):
        raise CapabilityError("runtime observation lacks bound native-session artifacts")
    for mode in ("positive", "negative", "multi_agent"):
        for field in ("sha256", "session_sha256"):
            if re.fullmatch(r"[0-9a-f]{64}", artifacts[f"native_{mode}_{field}"]) is None:
                raise CapabilityError("runtime observation has malformed native digest")
    try:
        pairs = []
        for count, path in enumerate(home.iterdir()):
            if count >= 4096:
                raise CapabilityError("runtime observation artifact inventory exceeds bound")
            match = re.fullmatch(r"native-(positive|negative|multi-agent)-([0-9a-f]{32})\.jsonl", path.name)
            if match:
                pairs.append((match.group(1), match.group(2), path))
        nonces = {nonce for _mode, nonce, _path in pairs}
        if (nonces != {observation_id} or len(pairs) != 3
                or {mode for mode, _nonce, _path in pairs} != {"positive", "negative", "multi-agent"}):
            raise CapabilityError("runtime observation native transcript pair is missing or ambiguous")
        paths = {mode: path for mode, _nonce, path in pairs}
        valid, derived = _native_proof(
            paths["positive"], paths["negative"], runtime=home, nonce=next(iter(nonces)),
            worktree=worktree, model=model, effort=effort,
        )
        multi_valid, multi_derived = _native_multi_agent_proof(
            paths["multi-agent"], runtime=home, nonce=next(iter(nonces)),
            worktree=worktree, model=model, effort=effort,
        )
    except (OSError, ValueError) as error:
        raise CapabilityError("runtime observation native evidence is unsafe") from error
    derived.update(multi_derived)
    if not valid or not multi_valid or any(derived.get(key) != artifacts[key] for key in required):
        raise CapabilityError("runtime observation native transcript proof mismatch")


def _required_disable_proof(home: Path, artifacts: dict[str, object], observation_id: str) -> None:
    """Replay the private argv records proving native delegation was unavailable."""
    for name in ("ordinary", "native-positive", "native-negative", "native-multi-agent"):
        path = home / f"observer-{name}-invocation-{observation_id}.json"
        _private_regular(path, f"{name} invocation")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CapabilityError("runtime observation invocation is invalid") from exc
        if not isinstance(record, dict) or not isinstance(record.get("argv"), list):
            raise CapabilityError("runtime observation invocation is malformed")
        argv = record["argv"]
        if (any(not isinstance(value, str) for value in argv)
                or [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--disable"]
                != list(DISABLED_NATIVE_FEATURES)):
            raise CapabilityError("runtime observation lacks required native multi-agent disable flags")
        digest_key = name.replace("-", "_") + "_invocation_sha256"
        if artifacts.get(digest_key) != _digest(path):
            raise CapabilityError("runtime observation invocation digest differs")


def _require_observation(home: Path, binary: str | None = None, worktree: Path | None = None,
                         model: str = "", effort: str = "", sandbox_mode: str = "workspace-write",
                         network_enabled: bool = False, roots: list[str] | None = None) -> dict[str, object]:
    """Require an executable-produced, runtime-hash-bound canary record.

    A staged bundle has no admission value on its own.  The observer is
    intentionally separate from bundle construction: it must record each
    native surface after the exact runtime has been launched.
    """
    path = home / "runtime-observation.json"
    _private_regular(path, "runtime observation")
    try:
        observed = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise CapabilityError("runtime observation is invalid JSON") from exc
    if not isinstance(observed, dict):
        raise CapabilityError("runtime observation is not an object")
    expected = {
        **_path_identity(home, "runtime home"),
        "config_sha256": _digest(home / "config.toml"),
        "hooks_sha256": _digest(home / "hooks.json"),
        "skills_sha256": _tree_digest(home / "skills", frozenset({".system"})),
        "agents_sha256": _tree_digest(home / "agents"),
        "gsd_core_sha256": _tree_digest(home / "gsd-core"),
        "scripts_sha256": _tree_digest(home / "scripts"),
        "gsd_manifest_sha256": _digest(home / "gsd-file-manifest.json"),
    }
    if observed.get("schema") != OBSERVATION_SCHEMA or observed.get("runtime") != expected:
        raise CapabilityError("runtime observation is not bound to this exact staged bundle")
    required = {"auth", "skill_discovery", "native_network_denied", "native_multi_agent_denied", "hooks", "write_boundary"}
    outcomes = observed.get("observed")
    if not isinstance(outcomes, dict) or any(outcomes.get(name) is not True for name in required):
        raise CapabilityError("runtime observation lacks required executable capability outcomes")
    if outcomes.get("native_network_proof") != "persisted-session-paired":
        raise CapabilityError("runtime observation lacks persisted native-tool denial proof")
    if outcomes.get("native_multi_agent_proof") != "persisted-session-paired":
        raise CapabilityError("runtime observation lacks persisted native multi-agent denial proof")
    observation = observed.get("observation")
    if (not isinstance(observation, dict) or set(observation) != {"id", "created_at_unix", "environment_sha256", "telemetry_schema"}
            or re.fullmatch(r"[0-9a-f]{32}", str(observation.get("id", ""))) is None
            or not isinstance(observation.get("created_at_unix"), (int, float))
            or not isinstance(observation.get("environment_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", observation["environment_sha256"]) is None
            or observation.get("telemetry_schema") != TELEMETRY_SCHEMA):
        raise CapabilityError("runtime observation identity or telemetry schema is malformed")
    age = time.time() - float(observation["created_at_unix"])
    if age < -5 or age > OBSERVATION_FRESHNESS_SECONDS:
        raise CapabilityError("runtime observation is stale or from the future")
    artifacts = observed.get("artifacts")
    required_native_artifacts = {
        f"native_{mode}_{field}"
        for mode in ("positive", "negative")
        for field in ("session_sha256", "thread_id", "turn_id", "call_id")
    }
    if not isinstance(artifacts, dict) or any(not isinstance(artifacts.get(key), str) or not artifacts[key]
                                              for key in required_native_artifacts):
        raise CapabilityError("runtime observation lacks bound native-session artifacts")
    shell_probe = home / SHELL_PROBE_NAME
    _private_regular(shell_probe, "runtime observation shell probe")
    if artifacts.get("shell_probe_sha256") != _digest(shell_probe):
        raise CapabilityError("runtime observation shell probe differs")
    generated_skills = home / "skills" / ".system"
    if generated_skills.exists() and artifacts.get("codex_system_skills_sha256") != _tree_digest(generated_skills):
        raise CapabilityError("runtime observation generated-skill evidence differs")
    _revalidate_native_artifacts(home, artifacts, worktree, model, effort, observation["id"])
    _required_disable_proof(home, artifacts, observation["id"])
    if outcomes.get("sandbox_policy") != sandbox_mode:
        raise CapabilityError("runtime observation did not measure the selected sandbox policy")
    if sandbox_mode == "workspace-write" and outcomes.get("shell_denied") is not True:
        raise CapabilityError("runtime observation lacks workspace write-boundary denial")
    events = outcomes.get("hook_events")
    if not isinstance(events, list) or not REQUIRED_HOOK_EVENTS.issubset(events):
        raise CapabilityError("runtime observation lacks CLI-observed required hook events")
    if binary is not None and observed.get("binary") != _binary_chain(Path(binary)):
        raise CapabilityError("runtime observation was produced by a different Codex executable chain")
    roots = [str(Path(root).resolve()) for root in (roots or [])]
    if worktree is not None and observed.get("workspace") != _path_identity(worktree, "workspace"):
        raise CapabilityError("runtime observation is not bound to this workspace device/inode")
    if observed.get("execution") != {
        "model": model, "effort": effort, "sandbox": sandbox_mode,
        "network_enabled": network_enabled, "roots": roots,
        "disabled_features": list(DISABLED_NATIVE_FEATURES),
    }:
        raise CapabilityError("runtime observation is not bound to this model/effort/workspace tuple")
    recorded_supervisor = observed.get("supervisor")
    current_supervisor = current_supervisor_identity()
    if (not isinstance(recorded_supervisor, dict)
            or recorded_supervisor.get("host_id") != current_supervisor["host_id"]
            or recorded_supervisor.get("boot_id") != current_supervisor["boot_id"]):
        raise CapabilityError("runtime observation was produced by another host or boot")
    telemetry = observed.get("telemetry")
    if not isinstance(telemetry, dict) or telemetry.get("schema") != TELEMETRY_SCHEMA:
        raise CapabilityError("runtime observation telemetry is malformed")
    return observed


def verify_runtime(
    home: Path,
    worktree: Path,
    sandbox_mode: str = "workspace-write",
    network_enabled: bool = False,
    roots: list[str] | None = None,
    binary: str | None = None,
    model: str = "",
    effort: str = "",
) -> QualifiedCodexRuntime:
    worktree = worktree.resolve()
    config_path = home / "config.toml"
    if not config_path.is_file() or config_path.is_symlink():
        raise CapabilityError("runtime config is missing or unsafe")
    roots = [str(worktree)] if roots is None else [str(Path(root).resolve()) for root in roots]
    if sandbox_mode not in {"read-only", "workspace-write", "danger-full-access"}:
        raise CapabilityError("runtime requested an unknown sandbox mode")
    if not roots or roots[0] != str(worktree) or len(set(roots)) != len(roots):
        raise CapabilityError("runtime writable-root contract is malformed")
    if any(not Path(root).is_absolute() for root in roots):
        raise CapabilityError("runtime writable roots must be absolute")
    configured = _parse_toml(config_path.read_text(encoding="utf-8"))
    if configured != _runtime_policy(worktree, sandbox_mode, network_enabled, roots):
        raise CapabilityError("runtime config contains non-allowlisted execution surfaces")
    _private_regular(home / "auth.json", "runtime auth")
    hooks = home / "hooks.json"
    if not hooks.is_file() or hooks.is_symlink():
        raise CapabilityError("verified hook registration is missing")
    registered = json.loads(hooks.read_text(encoding="utf-8")).get("hooks", {})
    if not REQUIRED_HOOK_EVENTS.issubset(registered):
        raise CapabilityError("required verified hook events are absent")
    if any(not (home / name).is_dir() for name in ("skills", "agents", "gsd-core", "scripts")):
        raise CapabilityError("private GSD skill or agent discovery roots are absent")
    observed = _require_observation(home, binary, worktree, model, effort, sandbox_mode, network_enabled, roots)
    return QualifiedCodexRuntime(
        binary=tuple(sorted(observed["binary"].items())), runtime=tuple(sorted(observed["runtime"].items())),
        workspace=tuple(sorted(observed["workspace"].items())), supervisor=tuple(sorted(observed["supervisor"].items())),
        execution=tuple(sorted(observed["execution"].items())), observation=tuple(sorted(observed["observation"].items())),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    cli = sub.add_parser("cli")
    cli.add_argument("binary")
    render = sub.add_parser("render-config")
    render.add_argument("path", type=Path)
    render.add_argument("worktree", type=Path)
    render.add_argument("sandbox_mode")
    render.add_argument("network_enabled", choices=("true", "false"))
    render.add_argument("roots", nargs="+")
    runtime = sub.add_parser("runtime")
    runtime.add_argument("home", type=Path)
    runtime.add_argument("worktree", type=Path)
    runtime.add_argument("sandbox_mode")
    runtime.add_argument("network_enabled", choices=("true", "false"))
    runtime.add_argument("roots", nargs="+")
    runtime.add_argument("--binary")
    runtime.add_argument("--model", default="")
    runtime.add_argument("--effort", default="")
    args = parser.parse_args(argv)
    try:
        if args.command == "cli":
            result = admit_cli(args.binary)
        elif args.command == "render-config":
            render_runtime_config(args.path, args.worktree, args.sandbox_mode,
                                  args.network_enabled == "true", args.roots)
            result = {"schema": "ffs.codex-runtime-config/v1", "status": "rendered"}
        else:
            result = verify_runtime(
            args.home, args.worktree, args.sandbox_mode, args.network_enabled == "true", args.roots, args.binary,
            args.model, args.effort
            )
    except (CapabilityError, OSError, json.JSONDecodeError) as exc:
        print(f"host-capabilities: {exc}", file=sys.stderr)
        return 78
    print(json.dumps(result.to_dict() if isinstance(result, QualifiedCodexRuntime) else result, sort_keys=True))
    return 0


def _unique_native_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _native_jsonl(path: Path, limit: int = 2 * 1024 * 1024, *, allow_readable: bool = False) -> tuple[list[dict], str] | None:
    """Parse and hash one bounded regular file from a single anchored read."""
    directory = None
    try:
        path = path.absolute()
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parents = [(os.fstat(directory).st_dev, os.fstat(directory).st_ino)]
        for component in path.parts[1:-1]:
            if component in (".", ".."):
                return None
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
            parent_info = os.fstat(directory)
            parents.append((parent_info.st_dev, parent_info.st_ino))
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            forbidden_mode = 0o022 if allow_readable else 0o077
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_uid != os.getuid() or before.st_mode & forbidden_mode
                    or before.st_size > limit):
                return None
            content = source.read(limit + 1)
            after = os.fstat(source.fileno())
            leaf = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
            identity = tuple(getattr(before, name) for name in fields)
            if any(tuple(getattr(info, name) for name in fields) != identity for info in (after, leaf)):
                return None
        # A retained directory descriptor can outlive replacement of its path.
        # Reopen the canonical chain and leaf before returning those bytes.
        current = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(current)
            if (info.st_dev, info.st_ino) != parents[0]:
                return None
            for ordinal, component in enumerate(path.parts[1:-1], 1):
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                os.close(current)
                current = child
                info = os.fstat(current)
                if (info.st_dev, info.st_ino) != parents[ordinal]:
                    return None
            info = os.stat(path.name, dir_fd=current, follow_symlinks=False)
            if tuple(getattr(info, name) for name in fields) != identity:
                return None
        finally:
            os.close(current)
    except (OSError, ValueError):
        return None
    finally:
        if directory is not None:
            os.close(directory)
    if len(content) > limit or len(content) != before.st_size:
        return None

    try:
        records = [json.loads(line, object_pairs_hook=_unique_native_pairs)
                   for line in content.decode("utf-8").splitlines() if line.strip()]
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not records or not all(isinstance(record, dict) for record in records):
        return None
    return records, hashlib.sha256(content).hexdigest()


def _private_jsonl(path: Path, limit: int = 2 * 1024 * 1024, *, allow_readable: bool = False) -> list[dict] | None:
    """Compatibility reader shared with the ordinary shell observer."""
    captured = _native_jsonl(path, limit, allow_readable=allow_readable)
    return captured[0] if captured is not None else None


def _native_thread(records: list[dict]) -> str | None:
    ids = [record.get("thread_id") for record in records if record.get("type") == "thread.started"]
    if (len(ids) != 1 or not isinstance(ids[0], str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ids[0]) is None):
        return None
    return ids[0]


def _thread_id(transcript: Path) -> str | None:
    records = _private_jsonl(transcript)
    return _native_thread(records) if records is not None else None


def _session_proof(runtime: Path, thread_id: str, worktree: Path, model: str, effort: str,
                   program: str, expected_machine: dict[str, object], expected_status: str,
                   expected_error: str = "") -> tuple[bool, dict[str, str]]:
    if not isinstance(thread_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id) is None:
        return False, {}
    sessions = runtime / "sessions"
    candidates = []
    try:
        if sessions.is_symlink() or not sessions.is_dir():
            return False, {}
        # Do not interpret session IDs as glob patterns or descend link aliases.
        for count, path in enumerate(sessions.rglob("*")):
            if count >= 4096 or len(path.relative_to(sessions).parts) > 16 or path.is_symlink():
                return False, {}
            if path.name.startswith("rollout-") and path.name.endswith(f"{thread_id}.jsonl"):
                candidates.append(path)
        if len(candidates) != 1:
            return False, {}
        # Codex 0.154 creates rollout leaves mode 0644 inside the private 0700
        # runtime root.  Group/world write remains forbidden; the private root
        # supplies confidentiality and ancestry binding.
        captured = _native_jsonl(candidates[0], allow_readable=True)
    except (OSError, ValueError):
        return False, {}
    if captured is None:
        return False, {}
    records, digest = captured
    expected_cwd = str(worktree.resolve())
    metadata = [entry.get("payload") for entry in records if entry.get("type") == "session_meta"]
    contexts = [entry.get("payload") for entry in records if entry.get("type") == "turn_context"]
    if (len(metadata) != 1 or not isinstance(metadata[0], dict)
            or metadata[0].get("id") != thread_id or metadata[0].get("cwd") != expected_cwd):
        return False, {}
    matching = [context for context in contexts if isinstance(context, dict)
                and context.get("cwd") == expected_cwd and context.get("model") == model
                and context.get("effort") == effort]
    if len(matching) != 1:
        return False, {}
    turn_id = matching[0].get("turn_id")
    if not isinstance(turn_id, str) or not turn_id:
        return False, {}
    calls, tool_calls, outputs = [], [], []
    for entry in records:
        if entry.get("type") != "response_item":
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return False, {}
        metadata = payload.get("internal_chat_message_metadata_passthrough", {})
        if not isinstance(metadata, dict):
            return False, {}
        if metadata.get("turn_id") != turn_id:
            continue
        if payload.get("type") in {"custom_tool_call", "function_call"}:
            tool_calls.append(payload)
        if (payload.get("type") == "custom_tool_call" and payload.get("name") == "exec"
                and payload.get("namespace") in (None, "functions") and payload.get("input") == program):
            calls.append(payload)
        elif payload.get("type") == "custom_tool_call_output":
            outputs.append(payload)
    if len(tool_calls) != 1 or len(calls) != 1:
        return False, {}
    call_id = calls[0].get("call_id")
    matching_outputs = [output for output in outputs if output.get("call_id") == call_id]
    if not isinstance(call_id, str) or not call_id or len(matching_outputs) != 1:
        return False, {}
    rendered_parts = matching_outputs[0].get("output")
    if not isinstance(rendered_parts, list):
        return False, {}
    if any(not isinstance(part, dict) or (part.get("type") == "input_text"
           and not isinstance(part.get("text"), str)) for part in rendered_parts):
        return False, {}
    rendered = "\n".join(part["text"] for part in rendered_parts if part.get("type") == "input_text")
    machine_lines = []
    for line in rendered.splitlines():
        try:
            machine_lines.append(json.loads(line, object_pairs_hook=_unique_native_pairs))
        except (ValueError, RecursionError):
            continue
    exact_machine = json.dumps(expected_machine, sort_keys=True, separators=(",", ":"))
    matches = sum(json.dumps(item, sort_keys=True, separators=(",", ":")) == exact_machine
                  for item in machine_lines)
    if (matches != 1 or expected_status not in rendered
            or (expected_error and expected_error not in rendered)):
        return False, {}
    return True, {"session_sha256": digest, "thread_id": thread_id,
                  "turn_id": turn_id, "call_id": call_id}


def _native_proof(positive: Path | None, negative: Path | None, *, runtime: Path | None = None,
                  nonce: str = "", worktree: Path | None = None, model: str = "", effort: str = "") -> tuple[bool, dict[str, str]]:
    """Require persisted CLI-native tool evidence; agent prose has no proof value."""
    if positive is None or negative is None or runtime is None or worktree is None or not nonce:
        return False, {}
    positive_read, negative_read = _native_jsonl(positive), _native_jsonl(negative)
    if positive_read is None or negative_read is None:
        return False, {}
    positive_thread, negative_thread = _native_thread(positive_read[0]), _native_thread(negative_read[0])
    if not positive_thread or not negative_thread or positive_thread == negative_thread:
        return False, {}
    positive_program = f"text({{nonce:{json.dumps(nonce)},available:typeof tools.web__run==='function'}})"
    negative_program = f"text({{nonce:{json.dumps(nonce)},attempt:true}});await tools.web__run({{time:[{{utc_offset:'+00:00'}}]}})"
    positive_ok, positive_artifacts = _session_proof(
        runtime, positive_thread, worktree, model, effort, positive_program,
        {"nonce": nonce, "available": True}, "Script completed",
    )
    negative_ok, negative_artifacts = _session_proof(
        runtime, negative_thread, worktree, model, effort, negative_program,
        {"nonce": nonce, "attempt": True}, "Script failed", "TypeError: tools.web__run is not a function",
    )
    if not positive_ok or not negative_ok:
        return False, {}
    artifacts = {"native_positive_sha256": positive_read[1], "native_negative_sha256": negative_read[1]}
    artifacts.update({"native_positive_" + key: value for key, value in positive_artifacts.items()})
    artifacts.update({"native_negative_" + key: value for key, value in negative_artifacts.items()})
    return True, artifacts


def _native_multi_agent_proof(transcript: Path | None, *, runtime: Path | None = None,
                              nonce: str = "", worktree: Path | None = None,
                              model: str = "", effort: str = "") -> tuple[bool, dict[str, str]]:
    """Prove both native delegation tools were absent in the exact persisted session."""
    if transcript is None or runtime is None or worktree is None or not nonce:
        return False, {}
    captured = _native_jsonl(transcript)
    if captured is None:
        return False, {}
    records, digest = captured
    thread = _native_thread(records)
    if not thread:
        return False, {}
    program = (f"text({{nonce:{json.dumps(nonce)},multi_agent:typeof tools.multi_agent==='function',"
               "multi_agent_v2:typeof tools.multi_agent_v2==='function'})")
    valid, artifacts = _session_proof(
        runtime, thread, worktree, model, effort, program,
        {"nonce": nonce, "multi_agent": False, "multi_agent_v2": False}, "Script completed",
    )
    if not valid:
        return False, {}
    return True, {"native_multi_agent_sha256": digest,
                  **{"native_multi_agent_" + key: value for key, value in artifacts.items()}}


if __name__ == "__main__":
    raise SystemExit(main())
