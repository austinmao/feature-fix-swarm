"""Bind prepared native review closure to an admitted ordinary host material.

This is launch material only.  It neither starts a process nor creates a
qualification, observation, or authority receipt.  A Supervisor must still
validate the fresh runtime-receipt row and own credential revocation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import stat
from typing import Final

from host_capabilities import ArtifactReviewMaterial, CapabilityError, validate_artifact_review_material
from .claude_host import ClaudeLaunchMaterial
from .codex_host import CodexLaunchMaterial
from .ownership import OwnershipRefused
from .native_review_runtime import (
    NativeReviewMaterial, NativeReviewRuntimeRefused, _canonical, _digest,
    _read_checked, _write_private_new, validate_native_review_material,
)


NATIVE_REVIEW_LAUNCH_SCHEMA: Final = "ffs.native-review-launch/v1"


class NativeReviewTransportRefused(ValueError):
    """A prepared native review cannot be attached to this host invocation."""


def read_native_review_launch(path: Path, *, expected_material_sha256: str,
                             credential_required: bool = False) -> NativeReviewLaunchMaterial:
    """Restore only a bounded canonical material bound by the original dispatch."""
    try:
        raw, _identity = _read_checked(path, "native review launch material", private=True)
        data = json.loads(raw)
        if type(data) is not dict:
            raise ValueError("native launch material must be an object")
        native = dict(data.pop("native"))
        artifact = dict(data.pop("artifact"))
        native["argv"] = tuple(native["argv"])
        for name in ("environment", "provenance"):
            native[name] = tuple(tuple(pair) for pair in native[name])
        for name in ("requested_model", "environment", "selected_artifacts", "selected_contents", "provenance"):
            if artifact[name] is not None:
                artifact[name] = tuple(tuple(pair) for pair in artifact[name])
        value = NativeReviewLaunchMaterial(**data, native=NativeReviewMaterial(**native),
                                          artifact=ArtifactReviewMaterial(**artifact))
        if raw != _canonical(value.to_dict()) + b"\n":
            raise ValueError("noncanonical native material")
        return validate_native_review_launch_material(
            value, expected_material_sha256=expected_material_sha256,
            credential_required=credential_required)
    except (NativeReviewRuntimeRefused, TypeError, KeyError, ValueError, RecursionError) as error:
        raise NativeReviewTransportRefused("retained native launch material is invalid") from error


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise NativeReviewTransportRefused(f"{label} is malformed")
    return value


def _ordinary_fields(value: object):
    if type(value) is CodexLaunchMaterial:
        return ("codex", value.binary, value.version, value.cwd, value.model, value.effort,
                value.runtime, value.runtime_sha256, value.auth_path, value.auth_sha256,
                value.auth_device, value.auth_inode)
    if type(value) is ClaudeLaunchMaterial:
        return ("claude", value.binary, value.version, value.cwd, value.model, value.effort,
                value.runtime, value.runtime_sha256, value.credential_path, value.credential_sha256,
                value.credential_device, value.credential_inode)
    raise NativeReviewTransportRefused("ordinary material has invalid type")


def _ordinary_binary_sha256(binary: object) -> str:
    try:
        chain = dict(binary)
        if len(chain) != len(binary):
            raise ValueError("duplicate binary identity keys")
    except (TypeError, ValueError) as error:
        raise NativeReviewTransportRefused("ordinary binary identity is malformed") from error
    # Both qualified adapters retain the launcher identity, which is the
    # executable identity consumed by NativeReviewMaterial.
    return _sha256(chain.get("launcher_sha256"), "ordinary binary identity")


def _runtime_tuple_hash(runtime: object) -> str:
    """Use the same stable tuple, not the adapter's serialized runtime hash."""
    try:
        from .state import qualified_runtime_tuple_hash
        return _sha256(qualified_runtime_tuple_hash(runtime), "qualified runtime tuple")
    except (OwnershipRefused, ValueError) as error:
        raise NativeReviewTransportRefused("ordinary runtime is malformed") from error


def _credential_target(native: NativeReviewMaterial) -> Path:
    root = Path(native.runtime_root)
    return root / "auth.json" if native.host == "codex" else root / "claude-config" / ".credentials.json"


def _read_source(path: str, digest: str, device: int, inode: int) -> bytes:
    source = Path(path)
    try:
        data, identity = _read_checked(source, "ordinary host credential", private=True)
        mode = stat.S_IMODE(source.lstat().st_mode)
    except (NativeReviewRuntimeRefused, OSError) as error:
        raise NativeReviewTransportRefused("ordinary credential is unavailable") from error
    if (identity != (device, inode) or mode != 0o600
            or _digest(data) != _sha256(digest, "ordinary credential digest")):
        raise NativeReviewTransportRefused("ordinary credential drifted")
    return data


@dataclass(frozen=True)
class NativeReviewLaunchMaterial:
    """Exact Supervisor payload for one restricted authenticated review.

    ``credential_*`` identify a copy in the new native private runtime, never
    the ordinary runtime's credential.  Supervisor removes that copy under its
    existing host-specific guard.  The source identity is retained only as a
    non-secret audit binding.
    """
    schema: str
    native: NativeReviewMaterial
    artifact: ArtifactReviewMaterial
    runtime_receipt_sha256: str
    runtime_tuple_hash: str
    ordinary_runtime_sha256: str
    artifact_prompt_sha256: str
    credential_path: str
    credential_sha256: str
    credential_device: int
    credential_inode: int
    source_credential_sha256: str
    source_credential_device: int
    source_credential_inode: int

    def execution_environment(self) -> dict[str, str]:
        return dict(self.native.environment)

    def replay_binding(self) -> dict[str, object]:
        return {
            "schema": self.schema, "operation": "native-restricted-review-launch",
            "native": self.native.replay_binding(),
            "artifact": self.artifact.replay_binding(),
            "runtime_receipt_sha256": self.runtime_receipt_sha256,
            "runtime_tuple_hash": self.runtime_tuple_hash,
            "ordinary_runtime_sha256": self.ordinary_runtime_sha256,
            "artifact_prompt_sha256": self.artifact_prompt_sha256,
            "credential_sha256": self.credential_sha256,
            "source_credential_sha256": self.source_credential_sha256,
            "credential_identity": [self.credential_device, self.credential_inode],
            "source_credential_identity": [self.source_credential_device, self.source_credential_inode],
            "material_sha256": self.material_sha256(),
        }

    def material_sha256(self) -> str:
        # Deliberately cover every immutable field, including native root,
        # workspace identities, argv, and closure paths omitted from replay.
        return _digest(_canonical(self.to_dict()))

    def to_dict(self) -> dict:
        """Preserve the retained pre-context representation when absent."""
        value = asdict(self)
        if self.artifact.review_context_json is None:
            value["artifact"].pop("review_context_json")
        return value


def prepare_native_review_launch(*, native: NativeReviewMaterial, artifact: ArtifactReviewMaterial,
                                 ordinary: CodexLaunchMaterial | ClaudeLaunchMaterial,
                                 runtime_receipt_sha256: str) -> NativeReviewLaunchMaterial:
    """Stage one guarded credential copy from an already-built ordinary adapter.

    The caller supplies the exact ordinary material it obtained after the
    four-probe qualification.  This function never consults HOME, keychains,
    environment variables, or any ambient credential location.
    """
    try:
        native = validate_native_review_material(native)
        artifact = validate_artifact_review_material(artifact)
    except (NativeReviewRuntimeRefused, CapabilityError) as error:
        raise NativeReviewTransportRefused("prepared review closure is invalid") from error
    (host, binary, version, cwd, model, effort, runtime, ordinary_runtime_sha256,
     source_path, source_sha256, source_device, source_inode) = _ordinary_fields(ordinary)
    tuple_hash = _runtime_tuple_hash(runtime)
    if (not ordinary.argv or native.host != host or native.binary != ordinary.argv[0]
            or native.binary_sha256 != _ordinary_binary_sha256(binary)
            or native.cli_version != version or native.workspace != cwd
            or native.requested_model != model or native.effort != effort
            or native.runtime_identity != tuple_hash):
        raise NativeReviewTransportRefused("native and ordinary materials differ")
    if (artifact.host != host or artifact.effective_model != model or artifact.effective_effort != effort
            or native.argv[-1] != artifact.prompt
            or native.prompt_sha256 != hashlib.sha256(artifact.prompt.encode("utf-8")).hexdigest()):
        raise NativeReviewTransportRefused("artifact envelope is not the native prompt")
    receipt = _sha256(runtime_receipt_sha256, "runtime receipt")
    ordinary_runtime_sha = _sha256(ordinary_runtime_sha256, "ordinary runtime")
    payload = runtime.to_dict()
    if ordinary_runtime_sha != _digest(_canonical(payload)):
        raise NativeReviewTransportRefused("ordinary serialized runtime drifted")
    credential_name = "auth.json" if host == "codex" else ".credentials.json"
    schema = "ffs.qualified-codex-runtime/v1" if host == "codex" else "ffs.qualified-claude-runtime/v1"
    if (payload["schema"] != schema or payload["binary"] != dict(binary)
            or payload["workspace"]["path"] != native.workspace
            or payload["execution"]["model"] != model or payload["execution"]["effort"] != effort
            or Path(source_path) != Path(payload["runtime"]["path"]) / credential_name):
        raise NativeReviewTransportRefused("ordinary qualified bindings differ")
    source = _read_source(source_path, source_sha256, source_device, source_inode)
    target = _credential_target(native)
    try:
        target_digest, target_identity = _write_private_new(target, source)
    except NativeReviewRuntimeRefused as error:
        raise NativeReviewTransportRefused("native credential staging failed") from error
    if target_digest != source_sha256:
        raise NativeReviewTransportRefused("native credential staging drifted")
    return NativeReviewLaunchMaterial(
        NATIVE_REVIEW_LAUNCH_SCHEMA, native, artifact, receipt, tuple_hash, ordinary_runtime_sha,
        hashlib.sha256(artifact.prompt.encode("utf-8")).hexdigest(), str(target), target_digest,
        target_identity[0], target_identity[1], source_sha256, source_device, source_inode,
    )


def validate_native_review_launch_material(
    value: object, *, expected_material_sha256: str, credential_required: bool = True,
) -> NativeReviewLaunchMaterial:
    """Validate a previously bound material; postlaunch requires revocation.

    The expected full digest must come from the prelaunch authority binding.
    An observation or a digest recomputed from untrusted completion input
    cannot supply that authority.
    """
    if type(value) is not NativeReviewLaunchMaterial or value.schema != NATIVE_REVIEW_LAUNCH_SCHEMA:
        raise NativeReviewTransportRefused("native launch material has invalid type")
    if (type(credential_required) is not bool
            or value.material_sha256() != _sha256(expected_material_sha256, "material")):
        raise NativeReviewTransportRefused("native launch binding drifted")
    try:
        native = validate_native_review_material(value.native)
        artifact = validate_artifact_review_material(value.artifact)
    except (NativeReviewRuntimeRefused, CapabilityError) as error:
        raise NativeReviewTransportRefused("native review closure drifted") from error
    for field, label in ((value.runtime_receipt_sha256, "runtime receipt"),
                         (value.runtime_tuple_hash, "runtime tuple"),
                         (value.ordinary_runtime_sha256, "ordinary runtime"),
                         (value.artifact_prompt_sha256, "artifact prompt"),
                         (value.credential_sha256, "credential"),
                         (value.source_credential_sha256, "source credential")):
        _sha256(field, label)
    if (value.runtime_tuple_hash != native.runtime_identity
            or value.artifact_prompt_sha256 != native.prompt_sha256
            or native.argv[-1] != artifact.prompt
            or artifact.host != native.host or artifact.effective_model != native.requested_model
            or artifact.effective_effort != native.effort):
        raise NativeReviewTransportRefused("native review bindings differ")
    target = _credential_target(native)
    if value.credential_path != str(target) or value.credential_sha256 != value.source_credential_sha256:
        raise NativeReviewTransportRefused("native credential binding drifted")
    if any(type(item) is not int or item < 0 for item in (
            value.credential_device, value.credential_inode,
            value.source_credential_device, value.source_credential_inode)):
        raise NativeReviewTransportRefused("credential identity is malformed")
    if not credential_required:
        try:
            target.lstat()
        except FileNotFoundError:
            return value
        except OSError as error:
            raise NativeReviewTransportRefused("native credential is unavailable") from error
        raise NativeReviewTransportRefused("native credential revocation is unproven")
    try:
        data, identity = _read_checked(target, "native review credential", private=True)
        mode = stat.S_IMODE(target.lstat().st_mode)
    except (NativeReviewRuntimeRefused, OSError) as error:
        raise NativeReviewTransportRefused("native credential is unavailable") from error
    if (identity != (value.credential_device, value.credential_inode) or mode != 0o600
            or _digest(data) != value.credential_sha256):
        raise NativeReviewTransportRefused("native credential drifted")
    return value
