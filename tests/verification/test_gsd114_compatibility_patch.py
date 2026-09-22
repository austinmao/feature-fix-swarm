"""Focused qualification for the pinned GSD 1.14 FFS compatibility patch."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "lib"))

PIN = "f8542fef67c1f978ffa7" + "0912cb6f2aaab76464c6"
PATCH = Path(__file__).parents[2] / "patches" / "gsd-1.14-ffs-supervised-dispatch.patch"
INSTALLED = Path(__file__).parents[2] / "node_modules" / "@opengsd" / "gsd-core"
EXPECTED = {
    "install.js": "sha256:3669a79b6f80f2f373a78a2e0cecf58c5f8cac736f17eb08d49ee368672b197c",
    "gsd-tools.cjs": "sha256:f0b3dde4d9bca6c81b53459c51547ea5ea5daeec965431481815cd3b94ba3328",
    "execute-phase.md": "sha256:82ad1b4049f3660c8a979bcd41b7fa9b65bc44ad16a2e2de129ffc3cf31f2a97",
    "executor-isolation-dispatch.md": "sha256:435ea7ecdddc48796d56f4252b59d099423feb9f83aaa4b697743b3f7ad25bea",
    "ffs-supervised-dispatch.cjs": "sha256:f3356ecdc8f9f24a7f036f4c0143177db53cd8be87feca80111e6479e11fa619",
}
BASELINE = {
    "install.js": "sha256:0acbd01933783537f934b33b6cc9132ff8e11ae37f0fa88ff63bc402aa0ae861",
    "gsd-tools.cjs": "sha256:ec066117822d0270bafa6ed3f863b4aebee8bfda243efcd828bf8a1d85732a92",
    "execute-phase.md": "sha256:ba69804f311a5efb7ebd87b824917a82fbacbb3389cf56de06346d64d31beb4a",
    "executor-isolation-dispatch.md": "sha256:7c791b8311ed047abcb747c2e8e7a2362c199b66daa3d9fb89bf0feb1d58ab32",
}


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _git(path: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=path, text=True, capture_output=True, check=False,
    )
    if check and result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


@pytest.fixture(scope="module")
def patched_gsd(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Copy the exact source checkout, apply the patch, and verify output hashes."""
    source = os.environ.get("GSD114_SOURCE_ROOT")
    source_path = Path(source) if source else Path("/tmp/gsd-core-ffs-source-r2")
    if not (source_path / ".git").exists():
        pytest.skip("set GSD114_SOURCE_ROOT to an exact pinned gsd-core checkout")
    assert _git(source_path, "rev-parse", "HEAD") == PIN
    target = tmp_path_factory.mktemp("gsd114-source") / "gsd-core"
    shutil.copytree(source_path, target, symlinks=True)
    subprocess.run(["git", "apply", "--check", str(PATCH)], cwd=target, check=True)
    subprocess.run(["git", "apply", str(PATCH)], cwd=target, check=True)
    assert _sha256(target / "bin/install.js") == EXPECTED["install.js"]
    assert _sha256(target / "gsd-core/bin/gsd-tools.cjs") == EXPECTED["gsd-tools.cjs"]
    assert _sha256(target / "gsd-core/workflows/execute-phase.md") == EXPECTED["execute-phase.md"]
    assert _sha256(target / "gsd-core/workflows/execute-phase/steps/executor-isolation-dispatch.md") == EXPECTED["executor-isolation-dispatch.md"]
    assert _sha256(target / "gsd-core/bin/ffs-supervised-dispatch.cjs") == EXPECTED["ffs-supervised-dispatch.cjs"]
    return target


class _ChannelStore:
    """Minimal durable seam needed to exercise the real worker-channel server."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    @contextmanager
    def fenced_operation(self, _token):
        yield

    def transaction(self):
        return nullcontext(self)

    def execute(self, _query, _parameters=()):
        return self

    def fetchone(self):
        return None

    def record_event_once(self, _token, _activity, _key, payload):
        event = {"id": len(self.events) + 1, "payload": payload}
        self.events.append(event)
        return event


def _fake_supervisor(tmp_path: Path) -> Path:
    script = tmp_path / "supervisor.py"
    script.write_text(
        "import json, sys\n"
        "manifest = json.load(sys.stdin)\n"
        "results = []\n"
        "for p in manifest['plans']:\n"
        "    result = dict(plan_id=p['id'], status='complete', summary='scoped result', changed_files=p['files_modified'])\n"
        "    if manifest['commit_mode'] == 'fixture-commits': result['commit'] = 'a' * 40\n"
        "    else: result['patch'] = 'diff --git a/src/shared.txt b/src/shared.txt'\n"
        "    results.append(result)\n"
        "print(json.dumps({\n"
        "  'schema': 'ffs.gsd-supervised-dispatch/v1',\n"
        "  'mode': 'ffs-supervised-process',\n"
        "  'wave': manifest['wave'],\n"
        "  'initial_head': manifest['initial_head'],\n"
        "  'apply_between_waves': True,\n"
        "  'results': results\n"
        "}))\n",
    )
    return script


def _manifest(base: str, worktrees: list[Path], *, commit_mode: str = "patches") -> dict:
    plans = []
    for index, worktree in enumerate(worktrees, start=1):
        prompt = f"fresh executor prompt {index}"
        plans.append({
            "id": f"01-0{index}",
            "initial_head": base,
            "prompt": prompt,
            "prompt_fresh": True,
            "prompt_nonce": f"nonce-{index}",
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "files_modified": ["src/shared.txt"],
            "files_deleted": [],
            "depends_on": [],
        })
    root = str(worktrees[0].parent.parent)
    admission = {
        "schema": "ffs.supervisor-admission/v1", "available": True,
        "repository_id": "repo", "run_id": "run", "activity_id": "activity",
        "generation": 1, "workspace": root, "runtime_identity": "runtime",
    }
    return {
        "schema": "ffs.gsd-supervised-dispatch/v1",
        "mode": "ffs-supervised-process",
        "phase": "1",
        "wave": 1,
        "initial_head": base,
        "commit_mode": commit_mode,
        "apply_between_waves": True,
        "orchestrator_root": root,
        "admission": admission,
        "plans": plans,
    }


def test_patch_applies_to_exact_pinned_source_and_installed_baseline_hashes(patched_gsd: Path) -> None:
    assert _sha256(INSTALLED / "bin/install.js") == BASELINE["install.js"]
    assert _sha256(INSTALLED / "gsd-core/bin/gsd-tools.cjs") == BASELINE["gsd-tools.cjs"]
    assert _sha256(INSTALLED / "gsd-core/workflows/execute-phase.md") == BASELINE["execute-phase.md"]
    assert _sha256(INSTALLED / "gsd-core/workflows/execute-phase/steps/executor-isolation-dispatch.md") == BASELINE["executor-isolation-dispatch.md"]
    assert _git(patched_gsd, "rev-parse", "HEAD") == PIN


def test_installed_compatibility_contract_derives_private_wave_paths_without_tmpdir(
    patched_gsd: Path, tmp_path: Path,
) -> None:
    """Exercise the installed workflow's private wave-path setup literally.

    Codex deliberately excludes both /tmp and TMPDIR from its workspace-write
    sandbox.  The workflow must therefore derive its manifest/result pair from
    the supervisor-admitted workspace, including when that workspace has spaces.
    """
    workflow = (patched_gsd / "gsd-core/workflows/execute-phase/steps/executor-isolation-dispatch.md").read_text()
    section = workflow.split("## FFS-supervised-process compatibility mode\n", 1)[1]
    match = re.search(r"```bash\n(?P<setup>[^`]*FFS_ADMISSION_BINDING[^`]*)\n   ```", section)
    assert match is not None
    setup = match.group("setup")
    assert "mktemp" not in setup
    assert "TMPDIR" not in setup

    workspace = tmp_path / "admitted workspace with spaces"
    (workspace / ".planning").mkdir(parents=True)
    _git(workspace, "init", "-b", "main")
    admission = {
        "schema": "ffs.supervisor-admission/v1", "available": True,
        "repository_id": "repo", "run_id": "run", "activity_id": "activity-1",
        "generation": 1, "workspace": str(workspace), "runtime_identity": "runtime",
    }
    script = setup + '\nprintf "%s\\n%s\\n" "$FFS_WAVE_MANIFEST" "$FFS_WAVE_RESULT"\n'
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script], cwd=workspace,
        env=os.environ | {
            "FFS_DISPATCH_JSON": json.dumps({"mode": "ffs-supervised-process", "admission": admission}),
            "TMPDIR": "/not-usable-by-codex",
            "FFS_WAVE_NUMBER": "2",
        }, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    manifest, output = result.stdout.splitlines()
    private = workspace / ".planning/.ffs-supervised/waves/activity-1"
    assert manifest == str(private / "wave-2.manifest.json")
    assert output == str(private / "wave-2.result.json")
    assert all(path.stat().st_mode & 0o777 == 0o700 for path in (
        workspace / ".planning/.ffs-supervised", workspace / ".planning/.ffs-supervised/waves", private,
    ))
    assert not Path(manifest).exists()
    assert not Path(output).exists()

    Path(manifest).write_text("retained manifest")
    Path(output).write_text("retained result")
    Path(str(output) + ".receipt.json").write_text("retained receipt")
    retained = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script], cwd=workspace,
        env=os.environ | {"FFS_DISPATCH_JSON": json.dumps({"mode": "ffs-supervised-process", "admission": admission}),
                          "FFS_WAVE_NUMBER": "2"},
        text=True, capture_output=True, check=False,
    )
    assert retained.returncode == 0
    assert retained.stdout.splitlines() == [manifest, output]

    assert Path(manifest).read_text() == "retained manifest"
    assert Path(output).read_text() == "retained result"
    next_wave = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script], cwd=workspace,
        env=os.environ | {"FFS_DISPATCH_JSON": json.dumps({"mode": "ffs-supervised-process", "admission": admission}),
                          "FFS_WAVE_NUMBER": "3"},
        text=True, capture_output=True, check=False,
    )
    assert next_wave.returncode == 0, next_wave.stderr
    assert next_wave.stdout.splitlines() == [str(private / "wave-3.manifest.json"), str(private / "wave-3.result.json")]


def test_installed_workflow_mechanically_refuses_partial_retained_wave_evidence(
    patched_gsd: Path,
) -> None:
    workflow = (patched_gsd / "gsd-core/workflows/execute-phase/steps/executor-isolation-dispatch.md").read_text()
    section = workflow.split("## FFS-supervised-process compatibility mode\n", 1)[1]
    assert 'FFS_WAVE_RECEIPT="$FFS_WAVE_RESULT.receipt.json"' in section
    assert "FFS_WAVE_RETAINED=complete" in section
    assert "partial retained wave evidence; refusing without relaunch" in section
    assert "fs.constants.O_EXCL" in section
    assert "Do not regenerate prompts" in section
    assert "write a replacement manifest" in section


def test_fresh_patched_install_manifests_every_changeset_file(
    tmp_path: Path,
) -> None:
    package = tmp_path / "gsd-core-package"
    shutil.copytree(INSTALLED, package, symlinks=True)
    subprocess.run(["git", "apply", "--check", str(PATCH)], cwd=package, check=True)
    subprocess.run(["git", "apply", str(PATCH)], cwd=package, check=True)
    install_root = tmp_path / "fresh-install"
    install_root.mkdir()
    receipt_path = tmp_path / "install-receipt.json"
    script = """
process.env.GSD_TEST_MODE = '1';
const fs = require('node:fs');
const installer = require(process.argv[1]);
const result = installer.install(false, 'claude');
fs.writeFileSync(process.argv[2], JSON.stringify({
  config_dir: result.configDir,
  changeset_files: installer.GSD_CHANGESET_FILES,
}));
"""
    environment = os.environ | {
        "GSD_TEST_MODE": "1",
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
    }
    result = subprocess.run(
        ["node", "-e", script, str(package / "bin/install.js"), str(receipt_path)],
        cwd=install_root, env=environment, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(receipt_path.read_text())
    config_dir = Path(receipt["config_dir"])
    manifest = json.loads((config_dir / "gsd-file-manifest.json").read_text())
    manifest_files = set(manifest["files"])
    changeset_dir = config_dir / "scripts" / "changeset"
    installed = {
        "scripts/changeset/" + path.name
        for path in changeset_dir.iterdir()
        if path.is_file()
    }
    expected = {"scripts/changeset/" + name for name in receipt["changeset_files"]}
    assert installed == expected
    assert installed <= manifest_files
    assert "scripts/changeset/README.md" in manifest_files


def test_dispatch_adapter_sends_canonical_json_to_real_python_bridge(
    patched_gsd: Path, tmp_path: Path,
) -> None:
    from process_identity import ProcessIdentity
    from run_state.worker_channel import WorkerBinding, WorkerChannelServer

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    document = _manifest("a" * 40, [workspace / "children" / "one"])
    manifest = workspace / "wave.json"
    output = workspace / "wave-result.json"
    admission = workspace / "admission.json"
    # Both adapters reject noncanonical bytes before any child is admitted.
    manifest.write_text(_canonical_json(document))
    admission.write_text(json.dumps(document["admission"]))

    identity = ProcessIdentity.current()
    binding = WorkerBinding(
        "repo", "run", "activity", "intent", 1, identity, str(workspace), "runtime",
        "c" * 64, "d" * 64, ("worker",), (str(workspace),), identity,
    )
    store = _ChannelStore()
    with tempfile.TemporaryDirectory(prefix="ffs-gsd114-", dir="/tmp") as socket_dir:
        server = WorkerChannelServer(store, object(), Path(socket_dir).resolve() / "worker.sock")
        server._verify_binding = lambda _tx, _binding: None
        server._primary_bindings[binding.intent_id] = binding

        def consume(_event_id: int) -> dict:
            return {
                "schema": document["schema"], "mode": document["mode"],
                "wave": document["wave"], "initial_head": document["initial_head"],
                "apply_between_waves": True,
                "results": [{
                    "plan_id": document["plans"][0]["id"], "status": "complete",
                    "summary": "no changes", "changed_files": [], "patch": "",
                }],
            }

        server.attach_wave_consumer(consume)
        server.start()
        try:
            python_path = str(ROOT / "lib")
            if os.environ.get("PYTHONPATH"):
                python_path += os.pathsep + os.environ["PYTHONPATH"]
            env = os.environ | {
                "PYTHONPATH": python_path,
                "FFS_SUPERVISED_DISPATCH_COMMAND_JSON": json.dumps([
                    sys.executable, "-m", "run_state.gsd_wave_bridge",
                ]),
                "FFS_SUPERVISED_ADMISSION_FILE": str(admission),
                "FFS_WORKER_ENDPOINT": str(server.endpoint),
                "FFS_WORKER_SCOPE": json.dumps(binding.scope(), sort_keys=True),
            }
            result = subprocess.run(
                ["node", str(patched_gsd / "gsd-core/bin/ffs-supervised-dispatch.cjs"),
                 "--manifest", str(manifest), "--output", str(output)],
                env=env, text=True, capture_output=True, check=False,
            )
        finally:
            server.close()

    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text()) == consume(1)
    retained = list((workspace / ".planning/.ffs-wave-requests").glob("*.json"))
    assert len(retained) == 1
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    assert retained[0].read_bytes() == canonical
    assert store.events[0]["payload"]["manifest"] == document


def test_dispatch_adapter_refuses_without_admission(patched_gsd: Path, tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    output = tmp_path / "result.json"
    manifest.write_text(_canonical_json(_manifest("a" * 40, [tmp_path / "one", tmp_path / "two"])))
    env = os.environ.copy()
    env.pop("FFS_SUPERVISED_DISPATCH_COMMAND_JSON", None)
    result = subprocess.run(
        ["node", str(patched_gsd / "gsd-core/bin/ffs-supervised-dispatch.cjs"), "--manifest", str(manifest), "--output", str(output)],
        env=env, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 78
    assert not output.exists()


@pytest.mark.parametrize("mutate, expected", [
    (lambda value: value, "canonical JSON"),
    (lambda value: {**value, "unexpected": True}, "schema"),
])
def test_dispatch_adapter_matches_python_manifest_strictness(
    patched_gsd: Path, tmp_path: Path, mutate, expected: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    document = mutate(_manifest("a" * 40, [workspace / "child"]))
    manifest, output = tmp_path / "manifest.json", tmp_path / "result.json"
    # The first row is deliberately noncanonical; the second is canonical
    # bytes with an unknown control field. Both must fail before spawning.
    encoded = json.dumps(document, indent=2) if expected == "canonical JSON" else _canonical_json(document)
    manifest.write_text(encoded)
    admission = tmp_path / "admission.json"
    admission.write_text(_canonical_json(document["admission"]))
    result = subprocess.run(
        ["node", str(patched_gsd / "gsd-core/bin/ffs-supervised-dispatch.cjs"),
         "--manifest", str(manifest), "--output", str(output)],
        env=os.environ | {
            "FFS_SUPERVISED_DISPATCH_COMMAND_JSON": json.dumps([sys.executable, str(_fake_supervisor(tmp_path))]),
            "FFS_SUPERVISED_ADMISSION_FILE": str(admission),
        }, text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert expected in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("mutate, accepted", [
    (lambda value: {**value, "plans": [{key: item for key, item in value["plans"][0].items() if key != "depends_on"}]}, True),
    (lambda value: {**value, "plans": [{**value["plans"][0], "files_modified": ["src\\bad.py"]}]}, False),
    (lambda value: {**value, "plans": [{**value["plans"][0], "files_modified": ["src/\0bad.py"]}]}, False),
])
def test_shared_node_python_manifest_corpus(
    patched_gsd: Path, tmp_path: Path, mutate, accepted: bool,
) -> None:
    from run_state.worker_channel import WorkerChannelRefused, parse_gsd_wave_manifest

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    document = mutate(_manifest("a" * 40, [workspace / "child"]))
    raw = _canonical_json(document).encode()
    try:
        parse_gsd_wave_manifest(raw)
        python_accepted = True
    except WorkerChannelRefused:
        python_accepted = False

    manifest, output = tmp_path / "manifest.json", tmp_path / "result.json"
    manifest.write_bytes(raw)
    admission = tmp_path / "admission.json"
    admission.write_text(_canonical_json(document["admission"]))
    node = subprocess.run(
        ["node", str(patched_gsd / "gsd-core/bin/ffs-supervised-dispatch.cjs"),
         "--manifest", str(manifest), "--output", str(output)],
        env=os.environ | {
            "FFS_SUPERVISED_DISPATCH_COMMAND_JSON": json.dumps([sys.executable, str(_fake_supervisor(tmp_path))]),
            "FFS_SUPERVISED_ADMISSION_FILE": str(admission),
        }, text=True, capture_output=True, check=False,
    )
    assert python_accepted is accepted
    assert (node.returncode == 0) is accepted, node.stderr


def test_dispatch_adapter_reuses_valid_result_receipt_without_relaunch(
    patched_gsd: Path, tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest, output, calls = tmp_path / "manifest.json", tmp_path / "result.json", tmp_path / "calls"
    document = _manifest("a" * 40, [workspace / "child"])
    manifest.write_text(_canonical_json(document))
    supervisor = tmp_path / "receipt-supervisor.py"
    supervisor.write_text(
        "import json, pathlib, sys\n"
        "m=json.load(sys.stdin); p=pathlib.Path(sys.argv[1]); p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
        "plan=m['plans'][0]; print(json.dumps({'schema':m['schema'],'mode':m['mode'],'wave':m['wave'],"
        "'initial_head':m['initial_head'],'apply_between_waves':True,'results':[{'plan_id':plan['id'],"
        "'status':'complete','summary':'no changes','changed_files':[],'patch':''}]}))\n",
    )
    env = os.environ | {
        "FFS_SUPERVISED_DISPATCH_COMMAND_JSON": json.dumps([sys.executable, str(supervisor), str(calls)]),
        "FFS_SUPERVISED_ADMISSION_FILE": str(tmp_path / "admission.json"),
    }
    Path(env["FFS_SUPERVISED_ADMISSION_FILE"]).write_text(_canonical_json(document["admission"]))
    command = ["node", str(patched_gsd / "gsd-core/bin/ffs-supervised-dispatch.cjs"),
               "--manifest", str(manifest), "--output", str(output)]
    assert subprocess.run(command, env=env, text=True, capture_output=True).returncode == 0
    assert subprocess.run(command, env=env, text=True, capture_output=True).returncode == 0
    assert calls.read_text() == "x"
    receipt = json.loads(Path(str(output) + ".receipt.json").read_text())
    assert receipt["manifest_sha256"] == hashlib.sha256(_canonical_json(document).encode()).hexdigest()
    assert receipt["commit_mode"] == "patches"
    document["plans"][0]["prompt"] += " changed"
    document["plans"][0]["prompt_sha256"] = hashlib.sha256(document["plans"][0]["prompt"].encode()).hexdigest()
    manifest.write_text(_canonical_json(document))
    refused = subprocess.run(command, env=env, text=True, capture_output=True)
    assert refused.returncode != 0 and "completion receipt input mismatch" in refused.stderr
    assert calls.read_text() == "x"


def test_dispatch_adapter_rejects_patch_path_hidden_by_changed_files(
    patched_gsd: Path, tmp_path: Path,
) -> None:
    supervisor = tmp_path / "dishonest-supervisor.py"
    supervisor.write_text(
        "import json, sys\n"
        "m=json.load(sys.stdin); p=m['plans'][0]\n"
        "print(json.dumps({'schema':m['schema'],'mode':m['mode'],'wave':m['wave'],"
        "'initial_head':m['initial_head'],'apply_between_waves':True,'results':["
        "{'plan_id':p['id'],'status':'complete','summary':'dishonest',"
        "'changed_files':['src/shared.txt'],"
        "'patch':'diff --git a/src/hidden.txt b/src/hidden.txt'}]}))\n"
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    manifest = tmp_path / "manifest.json"
    output = tmp_path / "result.json"
    manifest.write_text(_canonical_json(_manifest("a" * 40, [worktree])))
    admission = tmp_path / "admission.json"
    admission.write_text(json.dumps(json.loads(manifest.read_text())["admission"]))
    env = os.environ | {
        "FFS_SUPERVISED_DISPATCH_COMMAND_JSON": json.dumps([sys.executable, str(supervisor)]),
        "FFS_SUPERVISED_ADMISSION_FILE": str(admission),
    }
    result = subprocess.run(
        ["node", str(patched_gsd / "gsd-core/bin/ffs-supervised-dispatch.cjs"),
         "--manifest", str(manifest), "--output", str(output)],
        env=env, text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert "out-of-scope" in result.stderr
    assert not output.exists()


def test_dispatch_adapter_accepts_honest_no_change_and_failed_results(
    patched_gsd: Path, tmp_path: Path,
) -> None:
    supervisor = tmp_path / "empty-results-supervisor.py"
    supervisor.write_text(
        "import json, sys\n"
        "m=json.load(sys.stdin); results=[]\n"
        "for i,p in enumerate(m['plans']):\n"
        " results.append({'plan_id':p['id'],'status':'complete' if i == 0 else 'failed',"
        "'summary':'no changes' if i == 0 else 'executor failed',"
        "'changed_files':[],'patch':''})\n"
        "print(json.dumps({'schema':m['schema'],'mode':m['mode'],'wave':m['wave'],"
        "'initial_head':m['initial_head'],'apply_between_waves':True,'results':results}))\n"
    )
    worktrees = [tmp_path / "one", tmp_path / "two"]
    for worktree in worktrees:
        worktree.mkdir()
    manifest = tmp_path / "manifest.json"
    output = tmp_path / "result.json"
    manifest.write_text(_canonical_json(_manifest("a" * 40, worktrees)))
    admission = tmp_path / "admission.json"
    admission.write_text(json.dumps(json.loads(manifest.read_text())["admission"]))
    env = os.environ | {
        "FFS_SUPERVISED_DISPATCH_COMMAND_JSON": json.dumps([sys.executable, str(supervisor)]),
        "FFS_SUPERVISED_ADMISSION_FILE": str(admission),
    }
    result = subprocess.run(
        ["node", str(patched_gsd / "gsd-core/bin/ffs-supervised-dispatch.cjs"),
         "--manifest", str(manifest), "--output", str(output)],
        env=env, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    reply = json.loads(output.read_text())
    assert [item["status"] for item in reply["results"]] == ["complete", "failed"]
    assert all(item["patch"] == "" and item["changed_files"] == [] for item in reply["results"])


def test_dispatch_adapter_preserves_prior_wave_dependencies_and_rejects_internal_edges(
    patched_gsd: Path, tmp_path: Path,
) -> None:
    supervisor = _fake_supervisor(tmp_path)
    worktrees = [tmp_path / "one", tmp_path / "two"]
    for worktree in worktrees:
        worktree.mkdir()
    document = _manifest("a" * 40, worktrees)
    document["plans"][0]["depends_on"] = ["00-prior"]
    manifest = tmp_path / "manifest.json"
    output = tmp_path / "result.json"
    admission = tmp_path / "admission.json"
    admission.write_text(json.dumps(document["admission"]))
    env = os.environ | {
        "FFS_SUPERVISED_DISPATCH_COMMAND_JSON": json.dumps([sys.executable, str(supervisor)]),
        "FFS_SUPERVISED_ADMISSION_FILE": str(admission),
    }
    manifest.write_text(_canonical_json(document))
    command = [
        "node", str(patched_gsd / "gsd-core/bin/ffs-supervised-dispatch.cjs"),
        "--manifest", str(manifest), "--output", str(output),
    ]
    accepted = subprocess.run(command, env=env, text=True, capture_output=True, check=False)
    assert accepted.returncode == 0, accepted.stderr

    output.unlink()
    document["plans"][0]["depends_on"] = [document["plans"][1]["id"]]
    manifest.write_text(_canonical_json(document))
    refused = subprocess.run(command, env=env, text=True, capture_output=True, check=False)
    assert refused.returncode != 0
    assert "unresolved internal dependency" in refused.stderr
    assert not output.exists()


def test_disposable_fixture_keeps_ahead_head_and_allows_same_relative_path_overlap(
    patched_gsd: Path, tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "ffs@example.invalid")
    _git(repo, "config", "user.name", "FFS fixture")
    (repo / "src").mkdir()
    (repo / "src/shared.txt").write_text("base\n")
    _git(repo, "add", "src/shared.txt")
    _git(repo, "commit", "-m", "baseline")
    baseline = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", baseline)
    (repo / "ahead.txt").write_text("ahead\n")
    _git(repo, "add", "ahead.txt")
    _git(repo, "commit", "-m", "ahead default branch")
    expected_head = _git(repo, "rev-parse", "HEAD")
    one, two = tmp_path / "one", tmp_path / "two"
    _git(repo, "worktree", "add", "-b", "ffs-one", str(one), expected_head)
    _git(repo, "worktree", "add", "-b", "ffs-two", str(two), expected_head)
    assert _git(one, "rev-parse", "HEAD") == expected_head
    assert _git(two, "rev-parse", "HEAD") == expected_head
    (one / "src/shared.txt").write_text("one\n")
    (two / "src/shared.txt").write_text("two\n")
    assert (one / "src/shared.txt").read_text() != (two / "src/shared.txt").read_text()

    fake = _fake_supervisor(tmp_path)
    admission = tmp_path / "admission.json"
    manifest = tmp_path / "wave.json"
    output = tmp_path / "wave-result.json"
    manifest.write_text(_canonical_json(_manifest(expected_head, [one, two])))
    admission.write_text(json.dumps(json.loads(manifest.read_text())["admission"]))
    env = os.environ | {
        "FFS_SUPERVISED_DISPATCH_COMMAND_JSON": json.dumps([sys.executable, str(fake)]),
        "FFS_SUPERVISED_ADMISSION_FILE": str(admission),
    }
    result = subprocess.run(
        ["node", str(patched_gsd / "gsd-core/bin/ffs-supervised-dispatch.cjs"), "--manifest", str(manifest), "--output", str(output)],
        env=env, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    reply = json.loads(output.read_text())
    assert [item["plan_id"] for item in reply["results"]] == ["01-01", "01-02"]
    assert _git(repo, "rev-parse", "HEAD") == expected_head

    fixture_manifest = tmp_path / "fixture-wave.json"
    fixture_output = tmp_path / "fixture-wave-result.json"
    fixture_manifest.write_text(_canonical_json(_manifest(expected_head, [one, two], commit_mode="fixture-commits")))
    fixture_result = subprocess.run(
        ["node", str(patched_gsd / "gsd-core/bin/ffs-supervised-dispatch.cjs"), "--manifest", str(fixture_manifest), "--output", str(fixture_output)],
        env=env, text=True, capture_output=True, check=False,
    )
    assert fixture_result.returncode == 0, fixture_result.stderr
    assert all("commit" in item for item in json.loads(fixture_output.read_text())["results"])
