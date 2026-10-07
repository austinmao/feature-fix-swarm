"""spec-014 E8 prerequisite 5: the admitted Codex CLI version is bound to the launcher digest.

``admit_cli`` reads the version from the executable before qualification, and the qualified runtime
later pins the launcher chain it ran.  Nothing tied the two, so bytes swapped between admission and
qualification could run under the admitted version's provenance (F53 review r1, MEDIUM).  Admission
now records the chain it inspected (unchanged across its own probes), and a managed session refuses
when the qualified chain differs from it.

Fixture-level proof only: not native host qualification and not E8.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import host_capabilities
from host_capabilities import REQUIRED_EXEC_FLAGS, REQUIRED_HOOK_FLAG, CapabilityError, _binary_chain
from run_state.managed import prepare_managed_run
from run_state.supervisor import SupervisorRefused, run_managed_command
from test_managed_codex_dispatch import _qualified_host, _setup


def _cli(tmp_path: Path, *, on_version: str = "") -> Path:
    binary = tmp_path / "admitted-codex"
    help_text = " ".join((*REQUIRED_EXEC_FLAGS, REQUIRED_HOOK_FLAG))
    binary.write_text(
        "#!/bin/sh\n"
        f"if [ \"$1\" = --version ]; then {on_version}echo 'codex-cli 0.154.0'; fi\n"
        f"if [ \"$1\" = exec ]; then echo '{help_text}'; fi\n")
    binary.chmod(0o755)
    return binary


def test_admission_records_the_launcher_chain_it_inspected(tmp_path):
    binary = _cli(tmp_path)
    admitted = host_capabilities.admit_cli(str(binary))
    assert admitted["version"] == "0.154.0"
    assert admitted["binary"] == _binary_chain(binary)


def test_a_launcher_that_changes_while_it_is_admitted_refuses(tmp_path):
    binary = _cli(tmp_path, on_version=f"printf '# changed\\n' >> '{tmp_path / 'admitted-codex'}'; ")
    with pytest.raises(CapabilityError, match="changed while"):
        host_capabilities.admit_cli(str(binary))


def test_a_js_launcher_is_admitted_under_its_pinned_node_not_the_ambient_one(tmp_path, monkeypatch):
    from test_native_review_runtime import _js_launcher, _sha, _write
    help_text = " ".join((*REQUIRED_EXEC_FLAGS, REQUIRED_HOOK_FLAG))

    def node_body(version: str) -> bytes:
        return ("#!/bin/sh\n"
                f"if [ \"$2\" = --version ]; then echo 'codex-cli {version}'\n"
                f"elif [ \"$2\" = exec ]; then echo '{help_text}'\n"
                "else echo 'darwin arm64'; fi\n").encode()

    launcher, node = _js_launcher(tmp_path, node_body=node_body("0.154.0"))
    decoy = tmp_path / "ambient" / "bin"
    decoy.mkdir(parents=True)
    _write(decoy / "node", node_body("0.159.0"), 0o755)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))
    monkeypatch.setenv("PATH", f"{decoy}:/usr/bin:/bin")
    admitted = host_capabilities.admit_cli(str(launcher))
    assert admitted["version"] == "0.154.0"
    assert admitted["binary"]["node_sha256"] == _sha(node)


def test_admission_probes_ignore_ambient_node_startup_options(tmp_path, monkeypatch):
    from test_native_review_runtime import _js_launcher
    help_text = " ".join((*REQUIRED_EXEC_FLAGS, REQUIRED_HOOK_FLAG))
    body = ("#!/bin/sh\n"
            "if [ -n \"$NODE_OPTIONS\" ]; then version=0.159.0; else version=0.154.0; fi\n"
            "if [ \"$2\" = --version ]; then echo \"codex-cli $version\"\n"
            f"elif [ \"$2\" = exec ]; then echo '{help_text}'\n"
            "else echo 'darwin arm64'; fi\n").encode()
    launcher, node = _js_launcher(tmp_path, node_body=body)
    monkeypatch.setenv("CODEX_NODE_BINARY", str(node))
    monkeypatch.setenv("NODE_OPTIONS", "--require=/nonexistent/version-spoof.js")
    assert host_capabilities.admit_cli(str(launcher))["version"] == "0.154.0"


def _run(tmp_path, monkeypatch, admit):
    primary, authority, _repository_id, env = _setup(tmp_path)
    request = _qualified_host(tmp_path, monkeypatch)
    monkeypatch.setattr(host_capabilities, "admit_cli", lambda binary: admit(Path(binary)))
    monkeypatch.chdir(primary)
    outcome = {}

    def execute(store, token, context):
        from run_state.cli import _load_upstream_runtime
        upstream_runtime, _digest = _load_upstream_runtime(SimpleNamespace(
            upstream_runtime_manifest=env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
            upstream_runtime_sha256=env["FFS_UPSTREAM_RUNTIME_SHA256"],
        ))
        try:
            outcome["rc"] = run_managed_command(
                store, token, context, command=("/gsd-plan-phase", "1"),
                request_key="cli-version", dispatch_limit=3, token_limit=1000,
                host_request=request, upstream_runtime=upstream_runtime,
            )
        except SupervisorRefused as error:
            outcome["code"] = error.code
        return 0

    assert prepare_managed_run(
        objective="cli version binding", state_root=authority,
        selection_manifest=env["FFS_SELECTION_MANIFEST"],
        upstream_runtime_manifest=env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
        upstream_runtime_sha256=env["FFS_UPSTREAM_RUNTIME_SHA256"],
        request_key="cli-version", command=("/gsd-plan-phase", "1"),
        dispatch_limit=3, token_limit=1000, on_ready=execute,
        run_id="cli-version", activity="plan", scope="1",
        host_request=request,
    ) == 0
    return outcome


def test_a_launcher_swapped_between_admission_and_qualification_refuses(tmp_path, monkeypatch):
    def admit_then_swap(binary: Path):
        chain = _binary_chain(binary)
        binary.write_bytes(binary.read_bytes() + b"\n# swapped after admission\n")
        return {"version": "0.154.0", "binary": chain}

    assert _run(tmp_path, monkeypatch, admit_then_swap) == {"code": "HOST_CLI_VERSION_UNBOUND"}


def test_an_admission_without_a_recorded_chain_refuses(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, lambda _binary: {"version": "0.154.0"}) == {
        "code": "HOST_CLI_VERSION_UNBOUND"}


def test_an_admission_bound_to_the_qualified_chain_runs(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, lambda binary: {"version": "0.154.0", "binary": _binary_chain(binary)}) == {
        "rc": 0}
