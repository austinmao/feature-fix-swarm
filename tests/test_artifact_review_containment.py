"""MacOS effect checks for the offline artifact-review policy boundary."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import os
import shutil
import socket
import subprocess
import sys
import uuid

import pytest


LIB = Path(__file__).resolve().parents[1] / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))


REPOSITORY = "artifact-review-repository"
RUN = "artifact-review-run"
ACTIVITY = "20000000-0000-4000-8000-000000000001"
ATTEMPT = "20000000-0000-4000-8000-000000000002"
COMPOSITION = "a" * 64
RUNTIME = "b" * 64


def _private(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True)
    path.chmod(0o700)
    return path


def _inputs(tmp_path: Path, *, executable: str, runtime_roots: tuple[str, ...]):
    from run_context import RunContext
    from run_state.worker_policy import ArtifactReviewRoots, WorkerRegistration

    primary = _private(tmp_path / "primary")
    state = _private(tmp_path / "state")
    common = _private(tmp_path / "repo.git")
    sibling = _private(tmp_path / "sibling")
    workspace = _private(tmp_path / "workspace")
    socket_root = _private(tmp_path / "sockets")
    policy_root = _private(tmp_path / "policies")
    artifact = _private(tmp_path / "public-artifacts")
    scratch = _private(tmp_path / "attempt-scratch")
    context = RunContext(
        repository_id=REPOSITORY, run_id=RUN, activity_id=ACTIVITY,
        attempt_id=ATTEMPT, generation=3, workspace=str(workspace),
        evidence_root=str(tmp_path / "evidence"), workspace_state="ready", ready=True,
        selected_input_manifest_hash="c" * 64, runtime_tuple_hash=RUNTIME,
    )
    registration = WorkerRegistration(
        repository_id=REPOSITORY, run_id=RUN, activity_id=ACTIVITY,
        attempt_id=ATTEMPT, generation=3, workspace=str(workspace),
        primary_root=str(primary), state_root=str(state), git_common_dir=str(common),
        sibling_roots=(str(sibling),), socket_root=str(socket_root),
        ipc_endpoint=str(socket_root / "implementation-worker.sock"),
        policy_root=str(policy_root), fixture_epoch_id=str(uuid.uuid4()),
        capability_receipt_id=str(uuid.uuid4()), composition_evidence_hash=COMPOSITION,
    )
    roots = ArtifactReviewRoots(
        artifact_root=str(artifact), runtime_read_only_roots=runtime_roots,
        attempt_scratch=str(scratch), runtime_tuple_hash=RUNTIME,
        manifest_sha256="d" * 64, executable=executable,
    )
    return context, registration, roots, {
        "primary": primary, "state": state, "common": common, "sibling": sibling,
        "workspace": workspace, "socket": socket_root, "policy": policy_root,
        "artifact": artifact, "scratch": scratch,
    }


def _tool(tmp_path: Path) -> str:
    runtime = _private(tmp_path / "runtime")
    executable = runtime / "review-tool"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    return str(executable)


def test_artifact_review_policy_has_closed_no_ipc_descriptor_and_refuses_protected_roots(
    tmp_path: Path,
) -> None:
    from run_state.worker_policy import (
        WorkerPolicyRefused, _darwin_artifact_review_profile,
        build_artifact_review_policy,
    )

    executable = _tool(tmp_path)
    context, registration, roots, paths = _inputs(
        tmp_path, executable=executable, runtime_roots=(str(Path(executable).parent),),
    )
    policy = build_artifact_review_policy(context, registration, roots)
    assert policy.ipc_endpoint is None
    assert policy.writable_roots == (str(paths["scratch"]),)
    assert policy.read_only_roots == (str(paths["artifact"]), str(Path(executable).parent))
    profile = _darwin_artifact_review_profile(policy)
    assert registration.ipc_endpoint not in profile
    assert '(deny network*)' in profile
    assert '(deny process-fork)' in profile
    assert f'(literal {json.dumps(policy.executable)})' in profile

    for protected in (
        registration.primary_root, registration.state_root, registration.git_common_dir,
        registration.sibling_roots[0], registration.socket_root, registration.policy_root,
        registration.workspace, str(Path.home()),
    ):
        with pytest.raises(WorkerPolicyRefused) as refused:
            build_artifact_review_policy(
                context, registration, replace(roots, artifact_root=protected),
            )
        assert refused.value.code == "UNSAFE_WORKER_ROOT"
        with pytest.raises(WorkerPolicyRefused) as refused:
            build_artifact_review_policy(
                context, registration, replace(roots, runtime_read_only_roots=(protected,)),
            )
        assert refused.value.code == "UNSAFE_WORKER_ROOT"
        with pytest.raises(WorkerPolicyRefused) as refused:
            build_artifact_review_policy(
                context, registration, replace(roots, attempt_scratch=protected),
            )
        assert refused.value.code == "UNSAFE_WORKER_ROOT"


def test_artifact_review_policy_binds_exact_executable_and_refuses_unavailable_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from run_state.worker_policy import (
        WorkerPolicyRefused, WorkerRuntimeRoots, build_artifact_review_argv,
        build_artifact_review_policy, build_contained_argv, build_worker_policy,
    )

    executable = _tool(tmp_path)
    context, registration, roots, _paths = _inputs(
        tmp_path, executable=executable, runtime_roots=(str(Path(executable).parent),),
    )
    policy = build_artifact_review_policy(context, registration, roots)
    with pytest.raises(WorkerPolicyRefused) as refused:
        build_artifact_review_argv(policy, ("/usr/bin/true",), platform="darwin")
    assert refused.value.code == "UNSAFE_REVIEW_EXECUTABLE"
    with pytest.raises(WorkerPolicyRefused) as refused:
        build_artifact_review_argv(policy, (policy.executable,), platform="linux")
    assert refused.value.code == "CONFINEMENT_UNAVAILABLE"
    monkeypatch.setattr("run_state.worker_policy.os.path.isfile", lambda _path: False)
    with pytest.raises(WorkerPolicyRefused) as refused:
        build_artifact_review_argv(policy, (policy.executable,), platform="darwin")
    assert refused.value.code == "CONFINEMENT_UNAVAILABLE"
    with pytest.raises(WorkerPolicyRefused) as refused:
        build_artifact_review_argv(replace(policy, executable="/usr/bin/true"), ("/usr/bin/true",))
    assert refused.value.code == "POLICY_HASH_MISMATCH"
    worker_policy = build_worker_policy(
        context, registration,
        WorkerRuntimeRoots(roots.runtime_read_only_roots, roots.attempt_scratch,
                           roots.runtime_tuple_hash, roots.manifest_sha256),
    )
    with pytest.raises(WorkerPolicyRefused) as refused:
        build_contained_argv(policy, (policy.executable,), platform="darwin")
    assert refused.value.code == "CONFINEMENT_UNAVAILABLE"
    with pytest.raises(WorkerPolicyRefused) as refused:
        build_artifact_review_argv(worker_policy, (policy.executable,), platform="darwin")
    assert refused.value.code == "CONFINEMENT_UNAVAILABLE"


def test_artifact_review_policy_hash_binds_runtime_and_manifest(tmp_path: Path) -> None:
    from run_state.worker_policy import build_artifact_review_policy

    executable = _tool(tmp_path)
    context, registration, roots, _paths = _inputs(
        tmp_path, executable=executable, runtime_roots=(str(Path(executable).parent),),
    )
    policy = build_artifact_review_policy(context, registration, roots)
    runtime_changed = build_artifact_review_policy(
        replace(context, runtime_tuple_hash="e" * 64), registration,
        replace(roots, runtime_tuple_hash="e" * 64),
    )
    manifest_changed = build_artifact_review_policy(
        context, registration, replace(roots, manifest_sha256="f" * 64),
    )
    assert runtime_changed.runtime_tuple_hash == "e" * 64
    assert manifest_changed.manifest_sha256 == "f" * 64
    assert policy.policy_sha256 != runtime_changed.policy_sha256
    assert policy.policy_sha256 != manifest_changed.policy_sha256


def _build_probe(runtime: Path) -> str:
    source = runtime / "review_probe.c"
    executable = runtime / "review_probe"
    source.write_text(
        """
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static int fail(const char *message) { perror(message); return 1; }
static void *thread_main(void *ignored) { return ignored; }
static int copy_file(const char *input, const char *output) {
    char buffer[128]; ssize_t count;
    int in = open(input, O_RDONLY); if (in < 0) return fail("open-input");
    int out = open(output, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (out < 0) { close(in); return fail("open-output"); }
    while ((count = read(in, buffer, sizeof(buffer))) > 0)
        if (write(out, buffer, (size_t)count) != count) { close(in); close(out); return fail("write"); }
    close(in); close(out); return count < 0 ? fail("read") : 0;
}
int main(int argc, char **argv) {
    if (argc < 2) return 64;
    if (!strcmp(argv[1], "copy") && argc == 4) return copy_file(argv[2], argv[3]);
    if (!strcmp(argv[1], "read") && argc == 3) { int fd=open(argv[2],O_RDONLY); if(fd<0)return fail("read"); close(fd); return 0; }
    if (!strcmp(argv[1], "write") && argc == 3) { int fd=open(argv[2],O_WRONLY|O_CREAT|O_TRUNC,0600); if(fd<0)return fail("write"); close(fd); return 0; }
    if (!strcmp(argv[1], "symlink-read") && argc == 4) { if(symlink(argv[3],argv[2]))return fail("symlink"); char path[1024]; snprintf(path,sizeof(path),"%s/secret.txt",argv[2]); int fd=open(path,O_RDONLY); if(fd<0)return fail("symlink-read"); close(fd); return 0; }
    if (!strcmp(argv[1], "hardlink") && argc == 4) { if(link(argv[2],argv[3]))return fail("link"); return 0; }
    if (!strcmp(argv[1], "rename") && argc == 4) { if(rename(argv[2],argv[3]))return fail("rename"); return 0; }
    if (!strcmp(argv[1], "fork")) { pid_t child=fork(); if(child<0)return fail("fork"); if(!child)_exit(0); return waitpid(child,NULL,0)<0?fail("waitpid"):0; }
    if (!strcmp(argv[1], "exec") && argc == 3) { execl(argv[2],argv[2],(char *)NULL); return fail("exec"); }
    if (!strcmp(argv[1], "thread")) { pthread_t thread; if(pthread_create(&thread,NULL,thread_main,NULL))return fail("pthread_create"); return pthread_join(thread,NULL)?fail("pthread_join"):0; }
    if (!strcmp(argv[1], "network") && argc == 3) { struct sockaddr_in peer={0}; int fd=socket(AF_INET,SOCK_STREAM,0); if(fd<0)return fail("socket"); peer.sin_family=AF_INET; peer.sin_port=htons((unsigned short)atoi(argv[2])); inet_pton(AF_INET,"127.0.0.1",&peer.sin_addr); if(connect(fd,(struct sockaddr *)&peer,sizeof(peer)))return fail("connect"); close(fd); return 0; }
    return 64;
}
""",
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["/usr/bin/cc", "-Wall", "-Werror", "-o", str(executable), str(source)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    return str(executable)


def _run(policy, argv: tuple[str, ...], scratch: Path) -> subprocess.CompletedProcess[bytes]:
    from run_state.worker_policy import build_artifact_review_argv

    return subprocess.run(
        build_artifact_review_argv(policy, argv, platform="darwin"),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={"HOME": str(scratch), "TMPDIR": str(scratch), "PATH": "/usr/bin:/bin", "LANG": "C"},
        timeout=10, check=False,
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox-exec effect qualification")
def test_macos_artifact_review_policy_enforces_narrow_os_effects(tmp_path: Path) -> None:
    from run_state.worker_policy import build_artifact_review_policy

    runtime = _private(tmp_path / "fixture-runtime")
    executable = _build_probe(runtime)
    runtime_roots = (str(runtime), "/usr/lib", "/System/Library")
    context, registration, roots, paths = _inputs(
        tmp_path, executable=executable, runtime_roots=runtime_roots,
    )
    policy = build_artifact_review_policy(context, registration, roots)
    artifact = paths["artifact"] / "review-input.txt"
    artifact.write_text("public artifact\n", encoding="utf-8")
    protected = (
        paths["primary"], paths["state"], paths["common"], paths["sibling"],
        paths["socket"], paths["policy"],
    )
    for root in protected:
        (root / "secret.txt").write_text("private material\n", encoding="utf-8")
    copied = paths["scratch"] / "copied.txt"
    completed = _run(
        policy,
        (policy.executable, "copy", str(artifact), str(copied)),
        paths["scratch"],
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    assert copied.read_text(encoding="utf-8") == "public artifact\n"

    for target in (
        artifact, paths["workspace"] / "escape.txt",
        *(root / "escape.txt" for root in protected),
    ):
        completed = _run(
            policy, (policy.executable, "write", str(target)),
            paths["scratch"],
        )
        assert completed.returncode != 0
    assert artifact.read_text(encoding="utf-8") == "public artifact\n"
    assert not (paths["workspace"] / "escape.txt").exists()
    for root in protected:
        assert not (root / "escape.txt").exists()
        completed = _run(
            policy, (policy.executable, "read", str(root / "secret.txt")), paths["scratch"],
        )
        assert completed.returncode != 0

    alias = paths["scratch"] / "primary-link"
    completed = _run(
        policy,
        (policy.executable, "symlink-read", str(alias), str(paths["primary"])),
        paths["scratch"],
    )
    assert completed.returncode != 0
    assert alias.is_symlink()
    assert os.readlink(alias) == str(paths["primary"])
    assert (paths["primary"] / "secret.txt").read_text(encoding="utf-8") == "private material\n"

    completed = _run(
        policy,
        (policy.executable, "hardlink", str(artifact), str(paths["scratch"] / "artifact-link")), paths["scratch"],
    )
    assert completed.returncode != 0
    assert not (paths["scratch"] / "artifact-link").exists()
    completed = _run(
        policy,
        (policy.executable, "rename", str(artifact), str(paths["scratch"] / "renamed-artifact")), paths["scratch"],
    )
    assert completed.returncode != 0
    assert artifact.read_text(encoding="utf-8") == "public artifact\n"
    completed = _run(policy, (policy.executable, "thread"), paths["scratch"])
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    completed = _run(policy, (policy.executable, "fork"), paths["scratch"])
    assert completed.returncode != 0
    unapproved = runtime / "unapproved-helper"
    shutil.copyfile(policy.executable, unapproved)
    unapproved.chmod(0o700)
    completed = _run(policy, (policy.executable, "exec", str(unapproved)), paths["scratch"])
    assert completed.returncode != 0
    assert b"Operation not permitted" in completed.stderr
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(.25)
    try:
        completed = subprocess.run(
            [policy.executable, "network", str(listener.getsockname()[1])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=10, check=False,
        )
        assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
        connection, _address = listener.accept()
        connection.close()
    finally:
        listener.close()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(.25)
    try:
        completed = _run(
            policy,
            (policy.executable, "network", str(listener.getsockname()[1])),
            paths["scratch"],
        )
        assert completed.returncode != 0
        assert b"Operation not permitted" in completed.stderr
        with pytest.raises(TimeoutError):
            listener.accept()
    finally:
        listener.close()
