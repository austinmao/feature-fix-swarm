"""Tests for the FFS-supervised wave helper modes (spec-014 E8 prerequisite 1c).

The outer GSD orchestrator used to hand-copy a bash+node template from the
patched workflow doc to bind the FFS admission and build the wave directory,
carrying shell variables from one tool call into the next. A live run (M5b)
hand-wrote that glue, read an unexported variable, aborted silently under
`set -e`, and FFS then refused WAVE_EXECUTION_UNPROVEN at EXECUTE.

The shipped adapter now owns all of it through three modes that take only the
wave number: `--prepare-wave`, `--write-wave-manifest`, `--dispatch-wave`.
These tests pin the modes themselves and the rewritten doc section: every
fenced bash block must be complete on its own in a fresh `bash -euo pipefail`.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap
import time

import pytest

ROOT = Path(__file__).parents[1]

PATCH = ROOT / "patches" / "gsd-1.14-ffs-supervised-dispatch.patch"
INSTALLED = ROOT / "node_modules" / "@opengsd" / "gsd-core"
ADAPTER_RELATIVE = "gsd-core/bin/ffs-supervised-dispatch.cjs"
DOC_RELATIVE = "gsd-core/workflows/execute-phase/steps/executor-isolation-dispatch.md"
SECTION_HEADING = "## FFS-supervised-process compatibility mode\n"
ACTIVITY = "activity-1"
MODES = ("--prepare-wave", "--write-wave-manifest", "--dispatch-wave")

# Only these names may be referenced by a documented block without being
# assigned inside that same block: the host environment plus the two inputs.
DOCUMENTED_INPUTS = {"FFS_WAVE_NUMBER", "FFS_WAVE_MANIFEST_JSON", "CODEX_HOME", "CLAUDE_CONFIG_DIR"}

FAKE_SUPERVISOR = """\
import json, os, sys, time
with open({counter!r}, "a") as handle:
    handle.write("launch\\n")
manifest = json.load(sys.stdin)
time.sleep(float(os.environ.get("FAKE_SUPERVISOR_SLEEP", "0")))
if os.environ.get("FAKE_SUPERVISOR_EXIT"):
    sys.exit(int(os.environ["FAKE_SUPERVISOR_EXIT"]))
results = [
    dict(plan_id=p["id"], status="complete", summary="ok", changed_files=[], patch="")
    for p in manifest["plans"]
]
print(json.dumps({{
    "schema": manifest["schema"], "mode": manifest["mode"], "wave": manifest["wave"],
    "initial_head": manifest["initial_head"], "apply_between_waves": True, "results": results,
}}))
"""


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@pytest.fixture(scope="module")
def patched_package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Apply the pinned patch to a clean copy of the installed package."""
    target = tmp_path_factory.mktemp("wave-helper") / "gsd-core"
    shutil.copytree(INSTALLED, target, symlinks=True)
    subprocess.run(["git", "apply", "--check", str(PATCH)], cwd=target, check=True)
    subprocess.run(["git", "apply", str(PATCH)], cwd=target, check=True)
    return target


class Bench:
    """A temp git workspace, an admission file, and a fake supervisor command."""

    def __init__(self, package: Path, root: Path) -> None:
        self.package = package
        self.root = root
        workspace = root / "workspace"
        (workspace / ".planning").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=workspace, check=True)
        self.workspace = workspace.resolve()
        self.home = root / "home"
        self.home.mkdir()
        self.counter = root / "supervisor-launches"
        self.counter.write_text("")
        supervisor = root / "supervisor.py"
        supervisor.write_text(FAKE_SUPERVISOR.format(counter=str(self.counter)))
        self.command_json = json.dumps([sys.executable, str(supervisor)])
        self.admission_file = root / "admission.json"
        self.admission: dict = {}
        self.write_admission()

    @property
    def wave_dir(self) -> Path:
        return self.workspace / ".planning" / ".ffs-supervised" / "waves" / ACTIVITY

    def wave_file(self, kind: str, wave: int = 1) -> Path:
        name = {
            "manifest": f"wave-{wave}.manifest.json",
            "result": f"wave-{wave}.result.json",
            "receipt": f"wave-{wave}.result.json.receipt.json",
        }[kind]
        return self.wave_dir / name

    def write_admission(self, **changes: object) -> None:
        self.admission = {
            "activity_id": ACTIVITY, "available": True, "generation": 1, "repository_id": "repo",
            "run_id": "run", "runtime_identity": "runtime", "schema": "ffs.supervisor-admission/v1",
            "workspace": str(self.workspace),
        } | changes
        self.admission_file.write_text(json.dumps(self.admission))

    def launches(self) -> int:
        return len(self.counter.read_text().splitlines())

    def env(self, **overrides: str | None) -> dict[str, str]:
        """The documented environment and nothing else; None removes a name."""
        environment: dict[str, str | None] = {
            "PATH": os.environ["PATH"], "HOME": str(self.home), "CODEX_HOME": str(self.package),
            "FFS_SUPERVISED_ADMISSION_FILE": str(self.admission_file),
            "FFS_SUPERVISED_DISPATCH_COMMAND_JSON": self.command_json,
            "FFS_SUPERVISED_COMMIT_MODE": "patches",
        } | overrides
        return {key: value for key, value in environment.items() if value is not None}

    def adapter(
        self, *args: str, stdin: str | None = None, cwd: Path | None = None, env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["node", str(self.package / ADAPTER_RELATIVE), *args],
            cwd=cwd or self.workspace, env=self.env() if env is None else env,
            input=stdin, text=True, capture_output=True, check=False,
        )

    def seed_wave_dir(self) -> None:
        for directory in (
            self.workspace / ".planning" / ".ffs-supervised",
            self.workspace / ".planning" / ".ffs-supervised" / "waves",
            self.wave_dir,
        ):
            directory.mkdir(mode=0o700, exist_ok=True)
            directory.chmod(0o700)


@pytest.fixture
def bench(patched_package: Path, tmp_path: Path) -> Bench:
    return Bench(patched_package, tmp_path)


def _snapshot(*roots: Path) -> dict[str, str]:
    """Everything below the roots (never following links), minus .git."""
    seen: dict[str, str] = {}
    for root in roots:
        for current, directories, files in os.walk(root):
            directories[:] = [name for name in directories if name != ".git"]
            for name in directories + files:
                path = Path(current) / name
                if path.is_symlink():
                    seen[str(path)] = "link->" + os.readlink(path)
                elif path.is_dir():
                    seen[str(path)] = "dir"
                else:
                    seen[str(path)] = "file:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return seen


def _prepared(result: subprocess.CompletedProcess) -> dict:
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    return json.loads(lines[0])


def _manifest_for(prepared: dict) -> dict:
    """The manifest an orchestrator builds from prepare-wave's JSON, with the
    ordinary insertion-order keys a real executor writes (not sorted)."""
    prompt = "fresh executor prompt"
    head = "a" * 40
    return {
        "schema": "ffs.gsd-supervised-dispatch/v1",
        "mode": prepared["mode"],
        "admission": prepared["admission"],
        "orchestrator_root": prepared["admission"]["workspace"],
        "initial_head": head,
        "wave": prepared["wave"],
        "phase": "1",
        "commit_mode": prepared["commit_mode"],
        "apply_between_waves": prepared["apply_between_waves"],
        "plans": [{
            "id": "01-01", "initial_head": head, "prompt": prompt, "prompt_fresh": True,
            "prompt_nonce": "nonce-1", "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "files_modified": ["src/shared.txt"], "files_deleted": [],
        }],
    }


def _stdin_for(mode: str, bench: Bench) -> str | None:
    if mode != "--write-wave-manifest":
        return None
    return json.dumps(_manifest_for({
        "mode": "ffs-supervised-process", "admission": bench.admission, "wave": 1,
        "commit_mode": "patches", "apply_between_waves": True,
    }))


def _assert_refused(result: subprocess.CompletedProcess, reason: str) -> None:
    assert result.returncode == 78, (result.returncode, result.stderr)
    lines = result.stderr.strip().splitlines()
    assert len(lines) == 1 and reason in lines[0], result.stderr
    assert result.stdout == ""


def test_prepare_wave_creates_private_dirs_and_reports_the_legacy_layout(bench: Bench) -> None:
    result = bench.adapter("--prepare-wave", "1", env=bench.env(FFS_SUPERVISED_COMMIT_MODE=None))
    prepared = _prepared(result)
    assert prepared["wave"] == 1
    assert prepared["admission"] == bench.admission
    assert prepared["mode"] == "ffs-supervised-process"
    assert prepared["commit_mode"] == "patches"
    assert prepared["apply_between_waves"] is True
    assert prepared["retained"] == "none"
    # The readers in run_state (supervisor, wave_candidate, candidate_chain) build
    # exactly this layout; it must not move.
    assert prepared["manifest_path"] == str(bench.wave_file("manifest"))
    assert prepared["result_path"] == str(bench.wave_file("result"))
    assert prepared["receipt_path"] == str(bench.wave_file("receipt"))
    assert prepared["receipt_path"] == prepared["result_path"] + ".receipt.json"
    for directory in (bench.workspace / ".planning" / ".ffs-supervised",
                      bench.workspace / ".planning" / ".ffs-supervised" / "waves", bench.wave_dir):
        assert directory.is_dir() and not directory.is_symlink()
        assert directory.stat().st_mode & 0o777 == 0o700
    assert list(bench.wave_dir.iterdir()) == []


def test_prepare_wave_chmods_a_preexisting_directory_and_reports_the_commit_mode(bench: Bench) -> None:
    bench.seed_wave_dir()
    bench.wave_dir.chmod(0o755)
    prepared = _prepared(bench.adapter(
        "--prepare-wave", "3", env=bench.env(FFS_SUPERVISED_COMMIT_MODE="fixture-commits"),
    ))
    assert prepared["wave"] == 3 and prepared["commit_mode"] == "fixture-commits"
    assert prepared["manifest_path"] == str(bench.wave_file("manifest", 3))
    assert bench.wave_dir.stat().st_mode & 0o777 == 0o700


def _case_env_missing(name: str):
    return lambda bench: ({"env": bench.env(**{name: None})}, name)


def _case_admission(reason: str, **changes: object):
    def setup(bench: Bench):
        bench.write_admission(**changes)
        return {}, reason
    return setup


def _case_admission_not_json(bench: Bench):
    bench.admission_file.write_text("{nope")
    return {}, "unreadable"


def _case_command_not_argv(bench: Bench):
    return {"env": bench.env(FFS_SUPERVISED_DISPATCH_COMMAND_JSON='"echo"')}, "unavailable or malformed"


def _case_workspace_mismatch(bench: Bench):
    other = bench.root / "other"
    other.mkdir()
    bench.write_admission(workspace=str(other))
    return {}, "admitted workspace mismatch"


def _case_not_a_git_workspace(bench: Bench):
    plain = bench.root / "plain"
    plain.mkdir()
    bench.write_admission(workspace=str(plain.resolve()))
    return {"cwd": plain}, "git toplevel"


def _case_wave(value: str):
    return lambda bench: ({"wave": value}, "wave number")


BINDING_REFUSALS = {
    "admission-file-env-missing": _case_env_missing("FFS_SUPERVISED_ADMISSION_FILE"),
    "command-env-missing": _case_env_missing("FFS_SUPERVISED_DISPATCH_COMMAND_JSON"),
    "admission-not-json": _case_admission_not_json,
    "admission-unavailable": _case_admission("unavailable or malformed", available=False),
    "admission-extra-key": _case_admission("unavailable or malformed", extra="x"),
    "admission-relative-workspace": _case_admission("unavailable or malformed", workspace="relative/path"),
    "admission-zero-generation": _case_admission("unavailable or malformed", generation=0),
    "command-not-argv": _case_command_not_argv,
    "workspace-mismatch": _case_workspace_mismatch,
    "not-a-git-workspace": _case_not_a_git_workspace,
    **{f"activity-{index}": _case_admission("activity_id", activity_id=value)
       for index, value in enumerate(["../escape", "a/b", ".hidden", "-lead", "has space", "caf\u00e9"])},
    **{f"wave-{index}": _case_wave(value)
       for index, value in enumerate(["0", "01", "1a", "", "-1", "1.5", " 1", "99999999999999999999"])},
}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("case", sorted(BINDING_REFUSALS))
def test_every_mode_reruns_the_binding_and_refuses_with_a_named_reason(
    bench: Bench, mode: str, case: str,
) -> None:
    extra, reason = BINDING_REFUSALS[case](bench)
    before = _snapshot(bench.root)
    result = bench.adapter(
        mode, extra.get("wave", "1"), stdin=_stdin_for(mode, bench),
        cwd=extra.get("cwd"), env=extra.get("env"),
    )
    _assert_refused(result, reason)
    assert _snapshot(bench.root) == before, "a refusal must create no directory, manifest, result, or receipt"
    assert bench.launches() == 0


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("link", ["ffs-supervised", "waves", "activity"])
def test_a_symlinked_private_directory_is_refused_and_never_followed(
    bench: Bench, mode: str, link: str,
) -> None:
    decoy = bench.root / "decoy"
    decoy.mkdir()
    planning = bench.workspace / ".planning"
    if link == "ffs-supervised":
        (planning / ".ffs-supervised").symlink_to(decoy)
    else:
        (planning / ".ffs-supervised").mkdir(mode=0o700)
        if link == "waves":
            (planning / ".ffs-supervised" / "waves").symlink_to(decoy)
        else:
            (planning / ".ffs-supervised" / "waves").mkdir(mode=0o700)
            bench.wave_dir.symlink_to(decoy)
    before = _snapshot(bench.root)
    result = bench.adapter(mode, "1", stdin=_stdin_for(mode, bench))
    _assert_refused(result, "symlink")
    assert list(decoy.iterdir()) == []
    assert _snapshot(bench.root) == before
    assert bench.launches() == 0


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("kind", ["manifest", "result", "receipt"])
def test_a_symlinked_retained_file_is_refused_and_never_followed(bench: Bench, mode: str, kind: str) -> None:
    bench.seed_wave_dir()
    decoy = bench.root / "decoy.txt"
    decoy.write_text("decoy")
    bench.wave_file(kind).symlink_to(decoy)
    before = _snapshot(bench.root)
    result = bench.adapter(mode, "1", stdin=_stdin_for(mode, bench))
    _assert_refused(result, "symlinked retained wave evidence")
    assert decoy.read_text() == "decoy" and bench.wave_file(kind).is_symlink()
    assert _snapshot(bench.root) == before
    assert bench.launches() == 0


@pytest.mark.parametrize("present", [
    ("manifest",), ("result",), ("receipt",), ("manifest", "result"), ("manifest", "receipt"), ("result", "receipt"),
], ids="+".join)
def test_prepare_wave_refuses_a_partial_retained_set_without_touching_it(
    bench: Bench, present: tuple[str, ...],
) -> None:
    bench.seed_wave_dir()
    for kind in present:
        bench.wave_file(kind).write_text(f"retained {kind}")
    before = _snapshot(bench.root)
    _assert_refused(bench.adapter("--prepare-wave", "1"), "partial retained wave evidence; refusing without relaunch")
    assert _snapshot(bench.root) == before


def test_retained_complete_is_reported_and_write_wave_manifest_refuses_to_replace_it(bench: Bench) -> None:
    bench.seed_wave_dir()
    for kind in ("manifest", "result", "receipt"):
        bench.wave_file(kind).write_text(f"retained {kind}")
    before = _snapshot(bench.root)
    assert _prepared(bench.adapter("--prepare-wave", "1"))["retained"] == "complete"
    stdin = _stdin_for("--write-wave-manifest", bench)
    _assert_refused(bench.adapter("--write-wave-manifest", "1", stdin=stdin), "retained")
    assert _snapshot(bench.root) == before
    assert bench.launches() == 0


def test_write_wave_manifest_refuses_a_manifest_that_is_not_for_this_wave_and_admission(bench: Bench) -> None:
    prepared = _prepared(bench.adapter("--prepare-wave", "1"))
    other_wave = json.dumps(_manifest_for(prepared) | {"wave": 2})
    other_admission = json.dumps(_manifest_for(prepared) | {
        "admission": prepared["admission"] | {"run_id": "another-run"},
    })
    before = _snapshot(bench.root)
    _assert_refused(bench.adapter("--write-wave-manifest", "1", stdin=other_wave), "wave")
    _assert_refused(bench.adapter("--write-wave-manifest", "1", stdin=other_admission), "admission")
    assert _snapshot(bench.root) == before


def test_dispatch_wave_without_a_manifest_refuses_and_launches_nothing(bench: Bench) -> None:
    _assert_refused(bench.adapter("--dispatch-wave", "1"), "no wave manifest")
    assert bench.launches() == 0
    bench.seed_wave_dir()
    bench.wave_file("result").write_text("retained result")
    _assert_refused(bench.adapter("--dispatch-wave", "1"), "no wave manifest")
    assert bench.launches() == 0


def test_a_mode_takes_only_its_wave_number(bench: Bench) -> None:
    result = bench.adapter("--prepare-wave", "1", "--output", str(bench.root / "elsewhere.json"))
    assert result.returncode == 2
    assert "--prepare-wave" in result.stderr
    assert bench.launches() == 0


def test_prepare_write_dispatch_end_to_end_then_a_second_dispatch_never_relaunches(bench: Bench) -> None:
    prepared = _prepared(bench.adapter("--prepare-wave", "1"))
    manifest = _manifest_for(prepared)
    assert json.dumps(manifest) != _canonical_json(manifest), "fixture must be non-canonical"
    written = bench.adapter("--write-wave-manifest", "1", stdin=json.dumps(manifest))
    assert written.returncode == 0, written.stderr
    manifest_path = bench.wave_file("manifest")
    assert manifest_path.read_text() == _canonical_json(manifest)
    assert manifest_path.stat().st_mode & 0o777 == 0o600
    assert sorted(entry.name for entry in bench.wave_dir.iterdir()) == [manifest_path.name]

    dispatched = bench.adapter("--dispatch-wave", "1")
    assert dispatched.returncode == 0, dispatched.stderr
    assert bench.launches() == 1
    result_raw = bench.wave_file("result").read_bytes()
    assert json.loads(result_raw)["results"][0]["status"] == "complete"
    assert json.loads(bench.wave_file("receipt").read_text()) == {
        "schema": "ffs.gsd-no-commit-completion/v1",
        "manifest_sha256": hashlib.sha256(_canonical_json(manifest).encode()).hexdigest(),
        "result_sha256": hashlib.sha256(result_raw).hexdigest(),
        "initial_head": manifest["initial_head"], "commit_mode": "patches",
    }
    assert sorted(entry.name for entry in bench.wave_dir.iterdir()) == [
        manifest_path.name, bench.wave_file("result").name, bench.wave_file("receipt").name,
    ]

    retained = _snapshot(bench.wave_dir)
    again = bench.adapter("--dispatch-wave", "1")
    assert again.returncode == 0, again.stderr
    assert bench.launches() == 1, "a retained-complete wave must validate its receipt and never relaunch"
    assert _snapshot(bench.wave_dir) == retained
    assert _prepared(bench.adapter("--prepare-wave", "1"))["retained"] == "complete"

    bench.wave_file("receipt").chmod(0o600)
    bench.wave_file("receipt").write_text(json.dumps({"schema": "ffs.gsd-no-commit-completion/v1"}) + "\n")
    tampered = bench.adapter("--dispatch-wave", "1")
    assert tampered.returncode != 0 and "receipt" in tampered.stderr
    assert bench.launches() == 1


def test_dispatch_wave_never_relaunches_over_a_partial_publication(bench: Bench) -> None:
    prepared = _prepared(bench.adapter("--prepare-wave", "1"))
    written = bench.adapter("--write-wave-manifest", "1", stdin=json.dumps(_manifest_for(prepared)))
    assert written.returncode == 0, written.stderr
    bench.wave_file("result").write_text("interrupted publish")
    bench.wave_file("result").chmod(0o600)
    before = _snapshot(bench.wave_dir)
    result = bench.adapter("--dispatch-wave", "1")
    assert result.returncode != 0 and result.stderr.strip()
    assert bench.launches() == 0
    assert _snapshot(bench.wave_dir) == before


def _doc_section(package: Path) -> str:
    return (package / DOC_RELATIVE).read_text().split(SECTION_HEADING, 1)[1]


def _fenced_bash_blocks(section: str) -> list[str]:
    found = re.findall(r"^[ \t]*```bash\n(.*?)^[ \t]*```[ \t]*$", section, re.S | re.M)
    return [textwrap.dedent(block) for block in found]


def _run_block(bench: Bench, block: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """A fresh `bash -euo pipefail` with only the documented environment."""
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", block], cwd=bench.workspace, env=env,
        text=True, capture_output=True, check=False,
    )


@pytest.mark.parametrize("host_variable", ["CODEX_HOME", "CLAUDE_CONFIG_DIR"])
def test_m5b_each_documented_block_runs_alone_in_a_fresh_shell_and_the_wave_completes(
    bench: Bench, host_variable: str,
) -> None:
    section = _doc_section(bench.package)
    blocks = _fenced_bash_blocks(section)
    for block in blocks:
        assert "node -e" not in block, "the outer model must not hand-run node glue"
        for glue in ("gsd_run", "FFS_ADMISSION_BINDING", "mktemp", "TMPDIR"):
            assert glue not in block, f"{glue} is hand-copied binding glue"
        referenced = set(re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", block))
        assigned = set(re.findall(
            r"(?m)(?:^|[;&|({]\s*|\bexport\s+|\bdo\s+)([A-Za-z_][A-Za-z0-9_]*)=", block,
        ))
        assert referenced <= DOCUMENTED_INPUTS | assigned, (
            f"variable defined only in another block: {sorted(referenced - DOCUMENTED_INPUTS - assigned)}"
        )
    assert "FFS_ADMISSION_BINDING" not in section
    assert [tuple(mode in block for mode in MODES) for block in blocks] == [
        (True, False, False), (False, True, False), (False, False, True),
    ]

    environment = bench.env(CODEX_HOME=None, CLAUDE_CONFIG_DIR=None, FFS_WAVE_NUMBER="1")
    environment[host_variable] = str(bench.package)
    prepare = _run_block(bench, blocks[0], environment)
    prepared = _prepared(prepare)
    assert prepared["retained"] == "none"
    manifest = _manifest_for(prepared)

    write_environment = environment | {"FFS_WAVE_MANIFEST_JSON": json.dumps(manifest)}
    written = _run_block(bench, blocks[1], write_environment)
    assert written.returncode == 0, written.stderr
    dispatched = _run_block(bench, blocks[2], environment)
    assert dispatched.returncode == 0, dispatched.stderr

    assert bench.launches() == 1
    assert json.loads(bench.wave_file("result").read_text())["results"][0]["status"] == "complete"
    assert bench.wave_file("receipt").is_file()
    assert _prepared(_run_block(bench, blocks[0], environment))["retained"] == "complete"
    assert _run_block(bench, blocks[2], environment).returncode == 0
    assert bench.launches() == 1


# ---------------------------------------------------------------------------
# r1 review findings (Codex cross-vendor review of 45c2ff7)
# ---------------------------------------------------------------------------

STUB_FS_FAILURE = """\
const fs = require('node:fs');
const real = { closeSync: fs.closeSync };
const kind = process.env.FAKE_FS_FAILURE;
if (kind === 'fchmod') {
  fs.fchmodSync = () => { const e = new Error('EPERM: operation not permitted, fchmod'); e.code = 'EPERM'; throw e; };
}
if (kind === 'close') {
  fs.closeSync = (fd) => {
    real.closeSync(fd);
    if (new Error().stack.includes('privateDirectory')) {
      const e = new Error('EIO: i/o error, close'); e.code = 'EIO'; throw e;
    }
  };
}
"""


def _written_wave(bench: Bench, wave: int = 1, **overrides: object) -> dict:
    """prepare-wave then write the manifest through the writer; returns the manifest."""
    prepared = _prepared(bench.adapter("--prepare-wave", str(wave)))
    manifest = _manifest_for(prepared) | overrides
    written = bench.adapter("--write-wave-manifest", str(wave), stdin=json.dumps(manifest))
    assert written.returncode == 0, written.stderr
    return manifest


def test_dispatch_wave_never_relaunches_after_an_uncertain_launch(bench: Bench) -> None:
    """r1-1: the removed template refused a manifest-only set on re-run. A launch
    that died before it published a result leaves exactly that set; a second
    dispatch-wave must refuse, not launch the same manifest again."""
    _written_wave(bench)
    crashed = bench.adapter("--dispatch-wave", "1", env=bench.env(FAKE_SUPERVISOR_EXIT="3"))
    assert crashed.returncode != 0 and bench.launches() == 1
    assert not bench.wave_file("result").exists() and not bench.wave_file("receipt").exists()
    before = _snapshot(bench.root)
    again = bench.adapter("--dispatch-wave", "1")
    _assert_refused(again, "uncertain launch")
    assert bench.launches() == 1, "an uncertain launch must never be relaunched"
    assert _snapshot(bench.root) == before
    # The uncertain set blocks every other mode too, exactly like any partial set.
    _assert_refused(bench.adapter("--prepare-wave", "1"), "partial retained wave evidence")
    _assert_refused(
        bench.adapter("--write-wave-manifest", "1", stdin=_stdin_for("--write-wave-manifest", bench)),
        "partial retained wave evidence",
    )


def test_concurrent_dispatch_wave_launches_the_wave_once(bench: Bench) -> None:
    """r1-1: a durable exclusive claim is taken before the launch, so a second
    dispatch-wave started while the first is in flight refuses."""
    _written_wave(bench)
    manifest_name = bench.wave_file("manifest").name
    first = subprocess.Popen(
        ["node", str(bench.package / ADAPTER_RELATIVE), "--dispatch-wave", "1"], cwd=bench.workspace,
        env=bench.env(FAKE_SUPERVISOR_SLEEP="3"), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 20
        while bench.launches() < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert bench.launches() == 1, "the first supervisor never started"
        claims = [entry for entry in bench.wave_dir.iterdir() if entry.name != manifest_name]
        assert len(claims) == 1, [entry.name for entry in claims]
        assert claims[0].stat().st_mode & 0o777 == 0o600 and not claims[0].is_symlink()
        second = bench.adapter("--dispatch-wave", "1")
        _assert_refused(second, "uncertain launch")
        assert bench.launches() == 1
    finally:
        _, first_error = first.communicate(timeout=60)
    assert first.returncode == 0, first_error
    assert bench.launches() == 1
    assert sorted(entry.name for entry in bench.wave_dir.iterdir()) == [
        manifest_name, bench.wave_file("result").name, bench.wave_file("receipt").name,
    ], "a completed wave keeps exactly its manifest, result and receipt"


def test_the_wave_directory_is_never_enumerated_by_the_run_state_readers() -> None:
    """r1-1: the dispatch claim lives in the wave directory. The supervisor,
    wave_candidate and candidate_chain readers build explicit wave-N paths and
    never list the directory, and the workspace inventory prunes the whole
    .planning/.ffs-supervised subtree. (Coverage: passes on 45c2ff7 too.)"""
    sys.path.insert(0, str(ROOT / "lib"))
    from run_state.wave_execution import _internal_path

    assert _internal_path(f".planning/.ffs-supervised/waves/{ACTIVITY}/wave-1.dispatch-claim.json")
    for reader in ("supervisor.py", "wave_candidate.py", "candidate_chain.py"):
        source = (ROOT / "lib" / "run_state" / reader).read_text()
        assert not re.search(r"iterdir|\.glob\(|rglob|listdir|scandir|os\.walk", source), reader


@pytest.mark.parametrize("override", [
    {"wave": 2}, {"admission_run_id": "another-run"}, {"orchestrator_root": "elsewhere"},
], ids=["wave", "admission", "orchestrator-root"])
def test_dispatch_wave_refuses_a_retained_manifest_that_is_not_for_this_wave(
    bench: Bench, override: dict,
) -> None:
    """r1-2: dispatch-wave must run the same manifest binding as the writer."""
    prepared = _prepared(bench.adapter("--prepare-wave", "1"))
    manifest = _manifest_for(prepared)
    if "admission_run_id" in override:
        manifest["admission"] = prepared["admission"] | {"run_id": override["admission_run_id"]}
    elif "orchestrator_root" in override:
        manifest["orchestrator_root"] = str(bench.root)
    else:
        manifest |= override
    legacy = bench.adapter("--write-manifest", str(bench.wave_file("manifest")), stdin=json.dumps(manifest))
    assert legacy.returncode == 0, legacy.stderr
    before = _snapshot(bench.root)
    result = bench.adapter("--dispatch-wave", "1")
    _assert_refused(result, next(iter(override)).split("_")[0])
    assert bench.launches() == 0
    assert _snapshot(bench.root) == before


def test_dispatch_wave_refuses_another_waves_complete_evidence(bench: Bench) -> None:
    """r1-2: a complete, receipt-valid set copied from wave 2 into wave 1's
    names must not satisfy dispatch-wave 1."""
    _written_wave(bench, wave=2)
    dispatched = bench.adapter("--dispatch-wave", "2")
    assert dispatched.returncode == 0, dispatched.stderr
    bench.seed_wave_dir()
    for kind in ("manifest", "result", "receipt"):
        bench.wave_file(kind, 1).write_bytes(bench.wave_file(kind, 2).read_bytes())
        bench.wave_file(kind, 1).chmod(0o600)
    before = _snapshot(bench.root)
    _assert_refused(bench.adapter("--dispatch-wave", "1"), "wave")
    assert bench.launches() == 1
    assert _snapshot(bench.root) == before


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_a_symlinked_or_non_directory_planning_is_refused_before_anything_is_created(
    bench: Bench, mode: str, kind: str,
) -> None:
    """r1-3: mkdir resolved the path string, so a symlinked .planning redirected
    the private directories outside the workspace."""
    decoy = bench.root / "decoy"
    decoy.mkdir()
    planning = bench.workspace / ".planning"
    planning.rmdir()
    if kind == "symlink":
        planning.symlink_to(decoy)
    else:
        planning.write_text("not a directory")
    before = _snapshot(bench.root)
    result = bench.adapter(mode, "1", stdin=_stdin_for(mode, bench))
    _assert_refused(result, ".planning must be a real directory")
    assert list(decoy.iterdir()) == []
    assert _snapshot(bench.root) == before
    assert bench.launches() == 0


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("failure", ["fchmod", "close"])
def test_a_chmod_or_close_failure_while_binding_is_a_named_refusal(
    bench: Bench, tmp_path: Path, mode: str, failure: str,
) -> None:
    """r1-4: privateDirectory's fchmod/close ran outside the Refusal conversion,
    so a failure exited 1 with a raw errno message."""
    stub = tmp_path / "stub-fs-failure.cjs"
    stub.write_text(STUB_FS_FAILURE)
    environment = bench.env(NODE_OPTIONS=f"--require {stub}", FAKE_FS_FAILURE=failure)
    result = bench.adapter(mode, "1", stdin=_stdin_for(mode, bench), env=environment)
    _assert_refused(result, "0700")


@pytest.mark.parametrize("runtime_home", ["unset", "empty"])
@pytest.mark.parametrize("index", [0, 1, 2], ids=["prepare", "write", "dispatch"])
def test_each_documented_block_refuses_before_node_when_no_runtime_home_is_set(
    bench: Bench, tmp_path: Path, index: int, runtime_home: str,
) -> None:
    """r1-5: with neither CODEX_HOME nor CLAUDE_CONFIG_DIR the adapter path
    expanded to /gsd-core/... and node failed with exit 1. The block must name
    the missing variables and exit 78 without invoking node."""
    marker = tmp_path / "node-invoked"
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "node").write_text(f"#!/bin/sh\necho invoked >> {marker}\nexit 1\n")
    (shim / "node").chmod(0o755)
    block = _fenced_bash_blocks(_doc_section(bench.package))[index]
    blank = None if runtime_home == "unset" else ""
    environment = bench.env(
        CODEX_HOME=blank, CLAUDE_CONFIG_DIR=blank, FFS_WAVE_NUMBER="1", FFS_WAVE_MANIFEST_JSON="{}",
        PATH=f"{shim}:{os.environ['PATH']}",
    )
    result = _run_block(bench, block, environment)
    assert not marker.exists(), "node must not run without a runtime home"
    assert result.returncode == 78, (result.returncode, result.stderr)
    assert "CODEX_HOME" in result.stderr and "CLAUDE_CONFIG_DIR" in result.stderr


def test_a_wave_mode_is_recognized_only_as_the_first_argument(bench: Bench, tmp_path: Path) -> None:
    """r1-6: a legacy form whose value happens to be a mode name keeps its
    former behavior: here --write-manifest writes a file literally named
    --dispatch-wave."""
    legacy_cwd = tmp_path / "legacy-cwd"
    legacy_cwd.mkdir()
    result = bench.adapter(
        "--write-manifest", "--dispatch-wave", stdin=_stdin_for("--write-wave-manifest", bench), cwd=legacy_cwd,
    )
    assert result.returncode == 0, result.stderr
    assert [entry.name for entry in legacy_cwd.iterdir()] == ["--dispatch-wave"]
    assert bench.launches() == 0


@pytest.mark.parametrize("kind", ["result", "receipt"])
def test_write_wave_manifest_refuses_a_lone_result_or_receipt_and_writes_no_manifest(
    bench: Bench, kind: str,
) -> None:
    """r1-7: the doc assertion for the retained-is-none guard matches prose;
    this pins the behavior. (Coverage: passes on 45c2ff7 too.)"""
    bench.seed_wave_dir()
    bench.wave_file(kind).write_text(f"retained {kind}")
    before = _snapshot(bench.root)
    result = bench.adapter("--write-wave-manifest", "1", stdin=_stdin_for("--write-wave-manifest", bench))
    _assert_refused(result, "partial retained wave evidence")
    assert not bench.wave_file("manifest").exists()
    assert _snapshot(bench.root) == before


def test_a_leftover_dispatch_claim_beside_a_complete_set_neither_blocks_nor_relaunches(bench: Bench) -> None:
    """r1-1: the claim is removed once the receipt exists; if a crash left it
    behind, the complete set still validates and nothing launches."""
    _written_wave(bench)
    assert bench.adapter("--dispatch-wave", "1").returncode == 0
    bench.wave_dir.joinpath("wave-1.dispatch-claim.json").write_text("{}\n")
    bench.wave_dir.joinpath("wave-1.dispatch-claim.json").chmod(0o600)
    assert _prepared(bench.adapter("--prepare-wave", "1"))["retained"] == "complete"
    again = bench.adapter("--dispatch-wave", "1")
    assert again.returncode == 0, again.stderr
    assert bench.launches() == 1


STUB_CLAIM_GATE = """\
const fs = require('node:fs');
const gate = process.env.FAKE_CLAIM_GATE;
const parked = process.env.FAKE_CLAIM_PARKED;
if (gate && parked) {
  const realOpen = fs.openSync;
  fs.openSync = function (file, ...rest) {
    if (typeof file === 'string' && file.endsWith('.dispatch-claim.json')) {
      fs.writeFileSync(parked, JSON.stringify({
        pid: process.pid, detached: process.env.FFS_SUPERVISED_DISPATCH_DETACHED === '1',
      }));
      const sleeper = new Int32Array(new SharedArrayBuffer(4));
      while (!fs.existsSync(gate)) Atomics.wait(sleeper, 0, 0, 20);
    }
    return realOpen.call(this, file, ...rest);
  };
}
"""


def test_a_dispatch_that_claims_after_the_winner_released_does_not_relaunch(
    bench: Bench, tmp_path: Path,
) -> None:
    """r1-1 residual: P2 passes the result/receipt exists() check, the winner P1
    then claims, launches, publishes and releases, and P2's claim succeeds. P2
    must re-check after claiming and take the receipt-validation path.

    The race is made deterministic with a --require preload that parks the
    process opening a *.dispatch-claim.json (the claim create) until a gate file
    exists. NODE_OPTIONS reaches the detached child through its inherited
    environment; the parked marker proves the preload ran in the child that
    claims, not in the foreground."""
    _written_wave(bench)
    preload = tmp_path / "claim-gate.cjs"
    preload.write_text(STUB_CLAIM_GATE)
    gate, parked = tmp_path / "gate", tmp_path / "parked.json"
    second_environment = bench.env(
        NODE_OPTIONS=f"--require {preload}", FAKE_CLAIM_GATE=str(gate), FAKE_CLAIM_PARKED=str(parked),
    )
    second = subprocess.Popen(
        ["node", str(bench.package / ADAPTER_RELATIVE), "--dispatch-wave", "1"], cwd=bench.workspace,
        env=second_environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while not parked.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert parked.exists(), "the preload never parked the process that creates the claim"
        marker = json.loads(parked.read_text())
        assert marker["detached"] is True and marker["pid"] != second.pid
        assert bench.launches() == 0, "P2 must be parked before it launches"
        winner = bench.adapter("--dispatch-wave", "1")
        assert winner.returncode == 0, winner.stderr
        assert bench.launches() == 1
        assert sorted(entry.name for entry in bench.wave_dir.iterdir()) == [
            bench.wave_file("manifest").name, bench.wave_file("result").name, bench.wave_file("receipt").name,
        ], "the winner published and released its claim"
    finally:
        gate.write_text("go")
        _, second_error = second.communicate(timeout=60)
    assert bench.launches() == 1, "the late claimer launched the wave a second time"
    # P2 validates the winner's receipt and returns normally (0), not a refusal.
    assert second.returncode == 0, second_error
    assert sorted(entry.name for entry in bench.wave_dir.iterdir()) == [
        bench.wave_file("manifest").name, bench.wave_file("result").name, bench.wave_file("receipt").name,
    ], "the late claimer released its own claim"
