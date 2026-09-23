"""Independent acceptance for the M4 per-worker policy boundary."""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sys
import uuid

import pytest


LIB = Path(__file__).resolve().parents[2]
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

REPOSITORY = "fixture-repository"
RUN = "worker-policy-run"
ACTIVITY = "10000000-0000-4000-8000-000000000001"
ATTEMPT = "10000000-0000-4000-8000-000000000002"
CAPABILITY_RECEIPT = "10000000-0000-4000-8000-000000000003"
COMPOSITION = "a" * 64
RUNTIME = "b" * 64


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _private(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True)
    path.chmod(0o700)
    return path


def _policy_inputs(tmp_path: Path):
    from run_context import RunContext
    from run_state.worker_policy import WorkerRegistration, WorkerRuntimeRoots

    primary = _private(tmp_path / "primary")
    common = _private(tmp_path / "repo.git")
    state = _private(tmp_path / "state")
    sibling = _private(tmp_path / "sibling")
    workspace = _private(tmp_path / "workspaces" / "run")
    socket_root = _private(tmp_path / "sockets")
    scratch = _private(tmp_path / "attempt-scratch")
    runtime = _private(tmp_path / "runtime")
    policy_root = _private(tmp_path / "policies")
    endpoint = socket_root / "worker.sock"
    context = RunContext(
        repository_id=REPOSITORY, run_id=RUN, activity_id=ACTIVITY,
        attempt_id=ATTEMPT, generation=7, workspace=str(workspace),
        evidence_root=str(tmp_path / "evidence"), workspace_state="ready",
        ready=True, selected_input_manifest_hash="c" * 64,
        runtime_tuple_hash=RUNTIME,
    )
    registration = WorkerRegistration(
        repository_id=REPOSITORY, run_id=RUN, activity_id=ACTIVITY,
        attempt_id=ATTEMPT, generation=7, workspace=str(workspace),
        primary_root=str(primary), state_root=str(state),
        git_common_dir=str(common), sibling_roots=(str(sibling),),
        socket_root=str(socket_root), ipc_endpoint=str(endpoint),
        policy_root=str(policy_root), fixture_epoch_id=str(uuid.uuid4()),
        capability_receipt_id=CAPABILITY_RECEIPT,
        composition_evidence_hash=COMPOSITION,
    )
    roots = WorkerRuntimeRoots(
        read_only_roots=(str(runtime),), attempt_scratch=str(scratch),
        runtime_tuple_hash=RUNTIME, manifest_sha256="d" * 64,
    )
    return context, registration, roots


def test_worker_policy_is_closed_and_hashes_canonical_roots(tmp_path: Path) -> None:
    from run_state.worker_policy import build_worker_policy

    context, registration, roots = _policy_inputs(tmp_path)
    policy = build_worker_policy(context, registration, roots)
    payload = asdict(policy)
    assert set(payload) == {
        "schema_version", "repository_id", "run_id", "activity_id",
        "attempt_id", "generation", "workspace", "read_only_roots",
        "writable_roots", "ipc_endpoint", "network_policy",
        "sandbox_layers", "composition_evidence_hash", "policy_sha256",
    }
    assert payload["schema_version"] == 1
    assert payload["repository_id"] == REPOSITORY
    assert payload["run_id"] == RUN
    assert payload["activity_id"] == ACTIVITY
    assert payload["attempt_id"] == ATTEMPT
    assert payload["generation"] == 7
    assert payload["workspace"] == str(Path(context.workspace).resolve())
    assert payload["writable_roots"] == (
        str(Path(context.workspace).resolve()),
        str(Path(roots.attempt_scratch).resolve()),
    )
    assert payload["read_only_roots"] == tuple(
        str(Path(item).resolve()) for item in roots.read_only_roots
    )
    assert set(payload["network_policy"]) == {
        "model_egress", "shell_network", "native_network",
    }
    assert set(payload["sandbox_layers"]) == {
        "filesystem", "shell_network", "native_tools", "nesting",
    }
    unsigned = dict(payload)
    unsigned.pop("policy_sha256")
    assert policy.policy_sha256 == hashlib.sha256(_canonical(unsigned)).hexdigest()


@pytest.mark.parametrize(
    "unsafe_kind",
    ["primary-workspace", "state-scratch", "git-scratch", "sibling-scratch", "broad-parent", "symlink-scratch"],
)
def test_worker_policy_refuses_unsafe_roots_even_when_inputs_agree(
    tmp_path: Path, unsafe_kind: str,
) -> None:
    from run_state.worker_policy import WorkerPolicyRefused, build_worker_policy

    context, registration, roots = _policy_inputs(tmp_path)
    if unsafe_kind == "primary-workspace":
        context = replace(context, workspace=registration.primary_root)
        registration = replace(registration, workspace=registration.primary_root)
    elif unsafe_kind == "state-scratch":
        roots = replace(roots, attempt_scratch=registration.state_root)
    elif unsafe_kind == "git-scratch":
        roots = replace(roots, attempt_scratch=registration.git_common_dir)
    elif unsafe_kind == "sibling-scratch":
        roots = replace(roots, attempt_scratch=registration.sibling_roots[0])
    elif unsafe_kind == "broad-parent":
        roots = replace(roots, attempt_scratch=str(tmp_path))
    else:
        alias = tmp_path / "scratch-alias"
        alias.symlink_to(Path(roots.attempt_scratch), target_is_directory=True)
        roots = replace(roots, attempt_scratch=str(alias))
    with pytest.raises(WorkerPolicyRefused) as refused:
        build_worker_policy(context, registration, roots)
    assert refused.value.code == "UNSAFE_WORKER_ROOT"


@pytest.mark.parametrize(
    "change",
    ["repository", "run", "activity", "attempt", "generation", "workspace", "runtime"],
)
def test_worker_policy_refuses_context_and_runtime_binding_mismatch(
    tmp_path: Path, change: str,
) -> None:
    from run_state.worker_policy import WorkerPolicyRefused, build_worker_policy

    context, registration, roots = _policy_inputs(tmp_path)
    if change == "runtime":
        roots = replace(roots, runtime_tuple_hash="0" * 64)
    else:
        field = {
            "repository": "repository_id", "run": "run_id", "activity": "activity_id",
            "attempt": "attempt_id", "generation": "generation", "workspace": "workspace",
        }[change]
        value = 8 if field == "generation" else (
            str(_private(tmp_path / "other-workspace")) if field == "workspace" else "other"
        )
        registration = replace(registration, **{field: value})
    with pytest.raises(WorkerPolicyRefused) as refused:
        build_worker_policy(context, registration, roots)
    assert refused.value.code == "WORKER_POLICY_MISMATCH"


def test_worker_policy_refuses_endpoint_outside_registered_socket_root(tmp_path: Path) -> None:
    from run_state.worker_policy import WorkerPolicyRefused, build_worker_policy

    context, registration, roots = _policy_inputs(tmp_path)
    registration = replace(
        registration, ipc_endpoint=str(_private(tmp_path / "foreign-sockets") / "worker.sock"),
    )
    with pytest.raises(WorkerPolicyRefused) as refused:
        build_worker_policy(context, registration, roots)
    assert refused.value.code == "UNSAFE_IPC_ENDPOINT"


def test_contained_argv_has_no_uncontained_platform_fallback(tmp_path: Path) -> None:
    from run_state.worker_policy import (
        WorkerPolicyRefused, build_contained_argv, build_worker_policy,
    )

    context, registration, roots = _policy_inputs(tmp_path)
    policy = build_worker_policy(context, registration, roots)
    argv = (sys.executable, "-c", "pass")
    platform = "darwin" if sys.platform == "darwin" else "linux"
    if platform == "darwin":
        contained = build_contained_argv(policy, argv, platform=platform)
        assert type(contained) is list and tuple(contained[-len(argv):]) == argv
        assert contained[0] == "/usr/bin/sandbox-exec"
    else:
        # bwrap cannot currently prove the required anti-nesting boundary.
        # Refusal is safer than a nominally isolated worker backend.
        with pytest.raises(WorkerPolicyRefused) as refused:
            build_contained_argv(policy, argv, platform=platform)
        assert refused.value.code == "CONFINEMENT_UNAVAILABLE"
    with pytest.raises(WorkerPolicyRefused) as refused:
        build_contained_argv(policy, argv, platform="unsupported")
    assert refused.value.code == "CONFINEMENT_UNAVAILABLE"


def test_contained_argv_revalidates_policy_hash_and_smoke_fails_closed(tmp_path: Path) -> None:
    from run_state.worker_policy import (
        WorkerPolicyRefused, build_contained_argv, build_worker_policy, smoke_containment,
    )

    context, registration, roots = _policy_inputs(tmp_path)
    policy = build_worker_policy(context, registration, roots)
    with pytest.raises(WorkerPolicyRefused) as refused:
        build_contained_argv(replace(policy, workspace=str(tmp_path / "forged")), (sys.executable, "-c", "pass"))
    assert refused.value.code == "POLICY_HASH_MISMATCH"

    # This starts a real disposable sandbox-exec process on macOS. The
    # fixture runtime root deliberately excludes /usr, so qualification must
    # fail rather than treat a parser-valid profile as a usable containment.
    if sys.platform == "darwin":
        with pytest.raises(WorkerPolicyRefused) as refused:
            smoke_containment(policy)
        assert refused.value.code == "CONFINEMENT_UNAVAILABLE"
    else:
        with pytest.raises(WorkerPolicyRefused) as refused:
            smoke_containment(policy)
        assert refused.value.code == "CONFINEMENT_UNAVAILABLE"
